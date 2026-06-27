import os
import secrets


def _load_or_create_secret_key():
    """Return a stable Flask secret key.

    Sessions are required for CSRF tokens and language preferences. In Docker
    installs users often start without a secret configured, so persist a random
    key beside the SQLite DB instead of silently using an ephemeral one.
    """
    configured = os.getenv("OPENACE_SECRET_KEY") or os.getenv("SECRET_KEY")
    if configured:
        return configured

    db_path = os.getenv("DB_PATH", "/openace/checkdb/data.db")
    secret_file = os.getenv(
        "OPENACE_SECRET_FILE",
        os.path.join(os.path.dirname(db_path), ".openace_secret_key"),
    )
    try:
        with open(secret_file, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass

    value = secrets.token_urlsafe(48)
    try:
        os.makedirs(os.path.dirname(secret_file) or ".", exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(secret_file, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        return value
    except FileExistsError:
        with open(secret_file, "r", encoding="utf-8") as fh:
            existing = fh.read().strip()
            if existing:
                return existing
        return value
    except OSError:
        # Last-resort fallback for read-only/local contexts. Docker/start.sh
        # creates a writable DB directory, so production should persist above.
        return value

class Config:
    SECRET_KEY = _load_or_create_secret_key()
    ACESTREAM_HOST = os.getenv("ACESTREAM_HOST", "127.0.0.1")
    ACESTREAM_PORT = os.getenv("ACESTREAM_PORT", "6878")
    ACESTREAM_ENGINE = f"http://{ACESTREAM_HOST}:{ACESTREAM_PORT}"
    ACESTREAM_IP = os.getenv("ACESTREAM_IP", "")
