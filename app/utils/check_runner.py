import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.utils import check_store
from app.utils.acestream import CHECK_TIMEOUT_S, check_stream
from app.utils.logging_utils import log_event

COMPONENT = "check_runner"

MAX_CONCURRENT_CHECKS = 4
DELAY_BETWEEN_S = 0.5  # safety pause per worker so we never hammer the engine

_engine_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_CHECKS)

_COUNTER_KEYS = ("live", "dead", "timeout", "error", "skipped")


class CheckRunner:
    """Runs a list of channels through ``check_stream`` with bounded concurrency.

    A single daemon thread owns the loop; up to ``MAX_CONCURRENT_CHECKS``
    worker threads probe the engine in parallel, coordinated by
    ``_engine_semaphore`` (shared with manual single checks). All shared state
    is read/written under ``_lock`` so the ``/check/status`` endpoint can poll
    a consistent snapshot while the run is in progress.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.running = False
        self.stop_flag = False
        self.targets = []
        self.index = 0
        self.current = None
        self.counters = {k: 0 for k in _COUNTER_KEYS}
        self.started_at = None
        self.finished_at = None

    def start(self, engine_url, targets):
        """Begin a run. Returns False if one is already in flight."""
        with self._lock:
            if self.running:
                return False
            self._reset()
            self.running = True
            self.targets = list(targets)
            self.started_at = time.time()
        log_event("info", "check_run_started", COMPONENT, total=len(self.targets))
        try:
            threading.Thread(target=self._run, args=(engine_url,),
                             name="check-runner", daemon=True).start()
        except RuntimeError as e:
            with self._lock:
                self._reset()
            log_event("error", "check_run_thread_failed", COMPONENT, error=str(e))
            raise
        return True

    def stop(self):
        with self._lock:
            if self.running:
                self.stop_flag = True

    def _probe(self, channel, engine_url):
        infohash = channel["infohash"]
        log_context = {"content_id": infohash, "check": True}
        with _engine_semaphore:
            result = check_stream(engine_url, infohash, timeout=CHECK_TIMEOUT_S,
                                  component=COMPONENT, log_context=log_context)
        time.sleep(DELAY_BETWEEN_S)
        check_store.record_result(
            infohash, result["outcome"], result["response_ms"],
            result["peers"], result["speed"],
            name=channel.get("name"), group=channel.get("group"),
            plugin=channel.get("plugin"),
        )
        return result["outcome"]

    def _run(self, engine_url):
        try:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHECKS) as pool:
                i = 0
                total = len(self.targets)
                while i < total:
                    with self._lock:
                        if self.stop_flag:
                            self.counters["skipped"] += total - i
                            break
                    batch = self.targets[i:i + MAX_CONCURRENT_CHECKS]
                    with self._lock:
                        self.current = batch[0] if batch else None
                    futures = {pool.submit(self._probe, ch, engine_url): ch
                               for ch in batch}
                    for fut in as_completed(futures):
                        try:
                            outcome = fut.result()
                        except Exception as e:
                            log_event("error", "check_probe_failed", COMPONENT,
                                      error=str(e))
                            outcome = "error"
                        with self._lock:
                            self.counters[outcome] = self.counters.get(outcome, 0) + 1
                    with self._lock:
                        self.index = min(i + len(batch), total)
                    i += len(batch)
        except Exception as e:
            log_event("error", "check_run_crashed", COMPONENT, error=str(e))
        finally:
            with self._lock:
                self.running = False
                self.current = None
                self.finished_at = time.time()
                final_counters = dict(self.counters)
            log_event("info", "check_run_finished", COMPONENT, **final_counters)

    def snapshot(self):
        with self._lock:
            return {
                "running": self.running,
                "total": len(self.targets),
                "index": self.index,
                "done": sum(self.counters.values()),
                "current": self.current,
                "counters": dict(self.counters),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


# Process-wide singleton (single gevent worker, so shared across all requests).
runner = CheckRunner()
