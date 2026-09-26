"""Redundant-channel consistency check (read-only).

Two channels measuring the same quantity (redundant sensors) are compared
over a half-open UTC query window ``[start, end)``:

* each channel's samples are expanded into **held intervals** exactly like
  the rule engine does: a sample ``(tᵢ, vᵢ)`` holds its value on
  ``[tᵢ, min(tᵢ₊₁, tᵢ + ttl, horizon))`` where ``ttl`` is the channel's
  optional per-channel sample validity period and ``horizon`` is the global
  data endpoint (the newest timestamp in the whole dataset).  The caller
  supplies the channel's samples inside the window **plus the last sample
  before the window start**, so a value still valid at ``start`` is honoured
  (an expired one simply clips away);
* the two channels are compared **only on the intersection of their valid
  stretches** — wherever either side has expired (a TTL gap) or has no
  sample yet, nothing is extrapolated and that time is not comparable;
* on the intersection both held values are piecewise constant, so the
  absolute difference is too.  Stretches with ``|vₐ − v_b|`` **strictly
  greater** than the threshold form raw inconsistent pieces; pieces that
  merely touch (``[a,b)`` followed by ``[b,c)``) merge into one segment;
* every merged segment reports its start/end, duration, the **maximum
  absolute difference** and the pair of samples (timestamp + value from each
  channel) that produced it; the result also totals the **comparable
  seconds** (intersection length) and the **inconsistent seconds**.

Everything here is a pure function of the supplied samples: nothing is read
from or written to any table, and rule evaluation is never triggered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_EPS = 1e-7  # seconds of slack for boundary comparisons (float timestamps)


@dataclass(frozen=True)
class HoldPiece:
    """One sample's held value on the half-open interval ``[start, end)``."""

    start: float
    end: float
    ts: float  # the sample's own timestamp (<= start after window clipping)
    value: float


@dataclass(frozen=True)
class SamplePair:
    """The two samples (one per channel) behind a compared stretch."""

    a_ts: float
    a_value: float
    b_ts: float
    b_value: float

    def to_dict(self) -> dict:
        return {
            "a": {"ts": self.a_ts, "value": self.a_value},
            "b": {"ts": self.b_ts, "value": self.b_value},
        }


@dataclass(frozen=True)
class InconsistentSegment:
    """One maximal half-open stretch where ``|vₐ − v_b| > threshold``."""

    start: float
    end: float
    max_abs_diff: float
    max_at: SamplePair

    @property
    def duration_seconds(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "duration_seconds": self.duration_seconds,
            "max_abs_diff": self.max_abs_diff,
            "max_at": self.max_at.to_dict(),
        }


@dataclass(frozen=True)
class ConsistencyResult:
    start: float
    end: float
    threshold: float
    horizon: float
    comparable_seconds: float
    inconsistent_seconds: float
    segments: list[InconsistentSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "threshold": self.threshold,
            "horizon": self.horizon,
            "comparable_seconds": self.comparable_seconds,
            "inconsistent_seconds": self.inconsistent_seconds,
            "inconsistent_ratio": (
                self.inconsistent_seconds / self.comparable_seconds
                if self.comparable_seconds > 0
                else 0.0
            ),
            "segment_count": len(self.segments),
            "segments": [seg.to_dict() for seg in self.segments],
        }


def hold_pieces(
    samples,
    horizon: float,
    ttl: float | None = None,
) -> list[HoldPiece]:
    """Expand samples into held pieces ``[ts, min(next ts, ts + ttl, horizon))``.

    ``samples`` is an iterable of ``(ts, value)`` pairs; equal timestamps keep
    the last supplied value (same rule as the rule engine).  A sample whose
    capped interval is empty (e.g. it sits exactly on the horizon) produces no
    piece.  ``ttl`` must be a positive number of seconds when given.
    """
    horizon = float(horizon)
    if not math.isfinite(horizon):
        raise ValueError("horizon must be a finite number")
    if ttl is not None:
        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("sample validity (ttl) must be a positive number of seconds")

    ordered: list[tuple[float, float]] = []
    for ts, value in sorted(((float(t), float(v)) for t, v in samples), key=lambda p: p[0]):
        if ordered and ordered[-1][0] == ts:
            ordered[-1] = (ts, value)  # last write at identical timestamp
        else:
            ordered.append((ts, value))

    pieces: list[HoldPiece] = []
    for i, (ts, value) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else horizon
        if ttl is not None:
            end = min(end, ts + ttl, horizon)
        if end > ts + _EPS:
            pieces.append(HoldPiece(start=ts, end=end, ts=ts, value=value))
    return pieces


def _clip(pieces: list[HoldPiece], start: float, end: float) -> list[HoldPiece]:
    """Clip held pieces to the half-open query window ``[start, end)``."""
    out: list[HoldPiece] = []
    for p in pieces:
        lo = p.start if p.start > start else start
        hi = p.end if p.end < end else end
        if hi > lo + _EPS:
            out.append(HoldPiece(start=lo, end=hi, ts=p.ts, value=p.value))
    return out


def check_consistency(
    samples_a,
    samples_b,
    horizon: float,
    start: float,
    end: float,
    threshold: float,
    ttl_a: float | None = None,
    ttl_b: float | None = None,
) -> ConsistencyResult:
    """Compare two redundant channels over the half-open window ``[start, end)``.

    ``samples_a`` / ``samples_b`` are iterables of ``(ts, value)`` covering
    the window plus the last sample before it; ``horizon`` is the global data
    endpoint and ``ttl_a`` / ``ttl_b`` the optional per-channel sample
    validity periods.  See the module docstring for the comparison rules.

    Raises ``ValueError`` for a non-finite/inverted window, a negative or
    non-finite threshold, or a non-positive TTL.
    """
    start = float(start)
    end = float(end)
    threshold = float(threshold)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("check end must be greater than start (half-open [start, end))")
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be a finite non-negative number")

    pieces_a = _clip(hold_pieces(samples_a, horizon, ttl_a), start, end)
    pieces_b = _clip(hold_pieces(samples_b, horizon, ttl_b), start, end)

    # Two-pointer sweep over the intersection of both valid stretches.  On
    # each overlap both held values are constant, so the absolute difference
    # is constant too; gaps on either side are simply never visited.
    comparable = 0.0
    raw: list[tuple[float, float, float, SamplePair]] = []
    i = j = 0
    while i < len(pieces_a) and j < len(pieces_b):
        pa, pb = pieces_a[i], pieces_b[j]
        lo = max(pa.start, pb.start)
        hi = min(pa.end, pb.end)
        if hi > lo + _EPS:
            diff = abs(pa.value - pb.value)
            comparable += hi - lo
            if diff > threshold:
                raw.append((lo, hi, diff, SamplePair(pa.ts, pa.value, pb.ts, pb.value)))
        if pa.end <= pb.end:
            i += 1
        else:
            j += 1

    # Merge touching pieces into maximal segments, keeping the largest
    # difference and the sample pair that first reached it.
    segments: list[InconsistentSegment] = []
    for lo, hi, diff, pair in raw:
        if segments and lo <= segments[-1].end + _EPS:
            last = segments[-1]
            if diff > last.max_abs_diff:
                segments[-1] = InconsistentSegment(last.start, hi, diff, pair)
            else:
                segments[-1] = InconsistentSegment(last.start, hi, last.max_abs_diff, last.max_at)
        else:
            segments.append(InconsistentSegment(lo, hi, diff, pair))

    inconsistent = sum(seg.duration_seconds for seg in segments)
    return ConsistencyResult(
        start=start,
        end=end,
        threshold=threshold,
        horizon=float(horizon),
        comparable_seconds=comparable,
        inconsistent_seconds=inconsistent,
        segments=segments,
    )
