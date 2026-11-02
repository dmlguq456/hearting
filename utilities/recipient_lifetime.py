"""Pass-local recipient lifetime evidence for peer watch settlement.

The result is intentionally tri-state. This module never serializes evidence and
does not infer lifetime from wall-clock age, activity timestamps, or retries.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import stat
import time

from claude_process_identity import _owner_directory, is_local_path


ALIVE = "alive"
ENDED = "ended"
UNKNOWN = "unknown"
_STATES = {ALIVE, ENDED, UNKNOWN}
_HISTORY_MAX_BYTES = 64 * 1024 * 1024
_HISTORY_MAX_LINE = 1024 * 1024
_HISTORY_BUDGET_SECONDS = 1.0


def _usable_session_id(session_id: str) -> bool:
    return (isinstance(session_id, str) and bool(session_id)
            and session_id != "-"
            and all(char.isalnum() or char in "._-" for char in session_id)
            and session_id.strip(".") != "")


def _native_claude_home() -> Path | None:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(configured) if configured else Path.home() / ".claude"
    if not home.is_absolute() or ".." in home.parts:
        return None
    return home


def _no_symlink_path(path: Path) -> bool:
    cursor = Path(path.anchor)
    for component in path.parts[1:]:
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_history_session_ids(home: Path) -> set[str] | None:
    """Read exact completed native history rows once for this recipient pass."""
    try:
        if (not _owner_directory(home) or not _no_symlink_path(home)
                or not is_local_path(home)):
            return None
        path = home / "history.jsonl"
        try:
            before = path.lstat()
        except FileNotFoundError:
            return set()
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_size > _HISTORY_MAX_BYTES or not is_local_path(path)):
            return None
        flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            identity = _file_identity(before)
            if (not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != identity):
                return None
            found = set()
            started = time.monotonic()
            total = 0
            with os.fdopen(os.dup(fd), "rb") as handle:
                while True:
                    line = handle.readline(_HISTORY_MAX_LINE + 1)
                    if not line:
                        break
                    total += len(line)
                    if (total > _HISTORY_MAX_BYTES
                            or time.monotonic() - started > _HISTORY_BUDGET_SECONDS):
                        return None
                    oversized = len(line) > _HISTORY_MAX_LINE
                    while not line.endswith(b"\n") and oversized:
                        line = handle.readline(_HISTORY_MAX_LINE + 1)
                        total += len(line)
                        if (total > _HISTORY_MAX_BYTES
                                or time.monotonic() - started > _HISTORY_BUDGET_SECONDS):
                            return None
                        if not line:
                            break
                        oversized = oversized or len(line) > _HISTORY_MAX_LINE
                    if oversized or not line.endswith(b"\n"):
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict):
                        session_id = row.get("sessionId")
                        if _usable_session_id(session_id):
                            found.add(session_id)
            after = os.fstat(fd)
            current = path.lstat()
            if (_file_identity(after) != identity or _file_identity(current) != identity
                    or not _owner_directory(home) or not _no_symlink_path(home)
                    or not is_local_path(home)):
                return None
            return found
        finally:
            os.close(fd)
    except (OSError, TypeError, ValueError):
        return None


def _transcript_exists(home: Path, session_id: str) -> bool:
    """Accept only a stable, owner-local exact `<sid>.jsonl` native transcript."""
    try:
        projects = home / "projects"
        if (not _owner_directory(home) or not _owner_directory(projects)
                or not _no_symlink_path(home) or not _no_symlink_path(projects)
                or not is_local_path(home) or not is_local_path(projects)):
            return False
        filename = session_id + ".jsonl"
        started = time.monotonic()
        with os.scandir(projects) as entries:
            for entry in entries:
                if time.monotonic() - started > _HISTORY_BUDGET_SECONDS:
                    return False
                try:
                    directory_info = entry.stat(follow_symlinks=False)
                    if (not stat.S_ISDIR(directory_info.st_mode)
                            or directory_info.st_uid != os.getuid()):
                        continue
                    directory = Path(entry.path)
                    if not is_local_path(directory):
                        continue
                    path = directory / filename
                    before = path.lstat()
                    if (not stat.S_ISREG(before.st_mode)
                            or before.st_uid != os.getuid() or not is_local_path(path)):
                        continue
                    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
                    fd = os.open(path, flags)
                    try:
                        opened = os.fstat(fd)
                        identity = _file_identity(before)
                        if (not stat.S_ISREG(opened.st_mode)
                                or _file_identity(opened) != identity):
                            continue
                        os.read(fd, 1)
                        after = os.fstat(fd)
                        current = path.lstat()
                        current_directory = directory.lstat()
                        if (_file_identity(after) == identity
                                and _file_identity(current) == identity
                                and _file_identity(current_directory) ==
                                (directory_info.st_dev, directory_info.st_ino,
                                 directory_info.st_mode, directory_info.st_uid,
                                 directory_info.st_size, directory_info.st_mtime_ns,
                                 directory_info.st_ctime_ns)
                                and _owner_directory(home) and _no_symlink_path(home)
                                and is_local_path(home)):
                            return True
                    finally:
                        os.close(fd)
                except OSError:
                    continue
    except (OSError, TypeError, ValueError):
        return False
    return False


def _has_native_existence_evidence(
    session_id: str, history_cache: dict[str, set[str] | None]
) -> bool:
    if not _usable_session_id(session_id):
        return False
    try:
        home = _native_claude_home()
        if home is None:
            return False
        key = str(home)
        if key not in history_cache:
            history_cache[key] = _read_history_session_ids(home)
        history_ids = history_cache[key]
        if history_ids is not None and session_id in history_ids:
            return True
        return _transcript_exists(home, session_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


class RecipientCensus:
    """Memoize one bounded evidence probe for each recipient during one pass.

    A Claude session that a live process still names only through an old claim
    (a stale statusline tap or launch argument) is ENDED when that same process's
    native session is the confirmed same-harness tidy successor of the claim. The
    positive evidence is the live process holding the successor, so it does not
    need the census to be complete.
    """

    __slots__ = ("_probe", "_results", "_host_result", "_history_sids", "_lineage", "_graph")

    def __init__(self, probe: Callable[[str, str], str] | None = None, *,
                 lineage: Callable[[str, str], tuple[str, str]] | None = None):
        self._probe = probe
        self._results: dict[tuple[str, str], str] = {}
        self._host_result = None
        self._history_sids: dict[str, set[str] | None] = {}
        self._lineage = lineage
        self._graph = None

    def _chain_end(self, harness: str, session_id: str) -> tuple[str, str]:
        if self._lineage is not None:
            return self._lineage(harness, session_id)
        try:
            import dispatch_seat_handover as handover
            if self._graph is None:
                self._graph = handover.lineage_graph()
            return handover._chain_end(self._graph, harness, session_id)
        except Exception:
            return session_id, "unresolved"

    def _superseded(self, session_id: str, holders) -> bool:
        claims = getattr(holders, "process_claims", ())
        if not claims or any(native == session_id for native, _sids in claims):
            return False
        named_by = [native for native, sids in claims if session_id in sids]
        if not named_by:
            return False
        try:
            return all(native and self._chain_end("claude", session_id) == (native, "successor")
                       for native in named_by)
        except Exception:
            return False

    def state(self, harness: str, session_id: str) -> str:
        key = (harness, session_id)
        if key not in self._results:
            result = UNKNOWN
            if self._probe is None and harness == "claude":
                if self._host_result is None:
                    try:
                        from claude_process_identity import claude_session_census
                        self._host_result = claude_session_census()
                    except Exception:
                        self._host_result = (False, set(), {"census-failed"})
                complete, holders, _reasons = self._host_result
                if session_id in holders and self._superseded(session_id, holders):
                    result = ENDED
                elif session_id in holders:
                    result = ALIVE
                elif complete and _has_native_existence_evidence(
                        session_id, self._history_sids):
                    result = ENDED
            elif self._probe is not None:
                try:
                    observed = self._probe(harness, session_id)
                    if observed in _STATES:
                        result = observed
                except Exception:
                    result = UNKNOWN
            self._results[key] = result
        return self._results[key]


def recipient_state(*, harness: str, session_id: str,
                    seat_state: str | None = None,
                    lifetime_state: str | None = None,
                    successor_state: str | None = None,
                    census: RecipientCensus | None = None) -> str:
    """Resolve exact positive lifetime evidence without turning uncertainty into end.

    A confirmed successor or live seat keeps the duty alive. A failed or
    conflicting successor lookup is unknown. Existing seat/lifetime rules may
    prove end. A seat-less Claude recipient can use the pass-local complete
    census; seat-less Codex and OpenCode remain unknown absent their existing
    positive lifetime evidence.
    """
    if successor_state == ALIVE or seat_state == ALIVE or lifetime_state == ALIVE:
        return ALIVE
    if successor_state not in (None, ENDED):
        return UNKNOWN
    if seat_state not in (None, ALIVE, ENDED):
        return UNKNOWN
    if lifetime_state == ENDED:
        return ENDED
    if lifetime_state not in (None, ALIVE, ENDED):
        return UNKNOWN
    if seat_state == ENDED:
        return ENDED
    if harness == "claude" and session_id and census is not None:
        return census.state(harness, session_id)
    return UNKNOWN
