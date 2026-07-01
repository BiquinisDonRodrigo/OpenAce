"""Unit tests for the FFmpeg-proxy helpers and command building.

Covers the pure functions introduced for the AceStream-driven proxy improvements:
M2 (stat tuning), M3 (adaptive queue), M4 (lazy HLS), M5 (error sentinel) and
M6 (188-byte MPEG-TS alignment).
"""
import queue
import threading

from app.utils import ffmpeg_manager as fm
from app.utils.ffmpeg_manager import (
    _align_ts_packets,
    _compute_hls_time,
    _compute_rtbufsize,
    _humanize_bytes,
    _parse_ffmpeg_size,
    _StreamSession,
    FFmpegManager,
)


# ---------------------------------------------------------------------------
# M6 - MPEG-TS packet alignment
# ---------------------------------------------------------------------------
class TestAlignTsPackets:
    def test_passthrough_when_disabled(self, monkeypatch):
        monkeypatch.setattr(fm, "TS_ALIGN", False)
        body, residue, synced = _align_ts_packets(b"\x47" + b"x" * 187, b"", False)
        assert synced is True
        assert residue == b""
        assert body == b"\x47" + b"x" * 187

    def test_already_aligned_passes_through(self):
        packet = b"\x47" + b"\x00" * 187
        body, residue, synced = _align_ts_packets(packet * 3, b"", False)
        assert synced is True
        assert residue == b""
        assert body == packet * 3

    def test_drops_leading_garbage_until_sync(self):
        garbage = b"\x00\x01\x02\x03"
        packet = b"\x47" + b"\x00" * 187
        # two consecutive packets prove the 0x47 is a real sync, not payload
        body, residue, synced = _align_ts_packets(garbage + packet * 2, b"", False)
        assert synced is True
        assert residue == b""
        assert body == packet * 2
        # leading garbage was dropped (not forwarded to subscribers)
        assert b"\x00\x01\x02\x03" not in body

    def test_non_multiple_chunk_keeps_residue(self):
        packet = b"\x47" + b"\x00" * 187
        data = packet * 2 + b"\x47" + b"\x00" * 50  # 50 trailing bytes (< 188)
        body, residue, synced = _align_ts_packets(data, b"", False)
        assert synced is True
        assert len(residue) == 51  # 1 sync byte + 50 payload bytes
        assert len(body) == 188 * 2
        # residue carries over and is completed by the next chunk
        body2, residue2, synced2 = _align_ts_packets(b"\x00" * 137, residue, synced)
        assert synced2 is True
        assert residue2 == b""
        assert len(body2) == 188

    def test_no_sync_keeps_tail_and_stays_unsynced(self):
        body, residue, synced = _align_ts_packets(b"\x00" * 500, b"", False)
        assert synced is False
        assert body == b""  # nothing forwarded while unsynced
        assert len(residue) <= 188  # only a tail worth re-scanning is kept


# ---------------------------------------------------------------------------
# M2 - stat-driven tuning helpers
# ---------------------------------------------------------------------------
class TestStatTuningHelpers:
    def test_humanize_roundtrip(self):
        assert _humanize_bytes(8 * 1024 * 1024) == "8M"
        assert _humanize_bytes(1024) == "1K"
        assert _humanize_bytes(0) == "0"

    def test_parse_ffmpeg_size(self):
        assert _parse_ffmpeg_size("5M") == 5 * 1024 * 1024
        assert _parse_ffmpeg_size("8388608") == 8388608
        assert _parse_ffmpeg_size("2G") == 2 * 1024 * 1024 * 1024

    def test_rtbufsize_zero_bitrate_returns_base(self):
        assert _compute_rtbufsize(0, "5M") == "5M"

    def test_rtbufsize_invalid_bitrate_returns_base(self):
        assert _compute_rtbufsize("nonsense", "5M") == "5M"

    def test_rtbufsize_high_bitrate_grows_buffer(self):
        # 10 Mbps -> 5s buffer ~= 6.25MB -> larger than the 5M base
        result = _compute_rtbufsize(10_000_000, "5M")
        assert _parse_ffmpeg_size(result) >= _parse_ffmpeg_size("5M")
        assert _parse_ffmpeg_size(result) >= (10_000_000 * 5 // 8)

    def test_rtbufsize_low_bitrate_keeps_base(self):
        # 100 kbps -> 5s ~= 62KB -> below base, base wins
        assert _compute_rtbufsize(100_000, "5M") == "5M"

    def test_hls_time_zero_buffer_returns_base(self):
        assert _compute_hls_time(0, 2) == 2

    def test_hls_time_small_buffer_is_capped(self):
        # buffer 3s -> floor(3/2) = 1
        assert _compute_hls_time(3, 2) == 1

    def test_hls_time_large_buffer_keeps_base(self):
        # buffer 30s -> floor(30/2)=15 but base caps to 2
        assert _compute_hls_time(30, 2) == 2

    def test_hls_time_never_below_one(self):
        assert _compute_hls_time(1, 4) >= 1


# ---------------------------------------------------------------------------
# M4 - build command with / without HLS, with / without stat tuning
# ---------------------------------------------------------------------------
class TestBuildCmd:
    def _manager(self, monkeypatch):
        # avoid scanning /proc and starting the reaper during unit tests
        monkeypatch.setattr(FFmpegManager, "_cleanup_orphan_ffmpeg", lambda self: None)
        monkeypatch.setattr(FFmpegManager, "_reaper_loop", lambda self: None)
        mgr = FFmpegManager()
        yield mgr
        mgr.shutdown(reason="test")

    def test_command_includes_hls_by_default(self, monkeypatch, tmp_path):
        gen = self._manager(monkeypatch)
        mgr = next(gen)
        cmd = mgr._build_ffmpeg_cmd(
            "x" * 40,
            {"playback_url": "http://127.0.0.1/x", "is_live": True},
            str(tmp_path),
        )
        assert "-f" in cmd and "hls" in cmd
        assert cmd.count("-map") == 2  # one for HLS, one for MPEG-TS
        assert "pipe:1" in cmd

    def test_command_omits_hls_when_lazy(self, monkeypatch, tmp_path):
        gen = self._manager(monkeypatch)
        mgr = next(gen)
        cmd = mgr._build_ffmpeg_cmd(
            "x" * 40,
            {"playback_url": "http://127.0.0.1/x", "is_live": True},
            str(tmp_path),
            include_hls=False,
        )
        assert "-f" in cmd and "hls" not in cmd
        assert cmd.count("-map") == 1  # MPEG-TS only
        assert "pipe:1" in cmd

    def test_command_applies_stat_tuning(self, monkeypatch, tmp_path):
        gen = self._manager(monkeypatch)
        mgr = next(gen)
        monkeypatch.setattr(fm, "FFMPEG_STAT_TUNING", True)
        cmd = mgr._build_ffmpeg_cmd(
            "x" * 40,
            {"playback_url": "http://127.0.0.1/x", "is_live": True,
             "stat_hints": {"current_bitrate": 10_000_000, "player_buffer_time": 3}},
            str(tmp_path),
        )
        rtbufsize = cmd[cmd.index("-rtbufsize") + 1]
        # high bitrate must grow the buffer past the 5M default
        assert _parse_ffmpeg_size(rtbufsize) > _parse_ffmpeg_size("5M")


# ---------------------------------------------------------------------------
# M3 - adaptive queue size
# ---------------------------------------------------------------------------
class TestAdaptiveQueue:
    def _session(self, is_live):
        class _FakeProc:
            pid = 12345

            def poll(self):
                return None

        return _StreamSession(process=_FakeProc(), output_dir="/tmp/x",
                              is_live=is_live)

    def test_live_uses_live_queue_max(self, monkeypatch):
        monkeypatch.setattr(fm, "QUEUE_MAX_LIVE", 64)
        monkeypatch.setattr(fm, "QUEUE_MAX_VOD", 512)
        s = self._session(is_live=True)
        assert s.queue_max == 64

    def test_vod_uses_vod_queue_max(self, monkeypatch):
        monkeypatch.setattr(fm, "QUEUE_MAX_LIVE", 64)
        monkeypatch.setattr(fm, "QUEUE_MAX_VOD", 512)
        s = self._session(is_live=False)
        assert s.queue_max == 512

    def test_subscribe_uses_session_queue_max(self, monkeypatch):
        monkeypatch.setattr(fm, "QUEUE_MAX_LIVE", 8)
        s = self._session(is_live=True)
        q = s.subscribe()
        assert q is not None
        assert q.maxsize == 8
        s.close_subscribers()


# ---------------------------------------------------------------------------
# M5 - error sentinel vs clean sentinel in iterate_mpegts
# ---------------------------------------------------------------------------
class TestIterateSentinels:
    def test_data_then_clean_close(self, monkeypatch):
        monkeypatch.setattr(fm, "ITERATE_TIMEOUT_S", 1)
        mgr = FFmpegManager.__new__(FFmpegManager)  # bypass __init__/reaper
        q = queue.Queue()
        q.put(b"chunk1")
        q.put(fm._SENTINEL)
        out = list(mgr.iterate_mpegts(q))
        assert out == [b"chunk1"]

    def test_error_sentinel_breaks_without_yielding(self, monkeypatch):
        monkeypatch.setattr(fm, "ITERATE_TIMEOUT_S", 1)
        mgr = FFmpegManager.__new__(FFmpegManager)
        q = queue.Queue()
        q.put(b"chunk1")
        q.put(fm._ERROR_SENTINEL)
        q.put(b"never")
        out = list(mgr.iterate_mpegts(q))
        # error sentinel stops iteration; "never" is never consumed
        assert out == [b"chunk1"]

    def test_broadcast_error_signals_all_subscribers(self, monkeypatch):
        monkeypatch.setattr(fm, "QUEUE_MAX_LIVE", 8)
        s = _StreamSession(process=None, output_dir="/tmp/x", is_live=True)
        q1 = s.subscribe()
        q2 = s.subscribe()
        s.broadcast_error("deadbeef")
        assert q1.get_nowait() is fm._ERROR_SENTINEL
        assert q2.get_nowait() is fm._ERROR_SENTINEL
        s.close_subscribers()


class TestRestartRecovery:
    class _FakeProc:
        def __init__(self, alive=True, output_dir="/tmp/new"):
            self.pid = 12345
            self.returncode = None if alive else 1
            self.stdout = None
            self.stderr = None
            self._openace_output_dir = output_dir

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    def _manager_for_restart(self, monkeypatch, tmp_path):
        mgr = FFmpegManager.__new__(FFmpegManager)
        mgr._streams = {}
        mgr._lock = threading.Lock()
        mgr._closed = False
        mgr._session_cache = {}
        mgr._session_cache_ttl = 30
        mgr._engine_url = "http://127.0.0.1:6878"
        monkeypatch.setattr(mgr, "_start_stdout_drain", lambda *args, **kwargs: None)
        monkeypatch.setattr(mgr, "_start_stderr_drain", lambda *args, **kwargs: None)
        monkeypatch.setattr(mgr, "_process_survived_start_probe", lambda *args, **kwargs: True)
        return mgr

    def test_restart_reset_after_long_uptime(self, monkeypatch, tmp_path):
        cid = "a" * 40
        mgr = self._manager_for_restart(monkeypatch, tmp_path)
        old_proc = self._FakeProc(alive=False, output_dir=str(tmp_path / "old"))
        session = _StreamSession(
            process=old_proc,
            output_dir=str(tmp_path / "old"),
            command_url="http://engine/cmd",
            playback_url="http://engine/play",
            is_live=True,
            stat_url="http://engine/stat",
        )
        session.hls_requested = True
        session.restart_attempts = 2
        session.process_started_at = 0
        session.process_start_bytes_total = 0
        session.bytes_total = 188
        mgr._streams[cid] = session
        mgr._session_cache[cid] = {
            "data": {
                "playback_url": "http://engine/play",
                "command_url": "http://engine/cmd",
                "stat_url": "http://engine/stat",
                "is_live": True,
            },
            "ts": 0,
        }

        new_proc = self._FakeProc(alive=True, output_dir=str(tmp_path / "new"))
        monkeypatch.setattr(mgr, "_popen_ffmpeg", lambda *args, **kwargs: new_proc)
        monkeypatch.setattr(fm.time, "monotonic", lambda: fm.RESTART_RESET_MIN_UPTIME_S + 1)
        monkeypatch.setattr(fm.time, "sleep", lambda _s: None)

        assert mgr._restart_session(cid, session) is True
        assert session.restart_attempts == 0
        assert session.process_started_at == fm.RESTART_RESET_MIN_UPTIME_S + 1

    def test_restart_reset_only_after_min_uptime(self, monkeypatch, tmp_path):
        cid = "b" * 40
        mgr = self._manager_for_restart(monkeypatch, tmp_path)
        session = _StreamSession(
            process=self._FakeProc(alive=False, output_dir=str(tmp_path / "old")),
            output_dir=str(tmp_path / "old"),
            command_url="http://engine/cmd",
            playback_url="http://engine/play",
            is_live=True,
        )
        session.hls_requested = True
        session.restart_attempts = 2
        session.process_started_at = 10
        session.process_start_bytes_total = 0
        session.bytes_total = 188
        mgr._streams[cid] = session

        monkeypatch.setattr(mgr, "_popen_ffmpeg", lambda *args, **kwargs: self._FakeProc(alive=True))
        monkeypatch.setattr(fm.time, "monotonic", lambda: 15)
        monkeypatch.setattr(fm.time, "sleep", lambda _s: None)

        assert mgr._restart_session(cid, session) is True
        assert session.restart_attempts == 3

    def test_restart_no_reset_if_no_data_produced(self, monkeypatch, tmp_path):
        cid = "c" * 40
        mgr = self._manager_for_restart(monkeypatch, tmp_path)
        session = _StreamSession(
            process=self._FakeProc(alive=False, output_dir=str(tmp_path / "old")),
            output_dir=str(tmp_path / "old"),
            command_url="http://engine/cmd",
            playback_url="http://engine/play",
            is_live=True,
        )
        session.hls_requested = True
        session.restart_attempts = 2
        session.process_started_at = 0
        session.process_start_bytes_total = 0
        session.bytes_total = 0
        mgr._streams[cid] = session

        monkeypatch.setattr(mgr, "_popen_ffmpeg", lambda *args, **kwargs: self._FakeProc(alive=True))
        monkeypatch.setattr(fm.time, "monotonic", lambda: fm.RESTART_RESET_MIN_UPTIME_S + 1)
        monkeypatch.setattr(fm.time, "sleep", lambda _s: None)

        assert mgr._restart_session(cid, session) is True
        assert session.restart_attempts == 3

    def test_no_infinite_restart_loop_on_pathological_crash(self, monkeypatch, tmp_path):
        cid = "d" * 40
        mgr = self._manager_for_restart(monkeypatch, tmp_path)
        session = _StreamSession(
            process=self._FakeProc(alive=False, output_dir=str(tmp_path / "old")),
            output_dir=str(tmp_path / "old"),
            command_url="http://engine/cmd",
            playback_url="http://engine/play",
            is_live=True,
        )
        session.hls_requested = True
        mgr._streams[cid] = session
        now = {"value": 100.0}

        def fake_popen(*args, **kwargs):
            return self._FakeProc(alive=True, output_dir=str(tmp_path / f"new-{session.restart_attempts}"))

        monkeypatch.setattr(fm, "MAX_RESTART_ATTEMPTS", 3)
        monkeypatch.setattr(mgr, "_popen_ffmpeg", fake_popen)
        monkeypatch.setattr(fm.time, "monotonic", lambda: now["value"])
        monkeypatch.setattr(fm.time, "sleep", lambda _s: None)

        for expected_attempts in (1, 2, 3):
            session.process.returncode = 1
            session.process_started_at = now["value"] - 4
            session.process_start_bytes_total = session.bytes_total
            session.bytes_total += 188
            assert mgr._restart_session(cid, session) is True
            assert session.restart_attempts == expected_attempts
            now["value"] += 10

        assert mgr._restart_session(cid, session) is False
        assert session.restart_attempts == 3

    def test_fast_restart_probe_rejects_crashed_process(self, monkeypatch):
        mgr = FFmpegManager.__new__(FFmpegManager)
        monkeypatch.setattr(fm, "FFMPEG_START_PROBE_S", 0.01)
        crashed = self._FakeProc(alive=False)
        assert mgr._process_survived_start_probe(crashed, "a" * 40, 1) is False

    def test_start_wait_timeout_covers_negotiate_worst_case(self):
        assert fm.START_WAIT_TIMEOUT_S >= fm.READY_TIMEOUT_S + fm._session_open_worst_case_s()


class TestSpawnCleanup:
    def test_spawn_cleans_up_process_on_drain_failure(self, monkeypatch, tmp_path):
        mgr = FFmpegManager.__new__(FFmpegManager)
        out_dir = tmp_path / "out"
        terminated = []

        def fake_popen(content_id, session_info, output_dir=None, include_hls=True):
            out_dir.mkdir(parents=True, exist_ok=True)
            return TestRestartRecovery._FakeProc(alive=True, output_dir=str(out_dir))

        def fail_stdout(*args, **kwargs):
            raise RuntimeError("drain failed")

        monkeypatch.setattr(mgr, "_new_output_dir", lambda content_id: str(out_dir))
        monkeypatch.setattr(mgr, "_popen_ffmpeg", fake_popen)
        monkeypatch.setattr(mgr, "_start_stdout_drain", fail_stdout)
        monkeypatch.setattr(mgr, "_start_stderr_drain", lambda *args, **kwargs: None)
        monkeypatch.setattr(mgr, "_terminate_process", lambda process, content_id, reason: terminated.append((process, reason)))

        try:
            mgr._spawn("a" * 40, {"playback_url": "http://engine/play", "command_url": "http://engine/cmd"})
            assert False, "_spawn should propagate drain startup failures"
        except RuntimeError:
            pass

        assert terminated and terminated[0][1] == "spawn_drain_failed"
        assert not out_dir.exists()

    def test_direct_getstream_spawn_must_survive_probe(self, monkeypatch, tmp_path):
        cid = "a" * 40
        mgr = FFmpegManager.__new__(FFmpegManager)
        mgr._streams = {}
        mgr._starting = {}
        mgr._spawn_reservations = 0
        mgr._lock = threading.Lock()
        mgr._closed = False
        mgr._session_cache = {}
        mgr._engine_url = "http://127.0.0.1:6878"
        out_dir = tmp_path / "direct"
        out_dir.mkdir()

        class Proc:
            returncode = 1

            def poll(self):
                return self.returncode

        session_info = {
            "playback_url": "http://engine/ace/getstream?id=" + cid,
            "command_url": None,
            "stat_url": None,
            "is_live": True,
            "direct_getstream": True,
        }

        def fake_spawn(content_id, info):
            assert content_id == cid
            assert info is session_info
            return _StreamSession(Proc(), str(out_dir), playback_url=info["playback_url"])

        monkeypatch.setattr(fm, "negotiate_stream", lambda *args, **kwargs: session_info)
        monkeypatch.setattr(mgr, "_spawn", fake_spawn)
        monkeypatch.setattr(mgr, "_direct_getstream_produced_data", lambda *args, **kwargs: False)
        monkeypatch.setattr(mgr, "_terminate_process", lambda *args, **kwargs: None)

        assert mgr.ensure_stream(cid) is None
        assert cid not in mgr._streams
        assert not out_dir.exists()


class TestCloseSubscribers:
    def test_close_subscribers_idempotent(self):
        session = _StreamSession(process=None, output_dir="/tmp/x", is_live=True)
        q = session.subscribe()

        session.close_subscribers()
        session.close_subscribers()

        assert q.get_nowait() is fm._SENTINEL
        try:
            q.get_nowait()
            assert False, "second close should not enqueue another sentinel"
        except queue.Empty:
            pass


class TestQueueLogging:
    def test_queue_logs_session_queue_max(self, monkeypatch):
        events = []
        monkeypatch.setattr(fm, "log_event", lambda level, event, component, **payload: events.append((event, payload)))
        session = _StreamSession(process=None, output_dir="/tmp/x", is_live=True)
        session.queue_max = 1
        q = session.subscribe()
        q.put_nowait(b"old")

        session.broadcast(b"new", "a" * 40)

        trimmed = [payload for event, payload in events if event == "ffmpeg_mpegts_queue_trimmed"]
        assert trimmed
        assert trimmed[-1]["queue_max"] == 1
