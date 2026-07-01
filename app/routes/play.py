import re

import requests
from flask import Blueprint, Response, current_app, request

from app.utils import environment_store
from app.utils.logging_utils import log_event
from app.utils.stream_registry import register, unregister
from app.utils.upstream import session as upstream_session

play_bp = Blueprint('play', __name__, url_prefix='/play')
COMPONENT = "play_proxy"
_VALID_ID = re.compile(r'^[0-9a-fA-F]{40}$')

_manager = None


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


def _engine_url():
    configured = current_app.config.get("ACESTREAM_ENGINE")
    if configured:
        return str(configured).rstrip("/")
    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = current_app.config.get("ACESTREAM_PORT", "6878")
    return f"http://{host}:{port}"


def _direct_mpegts(content_id, log_context):
    upstream_url = f"{_engine_url()}/ace/getstream?id={content_id}"
    upstream_resp = None
    try:
        upstream_resp = upstream_session.get(
            upstream_url, stream=True, timeout=(5, None)
        )
        if upstream_resp.status_code >= 400:
            log_event("warning", "direct_mpegts_upstream_error", COMPONENT,
                      status=upstream_resp.status_code, **log_context)
            upstream_resp.close()
            return Response("Stream not available", status=503)
    except requests.RequestException as exc:
        log_event("warning", "direct_mpegts_open_failed", COMPONENT,
                  error=str(exc), **log_context)
        if upstream_resp is not None:
            upstream_resp.close()
        return Response("Stream not available", status=503)

    client_ip = request.remote_addr
    log_event("info", "direct_mpegts_available", COMPONENT,
              client_ip=client_ip, **log_context)

    def generate():
        register(content_id, "mpegts", client_ip=client_ip)
        try:
            for chunk in upstream_resp.iter_content(
                chunk_size=max(environment_store.get_int("OPENACE_CHUNK_SIZE"), 1)
            ):
                if chunk:
                    yield chunk
        except requests.RequestException as exc:
            log_event("warning", "direct_mpegts_stream_failed", COMPONENT,
                      error=str(exc), client_ip=client_ip, **log_context)
        finally:
            upstream_resp.close()
            unregister(content_id, "mpegts", client_ip=client_ip)

    return _apply_streaming_headers(Response(generate(), content_type='video/MP2T'))


@play_bp.route('/mpegts/<content_id>', methods=['GET', 'HEAD'])
def play(content_id):
    if not _VALID_ID.match(content_id):
        return Response("Invalid content id", status=400)

    log_context = {"content_id": content_id}
    log_event("info", "stream_request_started", COMPONENT, **log_context)

    # Live MPEG-TS is not byte-seekable; honour HEAD metadata requests without
    # subscribing to the stream, and ignore Range headers (some players emit
    # "Range: bytes=0-" on live sources).
    if request.method == 'HEAD':
        return _apply_streaming_headers(Response(content_type='video/MP2T'))

    if request.headers.get('Range'):
        log_event("info", "play_range_ignored", COMPONENT, **log_context)

    if not _ffmpeg_enabled():
        return _direct_mpegts(content_id, log_context)

    if _manager is None:
        return Response("Stream manager not available", status=503)

    stream, q = _manager.subscribe_mpegts(content_id)
    if stream is None:
        return Response("Stream not available", status=503)

    client_ip = request.remote_addr
    log_event("info", "stream_available", COMPONENT, client_ip=client_ip, **log_context)

    def generate():
        register(content_id, "mpegts", client_ip=client_ip)
        client_context = {**log_context, "client_ip": client_ip}
        try:
            yield from _manager.iterate_mpegts(q, component=COMPONENT, log_context=client_context)
        finally:
            try:
                _manager.unsubscribe_mpegts(stream, q)
            finally:
                unregister(content_id, "mpegts", client_ip=client_ip)

    return _apply_streaming_headers(Response(generate(), content_type='video/MP2T'))
