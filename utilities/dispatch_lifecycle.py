#!/usr/bin/env python3
"""Namespace-safe lifecycle selection and foreground child supervision."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import frame_repository
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from dispatch_contract import (
    DispatchContractError,
    GROUP_REAP_PROOF,
    PostClaimAdmission,
    ReviewAdmissionCleanup,
    process_group_observation,
    attempt_scan_namespace_authority,
    attempt_tagged_descendants,
    process_identity_is_live,
    process_start_ticks,
    signal_exact_process_group,
)

DETACHED = "detached"
FOREGROUND_SCOPED = "foreground-scoped"
LIFECYCLES = (DETACHED, FOREGROUND_SCOPED)

FOREGROUND_TIMEOUT_DEFAULT = 14400.0  # 4h: the default wait, and what a non-positive/non-finite request clamps to
FOREGROUND_TIMEOUT_MAX = 86400.0  # 24h hard ceiling: no finite request may be effectively infinite
_REVIEW_WITNESS_UNLOCK_TIMEOUT = 1.0
_REVIEW_WITNESS_POLL_INTERVAL = 0.02

# The wrapper itself was asked to stop (SIGINT/SIGTERM/SIGHUP) and stopped its
# worker. A worker that died of a signal on its own stays `signal-N`.
FOREGROUND_INTERRUPTED = "interrupted"
# One stderr line every launcher prints first on a foreground-scoped `--start`:
# the call it is in hosts the worker, so a caller that stops it stops the work.
FOREGROUND_NOTICE = (
    "notice: the worker runs inside this call — wait until this call returns "
    "(it can take a while); stopping the call stops the worker."
)
# One plain line for a caller whose `--start` found its attempt already
# claimed; an attempt that is still running needs no extra line.
EXISTING_ATTEMPT_NOTES = {
    "existing-dead": (
        "the earlier attempt is no longer running; run the same --start again "
        "and a replacement starts"
    ),
    "existing-unverified": (
        "this place cannot see whether the earlier attempt is still running; the "
        "runtime checks it and replaces it if it died — end the turn and wait"
    ),
}
# Time a launcher gives its wrapper to stop the worker and close the row after
# the first stop request: the wrapper's TERM (5s) and KILL (5s) windows plus
# room for the registry close.
FORWARDED_TERMINATION_GRACE = 30.0
FORWARDED_POST_EXIT_DRAIN_GRACE = 0.25


@dataclass(frozen=True)
class FiniteWatchdogBudget:
    """One immutable launch-origin budget shared by every lifecycle consumer.

    ``deadline_monotonic_ns`` is the only enforcement clock.  The epoch value
    is retained solely as an audit/cross-process representation; consumers must
    not derive a second deadline from it.
    """

    timeout_seconds: float
    origin_monotonic_ns: int
    deadline_monotonic_ns: int
    origin_epoch: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > FOREGROUND_TIMEOUT_MAX
            or isinstance(self.origin_monotonic_ns, bool)
            or not isinstance(self.origin_monotonic_ns, int)
            or isinstance(self.deadline_monotonic_ns, bool)
            or not isinstance(self.deadline_monotonic_ns, int)
            or self.origin_monotonic_ns < 0
            or self.deadline_monotonic_ns != self.origin_monotonic_ns + int(self.timeout_seconds * 1_000_000_000)
            or isinstance(self.origin_epoch, bool)
            or not isinstance(self.origin_epoch, (int, float))
            or not math.isfinite(self.origin_epoch)
        ):
            raise ValueError("invalid-finite-watchdog-budget")

    @property
    def digest(self) -> str:
        payload = {
            "timeout_seconds": self.timeout_seconds,
            "origin_monotonic_ns": self.origin_monotonic_ns,
            "deadline_monotonic_ns": self.deadline_monotonic_ns,
            "origin_epoch": self.origin_epoch,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


def begin_finite_watchdog(
    raw_timeout: object = None, *, origin_monotonic_ns: int | None = None,
    origin_epoch: float | None = None,
) -> FiniteWatchdogBudget:
    """Normalize once and start the authoritative launch-origin clock."""

    try:
        requested = float(raw_timeout) if raw_timeout is not None else float("nan")
    except (TypeError, ValueError, OverflowError):
        requested = float("nan")
    if not math.isfinite(requested) or requested <= 0:
        requested = FOREGROUND_TIMEOUT_DEFAULT
    else:
        requested = min(requested, FOREGROUND_TIMEOUT_MAX)
    origin_ns = time.monotonic_ns() if origin_monotonic_ns is None else origin_monotonic_ns
    if isinstance(origin_ns, bool) or not isinstance(origin_ns, int) or origin_ns < 0:
        raise ValueError("invalid-watchdog-origin-monotonic")
    epoch = time.time() if origin_epoch is None else origin_epoch
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)) or not math.isfinite(float(epoch)):
        raise ValueError("invalid-watchdog-origin-epoch")
    return FiniteWatchdogBudget(
        timeout_seconds=requested,
        origin_monotonic_ns=origin_ns,
        deadline_monotonic_ns=origin_ns + int(requested * 1_000_000_000),
        origin_epoch=float(epoch),
    )


def remaining_watchdog_seconds(
    budget: FiniteWatchdogBudget, *, now_monotonic_ns: int | None = None
) -> float:
    """Return remaining time from an existing budget without starting a clock."""

    if not isinstance(budget, FiniteWatchdogBudget):
        raise TypeError("watchdog-budget-required")
    now = time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
    return max(0.0, (budget.deadline_monotonic_ns - now) / 1_000_000_000)


def launch_review_watchdog(*args, **kwargs):
    """Common launcher seam kept here for adapter callers.

    The import is intentionally lazy: ``dispatch_contract`` is a dependency of
    this module, while the watchdog implementation uses this budget module.
    """

    from review_watchdog import launch_review_watchdog as _launch

    return _launch(*args, **kwargs)


class ReviewAdmissionError(DispatchContractError):
    """A typed, already-cleaned failure while joining watchdog admission."""

    def __init__(self, reason: str, detail: str, cleanup: ReviewAdmissionCleanup):
        super().__init__(reason, detail)
        self.cleanup = cleanup


def _review_cleanup(
    handle: Any,
    *,
    child: Mapping[str, object] | None,
    lease_acquired: bool,
    lease_release: Callable[[], Any] | None,
    witness_probe: Callable[[], bool] | None,
    payload_marker: str = "absent",
) -> ReviewAdmissionCleanup:
    """Close external admission resources without touching the jobs lock."""

    control_ok = True
    committing = bool(getattr(handle, "_committing", False))
    if not committing:
        try:
            if getattr(handle, "control_fd", -1) >= 0 and not getattr(handle, "_control_closed", False):
                handle.abort()
        except BaseException:
            control_ok = False
            try:
                handle.close_control()
            except BaseException:
                pass
    else:
        # A commit write may have reached the watchdog before its close fault.
        # Never send ABORT after that point; post-release cleanup is governed by
        # the sealed watchdog identity and the same absolute budget.
        try:
            handle.close_control()
        except BaseException:
            control_ok = False
    process = getattr(handle, "process", None)
    watchdog_ok = False
    if process is not None:
        receipt = getattr(handle, "receipt", None)
        expected = receipt.get("watchdog") if isinstance(receipt, Mapping) else None
        expected_identity = (
            expected if isinstance(expected, Mapping)
            and str(expected.get("pid", "")) == str(getattr(process, "pid", ""))
            and all(str(expected.get(key, "")) for key in (
                "pid_start", "pgid", "pid_ns", "pid_observer_ns"
            )) else None
        )
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            # The current /proc start value is never a cleanup target.  If the
            # sealed readiness identity is absent, no signal is safe.
            if expected_identity is not None:
                try:
                    if attempt_scan_namespace_authority(dict(expected_identity)):
                        signal_exact_process_group(
                            int(expected_identity["pid"]),
                            str(expected_identity["pid_start"]), signal.SIGTERM,
                        )
                    # TERM asks the finite watchdog to drain its own fenced
                    # group and tagged descendants. Never kill that authority
                    # merely because this adapter's bounded join expires.
                    process.wait(timeout=5.0)
                except (BaseException, subprocess.TimeoutExpired):
                    pass
        try:
            watchdog_ok = (
                expected_identity is not None
                and attempt_scan_namespace_authority(dict(expected_identity))
                and process.poll() is not None
                and process_group_observation(int(expected_identity["pgid"])).state == "empty"
            )
        except (OSError, TypeError, ValueError):
            watchdog_ok = False
    child_ok = child is None
    if child is not None:
        try:
            child_pgid = int(str(child.get("pgid", "")))
            child_ok = (
                all(str(child.get(key, "")) for key in (
                    "pid_start", "pid_ns", "pid_observer_ns"
                ))
                and attempt_scan_namespace_authority(dict(child))
                and process_group_observation(child_pgid).state == "empty"
                and attempt_tagged_descendants({
                    **dict(child), "attempt_id": str(getattr(handle, "attempt_id", "")),
                }).state == "empty"
            )
        except (OSError, TypeError, ValueError):
            child_ok = False
    lease_state = "never-acquired"
    if lease_acquired:
        lease_state = "unverified"
        if watchdog_ok and child_ok and lease_release is not None:
            try:
                release_result = lease_release()
                if release_result is not False and (
                    not isinstance(release_result, Mapping)
                    or release_result.get("status") in {"released", "already-released"}
                ):
                    lease_state = "released"
            except BaseException:
                lease_state = "unverified"
    witness_state = "unlocked"
    if lease_acquired and witness_probe is None:
        witness_state = "unverified"
    elif witness_probe is not None:
        # This proof is intentionally after the exact watchdog wait/death and
        # is bounded by an absolute monotonic deadline.  A single probe can
        # race the watchdog's finally block; an unbounded wait would leak the
        # launch transaction.
        deadline = time.monotonic() + _REVIEW_WITNESS_UNLOCK_TIMEOUT
        witness_state = "unverified"
        while True:
            try:
                if bool(witness_probe()):
                    witness_state = "unlocked"
                    break
            except BaseException:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_REVIEW_WITNESS_POLL_INTERVAL, remaining))
    readiness_fd = getattr(handle, "readiness_fd", -1)
    if readiness_fd >= 0:
        _close(readiness_fd)
        handle.readiness_fd = -1
    readiness_state = "closed-removed" if getattr(handle, "readiness_fd", -1) < 0 else "unverified"
    verified = (
        control_ok and watchdog_ok and child_ok
        and readiness_state == "closed-removed"
        and lease_state in {"released", "never-acquired"}
        and witness_state == "unlocked"
    )
    post_release = committing
    return ReviewAdmissionCleanup(
        watchdog_group="empty" if watchdog_ok else "unverified",
        fenced_child_group="empty" if child_ok else "unverified",
        readiness=readiness_state,
        review_lease=lease_state,
        governed_witness=witness_state,
        payload_marker=("may-have-started" if post_release else payload_marker) if verified else "unverified",
        status=("verified-post-release-reaped" if post_release else "verified-never-launched") if verified else "unverified",
    )


def acquire_review_admission(
    *,
    handle: Any,
    budget: FiniteWatchdogBudget,
    identity: Mapping[str, str],
    root: str | Path,
    cycle_id: str,
    attempt_id: str,
    review_output: str | Path,
    binding: Mapping[str, object],
    jobs: str | Path,
    nonce: str,
    lease_acquire: Callable[..., Mapping[str, object]] | None = None,
    lease_release: Callable[[], Any] | None = None,
    witness_probe: Callable[[], bool] | None = None,
    readiness_timeout: float | None = None,
) -> PostClaimAdmission:
    """Join a watchdog and lease as one closed, adapter-neutral transaction.

    The callback parameters are deliberate dependency-injection seams.  The
    default lease import is lazy so the three adapters can call this helper
    without creating an import cycle or changing their public callback shape.
    """

    if not isinstance(budget, FiniteWatchdogBudget):
        raise DispatchContractError("review-admission-budget-invalid")
    if not isinstance(nonce, str) or len(nonce) != 64 or any(
        char not in "0123456789abcdef" for char in nonce
    ):
        raise DispatchContractError("review-admission-nonce-invalid")
    supplied_identity_nonce = identity.get("review_governed_lease_nonce")
    if supplied_identity_nonce not in (None, nonce):
        raise DispatchContractError("review-admission-nonce-mismatch")
    receipt: Mapping[str, object] | None = None
    lease_acquired = False
    if lease_acquire is None:
        from artifact_producer import review_lease_acquire as lease_acquire
    if lease_release is None:
        from artifact_producer import review_lease_release
        lease_release = lambda: review_lease_release(
            Path(root), cycle_id=cycle_id, attempt_id=attempt_id
        )
    try:
        receipt = handle.read_ready(timeout=readiness_timeout)
        watchdog = receipt.get("watchdog")
        child = receipt.get("child")
        if not isinstance(watchdog, Mapping) or not isinstance(child, Mapping):
            raise DispatchContractError("review-admission-readiness-invalid")
        for key in ("pid", "pid_start", "pgid", "pid_ns", "pid_observer_ns"):
            if str(watchdog.get(key, "")) != str(identity.get(key, "")):
                raise DispatchContractError("review-admission-watchdog-identity-mismatch", key)
            if not str(child.get(key, "")):
                raise DispatchContractError("review-admission-child-identity-incomplete", key)
        if receipt.get("budget_digest") != budget.digest:
            raise DispatchContractError("review-admission-budget-digest-mismatch")
        if receipt.get("deadline_monotonic_ns") != budget.deadline_monotonic_ns:
            raise DispatchContractError("review-admission-deadline-mismatch")
        if receipt.get("nonce") != nonce:
            raise DispatchContractError("review-admission-nonce-mismatch")
        governed_identity = dict(identity)
        governed_identity["review_governed_lease"] = "summary-flock-v1"
        governed_identity["review_governed_lease_nonce"] = nonce
        result = lease_acquire(
            Path(root), cycle_id=cycle_id, attempt_id=attempt_id,
            review_output=review_output, binding=binding,
            deadline_seconds=budget.timeout_seconds,
            governed_identity=governed_identity, jobs=jobs, watchdog_budget=budget,
        )
        if not isinstance(result, Mapping) or result.get("status") not in {"acquired", "already-held"}:
            raise DispatchContractError("review-admission-lease-not-acquired")
        lease_acquired = True
        registry_metadata = result.get("registry_metadata")
        if not isinstance(registry_metadata, Mapping):
            raise DispatchContractError("review-admission-lease-metadata-missing")
        child_fields = {
            "review_admission": "prepared",
            "review_watchdog_budget_digest": budget.digest,
            "review_readiness_digest": str(receipt["receipt_digest"]),
            "review_fence_pid": str(child["pid"]),
            "review_fence_pid_start": str(child["pid_start"]),
            "review_fence_pgid": str(child["pgid"]),
            "review_fence_pid_ns": str(child["pid_ns"]),
            "review_fence_pid_observer_ns": str(child["pid_observer_ns"]),
            "review_governed_lease_nonce": nonce,
        }
        metadata = {
            key: str(value) for key, value in registry_metadata.items()
            if value not in (None, "")
        }
        metadata.update(child_fields)
        cleanup_result: ReviewAdmissionCleanup | None = None

        def abort(reason: str) -> ReviewAdmissionCleanup:
            nonlocal cleanup_result
            if cleanup_result is None:
                cleanup_result = _review_cleanup(
                    handle, child=child, lease_acquired=lease_acquired,
                    lease_release=lease_release, witness_probe=witness_probe,
                )
            return cleanup_result

        def commit() -> None:
            handle.commit()

        return PostClaimAdmission(metadata, abort=abort, commit=commit)
    except BaseException as exc:
        child_mapping = receipt.get("child") if isinstance(receipt, Mapping) else None
        cleanup = _review_cleanup(
            handle,
            child=child_mapping if isinstance(child_mapping, Mapping) else None,
            lease_acquired=lease_acquired,
            lease_release=lease_release,
            witness_probe=witness_probe,
        )
        if isinstance(exc, ReviewAdmissionError):
            raise
        raise ReviewAdmissionError(
            getattr(exc, "reason", "review-admission-failed"), str(exc), cleanup
        ) from exc


def acquire_foreground_review_admission(
    *,
    budget: FiniteWatchdogBudget,
    identity: Mapping[str, str],
    root: str | Path,
    cycle_id: str,
    attempt_id: str,
    review_output: str | Path,
    binding: Mapping[str, object],
    jobs: str | Path,
    lease_acquire: Callable[..., Mapping[str, object]] | None = None,
    lease_release: Callable[[], Any] | None = None,
    witness_probe: Callable[[], bool] | None = None,
) -> PostClaimAdmission:
    """Acquire a foreground review lease without inventing a sidecar identity.

    Foreground launches already have the exact registered process identity and
    are reaped by ``spawn_claimed_attempt``.  This admission object contributes
    only the external lease/witness half of rollback; its abort callback proves
    the direct process group is empty after the transaction closes that group.
    """

    if not isinstance(budget, FiniteWatchdogBudget):
        raise DispatchContractError("review-admission-budget-invalid")
    if lease_acquire is None:
        from artifact_producer import review_lease_acquire as lease_acquire
    if lease_release is None:
        from artifact_producer import review_lease_release
        lease_release = lambda: review_lease_release(
            Path(root), cycle_id=cycle_id, attempt_id=attempt_id
        )
    governed_identity = dict(identity)
    result = lease_acquire(
        Path(root), cycle_id=cycle_id, attempt_id=attempt_id,
        review_output=review_output, binding=binding,
        deadline_seconds=budget.timeout_seconds,
        governed_identity=governed_identity, jobs=jobs, watchdog_budget=budget,
    )
    if not isinstance(result, Mapping) or result.get("status") not in {
        "acquired", "already-held"
    }:
        raise DispatchContractError("review-admission-lease-not-acquired")
    registry_metadata = result.get("registry_metadata")
    if not isinstance(registry_metadata, Mapping):
        raise DispatchContractError("review-lease-metadata-missing")

    metadata = {
        key: str(value) for key, value in registry_metadata.items()
        if value not in (None, "")
    }
    metadata["review_admission"] = "prepared"
    nonce = governed_identity.get("review_governed_lease_nonce")
    if nonce:
        metadata["review_governed_lease_nonce"] = str(nonce)
    cleanup_result: ReviewAdmissionCleanup | None = None

    def abort(_reason: str) -> ReviewAdmissionCleanup:
        nonlocal cleanup_result
        if cleanup_result is not None:
            return cleanup_result
        try:
            pgid = int(str(identity.get("pgid", "")))
            group_empty = (
                attempt_scan_namespace_authority(dict(identity))
                and process_group_observation(pgid).state == "empty"
                and attempt_tagged_descendants({**dict(identity), "attempt_id": attempt_id}).state == "empty"
            )
        except (OSError, TypeError, ValueError):
            group_empty = False
        lease_state = "unverified"
        if group_empty:
            try:
                release_result = lease_release()
                if release_result is not False and (
                    not isinstance(release_result, Mapping)
                    or release_result.get("status") in {"released", "already-released"}
                ):
                    lease_state = "released"
            except BaseException:
                pass
        witness_state = "unlocked"
        if witness_probe is not None:
            witness_deadline = time.monotonic() + _REVIEW_WITNESS_UNLOCK_TIMEOUT
            witness_state = "unverified"
            while True:
                try:
                    if bool(witness_probe()):
                        witness_state = "unlocked"
                        break
                except BaseException:
                    break
                remaining = witness_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(_REVIEW_WITNESS_POLL_INTERVAL, remaining))
        verified = (
            group_empty
            and lease_state == "released"
            and witness_state == "unlocked"
        )
        cleanup_result = ReviewAdmissionCleanup(
            watchdog_group="empty",
            fenced_child_group="empty" if group_empty else "unverified",
            readiness="closed-removed",
            review_lease=lease_state,
            governed_witness=witness_state,
            payload_marker="absent" if verified else "unverified",
            status="verified-never-launched" if verified else "unverified",
        )
        return cleanup_result

    def commit() -> None:
        return None

    return PostClaimAdmission(metadata, abort=abort, commit=commit)


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
    The optional foreground activity window may stop confirmed quiet workers
    earlier; this immutable finite budget remains the hard deadline.
    """

    return begin_finite_watchdog(timeout).timeout_seconds


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
    watchdog_budget: FiniteWatchdogBudget | None = None,
    activity_context: Any | None = None,
) -> ForegroundResult:
    """Wait in scope, optionally stopping a confirmed quiet child before timeout."""

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
        quiet = None
        if activity_context is not None and activity_context.window_seconds > 0:
            from foreground_activity import ActivityWindow, ForegroundActivityMonitor
            window = activity_context.window_seconds
            # Same decision as an owner turn: an unknown sample discards the
            # quiet time before it, and the next known sample starts a fresh
            # full window.
            quiet = ActivityWindow(
                ForegroundActivityMonitor(proc.pid, leader_start, activity_context).sample,
                window_seconds=window, sample_seconds=min(30.0, max(0.01, window / 20.0)),
            )
        if watchdog_budget is None:
            bounded_timeout = bounded_foreground_timeout(timeout)
            deadline = time.monotonic() + bounded_timeout
        else:
            # The caller supplied the launch-origin budget.  Do not normalize
            # or restart it here; foreground consumption is the same clock.
            deadline = watchdog_budget.deadline_monotonic_ns / 1_000_000_000
        while True:
            exit_code = proc.poll()
            # A live, unreaped leader already proves the group is nonempty.
            # Scanning /proc on every wait tick only repeats that fact. Keep
            # the complete fresh scan at exit and every stop/cleanup boundary.
            group_empty = _group_empty(proc.pid) if exit_code is not None else False
            if exit_code is not None and group_empty is True:
                break
            if received:
                exit_code, group_empty = _bounded_group_stop(
                    proc, leader_start, poll_interval=poll_interval
                )
                return ForegroundResult(
                    exit_code, FOREGROUND_INTERRUPTED, group_empty
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
            # Stop only on a fresh observation, not the last throttled one.
            if quiet is not None and quiet.expired() and quiet.expired(resample=True):
                if received:
                    exit_code, group_empty = _bounded_group_stop(
                        proc, leader_start, poll_interval=poll_interval
                    )
                    return ForegroundResult(exit_code, FOREGROUND_INTERRUPTED, group_empty)
                if parent_is_live is not None and not parent_is_live():
                    exit_code, group_empty = _bounded_group_stop(
                        proc, leader_start, poll_interval=poll_interval
                    )
                    return ForegroundResult(exit_code, "parent-terminated", group_empty)
                if parent_pid is not None and parent_pid_start and not process_identity_is_live(
                    parent_pid, parent_pid_start
                ):
                    exit_code, group_empty = _bounded_group_stop(
                        proc, leader_start, poll_interval=poll_interval
                    )
                    return ForegroundResult(exit_code, "parent-terminated", group_empty)
                if time.monotonic() >= deadline:
                    exit_code, group_empty = _bounded_group_stop(
                        proc, leader_start, poll_interval=poll_interval
                    )
                    return ForegroundResult(exit_code, "timeout", group_empty)
                exit_code, group_empty = _bounded_group_stop(
                    proc, leader_start, poll_interval=poll_interval
                )
                return ForegroundResult(exit_code, "no-progress", group_empty)
            sleep_for = min(max(poll_interval, 0.01), remaining)
            if quiet is not None:
                now = time.monotonic()
                sleep_for = min(sleep_for, max(0.01, quiet.next_sample - now))
                if quiet.quiet_since is not None:
                    sleep_for = min(sleep_for, max(0.01, quiet.quiet_since + quiet.window_seconds - now))
            time.sleep(sleep_for)
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
        return ForegroundResult(exit_code, FOREGROUND_INTERRUPTED)
    if exit_code < 0:
        return ForegroundResult(exit_code, f"signal-{-exit_code}")
    if exit_code:
        return ForegroundResult(exit_code, f"exit-{exit_code}")
    return ForegroundResult(exit_code, "")


@dataclass(frozen=True)
class ForwardedRun:
    """What a launcher learns from one wrapper run it hosted.

    ``received_signal`` is the first stop request this launcher received while
    the wrapper ran; ``cleanup_incomplete`` means the wrapper had not finished
    within the grace window and was killed, or captured output did not reach EOF
    within the bounded post-exit drain window.
    """

    returncode: int
    stdout: str | None
    stderr: str | None
    received_signal: int | None = None
    cleanup_incomplete: bool = False
    timed_out: bool = False


def _forwarded_signals() -> list[int]:
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    return signals


def _close_forwarded_pipe(selector: selectors.BaseSelector, stream: Any) -> None:
    """Unregister and close a nonblocking pipe owned by this launcher."""

    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _drain_forwarded_pipes(
    selector: selectors.BaseSelector,
) -> bool:
    """Read at most one bounded chunk per ready pipe before deadline checks.

    A busy writer may keep a nonblocking pipe readable indefinitely. Reading
    only one chunk from each ready stream lets the caller poll the wrapper and
    check its fixed timeout/signal deadlines between drain turns.
    """

    incomplete = False
    for key, _events in selector.select(timeout=0):
        stream = key.fileobj
        sink = key.data
        try:
            # The descriptor is nonblocking and each read is capped at 64 KiB.
            # The selector will report any remaining bytes on the next turn.
            chunk = os.read(stream.fileno(), 65536)
        except BlockingIOError:
            continue
        except (OSError, ValueError):
            incomplete = True
            _close_forwarded_pipe(selector, stream)
            continue
        if not chunk:
            _close_forwarded_pipe(selector, stream)
            continue
        sink.append(chunk)
    return incomplete


@frame_repository.forwarded_entry
def run_forwarding_termination(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    pass_fds: tuple[int, ...] = (),
    capture: bool,
    timeout: float | None,
    grace: float = FORWARDED_TERMINATION_GRACE,
    poll_interval: float = 0.05,
    terminate_on_timeout: bool = False,
) -> ForwardedRun:
    """Run a wrapper inside this call and hand any stop request to it.

    ``subprocess.run`` answers SIGINT by killing its child 0.25s later, so a
    foreground wrapper never got to stop its worker and close the row. Here the
    first SIGINT/SIGTERM/SIGHUP fixes one cleanup deadline (``now + grace``)
    and is passed to the wrapper; later signals are passed too but never move
    that deadline. A wrapper still running at the deadline is killed (only the
    wrapper: its worker group belongs to the wrapper's own cleanup) and the
    result says ``cleanup_incomplete``. With ``terminate_on_timeout`` the same
    bounded TERM cleanup applies at the outer deadline and reports ``timed_out``.
    Once the wrapper exits, captured pipes get one bounded short drain; a pipe
    still open afterward sets ``cleanup_incomplete`` and does not extend the
    execution wait. Otherwise timeout retains its existing
    ``subprocess.TimeoutExpired`` meaning.
    Handlers are installed only on the main thread and are always restored.
    """

    received: list[int] = []
    cleanup_deadline: list[float] = []
    child: list[subprocess.Popen] = []
    pending: list[int] = []

    def deliver(proc: subprocess.Popen, signum: int) -> None:
        try:
            proc.send_signal(signum)
        except OSError:
            pass

    def forward(signum: int, _frame: object) -> None:
        received.append(signum)
        if not cleanup_deadline:
            cleanup_deadline.append(time.monotonic() + grace)
        if child:
            deliver(child[0], signum)
        else:
            pending.append(signum)

    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in _forwarded_signals():
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, forward)
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    selector: selectors.BaseSelector | None = None
    proc: subprocess.Popen | None = None

    def text(chunks: list[bytes]) -> str | None:
        if not capture:
            return None
        return b"".join(list(chunks)).decode("utf-8", errors="replace")

    try:
        if received:
            # Stopped before anything was launched: launch nothing.
            return ForwardedRun(-received[0], text([]), text([]), received[0])
        pipe = subprocess.PIPE if capture else None
        proc = subprocess.Popen(
            list(command), cwd=cwd, env=None if env is None else dict(env),
            stdout=pipe, stderr=pipe,
            **({"pass_fds": pass_fds} if pass_fds else {}),
        )
        child.append(proc)
        for signum in pending:
            deliver(proc, signum)
        selector = selectors.DefaultSelector() if capture else None
        if capture:
            for stream, sink in ((proc.stdout, stdout_chunks), (proc.stderr, stderr_chunks)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, sink)
        outer_deadline = None if timeout is None else time.monotonic() + timeout
        cleanup_incomplete = False
        timed_out = False
        timeout_expired = False
        try:
            while proc.poll() is None:
                now = time.monotonic()
                if cleanup_deadline:
                    if now >= cleanup_deadline[0]:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        proc.wait()
                        cleanup_incomplete = True
                        break
                elif outer_deadline is not None and now >= outer_deadline:
                    if terminate_on_timeout:
                        timed_out = True
                        forward(signal.SIGTERM, None)
                        continue
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    proc.wait()
                    timeout_expired = True
                    break

                wait_for = max(0.0, poll_interval)
                deadline = cleanup_deadline[0] if cleanup_deadline else outer_deadline
                if deadline is not None:
                    wait_for = min(wait_for, max(0.0, deadline - now))
                if selector is not None and selector.get_map():
                    selector.select(wait_for)
                    cleanup_incomplete |= _drain_forwarded_pipes(selector)
                elif wait_for:
                    time.sleep(wait_for)

            if timeout_expired:
                # Preserve bytes already in the wrapper pipes, but never let a
                # descendant holding those pipes extend the caller's timeout.
                drain_deadline = time.monotonic() + FORWARDED_POST_EXIT_DRAIN_GRACE
                while selector is not None and selector.get_map():
                    remaining = drain_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    selector.select(remaining)
                    cleanup_incomplete |= _drain_forwarded_pipes(selector)
                raise subprocess.TimeoutExpired(
                    list(command), timeout,
                    output=text(stdout_chunks), stderr=text(stderr_chunks),
                )

            proc.wait()
            # Process exit is the execution boundary. EOF may be delayed by a
            # descendant that inherited stdout/stderr, so allow only this short
            # fixed drain window after exit.
            drain_deadline = time.monotonic() + FORWARDED_POST_EXIT_DRAIN_GRACE
            while selector is not None and selector.get_map():
                remaining = drain_deadline - time.monotonic()
                if remaining <= 0:
                    cleanup_incomplete = True
                    break
                selector.select(min(max(0.0, poll_interval), remaining))
                cleanup_incomplete |= _drain_forwarded_pipes(selector)
        except BaseException:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait()
            raise
        return ForwardedRun(
            proc.returncode,
            text(stdout_chunks),
            text(stderr_chunks),
            received[0] if received else None,
            cleanup_incomplete,
            timed_out,
        )
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait()
            except OSError:
                pass
        if selector is not None:
            for key in list(selector.get_map().values()):
                _close_forwarded_pipe(selector, key.fileobj)
            selector.close()
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
