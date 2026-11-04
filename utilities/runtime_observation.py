#!/usr/bin/env python3
"""Bounded reuse of small immutable projections over stable file generations.

Callers provide the complete file dependency set and a loader that performs
the real domain verification. Only immutable, bounded result projections are
retained; claims, recipients, process liveness, and transition authority do not
belong in this cache.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
import threading
import time
from typing import Callable, Hashable, Iterable, TypeVar
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import functools
import sys
from collections.abc import Mapping


T = TypeVar("T")


def retained_size(value, limit=None):
    """Size one owned built-in projection graph, counting shared children once.

    Functions/modules referenced as policy identities do not own their globals.
    The traversal is bounded too: an oversized candidate stops at the limit.
    """
    seen, pending, total = set(), [value], 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if limit is not None and total > limit:
            return total
        storage = getattr(type(item), '_projection_storage', None)
        if isinstance(storage, property):
            # The accessor can return a temporary tuple of stored children.
            # Its container is not retained; only those real children are.
            pending.extend(storage.__get__(item, type(item)))
        elif isinstance(item, Mapping):
            pending.extend(item.keys())
            pending.extend(item.values())
            attributes = getattr(item, '__dict__', None)
            if attributes:
                pending.append(attributes)
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
            attributes = getattr(item, '__dict__', None)
            if attributes:
                pending.append(attributes)
    return total


class ProjectionCache(OrderedDict):
    """LRU ownership with entry and byte charges (exact or schema-derived)."""

    def __init__(self, max_entries=32, max_bytes=512 * 1024 * 1024):
        super().__init__()
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.weights, self.bytes = {}, 0
        self.admissions = self.evictions = self.rejections = 0

    def __setitem__(self, key, value):
        self.remember(key, value)

    def remember(self, key, value, *, weight=None):
        """Admit an exact graph weight or a caller's schema-derived charge."""
        self.pop(key, None)
        if weight is None:
            weight = retained_size((key, value), self.max_bytes)
        if weight > self.max_bytes:
            self.rejections += 1
            return
        super().__setitem__(key, value)
        self.weights[key] = weight
        self.bytes += weight
        self.admissions += 1
        while len(self) > self.max_entries or self.bytes > self.max_bytes:
            self.popitem(last=False)

    def pop(self, key, default=None):
        self.bytes -= self.weights.pop(key, 0)
        return super().pop(key, default)

    def __delitem__(self, key):
        super().__delitem__(key)
        self.bytes -= self.weights.pop(key)

    def popitem(self, last=True):
        key, value = super().popitem(last=last)
        self.bytes -= self.weights.pop(key)
        self.evictions += 1
        return key, value

    def clear(self):
        super().clear()
        self.weights.clear()
        self.bytes = 0


_PASS_READS = ContextVar("pass_observations", default=None)
_PASS_READ_BYTES = 768 * 1024


def observation_scope_active():
    return _PASS_READS.get() is not None


@contextmanager
def observation_read_scope():
    """Share fresh reads only within this pass, never across control operations."""
    if _PASS_READS.get() is not None:
        yield
        return
    token = _PASS_READS.set([OrderedDict(), 0])
    try:
        yield
    finally:
        _PASS_READS.reset(token)


def observe_once(reader=None, *, immutable=False):
    """Bound repeated passive reads to 4096 entries / 768 KiB per pass.

    Outside an explicit scope this is the original fresh reader. Exceptions
    remain fresh, and mutable results are never shared with another caller.
    """
    if reader is None:
        return lambda target: observe_once(target, immutable=immutable)
    clone = (lambda value: value) if immutable else copy.deepcopy
    @functools.wraps(reader)
    def read(*args, **kwargs):
        scope = _PASS_READS.get()
        if scope is None:
            return reader(*args, **kwargs)
        try:
            uid = os.geteuid() if hasattr(os, 'geteuid') else None
        except OSError:
            return reader(*args, **kwargs)
        key = (reader, uid, args, tuple(sorted(kwargs.items())))
        try:
            hash(key)
        except TypeError:
            return reader(*args, **kwargs)
        entries = scope[0]
        if key in entries:
            value, _weight = entries[key]
            entries.move_to_end(key)
            return clone(value)
        value = reader(*args, **kwargs)
        weight = retained_size((key, value), _PASS_READ_BYTES) + 128
        if weight <= _PASS_READ_BYTES:
            entries[key] = (clone(value), weight)
            scope[1] += weight
            while len(entries) > 4096 or scope[1] > _PASS_READ_BYTES:
                _key, (_value, removed) = entries.popitem(last=False)
                scope[1] -= removed
        return value
    return read


@dataclass(frozen=True)
class FileGeneration:
    path_identity: str
    resolved_path: str
    signature: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _Entry:
    generations: tuple[FileGeneration, ...]
    value: object
    weight: int
    expires_at: float | None


def file_generation(path: str | os.PathLike[str]) -> FileGeneration:
    """Return a generation that detects replacement and same-size rewrites."""
    candidate = Path(path).expanduser()
    identity = str(candidate.absolute())
    resolved = str(candidate.resolve(strict=True))
    info = candidate.stat()
    return FileGeneration(
        identity,
        resolved,
        (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
    )


def stable_read_bytes(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024,
                      retries: int = 3) -> bytes:
    """Read one exact regular-file generation or raise without returning old bytes."""
    candidate = Path(path).expanduser()
    identity = str(candidate.absolute())
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    last_error = None
    for _attempt in range(max(1, retries)):
        fd = None
        try:
            before = file_generation(candidate)
            fd = os.open(candidate, flags)
            fd_before = os.fstat(fd)
            if (fd_before.st_dev, fd_before.st_ino, fd_before.st_size,
                    fd_before.st_mtime_ns, fd_before.st_ctime_ns) != before.signature:
                raise OSError("observation-file-changed-before-read")
            chunks = []
            size = 0
            while True:
                chunk = os.read(fd, chunk_size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            fd_after = os.fstat(fd)
            after = file_generation(candidate)
            signature = (fd_after.st_dev, fd_after.st_ino, fd_after.st_size,
                         fd_after.st_mtime_ns, fd_after.st_ctime_ns)
            if (before != after or before.signature != signature
                    or size != before.signature[2]):
                raise OSError("observation-file-changed-during-read")
            return b"".join(chunks)
        except OSError as exc:
            last_error = exc
        finally:
            if fd is not None:
                os.close(fd)
    raise OSError(getattr(last_error, "errno", None),
                  f"could not read stable observation file: {identity}") from last_error


class ObservationContext:
    """Per-observer LRU for immutable projections tied to exact file generations."""

    def __init__(self, *, max_entries: int = 128, max_weight: int = 4 * 1024 * 1024):
        if max_entries < 1 or max_weight < 1:
            raise ValueError("observation-cache-bounds-invalid")
        self.max_entries = max_entries
        self.max_weight = max_weight
        self._entries: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._states: OrderedDict[Hashable, tuple[object, float | None]] = OrderedDict()
        self._weight = 0
        self._lock = threading.RLock()
        self._metrics = {"hits": 0, "misses": 0, "invalidations": 0,
                         "loads": 0, "evictions": 0,
                         "state_hits": 0, "state_misses": 0, "state_evictions": 0}

    @staticmethod
    def _generations(paths: Iterable[str | os.PathLike[str]]) -> tuple[FileGeneration, ...]:
        return tuple(file_generation(path) for path in paths)

    def memoize(
        self,
        key: Hashable,
        dependencies: Iterable[str | os.PathLike[str]],
        loader: Callable[[], T],
        *,
        weight: int | Callable[[T], int] = 1,
        ttl: float | None = None,
    ) -> T:
        """Load a small immutable projection once per exact dependency generation.

        A failed stat or loader is never cached. Generations are checked both
        before and after the loader, so a concurrent replacement cannot publish
        a result under the old snapshot. ``weight`` is a caller-estimated bound
        on the retained projection.
        """
        if isinstance(weight, int) and (weight < 1 or weight > self.max_weight):
            raise ValueError("observation-projection-weight-invalid")
        paths = tuple(Path(path) for path in dependencies)
        try:
            before = self._generations(paths)
        except OSError:
            # A missing/unreadable dependency invalidates the old projection;
            # it must never survive a failed observation and become stale
            # success when this key is requested again.
            with self._lock:
                if key in self._entries:
                    self._drop(key)
                    self._metrics["invalidations"] += 1
            raise
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                if (entry.generations == before
                        and (entry.expires_at is None or now < entry.expires_at)):
                    self._entries.move_to_end(key)
                    self._metrics["hits"] += 1
                    return entry.value  # type: ignore[return-value]
                self._drop(key)
                self._metrics["invalidations"] += 1
            self._metrics["misses"] += 1
            self._metrics["loads"] += 1

        value = loader()
        after = self._generations(paths)
        if before != after:
            with self._lock:
                self._drop(key)
                self._metrics["invalidations"] += 1
            raise OSError("observation-dependency-changed-during-read")

        retained_weight = weight(value) if callable(weight) else weight
        if retained_weight < 1 or retained_weight > self.max_weight:
            return value

        expires_at = None if ttl is None else time.monotonic() + max(0.0, ttl)
        with self._lock:
            self._drop(key)
            self._entries[key] = _Entry(after, value, retained_weight, expires_at)
            self._weight += retained_weight
            while (len(self._entries) > self.max_entries
                   or self._weight > self.max_weight):
                oldest = next(iter(self._entries))
                self._drop(oldest)
                self._metrics["evictions"] += 1
        return value

    def discard(self, key: Hashable) -> None:
        with self._lock:
            self._drop(key)

    def state(self, key: Hashable, factory: Callable[[], T], *, ttl: float = 3600.0) -> T:
        """Return bounded in-memory observer bookkeeping, never cached authority.

        This is for retry deadlines and diagnostic cadence across short
        checkpoints. Callers must re-observe all file/process/recipient
        authority before acting on the work.
        """
        now = time.monotonic()
        with self._lock:
            entry = self._states.get(key)
            if entry is not None and (entry[1] is None or now < entry[1]):
                self._states.move_to_end(key)
                self._metrics["state_hits"] += 1
                return entry[0]  # type: ignore[return-value]
            if entry is not None:
                self._states.pop(key, None)
            self._metrics["state_misses"] += 1
            value = factory()
            expires_at = None if ttl <= 0 else now + ttl
            self._states[key] = (value, expires_at)
            while len(self._states) > self.max_entries:
                self._states.popitem(last=False)
                self._metrics["state_evictions"] += 1
            return value

    def _drop(self, key: Hashable) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._weight -= entry.weight

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {**self._metrics, "entries": len(self._entries),
                    "weight": self._weight}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._states.clear()
            self._weight = 0
