"""Unit tests for the read-only single-channel abrupt-change recovery diagnostic.

Adjacent valid samples are compared by change rate over their actual time
difference; TTL-expiry gaps cut continuity (no jump across a gap); the last
sample before the window start may serve as the baseline; super-threshold
jumps look forward within the observation duration for the first sample
back inside the baseline tolerance; already attributed samples never open a
second segment.
"""

import pytest

from app.recovery import (
    REASON_OBSERVATION_EXPIRED,
    REASON_RANGE_END,
    REASON_VALIDITY_GAP,
    diagnose_mutation_recovery,
)

P = 60.0
START = 1000.0
END = START + 10 * P
RATE = 0.1  # jump of 6 over 60s is exactly the threshold (not a jump)


def s(i, v, t0=START, step=P):
    """(id, ts, value) triple for sample index ``i``."""
    return (i + 1, t0 + i * step, v)


def at(ts, v, eid=1):
    """Raw (id, ts, value) triple at an arbitrary timestamp."""
    return (eid, ts, v)


def series(values, t0=START, step=P):
    return [s(i, v, t0, step) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# Basic jump + recovery
# --------------------------------------------------------------------------- #


def test_jump_and_recovery_uses_actual_elapsed_time():
    # baseline 10 -> 30 over 60s: rate 20/60 ~ 0.333 > 0.1, then back to 10
    samples = series([10, 30, 10])
    result = diagnose_mutation_recovery(
        samples, START, START + 3 * P, RATE, regression_tolerance=1.0, observation_seconds=300
    )
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.recovered is True
    assert seg.baseline.value == 10
    assert seg.jump.ts == START + P and seg.jump.value == 30
    assert seg.jump_rate == pytest.approx(20 / P)
    assert seg.end.ts == START + 2 * P and seg.end.value == 10
    assert seg.max_deviation == pytest.approx(20)
    assert seg.max_deviation_sample.value == 30
    assert seg.recovery_seconds == pytest.approx(P)
    assert seg.unrecovered_reason is None


def test_irregular_sampling_rate_uses_actual_gap():
    # a +19 change spread over 200s is 0.095/s: below the 0.1 threshold,
    # although the same absolute change over 60s would have crossed it
    samples = [
        (1, START, 10.0),
        (2, START + 200, 29.0),
    ]
    result = diagnose_mutation_recovery(samples, START, START + 260, RATE, 1.0, 300)
    assert result.segments == []


def test_rate_equal_to_threshold_is_not_a_jump():
    # 10 -> 16 over 60s == 0.1 exactly: strict greater-than, no segment
    samples = series([10, 16, 10])
    result = diagnose_mutation_recovery(samples, START, START + 3 * P, RATE, 1.0, 300)
    assert result.segments == []


def test_regression_inside_tolerance_boundary_counts_as_recovered():
    # |11 - 10| == tolerance 1: recovered (<= is back inside)
    samples = series([10, 30, 11])
    result = diagnose_mutation_recovery(samples, START, START + 3 * P, RATE, 1.0, 300)
    assert len(result.segments) == 1
    assert result.segments[0].recovered is True


def test_first_back_to_baseline_sample_is_the_recovery_sample():
    samples = series([10, 30, 25, 11, 10])
    result = diagnose_mutation_recovery(samples, START, START + 5 * P, RATE, 1.0, 300)
    seg = result.segments[0]
    assert seg.recovered is True
    assert seg.end.ts == START + 3 * P  # value 11 at i=3, not the later 10
    assert seg.max_deviation == pytest.approx(20)


def test_max_deviation_tracks_largest_excursion_while_open():
    samples = series([10, 20, 40, 20, 10])
    result = diagnose_mutation_recovery(samples, START, START + 5 * P, RATE, 1.0, 300)
    seg = result.segments[0]
    assert seg.max_deviation == pytest.approx(30)
    assert seg.max_deviation_sample.ts == START + 2 * P
    assert seg.recovery_seconds == pytest.approx(3 * P)


def test_recovery_after_observation_duration_is_unrecovered():
    # jump at t=60; observation 120s; the first back-to-baseline sample is
    # at t=240 (180s after the jump), after the deadline t=180
    samples = [
        at(START, 10),
        at(START + P, 30),
        at(START + 2 * P, 30),
        at(START + 3 * P, 15),
        at(START + 4 * P, 10),
        at(START + 5 * P, 10),
    ]
    result = diagnose_mutation_recovery(samples, START, START + 6 * P, RATE, 1.0, 120)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.recovered is False
    assert seg.unrecovered_reason == REASON_OBSERVATION_EXPIRED
    assert seg.end.ts == START + 3 * P  # last candidate at/before the deadline
    assert seg.recovery_seconds is None


def test_sample_just_inside_observation_window_recovers():
    # observation 120s exactly reaches the t=180 sample (jump at t=60): the
    # boundary is inclusive, so that back-to-baseline sample still recovers
    samples = [
        at(START, 10),
        at(START + P, 30),
        at(START + 2 * P, 30),
        at(START + 3 * P, 10),
        at(START + 4 * P, 10),
    ]
    result = diagnose_mutation_recovery(samples, START, START + 5 * P, RATE, 1.0, 120)
    seg = result.segments[0]
    assert seg.recovered is True
    assert seg.end.ts == START + 3 * P
    assert seg.recovery_seconds == pytest.approx(120)


def test_open_segment_at_window_end_is_range_end():
    # observation would complete after the window: range_end, not expired
    samples = series([10, 30, 30])
    result = diagnose_mutation_recovery(samples, START, START + 3 * P, RATE, 1.0, 1000)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.recovered is False
    assert seg.unrecovered_reason == REASON_RANGE_END
    assert seg.end.ts == START + 2 * P


# --------------------------------------------------------------------------- #
# Baseline before the window start
# --------------------------------------------------------------------------- #


def test_last_sample_before_window_serves_as_baseline():
    samples = [(1, START - 30, 10.0)] + series([30, 10])
    result = diagnose_mutation_recovery(samples, START, START + 2 * P, RATE, 1.0, 300)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.baseline.ts == START - 30
    assert seg.jump.ts == START
    assert seg.recovered is True


def test_expired_pre_window_sample_is_not_a_baseline_with_ttl():
    # last pre-window sample expired 15s before start (TTL 45): the in-window
    # jump has no contiguous predecessor, so no segment opens across the gap
    samples = [(1, START - 60, 10.0)] + series([90, 10])
    result = diagnose_mutation_recovery(
        samples, START, START + 2 * P, RATE, 1.0, 300, ttl=45
    )
    assert result.segments == []


def test_still_valid_pre_window_sample_is_a_baseline_with_ttl():
    samples = [(1, START - 30, 10.0)] + series([90, 10])
    result = diagnose_mutation_recovery(
        samples, START, START + 2 * P, RATE, 1.0, 300, ttl=45
    )
    assert len(result.segments) == 1
    assert result.segments[0].baseline.ts == START - 30


# --------------------------------------------------------------------------- #
# TTL gaps
# --------------------------------------------------------------------------- #


def test_ttl_gap_cuts_continuity_no_jump_judged_across_it():
    # +80 change across an expired gap: must not open a segment
    samples = series([10, 90, 10], step=120)
    result = diagnose_mutation_recovery(
        samples, START, START + 3 * 120, RATE, 1.0, 600, ttl=60
    )
    assert result.segments == []


def test_ttl_gap_while_waiting_recovery_is_validity_gap():
    # jump at t=60; a further sample at t=120 stays off-baseline; the next
    # sample at t=300 is across a TTL-expired gap (TTL 90s, expires t=210)
    samples = [
        at(START, 10),
        at(START + P, 40),
        at(START + 2 * P, 40),
        at(START + 5 * P, 10),
    ]
    result = diagnose_mutation_recovery(
        samples, START, START + 6 * P, RATE, 1.0, 600, ttl=90
    )
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.recovered is False
    assert seg.unrecovered_reason == REASON_VALIDITY_GAP
    assert seg.end.ts == START + 2 * P  # last sample before the gap


def test_gap_after_observation_deadline_is_observation_expired():
    # observation 30s expires at t=90 before the TTL gap (expiry t=210);
    # the post-deadline sample t=120 is not attributed and must not reopen
    samples = [
        at(START, 10),
        at(START + P, 40),
        at(START + 2 * P, 40),
        at(START + 5 * P, 10),
    ]
    result = diagnose_mutation_recovery(
        samples, START, START + 6 * P, RATE, 1.0, 30, ttl=90
    )
    assert len(result.segments) == 1
    assert result.segments[0].unrecovered_reason == REASON_OBSERVATION_EXPIRED
    assert result.segments[0].end.ts == START + P  # deadline t=90 had no sample


def test_ttl_gap_starting_exactly_at_window_end_is_range_end():
    # 60s spacing with TTL 60: neighbouring held pieces touch and stay
    # contiguous; the last sample's validity ends exactly on the window end
    samples = [
        at(START, 10),
        at(START + P, 40),
        at(START + 2 * P, 40),
    ]
    result = diagnose_mutation_recovery(
        samples, START, START + 2 * P + 30, RATE, 1.0, 600, ttl=60
    )
    assert len(result.segments) == 1
    assert result.segments[0].unrecovered_reason == REASON_RANGE_END


# --------------------------------------------------------------------------- #
# Attribution: no double opening
# --------------------------------------------------------------------------- #


def test_attributed_samples_do_not_open_another_segment():
    # 10 -> 40 -> 10 (recovered at i=2): i=2 is attributed and must not
    # pair backwards as a "jump"; then 10 -> 50 at i=3 opens the next segment
    samples = series([10, 40, 10, 50, 10])
    result = diagnose_mutation_recovery(samples, START, START + 5 * P, RATE, 1.0, 300)
    assert len(result.segments) == 2
    assert result.segments[0].jump.ts == START + P
    assert result.segments[1].jump.ts == START + 3 * P
    assert result.segments[1].baseline.value == 10


def test_recovered_sample_not_reused_as_jump():
    samples = series([10, 40, 9, 5])
    result = diagnose_mutation_recovery(samples, START, START + 4 * P, RATE, 1.0, 300)
    # i=2 (9) recovers; the 9 -> 5 step at i=3 is below rate (4/60 < 0.1)
    assert len(result.segments) == 1
    assert result.segments[0].recovered is True


def test_new_jump_after_expired_segment_opens_fresh_segment():
    # jump t=30 (10 -> 40) expires on observation at t=120 (deadline t=90);
    # the t=120 step 40 -> 45 is below the rate threshold and does not open;
    # the t=180 step 45 -> 10 is a fresh jump and recovers at t=220
    samples = [
        at(START, 10),
        at(START + 30, 40),
        at(START + 2 * P, 45),
        at(START + 3 * P, 10),
        at(START + 220, 45),
    ]
    result = diagnose_mutation_recovery(samples, START, START + 260, RATE, 1.0, 60)
    assert len(result.segments) == 2
    first, second = result.segments
    assert first.jump.ts == START + 30
    assert first.unrecovered_reason == REASON_OBSERVATION_EXPIRED
    assert first.end.ts == START + 30  # deadline t=90 had no in-window sample
    assert second.jump.ts == START + 3 * P
    # t=220 is within the second segment's 60s observation window (deadline
    # t=240) and back at the baseline 45 ± 1: recovered after 40s
    assert second.recovered is True
    assert second.end.ts == START + 220
    assert second.recovery_seconds == pytest.approx(40)
    assert second.baseline.value == 45


def test_multiple_independent_segments():
    samples = series([10, 40, 10, 10, 45, 10])
    result = diagnose_mutation_recovery(samples, START, START + 6 * P, RATE, 1.0, 300)
    assert len(result.segments) == 2
    assert [seg.jump.ts for seg in result.segments] == [START + P, START + 4 * P]
    assert all(seg.recovered for seg in result.segments)


# --------------------------------------------------------------------------- #
# Window half-open semantics / misc
# --------------------------------------------------------------------------- #


def test_sample_on_window_end_is_excluded():
    samples = series([10, 40, 10])
    # end lands exactly on the recovery sample i=2: it is outside the window,
    # and the observation deadline t=360 lies beyond the end t=120
    result = diagnose_mutation_recovery(
        samples, START, START + 2 * P, RATE, 1.0, 300
    )
    assert len(result.segments) == 1
    assert result.segments[0].recovered is False
    assert result.segments[0].unrecovered_reason == REASON_RANGE_END


def test_observation_deadline_inside_window_without_further_samples_is_expired():
    # deadline t=180 < end t=240, last in-window sample at t=120 off baseline
    samples = [
        at(START, 10),
        at(START + P, 40),
        at(START + 2 * P, 40),
    ]
    result = diagnose_mutation_recovery(
        samples, START, START + 4 * P, RATE, 1.0, 120
    )
    assert len(result.segments) == 1
    assert result.segments[0].unrecovered_reason == REASON_OBSERVATION_EXPIRED
    assert result.segments[0].end.ts == START + 2 * P


def test_equal_timestamps_last_wins():
    # two writes at t=60: 99 first, 40 last wins; jump 10 -> 40, back to 10
    samples = [
        at(START, 10),
        at(START + P, 99, eid=98),
        at(START + P, 40, eid=99),
        at(START + 2 * P, 10, eid=3),
    ]
    result = diagnose_mutation_recovery(samples, START, START + 3 * P, RATE, 1.0, 300)
    assert len(result.segments) == 1
    assert result.segments[0].jump.value == 40
    assert result.segments[0].jump.id == 99
    assert result.segments[0].recovered is True


def test_payload_counts_and_shape():
    samples = series([10, 30, 10])
    payload = diagnose_mutation_recovery(
        samples, START, START + 3 * P, RATE, 1.0, 300
    ).to_dict()
    assert payload["segment_count"] == 1
    assert payload["recovered_count"] == 1
    assert payload["unrecovered_count"] == 0
    seg = payload["segments"][0]
    assert set(seg) == {
        "baseline_sample", "start_sample", "end_sample", "jump_rate",
        "max_deviation", "max_deviation_sample", "recovered",
        "recovery_seconds", "unrecovered_reason",
    }
    assert seg["baseline_sample"] == {"event_id": 1, "ts": START, "value": 10}


def test_invalid_arguments_rejected():
    samples = series([1, 2])
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, END, START, RATE, 1.0, 300)  # inverted
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, 0, 1.0, 300)  # zero rate
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, -1, 1.0, 300)  # negative rate
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, RATE, -0.1, 300)  # neg tolerance
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, RATE, 1.0, 0)  # zero observation
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, RATE, 1.0, 300, ttl=0)
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, RATE, 1.0, 300, ttl=-5)
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, float("nan"), 1.0, 300)
    with pytest.raises(ValueError):
        diagnose_mutation_recovery(samples, START, END, RATE, 1.0, float("inf"))
