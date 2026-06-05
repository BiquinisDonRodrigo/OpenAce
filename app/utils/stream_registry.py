import threading
import time

_lock = threading.Lock()
_active = {}


def register(content_id, fmt):
    with _lock:
        key = (content_id, fmt)
        entry = _active.get(key)
        if entry:
            entry["clients"] += 1
        else:
            _active[key] = {"clients": 1, "started_at": time.time()}


def unregister(content_id, fmt):
    with _lock:
        key = (content_id, fmt)
        entry = _active.get(key)
        if not entry:
            return
        entry["clients"] -= 1
        if entry["clients"] <= 0:
            del _active[key]


def get_active():
    with _lock:
        return [
            {
                "content_id": cid,
                "format": fmt,
                "clients": e["clients"],
                "started_at": e["started_at"],
            }
            for (cid, fmt), e in _active.items()
        ]
