import os
import random

from flask import Flask, Response, g, jsonify, redirect, request

from app.config import Config
from app.logging_config import configure_logging
from app.utils.logging_utils import log_event
from app.routes import register_blueprints

_EULA_EXEMPT_PREFIXES = ("/eula", "/api/eula/", "/static/", "/setup", "/api/setup/")

_AUTH_EXEMPT_PATHS = frozenset({"/", "/login", "/logout", "/api/auth/login", "/api/auth/logout"})
_AUTH_EXEMPT_PREFIXES = ("/eula", "/api/eula/", "/static/", "/setup", "/api/setup/")

_ROLE_HIERARCHY = {"admin": 3, "user": 2, "viewer": 1}


def _is_auth_exempt(path):
    if path in _AUTH_EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


def _get_required_role(path, method):
    if path.endswith("/mpegts.m3u") or path.endswith("/hls.m3u"):
        return "viewer"
    if path.startswith("/play/"):
        return "viewer"
    if path.startswith("/admin/") or path.startswith("/api/admin/"):
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
    from app.utils import auth_store

    session_id = request.cookies.get("openace_session")
    if session_id:
        session = auth_store.get_session(session_id)
        if session:
            user = auth_store.get_user_by_id(session["user_id"])
            if user and user["enabled"]:
                duration = int(os.environ.get("SESSION_DURATION_HOURS", "24"))
                auth_store.renew_session(session_id, duration)
                return user

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token_value = auth_header[7:].strip()
        if token_value:
            token = auth_store.get_token_by_value(token_value)
            if token:
                user = auth_store.get_user_by_id(token["user_id"])
                if user and user["enabled"]:
                    return user

    token_param = request.args.get("token")
    if token_param:
        token = auth_store.get_token_by_value(token_param)
        if token:
            user = auth_store.get_user_by_id(token["user_id"])
            if user and user["enabled"]:
                return user

    auth = request.authorization
    if auth and auth.username and auth.password:
        user = auth_store.verify_password(auth.username, auth.password)
        if user:
            return user

    return None


def _auto_setup():
    from datetime import datetime, timezone
    from app.utils import auth_store, eula_store, setup_store
    from app.utils.plugin_refresh import bootstrap_all

    admin_user = os.environ.get("OPENACE_ADMIN_USER", "admin")
    admin_pass = os.environ.get("OPENACE_ADMIN_PASSWORD")
    eula_accept = os.environ.get("OPENACE_EULA_ACCEPT", "").lower() in ("true", "1", "yes")

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
    eula_store.accept("auto-setup", "auto-setup", "1.0", eula_store.EXPECTED_PHRASE)
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

    if os.environ.get("REVERSE_PROXY", "").lower() in ("true", "1", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    if os.environ.get('WERKZEUG_RUN_MAIN') or not app.debug:
        from app.utils import setup_store

        if (os.environ.get("OPENACE_AUTO_SETUP", "").lower() in ("true", "1", "yes")
                and setup_store.is_setup_required()):
            _auto_setup()

        if not setup_store.is_setup_required():
            from app.utils.plugin_refresh import bootstrap_all
            bootstrap_all()

            from app.utils import auth_store
            generated_pw = auth_store.ensure_admin_exists()
            if generated_pw:
                admin_user = os.environ.get("OPENACE_ADMIN_USER", "admin")
                print(f"\n{'=' * 60}")
                print(f"  OPENACE — CONTRASEÑA ADMIN GENERADA")
                print(f"  Usuario:     {admin_user}")
                print(f"  Contraseña:  {generated_pw}")
                print(f"  ¡Guárdala! Solo se muestra una vez.")
                print(f"{'=' * 60}\n")

    register_blueprints(app)

    @app.route("/")
    def index():
       log_event("info", "health_check", "core")
       return "OpenAce is running"

    @app.before_request
    def _setup_guard():
        from app.utils import setup_store as ss
        if not ss.is_setup_required():
            return None
        path = request.path
        if (path.startswith("/setup") or path.startswith("/api/setup/")
                or path.startswith("/static/")):
            return None
        return redirect("/setup")

    @app.before_request
    def _eula_guard():
        path = request.path
        if any(path.startswith(p) for p in _EULA_EXEMPT_PREFIXES):
            return None
        from app.utils import eula_store
        if not eula_store.is_globally_accepted():
            from urllib.parse import quote
            safe_path = quote(request.full_path, safe='')
            return redirect(f"/eula?redirect={safe_path}")

    @app.before_request
    def _auth_guard():
        g.user = None

        if os.environ.get("AUTH_ENABLED", "true").lower() in ("false", "0", "no"):
            return None

        g.user = _try_authenticate()

        if random.random() < 0.01:
            from app.utils import auth_store
            auth_store.cleanup_expired_sessions()

        path = request.path
        if _is_auth_exempt(path):
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

    return app
