import os
import threading
from urllib.parse import urlparse

import requests

from app.utils.m3u_parser import extract_infohash, iter_extinf_entries
from app.utils import plugin_cache, plugin_store
from app.utils.logging_utils import log_event

COMPONENT = "plugin_refresh"
MAX_M3U_SIZE = 50 * 1024 * 1024


def _resolve_ipfs_url(url):
    """Rewrite IPFS/IPNS URLs to go through the local Kubo gateway."""
    if not url:
        return url
    path = urlparse(url).path
    for prefix in ('/ipns/', '/ipfs/'):
        idx = path.find(prefix)
        if idx >= 0:
            gateway = os.environ.get("IPFS_GATEWAY", "http://kubo:48080").rstrip('/')
            return f"{gateway}{path[idx:]}"
    return url

_timers = {}
_timers_lock = threading.Lock()


def _parse_m3u_to_channels(text):
    channels = []
    groups_seen = {}
    for attrs, url_line in iter_extinf_entries(text):
        infohash = extract_infohash(url_line)
        if not infohash:
            continue
        group = attrs.get("group", "")
        group_logo = attrs.get("group_logo", "")
        if group and group not in groups_seen:
            groups_seen[group] = group_logo
        channels.append({
            "name": attrs.get("name", "Unknown"),
            "infohash": infohash,
            "tvg_id": attrs.get("tvgid", ""),
            "tvg_logo": attrs.get("logo", ""),
            "group_title": group,
        })
    groups = [{"name": name, "logo_url": logo} for name, logo in groups_seen.items()]
    return channels, groups


def parse_m3u_text(text):
    return _parse_m3u_to_channels(text)


def fetch_and_cache(plugin):
    plugin_id = plugin["id"]
    source_url = plugin.get("source_url")
    if not source_url:
        plugin_store.update_refresh_status(plugin_id, "error", "no source URL", 0)
        return False

    resolved_url = _resolve_ipfs_url(source_url)
    try:
        resp = requests.get(resolved_url, timeout=60, stream=True)
        resp.raise_for_status()
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length > MAX_M3U_SIZE:
            resp.close()
            raise ValueError(f"M3U too large: {content_length} bytes")
        chunks = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=65536):
            downloaded += len(chunk)
            if downloaded > MAX_M3U_SIZE:
                resp.close()
                raise ValueError(f"M3U too large: >{MAX_M3U_SIZE} bytes")
            chunks.append(chunk)
        resp.close()
        text = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as e:
        error_msg = str(e)[:500]
        plugin_store.update_refresh_status(plugin_id, "error", error_msg, 0)
        log_event("error", "plugin_fetch_failed", COMPONENT,
                  plugin=plugin["name"], error=error_msg)
        return False

    channels, groups = _parse_m3u_to_channels(text)
    plugin_cache.set_channels(plugin_id, channels, groups)
    plugin_store.update_refresh_status(plugin_id, "ok", None, len(channels))
    log_event("info", "plugin_fetched", COMPONENT,
              plugin=plugin["name"], channels=len(channels))
    return True


def _timer_tick(plugin_id):
    plugin = plugin_store.get_by_id(plugin_id)
    if not plugin or not plugin["enabled"]:
        return
    try:
        fetch_and_cache(plugin)
    except Exception as e:
        log_event("error", "plugin_refresh_exception", COMPONENT,
                  plugin_id=plugin_id, error=str(e))
    finally:
        plugin = plugin_store.get_by_id(plugin_id)
        if plugin and plugin["enabled"]:
            _schedule_timer(plugin)


def _schedule_timer(plugin):
    plugin_id = plugin["id"]
    interval = plugin.get("refresh_interval", 3600)
    if not interval or interval <= 0:
        return
    t = threading.Timer(interval, _timer_tick, args=(plugin_id,))
    t.daemon = True
    with _timers_lock:
        old = _timers.pop(plugin_id, None)
        if old:
            old.cancel()
        _timers[plugin_id] = t
    t.start()


def start_plugin_timer(plugin):
    def _init():
        try:
            fetch_and_cache(plugin)
        except Exception as e:
            log_event("error", "plugin_init_fetch_error", COMPONENT,
                      plugin=plugin["name"], error=str(e))
        _schedule_timer(plugin)

    t = threading.Thread(target=_init,
                         name=f"plugin-init-{plugin['name']}", daemon=True)
    t.start()


def stop_plugin_timer(plugin_id):
    with _timers_lock:
        t = _timers.pop(plugin_id, None)
        if t:
            t.cancel()


def restart_plugin_timer(plugin):
    stop_plugin_timer(plugin["id"])
    if (plugin.get("enabled")
            and plugin.get("source_type") == "url"
            and plugin.get("source_url")):
        start_plugin_timer(plugin)


def bootstrap_all():
    plugins = plugin_store.get_all()
    for plugin in plugins:
        if (plugin["enabled"]
                and plugin["source_type"] == "url"
                and plugin.get("source_url")):
            start_plugin_timer(plugin)
            log_event("info", "plugin_timer_started", COMPONENT,
                      plugin=plugin["name"])
