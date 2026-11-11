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
_readlink = os.readlink


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
    if not text.startswith("/"):
        text = os.path.abspath(text)   # the observer's working directory does not move
    if (text in _EXCLUDED_ROOTS or text.startswith(_EXCLUDED_PREFIXES)
            or text.endswith("-shm")):
        # A SQLite ``-shm`` file is the WAL's shared-memory index; readers touch it.
        return None
    return os.path.normpath(text)


_UNSET = object()


def _at(path, dir_fd):
    """Absolute spelling of ``path`` opened relative to ``dir_fd``, or None."""
    try:
        if dir_fd is None:
            return path
        if path is None or isinstance(path, int):
            return None
        text = os.fsdecode(os.fspath(path))
        if text.startswith("/"):
            return text
        return os.path.join(_readlink("/proc/self/fd/%d" % dir_fd), text)
    except (OSError, TypeError, ValueError):
        return None


def _fd_path(fd):
    try:
        return _readlink("/proc/self/fd/%d" % fd)
    except (OSError, TypeError, ValueError):
        return None


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
        # Generation of each read input right after this pass's own last write
        # to it (or to an entry of it, for a directory). A write first checks
        # that nothing else changed the input since that point.
        self._expected = {}
        # Paths this pass writes itself. Each was checked unchanged since its
        # read when the first own write began; from then on it is this pass's
        # output, whose later content writes go through descriptors this
        # module does not see, and its final generation is adopted.
        self._own = set()
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
            self._before_write(text)
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

    def _before_write(self, text):
        for candidate in (text, os.path.dirname(text)):
            if candidate in self._own:
                continue
            recorded = self._expected.get(candidate, self.paths.get(candidate, _UNSET))
            if recorded is not _UNSET and not self._same(candidate, recorded):
                # Changed by someone else since it was read or last written
                # here; re-stamping after this write would hide that change.
                self.complete = False
        self._own.add(text)

    def _after_write(self, text):
        for candidate in (text, os.path.dirname(text)):
            if candidate in self.paths and candidate not in self._own:
                self._expected[candidate] = self._generation(candidate)

    def _restamp_written(self):
        """Adopt this pass's own writes as the new generation of the inputs they touched.

        Each own write was chained to the generation left by the previous one,
        so only a generation this pass produced itself is adopted. A later
        foreign change, or content still being written through an open handle,
        leaves the inputs changed (the next tick runs a full pass).
        """
        self.written = set()
        expected, self._expected = self._expected, {}
        for candidate, generation in expected.items():
            if self._same(candidate, generation):
                self.paths[candidate] = generation
            else:
                self.complete = False
        own, self._own = self._own, set()
        for candidate in own:
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


def _wrote(path):
    """Right after an own write returned: remember the generation it left."""
    target = _active if _active is not None else _writes
    if target is None or getattr(_guard, "busy", False):
        return
    text = _absolute(path)
    if text is not None and text not in target.ignore:
        target._after_write(text)


def _unresolved():
    """A read this module cannot attribute to a path: the pass is not provably unchanged."""
    if _active is not None:
        _active.complete = False


def _write_mode(mode, flags):
    if isinstance(mode, str) and any(char in mode for char in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _wrap_open(original):
    def open(file, mode="r", *args, **kwargs):
        if _active is None and _writes is None:
            return original(file, mode, *args, **kwargs)
        write = _write_mode(mode, None)
        _note(file, write)
        handle = original(file, mode, *args, **kwargs)
        if write:
            _wrote(file)
        return handle
    open.__wrapped__ = original
    open.__doc__ = original.__doc__
    return open


def _wrap_os_open(original):
    def open(path, flags, *args, **kwargs):
        if _active is None and _writes is None:
            return original(path, flags, *args, **kwargs)
        target = _at(path, kwargs.get("dir_fd"))
        write = _write_mode(None, flags)
        if target is None:
            if not write:
                _unresolved()
            return original(path, flags, *args, **kwargs)
        _note(target, write)
        fd = original(path, flags, *args, **kwargs)
        if write:
            _wrote(target)
        return fd
    open.__wrapped__ = original
    open.__doc__ = original.__doc__
    return open


def _wrap_listing(original):
    def listing(path=".", *args, **kwargs):
        target = _active
        if target is not None:
            spelled = _fd_path(path) if isinstance(path, int) else path
            if spelled is None:
                target.complete = False
            else:
                target._note(spelled)
        return original(path, *args, **kwargs)
    listing.__wrapped__ = original
    listing.__doc__ = original.__doc__
    return listing


def _wrap_stat(original, follows):
    def stat_path(path, *args, **kwargs):
        target = _active
        if target is None:
            return original(path, *args, **kwargs)
        if args or isinstance(path, int):
            target.complete = False   # an fd or positional form names no path here
            return original(path, *args, **kwargs)
        if kwargs.get("dir_fd") is not None:
            resolved = _at(path, kwargs["dir_fd"])
            if resolved is None:
                target.complete = False
                return original(path, *args, **kwargs)
            follow = follows and kwargs.get("follow_symlinks", True)
            try:
                meta = original(path, *args, **kwargs)
            except OSError:
                target._note(resolved, loose=True, meta=None, follow=follow)
                raise
            target._note(resolved, loose=True, meta=meta, follow=follow)
            return meta
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
        if (_active is None and _writes is None) or any(
                key.endswith("dir_fd") and value is not None for key, value in kwargs.items()):
            # An fd-relative mutation is not attributed: a read input it touches
            # stays changed, so the next tick runs a full pass.
            return original(*args, **kwargs)
        for index in positions:
            if index < len(args):
                _note(args[index], True)
        result = original(*args, **kwargs)
        for index in positions:
            if index < len(args):
                _wrote(args[index])
        return result
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
