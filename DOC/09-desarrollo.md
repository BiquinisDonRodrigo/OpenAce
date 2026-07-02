# Desarrollo

Guia para desarrollar y contribuir a OpenAce.

## Entorno local sin Docker

Requisitos: Python 3.10, FFmpeg en el `PATH` y un motor AceStream corriendo por separado.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=.
export ACESTREAM_HOST=127.0.0.1 ACESTREAM_PORT=6878
export DB_PATH=/tmp/openace/data.db
python server.py
```

Para hot-reload:

```bash
flask --app server:app run --host 0.0.0.0 --port 8888 --debug --reload
```

## Entorno local con Docker (hot-reload)

Construye localmente y monta `app/` para iterar sin reconstruir la imagen:

```bash
# Sin VPN
docker compose -f docker-compose.dev.simple.yaml up --build

# Con VPN
docker compose -f docker-compose.dev.yaml up --build
```

Flask arranca en modo `--debug --reload` con `app/` y `server.py` montados como volumenes.

## Estructura del repositorio

```
.
├── app/
│   ├── __init__.py              # Factory de Flask, guards, bootstrap de plugins
│   ├── config.py                # Config desde variables de entorno
│   ├── logging_config.py        # Logging JSON estructurado a stdout
│   ├── plugins/
│   │   └── __init__.py          # (vacio, reservado)
│   ├── routes/
│   │   ├── __init__.py          # Registro de blueprints + FFmpegManager
│   │   ├── play.py              # /play/mpegts/<id> via FFmpegManager
│   │   ├── hls.py               # /play/hls/<id> + segmentos
│   │   ├── playlist.py          # /<plugin>/mpegts.m3u, /<plugin>/hls.m3u
│   │   ├── panel.py             # /panel, /peers, /api/peers/status
│   │   ├── check.py             # /check (UI + API del checker)
│   │   ├── eula.py              # /eula (UI + API de consentimiento)
│   │   ├── plugins_api.py       # /plugins (UI) + /api/plugins (REST CRUD)
│   │   ├── auth.py              # /login, /admin/users, /api/auth, /api/admin
│   │   └── setup.py             # /setup (wizard de configuracion inicial)
│   └── utils/
│       ├── acestream.py         # negotiate/check/stop streams contra el engine
│       ├── ffmpeg_manager.py    # Spawn + reaper de procesos FFmpeg MPEG-TS/HLS
│       ├── stream_registry.py   # Tracking de streams activos en memoria
│       ├── check_store.py       # SQLite: schema init, CRUD channels
│       ├── check_runner.py      # Runner secuencial de comprobacion masiva
│       ├── eula_store.py        # accept/revoke/status EULA en SQLite
│       ├── auth_store.py        # Usuarios, sesiones, tokens API en SQLite
│       ├── auth_helpers.py      # current_user(), require_role(), role hierarchy
│       ├── setup_store.py       # Estado del asistente de configuracion
│       ├── plugin_store.py      # CRUD plugins en SQLite
│       ├── plugin_cache.py      # Cache en memoria de canales por plugin
│       ├── plugin_refresh.py    # Fetch M3U, parseo, timers de refresco
│       ├── m3u_parser.py        # Parser de EXTINF/EXTGRP + extraccion de infohash
│       └── logging_utils.py     # Helper log_event()
├── ipfs/
│   └── container-init.d/
│       └── 001-gateway-port.sh  # Configura el gateway Kubo en puerto 48080
├── nginx/
│   └── openace.conf             # Configuracion nginx reverse proxy con SSL
├── DOC/                         # Documentacion del proyecto
├── Dockerfile                   # Imagen: Python slim + AceStream + FFmpeg
├── docker-compose.simple.yaml   # Produccion selfhost casa/LAN sin VPN + Kubo (:8888 publico en la LAN)
├── docker-compose.yaml          # Produccion selfhost casa/LAN con Gluetun + Kubo (:8888 publico en la LAN)
├── docker-compose.vps.simple.yaml # Produccion VPS sin VPN + nginx reverse proxy (:8888 solo localhost)
├── docker-compose.vps.yaml      # Produccion VPS con Gluetun + nginx reverse proxy (:8888 solo localhost)
├── docker-compose.dev.simple.yaml # Desarrollo sin VPN + Kubo + hot-reload
├── docker-compose.dev.yaml      # Desarrollo con Gluetun + Kubo + hot-reload
├── server.py                    # Entry point WSGI
├── start.sh                     # Entrypoint del contenedor
├── requirements.txt
├── env-example
└── .github/workflows/
    └── docker-build.yml         # CI: build + push a GHCR
```

## Arquitectura interna

### Entry point

`server.py` → `app.create_app()` (Flask factory). Gunicorn con gevent worker sirve la app en produccion (`start.sh`); Flask dev server en desarrollo.

### Blueprints (app/routes/)

Cada fichero es un Flask blueprint. Las paginas HTML usan `render_template_string` (HTML inline, sin ficheros de plantilla).

| Fichero | Rutas | Proposito |
|---|---|---|
| `play.py` | `/play/mpegts/<id>` | Stream MPEG-TS servido por FFmpegManager |
| `hls.py` | `/play/hls/<id>`, segmentos | HLS via procesos FFmpeg bajo demanda |
| `playlist.py` | `/<plugin>/mpegts.m3u`, `/<plugin>/hls.m3u` | Generacion de playlists M3U |
| `panel.py` | `/panel`, `/peers`, `/api/peers/status` | Dashboard y monitorizacion de peers |
| `check.py` | `/check`, `/check/*` | Channel checker UI y API |
| `eula.py` | `/eula`, `/api/eula/*` | EULA consent gate UI y API |
| `plugins_api.py` | `/plugins`, `/api/plugins/*` | Gestion de plugins UI y REST CRUD |
| `auth.py` | `/login`, `/logout`, `/admin/users`, `/api/auth/*`, `/api/admin/*` | Autenticacion, usuarios y tokens |
| `setup.py` | `/setup/*`, `/api/setup/*` | Asistente de configuracion inicial |

### Utilidades (app/utils/)

| Fichero | Funcion |
|---|---|
| `acestream.py` | Negociar/comprobar/parar streams contra el engine HTTP API |
| `ffmpeg_manager.py` | Spawn de FFmpeg para MPEG-TS/HLS, colas por cliente y reaper configurable con `OPENACE_IDLE_TIMEOUT_S` |
| `stream_registry.py` | Tracking en memoria de streams MPEG-TS y HLS activos |
| `plugin_store.py` | SQLite CRUD para definiciones de plugins |
| `plugin_cache.py` | Cache en memoria de canales parseados por plugin |
| `plugin_refresh.py` | Fetch de fuentes M3U, parseo, timers daemon de refresco |
| `m3u_parser.py` | Parser de lineas EXTINF/EXTGRP, extraccion de infohashes AceStream |
| `check_store.py` | Schema init y CRUD SQLite para resultados del checker |
| `check_runner.py` | Runner secuencial de comprobacion masiva |
| `eula_store.py` | Accept/revoke/status EULA en SQLite |
| `auth_store.py` | CRUD de usuarios, sesiones y tokens API; hashing de passwords (werkzeug); rate limiting en memoria |
| `auth_helpers.py` | `current_user()`, `require_role()`, jerarquia de roles (admin > user > viewer) |
| `setup_store.py` | Key-value SQLite para estado del wizard |

### Comportamientos en runtime

- **Setup guard**: `app/__init__.py` `before_request` redirige todas las rutas a `/setup` hasta que se complete el wizard. Auto-setup posible via variables de entorno.
- **EULA guard**: `before_request` redirige a `/eula` hasta la aceptacion global.
- **Auth guard**: `before_request` autentica via cookie, Bearer, `?token=` o Basic Auth. Roles por ruta.
- **Plugin bootstrap**: Tras completar el setup, `plugin_refresh.bootstrap_all()` carga plugins desde SQLite e inicia los timers.
- **FFmpegManager**: Instanciado una vez en `app/routes/__init__.py`, inyectado en el blueprint HLS via `set_manager()`.
- **FFmpegManager**: Gestiona salidas MPEG-TS/HLS, colas por cliente, limite de streams (`OPENACE_MAX_STREAMS`) y reaper configurable (`OPENACE_IDLE_TIMEOUT_S`).
- **IPFS**: URLs con `/ipfs/` o `/ipns/` se reescriben al gateway Kubo local (`IPFS_GATEWAY`).

### Base de datos

SQLite unica en `DB_PATH` (default `/openace/checkdb/data.db`). Seis dominios: plugins, resultados del checker, consentimientos EULA, usuarios, tokens/sesiones API, y estado del setup. Cada store module inicializa su schema automaticamente.

## Logging y monitorizacion

- **Formato JSON** en stdout/stderr, visible con `docker logs`.
- **Campos por evento**: `timestamp` (UTC ISO-8601), `level`, `component`, `event` y campos arbitrarios.
- **Componentes**: `play_proxy`, `hls_ffmpeg`, `ffmpeg_manager`, `playlist_proxy`, `acestream`, `check`, `check_runner`, `check_store`, `eula`, `plugins_api`, `plugin_refresh`, `core`.
- **AceStream** tambien se redirige a stdout/stderr desde `start.sh`.
- **Healthcheck Docker**: `curl -fsS http://127.0.0.1:8888/healthz` cada 30 s. Es un chequeo profundo: ademas de Flask prueba el motor AceStream y reporta la sincronizacion del puerto P2P con Gluetun (200 si el motor responde, 503 si esta caido).

```bash
docker logs -f open-ace
```

## Build de imagen

El `Dockerfile` acepta build args para cambiar o fijar el tarball del engine AceStream:

| Build arg | Descripcion |
|---|---|
| `ACESTREAM_URL` | URL del tarball AceStream a descargar durante el build |
| `ACESTREAM_SHA256` | SHA-256 esperado del tarball; si se define, el build lo verifica |

El script `ipfs/container-init.d/001-gateway-port.sh` configura Kubo para exponer el gateway en `/ip4/0.0.0.0/tcp/48080` dentro del contenedor.

## CI/CD

Workflow: [`.github/workflows/docker-build.yml`](../.github/workflows/docker-build.yml)

| Trigger | Accion |
|---|---|
| `push` a `master` | Construye y publica `:latest` y `:<sha>` en GHCR. |
| `pull_request` | Solo construye (no publica). |

- Usa **Buildx** con cache de GitHub Actions (`type=gha,mode=max`).
- Permisos: `contents: read`, `packages: write`.
- Autenticacion: `GITHUB_TOKEN` automatico.

## Imagen pre-construida (GHCR)

Cada push a `master` publica:

| Tag | Descripcion |
|---|---|
| `ghcr.io/biquinisdonrodrigo/openace:latest` | Ultima version estable |
| `ghcr.io/biquinisdonrodrigo/openace:<sha>` | Pinneada al commit |

```bash
docker pull ghcr.io/biquinisdonrodrigo/openace:latest
```
