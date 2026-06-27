import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger

LOG_DIR = "/var/log/openace"
LOG_FILE = os.path.join(LOG_DIR, "proxy.log")

_TOKEN_RE = re.compile(r'([\?&]token=)[^\s&"\']+')


class _RedactTokenFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, dict):
            if not any(
                isinstance(v, (str, bytes)) and "token=" in str(v)
                for v in record.msg.values()
            ):
                return True
        msg = record.getMessage()
        if "token=" in msg:
            record.msg = _TOKEN_RE.sub(r'\1[REDACTED]', msg)
            record.args = None
        return True

try:
    os.makedirs(LOG_DIR, exist_ok=True)
except OSError:
    LOG_DIR = "/tmp/openace-logs"
    LOG_FILE = os.path.join(LOG_DIR, "proxy.log")
    os.makedirs(LOG_DIR, exist_ok=True)


class OpenAceJsonFormatter(jsonlogger.JsonFormatter):
    """Guarantee timestamp/level/component/event on every line.

    log_event() supplies these via a dict message. Third-party records
    (Werkzeug, urllib3, gevent) have no such dict, so without this their
    level/timestamp would serialize as null and slip past severity-based
    log filters. Backfill them from the LogRecord's own metadata.
    """

    def add_fields(self, log_data, record, message_dict):
        super().add_fields(log_data, record, message_dict)
        if not log_data.get("timestamp"):
            log_data["timestamp"] = datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat()
        if not log_data.get("level"):
            log_data["level"] = record.levelname
        if not log_data.get("component"):
            log_data["component"] = record.name
        if not log_data.get("event"):
            log_data["event"] = ""


def configure_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    log_format = '%(timestamp)s %(level)s %(component)s %(event)s %(message)s'
    json_formatter = OpenAceJsonFormatter(log_format)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(json_formatter)

    redact_filter = _RedactTokenFilter()
    stream_handler.addFilter(redact_filter)

    logger.handlers = []
    logger.addHandler(stream_handler)
