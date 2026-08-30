#!/usr/bin/env python3
"""Namespace-safe lifecycle selection and foreground child supervision."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Callable, Mapping

from dispatch_contract import (
    GROUP_REAP_PROOF,
    process_group_observation,
    process_identity_is_live,
    process_start_ticks,
    signal_exact_process_group,
)

DETACHED = "detached"
FOREGROUND_SCOPED = "foreground-scoped"
LIFECYCLES = (DETACHED, FOREGROUND_SCOPED)

FOREGROUND_TIMEOUT_DEFAULT = 3600.0  # 1h: what a non-positive/non-finite request clamps to
FOREGROUND_TIMEOUT_MAX = 86400.0  # 24h hard ceiling: no finite request may be effectively infinite


def pid_namespace_evidence(
    status_path: Path = Path("/proc/self/status"),
    init_comm_path: Path = Path("/proc/1/comm"),
) -> dict[str, str]:
    """Return bounded, non-sensitive evidence used by lifecycle selection."""
    width = 0
    nspid_state = "unreadable"
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("NSpid:"):
                width = max(0, len(line.split()) - 1)
                nspid_state = "nested" if width > 1 else "single"
                break
        else:
            nspid_state = "absent"
    except OSError:
        pass
    try:
        comm = init_comm_path.read_text(encoding="utf-8").strip()
        init_class = "system-init" if comm in {"systemd", "init"} else "non-system-init"
    except OSError:
        init_class = "unreadable"
    if nspid_state == "nested":
        source = "nspid-vector"
    elif init_class == "non-system-init":
        source = "pid1-class"
    elif nspid_state == "unreadable" or init_class == "unreadable":
        source = "proc-unreadable"
    else:
        source = "host-like"
    return {
        "lifecycle_selector_source": source,
        "lifecycle_nspid_width": str(width),
        "lifecycle_pid1_class": init_class,
    }


def bounded_foreground_timeout(timeout: float) -> float:
    """Clamp a foreground wait to a finite window — it may never be indefinite.

    A foreground-scoped parent blocks on its child for the whole wait, so an
    unbounded wait is a hang hazard, not a valid choice: a wedged child would pin
    the parent forever with no visibility. Two ways in are closed here:
      * ``<= 0`` (the historical "disable timeout" sentinel) and any non-finite
        request — ``inf``/``nan``, both accepted by ``argparse type=float`` — clamp
        to the safe default;
      * any finite request above the hard ceiling clamps down to it, so even an
        absurd value like ``1e18`` cannot be effectively infinite.
    (A no-progress watchdog that tells slow-but-progressing apart from wedged is
    the planned follow-up; until it lands, a finite window is the floor of safety.)
    """

    if not math.isfinite(timeout) or timeout <= 0:
        return FOREGROUND_TIMEOUT_DEFAULT
    return min(timeout, FOREGROUND_TIMEOUT_MAX)


def pid_namespace_scoped(
    status_path: Path = Path("/proc/self/status"),
    init_comm_path: Path = Path("/proc/1/comm"),
) -> bool:
    """Detect a transient nested PID namespace conservatively.

    A nested ``NSpid`` vector is authoritative. When proc is remounted inside
    the namespace, a non-init PID 1 is the fallback signal. An unreadable proc
    fails safe because a detached child cannot then be proven durable.
    """

    evidence = pid_namespace_evidence(status_path, init_comm_path)
    return evidence["lifecycle_selector_source"] in {
        "nspid-vector", "pid1-class", "proc-unreadable"
    }


TRANSIENT_SELECTOR_SOURCES = ("nspid-vector", "pid1-class", "proc-unreadable")
HOST_LIKE_SELECTOR_SOURCE = "host-like"
SANDBOXED_PARENT_SANDBOXES = ("workspace-write",)


def _override_admissible(
    env: Mapping[str, str], evidence: Mapping[str, str], parent_sandbox: str | None
) -> bool:
    """Whether an ``AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN`` assertion may be honored.

    Override presence is a precondition checked by the caller. It is admissible
    only for a launcher whose own observed scope is host-like (not one of the
    transient selector sources) and whose sealed parent sandbox is not a checked
    sandboxed value — a registered headless owner inside a tool sandbox cannot
    assert that its PID namespace outlives the tool call.
    """

    if evidence.get("lifecycle_selector_source") != HOST_LIKE_SELECTOR_SOURCE:
        return False
    sandbox = parent_sandbox if parent_sandbox is not None else env.get(
        "AGENT_DISPATCH_CURRENT_SANDBOX", ""
    )
    return sandbox not in SANDBOXED_PARENT_SANDBOXES


def select_launch_lifecycle(
    environ: Mapping[str, str] | None = None,
    *,
    namespace_scoped: bool | None = None,
    parent_sandbox: str | None = None,
    evidence: Mapping[str, str] | None = None,
) -> str:
    """Choose the lifecycle for an actual dispatch-chain launcher scope."""

    env = os.environ if environ is None else environ
    if evidence is not None:
        observed = dict(evidence)
    elif namespace_scoped is not None:
        observed = {
            "lifecycle_selector_source": "pid1-class" if namespace_scoped else "host-like"
        }
    else:
        observed = pid_namespace_evidence()
    if env.get("AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN") == "1":
        return DETACHED if _override_admissible(env, observed, parent_sandbox) else FOREGROUND_SCOPED
    scoped = observed.get("lifecycle_selector_source") in TRANSIENT_SELECTOR_SOURCES
    return FOREGROUND_SCOPED if scoped else DETACHED


@dataclass(frozen=True)
class LifecycleResolution:
    requested: str
    effective: str
    reselection: str
    evidence: dict[str, str]
    override: str = "absent"

    def metadata(self) -> dict[str, str]:
        return {
            "launch_lifecycle_requested": self.requested,
            "launch_lifecycle": self.effective,
            "launch_lifecycle_reselection": self.reselection,
            "launch_lifecycle_override": self.override,
            **self.evidence,
        }


def reconcile_launch_lifecycle(
    requested: str,
    environ: Mapping[str, str] | None = None,
    *,
    evidence: Mapping[str, str] | None = None,
    parent_sandbox: str | None = None,
) -> LifecycleResolution:
    """Re-evaluate a provisional caller selection in the wrapper's scope."""

    if requested not in LIFECYCLES:
        raise ValueError(f"unknown launch lifecycle: {requested}")
    env = os.environ if environ is None else environ
    observed = dict(evidence) if evidence is not None else pid_namespace_evidence()
    scoped = observed.get("lifecycle_selector_source") in TRANSIENT_SELECTOR_SOURCES
    override_set = env.get("AGENT_DISPATCH_ALLOW_NAMESPACED_SPAWN") == "1"
    if override_set:
        admissible = _override_admissible(env, observed, parent_sandbox)
        actual = DETACHED if admissible else FOREGROUND_SCOPED
        override = "honored" if admissible else "rejected"
    else:
        actual = FOREGROUND_SCOPED if scoped else DETACHED
        override = "absent"
    effective = (
        FOREGROUND_SCOPED
        if requested == DETACHED and actual == FOREGROUND_SCOPED
        else requested
    )
    if override == "rejected":
        reselection = "override-rejected-transient-scope"
    elif effective != requested:
        reselection = "promoted-wrapper-scope"
    else:
        reselection = "retained-wrapper-scope"
    return LifecycleResolution(
        requested=requested,
        effective=effective,
        reselection=reselection,
        evidence=observed,
        override=override,
    )


@dataclass(frozen=True)
class ForegroundResult:
    exit_code: int
    failure: str
    group_empty: bool = True


def _group_empty(pgid: int) -> bool | None:
    observation = process_group_observation(pgid)
    if observation.state == "empty":
        return True
    if observation.state == "populated":
        return False
    return None


def _terminate_group(proc: subprocess.Popen, signum: int, leader_start: str) -> str:
    return signal_exact_process_group(proc.pid, leader_start, signum)


def _wait_group_empty(pgid: int, deadline: float, poll_interval: float) -> bool:
    while time.monotonic() < deadline:
        if _group_empty(pgid) is True:
            return True
        time.sleep(max(0.01, min(poll_interval, deadline - time.monotonic())))
    return _group_empty(pgid) is True


def _stop_direct_child(proc: subprocess.Popen, grace: float = 0.5) -> None:
    """Best-effort cleanup when exact group identity cannot be established."""

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
        proc.wait(timeout=grace)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _bounded_group_stop(
    proc: subprocess.Popen,
    leader_start: str,
    *,
    grace: float = 5.0,
    poll_interval: float = 0.05,
) -> tuple[int, bool]:
    _terminate_group(proc, signal.SIGTERM, leader_start)
    empty = _wait_group_empty(proc.pid, time.monotonic() + grace, poll_interval)
    if not empty:
        _terminate_group(proc, signal.SIGKILL, leader_start)
        empty = _wait_group_empty(proc.pid, time.monotonic() + grace, poll_interval)
    try:
        exit_code = proc.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        exit_code = proc.poll()
    return (exit_code if exit_code is not None else -signal.SIGKILL), empty


@dataclass(frozen=True)
class PostExitOutcome:
    launch_outcome: str
    group_reap_proof: str = ""
    group_reap_pgid: str = ""


def deterministic_post_exit_outcome(
    proc: subprocess.Popen,
    *,
    fence_released: bool,
    grace: float = 1.0,
    poll_interval: float = 0.05,
) -> PostExitOutcome:
    """Classify a killed, fenced process's post-exit state deterministically.

    The caller has already terminated and waited on the exact process group
    (mirroring `_bounded_group_stop`'s own termination) before calling this.
    Returns the proved foreground receipt when the exact group is proved
    empty, or `never-launched` when the launch fence was never released so no
    payload can have executed. Only the case neither of those two can prove
    (fence released, group emptiness unprovable within the grace window)
    falls back to an empty outcome so the caller preserves its prior
    unverified behavior instead of fabricating a claim it cannot prove.
    """

    if not fence_released:
        return PostExitOutcome(launch_outcome="never-launched")
    if _wait_group_empty(proc.pid, time.monotonic() + grace, poll_interval):
        return PostExitOutcome(
            launch_outcome="governed-process-reaped",
            group_reap_proof=GROUP_REAP_PROOF,
            group_reap_pgid=str(proc.pid),
        )
    return PostExitOutcome(launch_outcome="")


def wait_foreground(
    proc: subprocess.Popen,
    timeout: float,
    *,
    parent_pid: int | None = None,
    parent_pid_start: str | None = None,
    parent_is_live: Callable[[], bool] | None = None,
    poll_interval: float = 0.2,
) -> ForegroundResult:
    """Wait in scope, forwarding termination and returning a typed outcome."""

    received: list[int] = []
    previous: dict[int, object] = {}
    leader_start = process_start_ticks(proc.pid)
    if not leader_start:
        _stop_direct_child(proc)
        return ForegroundResult(
            proc.poll() if proc.poll() is not None else -1,
            "process-identity-unavailable",
            False,
        )

    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        _terminate_group(proc, signum, leader_start)

    forwarded_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        forwarded_signals.append(signal.SIGHUP)
    for signum in forwarded_signals:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)
    try:
        bounded_timeout = bounded_foreground_timeout(timeout)
        deadline = time.monotonic() + bounded_timeout
        while True:
            exit_code = proc.poll()
            group_empty = _group_empty(proc.pid)
            if exit_code is not None and group_empty is True:
                break
            if received:
                exit_code, group_empty = _bounded_group_stop(
                    proc, leader_start, poll_interval=poll_interval
                )
                return ForegroundResult(
                    exit_code, f"signal-{received[-1]}", group_empty
                )
            parent_lost = False
            if parent_is_live is not None:
                parent_lost = not parent_is_live()
            elif parent_pid is not None and parent_pid_start:
                parent_lost = not process_identity_is_live(
                    parent_pid, parent_pid_start
                )
            if parent_lost:
                exit_code, group_empty = _bounded_group_stop(
                    proc, leader_start, poll_interval=poll_interval
                )
                return ForegroundResult(exit_code, "parent-terminated", group_empty)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                exit_code, group_empty = _bounded_group_stop(
                    proc, leader_start, poll_interval=poll_interval
                )
                return ForegroundResult(exit_code, "timeout", group_empty)
            time.sleep(min(max(poll_interval, 0.01), remaining))
    except BaseException:
        try:
            _exit_code, group_empty = _bounded_group_stop(
                proc, leader_start, poll_interval=poll_interval
            )
            if not group_empty:
                _stop_direct_child(proc)
        except BaseException:
            _stop_direct_child(proc)
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    if received:
        return ForegroundResult(exit_code, f"signal-{received[-1]}")
    if exit_code < 0:
        return ForegroundResult(exit_code, f"signal-{-exit_code}")
    if exit_code:
        return ForegroundResult(exit_code, f"exit-{exit_code}")
    return ForegroundResult(exit_code, "")
