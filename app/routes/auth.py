import os
import re

from flask import Blueprint, Response, g, jsonify, make_response, redirect, request

from app.utils import auth_store, environment_store
from app.utils.auth_helpers import current_user, get_json_body, require_role
from app.utils.logging_utils import log_event

auth_bp = Blueprint("auth", __name__)
COMPONENT = "auth"
_USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


def _client_ip():
    return request.remote_addr


def _session_duration_hours():
    try:
        return environment_store.get_int("SESSION_DURATION_HOURS")
    except (TypeError, ValueError):
        return 24


def _secure_cookie():
    return request.is_secure or environment_store.get_bool("REVERSE_PROXY")


def _validate_username(username):
    if len(username) < 3 or len(username) > 32:
        return "El usuario debe tener entre 3 y 32 caracteres"
    if not _USERNAME_RE.match(username):
        return "Usuario solo puede contener letras, números, '.', '_' y '-'"
    return None


# ---------------------------------------------------------------------------
# Security headers are now applied globally in app/__init__.py:create_app()
# via @app.after_request. The previous @auth_bp.after_request was redundant.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET"])
def login_page():
    return Response(_render_login_html(), content_type="text/html; charset=utf-8")


@auth_bp.route("/api/auth/login", methods=["POST"])
def api_login():
    data, jerr = get_json_body()
    if jerr:
        return jerr
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = _client_ip()

    if not username or not password:
        return jsonify({"ok": False, "error": "Usuario y contraseña requeridos"}), 400

    if not auth_store.check_and_record_login_attempt(ip):
        log_event("warning", "login_rate_limited", COMPONENT, ip=ip, username=username)
        return (
            jsonify({"ok": False, "error": "Demasiados intentos. Espera unos minutos."}),
            429,
        )

    user = auth_store.verify_password(username, password)
    if user is None:
        log_event("warning", "login_failed", COMPONENT, ip=ip, username=username)
        return jsonify({"ok": False, "error": "Usuario o contraseña incorrectos"}), 401
    auth_store.clear_login_attempts(ip)

    duration = _session_duration_hours()
    old_session_id = request.cookies.get("openace_session")
    if old_session_id:
        auth_store.delete_session(old_session_id)

    session_id = auth_store.create_session(user["id"], ip, duration)
    auth_store.update_last_login(user["id"])
    log_event("info", "login_success", COMPONENT, ip=ip, username=username)

    resp = jsonify({"ok": True, "user": user})
    resp.set_cookie(
        "openace_session",
        session_id,
        httponly=True,
        secure=_secure_cookie(),
        samesite="Lax",
        max_age=duration * 3600,
    )
    return resp


@auth_bp.route("/logout", methods=["POST"])
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
    return Response(_render_admin_html(), content_type="text/html; charset=utf-8")


@auth_bp.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def api_list_users():
    return jsonify(auth_store.get_all_users())


@auth_bp.route("/api/admin/users", methods=["POST"])
@require_role("admin")
def api_create_user():
    data, jerr = get_json_body()
    if jerr:
        return jerr
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = data.get("role", "user")
    expires_at = data.get("expires_at") or None

    if not username or not password:
        return jsonify({"error": "Usuario y contraseña requeridos"}), 400
    username_error = _validate_username(username)
    if username_error:
        return jsonify({"error": username_error}), 400
    if len(password) < 8:
        return jsonify({"error": "La contraseña debe tener al menos 8 caracteres"}), 400
    if role not in ("admin", "user", "viewer"):
        return jsonify({"error": "Rol inválido (debe ser admin, user o viewer)"}), 400
    if auth_store.get_user_by_username(username):
        return jsonify({"error": f"El usuario '{username}' ya existe"}), 409

    user = auth_store.create_user(username, password, role, expires_at=expires_at)
    log_event("info", "user_created", COMPONENT, username=username, role=role)
    return jsonify(user), 201


@auth_bp.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@require_role("admin")
def api_update_user(user_id):
    existing = auth_store.get_user_by_id(user_id)
    if not existing:
        return jsonify({"error": "Usuario no encontrado"}), 404

    data, jerr = get_json_body()
    if jerr:
        return jerr
    me = current_user()
    if "username" in data:
        data["username"] = (data.get("username") or "").strip()
        username_error = _validate_username(data["username"])
        if username_error:
            return jsonify({"error": username_error}), 400
    if me and me["id"] == user_id:
        if "role" in data and data["role"] != "admin":
            return jsonify({"error": "No puedes degradarte a ti mismo"}), 400
        if "enabled" in data and data["enabled"] in (False, 0, "0", "false", "False", "no", "off"):
            return jsonify({"error": "No puedes deshabilitarte a ti mismo"}), 400
    if "username" in data and data["username"] != existing["username"]:
        conflict = auth_store.get_user_by_username(data["username"])
        if conflict and conflict["id"] != user_id:
            return jsonify({"error": f"El usuario '{data['username']}' ya existe"}), 409
    if "password" in data and data["password"] and len(data["password"]) < 8:
        return jsonify({"error": "La contraseña debe tener al menos 8 caracteres"}), 400

    try:
        user = auth_store.update_user(user_id, data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
    data, jerr = get_json_body()
    if jerr:
        return jerr
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id requerido"}), 400
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "user_id debe ser un entero"}), 400
    if user_id <= 0:
        return jsonify({"error": "user_id inválido"}), 400
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

_LOGIN_BODY = """
<div class="login-wrap">
  <div class="card login-card">
    <h1 class="login-title">Iniciar sesión</h1>
    <form id="f" novalidate>
      <div class="input-group">
        <label for="u">Usuario</label>
        <input type="text" id="u" name="username" autocomplete="username" required autofocus>
      </div>
      <div class="input-group">
        <label for="p">Contraseña</label>
        <div class="password-wrap">
          <input type="password" id="p" name="password" autocomplete="current-password" required>
          <button type="button" class="password-toggle" id="pw-toggle" aria-label="Mostrar contraseña" aria-pressed="false">
            <span aria-hidden="true">👁</span>
          </button>
        </div>
      </div>
      <div class="msg" id="msg" role="alert" aria-live="assertive"></div>
      <button type="submit" class="btn btn-primary btn-block btn-lg" id="btn">Iniciar sesión</button>
    </form>
  </div>
</div>
"""

_LOGIN_EXTRA_CSS = """
.login-wrap{display:flex;align-items:center;justify-content:center;min-height:calc(100vh - var(--header-h));padding:var(--space-5) var(--space-3)}
.login-card{max-width:400px;width:100%;padding:var(--space-5)}
.login-title{font-size:1.286rem;text-align:center;margin-bottom:var(--space-4)}
.password-wrap{position:relative;display:flex;align-items:center}
.password-wrap input{padding-right:42px}
.password-toggle{position:absolute;right:6px;background:transparent;border:none;color:var(--muted);cursor:pointer;padding:6px 8px;border-radius:var(--radius-sm);min-height:auto;min-width:auto;line-height:1}
.password-toggle:hover{color:var(--text)}
.password-toggle:focus{outline:none;box-shadow:var(--focus-ring)}
.password-toggle[aria-pressed="true"]{color:var(--blue)}
"""

_LOGIN_EXTRA_JS = r"""
(function(){
  var params = new URLSearchParams(location.search);
  var redir = '/panel';
  try {
    var r = params.get('redirect');
    if (r && r.indexOf('\\') === -1) {
      var target = new URL(r, location.origin);
      if (target.origin === location.origin) redir = target.pathname + target.search + target.hash;
    }
  } catch(e) { /* leave default */ }

  function setMsg(text, kind) {
    var m = document.getElementById('msg');
    m.className = 'msg' + (kind ? ' ' + kind : '');
    m.textContent = text;
  }

  // Password show/hide toggle (a11y: keyboard-operable, aria-pressed synced)
  var toggle = document.getElementById('pw-toggle');
  var pwInput = document.getElementById('p');
  toggle.addEventListener('click', function(){
    var show = pwInput.type === 'password';
    pwInput.type = show ? 'text' : 'password';
    toggle.setAttribute('aria-pressed', show ? 'true' : 'false');
    toggle.setAttribute('aria-label', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
    pwInput.focus();
  });

  document.getElementById('f').addEventListener('submit', async function(e){
    e.preventDefault();
    var b = document.getElementById('btn');
    var originalLabel = b.textContent;
    b.disabled = true;
    b.innerHTML = '<span class="spinner" aria-hidden="true"></span> Verificando…';
    setMsg('', '');
    try {
      var d = await fetchJSON('/api/auth/login', { method: 'POST', body: {
        username: document.getElementById('u').value,
        password: document.getElementById('p').value,
      }}, 10000);
      if (d.ok) {
        setMsg('Redirigiendo…', 'ok');
        b.innerHTML = '<span class="spinner" aria-hidden="true"></span> Redirigiendo…';
        location.href = redir;
      } else {
        var msg = d.error || 'No se pudo iniciar sesión.';
        setMsg(msg, 'err');
        b.disabled = false;
        b.textContent = originalLabel;
      }
    } catch(x) {
      setMsg(x.status === 429 ? (x.message || 'Demasiados intentos. Espera unos minutos.') : (x.message || 'Error de conexión con el servidor.'), 'err');
      b.disabled = false;
      b.textContent = originalLabel;
    }
  });

  // If ?redirect= is present, show a banner explaining why we need auth.
  if (params.get('redirect')) {
    var banner = document.createElement('div');
    banner.className = 'msg msg-info';
    banner.style.marginBottom = 'var(--space-3)';
    banner.setAttribute('role', 'status');
    banner.textContent = 'Debes iniciar sesión para acceder a la página solicitada.';
    document.querySelector('.login-card').insertBefore(banner, document.getElementById('f'));
  }
})();
"""


def _render_login_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Iniciar sesión",
        body=_LOGIN_BODY,
        extra_css=_LOGIN_EXTRA_CSS,
        extra_js=_LOGIN_EXTRA_JS,
        body_class="page-login",
        show_header=False,
        container_class="",
        robots_noindex=True,
        description="Iniciar sesión en OpenAce",
    )


# ---------------------------------------------------------------------------
# HTML – Admin Users
# ---------------------------------------------------------------------------

_ADMIN_BODY = """
<div class="admin-grid">
  <section class="card admin-section" aria-labelledby="users-title">
    <h2 id="users-title" class="card-title">Usuarios</h2>
    <div class="action-bar"><button type="button" class="btn btn-primary" id="open-user-modal">+ Nuevo usuario</button></div>
    <div class="table-wrap">
      <table>
        <caption class="sr-only">Lista de usuarios</caption>
        <thead><tr>
          <th scope="col">Usuario</th><th scope="col">Rol</th><th scope="col">Estado</th>
          <th scope="col">Expira</th><th scope="col">Creado</th><th scope="col">Último acceso</th><th scope="col">Acciones</th>
        </tr></thead>
        <tbody id="user-rows"><tr><td colspan="7" class="empty">Cargando…</td></tr></tbody>
      </table>
    </div>
  </section>

  <section class="card admin-section" aria-labelledby="tokens-title">
    <h2 id="tokens-title" class="card-title">Tokens API</h2>
    <div class="action-bar"><button type="button" class="btn btn-primary" id="open-token-modal">+ Nuevo token</button></div>
    <div class="table-wrap">
      <table>
        <caption class="sr-only">Lista de tokens API</caption>
        <thead><tr>
          <th scope="col">Token</th><th scope="col">Usuario</th><th scope="col">Descripción</th>
          <th scope="col">Creado</th><th scope="col">Expira</th><th scope="col">Acciones</th>
        </tr></thead>
        <tbody id="token-rows"><tr><td colspan="6" class="empty">Cargando…</td></tr></tbody>
      </table>
    </div>
    <div id="new-token-display" hidden>
      <div class="token-display" id="token-value" tabindex="0" role="button" aria-label="Copiar token al portapapeles"></div>
      <div class="token-hint">¡Copia este token ahora! No se volverá a mostrar.</div>
      <button type="button" class="btn btn-sm btn-ghost" id="dismiss-token-display">Cerrar</button>
      <button type="button" class="btn btn-sm" id="reveal-token-toggle" aria-pressed="true">Ocultar</button>
    </div>
  </section>
</div>

<!-- User Modal -->
<div id="user-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="user-modal-title" hidden>
  <div class="modal" role="document">
    <h2 id="user-modal-title">Nuevo usuario</h2>
    <form id="user-form">
      <div class="input-group">
        <label for="fu-name">Usuario</label>
        <input id="fu-name" name="username" required autocomplete="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_.\\-]+">
        <span class="hint">3-32 caracteres: letras, números, _, -, .</span>
      </div>
      <div class="input-group">
        <label for="fu-pass">Contraseña</label>
        <div class="password-wrap">
          <input id="fu-pass" name="password" type="password" autocomplete="new-password" minlength="8">
          <button type="button" class="password-toggle" data-target="fu-pass" aria-label="Mostrar contraseña" aria-pressed="false"><span aria-hidden="true">👁</span></button>
        </div>
        <span class="hint" id="fu-pass-hint">Requerida (mínimo 8 caracteres)</span>
      </div>
      <div class="input-group">
        <label for="fu-role">Rol</label>
        <select id="fu-role">
          <option value="admin">Admin</option>
          <option value="user" selected>User</option>
          <option value="viewer">Viewer</option>
        </select>
      </div>
      <div class="input-group">
        <label for="fu-expires">Expiración (opcional)</label>
        <select id="fu-expires">
          <option value="">Sin expiración</option>
          <option value="1h">1 hora</option>
          <option value="6h">6 horas</option>
          <option value="12h">12 horas</option>
          <option value="1d">1 día</option>
          <option value="3d">3 días</option>
          <option value="7d">7 días</option>
          <option value="30d">30 días</option>
          <option value="custom">Fecha personalizada</option>
        </select>
        <input type="datetime-local" id="fu-expires-custom" hidden>
        <span class="hint" id="fu-expires-hint"></span>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn" data-close="user-modal">Cancelar</button>
        <button type="submit" class="btn btn-primary">Guardar</button>
      </div>
    </form>
  </div>
</div>

<!-- Token Modal -->
<div id="token-modal" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="token-modal-title" hidden>
  <div class="modal" role="document">
    <h2 id="token-modal-title">Nuevo token API</h2>
    <form id="token-form">
      <div class="input-group">
        <label for="ft-user">Usuario</label>
        <select id="ft-user"></select>
      </div>
      <div class="input-group">
        <label for="ft-desc">Descripción (opcional)</label>
        <input id="ft-desc" placeholder="Ej: TiviMate, Kodi…" autocomplete="off" maxlength="120">
      </div>
      <div class="input-group">
        <label for="ft-expires">Expiración (opcional)</label>
        <select id="ft-expires">
          <option value="">Sin expiración</option>
          <option value="1h">1 hora</option>
          <option value="6h">6 horas</option>
          <option value="12h">12 horas</option>
          <option value="1d">1 día</option>
          <option value="7d">7 días</option>
          <option value="30d">30 días</option>
          <option value="90d">90 días</option>
          <option value="365d">1 año</option>
          <option value="custom">Fecha personalizada</option>
        </select>
        <input type="datetime-local" id="ft-expires-custom" hidden>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn" data-close="token-modal">Cancelar</button>
        <button type="submit" class="btn btn-primary">Generar</button>
      </div>
    </form>
  </div>
</div>
"""

_ADMIN_EXTRA_CSS = """
.admin-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,.85fr);gap:var(--space-3);align-items:start;margin-top:var(--space-3)}
.admin-section .action-bar{display:flex;justify-content:flex-end;gap:var(--space-2);margin-bottom:var(--space-2)}
.admin-section .card-title{margin-bottom:var(--space-2)}
.token-display{
  background:var(--bg);border:1px solid var(--green);border-radius:var(--radius);
  padding:var(--space-2);margin-top:var(--space-3);word-break:break-all;
  font-family:var(--font-mono);font-size:.85rem;color:var(--green);
  cursor:pointer;
}
.token-display:hover{background:rgba(63,185,80,.05)}
.token-display:focus{outline:none;box-shadow:var(--focus-ring)}
.token-display[data-masked="true"]{filter:blur(4px);cursor:pointer}
.token-hint{font-size:.786rem;color:var(--yellow);margin-top:var(--space-1)}
#new-token-display{margin-top:var(--space-3);padding-top:var(--space-3);border-top:1px solid var(--border-soft)}
#new-token-display button{margin-top:var(--space-2);margin-right:var(--space-2)}
@media(max-width:1180px){.admin-grid{grid-template-columns:1fr}.table-wrap{max-height:none}}
"""

_ADMIN_EXTRA_JS = r"""
(function(){
  var users = [];
  var editingUserId = null;
  var usersSeq = 0, tokensSeq = 0;

  var baseEsc = window.esc;
  var esc = function(s){ return baseEsc(s); };
  function toast(m, kind){ return window.toast(m, kind || 'success'); }

  function relTime(iso){
    if(!iso) return '—';
    var t = new Date(iso).getTime();
    if(isNaN(t)) return '—';
    var s = Math.round((Date.now() - t) / 1000);
    if(s < 0) return 'ahora';
    if(s < 60) return 'hace ' + s + 's';
    if(s < 3600) return 'hace ' + Math.floor(s/60) + 'm';
    if(s < 86400) return 'hace ' + Math.floor(s/3600) + 'h';
    return 'hace ' + Math.floor(s/86400) + 'd';
  }

  function expiresLabel(iso){
    if(!iso) return '—';
    var t = new Date(iso).getTime();
    if(isNaN(t)) return '—';
    var ms = t - Date.now();
    if(ms <= 0) return '<span class="badge badge-red">Expirado</span>';
    var s = Math.round(ms / 1000);
    var txt;
    if(s < 3600) txt = 'en ' + Math.ceil(s/60) + 'm';
    else if(s < 86400) txt = 'en ' + Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
    else txt = 'en ' + Math.floor(s/86400) + 'd';
    return '<span class="badge badge-yellow">' + txt + '</span>';
  }

  function roleBadge(r){
    var m = {admin:'blue', user:'green', viewer:'yellow'};
    return '<span class="badge badge-' + (m[r] || 'muted') + '">' + esc(r) + '</span>';
  }

  function computeExpiresAt(sel, customInput){
    var v = sel.value;
    if(!v) return null;
    if(v === 'custom'){
      var cv = customInput.value;
      if(!cv) return null;
      var t = new Date(cv).getTime();
      if(isNaN(t)) return null;
      return new Date(cv).toISOString();
    }
    var m = v.match(/^(\d+)([hd])$/);
    if(!m) return null;
    var ms = parseInt(m[1], 10) * (m[2] === 'h' ? 3600000 : 86400000);
    return new Date(Date.now() + ms).toISOString();
  }

  function setupExpiresToggle(selId, customId){
    var sel = document.getElementById(selId);
    var custom = document.getElementById(customId);
    sel.addEventListener('change', function(){
      custom.hidden = (sel.value !== 'custom');
    });
  }

  // ---- Loaders with sequence-id race protection ----
  function loadUsers(){
    var seq = ++usersSeq;
    return fetchJSON('/api/admin/users', { cache: 'no-store' }).then(function(data){
      if(seq !== usersSeq) return;  // stale response
      if(!Array.isArray(data)){ data = []; }
      users = data;
      renderUsers();
    }).catch(function(){
      if(seq !== usersSeq) return;
      document.getElementById('user-rows').innerHTML = '<tr><td colspan="7" class="empty">Error al cargar. <button type="button" class="btn-link" onclick="loadUsers()">Reintentar</button></td></tr>';
    });
  }

  function userStatus(u){
    if(!u.enabled) return '<span class="badge badge-red">Deshabilitado</span>';
    if(u.expires_at && new Date(u.expires_at).getTime() <= Date.now()) return '<span class="badge badge-red">Expirado</span>';
    return '<span class="badge badge-green">Activo</span>';
  }

  function renderUsers(){
    var tb = document.getElementById('user-rows');
    if(!users.length){
      tb.innerHTML = '<tr><td colspan="7" class="empty">No hay usuarios. Crea el primero con el botón superior.</td></tr>';
      return;
    }
    tb.innerHTML = users.map(function(u){
      return '<tr>' +
        '<td><strong>' + esc(u.username) + '</strong></td>' +
        '<td>' + roleBadge(u.role) + '</td>' +
        '<td>' + userStatus(u) + '</td>' +
        '<td class="mono">' + expiresLabel(u.expires_at) + '</td>' +
        '<td class="mono">' + relTime(u.created_at) + '</td>' +
        '<td class="mono">' + relTime(u.last_login) + '</td>' +
        '<td><button type="button" class="btn btn-sm" onclick="editUser(' + u.id + ')">Editar</button> ' +
        '<button type="button" class="btn btn-sm btn-danger" onclick="deleteUser(' + u.id + ')">Eliminar</button></td>' +
      '</tr>';
    }).join('');
  }

  // ---- Modal management with focus-trap via setupModal ----
  var userModalCtrl = null, tokenModalCtrl = null;

  function initModals(){
    userModalCtrl = window.setupModal(document.getElementById('user-modal'), {
      initialFocus: '#fu-name',
      onClose: closeUserModal
    });
    tokenModalCtrl = window.setupModal(document.getElementById('token-modal'), {
      initialFocus: '#ft-user',
      onClose: closeTokenModal
    });
    document.getElementById('open-user-modal').addEventListener('click', function(){ showUserModal(null); });
    document.getElementById('open-token-modal').addEventListener('click', showTokenModal);
    document.querySelectorAll('[data-close]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var id = btn.getAttribute('data-close');
        if(id === 'user-modal' && userModalCtrl) userModalCtrl.close();
        if(id === 'token-modal' && tokenModalCtrl) tokenModalCtrl.close();
      });
    });
  }

  window.showUserModal = function(id){
    editingUserId = id || null;
    document.getElementById('user-form').reset();
    document.getElementById('fu-expires').value = '';
    var customExp = document.getElementById('fu-expires-custom');
    customExp.hidden = true;
    customExp.value = '';
    document.getElementById('fu-expires-hint').textContent = '';
    if(editingUserId){
      var u = users.find(function(x){ return x.id === editingUserId; });
      if(!u){ toast('Usuario no encontrado', 'error'); return; }
      document.getElementById('user-modal-title').textContent = 'Editar usuario';
      document.getElementById('fu-name').value = u.username;
      document.getElementById('fu-role').value = u.role;
      document.getElementById('fu-pass-hint').textContent = 'Dejar vacío para no cambiar';
      if(u.expires_at){
        document.getElementById('fu-expires').value = 'custom';
        customExp.hidden = false;
        var d = new Date(u.expires_at);
        var pad = function(n){ return String(n).padStart(2, '0'); };
        customExp.value = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
        document.getElementById('fu-expires-hint').textContent = 'Actual: ' + d.toLocaleString();
      }
    } else {
      document.getElementById('user-modal-title').textContent = 'Nuevo usuario';
      document.getElementById('fu-pass-hint').textContent = 'Requerida (mínimo 8 caracteres)';
    }
    userModalCtrl.open();
  };
  function closeUserModal(){
    document.getElementById('user-modal').hidden = true;
    document.body.style.overflow = '';
    editingUserId = null;
  }
  window.editUser = function(id){ showUserModal(id); };

  function handleUserSubmit(e){
    e.preventDefault();
    var data = {
      username: document.getElementById('fu-name').value.trim(),
      role: document.getElementById('fu-role').value
    };
    // Client-side validation matches server rules.
    if(!/^[A-Za-z0-9_.\-]{3,32}$/.test(data.username)){
      toast('Usuario inválido. Usa 3-32 caracteres alfanuméricos.', 'error');
      document.getElementById('fu-name').focus();
      return;
    }
    var pw = document.getElementById('fu-pass').value;
    var wasEditing = !!editingUserId;
    if(wasEditing){
      if(pw){
        if(pw.length < 8){ toast('La contraseña debe tener al menos 8 caracteres', 'error'); return; }
        data.password = pw;
      }
    } else {
      if(pw.length < 8){ toast('Contraseña requerida (mínimo 8 caracteres)', 'error'); return; }
      data.password = pw;
    }
    var exp = computeExpiresAt(document.getElementById('fu-expires'), document.getElementById('fu-expires-custom'));
    if(document.getElementById('fu-expires').value === 'custom' && !exp){
      toast('Selecciona una fecha de expiración válida', 'error');
      return;
    }
    data.expires_at = exp;
    var btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    var original = btn.textContent;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Guardando…';
    var url = wasEditing ? '/api/admin/users/' + editingUserId : '/api/admin/users';
    var method = wasEditing ? 'PUT' : 'POST';
    fetchJSON(url, { method: method, body: data })
      .then(function(d){
        if(userModalCtrl) userModalCtrl.close();
        toast(wasEditing ? 'Usuario actualizado' : 'Usuario creado');
        return Promise.all([loadUsers(), loadTokens()]);
      })
      .catch(function(e){
        toast((e && e.body && e.body.error) || e.message || 'Error', 'error');
      })
      .finally(function(){
        btn.disabled = false;
        btn.textContent = original;
      });
  }

  window.deleteUser = function(id){
    var u = users.find(function(x){ return x.id === id; });
    // Note: confirm() is plain text; do NOT use esc() here.
    if(!confirm('¿Eliminar al usuario "' + (u ? u.username : '') + '"? Esta acción no se puede deshacer.')) return;
    fetchJSON('/api/admin/users/' + id, { method: 'DELETE' })
      .then(function(){ toast('Usuario eliminado'); return Promise.all([loadUsers(), loadTokens()]); })
      .catch(function(e){ toast((e && e.body && e.body.error) || e.message || 'Error', 'error'); });
  };

  // ---- Tokens ----
  function loadTokens(){
    var seq = ++tokensSeq;
    return fetchJSON('/api/admin/tokens', { cache: 'no-store' }).then(function(tokens){
      if(seq !== tokensSeq) return;
      if(!Array.isArray(tokens)) tokens = [];
      renderTokens(tokens);
    }).catch(function(){
      if(seq !== tokensSeq) return;
      document.getElementById('token-rows').innerHTML = '<tr><td colspan="6" class="empty">Error al cargar. <button type="button" class="btn-link" onclick="loadTokens()">Reintentar</button></td></tr>';
    });
  }

  function renderTokens(tokens){
    var tb = document.getElementById('token-rows');
    if(!tokens.length){
      tb.innerHTML = '<tr><td colspan="6" class="empty">No hay tokens</td></tr>';
      return;
    }
    tb.innerHTML = tokens.map(function(t){
      return '<tr>' +
        '<td class="mono">' + esc(t.token_preview) + '</td>' +
        '<td>' + esc(t.username) + '</td>' +
        '<td>' + esc(t.description || '—') + '</td>' +
        '<td class="mono">' + relTime(t.created_at) + '</td>' +
        '<td>' + expiresLabel(t.expires_at) + '</td>' +
        '<td><button type="button" class="btn btn-sm btn-danger" onclick="deleteToken(' + t.id + ')">Revocar</button></td>' +
      '</tr>';
    }).join('');
  }

  function showTokenModal(){
    document.getElementById('token-form').reset();
    document.getElementById('ft-expires').value = '';
    var customExp = document.getElementById('ft-expires-custom');
    customExp.hidden = true;
    customExp.value = '';
    var sel = document.getElementById('ft-user');
    sel.innerHTML = users.map(function(u){
      return '<option value="' + u.id + '">' + esc(u.username) + ' (' + u.role + ')</option>';
    }).join('');
    document.getElementById('new-token-display').hidden = true;
    tokenModalCtrl.open();
  }
  function closeTokenModal(){
    document.getElementById('token-modal').hidden = true;
    document.body.style.overflow = '';
  }

  window.deleteToken = function(id){
    if(!confirm('¿Revocar este token? Los clientes que lo usen perderán acceso.')) return;
    fetchJSON('/api/admin/tokens/' + id, { method: 'DELETE' })
      .then(function(){
        toast('Token revocado');
        // Only hide the new-token panel if we just created and displayed a token
        // (we cannot reliably tell if the revoked id matches the displayed one
        // because tokens are shown truncated elsewhere). Leave the panel alone.
        return loadTokens();
      })
      .catch(function(e){
        var msg = (e && e.body && e.body.error) || e.message || 'Error';
        toast(msg, 'error');
      });
  };

  // ---- Token submit handler (async, uses fetchJSON) ----
  async function handleTokenSubmitAsync(e){
    e.preventDefault();
    var userId = parseInt(document.getElementById('ft-user').value, 10);
    if(!isFinite(userId)){ toast('Selecciona un usuario', 'error'); return; }
    var data = {
      user_id: userId,
      description: document.getElementById('ft-desc').value.trim() || null
    };
    var exp = computeExpiresAt(document.getElementById('ft-expires'), document.getElementById('ft-expires-custom'));
    if(document.getElementById('ft-expires').value === 'custom' && !exp){
      toast('Selecciona una fecha de expiración válida', 'error');
      return;
    }
    if(exp) data.expires_at = exp;
    var btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    var original = btn.textContent;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Generando…';
    try {
      var d = await fetchJSON('/api/admin/tokens', { method: 'POST', body: data });
      if(tokenModalCtrl) tokenModalCtrl.close();
      document.getElementById('token-value').textContent = d.token;
      document.getElementById('token-value').setAttribute('data-masked', 'false');
      document.getElementById('reveal-token-toggle').setAttribute('aria-pressed', 'true');
      document.getElementById('reveal-token-toggle').textContent = 'Ocultar';
      document.getElementById('new-token-display').hidden = false;
      toast('Token generado — cópialo ahora');
      loadTokens();
      // Auto-hide the token after 60 seconds for shoulder-surfing protection.
      if(window._tokenHideTimer) clearTimeout(window._tokenHideTimer);
      window._tokenHideTimer = setTimeout(function(){
        var disp = document.getElementById('new-token-display');
        if(!disp.hidden){
          disp.hidden = true;
          toast('Token oculto automáticamente por seguridad', 'info');
        }
      }, 60000);
    } catch(x){
      toast((x && x.body && x.body.error) || x.message || 'Error', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // ---- Token panel controls ----
  document.getElementById('dismiss-token-display').addEventListener('click', function(){
    document.getElementById('new-token-display').hidden = true;
    if(window._tokenHideTimer){ clearTimeout(window._tokenHideTimer); window._tokenHideTimer = null; }
  });
  document.getElementById('reveal-token-toggle').addEventListener('click', function(){
    var el = document.getElementById('token-value');
    var masked = el.getAttribute('data-masked') === 'true';
    el.setAttribute('data-masked', masked ? 'false' : 'true');
    this.setAttribute('aria-pressed', masked ? 'true' : 'false');
    this.textContent = masked ? 'Ocultar' : 'Mostrar';
  });
  document.getElementById('token-value').addEventListener('click', function(){
    var v = this.textContent;
    window.copyToClipboard(v).then(function(ok){
      toast(ok ? 'Token copiado' : 'No se pudo copiar', ok ? 'success' : 'error');
    });
  });
  document.getElementById('token-value').addEventListener('keydown', function(e){
    if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); this.click(); }
  });

  // ---- Password show/hide in user modal ----
  document.querySelectorAll('.password-toggle').forEach(function(btn){
    btn.addEventListener('click', function(){
      var target = document.getElementById(btn.getAttribute('data-target'));
      if(!target) return;
      var show = target.type === 'password';
      target.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
      target.focus();
    });
  });

  // ---- datetime-local min=today (prevent past dates) ----
  var now = new Date();
  var pad = function(n){ return String(n).padStart(2, '0'); };
  var minDate = now.getFullYear() + '-' + pad(now.getMonth()+1) + '-' + pad(now.getDate()) + 'T' + pad(now.getHours()) + ':' + pad(now.getMinutes());
  document.getElementById('fu-expires-custom').setAttribute('min', minDate);
  document.getElementById('ft-expires-custom').setAttribute('min', minDate);

  // ---- Wire forms and run initial loads ----
  document.getElementById('user-form').addEventListener('submit', handleUserSubmit);
  document.getElementById('token-form').addEventListener('submit', handleTokenSubmitAsync);
  setupExpiresToggle('fu-expires', 'fu-expires-custom');
  setupExpiresToggle('ft-expires', 'ft-expires-custom');
  initModals();
  loadUsers();
  loadTokens();

  // Export reloaders for "Reintentar" buttons
  window.loadUsers = loadUsers;
  window.loadTokens = loadTokens;
})();
"""


def _render_admin_html():
    from app.ui.base import render_page
    return render_page(
        title="OpenAce · Usuarios y Tokens",
        body=_ADMIN_BODY,
        extra_css=_ADMIN_EXTRA_CSS,
        extra_js=_ADMIN_EXTRA_JS,
        body_class="page-admin",
        active_nav="/admin/users",
        show_header=True,
        container_class="container",
        robots_noindex=True,
        description="Gestión de usuarios y tokens API de OpenAce",
    )
