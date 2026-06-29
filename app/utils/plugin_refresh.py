import ipaddress
import os
import random
import socket
import threading
import time
from urllib.parse import urlparse

import requests

from app.utils.m3u_parser import extract_infohash, iter_extinf_entries
from app.utils import environment_store, plugin_cache, plugin_store
from app.utils.logging_utils import log_event
from app.utils.upstream import session

COMPONENT = "plugin_refresh"
MAX_M3U_SIZE = 50 * 1024 * 1024

_ssrf_cache = {}
_ssrf_cache_lock = threading.Lock()
_ssrf_pin_lock = threading.Lock()
_SSRF_CACHE_TTL_S = 300
_SSRF_CACHE_MAX = 1024


def _purge_ssrf_cache_locked(now):
    stale = [key for key, entry in _ssrf_cache.items() if now - entry[2] >= _SSRF_CACHE_TTL_S]
    for key in stale:
        _ssrf_cache.pop(key, None)
    while len(_ssrf_cache) > _SSRF_CACHE_MAX:
        oldest = min(_ssrf_cache, key=lambda key: _ssrf_cache[key][2])
        _ssrf_cache.pop(oldest, None)


def _resolve_ipfs_url(url):
    """Rewrite IPFS/IPNS URLs to go through the local Kubo gateway."""
    if not url:
        return url
    parsed = urlparse(url)
    path = parsed.path
    for prefix in ('/ipns/', '/ipfs/'):
        idx = path.find(prefix)
        if idx >= 0:
            gateway = environment_store.get_str("IPFS_GATEWAY").rstrip('/')
            rewritten = f"{gateway}{path[idx:]}"
            if parsed.query:
                rewritten += f"?{parsed.query}"
            if parsed.fragment:
                rewritten += f"#{parsed.fragment}"
            return rewritten
    return url


def _is_safe_source_url(url):
    """Reject URLs commonly used for SSRF (loopback, link-local/cloud-metadata).
    Private IPs are allowed because this app commonly runs in Docker/LAN
    setups where M3U sources live on the local network. Redirects are
    separately disabled at the requests.get call site.
    """
    resolved_url, _ = _resolve_safe_source(url)
    return resolved_url is not None


def _default_port(parsed):
    try:
        port = parsed.port
    except ValueError:
        return None
    if port:
        return port
    return 443 if parsed.scheme == "https" else 80


def _check_ssrf(url):
    return _resolve_safe_addrinfos(url) is not None


def _resolve_safe_addrinfos(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    port = _default_port(parsed)
    if port is None:
        return None
    try:
        addrinfos = socket.getaddrinfo(hostname, port)
    except (socket.gaierror, socket.herror):
        return None
    checked_any = False
    for family, _, _, _, sockaddr in addrinfos:
        ip = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue
        checked_any = True
        if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_unspecified:
            return None
    return addrinfos if checked_any else None


def _resolve_safe_source(url):
    resolved_url = _resolve_ipfs_url(url)
    if not resolved_url:
        return None, None
    now = time.time()
    with _ssrf_cache_lock:
        _purge_ssrf_cache_locked(now)
        entry = _ssrf_cache.get(resolved_url)
        if entry and now - entry[2] < _SSRF_CACHE_TTL_S:
            return (resolved_url, entry[1]) if entry[0] else (None, None)
    addrinfos = _resolve_safe_addrinfos(resolved_url)
    ok = addrinfos is not None
    with _ssrf_cache_lock:
        _ssrf_cache[resolved_url] = (ok, addrinfos, now)
        _purge_ssrf_cache_locked(now)
    return (resolved_url, addrinfos) if ok else (None, None)


def _get_with_pinned_dns(url, addrinfos, **kwargs):
    parsed = urlparse(url)
    hostname = parsed.hostname
    original_getaddrinfo = socket.getaddrinfo

    def pinned_getaddrinfo(host, port, *args, **inner_kwargs):
        if host == hostname:
            return addrinfos
        return original_getaddrinfo(host, port, *args, **inner_kwargs)

    with _ssrf_pin_lock:
        socket.getaddrinfo = pinned_getaddrinfo
        try:
            return session.get(url, **kwargs)
        finally:
            socket.getaddrinfo = original_getaddrinfo

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

    resolved_url, addrinfos = _resolve_safe_source(source_url)
    if not resolved_url:
        plugin_store.update_refresh_status(plugin_id, "error", "source URL blocked (SSRF guard)", 0)
        log_event("warning", "plugin_source_blocked", COMPONENT,
                  plugin=plugin["name"], source_url=source_url[:200])
        return False

    headers = {}
    cached_channels = plugin_cache.get_channels(plugin_id)
    if cached_channels:
        etag = plugin.get("etag")
        last_modified = plugin.get("last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
    try:
        with _get_with_pinned_dns(resolved_url, addrinfos, timeout=60, stream=True,
                                  allow_redirects=False, headers=headers) as resp:
            if resp.status_code == 304:
                plugin_store.update_refresh_status(plugin_id, "ok", None,
                                                    plugin.get("channel_count", 0))
                log_event("info", "plugin_not_modified", COMPONENT,
                          plugin=plugin["name"])
                return True
            if resp.is_redirect or resp.is_permanent_redirect:
                resp.close()
                raise ValueError(f"Redirects not allowed for plugin source: {resp.status_code}")
            resp.raise_for_status()
            try:
                content_length = int(resp.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                content_length = 0
            if content_length > MAX_M3U_SIZE:
                raise ValueError(f"M3U too large: {content_length} bytes")
            new_etag = resp.headers.get("ETag")
            new_last_modified = resp.headers.get("Last-Modified")
            chunks = []
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=65536):
                downloaded += len(chunk)
                if downloaded > MAX_M3U_SIZE:
                    raise ValueError(f"M3U too large: >{MAX_M3U_SIZE} bytes")
                chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as e:
        error_msg = str(e)[:500]
        plugin_store.update_refresh_status(plugin_id, "error", error_msg, 0)
        log_event("error", "plugin_fetch_failed", COMPONENT,
                  plugin=plugin["name"], error=error_msg)
        return False

    channels, groups = _parse_m3u_to_channels(text)
    plugin_cache.set_channels(plugin_id, channels, groups)
    plugin_store.update_refresh_status(plugin_id, "ok", None, len(channels),
                                        etag=new_etag, last_modified=new_last_modified)
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
            try:
                _schedule_timer(plugin)
            except Exception as e:
                log_event("error", "plugin_reschedule_failed", COMPONENT,
                          plugin_id=plugin_id, error=str(e))


def _schedule_timer(plugin):
    plugin_id = plugin["id"]
    try:
        interval = int(plugin.get("refresh_interval", 3600) or 3600)
    except (TypeError, ValueError):
        log_event("warning", "plugin_invalid_refresh_interval", COMPONENT,
                  plugin_id=plugin_id, value=plugin.get("refresh_interval"))
        return
    if interval <= 0:
        return
    try:
        t = threading.Timer(interval, _timer_tick, args=(plugin_id,))
        t.daemon = True
        with _timers_lock:
            old = _timers.pop(plugin_id, None)
            if old:
                old.cancel()
            _timers[plugin_id] = t
        t.start()
    except Exception as e:
        log_event("error", "plugin_schedule_timer_failed", COMPONENT,
                  plugin_id=plugin_id, error=str(e))


def start_plugin_timer(plugin):
    def _init():
        try:
            fetch_and_cache(plugin)
        except Exception as e:
            log_event("error", "plugin_init_fetch_error", COMPONENT,
                      plugin=plugin["name"], error=str(e))
        try:
            _schedule_timer(plugin)
        except Exception as e:
            log_event("error", "plugin_init_schedule_failed", COMPONENT,
                      plugin=plugin["name"], error=str(e))

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
    for idx, plugin in enumerate(plugins):
        if (plugin["enabled"]
                and plugin["source_type"] == "url"
                and plugin.get("source_url")):
            staggered_start(plugin, idx)
            log_event("info", "plugin_timer_started", COMPONENT,
                      plugin=plugin["name"])


def staggered_start(plugin, idx):
    """Start a plugin timer with a small jitter delay to avoid a
    thundering herd of simultaneous fetches during bootstrap."""
    delay = random.uniform(0, min(idx * 1.5, 30))

    def _delayed():
        if delay > 0:
            time.sleep(delay)
        start_plugin_timer(plugin)

    t = threading.Thread(target=_delayed,
                         name=f"plugin-bootstrap-{plugin['name']}", daemon=True)
    t.start()
