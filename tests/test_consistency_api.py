"""End-to-end tests for the read-only redundant-channel consistency check API.

The check reads the current events on the fly: it never inserts/updates or
deletes events, never writes fault results and never triggers rule
re-evaluation.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import to_epoch

BASE = to_epoch("2026-09-23T00:00:00Z")
P = 60.0


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


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _load(rows, device, metric):
    payload = [{"device": device, "metric": metric, "ts": ts, "value": v} for ts, v in rows]
    services.import_events("\n".join(json.dumps(r) for r in payload), reevaluate=False)


def _load_pair(a_values, b_values, step=P, device_a="pump-1", device_b="pump-2", metric="temp"):
    _load([(BASE + i * step, v) for i, v in enumerate(a_values)], device_a, metric)
    _load([(BASE + i * step, v) for i, v in enumerate(b_values)], device_b, metric)


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


def test_service_basic_segments_and_totals(scratch_db):
    # b drifts above the threshold for two touching minutes, then recovers
    _load_pair([80, 80, 80, 80, 80], [80, 90, 95, 80, 80])
    result = services.query_channel_consistency(
        "pump-1", "temp", "pump-2", "temp", BASE, BASE + 4 * P, 5.0
    )
    assert result["comparable_seconds"] == pytest.approx(4 * P)
    assert result["inconsistent_seconds"] == pytest.approx(2 * P)
    assert result["inconsistent_ratio"] == pytest.approx(0.5)
    assert result["segment_count"] == 1
    seg = result["segments"][0]
    assert (seg["start"], seg["end"]) == (BASE + P, BASE + 3 * P)
    assert seg["duration_seconds"] == pytest.approx(2 * P)
    assert seg["max_abs_diff"] == pytest.approx(15)
    assert seg["max_at"]["a"] == {"ts": BASE + 2 * P, "value": 80}
    assert seg["max_at"]["b"] == {"ts": BASE + 2 * P, "value": 95}
    assert result["channel_a"]["ttl_seconds"] is None
    assert result["channel_b"]["event_count"] == 5
    assert result["horizon"] == BASE + 4 * P


def test_service_honours_channel_ttl_and_pre_window_sample(scratch_db):
    # channel b has one sample before the window and a 90s TTL: it is still
    # valid for the first 30s of the window, then expires (no extrapolation)
    _load([(BASE, 10.0), (BASE + 90, 10.0)], "pump-1", "temp")
    _load([(BASE - 60, 4.0)], "pump-2", "temp")
    services.set_channel_ttl("pump-2", "temp", 90.0)
    result = services.query_channel_consistency(
        "pump-1", "temp", "pump-2", "temp", BASE, BASE + 120, 1.0
    )
    # b holds [BASE-60, BASE+30): comparable only [BASE, BASE+30)
    assert result["comparable_seconds"] == pytest.approx(30)
    assert result["channel_b"]["ttl_seconds"] == 90.0
    assert [(s["start"], s["end"]) for s in result["segments"]] == [(BASE, BASE + 30)]
    assert result["segments"][0]["max_at"]["b"] == {"ts": BASE - 60, "value": 4.0}


def test_service_unknown_channel_and_bad_args(scratch_db):
    _load_pair([1], [1])
    with pytest.raises(KeyError):
        services.query_channel_consistency("pump-1", "nope", "pump-2", "temp", BASE, BASE + P, 1.0)
    with pytest.raises(KeyError):
        services.query_channel_consistency("pump-1", "temp", "ghost", "temp", BASE, BASE + P, 1.0)
    with pytest.raises(ValueError):
        services.query_channel_consistency("pump-1", "temp", "pump-1", "temp", BASE, BASE + P, 1.0)
    with pytest.raises(ValueError):
        services.query_channel_consistency("pump-1", "temp", "pump-2", "temp", BASE + P, BASE, 1.0)
    with pytest.raises(ValueError):
        services.query_channel_consistency("pump-1", "temp", "pump-2", "temp", BASE, BASE + P, -1.0)


def test_service_is_read_only(scratch_db):
    _load_pair([80, 90], [80, 80])
    db.execute("INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
               ("r", "AND", 0, 0))
    db.execute("INSERT INTO fault_intervals (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)"
               " VALUES (?,?,?,?,?,?)", (1, BASE, BASE + P, 0, BASE, "[]"))
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    services.query_channel_consistency("pump-1", "temp", "pump-2", "temp", BASE, BASE + 2 * P, 1.0)
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults
    # the stored fault row is verbatim, not recomputed against the new data
    row = db.query_one("SELECT start_ts, end_ts FROM fault_intervals WHERE rule_id = 1")
    assert (row["start_ts"], row["end_ts"]) == (BASE, BASE + P)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _params(**over):
    params = {
        "device_a": "pump-1", "metric_a": "temp",
        "device_b": "pump-2", "metric_b": "temp",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:04:00Z",
        "threshold": 5,
    }
    params.update(over)
    return params


def test_api_check_ok(scratch_db, client):
    _load_pair([80, 80, 80, 80, 80], [80, 90, 95, 80, 80])
    resp = client.get("/api/consistency-check", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_a"] == {
        "device": "pump-1", "metric": "temp", "ttl_seconds": None,
        "event_count": 5, "min_ts": BASE, "max_ts": BASE + 4 * P,
    }
    assert body["segment_count"] == 1
    assert body["comparable_seconds"] == pytest.approx(4 * P)
    assert body["inconsistent_seconds"] == pytest.approx(2 * P)
    seg = body["segments"][0]
    assert seg["max_abs_diff"] == pytest.approx(15)
    assert seg["max_at"]["b"]["value"] == 95


def test_api_epoch_range_and_half_open_end(scratch_db, client):
    # the last sample lands exactly on the end bound: outside the window
    _load_pair([80, 80, 80], [80, 80, 999])
    resp = client.get("/api/consistency-check", params=_params(
        start=BASE, end=BASE + 2 * P, threshold=5,
    ))
    assert resp.status_code == 200
    body = resp.json()
    assert body["comparable_seconds"] == pytest.approx(2 * P)
    assert body["segments"] == []


def test_api_same_channel_400(scratch_db, client):
    _load_pair([1], [1])
    resp = client.get("/api/consistency-check", params=_params(device_b="pump-1"))
    assert resp.status_code == 400


def test_api_unknown_channel_404(scratch_db, client):
    _load_pair([1], [1])
    resp = client.get("/api/consistency-check", params=_params(metric_b="nope"))
    assert resp.status_code == 404


def test_api_inverted_window_400(scratch_db, client):
    _load_pair([1], [1])
    resp = client.get("/api/consistency-check", params=_params(
        start="2026-09-23T00:04:00Z", end="2026-09-23T00:00:00Z",
    ))
    assert resp.status_code == 400


def test_api_bad_threshold_rejected(scratch_db, client):
    _load_pair([1], [1])
    resp = client.get("/api/consistency-check", params=_params(threshold=-1))
    assert resp.status_code == 422  # Query(..., ge=0) rejects a negative threshold
    resp = client.get("/api/consistency-check", params=_params(threshold="nan"))
    assert resp.status_code == 422  # NaN fails the ge=0 query validation
    resp = client.get("/api/consistency-check", params=_params(threshold="inf"))
    assert resp.status_code == 400  # non-finite threshold rejected explicitly


def test_api_missing_params_422(client):
    assert client.get("/api/consistency-check").status_code == 422
    resp = client.get("/api/consistency-check", params={
        "device_a": "pump-1", "metric_a": "temp",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:04:00Z",
        "threshold": 5,
    })
    assert resp.status_code == 422


def test_api_does_not_touch_events_or_faults(scratch_db, client):
    _load_pair([80, 90], [80, 80])
    db.execute("INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
               ("r", "AND", 0, 0))
    db.execute("INSERT INTO fault_intervals (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)"
               " VALUES (?,?,?,?,?,?)", (1, BASE, BASE + P, 0, BASE, "[]"))
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    resp = client.get("/api/consistency-check", params=_params(end="2026-09-23T00:02:00Z", threshold=1))
    assert resp.status_code == 200
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults
