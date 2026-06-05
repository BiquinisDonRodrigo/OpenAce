import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_channel_cache = {}


def get_channels(plugin_id):
    with _lock:
        entry = _channel_cache.get(plugin_id)
        return list(entry["channels"]) if entry else []


def get_groups(plugin_id):
    with _lock:
        entry = _channel_cache.get(plugin_id)
        return list(entry.get("groups", [])) if entry else []


def set_channels(plugin_id, channels, groups=None):
    with _lock:
        _channel_cache[plugin_id] = {
            "channels": list(channels),
            "groups": list(groups or []),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }


def remove(plugin_id):
    with _lock:
        _channel_cache.pop(plugin_id, None)


def get_entry(plugin_id):
    with _lock:
        entry = _channel_cache.get(plugin_id)
        return dict(entry) if entry else None


def get_all():
    with _lock:
        return {k: dict(v) for k, v in _channel_cache.items()}
