"""Base UI primitives shared by every page.

This module is dependency-light (only stdlib + Flask current_app) so it can be
imported safely from any route handler. It produces consistent HTML output:

- Single ``:root`` palette (dark + light via prefers-color-scheme + manual
  override via ``data-theme`` attribute on ``<html>``).
- Unified typography stack with safe fallbacks.
- Reset + scrollbar styling + ``color-scheme: dark light`` so native form
  controls render in the right theme.
- Reusable components: ``.btn`` (+ variants), ``.card``, ``.badge`` (+ colors),
  ``.input-group``, ``.table-wrap``, ``.modal-backdrop`` + ``.modal``,
  ``.msg[role=alert]``, ``.toast``, ``.spinner``, ``.skip-link``.
- Helpers: ``esc()``, ``fetchJSON()`` with timeout, ``toast()``, ``copyToClipboard()``,
  ``setupModal()`` with focus-trap, ``csrfToken()``, ``_()`` for JS i18n.

Designed for vanilla JS and browsers as old as Chrome 60 / Firefox 60 / Safari
12. No ``:focus-visible``, no ``AbortController``, no native ``<dialog>``.
"""

from __future__ import annotations

import html as _html
import secrets
from typing import Any

from flask import current_app, g, request, session

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

BASE_CSS = r"""
[hidden]{display:none!important}
@font-face{
  font-family:"Manrope";
  src:url("/static/fonts/manrope-latin-400-normal.woff2") format("woff2");
  font-weight:400;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"Manrope";
  src:url("/static/fonts/manrope-latin-500-normal.woff2") format("woff2");
  font-weight:500;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"Manrope";
  src:url("/static/fonts/manrope-latin-600-normal.woff2") format("woff2");
  font-weight:600;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"Manrope";
  src:url("/static/fonts/manrope-latin-700-normal.woff2") format("woff2");
  font-weight:700;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"JetBrains Mono";
  src:url("/static/fonts/jetbrains-mono-latin-400-normal.woff2") format("woff2");
  font-weight:400;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"JetBrains Mono";
  src:url("/static/fonts/jetbrains-mono-latin-500-normal.woff2") format("woff2");
  font-weight:500;
  font-style:normal;
  font-display:swap;
}
@font-face{
  font-family:"JetBrains Mono";
  src:url("/static/fonts/jetbrains-mono-latin-600-normal.woff2") format("woff2");
  font-weight:600;
  font-style:normal;
  font-display:swap;
}
:root{
  /* Palette - dark theme (default) */
  --bg:#0d1117;
  --surface:#161b22;
  --surface-2:#1c2129;
  --border:#30363d;
  --border-soft:#21262d;
  --text:#e6edf3;
  --muted:#8b949e;
  --dim:#6e7681;
  --blue:#58a6ff;
  --blue-dim:#1f6feb;
  --green:#3fb950;
  --green-dim:#238636;
  --red:#f85149;
  --red-dim:#da3633;
  --yellow:#d29922;
  --orange:#db6d28;
  --purple:#bc8cff;
  --purple-dim:#8957e5;
  /* Semantic aliases (kept stable across themes) */
  --primary:var(--blue);
  --primary-dim:var(--blue-dim);
  --success:var(--green);
  --danger:var(--red);
  --warning:var(--yellow);
  --info:var(--blue);
  /* Component tokens */
  --radius:7px;
  --radius-sm:5px;
  --radius-pill:999px;
  --space-1:4px;
  --space-2:8px;
  --space-3:14px;
  --space-4:20px;
  --space-5:28px;
  --space-6:40px;
  --header-h:52px;
  --tap-min:44px;
  --tap-min-sm:32px;
  --shadow-sm:0 1px 3px rgba(1,4,9,.2);
  --shadow-md:0 6px 16px rgba(1,4,9,.18);
  --shadow-lg:0 12px 30px rgba(1,4,9,.14);
  --shadow-xl:0 24px 70px rgba(0,0,0,.35);
  --focus-ring:0 0 0 3px rgba(88,166,255,.45);
  --focus-ring-dim:0 0 0 2px rgba(31,111,235,.45);
  --font-sans:"Manrope",-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  --font-mono:"JetBrains Mono",ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --transition-fast:120ms ease;
  --transition:180ms ease;
  color-scheme:dark;
}

/* Light theme: prefers-color-scheme OR manual [data-theme="light"] */
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]){
    --bg:#f6f8fa;
    --surface:#ffffff;
    --surface-2:#eaeef2;
    --border:#d0d7de;
    --border-soft:#eaeef2;
    --text:#1f2328;
    --muted:#59636e;
    --dim:#818b98;
    --blue:#0969da;
    --blue-dim:#218bff;
    --green:#1a7f37;
    --green-dim:#2da44e;
    --red:#cf222e;
    --red-dim:#a40e26;
    --yellow:#9a6700;
    --orange:#bc4c00;
    --purple:#8250df;
    --purple-dim:#6639ba;
    --shadow-sm:0 1px 3px rgba(31,35,40,.12);
    --shadow-md:0 6px 16px rgba(31,35,40,.15);
    --shadow-lg:0 12px 30px rgba(31,35,40,.18);
    --shadow-xl:0 24px 70px rgba(31,35,40,.25);
    --focus-ring:0 0 0 3px rgba(9,105,218,.4);
    color-scheme:light;
  }
}
/* Manual override wins over prefers-color-scheme */
:root[data-theme="light"]{
  --bg:#f6f8fa;
  --surface:#ffffff;
  --surface-2:#eaeef2;
  --border:#d0d7de;
  --border-soft:#eaeef2;
  --text:#1f2328;
  --muted:#59636e;
  --dim:#818b98;
  --blue:#0969da;
  --blue-dim:#218bff;
  --green:#1a7f37;
  --green-dim:#2da44e;
  --red:#cf222e;
  --red-dim:#a40e26;
  --yellow:#9a6700;
  --orange:#bc4c00;
  --purple:#8250df;
  --purple-dim:#6639ba;
  --shadow-sm:0 1px 3px rgba(31,35,40,.12);
  --shadow-md:0 6px 16px rgba(31,35,40,.15);
  --shadow-lg:0 12px 30px rgba(31,35,40,.18);
  --shadow-xl:0 24px 70px rgba(31,35,40,.25);
  --focus-ring:0 0 0 3px rgba(9,105,218,.4);
  color-scheme:light;
}

*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
html{font-size:14px}
body{
  font-family:var(--font-sans);
  background:var(--bg);
  color:var(--text);
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
  min-height:100vh;
}
img,svg{max-width:100%;display:block}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
button{font-family:inherit;font-size:inherit}
input,select,textarea{font-family:inherit;font-size:inherit}
code,pre,.mono{font-family:var(--font-mono)}
h1,h2,h3,h4,h5,h6{margin:0;font-weight:600;line-height:1.25;color:var(--text)}
h1{font-size:1.286rem}
h2{font-size:1.143rem}
h3{font-size:1rem}
h4{font-size:.929rem}
p{margin:0 0 var(--space-3)}

/* Scrollbars */
*{scrollbar-width:thin;scrollbar-color:var(--border) transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}

/* Skip-link */
.skip-link{
  position:absolute;
  left:-9999px;
  top:0;
  z-index:9999;
  background:var(--surface);
  color:var(--text);
  padding:var(--space-2) var(--space-3);
  border-radius:0 0 var(--radius) 0;
  border:1px solid var(--border);
  border-top:none;
}
.skip-link:focus{left:0}

/* Layout */
.container{width:min(100%,1680px);margin:0 auto;padding:0 var(--space-4)}
.container-narrow{width:min(100%,640px);margin:0 auto;padding:0 var(--space-4)}
.container-medium{width:min(100%,960px);margin:0 auto;padding:0 var(--space-4)}

/* Header (shared) */
.app-header{
  position:sticky;
  top:0;
  z-index:30;
  display:flex;
  align-items:center;
  gap:var(--space-3);
  padding:10px var(--space-4);
  background:rgba(13,17,23,.85);
  -webkit-backdrop-filter:blur(10px);
  backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border);
  min-height:var(--header-h);
}
:root[data-theme="light"] .app-header{background:rgba(255,255,255,.85)}
@media (prefers-color-scheme: light){
  :root:not([data-theme="dark"]) .app-header{background:rgba(255,255,255,.85)}
}
.app-header .brand{
  font-weight:700;
  font-size:1rem;
  color:var(--blue);
  letter-spacing:-.02em;
  text-decoration:none;
  display:flex;
  align-items:center;
  gap:var(--space-2);
}
.app-header .brand:hover{text-decoration:none}
.app-header .brand svg{width:20px;height:20px}
.app-header nav{display:flex;align-items:center;gap:var(--space-3);margin-left:var(--space-3)}
.app-header nav a{
  color:var(--text);
  font-size:.875rem;
  padding:6px 10px;
  border-radius:var(--radius-sm);
  text-decoration:none;
}
.app-header nav a:hover{background:var(--surface-2);text-decoration:none}
.app-header nav a[aria-current="page"]{color:var(--blue);background:rgba(88,166,255,.12)}
.app-header .spacer{flex:1 1 auto}
.app-header .user-chip{
  display:inline-flex;
  align-items:center;
  gap:6px;
  font-size:.875rem;
  color:var(--muted);
  padding:4px 10px;
  border-radius:var(--radius-pill);
  background:var(--surface-2);
}
.app-header .user-chip strong{color:var(--text);font-weight:600}
.app-header .icon-btn{
  background:transparent;
  border:1px solid var(--border);
  color:var(--text);
  padding:6px 10px;
  border-radius:var(--radius-sm);
  cursor:pointer;
  font-size:.85rem;
  min-height:var(--tap-min-sm);
  display:inline-flex;
  align-items:center;
  gap:6px;
}
.app-header .icon-btn:hover{background:var(--surface-2)}
.app-header .icon-btn[aria-pressed="true"]{border-color:var(--blue);color:var(--blue);background:rgba(88,166,255,.12)}
.app-header select.icon-btn{appearance:auto;padding-right:8px}

/* Main landmark */
.app-main{display:block}

/* Buttons */
.btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:6px;
  padding:8px 14px;
  min-height:var(--tap-min-sm);
  font-size:.875rem;
  font-weight:500;
  border:1px solid var(--border);
  border-radius:var(--radius);
  background:var(--surface);
  color:var(--text);
  cursor:pointer;
  text-decoration:none;
  transition:background var(--transition-fast),border-color var(--transition-fast),opacity var(--transition-fast);
  white-space:nowrap;
  line-height:1.2;
}
.btn:hover{background:var(--surface-2);text-decoration:none}
.btn:focus{outline:none;box-shadow:var(--focus-ring)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-primary{background:var(--blue);border-color:var(--blue);color:#fff}
.btn-primary:hover{background:var(--blue-dim);border-color:var(--blue-dim)}
.btn-success{background:var(--green);border-color:var(--green);color:#fff}
.btn-success:hover{background:var(--green-dim);border-color:var(--green-dim)}
.btn-danger{background:var(--red);border-color:var(--red);color:#fff}
.btn-danger:hover{background:var(--red-dim);border-color:var(--red-dim)}
.btn-ghost{background:transparent;border-color:transparent;color:var(--text)}
.btn-ghost:hover{background:var(--surface-2);border-color:var(--border)}
.btn-link{background:none;border:none;color:var(--blue);padding:4px 6px;min-height:auto}
.btn-link:hover{text-decoration:underline}
.btn-sm{padding:4px 10px;font-size:.786rem;min-height:var(--tap-min-sm)}
.btn-lg{padding:11px 20px;font-size:1rem;min-height:var(--tap-min)}
.btn-block{display:flex;width:100%}

/* Cards */
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:var(--space-3);
  box-shadow:var(--shadow-lg);
  margin-bottom:var(--space-3);
}
.card-title{font-size:.929rem;font-weight:600;margin-bottom:var(--space-2);color:var(--text)}
.card-flush{padding:0;overflow:hidden}

/* Inputs */
.input-group{display:block;margin-bottom:var(--space-3)}
.input-group label{display:block;font-size:.85rem;font-weight:500;margin-bottom:6px;color:var(--text)}
.input-group .hint{display:block;font-size:.786rem;color:var(--muted);margin-top:4px}
.input-group .error{display:block;font-size:.786rem;color:var(--red);margin-top:4px}
input[type="text"],input[type="password"],input[type="email"],input[type="url"],
input[type="number"],input[type="search"],input[type="tel"],input[type="date"],
input[type="datetime-local"],input[type="time"],select,textarea{
  display:block;
  width:100%;
  padding:8px 10px;
  font-size:.875rem;
  background:var(--bg);
  color:var(--text);
  border:1px solid var(--border);
  border-radius:var(--radius);
  transition:border-color var(--transition-fast),box-shadow var(--transition-fast);
  min-height:var(--tap-min-sm);
}
input:focus,select:focus,textarea:focus{
  outline:none;
  border-color:var(--blue);
  box-shadow:var(--focus-ring);
}
input::placeholder,textarea::placeholder{color:var(--dim)}
input[type="checkbox"],input[type="radio"]{
  width:16px;height:16px;
  vertical-align:middle;
  margin-right:6px;
  accent-color:var(--blue);
}
.checkbox-row{display:flex;align-items:center;gap:6px}
.checkbox-row label{margin:0;font-weight:normal}

/* Badges */
.badge{
  display:inline-flex;
  align-items:center;
  gap:4px;
  padding:2px 8px;
  font-size:.714rem;
  font-weight:500;
  border-radius:var(--radius-pill);
  background:rgba(139,148,158,.12);
  color:var(--muted);
  white-space:nowrap;
  line-height:1.4;
}
.badge::before{
  content:"";
  width:6px;
  height:6px;
  border-radius:50%;
  background:currentColor;
  flex-shrink:0;
}
.badge-no-dot::before{display:none}
.badge-green{background:rgba(63,185,80,.12);color:var(--green)}
.badge-red{background:rgba(248,81,73,.12);color:var(--red)}
.badge-yellow{background:rgba(210,153,34,.12);color:var(--yellow)}
.badge-orange{background:rgba(219,109,40,.12);color:var(--orange)}
.badge-blue{background:rgba(88,166,255,.12);color:var(--blue)}
.badge-purple{background:rgba(188,140,255,.12);color:var(--purple)}
.badge-muted{background:rgba(139,148,158,.12);color:var(--muted)}

/* Tables */
.table-wrap{
  width:100%;
  overflow:auto;
  border-radius:var(--radius);
  border:1px solid var(--border);
  background:var(--surface);
  max-height:calc(100vh - var(--header-h) - 80px);
}
.table-wrap table{
  width:100%;
  border-collapse:collapse;
  font-size:.85rem;
}
.table-wrap th,.table-wrap td{
  padding:8px 12px;
  text-align:left;
  border-bottom:1px solid var(--border-soft);
  vertical-align:middle;
}
.table-wrap th{
  font-weight:600;
  font-size:.786rem;
  text-transform:uppercase;
  letter-spacing:.04em;
  color:var(--muted);
  background:var(--surface-2);
  position:sticky;
  top:0;
  z-index:1;
}
.table-wrap tbody tr:hover{background:var(--surface-2)}
.table-wrap tr:last-child td{border-bottom:none}
.table-wrap th.sortable{cursor:pointer;user-select:none}
.table-wrap th.sortable:hover{color:var(--text)}
.table-wrap th[tabindex="0"]:focus{outline:none;box-shadow:inset 0 0 0 2px var(--blue)}

/* Modal */
.modal-backdrop{
  position:fixed;
  top:0;
  right:0;
  bottom:0;
  left:0;
  inset:0;
  background:rgba(1,4,9,.6);
  z-index:1000;
  display:flex;
  align-items:flex-start;
  justify-content:center;
  padding:5vh var(--space-4);
  overflow-y:auto;
}
.modal{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  width:100%;
  max-width:480px;
  padding:var(--space-4);
  box-shadow:var(--shadow-xl);
  margin-bottom:5vh;
}
.modal.modal-lg{max-width:640px}
.modal.modal-xl{max-width:960px}
.modal-title{font-size:1.143rem;margin-bottom:var(--space-3);color:var(--text)}
.modal-footer{
  display:flex;
  justify-content:flex-end;
  gap:var(--space-2);
  margin-top:var(--space-4);
  padding-top:var(--space-3);
  border-top:1px solid var(--border-soft);
}

/* Messages (inline alert) */
.msg{
  padding:8px 12px;
  border-radius:var(--radius-sm);
  font-size:.85rem;
  margin-bottom:var(--space-2);
  min-height:20px;
  border:1px solid transparent;
}
.msg:empty{display:none}
.msg.msg-error,.msg.err{
  background:rgba(248,81,73,.12);
  color:var(--red);
  border-color:rgba(248,81,73,.3);
}
.msg.msg-success,.msg.ok{
  background:rgba(63,185,80,.12);
  color:var(--green);
  border-color:rgba(63,185,80,.3);
}
.msg.msg-info{
  background:rgba(88,166,255,.12);
  color:var(--blue);
  border-color:rgba(88,166,255,.3);
}

/* Toasts */
.toast-stack{
  position:fixed;
  bottom:var(--space-4);
  right:var(--space-4);
  z-index:2000;
  display:flex;
  flex-direction:column;
  gap:var(--space-2);
  pointer-events:none;
  max-width:360px;
  max-width:min(360px,calc(100vw - 32px));
}
.toast{
  padding:10px 14px;
  border-radius:var(--radius);
  font-size:.85rem;
  font-weight:500;
  background:var(--surface);
  color:var(--text);
  border:1px solid var(--border);
  box-shadow:var(--shadow-md);
  pointer-events:auto;
  animation:fadeInUp 180ms ease;
  display:flex;
  align-items:center;
  gap:8px;
}
.toast.toast-success{border-color:var(--green);color:var(--green)}
.toast.toast-error{border-color:var(--red);color:var(--red)}
.toast.toast-info{border-color:var(--blue);color:var(--blue)}
.toast.toast-warning{border-color:var(--yellow);color:var(--yellow)}
@keyframes fadeInUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}

/* Spinner */
.spinner{
  display:inline-block;
  width:14px;
  height:14px;
  border:2px solid currentColor;
  border-top-color:transparent;
  border-radius:50%;
  animation:spin 700ms linear infinite;
  vertical-align:middle;
}
.spinner-lg{width:24px;height:24px;border-width:3px}
@keyframes spin{to{transform:rotate(360deg)}}

/* Skeleton */
.skeleton{
  background:linear-gradient(90deg,var(--surface-2) 25%,var(--border-soft) 37%,var(--surface-2) 63%);
  background-size:400% 100%;
  animation:shimmer 1400ms ease infinite;
  border-radius:var(--radius-sm);
}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:-100% 0}}

/* Empty / loading states */
.empty-state{
  text-align:center;
  padding:var(--space-5) var(--space-3);
  color:var(--muted);
}
.empty-state svg{margin:0 auto var(--space-3);opacity:.5}

/* Responsive helpers */
@media (max-width:1180px){
  .grid-collapse-lg{grid-template-columns:1fr !important}
}
@media (max-width:780px){
  .app-header{flex-wrap:wrap;gap:var(--space-2);padding:8px var(--space-3)}
  .app-header .brand{flex:0 0 auto}
  .app-header .spacer{display:none}
  .app-header nav{order:3;width:100%;margin-left:0;overflow-x:auto;padding-bottom:4px;scrollbar-width:none}
  .app-header nav::-webkit-scrollbar{display:none}
  .app-header nav a{flex:0 0 auto}
  .app-header .user-chip{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .container,.container-narrow,.container-medium{padding:0 var(--space-3)}
  .table-wrap{max-height:none}
  .modal{padding:var(--space-3)}
  .modal-footer{flex-direction:column-reverse;align-items:stretch}
  .modal-footer .btn,.modal-footer button{width:100%}
  .toast-stack{left:var(--space-3);right:var(--space-3);max-width:none}
}
@media (max-width:520px){
  .app-header .brand strong{display:none}
  .app-header .icon-btn{padding:6px 8px}
  .btn{white-space:normal}
}
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation-duration:.01ms !important;animation-iteration-count:1 !important;scroll-behavior:auto !important;transition-duration:.01ms !important}
}
.sr-only{
  position:absolute;
  width:1px;height:1px;
  padding:0;margin:-1px;
  overflow:hidden;
  clip:rect(0,0,0,0);
  white-space:nowrap;
  border:0;
}
"""

# ---------------------------------------------------------------------------
# JS
# ---------------------------------------------------------------------------

BASE_JS = r"""
(function(){
  "use strict";

  // ----- i18n catalog (injected by render_page as window.I18N_CATALOG) -----
  var CATALOG = (window.I18N_CATALOG && typeof window.I18N_CATALOG === 'object') ? window.I18N_CATALOG : {};
  window._ = function(key){
    if (!key) return '';
    return Object.prototype.hasOwnProperty.call(CATALOG, key) ? CATALOG[key] : key;
  };

  // ----- esc: HTML-escape, escapes & < > " ' / -----
  window.esc = function(s){
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/'/g,'&#39;')
      .replace(/\//g,'&#47;');
  };

  // ----- CSRF token (double-submit cookie) -----
  window.csrfToken = function(){
    var m = document.cookie.match(/(?:^|;\s)csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  };
  window.fetchJSON = function(url, opts, timeoutMs){
    opts = opts || {};
    timeoutMs = timeoutMs || 12000;
    var headers = Object.assign({'Accept':'application/json'}, opts.headers || {});
    if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === 'object') {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      opts.body = JSON.stringify(opts.body);
    }
    if (!opts.skipCsrf && (opts.method === 'POST' || opts.method === 'PUT' || opts.method === 'DELETE' || opts.method === 'PATCH')) {
      var tok = window.csrfToken();
      if (tok) headers['X-CSRF-Token'] = tok;
    }
    return new Promise(function(resolve, reject){
      var done = false;
      var t = setTimeout(function(){
        if (done) return;
        done = true;
        var e = new Error('Timeout');
        e.name = 'TimeoutError';
        reject(e);
      }, timeoutMs);
      fetch(url, {method: opts.method || 'GET', headers: headers, body: opts.body, credentials: 'same-origin', cache: opts.cache || 'default'})
        .then(function(r){
          if (done) return;
          done = true; clearTimeout(t);
          if (!r.ok) {
            return r.json().catch(function(){ return {error: 'HTTP ' + r.status}; }).then(function(d){
              var e = new Error(d.error || ('HTTP ' + r.status));
              e.status = r.status; e.body = d;
              throw e;
            });
          }
          var ct = r.headers.get('content-type') || '';
          if (ct.indexOf('application/json') !== -1) return r.json();
          return r.text();
        })
        .then(resolve, reject);
    });
  };

  // ----- Toast stack -----
  function _ensureStack(){
    var s = document.querySelector('.toast-stack');
    if (!s) {
      s = document.createElement('div');
      s.className = 'toast-stack';
      s.setAttribute('aria-live','polite');
      s.setAttribute('role','status');
      document.body.appendChild(s);
    }
    return s;
  }
  window.toast = function(msg, type){
    type = type || 'success';
    var s = _ensureStack();
    var t = document.createElement('div');
    t.className = 'toast toast-' + type;
    t.setAttribute('role', type === 'error' ? 'alert' : 'status');
    var icon = type === 'success' ? '\u2713' : type === 'error' ? '\u26A0' : type === 'warning' ? '\u26A0' : '\u2139';
    t.innerHTML = '<span aria-hidden="true">' + icon + '</span><span>' + window.esc(msg) + '</span>';
    s.appendChild(t);
    setTimeout(function(){
      t.style.opacity = '0';
      t.style.transition = 'opacity 180ms';
      setTimeout(function(){ if (t.parentNode) t.parentNode.removeChild(t); }, 220);
    }, 2800);
  };

  // ----- copyToClipboard with fallback -----
  window.copyToClipboard = function(text){
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function(){ return true; }, function(){ return _legacyCopy(text); });
    }
    return Promise.resolve(_legacyCopy(text));
  };
  function _legacyCopy(text){
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly','');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch(e) { return false; }
  }

  // ----- Modal: focus trap + Esc + restore focus -----
  window.setupModal = function(modalEl, opts){
    opts = opts || {};
    var prevFocus = null;
    var onKeydown = function(e){
      if (e.key === 'Escape') {
        e.preventDefault();
        close();
      } else if (e.key === 'Tab') {
        var focusables = modalEl.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])');
        if (!focusables.length) return;
        var first = focusables[0], last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault(); first.focus();
        }
      }
    };
    var open = function(){
      prevFocus = document.activeElement;
      modalEl.hidden = false;
      modalEl.style.display = '';
      modalEl.setAttribute('aria-hidden','false');
      document.addEventListener('keydown', onKeydown);
      document.body.style.overflow = 'hidden';
      var focusables = modalEl.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])');
      var initial = opts.initialFocus ? modalEl.querySelector(opts.initialFocus) : null;
      (initial || focusables[0] || modalEl).focus();
    };
    var close = function(){
      modalEl.style.display = 'none';
      modalEl.hidden = true;
      modalEl.setAttribute('aria-hidden','true');
      document.removeEventListener('keydown', onKeydown);
      document.body.style.overflow = '';
      if (opts.onClose) opts.onClose();
      if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
    };
    if (opts.closeOnBackdrop !== false) {
      modalEl.addEventListener('click', function(e){
        if (e.target === modalEl) close();
      });
    }
    modalEl.style.display = 'none';
    modalEl.hidden = true;
    return { open: open, close: close };
  };

  // ----- Theme toggle (persisted in localStorage, also honors prefers-color-scheme) -----
  function _syncThemeButton(theme){
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var isLight = theme === 'light';
    var label = isLight ? 'Cambiar a tema oscuro' : 'Cambiar a tema claro';
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
    btn.setAttribute('aria-pressed', isLight ? 'true' : 'false');
    var icon = btn.querySelector('[aria-hidden="true"]');
    if (icon) icon.textContent = isLight ? '\u2600' : '\u263E';
  }
  window.applyTheme = function(theme){
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('openace-theme', theme); } catch(e) {}
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#ffffff' : '#0d1117');
    _syncThemeButton(theme);
  };
  window.initThemeToggle = function(btnId){
    var btn = document.getElementById(btnId);
    if (!btn) return;
    var stored = null;
    try { stored = localStorage.getItem('openace-theme'); } catch(e) {}
    if (stored) window.applyTheme(stored);
    else _syncThemeButton(window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    btn.addEventListener('click', function(){
      var current = document.documentElement.getAttribute('data-theme') ||
        (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      window.applyTheme(current === 'light' ? 'dark' : 'light');
    });
  };

  // ----- Language switch -----
  window.switchLang = function(lang){
    var url = new URL(location.href);
    url.searchParams.set('lang', lang);
    location.href = url.toString();
  };

  // ----- Visibility-aware refresh helper -----
  window.makeVisibilityAware = function(intervalMs, fn){
    var timer = null, running = true;
    function start(){ if (!timer) timer = setInterval(fn, intervalMs); }
    function stop(){ if (timer) { clearInterval(timer); timer = null; } }
    function onVis(){
      if (document.hidden) stop();
      else { start(); fn(); }
    }
    document.addEventListener('visibilitychange', onVis);
    start();
    return {
      pause: function(){ running = false; stop(); },
      resume: function(){ running = true; start(); },
      isPaused: function(){ return !running; },
      isRunning: function(){ return timer !== null; },
      trigger: function(){ fn(); }
    };
  };
})();
"""

# ---------------------------------------------------------------------------
# SVG favicon (inline, no extra HTTP request)
# ---------------------------------------------------------------------------

svg_favicon = (
    '<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;utf8,'
    + _html.escape(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#0d1117"/>'
        '<path d="M16 6 L25 24 L7 24 Z" fill="#58a6ff" opacity="0.85"/>'
        '<circle cx="16" cy="20" r="3" fill="#3fb950"/>'
        '</svg>'
    )
    + '">'
)


# ---------------------------------------------------------------------------
# CSRF helpers (double-submit cookie pattern)
# ---------------------------------------------------------------------------

def csrf_token() -> str:
    """Return the current CSRF token, creating one if needed.

    Stored in the Flask session (signed cookie) and mirrored to a readable
    ``csrf_token`` cookie on the response so that JS can read it via
    ``document.cookie``.
    """
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def csrf_input() -> str:
    """HTML for a hidden ``<input>`` carrying the CSRF token (form fallback)."""
    tok = csrf_token()
    return (
        f'<input type="hidden" name="csrf_token" value="{_html.escape(tok)}" '
        f'aria-hidden="true">'
    )


# ---------------------------------------------------------------------------
# Page shell
# ---------------------------------------------------------------------------

_HEADER_NAV = (
    # (path, label_key, required_role)
    ("/panel", "nav.dashboard", None),
    ("/peers", "nav.peers", "user"),
    ("/plugins", "nav.plugins", "user"),
    ("/check", "nav.checker", "user"),
    ("/environment", "nav.environment", "admin"),
    ("/admin/users", "nav.users", "admin"),
    ("/eula", "nav.eula", None),
)


def _user_nav_items() -> list[tuple[str, str, str | None]]:
    """Filter nav items by current user role."""
    user = getattr(g, "user", None)
    role = (user or {}).get("role")
    hierarchy = {"admin": 3, "user": 2, "viewer": 1}
    user_level = hierarchy.get(role, 0)
    items: list[tuple[str, str, str | None]] = []
    for path, label_key, required in _HEADER_NAV:
        if required is None:
            req_level = 0
        else:
            req_level = hierarchy.get(required, 0)
        if user_level >= req_level:
            items.append((path, label_key, required))
    return items


def _lang_options() -> list[tuple[str, str]]:
    # Lazy import to avoid circular at module load time
    try:
        from app.ui.i18n import get_locales
        return get_locales()
    except Exception:
        return [("es", "Español"), ("en", "English")]


def _render_header(active_nav: str = "", page_title_key: str = "") -> str:
    """Render the sticky app header. Empty string on setup/eula/login pages
    where no auth is guaranteed yet."""
    from app.ui.i18n import _, get_locale

    nav_items = _user_nav_items()
    nav_html = ""
    if nav_items:
        links = []
        for path, label_key, _required in nav_items:
            is_active = (active_nav == path) or (
                active_nav == "/panel" and path == "/panel"
            )
            attr = ' aria-current="page"' if is_active else ""
            links.append(
                f'<a href="{path}"{attr}>{_html.escape(_(label_key))}</a>'
            )
        nav_html = '<nav aria-label="' + _html.escape(_("nav.primary")) + '">' + "".join(links) + "</nav>"

    user = getattr(g, "user", None)
    user_chip = ""
    if user:
        user_chip = (
            f'<span class="user-chip"><span aria-hidden="true">\U0001F464</span>'
            f'<strong>{_html.escape(user.get("username", ""))}</strong>'
            f'<span class="sr-only">({_("role." + user.get("role", "user"))})</span></span>'
        )

    logout_html = ""
    if user:
        logout_html = (
            '<form method="POST" action="/logout" style="display:inline">'
            + csrf_input()
            + '<button type="submit" class="btn btn-sm btn-ghost">'
            + _("nav.logout")
            + "</button></form>"
        )

    # Theme toggle button
    theme_btn = (
        '<button type="button" id="theme-toggle" class="icon-btn" '
        'aria-label="' + _html.escape(_("nav.theme_toggle")) + '" title="' + _html.escape(_("nav.theme_toggle")) + '">'
        '<span aria-hidden="true">\u263E</span>'
        '</button>'
    )

    # Language selector
    current_locale = get_locale()
    lang_options = "".join(
        f'<option value="{code}"' + (' selected' if code == current_locale else '') + f'>{_html.escape(name)}</option>'
        for code, name in _lang_options()
    )
    lang_select = (
        '<select class="icon-btn" onchange="switchLang(this.value)" '
        'aria-label="' + _html.escape(_("nav.language")) + '">' + lang_options + '</select>'
    )

    brand = (
        '<a class="brand" href="/panel" aria-label="OpenAce">'
        '<svg viewBox="0 0 32 32" aria-hidden="true">'
        '<path d="M16 6 L25 24 L7 24 Z" fill="currentColor" opacity="0.85"/>'
        '<circle cx="16" cy="20" r="3" fill="var(--green)"/>'
        '</svg>'
        '<strong>OpenAce</strong>'
        '</a>'
    )

    return (
        '<header class="app-header">'
        + brand
        + nav_html
        + '<span class="spacer"></span>'
        + user_chip
        + lang_select
        + theme_btn
        + logout_html
        + "</header>"
    )


def render_page(
    title: str,
    body: str,
    *,
    extra_css: str = "",
    extra_js: str = "",
    body_class: str = "",
    active_nav: str = "",
    show_header: bool = True,
    container_class: str = "container",
    robots_noindex: bool = False,
    description: str = "",
) -> str:
    """Render a full HTML page with shared shell.

    Parameters
    ----------
    title:
        Already-translated page title (used in ``<title>`` and ``<h1>``).
    body:
        HTML content for ``<main>``.
    extra_css:
        Page-specific CSS appended after :data:`BASE_CSS`.
    extra_js:
        Page-specific JS appended after :data:`BASE_JS`.
    body_class:
        Optional class on ``<body>``.
    active_nav:
        Nav path to highlight (e.g. ``"/peers"``).
    show_header:
        Set ``False`` for pages that should not show the auth header (login,
        setup wizard, EULA).
    container_class:
        One of ``container``, ``container-medium``, ``container-narrow``, or
        empty for no container.
    robots_noindex:
        Add ``<meta name="robots" content="noindex,nofollow">`` for private pages.
    description:
        ``<meta name="description">`` content.
    """
    from app.ui.i18n import _ as _i18n, get_catalog, get_locale

    lang = get_locale()
    catalog = get_catalog(lang)

    head_extras = []
    if robots_noindex:
        head_extras.append('<meta name="robots" content="noindex,nofollow">')
    if description:
        head_extras.append(
            f'<meta name="description" content="{_html.escape(description)}">'
        )

    # Inject i18n catalog as JSON for JS consumption. Always embedded inline
    # (no extra HTTP request) and kept small (only the strings actually used).
    import json
    catalog_json = (
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    catalog_script = f'<script>window.I18N_CATALOG = {catalog_json};</script>'

    header_html = _render_header(active_nav, title) if show_header else ""

    main_open = f'<main class="app-main {container_class}">'.rstrip()
    if not container_class:
        main_open = '<main class="app-main">'

    return (
        '<!DOCTYPE html>'
        f'<html lang="{lang}">'
        "<head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '<meta name="theme-color" content="#0d1117">'
        f"<title>{_html.escape(title)}</title>"
        + svg_favicon
        + "".join(head_extras)
        + f"<style>{BASE_CSS}{extra_css}</style>"
        + "</head>"
        + f'<body class="{body_class}">'
        + '<a href="#main-content" class="skip-link">' + _i18n("nav.skip_to_content") + "</a>"
        + header_html
        + main_open
        + '<div id="main-content" tabindex="-1">'
        + body
        + "</div>"
        + "</main>"
        + catalog_script
        + f"<script>{BASE_JS}</script>"
        + f"<script>initThemeToggle('theme-toggle');</script>"
        + (f"<script>{extra_js}</script>" if extra_js else "")
        + "</body></html>"
    )
