"""Non-blocking live refresh primitives for Fleet.

The curses thread must never execute a collector. ``RefreshPump`` owns one
daemon worker at a time, coalesces any number of refresh requests into at most
one follow-up run, and publishes only complete successful results.
"""

from dataclasses import dataclass, field
import threading
import time


@dataclass
class LiveSnapshot:
    sessions: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    usage_snapshots: dict = field(default_factory=dict)
    malformed: int = 0
    memory: object = None
    governor: object = None
    hearting: dict = None


class RefreshPump:
    """Run an arbitrary producer off-thread with last-good atomic handoff."""

    def __init__(self, producer, interval, clock=time.monotonic, thread_factory=threading.Thread):
        self._producer = producer
        self._interval = max(0.1, float(interval))
        self._clock = clock
        self._thread_factory = thread_factory
        self._lock = threading.RLock()
        self._thread = None
        self._running = False
        self._pending = False
        self._stopped = False
        self._next_due = self._clock()
        self._generation = 0
        self._latest = None
        self._last_error = None

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def running(self):
        with self._lock:
            return self._running

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def start(self):
        return self.request(force=True)

    def request_due(self, now=None):
        return self.request(force=False, now=now)

    def request(self, force=False, now=None):
        """Schedule a run without waiting for the producer.

        Returns True only when this call starts a new worker. Requests received
        while a worker is active collapse into one pending follow-up.
        """
        current = self._clock() if now is None else float(now)
        with self._lock:
            if self._stopped:
                return False
            if not force and current < self._next_due:
                return False
            if self._running:
                # A periodic deadline that expires during a slow collection is
                # already represented by that in-flight collection. Queuing it
                # would make a producer slower than ``interval`` run forever
                # with no idle gap. Only an explicit user refresh earns one
                # coalesced follow-up.
                if force:
                    self._pending = True
                return False
            self._running = True
            self._next_due = current + self._interval
            self._start_locked()
            return True

    def poll(self, after_generation=0):
        """Return ``(generation, value)`` only when a newer success exists."""
        with self._lock:
            if self._generation <= after_generation:
                return None
            return self._generation, self._latest

    def stop(self, join_timeout=1.0):
        """Prevent follow-ups and wait only a bounded time for the active worker."""
        with self._lock:
            self._stopped = True
            self._pending = False
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(join_timeout)))

    def _start_locked(self):
        thread = self._thread_factory(
            target=self._run,
            name="fleet-refresh",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self):
        value = None
        error = None
        try:
            value = self._producer()
        except Exception as exc:  # fail-soft: the TUI keeps the prior complete snapshot
            error = exc

        with self._lock:
            if error is None:
                self._latest = value
                self._generation += 1
                self._last_error = None
            else:
                self._last_error = error
            self._running = False
            # Schedule from completion, not start. A slow producer therefore
            # gets a real cooldown instead of immediately chasing elapsed ticks.
            self._next_due = self._clock() + self._interval
            if self._pending and not self._stopped:
                self._pending = False
                self._running = True
                self._start_locked()
