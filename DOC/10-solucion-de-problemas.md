# Solucion de problemas

Errores comunes y como resolverlos.

## Despliegue y arranque

### La playlist responde 503 "Playlist not ready, retry in a moment."

El plugin todavia no ha completado su primer fetch. Revisa los logs por eventos `plugin_fetched` o `plugin_fetch_failed`. Espera unos segundos y reintenta.

```bash
docker logs open-ace | grep plugin_fetch
```

### HLS responde 503 "Stream buffering, retry"

FFmpeg arranco pero todavia no ha producido el primer segmento (timeout de 30 s). El primer arranque para un infohash frio puede ser lento. Reintenta.

### HLS responde 503 "Stream stale, retry"

El manifiesto o el segmento HLS mas reciente quedo obsoleto durante mas de 30 s (`OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S`). OpenAce descarta ese proceso FFmpeg y fuerza un nuevo arranque. Reintenta la peticion o cambia de canal y vuelve.

### La playlist tarda o responde 503

Si el plugin no tiene cache, OpenAce intenta refrescarlo de forma sincronica. Las peticiones concurrentes pueden esperar hasta 60 s. Revisa que la URL M3U no supere 50 MB, no redirija y no apunte a loopback/link-local.

### Streaming MPEG-TS funciona pero HLS no

Verifica que FFmpeg este disponible y que el contenedor pueda escribir en `/tmp/openace/`:

```bash
docker exec open-ace ffmpeg -version
docker exec open-ace ls -la /tmp/openace/
```

### Gluetun no expone el puerto forwarded

`start.sh` espera hasta 20 s a que aparezca `/tmp/gluetun/forwarded_port`. Si no lo encuentra, cae a `ACESTREAM_PORT` (6878). Causas posibles:
- VPN sin port forwarding habilitado
- Servidor en pais sin soporte
- Clave WireGuard invalida

### Los plugins con URL IPFS/IPNS no cargan canales

Verifica que el contenedor Kubo este sano y que el gateway escuche en el puerto 48080:

```bash
docker exec kubo ipfs id
```

Comprueba que la variable `IPFS_GATEWAY` apunte a `http://kubo:48080` en el docker-compose.

## Autenticacion

### El reproductor IPTV da error 401

El reproductor no esta autenticado. Genera un token API desde `/admin/users` y anadelo a la URL de la playlist:

```
http://<host>:8888/<plugin>/mpegts.m3u?token=<tu-token>
```

Si el token expiro o fue revocado, genera uno nuevo.

### El reproductor pide autenticacion

Asegurate de incluir el token en la URL: `?token=<tu-token>`. La mayoria de reproductores IPTV no soportan login con cookie; usa un token API o HTTP Basic Auth.

### Error 401 / No autenticado

- El token ha expirado o ha sido revocado
- Genera un nuevo token desde `/admin/users`
- Verifica que el token pertenece a un usuario activo y no expirado

### Error 403 en POST/PUT/DELETE/PATCH detras del proxy

OpenAce valida la cabecera `Origin` en metodos mutadores. Si el proxy cambia `Host` o protocolo, puede parecer un origen cruzado. Verifica `REVERSE_PROXY=true`, `PUBLIC_BASE_URL` si aplica, y que el proxy envie `Host` y `X-Forwarded-Proto` correctamente.

### Olvide la contrasena del admin

Elimina el fichero de base de datos (`./data/data.db`) y reinicia los contenedores. El asistente de configuracion aparecera de nuevo. Alternativamente, usa `OPENACE_AUTO_SETUP=true` con las variables de entorno para reconfigurarlo.

## EULA

### El EULA no me deja pasar

Debes escribir la frase exacta: **He leido y acepto el acuerdo**. Respeta mayusculas, tildes y espacios. Si previamente revocaste el consentimiento, acepta de nuevo.

## Channel Checker

### El channel checker se queda "busy"

Solo puede haber una comprobacion masiva en curso. Si el runner esta activo, espera a que termine o pulsa **Parar**. La comprobacion manual tambien serializa contra el runner para no sobrecargar el engine.

### El canal aparece como "caido" en el checker

Puede ser temporal: los canales AceStream dependen de la disponibilidad de seeders en la red P2P. Recomprueba mas tarde o prueba reproducirlo directamente (a veces el checker tiene timeout pero el canal funciona).

## Reverse proxy y acceso publico

### Las playlists salen con `http://127.0.0.1:8888` o con `http` en vez de `https`

Causas habituales:

- Falta `PUBLIC_BASE_URL=https://tu-dominio` en `.env`.
- Falta `REVERSE_PROXY=true`.
- El proxy no envia `Host` o `X-Forwarded-Proto` correctamente.

Verifica la URL generada:

```bash
curl -s "https://tu-dominio/<plugin>/mpegts.m3u?token=<tu-token>" | grep "^http"
```

### El login funciona directo pero falla detras del proxy

Con `REVERSE_PROXY=true`, la cookie de sesion se marca como `Secure`. Asegurate de acceder por HTTPS real y de que nginx/Caddy envie `X-Forwarded-Proto: https`. No actives `REVERSE_PROXY=true` para acceso directo por HTTP en LAN.

### El streaming se corta detras de nginx

Revisa que el bloque `/play/` del proxy conserve:

```nginx
proxy_buffering off;
proxy_cache off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

### El puerto `8888` es visible desde internet

No publiques Gunicorn directamente. Usa los compose VPS o vincula el puerto a loopback:

```yaml
ports:
  - "127.0.0.1:8888:8888"
```

Comprueba la escucha local:

```bash
ss -ltnp | grep 8888
```

### Casa + reverse proxy en VPS devuelve 502

Desde la VPS, comprueba que el tunel llega a la maquina de casa:

```bash
curl -I http://IP_TUNEL_CASA:8888/
```

Si falla, revisa WireGuard/Tailscale/ZeroTier/SSH, firewall local y que nginx apunte a la IP privada o puerto local correcto.

## Streaming

### El stream se corta frecuentemente

- Verifica la conexion de red del servidor
- Si usas VPN, prueba diferentes servidores/paises
- Comprueba el numero de peers en `/peers` — pocos peers indican un canal con poca disponibilidad
- Usa MPEG-TS en lugar de HLS para menor latencia

### El motor AceStream aparece como offline en el dashboard

- Verifica que el proceso `start-engine` este corriendo dentro del contenedor: `docker exec open-ace ps aux | grep acestream`
- Revisa los logs del contenedor: `docker logs -f open-ace`
- Si usas VPN, verifica que Gluetun este conectado: `docker logs acestream-vpn`

## Diagnostico general

### Ver logs

```bash
# Logs del proxy y del motor AceStream (JSON/stdout/stderr)
docker logs -f open-ace
```

### Verificar estado de los servicios

```bash
# Estado de los contenedores
docker compose ps

# Health check
curl http://localhost:8888/

# Estado completo via API
curl http://localhost:8888/api/peers/status
```
