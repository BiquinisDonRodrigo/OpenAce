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