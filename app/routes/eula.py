from flask import Blueprint, Response, jsonify, request

from app.utils import eula_store
from app.utils.auth_helpers import get_json_body, require_role
from app.utils.logging_utils import log_event

eula_bp = Blueprint("eula", __name__)
COMPONENT = "eula"


@eula_bp.route("/eula")
def eula_page():
    return Response(_render_eula_html(), content_type="text/html; charset=utf-8")


def _client_ip():
    return request.remote_addr


@eula_bp.route("/api/eula/accept", methods=["POST"])
def eula_accept():
    try:
        payload, jerr = get_json_body()
        if jerr:
            return jerr
        version = payload.get("version", "1.0")
        ip = _client_ip()
        ua = request.headers.get("User-Agent", "")

        # Acceptance flow uses the checkbox. We accept both an explicit
        # ``accepted: true`` JSON flag and a legacy ``phrase`` for backwards
        # compatibility, but reject any request that does not signal genuine
        # consent. This closes the previous bug where the backend ignored
        # the client-side checkbox and always recorded acceptance.
        via_checkbox = bool(payload.get("accepted"))
        legacy_phrase = payload.get("phrase")

        if not via_checkbox and legacy_phrase != eula_store.EXPECTED_PHRASE:
            return jsonify({
                "ok": False,
                "error": "Debes marcar la casilla de aceptación.",
            }), 400

        if via_checkbox:
            result = eula_store.accept(ip, ua, version, via_checkbox=True)
        else:
            result = eula_store.accept(ip, ua, version, legacy_phrase)

        if result is None:
            return jsonify({"ok": False, "error": "No se pudo registrar el consentimiento."}), 400

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
@require_role("admin")
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
        st = eula_store.status()
        return jsonify(st)
    except Exception as e:
        log_event("error", "eula_status_failed", COMPONENT, error=str(e))
        return jsonify({"accepted": False, "error": "Error interno."}), 500


_EULA_DOC_BODY = """
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
      aceptaci&oacute;n se formaliza marcando la casilla
      &laquo;He le&iacute;do y acepto el acuerdo&raquo; situada al pie
      de este documento y pulsando el bot&oacute;n
      &laquo;Formalizar aceptaci&oacute;n&raquo;.</p>
      <p>El consentimiento queda registrado con marca temporal,
      direcci&oacute;n IP de origen y un hash criptogr&aacute;fico
      univoco que identifica la versi&oacute;n del EULA aceptada.</p>
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
      <h2>4. Responsabilidad del usuario</h2>
      <p>El usuario se compromete a utilizar la Aplicaci&oacute;n de forma
      l&iacute;cita, respetando la legislaci&oacute;n vigente sobre derechos
      de autor y propiedad intelectual, asi como cualesquiera otras normas
      aplicables en su jurisdicci&oacute;n.</p>
      <p>El usuario es &uacute;nico responsable de los contenidos a los que
      accede y de las listas M3U o infohashes que introduce en la Aplicaci&oacute;n.</p>
    </div>
    <div class="clause">
      <h2>5. Exenci&oacute;n de responsabilidad del operador</h2>
      <p>El desarrollador y/o operador de la Aplicaci&oacute;n act&uacute;a
      como mero proveedor de infraestructura t&eacute;cnica (proxy HTTP y
      agregador) y no tiene control sobre los contenidos disponibles a
      trav&eacute;s de enlaces externos AceStream, M3U o IPFS. Conforme a la
      <em lang="en">Directive (EU) 2000/31/EC</em> sobre comercio electr&oacute;nico,
      la responsabilidad del operador por contenidos enlazados queda amparado por el r&eacute;gimen de exenci&oacute;n
      de los arts.&nbsp;12&ndash;15 de dicha Directiva.</p>
      <ul>
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
      (<em lang="en">as is</em>), sin garant&iacute;a de disponibilidad, exactitud o
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
    <div class="status-row" id="status-row" aria-live="polite">
      <span class="badge badge-muted" id="status-badge">Pendiente de aceptaci&oacute;n</span>
      <span class="status-detail" id="status-detail"></span>
    </div>

    <form id="accept-form" class="accept-form" onsubmit="return false;">
      <div class="checkbox-row input-group">
        <input type="checkbox" id="accept-cb" name="accept" value="1" aria-describedby="accept-cb-hint">
        <label for="accept-cb" id="accept-cb-hint">He le&iacute;do y acepto el acuerdo</label>
      </div>
      <button type="submit" class="btn btn-success btn-block btn-lg" id="accept-btn" disabled>Formalizar aceptaci&oacute;n</button>
    </form>

    <div class="msg" id="msg" role="alert" aria-live="assertive"></div>
    <div class="revoke-row" id="revoke-row" hidden>
      <button type="button" class="btn btn-ghost btn-sm" id="revoke-btn">Revocar consentimiento</button>
    </div>
  </div>
"""

_EULA_EXTRA_CSS = """
.eula-wrap{display:flex;align-items:flex-start;justify-content:center;padding:var(--space-5) var(--space-3)}
.doc{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);max-width:680px;width:100%;overflow:hidden;box-shadow:var(--shadow-lg)}
.doc-header{padding:var(--space-5) var(--space-4) var(--space-3);border-bottom:1px solid var(--border)}
.doc-header h1{font-size:1.143rem;font-weight:600;color:var(--text);margin-bottom:var(--space-1)}
.doc-header .meta{font-size:.786rem;color:var(--dim);display:flex;gap:var(--space-2);flex-wrap:wrap}
.doc-header .meta span{white-space:nowrap}
.doc-body{padding:0 var(--space-4);max-height:50vh;overflow-y:auto}
.clause{padding:var(--space-3) 0;border-bottom:1px solid var(--border-soft)}
.clause:last-child{border-bottom:none}
.clause h2{font-size:.85rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--blue);margin-bottom:var(--space-1)}
.clause p{font-size:.85rem;line-height:1.7;color:var(--muted);margin-bottom:var(--space-1)}
.clause p:last-child{margin-bottom:0}
.clause ul{margin:var(--space-1) 0 0 var(--space-4);font-size:.85rem;line-height:1.7;color:var(--muted)}
.clause ul li{margin-bottom:var(--space-1)}
.doc-footer{padding:var(--space-3) var(--space-4) var(--space-4);border-top:1px solid var(--border);background:var(--surface-2)}
.status-row{display:flex;align-items:center;gap:var(--space-2);margin-bottom:var(--space-3);flex-wrap:wrap}
.status-detail{font-size:.786rem;color:var(--dim)}
.accept-form{display:flex;flex-direction:column;gap:var(--space-3)}
.accept-form .checkbox-row{margin-bottom:0}
.revoke-row{text-align:center;margin-top:var(--space-2)}
@media(max-width:780px){
  .doc-body{max-height:none;padding:0 var(--space-3)}
  .doc-header,.doc-footer{padding-left:var(--space-3);padding-right:var(--space-3)}
}
"""

_EULA_EXTRA_JS = r"""
(function(){
  var $ = function(id){ return document.getElementById(id); };

  // Safe redirect parsing (URLSearchParams.get already decodes once).
  var redirectTo = '/';
  try {
    var params = new URLSearchParams(location.search);
    var r = params.get('redirect');
    if (r && r.indexOf('\\') === -1) {
      var target = new URL(r, location.origin);
      if (target.origin === location.origin) redirectTo = target.pathname + target.search + target.hash;
    }
  } catch(e) { /* leave default '/' */ }

  function setMsg(text, kind) {
    var m = $('msg');
    if (kind === 'err') m.className = 'msg err';
    else if (kind === 'ok') m.className = 'msg ok';
    else if (kind === 'info') m.className = 'msg msg-info';
    else m.className = 'msg';
    m.textContent = text;
  }

  function refreshAcceptBtn() {
    $('accept-btn').disabled = !$('accept-cb').checked;
  }
  $('accept-cb').addEventListener('change', refreshAcceptBtn);
  // Enter on the checkbox toggles it (Space is native).
  $('accept-cb').addEventListener('keydown', function(e){
    if (e.key === 'Enter') {
      e.preventDefault();
      $('accept-cb').checked = !$('accept-cb').checked;
      refreshAcceptBtn();
    }
  });

  function checkStatus() {
    return fetchJSON('/api/eula/status', { cache: 'no-store' }).then(function(d){
      if (d.accepted) {
        $('status-badge').className = 'badge badge-green';
        $('status-badge').textContent = 'Aceptado';
        $('status-detail').textContent = 'Consentimiento #' + (d.consent_id || '') + ' \u2014 ' + (d.accepted_at || '');
        $('accept-form').hidden = true;
        setMsg('Tu consentimiento está registrado. Puedes acceder al servicio.', 'ok');
        $('revoke-row').hidden = true;
        refreshRevokeVisibility();
        // Auto-redirect if a target was supplied.
        var params = new URLSearchParams(location.search);
        if (redirectTo !== '/' && params.get('redirect')) {
          setTimeout(function(){ location.href = redirectTo; }, 1500);
        }
      } else {
        $('status-badge').className = 'badge badge-muted';
        $('status-badge').textContent = 'Pendiente de aceptación';
        $('status-detail').textContent = '';
        $('accept-form').hidden = false;
        $('revoke-row').hidden = true;
        setMsg('', '');
      }
    }).catch(function(){
      setMsg('No se pudo verificar el estado del consentimiento. Revisa tu conexión y recarga la página.', 'err');
      $('accept-btn').disabled = true;
    });
  }

  $('accept-form').addEventListener('submit', function(ev){
    ev.preventDefault();
    if (!$('accept-cb').checked) {
      setMsg('Debes marcar la casilla para aceptar.', 'err');
      $('accept-cb').focus();
      return;
    }
    var btn = $('accept-btn');
    btn.disabled = true;
    var original = btn.textContent;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Procesando…';
    setMsg('', '');
    fetchJSON('/api/eula/accept', { method: 'POST', body: { accepted: true, version: '1.0' } })
      .then(function(d){
        setMsg('Consentimiento registrado. Redirigiendo…', 'ok');
        setTimeout(function(){ location.href = redirectTo; }, 800);
      })
      .catch(function(e){
        setMsg((e && e.body && e.body.error) || 'No se pudo registrar el consentimiento.', 'err');
        btn.disabled = false;
        btn.textContent = original;
      });
  });

  // Revoke: visible only to admins (queried via /api/auth/me).
  function refreshRevokeVisibility() {
    fetchJSON('/api/auth/me', { cache: 'no-store' }).then(function(d){
      $('revoke-row').hidden = !(d && d.user && d.user.role === 'admin');
    }).catch(function(){ $('revoke-row').hidden = true; });
  }

  $('revoke-btn').addEventListener('click', function(){
    if (!confirm('Se revocará el consentimiento global al EULA. Todos los usuarios perderán el acceso hasta que se vuelva a aceptar. ¿Continuar?')) return;
    var btn = $('revoke-btn');
    var original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner" aria-hidden="true"></span> Revocando…';
    setMsg('', '');
    fetchJSON('/api/eula/revoke', { method: 'POST' })
      .then(function(){
        setMsg('Consentimiento revocado.', 'info');
        $('accept-form').hidden = false;
        $('accept-cb').checked = false;
        refreshAcceptBtn();
        return checkStatus();
      })
      .catch(function(e){
        if (e.status === 401 || e.status === 403) {
          setMsg('Se requiere rol de administrador para revocar el consentimiento.', 'err');
        } else {
          setMsg((e && e.body && e.body.error) || 'No se pudo revocar.', 'err');
        }
      })
      .finally(function(){
        btn.disabled = false;
        btn.textContent = original;
      });
  });

  checkStatus();
  refreshRevokeVisibility();
})();
"""


def _render_eula_html():
    from app.ui.base import render_page
    from flask import g
    show_header = getattr(g, "user", None) is not None
    body = '<div class="eula-wrap">' + _EULA_DOC_BODY + '</div>'
    if show_header:
        body = '<div style="padding:8px 16px"><a href="/panel" class="btn btn-sm btn-outline">← Panel</a></div>' + body
    return render_page(
        title="OpenAce · Acuerdo de Licencia de Usuario Final",
        body=body,
        extra_css=_EULA_EXTRA_CSS,
        extra_js=_EULA_EXTRA_JS,
        body_class="page-eula",
        active_nav="/eula",
        show_header=show_header,
        container_class="",
        robots_noindex=True,
        description="Acuerdo de Licencia de Usuario Final de OpenAce",
    )
