import ipaddress
import re
import subprocess
import threading
import time

import psutil
import requests
from flask import Blueprint, current_app, jsonify, render_template_string

from datetime import datetime, timezone

from app.utils import plugin_cache as _plugin_cache
from app.utils import plugin_store as _plugin_store
from app.utils import stream_registry
from app.routes import hls as hls_module

panel_bp = Blueprint("peers", __name__)

_ip_cache: dict = {"data": None, "ts": 0.0}
_ip_lock = threading.Lock()

# Per-IP geo cache: ip -> {"data": {...}, "ts": float}
_peer_cache: dict = {}
_peer_cache_lock = threading.Lock()

_speed_cache: dict = {}
_speed_cache_lock = threading.Lock()


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


def _get_peer_ip_info(ip: str) -> dict:
    """Fetch ipinfo.io geo data for a specific peer IP, cached for 1 hour."""
    with _peer_cache_lock:
        entry = _peer_cache.get(ip)
        if entry and time.time() - entry["ts"] < 3600:
            return entry["data"]
    try:
        r = requests.get(f"https://ipinfo.io/{ip}/json", timeout=5)
        r.raise_for_status()
        data = r.json()
    except Exception:
        data = {}
    with _peer_cache_lock:
        _peer_cache[ip] = {"data": data, "ts": time.time()}
    return data


def _get_socket_byte_counts() -> dict:
    """Parse ss -tin for per-socket bytes_sent / bytes_received."""
    result = {}
    try:
        out = subprocess.check_output(
            ["ss", "-tin"], text=True, timeout=5, stderr=subprocess.DEVNULL
        )
    except Exception:
        return result
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
    return result


def _get_acestream_peer_connections() -> list:
    """Return TCP connections belonging to any acestreamengine process, enriched with ipinfo geo."""
    peers = []
    seen_ips: set = set()
    ip_info_map: dict = {}

    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.info["name"] or ""
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "acestreamengine" not in name and "acestreamengine" not in cmdline:
                    continue
                for conn in proc.net_connections(kind="tcp"):
                    if not conn.raddr:
                        continue
                    rip = conn.raddr.ip
                    seen_ips.add(rip)
                    peers.append({
                        "pid": proc.pid,
                        "proto": "tcp",
                        "local": f"{conn.laddr.ip}:{conn.laddr.port}",
                        "remote": f"{rip}:{conn.raddr.port}",
                        "remote_ip": rip,
                        "remote_port": conn.raddr.port,
                        "state": conn.status,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.AccessDenied, PermissionError):
        pass

    # Enrich unique IPs (skip private/loopback/link-local to avoid pointless lookups)
    for ip in seen_ips:
        try:
            is_public = ipaddress.ip_address(ip).is_global
        except ValueError:
            is_public = False
        if is_public:
            ip_info_map[ip] = _get_peer_ip_info(ip)

    for peer in peers:
        geo = ip_info_map.get(peer["remote_ip"], {})
        peer["org"] = geo.get("org", "—")
        peer["city"] = geo.get("city", "—")
        peer["country"] = geo.get("country", "—")
        peer["timezone"] = geo.get("timezone", "—")
        peer["loc"] = geo.get("loc", "—")

    now = time.time()
    socket_stats = _get_socket_byte_counts()
    with _speed_cache_lock:
        for peer in peers:
            key = (peer["local"], peer["remote"])
            cur = socket_stats.get(key)
            prev = _speed_cache.get(key) if cur else None
            if cur and prev and now - prev["ts"] > 0:
                dt = now - prev["ts"]
                peer["download_speed"] = round(max(0, cur["recv"] - prev["recv"]) / dt)
                peer["upload_speed"] = round(max(0, cur["sent"] - prev["sent"]) / dt)
            else:
                peer["download_speed"] = None
                peer["upload_speed"] = None
            if cur:
                _speed_cache[key] = {"sent": cur["sent"], "recv": cur["recv"], "ts": now}
        stale = [k for k, v in _speed_cache.items() if now - v["ts"] > 30]
        for k in stale:
            del _speed_cache[k]

    return peers


def _get_engine_status(host, port):
    url = f"http://{host}:{port}/webui/api/service?method=get_version"
    try:
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            result = r.json().get("result", {})
            return {"up": True, "version": result.get("version", "?")}
    except Exception:
        pass
    return {"up": False, "version": None}


def _get_connections():
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
        pass
    return incoming, outgoing_ace, outgoing_ext


def _build_name_map():
    name_map = {}
    try:
        for plugin in _plugin_store.get_all():
            for ch in _plugin_cache.get_channels(plugin["id"]):
                ih = ch.get("infohash")
                if ih and ih not in name_map:
                    name_map[ih] = ch.get("name", "Desconocido")
    except Exception:
        pass
    return name_map


@panel_bp.route("/api/peers/status")
def api_status():
    host = current_app.config.get("ACESTREAM_HOST", "127.0.0.1")
    port = current_app.config.get("ACESTREAM_PORT", "6878")

    engine = _get_engine_status(host, port)
    ip_info = _get_ip_info()

    manager = hls_module._manager
    hls_streams = []
    if manager is not None:
        with manager._lock:
            for cid, sess in manager._streams.items():
                hls_streams.append({
                    "content_id": cid,
                    "full_id": cid,
                    "alive": sess.process.poll() is None,
                    "idle_s": round(sess.idle_seconds()),
                    "pid": sess.process.pid,
                })

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
        pass

    incoming, outgoing_ace, outgoing_ext = _get_connections()
    engine_peers = _get_acestream_peer_connections()

    name_map = _build_name_map()
    active_streams = []
    for s in stream_registry.get_active():
        cid = s["content_id"]
        s["name"] = name_map.get(cid, "Desconocido")
        active_streams.append(s)

    return jsonify({
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
    })


_PANEL_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce · Panel</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--blue); white-space: nowrap; }
  .header-stats { display: flex; gap: 16px; align-items: center; }
  .header-stats .hs { font-size: 12px; font-family: monospace; white-space: nowrap; }
  .header-stats .hs-label { color: var(--muted); margin-right: 4px; }
  .header-stats .hs-down { color: var(--green); }
  .header-stats .hs-up { color: var(--yellow); }
  #refresh-counter { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
  #error-banner { display: none; background: rgba(248,81,73,.1); border: 1px solid var(--red); color: var(--red); padding: 10px 24px; font-size: 13px; }

  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; padding: 20px 24px 0; }
  .grid-1 { display: grid; grid-template-columns: 1fr; gap: 16px; padding: 16px 24px 0; }
  @media (max-width: 960px) { .grid-3 { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 600px) { .grid-3 { grid-template-columns: 1fr; } }
  .pad-bottom { padding-bottom: 24px; }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; min-width: 0; overflow: hidden; }
  .card-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  .stat-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px solid var(--border); min-width: 0; }
  .stat-row:last-child { border-bottom: none; }
  .stat-label { color: var(--muted); flex-shrink: 0; }
  .stat-value { font-weight: 500; font-family: monospace; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap; flex-shrink: 0; }
  .badge-green  { background: rgba(63,185,80,.15);  color: var(--green); }
  .badge-red    { background: rgba(248,81,73,.15);  color: var(--red); }
  .badge-yellow { background: rgba(210,153,34,.15); color: var(--yellow); }
  .badge-blue   { background: rgba(88,166,255,.15); color: var(--blue); }
  .badge-muted  { background: rgba(139,148,158,.15); color: var(--muted); }

  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; flex-shrink: 0; }
  .dot-green { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot-red   { background: var(--red); }

  .section-title { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--blue); padding: 20px 24px 8px; border-bottom: 2px solid var(--blue); margin: 0 24px; }

  .conn-list { list-style: none; }
  .conn-list li { padding: 5px 0; border-bottom: 1px solid rgba(48,54,61,.4); font-family: monospace; font-size: 12px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .conn-list li:last-child { border-bottom: none; }

  .empty { color: var(--muted); font-size: 12px; font-style: italic; padding: 8px 0; }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 6px 8px; font-size: 11px; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  th[aria-sort]::after { font-size: 9px; margin-left: 4px; color: var(--blue); }
  th[aria-sort="asc"]::after { content: "▲"; }
  th[aria-sort="desc"]::after { content: "▼"; }
  td { padding: 7px 8px; border-bottom: 1px solid rgba(48,54,61,.5); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  .mono { font-family: monospace; font-size: 12px; }
</style>
</head>
<body>
<header>
  <a href="/panel" style="color:var(--muted);text-decoration:none;font-size:13px;">&larr; Dashboard</a>
  <h1>OpenAce · Peers</h1>
  <div class="header-stats">
    <span class="hs"><span class="hs-label">↓ Bajada</span><span class="hs-down" id="hdr-down">—</span></span>
    <span class="hs"><span class="hs-label">↑ Subida</span><span class="hs-up" id="hdr-up">—</span></span>
  </div>
  <span id="refresh-counter">actualizando…</span>
</header>
<div id="error-banner">No se puede conectar con la API. Reintentando…</div>

<div class="section-title">Reproduciendo ahora</div>
<div class="grid-1">
  <div class="card">
    <div class="card-title">Streams activos</div>
    <div id="now-playing"><div class="empty">Cargando…</div></div>
  </div>
</div>

<div class="section-title">Motor AceStream</div>
<div class="grid-3" style="grid-template-columns: 1fr 1fr;">
  <div class="card" id="card-engine">
    <div class="card-title">Estado del motor</div>
    <div id="engine-content"><div class="empty">Cargando…</div></div>
  </div>
  <div class="card" id="card-summary">
    <div class="card-title">Resumen</div>
    <div id="summary-content"><div class="empty">Cargando…</div></div>
  </div>
</div>
<div class="grid-3" style="grid-template-columns: 1fr 1fr;">
  <div class="card">
    <div class="card-title">Conexiones al motor · puerto 6878</div>
    <ul class="conn-list" id="outgoing-ace-list"><li class="empty">Cargando…</li></ul>
  </div>
  <div class="card">
    <div class="card-title">Conexiones externas salientes</div>
    <ul class="conn-list" id="outgoing-ext-list"><li class="empty">Cargando…</li></ul>
  </div>
</div>
<div class="grid-1">
  <div class="card">
    <div class="card-title">Peers P2P</div>
    <div id="engine-peers-content"><div class="empty">Cargando…</div></div>
  </div>
</div>

<div class="section-title">Proxy HTTP · Puerto 8888</div>
<div class="grid-1">
  <div class="card">
    <div class="card-title">Conexiones entrantes</div>
    <ul class="conn-list" id="incoming-list"><li class="empty">Cargando…</li></ul>
  </div>
</div>

<div class="section-title">Red</div>
<div class="grid-1">
  <div class="card" id="card-ip">
    <div class="card-title">IP pública (ipinfo.io)</div>
    <div id="ip-content"><div class="empty">Cargando…</div></div>
  </div>
</div>

<div class="section-title">Servicios añadidos</div>
<div class="grid-1">
  <div class="card">
    <div class="card-title">Plugins</div>
    <div id="plugins-content"><div class="empty">Cargando…</div></div>
  </div>
</div>
<div class="grid-1 pad-bottom">
  <div class="card">
    <div class="card-title">Streams HLS activos (FFmpeg)</div>
    <div id="hls-content"><div class="empty">Cargando…</div></div>
  </div>
</div>

<script>
const INTERVAL = 5000;
let countdown = INTERVAL / 1000;

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

function badge(text, color) {
  return `<span class="badge badge-${color}">${text}</span>`;
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ---------- sorting (state survives the 5s refresh) ---------- */
const SORTS = {
  plugins: { key: 'name',    type: 'text', dir: 1 },
  hls:     { key: 'full_id', type: 'text', dir: 1 },
  peers:   { key: 'state',   type: 'text', dir: 1 },
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
  return '<th class="sortable" data-table="' + table + '" data-key="' + key + '" data-type="' + type + '"' + aria + styleAttr + '>' + label + '</th>';
}

function render(data) {
  // Now playing
  const streams = data.active_streams || [];
  if (!streams.length) {
    document.getElementById('now-playing').innerHTML = '<div class="empty">No hay streams reproduciéndose</div>';
  } else {
    document.getElementById('now-playing').innerHTML = `
      <div class="table-wrap"><table style="table-layout:auto">
        <thead><tr>
          <th>Canal</th>
          <th>Content ID</th>
          <th>Formato</th>
          <th>Clientes</th>
          <th>Tiempo</th>
        </tr></thead>
        <tbody>${streams.map(s => `
          <tr>
            <td><strong>${esc(s.name)}</strong></td>
            <td class="mono" style="font-size:11px;word-break:break-all;white-space:normal">${esc(s.content_id)}</td>
            <td>${badge(s.format.toUpperCase(), s.format === 'hls' ? 'blue' : 'green')}</td>
            <td>${s.clients}</td>
            <td class="mono">${fmt_secs(Math.floor(Date.now()/1000 - s.started_at))}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>`;
  }

  // Engine
  const e = data.engine;
  document.getElementById('engine-content').innerHTML = `
    <div class="stat-row">
      <span class="stat-label">Estado</span>
      <span class="stat-value">
        <span class="dot ${e.up ? 'dot-green' : 'dot-red'}"></span>${e.up ? badge('ONLINE','green') : badge('OFFLINE','red')}
      </span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Versión</span>
      <span class="stat-value mono">${esc(e.version || '—')}</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Endpoint</span>
      <span class="stat-value mono">127.0.0.1:6878</span>
    </div>`;

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
    <div class="stat-row"><span class="stat-label">Streams HLS</span><span class="stat-value">${badge(data.hls_streams.length, data.hls_streams.length ? 'green' : 'muted')}</span></div>
    <div class="stat-row"><span class="stat-label">Plugins cargados</span><span class="stat-value">${badge(data.plugins.length,'blue')}</span></div>`;

  // Header speed totals
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
      <div class="table-wrap"><table style="table-layout:auto">
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

  // HLS Streams
  const hls = sortRows(data.hls_streams, SORTS.hls);
  if (!hls.length) {
    document.getElementById('hls-content').innerHTML = '<div class="empty">No hay streams HLS activos</div>';
  } else {
    document.getElementById('hls-content').innerHTML = `
      <div class="table-wrap"><table style="table-layout:auto">
        <thead><tr>
          ${sortableTh('hls','full_id','text','Content ID','width:55%')}
          ${sortableTh('hls','pid','num','PID','width:15%')}
          ${sortableTh('hls','alive','bool','Estado','width:15%')}
          ${sortableTh('hls','idle_s','num','Inactividad','width:15%')}
        </tr></thead>
        <tbody>${hls.map(s => `
          <tr>
            <td class="mono" title="${esc(s.full_id)}">${esc(s.content_id)}</td>
            <td class="mono">${s.pid}</td>
            <td>${s.alive ? badge('VIVO','green') : badge('MUERTO','red')}</td>
            <td class="mono">${fmt_secs(s.idle_s)}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>`;
  }

  // Engine peers
  const ep = sortRows(data.engine_peers, SORTS.peers);
  const stateColor = s => ({'ESTABLISHED':'green','TIME_WAIT':'yellow','CLOSE_WAIT':'yellow','LISTEN':'blue'}[s] || 'muted');
  if (!ep.length) {
    document.getElementById('engine-peers-content').innerHTML = '<div class="empty">No hay peers activos — ¿hay streams reproduciéndose?</div>';
  } else {
    document.getElementById('engine-peers-content').innerHTML = `
      <div class="table-wrap">
      <table style="table-layout:auto;min-width:900px">
        <thead><tr>
          ${sortableTh('peers','state','text','Estado','width:110px')}
          ${sortableTh('peers','local','text','Local','width:150px')}
          ${sortableTh('peers','remote','text','Remote','width:150px')}
          ${sortableTh('peers','org','text','Org / ISP')}
          ${sortableTh('peers','city','text','Ciudad','width:100px')}
          ${sortableTh('peers','country','text','País','width:50px')}
          ${sortableTh('peers','timezone','text','Timezone','width:130px')}
          ${sortableTh('peers','loc','text','Coords','width:110px')}
          ${sortableTh('peers','download_speed','num','↓ Bajada','width:100px')}
          ${sortableTh('peers','upload_speed','num','↑ Subida','width:100px')}
        </tr></thead>
        <tbody>${ep.map(p => `
          <tr>
            <td>${badge(p.state, stateColor(p.state))}</td>
            <td class="mono" style="font-size:11px" title="${esc(p.local)}">${esc(p.local)}</td>
            <td class="mono" style="font-size:11px" title="${esc(p.remote)}">${esc(p.remote)}</td>
            <td style="font-size:12px" title="${esc(p.org)}">${esc(p.org)}</td>
            <td style="font-size:12px" title="${esc(p.city)}">${esc(p.city)}</td>
            <td style="font-size:12px">${esc(p.country)}</td>
            <td style="font-size:11px;color:var(--muted)" title="${esc(p.timezone)}">${esc(p.timezone)}</td>
            <td class="mono" style="font-size:11px;color:var(--muted)">${esc(p.loc)}</td>
            <td class="mono" style="font-size:11px">${fmt_speed(p.download_speed)}</td>
            <td class="mono" style="font-size:11px">${fmt_speed(p.upload_speed)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
      </div>`;
  }
}

async function refresh() {
  try {
    const r = await fetch('/api/peers/status');
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    lastData = data;
    render(data);
    document.getElementById('error-banner').style.display = 'none';
  } catch(err) {
    document.getElementById('error-banner').style.display = 'block';
  }
  countdown = INTERVAL / 1000;
}

document.addEventListener('click', e => {
  const th = e.target.closest('th.sortable');
  if (!th) return;
  const st = SORTS[th.dataset.table];
  if (!st) return;
  const key = th.dataset.key;
  if (st.key === key) { st.dir *= -1; }
  else { st.key = key; st.type = th.dataset.type || 'text'; st.dir = 1; }
  if (lastData) render(lastData);
});

setInterval(() => {
  countdown--;
  document.getElementById('refresh-counter').textContent = `Actualiza en ${countdown}s`;
  if (countdown <= 0) refresh();
}, 1000);

refresh();
</script>
</body>
</html>"""


@panel_bp.route("/peers")
def peers_panel():
    return render_template_string(_PANEL_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce · Dashboard</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; min-height: 100vh; display: flex; flex-direction: column; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--blue); }
  header .subtitle { color: var(--muted); font-size: 13px; }
  .dashboard { flex: 1; display: flex; align-items: center; justify-content: center; padding: 40px 24px; }
  .cards { display: grid; grid-template-columns: repeat(2, 1fr); gap: 24px; max-width: 700px; width: 100%; }
  @media (max-width: 600px) { .cards { grid-template-columns: 1fr; max-width: 360px; } }
  .shortcut { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 32px 28px; text-decoration: none; color: var(--text); transition: border-color .2s, box-shadow .2s, transform .15s; display: flex; flex-direction: column; align-items: center; gap: 16px; text-align: center; }
  .shortcut:hover { border-color: var(--blue); box-shadow: 0 0 20px rgba(88,166,255,.15); transform: translateY(-2px); }
  .shortcut:active { transform: translateY(0); }
  .shortcut .icon { font-size: 40px; line-height: 1; }
  .shortcut .label { font-size: 18px; font-weight: 600; }
  .shortcut .desc { font-size: 13px; color: var(--muted); line-height: 1.5; }
  .shortcut.peers { border-top: 3px solid var(--green); }
  .shortcut.check { border-top: 3px solid var(--yellow); }
  .shortcut.eula { border-top: 3px solid var(--blue); }
  .shortcut.peers:hover { border-color: var(--green); box-shadow: 0 0 20px rgba(63,185,80,.15); }
  .shortcut.check:hover { border-color: var(--yellow); box-shadow: 0 0 20px rgba(210,153,34,.15); }
  .shortcut.eula:hover { border-color: var(--blue); box-shadow: 0 0 20px rgba(88,166,255,.15); }
  .shortcut.plugins { border-top: 3px solid var(--purple); }
  .shortcut.plugins:hover { border-color: var(--purple); box-shadow: 0 0 20px rgba(188,140,255,.15); }
</style>
</head>
<body>
<header>
  <h1>OpenAce</h1>
  <span class="subtitle">Dashboard</span>
</header>
<div class="dashboard">
  <div class="cards">
    <a class="shortcut peers" href="/peers">
      <div class="icon">&#127760;</div>
      <div class="label">Peers &amp; Estado</div>
      <div class="desc">Motor AceStream, conexiones P2P, streams HLS, plugins y red</div>
    </a>
    <a class="shortcut check" href="/check">
      <div class="icon">&#9889;</div>
      <div class="label">Channel Checker</div>
      <div class="desc">Verificar canales, comprobación masiva, historial de resultados</div>
    </a>
    <a class="shortcut eula" href="/eula">
      <div class="icon">&#128220;</div>
      <div class="label">EULA</div>
      <div class="desc">Acuerdo de licencia, consentimiento y revocación</div>
    </a>
    <a class="shortcut plugins" href="/plugins">
      <div class="icon">&#128268;</div>
      <div class="label">Plugins</div>
      <div class="desc">Gestionar fuentes M3U, crear y editar plugins dinámicos</div>
    </a>
  </div>
</div>
</body>
</html>"""


@panel_bp.route("/panel")
def dashboard():
    return render_template_string(_DASHBOARD_HTML)
