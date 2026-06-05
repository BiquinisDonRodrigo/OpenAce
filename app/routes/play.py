import re
import time

from flask import Blueprint, Response, current_app

from app.utils.acestream import negotiate_stream, stop_stream
from app.utils.logging_utils import log_event
from app.utils.stream_registry import register, unregister
from app.utils.upstream import open_upstream, stream_to_client

play_bp = Blueprint('play', __name__, url_prefix='/play')
COMPONENT = "play_proxy"
_VALID_ID = re.compile(r'^[0-9a-fA-F]{40}$')


@play_bp.route('/mpegts/<content_id>')
def play(content_id):
    if not _VALID_ID.match(content_id):
        return Response("Invalid content id", status=400)
    log_context = {"content_id": content_id}
    log_event("info", "stream_request_started", COMPONENT, **log_context)

    session_info = negotiate_stream(
        current_app.config['ACESTREAM_ENGINE'],
        content_id,
        component=COMPONENT,
        log_context=log_context,
    )
    if session_info is None:
        return Response("Stream not available", status=503)

    command_url = session_info["command_url"]
    r = open_upstream(
        session_info["playback_url"],
        retries=10,
        backoff=0.5,
        connect_timeout=5,
        read_timeout=60,
        retriable_statuses=(502, 503, 504),
        component=COMPONENT,
        log_context=log_context,
    )
    if r is None:
        stop_stream(command_url, component=COMPONENT, log_context=log_context)
        return Response("Stream not available", status=503)
    if r.status_code != 200:
        log_event("warning", "stream_unexpected_status", COMPONENT,
                  status_code=r.status_code, **log_context)
        status = r.status_code
        r.close()
        stop_stream(command_url, component=COMPONENT, log_context=log_context)
        return Response("Upstream error", status=status)

    log_event("info", "stream_available", COMPONENT, status_code=r.status_code, **log_context)
    register(content_id, "mpegts")

    state = {"total": 0, "last_log": time.time()}

    def on_chunk(n):
        state["total"] += n
        now = time.time()
        if state["total"] >= 5 * 1024 * 1024 or (now - state["last_log"]) > 60:
            mb = round(state["total"] / (1024 * 1024), 2)
            log_event("info", "stream_progress", COMPONENT,
                      bytes_transmitted_mb=mb, **log_context)
            state["total"] = 0
            state["last_log"] = now

    def on_close():
        unregister(content_id, "mpegts")
        stop_stream(command_url, component=COMPONENT, log_context=log_context)

    return Response(
        stream_to_client(
            r,
            chunk_size=8192,
            on_chunk=on_chunk,
            on_close=on_close,
            component=COMPONENT,
            log_context=log_context,
        ),
        content_type='video/mp2t',
    )
