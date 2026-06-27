import threading
import time

HLS_CLIENT_TTL_S = 30

_lock = threading.Lock()
_active = {}
_hls_clients = {}


def register(content_id, fmt, client_ip=None):
    with _lock:
        key = (content_id, fmt)
        entry = _active.get(key)
        if entry:
            entry["clients"] += 1
        else:
            entry = {
                "clients": 1,
                "started_at": time.time(),
                "client_ips": {},
            }
            _active[key] = entry
        if client_ip:
            entry["client_ips"][client_ip] = entry["client_ips"].get(client_ip, 0) + 1


def unregister(content_id, fmt, client_ip=None):
    with _lock:
        key = (content_id, fmt)
        entry = _active.get(key)
        if not entry:
            return
        entry["clients"] -= 1
        if client_ip:
            current = entry["client_ips"].get(client_ip, 0) - 1
            if current > 0:
                entry["client_ips"][client_ip] = current
            else:
                entry["client_ips"].pop(client_ip, None)
        if entry["clients"] <= 0:
            del _active[key]


def _reap_expired_locked(now=None):
    now = time.monotonic() if now is None else now
    expired_content_ids = []
    for content_id, clients in list(_hls_clients.items()):
        for client_id, entry in list(clients.items()):
            if now - entry["last_seen"] > HLS_CLIENT_TTL_S:
                del clients[client_id]
        if not clients:
            expired_content_ids.append(content_id)
    for content_id in expired_content_ids:
        _hls_clients.pop(content_id, None)


def reap_expired(now=None):
    with _lock:
        _reap_expired_locked(now)


def touch_hls_client(content_id, client_id, client_ip=None, user_agent=None):
    now = time.monotonic()
    with _lock:
        clients = _hls_clients.setdefault(content_id, {})
        entry = clients.get(client_id)
        if entry is None:
            clients[client_id] = {
                "client_ip": client_ip,
                "started_at": time.time(),
                "last_seen": now,
                "user_agent": user_agent or "",
            }
            return
        entry["last_seen"] = now
        if client_ip:
            entry["client_ip"] = client_ip
        if user_agent is not None:
            entry["user_agent"] = user_agent


def clear_hls(content_id):
    with _lock:
        _hls_clients.pop(content_id, None)


def get_active():
    with _lock:
        _reap_expired_locked()
        active = [
            {
                "content_id": cid,
                "format": fmt,
                "clients": e["clients"],
                "started_at": e["started_at"],
                "client_ips": sorted(e["client_ips"]),
            }
            for (cid, fmt), e in _active.items()
        ]
        for cid, clients in _hls_clients.items():
            if not clients:
                continue
            active.append({
                "content_id": cid,
                "format": "hls",
                "clients": len(clients),
                "started_at": min(e["started_at"] for e in clients.values()),
                "client_ips": sorted({e["client_ip"] for e in clients.values() if e.get("client_ip")}),
            })
        return active


def has_hls_clients(content_id):
    """Return True if there are active HLS clients for *content_id*."""
    with _lock:
        clients = _hls_clients.get(content_id)
        return bool(clients)
