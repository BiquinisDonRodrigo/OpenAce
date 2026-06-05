import os

from flask import Flask, redirect, request

from app.config import Config
from app.logging_config import configure_logging
from app.utils.logging_utils import log_event
from app.routes import register_blueprints

_EULA_EXEMPT_PREFIXES = ("/eula", "/api/eula/", "/static/")


def create_app():
    configure_logging()
    app = Flask(__name__)
    app.config.from_object(Config)
    if os.environ.get('WERKZEUG_RUN_MAIN') or not app.debug:
        from app.utils.plugin_refresh import bootstrap_all
        bootstrap_all()
    register_blueprints(app)

    @app.route("/")
    def index():
       log_event("info", "health_check", "core")
       return "OpenAce is running"

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

    return app
