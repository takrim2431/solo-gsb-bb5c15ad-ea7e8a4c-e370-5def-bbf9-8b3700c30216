"""Tests for fault-event clustering built on top of stored fault dicts."""

from app.services import build_fault_events


def fault(fid, rule_id, name, start, end, logic="AND", duration=0.0):
    return {
        "id": fid,
        "rule_id": rule_id,
        "rule_name": name,
        "logic": logic,
        "start": float(start),
        "end": float(end),
        "duration_required": float(duration),
        "confirmed_at": float(start) + float(duration),
        "evidence": [
            {
                "condition": {"device": "d", "metric": "m", "op": ">", "threshold": 1},
                "held_intervals": [[float(start), float(end)]],
                "events": [{"event_id": fid, "ts": float(start), "value": 2.0}],
            }
        ],
    }


def test_events_merge_overlapping_different_rules_and_span_extremes():
    faults = [fault(1, 1, "R1", 0, 10), fault(2, 2, "R2", 5, 15)]
    events = build_fault_events(faults)
    assert len(events) == 1
    ev = events[0]
    assert (ev["start"], ev["end"]) == (0.0, 15.0)
    assert ev["rule_count"] == 2
    assert ev["fault_count"] == 2
    assert ev["rule_names"] == ["R1", "R2"]
    assert [m["fault_id"] for m in ev["members"]] == [1, 2]
    # original member intervals and trigger evidence are carried verbatim
    assert ev["members"][0]["start"] == 0.0 and ev["members"][0]["end"] == 10.0
    assert ev["members"][0]["evidence"][0]["events"][0]["event_id"] == 1


def test_events_merge_end_to_end_touch():
    faults = [fault(1, 1, "R1", 0, 6), fault(2, 2, "R2", 6, 9)]
    events = build_fault_events(faults)
    assert len(events) == 1
    assert (events[0]["start"], events[0]["end"]) == (0.0, 9.0)


def test_events_transitive_chain():
    # R1 [0,6) touches R2 [6,10), R2 touches R3 [10,14): one event even
    # though R1 and R3 never intersect.
    faults = [
        fault(1, 1, "R1", 0, 6),
        fault(2, 2, "R2", 6, 10),
        fault(3, 3, "R3", 10, 14),
    ]
    events = build_fault_events(faults)
    assert len(events) == 1
    assert (events[0]["start"], events[0]["end"]) == (0.0, 14.0)
    assert events[0]["rule_count"] == 3


def test_events_keep_real_gap_apart():
    faults = [fault(1, 1, "R1", 0, 6), fault(2, 2, "R2", 7, 10)]
    events = build_fault_events(faults)
    assert [(e["start"], e["end"]) for e in events] == [(0.0, 6.0), (7.0, 10.0)]
    assert [e["id"] for e in events] == ["E1", "E2"]


def test_event_window_filters_events_but_keeps_outside_members():
    # Event spans [0,14) via a chain; window covers only the beginning, yet all
    # members are listed with an in_window flag.  Member 3 is [10,14) and only
    # touches the window end at 10, so half-open semantics put it out of window.
    faults = [
        fault(1, 1, "R1", 0, 6),
        fault(2, 2, "R2", 6, 10),
        fault(3, 3, "R3", 10, 14),
        fault(4, 4, "R4", 30, 40),
    ]
    events = build_fault_events(faults, start=5, end=10)
    assert len(events) == 1
    ev = events[0]
    assert (ev["start"], ev["end"]) == (0.0, 14.0)
    flags = {m["fault_id"]: m["in_window"] for m in ev["members"]}
    assert flags == {1: True, 2: True, 3: False}
    assert ev["member_count_in_window"] == 2


def test_event_window_excludes_disjoint_event():
    faults = [fault(1, 1, "R1", 0, 6), fault(2, 2, "R2", 20, 25)]
    events = build_fault_events(faults, start=10, end=30)
    assert [(e["start"], e["end"]) for e in events] == [(20.0, 25.0)]


def test_original_faults_not_mutated():
    faults = [fault(1, 1, "R1", 0, 6), fault(2, 2, "R2", 6, 10)]
    snapshot = [dict(f) for f in faults]
    build_fault_events(faults)
    assert faults == snapshot
    assert "in_window" not in faults[0]
