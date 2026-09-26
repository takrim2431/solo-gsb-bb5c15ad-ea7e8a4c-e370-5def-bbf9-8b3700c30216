"""Tests for planned-maintenance semantics in the rule engine.

Maintenance windows are named half-open intervals on a device.  Evaluation
merges overlapping/touching windows per device, subtracts them from that
device's truth segments (resetting the sustained duration), and reports the
suppressed pieces separately.
"""

from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    evaluate_rule,
    merge_device_windows,
    split_with_window_attribution,
    subtract,
)


def ev(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def minute_series(eid_base, predicate_fn, device="d", metric="m"):
    """One sample per minute for minutes 0..30, value from predicate_fn(minute)."""
    return [
        ev(eid_base + m, m * 60, predicate_fn(m), device, metric)
        for m in range(31)
    ]


def ivs(faults):
    return [(f.start, f.end) for f in faults]


def mw(wid, device, start_min, end_min, name="检修"):
    return MaintenanceWindow(
        id=wid, device=device, start=start_min * 60, end=end_min * 60, name=name
    )


# --------------------------------------------------------------------------- #
# subtract / merge_device_windows primitives
# --------------------------------------------------------------------------- #


def test_subtract_middle_splits_interval():
    assert subtract([Interval(0, 10)], [Interval(3, 6)]) == [
        Interval(0, 3),
        Interval(6, 10),
    ]


def test_subtract_touching_boundary_keeps_interval_intact():
    # half-open: [0,6) minus a window starting exactly at 6 loses nothing
    assert subtract([Interval(0, 6)], [Interval(6, 10)]) == [Interval(0, 6)]
    assert subtract([Interval(6, 10)], [Interval(0, 6)]) == [Interval(6, 10)]


def test_subtract_multiple_windows_and_full_cover():
    assert subtract([Interval(0, 20)], [Interval(2, 4), Interval(8, 12)]) == [
        Interval(0, 2),
        Interval(4, 8),
        Interval(12, 20),
    ]
    assert subtract([Interval(0, 10)], [Interval(0, 10)]) == []
    assert subtract([], [Interval(0, 10)]) == []
    assert subtract([Interval(0, 10)], []) == [Interval(0, 10)]


def test_merge_device_windows_merges_overlap_and_touch_per_device():
    merged = merge_device_windows(
        [
            ("pump-1", 0.0, 60.0),
            ("pump-1", 60.0, 120.0),   # touching -> merges
            ("pump-1", 100.0, 200.0),  # overlapping -> merges
            ("pump-1", 300.0, 360.0),  # real gap -> separate
            ("pump-2", 0.0, 60.0),     # other device never merges
        ]
    )
    assert merged["pump-1"] == [Interval(0, 200), Interval(300, 360)]
    assert merged["pump-2"] == [Interval(0, 60)]


def test_split_with_window_attribution_boundaries():
    win = lambda wid, s, e: MaintenanceWindow(id=wid, device="d", start=s, end=e)
    # zero-length pieces / windows are dropped, zero-length intersections emit nothing
    assert split_with_window_attribution([Interval(10, 10)], [win(1, 0, 20)]) == []
    assert split_with_window_attribution([Interval(0, 10)], [win(1, 5, 5)]) == []
    # a window only touching at an endpoint covers no half-open subpiece, so
    # the piece is dropped rather than attributed with no covering window
    assert split_with_window_attribution([Interval(0, 5)], [win(1, 5, 10)]) == []
    # windows fully outside the piece do not bound or attribute it
    assert split_with_window_attribution(
        [Interval(0, 10)], [win(1, 20, 30), win(2, -5, 0)]
    ) == []
    # overlapping windows share the overlap; touching windows switch at junction
    pieces = split_with_window_attribution(
        [Interval(0, 10)],
        [win(7, 0, 6), win(8, 6, 10), win(9, 2, 4)],
    )
    assert [(p.start, p.end, ids) for p, ids in pieces] == [
        (0, 2, [7]),
        (2, 4, [7, 9]),
        (4, 6, [7]),
        (6, 10, [8]),
    ]


# --------------------------------------------------------------------------- #
# evaluation semantics
# --------------------------------------------------------------------------- #


def test_maintenance_resets_duration_and_reconfirms_after_window():
    # temp >= 80 held on [6,17); maintenance [10,12) cuts it in two.
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 12)],
    )
    # [6,10) is 4 min < 5 min -> dropped; [12,17) is 5 min -> fault
    assert ivs(result.faults) == [(12 * 60, 17 * 60)]
    # duration re-accumulates from the post-window segment start
    assert result.faults[0].confirmed_at == 12 * 60 + 300
    # the cut piece is reported as suppressed, linked to the window
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (10 * 60, 12 * 60)
    ]
    assert result.suppressed[0].maintenance_ids == [1]
    assert result.suppressed[0].device == "d" and result.suppressed[0].metric == "m"


def test_maintenance_without_windows_keeps_legacy_behavior():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events}, horizon=30 * 60
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []


def test_maintenance_on_other_device_does_not_affect_rule():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "other-device", 6, 17)],
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []


def test_touching_maintenance_windows_merge_before_subtraction():
    # two adjacent windows [8,10) and [10,12) act as one [8,12) cut
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 8, 10, "A"), mw(2, "d", 10, 12, "B")],
    )
    assert ivs(result.faults) == [(12 * 60, 17 * 60)]
    # the merged cut is one continuous span for truth/duration purposes, but
    # attribution follows the half-open junction: each piece names only the
    # window actually covering it (window 1 ends at 10*60, window 2 starts
    # there), so the suppressed span is split at 10*60.
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (8 * 60, 10 * 60, [1]),
        (10 * 60, 12 * 60, [2]),
    ]


def test_separate_windows_attribute_only_the_covering_window():
    # Two far-apart windows each cut a fault: neither suppressed piece may
    # point at the other window (regression for device-wide id attribution).
    events = minute_series(100, lambda m: 90 if 2 <= m <= 28 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 120, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 6, 8, "A"), mw(2, "d", 20, 22, "B")],
    )
    attributed = [
        (s.interval.start, s.interval.end, tuple(s.maintenance_ids))
        for s in result.suppressed
    ]
    assert attributed == [
        (6 * 60, 8 * 60, (1,)),
        (20 * 60, 22 * 60, (2,)),
    ]


def test_overlapping_windows_share_attribution_on_their_overlap():
    # [9,11) and [10,12) overlap on [10,11); the suppressed span is split at
    # both boundaries with joint attribution only inside the overlap.
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 9, 11, "A"), mw(2, "d", 10, 12, "B")],
    )
    assert [
        (s.interval.start, s.interval.end, s.maintenance_ids)
        for s in result.suppressed
    ] == [
        (9 * 60, 10 * 60, [1]),
        (10 * 60, 11 * 60, [1, 2]),
        (11 * 60, 12 * 60, [2]),
    ]


def test_evidence_never_crosses_maintenance_boundary():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 12)],
    )
    evidence = result.faults[0].evidence[0]
    # held sub-intervals are clipped at the window start
    assert evidence.held_intervals == [Interval(12 * 60, 17 * 60)]
    # only samples at/after the window end witness the surviving fault
    assert [w.ts for w in evidence.events] == [m * 60 for m in range(12, 17)]


def test_maintenance_covering_whole_truth_suppresses_fault():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 10 else 10)  # [6,11)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 5, 12)],
    )
    assert result.faults == []
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (6 * 60, 11 * 60)
    ]


def test_and_rule_maintenance_on_one_condition_device():
    # AND of temp (device d) and vib (device e); maintenance on e only.
    temp = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 60, device="d", metric="temp")
    vib = minute_series(200, lambda m: 3.6 if 6 <= m <= 16 else 1.0, device="e", metric="vib")
    result = evaluate_rule(
        2,
        "AND",
        120,
        [Condition("d", "temp", ">=", 80), Condition("e", "vib", ">", 3)],
        {("d", "temp"): temp, ("e", "vib"): vib},
        horizon=30 * 60,
        maintenance=[mw(1, "e", 10, 12)],
    )
    # truth [6,17) minus [10,12): [6,10) and [12,17), both >= 120s
    assert ivs(result.faults) == [(6 * 60, 10 * 60), (12 * 60, 17 * 60)]
    assert result.faults[0].confirmed_at == 6 * 60 + 120
    assert result.faults[1].confirmed_at == 12 * 60 + 120
    # suppressed piece attributed to the vib channel (device e), not temp
    assert [(s.device, s.metric, s.interval.start, s.interval.end) for s in result.suppressed] == [
        ("e", "vib", 10 * 60, 12 * 60)
    ]


def test_or_rule_suppressed_only_where_rule_truth_is_lost():
    # vib held [3,9), press held [6,14); maintenance on vib's device [4,8).
    # The rule's OR truth survives on [6,8) via press, so only [4,6) is lost.
    vib = minute_series(300, lambda m: 6.0 if 3 <= m <= 8 else 2.0, device="d", metric="vib")
    press = minute_series(400, lambda m: 26.0 if 6 <= m <= 13 else 52.0, device="e", metric="press")
    result = evaluate_rule(
        3,
        "OR",
        0,
        [Condition("d", "vib", ">", 5), Condition("e", "press", "<", 30)],
        {("d", "vib"): vib, ("e", "press"): press},
        horizon=30 * 60,
        maintenance=[mw(1, "d", 4, 8)],
    )
    assert ivs(result.faults) == [(3 * 60, 4 * 60), (6 * 60, 14 * 60)]
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (4 * 60, 6 * 60)
    ]


def test_maintenance_window_with_zero_length_is_ignored():
    events = minute_series(100, lambda m: 90 if 6 <= m <= 16 else 10)
    cond = Condition("d", "m", ">=", 80)
    result = evaluate_rule(
        1, "AND", 300, [cond], {("d", "m"): events},
        horizon=30 * 60, maintenance=[mw(1, "d", 10, 10)],
    )
    assert ivs(result.faults) == [(6 * 60, 17 * 60)]
    assert result.suppressed == []
