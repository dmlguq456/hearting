"""Read-only kernel notifications for the existing native display sources.

The listener blocks on descriptors, without a timer or periodic stat/proc read.
Notifications only mark a display read dirty; they establish no identity. Normal
complete collections remain the fallback when a source cannot be watched.
"""
from __future__ import annotations

import ctypes
import os
import re
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
        self._reset = threading.Event()
        self._stopped = threading.Event()
        self.ready = threading.Event()
        # True only while every requested source is watched: the source list
        # was not truncated and each directory and process watch registered.
        # A consumer that stretches its cadence on silence must not do so
        # while any source is unwatched.
        self.watching = False
        self._complete = True
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

    def configure(self, paths, patterns=(), processes=(), complete=True):
        """Watch these sources; ``complete=False`` marks a truncated list."""
        sources = (tuple(sorted(set(paths))), tuple(sorted(set(patterns))),
                   tuple(sorted(set(processes))))
        with self._lock:
            if self._stopped.is_set() or (sources == self._sources
                                          and bool(complete) == self._complete):
                return
            self._sources = sources
            self._complete = bool(complete)
            self.watching = False
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

    def take_reset(self):
        """True once after a watch set was lost or rebuilt on overflow."""
        reset = self._reset.is_set()
        if reset:
            self._reset.clear()
        return reset

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
    def _directories(paths, patterns, unresolved=None):
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
                if unresolved is not None:
                    unresolved.append(directory)
                continue
        return result

    def _rebuild(self, selector, sources):
        for key in list(selector.get_map().values()):
            if key.data != 'control':
                selector.unregister(key.fd)
                os.close(key.fd)
        paths, patterns, processes = sources
        watched, event_fd = {}, None
        with self._lock:
            complete = self._complete
        unwatched = []
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
            for directory, names in self._directories(paths, patterns, unwatched).items():
                wd = add(event_fd, os.fsencode(directory), _MASK)
                if wd >= 0:
                    watched.setdefault(wd, set()).update(names)
                else:
                    # Watch limit, permission or a vanished directory: these
                    # names would stay silent, so the normal cadence stays.
                    unwatched.append(directory)
        except (AttributeError, OSError, ValueError):
            if event_fd is not None and event_fd >= 0:
                try:
                    selector.unregister(event_fd)
                except KeyError:
                    pass
                os.close(event_fd)
            event_fd = None
        for pid, birth in processes:
            fd = None
            try:
                fd = os.pidfd_open(pid)
                if read_proc_start(pid) != birth:
                    os.close(fd)
                    continue  # already exited or replaced: the change set below covers it
                selector.register(fd, selectors.EVENT_READ, 'process')
            except ProcessLookupError:
                continue      # already exited: the change set below covers it
            except (AttributeError, OSError, ValueError):
                if fd is not None:
                    os.close(fd)
                unwatched.append(pid)
        self.watching = event_fd is not None and complete and not unwatched
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
            sources = configured = None
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
                            requested = (self._sources, self._complete)
                        if requested != configured:
                            configured = requested
                            sources = requested[0]
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
                            self._reset.set()
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
# Content writes of sessions already on the board (transcripts, rollouts, the
# OpenCode WAL, progress logs) restart basic collection at most this often. The
# native display reader keeps their model/effort/context cells current at once.
DEFAULT_CONTENT_REFRESH = 10.0
MIN_IDLE_REFRESH = 2.0
_SOURCE_LIMIT = 512


def _seconds(env, name, default, interval):
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return max(float(interval), default)
    try:
        value = float(raw)
    except ValueError:
        return max(float(interval), default)
    if value <= 0:
        return None
    return max(float(interval), MIN_IDLE_REFRESH, value)


def idle_refresh_interval(interval, env=None):
    """``FLEET_IDLE_REFRESH_INTERVAL`` seconds; ``0`` keeps the periodic cadence."""
    return _seconds(os.environ if env is None else env, "FLEET_IDLE_REFRESH_INTERVAL",
                    DEFAULT_IDLE_REFRESH, interval)


def content_refresh_interval(interval, env=None):
    """``FLEET_CONTENT_REFRESH_INTERVAL`` seconds; ``0`` treats content writes as structural."""
    return _seconds(os.environ if env is None else env, "FLEET_CONTENT_REFRESH_INTERVAL",
                    DEFAULT_CONTENT_REFRESH, interval)


class IdleCadence:
    """Gate a periodic pump on input change notifications.

    A structural change (registries, dispatch logs, route state, a watched
    process exit, a watch reset), user input or an explicit refresh restores the
    pump's own cadence at once. A content write of an already shown session
    runs the pump at most once per ``content_interval``. While nothing changed,
    one fallback run per ``idle_interval`` keeps unwatched inputs and
    clock-driven fields current. Without an installed watch set the pump keeps
    its normal cadence.
    """

    def __init__(self, idle_interval, content_interval=None):
        self.idle_interval = None if idle_interval is None else float(idle_interval)
        self.content_interval = None if content_interval is None else float(content_interval)
        self._dirty = True
        self._content = False
        self._last_start = None

    def mark(self):
        self._dirty = True

    def mark_content(self):
        if self.content_interval is None:
            self._dirty = True
        else:
            self._content = True

    @property
    def dirty(self):
        return self._dirty

    def due(self, now, *, watching=True):
        if (self.idle_interval is None or not watching or self._dirty
                or self._last_start is None):
            return True
        elapsed = now - self._last_start
        if self._content and elapsed >= self.content_interval:
            return True
        return elapsed >= self.idle_interval

    def started(self, now):
        self._dirty = self._content = False
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


def _limited(paths, patterns, processes, complete=True):
    """Sources bounded to ``_SOURCE_LIMIT`` each; a cut list carries ``False``.

    The trailing flag is ``NativeDisplayEvents.configure``'s ``complete``: a
    truncated list is not a full watch, so its consumer keeps the normal
    cadence instead of waiting for events that cannot arrive.
    """
    parts = (tuple(sorted(paths)), tuple(sorted(patterns)), tuple(sorted(processes)))
    if complete and all(len(part) <= _SOURCE_LIMIT for part in parts):
        return parts
    return tuple(part[:_SOURCE_LIMIT] for part in parts) + (False,)


def basic_event_sources(snapshot, display_sources=None):
    """Watchable inputs of one basic collection: ``(structural, content)``.

    Each part is ``(paths, patterns, processes)``. Paths are files whose parent
    directory is watched; patterns are ``(directory, prefix)`` with ``""``
    meaning any entry. Structural inputs decide which rows exist and how they
    are bound; content inputs are the activity files of rows already shown.
    Only stable, live identities are listed so an unchanged board keeps one
    watch set.
    """
    paths, patterns, processes = set(), set(), set()
    content = set()
    registry_dirs = set()
    try:
        from . import session_registry
        for harness in ("claude", "codex", "opencode"):
            directory = str(session_registry._dir_for(harness))
            registry_dirs.add(directory)
            patterns.add((directory, ""))
    except Exception:
        pass
    if display_sources:
        try:
            shown_paths, shown_patterns, shown_processes = display_sources
            for path in shown_paths:
                path = str(path)
                parent = Path(path).parent
                if parent.name == ".statusline":
                    # Rewritten about once a minute while idle; the display
                    # reader alone owns those cells.
                    continue
                (paths if str(parent) in registry_dirs else content).add(path)
            patterns.update(tuple(item) for item in shown_patterns)
            processes.update(tuple(item) for item in shown_processes)
        except (TypeError, ValueError):
            pass
    try:
        from .collectors import dispatch
        paths.add(str(dispatch._jobs_path()))
        paths.update(str(path) for path in dispatch._installed_registry_paths())
    except Exception:
        pass
    try:
        from . import route_chain
        root = str(route_chain.state_root())
        patterns.update((os.path.join(root, harness), "") for harness in ("claude", "codex", "opencode"))
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
    complete = True
    for name in ("sessions", "jobs", "resources"):
        listed = tuple(getattr(snapshot, name, None) or ())
        complete = complete and len(listed) <= _SOURCE_LIMIT
        rows.extend(listed[:_SOURCE_LIMIT])
    try:
        from .route import _outcome_path
    except Exception:
        _outcome_path = None
    for row in rows:
        identity = _proc_identity(getattr(row, "pid", None),
                                  getattr(row, "proc_start", None) or getattr(row, "starttime", None))
        if identity is None:
            # A finished row's files are settled; its registry/roster changes
            # arrive through the watched registries or the fallback.
            continue
        processes.add(identity)
        for attribute, target in (("_transcript_path", content), ("log_path", content),
                                  ("registry_path", paths)):
            value = getattr(row, attribute, None)
            if isinstance(value, (str, os.PathLike)) and str(value):
                target.add(str(value))
        # A direct or inline route closes through its outcome sidecar alone,
        # without a jobs row change; the route and its sidecar are structural.
        for attribute in ("route_file", "owner_route_file"):
            value = getattr(row, attribute, None)
            if isinstance(value, (str, os.PathLike)) and str(value):
                paths.add(str(value))
                if _outcome_path is not None:
                    paths.add(_outcome_path(str(value)))
    content -= paths
    return (_limited(paths, patterns, processes, complete),
            _limited(content, (), (), complete))


_HARNESS_COMMS = frozenset(("claude", "codex", "opencode", "node"))
_VERSION_COMM = re.compile(r"\d+\.\d+\.\d+")   # a native Claude binary names itself by version
_YOUNG_SECONDS = 10.0


class ProcessBirths:
    """Notice a new same-user harness-like process between basic collections.

    Process creation has no kernel notification Fleet can watch. One listing of
    ``/proc`` and a ``comm`` read of new (or still young) PIDs is enough to
    restore the normal cadence for a session or worker that has not written
    any watched registry yet; the census itself stays in basic collection.
    """

    def __init__(self, clock=None, proc_root="/proc"):
        import time as _time
        self._clock = clock or _time.monotonic
        self._root = proc_root
        self._known = None
        self._young = {}
        self._uid = os.getuid()

    def _comm(self, name):
        try:
            if os.stat(os.path.join(self._root, name)).st_uid != self._uid:
                return None
            with open(os.path.join(self._root, name, "comm"), "rb") as handle:
                return handle.read().decode("utf-8", "replace").strip()
        except OSError:
            return None

    def changed(self):
        try:
            current = {name for name in os.listdir(self._root) if name.isdigit()}
        except OSError:
            return False
        now = self._clock()
        if self._known is None:
            self._known = current
            return False
        born = current - self._known
        self._known = current
        for name in born:
            self._young[name] = now
        found = False
        for name, seen in list(self._young.items()):
            if name not in current or now - seen > _YOUNG_SECONDS:
                self._young.pop(name, None)
                continue
            comm = self._comm(name)
            if comm is None:
                self._young.pop(name, None)
            elif comm in _HARNESS_COMMS or _VERSION_COMM.fullmatch(comm):
                self._young.pop(name, None)  # one mark per process
                found = True
        return found
