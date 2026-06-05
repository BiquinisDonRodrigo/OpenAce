import time
import requests
from requests.adapters import HTTPAdapter

from app.utils.logging_utils import log_event

COMPONENT = "upstream"
_MAX_BACKOFF = 8.0

session = requests.Session()
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
session.mount("http://", _adapter)
session.mount("https://", _adapter)


def open_upstream(
    url,
    *,
    retries=3,
    backoff=0.5,
    connect_timeout=5,
    read_timeout=30,
    retriable_statuses=(502, 503, 504),
    component=COMPONENT,
    log_context=None,
):
    log_context = log_context or {}
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            # The AceStream engine is a basic BaseHTTPServer that hangs when a
            # streaming response is served over a reused keep-alive socket. Force a
            # fresh connection so the pooled session never hands us a stale one.
            r = session.get(url, stream=True, timeout=(connect_timeout, read_timeout),
                            headers={"Connection": "close"})
            if r.status_code in retriable_statuses:
                log_event("warning", "upstream_retriable_status", component,
                          url=url, attempt=attempt, status_code=r.status_code, **log_context)
                r.close()
                last_error = f"status {r.status_code}"
            else:
                return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = str(e)
            log_event("warning", "upstream_attempt_failed", component,
                      url=url, attempt=attempt, error=last_error, **log_context)

        if attempt < retries:
            time.sleep(min(backoff * (2 ** (attempt - 1)), _MAX_BACKOFF))

    log_event("error", "upstream_unavailable", component,
              url=url, attempts=retries, last_error=last_error, **log_context)
    return None


def stream_to_client(response, chunk_size=65536, on_chunk=None, on_close=None,
                     component=COMPONENT, log_context=None):
    log_context = log_context or {}

    def generator():
        try:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                yield chunk
                if on_chunk is not None:
                    on_chunk(len(chunk))
        except requests.exceptions.ReadTimeout:
            log_event("error", "upstream_read_timeout", component, **log_context)
        except GeneratorExit:
            raise
        except Exception as e:
            log_event("error", "upstream_stream_error", component, error=str(e), **log_context)
        finally:
            response.close()
            if on_close is not None:
                try:
                    on_close()
                except Exception as e:
                    log_event("warning", "upstream_on_close_error", component, error=str(e), **log_context)
            log_event("info", "upstream_stream_closed", component, **log_context)

    return generator()
