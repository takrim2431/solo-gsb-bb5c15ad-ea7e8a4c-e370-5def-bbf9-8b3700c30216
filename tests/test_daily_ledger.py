"""Unit tests for the per-day fault ledger of a rule combination.

The ledger merges the selected rules' stored fault intervals as half-open
intervals (overlap/touch dedup), then splits them at UTC midnights inside
the query window and accounts fault seconds / new / continuing segments /
coverage per UTC day.
"""

import pytest

from app.engine import Interval, daily_fault_ledger, to_epoch, utc_midnight

DAY = 86400.0
# 2026-09-23T00:00:00Z — a UTC midnight used as the day base in all tests.
BASE = to_epoch("2026-09-23T00:00:00Z")


def day(n):
    return BASE + n * DAY


def test_utc_midnight_floors_to_day_start():
    assert utc_midnight(BASE + 5 * 3600 + 30) == BASE
    assert utc_midnight(BASE) == BASE
    assert utc_midnight(BASE - 1) == BASE - DAY


def test_single_day_single_segment():
    days = daily_fault_ledger([Interval(day(0) + 3600, day(0) + 7200)], day(0), day(1))
    assert len(days) == 1
    d = days[0]
    assert d["date"] == "2026-09-23"
    assert d["fault_seconds"] == 3600.0
    assert d["new_segments"] == 1
    assert d["continuing_segments"] == 0
    assert d["queried_seconds"] == DAY
    assert d["coverage"] == pytest.approx(3600.0 / DAY)


def test_overlapping_faults_across_rules_counted_once():
    # Two rules fault over the same stretch: merged before accounting.
    days = daily_fault_ledger(
        [Interval(day(0) + 100, day(0) + 400), Interval(day(0) + 300, day(0) + 500)],
        day(0),
        day(1),
    )
    assert days[0]["fault_seconds"] == 400.0
    assert days[0]["new_segments"] == 1


def test_touching_segments_merge_into_one_new_segment():
    days = daily_fault_ledger(
        [Interval(day(0), day(0) + 100), Interval(day(0) + 100, day(0) + 250)],
        day(0),
        day(1),
    )
    assert days[0]["fault_seconds"] == 250.0
    assert days[0]["new_segments"] == 1
    assert days[0]["continuing_segments"] == 0


def test_cross_day_segment_counts_new_on_start_day_and_continuing_after():
    # Noon day0 -> noon day1 (36h), query covers both whole days.
    days = daily_fault_ledger(
        [Interval(day(0) + DAY / 2, day(1) + DAY / 2)], day(0), day(2)
    )
    assert [d["date"] for d in days] == ["2026-09-23", "2026-09-24"]
    assert days[0]["fault_seconds"] == DAY / 2
    assert (days[0]["new_segments"], days[0]["continuing_segments"]) == (1, 0)
    assert days[1]["fault_seconds"] == DAY / 2
    assert (days[1]["new_segments"], days[1]["continuing_segments"]) == (0, 1)
    assert days[0]["coverage"] == pytest.approx(0.5)
    assert days[1]["coverage"] == pytest.approx(0.5)


def test_segment_ending_exactly_at_midnight_does_not_enter_next_day():
    days = daily_fault_ledger([Interval(day(0) + 3600, day(1))], day(0), day(2))
    assert days[0]["fault_seconds"] == DAY - 3600
    assert days[1]["fault_seconds"] == 0.0
    assert (days[1]["new_segments"], days[1]["continuing_segments"]) == (0, 0)
    assert days[1]["coverage"] == 0.0


def test_segment_starting_exactly_at_midnight_is_new_on_that_day():
    days = daily_fault_ledger([Interval(day(1), day(1) + 100)], day(0), day(2))
    assert (days[0]["new_segments"], days[0]["continuing_segments"]) == (0, 0)
    assert (days[1]["new_segments"], days[1]["continuing_segments"]) == (1, 0)
    assert days[1]["fault_seconds"] == 100.0


def test_partial_first_and_last_day_use_actual_queried_seconds():
    # Query from noon day0 to 06:00 day1; one segment covers the whole window.
    start = day(0) + DAY / 2
    end = day(1) + 6 * 3600
    days = daily_fault_ledger([Interval(start, end)], start, end)
    assert days[0]["queried_seconds"] == DAY / 2
    assert days[0]["fault_seconds"] == DAY / 2
    assert days[0]["coverage"] == pytest.approx(1.0)
    assert days[1]["queried_seconds"] == 6 * 3600
    assert days[1]["fault_seconds"] == 6 * 3600
    assert days[1]["coverage"] == pytest.approx(1.0)
    assert (days[0]["new_segments"], days[1]["continuing_segments"]) == (1, 1)


def test_segment_clipped_by_query_start_on_same_day_is_still_new():
    # Segment starts earlier the same calendar day but before the query bound.
    start = day(0) + 12 * 3600
    days = daily_fault_ledger([Interval(day(0) + 3600, day(0) + 50000)], start, day(1))
    assert days[0]["fault_seconds"] == 50000 - 12 * 3600
    assert (days[0]["new_segments"], days[0]["continuing_segments"]) == (1, 0)


def test_segment_started_before_query_window_counts_as_continuing():
    # Segment began on the previous UTC day; query starts mid-day0.
    start = day(0) + DAY / 2
    days = daily_fault_ledger([Interval(day(-1) + 100, day(1) + 100)], start, day(2))
    assert days[0]["fault_seconds"] == DAY / 2
    assert (days[0]["new_segments"], days[0]["continuing_segments"]) == (0, 1)
    assert days[1]["fault_seconds"] == 100.0
    assert (days[1]["new_segments"], days[1]["continuing_segments"]) == (0, 1)


def test_days_without_faults_are_listed_with_zeros():
    days = daily_fault_ledger([Interval(day(1) + 60, day(1) + 120)], day(0), day(3))
    assert [d["date"] for d in days] == ["2026-09-23", "2026-09-24", "2026-09-25"]
    assert days[0]["fault_seconds"] == 0.0 and days[0]["coverage"] == 0.0
    assert days[1]["fault_seconds"] == 60.0
    assert days[2]["fault_seconds"] == 0.0
    assert sum(d["new_segments"] for d in days) == 1


def test_empty_intervals_yield_zero_rows_per_day():
    days = daily_fault_ledger([], day(0), day(2))
    assert len(days) == 2
    for d in days:
        assert d["fault_seconds"] == 0.0
        assert d["new_segments"] == 0
        assert d["continuing_segments"] == 0
        assert d["coverage"] == 0.0
        assert d["queried_seconds"] == DAY


def test_invalid_window_rejected():
    with pytest.raises(ValueError):
        daily_fault_ledger([], day(1), day(0))
    with pytest.raises(ValueError):
        daily_fault_ledger([], day(0), day(0))
