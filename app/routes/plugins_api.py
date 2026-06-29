import json
import re
import threading
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from app.utils import plugin_cache, plugin_store
from app.utils import plugin_refresh as refresh_engine
from app.utils.auth_helpers import get_json_body
from app.utils.logging_utils import log_event

plugins_api_bp = Blueprint("plugins_api", __name__)
COMPONENT = "plugins_api"

_inflight_refresh = set()
_inflight_lock = threading.Lock()


def _slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug or 'plugin'


def _valid_channels(channels):
    if not isinstance(channels, list):
        return []
    return [ch for ch in channels if isinstance(ch, dict) and ch.get("infohash")]


def _validate_plugin_payload(data, *, partial=False):
    cleaned = dict(data or {})
    if not partial or "display_name" in cleaned:
        display = (cleaned.get("display_name") or "").strip()
        if not display:
            return None, "display_name es obligatorio"
        if len(display) > 80:
            return None, "display_name no puede superar 80 caracteres"
        cleaned["display_name"] = display
        cleaned["name"] = cleaned.get("name") or _slugify(display)

    if "name" in cleaned:
        name = _slugify(cleaned.get("name") or "")
        if not name:
            return None, "name inválido"
        cleaned["name"] = name

    if not partial or "source_type" in cleaned or "source_url" in cleaned:
        source_type = cleaned.get("source_type") or ("url" if cleaned.get("source_url") else "file")
        if source_type not in ("url", "file"):
            return None, "source_type inválido"
        cleaned["source_type"] = source_type
        source_url = (cleaned.get("source_url") or "").strip() or None
        if source_type == "url":
            if not source_url:
                return None, "source_url es obligatorio para plugins URL"
            parsed = urlparse(source_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                return None, "source_url debe ser HTTP/HTTPS"
        cleaned["source_url"] = source_url

    if not partial or "refresh_interval" in cleaned:
        try:
            interval = int(cleaned.get("refresh_interval", 3600))
        except (TypeError, ValueError):
            return None, "refresh_interval inválido"
        if interval < 60 or interval > 604800:
            return None, "refresh_interval debe estar entre 60 y 604800 segundos"
        cleaned["refresh_interval"] = interval

    if "output_format" in cleaned and cleaned["output_format"] not in ("ace", "mpegts", "hls"):
        return None, "output_format inválido"

    return cleaned, None


@plugins_api_bp.route("/api/plugins", methods=["GET"])
def api_list_plugins():
    try:
        plugins = plugin_store.get_all()
        for p in plugins:
            entry = plugin_cache.get_entry(p["id"])
            p["cached_channels"] = len(entry["channels"]) if entry else 0
        # Pagination: ?page=1&per_page=50 (max 200). Without params, returns all.
        page = request.args.get("page", type=int)
        per_page = request.args.get("per_page", type=int)
        if page is not None and page > 0:
            per_page = min(max(per_page or 50, 1), 200)
            total = len(plugins)
            start = (page - 1) * per_page
            end = start + per_page
            page_items = plugins[start:end]
            return jsonify({
                "items": page_items,
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
            })
        return jsonify(plugins)
    except Exception as e:
        log_event("error", "api_list_plugins_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins", methods=["POST"])
def api_create_plugin():
    try:
        data, jerr = get_json_body()
        if jerr:
            return jerr
        data, err = _validate_plugin_payload(data)
        if err:
            return jsonify({"error": err}), 400
        existing = plugin_store.get_by_name(data["name"])
        if existing:
            return jsonify({"error": f"Ya existe un plugin con el nombre '{data['name']}'"}), 409
        plugin = plugin_store.create(data)
        if (plugin.get("enabled")
                and plugin.get("source_type") == "url"
                and plugin.get("source_url")):
            refresh_engine.start_plugin_timer(plugin)
        log_event("info", "plugin_created", COMPONENT, plugin=plugin["name"])
        return jsonify(plugin), 201
    except Exception as e:
        log_event("error", "api_create_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>", methods=["GET"])
def api_get_plugin(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404
        entry = plugin_cache.get_entry(plugin_id)
        plugin["cached_channels"] = len(entry["channels"]) if entry else 0
        return jsonify(plugin)
    except Exception as e:
        log_event("error", "api_get_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>", methods=["PUT"])
def api_update_plugin(plugin_id):
    try:
        existing = plugin_store.get_by_id(plugin_id)
        if not existing:
            return jsonify({"error": "Plugin no encontrado"}), 404
        data, jerr = get_json_body()
        if jerr:
            return jerr
        data, err = _validate_plugin_payload(data, partial=True)
        if err:
            return jsonify({"error": err}), 400
        if "name" in data and data["name"] != existing["name"]:
            conflict = plugin_store.get_by_name(data["name"])
            if conflict and conflict["id"] != plugin_id:
                return jsonify({"error": f"Ya existe un plugin con el nombre '{data['name']}'"}), 409
        plugin = plugin_store.update(plugin_id, data)
        if plugin is None:
            return jsonify({"error": "Plugin no encontrado"}), 404
        refresh_engine.restart_plugin_timer(plugin)
        log_event("info", "plugin_updated", COMPONENT, plugin=plugin["name"])
        return jsonify(plugin)
    except Exception as e:
        log_event("error", "api_update_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>", methods=["DELETE"])
def api_delete_plugin(plugin_id):
    try:
        existing = plugin_store.get_by_id(plugin_id)
        if not existing:
            return jsonify({"error": "Plugin no encontrado"}), 404
        refresh_engine.stop_plugin_timer(plugin_id)
        plugin_cache.remove(plugin_id)
        plugin_store.delete(plugin_id)
        log_event("info", "plugin_deleted", COMPONENT, plugin=existing["name"])
        return jsonify({"ok": True})
    except Exception as e:
        log_event("error", "api_delete_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>/refresh", methods=["POST"])
def api_refresh_plugin(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404

        with _inflight_lock:
            if plugin_id in _inflight_refresh:
                return jsonify({"ok": False, "error": "Refresco ya en curso"}), 409
            _inflight_refresh.add(plugin_id)

        def _do():
            try:
                refresh_engine.fetch_and_cache(plugin)
            finally:
                with _inflight_lock:
                    _inflight_refresh.discard(plugin_id)

        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True, "message": "Refresco iniciado"})
    except Exception as e:
        with _inflight_lock:
            _inflight_refresh.discard(plugin_id)
        log_event("error", "api_refresh_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>/import", methods=["POST"])
def api_import_plugin(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404

        f = request.files.get("file")
        if f:
            text = f.read().decode("utf-8", errors="replace")
        else:
            data, jerr = get_json_body()
            if jerr:
                return jerr
            text = data.get("content", "")

        if not text.strip():
            return jsonify({"error": "Sin contenido M3U"}), 400

        channels, groups = refresh_engine.parse_m3u_text(text)
        plugin_cache.set_channels(plugin_id, channels, groups)
        plugin_store.update_refresh_status(plugin_id, "ok", None, len(channels))
        log_event("info", "plugin_imported", COMPONENT,
                  plugin=plugin["name"], channels=len(channels))
        return jsonify({"ok": True, "channels": len(channels), "groups": len(groups)})
    except HTTPException:
        raise
    except Exception as e:
        log_event("error", "api_import_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>/channels", methods=["GET"])
def api_plugin_channels(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404
        entry = plugin_cache.get_entry(plugin_id)
        if entry:
            return jsonify({
                "channels": list(entry.get("channels", [])),
                "groups": list(entry.get("groups", [])),
                "fetched_at": entry.get("fetched_at"),
            })
        return jsonify({"channels": [], "groups": [], "fetched_at": None})
    except Exception as e:
        log_event("error", "api_plugin_channels_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/export", methods=["GET"])
def api_export_all():
    try:
        plugins = plugin_store.get_all()
        result = []
        for p in plugins:
            channels = plugin_cache.get_channels(p["id"])
            result.append({
                "name": p["name"],
                "display_name": p["display_name"],
                "source_type": p["source_type"],
                "source_url": p["source_url"],
                "refresh_interval": p["refresh_interval"],
                "enabled": p["enabled"],
                "channels": channels,
            })
        body = json.dumps(result, ensure_ascii=False, indent=2)
        return Response(body, content_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=openace-plugins.json"})
    except Exception as e:
        log_event("error", "api_export_all_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>/export", methods=["GET"])
def api_export_plugin(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404
        channels = plugin_cache.get_channels(plugin_id)
        result = {
            "name": plugin["name"],
            "display_name": plugin["display_name"],
            "source_type": plugin["source_type"],
            "source_url": plugin["source_url"],
            "refresh_interval": plugin["refresh_interval"],
            "enabled": plugin["enabled"],
            "channels": channels,
        }
        body = json.dumps(result, ensure_ascii=False, indent=2)
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', plugin['name'])
        return Response(body, content_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename={safe_name}.json"})
    except Exception as e:
        log_event("error", "api_export_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/import", methods=["POST"])
def api_import_json():
    try:
        f = request.files.get("file")
        if f:
            raw = f.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        else:
            data, jerr = get_json_body()
            if jerr:
                return jerr

        if not data:
            return jsonify({"error": "Sin datos JSON"}), 400

        items = data if isinstance(data, list) else [data]
        imported = []
        for item in items:
            if not isinstance(item, dict):
                continue
            display = item.get("display_name") or item.get("name")
            if not display:
                continue
            name = item.get("name") or _slugify(display)
            if plugin_store.get_by_name(name):
                imported.append({"name": name, "status": "exists"})
                continue
            # Validate payload through the same validator as create/update
            cleaned, verr = _validate_plugin_payload(item, partial=True)
            if verr:
                imported.append({"name": name, "status": "skipped", "error": verr})
                continue
            plugin = plugin_store.create({
                "name": cleaned.get("name", name),
                "display_name": cleaned.get("display_name", name),
                "source_type": cleaned.get("source_type", "url"),
                "source_url": cleaned.get("source_url"),
                "refresh_interval": cleaned.get("refresh_interval", 3600),
                "enabled": item.get("enabled", True),
            })
            channels = _valid_channels(item.get("channels", []))
            if channels:
                plugin_cache.set_channels(plugin["id"], channels)
                plugin_store.update_refresh_status(plugin["id"], "ok", None, len(channels))
            if (plugin["enabled"]
                    and plugin.get("source_type") == "url"
                    and plugin.get("source_url")):
                refresh_engine.start_plugin_timer(plugin)
            imported.append({"name": name, "status": "created", "channels": len(channels)})
            log_event("info", "plugin_imported_json", COMPONENT, plugin=name)

        return jsonify({"imported": imported})
    except json.JSONDecodeError:
        return jsonify({"error": "JSON inválido"}), 400
    except HTTPException:
        raise
    except Exception as e:
        log_event("error", "api_import_json_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


_PLUGINS_BODY = """
<div class="action-bar">
  <input type="file" id="import-file" accept=".json" hidden>
  <button type="button" class="btn" id="btn-export-all">Exportar todo</button>
  <button type="button" class="btn" id="btn-import-json">Importar JSON</button>
  <button type="button" class="btn btn-primary" id="btn-new-plugin">+ Nuevo Plugin</button>
</div>

<div class="grid" id="plugins-grid" aria-busy="true">
  <div class="empty">Cargando…</div>
</div>

<section id="channels-section" class="channels-section" hidden aria-labelledby="ch-title">
  <div class="channels-card">
    <div class="channels-header">
      <h3 id="ch-title">Canales</h3>
      <div class="channels-controls">
        <label for="ch-filter" class="sr-only">Filtrar canales</label>
        <input class="channels-filter" id="ch-filter" placeholder="Filtrar canales…" autocomplete="off">
        <button type="button" class="btn btn-sm" id="btn-close-channels">Cerrar</button>
      </div>
    </div>
    <div id="ch-info" class="ch-info"></div>
    <div class="table-wrap">
      <table>
        <caption class="sr-only">Lista de canales del plugin</caption>
        <thead><tr>
          <th scope="col" style="width:44px">Logo</th>
          <th scope="col">Nombre</th>
          <th scope="col" style="width:180px">Infohash</th>
          <th scope="col">Grupo</th>
          <th scope="col" style="width:130px">TVG-ID</th>
        </tr></thead>
        <tbody id="ch-body"></tbody>
      </table>
    </div>
  </div>
</section>

<div id="modal-backdrop" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="modal-title" hidden>
  <div class="modal" role="document">
    <h2 id="modal-title">Nuevo Plugin</h2>
    <form id="pform">
      <div class="input-group">
        <label for="f-display">Nombre</label>
        <input id="f-display" name="display_name" required placeholder="Nombre del plugin" maxlength="80" autocomplete="off">
      </div>
      <div class="input-group">
        <label for="f-url">URL de la lista M3U</label>
        <input id="f-url" name="source_url" type="url" placeholder="https://… o enlace IPFS/IPNS" spellcheck="false" autocomplete="off">
        <span class="hint">Acepta HTTP/HTTPS y enlaces IPFS/IPNS (se resuelven por Kubo local).</span>
      </div>
      <div class="input-group">
        <label for="f-file">O subir archivo</label>
        <input id="f-file" type="file" accept=".m3u,.m3u8,.txt">
      </div>
      <div class="form-row">
        <div class="input-group">
          <label for="f-interval">Refresco (min)</label>
          <input id="f-interval" name="refresh_interval" type="number" min="1" max="10080" step="1" value="60" inputmode="numeric">
        </div>
        <div class="input-group checkbox-row">
          <input type="checkbox" id="f-enabled" checked>
          <label for="f-enabled">Habilitado</label>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn" data-close="modal-backdrop">Cancelar</button>
        <button type="submit" class="btn btn-primary">Guardar</button>
      </div>
    </form>
  </div>
</div>
"""

_PLUGINS_EXTRA_CSS = """
.action-bar{position:sticky;top:var(--header-h);z-index:20;display:flex;justify-content:flex-end;gap:var(--space-2);padding:var(--space-2) 0;margin-bottom:var(--space-3);background:var(--bg);border-bottom:1px solid var(--border-soft)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:var(--space-3);margin-top:var(--space-3)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--space-3);min-width:0;box-shadow:var(--shadow-lg)}
.card-header{display:flex;justify-content:space-between;align-items:flex-start;gap:var(--space-2);margin-bottom:var(--space-2)}
.card-title{font-size:1rem;font-weight:700;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.card-slug{font-size:.786rem;color:var(--muted);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.stat-row{display:flex;justify-content:space-between;gap:var(--space-2);padding:var(--space-1) 0;font-size:.85rem;border-bottom:1px solid var(--border-soft)}
.stat-row:last-of-type{border-bottom:none}
.stat-label{color:var(--muted)}
.stat-value{font-family:var(--font-mono);font-size:.786rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.urls{margin-top:var(--space-2);padding-top:var(--space-2);border-top:1px solid var(--border-soft)}
.url-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:var(--space-2);margin:var(--space-1) 0}
.url-tag{font-size:.714rem;font-weight:700;text-transform:uppercase;padding:2px 6px;border-radius:var(--radius-sm);background:rgba(88,166,255,.12);color:var(--blue);min-width:38px;text-align:center}
.url-text{font-family:var(--font-mono);font-size:.786rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.url-copy{background:none;border:1px solid transparent;color:var(--blue);cursor:pointer;font-size:.786rem;padding:2px 6px;border-radius:var(--radius-sm);min-height:auto;min-width:auto}
.url-copy:hover{background:rgba(88,166,255,.15);border-color:rgba(88,166,255,.35)}
.url-copy:focus{outline:none;box-shadow:var(--focus-ring)}
.card-actions{display:flex;gap:var(--space-1);margin-top:var(--space-2);padding-top:var(--space-2);border-top:1px solid var(--border-soft);flex-wrap:wrap}
.channels-section{margin-top:var(--space-3)}
.channels-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:var(--space-3);box-shadow:var(--shadow-lg)}
.channels-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:var(--space-2);flex-wrap:wrap;gap:var(--space-2)}
.channels-header h3{font-size:1rem;color:var(--blue);margin:0}
.channels-controls{display:flex;gap:var(--space-2);align-items:center}
.channels-filter{width:280px;max-width:100%}
.ch-info{font-size:.786rem;color:var(--muted);margin-bottom:var(--space-2)}
.empty{color:var(--muted);font-size:.85rem;text-align:center;padding:var(--space-5) var(--space-3);grid-column:1/-1}
.form-row{display:flex;gap:var(--space-3)}
.form-row .input-group{flex:1;min-width:0}
.form-row .input-group.checkbox-row{flex:0 0 auto;align-self:flex-end;display:flex;align-items:center;gap:6px;margin-bottom:var(--space-3)}
.logo-thumb{width:22px;height:22px;object-fit:contain;border-radius:var(--radius-sm)}
.hash-cell{display:flex;align-items:center;gap:var(--space-1);min-width:0}
@media(max-width:780px){
  .action-bar{justify-content:flex-start;overflow-x:auto}
  .grid{grid-template-columns:1fr}
  .form-row{flex-direction:column;gap:0}
  .channels-filter{width:100%}
}
"""

_PLUGINS_EXTRA_JS = r"""
(function(){
  var plugins = [];
  var editingId = null;
  var allCh = [];
  var pluginsSeq = 0, channelsSeq = 0;
  var channelsPluginId = null;

  var baseEsc = window.esc;
  var esc = function(s){ return baseEsc(s); };
  function toast(m, kind){ return window.toast(m, kind || 'success'); }
  function slugify(t){
    return String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'plugin';
  }
  function relTime(iso){
    if(!iso) return '\u2014';
    var t = new Date(iso).getTime();
    if(isNaN(t)) return '\u2014';
    var s = Math.round((Date.now() - t) / 1000);
    if(s < 0) return 'ahora';
    if(s < 60) return 'hace ' + s + 's';
    if(s < 3600) return 'hace ' + Math.floor(s/60) + 'm';
    if(s < 86400) return 'hace ' + Math.floor(s/3600) + 'h';
    return 'hace ' + Math.floor(s/86400) + 'd';
  }

  function loadPlugins(){
    var seq = ++pluginsSeq;
    var grid = document.getElementById('plugins-grid');
    grid.setAttribute('aria-busy', 'true');
    return fetchJSON('/api/plugins', { cache: 'no-store' }).then(function(data){
      if(seq !== pluginsSeq) return;
      plugins = Array.isArray(data) ? data : [];
      renderPlugins();
    }).catch(function(e){
      if(seq !== pluginsSeq) return;
      grid.innerHTML = '<div class="empty">Error al cargar plugins. <button type="button" class="btn-link" id="retry-load">Reintentar</button></div>';
      var retry = document.getElementById('retry-load');
      if(retry) retry.addEventListener('click', loadPlugins);
    }).finally(function(){
      grid.setAttribute('aria-busy', 'false');
    });
  }

  function renderPlugins(){
    var g = document.getElementById('plugins-grid');
    if(!plugins.length){
      g.innerHTML = '<div class="empty">No hay plugins configurados. Crea uno con el bot\u00f3n superior.</div>';
      return;
    }
    g.innerHTML = plugins.map(renderCard).join('');
  }

  function renderCard(p){
    var sc = !p.enabled ? 'muted' : (p.last_status === 'ok' ? 'green' : p.last_status === 'error' ? 'red' : 'yellow');
    var st = !p.enabled ? 'deshabilitado' : p.last_status === 'ok' ? 'ok' : p.last_status === 'error' ? 'error' : 'pendiente';
    var b = location.origin;
    var ch = p.cached_channels || p.channel_count || 0;
    var errHtml = p.last_error
      ? '<div class="card-error" title="' + esc(p.last_error) + '">' + esc(String(p.last_error).substring(0, 120)) + '</div>'
      : '';
    return '<article class="card">' +
      '<div class="card-header"><div>' +
        '<div class="card-title" title="' + esc(p.display_name) + '">' + esc(p.display_name) + '</div>' +
        '<div class="card-slug" title="' + esc(p.name) + '">' + esc(p.name) + '</div>' +
      '</div>' +
      '<span class="badge badge-' + sc + '">' + st + '</span></div>' +
      '<div class="stat-row"><span class="stat-label">Canales</span><span class="stat-value">' + ch + '</span></div>' +
      '<div class="stat-row"><span class="stat-label">Intervalo</span><span class="stat-value">' + (p.refresh_interval ? Math.round(p.refresh_interval/60) + 'm' : '\u2014') + '</span></div>' +
      '<div class="stat-row"><span class="stat-label">\u00daltimo refresco</span><span class="stat-value">' + relTime(p.last_refresh) + '</span></div>' +
      errHtml +
      '<div class="urls">' +
        urlRow('mpegts', b + '/' + p.name + '/mpegts.m3u') +
        urlRow('hls', b + '/' + p.name + '/hls.m3u') +
      '</div>' +
      '<div class="card-actions">' +
        '<button type="button" class="btn btn-sm" data-action="channels" data-id="' + p.id + '">Canales</button>' +
        '<button type="button" class="btn btn-sm btn-success" data-action="refresh" data-id="' + p.id + '">Refrescar</button>' +
        '<button type="button" class="btn btn-sm" data-action="export" data-id="' + p.id + '">Exportar</button>' +
        '<button type="button" class="btn btn-sm" data-action="edit" data-id="' + p.id + '">Editar</button>' +
        '<button type="button" class="btn btn-sm btn-danger" data-action="delete" data-id="' + p.id + '">Eliminar</button>' +
      '</div>' +
    '</article>';
  }

  function urlRow(tag, url){
    return '<div class="url-row"><span class="url-tag">' + tag + '</span>' +
      '<span class="url-text" title="' + esc(url) + '">' + esc(url) + '</span>' +
      '<button type="button" class="url-copy" data-url="' + esc(url) + '" aria-label="Copiar URL ' + tag + '">copiar</button></div>';
  }

  function doRefresh(id){
    // Poll last_refresh until it changes (or up to ~30s).
    var p = plugins.find(function(x){ return x.id === id; });
    var previousRefresh = (p && p.last_refresh) || null;
    var attempts = 0;
    toast('Refresco iniciado');
    function poll(){
      attempts++;
      if(attempts > 10) return;  // ~30s @ 3s
      fetchJSON('/api/plugins', { cache: 'no-store' }).then(function(data){
        plugins = Array.isArray(data) ? data : [];
        var updated = plugins.find(function(x){ return x.id === id; });
        if(updated && updated.last_refresh !== previousRefresh){
          renderPlugins();
        } else {
          setTimeout(poll, 3000);
        }
      }).catch(function(){ setTimeout(poll, 3000); });
    }
    setTimeout(poll, 1500);
  }

  function doExport(id){ window.location.href = '/api/plugins/' + id + '/export'; }
  function doExportAll(){ window.location.href = '/api/plugins/export'; }

  function doImportJson(input){
    if(!input.files || !input.files.length) return;
    var fd = new FormData();
    fd.append('file', input.files[0]);
    var originalLabel = '';
    var btn = document.getElementById('btn-import-json');
    if(btn){ originalLabel = btn.textContent; btn.disabled = true; btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Importando…'; }
    fetchJSON('/api/plugins/import', { method: 'POST', body: fd, skipCsrf: false })
      .then(function(d){
        var cr = (d.imported || []).filter(function(x){ return x.status === 'created'; }).length;
        var ex = (d.imported || []).filter(function(x){ return x.status === 'exists'; }).length;
        toast(cr + ' importado(s)' + (ex ? ', ' + ex + ' ya exist\u00edan' : ''), cr > 0 ? 'success' : 'info');
        return loadPlugins();
      })
      .catch(function(e){
        toast((e && e.body && e.body.error) || 'Error al importar', 'error');
      })
      .finally(function(){
        if(btn){ btn.disabled = false; btn.textContent = originalLabel; }
        input.value = '';
      });
  }

  function doDelete(id){
    var p = plugins.find(function(x){ return x.id === id; });
    var name = p ? p.display_name : 'plugin';
    if(!confirm('\u00bfEliminar "' + name + '"? Esta acci\u00f3n no se puede deshacer.')) return;
    fetchJSON('/api/plugins/' + id, { method: 'DELETE' })
      .then(function(){ toast('Eliminado'); if(channelsPluginId === id) closeCh(); return loadPlugins(); })
      .catch(function(e){ toast((e && e.body && e.body.error) || 'Error', 'error'); });
  }

  // ---- Modal with focus-trap ----
  var modalCtrl = null;
  function initModal(){
    modalCtrl = window.setupModal(document.getElementById('modal-backdrop'), {
      initialFocus: '#f-display',
      onClose: closeModal
    });
    document.getElementById('btn-new-plugin').addEventListener('click', function(){ showModal(null); });
    document.querySelectorAll('[data-close]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var id = btn.getAttribute('data-close');
        if(id === 'modal-backdrop' && modalCtrl) modalCtrl.close();
      });
    });
  }

  function showModal(pid){
    editingId = pid || null;
    document.getElementById('pform').reset();
    if(editingId){
      var p = plugins.find(function(x){ return x.id === editingId; });
      if(!p){ toast('Plugin no encontrado', 'error'); return; }
      document.getElementById('modal-title').textContent = 'Editar Plugin';
      document.getElementById('f-display').value = p.display_name;
      document.getElementById('f-url').value = p.source_url || '';
      document.getElementById('f-interval').value = Math.round((p.refresh_interval || 3600) / 60);
      document.getElementById('f-enabled').checked = p.enabled;
    } else {
      document.getElementById('modal-title').textContent = 'Nuevo Plugin';
    }
    modalCtrl.open();
  }
  function closeModal(){
    document.getElementById('modal-backdrop').hidden = true;
    document.body.style.overflow = '';
    editingId = null;
  }

  function handleSubmit(e){
    e.preventDefault();
    var url = document.getElementById('f-url').value.trim() || null;
    // Client-side URL validation (server also validates).
    if(url){
      try {
        var parsed = new URL(url);
        if(parsed.protocol !== 'http:' && parsed.protocol !== 'https:'){
          toast('La URL debe ser HTTP o HTTPS', 'error');
          return;
        }
      } catch(_) {
        toast('URL inv\u00e1lida', 'error');
        return;
      }
    }
    var intervalMin = parseInt(document.getElementById('f-interval').value, 10);
    if(!isFinite(intervalMin) || intervalMin < 1 || intervalMin > 10080){
      toast('Intervalo debe estar entre 1 y 10080 minutos', 'error');
      return;
    }
    var data = {
      display_name: document.getElementById('f-display').value,
      source_url: url,
      source_type: url ? 'url' : 'file',
      refresh_interval: intervalMin * 60,
      enabled: document.getElementById('f-enabled').checked
    };
    if(!editingId) data.name = slugify(data.display_name);
    var btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    var original = btn.textContent;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Guardando…';
    var url2 = editingId ? ('/api/plugins/' + editingId) : '/api/plugins';
    var method = editingId ? 'PUT' : 'POST';
    var createdId = null;
    fetchJSON(url2, { method: method, body: data })
      .then(function(res){
        createdId = editingId || res.id;
        // Optional file upload. PREVIOUSLY this block awaited fetch but
        // never checked r.ok, so a failed M3U import produced a 'Creado'
        // success toast with zero channels. Now we surface the error.
        var fi = document.getElementById('f-file');
        if(fi.files && fi.files.length){
          var fd = new FormData();
          fd.append('file', fi.files[0]);
          return fetchJSON('/api/plugins/' + createdId + '/import', { method: 'POST', body: fd });
        }
        return null;
      })
      .then(function(importResult){
        if(modalCtrl) modalCtrl.close();
        var msg = editingId ? 'Actualizado' : 'Creado';
        if(importResult && importResult.channels === 0){
          msg += ' (sin canales en el archivo)';
        }
        toast(msg);
        return loadPlugins();
      })
      .catch(function(e){
        toast((e && e.body && e.body.error) || e.message || 'Error', 'error');
      })
      .finally(function(){
        btn.disabled = false;
        btn.textContent = original;
      });
  }

  function showCh(id){
    var seq = ++channelsSeq;
    channelsPluginId = id;
    var p = plugins.find(function(x){ return x.id === id; });
    var name = p ? p.display_name : 'Plugin';
    document.getElementById('ch-title').textContent = 'Canales \u2014 ' + name;
    document.getElementById('ch-filter').value = '';
    document.getElementById('channels-section').hidden = false;
    document.getElementById('ch-body').innerHTML = '<tr><td colspan="5" class="empty">Cargando…</td></tr>';
    document.getElementById('channels-section').scrollIntoView({ behavior: 'smooth' });
    fetchJSON('/api/plugins/' + id + '/channels', { cache: 'no-store' }).then(function(d){
      if(seq !== channelsSeq) return;
      allCh = (d && d.channels) || [];
      document.getElementById('ch-info').textContent = allCh.length + ' canales \u00b7 ' + relTime(d.fetched_at);
      renderCh(allCh);
    }).catch(function(){
      if(seq !== channelsSeq) return;
      document.getElementById('ch-body').innerHTML = '<tr><td colspan="5" class="empty">Error al cargar canales.</td></tr>';
    });
  }

  function renderCh(list){
    var tb = document.getElementById('ch-body');
    if(!list.length){ tb.innerHTML = '<tr><td colspan="5" class="empty">Sin canales</td></tr>'; return; }
    // Cap to first 500 rows to avoid freezing the tab on huge M3Us.
    var capped = list.slice(0, 500);
    var suffix = list.length > capped.length ? '<tr><td colspan="5" class="empty">Mostrando 500 de ' + list.length + '. Usa el filtro para acotar.</td></tr>' : '';
    tb.innerHTML = capped.map(function(c){
      var hash = c.infohash || '';
      var hashShort = hash ? esc(hash.substring(0, 12)) + '\u2026' : '\u2014';
      return '<tr>' +
        '<td>' + (c.tvg_logo ? '<img class="logo-thumb" src="' + esc(c.tvg_logo) + '" alt="" loading="lazy" onerror="this.style.display=\'none\'" />' : '\u2014') + '</td>' +
        '<td title="' + esc(c.name) + '">' + esc(c.name) + '</td>' +
        '<td class="mono"><div class="hash-cell"><span title="' + esc(hash) + '">' + hashShort + '</span>' +
        (hash ? '<button type="button" class="url-copy" data-url="' + esc(hash) + '" aria-label="Copiar infohash">copiar</button>' : '') + '</div></td>' +
        '<td title="' + esc(c.group_title || '') + '">' + esc(c.group_title || '\u2014') + '</td>' +
        '<td class="mono" title="' + esc(c.tvg_id || '') + '">' + esc(c.tvg_id || '\u2014') + '</td>' +
      '</tr>';
    }).join('') + suffix;
  }

  var filterTimer = null;
  function filterCh(){
    if(filterTimer) clearTimeout(filterTimer);
    filterTimer = setTimeout(function(){
      var q = document.getElementById('ch-filter').value.toLowerCase();
      if(!q){ renderCh(allCh); return; }
      renderCh(allCh.filter(function(c){
        return (c.name || '').toLowerCase().indexOf(q) !== -1 ||
               (c.infohash || '').indexOf(q) !== -1 ||
               (c.group_title || '').toLowerCase().indexOf(q) !== -1;
      }));
    }, 200);
  }

  function closeCh(){
    document.getElementById('channels-section').hidden = true;
    channelsPluginId = null;
    allCh = [];
  }

  // ---- Event delegation ----
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-action]');
    if(btn){
      var id = parseInt(btn.dataset.id, 10);
      if(!isFinite(id)){ return; }
      var a = btn.dataset.action;
      if(a === 'channels') showCh(id);
      else if(a === 'refresh') doRefresh(id);
      else if(a === 'export') doExport(id);
      else if(a === 'edit') showModal(id);
      else if(a === 'delete') doDelete(id);
      return;
    }
    var cp = e.target.closest('[data-url]');
    if(cp){
      window.copyToClipboard(cp.dataset.url).then(function(ok){
        toast(ok ? 'Copiado' : 'No se pudo copiar', ok ? 'success' : 'error');
      });
      return;
    }
  });

  // Wire static buttons
  document.getElementById('btn-export-all').addEventListener('click', doExportAll);
  document.getElementById('btn-import-json').addEventListener('click', function(){
    document.getElementById('import-file').click();
  });
  document.getElementById('import-file').addEventListener('change', function(){
    doImportJson(this);
  });
  document.getElementById('btn-close-channels').addEventListener('click', closeCh);
  document.getElementById('ch-filter').addEventListener('input', filterCh);

  // Wire form and modal
  document.getElementById('pform').addEventListener('submit', handleSubmit);
  initModal();
  loadPlugins();
})();
"""


def _render_plugins_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Plugins",
        body=_PLUGINS_BODY,
        extra_css=_PLUGINS_EXTRA_CSS,
        extra_js=_PLUGINS_EXTRA_JS,
        body_class="page-plugins",
        active_nav="/plugins",
        show_header=True,
        container_class="container",
        robots_noindex=True,
        description="Gestión de plugins M3U de OpenAce",
    )


@plugins_api_bp.route("/plugins")
def plugins_page():
    return Response(_render_plugins_html(), content_type="text/html; charset=utf-8")
