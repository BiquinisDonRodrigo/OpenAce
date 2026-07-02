# Referencia de API HTTP

Todos los endpoints expuestos por OpenAce.

## Paginas web

| Ruta | Descripcion |
|---|---|
| `/setup` | Asistente de configuracion inicial (4 pasos). Solo visible si no se ha completado. |
| `/login` | Pagina de inicio de sesion. |
| `/panel` | Dashboard principal con accesos directos. |
| `/peers` | Panel de estado: motor, peers P2P, streams, plugins, red. |
| `/check` | Channel Checker: comprobacion manual y masiva. |
| `/plugins` | Gestion de plugins: crear, editar, eliminar, importar/exportar. |
| `/admin/users` | Gestion de usuarios y tokens API (solo admin). |
| `/eula` | Acuerdo de licencia de usuario final. |

## Streaming

| Metodo | Ruta | Descripcion |
|---|---|---|
| GET | `/` | Healthcheck basico. Devuelve `OpenAce is running`. |
| GET | `/healthz` | Healthcheck Docker. Prueba el motor AceStream y reporta el estado VPN/P2P (puerto activo vs. Gluetun). HTTP 200 si el motor responde, 503 si esta caido. Exento de setup/EULA/auth. |
| GET | `/<plugin>/mpegts.m3u` | Playlist M3U con URLs `/play/mpegts/<id>`. |
| GET | `/<plugin>/hls.m3u` | Playlist M3U con URLs `/play/hls/<id>`. |
| GET | `/play/mpegts/<content_id>` | Stream MPEG-TS generado por FFmpeg desde AceStream. |
| GET | `/play/hls/<content_id>` | Manifiesto HLS; arranca FFmpeg si no existe y puede redirigir con `hls_client`. |
| GET | `/play/hls/<content_id>/<seg>` | Segmento `.ts`. Refresca el TTL de FFmpeg. |

### Notas sobre HLS

- El primer GET a `/play/hls/<id>` redirige con `?hls_client=<uuid>` para identificar al cliente HLS.
- El primer manifiesto puede tardar hasta **30 s** mientras FFmpeg genera segmentos.
- La playlist se reescribe para que los segmentos apunten al proxy (`/play/hls/<id>/segNNN.ts`) propagando `token` y `hls_client`.
- Si el manifiesto queda obsoleto durante mas de 12 s, el stream se descarta y responde 503 `Stream stale, retry`.
- Tras `OPENACE_IDLE_TIMEOUT_S` segundos sin peticiones (180 por defecto), el reaper mata el proceso y limpia los segmentos.
- FFmpeg usa `-c copy` (sin recomprimir): bajo coste de CPU.
- Las respuestas de streaming usan cabeceras anti-cache/anti-buffer (`Cache-Control: no-store`, `X-Accel-Buffering: no`).

## API de plugins

| Metodo | Ruta | Descripcion |
|---|---|---|
| GET | `/api/plugins` | Listar todos los plugins. |
| POST | `/api/plugins` | Crear un plugin nuevo. |
| GET | `/api/plugins/<id>` | Detalle de un plugin. |
| PUT | `/api/plugins/<id>` | Actualizar un plugin. |
| DELETE | `/api/plugins/<id>` | Eliminar un plugin. |
| POST | `/api/plugins/<id>/refresh` | Forzar refresco de un plugin. Si ya hay un refresco en curso devuelve 409. |
| POST | `/api/plugins/<id>/import` | Importar M3U (archivo o JSON) a un plugin. |
| GET | `/api/plugins/<id>/channels` | Listar canales cacheados de un plugin. |
| GET | `/api/plugins/<id>/export` | Exportar un plugin como JSON. |
| GET | `/api/plugins/export` | Exportar todos los plugins como JSON. |
| POST | `/api/plugins/import` | Importar plugins desde JSON. |

### Notas sobre playlists y plugins

- Si una playlist se solicita antes de que el plugin tenga cache, OpenAce intenta un refresco sincronico. Las peticiones concurrentes esperan hasta 60 s.
- Las fuentes remotas M3U solo aceptan HTTP/HTTPS, bloquean loopback/link-local, no siguen redirects y tienen limite de 50 MB.
- Los refrescos soportan `ETag`, `Last-Modified` y respuestas `304 Not Modified`.
- La importacion JSON no sobrescribe slugs existentes; devuelve estado `exists`.

## API del checker

| Metodo | Ruta | Descripcion |
|---|---|---|
| POST | `/check/single` | Comprobar un canal individual. |
| POST | `/check/start` | Iniciar comprobacion masiva (filtrable). |
| POST | `/check/stop` | Detener comprobacion masiva en curso. |
| GET | `/check/status` | Estado actual del runner (progreso, contadores). |
| GET | `/check/results` | Resultados con filtros por estado/plugin/grupo. |

## API de EULA

| Metodo | Ruta | Descripcion |
|---|---|---|
| POST | `/api/eula/accept` | Aceptar el EULA (requiere frase literal). |
| POST | `/api/eula/revoke` | Revocar consentimiento. Requiere rol `admin`. |
| GET | `/api/eula/status` | Consultar estado de aceptacion. |

## API de autenticacion

| Metodo | Ruta | Descripcion |
|---|---|---|
| POST | `/api/auth/login` | Iniciar sesion (devuelve cookie `openace_session`). |
| POST | `/api/auth/logout` | Cerrar sesion (invalida cookie). |
| GET | `/api/auth/me` | Consultar usuario autenticado actual. |

## API de administracion (requiere admin)

| Metodo | Ruta | Descripcion |
|---|---|---|
| GET | `/api/admin/users` | Listar todos los usuarios. |
| POST | `/api/admin/users` | Crear un usuario nuevo. |
| PUT | `/api/admin/users/<id>` | Actualizar un usuario. |
| DELETE | `/api/admin/users/<id>` | Eliminar un usuario. |
| GET | `/api/admin/tokens` | Listar todos los tokens API. |
| POST | `/api/admin/tokens` | Generar un token API para un usuario. |
| DELETE | `/api/admin/tokens/<id>` | Revocar un token API. |

## API del setup

| Metodo | Ruta | Descripcion |
|---|---|---|
| GET | `/api/setup/status` | Estado actual del asistente de configuracion. |
| POST | `/api/setup/eula` | Paso 1: aceptar EULA. |
| POST | `/api/setup/users` | Paso 2: crear usuarios. |
| POST | `/api/setup/plugins` | Paso 3: crear plugins. |
| POST | `/api/setup/finalize` | Paso 4: finalizar configuracion. Puede devolver `admin_token`; copialo porque solo se muestra una vez. |
| POST | `/api/setup/complete` | Setup completo en una sola llamada. Devuelve `admin_token`; copialo porque solo se muestra una vez. |
| POST | `/api/setup/reset` | Reiniciar asistente (requiere admin). |

## API del dashboard

| Metodo | Ruta | Descripcion |
|---|---|---|
| GET | `/api/peers/status` | Estado completo: motor, IP publica, peers, streams, plugins y conexiones. Usa `ipinfo.io` para IP/geolocalizacion con cache y limite de nuevas consultas por refresco. |

## Autenticacion en las APIs

Todas las APIs (excepto `/api/auth/login`, `/api/eula/*` y `/api/setup/*` durante el setup inicial) requieren autenticacion. Metodos soportados:

- **Cookie de sesion**: Login web normal (`POST /api/auth/login`)
- **Bearer token**: Cabecera `Authorization: Bearer <token>`
- **Token en URL**: Parametro `?token=<valor>`
- **HTTP Basic Auth**: Cabecera `Authorization: Basic <base64>`

Los tokens API se generan desde `/admin/users` o `POST /api/admin/tokens`. Usuarios y tokens pueden tener `expires_at`; si un usuario expira, sus sesiones, tokens y Basic Auth dejan de ser validos. Las sesiones cookie se renuevan automaticamente cuando queda menos del 25% de su vida.

## Proteccion CSRF por Origin

Las peticiones `POST`, `PUT`, `DELETE` y `PATCH` con cabecera `Origin` deben venir del mismo origen que OpenAce. Si el origen no coincide, la API responde 403. Detras de reverse proxy, configura correctamente `Host`, `X-Forwarded-Proto` y `REVERSE_PROXY=true`.

## Rutas web del setup

Ademas de `/api/setup/*`, existen rutas web `POST /setup/eula`, `/setup/users`, `/setup/plugins` y `/setup/summary` usadas por el asistente HTML.
