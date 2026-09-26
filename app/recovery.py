"""Single-channel abrupt-change recovery diagnostic (read-only).

Given one channel's samples over a half-open UTC query window
``[start, end)``, the diagnostic walks **adjacent valid samples** in time
order and flags abrupt changes:

* the change rate of a neighbouring pair is
  ``|vᵢ − vᵢ₋₁| / (tᵢ − tᵢ₋₁)`` — the **actual elapsed time** between the
  two samples, so irregular sampling is honoured (no assumed period);
* when the channel has a configured sample validity period (TTL), a pair
  whose previous sample has already expired by the time the next sample
  arrives (``tᵢ > tᵢ₋₁ + ttl``) is separated by an **invalid gap**: the
  gap cuts continuity, no rate is judged across it and no mutation may
  span it.  Without a TTL every neighbouring pair is contiguous (the
  sample holds until the next one, exactly like the rule engine);
* only a sample **inside** the window may open a mutation segment; the
  nearest sample before ``start`` that is still valid at the first
  in-window sample may serve as the **pre-mutation baseline**;
* when the rate is **strictly greater** than the jump-rate threshold the
  baseline (the previous sample of the pair) is recorded and the
  diagnostic looks forward for the **first** later sample, within
  ``observation_seconds`` of the jump, whose value is back inside the
  baseline tolerance (``|v − baseline| ≤ regression_tolerance``).  Such a
  sample marks the segment **recovered**; otherwise the segment stays
  **unrecovered**;
* samples already attributed to an open segment never open another one
  (another big jump while a segment is open only updates its maximum
  deviation).  Recovery and opening never happen on the same sample — a
  sample that returns to baseline closes its segment and is not reused.

Unrecovered segments carry one of three reasons:

* ``observation_expired`` — the observation duration elapsed (inside the
  window) without any sample returning to the baseline tolerance;
* ``validity_gap`` — a TTL expiry gap cut continuity before the
  observation duration completed;
* ``range_end`` — the window ended while the segment was still ongoing
  (neither the observation duration nor an in-range expiry gap reached).

Every segment reports its baseline/jump/end samples, the jump rate, the
maximum absolute deviation from the baseline (and the sample reaching
it), the recovery time (jump sample → recovery sample) and the
unrecovered reason.  Everything here is a pure function of the supplied
samples: nothing is read from or written to any table, and rule
evaluation is never triggered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_EPS = 1e-7  # seconds of slack for boundary comparisons (float timestamps)

REASON_OBSERVATION_EXPIRED = "observation_expired"
REASON_VALIDITY_GAP = "validity_gap"
REASON_RANGE_END = "range_end"


@dataclass(frozen=True)
class Sample:
    """One valid sample; ``id`` is the events row id (may be None)."""

    id: int | None
    ts: float
    value: float

    def to_dict(self) -> dict:
        return {"event_id": self.id, "ts": self.ts, "value": self.value}


@dataclass(frozen=True)
class MutationSegment:
    """One abrupt-change segment and its recovery outcome.

    ``baseline`` is the pre-jump sample, ``jump`` the sample whose rate
    from the baseline crossed the threshold (the segment start), ``end``
    the recovery sample when recovered or otherwise the last sample
    attributed before the segment was cut/expired/met the window end.
    """

    baseline: Sample
    jump: Sample
    end: Sample
    jump_rate: float
    max_deviation: float
    max_deviation_sample: Sample
    recovered: bool
    recovery_seconds: float | None
    unrecovered_reason: str | None

    def to_dict(self) -> dict:
        return {
            "baseline_sample": self.baseline.to_dict(),
            "start_sample": self.jump.to_dict(),
            "end_sample": self.end.to_dict(),
            "jump_rate": self.jump_rate,
            "max_deviation": self.max_deviation,
            "max_deviation_sample": self.max_deviation_sample.to_dict(),
            "recovered": self.recovered,
            "recovery_seconds": self.recovery_seconds,
            "unrecovered_reason": self.unrecovered_reason,
        }


@dataclass(frozen=True)
class RecoveryDiagnosis:
    start: float
    end: float
    rate_threshold: float
    regression_tolerance: float
    observation_seconds: float
    ttl: float | None
    segments: list[MutationSegment] = field(default_factory=list)

    def to_dict(self) -> dict:
        recovered = sum(1 for s in self.segments if s.recovered)
        return {
            "start": self.start,
            "end": self.end,
            "rate_threshold": self.rate_threshold,
            "regression_tolerance": self.regression_tolerance,
            "observation_seconds": self.observation_seconds,
            "ttl_seconds": self.ttl,
            "segment_count": len(self.segments),
            "recovered_count": recovered,
            "unrecovered_count": len(self.segments) - recovered,
            "segments": [s.to_dict() for s in self.segments],
        }


class _OpenSegment:
    """Mutable builder for a segment currently looking for recovery."""

    def __init__(self, baseline: Sample, jump: Sample, jump_rate: float):
        self.baseline = baseline
        self.jump = jump
        self.end = jump  # last attributed sample
        self.jump_rate = jump_rate
        self.max_deviation = abs(jump.value - baseline.value)
        self.max_deviation_sample = jump

    def observe(self, sample: Sample) -> None:
        self.end = sample
        deviation = abs(sample.value - self.baseline.value)
        if deviation > self.max_deviation:
            self.max_deviation = deviation
            self.max_deviation_sample = sample

    def back_to_baseline(self, sample: Sample, tolerance: float) -> bool:
        return abs(sample.value - self.baseline.value) <= tolerance + _EPS

    def build(self, *, recovered: bool, reason: str | None) -> MutationSegment:
        return MutationSegment(
            baseline=self.baseline,
            jump=self.jump,
            end=self.end,
            jump_rate=self.jump_rate,
            max_deviation=self.max_deviation,
            max_deviation_sample=self.max_deviation_sample,
            recovered=recovered,
            recovery_seconds=(self.end.ts - self.jump.ts) if recovered else None,
            unrecovered_reason=None if recovered else reason,
        )


def _normalize_samples(samples) -> list[Sample]:
    """Order ``(id, ts, value)`` triples by ts; equal ts keeps the last one."""
    ordered: list[Sample] = []
    triples = sorted(
        ((sid, float(ts), float(value)) for sid, ts, value in samples),
        key=lambda t: t[1],
    )
    for sid, ts, value in triples:
        if ordered and ordered[-1].ts == ts:
            ordered[-1] = Sample(sid, ts, value)  # last write wins
        else:
            ordered.append(Sample(sid, ts, value))
    return ordered


def diagnose_mutation_recovery(
    samples,
    start: float,
    end: float,
    rate_threshold: float,
    regression_tolerance: float,
    observation_seconds: float,
    ttl: float | None = None,
) -> RecoveryDiagnosis:
    """Scan one channel for abrupt changes and their recovery.

    ``samples`` is an iterable of ``(event_id, ts, value)`` triples
    covering the window plus the last sample before its start (an expired
    one simply fails continuity and cannot be a baseline).  See the
    module docstring for the scan rules.

    Raises ``ValueError`` for a non-finite/inverted window, a
    non-positive rate threshold / observation duration, a negative
    regression tolerance, or a non-positive TTL.
    """
    start = float(start)
    end = float(end)
    rate_threshold = float(rate_threshold)
    regression_tolerance = float(regression_tolerance)
    observation_seconds = float(observation_seconds)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("diagnostic end must be greater than start (half-open [start, end))")
    if not math.isfinite(rate_threshold) or rate_threshold <= 0:
        raise ValueError("rate_threshold must be a positive finite number of units per second")
    if not math.isfinite(regression_tolerance) or regression_tolerance < 0:
        raise ValueError("regression_tolerance must be a finite non-negative number")
    if not math.isfinite(observation_seconds) or observation_seconds <= 0:
        raise ValueError("observation_seconds must be a positive finite number of seconds")
    if ttl is not None:
        ttl = float(ttl)
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("sample validity (ttl) must be a positive number of seconds")

    ordered = _normalize_samples(samples)
    segments: list[MutationSegment] = []
    open_seg: _OpenSegment | None = None

    k = 1
    while k < len(ordered):
        prev, cur = ordered[k - 1], ordered[k]
        in_window = start <= cur.ts < end
        gap = ttl is not None and cur.ts > prev.ts + ttl + _EPS
        # A gap only cuts the segment when validity actually expires inside
        # the query window; validity merely reaching the window end is the
        # normal range-end cut handled at finalize time.
        gap_in_window = gap and prev.ts + ttl < end - _EPS

        if open_seg is not None:
            deadline = open_seg.jump.ts + observation_seconds
            if gap_in_window:
                # The invalid gap cuts continuity: no rate and no recovery is
                # judged across it.  Whichever boundary came first gives the
                # reason; ``cur`` lies beyond the gap and never reattaches.
                gap_start = prev.ts + ttl
                reason = (
                    REASON_VALIDITY_GAP
                    if gap_start <= deadline + _EPS
                    else REASON_OBSERVATION_EXPIRED
                )
                segments.append(open_seg.build(recovered=False, reason=reason))
                open_seg = None
                k += 1
                continue
            if in_window and cur.ts > deadline + _EPS and deadline < end - _EPS:
                # First in-window sample strictly after the observation
                # deadline (which itself elapsed strictly inside the window):
                # the previous sample was the final recovery candidate, so
                # this segment expires un-recovered.  ``cur`` is not
                # attributed to it — reprocess the pair with no open segment
                # so a fresh abrupt change here may open its own segment.
                segments.append(
                    open_seg.build(recovered=False, reason=REASON_OBSERVATION_EXPIRED)
                )
                open_seg = None
                continue  # reprocess the same pair
            if in_window:
                open_seg.observe(cur)
                if cur.ts <= deadline + _EPS and open_seg.back_to_baseline(
                    cur, regression_tolerance
                ):
                    # First sample back inside the tolerance within the
                    # observation duration: recovered.  This sample is
                    # attributed to the segment and cannot open a new one.
                    segments.append(open_seg.build(recovered=True, reason=None))
                    open_seg = None
                    k += 1
                    continue

        # No open segment: an in-window, contiguous sample may open one.
        # Only the jumping sample itself has to lie in the window — its
        # baseline may be the still-valid last sample before the window.
        # Samples already attributed to a segment never reach here as a
        # jumping sample (a recovered/gap/expired segment closes first); an
        # attributed sample may still serve as the baseline.
        if open_seg is None and in_window and not gap:
            dt = cur.ts - prev.ts
            if dt > _EPS:
                rate = abs(cur.value - prev.value) / dt
                if rate > rate_threshold:
                    open_seg = _OpenSegment(prev, cur, rate)
        k += 1

    if open_seg is not None:
        deadline = open_seg.jump.ts + observation_seconds
        last = open_seg.end
        gap_start = last.ts + ttl if ttl is not None else float("inf")
        if deadline > end + _EPS and gap_start >= end - _EPS:
            # Both the observation deadline and the next expiry gap lie at or
            # beyond the window end: the window cut ends the segment.
            reason = REASON_RANGE_END
        elif gap_start < end - _EPS and gap_start <= deadline + _EPS:
            # The last attributed sample's validity expired inside the window
            # (a following sample lies across a gap the loop did not reach as
            # an in-window pair) before the observation duration completed.
            reason = REASON_VALIDITY_GAP
        elif deadline < end - _EPS:
            # The deadline elapsed strictly inside the window but no further
            # sample witnessed it (e.g. the last in-window sample was the
            # final recovery candidate).
            reason = REASON_OBSERVATION_EXPIRED
        else:
            # Deadline exactly at/after the window end with no TTL gap: from
            # the half-open window's perspective the segment reaches its end
            # still waiting, i.e. cut by the range boundary.
            reason = REASON_RANGE_END
        segments.append(open_seg.build(recovered=False, reason=reason))

    return RecoveryDiagnosis(
        start=start,
        end=end,
        rate_threshold=rate_threshold,
        regression_tolerance=regression_tolerance,
        observation_seconds=observation_seconds,
        ttl=ttl,
        segments=segments,
    )
