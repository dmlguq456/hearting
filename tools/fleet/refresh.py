"""Non-blocking live refresh primitives for Fleet.

The curses thread must never execute a collector. ``RefreshPump`` owns one
daemon worker at a time, coalesces any number of refresh requests into at most
one follow-up run, and publishes only complete successful results.
"""

from dataclasses import dataclass, field
from concurrent.futures import Future
import os
import threading
import time


def background_read(producer):
    """Overlap one independent read; the caller still owns result publication.

    Daemon lifetime matches RefreshPump: a stalled read must not keep a closed
    TUI alive. Each snapshot awaits its read before another tick can start.
    Thread exhaustion is an unconfirmed read, never a synchronous wait that
    defeats the caller's publication deadline.
    """
    result = Future()

    def collect():
        if not result.set_running_or_notify_cancel():
            return
        try:
            result.set_result(producer())
        except BaseException as exc:
            result.set_exception(exc)

    try:
        threading.Thread(target=collect, daemon=True, name="fleet-read").start()
    except (RuntimeError, OSError) as exc:
        result.set_exception(exc)
    return result


@dataclass
class LiveSnapshot:
    sessions: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    usage_snapshots: dict = field(default_factory=dict)
    malformed: int = 0
    resource_diagnostics: list = field(default_factory=list)
    node_evidence: dict = field(default_factory=dict)
    degradations: dict = field(default_factory=dict)
    route_entities: list = field(default_factory=list)
    observations: dict = field(default_factory=dict)
    pending_delivery: object = None
    tag_metadata: list = field(default_factory=list)
    display_snapshot: object = None
    memory: object = None
    governor: object = None
    hearting: dict = None
    answer_waits: object = None

    def __iter__(self):
        # Keep the existing sessions, jobs unpacking surface for CLI readers.
        return iter((self.sessions, self.jobs))


def as_snapshot(value):
    """Legacy/test collectors may still return just their two row lists."""
    if isinstance(value, LiveSnapshot):
        snapshot = value
    else:
        sessions, jobs = value
        snapshot = LiveSnapshot(sessions=list(sessions), jobs=list(jobs))
    if snapshot.display_snapshot is None:
        try:
            from .display_snapshot import build
            snapshot.display_snapshot = build(snapshot.sessions, snapshot.jobs, snapshot.resources)
        except Exception:
            pass
    return snapshot


def fleet_answer_wait_view(result):
    """Keep answer-wait identity and timing in Fleet, without private text."""
    if not isinstance(result, dict):
        return None
    items = []
    for item in result.get("items", ()) or ():
        if not isinstance(item, dict):
            continue
        allowed = ("id", "source", "kind", "route_id", "node", "attempt_id", "since", "stale",
                   "session_id", "harness", "pane_id")
        row = {key: item[key] for key in allowed if key in item}
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        for key in ("harness", "session_id"):
            if key not in row and key in owner:
                row[key] = owner[key]
        if isinstance(row.get("stale"), bool):
            row["stale"] = "older" if row["stale"] else "current"
        items.append(row)
        if "session_id" in row or "harness" in row:
            items[-1]["owner"] = {key: row[key] for key in ("harness", "session_id")
                                   if key in row}
    health = []
    for item in result.get("health", ()) or ():
        if isinstance(item, dict):
            health.append({key: item[key] for key in ("source", "state") if key in item})
    return {
        "items": items,
        "by_session": dict(result.get("by_session", {}) or {}),
        "unattributed": list(result.get("unattributed", ()) or ()),
        "total": int(result.get("total", 0) or 0),
        "older": int(result.get("older", 0) or 0),
        "health": health,
        "observed_at": result.get("observed_at"),
        "complete": bool(result.get("complete", False)),
    }


# Measured normal tick = 10-12s (attach_projections 5.64s + dispatch.collect
# 4.33s). At the default 2s interval, interval*6 == 12s would be
# indistinguishable from a normal tick, so a floor must dominate. 90s is
# ~8x the measured normal-tick ceiling: it absorbs a transient NFS stall
# while still landing well before a user perceives Fleet as stuck.
#
# The measurement above is for the snapshot pump only. The compute-host pump
# (remote GPU host queries) borrows the same 90s floor as a POLICY CHOICE, not
# a measurement -- its producer's normal duration was not profiled in this
# cycle. This threshold controls the collection-delay label only. A slow live
# producer remains responsible for returning its result; another scan cannot
# repair blocked NFS IO and would multiply contention. See
# FLEET_REFRESH_STALL_AFTER to override either pump's display threshold.
DEFAULT_STALL_FLOOR = 90.0
DEFAULT_STALL_MULTIPLIER = 6
MIN_STALL_AFTER = 5.0


class RefreshWorkerDied(RuntimeError):
    """The worker thread exited without the pump observing completion."""


def _resolve_stall_after(interval):
    override = os.environ.get("FLEET_REFRESH_STALL_AFTER")
    if override:
        try:
            return max(MIN_STALL_AFTER, float(override))
        except ValueError:
            pass
    return max(DEFAULT_STALL_FLOOR, interval * DEFAULT_STALL_MULTIPLIER)


class RefreshPump:
    """Run an arbitrary producer off-thread with last-good atomic handoff."""

    def __init__(self, producer, interval, clock=time.monotonic,
                 thread_factory=threading.Thread, name="fleet-refresh",
                 stall_after=None):
        self._producer = producer
        self._interval = max(0.1, float(interval))
        self._clock = clock
        self._thread_factory = thread_factory
        self._name = str(name)
        self._lock = threading.RLock()
        self._thread = None
        self._running = False
        self._pending = False
        self._stopped = False
        self._next_due = self._clock()
        self._generation = 0
        self._latest = None
        self._last_error = None
        self._started_at = None
        self._last_success_at = None
        self._stall_after = (
            float(stall_after) if stall_after is not None
            else _resolve_stall_after(self._interval)
        )

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
            self._reap_locked(current)
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

    def _reap_locked(self, now):
        """Recover only a worker that actually exited without clearing state.

        Must be called with ``self._lock`` held.
        """
        if not self._running:
            return
        thread = self._thread
        if thread is None or not thread.is_alive():
            if self._last_error is None:
                self._last_error = RefreshWorkerDied(
                    "worker thread exited without clearing state")
            self._running = False
            return

    def poll(self, after_generation=0):
        """Return ``(generation, value)`` only when a newer success exists."""
        with self._lock:
            if self._generation <= after_generation:
                return None
            return self._generation, self._latest

    def health(self, now=None):
        """Single read surface for render. Do not read ``running``/``last_error``
        directly outside this method for display purposes.

        This is a pure read: it never reaps or restarts a worker. It reports
        "stalled" the moment a running worker crosses ``stall_after``, even if
        nothing has called ``request()``/``request_due()``. The producer is
        still live and retains responsibility for publishing its result.
        """
        current = self._clock() if now is None else float(now)
        with self._lock:
            last_success_at = self._last_success_at
            age = None if last_success_at is None else current - last_success_at
            stalled = (
                self._running and self._started_at is not None
                and current - self._started_at > self._stall_after
            )
            if stalled:
                state = "stalled"
            elif self._last_error is not None:
                state = "failed"
            elif self._running:
                state = "running"
            else:
                state = "idle"
            last_error = None
            if self._last_error is not None:
                last_error = "%s: %s" % (
                    type(self._last_error).__name__, str(self._last_error)[:120])
            return {
                "state": state,
                "last_success_at": last_success_at,
                "age": age,
                "last_error": last_error,
                "leaked_workers": 0,  # retained read schema; live workers are never abandoned
                "stall_after": self._stall_after,
            }

    def stop(self, join_timeout=1.0):
        """Prevent follow-ups and wait only a bounded time for the active worker."""
        with self._lock:
            self._stopped = True
            self._pending = False
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(join_timeout)))

    def _start_locked(self):
        self._started_at = self._clock()
        thread = self._thread_factory(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self):
        value = None
        error = None
        try:
            value = self._producer()
        except BaseException as exc:  # noqa: BLE001
            # Recorded, then swallowed rather than re-raised. The worker is a
            # daemon thread with no join()-ing caller on this path: a
            # re-raise would only reach threading.excepthook, which prints to
            # stderr and can corrupt the curses screen. KeyboardInterrupt and
            # SystemExit are still visible -- they land in `_last_error` and
            # surface through `health()` -- so they never disappear silently.
            error = exc

        with self._lock:
            if error is None:
                self._latest = value
                self._generation += 1
                self._last_error = None
                self._last_success_at = self._clock()
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
