"""Shared UI primitives for OpenAce.

Centralizes:
- CSS base (palette, typography, components, dark/light themes).
- JS base (esc(), fetchJSON(), toast(), copyToClipboard(), focus-trap, i18n).
- render_page() helper for consistent page shell (meta, header, nav, CSRF).
- i18n wrapper around Flask-Babel usable both from Python and (via catalog
  injection) from inline JS.

All inline HTML/CSS/JS in app/routes/*.py should import from here instead of
re-declaring :root, font stacks, doctype boilerplate, security headers, etc.
"""

from app.ui.base import (
    BASE_CSS,
    BASE_JS,
    render_page,
    svg_favicon,
    csrf_input,
    csrf_token,
)
from app.ui.i18n import _, get_catalog, get_locale, get_locales, ngettext

__all__ = [
    "BASE_CSS",
    "BASE_JS",
    "render_page",
    "svg_favicon",
    "csrf_input",
    "csrf_token",
    "_",
    "ngettext",
    "get_locale",
    "get_locales",
    "get_catalog",
]
