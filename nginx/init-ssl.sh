#!/bin/bash
# Configura SSL con Let's Encrypt para OpenAce.
#
# Uso:
#   ./nginx/init-ssl.sh mi-dominio.com [email@example.com]
#
# Pasos que ejecuta:
#   1. Escribe el dominio en la config de nginx
#   2. Genera un certificado temporal autofirmado
#   3. Arranca nginx para responder al challenge ACME
#   4. Solicita el certificado real a Let's Encrypt
#   5. Recarga nginx y levanta el stack completo
set -e

DOMAIN="${1:?Uso: $0 <dominio> [email]}"
EMAIL="${2:-}"
COMPOSE_FILE="docker-compose.nginx.yaml"
CERT_PATH="./certbot/conf/live/$DOMAIN"
NGINX_CONF="./nginx/openace.conf"

echo ""
echo "============================================================"
echo "  OpenAce — Configuración SSL"
echo "  Dominio: $DOMAIN"
echo "============================================================"
echo ""

# 1. Sustituir _DOMAIN_ en la configuración de nginx
if grep -q '_DOMAIN_' "$NGINX_CONF" 2>/dev/null; then
    echo "[1/5] Configurando nginx para $DOMAIN..."
    sed -i "s/_DOMAIN_/$DOMAIN/g" "$NGINX_CONF"
else
    echo "[1/5] nginx ya configurado para este dominio."
fi

# 2. Crear directorios
mkdir -p certbot/conf certbot/www

# 3. Certificado temporal para arrancar nginx
if [ ! -f "$CERT_PATH/fullchain.pem" ]; then
    echo "[2/5] Generando certificado temporal autofirmado..."
    mkdir -p "$CERT_PATH"
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "$CERT_PATH/privkey.pem" \
        -out "$CERT_PATH/fullchain.pem" \
        -subj "/CN=$DOMAIN" 2>/dev/null

    echo "[3/5] Arrancando nginx con certificado temporal..."
    docker compose -f "$COMPOSE_FILE" up -d nginx
    sleep 3

    echo "[4/5] Solicitando certificado a Let's Encrypt..."
    CERTBOT_ARGS="certonly --webroot --webroot-path /var/www/certbot"
    CERTBOT_ARGS="$CERTBOT_ARGS -d $DOMAIN --agree-tos --non-interactive --force-renewal"
    if [ -n "$EMAIL" ]; then
        CERTBOT_ARGS="$CERTBOT_ARGS --email $EMAIL"
    else
        CERTBOT_ARGS="$CERTBOT_ARGS --register-unsafely-without-email"
    fi

    docker compose -f "$COMPOSE_FILE" run --rm certbot $CERTBOT_ARGS

    echo "[5/5] Recargando nginx con certificado real..."
    docker compose -f "$COMPOSE_FILE" exec nginx nginx -s reload
else
    echo "[2-4/5] Ya existe un certificado para $DOMAIN, omitiendo."
fi

# 5. Levantar todo el stack
echo ""
echo "Levantando todos los servicios..."
docker compose -f "$COMPOSE_FILE" up -d

echo ""
echo "============================================================"
echo "  ¡Listo!"
echo "  https://$DOMAIN"
echo "============================================================"
echo ""
