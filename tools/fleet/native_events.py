"""Read-only kernel notifications for the existing native display sources.

The listener blocks on descriptors, without a timer or periodic stat/proc read.
Notifications only mark a display read dirty; they establish no identity. Normal
complete collections remain the fallback when a source cannot be watched.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import selectors
import struct
import threading

from .collectors.procscan import read_proc_start

_HEADER = struct.Struct('iIII')
# modify/attrib, move/create/delete, directory loss. Close-after-write is not a
# change by itself: SQLite readers reopen a WAL read-write and close it untouched.
_MASK = 0x00000FC6
_RESET = 0x0000CC00  # self-delete/move, overflow, ignored watch


class NativeDisplayEvents:
    def __init__(self):
        self._lock = threading.Lock()
        self._dirty = threading.Event()
        self._stopped = threading.Event()
        self.ready = threading.Event()
        # True only while a kernel watch set is installed. A consumer that
        # stretches its cadence on silence must not do so without one.
        self.watching = False
        self._sources = None
        self._thread = None
        self._read = self._write = None
        controls = ()
        try:
            controls = os.pipe()
            for fd in controls:
                os.set_blocking(fd, False)
                os.set_inheritable(fd, False)
            self._read, self._write = controls
        except (AttributeError, OSError):
            for fd in controls:
                os.close(fd)
            self.ready.set()

    def configure(self, paths, patterns=(), processes=()):
        sources = (tuple(sorted(set(paths))), tuple(sorted(set(patterns))),
                   tuple(sorted(set(processes))))
        with self._lock:
            if self._stopped.is_set() or sources == self._sources:
                return
            self._sources = sources
            if self._read is None:
                return
            self.ready.clear()
            if self._thread is None:
                self._thread = threading.Thread(target=self._run,
                    name='fleet-native-events', daemon=True)
                self._thread.start()
        self._wake()

    def take_changed(self):
        changed = self._dirty.is_set()
        if changed:
            self._dirty.clear()
        return changed

    def _wake(self):
        with self._lock:
            if self._write is not None:
                try:
                    os.write(self._write, b'w')
                except OSError:
                    pass

    def stop(self, join_timeout=1.0):
        self._stopped.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(join_timeout)
        else:
            self._close_controls()

    def _close_controls(self):
        with self._lock:
            for name in ('_read', '_write'):
                fd = getattr(self, name)
                setattr(self, name, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    @staticmethod
    def _drain(fd):
        while True:
            try:
                if not os.read(fd, 65536):
                    return
            except BlockingIOError:
                return

    @staticmethod
    def _directories(paths, patterns):
        wanted = [(str(Path(path).parent), Path(path).name, False) for path in paths]
        wanted.extend((directory, prefix, True) for directory, prefix in patterns)
        result = {}
        for directory, name, prefix in wanted:
            try:
                requested = Path(directory)
                parent = requested
                while not parent.is_dir() and parent.parent != parent:
                    parent = parent.parent
                if parent != requested:
                    name, prefix = requested.relative_to(parent).parts[0], False
                result.setdefault(str(parent), set()).add((name, prefix, parent != requested))
            except (OSError, ValueError, IndexError):
                continue
        return result

    def _rebuild(self, selector, sources):
        for key in list(selector.get_map().values()):
            if key.data != 'control':
                selector.unregister(key.fd)
                os.close(key.fd)
        paths, patterns, processes = sources
        watched, event_fd = {}, None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            init = libc.inotify_init1
            init.argtypes, init.restype = (ctypes.c_int,), ctypes.c_int
            add = libc.inotify_add_watch
            add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
            add.restype = ctypes.c_int
            event_fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
            if event_fd < 0:
                raise OSError('inotify unavailable')
            selector.register(event_fd, selectors.EVENT_READ, 'files')
            for directory, names in self._directories(paths, patterns).items():
                wd = add(event_fd, os.fsencode(directory), _MASK)
                if wd >= 0:
                    watched.setdefault(wd, set()).update(names)
        except (AttributeError, OSError, ValueError):
            if event_fd is not None and event_fd >= 0:
                try:
                    selector.unregister(event_fd)
                except KeyError:
                    pass
                os.close(event_fd)
            event_fd = None
        self.watching = event_fd is not None
        for pid, birth in processes:
            fd = None
            try:
                fd = os.pidfd_open(pid)
                if read_proc_start(pid) != birth:
                    os.close(fd)
                    continue
                selector.register(fd, selectors.EVENT_READ, 'process')
            except (AttributeError, OSError, ValueError):
                if fd is not None:
                    os.close(fd)
        # A write between the preceding read and watch installation is covered
        # by one post-registration read, without a registration-gap timer.
        self._dirty.set()
        self.ready.set()
        return event_fd, watched

    @staticmethod
    def _files_changed(fd, watched):
        changed = reset = False
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                break
            if not data:
                return True, True
            offset = 0
            while offset + _HEADER.size <= len(data):
                wd, mask, _cookie, size = _HEADER.unpack_from(data, offset)
                offset += _HEADER.size
                if offset + size > len(data):
                    return True, True
                name = os.fsdecode(data[offset:offset + size].split(b'\0', 1)[0])
                offset += size
                if mask & _RESET:
                    changed = reset = True
                if not name and mask & 0x00000004:  # watched directory's ownership/mode
                    changed = True
                for wanted, prefix, missing_parent in watched.get(wd, ()):
                    if (prefix and name.startswith(wanted)) or (not prefix and name == wanted):
                        changed = True
                        reset |= missing_parent
        return changed, reset

    def _run(self):
        selector = None
        try:
            selector = selectors.DefaultSelector()
            selector.register(self._read, selectors.EVENT_READ, 'control')
            sources = None
            event_fd, watched = None, {}
            while not self._stopped.is_set():
                # No timeout: only source events, process exit or configuration
                # changes can wake this listener.
                events = selector.select()
                for key, _mask in events:
                    if self._stopped.is_set():
                        break
                    if key.data == 'control':
                        self._drain(self._read)
                        with self._lock:
                            requested = self._sources
                        if requested != sources:
                            sources = requested
                            event_fd, watched = self._rebuild(selector, sources)
                            break  # the remaining events refer to retired descriptors
                    elif key.data == 'process':
                        if key.fd in selector.get_map():
                            selector.unregister(key.fd)
                            os.close(key.fd)
                            self._dirty.set()
                    elif key.fd == event_fd:
                        changed, reset = self._files_changed(event_fd, watched)
                        if reset:
                            self.watching = False
                            self.ready.clear()
                        if changed:
                            self._dirty.set()
                        if reset:
                            event_fd, watched = self._rebuild(selector, sources)
                            break
        except (OSError, ValueError):
            self.watching = False
            # Event delivery is optional. The normal complete snapshot still
            # causes its existing display read; never substitute a new timer.
            self.ready.set()
        finally:
            self.watching = False
            if selector is not None:
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fd)
                    if key.data != 'control':
                        os.close(key.fd)
                selector.close()
            self._close_controls()


# Basic collection while its watched inputs stay silent. Inputs inotify cannot
# see (NFS writes from another host, /proc births, Herdr panes, remote hosts)
# and clock-derived fields still refresh at this cadence.
DEFAULT_IDLE_REFRESH = 30.0
MIN_IDLE_REFRESH = 2.0
_SOURCE_LIMIT = 512


def idle_refresh_interval(interval, env=None):
    """``FLEET_IDLE_REFRESH_INTERVAL`` seconds; ``0`` keeps the periodic cadence."""
    env = os.environ if env is None else env
    raw = env.get("FLEET_IDLE_REFRESH_INTERVAL")
    if raw is None or not str(raw).strip():
        return max(float(interval), DEFAULT_IDLE_REFRESH)
    try:
        value = float(raw)
    except ValueError:
        return max(float(interval), DEFAULT_IDLE_REFRESH)
    if value <= 0:
        return None
    return max(float(interval), MIN_IDLE_REFRESH, value)


class IdleCadence:
    """Gate a periodic pump on input change notifications.

    Any notification, watch reset, user input or explicit refresh marks the
    inputs dirty and restores the pump's own cadence at once. While nothing is
    dirty, one fallback run per ``idle_interval`` keeps unwatched inputs and
    clock-driven fields current. Without an installed watch set the pump keeps
    its normal cadence.
    """

    def __init__(self, idle_interval):
        self.idle_interval = None if idle_interval is None else float(idle_interval)
        self._dirty = True
        self._last_start = None

    def mark(self):
        self._dirty = True

    @property
    def dirty(self):
        return self._dirty

    def due(self, now, *, watching=True):
        return (self.idle_interval is None or not watching or self._dirty
                or self._last_start is None or now - self._last_start >= self.idle_interval)

    def started(self, now):
        self._dirty = False
        self._last_start = float(now)


def _proc_identity(pid, birth):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or birth in (None, ""):
        return None
    try:
        actual = read_proc_start(pid)
    except (OSError, ValueError):
        return None
    if actual is None or str(actual) != str(birth):
        return None
    return pid, actual


def basic_event_sources(snapshot, display_sources=None):
    """Watchable inputs of one basic collection: ``(paths, patterns, processes)``.

    Only stable, existing identities are listed so an unchanged board keeps one
    watch set. Paths are files whose parent directory is watched; patterns are
    ``(directory, prefix)`` with ``""`` meaning any entry of that directory.
    """
    paths, patterns, processes = set(), set(), set()
    if display_sources:
        try:
            shown_paths, shown_patterns, shown_processes = display_sources
            # A Claude statusline tap is rewritten about once a minute while
            # idle; the display reader owns those values, not basic collection.
            paths.update(str(path) for path in shown_paths
                         if Path(str(path)).parent.name != ".statusline")
            patterns.update(tuple(item) for item in shown_patterns)
            processes.update(tuple(item) for item in shown_processes)
        except (TypeError, ValueError):
            pass
    try:
        from . import session_registry
        for harness in ("claude", "codex", "opencode"):
            patterns.add((str(session_registry._dir_for(harness)), ""))
    except Exception:
        pass
    try:
        from .collectors import dispatch
        paths.add(str(dispatch._jobs_path()))
        paths.update(str(path) for path in dispatch._installed_registry_paths())
    except Exception:
        pass
    try:
        from .collectors import usage_cache
        paths.update(str(usage_cache._path(harness)) for harness in ("claude", "codex", "opencode"))
    except Exception:
        pass
    try:
        import human_wait_state
        paths.add(str(human_wait_state._fleet_cache_path()))
    except Exception:
        pass
    rows = []
    for name in ("sessions", "jobs", "resources"):
        rows.extend(tuple(getattr(snapshot, name, None) or ())[:_SOURCE_LIMIT])
    for row in rows:
        identity = _proc_identity(getattr(row, "pid", None),
                                  getattr(row, "proc_start", None) or getattr(row, "starttime", None))
        if identity is None:
            # A finished row's files are settled; its registry/roster changes
            # arrive through the watched registries or the fallback.
            continue
        processes.add(identity)
        for attribute in ("_transcript_path", "log_path", "registry_path"):
            value = getattr(row, attribute, None)
            if isinstance(value, (str, os.PathLike)) and str(value):
                paths.add(str(value))
    return (tuple(sorted(paths))[:_SOURCE_LIMIT], tuple(sorted(patterns))[:_SOURCE_LIMIT],
            tuple(sorted(processes))[:_SOURCE_LIMIT])
