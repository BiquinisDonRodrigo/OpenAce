import json
import re
import threading

from flask import Blueprint, Response, jsonify, request

from app.utils import plugin_cache, plugin_store
from app.utils import plugin_refresh as refresh_engine
from app.utils.logging_utils import log_event

plugins_api_bp = Blueprint("plugins_api", __name__)
COMPONENT = "plugins_api"


def _slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug or 'plugin'


@plugins_api_bp.route("/api/plugins", methods=["GET"])
def api_list_plugins():
    try:
        plugins = plugin_store.get_all()
        for p in plugins:
            entry = plugin_cache.get_entry(p["id"])
            p["cached_channels"] = len(entry["channels"]) if entry else 0
        return jsonify(plugins)
    except Exception as e:
        log_event("error", "api_list_plugins_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins", methods=["POST"])
def api_create_plugin():
    try:
        data = request.get_json(silent=True) or {}
        if not data.get("display_name"):
            return jsonify({"error": "display_name es obligatorio"}), 400
        if not data.get("name"):
            data["name"] = _slugify(data["display_name"])
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
        data = request.get_json(silent=True) or {}
        if "name" in data and data["name"] != existing["name"]:
            conflict = plugin_store.get_by_name(data["name"])
            if conflict and conflict["id"] != plugin_id:
                return jsonify({"error": f"Ya existe un plugin con el nombre '{data['name']}'"}), 409
        plugin = plugin_store.update(plugin_id, data)
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

        def _do():
            refresh_engine.fetch_and_cache(plugin)

        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True, "message": "Refresco iniciado"})
    except Exception as e:
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
            data = request.get_json(silent=True) or {}
            text = data.get("content", "")

        if not text.strip():
            return jsonify({"error": "Sin contenido M3U"}), 400

        channels, groups = refresh_engine.parse_m3u_text(text)
        plugin_cache.set_channels(plugin_id, channels, groups)
        plugin_store.update_refresh_status(plugin_id, "ok", None, len(channels))
        log_event("info", "plugin_imported", COMPONENT,
                  plugin=plugin["name"], channels=len(channels))
        return jsonify({"ok": True, "channels": len(channels), "groups": len(groups)})
    except Exception as e:
        log_event("error", "api_import_plugin_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


@plugins_api_bp.route("/api/plugins/<int:plugin_id>/channels", methods=["GET"])
def api_plugin_channels(plugin_id):
    try:
        plugin = plugin_store.get_by_id(plugin_id)
        if not plugin:
            return jsonify({"error": "Plugin no encontrado"}), 404
        channels = plugin_cache.get_channels(plugin_id)
        groups = plugin_cache.get_groups(plugin_id)
        entry = plugin_cache.get_entry(plugin_id)
        return jsonify({
            "channels": channels,
            "groups": groups,
            "fetched_at": entry.get("fetched_at") if entry else None,
        })
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
            data = request.get_json(silent=True)

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
            plugin = plugin_store.create({
                "name": name,
                "display_name": item.get("display_name", name),
                "source_type": item.get("source_type", "url"),
                "source_url": item.get("source_url"),
                "refresh_interval": item.get("refresh_interval", 3600),
                "enabled": item.get("enabled", True),
            })
            channels = item.get("channels", [])
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
    except Exception as e:
        log_event("error", "api_import_json_error", COMPONENT, error=str(e))
        return jsonify({"error": "Error interno del servidor."}), 500


_PLUGINS_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce &middot; Plugins</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --muted: #8b949e;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:16px}
header a{color:var(--muted);text-decoration:none;font-size:13px}
header h1{font-size:18px;font-weight:600;color:var(--blue)}

.action-bar{padding:16px 24px;display:flex;justify-content:flex-end}

.btn{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 16px;font-size:13px;cursor:pointer;transition:border-color .2s}
.btn:hover{border-color:var(--blue)}
.btn-primary{background:rgba(88,166,255,.15);border-color:var(--blue);color:var(--blue)}
.btn-primary:hover{background:rgba(88,166,255,.25)}
.btn-danger{color:var(--red)}.btn-danger:hover{border-color:var(--red);background:rgba(248,81,73,.1)}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-success{color:var(--green)}.btn-success:hover{border-color:var(--green);background:rgba(63,185,80,.1)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:0 24px 24px}

.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.card-title{font-size:15px;font-weight:600}
.card-slug{font-size:11px;color:var(--muted);font-family:monospace}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-ok{background:rgba(63,185,80,.15);color:var(--green)}
.badge-error{background:rgba(248,81,73,.15);color:var(--red)}
.badge-pending{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge-disabled{background:rgba(139,148,158,.15);color:var(--muted)}
.stat-row{display:flex;justify-content:space-between;padding:4px 0;font-size:13px}
.stat-label{color:var(--muted)}.stat-value{font-family:monospace;font-size:12px}

.urls{margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
.url-row{display:flex;align-items:center;gap:8px;margin:4px 0}
.url-tag{font-size:10px;font-weight:700;text-transform:uppercase;padding:2px 6px;border-radius:4px;background:rgba(88,166,255,.1);color:var(--blue);min-width:38px;text-align:center}
.url-text{font-family:monospace;font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.url-copy{background:none;border:none;color:var(--blue);cursor:pointer;font-size:12px;padding:2px 6px;border-radius:4px}
.url-copy:hover{background:rgba(88,166,255,.15)}

.card-actions{display:flex;gap:8px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border);flex-wrap:wrap}

.modal-backdrop{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:1000}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:520px;max-width:90vw;max-height:90vh;overflow-y:auto}
.modal h2{font-size:16px;margin-bottom:16px;color:var(--blue)}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;font-weight:500;text-transform:uppercase;letter-spacing:.05em}
.form-group input,.form-group select{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 10px;font-size:13px}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--blue)}
.form-row{display:flex;gap:12px}.form-row .form-group{flex:1}
.form-check{display:flex;align-items:center;gap:8px}
.form-check input[type="checkbox"]{accent-color:var(--blue)}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}

.channels-section{margin:0 24px 24px}
.channels-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
.channels-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px}
.channels-filter{background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:13px;width:250px}
.channels-filter:focus{outline:none;border-color:var(--blue)}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:6px 8px;font-size:11px;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border)}
td{padding:7px 8px;border-bottom:1px solid rgba(48,54,61,.5);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px}
tr:last-child td{border-bottom:none}
.mono{font-family:monospace;font-size:12px}
.logo-thumb{width:24px;height:24px;object-fit:contain;border-radius:4px}
.hash-cell{display:flex;align-items:center;gap:4px}

.toast{position:fixed;bottom:20px;right:20px;background:var(--surface);border:1px solid var(--green);color:var(--green);padding:10px 20px;border-radius:8px;font-size:13px;z-index:2000;animation:fadeIO 2s forwards}
@keyframes fadeIO{0%{opacity:0;transform:translateY(10px)}15%{opacity:1;transform:translateY(0)}85%{opacity:1}100%{opacity:0}}
.empty{color:var(--muted);font-size:13px;font-style:italic;text-align:center;padding:40px 20px}
</style>
</head>
<body>
<header>
  <a href="/panel">&larr; Dashboard</a>
  <h1>OpenAce &middot; Plugins</h1>
</header>

<div class="action-bar">
  <input type="file" id="import-file" accept=".json" style="display:none" onchange="doImportJson(this)" />
  <button class="btn" onclick="doExportAll()">Exportar todo</button>
  <button class="btn" onclick="document.getElementById('import-file').click()">Importar JSON</button>
  <button class="btn btn-primary" onclick="showModal()">+ Nuevo Plugin</button>
</div>

<div class="grid" id="plugins-grid"></div>

<div id="channels-section" class="channels-section" style="display:none">
  <div class="channels-card">
    <div class="channels-header">
      <h3 id="ch-title" style="font-size:15px;color:var(--blue)">Canales</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <input class="channels-filter" id="ch-filter" placeholder="Filtrar canales..." oninput="filterCh()" />
        <button class="btn btn-sm" onclick="closeCh()">Cerrar</button>
      </div>
    </div>
    <div id="ch-info" style="font-size:12px;color:var(--muted);margin-bottom:8px"></div>
    <div style="overflow-x:auto">
      <table><thead><tr>
        <th style="width:30px">Logo</th><th>Nombre</th>
        <th style="width:160px">Infohash</th><th>Grupo</th>
        <th style="width:120px">TVG-ID</th>
      </tr></thead><tbody id="ch-body"></tbody></table>
    </div>
  </div>
</div>

<div id="modal-backdrop" class="modal-backdrop" style="display:none" onclick="event.target===this&&closeModal()">
  <div class="modal">
    <h2 id="modal-title">Nuevo Plugin</h2>
    <form id="pform" onsubmit="handleSubmit(event)">
      <div class="form-group">
        <label>Nombre</label>
        <input id="f-display" required placeholder="Nombre del plugin" />
      </div>
      <div class="form-group">
        <label>URL de la lista M3U</label>
        <input id="f-url" placeholder="https://... o enlace IPFS/IPNS" />
        <div style="margin-top:4px;font-size:11px;color:var(--muted)">Acepta HTTP/HTTPS y enlaces IPFS/IPNS (se resuelven por Kubo local)</div>
      </div>
      <div class="form-group">
        <label>O subir archivo</label>
        <input id="f-file" type="file" accept=".m3u,.m3u8,.txt" />
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Refresco (min)</label>
          <input id="f-interval" type="number" min="1" value="60" />
        </div>
        <div class="form-group">
          <div class="form-check" style="margin-top:22px">
            <input type="checkbox" id="f-enabled" checked />
            <label style="text-transform:none;font-size:13px;color:var(--text)">Habilitado</label>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" onclick="closeModal()">Cancelar</button>
        <button type="submit" class="btn btn-primary">Guardar</button>
      </div>
    </form>
  </div>
</div>

<script>
let plugins=[], editingId=null, allCh=[];

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function slugify(t){return t.toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'')||'plugin'}
function relTime(iso){if(!iso)return'\\u2014';const s=Math.round((Date.now()-new Date(iso).getTime())/1000);if(s<0)return'ahora';if(s<60)return'hace '+s+'s';if(s<3600)return'hace '+Math.floor(s/60)+'m';if(s<86400)return'hace '+Math.floor(s/3600)+'h';return'hace '+Math.floor(s/86400)+'d'}
function toast(m){const d=document.createElement('div');d.className='toast';d.textContent=m;document.body.appendChild(d);setTimeout(()=>d.remove(),2200)}
function copyUrl(u){if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(u).then(()=>toast('Copiado'))}else{const t=document.createElement('textarea');t.value=u;t.style.position='fixed';t.style.left='-9999px';document.body.appendChild(t);t.select();document.execCommand('copy');document.body.removeChild(t);toast('Copiado')}}

async function loadPlugins(){
  try{const r=await fetch('/api/plugins');plugins=await r.json();renderPlugins()}
  catch(e){document.getElementById('plugins-grid').innerHTML='<div class="empty">Error al cargar plugins</div>'}}

function renderPlugins(){
  const g=document.getElementById('plugins-grid');
  if(!plugins.length){g.innerHTML='<div class="empty">No hay plugins configurados. Crea uno con el bot\\u00f3n superior.</div>';return}
  g.innerHTML=plugins.map(renderCard).join('')}

function renderCard(p){
  const sc=!p.enabled?'disabled':(p.last_status||'pending');
  const st=!p.enabled?'deshabilitado':p.last_status==='ok'?'ok':p.last_status==='error'?'error':'pendiente';
  const b=location.origin;
  const ch=p.cached_channels||p.channel_count||0;
  return '<div class="card">'+
    '<div class="card-header"><div><div class="card-title">'+esc(p.display_name)+'</div>'+
    '<div class="card-slug">'+esc(p.name)+'</div></div>'+
    '<span class="badge badge-'+sc+'">'+st+'</span></div>'+
    '<div class="stat-row"><span class="stat-label">Canales</span><span class="stat-value">'+ch+'</span></div>'+
    '<div class="stat-row"><span class="stat-label">Intervalo</span><span class="stat-value">'+(p.refresh_interval?Math.round(p.refresh_interval/60)+'m':'\\u2014')+'</span></div>'+
    '<div class="stat-row"><span class="stat-label">\\u00daltimo refresco</span><span class="stat-value">'+relTime(p.last_refresh)+'</span></div>'+
    (p.last_error?'<div style="margin-top:8px;font-size:11px;color:var(--red);word-break:break-all">'+esc(p.last_error.substring(0,120))+'</div>':'')+
    '<div class="urls">'+
    urlRow('mpegts',b+'/'+p.name+'/mpegts.m3u')+
    urlRow('hls',b+'/'+p.name+'/hls.m3u')+
    '</div>'+
    '<div class="card-actions">'+
    '<button class="btn btn-sm" data-action="channels" data-id="'+p.id+'">Canales</button>'+
    '<button class="btn btn-sm btn-success" data-action="refresh" data-id="'+p.id+'">Refrescar</button>'+
    '<button class="btn btn-sm" data-action="export" data-id="'+p.id+'">Exportar</button>'+
    '<button class="btn btn-sm" data-action="edit" data-id="'+p.id+'">Editar</button>'+
    '<button class="btn btn-sm btn-danger" data-action="delete" data-id="'+p.id+'">Eliminar</button>'+
    '</div></div>'}

function urlRow(tag,url){
  return '<div class="url-row"><span class="url-tag">'+tag+'</span>'+
    '<span class="url-text" title="'+esc(url)+'">'+esc(url)+'</span>'+
    '<button class="url-copy" data-url="'+esc(url)+'">copiar</button></div>'}

async function doRefresh(id){
  try{await fetch('/api/plugins/'+id+'/refresh',{method:'POST'});toast('Refresco iniciado');setTimeout(loadPlugins,3000)}
  catch(e){toast('Error')}}

function doExport(id){window.location.href='/api/plugins/'+id+'/export'}
function doExportAll(){window.location.href='/api/plugins/export'}

async function doImportJson(input){
  if(!input.files.length)return;
  const fd=new FormData();fd.append('file',input.files[0]);
  try{
    const r=await fetch('/api/plugins/import',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok){toast(d.error||'Error');return}
    const cr=(d.imported||[]).filter(x=>x.status==='created').length;
    const ex=(d.imported||[]).filter(x=>x.status==='exists').length;
    toast(cr+' importado(s)'+(ex?', '+ex+' ya exist\\u00edan':''));
    loadPlugins()
  }catch(e){toast('Error: '+e.message)}
  input.value=''}

async function doDelete(id){
  const p=plugins.find(x=>x.id===id);
  const name=p?p.display_name:'plugin';
  if(!confirm('\\u00bfEliminar "'+name+'"?'))return;
  try{await fetch('/api/plugins/'+id,{method:'DELETE'});toast('Eliminado');loadPlugins();closeCh()}
  catch(e){toast('Error')}}

function showModal(pid){
  editingId=pid||null;
  document.getElementById('pform').reset();
  if(editingId){
    const p=plugins.find(x=>x.id===editingId);
    if(p){
      document.getElementById('modal-title').textContent='Editar Plugin';
      document.getElementById('f-display').value=p.display_name;
      document.getElementById('f-url').value=p.source_url||'';
      document.getElementById('f-interval').value=Math.round((p.refresh_interval||3600)/60);
      document.getElementById('f-enabled').checked=p.enabled}}
  else{document.getElementById('modal-title').textContent='Nuevo Plugin'}
  document.getElementById('modal-backdrop').style.display='flex'}

function closeModal(){document.getElementById('modal-backdrop').style.display='none';editingId=null}

async function handleSubmit(e){
  e.preventDefault();
  const url=document.getElementById('f-url').value.trim()||null;
  const data={
    display_name:document.getElementById('f-display').value,
    source_url:url,
    source_type:url?'url':'file',
    refresh_interval:parseInt(document.getElementById('f-interval').value||'60')*60,
    enabled:document.getElementById('f-enabled').checked};
  if(!editingId)data.name=slugify(data.display_name);
  try{
    let r;
    if(editingId){r=await fetch('/api/plugins/'+editingId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
    else{r=await fetch('/api/plugins',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
    const res=await r.json();
    if(!r.ok){toast(res.error||'Error');return}
    const fi=document.getElementById('f-file');
    if(fi.files.length){
      const fd=new FormData();fd.append('file',fi.files[0]);
      await fetch('/api/plugins/'+(editingId||res.id)+'/import',{method:'POST',body:fd})}
    closeModal();toast(editingId?'Actualizado':'Creado');loadPlugins()
  }catch(e){toast('Error: '+e.message)}}

async function showCh(id){
  const p=plugins.find(x=>x.id===id);
  const name=p?p.display_name:'Plugin';
  document.getElementById('ch-title').textContent='Canales \\u2014 '+name;
  document.getElementById('ch-filter').value='';
  document.getElementById('channels-section').style.display='';
  document.getElementById('ch-body').innerHTML='<tr><td colspan="5" style="text-align:center;color:var(--muted)">Cargando...</td></tr>';
  document.getElementById('channels-section').scrollIntoView({behavior:'smooth'});
  try{
    const r=await fetch('/api/plugins/'+id+'/channels');const d=await r.json();
    allCh=d.channels||[];
    document.getElementById('ch-info').textContent=allCh.length+' canales \\u00b7 '+relTime(d.fetched_at);
    renderCh(allCh)
  }catch(e){document.getElementById('ch-body').innerHTML='<tr><td colspan="5" style="color:var(--red)">Error</td></tr>'}}

function renderCh(list){
  const tb=document.getElementById('ch-body');
  if(!list.length){tb.innerHTML='<tr><td colspan="5" class="empty">Sin canales</td></tr>';return}
  tb.innerHTML=list.map(c=>'<tr>'+
    '<td>'+(c.tvg_logo?'<img class="logo-thumb" src="'+esc(c.tvg_logo)+'" onerror="this.style.display=\\'none\\'" />':'\\u2014')+'</td>'+
    '<td title="'+esc(c.name)+'">'+esc(c.name)+'</td>'+
    '<td class="mono"><div class="hash-cell"><span title="'+esc(c.infohash)+'">'+esc(c.infohash.substring(0,12))+'\\u2026</span>'+
    '<button class="url-copy" data-url="'+esc(c.infohash)+'">copiar</button></div></td>'+
    '<td title="'+esc(c.group_title)+'">'+esc(c.group_title)+'</td>'+
    '<td class="mono" title="'+esc(c.tvg_id)+'">'+esc(c.tvg_id||'\\u2014')+'</td></tr>').join('')}

function filterCh(){
  const q=document.getElementById('ch-filter').value.toLowerCase();
  if(!q){renderCh(allCh);return}
  renderCh(allCh.filter(c=>(c.name||'').toLowerCase().includes(q)||(c.infohash||'').includes(q)||(c.group_title||'').toLowerCase().includes(q)))}

function closeCh(){document.getElementById('channels-section').style.display='none'}

document.addEventListener('click',function(e){
  const btn=e.target.closest('[data-action]');
  if(btn){
    const id=parseInt(btn.dataset.id);
    const a=btn.dataset.action;
    if(a==='channels')showCh(id);
    else if(a==='refresh')doRefresh(id);
    else if(a==='export')doExport(id);
    else if(a==='edit')showModal(id);
    else if(a==='delete')doDelete(id);
    return;
  }
  const cp=e.target.closest('[data-url]');
  if(cp){copyUrl(cp.dataset.url);return}
});

loadPlugins();
</script>
</body>
</html>"""


@plugins_api_bp.route("/plugins")
def plugins_page():
    return Response(_PLUGINS_HTML, content_type="text/html; charset=utf-8")
