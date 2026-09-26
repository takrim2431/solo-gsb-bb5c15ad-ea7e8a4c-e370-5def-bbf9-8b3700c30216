"""Import, rule evaluation and timeline services built on the engine + db."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterable

from . import db
from .audit import audit_cadence
from .consistency import check_consistency
from .pairing import PairableFault, pair_faults
from .recovery import diagnose_mutation_recovery
from .engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    cluster_intervals,
    daily_fault_ledger,
    diff_faults,
    evaluate_rule,
    to_epoch,
    to_number,
    union,
)

DEFAULT_DEVICE_FIELD = "device"
DEFAULT_METRIC_FIELD = "metric"
DEFAULT_TS_FIELD = "ts"
DEFAULT_VALUE_FIELD = "value"


def parse_jsonl(text: str) -> list[dict]:
    """Parse JSONL; a top-level JSON array is also accepted."""
    text = text.strip()
    if not text:
        return []
    if text[0] == "[":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON body must be an array of event objects")
        return [r for r in data if isinstance(r, dict)]

    rows: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {lineno}: invalid JSON ({exc.msg})") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"line {lineno}: event must be a JSON object")
        rows.append(obj)
    return rows


def _extract_metrics(
    row: dict, device_field: str, metric_field: str, ts_field: str, value_field: str
) -> list[tuple[str, str, object]]:
    """Turn one raw row into (device, metric, raw_value) triples.

    Two shapes are supported:
    1. flat  - {"device": "d", "metric": "m", "ts": ..., "value": 3.1}
    2. nested - {"device": "d", "ts": ..., "value": {"temp": 1, "vib": 0.2}}
       (one event per key of the value object; the metric column is ignored)
    """
    device = row.get(device_field)
    if device is None:
        raise ValueError("missing device")
    raw_ts = row.get(ts_field)
    if raw_ts is None:
        raise ValueError("missing timestamp")

    value = row.get(value_field) if value_field in row else row.get(DEFAULT_VALUE_FIELD)
    if isinstance(value, dict):
        if not value:
            raise ValueError("empty nested metric object")
        return [(str(device), str(sub_metric), sub_value) for sub_metric, sub_value in value.items()]

    metric = row.get(metric_field)
    if metric is None:
        raise ValueError("missing metric")
    return [(str(device), str(metric), value)]


def import_events(
    text: str,
    filename: str | None = None,
    device_field: str = DEFAULT_DEVICE_FIELD,
    metric_field: str = DEFAULT_METRIC_FIELD,
    ts_field: str = DEFAULT_TS_FIELD,
    value_field: str = DEFAULT_VALUE_FIELD,
    reevaluate: bool = True,
) -> dict:
    rows = parse_jsonl(text)
    now = time.time()

    inserted = 0
    overwritten = 0
    skipped: list[dict] = []
    seen_keys: set[tuple[str, str, float]] = set()
    with db.transaction() as tx:
        cur = tx.execute(
            "INSERT INTO imports (filename, imported_at, total_rows, snapshot_complete)"
            " VALUES (?,?,?,1)",
            (filename, now, len(rows)),
        )
        import_id = cur.lastrowid
        for index, row in enumerate(rows):
            try:
                triples = _extract_metrics(
                    row, device_field, metric_field, ts_field, value_field
                )
                ts = to_epoch(row[ts_field])
            except (ValueError, KeyError, TypeError) as exc:
                skipped.append({"line": index + 1, "reason": str(exc)})
                continue
            for device, metric, raw_value in triples:
                num = to_number(raw_value)
                if num is None:
                    skipped.append(
                        {"line": index + 1, "reason": f"non-numeric value for {device}/{metric}"}
                    )
                    continue
                # Snapshot the pre-import state on this batch's first encounter
                # of the key; later duplicates in the same batch still overwrite
                # (the last valid value wins) but must not re-snapshot.
                db.snapshot_import_key(tx, import_id, seen_keys, device, metric, ts)
                is_new = db.upsert_event(
                    tx, device, metric, ts, num, json.dumps(row, ensure_ascii=False), import_id
                )
                if is_new:
                    inserted += 1
                else:
                    overwritten += 1
        tx.execute(
            "UPDATE imports SET inserted_rows = ?, overwritten_rows = ?, skipped_rows = ?"
            " WHERE id = ?",
            (inserted, overwritten, len(skipped), import_id),
        )

    result = {
        "import_id": import_id,
        "total_rows": len(rows),
        "inserted": inserted,
        "overwritten": overwritten,
        "skipped": len(skipped),
        "skipped_details": skipped[:100],
    }
    # New or back-filled samples — including older timestamps — change held
    # intervals and can bridge or open TTL gaps, so stored faults are
    # recomputed immediately (maintenance windows keep participating).
    if reevaluate:
        evaluation = evaluate_all_rules()
        result["evaluation"] = {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        }
    return result


class UndoNotAllowed(Exception):
    """Raised when an import batch cannot be revoked (409 at the API)."""


def list_import_batches() -> list[dict]:
    """All import batches, newest first, with their undo eligibility."""
    with db.read_snapshot():
        latest = db.query_one(
            "SELECT MAX(id) AS id FROM imports WHERE revoked_at IS NULL"
        )
        latest_id = latest["id"] if latest is not None else None
        rows = db.query(
            """
            SELECT i.*,
                   (SELECT COUNT(*) FROM import_snapshots s WHERE s.import_id = i.id) AS snapshot_rows,
                   (SELECT SUM(CASE WHEN s.event_id IS NOT NULL THEN 1 ELSE 0 END)
                      FROM import_snapshots s WHERE s.import_id = i.id) AS overwritten_keys
            FROM imports i
            ORDER BY i.id DESC
            """
        )
        batches: list[dict] = []
        for row in rows:
            batch = {
                "id": row["id"],
                "filename": row["filename"],
                "imported_at": row["imported_at"],
                "total_rows": row["total_rows"],
                "inserted_rows": row["inserted_rows"],
                "overwritten_rows": row["overwritten_rows"],
                "skipped_rows": row["skipped_rows"],
                "snapshot_keys": row["snapshot_rows"],
                "overwritten_keys": row["overwritten_keys"] or 0,
                "snapshot_complete": bool(row["snapshot_complete"]),
                "revoked": row["revoked_at"] is not None,
                "revoked_at": row["revoked_at"],
                "can_undo": False,
                "undo_reason": None,
            }
            if batch["revoked"]:
                batch["undo_reason"] = "该批次已撤销"
            elif not batch["snapshot_complete"]:
                # Batch imported before the undo feature: nothing to restore from.
                batch["undo_reason"] = "旧批次缺少导入前快照，无法撤销"
            elif row["id"] != latest_id:
                batch["undo_reason"] = "仅允许撤销最近一次未撤销的导入批次"
            else:
                batch["can_undo"] = True
            batches.append(batch)
    return batches


def undo_import(import_id: int, reevaluate: bool = True) -> dict:
    """Revoke one import batch atomically.

    Only the most recent non-revoked batch whose pre-import snapshots are
    complete may be revoked.  In a single transaction the batch's newly
    inserted events are deleted, while events it overwritten get back their
    previous value, raw content and original batch ownership; the batch is
    then marked revoked.  Any failure rolls the whole operation back.
    """
    row = db.query_one("SELECT * FROM imports WHERE id = ?", (import_id,))
    if row is None:
        raise KeyError(f"import batch {import_id} not found")
    if row["revoked_at"] is not None:
        raise UndoNotAllowed("该导入批次已撤销，不能重复撤销")
    if not row["snapshot_complete"]:
        raise UndoNotAllowed("该批次导入时未记录快照（旧数据），无法撤销")
    latest = db.query_one(
        "SELECT MAX(id) AS id FROM imports WHERE revoked_at IS NULL"
    )
    if latest is None or latest["id"] != import_id:
        raise UndoNotAllowed("仅允许撤销最近一次导入批次，请先撤销其后的导入")

    with db.transaction() as tx:
        snapshots = tx.execute(
            "SELECT * FROM import_snapshots WHERE import_id = ?"
            " ORDER BY id",
            (import_id,),
        ).fetchall()
        deleted = 0
        restored = 0
        for snap in snapshots:
            if snap["event_id"] is None:
                # The batch created this key: delete the current event, but
                # only if it still belongs to the batch being revoked.
                cur = tx.execute(
                    "DELETE FROM events"
                    " WHERE device = ? AND metric = ? AND ts = ? AND import_id = ?",
                    (snap["device"], snap["metric"], snap["ts"], import_id),
                )
                if cur.rowcount == 0:
                    raise RuntimeError(
                        f"snapshot consistency broken for new key"
                        f" {snap['device']}/{snap['metric']}@{snap['ts']}"
                    )
                deleted += cur.rowcount
            else:
                # The batch overwrote a pre-existing event: put its value,
                # raw content and original batch ownership back.
                cur = tx.execute(
                    "UPDATE events SET value = ?, raw = ?, import_id = ?"
                    " WHERE id = ? AND import_id = ?",
                    (
                        snap["value"],
                        snap["raw"],
                        snap["origin_import_id"],
                        snap["event_id"],
                        import_id,
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"snapshot consistency broken for event {snap['event_id']}"
                    )
                restored += 1
        tx.execute(
            "UPDATE imports SET revoked_at = ? WHERE id = ?",
            (time.time(), import_id),
        )

    result = {
        "import_id": import_id,
        "deleted_events": deleted,
        "restored_events": restored,
    }
    # Faults derive from the current events, so the existing evaluation flow
    # refreshes them after the data is back to its pre-batch state.
    if reevaluate:
        evaluation = evaluate_all_rules()
        result["evaluation"] = {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        }
    return result


def _load_events_index(
    conn: sqlite3.Connection | None = None,
) -> tuple[dict[tuple[str, str], list[Event]], float | None]:
    events_by_channel: dict[tuple[str, str], list[Event]] = defaultdict(list)
    horizon: float | None = None
    for row in db.query(
        "SELECT id, device, metric, ts, value FROM events ORDER BY ts, id", conn=conn
    ):
        if row["value"] is None:
            continue
        ev = Event(
            id=row["id"],
            device=row["device"],
            metric=row["metric"],
            ts=row["ts"],
            value=row["value"],
        )
        events_by_channel[(ev.device, ev.metric)].append(ev)
        horizon = ev.ts if horizon is None else max(horizon, ev.ts)
    return events_by_channel, horizon


def _load_maintenance_windows(
    conn: sqlite3.Connection | None = None,
) -> list[MaintenanceWindow]:
    """All stored planned-maintenance windows as engine inputs."""
    return [
        MaintenanceWindow(
            id=w["id"],
            device=w["device"],
            start=w["start"],
            end=w["end"],
            name=w["name"],
        )
        for w in db.fetch_maintenance_windows(conn=conn)
    ]


def _load_channel_ttls(
    conn: sqlite3.Connection | None = None,
) -> dict[tuple[str, str], float]:
    """Configured per-channel sample validity periods as engine input.

    Only channels with a positive configured TTL appear here; every other
    channel keeps the legacy hold-to-next-sample / hold-to-horizon semantics.
    """
    return {
        (row["device"], row["metric"]): row["ttl_seconds"]
        for row in db.fetch_channel_ttls(conn=conn)
    }


def evaluate_all_rules() -> dict:
    """Re-evaluate every rule and replace its stored fault intervals.

    Planned-maintenance windows are merged per device and subtracted from the
    truth segments before the duration check, so a maintenance interruption
    resets the sustained duration and re-arms confirmation after the window.
    Per-channel sample validity periods cap each sample's held interval, so
    expired stretches satisfy no condition and likewise reset the duration.
    The suppressed pieces are stored alongside for the timeline/export.

    The input reads and the fault/suppressed replacement run inside **one
    serialized write transaction** (the same write lock every event import,
    rule, validity-period and maintenance-window write funnels through), so
    the inputs read here are the latest committed state at evaluation time
    and no concurrent commit can land between the read and the replacement:
    a stale in-flight evaluation can never overwrite a newer committed
    recompute, and whichever evaluation commits last always reflects the
    latest committed inputs.  Any failure rolls the whole transaction back,
    so a failed evaluation never leaves partially replaced faults behind,
    and readers only ever see the committed results.
    """
    with db.transaction() as tx:
        # Gather every evaluation input on the writer connection, inside the
        # replacement's own transaction: the read and the write serialize
        # with all other writers in the same order.
        rules = db.fetch_rules(conn=tx)
        events_by_channel, horizon = _load_events_index(tx)
        if horizon is None:
            horizon = 0.0
        maintenance = _load_maintenance_windows(tx)
        ttl_by_channel = _load_channel_ttls(tx)

        total = 0
        total_suppressed = 0
        per_rule: list[dict] = []
        for rule in rules:
            conditions = [Condition.from_dict(c) for c in rule["conditions"]]
            result = evaluate_rule(
                rule_id=rule["id"],
                logic=rule["logic"],
                duration_required=rule["duration"],
                conditions=conditions,
                events_by_channel=events_by_channel,
                horizon=horizon,
                maintenance=maintenance,
                ttl_by_channel=ttl_by_channel,
            )
            tx.execute("DELETE FROM fault_intervals WHERE rule_id = ?", (rule["id"],))
            tx.execute("DELETE FROM suppressed_segments WHERE rule_id = ?", (rule["id"],))
            for f in result.faults:
                tx.execute(
                    """
                    INSERT INTO fault_intervals
                        (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        f.rule_id,
                        f.start,
                        f.end,
                        f.duration_required,
                        f.confirmed_at,
                        json.dumps([e.to_dict() for e in f.evidence], ensure_ascii=False),
                    ),
                )
            for s in result.suppressed:
                tx.execute(
                    """
                    INSERT INTO suppressed_segments
                        (rule_id, device, metric, start_ts, end_ts, maintenance_ids)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        rule["id"],
                        s.device,
                        s.metric,
                        s.interval.start,
                        s.interval.end,
                        json.dumps(s.maintenance_ids),
                    ),
                )
            total += len(result.faults)
            total_suppressed += len(result.suppressed)
            per_rule.append(
                {
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "faults": len(result.faults),
                    "suppressed": len(result.suppressed),
                }
            )

    return {
        "rules_evaluated": len(rules),
        "fault_intervals": total,
        "suppressed_segments": total_suppressed,
        "maintenance_windows": len(maintenance),
        "per_rule": per_rule,
    }


def _fault_outline(fault) -> dict:
    """Compact fault payload for the impact preview (evidence is irrelevant)."""
    return {
        "rule_id": fault.rule_id,
        "start": fault.start,
        "end": fault.end,
        "duration_required": fault.duration_required,
        "confirmed_at": fault.confirmed_at,
    }


def preview_rule(rule_id: int | None, draft: dict) -> dict:
    """Evaluate a not-yet-saved rule draft against the live data, read-only.

    The baseline is the rule *as currently stored* (or nothing for a new
    rule), the candidate is the unsaved draft.  Both are evaluated right now
    with the current events, the currently stored planned-maintenance windows
    and the configured per-channel sample validity periods — exactly what a
    save + re-evaluation would see — but nothing is written to the rules or
    fault tables.  The two confirmed fault sets are compared as half-open
    interval coverings (see :func:`diff_faults`).
    """
    events_by_channel, horizon = _load_events_index()
    if horizon is None:
        horizon = 0.0
    maintenance = _load_maintenance_windows()
    ttl_by_channel = _load_channel_ttls()

    baseline_rule = db.fetch_rule(rule_id) if rule_id is not None else None
    if rule_id is not None and baseline_rule is None:
        raise KeyError(f"rule {rule_id} not found")

    def run(rule: dict, rid: int):
        return evaluate_rule(
            rule_id=rid,
            logic=rule["logic"],
            duration_required=rule["duration"],
            conditions=[Condition.from_dict(c) for c in rule["conditions"]],
            events_by_channel=events_by_channel,
            horizon=horizon,
            maintenance=maintenance,
            ttl_by_channel=ttl_by_channel,
        )

    baseline_result = (
        run(baseline_rule, baseline_rule["id"]) if baseline_rule is not None else None
    )
    draft_result = run(draft, rule_id if rule_id is not None else -1)

    baseline_faults = baseline_result.faults if baseline_result is not None else []
    segments = diff_faults(baseline_faults, draft_result.faults)
    counts = {"added": 0, "removed": 0, "overlap_changed": 0, "overlap_unchanged": 0}
    for seg in segments:
        counts[seg.kind] += 1

    return {
        "rule_id": rule_id,
        "mode": "edit" if baseline_rule is not None else "create",
        "baseline": {
            "name": baseline_rule["name"] if baseline_rule is not None else None,
            "faults": [_fault_outline(f) for f in baseline_faults],
        },
        "draft": {
            "name": draft["name"],
            "faults": [_fault_outline(f) for f in draft_result.faults],
        },
        "maintenance_windows": len(maintenance),
        "channel_ttls": len(ttl_by_channel),
        "diff": {
            "segments": [seg.to_dict() for seg in segments],
            "counts": counts,
            "changed": counts["added"] + counts["removed"] + counts["overlap_changed"] > 0,
        },
    }


def list_channels() -> list[dict]:
    rows = db.query(
        """
        SELECT device, metric, COUNT(*) AS n, MIN(ts) AS min_ts, MAX(ts) AS max_ts,
               MIN(value) AS min_value, MAX(value) AS max_value
        FROM events WHERE value IS NOT NULL
        GROUP BY device, metric ORDER BY device, metric
        """
    )
    return [dict(r) for r in rows]


def list_channel_ttls() -> list[dict]:
    """Configured per-channel sample validity periods (positive seconds).

    Channels absent from the list have no TTL and keep legacy hold semantics.
    """
    return db.fetch_channel_ttls()


def set_channel_ttl(device: str, metric: str, ttl_seconds: float) -> dict:
    """Set/replace a channel's sample validity period and re-evaluate rules.

    Changing the TTL changes how every rule using the channel expands its
    samples, so the stored fault intervals are recomputed immediately.
    Planned maintenance keeps participating in the evaluation as usual.
    """
    ttl_seconds = float(ttl_seconds)
    if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive number of seconds")
    saved = db.upsert_channel_ttl(device, metric, ttl_seconds)
    evaluation = evaluate_all_rules()
    return {
        **saved,
        "evaluation": {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        },
    }


def clear_channel_ttl(device: str, metric: str) -> dict:
    """Remove a channel's validity period (back to legacy semantics) and re-evaluate."""
    existed = db.fetch_channel_ttl(device, metric)
    removed = db.delete_channel_ttl(device, metric)
    if not removed:
        raise KeyError(f"no sample validity configured for channel {device}/{metric}")
    evaluation = evaluate_all_rules()
    return {
        "cleared": {"device": device, "metric": metric, "had_ttl_seconds": existed["ttl_seconds"]},
        "evaluation": {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        },
    }


def list_events(
    start: float | None = None,
    end: float | None = None,
    device: str | None = None,
    metric: str | None = None,
    limit: int = 2000,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where = []
    params: list = []
    # Point events: inclusive range on both ends.
    if start is not None:
        where.append("ts >= ?")
        params.append(start)
    if end is not None:
        where.append("ts <= ?")
        params.append(end)
    if device:
        where.append("device = ?")
        params.append(device)
    if metric:
        where.append("metric = ?")
        params.append(metric)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    # COUNT and the page SELECT must read the same committed snapshot:
    # without pinning, a concurrent import commit between the two statements
    # could return a total that does not match the listed rows.
    with db.read_snapshot():
        total = db.query_one(f"SELECT COUNT(*) AS c FROM events{clause}", params)["c"]
        rows = db.query(
            f"SELECT id, device, metric, ts, value, import_id FROM events{clause} ORDER BY ts, id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
    return [dict(r) for r in rows], total


def query_faults(
    start: float | None = None, end: float | None = None
) -> list[dict]:
    where = []
    params: list = []
    if start is not None:
        where.append("end_ts > ?")  # half-open overlap: [start,end) ∩ range
        params.append(start)
    if end is not None:
        where.append("start_ts < ?")
        params.append(end)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"""
        SELECT f.*, r.name AS rule_name, r.logic
        FROM fault_intervals f JOIN rules r ON r.id = f.rule_id
        {clause} ORDER BY f.start_ts, f.rule_id
        """,
        params,
    )
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "logic": row["logic"],
                "start": row["start_ts"],
                "end": row["end_ts"],
                "duration_required": row["duration_required"],
                "confirmed_at": row["confirmed_at"],
                "evidence": json.loads(row["evidence_json"]),
            }
        )
    return out


def _overlaps_window(start: float, end: float, win_start: float | None, win_end: float | None) -> bool:
    """Half-open overlap with the filter window [win_start, win_end)."""
    if win_start is not None and end <= win_start:
        return False
    if win_end is not None and start >= win_end:
        return False
    return True


def _fault_member(fault: dict, in_window: bool) -> dict:
    """One member fault as embedded inside a fault event.

    The payload is a copy of the original stored fault (id, interval and
    trigger evidence untouched) plus the window-membership flag; the event
    view never rewrites fault results.
    """
    return {
        "fault_id": fault["id"],
        "rule_id": fault["rule_id"],
        "rule_name": fault["rule_name"],
        "logic": fault["logic"],
        "start": fault["start"],
        "end": fault["end"],
        "duration_required": fault["duration_required"],
        "confirmed_at": fault["confirmed_at"],
        "in_window": in_window,
        "evidence": fault["evidence"],
    }


def build_fault_events(
    faults: list[dict], start: float | None = None, end: float | None = None
) -> list[dict]:
    """Group faults from different rules into fault *events*.

    Two faults (typically produced by different rules) belong to the same
    event when their ``[start,end)`` intervals overlap or touch end-to-end;
    the relation is transitive, so chaining links are merged as well.  An
    event spans ``[min member start, max member end)``.

    Components are first computed over *all* faults (so chains crossing the
    filter window stay intact), then only events overlapping the
    half-open filter window are returned.  Members outside the window are
    still listed, flagged ``in_window: false``.
    """
    groups = cluster_intervals([Interval(f["start"], f["end"]) for f in faults])
    events: list[dict] = []
    for members in groups:
        member_faults = [faults[i] for i in members]
        ev_start = min(f["start"] for f in member_faults)
        ev_end = max(f["end"] for f in member_faults)
        if not _overlaps_window(ev_start, ev_end, start, end):
            continue
        member_payload = [
            _fault_member(f, _overlaps_window(f["start"], f["end"], start, end))
            for f in sorted(member_faults, key=lambda f: (f["start"], f["end"], f["id"]))
        ]
        rule_ids = sorted({f["rule_id"] for f in member_faults})
        events.append(
            {
                "start": ev_start,
                "end": ev_end,
                "rule_count": len(rule_ids),
                "fault_count": len(member_payload),
                "member_count_in_window": sum(1 for m in member_payload if m["in_window"]),
                "rule_ids": rule_ids,
                "rule_names": sorted({f["rule_name"] for f in member_faults}),
                "members": member_payload,
            }
        )
    events.sort(key=lambda e: (e["start"], e["end"]))
    # Sequential display id, stable within the queried time range.
    for index, event in enumerate(events, start=1):
        event["id"] = f"E{index}"
    return events


def query_fault_events(
    start: float | None = None, end: float | None = None
) -> list[dict]:
    """Fault events for the filter window; clustering sees every stored fault."""
    # The full fault set is read in one committed view so the clustering
    # never mixes intervals from before/after a concurrent rule recompute.
    with db.read_snapshot():
        faults = query_faults(None, None)
    return build_fault_events(faults, start, end)


def query_fault_daily_ledger(
    rule_ids: list[int], start: float, end: float
) -> dict:
    """Read-only per-day fault ledger for a combination of selected rules.

    The *already stored* confirmed fault intervals of the given rules are
    read verbatim (rule changes keep being recomputed by the existing
    evaluation flow), merged across rules as half-open intervals —
    overlapping or touching pieces become one — then split at UTC midnights
    within the half-open query window ``[start, end)``.  See
    :func:`app.engine.daily_fault_ledger` for the per-day metrics.

    Raises ``ValueError`` on an empty/inverted window and ``KeyError`` for
    unknown rule ids.
    """
    start = float(start)
    end = float(end)
    if end <= start:
        raise ValueError("ledger end must be greater than start")
    if not rule_ids:
        raise ValueError("at least one rule is required")

    seen: set[int] = set()
    unique_ids: list[int] = []
    for rid in rule_ids:
        rid = int(rid)
        if rid not in seen:
            seen.add(rid)
            unique_ids.append(rid)

    # Rule definitions and their stored faults come from one committed view.
    with db.read_snapshot():
        selected_rules: list[dict] = []
        missing: list[int] = []
        for rid in unique_ids:
            rule = db.fetch_rule(rid)
            if rule is None:
                missing.append(rid)
            else:
                selected_rules.append(rule)
        if missing:
            raise KeyError(f"rule(s) not found: {', '.join(str(i) for i in missing)}")

        faults = [
            f
            for f in query_faults(None, None)
            if f["rule_id"] in seen and f["end"] > start and f["start"] < end
        ]
    intervals = [Interval(f["start"], f["end"]) for f in faults]
    merged = [iv for iv in union(intervals) if iv.end > start and iv.start < end]
    days = daily_fault_ledger(intervals, start, end)

    total_fault_seconds = sum(d["fault_seconds"] for d in days)
    total_queried_seconds = sum(d["queried_seconds"] for d in days)
    return {
        "start": start,
        "end": end,
        "rules": [
            {"id": r["id"], "name": r["name"], "logic": r["logic"], "duration": r["duration"]}
            for r in selected_rules
        ],
        "merged_segments": [[iv.start, iv.end] for iv in merged],
        "days": days,
        "totals": {
            "fault_seconds": total_fault_seconds,
            "queried_seconds": total_queried_seconds,
            "new_segments": sum(d["new_segments"] for d in days),
            "continuing_segments": sum(d["continuing_segments"] for d in days),
            "merged_segments": len(merged),
            "coverage": (total_fault_seconds / total_queried_seconds) if total_queried_seconds else 0.0,
        },
    }


def query_sampling_audit(
    device: str,
    metric: str,
    start: float,
    end: float,
    period_seconds: float,
    tolerance_seconds: float,
) -> dict:
    """Read-only sampling-cadence audit for one existing channel.

    Reads the channel's actual events from the ``events`` table inside the
    half-open window ``[start, end)`` (events are a point set, so the closing
    bound is exclusive just like the audit window) and reconciles them against
    the expected beat grid anchored at ``start`` (see
    :func:`app.audit.audit_cadence`).  Nothing is written: events, faults and
    every other table are left untouched, and rule evaluation is never
    triggered.

    Raises ``KeyError`` when the (device, metric) channel has no events and
    ``ValueError`` for an invalid window / period / tolerance.
    """
    device = device.strip()
    metric = metric.strip()
    if not device or not metric:
        raise ValueError("device and metric are required")
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("audit end must be greater than start (half-open [start, end))")

    # Channel totals and the windowed events are read from one snapshot so
    # the reported channel metadata matches the reconciled samples.
    with db.read_snapshot():
        channel = db.query_one(
            "SELECT COUNT(*) AS c, MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM events"
            " WHERE device = ? AND metric = ?",
            (device, metric),
        )
        if channel is None or channel["c"] == 0:
            raise KeyError(f"channel not found: {device}/{metric}")

        rows = db.query(
            "SELECT ts FROM events WHERE device = ? AND metric = ? AND ts >= ? AND ts < ?"
            " ORDER BY ts, id",
            (device, metric, start, end),
        )
        result = audit_cadence(
            start,
            end,
            period_seconds,
            tolerance_seconds,
            (r["ts"] for r in rows),
        )
    payload = result.to_dict()
    payload["device"] = device
    payload["metric"] = metric
    payload["channel"] = {
        "device": device,
        "metric": metric,
        "event_count": channel["c"],
        "min_ts": channel["min_ts"],
        "max_ts": channel["max_ts"],
    }
    return payload


def _channel_samples_for_window(
    device: str, metric: str, start: float, end: float
) -> list[tuple[float, float]]:
    """Samples shaping a channel's held values over the window ``[start, end)``.

    Every sample inside the window plus the **last sample before the window
    start** — that sample's held interval may still reach into the window
    (an expired one clips away later), and older samples never can because
    their held interval ends at the next sample's timestamp at the latest.
    """
    before = db.query_one(
        "SELECT ts, value FROM events"
        " WHERE device = ? AND metric = ? AND value IS NOT NULL AND ts < ?"
        " ORDER BY ts DESC, id DESC LIMIT 1",
        (device, metric, start),
    )
    rows = db.query(
        "SELECT ts, value FROM events"
        " WHERE device = ? AND metric = ? AND value IS NOT NULL AND ts >= ? AND ts < ?"
        " ORDER BY ts, id",
        (device, metric, start, end),
    )
    samples = [(r["ts"], r["value"]) for r in rows]
    if before is not None:
        samples.insert(0, (before["ts"], before["value"]))
    return samples


def query_channel_consistency(
    device_a: str,
    metric_a: str,
    device_b: str,
    metric_b: str,
    start: float,
    end: float,
    threshold: float,
) -> dict:
    """Read-only consistency check between two existing (redundant) channels.

    Both channels' samples are expanded into held intervals over the
    half-open window ``[start, end)`` — honouring each channel's configured
    sample validity period (TTL) and the global data endpoint, and including
    the last sample before ``start`` while it is still valid — then compared
    only where both sides hold a value (see
    :func:`app.consistency.check_consistency`).  The query reads the current
    events on the fly: it never writes fault results and never triggers rule
    re-evaluation.

    Raises ``KeyError`` when either channel has no events and ``ValueError``
    for identical channels or an invalid window / threshold.
    """
    device_a = device_a.strip()
    metric_a = metric_a.strip()
    device_b = device_b.strip()
    metric_b = metric_b.strip()
    if not device_a or not metric_a or not device_b or not metric_b:
        raise ValueError("both channels (device + metric) are required")
    if (device_a, metric_a) == (device_b, metric_b):
        raise ValueError("consistency check requires two different channels")
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("check end must be greater than start (half-open [start, end))")
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be a finite non-negative number")

    def channel_info(device: str, metric: str) -> dict:
        row = db.query_one(
            "SELECT COUNT(*) AS c, MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM events"
            " WHERE device = ? AND metric = ?",
            (device, metric),
        )
        if row is None or row["c"] == 0:
            raise KeyError(f"channel not found: {device}/{metric}")
        return row

    # Both channels' samples, the horizon and the TTLs are read from one
    # committed snapshot so the comparison inputs never straddle a commit.
    with db.read_snapshot():
        info_a = channel_info(device_a, metric_a)
        info_b = channel_info(device_b, metric_b)

        horizon_row = db.query_one("SELECT MAX(ts) AS hi FROM events")
        horizon = horizon_row["hi"] if horizon_row and horizon_row["hi"] is not None else 0.0

        ttl_a = db.fetch_channel_ttl(device_a, metric_a)
        ttl_b = db.fetch_channel_ttl(device_b, metric_b)

        result = check_consistency(
            _channel_samples_for_window(device_a, metric_a, start, end),
            _channel_samples_for_window(device_b, metric_b, start, end),
            horizon,
            start,
            end,
            threshold,
            ttl_a=ttl_a["ttl_seconds"] if ttl_a else None,
            ttl_b=ttl_b["ttl_seconds"] if ttl_b else None,
        )
    payload = result.to_dict()
    payload["channel_a"] = {
        "device": device_a,
        "metric": metric_a,
        "ttl_seconds": ttl_a["ttl_seconds"] if ttl_a else None,
        "event_count": info_a["c"],
        "min_ts": info_a["min_ts"],
        "max_ts": info_a["max_ts"],
    }
    payload["channel_b"] = {
        "device": device_b,
        "metric": metric_b,
        "ttl_seconds": ttl_b["ttl_seconds"] if ttl_b else None,
        "event_count": info_b["c"],
        "min_ts": info_b["min_ts"],
        "max_ts": info_b["max_ts"],
    }
    return payload


def query_fault_pairing(
    cause_rule_id: int,
    effect_rule_id: int,
    start: float,
    end: float,
    max_lag_seconds: float,
) -> dict:
    """Read-only sequence pairing of two different rules' stored faults.

    Only the **already stored** confirmed fault intervals whose **start**
    lies inside the half-open UTC window ``[start, end)`` take part; each
    cause fault may be paired with an effect fault whose start is strictly
    later by at most ``max_lag_seconds``.  The pairing is order-preserving
    and one-to-one, first maximizing the pair count and then minimizing the
    total lag, with ties resolved stably by fault id (see
    :func:`app.pairing.pair_faults`).  The query only ``SELECT``\ s stored
    rows: it never re-evaluates rules and never writes fault results.

    Raises ``KeyError`` for unknown rule ids and ``ValueError`` for identical
    rules, an empty/inverted window or a non-positive lag.
    """
    cause_rule_id = int(cause_rule_id)
    effect_rule_id = int(effect_rule_id)
    if cause_rule_id == effect_rule_id:
        raise ValueError("cause and effect must be two different rules")
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("pairing end must be greater than start (half-open [start, end))")
    max_lag = float(max_lag_seconds)
    if not math.isfinite(max_lag) or max_lag <= 0:
        raise ValueError("max_lag_seconds must be a positive finite number of seconds")

    # Both rule definitions and the stored faults come from one committed
    # snapshot so pairing never matches intervals from mixed recomputations.
    with db.read_snapshot():
        cause_rule = db.fetch_rule(cause_rule_id)
        if cause_rule is None:
            raise KeyError(f"rule not found: {cause_rule_id}")
        effect_rule = db.fetch_rule(effect_rule_id)
        if effect_rule is None:
            raise KeyError(f"rule not found: {effect_rule_id}")

        # Stored faults whose START instant falls inside [start, end) — faults
        # starting before the window never anchor a pair, however far they reach.
        rows = db.query(
            "SELECT id, rule_id, start_ts, end_ts FROM fault_intervals"
            " WHERE rule_id IN (?, ?) AND start_ts >= ? AND start_ts < ?"
            " ORDER BY start_ts, id",
            (cause_rule_id, effect_rule_id, start, end),
        )
        causes = [
            PairableFault(id=r["id"], start=r["start_ts"], end=r["end_ts"])
            for r in rows
            if r["rule_id"] == cause_rule_id
        ]
        effects = [
            PairableFault(id=r["id"], start=r["start_ts"], end=r["end_ts"])
            for r in rows
            if r["rule_id"] == effect_rule_id
        ]

    result = pair_faults(causes, effects, max_lag)
    payload = result.to_dict()
    payload["cause_rule"] = {
        "id": cause_rule["id"],
        "name": cause_rule["name"],
        "logic": cause_rule["logic"],
        "duration": cause_rule["duration"],
    }
    payload["effect_rule"] = {
        "id": effect_rule["id"],
        "name": effect_rule["name"],
        "logic": effect_rule["logic"],
        "duration": effect_rule["duration"],
    }
    payload["start"] = start
    payload["end"] = end
    return payload


def query_mutation_recovery(
    device: str,
    metric: str,
    start: float,
    end: float,
    rate_threshold: float,
    regression_tolerance: float,
    observation_seconds: float,
) -> dict:
    """Read-only single-channel abrupt-change recovery diagnostic.

    Reads the channel's stored events over the half-open window
    ``[start, end)`` plus the last sample before its start (that sample may
    serve as the pre-mutation baseline while still valid under the channel's
    configured TTL), then walks adjacent valid samples as described in
    :func:`app.recovery.diagnose_mutation_recovery`: change rates use the
    actual inter-sample elapsed time, TTL-expiry gaps cut continuity (no
    jump is judged across them), and every super-threshold jump records its
    baseline and looks forward for the first sample back inside the baseline
    tolerance within the observation duration.  The query only ``SELECT``\ s
    the ``events`` / ``channel_ttls`` tables: it never recomputes rules or
    writes fault results.

    Raises ``KeyError`` when the channel has no events and ``ValueError``
    for an invalid window or parameter.
    """
    device = device.strip()
    metric = metric.strip()
    if not device or not metric:
        raise ValueError("device and metric are required")
    start = float(start)
    end = float(end)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("diagnostic end must be greater than start (half-open [start, end))")
    rate_threshold = float(rate_threshold)
    if not math.isfinite(rate_threshold) or rate_threshold <= 0:
        raise ValueError("rate_threshold must be a positive finite number of units per second")
    regression_tolerance = float(regression_tolerance)
    if not math.isfinite(regression_tolerance) or regression_tolerance < 0:
        raise ValueError("regression_tolerance must be a finite non-negative number")
    observation_seconds = float(observation_seconds)
    if not math.isfinite(observation_seconds) or observation_seconds <= 0:
        raise ValueError("observation_seconds must be a positive finite number of seconds")

    # Channel totals, the pre-window baseline, windowed samples and the TTL
    # share one committed snapshot.
    with db.read_snapshot():
        channel = db.query_one(
            "SELECT COUNT(*) AS c, MIN(ts) AS min_ts, MAX(ts) AS max_ts FROM events"
            " WHERE device = ? AND metric = ?",
            (device, metric),
        )
        if channel is None or channel["c"] == 0:
            raise KeyError(f"channel not found: {device}/{metric}")

        before = db.query_one(
            "SELECT id, ts, value FROM events"
            " WHERE device = ? AND metric = ? AND value IS NOT NULL AND ts < ?"
            " ORDER BY ts DESC, id DESC LIMIT 1",
            (device, metric, start),
        )
        rows = db.query(
            "SELECT id, ts, value FROM events"
            " WHERE device = ? AND metric = ? AND value IS NOT NULL AND ts >= ? AND ts < ?"
            " ORDER BY ts, id",
            (device, metric, start, end),
        )
        samples: list[tuple[int, float, float]] = [(r["id"], r["ts"], r["value"]) for r in rows]
        if before is not None:
            samples.insert(0, (before["id"], before["ts"], before["value"]))

        ttl_row = db.fetch_channel_ttl(device, metric)
        ttl = ttl_row["ttl_seconds"] if ttl_row else None

    result = diagnose_mutation_recovery(
        samples,
        start,
        end,
        rate_threshold,
        regression_tolerance,
        observation_seconds,
        ttl=ttl,
    )
    payload = result.to_dict()
    payload["device"] = device
    payload["metric"] = metric
    payload["channel"] = {
        "device": device,
        "metric": metric,
        "ttl_seconds": ttl,
        "event_count": channel["c"],
        "min_ts": channel["min_ts"],
        "max_ts": channel["max_ts"],
    }
    return payload


def query_suppressed(
    start: float | None = None, end: float | None = None
) -> list[dict]:
    """Suppressed segments (truth lost to maintenance), half-open filtered.

    Each segment is linked to its rule and to the maintenance windows that
    suppressed it (ids resolved to names for display/export).
    """
    where = []
    params: list = []
    if start is not None:
        where.append("s.end_ts > ?")
        params.append(start)
    if end is not None:
        where.append("s.start_ts < ?")
        params.append(end)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"""
        SELECT s.*, r.name AS rule_name
        FROM suppressed_segments s JOIN rules r ON r.id = s.rule_id
        {clause} ORDER BY s.start_ts, s.rule_id, s.id
        """,
        params,
    )
    windows = {w["id"]: w for w in db.fetch_maintenance_windows()}
    out = []
    for row in rows:
        linked = []
        for wid in json.loads(row["maintenance_ids"]):
            w = windows.get(wid)
            linked.append(
                {"id": wid, "name": w["name"], "device": w["device"]}
                if w
                else {"id": wid, "name": None, "device": row["device"]}
            )
        out.append(
            {
                "id": row["id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "device": row["device"],
                "metric": row["metric"],
                "start": row["start_ts"],
                "end": row["end_ts"],
                "maintenance_ids": json.loads(row["maintenance_ids"]),
                "maintenance_windows": linked,
            }
        )
    return out


def query_annotations(start: float | None = None, end: float | None = None) -> list[dict]:
    where = []
    params: list = []
    if start is not None:
        where.append("end_ts > ?")
        params.append(start)
    if end is not None:
        where.append("start_ts < ?")
        params.append(end)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"SELECT * FROM annotations{clause} ORDER BY start_ts, id", params
    )
    return [
        {
            "id": r["id"],
            "start": r["start_ts"],
            "end": r["end_ts"],
            "label": r["label"],
            "color": r["color"],
            "note": r["note"],
        }
        for r in rows
    ]


def _bucket_extremes(
    bucket: list[tuple[float, float, int]],
) -> list[tuple[float, float, int]]:
    """A bucket's minimum and maximum samples (first occurrence wins ties).

    Both extrema are kept so an isolated spike inside the bucket survives
    sampling; a constant (or single-sample) bucket contributes its sample
    only once.
    """
    low = min(bucket, key=lambda p: p[1])
    high = max(bucket, key=lambda p: p[1])
    if low is high:
        return [low]
    return [low, high]


def _pick_deviation_extreme(
    first: tuple[float, float, int],
    last: tuple[float, float, int],
    low: tuple[float, float, int],
    high: tuple[float, float, int],
) -> tuple[float, float, int]:
    """Single-slot choice between a bucket's min/max candidates.

    The candidate deviating more from the bucket's own first→last baseline
    (the straight line between the bucket's ``first`` and ``last`` samples)
    wins; equal deviations resolve stably to the sample appearing first in
    (ts, event id) order.  A constant (or single-sample) bucket — where the
    minimum and maximum are the same sample — contributes it once.
    """
    if low is high:
        return low
    (t0, v0, _), (t1, v1, _) = first, last

    def deviation(p: tuple[float, float, int]) -> float:
        if t1 == t0:
            return abs(p[1] - v0)
        baseline = v0 + (v1 - v0) * (p[0] - t0) / (t1 - t0)
        return abs(p[1] - baseline)

    dev_low, dev_high = deviation(low), deviation(high)
    if dev_low > dev_high:
        return low
    if dev_high > dev_low:
        return high
    return min((low, high), key=lambda p: (p[0], p[2]))


def _largest_deviation_extreme(
    bucket: list[tuple[float, float, int]],
) -> tuple[float, float, int]:
    """The single bucket extreme to keep when the budget allows only one.

    Between the bucket's minimum and maximum candidates, the one deviating
    more from the bucket's own first→last baseline (the straight line
    between the bucket's first and last samples) is kept; equal deviations
    resolve stably to the sample appearing first in (ts, event id) order.
    """
    low = min(bucket, key=lambda p: p[1])
    high = max(bucket, key=lambda p: p[1])
    return _pick_deviation_extreme(bucket[0], bucket[-1], low, high)


def downsample(values: list[tuple[float, float, int]], max_points: int) -> list[tuple[float, float, int]]:
    """Bounded extreme-value sampling; never returns more than max_points.

    values: (ts, value, event_id) sorted by (ts, id).  A series that fits
    the budget is returned unchanged.  Otherwise the first and last samples
    of the queried range are always kept, the middle samples are split into
    time-ordered buckets, and — within the remaining point budget — each
    bucket contributes its minimum and maximum (so isolated spikes survive,
    unlike stride sampling).  An odd budget leaves a single slot for the
    last bucket, which keeps the extreme deviating most from its own
    first→last baseline (ties keep the earlier sample).  The result is
    emitted in the original (ts, event id) order.
    """
    n = len(values)
    if n <= max_points:
        return values
    if max_points <= 0:
        return []
    if max_points == 1:
        return values[:1]

    middle = values[1:-1]
    budget = max_points - 2  # point slots left for middle samples
    if budget <= 0:
        return [values[0], values[-1]]
    bucket_count = (budget + 1) // 2  # each bucket wants min+max
    base, extra = divmod(len(middle), bucket_count)
    picked = [values[0]]
    start = 0
    for i in range(bucket_count):
        size = base + (1 if i < extra else 0)
        bucket = middle[start : start + size]
        start += size
        # Even budget: min+max per bucket; an odd budget leaves one slot
        # for the last bucket.
        if budget % 2 == 1 and i == bucket_count - 1:
            picked.append(_largest_deviation_extreme(bucket))
        else:
            picked.extend(_bucket_extremes(bucket))
    picked.append(values[-1])
    picked.sort(key=lambda p: (p[0], p[2]))
    return picked


def _downsample_stream(
    total: int,
    batches: Iterable[list[tuple[float, float, int]]],
    max_points: int,
) -> list[tuple[float, float, int]]:
    """downsample() folded over a batched (ts, event id)-ordered row stream.

    ``total`` is the exact number of samples the batches will yield (the
    COUNT taken in the same read snapshot); each batch is a list of
    (ts, value, event_id) tuples ordered by (ts, event id).  The selection
    is exactly :func:`downsample`'s — first/last samples kept, the middle
    split into the same time-ordered buckets, per-bucket min+max (first
    occurrence wins ties), the odd-budget single slot with its first→last
    baseline tie-break, original order in the output — but at any moment
    only one batch, the current bucket's first/last/min/max candidates and
    the (≤ max_points) picked samples are held, so the working memory stays
    bounded by max_points plus one batch instead of growing with the raw
    row count of the queried range.
    """
    if total <= max_points:
        # Fits the budget: every sample is kept, exactly like downsample().
        kept: list[tuple[float, float, int]] = []
        for batch in batches:
            kept.extend(batch)
        return kept
    if max_points <= 0:
        return []
    if max_points == 1:
        for batch in batches:
            if batch:
                return [batch[0]]
        return []
    if max_points == 2:
        # Only the range's first and last samples survive the budget.
        first = last = None
        for batch in batches:
            for row in batch:
                if first is None:
                    first = row
                last = row
        return [first, last] if first is not None else []

    budget = max_points - 2  # point slots left for middle samples
    bucket_count = (budget + 1) // 2  # each bucket wants min+max
    base, extra = divmod(total - 2, bucket_count)
    single_slot_last = budget % 2 == 1

    picked: list[tuple[float, float, int]] = []
    # Current bucket's incremental state: its first and last samples (the
    # single-slot baseline anchors) and its min/max candidates (replaced
    # only on a strictly smaller/larger value, so the first occurrence
    # wins ties exactly like min()/max() over the materialized bucket).
    bucket_first = bucket_last = bucket_low = bucket_high = None
    bucket_fill = 0
    bucket_index = 0
    bucket_size = base + (1 if extra > 0 else 0)

    def feed_middle(row: tuple[float, float, int]) -> None:
        nonlocal bucket_first, bucket_last, bucket_low, bucket_high
        nonlocal bucket_fill, bucket_index, bucket_size
        if bucket_fill == 0:
            bucket_first = bucket_last = bucket_low = bucket_high = row
            bucket_fill = 1
        else:
            bucket_last = row
            bucket_fill += 1
            if row[1] < bucket_low[1]:
                bucket_low = row
            if row[1] > bucket_high[1]:
                bucket_high = row
        if bucket_fill < bucket_size:
            return
        # Bucket complete: emit its selection, same rules as downsample().
        if single_slot_last and bucket_index == bucket_count - 1:
            picked.append(
                _pick_deviation_extreme(bucket_first, bucket_last, bucket_low, bucket_high)
            )
        elif bucket_low is bucket_high:
            picked.append(bucket_low)
        else:
            picked.extend((bucket_low, bucket_high))
        bucket_index += 1
        bucket_fill = 0
        bucket_size = base + (1 if bucket_index < extra else 0)

    first_row = None
    pending = None  # held back one row so the range's last sample stays out of the buckets
    for batch in batches:
        for row in batch:
            if first_row is None:
                first_row = row
                continue
            if pending is not None:
                feed_middle(pending)
            pending = row
    last_row = pending

    result = [first_row, *picked, last_row]
    result.sort(key=lambda p: (p[0], p[2]))
    return result


def _sample_tuple_batches(
    batches: Iterable[list],
) -> Iterable[list[tuple[float, float, int]]]:
    """Project raw row batches to (ts, value, event_id) tuples, one batch at a time."""
    for batch in batches:
        yield [(r["ts"], r["value"], r["id"]) for r in batch]


def timeline_data(start: float | None, end: float | None, max_points: int = 400) -> dict:
    # The whole timeline is one committed view: channels, their samples,
    # faults, annotations, maintenance and suppression must describe the
    # same committed instant, not a mix across concurrent writes.
    with db.read_snapshot():
        channels = list_channels()
        channels_in_range = []
        for ch in channels:
            if start is not None and ch["max_ts"] < start:
                continue
            if end is not None and ch["min_ts"] > end:
                continue
            channels_in_range.append(ch)

        channel_payload = []
        for ch in channels_in_range:
            clause = " WHERE device = ? AND metric = ? AND value IS NOT NULL"
            params: list = [ch["device"], ch["metric"]]
            if start is not None:
                clause += " AND ts >= ?"
                params.append(start)
            if end is not None:
                clause += " AND ts <= ?"
                params.append(end)
            # Count the range's samples first, inside this pinned snapshot,
            # so the bucket layout is computed from the same committed view
            # the scan then reads — a concurrent commit cannot desync them.
            total = db.query_one(f"SELECT COUNT(*) AS c FROM events{clause}", params)["c"]
            select_sql = f"SELECT ts, value, id FROM events{clause} ORDER BY ts, id"
            if total <= max_points:
                # Fits the point budget: every sample is returned, and the
                # single fetch is itself bounded by max_points rows.
                values = [
                    (r["ts"], r["value"], r["id"]) for r in db.query(select_sql, params)
                ]
            else:
                # Over budget: stream the ordered rows in bounded batches
                # and fold them into the same bucketed extreme selection,
                # so the sampling working set grows with max_points (plus
                # one batch), not with the raw row count of the range.
                batches = db.query_batches(select_sql, params)
                try:
                    values = _downsample_stream(
                        total, _sample_tuple_batches(batches), max_points
                    )
                finally:
                    batches.close()
            channel_payload.append(
                {
                    "device": ch["device"],
                    "metric": ch["metric"],
                    "count": ch["n"],
                    "shown": total,
                    "min_ts": ch["min_ts"],
                    "max_ts": ch["max_ts"],
                    "min_value": ch["min_value"],
                    "max_value": ch["max_value"],
                    "values": [{"ts": t, "v": v, "id": i} for t, v, i in values],
                }
            )

        bounds = db.query_one("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM events")
        return {
            "channels": channel_payload,
            "channel_ttls": db.fetch_channel_ttls(),
            "faults": query_faults(start, end),
            "annotations": query_annotations(start, end),
            "maintenance": db.fetch_maintenance_windows(start, end),
            "suppressed": query_suppressed(start, end),
            "data_start": bounds["lo"],
            "data_end": bounds["hi"],
        }
