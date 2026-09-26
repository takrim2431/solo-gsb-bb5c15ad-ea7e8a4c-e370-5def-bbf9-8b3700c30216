"""Concurrency tests for the committed-view read layer.

Reads (``db.query`` / ``db.query_one`` and every service/API built on them)
run on dedicated read connections and therefore see only committed data:

* rows written by an open write transaction are invisible until commit and
  stay invisible forever when the transaction rolls back;
* a ``db.read_snapshot()`` block pins one committed view for all statements
  inside it, so the /api/events COUNT and its page SELECT can never straddle
  a concurrent commit;
* the same isolation applies to faults and configuration (rules): /api/faults
  and the timeline never show uncommitted or rolled-back rows.
"""

import json
import os
import tempfile
import threading
import time

import pytest

from app import db, services
from app.engine import to_epoch


def at(minute: int) -> float:
    return to_epoch(f"2026-09-23T00:{minute:02d}:00Z")


def event_row(device, metric, minute, value):
    return {
        "device": device,
        "metric": metric,
        "ts": f"2026-09-23T00:{minute:02d}:00Z",
        "value": value,
    }


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


class HeldWrite:
    """Keep an INSERT open (uncommitted) on the writer connection."""

    def __init__(self, sql, params=()):
        self.sql = sql
        self.params = params
        self.started = threading.Event()
        self.release = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.rollback = True
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            conn = db.get_conn()
            with db._write_lock:
                conn.execute(self.sql, tuple(self.params))
                self.started.set()
                self.release.wait(10)
                if self.rollback:
                    conn.rollback()
                else:
                    conn.commit()
        except Exception as exc:  # pragma: no cover - surfaced by the test
            self.error = exc
        finally:
            self.done.set()

    def __enter__(self):
        self._thread.start()
        assert self.started.wait(5), "writer never started"
        # Let the uncommitted INSERT settle before readers look.
        time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.release.set()
        assert self.done.wait(5), "writer never finished"
        assert self.error is None, self.error


# --------------------------------------------------------------------------- #
# db layer: independent committed view
# --------------------------------------------------------------------------- #


def test_reads_do_not_see_uncommitted_event(scratch_db):
    services.import_events(
        json.dumps(event_row("d", "m", 1, 10)), reevaluate=False
    )
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99,'x',NULL)",
        (at(2),),
    )
    with held:
        # A reader on a different connection (even a different thread) sees
        # the old committed state while the write is open.
        assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 1
        seen = db.query(
            "SELECT id FROM events WHERE device='d' AND metric='m' AND ts=?",
            (at(2),),
        )
        assert seen == []
    # Rolled back: the event never existed.
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 1


def test_reads_see_committed_event_after_commit(scratch_db):
    services.import_events(
        json.dumps(event_row("d", "m", 1, 10)), reevaluate=False
    )
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99,'x',NULL)",
        (at(2),),
    )
    held.rollback = False
    with held:
        assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 1
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 2


def test_read_snapshot_pins_committed_view(scratch_db):
    services.import_events(
        json.dumps(event_row("d", "m", 1, 10)), reevaluate=False
    )
    pinned = {}
    hold = threading.Event()
    started = threading.Event()

    def reader():
        with db.read_snapshot():
            pinned["before"] = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]
            started.set()
            hold.wait(5)
            pinned["inside"] = db.query_one("SELECT COUNT(*) AS c FROM events")["c"]

    t = threading.Thread(target=reader)
    t.start()
    assert started.wait(5)
    # A concurrent commit must not change the pinned snapshot.
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO events (device, metric, ts, value, raw, import_id)"
            " VALUES ('d','m',?,99,'x',NULL)",
            (at(2),),
        )
    hold.set()
    t.join(5)
    assert pinned == {"before": 1, "inside": 1}
    # After the snapshot closes the new committed value is visible.
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 2


def test_nested_read_snapshot_shares_one_view(scratch_db):
    with db.read_snapshot():
        with db.read_snapshot():
            assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0
    # Snapshot state is cleaned up and ad-hoc reads still work afterwards.
    assert getattr(db._read_local, "snapshot_conn", None) is None
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 0


# --------------------------------------------------------------------------- #
# /api/events: total and list from one committed snapshot
# --------------------------------------------------------------------------- #


def test_api_events_hides_uncommitted_and_rolled_back_row(client):
    client.post(
        "/api/import/text", json={"text": json.dumps(event_row("d", "m", 1, 10))}
    )
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99,'x',NULL)",
        (at(2),),
    )
    with held:
        body = client.get("/api/events").json()
        assert body["total"] == 1
        assert len(body["events"]) == 1
    body = client.get("/api/events").json()
    assert body["total"] == 1 and len(body["events"]) == 1


def test_api_events_total_matches_list_under_concurrent_commits(client):
    # Seed enough rows for pagination to matter.
    rows = [event_row("d", "m", m, m) for m in range(1, 41)]
    client.post(
        "/api/import/text", json={"text": "\n".join(json.dumps(r) for r in rows)}
    )

    stop = threading.Event()

    def committer():
        n = 0
        while not stop.is_set():
            with db.transaction() as tx:
                tx.execute(
                    "INSERT INTO events (device, metric, ts, value, raw, import_id)"
                    " VALUES ('d','m',?,?,'x',NULL)",
                    (1000.0 + n, n),
                )
            n += 1
            time.sleep(0.0005)

    t = threading.Thread(target=committer)
    t.start()
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            body = client.get("/api/events?limit=25&offset=0").json()
            # The page length must agree with the total counted in the very
            # same snapshot even while commits keep landing.
            assert len(body["events"]) == min(25, body["total"])
    finally:
        stop.set()
        t.join(5)


# --------------------------------------------------------------------------- #
# Faults, configuration and the timeline reuse the same read layer
# --------------------------------------------------------------------------- #


def _seed_rule_with_fault(client):
    rows = [event_row("d", "m", m, 90) for m in range(1, 5)]
    client.post(
        "/api/import/text", json={"text": "\n".join(json.dumps(r) for r in rows)}
    )
    rule = client.post(
        "/api/rules",
        json={
            "name": "r",
            "logic": "AND",
            "duration": 0,
            "conditions": [
                {"device": "d", "metric": "m", "op": ">=", "threshold": 80}
            ],
        },
    ).json()
    n = client.post("/api/evaluate").json()["fault_intervals"]
    assert n == 1
    return rule["id"]


def test_api_faults_hides_uncommitted_fault(client):
    _seed_rule_with_fault(client)
    assert len(client.get("/api/faults").json()) == 1
    held = HeldWrite(
        """
        INSERT INTO fault_intervals
            (rule_id, start_ts, end_ts, duration_required, confirmed_at, evidence_json)
        VALUES (1, ?, ?, 0, ?, '[]')
        """,
        (at(10), at(11), at(10)),
    )
    with held:
        assert len(client.get("/api/faults").json()) == 1
        assert len(client.get("/api/fault-events").json()) == 1
    # Rolled-back fault stays invisible.
    assert len(client.get("/api/faults").json()) == 1


def test_api_config_hides_uncommitted_rule(client):
    _seed_rule_with_fault(client)
    assert client.get("/api/meta").json()["rule_count"] == 1
    held = HeldWrite(
        "INSERT INTO rules (name, logic, duration, created_at) VALUES ('ghost','AND',0,0)"
    )
    with held:
        assert client.get("/api/meta").json()["rule_count"] == 1
        assert {r["name"] for r in client.get("/api/rules").json()} == {"r"}
    assert client.get("/api/meta").json()["rule_count"] == 1


def test_api_timeline_hides_uncommitted_event(client):
    client.post(
        "/api/import/text",
        json={
            "text": "\n".join(
                json.dumps(event_row("d", "m", m, m)) for m in range(1, 4)
            )
        },
    )
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99,'x',NULL)",
        (at(20),),
    )
    with held:
        body = client.get("/api/timeline").json()
        channel = next(c for c in body["channels"] if c["device"] == "d")
        assert channel["count"] == 3
        assert body["data_end"] == at(3)
    body = client.get("/api/timeline").json()
    channel = next(c for c in body["channels"] if c["device"] == "d")
    assert channel["count"] == 3


def test_api_timeline_snapshot_coherent_with_commit_after(client):
    # Committing after the open transaction rolls forward makes the new data
    # visible to the next (separate snapshot) request.
    client.post(
        "/api/import/text", json={"text": json.dumps(event_row("d", "m", 1, 10))}
    )
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99,'x',NULL)",
        (at(2),),
    )
    held.rollback = False
    with held:
        assert client.get("/api/events").json()["total"] == 1
    body = client.get("/api/events").json()
    assert body["total"] == 2
