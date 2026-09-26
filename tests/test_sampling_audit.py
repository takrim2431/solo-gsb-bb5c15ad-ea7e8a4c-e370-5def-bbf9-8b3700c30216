"""Unit tests for the read-only sampling-cadence audit (app.audit).

The audit generates an expected beat grid anchored at the window start and
books each in-range event to its nearest expected instant only when the
deviation is within a sub-half-period tolerance; one instant counts at most
once, consecutive misses merge into missing-sample segments.
"""

import math

import pytest

from app.audit import audit_cadence, expected_grid

S, E, P = 0.0, 600.0, 60.0  # ten expected instants: 0,60,...,540
T = 5.0


def beat(k):
    return k * P


# --------------------------------------------------------------------------- #
# grid generation
# --------------------------------------------------------------------------- #


def test_grid_anchored_at_start_and_half_open():
    grid = expected_grid(S, E, P)
    assert grid == [0, 60, 120, 180, 240, 300, 360, 420, 480, 540]
    assert len(grid) == 10


def test_grid_point_exactly_on_end_is_excluded():
    # span == period: only the anchor belongs to [start, end)
    assert expected_grid(0, 60, 60) == [0]
    # one second beyond: the second point is inside
    assert expected_grid(0, 61, 60) == [0, 60]


def test_grid_span_shorter_than_period_still_includes_anchor():
    assert expected_grid(100, 130, 60) == [100]


def test_grid_invalid_arguments():
    with pytest.raises(ValueError):
        expected_grid(0, 0, 60)
    with pytest.raises(ValueError):
        expected_grid(10, 0, 60)
    with pytest.raises(ValueError):
        expected_grid(0, 100, 0)
    with pytest.raises(ValueError):
        expected_grid(0, 100, -1)
    with pytest.raises(ValueError, match="too many"):
        expected_grid(0, 10, 1e-9)


# --------------------------------------------------------------------------- #
# booking
# --------------------------------------------------------------------------- #


def test_perfect_series_full_hit_rate():
    result = audit_cadence(S, E, P, T, [beat(k) for k in range(10)])
    assert result.expected_count == 10
    assert result.hit_count == 10
    assert result.miss_count == 0
    assert result.hit_rate == 1.0
    assert result.missing_segments == []
    assert result.extra_events == []


def test_jitter_within_tolerance_books_nearest_instant():
    events = [beat(k) + (3 if k % 2 == 0 else -3) for k in range(10)]
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_count == 10
    assert result.missing_segments == []
    assert result.hit_slots == list(range(10))


def test_jitter_beyond_tolerance_is_missed_and_extra():
    # 120's sample lands 6s late (nearest is 120, out of a 5s tolerance).
    events = [beat(k) for k in range(10)]
    events[2] += 6
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_count == 9
    assert result.miss_count == 1
    assert result.hit_rate == pytest.approx(0.9)
    seg = result.missing_segments
    assert len(seg) == 1
    assert seg[0].start == seg[0].end == 120
    assert seg[0].miss_count == 1
    assert seg[0].expected_times == [120]
    assert result.extra_events == [{"ts": 126, "reason": "out_of_tolerance"}]


def test_two_events_for_one_instant_count_once_closest_wins():
    events = [beat(k) for k in range(10)] + [121, 123]
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_count == 10  # instant 120 still booked a single time
    assert result.miss_count == 0
    # 120 exact is closest; 121 and 123 are surplus duplicates for that slot
    assert sorted(x["ts"] for x in result.extra_events if x["reason"] == "duplicate") == [121, 123]


def test_duplicate_closest_event_takes_the_slot():
    # No exact sample: 123 is closer than 125, so 123 books instant 120 and
    # 125 becomes a duplicate surplus.
    events = [beat(k) for k in (0, 1, 3, 4, 5, 6, 7, 8, 9)] + [125, 123]
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_slots[2] == 2
    assert result.extra_events == [{"ts": 125, "reason": "duplicate"}]


def test_events_outside_window_are_ignored():
    events = [beat(k) for k in range(10)] + [-5, 600, 660]
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_count == 10
    # half-open window: an event exactly on `end` is not in range
    assert result.actual_count_in_range == 10
    assert result.extra_events == []


def test_consecutive_misses_merge_into_segments():
    # miss 120/180 (one segment) and 420 alone (second segment)
    events = [beat(k) for k in range(10) if k not in (2, 3, 7)]
    result = audit_cadence(S, E, P, T, events)
    assert result.hit_count == 7
    assert result.miss_count == 3
    assert len(result.missing_segments) == 2
    seg1, seg2 = result.missing_segments
    assert (seg1.start, seg1.end, seg1.miss_count, seg1.expected_times) == (
        120, 180, 2, [120, 180],
    )
    assert (seg2.start, seg2.end, seg2.miss_count, seg2.expected_times) == (
        420, 420, 1, [420],
    )
    assert result.hit_rate == pytest.approx(0.7)


def test_all_missed_is_one_segment():
    result = audit_cadence(S, E, P, T, [])
    assert result.hit_count == 0
    assert result.hit_rate == 0.0
    assert len(result.missing_segments) == 1
    seg = result.missing_segments[0]
    assert (seg.start, seg.end, seg.miss_count) == (0, 540, 10)


def test_tolerance_must_be_below_half_period():
    with pytest.raises(ValueError, match="half the period"):
        audit_cadence(S, E, 60, 30, [])
    with pytest.raises(ValueError, match="half the period"):
        audit_cadence(S, E, 60, 31, [])
    with pytest.raises(ValueError):
        audit_cadence(S, E, 60, -1, [])
    # exactly 0 is valid: only on-grid samples book
    result = audit_cadence(S, E, 60, 0, [0, 60])
    assert result.hit_count == 2


def test_event_at_half_period_distance_never_books():
    # Event at 150 is equidistant between 120 and 180; with any valid
    # tolerance (<30) it cannot book either.
    result = audit_cadence(S, E, P, 29.9, [150])
    assert result.hit_count == 0
    assert result.extra_events == [{"ts": 150, "reason": "out_of_tolerance"}]
    assert len(result.missing_segments) == 1


def test_zero_tolerance_requires_exact_grid():
    exact = audit_cadence(S, E, P, 0, [beat(k) for k in range(10)])
    assert exact.hit_count == 10
    shifted = audit_cadence(S, E, P, 0, [beat(k) + 1 for k in range(10)])
    assert shifted.hit_count == 0
    assert len(shifted.extra_events) == 10


def test_to_dict_payload_shape():
    result = audit_cadence(S, E, P, T, [beat(k) for k in range(9)])
    payload = result.to_dict()
    assert payload["expected_count"] == 10
    assert payload["hit_count"] == 9
    assert payload["miss_count"] == 1
    assert payload["hit_rate"] == pytest.approx(0.9)
    assert payload["missing_segments"][0]["span_seconds"] == 0
    assert math.isfinite(payload["period_seconds"])
