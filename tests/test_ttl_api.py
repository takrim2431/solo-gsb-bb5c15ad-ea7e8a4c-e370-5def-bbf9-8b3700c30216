"""End-to-end tests for per-channel sample validity (TTL): storage, API
view/set/clear, automatic re-evaluation, back-fill recomputation and read-only
preview participation.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import to_epoch


def at(minute: int, second: int = 0) -> float:
    return to_epoch(f"2026-09-23T00:{minute:02d}:{second:02d}Z")


@pytest.fixture()
def scratch_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.DB_PATH = tmp.name
    db._conn = None
    db.init_db()
    yield tmp.name
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(tmp.name + suffix)
        except OSError:
            pass


def _import_minute_series(true_minutes=range(6, 17), device="d", metric="m"):
    rows = [
        {
            "device": device,
            "metric": metric,
            "ts": f"2026-09-23T00:{m:02d}:00Z",
            "value": 90 if m in true_minutes else 10,
        }
        for m in range(31)
    ]
    return services.import_events("\n".join(json.dumps(r) for r in rows))


def _create_rule(duration=300, device="d", metric="m"):
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("TTL 规则", "AND", duration, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, device, metric, ">=", 80, 0),
    )
    return rule_id


# --------------------------------------------------------------------------- #
# storage helpers
# --------------------------------------------------------------------------- #


def test_ttl_crud_roundtrip(scratch_db):
    assert db.fetch_channel_ttls() == []
    services.set_channel_ttl("pump-1", "temp", 120)
    row = db.fetch_channel_ttl("pump-1", "temp")
    assert row["ttl_seconds"] == 120.0
    assert {t["device"] for t in db.fetch_channel_ttls()} == {"pump-1"}
    # replace
    services.set_channel_ttl("pump-1", "temp", 45)
    assert db.fetch_channel_ttl("pump-1", "temp")["ttl_seconds"] == 45.0
    # clear
    assert services.clear_channel_ttl("pump-1", "temp")["cleared"]["had_ttl_seconds"] == 45.0
    assert db.fetch_channel_ttl("pump-1", "temp") is None
    with pytest.raises(KeyError):
        services.clear_channel_ttl("pump-1", "temp")


def test_set_ttl_rejects_non_positive(scratch_db):
    with pytest.raises(ValueError):
        services.set_channel_ttl("d", "m", 0)
    with pytest.raises(ValueError):
        services.set_channel_ttl("d", "m", -1)


# --------------------------------------------------------------------------- #
# evaluation picks TTL up; changing/clearing it recomputes stored faults
# --------------------------------------------------------------------------- #


def test_ttl_change_recomputes_stored_faults(scratch_db):
    _import_minute_series()
    rid = _create_rule(duration=300)
    services.evaluate_all_rules()
    faults = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in faults] == [(at(6), at(17))]

    # 30s TTL against 60s spacing: every high sample held only 30s, below the
    # 300s sustained requirement -> no faults
    res = services.set_channel_ttl("d", "m", 30)
    assert res["evaluation"]["fault_intervals"] == 0
    assert services.query_faults(None, None) == []

    # clearing restores the original fault
    res = services.clear_channel_ttl("d", "m")
    assert res["evaluation"]["fault_intervals"] == 1
    faults = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in faults] == [(at(6), at(17))]
    # confirmed_at is recomputed from the surviving segment start
    assert faults[0]["confirmed_at"] == at(6) + 300
    assert rid


def test_short_ttl_resets_duration_within_contiguous_high_run(scratch_db):
    _import_minute_series()
    _create_rule(duration=120)
    # samples 60s apart, TTL 30s: each high sample yields a 30s piece
    services.set_channel_ttl("d", "m", 30)
    assert services.query_faults(None, None) == []
    # TTL exactly 60s: pieces touch -> continuous again
    services.set_channel_ttl("d", "m", 60)
    faults = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in faults] == [(at(6), at(17))]


def test_fault_interval_and_evidence_do_not_cross_ttl_gap(scratch_db):
    # high samples at minutes 6..16, TTL 59s leaves a 1s gap per step;
    # duration 60s must fail everywhere, and even at duration 0 each stored
    # fault piece plus its evidence must stay within one held interval.
    _import_minute_series()
    _create_rule(duration=0)
    services.set_channel_ttl("d", "m", 59)
    faults = services.query_faults(None, None)
    assert len(faults) == 11
    for fault, minute in zip(faults, range(6, 17)):
        assert (fault["start"], fault["end"]) == (at(minute), at(minute, 59))
        group = fault["evidence"][0]
        assert group["held_intervals"] == [[at(minute), at(minute, 59)]]
        assert [e["ts"] for e in group["events"]] == [at(minute)]


def test_backfilled_old_sample_recomputes_faults(scratch_db):
    # import only minutes 8..30 initially, so the high run starts at 8
    _import_minute_series(true_minutes=range(6, 17))
    _create_rule(duration=300)
    # remove the first high minutes to simulate a late-arriving history
    db.execute("DELETE FROM events WHERE ts <= ?", (at(7),))
    services.evaluate_all_rules()
    assert [(f["start"], f["end"]) for f in services.query_faults(None, None)] == [
        (at(8), at(17))
    ]
    # back-fill the older samples: import auto-re-evaluates and the fault now
    # starts at minute 6
    rows = [
        json.dumps({"device": "d", "metric": "m", "ts": f"2026-09-23T00:{m:02d}:00Z", "value": 90})
        for m in (6, 7)
    ]
    result = services.import_events("\n".join(rows))
    assert result["evaluation"]["rules_evaluated"] == 1
    assert [(f["start"], f["end"]) for f in services.query_faults(None, None)] == [
        (at(6), at(17))
    ]


def test_backfill_inside_ttl_gap_reopens_bridge(scratch_db):
    # high at 6 and 12, low at 7..11; TTL 60s exactly matches the spacing,
    # so the two 60s pieces [6,7) and [12,13) exist but the 90s rule never fires
    _import_minute_series(true_minutes={6, 12})
    _create_rule(duration=90)
    services.set_channel_ttl("d", "m", 60)
    assert services.query_faults(None, None) == []
    # back-fill corrected high values for the intermediate minutes: the
    # touching pieces (TTL == spacing) bridge into a continuous run [6,13)
    rows = [
        json.dumps({"device": "d", "metric": "m", "ts": f"2026-09-23T00:{m:02d}:00Z", "value": 90})
        for m in (7, 8, 9, 10, 11)
    ]
    services.import_events("\n".join(rows))
    faults = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in faults] == [(at(6), at(13))]


def test_ttl_participates_in_preview_but_is_not_written(scratch_db):
    _import_minute_series()
    rule_id = _create_rule(duration=300)
    services.set_channel_ttl("d", "m", 30)  # makes the stored rule faultless
    # baseline in the preview must also see the TTL (self-recomputed), and
    # draft shortening the duration below 30s shows added pieces
    result = services.preview_rule(
        rule_id,
        {
            "name": "TTL 规则",
            "logic": "AND",
            "duration": 0,
            "conditions": [{"device": "d", "metric": "m", "op": ">=", "threshold": 80}],
        },
    )
    assert result["channel_ttls"] == 1
    assert result["baseline"]["faults"] == []
    added = [(s["start"], s["end"]) for s in result["diff"]["segments"] if s["kind"] == "added"]
    assert added == [(at(m), at(m, 30)) for m in range(6, 17)]
    # preview wrote nothing to the rules/faults
    assert services.query_faults(None, None) == []
    assert db.query_one("SELECT duration AS d FROM rules WHERE id = ?", (rule_id,))["d"] == 300


def test_ttl_reset_clears_table(scratch_db):
    services.set_channel_ttl("d", "m", 60)
    db.reset_all()
    assert db.fetch_channel_ttls() == []


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_api_ttl_lifecycle(client):
    # empty initially
    assert client.get("/api/channel-ttls").json() == []
    # set
    r = client.put("/api/channel-ttls", json={"device": "d", "metric": "m", "ttl_seconds": 90})
    assert r.status_code == 200
    body = r.json()
    assert body["device"] == "d" and body["ttl_seconds"] == 90
    assert body["evaluation"]["rules_evaluated"] == 0
    # view, with filters
    assert len(client.get("/api/channel-ttls").json()) == 1
    assert client.get("/api/channel-ttls", params={"device": "d"}).json()[0]["metric"] == "m"
    assert client.get("/api/channel-ttls", params={"device": "x"}).json() == []
    # validation
    assert client.put(
        "/api/channel-ttls", json={"device": "d", "metric": "m", "ttl_seconds": 0}
    ).status_code == 422
    assert client.put(
        "/api/channel-ttls", json={"device": "d", "metric": "m", "ttl_seconds": -3}
    ).status_code == 422
    # clear + 404 on repeat
    r = client.delete("/api/channel-ttls", params={"device": "d", "metric": "m"})
    assert r.status_code == 200 and r.json()["cleared"]["had_ttl_seconds"] == 90
    r = client.delete("/api/channel-ttls", params={"device": "d", "metric": "m"})
    assert r.status_code == 404


def test_api_timeline_and_export_include_ttls(client):
    client.put("/api/channel-ttls", json={"device": "d", "metric": "m", "ttl_seconds": 90})
    tl = client.get("/api/timeline").json()
    assert [(t["device"], t["metric"], t["ttl_seconds"]) for t in tl["channel_ttls"]] == [
        ("d", "m", 90.0)
    ]
    exported = client.get("/api/export").json()
    assert [t["device"] for t in exported["channel_ttls"]] == ["d"]
