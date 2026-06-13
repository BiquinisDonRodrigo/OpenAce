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
- Hace proxy de streams MPEG-TS desde el motor AceStream
- Transcodifica a HLS bajo demanda usando FFmpeg
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
| `IPFS_GATEWAY` | URL del gateway IPFS local | `http://kubo:48080` |
| `DB_PATH` | Ruta de la base de datos SQLite | `/openace/checkdb/data.db` |
| `AUTH_ENABLED` | Activar/desactivar autenticacion | `true` |
| `SESSION_DURATION_HOURS` | Duracion de la sesion en horas | `24` |
| `OPENACE_ADMIN_USER` | Nombre del usuario admin | `admin` |
| `OPENACE_ADMIN_PASSWORD` | Contrasena del admin (auto-setup) | — |
| `OPENACE_AUTO_SETUP` | Activar auto-setup al arrancar | `false` |
| `OPENACE_EULA_ACCEPT` | Aceptar EULA automaticamente | `false` |
| `REVERSE_PROXY` | Activar soporte para reverse proxy | `false` |

### Variables VPN (solo con Gluetun)

| Variable | Descripcion |
|---|---|
| `WG_PRIVATE_KEY` | Clave privada WireGuard |
| `ProtonCountries` | Paises para la conexion VPN |

## Paso 3: Elegir modo de despliegue

### Opcion A: Sin VPN

```bash
docker compose -f docker-compose.simple.yaml up -d
```

Servicios que se levantan:
- `kubo` — Nodo IPFS
- `open-ace` — OpenAce (AceStream + proxy)

### Opcion B: Con VPN

```bash
docker compose up -d
```

Servicios que se levantan:
- `acestream-vpn` — Gluetun (VPN WireGuard)
- `kubo` — Nodo IPFS
- `open-ace` — OpenAce (enruta trafico a traves de la VPN)

Con VPN, el contenedor `open-ace` usa `network_mode: service:acestream-vpn`, lo que significa que todo su trafico de red pasa por el tunel VPN.

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

Accede a `http://<tu-ip>:8888` en el navegador. Se mostrara el asistente de configuracion inicial (ver [Configuracion inicial](04-configuracion-inicial.md)).

## Volumenes y persistencia

| Volumen | Contenido |
|---|---|
| `./data` | Base de datos SQLite (plugins, checks, EULA, usuarios) |
| `./logs` | Logs de AceStream y del proxy |
| `./ipfs/data` | Datos del nodo IPFS |
| `./ipfs/export` | Exportaciones IPFS |
| `./gluetun-data` | Datos de Gluetun (puerto reenviado) |

## Puertos expuestos

| Puerto | Servicio | Descripcion |
|---|---|---|
| `8888` | OpenAce | Interfaz web y API |
| `4001/tcp+udp` | Kubo | Protocolo IPFS (swarm) |
| `5001` | Kubo | API IPFS (solo localhost) |
| `8001` | Gluetun | Panel de control VPN (solo con VPN) |

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

- [Configuracion inicial](04-configuracion-inicial.md)
- [Despliegue en VPS](03-despliegue-vps.md) para SSL y acceso desde internet
- [Securizacion de la VPS](07-securizacion-vps.md) para proteger el servidor
- [Modulos](05-modulos.md) para entender cada seccion de la interfaz
