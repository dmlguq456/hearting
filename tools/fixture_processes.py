#!/usr/bin/env python3
"""Stop and reap every process a test fixture started, before its temp dir goes.

A test that starts a detached helper (a `peer-steward.py watch` watcher, a fake `herdr`
child) owns that helper's lifetime. Deleting the fixture's TemporaryDirectory while the
helper still writes receipts into it fails intermittently with ``OSError: [Errno 39]
Directory not empty`` (CI, main 28170cd3 and 7dbe8e7e, 2026-09-24).

The old per-suite reapers matched the fixture path in ``/proc/<pid>/cmdline`` only. A
watcher's argv names its watch id, not the fixture; the path reaches it through the
environment. So a watcher between two `herdr` calls — or one whose `herdr` child had
already exited — matched nothing and survived the reaper, still writing (reproduced
2026-09-24: the watcher's cmdline lacked the marker, its environ carried it).

``reap(marker)`` therefore matches the marker in cmdline OR environ, pins each match by
``(pid, start ticks)``, SIGKILLs its process group (never the caller's own group), and
returns only after every matched process is gone or a zombie — a zombie writes nothing.
Used as a cleanup registered AFTER the TemporaryDirectory cleanup, so it runs first.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


def _start_ticks(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return fields[19].decode()
    except (OSError, IndexError):
        return None


def _state(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().rsplit(b")", 1)[1].split()[0].decode()
    except (OSError, IndexError):
        return None


def _carries(pid: int, needle: bytes) -> bool:
    for name in ("cmdline", "environ"):
        try:
            with open(f"/proc/{pid}/{name}", "rb") as fh:
                if needle in fh.read():
                    return True
        except OSError:
            continue
    return False


def _callers() -> set[int]:
    """A cleanup subprocess must preserve the shell/test that invoked it."""
    callers = {1}
    pid = os.getpid()
    while pid > 1 and pid not in callers:
        callers.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as stream:
                pid = int(stream.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return callers


def marked_processes(marker: str) -> dict[int, str]:
    """``{pid: start_ticks}`` of every live, non-zombie process carrying ``marker``."""
    needle = marker.encode()
    callers = _callers()
    found: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in callers:
            continue
        pid = int(entry)
        if _state(pid) in (None, "Z") or not _carries(pid, needle):
            continue
        ticks = _start_ticks(pid)
        if ticks is not None:
            found[pid] = ticks
    return found


def _gone(pid: int, ticks: str) -> bool:
    return _start_ticks(pid) != ticks or _state(pid) in (None, "Z")


def reap(marker: str, *, fifo: str | None = None, timeout: float = 10.0) -> list[int]:
    """Kill every process carrying ``marker`` and wait until each one is gone.

    Returns the pids it killed. Raises ``TimeoutError`` if one survives ``timeout``
    seconds: a cleanup that silently leaves a writer behind is the defect itself.
    """
    own_group = os.getpgrp()
    killed: dict[int, str] = {}
    deadline = time.monotonic() + timeout
    # Each snapshot can miss a newly detached descendant. Confirm an empty
    # snapshot after draining every generation, within the same deadline.
    while True:
        batch = marked_processes(marker)
        for pid, ticks in batch.items():
            if _start_ticks(pid) != ticks:
                continue
            try:
                group = os.getpgid(pid)
            except OSError:
                continue
            try:
                if group != own_group:
                    os.killpg(group, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            killed[pid] = ticks
        while time.monotonic() < deadline:
            if all(_gone(pid, ticks) for pid, ticks in killed.items()):
                break
            time.sleep(0.01)
        else:
            alive = sorted(pid for pid, ticks in killed.items() if not _gone(pid, ticks))
            if alive:
                raise TimeoutError("fixture processes survived SIGKILL: %s" % alive)
        if not batch:
            break
        if time.monotonic() >= deadline:
            alive = sorted(pid for pid, ticks in marked_processes(marker).items()
                           if not _gone(pid, ticks))
            if alive:
                raise TimeoutError("fixture processes survived SIGKILL: %s" % alive)
            break
    if fifo and os.path.exists(fifo):
        # Unblock anything left waiting on the pipe so no reader outlives its path.
        for _ in range(64):
            try:
                fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                break
            try:
                os.write(fd, b"go")
            except OSError:
                pass
            os.close(fd)
    return sorted(killed)


class NativeSession:
    """A real native Claude session a fixture can watch, with no Herdr.

    One process whose argv[0] is ``claude`` plus the runtime's own registry record
    (``<home>/.claude/sessions/<pid>.json``): exactly what the production readers
    (``peer_address.native_sessions``, ``terminal_surface.native_turn``) read. ``set``
    rewrites the record's status the way the runtime does and ``stop`` is a confirmed
    end. The fixture must point ``HOME`` and ``CLAUDE_CONFIG_DIR`` of the process under
    test at ``home`` / ``home/.claude``.
    """

    STATUS = {"idle": "idle", "working": "busy", "blocked": "waiting"}

    def __init__(self, test, *, sid, home, pane="w1:p9", name=None):
        self.sid, self.name = sid, name
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "HERDR_", "TMUX"))}
        env["HOME"] = str(home)
        env["CLAUDE_CONFIG_DIR"] = str(Path(home) / ".claude")
        env["HERDR_PANE_ID"] = pane
        self.proc = subprocess.Popen(
            ["bash", "-c", "exec -a /usr/bin/claude sleep 3600"], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        test.addCleanup(self.stop)
        self.pid = self.proc.pid
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                argv = Path(f"/proc/{self.pid}/cmdline").read_bytes()
            except OSError:
                argv = b""
            if b"claude" in argv:
                break
            time.sleep(0.01)
        self.birth = _start_ticks(self.pid)
        self.path = Path(home) / ".claude" / "sessions" / f"{self.pid}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.set("idle")

    def set(self, status):
        record = {"sessionId": self.sid, "procStart": self.birth,
                  "status": self.STATUS.get(status, status)}
        if self.name:
            record["name"] = self.name
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, self.path)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
