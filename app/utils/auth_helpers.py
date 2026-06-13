import os
from functools import wraps

from flask import Response, g, jsonify, redirect, request

ROLE_HIERARCHY = {"admin": 3, "user": 2, "viewer": 1}


def current_user():
    return getattr(g, "user", None)


def auth_enabled():
    return os.environ.get("AUTH_ENABLED", "true").lower() not in ("false", "0", "no")


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
