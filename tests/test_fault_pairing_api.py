"""End-to-end tests for the rule fault sequence pairing API.

The pairing query is read-only: it reads the stored confirmed fault intervals
of two different rules (only those whose start lies inside the half-open UTC
window), pairs cause faults with strictly-later effect faults within the max
lag, and never re-evaluates rules or writes anything.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import to_epoch

BASE = to_epoch("2026-09-23T00:00:00Z")


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


def _create_rule(name, device, metric, threshold=80, duration=0):
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        (name, "AND", duration, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, device, metric, ">=", threshold, 0),
    )
    return rule_id


def _high_low(device, metric, spans):
    """Events producing one [start, end) fault per (start, end) span."""
    rows = []
    for start, end in spans:
        rows.append({"device": device, "metric": metric, "ts": BASE + start, "value": 90})
        rows.append({"device": device, "metric": metric, "ts": BASE + end, "value": 10})
    services.import_events("\n".join(json.dumps(r) for r in rows), reevaluate=False)


@pytest.fixture()
def evaluated_rules(scratch_db):
    # Cause rule R1 faults: [100,200), [1000,1100).
    # Effect rule R2 faults: [150,250), [400,500), [1200,1300).
    _high_low("d", "m", [(100, 200), (1000, 1100)])
    _high_low("d2", "m2", [(150, 250), (400, 500), (1200, 1300)])
    r1 = _create_rule("前因规则", "d", "m")
    r2 = _create_rule("后续规则", "d2", "m2")
    services.evaluate_all_rules()
    return r1, r2


def _fault_ids_by_rule(rule_id):
    rows = db.query(
        "SELECT id, start_ts FROM fault_intervals WHERE rule_id = ? ORDER BY start_ts",
        (rule_id,),
    )
    return {int(r["start_ts"] - BASE): r["id"] for r in rows}


# --------------------------------------------------------------------------- #
# service-level behavior
# --------------------------------------------------------------------------- #


def test_service_pairs_and_reports(evaluated_rules):
    r1, r2 = evaluated_rules
    cause_ids = _fault_ids_by_rule(r1)
    effect_ids = _fault_ids_by_rule(r2)
    result = services.query_fault_pairing(r1, r2, BASE, BASE + 2000, 300)
    assert result["cause_rule"]["id"] == r1
    assert result["effect_rule"]["id"] == r2
    assert result["cause_faults_in_range"] == 2
    assert result["effect_faults_in_range"] == 3
    assert result["pair_count"] == 2
    assert [(p["cause_fault_id"], p["effect_fault_id"]) for p in result["pairs"]] == [
        (cause_ids[100], effect_ids[150]),
        (cause_ids[1000], effect_ids[1200]),
    ]
    assert [p["lag_seconds"] for p in result["pairs"]] == [50.0, 200.0]
    assert result["total_lag_seconds"] == pytest.approx(250)
    assert result["cause_pair_rate"] == pytest.approx(1.0)
    assert result["effect_pair_rate"] == pytest.approx(2 / 3)
    assert result["unmatched_cause"] == []
    assert [f["fault_id"] for f in result["unmatched_effect"]] == [effect_ids[400]]
    assert result["unmatched_effect"][0]["start"] == BASE + 400


def test_service_only_faults_starting_in_range(evaluated_rules):
    r1, r2 = evaluated_rules
    cause_ids = _fault_ids_by_rule(r1)
    effect_ids = _fault_ids_by_rule(r2)
    # Window [500, 2000): the cause fault starting at 100 and the effect
    # faults starting at 150/400 are excluded even though some reach inside.
    result = services.query_fault_pairing(r1, r2, BASE + 500, BASE + 2000, 300)
    assert result["cause_faults_in_range"] == 1
    assert result["effect_faults_in_range"] == 1
    assert [(p["cause_fault_id"], p["effect_fault_id"]) for p in result["pairs"]] == [
        (cause_ids[1000], effect_ids[1200])
    ]
    # A window starting exactly at a fault start includes that fault.
    result = services.query_fault_pairing(r1, r2, BASE + 100, BASE + 500, 300)
    assert result["cause_faults_in_range"] == 1
    assert result["effect_faults_in_range"] == 2
    assert result["pair_count"] == 1


def test_service_max_lag_gates_pairs(evaluated_rules):
    r1, r2 = evaluated_rules
    # 60s lag allows the first pair (50s) but not the second (200s).
    result = services.query_fault_pairing(r1, r2, BASE, BASE + 2000, 60)
    assert result["pair_count"] == 1
    assert result["pairs"][0]["lag_seconds"] == pytest.approx(50)
    assert len(result["unmatched_cause"]) == 1
    assert len(result["unmatched_effect"]) == 2


def test_service_bad_args(evaluated_rules, scratch_db):
    r1, r2 = evaluated_rules
    with pytest.raises(ValueError):
        services.query_fault_pairing(r1, r1, BASE, BASE + 2000, 300)
    with pytest.raises(KeyError):
        services.query_fault_pairing(r1, 999, BASE, BASE + 2000, 300)
    with pytest.raises(KeyError):
        services.query_fault_pairing(999, r2, BASE, BASE + 2000, 300)
    with pytest.raises(ValueError):
        services.query_fault_pairing(r1, r2, BASE + 2000, BASE, 300)
    with pytest.raises(ValueError):
        services.query_fault_pairing(r1, r2, BASE, BASE + 2000, 0)
    with pytest.raises(ValueError):
        services.query_fault_pairing(r1, r2, BASE, BASE + 2000, float("inf"))


def test_service_is_read_only(evaluated_rules):
    r1, r2 = evaluated_rules
    before = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    services.query_fault_pairing(r1, r2, BASE, BASE + 2000, 300)
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before == 5


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_api_pairing_ok(evaluated_rules, client):
    r1, r2 = evaluated_rules
    resp = client.get(
        "/api/fault-pairing",
        params={
            "cause_rule": r1,
            "effect_rule": r2,
            "start": "2026-09-23T00:00:00Z",
            "end": "2026-09-23T00:33:20Z",
            "max_lag_seconds": 300,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cause_rule"]["name"] == "前因规则"
    assert body["effect_rule"]["name"] == "后续规则"
    assert body["start"] == BASE and body["end"] == BASE + 2000
    assert body["max_lag_seconds"] == 300.0
    assert body["pair_count"] == 2
    assert body["cause_pair_rate"] == pytest.approx(1.0)
    assert len(body["pairs"]) == 2
    assert len(body["unmatched_effect"]) == 1


def test_api_swapped_roles(evaluated_rules, client):
    # swapping cause/effect looks at the inverse direction: R2's earlier
    # faults cannot follow R1's later ones
    r1, r2 = evaluated_rules
    resp = client.get(
        "/api/fault-pairing",
        params={"cause_rule": r2, "effect_rule": r1, "start": BASE, "end": BASE + 2000,
                "max_lag_seconds": 300},
    )
    assert resp.status_code == 200
    body = resp.json()
    # R2 faults start at 150/400/1200, R1 at 100/1000: only 1200->? nothing
    # follows within 300s (R1's 1000 precedes it), and 150/400 -> 1000 is
    # beyond the lag; only 400->1000? 600s > 300. 150->1000 too far.
    assert body["pair_count"] == 0
    assert body["cause_faults_in_range"] == 3
    assert body["effect_faults_in_range"] == 2


def test_api_same_rule_400(evaluated_rules, client):
    r1, _ = evaluated_rules
    resp = client.get(
        "/api/fault-pairing",
        params={"cause_rule": r1, "effect_rule": r1, "start": BASE, "end": BASE + 2000,
                "max_lag_seconds": 300},
    )
    assert resp.status_code == 400


def test_api_unknown_rule_404(evaluated_rules, client):
    r1, _ = evaluated_rules
    resp = client.get(
        "/api/fault-pairing",
        params={"cause_rule": r1, "effect_rule": 999, "start": BASE, "end": BASE + 2000,
                "max_lag_seconds": 300},
    )
    assert resp.status_code == 404


def test_api_invalid_window_400(evaluated_rules, client):
    r1, r2 = evaluated_rules
    resp = client.get(
        "/api/fault-pairing",
        params={"cause_rule": r1, "effect_rule": r2,
                "start": "2026-09-24T00:00:00Z", "end": "2026-09-23T00:00:00Z",
                "max_lag_seconds": 300},
    )
    assert resp.status_code == 400


def test_api_bad_max_lag(evaluated_rules, client):
    r1, r2 = evaluated_rules
    base = {"cause_rule": r1, "effect_rule": r2, "start": BASE, "end": BASE + 2000}
    assert client.get("/api/fault-pairing", params={**base, "max_lag_seconds": 0}).status_code == 422
    assert client.get("/api/fault-pairing", params={**base, "max_lag_seconds": -5}).status_code == 422
    assert client.get("/api/fault-pairing", params={**base, "max_lag_seconds": "inf"}).status_code == 400
    assert client.get("/api/fault-pairing", params={**base, "max_lag_seconds": "abc"}).status_code == 422


def test_api_missing_params_422(client):
    assert client.get("/api/fault-pairing").status_code == 422
    assert client.get(
        "/api/fault-pairing", params={"cause_rule": 1, "effect_rule": 2}
    ).status_code == 422


def test_api_is_read_only(evaluated_rules, client):
    r1, r2 = evaluated_rules
    before_faults = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    before_events = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
    client.get(
        "/api/fault-pairing",
        params={"cause_rule": r1, "effect_rule": r2, "start": BASE, "end": BASE + 2000,
                "max_lag_seconds": 300},
    )
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == before_faults
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == before_events
