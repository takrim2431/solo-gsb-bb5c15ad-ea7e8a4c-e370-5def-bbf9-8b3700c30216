"""Regression tests for fault-evidence attribution by *effective sample span*.

A sample witnesses a fault interval exactly while its effective span — the
held interval after the TTL cap and the maintenance subtraction — overlaps
the fault's half-open ``[start, end)``.  Consequences pinned down here:

* a sample taken **before** the fault start whose value is still in effect
  when the AND intersection begins (or that keeps holding past a maintenance
  window's end) is kept as evidence;
* each evidence item's ``held_intervals`` are clipped to the fault's
  half-open bounds, never crossing them;
* a sample whose span expired (TTL gap) or was subtracted away (maintenance)
  never witnesses that stretch — even when its timestamp falls inside a
  fault sustained by another OR channel;
* the latching reset threshold attributes evidence through the same spans,
  so hysteresis-band samples holding into the fault count while an earlier
  trigger sample whose span never reaches the fault does not.

The page, the JSON APIs and the export all read the same stored evaluation
results, so the fixed attribution shows up identically everywhere.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    evaluate_rule,
    to_epoch,
)


def at(minute: int, second: int = 0) -> float:
    return to_epoch(f"2026-09-23T00:{minute:02d}:{second:02d}Z")


def ev(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def evidence_by_metric(fault):
    return {g.condition.metric: g for g in fault.evidence}


# --------------------------------------------------------------------------- #
# pre-fault samples kept by their effective span
# --------------------------------------------------------------------------- #


def test_and_rule_keeps_pre_fault_sample_holding_into_intersection():
    # temp sampled at t=0 holds 90 until t=1800; vib only holds [600,1200).
    # The AND truth (and fault) is [600,1200): the t=0 temp sample is in
    # effect for the whole fault and must witness it.
    temp = [ev(1, 0, 90, "d", "temp"), ev(2, 1800, 10, "d", "temp")]
    vib = [ev(3, 600, 5, "d", "vib"), ev(4, 1200, 1, "d", "vib")]
    result = evaluate_rule(
        1,
        "AND",
        0,
        [Condition("d", "temp", ">=", 80), Condition("d", "vib", ">", 3)],
        {("d", "temp"): temp, ("d", "vib"): vib},
        horizon=1800,
    )
    assert [(f.start, f.end) for f in result.faults] == [(600, 1200)]
    groups = evidence_by_metric(result.faults[0])
    # the pre-fault temp sample is evidence; its held interval is clipped to
    # the fault's half-open bounds instead of stretching back to t=0
    assert [e.id for e in groups["temp"].events] == [1]
    assert groups["temp"].held_intervals == [Interval(600, 1200)]
    assert [e.id for e in groups["vib"].events] == [3]
    assert groups["vib"].held_intervals == [Interval(600, 1200)]


def test_held_intervals_never_cross_fault_bounds():
    # Both channels hold far beyond the AND intersection [600,1200): every
    # evidence held_interval stays clipped to the fault, on both sides.
    temp = [ev(1, 0, 90, "d", "temp"), ev(2, 2000, 10, "d", "temp")]
    vib = [ev(3, 100, 5, "d", "vib"), ev(4, 1900, 1, "d", "vib")]
    press = [ev(5, 600, 20, "d", "press"), ev(6, 1200, 50, "d", "press")]
    result = evaluate_rule(
        1,
        "AND",
        0,
        [
            Condition("d", "temp", ">=", 80),
            Condition("d", "vib", ">", 3),
            Condition("d", "press", "<", 30),
        ],
        {("d", "temp"): temp, ("d", "vib"): vib, ("d", "press"): press},
        horizon=2000,
    )
    assert [(f.start, f.end) for f in result.faults] == [(600, 1200)]
    for group in result.faults[0].evidence:
        assert group.held_intervals == [Interval(600, 1200)]
        assert len(group.events) == 1


def test_sample_holding_past_maintenance_end_witnesses_post_window_fault():
    # One sample at t=90 holds [90,300); maintenance [120,180) cuts the truth
    # into [90,120) and [180,300).  The same pre-window sample is in effect
    # on both surviving faults and must witness both — including the one
    # starting at the window end, which its timestamp precedes.
    events = [ev(1, 90, 90), ev(2, 300, 10)]
    result = evaluate_rule(
        1,
        "AND",
        0,
        [Condition("d", "m", ">", 50)],
        {("d", "m"): events},
        horizon=300,
        maintenance=[MaintenanceWindow(1, "d", 120, 180, "检修")],
    )
    assert [(f.start, f.end) for f in result.faults] == [(90, 120), (180, 300)]
    for fault, piece in zip(result.faults, [(90, 120), (180, 300)]):
        group = fault.evidence[0]
        assert [e.id for e in group.events] == [1]
        assert group.held_intervals == [Interval(*piece)]


def test_maintenance_fully_subtracted_sample_is_not_evidence():
    # OR across devices: channel a (device d) holds [0,180) via per-minute
    # samples, but maintenance [0,100) on d subtracts everything before 100.
    # Channel b (device e) sustains the OR fault [0,300) the whole time, so
    # a's subtracted samples (timestamps inside the fault!) must not appear:
    # only the samples still in effect after the window do.
    a = [
        ev(1, 0, 90, "d", "a"),
        ev(2, 60, 90, "d", "a"),
        ev(3, 120, 90, "d", "a"),
        ev(4, 180, 10, "d", "a"),
    ]
    b = [ev(5, 0, 5, "e", "b"), ev(6, 300, 0, "e", "b")]
    result = evaluate_rule(
        1,
        "OR",
        0,
        [Condition("d", "a", ">", 50), Condition("e", "b", ">", 3)],
        {("d", "a"): a, ("e", "b"): b},
        horizon=300,
        maintenance=[MaintenanceWindow(1, "d", 0, 100, "检修")],
    )
    assert [(f.start, f.end) for f in result.faults] == [(0, 300)]
    groups = evidence_by_metric(result.faults[0])
    # t=0's span [0,60) is entirely subtracted; t=60 survives on [100,120)
    assert [e.id for e in groups["a"].events] == [2, 3]
    assert groups["a"].held_intervals == [Interval(100, 180)]
    assert [e.id for e in groups["b"].events] == [5]
    assert groups["b"].held_intervals == [Interval(0, 300)]


# --------------------------------------------------------------------------- #
# TTL gaps and the reset-threshold latch
# --------------------------------------------------------------------------- #


def test_ttl_valid_pre_fault_sample_kept_expired_one_dropped():
    # Channel a sampled at t=0 (value 90); channel b limits the AND
    # intersection to [500,600).  With TTL 1000 the t=0 sample is still valid
    # through the fault and witnesses it; with TTL 50 it expired long before
    # the intersection, so the AND rule never fires at all.
    a = [ev(1, 0, 90, "d", "a"), ev(2, 1500, 10, "d", "a")]
    b = [ev(3, 500, 5, "d", "b"), ev(4, 600, 1, "d", "b")]
    conditions = [Condition("d", "a", ">", 50), Condition("d", "b", ">", 3)]
    channels = {("d", "a"): a, ("d", "b"): b}

    result = evaluate_rule(
        1, "AND", 0, conditions, channels, horizon=1500,
        ttl_by_channel={("d", "a"): 1000},
    )
    assert [(f.start, f.end) for f in result.faults] == [(500, 600)]
    groups = evidence_by_metric(result.faults[0])
    assert [e.id for e in groups["a"].events] == [1]
    assert groups["a"].held_intervals == [Interval(500, 600)]

    result = evaluate_rule(
        1, "AND", 0, conditions, channels, horizon=1500,
        ttl_by_channel={("d", "a"): 50},
    )
    assert result.faults == []


def test_ttl_gap_fault_only_cites_samples_valid_inside_it():
    # Samples at t=0/300/600 with TTL 100: three faults [0,100), [300,400),
    # [600,700).  Each fault cites exactly the sample whose capped span
    # overlaps it — an expired neighbour never leaks across the gap.
    events = [ev(1, 0, 90), ev(2, 300, 90), ev(3, 600, 90), ev(4, 900, 10)]
    result = evaluate_rule(
        1, "AND", 0, [Condition("d", "m", ">", 50)], {("d", "m"): events},
        horizon=900, ttl_by_channel={("d", "m"): 100},
    )
    assert [(f.start, f.end) for f in result.faults] == [
        (0, 100), (300, 400), (600, 700),
    ]
    for fault, (eid, start) in zip(result.faults, [(1, 0), (2, 300), (3, 600)]):
        group = fault.evidence[0]
        assert [e.id for e in group.events] == [eid]
        assert group.held_intervals == [Interval(start, start + 100)]


def test_reset_threshold_band_sample_holding_into_fault_is_evidence():
    # a: trigger 90 at t=0, hysteresis-band 77 at t=500 (stays latched),
    # reset 70 at t=1000.  b limits the AND fault to [600,900).  The band
    # sample at t=500 holds through the whole fault and witnesses it; the
    # original trigger sample at t=0 ends its hold at t=500 and does not.
    a = [ev(1, 0, 90, "d", "a"), ev(2, 500, 77, "d", "a"), ev(3, 1000, 70, "d", "a")]
    b = [ev(4, 600, 5, "d", "b"), ev(5, 900, 1, "d", "b")]
    result = evaluate_rule(
        1,
        "AND",
        0,
        [Condition("d", "a", ">", 80, reset_threshold=75), Condition("d", "b", ">", 3)],
        {("d", "a"): a, ("d", "b"): b},
        horizon=1000,
    )
    assert [(f.start, f.end) for f in result.faults] == [(600, 900)]
    groups = evidence_by_metric(result.faults[0])
    assert [(e.id, e.value) for e in groups["a"].events] == [(2, 77)]
    assert groups["a"].held_intervals == [Interval(600, 900)]


# --------------------------------------------------------------------------- #
# services / API / export read one consistent fixed result
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


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def _import_pre_fault_dataset():
    """temp sampled once at 00:00 (holds 90 until 00:30); vib high 00:10-00:20."""
    rows = [
        {"device": "d", "metric": "temp", "ts": "2026-09-23T00:00:00Z", "value": 90},
        {"device": "d", "metric": "temp", "ts": "2026-09-23T00:30:00Z", "value": 10},
        {"device": "d", "metric": "vib", "ts": "2026-09-23T00:10:00Z", "value": 5},
        {"device": "d", "metric": "vib", "ts": "2026-09-23T00:20:00Z", "value": 1},
    ]
    return services.import_events("\n".join(json.dumps(r) for r in rows))


def _create_and_rule():
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("AND 规则", "AND", 0, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, "d", "temp", ">=", 80, 0),
    )
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, "d", "vib", ">", 3, 1),
    )
    return rule_id


def test_stored_faults_carry_pre_fault_sample_evidence(scratch_db):
    _import_pre_fault_dataset()
    _create_and_rule()
    services.evaluate_all_rules()
    faults = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in faults] == [(at(10), at(20))]
    groups = {g["condition"]["metric"]: g for g in faults[0]["evidence"]}
    # the 00:00 temp sample (before the fault start) is the witness, and the
    # held sub-interval is clipped to the fault [00:10, 00:20)
    assert [e["ts"] for e in groups["temp"]["events"]] == [at(0)]
    assert groups["temp"]["held_intervals"] == [[at(10), at(20)]]
    assert [e["ts"] for e in groups["vib"]["events"]] == [at(10)]


def test_faults_timeline_export_and_fault_events_read_same_evidence(client):
    client.post(
        "/api/import/text",
        json={
            "text": "\n".join(
                json.dumps(r)
                for r in [
                    {"device": "d", "metric": "temp", "ts": "2026-09-23T00:00:00Z", "value": 90},
                    {"device": "d", "metric": "temp", "ts": "2026-09-23T00:30:00Z", "value": 10},
                    {"device": "d", "metric": "vib", "ts": "2026-09-23T00:10:00Z", "value": 5},
                    {"device": "d", "metric": "vib", "ts": "2026-09-23T00:20:00Z", "value": 1},
                ]
            )
        },
    )
    client.post(
        "/api/rules",
        json={
            "name": "AND 规则",
            "logic": "AND",
            "duration": 0,
            "conditions": [
                {"device": "d", "metric": "temp", "op": ">=", "threshold": 80},
                {"device": "d", "metric": "vib", "op": ">", "threshold": 3},
            ],
        },
    )
    client.post("/api/evaluate")

    def temp_group(evidence):
        return next(g for g in evidence if g["condition"]["metric"] == "temp")

    faults = client.get("/api/faults").json()
    assert len(faults) == 1
    expected_events = [{"event_id": 1, "ts": at(0), "value": 90.0}]
    assert temp_group(faults[0]["evidence"])["events"] == expected_events
    assert temp_group(faults[0]["evidence"])["held_intervals"] == [[at(10), at(20)]]

    # the timeline (page detail) and the fault-event view carry the same item
    timeline = client.get("/api/timeline").json()
    assert temp_group(timeline["faults"][0]["evidence"])["events"] == expected_events
    fault_events = client.get("/api/fault-events").json()
    assert len(fault_events) == 1
    assert temp_group(fault_events[0]["members"][0]["evidence"])["events"] == expected_events

    # export: the pre-fault sample predates the queried event window, so it
    # rides along via evidence_only_events while the fault evidence itself
    # stays identical to the API/timeline reads
    exported = client.get(
        "/api/export", params={"start": "2026-09-23T00:05:00Z"}
    ).json()
    assert temp_group(exported["fault_intervals"][0]["evidence"])["events"] == expected_events
    assert [e["id"] for e in exported["evidence_only_events"]] == [1]
    assert exported["evidence_only_events"][0]["ts"] == at(0)
    # fault events inside the export bundle agree as well
    assert temp_group(
        exported["fault_events"][0]["members"][0]["evidence"]
    )["events"] == expected_events
