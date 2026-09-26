"""SQLite persistence layer."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Generator, Iterable

DB_PATH = os.environ.get("SENSOR_DB_PATH", "/data/sensor.db")
# When running locally without docker, fall back to ./data next to the repo.
if DB_PATH == "/data/sensor.db" and not os.path.isdir("/data"):
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sensor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    imported_at REAL NOT NULL,
    total_rows INTEGER NOT NULL DEFAULT 0,
    inserted_rows INTEGER NOT NULL DEFAULT 0,
    overwritten_rows INTEGER NOT NULL DEFAULT 0,
    skipped_rows INTEGER NOT NULL DEFAULT 0,
    -- 1 for batches imported with pre-import key snapshots (undoable);
    -- 0 for batches created before the undo feature existed.
    snapshot_complete INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL
);

-- Pre-import snapshot of every (device, metric, ts) key a batch touched for
-- the first time within that batch. event_id NULL means the key did not exist
-- before the batch; otherwise the row carries the overwritten event's value,
-- raw content and original batch ownership for restore-on-undo.
CREATE TABLE IF NOT EXISTS import_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts REAL NOT NULL,
    event_id INTEGER,
    value REAL,
    raw TEXT,
    origin_import_id INTEGER,
    UNIQUE (import_id, device, metric, ts)
);
CREATE INDEX IF NOT EXISTS idx_import_snapshots_batch ON import_snapshots(import_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts REAL NOT NULL,
    value REAL,
    raw TEXT,
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    UNIQUE (device, metric, ts)
);
CREATE INDEX IF NOT EXISTS idx_events_channel ON events(device, metric, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    logic TEXT NOT NULL DEFAULT 'AND',
    duration REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_conditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    op TEXT NOT NULL,
    threshold REAL NOT NULL,
    reset_threshold REAL,
    ord INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fault_intervals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    duration_required REAL NOT NULL,
    confirmed_at REAL NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faults_rule ON fault_intervals(rule_id);
CREATE INDEX IF NOT EXISTS idx_faults_ts ON fault_intervals(start_ts, end_ts);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    label TEXT NOT NULL,
    color TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_annot_ts ON annotations(start_ts, end_ts);

CREATE TABLE IF NOT EXISTS maintenance_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    name TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maint_device ON maintenance_windows(device, start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_maint_ts ON maintenance_windows(start_ts, end_ts);

CREATE TABLE IF NOT EXISTS suppressed_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    maintenance_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_supp_rule ON suppressed_segments(rule_id);
CREATE INDEX IF NOT EXISTS idx_supp_ts ON suppressed_segments(start_ts, end_ts);

CREATE TABLE IF NOT EXISTS channel_ttls (
    device TEXT NOT NULL,
    metric TEXT NOT NULL,
    ttl_seconds REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device, metric)
);
"""


def connect(read_only: bool = False) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
        # Readers run in autocommit: with no Python-level implicit
        # transaction each statement opens its own read transaction and
        # releases it as soon as the rows are fetched, so a bare SELECT
        # sees the latest committed snapshot and never holds a read lock
        # that would block WAL writers.
        isolation_level=None if read_only else "",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if read_only:
        # Defensive: this connection is the committed-view read layer and
        # must never mutate the database. journal_mode is left untouched
        # (the writer already persists WAL on the database file).
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


_conn: sqlite3.Connection | None = None
_write_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


# --------------------------------------------------------------------------- #
# Read layer: connections that never take part in a write transaction.
#
# A single shared connection cannot read and write concurrently: while the
# writer holds an open transaction on it, every SELECT executed through it
# sees the writer's uncommitted changes (and after a rollback those reads
# have observed rows that never existed).  Read-only access therefore goes
# through separate per-thread connections.  In WAL mode a connection only
# sees the database as it was committed when its read transaction started,
# which gives readers an independent committed snapshot: uncommitted and
# later-rolled-back writes are invisible, committed writes become visible
# to the next statement/snapshot.
# --------------------------------------------------------------------------- #

_read_local = threading.local()


def _get_read_conn() -> sqlite3.Connection:
    conn: sqlite3.Connection | None = getattr(_read_local, "conn", None)
    # Tests (and reconfiguration) may repoint DB_PATH and reset the writer;
    # a reader cached for another file must be replaced before first use.
    if conn is None or os.path.realpath(conn.execute("PRAGMA database_list").fetchone()[2]) \
            != os.path.realpath(DB_PATH):
        if conn is not None:
            conn.close()
        conn = connect(read_only=True)
        _read_local.conn = conn
    return conn


@contextmanager
def read_snapshot():
    """Pin all read-layer queries made on this thread to one committed view.

    Every ``query``/``query_one`` inside the block runs on the same read
    connection inside one explicit read transaction, so multi-statement
    reads (e.g. an events COUNT followed by its page SELECT) cannot have a
    concurrent commit land between them.  The snapshot is the last committed
    state at the block's entry; writes committed afterwards are not seen, and
    uncommitted/rolled-back writes are never visible.
    """
    conn = _get_read_conn()
    if getattr(_read_local, "snapshot_conn", None) is not None:
        # Already inside a read snapshot on this thread: reuse it so the
        # outer block owns the single pinned transaction.
        yield conn
        return
    conn.execute("BEGIN")
    _read_local.snapshot_conn = conn
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        _read_local.snapshot_conn = None


def _read_target() -> sqlite3.Connection:
    """Connection for an ad-hoc read: an open snapshot if one is active on
    this thread, otherwise the thread's autocommit reader (one committed
    snapshot per statement)."""
    snapshot_conn = getattr(_read_local, "snapshot_conn", None)
    if snapshot_conn is not None:
        return snapshot_conn
    return _get_read_conn()


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    # Migration for databases created before the optional reset threshold:
    # CREATE TABLE IF NOT EXISTS does not add columns to an existing table.
    condition_cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(rule_conditions)").fetchall()
    }
    if "reset_threshold" not in condition_cols:
        conn.execute("ALTER TABLE rule_conditions ADD COLUMN reset_threshold REAL")
    # Migration for databases created before import-batch undo: the imports
    # table gains the snapshot flag and the revocation timestamp.
    import_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(imports)").fetchall()
    }
    if "overwritten_rows" not in import_cols:
        conn.execute("ALTER TABLE imports ADD COLUMN overwritten_rows INTEGER NOT NULL DEFAULT 0")
    if "snapshot_complete" not in import_cols:
        # Pre-feature batches have no snapshots and must be refused on undo.
        conn.execute(
            "ALTER TABLE imports ADD COLUMN snapshot_complete INTEGER NOT NULL DEFAULT 0"
        )
    if "revoked_at" not in import_cols:
        conn.execute("ALTER TABLE imports ADD COLUMN revoked_at REAL")
    conn.commit()


@contextmanager
def transaction():
    """Serialize writers; sqlite3 connections are shared across event-loop
    threads and a single connection cannot run two transactions at once."""
    conn = get_conn()
    with _write_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(
    sql: str,
    params: Iterable[Any] = (),
    conn: sqlite3.Connection | None = None,
) -> list[sqlite3.Row]:
    """Run a SELECT and fetch all rows.

    Without ``conn`` the read goes through the committed-view read layer
    (an open read snapshot on this thread, otherwise the autocommit reader).
    With ``conn`` the statement runs on that connection — used by the write
    path to read its inputs inside its own serialized write transaction.
    """
    target = conn if conn is not None else _read_target()
    return target.execute(sql, tuple(params)).fetchall()


def query_batches(
    sql: str, params: Iterable[Any] = (), batch_size: int = 2000
) -> Generator[list[sqlite3.Row], None, None]:
    """Yield SELECT rows in bounded batches instead of one fetchall.

    At most ``batch_size`` rows are materialized at a time, so scans over
    arbitrarily large result sets keep a constant memory footprint.  The
    stream runs on the read connection: inside a ``read_snapshot()`` block
    every batch reads the same pinned committed view (a concurrent commit
    cannot change rows mid-stream); outside one, the open cursor holds its
    own read transaction for the duration of the stream.  The cursor is
    closed when the generator is exhausted or closed.
    """
    cur = _read_target().execute(sql, tuple(params))
    try:
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            yield rows
    finally:
        cur.close()


def query_one(
    sql: str,
    params: Iterable[Any] = (),
    conn: sqlite3.Connection | None = None,
) -> sqlite3.Row | None:
    """Single-row variant of :func:`query` (same connection routing)."""
    target = conn if conn is not None else _read_target()
    return target.execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    conn = get_conn()
    with _write_lock:
        cur = conn.execute(sql, tuple(params))
        conn.commit()
    return cur


def reset_all() -> None:
    # One serialized write transaction, like every other write path: a reset
    # cannot interleave with an in-flight import or rule re-evaluation, so a
    # stale evaluation can never resurrect faults after the reset commits.
    with transaction() as conn:
        for table in (
            "fault_intervals",
            "suppressed_segments",
            "rule_conditions",
            "annotations",
            "maintenance_windows",
            "rules",
            "events",
            "import_snapshots",
            "imports",
            "channel_ttls",
        ):
            conn.execute(f"DELETE FROM {table}")


def fetch_rule(rule_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    row = query_one("SELECT * FROM rules WHERE id = ?", (rule_id,), conn=conn)
    if row is None:
        return None
    conds = query(
        "SELECT * FROM rule_conditions WHERE rule_id = ? ORDER BY ord, id",
        (rule_id,),
        conn=conn,
    )
    return {
        "id": row["id"],
        "name": row["name"],
        "logic": row["logic"],
        "duration": row["duration"],
        "created_at": row["created_at"],
        "conditions": [
            {
                "id": c["id"],
                "device": c["device"],
                "metric": c["metric"],
                "op": c["op"],
                "threshold": c["threshold"],
                "reset_threshold": c["reset_threshold"],
            }
            for c in conds
        ],
    }


def fetch_rules(conn: sqlite3.Connection | None = None) -> list[dict]:
    rows = query("SELECT id FROM rules ORDER BY id", conn=conn)
    return [r for r in (fetch_rule(row["id"], conn=conn) for row in rows) if r]


def upsert_event(
    conn: sqlite3.Connection,
    device: str,
    metric: str,
    ts: float,
    value: float | None,
    raw: str,
    import_id: int | None,
) -> bool:
    """Insert an event, or replace value/raw on (device, metric, ts) collision.

    Returns True when a new row was inserted, False on overwrite.
    """
    existing = conn.execute(
        "SELECT id FROM events WHERE device = ? AND metric = ? AND ts = ?",
        (device, metric, ts),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO events (device, metric, ts, value, raw, import_id) VALUES (?,?,?,?,?,?)",
            (device, metric, ts, value, raw, import_id),
        )
        return True
    conn.execute(
        "UPDATE events SET value = ?, raw = ?, import_id = ? WHERE id = ?",
        (value, raw, import_id, existing["id"]),
    )
    return False


def snapshot_import_key(
    conn: sqlite3.Connection,
    import_id: int,
    seen_keys: set[tuple[str, str, float]],
    device: str,
    metric: str,
    ts: float,
) -> None:
    """Record the pre-import state of a (device, metric, ts) key, once per batch.

    The caller tracks keys already snapshotted in *seen_keys*: repeated keys
    within the same batch snapshot only their first encounter even though the
    last valid row still wins the overwrite.  An event row existing before the
    batch is archived with its value, raw content and original batch ownership;
    a missing key is stored with ``event_id IS NULL`` so undo deletes the event
    the batch went on to create.
    """
    key = (device, metric, ts)
    if key in seen_keys:
        return
    seen_keys.add(key)
    existing = conn.execute(
        "SELECT id, value, raw, import_id FROM events"
        " WHERE device = ? AND metric = ? AND ts = ?",
        (device, metric, ts),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO import_snapshots (import_id, device, metric, ts, event_id)"
            " VALUES (?,?,?,?,?)",
            (import_id, device, metric, ts, None),
        )
    else:
        conn.execute(
            "INSERT INTO import_snapshots"
            " (import_id, device, metric, ts, event_id, value, raw, origin_import_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                import_id,
                device,
                metric,
                ts,
                existing["id"],
                existing["value"],
                existing["raw"],
                existing["import_id"],
            ),
        )


def fetch_maintenance_window(
    window_id: int, conn: sqlite3.Connection | None = None
) -> dict | None:
    row = query_one(
        "SELECT * FROM maintenance_windows WHERE id = ?", (window_id,), conn=conn
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "device": row["device"],
        "name": row["name"],
        "start": row["start_ts"],
        "end": row["end_ts"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def fetch_maintenance_windows(
    start: float | None = None,
    end: float | None = None,
    device: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Windows overlapping the half-open filter window [start, end)."""
    where = []
    params: list = []
    if start is not None:
        where.append("end_ts > ?")
        params.append(start)
    if end is not None:
        where.append("start_ts < ?")
        params.append(end)
    if device:
        where.append("device = ?")
        params.append(device)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = query(
        f"SELECT id FROM maintenance_windows{clause} ORDER BY start_ts, device, id",
        params,
        conn=conn,
    )
    return [w for w in (fetch_maintenance_window(r["id"], conn=conn) for r in rows) if w]


def faults_for_export() -> list[dict]:
    rows = query(
        """
        SELECT f.*, r.name AS rule_name, r.logic, r.duration
        FROM fault_intervals f JOIN rules r ON r.id = f.rule_id
        ORDER BY f.start_ts, f.rule_id
        """
    )
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "logic": row["logic"],
                "start": row["start_ts"],
                "end": row["end_ts"],
                "duration_required": row["duration_required"],
                "confirmed_at": row["confirmed_at"],
                "evidence": json.loads(row["evidence_json"]),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Per-channel sample validity periods (TTL)
# --------------------------------------------------------------------------- #


def fetch_channel_ttl(device: str, metric: str) -> dict | None:
    row = query_one(
        "SELECT * FROM channel_ttls WHERE device = ? AND metric = ?",
        (device, metric),
    )
    if row is None:
        return None
    return {
        "device": row["device"],
        "metric": row["metric"],
        "ttl_seconds": row["ttl_seconds"],
        "updated_at": row["updated_at"],
    }


def fetch_channel_ttls(
    device: str | None = None,
    metric: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """All configured sample validity periods; channels without a row are absent
    (they keep the legacy hold-to-next-sample semantics)."""
    where = []
    params: list = []
    if device is not None:
        where.append("device = ?")
        params.append(device)
    if metric is not None:
        where.append("metric = ?")
        params.append(metric)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = query(
        f"SELECT device, metric, ttl_seconds, updated_at FROM channel_ttls{clause}"
        " ORDER BY device, metric",
        params,
        conn=conn,
    )
    return [dict(r) for r in rows]


def upsert_channel_ttl(device: str, metric: str, ttl_seconds: float) -> dict:
    """Set (or replace) a channel's positive sample validity period."""
    execute(
        """
        INSERT INTO channel_ttls (device, metric, ttl_seconds, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(device, metric) DO UPDATE SET
            ttl_seconds = excluded.ttl_seconds,
            updated_at = excluded.updated_at
        """,
        (device, metric, float(ttl_seconds), time.time()),
    )
    return fetch_channel_ttl(device, metric)


def delete_channel_ttl(device: str, metric: str) -> bool:
    """Clear a channel's validity period; returns True when a row was removed."""
    cur = execute(
        "DELETE FROM channel_ttls WHERE device = ? AND metric = ?",
        (device, metric),
    )
    return cur.rowcount > 0
