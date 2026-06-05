from flask import Blueprint, Response, jsonify, request

from app.utils import eula_store
from app.utils.logging_utils import log_event

eula_bp = Blueprint("eula", __name__)
COMPONENT = "eula"


@eula_bp.route("/eula")
def eula_page():
    return Response(_EULA_HTML, content_type="text/html; charset=utf-8")


def _client_ip():
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.headers.get("X-Real-IP")
        or request.remote_addr
    )


@eula_bp.route("/api/eula/accept", methods=["POST"])
def eula_accept():
    try:
        payload = request.get_json(silent=True) or {}
        phrase = payload.get("phrase", "")
        version = payload.get("version", "1.0")
        ip = _client_ip()
        ua = request.headers.get("User-Agent", "")

        result = eula_store.accept(ip, ua, version, phrase)
        if result is None:
            return jsonify({"ok": False, "error": "La frase no coincide."}), 400

        log_event("info", "eula_accepted", COMPONENT, ip=ip, consent_id=result["consent_id"])
        return jsonify({
            "ok": True,
            "consent_id": result["consent_id"],
            "accepted_at": result["accepted_at"],
        })
    except Exception as e:
        log_event("error", "eula_accept_failed", COMPONENT, error=str(e))
        return jsonify({"ok": False, "error": "Error interno."}), 500


@eula_bp.route("/api/eula/revoke", methods=["POST"])
def eula_revoke():
    try:
        ip = _client_ip()
        eula_store.revoke(ip)
        log_event("info", "eula_revoked", COMPONENT, ip=ip)
        return jsonify({"ok": True})
    except Exception as e:
        log_event("error", "eula_revoke_failed", COMPONENT, error=str(e))
        return jsonify({"ok": False, "error": "Error interno."}), 500


@eula_bp.route("/api/eula/status")
def eula_status():
    try:
        ip = _client_ip()
        return jsonify(eula_store.status(ip))
    except Exception as e:
        log_event("error", "eula_status_failed", COMPONENT, error=str(e))
        return jsonify({"accepted": False, "error": "Error interno."}), 500


_EULA_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenAce · Acuerdo de Licencia de Usuario Final</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface-2: #1c2129; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --dim: #484f58;
    --green: #3fb950; --red: #f85149; --blue: #58a6ff; --blue-dim: #1f6feb;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 14px; min-height: 100vh; display: flex; flex-direction: column; }

  .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; display: flex; align-items: center; gap: 12px; }
  .topbar .logo { font-size: 16px; font-weight: 700; color: var(--blue); letter-spacing: -.02em; }
  .topbar .sep { color: var(--dim); }
  .topbar .page { font-size: 14px; color: var(--muted); font-weight: 500; }

  .container { flex: 1; display: flex; align-items: center; justify-content: center; padding: 32px 20px; }
  .doc { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; max-width: 640px; width: 100%; overflow: hidden; }

  .doc-header { padding: 24px 28px 20px; border-bottom: 1px solid var(--border); }
  .doc-header h1 { font-size: 18px; font-weight: 600; color: var(--text); margin-bottom: 6px; }
  .doc-header .meta { font-size: 12px; color: var(--dim); display: flex; gap: 16px; flex-wrap: wrap; }
  .doc-header .meta span { white-space: nowrap; }

  .doc-body { padding: 0 28px; max-height: 380px; overflow-y: auto; }
  .doc-body::-webkit-scrollbar { width: 6px; }
  .doc-body::-webkit-scrollbar-track { background: transparent; }
  .doc-body::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 3px; }

  .clause { padding: 18px 0; border-bottom: 1px solid rgba(48,54,61,.5); }
  .clause:last-child { border-bottom: none; }
  .clause h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--blue); margin-bottom: 8px; }
  .clause p { font-size: 13px; line-height: 1.75; color: var(--muted); margin-bottom: 8px; }
  .clause p:last-child { margin-bottom: 0; }
  .clause ul { margin: 6px 0 0 18px; font-size: 13px; line-height: 1.75; color: var(--muted); }
  .clause ul li { margin-bottom: 4px; }

  .doc-footer { padding: 20px 28px 24px; border-top: 1px solid var(--border); background: var(--surface-2); }

  .status-row { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }
  .badge { display: inline-flex; align-items: center; gap: 5px; padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge-green { background: rgba(63,185,80,.12); color: var(--green); }
  .badge-muted { background: rgba(139,148,158,.1); color: var(--muted); }
  .badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; }
  .badge-green::before { background: var(--green); }
  .badge-muted::before { background: var(--muted); }
  .status-detail { font-size: 12px; color: var(--dim); }

  .accept-form { display: flex; flex-direction: column; gap: 12px; }
  .input-group { display: flex; flex-direction: column; gap: 5px; }
  .input-group label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .input-group input { background: var(--bg); border: 1px solid var(--border); color: var(--text); border-radius: 6px; padding: 10px 12px; font-size: 14px; width: 100%; transition: border-color .15s; }
  .input-group input:focus { outline: none; border-color: var(--blue-dim); box-shadow: 0 0 0 2px rgba(31,111,235,.25); }
  .input-group input::placeholder { color: var(--dim); }
  .input-group .field-hint { font-size: 11px; color: var(--dim); line-height: 1.4; }

  .btn { display: block; width: 100%; border: none; border-radius: 6px; padding: 11px; font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity .15s; }
  .btn:disabled { opacity: .45; cursor: default; }
  .btn-primary { background: var(--green); color: #fff; }
  .btn-primary:hover:not(:disabled) { opacity: .9; }

  .msg { text-align: center; font-size: 13px; min-height: 20px; }
  .msg.err { color: var(--red); }
  .msg.ok { color: var(--green); }

  .revoke-row { text-align: center; margin-top: 8px; }
  .revoke-row a { color: var(--dim); font-size: 11px; cursor: pointer; text-decoration: none; border-bottom: 1px dashed var(--dim); transition: color .15s; }
  .revoke-row a:hover { color: var(--red); border-color: var(--red); }
</style>
</head>
<body>
<div class="topbar">
  <span class="logo">OpenAce</span>
  <span class="sep">/</span>
  <span class="page">Acuerdo de Licencia</span>
</div>

<div class="container">
<div class="doc">

  <div class="doc-header">
    <h1>Acuerdo de Licencia de Usuario Final (EULA)</h1>
    <div class="meta">
      <span>Versi&oacute;n 1.0</span>
      <span>Vigente desde: 30 de mayo de 2026</span>
      <span>Identificador: EULA-OA-1.0</span>
    </div>
  </div>

  <div class="doc-body">
    <div class="clause">
      <h2>1. Objeto</h2>
      <p>El presente acuerdo regula las condiciones de uso del servicio
      OpenAce (&laquo;la Aplicaci&oacute;n&raquo;), que act&uacute;a como
      proxy HTTP para el motor AceStream, proporcionando agregaci&oacute;n
      de listas de reproducci&oacute;n M3U y transcodificaci&oacute;n HLS
      bajo demanda.</p>
    </div>
    <div class="clause">
      <h2>2. Aceptaci&oacute;n</h2>
      <p>El acceso y uso de la Aplicaci&oacute;n requiere la
      aceptaci&oacute;n &iacute;ntegra de este acuerdo. Dicha
      aceptaci&oacute;n se formaliza mediante la introducci&oacute;n de la
      frase literal indicada al pie de este documento y queda registrada
      con marca temporal, direcci&oacute;n IP de origen y hash
      criptogr&aacute;fico de la frase.</p>
      <p>Estos datos se almacenan localmente en una base de datos SQLite
      (<code>data.db</code>) ubicada en el propio servidor que ejecuta la
      Aplicaci&oacute;n. No se transmiten a servicios externos.</p>
    </div>
    <div class="clause">
      <h2>3. Contenido de terceros</h2>
      <p>La Aplicaci&oacute;n no aloja, produce ni controla los contenidos
      retransmitidos a trav&eacute;s del motor AceStream. Todo el material
      audiovisual es responsabilidad exclusiva de los proveedores de origen
      y de la red P2P. El operador de la Aplicaci&oacute;n no asume
      responsabilidad alguna sobre la legalidad, exactitud o disponibilidad
      de dichos contenidos.</p>
    </div>
    <div class="clause">
      <h2>4. Tratamiento de datos</h2>
      <p>La Aplicaci&oacute;n registra exclusivamente los siguientes datos,
      almacenados en una base de datos local SQLite
      (<code>data.db</code>) en el servidor:</p>
      <ul>
        <li>Direcci&oacute;n IP del usuario, con fines de control de
        consentimiento.</li>
        <li>Cadena User-Agent del navegador.</li>
        <li>Hash SHA-256 de la frase de aceptaci&oacute;n (nunca la frase
        en texto plano).</li>
        <li>Marca temporal de la aceptaci&oacute;n y, en su caso, de la
        revocaci&oacute;n.</li>
      </ul>
      <p>No se almacenan otros datos personales identificativos. Los datos
      no se transmiten a terceros ni a servicios externos; permanecen
      &uacute;nicamente en el fichero <code>data.db</code> del servidor
      local. Las preferencias del usuario se almacenan exclusivamente en
      cookies locales del propio dispositivo.</p>
      <p>El usuario puede revocar su consentimiento en cualquier momento
      desde esta misma p&aacute;gina, lo que inhabilitar&aacute; el acceso
      a la Aplicaci&oacute;n hasta una nueva aceptaci&oacute;n.</p>
    </div>
    <div class="clause">
      <h2>5. Marco legal europeo aplicable</h2>
      <p>El presente EULA se rige por la legislaci&oacute;n de la
      Uni&oacute;n Europea y espa&ntilde;ola. Son de aplicaci&oacute;n,
      entre otras normas:</p>
      <ul>
        <li><strong>Reglamento (UE) 2016/679 (RGPD)</strong> &mdash; La
        Aplicaci&oacute;n no recoge datos personales identificativos. Las
        preferencias del usuario se almacenan exclusivamente en cookies
        locales del propio dispositivo.</li>
        <li><strong>Directiva 2019/790/UE</strong> sobre derechos de autor
        en el mercado &uacute;nico digital &mdash; El usuario asume la
        responsabilidad de respetar los derechos de autor de los contenidos
        a los que acceda.</li>
        <li><strong>Directiva 2000/31/CE (Comercio Electr&oacute;nico)</strong>
        &mdash; El desarrollador, al actuar como mero intermediario
        t&eacute;cnico sin control editorial sobre los contenidos
        enlazados, queda amparado por el r&eacute;gimen de exenci&oacute;n
        de los arts.&nbsp;12&ndash;15 de dicha Directiva.</li>
        <li><strong>Ley 34/2002 (LSSI-CE, Espa&ntilde;a)</strong> &mdash;
        El desarrollador no es responsable de los contenidos accesibles a
        trav&eacute;s de los enlaces externos, conforme al art.&nbsp;17 de
        la citada ley.</li>
        <li><strong>Real Decreto Legislativo 1/1996 (LPI,
        Espa&ntilde;a)</strong> &mdash; Cualquier infracci&oacute;n de los
        derechos de propiedad intelectual derivada del uso de la
        Aplicaci&oacute;n es responsabilidad exclusiva del usuario.</li>
      </ul>
    </div>
    <div class="clause">
      <h2>6. Ausencia de garant&iacute;as</h2>
      <p>La Aplicaci&oacute;n se proporciona &laquo;tal cual&raquo;
      (<em>as is</em>), sin garant&iacute;a de disponibilidad, exactitud o
      idoneidad para ning&uacute;n prop&oacute;sito concreto.</p>
    </div>
    <div class="clause">
      <h2>7. Limitaci&oacute;n de responsabilidad</h2>
      <p>En ning&uacute;n caso el operador ser&aacute; responsable por
      da&ntilde;os directos, indirectos, incidentales, especiales o
      consecuentes derivados del uso o la imposibilidad de uso de la
      Aplicaci&oacute;n, incluyendo p&eacute;rdida de datos o
      interrupci&oacute;n del servicio.</p>
    </div>
    <div class="clause">
      <h2>8. Modificaciones del EULA</h2>
      <p>El desarrollador se reserva el derecho de modificar el presente
      EULA en cualquier momento. El uso continuado de la Aplicaci&oacute;n
      tras la publicaci&oacute;n de cambios implicar&aacute; la
      aceptaci&oacute;n de los nuevos t&eacute;rminos.</p>
    </div>
    <div class="clause">
      <h2>9. Resoluci&oacute;n</h2>
      <p>El operador podr&aacute; revocar el acceso a la Aplicaci&oacute;n
      en cualquier momento y sin previo aviso si detecta un uso indebido o
      contrario a la legislaci&oacute;n aplicable.</p>
    </div>
  </div>

  <div class="doc-footer">
    <div class="status-row" id="status-row">
      <span class="badge badge-muted" id="status-badge">Pendiente de aceptaci&oacute;n</span>
      <span class="status-detail" id="status-detail"></span>
    </div>

    <div id="accept-section" class="accept-form">
      <div class="input-group">
        <label for="phrase">Frase de aceptaci&oacute;n</label>
        <input type="text" id="phrase" placeholder="Escribe aqu&iacute; la frase exacta&hellip;" autocomplete="off">
        <span class="field-hint">Introduce literalmente: <strong>He le&iacute;do y acepto el acuerdo</strong></span>
      </div>
      <button class="btn btn-primary" id="accept-btn">Formalizar aceptaci&oacute;n</button>
    </div>

    <div class="msg" id="msg"></div>
    <div class="revoke-row" id="revoke-row" style="display:none">
      <a id="revoke-link">Revocar consentimiento</a>
    </div>
  </div>

</div>
</div>

<script>
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
let _redir = decodeURIComponent(params.get('redirect') || '/');
if (!_redir.startsWith('/') || _redir.startsWith('//')) _redir = '/';
const redirectTo = _redir;

async function checkStatus() {
  try {
    const r = await fetch('/api/eula/status');
    const d = await r.json();
    if (d.accepted) {
      $('status-badge').className = 'badge badge-green';
      $('status-badge').textContent = 'Aceptado';
      $('status-detail').textContent = 'Consentimiento #' + d.consent_id + ' — ' + d.accepted_at;
      $('accept-section').style.display = 'none';
      $('msg').className = 'msg ok';
      $('msg').textContent = 'Tu consentimiento está registrado. Puedes acceder al servicio.';
      $('revoke-row').style.display = '';
    } else {
      $('status-badge').className = 'badge badge-muted';
      $('status-badge').textContent = 'Pendiente de aceptación';
      $('status-detail').textContent = '';
      $('accept-section').style.display = '';
      $('revoke-row').style.display = 'none';
    }
  } catch(e) {}
}

$('accept-btn').addEventListener('click', async () => {
  const phrase = $('phrase').value.trim();
  if (!phrase) return;
  $('accept-btn').disabled = true;
  $('msg').textContent = '';
  $('msg').className = 'msg';
  try {
    const r = await fetch('/api/eula/accept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phrase, version: '1.0' }),
    });
    const d = await r.json();
    if (d.ok) {
      $('msg').className = 'msg ok';
      $('msg').textContent = 'Consentimiento registrado. Redirigiendo…';
      setTimeout(() => { location.href = redirectTo; }, 1000);
    } else {
      $('msg').className = 'msg err';
      $('msg').textContent = d.error || 'La frase introducida no es correcta.';
      $('accept-btn').disabled = false;
    }
  } catch(e) {
    $('msg').className = 'msg err';
    $('msg').textContent = 'Error de conexión con el servidor.';
    $('accept-btn').disabled = false;
  }
});

$('phrase').addEventListener('keydown', e => { if (e.key === 'Enter') $('accept-btn').click(); });

$('revoke-link').addEventListener('click', async () => {
  if (!confirm('Se revocará tu consentimiento y perderás el acceso al servicio hasta que vuelvas a aceptar. ¿Continuar?')) return;
  try {
    await fetch('/api/eula/revoke', { method: 'POST' });
    $('msg').className = 'msg';
    $('msg').textContent = '';
    $('accept-section').style.display = '';
    $('phrase').value = '';
    $('accept-btn').disabled = false;
    checkStatus();
  } catch(e) {}
});

checkStatus();
</script>
</body>
</html>"""
