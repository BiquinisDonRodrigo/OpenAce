<div align="center">

# OpenAce

**Proxy HTTP para AceStream con plugins M3U dinámicos, transcodificación HLS, panel de control y consent gate.**

[![Build & Publish Docker image](https://github.com/BiquinisDonRodrigo/OpenAce/actions/workflows/docker-build.yml/badge.svg)](https://github.com/BiquinisDonRodrigo/OpenAce/actions/workflows/docker-build.yml)
[![Container](https://img.shields.io/badge/ghcr.io-openace-2496ed?logo=docker&logoColor=white)](https://github.com/BiquinisDonRodrigo/OpenAce/pkgs/container/openace)
[![Python](https://img.shields.io/badge/python-3.10-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-gunicorn%2Fgevent-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#licencia)

</div>

---

## Tabla de contenidos

- [Descripcion general](#descripción-general)
- [Características](#características)
- [Arquitectura](#arquitectura)
- [Stack tecnológico](#stack-tecnológico)
- [Inicio rápido](#inicio-rápido)
- [Despliegue con Docker](#despliegue-con-docker)
- [Imagen pre-construida (GHCR)](#imagen-pre-construida-ghcr)
- [Variables de entorno](#variables-de-entorno)
- [Endpoints HTTP](#endpoints-http)
- [Sistema de plugins](#sistema-de-plugins)
- [EULA / Consent gate](#eula--consent-gate)
- [Dashboard y panel de peers](#dashboard-y-panel-de-peers)
- [Channel Checker](#channel-checker)
- [Desarrollo local](#desarrollo-local)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Logging y monitorización](#logging-y-monitorización)
- [CI/CD](#cicd)
- [Solución de problemas](#solución-de-problemas)
- [Aviso legal](#aviso-legal)
- [Licencia](#licencia)

---

## Descripción general

`OpenAce` es un **proxy HTTP** escrito en **Flask + Gunicorn (gevent)** que envuelve al motor [AceStream](https://www.acestream.org/) y expone:

- **Plugins M3U dinámicos** gestionados desde una interfaz web y API REST, con refresco automático programado.
- Streaming **MPEG-TS** directo, reenviando el flujo del engine al cliente.
- Streaming **HLS** (`.m3u8` + segmentos `.ts`) **transcodificado bajo demanda** con FFmpeg.
- **Dashboard completo** con estado del motor, peers P2P, streams activos y plugins.
- **Channel Checker** para verificar el estado de canales de forma individual o masiva.
- **EULA / Consent gate** que requiere aceptación antes de acceder al servicio.

El proyecto se ejecuta como contenedor único (motor AceStream, FFmpeg y proxy Python en la misma imagen) y opcionalmente detrás de una VPN gestionada por [Gluetun](https://github.com/qdm12/gluetun). Incluye un nodo [Kubo (IPFS)](https://github.com/ipfs/kubo) para resolver listas alojadas en IPFS/IPNS.

## Características

- **Sistema de plugins dinámico** — las fuentes M3U se gestionan desde la web (`/plugins`) o la API REST (`/api/plugins`). Crear, editar, eliminar, importar y exportar plugins sin tocar código.
- **Soporte IPFS/IPNS** — las URLs de origen se resuelven a través del nodo Kubo local integrado en el docker-compose.
- **Refresco automático programado** — cada plugin tiene un intervalo configurable; un timer daemon re-descarga la fuente sin bloquear peticiones.
- **Import/Export** — plugins exportables como JSON (individual o todos a la vez), importables desde archivo o URL.
- **HLS bajo demanda** — el primer cliente que pide `/play/hls/<id>` arranca un proceso FFmpeg; un *reaper* lo destruye tras 60 s sin actividad.
- **Channel Checker** — verificación individual o masiva de canales contra el engine, con historial persistido en SQLite.
- **EULA consent gate** — middleware que bloquea el acceso hasta que el usuario acepta el acuerdo, con registro por IP y revocación.
- **Dashboard en tiempo real** — streams activos, peers P2P con geolocalización, velocidades de red, estado del motor y plugins.
- **Stream registry** — tracking de streams activos (MPEG-TS y HLS) con conteo de clientes.
- **Logging JSON estructurado** con rotación diaria mediante `logrotate`.
- **Healthcheck Docker** integrado (`GET /`).
- **Imagen multi-tag publicada en GHCR** en cada push a `main`.

## Arquitectura

```
┌──────────────┐      HTTP      ┌─────────────────────┐      HTTP      ┌──────────────────┐
│   Cliente    │ ─────────────► │  Flask Proxy :8888  │ ─────────────► │ AceStream Engine │
│ (IPTV/VLC)   │                │   (gunicorn+gevent) │                │      :6878       │
└──────────────┘                └─────────┬───────────┘                └──────────────────┘
                                          │                                     │
                                          │ spawn (rutas /play/hls/*)           │ P2P
                                          ▼                                     ▼
                                    ┌──────────┐                         ┌────────────┐
                                    │  ffmpeg  │  ── HLS ──► cliente    │   Red P2P  │
                                    └──────────┘                         └────────────┘
                                          │
                              ┌───────────┴───────────┐
                              │    Kubo (IPFS node)   │
                              │     :48080 gateway    │
                              └───────────────────────┘
```

### Variantes de despliegue

| Archivo | VPN | IPFS | Uso |
|---|---|---|---|
| `docker-compose.yaml` | Gluetun (WireGuard) | Kubo | Producción con VPN |
| `docker-compose.simple.yaml` | No | Kubo | Producción sin VPN |
| `docker-compose.dev.yaml` | Gluetun (WireGuard) | Kubo | Desarrollo con hot-reload |
| `docker-compose.dev.simple.yaml` | No | Kubo | Desarrollo sin VPN |

### Procesos dentro del contenedor

1. **`start-engine`** — binario AceStream en background. Si el contenedor está adjunto a Gluetun, el puerto se descubre desde `/tmp/gluetun/forwarded_port`; en otro caso usa `ACESTREAM_PORT`.
2. **`gunicorn`** — sirve la app Flask en `0.0.0.0:8888` con worker `gevent` y `--timeout 3600` (necesario para streaming largo).
3. **`ffmpeg`** — instancias *spawned on-demand* desde el blueprint HLS, escribiendo segmentos a `/tmp/openace/<content_id>/`.
4. **`cron` + `logrotate`** — rotación diaria de los logs.
5. **Hilos daemon** — uno por plugin para refresco, un *reaper* para FFmpeg, y el runner del channel checker.

### Base de datos

Una única base SQLite (`data.db`, ubicada en el volumen `./data`) almacena:

- **Plugins** — configuración de cada fuente M3U.
- **Channels** — resultados del channel checker (estado, peers, velocidad, timestamps).
- **EULA consents** — registros de aceptación/revocación por IP.

## Stack tecnológico

| Componente | Tecnología |
|---|---|
| Motor P2P | AceStream Engine 3.2.11 (Python 3.10) |
| Web framework | Flask |
| WSGI server | Gunicorn con worker `gevent` |
| Transcodificación | FFmpeg (HLS, copy codec) |
| Base de datos | SQLite (plugins, checker, EULA) |
| Cliente HTTP | `requests` con `HTTPAdapter` y reintentos |
| IPFS | Kubo (nodo local, gateway en :48080) |
| Logging | `python-json-logger` + `RotatingFileHandler` |
| Contenedores | Docker + Docker Compose v2 |
| VPN (opcional) | Gluetun (WireGuard / ProtonVPN) |
| Registry | GitHub Container Registry |
| CI | GitHub Actions + Docker Buildx |

## Inicio rápido

```bash
# 1. Clonar y configurar
git clone https://github.com/BiquinisDonRodrigo/OpenAce.git
cd OpenAce
cp env-example .env
# Editar .env con tu clave WireGuard (solo si usas VPN)

# 2. Arrancar (sin VPN)
docker compose -f docker-compose.simple.yaml up -d

# 3. Abrir el dashboard
# http://localhost:8888/panel
```

Al primer acceso aparecerá la pantalla EULA. Tras aceptar, puedes:

1. Ir a **Plugins** (`/plugins`) y crear una fuente M3U.
2. Copiar la URL de playlist generada (`http://host:8888/<plugin>/mpegts.m3u`).
3. Abrir la URL en tu reproductor IPTV.

## Despliegue con Docker

### Opcion A — Con VPN (Gluetun + WireGuard)

Recomendado en producción. Todo el tráfico saliente pasa por la VPN (`network_mode: service:acestream-vpn`). Si la VPN soporta *port forwarding*, el motor lo descubre automáticamente.

```bash
cp env-example .env
# Editar WG_PRIVATE_KEY con tu clave WireGuard
docker compose up -d
```

Puertos expuestos: `8888` (proxy), `8001` (panel Gluetun), `4001` (IPFS swarm), `5001` (IPFS API, solo localhost).

### Opcion B — Sin VPN

Para entornos donde la red ya está controlada:

```bash
docker compose -f docker-compose.simple.yaml up -d
```

Puertos expuestos: `8888` (proxy), `4001` (IPFS swarm), `5001` (IPFS API, solo localhost).

### Opcion C — Desarrollo con hot-reload

Construye localmente y monta `app/` para iterar sin reconstruir la imagen:

```bash
# Con VPN
docker compose -f docker-compose.dev.yaml up --build

# Sin VPN
docker compose -f docker-compose.dev.simple.yaml up --build
```

Flask arranca en modo `--debug --reload` con el código fuente montado como volumen.

## Imagen pre-construida (GHCR)

Cada push a `main` publica:

| Tag | Descripción |
|---|---|
| `ghcr.io/biquinisdonrodrigo/openace:latest` | Última versión estable |
| `ghcr.io/biquinisdonrodrigo/openace:<sha>` | Pinneada al commit |

```bash
docker pull ghcr.io/biquinisdonrodrigo/openace:latest
```

## Variables de entorno

Todas tienen valor por defecto. Ver `env-example` para una plantilla.

| Variable | Default | Descripción |
|---|---|---|
| `TZ` | `Europe/Madrid` | Zona horaria del contenedor. |
| `WG_PRIVATE_KEY` | — | Clave privada WireGuard. Solo con `docker-compose.yaml` (Gluetun). |
| `ProtonCountries` | `switzerland,spain` | Países para la conexión VPN (separados por coma). |
| `ACESTREAM_HOST` | `127.0.0.1` | Host donde el proxy contacta al engine. |
| `ACESTREAM_PORT` | `6878` | Puerto del engine (fallback si Gluetun no provee port forwarding). |

Variables inyectadas automáticamente por los docker-compose (no necesitan configurarse en `.env`):

| Variable | Valor | Descripción |
|---|---|---|
| `IPFS_GATEWAY` | `http://kubo:48080` | Gateway del nodo Kubo para resolver IPFS/IPNS. |
| `DB_PATH` | `/openace/checkdb/data.db` | Ruta de la base de datos SQLite. |

## Endpoints HTTP

### Páginas web

| Ruta | Descripción |
|---|---|
| `/panel` | Dashboard principal con accesos directos. |
| `/peers` | Panel de estado: motor, peers P2P, streams, plugins, red. |
| `/check` | Channel Checker: comprobación manual y masiva. |
| `/plugins` | Gestión de plugins: crear, editar, eliminar, importar/exportar. |
| `/eula` | Acuerdo de licencia de usuario final. |

### Streaming

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/` | Healthcheck. Devuelve `OpenAce is running`. |
| GET | `/<plugin>/mpegts.m3u` | Playlist M3U con URLs `/play/mpegts/<id>`. |
| GET | `/<plugin>/hls.m3u` | Playlist M3U con URLs `/play/hls/<id>`. |
| GET | `/play/mpegts/<content_id>` | Proxy MPEG-TS directo al engine. |
| GET | `/play/hls/<content_id>` | Manifiesto HLS; arranca FFmpeg si no existe. |
| GET | `/play/hls/<content_id>/<seg>` | Segmento `.ts`. Refresca el TTL de FFmpeg. |

### API de plugins

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/plugins` | Listar todos los plugins. |
| POST | `/api/plugins` | Crear un plugin nuevo. |
| GET | `/api/plugins/<id>` | Detalle de un plugin. |
| PUT | `/api/plugins/<id>` | Actualizar un plugin. |
| DELETE | `/api/plugins/<id>` | Eliminar un plugin. |
| POST | `/api/plugins/<id>/refresh` | Forzar refresco de un plugin. |
| POST | `/api/plugins/<id>/import` | Importar M3U (archivo o JSON) a un plugin. |
| GET | `/api/plugins/<id>/channels` | Listar canales cacheados de un plugin. |
| GET | `/api/plugins/<id>/export` | Exportar un plugin como JSON. |
| GET | `/api/plugins/export` | Exportar todos los plugins como JSON. |
| POST | `/api/plugins/import` | Importar plugins desde JSON. |

### API del checker

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/check/single` | Comprobar un canal individual. |
| POST | `/check/start` | Iniciar comprobación masiva (filtrable). |
| POST | `/check/stop` | Detener comprobación masiva en curso. |
| GET | `/check/status` | Estado actual del runner (progreso, contadores). |
| GET | `/check/results` | Resultados con filtros por estado/plugin/grupo. |

### API de EULA

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/api/eula/accept` | Aceptar el EULA (requiere frase literal). |
| POST | `/api/eula/revoke` | Revocar consentimiento. |
| GET | `/api/eula/status` | Consultar estado de aceptación. |

### API del dashboard

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/api/peers/status` | Estado completo: motor, IP, peers, streams, plugins, conexiones. |

### Notas sobre HLS

- El primer GET a `/play/hls/<id>` puede tardar hasta **30 s** mientras se genera el primer manifiesto.
- La playlist se reescribe para que los segmentos apunten al proxy (`/play/hls/<id>/segNNN.ts`).
- Tras **60 s** sin peticiones el reaper mata el proceso y limpia los segmentos.
- FFmpeg usa `-c copy` (sin recomprimir): muy bajo coste de CPU.

## Sistema de plugins

Los plugins se gestionan completamente desde la interfaz web o la API REST. Cada plugin define:

| Campo | Descripción |
|---|---|
| `display_name` | Nombre visible en la interfaz. |
| `name` | Slug URL-safe (se genera automáticamente si no se proporciona). |
| `source_url` | URL de la lista M3U (HTTP/HTTPS/IPFS/IPNS). |
| `refresh_interval` | Segundos entre refrescos automáticos. |
| `enabled` | Activa o desactiva el plugin. |

### Flujo de un plugin

1. **Creación** — via `/plugins` (web) o `POST /api/plugins`.
2. **Fetch inicial** — se descarga la M3U de `source_url`, se parsea y se cachean los canales en memoria.
3. **Timer de refresco** — un hilo daemon re-descarga cada `refresh_interval` segundos.
4. **Playlist disponible** — accesible en `/<slug>/mpegts.m3u` o `/<slug>/hls.m3u`.

### Resolución IPFS/IPNS

Si la `source_url` contiene una ruta `/ipfs/` o `/ipns/`, se reescribe automáticamente para usar el gateway Kubo local (`IPFS_GATEWAY`), evitando depender de gateways públicos.

### Import/Export

- **Exportar** un plugin o todos como JSON desde la web o `GET /api/plugins/export`.
- **Importar** un JSON con la definición de plugins (incluyendo canales opcionalmente) via `POST /api/plugins/import`.
- **Subir M3U** directamente a un plugin via la web o `POST /api/plugins/<id>/import`.

## EULA / Consent gate

Un middleware intercepta todas las peticiones y redirige a `/eula` si el usuario (identificado por IP) no ha aceptado el acuerdo.

- El usuario debe escribir la frase literal **"He leído y acepto el acuerdo"**.
- Se almacena un hash SHA-256 de la frase (nunca el texto plano), la IP, el User-Agent y la marca temporal.
- El consentimiento puede revocarse desde la misma página.
- Los datos se guardan en la tabla `eula_consents` de la base de datos SQLite local.
- Las rutas `/eula`, `/api/eula/` y `/static/` están exentas del guard.

## Dashboard y panel de peers

### Dashboard (`/panel`)

Página central con accesos directos a las cuatro secciones:

- **Peers & Estado** — panel de monitorización en tiempo real.
- **Channel Checker** — verificación de canales.
- **EULA** — gestión de consentimiento.
- **Plugins** — gestión de fuentes M3U.

### Panel de peers (`/peers`)

Auto-refresco cada 5 segundos con:

- **Reproduciendo ahora** — streams activos con formato (MPEG-TS/HLS), clientes conectados y duración.
- **Motor AceStream** — estado online/offline, versión, resumen de conexiones.
- **Peers P2P** — tabla con IP remota, estado TCP, organización/ISP, ciudad, país, timezone, coordenadas, velocidades de bajada y subida por peer.
- **Conexiones** — clientes entrantes (:8888), conexiones al motor (:6878), conexiones externas.
- **Plugins** — nombre, canales cargados, intervalo de refresco, último refresco.
- **Streams HLS** — procesos FFmpeg activos con PID, estado y tiempo de inactividad.

## Channel Checker

Accesible desde `/check`, permite verificar si los canales AceStream están vivos.

### Comprobación manual

Pega un infohash, una URL `acestream://...` o una URL con `?id=...`. El checker abre una sesión contra el engine, monitorea el estado y devuelve el resultado (vivo, caído, timeout o error) con peers y velocidad.

### Comprobación masiva

1. Filtra por **plugin**, **grupo** o **estado** (no comprobados, vivos, caídos, etc.).
2. Pulsa **Iniciar** — un runner secuencial comprueba cada canal uno a uno contra el engine.
3. La barra de progreso y los contadores se actualizan en tiempo real.
4. Se puede **detener** en cualquier momento; los canales restantes se marcan como "saltados".

Los resultados quedan persistidos en la base de datos y se muestran en una tabla sortable con filtro de búsqueda. Cada fila permite re-comprobar el canal y copiar las URLs de reproducción (MPEG-TS / HLS).

## Desarrollo local

### Sin Docker

Necesitas Python 3.10, FFmpeg en el `PATH` y un engine AceStream corriendo por separado.

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

### Con Docker (hot-reload)

Ver [Opción C](#opcion-c--desarrollo-con-hot-reload) — `app/` y `server.py` se montan como volumen.

## Estructura del repositorio

```
.
├── app/
│   ├── __init__.py              # Factory de Flask, EULA guard, bootstrap de plugins
│   ├── config.py                # Config desde variables de entorno
│   ├── logging_config.py        # Logging JSON estructurado + rotación
│   ├── plugins/
│   │   └── __init__.py          # (vacío, reservado)
│   ├── routes/
│   │   ├── __init__.py          # Registro de blueprints + FFmpegManager
│   │   ├── play.py              # /play/mpegts/<id>
│   │   ├── hls.py               # /play/hls/<id> + segmentos
│   │   ├── playlist.py          # /<plugin>/mpegts.m3u, /<plugin>/hls.m3u
│   │   ├── panel.py             # /panel, /peers, /api/peers/status
│   │   ├── check.py             # /check (UI + API del checker)
│   │   ├── eula.py              # /eula (UI + API de consentimiento)
│   │   └── plugins_api.py       # /plugins (UI) + /api/plugins (REST CRUD)
│   └── utils/
│       ├── acestream.py         # negotiate/check/stop streams contra el engine
│       ├── upstream.py          # HTTP session pool + streaming a cliente
│       ├── ffmpeg_manager.py    # Spawn + reaper de procesos FFmpeg
│       ├── stream_registry.py   # Tracking de streams activos en memoria
│       ├── check_store.py       # SQLite: schema init, CRUD channels
│       ├── check_runner.py      # Runner secuencial de comprobación masiva
│       ├── eula_store.py        # accept/revoke/status EULA en SQLite
│       ├── plugin_store.py      # CRUD plugins en SQLite
│       ├── plugin_cache.py      # Cache en memoria de canales por plugin
│       ├── plugin_refresh.py    # Fetch M3U, parseo, timers de refresco
│       ├── m3u_parser.py        # Parser de EXTINF/EXTGRP + extracción de infohash
│       └── logging_utils.py     # Helper log_event()
├── ipfs/
│   └── container-init.d/
│       └── 001-gateway-port.sh  # Configura el gateway Kubo en puerto 48080
├── logrotate/
│   └── acestream.conf           # Política de rotación de logs
├── Dockerfile                   # Imagen: Python slim + AceStream + FFmpeg
├── docker-compose.yaml          # Producción con Gluetun + Kubo
├── docker-compose.simple.yaml   # Producción sin VPN + Kubo
├── docker-compose.dev.yaml      # Desarrollo con Gluetun + Kubo + hot-reload
├── docker-compose.dev.simple.yaml # Desarrollo sin VPN + Kubo + hot-reload
├── server.py                    # Entry point WSGI
├── start.sh                     # Entrypoint del contenedor
├── requirements.txt
├── env-example
└── .github/workflows/
    └── docker-build.yml         # CI: build + push a GHCR
```

## Logging y monitorización

- **Formato JSON** en stdout y en `/var/log/openace/proxy.log` (5 MB, 2 backups, vía `RotatingFileHandler`).
- **Campos por evento**: `timestamp` (UTC ISO-8601), `level`, `component`, `event` y campos arbitrarios.
- **Componentes**: `play_proxy`, `hls_ffmpeg`, `ffmpeg_manager`, `playlist_proxy`, `acestream`, `upstream`, `check`, `check_runner`, `check_store`, `eula`, `plugins_api`, `plugin_refresh`, `core`.
- **Logrotate** via cron diario sobre `acestream.log` y `proxy.log`.
- **Healthcheck Docker**: `curl -fsS http://127.0.0.1:8888/` cada 30 s.

```bash
docker logs -f open-ace                                    # JSON en stdout
docker exec open-ace tail -f /var/log/openace/proxy.log    # log del proxy
docker exec open-ace tail -f /var/log/openace/acestream.log # log del engine
```

## CI/CD

Workflow: [`.github/workflows/docker-build.yml`](.github/workflows/docker-build.yml)

| Trigger | Acción |
|---|---|
| `push` a main | Construye y publica `:latest` y `:<sha>` en GHCR. |
| `pull_request` | Solo construye (no publica). |

- Usa **Buildx** con caché de GitHub Actions (`type=gha,mode=max`).
- Permisos: `contents: read`, `packages: write`.
- Autenticación: `GITHUB_TOKEN` automático.

## Solución de problemas

<details>
<summary><b>La playlist responde 503 "Playlist not ready, retry in a moment."</b></summary>

El plugin todavía no ha completado su primer fetch. Revisa los logs por eventos `plugin_fetched` o `plugin_fetch_failed`. Espera unos segundos y reintenta.
</details>

<details>
<summary><b>HLS responde 503 "Stream buffering, retry"</b></summary>

FFmpeg arrancó pero todavía no ha producido el primer segmento (timeout de 30 s). El primer arranque para un infohash frío puede ser lento. Reintenta.
</details>

<details>
<summary><b>Streaming MPEG-TS funciona pero HLS no</b></summary>

Verifica que FFmpeg esté disponible: `docker exec open-ace ffmpeg -version`. Asegúrate de que el contenedor pueda escribir en `/tmp/openace/`.
</details>

<details>
<summary><b>Gluetun no expone el puerto forwarded</b></summary>

`start.sh` espera hasta 20 s a que aparezca `/tmp/gluetun/forwarded_port`. Si no lo encuentra, cae a `ACESTREAM_PORT` (6878). Causas: VPN sin port forwarding habilitado, server en país sin soporte, o clave WireGuard inválida.
</details>

<details>
<summary><b>Los plugins con URL IPFS/IPNS no cargan canales</b></summary>

Verifica que el contenedor Kubo esté sano: `docker exec kubo ipfs id`. El gateway debe escuchar en el puerto 48080. Comprueba que la variable `IPFS_GATEWAY` apunte a `http://kubo:48080` en el docker-compose.
</details>

<details>
<summary><b>El EULA no me deja pasar</b></summary>

Debes escribir la frase exacta: **He leído y acepto el acuerdo**. Respeta mayúsculas, tildes y espacios. Si previamente revocaste el consentimiento, acepta de nuevo.
</details>

<details>
<summary><b>El channel checker se queda "busy"</b></summary>

Solo puede haber una comprobación masiva en curso. Si el runner está activo, espera a que termine o pulsa **Parar**. La comprobación manual también serializa contra el runner para no sobrecargar el engine.
</details>

## Aviso legal

Este proyecto es una **infraestructura técnica** (proxy + agregador) que no aloja, distribuye ni produce contenido alguno. Los infohashes y listas M3U provienen de **fuentes públicas de terceros** y son responsabilidad exclusiva de quien los publica y de quien los consume. El usuario final es responsable de cumplir la legislación aplicable en su jurisdicción. Los autores no se hacen responsables del uso indebido del software.

## Licencia

Distribuido bajo licencia **MIT**. Consulta el archivo `LICENSE` para más detalles.
