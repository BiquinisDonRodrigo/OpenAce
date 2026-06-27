<div align="center">

# OpenAce

**Proxy HTTP para AceStream con autenticacion multiusuario, plugins M3U dinamicos, streaming MPEG-TS/HLS gestionado por FFmpeg, panel de control y asistente de configuracion inicial.**

[![Build & Publish Docker image](https://github.com/BiquinisDonRodrigo/OpenAce/actions/workflows/docker-build.yml/badge.svg)](https://github.com/BiquinisDonRodrigo/OpenAce/actions/workflows/docker-build.yml)
[![Container](https://img.shields.io/badge/ghcr.io-openace-2496ed?logo=docker&logoColor=white)](https://github.com/BiquinisDonRodrigo/OpenAce/pkgs/container/openace)
[![Python](https://img.shields.io/badge/python-3.10-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-gunicorn%2Fgevent-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](#licencia)

</div>

---

## Que es OpenAce

OpenAce es un **proxy HTTP** escrito en **Flask + Gunicorn (gevent)** que envuelve al motor [AceStream](https://www.acestream.org/). Se ejecuta como contenedor unico (motor AceStream, FFmpeg y proxy Python en la misma imagen) y opcionalmente detras de una VPN gestionada por [Gluetun](https://github.com/qdm12/gluetun). Incluye un nodo [Kubo (IPFS)](https://github.com/ipfs/kubo) para resolver listas alojadas en IPFS/IPNS.

## Caracteristicas

- **Asistente de configuracion inicial** — wizard de 4 pasos al primer acceso. Soporta auto-setup por variables de entorno.
- **Autenticacion multiusuario** — tres roles (admin/user/viewer), sesiones, tokens API (Bearer, URL, Basic Auth) y rate limiting.
- **Plugins M3U dinamicos** — fuentes M3U gestionadas desde la web o API REST, con refresco automatico.
- **Streaming MPEG-TS/HLS bajo demanda** — FFmpeg genera salidas MPEG-TS y HLS con codec copy.
- **Reaper configurable** — cierre automatico de streams inactivos con `OPENACE_IDLE_TIMEOUT_S` (180s por defecto).
- **Soporte IPFS/IPNS** — resolucion via nodo Kubo local.
- **Import/Export** — plugins exportables como JSON, importables desde archivo o URL.
- **Dashboard en tiempo real** — streams activos, peers P2P con geolocalizacion, estado del motor.
- **Channel Checker** — verificacion individual o masiva contra el engine.
- **Gestion de usuarios y tokens API** — panel de administracion con CRUD.
- **EULA consent gate** — aceptacion obligatoria con registro por IP.
- **Soporte reverse proxy** — ProxyFix + configuracion nginx con SSL incluida.
- **Logging JSON estructurado** por stdout/stderr, compatible con `docker logs`.
- **Healthcheck Docker** integrado.

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

## Inicio rapido

```bash
git clone https://github.com/BiquinisDonRodrigo/OpenAce.git
cd OpenAce
# Selfhost en casa / LAN, sin VPN
docker compose -f docker-compose.simple.yaml up -d
```

Accede a `http://localhost:8888`. Al primer acceso aparecera el asistente de configuracion (EULA, usuarios, plugins, resumen). Este inicio rapido esta pensado para casa/LAN; para publicar en internet usa siempre reverse proxy y no expongas `8888` directamente. Tras completarlo:

1. Inicia sesion con el usuario admin.
2. Crea un plugin M3U en `/plugins` (si no lo hiciste en el asistente).
3. Copia la URL de playlist generada (`http://host:8888/<plugin>/mpegts.m3u?token=<tu-token>`).
4. Abre la URL en tu reproductor IPTV.

Para despliegue con VPN, reverse proxy, VPS o acceso remoto, consulta la [documentacion completa](DOC/README.md).

## Elige tu escenario de despliegue

| Escenario | Guia recomendada | Notas |
|---|---|---|
| Selfhost en casa/LAN | [Escenarios selfhost](DOC/11-escenarios-selfhost.md#1-selfhost-en-casa-con-acceso-directo) | Acceso directo a `http://IP:8888`, `REVERSE_PROXY=false` |
| Casa con reverse proxy en VPS | [Escenarios selfhost](DOC/11-escenarios-selfhost.md#2-selfhost-en-casa-con-reverse-proxy-en-una-vps) | Requiere tunel privado entre VPS y casa |
| Casa con reverse proxy local | [Escenarios selfhost](DOC/11-escenarios-selfhost.md#3-selfhost-en-casa-con-reverse-proxy-en-la-misma-maquina) | Nginx/Caddy en la misma maquina, OpenAce en localhost |
| Selfhost en VPS | [Despliegue en VPS](DOC/03-despliegue-vps.md) | Publicar solo `80/443`, OpenAce en `127.0.0.1:8888` |

## Stack tecnologico

| Componente | Tecnologia |
|---|---|
| Motor P2P | AceStream Engine 3.2.11 (Python 3.10) |
| Web framework | Flask |
| WSGI server | Gunicorn con worker `gevent` |
| Transcodificacion | FFmpeg (HLS, copy codec) |
| Base de datos | SQLite |
| Cliente HTTP | `requests` con `HTTPAdapter` y reintentos |
| IPFS | Kubo (nodo local, gateway en :48080) |
| Contenedores | Docker + Docker Compose v2 |
| VPN (opcional) | Gluetun (WireGuard / ProtonVPN) |
| CI | GitHub Actions + Docker Buildx |
| Registry | GitHub Container Registry |

## Documentacion

| Guia | Descripcion |
|---|---|
| [Despliegue rapido](DOC/01-despliegue-rapido.md) | Funcionando en menos de 5 minutos |
| [Despliegue paso a paso](DOC/02-despliegue-paso-a-paso.md) | Entender cada componente |
| [Despliegue en VPS](DOC/03-despliegue-vps.md) | Nginx, SSL y acceso desde internet |
| [Configuracion inicial](DOC/04-configuracion-inicial.md) | Asistente de setup y auto-setup |
| [Escenarios selfhost](DOC/11-escenarios-selfhost.md) | Casa, proxy local, proxy en VPS y VPS completo |
| [Modulos](DOC/05-modulos.md) | Dashboard, peers, checker, plugins, usuarios |
| [Reproductores](DOC/06-reproductores.md) | TiviMate, Kodi, VLC, Jellyfin y otros |
| [Securizacion VPS](DOC/07-securizacion-vps.md) | Firewall, fail2ban y SSH |
| [API HTTP](DOC/08-api-referencia.md) | Referencia completa de endpoints |
| [Desarrollo](DOC/09-desarrollo.md) | Entorno local, estructura, CI/CD |
| [Solucion de problemas](DOC/10-solucion-de-problemas.md) | Errores comunes y diagnostico |

## Variables de entorno

Todas tienen valor por defecto. Ver `env-example` para una plantilla completa.

| Variable | Default | Descripcion |
|---|---|---|
| `TZ` | `Europe/Madrid` | Zona horaria del contenedor |
| `WG_PRIVATE_KEY` | — | Clave privada WireGuard (solo con VPN) |
| `ProtonCountries` | — | Paises para la conexion VPN |
| `ACESTREAM_HOST` | `127.0.0.1` | Host del motor AceStream |
| `ACESTREAM_PORT` | `6878` | Puerto del motor AceStream |
| `ACESTREAM_IP` | — | IP publica opcional anunciada por AceStream |
| `IPFS_GATEWAY` | `http://kubo:48080` | URL del gateway IPFS local |
| `DB_PATH` | `/openace/checkdb/data.db` | Ruta de la base de datos SQLite |
| `AUTH_ENABLED` | `true` | Activar/desactivar autenticacion |
| `SESSION_DURATION_HOURS` | `24` | Duracion de la sesion en horas |
| `REVERSE_PROXY` | `false` | Activar soporte reverse proxy; los compose VPS lo activan |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs de proxy permitidas por Gunicorn en modo reverse proxy |
| `PUBLIC_BASE_URL` | — | URL publica opcional para generar playlists M3U absolutas |
| `OPENACE_AUTO_SETUP` | `false` | Auto-setup sin wizard |
| `OPENACE_ADMIN_USER` | `admin` | Nombre del usuario admin |
| `OPENACE_ADMIN_PASSWORD` | — | Contrasena del admin (auto-setup) |
| `OPENACE_EULA_ACCEPT` | `false` | Aceptar EULA automaticamente |
| `OPENACE_IDLE_TIMEOUT_S` | `180` | Segundos de inactividad antes de cerrar un stream FFmpeg |
| `OPENACE_CHUNK_SIZE` | `65536` | Tamano de lectura del pipe FFmpeg en bytes |
| `OPENACE_QUEUE_MAX` | `256` | Tamano maximo de cola por cliente MPEG-TS |
| `OPENACE_PIPE_BUFFER_SIZE` | `1048576` | Tamano del buffer del pipe del OS en bytes |
| `OPENACE_MAX_STREAMS` | `32` | Maximo de streams FFmpeg simultaneos |
| `OPENACE_ITERATE_TIMEOUT_S` | `180` | Timeout de iteracion del stream FFmpeg en segundos |
| `OPENACE_FFMPEG_RW_TIMEOUT_US` | `120000000` | Timeout de lectura/escritura FFmpeg en microsegundos |
| `OPENACE_FFMPEG_RESTARTS` | `3` | Reintentos maximos de FFmpeg por stream |
| `OPENACE_FFMPEG_RESTART_BACKOFF_S` | `2` | Espera entre reintentos de FFmpeg en segundos |
| `OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S` | `30` | Edad maxima de segmentos HLS obsoletos antes de limpieza |
| `GUNICORN_WORKERS` | `1` | Workers Gunicorn (1 recomendado: el estado de FFmpeg/timers es in-memory) |
| `GUNICORN_WORKER_CONNECTIONS` | `2000` | Conexiones por worker gevent |

Detalle completo en [Despliegue paso a paso](DOC/02-despliegue-paso-a-paso.md#variables-disponibles).

## Aviso legal

Este proyecto es una **infraestructura tecnica** (proxy + agregador) que no aloja, distribuye ni produce contenido alguno. Los infohashes y listas M3U provienen de **fuentes publicas de terceros** y son responsabilidad exclusiva de quien los publica y de quien los consume. El usuario final es responsable de cumplir la legislacion aplicable en su jurisdiccion. Los autores no se hacen responsables del uso indebido del software.

## Licencia

Distribuido bajo licencia **MIT**. Consulta el archivo `LICENSE` para mas detalles.
