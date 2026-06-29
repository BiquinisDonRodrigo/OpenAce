#!/bin/bash
set -e

# Fix ownership of volume-mounted directories, then drop to non-root user.
chown -R openace:openace /openace/checkdb /tmp/openace 2>/dev/null || true
if ! gosu openace test -w /openace/checkdb; then
  echo "ERROR: /openace/checkdb is not writable by the openace user" >&2
  exit 1
fi

export PYTHONPATH=/openace

config_value() {
  local key="$1"
  local default="$2"
  gosu openace python3 -c 'from app.utils import environment_store; import sys; print(environment_store.get_str(sys.argv[1]) or sys.argv[2])' "$key" "$default" 2>/dev/null || printf '%s\n' "$default"
}

FORWARDED_PORT_FILE="/tmp/gluetun/forwarded_port"

is_valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1 && "$1" <= 65535 ))
}

# Wait until the file exists and has content (max 20 seconds)
echo "Looking for Gluetun port file: $FORWARDED_PORT_FILE"
for i in {1..20}; do
  if [[ -s "$FORWARDED_PORT_FILE" ]]; then
    PORT=$(cat "$FORWARDED_PORT_FILE")
    if is_valid_port "$PORT"; then
      echo "Port successfully retrieved from Gluetun: $PORT"
      break
    fi
    echo "Ignoring invalid Gluetun port: $PORT" >&2
    PORT=""
  fi
  echo "Gluetun hasn't provided a port yet, waiting... ($i/20)"
  sleep 1
done

# If no forwarded port, use default
if [[ -z "$PORT" ]]; then
  PORT=$(config_value ACESTREAM_PORT 6878)
  echo "No forwarded port found, falling back to default: $PORT"
fi
if ! is_valid_port "$PORT"; then
  echo "ERROR: invalid P2P port: $PORT" >&2
  exit 1
fi

# The AceStream engine HTTP API must always be on ACESTREAM_PORT (default 6878)
# so the Flask proxy can reach it. The Gluetun forwarded port is for P2P only
# and is passed via --bind instead of --port to keep the API on 6878.
API_PORT=$(config_value ACESTREAM_PORT 6878)
P2P_PORT="$PORT"

# Start AceStream engine in the background with a restart loop.
# Engine output goes to stdout/stderr (Docker log collection).
start_engine() {
  while true; do
    echo "Starting AceStream (API on $API_PORT, P2P on $P2P_PORT)..."
    set +e
    if [[ "$P2P_PORT" != "$API_PORT" ]]; then
      gosu openace /openace/start-engine --client-console --port "$API_PORT" --bind "$P2P_PORT" 2>&1
    else
      gosu openace /openace/start-engine --client-console --port "$API_PORT" 2>&1
    fi
    exit_code=$?
    set -e
    echo "AceStream exited (code $exit_code), restarting in 3s..." >&2
    sleep 3
  done
}
start_engine &

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
