# Shared AceStream engine launch + Gluetun/VPN port-forward logic.
#
# Sourced by:
#   - start.sh                     (production entrypoint, runs gunicorn)
#   - docker-compose.dev.yaml      (dev entrypoint, runs the Flask dev server)
#
# CONTRACT: this file MUST ONLY define variables and functions. It must have
# NO side effects when sourced, so the caller decides what to execute and in
# what order (e.g. engine_init -> start_engine & -> port_watch_loop &).

FORWARDED_PORT_FILE="/tmp/gluetun/forwarded_port"
OPENACE_STATE_DIR="${OPENACE_STATE_DIR:-/tmp/openace}"
ACTIVE_P2P_PORT_FILE="$OPENACE_STATE_DIR/active_p2p_port"
GLUETUN_P2P_PORT_FILE="$OPENACE_STATE_DIR/gluetun_p2p_port"
VPN_MODE_FILE="$OPENACE_STATE_DIR/vpn_mode"
ENGINE_PID_FILE="$OPENACE_STATE_DIR/engine.pid"
# Gluetun HTTP control server (exposed by HTTP_CONTROL_SERVER_ADDRESS=:8001).
GLUETUN_CONTROL_URL="http://127.0.0.1:8001/v1/port_forwarded"
# How often the port-forward watcher polls Gluetun (ProtonVPN rotates ports on
# reconnect). Overridable via env.
PORT_WATCH_INTERVAL_S="${PORT_WATCH_INTERVAL_S:-45}"

# Read a typed setting from environment_store, falling back to a default.
config_value() {
  local key="$1"
  local default="$2"
  gosu openace python3 -c 'from app.utils import environment_store; import sys; print(environment_store.get_str(sys.argv[1]) or sys.argv[2])' "$key" "$default" 2>/dev/null || printf '%s\n' "$default"
}

is_valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( "$1" >= 1 && "$1" <= 65535 ))
}

# Query Gluetun for the current forwarded port.
# Order: control API (GET /v1/port_forwarded) -> forwarded_port file. Some
# Gluetun versions don't expose the HTTP route, so the file is the reliable
# fallback (Gluetun rewrites it whenever the forwarded port changes).
# Echoes the port on success (and caches it to GLUETUN_P2P_PORT_FILE for the
# dashboard), echoes nothing on failure.
query_gluetun_port() {
  local port=""
  if command -v curl >/dev/null 2>&1; then
    port=$(curl -fsS --max-time 3 "$GLUETUN_CONTROL_URL" 2>/dev/null \
           | sed -nE 's/.*"port"[[:space:]]*:[[:space:]]*([0-9]{1,5}).*/\1/p' | head -n1)
  fi
  if ! is_valid_port "$port" && [[ -s "$FORWARDED_PORT_FILE" ]]; then
    local fp
    fp=$(<"$FORWARDED_PORT_FILE")
    if is_valid_port "$fp"; then
      port="$fp"
    fi
  fi
  if is_valid_port "$port"; then
    printf '%s\n' "$port" > "$GLUETUN_P2P_PORT_FILE" 2>/dev/null || true
    printf '%s\n' "$port"
  else
    printf '\n'
  fi
}

# Resolve the P2P port the engine should bind to.
# Order: Gluetun control API -> forwarded_port file. Returns non-zero when no
# forwarded port is available (caller falls back to the API port).
resolve_p2p_port() {
  local port
  port=$(query_gluetun_port)
  if is_valid_port "$port"; then
    printf '%s\n' "$port"
    return 0
  fi
  if [[ -s "$FORWARDED_PORT_FILE" ]]; then
    local fp
    fp=$(<"$FORWARDED_PORT_FILE")
    if is_valid_port "$fp"; then
      printf '%s\n' "$fp"
      return 0
    fi
  fi
  return 1
}

# Detect VPN/Gluetun mode: the gluetun-data volume is only mounted in the VPN
# composes, so /tmp/gluetun being a directory is a reliable signal. Sets VPN_MODE.
detect_vpn_mode() {
  VPN_MODE=0
  if [[ -d /tmp/gluetun ]]; then
    VPN_MODE=1
  fi
}

# Start AceStream engine in a restart loop.
# Re-resolves the P2P port on every iteration, so a port rotation detected by
# the watcher (which kills the engine PID) triggers a re-bind. Writes the
# active port marker for the dashboard. Engine output goes to stdout/stderr.
# Requires API_PORT to be set (by engine_init).
start_engine() {
  local backoff=3
  while true; do
    local current_p2p
    if ! current_p2p=$(resolve_p2p_port); then
      current_p2p="$API_PORT"
    fi
    printf '%s\n' "$current_p2p" > "$ACTIVE_P2P_PORT_FILE" 2>/dev/null || true
    echo "Starting AceStream (API on $API_PORT, P2P on $current_p2p)..."
    local start_ts=$SECONDS
    set +e
    if [[ "$current_p2p" != "$API_PORT" ]]; then
      gosu openace /openace/start-engine --client-console --port "$API_PORT" --bind "$current_p2p" 2>&1 &
    else
      gosu openace /openace/start-engine --client-console --port "$API_PORT" 2>&1 &
    fi
    local pid=$!
    printf '%s\n' "$pid" > "$ENGINE_PID_FILE" 2>/dev/null || true
    wait "$pid" || true
    local exit_code=$?
    set -e
    rm -f "$ENGINE_PID_FILE" 2>/dev/null || true
    local ran=$(( SECONDS - start_ts ))
    # Exponential backoff on rapid crashes; reset if the engine ran long enough.
    if (( ran > 15 )); then
      backoff=3
    else
      backoff=$(( backoff * 2 ))
      if (( backoff > 60 )); then backoff=60; fi
    fi
    echo "AceStream (pid $pid) exited (code $exit_code, ran ${ran}s), restarting in ${backoff}s..." >&2
    sleep "$backoff"
  done
}

# Port-forward watcher (VPN mode only): ProtonVPN rotates the forwarded port on
# reconnect. Poll Gluetun's control API and, when the port changes, signal the
# engine to restart so it re-binds. Streams in flight are interrupted.
port_watch_loop() {
  if [[ "$VPN_MODE" != "1" ]]; then return 0; fi
  while true; do
    sleep "$PORT_WATCH_INTERVAL_S"
    local gluetun_port active_port pid
    gluetun_port=$(query_gluetun_port)
    if [[ -z "$gluetun_port" ]]; then continue; fi
    active_port=""
    if [[ -r "$ACTIVE_P2P_PORT_FILE" ]]; then active_port=$(<"$ACTIVE_P2P_PORT_FILE"); fi
    if [[ -n "$active_port" && "$gluetun_port" != "$active_port" ]]; then
      pid=""
      if [[ -r "$ENGINE_PID_FILE" ]]; then pid=$(<"$ENGINE_PID_FILE"); fi
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "Gluetun forwarded port changed ($active_port -> $gluetun_port); restarting engine to rebind." >&2
        kill -TERM "$pid" 2>/dev/null || true
      fi
    fi
  done
}

# One-time startup wiring. Detects VPN mode, resolves the API port and the
# initial P2P port (waiting up to 20s for Gluetun), and writes the vpn_mode
# marker. Exposes globals: API_PORT, RESOLVED_P2P_PORT, VPN_MODE.
engine_init() {
  detect_vpn_mode
  API_PORT=$(config_value ACESTREAM_PORT 6878)
  echo "Resolving P2P port (Gluetun control API / forwarded_port file)..."
  RESOLVED_P2P_PORT=""
  local i port
  for i in {1..20}; do
    if port=$(resolve_p2p_port); then
      RESOLVED_P2P_PORT="$port"
      echo "P2P port resolved: $RESOLVED_P2P_PORT"
      break
    fi
    echo "Waiting for Gluetun forwarded port... ($i/20)"
    sleep 1
  done
  if [[ -z "$RESOLVED_P2P_PORT" ]]; then
    RESOLVED_P2P_PORT="$API_PORT"
    if [[ "$VPN_MODE" == "1" ]]; then
      echo "WARNING: Gluetun/VPN detected but no forwarded port after 20s; binding P2P to $API_PORT (NOT forwarded through VPN). Streams may have poor inbound P2P." >&2
    else
      echo "No Gluetun detected (non-VPN mode); binding P2P to $API_PORT." >&2
    fi
  fi
  printf '%s\n' "$VPN_MODE" > "$VPN_MODE_FILE" 2>/dev/null || true
}
