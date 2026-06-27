"""EULA consent storage. Records acceptances with IP, user-agent, and a stable
hash that uniquely identifies the EULA version + acceptance modality (checkbox
vs. legacy phrase). Used by setup wizard, /eula page, and auto-setup."""

import hashlib
import threading
import time

from app.utils.check_store import _connect, _ensure_init, _lock


# Legacy phrase kept for backwards-compatibility with existing DB rows and
# the setup wizard. New acceptance flow uses the checkbox-based constant
# below so the UI matches the legal text (clause 2 of the EULA was rewritten
# to describe the checkbox ritual instead of the typed-phrase one).
EXPECTED_PHRASE = "He leído y acepto el acuerdo"

# Stable marker hashed and recorded when acceptance is via the checkbox flow.
# Using a versioned constant lets us rotate it later if the EULA changes
# substantively without breaking historical rows.
CHECKBOX_ACCEPT_MARKER = "EULA-OA-CHECKBOX-ACCEPTED-1.0"

_accepted_cache = None
_accepted_cache_lock = threading.Lock()


def _invalidate_accepted_cache():
    global _accepted_cache
    with _accepted_cache_lock:
        _accepted_cache = None


def phrase_hash(phrase: str) -> str:
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def accept(ip: str, user_agent: str, version: str, phrase: str | None = None,
           *, via_checkbox: bool = False) -> dict | None:
    """Record EULA acceptance.

    Two acceptance modes (mutually exclusive via args):

    * ``via_checkbox=True`` (recommended): records consent using the stable
      :data:`CHECKBOX_ACCEPT_MARKER` hash. This matches the checkbox UI.
    * ``phrase`` (legacy): records consent if and only if the phrase matches
      :data:`EXPECTED_PHRASE`. Returns ``None`` otherwise (kept so old
      callers don't break).

    Returns ``{"consent_id": ..., "accepted_at": ...}`` on success.
    """
    if via_checkbox:
        marker = CHECKBOX_ACCEPT_MARKER
    elif phrase == EXPECTED_PHRASE:
        marker = phrase
    else:
        return None
    _ensure_init()
    h = phrase_hash(marker)
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                """
                INSERT INTO eula_consents (ip, user_agent, eula_version, phrase_hash)
                VALUES (?, ?, ?, ?)
                RETURNING id, accepted_at
                """,
                (ip, user_agent, version, h),
            ).fetchone()
            conn.commit()
            result = {
                "consent_id": row[0],
                "accepted_at": row[1],
            }
        finally:
            conn.close()
    _invalidate_accepted_cache()
    return result


def revoke(ip: str) -> bool:
    _ensure_init()
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                UPDATE eula_consents
                SET revoked_at = ?, revoked_ip = ?
                WHERE revoked_at IS NULL
                """,
                (now, ip),
            )
            conn.commit()
            changed = cur.rowcount > 0
        finally:
            conn.close()
    if changed:
        _invalidate_accepted_cache()
    return changed


def is_globally_accepted() -> bool:
    global _accepted_cache
    if _accepted_cache is not None:
        return _accepted_cache
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM eula_consents WHERE revoked_at IS NULL LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    accepted = row is not None
    with _accepted_cache_lock:
        _accepted_cache = accepted
    return accepted


def status() -> dict:
    """Return global EULA consent status. Consent is global (any accepted
    consent unlocks the app for all clients); the historical per-IP parameter
    has been removed to match the actual enforcement in before_request.
    """
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id, accepted_at, eula_version, ip
            FROM eula_consents
            WHERE revoked_at IS NULL
            ORDER BY accepted_at DESC
            LIMIT 1
            """,
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            "accepted": True,
            "consent_id": row[0],
            "accepted_at": row[1],
            "version": row[2],
            "accepted_from": row[3],
        }
    return {"accepted": False}
