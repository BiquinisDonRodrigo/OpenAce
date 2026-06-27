import ipaddress
import re
import subprocess
import threading
import time

import psutil
import requests
from flask import Blueprint, Response, current_app, jsonify, request

from datetime import datetime, timezone

from app.utils import plugin_cache as _plugin_cache
from app.utils import plugin_store as _plugin_store
from app.utils import stream_registry
from app.utils.logging_utils import log_event
from app.routes import hls as hls_module

panel_bp = Blueprint("peers", __name__)

_ip_cache: dict = {"data": None, "ts": 0.0}
_ip_lock = threading.Lock()

# Per-IP geo cache: ip -> {"data": {...}, "ts": float}
_peer_cache: dict = {}
_peer_cache_lock = threading.Lock()
_PEER_CACHE_MAX = 500

_speed_cache: dict = {}
_speed_cache_lock = threading.Lock()

_status_cache: dict = {"data": None, "ts": 0.0}
_status_cache_lock = threading.Lock()
_engine_status_cache: dict = {"data": None, "ts": 0.0, "key": None}
_engine_status_cache_lock = threading.Lock()
_connections_cache: dict = {"data": None, "ts": 0.0}
_connections_cache_lock = threading.Lock()
_socket_counts_cache: dict = {"data": None, "ts": 0.0}
_socket_counts_cache_lock = threading.Lock()
_name_map_cache: dict = {"data": None, "ts": 0.0}
_name_map_cache_lock = threading.Lock()
_plugins_info_cache: dict = {"data": None, "ts": 0.0}
_plugins_info_cache_lock = threading.Lock()

STATUS_CACHE_TTL_S = 2
ENGINE_STATUS_CACHE_TTL_S = 30
CONNECTIONS_CACHE_TTL_S = 5
SOCKET_COUNTS_CACHE_TTL_S = 3
NAME_MAP_CACHE_TTL_S = 60
PLUGINS_INFO_CACHE_TTL_S = 30
PEER_GEO_TIMEOUT_S = 1.5
MAX_GEO_LOOKUPS_PER_REFRESH = 3
_CONTENT_ID_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _get_ip_info():
    with _ip_lock:
        if _ip_cache["data"] and time.time() - _ip_cache["ts"] < 300:
            return _ip_cache["data"]
    try:
        r = requests.get("https://ipinfo.io/json", timeout=5)
        r.raise_for_status()
        data = r.json()
    except Exception:
        with _ip_lock:
            return _ip_cache["data"]
    with _ip_lock:
        _ip_cache["data"] = data
        _ip_cache["ts"] = time.time()
    return data


def _get_cached_peer_ip_info(ip: str) -> dict | None:
    """Return cached ipinfo.io geo data for a peer IP, if still valid."""
    with _peer_cache_lock:
        entry = _peer_cache.get(ip)
        if entry and time.time() - entry["ts"] < 3600:
            return entry["data"]
    return None


def _get_peer_ip_info(ip: str) -> dict:
    """Fetch ipinfo.io geo data for a specific peer IP, cached for 1 hour."""
    cached = _get_cached_peer_ip_info(ip)
    if cached is not None:
        return cached
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=PEER_GEO_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
    except Exception:
        data = {}
    now = time.time()
    with _peer_cache_lock:
        _peer_cache[ip] = {"data": data, "ts": now}
        if len(_peer_cache) > _PEER_CACHE_MAX:
            stale = [k for k, v in _peer_cache.items() if now - v["ts"] > 3600]
            for k in stale:
                del _peer_cache[k]
            if len(_peer_cache) > _PEER_CACHE_MAX:
                overflow = len(_peer_cache) - _PEER_CACHE_MAX
                oldest = sorted(_peer_cache.items(), key=lambda kv: kv[1]["ts"])[:overflow]
                for k, _ in oldest:
                    del _peer_cache[k]
    return data


def _get_socket_byte_counts() -> dict:
    """Parse ss -tin for per-socket bytes_sent / bytes_received."""
    now = time.time()
    with _socket_counts_cache_lock:
        if (
            _socket_counts_cache["data"] is not None
            and now - _socket_counts_cache["ts"] < SOCKET_COUNTS_CACHE_TTL_S
        ):
            return _socket_counts_cache["data"]

    result = {}
    try:
        out = subprocess.check_output(
            ["ss", "-tin"], text=True, timeout=5, stderr=subprocess.DEVNULL
        )
    except Exception:
        with _socket_counts_cache_lock:
            return _socket_counts_cache["data"] or result
    key = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            parts = line.split()
            key = (parts[3], parts[4]) if len(parts) >= 5 else None
        elif key:
            m_s = re.search(r"bytes_sent:(\d+)", line)
            m_r = re.search(r"bytes_received:(\d+)", line)
            if m_s or m_r:
                result[key] = {
                    "sent": int(m_s.group(1)) if m_s else 0,
                    "recv": int(m_r.group(1)) if m_r else 0,
                }
    with _socket_counts_cache_lock:
        _socket_counts_cache["data"] = result
        _socket_counts_cache["ts"] = time.time()
    return result


def _get_acestream_peer_connections() -> list:
    """Return real P2P peers from the engine's ``/app/monitor`` endpoint.

    The previous implementation enumerated the engine process' TCP sockets via
    ``psutil``. Without an active stream that only surfaced the loopback
    control connections between OpenAce and the engine (127.0.0.1), which
    were painted as fake P2P peers with all geo fields in '—'.  The engine
    exposes the genuine P2P peers (with real IPs and per-peer rates) via the
    ``/app/monitor`` endpoint on the LM port, so we use that instead.
    """
    from app.utils.acestream_api import AceStreamAPI

    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = str(current_app.config.get("ACESTREAM_PORT", "6878"))
    api = AceStreamAPI(host, port)

    monitor = api.get_monitor()
    raw_peers = (monitor or {}).get("connected_peers") or []
    if not raw_peers:
        return []

    # Collect unique public IPs for geo enrichment (capped to avoid bursts).
    seen_ips: set = set()
    for p in raw_peers:
        ip = (p.get("ip") or "").strip()
        if not ip:
            continue
        try:
            if ipaddress.ip_address(ip).is_global:
                seen_ips.add(ip)
        except ValueError:
            continue

    ip_info_map: dict = {}
    lookups_left = MAX_GEO_LOOKUPS_PER_REFRESH
    for ip in seen_ips:
        cached = _get_cached_peer_ip_info(ip)
        if cached is not None:
            ip_info_map[ip] = cached
        elif lookups_left > 0:
            ip_info_map[ip] = _get_peer_ip_info(ip)
            lookups_left -= 1
        else:
            ip_info_map[ip] = {}

    peers = []
    for p in raw_peers:
        ip = (p.get("ip") or "").strip()
        geo = ip_info_map.get(ip, {}) if ip else {}
        peers.append({
            "peer_id": p.get("id") or "—",
            "ip": ip or "—",
            "external_port": p.get("external_port"),
            "download_speed": p.get("downrate"),
            "upload_speed": p.get("uprate"),
            "dedicated_download_rate": p.get("dedicated_download_rate"),
            "channel_download_rate": p.get("channel_download_rate"),
            "state": "ESTABLISHED",
            "org": geo.get("org", "—"),
            "city": geo.get("city", "—"),
            "country": geo.get("country", "—"),
            "timezone": geo.get("timezone", "—"),
            "loc": geo.get("loc", "—"),
        })
    return peers


def _get_engine_status(host, port):
    cache_key = (host, port)
    now = time.time()
    with _engine_status_cache_lock:
        if (
            _engine_status_cache["key"] == cache_key
            and _engine_status_cache["data"] is not None
            and now - _engine_status_cache["ts"] < ENGINE_STATUS_CACHE_TTL_S
        ):
            return _engine_status_cache["data"]

    from app.utils.acestream_api import AceStreamAPI
    api = AceStreamAPI(host, str(port))

    # Version is the liveness check — if it fails the engine is down.
    version_result = api.get_version()
    if version_result is None:
        return {"up": False, "version": None}

    data = {
        "up": True,
        "version": version_result.get("version", "?"),
        "platform": version_result.get("platform"),
    }

    # Enrich with live monitor stats + server settings (best-effort, non-fatal).
    try:
        info = api.get_engine_info()
        monitor = info.get("monitor") if info else None
        if isinstance(monitor, dict):
            data["download_speed"] = monitor.get("download_speed")
            data["upload_speed"] = monitor.get("upload_speed")
            data["downloaded"] = monitor.get("downloaded")
            data["uploaded"] = monitor.get("uploaded")
            data["max_peers"] = monitor.get("max_peers")
            data["max_connections"] = monitor.get("max_connections")
            data["connected_peers_count"] = monitor.get("connected_peers_count")
            data["upload_slots"] = monitor.get("upload_slots")
            data["run_time"] = monitor.get("run_time")
            data["cpu_percent"] = monitor.get("cpu_percent")
            data["total_http_uploaded"] = monitor.get("total_http_uploaded")
            data["status"] = monitor.get("status")

        settings = info.get("settings") if info else None
        if isinstance(settings, dict):
            data["external_ip"] = settings.get("external_ip")
            data["allow_remote_access"] = bool(settings.get("allow_remote_access"))
            data["allow_intranet_access"] = bool(settings.get("allow_intranet_access"))
    except Exception:
        pass

    with _engine_status_cache_lock:
        _engine_status_cache["key"] = cache_key
        _engine_status_cache["data"] = data
        _engine_status_cache["ts"] = time.time()
    return data


def _get_connections():
    now = time.time()
    with _connections_cache_lock:
        if (
            _connections_cache["data"] is not None
            and now - _connections_cache["ts"] < CONNECTIONS_CACHE_TTL_S
        ):
            return _connections_cache["data"]

    incoming = []
    outgoing_ace = []
    outgoing_ext = []
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != "ESTABLISHED" or not conn.raddr:
                continue
            lport = conn.laddr.port
            rport = conn.raddr.port
            rip = conn.raddr.ip
            if lport == 8888:
                incoming.append(f"{rip}:{rport}")
            elif rport == 6878:
                outgoing_ace.append(f"{conn.laddr.ip}:{lport} → {rip}:{rport}")
            else:
                outgoing_ext.append(f"{conn.laddr.ip}:{lport} → {rip}:{rport}")
    except (psutil.AccessDenied, PermissionError):
        with _connections_cache_lock:
            return _connections_cache["data"] or (incoming, outgoing_ace, outgoing_ext)

    data = (incoming, outgoing_ace, outgoing_ext)
    with _connections_cache_lock:
        _connections_cache["data"] = data
        _connections_cache["ts"] = time.time()
    return data


def _build_name_map():
    now = time.time()
    with _name_map_cache_lock:
        if (
            _name_map_cache["data"] is not None
            and now - _name_map_cache["ts"] < NAME_MAP_CACHE_TTL_S
        ):
            return _name_map_cache["data"]

    name_map = {}
    try:
        for plugin in _plugin_store.get_all():
            for ch in _plugin_cache.get_channels(plugin["id"]):
                ih = ch.get("infohash")
                if ih and ih not in name_map:
                    name_map[ih] = ch.get("name", "Desconocido")
    except Exception:
        with _name_map_cache_lock:
            return _name_map_cache["data"] or name_map

    with _name_map_cache_lock:
        _name_map_cache["data"] = name_map
        _name_map_cache["ts"] = time.time()
    return name_map


def _get_plugins_info():
    now = time.time()
    with _plugins_info_cache_lock:
        if (
            _plugins_info_cache["data"] is not None
            and now - _plugins_info_cache["ts"] < PLUGINS_INFO_CACHE_TTL_S
        ):
            return _plugins_info_cache["data"]

    plugins_info = []
    try:
        for p in _plugin_store.get_all():
            entry = _plugin_cache.get_entry(p["id"])
            ch_count = len(entry["channels"]) if entry else 0
            age = None
            if p["last_refresh"]:
                try:
                    lr = datetime.fromisoformat(p["last_refresh"])
                    if lr.tzinfo is None:
                        lr = lr.replace(tzinfo=timezone.utc)
                    age = round((datetime.now(timezone.utc) - lr).total_seconds())
                except Exception:
                    pass
            plugins_info.append({
                "name": p["display_name"],
                "slug": p["name"],
                "channels": ch_count,
                "refresh_interval": p["refresh_interval"],
                "last_refresh_s": age,
            })
    except Exception:
        with _plugins_info_cache_lock:
            return _plugins_info_cache["data"] or plugins_info

    with _plugins_info_cache_lock:
        _plugins_info_cache["data"] = plugins_info
        _plugins_info_cache["ts"] = time.time()
    return plugins_info


@panel_bp.route("/api/peers/hls/<content_id>/kill", methods=["POST"])
def api_kill_hls_stream(content_id):
    if not _CONTENT_ID_RE.match(content_id):
        return jsonify({"error": "Content ID inválido"}), 400

    manager = hls_module._manager
    if manager is None:
        return jsonify({"error": "Stream manager no disponible"}), 503

    session = manager.drop(content_id)
    if session is None:
        return jsonify({"error": "Stream no encontrado"}), 404

    log_event(
        "info", "ffmpeg_stream_killed_from_panel", "panel",
        content_id=content_id,
        remote_addr=request.remote_addr,
    )
    with _status_cache_lock:
        _status_cache["data"] = None
        _status_cache["ts"] = 0.0
    return jsonify({"ok": True, "content_id": content_id})


@panel_bp.route("/api/peers/status")
def api_status():
    now = time.time()
    with _status_cache_lock:
        if (
            _status_cache["data"] is not None
            and now - _status_cache["ts"] < STATUS_CACHE_TTL_S
        ):
            return jsonify(_status_cache["data"])

    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = current_app.config.get("ACESTREAM_PORT", "6878")

    engine = _get_engine_status(host, port)
    ip_info = _get_ip_info()

    manager = hls_module._manager
    hls_streams = []
    if manager is not None:
        hls_streams = manager.snapshot()

    plugins_info = _get_plugins_info()
    incoming, outgoing_ace, outgoing_ext = _get_connections()
    engine_peers = _get_acestream_peer_connections()

    name_map = _build_name_map()
    active_streams = []
    for s in stream_registry.get_active():
        cid = s["content_id"]
        s["name"] = name_map.get(cid, "Desconocido")
        active_streams.append(s)

    data = {
        "engine": engine,
        "ip_info": ip_info,
        "hls_streams": hls_streams,
        "plugins": plugins_info,
        "connections": {
            "incoming": incoming,
            "outgoing_acestream": outgoing_ace,
            "outgoing_external": outgoing_ext,
        },
        "engine_peers": engine_peers,
        "active_streams": active_streams,
        "ts": int(time.time()),
    }
    with _status_cache_lock:
        _status_cache["data"] = data
        _status_cache["ts"] = time.time()
    return jsonify(data)


@panel_bp.route("/api/admin/engine/disk-cache-cleanup", methods=["POST"])
def api_engine_disk_cache_cleanup():
    """Trigger disk-cache cleanup on the AceStream engine (admin only)."""
    from app.utils.acestream_api import AceStreamAPI
    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = str(current_app.config.get("ACESTREAM_PORT", "6878"))
    api = AceStreamAPI(host, port)
    result = api.disk_cache_cleanup()
    if result is None:
        return jsonify({"ok": False, "error": "Engine no responde o metodo no soportado"}), 502
    # Invalidate engine status cache so next poll picks up fresh disk stats.
    with _engine_status_cache_lock:
        _engine_status_cache["data"] = None
    return jsonify({"ok": True, "result": result})


@panel_bp.route("/api/engine/geoip/<path:ip>")
def api_engine_geoip(ip):
    """Best-effort Geo-IP lookup using the engine's built-in geoip database."""
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return jsonify({"ok": False, "error": "IP inválida"}), 400
    from app.utils.acestream_api import AceStreamAPI
    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = str(current_app.config.get("ACESTREAM_PORT", "6878"))
    api = AceStreamAPI(host, port)
    country = api.get_geoip_country(ip)
    asn = api.get_asn(ip)
    if country is None and asn is None:
        return jsonify({"ok": False, "error": "Engine geoip no disponible"}), 502
    return jsonify({"ok": True, "ip": ip, "country": country, "asn": asn})


_PANEL_HEAD_CSS = """
  /* Page-specific (peers dashboard). Tokens and base components come from BASE_CSS. */
  .panel-header-bar{display:flex;gap:var(--space-3);align-items:center;flex-wrap:wrap;padding:var(--space-3) var(--space-4);background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);margin-top:var(--space-3)}
  .panel-header-bar h1{font-size:1rem;font-weight:600;color:var(--blue);white-space:nowrap;margin:0}
  .header-stats{display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap}
  .header-stats .hs{font-size:.786rem;font-family:var(--font-mono);white-space:nowrap}
  .header-stats .hs-label{color:var(--muted);margin-right:var(--space-1)}
  .header-stats .hs-down{color:var(--green)}
  .header-stats .hs-up{color:var(--yellow)}
  #refresh-counter{margin-left:auto;color:var(--muted);font-size:.786rem;white-space:nowrap;display:inline-flex;align-items:center;gap:var(--space-1)}
  #refresh-counter[aria-live="polite"]{ /* aria-live set in HTML */ }
  #error-banner{display:none;background:rgba(248,81,73,.1);border:1px solid var(--red);color:var(--red);padding:10px var(--space-4);font-size:.85rem}
  #error-banner.show{display:block}

  .grid-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--space-3);margin-top:var(--space-3)}
  .grid-2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-3)}
  .grid-1{display:grid;grid-template-columns:1fr;gap:var(--space-3)}
  .dashboard-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:var(--space-3);align-items:start;margin-top:var(--space-3)}
  .stack{display:flex;flex-direction:column;gap:var(--space-3);min-width:0}
  @media(max-width:1180px){.grid-3{grid-template-columns:repeat(2,minmax(0,1fr))}.dashboard-grid{grid-template-columns:1fr}}
  @media(max-width:760px){.grid-3,.grid-2{grid-template-columns:1fr}.header-stats{flex-direction:column;align-items:flex-start}#refresh-counter{margin-left:0}}

  /* Promote legacy .section-title / .card-title to heading-like semantics
     via ARIA (HTML tag is left alone to avoid touching JS string builders). */
  .section-title{font-size:.786rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--blue);padding-bottom:var(--space-1);border-bottom:1px solid rgba(88,166,255,.7);margin-top:var(--space-3)}
  .section-title:first-child{margin-top:0}
  .card .card-title{font-size:.714rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:var(--space-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

  .stat-row{display:flex;justify-content:space-between;align-items:center;gap:var(--space-2);padding:var(--space-1) 0;border-bottom:1px solid var(--border-soft);min-width:0}
  .stat-row:last-child{border-bottom:none}
  .stat-label{color:var(--muted);flex-shrink:0}
  .stat-value{font-weight:500;font-family:var(--font-mono);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}

  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:var(--space-1);flex-shrink:0}
  .dot-green{background:var(--green);box-shadow:0 0 6px var(--green)}
  .dot-red{background:var(--red)}

  .conn-list{list-style:none;max-height:190px;overflow:auto;margin:0;padding:0}
  .conn-list li{padding:var(--space-1) 0;border-bottom:1px solid var(--border-soft);font-family:var(--font-mono);font-size:.786rem;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .conn-list li:last-child{border-bottom:none}

  .compact-table{max-height:300px}
  .wide-table{max-height:430px}

  .btn-kill{background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.45);border-radius:var(--radius-sm);padding:4px 10px;font-size:.786rem;font-weight:600;cursor:pointer;min-height:var(--tap-min-sm)}
  .btn-kill:hover{background:rgba(248,81,73,.25);border-color:var(--red)}
  .btn-kill:disabled{opacity:.5;cursor:not-allowed}
  .btn-kill:focus{outline:none;box-shadow:var(--focus-ring)}

  /* Pause button for auto-refresh */
  .refresh-pause{background:transparent;border:1px solid var(--border);color:var(--text);border-radius:var(--radius-sm);padding:4px 10px;font-size:.786rem;cursor:pointer;min-height:var(--tap-min-sm)}
  .refresh-pause:hover{background:var(--surface-2)}
  .refresh-pause:focus{outline:none;box-shadow:var(--focus-ring)}
  .refresh-pause[aria-pressed="true"]{color:var(--yellow);border-color:var(--yellow)}

  @media(max-width:760px){.table-wrap{max-height:none}.conn-list{max-height:none}}
"""

_PANEL_BODY = """
<div class="panel-header-bar">
  <h1>Peers &amp; Estado</h1>
  <div class="header-stats" aria-label="Resumen rápido">
    <span class="hs"><span class="hs-label">Motor</span><span id="hdr-engine">—</span></span>
    <span class="hs"><span class="hs-label">Streams</span><span id="hdr-streams">—</span></span>
    <span class="hs"><span class="hs-label">Clientes</span><span id="hdr-clients">—</span></span>
    <span class="hs"><span class="hs-label">↓ Bajada</span><span class="hs-down" id="hdr-down">—</span></span>
    <span class="hs"><span class="hs-label">↑ Subida</span><span class="hs-up" id="hdr-up">—</span></span>
  </div>
  <span id="refresh-counter" aria-live="off" aria-hidden="true">actualizando…</span>
  <button type="button" class="refresh-pause" id="refresh-pause-btn" aria-pressed="false" aria-label="Pausar auto-refresh">⏸ Pausar</button>
</div>
<div id="error-banner" role="alert" aria-live="assertive">No se puede conectar con la API. Reintentando…</div>

<div class="section-title" role="heading" aria-level="2">Vista general</div>
<div class="grid-3">
  <div class="card" id="card-engine">
    <div class="card-title" role="heading" aria-level="3">Motor AceStream</div>
    <div id="engine-content"><div class="empty">Cargando…</div></div>
  </div>
  <div class="card" id="card-summary">
    <div class="card-title" role="heading" aria-level="3">Resumen operativo</div>
    <div id="summary-content"><div class="empty">Cargando…</div></div>
  </div>
  <div class="card" id="card-ip">
    <div class="card-title" role="heading" aria-level="3">Red pública</div>
    <div id="ip-content"><div class="empty">Cargando…</div></div>
  </div>
</div>

<div class="dashboard-grid">
  <div class="stack">
    <div class="section-title" role="heading" aria-level="2">Reproduciendo ahora</div>
    <div class="card">
      <div class="card-title" role="heading" aria-level="3">Streams activos · OpenAce + FFMPEG</div>
      <div id="now-playing"><div class="empty">Cargando…</div></div>
    </div>

    <div class="section-title" role="heading" aria-level="2">Peers P2P AceStream</div>
    <div class="card">
      <div class="card-title" role="heading" aria-level="3">Peers conectados</div>
      <div id="engine-peers-content"><div class="empty">Cargando…</div></div>
    </div>
  </div>

  <div class="stack">
    <div class="section-title" role="heading" aria-level="2">Conectividad</div>
    <div class="grid-1">
      <div class="card">
        <div class="card-title" role="heading" aria-level="3">Clientes HTTP · Puerto 8888</div>
        <ul class="conn-list" id="incoming-list"><li class="empty">Cargando…</li></ul>
      </div>
      <div class="card">
        <div class="card-title" role="heading" aria-level="3">Conexiones locales al motor</div>
        <div class="empty" style="font-style:normal;padding-top:0">Control OpenAce y playback FFMPEG.</div>
        <ul class="conn-list" id="outgoing-ace-list"><li class="empty">Cargando…</li></ul>
      </div>
      <div class="card">
        <div class="card-title" role="heading" aria-level="3">Conexiones externas salientes</div>
        <ul class="conn-list" id="outgoing-ext-list"><li class="empty">Cargando…</li></ul>
      </div>
    </div>

    <div class="section-title" role="heading" aria-level="2">Servicios añadidos</div>
    <div class="card">
      <div class="card-title" role="heading" aria-level="3">Plugins</div>
      <div id="plugins-content"><div class="empty">Cargando…</div></div>
    </div>
  </div>
</div>
"""

_PANEL_EXTRA_JS = r"""
const INTERVAL = 5000;
let countdown = INTERVAL / 1000;
let refreshInFlight = false;
let paused = false;  // toggled by the pause/resume button
let lastRenderJson = '';  // for diff-render: skip DOM updates if data unchanged
let firstSuccessfulRender = false;

function fmt_secs(s) {
  if (s === null || s === undefined) return '—';
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function fmt_speed(bps) {
  if (bps == null || bps === 0) return '—';
  if (bps < 1024) return bps + ' B/s';
  if (bps < 1048576) return (bps / 1024).toFixed(1) + ' KB/s';
  return (bps / 1048576).toFixed(2) + ' MB/s';
}

function fmt_percent(v) {
  if (v === null || v === undefined || v === '' || v === '?') return null;
  return String(v).endsWith('%') ? String(v) : String(v) + '%';
}

function badge(text, color) {
  return `<span class="badge badge-${color}">${text}</span>`;
}

const esc = window.esc || function(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;')
    .replace(/\//g,'&#47;');
};

/* ---------- sorting (state survives the 5s refresh) ---------- */
const SORTS = {
  plugins: { key: 'name',    type: 'text', dir: 1 },
  hls:     { key: 'full_id', type: 'text', dir: 1 },
  peers:   { key: 'ip',      type: 'text', dir: 1 },
};
let lastData = null;
function cmpVal(a, b, type) {
  if (type === 'num') {
    const na = (a == null || a === '') ? -Infinity : Number(a);
    const nb = (b == null || b === '') ? -Infinity : Number(b);
    return na - nb;
  }
  if (type === 'bool') return (a ? 1 : 0) - (b ? 1 : 0);
  return String(a == null ? '' : a).localeCompare(String(b == null ? '' : b), 'es', { numeric: true, sensitivity: 'base' });
}
function sortRows(arr, st) {
  const out = (arr || []).slice();
  if (st && st.key) out.sort((x, y) => st.dir * cmpVal(x[st.key], y[st.key], st.type));
  return out;
}
function sortableTh(table, key, type, label, style) {
  const st = SORTS[table];
  const aria = (st && st.key === key) ? ' aria-sort="' + (st.dir === 1 ? 'asc' : 'desc') + '"' : '';
  const styleAttr = style ? ' style="' + style + '"' : '';
  return '<th class="sortable" tabindex="0" data-table="' + table + '" data-key="' + key + '" data-type="' + type + '"' + aria + styleAttr + '>' + label + '</th>';
}

function normalizeStatusPayload(data) {
  if (!data || typeof data !== 'object') {
    throw new Error('Respuesta inválida de la API');
  }
  const conns = data.connections && typeof data.connections === 'object' ? data.connections : {};
  return {
    ...data,
    engine: data.engine && typeof data.engine === 'object' ? data.engine : { up: false, version: null },
    ip_info: data.ip_info && typeof data.ip_info === 'object' ? data.ip_info : {},
    hls_streams: Array.isArray(data.hls_streams) ? data.hls_streams : [],
    plugins: Array.isArray(data.plugins) ? data.plugins : [],
    engine_peers: Array.isArray(data.engine_peers) ? data.engine_peers : [],
    active_streams: Array.isArray(data.active_streams) ? data.active_streams : [],
    connections: {
      incoming: Array.isArray(conns.incoming) ? conns.incoming : [],
      outgoing_acestream: Array.isArray(conns.outgoing_acestream) ? conns.outgoing_acestream : [],
      outgoing_external: Array.isArray(conns.outgoing_external) ? conns.outgoing_external : [],
    },
  };
}

function render(data) {
  if (!data || typeof data !== 'object') {
    console.error('[peers] render() called with non-object data:', typeof data);
    return;
  }
  // Now playing + FFMPEG
  const streams = data.active_streams || [];
  const hlsById = new Map((data.hls_streams || []).map(s => [s.content_id, s]));
  const mergedStreams = streams.map(s => ({ ...s, ffmpeg: hlsById.get(s.content_id) || null }));
  for (const h of data.hls_streams || []) {
    if (!streams.some(s => s.content_id === h.content_id)) {
      mergedStreams.push({
        name: 'FFMPEG directo',
        content_id: h.content_id,
        format: 'hls',
        clients: 0,
        client_ips: [],
        started_at: Math.floor(Date.now() / 1000) - (h.idle_s || 0),
        ffmpeg: h,
      });
    }
  }
  if (!mergedStreams.length) {
    document.getElementById('now-playing').innerHTML = '<div class="empty">No hay streams reproduciéndose</div>';
  } else {
    document.getElementById('now-playing').innerHTML = `
      <div class="table-wrap wide-table"><table style="table-layout:auto">
        <thead><tr>
          <th>Canal</th>
          <th>Content ID</th>
          <th>Formato</th>
          <th>Clientes</th>
          <th>IPs</th>
          <th>Tiempo</th>
          <th>FFMPEG</th>
          <th>PID</th>
          <th>Inactividad</th>
          <th>Acciones</th>
        </tr></thead>
        <tbody>${mergedStreams.map(s => `
          <tr>
            <td><strong>${esc(s.name)}</strong></td>
            <td class="mono" style="font-size:11px;word-break:break-all;white-space:normal" title="${esc(s.content_id)}">${esc(s.content_id)}</td>
            <td>${badge(String(s.format || 'hls').toUpperCase(), s.format === 'hls' ? 'blue' : 'green')}</td>
            <td>${badge(s.clients || 0, s.clients ? 'green' : 'muted')}</td>
            <td class="mono" style="font-size:11px">${(s.client_ips||[]).map(ip => esc(ip)).join('<br>') || '—'}</td>
            <td class="mono">${fmt_secs(Math.max(0, Math.floor(Date.now()/1000 - (s.started_at || Date.now()/1000))))}</td>
            <td>${s.ffmpeg ? (s.ffmpeg.alive ? badge('VIVO','green') : badge('MUERTO','red')) : badge('NO','muted')}</td>
            <td class="mono">${s.ffmpeg ? esc(s.ffmpeg.pid) : '—'}</td>
            <td class="mono">${s.ffmpeg ? fmt_secs(s.ffmpeg.idle_s) : '—'}</td>
            <td>${s.ffmpeg ? `<button type="button" class="btn-kill" data-cid="${esc(s.content_id)}">Kill</button>` : '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>`;
  }

  // Engine
  const e = data.engine;
  function engineRow(label, value) {
    if (value === null || value === undefined || value === '' || value === '—') return '';
    return `<div class="stat-row"><span class="stat-label">${label}</span><span class="stat-value mono">${value}</span></div>`;
  }
  document.getElementById('engine-content').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Estado</span>
      <span class="stat-value">
        <span class="dot ${e.up ? 'dot-green' : 'dot-red'}"></span>${e.up ? badge('ONLINE','green') : badge('OFFLINE','red')}
      </span>
    </div>
    ${engineRow('Versión', esc(e.version))}
    ${engineRow('Plataforma', esc(e.platform))}
    ${engineRow('IP externa', esc(e.external_ip))}
    ${engineRow('↓ Bajada', fmt_speed(e.download_speed))}
    ${engineRow('↑ Subida', fmt_speed(e.upload_speed))}
    ${engineRow('Descargado', fmt_speed(e.downloaded))}
    ${engineRow('Subido', fmt_speed(e.uploaded))}
    ${engineRow('Peers máx.', e.max_peers)}
    ${engineRow('Peers activos', e.connected_peers_count)}
    ${engineRow('Slots subida', e.upload_slots)}
    ${engineRow('Conexiones máx.', e.max_connections)}
    ${engineRow('Uptime', fmt_secs(e.run_time))}
    ${engineRow('CPU', fmt_percent(e.cpu_percent))}
  `;

  // IP Info
  const ip = data.ip_info || {};
  document.getElementById('ip-content').innerHTML = ip.ip ? `
    <div class="stat-row"><span class="stat-label">IP</span><span class="stat-value mono" title="${esc(ip.ip)}">${esc(ip.ip)}</span></div>
    <div class="stat-row"><span class="stat-label">Hostname</span><span class="stat-value mono" style="font-size:11px" title="${esc(ip.hostname||'')}">${esc(ip.hostname || '—')}</span></div>
    <div class="stat-row"><span class="stat-label">Ciudad</span><span class="stat-value" title="${esc((ip.city||'—')+', '+(ip.region||''))}">${esc(ip.city || '—')}, ${esc(ip.region || '')}</span></div>
    <div class="stat-row"><span class="stat-label">País</span><span class="stat-value">${esc(ip.country || '—')}</span></div>
    <div class="stat-row"><span class="stat-label">Org / ISP</span><span class="stat-value mono" style="font-size:11px" title="${esc(ip.org||'')}">${esc(ip.org || '—')}</span></div>
    <div class="stat-row"><span class="stat-label">Timezone</span><span class="stat-value">${esc(ip.timezone || '—')}</span></div>` :
    '<div class="empty">No disponible</div>';

  // Summary
  const conns = data.connections;
  const peers = data.engine_peers || [];
  document.getElementById('summary-content').innerHTML = `
    <div class="stat-row"><span class="stat-label">Clientes conectados</span><span class="stat-value">${badge(conns.incoming.length,'blue')}</span></div>
    <div class="stat-row"><span class="stat-label">Conexiones al motor</span><span class="stat-value">${badge(conns.outgoing_acestream.length,'blue')}</span></div>
    <div class="stat-row"><span class="stat-label">Peers P2P</span><span class="stat-value">${badge(peers.length, peers.length ? 'green' : 'muted')}</span></div>
    <div class="stat-row"><span class="stat-label">Conexiones externas</span><span class="stat-value">${badge(conns.outgoing_external.length,'muted')}</span></div>
    <div class="stat-row"><span class="stat-label">Streams FFMPEG</span><span class="stat-value">${badge(data.hls_streams.length, data.hls_streams.length ? 'green' : 'muted')}</span></div>
    <div class="stat-row"><span class="stat-label">Plugins cargados</span><span class="stat-value">${badge(data.plugins.length,'blue')}</span></div>`;

  // Header totals
  document.getElementById('hdr-engine').innerHTML = e.up ? badge('ONLINE','green') : badge('OFFLINE','red');
  document.getElementById('hdr-streams').textContent = mergedStreams.length;
  document.getElementById('hdr-clients').textContent = conns.incoming.length;
  document.getElementById('hdr-down').textContent = fmt_speed(peers.reduce((s,p) => s + (p.download_speed || 0), 0));
  document.getElementById('hdr-up').textContent = fmt_speed(peers.reduce((s,p) => s + (p.upload_speed || 0), 0));

  // Connection lists
  function renderList(id, items, emptyMsg) {
    const ul = document.getElementById(id);
    if (!items.length) { ul.innerHTML = `<li class="empty">${emptyMsg}</li>`; return; }
    ul.innerHTML = items.map(x => `<li title="${esc(x)}">${esc(x)}</li>`).join('');
  }
  renderList('incoming-list',     conns.incoming,          'Sin clientes conectados');
  renderList('outgoing-ace-list', conns.outgoing_acestream,'Sin conexiones al motor');
  renderList('outgoing-ext-list', conns.outgoing_external, 'Sin conexiones externas');

  // Plugins
  const plugins = sortRows(data.plugins, SORTS.plugins);
  if (!plugins.length) {
    document.getElementById('plugins-content').innerHTML = '<div class="empty">No hay plugins cargados</div>';
  } else {
    document.getElementById('plugins-content').innerHTML = `
      <div class="table-wrap compact-table"><table style="table-layout:auto">
        <thead><tr>
          ${sortableTh('plugins','name','text','Plugin','width:40%')}
          ${sortableTh('plugins','channels','num','Canales','width:15%')}
          ${sortableTh('plugins','refresh_interval','num','Intervalo','width:20%')}
          ${sortableTh('plugins','last_refresh_s','num','Último refresco','width:25%')}
        </tr></thead>
        <tbody>${plugins.map(p => `
          <tr>
            <td title="${esc(p.name)} (${esc(p.slug)})"><strong>${esc(p.name)}</strong> <span class="mono" style="color:var(--muted);font-size:11px">${esc(p.slug)}</span></td>
            <td>${badge(p.channels, p.channels ? 'green' : 'yellow')}</td>
            <td class="mono">${fmt_secs(p.refresh_interval)}</td>
            <td class="mono">${p.last_refresh_s !== null ? 'hace ' + fmt_secs(p.last_refresh_s) : '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>`;
  }

  // Engine peers (real P2P peers from /app/monitor)
  const ep = sortRows(data.engine_peers, SORTS.peers);
  if (!ep.length) {
    document.getElementById('engine-peers-content').innerHTML = '<div class="empty">No hay peers P2P activos — reproduce un stream para verlos.</div>';
  } else {
    document.getElementById('engine-peers-content').innerHTML = `
      <div class="table-wrap">
      <table style="table-layout:auto">
        <thead><tr>
          ${sortableTh('peers','ip','text','IP','width:160px')}
          ${sortableTh('peers','external_port','num','Puerto','width:80px')}
          ${sortableTh('peers','country','text','País','width:60px')}
          ${sortableTh('peers','city','text','Ciudad','width:100px')}
          ${sortableTh('peers','org','text','Org / ISP')}
          ${sortableTh('peers','download_speed','num','↓ Bajada','width:100px')}
          ${sortableTh('peers','upload_speed','num','↑ Subida','width:100px')}
        </tr></thead>
        <tbody>${ep.map(p => `
          <tr>
            <td class="mono" style="font-size:11px" title="${esc(p.peer_id)}">${esc(p.ip)}</td>
            <td class="mono" style="font-size:11px">${esc(p.external_port || '—')}</td>
            <td style="font-size:12px">${esc(p.country)}</td>
            <td style="font-size:12px" title="${esc(p.city)}">${esc(p.city)}</td>
            <td style="font-size:12px" title="${esc(p.org)}">${esc(p.org)}</td>
            <td class="mono" style="font-size:11px">${fmt_speed(p.download_speed)}</td>
            <td class="mono" style="font-size:11px">${fmt_speed(p.upload_speed)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
      </div>`;
  }
}

async function refresh(force) {
  if (refreshInFlight) return;
  if (paused && !force) return;  // user has paused auto-refresh
  refreshInFlight = true;
  try {
    // Use shared fetchJSON with timeout (8s). Previously raw fetch could hang
    // forever if psutil/ss stuck, leaving the UI frozen and skipping all
    // future refreshes.
    let data = await fetchJSON('/api/peers/status', { cache: 'no-store' }, 8000);
    // fetchJSON may return a string if the Content-Type header was missing
    // or stripped (e.g. by a VPN proxy). Parse it to an object so render()
    // can access data.engine etc. without a TypeError.
    if (typeof data === 'string') {
      data = JSON.parse(data);
    }
    data = normalizeStatusPayload(data);
    lastData = data;
    // Diff-render: if the payload hasn't changed since last render, skip
    // re-writing innerHTML. This preserves scroll position and focus inside
    // the tables/peers lists during auto-refresh.
    const sig = JSON.stringify(data);
    if (sig !== lastRenderJson) {
      render(data);
      lastRenderJson = sig;
    }
    firstSuccessfulRender = true;
    const banner = document.getElementById('error-banner');
    banner.classList.remove('show');
    banner.style.display = 'none';
    banner.textContent = 'No se puede conectar con la API. Reintentando…';
  } catch(err) {
    console.error('[peers] refresh failed:', err);
    const banner = document.getElementById('error-banner');
    banner.textContent = 'No se puede conectar con la API: ' + (err && err.message ? err.message : err) + '. Reintentando…';
    banner.classList.add('show');
    banner.style.display = 'block';
  } finally {
    refreshInFlight = false;
    countdown = INTERVAL / 1000;
    updateRefreshCounter();
  }
}

function retryInitialRefresh(delayMs) {
  setTimeout(function(){ if (!firstSuccessfulRender) refresh(true); }, delayMs);
}

function updateRefreshCounter(){
  const el = document.getElementById('refresh-counter');
  if(!el) return;
  if(paused){
    el.textContent = 'pausado';
    return;
  }
  // Clamp countdown so we never display negatives during a slow refresh.
  el.textContent = 'Actualiza en ' + Math.max(0, countdown) + 's';
}

document.addEventListener('click', async e => {
  const killBtn = e.target.closest('.btn-kill');
  if (killBtn) {
    const cid = killBtn.dataset.cid;
    if (!cid || !confirm(`¿Matar el stream ${cid}?`)) return;
    killBtn.disabled = true;
    try {
      await fetchJSON('/api/peers/hls/' + encodeURIComponent(cid) + '/kill', { method: 'POST' }, 10000);
      toast('Stream detenido', 'success');
      await refresh(true);
    } catch (err) {
      toast('No se pudo detener el stream: ' + err.message, 'error');
      killBtn.disabled = false;
    }
    return;
  }

  const th = e.target.closest('th.sortable');
  if (!th) return;
  const st = SORTS[th.dataset.table];
  if (!st) return;
  const key = th.dataset.key;
  if (st.key === key) { st.dir *= -1; }
  else { st.key = key; st.type = th.dataset.type || 'text'; st.dir = 1; }
  if (lastData) render(lastData);
});

document.addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const th = e.target.closest('th.sortable');
  if (!th) return;
  e.preventDefault();
  th.click();
});

setInterval(() => {
  if (paused) return;  // don't decrement while paused
  countdown--;
  updateRefreshCounter();
  if (countdown <= 0) refresh();
}, 1000);

// Pause/resume control: stops the 5s polling when the user wants to inspect a row.
document.getElementById('refresh-pause-btn').addEventListener('click', function(){
  paused = !paused;
  this.setAttribute('aria-pressed', paused ? 'true' : 'false');
  this.textContent = paused ? '▶ Reanudar' : '⏸ Pausar';
  this.setAttribute('aria-label', paused ? 'Reanudar auto-refresh' : 'Pausar auto-refresh');
  updateRefreshCounter();
});

// Pause automatically when the tab is hidden (battery/CPU saving).
document.addEventListener('visibilitychange', function(){
  if (document.hidden) {
    // Save state so we only restore on return if user hadn't manually paused.
    if (!paused) { document.body.dataset.autoPaused = 'true'; paused = true; }
  } else if (document.body.dataset.autoPaused === 'true') {
    delete document.body.dataset.autoPaused;
    paused = false;
    const btn = document.getElementById('refresh-pause-btn');
    if (btn) {
      btn.setAttribute('aria-pressed', 'false');
      btn.textContent = '⏸ Pausar';
      btn.setAttribute('aria-label', 'Pausar auto-refresh');
    }
    refresh();  // immediate refresh on return
  }
});

retryInitialRefresh(0);
retryInitialRefresh(500);
retryInitialRefresh(1500);
retryInitialRefresh(3000);
"""


def _render_panel_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Peers",
        body=_PANEL_BODY,
        extra_css=_PANEL_HEAD_CSS,
        extra_js=_PANEL_EXTRA_JS,
        body_class="page-peers",
        active_nav="/peers",
        show_header=True,  # inherit unified app header (nav, logout, theme, lang)
        container_class="container",
        robots_noindex=True,
        description="Monitor de peers AceStream y streams activos de OpenAce",
    )


@panel_bp.route("/peers")
def peers_panel():
    return Response(_render_panel_html(), content_type="text/html; charset=utf-8")


_DASHBOARD_BODY = """
<div class="dashboard-wrap">
  <nav class="cards" aria-label="Secciones">
    <a class="shortcut peers" href="/peers">
      <span class="icon" aria-hidden="true">\U0001F310</span>
      <span class="label">Peers &amp; Estado</span>
      <span class="desc">Motor AceStream, conexiones P2P, streams HLS, plugins y red</span>
    </a>
    <a class="shortcut check" href="/check">
      <span class="icon" aria-hidden="true">\u26A1</span>
      <span class="label">Channel Checker</span>
      <span class="desc">Verificar canales, comprobación masiva, historial de resultados</span>
    </a>
    <a class="shortcut eula" href="/eula">
      <span class="icon" aria-hidden="true">\U0001F4DC</span>
      <span class="label">EULA</span>
      <span class="desc">Acuerdo de licencia, consentimiento y revocación</span>
    </a>
    <a class="shortcut plugins" href="/plugins">
      <span class="icon" aria-hidden="true">\U0001F3B6</span>
      <span class="label">Plugins</span>
      <span class="desc">Gestionar fuentes M3U, crear y editar plugins dinámicos</span>
    </a>
    <a class="shortcut admin" href="/admin/users" id="card-admin" hidden>
      <span class="icon" aria-hidden="true">\U0001F464</span>
      <span class="label">Usuarios</span>
      <span class="desc">Gestión de usuarios, roles y tokens API</span>
    </a>
  </nav>
</div>
"""

_DASHBOARD_EXTRA_CSS = """
.dashboard-wrap{display:flex;align-items:center;justify-content:center;padding:var(--space-6) var(--space-4)}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-4);max-width:720px;width:100%;padding:0;margin:0;border:0}
.shortcut{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:var(--space-5) var(--space-4);
  text-decoration:none;color:var(--text);
  display:flex;flex-direction:column;align-items:center;gap:var(--space-3);text-align:center;
  transition:border-color var(--transition),box-shadow var(--transition),transform var(--transition-fast);
  box-shadow:var(--shadow-md);
}
.shortcut:hover{text-decoration:none;border-color:var(--blue);box-shadow:var(--shadow-lg);transform:translateY(-2px)}
.shortcut:focus{outline:none;box-shadow:var(--focus-ring)}
.shortcut:active{transform:translateY(0)}
.shortcut .icon{font-size:36px;line-height:1}
.shortcut .label{font-size:1.143rem;font-weight:600;color:var(--text)}
.shortcut .desc{font-size:.85rem;color:var(--muted);line-height:1.5}
.shortcut.peers{border-top:3px solid var(--green)}
.shortcut.check{border-top:3px solid var(--yellow)}
.shortcut.eula{border-top:3px solid var(--blue)}
.shortcut.plugins{border-top:3px solid var(--purple)}
.shortcut.admin{border-top:3px solid var(--text)}
@media (max-width:600px){
  .cards{grid-template-columns:1fr;max-width:380px}
  .shortcut{padding:var(--space-4)}
}
"""

_DASHBOARD_EXTRA_JS = r"""
(async function(){
  try {
    const d = await fetchJSON('/api/auth/me', { cache: 'no-store' }, 8000);
    if (d && d.user && d.user.role === 'admin') {
      const card = document.getElementById('card-admin');
      if (card) card.hidden = false;
    }
  } catch(e) { /* non-fatal: card simply stays hidden */ }
})();
"""


def _render_dashboard_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Dashboard",
        body=_DASHBOARD_BODY,
        extra_css=_DASHBOARD_EXTRA_CSS,
        extra_js=_DASHBOARD_EXTRA_JS,
        body_class="page-dashboard",
        active_nav="/panel",
        show_header=True,
        container_class="",
        robots_noindex=True,
        description="Panel principal de OpenAce",
    )


@panel_bp.route("/panel")
def dashboard():
    return Response(_render_dashboard_html(), content_type="text/html; charset=utf-8")
