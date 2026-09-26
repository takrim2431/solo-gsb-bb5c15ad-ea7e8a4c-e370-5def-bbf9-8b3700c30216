"""Half-open interval / sweep-line rule engine.

Each sensor channel is a sequence of timestamped samples.  A sample's value is
held from its own timestamp up to (but excluding) the timestamp of the next
sample on the same channel — i.e. every piece of the derived truth model is a
half-open interval ``[start, end)``.

A comparison condition (``>``/``>=``/``<``/``<=`` only) may additionally carry
an optional **reset threshold** that turns the predicate into a latching one:
the condition first becomes true only when a held value satisfies the original
comparison, then stays true while the value merely loiters between the trigger
and reset boundaries, and disarms only when a held value reaches the reset
boundary (high trigger: ``value <= reset``; low trigger: ``value >= reset``).
An invalid TTL gap clears the latched state as well, so the next valid sample
must re-cross the original trigger before the condition can hold again.

A channel may additionally carry a positive **sample validity period (TTL)**
configured per ``(device, metric)``.  With a TTL of ``s`` seconds the value of
a sample taken at ``tᵢ`` is held only on
``[tᵢ, min(tᵢ₊₁, tᵢ + s, horizon))``; the open stretch after that is an
invalid gap during which the channel satisfies no condition.  Channels
without a TTL keep the legacy hold-to-next-sample / hold-to-horizon semantics.

Touching half-open intervals (``[a, b)`` followed by ``[b, c)``) are
consecutive: a sweep-line that emits a segment only when the truth value
actually changes merges them automatically, never emitting a zero-length
"gap" or "reset" segment at ``b``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

OPERATORS = {">", ">=", "<", "<=", "==", "!="}
# Only the four ordering comparisons support an optional reset threshold;
# equality predicates have no meaningful "below/above the trigger" side.
RESET_OPS = {">", ">=", "<", "<="}
HIGH_TRIGGER_OPS = {">", ">="}
LOGIC = {"AND", "OR"}
_EPS = 1e-7  # seconds of slack for duration comparisons (float boundaries)


def _normalize_epoch_number(n: float) -> float:
    # Heuristic for epoch milliseconds: numeric values past ~year 10000 in
    # seconds are treated as milliseconds (applied to numbers and numeric
    # strings so that JSONL payloads and query params behave identically).
    return n / 1000.0 if n > 1e11 else n


def to_epoch(value: Any) -> float:
    """Convert an epoch number (s/ms) / ISO-8601 string to epoch seconds (float)."""
    if value is None:
        raise ValueError("timestamp is null")
    if isinstance(value, bool):
        raise ValueError(f"unsupported timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return _normalize_epoch_number(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty timestamp")
        # Numeric strings are epoch seconds/milliseconds, same as JSON numbers.
        try:
            return _normalize_epoch_number(float(text))
        except ValueError:
            pass
        normalized = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            raise ValueError(f"unparsable timestamp: {value!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unsupported timestamp type: {type(value).__name__}")


def to_number(value: Any) -> float | None:
    """Best-effort numeric conversion; returns None when not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def compare(value: float, op: str, threshold: float) -> bool:
    if op == ">":
        return value > threshold
    if op == ">=":
        return value >= threshold
    if op == "<":
        return value < threshold
    if op == "<=":
        return value <= threshold
    if op == "==":
        return value == threshold
    if op == "!=":
        return value != threshold
    raise ValueError(f"unknown operator: {op}")


def validate_reset_threshold(op: str, threshold: float, reset: float) -> None:
    """Validate an optional latching reset threshold against the trigger side.

    A reset threshold is only meaningful for the ordering operators
    ``>``/``>=`` (a high-value trigger) and ``<``/``<=`` (a low-value
    trigger); it must sit strictly on the release side of the trigger
    threshold (below it for high triggers, above it for low triggers).
    """
    if op not in RESET_OPS:
        raise ValueError("reset threshold is only supported for >, >=, <, <=")
    if op in HIGH_TRIGGER_OPS:
        if reset >= threshold:
            raise ValueError("reset threshold must be lower than the trigger threshold")
    elif reset <= threshold:
        raise ValueError("reset threshold must be higher than the trigger threshold")


def at_reset_boundary(value: float, op: str, reset: float) -> bool:
    """Whether a held value reaches (and thus disarms) the reset boundary.

    High-value triggers (``>``/``>=``) disarm on ``value <= reset``;
    low-value triggers (``<``/``<=``) disarm on ``value >= reset``.
    """
    if op in HIGH_TRIGGER_OPS:
        return value <= reset
    return value >= reset


@dataclass(frozen=True)
class Interval:
    """Half-open interval [start, end)."""

    start: float
    end: float

    def nonempty(self) -> bool:
        return self.end > self.start + _EPS


def union(intervals: Iterable[Interval]) -> list[Interval]:
    """Merge overlapping or *touching* intervals.

    ``[a,b)`` and ``[b,c)`` touch and are treated as one ``[a,c)``.
    """
    ordered = sorted(
        (iv for iv in intervals if iv.end > iv.start), key=lambda iv: iv.start
    )
    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start <= merged[-1].end:
            if iv.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, iv.end)
        else:
            merged.append(iv)
    return merged


def cluster_intervals(intervals: Iterable[Interval]) -> list[list[int]]:
    """Cluster interval indices by overlap-or-touch, *transitively*.

    Half-open intervals that overlap (``[a,b)`` vs ``[a',b')`` with
    ``a' < b``) or merely touch (``[a,b)`` followed by ``[b,c)``) belong to
    the same cluster, and the relation chains: A~B and B~C implies
    ``{A, B, C}`` even when A and C never intersect.

    Returns clusters as lists of original input indices.  Empty/zero-length
    inputs are dropped; clusters are ordered by their earliest ``start``.
    """
    ordered = sorted(
        ((i, iv) for i, iv in enumerate(intervals) if iv.end > iv.start),
        key=lambda item: (item[1].start, item[1].end),
    )
    clusters: list[list[int]] = []
    current: list[int] = []
    cur_end: float = -math.inf
    for index, iv in ordered:
        if current and iv.start <= cur_end + _EPS:
            current.append(index)
            if iv.end > cur_end:
                cur_end = iv.end
        else:
            if current:
                clusters.append(current)
            current = [index]
            cur_end = iv.end
    if current:
        clusters.append(current)
    return clusters


def intersect(intervals: Iterable[Interval]) -> list[Interval]:
    """Intersection of interval lists, each pre-merged (touching = continuous).

    Boundary events at the same timestamp are applied as one atomic group so
    that ``[a,b)`` ending and ``[b,c)`` starting on two different operands do
    not spuriously break the result at ``b``.
    """
    lists = [ivs for ivs in (sorted(set(ivs), key=lambda iv: iv.start) for ivs in intervals) if ivs]
    if not lists:
        return []

    groups: dict[float, int] = defaultdict(int)
    n = len(lists)
    for ivs in lists:
        for iv in ivs:
            groups[iv.start] += 1
            groups[iv.end] -= 1

    # A segment is emitted only when the truth value actually changes into or
    # out of "all n active"; boundaries whose deltas cancel at the same instant
    # (one interval ends there, another starts there) leave truth unchanged and
    # must not split the result.
    result: list[Interval] = []
    active = 0
    seg_start: float | None = None
    for t in sorted(groups):
        before = active
        active += groups[t]
        if before < n <= active:
            seg_start = t
        elif before >= n > active:
            if seg_start is not None and t > seg_start:
                result.append(Interval(seg_start, t))
            seg_start = None
    return result


def subtract(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Set difference of two pre-merged half-open interval lists.

    Everything in ``a`` not covered by ``b``; touching boundaries carry no
    point of the half-open model, so subtracting ``[b0, b1)`` only removes the
    covered interior (``a`` ending exactly at ``b0`` is left intact).
    """
    if not a or not b:
        return list(a)
    result: list[Interval] = []
    j = 0
    for iv in a:
        cur = iv.start
        while j < len(b) and b[j].end <= iv.start:
            j += 1
        k = j
        while k < len(b) and b[k].start < iv.end:
            cut = b[k]
            if cut.start > cur:
                result.append(Interval(cur, min(cut.start, iv.end)))
            cur = max(cur, min(cut.end, iv.end))
            if cur >= iv.end:
                break
            k += 1
        if cur < iv.end:
            result.append(Interval(cur, iv.end))
    return result


def merge_device_windows(
    windows: Iterable[tuple[str, float, float]],
) -> dict[str, list[Interval]]:
    """Merge overlapping or touching maintenance windows, grouped by device.

    ``[a,b)`` and ``[b,c)`` on the same device touch and become one
    ``[a,c)``; windows on different devices are never merged.  Zero-length
    windows are dropped.
    """
    by_device: dict[str, list[Interval]] = defaultdict(list)
    for device, start, end in windows:
        if end > start:
            by_device[str(device)].append(Interval(float(start), float(end)))
    return {device: union(ivs) for device, ivs in by_device.items()}


def split_with_window_attribution(
    pieces: Iterable[Interval], windows: Iterable[MaintenanceWindow]
) -> list[tuple[Interval, list[int]]]:
    """Split half-open ``pieces`` at window boundaries, attributing each subpiece.

    Every piece is cut at the starts and ends of the given (per-device,
    unmerged) maintenance windows; each emitted subpiece carries only the ids
    of the windows that actually cover it.  Consequences of the half-open
    model:

    * overlapping windows covering the same subpiece share the attribution;
    * merely touching windows (``[a,b)`` then ``[b,c)``) switch attribution
      at the junction ``b``;
    * a subpiece covered by no window is never emitted (the caller only
      intersects with merged maintenance coverage, so this is a safety net);
    * a zero-length intersection produces no subpiece.
    """
    valid = [w for w in windows if w.end > w.start]
    out: list[tuple[Interval, list[int]]] = []
    for piece in pieces:
        if piece.end <= piece.start:
            continue
        bounds = {piece.start, piece.end}
        for w in valid:
            if w.start > piece.start and w.start < piece.end:
                bounds.add(w.start)
            if w.end > piece.start and w.end < piece.end:
                bounds.add(w.end)
        ordered = sorted(bounds)
        for seg_start, seg_end in zip(ordered, ordered[1:]):
            if seg_end <= seg_start:
                continue
            # Half-open coverage: a window covers a segment iff the whole
            # segment [seg_start, seg_end) lies inside [w.start, w.end).
            ids = sorted(
                {
                    w.id
                    for w in valid
                    if w.start <= seg_start and w.end >= seg_end
                }
            )
            if ids:
                out.append((Interval(seg_start, seg_end), ids))
    return out


@dataclass
class Condition:
    device: str
    metric: str
    op: str
    threshold: float
    reset_threshold: float | None = None

    def __post_init__(self) -> None:
        if not str(self.device).strip():
            raise ValueError("condition device is required")
        if not str(self.metric).strip():
            raise ValueError("condition metric is required")
        if self.op not in OPERATORS:
            raise ValueError(f"operator must be one of {sorted(OPERATORS)}")
        self.threshold = float(self.threshold)
        if self.reset_threshold is not None:
            reset = float(self.reset_threshold)
            validate_reset_threshold(self.op, self.threshold, reset)
            self.reset_threshold = reset

    @property
    def channel(self) -> tuple[str, str]:
        return (str(self.device), str(self.metric))

    @classmethod
    def from_dict(cls, raw: dict) -> "Condition":
        return cls(
            device=raw["device"],
            metric=raw["metric"],
            op=raw["op"],
            threshold=raw["threshold"],
            reset_threshold=raw.get("reset_threshold"),
        )

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "metric": self.metric,
            "op": self.op,
            "threshold": self.threshold,
            "reset_threshold": self.reset_threshold,
        }


@dataclass
class Event:
    id: int
    device: str
    metric: str
    ts: float
    value: float


@dataclass
class EvidenceItem:
    """Samples that witness one condition inside a fault interval."""

    condition: Condition
    events: list[Event] = field(default_factory=list)
    # maximal [start,end) sub-intervals in which this condition held
    held_intervals: list[Interval] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "condition": self.condition.to_dict(),
            "held_intervals": [[iv.start, iv.end] for iv in self.held_intervals],
            "events": [
                {"event_id": e.id, "ts": e.ts, "value": e.value} for e in self.events
            ],
        }


@dataclass
class FaultInterval:
    start: float
    end: float
    rule_id: int
    duration_required: float
    confirmed_at: float
    evidence: list[EvidenceItem]

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class MaintenanceWindow:
    """A named, half-open planned-maintenance window on one device."""

    id: int
    device: str
    start: float
    end: float
    name: str = ""


@dataclass
class SuppressedSegment:
    """Truth the rule *would* have had that was suppressed by maintenance.

    ``interval`` is always a piece of (raw truth − effective truth) ∩
    (this condition's held intervals) ∩ (merged maintenance of its device):
    the exact span where maintenance knocked the rule's truth out.
    ``maintenance_ids`` names only the windows that actually cover
    ``interval``: pieces are split at window boundaries, so overlapping
    windows share attribution while touching windows hand over at the
    junction.
    """

    device: str
    metric: str
    interval: Interval
    maintenance_ids: list[int] = field(default_factory=list)


@dataclass
class RuleEvaluation:
    faults: list[FaultInterval] = field(default_factory=list)
    suppressed: list[SuppressedSegment] = field(default_factory=list)


def channel_sample_spans(
    events: list[Event],
    condition: Condition,
    horizon: float,
    ttl: float | None = None,
) -> list[tuple[Event, Interval]]:
    """Each sample paired with the half-open span its value is in effect.

    Returns ``(sample, span)`` pairs in sample order, covering exactly the
    stretches where the (possibly latching) predicate holds: ``span`` is
    ``[sample.ts, min(next_sample.ts, sample.ts + ttl, horizon))`` for the
    samples whose held value keeps the condition true.  Equal timestamps keep
    the last supplied value.

    When ``ttl`` is a positive number of seconds the hold is capped, and
    whatever remains after the cap is an *invalid gap* (no held value at
    all), so the condition drops out of AND/OR combinations there and any
    truth interruption makes the sustained duration re-accumulate from
    scratch.

    When the condition carries a ``reset_threshold`` the predicate becomes a
    latch evaluated in held-interval order: it first arms only on a value
    satisfying the original comparison, then stays armed through values inside
    the hysteresis band, and disarms only when a value reaches the reset
    boundary (high trigger: ``value <= reset``; low trigger:
    ``value >= reset``).  An invalid TTL gap disarms the latch as well, so the
    next valid sample must re-cross the original trigger before anything is
    held again.
    """
    ttl = float(ttl) if ttl is not None else None
    if ttl is not None and ttl <= 0:
        raise ValueError("sample validity (ttl) must be a positive number of seconds")

    ordered: list[Event] = []
    for ev in sorted(events, key=lambda e: (e.ts, e.id)):
        if ordered and ordered[-1].ts == ev.ts:
            ordered[-1] = ev  # last write at identical timestamp
        else:
            ordered.append(ev)

    spans: list[tuple[Event, Interval]] = []
    latched = False
    prev_end: float | None = None
    for i, ev in enumerate(ordered):
        seg_end = ordered[i + 1].ts if i + 1 < len(ordered) else horizon
        if ttl is not None:
            seg_end = min(seg_end, ev.ts + ttl, horizon)
        # A positive stretch between the previous held piece and this
        # sample's timestamp means the previous value expired (TTL gap): the
        # channel holds nothing in between, so the latch re-arms from scratch.
        if prev_end is not None and ev.ts > prev_end:
            latched = False
        if seg_end <= ev.ts:
            prev_end = seg_end
            continue
        if condition.reset_threshold is not None:
            if not latched:
                latched = compare(ev.value, condition.op, condition.threshold)
            elif at_reset_boundary(ev.value, condition.op, condition.reset_threshold):
                latched = False
        else:
            latched = compare(ev.value, condition.op, condition.threshold)
        if latched:
            spans.append((ev, Interval(ev.ts, seg_end)))
        prev_end = seg_end
    return spans


def channel_held_intervals(
    events: list[Event],
    condition: Condition,
    horizon: float,
    ttl: float | None = None,
) -> tuple[list[Interval], list[Event]]:
    """Return ([start,end) intervals where the predicate held], witnessing events).

    Derived from :func:`channel_sample_spans`: the held intervals are the
    merged (touching = continuous) per-sample spans, the witnesses are the
    samples those spans belong to.  See that function for the hold / TTL /
    latching semantics.
    """
    spans = channel_sample_spans(events, condition, horizon, ttl=ttl)
    return union([iv for _, iv in spans]), [ev for ev, _ in spans]


def _merge_or(per_condition: list[list[Interval]]) -> list[Interval]:
    """Union via sweep-line; touching segments are continuous."""
    return union(iv for ivs in per_condition for iv in ivs)


def evaluate_rule(
    rule_id: int,
    logic: str,
    duration_required: float,
    conditions: list[Condition],
    events_by_channel: dict[tuple[str, str], list[Event]],
    horizon: float,
    maintenance: list[MaintenanceWindow] | None = None,
    ttl_by_channel: dict[tuple[str, str], float] | None = None,
) -> RuleEvaluation:
    """Evaluate one rule against indexed channel data.

    Conditions with a ``reset_threshold`` are latching (see
    :func:`channel_held_intervals`): the first value must satisfy the
    original comparison, afterwards the condition stays satisfied until a
    held value reaches the reset boundary; the latch is also cleared by an
    invalid TTL gap, so the next sample must re-cross the trigger.

    Per-channel **sample validity periods** (``ttl_by_channel``) cap each
    sample's held interval to ``[ts, min(next ts, ts + ttl, horizon))``;
    invalid gaps satisfy none of the rule's conditions, so an AND rule drops
    out for their whole duration while an OR rule survives only while another
    channel still holds.  Either way the truth interruption splits the merged
    truth segments, which re-accumulates the sustained duration from each
    surviving segment's start, and fault intervals, confirmation instants and
    trigger evidence can never cross an invalid gap.

    Planned maintenance is applied *before* combining conditions: per device,
    overlapping or touching windows are merged into maximal half-open spans,
    then subtracted from every held interval of that device's channels.  A
    maintenance span therefore cuts the rule's truth, **resets the sustained
    duration** (each surviving truth segment re-accumulates from its own
    start, so ``confirmed_at = segment.start + duration``) and clips trigger
    evidence so it never crosses a maintenance boundary.

    Trigger evidence is attributed by each sample's **effective span** — its
    held interval after the TTL cap and the maintenance subtraction.  A
    sample witnesses a fault interval exactly while such a span overlaps it,
    so a sample taken *before* the fault start whose value is still in effect
    when the AND intersection begins (or that keeps holding past a
    maintenance window's end) is kept as evidence, while a sample whose span
    expired (TTL gap) or was subtracted away (maintenance) never witnesses
    that stretch.  Each evidence item's ``held_intervals`` are clipped to the
    fault's half-open ``[start, end)``, never crossing its boundaries.

    Returns the surviving fault intervals (length >= ``duration_required``)
    plus the suppressed segments: spans where the rule's truth was lost to
    maintenance, attributed per condition channel.  Each suppressed span is
    split at the boundaries of the device's (unmerged) maintenance windows,
    and every resulting segment names only the windows covering it, so two
    separate windows yield separately attributed segments, overlapping
    windows share the attribution and touching windows hand over at the
    junction (zero-length intersections emit nothing).
    """
    logic = logic.upper()
    if logic not in LOGIC:
        raise ValueError("logic must be AND or OR")
    duration_required = float(duration_required)
    if duration_required < 0:
        raise ValueError("duration must be non-negative")
    if not conditions:
        return RuleEvaluation()

    ttl_by_channel = ttl_by_channel or {}
    windows = maintenance or []
    merged_by_device = merge_device_windows(
        (w.device, w.start, w.end) for w in windows
    )
    # The *unmerged* windows are kept per device for attribution: suppressed
    # pieces are split at window boundaries so each piece names only the
    # window(s) actually covering it (overlapping windows share attribution;
    # touching windows hand over at their junction).
    raw_windows_by_device: dict[str, list[MaintenanceWindow]] = defaultdict(list)
    for w in windows:
        if w.end > w.start:
            raw_windows_by_device[str(w.device)].append(w)

    raw_held_per: list[list[Interval]] = []
    held_per: list[list[Interval]] = []
    # Per condition, each witness sample paired with its *effective* spans:
    # the pieces of its held interval that survive the maintenance
    # subtraction (the TTL cap is already applied by channel_sample_spans).
    # Evidence is attributed from these spans, so a pre-fault sample still in
    # effect inside the fault is kept, while spans lost to a TTL gap or a
    # maintenance window contribute nothing.
    effective_spans_per: list[list[tuple[Event, Interval]]] = []
    for cond in conditions:
        spans = channel_sample_spans(
            events_by_channel.get(cond.channel, []),
            cond,
            horizon,
            ttl=ttl_by_channel.get(cond.channel),
        )
        raw_held_per.append(union([iv for _, iv in spans]))
        maint = merged_by_device.get(str(cond.device))
        if maint:
            effective = [
                (ev, piece)
                for ev, span in spans
                for piece in subtract([span], maint)
            ]
        else:
            effective = spans
        effective_spans_per.append(effective)
        held_per.append(union([iv for _, iv in effective]))

    # AND over channels that actually exist; a condition with no channel
    # data can never be satisfied.
    if logic == "AND" and any(
        cond.channel not in events_by_channel for cond in conditions
    ):
        return RuleEvaluation()

    def combine(held_lists: list[list[Interval]]) -> list[Interval]:
        if logic == "OR":
            return _merge_or(held_lists)
        return intersect(held_lists)

    truth = combine(held_per)

    suppressed: list[SuppressedSegment] = []
    if windows:
        # Spans where the rule's truth existed before maintenance but is gone
        # after; suppressed pieces are clipped to these spans so they mark
        # exactly where maintenance knocked the rule out.
        lost = subtract(combine(raw_held_per), truth)
        if lost:
            for cond, raw_held in zip(conditions, raw_held_per):
                device = str(cond.device)
                maint = merged_by_device.get(device)
                device_windows = raw_windows_by_device.get(device)
                if not maint or not device_windows:
                    continue
                covered_pieces = intersect([raw_held, maint, lost])
                for piece, ids in split_with_window_attribution(
                    covered_pieces, device_windows
                ):
                    suppressed.append(
                        SuppressedSegment(
                            device=device,
                            metric=str(cond.metric),
                            interval=piece,
                            maintenance_ids=ids,
                        )
                    )

    faults: list[FaultInterval] = []
    for iv in truth:
        if iv.end - iv.start + _EPS >= duration_required:
            evidence: list[EvidenceItem] = []
            for cond, effective in zip(conditions, effective_spans_per):
                # A sample witnesses this fault exactly while one of its
                # effective spans overlaps [iv.start, iv.end); the reported
                # held sub-intervals are those overlaps, clipped to the
                # fault's half-open bounds.  Spans are emitted in sample
                # order, so the scan can stop once it passes the fault end.
                clipped: list[Interval] = []
                inside_events: list[Event] = []
                for ev, piece in effective:
                    if piece.start >= iv.end:
                        break
                    lo = max(piece.start, iv.start)
                    hi = min(piece.end, iv.end)
                    if hi <= lo:
                        continue
                    clipped.append(Interval(lo, hi))
                    if not inside_events or inside_events[-1] is not ev:
                        inside_events.append(ev)
                evidence.append(
                    EvidenceItem(
                        condition=cond,
                        events=inside_events,
                        held_intervals=union(clipped),
                    )
                )
            faults.append(
                FaultInterval(
                    start=iv.start,
                    end=iv.end,
                    rule_id=rule_id,
                    duration_required=duration_required,
                    confirmed_at=iv.start + duration_required,
                    evidence=evidence,
                )
            )
    return RuleEvaluation(faults=faults, suppressed=suppressed)


# --------------------------------------------------------------------------- #
# Fault-set diff (draft-rule impact preview)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FaultDiffSegment:
    """One half-open piece of the symmetric comparison of two fault sets.

    kind:
      * ``"added"``     — covered by a draft fault only;
      * ``"removed"``   — covered by a baseline fault only;
      * ``"overlap_changed"`` — covered on both sides but the confirmation
        instants of the two faults differ (the segment carries both
        ``baseline_confirmed_at`` and ``draft_confirmed_at``);
      * ``"overlap_unchanged"`` — covered on both sides with the same
        confirmation instant.
    """

    kind: str
    start: float
    end: float
    baseline_confirmed_at: float | None = None
    draft_confirmed_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "baseline_confirmed_at": self.baseline_confirmed_at,
            "draft_confirmed_at": self.draft_confirmed_at,
        }


def _coalesce_diff(segments: list[tuple[float, float, str, FaultInterval | None, FaultInterval | None]]) -> list[FaultDiffSegment]:
    """Join adjacent diff pieces of the same kind and same fault provenance.

    The sweep line may split an otherwise continuous span at a boundary where
    one side swaps between two of its own faults; pieces of the same kind that
    merely touch and still come from the same pair of faults are re-joined so
    the caller sees maximal half-open time pieces.
    """
    out: list[FaultDiffSegment] = []
    for start, end, kind, base, draft in segments:
        if (
            out
            and abs(out[-1].end - start) <= _EPS
            and out[-1].kind == kind
            and out[-1].baseline_confirmed_at == (base.confirmed_at if base else None)
            and out[-1].draft_confirmed_at == (draft.confirmed_at if draft else None)
        ):
            out[-1] = FaultDiffSegment(
                kind=kind,
                start=out[-1].start,
                end=end,
                baseline_confirmed_at=out[-1].baseline_confirmed_at,
                draft_confirmed_at=out[-1].draft_confirmed_at,
            )
        else:
            out.append(
                FaultDiffSegment(
                    kind=kind,
                    start=start,
                    end=end,
                    baseline_confirmed_at=base.confirmed_at if base else None,
                    draft_confirmed_at=draft.confirmed_at if draft else None,
                )
            )
    return out


def diff_faults(
    baseline: list[FaultInterval], draft: list[FaultInterval]
) -> list[FaultDiffSegment]:
    """Compare two confirmed fault sets of the same rule.

    Both sets are compared as half-open interval coverings:

    * time covered by the draft only is emitted as ``added`` pieces;
    * time covered by the baseline only as ``removed`` pieces;
    * overlapping time is covered on both sides; when the two faults share
      the same ``confirmed_at`` it is ``overlap_unchanged``, otherwise
      ``overlap_changed`` — the interval need not move at all, a shifted
      confirmation instant alone marks it.

    Fault intervals within one side never overlap (the engine emits merged,
    disjoint truth segments); the comparison sweeps over the union of both
    sets' boundaries, so the half-open boundary ``[a,b)`` / ``[b,c)`` never
    produces a spurious zero-length piece.
    """
    starts_base = {f.start: f for f in baseline if f.end > f.start}
    ends_base = {f.end: f for f in baseline if f.end > f.start}
    starts_draft = {f.start: f for f in draft if f.end > f.start}
    ends_draft = {f.end: f for f in draft if f.end > f.start}
    bounds = sorted(
        set(starts_base) | set(ends_base) | set(starts_draft) | set(ends_draft)
    )

    def kind_of(b: FaultInterval | None, d: FaultInterval | None) -> str | None:
        if b is not None and d is not None:
            if abs(b.confirmed_at - d.confirmed_at) <= _EPS:
                return "overlap_unchanged"
            return "overlap_changed"
        if d is not None:
            return "added"
        if b is not None:
            return "removed"
        return None

    raw: list[tuple[float, float, str, FaultInterval | None, FaultInterval | None]] = []
    active_base: FaultInterval | None = None
    active_draft: FaultInterval | None = None
    for i, t in enumerate(bounds):
        # Apply every boundary event at t atomically: an ending fault leaves
        # first, a starting one at the same instant takes over (touching
        # intervals carry no point in the half-open model).
        if active_base is not None and ends_base.get(t) is active_base:
            active_base = None
        if active_draft is not None and ends_draft.get(t) is active_draft:
            active_draft = None
        if t in starts_base:
            active_base = starts_base[t]
        if t in starts_draft:
            active_draft = starts_draft[t]
        if i + 1 >= len(bounds):
            break
        kind = kind_of(active_base, active_draft)
        if kind is not None:
            raw.append((t, bounds[i + 1], kind, active_base, active_draft))

    return _coalesce_diff(raw)


# --------------------------------------------------------------------------- #
# Per-day fault ledger for a combination of rules
# --------------------------------------------------------------------------- #

DAY_SECONDS = 86400.0


def utc_midnight(t: float) -> float:
    """Floor an epoch timestamp to the UTC midnight that starts its day.

    POSIX days are exactly 86400 seconds and epoch zero is 1970-01-01T00:00Z,
    so UTC midnights are integer multiples of ``DAY_SECONDS``.
    """
    return math.floor(t / DAY_SECONDS) * DAY_SECONDS


def daily_fault_ledger(
    intervals: Iterable[Interval], start: float, end: float
) -> list[dict]:
    """Per-day fault accounting for the half-open query window ``[start, end)``.

    The input intervals (stored confirmed faults of the selected rules) are
    first merged as half-open intervals: overlapping or merely touching
    pieces become one segment, so fault time covered by several rules at
    once is counted only once.  Every merged segment is then split at UTC
    midnights and accounted per day:

    * ``fault_seconds`` — total merged (deduplicated) fault time in the day,
      clipped both to the day and to the query window;
    * ``new_segments`` — segments whose start instant lies inside this UTC
      day (a cross-day segment is "new" on its start day only);
    * ``continuing_segments`` — segments that started on an earlier UTC day
      and continue into this one across midnight;
    * ``queried_seconds`` — length of the day actually covered by the query:
      86400 for whole days, clipped at the first/last day when the query
      bounds fall mid-day; ``coverage`` is fault_seconds / queried_seconds.

    A segment ending exactly at a midnight is half-open, so it produces no
    piece on the next day (no continuing segment there).  Every UTC day
    touched by the query appears in the result, including days without any
    fault time (zeros, coverage 0).
    """
    start = float(start)
    end = float(end)
    if end <= start:
        raise ValueError("ledger end must be greater than start")

    merged = union(intervals)
    days: list[dict] = []
    day0 = utc_midnight(start)
    while day0 < end:
        day1 = day0 + DAY_SECONDS
        win0 = max(day0, start)
        win1 = min(day1, end)
        queried_seconds = win1 - win0

        fault_seconds = 0.0
        new_segments = 0
        continuing_segments = 0
        for iv in merged:
            seg0 = max(iv.start, win0)
            seg1 = min(iv.end, win1)
            if seg1 > seg0:
                fault_seconds += seg1 - seg0
                if iv.start >= day0:
                    # The segment starts inside this UTC day (a segment that
                    # began earlier on the same calendar day but before the
                    # query bound still has this day as its start day).
                    new_segments += 1
                else:
                    # It started on an earlier UTC day and crosses midnight.
                    continuing_segments += 1

        days.append(
            {
                "date": datetime.fromtimestamp(day0, tz=timezone.utc).strftime("%Y-%m-%d"),
                "day_start": day0,
                "day_end": day1,
                "queried_start": win0,
                "queried_end": win1,
                "queried_seconds": queried_seconds,
                "fault_seconds": fault_seconds,
                "new_segments": new_segments,
                "continuing_segments": continuing_segments,
                "coverage": fault_seconds / queried_seconds,
            }
        )
        day0 = day1
    return days
