"""Bounded extreme-value sampling for the timeline channels.

``services.downsample`` keeps the first/last samples of the queried range,
splits the middle samples into time-ordered buckets and — within the
remaining point budget — keeps each bucket's minimum and maximum, so an
isolated spike survives sampling and the result never exceeds max_points.
``GET /api/timeline`` uses it per channel while count/shown and the fault,
maintenance, suppressed and annotation layers keep using the original data.

For ranges larger than the point budget the timeline never materializes
the full row set: inside the same read snapshot it COUNTs the range first,
then streams the (ts, event id)-ordered rows in bounded batches and folds
them into the very same bucketed extreme selection
(``services._downsample_stream``), so the per-channel working memory grows
with max_points, not with the raw row count.
"""

import json
import os
import random
import tempfile
import threading
import time

import pytest

from app import db, services
from app.engine import to_epoch


def at(minute: int) -> float:
    return to_epoch(f"2026-09-23T00:{minute:02d}:00Z")


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


def series(points):
    """[(minute, value), ...] -> (ts, value, event_id) rows sorted by ts."""
    return [(at(m), float(v), i + 1) for i, (m, v) in enumerate(points)]


# --------------------------------------------------------------------------- #
# services.downsample
# --------------------------------------------------------------------------- #


def test_under_budget_returns_everything():
    values = series([(0, 1), (1, 5), (2, 2), (3, 4)])
    assert services.downsample(values, 4) == values
    assert services.downsample(values, 10) == values
    assert services.downsample([], 5) == []


def test_never_exceeds_budget_and_keeps_first_last():
    values = series([(m, (m * 7) % 5) for m in range(20)])
    for max_points in range(2, 10):
        out = services.downsample(values, max_points)
        assert len(out) <= max_points
        assert out[0] == values[0] and out[-1] == values[-1]
        # original (ts, event id) order
        assert [(p[0], p[2]) for p in out] == sorted((p[0], p[2]) for p in out)


def test_stride_bug_fixed_twenty_samples_four_points():
    # The old stride sampler returned 5 points here and dropped the spike.
    values = series([(m, 100 if m == 7 else 10) for m in range(20)])
    out = services.downsample(values, 4)
    assert len(out) <= 4
    assert out[0] == values[0] and out[-1] == values[-1]
    assert any(p[1] == 100.0 and p[0] == at(7) for p in out)


def test_isolated_spike_and_dip_survive():
    values = series([(m, 10) for m in range(30)])
    values[10] = (values[10][0], 99.0, values[10][2])   # spike
    values[20] = (values[20][0], -50.0, values[20][2])  # dip
    out = services.downsample(values, 6)
    kept = {p[1] for p in out}
    assert 99.0 in kept and -50.0 in kept


def test_bucket_min_and_max_both_kept():
    # 8 samples, budget 6 -> two 3-sample buckets, each gives min+max.
    values = series([(0, 0), (1, 3), (2, -2), (3, 1), (4, 4), (5, -1), (6, 2), (7, 0)])
    out = services.downsample(values, 6)
    assert len(out) == 6
    assert [p[1] for p in out] == [0.0, 3.0, -2.0, 4.0, -1.0, 0.0]


def test_constant_bucket_contributes_once():
    values = series([(m, 5) for m in range(10)])
    out = services.downsample(values, 4)
    assert len(out) <= 4
    assert out[0] == values[0] and out[-1] == values[-1]


def test_single_slot_keeps_larger_baseline_deviation():
    # max_points=3 -> one middle bucket with a single slot.  The bucket's
    # first->last baseline slopes from (min1, 0) to (min9, 10); the min
    # candidate (min8, -1) deviates more from it than the max (min4, 11),
    # even though 11 is the larger absolute value.
    values = series([(0, 0), (1, 0), (4, 11), (8, -1), (9, 10), (10, 10)])
    out = services.downsample(values, 3)
    assert [p[0] for p in out] == [at(0), at(8), at(10)]


def test_single_slot_tie_keeps_earlier_sample():
    # Flat baseline 0: min -1 (min2) and max +1 (min3) deviate equally;
    # the earlier sample wins.
    values = series([(0, 0), (1, 0), (2, -1), (3, 1), (4, 0), (5, 0)])
    out = services.downsample(values, 3)
    assert [p[0] for p in out] == [at(0), at(2), at(5)]


def test_odd_budget_leaves_single_slot_for_last_bucket():
    # max_points=5 -> budget 3 -> two buckets; the last one gets one slot
    # and keeps its larger-deviation extreme (the min9 spike).
    values = series(
        [(0, 0), (1, 0.5), (2, -0.5), (3, 0.4), (4, -0.4), (5, 0), (6, 9), (7, -1), (8, 0), (9, 0)]
    )
    out = services.downsample(values, 5)
    assert len(out) == 5
    assert any(p[0] == at(6) and p[1] == 9.0 for p in out)


def test_tiny_budgets():
    values = series([(m, m % 3) for m in range(10)])
    assert services.downsample(values, 2) == [values[0], values[-1]]
    assert services.downsample(values, 1) == [values[0]]
    assert services.downsample(values, 0) == []


# --------------------------------------------------------------------------- #
# GET /api/timeline
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(scratch_db):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _import_spike_series():
    rows = [
        {
            "device": "pump-1",
            "metric": "temp",
            "ts": f"2026-09-23T00:{m:02d}:00Z",
            "value": 100 if m == 7 else 10,
        }
        for m in range(20)
    ]
    return services.import_events("\n".join(json.dumps(r) for r in rows))


def test_api_timeline_bounded_sampling_keeps_spike(client):
    _import_spike_series()
    body = client.get("/api/timeline", params={"max_points": 4}).json()
    (ch,) = body["channels"]
    # count/shown still describe the original data
    assert ch["count"] == 20 and ch["shown"] == 20
    values = ch["values"]
    assert len(values) <= 4
    assert values[0]["ts"] == at(0) and values[-1]["ts"] == at(19)
    assert any(p["v"] == 100.0 and p["ts"] == at(7) for p in values)
    assert [p["ts"] for p in values] == sorted(p["ts"] for p in values)


def test_api_timeline_under_budget_returns_all_samples(client):
    _import_spike_series()
    body = client.get("/api/timeline", params={"max_points": 400}).json()
    (ch,) = body["channels"]
    assert len(ch["values"]) == 20


def test_api_timeline_other_layers_use_original_data(client):
    _import_spike_series()
    rule = client.post(
        "/api/rules",
        json={
            "name": "高温",
            "logic": "AND",
            "duration": 0,
            "conditions": [
                {"device": "pump-1", "metric": "temp", "op": ">=", "threshold": 80}
            ],
        },
    ).json()
    client.post("/api/evaluate")
    client.post(
        "/api/maintenance",
        json={
            "device": "pump-1",
            "name": "检修",
            "start": "2026-09-23T00:02:00Z",
            "end": "2026-09-23T00:03:00Z",
        },
    )
    body = client.get("/api/timeline", params={"max_points": 4}).json()
    # fault layer: the minute-7 spike still confirms a fault [00:07, 00:08)
    faults = [(f["rule_id"], f["start"], f["end"]) for f in body["faults"]]
    assert (rule["id"], at(7), at(8)) in faults
    # maintenance layer untouched by channel sampling
    assert [(w["name"], w["start"], w["end"]) for w in body["maintenance"]] == [
        ("检修", at(2), at(3))
    ]


# --------------------------------------------------------------------------- #
# services._downsample_stream: same selection over a batched row stream
# --------------------------------------------------------------------------- #


def _batched(values, size):
    return [values[i : i + size] for i in range(0, len(values), size)]


def test_stream_matches_list_downsample_across_batch_sizes():
    rng = random.Random(20260926)
    for _ in range(40):
        n = rng.randrange(0, 500)
        # Sorted by (ts, id) like the timeline scan; flat runs, noise and
        # isolated spikes/dips exercise every tie-break.
        values = []
        v = 0.0
        for i in range(n):
            r = rng.random()
            if r < 0.2:
                v = rng.choice((-500.0, 500.0))  # spike / dip
            elif r >= 0.35:
                v += rng.uniform(-5, 5)
            # else: flat run keeps the previous value
            values.append((float(i), round(v, 3), i + 1))
        for max_points in (0, 1, 2, 3, 4, 5, 6, 7, 9, 16, 33, 100, 400):
            expected = services.downsample(values, max_points)
            for batch_size in (1, 2, 7, 64, 10_000):
                got = services._downsample_stream(
                    n, iter(_batched(values, batch_size)), max_points
                )
                assert got == expected, (n, max_points, batch_size)


def test_stream_matches_list_downsample_edge_series():
    cases = [
        [(float(i), 5.0, i + 1) for i in range(100)],  # constant
        [(float(i), 5.0, i + 1) for i in range(1)],  # single sample
        [(float(i), float(i), i + 1) for i in range(2)],  # two samples
        [(float(i), float((i * 7) % 3), i + 1) for i in range(997)],
    ]
    for values in cases:
        for max_points in range(0, 12):
            expected = services.downsample(values, max_points)
            for batch_size in (1, 5, 2000):
                got = services._downsample_stream(
                    len(values), iter(_batched(values, batch_size)), max_points
                )
                assert got == expected, (len(values), max_points, batch_size)


# --------------------------------------------------------------------------- #
# GET /api/timeline over large ranges: bounded-memory batched scan
# --------------------------------------------------------------------------- #

LARGE_N = 3000
BASE_TS = 1790121600  # 2026-09-23T00:00:00Z


def _large_rows(n=LARGE_N):
    rows = []
    for i in range(n):
        value = (i * 37) % 97 - 48
        if i == 1234:
            value = 500  # isolated spike
        elif i == 2000:
            value = -500  # isolated dip
        rows.append({"device": "d", "metric": "m", "ts": BASE_TS + i, "value": value})
    return rows


def _import_large_series(n=LARGE_N):
    return services.import_events(
        "\n".join(json.dumps(r) for r in _large_rows(n)), reevaluate=False
    )


def _reference_values(max_points, start=None, end=None):
    """List-based downsample over the full ordered range (the old behavior)."""
    sql = "SELECT ts, value, id FROM events WHERE device = 'd' AND metric = 'm' AND value IS NOT NULL"
    params: list = []
    if start is not None:
        sql += " AND ts >= ?"
        params.append(start)
    if end is not None:
        sql += " AND ts <= ?"
        params.append(end)
    sql += " ORDER BY ts, id"
    rows = db.query(sql, params)
    return services.downsample([(r["ts"], r["value"], r["id"]) for r in rows], max_points)


def test_api_timeline_large_range_matches_reference_downsample(client):
    _import_large_series()
    body = client.get("/api/timeline", params={"max_points": 120}).json()
    (ch,) = body["channels"]
    # count/shown still describe the original data
    assert ch["count"] == LARGE_N and ch["shown"] == LARGE_N
    assert len(ch["values"]) <= 120
    assert [(p["ts"], p["v"], p["id"]) for p in ch["values"]] == _reference_values(120)
    # The isolated spike/dip survive the batched scan.
    kept = {p["v"] for p in ch["values"]}
    assert 500.0 in kept and -500.0 in kept
    assert ch["values"][0]["ts"] == BASE_TS
    assert ch["values"][-1]["ts"] == BASE_TS + LARGE_N - 1


def test_api_timeline_large_range_with_time_window(client):
    _import_large_series()
    start, end = BASE_TS + 500, BASE_TS + 2500
    body = client.get(
        "/api/timeline", params={"start": start, "end": end, "max_points": 77}
    ).json()
    (ch,) = body["channels"]
    assert ch["shown"] == 2001  # inclusive bounds: ts 500..2500
    assert len(ch["values"]) <= 77
    assert [(p["ts"], p["v"], p["id"]) for p in ch["values"]] == _reference_values(
        77, start, end
    )


def test_api_timeline_large_range_never_fetches_full_range(client, monkeypatch):
    _import_large_series()
    fetched_sql = []
    real_query = db.query

    def spy_query(sql, params=(), conn=None):
        fetched_sql.append(sql)
        return real_query(sql, params, conn=conn)

    monkeypatch.setattr(db, "query", spy_query)

    batch_lens = []
    real_query_batches = db.query_batches

    def spy_query_batches(sql, params=(), batch_size=2000):
        for batch in real_query_batches(sql, params, batch_size):
            batch_lens.append(len(batch))
            yield batch

    monkeypatch.setattr(db, "query_batches", spy_query_batches)

    body = client.get("/api/timeline", params={"max_points": 100}).json()
    (ch,) = body["channels"]
    assert ch["shown"] == LARGE_N
    assert len(ch["values"]) <= 100
    # The ordered channel scan streams in bounded batches (default 2000)
    # instead of materializing all 3000 rows in one fetchall.
    assert batch_lens and max(batch_lens) <= 2000 < LARGE_N
    assert not any("ORDER BY ts, id" in sql for sql in fetched_sql)


# --------------------------------------------------------------------------- #
# Concurrent writes: the batched scan stays inside the pinned committed view
# --------------------------------------------------------------------------- #


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


def test_query_batches_stream_stays_in_pinned_snapshot(scratch_db):
    services.import_events(
        "\n".join(json.dumps(r) for r in _large_rows(50)), reevaluate=False
    )
    started = threading.Event()
    hold = threading.Event()
    seen = {}

    def reader():
        with db.read_snapshot():
            seen["count"] = db.query_one(
                "SELECT COUNT(*) AS c FROM events WHERE device = 'd' AND metric = 'm'"
            )["c"]
            started.set()
            hold.wait(5)
            # A concurrent commit lands between the COUNT and this scan:
            # the pinned snapshot must still stream the same 50 rows.
            seen["streamed"] = sum(
                len(batch)
                for batch in db.query_batches(
                    "SELECT ts FROM events WHERE device = 'd' AND metric = 'm'"
                    " ORDER BY ts, id",
                    batch_size=7,
                )
            )

    t = threading.Thread(target=reader)
    t.start()
    assert started.wait(5)
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO events (device, metric, ts, value, raw, import_id)"
            " VALUES ('d','m',?,999,'x',NULL)",
            (BASE_TS + 10_000,),
        )
    hold.set()
    t.join(5)
    assert seen == {"count": 50, "streamed": 50}
    # After the snapshot closes the committed row is visible.
    assert db.query_one("SELECT COUNT(*) AS c FROM events")["c"] == 51


def test_api_timeline_large_range_hides_uncommitted_rows(client):
    _import_large_series()
    held = HeldWrite(
        "INSERT INTO events (device, metric, ts, value, raw, import_id)"
        " VALUES ('d','m',?,99999,'x',NULL)",
        (BASE_TS + 10_000,),
    )
    with held:
        body = client.get("/api/timeline", params={"max_points": 100}).json()
        (ch,) = body["channels"]
        assert ch["count"] == LARGE_N and ch["shown"] == LARGE_N
        assert ch["max_ts"] == BASE_TS + LARGE_N - 1
        assert len(ch["values"]) <= 100
        assert all(p["v"] != 99999 for p in ch["values"])
    # Rolled back: the row never existed for the timeline either.
    body = client.get("/api/timeline", params={"max_points": 100}).json()
    (ch,) = body["channels"]
    assert ch["count"] == LARGE_N and ch["shown"] == LARGE_N


def test_api_timeline_snapshot_coherent_under_concurrent_commits(client):
    _import_large_series(1500)
    stop = threading.Event()

    def committer():
        n = 0
        while not stop.is_set():
            with db.transaction() as tx:
                tx.execute(
                    "INSERT INTO events (device, metric, ts, value, raw, import_id)"
                    " VALUES ('d','m',?,?,'x',NULL)",
                    (BASE_TS + 100_000 + n, n % 50),
                )
            n += 1
            time.sleep(0.0005)

    t = threading.Thread(target=committer, daemon=True)
    t.start()
    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            body = client.get("/api/timeline", params={"max_points": 100}).json()
            (ch,) = body["channels"]
            # count/shown, the channel bounds and the scanned samples all
            # come from one pinned committed view, even while commits land.
            assert ch["shown"] == ch["count"]
            assert ch["values"][0]["ts"] == ch["min_ts"]
            assert ch["values"][-1]["ts"] == ch["max_ts"]
            assert body["data_end"] == ch["max_ts"]
            assert len(ch["values"]) <= 100
            ts_list = [p["ts"] for p in ch["values"]]
            assert ts_list == sorted(ts_list)
    finally:
        stop.set()
        t.join(5)
