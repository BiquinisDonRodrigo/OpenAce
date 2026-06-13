# Securizacion de la VPS

Guia para asegurar la VPS donde se ejecuta OpenAce, limitando el acceso solo a los puertos necesarios.

## Puertos del servicio

### Puertos que deben estar abiertos al exterior

| Puerto | Protocolo | Servicio | Motivo |
|---|---|---|---|
| 22 | TCP | SSH | Administracion remota |
| 80 | TCP | nginx | Redireccion HTTP a HTTPS y challenge ACME (Let's Encrypt) |
| 443 | TCP | nginx | Trafico HTTPS (interfaz web, API, streaming) |
| 4001 | TCP + UDP | Kubo (IPFS) | Protocolo swarm IPFS, necesario para resolver listas M3U via IPFS/IPNS |

### Puertos que deben estar bloqueados al exterior

| Puerto | Servicio | Motivo del bloqueo |
|---|---|---|
| 8888 | OpenAce (gunicorn) | Sin SSL ni rate limiting; todo el trafico debe pasar por nginx |
| 5001 | Kubo (API IPFS) | API de administracion IPFS, solo accesible desde localhost |
| 6878 | AceStream Engine | API interna del motor, solo accesible dentro del contenedor |
| 8001 | Gluetun (panel VPN) | Panel de control de la VPN, solo con despliegue VPN |

## Paso 1: Configurar iptables

iptables es el firewall nativo del kernel Linux. A diferencia de wrappers como UFW, opera directamente sobre netfilter, lo que lo hace mas predecible cuando se combina con Docker (que tambien manipula iptables internamente).

### Instalar iptables-persistent

Para que las reglas sobrevivan a reinicios:

```bash
sudo apt install -y iptables-persistent
```

Durante la instalacion preguntara si quieres guardar las reglas actuales. Responde "Si".

### Reglas IPv4

```bash
# Limpiar reglas existentes
sudo iptables -F INPUT
sudo iptables -F OUTPUT

# Politica por defecto: denegar entrante, permitir saliente
sudo iptables -P INPUT DROP
sudo iptables -P FORWARD DROP
sudo iptables -P OUTPUT ACCEPT

# Permitir loopback (comunicacion interna del servidor)
sudo iptables -A INPUT -i lo -j ACCEPT

# Permitir conexiones ya establecidas y relacionadas
sudo iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# SSH (cambiar el puerto si usas uno personalizado)
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT

# HTTP y HTTPS (nginx)
sudo iptables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT

# IPFS swarm (necesario para resolver listas IPFS/IPNS)
sudo iptables -A INPUT -p tcp --dport 4001 -j ACCEPT
sudo iptables -A INPUT -p udp --dport 4001 -j ACCEPT

# Bloquear explicitamente puertos internos (logging opcional)
sudo iptables -A INPUT -p tcp --dport 8888 -j DROP
sudo iptables -A INPUT -p tcp --dport 5001 -j DROP
sudo iptables -A INPUT -p tcp --dport 8001 -j DROP

# Permitir ping (ICMP) — opcional pero recomendado para diagnostico
sudo iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT
```

### Reglas IPv6

```bash
# Limpiar reglas existentes
sudo ip6tables -F INPUT
sudo ip6tables -F OUTPUT

# Politica por defecto
sudo ip6tables -P INPUT DROP
sudo ip6tables -P FORWARD DROP
sudo ip6tables -P OUTPUT ACCEPT

# Permitir loopback
sudo ip6tables -A INPUT -i lo -j ACCEPT

# Permitir conexiones establecidas
sudo ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# ICMPv6 (necesario para el funcionamiento de IPv6)
sudo ip6tables -A INPUT -p ipv6-icmp -j ACCEPT

# SSH
sudo ip6tables -A INPUT -p tcp --dport 22 -j ACCEPT

# HTTP y HTTPS
sudo ip6tables -A INPUT -p tcp --dport 80 -j ACCEPT
sudo ip6tables -A INPUT -p tcp --dport 443 -j ACCEPT

# IPFS swarm
sudo ip6tables -A INPUT -p tcp --dport 4001 -j ACCEPT
sudo ip6tables -A INPUT -p udp --dport 4001 -j ACCEPT

# Bloquear puertos internos
sudo ip6tables -A INPUT -p tcp --dport 8888 -j DROP
sudo ip6tables -A INPUT -p tcp --dport 5001 -j DROP
sudo ip6tables -A INPUT -p tcp --dport 8001 -j DROP
```

### Guardar las reglas

```bash
sudo netfilter-persistent save
```

Esto guarda las reglas en `/etc/iptables/rules.v4` y `/etc/iptables/rules.v6`. Se restauran automaticamente en cada reinicio.

### Verificar las reglas activas

```bash
# IPv4
sudo iptables -L INPUT -n -v --line-numbers

# IPv6
sudo ip6tables -L INPUT -n -v --line-numbers
```

Resultado esperado (IPv4):

```
Chain INPUT (policy DROP)
num   target   prot  opt  source    destination
1     ACCEPT   all   --   0.0.0.0/0  0.0.0.0/0    /* loopback */
2     ACCEPT   all   --   0.0.0.0/0  0.0.0.0/0    ctstate RELATED,ESTABLISHED
3     ACCEPT   tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:22
4     ACCEPT   tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:80
5     ACCEPT   tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:443
6     ACCEPT   tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:4001
7     ACCEPT   udp   --   0.0.0.0/0  0.0.0.0/0    udp dpt:4001
8     DROP     tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:8888
9     DROP     tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:5001
10    DROP     tcp   --   0.0.0.0/0  0.0.0.0/0    tcp dpt:8001
11    ACCEPT   icmp  --   0.0.0.0/0  0.0.0.0/0    icmptype 8
```

### Limitar intentos de SSH (proteccion contra fuerza bruta)

Sustituye la regla SSH basica por esta que limita a 5 conexiones nuevas por minuto:

```bash
sudo iptables -D INPUT -p tcp --dport 22 -j ACCEPT
sudo iptables -I INPUT 3 -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --set --name ssh
sudo iptables -I INPUT 4 -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --update --seconds 60 --hitcount 5 --name ssh -j DROP
sudo iptables -I INPUT 5 -p tcp --dport 22 -j ACCEPT
```

Lo mismo para IPv6:

```bash
sudo ip6tables -D INPUT -p tcp --dport 22 -j ACCEPT
sudo ip6tables -I INPUT 3 -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --set --name ssh
sudo ip6tables -I INPUT 4 -p tcp --dport 22 -m conntrack --ctstate NEW -m recent --update --seconds 60 --hitcount 5 --name ssh -j DROP
sudo ip6tables -I INPUT 5 -p tcp --dport 22 -j ACCEPT
```

Guarda despues de modificar:

```bash
sudo netfilter-persistent save
```

## Paso 2: Docker y el firewall

**Importante:** Docker manipula iptables directamente para gestionar el networking de contenedores. Crea sus propias cadenas (`DOCKER`, `DOCKER-USER`) y puede exponer puertos al exterior independientemente de tus reglas en la cadena INPUT.

### Solucion recomendada: Vincular puertos solo a localhost

Edita tu `docker-compose` para que los puertos internos solo escuchen en `127.0.0.1`:

```yaml
services:
  open-ace:
    ports:
      - "127.0.0.1:8888:8888"  # Solo accesible desde localhost (nginx)
    # ...

  kubo:
    ports:
      - "4001:4001/tcp"          # IPFS swarm: debe ser publico
      - "4001:4001/udp"
      - "127.0.0.1:5001:5001"   # API IPFS: solo localhost
    # ...
```

Con VPN, modifica igualmente el contenedor `acestream-vpn`:

```yaml
services:
  acestream-vpn:
    ports:
      - "127.0.0.1:8888:8888"  # Solo accesible desde localhost
      - "127.0.0.1:8001:8001"  # Panel VPN: solo localhost
    # ...
```

Esto es la forma mas fiable de proteger puertos: Docker no puede exponer un puerto que solo escucha en loopback, independientemente de lo que haga con iptables.

### Alternativa: Reglas en la cadena DOCKER-USER

Docker respeta las reglas de la cadena `DOCKER-USER`. Puedes usarla para filtrar trafico hacia los contenedores sin desactivar el networking de Docker:

```bash
# Bloquear acceso externo al puerto 8888 de contenedores
sudo iptables -I DOCKER-USER -p tcp --dport 8888 -j DROP
sudo iptables -I DOCKER-USER -p tcp --dport 8888 -s 127.0.0.1 -j ACCEPT

# Bloquear acceso externo al puerto 5001 de contenedores
sudo iptables -I DOCKER-USER -p tcp --dport 5001 -j DROP
sudo iptables -I DOCKER-USER -p tcp --dport 5001 -s 127.0.0.1 -j ACCEPT

sudo netfilter-persistent save
```

### Alternativa: Desactivar la manipulacion de iptables por Docker

Crea o edita `/etc/docker/daemon.json`:

```json
{
  "iptables": false
}
```

Reinicia Docker:

```bash
sudo systemctl restart docker
```

**Atencion:** Con esta opcion, los contenedores no tendran acceso a internet automaticamente. Tendras que gestionar NAT y forwarding manualmente. Solo usa este metodo si entiendes las implicaciones.

## Paso 3: Securizar SSH

### Cambiar el puerto por defecto

Edita `/etc/ssh/sshd_config`:

```
Port 2222
```

Actualiza las reglas de iptables:

```bash
# Eliminar regla del puerto 22
sudo iptables -D INPUT -p tcp --dport 22 -j ACCEPT
sudo ip6tables -D INPUT -p tcp --dport 22 -j ACCEPT

# Anadir regla para el nuevo puerto
sudo iptables -A INPUT -p tcp --dport 2222 -j ACCEPT
sudo ip6tables -A INPUT -p tcp --dport 2222 -j ACCEPT

sudo netfilter-persistent save
sudo systemctl restart sshd
```

Si usas las reglas de rate limiting del paso 1, actualiza tambien esas reglas cambiando `--dport 22` por `--dport 2222`.

### Desactivar login con contrasena

Usa autenticacion por clave publica. Edita `/etc/ssh/sshd_config`:

```
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
```

Asegurate de tener tu clave publica en `~/.ssh/authorized_keys` antes de aplicar estos cambios.

```bash
sudo systemctl restart sshd
```

## Paso 4: Instalar fail2ban

fail2ban monitoriza los logs y bloquea IPs con comportamiento sospechoso anadiendo reglas de iptables dinamicamente.

```bash
sudo apt install -y fail2ban
```

Crea `/etc/fail2ban/jail.local`:

```ini
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5
banaction = iptables-multiport

[sshd]
enabled = true
port = 2222
logpath = /var/log/auth.log

[nginx-http-auth]
enabled = true
logpath = /var/log/nginx/openace_error.log

[nginx-limit-req]
enabled = true
logpath = /var/log/nginx/openace_error.log
maxretry = 10
```

Arranca fail2ban:

```bash
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

Verifica el estado:

```bash
sudo fail2ban-client status
sudo fail2ban-client status sshd
```

Para ver las IPs baneadas:

```bash
sudo fail2ban-client status sshd
sudo iptables -L f2b-sshd -n
```

## Paso 5: Actualizaciones automaticas de seguridad

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Selecciona "Si" para activar las actualizaciones automaticas. Solo se instalaran parches de seguridad.

## Paso 6: Verificar la securizacion

### Escanear puertos abiertos desde fuera

Desde otra maquina o usando un servicio online:

```bash
nmap -Pn tu-dominio.com
```

Resultado esperado: solo deben aparecer los puertos 22 (o el personalizado), 80, 443 y 4001.

### Verificar desde la propia VPS

```bash
# Puertos escuchando
sudo ss -tlnp

# Reglas de iptables (IPv4)
sudo iptables -L -n -v --line-numbers

# Reglas de iptables (IPv6)
sudo ip6tables -L -n -v --line-numbers

# Cadena DOCKER-USER (si usas reglas ahi)
sudo iptables -L DOCKER-USER -n -v --line-numbers

# Estado de fail2ban
sudo fail2ban-client status
```

### Test de SSL

Usa SSL Labs para verificar la configuracion TLS:

```
https://www.ssllabs.com/ssltest/analyze.html?d=tu-dominio.com
```

Deberia obtener una calificacion A o A+.

## Script de configuracion rapida

Script que aplica todas las reglas de iptables de una sola vez. Guardalo como `setup-firewall.sh`:

```bash
#!/bin/bash
set -e

SSH_PORT=${1:-22}

echo "Configurando iptables (SSH en puerto $SSH_PORT)..."

# --- IPv4 ---
iptables -F INPUT
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT

iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -p tcp --dport "$SSH_PORT" -m conntrack --ctstate NEW -m recent --set --name ssh
iptables -A INPUT -p tcp --dport "$SSH_PORT" -m conntrack --ctstate NEW -m recent --update --seconds 60 --hitcount 5 --name ssh -j DROP
iptables -A INPUT -p tcp --dport "$SSH_PORT" -j ACCEPT
iptables -A INPUT -p tcp --dport 80 -j ACCEPT
iptables -A INPUT -p tcp --dport 443 -j ACCEPT
iptables -A INPUT -p tcp --dport 4001 -j ACCEPT
iptables -A INPUT -p udp --dport 4001 -j ACCEPT
iptables -A INPUT -p tcp --dport 8888 -j DROP
iptables -A INPUT -p tcp --dport 5001 -j DROP
iptables -A INPUT -p tcp --dport 8001 -j DROP
iptables -A INPUT -p icmp --icmp-type echo-request -j ACCEPT

# --- IPv6 ---
ip6tables -F INPUT
ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
ip6tables -P OUTPUT ACCEPT

ip6tables -A INPUT -i lo -j ACCEPT
ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
ip6tables -A INPUT -p ipv6-icmp -j ACCEPT
ip6tables -A INPUT -p tcp --dport "$SSH_PORT" -j ACCEPT
ip6tables -A INPUT -p tcp --dport 80 -j ACCEPT
ip6tables -A INPUT -p tcp --dport 443 -j ACCEPT
ip6tables -A INPUT -p tcp --dport 4001 -j ACCEPT
ip6tables -A INPUT -p udp --dport 4001 -j ACCEPT
ip6tables -A INPUT -p tcp --dport 8888 -j DROP
ip6tables -A INPUT -p tcp --dport 5001 -j DROP
ip6tables -A INPUT -p tcp --dport 8001 -j DROP

# --- Guardar ---
netfilter-persistent save

echo "Firewall configurado correctamente."
echo "Puertos abiertos: $SSH_PORT (SSH), 80 (HTTP), 443 (HTTPS), 4001 (IPFS)"
echo "Puertos bloqueados: 8888, 5001, 8001"
```

Uso:

```bash
# Puerto SSH por defecto (22)
sudo bash setup-firewall.sh

# Puerto SSH personalizado
sudo bash setup-firewall.sh 2222
```

## Resumen de securizacion

| Medida | Estado |
|---|---|
| iptables configurado (IPv4 + IPv6) | Obligatorio |
| Puertos Docker vinculados a localhost | Obligatorio |
| SSH con clave publica | Muy recomendado |
| Puerto SSH personalizado | Recomendado |
| Rate limiting en SSH (iptables) | Recomendado |
| fail2ban instalado | Muy recomendado |
| Actualizaciones automaticas | Recomendado |
| SSL con calificacion A+ | Obligatorio (via nginx) |

## Siguientes pasos

- [Despliegue en VPS](03-despliegue-vps.md) para la configuracion de nginx y SSL
- [Configuracion inicial](04-configuracion-inicial.md) para completar el asistente
- [Modulos](05-modulos.md) para entender la interfaz
