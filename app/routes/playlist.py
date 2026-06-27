import os
import threading
from urllib.parse import urlencode

from flask import Blueprint, Response, request

from app.utils import plugin_cache, plugin_refresh, plugin_store
from app.utils.logging_utils import log_event

playlist_bp = Blueprint('playlist', __name__)
COMPONENT = "playlist_proxy"

_fetching = {}
_fetching_lock = threading.Lock()


def _public_base_url():
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip('/')
    return request.host_url.rstrip('/')


def _m3u_safe(value):
    """Strip characters that could break M3U structure (newlines, quotes)."""
    if not value:
        return ""
    return str(value).replace("\r", "").replace("\n", " ").replace('"', "")


def _render_m3u(channels, base_url, fmt, token=None):
    lines = ['#EXTM3U', '']
    suffix = f"?{urlencode({'token': token})}" if token else ""
    for ch in channels:
        name = _m3u_safe(ch.get("name", "Unknown"))
        group = _m3u_safe(ch.get("group_title", ""))
        logo = _m3u_safe(ch.get("tvg_logo", ""))
        tvg_id = _m3u_safe(ch.get("tvg_id", ""))
        infohash = _m3u_safe(ch["infohash"])

        if fmt == 'mpegts':
            url = f"{base_url}/play/mpegts/{infohash}{suffix}"
        else:
            url = f"{base_url}/play/hls/{infohash}{suffix}"

        lines.append(f'#EXTINF:-1 group-title="{group}" tvg-name="{name}" tvg-id="{tvg_id}" tvg-logo="{logo}",{name}')
        lines.append(f'#EXTGRP:{group}')
        lines.append(url)
    return '\n'.join(lines) + '\n'


def _render(plugin_name, fmt):
    plugin = plugin_store.get_by_name(plugin_name)
    if plugin is None:
        return Response(f"Unknown plugin: {plugin_name}\n", status=404, mimetype='text/plain')

    channels = plugin_cache.get_channels(plugin["id"])
    if not channels and plugin.get("source_type") == "url" and plugin.get("source_url"):
        plugin_id = plugin["id"]
        with _fetching_lock:
            event = _fetching.get(plugin_id)
            if event is None:
                event = threading.Event()
                _fetching[plugin_id] = event
                is_fetcher = True
            else:
                is_fetcher = False
        if is_fetcher:
            try:
                log_event("info", "playlist_cache_miss_refresh", COMPONENT,
                          plugin=plugin_name, format=fmt)
                plugin_refresh.fetch_and_cache(plugin)
            finally:
                with _fetching_lock:
                    _fetching.pop(plugin_id, None)
                event.set()
        else:
            log_event("info", "playlist_cache_miss_wait", COMPONENT,
                      plugin=plugin_name, format=fmt)
            event.wait(timeout=60)
        channels = plugin_cache.get_channels(plugin["id"])
    if not channels:
        log_event("warning", "playlist_empty", COMPONENT, plugin=plugin_name, format=fmt)
        return Response("Playlist not ready, retry in a moment.\n", status=503, mimetype='text/plain')

    base_url = _public_base_url()
    token = request.args.get("token")
    body = _render_m3u(channels, base_url, fmt, token=token)
    log_event("info", "playlist_served", COMPONENT, plugin=plugin_name, format=fmt, channels=len(channels))
    return Response(body, content_type='audio/mpegurl; charset=utf-8')


@playlist_bp.route('/<plugin_name>/mpegts.m3u')
def playlist_mpegts(plugin_name):
    return _render(plugin_name, 'mpegts')


@playlist_bp.route('/<plugin_name>/hls.m3u')
def playlist_hls(plugin_name):
    return _render(plugin_name, 'hls')
