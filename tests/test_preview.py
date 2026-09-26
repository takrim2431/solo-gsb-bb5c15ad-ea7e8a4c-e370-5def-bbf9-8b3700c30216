"""Tests for the unsaved-draft impact preview.

Two layers are covered:

* ``engine.diff_faults`` — the half-open sweep-line comparison of two
  confirmed fault sets for the same rule (added/removed pieces, overlapping
  pieces with changed/unchanged confirmation instants);
* ``services.preview_rule`` — the read-only baseline-vs-draft evaluation
  against the live events and planned-maintenance windows, asserting that
  neither the rules nor the fault/suppressed tables are written.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import FaultInterval, diff_faults, to_epoch


# --------------------------------------------------------------------------- #
# engine.diff_faults
# --------------------------------------------------------------------------- #


def at(minute: int) -> float:
    """Epoch seconds of minute ``minute`` of the bundled sample day (UTC)."""
    return to_epoch(f"2026-09-23T00:{minute:02d}:00Z")


def F(rule_id, start, end, confirmed_at=None, duration=0.0):
    """FaultInterval test factory; confirmed_at defaults to start+duration."""
    return FaultInterval(
        start=float(start),
        end=float(end),
        rule_id=rule_id,
        duration_required=float(duration),
        confirmed_at=(
            float(confirmed_at)
            if confirmed_at is not None
            else float(start) + float(duration)
        ),
        evidence=[],
    )


def kinds(segments):
    return [(s.kind, s.start, s.end) for s in segments]


def test_diff_identical_faults_is_unchanged():
    segs = diff_faults([F(1, 0, 10, duration=3)], [F(1, 0, 10, duration=3)])
    assert kinds(segs) == [("overlap_unchanged", 0.0, 10.0)]
    assert segs[0].baseline_confirmed_at == 3.0
    assert segs[0].draft_confirmed_at == 3.0


def test_diff_same_interval_shifted_confirmation_is_changed():
    # The interval does not move at all — only confirmed_at differs, and that
    # alone must mark the whole overlap.
    segs = diff_faults(
        [F(1, 0, 10, confirmed_at=3)], [F(1, 0, 10, confirmed_at=7)]
    )
    assert len(segs) == 1
    seg = segs[0]
    assert seg.kind == "overlap_changed"
    assert (seg.start, seg.end) == (0.0, 10.0)
    assert seg.baseline_confirmed_at == 3.0
    assert seg.draft_confirmed_at == 7.0


def test_diff_disjoint_sets_are_added_and_removed():
    segs = diff_faults([F(1, 0, 6)], [F(1, 10, 16)])
    assert kinds(segs) == [("removed", 0.0, 6.0), ("added", 10.0, 16.0)]
    assert segs[0].baseline_confirmed_at == 0.0
    assert segs[0].draft_confirmed_at is None
    assert segs[1].baseline_confirmed_at is None
    assert segs[1].draft_confirmed_at == 10.0


def test_diff_touching_boundary_creates_no_overlap():
    # [0,6) baseline followed by touching [6,10) draft: half-open, so they
    # never overlap — one removed + one added piece, no zero-length noise.
    segs = diff_faults([F(1, 0, 6)], [F(1, 6, 10)])
    assert kinds(segs) == [("removed", 0.0, 6.0), ("added", 6.0, 10.0)]


def test_diff_partial_overlap_splits_into_three_pieces():
    # baseline [0,10), draft [5,15): removed head, overlap middle, added tail
    segs = diff_faults(
        [F(1, 0, 10, confirmed_at=4)], [F(1, 5, 15, confirmed_at=8)]
    )
    assert kinds(segs) == [
        ("removed", 0.0, 5.0),
        ("overlap_changed", 5.0, 10.0),
        ("added", 10.0, 15.0),
    ]


def test_diff_unchanged_overlap_with_changed_edges():
    # The overlapping middle keeps the equal confirmation; only the edges
    # differ (the draft starts earlier and ends later).
    segs = diff_faults([F(1, 2, 8, confirmed_at=3)], [F(1, 0, 10, confirmed_at=3)])
    assert kinds(segs) == [
        ("added", 0.0, 2.0),
        ("overlap_unchanged", 2.0, 8.0),
        ("added", 8.0, 10.0),
    ]
    assert segs[1].baseline_confirmed_at == 3.0
    assert segs[1].draft_confirmed_at == 3.0


def test_diff_empty_baseline_marks_everything_added():
    segs = diff_faults([], [F(1, 0, 6), F(1, 8, 12)])
    assert kinds(segs) == [("added", 0.0, 6.0), ("added", 8.0, 12.0)]


def test_diff_empty_draft_marks_everything_removed():
    segs = diff_faults([F(1, 0, 6), F(1, 8, 12)], [])
    assert kinds(segs) == [("removed", 0.0, 6.0), ("removed", 8.0, 12.0)]


def test_diff_both_empty_is_empty():
    assert diff_faults([], []) == []


def test_diff_touching_faults_with_equal_confirmation_coalesce():
    # Two touching draft faults share the same confirmed_at; their added
    # pieces join into one maximal half-open span.
    segs = diff_faults(
        [], [F(1, 0, 6, confirmed_at=5), F(1, 6, 10, confirmed_at=5)]
    )
    assert kinds(segs) == [("added", 0.0, 10.0)]


# --------------------------------------------------------------------------- #
# services.preview_rule (read-only end-to-end against a scratch DB)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def scratch_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.DB_PATH = tmp.name
    db._conn = None
    db.init_db()
    yield tmp.name
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _import_minute_series(true_minutes=range(6, 17)):
    """One sample per minute 00:00..00:30, high (90) on true_minutes else 10."""
    rows = [
        {
            "device": "d",
            "metric": "m",
            "ts": f"2026-09-23T00:{m:02d}:00Z",
            "value": 90 if m in true_minutes else 10,
        }
        for m in range(31)
    ]
    services.import_events("\n".join(json.dumps(r) for r in rows))


def _rule(name="预览规则", duration=300, threshold=80, op=">="):
    return {
        "name": name,
        "logic": "AND",
        "duration": duration,
        "conditions": [
            {"device": "d", "metric": "m", "op": op, "threshold": threshold}
        ],
    }


def _create_stored_rule(duration=300):
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("预览规则", "AND", duration, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, "d", "m", ">=", 80, 0),
    )
    return rule_id


def test_preview_edit_shifted_confirmation_without_writing(scratch_db):
    _import_minute_series()
    rule_id = _create_stored_rule(duration=300)
    services.evaluate_all_rules()
    assert services.query_faults(None, None)[0]["confirmed_at"] == at(6) + 300

    # The draft keeps the same [6,17) interval but changes the duration, so
    # confirmed_at moves from 11:00 to 12:00 while the interval never moves.
    result = services.preview_rule(rule_id, _rule(duration=360))
    counts = result["diff"]["counts"]
    assert result["mode"] == "edit"
    assert counts["added"] == 0 and counts["removed"] == 0
    assert counts["overlap_changed"] == 1
    assert counts["overlap_unchanged"] == 0
    seg = result["diff"]["segments"][0]
    assert (seg["start"], seg["end"]) == (at(6), at(17))
    assert seg["baseline_confirmed_at"] == at(6) + 300
    assert seg["draft_confirmed_at"] == at(6) + 360

    # Nothing was written: the stored rule and fault keep the old values.
    stored = services.query_faults(None, None)
    assert len(stored) == 1
    assert stored[0]["confirmed_at"] == at(6) + 300
    assert db.query_one(
        "SELECT duration AS d FROM rules WHERE id = ?", (rule_id,)
    )["d"] == 300


def test_preview_create_has_empty_baseline(scratch_db):
    _import_minute_series()
    result = services.preview_rule(None, _rule(name="新规则"))
    assert result["mode"] == "create"
    assert result["baseline"]["faults"] == []
    assert result["diff"]["counts"]["added"] == 1
    assert result["diff"]["counts"]["removed"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM rules")["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == 0


def test_preview_draft_below_duration_shows_removed(scratch_db):
    _import_minute_series()
    rule_id = _create_stored_rule(duration=300)
    services.evaluate_all_rules()

    # 11 minutes of truth against a 15-minute requirement: no confirmed fault
    result = services.preview_rule(rule_id, _rule(duration=15 * 60))
    counts = result["diff"]["counts"]
    assert counts["removed"] == 1
    assert counts["added"] == 0
    assert counts["overlap_changed"] == 0
    assert result["draft"]["faults"] == []
    assert len(services.query_faults(None, None)) == 1  # stored fault untouched


def test_preview_evaluates_both_sides_with_current_maintenance(scratch_db):
    _import_minute_series()
    rule_id = _create_stored_rule(duration=300)
    services.evaluate_all_rules()
    db.execute(
        "INSERT INTO maintenance_windows (device, name, start_ts, end_ts, note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("d", "检修", at(10), at(12), None, 0.0),
    )
    # Stored faults are now stale (still [6,17)); the preview must not trust
    # them — it re-evaluates the baseline itself with the window applied.
    result = services.preview_rule(rule_id, _rule(duration=240))
    assert result["maintenance_windows"] == 1

    # Baseline (300s, window cut): [6,10) is too short, [12,17) confirms 17:00.
    assert [(f["start"], f["end"]) for f in result["baseline"]["faults"]] == [
        (at(12), at(17))
    ]
    # Draft (240s, same window): [6,10) confirms 10:00, [12,17) confirms 16:00.
    assert sorted((f["start"], f["end"]) for f in result["draft"]["faults"]) == [
        (at(6), at(10)),
        (at(12), at(17)),
    ]

    by_kind: dict = {}
    for seg in result["diff"]["segments"]:
        by_kind.setdefault(seg["kind"], []).append(seg)
    assert [(s["start"], s["end"]) for s in by_kind["added"]] == [(at(6), at(10))]
    changed = by_kind["overlap_changed"]
    assert [(s["start"], s["end"]) for s in changed] == [(at(12), at(17))]
    assert changed[0]["baseline_confirmed_at"] == at(17)
    assert changed[0]["draft_confirmed_at"] == at(16)
    assert "removed" not in by_kind

    # The stale stored fault table is still untouched.
    assert services.query_faults(None, None)[0]["start"] == at(6)


def test_preview_unchanged_draft_reports_no_change(scratch_db):
    _import_minute_series()
    rule_id = _create_stored_rule(duration=300)
    services.evaluate_all_rules()
    result = services.preview_rule(rule_id, _rule(duration=300))
    assert result["diff"]["changed"] is False
    counts = result["diff"]["counts"]
    assert counts["overlap_unchanged"] == 1
    assert counts["added"] == counts["removed"] == counts["overlap_changed"] == 0


def test_preview_unknown_rule_raises(scratch_db):
    _import_minute_series()
    with pytest.raises(KeyError):
        services.preview_rule(999, _rule())
