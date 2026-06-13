import threading
import time
from queue import Queue, Empty, Full

from app.utils.acestream import negotiate_stream, stop_stream
from app.utils.logging_utils import log_event
from app.utils.upstream import open_upstream

COMPONENT = "shared_stream"
_SENTINEL = object()
GRACE_PERIOD_S = 30
CHUNK_SIZE = 8192
QUEUE_MAX = 512

_streams = {}
_lock = threading.Lock()


class _SharedStream:

    def __init__(self, content_id, command_url):
        self.content_id = content_id
        self.command_url = command_url
        self.started_at = time.time()
        self._sub_lock = threading.Lock()
        self._subscribers = []
        self._closed = False
        self._bytes = 0
        self._last_log = time.time()

    def subscribe(self):
        q = Queue(maxsize=QUEUE_MAX)
        with self._sub_lock:
            if self._closed:
                q.put(_SENTINEL)
                return q
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass
            return len(self._subscribers)

    def broadcast(self, chunk):
        with self._sub_lock:
            self._bytes += len(chunk)
            now = time.time()
            if self._bytes >= 5_242_880 or (now - self._last_log) > 60:
                log_event("info", "stream_progress", COMPONENT,
                          content_id=self.content_id,
                          bytes_mb=round(self._bytes / 1_048_576, 2),
                          clients=len(self._subscribers))
                self._bytes = 0
                self._last_log = now
            stale = []
            for q in self._subscribers:
                try:
                    q.put_nowait(chunk)
                except Full:
                    stale.append(q)
            for q in stale:
                self._subscribers.remove(q)

    def close(self):
        with self._sub_lock:
            self._closed = True
            for q in self._subscribers:
                try:
                    q.put_nowait(_SENTINEL)
                except Full:
                    pass
            self._subscribers.clear()

    @property
    def client_count(self):
        with self._sub_lock:
            return len(self._subscribers)

    @property
    def is_closed(self):
        return self._closed


def get_or_create(engine_url, content_id, *, component=COMPONENT, log_context=None):
    log_context = log_context or {}

    with _lock:
        existing = _streams.get(content_id)
        if existing and not existing.is_closed:
            q = existing.subscribe()
            log_event("info", "stream_joined", component,
                      clients=existing.client_count, **log_context)
            return existing, q

    session_info = negotiate_stream(engine_url, content_id,
                                    component=component, log_context=log_context)
    if session_info is None:
        return None, None

    r = open_upstream(
        session_info["playback_url"],
        retries=10, backoff=0.5,
        connect_timeout=5, read_timeout=60,
        retriable_statuses=(502, 503, 504),
        component=component, log_context=log_context,
    )
    if r is None:
        stop_stream(session_info["command_url"],
                    component=component, log_context=log_context)
        return None, None
    if r.status_code != 200:
        log_event("warning", "stream_unexpected_status", component,
                  status_code=r.status_code, **log_context)
        r.close()
        stop_stream(session_info["command_url"],
                    component=component, log_context=log_context)
        return None, None

    stream = _SharedStream(content_id, session_info["command_url"])

    with _lock:
        existing = _streams.get(content_id)
        if existing and not existing.is_closed:
            r.close()
            stop_stream(session_info["command_url"],
                        component=component, log_context=log_context)
            q = existing.subscribe()
            return existing, q
        _streams[content_id] = stream

    q = stream.subscribe()
    log_event("info", "stream_created", component, **log_context)

    def _reader():
        grace_start = None
        try:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                if chunk:
                    stream.broadcast(chunk)
                if stream.client_count == 0:
                    if grace_start is None:
                        grace_start = time.monotonic()
                        log_event("info", "stream_grace_started", COMPONENT,
                                  content_id=content_id)
                    elif time.monotonic() - grace_start >= GRACE_PERIOD_S:
                        log_event("info", "stream_grace_expired", COMPONENT,
                                  content_id=content_id)
                        break
                else:
                    grace_start = None
        except Exception as e:
            log_event("error", "stream_reader_error", COMPONENT,
                      content_id=content_id, error=str(e))
        finally:
            r.close()
            stream.close()
            stop_stream(stream.command_url, component=COMPONENT,
                        log_context={"content_id": content_id})
            with _lock:
                _streams.pop(content_id, None)
            log_event("info", "stream_closed", COMPONENT,
                      content_id=content_id)

    threading.Thread(target=_reader, daemon=True).start()
    return stream, q


def iterate(q, timeout=60):
    while True:
        try:
            chunk = q.get(timeout=timeout)
        except Empty:
            break
        if chunk is _SENTINEL:
            break
        yield chunk


def get_active():
    with _lock:
        return [
            {
                "content_id": cid,
                "clients": s.client_count,
                "started_at": s.started_at,
            }
            for cid, s in _streams.items()
            if not s.is_closed
        ]
