import os
import sqlite3
import threading
import time

from app.utils.logging_utils import log_event

COMPONENT = "check_store"

DB_PATH = os.environ.get("DB_PATH", "/openace/checkdb/data.db")

_VALID_STATUSES = {"live", "dead", "timeout", "error", "skipped"}

_lock = threading.Lock()
_initialised = False

_pool = []
_pool_lock = threading.Lock()
_POOL_MAX = 10


class _PooledConn:
    """Proxy around sqlite3.Connection whose close() returns it to the pool
    instead of closing the underlying handle. All attribute access delegates
    to the real connection, so existing store code works unchanged."""

    __slots__ = ("_conn",)

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        with _pool_lock:
            if len(_pool) < _POOL_MAX:
                _pool.append(self._conn)
            else:
                try:
                    self._conn.close()
                except Exception:
                    pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


def _new_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA cache_size=-8192")
    return conn


def _connect():
    with _pool_lock:
        while _pool:
            conn = _pool.pop()
            try:
                conn.execute("SELECT 1")
                return _PooledConn(conn)
            except sqlite3.Error:
                try:
                    conn.close()
                except Exception:
                    pass
    return _PooledConn(_new_connection())


def _ensure_init():
    """Create the data dir + schema once. Cheap and idempotent after first call."""
    global _initialised
    if _initialised:
        return
    with _lock:
        if _initialised:
            return
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    infohash    TEXT PRIMARY KEY,
                    name        TEXT,
                    group_title TEXT,
                    plugin      TEXT,
                    status      TEXT,
                    last_check  INTEGER,
                    response_ms INTEGER,
                    peers       INTEGER,
                    speed       INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eula_consents (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    accepted_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ip            TEXT,
                    user_agent    TEXT,
                    eula_version  TEXT NOT NULL DEFAULT '1.0',
                    phrase_hash   TEXT NOT NULL,
                    revoked_at    DATETIME DEFAULT NULL,
                    revoked_ip    TEXT DEFAULT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_eula_ip
                    ON eula_consents (ip, revoked_at)
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_eula_active ON eula_consents(id) WHERE revoked_at IS NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS plugins (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT UNIQUE NOT NULL,
                    display_name    TEXT NOT NULL,
                    source_type     TEXT NOT NULL DEFAULT 'url',
                    source_url      TEXT,
                    refresh_interval INTEGER DEFAULT 3600,
                    acestream_ip    TEXT DEFAULT NULL,
                    acestream_port  INTEGER DEFAULT NULL,
                    output_format   TEXT DEFAULT 'ace',
                    enabled         INTEGER DEFAULT 1,
                    last_refresh    TEXT,
                    last_status     TEXT DEFAULT 'pending',
                    last_error      TEXT,
                    channel_count   INTEGER DEFAULT 0,
                    etag            TEXT,
                    last_modified   TEXT,
                    created_at      TEXT DEFAULT (datetime('now')),
                    updated_at      TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_status ON channels(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_plugin ON channels(plugin)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_group ON channels(group_title)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_unchecked ON channels(infohash) WHERE status IS NULL")
            for col in ("etag", "last_modified"):
                try:
                    conn.execute(f"ALTER TABLE plugins ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()
        _initialised = True
        log_event("info", "db_ready", COMPONENT, path=DB_PATH)


def _row_to_dict(row):
    return {
        "infohash": row["infohash"],
        "name": row["name"],
        "group": row["group_title"],
        "plugin": row["plugin"],
        "status": row["status"],
        "last_check": row["last_check"],
        "response_ms": row["response_ms"],
        "peers": row["peers"],
        "speed": row["speed"],
    }


def purge_stale(current_infohashes):
    """Remove channel rows whose infohash is not in the provided set."""
    _ensure_init()
    if not current_infohashes:
        return
    with _lock:
        conn = _connect()
        try:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep (h TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _keep")
            conn.executemany("INSERT OR IGNORE INTO _keep VALUES (?)",
                             [(h,) for h in current_infohashes])
            conn.execute("DELETE FROM channels WHERE infohash NOT IN (SELECT h FROM _keep)")
            conn.commit()
        finally:
            conn.close()


def sync_catalog(channels):
    """Upsert channel metadata from the plugin registry without clobbering results.

    ``channels`` is an iterable of dicts with ``infohash``/``name``/``group``/
    ``plugin``. New rows get NULL status; existing rows keep their status and
    last-check fields while their metadata is refreshed.
    """
    _ensure_init()
    rows = [
        (c["infohash"], c.get("name"), c.get("group"), c.get("plugin"))
        for c in channels
        if c.get("infohash")
    ]
    if not rows:
        return
    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                """
                INSERT INTO channels (infohash, name, group_title, plugin)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(infohash) DO UPDATE SET
                    name = excluded.name,
                    group_title = excluded.group_title,
                    plugin = excluded.plugin
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()


def record_result(infohash, status, response_ms, peers, speed, *,
                  name=None, group=None, plugin=None):
    """Persist the outcome of a single check, stamping last_check with now."""
    _ensure_init()
    if status not in _VALID_STATUSES:
        status = "error"
    now = int(time.time())
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO channels (infohash, name, group_title, plugin,
                                      status, last_check, response_ms, peers, speed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(infohash) DO UPDATE SET
                    status = excluded.status,
                    last_check = excluded.last_check,
                    response_ms = excluded.response_ms,
                    peers = excluded.peers,
                    speed = excluded.speed
                """,
                (infohash, name, group, plugin, status, now, response_ms, peers, speed),
            )
            conn.commit()
        finally:
            conn.close()


def record_results_batch(results):
    """Persist multiple check outcomes in a single transaction."""
    _ensure_init()
    now = int(time.time())
    rows = []
    for r in results:
        status = r["outcome"]
        if status not in _VALID_STATUSES:
            status = "error"
        rows.append((
            r["infohash"], r.get("name"), r.get("group"), r.get("plugin"),
            status, now, r["response_ms"], r["peers"], r["speed"],
        ))
    if not rows:
        return
    with _lock:
        conn = _connect()
        try:
            conn.executemany(
                """
                INSERT INTO channels (infohash, name, group_title, plugin,
                                      status, last_check, response_ms, peers, speed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(infohash) DO UPDATE SET
                    status = excluded.status,
                    last_check = excluded.last_check,
                    response_ms = excluded.response_ms,
                    peers = excluded.peers,
                    speed = excluded.speed
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()


def get_results(status=None, plugin=None, group=None):
    """Return cached rows (optionally filtered) ordered by name.

    ``status`` accepts a concrete status, ``"unchecked"`` (never validated), or
    ``None``/``"all"`` for everything.
    """
    _ensure_init()
    clauses = []
    params = []
    if status and status != "all":
        if status == "unchecked":
            clauses.append("status IS NULL")
        else:
            clauses.append("status = ?")
            params.append(status)
    if plugin and plugin != "all":
        clauses.append("plugin = ?")
        params.append(plugin)
    if group and group != "all":
        clauses.append("group_title = ?")
        params.append(group)

    sql = "SELECT * FROM channels"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY LOWER(name)"

    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_one(infohash):
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM channels WHERE infohash = ?", (infohash,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def distinct_plugins():
    return _distinct("plugin")


def distinct_groups():
    return _distinct("group_title")


def _distinct(column):
    _ensure_init()
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM channels "
            f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY LOWER({column})"
        ).fetchall()
    finally:
        conn.close()
    return [r["v"] for r in rows]
