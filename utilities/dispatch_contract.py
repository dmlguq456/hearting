#!/usr/bin/env python3
"""Portable SD-48/49 primitives used by headless dispatch adapters."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Callable, Iterator, NamedTuple

from replica_batch_contract import (
    DIGEST,
    ReplicaBatchContractError,
    verify_manifest,
)
from stage_session_contract import load_manifest


ELIGIBILITY = {"supported", "unsupported", "unknown"}
LAUNCH_AUTHORITIES = {"conductor", "ancestor-broker"}
STANDARD_PLUS_INTENSITIES = frozenset(
    {"standard", "strong", "thorough", "adversarial"}
)
ATTEMPT_SCHEMA_VERSION = 2
SUBSESSION_ID_RE = re.compile(r"^ss-[A-Za-z0-9._-]{4,200}$")
SESSION_CHAIN_ID_RE = re.compile(r"^ssc-[A-Za-z0-9._-]{4,200}$")
SUPERVISOR_LEASE_KIND = "flock-v1"
SUPERVISOR_LEASE_NONCE_RE = re.compile(r"[0-9a-f]{64}")
PARENT_LIVENESS_METADATA_KEYS = (
    "attempt_id",
    "attempt_schema_version",
    "dispatch_depth",
    "worker_type",
    "harness",
    "transport",
    "execution_surface",
    "registered_worker",
    "runtime_sandbox",
    "pid",
    "pid_start",
    "pid_scope",
    "pid_host",
    "pid_host_start",
    "pid_host_ns",
    "pid_host_proof",
    "pid_ns",
    "pid_observer_ns",
    "pgid",
    "pgid_host",
    "completion_delivery",
    "supervisor_lease",
    "supervisor_lease_file",
    "supervisor_lease_nonce",
)
WRAPPER_TRANSPORTS = {"headless", "interactive"}
CANONICAL_PARENT_TRANSPORTS = WRAPPER_TRANSPORTS
# The runtime a dispatch-depth-N node's PARENT runs under. A dispatch-depth-2
# node is opened by the dispatch-depth-1 registered-headless capability owner,
# so its sealed parent transport is always `headless`; only the user-facing
# dispatch-depth-0 session is `interactive`. Every surface that probes, seals,
# compiles, or launches a checked nested tuple resolves the expectation from
# here instead of reading the probing caller's own runtime (2026-08-04
# Cairn incident: a standard route sealed with the depth-0 caller's
# `interactive` transport made every same/cross-harness candidate unresolvable
# at launch and demoted the whole cycle to the inline hop).
PARENT_TRANSPORT_BY_DISPATCH_DEPTH = {0: "interactive", 1: "headless"}
# Canonical parent-sandbox labels each adapter wrapper actually exports as
# AGENT_DISPATCH_CURRENT_SANDBOX; the first label is what `auto` resolves to
# (2026-07-31 v2-audit incident: a route sealed with parent_sandbox=none).
# utilities/dispatch_contract.test.py pins this table against the literals the
# wrappers export, because a stale copy would now reject correctly probed
# evidence at compile time, not merely at probe time.
WRAPPER_PARENT_SANDBOXES = {
    "claude": ("adapter-default",),
    "codex": ("workspace-write", "danger-full-access", "read-only"),
    "opencode": ("adapter-default",),
}
WRAPPER_PARENT_HARNESSES = tuple(sorted(WRAPPER_PARENT_SANDBOXES))
EXECUTION_SURFACES = {
    "registered-headless",
    "codex-native-subagent",
    "claude-subagent",
    "claude-agent-team-teammate",
    "inline",
}
FALLBACK_HOPS = {
    "same-harness-headless",
    "cross-harness-headless",
    "native-subagent",
    "inline",
}
ATTEMPT_MUTABLE_METADATA = {
    "launch_claimed",
    "pid",
    "pid_start",
    "pid_scope",
    "pid_host",
    "pid_host_start",
    "pid_host_ns",
    "pid_ns",
    "pid_observer_ns",
    "pid_host_proof",
    "pgid",
    "pgid_host",
    "group_reap_proof",
    "group_reap_pgid",
    "attempt_descendant_proof",
    "attempt_descendant_observer_ns",
    "reap_watch",
    "reap_watch_pid",
    "launch_lifecycle",
    "launch_lifecycle_requested",
    "launch_lifecycle_reselection",
    "lifecycle_selector_source",
    "lifecycle_nspid_width",
    "lifecycle_pid1_class",
    "launch_started",
    "launch_outcome",
    "updated_at",
    "note",
    "completion_marker",
    "completion_marker_history",
    "parent_completion_harvested",
    "managed_delivery_state",
    "managed_sealed_batch_id",
    "managed_sidecar_pid",
    "managed_sidecar_log",
    "summary_owner",
    "summary_sid",
    "summary_owner_pid",
    "summary_owner_pid_start",
    "summary_owner_pid_scope",
    "summary_owner_pid_host",
    "summary_owner_pid_host_start",
    "summary_owner_pid_host_ns",
    "summary_owner_pid_observer_ns",
    "summary_owner_pid_host_proof",
    "summary_state_file",
    "watchdog",
    "heartbeat",
    "teardown_claim",
    "teardown_claimed_at",
    "teardown_claim_pid",
    "teardown_claim_pid_start",
    "reap_close_deferred",
    "reap_close_deferred_at",
}
ATTEMPT_TERMINAL_EVIDENCE_KEYS = {
    "api_status",
    "capacity_log",
    "classifier_source",
    "detected_by",
    "failure_class",
    "process_exit",
    "reconcile_reason",
    "reset",
    "terminal_event",
    "watchdog_windows",
    "terminal_conflict",
    "prior_terminal_note",
    "prior_classifier_source",
    "prior_failure_class",
    "conflicting_classifier_source",
    "conflicting_failure_class",
}
_MODULE_ROOT = Path(__file__).resolve().parents[1]
_CAPACITY_TERMINAL_RE = re.compile(
    r"(?:error\s*[:\-]\s*)?(?:selected\s+)?model(?:\s+[A-Za-z0-9._:/-]+)?\s+"
    r"(?:is\s+)?at\s+capacity[.!]?",
    re.I,
)


def codex_standard_owner_network_enabled(
    *, dispatch_depth: int, worker_type: str, intensity: str, sandbox: str
) -> bool:
    """Return whether the Codex wrapper grants its scoped nested network profile."""

    return (
        dispatch_depth == 1
        and worker_type == "owner"
        and intensity in STANDARD_PLUS_INTENSITIES
        and sandbox == "workspace-write"
    )


GOVERNOR_RESERVATION_ENV = "AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN"
PID_HOST_NAMESPACE_PROOF = "nspid-procfs-root-v1"
GROUP_REAP_PROOF = "pgid-empty-v1"
REPLICA_RESERVATION_ROW_KEYS = (
    "reservation_kind",
    "batch_declared_size",
    "batch_admission_count",
    "batch_group",
    "batch_route_id",
    "batch_parent_attempt_id",
    "batch_attempt_id",
    "batch_route_node",
    "batch_harness",
    "batch_fallback_hop",
    "batch_fallback_ordinal",
    "batch_independence",
    "batch_assignment_sha256",
    "batch_model_profile",
    "batch_perspective",
    "batch_parallel_leg_index",
    "batch_peer_count",
    "batch_peer_set_sha256",
    # Schema-v1 two-way recovery compatibility.
    "batch_peer_attempt_id",
    "batch_peer_state",
    "batch_peer_proof_sha256",
    "batch_manifest_sha256",
    "batch_leg_sha256",
)


def anchored_capacity_failure(text: str) -> bool:
    """Accept only a terminal capacity error, never prose discussing one.

    Adapters may emit either a plain CLI line or a JSON event.  The bounded
    last-three-line rule is shared by the early wrapper watch and the SD-58
    foreground watchdog so delayed failures receive the same classification.
    """

    def terminal(value: str) -> bool:
        return bool(_CAPACITY_TERMINAL_RE.fullmatch(value.strip()))

    lines = [line.strip() for line in text.splitlines() if line.strip()][-3:]
    for line in lines:
        if len(line) > 200:
            continue
        if terminal(line):
            return True
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pending = [payload]
        while pending:
            item = pending.pop()
            for key, value in item.items():
                if isinstance(value, dict):
                    pending.append(value)
                elif key in {"message", "error", "detail"} and isinstance(value, str) and terminal(value):
                    return True
    return False


def resolve_agent_home(runtime_pointer: str | Path | None = None) -> Path:
    """Validated AGENT_HOME (source root) resolution shared by every consumer
    that must agree on where the packaged/versioned agent installation lives.

    This function resolves only the **source root** -- the immutable,
    versioned code checkout. It is not responsible for dispatch state
    (jobs.log, completion markers, logs, ...); that is
    `resolve_dispatch_state_root()`'s job, derived from the canonical
    registry path, not from this function's return value.

    Mirrors adapters/claude/bin/dispatch-headless.py:546-558's preference
    order. A naive `os.environ.get("AGENT_HOME", ROOT)` falls back to the
    caller's own worktree when AGENT_HOME is unset, which previously split
    consumers between the wrapper (writer, worktree-relative) and the
    liveness/Stop readers (agent-home-relative) -- SD-14b(2). Every consumer
    that must land in the SAME directory as another process has to go
    through this one function, not re-derive its own fallback.

    `runtime_pointer` is an optional caller-supplied candidate (not a new env
    var) inserted between `CLAUDE_HOME` and the XDG `current` pointer, so a
    runtime with its own bundle/pointer convention (codex `~/.codex/hearting`,
    opencode `~/.config/opencode/hearting`) can prioritize it without forking
    this function.
    """

    def _valid(candidate: str | None) -> bool:
        return bool(candidate) and (Path(candidate) / "core" / "CORE.md").is_file()

    candidates = [
        os.environ.get("AGENT_HOME"),
        os.environ.get("CLAUDE_HOME"),
    ]
    if runtime_pointer is not None:
        candidates.append(str(runtime_pointer))
    candidates.extend(
        [
            str(Path.home() / ".local" / "share" / "hearting" / "current"),
            str(Path.home() / "hearting"),
            str(Path.home() / "agent_setting"),
            str(Path.home() / ".claude"),
        ]
    )
    for candidate in candidates:
        if _valid(candidate):
            return Path(candidate)
    # No candidate is marked: converge on utilities/agent-home.sh's final
    # fallback ($HOME/.claude, unvalidated) so the two resolver chains cannot
    # silently diverge in a bare environment (review F-4). _MODULE_ROOT stays
    # only for the pathological case where even $HOME is undefined.
    try:
        return Path.home() / ".claude"
    except RuntimeError:
        return _MODULE_ROOT


def agent_home_equivalent(a: str | Path, b: str | Path) -> bool:
    """Compare two agent-home candidates by resolved identity.

    Stored/compared state paths must keep pointer form (no `.resolve()`); use
    this helper only at comparison sites, never to normalize a path before
    writing or persisting it.
    """

    return Path(a).resolve(strict=False) == Path(b).resolve(strict=False)


def resolve_model_governor_root(
    artifact_root: str | Path,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """Resolve one canonical governor root and reject ambient split-brain roots."""

    env = os.environ if environ is None else environ
    expected = (
        Path(artifact_root).expanduser().resolve(strict=False)
        / ".runtime"
        / "model-worker-governor"
    )
    explicit = env.get("AGENT_MODEL_GOVERNOR_ROOT", "")
    if explicit:
        selected = Path(explicit).expanduser().resolve(strict=False)
        if selected != expected:
            raise DispatchContractError(
                "noncanonical-model-governor-root",
                f"expected={expected} actual={selected}",
            )
    return expected


class DispatchContractError(ValueError):
    """Structured dispatch-contract failure."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class RegistrySelection:
    path: Path
    source: str
    inherited: bool


@dataclass(frozen=True)
class BrokerSelection:
    root: Path
    instance_id: str
    pid: int
    start_ticks: str
    jobs: Path


@dataclass(frozen=True)
class ParentAttemptBinding:
    """One live depth-1 owner identity sealed into a depth-2 attempt."""

    attempt_id: str
    pid: int
    pid_start: str
    pid_scope: str
    pid_host: int | None
    pid_host_start: str
    observed_pid: int | None
    observed_pid_start: str
    liveness_source: str
    harness: str
    transport: str
    runtime_sandbox: str
    repository_identity: str
    worktree: str
    slug: str
    liveness_metadata_fingerprint: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProcessQuiescence:
    """Exact governed-process state used by every readiness consumer."""

    state: str
    reason: str
    pid: int | None = None
    identity: AuthoritativeProcessIdentity | None = None


@dataclass(frozen=True)
class AuthoritativeProcessIdentity:
    """One exact PID/start identity valid in the current observer namespace."""

    source: str
    pid: int
    expected_start: str


@dataclass(frozen=True)
class ProcessGroupObservation:
    """One complete, populated, or unverifiable process-group observation."""

    state: str
    members: tuple[tuple[int, str, str], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class AttemptReadiness:
    """Semantic-terminal plus governed-process readiness for one attempt."""

    state: str
    reason: str
    attempt_id: str = ""


@dataclass(frozen=True)
class ObservedAttemptLiveness:
    """Pure registry/process verdict shared by dispatch observation surfaces."""

    state: str
    reason: str
    process_state: str
    process_reason: str


def parse_registry_metadata(pipe: str) -> dict[str, str]:
    """Parse the stable six-column registry's comma-delimited metadata."""

    return dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)


def canonical_repository_identity(path: str | Path) -> str:
    """Return one physical Git-repository identity for primary/linked worktrees.

    Git's common directory is shared by every linked worktree.  Non-Git or
    unavailable paths retain a physical-path identity so fixtures and explicit
    foreign repositories still fail closed instead of collapsing together.
    """

    candidate = Path(path).expanduser().resolve(strict=False)
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--git-common-dir"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return str(candidate)
    raw = result.stdout.strip()
    if result.returncode or not raw or "\n" in raw:
        return str(candidate)
    common = Path(raw)
    if not common.is_absolute():
        common = candidate / common
    return str(common.resolve(strict=False))


def process_start_ticks(pid: int) -> str | None:
    """Return Linux proc start ticks for an exact PID identity."""

    if pid <= 0:
        return None
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2 :].split()
        return tail[19]
    except (OSError, IndexError):
        return None


def process_namespace_identity(pid: int | str = "self") -> str | None:
    """Return the PID namespace inode without treating an unreadable link as absence."""

    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


def process_state(pid: int) -> str | None:
    """Return the one-letter proc state; zombies are not live workers."""

    if pid <= 0:
        return None
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2 :].split()
        return tail[0]
    except (OSError, IndexError):
        return None


def process_identity_is_live(pid: int, expected_start: str) -> bool:
    visibility, actual_start, state = _proc_observation(pid)
    return (
        bool(expected_start)
        and visibility == "present"
        and actual_start == expected_start
        and state != "Z"
    )


def supervisor_lease_path(jobs: str | Path, attempt_id: str) -> Path:
    """Return the only canonical liveness-lease path for an owner attempt."""

    if re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", attempt_id) is None:
        raise DispatchContractError("supervisor-lease-attempt-invalid", attempt_id)
    return dispatch_state_root(jobs) / "supervisor-state" / f"{attempt_id}.lease"


def _validated_supervisor_lease_path(
    jobs: str | Path, attempt_id: str, raw_path: str | Path
) -> Path:
    expected = supervisor_lease_path(jobs, attempt_id)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() or str(candidate) != str(expected):
        raise DispatchContractError(
            "supervisor-lease-path-noncanonical",
            f"expected={expected} actual={candidate}",
        )
    if candidate.parent.is_symlink() or candidate.is_symlink():
        raise DispatchContractError("supervisor-lease-path-symlink", str(candidate))
    return candidate


def _open_supervisor_lease(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise DispatchContractError("supervisor-lease-not-regular", str(path))
    return fd


def _supervisor_lease_metadata_valid(
    jobs: str | Path, metadata: dict[str, str]
) -> bool:
    attempt_id = metadata.get("attempt_id", "")
    delivery_by_harness = {
        "claude": "session-resume-supervised",
        "codex": "app-server-supervised",
    }
    harness = metadata.get("harness", "")
    if (
        metadata.get("attempt_schema_version") != "2"
        or metadata.get("dispatch_depth") != "1"
        or metadata.get("worker_type") != "owner"
        or harness not in delivery_by_harness
        or metadata.get("transport") != "headless"
        or metadata.get("execution_surface") != "registered-headless"
        or metadata.get("registered_worker") != "1"
        or metadata.get("completion_delivery") != delivery_by_harness[harness]
        or metadata.get("supervisor_lease") != SUPERVISOR_LEASE_KIND
        or SUPERVISOR_LEASE_NONCE_RE.fullmatch(
            metadata.get("supervisor_lease_nonce", "")
        )
        is None
        or not attempt_id
    ):
        return False
    try:
        _validated_supervisor_lease_path(
            jobs, attempt_id, metadata.get("supervisor_lease_file", "")
        )
    except DispatchContractError:
        return False
    return True


def _supervisor_lease_payload(metadata: dict[str, str]) -> bytes:
    return (
        f"kind={SUPERVISOR_LEASE_KIND}\n"
        f"attempt_id={metadata['attempt_id']}\n"
        f"nonce={metadata['supervisor_lease_nonce']}\n"
    ).encode("ascii")


def _supervisor_lease_payload_matches(fd: int, metadata: dict[str, str]) -> bool:
    expected = _supervisor_lease_payload(metadata)
    try:
        observed = os.pread(fd, len(expected) + 1, 0)
    except OSError:
        return False
    return observed == expected


def supervisor_lease_is_held(
    jobs: str | Path, metadata: dict[str, str]
) -> bool:
    """Probe a declared lease without treating an existing stale file as live."""

    if not _supervisor_lease_metadata_valid(jobs, metadata):
        return False
    try:
        path = _validated_supervisor_lease_path(
            jobs,
            metadata["attempt_id"],
            metadata["supervisor_lease_file"],
        )
        fd = _open_supervisor_lease(path, create=False)
    except (DispatchContractError, OSError):
        return False
    try:
        if not _supervisor_lease_payload_matches(fd, metadata):
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
    finally:
        os.close(fd)


def _declared_supervisor_lease_metadata(
    jobs: Path, attempt_id: str
) -> dict[str, str]:
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise DispatchContractError(
                "supervisor-lease-registry-unreadable", str(exc)
            ) from exc
        matches: list[tuple[str, dict[str, str]]] = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((fields[1], metadata))
        if len(matches) != 1 or matches[0][0] not in {"open", "running"}:
            raise DispatchContractError(
                "supervisor-lease-attempt-not-open", attempt_id
            )
        metadata = matches[0][1]
        if not _supervisor_lease_metadata_valid(jobs, metadata):
            raise DispatchContractError(
                "supervisor-lease-declaration-invalid", attempt_id
            )
        return metadata


@contextmanager
def hold_supervisor_lease(
    jobs: str | Path, attempt_id: str, raw_path: str | Path
) -> Iterator[Path]:
    """Hold one exact owner lease until the supervisor finalization boundary."""

    registry = Path(jobs).expanduser().resolve(strict=False)
    path = _validated_supervisor_lease_path(registry, attempt_id, raw_path)
    metadata = _declared_supervisor_lease_metadata(registry, attempt_id)
    if metadata.get("supervisor_lease_file") != str(path):
        raise DispatchContractError("supervisor-lease-declaration-changed", attempt_id)
    if path.parent.is_symlink():
        raise DispatchContractError("supervisor-lease-path-symlink", str(path.parent))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise DispatchContractError("supervisor-lease-path-symlink", str(path))
    try:
        fd = _open_supervisor_lease(path, create=True)
    except OSError as exc:
        raise DispatchContractError("supervisor-lease-open-failed", str(exc)) from exc
    inode = os.fstat(fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise DispatchContractError(
            "supervisor-lease-already-held", attempt_id
        ) from exc
    except OSError:
        os.close(fd)
        raise
    try:
        try:
            confirmed = _declared_supervisor_lease_metadata(registry, attempt_id)
            for key in (
                "supervisor_lease",
                "supervisor_lease_file",
                "supervisor_lease_nonce",
            ):
                if confirmed.get(key) != metadata.get(key):
                    raise DispatchContractError(
                        "supervisor-lease-declaration-changed", attempt_id
                    )
            payload = _supervisor_lease_payload(metadata)
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            if os.pwrite(fd, payload, 0) != len(payload):
                raise OSError("short supervisor lease write")
            os.fsync(fd)
        except OSError as exc:
            raise DispatchContractError(
                "supervisor-lease-initialize-failed", str(exc)
            ) from exc
        yield path
    finally:
        preserve_recovery_file = sys.exc_info()[0] is not None
        try:
            current = path.lstat()
            if (
                not preserve_recovery_file
                and (current.st_dev, current.st_ino) == (inode.st_dev, inode.st_ino)
            ):
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def remove_supervisor_lease(path: str | Path) -> bool:
    """Remove an unlocked exact lease file without following replacements."""

    lease = Path(path)
    if lease.parent.is_symlink() or lease.is_symlink():
        return False
    try:
        fd = _open_supervisor_lease(lease, create=False)
    except FileNotFoundError:
        return True
    except (DispatchContractError, OSError):
        return False
    inode = os.fstat(fd)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        current = lease.lstat()
        if (current.st_dev, current.st_ino) != (inode.st_dev, inode.st_ino):
            return False
        lease.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def process_namespace_pids(pid: int) -> tuple[int, ...]:
    """Return the outer-to-inner NSpid vector without guessing on failure."""

    try:
        lines = (Path("/proc") / str(pid) / "status").read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return ()
    for line in lines:
        if not line.startswith("NSpid:"):
            continue
        try:
            return tuple(int(value) for value in line.split()[1:])
        except ValueError:
            return ()
    return ()


def local_identity_namespace_authority(metadata: dict[str, str]) -> bool:
    """True when the current observer namespace owns this row's local PID."""

    current_namespace = process_namespace_identity()
    recorded_observer = metadata.get("pid_observer_ns", "")
    recorded_pid_namespace = metadata.get("pid_ns", "")
    pid_scope = metadata.get("pid_scope", "host-visible")
    return bool(
        recorded_observer
        and current_namespace == recorded_observer
        and (
            not recorded_pid_namespace
            or recorded_pid_namespace == recorded_observer
        )
    ) or (not recorded_observer and pid_scope != "namespace-local")


def attempt_scan_namespace_authority(metadata: dict[str, str]) -> bool:
    """True when *finding nothing* is proof this attempt has no live process.

    Deliberately not the same question as ``local_identity_namespace_authority``.
    That one asks whether a recorded PID number means anything here; this asks
    whether a ``/proc`` walk here could have seen the attempt's processes at all.
    A namespace-local row whose PID is meaningless to us is still fully scannable
    when we are the namespace that watched it launch -- which is exactly the
    ghost row SD-58 needs to be able to close.

    Three ways to hold that authority: we are the namespace that recorded the
    observation; launch proved the procfs-root namespace and we are in it, so
    every descendant is visible; or the row predates the observer field and was
    recorded as host-visible. Anything else fails closed, because a narrower or
    sibling namespace's empty scan is invisibility, not absence.
    """

    current_namespace = process_namespace_identity()
    if not current_namespace:
        return False
    recorded_observer = metadata.get("pid_observer_ns", "")
    if recorded_observer:
        if recorded_observer == current_namespace:
            return True
        return (
            metadata.get("pid_host_proof") == PID_HOST_NAMESPACE_PROOF
            and metadata.get("pid_host_ns") == current_namespace
        )
    return metadata.get("pid_scope", "host-visible") != "namespace-local"


def authoritative_process_identities(
    metadata: dict[str, str],
) -> tuple[AuthoritativeProcessIdentity, ...]:
    """Resolve only PID identities whose namespace provenance is authoritative.

    ``NSpid[0]`` is relative to the PID namespace of the procfs mount, not
    necessarily the host namespace.  A cross-namespace identity is therefore
    usable only when launch recorded that procfs-root namespace and the current
    observer is in that exact namespace.  Legacy host-visible local identities
    remain usable, while namespace-local or namespace-mismatched evidence fails
    closed.
    """

    current_namespace = process_namespace_identity()
    candidates: list[AuthoritativeProcessIdentity] = []

    raw_pid = metadata.get("pid", "")
    local_start = metadata.get("pid_start", "")
    local_authoritative = local_identity_namespace_authority(metadata)
    if raw_pid.isdigit() and local_start and local_authoritative:
        candidates.append(
            AuthoritativeProcessIdentity("local", int(raw_pid), local_start)
        )

    raw_host = metadata.get("pid_host", "")
    host_start = metadata.get("pid_host_start", "") or local_start
    recorded_host_namespace = metadata.get("pid_host_ns", "")
    host_authoritative = (
        raw_host.isdigit()
        and bool(host_start)
        and (not local_start or host_start == local_start)
        and metadata.get("pid_host_proof") == PID_HOST_NAMESPACE_PROOF
        and bool(current_namespace)
        and current_namespace == recorded_host_namespace
    )
    if host_authoritative:
        candidate = AuthoritativeProcessIdentity("host", int(raw_host), host_start)
        if not any(
            (item.pid, item.expected_start)
            == (candidate.pid, candidate.expected_start)
            for item in candidates
        ):
            candidates.append(candidate)

    # Two distinct identities cannot both name the same process from one
    # observer namespace. Treat internally inconsistent metadata as having no
    # signal/readiness authority instead of choosing a preferred numeric PID.
    if len(candidates) > 1:
        return ()
    return tuple(candidates)


def process_launch_identity(pid: int) -> dict[str, str]:
    """Capture local and namespace-bound procfs PID evidence for a new leader."""

    values = {"pid": str(pid)}
    observer_namespace = process_namespace_identity()
    child_namespace = process_namespace_identity(pid)
    if observer_namespace:
        values["pid_observer_ns"] = observer_namespace
    if child_namespace:
        values["pid_ns"] = child_namespace
    procfs_pid_aligned = bool(
        observer_namespace
        and child_namespace
        and observer_namespace == child_namespace
    )
    start = process_start_ticks(pid) if procfs_pid_aligned else None
    if start:
        values["pid_start"] = start
    namespace_pids = process_namespace_pids(pid) if procfs_pid_aligned else ()
    procfs_root_namespace = (
        process_namespace_identity(1) if procfs_pid_aligned else None
    )
    # Some sandboxes hide /proc/1/ns/pid.  A one-element vector is still
    # safely bound to the launch observer: the new child has no PID-namespace
    # ancestor between that observer and this procfs view.  For a multi-level
    # vector, absence of /proc/1 namespace evidence must remain unverifiable.
    if (
        not procfs_root_namespace
        and len(namespace_pids) == 1
        and observer_namespace
        and child_namespace == observer_namespace
    ):
        procfs_root_namespace = observer_namespace
    if (
        namespace_pids
        and namespace_pids[-1] == pid
        and procfs_root_namespace
    ):
        values["pid_host"] = str(namespace_pids[0])
        if start:
            values["pid_host_start"] = start
        values["pid_host_ns"] = procfs_root_namespace
        values["pid_host_proof"] = PID_HOST_NAMESPACE_PROOF
    try:
        pgid = os.getpgid(pid)
        values["pgid"] = str(pgid)
        if pgid == pid and values.get("pid_host"):
            values["pgid_host"] = values["pid_host"]
    except (OSError, ProcessLookupError):
        pass
    return values


def _proc_observation(pid: int) -> tuple[str, str, str]:
    """Return (visibility,start,state) while distinguishing absence from denial."""

    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", "", ""
    except PermissionError:
        return "inaccessible", "", ""
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return "missing", "", ""
        return "inaccessible", "", ""
    try:
        tail = raw[raw.rfind(")") + 2 :].split()
        return "present", tail[19], tail[0]
    except IndexError:
        return "inaccessible", "", ""


def process_observation(pid: int) -> tuple[str, str, str]:
    """Public exact-PID observation used by lifecycle and signal paths."""

    return _proc_observation(pid)


def exact_process_group_signal_authority(pid: int, expected_start: str) -> str:
    """Return signal authority only for a current exact process-group leader."""

    visibility, actual_start, state = _proc_observation(pid)
    if visibility == "missing":
        return "leader-gone"
    if visibility != "present":
        return "identity-unverifiable"
    if actual_start != expected_start:
        return "pid-reused"
    if state == "Z":
        return "leader-gone"
    try:
        return "authoritative" if os.getpgid(pid) == pid else "non-group-leader"
    except ProcessLookupError:
        return "leader-gone"
    except OSError:
        return "signal-error"


def signal_exact_process_group(pid: int, expected_start: str, signum: int) -> str:
    """Signal only after two adjacent exact leader/start/PGID validations."""

    authority = exact_process_group_signal_authority(pid, expected_start)
    if authority != "authoritative":
        return authority
    authority = exact_process_group_signal_authority(pid, expected_start)
    if authority != "authoritative":
        return authority
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return "leader-gone"
    except OSError:
        return "signal-error"
    return "signalled"


def process_group_observation(pgid: int) -> ProcessGroupObservation:
    """Observe a group without collapsing inaccessible procfs into emptiness.

    A known non-zombie member proves population even if another proc entry was
    concurrently inaccessible. Emptiness is returned only after a complete
    scan; otherwise the result is explicitly unverifiable.
    """

    if pgid <= 0:
        return ProcessGroupObservation("unverifiable", reason="invalid-pgid")
    members: list[tuple[int, str, str]] = []
    incomplete_reason = ""
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessGroupObservation(
            "unverifiable", reason=f"procfs-enumeration:{exc.errno or 'error'}"
        )
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            tail = raw[raw.rfind(")") + 2 :].split()
            if int(tail[2]) != pgid:
                continue
            members.append((int(entry.name), tail[19], tail[0]))
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            incomplete_reason = f"procfs-member:{entry.name}:{exc.errno or 'error'}"
        except (IndexError, ValueError):
            incomplete_reason = f"procfs-member:{entry.name}:malformed"
    ordered = tuple(sorted(members, key=lambda member: member[0]))
    if any(state != "Z" for _pid, _start, state in ordered):
        return ProcessGroupObservation("populated", ordered, incomplete_reason)
    if incomplete_reason:
        return ProcessGroupObservation("unverifiable", ordered, incomplete_reason)
    return ProcessGroupObservation("empty", ordered)


def process_group_members(pgid: int) -> tuple[tuple[int, str, str], ...]:
    """Compatibility view of known members; emptiness requires the typed API."""

    return process_group_observation(pgid).members


ATTEMPT_DESCENDANT_ENV = "AGENT_DISPATCH_ATTEMPT_ID"
ATTEMPT_DESCENDANT_PROOF = "attempt-tagged-empty-v1"

# Every reason `completion_marker_gate` raises because some process has not
# stopped yet. They share one exit code (78) and one meaning for the caller:
# nothing was spawned, and waiting may fix it. Adapters map this set rather
# than matching a name prefix, so a new member cannot silently fall to 65.
PRELAUNCH_PROCESS_BLOCK_REASONS = (
    "predecessor-process-draining",
    "predecessor-process-unverifiable",
    "prior-attempt-still-live",
    "prior-attempt-unverifiable",
)


def attempt_tagged_descendants(metadata: dict[str, str]) -> ProcessGroupObservation:
    """Find live processes still tagged with this attempt, whatever group they left.

    The recorded leader and process group are the only things SD-79's quiescent
    verdict looks at, so a descendant that re-``setsid``'d out of that group
    reads as absence even while it runs. Every dispatched worker carries its
    attempt id in the environment, so scanning ``/proc/<pid>/environ`` for that
    tag finds the process wherever it went.

    Emptiness is evidence only from the namespace that recorded the identities;
    from anywhere else the tagged processes may simply be invisible, so that
    case is ``unverifiable`` rather than a false death. Another uid's process is
    never one of this harness's workers, so an unreadable ``environ`` is skipped
    instead of poisoning the scan.
    """

    attempt_id = metadata.get("attempt_id", "")
    if not attempt_id:
        return ProcessGroupObservation("unverifiable", reason="attempt-id-missing")
    tag = f"{ATTEMPT_DESCENDANT_ENV}={attempt_id}".encode()
    members: list[tuple[int, str, str]] = []
    incomplete_reason = ""
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessGroupObservation(
            "unverifiable", reason=f"procfs-enumeration:{exc.errno or 'error'}"
        )
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            tail = raw[raw.rfind(")") + 2 :].split()
            state, start = tail[0], tail[19]
            if state == "Z":
                continue
            environ = (entry / "environ").read_bytes()
        except (FileNotFoundError, PermissionError):
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EPERM}:
                continue
            incomplete_reason = f"procfs-environ:{entry.name}:{exc.errno or 'error'}"
            continue
        except (IndexError, ValueError):
            incomplete_reason = f"procfs-member:{entry.name}:malformed"
            continue
        if tag in environ.split(b"\0"):
            members.append((int(entry.name), start, state))
    ordered = tuple(sorted(members, key=lambda member: member[0]))
    if ordered:
        return ProcessGroupObservation("populated", ordered, incomplete_reason)
    if incomplete_reason:
        return ProcessGroupObservation("unverifiable", (), incomplete_reason)
    if not attempt_scan_namespace_authority(metadata):
        return ProcessGroupObservation(
            "unverifiable", (), "observer-namespace-mismatch"
        )
    return ProcessGroupObservation("empty")


def _foreground_reap_receipt(metadata: dict[str, str]) -> bool:
    raw_pid = metadata.get("pid", "")
    raw_group = metadata.get("pgid", "")
    observer_namespace = metadata.get("pid_observer_ns", "")
    process_namespace = metadata.get("pid_ns", "")
    return bool(
        raw_pid.isdigit()
        and metadata.get("pid_start")
        and raw_group == raw_pid
        and observer_namespace
        and process_namespace == observer_namespace
        and metadata.get("launch_lifecycle") == "foreground-scoped"
        and metadata.get("launch_outcome") == "governed-process-reaped"
        and metadata.get("group_reap_proof") == GROUP_REAP_PROOF
        and metadata.get("group_reap_pgid") == raw_group
    )


def _detached_group_drain_receipt(metadata: dict[str, str]) -> bool:
    raw_pid = metadata.get("pid", "")
    raw_group = metadata.get("pgid", "")
    observer_namespace = metadata.get("pid_observer_ns", "")
    process_namespace = metadata.get("pid_ns", "")
    return bool(
        raw_pid.isdigit()
        and metadata.get("pid_start")
        and raw_group == raw_pid
        and observer_namespace
        and process_namespace == observer_namespace
        and metadata.get("launch_lifecycle") == "detached"
        and metadata.get("launch_outcome") == "governed-process-group-drained"
        and metadata.get("group_reap_proof") == GROUP_REAP_PROOF
        and metadata.get("group_reap_pgid") == raw_group
        and metadata.get("attempt_descendant_proof") == ATTEMPT_DESCENDANT_PROOF
        and metadata.get("attempt_descendant_observer_ns") == observer_namespace
    )


def _post_exit_receipt_reason(metadata: dict[str, str]) -> str:
    if _foreground_reap_receipt(metadata):
        return "governed-process-group-reaped"
    if _detached_group_drain_receipt(metadata):
        return "governed-process-group-drained"
    return ""


def attempt_process_quiescence(
    metadata: dict[str, str], *, terminal_receipt: bool = False
) -> ProcessQuiescence:
    """Classify the exact governed process, then prove no tagged descendant survives.

    The leader/process-group verdict below is left exactly as it was; it is only
    post-processed on its way to ``quiescent``. That is the one verdict a false
    negative can turn into a duplicate launch, and it is also the rare one, so
    the ``/proc`` scan runs only at the moment quiescence is about to be
    declared and never on a hot path. ``live`` and ``unverifiable`` keep their
    previous meaning to the letter.
    """

    result = _attempt_process_quiescence_impl(metadata)
    if result.state != "quiescent":
        return result
    # D-1: a legacy row records no attempt id, so there is no tag to scan for.
    # Answering `unverifiable` for all of them would retroactively freeze every
    # successor, join, wait, and cleanup gate that reads an old row, so they
    # keep the verdict they already had instead.
    if not metadata.get("attempt_id"):
        return result
    probe = attempt_tagged_descendants(metadata)
    if probe.state == "populated":
        return ProcessQuiescence(
            "live", "attempt-descendant-live", probe.members[0][0]
        )
    if probe.state == "unverifiable":
        # SD-79/80/89: the observer that produced a complete post-exit receipt
        # may itself have disappeared before a successor or retry is launched.
        # Consume that receipt only at an exact terminal gate, and only for the
        # one unavailability it can explain. A visible tagged process already
        # returned above, while incomplete scans remain fail-closed.
        if (
            terminal_receipt
            and probe.reason == "observer-namespace-mismatch"
            and _post_exit_receipt_reason(metadata)
        ):
            return result
        return ProcessQuiescence("unverifiable", "attempt-descendant-unverifiable")
    return result


def _attempt_process_quiescence_impl(metadata: dict[str, str]) -> ProcessQuiescence:
    """Classify the exact governed process without PID-namespace guessing.

    A candidate PID is authoritative only in the namespace that observed it, or
    when a namespace-bound ``NSpid`` mapping is checked from that same namespace.
    Missing identity is never synthesized into success unless the atomic launch
    path explicitly recorded that no governed process remains.
    """

    launch_outcome = metadata.get("launch_outcome", "")

    raw_pid = metadata.get("pid", "")
    if not raw_pid:
        if launch_outcome in {
            "never-launched",
            "reaped-before-publish",
        }:
            return ProcessQuiescence("quiescent", launch_outcome)
        return ProcessQuiescence("unverifiable", "process-identity-missing")
    if not raw_pid.isdigit() or not metadata.get("pid_start"):
        return ProcessQuiescence("unverifiable", "process-identity-invalid")

    candidates = authoritative_process_identities(metadata)
    receipt_reason = _post_exit_receipt_reason(metadata)

    if not candidates:
        if receipt_reason:
            return ProcessQuiescence("quiescent", receipt_reason)
        return ProcessQuiescence("unverifiable", "process-namespace-unverifiable")

    terminal: list[ProcessQuiescence] = []
    unresolved: list[str] = []
    for candidate in candidates:
        source, pid, expected_start = (
            candidate.source,
            candidate.pid,
            candidate.expected_start,
        )
        visibility, actual_start, state = _proc_observation(pid)
        if visibility == "inaccessible":
            unresolved.append(f"{source}-process-identity-inaccessible")
            continue
        group_field = "pgid_host" if source == "host" else "pgid"
        raw_group = metadata.get(group_field, "")
        group_id = int(raw_group) if raw_group.isdigit() else None
        group_is_owned = group_id == pid
        if visibility == "missing":
            if not group_is_owned:
                unresolved.append(f"{source}-process-group-identity-unverifiable")
                continue
            group = process_group_observation(group_id)
            live_members = [member for member in group.members if member[2] != "Z"]
            if live_members:
                return ProcessQuiescence(
                    "live",
                    f"{source}-process-group-live",
                    live_members[0][0],
                    candidate,
                )
            if group.state != "empty":
                unresolved.append(f"{source}-process-group-unverifiable")
                continue
            terminal_reason = f"{source}-pid-gone"
            if receipt_reason:
                terminal_reason = receipt_reason
            terminal.append(
                ProcessQuiescence("quiescent", terminal_reason, pid, candidate)
            )
            continue
        if actual_start != expected_start:
            terminal.append(
                ProcessQuiescence(
                    "quiescent", f"{source}-pid-reused", pid, candidate
                )
            )
            continue
        if state == "Z":
            if not group_is_owned:
                unresolved.append(f"{source}-process-group-identity-unverifiable")
                continue
            group = process_group_observation(group_id)
            live_members = [
                member
                for member in group.members
                if member[0] != pid and member[2] != "Z"
            ]
            if live_members:
                return ProcessQuiescence(
                    "live",
                    f"{source}-process-group-live",
                    live_members[0][0],
                    candidate,
                )
            if group.state != "empty":
                unresolved.append(f"{source}-process-group-unverifiable")
                continue
            terminal.append(
                ProcessQuiescence(
                    "quiescent", f"{source}-pid-zombie", pid, candidate
                )
            )
            continue
        return ProcessQuiescence("live", f"{source}-pid-live", pid, candidate)
    if receipt_reason:
        return ProcessQuiescence("quiescent", receipt_reason)
    if unresolved:
        return ProcessQuiescence("unverifiable", unresolved[0])
    if terminal:
        return terminal[0]
    return ProcessQuiescence("unverifiable", "process-identity-unverifiable")


def observed_attempt_liveness(
    status: str,
    metadata: dict[str, str],
    *,
    terminal_envelope: bool = False,
) -> ObservedAttemptLiveness:
    """Combine registry state and exact process evidence without mutation.

    Consumers may supply only whether an exact final runtime envelope exists;
    its content remains private to the terminal classifier.  An open row whose
    governed process is gone is never synthesized back to alive.  It becomes a
    visible reconciliation obligation, whether or not the envelope survived.
    """

    process = attempt_process_quiescence(
        metadata,
        terminal_receipt=status in {"done", "killed", "cancelled"},
    )
    if status in {"open", "running"}:
        if process.state == "live":
            state, reason = "alive", process.reason
        elif process.state == "quiescent":
            state = "reconcile-needed"
            reason = "terminal-observed" if terminal_envelope else "process-exited"
        else:
            state, reason = "unverifiable", process.reason
    elif status in {"done", "killed", "cancelled"}:
        if process.state == "quiescent":
            state, reason = "terminal", "registry-closed"
        elif process.state == "live":
            state, reason = "alive", "registry-terminal-process-live"
        else:
            state, reason = "unverifiable", process.reason
    else:
        state, reason = "unverifiable", "registry-status-invalid"
    return ObservedAttemptLiveness(
        state=state,
        reason=reason,
        process_state=process.state,
        process_reason=process.reason,
    )


def observed_supervised_owner_liveness(
    jobs: str | Path,
    status: str,
    metadata: dict[str, str],
    *,
    supervisor_phase: str = "",
    terminal_envelope: bool = False,
) -> ObservedAttemptLiveness:
    """Classify an owner without confusing an inner turn exit for owner death.

    ``parked`` and ``deliverable`` are runtime-owned phases.  They count as
    alive only while the exact outer supervisor lease is both well-formed and
    held; a stale file, foreign nonce, or PID-reused process remains
    fail-closed.  All other cases retain the ordinary exact-attempt verdict.
    """

    if (
        status in {"open", "running"}
        and supervisor_phase in {"parked", "deliverable", "recovery"}
        and supervisor_lease_is_held(jobs, metadata)
    ):
        return ObservedAttemptLiveness(
            state="parked-supervised",
            reason=f"supervisor-{supervisor_phase}",
            process_state="live",
            process_reason="supervisor-lease-held",
        )
    return observed_attempt_liveness(
        status,
        metadata,
        terminal_envelope=terminal_envelope,
    )


def _governor_json(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    allow_absent: bool = False,
) -> dict[str, object]:
    result = subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        payload = {}
    if (
        result.returncode != 0
        and not (allow_absent and isinstance(payload, dict) and payload.get("state") == "absent")
    ) or not isinstance(payload, dict):
        detail = (result.stderr or result.stdout).strip()[:512] or f"exit-{result.returncode}"
        raise DispatchContractError("model-worker-governor-denied", detail)
    return payload


def replica_batch_expectation(
    route_file: str | Path | None,
    route_node: str | None,
    action: str,
    *,
    attempt_id: str = "",
    parent_attempt_id: str = "",
    harness: str = "",
    fallback_hop: str = "",
    fallback_ordinal: int | str | None = None,
    assignment_sha256: str = "",
) -> dict[str, object] | None:
    """Return the exact governor binding required by a parallel route leg.

    A parallel row has no standalone registered form. ``start`` is authorized
    only by a live opaque governor reservation whose immutable provenance was
    created from the complete 2..4-leg manifest by ``dispatch-batch``. The
    function name remains as a one-window adapter import alias.
    """

    if not route_file or not route_node:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DispatchContractError("route-record-unreadable", str(exc)) from exc
    if not isinstance(route, dict) or not isinstance(route.get("nodes"), list):
        raise DispatchContractError("route-record-invalid", "route nodes must be an array")
    matches = [
        node for node in route["nodes"]
        if isinstance(node, dict) and node.get("id") == route_node
    ]
    if len(matches) != 1:
        raise DispatchContractError("route-node-not-unique", str(route_node))
    node = matches[0]
    group = node.get("parallel_group") or node.get("replica_group")
    if not group:
        return None
    members = [
        candidate for candidate in route["nodes"]
        if isinstance(candidate, dict)
        and (candidate.get("parallel_group") or candidate.get("replica_group")) == group
    ]
    if not 2 <= len(members) <= 4 or any(candidate.get("dispatch_depth") != 2 for candidate in members):
        raise DispatchContractError(
            "parallel-group-contract-invalid", f"group={group} count={len(members)}"
        )
    if action != "start":
        raise DispatchContractError(
            "parallel-group-batch-required",
            f"group={group} action={action}; use dispatch-batch --parallel-group {group} --action start",
        )
    values = {
        "attempt_id": attempt_id,
        "parent_attempt_id": parent_attempt_id,
        "harness": harness,
        "fallback_hop": fallback_hop,
        "fallback_ordinal": str(fallback_ordinal or ""),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise DispatchContractError(
            "parallel-group-batch-binding-missing", ",".join(missing)
        )
    try:
        ordinal = int(str(fallback_ordinal))
    except (TypeError, ValueError) as exc:
        raise DispatchContractError(
            "parallel-group-batch-binding-invalid",
            f"fallback_ordinal={fallback_ordinal}",
        ) from exc
    if ordinal < 1:
        raise DispatchContractError(
            "parallel-group-batch-binding-invalid",
            f"fallback_ordinal={fallback_ordinal}",
        )
    allowed_members: dict[str, list[dict[str, object]]] = {}
    for member in members:
        member_id = str(member.get("id", ""))
        allowed: list[dict[str, object]] = []
        for entry in member.get("fallback_hops", []):
            if not isinstance(entry, dict):
                continue
            hop = entry.get("fallback_hop")
            hop_ordinal = entry.get("ordinal")
            if not isinstance(hop, str) or isinstance(hop_ordinal, bool) or not isinstance(hop_ordinal, int):
                continue
            for candidate in entry.get("candidates", []):
                if not isinstance(candidate, dict) or candidate.get("status") != "supported":
                    continue
                child_harness = candidate.get("child_harness")
                if child_harness not in {"codex", "claude", "opencode"}:
                    continue
                allowed.append({
                    "harness": child_harness,
                    "fallback_hop": hop,
                    "fallback_ordinal": hop_ordinal,
                })
        if not allowed:
            raise DispatchContractError(
                "parallel-group-route-binding-invalid", f"node={member_id}"
            )
        allowed_members[member_id] = allowed
    expected = {
        "reservation_kind": "parallel-batch" if node.get("parallel_group") else "replica-batch",
        "batch_declared_size": len(members),
        "batch_group": str(group),
        "batch_route_id": str(route.get("route_id", "")),
        "batch_parent_attempt_id": parent_attempt_id,
        "batch_attempt_id": attempt_id,
        "batch_route_node": str(route_node),
        "batch_harness": harness,
        "batch_fallback_hop": fallback_hop,
        "batch_fallback_ordinal": ordinal,
        "batch_model_profile": node.get("model_profile"),
        "batch_perspective": node.get("perspective"),
        "batch_parallel_leg_index": node.get("parallel_leg_index"),
        "_batch_route_nodes": sorted(str(member.get("id", "")) for member in members),
        "_batch_allowed_members": allowed_members,
    }
    if assignment_sha256:
        if not DIGEST.fullmatch(assignment_sha256):
            raise DispatchContractError(
                "parallel-group-assignment-invalid", assignment_sha256
            )
        expected["batch_assignment_sha256"] = assignment_sha256
    return expected


def _validate_replica_reservation(
    payload: dict[str, object], expected: dict[str, object] | None
) -> None:
    if expected is None:
        if payload.get("reservation_kind") in {"replica-batch", "parallel-batch"}:
            raise DispatchContractError(
                "parallel-group-reservation-mismatch",
                "parallel batch token cannot authorize a non-group start",
            )
        return
    public_expected = {
        key: value for key, value in expected.items() if not key.startswith("_")
    }
    mismatches = {
        key: (value, payload.get(key))
        for key, value in public_expected.items()
        if payload.get(key) != value
    }
    for key in ("batch_manifest_sha256", "batch_leg_sha256"):
        value = payload.get(key)
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            mismatches[key] = ("sha256:<64 lowercase hex>", value)
    manifest = payload.get("batch_manifest")
    try:
        verified, manifest_digest, leg_digests = verify_manifest(manifest)
    except ReplicaBatchContractError as exc:
        mismatches["batch_manifest"] = ("valid canonical manifest", str(exc))
        verified, manifest_digest, leg_digests = {}, "", {}
    if manifest_digest and payload.get("batch_manifest_sha256") != manifest_digest:
        mismatches["batch_manifest_sha256"] = (
            manifest_digest,
            payload.get("batch_manifest_sha256"),
        )
    if verified:
        common = {
            "route_id": public_expected.get("batch_route_id"),
            "parent_attempt_id": public_expected.get("batch_parent_attempt_id"),
        }
        manifest_group = verified.get("parallel_group") or verified.get("replica_group")
        if manifest_group != public_expected.get("batch_group"):
            mismatches["manifest.parallel_group"] = (
                public_expected.get("batch_group"), manifest_group
            )
        for key, value in common.items():
            if verified.get(key) != value:
                mismatches[f"manifest.{key}"] = (value, verified.get(key))
        route_nodes = sorted(str(member.get("route_node", "")) for member in verified["members"])
        if route_nodes != expected.get("_batch_route_nodes"):
            mismatches["manifest.route_nodes"] = (
                expected.get("_batch_route_nodes"), route_nodes
            )
        allowed = expected.get("_batch_allowed_members", {})
        for manifest_member in verified["members"]:
            member_node = str(manifest_member.get("route_node", ""))
            allowed_for_member = (
                allowed.get(member_node, []) if isinstance(allowed, dict) else []
            )
            member_tuple = {
                "harness": manifest_member.get("harness"),
                "fallback_hop": manifest_member.get("fallback_hop"),
                "fallback_ordinal": manifest_member.get("fallback_ordinal"),
            }
            if member_tuple not in allowed_for_member:
                mismatches[f"manifest.member.{member_node}.route_binding"] = (
                    allowed_for_member, member_tuple
                )
        selected = [
            member for member in verified["members"]
            if member.get("attempt_id") == public_expected.get("batch_attempt_id")
        ]
        if len(selected) != 1:
            mismatches["manifest.selected_member"] = (
                public_expected.get("batch_attempt_id"), len(selected)
            )
        else:
            member = selected[0]
            member_expected = {
                "route_node": public_expected.get("batch_route_node"),
                "harness": public_expected.get("batch_harness"),
                "fallback_hop": public_expected.get("batch_fallback_hop"),
                "fallback_ordinal": public_expected.get("batch_fallback_ordinal"),
            }
            if int(verified.get("schema_version", 1)) == 2:
                member_expected.update({
                    "model_profile": public_expected.get("batch_model_profile"),
                    "perspective": public_expected.get("batch_perspective"),
                    "parallel_leg_index": public_expected.get("batch_parallel_leg_index"),
                })
            for key, value in member_expected.items():
                if member.get(key) != value:
                    mismatches[f"manifest.member.{key}"] = (value, member.get(key))
            expected_assignment = public_expected.get("batch_assignment_sha256")
            if expected_assignment and member.get("assignment_sha256") != expected_assignment:
                mismatches["manifest.member.assignment_sha256"] = (
                    expected_assignment, member.get("assignment_sha256")
                )
            attempt = str(member.get("attempt_id", ""))
            if payload.get("batch_leg_sha256") != leg_digests.get(attempt):
                mismatches["batch_leg_sha256"] = (
                    leg_digests.get(attempt), payload.get("batch_leg_sha256")
                )
        if payload.get("batch_independence") != verified.get("independence"):
            mismatches["batch_independence"] = (
                verified.get("independence"), payload.get("batch_independence")
            )
    declared_size = public_expected.get("batch_declared_size")
    admission = payload.get("batch_admission_count")
    if (isinstance(declared_size, bool) or not isinstance(declared_size, int)
            or not 2 <= declared_size <= 4):
        mismatches["batch_declared_size"] = ("integer 2..4", declared_size)
        declared_size = 0
    if isinstance(admission, bool) or admission not in {1, declared_size}:
        mismatches["batch_admission_count"] = (f"1|{declared_size}", admission)
    elif admission == 1:
        selected_attempt = str(public_expected.get("batch_attempt_id", ""))
        peer_members = (
            [
                member for member in verified.get("members", [])
                if str(member.get("attempt_id", "")) != selected_attempt
            ]
            if verified
            else []
        )
        expected_peers = sorted(str(member.get("attempt_id", "")) for member in peer_members)
        proof_keys = {
            "agent_home", "attempt_id", "jobs", "manifest_sha256",
            "reason", "route", "state",
        }
        proofs = payload.get("batch_peer_set")
        if payload.get("batch_peer_count") != len(expected_peers):
            mismatches["batch_peer_count"] = (len(expected_peers), payload.get("batch_peer_count"))
        if not isinstance(proofs, list) or len(proofs) != len(expected_peers):
            mismatches["batch_peer_set"] = ("exact N-1 canonical proofs", proofs)
        else:
            actual_peers=[]
            for index, proof in enumerate(proofs):
                label=f"batch_peer_set[{index}]"
                if not isinstance(proof, dict) or set(proof) != proof_keys:
                    mismatches[label] = ("canonical peer proof", proof)
                    continue
                actual_peers.append(str(proof.get("attempt_id", "")))
                if proof.get("manifest_sha256") != manifest_digest:
                    mismatches[f"{label}.manifest_sha256"] = (manifest_digest, proof.get("manifest_sha256"))
                if proof.get("state") not in {"active", "completed"}:
                    mismatches[f"{label}.state"] = ("active|completed", proof.get("state"))
                for key in ("agent_home", "jobs", "route"):
                    value=proof.get(key)
                    if not isinstance(value,str) or not Path(value).is_absolute():
                        mismatches[f"{label}.{key}"] = ("absolute path", value)
                if not isinstance(proof.get("reason"),str) or not proof.get("reason"):
                    mismatches[f"{label}.reason"] = ("non-empty observation reason", proof.get("reason"))
            if actual_peers != expected_peers:
                mismatches["batch_peer_set.attempts"] = (expected_peers, actual_peers)
            encoded=json.dumps(proofs,separators=(",",":"),sort_keys=True).encode("utf-8")
            proof_digest="sha256:"+hashlib.sha256(encoded).hexdigest()
            if payload.get("batch_peer_set_sha256") != proof_digest:
                mismatches["batch_peer_set_sha256"] = (proof_digest,payload.get("batch_peer_set_sha256"))
    elif admission == declared_size:
        for key in (
            "batch_peer_count", "batch_peer_set", "batch_peer_set_sha256",
            "batch_peer_attempt_id", "batch_peer_state",
            "batch_peer_proof", "batch_peer_proof_sha256",
        ):
            if key in payload:
                mismatches[key] = ("absent for full batch", payload.get(key))
    if mismatches:
        detail = ";".join(
            f"{key}:expected={wanted}:actual={actual}"
            for key, (wanted, actual) in sorted(mismatches.items())
        )
        raise DispatchContractError("parallel-group-reservation-mismatch", detail)


def reserve_governor_token(
    governor: Path,
    root: Path,
    worker_class: str,
    *,
    provided_token: str = "",
    expected_reservation: dict[str, object] | None = None,
) -> tuple[str, dict[str, object]]:
    """Reserve one slot, or validate a token atomically reserved by a batch."""

    if provided_token:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                provided_token,
                "--class",
                worker_class,
            ],
            allow_absent=True,
        )
        if payload.get("state") != "unclaimed":
            raise DispatchContractError(
                "model-worker-reservation-unavailable", str(payload.get("state", "invalid"))
            )
        _validate_replica_reservation(payload, expected_reservation)
        return provided_token, payload
    if expected_reservation is not None:
        raise DispatchContractError(
            "parallel-group-batch-required",
            "parallel start requires an exact bound batch reservation",
        )
    payload = _governor_json(
        [
            sys.executable,
            str(governor),
            "--root",
            str(root),
            "reserve",
            "--class",
            worker_class,
            "--count",
            "1",
            "--pid",
            str(os.getpid()),
        ]
    )
    tokens = payload.get("tokens")
    if not isinstance(tokens, list) or len(tokens) != 1 or not isinstance(tokens[0], str):
        raise DispatchContractError("model-worker-reservation-invalid", "expected one token")
    return tokens[0], {}


def cancel_governor_reservation(governor: Path, root: Path, token: str) -> None:
    """Cancel only an unclaimed token; a claimed runner retains its lease."""

    if not token:
        return
    try:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                token,
            ]
        )
    except DispatchContractError:
        return
    if payload.get("state") != "unclaimed":
        return
    subprocess.run(
        [
            sys.executable,
            str(governor),
            "--root",
            str(root),
            "cancel",
            "--token",
            token,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def wait_governor_reservation_claim(
    governor: Path,
    root: Path,
    token: str,
    proc: subprocess.Popen,
    *,
    timeout: float = 5.0,
    expected_reservation: dict[str, object] | None = None,
) -> dict[str, object]:
    """Observe reserve→runner transfer before the reserving process may exit."""

    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        payload = _governor_json(
            [
                sys.executable,
                str(governor),
                "--root",
                str(root),
                "reservation-check",
                "--token",
                token,
                "--class",
                "dispatch",
            ],
            allow_absent=True,
        )
        if payload.get("state") == "claimed":
            _validate_replica_reservation(payload, expected_reservation)
            if (
                str(payload.get("claimant_pid", "")) != str(proc.pid)
                or str(payload.get("claimant_starttime", ""))
                != str(process_start_ticks(proc.pid) or payload.get("claimant_starttime", ""))
            ):
                raise DispatchContractError(
                    "model-worker-reservation-claim-mismatch",
                    f"expected_pid={proc.pid} claimant_pid={payload.get('claimant_pid', '-')}",
                )
            return payload
        if payload.get("state") == "absent":
            raise DispatchContractError(
                "model-worker-reservation-lost",
                "reservation disappeared before the governed runner claimed it",
            )
        if proc.poll() is not None or time.monotonic() >= deadline:
            raise DispatchContractError(
                "model-worker-reservation-claim-timeout",
                f"state={payload.get('state', 'unknown')} exit={proc.returncode}",
            )
        time.sleep(0.02)


_PROCESS_IDENTITY_METADATA_KEYS = {
    "pid",
    "pid_start",
    "pid_host",
    "pid_host_start",
    "pid_host_ns",
    "pid_ns",
    "pid_observer_ns",
    "pid_host_proof",
    "pgid",
    "pgid_host",
}


def _launch_identity_complete(pid: int, identity: dict[str, str]) -> bool:
    observer_namespace = identity.get("pid_observer_ns", "")
    expected_start = identity.get("pid_start", "")
    if not (
        identity.get("pid") == str(pid)
        and expected_start
        and observer_namespace
        and identity.get("pid_ns") == observer_namespace
        and identity.get("pgid") == str(pid)
    ):
        return False
    visibility, actual_start, state = _proc_observation(pid)
    if not (
        visibility == "present"
        and actual_start == expected_start
        and state != "Z"
        and exact_process_group_signal_authority(pid, expected_start)
        == "authoritative"
    ):
        return False

    host_keys = {
        "pid_host",
        "pid_host_start",
        "pid_host_ns",
        "pid_host_proof",
        "pgid_host",
    }
    if any(identity.get(key) for key in host_keys):
        raw_host = identity.get("pid_host", "")
        if not (
            raw_host.isdigit()
            and identity.get("pid_host_start") == expected_start
            and identity.get("pid_host_ns")
            and identity.get("pid_host_proof") == PID_HOST_NAMESPACE_PROOF
            and identity.get("pgid_host") == raw_host
        ):
            return False
    return True


def _abort_fenced_launch(
    proc: subprocess.Popen,
    gate_write: int,
    expected_start: str,
) -> bool:
    """Close an unreleased gate and verify that its exact group is empty."""

    try:
        os.close(gate_write)
    except OSError:
        pass
    try:
        proc.wait(timeout=0.75)
    except (OSError, subprocess.TimeoutExpired):
        status = (
            signal_exact_process_group(proc.pid, expected_start, signal.SIGKILL)
            if expected_start
            else "identity-unverifiable"
        )
        if status != "signalled":
            try:
                proc.kill()
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.wait(timeout=0.75)
        except (OSError, subprocess.TimeoutExpired):
            pass
    group = process_group_observation(proc.pid)
    return proc.poll() is not None and group.state == "empty"


def _parent_liveness_evidence(
    jobs: Path, metadata: dict[str, str]
) -> tuple[bool, str, AuthoritativeProcessIdentity | None]:
    process = attempt_process_quiescence(metadata)
    if process.state == "live" and process.identity is not None:
        return True, "process", process.identity
    if (
        process.state == "unverifiable"
        and process.reason == "process-namespace-unverifiable"
        and supervisor_lease_is_held(jobs, metadata)
    ):
        return True, "supervisor-lease", None
    return False, process.reason, None


class ParentCompletionWindow(NamedTuple):
    """Whether an exact live parent still owns delivery of a child's result."""

    deferred: bool
    source: str


def parent_completion_window(
    jobs: Path, child_fields: list[str], child_metadata: dict[str, str]
) -> ParentCompletionWindow:
    """Decide whether a proven-live exact parent still owns this child's completion.

    F-1: extends the S-3 missing-result closure axis with the delivering
    parent's liveness, so reap-watch does not race a still-live conductor's
    ``capability-route.py complete``. Never acquires ``<jobs>.lock`` (SD-49) —
    callers under the lock re-evaluate this as the sole authoritative
    decision point (see ``still_orphan`` in ``dispatch-registry.py`` for the
    same unlocked-read precedent).
    """

    parent_attempt_id = child_metadata.get("parent_attempt_id", "")
    if not parent_attempt_id:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    try:
        lines = Path(jobs).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    all_matches: list[tuple[list[str], dict[str, str]]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") == parent_attempt_id:
            all_matches.append((fields, metadata))
    if not all_matches:
        return ParentCompletionWindow(False, "parent-attempt-absent")
    open_matches = [m for m in all_matches if m[0][1] in {"open", "running"}]
    if not open_matches:
        return ParentCompletionWindow(False, "parent-attempt-not-open")
    if len(open_matches) > 1:
        return ParentCompletionWindow(False, "parent-attempt-ambiguous")
    parent_fields, parent_metadata = open_matches[0]
    try:
        validate_attempt_metadata(parent_metadata)
    except DispatchContractError:
        return ParentCompletionWindow(False, "parent-contract-invalid")
    # Strict AND on the same two axes `spawn_claimed_attempt` already treats as
    # the canonical depth-1 owner identity (dispatch_depth == "1" and
    # worker_type == "owner"); depth-3 dispatch is forbidden, so no other
    # parent role exists to widen this against (plan-check round 1, finding 3).
    same_identity = (
        parent_metadata.get("dispatch_depth") == "1"
        and parent_metadata.get("worker_type") == "owner"
        and parent_fields[3] == child_fields[3]
        and canonical_repository_identity(parent_fields[2])
        == canonical_repository_identity(child_fields[2])
        and parent_fields[4] == child_metadata.get("parent")
    )
    if not same_identity:
        return ParentCompletionWindow(False, "parent-identity-foreign")
    live, reason, _identity = _parent_liveness_evidence(jobs, parent_metadata)
    if not live:
        return ParentCompletionWindow(False, f"parent-not-live:{reason}")
    return ParentCompletionWindow(True, f"parent-live:{reason}")


def _parent_metadata_matches_binding(
    metadata: dict[str, str], binding: ParentAttemptBinding
) -> bool:
    return tuple(
        (key, metadata.get(key, "")) for key in PARENT_LIVENESS_METADATA_KEYS
    ) == binding.liveness_metadata_fingerprint


def _parent_binding_is_live_from_metadata(
    jobs: Path,
    metadata: dict[str, str],
    binding: ParentAttemptBinding,
) -> bool:
    if not _parent_metadata_matches_binding(metadata, binding):
        return False
    if (
        binding.observed_pid is not None
        and binding.observed_pid_start
    ):
        return process_identity_is_live(
            binding.observed_pid, binding.observed_pid_start
        )
    live, _source, _identity = _parent_liveness_evidence(jobs, metadata)
    return live


def parent_attempt_binding_is_live(
    jobs: str | Path, binding: ParentAttemptBinding
) -> bool:
    """Revalidate one exact parent row plus its current liveness evidence."""

    registry = Path(jobs).expanduser().resolve(strict=False)
    try:
        with Path(f"{registry}.lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            lines = registry.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            matches: list[tuple[list[str], dict[str, str]]] = []
            for line in lines:
                fields = line.split("\t")
                if len(fields) != 6 or fields[1] not in {"open", "running"}:
                    continue
                metadata = parse_registry_metadata(fields[5])
                if metadata.get("attempt_id") == binding.attempt_id:
                    matches.append((fields, metadata))
            if len(matches) != 1:
                return False
            fields, metadata = matches[0]
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError:
                return False
            if (
                fields[4] != binding.slug
                or fields[3] != binding.worktree
                or canonical_repository_identity(fields[2])
                != binding.repository_identity
            ):
                return False
            return _parent_binding_is_live_from_metadata(
                registry, metadata, binding
            )
    except OSError:
        return False


def spawn_claimed_attempt(
    jobs: Path,
    attempt_id: str,
    *,
    parent_binding: ParentAttemptBinding | None,
    spawn: Callable[[int], subprocess.Popen],
    launch_metadata: dict[str, str] | None = None,
    preclaim: Callable[[list[str]], None] | None = None,
    pre_release: Callable[[dict[str, str]], dict[str, str] | None] | None = None,
) -> tuple[subprocess.Popen, dict[str, str]]:
    """Claim one registered attempt while publishing its fenced process.

    The row stays ``launch_claimed=0`` until a complete fenced PID identity is
    ready. The same registry replacement publishes the identity and transitions
    the claim to 1. A launcher killed before spawn therefore leaves a retryable
    registered row, while a launcher killed after spawn leaves either a blocked
    fence or a fully attributable process group. ``pre_release`` may attach a
    bounded observer to that exact identity; its metadata is committed in the
    same replacement and any failure aborts the still-fenced worker.
    """

    if not attempt_id:
        raise DispatchContractError("attempt-id-required")
    ensure_global_registry_writable(jobs)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) == 6 and row_has_attempt(fields[5], attempt_id):
                matches.append((index, fields, parse_registry_metadata(fields[5])))
        if len(matches) != 1:
            raise DispatchContractError(
                "attempt-row-not-unique", f"attempt_id={attempt_id} rows={len(matches)}"
            )
        child_index, child_fields, child_meta = matches[0]
        validate_attempt_metadata(child_meta)
        if child_fields[1] not in {"open", "running"}:
            raise DispatchContractError("attempt-not-open", attempt_id)
        if child_meta.get("launch_claimed") == "1":
            raise DispatchContractError("attempt-launch-already-claimed", attempt_id)
        if child_meta.get("launch_claimed") != "0":
            raise DispatchContractError("attempt-launch-claim-invalid", attempt_id)

        if parent_binding is not None:
            parent_matches = []
            for line in lines:
                fields = line.split("\t")
                if len(fields) != 6 or fields[1] not in {"open", "running"}:
                    continue
                meta = parse_registry_metadata(fields[5])
                if meta.get("attempt_id") == parent_binding.attempt_id:
                    parent_matches.append((fields, meta))
            if len(parent_matches) != 1:
                raise DispatchContractError(
                    "parent-attempt-not-live", parent_binding.attempt_id
                )
            parent_fields, parent_meta = parent_matches[0]
            try:
                validate_attempt_metadata(parent_meta)
            except DispatchContractError as exc:
                raise DispatchContractError(
                    "parent-attempt-not-live", parent_binding.attempt_id
                ) from exc
            same_identity = (
                parent_meta.get("dispatch_depth") == "1"
                and parent_meta.get("worker_type") == "owner"
                and canonical_repository_identity(parent_fields[2])
                == parent_binding.repository_identity
                and canonical_repository_identity(child_fields[2])
                == parent_binding.repository_identity
                and parent_fields[3] == child_fields[3]
                and parent_fields[4] == child_meta.get("parent")
                and child_meta.get("parent_attempt_id") == parent_binding.attempt_id
                and parent_fields[3] == parent_binding.worktree
                and parent_fields[4] == parent_binding.slug
                and _parent_metadata_matches_binding(parent_meta, parent_binding)
            )
            if not same_identity:
                raise DispatchContractError(
                    "parent-attempt-identity-changed", parent_binding.attempt_id
                )
            if not _parent_binding_is_live_from_metadata(
                jobs, parent_meta, parent_binding
            ):
                raise DispatchContractError(
                    "parent-attempt-not-live", parent_binding.attempt_id
                )

        if preclaim is not None:
            preclaim(lines)

        gate_read, gate_write = os.pipe()
        try:
            proc = spawn(gate_read)
        except BaseException:
            os.close(gate_read)
            os.close(gate_write)
            raise
        os.close(gate_read)
        identity = process_launch_identity(proc.pid)
        provided_metadata = {
            key: str(value)
            for key, value in (launch_metadata or {}).items()
            if value not in (None, "")
        }
        conflicting_identity = sorted(
            _PROCESS_IDENTITY_METADATA_KEYS.intersection(provided_metadata)
        )
        if conflicting_identity:
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity.get("pid_start", "")
            )
            raise DispatchContractError(
                (
                    "attempt-launch-identity-metadata-conflict"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                ",".join(conflicting_identity),
            )
        if not _launch_identity_complete(proc.pid, identity):
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity.get("pid_start", "")
            )
            raise DispatchContractError(
                (
                    "attempt-launch-identity-incomplete"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                f"pid={proc.pid}",
            )
        observer_metadata: dict[str, str] = {}
        if pre_release is not None:
            try:
                observer_metadata = {
                    key: str(value)
                    for key, value in (pre_release(dict(identity)) or {}).items()
                    if value not in (None, "")
                }
            except BaseException as exc:
                cleanup_verified = _abort_fenced_launch(
                    proc, gate_write, identity.get("pid_start", "")
                )
                raise DispatchContractError(
                    (
                        "attempt-pre-release-callback-failed"
                        if cleanup_verified
                        else "attempt-launch-cleanup-unverified"
                    ),
                    str(exc),
                ) from exc
        callback_conflicts = sorted(
            ({*_PROCESS_IDENTITY_METADATA_KEYS, "launch_claimed"})
            .intersection(observer_metadata)
        )
        if callback_conflicts:
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity.get("pid_start", "")
            )
            raise DispatchContractError(
                (
                    "attempt-pre-release-metadata-conflict"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                ",".join(callback_conflicts),
            )
        identity.update(provided_metadata)
        identity.update(observer_metadata)
        try:
            replace_keys = {*identity, "launch_claimed"}
            parts = [
                part
                for part in child_fields[5].split(",")
                if part.split("=", 1)[0] not in replace_keys
            ]
            parts.extend(f"{key}={value}" for key, value in sorted(identity.items()))
            parts.append("launch_claimed=1")
            child_fields[5] = ",".join(parts)
            lines[child_index] = "\t".join(child_fields)
            _atomic_registry_replace(jobs, lines)
        except OSError as exc:
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity.get("pid_start", "")
            )
            raise DispatchContractError(
                (
                    "attempt-launch-identity-record-failed"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                str(exc),
            ) from exc
        if parent_binding is not None and not _parent_binding_is_live_from_metadata(
            jobs, parent_meta, parent_binding
        ):
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity["pid_start"]
            )
            raise DispatchContractError(
                (
                    "parent-attempt-not-live-after-spawn"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                parent_binding.attempt_id,
            )
        try:
            os.write(gate_write, b"1")
        except OSError as exc:
            cleanup_verified = _abort_fenced_launch(
                proc, gate_write, identity["pid_start"]
            )
            raise DispatchContractError(
                (
                    "attempt-launch-fence-release-failed"
                    if cleanup_verified
                    else "attempt-launch-cleanup-unverified"
                ),
                str(exc),
            ) from exc
        else:
            os.close(gate_write)
        return proc, identity


def resolve_live_parent_attempt(
    jobs: Path,
    *,
    parent_slug: str,
    repo: str,
    worktree: str,
    expected_attempt_id: str | None = None,
    expected_harness: str | None = None,
    expected_transport: str | None = None,
    expected_sandbox: str | None = None,
) -> ParentAttemptBinding:
    """Resolve exactly one open, live depth-1 owner before a depth-2 claim.

    A slug is only a lookup constraint.  Teardown authority is the returned
    attempt id, and a same-slug retry cannot satisfy an explicitly inherited
    parent attempt id.
    """

    if not parent_slug:
        raise DispatchContractError("parent-slug-required", "depth-2 parent is required")
    requested_repository = canonical_repository_identity(repo)
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        candidates: list[tuple[list[str], dict[str, str]]] = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            if (
                canonical_repository_identity(fields[2]) != requested_repository
                or fields[3] != worktree
                or fields[4] != parent_slug
            ):
                continue
            metadata = parse_registry_metadata(fields[5])
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError:
                continue
            if metadata.get("dispatch_depth") != "1" or metadata.get("worker_type") != "owner":
                continue
            if expected_attempt_id and metadata.get("attempt_id") != expected_attempt_id:
                continue
            expected_runtime = {
                "harness": expected_harness,
                "transport": expected_transport,
                "runtime_sandbox": expected_sandbox,
            }
            if any(
                value is not None and metadata.get(key) != value
                for key, value in expected_runtime.items()
            ):
                continue
            candidates.append((fields, metadata))

        if not candidates:
            reason = "parent-attempt-not-found" if expected_attempt_id else "live-parent-not-found"
            raise DispatchContractError(reason, expected_attempt_id or parent_slug)
        if len(candidates) != 1:
            raise DispatchContractError(
                "parent-attempt-ambiguous",
                f"parent={parent_slug} candidates={len(candidates)}",
            )
        parent_fields, metadata = candidates[0]
        attempt_id = metadata.get("attempt_id", "")
        raw_pid = metadata.get("pid", "")
        pid_start = metadata.get("pid_start", "")
        raw_host = metadata.get("pid_host", "")
        host_start = metadata.get("pid_host_start", "") or pid_start
        if not attempt_id or not raw_pid.isdigit() or not pid_start:
            raise DispatchContractError("parent-process-identity-missing", attempt_id or parent_slug)
        pid = int(raw_pid)
        host_pid = int(raw_host) if raw_host.isdigit() else None
        live, liveness_source, observed = _parent_liveness_evidence(jobs, metadata)
        if not live:
            raise DispatchContractError("parent-attempt-not-live", attempt_id)
        return ParentAttemptBinding(
            attempt_id=attempt_id,
            pid=pid,
            pid_start=pid_start,
            pid_scope=metadata.get("pid_scope", "host-visible"),
            pid_host=host_pid,
            pid_host_start=host_start,
            observed_pid=observed.pid if observed is not None else None,
            observed_pid_start=observed.expected_start if observed is not None else "",
            liveness_source=liveness_source,
            harness=metadata.get("harness", ""),
            transport=metadata.get("transport", ""),
            runtime_sandbox=metadata.get("runtime_sandbox", ""),
            repository_identity=requested_repository,
            worktree=parent_fields[3],
            slug=parent_fields[4],
            liveness_metadata_fingerprint=tuple(
                (key, metadata.get(key, ""))
                for key in PARENT_LIVENESS_METADATA_KEYS
            ),
        )


def _registered_worker(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"1", "true"}:
        return True
    if str(value).lower() in {"0", "false"}:
        return False
    raise DispatchContractError("invalid-registered-worker", str(value))


def validate_attempt_metadata(
    metadata: dict[str, object],
    *,
    registered_headless_wrapper: bool = False,
) -> None:
    """Validate independent v20 attempt axes before claim, spawn, or completion."""

    try:
        schema_version = int(metadata.get("attempt_schema_version", 0))
        dispatch_depth = int(metadata.get("dispatch_depth", -1))
    except (TypeError, ValueError) as exc:
        raise DispatchContractError("invalid-attempt-metadata", str(exc)) from exc
    if schema_version != ATTEMPT_SCHEMA_VERSION:
        raise DispatchContractError(
            "legacy-attempt-row-read-only",
            f"attempt schema v{schema_version or 1} cannot be claimed or completed",
        )
    if any(key in metadata for key in ("depth", "owner_depth", "max_depth")):
        raise DispatchContractError(
            "bare-dispatch-depth-field",
            "current attempt metadata accepts dispatch_depth only",
        )
    if dispatch_depth not in {0, 1, 2}:
        raise DispatchContractError("invalid-dispatch-depth", str(dispatch_depth))

    transport = str(metadata.get("transport", ""))
    surface = str(metadata.get("execution_surface", ""))
    fallback_hop = str(metadata.get("fallback_hop", ""))
    registered = _registered_worker(metadata.get("registered_worker"))
    if transport not in WRAPPER_TRANSPORTS:
        raise DispatchContractError("invalid-transport", transport)
    if surface not in EXECUTION_SURFACES:
        raise DispatchContractError("invalid-execution-surface", surface)
    if fallback_hop not in FALLBACK_HOPS and not (
        dispatch_depth == 0 and fallback_hop == ""
    ):
        raise DispatchContractError("invalid-fallback-hop", fallback_hop)
    if dispatch_depth == 0 and (
        surface != "inline"
        or registered
        or transport != "interactive"
        or fallback_hop
    ):
        raise DispatchContractError("direct-attempt-axes-mismatch", surface)
    if surface == "claude-agent-team-teammate":
        raise DispatchContractError(
            "teammate-not-dispatch-attempt",
            "Claude agent-team teammates carry peer-session lifecycle, not dispatch depth",
        )
    if registered != (surface == "registered-headless"):
        raise DispatchContractError("attempt-registration-surface-mismatch", surface)
    if registered and transport != "headless":
        raise DispatchContractError("registered-worker-transport-mismatch", transport)
    if surface == "registered-headless" and fallback_hop not in {
        "same-harness-headless",
        "cross-harness-headless",
    }:
        raise DispatchContractError("registered-worker-fallback-mismatch", fallback_hop)
    native_surfaces = {"codex-native-subagent", "claude-subagent"}
    if surface in native_surfaces and (
        fallback_hop != "native-subagent" or transport != "headless"
    ):
        raise DispatchContractError(
            "native-surface-axes-mismatch",
            f"transport={transport},fallback_hop={fallback_hop}",
        )
    if surface == "inline" and dispatch_depth > 0 and fallback_hop != "inline":
        raise DispatchContractError("inline-surface-fallback-mismatch", fallback_hop)
    if registered_headless_wrapper and (surface != "registered-headless" or not registered):
        raise DispatchContractError("headless-wrapper-surface-mismatch", surface)

    # A route stage owns one semantic gate; optional sub-sessions are only
    # execution-capacity attempts below that stage. Legacy/current ordinary
    # attempts omit these fields and remain stage-authoritative.
    subsession_id = str(metadata.get("subsession_id", ""))
    raw_authority = metadata.get("stage_authority", "1")
    stage_authority = _registered_worker(raw_authority)
    if subsession_id:
        if not SUBSESSION_ID_RE.fullmatch(subsession_id):
            raise DispatchContractError("subsession-id-invalid", subsession_id)
        if stage_authority:
            raise DispatchContractError("subsession-stage-authority-forbidden", subsession_id)
        if dispatch_depth != 2:
            raise DispatchContractError("subsession-depth-invalid", str(dispatch_depth))
        required = (
            "route_id", "route_node", "session_chain_id", "subsession_index",
            "subsession_count", "subsession_mode", "subsession_purpose",
            "phase_brief", "phase_brief_sha256", "state_ledger", "fixed_files_sha256",
            "narrow_verify_sha256", "expected_round_trips",
        )
        missing = [key for key in required if not str(metadata.get(key, ""))]
        if missing:
            raise DispatchContractError("subsession-metadata-missing", ",".join(missing))
        chain_id = str(metadata["session_chain_id"])
        if not SESSION_CHAIN_ID_RE.fullmatch(chain_id):
            raise DispatchContractError("session-chain-id-invalid", chain_id)
        mode = str(metadata["subsession_mode"])
        if mode not in {"serial", "parallel"}:
            raise DispatchContractError("subsession-mode-invalid", mode)
        purpose = str(metadata["subsession_purpose"])
        if purpose not in {"planned", "gap-retry"}:
            raise DispatchContractError("subsession-purpose-invalid", purpose)
        try:
            index = int(str(metadata["subsession_index"]))
            count = int(str(metadata["subsession_count"]))
            rounds = int(str(metadata["expected_round_trips"]))
        except ValueError as exc:
            raise DispatchContractError("subsession-number-invalid", str(exc)) from exc
        if not 1 <= index <= count <= 16 or not 1 <= rounds <= 20:
            raise DispatchContractError(
                "subsession-number-out-of-range", f"index={index},count={count},rounds={rounds}"
            )
        if purpose == "planned" and str(metadata.get("capacity_retry", "0")) == "1":
            raise DispatchContractError(
                "planned-subsession-retry-conflation", subsession_id
            )
        for key in (
            "phase_brief_sha256", "fixed_files_sha256", "narrow_verify_sha256"
        ):
            value = str(metadata[key])
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise DispatchContractError("subsession-digest-invalid", f"{key}={value}")
        for key in ("phase_brief", "state_ledger"):
            if not Path(str(metadata[key])).is_absolute():
                raise DispatchContractError(
                    "subsession-path-not-absolute", f"{key}={metadata[key]}"
                )
        if mode == "parallel" and not (
            metadata.get("parallel_group") or metadata.get("batch_group")
        ):
            raise DispatchContractError("parallel-subsession-batch-required", subsession_id)
    elif not stage_authority:
        raise DispatchContractError(
            "stage-authority-zero-without-subsession", str(metadata.get("route_node", ""))
        )


def headless_attempt_policy(
    *,
    route_file: str | None,
    route_node: str | None,
    intensity: str,
    harness: str,
    dispatch_depth: int,
    parent_slug: str | None,
    execution_surface: str,
    registered_worker: bool,
    fallback_hop: str | None,
    fallback_ordinal: int,
    parent_harness: str,
    parent_transport: str,
    parent_sandbox: str,
    launch_authority: str,
) -> dict[str, object]:
    """Bind one registered wrapper invocation to its immutable route axes."""

    effective_hop = fallback_hop or {
        1: "same-harness-headless",
        2: "cross-harness-headless",
    }.get(fallback_ordinal, "same-harness-headless")
    metadata: dict[str, object] = {
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "dispatch_depth": dispatch_depth,
        "transport": "headless",
        "execution_surface": execution_surface,
        "registered_worker": registered_worker,
        "fallback_hop": effective_hop,
    }
    validate_attempt_metadata(metadata, registered_headless_wrapper=True)
    policy: dict[str, object] = {
        "fallback_hop": effective_hop,
        "fallback_ordinal": fallback_ordinal,
        "quick": False,
        "terminal_attempt_limit": None,
        "replacement_attempt_limit": 0,
        "replacement_notes": frozenset(),
    }

    if not route_file:
        if intensity == "direct":
            raise DispatchContractError("direct-main-inline-only", "direct routes do not register workers")
        if intensity == "quick":
            raise DispatchContractError(
                "quick-headless-unavailable",
                "quick dispatch requires a current immutable route",
            )
        return policy
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DispatchContractError("route-record-unreadable", str(exc)) from exc
    if route.get("schema_version") != 2:
        raise DispatchContractError(
            "legacy-route-read-only",
            f"route schema v{route.get('schema_version', 1)} cannot register or start workers",
        )
    node = next((row for row in route.get("nodes", []) if row.get("id") == route_node), None)
    if node is None:
        raise DispatchContractError("route-node-unknown", str(route_node))
    if route.get("effective_intensity") == "direct":
        raise DispatchContractError("direct-main-inline-only", "direct routes do not register workers")
    if int(node.get("dispatch_depth", -1)) != dispatch_depth:
        raise DispatchContractError("route-dispatch-depth-mismatch", str(node.get("dispatch_depth")))

    if route.get("effective_intensity") == "quick":
        if dispatch_depth != 1 or parent_slug or route_node != "one-shot":
            raise DispatchContractError("quick-route-shape-invalid", str(route_node))
        if node.get("execution_surface") != "registered-headless" or node.get("registered_worker") is not True:
            raise DispatchContractError("quick-route-surface-invalid", str(node.get("execution_surface")))
        if effective_hop != "same-harness-headless":
            raise DispatchContractError("quick-fallback-forbidden", effective_hop)
        candidates = [
            row
            for row in route.get("registered_headless_candidates") or []
            if row.get("status") == "supported"
            and row.get("harness") == harness
            and row.get("transport") == "headless"
            and row.get("surface") == "registered-headless"
        ]
        if not candidates:
            raise DispatchContractError("quick-headless-unavailable", harness)
        policy.update(
            quick=True,
            terminal_attempt_limit=len(candidates),
            replacement_attempt_limit=1,
            replacement_notes=frozenset({"dead-protocol", "dead-permission-reject"}),
        )
        return policy

    chain = node.get("fallback_hops")
    if not isinstance(chain, list):
        raise DispatchContractError("route-fallback-hops-missing", str(route_node))
    expected_candidate = {
        "parent_harness": parent_harness,
        "parent_transport": parent_transport,
        "parent_sandbox": parent_sandbox,
        "child_harness": harness,
        "launch_authority": launch_authority,
        "status": "supported",
    }

    def candidate_matches(candidate: object) -> bool:
        return isinstance(candidate, dict) and all(
            candidate.get(key) == value for key, value in expected_candidate.items()
        )

    selected = None
    if fallback_ordinal == 0:
        selected = next(
            (
                row
                for row in chain
                if any(candidate_matches(candidate) for candidate in row.get("candidates", []))
            ),
            None,
        )
        if selected is not None:
            fallback_ordinal = int(selected["ordinal"])
            effective_hop = str(selected["fallback_hop"])
            policy.update(fallback_ordinal=fallback_ordinal, fallback_hop=effective_hop)
    else:
        selected = next(
            (row for row in chain if int(row.get("ordinal", 0)) == fallback_ordinal),
            None,
        )
    if selected is None or selected.get("fallback_hop") != effective_hop:
        raise DispatchContractError("route-fallback-hop-mismatch", effective_hop)
    if not any(candidate_matches(candidate) for candidate in selected.get("candidates", [])):
        raise DispatchContractError(
            "route-fallback-candidate-mismatch",
            json.dumps(expected_candidate, sort_keys=True),
        )
    if effective_hop not in {"same-harness-headless", "cross-harness-headless"}:
        raise DispatchContractError("headless-wrapper-fallback-mismatch", effective_hop)
    return policy


def _absolute(path: str | Path, field: str) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        raise DispatchContractError(f"{field}-must-be-absolute", str(value))
    return value.resolve(strict=False)


def _versioned_source_layout(path: str | Path) -> tuple[str, Path | None]:
    """Classify installed immutable source trees without trusting symlink spelling.

    A Codex activation has an unambiguous mutable runtime home immediately before
    ``.harness/bundles/<id>/source``.  A shared Hearting release does not encode
    which runtime owns the launch, so selecting a registry from it must fail
    closed instead of guessing.
    """

    candidate = Path(path).expanduser().resolve(strict=False)
    parts = candidate.parts
    for index, part in enumerate(parts):
        if (
            part == ".harness"
            and index + 3 < len(parts)
            and parts[index + 1] == "bundles"
            and parts[index + 3] == "source"
        ):
            return "bundle", Path(*parts[:index])
        if (
            part == "hearting"
            and index + 2 < len(parts)
            and parts[index + 1] == "releases"
        ):
            return "shared-release", None
    return "mutable-or-checkout", None


def _validated_registry_path(path: str | Path, field: str) -> Path:
    candidate = _absolute(path, field)
    layout, _runtime_home = _versioned_source_layout(candidate)
    if layout == "bundle":
        raise DispatchContractError(
            "versioned-source-registry-fallback",
            f"{field}={candidate}; set AGENT_DISPATCH_JOBS to activation-owned mutable state",
        )
    return candidate


def _fallback_registry(agent_home: Path) -> Path:
    home = Path(agent_home).expanduser()
    resolved_home = home.resolve(strict=False)
    layout, runtime_home = _versioned_source_layout(resolved_home)
    if layout == "bundle":
        assert runtime_home is not None
        return (runtime_home / ".harness" / "dispatch" / "jobs.log").resolve(
            strict=False
        )
    if layout == "shared-release":
        raise DispatchContractError(
            "versioned-source-registry-fallback",
            f"agent_home={resolved_home}; set AGENT_DISPATCH_JOBS explicitly",
        )
    return home / ".dispatch" / "jobs.log"


def resolve_global_registry(
    agent_home: Path,
    explicit_jobs: str | None,
    dispatch_depth: int,
    action: str,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> RegistrySelection:
    """Resolve the one authoritative registry and reject nested overrides.

    Dispatch-depth-0/root dispatch may select an explicit registry once. The wrapper then
    exports it through AGENT_DISPATCH_JOBS. A real nested start must inherit that
    path; argv may repeat it, but cannot replace it.
    """

    env = os.environ if environ is None else environ
    inherited_raw = env.get("AGENT_DISPATCH_JOBS")
    explicit = _validated_registry_path(explicit_jobs, "jobs") if explicit_jobs else None
    inherited = (
        _validated_registry_path(inherited_raw, "agent-dispatch-jobs")
        if inherited_raw
        else None
    )

    managed_parent = (
        env.get("AGENT_CODEX_MANAGED_GATEWAY") == "1"
        and env.get("AGENT_CODEX_MANAGED_PARENT_RUNTIME") == "codex"
    )
    if managed_parent and inherited and explicit and inherited != explicit:
        raise DispatchContractError(
            "managed-parent-registry-immutable",
            f"explicit={explicit} inherited={inherited}",
        )

    if dispatch_depth > 1 and inherited and explicit and inherited != explicit:
        raise DispatchContractError(
            "noncanonical-nested-jobs",
            f"explicit={explicit} inherited={inherited}",
        )

    nested_start = dispatch_depth > 1 and action == "start"
    if nested_start and inherited is None:
        raise DispatchContractError(
            "global-registry-unset",
            "nested --start requires inherited AGENT_DISPATCH_JOBS",
        )

    # An ordinary dispatch-depth-1 invocation is the root dispatch boundary and may
    # choose a new registry over unrelated ambient shell state.  A managed interactive
    # parent is different: its launcher enrolled one canonical registry, so the check
    # above makes that inherited path immutable for the entire session.
    if dispatch_depth <= 1 and explicit:
        return RegistrySelection(explicit, "root-explicit", False)
    if inherited:
        return RegistrySelection(inherited, "inherited-env", True)
    if explicit:
        return RegistrySelection(explicit, "root-explicit", False)
    fallback = _fallback_registry(agent_home).resolve(strict=False)
    source = "activation-runtime" if fallback.parent.name == "dispatch" else "agent-home"
    return RegistrySelection(fallback, source, False)


def dispatch_state_root(jobs: str | Path) -> Path:
    """The one derivation: dispatch state lives beside its canonical registry."""

    return Path(jobs).expanduser().resolve(strict=False).parent


def validate_dispatch_log_dir(
    jobs: str | Path, log_dir: str | Path | None
) -> Path:
    """Resolve a launch log directory inside the registry-owned state root."""

    state_root = dispatch_state_root(jobs)
    candidate = (
        state_root / "logs"
        if log_dir is None
        else Path(log_dir).expanduser().resolve(strict=False)
    )
    try:
        candidate.relative_to(state_root)
    except ValueError as exc:
        raise DispatchContractError(
            "log-dir-outside-dispatch-state-root", str(candidate)
        ) from exc
    if candidate == state_root or candidate.is_symlink() or candidate.parent.is_symlink():
        raise DispatchContractError(
            "log-dir-outside-dispatch-state-root", str(candidate)
        )
    return candidate


def resolve_dispatch_state_root(
    agent_home: Path,
    explicit_jobs: str | Path | None = None,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """Resolve the one canonical dispatch state root.

    Chain: ① `explicit_jobs` (a `RegistrySelection.path` the caller already
    holds) -> ② inherited `AGENT_DISPATCH_JOBS` -> ③ a checked fallback.
    Maintainer checkouts retain `agent_home/.dispatch`; Codex bundle sources
    derive activation-owned mutable state, and ambiguous shared releases fail
    closed. No new env
    var -- the only override surface remains `AGENT_DISPATCH_JOBS`, so marker
    root and registry root cannot structurally diverge. A caller that already
    has a `RegistrySelection` must pass `explicit_jobs=selection.path` so
    marker root and registry root are pinned to the same value at the call
    site, not re-derived independently.
    """

    if explicit_jobs is not None:
        return dispatch_state_root(_validated_registry_path(explicit_jobs, "jobs"))
    env = os.environ if environ is None else environ
    inherited = env.get("AGENT_DISPATCH_JOBS")
    if inherited:
        return dispatch_state_root(
            _validated_registry_path(inherited, "agent-dispatch-jobs")
        )
    return _fallback_registry(agent_home).parent


def dispatch_state_roots(
    agent_home: Path, jobs: str | Path | None = None
) -> tuple[Path, ...]:
    """Read order for dispatch state: canonical state root first, legacy
    agent-home-relative tree second. Deduplicated. The writer uses only
    `dispatch_state_roots(...)[0]`; only readers should iterate the tuple.
    """

    canonical = resolve_dispatch_state_root(agent_home, explicit_jobs=jobs)
    legacy = Path(agent_home) / ".dispatch"
    if canonical == legacy:
        return (canonical,)
    return (canonical, legacy)


def ensure_global_registry_writable(path: Path) -> None:
    """Open the global registry and its lock before any child spawn."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{path}.lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with path.open("a", encoding="utf-8") as registry:
                registry.flush()
                os.fsync(registry.fileno())
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise DispatchContractError("global-registry-unwritable", f"{path}: {exc}") from exc


def ensure_launch_broker(
    agent_home: Path,
    jobs: Path,
    *,
    dispatch_depth: int,
    action: str,
    intensity: str,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> BrokerSelection | None:
    """Reject production launch-broker creation after dispatch contract v3.

    The callable remains for one compatibility release so an overlooked caller
    fails closed with a stable reason instead of silently resurrecting the
    resident broker. Diagnostic ``status``/``stop`` remain in dispatch-broker.py.
    """

    if (
        action != "start"
        or dispatch_depth != 1
        or intensity not in {"standard", "strong", "thorough", "adversarial"}
    ):
        return None
    raise DispatchContractError(
        "launch-broker-retired",
        "dispatch contract v3 launches checked headless adapters directly from the conductor",
    )


def validate_nested_eligibility(
    *,
    dispatch_depth: int,
    action: str,
    parent_harness: str,
    parent_transport: str,
    parent_sandbox: str,
    child_harness: str,
    launch_authority: str,
    status: str,
    source: str,
) -> None:
    if dispatch_depth < 2:
        return
    if launch_authority not in LAUNCH_AUTHORITIES:
        raise DispatchContractError("invalid-launch-authority", launch_authority)
    if status not in ELIGIBILITY:
        raise DispatchContractError("invalid-nested-eligibility", status)
    if parent_transport not in CANONICAL_PARENT_TRANSPORTS and parent_transport != "unknown":
        raise DispatchContractError(
            "invalid-parent-transport",
            f"{parent_transport}; expected one of {sorted(CANONICAL_PARENT_TRANSPORTS)}",
        )
    # Canonical vocabulary is not enough: this call site is already inside a
    # dispatch-depth-2 launch, whose parent is by construction the depth-1
    # registered-headless owner. `interactive` is a well-formed word for the
    # depth-0 session and a launch-time contradiction here.
    expected_parent_transport = PARENT_TRANSPORT_BY_DISPATCH_DEPTH[1]
    if parent_transport not in (expected_parent_transport, "unknown"):
        raise DispatchContractError(
            "parent-transport-not-registered-headless",
            f"dispatch_depth={dispatch_depth} sealed parent_transport={parent_transport};"
            f" a dispatch-depth-2 parent is the {expected_parent_transport} depth-1 owner",
        )
    missing = [
        name
        for name, value in (
            ("parent_harness", parent_harness),
            ("parent_transport", parent_transport),
            ("parent_sandbox", parent_sandbox),
            ("child_harness", child_harness),
            ("eligibility_source", source),
        )
        if not value or value == "unknown"
    ]
    if action == "start" and missing:
        raise DispatchContractError("nested-eligibility-evidence-missing", ",".join(missing))
    if action == "start" and status != "supported":
        raise DispatchContractError(f"nested-child-spawn-{status}", source or "no checked evidence")


def completion_marker_gate(
    route_file: str | None,
    route_node: str | None,
    action: str,
    agent_home: Path,
    jobs: Path | None = None,
    *,
    registry_lines: list[str] | None = None,
    attempt_id: str | None = None,
) -> None:
    """SD-56 decision gate: a record-bound ``--start`` must not spawn a node
    whose ``depends_on`` predecessors have no completion marker, nor one whose
    own previous attempt has not actually stopped.

    ``agent_home`` is an explicit argument, not re-read from the environment,
    so the writer (capability-route.py complete) and every reader (this gate,
    called once per wrapper) are structurally forced to agree on one root.

    ``attempt_id`` is the identity about to launch. It is what makes "sibling"
    mean something: without it the caller's own freshly claimed row is the most
    recent row for this node and would block every launch.
    """

    if not route_file:
        return
    route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    contract_version = route.get("dispatch_contract_version") or route.get("broker_contract_version")
    contract_version = contract_version or 1
    if action in {"register", "start"} and contract_version != 3:
        raise DispatchContractError(
            "legacy-broker-route-read-only",
            f"dispatch contract v{contract_version} cannot register or start workers",
        )
    if action != "start" or contract_version != 3:
        return
    node = next((row for row in route.get("nodes", []) if row.get("id") == route_node), None)
    if node is None:
        return
    missing = []
    blocked: list[tuple[str, AttemptReadiness]] = []
    for dep in node.get("depends_on", []):
        marker_path = next(
            (
                candidate
                for candidate in (
                    root / "completion" / route["route_id"] / f"{dep}.json"
                    for root in dispatch_state_roots(agent_home, jobs)
                )
                if candidate.is_file()
            ),
            None,
        )
        if marker_path is None:
            missing.append(dep)
            continue
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append(dep)
            continue
        dep_node = next((row for row in route.get("nodes", []) if row.get("id") == dep), None)
        if dep_node is None or not completion_marker_is_current(route, dep_node, marker_path, marker):
            missing.append(dep)
            continue
        readiness = completion_attempt_readiness(
            route,
            dep_node,
            marker,
            jobs or (resolve_dispatch_state_root(agent_home) / "jobs.log"),
            registry_lines=registry_lines,
        )
        if readiness.state != "ready":
            blocked.append((dep, readiness))
    if missing:
        raise DispatchContractError("completion-marker-missing", ",".join(missing))
    if blocked:
        reason = (
            "predecessor-process-draining"
            if any(item.state == "draining" for _, item in blocked)
            else "predecessor-process-unverifiable"
        )
        detail = ",".join(
            f"{dep}:{item.attempt_id or '-'}:{item.reason}" for dep, item in blocked
        )
        raise DispatchContractError(reason, detail)
    _sibling_attempt_gate(
        route,
        route_node,
        jobs or (resolve_dispatch_state_root(agent_home) / "jobs.log"),
        registry_lines=registry_lines,
        attempt_id=attempt_id,
    )


def _sibling_attempt_gate(
    route: dict[str, object],
    route_node: str | None,
    jobs: Path,
    *,
    registry_lines: list[str] | None = None,
    attempt_id: str | None = None,
) -> None:
    """SD-79: refuse to launch over a previous attempt of *this* node that still runs.

    The ``depends_on`` loop above cannot cover this. A retry, a fallback hop, and
    a capacity re-selection are all further attempts at the *same* node, so they
    never appear in any node's ``depends_on`` list and that loop structurally
    never fires for them. This is also not
    ``completion_attempt_readiness``'s ``conflicting_active`` scan: that one asks
    whether a *registry status word* says another attempt is still open, while
    this one asks the operating system whether the previous attempt's processes
    are still alive. A row closed by a false death verdict looks quiet to the
    first check and loud to this one -- which is the whole failure this repairs.
    Do not merge them.
    """

    if registry_lines is None:
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
    else:
        lines = registry_lines
    sibling: tuple[str, dict[str, str]] | None = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if (
            metadata.get("route_id") != route.get("route_id")
            or metadata.get("route_node") != route_node
        ):
            continue
        candidate = metadata.get("attempt_id", "")
        if not candidate or candidate == (attempt_id or ""):
            continue
        # A row that never recorded a governed process cannot have leaked one,
        # and judging it `unverifiable` would wedge the node permanently.
        if not metadata.get("pid"):
            continue
        # Only the most recent sibling by registry order is authoritative; older
        # rows are its lineage, not independent claimants.
        sibling = (fields[1], metadata)
    if sibling is None:
        return
    sibling_status, sibling_metadata = sibling
    process = attempt_process_quiescence(
        sibling_metadata,
        terminal_receipt=sibling_status in {"done", "killed", "cancelled"},
    )
    if process.state == "quiescent":
        return
    reason = (
        "prior-attempt-still-live"
        if process.state == "live"
        else "prior-attempt-unverifiable"
    )
    raise DispatchContractError(
        reason,
        f"{route_node}:{sibling_metadata.get('attempt_id', '-')}:{process.reason}",
    )


def completion_marker_is_current(
    route: dict[str, object],
    node: dict[str, object],
    marker_path: Path,
    marker: dict[str, object] | None = None,
) -> bool:
    """Prove one schema-v2 marker and its immutable history/attempt linkage."""

    try:
        marker = marker or json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("schema_version") != 2:
            return False
        node_id = str(node["id"])
        sequence = int(marker.get("sequence", 0))
        if sequence < 1:
            return False
        expected = {
            "route_id": route.get("route_id"),
            "route_hash": route.get("route_hash"),
            "registry_digest": route.get("registry_digest"),
            "node_id": node_id,
            "completion_gate": node.get("completion_gate"),
        }
        if any(marker.get(key) != value for key, value in expected.items()):
            return False
        evidence_record = marker.get("evidence")
        if not isinstance(evidence_record, dict):
            return False
        evidence = Path(str(evidence_record.get("path", "")))
        if not evidence.is_absolute() or not evidence.is_file():
            return False
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_record.get("sha256"):
            return False
        history_path = marker_path.parent / f"{node_id}.{sequence}.json"
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if history != marker:
            return False

        if node.get("kind") == "resource-runner":
            return (
                marker.get("attempt_id") is None
                and marker.get("dispatch_depth") is None
                and marker.get("transport") is None
                and marker.get("execution_surface") is None
                and marker.get("registered_worker") is False
                and marker.get("fallback_hop") is None
            )

        if marker.get("stage_authority") == "owner-chain":
            manifest_path = Path(str(marker.get("subsession_manifest", "")))
            if (
                not manifest_path.is_absolute()
                or not manifest_path.is_file()
                or hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                != marker.get("subsession_manifest_sha256")
            ):
                return False
            manifest = load_manifest(manifest_path, node=node)
            return (
                manifest.get("route_id") == route.get("route_id")
                and manifest.get("route_hash") == route.get("route_hash")
                and manifest.get("chain_id") == marker.get("session_chain_id")
                and marker.get("attempt_id")
                == "att-stage-" + str(marker.get("subsession_manifest_sha256"))[:32]
                and marker.get("dispatch_depth") == node.get("dispatch_depth")
                and marker.get("transport") == "headless"
                and marker.get("execution_surface") == "inline"
                and marker.get("registered_worker") is False
                and marker.get("fallback_hop") == "inline"
            )

        attempt_id = marker.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return False
        axes = {
            "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
            "dispatch_depth": marker.get("dispatch_depth"),
            "transport": marker.get("transport"),
            "execution_surface": marker.get("execution_surface"),
            "registered_worker": marker.get("registered_worker"),
            "fallback_hop": marker.get("fallback_hop") or "",
        }
        validate_attempt_metadata(axes)
        if int(axes["dispatch_depth"]) != node.get("dispatch_depth"):
            return False
        safe_attempt = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in attempt_id
        )
        link_path = marker_path.parent / f"{node_id}.{safe_attempt}.attempt.json"
        link = json.loads(link_path.read_text(encoding="utf-8"))
        link_expected = {
            "schema_version": 2,
            "route_id": route.get("route_id"),
            "node_id": node_id,
            "attempt_id": attempt_id,
            "dispatch_depth": marker.get("dispatch_depth"),
            "transport": marker.get("transport"),
            "execution_surface": marker.get("execution_surface"),
            "registered_worker": marker.get("registered_worker"),
            "fallback_hop": marker.get("fallback_hop"),
            "evidence_sha256": evidence_record.get("sha256"),
        }
        if not all(link.get(key) == value for key, value in link_expected.items()):
            return False
        # The link records its own absolute location as written by the marker
        # writer, whose env may spell the same directory in pointer form while
        # this reader resolved it (or vice versa). Identity, not spelling, is
        # the contract, so compare through agent_home_equivalent -- the
        # comparison-site normalizer this module defines for exactly this.
        for key, expected_path in (
            ("completion_marker", marker_path),
            ("completion_marker_history", history_path),
        ):
            recorded = link.get(key)
            if not isinstance(recorded, str) or not agent_home_equivalent(
                recorded, expected_path
            ):
                return False
        return True
    except (DispatchContractError, KeyError, OSError, TypeError, ValueError):
        return False


def completion_attempt_readiness(
    route: dict[str, object],
    node: dict[str, object],
    marker: dict[str, object],
    jobs: Path,
    *,
    registry_lines: list[str] | None = None,
) -> AttemptReadiness:
    """Combine a current semantic marker with its exact governed process state."""

    if marker.get("stage_authority") == "owner-chain":
        return AttemptReadiness("ready", "subsession-chain-quiescence-verified-at-stage-gate")
    if node.get("kind") == "resource-runner" or marker.get("registered_worker") is False:
        return AttemptReadiness("ready", "semantic-terminal-no-registered-process")
    attempt_id = marker.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        return AttemptReadiness("unverifiable", "marker-attempt-id-missing")
    if registry_lines is None:
        try:
            lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return AttemptReadiness("unverifiable", "registry-unreadable", attempt_id)
    else:
        lines = registry_lines

    exact: list[tuple[list[str], dict[str, str]]] = []
    conflicting_active: list[str] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if (
            metadata.get("route_id") != route.get("route_id")
            or metadata.get("route_node") != node.get("id")
        ):
            continue
        if metadata.get("attempt_id") == attempt_id:
            exact.append((fields, metadata))
        elif fields[1] in {"open", "running"} and metadata.get("attempt_id"):
            conflicting_active.append(metadata["attempt_id"])
    if len(exact) != 1:
        return AttemptReadiness(
            "unverifiable", f"marker-attempt-row-count-{len(exact)}", attempt_id
        )
    fields, metadata = exact[0]
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError as exc:
        return AttemptReadiness("unverifiable", exc.reason, attempt_id)
    if fields[1] != "done" or metadata.get("note") != "completed-marker":
        return AttemptReadiness("unverifiable", "marker-attempt-not-terminal", attempt_id)
    if conflicting_active:
        return AttemptReadiness("draining", "conflicting-active-retry", attempt_id)
    process = attempt_process_quiescence(metadata, terminal_receipt=True)
    if process.state == "quiescent":
        return AttemptReadiness("ready", process.reason, attempt_id)
    if process.state == "live":
        return AttemptReadiness("draining", process.reason, attempt_id)
    return AttemptReadiness("unverifiable", process.reason, attempt_id)


def new_attempt_id(value: str | None = None) -> str:
    if value:
        if not value.startswith("att-") or len(value) < 12:
            raise DispatchContractError("invalid-attempt-id", value)
        return value
    return "att-" + uuid.uuid4().hex


def row_has_attempt(pipe: str, attempt_id: str) -> bool:
    metadata = parse_registry_metadata(pipe)
    return metadata.get("attempt_id") == attempt_id


def _immutable_attempt_identity(fields: list[str]) -> tuple[object, ...]:
    if len(fields) != 6:
        raise DispatchContractError("invalid-registry-row", "expected six tab-separated fields")
    metadata = parse_registry_metadata(fields[5])
    validate_attempt_metadata(metadata)
    immutable_metadata = tuple(
        sorted(
            (key, value)
            for key, value in metadata.items()
            if key not in ATTEMPT_MUTABLE_METADATA
        )
    )
    return fields[2], fields[3], fields[4], immutable_metadata


def _atomic_registry_replace(jobs: Path, lines: list[str]) -> None:
    """Replace the registry after fsync without exposing a truncated file."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{jobs.name}.claim-", dir=str(jobs.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as registry:
            registry.write("\n".join(lines) + "\n")
            registry.flush()
            os.fsync(registry.fileno())
        os.replace(tmp_name, jobs)
        dir_fd = os.open(str(jobs.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def attempt_launch_is_available(jobs: Path, attempt_id: str) -> bool:
    """Return true only for one exact current open registered-only row."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        matches = []
        for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((fields, metadata))
        if len(matches) != 1:
            return False
        fields, metadata = matches[0]
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            return False
        return fields[1] == "open" and metadata.get("launch_claimed") == "0"


def mark_attempt_launch_started(jobs: Path, attempt_id: str, pid: int) -> None:
    """Let the exact launch fence durably attest before it executes payload."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            raise DispatchContractError(
                "attempt-row-not-unique", f"attempt_id={attempt_id} rows={len(matches)}"
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)
        expected_start = metadata.get("pid_start", "")
        if (
            fields[1] not in {"open", "running"}
            or metadata.get("launch_claimed") != "1"
            or metadata.get("launch_fence") != "registry-v1"
            or metadata.get("pid") != str(pid)
            or metadata.get("pgid") != str(pid)
            or not expected_start
            or not process_identity_is_live(pid, expected_start)
            or exact_process_group_signal_authority(pid, expected_start)
            != "authoritative"
        ):
            raise DispatchContractError(
                "attempt-launch-fence-identity-mismatch", attempt_id
            )
        fields[5] = _updated_attempt_metadata(
            fields[5], {"launch_started": "1"}
        )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)


def recover_unstarted_attempt(jobs: Path, attempt_id: str) -> bool:
    """Reset only a dead registry-v1 fence that never authorized payload exec."""

    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            return False
        index, fields, metadata = matches[0]
        try:
            validate_attempt_metadata(metadata)
        except DispatchContractError:
            return False
        if (
            fields[1] != "open"
            or metadata.get("launch_claimed") != "1"
            or metadata.get("launch_fence") != "registry-v1"
            or metadata.get("launch_started") == "1"
            or metadata.get("launch_outcome")
        ):
            return False
        process = attempt_process_quiescence(metadata)
        if process.state != "quiescent":
            return False
        remove = {
            *_PROCESS_IDENTITY_METADATA_KEYS,
            "launch_claimed",
            "launch_lifecycle",
            "launch_started",
        }
        parts = [
            part for part in fields[5].split(",")
            if part.split("=", 1)[0] not in remove
        ]
        parts.append("launch_claimed=0")
        fields[5] = ",".join(parts)
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return True


def claim_attempt_row(
    jobs: Path,
    attempt_id: str,
    row: str,
    *,
    launch: bool = False,
    exclusive_metadata: dict[str, str] | None = None,
    exclusive_live_metadata: dict[str, str] | None = None,
    terminal_attempt_limit: int | None = None,
    replacement_attempt_limit: int = 0,
    replacement_notes: frozenset[str] = frozenset(),
    preclaim: Callable[[list[str]], None] | None = None,
) -> bool:
    """Atomically register ``attempt_id`` and claim its launch at most once.

    A prior ``--register`` row may transition from ``launch_claimed=0`` to 1 on
    the first ``--start``. Concurrent starts serialize on the same lock; callers
    must not spawn a child when this returns ``False``.
    """

    if not attempt_id:
        raise DispatchContractError("attempt-id-required", "registered dispatches require an attempt id")
    row_fields = row.rstrip("\n").split("\t")
    if len(row_fields) != 6:
        raise DispatchContractError("invalid-registry-row", "expected six tab-separated fields")
    row_metadata = parse_registry_metadata(row_fields[5])
    validate_attempt_metadata(row_metadata)
    if row_metadata.get("attempt_id") != attempt_id:
        raise DispatchContractError("attempt-row-identity-mismatch", attempt_id)
    ensure_global_registry_writable(jobs)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, existing in enumerate(lines):
            fields = existing.split("\t")
            if len(fields) == 6 and row_has_attempt(fields[5], attempt_id):
                metadata = parse_registry_metadata(fields[5])
                validate_attempt_metadata(metadata)
                if _immutable_attempt_identity(fields) != _immutable_attempt_identity(row_fields):
                    raise DispatchContractError(
                        "attempt-identity-conflict",
                        f"attempt_id={attempt_id}",
                    )
                if not launch or metadata.get("launch_claimed") == "1" or fields[1] != "open":
                    return False
                if preclaim is not None:
                    preclaim(lines)
                pipe = ",".join(part for part in fields[5].split(",") if not part.startswith("launch_claimed="))
                fields[5] = pipe + ",launch_claimed=1"
                lines[index] = "\t".join(fields)
                _atomic_registry_replace(jobs, lines)
                return True
        if exclusive_metadata:
            for existing in lines:
                fields = existing.split("\t")
                if len(fields) != 6:
                    continue
                metadata = parse_registry_metadata(fields[5])
                if all(metadata.get(key) == value for key, value in exclusive_metadata.items()):
                    return False
        if exclusive_live_metadata:
            matching_terminal_attempts = set()
            replacement_attempts = set()
            for existing in lines:
                fields = existing.split("\t")
                if len(fields) != 6:
                    continue
                metadata = parse_registry_metadata(fields[5])
                if not all(
                    metadata.get(key) == value
                    for key, value in exclusive_live_metadata.items()
                ):
                    continue
                validate_attempt_metadata(metadata)
                if fields[1] in {"open", "running"}:
                    return False
                if fields[1] == "done" and metadata.get("attempt_id"):
                    # A failed terminal note in replacement_notes counts
                    # against the separate replacement budget instead of the
                    # ordinary terminal_attempt_limit -- a success (note
                    # absent, or a passing note like completed-marker /
                    # completed-supervisor) still counts as ordinary so a
                    # duplicate launch after success stays refused.
                    if metadata.get("note") in replacement_notes:
                        replacement_attempts.add(metadata["attempt_id"])
                    else:
                        matching_terminal_attempts.add(metadata["attempt_id"])
            if (
                terminal_attempt_limit is not None
                and len(matching_terminal_attempts) >= terminal_attempt_limit
            ):
                raise DispatchContractError(
                    "quick-registered-headless-exhausted",
                    f"terminal_attempts={len(matching_terminal_attempts)} limit={terminal_attempt_limit}",
                )
            if len(replacement_attempts) > replacement_attempt_limit:
                raise DispatchContractError(
                    "quick-replacement-attempts-exhausted",
                    f"replacement_attempts={len(replacement_attempts)} limit={replacement_attempt_limit}",
                )
        if launch and preclaim is not None:
            preclaim(lines)
        row_fields[5] += f",launch_claimed={1 if launch else 0}"
        with jobs.open("a", encoding="utf-8") as registry:
            registry.write("\t".join(row_fields) + "\n")
            registry.flush()
            os.fsync(registry.fileno())
        return True


def _row_identity(fields: list[str]) -> tuple[str, ...] | None:
    if len(fields) != 6:
        return None
    metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
    if metadata.get("attempt_id"):
        return ("attempt", metadata["attempt_id"])
    route_id = metadata.get("route_id")
    route_node = metadata.get("route_node")
    parent = metadata.get("parent")
    if route_id and route_node and parent:
        return ("legacy", route_id, route_node, parent, fields[4])
    return None


def _updated_attempt_metadata(
    pipe: str,
    values: dict[str, str],
    *,
    terminal: bool = False,
) -> str:
    """Replace only explicitly mutable keys; never append last-wins identity."""

    raw_parts = [part for part in pipe.split(",") if "=" in part]
    keys = [part.split("=", 1)[0] for part in raw_parts]
    immutable_duplicates = {
        key for key in keys
        if keys.count(key) > 1 and key not in ATTEMPT_MUTABLE_METADATA
    }
    if immutable_duplicates:
        raise DispatchContractError(
            "attempt-immutable-metadata-duplicate",
            ",".join(sorted(immutable_duplicates)),
        )
    metadata = parse_registry_metadata(pipe)
    allowed_new = ATTEMPT_TERMINAL_EVIDENCE_KEYS if terminal else set()
    replace: dict[str, str] = {}
    for key, raw_value in values.items():
        value = str(raw_value).replace(",", ";")
        if not key or "=" in key or "," in key:
            raise DispatchContractError("attempt-metadata-key-invalid", key)
        if key not in ATTEMPT_MUTABLE_METADATA and key not in allowed_new:
            if metadata.get(key) == value:
                continue
            raise DispatchContractError("attempt-immutable-metadata-mutation", key)
        if (
            key == "launch_outcome"
            and metadata.get(key)
            and metadata.get(key) != value
        ):
            raise DispatchContractError(
                "attempt-launch-outcome-conflict",
                f"existing={metadata.get(key)} requested={value}",
            )
        replace[key] = value
    retained = [
        part for part in raw_parts if part.split("=", 1)[0] not in replace
    ]
    retained.extend(f"{key}={value}" for key, value in sorted(replace.items()))
    return ",".join(retained)


def close_attempt_row(
    jobs: Path,
    attempt_id: str,
    note: str,
    *,
    evidence: dict[str, str] | None = None,
) -> bool:
    """Close one exact SD-49 attempt atomically and idempotently."""
    if not attempt_id or not note:
        raise DispatchContractError("attempt-close-invalid", "attempt_id and note are required")
    ensure_global_registry_writable(jobs)
    lock_path = Path(f"{jobs}.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            if metadata.get("teardown_claim"):
                return False
            fields[1] = "done"
            values = {"note": note}
            values.update({
                key: value for key, value in (evidence or {}).items()
                if value not in (None, "")
            })
            try:
                fields[5] = _updated_attempt_metadata(
                    fields[5], values, terminal=True
                )
            except DispatchContractError:
                return False
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def attempt_launch_state(
    jobs: Path,
    attempt_id: str,
    *,
    claimed: bool,
    action: str,
) -> str:
    """Return the typed launch receipt state for one exact attempt."""
    if action == "dry-run":
        return "preview-only"
    if claimed:
        return "claimed"
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "existing-unknown"
    states = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        if parse_registry_metadata(fields[5]).get("attempt_id") == attempt_id:
            states.append(fields[1])
    if any(state in {"open", "running"} for state in states):
        return "existing-active"
    if states:
        return "existing-completed"
    return "existing-unknown"


def reconcile_attempt_terminal(
    jobs: Path,
    attempt_id: str,
    note: str,
    *,
    evidence: dict[str, str] | None = None,
) -> str:
    """Atomically close one supervisor-owned attempt or prove it already closed.

    Unlike a best-effort close, a missing/duplicate exact row is a typed
    contract failure.  This lets supervisors avoid reporting successful
    completion while their canonical row remains open.
    """

    if not attempt_id or not note:
        raise DispatchContractError(
            "attempt-terminal-reconcile-invalid",
            "attempt_id and note are required",
        )
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        matches: list[tuple[int, list[str], dict[str, str]]] = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            raise DispatchContractError(
                "attempt-terminal-row-not-unique",
                f"attempt_id={attempt_id} rows={len(matches)}",
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)
        if fields[1] in {"done", "killed", "cancelled"}:
            incoming = evidence or {}
            prior_class = metadata.get("failure_class", "")
            next_class = str(incoming.get("failure_class", ""))
            if not prior_class or not next_class or prior_class == next_class:
                return "already-terminal"

            def authority(source: str, detected: str) -> int:
                if source == "supervisor-terminal-v1":
                    return 30
                if source == "completion-join-terminal-verdict-v1":
                    return 20
                if detected == "foreground-terminal-handoff":
                    return 10
                return 0

            prior_rank = authority(
                metadata.get("classifier_source", ""),
                metadata.get("detected_by", ""),
            )
            next_rank = authority(
                str(incoming.get("classifier_source", "")),
                str(incoming.get("detected_by", "")),
            )
            semantic = {"pass", "fail", "blocked"}
            values = {
                "terminal_conflict": "1",
                "prior_terminal_note": metadata.get("note", ""),
                "prior_classifier_source": metadata.get("classifier_source", ""),
                "prior_failure_class": prior_class,
            }
            if next_rank > prior_rank and next_class in semantic:
                values.update({"note": note, **incoming})
                fields[5] = _updated_attempt_metadata(
                    fields[5], values, terminal=True
                )
                lines[index] = "\t".join(fields)
                _atomic_registry_replace(jobs, lines)
                return "repaired-terminal"
            if next_rank == prior_rank and {prior_class, next_class} <= semantic:
                values.update(
                    {
                        "note": "dead-terminal-conflict",
                        "failure_class": "contract",
                        "conflicting_classifier_source": str(
                            incoming.get("classifier_source", "")
                        ),
                        "conflicting_failure_class": next_class,
                    }
                )
                fields[5] = _updated_attempt_metadata(
                    fields[5], values, terminal=True
                )
                lines[index] = "\t".join(fields)
                _atomic_registry_replace(jobs, lines)
                return "terminal-conflict"
            return "already-terminal"
        if fields[1] not in {"open", "running"}:
            raise DispatchContractError(
                "attempt-terminal-status-invalid", fields[1]
            )
        if metadata.get("teardown_claim"):
            raise DispatchContractError(
                "attempt-terminal-teardown-claimed", attempt_id
            )
        values = {"note": note}
        values.update(
            {
                key: value
                for key, value in (evidence or {}).items()
                if value not in (None, "")
            }
        )
        fields[1] = "done"
        fields[5] = _updated_attempt_metadata(fields[5], values, terminal=True)
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return "closed"


def launch_orphan_watch(
    jobs: Path,
    agent_home: Path,
    attempt_id: str,
    pid: int,
    pid_start: str,
) -> int:
    """Start one exact post-exit owner watcher outside the model governor.

    The watcher is deterministic infrastructure, not a model worker. It only
    waits for the recorded PID/start identity to end and then asks the shared
    registry classifier to close a true orphan; it never resumes work.
    """
    if not attempt_id or pid <= 0 or not pid_start:
        raise DispatchContractError(
            "orphan-watch-identity-invalid",
            "attempt_id, pid, and pid_start are required",
        )
    script = _MODULE_ROOT / "utilities" / "dispatch-orphan-watch.py"
    try:
        proc = subprocess.Popen(
            [
                sys.executable, str(script),
                "--jobs", str(Path(jobs).resolve()),
                "--agent-home", str(Path(agent_home).resolve()),
                "--attempt-id", attempt_id,
                "--pid", str(pid),
                "--pid-start", str(pid_start),
            ],
            cwd="/",
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DispatchContractError("orphan-watch-launch-failed", str(exc)) from exc
    return proc.pid


def launch_reap_watch(
    jobs: Path,
    attempt_id: str,
    pid: int,
    pid_start: str,
    pgid: int,
) -> int:
    """Start the exact detached-process drain observer in the launch namespace."""

    if not attempt_id or pid <= 0 or pgid != pid or not pid_start:
        raise DispatchContractError(
            "reap-watch-identity-invalid",
            "attempt_id, leader pid/start, and leader pgid are required",
        )
    script = _MODULE_ROOT / "utilities" / "dispatch-reap-watch.py"
    # The observer is governance machinery, not part of the governed attempt.
    # A direct wrapper can inherit the same attempt tag that supplied its
    # default --attempt-id; retaining it here would make the observer discover
    # itself forever and prevent the empty receipt it exists to issue.
    watcher_env = {
        key: value
        for key, value in os.environ.items()
        if key != ATTEMPT_DESCENDANT_ENV
    }
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--jobs", str(Path(jobs).resolve()),
                "--attempt-id", attempt_id,
                "--pid", str(pid),
                "--pid-start", pid_start,
                "--pgid", str(pgid),
            ],
            cwd="/",
            env=watcher_env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DispatchContractError("reap-watch-launch-failed", str(exc)) from exc
    return proc.pid


def close_attempt_row_if(
    jobs: Path,
    attempt_id: str,
    note: str,
    predicate: Callable[[list[str]], bool],
    *,
    evidence: dict[str, str] | None = None,
    teardown_claim: str | None = None,
) -> bool:
    """Revalidate and close one exact attempt inside the SD-49 lock.

    Reconciliation decisions depend on mutable process, worktree, marker and
    heartbeat evidence.  A read-then-``close_attempt_row`` sequence leaves a
    race between the decision and mutation.  This primitive re-reads the row
    and invokes the caller's safety predicate while the canonical registry is
    locked; a changed or newly-live row is therefore left untouched.
    """
    if not attempt_id or not note:
        raise DispatchContractError("attempt-close-invalid", "attempt_id and note are required")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            recorded_claim = metadata.get("teardown_claim", "")
            if recorded_claim:
                if not teardown_claim or recorded_claim != teardown_claim:
                    return False
            elif teardown_claim:
                return False
            if not predicate(fields.copy()):
                continue
            fields[1] = "done"
            values = {"note": note}
            if teardown_claim:
                values.update(
                    teardown_claim="",
                    teardown_claimed_at="",
                    teardown_claim_pid="",
                    teardown_claim_pid_start="",
                )
            values.update({
                key: value for key, value in (evidence or {}).items()
                if value not in (None, "")
            })
            try:
                fields[5] = _updated_attempt_metadata(
                    fields[5], values, terminal=True
                )
            except DispatchContractError:
                return False
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def annotate_attempt_row(jobs: Path, attempt_id: str, values: dict[str, str]) -> bool:
    """Replace only mutable metadata on one exact attempt under the lock."""
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            fields[5] = _updated_attempt_metadata(fields[5], values)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def annotate_attempt_row_if(
    jobs: Path,
    attempt_id: str,
    values: dict[str, str],
    predicate: Callable[[list[str]], bool],
) -> bool:
    """Compare-and-set mutable metadata on one exact open attempt row."""

    if not attempt_id:
        raise DispatchContractError("attempt-id-required")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in {"open", "running"}:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") != attempt_id:
                continue
            validate_attempt_metadata(metadata)
            if not predicate(fields.copy()):
                return False
            fields[5] = _updated_attempt_metadata(fields[5], values)
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            return True
    return False


def reconcile_local_registry(global_jobs: Path, local_jobs: Path) -> tuple[int, int]:
    """Copy only current-contract local rows into the global registry once."""

    ensure_global_registry_writable(global_jobs)
    if not local_jobs.is_file():
        return 0, 0
    local_lines = local_jobs.read_text(encoding="utf-8").splitlines()
    lock_path = Path(f"{global_jobs}.lock")
    reconciled = 0
    malformed = 0
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        global_lines = global_jobs.read_text(encoding="utf-8").splitlines()
        identities = {
            identity for line in global_lines
            if (identity := _row_identity(line.split("\t"))) is not None
        }
        additions: list[str] = []
        for line in local_lines:
            fields = line.split("\t")
            identity = _row_identity(fields)
            if identity is None:
                malformed += 1
                continue
            metadata=parse_registry_metadata(fields[5])
            try:
                validate_attempt_metadata(metadata)
            except DispatchContractError:
                malformed += 1
                continue
            if identity in identities:
                continue
            fields[5] += f",reconciled_from={local_jobs}"
            additions.append("\t".join(fields))
            identities.add(identity)
            reconciled += 1
        if additions:
            with global_jobs.open("a", encoding="utf-8") as registry:
                for line in additions:
                    registry.write(line + "\n")
                registry.flush()
                os.fsync(registry.fileno())
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return reconciled, malformed
