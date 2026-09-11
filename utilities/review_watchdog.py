#!/usr/bin/env python3
"""Common detached review watchdog and its private admission protocol.

The watchdog is the registered identity.  The fenced child is a separate
session/group used only for readiness and exact timeout teardown.  This module
cannot close registry rows. It seals only its exact process result for the reaper.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Mapping, Sequence

from dispatch_contract import (
    DispatchContractError,
    REVIEW_GOVERNED_LEASE_KIND,
    REVIEW_GOVERNED_LEASE_NONCE_RE,
    _parse_review_metadata,
    process_group_observation,
    process_start_ticks,
    attempt_tagged_descendants,
    process_launch_identity,
    signal_exact_process_group,
    seal_detached_review_result,
)
from dispatch_lifecycle import (
    FiniteWatchdogBudget,
    remaining_watchdog_seconds,
)


READY = b"READY\n"
COMMIT = b"COMMIT\n"
ABORT = b"ABORT\n"
_MAX_RECEIPT = 16 * 1024
_MAX_CONTROL = 128
_WITNESS_UNLOCK_TIMEOUT = 1.0
_WITNESS_POLL_INTERVAL = 0.02


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _budget_json(budget: FiniteWatchdogBudget) -> str:
    return json.dumps(
        {
            "timeout_seconds": budget.timeout_seconds,
            "origin_monotonic_ns": budget.origin_monotonic_ns,
            "deadline_monotonic_ns": budget.deadline_monotonic_ns,
            "origin_epoch": budget.origin_epoch,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _budget_from_json(raw: str) -> FiniteWatchdogBudget:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("watchdog-budget-invalid")
    return FiniteWatchdogBudget(
        timeout_seconds=float(value["timeout_seconds"]),
        origin_monotonic_ns=int(value["origin_monotonic_ns"]),
        deadline_monotonic_ns=int(value["deadline_monotonic_ns"]),
        origin_epoch=float(value["origin_epoch"]),
    )


def _write_once(fd: int, payload: bytes) -> None:
    if len(payload) > _MAX_RECEIPT:
        raise ValueError("watchdog-receipt-too-large")
    written = os.write(fd, payload)
    if written != len(payload):
        raise OSError("watchdog-short-write")


def _validated_nonce(value: object) -> str:
    if not isinstance(value, str) or REVIEW_GOVERNED_LEASE_NONCE_RE.fullmatch(value) is None:
        raise ValueError("watchdog-nonce-invalid")
    return value


def _set_parent_death_signal() -> bool:
    """Couple watchdog death to its fenced child where Linux permits it."""

    if sys.platform != "linux":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        # Linux PR_SET_PDEATHSIG = 1.
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl")
        return True
    except (OSError, ImportError):
        return False


def _identity(pid: int) -> dict[str, str]:
    value = process_launch_identity(pid)
    if not value.get("pid_start") or value.get("pgid") != str(pid):
        raise ValueError("watchdog-identity-unavailable")
    return value


def _close(fd: int | None) -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _group_empty(pgid: int) -> bool:
    return process_group_observation(pgid).state == "empty"


def _drain_owned_residue(child_meta: Mapping[str, str], attempt_id: str, grace: float) -> bool:
    """Drain exact group members and attempt-tagged setsid descendants by pidfd."""
    def observed():
        group = process_group_observation(int(child_meta["pid"]))
        tagged = attempt_tagged_descendants({**child_meta, "attempt_id": attempt_id})
        if group.state not in {"empty", "populated"} or tagged.state not in {"empty", "populated"}:
            return None
        if group.reason or tagged.reason:
            return None
        return {(pid, start) for pid, start, state in (*group.members, *tagged.members)
                if pid != os.getpid() and state != "Z"}

    for signum in (signal.SIGTERM, signal.SIGKILL):
        members = observed()
        if members is None:
            return False
        if not members:
            return True
        for pid, start in sorted(members, reverse=True):
            try:
                fd = os.pidfd_open(pid)
                try:
                    if process_start_ticks(pid) != start:
                        return False
                    signal.pidfd_send_signal(fd, signum)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                continue
            except (OSError, AttributeError):
                return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            members = observed()
            if members is None:
                return False
            if not members:
                return True
            time.sleep(.02)
    return observed() == set()


def _reap_child(child: subprocess.Popen, child_meta: Mapping[str, str], *, grace: float = 0.75, attempt_id: str | None = None) -> bool:
    if child.poll() is None:
        if signal_exact_process_group(int(child_meta["pid"]), child_meta["pid_start"], signal.SIGTERM) != "signalled":
            return False
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            if signal_exact_process_group(int(child_meta["pid"]), child_meta["pid_start"], signal.SIGKILL) != "signalled":
                return False
            try:
                child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
    try:
        child.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        return False
    if attempt_id is not None:
        return _drain_owned_residue(child_meta, attempt_id, grace)
    return _group_empty(int(child_meta["pid"]))


def _preserve_signal_exit(returncode: int) -> int:
    """Terminate this watchdog with a child's signal status after cleanup."""

    if returncode >= 0:
        return returncode
    signum = -returncode
    try:
        signal.Signals(signum)
    except ValueError:
        return 126
    # A caller can inherit an ignored signal disposition.  Restore the
    # default where Python permits it; SIGKILL/SIGSTOP cannot be changed and
    # are still safe to deliver directly below.
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        os.kill(os.getpid(), signum)
    except OSError:
        return 126
    # A successful self-signal does not return.  Keep a safety result for
    # unusual platforms where delivery is deferred or otherwise suppressed.
    return 126


def _bind_launch_fence_parent(child_argv: Sequence[str], parent_pid: int) -> list[str]:
    """Make a future adapter-supplied fence observe this watchdog as parent."""

    argv = [str(item) for item in child_argv]
    if not any(item.endswith("launch-fence.py") for item in argv):
        return argv
    try:
        index = argv.index("--parent-pid")
        argv[index + 1] = str(parent_pid)
    except (ValueError, IndexError) as exc:
        raise ValueError("watchdog-launch-fence-parent-missing") from exc
    try:
        index = argv.index("--post-release-parent-death-signal")
        argv[index + 1] = "kill"
    except (ValueError, IndexError):
        marker = argv.index("--") if "--" in argv else len(argv)
        argv[marker:marker] = ["--post-release-parent-death-signal", "kill"]
    return argv


def _timeout_authority(
    watchdog: Mapping[str, str], child: Mapping[str, str],
    receipt: Mapping[str, object], budget: FiniteWatchdogBudget,
    jobs: str | Path | None = None,
) -> bool:
    """Revalidate exact PID/start/PGID/namespace before timeout signalling."""

    nonce = receipt.get("nonce")
    if (
        receipt.get("budget_digest") != budget.digest
        or receipt.get("deadline_monotonic_ns") != budget.deadline_monotonic_ns
        or not isinstance(receipt.get("attempt_id"), str)
        or not _is_receipt_digest_valid(receipt)
    ):
        return False
    try:
        nonce = _validated_nonce(nonce)
    except ValueError:
        return False
    try:
        current_watchdog = _identity(os.getpid())
        current_child = _identity(int(str(child["pid"])))
    except (OSError, TypeError, ValueError, KeyError):
        return False
    for expected, current in ((watchdog, current_watchdog), (child, current_child)):
        for key in ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns"):
            if str(expected.get(key, "")) != str(current.get(key, "")):
                return False
    if jobs is None:
        return False
    try:
        matches: list[dict[str, str]] = []
        for line in Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            metadata = _parse_review_metadata(fields[5])
            if metadata.get("attempt_id") == receipt.get("attempt_id"):
                matches.append(metadata)
        if len(matches) != 1:
            return False
        metadata = matches[0]
        expected_row = {
            "pid": watchdog.get("pid"), "pid_start": watchdog.get("pid_start"),
            "pgid": watchdog.get("pgid"), "pid_ns": watchdog.get("pid_ns"),
            "pid_observer_ns": watchdog.get("pid_observer_ns"),
            "review_fence_pid": child.get("pid"),
            "review_fence_pid_start": child.get("pid_start"),
            "review_fence_pgid": child.get("pgid"),
            "review_fence_pid_ns": child.get("pid_ns"),
            "review_fence_pid_observer_ns": child.get("pid_observer_ns"),
            "review_watchdog_budget_digest": budget.digest,
            "review_readiness_digest": receipt.get("receipt_digest"),
            "review_governed_lease_nonce": nonce,
            "review_governed_lease": REVIEW_GOVERNED_LEASE_KIND,
            "attempt_id": receipt.get("attempt_id"),
            "launch_claimed": "1",
            "review_admission": "prepared",
        }
        if any(
            key not in metadata or str(metadata.get(key)) != str(value)
            for key, value in expected_row.items()
        ):
            return False
    except (DispatchContractError, OSError, UnicodeError, TypeError, ValueError):
        return False
    return True


def _is_receipt_digest_valid(receipt: Mapping[str, object]) -> bool:
    digest = receipt.get("receipt_digest")
    if not isinstance(digest, str):
        return False
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    return digest == _canonical_digest(unsigned)


def _release_review_lease(spec: Mapping[str, object] | None) -> bool:
    if not spec:
        return True
    try:
        from artifact_producer import review_lease_release
        result = review_lease_release(
            Path(str(spec["root"])),
            cycle_id=str(spec["cycle_id"]), attempt_id=str(spec["attempt_id"]),
        )
        return result.get("status") in {"released", "already-released"}
    except (BaseException, KeyError):
        return False


def _receipt(attempt_id: str, watchdog: Mapping[str, str], child: Mapping[str, str], budget: FiniteWatchdogBudget, nonce: str) -> dict[str, object]:
    nonce = _validated_nonce(nonce)
    body: dict[str, object] = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "watchdog": dict(watchdog),
        "child": dict(child),
        "budget_digest": budget.digest,
        "deadline_monotonic_ns": budget.deadline_monotonic_ns,
        "nonce": nonce,
    }
    body["receipt_digest"] = _canonical_digest(body)
    return body


def _run_watchdog(
    *, attempt_id: str, budget: FiniteWatchdogBudget, readiness_fd: int,
    control_fd: int, gate_fd: int, child_argv: Sequence[str],
    failure_fd: int | None = None,
    nonce: str,
    lease_release_spec: Mapping[str, object] | None = None,
    jobs: str | Path | None = None,
) -> int:
    nonce = _validated_nonce(nonce)
    # The detached watchdog outlives the short-lived adapter launcher.
    # Only its fenced child is coupled to watchdog death (below). Admission
    # before COMMIT is bounded by the control pipe and finite budget.
    watchdog = _identity(os.getpid())
    interrupted = [0]
    previous_signals = {}
    def request_shutdown(signum, _frame):
        interrupted[0] = signum
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_signals[signum] = signal.signal(signum, request_shutdown)
    pass_fds = [gate_fd]
    if failure_fd is not None:
        pass_fds.append(failure_fd)
    child = None
    child_meta = None
    try:
        def require_parent_death_signal() -> None:
            if not _set_parent_death_signal():
                raise RuntimeError("watchdog-child-parent-death-unavailable")

        child = subprocess.Popen(
            _bind_launch_fence_parent(child_argv, os.getpid()),
            start_new_session=True,
            preexec_fn=require_parent_death_signal if os.name == "posix" else None,
            pass_fds=tuple(pass_fds),
            close_fds=True,
            env={**os.environ, "AGENT_DISPATCH_ATTEMPT_ID": attempt_id},
        )
        # Only the fence may retain this close-on-exec writer. Keeping a
        # watchdog copy would hide a successful payload exec from the adapter.
        _close(failure_fd)
        failure_fd = None
        child_meta = _identity(child.pid)
        receipt = _receipt(attempt_id, watchdog, child_meta, budget, nonce)
        _write_once(readiness_fd, (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode())
    except BaseException:
        _close(readiness_fd)
        _close(control_fd)
        _close(gate_fd)
        _close(failure_fd)
        reaped = child is None
        if child is not None and child_meta is not None:
            try:
                reaped = _reap_child(child, child_meta, attempt_id=attempt_id)
            except BaseException:
                reaped = False
        if reaped:
            _release_review_lease(lease_release_spec)
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        return 70 if reaped else 126
    finally:
        _close(readiness_fd)

    lease_release_allowed = True

    def reap_or_fail_closed() -> bool:
        nonlocal lease_release_allowed
        try:
            reaped = _reap_child(child, child_meta, attempt_id=attempt_id)
        except BaseException:
            reaped = False
        if not reaped:
            lease_release_allowed = False
        return reaped

    selector = selectors.DefaultSelector()
    try:
        selector.register(control_fd, selectors.EVENT_READ)
        token: bytes | None = None
        while token is None:
            if interrupted[0]:
                _close(control_fd)
                _close(gate_fd)
                return -interrupted[0] if reap_or_fail_closed() else 126
            if child.poll() is not None:
                reaped = reap_or_fail_closed()
                _close(control_fd)
                _close(gate_fd)
                if not reaped:
                    return 126
                return int(child.returncode or 0)
            remaining = remaining_watchdog_seconds(budget)
            if remaining <= 0:
                if not _timeout_authority(watchdog, child_meta, receipt, budget, jobs):
                    lease_release_allowed = False
                    _close(control_fd)
                    _close(gate_fd)
                    return 126
                if not reap_or_fail_closed():
                    _close(control_fd)
                    _close(gate_fd)
                    return 126
                _close(control_fd)
                _close(gate_fd)
                return 124
            wait = selector.select(timeout=min(0.1, remaining))
            if not wait:
                continue
            token = os.read(control_fd, _MAX_CONTROL)
        if token != COMMIT:
            # EOF, ABORT, malformed, duplicate, and trailing bytes all close
            # the gate and reap the exact fenced group.
            _close(gate_fd)
            reaped = reap_or_fail_closed()
            _close(control_fd)
            if not reaped:
                return 126
            return 125
        _close(control_fd)
        _close(gate_fd)
        # COMMIT never restarts the budget.  Once admitted, the child remains
        # governed by the same absolute deadline.
        while child.poll() is None:
            if interrupted[0]:
                return -interrupted[0] if reap_or_fail_closed() else 126
            remaining = remaining_watchdog_seconds(budget)
            if remaining <= 0:
                if not _timeout_authority(watchdog, child_meta, receipt, budget, jobs):
                    lease_release_allowed = False
                    return 126
                if not reap_or_fail_closed():
                    return 126
                return 124
            try:
                child.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
        child_returncode = int(child.returncode or 0)
        if not reap_or_fail_closed():
            return 126
        return child_returncode
    finally:
        selector.close()
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        if lease_release_allowed and not _release_review_lease(lease_release_spec):
            return 126


@dataclass
class ReviewWatchdogHandle:
    process: subprocess.Popen
    readiness_fd: int
    control_fd: int
    budget: FiniteWatchdogBudget
    attempt_id: str
    nonce: str
    _receipt: dict[str, object] | None = None
    _control_closed: bool = False
    _committing: bool = False

    def read_ready(self, timeout: float | None = None) -> dict[str, object]:
        if self._receipt is not None:
            raise ValueError("watchdog-readiness-duplicate")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.readiness_fd, selectors.EVENT_READ)
            wait = selector.select(timeout if timeout is not None else remaining_watchdog_seconds(self.budget))
            if not wait:
                raise TimeoutError("watchdog-readiness-timeout")
            raw = os.read(self.readiness_fd, _MAX_RECEIPT)
        finally:
            selector.close()
            _close(self.readiness_fd)
            self.readiness_fd = -1
        if not raw or raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
            raise ValueError("watchdog-readiness-malformed")
        receipt = json.loads(raw[:-1].decode("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("watchdog-readiness-malformed")
        digest = receipt.pop("receipt_digest", None)
        if not isinstance(digest, str) or digest != _canonical_digest(receipt):
            raise ValueError("watchdog-readiness-digest-mismatch")
        receipt["receipt_digest"] = digest
        if (
            receipt.get("attempt_id") != self.attempt_id
            or receipt.get("budget_digest") != self.budget.digest
            or receipt.get("nonce") != self.nonce
            or not _validated_nonce(receipt.get("nonce"))
            or receipt.get("deadline_monotonic_ns") != self.budget.deadline_monotonic_ns
        ):
            raise ValueError("watchdog-readiness-binding-mismatch")
        self._receipt = receipt
        return dict(receipt)

    @property
    def receipt(self) -> dict[str, object] | None:
        return None if self._receipt is None else dict(self._receipt)

    def _send(self, token: bytes) -> None:
        if self._control_closed:
            raise ValueError("watchdog-control-closed")
        if token not in (COMMIT, ABORT):
            raise ValueError("watchdog-control-token-invalid")
        try:
            written = os.write(self.control_fd, token)
            if written != len(token):
                raise OSError("watchdog-control-short-write")
        finally:
            _close(self.control_fd)
            self.control_fd = -1
            self._control_closed = True

    def close_control(self) -> None:
        _close(self.control_fd)
        self.control_fd = -1
        self._control_closed = True

    def commit(self) -> None:
        self._committing = True
        self._send(COMMIT)

    def abort(self) -> None:
        self._send(ABORT)


def launch_review_watchdog(
    child_argv: Sequence[str], *, gate_fd: int, budget: FiniteWatchdogBudget,
    attempt_id: str, failure_fd: int | None = None,
    nonce: str,
    env: Mapping[str, str] | None = None,
    lease_release_spec: Mapping[str, object] | None = None,
    jobs: str | Path | None = None,
) -> ReviewWatchdogHandle:
    """Start a detached watchdog and return only the launcher-owned handles."""

    if not child_argv or not isinstance(budget, FiniteWatchdogBudget):
        raise ValueError("watchdog-launch-input-invalid")
    nonce = _validated_nonce(nonce)
    readiness_read, readiness_write = os.pipe()
    control_read, control_write = os.pipe()
    args = [
        sys.executable, str(Path(__file__).resolve()), "--run",
        "--attempt-id", attempt_id, "--budget", _budget_json(budget),
        "--nonce", nonce,
        "--readiness-fd", str(readiness_write), "--control-fd", str(control_read),
        "--gate-fd", str(gate_fd),
    ]
    pass_fds = [readiness_write, control_read, gate_fd]
    if failure_fd is not None:
        args.extend(["--failure-fd", str(failure_fd)])
        pass_fds.append(failure_fd)
    if lease_release_spec is not None:
        args.extend([
            "--lease-release",
            json.dumps(dict(lease_release_spec), sort_keys=True, separators=(",", ":")),
        ])
    if jobs is not None:
        args.extend(["--jobs", str(jobs)])
    args.extend(["--", *[str(item) for item in child_argv]])
    try:
        proc = subprocess.Popen(
            args, start_new_session=True, pass_fds=tuple(pass_fds), close_fds=True,
            env=None if env is None else dict(env),
        )
    except BaseException:
        for fd in (readiness_read, readiness_write, control_read, control_write):
            _close(fd)
        raise
    _close(readiness_write)
    _close(control_read)
    return ReviewWatchdogHandle(proc, readiness_read, control_write, budget, attempt_id, nonce)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "--run":
        return 64
    try:
        def value(name: str) -> str:
            index = args.index(name)
            return args[index + 1]
        marker = args.index("--")
        child = args[marker + 1:]
        result = _run_watchdog(
            attempt_id=value("--attempt-id"),
            budget=_budget_from_json(value("--budget")),
            nonce=value("--nonce"),
            readiness_fd=int(value("--readiness-fd")),
            control_fd=int(value("--control-fd")),
            gate_fd=int(value("--gate-fd")),
            child_argv=child,
            failure_fd=int(value("--failure-fd")) if "--failure-fd" in args[:marker] else None,
            lease_release_spec=(
                json.loads(value("--lease-release"))
                if "--lease-release" in args[:marker] else None
            ),
            jobs=value("--jobs") if "--jobs" in args[:marker] else None,
        )
        if "--jobs" in args[:marker]:
            try:
                seal_detached_review_result(
                    value("--jobs"), value("--attempt-id"),
                    budget_digest=_budget_from_json(value("--budget")).digest,
                    nonce=value("--nonce"), exit_code=result,
                )
            except (DispatchContractError, OSError, ValueError):
                return 126
        return _preserve_signal_exit(result)
    except (KeyError, ValueError, IndexError, OSError, RuntimeError, json.JSONDecodeError):
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
