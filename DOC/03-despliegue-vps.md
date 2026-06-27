# Despliegue en VPS

Guia para exponer OpenAce a internet con nginx como reverse proxy y SSL con Let's Encrypt.

Si no tienes claro si necesitas VPS completa, OpenAce en casa con proxy en VPS, o proxy en la misma maquina de casa, empieza por [Escenarios selfhost](11-escenarios-selfhost.md).

## Proveedores VPS no recomendados

| Proveedor | Problema | Detalle |
|---|---|---|
| **OVH** | Motor AceStream inestable | Los streams se cortan a los pocos segundos. El motor de AceStream no logra mantener suficientes peers para sostener la reproduccion, tanto con VPN como sin ella. Se ha probado con distintas configuraciones y el problema persiste. No es un fallo de OpenAce sino una limitacion de la red/infraestructura de OVH con trafico P2P. |

Si tu proveedor no aparece en esta lista, deberia funcionar sin problemas. Proveedores como Hetzner, Contabo o Netcup son opciones probadas.

## Requisitos

- VPS con acceso root (Ubuntu 22.04+ / Debian 12+)
- Dominio apuntando a la IP de la VPS (registro A y/o AAAA)
- Puertos 80 y 443 abiertos en el firewall
- Docker y Docker Compose instalados
- OpenAce desplegado con `docker-compose.vps.simple.yaml` o `docker-compose.vps.yaml`

## Paso 1: Instalar nginx

```bash
sudo apt update
sudo apt install -y nginx
```

## Paso 2: Copiar la configuracion de nginx

```bash
sudo cp nginx/openace.conf /etc/nginx/sites-available/openace.conf
```

Reemplaza `_DOMAIN_` por tu dominio real:

```bash
sudo sed -i 's/_DOMAIN_/tu-dominio.com/g' /etc/nginx/sites-available/openace.conf
```

Activa el sitio y desactiva el default:

```bash
sudo ln -sf /etc/nginx/sites-available/openace.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
```

## Paso 3: Obtener certificado SSL con Let's Encrypt

Instala certbot:

```bash
sudo apt install -y certbot python3-certbot-nginx
```

Antes de solicitar el certificado, comenta temporalmente el bloque `server` de HTTPS en la configuracion de nginx (el de puerto 443), ya que aun no existen los certificados. Deja solo el bloque de puerto 80.

Arranca nginx y solicita el certificado:

```bash
sudo nginx -t
sudo systemctl start nginx

sudo certbot --nginx -d tu-dominio.com
```

Certbot modificara la configuracion automaticamente para usar el certificado. Si prefieres usar la configuracion manual del fichero `openace.conf`, usa `certonly` en su lugar:

```bash
sudo certbot certonly --webroot --webroot-path /var/www/certbot -d tu-dominio.com
```

## Paso 4: Levantar OpenAce en modo VPS

Usa el compose VPS que corresponda:

```bash
# Sin VPN
docker compose -f docker-compose.vps.simple.yaml up -d

# Con VPN/Gluetun
docker compose -f docker-compose.vps.yaml up -d
```

Estos compose ya activan `REVERSE_PROXY=true` y vinculan los puertos internos sensibles a `127.0.0.1`. Si nginx no corre en la misma maquina o necesitas limitar otra IP de proxy, ajusta `FORWARDED_ALLOW_IPS` en `.env`.

`REVERSE_PROXY=true` activa dos cosas:
- **ProxyFix** en Flask: `request.remote_addr` refleja la IP real del cliente, `request.is_secure` detecta HTTPS
- **--forwarded-allow-ips** en gunicorn: confiar en las cabeceras de proxy reenviadas por nginx

Las peticiones mutadoras (`POST`, `PUT`, `DELETE`, `PATCH`) validan la cabecera `Origin`; si el proxy no preserva `Host`/protocolo, pueden responder 403.

## Paso 5: Iniciar nginx

```bash
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
```

Accede a `https://tu-dominio.com`.

## Paso 6: Renovacion automatica del certificado

Certbot instala un timer de systemd que renueva automaticamente. Verifica:

```bash
sudo systemctl status certbot.timer
```

Para probar la renovacion:

```bash
sudo certbot renew --dry-run
```

## Configuracion de nginx incluida

El fichero `nginx/openace.conf` incluye:

### Seguridad

- **TLS 1.2 y 1.3** con cifrados modernos (ECDHE + AES-GCM / CHACHA20)
- **HSTS** con max-age de 2 anos, incluye subdominios y preload
- **OCSP Stapling** para verificacion rapida de certificados
- **X-Frame-Options: DENY** para prevenir clickjacking
- **X-Content-Type-Options: nosniff** para prevenir MIME sniffing
- **Referrer-Policy** y **Permissions-Policy** restrictivos
- **server_tokens off** para ocultar la version de nginx

### Rate limiting

| Ruta | Limite | Proposito |
|---|---|---|
| `/api/auth/login` | 5 peticiones/segundo, burst 3 | Proteccion contra fuerza bruta |
| `/api/setup/` | 3 peticiones/segundo, burst 5 | Proteccion del asistente |

### Streaming

La ruta `/play/` (MPEG-TS y HLS) tiene configuracion especial:
- `proxy_buffering off` — Sin buffering para streaming en tiempo real
- `proxy_cache off` — Sin cache
- `proxy_request_buffering off` — Sin buffering de peticiones
- Timeouts de 3600 segundos (1 hora) para streams de larga duracion

OpenAce tambien envia cabeceras anti-cache/anti-buffer en las respuestas de streaming (`Cache-Control: no-store`, `X-Accel-Buffering: no`).

### Soporte IPv4 e IPv6

Nginx escucha en ambos protocolos:
- `listen 80` + `listen [::]:80`
- `listen 443 ssl http2` + `listen [::]:443 ssl http2`

## Con VPN

Si usas el despliegue VPS con VPN (`docker-compose.vps.yaml`), el contenedor `open-ace` no tiene su propia IP en la red Docker. En la configuracion incluida, nginx instalado en la misma VPS debe seguir apuntando a `127.0.0.1` porque Gluetun publica `127.0.0.1:8888`. Si instalas nginx dentro de Docker o en otra maquina de la red Docker, edita `openace.conf` y cambia la variable `$openace` de:

```nginx
set $openace 127.0.0.1;
```

a la IP o nombre del contenedor VPN accesible desde esa red Docker:

```nginx
set $openace acestream-vpn;
```

Esto solo funciona si nginx tambien esta dentro de Docker o tiene resolucion/ruta hacia esa red. Con nginx instalado en el host, deja `127.0.0.1` y publica `8888` en loopback como hacen los compose VPS.

## Firewall y securizacion

Consulta la guia completa en [Securizacion de la VPS](07-securizacion-vps.md). Como minimo, configura iptables y vincula los puertos Docker a localhost para que el trafico pase siempre por nginx.

## Verificar el despliegue

```bash
# Test SSL
curl -I https://tu-dominio.com

# Debe devolver cabeceras como:
# strict-transport-security: max-age=63072000; includeSubDomains; preload
# x-content-type-options: nosniff
# x-frame-options: DENY

# Test streaming
curl -s -o /dev/null -w "%{http_code}" https://tu-dominio.com/
# Debe devolver 200 (o 302 si no has completado el setup)
```

## Siguientes pasos

- [Escenarios selfhost](11-escenarios-selfhost.md) si quieres comparar VPS completa con proxy local o proxy en VPS hacia casa
- [Securizacion de la VPS](07-securizacion-vps.md) para proteger el servidor
- [Configuracion inicial](04-configuracion-inicial.md) para completar el asistente
- [Modulos](05-modulos.md) para entender la interfaz
- [Reproductores](06-reproductores.md) para configurar los clientes
- [Solucion de problemas](10-solucion-de-problemas.md) si algo no funciona
