import threading
from datetime import datetime, timezone

from app.utils.check_store import _connect, _ensure_init, _lock
from app.utils.logging_utils import log_event

COMPONENT = "setup_store"

_setup_initialized = False
_setup_init_lock = threading.Lock()
_setup_done = None


def _ensure_setup_init():
    global _setup_initialized
    if _setup_initialized:
        return
    _ensure_init()
    with _setup_init_lock:
        if _setup_initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS setup_state (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _setup_initialized = True
        log_event("info", "setup_schema_ready", COMPONENT)


def is_setup_required():
    global _setup_done
    if _setup_done is not None:
        return not _setup_done
    _ensure_setup_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM setup_state WHERE key = 'setup_completed'"
        ).fetchone()
    finally:
        conn.close()
    _setup_done = row is not None and row["value"] == "true"
    return not _setup_done


def get_state(key):
    _ensure_setup_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM setup_state WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def set_state(key, value):
    global _setup_done
    _ensure_setup_init()
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO setup_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            conn.commit()
        finally:
            conn.close()
    _setup_done = None


def get_current_step():
    step = get_state("setup_step")
    return step if step else None
