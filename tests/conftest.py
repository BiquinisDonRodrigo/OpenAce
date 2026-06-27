import os
import tempfile

os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("OPENACE_AUTO_SETUP", "true")
os.environ.setdefault("OPENACE_ADMIN_USER", "admin")
os.environ.setdefault("OPENACE_ADMIN_PASSWORD", "test-password-123")
os.environ.setdefault("OPENACE_EULA_ACCEPT", "true")

_tmp = tempfile.mkdtemp(prefix="openace_test_")
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "data.db"))

import pytest
from app import create_app
from app.utils import auth_store


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