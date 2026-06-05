import collections
import os
import shutil
import subprocess
import threading
import time

from app.utils.acestream import negotiate_stream, stop_stream
from app.utils.logging_utils import log_event
from app.utils.stream_registry import register as reg_stream, unregister as unreg_stream

COMPONENT = "ffmpeg_manager"
HLS_OUTPUT_BASE = "/tmp/openace"
IDLE_TIMEOUT_S = 60
REAPER_INTERVAL_S = 10
HLS_TIME = 2
HLS_LIST_SIZE = 10
HLS_FLAGS = "delete_segments+append_list"
MANIFEST_FILENAME = "playlist.m3u8"
SEGMENT_PREFIX = "seg"


class _StreamSession:
    __slots__ = ("process", "output_dir", "command_url", "last_request", "lock",
                 "stderr_tail", "stderr_thread")

    def __init__(self, process, output_dir, command_url=None):
        self.process = process
        self.output_dir = output_dir
        self.command_url = command_url
        self.last_request = time.monotonic()
        self.lock = threading.Lock()
        # Bounded ring buffer drained continuously by a daemon thread; keeps the
        # tail of FFmpeg's stderr without ever letting the OS pipe fill up.
        self.stderr_tail = collections.deque(maxlen=200)
        self.stderr_thread = None

    def touch(self):
        with self.lock:
            self.last_request = time.monotonic()

    def idle_seconds(self):
        with self.lock:
            return time.monotonic() - self.last_request


class FFmpegManager:
    def __init__(self, acestream_host="127.0.0.1", acestream_port="6878"):
        self._streams = {}
        self._lock = threading.Lock()
        self._engine_url = f"http://{acestream_host}:{acestream_port}"
        threading.Thread(target=self._reaper_loop, name="ffmpeg-reaper", daemon=True).start()

    def ensure_stream(self, content_id: str) -> str | None:
        with self._lock:
            session = self._streams.get(content_id)
            if session and session.process.poll() is None:
                session.touch()
                return session.output_dir

        # Wait for the engine to report a ready stream before spawning FFmpeg.
        # Done outside the lock so a slow cold start does not serialize spawns of
        # other content ids.
        log_context = {"content_id": content_id}
        session_info = negotiate_stream(self._engine_url, content_id,
                                        component=COMPONENT, log_context=log_context)
        if session_info is None:
            return None

        out_dir = None
        stale_command_url = None
        with self._lock:
            existing = self._streams.get(content_id)
            if existing and existing.process.poll() is None:
                # Another request spawned while we negotiated; reuse it and drop ours.
                existing.touch()
                out_dir = existing.output_dir
                stale_command_url = session_info["command_url"]
            else:
                session = self._spawn(content_id, session_info)
                if session is not None:
                    self._streams[content_id] = session
                    out_dir = session.output_dir
                    reg_stream(content_id, "hls")

        # Release engine sessions outside the lock (network I/O).
        if stale_command_url:
            stop_stream(stale_command_url, component=COMPONENT, log_context=log_context)
        elif out_dir is None:
            stop_stream(session_info["command_url"], component=COMPONENT, log_context=log_context)
        return out_dir

    def touch(self, content_id: str):
        with self._lock:
            session = self._streams.get(content_id)
        if session:
            session.touch()

    def output_dir(self, content_id: str) -> str:
        return os.path.join(HLS_OUTPUT_BASE, content_id)

    def _spawn(self, content_id: str, session_info: dict) -> "_StreamSession | None":
        out_dir = self.output_dir(content_id)
        os.makedirs(out_dir, exist_ok=True)
        manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
        segment_pattern = os.path.join(out_dir, f"{SEGMENT_PREFIX}%03d.ts")
        source_url = session_info["playback_url"]

        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_on_http_error", "4xx,5xx",
            "-reconnect_delay_max", "30",
            "-rw_timeout", "30000000",
            "-i", source_url,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(HLS_TIME),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", HLS_FLAGS,
            "-hls_segment_filename", segment_pattern,
            manifest_path,
        ]
        log_event("info", "ffmpeg_spawn", COMPONENT, content_id=content_id)
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                close_fds=True,
            )
        except (FileNotFoundError, OSError) as exc:
            log_event("error", "ffmpeg_spawn_failed", COMPONENT, content_id=content_id, error=str(exc))
            return None
        session = _StreamSession(process=process, output_dir=out_dir,
                                 command_url=session_info.get("command_url"))
        self._start_stderr_drain(session)
        return session

    def _start_stderr_drain(self, session: "_StreamSession"):
        """Continuously drain FFmpeg's stderr into a bounded buffer.

        Without an active reader the OS pipe (~64 KB) fills on chatty live-TS
        remuxes (e.g. repeated "Non-monotonous DTS" warnings), at which point
        FFmpeg blocks on the write and stops producing HLS segments.
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
            self._kill_session(content_id, session)
        return session

    def _kill_session(self, content_id: str, session: "_StreamSession"):
        unreg_stream(content_id, "hls")
        log_event("info", "ffmpeg_killing", COMPONENT, content_id=content_id, pid=session.process.pid)
        try:
            session.process.terminate()
            try:
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.process.kill()
                session.process.wait()
        except OSError:
            pass
        if session.stderr_thread is not None:
            session.stderr_thread.join(timeout=1)
        stderr_bytes = b"".join(session.stderr_tail)
        if stderr_bytes:
            log_event("warning", "ffmpeg_stderr", COMPONENT, content_id=content_id,
                      output=stderr_bytes.decode(errors="replace")[-2000:])
        stop_stream(session.command_url, component=COMPONENT, log_context={"content_id": content_id})
        shutil.rmtree(session.output_dir, ignore_errors=True)
        log_event("info", "ffmpeg_cleaned", COMPONENT, content_id=content_id)

    def _reaper_loop(self):
        while True:
            time.sleep(REAPER_INTERVAL_S)
            with self._lock:
                candidates = [
                    (cid, sess) for cid, sess in self._streams.items()
                    if sess.idle_seconds() >= IDLE_TIMEOUT_S or sess.process.poll() is not None
                ]
            for content_id, session in candidates:
                self._kill_session(content_id, session)
                with self._lock:
                    if self._streams.get(content_id) is session:
                        del self._streams[content_id]
