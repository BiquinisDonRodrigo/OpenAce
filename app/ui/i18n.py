"""I18n wrapper around Flask-Babel.

Designed to work with Flask-Babel 4.x. Strings are extracted by ``pybabel`` via
``babel.cfg`` at the project root.

JavaScript strings are mirrored through :func:`get_catalog` which exposes the
current locale's catalog as a dict; ``render_page`` embeds it as
``window.I18N_CATALOG`` so that the JS ``_()`` helper can resolve keys.

If Babel isn't available for some reason (e.g., during unit tests in a slim
image), we gracefully degrade to identity translation.
"""

from __future__ import annotations

import json
import os
from typing import Iterable

# Hardcoded list of supported locales; first entry is the default.
SUPPORTED_LOCALES: list[tuple[str, str]] = [
    ("es", "Español"),
    ("en", "English"),
]
DEFAULT_LOCALE = "es"

try:
    from flask_babel import Babel, get_translations  # type: ignore
    from flask import current_app, g, request, session  # type: ignore
    _HAS_BABEL = True
except Exception:  # pragma: no cover
    _HAS_BABEL = False
    Babel = None  # type: ignore

babel_instance = None


def init_babel(app):
    """Initialize Babel on the given Flask app. No-op if Flask-Babel missing."""
    global babel_instance
    if not _HAS_BABEL:
        app.logger.warning("Flask-Babel not installed; i18n disabled")
        return

    # Translations directory: app/translations/
    trans_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "translations")
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = trans_dir
    app.config["BABEL_DEFAULT_LOCALE"] = DEFAULT_LOCALE
    app.config["BABEL_DOMAIN"] = "messages"

    babel_instance = Babel(app, locale_selector=_select_locale)


def _select_locale() -> str:
    """Pick a locale from (1) ?lang= query, (2) session, (3) cookie, (4) Accept-Language."""
    if not _HAS_BABEL:
        return DEFAULT_LOCALE

    # 1. Query string
    lang_arg = request.args.get("lang") if request else None
    if lang_arg:
        lang_arg = lang_arg.lower()[:2]
        if any(code == lang_arg for code, _ in SUPPORTED_LOCALES):
            session["lang"] = lang_arg
            return lang_arg

    # 2. Session
    sess_lang = session.get("lang") if session else None
    if sess_lang and any(code == sess_lang for code, _ in SUPPORTED_LOCALES):
        return sess_lang

    # 3. Cookie
    cookie_lang = request.cookies.get("lang") if request else None
    if cookie_lang and any(code == cookie_lang for code, _ in SUPPORTED_LOCALES):
        return cookie_lang

    # 4. Accept-Language header
    accept = request.headers.get("Accept-Language", "") if request else ""
    if accept:
        # Strip q-values, take first 2-letter code we support.
        for part in accept.split(","):
            code = part.split(";")[0].strip().lower()[:2]
            if any(c == code for c, _ in SUPPORTED_LOCALES):
                return code

    return DEFAULT_LOCALE


def get_locale() -> str:
    """Return the current locale code (e.g. ``"es"``)."""
    if not _HAS_BABEL:
        return DEFAULT_LOCALE
    try:
        from flask_babel import get_locale as _fl  # type: ignore
        return str(_fl())
    except Exception:
        return _select_locale()


def get_locales() -> list[tuple[str, str]]:
    """Return list of ``(code, name)`` for the language switcher."""
    return list(SUPPORTED_LOCALES)


def _fallback_translations() -> dict[str, str]:
    """Spanish fallback catalog used when compiled .mo files are missing.

    Kept intentionally small (only UI shell strings). Page-specific strings
    are translated directly in the route modules and should be added here when
    a string appears in a shared component.
    """
    return {
        "nav.skip_to_content": "Saltar al contenido",
        "nav.primary": "Principal",
        "nav.dashboard": "Panel",
        "nav.peers": "Peers",
        "nav.plugins": "Plugins",
        "nav.checker": "Comprobador",
        "nav.users": "Usuarios",
        "nav.eula": "Acuerdo",
        "nav.logout": "Cerrar sesión",
        "nav.theme_toggle": "Cambiar tema",
        "nav.language": "Idioma",
        "role.admin": "administrador",
        "role.user": "usuario",
        "role.viewer": "espectador",
        "common.loading": "Cargando…",
        "common.error": "Error",
        "common.retry": "Reintentar",
        "common.cancel": "Cancelar",
        "common.save": "Guardar",
        "common.delete": "Eliminar",
        "common.edit": "Editar",
        "common.close": "Cerrar",
        "common.confirm": "Confirmar",
        "common.empty": "No hay datos",
        "common.copied": "Copiado",
        "common.copy": "Copiar",
    }


_FALLBACK_ES = _fallback_translations()
_FALLBACK_EN = {
    "nav.skip_to_content": "Skip to content",
    "nav.primary": "Primary",
    "nav.dashboard": "Dashboard",
    "nav.peers": "Peers",
    "nav.plugins": "Plugins",
    "nav.checker": "Checker",
    "nav.users": "Users",
    "nav.eula": "Agreement",
    "nav.logout": "Log out",
    "nav.theme_toggle": "Toggle theme",
    "nav.language": "Language",
    "role.admin": "admin",
    "role.user": "user",
    "role.viewer": "viewer",
    "common.loading": "Loading…",
    "common.error": "Error",
    "common.retry": "Retry",
    "common.cancel": "Cancel",
    "common.save": "Save",
    "common.delete": "Delete",
    "common.edit": "Edit",
    "common.close": "Close",
    "common.confirm": "Confirm",
    "common.empty": "No data",
    "common.copied": "Copied",
    "common.copy": "Copy",
}


def get_catalog(lang: str | None = None) -> dict[str, str]:
    """Return the full message catalog for ``lang`` (current locale by default).

    Tries to load compiled Babel translations first, falls back to the
    hardcoded ES/EN catalogs above.
    """
    lang = lang or get_locale()

    if _HAS_BABEL:
        try:
            translations = get_translations()
            catalog = {}
            # Flask-Babel exposes the catalog as a dict-like object via ._catalog
            cat = getattr(translations, "_catalog", None) or getattr(
                getattr(translations, "catalog", None), "_catalog", None
            )
            if isinstance(cat, dict):
                for key, value in cat.items():
                    if isinstance(key, str) and isinstance(value, str) and value:
                        catalog[key] = value
            if catalog:
                return catalog
        except Exception:
            pass

    if lang == "en":
        return dict(_FALLBACK_EN)
    return dict(_FALLBACK_ES)


def _(key: str, **kwargs) -> str:
    """Translate a string. Uses Flask-Babel if available; else fallback catalog.

    ``key`` is the msgid (English-style key like ``"nav.dashboard"``).
    ``kwargs`` are optional ``str.format`` arguments.
    """
    lang = get_locale()

    # Try Babel first (proper gettext lookup)
    if _HAS_BABEL:
        try:
            from flask_babel import gettext as _f  # type: ignore
            msg = _f(key)
            if msg and msg != key:
                return msg.format(**kwargs) if kwargs else msg
        except Exception:
            pass

    # Fall back to in-memory catalog
    catalog = get_catalog(lang)
    msg = catalog.get(key, key)
    if kwargs:
        try:
            return msg.format(**kwargs)
        except Exception:
            return msg
    return msg


def ngettext(singular: str, plural: str, n: int) -> str:
    """Pluralize a string."""
    if not _HAS_BABEL:
        return singular if n == 1 else plural
    try:
        from flask_babel import ngettext as _nf  # type: ignore
        return _nf(singular, plural, n)
    except Exception:
        return singular if n == 1 else plural
