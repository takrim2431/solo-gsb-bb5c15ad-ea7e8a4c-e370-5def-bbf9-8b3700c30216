"""End-to-end tests for condition reset thresholds: persistence round-trip,
save/preview/export/history-read consistency, automatic re-evaluation and the
schema migration for databases created before the column existed.
"""

import json
import os
import tempfile

import pytest

from app import db, services
from app.engine import Condition, to_epoch


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


def _import_series(values, device="d", metric="m"):
    rows = [
        {
            "device": device,
            "metric": metric,
            "ts": f"2026-09-23T00:{m:02d}:00Z",
            "value": v,
        }
        for m, v in enumerate(values)
    ]
    return services.import_events("\n".join(json.dumps(r) for r in rows))


def _rule_payload(name="复位规则", duration=0, op=">", threshold=80, reset=75):
    cond = {"device": "d", "metric": "m", "op": op, "threshold": threshold}
    if reset is not None:
        cond["reset_threshold"] = reset
    return {"name": name, "logic": "AND", "duration": duration, "conditions": [cond]}


# --------------------------------------------------------------------------- #
# engine wiring through the services layer
# --------------------------------------------------------------------------- #


def test_reset_threshold_recomputes_stored_faults(scratch_db):
    # trigger at 1, band dip at 2, trigger at 3, disarm at 4; without reset
    # the dip fragments the truth, with reset it merges into one fault
    _import_series([70, 85, 77, 85, 70, 70, 70])
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("复位规则", "AND", 0, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, reset_threshold, ord)"
        " VALUES (?,?,?,?,?,?,?)",
        (rule_id, "d", "m", ">", 80, None, 0),
    )
    services.evaluate_all_rules()
    plain = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in plain] == [
        (at(1), at(2)), (at(3), at(4)),
    ]

    db.execute(
        "UPDATE rule_conditions SET reset_threshold = ? WHERE rule_id = ?",
        (75, rule_id),
    )
    services.evaluate_all_rules()
    latched = services.query_faults(None, None)
    assert [(f["start"], f["end"]) for f in latched] == [(at(1), at(4))]
    cond = latched[0]["evidence"][0]["condition"]
    assert cond["reset_threshold"] == 75.0
    assert [ev["ts"] for ev in latched[0]["evidence"][0]["events"]] == [
        at(1), at(2), at(3),
    ]


def test_clearing_reset_threshold_restores_plain_comparison(scratch_db):
    _import_series([70, 85, 77, 85, 70])
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("复位规则", "AND", 0, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, reset_threshold, ord)"
        " VALUES (?,?,?,?,?,?,?)",
        (rule_id, "d", "m", ">", 80, 75, 0),
    )
    services.evaluate_all_rules()
    assert [(f["start"], f["end"]) for f in services.query_faults(None, None)] == [
        (at(1), at(4))
    ]
    db.execute(
        "UPDATE rule_conditions SET reset_threshold = NULL WHERE rule_id = ?",
        (rule_id,),
    )
    services.evaluate_all_rules()
    assert [(f["start"], f["end"]) for f in services.query_faults(None, None)] == [
        (at(1), at(2)),
        (at(3), at(4)),
    ]
    # history read carries the cleared (null) threshold
    stored = db.fetch_rule(rule_id)
    assert stored["conditions"][0]["reset_threshold"] is None


def test_preview_uses_draft_reset_threshold_without_writing(scratch_db):
    _import_series([70, 85, 77, 85, 70])
    # baseline: no rule -> empty; draft with reset merges the dip
    result = services.preview_rule(None, _rule_payload())
    assert result["mode"] == "create"
    assert [(s["start"], s["end"]) for s in result["draft"]["faults"]] == [
        (at(1), at(4))
    ]
    # nothing was written
    assert db.query_one("SELECT COUNT(*) AS c FROM rules")["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM fault_intervals")["c"] == 0


def test_preview_edit_compares_plain_vs_latched(scratch_db):
    _import_series([70, 85, 77, 85, 70])
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("复位规则", "AND", 0, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, reset_threshold, ord)"
        " VALUES (?,?,?,?,?,?,?)",
        (rule_id, "d", "m", ">", 80, None, 0),
    )
    services.evaluate_all_rules()
    result = services.preview_rule(rule_id, _rule_payload(reset=75))
    by_kind: dict = {}
    for seg in result["diff"]["segments"]:
        by_kind.setdefault(seg["kind"], []).append(seg)
    # the dip minute [2,3) is newly covered by the latched draft
    assert [(s["start"], s["end"]) for s in by_kind["added"]] == [(at(2), at(3))]
    assert "removed" not in by_kind
    # stored rule is untouched
    assert db.query_one(
        "SELECT reset_threshold AS r FROM rule_conditions WHERE rule_id = ?",
        (rule_id,),
    )["r"] is None


# --------------------------------------------------------------------------- #
# migration for a database created before the column
# --------------------------------------------------------------------------- #


def test_schema_migration_adds_reset_threshold_to_old_db(scratch_db):
    # create the rule row first (conditions reference it via foreign key)
    db.execute(
        "INSERT INTO rules (id, name, logic, duration, created_at) VALUES (1,'r','AND',0,0)"
    )
    # simulate a pre-feature database: drop the column by recreating the table
    conn = db.get_conn()
    conn.execute("ALTER TABLE rule_conditions RENAME TO rule_conditions_old")
    conn.execute(
        """
        CREATE TABLE rule_conditions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            device TEXT NOT NULL, metric TEXT NOT NULL,
            op TEXT NOT NULL, threshold REAL NOT NULL, ord INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (1,'d','m','>',80,0)"
    )
    conn.commit()
    # a fresh process would call init_db() again on the old-shaped schema
    db.init_db()
    row = db.query_one("SELECT * FROM rule_conditions WHERE id = 1")
    assert row["reset_threshold"] is None
    # and the old condition still evaluates as a plain predicate
    _import_series([70, 85, 77, 85, 70])
    services.evaluate_all_rules()
    assert [(f["start"], f["end"]) for f in services.query_faults(None, None)] == [
        (at(1), at(2)),
        (at(3), at(4)),
    ]


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_api_rule_crud_roundtrip_with_reset(client):
    r = client.post("/api/rules", json=_rule_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["conditions"][0]["reset_threshold"] == 75.0
    rule_id = body["id"]
    # history read is identical to the saved payload
    got = client.get(f"/api/rules/{rule_id}").json()
    assert got["conditions"][0] == body["conditions"][0]
    assert client.get("/api/rules").json()[0]["conditions"][0]["reset_threshold"] == 75.0
    # clear the reset threshold on update
    updated = _rule_payload(reset=None)
    r = client.put(f"/api/rules/{rule_id}", json=updated)
    assert r.status_code == 200
    assert r.json()["conditions"][0]["reset_threshold"] is None
    # set it again
    r = client.put(f"/api/rules/{rule_id}", json=_rule_payload(reset=70))
    assert r.json()["conditions"][0]["reset_threshold"] == 70.0


def test_api_rejects_invalid_reset_threshold(client):
    # equality operator with reset
    r = client.post(
        "/api/rules",
        json=_rule_payload(op="==", threshold=80, reset=75),
    )
    assert r.status_code == 400 and "仅支持" in r.json()["detail"]
    # high trigger reset not below trigger
    r = client.post("/api/rules", json=_rule_payload(op=">", threshold=80, reset=80))
    assert r.status_code == 400 and "低于" in r.json()["detail"]
    r = client.post("/api/rules", json=_rule_payload(op=">=", threshold=80, reset=81))
    assert r.status_code == 400
    # low trigger reset not above trigger
    r = client.post("/api/rules", json=_rule_payload(op="<", threshold=30, reset=30))
    assert r.status_code == 400 and "高于" in r.json()["detail"]
    r = client.post("/api/rules", json=_rule_payload(op="<=", threshold=30, reset=29))
    assert r.status_code == 400
    # !=" never supports it
    r = client.post(
        "/api/rules",
        json=_rule_payload(op="!=", threshold=30, reset=40),
    )
    assert r.status_code == 400
    # a low-side reset on a high op and vice-versa
    r = client.post("/api/rules", json=_rule_payload(op=">", threshold=80, reset=79))
    assert r.status_code == 200
    r = client.post("/api/rules", json=_rule_payload(op="<", threshold=30, reset=31))
    assert r.status_code == 200


def test_api_evaluation_and_export_carry_reset_threshold(client):
    client.post("/api/import/text", json={
        "text": "\n".join(
            json.dumps({
                "device": "d", "metric": "m",
                "ts": f"2026-09-23T00:{m:02d}:00Z", "value": v,
            })
            for m, v in enumerate([70, 85, 77, 85, 70])
        )
    })
    client.post("/api/rules", json=_rule_payload())
    client.post("/api/evaluate")
    faults = client.get("/api/faults").json()
    assert [(f["start"], f["end"]) for f in faults] == [(at(1), at(4))]
    assert faults[0]["evidence"][0]["condition"]["reset_threshold"] == 75.0

    exported = client.get("/api/export").json()
    assert exported["rules"][0]["conditions"][0]["reset_threshold"] == 75.0
    assert exported["fault_intervals"][0]["evidence"][0]["condition"]["reset_threshold"] == 75.0

    tl = client.get("/api/timeline").json()
    assert tl["faults"][0]["evidence"][0]["condition"]["reset_threshold"] == 75.0


def test_demo_endpoint_rules_without_reset_key_still_work(client):
    # The bundled DEMO_RULES dicts do not carry reset_threshold; inserting
    # them must default to NULL instead of raising KeyError.
    r = client.post("/api/demo")
    assert r.status_code == 200, r.text
    assert r.json()["rules_created"] == 3
    for rule in client.get("/api/rules").json():
        for cond in rule["conditions"]:
            assert cond["reset_threshold"] is None
