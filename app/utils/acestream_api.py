"""Cliente unificado para el API HTTP del motor AceStream.

El motor AceStream 3.2.11 (modo ``--client-console``) expone TRES surfaces API
con distinto control de acceso.  Este modulo usa las correctas:

1.  ``/webui/api/service?method=<name>``  — metodos PUBLICOS (sin auth).
    Solo ``get_version`` y ``get_api_access_token`` funcionan aqui; el resto
    devuelve ``{"error": "access denied"}``.

2.  ``/server/api/?method=<name>&token=<api_access_token>``  — metodos
    privilegiados del webui del servidor.  El ``api_access_token`` se obtiene
    del metodo publico ``get_api_access_token`` o del fichero
    ``engine_runtime.json``.  Metodos utiles: ``get_server_settings``,
    ``get_status``.

3.  ``/app/monitor`` en el puerto LM (8621 por defecto, sin auth)  — stats en
    vivo del motor: velocidades, contadores, ``max_peers`` y
    ``connected_peers[]`` (los peers P2P reales con ip, puerto y tasas).

Todos los metodos son best-effort: ante cualquier error de transporte o del
motor devuelven ``None`` (o ``{}``) y loguean un warning, para que el llamador
degrade con elegancia.
"""

import json
import os
import subprocess
import threading
import time

from app.utils.logging_utils import log_event
from app.utils.upstream import session as _http_session

COMPONENT = "acestream_api"

API_TIMEOUT_S = float(os.environ.get("OPENACE_ENGINE_API_TIMEOUT", "3"))
MONITOR_TIMEOUT_S = float(os.environ.get("OPENACE_ENGINE_MONITOR_TIMEOUT", "3"))
CACHE_TTL_S = int(os.environ.get("OPENACE_ENGINE_STATUS_CACHE_S", "30"))

DEFAULT_MONITOR_PORT = "8621"

# Local services that are never the AceStream LM monitor endpoint. Filtering
# them avoids noisy probes against Flask/Gluetun/DNS and AceStream control ports.
_STATIC_NON_LM_PORTS = {53, 8001, 8888, 9999}

# Caducidad del api_access_token en cache (es estable por proceso del motor).
_API_TOKEN_TTL_S = 300

# TTL del puerto LM cacheado. El LM puede cambiar al rearrancar el engine.
_LM_PORT_TTL_S = 120

# Timeout por candidato al sondar /app/monitor. Debe ser corto: algunos
# puertos (p.ej. 53) cuelgan ~1s sin responder.
_LM_PROBE_TIMEOUT_S = 0.5

# Fichero que el motor escribe al arrancar con puertos/tokens.
_ENGINE_RUNTIME_PATH = "/openace/engine_runtime.json"

_lm_cache_lock = threading.Lock()
_lm_cache = {"key": None, "port": None, "ts": 0.0}


def _read_engine_runtime() -> dict:
    """Best-effort read of the engine's runtime metadata file."""
    path = os.environ.get("OPENACE_ENGINE_RUNTIME_PATH", _ENGINE_RUNTIME_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _list_listen_ports() -> list:
    """Return local TCP ports in LISTEN state via ``ss -tln``.

    Uses iproute2 (installed in the Docker image). Falls back to an empty
    list if ``ss`` is unavailable. Does NOT require /proc access (psutil
    net_connections raises PermissionError for the engine process under the
    non-root openace user).
    """
    try:
        out = subprocess.check_output(
            ["ss", "-tln"], text=True, timeout=2, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    ports = set()
    for line in out.splitlines()[1:]:
        # Local Address column, e.g. "0.0.0.0:8621" or "*:53" or "[::]:53"
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        # Handle both "1.2.3.4:PORT" and "*:PORT" / "[::]:PORT"
        if ":" in local:
            port_str = local.rsplit(":", 1)[-1]
            if port_str.isdigit():
                ports.add(int(port_str))
    return sorted(ports)


def _looks_like_monitor(data: dict, version_code=None) -> bool:
    """Heuristic: does this JSON look like the engine ``/app/monitor`` payload?

    Guards against false positives — e.g. port 9999 returns HTTP 200 with an
    empty body, which would pass a naive ``r.ok`` check.
    """
    if not isinstance(data, dict):
        return False
    if data.get("type") != "client":
        return False
    # ``max_peers`` is always present in the monitor payload; absent in the
    # empty 200 from other local services.
    if "max_peers" not in data:
        return False
    # Optional: cross-check version_code if the engine version is known.
    if version_code is not None:
        if data.get("version_code") not in (None, version_code):
            return False
    return True


def _probe_lm_port(host: str, port: int, version_code=None) -> dict | None:
    """Probe ``/app/monitor`` on a single candidate port; return payload or None."""
    url = f"http://{host}:{port}/app/monitor"
    try:
        resp = _http_session.get(url, timeout=_LM_PROBE_TIMEOUT_S)
        try:
            data = resp.json()
        finally:
            resp.close()
    except Exception:
        return None
    if _looks_like_monitor(data, version_code):
        return data
    return None


def _discover_lm_port(host: str, known_ports: set, version_code=None) -> str | None:
    """Discover the dynamic LM port by probing ``/app/monitor`` on all LISTEN ports.

    Returns the first port that responds with a valid monitor payload, or
    ``None`` if none does. Excludes the Flask app port (8888) and the engine
    HTTP port (already in ``known_ports``) to avoid self-probes.
    """
    import concurrent.futures

    excluded = set(known_ports) | _STATIC_NON_LM_PORTS
    candidates = [p for p in _list_listen_ports() if p not in excluded]
    if not candidates:
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_probe_lm_port, host, p, version_code): p
            for p in candidates
        }
        try:
            iterator = concurrent.futures.as_completed(futures, timeout=3.0)
            for fut in iterator:
                try:
                    result = fut.result(timeout=_LM_PROBE_TIMEOUT_S + 0.2)
                except Exception:
                    continue
                if result is not None:
                    return str(futures[fut])
        except concurrent.futures.TimeoutError:
            return None
    return None


class AceStreamAPI:
    """Thin wrapper around the AceStream engine HTTP API.

    All methods are best-effort: on any transport or engine error they return
    ``None`` (or ``{}`` for dict-returning methods) and log a warning, so the
    caller can degrade gracefully.
    """

    def __init__(self, host="127.0.0.1", port="6878", monitor_port=None):
        self._host = host
        self._port = str(port)
        self._base = f"http://{host}:{self._port}"
        self._webui = f"{self._base}/webui/api/service"
        self._server_api = f"{self._base}/server/api/"

        # Explicit monitor port (env or arg) disables dynamic discovery.
        self._monitor_port_override = str(
            monitor_port
            or os.environ.get("ACESTREAM_MONITOR_PORT")
            or ""
        )
        self._api_token: str | None = None
        self._api_token_ts = 0.0
        self._lm_version_code: int | None = None

    # ------------------------------------------------------------------
    #  Low-level helpers
    # ------------------------------------------------------------------

    def _call_public(self, method, **params):
        """Call ``/webui/api/service?method=<method>`` (no auth, public only)."""
        qs = {"method": method}
        qs.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = _http_session.get(self._webui, params=qs, timeout=API_TIMEOUT_S)
            try:
                data = resp.json()
            finally:
                resp.close()
        except Exception as e:
            log_event("warning", "engine_api_call_failed", COMPONENT,
                      method=method, error=str(e))
            return None
        if data.get("error"):
            log_event("warning", "engine_api_error", COMPONENT,
                      method=method, error=data["error"])
            return None
        return data.get("result")

    def _get_api_access_token(self):
        """Return the api_access_token, cached for ``_API_TOKEN_TTL_S`` seconds.

        Source priority: cached -> public method get_api_access_token ->
        engine_runtime.json file.
        """
        if self._api_token and time.time() - self._api_token_ts < _API_TOKEN_TTL_S:
            return self._api_token

        token = None
        result = self._call_public("get_api_access_token")
        if isinstance(result, dict) and result.get("token"):
            token = result["token"]

        if not token:
            rt = _read_engine_runtime()
            token = rt.get("api_access_token")

        if token:
            self._api_token = token
            self._api_token_ts = time.time()
        return token

    def _call_priv(self, method, **params):
        """Call ``/server/api/?method=<method>&token=<api_access_token>``."""
        token = self._get_api_access_token()
        if not token:
            log_event("warning", "engine_api_no_token", COMPONENT, method=method)
            return None
        qs = {"method": method, "token": token}
        qs.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = _http_session.get(self._server_api, params=qs, timeout=API_TIMEOUT_S)
            try:
                data = resp.json()
            finally:
                resp.close()
        except Exception as e:
            log_event("warning", "engine_api_call_failed", COMPONENT,
                      method=method, error=str(e))
            return None
        if data.get("error"):
            log_event("warning", "engine_api_error", COMPONENT,
                      method=method, error=data["error"])
            return None
        return data.get("result")

    def _resolve_monitor_url(self) -> str | None:
        """Return the ``/app/monitor`` URL, discovering the LM port if needed.

        Priority:
        1. Explicit override (``ACESTREAM_MONITOR_PORT`` env or constructor arg).
        2. Cached LM port (refreshed after ``_LM_PORT_TTL_S``).
        3. Dynamic discovery via ``ss -tln`` + probing.
        """
        if self._monitor_port_override:
            return f"http://{self._host}:{self._monitor_port_override}/app/monitor"

        now = time.time()
        cache_key = (self._host, self._port)
        with _lm_cache_lock:
            if (
                _lm_cache["key"] == cache_key
                and _lm_cache["port"]
                and now - _lm_cache["ts"] < _LM_PORT_TTL_S
            ):
                return f"http://{self._host}:{_lm_cache['port']}/app/monitor"

        # Discover the LM port. Probe known engine version_code for a stricter
        # match (fetched lazily from get_version, non-fatal if unavailable).
        version_code = self._lm_version_code
        known = {int(self._port), 8888}
        rt = _read_engine_runtime()
        for key in (
            "http_port",
            "https_port",
            "legacy_api_port",
            "websocket_port",
            "api_port",
            "p2p_port",
        ):
            try:
                if rt.get(key) is not None:
                    known.add(int(rt[key]))
            except (TypeError, ValueError):
                pass
        port = _discover_lm_port(self._host, known, version_code)
        if not port:
            return None
        with _lm_cache_lock:
            _lm_cache["key"] = cache_key
            _lm_cache["port"] = port
            _lm_cache["ts"] = now
        return f"http://{self._host}:{port}/app/monitor"

    def get_monitor(self):
        """Return the live engine stats from ``/app/monitor`` (LM port, no auth).

        Contains: version, download_speed, upload_speed, downloaded, uploaded,
        max_peers, connected_peers_count, connected_peers[], run_time,
        cpu_percent, total_http_uploaded, transport_stats, etc.  Returns
        ``None`` on any error.  The LM port is dynamic per engine restart and is
        auto-discovered via ``ss -tln`` + probing ``/app/monitor`` on each
        candidate (cached for ``_LM_PORT_TTL_S`` seconds).
        """
        url = self._resolve_monitor_url()
        if not url:
            log_event("warning", "engine_monitor_port_not_found", COMPONENT)
            return None
        try:
            resp = _http_session.get(url, timeout=MONITOR_TIMEOUT_S)
            try:
                data = resp.json()
            finally:
                resp.close()
        except Exception as e:
            # Cached port may be stale (engine restarted with a new LM port).
            # Invalidate the cache and retry once with a fresh discovery.
            with _lm_cache_lock:
                had_cached_port = _lm_cache["key"] == (self._host, self._port) and _lm_cache["port"]
                if had_cached_port:
                    _lm_cache["key"] = None
                    _lm_cache["port"] = None
                    _lm_cache["ts"] = 0.0
            if had_cached_port:
                url = self._resolve_monitor_url()
                if not url:
                    log_event("warning", "engine_monitor_port_not_found", COMPONENT)
                    return None
                try:
                    resp = _http_session.get(url, timeout=MONITOR_TIMEOUT_S)
                    try:
                        data = resp.json()
                    finally:
                        resp.close()
                except Exception as e2:
                    log_event("warning", "engine_monitor_failed", COMPONENT, error=str(e2))
                    return None
            else:
                log_event("warning", "engine_monitor_failed", COMPONENT, error=str(e))
                return None
        if not isinstance(data, dict):
            return None
        if data.get("error"):
            log_event("warning", "engine_monitor_error", COMPONENT,
                      error=data.get("error"))
            return None
        # Cache the engine version_code for stricter future probe matching.
        if data.get("version_code") is not None:
            self._lm_version_code = data.get("version_code")
        return data

    # ------------------------------------------------------------------
    #  Engine status / config
    # ------------------------------------------------------------------

    def get_version(self):
        """Return ``{"version": "3.2.11", "platform": "linux", ...}`` or ``None``.

        Public method — also used as the engine liveness check.
        """
        return self._call_public("get_version")

    def get_status(self):
        """Return ``{"version": {...}, "playlist_loaded": bool}`` or ``None``."""
        return self._call_priv("get_status")

    def get_settings(self):
        """Full engine server settings dict (privileged)."""
        return self._call_priv("get_server_settings")

    def disk_cache_cleanup(self):
        """Trigger disk-cache cleanup on the engine (best-effort)."""
        return self._call_priv("disk_cache_cleanup")

    # ------------------------------------------------------------------
    #  Content metadata  (best-effort; may be unavailable in client mode)
    # ------------------------------------------------------------------

    def get_content_metadata(self, infohash):
        return self._call_priv("get_content_metadata", infohash=infohash)

    def get_is_live(self, infohash):
        return self._call_priv("get_is_live", infohash=infohash)

    # ------------------------------------------------------------------
    #  Geo-IP  (engine built-in geoip; may be denied in client mode)
    # ------------------------------------------------------------------

    def get_geoip_country(self, ip):
        return self._call_priv("get_geoip_country", ip=ip)

    def get_asn(self, ip):
        return self._call_priv("get_asn", ip=ip)

    # ------------------------------------------------------------------
    #  Aggregated helper for the dashboard
    # ------------------------------------------------------------------

    def get_engine_info(self):
        """Fetch version + settings + live monitor in parallel.

        Returns a flat dict suitable for the dashboard. Each key is ``None``
        if the individual call failed.
        """
        import concurrent.futures

        fields = {}
        errors = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "version": pool.submit(self.get_version),
                "settings": pool.submit(self.get_settings),
                "monitor": pool.submit(self.get_monitor),
            }
            for key, fut in futures.items():
                try:
                    fields[key] = fut.result(timeout=API_TIMEOUT_S + 1)
                except Exception as e:
                    fields[key] = None
                    errors.append(f"{key}: {e}")
        if errors:
            log_event("debug", "engine_info_partial", COMPONENT, errors=errors)
        return fields


# ------------------------------------------------------------------
#  Module-level singleton + cache
# ------------------------------------------------------------------

_api_lock = threading.Lock()
_api_instance = None

_cache_lock = threading.Lock()
_cache = {"key": None, "data": None, "ts": 0.0}


def get_api():
    """Return the shared :class:`AceStreamAPI` singleton."""
    global _api_instance
    with _api_lock:
        if _api_instance is None:
            from flask import current_app
            host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
            port = str(current_app.config.get("ACESTREAM_PORT", "6878"))
            _api_instance = AceStreamAPI(host, port)
        return _api_instance


def reset_api():
    """Discard the cached singleton (used on config reload)."""
    global _api_instance
    with _api_lock:
        _api_instance = None


def get_cached_engine_info():
    """Return cached ``get_engine_info()`` result, refreshing every ``CACHE_TTL_S``."""
    host = os.environ.get("ACESTREAM_HOST", "127.0.0.1")
    port = os.environ.get("ACESTREAM_PORT", "6878")
    monitor_port = os.environ.get("ACESTREAM_MONITOR_PORT")
    cache_key = (host, port, monitor_port)
    now = time.time()
    with _cache_lock:
        if (
            _cache["key"] == cache_key
            and _cache["data"] is not None
            and now - _cache["ts"] < CACHE_TTL_S
        ):
            return _cache["data"]
    api = AceStreamAPI(host, port, monitor_port)
    data = api.get_engine_info()
    with _cache_lock:
        _cache["key"] = cache_key
        _cache["data"] = data
        _cache["ts"] = time.time()
    return data
