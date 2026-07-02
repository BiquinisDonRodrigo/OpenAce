import os
import random
import secrets
from urllib.parse import quote, urlparse

from flask import Flask, Response, g, jsonify, redirect, request, session
from werkzeug.exceptions import HTTPException

from app.config import Config
from app.logging_config import configure_logging
from app.utils import auth_store, environment_store, eula_store, setup_store
from app.utils.logging_utils import log_event
from app.routes import register_blueprints

# Optional Babel integration. Falls back gracefully if not installed.
try:
    from app.ui.i18n import init_babel as _init_babel
    _HAS_I18N = True
except Exception:  # pragma: no cover
    _HAS_I18N = False
    _init_babel = None

_EULA_EXEMPT_PREFIXES = ("/eula", "/api/eula/", "/static/", "/setup", "/api/setup/")
_EULA_EXEMPT_PATHS = frozenset({"/healthz"})

_AUTH_EXEMPT_PATHS = frozenset({"/", "/login", "/logout", "/api/auth/login", "/api/auth/logout", "/favicon.ico", "/healthz"})
_AUTH_EXEMPT_PREFIXES = ("/eula", "/api/eula/", "/static/")

_ROLE_HIERARCHY = {"admin": 3, "user": 2, "viewer": 1}

def _auth_enabled():
    return environment_store.get_bool("AUTH_ENABLED")


def _is_first_boot():
    """True only when no users exist yet (genuine first-run setup)."""
    return not auth_store.has_users()


def _is_auth_exempt(path):
    if path in _AUTH_EXEMPT_PATHS:
        return True
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return True
    if path.startswith("/setup") or path.startswith("/api/setup/"):
        return _is_first_boot()
    return False


def _get_required_role(path, method):
    if path.endswith("/mpegts.m3u") or path.endswith("/hls.m3u"):
        return "viewer"
    if path.startswith("/play/"):
        return "viewer"
    if path.startswith("/admin/") or path.startswith("/api/admin/"):
        return "admin"
    if path.startswith("/environment") or path.startswith("/api/environment"):
        return "admin"
    if path.startswith("/api/peers/hls/") and path.endswith("/kill"):
        return "admin"
    if path.startswith("/peers") or path == "/panel" or path.startswith("/api/peers/"):
        return "user"
    if path.startswith("/check"):
        return "user"
    if path.startswith("/plugins") or path.startswith("/api/plugins"):
        if method in ("POST", "PUT", "DELETE"):
            return "admin"
        return "user"
    return None


def _try_authenticate():
    from datetime import datetime, timezone

    def _is_valid(user):
        return user and user["enabled"] and not auth_store.is_user_expired(user)

    def _should_renew(session):
        try:
            expires_at = auth_store._parse_iso(session["expires_at"])
            created_at = auth_store._parse_iso(session["created_at"])
        except (ValueError, TypeError, KeyError):
            return True
        duration = expires_at - created_at
        remaining = expires_at - datetime.now(timezone.utc)
        return remaining < duration * 0.25

    session_id = request.cookies.get("openace_session")
    if session_id:
        session, user = auth_store.get_session_with_user(session_id)
        if session and _is_valid(user):
            if _should_renew(session):
                duration = _session_duration_hours()
                auth_store.renew_session(session_id, duration)
            g.auth_method = "session"
            return user

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token_value = auth_header[7:].strip()
        if token_value:
            token, user = auth_store.get_token_with_user(token_value)
            if _is_valid(user):
                g.auth_method = "token"
                return user

    token_param = request.args.get("token")
    if token_param:
        token, user = auth_store.get_token_with_user(token_param)
        if _is_valid(user):
            g.auth_method = "token"
            return user

    auth = request.authorization
    if auth and auth.username and auth.password:
        client_ip = _client_ip()
        if not auth_store.check_and_record_login_attempt(client_ip):
            return None
        user = auth_store.verify_password_cached(auth.username, auth.password)
        if user:
            auth_store.clear_login_attempts(client_ip)
            g.auth_method = "basic"
            return user

    return None


def _client_ip():
    return request.remote_addr or "0.0.0.0"


def _session_duration_hours():
    try:
        return environment_store.get_int("SESSION_DURATION_HOURS")
    except (TypeError, ValueError):
        return 24


def _secure_cookie():
    return request.is_secure or environment_store.get_bool("REVERSE_PROXY")


def _origin_ok():
    """CSRF defense via Origin header. Browsers always send Origin on
    state-changing requests (POST/PUT/DELETE/PATCH); it cannot be read
    cross-origin. If Origin is absent (non-browser/API clients), allow.
    Reject when Origin host does not match the request Host.
    Scheme and port are compared leniently to support TLS-terminating proxies.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    origin_host = parsed.netloc.lower()
    if not origin_host:
        return False
    expected = urlparse(request.host_url)
    origin_scheme = parsed.scheme or "http"
    expected_scheme = expected.scheme or "http"
    # Allow http origin when expected is https (TLS-terminating proxy without ProxyFix)
    scheme_ok = origin_scheme == expected_scheme or origin_scheme == "http"
    host_ok = (parsed.hostname or "").lower() == (expected.hostname or "").lower()
    origin_port = parsed.port or (443 if origin_scheme == "https" else 80)
    expected_port = expected.port or (443 if expected_scheme == "https" else 80)
    port_ok = origin_port == expected_port
    return scheme_ok and host_ok and port_ok


def _csrf_ok():
    """Validate CSRF for browser session-authenticated unsafe requests.

    API clients using Bearer tokens, query tokens or Basic Auth do not have a
    Flask session CSRF token. Browser UI requests authenticated by the
    openace_session cookie must echo the session token via X-CSRF-Token or a
    csrf_token form field.
    """
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return True
    if getattr(g, "auth_method", None) != "session":
        return True
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(expected and supplied and secrets.compare_digest(str(expected), str(supplied)))


def _auto_setup():
    from datetime import datetime, timezone
    from app.utils.plugin_refresh import bootstrap_all

    admin_user = environment_store.get_str("OPENACE_ADMIN_USER") or "admin"
    admin_pass = environment_store.get_str("OPENACE_ADMIN_PASSWORD")
    eula_accept = environment_store.get_bool("OPENACE_EULA_ACCEPT")

    if not admin_pass:
        log_event("error", "auto_setup_failed", "core",
                  reason="OPENACE_ADMIN_PASSWORD no definida")
        print("\n" + "=" * 60)
        print("  ERROR: OPENACE_AUTO_SETUP requiere OPENACE_ADMIN_PASSWORD")
        print("=" * 60 + "\n")
        return

    if not eula_accept:
        log_event("error", "auto_setup_failed", "core",
                  reason="OPENACE_EULA_ACCEPT no es true")
        print("\n" + "=" * 60)
        print("  ERROR: OPENACE_AUTO_SETUP requiere OPENACE_EULA_ACCEPT=true")
        print("=" * 60 + "\n")
        return

    now = datetime.now(timezone.utc).isoformat()
    eula_store.accept("auto-setup", "auto-setup", "1.0", via_checkbox=True)
    auth_store.create_user(admin_user, admin_pass, role="admin")
    setup_store.set_state("setup_started_at", now)
    setup_store.set_state("setup_step", "plugins")
    setup_store.set_state("setup_completed", "true")
    setup_store.set_state("setup_completed_at", now)
    bootstrap_all()

    log_event("info", "auto_setup_completed", "core", admin=admin_user)
    print("\n" + "=" * 60)
    print("  OPENACE — AUTO-SETUP COMPLETADO")
    print(f"  Usuario admin: {admin_user}")
    print("=" * 60 + "\n")


def create_app():
    configure_logging()
    app = Flask(__name__)
    app.config.from_object(Config)
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    # Ensure session is available for CSRF tokens and language preference.
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SESSION_COOKIE_SECURE", environment_store.get_bool("REVERSE_PROXY"))

    if environment_store.get_bool("REVERSE_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    if os.environ.get('WERKZEUG_RUN_MAIN') or not app.debug:
        auto_setup_ran = False
        if (environment_store.get_bool("OPENACE_AUTO_SETUP")
                and setup_store.is_setup_required()):
            _auto_setup()
            auto_setup_ran = True

        if not setup_store.is_setup_required():
            if not auto_setup_ran:
                from app.utils.plugin_refresh import bootstrap_all
                bootstrap_all()

            generated_pw = auth_store.ensure_admin_exists()
            if generated_pw:
                admin_user = environment_store.get_str("OPENACE_ADMIN_USER") or "admin"
                print(f"\n{'=' * 60}")
                print(f"  OPENACE — CONTRASEÑA ADMIN GENERADA")
                print(f"  Usuario:     {admin_user}")
                print(f"  Contraseña:  {generated_pw}")
                print(f"  ¡Guárdala! Solo se muestra una vez.")
                print(f"{'=' * 60}\n")

    # --- I18n initialization ---
    if _HAS_I18N:
        try:
            _init_babel(app)
        except Exception as exc:  # pragma: no cover
            app.logger.warning("Babel init failed: %s", exc)

    register_blueprints(app)

    # --- Global security headers (apply to ALL blueprints) ---
    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # Content-Security-Policy: allow inline (we use inline <script>/<style>
        # throughout) but otherwise lock down. 'unsafe-inline' is a TODO that
        # can be replaced by nonces once all scripts/styles migrate.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'self'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        return response

    # --- CSRF cookie helper: mirror session token to readable cookie ---
    @app.after_request
    def _csrf_cookie(response):
        try:
            from app.ui.base import csrf_token as _csrf_token
            tok = _csrf_token()
            if tok and request.cookies.get("csrf_token") != tok:
                response.set_cookie(
                    "csrf_token",
                    tok,
                    httponly=False,  # must be readable by JS
                    samesite="Lax",
                    secure=_secure_cookie(),
                )
        except Exception:
            pass
        # Persist language preference (set by ?lang=)
        try:
            from flask import session as _session
            lang = _session.get("lang")
            if lang and not request.cookies.get("lang"):
                response.set_cookie("lang", lang, samesite="Lax", secure=_secure_cookie(), max_age=31536000)
        except Exception:
            pass
        return response

    @app.route("/")
    def index():
       log_event("info", "health_check", "core")
       return "OpenAce is running"

    @app.route("/healthz")
    def healthz():
        # Deep healthcheck: Flask is up by definition here, but we also probe
        # the AceStream engine liveness and report the P2P/VPN port sync state
        # written by start.sh. HTTP 503 when the engine is unreachable so the
        # container surfaces as unhealthy (Docker's `restart` policy does not
        # react to unhealthy, so this only informs the operator).
        from app.utils.acestream_api import AceStreamAPI
        from app.utils.vpn_status import get_vpn_status
        host = app.config.get("ACESTREAM_HOST", "127.0.0.1")
        port = str(app.config.get("ACESTREAM_PORT", "6878"))
        api = AceStreamAPI(host, port)
        version = api.get_version()
        engine_up = version is not None
        vpn = get_vpn_status()
        body = {
            "status": "ok" if engine_up else "degraded",
            "engine_up": engine_up,
            "version": version.get("version") if version else None,
            **vpn,
        }
        return jsonify(body), (200 if engine_up else 503)

    @app.before_request
    def _setup_guard():
        if not setup_store.is_setup_required():
            return None
        path = request.path
        if path.startswith("/static/") or path == "/healthz":
            return None
        if path in _AUTH_EXEMPT_PATHS:
            return None
        if path.startswith("/setup") or path.startswith("/api/setup/"):
            if _is_first_boot():
                return None
            g.user = _try_authenticate()
            if g.user and g.user.get("role") == "admin":
                return None
            safe = quote(request.full_path, safe="")
            return redirect(f"/login?redirect={safe}")
        return redirect("/setup")

    @app.before_request
    def _favicon():
        if request.path == "/favicon.ico":
            return Response(status=204)

    @app.before_request
    def _eula_guard():
        path = request.path
        if path in _EULA_EXEMPT_PATHS or any(path.startswith(p) for p in _EULA_EXEMPT_PREFIXES):
            return None
        if not eula_store.is_globally_accepted():
            if request.path.startswith("/api/"):
                return jsonify({"error": "EULA no aceptado"}), 403
            safe_path = quote(request.full_path, safe='')
            return redirect(f"/eula?redirect={safe_path}")

    @app.before_request
    def _auth_guard():
        g.user = None
        g.auth_method = None

        if not _auth_enabled():
            return None

        if request.method in ("POST", "PUT", "DELETE", "PATCH") and not _origin_ok():
            log_event("warning", "csrf_origin_rejected", "core",
                      method=request.method, path=request.path,
                      origin=request.headers.get("Origin"))
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Origin no permitido"}), 403
            return Response("Origin no permitido", status=403)

        # Rate limit API write endpoints (throttle abuse even with valid tokens).
        client_ip = request.remote_addr or ""
        if (request.method in ("POST", "PUT", "DELETE", "PATCH")
                and request.path.startswith("/api/")
                and client_ip
                and not auth_store.check_api_rate_limit(client_ip)):
            log_event("warning", "api_rate_limited", "core",
                      method=request.method, path=request.path, ip=client_ip)
            return jsonify({"error": "Demasiadas peticiones. Inténtalo más tarde."}), 429

        path = request.path
        g.user = _try_authenticate()

        # CSRF only applies to session-authenticated unsafe requests.
        # Auth-exempt endpoints (login, logout, EULA, static, healthz)
        # are skipped so an active session cookie does not block re-login
        # or logout when the browser does not send a CSRF token.
        is_exempt = _is_auth_exempt(path)
        if not is_exempt and not _csrf_ok():
            log_event("warning", "csrf_token_rejected", "core",
                      method=request.method, path=request.path)
            if path.startswith("/api/") or request.is_json:
                return jsonify({"error": "CSRF token inválido"}), 403
            return Response("CSRF token inválido", status=403)

        if random.random() < 0.01:
            auth_store.cleanup_expired_sessions()

        if is_exempt:
            return None

        if g.user is None:
            if (
                path.startswith("/api/")
                or path.startswith("/play/")
                or path.endswith(".m3u")
            ):
                return jsonify({"error": "No autenticado"}), 401
            from urllib.parse import quote
            safe = quote(request.full_path, safe="")
            return redirect(f"/login?redirect={safe}")

        required = _get_required_role(path, request.method)
        if required:
            user_level = _ROLE_HIERARCHY.get(g.user.get("role"), 0)
            req_level = _ROLE_HIERARCHY.get(required, 0)
            if user_level < req_level:
                if path.startswith("/api/"):
                    return jsonify({"error": "Permisos insuficientes"}), 403
                return Response("Acceso denegado", status=403)

        return None

    # --- JSON error handlers for API routes ---
    def _is_api_request():
        return request.path.startswith("/api/") or request.is_json

    @app.errorhandler(404)
    def _not_found(err):
        if _is_api_request():
            return jsonify({"error": "No encontrado"}), 404
        return err

    @app.errorhandler(405)
    def _method_not_allowed(err):
        if _is_api_request():
            return jsonify({"error": "Método no permitido"}), 405
        return err

    @app.errorhandler(500)
    def _internal_error(err):
        log_event("error", "unhandled_exception", "core", path=request.path, error=str(err))
        if _is_api_request():
            return jsonify({"error": "Error interno del servidor"}), 500
        return Response("Error interno del servidor", status=500)

    @app.errorhandler(HTTPException)
    def _http_exception(err):
        if _is_api_request():
            return jsonify({"error": err.description or err.name}), err.code
        return err

    return app
