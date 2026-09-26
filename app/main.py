"""FastAPI application: multi-channel sensor fault-rule workbench."""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import db, services
from .engine import LOGIC, OPERATORS, RESET_OPS, to_epoch

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Sensor Fault Rules", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# Helpers / schemas
# --------------------------------------------------------------------------- #


def parse_bound(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return to_epoch(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid time bound: {exc}") from exc


class ConditionIn(BaseModel):
    device: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    op: str
    threshold: float
    # Optional latching reset threshold; only valid for >, >=, <, <=.
    reset_threshold: float | None = None

    def validated(self) -> dict:
        if self.op not in OPERATORS:
            raise HTTPException(
                status_code=400, detail=f"op must be one of {sorted(OPERATORS)}"
            )
        reset = self.reset_threshold
        if reset is not None:
            if not math.isfinite(reset):
                raise HTTPException(
                    status_code=400, detail="reset threshold must be a finite number"
                )
            if self.op not in RESET_OPS:
                raise HTTPException(
                    status_code=400,
                    detail="复位阈值仅支持 >、>=、<、<= 条件",
                )
            if self.op in {">", ">="}:
                if reset >= self.threshold:
                    raise HTTPException(
                        status_code=400,
                        detail="高值触发（>、>=）的复位阈值必须低于触发阈值",
                    )
            elif reset <= self.threshold:
                raise HTTPException(
                    status_code=400,
                    detail="低值触发（<、<=）的复位阈值必须高于触发阈值",
                )
        return self.model_dump()


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    logic: str = "AND"
    duration: float = Field(0, ge=0)
    conditions: list[ConditionIn] = Field(min_length=1)

    def normalized(self) -> dict:
        logic = self.logic.upper()
        if logic not in LOGIC:
            raise HTTPException(status_code=400, detail="logic must be AND or OR")
        return {
            "name": self.name.strip(),
            "logic": logic,
            "duration": self.duration,
            "conditions": [c.validated() for c in self.conditions],
        }


class RulePreviewIn(RuleIn):
    # None/absent: preview a brand-new rule against an empty baseline.
    rule_id: int | None = None


class AnnotationIn(BaseModel):
    start: Any
    end: Any
    label: str = Field(min_length=1, max_length=200)
    color: str | None = None
    note: str | None = None

    def bounds(self) -> tuple[float, float]:
        try:
            start = to_epoch(self.start)
            end = to_epoch(self.end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if end <= start:
            raise HTTPException(status_code=400, detail="annotation end must be greater than start")
        return start, end


class MaintenanceIn(BaseModel):
    device: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    start: Any
    end: Any
    note: str | None = None

    def normalized(self) -> dict:
        device = self.device.strip()
        name = self.name.strip()
        if not device or not name:
            raise HTTPException(status_code=400, detail="maintenance device and name are required")
        try:
            start = to_epoch(self.start)
            end = to_epoch(self.end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if end <= start:
            raise HTTPException(
                status_code=400,
                detail="maintenance window end must be greater than start (half-open [start, end))",
            )
        return {
            "device": device,
            "name": name,
            "start": start,
            "end": end,
            "note": self.note,
        }


class ChannelTtlIn(BaseModel):
    """Per-channel sample validity period; a positive number of seconds."""

    device: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=200)
    ttl_seconds: float = Field(gt=0)

    def normalized(self) -> dict:
        ttl = float(self.ttl_seconds)
        if ttl == float("inf") or ttl != ttl:  # reject inf / NaN
            raise HTTPException(status_code=400, detail="ttl_seconds must be a finite positive number")
        return {"device": self.device.strip(), "metric": self.metric.strip(), "ttl_seconds": ttl}


class ImportBody(BaseModel):
    text: str = ""
    filename: str | None = None
    device_field: str | None = None
    metric_field: str | None = None
    ts_field: str | None = None
    value_field: str | None = None


DEMO_RULES = [
    {
        "name": "pump-1 高温持续 (temp>=80, 5分钟)",
        "logic": "AND",
        "duration": 300,
        "conditions": [{"device": "pump-1", "metric": "temp", "op": ">=", "threshold": 80}],
    },
    {
        "name": "pump-1 高温且振动 (AND, 2分钟)",
        "logic": "AND",
        "duration": 120,
        "conditions": [
            {"device": "pump-1", "metric": "temp", "op": ">=", "threshold": 80},
            {"device": "pump-1", "metric": "vib", "op": ">", "threshold": 3},
        ],
    },
    {
        "name": "pump-2 振动大或压力低 (OR, 相接合并, 200秒)",
        "logic": "OR",
        "duration": 200,
        "conditions": [
            {"device": "pump-2", "metric": "vib", "op": ">", "threshold": 5},
            {"device": "pump-2", "metric": "press", "op": "<", "threshold": 30},
        ],
    },
]

SAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "telemetry.jsonl")


# --------------------------------------------------------------------------- #
# Health / meta
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "time": time.time()}


@app.get("/api/meta")
def meta() -> dict:
    # One committed view for all of the aggregate counters.
    with db.read_snapshot():
        bounds = db.query_one("SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM events")
        event_count = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
        rule_count = db.query_one("SELECT COUNT(*) AS c FROM rules")["c"]
    return {
        "operators": sorted(OPERATORS),
        "logic": sorted(LOGIC),
        "data_start": bounds["lo"],
        "data_end": bounds["hi"],
        "event_count": event_count,
        "rule_count": rule_count,
    }


# --------------------------------------------------------------------------- #
# Import & events
# --------------------------------------------------------------------------- #


@app.post("/api/import")
async def import_uploaded(
    file: UploadFile | None = File(None),
    body: str | None = Form(None),
    device_field: str | None = Form(None),
    metric_field: str | None = Form(None),
    ts_field: str | None = Form(None),
    value_field: str | None = Form(None),
) -> dict:
    if file is not None:
        raw = (await file.read()).decode("utf-8", errors="replace")
        filename = file.filename
    elif body:
        raw = body
        filename = None
    else:
        raise HTTPException(status_code=400, detail="provide a JSONL file or a text body")
    try:
        return services.import_events(
            raw,
            filename=filename,
            device_field=device_field or services.DEFAULT_DEVICE_FIELD,
            metric_field=metric_field or services.DEFAULT_METRIC_FIELD,
            ts_field=ts_field or services.DEFAULT_TS_FIELD,
            value_field=value_field or services.DEFAULT_VALUE_FIELD,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/import/text")
def import_text(payload: ImportBody) -> dict:
    try:
        return services.import_events(
            payload.text,
            filename=payload.filename,
            device_field=payload.device_field or services.DEFAULT_DEVICE_FIELD,
            metric_field=payload.metric_field or services.DEFAULT_METRIC_FIELD,
            ts_field=payload.ts_field or services.DEFAULT_TS_FIELD,
            value_field=payload.value_field or services.DEFAULT_VALUE_FIELD,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/imports")
def list_imports() -> list[dict]:
    """Import batches (newest first) with undo entry and eligibility flags."""
    return services.list_import_batches()


@app.post("/api/imports/{import_id}/undo")
def undo_import(import_id: int) -> dict:
    """Revoke the most recent, fully snapshotted import batch.

    Deletes the batch's newly inserted events, restores overwritten events'
    previous value/raw/batch ownership in one transaction, then refreshes
    faults via the regular evaluation flow.
    """
    try:
        return services.undo_import(import_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except services.UndoNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/channels")
def channels() -> list[dict]:
    return services.list_channels()


@app.get("/api/events")
def get_events(
    start: str | None = None,
    end: str | None = None,
    device: str | None = None,
    metric: str | None = None,
    limit: int = Query(2000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> dict:
    rows, total = services.list_events(
        parse_bound(start), parse_bound(end), device, metric, limit, offset
    )
    return {"total": total, "limit": limit, "offset": offset, "events": rows}


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def _insert_rule(data: dict, rule_id: int | None = None) -> int:
    with db.transaction() as conn:
        if rule_id is None:
            cur = conn.execute(
                "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
                (data["name"], data["logic"], data["duration"], time.time()),
            )
            rule_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE rules SET name = ?, logic = ?, duration = ? WHERE id = ?",
                (data["name"], data["logic"], data["duration"], rule_id),
            )
            conn.execute("DELETE FROM rule_conditions WHERE rule_id = ?", (rule_id,))
            conn.execute("DELETE FROM fault_intervals WHERE rule_id = ?", (rule_id,))
            conn.execute("DELETE FROM suppressed_segments WHERE rule_id = ?", (rule_id,))
        for ord_, cond in enumerate(data["conditions"]):
            conn.execute(
                "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, reset_threshold, ord)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rule_id,
                    cond["device"],
                    cond["metric"],
                    cond["op"],
                    cond["threshold"],
                    cond.get("reset_threshold"),
                    ord_,
                ),
            )
    return rule_id


@app.get("/api/rules")
def get_rules() -> list[dict]:
    return db.fetch_rules()


@app.post("/api/rules")
def create_rule(rule: RuleIn) -> dict:
    rule_id = _insert_rule(rule.normalized())
    return db.fetch_rule(rule_id)


@app.post("/api/rules/preview")
def preview_rule(preview: RulePreviewIn) -> dict:
    """Preview the impact of an unsaved rule draft without writing anything.

    The stored rule (if ``rule_id`` is given) and the draft are each
    evaluated against the current events and planned-maintenance windows;
    the two confirmed fault sets are compared as half-open intervals.  No
    rule, fault or maintenance row is created or modified.
    """
    try:
        return services.preview_rule(preview.rule_id, preview.normalized())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


@app.get("/api/rules/{rule_id}")
def get_rule(rule_id: int) -> dict:
    rule = db.fetch_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    return rule


@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, rule: RuleIn) -> dict:
    if db.fetch_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    _insert_rule(rule.normalized(), rule_id=rule_id)
    return db.fetch_rule(rule_id)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int) -> dict:
    if db.fetch_rule(rule_id) is None:
        raise HTTPException(status_code=404, detail="rule not found")
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    return {"deleted": rule_id}


@app.post("/api/evaluate")
def evaluate() -> dict:
    return services.evaluate_all_rules()


@app.get("/api/faults")
def get_faults(start: str | None = None, end: str | None = None) -> list[dict]:
    return services.query_faults(parse_bound(start), parse_bound(end))


@app.get("/api/fault-events")
def get_fault_events(start: str | None = None, end: str | None = None) -> list[dict]:
    """Fault events: different rules' faults whose intervals overlap/touch,
    merged transitively. Filtering follows the same [start,end) window."""
    return services.query_fault_events(parse_bound(start), parse_bound(end))


@app.get("/api/fault-daily-ledger")
def get_fault_daily_ledger(
    rules: list[int] = Query(..., min_length=1),
    start: str = Query(...),
    end: str = Query(...),
) -> dict:
    """Read-only per-day fault ledger for a combination of selected rules.

    Reads the stored confirmed fault intervals of the given rules (no
    re-evaluation — rule changes keep being recomputed by the existing
    flow), merges overlapping/touching half-open pieces across rules, then
    splits the fault time at UTC midnights inside [start, end).  Each day
    reports deduplicated fault seconds, newly started segments, segments
    continuing from previous days, and the coverage against the day's
    actually queried seconds.
    """
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)
    if start_ts is None or end_ts is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    try:
        return services.query_fault_daily_ledger(rules, start_ts, end_ts)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Sampling-cadence audit (read-only)
# --------------------------------------------------------------------------- #


@app.get("/api/sampling-audit")
def get_sampling_audit(
    device: str = Query(..., min_length=1),
    metric: str = Query(..., min_length=1),
    start: str = Query(...),
    end: str = Query(...),
    period_seconds: float = Query(..., gt=0),
    tolerance_seconds: float = Query(..., ge=0),
) -> dict:
    """Read-only sampling-cadence audit for one existing channel.

    Expected sample instants are generated as ``start + k*period`` inside the
    half-open window ``[start, end)`` (anchored at ``start``); actual events
    of the channel within the window and within ``tolerance_seconds`` of their
    nearest expected instant book that instant (one event per instant at most;
    tolerance must be strictly below half a period).  Consecutive unbooked
    instants are merged into missing-sample segments.  The audit reads the
    stored events only — it never writes events or recomputes faults.
    """
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)
    if start_ts is None or end_ts is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    if not math.isfinite(period_seconds) or period_seconds <= 0:
        raise HTTPException(status_code=400, detail="period_seconds must be a positive finite number of seconds")
    if not math.isfinite(tolerance_seconds) or tolerance_seconds < 0:
        raise HTTPException(status_code=400, detail="tolerance_seconds must be a finite non-negative number of seconds")
    try:
        return services.query_sampling_audit(
            device, metric, start_ts, end_ts, period_seconds, tolerance_seconds
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Redundant-channel consistency check (read-only)
# --------------------------------------------------------------------------- #


@app.get("/api/consistency-check")
def get_consistency_check(
    device_a: str = Query(..., min_length=1),
    metric_a: str = Query(..., min_length=1),
    device_b: str = Query(..., min_length=1),
    metric_b: str = Query(..., min_length=1),
    start: str = Query(...),
    end: str = Query(...),
    threshold: float = Query(..., ge=0),
) -> dict:
    """Read-only consistency check between two different existing channels.

    Over the half-open UTC window ``[start, end)`` both channels' samples
    are expanded into held intervals (per-channel TTL and the global data
    endpoint apply; the last sample before ``start`` counts while still
    valid) and compared only where both sides hold a value — expired gaps
    are never extrapolated.  Stretches with absolute difference strictly
    greater than ``threshold`` merge into inconsistent segments, each
    reporting start/end, duration, the maximum difference and the two
    samples behind it; comparable and inconsistent seconds are totalled.
    The check reads the current events only — it never writes fault results
    or triggers rule re-evaluation.
    """
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)
    if start_ts is None or end_ts is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    if not math.isfinite(threshold):
        raise HTTPException(status_code=400, detail="threshold must be a finite non-negative number")
    try:
        return services.query_channel_consistency(
            device_a, metric_a, device_b, metric_b, start_ts, end_ts, threshold
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Rule fault sequence pairing (read-only)
# --------------------------------------------------------------------------- #


@app.get("/api/fault-pairing")
def get_fault_pairing(
    cause_rule: int = Query(...),
    effect_rule: int = Query(...),
    start: str = Query(...),
    end: str = Query(...),
    max_lag_seconds: float = Query(..., gt=0),
) -> dict:
    """Read-only sequence pairing of two different rules' stored faults.

    Only stored confirmed faults whose **start** lies inside the half-open
    UTC window ``[start, end)`` take part.  A cause fault pairs with an
    effect fault whose start is strictly later by at most
    ``max_lag_seconds`` (positive); every fault joins at most one pair.  The
    order-preserving one-to-one pairing first maximizes the pair count, then
    minimizes the total lag, breaking remaining ties stably by fault id.
    The response lists the pairs (fault ids, starts, lag), the unmatched
    faults on both sides and the pairing rates.  The query never recomputes
    or writes fault results.
    """
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)
    if start_ts is None or end_ts is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    if not math.isfinite(max_lag_seconds) or max_lag_seconds <= 0:
        raise HTTPException(status_code=400, detail="max_lag_seconds must be a positive finite number of seconds")
    try:
        return services.query_fault_pairing(
            cause_rule, effect_rule, start_ts, end_ts, max_lag_seconds
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Single-channel abrupt-change recovery diagnostic (read-only)
# --------------------------------------------------------------------------- #


@app.get("/api/mutation-recovery")
def get_mutation_recovery(
    device: str = Query(..., min_length=1),
    metric: str = Query(..., min_length=1),
    start: str = Query(...),
    end: str = Query(...),
    rate_threshold: float = Query(..., gt=0),
    regression_tolerance: float = Query(..., ge=0),
    observation_seconds: float = Query(..., gt=0),
) -> dict:
    """Read-only abrupt-change recovery diagnostic for one existing channel.

    Adjacent valid samples inside the half-open UTC window ``[start, end)``
    are compared by change rate against their **actual** time difference; a
    configured sample validity period (TTL) expiry gap cuts continuity and
    no jump is judged across it.  The last still-valid sample before
    ``start`` may serve as the pre-mutation baseline.  A jump strictly
    greater than ``rate_threshold`` records its baseline and looks forward
    for the first sample back within ``regression_tolerance`` of the
    baseline within ``observation_seconds`` — recovered if found, otherwise
    unrecovered with a reason (observation expired / TTL gap / window end).
    Samples already attributed to a segment never open another one.  The
    query only reads stored events and channel TTLs — it never recomputes
    faults or writes anything.
    """
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)
    if start_ts is None or end_ts is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    if not math.isfinite(rate_threshold):
        raise HTTPException(status_code=400, detail="rate_threshold must be a finite positive number per second")
    if not math.isfinite(regression_tolerance):
        raise HTTPException(status_code=400, detail="regression_tolerance must be a finite non-negative number")
    if not math.isfinite(observation_seconds):
        raise HTTPException(status_code=400, detail="observation_seconds must be a positive finite number of seconds")
    try:
        return services.query_mutation_recovery(
            device,
            metric,
            start_ts,
            end_ts,
            rate_threshold,
            regression_tolerance,
            observation_seconds,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Annotations
# --------------------------------------------------------------------------- #


@app.get("/api/annotations")
def get_annotations(start: str | None = None, end: str | None = None) -> list[dict]:
    return services.query_annotations(parse_bound(start), parse_bound(end))


@app.post("/api/annotations")
def create_annotation(ann: AnnotationIn) -> dict:
    start_ts, end_ts = ann.bounds()
    cur = db.execute(
        "INSERT INTO annotations (start_ts, end_ts, label, color, note, created_at) VALUES (?,?,?,?,?,?)",
        (start_ts, end_ts, ann.label.strip(), ann.color, ann.note, time.time()),
    )
    row = db.query_one("SELECT * FROM annotations WHERE id = ?", (cur.lastrowid,))
    return {
        "id": row["id"],
        "start": row["start_ts"],
        "end": row["end_ts"],
        "label": row["label"],
        "color": row["color"],
        "note": row["note"],
    }


@app.delete("/api/annotations/{ann_id}")
def delete_annotation(ann_id: int) -> dict:
    row = db.query_one("SELECT id FROM annotations WHERE id = ?", (ann_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="annotation not found")
    db.execute("DELETE FROM annotations WHERE id = ?", (ann_id,))
    return {"deleted": ann_id}


# --------------------------------------------------------------------------- #
# Planned maintenance windows
# --------------------------------------------------------------------------- #


def _maintenance_response(window_id: int) -> dict:
    """Window payload plus the re-evaluation summary it triggered."""
    window = db.fetch_maintenance_window(window_id)
    evaluation = services.evaluate_all_rules()
    return {
        **window,
        "evaluation": {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        },
    }


@app.get("/api/maintenance")
def get_maintenance(
    start: str | None = None,
    end: str | None = None,
    device: str | None = None,
) -> list[dict]:
    return db.fetch_maintenance_windows(parse_bound(start), parse_bound(end), device)


@app.post("/api/maintenance", status_code=201)
def create_maintenance(mw: MaintenanceIn) -> dict:
    data = mw.normalized()
    cur = db.execute(
        "INSERT INTO maintenance_windows (device, name, start_ts, end_ts, note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (data["device"], data["name"], data["start"], data["end"], data["note"], time.time()),
    )
    return _maintenance_response(cur.lastrowid)


@app.get("/api/maintenance/{window_id}")
def get_maintenance_window(window_id: int) -> dict:
    window = db.fetch_maintenance_window(window_id)
    if window is None:
        raise HTTPException(status_code=404, detail="maintenance window not found")
    return window


@app.put("/api/maintenance/{window_id}")
def update_maintenance(window_id: int, mw: MaintenanceIn) -> dict:
    if db.fetch_maintenance_window(window_id) is None:
        raise HTTPException(status_code=404, detail="maintenance window not found")
    data = mw.normalized()
    db.execute(
        "UPDATE maintenance_windows SET device = ?, name = ?, start_ts = ?, end_ts = ?, note = ?"
        " WHERE id = ?",
        (data["device"], data["name"], data["start"], data["end"], data["note"], window_id),
    )
    return _maintenance_response(window_id)


@app.delete("/api/maintenance/{window_id}")
def delete_maintenance(window_id: int) -> dict:
    if db.fetch_maintenance_window(window_id) is None:
        raise HTTPException(status_code=404, detail="maintenance window not found")
    db.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
    evaluation = services.evaluate_all_rules()
    return {
        "deleted": window_id,
        "evaluation": {
            "rules_evaluated": evaluation["rules_evaluated"],
            "fault_intervals": evaluation["fault_intervals"],
            "suppressed_segments": evaluation["suppressed_segments"],
        },
    }


@app.get("/api/suppressed")
def get_suppressed(start: str | None = None, end: str | None = None) -> list[dict]:
    """Segments where rule truth was suppressed by planned maintenance."""
    return services.query_suppressed(parse_bound(start), parse_bound(end))


# --------------------------------------------------------------------------- #
# Per-channel sample validity periods (TTL)
# --------------------------------------------------------------------------- #


@app.get("/api/channel-ttls")
def get_channel_ttls(
    device: str | None = None, metric: str | None = None
) -> list[dict]:
    """Configured sample validity periods (positive seconds) per channel.

    Channels without an entry keep the legacy hold-to-next-sample semantics.
    """
    return db.fetch_channel_ttls(device, metric)


@app.put("/api/channel-ttls")
def put_channel_ttl(payload: ChannelTtlIn) -> dict:
    """Set or replace one channel's sample validity period.

    Stored fault intervals are recomputed afterwards (maintenance windows
    keep participating in the evaluation).
    """
    data = payload.normalized()
    try:
        return services.set_channel_ttl(data["device"], data["metric"], data["ttl_seconds"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/channel-ttls")
def delete_channel_ttl(device: str = Query(..., min_length=1), metric: str = Query(..., min_length=1)) -> dict:
    """Clear one channel's validity period (revert to legacy semantics).

    Returns 404 when the channel had no configured period; stored faults are
    recomputed afterwards.
    """
    try:
        return services.clear_channel_ttl(device.strip(), metric.strip())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


# --------------------------------------------------------------------------- #
# Timeline / export / reset
# --------------------------------------------------------------------------- #


@app.get("/api/timeline")
def timeline(
    start: str | None = None,
    end: str | None = None,
    max_points: int = Query(400, ge=2, le=5000),
) -> dict:
    return services.timeline_data(parse_bound(start), parse_bound(end), max_points)


@app.get("/api/export")
def export(
    start: str | None = None,
    end: str | None = None,
    download: bool = False,
) -> Any:
    start_ts = parse_bound(start)
    end_ts = parse_bound(end)

    # The full export bundle describes one committed snapshot, so events,
    # faults, rules, maintenance and evidence references cannot straddle a
    # concurrent import / recompute commit.
    with db.read_snapshot():
        events, total = services.list_events(
            start_ts, end_ts, limit=10_000_000, offset=0
        )
        faults = [f for f in services.query_faults(start_ts, end_ts)]
        annotations = services.query_annotations(start_ts, end_ts)
        maintenance = db.fetch_maintenance_windows(start_ts, end_ts)
        suppressed = services.query_suppressed(start_ts, end_ts)

        # Split fault evidence into explicit per-condition evidence references so
        # the export carries raw events, fault intervals and their trigger proof.
        event_ids: set[int] = {e["id"] for e in events}
        referenced_events: list[dict] = []
        seen: set[int] = set()
        for fault in faults:
            for group in fault["evidence"]:
                for ev in group["events"]:
                    eid = ev["event_id"]
                    if eid not in seen and eid not in event_ids:
                        seen.add(eid)
                        row = db.query_one(
                            "SELECT id, device, metric, ts, value FROM events WHERE id = ?",
                            (eid,),
                        )
                        if row is not None:
                            referenced_events.append(dict(row))

        payload = {
            "exported_at": time.time(),
            "time_range": {
                "start": start_ts,
                "end": end_ts,
                "note": "events filtered inclusively; fault intervals/annotations/maintenance windows overlap the half-open [start,end) window",
            },
            "rules": db.fetch_rules(),
            "events": events,
            "events_truncated": False,
            "events_total_in_range": total,
            "evidence_only_events": referenced_events,
            "fault_intervals": faults,
            "fault_events": services.query_fault_events(start_ts, end_ts),
            "maintenance_windows": maintenance,
            "suppressed_segments": suppressed,
            "channel_ttls": db.fetch_channel_ttls(),
            "annotations": annotations,
        }

    if download:
        return JSONResponse(
            payload,
            headers={
                "Content-Disposition": 'attachment; filename="fault_export.json"',
                "Access-Control-Expose-Headers": "Content-Disposition",
            },
        )
    return payload


@app.post("/api/reset")
def reset() -> dict:
    db.reset_all()
    return {"status": "ok"}


@app.post("/api/demo")
def demo() -> dict:
    """Load the bundled sample dataset and create demo rules (idempotent-ish)."""
    if not os.path.exists(SAMPLE_PATH):
        raise HTTPException(status_code=404, detail="sample dataset not found")
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    result = services.import_events(text, filename="samples/telemetry.jsonl")

    existing = {r["name"] for r in db.fetch_rules()}
    created = 0
    for data in DEMO_RULES:
        if data["name"] in existing:
            continue
        _insert_rule(data)
        created += 1
    return {
        "inserted": result["inserted"],
        "overwritten": result["overwritten"],
        "skipped": result["skipped"],
        "rules_created": created,
        "evaluation": result.get("evaluation"),
        "note": "示例导入后已自动重算已有规则；新建的示例规则请点击「评估规则」生成故障区间",
    }


# --------------------------------------------------------------------------- #
# Static frontend
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "app.js"))


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "styles.css"))
