"""Tests for the /play/mpegts proxy route: Content-Type casing (M1), HEAD and
Range handling (M7), and the 503 paths."""
import queue

from app.routes import play
from app.utils.ffmpeg_manager import _SENTINEL


VALID_ID = "a" * 40


class _FakeSession:
    pass


class _FakeManager:
    """Minimal stand-in for FFmpegManager used by the play route."""

    def __init__(self, *, available=True, chunks=()):
        self.available = available
        self.chunks = list(chunks)
        self.subscribed = 0
        self.unsubscribed = 0
        self.last_session = None
        self.last_q = None

    def subscribe_mpegts(self, content_id):
        self.subscribed += 1
        if not self.available:
            return None, None
        q = queue.Queue()
        for c in self.chunks:
            q.put(c)
        q.put(_SENTINEL)
        sess = _FakeSession()
        self.last_session = sess
        self.last_q = q
        return sess, q

    def iterate_mpegts(self, q, component=None, log_context=None):
        while True:
            item = q.get_nowait()
            if item is _SENTINEL:
                break
            yield item

    def unsubscribe_mpegts(self, session, q):
        self.unsubscribed += 1


import pytest


@pytest.fixture
def fake_manager(monkeypatch):
    mgr = _FakeManager(chunks=[b"TS-DATA"])
    play.set_manager(mgr)
    yield mgr
    play.set_manager(None)


class TestContentIdValidation:
    def test_invalid_id_returns_400(self, authed):
        client, _ = authed
        r = client.get("/play/mpegts/not-hex")
        assert r.status_code == 400


class TestContentTypeM1:
    def test_get_returns_canonical_mp2t(self, authed, fake_manager):
        client, _ = authed
        r = client.get(f"/play/mpegts/{VALID_ID}")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "video/MP2T"
        # fully consume the streaming body so the generator's finally (unsubscribe)
        # runs while the fake manager is still installed.
        assert b"TS-DATA" in r.data
        assert fake_manager.subscribed == 1
        assert fake_manager.unsubscribed == 1

    def test_unavailable_returns_503(self, authed, fake_manager):
        client, _ = authed
        fake_manager.available = False
        r = client.get(f"/play/mpegts/{VALID_ID}")
        assert r.status_code == 503


class TestHeadAndRangeM7:
    def test_head_does_not_subscribe(self, authed, fake_manager):
        client, _ = authed
        r = client.head(f"/play/mpegts/{VALID_ID}")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "video/MP2T"
        # a HEAD must not open a stream subscription
        assert fake_manager.subscribed == 0
        # HEAD response has no body
        assert r.data == b""

    def test_range_header_is_ignored_not_206(self, authed, fake_manager):
        client, _ = authed
        r = client.get(f"/play/mpegts/{VALID_ID}", headers={"Range": "bytes=0-1023"})
        # live TS is not byte-seekable: we serve 200, never 206
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "video/MP2T"
        assert b"TS-DATA" in r.data  # consume the streaming body
        assert fake_manager.subscribed == 1


class TestStreamingHeaders:
    def test_no_cache_headers_present(self, authed, fake_manager):
        client, _ = authed
        r = client.get(f"/play/mpegts/{VALID_ID}")
        assert r.headers["Cache-Control"].startswith("no-store")
        assert r.headers["X-Accel-Buffering"] == "no"
        _ = r.data  # consume body to run the generator's cleanup
