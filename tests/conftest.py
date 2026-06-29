import os

os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("OPENACE_AUTO_SETUP", "true")
os.environ.setdefault("OPENACE_ADMIN_USER", "admin")
os.environ.setdefault("OPENACE_ADMIN_PASSWORD", "test-password-123")
os.environ.setdefault("OPENACE_EULA_ACCEPT", "true")

os.environ.setdefault("DB_PATH", "/tmp/openace_test_initial.db")

import pytest
from app import create_app
from app.utils import auth_store, check_store, environment_store, eula_store, plugin_cache, setup_store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db_path = os.path.join(tmp_path, "data.db")
    os.environ["DB_PATH"] = db_path
    with check_store._pool_lock:
        for conn in check_store._pool:
            try:
                conn.close()
            except Exception:
                pass
        check_store._pool.clear()
    check_store.DB_PATH = db_path
    check_store._initialised = False
    environment_store._initialized = False
    environment_store._cache = None
    environment_store._cache_ts = 0.0
    auth_store._auth_initialized = False
    auth_store._login_attempts.clear()
    auth_store._api_write_attempts.clear()
    auth_store._basic_auth_cache.clear()
    eula_store._accepted_cache = None
    eula_store._accepted_cache_ts = 0.0
    setup_store._setup_initialized = False
    setup_store._setup_complete_cache = None
    setup_store._setup_complete_cache_ts = 0.0
    plugin_cache._channel_cache.clear()
    yield


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authed(client):
    """Client with an active session and CSRF token. Resets API rate limit."""
    auth_store._api_write_attempts.clear()
    client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    import re
    html = client.get("/admin/users").get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    token = m.group(1) if m else ""
    return client, token
