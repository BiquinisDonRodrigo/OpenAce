"""Unit tests for the FFmpeg-proxy helpers and command building.

Covers the pure functions introduced for the AceStream-driven proxy improvements:
M2 (stat tuning), M3 (adaptive queue), M4 (lazy HLS), M5 (error sentinel) and
M6 (188-byte MPEG-TS alignment).
"""
import queue

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
