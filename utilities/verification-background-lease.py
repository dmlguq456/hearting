#!/usr/bin/env python3
"""Run one bounded verification command with an exact attempt-owned lease.

Everything the command starts ends with it. The lease is the command's child
subreaper, so a fixture that detaches (setsid, double fork) re-parents here
instead of to init, and every process also carries this run's scope tag. When
the command exits, times out, or the lease is told to stop, whatever is still
in that scope is terminated. A test that left a runner or refresher behind
used to keep it running on a shared host until something else killed it.

The scope's resident memory has one ceiling, so a leak ends this command
instead of the kernel's OOM killer ending other work on the host.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import secrets
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from dispatch_contract import (
    dispatch_state_root,
    parse_registry_metadata,
    process_launch_identity,
)


def exact_attempt(jobs: Path, attempt_id: str) -> tuple[str, dict[str, str]] | None:
    found = None
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == attempt_id:
            found = (fields[1], metadata)
    return found


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def lease_context(command: list[str], timeout: float) -> tuple[Path, dict[str, object]] | None:
    attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    raw_jobs = os.environ.get("AGENT_DISPATCH_JOBS", "")
    if not attempt or not raw_jobs:
        return None
    jobs = Path(raw_jobs).expanduser().resolve(strict=False)
    row = exact_attempt(jobs, attempt)
    if row is None or row[0] not in {"open", "running"}:
        return None
    metadata = row[1]
    if metadata.get("worker_type") == "owner":
        if metadata.get("dispatch_depth") != "1":
            return None
        try:
            from dispatch_completion_join import OWNER_ROUTE_NODE
            from foreground_activity import resolve_owner_activity_binding
            binding = resolve_owner_activity_binding(jobs, attempt, metadata)
        except Exception:
            # A verification lease is optional. An owner without a uniquely
            # resolved current binding runs under the existing no-lease path.
            return None
        route_id, route_node = binding.route_id, OWNER_ROUTE_NODE
    else:
        route_id = metadata.get("route_id", "")
        route_node = metadata.get("route_node", "")
    if not route_id or not route_node:
        return None
    digest = hashlib.sha256("\0".join(command).encode("utf-8")).hexdigest()
    path = dispatch_state_root(jobs) / "verification-leases" / f"{attempt}.json"
    return path, {
        "schema_version": 1,
        "attempt_id": attempt,
        "route_id": route_id,
        "route_node": route_node,
        "command_digest": digest,
        "started_at": time.time(),
        "deadline": time.time() + timeout,
    }


SCOPE_ENV = "HEARTING_VERIFICATION_SCOPE"
_PR_SET_CHILD_SUBREAPER = 36
MEMORY_POLL_SECONDS = 1.0
MEMORY_EXCEEDED_EXIT = 125
SUBREAPER_UNAVAILABLE_EXIT = 126
SCOPE_CLEANUP_FAILED_EXIT = 127
_CLEANUP_ACTIVE = False
_LAUNCHING = False
_DEFERRED_STOP: int | None = None


def memory_budget_bytes() -> int:
    """16 GiB, or a quarter of this host's memory when that is smaller."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        total = 0
    budget = 16 * 1024 ** 3
    return min(budget, total // 4) if total > 0 else budget


def become_subreaper() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def process_table() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, resident bytes) for every live, non-zombie process."""
    page = os.sysconf("SC_PAGE_SIZE")
    table: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] in (b"Z", b"X"):
            continue
        table[int(name)] = (int(fields[1]), int(fields[21]) * page)
    return table


def scope_pids(table: dict[int, tuple[int, int]], scope: str | None) -> set[int]:
    """This lease's descendants, plus any process carrying its scope tag."""
    own = os.getpid()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _rss) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(children.get(own, ()))
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack.extend(children.get(pid, ()))
    if scope:
        marker = f"{SCOPE_ENV}={scope}".encode()
        for pid in table:
            if pid in found or pid == own:
                continue
            try:
                if marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                    found.add(pid)
            except OSError:
                continue
    found.discard(own)
    return found


def reap_zombies(keep: int | None = None) -> None:
    """Collect exited adopted children; `keep` (the command) stays for its own wait."""
    own = str(os.getpid()).encode()
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == keep:
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()
            if fields[0] == b"Z" and fields[1] == own:
                os.waitpid(int(name), os.WNOHANG)
        except (OSError, IndexError):
            continue


def end_scope(scope: str | None, *, grace: float = 2.0) -> list[int]:
    """Terminate everything still in this run's scope; a killed parent can
    re-parent its own children here, so repeat until nothing is left."""
    ended: list[int] = []
    for _round in range(5):
        pids = scope_pids(process_table(), scope)
        if not pids:
            break
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                reap_zombies()
                live = process_table()
                if not any(pid in live for pid in pids):
                    break
                time.sleep(.05)
            else:
                continue
            break
        ended.extend(sorted(pids))
        reap_zombies()
    return ended


def wait_within_budget(process: subprocess.Popen[bytes], timeout: float, scope: str) -> int | None:
    """The command's exit status; None on expiry. Raises MemoryError past the budget."""
    budget = memory_budget_bytes()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return process.wait(timeout=min(MEMORY_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            pass
        table = process_table()
        resident = sum(table[pid][1] for pid in scope_pids(table, scope) if pid in table)
        if resident > budget:
            raise MemoryError(f"verification-memory-budget-exceeded resident_bytes={resident} "
                              f"budget_bytes={budget}")


def _stop(signum, _frame):
    global _DEFERRED_STOP
    if _CLEANUP_ACTIVE:
        return
    if _LAUNCHING:
        _DEFERRED_STOP = signum
        return
    raise SystemExit(128 + signum)


def terminate_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    global _CLEANUP_ACTIVE, _LAUNCHING, _DEFERRED_STOP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--require-subreaper", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if args.timeout <= 0 or not command:
        parser.error("a positive --timeout and command are required")
    subreaper_ready = become_subreaper()
    if (args.require_subreaper
            and (not subreaper_ready or os.getpid() not in process_table())):
        print("verification-subreaper-unavailable", file=sys.stderr)
        return SUBREAPER_UNAVAILABLE_EXIT
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _stop)
    scope = secrets.token_hex(8)
    process: subprocess.Popen | None = None
    lease_path: Path | None = None
    status = 1
    _CLEANUP_ACTIVE = False
    _DEFERRED_STOP = None
    _LAUNCHING = True
    try:
        # A stop delivered between Popen's return and the assignment must not
        # lose the child identity. Defer only that window; all setup after it
        # is already inside this lifetime's cleanup boundary.
        process = subprocess.Popen(command, start_new_session=True,
                                   env={**os.environ, SCOPE_ENV: scope})
        _LAUNCHING = False
        if _DEFERRED_STOP is not None:
            raise SystemExit(128 + _DEFERRED_STOP)
        # Adopted descendants are reaped without taking the command's status.
        signal.signal(signal.SIGCHLD, lambda _signum, _frame: reap_zombies(keep=process.pid))
        lease = lease_context(command, args.timeout)
        if lease is not None:
            lease_path, value = lease
            identity = process_launch_identity(process.pid)
            if identity.get("pid_start") and identity.get("pgid") == str(process.pid):
                value.update({"pid": process.pid, "pid_start": identity["pid_start"],
                              "pgid": process.pid})
                atomic_json(lease_path, value)
            else:
                lease_path = None
        status = wait_within_budget(process, args.timeout, scope)
        if status is None:
            terminate_group(process)
            status = 124
    except MemoryError as exc:
        print(exc, file=sys.stderr)
        if process is not None:
            terminate_group(process)
        status = MEMORY_EXCEEDED_EXIT
    finally:
        _CLEANUP_ACTIVE = True
        _LAUNCHING = False
        if process is not None and process.poll() is None:
            terminate_group(process)
        ended = end_scope(scope)
        remaining = sorted(scope_pids(process_table(), scope))
        if ended:
            print(f"verification-scope-ended pids={','.join(map(str, ended))}", file=sys.stderr)
        if remaining:
            print(f"verification-scope-cleanup-incomplete pids={','.join(map(str, remaining))}",
                  file=sys.stderr)
            status = SCOPE_CLEANUP_FAILED_EXIT
        if lease_path is not None:
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
    return status


if __name__ == "__main__":
    raise SystemExit(main())
