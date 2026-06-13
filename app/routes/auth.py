import os

from flask import Blueprint, Response, g, jsonify, make_response, redirect, request

from app.utils import auth_store
from app.utils.auth_helpers import current_user, require_role
from app.utils.logging_utils import log_event

auth_bp = Blueprint("auth", __name__)
COMPONENT = "auth"


def _client_ip():
    return request.remote_addr


@auth_bp.after_request
def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET"])
def login_page():
    return Response(_LOGIN_HTML, content_type="text/html; charset=utf-8")


@auth_bp.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = _client_ip()

    if not username or not password:
        return jsonify({"ok": False, "error": "Usuario y contraseña requeridos"}), 400

    if not auth_store.check_rate_limit(ip):
        return (
            jsonify({"ok": False, "error": "Demasiados intentos. Espera unos minutos."}),
            429,
        )

    user = auth_store.verify_password(username, password)
    if user is None:
        auth_store.record_failed_attempt(ip)
        log_event("warning", "login_failed", COMPONENT, ip=ip, username=username)
        return jsonify({"ok": False, "error": "Usuario o contraseña incorrectos"}), 401

    duration = int(os.environ.get("SESSION_DURATION_HOURS", "24"))
    session_id = auth_store.create_session(user["id"], ip, duration)
    auth_store.update_last_login(user["id"])
    log_event("info", "login_success", COMPONENT, ip=ip, username=username)

    resp = jsonify({"ok": True, "user": user})
    resp.set_cookie(
        "openace_session",
        session_id,
        httponly=True,
        samesite="Lax",
        max_age=duration * 3600,
    )
    return resp


@auth_bp.route("/logout")
def logout():
    session_id = request.cookies.get("openace_session")
    if session_id:
        auth_store.delete_session(session_id)
    resp = make_response(redirect("/login"))
    resp.delete_cookie("openace_session")
    return resp


@auth_bp.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session_id = request.cookies.get("openace_session")
    if session_id:
        auth_store.delete_session(session_id)
    resp = jsonify({"ok": True})
    resp.delete_cookie("openace_session")
    return resp


@auth_bp.route("/api/auth/me")
def api_me():
    user = current_user()
    if user is None:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "user": user})


# ---------------------------------------------------------------------------
# Admin – Users
# ---------------------------------------------------------------------------

@auth_bp.route("/admin/users")
@require_role("admin")
def admin_users_page():
    return Response(_ADMIN_HTML, content_type="text/html; charset=utf-8")


@auth_bp.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def api_list_users():
    return jsonify(auth_store.get_all_users())


@auth_bp.route("/api/admin/users", methods=["POST"])
@require_role("admin")
def api_create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "user")

    if not username or not password:
        return jsonify({"error": "Usuario y contraseña requeridos"}), 400
    if auth_store.get_user_by_username(username):
        return jsonify({"error": f"El usuario '{username}' ya existe"}), 409

    user = auth_store.create_user(username, password, role)
    log_event("info", "user_created", COMPONENT, username=username, role=role)
    return jsonify(user), 201


@auth_bp.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@require_role("admin")
def api_update_user(user_id):
    existing = auth_store.get_user_by_id(user_id)
    if not existing:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data = request.get_json(silent=True) or {}
    if "username" in data and data["username"] != existing["username"]:
        conflict = auth_store.get_user_by_username(data["username"])
        if conflict and conflict["id"] != user_id:
            return jsonify({"error": f"El usuario '{data['username']}' ya existe"}), 409

    user = auth_store.update_user(user_id, data)
    log_event("info", "user_updated", COMPONENT, username=user["username"])
    return jsonify(user)


@auth_bp.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_user(user_id):
    me = current_user()
    if me and me["id"] == user_id:
        return jsonify({"error": "No puedes eliminarte a ti mismo"}), 400
    existing = auth_store.get_user_by_id(user_id)
    if not existing:
        return jsonify({"error": "Usuario no encontrado"}), 404
    auth_store.delete_user(user_id)
    log_event("info", "user_deleted", COMPONENT, username=existing["username"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin – Tokens
# ---------------------------------------------------------------------------

@auth_bp.route("/api/admin/tokens", methods=["GET"])
@require_role("admin")
def api_list_tokens():
    return jsonify(auth_store.get_all_tokens())


@auth_bp.route("/api/admin/tokens", methods=["POST"])
@require_role("admin")
def api_create_token():
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id requerido"}), 400
    user = auth_store.get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "Usuario no encontrado"}), 404
    token = auth_store.create_token(
        user_id,
        description=data.get("description"),
        expires_at=data.get("expires_at"),
    )
    log_event("info", "token_created", COMPONENT, username=user["username"])
    return jsonify(token), 201


@auth_bp.route("/api/admin/tokens/<int:token_id>", methods=["DELETE"])
@require_role("admin")
def api_delete_token(token_id):
    if not auth_store.delete_token(token_id):
        return jsonify({"error": "Token no encontrado"}), 404
    log_event("info", "token_deleted", COMPONENT, token_id=token_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# HTML – Login
# ---------------------------------------------------------------------------

_LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce &middot; Iniciar sesi&oacute;n</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--dim:#484f58;--green:#3fb950;--red:#f85149;--blue:#58a6ff;--blue-dim:#1f6feb}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;min-height:100vh;display:flex;flex-direction:column}
.topbar{background:var(--surface);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;gap:12px}
.topbar .logo{font-size:16px;font-weight:700;color:var(--blue)}
.topbar .sep{color:var(--dim)}
.topbar .page{font-size:14px;color:var(--muted);font-weight:500}
.container{flex:1;display:flex;align-items:center;justify-content:center;padding:32px 20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;max-width:400px;width:100%;padding:32px 28px}
.card h1{font-size:18px;font-weight:600;margin-bottom:24px;text-align:center}
.input-group{display:flex;flex-direction:column;gap:5px;margin-bottom:16px}
.input-group label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.input-group input{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:10px 12px;font-size:14px;width:100%;transition:border-color .15s}
.input-group input:focus{outline:none;border-color:var(--blue-dim);box-shadow:0 0 0 2px rgba(31,111,235,.25)}
.btn{display:block;width:100%;border:none;border-radius:6px;padding:11px;font-size:14px;font-weight:600;cursor:pointer;transition:opacity .15s}
.btn:disabled{opacity:.45;cursor:default}
.btn-primary{background:var(--blue);color:#fff}
.btn-primary:hover:not(:disabled){opacity:.9}
.msg{text-align:center;font-size:13px;min-height:20px;margin-bottom:12px}
.msg.err{color:var(--red)}
.msg.ok{color:var(--green)}
</style>
</head>
<body>
<div class="topbar"><span class="logo">OpenAce</span><span class="sep">/</span><span class="page">Iniciar sesi&oacute;n</span></div>
<div class="container"><div class="card">
<h1>Iniciar sesi&oacute;n</h1>
<form id="f">
<div class="input-group"><label for="u">Usuario</label><input type="text" id="u" autocomplete="username" required></div>
<div class="input-group"><label for="p">Contrase&ntilde;a</label><input type="password" id="p" autocomplete="current-password" required></div>
<div class="msg" id="msg"></div>
<button type="submit" class="btn btn-primary" id="btn">Iniciar sesi&oacute;n</button>
</form>
</div></div>
<script>
const params=new URLSearchParams(location.search);
let redir=decodeURIComponent(params.get('redirect')||'/panel');
if(!redir.startsWith('/')||redir.startsWith('//'))redir='/panel';
document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();
  const b=document.getElementById('btn'),m=document.getElementById('msg');
  b.disabled=true;m.textContent='';m.className='msg';
  try{
    const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
    const d=await r.json();
    if(d.ok){m.className='msg ok';m.textContent='Redirigiendo…';location.href=redir}
    else{m.className='msg err';m.textContent=d.error||'Error';b.disabled=false}
  }catch(x){m.className='msg err';m.textContent='Error de conexión';b.disabled=false}
};
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTML – Admin Users
# ---------------------------------------------------------------------------

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce &middot; Usuarios</title>
<style>
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--dim:#484f58;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--blue-dim:#1f6feb}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:14px 24px;display:flex;align-items:center;gap:16px}
header a{color:var(--muted);text-decoration:none;font-size:13px}header a:hover{color:var(--blue)}
header h1{font-size:18px;font-weight:600;color:var(--blue)}

.wrap{padding:16px 24px 40px;display:flex;flex-direction:column;gap:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px}
.card-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:14px}
.action-bar{display:flex;justify-content:flex-end;gap:8px;margin-bottom:8px}

.btn{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 16px;font-size:13px;cursor:pointer;transition:border-color .2s}
.btn:hover{border-color:var(--blue)}
.btn-primary{background:rgba(88,166,255,.15);border-color:var(--blue);color:var(--blue)}
.btn-primary:hover{background:rgba(88,166,255,.25)}
.btn-danger{color:var(--red)}.btn-danger:hover{border-color:var(--red);background:rgba(248,81,73,.1)}
.btn-sm{padding:4px 10px;font-size:12px}

.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-green{background:rgba(63,185,80,.15);color:var(--green)}
.badge-red{background:rgba(248,81,73,.15);color:var(--red)}
.badge-blue{background:rgba(88,166,255,.15);color:var(--blue)}
.badge-yellow{background:rgba(210,153,34,.15);color:var(--yellow)}
.badge-muted{background:rgba(139,148,158,.15);color:var(--muted)}

table{width:100%;border-collapse:collapse}
th{text-align:left;padding:8px 10px;font-size:11px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid rgba(48,54,61,.5);font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:none}
.mono{font-family:monospace;font-size:12px}
.empty{color:var(--muted);font-style:italic;padding:24px;text-align:center}

.modal-backdrop{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:1000}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;width:460px;max-width:90vw;max-height:90vh;overflow-y:auto}
.modal h2{font-size:16px;margin-bottom:16px;color:var(--blue)}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;font-weight:500;text-transform:uppercase;letter-spacing:.05em}
.form-group input,.form-group select{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:8px 10px;font-size:13px}
.form-group input:focus,.form-group select:focus{outline:none;border-color:var(--blue)}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px;padding-top:16px;border-top:1px solid var(--border)}

.toast{position:fixed;bottom:20px;right:20px;background:var(--surface);border:1px solid var(--green);color:var(--green);padding:10px 20px;border-radius:8px;font-size:13px;z-index:2000;animation:fadeIO 2.5s forwards}
@keyframes fadeIO{0%{opacity:0;transform:translateY(10px)}15%{opacity:1;transform:translateY(0)}85%{opacity:1}100%{opacity:0}}
.toast-err{border-color:var(--red);color:var(--red)}

.token-display{background:var(--bg);border:1px solid var(--green);border-radius:6px;padding:12px;margin-top:12px;word-break:break-all;font-family:monospace;font-size:13px;color:var(--green);cursor:pointer}
.token-display:hover{background:rgba(63,185,80,.05)}
.token-hint{font-size:11px;color:var(--yellow);margin-top:6px}
</style>
</head>
<body>
<header>
  <a href="/panel">&larr; Dashboard</a>
  <h1>OpenAce &middot; Usuarios</h1>
</header>

<div class="wrap">
  <!-- Users -->
  <div class="card">
    <div class="card-title">Usuarios</div>
    <div class="action-bar"><button class="btn btn-primary" onclick="showUserModal()">+ Nuevo usuario</button></div>
    <table>
      <thead><tr><th>Usuario</th><th>Rol</th><th>Estado</th><th>Creado</th><th>&Uacute;ltimo acceso</th><th>Acciones</th></tr></thead>
      <tbody id="user-rows"><tr><td colspan="6" class="empty">Cargando&hellip;</td></tr></tbody>
    </table>
  </div>

  <!-- Tokens -->
  <div class="card">
    <div class="card-title">Tokens API</div>
    <div class="action-bar"><button class="btn btn-primary" onclick="showTokenModal()">+ Nuevo token</button></div>
    <table>
      <thead><tr><th>Token</th><th>Usuario</th><th>Descripci&oacute;n</th><th>Creado</th><th>Expira</th><th>Acciones</th></tr></thead>
      <tbody id="token-rows"><tr><td colspan="6" class="empty">Cargando&hellip;</td></tr></tbody>
    </table>
    <div id="new-token-display" style="display:none">
      <div class="token-display" id="token-value" onclick="copyToken()"></div>
      <div class="token-hint">&iexcl;Copia este token ahora! No se volver&aacute; a mostrar.</div>
    </div>
  </div>
</div>

<!-- User Modal -->
<div id="user-modal" class="modal-backdrop" style="display:none" onclick="event.target===this&&closeUserModal()">
<div class="modal">
<h2 id="user-modal-title">Nuevo usuario</h2>
<form id="user-form" onsubmit="handleUserSubmit(event)">
  <div class="form-group"><label>Usuario</label><input id="fu-name" required autocomplete="off"></div>
  <div class="form-group"><label>Contrase&ntilde;a</label><input id="fu-pass" type="password" autocomplete="new-password"><div style="font-size:11px;color:var(--dim);margin-top:4px" id="fu-pass-hint">Requerida</div></div>
  <div class="form-group"><label>Rol</label><select id="fu-role"><option value="admin">Admin</option><option value="user" selected>User</option><option value="viewer">Viewer</option></select></div>
  <div class="modal-actions"><button type="button" class="btn" onclick="closeUserModal()">Cancelar</button><button type="submit" class="btn btn-primary">Guardar</button></div>
</form>
</div></div>

<!-- Token Modal -->
<div id="token-modal" class="modal-backdrop" style="display:none" onclick="event.target===this&&closeTokenModal()">
<div class="modal">
<h2>Nuevo token API</h2>
<form id="token-form" onsubmit="handleTokenSubmit(event)">
  <div class="form-group"><label>Usuario</label><select id="ft-user"></select></div>
  <div class="form-group"><label>Descripci&oacute;n (opcional)</label><input id="ft-desc" placeholder="Ej: TiviMate, Kodi&hellip;" autocomplete="off"></div>
  <div class="modal-actions"><button type="button" class="btn" onclick="closeTokenModal()">Cancelar</button><button type="submit" class="btn btn-primary">Generar</button></div>
</form>
</div></div>

<script>
let users=[],editingUserId=null;

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function toast(m,err){const d=document.createElement('div');d.className='toast'+(err?' toast-err':'');d.textContent=m;document.body.appendChild(d);setTimeout(()=>d.remove(),2800)}
function copyToken(){
  const v=document.getElementById('token-value').textContent;
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(v).then(()=>toast('Token copiado'),()=>_fbCopy(v));return}
  _fbCopy(v)}
function _fbCopy(t){const a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;left:-9999px';document.body.appendChild(a);a.focus();a.select();try{document.execCommand('copy');toast('Token copiado')}catch(e){toast('No se pudo copiar',true)}a.remove()}
function relTime(iso){if(!iso)return'—';const s=Math.round((Date.now()-new Date(iso).getTime())/1000);if(s<0)return'ahora';if(s<60)return'hace '+s+'s';if(s<3600)return'hace '+Math.floor(s/60)+'m';if(s<86400)return'hace '+Math.floor(s/3600)+'h';return'hace '+Math.floor(s/86400)+'d'}
function roleBadge(r){const m={admin:'blue',user:'green',viewer:'yellow'};return'<span class="badge badge-'+(m[r]||'muted')+'">'+esc(r)+'</span>'}

async function loadUsers(){
  try{const r=await fetch('/api/admin/users');users=await r.json();renderUsers()}
  catch(e){document.getElementById('user-rows').innerHTML='<tr><td colspan="6" class="empty">Error</td></tr>'}}

function renderUsers(){
  const tb=document.getElementById('user-rows');
  if(!users.length){tb.innerHTML='<tr><td colspan="6" class="empty">No hay usuarios</td></tr>';return}
  tb.innerHTML=users.map(u=>`<tr>
    <td><strong>${esc(u.username)}</strong></td>
    <td>${roleBadge(u.role)}</td>
    <td>${u.enabled?'<span class="badge badge-green">Activo</span>':'<span class="badge badge-red">Deshabilitado</span>'}</td>
    <td class="mono">${relTime(u.created_at)}</td>
    <td class="mono">${relTime(u.last_login)}</td>
    <td><button class="btn btn-sm" onclick="editUser(${u.id})">Editar</button> <button class="btn btn-sm btn-danger" onclick="deleteUser(${u.id})">Eliminar</button></td>
  </tr>`).join('')}

function showUserModal(id){
  editingUserId=id||null;
  document.getElementById('user-form').reset();
  if(editingUserId){
    const u=users.find(x=>x.id===editingUserId);
    if(u){document.getElementById('user-modal-title').textContent='Editar usuario';document.getElementById('fu-name').value=u.username;document.getElementById('fu-role').value=u.role;document.getElementById('fu-pass-hint').textContent='Dejar vacío para no cambiar'}
  }else{document.getElementById('user-modal-title').textContent='Nuevo usuario';document.getElementById('fu-pass-hint').textContent='Requerida'}
  document.getElementById('user-modal').style.display='flex'}
function closeUserModal(){document.getElementById('user-modal').style.display='none';editingUserId=null}
function editUser(id){showUserModal(id)}

async function handleUserSubmit(e){
  e.preventDefault();
  const data={username:document.getElementById('fu-name').value.trim(),role:document.getElementById('fu-role').value};
  const pw=document.getElementById('fu-pass').value;
  if(pw)data.password=pw;
  if(!editingUserId&&!pw){toast('Contraseña requerida',true);return}
  try{
    let r;
    if(editingUserId){r=await fetch('/api/admin/users/'+editingUserId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
    else{data.password=pw;r=await fetch('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})}
    const d=await r.json();
    if(!r.ok){toast(d.error||'Error',true);return}
    closeUserModal();toast(editingUserId?'Usuario actualizado':'Usuario creado');loadUsers();loadTokens()
  }catch(x){toast('Error de conexión',true)}}

async function deleteUser(id){
  const u=users.find(x=>x.id===id);
  if(!confirm('¿Eliminar al usuario "'+esc(u?u.username:''))+'"?')return;
  try{const r=await fetch('/api/admin/users/'+id,{method:'DELETE'});const d=await r.json();if(!r.ok){toast(d.error||'Error',true);return}toast('Usuario eliminado');loadUsers();loadTokens()}
  catch(x){toast('Error',true)}}

// Tokens
async function loadTokens(){
  try{const r=await fetch('/api/admin/tokens');const tokens=await r.json();renderTokens(tokens)}
  catch(e){document.getElementById('token-rows').innerHTML='<tr><td colspan="6" class="empty">Error</td></tr>'}}

function renderTokens(tokens){
  const tb=document.getElementById('token-rows');
  if(!tokens.length){tb.innerHTML='<tr><td colspan="6" class="empty">No hay tokens</td></tr>';return}
  tb.innerHTML=tokens.map(t=>`<tr>
    <td class="mono">${esc(t.token_preview)}</td>
    <td>${esc(t.username)}</td>
    <td>${esc(t.description||'—')}</td>
    <td class="mono">${relTime(t.created_at)}</td>
    <td class="mono">${t.expires_at?relTime(t.expires_at):'—'}</td>
    <td><button class="btn btn-sm btn-danger" onclick="deleteToken(${t.id})">Revocar</button></td>
  </tr>`).join('')}

function showTokenModal(){
  document.getElementById('token-form').reset();
  const sel=document.getElementById('ft-user');
  sel.innerHTML=users.map(u=>'<option value="'+u.id+'">'+esc(u.username)+' ('+u.role+')</option>').join('');
  document.getElementById('new-token-display').style.display='none';
  document.getElementById('token-modal').style.display='flex'}
function closeTokenModal(){document.getElementById('token-modal').style.display='none'}

async function handleTokenSubmit(e){
  e.preventDefault();
  const data={user_id:parseInt(document.getElementById('ft-user').value),description:document.getElementById('ft-desc').value.trim()||null};
  try{
    const r=await fetch('/api/admin/tokens',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    const d=await r.json();
    if(!r.ok){toast(d.error||'Error',true);return}
    closeTokenModal();
    document.getElementById('token-value').textContent=d.token;
    document.getElementById('new-token-display').style.display='';
    toast('Token generado — cópialo ahora');
    loadTokens()
  }catch(x){toast('Error',true)}}

async function deleteToken(id){
  if(!confirm('¿Revocar este token?'))return;
  try{const r=await fetch('/api/admin/tokens/'+id,{method:'DELETE'});if(!r.ok){toast('Error',true);return}toast('Token revocado');loadTokens();document.getElementById('new-token-display').style.display='none'}
  catch(x){toast('Error',true)}}

loadUsers();loadTokens();
</script>
</body>
</html>"""
