import os
import time

import requests

from app.utils import environment_store
from app.utils.logging_utils import log_event
from app.utils.upstream import session

COMPONENT = "acestream"

# stat.status values that mean the engine is downloading and the stream is servable
READY_STATUSES = {"dl", "dl_finished"}
# stat.status values that mean a fatal failure (stop waiting)
ERROR_STATUSES = {"err", "error"}
# everything else (prebuf, buf, check, idle, ...) is treated as "still warming up"

READY_TIMEOUT_S = 60
POLL_INTERVAL_S = 0.5
# When the engine reports a READY status we can back off to reduce engine load.
READY_POLL_INTERVAL_S = environment_store.get_float("OPENACE_STAT_POLL_READY_S")
REQUEST_TIMEOUT_S = 5
SESSION_RETRIES = 5
SESSION_BACKOFF_S = 0.5
_MAX_BACKOFF = 8.0

# Treat a stream as dead early when peers==0 for this many consecutive polls.
ZERO_PEER_DEAD_POLLS = environment_store.get_int("OPENACE_ZERO_PEER_DEAD_POLLS")

# Per-channel ceiling for the /check probe: how long we wait for the engine to
# resolve an infohash before declaring it a timeout.
CHECK_TIMEOUT_S = 10


def negotiate_stream(engine_url, content_id, *, ready_timeout=READY_TIMEOUT_S,
                     poll_interval=POLL_INTERVAL_S, request_timeout=REQUEST_TIMEOUT_S,
                     component=COMPONENT, log_context=None):
    """Open an AceStream session via the JSON API and wait until it is ready to serve.

    Uses ``/ace/getstream?id=...&format=json`` (returns immediately, no cold-start
    500) to obtain the playback/stat/command URLs, then polls ``stat_url`` until the
    engine reports a READY status. Returns a dict with ``playback_url``/``stat_url``/
    ``command_url``/``is_live`` once ready, or ``None`` on failure/timeout.
    """
    log_context = log_context or {}
    info = _open_session(engine_url, content_id, request_timeout, component, log_context)
    if info is None:
        return None
    playback_url, stat_url, command_url, is_live = info

    deadline = time.monotonic() + ready_timeout
    last_status = None
    zero_peer_polls = 0
    last_stat = None
    while True:
        if time.monotonic() >= deadline:
            log_event("warning", "acestream_ready_timeout", component,
                      last_status=last_status, **log_context)
            stop_stream(command_url, request_timeout=request_timeout,
                        component=component, log_context=log_context)
            return None
        last_stat = read_stat(stat_url, request_timeout=request_timeout,
                              component=component, log_context=log_context)
        status = last_stat.get("status") if last_stat else None
        peers = _as_int(last_stat.get("peers")) if last_stat else 0

        if status in READY_STATUSES:
            log_event("info", "acestream_ready", component, status=status,
                      peers=peers, is_live=is_live, **log_context)
            # M2: surface engine-reported hints so the FFmpeg manager can tune
            # rtbufsize / HLS segment size from the live bitrate and buffer time.
            stat_hints = {
                "player_buffer_time": last_stat.get("player_buffer_time") if last_stat else None,
                "live_buffer_time": last_stat.get("live_buffer_time") if last_stat else None,
                "current_bitrate": last_stat.get("current_bitrate") if last_stat else None,
            }
            return {
                "playback_url": playback_url,
                "stat_url": stat_url,
                "command_url": command_url,
                "is_live": is_live,
                "stat_hints": stat_hints,
            }
        if status in ERROR_STATUSES:
            log_event("warning", "acestream_stat_error", component, status=status, **log_context)
            stop_stream(command_url, request_timeout=request_timeout,
                        component=component, log_context=log_context)
            return None

        # Dead-peer early exit: if the engine never found any peers, the stream
        # is almost certainly dead — bail out before burning the full timeout.
        if peers == 0:
            zero_peer_polls += 1
            if zero_peer_polls >= ZERO_PEER_DEAD_POLLS:
                log_event("warning", "acestream_zero_peers_dead", component,
                          polls=zero_peer_polls, last_status=status, **log_context)
                stop_stream(command_url, request_timeout=request_timeout,
                            component=component, log_context=log_context)
                return None
        else:
            zero_peer_polls = 0

        if status is not None:
            last_status = status

        # Adaptive interval: poll aggressively while warming up, back off once
        # the stream transitions to ready (handled at loop top).
        time.sleep(poll_interval)


def stop_stream(command_url, *, request_timeout=REQUEST_TIMEOUT_S, component=COMPONENT, log_context=None):
    """Best-effort release of an AceStream session so the engine can free resources."""
    if not command_url:
        return
    log_context = log_context or {}
    try:
        session.get(f"{command_url}?method=stop", timeout=request_timeout).close()
        log_event("info", "acestream_stopped", component, **log_context)
    except requests.RequestException as e:
        log_event("warning", "acestream_stop_failed", component, error=str(e), **log_context)


def _open_session(engine_url, content_id, request_timeout, component, log_context):
    api_url = f"{engine_url}/ace/getstream?id={content_id}&format=json"
    last_error = None
    for attempt in range(1, SESSION_RETRIES + 1):
        try:
            resp = session.get(api_url, timeout=request_timeout)
            try:
                payload = resp.json()
            finally:
                resp.close()
        except (requests.RequestException, ValueError) as e:
            last_error = str(e)
            log_event("warning", "acestream_session_attempt_failed", component,
                      attempt=attempt, error=last_error, **log_context)
            if attempt < SESSION_RETRIES:
                time.sleep(min(SESSION_BACKOFF_S * (2 ** (attempt - 1)), _MAX_BACKOFF))
            continue

        if payload.get("error"):
            log_event("warning", "acestream_session_error", component,
                      error=payload["error"], **log_context)
            return None
        response = payload.get("response") or {}
        playback_url = response.get("playback_url")
        stat_url = response.get("stat_url")
        command_url = response.get("command_url")
        is_live = response.get("is_live")
        if not playback_url or not stat_url:
            log_event("error", "acestream_missing_urls", component,
                      response_keys=list(response.keys()), **log_context)
            if command_url:
                stop_stream(command_url, request_timeout=request_timeout,
                            component=component, log_context=log_context)
            return None
        log_event("info", "acestream_session_opened", component,
                  is_live=is_live, **log_context)
        return playback_url, stat_url, command_url, is_live

    log_event("error", "acestream_session_unavailable", component,
              attempts=SESSION_RETRIES, last_error=last_error, **log_context)
    return None


def read_stat(stat_url, *, request_timeout=REQUEST_TIMEOUT_S, component=COMPONENT, log_context=None):
    """Fetch a one-shot stat snapshot for an already-open session.

    Returns the engine's ``response`` object containing all available fields:
    ``status``, ``peers``, ``speed_down``, ``speed_up``, ``p2p_current_rate``,
    ``player_buffer_time``, ``live_buffer_time``, ``current_bitrate``,
    ``avg_bitrate``, etc.  Returns ``{"status": "err"}`` if the engine reports
    an error, or ``None`` on a transport failure.
    """
    log_context = log_context or {}
    try:
        resp = session.get(stat_url, timeout=request_timeout)
        try:
            data = resp.json()
        finally:
            resp.close()
    except (requests.RequestException, ValueError) as e:
        log_event("warning", "acestream_stat_failed", component, error=str(e), **log_context)
        return None
    if data.get("error"):
        return {"status": "err"}
    return data.get("response") or {}


def check_stream(engine_url, content_id, *, timeout=CHECK_TIMEOUT_S,
                 poll_interval=POLL_INTERVAL_S, request_timeout=REQUEST_TIMEOUT_S,
                 component=COMPONENT, log_context=None):
    """Probe a single infohash sequentially and classify the outcome.

    Opens one session, polls its stat until the engine reaches a READY status
    (``live``), reports an error status (``dead``), or the overall ``timeout``
    elapses (``timeout``). Transport failures or a rejected id map to ``error``/
    ``dead``. Always releases the session. Returns a dict::

        {"outcome": "live"|"dead"|"timeout"|"error",
         "peers": int, "speed": int, "response_ms": int}

    Unlike :func:`negotiate_stream`, the session is opened with a single request
    (no retry/backoff) so one bad channel can't blow past the per-channel budget.
    """
    log_context = log_context or {}
    start = time.monotonic()

    def _elapsed_ms():
        return int((time.monotonic() - start) * 1000)

    api_url = f"{engine_url}/ace/getstream?id={content_id}&format=json"
    try:
        resp = session.get(api_url, timeout=request_timeout)
        try:
            payload = resp.json()
        finally:
            resp.close()
    except (requests.RequestException, ValueError) as e:
        log_event("warning", "check_session_failed", component, error=str(e), **log_context)
        return {"outcome": "error", "peers": 0, "speed": 0, "response_ms": _elapsed_ms()}

    if payload.get("error"):
        log_event("info", "check_session_rejected", component, error=payload["error"], **log_context)
        return {"outcome": "dead", "peers": 0, "speed": 0, "response_ms": _elapsed_ms()}

    response = payload.get("response") or {}
    stat_url = response.get("stat_url")
    command_url = response.get("command_url")
    if not stat_url or not command_url:
        log_event("warning", "check_missing_urls", component,
                  response_keys=list(response.keys()), **log_context)
        return {"outcome": "error", "peers": 0, "speed": 0, "response_ms": _elapsed_ms()}

    peers = 0
    speed = 0
    deadline = start + timeout
    zero_peer_polls = 0
    try:
        while True:
            stat = read_stat(stat_url, request_timeout=request_timeout,
                             component=component, log_context=log_context)
            status = stat.get("status") if stat else None
            if stat:
                peers = _as_int(stat.get("peers"))
                speed = _as_int(stat.get("speed_down"))

            # Dead-peer early exit for the checker too.
            if peers == 0 and status not in READY_STATUSES:
                zero_peer_polls += 1
                if zero_peer_polls >= ZERO_PEER_DEAD_POLLS:
                    return {"outcome": "dead", "peers": peers, "speed": speed, "response_ms": _elapsed_ms()}
            else:
                zero_peer_polls = 0

            if status in READY_STATUSES:
                return {"outcome": "live", "peers": peers, "speed": speed, "response_ms": _elapsed_ms()}
            if status in ERROR_STATUSES:
                return {"outcome": "dead", "peers": peers, "speed": speed, "response_ms": _elapsed_ms()}
            if time.monotonic() >= deadline:
                return {"outcome": "timeout", "peers": peers, "speed": speed, "response_ms": _elapsed_ms()}
            time.sleep(poll_interval)
    finally:
        stop_stream(command_url, request_timeout=request_timeout,
                    component=component, log_context=log_context)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
