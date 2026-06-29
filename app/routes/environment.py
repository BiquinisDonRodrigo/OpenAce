from flask import Blueprint, Response, jsonify

from app.ui.base import render_page
from app.utils import environment_store
from app.utils.auth_helpers import get_json_body, require_role

environment_bp = Blueprint("environment", __name__)


@environment_bp.route("/environment")
@require_role("admin")
def environment_page():
    return Response(
        render_page(
            title="OpenAce · Environment",
            body=_BODY,
            extra_css=_CSS,
            extra_js=_JS,
            active_nav="/environment",
            robots_noindex=True,
            description="Configuracion runtime de OpenAce",
        ),
        content_type="text/html; charset=utf-8",
    )


@environment_bp.route("/api/environment", methods=["GET"])
@require_role("admin")
def api_environment_list():
    return jsonify({"items": environment_store.list_settings()})


@environment_bp.route("/api/environment", methods=["PUT"])
@require_role("admin")
def api_environment_update():
    data, jerr = get_json_body()
    if jerr:
        return jerr
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, dict):
        return jsonify({"error": "values debe ser un objeto"}), 400
    try:
        changed = environment_store.update_settings(values)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "changed": changed, "items": environment_store.list_settings()})


@environment_bp.route("/api/environment/<key>", methods=["DELETE"])
@require_role("admin")
def api_environment_reset(key):
    try:
        environment_store.reset_setting(key)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "items": environment_store.list_settings()})


_BODY = r"""
<section class="env-hero card">
  <div>
    <p class="eyebrow">Configuracion</p>
    <h1>Environment</h1>
    <p class="muted">Estas opciones sustituyen al antiguo <code>.env</code>. En el <code>.env</code> solo quedan <code>TZ</code>, <code>WG_PRIVATE_KEY</code> y <code>ProtonCountries</code>.</p>
  </div>
  <div class="env-actions">
    <button class="btn btn-primary" id="save-btn" type="button">Guardar cambios</button>
    <button class="btn" id="reload-btn" type="button">Recargar</button>
  </div>
</section>
<div id="msg" class="msg" role="status" aria-live="polite"></div>
<form id="env-form" class="env-form" autocomplete="off"><div class="empty">Cargando...</div></form>
"""


_CSS = r"""
.env-hero{display:flex;align-items:flex-start;justify-content:space-between;gap:var(--space-4);margin:var(--space-4) 0}
.env-hero h1{font-size:1.45rem;margin:2px 0 8px}
.eyebrow{margin:0;color:var(--blue);font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em}
.muted{color:var(--muted)}
.env-actions{display:flex;gap:var(--space-2);flex-wrap:wrap;justify-content:flex-end}
.env-form{display:grid;gap:var(--space-4);padding-bottom:var(--space-6)}
.env-section{padding:0;overflow:hidden}
.env-section-title{padding:var(--space-3) var(--space-4);background:var(--surface-2);border-bottom:1px solid var(--border)}
.env-section-title h2{font-size:1rem;margin:0}
.env-table-card{padding:0;overflow:auto}
.env-table{width:100%;border-collapse:collapse;min-width:860px}
.env-table th{position:sticky;top:0;z-index:1;background:var(--surface-2)}
.env-table th,.env-table td{vertical-align:top;padding:var(--space-3);border-bottom:1px solid var(--border-soft)}
.env-table tr:last-child td{border-bottom:none}
.env-table .param-name{min-width:240px}
.env-table .param-value{min-width:260px}
.env-table .param-desc{min-width:320px;color:var(--muted);line-height:1.55}
.env-label{display:block;font-weight:700;color:var(--text);margin-bottom:4px}
.env-key{display:block;font-family:var(--font-mono);font-size:.78rem;color:var(--muted)}
.env-group{display:inline-block;margin-top:8px;font-size:.76rem;color:var(--dim)}
.env-input-wrap{display:flex;flex-direction:column;gap:8px}
.env-input-wrap input,.env-input-wrap select,.env-input-wrap textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:var(--radius-sm);padding:9px 10px}
.env-input-wrap textarea{min-height:88px;resize:vertical;line-height:1.45}
.env-input-wrap input:focus,.env-input-wrap select:focus,.env-input-wrap textarea:focus{outline:none;border-color:var(--blue);box-shadow:var(--focus-ring-dim)}
.badge{white-space:nowrap}
.badge-restart{background:rgba(210,153,34,.16);color:var(--yellow)}
.badge-source{background:rgba(139,148,158,.14);color:var(--muted)}
.env-meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.reset-btn{align-self:flex-start;font-size:.78rem;padding:5px 9px}
.msg{min-height:24px;margin-bottom:var(--space-3)}
.msg.ok{color:var(--green)}.msg.err{color:var(--red)}
@media(max-width:800px){.env-hero{flex-direction:column}.env-actions{justify-content:flex-start}.env-table{min-width:760px}}
"""


_JS = r"""
(function(){
  var form = document.getElementById('env-form');
  var msg = document.getElementById('msg');
  var saveBtn = document.getElementById('save-btn');
  var reloadBtn = document.getElementById('reload-btn');

  function setMsg(text, kind){
    msg.textContent = text || '';
    msg.className = 'msg' + (kind ? ' ' + kind : '');
  }

  function inputFor(item){
    var id = 'env-' + item.key;
    if (item.type === 'bool') {
      return '<select id="' + id + '" name="' + esc(item.key) + '">'
        + '<option value="true"' + (item.value === 'true' ? ' selected' : '') + '>true</option>'
        + '<option value="false"' + (item.value === 'false' ? ' selected' : '') + '>false</option>'
        + '</select>';
    }
    if (item.key === 'PUBLIC_BASE_URL') {
      return '<textarea id="' + id + '" name="' + esc(item.key) + '" placeholder="https://openace.dominio1.tld\nhttps://openace.dominio2.tld">' + esc(item.value) + '</textarea>';
    }
    var type = item.secret ? 'password' : (item.type === 'int' || item.type === 'float' ? 'number' : 'text');
    var attrs = '';
    if (item.min !== null && item.min !== undefined) attrs += ' min="' + esc(item.min) + '"';
    if (item.max !== null && item.max !== undefined) attrs += ' max="' + esc(item.max) + '"';
    if (item.type === 'float') attrs += ' step="any"';
    var placeholder = item.secret && item.configured ? 'Configurado; dejar vacio para no cambiar' : '';
    return '<input id="' + id + '" name="' + esc(item.key) + '" type="' + type + '" value="' + esc(item.value) + '" placeholder="' + esc(placeholder) + '"' + attrs + '>';
  }

  function render(items){
    if (!items.length) {
      form.innerHTML = '<div class="empty">No hay parametros configurables.</div>';
      return;
    }
    var groups = {};
    var order = [];
    items.forEach(function(item){
      if (!groups[item.group]) {
        groups[item.group] = [];
        order.push(item.group);
      }
      groups[item.group].push(item);
    });
    form.innerHTML = order.map(function(group){
      var rows = groups[group].map(function(item){
      var badges = '<span class="badge badge-source">' + esc(item.source) + '</span>';
      if (item.restart_required) badges += '<span class="badge badge-restart">requiere reinicio</span>';
      return '<tr>'
        + '<td class="param-name"><label class="env-label" for="env-' + esc(item.key) + '">' + esc(item.label) + '</label><span class="env-key">' + esc(item.key) + '</span></td>'
        + '<td class="param-value"><div class="env-input-wrap">' + inputFor(item) + '<button class="btn reset-btn" type="button" data-reset="' + esc(item.key) + '">Restablecer default</button></div></td>'
        + '<td class="param-desc">' + esc(item.help || 'Sin descripcion disponible.') + '<div class="env-meta">' + badges + '</div></td>'
        + '</tr>';
      }).join('');
      return '<section class="card env-section"><div class="env-section-title"><h2>' + esc(group) + '</h2></div><div class="table-wrap env-table-card"><table class="env-table"><thead><tr><th>Nombre del parametro</th><th>Valor del parametro</th><th>Descripcion</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
    }).join('');
  }

  async function load(){
    setMsg('Cargando...', '');
    var data = await fetchJSON('/api/environment');
    render(data.items || []);
    setMsg('', '');
  }

  async function save(){
    var values = {};
    Array.prototype.forEach.call(form.elements, function(el){
      if (!el.name || el.disabled) return;
      values[el.name] = el.value;
    });
    saveBtn.disabled = true;
    try {
      var data = await fetchJSON('/api/environment', {method:'PUT', body:{values:values}}, 15000);
      render(data.items || []);
      var changed = (data.changed || []).length;
      setMsg(changed ? 'Guardado. Reinicia el contenedor para aplicar las opciones marcadas.' : 'No habia cambios aplicables.', 'ok');
    } catch(e) {
      setMsg(e.message || 'No se pudo guardar.', 'err');
    } finally {
      saveBtn.disabled = false;
    }
  }

  form.addEventListener('click', async function(e){
    var btn = e.target.closest('[data-reset]');
    if (!btn) return;
    btn.disabled = true;
    try {
      var data = await fetchJSON('/api/environment/' + encodeURIComponent(btn.getAttribute('data-reset')), {method:'DELETE'}, 15000);
      render(data.items || []);
      setMsg('Default restaurado.', 'ok');
    } catch(err) {
      setMsg(err.message || 'No se pudo restablecer.', 'err');
    }
  });

  saveBtn.addEventListener('click', save);
  reloadBtn.addEventListener('click', load);
  load().catch(function(e){ setMsg(e.message || 'No se pudo cargar.', 'err'); });
})();
"""
