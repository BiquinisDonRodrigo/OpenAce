import threading
import time
from datetime import datetime, timezone

from app.utils.check_store import _connect, _ensure_init, _lock
from app.utils.logging_utils import log_event

COMPONENT = "setup_store"

_setup_initialized = False
_setup_init_lock = threading.Lock()
_setup_complete_lock = threading.Lock()

_setup_complete_cache = None
_setup_complete_cache_ts = 0.0
_setup_cache_lock = threading.Lock()
_SETUP_CACHE_TTL_S = 5.0


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
    global _setup_complete_cache, _setup_complete_cache_ts
    now_mono = time.monotonic()
    with _setup_cache_lock:
        if (_setup_complete_cache is not None
                and now_mono - _setup_complete_cache_ts < _SETUP_CACHE_TTL_S):
            return not _setup_complete_cache
    _ensure_setup_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM setup_state WHERE key = 'setup_completed'"
        ).fetchone()
    finally:
        conn.close()
    completed = row is not None and row["value"] == "true"
    with _setup_cache_lock:
        _setup_complete_cache = completed
        _setup_complete_cache_ts = now_mono
    return not completed


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
        if key == "setup_completed":
            global _setup_complete_cache, _setup_complete_cache_ts
            with _setup_cache_lock:
                _setup_complete_cache = (value == "true")
                _setup_complete_cache_ts = time.monotonic()


def get_current_step():
    step = get_state("setup_step")
    return step if step else None


def complete_setup_atomic(finalize_fn):
    """Run finalize_fn() only if setup is still required, holding an
    inter-thread lock across the check-and-complete so concurrent setup
    completion requests cannot both succeed. finalize_fn must return
    (result, status_code) and is responsible for writing setup_completed=true.
    Returns (None, 409) if setup is already complete.
    """
    with _setup_complete_lock:
        if not is_setup_required():
            return None, 409
        return finalize_fn()
