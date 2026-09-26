"""End-to-end tests for the read-only single-channel mutation recovery API.

The diagnostic reads the current events and the channel's configured TTL:
it never inserts/updates/deletes events, never writes fault results and
never triggers rule re-evaluation.
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


def _load(ts_values, device="d", metric="m"):
    payload = [{"device": device, "metric": metric, "ts": ts, "value": v} for ts, v in ts_values]
    services.import_events("\n".join(json.dumps(r) for r in payload), reevaluate=False)


def _series(values, step=P):
    return [(BASE + i * step, v) for i, v in enumerate(values)]


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


def test_service_recovered_segment(scratch_db):
    _load(_series([10, 30, 10]))
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 3 * P, 0.1, 1.0, 300
    )
    assert result["segment_count"] == 1
    assert result["recovered_count"] == 1
    seg = result["segments"][0]
    assert seg["recovered"] is True
    assert seg["baseline_sample"]["ts"] == BASE
    assert seg["start_sample"]["ts"] == BASE + P
    assert seg["end_sample"]["ts"] == BASE + 2 * P
    assert seg["jump_rate"] == pytest.approx(20 / P)
    assert seg["max_deviation"] == pytest.approx(20)
    assert seg["recovery_seconds"] == pytest.approx(P)
    assert seg["unrecovered_reason"] is None
    assert result["channel"]["ttl_seconds"] is None
    assert result["channel"]["event_count"] == 3


def test_service_pre_window_baseline(scratch_db):
    _load([(BASE - 30, 10.0)] + _series([30, 10]))
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 2 * P, 0.1, 1.0, 300
    )
    seg = result["segments"][0]
    assert seg["baseline_sample"]["ts"] == BASE - 30
    assert seg["start_sample"]["ts"] == BASE
    assert seg["recovered"] is True


def test_service_ttl_cuts_across_gap(scratch_db):
    # two-minute spacing with a 60s TTL: every transition crosses an expiry
    # gap, so the big ±70 jumps must never open a segment (without the TTL
    # they would — the gap, not the rate, is what suppresses them)
    _load(_series([10, 80, 10, 80], step=120))
    services.set_channel_ttl("d", "m", 60.0)
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 4 * 120, 0.1, 1.0, 600
    )
    assert result["segments"] == []
    assert result["channel"]["ttl_seconds"] == 60.0

    # clearing the TTL restores contiguity and the jumps are detected again
    services.clear_channel_ttl("d", "m")
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 4 * 120, 0.1, 1.0, 600
    )
    assert result["segment_count"] == 2
    assert result["channel"]["ttl_seconds"] is None


def test_service_unrecovered_reasons(scratch_db):
    # observation expires inside the window, no return: observation_expired
    _load(_series([10, 40, 40]))
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 4 * P, 0.1, 1.0, 60
    )
    assert result["unrecovered_count"] == 1
    assert result["segments"][0]["unrecovered_reason"] == "observation_expired"

    # window ends while still waiting: range_end
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 3 * P, 0.1, 1.0, 600
    )
    assert result["segments"][0]["unrecovered_reason"] == "range_end"

    # TTL gap while waiting: validity_gap
    _load([(BASE + 5 * P, 10.0)])
    services.set_channel_ttl("d", "m", 90.0)
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 6 * P, 0.1, 1.0, 600
    )
    assert result["segments"][0]["unrecovered_reason"] == "validity_gap"
    assert result["segments"][0]["end_sample"]["ts"] == BASE + 2 * P


def test_service_attributed_samples_do_not_reopen(scratch_db):
    # 10 -> 40 -> 10 recovers at i=2; the i=2 sample must not open a segment
    _load(_series([10, 40, 10, 10, 40, 10]))
    result = services.query_mutation_recovery(
        "d", "m", BASE, BASE + 6 * P, 0.1, 1.0, 300
    )
    assert result["segment_count"] == 2
    assert [s["start_sample"]["ts"] for s in result["segments"]] == [
        BASE + P,
        BASE + 4 * P,
    ]


def test_service_unknown_channel_and_bad_args(scratch_db):
    _load(_series([1]))
    with pytest.raises(KeyError):
        services.query_mutation_recovery("ghost", "m", BASE, BASE + P, 0.1, 1.0, 300)
    with pytest.raises(ValueError):
        services.query_mutation_recovery("d", "m", BASE + P, BASE, 0.1, 1.0, 300)
    with pytest.raises(ValueError):
        services.query_mutation_recovery("d", "m", BASE, BASE + P, 0, 1.0, 300)
    with pytest.raises(ValueError):
        services.query_mutation_recovery("d", "m", BASE, BASE + P, -1, 1.0, 300)
    with pytest.raises(ValueError):
        services.query_mutation_recovery("d", "m", BASE, BASE + P, 0.1, -1, 300)
    with pytest.raises(ValueError):
        services.query_mutation_recovery("d", "m", BASE, BASE + P, 0.1, 1.0, 0)


def test_service_is_read_only(scratch_db):
    _load(_series([10, 40, 10]))
    db.execute("INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
               ("r", "AND", 0, 0))
    db.execute("INSERT INTO fault_intervals (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)"
               " VALUES (?,?,?,?,?,?)", (1, BASE, BASE + P, 0, BASE, "[]"))
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    services.query_mutation_recovery("d", "m", BASE, BASE + 3 * P, 0.1, 1.0, 300)
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults
    row = db.query_one("SELECT start_ts, end_ts FROM fault_intervals WHERE rule_id = 1")
    assert (row["start_ts"], row["end_ts"]) == (BASE, BASE + P)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _params(**over):
    params = {
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:03:00Z",
        "rate_threshold": 0.1,
        "regression_tolerance": 1,
        "observation_seconds": 300,
    }
    params.update(over)
    return params


def test_api_diagnosis_ok(scratch_db, client):
    _load(_series([10, 30, 10]))
    resp = client.get("/api/mutation-recovery", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["device"] == "d" and body["metric"] == "m"
    assert body["rate_threshold"] == pytest.approx(0.1)
    assert body["segment_count"] == 1
    seg = body["segments"][0]
    assert seg["recovered"] is True
    assert seg["recovery_seconds"] == pytest.approx(P)
    assert seg["start_sample"]["value"] == 30
    assert body["channel"] == {
        "device": "d", "metric": "m", "ttl_seconds": None,
        "event_count": 3, "min_ts": BASE, "max_ts": BASE + 2 * P,
    }


def test_api_epoch_range(scratch_db, client):
    _load(_series([10, 30, 10]))
    resp = client.get("/api/mutation-recovery", params=_params(start=BASE, end=BASE + 3 * P))
    assert resp.status_code == 200
    assert resp.json()["segment_count"] == 1


def test_api_unknown_channel_404(scratch_db, client):
    _load(_series([1]))
    resp = client.get("/api/mutation-recovery", params=_params(metric="ghost"))
    assert resp.status_code == 404


def test_api_inverted_window_400(scratch_db, client):
    _load(_series([1]))
    resp = client.get("/api/mutation-recovery", params=_params(
        start="2026-09-23T00:03:00Z", end="2026-09-23T00:00:00Z",
    ))
    assert resp.status_code == 400


def test_api_bad_parameters_rejected(scratch_db, client):
    _load(_series([1]))
    assert client.get("/api/mutation-recovery", params=_params(rate_threshold=0)).status_code == 422
    assert client.get("/api/mutation-recovery", params=_params(rate_threshold=-1)).status_code == 422
    assert client.get("/api/mutation-recovery", params=_params(rate_threshold="nan")).status_code == 422
    assert client.get("/api/mutation-recovery", params=_params(rate_threshold="inf")).status_code == 400
    assert client.get("/api/mutation-recovery", params=_params(regression_tolerance=-1)).status_code == 422
    assert client.get("/api/mutation-recovery", params=_params(observation_seconds=0)).status_code == 422
    assert client.get("/api/mutation-recovery", params=_params(observation_seconds="nan")).status_code == 422


def test_api_missing_params_422(client):
    assert client.get("/api/mutation-recovery").status_code == 422
    resp = client.get("/api/mutation-recovery", params={
        "device": "d", "metric": "m",
        "start": "2026-09-23T00:00:00Z", "end": "2026-09-23T00:03:00Z",
        "rate_threshold": 0.1,
    })
    assert resp.status_code == 422


def test_api_does_not_touch_events_or_faults(scratch_db, client):
    _load(_series([10, 40, 10]))
    db.execute("INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
               ("r", "AND", 0, 0))
    db.execute("INSERT INTO fault_intervals (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)"
               " VALUES (?,?,?,?,?,?)", (1, BASE, BASE + P, 0, BASE, "[]"))
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    resp = client.get("/api/mutation-recovery", params=_params())
    assert resp.status_code == 200
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults
