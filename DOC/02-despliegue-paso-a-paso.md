# Despliegue paso a paso

Explicacion detallada de cada componente y como se despliega OpenAce.

## Arquitectura del stack

OpenAce ejecuta todo dentro de un unico contenedor Docker:

```
+-----------------------------------------------------------+
|  Contenedor open-ace                                      |
|                                                           |
|  +---------------------+  +--------------------------+    |
|  | AceStream Engine    |  | Gunicorn + gevent        |    |
|  | (puerto 6878)       |  | (puerto 8888)            |    |
|  | Motor P2P           |  | Flask app (proxy HTTP)   |    |
|  +---------------------+  +--------------------------+    |
|                                                           |
|  +---------------------+                                  |
|  | FFmpeg              |                                  |
|  | (HLS bajo demanda)  |                                  |
|  +---------------------+                                  |
+-----------------------------------------------------------+
         |                           |
  +------+------+            +-------+-------+
  | Kubo (IPFS) |            | Gluetun (VPN) |
  | (opcional)  |            | (opcional)     |
  +-----------  +            +---------------+
```

## Componentes

### 1. AceStream Engine

Motor P2P que gestiona las conexiones a la red AceStream. Se ejecuta internamente en el contenedor en el puerto 6878. OpenAce se comunica con el a traves de su API HTTP interna.

### 2. Proxy HTTP (Flask + Gunicorn)

La aplicacion Python que:
- Genera listas M3U dinamicas a partir de plugins configurados
- Sirve streams MPEG-TS generados por FFmpeg desde el motor AceStream
- Genera HLS bajo demanda usando FFmpeg
- Proporciona un dashboard de monitorizacion en tiempo real
- Gestiona usuarios, autenticacion y EULA

Gunicorn usa el worker `gevent` para manejar conexiones de larga duracion (streams) con un timeout de 3600 segundos (1 hora).

### 3. Kubo (IPFS)

Nodo IPFS local que permite resolver URLs `ipfs://` e `ipns://` en las fuentes M3U de los plugins. Se ejecuta como servicio separado en Docker Compose y se comunica con OpenAce a traves del gateway HTTP en el puerto 48080.

### 4. Gluetun (VPN, opcional)

Contenedor VPN basado en WireGuard. Cuando se usa, OpenAce enruta todo su trafico de red a traves de la VPN. Esto es especialmente util para:
- Ocultar tu IP real en la red P2P de AceStream
- Port forwarding para mejorar la conectividad P2P

## Paso 1: Clonar el repositorio

```bash
git clone https://github.com/BiquinisDonRodrigo/OpenAce.git
cd OpenAce
```

## Paso 2: Configurar variables de entorno

Copia el fichero de ejemplo:

```bash
cp env-example .env
```

### Variables disponibles

| Variable | Descripcion | Valor por defecto |
|---|---|---|
| `TZ` | Zona horaria del contenedor | `Europe/Madrid` |
| `ACESTREAM_HOST` | Host del motor AceStream | `127.0.0.1` |
| `ACESTREAM_PORT` | Puerto del motor AceStream | `6878` |
| `ACESTREAM_IP` | IP publica opcional anunciada por AceStream | — |
| `IPFS_GATEWAY` | URL del gateway IPFS local | `http://kubo:48080` |
| `DB_PATH` | Ruta de la base de datos SQLite | `/openace/checkdb/data.db` |
| `AUTH_ENABLED` | Activar/desactivar autenticacion | `true` |
| `SESSION_DURATION_HOURS` | Duracion de la sesion en horas | `24` |
| `OPENACE_ADMIN_USER` | Nombre del usuario admin | `admin` |
| `OPENACE_ADMIN_PASSWORD` | Contrasena del admin (auto-setup) | — |
| `OPENACE_AUTO_SETUP` | Activar auto-setup al arrancar | `false` |
| `OPENACE_EULA_ACCEPT` | Aceptar EULA automaticamente | `false` |
| `REVERSE_PROXY` | Activar soporte para reverse proxy | `false` |
| `FORWARDED_ALLOW_IPS` | IPs de proxy permitidas por Gunicorn en modo reverse proxy | `127.0.0.1` |
| `PUBLIC_BASE_URL` | URL publica opcional para generar playlists M3U absolutas | — |
| `OPENACE_FFMPEG_ENABLED` | Activar FFmpeg para MPEG-TS/HLS; si esta en `false`, MPEG-TS mantiene `/play/mpegts/...` y se proxya directo desde AceStream, HLS devuelve 503 | `false` |
| `OPENACE_IDLE_TIMEOUT_S` | Segundos de inactividad antes de cerrar streams FFmpeg | `180` |
| `OPENACE_CHUNK_SIZE` | Tamano de lectura del pipe FFmpeg en bytes | `65536` |
| `OPENACE_QUEUE_MAX` | Tamano maximo de cola por cliente MPEG-TS | `256` |
| `OPENACE_PIPE_BUFFER_SIZE` | Tamano del buffer del pipe del OS en bytes | `1048576` |
| `OPENACE_MAX_STREAMS` | Maximo de streams FFmpeg simultaneos | `32` |
| `OPENACE_ITERATE_TIMEOUT_S` | Timeout de iteracion del stream FFmpeg en segundos | `180` |
| `OPENACE_FFMPEG_RW_TIMEOUT_US` | Timeout de lectura/escritura FFmpeg en microsegundos | `120000000` |
| `OPENACE_FFMPEG_RESTARTS` | Reintentos maximos de FFmpeg por stream | `3` |
| `OPENACE_FFMPEG_RESTART_BACKOFF_S` | Espera entre reintentos de FFmpeg en segundos | `2` |
| `OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S` | Edad maxima de segmentos HLS obsoletos antes de limpieza | `30` |
| `GUNICORN_WORKERS` | Numero de workers Gunicorn | `2` |
| `GUNICORN_WORKER_CONNECTIONS` | Conexiones gevent por worker | `2000` |

### Variables VPN (solo con Gluetun)

| Variable | Descripcion |
|---|---|
| `WG_PRIVATE_KEY` | Clave privada WireGuard |
| `ProtonCountries` | Paises para la conexion VPN |

## Paso 3: Elegir modo de despliegue

Si dudas entre casa/LAN, proxy local, proxy en VPS o VPS completo, consulta [Escenarios selfhost](11-escenarios-selfhost.md). Como regla general: los compose `docker-compose.simple.yaml` y `docker-compose.yaml` son para acceso directo en LAN; los compose `docker-compose.vps.simple.yaml` y `docker-compose.vps.yaml` son para publicar con nginx/HTTPS y `8888` solo en loopback.

### Clasificacion de compose

| Fichero | Uso recomendado | Exposicion |
|---|---|---|
| `docker-compose.simple.yaml` | Produccion selfhost en casa/LAN sin VPN | Publica `:8888` para acceso directo en la LAN |
| `docker-compose.yaml` | Produccion selfhost en casa/LAN con Gluetun | Publica `:8888` para acceso directo en la LAN |
| `docker-compose.vps.simple.yaml` | Produccion VPS sin VPN | `:8888` solo en `127.0.0.1`; publicar con nginx/HTTPS |
| `docker-compose.vps.yaml` | Produccion VPS con Gluetun | `:8888` y panel Gluetun solo en `127.0.0.1`; publicar con nginx/HTTPS |
| `docker-compose.dev.simple.yaml` | Desarrollo sin VPN | Build local, hot-reload, Flask debug |
| `docker-compose.dev.yaml` | Desarrollo con Gluetun | Build local, hot-reload, Flask debug |

### Opcion A: Sin VPN selfhost casa/LAN

```bash
docker compose -f docker-compose.simple.yaml up -d
```

Servicios que se levantan:
- `kubo` — Nodo IPFS
- `open-ace` — OpenAce (AceStream + proxy)

### Opcion B: Con VPN selfhost casa/LAN

```bash
docker compose up -d
```

Servicios que se levantan:
- `acestream-vpn` — Gluetun (VPN WireGuard)
- `kubo` — Nodo IPFS
- `open-ace` — OpenAce (enruta trafico a traves de la VPN)

Con VPN, el contenedor `open-ace` usa `network_mode: service:acestream-vpn`, lo que significa que todo su trafico de red pasa por el tunel VPN.

### Opcion C: VPS con nginx/HTTPS

Para exponer OpenAce a internet en una VPS no uses los compose de LAN. Usa uno de estos:

```bash
# VPS sin VPN
docker compose -f docker-compose.vps.simple.yaml up -d

# VPS con VPN/Gluetun
docker compose -f docker-compose.vps.yaml up -d
```

Estos compose activan `REVERSE_PROXY=true` y vinculan `8888` a `127.0.0.1` para que el trafico externo pase por nginx. Continua con [Despliegue en VPS](03-despliegue-vps.md) para configurar dominio, TLS y firewall.

## Paso 4: Verificar el despliegue

```bash
# Ver estado de los contenedores
docker compose ps

# Ver logs en tiempo real
docker compose logs -f open-ace

# Health check
curl http://localhost:8888/
```

## Paso 5: Configuracion inicial

En despliegues LAN/directos, accede a `http://IP-LAN:8888` en el navegador. En despliegues con reverse proxy, accede a la URL publica HTTPS configurada en nginx. Se mostrara el asistente de configuracion inicial (ver [Configuracion inicial](04-configuracion-inicial.md)).

## Volumenes y persistencia

| Volumen | Contenido |
|---|---|
| `./data` | Base de datos SQLite (plugins, checks, EULA, usuarios) |
| `./ipfs/data` | Datos del nodo IPFS |
| `./ipfs/export` | Exportaciones IPFS |
| `./gluetun-data` | Datos de Gluetun (puerto reenviado) |

## Puertos expuestos

| Puerto | Servicio | Descripcion |
|---|---|---|
| `8888` | OpenAce | Interfaz web y API; en compose VPS solo escucha en `127.0.0.1` |
| `4001/tcp+udp` | Kubo | Protocolo IPFS (swarm) |
| `5001` | Kubo | API IPFS (solo localhost) |
| `8001` | Gluetun | Panel de control VPN (solo con VPN; en compose LAN/dev puede quedar expuesto en todas las interfaces) |
| `6878` | AceStream | API del engine; solo se expone en `docker-compose.dev.simple.yaml` para desarrollo |

## Desarrollo con hot-reload

Para desarrollo, monta el codigo fuente como volumen para que los cambios se apliquen automaticamente:

```bash
# Sin VPN
docker compose -f docker-compose.dev.simple.yaml up --build

# Con VPN
docker compose -f docker-compose.dev.yaml up --build
```

Flask se ejecuta con `--debug --reload`, los directorios `app/` y `server.py` se montan como volumenes.

## Siguientes pasos

- [Escenarios selfhost](11-escenarios-selfhost.md) para elegir la topologia correcta
- [Configuracion inicial](04-configuracion-inicial.md) para el asistente de setup
- [Despliegue en VPS](03-despliegue-vps.md) para SSL y acceso desde internet
- [Securizacion de la VPS](07-securizacion-vps.md) para proteger el servidor
- [Modulos](05-modulos.md) para entender cada seccion de la interfaz
- [Solucion de problemas](10-solucion-de-problemas.md) si algo no funciona
