# Despliegue rapido

Guia rapida para tener OpenAce funcionando en menos de 5 minutos.

## Requisitos previos

- Docker Engine 24+ y Docker Compose v2
- Puerto 8888 disponible en la maquina o LAN
- Puerto 4001 disponible (IPFS)

Esta guia rapida esta pensada para acceso local/LAN. Si vas a publicar OpenAce en internet, consulta primero [Escenarios selfhost](11-escenarios-selfhost.md) y usa reverse proxy; no expongas `8888` directamente.

## Sin VPN

```bash
git clone https://github.com/BiquinisDonRodrigo/OpenAce.git
cd OpenAce
docker compose -f docker-compose.simple.yaml up -d
```

Accede a `http://localhost:8888` desde la misma maquina o a `http://IP-LAN:8888` desde tu red local. Se mostrara el asistente de configuracion inicial.

## Con VPN (WireGuard / ProtonVPN)

```bash
git clone https://github.com/BiquinisDonRodrigo/OpenAce.git
cd OpenAce
cp env-example .env
```

Edita `.env` y pon tu clave privada WireGuard:

```env
WG_PRIVATE_KEY=tu_clave_privada_wireguard
ProtonCountries=switzerland,spain
```

Arranca los servicios:

```bash
docker compose up -d
```

Accede a `http://localhost:8888` desde la misma maquina o a `http://IP-LAN:8888` desde tu red local.

## Auto-setup (sin asistente web)

Si quieres saltar el asistente de configuracion y que todo se configure automaticamente, define estas variables de entorno en tu `docker-compose`:

```yaml
environment:
  OPENACE_AUTO_SETUP: "true"
  OPENACE_ADMIN_USER: "admin"
  OPENACE_ADMIN_PASSWORD: "tu_password_segura"
  OPENACE_EULA_ACCEPT: "true"
```

Al arrancar, OpenAce creara el usuario admin, aceptara la EULA y quedara listo para usar.

## Verificar que funciona

```bash
# Health check
curl http://localhost:8888/

# Respuesta esperada:
# OpenAce is running
```

Accede al panel en `http://IP-LAN:8888/panel` para ver el estado del motor AceStream, conexiones y plugins.

## Detener los servicios

```bash
# Sin VPN
docker compose -f docker-compose.simple.yaml down

# Con VPN
docker compose down
```

## Siguientes pasos

- [Escenarios selfhost](11-escenarios-selfhost.md) para elegir entre casa, proxy local, proxy en VPS o VPS completo
- [Despliegue paso a paso](02-despliegue-paso-a-paso.md) para entender cada componente
- [Configuracion inicial](04-configuracion-inicial.md) para el asistente de setup
- [Despliegue en VPS](03-despliegue-vps.md) para exponer el servicio a internet con SSL
- [Securizacion de la VPS](07-securizacion-vps.md) para proteger el servidor
- [Solucion de problemas](10-solucion-de-problemas.md) si algo no funciona
