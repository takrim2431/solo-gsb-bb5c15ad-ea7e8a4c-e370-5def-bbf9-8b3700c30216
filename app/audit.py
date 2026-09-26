"""Sampling-cadence audit (read-only beat-vs-event reconciliation).

Given a channel that is expected to sample every ``period`` seconds, the audit
lays a grid of **expected sample instants** over a half-open UTC query window
``[start, end)``, anchored at the window start::

    expected[k] = start + k * period,  expected[k] < end

Actual events of the channel are then reconciled against that grid:

* only events **inside** the window (``start <= ts < end``) participate;
* an event counts for an expected instant only when it is that instant's
  nearest grid point **and** the absolute deviation is within ``tolerance``;
* because ``tolerance`` is required to be *strictly less than half a period*,
  the tolerance windows of two adjacent expected instants never overlap, so
  every in-tolerance event has exactly one candidate instant;
* when several events fall inside one instant's tolerance window only the
  closest one is booked (**each expected instant counts at most once**); the
  others are reported as surplus ``duplicate`` events;
* events whose nearest expected instant is further away than ``tolerance`` are
  not booked either (``out_of_tolerance``).

Expected instants left without a booked event are **misses**; consecutive
missed instants (two misses are consecutive when their grid indices differ by
exactly one) are merged into **missing-sample segments**, each reporting the
first/last missed expected instant and the number of missed points.  The
overall hit rate is booked instants / expected instants.

Everything here is a pure function of the supplied event timestamps: the audit
never inserts, updates or deletes events, faults or anything else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_EPS = 1e-7
# Safety valve for absurdly small periods (e.g. period=1e-9 over a long window)
# so the generated grid stays bounded.
MAX_EXPECTED_POINTS = 1_000_000


@dataclass(frozen=True)
class MissSegment:
    """One maximal run of consecutive unbooked expected instants.

    ``start``/``end`` are the expected instants of the first and last missed
    points (equal for a single-point run); ``expected_times`` lists every
    missed instant in the run, in order.
    """

    start: float
    end: float
    miss_count: int
    expected_times: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "miss_count": self.miss_count,
            "span_seconds": self.end - self.start,
            "expected_times": list(self.expected_times),
        }


@dataclass(frozen=True)
class CadenceAudit:
    start: float
    end: float
    period_seconds: float
    tolerance_seconds: float
    expected_times: list[float]
    hit_slots: list[int]
    actual_count_in_range: int
    extra_events: list[dict]
    missing_segments: list[MissSegment]

    @property
    def expected_count(self) -> int:
        return len(self.expected_times)

    @property
    def hit_count(self) -> int:
        return len(self.hit_slots)

    @property
    def miss_count(self) -> int:
        return self.expected_count - self.hit_count

    @property
    def hit_rate(self) -> float:
        return self.hit_count / self.expected_count if self.expected_times else 0.0

    def to_dict(self) -> dict:
        return {
            "anchor": self.start,
            "start": self.start,
            "end": self.end,
            "period_seconds": self.period_seconds,
            "tolerance_seconds": self.tolerance_seconds,
            "expected_count": self.expected_count,
            "hit_count": self.hit_count,
            "miss_count": self.miss_count,
            "hit_rate": self.hit_rate,
            "actual_count_in_range": self.actual_count_in_range,
            "matched_event_count": self.hit_count,
            "extra_event_count": len(self.extra_events),
            "extra_events": list(self.extra_events),
            "missing_segments": [seg.to_dict() for seg in self.missing_segments],
        }


def expected_grid(start: float, end: float, period: float) -> list[float]:
    """Expected instants ``start + k*period`` inside half-open ``[start, end)``.

    The grid is anchored at ``start``; a grid point landing exactly on ``end``
    is excluded (the window is half-open).  ``period`` must be a finite,
    positive number of seconds; the result is capped at
    :data:`MAX_EXPECTED_POINTS`.
    """
    start = float(start)
    end = float(end)
    period = float(period)
    for name, value in (("start", start), ("end", end), ("period", period)):
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
    if end <= start:
        raise ValueError("audit end must be greater than start (half-open [start, end))")
    if period <= 0:
        raise ValueError("period_seconds must be a positive number of seconds")
    # Points k with start + k*period < end.  Subtract epsilon before flooring
    # so a point landing exactly on `end` (span an integer multiple of period)
    # is excluded despite float rounding.
    count = math.floor((end - start) / period - _EPS) + 1
    if count < 1:
        count = 1
    if count > MAX_EXPECTED_POINTS:
        raise ValueError(
            f"too many expected points ({count}); widen the period or narrow the range"
        )
    return [start + k * period for k in range(count)]


def audit_cadence(
    start: float,
    end: float,
    period_seconds: float,
    tolerance_seconds: float,
    actual_timestamps,
) -> CadenceAudit:
    """Reconcile actual event timestamps against the expected beat grid.

    See module docstring for the booking rules.  ``actual_timestamps`` is an
    iterable of epoch-second floats; duplicates are tolerated (all but the
    closest event per slot become ``duplicate`` extras).  Events outside the
    half-open window are ignored entirely.

    Raises ``ValueError`` for a non-finite/empty/inverted window, a
    non-positive period, or a negative tolerance that is not strictly smaller
    than half the period.
    """
    period = float(period_seconds)
    tolerance = float(tolerance_seconds)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance_seconds must be a finite non-negative number of seconds")
    if math.isfinite(period) and period > 0 and tolerance >= period / 2.0:
        raise ValueError("tolerance_seconds must be strictly less than half the period")

    grid = expected_grid(start, end, period)
    start = float(start)
    end = float(end)
    n = len(grid)

    in_range = sorted(float(ts) for ts in actual_timestamps if start <= float(ts) < end)

    # slot -> (abs deviation, timestamp) of the event currently booked.
    booked: dict[int, tuple[float, float]] = {}
    extra: list[dict] = []
    for ts in in_range:
        k = int(math.floor((ts - start) / period + 0.5))  # nearest grid index
        if 0 <= k < n and abs(ts - grid[k]) <= tolerance + _EPS:
            candidate = (abs(ts - grid[k]), ts)
            current = booked.get(k)
            if current is None or candidate < current:
                if current is not None:
                    # A closer event takes the slot; the previous holder is a
                    # surplus duplicate and no expected instant is double booked.
                    extra.append({"ts": current[1], "reason": "duplicate"})
                booked[k] = candidate
            else:
                extra.append({"ts": ts, "reason": "duplicate"})
        else:
            extra.append({"ts": ts, "reason": "out_of_tolerance"})
    extra.sort(key=lambda e: (e["ts"], e["reason"]))

    hit_slots = sorted(booked)
    segments: list[MissSegment] = []
    run: list[int] = []

    def flush() -> None:
        if not run:
            return
        times = [grid[k] for k in run]
        segments.append(
            MissSegment(
                start=times[0],
                end=times[-1],
                miss_count=len(times),
                expected_times=times,
            )
        )

    for k in range(n):
        if k in booked:
            flush()
            run = []
        else:
            run.append(k)
    flush()

    return CadenceAudit(
        start=start,
        end=end,
        period_seconds=period,
        tolerance_seconds=tolerance,
        expected_times=grid,
        hit_slots=hit_slots,
        actual_count_in_range=len(in_range),
        extra_events=extra,
        missing_segments=segments,
    )
