# Escenarios de despliegue selfhost

Esta guia resume que configuracion usar segun donde ejecutes OpenAce y donde termina el reverse proxy.

## Regla rapida

- Si accedes directo a `http://IP:8888`, usa `REVERSE_PROXY=false`.
- Si accedes mediante nginx, Caddy, Traefik, Nginx Proxy Manager o una VPS intermedia, usa `REVERSE_PROXY=true`.
- Si el servicio es accesible desde internet, no expongas `8888` directamente: publica `80/443` en el proxy y deja OpenAce en `127.0.0.1` o en una red privada/tunel.
- Publica OpenAce en la raiz de un dominio o subdominio, por ejemplo `https://openace.midominio.com/`. No se recomienda publicarlo bajo subruta como `/openace/`.

## Matriz de escenarios

| Escenario | Compose recomendado | Reverse proxy | Binding recomendado | `REVERSE_PROXY` | `PUBLIC_BASE_URL` |
|---|---|---|---|---|---|
| Casa directo/LAN | `docker-compose.simple.yaml` o `docker-compose.yaml` | No | `8888:8888` o IP LAN | `false` | vacio |
| Casa + proxy en VPS | Compose en casa + nginx/Caddy en VPS | En VPS | IP privada/tunel, no internet | `true` | `https://dominio` |
| Casa + proxy local | Compose en casa + nginx/Caddy local | Misma maquina | `127.0.0.1:8888:8888` | `true` | `https://dominio` o `http://host.lan` |
| VPS completo | `docker-compose.vps.simple.yaml` o `docker-compose.vps.yaml` | En la VPS | `127.0.0.1:8888:8888` | `true` | `https://dominio` |

## Variables comunes

```env
AUTH_ENABLED=true
SESSION_DURATION_HOURS=24
ACESTREAM_HOST=127.0.0.1
ACESTREAM_PORT=6878
IPFS_GATEWAY=http://kubo:48080
DB_PATH=/openace/checkdb/data.db
```

`PUBLIC_BASE_URL` es opcional, pero recomendable con proxies remotos o tuneles. OpenAce lo usa para generar las URLs absolutas de las playlists M3U. Si lo dejas vacio, se usa la URL de la propia peticion HTTP, teniendo en cuenta `ProxyFix` cuando `REVERSE_PROXY=true`.

`FORWARDED_ALLOW_IPS` limita desde que IPs Gunicorn acepta cabeceras `X-Forwarded-*`. En produccion evita `*`: usa `127.0.0.1` si el proxy esta en la misma maquina, o la IP privada/tunel del proxy si esta en otra maquina.

## Checklist reverse proxy

- Activa `REVERSE_PROXY=true` solo cuando OpenAce este detras de un proxy controlado.
- Usa `PUBLIC_BASE_URL=https://tu-dominio` si el proxy o tunel no preserva correctamente `Host` y `X-Forwarded-Proto`.
- Envia `Host`, `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` y `X-Forwarded-Port`.
- La validacion CSRF por `Origin` en metodos mutadores depende de que `Host` y protocolo sean correctos.
- Para `/play/`, desactiva buffering y cache, y usa timeouts largos.
- No publiques OpenAce bajo subruta (`/openace/`); usa raiz de dominio o subdominio.

---

## 1. Selfhost en casa con acceso directo

Arquitectura:

```text
Cliente LAN -> http://IP-LAN:8888 -> OpenAce
```

Uso recomendado:

```bash
# Sin VPN
docker compose -f docker-compose.simple.yaml up -d

# Con VPN/Gluetun
docker compose -f docker-compose.yaml up -d
```

Variables:

```env
REVERSE_PROXY=false
PUBLIC_BASE_URL=
```

Notas:

- Pensado para uso en LAN domestica.
- Accede con `http://IP-DE-LA-MAQUINA:8888`.
- No abras el puerto `8888` en el router salvo que sepas exactamente lo que haces.
- Para acceso externo, usa una VPN de acceso remoto, Tailscale, o uno de los escenarios con reverse proxy.

---

## 2. Selfhost en casa con reverse proxy en una VPS

Arquitectura:

```text
Cliente
  |
  v
https://openace.midominio.com
  |
  v
VPS con nginx/Caddy
  |
  v
Tunel privado WireGuard / Tailscale / ZeroTier / SSH
  |
  v
OpenAce en casa:8888
```

Este escenario necesita una ruta privada real entre la VPS y la maquina de casa. La VPS no puede proxyear a tu red domestica sin WireGuard, Tailscale, ZeroTier, un tunel SSH persistente u otra solucion equivalente.

En la maquina de casa:

```env
REVERSE_PROXY=true
PUBLIC_BASE_URL=https://openace.midominio.com
FORWARDED_ALLOW_IPS=<IP_PRIVADA_DEL_PROXY_O_TUNEL>
```

Binding recomendado en casa, segun el tunel:

```yaml
ports:
  - "100.64.x.y:8888:8888"   # IP Tailscale/WireGuard de la maquina de casa
```

O si el tunel termina localmente y reenvia a OpenAce:

```yaml
ports:
  - "127.0.0.1:8888:8888"
```

En la VPS, el upstream de nginx debe apuntar a la IP privada o puerto local del tunel:

```nginx
set $openace 100.64.x.y;
proxy_pass http://$openace:8888;
```

Con tunel SSH local en la VPS:

```nginx
set $openace 127.0.0.1;
proxy_pass http://$openace:8888;
```

Recomendaciones:

- No abras `8888` directamente desde internet hacia tu casa.
- Mantén `AUTH_ENABLED=true`.
- Usa HTTPS en la VPS.
- Asegura que el proxy envie `Host`, `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` y `X-Forwarded-Port`.
- Conserva `proxy_buffering off` y timeouts largos para `/play/`.

---

## 3. Selfhost en casa con reverse proxy en la misma maquina

Arquitectura:

```text
Cliente -> nginx/Caddy local :80/:443 -> OpenAce 127.0.0.1:8888
```

Variables:

```env
REVERSE_PROXY=true
PUBLIC_BASE_URL=https://openace.midominio.com
FORWARDED_ALLOW_IPS=127.0.0.1
```

Binding recomendado:

```yaml
ports:
  - "127.0.0.1:8888:8888"
```

Ejemplo Caddy:

```caddyfile
openace.midominio.com {
    reverse_proxy 127.0.0.1:8888
}
```

Ejemplo nginx minimo:

```nginx
server {
    listen 443 ssl http2;
    server_name openace.midominio.com;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port $server_port;
    }

    location /play/ {
        proxy_pass http://127.0.0.1:8888;
        proxy_buffering off;
        proxy_cache off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Si el proxy local es solo HTTP, usa `PUBLIC_BASE_URL=http://host.lan`. Para acceso remoto se recomienda HTTPS.

---

## 4. Selfhost completo en una VPS

Arquitectura:

```text
Cliente -> https://openace.midominio.com -> nginx en VPS -> OpenAce 127.0.0.1:8888
```

Uso recomendado:

```bash
# VPS sin VPN
docker compose -f docker-compose.vps.simple.yaml up -d

# VPS con VPN/Gluetun
docker compose -f docker-compose.vps.yaml up -d
```

Variables:

```env
REVERSE_PROXY=true
PUBLIC_BASE_URL=https://openace.midominio.com
FORWARDED_ALLOW_IPS=127.0.0.1
```

Los compose VPS vinculan `8888` a `127.0.0.1`. En la variante con VPN, el puerto `8888` lo publica el servicio `acestream-vpn`, porque `open-ace` usa `network_mode: service:acestream-vpn`.

Firewall recomendado:

- Abrir: `22/tcp`, `80/tcp`, `443/tcp`.
- Bloquear al exterior: `8888/tcp`, `5001/tcp`, `6878/tcp`, `8001/tcp`.
- `4001/tcp+udp` de Kubo es opcional/avanzado: mantenlo abierto solo si quieres participar en el swarm IPFS desde la VPS.

Consulta tambien [Despliegue en VPS](03-despliegue-vps.md) y [Securizacion de la VPS](07-securizacion-vps.md).

---

## Validacion

### Comprobar que OpenAce responde

```bash
curl -I http://127.0.0.1:8888/
```

Debe devolver `200` si ya esta configurado, o `302` hacia `/setup`, `/eula` o `/login`.

### Comprobar reverse proxy

```bash
curl -I https://openace.midominio.com/
```

Debe devolver `200` o `302`, no `502`.

### Comprobar playlists

Con un plugin y token reales:

```bash
curl -s "https://openace.midominio.com/<plugin>/mpegts.m3u?token=<token>" | grep "^https://openace.midominio.com/play/mpegts/"
curl -s "https://openace.midominio.com/<plugin>/hls.m3u?token=<token>" | grep "^https://openace.midominio.com/play/hls/"
```

Si aparecen URLs internas como `http://127.0.0.1:8888`, revisa:

1. `PUBLIC_BASE_URL`.
2. `REVERSE_PROXY=true`.
3. Cabeceras `Host` y `X-Forwarded-Proto` en el proxy.

### Comprobar que `8888` no esta publico con proxy

```bash
ss -ltnp | grep 8888
```

En escenarios con proxy, debe aparecer `127.0.0.1:8888`, no `0.0.0.0:8888`.
