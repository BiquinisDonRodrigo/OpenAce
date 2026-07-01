import os
import socket
import time

from app.config import _load_or_create_secret_key
from app.logging_config import _redact_value
from app.routes import hls, panel
from app.utils import auth_store, environment_store, plugin_cache, plugin_refresh, plugin_store


def _csrf_headers(token):
    return {"X-CSRF-Token": token}


class TestLoginFlow:
    def test_login_success(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_login_wrong_password(self, client):
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_login_missing_fields(self, client):
        r = client.post("/api/auth/login", json={})
        assert r.status_code == 400

    def test_login_invalid_json(self, client):
        r = client.post("/api/auth/login", data="not json", content_type="application/json")
        assert r.status_code == 400
        assert "JSON" in r.get_json()["error"]


class TestBugA_CsrfOnAuthExempt:
    """Bug A: CSRF should NOT block login/logout for auth-exempt paths."""

    def test_relogin_with_active_session_no_csrf(self, authed):
        client, _ = authed
        # User is logged in. Re-login should work without CSRF (auth-exempt).
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401  # not 403 CSRF

    def test_logout_no_csrf_with_active_session(self, authed):
        client, _ = authed
        r = client.post("/api/auth/logout")
        assert r.status_code == 200  # not 403 CSRF

    def test_csrf_still_protects_admin_endpoints(self, authed):
        client, _ = authed
        r = client.post("/api/admin/tokens", json={"user_id": 1})
        assert r.status_code == 403  # CSRF required

    def test_csrf_allows_admin_with_token(self, authed):
        client, token = authed
        r = client.post("/api/admin/tokens", json={"user_id": 1, "description": "t"},
                        headers=_csrf_headers(token))
        assert r.status_code == 201


class TestBugB_ApiErrorHandlers:
    """Bug B: Unknown API routes should return JSON, not HTML."""

    def test_unknown_api_route_returns_json(self, authed):
        client, _ = authed
        r = client.get("/api/nonexistent")
        assert r.status_code == 404
        assert r.content_type == "application/json"
        assert "error" in r.get_json()

    def test_unknown_html_route_returns_html(self, client):
        r = client.get("/nonexistent-page")
        assert r.status_code in (404, 302)


class TestBugE_GeoIpValidation:
    """Bug E: GeoIP must validate IP input."""

    def test_invalid_ip_returns_400(self, authed):
        client, token = authed
        r = client.get("/api/engine/geoip/not-an-ip")
        assert r.status_code == 400
        assert "inv" in r.get_json()["error"].lower()


class TestBugF_SessionInvalidation:
    """Bug F: Login should invalidate previous session."""

    def test_login_invalidates_old_session(self, client):
        r1 = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        assert r1.status_code == 200
        old_cookie = r1.headers.get("Set-Cookie", "")
        # Login again
        r2 = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        assert r2.status_code == 200
        new_cookie = r2.headers.get("Set-Cookie", "")
        # Cookies should be different (different session IDs)
        assert old_cookie != new_cookie


class TestIssueI_InvalidJson:
    """Issue I: Malformed JSON should return 400, not silent {}."""

    def test_plugins_post_invalid_json(self, authed):
        client, token = authed
        r = client.post("/api/plugins", data="not json", content_type="application/json",
                        headers=_csrf_headers(token))
        assert r.status_code == 400

    def test_check_single_invalid_json(self, authed):
        client, token = authed
        r = client.post("/check/single", data="not json", content_type="application/json",
                        headers=_csrf_headers(token))
        assert r.status_code == 400


class TestIssueG_RateLimiting:
    """Issue G: API write endpoints should be rate limited."""

    def test_rate_limit_api_writes(self, authed):
        client, token = authed
        # The limit is 60 req/min. Sending 65 should trigger 429.
        codes = []
        for _ in range(65):
            r = client.post("/api/plugins", json={"display_name": "Rate Test"},
                            headers=_csrf_headers(token))
            codes.append(r.status_code)
            if r.status_code == 429:
                break
        assert 429 in codes


class TestIssueH_Pagination:
    """Issue H: /api/plugins should support pagination."""

    def test_pagination_params(self, authed):
        client, _ = authed
        r = client.get("/api/plugins?page=1&per_page=10")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)
        assert "items" in data
        assert "total" in data
        assert "pages" in data

    def test_no_pagination_returns_array(self, authed):
        client, _ = authed
        r = client.get("/api/plugins")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)


class TestPluginValidation:
    def test_create_plugin_no_name(self, authed):
        client, token = authed
        r = client.post("/api/plugins", json={}, headers=_csrf_headers(token))
        assert r.status_code == 400

    def test_create_plugin_bad_url(self, authed):
        client, token = authed
        r = client.post("/api/plugins", json={"display_name": "T", "source_url": "ftp://x"},
                        headers=_csrf_headers(token))
        assert r.status_code == 400

    def test_create_plugin_valid(self, authed):
        client, token = authed
        r = client.post("/api/plugins", json={"display_name": "Test P", "source_url": "http://x/m.m3u"},
                        headers=_csrf_headers(token))
        assert r.status_code == 201


class TestTokenAuth:
    def test_bearer_token_no_csrf_needed(self, app, authed):
        c, token = authed
        # Create a token
        r = c.post("/api/admin/tokens", json={"user_id": 1, "description": "api"},
                   headers=_csrf_headers(token))
        assert r.status_code == 201
        token_val = r.get_json()["token"]

        # Fresh client with no session, using Bearer token
        c2 = app.test_client()
        r2 = c2.post("/api/plugins", json={"display_name": "Via Token", "source_url": "http://x/m.m3u"},
                     headers={"Authorization": f"Bearer {token_val}"})
        assert r2.status_code == 201


class TestCriticalAuditFixes:
    def test_ssrf_blocks_unspecified_ipv4_and_ipv6(self):
        plugin_refresh._ssrf_cache.clear()
        assert plugin_refresh._is_safe_source_url("http://0.0.0.0:6379/x") is False
        assert plugin_refresh._is_safe_source_url("http://[::]:6379/x") is False

    def test_pinned_dns_uses_validated_addrinfo(self, monkeypatch):
        addrinfos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
        seen = []

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_get(url, **kwargs):
            seen.append(socket.getaddrinfo("example.test", 80)[0][4][0])
            return DummyResponse()

        monkeypatch.setattr(plugin_refresh.session, "get", fake_get)
        with plugin_refresh._get_with_pinned_dns("http://example.test/feed.m3u", addrinfos):
            pass

        assert seen == ["93.184.216.34"]

    def test_eula_rejects_string_false(self, client):
        r = client.post("/api/eula/accept", json={"accepted": "false"})
        assert r.status_code == 400

    def test_update_user_enabled_string_false_disables(self):
        username = f"disabled-{time.time_ns()}"
        user = auth_store.create_user(username, "test-password-123", role="user")
        updated = auth_store.update_user(user["id"], {"enabled": "false"})
        assert updated["enabled"] is False

    def test_basic_auth_cache_does_not_store_plain_password(self):
        auth_store._basic_auth_cache.clear()
        username = f"basic-cache-{time.time_ns()}"
        password = "test-password-123"
        auth_store.create_user(username, password, role="user")
        assert auth_store.verify_password_cached(username, password) is not None
        assert auth_store._basic_auth_cache
        assert all(key[1] != password for key in auth_store._basic_auth_cache)

    def test_login_rate_limit_check_and_record_is_atomic(self):
        ip = "203.0.113.10"
        auth_store.clear_login_attempts(ip)
        assert [auth_store.check_and_record_login_attempt(ip) for _ in range(5)] == [True] * 5
        assert auth_store.check_and_record_login_attempt(ip) is False


class TestMediumAuditFixes:
    def test_renew_session_preserves_created_at(self):
        user = auth_store.create_user(f"renew-{time.time_ns()}", "test-password-123")
        sid = auth_store.create_session(user["id"], "127.0.0.1", duration_hours=1)
        before = auth_store.get_session(sid)["created_at"]
        auth_store.renew_session(sid, duration_hours=2)
        after = auth_store.get_session(sid)
        assert after["created_at"] == before

    def test_invalid_session_duration_falls_back(self, client, monkeypatch):
        monkeypatch.setenv("SESSION_DURATION_HOURS", "bad")
        r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        assert r.status_code == 200

    def test_update_user_rejects_bad_username(self, authed):
        client, token = authed
        r = client.put("/api/admin/users/1", json={"username": "x!"}, headers=_csrf_headers(token))
        assert r.status_code == 400

    def test_admin_cannot_disable_self(self, authed):
        client, token = authed
        r = client.put("/api/admin/users/1", json={"enabled": False}, headers=_csrf_headers(token))
        assert r.status_code == 400

    def test_hls_manifest_cold_start_returns_retry_after(self, authed, tmp_path):
        client, _ = authed
        environment_store.update_settings({"OPENACE_FFMPEG_ENABLED": "true"})

        class Manager:
            def ensure_stream(self, content_id):
                return str(tmp_path)

            def is_alive(self, content_id):
                return True

        try:
            hls.set_manager(Manager())
            r = client.get(f"/play/hls/{'a' * 40}?hls_client={'b' * 32}")
            assert r.status_code == 503
            assert r.headers["Retry-After"] == "1"
        finally:
            hls.set_manager(None)

    def test_hls_returns_503_when_ffmpeg_disabled(self, authed):
        client, _ = authed
        r = client.get(f"/play/hls/{'a' * 40}?hls_client={'b' * 32}")
        assert r.status_code == 503
        assert r.get_data(as_text=True) == "FFmpeg disabled"
        segment = client.get(f"/play/hls/{'a' * 40}/seg000.ts")
        assert segment.status_code == 503
        assert segment.get_data(as_text=True) == "FFmpeg disabled"

    def test_hls_segment_returns_503_when_ffmpeg_dead(self, authed, tmp_path):
        client, _ = authed
        environment_store.update_settings({"OPENACE_FFMPEG_ENABLED": "true"})
        (tmp_path / "seg000.ts").write_bytes(b"stale")

        class Manager:
            def __init__(self):
                self.dropped = 0
                self.touched = 0

            def output_dir(self, content_id):
                return str(tmp_path)

            def is_alive(self, content_id):
                return False

            def drop(self, content_id):
                self.dropped += 1

            def touch(self, content_id):
                self.touched += 1

        manager = Manager()
        try:
            hls.set_manager(manager)
            r = client.get(f"/play/hls/{'a' * 40}/seg000.ts?hls_client={'b' * 32}")
            assert r.status_code == 503
            assert r.get_data(as_text=True) == "Stream stale, retry"
            assert manager.dropped == 1
            assert manager.touched == 0
        finally:
            hls.set_manager(None)

    def test_hls_segment_returns_503_when_segment_stale(self, authed, tmp_path):
        client, _ = authed
        environment_store.update_settings({"OPENACE_FFMPEG_ENABLED": "true"})
        segment = tmp_path / "seg000.ts"
        segment.write_bytes(b"stale")
        old = time.time() - 120
        os.utime(segment, (old, old))

        class Manager:
            def __init__(self):
                self.dropped = 0
                self.touched = 0

            def output_dir(self, content_id):
                return str(tmp_path)

            def is_alive(self, content_id):
                return True

            def drop(self, content_id):
                self.dropped += 1

            def touch(self, content_id):
                self.touched += 1

        manager = Manager()
        try:
            hls.set_manager(manager)
            r = client.get(f"/play/hls/{'a' * 40}/seg000.ts?hls_client={'b' * 32}")
            assert r.status_code == 503
            assert r.get_data(as_text=True) == "Stream stale, retry"
            assert manager.dropped == 1
            assert manager.touched == 0
        finally:
            hls.set_manager(None)

    def test_missing_segment_returns_503_when_alive(self, authed, tmp_path):
        client, _ = authed
        environment_store.update_settings({"OPENACE_FFMPEG_ENABLED": "true"})

        class Manager:
            def output_dir(self, content_id):
                return str(tmp_path)

            def is_alive(self, content_id):
                return True

        try:
            hls.set_manager(Manager())
            r = client.get(f"/play/hls/{'a' * 40}/seg000.ts?hls_client={'b' * 32}")
            assert r.status_code == 503
            assert r.headers["Retry-After"] == "1"
            assert r.get_data(as_text=True) == "Segment not ready, retry"
        finally:
            hls.set_manager(None)

    def test_missing_segment_returns_404_when_dead(self, authed, tmp_path):
        client, _ = authed
        environment_store.update_settings({"OPENACE_FFMPEG_ENABLED": "true"})

        class Manager:
            def output_dir(self, content_id):
                return str(tmp_path)

            def is_alive(self, content_id):
                return False

        try:
            hls.set_manager(Manager())
            r = client.get(f"/play/hls/{'a' * 40}/seg000.ts?hls_client={'b' * 32}")
            assert r.status_code == 404
        finally:
            hls.set_manager(None)

    def test_segments_stale_uses_cache(self, monkeypatch, tmp_path):
        cid = "e" * 40
        hls.clear_stale_log(cid)
        segment = tmp_path / "seg000.ts"
        segment.write_bytes(b"segment")
        calls = []
        real_listdir = hls.os.listdir

        def listdir_spy(path):
            calls.append(path)
            return real_listdir(path)

        monkeypatch.setattr(hls.os, "listdir", listdir_spy)

        first = hls._segments_stale(str(tmp_path), cid)
        second = hls._segments_stale(str(tmp_path), cid)

        assert first == second
        assert len(calls) == 1
        hls.clear_stale_log(cid)

    def test_log_redaction_preserves_dict_shape(self):
        redacted = _redact_value({"event": "x", "url": "/play?token=abc", "nested": {"password": "pw"}})
        assert redacted["event"] == "x"
        assert redacted["url"] == "/play?token=[REDACTED]"
        assert redacted["nested"]["password"] == "[REDACTED]"

    def test_empty_secret_file_is_persisted(self, tmp_path, monkeypatch):
        secret_file = tmp_path / "secret"
        secret_file.write_text("")
        monkeypatch.delenv("OPENACE_SECRET_KEY", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("OPENACE_SECRET_FILE", str(secret_file))
        value = _load_or_create_secret_key()
        assert value
        assert secret_file.read_text().strip() == value

    def test_plugin_json_import_filters_invalid_channels(self, authed):
        client, token = authed
        payload = {
            "name": "json-filter",
            "display_name": "Json Filter",
            "source_url": "http://example.com/list.m3u",
            "channels": [{"name": "bad"}, "bad", {"name": "ok", "infohash": "a" * 40}],
        }
        r = client.post("/api/plugins/import", json=payload, headers=_csrf_headers(token))
        assert r.status_code == 200
        assert r.get_json()["imported"][0]["channels"] == 1


class TestEnvironmentModule:
    def test_environment_page_requires_admin(self, client):
        r = client.get("/environment")
        assert r.status_code in (302, 401)

    def test_panel_contains_environment_shortcut(self, authed):
        client, _ = authed
        r = client.get("/panel")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert 'href="/environment"' in html
        assert 'id="card-environment"' in html

    def test_environment_page_renders_table_headers(self, authed):
        client, _ = authed
        r = client.get("/environment")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Nombre del parametro" in html
        assert "Valor del parametro" in html
        assert "Descripcion" in html

    def test_environment_api_lists_settings(self, authed):
        client, _ = authed
        r = client.get("/api/environment")
        assert r.status_code == 200
        items = r.get_json()["items"]
        keys = {item["key"] for item in items}
        assert "ACESTREAM_PORT" in keys
        assert "OPENACE_FFMPEG_ENABLED" in keys
        assert "ACESTREAM_IP" not in keys
        assert "WG_PRIVATE_KEY" not in keys
        assert "ProtonCountries" not in keys
        assert "TZ" not in keys
        ffmpeg_enabled = next(item for item in items if item["key"] == "OPENACE_FFMPEG_ENABLED")
        assert ffmpeg_enabled["value"] == "false"
        groups = {item["group"] for item in items}
        assert "OpenAce" in groups
        assert "FFmpeg" in groups
        assert "HLS" in groups

    def test_environment_update_validates_and_persists(self, authed):
        client, token = authed
        bad = client.put(
            "/api/environment",
            json={"values": {"ACESTREAM_PORT": "bad"}},
            headers=_csrf_headers(token),
        )
        assert bad.status_code == 400

        ok = client.put(
            "/api/environment",
            json={"values": {"SESSION_DURATION_HOURS": "48"}},
            headers=_csrf_headers(token),
        )
        assert ok.status_code == 200
        assert environment_store.get_int("SESSION_DURATION_HOURS") == 48

    def test_environment_reset_restores_default(self, authed):
        client, token = authed
        client.put(
            "/api/environment",
            json={"values": {"SESSION_DURATION_HOURS": "48"}},
            headers=_csrf_headers(token),
        )
        r = client.delete("/api/environment/SESSION_DURATION_HOURS", headers=_csrf_headers(token))
        assert r.status_code == 200
        assert environment_store.get_int("SESSION_DURATION_HOURS") == 24

    def test_playlist_uses_request_ip_or_domain(self, authed):
        client, token = authed
        plugin = plugin_store.create({"name": "playlist-host", "display_name": "Playlist Host"})
        plugin_cache.set_channels(plugin["id"], [{"name": "One", "group_title": "G", "infohash": "a" * 40}])
        client.put(
            "/api/environment",
            json={"values": {"PUBLIC_BASE_URL": "https://openace.dominio1.tld\nhttps://openace.dominio2.tld"}},
            headers=_csrf_headers(token),
        )
        playlist_token = auth_store.create_token(1, description="playlist-test")["token"]

        by_ip = client.get(f"/playlist-host/hls.m3u?token={playlist_token}", base_url="http://10.69.69.253:8888")
        assert by_ip.status_code == 200
        assert "http://10.69.69.253:8888/play/hls/" in by_ip.get_data(as_text=True)

        by_domain = client.get(f"/playlist-host/hls.m3u?token={playlist_token}", base_url="https://openace.dominio2.tld")
        assert by_domain.status_code == 200
        body = by_domain.get_data(as_text=True)
        assert "https://openace.dominio2.tld/play/hls/" in body
        assert "openace.dominio1.tld/play/hls/" not in body


class TestPeersPanel:
    class Addr:
        def __init__(self, ip, port):
            self.ip = ip
            self.port = port

    class Conn:
        def __init__(self, lip, lport, rip, rport, status="ESTABLISHED", pid=123):
            self.laddr = TestPeersPanel.Addr(lip, lport)
            self.raddr = TestPeersPanel.Addr(rip, rport)
            self.status = status
            self.pid = pid

    class Proc:
        def __init__(self, conns):
            self.info = {
                "pid": 123,
                "name": "start-engine",
                "exe": "/openace/start-engine",
                "cmdline": ["/openace/start-engine", "--client-console", "--port", "6878", "--bind", "50000"],
            }
            self._conns = conns

        def net_connections(self, kind="tcp"):
            return self._conns

    def test_os_peer_detection_filters_and_groups_public_process_connections(self, monkeypatch):
        panel._peer_cache.clear()
        panel._speed_cache.clear()
        conns = [
            self.Conn("10.2.0.2", 50000, "93.184.216.34", 40000),
            self.Conn("10.2.0.2", 51000, "93.184.216.34", 40001),
            self.Conn("127.0.0.1", 6878, "127.0.0.1", 46000),
            self.Conn("10.2.0.2", 52000, "10.0.0.7", 40002),
            self.Conn("10.2.0.2", 53000, "198.51.100.10", 40003),
            self.Conn("10.2.0.2", 54000, "1.1.1.1", 40004, status="TIME_WAIT"),
        ]
        monkeypatch.setattr(panel.psutil, "process_iter", lambda attrs: [self.Proc(conns)])
        monkeypatch.setattr(panel, "_get_socket_byte_counts", lambda: {})
        monkeypatch.setattr(panel, "_get_peer_ip_info", lambda ip: {
            "country": "US",
            "city": "Example City",
            "org": "AS15133 Example",
            "timezone": "America/New_York",
        })

        peers = panel._get_acestream_peer_connections()

        assert len(peers) == 1
        peer = peers[0]
        assert peer["ip"] == "93.184.216.34"
        assert peer["source"] == "os_process"
        assert peer["connections"] == 2
        assert peer["direction"] == "mixed"
        assert peer["remote_ports"] == [40000, 40001]
        assert peer["local_ports"] == [50000, 51000]
        assert peer["country"] == "US"
        assert peer["org"] == "AS15133 Example"

    def test_socket_rates_compute_download_and_upload_deltas(self, monkeypatch):
        panel._speed_cache.clear()
        key = ("10.2.0.2", 50000, "93.184.216.34", 40000)
        panel._speed_cache[key] = {"sent": 1000, "recv": 2000, "ts": 100.0}
        monkeypatch.setattr(panel.time, "time", lambda: 102.0)
        monkeypatch.setattr(panel, "_get_socket_byte_counts", lambda: {
            ("10.2.0.2:50000", "93.184.216.34:40000"): {"sent": 1400, "recv": 2600}
        })

        down, up = panel._socket_rates("10.2.0.2", 50000, "93.184.216.34", 40000)

        assert down == 300
        assert up == 200
