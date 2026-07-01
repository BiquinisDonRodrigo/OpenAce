import hashlib
import os
import re
import threading
import time
import uuid
from urllib.parse import urlencode

from flask import Blueprint, Response, abort, redirect, request, send_from_directory

from app.utils import environment_store, stream_registry
from app.utils.logging_utils import log_event

hls_bp = Blueprint('hls', __name__, url_prefix='/play/hls')
COMPONENT = "hls_ffmpeg"
MANIFEST_POLL_TIMEOUT_S = 30
MANIFEST_POLL_INTERVAL_S = 0.5
MANIFEST_FILENAME = "playlist.m3u8"
HLS_CLIENT_PARAM = "hls_client"
STALE_SEGMENT_MAX_AGE_S = environment_store.get_int("OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S")
STALE_LOG_INTERVAL_S = 30
_STALE_CACHE_TTL_S = 1.0
HLS_LAZY = environment_store.get_bool("OPENACE_HLS_LAZY")
SEGMENT_RE = re.compile(r'^[A-Za-z0-9_\-]+\.(?:ts|m3u8)$')
_VALID_ID = re.compile(r'^[0-9a-fA-F]{40}$')
_HLS_CLIENT_RE = re.compile(r'^[0-9a-f]{32}$')

_manager = None
_stale_log_last = {}
_stale_cache = {}
_stale_log_lock = threading.Lock()


def _apply_streaming_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Accel-Buffering"] = "no"
    return response


def set_manager(manager):
    global _manager
    _manager = manager


def _ffmpeg_enabled():
    return environment_store.get_bool("OPENACE_FFMPEG_ENABLED")


def clear_stale_log(content_id):
    """Called by FFmpegManager when a stream is dropped/killed so the
    stale-log throttle dict does not retain entries for dead streams."""
    with _stale_log_lock:
        _stale_log_last.pop(content_id, None)
        _stale_cache.pop(content_id, None)


@hls_bp.route('/<content_id>')
def hls_manifest(content_id):
    if not _VALID_ID.match(content_id):
        abort(400)
    if not _ffmpeg_enabled():
        return Response("FFmpeg disabled", status=503)
    if _manager is None:
        return Response("Stream manager not available", status=503)

    client_id = request.args.get(HLS_CLIENT_PARAM, "")
    if not _is_valid_hls_client(client_id):
        return redirect(_url_with_hls_client(uuid.uuid4().hex), code=302)

    out_dir = _manager.ensure_stream(content_id)
    if out_dir is None:
        return Response("FFmpeg failed to start", status=503)

    stream_registry.touch_hls_client(
        content_id,
        client_id,
        client_ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )

    manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
    if not os.path.exists(manifest_path):
        if not _manager.is_alive(content_id):
            log_event("warning", "hls_ffmpeg_exited", COMPONENT, content_id=content_id)
            _manager.drop(content_id)
            return Response("Upstream not ready", status=503)
        # M4: in lazy mode the session was spawned MPEG-TS-only; the first HLS
        # request asks the manager to restart it with the HLS output enabled.
        if HLS_LAZY and _manager.request_hls(content_id):
            log_event("info", "hls_lazy_pending", COMPONENT, content_id=content_id)
        log_event("info", "hls_manifest_not_ready", COMPONENT, content_id=content_id)
        return Response("Stream buffering, retry", status=503, headers={"Retry-After": "1"})

    stale, newest_segment, age = _segments_stale(out_dir, content_id)
    if stale:
        _log_stale_segments(content_id, newest_segment, age)
        _manager.drop(content_id)
        return Response("Stream stale, retry", status=503)

    try:
        with open(manifest_path, "r", errors="replace") as fh:
            raw = fh.read()
    except FileNotFoundError:
        log_event("warning", "hls_manifest_race_deleted", COMPONENT, content_id=content_id)
        return Response("Stream not ready, retry", status=503)

    with _stale_log_lock:
        _stale_log_last.pop(content_id, None)
    rewritten = _rewrite_manifest(raw, content_id, client_id)
    log_event("info", "hls_manifest_served", COMPONENT, content_id=content_id)
    return _apply_streaming_headers(Response(rewritten, content_type="application/vnd.apple.mpegurl"))


@hls_bp.route('/<content_id>/<filename>')
def hls_segment(content_id, filename):
    if not _VALID_ID.match(content_id):
        abort(400)
    if not _ffmpeg_enabled():
        return Response("FFmpeg disabled", status=503)
    if _manager is None:
        return Response("Stream manager not available", status=503)
    if not SEGMENT_RE.match(filename):
        abort(400)
    client_id = request.args.get(HLS_CLIENT_PARAM, "")
    if not _is_valid_hls_client(client_id):
        client_id = _legacy_hls_client_id()
    stream_registry.touch_hls_client(
        content_id,
        client_id,
        client_ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )
    out_dir = _manager.output_dir(content_id)
    path = os.path.join(out_dir, filename)
    if not os.path.exists(path):
        if _manager.is_alive(content_id):
            log_event("info", "hls_segment_not_ready", COMPONENT,
                      content_id=content_id, filename=filename)
            return Response("Segment not ready, retry", status=503,
                            headers={"Retry-After": "1"})
        _log_missing_segment(out_dir, content_id, filename)
        abort(404)
    if not _manager.is_alive(content_id):
        log_event("warning", "hls_segment_ffmpeg_dead", COMPONENT,
                  content_id=content_id, filename=filename)
        _manager.drop(content_id)
        return Response("Stream stale, retry", status=503)
    stale, newest_segment, age = _segments_stale(out_dir, content_id)
    if stale:
        _log_stale_segments(content_id, newest_segment, age)
        _manager.drop(content_id)
        return Response("Stream stale, retry", status=503)
    _manager.touch(content_id)
    response = send_from_directory(out_dir, filename, mimetype="video/MP2T" if filename.endswith(".ts") else None)
    return _apply_streaming_headers(response)


def _is_valid_hls_client(value: str) -> bool:
    return bool(value and _HLS_CLIENT_RE.match(value))


def _url_with_hls_client(client_id: str) -> str:
    args = request.args.to_dict(flat=True)
    args[HLS_CLIENT_PARAM] = client_id
    query = urlencode(args)
    return f"{request.path}?{query}" if query else request.path


def _legacy_hls_client_id() -> str:
    client_ip = request.remote_addr or ""
    user_agent = request.headers.get("User-Agent", "")
    raw = f"legacy:{client_ip}:{user_agent}".encode("utf-8", errors="ignore")
    return hashlib.md5(raw).hexdigest()


def _log_missing_segment(out_dir: str, content_id: str, filename: str):
    current_segments = []
    out_dir_exists = os.path.isdir(out_dir)
    if out_dir_exists:
        try:
            current_segments = sorted(
                name for name in os.listdir(out_dir)
                if name.endswith(".ts")
            )[-5:]
        except OSError:
            current_segments = []
    log_event(
        "warning", "hls_segment_missing", COMPONENT,
        content_id=content_id,
        filename=filename,
        out_dir_exists=out_dir_exists,
        current_segments=current_segments,
        client_ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
    )


def _segments_stale(out_dir: str, content_id: str | None = None):
    now = time.monotonic()
    if content_id is not None:
        with _stale_log_lock:
            cached = _stale_cache.get(content_id)
            if cached and cached[0] == out_dir and now - cached[4] < _STALE_CACHE_TTL_S:
                return cached[1], cached[2], cached[3]
    manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
    newest_name = None
    newest_mtime = None
    try:
        for name in os.listdir(out_dir):
            if not name.endswith(".ts"):
                continue
            path = os.path.join(out_dir, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if newest_mtime is None or mtime > newest_mtime:
                newest_name = name
                newest_mtime = mtime
    except OSError:
        pass
    if newest_mtime is not None:
        age = time.time() - newest_mtime
        result = (age > STALE_SEGMENT_MAX_AGE_S, newest_name, age)
        if content_id is not None:
            with _stale_log_lock:
                _stale_cache[content_id] = (out_dir, result[0], result[1], result[2], now)
        return result
    try:
        mtime = os.path.getmtime(manifest_path)
    except OSError:
        result = (False, None, None)
        if content_id is not None:
            with _stale_log_lock:
                _stale_cache[content_id] = (out_dir, result[0], result[1], result[2], now)
        return result
    age = time.time() - mtime
    result = (age > STALE_SEGMENT_MAX_AGE_S, MANIFEST_FILENAME, age)
    if content_id is not None:
        with _stale_log_lock:
            _stale_cache[content_id] = (out_dir, result[0], result[1], result[2], now)
    return result


def _log_stale_segments(content_id: str, newest_segment: str | None, age: float | None):
    now = time.monotonic()
    with _stale_log_lock:
        last = _stale_log_last.get(content_id, 0)
        if now - last < STALE_LOG_INTERVAL_S:
            return
        _stale_log_last[content_id] = now
    log_event(
        "warning", "hls_segments_stale", COMPONENT,
        content_id=content_id,
        newest_segment=newest_segment,
        age_seconds=round(age or 0, 2),
    )


def _rewrite_manifest(body: str, content_id: str, client_id: str) -> str:
    out = []
    params = {}
    token = request.args.get("token")
    if token:
        params["token"] = token
    params[HLS_CLIENT_PARAM] = client_id
    suffix = f"?{urlencode(params)}"
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if stripped.startswith("http"):
                from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode as _ue
                parts = urlsplit(stripped)
                basename = os.path.basename(parts.path)
                new_path = f"/play/hls/{content_id}/{basename}"
                existing = dict(parse_qsl(parts.query, keep_blank_values=True))
                existing.update(params)
                line = urlunsplit(("", "", new_path, _ue(existing), "")) + "\n"
            else:
                basename = os.path.basename(stripped)
                line = f"/play/hls/{content_id}/{basename}{suffix}\n"
        out.append(line)
    return "".join(out)
