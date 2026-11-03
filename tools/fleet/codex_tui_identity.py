"""A Codex TUI's thread, recorded by its own hook in the OpenCode ``tui-identity`` format.

The OpenCode TUI plugin writes ``$XDG_STATE_HOME/hearting/tui-identity/<pid>-<start>.json``
for the process it runs in. A Codex hook that runs below its TUI (the TUI owns its
app-server, so the TUI is an ancestor) writes the same file for that TUI, so a resumed or
switched thread is identified without the daemon's remembered ``codex_tui`` address, which
can name an earlier TUI. The record holds ``{"schema", "pid", "start", "sessionID", "harness":
"codex", "writtenAt"}``; OpenCode's own reader only accepts ``ses_`` ids, so the two never
read each other's files.

A hook below the shared background service has no TUI ancestor and records nothing.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import stat

SCHEMA = "hearting-tui-selection-v1"
MAX_BYTES = 1024
_THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NAME = re.compile(r"^(\d+)-(\d+)\.json$")
_MAX_ANCESTORS = 32
_PRUNE_LIMIT = 256


def _directory(environ=None):
    env = os.environ if environ is None else environ
    home = env.get("HOME") or ""
    base = env.get("XDG_STATE_HOME") or (os.path.join(home, ".local", "state") if home else "")
    return os.path.join(base, "hearting", "tui-identity") if base and os.path.isabs(base) else ""


def _read(path):
    """One regular, own, small JSON object, or None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_BYTES:
            return None
        raw = os.read(fd, MAX_BYTES + 1)
        row = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return row if isinstance(row, dict) else None


def read_thread(pid, start, environ=None):
    """The thread id this TUI's hook recorded for exactly ``(pid, start)``, or None."""
    directory = _directory(environ)
    if (not directory or type(pid) is not int or pid <= 0
            or isinstance(start, bool) or not str(start).isdigit() or int(start) <= 0):
        return None
    row = _read(os.path.join(directory, "%d-%s.json" % (pid, start)))
    if (row is None or row.get("schema") != SCHEMA or row.get("harness") != "codex"
            or type(row.get("pid")) is not int or row["pid"] != pid
            or str(row.get("start")) != str(start)
            or not isinstance(row.get("sessionID"), str)
            or not _THREAD_ID.fullmatch(row["sessionID"])):
        return None
    return row["sessionID"]


def main_thread(session_id):
    """False only when the thread's rollout is found and is a subagent's."""
    from .collectors import codex
    path = codex.exact_rollout_for_session_id(session_id)
    return not (path and codex._is_subagent(codex._rollout_meta(path)))


def tui_above(pid=None):
    """``(pid, start)`` of the nearest live Codex conversation TUI above this process, or None."""
    from .collectors import codex, procscan
    pid = os.getpid() if pid is None else pid
    for _ in range(_MAX_ANCESTORS):
        if not pid or pid <= 1:
            return None
        if procscan._comm_of(pid) == "codex":
            start = codex._live_tui_start(pid)
            if start:
                return pid, start
        try:
            with open("/proc/%d/stat" % pid, "rb") as handle:
                pid = int(handle.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
    return None


def _prune(directory, keep):
    """Drop dead Codex TUIs' records; OpenCode's retained records are never touched."""
    from .collectors import procscan
    try:
        names = os.listdir(directory)[:_PRUNE_LIMIT]
    except OSError:
        return
    for name in names:
        match = _NAME.match(name)
        if not match or name == keep:
            continue
        path = os.path.join(directory, name)
        row = _read(path)
        if row is None or row.get("harness") != "codex":
            continue
        if procscan.read_proc_start(int(match.group(1))) == match.group(2):
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def record(session_id, environ=None, tui=None):
    """Record ``session_id`` for the TUI above this hook. True when a record was written."""
    try:
        directory = _directory(environ)
        if not directory or not _THREAD_ID.fullmatch(session_id or "") or not main_thread(session_id):
            return False
        tui = tui_above() if tui is None else tui
        if not tui:
            return False
        pid, start = tui
        os.makedirs(directory, mode=0o700, exist_ok=True)
        name = "%d-%s.json" % (pid, start)
        body = json.dumps({
            "schema": SCHEMA, "pid": pid, "start": str(start), "sessionID": session_id,
            "harness": "codex",
            "writtenAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }, separators=(",", ":"))
        temporary = os.path.join(directory, ".%s.%d.tmp" % (name, os.getpid()))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, body.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(temporary, os.path.join(directory, name))
        _prune(directory, name)
        return True
    except Exception:
        return False
