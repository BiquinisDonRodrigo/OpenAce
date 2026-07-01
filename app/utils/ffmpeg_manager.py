import collections
import fcntl
import os
from queue import Empty, Full, Queue
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid

from app.utils.acestream import (
    negotiate_stream, read_stat, stop_stream, READY_TIMEOUT_S,
    REQUEST_TIMEOUT_S, SESSION_RETRIES, SESSION_BACKOFF_S,
)
from app.utils import environment_store, stream_registry
from app.utils.logging_utils import log_event

COMPONENT = "ffmpeg_manager"
HLS_OUTPUT_BASE = "/tmp/openace"
IDLE_TIMEOUT_S = environment_store.get_int("OPENACE_IDLE_TIMEOUT_S")
REAPER_INTERVAL_S = 10
HLS_TIME = environment_store.get_int("OPENACE_HLS_TIME")
HLS_LIST_SIZE = environment_store.get_int("OPENACE_HLS_LIST_SIZE")
# Tuned values for live streams: shorter segments + smaller list = lower latency.
HLS_TIME_LIVE = environment_store.get_int("OPENACE_HLS_TIME_LIVE")
HLS_LIST_SIZE_LIVE = environment_store.get_int("OPENACE_HLS_LIST_SIZE_LIVE")
HLS_DELETE_THRESHOLD = 10
# Real-time buffer limit for FFmpeg (reduces latency on live content).
FFMPEG_RTBUFSIZE = environment_store.get_str("OPENACE_FFMPEG_RTBUFSIZE")
HLS_FLAGS = "delete_segments+append_list+omit_endlist+program_date_time+temp_file"
MANIFEST_FILENAME = "playlist.m3u8"
SEGMENT_PREFIX = "seg"
CHUNK_SIZE = environment_store.get_int("OPENACE_CHUNK_SIZE")
QUEUE_MAX = environment_store.get_int("OPENACE_QUEUE_MAX")
PIPE_BUFFER_SIZE = environment_store.get_int("OPENACE_PIPE_BUFFER_SIZE")
ITERATE_TIMEOUT_S = environment_store.get_int("OPENACE_ITERATE_TIMEOUT_S")


def _session_open_worst_case_s():
    backoff = sum(
        min(SESSION_BACKOFF_S * (2 ** (attempt - 1)), 8.0)
        for attempt in range(1, SESSION_RETRIES)
    )
    return SESSION_RETRIES * REQUEST_TIMEOUT_S + backoff


START_WAIT_TIMEOUT_S = int(READY_TIMEOUT_S + _session_open_worst_case_s() + 5)
FFMPEG_START_PROBE_S = 3.0
RESTART_RESET_MIN_UPTIME_S = 30.0
FFMPEG_RW_TIMEOUT_US = environment_store.get_str("OPENACE_FFMPEG_RW_TIMEOUT_US")
MAX_RESTART_ATTEMPTS = environment_store.get_int("OPENACE_FFMPEG_RESTARTS")
RESTART_BACKOFF_S = environment_store.get_float("OPENACE_FFMPEG_RESTART_BACKOFF_S")
MAX_CONCURRENT_STREAMS = environment_store.get_int("OPENACE_MAX_STREAMS")
# Adaptive client buffer: live streams tolerate a smaller queue (lower latency)
# while VOD benefits from a larger one (smoother seek/prebuffer).
QUEUE_MAX_LIVE = environment_store.get_int("OPENACE_QUEUE_MAX_LIVE")
QUEUE_MAX_VOD = environment_store.get_int("OPENACE_QUEUE_MAX_VOD")
# Align MPEG-TS fan-out to 188-byte packet boundaries so every subscriber
# receives a clean, decodable stream even if FFmpeg ever emits a misaligned
# prefix (defensive; FFmpeg's -f mpegts output is already aligned).
TS_ALIGN = environment_store.get_bool("OPENACE_TS_ALIGN")
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
# Dynamically tune FFmpeg's realtime buffer / HLS segment size from the
# engine-reported bitrate and player_buffer_time (disabled by default).
FFMPEG_STAT_TUNING = environment_store.get_bool("OPENACE_FFMPEG_STAT_TUNING")
# Only produce the HLS output when an HLS client actually connects.
HLS_LAZY = environment_store.get_bool("OPENACE_HLS_LAZY")
_SENTINEL = object()
# Signalled when the source dies and cannot be restarted, so subscribers can
# distinguish a hard error from a clean close.
_ERROR_SENTINEL = object()


def _humanize_bytes(num):
    """Format a byte count as an FFmpeg size string (e.g. 8388608 -> '8M').

    Uses ceiling division so the humanized value never undershoots the original
    byte count (important for rtbufsize, which must be at least as large as the
    computed buffer, never smaller).
    """
    num = int(num)
    for unit in ("", "K", "M", "G"):
        if abs(num) < 1024 or unit == "G":
            return f"{num}{unit}"
        num = (num + 1023) // 1024
    return f"{num}G"


def _compute_rtbufsize(bitrate_bps, base):
    """Pick an FFmpeg realtime buffer that holds ~5s of stream, never below base.

    ``base`` is the existing OPENACE_FFMPEG_RTBUFSIZE value (e.g. '5M').
    Returns an FFmpeg size string. Falls back to ``base`` on any error.
    """
    try:
        base_bytes = _parse_ffmpeg_size(base)
    except (TypeError, ValueError):
        base_bytes = 5 * 1024 * 1024
    try:
        bitrate_bps = int(bitrate_bps)
    except (TypeError, ValueError):
        return base
    if bitrate_bps <= 0:
        return base
    wanted = bitrate_bps * 5 // 8
    return _humanize_bytes(max(base_bytes, wanted))


def _compute_hls_time(buffer_time_s, base):
    """Cap the live HLS segment duration to ~half the player buffer, never above base.

    Returns an int >= 1. ``base`` is the configured HLS_TIME_LIVE seconds.
    """
    try:
        base = int(base)
    except (TypeError, ValueError):
        base = 2
    if base < 1:
        base = 1
    try:
        buffer_time_s = float(buffer_time_s)
    except (TypeError, ValueError):
        return base
    if buffer_time_s <= 0:
        return base
    return max(1, min(base, int(buffer_time_s // 2)))


_FFMPEG_SIZE_UNITS = {"": 1, "k": 1024, "K": 1024, "m": 1024 ** 2, "M": 1024 ** 2,
                      "g": 1024 ** 3, "G": 1024 ** 3}


def _parse_ffmpeg_size(value):
    """Parse an FFmpeg size string ('5M', '8388608', '2G') into bytes."""
    if value is None:
        raise ValueError("none")
    text = str(value).strip()
    if not text:
        raise ValueError("empty")
    num_part = text
    while num_part and not (num_part[-1].isdigit() or num_part[-1] in ".-"):
        unit = num_part[-1]
        num_part = num_part[:-1]
        if not num_part:
            break
    if not num_part:
        raise ValueError(f"unparseable: {value}")
    number = float(num_part)
    suffix = text[len(num_part):]
    mult = _FFMPEG_SIZE_UNITS.get(suffix, 1)
    return int(number * mult)


def _align_ts_packets(data, residue=b"", synced=False):
    """Align a chunk of MPEG-TS bytes to 188-byte packet boundaries.

    Returns ``(body, new_residue, synced)``. ``body`` is what should be
    forwarded to subscribers (always a whole multiple of TS_PACKET_SIZE once
    synced), ``new_residue`` is the trailing bytes (< one packet) to keep for
    the next chunk, and ``synced`` reports whether the sync byte (0x47) was
    located. Any leading bytes before sync are dropped (logged by the caller).
    """
    if not TS_ALIGN:
        # Alignment disabled: forward everything, no residue management.
        return (residue + data), b"", True
    buf = residue + data
    if not buf:
        return b"", b"", synced
    if not synced:
        # Look for a 0x47 sync byte repeated one packet later to avoid locking
        # onto a 0x47 that happens to appear inside payload data.
        limit = max(0, len(buf) - 2 * TS_PACKET_SIZE)
        i = 0
        found = -1
        while i <= limit:
            i = buf.find(TS_SYNC_BYTE, i)
            if i < 0 or i > limit:
                break
            if buf[i + TS_PACKET_SIZE] == TS_SYNC_BYTE:
                found = i
                break
            i += 1
        if found < 0:
            # Keep the tail that could still contain a sync point.
            keep = min(len(buf), TS_PACKET_SIZE)
            return b"", buf[-keep:], False
        if found > 0:
            # Leading garbage dropped; caller logs how many bytes.
            buf = buf[found:]
        synced = True
    whole = (len(buf) // TS_PACKET_SIZE) * TS_PACKET_SIZE
    return buf[:whole], buf[whole:], synced


class _StreamSession:
    __slots__ = (
        "process", "output_dir", "command_url", "playback_url", "stat_url", "last_request", "lock",
        "stderr_tail", "stderr_thread", "stdout_thread", "subscribers",
        "sub_lock", "closed", "restarting", "bytes_total", "bytes_window", "last_progress",
        "dropped_clients", "restart_attempts", "_subscriber_count", "is_live",
        "queue_max", "ts_residue", "ts_synced", "dropped_lead_bytes", "hls_requested",
        "process_started_at", "process_start_bytes_total",
    )

    def __init__(self, process, output_dir, command_url=None, playback_url=None, is_live=None, stat_url=None):
        self.process = process
        self.output_dir = output_dir
        self.command_url = command_url
        self.playback_url = playback_url
        self.stat_url = stat_url
        self.is_live = is_live
        self.process_started_at = time.monotonic()
        self.last_request = time.monotonic()
        self.lock = threading.Lock()
        # Bounded ring buffer drained continuously by a daemon thread; keeps the
        # tail of FFmpeg's stderr without ever letting the OS pipe fill up.
        self.stderr_tail = collections.deque(maxlen=200)
        self.stderr_thread = None
        self.stdout_thread = None
        self.subscribers = set()
        self.sub_lock = threading.Lock()
        self.closed = False
        self.restarting = False
        self.bytes_total = 0
        self.process_start_bytes_total = 0
        self.bytes_window = 0
        self.last_progress = time.monotonic()
        self.dropped_clients = 0
        self.restart_attempts = 0
        self._subscriber_count = 0
        # Adaptive client queue: live -> smaller (latency), vod -> larger (smooth).
        self.queue_max = QUEUE_MAX_LIVE if is_live else QUEUE_MAX_VOD
        # MPEG-TS packet-alignment state (defensive; see _align_ts_packets).
        self.ts_residue = b""
        self.ts_synced = False
        self.dropped_lead_bytes = 0
        # Set when an HLS client actually requests the manifest so a lazy spawn
        # can be restarted with the HLS output enabled.
        self.hls_requested = False

    def touch(self):
        with self.lock:
            self.last_request = time.monotonic()

    def idle_seconds(self):
        with self.lock:
            return time.monotonic() - self.last_request

    @property
    def client_count(self):
        with self.sub_lock:
            return len(self.subscribers)

    def subscribe(self):
        q = Queue(maxsize=self.queue_max)
        with self.sub_lock:
            if self.closed:
                return None
            self.subscribers.add(q)
            self._subscriber_count = len(self.subscribers)
        self.touch()
        return q

    def unsubscribe(self, q):
        with self.sub_lock:
            self.subscribers.discard(q)
            self._subscriber_count = len(self.subscribers)
        self.touch()

    @staticmethod
    def _signal_queue(q, sentinel=_SENTINEL):
        try:
            q.put_nowait(sentinel)
            return True
        except Full:
            while True:
                try:
                    q.get_nowait()
                except Empty:
                    break
            try:
                q.put_nowait(sentinel)
                return True
            except Full:
                return False

    def broadcast_error(self, content_id):
        """Tell every subscriber the source died and cannot be restarted."""
        with self.sub_lock:
            subscribers = list(self.subscribers)
        for q in subscribers:
            self._signal_queue(q, _ERROR_SENTINEL)

    def broadcast(self, chunk, content_id):
        log_payload = None
        with self.lock:
            self.bytes_total += len(chunk)
            self.bytes_window += len(chunk)
            now = time.monotonic()
            elapsed = now - self.last_progress
            if elapsed >= 30:
                log_payload = {
                    "content_id": content_id,
                    "bytes_total": self.bytes_total,
                    "mbps": round((self.bytes_window * 8) / elapsed / 1_000_000, 3),
                }
                self.bytes_window = 0
                self.last_progress = now

        with self.sub_lock:
            subscribers = list(self.subscribers)
            dropped = self.dropped_clients

        if log_payload is not None:
            log_payload["clients"] = len(subscribers)
            log_payload["dropped_clients"] = dropped
            log_event("info", "ffmpeg_mpegts_progress", COMPONENT, **log_payload)

        stale = []
        trimmed = 0
        for q in subscribers:
            try:
                q.put_nowait(chunk)
            except Full:
                try:
                    q.get_nowait()
                    trimmed += 1
                    q.put_nowait(chunk)
                except (Empty, Full):
                    stale.append(q)

        if trimmed:
            log_event(
                "warning", "ffmpeg_mpegts_queue_trimmed", COMPONENT,
                content_id=content_id,
                trimmed=trimmed,
                clients=len(subscribers),
                queue_max=self.queue_max,
                chunk_size=CHUNK_SIZE,
            )

        if stale:
            with self.sub_lock:
                for q in stale:
                    if q in self.subscribers:
                        self.subscribers.discard(q)
                        self.dropped_clients += 1
                        if not self._signal_queue(q):
                            log_event(
                                "warning", "ffmpeg_mpegts_sentinel_failed", COMPONENT,
                                content_id=content_id,
                                queue_max=self.queue_max,
                            )
                self._subscriber_count = len(self.subscribers)
            log_event(
                "warning", "ffmpeg_mpegts_queue_full", COMPONENT,
                content_id=content_id,
                dropped=len(stale),
                clients=len(subscribers) - len(stale),
                queue_max=self.queue_max,
                chunk_size=CHUNK_SIZE,
            )

    def close_subscribers(self):
        with self.sub_lock:
            if self.closed:
                return
            self.closed = True
            subscribers = list(self.subscribers)
            self.subscribers.clear()
            self._subscriber_count = 0
        for q in subscribers:
            if not self._signal_queue(q):
                log_event(
                    "warning", "ffmpeg_mpegts_sentinel_failed", COMPONENT,
                    queue_max=QUEUE_MAX,
                )


class FFmpegManager:
    def __init__(self, acestream_host="127.0.0.1", acestream_port="6878"):
        self._streams = {}
        self._starting = {}
        self._spawn_reservations = 0
        self._lock = threading.Lock()
        self._closed = False
        self._stop_event = threading.Event()
        self._engine_url = f"http://{acestream_host}:{acestream_port}"
        # Per-stream stat cache: content_id -> {"data": {...}, "ts": float}
        self._stat_cache = {}
        self._stat_cache_ttl = 3.0  # seconds
        # Session-info cache for fast restarts: content_id -> {"data": {...}, "ts": float}
        self._session_cache = {}
        self._session_cache_ttl = float(os.environ.get("OPENACE_SESSION_CACHE_TTL_S", "30"))
        self._cleanup_orphan_ffmpeg()
        self._reaper_thread = threading.Thread(target=self._reaper_loop, name="ffmpeg-reaper", daemon=True)
        self._reaper_thread.start()

    def snapshot(self):
        """Return a list of dicts describing active HLS sessions.

        Each entry now includes engine-level stats when available:
        ``peers``, ``speed_down``, ``speed_up``, ``p2p_current_rate``,
        ``current_bitrate``, ``avg_bitrate``, ``player_buffer_time``,
        ``live_buffer_time``, ``stat_status``.
        """
        out = []
        with self._lock:
            items = list(self._streams.items())
        now = time.monotonic()
        for cid, sess in items:
            try:
                alive = sess.process.poll() is None
                idle = round(sess.idle_seconds())
                pid = sess.process.pid
            except Exception:
                alive, idle, pid = False, 0, None
            entry = {
                "content_id": cid,
                "full_id": cid,
                "alive": alive,
                "idle_s": idle,
                "pid": pid,
                "is_live": sess.is_live,
            }
            # Best-effort engine stat enrichment.
            stat = self._read_cached_stat(cid, now)
            if stat:
                entry["peers"] = _to_int(stat.get("peers"))
                entry["speed_down"] = _to_int(stat.get("speed_down"))
                entry["speed_up"] = _to_int(stat.get("speed_up"))
                entry["p2p_current_rate"] = _to_int(stat.get("p2p_current_rate"))
                entry["current_bitrate"] = _to_int(stat.get("current_bitrate"))
                entry["avg_bitrate"] = _to_int(stat.get("avg_bitrate"))
                entry["stat_status"] = stat.get("status")
            out.append(entry)
        return out

    def _read_cached_stat(self, content_id, now):
        """Read engine stat for a stream, with a short cache to avoid hammering."""
        with self._lock:
            cached = self._stat_cache.get(content_id)
            if cached and now - cached["ts"] < self._stat_cache_ttl:
                return cached["data"]
            sess = self._streams.get(content_id)
            stat_url = sess.stat_url if sess else None
        stat = None
        if stat_url:
            try:
                stat = read_stat(stat_url, request_timeout=3,
                                 component=COMPONENT,
                                 log_context={"content_id": content_id})
            except Exception:
                stat = None
        with self._lock:
            self._stat_cache[content_id] = {"data": stat, "ts": now}
        return stat

    def ensure_stream(self, content_id: str) -> str | None:
        starter = False
        with self._lock:
            if self._closed:
                log_event("warning", "ffmpeg_manager_closed", COMPONENT, content_id=content_id)
                return None
            session = self._streams.get(content_id)
            if session and session.process.poll() is None:
                session.touch()
                return session.output_dir

            alive_count = sum(
                1 for s in self._streams.values() if s.process.poll() is None
            )
            if alive_count >= MAX_CONCURRENT_STREAMS:
                log_event("warning", "ffmpeg_max_streams", COMPONENT,
                          content_id=content_id,
                          active=alive_count, cap=MAX_CONCURRENT_STREAMS)
                return None

            event = self._starting.get(content_id)
            if event is None:
                event = threading.Event()
                self._starting[content_id] = event
                starter = True

        if not starter:
            log_event("info", "ffmpeg_start_wait", COMPONENT, content_id=content_id)
            if not event.wait(timeout=START_WAIT_TIMEOUT_S):
                log_event("warning", "ffmpeg_start_wait_timeout", COMPONENT, content_id=content_id)
                return None
            with self._lock:
                if self._closed:
                    log_event("warning", "ffmpeg_start_wait_closed", COMPONENT, content_id=content_id)
                    return None
                session = self._streams.get(content_id)
                if session and session.process.poll() is None:
                    session.touch()
                    log_event("info", "ffmpeg_start_wait_done", COMPONENT, content_id=content_id)
                    return session.output_dir
            log_event("warning", "ffmpeg_start_wait_failed", COMPONENT, content_id=content_id)
            return None

        # Wait for the engine to report a ready stream before spawning FFmpeg.
        # Done outside the lock so a slow cold start does not serialize spawns of
        # other content ids. The _starting event prevents duplicate starts for
        # the same content id.
        log_context = {"content_id": content_id}
        session_info = None
        try:
            session_info = negotiate_stream(self._engine_url, content_id,
                                            component=COMPONENT, log_context=log_context)
            if session_info is None:
                return None

            # Cache for fast restarts.
            with self._lock:
                self._session_cache[content_id] = {
                    "data": session_info,
                    "ts": time.monotonic(),
                }

            out_dir = None
            stale_command_url = None
            closed_after_negotiate = False
            spawn_failed = False
            reserve_spawn = False
            spawned_session = None
            cancel_spawned_session = False
            with self._lock:
                if self._closed:
                    closed_after_negotiate = True
                else:
                    existing = self._streams.get(content_id)
                    if existing and existing.process.poll() is None:
                        existing.touch()
                        out_dir = existing.output_dir
                        if session_info["command_url"] != existing.command_url:
                            stale_command_url = session_info["command_url"]
                    else:
                        alive_count = sum(
                            1 for s in self._streams.values()
                            if s.process.poll() is None
                        )
                        if alive_count + self._spawn_reservations >= MAX_CONCURRENT_STREAMS:
                            log_event("warning", "ffmpeg_max_streams_race",
                                      COMPONENT, content_id=content_id,
                                      active=alive_count, cap=MAX_CONCURRENT_STREAMS)
                            stale_command_url = session_info["command_url"]
                        else:
                            self._spawn_reservations += 1
                            reserve_spawn = True

            if reserve_spawn:
                try:
                    spawned_session = self._spawn(content_id, session_info)
                    if (spawned_session is not None
                            and session_info.get("direct_getstream")
                            and not self._direct_getstream_produced_data(
                                spawned_session, content_id)):
                        self._terminate_process(
                            spawned_session.process, content_id,
                            "direct_getstream_probe_failed")
                        spawned_session.close_subscribers()
                        shutil.rmtree(spawned_session.output_dir, ignore_errors=True)
                        spawned_session = None
                except Exception:
                    spawn_failed = True
                with self._lock:
                    self._spawn_reservations -= 1
                    if self._closed:
                        closed_after_negotiate = True
                    else:
                        existing = self._streams.get(content_id)
                        if existing and existing.process.poll() is None:
                            existing.touch()
                            out_dir = existing.output_dir
                            if session_info["command_url"] != existing.command_url:
                                stale_command_url = session_info["command_url"]
                            cancel_spawned_session = spawned_session is not None
                        elif spawned_session is not None:
                            self._streams[content_id] = spawned_session
                            out_dir = spawned_session.output_dir
                        else:
                            spawn_failed = True

            if spawned_session is not None and out_dir != spawned_session.output_dir:
                cancel_spawned_session = True
            if cancel_spawned_session:
                self._terminate_process(spawned_session.process, content_id, "spawn_cancelled")
                stop_stream(spawned_session.command_url, component=COMPONENT, log_context=log_context)
                shutil.rmtree(spawned_session.output_dir, ignore_errors=True)

            if closed_after_negotiate:
                stop_stream(session_info["command_url"], component=COMPONENT, log_context=log_context)
                return None

            if stale_command_url:
                stop_stream(stale_command_url, component=COMPONENT, log_context=log_context)
            elif out_dir is None or spawn_failed:
                stop_stream(session_info["command_url"], component=COMPONENT, log_context=log_context)
            if spawn_failed:
                log_event("error", "ffmpeg_spawn_failed", COMPONENT, content_id=content_id)
                return None
            return out_dir
        finally:
            with self._lock:
                if self._starting.get(content_id) is event:
                    del self._starting[content_id]
            event.set()

    def subscribe_mpegts(self, content_id: str):
        out_dir = self.ensure_stream(content_id)
        if out_dir is None:
            return None, None
        with self._lock:
            session = self._streams.get(content_id)
        if session is None or session.process.poll() is not None:
            return None, None
        q = session.subscribe()
        if q is None:
            return None, None
        log_event("info", "ffmpeg_mpegts_subscribed", COMPONENT,
                  content_id=content_id, clients=session.client_count)
        return session, q

    def unsubscribe_mpegts(self, session: "_StreamSession", q):
        session.unsubscribe(q)
        log_event("info", "ffmpeg_mpegts_unsubscribed", COMPONENT,
                  clients=session.client_count)

    def iterate_mpegts(self, q, component=COMPONENT, log_context=None):
        ctx = log_context or {}
        while True:
            try:
                item = q.get(timeout=ITERATE_TIMEOUT_S)
            except Empty:
                log_event("warning", "ffmpeg_mpegts_client_timeout", component, **ctx)
                break
            if item is _SENTINEL:
                break
            if item is _ERROR_SENTINEL:
                log_event("warning", "ffmpeg_mpegts_stream_error", component, **ctx)
                break
            yield item

    def touch(self, content_id: str):
        with self._lock:
            session = self._streams.get(content_id)
        if session:
            session.touch()

    def request_hls(self, content_id: str) -> bool:
        """M4: an HLS client is waiting. In lazy mode, mark the session so the
        next restart also produces the HLS output, and trigger that restart if
        the current process was spawned without HLS. Returns False when there is
        nothing to upgrade (no session or lazy mode disabled).
        """
        if not HLS_LAZY:
            return False
        trigger_restart = False
        with self._lock:
            session = self._streams.get(content_id)
            if session is None:
                return False
            if session.hls_requested:
                return True
            session.hls_requested = True
            session.touch()
            if session.process.poll() is None and not session.restarting:
                session.restarting = True
                trigger_restart = True
        if trigger_restart:
            threading.Thread(
                target=self._restart_session, args=(content_id, session),
                name="ffmpeg-hls-restart", daemon=True,
            ).start()
        return True

    def shutdown(self, reason="shutdown"):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._streams.items())
            self._streams.clear()
            starting = list(self._starting.values())
            self._starting.clear()

        for event in starting:
            event.set()
        self._stop_event.set()

        log_event("info", "ffmpeg_manager_shutdown", COMPONENT,
                  reason=reason, streams=len(sessions), starting=len(starting))
        for content_id, session in sessions:
            self._kill_session(content_id, session, reason=reason)

        if self._reaper_thread is not threading.current_thread():
            self._reaper_thread.join(timeout=10)

    def output_dir(self, content_id: str) -> str:
        with self._lock:
            session = self._streams.get(content_id)
            if session is not None:
                return session.output_dir
        return os.path.join(HLS_OUTPUT_BASE, content_id)

    def _new_output_dir(self, content_id: str) -> str:
        return os.path.join(HLS_OUTPUT_BASE, content_id, uuid.uuid4().hex)

    def _cleanup_orphan_ffmpeg(self):
        if not os.path.isdir("/proc"):
            return
        killed = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or pid == str(os.getpid()):
                continue
            cmdline_path = os.path.join("/proc", pid, "cmdline")
            try:
                with open(cmdline_path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if not raw:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode(errors="replace")
            if not self._is_openace_ffmpeg_cmdline(cmdline):
                continue
            if self._terminate_pid(int(pid)):
                killed += 1
                log_event("warning", "ffmpeg_orphan_cleaned", COMPONENT,
                          pid=int(pid), cmdline=cmdline[:500])
        if killed:
            log_event("warning", "ffmpeg_orphans_cleaned", COMPONENT, count=killed)

    @staticmethod
    def _is_openace_ffmpeg_cmdline(cmdline: str) -> bool:
        return (
            "ffmpeg" in cmdline
            and HLS_OUTPUT_BASE in cmdline
            and MANIFEST_FILENAME in cmdline
            and "-f hls" in cmdline
            and "pipe:1" in cmdline
        )

    @staticmethod
    def _terminate_pid(pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except OSError as exc:
            log_event("warning", "ffmpeg_orphan_terminate_failed", COMPONENT,
                      pid=pid, error=str(exc))
            return False

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                log_event("warning", "ffmpeg_orphan_permission_denied", COMPONENT, pid=pid)
                return False
            except OSError as exc:
                log_event("warning", "ffmpeg_orphan_check_failed", COMPONENT,
                          pid=pid, error=str(exc))
                return False
            time.sleep(0.1)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError as exc:
            log_event("warning", "ffmpeg_orphan_kill_failed", COMPONENT,
                      pid=pid, error=str(exc))
            return False
        return True

    def _build_ffmpeg_cmd(self, content_id: str, session_info: dict, out_dir: str,
                          include_hls: bool = True):
        os.makedirs(out_dir, exist_ok=True)
        source_url = session_info["playback_url"]
        is_live = session_info.get("is_live")
        stat_hints = session_info.get("stat_hints") or {}

        hls_time = HLS_TIME_LIVE if is_live else HLS_TIME
        hls_list_size = HLS_LIST_SIZE_LIVE if is_live else HLS_LIST_SIZE
        rtbufsize = FFMPEG_RTBUFSIZE

        # M2: dynamically tune the realtime buffer / live segment size from the
        # engine-reported stats (only when the operator opts in).
        if FFMPEG_STAT_TUNING:
            current_bitrate = stat_hints.get("current_bitrate")
            player_buffer_time = stat_hints.get("player_buffer_time")
            if current_bitrate:
                tuned = _compute_rtbufsize(current_bitrate, FFMPEG_RTBUFSIZE)
                if tuned != FFMPEG_RTBUFSIZE:
                    rtbufsize = tuned
                    log_event("info", "ffmpeg_rtbufsize_tuned", COMPONENT,
                              content_id=content_id, bitrate=current_bitrate,
                              rtbufsize=rtbufsize)
            if is_live and player_buffer_time:
                tuned_hls = _compute_hls_time(player_buffer_time, HLS_TIME_LIVE)
                if tuned_hls != hls_time:
                    hls_time = tuned_hls
                    log_event("info", "ffmpeg_hls_time_tuned", COMPONENT,
                              content_id=content_id,
                              player_buffer_time=player_buffer_time, hls_time=hls_time)

        cmd = [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "warning",
            "-probesize", "50000",
            "-analyzeduration", "500000",
            "-fflags", "+nobuffer+discardcorrupt",
            "-rtbufsize", rtbufsize,
            "-thread_queue_size", "4096",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_on_http_error", "4xx,5xx",
            "-reconnect_delay_max", "30",
            "-rw_timeout", FFMPEG_RW_TIMEOUT_US,
            "-i", source_url,
        ]
        if include_hls:
            manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
            segment_pattern = os.path.join(out_dir, f"{SEGMENT_PREFIX}%03d.ts")
            cmd += [
                "-map", "0", "-c", "copy",
                "-f", "hls",
                "-hls_time", str(hls_time),
                "-hls_list_size", str(hls_list_size),
                "-hls_delete_threshold", str(HLS_DELETE_THRESHOLD),
                "-hls_flags", HLS_FLAGS,
                "-hls_segment_filename", segment_pattern,
                manifest_path,
            ]
        # MPEG-TS fan-out is always produced (pipe:1); it is the primary output.
        cmd += [
            "-map", "0", "-c", "copy",
            "-f", "mpegts", "-flush_packets", "1",
            "pipe:1",
        ]
        return cmd

    def _popen_ffmpeg(self, content_id: str, session_info: dict, out_dir=None, include_hls=True):
        out_dir = out_dir or self._new_output_dir(content_id)
        cmd = self._build_ffmpeg_cmd(content_id, session_info, out_dir, include_hls=include_hls)
        log_event("info", "ffmpeg_spawn", COMPONENT, content_id=content_id)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except (FileNotFoundError, OSError) as exc:
            log_event("error", "ffmpeg_spawn_failed", COMPONENT, content_id=content_id, error=str(exc))
            return None
        proc._openace_output_dir = out_dir
        if proc.stdout and PIPE_BUFFER_SIZE > 0:
            try:
                fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, PIPE_BUFFER_SIZE)
            except OSError as exc:
                log_event("warning", "ffmpeg_pipe_resize_failed", COMPONENT,
                          content_id=content_id, requested=PIPE_BUFFER_SIZE, error=str(exc))
        if proc.stderr and PIPE_BUFFER_SIZE > 0:
            try:
                fcntl.fcntl(proc.stderr.fileno(), fcntl.F_SETPIPE_SZ, PIPE_BUFFER_SIZE)
            except OSError as exc:
                log_event("warning", "ffmpeg_stderr_pipe_resize_failed", COMPONENT,
                          content_id=content_id, requested=PIPE_BUFFER_SIZE, error=str(exc))
        return proc

    def _process_survived_probe(self, process, content_id: str, event: str, **payload) -> bool:
        deadline = time.monotonic() + FFMPEG_START_PROBE_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_event("warning", event, COMPONENT,
                          content_id=content_id, returncode=process.returncode,
                          **payload)
                return False
            time.sleep(0.1)
        if process.poll() is not None:
            log_event("warning", event, COMPONENT,
                      content_id=content_id, returncode=process.returncode,
                      **payload)
            return False
        return True

    def _process_survived_start_probe(self, process, content_id: str, attempt: int) -> bool:
        return self._process_survived_probe(
            process, content_id, "ffmpeg_fast_restart_failed", attempt=attempt
        )

    def _direct_getstream_produced_data(self, session: "_StreamSession", content_id: str) -> bool:
        q = session.subscribe()
        if q is None:
            log_event("warning", "ffmpeg_direct_getstream_probe_closed", COMPONENT,
                      content_id=content_id)
            return False
        deadline = time.monotonic() + FFMPEG_START_PROBE_S
        try:
            while True:
                if session.process.poll() is not None:
                    log_event("warning", "ffmpeg_direct_getstream_probe_failed", COMPONENT,
                              content_id=content_id, returncode=session.process.returncode)
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log_event("warning", "ffmpeg_direct_getstream_probe_timeout", COMPONENT,
                              content_id=content_id)
                    return False
                try:
                    item = q.get(timeout=min(0.2, remaining))
                except Empty:
                    continue
                if item is _SENTINEL or item is _ERROR_SENTINEL:
                    log_event("warning", "ffmpeg_direct_getstream_probe_closed", COMPONENT,
                              content_id=content_id)
                    return False
                if item:
                    return True
        finally:
            session.unsubscribe(q)

    def _spawn(self, content_id: str, session_info: dict) -> "_StreamSession | None":
        out_dir = self._new_output_dir(content_id)
        # M4: in lazy mode the initial spawn only produces MPEG-TS; HLS is added
        # on demand via request_hls() triggering a restart.
        process = self._popen_ffmpeg(content_id, session_info, out_dir, include_hls=not HLS_LAZY)
        if process is None:
            return None
        log_event("info", "ffmpeg_stream_config", COMPONENT,
                  content_id=content_id, chunk_size=CHUNK_SIZE,
                  queue_max=session_info.get("is_live") and QUEUE_MAX_LIVE or QUEUE_MAX_VOD,
                  pipe_buffer=PIPE_BUFFER_SIZE, hls_lazy=HLS_LAZY, ts_align=TS_ALIGN,
                  stat_tuning=FFMPEG_STAT_TUNING)
        session = None
        try:
            session = _StreamSession(process=process, output_dir=out_dir,
                                     command_url=session_info.get("command_url"),
                                     playback_url=session_info.get("playback_url"),
                                     is_live=session_info.get("is_live"),
                                     stat_url=session_info.get("stat_url"))
            self._start_stdout_drain(content_id, session)
            self._start_stderr_drain(session)
        except Exception:
            self._terminate_process(process, content_id, "spawn_drain_failed")
            if session is not None:
                session.close_subscribers()
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        return session

    def _restart_session(self, content_id: str, session: "_StreamSession"):
        with self._lock:
            if self._streams.get(content_id) is not session or self._closed:
                return False
        if session.restart_attempts >= MAX_RESTART_ATTEMPTS:
            session.restarting = False
            return False
        if session.client_count == 0 and not session.hls_requested:
            session.restarting = False
            return False

        session.restart_attempts += 1
        attempt = session.restart_attempts
        log_event("warning", "ffmpeg_restart_attempt", COMPONENT,
                  content_id=content_id, attempt=attempt, clients=session.client_count)

        # M4: once an HLS client has requested the manifest in lazy mode, every
        # subsequent restart must include the HLS output.
        include_hls = (not HLS_LAZY) or session.hls_requested

        log_context = {"content_id": content_id, "restart_attempt": attempt}
        session_info = None
        process = None
        # Try session cache first (avoids a full negotiate round-trip).
        with self._lock:
            cached = self._session_cache.get(content_id)
        if cached and time.monotonic() - cached["ts"] < self._session_cache_ttl:
            fast_info = cached["data"]
            process = self._popen_ffmpeg(content_id, fast_info, include_hls=include_hls)
            if process is not None:
                if self._process_survived_start_probe(process, content_id, attempt):
                    session_info = fast_info
                    log_event("info", "ffmpeg_fast_restart_done", COMPONENT,
                              content_id=content_id, attempt=attempt, source="cache")
                else:
                    process = None
        elif session.playback_url:
            fast_info = {"playback_url": session.playback_url,
                         "command_url": session.command_url,
                         "stat_url": session.stat_url,
                         "is_live": session.is_live}
            process = self._popen_ffmpeg(content_id, fast_info, include_hls=include_hls)
            if process is not None:
                if self._process_survived_start_probe(process, content_id, attempt):
                    session_info = fast_info
                    log_event("info", "ffmpeg_fast_restart_done", COMPONENT,
                              content_id=content_id, attempt=attempt)
                else:
                    process = None

        if process is None:
            time.sleep(RESTART_BACKOFF_S * attempt)
            session_info = negotiate_stream(self._engine_url, content_id,
                                            component=COMPONENT, log_context=log_context)
            if session_info is None:
                log_event("warning", "ffmpeg_restart_negotiate_failed", COMPONENT,
                          content_id=content_id, attempt=attempt)
                session.restarting = False
                return False

            process = self._popen_ffmpeg(content_id, session_info, include_hls=include_hls)
            if process is None:
                stop_stream(session_info.get("command_url"), component=COMPONENT, log_context=log_context)
                session.restarting = False
                return False

        old_command_url = session.command_url
        old_process = session.process
        old_stderr_thread = session.stderr_thread
        old_stdout = old_process.stdout
        old_stderr = old_process.stderr
        old_uptime = time.monotonic() - session.process_started_at
        old_bytes_produced = session.bytes_total - session.process_start_bytes_total
        reset_restart_attempts = (
            old_uptime >= RESTART_RESET_MIN_UPTIME_S
            and old_bytes_produced > 0
        )
        stderr_bytes = b"".join(session.stderr_tail)
        if stderr_bytes:
            log_event("warning", "ffmpeg_restart_stderr", COMPONENT,
                      content_id=content_id,
                      output=stderr_bytes.decode(errors="replace")[-2000:])
        with self._lock:
            if self._streams.get(content_id) is not session or self._closed:
                try:
                    process.terminate()
                except OSError:
                    pass
                stop_stream(session_info.get("command_url"), component=COMPONENT, log_context=log_context)
                session.restarting = False
                return False
            session.process = process
            session.process_started_at = time.monotonic()
            session.process_start_bytes_total = session.bytes_total
            session.output_dir = getattr(process, "_openace_output_dir", session.output_dir)
            session.command_url = session_info.get("command_url")
            session.playback_url = session_info.get("playback_url")
            session.stderr_tail.clear()
            session.restarting = False
            if reset_restart_attempts:
                session.restart_attempts = 0
            session.touch()
        self._start_stdout_drain(content_id, session)
        self._start_stderr_drain(session)
        self._terminate_process(old_process, content_id, "restart_old_process")
        for old_pipe in (old_stdout, old_stderr):
            try:
                if old_pipe is not None:
                    old_pipe.close()
            except OSError:
                pass
        if old_stderr_thread is not None:
            old_stderr_thread.join(timeout=2)
        if old_command_url and old_command_url != session.command_url:
            stop_stream(old_command_url, component=COMPONENT, log_context=log_context)
        log_event("info", "ffmpeg_restart_done", COMPONENT,
                  content_id=content_id, attempt=attempt, clients=session.client_count)
        return True

    def _terminate_process(self, process, content_id: str, reason: str):
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return
        log_event("warning", "ffmpeg_terminate", COMPONENT,
                  content_id=content_id, pid=process.pid, reason=reason)
        try:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            log_event("warning", "ffmpeg_terminate_failed", COMPONENT,
                      content_id=content_id, pid=process.pid, reason=reason)

    def _start_stdout_drain(self, content_id: str, session: "_StreamSession"):
        """Continuously drain FFmpeg's MPEG-TS stdout and fan it out to clients."""
        stream = session.process.stdout
        if stream is None:
            return

        def _drain():
            log_event("info", "ffmpeg_stdout_started", COMPONENT, content_id=content_id)
            fd = stream.fileno()
            try:
                while True:
                    readable, _, _ = select.select([fd], [], [], 5.0)
                    if not readable:
                        continue
                    chunk = os.read(fd, CHUNK_SIZE)
                    if not chunk:
                        break
                    if session._subscriber_count:
                        if TS_ALIGN:
                            was_synced = session.ts_synced
                            before = len(session.ts_residue) + len(chunk)
                            body, session.ts_residue, session.ts_synced = _align_ts_packets(
                                chunk, session.ts_residue, session.ts_synced)
                            after = len(body) + len(session.ts_residue)
                            session.dropped_lead_bytes += before - after
                            if not was_synced and session.ts_synced and session.dropped_lead_bytes:
                                log_event("info", "ffmpeg_ts_synced", COMPONENT,
                                          content_id=content_id,
                                          dropped_lead_bytes=session.dropped_lead_bytes)
                            if body:
                                session.broadcast(body, content_id)
                        else:
                            session.broadcast(chunk, content_id)
            except (OSError, ValueError, select.error) as exc:
                log_event("warning", "ffmpeg_stdout_error", COMPONENT,
                          content_id=content_id, error=str(exc))
            finally:
                log_event("info", "ffmpeg_stdout_closed", COMPONENT, content_id=content_id)
                should_restart = False
                with self._lock:
                    if (self._streams.get(content_id) is session
                            and not self._closed
                            and (session.client_count > 0 or session.hls_requested)
                            and session.restart_attempts < MAX_RESTART_ATTEMPTS
                            and not session.restarting):
                        session.restarting = True
                        should_restart = True
                restarted_ok = should_restart and self._restart_session(content_id, session)
                if restarted_ok:
                    return
                # Source died and could not be recovered: signal the hard error
                # to any remaining subscriber before the clean close.
                if session.client_count > 0:
                    session.broadcast_error(content_id)
                session.close_subscribers()

        thread = threading.Thread(target=_drain, name="ffmpeg-stdout", daemon=True)
        session.stdout_thread = thread
        thread.start()

    def _start_stderr_drain(self, session: "_StreamSession"):
        """Continuously drain FFmpeg's stderr into a bounded buffer.

        Without an active reader the OS pipe (~64 KB) fills on chatty live-TS
        remuxes (e.g. repeated "Non-monotonous DTS" warnings), at which point
        FFmpeg blocks on the write and stops producing HLS/MPEG-TS output.
        """
        stream = session.process.stderr
        if stream is None:
            return
        buf = session.stderr_tail

        def _drain():
            try:
                for line in stream:
                    buf.append(line)
            except (OSError, ValueError):
                pass

        thread = threading.Thread(target=_drain, name="ffmpeg-stderr", daemon=True)
        session.stderr_thread = thread
        thread.start()

    def is_alive(self, content_id: str) -> bool:
        with self._lock:
            session = self._streams.get(content_id)
        return session is not None and session.process.poll() is None

    def drop(self, content_id: str):
        with self._lock:
            session = self._streams.pop(content_id, None)
        if session is not None:
            self._kill_session(content_id, session, reason="drop")
        return session

    def _kill_session(self, content_id: str, session: "_StreamSession", reason="cleanup"):
        idle_seconds = round(session.idle_seconds(), 2)
        clients = session.client_count
        stream_registry.clear_hls(content_id)
        try:
            from app.routes import hls as _hls
            _hls.clear_stale_log(content_id)
        except Exception:
            pass
        session.close_subscribers()
        log_event(
            "info", "ffmpeg_killing", COMPONENT,
            content_id=content_id,
            pid=session.process.pid,
            reason=reason,
            idle_seconds=idle_seconds,
            clients=clients,
        )
        try:
            session.process.terminate()
            try:
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.process.kill()
                try:
                    session.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log_event("warning", "ffmpeg_kill_timeout", COMPONENT,
                              content_id=content_id, pid=session.process.pid)
        except OSError:
            pass
        if session.stdout_thread is not None:
            session.stdout_thread.join(timeout=1)
        if session.stderr_thread is not None:
            session.stderr_thread.join(timeout=1)
        for pipe in (session.process.stdout, session.process.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        stderr_bytes = b"".join(session.stderr_tail)
        if stderr_bytes:
            log_event("warning", "ffmpeg_stderr", COMPONENT, content_id=content_id,
                      output=stderr_bytes.decode(errors="replace")[-2000:])
        stop_stream(session.command_url, component=COMPONENT, log_context={"content_id": content_id})
        shutil.rmtree(session.output_dir, ignore_errors=True)
        log_event("info", "ffmpeg_cleaned", COMPONENT, content_id=content_id)

    def _drop_session_if_current(self, content_id: str, session: "_StreamSession", reason="cleanup"):
        with self._lock:
            if self._streams.get(content_id) is not session:
                return False
            if reason == "idle_timeout":
                if session.client_count > 0 or session.idle_seconds() < IDLE_TIMEOUT_S:
                    return False
            elif reason == "process_exited":
                if session.restarting or session.process.poll() is None:
                    return False
            del self._streams[content_id]
        self._kill_session(content_id, session, reason=reason)
        return True

    def _reaper_loop(self):
        while not self._stop_event.wait(REAPER_INTERVAL_S):
            stream_registry.reap_expired()
            with self._lock:
                if self._closed:
                    return
                candidates = []
                for cid, sess in self._streams.items():
                    if sess.process.poll() is not None:
                        if not sess.restarting:
                            candidates.append((cid, sess, "process_exited"))
                    elif sess.client_count == 0 and sess.idle_seconds() >= IDLE_TIMEOUT_S:
                        candidates.append((cid, sess, "idle_timeout"))
            for content_id, session, reason in candidates:
                self._drop_session_if_current(content_id, session, reason=reason)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
