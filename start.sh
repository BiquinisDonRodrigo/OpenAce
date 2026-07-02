#!/bin/bash
set -e

# Fix ownership of volume-mounted directories, then drop to non-root user.
chown -R openace:openace /openace/checkdb /tmp/openace 2>/dev/null || true
if ! gosu openace test -w /openace/checkdb; then
  echo "ERROR: /openace/checkdb is not writable by the openace user" >&2
  exit 1
fi

export PYTHONPATH=/openace

# Shared engine launch + Gluetun/VPN port-forward logic (functions only).
# Defines config_value, resolve_p2p_port, start_engine, port_watch_loop, etc.
source /openace/engine.sh

# Detect VPN mode, resolve the API + initial P2P port (waits up to 20s for
# Gluetun), and write the vpn_mode marker. Then start the engine restart loop
# and the port-forward watcher in the background.
engine_init
start_engine &
port_watch_loop &

# Start proxy in the foreground as the openace user
GUNICORN_WORKERS_VALUE=$(config_value GUNICORN_WORKERS 1)
GUNICORN_WORKER_CONNECTIONS_VALUE=$(config_value GUNICORN_WORKER_CONNECTIONS 2000)
GUNICORN_ARGS="--chdir /openace --worker-class gevent --bind 0.0.0.0:8888 \
  --workers ${GUNICORN_WORKERS_VALUE} \
  --worker-connections ${GUNICORN_WORKER_CONNECTIONS_VALUE} \
  --keep-alive 15 \
  --timeout 3600 --graceful-timeout 3600 \
  --max-requests 1000 --max-requests-jitter 100"

REVERSE_PROXY_VALUE=$(config_value REVERSE_PROXY false)
if [[ "${REVERSE_PROXY_VALUE,,}" =~ ^(true|1|yes)$ ]]; then
  FORWARDED_ALLOW_IPS="$(config_value FORWARDED_ALLOW_IPS 127.0.0.1)"
  GUNICORN_ARGS="$GUNICORN_ARGS --forwarded-allow-ips=$FORWARDED_ALLOW_IPS"
  echo "Reverse proxy mode enabled — trusting forwarded headers from: $FORWARDED_ALLOW_IPS"
fi

exec gosu openace gunicorn $GUNICORN_ARGS server:app
