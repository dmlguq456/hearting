#!/usr/bin/env python3
"""Clear a Codex session's Fleet wait when its TUI exits.

Codex hooks run in the shared app-server daemon, which keeps a thread loaded
after its TUI leaves; its ``SessionEnd`` comes only when the thread unloads
(https://learn.chatgpt.com/docs/hooks#sessionend). Claude and OpenCode clear
the wait from their own session end. Here the prompt hook starts one small
detached watcher per thread: it finds the live TUI process that owns the thread
(``fleet.collectors.codex.process_for_thread``, the same proof Fleet and
peer-steward use), sleeps on that process (``pidfd``), and clears the exact
session's wait once it is gone. One watcher per thread (a held lock); a TUI
that cannot be proven starts nothing. Silent and fail-open.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
_SAFE_SID = re.compile(r"^[0-9a-f]{8}-[0-9a-f-]{27}$")
FIND_SECONDS = 20.0


def _state_dir(env) -> Path:
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    return Path(base) / "hearting" / "codex-tui-exit"


def launch(session_id: str, *, environ=None) -> bool:
    """Start the watcher for ``session_id`` unless one already holds it."""
    env = dict(os.environ if environ is None else environ)
    if not isinstance(session_id, str) or not _SAFE_SID.match(session_id):
        return False
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    lock = _state_dir(env) / f"{session_id}.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(lock, "a") as handle:  # cheap pre-check; the watcher takes it for real
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(handle, fcntl.LOCK_UN)
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "watch", session_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True, cwd=str(ROOT), env=env)
        try:
            sys.path.insert(0, str(ROOT / "utilities"))
            from detached_lifetime import bind
            bind(child)
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001 -- a prompt never depends on this watcher
        return False


def _owner(session_id: str):
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.collectors import codex
    deadline = time.monotonic() + FIND_SECONDS
    while True:
        try:
            owner = codex.process_for_thread(session_id, fresh=True)
        except Exception:  # noqa: BLE001
            owner = None
        if owner or time.monotonic() >= deadline:
            return owner
        time.sleep(1.0)


def watch(session_id: str) -> int:
    lock = _state_dir(os.environ) / f"{session_id}.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(lock, "a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another watcher holds this thread
    owner = _owner(session_id)
    if not owner:
        return 0
    pid, start = owner
    try:
        fd = os.pidfd_open(pid)
    except (OSError, AttributeError):
        return 0
    from fleet.collectors import procscan
    if procscan.read_proc_start(pid) == str(start):
        select.select([fd], [], [])  # sleeps until the proven TUI process ends
    os.close(fd)
    # Reopening the same thread while its old watcher unwinds is a resume.
    from fleet.collectors import codex
    current = codex.process_for_thread(session_id, fresh=True)
    if current and current != owner:
        return 0
    from peer_steward_base import peer_message
    peer_message.observe_steward_end("codex", session_id, reason="tui-exited")
    # Gone already, or now: either way the TUI that owned this thread has left.
    from fleet import interaction
    interaction.clear_wait(session_id, "codex")
    with_suppressed_unlink(lock)
    return 0


def with_suppressed_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def main(argv) -> int:
    if len(argv) == 2 and argv[0] == "watch":
        try:
            return watch(argv[1])
        except Exception:  # noqa: BLE001
            return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
