# Despliegue en VPS

Guia para exponer OpenAce a internet con nginx como reverse proxy y SSL con Let's Encrypt.

## Requisitos

- VPS con acceso root (Ubuntu 22.04+ / Debian 12+)
- Dominio apuntando a la IP de la VPS (registro A y/o AAAA)
- Puertos 80 y 443 abiertos en el firewall
- Docker y Docker Compose instalados
- OpenAce desplegado y funcionando (ver [Despliegue paso a paso](02-despliegue-paso-a-paso.md))

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

## Paso 4: Activar REVERSE_PROXY en OpenAce

Anade la variable `REVERSE_PROXY=true` a la configuracion del contenedor `open-ace` en tu `docker-compose`:

```yaml
services:
  open-ace:
    environment:
      REVERSE_PROXY: "true"
      # ... resto de variables
```

Reinicia los contenedores:

```bash
docker compose -f docker-compose.simple.yaml down
docker compose -f docker-compose.simple.yaml up -d
```

Esto activa dos cosas:
- **ProxyFix** en Flask: `request.remote_addr` refleja la IP real del cliente, `request.is_secure` detecta HTTPS
- **--forwarded-allow-ips** en gunicorn: confiar en las cabeceras de proxy reenviadas por nginx

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

### Soporte IPv4 e IPv6

Nginx escucha en ambos protocolos:
- `listen 80` + `listen [::]:80`
- `listen 443 ssl http2` + `listen [::]:443 ssl http2`

## Con VPN

Si usas el despliegue con VPN (`docker-compose.yaml`), el contenedor `open-ace` no tiene su propia IP en la red Docker. En ese caso, edita `openace.conf` y cambia el upstream de:

```
proxy_pass http://open-ace:8888;
```

a:

```
proxy_pass http://acestream-vpn:8888;
```

Esto aplica a las 4 directivas `proxy_pass` del fichero.

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

- [Securizacion de la VPS](07-securizacion-vps.md) para proteger el servidor
- [Configuracion inicial](04-configuracion-inicial.md) para completar el asistente
- [Modulos](05-modulos.md) para entender la interfaz
- [Reproductores](06-reproductores.md) para configurar los clientes
