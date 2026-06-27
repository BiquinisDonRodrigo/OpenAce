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

from app.utils.acestream import negotiate_stream, read_stat, stop_stream
from app.utils import stream_registry
from app.utils.logging_utils import log_event

COMPONENT = "ffmpeg_manager"
HLS_OUTPUT_BASE = "/tmp/openace"
IDLE_TIMEOUT_S = int(os.environ.get("OPENACE_IDLE_TIMEOUT_S", "180"))
REAPER_INTERVAL_S = 10
HLS_TIME = int(os.environ.get("OPENACE_HLS_TIME", "4"))
HLS_LIST_SIZE = int(os.environ.get("OPENACE_HLS_LIST_SIZE", "15"))
# Tuned values for live streams: shorter segments + smaller list = lower latency.
HLS_TIME_LIVE = int(os.environ.get("OPENACE_HLS_TIME_LIVE", "2"))
HLS_LIST_SIZE_LIVE = int(os.environ.get("OPENACE_HLS_LIST_SIZE_LIVE", "6"))
HLS_DELETE_THRESHOLD = 10
# Real-time buffer limit for FFmpeg (reduces latency on live content).
FFMPEG_RTBUFSIZE = os.environ.get("OPENACE_FFMPEG_RTBUFSIZE", "5M")
HLS_FLAGS = "delete_segments+append_list+omit_endlist+program_date_time+temp_file"
MANIFEST_FILENAME = "playlist.m3u8"
SEGMENT_PREFIX = "seg"
CHUNK_SIZE = int(os.environ.get("OPENACE_CHUNK_SIZE", "65536"))
QUEUE_MAX = int(os.environ.get("OPENACE_QUEUE_MAX", "256"))
PIPE_BUFFER_SIZE = int(os.environ.get("OPENACE_PIPE_BUFFER_SIZE", "1048576"))
ITERATE_TIMEOUT_S = int(os.environ.get("OPENACE_ITERATE_TIMEOUT_S", "180"))
START_WAIT_TIMEOUT_S = 75
FFMPEG_RW_TIMEOUT_US = os.environ.get("OPENACE_FFMPEG_RW_TIMEOUT_US", "120000000")
MAX_RESTART_ATTEMPTS = int(os.environ.get("OPENACE_FFMPEG_RESTARTS", "3"))
RESTART_BACKOFF_S = float(os.environ.get("OPENACE_FFMPEG_RESTART_BACKOFF_S", "2"))
MAX_CONCURRENT_STREAMS = int(os.environ.get("OPENACE_MAX_STREAMS", "32"))
_SENTINEL = object()


class _StreamSession:
    __slots__ = (
        "process", "output_dir", "command_url", "playback_url", "stat_url", "last_request", "lock",
        "stderr_tail", "stderr_thread", "stdout_thread", "subscribers",
        "sub_lock", "closed", "restarting", "bytes_total", "bytes_window", "last_progress",
        "dropped_clients", "restart_attempts", "_subscriber_count", "is_live",
    )

    def __init__(self, process, output_dir, command_url=None, playback_url=None, is_live=None, stat_url=None):
        self.process = process
        self.output_dir = output_dir
        self.command_url = command_url
        self.playback_url = playback_url
        self.stat_url = stat_url
        self.is_live = is_live
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
        self.bytes_window = 0
        self.last_progress = time.monotonic()
        self.dropped_clients = 0
        self.restart_attempts = 0
        self._subscriber_count = 0

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
        q = Queue(maxsize=QUEUE_MAX)
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
    def _signal_queue(q):
        try:
            q.put_nowait(_SENTINEL)
            return True
        except Full:
            try:
                q.get_nowait()
            except Empty:
                pass
            try:
                q.put_nowait(_SENTINEL)
                return True
            except Full:
                return False

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
                queue_max=QUEUE_MAX,
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
                                queue_max=QUEUE_MAX,
                            )
                self._subscriber_count = len(self.subscribers)
            log_event(
                "warning", "ffmpeg_mpegts_queue_full", COMPONENT,
                content_id=content_id,
                dropped=len(stale),
                clients=len(subscribers) - len(stale),
                queue_max=QUEUE_MAX,
                chunk_size=CHUNK_SIZE,
            )

    def close_subscribers(self):
        with self.sub_lock:
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
        cached = self._stat_cache.get(content_id)
        if cached and now - cached["ts"] < self._stat_cache_ttl:
            return cached["data"]
        with self._lock:
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
            self._session_cache[content_id] = {
                "data": session_info,
                "ts": time.monotonic(),
            }

            out_dir = None
            stale_command_url = None
            closed_after_negotiate = False
            spawn_failed = False
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
                        if alive_count >= MAX_CONCURRENT_STREAMS:
                            log_event("warning", "ffmpeg_max_streams_race",
                                      COMPONENT, content_id=content_id,
                                      active=alive_count, cap=MAX_CONCURRENT_STREAMS)
                            stale_command_url = session_info["command_url"]
                        else:
                            try:
                                session = self._spawn(content_id, session_info)
                            except Exception:
                                spawn_failed = True
                            else:
                                if session is not None:
                                    self._streams[content_id] = session
                                    out_dir = session.output_dir

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
            yield item

    def touch(self, content_id: str):
        with self._lock:
            session = self._streams.get(content_id)
        if session:
            session.touch()

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
        return os.path.join(HLS_OUTPUT_BASE, content_id)

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

    def _build_ffmpeg_cmd(self, content_id: str, session_info: dict):
        out_dir = self.output_dir(content_id)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)
        manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
        segment_pattern = os.path.join(out_dir, f"{SEGMENT_PREFIX}%03d.ts")
        source_url = session_info["playback_url"]
        is_live = session_info.get("is_live")

        hls_time = HLS_TIME_LIVE if is_live else HLS_TIME
        hls_list_size = HLS_LIST_SIZE_LIVE if is_live else HLS_LIST_SIZE

        return [
            "ffmpeg", "-nostdin", "-y", "-loglevel", "warning",
            "-probesize", "50000",
            "-analyzeduration", "500000",
            "-fflags", "+nobuffer+discardcorrupt",
            "-rtbufsize", FFMPEG_RTBUFSIZE,
            "-thread_queue_size", "4096",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_on_http_error", "4xx,5xx",
            "-reconnect_delay_max", "30",
            "-rw_timeout", FFMPEG_RW_TIMEOUT_US,
            "-i", source_url,
            "-map", "0", "-c", "copy",
            "-f", "hls",
            "-hls_time", str(hls_time),
            "-hls_list_size", str(hls_list_size),
            "-hls_delete_threshold", str(HLS_DELETE_THRESHOLD),
            "-hls_flags", HLS_FLAGS,
            "-hls_segment_filename", segment_pattern,
            manifest_path,
            "-map", "0", "-c", "copy",
            "-f", "mpegts", "-flush_packets", "1",
            "pipe:1",
        ]

    def _popen_ffmpeg(self, content_id: str, session_info: dict):
        cmd = self._build_ffmpeg_cmd(content_id, session_info)
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
        if proc.stdout and PIPE_BUFFER_SIZE > 0:
            try:
                fcntl.fcntl(proc.stdout.fileno(), fcntl.F_SETPIPE_SZ, PIPE_BUFFER_SIZE)
            except OSError as exc:
                log_event("warning", "ffmpeg_pipe_resize_failed", COMPONENT,
                          content_id=content_id, requested=PIPE_BUFFER_SIZE, error=str(exc))
        return proc

    def _spawn(self, content_id: str, session_info: dict) -> "_StreamSession | None":
        out_dir = self.output_dir(content_id)
        process = self._popen_ffmpeg(content_id, session_info)
        if process is None:
            return None
        log_event("info", "ffmpeg_stream_config", COMPONENT,
                  content_id=content_id, chunk_size=CHUNK_SIZE,
                  queue_max=QUEUE_MAX, pipe_buffer=PIPE_BUFFER_SIZE)
        session = _StreamSession(process=process, output_dir=out_dir,
                                 command_url=session_info.get("command_url"),
                                 playback_url=session_info.get("playback_url"),
                                 is_live=session_info.get("is_live"),
                                 stat_url=session_info.get("stat_url"))
        self._start_stdout_drain(content_id, session)
        self._start_stderr_drain(session)
        return session

    def _restart_session(self, content_id: str, session: "_StreamSession"):
        with self._lock:
            if self._streams.get(content_id) is not session or self._closed:
                return False
        if session.restart_attempts >= MAX_RESTART_ATTEMPTS or session.client_count == 0:
            session.restarting = False
            return False

        session.restart_attempts += 1
        attempt = session.restart_attempts
        log_event("warning", "ffmpeg_restart_attempt", COMPONENT,
                  content_id=content_id, attempt=attempt, clients=session.client_count)

        log_context = {"content_id": content_id, "restart_attempt": attempt}
        session_info = None
        process = None
        # Try session cache first (avoids a full negotiate round-trip).
        cached = self._session_cache.get(content_id)
        if cached and time.monotonic() - cached["ts"] < self._session_cache_ttl:
            fast_info = cached["data"]
            process = self._popen_ffmpeg(content_id, fast_info)
            if process is not None:
                time.sleep(1)
                if process.poll() is None:
                    session_info = fast_info
                    log_event("info", "ffmpeg_fast_restart_done", COMPONENT,
                              content_id=content_id, attempt=attempt, source="cache")
                else:
                    log_event("warning", "ffmpeg_fast_restart_failed", COMPONENT,
                              content_id=content_id, attempt=attempt,
                              returncode=process.returncode)
                    process = None
        elif session.playback_url:
            fast_info = {"playback_url": session.playback_url,
                         "command_url": session.command_url,
                         "stat_url": session.stat_url,
                         "is_live": session.is_live}
            process = self._popen_ffmpeg(content_id, fast_info)
            if process is not None:
                time.sleep(1)
                if process.poll() is None:
                    session_info = fast_info
                    log_event("info", "ffmpeg_fast_restart_done", COMPONENT,
                              content_id=content_id, attempt=attempt)
                else:
                    log_event("warning", "ffmpeg_fast_restart_failed", COMPONENT,
                              content_id=content_id, attempt=attempt,
                              returncode=process.returncode)
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

            process = self._popen_ffmpeg(content_id, session_info)
            if process is None:
                stop_stream(session_info.get("command_url"), component=COMPONENT, log_context=log_context)
                session.restarting = False
                return False

        old_command_url = session.command_url
        old_process = session.process
        stderr_bytes = b"".join(session.stderr_tail)
        if stderr_bytes:
            log_event("warning", "ffmpeg_restart_stderr", COMPONENT,
                      content_id=content_id,
                      output=stderr_bytes.decode(errors="replace")[-2000:])
        try:
            old_process.wait(timeout=0)
        except (subprocess.TimeoutExpired, OSError):
            pass
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
            session.command_url = session_info.get("command_url")
            session.playback_url = session_info.get("playback_url")
            session.stderr_tail.clear()
            session.restarting = False
            session.touch()
        self._start_stdout_drain(content_id, session)
        self._start_stderr_drain(session)
        if old_command_url and old_command_url != session.command_url:
            stop_stream(old_command_url, component=COMPONENT, log_context=log_context)
        log_event("info", "ffmpeg_restart_done", COMPONENT,
                  content_id=content_id, attempt=attempt, clients=session.client_count)
        return True

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
                            and session.client_count > 0
                            and session.restart_attempts < MAX_RESTART_ATTEMPTS
                            and not session.restarting):
                        session.restarting = True
                        should_restart = True
                if should_restart and self._restart_session(content_id, session):
                    return
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
