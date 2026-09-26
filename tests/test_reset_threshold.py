"""Tests for the optional condition **reset threshold** (latching predicate).

Only the ordering operators ``>``/``>=``/``<``/``<=`` accept a reset
threshold: a condition first becomes true when a held value satisfies the
original comparison, then stays latched while the value loiters inside the
hysteresis band, and disarms only when a held value reaches the reset
boundary (high trigger: ``value <= reset``; low trigger:
``value >= reset``).  An invalid TTL gap clears the latch as well, so the
next valid sample must re-cross the original trigger.  Everything downstream
(AND/OR, sustained duration, maintenance subtraction, evidence) keeps using
the existing evaluation chain on the resulting held intervals.
"""

import pytest

from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    at_reset_boundary,
    channel_held_intervals,
    evaluate_rule,
    validate_reset_threshold,
)


def ev(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def series(eid_base, values, start=0, step=60, device="d", metric="m"):
    """One sample every ``step`` seconds with the given values."""
    return [
        ev(eid_base + i, start + i * step, v, device, metric)
        for i, v in enumerate(values)
    ]


def ivs(faults):
    return [(f.start, f.end) for f in faults]


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def test_reset_threshold_validation():
    # high triggers need a strictly lower reset value
    validate_reset_threshold(">", 80, 79.9)
    validate_reset_threshold(">=", 80, 75)
    with pytest.raises(ValueError):
        validate_reset_threshold(">", 80, 80)
    with pytest.raises(ValueError):
        validate_reset_threshold(">", 80, 81)
    # low triggers need a strictly higher reset value
    validate_reset_threshold("<", 30, 30.1)
    validate_reset_threshold("<=", 30, 35)
    with pytest.raises(ValueError):
        validate_reset_threshold("<", 30, 30)
    with pytest.raises(ValueError):
        validate_reset_threshold("<=", 30, 29)
    # equality operators never support a reset threshold
    with pytest.raises(ValueError):
        validate_reset_threshold("==", 10, 5)
    with pytest.raises(ValueError):
        validate_reset_threshold("!=", 10, 5)


def test_condition_rejects_invalid_reset_threshold():
    with pytest.raises(ValueError):
        Condition("d", "m", "==", 10, reset_threshold=5)
    with pytest.raises(ValueError):
        Condition("d", "m", ">", 10, reset_threshold=10)
    with pytest.raises(ValueError):
        Condition("d", "m", "<", 10, reset_threshold=10)
    # accepted and normalized
    c = Condition("d", "m", ">=", 80, reset_threshold=75)
    assert c.reset_threshold == 75.0
    # legacy condition stays reset-free
    assert Condition("d", "m", ">", 80).reset_threshold is None


def test_condition_dict_roundtrip_carries_reset():
    for raw in (
        {"device": "d", "metric": "m", "op": ">", "threshold": 80, "reset_threshold": 75},
        {"device": "d", "metric": "m", "op": ">", "threshold": 80},
    ):
        cond = Condition.from_dict(raw)
        out = cond.to_dict()
        assert out["reset_threshold"] == raw.get("reset_threshold")


def test_at_reset_boundary_sides():
    assert at_reset_boundary(75, ">", 75) is True   # <= reset
    assert at_reset_boundary(74.9, ">", 75) is True
    assert at_reset_boundary(75.1, ">", 75) is False
    assert at_reset_boundary(35, "<", 35) is True   # >= reset
    assert at_reset_boundary(35.1, "<", 35) is True
    assert at_reset_boundary(34.9, "<", 35) is False


# --------------------------------------------------------------------------- #
# high-value trigger latching
# --------------------------------------------------------------------------- #


def test_high_trigger_latches_through_band_and_disarms_at_reset():
    # >80 arms at t=60; 77 sits inside (75,80] and must keep the latch;
    # 75 reaches the reset boundary (<=75) and disarms at t=180;
    # 90 crosses the trigger again at t=240; 70 disarms at t=300.
    events = series(1, [70, 85, 77, 75, 90, 70])
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    held, witnesses = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(60, 180), Interval(240, 300)]
    assert [w.id for w in witnesses] == [2, 3, 5]


def test_high_trigger_requires_original_comparison_first():
    # A sample inside the hysteresis band before any trigger sample must not
    # arm the latch: 77 (band), 85 (arms), 77 (latched), 70 (disarm).
    events = series(1, [77, 85, 77, 70])
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    held, _ = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(60, 180)]


def test_high_trigger_trigger_edge_equality_semantics():
    # >=80 arms on exactly 80; reset boundary is value <= 75, also inclusive.
    events = series(1, [80, 75, 80, 74, 80, 70])
    cond = Condition("d", "m", ">=", 80, reset_threshold=75)
    held, _ = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(0, 60), Interval(120, 180), Interval(240, 300)]


def test_latch_extends_held_truth_through_trigger_threshold_dips():
    # With a plain >80 predicate the two 77 dips split the truth into pieces;
    # the reset threshold (75) bridges them into one continuous held interval.
    events = series(1, [70, 85, 77, 77, 90, 70])
    plain, _ = channel_held_intervals(events, Condition("d", "m", ">", 80), horizon=300)
    assert plain == [Interval(60, 120), Interval(240, 300)]
    latched, witnesses = channel_held_intervals(
        events, Condition("d", "m", ">", 80, reset_threshold=75), horizon=300
    )
    assert latched == [Interval(60, 300)]
    assert [w.ts for w in witnesses] == [60, 120, 180, 240]


# --------------------------------------------------------------------------- #
# low-value trigger latching
# --------------------------------------------------------------------------- #


def test_low_trigger_latches_through_band_and_disarms_at_reset():
    # <30 arms at t=60; 33 sits inside [30,35) and keeps the latch;
    # 35 reaches the reset boundary (>=35) and disarms at t=180;
    # 20 re-arms at t=240; 40 disarms at t=300.
    events = series(1, [40, 25, 33, 35, 20, 40])
    cond = Condition("d", "m", "<", 30, reset_threshold=35)
    held, witnesses = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(60, 180), Interval(240, 300)]
    assert [w.id for w in witnesses] == [2, 3, 5]


def test_low_trigger_requires_original_comparison_first():
    events = series(1, [33, 25, 33, 40])
    cond = Condition("d", "m", "<", 30, reset_threshold=35)
    held, _ = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(60, 180)]


def test_low_trigger_trigger_edge_equality_semantics():
    # <=30 arms on exactly 30; reset boundary value >= 35 disarms.
    events = series(1, [30, 35, 30, 36])
    cond = Condition("d", "m", "<=", 30, reset_threshold=35)
    held, _ = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(0, 60), Interval(120, 180)]


# --------------------------------------------------------------------------- #
# rule-level: duration, AND/OR, maintenance, TTL
# --------------------------------------------------------------------------- #


def test_latched_held_interval_passes_duration_as_one_fault():
    # Plain >80 gives two short pieces (each 60s, below the 120s requirement);
    # the reset threshold bridges the band dip into one 240s latched fault.
    events = series(1, [70, 85, 77, 77, 90, 70])
    cond_plain = Condition("d", "m", ">", 80)
    plain = evaluate_rule(1, "AND", 120, [cond_plain], {("d", "m"): events}, horizon=300)
    assert plain.faults == []
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    result = evaluate_rule(1, "AND", 120, [cond], {("d", "m"): events}, horizon=300)
    assert ivs(result.faults) == [(60, 300)]
    assert result.faults[0].confirmed_at == 180
    group = result.faults[0].evidence[0]
    assert [w.ts for w in group.events] == [60, 120, 180, 240]
    assert group.held_intervals == [Interval(60, 300)]


def test_reset_threshold_works_in_or_rule():
    # channel a flickers across its trigger but stays inside its band, while b
    # never triggers; OR truth must follow a's single latched interval.
    a = series(100, [90, 77, 90, 70], device="d", metric="a")
    b = series(200, [1, 1, 1, 1], device="d", metric="b")
    result = evaluate_rule(
        2,
        "OR",
        0,
        [
            Condition("d", "a", ">", 80, reset_threshold=75),
            Condition("d", "b", ">", 5),
        ],
        {("d", "a"): a, ("d", "b"): b},
        horizon=240,
    )
    assert ivs(result.faults) == [(0, 180)]


def test_reset_threshold_works_in_and_rule():
    # a latches through a band dip; b holds throughout; AND truth is a's
    # latched interval (both [0,120)+(180... etc. per a's own pattern).
    a = series(100, [90, 90, 10, 90], device="d", metric="a")
    b = series(200, [5, 5, 5, 5], device="d", metric="b")
    result = evaluate_rule(
        2,
        "AND",
        0,
        [
            Condition("d", "a", ">", 80, reset_threshold=15),
            Condition("d", "b", ">", 3),
        ],
        {("d", "a"): a, ("d", "b"): b},
        horizon=240,
    )
    # a arms [0,120); value 10 <= 15 disarms at t=120; 90 re-arms [180,240)
    assert ivs(result.faults) == [(0, 120), (180, 240)]


def test_maintenance_cuts_latched_truth_and_resets_duration():
    # Latched truth would be [60,300); maintenance [120,180) is subtracted
    # exactly as for a plain predicate, splitting it into two pieces.
    events = series(1, [70, 85, 77, 77, 90, 70])
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    result = evaluate_rule(
        1, "AND", 0, [cond], {("d", "m"): events},
        horizon=300, maintenance=[MaintenanceWindow(1, "d", 120, 180, "检修")],
    )
    assert ivs(result.faults) == [(60, 120), (180, 300)]
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (120, 180)
    ]
    # evidence after the window starts only at samples at/after its end
    surviving = result.faults[1].evidence[0]
    assert surviving.held_intervals == [Interval(180, 300)]
    assert [w.ts for w in surviving.events] == [180, 240]


# --------------------------------------------------------------------------- #
# TTL gaps clear the latch
# --------------------------------------------------------------------------- #


def test_ttl_gap_clears_latch_band_sample_does_not_rearm():
    # t=0 value 90 arms; TTL 50 opens a [50,100) gap; the next sample t=100
    # is 77 — inside the band, but the latch was cleared by the gap, so it
    # must not re-arm (the original comparison >80 is not satisfied).
    events = [
        ev(1, 0, 90),
        ev(2, 100, 77),
        ev(3, 200, 90),
        ev(4, 260, 70),
    ]
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    held, witnesses = channel_held_intervals(events, cond, horizon=400, ttl=50)
    assert held == [Interval(0, 50), Interval(200, 250)]
    assert [w.id for w in witnesses] == [1, 3]


def test_ttl_gap_clears_latch_trigger_sample_rearms():
    # After the gap the value again satisfies >80, so re-arming is allowed.
    events = [ev(1, 0, 90), ev(2, 100, 90), ev(3, 200, 70)]
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    held, _ = channel_held_intervals(events, cond, horizon=300, ttl=50)
    assert held == [Interval(0, 50), Interval(100, 150)]


def test_ttl_touching_pieces_keep_latch():
    # TTL equal to sample spacing: pieces touch (no open gap), so the latch
    # survives across the touching boundary.
    events = series(1, [90, 77, 70], step=50)
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    held, _ = channel_held_intervals(events, cond, horizon=300, ttl=50)
    # arm [0,50); 77 band keeps it [50,100); 70 disarms at t=100 -> [0,100)
    assert held == [Interval(0, 100)]


def test_ttl_gap_resets_latched_duration_in_rule():
    # Two TTL-truncated arms separated by a real gap; duration 50 makes both
    # pieces confirmed faults independently with separate confirmation times.
    events = [ev(1, 0, 90), ev(2, 100, 90), ev(3, 200, 70)]
    cond = Condition("d", "m", ">", 80, reset_threshold=75)
    result = evaluate_rule(
        1, "AND", 50, [cond], {("d", "m"): events},
        horizon=300, ttl_by_channel={("d", "m"): 50},
    )
    assert ivs(result.faults) == [(0, 50), (100, 150)]
    assert [f.confirmed_at for f in result.faults] == [50, 150]
