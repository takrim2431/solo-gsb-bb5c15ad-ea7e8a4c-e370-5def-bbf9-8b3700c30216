"""Concurrency tests for rule evaluation vs. telemetry/config writes.

``services.evaluate_all_rules`` reads its inputs (events, rules, maintenance
windows, channel TTLs) and replaces the stored fault/suppressed rows inside
one serialized write transaction (the same write lock every event import,
rule, TTL and maintenance-window write funnels through), so:

* a stale in-flight evaluation — one that read its inputs before a
  concurrent import committed — can never overwrite the newer committed
  recompute: the final saved faults always correspond to the latest
  committed inputs;
* a failed evaluation rolls the whole replacement back, never leaving
  partially replaced faults behind;
* readers (services and API) only ever see committed fault results, never
  an in-flight replacement.
"""

import json
import os
import tempfile
import threading
import time

import pytest

from app import db, services
from app.engine import Condition, evaluate_rule


def event_row(ts, value, device="d", metric="m"):
    return {"device": device, "metric": metric, "ts": ts, "value": value}


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


def _make_rule(name="r", device="d", metric="m", op=">=", threshold=80, duration=0):
    with db.transaction() as tx:
        cur = tx.execute(
            "INSERT INTO rules (name, logic, duration, created_at) VALUES (?,?,?,?)",
            (name, "AND", duration, 0.0),
        )
        rule_id = cur.lastrowid
        tx.execute(
            "INSERT INTO rule_conditions (rule_id, device, metric, op, threshold, ord)"
            " VALUES (?,?,?,?,?,0)",
            (rule_id, device, metric, op, threshold),
        )
    return rule_id


def _fault_spans():
    return [(f["start"], f["end"]) for f in services.query_faults()]


class _BlockFirstCall:
    """Block the first caller of a services function until released.

    Later callers (e.g. the import's own re-evaluation) pass through, so the
    test controls exactly one in-flight evaluation.
    """

    def __init__(self, monkeypatch, attr, block_after_real_call=True):
        self._real = getattr(services, attr)
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = threading.Event()
        self._armed.set()
        self._block_after = block_after_real_call
        monkeypatch.setattr(services, attr, self)

    def __call__(self, *args, **kwargs):
        if self._armed.is_set() and not self._block_after:
            self._block()
        result = self._real(*args, **kwargs)
        if self._armed.is_set() and self._block_after:
            self._block()
        return result

    def _block(self):
        self._armed.clear()
        self.entered.set()
        assert self.release.wait(15), "test harness: blocked call never released"


# --------------------------------------------------------------------------- #
# Stale in-flight evaluation vs. concurrent import (the reported scenario)
# --------------------------------------------------------------------------- #


def test_stale_inflight_evaluation_cannot_overwrite_import_recompute(
    scratch_db, monkeypatch
):
    """High sample at 0s, low at 20s -> fault [0,20).  A manual evaluation
    has read its inputs but not saved yet when an import of a low sample at
    10s commits and recomputes [0,10).  The old evaluation's late commit must
    not restore the fault to [0,20): the last committed result must match the
    latest committed inputs."""
    services.import_events(
        "\n".join(json.dumps(event_row(ts, v)) for ts, v in [(0.0, 90), (20.0, 10)]),
        reevaluate=False,
    )
    _make_rule()
    services.evaluate_all_rules()
    assert _fault_spans() == [(0.0, 20.0)]

    # The old evaluation blocks right after reading its inputs, before
    # computing and replacing anything.
    hook = _BlockFirstCall(monkeypatch, "_load_events_index")
    eval_done = threading.Event()

    def run_evaluation():
        services.evaluate_all_rules()
        eval_done.set()

    ta = threading.Thread(target=run_evaluation)
    ta.start()
    assert hook.entered.wait(5), "evaluation never reached its input read"

    # The import (and its own recompute) must serialize behind the in-flight
    # evaluation: while the old evaluation holds the write section, the new
    # sample cannot commit underneath it.
    import_done = threading.Event()

    def run_import():
        services.import_events(json.dumps(event_row(10.0, 5)))
        import_done.set()

    tb = threading.Thread(target=run_import)
    try:
        tb.start()
        time.sleep(0.3)
        assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 2
    finally:
        # Let the old evaluation commit, then the import + recompute proceed;
        # the last commit evaluates the latest committed inputs.
        hook.release.set()
    assert eval_done.wait(10), "in-flight evaluation never finished"
    assert import_done.wait(10), "import never finished"
    ta.join(10)
    tb.join(10)

    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 3
    # The low sample at 10s ends the fault there — the stale [0,20) must be
    # gone for good.
    assert _fault_spans() == [(0.0, 10.0)]


def test_concurrent_imports_and_evaluations_converge_to_latest_inputs(scratch_db):
    """Under arbitrary interleavings, once every writer finished, the stored
    faults equal a fresh evaluation of the final committed inputs."""
    seed = [event_row(0.0, 90), event_row(50.0, 10), event_row(100.0, 95)]
    services.import_events(
        "\n".join(json.dumps(r) for r in seed), reevaluate=False
    )
    rule_id = _make_rule()
    services.evaluate_all_rules()

    def importer(base):
        for i in range(5):
            # Alternating high/low back-filled samples shift the truth edges.
            services.import_events(
                json.dumps(event_row(base + 10.0 * i, 5 + 90 * (i % 2)))
            )

    def evaluator():
        for _ in range(5):
            services.evaluate_all_rules()

    threads = [
        threading.Thread(target=importer, args=(10.0,)),
        threading.Thread(target=importer, args=(15.0,)),
        threading.Thread(target=evaluator),
        threading.Thread(target=evaluator),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
        assert not t.is_alive()

    # Expected faults: evaluate the stored rule against the final committed
    # inputs, in memory, without touching the stored results.
    events_by_channel, horizon = services._load_events_index()
    expected = evaluate_rule(
        rule_id=rule_id,
        logic="AND",
        duration_required=0,
        conditions=[Condition.from_dict(c) for c in db.fetch_rule(rule_id)["conditions"]],
        events_by_channel=events_by_channel,
        horizon=horizon if horizon is not None else 0.0,
        maintenance=[],
        ttl_by_channel={},
    )
    assert _fault_spans() == [(f.start, f.end) for f in expected.faults]


# --------------------------------------------------------------------------- #
# Atomicity of the replacement
# --------------------------------------------------------------------------- #


def test_failed_evaluation_leaves_previous_faults_untouched(scratch_db, monkeypatch):
    services.import_events(
        "\n".join(json.dumps(event_row(ts, 90)) for ts in (0.0, 10.0, 20.0)),
        reevaluate=False,
    )
    _make_rule(name="r1")
    _make_rule(name="r2")
    services.evaluate_all_rules()
    committed = services.query_faults()
    assert len(committed) == 2  # one fault per rule

    real_evaluate_rule = services.evaluate_rule
    calls = []

    def failing_evaluate_rule(**kwargs):
        calls.append(kwargs["rule_id"])
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real_evaluate_rule(**kwargs)

    monkeypatch.setattr(services, "evaluate_rule", failing_evaluate_rule)
    with pytest.raises(RuntimeError, match="boom"):
        services.evaluate_all_rules()

    # The first rule's delete+insert rolled back with the failure: the stored
    # faults are exactly the previously committed ones (ids included).
    assert services.query_faults() == committed


def test_inflight_replacement_invisible_to_readers(scratch_db, monkeypatch):
    """While an evaluation holds uncommitted replacements, concurrent readers
    keep seeing the last committed fault set."""
    services.import_events(
        "\n".join(json.dumps(event_row(ts, 90)) for ts in (0.0, 10.0, 20.0)),
        reevaluate=False,
    )
    _make_rule(name="r1")
    _make_rule(name="r2")
    services.evaluate_all_rules()
    committed = services.query_faults()
    assert len(committed) == 2

    # Block inside the second rule's evaluation: rule 1's delete+insert is
    # already uncommitted in the in-flight transaction at this point.
    real_evaluate_rule = services.evaluate_rule
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def pausing_evaluate_rule(**kwargs):
        calls.append(kwargs["rule_id"])
        if len(calls) == 2:
            entered.set()
            assert release.wait(15), "test harness: evaluation never released"
        return real_evaluate_rule(**kwargs)

    monkeypatch.setattr(services, "evaluate_rule", pausing_evaluate_rule)
    eval_done = threading.Event()

    def run_evaluation():
        services.evaluate_all_rules()
        eval_done.set()

    t = threading.Thread(target=run_evaluation)
    t.start()
    assert entered.wait(5), "evaluation never reached the second rule"
    try:
        # Mid-replacement: readers still get the complete committed fault set.
        assert services.query_faults() == committed
    finally:
        release.set()
    assert eval_done.wait(10)
    t.join(10)
    # Same inputs -> same intervals (ids are legitimately new after replace).
    assert _fault_spans() == [(0.0, 20.0), (0.0, 20.0)]


def test_api_faults_only_show_committed_results_during_evaluation(client, monkeypatch):
    client.post(
        "/api/import/text",
        json={"text": "\n".join(json.dumps(event_row(ts, 90)) for ts in (0.0, 10.0, 20.0))},
    )
    for name in ("r1", "r2"):
        client.post(
            "/api/rules",
            json={
                "name": name,
                "logic": "AND",
                "duration": 0,
                "conditions": [
                    {"device": "d", "metric": "m", "op": ">=", "threshold": 80}
                ],
            },
        )
    assert client.post("/api/evaluate").json()["fault_intervals"] == 2
    committed = client.get("/api/faults").json()
    assert len(committed) == 2

    real_evaluate_rule = services.evaluate_rule
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def pausing_evaluate_rule(**kwargs):
        calls.append(kwargs["rule_id"])
        if len(calls) == 2:
            entered.set()
            assert release.wait(15), "test harness: evaluation never released"
        return real_evaluate_rule(**kwargs)

    monkeypatch.setattr(services, "evaluate_rule", pausing_evaluate_rule)
    eval_done = threading.Event()

    def run_evaluation():
        services.evaluate_all_rules()
        eval_done.set()

    t = threading.Thread(target=run_evaluation)
    t.start()
    assert entered.wait(5), "evaluation never reached the second rule"
    try:
        # The API never exposes the in-flight (uncommitted) replacement.
        assert client.get("/api/faults").json() == committed
        assert len(client.get("/api/fault-events").json()) == 1
    finally:
        release.set()
    assert eval_done.wait(10)
    t.join(10)
    spans = [(f["start"], f["end"]) for f in client.get("/api/faults").json()]
    assert spans == [(0.0, 20.0), (0.0, 20.0)]
