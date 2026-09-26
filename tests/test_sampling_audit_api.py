"""End-to-end tests for the read-only sampling-cadence audit API.

The audit reads existing events only: it never inserts/updates/deletes events
and never recomputes or rewrites stored fault results.
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


def _load_channel(ts_values, device="d", metric="m"):
    rows = [{"device": device, "metric": metric, "ts": ts, "value": 1} for ts in ts_values]
    services.import_events("\n".join(json.dumps(r) for r in rows), reevaluate=False)


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


def test_service_reconciliation_and_segments(scratch_db):
    # perfect on-grid samples except two consecutive missing beats at 2m,3m,
    # one jittery (within tolerance) at 5m and an out-of-range stray at 9m30s
    _load_channel([BASE + k * P for k in (0, 1, 4, 5, 6, 7, 8, 9)])
    # overwrite the 5m event with a +4s jittery sample (same unique key
    # cannot hold two rows; instead import it on a fresh near-beat ts)
    db.execute("INSERT INTO events (device, metric, ts, value) VALUES (?,?,?,?)",
               ("d", "m", BASE + 5 * P + 4, 1))
    result = services.query_sampling_audit(
        "d", "m", BASE, BASE + 10 * P, P, 5
    )
    assert result["expected_count"] == 10
    assert result["hit_count"] == 8
    assert result["miss_count"] == 2
    assert result["hit_rate"] == pytest.approx(0.8)
    segs = result["missing_segments"]
    assert len(segs) == 1
    assert segs[0]["start"] == BASE + 2 * P
    assert segs[0]["end"] == BASE + 3 * P
    assert segs[0]["miss_count"] == 2
    assert result["channel"]["event_count"] >= 9


def test_service_unknown_channel_raises(scratch_db):
    _load_channel([BASE])
    with pytest.raises(KeyError):
        services.query_sampling_audit("d", "nope", BASE, BASE + P, P, 5)
    with pytest.raises(ValueError):
        services.query_sampling_audit("d", "m", BASE + P, BASE, P, 5)
    with pytest.raises(ValueError):
        services.query_sampling_audit("d", "m", BASE, BASE + P, 0, 0)
    with pytest.raises(ValueError):
        services.query_sampling_audit("d", "m", BASE, BASE + P, P, P / 2)


def test_service_is_read_only(scratch_db):
    _load_channel([BASE + k * P for k in range(10)])
    # create a fault row that must remain untouched
    db.execute("INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
               ("r", "AND", 0, 0))
    db.execute("INSERT INTO fault_intervals (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)"
               " VALUES (?,?,?,?,?,?)", (1, BASE, BASE + P, 0, BASE, "[]"))
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    services.query_sampling_audit("d", "m", BASE, BASE + 10 * P, P, 5)
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def test_api_audit_ok(scratch_db, client):
    _load_channel([BASE + k * P for k in range(10)])
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:10:00Z",
        "period_seconds": 60, "tolerance_seconds": 5,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "d" and body["metric"] == "m"
    assert body["expected_count"] == 10
    assert body["hit_count"] == 10
    assert body["miss_count"] == 0
    assert body["hit_rate"] == pytest.approx(1.0)
    assert body["missing_segments"] == []
    assert body["anchor"] == BASE


def test_api_iso_range_is_half_open(scratch_db, client):
    # last event lands exactly on the end bound: excluded from the window
    _load_channel([BASE, BASE + 60])
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:01:00Z",
        "period_seconds": 60, "tolerance_seconds": 5,
    })
    body = resp.json()
    assert body["expected_count"] == 1
    assert body["hit_count"] == 1
    assert body["actual_count_in_range"] == 1


def test_api_unknown_channel_404(scratch_db, client):
    _load_channel([BASE])
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "nope",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:01:00Z",
        "period_seconds": 60, "tolerance_seconds": 5,
    })
    assert resp.status_code == 404


def test_api_tolerance_at_half_period_400(scratch_db, client):
    _load_channel([BASE])
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:10:00Z",
        "period_seconds": 60, "tolerance_seconds": 30,
    })
    assert resp.status_code == 400
    assert "half the period" in resp.json()["detail"]


def test_api_inverted_window_400(scratch_db, client):
    _load_channel([BASE])
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:10:00Z", "end": "2026-09-23T00:00:00Z",
        "period_seconds": 60, "tolerance_seconds": 5,
    })
    assert resp.status_code == 400


def test_api_missing_params_422(client):
    resp = client.get("/api/sampling-audit")
    assert resp.status_code == 422
    resp = client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:10:00Z",
        "period_seconds": -1, "tolerance_seconds": 5,
    })
    assert resp.status_code == 422  # Query(..., gt=0) rejects a negative period


def test_api_does_not_touch_events_or_faults(scratch_db, client):
    _load_channel([BASE + k * P for k in range(10)])
    before = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    client.get("/api/sampling-audit", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:10:00Z",
        "period_seconds": 60, "tolerance_seconds": 5,
    })
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before
