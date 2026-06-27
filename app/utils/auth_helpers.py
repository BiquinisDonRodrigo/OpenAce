import os
from functools import wraps

from flask import Response, g, jsonify, redirect, request

ROLE_HIERARCHY = {"admin": 3, "user": 2, "viewer": 1}


def get_json_body():
    """Parse request JSON body, returning (data, error_response).

    Unlike ``request.get_json(silent=True) or {}`` which silently swallows
    malformed JSON as an empty dict, this helper surfaces a 400 error so the
    client knows their payload was unreadable.

    Returns ``(dict, None)`` on success or ``(None, (response, status))`` on
    failure.
    """
    if not request.data:
        return {}, None
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "JSON inválido"}), 400)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "JSON debe ser un objeto"}), 400)
    return data, None

_AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "true").lower() not in ("false", "0", "no")


def current_user():
    return getattr(g, "user", None)


def auth_enabled():
    return _AUTH_ENABLED


def require_role(role):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not auth_enabled():
                return f(*args, **kwargs)
            user = current_user()
            if user is None:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "No autenticado"}), 401
                return redirect("/login")
            required_level = ROLE_HIERARCHY.get(role, 0)
            user_level = ROLE_HIERARCHY.get(user.get("role"), 0)
            if user_level < required_level:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Permisos insuficientes"}), 403
                return Response("Acceso denegado", status=403)
            return f(*args, **kwargs)
        return decorated
    return decorator
