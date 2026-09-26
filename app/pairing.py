"""Rule fault sequence pairing (read-only).

Given the **already stored** confirmed fault intervals of two different rules —
a *cause* (前因) rule and an *effect* (后续) rule — this module pairs each cause
fault with a later effect fault:

* only faults whose **start instant** lies inside the half-open UTC query
  window ``[start, end)`` take part (the filtering itself is done by the
  caller when reading the stored faults);
* a pair ``(cause, effect)`` is allowed when the effect's start is **strictly
  later** than the cause's start and the gap does not exceed the positive
  ``max_lag_seconds``:  ``cause.start < effect.start <= cause.start + max_lag``;
* every fault interval participates in **at most one pair**;
* the pairing is **order-preserving** (non-crossing): when cause faults
  ``c1`` (earlier) and ``c2`` (later) are both paired, their effect partners
  also appear in time order — the pairing never swaps the temporal order of
  the two sequences;
* among all order-preserving one-to-one pairings the chosen one first
  **maximizes the number of pairs**, then **minimizes the total lag**
  (sum of ``effect.start - cause.start`` over the pairs);
* remaining ties are broken **by fault id**: both sequences are processed in
  ``(start, id)`` order and the reconstruction greedily pairs the earliest
  still-unmatched cause with the earliest still-unmatched effect that keeps
  an optimal completion, so the result is deterministic and stable.

The optimum is computed with a sequence-alignment dynamic program over the
two start-ordered fault lists; ``dp[i][j]`` holds the best
``(pair_count, -total_lag)`` achievable over the suffixes ``cause[i:],
effect[j:]``, which the lexicographic tuple comparison optimizes exactly in
the required order (count first, then minimal lag).

Everything here is a pure function of the supplied fault outlines: nothing is
read from or written to any table, and rule evaluation is never triggered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

_EPS = 1e-7  # seconds of slack for the lag boundary (float timestamps)


@dataclass(frozen=True)
class PairableFault:
    """A stored fault interval taking part in sequence pairing."""

    id: int
    start: float
    end: float

    def to_dict(self) -> dict:
        return {"fault_id": self.id, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class FaultPair:
    """One matched cause→effect pair, anchored at the two fault starts."""

    cause_id: int
    effect_id: int
    cause_start: float
    effect_start: float
    lag_seconds: float

    def to_dict(self) -> dict:
        return {
            "cause_fault_id": self.cause_id,
            "effect_fault_id": self.effect_id,
            "cause_start": self.cause_start,
            "effect_start": self.effect_start,
            "lag_seconds": self.lag_seconds,
        }


@dataclass(frozen=True)
class PairingResult:
    """Outcome of the sequence pairing over the in-range faults."""

    max_lag_seconds: float
    cause_count: int  # cause faults whose start lies inside the window
    effect_count: int  # effect faults whose start lies inside the window
    pairs: list[FaultPair] = field(default_factory=list)
    unmatched_cause: list[PairableFault] = field(default_factory=list)
    unmatched_effect: list[PairableFault] = field(default_factory=list)

    @property
    def pair_count(self) -> int:
        return len(self.pairs)

    @property
    def total_lag_seconds(self) -> float:
        return sum(p.lag_seconds for p in self.pairs)

    @property
    def cause_pair_rate(self) -> float:
        """Paired share of the in-range cause faults (0 when there are none)."""
        return self.pair_count / self.cause_count if self.cause_count else 0.0

    @property
    def effect_pair_rate(self) -> float:
        """Paired share of the in-range effect faults (0 when there are none)."""
        return self.pair_count / self.effect_count if self.effect_count else 0.0

    def to_dict(self) -> dict:
        return {
            "max_lag_seconds": self.max_lag_seconds,
            "cause_faults_in_range": self.cause_count,
            "effect_faults_in_range": self.effect_count,
            "pair_count": self.pair_count,
            "total_lag_seconds": self.total_lag_seconds,
            "cause_pair_rate": self.cause_pair_rate,
            "effect_pair_rate": self.effect_pair_rate,
            "pairs": [p.to_dict() for p in self.pairs],
            "unmatched_cause": [f.to_dict() for f in self.unmatched_cause],
            "unmatched_effect": [f.to_dict() for f in self.unmatched_effect],
        }


def pair_faults(
    cause_faults: list[PairableFault],
    effect_faults: list[PairableFault],
    max_lag_seconds: float,
) -> PairingResult:
    """Pair cause faults with later effect faults (see module docstring).

    ``cause_faults`` / ``effect_faults`` are the stored fault outlines of the
    two rules whose starts lie inside the query window.  Raises ``ValueError``
    when ``max_lag_seconds`` is not a positive finite number.
    """
    max_lag = float(max_lag_seconds)
    if not math.isfinite(max_lag) or max_lag <= 0:
        raise ValueError("max_lag_seconds must be a positive finite number of seconds")

    # (start, id) order: starts are unique within one rule's stored faults, so
    # the id only breaks ties — which is exactly the stable tie-break rule.
    causes = sorted(cause_faults, key=lambda f: (f.start, f.id))
    effects = sorted(effect_faults, key=lambda f: (f.start, f.id))
    n, m = len(causes), len(effects)

    def lag(i: int, j: int) -> float | None:
        """Lag of the candidate pair, or None when the pair is not allowed."""
        d = effects[j].start - causes[i].start
        if d > 0 and d <= max_lag + _EPS:
            return d
        return None

    # dp[i][j] = (pair_count, -total_lag) optimum over causes[i:], effects[j:].
    dp = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = dp[i + 1][j]  # leave cause i unmatched
            skip_effect = dp[i][j + 1]  # leave effect j unmatched
            if skip_effect > best:
                best = skip_effect
            d = lag(i, j)
            if d is not None:
                pair = (dp[i + 1][j + 1][0] + 1, dp[i + 1][j + 1][1] - d)
                if pair > best:
                    best = pair
            dp[i][j] = best

    # Forward reconstruction: pair the earliest unmatched cause with the
    # earliest unmatched effect whenever that still allows an optimal
    # completion — deterministic, and ties resolve towards smaller fault ids
    # because both sequences are scanned in (start, id) order.
    pairs: list[FaultPair] = []
    used_cause = [False] * n
    used_effect = [False] * m
    i = j = 0
    while i < n and j < m:
        d = lag(i, j)
        if d is not None and dp[i][j] == (dp[i + 1][j + 1][0] + 1, dp[i + 1][j + 1][1] - d):
            pairs.append(
                FaultPair(
                    cause_id=causes[i].id,
                    effect_id=effects[j].id,
                    cause_start=causes[i].start,
                    effect_start=effects[j].start,
                    lag_seconds=d,
                )
            )
            used_cause[i] = used_effect[j] = True
            i += 1
            j += 1
        elif dp[i][j] == dp[i + 1][j]:
            i += 1  # cause i stays unmatched
        else:
            j += 1  # effect j stays unmatched

    return PairingResult(
        max_lag_seconds=max_lag,
        cause_count=n,
        effect_count=m,
        pairs=pairs,
        unmatched_cause=[c for k, c in enumerate(causes) if not used_cause[k]],
        unmatched_effect=[e for k, e in enumerate(effects) if not used_effect[k]],
    )
