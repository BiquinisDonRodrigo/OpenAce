import os
import re
import time

from flask import Blueprint, Response, abort, send_from_directory

from app.utils.logging_utils import log_event

hls_bp = Blueprint('hls', __name__, url_prefix='/play/hls')
COMPONENT = "hls_ffmpeg"
MANIFEST_POLL_TIMEOUT_S = 30
MANIFEST_POLL_INTERVAL_S = 0.5
MANIFEST_FILENAME = "playlist.m3u8"
SEGMENT_RE = re.compile(r'^[A-Za-z0-9_\-]+\.(?:ts|m3u8)$')
_VALID_ID = re.compile(r'^[0-9a-fA-F]{40}$')

_manager = None


def set_manager(manager):
    global _manager
    _manager = manager


@hls_bp.route('/<content_id>')
def hls_manifest(content_id):
    if not _VALID_ID.match(content_id):
        abort(400)
    out_dir = _manager.ensure_stream(content_id)
    if out_dir is None:
        return Response("FFmpeg failed to start", status=503)

    manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
    deadline = time.monotonic() + MANIFEST_POLL_TIMEOUT_S
    while not os.path.exists(manifest_path):
        if not _manager.is_alive(content_id):
            log_event("warning", "hls_ffmpeg_exited", COMPONENT, content_id=content_id)
            _manager.drop(content_id)
            return Response("Upstream not ready", status=503)
        if time.monotonic() >= deadline:
            log_event("warning", "hls_manifest_timeout", COMPONENT, content_id=content_id)
            return Response("Stream buffering, retry", status=503)
        time.sleep(MANIFEST_POLL_INTERVAL_S)

    with open(manifest_path, "r", errors="replace") as fh:
        raw = fh.read()

    rewritten = _rewrite_manifest(raw, content_id)
    log_event("info", "hls_manifest_served", COMPONENT, content_id=content_id)
    return Response(rewritten, content_type="application/vnd.apple.mpegurl")


@hls_bp.route('/<content_id>/<filename>')
def hls_segment(content_id, filename):
    if not _VALID_ID.match(content_id):
        abort(400)
    if not SEGMENT_RE.match(filename):
        abort(400)
    out_dir = _manager.output_dir(content_id)
    if not os.path.exists(os.path.join(out_dir, filename)):
        abort(404)
    _manager.touch(content_id)
    return send_from_directory(out_dir, filename)


def _rewrite_manifest(body: str, content_id: str) -> str:
    out = []
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("http"):
            basename = os.path.basename(stripped)
            line = f"/play/hls/{content_id}/{basename}\n"
        out.append(line)
    return "".join(out)
