import re

from flask import Blueprint, Response, current_app, request

from app.utils import shared_stream
from app.utils.logging_utils import log_event
from app.utils.stream_registry import register, unregister

play_bp = Blueprint('play', __name__, url_prefix='/play')
COMPONENT = "play_proxy"
_VALID_ID = re.compile(r'^[0-9a-fA-F]{40}$')


@play_bp.route('/mpegts/<content_id>')
def play(content_id):
    if not _VALID_ID.match(content_id):
        return Response("Invalid content id", status=400)
    log_context = {"content_id": content_id}
    log_event("info", "stream_request_started", COMPONENT, **log_context)

    stream, q = shared_stream.get_or_create(
        current_app.config['ACESTREAM_ENGINE'],
        content_id,
        component=COMPONENT,
        log_context=log_context,
    )
    if stream is None:
        return Response("Stream not available", status=503)

    client_ip = request.remote_addr
    log_event("info", "stream_available", COMPONENT, client_ip=client_ip, **log_context)
    register(content_id, "mpegts", client_ip=client_ip)

    def generate():
        try:
            yield from shared_stream.iterate(q)
        finally:
            stream.unsubscribe(q)
            unregister(content_id, "mpegts", client_ip=client_ip)

    return Response(generate(), content_type='video/mp2t')
