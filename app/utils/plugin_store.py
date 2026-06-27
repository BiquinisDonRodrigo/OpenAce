from datetime import datetime, timezone

from app.utils.check_store import _connect, _ensure_init, _lock

COMPONENT = "plugin_store"

_ALLOWED_FIELDS = [
    "name", "display_name", "source_type", "source_url",
    "refresh_interval", "acestream_ip", "acestream_port",
    "output_format", "enabled",
]


def _row_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "display_name": row["display_name"],
        "source_type": row["source_type"],
        "source_url": row["source_url"],
        "refresh_interval": row["refresh_interval"],
        "acestream_ip": row["acestream_ip"],
        "acestream_port": row["acestream_port"],
        "output_format": row["output_format"],
        "enabled": bool(row["enabled"]),
        "last_refresh": row["last_refresh"],
        "last_status": row["last_status"],
        "last_error": row["last_error"],
        "channel_count": row["channel_count"],
        "etag": row["etag"] if "etag" in row.keys() else None,
        "last_modified": row["last_modified"] if "last_modified" in row.keys() else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_all():
    _ensure_init()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM plugins ORDER BY display_name").fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_by_id(plugin_id):
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM plugins WHERE id = ?", (plugin_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def get_by_name(name):
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM plugins WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def create(data):
    _ensure_init()
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """INSERT INTO plugins (name, display_name, source_type, source_url,
                   refresh_interval, acestream_ip, acestream_port, output_format,
                   enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   RETURNING *""",
                (
                    data["name"],
                    data["display_name"],
                    data.get("source_type", "url"),
                    data.get("source_url"),
                    data.get("refresh_interval", 3600),
                    data.get("acestream_ip"),
                    data.get("acestream_port"),
                    data.get("output_format", "ace"),
                    1 if data.get("enabled", True) else 0,
                    now,
                    now,
                ),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def update(plugin_id, data):
    _ensure_init()
    now = datetime.now(timezone.utc).isoformat()
    fields = []
    params = []
    for key in _ALLOWED_FIELDS:
        if key in data:
            value = data[key]
            if key == "enabled":
                value = 1 if value else 0
            fields.append(f"{key} = ?")
            params.append(value)
    if not fields:
        return get_by_id(plugin_id)
    fields.append("updated_at = ?")
    params.append(now)
    params.append(plugin_id)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"UPDATE plugins SET {', '.join(fields)} WHERE id = ? RETURNING *",
                params,
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def delete(plugin_id):
    _ensure_init()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute("DELETE FROM plugins WHERE id = ?", (plugin_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def update_refresh_status(plugin_id, status, error, channel_count, *,
                           etag=None, last_modified=None):
    _ensure_init()
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """UPDATE plugins SET last_refresh = ?, last_status = ?,
                   last_error = ?, channel_count = ?, updated_at = ?,
                   etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified)
                   WHERE id = ?""",
                (now, status, error, channel_count, now, etag, last_modified, plugin_id),
            )
            conn.commit()
        finally:
            conn.close()


