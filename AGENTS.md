# AGENTS.md

## Commands

- **Run tests**: `python3 -m pytest tests/ -v`
- **Compile check**: `python3 -m compileall -q app server.py`
- **Start dev server**: `DB_PATH=/tmp/openace_dev/data.db OPENACE_AUTO_SETUP=true OPENACE_ADMIN_USER=admin OPENACE_ADMIN_PASSWORD=dev-password OPENACE_EULA_ACCEPT=true python3 server.py` (serves on `:8888`)
- **Compile i18n catalogs**: `python3 -m babel.messages.frontend compile -d app/translations -D messages` (there are no checked-in `.mo` files; without this, runtime falls back to the hardcoded catalog in `app/ui/i18n.py`)
- **Build Docker image**: `docker build -t openace .` (CI runs tests first, then builds via buildx — see `.github/workflows/docker-build.yml`)
- **No linter is configured.** The CI gate is `compileall` + `pytest` only. When adding code, mirror existing style.

## Overview

OpenAce is an **HTTP proxy** for the [AceStream](https://www.acestream.org/) P2P engine, written in **Flask** served by **Gunicorn (gevent workers)**. It ships as a single container (AceStream engine + FFmpeg + Python proxy) and optionally fronts a Gluetun VPN and a Kubo (IPFS) node for resolving IPFS/IPNS M3U sources. Features: multi-user auth, dynamic M3U plugins, MPEG-TS/HLS streaming, peer dashboard, channel checker, first-run setup wizard, EULA gate.

Stack: Flask 3, Gunicorn+gevent, SQLite (apsw/stdlib), FFmpeg (codec copy), `requests` with pooled `HTTPAdapter`, Kubo IPFS gateway, Docker Compose, GitHub Actions → GHCR.

## Architecture

```
Cliente ──HTTP──► Flask Proxy :8888 ──HTTP──► AceStream Engine :6878
 (IPTV/VLC)      (gunicorn+gevent)              │
                    │ spawn (/play/hls/*)       │ P2P
                    ▼                           ▼
                 ffmpeg ──HLS──► cliente      Red P2P
                    │
                    └──► Kubo (IPFS gateway :48080)
```

Directory map:
- `server.py` — entrypoint (`create_app()`), `app.run` for dev.
- `app/__init__.py` — app factory + global middleware (setup/EULA/auth guards, CSRF, security headers, error handlers).
- `app/config.py` — `Config` class; deterministic `SECRET_KEY` persistence.
- `app/logging_config.py` — JSON logging + secret redaction.
- `app/routes/` — 10 blueprints (auth, panel/peers, plugins_api, check, setup, eula, hls, play, playlist, environment).
- `app/utils/` — 18 modules: SQLite stores & pool, AceStream clients, FFmpeg manager, M3U/plugin engine, auth, checker, logging.
- `app/ui/` — `base.py` (HTML shell, CSS, JS), `i18n.py` (Babel wrapper). All HTML/CSS/JS is inlined in Python — no template/static JS files.
- `app/plugins/` — **empty package** (placeholder). The real plugin/M3U system lives in `app/utils/plugin_*.py` + `app/utils/m3u_parser.py` + `app/routes/plugins_api.py` + `app/routes/playlist.py`.
- `app/static/fonts/` — Manrope + JetBrains Mono woff2 (OFL). Favicon is an inline SVG data-URI.
- `app/translations/` — `es`/`en` gettext catalogs.
- `tests/` — pytest, in-memory, no network/engine.
- `start.sh` — container entrypoint (engine loop + gunicorn).
- `DOC/` — user-facing deployment docs (Spanish).

## App factory & middleware

`create_app()` (`app/__init__.py`) wires config, i18n, blueprints (`register_blueprints` in `app/routes/__init__.py`), and this **ordered** `before_request` chain:
1. `_setup_guard` — if setup incomplete, redirect browsers to `/setup` (only first-boot OR authenticated admin may access setup URLs).
2. `_favicon` — 204 for `/favicon.ico`.
3. `_eula_guard` — if EULA not globally accepted → redirect browser to `/eula`, JSON 403 for `/api/*`.
4. `_auth_guard` — Origin check, API rate limit (429), resolves `g.user`, double-submit CSRF check, probabilistic expired-session cleanup (~1% of requests), enforces RBAC via `_get_required_role`.

`after_request`: `_security_headers` (CSP, X-Frame-Options, etc.) and `_csrf_cookie` (mirrors session CSRF token to a JS-readable cookie; persists `lang`).

Other: `MAX_CONTENT_LENGTH = 50 MB`; `ProxyFix` applied when `REVERSE_PROXY` is on. Error handlers return JSON for `/api/*` (or JSON requests), HTML otherwise (404/405/500/HTTPException).

On startup: if `OPENACE_AUTO_SETUP` and setup required → headless `_auto_setup()`; else `bootstrap_all()` plugins and `ensure_admin_exists()` (prints generated admin password once).

## Routes / Blueprints

| Blueprint | Prefix | Responsibility |
|---|---|---|
| `setup_bp` | — | 4-step wizard (EULA→users→plugins→summary) + JSON setup API (`/api/setup/*`) |
| `auth_bp` | — | login/logout, `/api/auth/me`, `/admin/users`, `/api/admin/users`, `/api/admin/tokens` |
| `play_bp` | `/play` | MPEG-TS stream proxy (`/play/mpegts/<id>`) |
| `hls_bp` | `/play/hls` | HLS manifest + segment serving (FFmpeg-driven) |
| `plugins_api_bp` | — | Plugin CRUD REST (`/api/plugins/*`) + `/plugins` SPA |
| `playlist_bp` | — | M3U output (`/<plugin>/mpegts.m3u`, `/<plugin>/hls.m3u`) |
| `panel_bp` (named `peers`) | — | dashboard (`/panel`), peers (`/peers`), `/api/peers/status`, engine geoip/cleanup |
| `check_bp` | — | channel checker (`/check`, `/check/single`, `/check/start|stop|status|results`) |
| `environment_bp` | — | runtime config editor (`/environment`, `/api/environment`) |
| `eula_bp` | — | EULA accept/revoke/status |

`FFmpegManager` is built once (only if `OPENACE_FFMPEG_ENABLED` and not the Werkzeug reloader parent), stored in `app.extensions["ffmpeg_manager"]`, and injected into `hls`/`play` via `set_manager()`.

## Auth model

**4 auth methods**, resolved in `_try_authenticate` (`app/__init__.py`):
1. `openace_session` cookie → `auth_method="session"` (subject to CSRF).
2. `Authorization: Bearer <token>` → `auth_method="token"`.
3. `?token=<token>` query param → `auth_method="token"`.
4. HTTP Basic Auth → `auth_method="basic"` (rate-limited like login; cached 30s).

**Roles**: `admin (3) > user (2) > viewer (1)` (`app/utils/auth_helpers.py:ROLE_HIERARCHY`). RBAC enforced centrally in `_get_required_role` (path+method → role): `/play/*`, `*.mpegts.m3u`, `*.hls.m3u` → viewer; `/peers`, `/panel`, `/api/peers/*`, `/check`, plugin GETs → user; `/admin/*`, `/api/admin/*`, `/environment`, `/api/peers/hls/*/kill`, plugin POST/PUT/DELETE, setup reset → admin. `@require_role(role)` adds explicit per-endpoint enforcement.

**CSRF (two layers)**: `_origin_ok()` (Origin host/port/scheme match, all unsafe methods) + `_csrf_ok()` (double-submit token via `X-CSRF-Token` header or form `csrf_token`, only for session-authenticated POST/PUT/DELETE/PATCH). Token/Bearer/Basic clients are CSRF-exempt.

**Rate limits** (`app/utils/auth_store.py`): login 5 attempts / 5 min / IP; API writes 60 req / 60 s / IP on `/api/*` POST/PUT/DELETE/PATCH (429).

**Sessions**: UUID4 id cookie; auto-renewed when <25% of duration remains; probabilistic cleanup of expired rows. Duration = `SESSION_DURATION_HOURS` (default 24).

## Data layer

`app/utils/check_store.py` is the **SQLite hub**: owns `DB_PATH` (env `DB_PATH`, default `/openace/checkdb/data.db`), the connection pool (`_POOL_MAX=10`, `check_same_thread=False`), pragmas (`WAL`, `synchronous=NORMAL`, `busy_timeout=10000`, `cache_size=-8192`, `foreign_keys=ON`), the global write lock `_lock`, and the `_PooledConn` proxy whose `close()` returns the connection to the pool. **Every other store imports `_connect`, `_ensure_init`, `_lock` from here** rather than opening its own pool.

`_ensure_init` (double-checked under `_lock`) creates `channels` + indexes, `eula_consents` + indexes, and `plugins` (with idempotent `ALTER TABLE … ADD COLUMN etag/last_modified` for upgrades).

Tables & owners:
- `channels` — `check_store` (catalog + check results; `infohash` PK, status/response_ms/peers/speed).
- `eula_consents` — created in `check_store`, written by `eula_store`.
- `plugins` — created in `check_store`, written by `plugin_store`.
- `environment_settings` — `environment_store`.
- `setup_state` — `setup_store`.
- `users`, `api_tokens`, `sessions` (+ indexes) — `auth_store` (`_ensure_auth_init`).

Writes serialize on `_lock`; reads borrow pooled connections without it. WAL + `busy_timeout=10s` handle cross-connection contention.

## Stream lifecycle

**Direct MPEG-TS** (`app/routes/play.py`): when `OPENACE_FFMPEG_ENABLED` is off, `/play/mpegts/<id>` proxies `GET /ace/getstream?id=` from the engine directly (streaming iteration, `X-Accel-Buffering: no`, no `Range`/206 support). HEAD returns headers only.

**FFmpeg path** (`app/utils/ffmpeg_manager.py`, largest module): `FFmpegManager.ensure_stream(content_id)` reuses an alive session or spawns one (enforces `OPENACE_MAX_STREAMS` default 32; dedupes concurrent starts per-id via `threading.Event`). Spawns ffmpeg remuxing engine MPEG-TS → fan-out (`pipe:1` → per-client `Queue`) **and** HLS on disk. Features: bounded restarts (`OPENACE_FFMPEG_RESTARTS`), idle/process-death reaping, client back-pressure (drop-oldest, then evict slow clients), lazy HLS upgrade (`OPENACE_HLS_LAZY`), 188-byte TS packet alignment (`OPENACE_TS_ALIGN`), stat-driven tuning (`OPENACE_FFMPEG_STAT_TUNING`). On startup it SIGTERMs leftover OpenAce ffmpeg processes found via `/proc`.

**Reaper**: daemon thread `"ffmpeg-reaper"` runs every 10s: `stream_registry.reap_expired()`, then drops exited/non-restarting processes or client-less sessions idle ≥ `OPENACE_IDLE_TIMEOUT_S` (default 180s).

**`stream_registry`** (`app/utils/stream_registry.py`): in-memory active-client accounting per `(content_id, fmt)`; HLS clients expire after `HLS_CLIENT_TTL_S=30`. Powers the dashboard and liveness decisions.

**HLS routes** (`app/routes/hls.py`): `/play/hls/<id>` serves a rewritten manifest (segment URIs rewritten to include `?hls_client=<id>`; `?token=` preserved); lazy-starts HLS on first request; drops stale segments. `/play/hls/<id>/<file>` serves `.ts`/`.m3u8` via `send_from_directory` (503 if not ready, 404 if ffmpeg dead).

## Plugin / M3U system

Four conceptual layers:
1. **Persistence** — `app/utils/plugin_store.py` (SQLite CRUD over `plugins`; `_ALLOWED_FIELDS` whitelist).
2. **Cache** — `app/utils/plugin_cache.py` (in-memory channels/groups per plugin, sorted by name).
3. **Fetch/parse/schedule** — `app/utils/plugin_refresh.py` + `app/utils/m3u_parser.py`.
4. **Transport** — `app/routes/plugins_api.py` (REST + SPA) and `app/routes/playlist.py` (M3U output).

`plugin_refresh.fetch_and_cache(plugin)`: rewrites `/ipfs/` & `/ipns/` paths through `IPFS_GATEWAY`, runs an **SSRF guard** (rejects loopback/link-local/unspecified IPs; allows private IPs for LAN/Docker; **disables redirects**), and uses **pinned DNS** (`_get_with_pinned_dns` monkeypatches `socket.getaddrinfo` to prevent TOCTOU re-resolution). Sends conditional `If-None-Match`/`If-Modified-Since`, honors 304, enforces `MAX_M3U_SIZE=50MB`. Schedules periodic refreshes via daemon `threading.Timer` (`bootstrap_all` staggers starts with jitter to avoid thundering herd).

`m3u_parser.extract_infohash` pulls a 40-hex hash from bare hash / `acestream://` / `http(s)://…?id=|infohash=|content_id=`. `playlist.py` renders `#EXTM3U` with `/play/mpegts/<hash>` or `/play/hls/<hash>` entries (forwards `?token=`), using `PUBLIC_BASE_URL` when the request host matches a configured origin, else `request.host_url`.

## Channels checker

`app/routes/check.py` + `app/utils/check_runner.py` + `check_store`. `CheckRunner` (singleton `runner`, single gevent worker assumed) probes channels with bounded concurrency (`MAX_CONCURRENT_CHECKS=4`, shared `_engine_semaphore` with manual checks) via `acestream.check_stream` (`CHECK_TIMEOUT_S=10`), persists results, and exposes `snapshot()` for `/check/status` polling. Statuses: `live`, `dead`, `timeout`, `error`, `skipped`. Bulk start returns 409 if already running. `check_store.purge_stale` removes channels no longer present in any plugin.

## UI layer

`app/ui/base.py` inlines all CSS/JS in Python strings (no template files):
- `render_page(title, body, *, extra_css, extra_js, active_nav, ...)` — full HTML shell with metas, inline SVG favicon, sticky role-filtered header nav, `<style>BASE_CSS</style>`, `<script>BASE_JS</script>`, and an XSS-hardened `window.I18N_CATALOG`. **All HTML routes render through this.**
- `BASE_CSS` — dark-first design system (light theme via `prefers-color-scheme` + manual `data-theme`), tokens, components (cards, tables with sticky headers, modals, toasts, spinners), responsive breakpoints.
- `BASE_JS` — `window.fetchJSON` (central HTTP helper: JSON bodies, auto `X-CSRF-Token`, timeout, non-2xx → `Error` with `.status/.body`), `window.setupModal` (focus-trap), `window.toast`, `window.copyToClipboard`, `window.csrfToken`, `window.esc`, theme toggle, `window.switchLang`, `window.makeVisibilityAware` (pauses intervals when tab hidden).
- Target browsers: Chrome 60 / Firefox 60 / Safari 12+ (no `:focus-visible`, `<dialog>`, `AbortController`).

## i18n

`app/ui/i18n.py` wraps Flask-Babel. Supported: `es` (default), `en`. Resolution: `?lang=` → session → `lang` cookie → `Accept-Language` → `es`.

**Critical**: there are **no compiled `.mo` files checked in**. `get_catalog()` falls back to the hardcoded `_FALLBACK_ES`/`_FALLBACK_EN` dicts so the app runs pre-`pybabel compile`. Run `pybabel compile -d app/translations -D messages` (or the Dockerfile step) to activate the `.po` catalogs. Extraction config: `babel.cfg`.

## Logging

Structured **JSON to stdout** (`app/logging_config.py`): `OpenAceJsonFormatter` guarantees `timestamp, level, component, event` fields; `_RedactTokenFilter` recursively redacts sensitive keys (`token, password, authorization, secret, api_key, apikey`) and `?token=` query params from every log record.

Convention: call `log_event(level, event, component, **fields)` from `app/utils/logging_utils.py`. Each route module defines a `COMPONENT` constant (e.g. `"auth"`, `"play_proxy"`, `"hls_ffmpeg"`, `"check"`, `"plugins_api"`, `"setup"`, `"eula"`, `"peers"`, `"playlist_proxy"`).

## Config / environment

`app/utils/environment_store.py` is the settings system: declares every configurable key (type/default/group/validation), persists overrides in `environment_settings`, and exposes typed getters (`get_str/get_int/get_float/get_bool`). Precedence: DB value → process env → spec default. Many modules read their tunables from here at import time.

Key groups: OpenAce (`AUTH_ENABLED`, `SESSION_DURATION_HOURS`, `OPENACE_SECRET_KEY`), AceStream (`ACESTREAM_HOST/PORT`), IPFS (`IPFS_GATEWAY`), Proxy (`REVERSE_PROXY`, `FORWARDED_ALLOW_IPS`, `PUBLIC_BASE_URL`), FFmpeg (`OPENACE_FFMPEG_ENABLED`, `OPENACE_IDLE_TIMEOUT_S`, `OPENACE_MAX_STREAMS`, queue/pipe/restart tunables), HLS (`OPENACE_HLS_*`), Gunicorn (`GUNICORN_WORKERS/WORKER_CONNECTIONS`), Auto-setup (`OPENACE_AUTO_SETUP`, `OPENACE_ADMIN_USER/PASSWORD`, `OPENACE_EULA_ACCEPT`). `list_settings()` redacts secrets and never exposes `EXCLUDED_ENV_KEYS = {TZ, WG_PRIVATE_KEY, ProtonCountries}` (those stay in `.env`). `DB_PATH` and `OPENACE_SECRET_FILE` are read directly from `os.environ` (needed before the DB/secret exist).

## Key patterns

- **JSON body parsing**: `get_json_body()` from `app/utils/auth_helpers.py` returns `(data, (response, status))` — **always use instead of** `request.get_json(silent=True) or {}`. Empty body → `({}, None)`; malformed → `(None, (jsonify({"error": ...}), 400))`.
- **Role enforcement**: `@require_role(role)` decorator; `current_user()` reads `g.user`; `auth_enabled()` checks `AUTH_ENABLED` (when off, `require_role` passes through).
- **HTTP client**: import the shared pooled session from `app/utils/upstream.py` (`from app.utils.upstream import session`) — do not create ad-hoc `requests` sessions.
- **Engine clients**: `app/utils/acestream.py` (`negotiate_stream`, `check_stream`, `stop_stream`, `read_stat`) for playback/probes; `app/utils/acestream_api.py` (`AceStreamAPI`, singleton `get_api()`) for dashboard/monitor/geoip.
- **Error responses**: JSON for `/api/*` routes, HTML for browser routes (handled centrally).
- **M3U output**: build via `app/routes/playlist.py` helpers; always sanitize with `_m3u_safe`.

## Testing

pytest, fully isolated, **no network and no real AceStream engine**. Heavy use of `monkeypatch` and fake/stub classes; the Flask `test_client` is the integration vehicle.

`tests/conftest.py` fixtures:
- `isolated_db` (autouse) — per-test `DB_PATH` under `tmp_path`, drains `check_store._pool`, resets `_initialised` flags and caches across all stores + `plugin_cache`.
- `app` → `create_app()`; `client` → `test_client()`; `authed` → logs in and extracts a CSRF token from `/admin/users` HTML, returns `(client, token)`.

Tests are organized one class per concern/ticket: `tests/test_api_stability.py` (auth/CSRF, rate limiting, SSRF/pinned-DNS, pagination, session lifecycle, plugin CRUD, HLS cold-start, env redaction, peer detection), `tests/test_ffmpeg_manager_units.py` (TS alignment, cmd building, adaptive queue, restart recovery, spawn cleanup), `tests/test_play_proxy.py` (content-id validation, content-type, HEAD/Range, streaming headers), `tests/test_acestream_units.py` (`negotiate_stream` fallback behavior).

## Deployment

`start.sh` (container entrypoint, `set -e`):
1. `chown`/verify `openace` can write `/openace/checkdb` & `/tmp/openace` (via `gosu`).
2. **Gluetun port-forward**: reads `/tmp/gluetun/forwarded_port` (waits ≤20s); splits API port (always `ACESTREAM_PORT=6878`, needed by Flask) from P2P port (forwarded, passed to engine via `--bind`).
3. **Engine**: `while true` restart loop (backgrounded) running `/openace/start-engine --client-console --port $API_PORT [--bind $P2P_PORT]`; output → stdout/stderr for `docker logs`.
4. **Gunicorn** (foreground, `exec gosu openace`): `--worker-class gevent --bind 0.0.0.0:8888`, `GUNICORN_WORKERS` (default 1), `GUNICORN_WORKER_CONNECTIONS` (default 2000), `--keep-alive 15 --timeout 3600 --graceful-timeout 3600 --max-requests 1000 --max-requests-jitter 100`. Adds `--forwarded-allow-ips` when `REVERSE_PROXY`.

Image: Python 3.10-slim; multi-stage (builder downloads AceStream engine tarball with SHA256 verification); installs ffmpeg/curl/gosu/iproute2; compiles i18n `.mo`; healthcheck on `/healthz`; non-root `openace` user. Compose variants in `docker-compose*.yaml` (simple/dev/vps/VPN). See `DOC/` for scenarios.
