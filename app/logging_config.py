import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone

from pythonjsonlogger import jsonlogger

LOG_DIR = "/var/log/openace"
LOG_FILE = os.path.join(LOG_DIR, "proxy.log")

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

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=2
    )
    file_handler.setFormatter(json_formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(json_formatter)

    logger.handlers = []
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
