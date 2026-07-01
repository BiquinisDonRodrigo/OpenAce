from app.utils import acestream


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


def test_negotiate_falls_back_to_direct_getstream_on_load_error(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return _FakeResponse({"error": "failed to load content"})

    monkeypatch.setattr(acestream.session, "get", fake_get)

    info = acestream.negotiate_stream(
        "http://127.0.0.1:6878",
        "a" * 40,
        request_timeout=1,
        component="test",
        log_context={"content_id": "a" * 40},
    )

    assert calls == [("http://127.0.0.1:6878/ace/getstream?id=" + "a" * 40 + "&format=json", 1)]
    assert info["playback_url"] == "http://127.0.0.1:6878/ace/getstream?id=" + "a" * 40
    assert info["stat_url"] is None
    assert info["command_url"] is None
    assert info["direct_getstream"] is True


def test_negotiate_does_not_fallback_on_transport_failure(monkeypatch):
    def fake_get(url, timeout):
        raise acestream.requests.ConnectionError("down")

    monkeypatch.setattr(acestream.session, "get", fake_get)
    monkeypatch.setattr(acestream.time, "sleep", lambda _s: None)

    info = acestream.negotiate_stream(
        "http://127.0.0.1:6878",
        "b" * 40,
        request_timeout=1,
        component="test",
        log_context={"content_id": "b" * 40},
    )

    assert info is None
