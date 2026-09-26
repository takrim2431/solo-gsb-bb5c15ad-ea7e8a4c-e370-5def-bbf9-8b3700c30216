"""Tests for per-channel sample validity periods (TTL).

A TTL configured for a ``(device, metric)`` channel caps each sample's held
half-open interval to ``[ts, min(next_ts, ts + ttl, horizon))``.  After
expiry the channel holds nothing: conditions drop out of AND/OR combinations,
the sustained duration re-accumulates after each invalid gap, and fault
intervals, confirmation instants and trigger evidence never cross a gap.
Channels without a TTL keep the legacy hold semantics.
"""

from app.engine import (
    Condition,
    Event,
    Interval,
    MaintenanceWindow,
    channel_held_intervals,
    evaluate_rule,
)
import pytest


def mkevent(eid, ts, value, device="d", metric="m"):
    return Event(id=eid, device=device, metric=metric, ts=ts, value=value)


def ivs(faults):
    return [(f.start, f.end) for f in faults]


def series(eid_base, values, start=0, step=300, device="d", metric="m"):
    """One sample every ``step`` seconds with the given values."""
    return [
        mkevent(eid_base + i, start + i * step, v, device, metric)
        for i, v in enumerate(values)
    ]


def mw(wid, device, start, end, name="检修"):
    return MaintenanceWindow(id=wid, device=device, start=start, end=end, name=name)


# --------------------------------------------------------------------------- #
# channel_held_intervals
# --------------------------------------------------------------------------- #


def test_ttl_caps_hold_before_next_sample():
    # samples 300s apart; TTL of 120s truncates every held segment
    events = series(1, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    held, witnesses = channel_held_intervals(events, cond, horizon=1000, ttl=120)
    assert held == [Interval(0, 120), Interval(300, 420), Interval(600, 720)]
    assert [w.id for w in witnesses] == [1, 2, 3]


def test_ttl_caps_last_sample_before_horizon():
    events = [mkevent(1, 0, 90), mkevent(2, 60, 90)]
    cond = Condition("d", "m", ">", 50)
    # legacy: last sample held through horizon
    held, _ = channel_held_intervals(events, cond, horizon=600)
    assert held == [Interval(0, 600)]
    # with TTL 120: first piece [0,120) overlaps the second [60,180) (the
    # next sample arrives well within the TTL), so they merge to [0,180);
    # the tail still ends at the last sample's ts + ttl, not the horizon
    held, _ = channel_held_intervals(events, cond, horizon=600, ttl=120)
    assert held == [Interval(0, 180)]
    # sparse samples: cap lands before the next sample -> real invalid gap
    sparse = [mkevent(1, 0, 90), mkevent(2, 300, 90)]
    held, _ = channel_held_intervals(sparse, cond, horizon=600, ttl=120)
    assert held == [Interval(0, 120), Interval(300, 420)]
    # horizon still wins when ttl reaches beyond it: touching pieces merge
    held, _ = channel_held_intervals(events, cond, horizon=100, ttl=500)
    assert held == [Interval(0, 100)]


def test_ttl_equal_to_sample_spacing_touches_and_merges():
    # cap lands exactly on the next sample timestamp: half-open touching
    # intervals are continuous, so the union stays one piece
    events = series(1, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    held, _ = channel_held_intervals(events, cond, horizon=1000, ttl=300)
    assert held == [Interval(0, 900)]


def test_ttl_only_caps_matching_samples():
    # a non-matching sample simply holds nothing; TTL cannot resurrect it
    events = series(1, [90, 10, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    held, _ = channel_held_intervals(events, cond, horizon=1000, ttl=120)
    assert held == [Interval(0, 120), Interval(600, 720)]


def test_non_positive_ttl_is_rejected():
    cond = Condition("d", "m", ">", 50)
    with pytest.raises(ValueError):
        channel_held_intervals([mkevent(1, 0, 90)], cond, horizon=10, ttl=0)
    with pytest.raises(ValueError):
        channel_held_intervals([mkevent(1, 0, 90)], cond, horizon=10, ttl=-5)


# --------------------------------------------------------------------------- #
# evaluate_rule: gaps, duration reset, AND/OR, evidence
# --------------------------------------------------------------------------- #


def test_ttl_gap_splits_truth_segments():
    events = series(100, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 0, [cond], {("d", "m"): events},
        horizon=1000, ttl_by_channel={("d", "m"): 120},
    )
    assert ivs(result.faults) == [(0, 120), (300, 420), (600, 720)]
    # each segment confirms independently at its own start (duration 0)
    assert [f.confirmed_at for f in result.faults] == [0, 300, 600]


def test_ttl_gap_resets_sustained_duration():
    # each 120s piece is long enough for 120s but not for a 200s requirement;
    # duration must re-accumulate from zero after the invalid gap
    events = series(100, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 200, [cond], {("d", "m"): events},
        horizon=1000, ttl_by_channel={("d", "m"): 120},
    )
    assert result.faults == []

    result = evaluate_rule(
        1, "AND", 120, [cond], {("d", "m"): events},
        horizon=1000, ttl_by_channel={("d", "m"): 120},
    )
    assert ivs(result.faults) == [(0, 120), (300, 420), (600, 720)]
    assert [f.confirmed_at for f in result.faults] == [120, 420, 720]


def test_ttl_and_rule_drops_out_during_one_channel_gap():
    # AND of two channels; channel a expires between samples (120s TTL vs
    # 300s spacing) while channel b holds continuously.
    a = series(100, [90, 90, 90], step=300, device="d", metric="a")
    b = series(200, [90, 90, 90, 90], step=150, device="d", metric="b")
    result = evaluate_rule(
        2,
        "AND",
        0,
        [Condition("d", "a", ">", 50), Condition("d", "b", ">", 50)],
        {("d", "a"): a, ("d", "b"): b},
        horizon=1000,
        ttl_by_channel={("d", "a"): 120},
    )
    assert ivs(result.faults) == [(0, 120), (300, 420), (600, 720)]


def test_ttl_or_rule_combines_each_channels_valid_pieces():
    # a holds [0,120) [300,420) ...; b holds a compensating [120,300):
    # OR covers [0,420) in two touching pieces, but the real gap [420,600)
    # on both channels stays open.
    a = series(100, [90, 90], step=300, device="d", metric="a")
    b = [
        mkevent(1, 120, 90, device="d", metric="b"),
        mkevent(2, 200, 90, device="d", metric="b"),
        mkevent(3, 420, 10, device="d", metric="b"),
    ]
    result = evaluate_rule(
        3,
        "OR",
        0,
        [Condition("d", "a", ">", 50), Condition("d", "b", ">", 50)],
        {("d", "a"): a, ("d", "b"): b},
        horizon=1000,
        ttl_by_channel={("d", "a"): 120},
    )
    # a: [0,120),[300,420); b: [120,300) (held to its own next sample 420) —
    # all three touch, merging into [0,420); nothing covers [420,600)
    assert ivs(result.faults) == [(0, 420)]


def test_ttl_evidence_never_crosses_invalid_gap():
    events = series(100, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 120, [cond], {("d", "m"): events},
        horizon=1000, ttl_by_channel={("d", "m"): 120},
    )
    assert len(result.faults) == 3
    for fault, start in zip(result.faults, (0, 300, 600)):
        group = fault.evidence[0]
        assert group.held_intervals == [Interval(start, start + 120)]
        assert [w.ts for w in group.events] == [start]


def test_ttl_last_sample_fault_ends_at_ts_plus_ttl():
    # a single high final sample with no later data: fault ends at ts + ttl,
    # never stretching to the horizon
    events = [mkevent(1, 0, 10), mkevent(2, 500, 90)]
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 0, [cond], {("d", "m"): events},
        horizon=1200, ttl_by_channel={("d", "m"): 120},
    )
    assert ivs(result.faults) == [(500, 620)]
    assert result.faults[0].evidence[0].held_intervals == [Interval(500, 620)]


def test_ttl_without_configuration_keeps_legacy_hold():
    events = series(100, [90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 0, [cond], {("d", "m"): events},
        horizon=1000, ttl_by_channel={("d", "other"): 60},
    )
    # unrelated channel TTL must not affect this channel
    assert ivs(result.faults) == [(0, 1000)]


# --------------------------------------------------------------------------- #
# TTL + planned maintenance together
# --------------------------------------------------------------------------- #


def test_ttl_gap_and_maintenance_both_reset_duration():
    # samples high every 300s; TTL 120 opens [120,300) gaps; maintenance
    # additionally removes [300,420) (which coincides with a valid piece).
    events = series(100, [90, 90, 90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 120, [cond], {("d", "m"): events},
        horizon=2000,
        maintenance=[mw(1, "d", 300, 420)],
        ttl_by_channel={("d", "m"): 120},
    )
    # surviving pieces: [0,120), [600,720), [900,1020); the [300,420) piece
    # is fully inside maintenance -> suppressed
    assert ivs(result.faults) == [(0, 120), (600, 720), (900, 1020)]
    assert [(s.interval.start, s.interval.end) for s in result.suppressed] == [
        (300, 420)
    ]
    assert result.suppressed[0].maintenance_ids == [1]


def test_ttl_expiry_is_not_reported_as_maintenance_suppression():
    # A truth loss caused purely by TTL expiry (no maintenance covering it)
    # must not appear in suppressed segments.
    events = series(100, [90, 90], step=300)
    cond = Condition("d", "m", ">", 50)
    result = evaluate_rule(
        1, "AND", 0, [cond], {("d", "m"): events},
        horizon=1000,
        maintenance=[mw(1, "other", 0, 1000)],
        ttl_by_channel={("d", "m"): 120},
    )
    assert ivs(result.faults) == [(0, 120), (300, 420)]
    assert result.suppressed == []
