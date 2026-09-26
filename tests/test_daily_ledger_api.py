"""End-to-end tests for the rule-combination fault daily ledger API.

The ledger is read-only: it reads the stored confirmed fault intervals,
merges them across the selected rules as half-open intervals and splits at
UTC midnights.  It never re-evaluates rules or writes anything.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import to_epoch

DAY = 86400.0
BASE = to_epoch("2026-09-23T00:00:00Z")


def t(hour, day_offset=0, minute=0):
    return BASE + day_offset * DAY + hour * 3600 + minute * 60


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


@pytest.fixture()
def evaluated_rules(scratch_db):
    # Rule R1 on d/m: high 23:00 day0 -> 01:00 day1 (2h across midnight).
    rows = [
        {"device": "d", "metric": "m", "ts": t(23), "value": 90},
        {"device": "d", "metric": "m", "ts": t(1, 1), "value": 10},
        # Rule R2 on d2/m2: high 23:20 -> 23:40 day0, fully inside R1's span.
        {"device": "d2", "metric": "m2", "ts": t(23, 0, 20), "value": 90},
        {"device": "d2", "metric": "m2", "ts": t(23, 0, 40), "value": 10},
    ]
    services.import_events("\n".join(json.dumps(r) for r in rows), reevaluate=False)
    r1 = _create_rule("跨日规则", "d", "m")
    r2 = _create_rule("同日重叠规则", "d2", "m2")
    services.evaluate_all_rules()
    return r1, r2


# --------------------------------------------------------------------------- #
# service-level behavior
# --------------------------------------------------------------------------- #


def test_service_merges_dedupes_and_splits(evaluated_rules):
    r1, r2 = evaluated_rules
    result = services.query_fault_daily_ledger([r1, r2], BASE, BASE + 2 * DAY)
    assert [d["date"] for d in result["days"]] == ["2026-09-23", "2026-09-24"]
    d0, d1 = result["days"]
    # 23:00..24:00 on day0 and 00:00..01:00 on day1; R2's 20min overlap is
    # merged into R1's span and never double counted.
    assert d0["fault_seconds"] == 3600.0
    assert (d0["new_segments"], d0["continuing_segments"]) == (1, 0)
    assert d1["fault_seconds"] == 3600.0
    assert (d1["new_segments"], d1["continuing_segments"]) == (0, 1)
    assert result["merged_segments"] == [[t(23), t(1, 1)]]
    assert result["totals"]["fault_seconds"] == 7200.0
    assert result["totals"]["coverage"] == pytest.approx(7200.0 / (2 * DAY))
    assert {r["name"] for r in result["rules"]} == {"跨日规则", "同日重叠规则"}


def test_service_one_rule_inside_other(evaluated_rules):
    _r1, r2 = evaluated_rules
    result = services.query_fault_daily_ledger([r2], BASE, BASE + 2 * DAY)
    d0 = result["days"][0]
    assert d0["fault_seconds"] == 1200.0  # 23:20..23:40
    assert d0["new_segments"] == 1
    assert result["days"][1]["fault_seconds"] == 0.0


def test_service_partial_window_denominators(evaluated_rules):
    # 23:30 day0 -> 00:30 day1: both day pieces are 1800s and fully faulty.
    start, end = t(23, 0, 30), t(0, 1, 30)
    result = services.query_fault_daily_ledger([evaluated_rules[0]], start, end)
    d0, d1 = result["days"]
    assert d0["queried_seconds"] == 1800.0 and d0["fault_seconds"] == 1800.0
    assert d0["new_segments"] == 1
    assert d1["queried_seconds"] == 1800.0 and d1["fault_seconds"] == 1800.0
    assert d1["continuing_segments"] == 1
    assert d0["coverage"] == pytest.approx(1.0)


def test_service_deduplicates_rule_ids(evaluated_rules):
    r1, r2 = evaluated_rules
    result = services.query_fault_daily_ledger([r1, r1, r2], BASE, BASE + DAY)
    assert [r["id"] for r in result["rules"]] == [r1, r2]


def test_service_unknown_rule_raises(scratch_db):
    with pytest.raises(KeyError):
        services.query_fault_daily_ledger([999], BASE, BASE + DAY)
    with pytest.raises(ValueError):
        services.query_fault_daily_ledger([], BASE, BASE + DAY)
    with pytest.raises(ValueError):
        services.query_fault_daily_ledger([1], BASE + DAY, BASE)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def test_api_ledger_ok(evaluated_rules, client):
    r1, r2 = evaluated_rules
    resp = client.get(
        "/api/fault-daily-ledger",
        params=[
            ("rules", r1),
            ("rules", r2),
            ("start", "2026-09-23T00:00:00Z"),
            ("end", "2026-09-25T00:00:00Z"),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["start"] == BASE and body["end"] == BASE + 2 * DAY
    assert [d["date"] for d in body["days"]] == ["2026-09-23", "2026-09-24"]
    assert body["days"][0]["new_segments"] == 1
    assert body["days"][1]["continuing_segments"] == 1


def test_api_unknown_rule_404(client):
    resp = client.get(
        "/api/fault-daily-ledger",
        params={"rules": 999, "start": "2026-09-23T00:00:00Z", "end": "2026-09-24T00:00:00Z"},
    )
    assert resp.status_code == 404


def test_api_invalid_window_400(evaluated_rules, client):
    r1, _ = evaluated_rules
    resp = client.get(
        "/api/fault-daily-ledger",
        params={"rules": r1, "start": "2026-09-24T00:00:00Z", "end": "2026-09-23T00:00:00Z"},
    )
    assert resp.status_code == 400


def test_api_missing_params_422(client):
    assert client.get("/api/fault-daily-ledger").status_code == 422
    assert client.get(
        "/api/fault-daily-ledger",
        params={"start": "2026-09-23T00:00:00Z", "end": "2026-09-24T00:00:00Z"},
    ).status_code == 422


def test_api_ledger_is_read_only(evaluated_rules, client):
    r1, r2 = evaluated_rules
    before = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    client.get(
        "/api/fault-daily-ledger",
        params={"rules": [r1, r2], "start": "2026-09-23T00:00:00Z", "end": "2026-09-25T00:00:00Z"},
    )
    after = db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"]
    assert before == after == 2
