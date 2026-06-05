import hashlib
import time

from app.utils.check_store import _connect, _ensure_init, _lock


EXPECTED_PHRASE = "He leído y acepto el acuerdo"


def phrase_hash(phrase: str) -> str:
    return hashlib.sha256(phrase.encode("utf-8")).hexdigest()


def accept(ip: str, user_agent: str, version: str, phrase: str) -> dict | None:
    if phrase != EXPECTED_PHRASE:
        return None
    _ensure_init()
    h = phrase_hash(phrase)
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO eula_consents (ip, user_agent, eula_version, phrase_hash)
                VALUES (?, ?, ?, ?)
                """,
                (ip, user_agent, version, h),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id, accepted_at FROM eula_consents WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return {
                "consent_id": row[0],
                "accepted_at": row[1],
            }
        finally:
            conn.close()


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
                WHERE id = (
                    SELECT id FROM eula_consents
                    WHERE ip = ? AND revoked_at IS NULL
                    ORDER BY accepted_at DESC
                    LIMIT 1
                )
                """,
                (now, ip, ip),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def status(ip: str) -> dict:
    _ensure_init()
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id, accepted_at, eula_version
            FROM eula_consents
            WHERE ip = ? AND revoked_at IS NULL
            ORDER BY accepted_at DESC
            LIMIT 1
            """,
            (ip,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            "accepted": True,
            "consent_id": row[0],
            "accepted_at": row[1],
            "version": row[2],
        }
    return {"accepted": False}
