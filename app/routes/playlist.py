from flask import Blueprint, Response, request

from app.utils import plugin_cache, plugin_store
from app.utils.logging_utils import log_event

playlist_bp = Blueprint('playlist', __name__)
COMPONENT = "playlist_proxy"


def _render_m3u(channels, base_url, fmt):
    lines = ['#EXTM3U', '']
    for ch in sorted(channels, key=lambda c: c.get("name", "").lower()):
        name = ch.get("name", "Unknown")
        group = ch.get("group_title", "")
        logo = ch.get("tvg_logo", "")
        tvg_id = ch.get("tvg_id", "")
        infohash = ch["infohash"]

        if fmt == 'mpegts':
            url = f"{base_url}/play/mpegts/{infohash}"
        else:
            url = f"{base_url}/play/hls/{infohash}"

        lines.append(f'#EXTINF:-1 group-title="{group}" tvg-name="{name}" tvg-id="{tvg_id}" tvg-logo="{logo}",{name}')
        lines.append(f'#EXTGRP:{group}')
        lines.append(url)
    return '\n'.join(lines) + '\n'


def _render(plugin_name, fmt):
    plugin = plugin_store.get_by_name(plugin_name)
    if plugin is None:
        return Response(f"Unknown plugin: {plugin_name}\n", status=404, mimetype='text/plain')

    channels = plugin_cache.get_channels(plugin["id"])
    if not channels:
        log_event("warning", "playlist_empty", COMPONENT, plugin=plugin_name, format=fmt)
        return Response("Playlist not ready, retry in a moment.\n", status=503, mimetype='text/plain')

    base_url = request.host_url.rstrip('/')
    body = _render_m3u(channels, base_url, fmt)
    log_event("info", "playlist_served", COMPONENT, plugin=plugin_name, format=fmt, channels=len(channels))
    return Response(body, content_type='audio/mpegurl; charset=utf-8')


@playlist_bp.route('/<plugin_name>/mpegts.m3u')
def playlist_mpegts(plugin_name):
    return _render(plugin_name, 'mpegts')


@playlist_bp.route('/<plugin_name>/hls.m3u')
def playlist_hls(plugin_name):
    return _render(plugin_name, 'hls')
