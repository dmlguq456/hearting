#!/usr/bin/env python3
"""Run one bounded verification command with an exact attempt-owned lease.

Everything the command starts ends with it. The lease is the command's child
subreaper, so a fixture that detaches (setsid, double fork) re-parents here
instead of to init, and every process also carries this run's scope tag. When
the command exits, times out, or the lease is told to stop, whatever is still
in that scope is terminated. A test that left a runner or refresher behind
used to keep it running on a shared host until something else killed it.

The scope's memory is measured as proportional set size so shared pages count
once, with resident size used where PSS cannot be read. The --memory-budget SIZE
option replaces the default ceiling with an approved value, and the lease prints
the scope's peak on exit.
"""

from __future__ import annotations

import argparse
import ctypes
from fractions import Fraction
import hashlib
import json
import os
import secrets
from pathlib import Path
import signal
import shutil
import stat
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
PSS_DUTY_FACTOR = 20
PEAK_RECORD_FRACTION = 16
SUBREAPER_UNAVAILABLE_EXIT = 126
SCOPE_CLEANUP_FAILED_EXIT = 127
_CLEANUP_ACTIVE = False
_LAUNCHING = False
_DEFERRED_STOP: int | None = None


def memory_budget_bytes() -> int:
    """16 GiB, or a quarter of this host's memory when that is smaller."""
    total = host_memory_bytes()
    budget = 16 * 1024 ** 3
    return min(budget, total // 4) if total > 0 else budget


def host_memory_bytes() -> int:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError):
        return 0


def parse_size(text: str) -> int:
    import re
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Za-z]*)", text.strip())
    if not match:
        raise ValueError("invalid size")
    number, unit = match.groups()
    unit = unit.lower()
    if unit in ("", "b"):
        if "." in number:
            raise ValueError("fractional bytes")
        multiplier = 1
    else:
        decimal = unit in {"kb", "mb", "gb", "tb"}
        prefix = unit[:-2] if unit.endswith("ib") else unit[:-1] if unit.endswith("b") else unit
        if prefix not in ("k", "m", "g", "t"):
            raise ValueError("unknown size unit")
        exponent = ("k", "m", "g", "t").index(prefix) + 1
        multiplier = (1000 if decimal else 1024) ** exponent
    result = int(Fraction(number) * multiplier)
    if result < 1:
        raise ValueError("size is less than one byte")
    return result


def resolve_memory_budget(raw: str | None) -> tuple[int, str]:
    if raw is None:
        return memory_budget_bytes(), "default"
    try:
        value = parse_size(raw)
    except ValueError:
        raise ValueError(f"{raw!r}: expected a positive size such as 140GB, 8GiB or 1073741824") from None
    host = host_memory_bytes()
    if host > 0 and value > host:
        raise ValueError(f"{raw!r} is {value} bytes, above this host's {host} bytes")
    return value, "option"


def become_subreaper() -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _read_process_stat(pid: int) -> tuple[bytes, int, int, int]:
    """Return (state, ppid, resident bytes, start ticks) for one PID."""
    fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()
    if len(fields) <= 21:
        raise ValueError("incomplete proc stat")
    page = os.sysconf("SC_PAGE_SIZE")
    return fields[0], int(fields[1]), int(fields[21]) * page, int(fields[19])


def process_snapshot() -> dict[int, tuple[bytes, int, int, int]]:
    """pid -> (state, ppid, resident bytes, start ticks) for readable processes."""
    snapshot: dict[int, tuple[bytes, int, int, int]] = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            snapshot[int(name)] = _read_process_stat(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return snapshot


def _live_process_table(snapshot: dict[int, tuple[bytes, int, int, int]]) -> dict[int, tuple[int, int]]:
    return {pid: (ppid, rss) for pid, (state, ppid, rss, _start) in snapshot.items()
            if state not in (b"Z", b"X")}


def process_table() -> dict[int, tuple[int, int]]:
    """pid -> (ppid, resident bytes) for every live, non-zombie process."""
    return _live_process_table(process_snapshot())


def pss_bytes(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as handle:
            for line in handle:
                if line.startswith(b"Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def measure_scope(table: dict[int, tuple[int, int]], pids: set[int]) -> tuple[int, str]:
    measured = 0
    fallbacks = 0
    live = 0
    for pid in pids:
        value = pss_bytes(pid)
        if value is None:
            if pid not in table or not os.path.exists(f"/proc/{pid}/stat"):
                continue
            value = table[pid][1]
            fallbacks += 1
        measured += value
        live += 1
    if fallbacks == 0:
        measure = "pss-sum"
    elif fallbacks == live:
        measure = "rss-sum"
    else:
        measure = "mixed"
    return measured, measure


class MemoryPeak:
    def __init__(self):
        self.bytes: int | None = None
        self.measure: str | None = None
        self.samples = 0
        self.interval = 0.0
        self.rss_peak: int | None = None
        self.rss_samples = 0
        self.written: int | None = None

    def record_rss(self, value: int) -> None:
        self.rss_samples += 1
        self.rss_peak = value if self.rss_peak is None else max(self.rss_peak, value)

    def record(self, value: int, measure: str, period: float) -> None:
        self.samples += 1
        self.interval = max(self.interval, period)
        if self.bytes is None or value > self.bytes:
            self.bytes = value
            self.measure = measure

    def json_due(self, budget: int) -> bool:
        return self.samples > 0 and (self.written is None or
                                     self.bytes - self.written >= budget // PEAK_RECORD_FRACTION)

    def line(self, budget: int, source: str) -> str:
        if self.samples == 0:
            measured = "bytes=none measure=none interval_s=none rss_sum_peak_bytes=none"
        else:
            measured = (f"bytes={self.bytes} measure={self.measure} "
                        f"interval_s={self.interval:.1f} rss_sum_peak_bytes={self.rss_peak}")
        return (f"verification-memory-peak {measured} scope=tree-aggregate "
                f"samples={self.samples} rss_samples={self.rss_samples} "
                f"rss_interval_s={MEMORY_POLL_SECONDS:g} budget_bytes={budget} "
                f"budget_source={source}")


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


def _process_children(pid: int) -> set[int]:
    """Read children from proc task files, including children with unreadable stat."""
    tasks = Path(f"/proc/{pid}/task")
    try:
        tids = [name for name in os.listdir(tasks) if name.isdigit()]
    except FileNotFoundError:
        return set()
    children: set[int] = set()
    for tid in tids:
        try:
            raw = Path(f"/proc/{pid}/task/{tid}/children").read_bytes()
        except FileNotFoundError:
            continue
        for item in raw.split():
            children.add(int(item))
    return children


def _remember_scope(snapshot: dict[int, tuple[bytes, int, int, int]], scope: str | None,
                    observed: dict[int, int | None]) -> set[int]:
    pids = scope_pids(_live_process_table(snapshot), scope)
    pending = list(pids | _process_children(os.getpid()) | set(observed))
    visited: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in visited or pid == os.getpid():
            continue
        visited.add(pid)
        identity = snapshot.get(pid)
        if identity is None:
            try:
                identity = _read_process_stat(pid)
            except FileNotFoundError:
                continue
            except (OSError, IndexError, ValueError):
                observed.setdefault(pid, None)
                continue
        state, _ppid, _rss, start = identity
        if state in (b"Z", b"X"):
            continue
        expected = observed.get(pid)
        if expected is not None and expected != start:
            continue
        observed[pid] = start
        pids.add(pid)
        pending.extend(_process_children(pid) - visited)
    return pids


def _owned_process_state(observed: dict[int, int | None]) -> tuple[set[int], dict[int, str]]:
    """Return same-identity live PIDs and owned PIDs whose state is unknown."""
    live: set[int] = set()
    unknown: dict[int, str] = {}
    for pid, expected_start in observed.items():
        try:
            state, _ppid, _rss, current_start = _read_process_stat(pid)
        except FileNotFoundError:
            continue
        except (OSError, IndexError, ValueError):
            unknown[pid] = "unreadable"
            continue
        if expected_start is None:
            unknown[pid] = "identity-unavailable"
        elif current_start != expected_start or state in (b"Z", b"X"):
            continue
        else:
            live.add(pid)
    return live, unknown


def create_private_scratch(command_env: dict[str, str]) -> tuple[Path, tuple[int, int]]:
    """Create one lease-owned root below the command's selected TMPDIR parent."""
    parent_value = command_env.get("TMPDIR") or tempfile.gettempdir()
    parent = Path(parent_value).expanduser().resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError(parent)
    root = Path(tempfile.mkdtemp(prefix="hearting-verification-", dir=parent))
    info = root.lstat()
    return root, (info.st_dev, info.st_ino)


def remove_private_scratch(root: Path, identity: tuple[int, int]) -> str | None:
    """Remove only the original private directory; return a reason if retained."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"root-stat-failed:{type(exc).__name__}"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "root-replaced"
    if (info.st_dev, info.st_ino) != identity:
        return "root-replaced"
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return f"remove-failed:{reason.replace(' ', '_')}"
    return None


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


def end_scope(scope: str | None, *, grace: float = 2.0,
              observed: dict[int, int | None] | None = None) -> list[int]:
    """Terminate everything still in this run's scope; a killed parent can
    re-parent its own children here, so repeat until nothing is left."""
    observed = {} if observed is None else observed
    ended: list[int] = []
    for _round in range(5):
        snapshot = process_snapshot()
        pids = _remember_scope(snapshot, scope, observed)
        live, _unknown = _owned_process_state(observed)
        pids.update(live)
        if not pids:
            break
        for sig in (signal.SIGTERM, signal.SIGKILL):
            signaled: set[int] = set()
            for pid in pids:
                expected_start = observed.get(pid)
                if expected_start is None:
                    continue
                try:
                    state, _ppid, _rss, current_start = _read_process_stat(pid)
                    if current_start != expected_start or state in (b"Z", b"X"):
                        continue
                    os.kill(pid, sig)
                    signaled.add(pid)
                except (OSError, IndexError, ValueError):
                    pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                reap_zombies()
                current = process_snapshot()
                _remember_scope(current, scope, observed)
                live, _unknown = _owned_process_state(observed)
                if not live:
                    break
                time.sleep(.05)
            else:
                continue
            break
        ended.extend(sorted(signaled))
        reap_zombies()
    return ended


def wait_within_budget(process: subprocess.Popen[bytes], timeout: float, scope: str,
                       budget: int | None = None, source: str = "default",
                       peak: MemoryPeak | None = None, on_peak=None,
                       observed: dict[int, int | None] | None = None) -> int | None:
    """The command's exit status; None on expiry. Raises MemoryError past the budget."""
    budget = memory_budget_bytes() if budget is None else budget
    peak = MemoryPeak() if peak is None else peak
    overlap = 0
    next_pss = 0.0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            return process.wait(timeout=min(MEMORY_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            pass
        snapshot = process_snapshot()
        table = _live_process_table(snapshot)
        pids = scope_pids(table, scope)
        if observed is not None:
            for pid in pids:
                identity = snapshot.get(pid)
                if identity is not None:
                    observed.setdefault(pid, identity[3])
        rss_sum = sum(table[pid][1] for pid in pids)
        peak.record_rss(rss_sum)
        if rss_sum - overlap > budget or time.monotonic() >= next_pss:
            started = time.monotonic()
            measured, measure = measure_scope(table, pids)
            cost = time.monotonic() - started
            overlap = max(0, rss_sum - measured)
            period = max(MEMORY_POLL_SECONDS, PSS_DUTY_FACTOR * cost)
            next_pss = time.monotonic() + period
            peak.record(measured, measure, period)
            if on_peak is not None and peak.json_due(budget):
                on_peak(peak, measure)
            if measured > budget:
                raise MemoryError(f"verification-memory-budget-exceeded resident_bytes={measured} "
                                  f"budget_bytes={budget} budget_source={source} measure={measure}")


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
    parser.add_argument("--memory-budget", metavar="SIZE", help="approved scope memory ceiling")
    parser.add_argument("--require-subreaper", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if args.timeout <= 0 or not command:
        parser.error("a positive --timeout and command are required")
    try:
        budget, source = resolve_memory_budget(args.memory_budget)
    except ValueError as exc:
        parser.error(f"--memory-budget {exc}")
    try:
        from verification_runtime import verification_command
        command, command_env = verification_command(command)
    except (OSError, ValueError, KeyError) as exc:
        print(f"loopback-only-unsupported: {exc}", file=sys.stderr)
        return 69
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
    scratch_root: Path | None = None
    scratch_identity: tuple[int, int] | None = None
    observed: dict[int, int | None] = {}
    peak = MemoryPeak()
    on_peak = None
    status = 1
    _CLEANUP_ACTIVE = False
    _DEFERRED_STOP = None
    _LAUNCHING = True
    try:
        scratch_root, scratch_identity = create_private_scratch(command_env)
        command_env = {**command_env, "TMPDIR": str(scratch_root)}
        # A stop delivered between Popen's return and the assignment must not
        # lose the child identity. Defer only that window; all setup after it
        # is already inside this lifetime's cleanup boundary.
        # Not passed to detached_lifetime.bind: this lease is a subreaper that ends
        # its whole scope itself, and a test reaper started here would sit inside it.
        process = subprocess.Popen(command, start_new_session=True,
                                   env={**command_env, SCOPE_ENV: scope})
        _LAUNCHING = False
        try:
            observed[process.pid] = _read_process_stat(process.pid)[3]
        except (OSError, IndexError, ValueError):
            observed[process.pid] = None
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
                              "pgid": process.pid, "memory_budget_bytes": budget,
                              "memory_budget_source": source})
                if args.memory_budget is not None:
                    value["memory_budget_option"] = args.memory_budget
                atomic_json(lease_path, value)
            else:
                lease_path = None
        if lease_path is not None:
            def on_peak(current: MemoryPeak, measure: str) -> None:
                value.update(memory_measure=measure, memory_peak_bytes=current.bytes,
                             memory_peak_measure=current.measure)
                try:
                    atomic_json(lease_path, value)
                except OSError:
                    return
                current.written = current.bytes
        status = wait_within_budget(process, args.timeout, scope, budget, source,
                                    peak, on_peak, observed)
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
        print(peak.line(budget, source), file=sys.stderr, flush=True)
        try:
            if process is not None and process.poll() is None:
                terminate_group(process)
            ended = end_scope(scope, observed=observed)
            snapshot = process_snapshot()
            _remember_scope(snapshot, scope, observed)
            live, unknown = _owned_process_state(observed)
            incomplete = live | unknown.keys()
            if ended:
                print(f"verification-scope-ended pids={','.join(map(str, ended))}",
                      file=sys.stderr)
            if incomplete:
                details = ",".join(f"{pid}:{'live' if pid in live else unknown[pid]}"
                                    for pid in sorted(incomplete))
                print(f"verification-scope-cleanup-incomplete pids={details}",
                      file=sys.stderr)
                if scratch_root is not None:
                    print(f"verification-scratch-preserved path={scratch_root} "
                          f"reason=owned-scope-incomplete identities={details}",
                          file=sys.stderr)
                status = SCOPE_CLEANUP_FAILED_EXIT
            elif scratch_root is not None and scratch_identity is not None:
                reason = remove_private_scratch(scratch_root, scratch_identity)
                if reason is not None:
                    print(f"verification-scratch-preserved path={scratch_root} reason={reason}",
                          file=sys.stderr)
                    status = SCOPE_CLEANUP_FAILED_EXIT
        except Exception as exc:
            print(f"verification-scope-cleanup-incomplete reason={type(exc).__name__}",
                  file=sys.stderr)
            if scratch_root is not None:
                print(f"verification-scratch-preserved path={scratch_root} "
                      f"reason=scope-cleanup-unknown:{type(exc).__name__}", file=sys.stderr)
            status = SCOPE_CLEANUP_FAILED_EXIT
        finally:
            if lease_path is not None:
                try:
                    lease_path.unlink()
                except FileNotFoundError:
                    pass
    return status


if __name__ == "__main__":
    raise SystemExit(main())
