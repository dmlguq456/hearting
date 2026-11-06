"""Read the existing exact-session prompt frontier; never read native conversations."""
import copy
import json
import os
from pathlib import Path
import stat
import sys
import threading

from runtime_observation import ProjectionCache

_CACHE = ProjectionCache(max_entries=512, max_bytes=2 * 1024 * 1024)
_LOCK = threading.RLock()
_MAX_BYTES = 64 * 1024


def _tidy():
    utilities = str(Path(__file__).resolve().parents[3] / "utilities")
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    import session_tidy
    return session_tidy


def _generation(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid)


def read(harness, sid):
    tidy = _tidy()
    if harness not in tidy.HARNESSES or not isinstance(sid, str) or not sid:
        return None
    seat = tidy.session_seat(tidy.Seat("native", "", harness=harness, sid=sid), harness, sid)
    path = tidy._prompt_seq_path(seat)
    key = (str(path), harness, sid, tidy.prompt_display)
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > _MAX_BYTES):
            return None
        generation = _generation(info)
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None and cached[0] == generation:
                _CACHE.move_to_end(key)
                return copy.deepcopy(cached[1])
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if _generation(os.fstat(stream.fileno())) != generation:
                return None
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES or _generation(path.lstat()) != generation:
            return None
        value = tidy.prompt_display(seat, harness, sid, current=json.loads(raw))
        with _LOCK:
            _CACHE[key] = (generation, value)
        return copy.deepcopy(value)
    except (OSError, ValueError, TypeError):
        return None


def enrich(sessions):
    for session in sessions:
        # Display aliases describe other native identities, never their input.
        try:
            session.prompt_display = read(session.harness, session.session_id)
        except Exception:
            session.prompt_display = None
