from flask import Blueprint, Response, current_app, jsonify, request

from app.utils.m3u_parser import extract_infohash
from app.utils import plugin_cache as _plugin_cache
from app.utils import plugin_store as _plugin_store
from app.utils import check_store
from app.utils.acestream import CHECK_TIMEOUT_S, check_stream
from app.utils.auth_helpers import get_json_body
from app.utils.check_runner import _engine_semaphore, runner
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
    return Response(_render_check_html(), content_type="text/html; charset=utf-8")


@check_bp.route("/check/single", methods=["POST"])
def check_single():
    payload, jerr = get_json_body()
    if jerr:
        return jerr
    infohash = extract_infohash(payload.get("value") or "")
    if not infohash:
        return jsonify({"error": "No se pudo extraer un ID válido de AceStream."}), 400

    name, group, plugin = _lookup_metadata(infohash)
    engine_url = current_app.config["ACESTREAM_ENGINE"]
    log_context = {"content_id": infohash, "check": True, "manual": True}

    # Coordinate with the bulk runner via the shared semaphore.
    with _engine_semaphore:
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
    payload, jerr = get_json_body()
    if jerr:
        return jerr
    status = payload.get("status") or "all"
    plugin = payload.get("plugin") or "all"
    group = payload.get("group") or "all"
    _valid_statuses = {"all", "unchecked", "live", "dead", "timeout", "error", "skipped"}
    if status not in _valid_statuses:
        return jsonify({"error": "status inválido"}), 400

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
    try:
        if not runner.start(engine_url, targets):
            return jsonify({"started": False, "reason": "busy"}), 409
    except RuntimeError as e:
        log_event("error", "check_start_thread_error", COMPONENT, error=str(e))
        return jsonify({"started": False, "reason": "thread_error", "error": str(e)}), 500
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


_CHECK_EXTRA_CSS = r"""
  .check-wrap { width: min(100%, 1680px); padding: 16px 0 32px; display: flex; flex-direction: column; gap: 16px; margin: 0 auto; }
  .top-grid { display: grid; grid-template-columns: minmax(320px, 1.15fr) minmax(320px, 1fr) minmax(420px, 1.55fr); gap: 16px; align-items: stretch; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; min-width: 0; box-shadow: 0 12px 30px rgba(1,4,9,.14); }
  .card.results-card { padding-bottom: 10px; }
  .card-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 12px; }

  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .fld { flex: 1 1 150px; display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
  input[type=text], input[type=search], select {
    width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 7px; padding: 8px 10px; font-size: 13px; min-width: 0;
  }
  input[type=text] { flex: 1 1 260px; }
  input:focus, select:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px rgba(88,166,255,.08); }
  button { background: var(--blue); color: #fff; border: none; border-radius: 7px; padding: 9px 14px; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  button:disabled { opacity: .5; cursor: default; }
  .btn-secondary { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-danger { background: var(--red); }
  .btn-mini { padding: 5px 10px; font-size: 12px; }

  .manual-result { margin-top: 12px; padding: 10px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg); display: none; }
  .manual-result.show { display: block; }
  .kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px 14px; }
  .kv div { font-size: 13px; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .kv span.k { color: var(--muted); margin-right: 6px; }

  .progress-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }
  .stat { background: var(--bg); border: 1px solid var(--border); border-radius: 7px; padding: 9px 10px; min-width: 0; }
  .stat .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat .value { font-size: 19px; font-weight: 700; margin-top: 3px; }
  .bar { height: 8px; background: var(--bg); border: 1px solid var(--border); border-radius: 999px; overflow: hidden; margin: 10px 0; }
  .bar > div { height: 100%; background: linear-gradient(90deg, var(--blue), var(--green)); width: 0; transition: width .3s; }
  .current { font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .current strong { color: var(--text); }

  .mono { font-family: monospace; font-size: 12px; color: var(--muted); }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .badge-green  { background: rgba(63,185,80,.15);  color: var(--green); }
  .badge-red    { background: rgba(248,81,73,.15);  color: var(--red); }
  .badge-yellow { background: rgba(210,153,34,.15); color: var(--yellow); }
  .badge-blue   { background: rgba(88,166,255,.15); color: var(--blue); }
  .badge-muted  { background: rgba(139,148,158,.12); color: var(--muted); }

  .table-wrap { overflow: auto; max-height: calc(100vh - 330px); border: 1px solid var(--border); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 1305px; }
  .w-channel { width: 250px; }
  .w-group { width: 130px; }
  .w-plugin { width: 90px; }
  .w-id { width: 300px; }
  .w-status { width: 90px; }
  .w-response { width: 80px; }
  .w-last { width: 120px; }
  .w-actions { width: 245px; }
  th { text-align: left; padding: 8px 10px; font-size: 10px; color: var(--muted); font-weight: 700; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--border); white-space: nowrap; position: sticky; top: 0; z-index: 1; background: var(--surface); }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover, th.sortable:focus { color: var(--text); outline: none; box-shadow: inset 0 0 0 2px var(--blue); }
  th[aria-sort]::after { font-size: 9px; margin-left: 4px; color: var(--blue); }
  th[aria-sort="asc"]::after { content: "▲"; }
  th[aria-sort="desc"]::after { content: "▼"; }
  td { padding: 7px 10px; border-bottom: 1px solid rgba(48,54,61,.5); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: rgba(88,166,255,.04); }
  .col-num { text-align: right; white-space: nowrap; }
  .col-id { word-break: break-all; white-space: normal; font-size: 11px; }
  .col-actions { overflow: visible; }
  .col-actions .act-group { display: flex; flex-direction: row; gap: 4px; align-items: center; min-width: 0; }
  .col-actions .btn-mini, .col-actions .btn-copy { flex: 0 0 auto; }
  .empty { color: var(--muted); font-style: italic; padding: 24px; text-align: center; }
  .err { color: var(--red); font-size: 13px; }
  .btn-copy { background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 3px 8px; font-size: 11px; font-weight: 500; border-radius: 5px; width: auto; text-align: center; }
  .btn-copy:hover { color: var(--text); border-color: var(--blue); }
  .btn-copy.copied { color: var(--green); border-color: var(--green); }
  @media (max-width: 1320px) { .top-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .top-grid .progress-card { grid-column: 1 / -1; } }
  @media (max-width: 780px) { .check-wrap { padding-left: 14px; padding-right: 14px; } .top-grid { grid-template-columns: 1fr; } .top-grid .progress-card { grid-column: auto; } .progress-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .table-wrap { max-height: none; } }
"""


_CHECK_BODY = r"""
<div class="check-wrap">

  <div class="top-grid">
    <div class="card">
      <h2 class="card-title">Comprobación manual</h2>
      <div class="row">
        <label class="sr-only" for="manual-input">ID, enlace AceStream o URL</label>
        <input type="text" id="manual-input" placeholder="Pega un ID, acestream://… o URL con ?id=…" autocomplete="off">
        <button id="manual-btn" type="button">Comprobar</button>
      </div>
      <div class="manual-result" id="manual-result" role="status" aria-live="polite"></div>
    </div>

    <div class="card">
      <h2 class="card-title">Comprobación masiva</h2>
      <div class="row">
        <div class="fld"><label for="f-plugin">Plugin</label>
          <select id="f-plugin"><option value="all">Todos</option></select>
        </div>
        <div class="fld"><label for="f-group">Grupo</label>
          <select id="f-group"><option value="all">Todos</option></select>
        </div>
        <div class="fld"><label for="f-status">Estado</label>
          <select id="f-status">
            <option value="all">Todos</option>
            <option value="unchecked">No comprobados</option>
            <option value="live">Vivos</option>
            <option value="dead">Caídos</option>
            <option value="timeout">Timeout</option>
            <option value="error">Error</option>
          </select>
        </div>
        <div class="row" style="flex:1 1 100%;justify-content:flex-end">
          <button id="start-btn" type="button">Iniciar</button>
          <button id="stop-btn" class="btn-danger" type="button" disabled>Parar</button>
        </div>
      </div>
      <div class="err" id="start-msg" role="alert" aria-live="assertive" style="margin-top:10px;display:none"></div>
    </div>

    <div class="card progress-card">
      <h2 class="card-title">Monitor de progreso</h2>
      <div class="bar" role="progressbar" aria-label="Progreso de comprobación" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="bar-fill"></div></div>
      <div class="current" id="current-line" role="status" aria-live="polite">En reposo.</div>
      <div class="progress-grid" style="margin-top:12px">
        <div class="stat"><div class="label">Progreso</div><div class="value" id="s-progress">0 / 0</div></div>
        <div class="stat"><div class="label">Vivos</div><div class="value" style="color:var(--green)" id="s-live">0</div></div>
        <div class="stat"><div class="label">Caídos</div><div class="value" style="color:var(--red)" id="s-dead">0</div></div>
        <div class="stat"><div class="label">Timeout</div><div class="value" style="color:var(--yellow)" id="s-timeout">0</div></div>
        <div class="stat"><div class="label">Error</div><div class="value" style="color:var(--yellow)" id="s-error">0</div></div>
        <div class="stat"><div class="label">Saltados</div><div class="value" style="color:var(--muted)" id="s-skipped">0</div></div>
      </div>
    </div>
  </div>

  <div class="card results-card">
    <h2 class="card-title">Resultados</h2>
    <div class="row" style="margin-bottom:12px">
      <label class="sr-only" for="table-filter">Filtrar resultados</label>
      <input type="search" id="table-filter" placeholder="Filtrar por nombre, grupo o ID…" autocomplete="off">
      <button class="btn-secondary btn-mini" id="reload-btn" type="button">↻ Recargar</button>
      <span class="mono" id="table-count" aria-live="polite"></span>
    </div>
    <div class="table-wrap">
      <table>
        <caption class="sr-only">Resultados de comprobación de canales AceStream</caption>
        <colgroup>
          <col class="w-channel">
          <col class="w-group">
          <col class="w-plugin">
          <col class="w-id">
          <col class="w-status">
          <col class="w-response">
          <col class="w-last">
          <col class="w-actions">
        </colgroup>
        <thead><tr>
          <th scope="col" class="sortable" data-key="name" data-type="text" tabindex="0">Canal</th>
          <th scope="col" class="sortable" data-key="group" data-type="text" tabindex="0">Grupo</th>
          <th scope="col" class="sortable" data-key="plugin" data-type="text" tabindex="0">Plugin</th>
          <th scope="col" class="sortable" data-key="infohash" data-type="text" tabindex="0">ID</th>
          <th scope="col" class="sortable" data-key="status" data-type="status" tabindex="0">Estado</th>
          <th scope="col" class="sortable col-num" data-key="response_ms" data-type="num" tabindex="0">Resp.</th>
          <th scope="col" class="sortable" data-key="last_check" data-type="num" tabindex="0">Última comp.</th>
          <th scope="col">Acción</th>
        </tr></thead>
        <tbody id="rows"><tr><td colspan="8" class="empty">Cargando…</td></tr></tbody>
      </table>
    </div>
  </div>

</div>

"""


_CHECK_EXTRA_JS = r"""
const STATUS_BADGE = {
  live:     '<span class="badge badge-green">✅ Vivo</span>',
  dead:     '<span class="badge badge-red">❌ Caído</span>',
  timeout:  '<span class="badge badge-yellow">⏱ Timeout</span>',
  error:    '<span class="badge badge-yellow">⚠ Error</span>',
  skipped:  '<span class="badge badge-muted">⤼ Saltado</span>',
};
const idleBadge = '<span class="badge badge-muted">—</span>';

const esc = window.esc;
function copyText(text) {
  return window.copyToClipboard(text).then(function(ok){ if(!ok) throw new Error('copy_failed'); });
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
        <button class="btn-secondary btn-mini row-check" data-h="${h}" type="button">Comprobar</button>
        <button class="btn-copy" data-h="${h}" data-fmt="mpegts" title="Copiar enlace MPEG-TS" type="button">📋 MPEG-TS</button>
        <button class="btn-copy" data-h="${h}" data-fmt="hls" title="Copiar enlace HLS" type="button">📋 HLS</button>
      </div></td>
    </tr>`;
  }).join('');
}

async function loadResults() {
  try {
    const data = await fetchJSON('/check/results', { cache: 'no-store' }, 12000);
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
  const tr = $('rows').querySelector('tr[data-infohash="' + esc(res.infohash) + '"]');
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
  applySort(th);
});
document.querySelector('table thead').addEventListener('keydown', e => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const th = e.target.closest('th.sortable');
  if (!th) return;
  e.preventDefault();
  applySort(th);
});
function applySort(th) {
  const key = th.dataset.key;
  if (SORT.key === key) { SORT.dir *= -1; }
  else { SORT.key = key; SORT.type = th.dataset.type || 'text'; SORT.dir = 1; }
  updateSortIndicators();
  renderTable();
}
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
  // Track in-flight state per-value so we never fire concurrent checks for
  // the same input (manual button was previously never disabled → races).
  if (btn) { btn.disabled = true; btn.dataset.originalText = btn.textContent; btn.textContent = '…'; }
  if (!btn) {
    $('manual-btn').disabled = true;
    box.className = 'manual-result show';
    box.innerHTML = '<span class="mono">Comprobando…</span>';
  }
  try {
    const data = await fetchJSON('/check/single', { method: 'POST', body: { value } }, 20000);
    if (!btn) {
      box.className = 'manual-result show';
      box.innerHTML = `<div class="kv">
        <div><span class="k">ID</span><span class="mono">${esc(data.infohash)}</span></div>
        <div><span class="k">Canal</span>${esc(data.name) || '—'}</div>
        <div><span class="k">Estado</span>${statusBadge(data.status)}</div>
        <div><span class="k">Respuesta</span>${fmtMs(data.response_ms)}</div>
        <div><span class="k">Peers</span>${data.peers == null ? 0 : data.peers}</div>
      </div>`;
    }
    updateRow(data);
  } catch (e) {
    if (!btn) {
      box.className = 'manual-result show';
      box.innerHTML = '<span class="err">' + esc(e.message) + '</span>';
    } else {
      // C5 fix: previously when singleCheck was invoked from a row button
      // (btn truthy) the catch did nothing — the user saw the button label
      // flip back to "Comprobar" with no indication that the request failed.
      // Now we patch the row's status cell inline.
      const tr = btn.closest('tr');
      if (tr) {
        const cell = tr.querySelector('.cell-status');
        if (cell) {
          cell.innerHTML = '<span class="badge badge-red" title="' + esc(e.message) + '">⚠ Error</span>';
        }
      }
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = btn.dataset.originalText || 'Comprobar';
      delete btn.dataset.originalText;
    } else {
      $('manual-btn').disabled = false;
    }
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
  document.querySelector('.bar').setAttribute('aria-valuenow', String(pct));
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
    const s = await fetchJSON('/check/status', { cache: 'no-store' }, 8000);
    renderStatus(s);
    if (s.running) {
      // C4 fix: if a bulk run is already in progress (e.g. user reopened the
      // page mid-run), ensure we have an interval ticking. Previously the
      // init poll() fetched a snapshot but never scheduled follow-ups,
      // freezing the UI.
      if (!pollTimer) {
        pollTimer = setInterval(poll, 1000);
        setRunningUI(true);
      }
    } else {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      setRunningUI(false);
      loadResults();
    }
  } catch (e) {
    // Network/HTTP failure: keep polling if we already have a timer, but
    // surface the error in the progress card instead of swallowing silently.
    if (pollTimer) {
      const cur = $('current-line');
      if (cur) cur.textContent = 'Conexión perdida, reintentando…';
    }
  }
}

$('start-btn').addEventListener('click', async () => {
  const msg = $('start-msg');
  msg.style.display = 'none';
  const body = { plugin: $('f-plugin').value, group: $('f-group').value, status: $('f-status').value };
  try {
    const data = await fetchJSON('/check/start', { method: 'POST', body }, 12000);
    if (!data.started) {
      msg.textContent = data.reason === 'empty' ? 'Ningún canal coincide con el filtro.' : 'No se pudo iniciar.';
      msg.style.display = 'block';
      return;
    }
    setRunningUI(true);
    if (!pollTimer) pollTimer = setInterval(poll, 1000);
    poll();
  } catch (e) {
    msg.textContent = e.status === 409 ? 'Ya hay una comprobación en curso.' : 'Error al iniciar.';
    msg.style.display = 'block';
  }
});

$('stop-btn').addEventListener('click', async () => {
  $('stop-btn').disabled = true;
  try { await fetchJSON('/check/stop', { method: 'POST' }, 8000); } catch (e) {}
});

/* ---------- init ---------- */
updateSortIndicators();
loadResults();
poll();  // pick up an already-running job if the page was reopened
"""


def _render_check_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Comprobador",
        body=_CHECK_BODY,
        extra_css=_CHECK_EXTRA_CSS,
        extra_js=_CHECK_EXTRA_JS,
        body_class="page-check",
        active_nav="/check",
        show_header=True,
        container_class="",
        robots_noindex=True,
        description="Comprobador manual y masivo de canales AceStream de OpenAce",
    )
