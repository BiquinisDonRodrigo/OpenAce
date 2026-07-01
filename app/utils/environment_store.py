import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.utils.check_store import _connect, _ensure_init, _lock
from app.utils.logging_utils import log_event

COMPONENT = "environment_store"

_initialized = False
_init_lock = threading.Lock()
_cache_lock = threading.Lock()
_cache = None
_cache_ts = 0.0
_CACHE_TTL_S = 1.0


def _setting(key, label, default, kind="string", group="General", help_text="", *,
             min_value=None, max_value=None, choices=None, secret=False,
             restart_required=False):
    return {
        "key": key,
        "label": label,
        "default": str(default),
        "type": kind,
        "group": group,
        "help": help_text,
        "min": min_value,
        "max": max_value,
        "choices": choices,
        "secret": secret,
        "restart_required": restart_required,
    }


SETTINGS = (
    _setting("AUTH_ENABLED", "Authentication enabled", "true", "bool", "OpenAce", restart_required=True),
    _setting("SESSION_DURATION_HOURS", "Session duration (hours)", "24", "int", "OpenAce", min_value=1, max_value=8760),
    _setting("OPENACE_SECRET_KEY", "OpenAce secret key", "", group="OpenAce", secret=True, restart_required=True),

    _setting("ACESTREAM_HOST", "AceStream host", "127.0.0.1", group="AceStream", restart_required=True),
    _setting("ACESTREAM_PORT", "AceStream API port", "6878", "int", "AceStream", min_value=1, max_value=65535, restart_required=True),

    _setting("IPFS_GATEWAY", "IPFS gateway", "http://kubo:48080", group="IPFS"),

    _setting("REVERSE_PROXY", "Reverse proxy mode", "false", "bool", "Proxy / URLs publicas", restart_required=True),
    _setting("FORWARDED_ALLOW_IPS", "Forwarded allow IPs", "127.0.0.1", group="Proxy / URLs publicas", restart_required=True),
    _setting("PUBLIC_BASE_URL", "PUBLIC_BASE_URLS", "", group="Proxy / URLs publicas"),

    _setting("OPENACE_FFMPEG_ENABLED", "FFmpeg enabled", "false", "bool", "FFmpeg", restart_required=True),
    _setting("OPENACE_IDLE_TIMEOUT_S", "Idle timeout (seconds)", "180", "int", "FFmpeg", min_value=10, max_value=86400, restart_required=True),
    _setting("OPENACE_CHUNK_SIZE", "Pipe read chunk size", "65536", "int", "FFmpeg", min_value=1024, max_value=10485760, restart_required=True),
    _setting("OPENACE_QUEUE_MAX", "Client queue max", "256", "int", "FFmpeg", min_value=1, max_value=100000, restart_required=True),
    _setting("OPENACE_PIPE_BUFFER_SIZE", "OS pipe buffer size", "1048576", "int", "FFmpeg", min_value=4096, max_value=16777216, restart_required=True),
    _setting("OPENACE_MAX_STREAMS", "Maximum simultaneous FFmpeg streams", "32", "int", "FFmpeg", min_value=1, max_value=1024, restart_required=True),
    _setting("OPENACE_ITERATE_TIMEOUT_S", "Stream iteration timeout", "180", "int", "FFmpeg", min_value=10, max_value=86400, restart_required=True),
    _setting("OPENACE_FFMPEG_RW_TIMEOUT_US", "FFmpeg read/write timeout (us)", "120000000", "int", "FFmpeg", min_value=1000000, max_value=3600000000, restart_required=True),
    _setting("OPENACE_FFMPEG_RESTARTS", "FFmpeg restart attempts", "3", "int", "FFmpeg", min_value=0, max_value=100, restart_required=True),
    _setting("OPENACE_FFMPEG_RESTART_BACKOFF_S", "FFmpeg restart backoff", "2", "float", "FFmpeg", min_value=0, max_value=3600, restart_required=True),
    _setting("OPENACE_FFMPEG_RTBUFSIZE", "FFmpeg realtime buffer size", "5M", group="FFmpeg", restart_required=True),
    _setting("OPENACE_FFMPEG_STAT_TUNING", "Tune FFmpeg from engine stats", "false", "bool", "FFmpeg", restart_required=True),
    _setting("OPENACE_QUEUE_MAX_LIVE", "Client queue max (live)", "256", "int", "FFmpeg", min_value=1, max_value=100000, restart_required=True),
    _setting("OPENACE_QUEUE_MAX_VOD", "Client queue max (vod)", "256", "int", "FFmpeg", min_value=1, max_value=100000, restart_required=True),
    _setting("OPENACE_TS_ALIGN", "Align MPEG-TS output to 188-byte packets", "true", "bool", "FFmpeg", restart_required=True),

    _setting("OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S", "Max stale HLS segment age", "30", "int", "HLS", min_value=1, max_value=3600, restart_required=True),
    _setting("OPENACE_HLS_TIME", "HLS segment duration", "4", "int", "HLS", min_value=1, max_value=60, restart_required=True),
    _setting("OPENACE_HLS_LIST_SIZE", "HLS list size", "15", "int", "HLS", min_value=1, max_value=200, restart_required=True),
    _setting("OPENACE_HLS_TIME_LIVE", "Live HLS segment duration", "2", "int", "HLS", min_value=1, max_value=60, restart_required=True),
    _setting("OPENACE_HLS_LIST_SIZE_LIVE", "Live HLS list size", "6", "int", "HLS", min_value=1, max_value=200, restart_required=True),
    _setting("OPENACE_HLS_LAZY", "Only produce HLS on demand", "false", "bool", "HLS", restart_required=True),
    _setting("OPENACE_STAT_POLL_READY_S", "Ready stat poll interval", "2.0", "float", "HLS", min_value=0.1, max_value=60, restart_required=True),
    _setting("OPENACE_ZERO_PEER_DEAD_POLLS", "Zero-peer dead polls", "10", "int", "HLS", min_value=1, max_value=1000, restart_required=True),
    _setting("OPENACE_HTTP_POOL_CONNECTIONS", "HTTP pool connections", "64", "int", "HLS", min_value=1, max_value=10000, restart_required=True),
    _setting("OPENACE_HTTP_POOL_MAXSIZE", "HTTP pool max size", "128", "int", "HLS", min_value=1, max_value=10000, restart_required=True),

    _setting("GUNICORN_WORKERS", "Gunicorn workers", "1", "int", "Gunicorn", min_value=1, max_value=32, restart_required=True),
    _setting("GUNICORN_WORKER_CONNECTIONS", "Gunicorn worker connections", "2000", "int", "Gunicorn", min_value=1, max_value=100000, restart_required=True),

    _setting("OPENACE_AUTO_SETUP", "Auto setup", "false", "bool", "Auto-setup", restart_required=True),
    _setting("OPENACE_ADMIN_USER", "Initial admin user", "admin", group="Auto-setup", restart_required=True),
    _setting("OPENACE_ADMIN_PASSWORD", "Initial admin password", "", group="Auto-setup", secret=True, restart_required=True),
    _setting("OPENACE_EULA_ACCEPT", "Auto-accept EULA", "false", "bool", "Auto-setup", restart_required=True),
)

_HELP_TEXTS = {
    "ACESTREAM_HOST": "Host donde OpenAce contacta con el motor AceStream. En Docker normal debe ser 127.0.0.1.",
    "ACESTREAM_PORT": "Puerto HTTP/API del motor AceStream usado por OpenAce para iniciar streams y consultar estado.",
    "IPFS_GATEWAY": "Gateway IPFS usado para resolver fuentes /ipfs/ e /ipns/ cuando se importan listas o plugins.",
    "AUTH_ENABLED": "Activa o desactiva la autenticacion de la interfaz web y de las rutas protegidas.",
    "OPENACE_SECRET_KEY": "Clave secreta Flask para sesiones y CSRF. Si queda vacia, OpenAce crea una clave persistente automaticamente.",
    "SESSION_DURATION_HOURS": "Horas de validez de una sesion web antes de requerir nuevo login.",
    "REVERSE_PROXY": "Activa soporte para cabeceras X-Forwarded-* cuando OpenAce esta detras de nginx, Caddy, Traefik u otro proxy.",
    "FORWARDED_ALLOW_IPS": "IPs desde las que Gunicorn acepta cabeceras X-Forwarded-* cuando REVERSE_PROXY esta activo.",
    "PUBLIC_BASE_URL": "Lista opcional de dominios/origins publicos permitidos, separados por coma, espacio o salto de linea. La M3U siempre usa la IP o dominio con el que entras; si un dominio coincide aqui, se normaliza con su esquema configurado.",
    "OPENACE_FFMPEG_ENABLED": "Activa FFmpeg para MPEG-TS/HLS. Si esta desactivado, MPEG-TS mantiene la URL /play/mpegts/<infohash> en OpenAce pero se proxya directamente desde el motor AceStream; HLS devuelve 503.",
    "OPENACE_IDLE_TIMEOUT_S": "Segundos de inactividad antes de cerrar streams FFmpeg MPEG-TS/HLS gestionados por OpenAce.",
    "OPENACE_CHUNK_SIZE": "Tamano de cada lectura del pipe de FFmpeg en bytes para streaming MPEG-TS.",
    "OPENACE_QUEUE_MAX": "Numero maximo de chunks en cola por cliente MPEG-TS antes de descartar datos o clientes lentos.",
    "OPENACE_PIPE_BUFFER_SIZE": "Tamano solicitado para el buffer del pipe del sistema operativo usado por FFmpeg.",
    "OPENACE_MAX_STREAMS": "Numero maximo de procesos/streams FFmpeg simultaneos que OpenAce permite arrancar.",
    "OPENACE_ITERATE_TIMEOUT_S": "Tiempo maximo de espera durante la iteracion/envio de un stream antes de cortar por timeout.",
    "OPENACE_FFMPEG_RW_TIMEOUT_US": "Timeout de lectura/escritura pasado a FFmpeg, en microsegundos.",
    "OPENACE_FFMPEG_RESTARTS": "Numero maximo de reintentos automaticos de FFmpeg por stream cuando falla.",
    "OPENACE_FFMPEG_RESTART_BACKOFF_S": "Espera entre reintentos de FFmpeg cuando un stream falla.",
    "OPENACE_HLS_STALE_SEGMENT_MAX_AGE_S": "Edad maxima permitida del segmento HLS mas reciente antes de considerar el stream obsoleto.",
    "OPENACE_HLS_TIME": "Duracion objetivo, en segundos, de cada segmento HLS para streams normales o VOD.",
    "OPENACE_HLS_LIST_SIZE": "Numero de segmentos que conserva la playlist HLS para streams normales o VOD.",
    "OPENACE_HLS_TIME_LIVE": "Duracion objetivo de segmentos HLS para streams en vivo. Menor valor reduce latencia.",
    "OPENACE_HLS_LIST_SIZE_LIVE": "Numero de segmentos en playlist para streams en vivo. Menor valor reduce latencia y margen de buffer.",
    "OPENACE_HLS_LAZY": "Cuando esta activo, el proxy arranca FFmpeg produciendo solo MPEG-TS y anade la salida HLS bajo demanda cuando un cliente la solicita. Ahorra CPU si solo se usa MPEG-TS. Derivado del analisis por ingenieria inversa del motor AceStream (salida unica distribuida).",
    "OPENACE_FFMPEG_RTBUFSIZE": "Limite de buffer realtime de FFmpeg para streams en vivo, por ejemplo 5M.",
    "OPENACE_FFMPEG_STAT_TUNING": "Ajusta dinamicamente el buffer realtime de FFmpeg (-rtbufsize) y la duracion de segmentos HLS en vivo a partir de las estadisticas que reporta el motor (current_bitrate, player_buffer_time). Desactivado por defecto. Derivado del analisis por ingenieria inversa del motor AceStream.",
    "OPENACE_QUEUE_MAX_LIVE": "Numero maximo de chunks en cola por cliente MPEG-TS para streams en vivo. Mas bajo que en VOD para priorizar latencia. Inspirado en max_buffer_chunks_ahead del motor AceStream (ingenieria inversa).",
    "OPENACE_QUEUE_MAX_VOD": "Numero maximo de chunks en cola por cliente MPEG-TS para streams VOD. Mas alto que en vivo para suavizar busquedas y prebuffer. Inspirado en max_buffer_chunks_ahead del motor AceStream (ingenieria inversa).",
    "OPENACE_TS_ALIGN": "Alinea el fan-out MPEG-TS a paquetes de 188 bytes y descarta cualquier prefijo hasta localizar el sync byte 0x47, para que cada suscriptor reciba un flujo decodificable. Defensivo: la salida -f mpegts de FFmpeg ya viene alineada. Inspirado en ProxyStream.data_before del motor AceStream (ingenieria inversa).",
    "OPENACE_STAT_POLL_READY_S": "Intervalo de polling del stat de AceStream cuando el stream ya esta listo.",
    "OPENACE_ZERO_PEER_DEAD_POLLS": "Numero de polls consecutivos con cero peers antes de declarar un stream como muerto.",
    "OPENACE_HTTP_POOL_CONNECTIONS": "Numero de pools de conexiones HTTP usados para hablar con AceStream y fuentes upstream.",
    "OPENACE_HTTP_POOL_MAXSIZE": "Tamano maximo del pool HTTP por host para peticiones concurrentes.",
    "GUNICORN_WORKERS": "Numero de workers Gunicorn. Se recomienda 1 porque streams, timers y caches son memoria local del proceso.",
    "GUNICORN_WORKER_CONNECTIONS": "Numero maximo de conexiones gevent por worker Gunicorn.",
    "OPENACE_AUTO_SETUP": "Permite completar el primer arranque sin wizard interactivo usando usuario, password y EULA configurados.",
    "OPENACE_ADMIN_USER": "Usuario administrador inicial usado por auto-setup o cuando se crea un admin automaticamente.",
    "OPENACE_ADMIN_PASSWORD": "Password inicial para auto-setup. Si queda vacio, OpenAce genera una password y la muestra una sola vez.",
    "OPENACE_EULA_ACCEPT": "Acepta la EULA automaticamente durante auto-setup. Debe ser true para que auto-setup finalice.",
}

EXCLUDED_ENV_KEYS = {"TZ", "WG_PRIVATE_KEY", "ProtonCountries"}
_SETTINGS_BY_KEY = {s["key"]: s for s in SETTINGS}


def _normalize_public_base_urls(value):
    if not value:
        return ""
    raw_items = [item.strip() for item in re.split(r"[\s,]+", str(value)) if item.strip()]
    normalized = []
    seen = set()
    for item in raw_items:
        candidate = item if "://" in item else f"https://{item}"
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("PUBLIC_BASE_URLS debe contener dominios u origins http/https validos")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("PUBLIC_BASE_URLS solo admite origins, sin path, query ni fragment")
        origin = f"{parsed.scheme}://{parsed.netloc.lower()}".rstrip("/")
        if origin not in seen:
            seen.add(origin)
            normalized.append(origin)
    return "\n".join(normalized)


def _ensure_environment_init():
    global _initialized
    if _initialized:
        return
    _ensure_init()
    with _init_lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS environment_settings (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized = True
        log_event("info", "environment_schema_ready", COMPONENT)


def _clear_cache():
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


def _load_values():
    global _cache, _cache_ts
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and now - _cache_ts < _CACHE_TTL_S:
            return dict(_cache)

    _ensure_environment_init()
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, value FROM environment_settings").fetchall()
    finally:
        conn.close()
    values = {row["key"]: row["value"] for row in rows}

    with _cache_lock:
        _cache = dict(values)
        _cache_ts = now
    return values


def get_raw(key):
    spec = _SETTINGS_BY_KEY.get(key)
    if spec is None:
        return os.environ.get(key, "")
    values = _load_values()
    if key in values:
        return values[key]
    env_value = os.environ.get(key)
    if env_value is not None:
        return env_value
    return spec["default"]


def get_str(key):
    return str(get_raw(key))


def get_int(key):
    spec = _SETTINGS_BY_KEY.get(key, {})
    try:
        return int(str(get_raw(key)).strip())
    except (TypeError, ValueError):
        try:
            return int(spec.get("default", 0))
        except (TypeError, ValueError):
            return 0


def get_float(key):
    spec = _SETTINGS_BY_KEY.get(key, {})
    try:
        return float(str(get_raw(key)).strip())
    except (TypeError, ValueError):
        try:
            return float(spec.get("default", 0))
        except (TypeError, ValueError):
            return 0.0


def get_bool(key):
    value = str(get_raw(key)).strip().lower()
    return value in ("1", "true", "yes", "on")


def _normalize_value(spec, value):
    if spec["key"] == "PUBLIC_BASE_URL":
        return _normalize_public_base_urls(value)

    kind = spec["type"]
    if value is None:
        value = ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    else:
        value = str(value).strip()

    if kind == "bool":
        lowered = value.lower()
        if lowered in ("1", "true", "yes", "on"):
            return "true"
        if lowered in ("0", "false", "no", "off"):
            return "false"
        raise ValueError(f"{spec['key']} debe ser booleano")

    if kind in ("int", "float"):
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec['key']} debe ser numerico")
        if spec.get("min") is not None and number < spec["min"]:
            raise ValueError(f"{spec['key']} debe ser >= {spec['min']}")
        if spec.get("max") is not None and number > spec["max"]:
            raise ValueError(f"{spec['key']} debe ser <= {spec['max']}")
        return str(number)

    choices = spec.get("choices")
    if choices and value not in choices:
        raise ValueError(f"{spec['key']} debe ser uno de: {', '.join(choices)}")
    return value


def list_settings():
    values = _load_values()
    out = []
    for spec in SETTINGS:
        key = spec["key"]
        stored = key in values
        effective = values.get(key)
        source = "stored" if stored else "default"
        if effective is None:
            env_value = os.environ.get(key)
            if env_value is not None:
                effective = env_value
                source = "environment"
            else:
                effective = spec["default"]
        item = dict(spec)
        item["help"] = item.get("help") or _HELP_TEXTS.get(key, "")
        item["source"] = source
        item["configured"] = bool(stored or (spec.get("secret") and effective))
        item["value"] = "" if spec.get("secret") else effective
        out.append(item)
    return out


def update_settings(data):
    if not isinstance(data, dict):
        raise ValueError("Payload invalido")
    now = datetime.now(timezone.utc).isoformat()
    changed = []
    with _lock:
        conn = _connect()
        try:
            for key, value in data.items():
                spec = _SETTINGS_BY_KEY.get(key)
                if spec is None:
                    raise ValueError(f"Setting desconocido: {key}")
                if spec.get("secret") and str(value or "") == "":
                    continue
                normalized = _normalize_value(spec, value)
                conn.execute(
                    """
                    INSERT INTO environment_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, normalized, now),
                )
                changed.append(key)
            conn.commit()
        finally:
            conn.close()
    _clear_cache()
    if changed:
        log_event("info", "environment_updated", COMPONENT, keys=changed)
    return changed


def reset_setting(key):
    if key not in _SETTINGS_BY_KEY:
        raise ValueError(f"Setting desconocido: {key}")
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM environment_settings WHERE key = ?", (key,))
            conn.commit()
        finally:
            conn.close()
    _clear_cache()


def defaults():
    return {spec["key"]: spec["default"] for spec in SETTINGS}
