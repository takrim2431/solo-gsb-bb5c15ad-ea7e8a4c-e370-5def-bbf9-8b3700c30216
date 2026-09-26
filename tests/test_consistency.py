"""Unit tests for the read-only redundant-channel consistency check.

Two channels' samples are expanded into held intervals (per-channel TTL and
the global data endpoint apply, and the last sample before the window start
counts while still valid), compared only where both sides hold a value —
expired gaps are never extrapolated.  Stretches with absolute difference
strictly greater than the threshold merge into inconsistent segments.
"""

import pytest

from app.consistency import check_consistency, hold_pieces

H = 1000.0  # global data endpoint (horizon)


def series(values, start=0.0, step=60.0):
    """(ts, value) pairs, one sample every ``step`` seconds."""
    return [(start + i * step, v) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# hold_pieces: sample -> held-interval expansion
# --------------------------------------------------------------------------- #


def test_hold_pieces_chain_to_next_sample_and_horizon():
    pieces = hold_pieces(series([1, 2, 3], step=60), horizon=200)
    assert [(p.start, p.end, p.ts, p.value) for p in pieces] == [
        (0, 60, 0, 1),
        (60, 120, 60, 2),
        (120, 200, 120, 3),  # last sample held to the horizon
    ]


def test_hold_pieces_ttl_truncates_and_leaves_gaps():
    pieces = hold_pieces(series([1, 2, 3], step=300), horizon=1000, ttl=120)
    assert [(p.start, p.end) for p in pieces] == [(0, 120), (300, 420), (600, 720)]


def test_hold_pieces_ttl_caps_last_sample_before_horizon():
    pieces = hold_pieces([(0, 1), (60, 2)], horizon=600, ttl=120)
    assert [(p.start, p.end) for p in pieces] == [(0, 60), (60, 180)]
    # horizon wins when it is earlier than ts + ttl
    pieces = hold_pieces([(0, 1), (60, 2)], horizon=100, ttl=500)
    assert [(p.start, p.end) for p in pieces] == [(0, 60), (60, 100)]


def test_hold_pieces_equal_timestamps_last_wins():
    pieces = hold_pieces([(0, 1), (0, 9), (60, 2)], horizon=100)
    assert [(p.ts, p.value) for p in pieces] == [(0, 9), (60, 2)]


def test_hold_pieces_sample_on_horizon_holds_nothing():
    assert hold_pieces([(0, 1), (100, 2)], horizon=100) == hold_pieces([(0, 1)], horizon=100)


def test_hold_pieces_invalid_ttl_rejected():
    with pytest.raises(ValueError):
        hold_pieces([(0, 1)], horizon=100, ttl=0)
    with pytest.raises(ValueError):
        hold_pieces([(0, 1)], horizon=100, ttl=-5)


# --------------------------------------------------------------------------- #
# check_consistency: windowed comparison
# --------------------------------------------------------------------------- #


def test_identical_channels_never_inconsistent():
    samples = series([1, 2, 3, 4], step=60)
    result = check_consistency(samples, samples, H, 0, 240, 0.0)
    assert result.comparable_seconds == pytest.approx(240)
    assert result.inconsistent_seconds == 0
    assert result.segments == []
    assert result.to_dict()["inconsistent_ratio"] == 0.0


def test_diff_strictly_greater_than_threshold_only():
    # b is exactly threshold away: consistent (strict comparison)
    a = series([10, 10], step=120)
    b = series([15, 14], step=120)
    result = check_consistency(a, b, H, 0, 240, 5.0)
    assert result.comparable_seconds == pytest.approx(240)
    assert result.segments == []
    # one tick above the threshold flips the second stretch
    result = check_consistency(a, b, H, 0, 240, 4.999999)
    assert [s.duration_seconds for s in result.segments] == [pytest.approx(120)]


def test_segment_reports_bounds_duration_max_and_sample_pair():
    a = series([0, 0, 0, 0], step=60)
    b = series([0, 5, 9, 0], step=60)
    result = check_consistency(a, b, H, 0, 240, 1.0)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert (seg.start, seg.end) == (60, 180)
    assert seg.duration_seconds == pytest.approx(120)
    assert seg.max_abs_diff == pytest.approx(9)
    assert seg.max_at.a_ts == 120 and seg.max_at.a_value == 0
    assert seg.max_at.b_ts == 120 and seg.max_at.b_value == 9
    assert result.inconsistent_seconds == pytest.approx(120)
    assert result.comparable_seconds == pytest.approx(240)


def test_touching_pieces_merge_into_one_segment():
    # b changes value mid-stretch but the diff stays above the threshold:
    # [60,120) and [120,180) touch and merge into a single [60,180) segment,
    # and the max comes from the second piece's samples.
    a = series([0, 0, 0], step=60)
    b = series([0, 5, 8], step=60)
    result = check_consistency(a, b, H, 0, 180, 1.0)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert (seg.start, seg.end) == (60, 180)
    assert seg.max_abs_diff == pytest.approx(8)
    assert seg.max_at.b_ts == 120 and seg.max_at.b_value == 8


def test_consistent_stretch_separates_segments():
    a = series([0, 0, 0, 0, 0], step=60)
    b = series([0, 5, 0, 5, 0], step=60)
    result = check_consistency(a, b, H, 0, 300, 1.0)
    assert [(s.start, s.end) for s in result.segments] == [(60, 120), (180, 240)]
    assert result.inconsistent_seconds == pytest.approx(120)


def test_only_both_valid_intersection_is_comparable():
    # a expires 30s into every 60s slot (TTL), b holds forever: the expired
    # half of each slot is not comparable and never extrapolated.
    a = series([0, 100, 0, 100], step=60)
    b = series([1, 1, 1, 1], step=60)
    result = check_consistency(a, b, H, 0, 240, 1.0, ttl_a=30)
    assert result.comparable_seconds == pytest.approx(120)  # 4 × 30s
    # |0-1| <= 1 consistent, |100-1| > 1 inconsistent, each 30s per slot
    assert [(s.start, s.end) for s in result.segments] == [(60, 90), (180, 210)]
    assert result.inconsistent_seconds == pytest.approx(60)


def test_gap_on_either_side_breaks_segments():
    # TTL gaps on both channels: pieces on either side of a gap do not merge.
    a = series([100, 100], step=120)
    b = series([0, 0], step=120)
    result = check_consistency(a, b, H, 0, 240, 1.0, ttl_a=60, ttl_b=60)
    assert result.comparable_seconds == pytest.approx(120)
    assert [(s.start, s.end) for s in result.segments] == [(0, 60), (120, 180)]


def test_last_sample_before_window_start_counts_while_valid():
    # no sample of b inside the window: the sample at ts=0 still holds there
    a = [(100, 10.0), (160, 10.0)]
    b = [(0, 4.0)]
    result = check_consistency(a, b, H, 100, 220, 1.0)
    assert result.comparable_seconds == pytest.approx(120)
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert (seg.start, seg.end) == (100, 220)
    assert seg.max_at.b_ts == 0 and seg.max_at.b_value == 4.0


def test_last_sample_before_window_start_expired_is_not_extrapolated():
    # b's last sample expired (TTL) before the window: nothing comparable
    a = [(100, 10.0), (160, 10.0)]
    b = [(0, 4.0)]
    result = check_consistency(a, b, H, 100, 220, 1.0, ttl_b=50)
    assert result.comparable_seconds == 0
    assert result.segments == []
    assert result.inconsistent_seconds == 0


def test_window_clips_held_intervals():
    a = series([0, 10, 0], step=60)
    b = series([0, 0, 0], step=60)
    # window cuts through the middle of the inconsistent stretch
    result = check_consistency(a, b, H, 30, 90, 1.0)
    assert result.comparable_seconds == pytest.approx(60)
    assert [(s.start, s.end) for s in result.segments] == [(60, 90)]
    assert result.inconsistent_seconds == pytest.approx(30)


def test_horizon_caps_comparison():
    # horizon lands mid-window: nothing is comparable past the data endpoint
    a = series([0, 10], step=60)
    b = series([0, 0], step=60)
    result = check_consistency(a, b, horizon=90, start=0, end=240, threshold=1.0)
    assert result.comparable_seconds == pytest.approx(90)
    assert [(s.start, s.end) for s in result.segments] == [(60, 90)]


def test_zero_threshold_flags_any_difference():
    a = series([1, 2], step=60)
    b = series([1, 2.0001], step=60)
    result = check_consistency(a, b, H, 0, 120, 0.0)
    assert [(s.start, s.end) for s in result.segments] == [(60, 120)]


def test_to_dict_payload_shape():
    a = series([0, 5], step=60)
    b = series([0, 0], step=60)
    payload = check_consistency(a, b, H, 0, 120, 1.0).to_dict()
    assert payload["start"] == 0 and payload["end"] == 120
    assert payload["threshold"] == 1.0
    assert payload["horizon"] == H
    assert payload["comparable_seconds"] == pytest.approx(120)
    assert payload["inconsistent_seconds"] == pytest.approx(60)
    assert payload["inconsistent_ratio"] == pytest.approx(0.5)
    assert payload["segment_count"] == 1
    seg = payload["segments"][0]
    assert seg["duration_seconds"] == pytest.approx(60)
    assert seg["max_at"] == {
        "a": {"ts": 60, "value": 5},
        "b": {"ts": 60, "value": 0},
    }


def test_invalid_arguments_rejected():
    with pytest.raises(ValueError):
        check_consistency([], [], H, 100, 100, 1.0)  # empty window
    with pytest.raises(ValueError):
        check_consistency([], [], H, 200, 100, 1.0)  # inverted window
    with pytest.raises(ValueError):
        check_consistency([], [], H, 0, 100, -1.0)  # negative threshold
    with pytest.raises(ValueError):
        check_consistency([], [], H, 0, 100, float("nan"))
    with pytest.raises(ValueError):
        check_consistency([], [], H, 0, 100, float("inf"))
