import threading
import time

from app.utils import check_store
from app.utils.acestream import CHECK_TIMEOUT_S, check_stream
from app.utils.logging_utils import log_event

COMPONENT = "check_runner"

DELAY_BETWEEN_S = 0.5  # safety pause between channels so we never hammer the engine

# Single serialization point for *every* engine probe. The bulk loop and the
# manual single-channel check both acquire this, guaranteeing the engine is
# never asked to resolve two infohashes at once.
_engine_lock = threading.Lock()

_COUNTER_KEYS = ("live", "dead", "timeout", "error", "skipped")


class CheckRunner:
    """Runs a list of channels through ``check_stream`` strictly one at a time.

    A single daemon thread owns the loop; all shared state is read/written under
    ``_lock`` so the ``/check/status`` endpoint can poll a consistent snapshot
    while the run is in progress.
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
        """Begin a sequential run. Returns False if one is already in flight."""
        with self._lock:
            if self.running:
                return False
            self._reset()
            self.running = True
            self.targets = list(targets)
            self.started_at = time.time()
        log_event("info", "check_run_started", COMPONENT, total=len(self.targets))
        threading.Thread(target=self._run, args=(engine_url,),
                         name="check-runner", daemon=True).start()
        return True

    def stop(self):
        with self._lock:
            if self.running:
                self.stop_flag = True

    def _run(self, engine_url):
        try:
            for i, channel in enumerate(self.targets):
                with self._lock:
                    if self.stop_flag:
                        remaining = len(self.targets) - i
                        self.counters["skipped"] += remaining
                        break
                    self.index = i
                    self.current = channel

                infohash = channel["infohash"]
                log_context = {"content_id": infohash, "check": True}
                with _engine_lock:
                    result = check_stream(engine_url, infohash, timeout=CHECK_TIMEOUT_S,
                                          component=COMPONENT, log_context=log_context)

                check_store.record_result(
                    infohash, result["outcome"], result["response_ms"],
                    result["peers"], result["speed"],
                    name=channel.get("name"), group=channel.get("group"),
                    plugin=channel.get("plugin"),
                )
                with self._lock:
                    self.counters[result["outcome"]] = self.counters.get(result["outcome"], 0) + 1

                time.sleep(DELAY_BETWEEN_S)
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
