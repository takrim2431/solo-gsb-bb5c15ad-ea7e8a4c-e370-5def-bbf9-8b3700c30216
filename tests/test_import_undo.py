"""End-to-end tests for import-batch undo (revocation).

Covers pre-import snapshots (one per batch-unique key, last value still wins),
undo deleting new events / restoring overwritten events' value/raw/ownership,
"latest batch only" and "snapshot complete" guards, revoked batches, the
single-transaction rollback semantics, fault refresh after undo and snapshots
being wiped on reset.  The concurrency section pins the eligibility checks
(batch exists / not revoked / snapshot complete / still the latest live batch)
and the revoke itself into one serialized write transaction: a concurrent
import or duplicate undo loses with the usual 409 semantics instead of an
out-of-order revoke or a 500.
"""

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager

import pytest

from app import db, services
from app.engine import to_epoch


def at(minute: int, second: int = 0) -> float:
    return to_epoch(f"2026-09-23T00:{minute:02d}:{second:02d}Z")


def row(device, metric, minute, value):
    return {
        "device": device,
        "metric": metric,
        "ts": f"2026-09-23T00:{minute:02d}:00Z",
        "value": value,
    }


def import_jsonl(rows, **kwargs):
    return services.import_events("\n".join(json.dumps(r) for r in rows), **kwargs)


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


def _event(device, metric, minute):
    return db.query_one(
        "SELECT * FROM events WHERE device = ? AND metric = ? AND ts = ?",
        (device, metric, at(minute)),
    )


def _create_rule(threshold=80, duration=0, device="d", metric="m"):
    rule_id = db.execute(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
        ("撤销规则", "AND", duration, 0.0),
    ).lastrowid
    db.execute(
        "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
        " VALUES (?,?,?,?,?,?)",
        (rule_id, device, metric, ">=", threshold, 0),
    )
    return rule_id


# --------------------------------------------------------------------------- #
# snapshot behaviour during import
# --------------------------------------------------------------------------- #


def test_import_records_one_snapshot_per_batch_unique_key(scratch_db):
    # Same key appears three times in the batch: snapshot once, last value wins.
    result = import_jsonl([row("d", "m", 1, 10), row("d", "m", 1, 20), row("d", "m", 1, 30)])
    assert result["inserted"] == 1
    assert result["overwritten"] == 2
    batch = db.query_one("SELECT * FROM imports WHERE id = ?", (result["import_id"],))
    assert batch["snapshot_complete"] == 1
    snaps = db.query(
        "SELECT * FROM import_snapshots WHERE import_id = ?", (result["import_id"],)
    )
    assert len(snaps) == 1
    assert snaps[0]["event_id"] is None  # key did not exist before
    ev = _event("d", "m", 1)
    assert ev["value"] == 30  # last valid value wins
    assert ev["import_id"] == result["import_id"]


def test_snapshot_archives_overwritten_event_state(scratch_db):
    first = import_jsonl([row("d", "m", 1, 10), row("d", "m", 2, 11)], filename="a.jsonl")
    second = import_jsonl([row("d", "m", 1, 99)], filename="b.jsonl")
    snaps = db.query(
        "SELECT * FROM import_snapshots WHERE import_id = ?", (second["import_id"],)
    )
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap["event_id"] is not None
    assert snap["value"] == 10
    assert json.loads(snap["raw"])["value"] == 10
    assert snap["origin_import_id"] == first["import_id"]


def test_nested_metric_keys_are_snapshotted_independently(scratch_db):
    result = import_jsonl(
        [{"device": "d", "ts": "2026-09-23T00:01:00Z", "value": {"temp": 1, "vib": 2}}]
    )
    assert result["inserted"] == 2
    snaps = db.query(
        "SELECT * FROM import_snapshots WHERE import_id = ?", (result["import_id"],)
    )
    assert {s["metric"] for s in snaps} == {"temp", "vib"}


def test_skipped_rows_create_no_snapshot(scratch_db):
    result = import_jsonl(
        [
            row("d", "m", 1, 10),
            {"device": "d", "metric": "m", "ts": "not-a-time", "value": 1},
            {"device": "d", "metric": "m", "ts": "2026-09-23T00:03:00Z", "value": "x"},
        ]
    )
    assert result["skipped"] == 2
    snaps = db.query(
        "SELECT * FROM import_snapshots WHERE import_id = ?", (result["import_id"],)
    )
    assert len(snaps) == 1


# --------------------------------------------------------------------------- #
# undo service
# --------------------------------------------------------------------------- #


def test_undo_deletes_new_events(scratch_db):
    result = import_jsonl([row("d", "m", 1, 10), row("d", "m", 2, 11)], filename="a.jsonl")
    undone = services.undo_import(result["import_id"])
    assert undone["deleted_events"] == 2
    assert undone["restored_events"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0
    batch = db.query_one("SELECT * FROM imports WHERE id = ?", (result["import_id"],))
    assert batch["revoked_at"] is not None


def test_undo_restores_overwritten_value_raw_and_ownership(scratch_db):
    first = import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    second = import_jsonl([row("d", "m", 1, 99)], filename="b.jsonl")
    ev = _event("d", "m", 1)
    assert ev["value"] == 99 and ev["import_id"] == second["import_id"]

    services.undo_import(second["import_id"])
    ev = _event("d", "m", 1)
    assert ev["value"] == 10
    assert json.loads(ev["raw"])["value"] == 10
    assert ev["import_id"] == first["import_id"]


def test_undo_in_batch_keeps_only_first_encounter_snapshot(scratch_db):
    # Batch A writes v=10; batch B writes 20 then 30 on the same key.
    import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    b = import_jsonl([row("d", "m", 1, 20), row("d", "m", 1, 30)], filename="b.jsonl")
    assert _event("d", "m", 1)["value"] == 30
    services.undo_import(b["import_id"])
    # Restored to A's value, not to the intermediate 20.
    assert _event("d", "m", 1)["value"] == 10


def test_undo_mixed_new_and_overwritten_keys(scratch_db):
    import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    second = import_jsonl(
        [row("d", "m", 1, 99), row("d", "m", 2, 20), row("d", "x", 1, 1)],
        filename="b.jsonl",
    )
    result = services.undo_import(second["import_id"])
    assert result["deleted_events"] == 2
    assert result["restored_events"] == 1
    assert _event("d", "m", 1)["value"] == 10
    assert _event("d", "m", 2) is None
    assert _event("d", "x", 1) is None


def test_undo_then_new_batch_works_and_old_revoked_batch_refused(scratch_db):
    a = import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    services.undo_import(a["import_id"])
    b = import_jsonl([row("d", "m", 2, 20)], filename="b.jsonl")
    # A is already revoked; B is the live latest.
    with pytest.raises(services.UndoNotAllowed):
        services.undo_import(a["import_id"])
    services.undo_import(b["import_id"])
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0


def test_only_latest_live_batch_may_be_undone(scratch_db):
    import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    b = import_jsonl([row("d", "m", 2, 20)], filename="b.jsonl")
    with pytest.raises(services.UndoNotAllowed, match="最近一次"):
        services.undo_import(1)
    # Undoing B first unlocks A (LIFO).
    services.undo_import(b["import_id"])
    services.undo_import(1)
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0


def test_unknown_batch_raises_keyerror(scratch_db):
    with pytest.raises(KeyError):
        services.undo_import(999)


def test_legacy_batch_without_snapshot_is_refused(scratch_db):
    # Simulate a pre-feature batch row: snapshot_complete = 0, no snapshots.
    bid = db.execute(
        "INSERT INTO imports (filename, imported_at, total_rows, inserted_rows, snapshot_complete)"
        " VALUES (?,?,?,?,0)",
        ("old.jsonl", 0.0, 1, 1),
    ).lastrowid
    with pytest.raises(services.UndoNotAllowed, match="快照"):
        services.undo_import(bid)


def test_undo_refreshes_faults(scratch_db):
    _create_rule()
    # First batch below threshold, second pushes above and creates a fault.
    import_jsonl([row("d", "m", 1, 10), row("d", "m", 2, 10)], reevaluate=False)
    second = import_jsonl(
        [row("d", "m", 1, 90), row("d", "m", 2, 90)], filename="hot.jsonl"
    )
    assert second["evaluation"]["fault_intervals"] == 1
    undone = services.undo_import(second["import_id"])
    assert undone["evaluation"]["fault_intervals"] == 0
    assert services.query_faults(None, None) == []


def test_undo_rolls_back_on_failure(scratch_db):
    a = import_jsonl([row("d", "m", 1, 10), row("d", "m", 2, 11)])
    # Break the batch's invariant: one new-key event no longer belongs to the
    # batch, so the guarded DELETE matches 0 rows. The whole undo transaction
    # must roll back, leaving all events and the batch status untouched.
    db.execute("UPDATE events SET import_id = NULL WHERE device = 'd' AND metric = 'm' AND ts = ?", (at(1),))
    with pytest.raises(RuntimeError, match="snapshot consistency"):
        services.undo_import(a["import_id"], reevaluate=False)
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 2
    assert db.query_one("SELECT revoked_at FROM imports WHERE id = ?", (a["import_id"],))["revoked_at"] is None
    assert db.query_one("SELECT value FROM events WHERE device='d' AND metric='m' AND ts = ?", (at(2),))["value"] == 11


def test_reset_clears_snapshots(scratch_db):
    result = import_jsonl([row("d", "m", 1, 10)])
    assert db.query_one("SELECT COUNT(*) AS c FROM import_snapshots")["c"] == 1
    db.reset_all()
    assert db.query_one("SELECT COUNT(*) AS c FROM import_snapshots")["c"] == 0
    assert db.query_one("SELECT COUNT(*) AS c FROM imports")["c"] == 0


def test_list_import_batches_flags(scratch_db):
    a = import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")
    b = import_jsonl([row("d", "m", 2, 20)], filename="b.jsonl")
    batches = services.list_import_batches()
    assert [x["id"] for x in batches] == [b["import_id"], a["import_id"]]
    assert batches[0]["can_undo"] is True
    assert batches[0]["undo_reason"] is None
    assert batches[1]["can_undo"] is False
    assert "最近一次" in batches[1]["undo_reason"]
    services.undo_import(b["import_id"])
    batches = services.list_import_batches()
    by_id = {x["id"]: x for x in batches}
    assert by_id[b["import_id"]]["revoked"] is True
    assert by_id[a["import_id"]]["can_undo"] is True


def test_schema_migration_for_old_imports_table(scratch_db):
    # Rebuild the imports table in its pre-feature shape, like test_reset_api.
    conn = db.get_conn()
    conn.execute("ALTER TABLE imports RENAME TO imports_old")
    conn.execute(
        """
        CREATE TABLE imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            imported_at REAL NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            skipped_rows INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO imports (filename, imported_at, total_rows) VALUES ('old.jsonl', 0, 1)"
    )
    conn.commit()
    db.init_db()
    row = db.query_one("SELECT * FROM imports WHERE id = 1")
    assert row["snapshot_complete"] == 0
    assert row["revoked_at"] is None
    assert row["overwritten_rows"] == 0
    # the new snapshot table exists and old batches are explicitly refused
    db.query("SELECT * FROM import_snapshots")
    with pytest.raises(services.UndoNotAllowed):
        services.undo_import(1)


# --------------------------------------------------------------------------- #
# concurrency: eligibility checks and the revoke share one write transaction
# --------------------------------------------------------------------------- #


def _blocking_transaction(entered, release):
    """Wrap db.transaction so the FIRST entered transaction blocks (holding
    the write lock) until released; every later transaction passes through."""
    armed = threading.Event()
    armed.set()
    real_transaction = db.transaction

    @contextmanager
    def blocking_transaction():
        if armed.is_set():
            armed.clear()
            with real_transaction() as tx:
                entered.set()
                assert release.wait(15), "test harness: transaction never released"
                yield tx
        else:
            with real_transaction() as tx:
                yield tx

    return blocking_transaction


def test_undo_rechecks_latest_after_concurrent_import(scratch_db, monkeypatch):
    """The reported race: the undo of batch A is checked while a concurrent
    import is committing batch B.  The undo must re-check inside the
    serialized write transaction, refuse with UndoNotAllowed (409 semantics)
    and leave batch A untouched — never revoke A out of order beneath B."""
    a = import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")

    # The concurrent import blocks mid-transaction (holding the write lock)
    # until the undo request has started and queued behind the lock.
    entered = threading.Event()
    release = threading.Event()
    armed = threading.Event()
    armed.set()
    real_snapshot = db.snapshot_import_key

    def blocking_snapshot(*args, **kwargs):
        if armed.is_set():
            armed.clear()
            entered.set()
            assert release.wait(15), "test harness: import never released"
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(db, "snapshot_import_key", blocking_snapshot)

    import_done = threading.Event()

    def run_import():
        services.import_events(json.dumps(row("d", "m", 2, 20)), filename="b.jsonl")
        import_done.set()

    ti = threading.Thread(target=run_import)
    ti.start()
    assert entered.wait(5), "import never reached its write transaction"

    outcome = {}

    def run_undo():
        try:
            outcome["result"] = services.undo_import(a["import_id"])
        except services.UndoNotAllowed as exc:
            outcome["conflict"] = str(exc)

    tu = threading.Thread(target=run_undo)
    tu.start()
    time.sleep(0.3)
    # The undo cannot commit while the import holds the write lock.
    assert not import_done.is_set()
    release.set()  # the import commits first: B becomes the latest live batch
    ti.join(10)
    tu.join(10)
    assert import_done.is_set()

    # Refused as a normal conflict — not revoked out of order, never a 500.
    assert "result" not in outcome
    assert "最近一次" in outcome["conflict"]
    batch_a = db.query_one("SELECT * FROM imports WHERE id = ?", (a["import_id"],))
    assert batch_a["revoked_at"] is None
    assert _event("d", "m", 1)["value"] == 10
    assert _event("d", "m", 2)["value"] == 20


def test_concurrent_duplicate_undo_loser_gets_conflict(scratch_db, monkeypatch):
    """Two undos of the same batch race: exactly one commits, the loser
    re-checks inside its own serialized transaction and gets the usual
    UndoNotAllowed (409 '已撤销') — never a snapshot-consistency 500."""
    a = import_jsonl([row("d", "m", 1, 10), row("d", "m", 2, 11)], filename="a.jsonl")

    # The first undo blocks inside its write transaction (holding the write
    # lock) until the second undo has queued behind the lock.
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(db, "transaction", _blocking_transaction(entered, release))

    outcomes = [{}, {}]

    def run_undo(index):
        try:
            outcomes[index]["result"] = services.undo_import(a["import_id"])
        except services.UndoNotAllowed as exc:
            outcomes[index]["conflict"] = str(exc)

    t1 = threading.Thread(target=run_undo, args=(0,))
    t1.start()
    assert entered.wait(5), "first undo never reached its write transaction"
    t2 = threading.Thread(target=run_undo, args=(1,))
    t2.start()
    time.sleep(0.3)  # second undo queues behind the first one's write lock
    release.set()
    t1.join(10)
    t2.join(10)

    # Exactly one undo succeeded; the loser got the 409 conflict semantics.
    results = [o["result"] for o in outcomes if "result" in o]
    conflicts = [o["conflict"] for o in outcomes if "conflict" in o]
    assert len(results) == 1
    assert results[0]["deleted_events"] == 2
    assert len(conflicts) == 1
    assert "已撤销" in conflicts[0]
    # The batch was revoked exactly once; its events are gone.
    batch = db.query_one("SELECT * FROM imports WHERE id = ?", (a["import_id"],))
    assert batch["revoked_at"] is not None
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0


def test_import_serializes_behind_inflight_undo(scratch_db, monkeypatch):
    """The mirror interleaving: an import requested while an undo holds the
    write transaction must wait, then import on top of the revoked state and
    become the new latest live batch — no deadlock, no lost update."""
    a = import_jsonl([row("d", "m", 1, 10)], filename="a.jsonl")

    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(db, "transaction", _blocking_transaction(entered, release))

    undo_done = threading.Event()

    def run_undo():
        services.undo_import(a["import_id"])
        undo_done.set()

    tu = threading.Thread(target=run_undo)
    tu.start()
    assert entered.wait(5), "undo never reached its write transaction"

    imported = {}
    import_done = threading.Event()

    def run_import():
        imported["result"] = services.import_events(
            json.dumps(row("d", "m", 2, 20)), filename="b.jsonl"
        )
        import_done.set()

    ti = threading.Thread(target=run_import)
    ti.start()
    time.sleep(0.3)
    # The import's write transaction serializes behind the in-flight undo.
    assert not import_done.is_set()
    release.set()
    tu.join(10)
    ti.join(10)
    assert undo_done.is_set() and import_done.is_set()

    # Final committed state: A revoked (its event deleted), B live and latest.
    batches = {b["id"]: b for b in services.list_import_batches()}
    assert batches[a["import_id"]]["revoked"] is True
    new_id = imported["result"]["import_id"]
    assert batches[new_id]["can_undo"] is True
    assert _event("d", "m", 1) is None
    assert _event("d", "m", 2)["value"] == 20


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def test_api_import_lists_and_undo(client):
    r = client.post(
        "/api/import/text",
        json={"text": "\n".join(json.dumps(x) for x in [row("d", "m", 1, 10), row("d", "m", 2, 11)])},
    )
    assert r.status_code == 200, r.text
    bid = r.json()["import_id"]

    batches = client.get("/api/imports").json()
    assert len(batches) == 1 and batches[0]["can_undo"] is True
    assert batches[0]["snapshot_keys"] == 2

    r = client.post(f"/api/imports/{bid}/undo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_events"] == 2
    assert body["restored_events"] == 0
    assert body["evaluation"]["rules_evaluated"] == 0

    after = client.get("/api/imports").json()
    assert after[0]["revoked"] is True and after[0]["can_undo"] is False


def test_api_undo_restore_counts(client):
    client.post("/api/import/text", json={"text": json.dumps(row("d", "m", 1, 10))})
    client.post("/api/import/text", json={"text": json.dumps(row("d", "m", 1, 99)), "filename": "b.jsonl"})
    r = client.post("/api/imports/2/undo")
    assert r.status_code == 200, r.text
    assert r.json()["restored_events"] == 1
    assert r.json()["deleted_events"] == 0
    events = client.get("/api/events").json()["events"]
    assert events[0]["value"] == 10


def test_api_undo_guards(client):
    client.post("/api/import/text", json={"text": json.dumps(row("d", "m", 1, 10))})
    client.post("/api/import/text", json={"text": json.dumps(row("d", "m", 2, 20))})
    # older live batch -> 409
    r = client.post("/api/imports/1/undo")
    assert r.status_code == 409 and "最近一次" in r.json()["detail"]
    # unknown -> 404
    assert client.post("/api/imports/42/undo").status_code == 404
    # newest works; then repeating it is 409
    assert client.post("/api/imports/2/undo").status_code == 200
    r = client.post("/api/imports/2/undo")
    assert r.status_code == 409 and "已撤销" in r.json()["detail"]


def test_api_reset_wipes_batch_snapshots(client):
    client.post("/api/import/text", json={"text": json.dumps(row("d", "m", 1, 10))})
    assert len(client.get("/api/imports").json()) == 1
    client.post("/api/reset")
    assert client.get("/api/imports").json() == []
