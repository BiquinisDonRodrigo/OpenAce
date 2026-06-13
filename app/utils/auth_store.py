import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from werkzeug.security import check_password_hash, generate_password_hash

from app.utils.check_store import _connect, _ensure_init, _lock
from app.utils.logging_utils import log_event

COMPONENT = "auth_store"

_auth_initialized = False
_auth_init_lock = threading.Lock()

_login_attempts = {}
_rate_lock = threading.Lock()


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
                    last_login TEXT
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
                "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)"
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
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username, password, role="user"):
    _ensure_auth_init()
    if role not in ("admin", "user", "viewer"):
        role = "user"
    pw_hash = generate_password_hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, pw_hash, role, now),
            )
            conn.commit()
            user_id = cur.lastrowid
        finally:
            conn.close()
    return get_user_by_id(user_id)


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
                value = 1 if value else 0
            if key == "role" and value not in ("admin", "user", "viewer"):
                continue
            fields.append(f"{key} = ?")
            params.append(value)
    if "password" in data and data["password"]:
        fields.append("password_hash = ?")
        params.append(generate_password_hash(data["password"]))
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
            return cur.rowcount > 0
        finally:
            conn.close()


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
    return _user_row_to_dict(row)


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
    finally:
        conn.close()
    if row is None:
        return None
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        delete_session(session_id)
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "ip_address": row["ip_address"],
    }


def renew_session(session_id, duration_hours=24):
    new_expires = (
        datetime.now(timezone.utc) + timedelta(hours=duration_hours)
    ).isoformat()
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
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
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


# ---------------------------------------------------------------------------
# Initial Admin Setup
# ---------------------------------------------------------------------------

def ensure_admin_exists():
    _ensure_auth_init()
    if user_count() > 0:
        return None
    admin_user = os.environ.get("OPENACE_ADMIN_USER", "admin")
    admin_pass_env = os.environ.get("OPENACE_ADMIN_PASSWORD")
    admin_pass = admin_pass_env or secrets.token_urlsafe(12)
    create_user(admin_user, admin_pass, role="admin")
    log_event("info", "admin_user_created", COMPONENT, username=admin_user)
    if admin_pass_env:
        return None
    return admin_pass
