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
import stat
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
        kind = type(item)
        if kind in (str, bytes, int, float, bool, type(None)):
            continue
        if kind is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
            continue
        if kind in (list, tuple, set, frozenset):
            pending.extend(item)
            continue
        storage = getattr(kind, '_projection_storage', None)
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


_INPUT_READS = ContextVar("pass_input_generations", default=None)
_INPUT_HOOK_INSTALLED = False


def observe_input(path, info=None, *, link=False):
    """Include a passive parser's stat-only/cache-hit dependency in its read set.

    The five-field generation is the same one used by the passive parser caches.
    Missing files are inputs too: their creation must invalidate an empty result.
    This records dependencies, never file contents or process/recipient authority.
    """
    reads = _INPUT_READS.get()
    if reads is not None:
        reads.observe(path, info, link=link)


def observe_clock(key, boundary, elapsed_min=0, now=None):
    """Record a parser's clock comparison without treating every tick as input."""
    reads = _INPUT_READS.get()
    if reads is not None:
        now = time.time() if now is None else now
        value = now - elapsed_min * 60 <= boundary
        if (key, boundary) in reads.clocks and reads.clocks[(key, boundary)] != value:
            reads.complete = False
            return
        if (key, boundary) not in reads.clocks:
            reads.bytes += retained_size((key, boundary)) + 64
            if reads.bytes > reads.max_bytes:
                reads.complete = False
                reads.paths.clear()
                reads.clocks.clear()
                return
        reads.clocks[(key, boundary)] = value


def observe_generation(key, generation, read):
    """Carry an existing collector's complete, domain-specific generation key."""
    reads = _INPUT_READS.get()
    if reads is None or not reads.complete:
        return
    if key in reads.providers and reads.providers[key][0] != generation:
        reads.complete = False
        return
    if key not in reads.providers:
        reads.bytes += retained_size((key, generation)) + 96
    if reads.bytes > reads.max_bytes:
        reads.complete = False
        reads.providers.clear()
        return
    reads.providers[key] = generation, read


def invalidate_inputs():
    reads = _INPUT_READS.get()
    if reads is not None:
        reads.complete = False


def passive_input(key, read):
    """Fresh immutable external display input, shared only in a passive scope.

    Outside Fleet's explicit input scope the original authoritative reader
    always runs. Reuse checks call it freshly as well; no subprocess result is
    inferred from a guessed list of filesystem dependencies.
    """
    reads = _INPUT_READS.get()
    if reads is None:
        return read()
    key = ('external-display', key)
    if key in reads.providers:
        return reads.providers[key][0]
    with untracked_inputs():
        value = read()
    observe_generation(key, value, read)
    return value


def input_stat(path, *, follow_symlinks=True):
    """A normal stat that also observes parser cache hits in an explicit scope."""
    try:
        info = os.stat(path, follow_symlinks=follow_symlinks)
    except OSError:
        observe_input(path)
        raise
    observe_input(path, info, link=not follow_symlinks)
    return info


def input_path_call(path, method, *args, **kwargs):
    """Keep Path/DirEntry semantics, including stat-only and negative reads."""
    observe_input(path)
    value = getattr(path, method)(*args, **kwargs)
    if method in ('stat', 'lstat'):
        observe_input(path, value,
                      link=method == 'lstat' or kwargs.get('follow_symlinks') is False)
    return value


@contextmanager
def untracked_inputs():
    """Fresh external observations do not become reusable filesystem evidence."""
    token = _INPUT_READS.set(None)
    try:
        yield
    finally:
        _INPUT_READS.reset(token)


def _input_audit(event, args):
    reads = _INPUT_READS.get()
    if reads is None:
        return
    if event in ("open", "os.listdir", "os.scandir"):
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            reads.observe(args[0])
    elif event == "sqlite3.connect":
        if args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            if path.startswith("file:"):
                from urllib.parse import unquote
                path = unquote(path[5:].split("?", 1)[0])
            for suffix in ("", "-wal", "-journal"):
                reads.observe(path + suffix)
    elif event in ("subprocess.Popen", "socket.connect"):
        # External results need a domain observer; an incomplete read set never
        # authorizes reuse. The caller can exclude its separately fresh census.
        reads.complete = False


class InputGenerations:
    """One bounded passive read set, checked again before reuse.

    Directory generations detect discovery/removal; individual parser generations
    detect edits to existing files, including cache hits. Concurrent changes,
    unavailable inputs and an oversized set all fall back to the real reader.
    No snapshots, raw bytes, native census or RPC results are retained here.
    """

    def __init__(self, max_bytes=8 * 1024 * 1024):
        self.paths = {}
        self.max_bytes = max_bytes
        self.bytes = 0
        self.complete = True
        self.clocks = {}
        self.providers = {}

    @staticmethod
    def _generation(path, info=None, *, link_info=False):
        try:
            link = os.lstat(path)
            target = link
            if stat.S_ISLNK(link.st_mode):
                try:
                    target = os.stat(path)
                except FileNotFoundError:
                    target = None
            def stamp(value):
                return (value.st_dev, value.st_ino, value.st_size,
                        value.st_mtime_ns, value.st_ctime_ns)
            if info is not None and (target is None and not link_info
                                     or stamp(info) != stamp(link if link_info else target)):
                raise OSError('input-changed-during-stat')
            return (stamp(link), link.st_mode,
                    ('missing-target',) if target is None else
                    stamp(target) if target is not link else None)
        except FileNotFoundError:
            return None
        except OSError:
            raise

    def observe(self, path, info=None, *, link=False):
        if not self.complete:
            return
        token = _INPUT_READS.set(None)
        try:
            path = os.path.abspath(os.fsdecode(path))
            if path == "/proc" or path.startswith(("/proc/", "/sys/", "/dev/")):
                self.complete = False
                return
            if path in self.paths:
                if info is not None:
                    previous = self.paths[path]
                    stamp = (info.st_dev, info.st_ino, info.st_size,
                             info.st_mtime_ns, info.st_ctime_ns)
                    observed = previous[0] if previous is not None and link else (
                        previous[2] or previous[0]) if previous is not None else None
                    if observed != stamp:
                        self.complete = False
                return
            generation = self._generation(path, info, link_info=link)
            self.bytes += retained_size((path, generation)) + 48
            if self.bytes > self.max_bytes:
                self.complete = False
                self.paths.clear()
                return
            self.paths[path] = generation
        except (OSError, ValueError, TypeError):
            self.complete = False
        finally:
            _INPUT_READS.reset(token)

    def current(self):
        self.changed_path = None
        if not self.complete:
            return False
        try:
            for path, generation in self.paths.items():
                if self._generation(path) != generation:
                    self.changed_path = path
                    return False
            with untracked_inputs():
                for generation, read in self.providers.values():
                    if read() != generation:
                        return False
            return True
        except Exception:
            return False

    @contextmanager
    def capture(self):
        global _INPUT_HOOK_INSTALLED
        if not _INPUT_HOOK_INSTALLED:
            sys.addaudithook(_input_audit)
            _INPUT_HOOK_INSTALLED = True
        token = _INPUT_READS.set(self)
        try:
            yield self
        finally:
            _INPUT_READS.reset(token)


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
