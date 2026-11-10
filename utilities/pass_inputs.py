"""Stat generations of what one read-only observation pass actually read.

A long-lived observer runs the same full pass on a timer. When nothing that
pass read has changed, a second pass would only rebuild the same result. This
module records the files a pass opened, the directories it listed, the paths it
stat'ed (missing ones included, so a file that appears is a change), SQLite
databases it opened, and the PIDs it probed with signal 0. ``changed()`` later
re-stats only those inputs.

What a pass reads through sockets, subprocess output or the clock is invisible
here; the caller keeps its own bounded fallback refresh for those. Recording is
process-local, off outside ``record()``, and never changes what a call returns.
"""
from __future__ import annotations

import builtins
from contextlib import contextmanager
import io
import os
import stat as _stat
import threading

_EXCLUDED_PREFIXES = ("/proc/", "/sys/", "/dev/")
_EXCLUDED_ROOTS = frozenset(("/proc", "/sys", "/dev"))
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_LIMIT = 200_000

_lock = threading.Lock()
_guard = threading.local()
_installed = False
_active = None          # the ReadSet being recorded, or None
_writes = None          # the ReadSet whose own writes are being re-stamped, or None
_lstat = os.lstat
_stat_fn = os.stat
_io_open = io.open
_listdir = os.listdir


def _meta_signature(meta):
    return (meta.st_dev, meta.st_ino, meta.st_mode, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _signature(path, follow=False):
    """Generation of one path: lstat (plus its target for a link), or stat when the pass followed it."""
    try:
        meta = (_stat_fn if follow else _lstat)(path)
    except (OSError, ValueError):
        return None
    sig = _meta_signature(meta)
    if not follow and _stat.S_ISLNK(meta.st_mode):
        try:
            sig += _meta_signature(_stat_fn(path))
        except (OSError, ValueError):
            sig += (None,)
    return sig


def _process_start(pid):
    try:
        with _io_open("/proc/%d/stat" % pid, "rb") as handle:
            raw = handle.read()
        return raw.rsplit(b")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def entry_names(path):
    """Visible entry names of a directory, read without recording, or None."""
    try:
        return tuple(sorted(name for name in _listdir(path) if not name.startswith(".")))
    except (OSError, ValueError):
        return None


def content(path, limit):
    """Up to ``limit`` bytes of a regular file, read without recording, or None."""
    try:
        if not _stat.S_ISREG(_lstat(path).st_mode):
            return None
        with _io_open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except (OSError, ValueError):
        return None
    return raw if len(raw) <= limit else None


def _absolute(path):
    if isinstance(path, int) or path is None:
        return None
    try:
        text = os.fsdecode(os.fspath(path))
    except (TypeError, ValueError):
        return None
    if (not text.startswith("/") or text in _EXCLUDED_ROOTS or text.startswith(_EXCLUDED_PREFIXES)
            or text.endswith("-shm")):
        # A SQLite ``-shm`` file is the WAL's shared-memory index; readers touch it.
        return None
    return os.path.normpath(text)


_UNSET = object()


class ReadSet:
    """Inputs of one pass and their generations at first read.

    A directory reached only through ``stat`` (path walks, ``mkdir(exist_ok)``,
    ``realpath``) is compared by identity and type; its entry list is an input
    only when the pass listed it. ``describe(path)`` may return a caller-defined
    generation for an input whose rewrites carry no meaning for the caller.
    """

    def __init__(self, ignore=(), describe=None):
        self.ignore = frozenset(os.path.normpath(str(path)) for path in ignore)
        self.describe = describe
        self.paths = {}
        self.loose = set()      # reached only through stat
        self.follow = set()     # generation taken with stat (the pass followed a link)
        self._spellings = {}
        self.pids = {}
        self.written = set()
        self.complete = True

    def _generation(self, path, meta=_UNSET):
        if self.describe is not None:
            # A describer's own reads are not inputs of the pass.
            _guard.busy = True
            try:
                described = self.describe(path)
            except Exception:
                described = None
            finally:
                _guard.busy = False
            if described is not None:
                return described
        if meta is not _UNSET:
            return None if meta is None else _meta_signature(meta)
        return _signature(path, path in self.follow)

    def _note(self, path, *, write=False, loose=False, meta=_UNSET, follow=False):
        # Hot path (every stat/open of a pass): a seen spelling costs one lookup.
        raw = path if type(path) is str else None
        if raw is not None and not write and raw in self._spellings:
            if not loose:
                self.loose.discard(self._spellings[raw])
            return
        if getattr(_guard, "busy", False):
            return
        text = _absolute(path)
        if text is None or text in self.ignore:
            if raw is not None and not write:
                self._spellings[raw] = None
            return
        if write:
            self.written.add(text)
            return
        if raw is not None:
            self._spellings[raw] = text
        if text not in self.paths:
            if len(self.paths) >= _LIMIT:
                self.complete = False
                return
            if meta is not _UNSET and follow:
                self.follow.add(text)
            elif meta is not _UNSET and meta is not None and _stat.S_ISLNK(meta.st_mode):
                meta = _UNSET   # an lstat'ed link also records its target
            self.paths[text] = self._generation(text, meta)
            if loose:
                self.loose.add(text)
        elif not loose:
            self.loose.discard(text)

    def _note_pid(self, pid):
        if isinstance(pid, int) and pid > 0 and pid not in self.pids:
            self.pids[pid] = _process_start(pid)

    def _restamp_written(self):
        written, self.written = self.written, set()
        for text in written:
            for candidate in (text, os.path.dirname(text)):
                if candidate in self.paths:
                    self.paths[candidate] = self._generation(candidate)

    def _same(self, path, recorded):
        current = self._generation(path)
        if current == recorded:
            return True
        if (path in self.loose and isinstance(recorded, tuple) and isinstance(current, tuple)
                and len(recorded) >= 3 and len(current) >= 3
                and isinstance(recorded[2], int) and isinstance(current[2], int)
                and _stat.S_ISDIR(recorded[2]) and _stat.S_ISDIR(current[2])):
            return recorded[:3] == current[:3]
        return False

    def changed(self):
        """True when any recorded input differs from its recorded generation."""
        if not self.complete:
            return True
        for path, recorded in self.paths.items():
            if not self._same(path, recorded):
                return True
        return any(_process_start(pid) != start for pid, start in self.pids.items())


def _note(path, write):
    """A pass records reads and writes; a caller's own-write scope records writes only."""
    if _active is not None:
        _active._note(path, write=write)
    elif write and _writes is not None:
        _writes._note(path, write=True)


def _write_mode(mode, flags):
    if isinstance(mode, str) and any(char in mode for char in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _wrap_open(original):
    def open(file, mode="r", *args, **kwargs):
        if _active is not None or _writes is not None:
            _note(file, _write_mode(mode, None))
        return original(file, mode, *args, **kwargs)
    open.__wrapped__ = original
    open.__doc__ = original.__doc__
    return open


def _wrap_os_open(original):
    def open(path, flags, *args, **kwargs):
        if (_active is not None or _writes is not None) and kwargs.get("dir_fd") is None:
            _note(path, _write_mode(None, flags))
        return original(path, flags, *args, **kwargs)
    open.__wrapped__ = original
    open.__doc__ = original.__doc__
    return open


def _wrap_listing(original):
    def listing(path=".", *args, **kwargs):
        target = _active
        if target is not None:
            target._note(path)
        return original(path, *args, **kwargs)
    listing.__wrapped__ = original
    listing.__doc__ = original.__doc__
    return listing


def _wrap_stat(original, follows):
    def stat_path(path, *args, **kwargs):
        target = _active
        if target is None or args or kwargs.get("dir_fd") is not None:
            return original(path, *args, **kwargs)
        follow = follows and kwargs.get("follow_symlinks", True)
        # The pass's own stat result is the generation; no second syscall.
        try:
            meta = original(path, *args, **kwargs)
        except OSError:
            target._note(path, loose=True, meta=None, follow=follow)
            raise
        target._note(path, loose=True, meta=meta, follow=follow)
        return meta
    stat_path.__wrapped__ = original
    stat_path.__doc__ = original.__doc__
    return stat_path


def _wrap_mutation(original, *positions):
    def mutate(*args, **kwargs):
        if _active is not None or _writes is not None:
            for index in positions:
                if index < len(args):
                    _note(args[index], True)
        return original(*args, **kwargs)
    mutate.__wrapped__ = original
    mutate.__doc__ = original.__doc__
    return mutate


def _wrap_kill(original):
    def kill(pid, sig):
        target = _active
        if target is not None and sig == 0:
            target._note_pid(pid)
        return original(pid, sig)
    kill.__wrapped__ = original
    kill.__doc__ = original.__doc__
    return kill


def _wrap_sqlite(original):
    def connect(database, *args, **kwargs):
        target = _active
        if target is not None and isinstance(database, (str, bytes, os.PathLike)):
            text = os.fsdecode(os.fspath(database))
            if text.startswith("file:"):
                text = text[5:].split("?", 1)[0]
            for suffix in ("", "-wal"):
                target._note(text + suffix)
        return original(database, *args, **kwargs)
    connect.__wrapped__ = original
    connect.__doc__ = original.__doc__
    return connect


def install():
    """Wrap the process's read entry points once. Only a dedicated observer calls this."""
    global _installed
    with _lock:
        if _installed:
            return
        wrapped_open = _wrap_open(io.open)
        builtins.open = wrapped_open
        io.open = wrapped_open
        os.open = _wrap_os_open(os.open)
        os.listdir = _wrap_listing(os.listdir)
        os.scandir = _wrap_listing(os.scandir)
        os.stat = _wrap_stat(os.stat, True)
        os.lstat = _wrap_stat(os.lstat, False)
        os.kill = _wrap_kill(os.kill)
        for name, positions in (("rename", (0, 1)), ("replace", (0, 1)), ("remove", (0,)),
                                ("unlink", (0,)), ("mkdir", (0,)), ("rmdir", (0,)),
                                ("chmod", (0,)), ("utime", (0,)), ("symlink", (1,)), ("link", (1,))):
            setattr(os, name, _wrap_mutation(getattr(os, name), *positions))
        try:
            import sqlite3
            sqlite3.connect = _wrap_sqlite(sqlite3.connect)
        except ImportError:
            pass
        _installed = True


@contextmanager
def record(ignore=(), describe=None):
    """Record one pass's inputs; the pass's own writes are re-stamped at exit."""
    global _active
    read_set = ReadSet(ignore, describe)
    read_set.complete = _installed
    previous, _active = _active, read_set
    try:
        yield read_set
    finally:
        _active = previous
        read_set._restamp_written()


@contextmanager
def own_writes(read_set):
    """Re-stamp recorded inputs this caller itself rewrites (e.g. its published view)."""
    global _writes
    previous, _writes = _writes, read_set
    try:
        yield read_set
    finally:
        _writes = previous
        read_set._restamp_written()
