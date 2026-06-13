import json
import re
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, redirect, request

from app.utils import auth_store, eula_store, plugin_store, setup_store
from app.utils import plugin_refresh
from app.utils.auth_helpers import require_role
from app.utils.logging_utils import log_event

setup_bp = Blueprint("setup", __name__)
COMPONENT = "setup"

_STEP_ORDER = ["eula", "users", "plugins", "summary"]


def _slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return slug or 'plugin'


def _client_ip():
    return request.remote_addr


def _esc(s):
    if not s:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _step_reached(completed, required):
    if completed is None:
        return False
    try:
        return _STEP_ORDER.index(completed) >= _STEP_ORDER.index(required)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = (
    ":root{--bg:#0d1117;--surface:#161b22;--surface-2:#1c2129;--border:#30363d;"
    "--text:#e6edf3;--muted:#8b949e;--dim:#484f58;--green:#3fb950;--red:#f85149;"
    "--yellow:#d29922;--blue:#58a6ff;--blue-dim:#1f6feb}"
    "*{box-sizing:border-box;margin:0;padding:0}"
    "body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"
    "\"Segoe UI\",Helvetica,Arial,sans-serif;font-size:14px;min-height:100vh;"
    "display:flex;flex-direction:column}"
    ".topbar{background:var(--surface);border-bottom:1px solid var(--border);"
    "padding:12px 24px;display:flex;align-items:center;gap:12px}"
    ".topbar .logo{font-size:16px;font-weight:700;color:var(--blue);letter-spacing:-.02em}"
    ".topbar .sep{color:var(--dim)}"
    ".topbar .page{font-size:14px;color:var(--muted);font-weight:500}"
    ".wrap{flex:1;display:flex;flex-direction:column;align-items:center;padding:32px 20px}"
    ".card{background:var(--surface);border:1px solid var(--border);border-radius:10px;"
    "max-width:680px;width:100%;padding:32px 28px}"
    ".card h1{font-size:18px;font-weight:600;margin-bottom:4px}"
    ".card .subtitle{font-size:13px;color:var(--muted);margin-bottom:24px}"
    ".progress-bar{display:flex;align-items:center;max-width:560px;width:100%;"
    "margin:0 auto 20px;padding:8px 0}"
    ".step{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;"
    "text-decoration:none;white-space:nowrap}"
    ".dot{width:24px;height:24px;border-radius:50%;display:flex;align-items:center;"
    "justify-content:center;font-size:11px;flex-shrink:0}"
    ".step-done .dot{background:var(--green);color:#fff}"
    ".step-current .dot{background:var(--blue);color:#fff}"
    ".step-pending .dot{border:2px solid var(--dim)}"
    ".step-done{color:var(--green)}.step-current{color:var(--blue)}.step-pending{color:var(--dim)}"
    ".step-done:hover{opacity:.8}"
    ".connector{flex:1;height:2px;margin:0 12px}"
    ".connector-done{background:var(--green)}.connector-pending{background:var(--dim)}"
    ".input-group{display:flex;flex-direction:column;gap:5px;margin-bottom:16px}"
    ".input-group label{font-size:11px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.06em;color:var(--muted)}"
    ".input-group input,.input-group select{background:var(--bg);border:1px solid var(--border);"
    "color:var(--text);border-radius:6px;padding:10px 12px;font-size:14px;width:100%;"
    "transition:border-color .15s}"
    ".input-group input:focus,.input-group select:focus{outline:none;"
    "border-color:var(--blue-dim);box-shadow:0 0 0 2px rgba(31,111,235,.25)}"
    ".btn{display:inline-flex;align-items:center;justify-content:center;"
    "border:1px solid var(--border);border-radius:6px;padding:10px 20px;"
    "font-size:14px;font-weight:600;cursor:pointer;transition:all .15s;"
    "background:var(--surface);color:var(--text);text-decoration:none}"
    ".btn:hover{border-color:var(--blue)}"
    ".btn:disabled{opacity:.45;cursor:default}"
    ".btn-primary{background:var(--blue);color:#fff;border-color:var(--blue)}"
    ".btn-primary:hover:not(:disabled){opacity:.9}"
    ".btn-outline{background:transparent;color:var(--muted);border-color:var(--border)}"
    ".btn-outline:hover{color:var(--text);border-color:var(--muted)}"
    ".btn-danger{color:var(--red);border-color:var(--border)}"
    ".btn-danger:hover{border-color:var(--red);background:rgba(248,81,73,.1)}"
    ".btn-sm{padding:6px 12px;font-size:12px}"
    ".actions{display:flex;justify-content:space-between;align-items:center;"
    "margin-top:24px;padding-top:20px;border-top:1px solid var(--border)}"
    ".actions-right{display:flex;gap:8px}"
    ".msg{text-align:center;font-size:13px;min-height:20px;margin-bottom:12px}"
    ".msg.err{color:var(--red)}.msg.ok{color:var(--green)}"
    ".badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}"
    ".badge-green{background:rgba(63,185,80,.15);color:var(--green)}"
    ".badge-red{background:rgba(248,81,73,.15);color:var(--red)}"
    ".badge-blue{background:rgba(88,166,255,.15);color:var(--blue)}"
    ".badge-yellow{background:rgba(210,153,34,.15);color:var(--yellow)}"
    ".badge-muted{background:rgba(139,148,158,.15);color:var(--muted)}"
    "table{width:100%;border-collapse:collapse}"
    "th{text-align:left;padding:8px 10px;font-size:11px;color:var(--muted);font-weight:600;"
    "text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);"
    "white-space:nowrap}"
    "td{padding:8px 10px;border-bottom:1px solid rgba(48,54,61,.5);font-size:13px}"
    "tr:last-child td{border-bottom:none}"
    ".section{margin-bottom:24px}"
    ".section-title{font-size:13px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.06em;color:var(--muted);margin-bottom:12px;"
    "padding-bottom:8px;border-bottom:1px solid var(--border)}"
    ".hint{font-size:12px;color:var(--dim);margin-top:4px}"
    ".check-group{display:flex;align-items:center;gap:10px;padding:16px 0}"
    ".check-group input[type=checkbox]{width:18px;height:18px;accent-color:var(--blue);flex-shrink:0}"
    ".check-group label{font-size:14px;color:var(--text);cursor:pointer}"
    ".eula-body{max-height:320px;overflow-y:auto;margin-bottom:16px;padding-right:8px}"
    ".eula-body::-webkit-scrollbar{width:6px}"
    ".eula-body::-webkit-scrollbar-track{background:transparent}"
    ".eula-body::-webkit-scrollbar-thumb{background:var(--dim);border-radius:3px}"
    ".clause{padding:14px 0;border-bottom:1px solid rgba(48,54,61,.4)}"
    ".clause:last-child{border-bottom:none}"
    ".clause h2{font-size:12px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.06em;color:var(--blue);margin-bottom:6px}"
    ".clause p{font-size:13px;line-height:1.7;color:var(--muted);margin-bottom:6px}"
    ".clause p:last-child{margin-bottom:0}"
    ".clause ul{margin:4px 0 0 18px;font-size:13px;line-height:1.7;color:var(--muted)}"
    ".clause ul li{margin-bottom:3px}"
    ".item-row{display:flex;align-items:center;justify-content:space-between;"
    "padding:8px 12px;background:var(--bg);border:1px solid var(--border);"
    "border-radius:6px;margin-bottom:6px}"
    ".item-info{display:flex;align-items:center;gap:10px;overflow:hidden}"
    ".item-detail{display:flex;flex-direction:column;gap:2px;overflow:hidden}"
    ".item-name{font-weight:600;font-size:13px}"
    ".item-meta{font-size:11px;color:var(--muted);overflow:hidden;"
    "text-overflow:ellipsis;white-space:nowrap}"
    ".summary-section{margin-bottom:20px}"
    ".summary-title{font-size:12px;font-weight:600;text-transform:uppercase;"
    "letter-spacing:.06em;color:var(--muted);margin-bottom:10px;"
    "display:flex;align-items:center;gap:8px}"
    ".info-box{background:var(--bg);border:1px solid var(--border);border-radius:6px;"
    "padding:12px 16px;font-size:13px;color:var(--muted);margin-top:16px;line-height:1.8}"
    ".info-box code{background:rgba(88,166,255,.1);padding:2px 6px;border-radius:4px;"
    "font-size:12px;color:var(--blue)}"
    "@media(max-width:600px){.card{padding:24px 16px}"
    ".progress-bar{flex-wrap:wrap;gap:4px}.connector{margin:0 4px}}"
)


# ---------------------------------------------------------------------------
# Progress indicator
# ---------------------------------------------------------------------------

def _progress_indicator(viewing_step):
    steps = [
        ("eula", "EULA", "/setup/eula"),
        ("users", "Usuarios", "/setup/users"),
        ("plugins", "Plugins", "/setup/plugins"),
        ("summary", "Resumen", "/setup/summary"),
    ]
    viewing_idx = next((i for i, s in enumerate(steps) if s[0] == viewing_step), 0)
    parts = []
    for i, (key, label, url) in enumerate(steps):
        if i > 0:
            done = viewing_step == "summary" or i <= viewing_idx
            parts.append('<span class="connector '
                         + ('connector-done' if done else 'connector-pending')
                         + '"></span>')
        if viewing_step == "summary" or i < viewing_idx:
            parts.append('<a href="' + url + '" class="step step-done">'
                         '<span class="dot">&#10003;</span>' + label + '</a>')
        elif i == viewing_idx:
            parts.append('<span class="step step-current">'
                         '<span class="dot"></span>' + label + '</span>')
        else:
            parts.append('<span class="step step-pending">'
                         '<span class="dot"></span>' + label + '</span>')
    return '<div class="progress-bar">' + ''.join(parts) + '</div>'


# ---------------------------------------------------------------------------
# Page renderer
# ---------------------------------------------------------------------------

def _render_page(title, step_key, body, script=""):
    p = _progress_indicator(step_key)
    return (
        '<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>OpenAce &middot; ' + title + '</title>\n'
        '<style>' + _CSS + '</style>\n</head>\n<body>\n'
        '<div class="topbar">\n'
        '  <span class="logo">OpenAce</span>\n'
        '  <span class="sep">/</span>\n'
        '  <span class="page">Configuraci&oacute;n inicial</span>\n'
        '</div>\n'
        '<div class="wrap">\n' + p + '\n<div class="card">\n'
        + body +
        '\n</div>\n</div>\n'
        + ('<script>\n' + script + '\n</script>\n' if script else '')
        + '</body>\n</html>'
    )


# ---------------------------------------------------------------------------
# Step 1 — EULA
# ---------------------------------------------------------------------------

_STEP1_BODY = (
    '<h1>Configuraci&oacute;n inicial</h1>\n'
    '<p class="subtitle">Paso 1 de 4 &mdash; Acuerdo de Licencia de Usuario Final</p>\n'
    '<div class="eula-body">\n'
    '  <div class="clause">\n'
    '    <h2>1. Objeto</h2>\n'
    '    <p>El presente acuerdo regula las condiciones de uso del servicio\n'
    '    OpenAce (&laquo;la Aplicaci&oacute;n&raquo;), que act&uacute;a como\n'
    '    proxy HTTP para el motor AceStream, proporcionando agregaci&oacute;n\n'
    '    de listas de reproducci&oacute;n M3U y transcodificaci&oacute;n HLS\n'
    '    bajo demanda.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>2. Aceptaci&oacute;n</h2>\n'
    '    <p>El acceso y uso de la Aplicaci&oacute;n requiere la\n'
    '    aceptaci&oacute;n &iacute;ntegra de este acuerdo. Dicha\n'
    '    aceptaci&oacute;n se formaliza mediante la introducci&oacute;n de la\n'
    '    frase literal indicada al pie de este documento y queda registrada\n'
    '    con marca temporal, direcci&oacute;n IP de origen y hash\n'
    '    criptogr&aacute;fico de la frase.</p>\n'
    '    <p>Estos datos se almacenan localmente en una base de datos SQLite\n'
    '    (<code>data.db</code>) ubicada en el propio servidor que ejecuta la\n'
    '    Aplicaci&oacute;n. No se transmiten a servicios externos.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>3. Contenido de terceros</h2>\n'
    '    <p>La Aplicaci&oacute;n no aloja, produce ni controla los contenidos\n'
    '    retransmitidos a trav&eacute;s del motor AceStream. Todo el material\n'
    '    audiovisual es responsabilidad exclusiva de los proveedores de origen\n'
    '    y de la red P2P. El operador de la Aplicaci&oacute;n no asume\n'
    '    responsabilidad alguna sobre la legalidad, exactitud o disponibilidad\n'
    '    de dichos contenidos.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>4. Tratamiento de datos</h2>\n'
    '    <p>La Aplicaci&oacute;n registra exclusivamente los siguientes datos,\n'
    '    almacenados en una base de datos local SQLite\n'
    '    (<code>data.db</code>) en el servidor:</p>\n'
    '    <ul>\n'
    '      <li>Direcci&oacute;n IP del usuario, con fines de control de\n'
    '      consentimiento.</li>\n'
    '      <li>Cadena User-Agent del navegador.</li>\n'
    '      <li>Hash SHA-256 de la frase de aceptaci&oacute;n (nunca la frase\n'
    '      en texto plano).</li>\n'
    '      <li>Marca temporal de la aceptaci&oacute;n y, en su caso, de la\n'
    '      revocaci&oacute;n.</li>\n'
    '    </ul>\n'
    '    <p>No se almacenan otros datos personales identificativos. Los datos\n'
    '    no se transmiten a terceros ni a servicios externos; permanecen\n'
    '    &uacute;nicamente en el fichero <code>data.db</code> del servidor\n'
    '    local. Las preferencias del usuario se almacenan exclusivamente en\n'
    '    cookies locales del propio dispositivo.</p>\n'
    '    <p>El usuario puede revocar su consentimiento en cualquier momento\n'
    '    desde esta misma p&aacute;gina, lo que inhabilitar&aacute; el acceso\n'
    '    a la Aplicaci&oacute;n hasta una nueva aceptaci&oacute;n.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>5. Marco legal europeo aplicable</h2>\n'
    '    <p>El presente EULA se rige por la legislaci&oacute;n de la\n'
    '    Uni&oacute;n Europea y espa&ntilde;ola. Son de aplicaci&oacute;n,\n'
    '    entre otras normas:</p>\n'
    '    <ul>\n'
    '      <li><strong>Reglamento (UE) 2016/679 (RGPD)</strong> &mdash; La\n'
    '      Aplicaci&oacute;n no recoge datos personales identificativos. Las\n'
    '      preferencias del usuario se almacenan exclusivamente en cookies\n'
    '      locales del propio dispositivo.</li>\n'
    '      <li><strong>Directiva 2019/790/UE</strong> sobre derechos de autor\n'
    '      en el mercado &uacute;nico digital &mdash; El usuario asume la\n'
    '      responsabilidad de respetar los derechos de autor de los contenidos\n'
    '      a los que acceda.</li>\n'
    '      <li><strong>Directiva 2000/31/CE (Comercio Electr&oacute;nico)</strong>\n'
    '      &mdash; El desarrollador, al actuar como mero intermediario\n'
    '      t&eacute;cnico sin control editorial sobre los contenidos\n'
    '      enlazados, queda amparado por el r&eacute;gimen de exenci&oacute;n\n'
    '      de los arts.&nbsp;12&ndash;15 de dicha Directiva.</li>\n'
    '      <li><strong>Ley 34/2002 (LSSI-CE, Espa&ntilde;a)</strong> &mdash;\n'
    '      El desarrollador no es responsable de los contenidos accesibles a\n'
    '      trav&eacute;s de los enlaces externos, conforme al art.&nbsp;17 de\n'
    '      la citada ley.</li>\n'
    '      <li><strong>Real Decreto Legislativo 1/1996 (LPI,\n'
    '      Espa&ntilde;a)</strong> &mdash; Cualquier infracci&oacute;n de los\n'
    '      derechos de propiedad intelectual derivada del uso de la\n'
    '      Aplicaci&oacute;n es responsabilidad exclusiva del usuario.</li>\n'
    '    </ul>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>6. Ausencia de garant&iacute;as</h2>\n'
    '    <p>La Aplicaci&oacute;n se proporciona &laquo;tal cual&raquo;\n'
    '    (<em>as is</em>), sin garant&iacute;a de disponibilidad, exactitud o\n'
    '    idoneidad para ning&uacute;n prop&oacute;sito concreto.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>7. Limitaci&oacute;n de responsabilidad</h2>\n'
    '    <p>En ning&uacute;n caso el operador ser&aacute; responsable por\n'
    '    da&ntilde;os directos, indirectos, incidentales, especiales o\n'
    '    consecuentes derivados del uso o la imposibilidad de uso de la\n'
    '    Aplicaci&oacute;n, incluyendo p&eacute;rdida de datos o\n'
    '    interrupci&oacute;n del servicio.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>8. Modificaciones del EULA</h2>\n'
    '    <p>El desarrollador se reserva el derecho de modificar el presente\n'
    '    EULA en cualquier momento. El uso continuado de la Aplicaci&oacute;n\n'
    '    tras la publicaci&oacute;n de cambios implicar&aacute; la\n'
    '    aceptaci&oacute;n de los nuevos t&eacute;rminos.</p>\n'
    '  </div>\n'
    '  <div class="clause">\n'
    '    <h2>9. Resoluci&oacute;n</h2>\n'
    '    <p>El operador podr&aacute; revocar el acceso a la Aplicaci&oacute;n\n'
    '    en cualquier momento y sin previo aviso si detecta un uso indebido o\n'
    '    contrario a la legislaci&oacute;n aplicable.</p>\n'
    '  </div>\n'
    '</div>\n'
    '<form method="POST" action="/setup/eula">\n'
    '  <div class="check-group">\n'
    '    <input type="checkbox" id="accept-cb">\n'
    '    <label for="accept-cb">He le&iacute;do y acepto los t&eacute;rminos de uso</label>\n'
    '  </div>\n'
    '  <div class="actions">\n'
    '    <div></div>\n'
    '    <button type="submit" class="btn btn-primary" id="next-btn" disabled>'
    'Siguiente &rarr;</button>\n'
    '  </div>\n'
    '</form>'
)

_STEP1_SCRIPT = (
    "document.getElementById('accept-cb').addEventListener('change',function(){"
    "document.getElementById('next-btn').disabled=!this.checked;});"
)


# ---------------------------------------------------------------------------
# Step 2 — Users
# ---------------------------------------------------------------------------

_STEP2_BODY = (
    '<h1>Configuraci&oacute;n inicial</h1>\n'
    '<p class="subtitle">Paso 2 de 4 &mdash; Configurar usuarios</p>\n'
    '<div id="existing-section" style="display:none">\n'
    '  <div class="section">\n'
    '    <div class="section-title">Usuarios existentes</div>\n'
    '    <div id="existing-list"></div>\n'
    '    <p style="font-size:12px;color:var(--dim);margin-top:8px">'
    'Ya hay usuarios configurados. Puedes a&ntilde;adir m&aacute;s o continuar al siguiente paso.</p>\n'
    '  </div>\n'
    '</div>\n'
    '<div id="admin-section" class="section">\n'
    '  <div class="section-title">Cuenta de administrador</div>\n'
    '  <div class="input-group">\n'
    '    <label for="admin-user">Usuario</label>\n'
    '    <input type="text" id="admin-user" value="admin" autocomplete="off">\n'
    '  </div>\n'
    '  <div class="input-group">\n'
    '    <label for="admin-pass">Contrase&ntilde;a</label>\n'
    '    <input type="password" id="admin-pass" autocomplete="new-password"'
    ' placeholder="M&iacute;nimo 8 caracteres">\n'
    '    <span class="hint" id="pass-hint"></span>\n'
    '  </div>\n'
    '  <div class="input-group">\n'
    '    <label for="admin-pass2">Confirmar contrase&ntilde;a</label>\n'
    '    <input type="password" id="admin-pass2" autocomplete="new-password">\n'
    '    <span class="hint" id="pass2-hint"></span>\n'
    '  </div>\n'
    '</div>\n'
    '<div class="section">\n'
    '  <div class="section-title">Usuarios adicionales (opcional)</div>\n'
    '  <div id="users-list"></div>\n'
    '  <button type="button" class="btn btn-sm btn-outline" id="add-user-btn">'
    '+ A&ntilde;adir usuario</button>\n'
    '  <div id="add-user-form" style="display:none;margin-top:12px;padding:16px;'
    'background:var(--bg);border:1px solid var(--border);border-radius:6px">\n'
    '    <div class="input-group">\n'
    '      <label for="new-user">Usuario</label>\n'
    '      <input type="text" id="new-user" autocomplete="off">\n'
    '    </div>\n'
    '    <div class="input-group">\n'
    '      <label for="new-pass">Contrase&ntilde;a</label>\n'
    '      <input type="password" id="new-pass" autocomplete="new-password"'
    ' placeholder="M&iacute;nimo 8 caracteres">\n'
    '    </div>\n'
    '    <div class="input-group">\n'
    '      <label for="new-role">Rol</label>\n'
    '      <select id="new-role">\n'
    '        <option value="user">User</option>\n'
    '        <option value="viewer">Viewer</option>\n'
    '      </select>\n'
    '    </div>\n'
    '    <div style="display:flex;gap:8px">\n'
    '      <button type="button" class="btn btn-sm btn-primary" id="confirm-add-btn">'
    'A&ntilde;adir</button>\n'
    '      <button type="button" class="btn btn-sm" id="cancel-add-btn">Cancelar</button>\n'
    '    </div>\n'
    '  </div>\n'
    '</div>\n'
    '<div class="msg" id="msg"></div>\n'
    '<div class="actions">\n'
    '  <a href="/setup/eula" class="btn btn-outline">&larr; Atr&aacute;s</a>\n'
    '  <button type="button" class="btn btn-primary" id="next-btn">Siguiente &rarr;</button>\n'
    '</div>'
)

_STEP2_SCRIPT = r"""
var additionalUsers=[];
var hasExisting=(typeof _existing!=='undefined')&&_existing.length>0;

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function showMsg(t,err){var m=document.getElementById('msg');m.textContent=t;m.className='msg'+(err?' err':'')}
function roleBadge(r){var c=r==='admin'?'blue':r==='user'?'green':'yellow';return'<span class="badge badge-'+c+'">'+esc(r)+'</span>'}

if(hasExisting){
  document.getElementById('existing-section').style.display='';
  document.getElementById('existing-list').innerHTML=_existing.map(function(u){
    return'<div class="item-row"><div class="item-info"><strong>'+esc(u.username)+'</strong> '+roleBadge(u.role)+'</div></div>'
  }).join('');
  document.getElementById('admin-user').value='';
  document.getElementById('admin-pass').placeholder='Dejar vacío para omitir';
}

function renderUsers(){
  var list=document.getElementById('users-list');
  if(!additionalUsers.length){list.innerHTML='';return}
  list.innerHTML=additionalUsers.map(function(u,i){
    return'<div class="item-row"><div class="item-info"><strong>'+esc(u.username)+'</strong> '+roleBadge(u.role)+'</div>'+
      '<button class="btn btn-sm btn-danger" onclick="removeUser('+i+')">Eliminar</button></div>'
  }).join('')}

function removeUser(i){additionalUsers.splice(i,1);renderUsers()}

document.getElementById('add-user-btn').onclick=function(){
  document.getElementById('add-user-form').style.display='';
  document.getElementById('new-user').focus()};
document.getElementById('cancel-add-btn').onclick=function(){
  document.getElementById('add-user-form').style.display='none'};

document.getElementById('confirm-add-btn').onclick=function(){
  var un=document.getElementById('new-user').value.trim();
  var pw=document.getElementById('new-pass').value;
  var role=document.getElementById('new-role').value;
  if(!un){showMsg('Nombre de usuario requerido',true);return}
  if(pw.length<8){showMsg('Contraseña mínimo 8 caracteres',true);return}
  var adminU=document.getElementById('admin-user').value.trim();
  if(un===adminU||additionalUsers.some(function(u){return u.username===un})){
    showMsg('Nombre de usuario duplicado',true);return}
  additionalUsers.push({username:un,password:pw,role:role});
  renderUsers();showMsg('');
  document.getElementById('add-user-form').style.display='none';
  document.getElementById('new-user').value='';
  document.getElementById('new-pass').value='';
  document.getElementById('new-role').value='user'};

document.getElementById('admin-pass').oninput=function(){
  var h=document.getElementById('pass-hint');
  if(this.value.length>0&&this.value.length<8){h.textContent='Mínimo 8 caracteres ('+this.value.length+'/8)';h.style.color='var(--red)'}
  else if(this.value.length>=8){h.textContent='✓ Longitud válida';h.style.color='var(--green)'}
  else{h.textContent=''}};

document.getElementById('admin-pass2').oninput=function(){
  var h=document.getElementById('pass2-hint');
  if(this.value&&this.value!==document.getElementById('admin-pass').value){
    h.textContent='Las contraseñas no coinciden';h.style.color='var(--red)'}
  else if(this.value){h.textContent='✓ Coinciden';h.style.color='var(--green)'}
  else{h.textContent=''}};

document.getElementById('next-btn').onclick=function(){
  var adminUser=document.getElementById('admin-user').value.trim();
  var adminPass=document.getElementById('admin-pass').value;
  var adminPass2=document.getElementById('admin-pass2').value;
  if(hasExisting&&!adminUser&&!adminPass&&additionalUsers.length===0){
    submitData({skip:true});return}
  if(adminUser||adminPass){
    if(!adminUser){showMsg('Nombre de admin requerido',true);return}
    if(adminPass.length<8){showMsg('La contraseña debe tener al menos 8 caracteres',true);return}
    if(adminPass!==adminPass2){showMsg('Las contraseñas no coinciden',true);return}
    submitData({admin:{username:adminUser,password:adminPass},users:additionalUsers})
  }else if(hasExisting&&additionalUsers.length>0){
    submitData({admin:null,users:additionalUsers})
  }else{
    showMsg('Configura al menos una cuenta de administrador',true)}};

function submitData(body){
  var btn=document.getElementById('next-btn');
  btn.disabled=true;showMsg('');
  fetch('/setup/users',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){location.href=d.next}
    else{showMsg(d.error||'Error',true);btn.disabled=false}})
  .catch(function(){showMsg('Error de conexión',true);btn.disabled=false})}
"""


# ---------------------------------------------------------------------------
# Step 3 — Plugins
# ---------------------------------------------------------------------------

_STEP3_BODY = (
    '<h1>Configuraci&oacute;n inicial</h1>\n'
    '<p class="subtitle">Paso 3 de 4 &mdash; Plugins</p>\n'
    '<p style="color:var(--muted);font-size:13px;margin-bottom:20px">'
    'Los plugins son fuentes de canales en formato M3U. Puedes a&ntilde;adir '
    'plugins ahora o hacerlo despu&eacute;s desde el panel de administraci&oacute;n.</p>\n'
    '<div id="existing-plugins" style="display:none">\n'
    '  <div class="section">\n'
    '    <div class="section-title">Plugins existentes</div>\n'
    '    <div id="existing-list"></div>\n'
    '  </div>\n'
    '</div>\n'
    '<div class="section">\n'
    '  <div class="section-title">A&ntilde;adir plugin</div>\n'
    '  <div class="input-group">\n'
    '    <label for="p-name">Nombre del plugin</label>\n'
    '    <input type="text" id="p-name" placeholder="Ej: Mi Lista" autocomplete="off">\n'
    '  </div>\n'
    '  <div class="input-group">\n'
    '    <label for="p-url">URL de la fuente M3U</label>\n'
    '    <input type="url" id="p-url" placeholder="https://..." autocomplete="off">\n'
    '  </div>\n'
    '  <div style="display:flex;gap:12px">\n'
    '    <div class="input-group" style="flex:1">\n'
    '      <label for="p-interval">Refresco (minutos)</label>\n'
    '      <input type="number" id="p-interval" value="60" min="1">\n'
    '    </div>\n'
    '    <div class="input-group" style="flex:1">\n'
    '      <label style="visibility:hidden">_</label>\n'
    '      <div style="display:flex;align-items:center;gap:8px;height:42px">\n'
    '        <input type="checkbox" id="p-enabled" checked'
    ' style="width:18px;height:18px;accent-color:var(--blue)">\n'
    '        <label for="p-enabled" style="font-size:13px;color:var(--text);'
    'text-transform:none;cursor:pointer">Habilitado</label>\n'
    '      </div>\n'
    '    </div>\n'
    '  </div>\n'
    '  <button type="button" class="btn btn-sm btn-primary" id="add-plugin-btn">'
    'A&ntilde;adir plugin</button>\n'
    '</div>\n'
    '<div class="section">\n'
    '  <div class="section-title">Plugins a crear</div>\n'
    '  <div id="plugins-list"></div>\n'
    '  <p id="no-plugins" style="color:var(--dim);font-size:13px">'
    'No se han a&ntilde;adido plugins a&uacute;n.</p>\n'
    '</div>\n'
    '<p style="color:var(--dim);font-size:12px;margin-bottom:4px">'
    'Este paso es opcional. Puedes a&ntilde;adir plugins m&aacute;s tarde.</p>\n'
    '<div class="msg" id="msg"></div>\n'
    '<div class="actions">\n'
    '  <a href="/setup/users" class="btn btn-outline">&larr; Atr&aacute;s</a>\n'
    '  <div class="actions-right">\n'
    '    <button type="button" class="btn btn-outline" id="skip-btn">'
    'Omitir este paso</button>\n'
    '    <button type="button" class="btn btn-primary" id="next-btn">'
    'Siguiente &rarr;</button>\n'
    '  </div>\n'
    '</div>'
)

_STEP3_SCRIPT = r"""
var plugins=[];
var hasExistingPlugins=(typeof _existingPlugins!=='undefined')&&_existingPlugins.length>0;

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function showMsg(t,err){var m=document.getElementById('msg');m.textContent=t;m.className='msg'+(err?' err':'')}

if(hasExistingPlugins){
  document.getElementById('existing-plugins').style.display='';
  document.getElementById('existing-list').innerHTML=_existingPlugins.map(function(p){
    var url=esc(p.source_url||'');if(url.length>40)url=url.substring(0,40)+'…';
    return'<div class="item-row"><div class="item-detail"><span class="item-name">'+esc(p.display_name)+'</span>'+
      '<span class="item-meta">'+url+'</span></div>'+
      '<span class="badge '+(p.enabled?'badge-green':'badge-muted')+'">'+(p.enabled?'Habilitado':'Deshabilitado')+'</span></div>'
  }).join('')}

function renderPlugins(){
  var list=document.getElementById('plugins-list');
  var noP=document.getElementById('no-plugins');
  if(!plugins.length){list.innerHTML='';noP.style.display='';return}
  noP.style.display='none';
  list.innerHTML=plugins.map(function(p,i){
    var url=esc(p.source_url);if(url.length>40)url=url.substring(0,40)+'…';
    return'<div class="item-row"><div class="item-detail"><span class="item-name">'+esc(p.display_name)+'</span>'+
      '<span class="item-meta">'+url+' · '+p.refresh_minutes+' min</span></div>'+
      '<button class="btn btn-sm btn-danger" onclick="removePlugin('+i+')">Eliminar</button></div>'
  }).join('')}

function removePlugin(i){plugins.splice(i,1);renderPlugins()}

document.getElementById('add-plugin-btn').onclick=function(){
  var name=document.getElementById('p-name').value.trim();
  var url=document.getElementById('p-url').value.trim();
  var interval=parseInt(document.getElementById('p-interval').value)||60;
  var enabled=document.getElementById('p-enabled').checked;
  if(!name){showMsg('Nombre del plugin requerido',true);return}
  if(!url){showMsg('URL de la fuente requerida',true);return}
  plugins.push({display_name:name,source_url:url,refresh_minutes:interval,enabled:enabled});
  renderPlugins();showMsg('');
  document.getElementById('p-name').value='';
  document.getElementById('p-url').value='';
  document.getElementById('p-interval').value='60';
  document.getElementById('p-enabled').checked=true};

function submitPlugins(skip){
  var body=skip?{skip:true}:{plugins:plugins};
  var btn=document.getElementById(skip?'skip-btn':'next-btn');
  btn.disabled=true;showMsg('');
  fetch('/setup/plugins',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){location.href=d.next}
    else{showMsg(d.error||'Error',true);btn.disabled=false}})
  .catch(function(){showMsg('Error de conexión',true);btn.disabled=false})}

document.getElementById('next-btn').onclick=function(){submitPlugins(false)};
document.getElementById('skip-btn').onclick=function(){submitPlugins(true)};
"""


# ---------------------------------------------------------------------------
# Wizard GET routes
# ---------------------------------------------------------------------------

@setup_bp.route("/setup")
@setup_bp.route("/setup/eula")
def setup_eula_page():
    if not setup_store.is_setup_required():
        return redirect("/")
    html = _render_page("Configuraci&oacute;n inicial", "eula",
                        _STEP1_BODY, _STEP1_SCRIPT)
    return Response(html, content_type="text/html; charset=utf-8")


@setup_bp.route("/setup/users")
def setup_users_page():
    if not setup_store.is_setup_required():
        return redirect("/")
    if setup_store.get_current_step() is None:
        return redirect("/setup")
    existing = auth_store.get_all_users()
    data_js = ""
    if existing:
        data_js = "var _existing=" + json.dumps(existing, ensure_ascii=False) + ";\n"
    html = _render_page("Usuarios", "users", _STEP2_BODY, data_js + _STEP2_SCRIPT)
    return Response(html, content_type="text/html; charset=utf-8")


@setup_bp.route("/setup/plugins")
def setup_plugins_page():
    if not setup_store.is_setup_required():
        return redirect("/")
    if not _step_reached(setup_store.get_current_step(), "users"):
        return redirect("/setup/users")
    existing = plugin_store.get_all()
    data_js = ""
    if existing:
        safe = [{"display_name": p["display_name"],
                 "source_url": p.get("source_url", ""),
                 "enabled": p["enabled"]} for p in existing]
        data_js = "var _existingPlugins=" + json.dumps(safe, ensure_ascii=False) + ";\n"
    html = _render_page("Plugins", "plugins", _STEP3_BODY, data_js + _STEP3_SCRIPT)
    return Response(html, content_type="text/html; charset=utf-8")


@setup_bp.route("/setup/summary")
def setup_summary_page():
    if not setup_store.is_setup_required():
        return redirect("/")
    if not _step_reached(setup_store.get_current_step(), "plugins"):
        return redirect("/setup/plugins")

    ip = _client_ip()
    eula_st = eula_store.status(ip)
    users = auth_store.get_all_users()
    plugins = plugin_store.get_all()

    body = '<h1>Configuraci&oacute;n inicial</h1>\n'
    body += '<p class="subtitle">Paso 4 de 4 &mdash; Resumen</p>\n'

    body += '<div class="summary-section">\n'
    body += '<div class="summary-title">EULA '
    body += '<span class="badge badge-green">Aceptado</span></div>\n'
    if eula_st.get("accepted_at"):
        body += ('<p style="font-size:12px;color:var(--dim)">Aceptado: '
                 + _esc(eula_st["accepted_at"]) + '</p>\n')
    body += '</div>\n'

    body += '<div class="summary-section">\n'
    body += '<div class="summary-title">Usuarios</div>\n'
    if users:
        body += '<table><thead><tr><th>Usuario</th><th>Rol</th><th>Estado</th></tr></thead><tbody>\n'
        for u in users:
            rc = "blue" if u["role"] == "admin" else ("green" if u["role"] == "user" else "yellow")
            body += '<tr><td><strong>' + _esc(u["username"]) + '</strong></td>'
            body += '<td><span class="badge badge-' + rc + '">' + _esc(u["role"]) + '</span></td>'
            body += '<td><span class="badge badge-green">Activo</span></td></tr>\n'
        body += '</tbody></table>\n'
    else:
        body += '<p style="color:var(--muted);font-size:13px">No hay usuarios.</p>\n'
    body += '</div>\n'

    body += '<div class="summary-section">\n'
    body += '<div class="summary-title">Plugins</div>\n'
    if plugins:
        body += '<table><thead><tr><th>Nombre</th><th>URL</th><th>Refresco</th><th>Estado</th></tr></thead><tbody>\n'
        for p in plugins:
            url = _esc(p.get("source_url") or "")
            if len(url) > 40:
                url = url[:40] + "&hellip;"
            interval_min = (p.get("refresh_interval") or 3600) // 60
            body += '<tr><td>' + _esc(p["display_name"]) + '</td>'
            body += '<td style="font-size:12px;color:var(--muted)">' + url + '</td>'
            body += '<td>' + str(interval_min) + ' min</td>'
            body += '<td><span class="badge badge-green">Habilitado</span></td></tr>\n'
        body += '</tbody></table>\n'
    else:
        body += ('<p style="color:var(--muted);font-size:13px">'
                 'No se configuraron plugins. Puedes a&ntilde;adirlos desde el panel.</p>\n')
    body += '</div>\n'

    admin_user = next((u for u in users if u["role"] == "admin"), None)
    body += '<div class="info-box">\n'
    body += '<strong>Acceso:</strong><br>\n'
    if admin_user:
        body += 'Usuario admin: <code>' + _esc(admin_user["username"]) + '</code><br>\n'
    body += 'Panel: <code>/panel</code><br>\n'
    body += 'Playlists: <code>/&lt;plugin&gt;/mpegts.m3u</code>\n'
    body += '</div>\n'

    body += '<div class="actions">\n'
    body += '<a href="/setup/plugins" class="btn btn-outline">&larr; Atr&aacute;s</a>\n'
    body += '<form method="POST" action="/setup/summary" style="margin:0">\n'
    body += '<button type="submit" class="btn btn-primary">Finalizar configuraci&oacute;n</button>\n'
    body += '</form>\n'
    body += '</div>'

    html = _render_page("Resumen", "summary", body)
    return Response(html, content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Wizard POST routes
# ---------------------------------------------------------------------------

@setup_bp.route("/setup/eula", methods=["POST"])
def setup_eula_post():
    if not setup_store.is_setup_required():
        return redirect("/")
    ip = _client_ip()
    ua = request.headers.get("User-Agent", "")
    eula_store.accept(ip, ua, "1.0", eula_store.EXPECTED_PHRASE)
    if setup_store.get_current_step() is None:
        setup_store.set_state("setup_started_at",
                              datetime.now(timezone.utc).isoformat())
    setup_store.set_state("setup_step", "eula")
    log_event("info", "setup_eula_accepted", COMPONENT, ip=ip)
    return redirect("/setup/users")


@setup_bp.route("/setup/users", methods=["POST"])
def setup_users_post():
    if not setup_store.is_setup_required():
        return jsonify({"ok": False, "error": "Setup ya completado"}), 409

    data = request.get_json(silent=True) or {}

    if data.get("skip") and auth_store.user_count() > 0:
        setup_store.set_state("setup_step", "users")
        return jsonify({"ok": True, "next": "/setup/plugins"})

    admin_data = data.get("admin")
    users_data = data.get("users", [])

    if not admin_data or not admin_data.get("username"):
        if auth_store.user_count() > 0 and users_data:
            all_names = []
            for u in users_data:
                name = (u.get("username") or "").strip()
                pw = u.get("password", "")
                if not name:
                    return jsonify({"ok": False, "error": "Nombre de usuario requerido"}), 400
                if len(pw) < 8:
                    return jsonify({"ok": False,
                                    "error": "La contraseña de '" + name + "' debe tener al menos 8 caracteres"}), 400
                if name in all_names:
                    return jsonify({"ok": False, "error": "Nombre duplicado: '" + name + "'"}), 400
                if auth_store.get_user_by_username(name):
                    return jsonify({"ok": False, "error": "El usuario '" + name + "' ya existe"}), 409
                all_names.append(name)
            for u in users_data:
                auth_store.create_user(u["username"].strip(), u["password"],
                                       role=u.get("role", "user"))
            setup_store.set_state("setup_step", "users")
            log_event("info", "setup_users_created", COMPONENT, count=len(users_data))
            return jsonify({"ok": True, "next": "/setup/plugins"})
        return jsonify({"ok": False, "error": "Datos de admin requeridos"}), 400

    admin_user = (admin_data.get("username") or "").strip()
    admin_pass = admin_data.get("password", "")

    if not admin_user:
        return jsonify({"ok": False, "error": "Nombre de admin requerido"}), 400
    if len(admin_pass) < 8:
        return jsonify({"ok": False,
                        "error": "La contraseña del admin debe tener al menos 8 caracteres"}), 400

    all_names = [admin_user]
    for u in users_data:
        name = (u.get("username") or "").strip()
        pw = u.get("password", "")
        if not name:
            return jsonify({"ok": False, "error": "Nombre de usuario requerido"}), 400
        if len(pw) < 8:
            return jsonify({"ok": False,
                            "error": "La contraseña de '" + name + "' debe tener al menos 8 caracteres"}), 400
        if name in all_names:
            return jsonify({"ok": False, "error": "Nombre duplicado: '" + name + "'"}), 400
        all_names.append(name)

    for name in all_names:
        if auth_store.get_user_by_username(name):
            return jsonify({"ok": False, "error": "El usuario '" + name + "' ya existe"}), 409

    auth_store.create_user(admin_user, admin_pass, role="admin")
    for u in users_data:
        auth_store.create_user(u["username"].strip(), u["password"],
                               role=u.get("role", "user"))

    setup_store.set_state("setup_step", "users")
    log_event("info", "setup_users_created", COMPONENT, count=1 + len(users_data))
    return jsonify({"ok": True, "next": "/setup/plugins"})


@setup_bp.route("/setup/plugins", methods=["POST"])
def setup_plugins_post():
    if not setup_store.is_setup_required():
        return jsonify({"ok": False, "error": "Setup ya completado"}), 409

    data = request.get_json(silent=True) or {}

    if data.get("skip"):
        setup_store.set_state("setup_step", "plugins")
        return jsonify({"ok": True, "next": "/setup/summary"})

    plugins_data = data.get("plugins", [])
    created = []
    for p in plugins_data:
        display_name = (p.get("display_name") or "").strip()
        if not display_name:
            continue
        name = _slugify(display_name)
        if plugin_store.get_by_name(name):
            continue
        refresh_minutes = p.get("refresh_minutes", 60)
        plugin = plugin_store.create({
            "name": name,
            "display_name": display_name,
            "source_type": "url",
            "source_url": p.get("source_url", ""),
            "refresh_interval": int(refresh_minutes) * 60,
            "enabled": p.get("enabled", True),
        })
        created.append({"name": name, "display_name": display_name})

    setup_store.set_state("setup_step", "plugins")
    log_event("info", "setup_plugins_created", COMPONENT, count=len(created))
    return jsonify({"ok": True, "next": "/setup/summary"})


@setup_bp.route("/setup/summary", methods=["POST"])
def setup_summary_post():
    if not setup_store.is_setup_required():
        return redirect("/")
    now = datetime.now(timezone.utc).isoformat()
    setup_store.set_state("setup_completed", "true")
    setup_store.set_state("setup_completed_at", now)
    plugin_refresh.bootstrap_all()
    log_event("info", "setup_completed", COMPONENT)
    return redirect("/login")


# ---------------------------------------------------------------------------
# API — Status
# ---------------------------------------------------------------------------

@setup_bp.route("/api/setup/status")
def api_setup_status():
    if not setup_store.is_setup_required():
        return jsonify({
            "setup_required": False,
            "setup_completed": True,
            "completed_at": setup_store.get_state("setup_completed_at"),
        })
    current = setup_store.get_current_step()
    return jsonify({
        "setup_required": True,
        "setup_completed": False,
        "current_step": current,
        "steps": {
            "eula": {"completed": _step_reached(current, "eula")},
            "users": {"completed": _step_reached(current, "users")},
            "plugins": {"completed": _step_reached(current, "plugins"), "required": False},
            "summary": {"completed": False},
        },
    })


# ---------------------------------------------------------------------------
# API — Step-by-step
# ---------------------------------------------------------------------------

@setup_bp.route("/api/setup/eula", methods=["POST"])
def api_eula_step():
    if not setup_store.is_setup_required():
        return jsonify({"status": "error", "message": "Setup ya completado"}), 409
    data = request.get_json(silent=True) or {}
    if not data.get("accepted"):
        return jsonify({"status": "error",
                        "message": "Debes aceptar el EULA para continuar"}), 400
    ip = _client_ip()
    ua = request.headers.get("User-Agent", "")
    eula_store.accept(ip, ua, "1.0", eula_store.EXPECTED_PHRASE)
    if setup_store.get_current_step() is None:
        setup_store.set_state("setup_started_at",
                              datetime.now(timezone.utc).isoformat())
    setup_store.set_state("setup_step", "eula")
    return jsonify({"status": "ok", "step": "eula", "next_step": "users"})


@setup_bp.route("/api/setup/users", methods=["POST"])
def api_users_step():
    if not setup_store.is_setup_required():
        return jsonify({"status": "error", "message": "Setup ya completado"}), 409
    if not _step_reached(setup_store.get_current_step(), "eula"):
        return jsonify({"status": "error",
                        "message": "Debes completar el paso anterior (EULA)"}), 400

    data = request.get_json(silent=True) or {}
    admin_data = data.get("admin", {})
    users_data = data.get("users", [])

    admin_user = (admin_data.get("username") or "").strip()
    admin_pass = admin_data.get("password", "")
    if not admin_user or not admin_pass:
        return jsonify({"status": "error",
                        "message": "admin.username y admin.password son obligatorios"}), 400
    if len(admin_pass) < 8:
        return jsonify({"status": "error",
                        "message": "La contraseña del admin debe tener al menos 8 caracteres"}), 400

    all_names = [admin_user]
    for u in users_data:
        name = (u.get("username") or "").strip()
        pw = u.get("password", "")
        if not name:
            return jsonify({"status": "error", "message": "Nombre de usuario requerido"}), 400
        if len(pw) < 8:
            return jsonify({"status": "error",
                            "message": "La contraseña de '" + name + "' debe tener al menos 8 caracteres"}), 400
        if name in all_names:
            return jsonify({"status": "error", "message": "Nombre duplicado: '" + name + "'"}), 400
        all_names.append(name)

    for name in all_names:
        if auth_store.get_user_by_username(name):
            return jsonify({"status": "error",
                            "message": "El usuario '" + name + "' ya existe"}), 409

    auth_store.create_user(admin_user, admin_pass, role="admin")
    created = [{"username": admin_user, "role": "admin"}]
    for u in users_data:
        role = u.get("role", "user")
        auth_store.create_user(u["username"].strip(), u["password"], role=role)
        created.append({"username": u["username"].strip(), "role": role})

    setup_store.set_state("setup_step", "users")
    return jsonify({"status": "ok", "step": "users", "next_step": "plugins",
                    "users_created": created}), 201


@setup_bp.route("/api/setup/plugins", methods=["POST"])
def api_plugins_step():
    if not setup_store.is_setup_required():
        return jsonify({"status": "error", "message": "Setup ya completado"}), 409
    if not _step_reached(setup_store.get_current_step(), "users"):
        return jsonify({"status": "error",
                        "message": "Debes completar el paso anterior (Usuarios)"}), 400

    data = request.get_json(silent=True) or {}
    if data.get("skip"):
        setup_store.set_state("setup_step", "plugins")
        return jsonify({"status": "ok", "step": "plugins", "next_step": "summary",
                        "plugins_created": []}), 201

    plugins_data = data.get("plugins", [])
    created = []
    for p in plugins_data:
        display_name = (p.get("display_name") or "").strip()
        if not display_name:
            continue
        name = _slugify(display_name)
        if plugin_store.get_by_name(name):
            continue
        refresh_minutes = p.get("refresh_minutes", 60)
        plugin = plugin_store.create({
            "name": name,
            "display_name": display_name,
            "source_type": "url",
            "source_url": p.get("source_url", ""),
            "refresh_interval": int(refresh_minutes) * 60,
            "enabled": p.get("enabled", True),
        })
        created.append({"name": name, "display_name": display_name,
                        "source_url": p.get("source_url", "")})

    setup_store.set_state("setup_step", "plugins")
    return jsonify({"status": "ok", "step": "plugins", "next_step": "summary",
                    "plugins_created": created}), 201


@setup_bp.route("/api/setup/finalize", methods=["POST"])
def api_finalize_step():
    if not setup_store.is_setup_required():
        return jsonify({"status": "error", "message": "Setup ya completado"}), 409
    if not _step_reached(setup_store.get_current_step(), "plugins"):
        return jsonify({"status": "error",
                        "message": "Debes completar los pasos anteriores"}), 400

    now = datetime.now(timezone.utc).isoformat()
    setup_store.set_state("setup_completed", "true")
    setup_store.set_state("setup_completed_at", now)

    users = auth_store.get_all_users()
    admin_user = next((u for u in users if u["role"] == "admin"), None)
    admin_token = None
    if admin_user:
        token = auth_store.create_token(admin_user["id"],
                                        description="Token generado en setup")
        admin_token = token["token"]

    plugin_refresh.bootstrap_all()
    plugins = plugin_store.get_all()

    log_event("info", "setup_completed_api", COMPONENT)
    return jsonify({
        "status": "ok",
        "message": "Configuración inicial completada",
        "setup_completed": True,
        "summary": {
            "eula_accepted": True,
            "users": [{"username": u["username"], "role": u["role"]} for u in users],
            "plugins": [{"name": p["name"], "source_url": p.get("source_url")}
                        for p in plugins],
            "admin_token": admin_token,
        },
    })


# ---------------------------------------------------------------------------
# API — All-in-one
# ---------------------------------------------------------------------------

@setup_bp.route("/api/setup/complete", methods=["POST"])
def api_setup_complete():
    if not setup_store.is_setup_required():
        return jsonify({"status": "error",
                        "message": "La configuración inicial ya fue completada"}), 409

    data = request.get_json(silent=True) or {}
    eula_data = data.get("eula", {})
    admin_data = data.get("admin", {})
    users_data = data.get("users", [])
    plugins_data = data.get("plugins", [])

    if not eula_data.get("accepted"):
        return jsonify({"status": "error", "message": "Debes aceptar el EULA"}), 400

    admin_user = (admin_data.get("username") or "").strip()
    admin_pass = admin_data.get("password", "")
    if not admin_user or not admin_pass:
        return jsonify({"status": "error",
                        "message": "admin.username y admin.password son obligatorios"}), 400
    if len(admin_pass) < 8:
        return jsonify({"status": "error",
                        "message": "La contraseña del admin debe tener al menos 8 caracteres"}), 400

    all_names = [admin_user]
    for u in users_data:
        name = (u.get("username") or "").strip()
        pw = u.get("password", "")
        if not name:
            return jsonify({"status": "error", "message": "Nombre de usuario requerido"}), 400
        if len(pw) < 8:
            return jsonify({"status": "error",
                            "message": "La contraseña de '" + name + "' debe tener al menos 8 caracteres"}), 400
        if name in all_names:
            return jsonify({"status": "error", "message": "Nombre duplicado: '" + name + "'"}), 400
        all_names.append(name)

    now = datetime.now(timezone.utc).isoformat()
    ip = _client_ip()
    ua = request.headers.get("User-Agent", "")

    eula_store.accept(ip, ua, "1.0", eula_store.EXPECTED_PHRASE)

    admin = auth_store.create_user(admin_user, admin_pass, role="admin")
    users_created = [{"username": admin_user, "role": "admin"}]
    for u in users_data:
        name = u["username"].strip()
        role = u.get("role", "user")
        auth_store.create_user(name, u["password"], role=role)
        users_created.append({"username": name, "role": role})

    plugins_created = []
    for p in plugins_data:
        display_name = (p.get("display_name") or "").strip()
        if not display_name:
            continue
        name = _slugify(display_name)
        if plugin_store.get_by_name(name):
            continue
        refresh_minutes = p.get("refresh_minutes", 60)
        plugin_store.create({
            "name": name,
            "display_name": display_name,
            "source_type": "url",
            "source_url": p.get("source_url", ""),
            "refresh_interval": int(refresh_minutes) * 60,
            "enabled": p.get("enabled", True),
        })
        plugins_created.append({"name": name, "display_name": display_name,
                                "source_url": p.get("source_url", "")})

    token = auth_store.create_token(admin["id"],
                                    description="Token generado en setup")

    setup_store.set_state("setup_started_at", now)
    setup_store.set_state("setup_step", "plugins")
    setup_store.set_state("setup_completed", "true")
    setup_store.set_state("setup_completed_at", now)

    plugin_refresh.bootstrap_all()
    log_event("info", "setup_completed_api", COMPONENT)

    return jsonify({
        "status": "ok",
        "message": "Configuración inicial completada",
        "data": {
            "eula_accepted": True,
            "users_created": users_created,
            "plugins_created": plugins_created,
            "admin_token": token["token"],
        },
    }), 201


# ---------------------------------------------------------------------------
# API — Reset
# ---------------------------------------------------------------------------

@setup_bp.route("/api/setup/reset", methods=["POST"])
@require_role("admin")
def api_setup_reset():
    setup_store.set_state("setup_completed", "false")
    setup_store.set_state("setup_step", "")
    log_event("info", "setup_reset", COMPONENT)
    return jsonify({"status": "ok",
                    "message": "Setup reseteado. Accede a /setup para reiniciar la configuración."})
