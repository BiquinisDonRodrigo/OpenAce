# AGENTS.md

## Commands

- **Run tests**: `python3 -m pytest tests/ -v`
- **Compile check**: `python3 -m compileall -q app server.py`
- **Start dev server**: `DB_PATH=/tmp/openace_dev/data.db OPENACE_AUTO_SETUP=true OPENACE_ADMIN_USER=admin OPENACE_ADMIN_PASSWORD=dev-password OPENACE_EULA_ACCEPT=true python3 server.py`

## Architecture

Flask proxy for AceStream. Single-container (engine + FFmpeg + Python proxy).
- `app/__init__.py`: app factory, auth guards (setup/EULA/auth), CSRF, error handlers
- `app/routes/`: blueprints (auth, panel/peers, plugins_api, check, setup, eula, hls, play, playlist)
- `app/utils/`: stores (SQLite with connection pool), ffmpeg_manager, auth_store, check_store
- `app/ui/base.py`: shared HTML shell, CSS, JS (render_page, setupModal, fetchJSON)

## Auth model

- Session cookie (`openace_session`) for browser UI → requires CSRF token (double-submit cookie)
- Bearer token / query param `?token=` / Basic Auth for API clients → no CSRF needed
- Roles: admin (3) > user (2) > viewer (1)
- Rate limit: login 5 attempts/5min per IP; API writes 60 req/min per IP

## Key patterns

- `get_json_body()` from `auth_helpers` returns `(data, (response, status))` — always use instead of `request.get_json(silent=True) or {}`
- SQLite: WAL mode, pooled connections (`_POOL_MAX=10`), `check_same_thread=False`
- Error handlers return JSON for `/api/*` routes, HTML for browser routes