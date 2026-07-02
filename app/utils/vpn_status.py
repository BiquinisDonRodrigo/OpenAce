"""Read-only view of the P2P/VPN state written by ``start.sh``.

The entrypoint publishes a few marker files under ``/tmp/openace`` describing
the resolved P2P port and (in VPN mode) the port Gluetun is currently
forwarding. This module reads them so the dashboard and the healthcheck can
report whether the engine is bound to the port Gluetun actually forwards — the
classic failure mode being ProtonVPN rotating the forwarded port while the
engine keeps listening on the stale one.

All access is best-effort: missing/unreadable files resolve to ``None`` so the
caller can degrade gracefully (e.g. outside the container, or before the
entrypoint has written anything).
"""

import os

COMPONENT = "vpn_status"

# Mirrors OPENACE_STATE_DIR in start.sh.
_STATE_DIR = os.environ.get("OPENACE_STATE_DIR", "/tmp/openace")


def _read_marker(name):
    """Return the trimmed contents of a marker file, or ``None``."""
    try:
        with open(os.path.join(_STATE_DIR, name), "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            return value or None
    except Exception:
        return None


def get_vpn_status():
    """Return the current P2P/VPN status as reported by ``start.sh``.

    Keys:
      * ``vpn_mode``       — bool, whether Gluetun/VPN is in use.
      * ``active_p2p_port`` — str port the engine is bound to (or ``None``).
      * ``gluetun_p2p_port`` — str port Gluetun reports as forwarded
        (``None`` outside VPN or while Gluetun is unreachable).
      * ``synced``         — bool/None: ``True`` if the engine's bound port
        matches Gluetun's forwarded port; ``None`` when not applicable
        (non-VPN) or unknown.
    """
    vpn_mode = _read_marker("vpn_mode") == "1"
    active = _read_marker("active_p2p_port")
    gluetun = _read_marker("gluetun_p2p_port")

    synced = None
    if vpn_mode:
        if active is not None and gluetun is not None:
            try:
                synced = int(active) == int(gluetun)
            except (TypeError, ValueError):
                synced = False
        elif active is not None:
            # Gluetun report missing while in VPN mode: treat as not synced.
            synced = False

    return {
        "vpn_mode": vpn_mode,
        "active_p2p_port": active,
        "gluetun_p2p_port": gluetun,
        "synced": synced,
    }
