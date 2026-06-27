import re

from flask import Blueprint, Response, request

from app.utils.logging_utils import log_event
from app.utils.stream_registry import register, unregister

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


@play_bp.route('/mpegts/<content_id>')
def play(content_id):
    if not _VALID_ID.match(content_id):
        return Response("Invalid content id", status=400)
    if _manager is None:
        return Response("Stream manager not available", status=503)

    log_context = {"content_id": content_id}
    log_event("info", "stream_request_started", COMPONENT, **log_context)

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

    return _apply_streaming_headers(Response(generate(), content_type='video/mp2t'))
