#!/bin/bash
set -e

# Fix ownership of volume-mounted directories, then drop to non-root user.
chown -R openace:openace /openace/checkdb /tmp/openace 2>/dev/null || true
if ! gosu openace test -w /openace/checkdb; then
  echo "ERROR: /openace/checkdb is not writable by the openace user" >&2
  exit 1
fi

FORWARDED_PORT_FILE="/tmp/gluetun/forwarded_port"

# Wait until the file exists and has content (max 20 seconds)
echo "Looking for Gluetun port file: $FORWARDED_PORT_FILE"
for i in {1..20}; do
  if [[ -s "$FORWARDED_PORT_FILE" ]]; then
    PORT=$(cat "$FORWARDED_PORT_FILE")
    echo "Port successfully retrieved from Gluetun: $PORT"
    break
  fi
  echo "Gluetun hasn't provided a port yet, waiting... ($i/20)"
  sleep 1
done

# If no forwarded port, use default
if [[ -z "$PORT" ]]; then
  PORT=${ACESTREAM_PORT:-6878}
  echo "No forwarded port found, falling back to default: $PORT"
fi

# The AceStream engine HTTP API must always be on ACESTREAM_PORT (default 6878)
# so the Flask proxy can reach it. The Gluetun forwarded port is for P2P only
# and is passed via --bind instead of --port to keep the API on 6878.
API_PORT=${ACESTREAM_PORT:-6878}
P2P_PORT="$PORT"

# Start AceStream engine in the background with a restart loop.
# Engine output goes to stdout/stderr (Docker log collection).
start_engine() {
  while true; do
    echo "Starting AceStream (API on $API_PORT, P2P on $P2P_PORT)..."
    if [[ "$P2P_PORT" != "$API_PORT" ]]; then
      gosu openace /openace/start-engine --client-console --port "$API_PORT" --bind "$P2P_PORT" 2>&1
    else
      gosu openace /openace/start-engine --client-console --port "$API_PORT" 2>&1
    fi
    exit_code=$?
    echo "AceStream exited (code $exit_code), restarting in 3s..." >&2
    sleep 3
  done
}
start_engine &

export PYTHONPATH=/openace

# Start proxy in the foreground as the openace user
GUNICORN_ARGS="--chdir /openace --worker-class gevent --bind 0.0.0.0:8888 \
  --workers ${GUNICORN_WORKERS:-2} \
  --worker-connections ${GUNICORN_WORKER_CONNECTIONS:-2000} \
  --keep-alive 15 \
  --timeout 3600 --graceful-timeout 3600 \
  --max-requests 1000 --max-requests-jitter 100"

if [[ "${REVERSE_PROXY,,}" =~ ^(true|1|yes)$ ]]; then
  FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
  GUNICORN_ARGS="$GUNICORN_ARGS --forwarded-allow-ips=$FORWARDED_ALLOW_IPS"
  echo "Reverse proxy mode enabled — trusting forwarded headers from: $FORWARDED_ALLOW_IPS"
fi

exec gosu openace gunicorn $GUNICORN_ARGS server:app
