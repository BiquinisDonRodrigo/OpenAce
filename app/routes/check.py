from flask import Blueprint, Response, current_app, jsonify, request

from app.utils.m3u_parser import extract_infohash
from app.utils import plugin_cache as _plugin_cache
from app.utils import plugin_store as _plugin_store
from app.utils import check_store
from app.utils.acestream import CHECK_TIMEOUT_S, check_stream
from app.utils.check_runner import _engine_lock, runner
from app.utils.logging_utils import log_event

check_bp = Blueprint("check", __name__)
COMPONENT = "check"


def _collect_channels():
    seen = {}
    for plugin in _plugin_store.get_all():
        for ch in _plugin_cache.get_channels(plugin["id"]):
            infohash = ch.get("infohash")
            if not infohash or infohash in seen:
                continue
            seen[infohash] = {
                "infohash": infohash,
                "name": ch.get("name", "Unknown"),
                "plugin": plugin["display_name"],
                "group": ch.get("group_title", plugin["display_name"]),
            }
    return sorted(seen.values(), key=lambda c: c["name"].lower())


def _lookup_metadata(infohash):
    """Best-effort name/group/plugin for a hash: cache first, then live catalog."""
    cached = check_store.get_one(infohash)
    if cached and cached.get("name"):
        return cached["name"], cached.get("group"), cached.get("plugin")
    for channel in _collect_channels():
        if channel["infohash"] == infohash:
            return channel["name"], channel["group"], channel["plugin"]
    return None, None, None


@check_bp.route("/check")
def check_page():
    try:
        channels = _collect_channels()
        check_store.sync_catalog(channels)
        if channels:
            check_store.purge_stale({c["infohash"] for c in channels})
    except Exception as e:
        log_event("error", "check_catalog_sync_failed", COMPONENT, error=str(e))
    return Response(_CHECK_HTML, content_type="text/html; charset=utf-8")


@check_bp.route("/check/single", methods=["POST"])
def check_single():
    payload = request.get_json(silent=True) or {}
    infohash = extract_infohash(payload.get("value") or "")
    if not infohash:
        return jsonify({"error": "No se pudo extraer un ID válido de AceStream."}), 400

    name, group, plugin = _lookup_metadata(infohash)
    engine_url = current_app.config["ACESTREAM_ENGINE"]
    log_context = {"content_id": infohash, "check": True, "manual": True}

    # Serialize against the bulk runner so we never probe two hashes at once.
    with _engine_lock:
        result = check_stream(engine_url, infohash, timeout=CHECK_TIMEOUT_S,
                              component=COMPONENT, log_context=log_context)

    check_store.record_result(
        infohash, result["outcome"], result["response_ms"],
        result["peers"], result["speed"],
        name=name, group=group, plugin=plugin,
    )
    return jsonify({
        "infohash": infohash,
        "name": name,
        "group": group,
        "plugin": plugin,
        "status": result["outcome"],
        "response_ms": result["response_ms"],
        "peers": result["peers"],
        "speed": result["speed"],
    })


@check_bp.route("/check/start", methods=["POST"])
def check_start():
    payload = request.get_json(silent=True) or {}
    status = payload.get("status") or "all"
    plugin = payload.get("plugin") or "all"
    group = payload.get("group") or "all"

    try:
        channels = _collect_channels()
        check_store.sync_catalog(channels)
        if channels:
            check_store.purge_stale({c["infohash"] for c in channels})
    except Exception as e:
        log_event("error", "check_catalog_sync_failed", COMPONENT, error=str(e))

    targets = check_store.get_results(status=status, plugin=plugin, group=group)
    if not targets:
        return jsonify({"started": False, "total": 0, "reason": "empty"})

    engine_url = current_app.config["ACESTREAM_ENGINE"]
    if not runner.start(engine_url, targets):
        return jsonify({"started": False, "reason": "busy"}), 409
    return jsonify({"started": True, "total": len(targets)})


@check_bp.route("/check/stop", methods=["POST"])
def check_stop():
    runner.stop()
    return jsonify({"ok": True})


@check_bp.route("/check/status")
def check_status():
    return jsonify(runner.snapshot())


@check_bp.route("/check/results")
def check_results():
    status = request.args.get("status") or "all"
    plugin = request.args.get("plugin") or "all"
    group = request.args.get("group") or "all"
    return jsonify({
        "results": check_store.get_results(status=status, plugin=plugin, group=group),
        "plugins": check_store.distinct_plugins(),
        "groups": check_store.distinct_groups(),
    })


_CHECK_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce · Comprobador</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--blue); white-space: nowrap; }
  header a { color: var(--muted); text-decoration: none; font-size: 13px; }
  header a:hover { color: var(--blue); }

  .wrap { padding: 16px 24px 40px; display: flex; flex-direction: column; gap: 16px; margin: 0 auto; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .card-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 14px; }

  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  label.fld { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  input[type=text], input[type=search], select {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 6px; padding: 8px 10px; font-size: 13px; min-width: 160px;
  }
  input[type=text] { flex: 1; min-width: 240px; }
  button { background: var(--blue); color: #fff; border: none; border-radius: 6px; padding: 9px 16px; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  button:disabled { opacity: .5; cursor: default; }
  .btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-danger { background: var(--red); }
  .btn-mini { padding: 4px 10px; font-size: 12px; }

  .manual-result { margin-top: 12px; padding: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--bg); display: none; }
  .manual-result.show { display: block; }
  .kv { display: flex; flex-wrap: wrap; gap: 6px 20px; }
  .kv div { font-size: 13px; }
  .kv span.k { color: var(--muted); margin-right: 6px; }

  .progress-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
  .stat { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; }
  .stat .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  .stat .value { font-size: 20px; font-weight: 700; margin-top: 4px; }
  .bar { height: 8px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; margin: 12px 0; }
  .bar > div { height: 100%; background: var(--blue); width: 0; transition: width .3s; }
  .current { font-size: 13px; color: var(--muted); }
  .current strong { color: var(--text); }

  .mono { font-family: monospace; font-size: 12px; color: var(--muted); }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 12px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .badge-green  { background: rgba(63,185,80,.15);  color: var(--green); }
  .badge-red    { background: rgba(248,81,73,.15);  color: var(--red); }
  .badge-yellow { background: rgba(210,153,34,.15); color: var(--yellow); }
  .badge-blue   { background: rgba(88,166,255,.15); color: var(--blue); }
  .badge-muted  { background: rgba(139,148,158,.12); color: var(--muted); }

  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 9px 12px; font-size: 11px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--border); white-space: nowrap; position: sticky; top: 0; background: var(--surface); }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  th[aria-sort]::after { font-size: 9px; margin-left: 4px; color: var(--blue); }
  th[aria-sort="asc"]::after { content: "▲"; }
  th[aria-sort="desc"]::after { content: "▼"; }
  td { padding: 8px 12px; border-bottom: 1px solid rgba(48,54,61,.5); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: rgba(88,166,255,.04); }
  .col-num { text-align: right; white-space: nowrap; }
  .col-id { word-break: break-all; font-size: 11px; }
  .col-actions .act-group { display: flex; flex-direction: column; gap: 3px; }
  .empty { color: var(--muted); font-style: italic; padding: 24px; text-align: center; }
  .err { color: var(--red); font-size: 13px; }
  .btn-copy { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 3px 8px; font-size: 11px; font-weight: 500; border-radius: 4px; width: 100%; text-align: center; }
  .btn-copy:hover { color: var(--text); border-color: var(--blue); }
  .btn-copy.copied { color: var(--green); border-color: var(--green); }
</style>
</head>
<body>
<header>
  <a href="/panel">&larr; Dashboard</a>
  <h1>OpenAce · Comprobador</h1>
</header>

<div class="wrap">

  <!-- Bloque 1: Comprobación manual -->
  <div class="card">
    <div class="card-title">1 · Comprobación manual</div>
    <div class="row">
      <input type="text" id="manual-input" placeholder="Pega un ID, acestream://… o una URL con ?id=…" autocomplete="off">
      <button id="manual-btn">Comprobar</button>
    </div>
    <div class="manual-result" id="manual-result"></div>
  </div>

  <!-- Bloque 2: Comprobación rápida (masiva) -->
  <div class="card">
    <div class="card-title">2 · Comprobación masiva</div>
    <div class="row">
      <label class="fld">Plugin
        <select id="f-plugin"><option value="all">Todos</option></select>
      </label>
      <label class="fld">Grupo
        <select id="f-group"><option value="all">Todos</option></select>
      </label>
      <label class="fld">Estado
        <select id="f-status">
          <option value="all">Todos</option>
          <option value="unchecked">No comprobados</option>
          <option value="live">Vivos</option>
          <option value="dead">Caídos</option>
          <option value="timeout">Timeout</option>
          <option value="error">Error</option>
        </select>
      </label>
      <label class="fld">&nbsp;
        <div class="row">
          <button id="start-btn">▶ Iniciar</button>
          <button id="stop-btn" class="btn-danger" disabled>■ Parar</button>
        </div>
      </label>
    </div>
    <div class="err" id="start-msg" style="margin-top:10px;display:none"></div>
  </div>

  <!-- Bloque 3: Monitor de progreso -->
  <div class="card">
    <div class="card-title">3 · Monitor de progreso</div>
    <div class="bar"><div id="bar-fill"></div></div>
    <div class="current" id="current-line">En reposo.</div>
    <div class="progress-grid" style="margin-top:14px">
      <div class="stat"><div class="label">Progreso</div><div class="value" id="s-progress">0 / 0</div></div>
      <div class="stat"><div class="label">✅ Vivos</div><div class="value" style="color:var(--green)" id="s-live">0</div></div>
      <div class="stat"><div class="label">❌ Caídos</div><div class="value" style="color:var(--red)" id="s-dead">0</div></div>
      <div class="stat"><div class="label">⏱ Timeout</div><div class="value" style="color:var(--yellow)" id="s-timeout">0</div></div>
      <div class="stat"><div class="label">⚠ Error</div><div class="value" style="color:var(--yellow)" id="s-error">0</div></div>
      <div class="stat"><div class="label">⤼ Saltados</div><div class="value" style="color:var(--muted)" id="s-skipped">0</div></div>
    </div>
  </div>

  <!-- Bloque 4: Tabla de resultados -->
  <div class="card">
    <div class="card-title">4 · Resultados</div>
    <div class="row" style="margin-bottom:12px">
      <input type="search" id="table-filter" placeholder="Filtrar por nombre, grupo o ID…" autocomplete="off">
      <button class="btn-secondary btn-mini" id="reload-btn">↻ Recargar</button>
      <span class="mono" id="table-count"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th class="sortable" data-key="name" data-type="text">Canal</th>
          <th class="sortable" data-key="group" data-type="text">Grupo</th>
          <th class="sortable" data-key="plugin" data-type="text">Plugin</th>
          <th class="sortable" data-key="infohash" data-type="text">ID</th>
          <th class="sortable" data-key="status" data-type="status">Estado</th>
          <th class="sortable col-num" data-key="response_ms" data-type="num">Resp.</th>
          <th class="sortable" data-key="last_check" data-type="num">Última comp.</th>
          <th>Acción</th>
        </tr></thead>
        <tbody id="rows"><tr><td colspan="8" class="empty">Cargando…</td></tr></tbody>
      </table>
    </div>
  </div>

</div>

<script>
const STATUS_BADGE = {
  live:     '<span class="badge badge-green">✅ Vivo</span>',
  dead:     '<span class="badge badge-red">❌ Caído</span>',
  timeout:  '<span class="badge badge-yellow">⏱ Timeout</span>',
  error:    '<span class="badge badge-yellow">⚠ Error</span>',
  skipped:  '<span class="badge badge-muted">⤼ Saltado</span>',
};
const idleBadge = '<span class="badge badge-muted">—</span>';

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
  return Promise.resolve();
}
function statusBadge(s) { return STATUS_BADGE[s] || idleBadge; }
function fmtMs(ms) { return (ms == null) ? '—' : (ms >= 1000 ? (ms/1000).toFixed(1)+'s' : ms+'ms'); }
function relTime(ts) {
  if (!ts) return 'nunca';
  const d = Math.floor(Date.now()/1000) - ts;
  if (d < 60) return 'hace ' + d + 's';
  if (d < 3600) return 'hace ' + Math.floor(d/60) + 'm';
  if (d < 86400) return 'hace ' + Math.floor(d/3600) + 'h';
  return 'hace ' + Math.floor(d/86400) + 'd';
}

const $ = id => document.getElementById(id);
let CACHE = [];  // last loaded results

/* ---------- sorting ---------- */
const SORT = { key: 'name', type: 'text', dir: 1 };
const STATUS_ORDER = { live: 0, dead: 1, timeout: 2, error: 3, skipped: 4 };
function statusRank(s) { return (s in STATUS_ORDER) ? STATUS_ORDER[s] : 99; }
function cmpVal(a, b, type) {
  if (type === 'num') {
    const na = (a == null || a === '') ? -Infinity : Number(a);
    const nb = (b == null || b === '') ? -Infinity : Number(b);
    return na - nb;
  }
  if (type === 'status') return statusRank(a) - statusRank(b);
  return String(a == null ? '' : a).localeCompare(String(b == null ? '' : b), 'es', { numeric: true, sensitivity: 'base' });
}
function updateSortIndicators() {
  document.querySelectorAll('th.sortable').forEach(th => {
    if (th.dataset.key === SORT.key) th.setAttribute('aria-sort', SORT.dir === 1 ? 'asc' : 'desc');
    else th.removeAttribute('aria-sort');
  });
}

/* ---------- Bloque 4: results table ---------- */
function renderTable() {
  const q = $('table-filter').value.trim().toLowerCase();
  const rows = CACHE.filter(c => {
    if (!q) return true;
    return ((c.name||'') + ' ' + (c.group||'') + ' ' + c.infohash).toLowerCase().includes(q);
  });
  rows.sort((a, b) => SORT.dir * cmpVal(a[SORT.key], b[SORT.key], SORT.type));
  $('table-count').textContent = rows.length + ' / ' + CACHE.length;
  if (!rows.length) {
    $('rows').innerHTML = '<tr><td colspan="8" class="empty">Sin resultados. ¿Han cargado los plugins?</td></tr>';
    return;
  }
  $('rows').innerHTML = rows.map(c => {
    const h = esc(c.infohash);
    return `<tr data-infohash="${h}">
      <td>${esc(c.name) || '<span class="mono">'+h+'</span>'}</td>
      <td>${esc(c.group)}</td>
      <td>${esc(c.plugin)}</td>
      <td class="mono col-id">${h}</td>
      <td class="cell-status">${statusBadge(c.status)}</td>
      <td class="col-num cell-resp">${fmtMs(c.response_ms)}</td>
      <td class="mono cell-time">${relTime(c.last_check)}</td>
      <td class="col-actions"><div class="act-group">
        <button class="btn-secondary btn-mini row-check" data-h="${h}" style="width:100%">Comprobar</button>
        <button class="btn-copy" data-h="${h}" data-fmt="mpegts" title="Copiar enlace MPEG-TS">📋 MPEG-TS</button>
        <button class="btn-copy" data-h="${h}" data-fmt="hls" title="Copiar enlace HLS">📋 HLS</button>
      </div></td>
    </tr>`;
  }).join('');
}

async function loadResults() {
  try {
    const r = await fetch('/check/results');
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    CACHE = data.results || [];
    syncDropdown('f-plugin', data.plugins);
    syncDropdown('f-group', data.groups);
    renderTable();
  } catch (e) {
    $('rows').innerHTML = '<tr><td colspan="8" class="empty err">No se pudieron cargar los resultados.</td></tr>';
  }
}

function syncDropdown(id, values) {
  const sel = $(id);
  const cur = sel.value;
  sel.innerHTML = '<option value="all">Todos</option>' +
    (values || []).map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function updateRow(res) {
  const tr = $('rows').querySelector(`tr[data-infohash="${CSS.escape(res.infohash)}"]`);
  if (!tr) return;
  tr.querySelector('.cell-status').innerHTML = statusBadge(res.status);
  tr.querySelector('.cell-resp').textContent = fmtMs(res.response_ms);
  tr.querySelector('.cell-time').textContent = relTime(Math.floor(Date.now()/1000));
  const c = CACHE.find(x => x.infohash === res.infohash);
  if (c) { c.status = res.status; c.response_ms = res.response_ms; c.last_check = Math.floor(Date.now()/1000); }
}

$('table-filter').addEventListener('input', renderTable);
$('reload-btn').addEventListener('click', loadResults);
document.querySelector('table thead').addEventListener('click', e => {
  const th = e.target.closest('th.sortable');
  if (!th) return;
  const key = th.dataset.key;
  if (SORT.key === key) { SORT.dir *= -1; }
  else { SORT.key = key; SORT.type = th.dataset.type || 'text'; SORT.dir = 1; }
  updateSortIndicators();
  renderTable();
});
$('rows').addEventListener('click', e => {
  const btn = e.target.closest('.row-check');
  if (btn) singleCheck(btn.dataset.h, btn);
  const copyBtn = e.target.closest('.btn-copy');
  if (copyBtn) {
    const h = copyBtn.dataset.h;
    const fmt = copyBtn.dataset.fmt;
    const url = location.origin + '/play/' + fmt + '/' + h;
    copyText(url).then(() => {
      copyBtn.classList.add('copied');
      const orig = copyBtn.textContent;
      copyBtn.textContent = '✓ Copiado';
      setTimeout(() => { copyBtn.textContent = orig; copyBtn.classList.remove('copied'); }, 1500);
    }).catch(() => {
      const orig = copyBtn.textContent;
      copyBtn.textContent = '✗ Error';
      setTimeout(() => { copyBtn.textContent = orig; }, 1500);
    });
  }
});

/* ---------- Bloque 1: manual check ---------- */
async function singleCheck(value, btn) {
  const box = $('manual-result');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  if (!btn) {
    box.className = 'manual-result show';
    box.innerHTML = '<span class="mono">Comprobando…</span>';
  }
  try {
    const r = await fetch('/check/single', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.status);
    if (!btn) {
      box.className = 'manual-result show';
      box.innerHTML = `<div class="kv">
        <div><span class="k">ID</span><span class="mono">${esc(data.infohash)}</span></div>
        <div><span class="k">Canal</span>${esc(data.name) || '—'}</div>
        <div><span class="k">Estado</span>${statusBadge(data.status)}</div>
        <div><span class="k">Respuesta</span>${fmtMs(data.response_ms)}</div>
        <div><span class="k">Peers</span>${data.peers ?? 0}</div>
      </div>`;
    }
    updateRow(data);
  } catch (e) {
    if (!btn) { box.className = 'manual-result show'; box.innerHTML = '<span class="err">' + esc(e.message) + '</span>'; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Comprobar'; }
  }
}
$('manual-btn').addEventListener('click', () => {
  const v = $('manual-input').value.trim();
  if (v) singleCheck(v, null);
});
$('manual-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('manual-btn').click(); });

/* ---------- Bloque 2 + 3: bulk run + progress ---------- */
let pollTimer = null;

function renderStatus(s) {
  const c = s.counters || {};
  $('s-progress').textContent = (s.done || 0) + ' / ' + (s.total || 0);
  $('s-live').textContent = c.live || 0;
  $('s-dead').textContent = c.dead || 0;
  $('s-timeout').textContent = c.timeout || 0;
  $('s-error').textContent = c.error || 0;
  $('s-skipped').textContent = c.skipped || 0;
  const pct = s.total ? Math.round((s.done || 0) / s.total * 100) : 0;
  $('bar-fill').style.width = pct + '%';
  if (s.running && s.current) {
    $('current-line').innerHTML = `Comprobando <strong>${esc(s.current.name) || esc(s.current.infohash)}</strong>` +
      (s.current.group ? ' · ' + esc(s.current.group) : '') +
      ' · <span class="mono">' + esc(s.current.infohash) + '</span>';
  } else if (!s.running && s.finished_at) {
    $('current-line').textContent = 'Finalizado.';
  } else if (!s.running) {
    $('current-line').textContent = 'En reposo.';
  }
  setRunningUI(s.running);
}

function setRunningUI(running) {
  $('start-btn').disabled = running;
  $('stop-btn').disabled = !running;
  ['f-plugin','f-group','f-status'].forEach(id => { $(id).disabled = running; });
}

async function poll() {
  try {
    const r = await fetch('/check/status');
    const s = await r.json();
    renderStatus(s);
    if (!s.running) { clearInterval(pollTimer); pollTimer = null; loadResults(); }
  } catch (e) { /* keep polling */ }
}

$('start-btn').addEventListener('click', async () => {
  const msg = $('start-msg');
  msg.style.display = 'none';
  const body = { plugin: $('f-plugin').value, group: $('f-group').value, status: $('f-status').value };
  try {
    const r = await fetch('/check/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await r.json();
    if (r.status === 409) { msg.textContent = 'Ya hay una comprobación en curso.'; msg.style.display = 'block'; return; }
    if (!data.started) {
      msg.textContent = data.reason === 'empty' ? 'Ningún canal coincide con el filtro.' : 'No se pudo iniciar.';
      msg.style.display = 'block';
      return;
    }
    setRunningUI(true);
    if (!pollTimer) pollTimer = setInterval(poll, 1000);
    poll();
  } catch (e) { msg.textContent = 'Error al iniciar.'; msg.style.display = 'block'; }
});

$('stop-btn').addEventListener('click', async () => {
  $('stop-btn').disabled = true;
  try { await fetch('/check/stop', { method: 'POST' }); } catch (e) {}
});

/* ---------- init ---------- */
updateSortIndicators();
loadResults();
poll();  // pick up an already-running job if the page was reopened
</script>
</body>
</html>"""
