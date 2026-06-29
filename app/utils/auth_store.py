import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

from app.utils import environment_store
from app.utils.check_store import _connect, _ensure_init, _lock
from app.utils.logging_utils import log_event

COMPONENT = "auth_store"

_auth_initialized = False
_auth_init_lock = threading.Lock()

_login_attempts = {}
_rate_lock = threading.Lock()

_api_write_attempts = {}
_api_rate_lock = threading.Lock()
_API_RATE_WINDOW_S = 60
_API_RATE_MAX_REQUESTS = 60
_last_api_purge = 0.0

_basic_auth_cache = {}
_basic_auth_cache_lock = threading.Lock()
_BASIC_AUTH_TTL_S = 30
_last_basic_auth_purge = 0.0


def _password_cache_digest(password):
    return hashlib.sha256(password.encode("utf-8", errors="surrogatepass")).hexdigest()


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _invalidate_basic_auth_cache(username=None):
    with _basic_auth_cache_lock:
        if username:
            for key in list(_basic_auth_cache):
                if key[0] == username:
                    _basic_auth_cache.pop(key, None)
        else:
            _basic_auth_cache.clear()


def _ensure_auth_init():
    global _auth_initialized
    if _auth_initialized:
        return
    _ensure_init()
    with _auth_init_lock:
        if _auth_initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_login TEXT,
                    expires_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    token TEXT UNIQUE NOT NULL,
                    description TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    expires_at TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip_address TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_tokens_token ON api_tokens(token)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)"
            )
            conn.commit()
        finally:
            conn.close()
        _auth_initialized = True
        log_event("info", "auth_schema_ready", COMPONENT)


def _user_row_to_dict(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "last_login": row["last_login"],
        "expires_at": row["expires_at"],
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username, password, role="user", expires_at=None):
    _ensure_auth_init()
    if role not in ("admin", "user", "viewer"):
        role = "user"
    pw_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id, username, role, enabled, created_at, last_login, expires_at",
                (username, pw_hash, role, now, expires_at),
            )
            row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    return _user_row_to_dict(row) if row else None


def get_user_by_id(user_id):
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return _user_row_to_dict(row) if row else None


def get_user_by_username(username):
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    return _user_row_to_dict(row) if row else None


def get_all_users():
    _ensure_auth_init()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    finally:
        conn.close()
    return [_user_row_to_dict(r) for r in rows]


def update_user(user_id, data):
    _ensure_auth_init()
    fields = []
    params = []
    for key in ("username", "role", "enabled"):
        if key in data:
            value = data[key]
            if key == "enabled":
                value = 1 if _parse_bool(value) else 0
            if key == "role" and value not in ("admin", "user", "viewer"):
                raise ValueError("Rol inválido: %r" % (value,))
            fields.append(f"{key} = ?")
            params.append(value)
    if "password" in data and data["password"]:
        fields.append("password_hash = ?")
        params.append(generate_password_hash(data["password"]))
    if "expires_at" in data:
        exp = data["expires_at"] or None
        if exp is not None:
            try:
                _parse_iso(exp)
            except (ValueError, TypeError):
                raise ValueError("expires_at no es una fecha ISO válida: %r" % (exp,))
        fields.append("expires_at = ?")
        params.append(exp)
    if not fields:
        return get_user_by_id(user_id)
    params.append(user_id)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params
            )
            conn.commit()
        finally:
            conn.close()
    _invalidate_basic_auth_cache()
    return get_user_by_id(user_id)


def delete_user(user_id):
    _ensure_auth_init()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (user_id,))
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()
            deleted = cur.rowcount > 0
        finally:
            conn.close()
    if deleted:
        _invalidate_basic_auth_cache()
    return deleted


def _parse_iso(s):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_user_expired(user):
    exp = user.get("expires_at")
    if not exp:
        return False
    try:
        return _parse_iso(exp) < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        log_event("warning", "user_expiry_parse_failed", COMPONENT,
                  user_id=user.get("id"), expires_at=exp)
        return True


def verify_password(username, password):
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND enabled = 1", (username,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    user = _user_row_to_dict(row)
    if is_user_expired(user):
        return None
    return user


def verify_password_cached(username, password):
    """verify_password with a short TTL cache to avoid re-running the KDF
    on every Basic-Auth request (IPTV clients send credentials per request)."""
    now = time.time()
    cache_key = (username, _password_cache_digest(password))
    with _basic_auth_cache_lock:
        _purge_basic_auth_cache(now)
        entry = _basic_auth_cache.get(cache_key)
    if entry and now - entry[1] < _BASIC_AUTH_TTL_S:
        return entry[0]
    user = verify_password(username, password)
    if user:
        with _basic_auth_cache_lock:
            _basic_auth_cache[cache_key] = (user, now)
    return user


def _purge_basic_auth_cache(now):
    global _last_basic_auth_purge
    if now - _last_basic_auth_purge < _BASIC_AUTH_TTL_S:
        return
    _last_basic_auth_purge = now
    stale = [key for key, (_, ts) in _basic_auth_cache.items()
             if now - ts >= _BASIC_AUTH_TTL_S]
    for key in stale:
        _basic_auth_cache.pop(key, None)


def update_last_login(user_id):
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?", (now, user_id)
            )
            conn.commit()
        finally:
            conn.close()


def user_count():
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
    finally:
        conn.close()
    return row["c"] if row else 0


def has_users():
    return user_count() > 0


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(user_id, ip_address, duration_hours=24):
    _ensure_auth_init()
    session_id = str(uuid4())
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=duration_hours)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, created_at, expires_at, ip_address) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, now.isoformat(), expires.isoformat(), ip_address),
            )
            conn.commit()
        finally:
            conn.close()
    return session_id


def get_session(session_id):
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        expires_at = _parse_iso(row["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            with _lock:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
            return None
    finally:
        conn.close()
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "ip_address": row["ip_address"],
    }


def get_session_with_user(session_id):
    """Return (session_dict, user_dict) in a single JOINed query, or (None, None).
    Expired sessions are deleted in the same connection.
    """
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT s.id AS s_id, s.user_id, s.created_at AS s_created_at,
                   s.expires_at, s.ip_address,
                   u.id AS u_id, u.username, u.password_hash, u.role,
                   u.enabled, u.created_at AS u_created_at, u.last_login,
                   u.expires_at AS u_expires_at
            FROM sessions s JOIN users u ON s.user_id = u.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            return None, None
        expires_at = _parse_iso(row["expires_at"])
        if expires_at < datetime.now(timezone.utc):
            with _lock:
                conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                conn.commit()
            return None, None
    finally:
        conn.close()
    session = {
        "id": row["s_id"],
        "user_id": row["user_id"],
        "created_at": row["s_created_at"],
        "expires_at": row["expires_at"],
        "ip_address": row["ip_address"],
    }
    user = {
        "id": row["u_id"],
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "created_at": row["u_created_at"],
        "last_login": row["last_login"],
        "expires_at": row["u_expires_at"],
    }
    return session, user


def renew_session(session_id, duration_hours=24):
    now = datetime.now(timezone.utc)
    new_expires = (now + timedelta(hours=duration_hours)).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE id = ?",
                (new_expires, session_id),
            )
            conn.commit()
        finally:
            conn.close()


def delete_session(session_id):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def delete_user_sessions(user_id):
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()


def cleanup_expired_sessions():
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# API Tokens
# ---------------------------------------------------------------------------

def create_token(user_id, description=None, expires_at=None):
    _ensure_auth_init()
    token_value = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO api_tokens (user_id, token, description, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, token_value, description, now, expires_at),
            )
            conn.commit()
            token_id = cur.lastrowid
        finally:
            conn.close()
    return {
        "id": token_id,
        "token": token_value,
        "user_id": user_id,
        "description": description,
        "created_at": now,
        "expires_at": expires_at,
    }


def get_token_by_value(token_value):
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token = ? AND enabled = 1",
            (token_value,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["expires_at"]:
        try:
            if _parse_iso(row["expires_at"]) < datetime.now(timezone.utc):
                return None
        except (ValueError, TypeError):
            return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "token": row["token"],
        "description": row["description"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "enabled": bool(row["enabled"]),
    }


def get_token_with_user(token_value):
    """Return (token_dict, user_dict) in a single JOINed query, or (None, None)."""
    _ensure_auth_init()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT t.id AS t_id, t.user_id, t.token, t.description,
                   t.created_at AS t_created_at, t.expires_at AS t_expires_at,
                   t.enabled AS t_enabled,
                   u.id AS u_id, u.username, u.password_hash, u.role,
                   u.enabled AS u_enabled, u.created_at AS u_created_at,
                   u.last_login, u.expires_at AS u_expires_at
            FROM api_tokens t JOIN users u ON t.user_id = u.id
            WHERE t.token = ? AND t.enabled = 1
            """,
            (token_value,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, None
    if row["t_expires_at"]:
        try:
            if _parse_iso(row["t_expires_at"]) < datetime.now(timezone.utc):
                return None, None
        except (ValueError, TypeError):
            return None, None
    token = {
        "id": row["t_id"],
        "user_id": row["user_id"],
        "token": row["token"],
        "description": row["description"],
        "created_at": row["t_created_at"],
        "expires_at": row["t_expires_at"],
        "enabled": bool(row["t_enabled"]),
    }
    user = {
        "id": row["u_id"],
        "username": row["username"],
        "role": row["role"],
        "enabled": bool(row["u_enabled"]),
        "created_at": row["u_created_at"],
        "last_login": row["last_login"],
        "expires_at": row["u_expires_at"],
    }
    return token, user


def get_all_tokens():
    _ensure_auth_init()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT t.id, t.user_id, u.username,
                      substr(t.token, 1, 8) || '...' as token_preview,
                      t.description, t.created_at, t.expires_at, t.enabled
               FROM api_tokens t JOIN users u ON t.user_id = u.id
               ORDER BY t.created_at DESC"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_token(token_id):
    _ensure_auth_init()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Rate Limiting (in-memory, 5 attempts per IP per 5 minutes)
# ---------------------------------------------------------------------------

def check_rate_limit(ip):
    now = time.time()
    with _rate_lock:
        _purge_stale_ips(now)
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        _login_attempts[ip] = attempts
        return len(attempts) < 5


def record_failed_attempt(ip):
    now = time.time()
    with _rate_lock:
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        attempts.append(now)
        _login_attempts[ip] = attempts


def check_and_record_login_attempt(ip):
    now = time.time()
    with _rate_lock:
        _purge_stale_ips(now)
        attempts = _login_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < 300]
        if len(attempts) >= 5:
            _login_attempts[ip] = attempts
            return False
        attempts.append(now)
        _login_attempts[ip] = attempts
        return True


def clear_login_attempts(ip):
    with _rate_lock:
        _login_attempts.pop(ip, None)


_last_purge = 0.0


def _purge_stale_ips(now):
    global _last_purge
    if now - _last_purge < 600:
        return
    _last_purge = now
    stale = [ip for ip, ts_list in _login_attempts.items()
             if not ts_list or now - max(ts_list) >= 300]
    for ip in stale:
        del _login_attempts[ip]


def check_api_rate_limit(ip):
    """Throttle write requests (POST/PUT/DELETE) per IP. 60 req/min."""
    now = time.time()
    with _api_rate_lock:
        _purge_stale_api_ips(now)
        attempts = _api_write_attempts.get(ip, [])
        attempts = [t for t in attempts if now - t < _API_RATE_WINDOW_S]
        _api_write_attempts[ip] = attempts
        if len(attempts) >= _API_RATE_MAX_REQUESTS:
            return False
        attempts.append(now)
        _api_write_attempts[ip] = attempts
        return True


def _purge_stale_api_ips(now):
    """Garbage-collect IPs with no recent API write attempts."""
    global _last_api_purge
    if now - _last_api_purge < 600:
        return
    _last_api_purge = now
    stale = [ip for ip, ts_list in _api_write_attempts.items()
             if not ts_list or now - max(ts_list) >= _API_RATE_WINDOW_S]
    for ip in stale:
        del _api_write_attempts[ip]


# ---------------------------------------------------------------------------
# Initial Admin Setup
# ---------------------------------------------------------------------------

def ensure_admin_exists():
    _ensure_auth_init()
    if user_count() > 0:
        return None
    admin_user = environment_store.get_str("OPENACE_ADMIN_USER") or "admin"
    admin_pass_env = environment_store.get_str("OPENACE_ADMIN_PASSWORD")
    admin_pass = admin_pass_env or secrets.token_urlsafe(12)
    create_user(admin_user, admin_pass, role="admin")
    log_event("info", "admin_user_created", COMPONENT, username=admin_user)
    if admin_pass_env:
        return None
    return admin_pass
