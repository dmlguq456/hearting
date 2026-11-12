"""Dispatch contract: process observation.

Process identity (pidfd, PID namespaces, cgroups), supervisor leases, the
process table scan, tagged descendants, execution observation, receipts,
quiescence proofs, liveness, and parent extinction.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
import operator
from pathlib import Path
import re
import stat
import sys
import time
from typing import Iterator, Mapping
from route_authority import linked_worktree_slice as is_linked_worktree_slice

from dispatch_contract_base import (
    _cancellation_quiescence_receipt_record, ARTIFACT_PROOF_RECEIPT,
    _has_persisted_process_identity,
    ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT, ATTEMPT_DESCENDANT_ENV, ATTEMPT_DESCENDANT_PROOF,
    ATTEMPT_DESCENDANT_RESIDUE_PROOF, AttemptExecutionObservation,
    AuthoritativeProcessIdentity, CANCELLATION_QUIESCENCE_MAX_WAIT_SECONDS,
    CANCELLATION_QUIESCENCE_POLL_SECONDS, canonical_repository_identity, DispatchContractError,
    GROUP_REAP_PROOF, NAMESPACE_PORTABLE_TEARDOWN_RECEIPT, ObservedAttemptLiveness,
    ParentExtinctionEvidence, PID_HOST_NAMESPACE_PROOF, ProcessGroupObservation,
    ProcessQuiescence, QuiescenceProof, RECOVERY_CONTRACT_VERSION, SUPERVISOR_LEASE_KIND,
    SUPERVISOR_LEASE_NONCE_RE,
)

from dispatch_contract_roots import (
    dispatch_state_root,
)

from dispatch_contract_policy import (
    validate_attempt_metadata,
)


PARENT_EXTINCTION_TERMINAL_STATUSES = frozenset(("done", "killed", "cancelled"))


def resolve_parent_extinction(
    child_metadata, parent_rows, parent_observation=None
):
    """Resolve exact parent extinction for a namespace-local foreground child.

    ``parent_rows`` is a preloaded registry snapshot containing ``(fields, meta)``
    pairs. The resolver never mutates either registry or process state. It
    deliberately requires a durable receipt or authoritative read-only
    quiescence observation in addition to the parent's terminal registry word.
    """
    child = child_metadata if isinstance(child_metadata, dict) else {}
    try:
        validate_attempt_metadata(child)
    except DispatchContractError:
        return ParentExtinctionEvidence("unproven", "child-contract-not-current")
    parent_id = str(child.get("parent_attempt_id") or "")
    required = (
        child.get("dispatch_depth") in (2, "2")
        and child.get("registered_worker") in (True, "1", "true")
        and child.get("pid_scope") == "namespace-local"
        and child.get("launch_lifecycle") == "foreground-scoped"
        and parent_id
        and (child.get("parent") or child.get("parent_slug"))
    )
    if not required:
        return ParentExtinctionEvidence("unproven", "child-contract-not-eligible")
    records = []
    matches = []
    for row in parent_rows or ():
        if isinstance(row, tuple) and len(row) == 2:
            fields, meta = row
            fields = list(fields or [])
            meta = dict(meta or {})
            record = {"fields": fields, "meta": meta}
        elif isinstance(row, dict):
            record = row
            fields = list(row.get("fields") or [])
            meta = dict(row.get("meta") or row)
        else:
            continue
        record = {**record, "fields": fields, "meta": meta}
        records.append(record)
        if meta.get("attempt_id") != parent_id:
            continue
        matches.append(record)
    if len(matches) != 1:
        return ParentExtinctionEvidence(
            "unproven", "parent-attempt-" + ("absent" if not matches else "ambiguous"), parent_id
        )
    record = matches[0]
    fields, parent = record["fields"], record["meta"]
    status = (fields[1] if len(fields) > 1 else parent.get("status", ""))
    if status not in PARENT_EXTINCTION_TERMINAL_STATUSES:
        return ParentExtinctionEvidence("unproven", "parent-not-terminal", parent_id)
    try:
        validate_attempt_metadata(parent)
    except DispatchContractError:
        return ParentExtinctionEvidence("unproven", "parent-contract-not-current", parent_id)
    if parent.get("dispatch_depth") not in (1, "1") or parent.get("worker_type") != "owner":
        return ParentExtinctionEvidence("unproven", "parent-contract-not-owner", parent_id)
    child_repo = child.get("repo") or child.get("repository")
    stable_parent_repo = fields[2] if len(fields) > 2 else ""
    metadata_parent_repo = parent.get("repo") or parent.get("repository") or ""
    if (
        stable_parent_repo
        and metadata_parent_repo
        and canonical_repository_identity(stable_parent_repo)
        != canonical_repository_identity(metadata_parent_repo)
    ):
        return ParentExtinctionEvidence("unproven", "parent-identity-foreign", parent_id)
    parent_repo = stable_parent_repo or metadata_parent_repo
    child_worktree = child.get("worktree") or child.get("cwd")
    stable_parent_worktree = fields[3] if len(fields) > 3 else ""
    metadata_parent_worktree = parent.get("worktree") or parent.get("cwd") or ""
    if (
        stable_parent_worktree
        and metadata_parent_worktree
        and Path(stable_parent_worktree).expanduser().resolve(strict=False)
        != Path(metadata_parent_worktree).expanduser().resolve(strict=False)
    ):
        return ParentExtinctionEvidence("unproven", "parent-worktree-foreign", parent_id)
    parent_worktree = stable_parent_worktree or metadata_parent_worktree
    child_parent_slug = child.get("parent") or child.get("parent_slug")
    stable_parent_slug = fields[4] if len(fields) > 4 else ""
    metadata_parent_slug = parent.get("slug") or ""
    if stable_parent_slug and metadata_parent_slug and stable_parent_slug != metadata_parent_slug:
        return ParentExtinctionEvidence("unproven", "parent-identity-foreign", parent_id)
    parent_slug = stable_parent_slug or metadata_parent_slug
    if not all((child_repo, parent_repo, child_worktree, parent_worktree,
                child_parent_slug, parent_slug)):
        return ParentExtinctionEvidence("unproven", "parent-identity-incomplete", parent_id)
    if (child_parent_slug != parent_slug or
            canonical_repository_identity(child_repo) != canonical_repository_identity(parent_repo)):
        return ParentExtinctionEvidence("unproven", "parent-identity-foreign", parent_id)
    if (Path(child_worktree).expanduser().resolve(strict=False)
        != Path(parent_worktree).expanduser().resolve(strict=False)
        and not is_linked_worktree_slice(child)
    ):
        return ParentExtinctionEvidence("unproven", "parent-worktree-foreign", parent_id)
    child_route_id = str(child.get("route_id") or "")
    child_route_file = str(child.get("route_file") or "")
    if bool(child_route_id) != bool(child_route_file):
        return ParentExtinctionEvidence("unproven", "parent-route-context-conflict", parent_id)
    route_candidates = (
        {(child_route_id, child_route_file)}
        if child_route_id and child_route_file else set()
    )
    parent_route_id = str(parent.get("route_id") or "")
    parent_route_file = str(parent.get("route_file") or "")
    if bool(parent_route_id) != bool(parent_route_file):
        return ParentExtinctionEvidence("unproven", "parent-route-context-conflict", parent_id)
    if parent_route_id and parent_route_file:
        route_candidates.add((parent_route_id, parent_route_file))
    for sibling_record in records:
        sibling = sibling_record["meta"]
        if sibling.get("parent_attempt_id") != parent_id:
            continue
        sibling_fields = sibling_record["fields"]
        stable_sibling_repo = sibling_fields[2] if len(sibling_fields) > 2 else ""
        metadata_sibling_repo = sibling.get("repo") or sibling.get("repository") or ""
        stable_sibling_worktree = sibling_fields[3] if len(sibling_fields) > 3 else ""
        metadata_sibling_worktree = sibling.get("worktree") or sibling.get("cwd") or ""
        if (
            stable_sibling_repo
            and metadata_sibling_repo
            and canonical_repository_identity(stable_sibling_repo)
            != canonical_repository_identity(metadata_sibling_repo)
        ) or (
            stable_sibling_worktree
            and metadata_sibling_worktree
            and Path(stable_sibling_worktree).expanduser().resolve(strict=False)
            != Path(metadata_sibling_worktree).expanduser().resolve(strict=False)
        ):
            return ParentExtinctionEvidence(
                "unproven", "parent-route-context-conflict", parent_id
            )
        sibling_repo = stable_sibling_repo or metadata_sibling_repo
        sibling_worktree = stable_sibling_worktree or metadata_sibling_worktree
        sibling_parent = sibling.get("parent") or sibling.get("parent_slug")
        if not all((sibling_repo, sibling_worktree, sibling_parent)):
            return ParentExtinctionEvidence(
                "unproven", "parent-route-context-conflict", parent_id
            )
        if (
            sibling_parent != parent_slug
            or canonical_repository_identity(sibling_repo)
            != canonical_repository_identity(parent_repo)
            or (Path(sibling_worktree).expanduser().resolve(strict=False)
                != Path(parent_worktree).expanduser().resolve(strict=False)
                and not is_linked_worktree_slice(sibling))
        ):
            return ParentExtinctionEvidence(
                "unproven", "parent-route-context-conflict", parent_id
            )
        sibling_route_id = str(sibling.get("route_id") or "")
        sibling_route_file = str(sibling.get("route_file") or "")
        if bool(sibling_route_id) != bool(sibling_route_file):
            return ParentExtinctionEvidence(
                "unproven", "parent-route-context-conflict", parent_id
            )
        if sibling_route_id and sibling_route_file:
            route_candidates.add((sibling_route_id, sibling_route_file))
    if len(route_candidates) > 1:
        return ParentExtinctionEvidence(
            "unproven", "parent-route-context-conflict", parent_id
        )
    receipt = post_exit_receipt_reason(parent)
    process = attempt_process_quiescence(parent, terminal_receipt=True)
    if process.state == "live":
        return ParentExtinctionEvidence(
            "unproven", "parent-process-still-live:" + process.reason, parent_id
        )
    if receipt:
        return ParentExtinctionEvidence("proven", "parent-terminal-receipt:" + receipt, parent_id)
    if process.state == "quiescent":
        return ParentExtinctionEvidence("proven", "parent-terminal-process:" + process.reason, parent_id)
    observation = (
        parent_observation if isinstance(parent_observation, dict) else {}
    )
    observed_pid = str(observation.get("pid") or "")
    observed_start = str(observation.get("pid_start") or "")
    observed_namespace = str(observation.get("pid_observer_ns") or "")
    current_namespace = process_namespace_identity() or ""
    recorded_observer = str(parent.get("pid_observer_ns") or "")
    recorded_process_namespace = str(parent.get("pid_ns") or "")
    observer_owns_recorded_pid = bool(
        observed_namespace
        and observed_namespace == current_namespace
        and (
            (
                recorded_observer == observed_namespace
                and recorded_process_namespace in {"", observed_namespace}
            )
            or (
                not recorded_observer
                and parent.get("pid_scope", "host-visible") != "namespace-local"
                and recorded_process_namespace in {"", observed_namespace}
            )
        )
    )
    if (
        observation.get("state") == "extinct"
        and observation.get("parent_attempt_id") == parent_id
        and observer_owns_recorded_pid
        and observed_pid
        and observed_pid == str(parent.get("pid") or "")
        and observed_start
        and observed_start == str(parent.get("pid_start") or "")
        and process.reason in {
            "local-process-group-identity-unverifiable",
            "host-process-group-identity-unverifiable",
            "post-exit-receipt-incomplete",
        }
    ):
        visibility, actual_start, state = _proc_observation(int(observed_pid))
        if (
            visibility == "missing"
            or (
                visibility == "present"
                and (actual_start != observed_start or state == "Z")
            )
        ):
            return ParentExtinctionEvidence(
                "proven", "parent-terminal-watcher-pid-extinct", parent_id
            )
    return ParentExtinctionEvidence("unproven", "parent-extinction-unproven:" + process.reason, parent_id)


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


def pidfd_open(pid: int, flags: int = 0) -> int:
    """Open a descriptor bound to one process, including older Python builds.

    The raw syscall is verified only for Linux x86_64 LP64. Other ABIs need
    their own checked syscall table before they can use this fallback.
    """
    native = getattr(os, "pidfd_open", None)
    if callable(native):
        return native(pid, flags)

    def unsupported() -> OSError:
        return OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    if sys.platform != "linux":
        raise unsupported()
    try:
        import ctypes
        import platform
    except ImportError:
        raise unsupported() from None
    if (platform.machine() != "x86_64" or ctypes.sizeof(ctypes.c_void_p) != 8
            or ctypes.sizeof(ctypes.c_long) != 8):
        raise unsupported()

    # Validate before ctypes converts arguments; narrowing a PID could bind
    # the descriptor to an entirely different process.
    pid_number = operator.index(pid)
    flags_number = operator.index(flags)
    if not -(1 << 31) <= pid_number < (1 << 31):
        raise OverflowError("pid out of range for pid_t")
    if not 0 <= flags_number < (1 << 32):
        raise OverflowError("flags out of range for unsigned int")
    try:
        syscall = ctypes.CDLL(None, use_errno=True).syscall
    except (OSError, AttributeError, TypeError):
        raise unsupported() from None
    if not callable(syscall):
        raise unsupported()
    syscall.restype = ctypes.c_long
    fd = syscall(ctypes.c_long(434), ctypes.c_int(pid_number),
                 ctypes.c_uint(flags_number))
    if fd == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    try:
        os.set_inheritable(fd, False)
    except BaseException:
        os.close(fd)
        raise
    return fd


def process_namespace_identity(pid: int | str = "self") -> str | None:
    """Return the PID namespace inode without treating an unreadable link as absence."""

    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


LAUNCH_CGROUP_METADATA_KEYS = ("launch_cgroup_b64", "launch_cgroup_ns")


def process_cgroup_namespace_identity(pid: int | str = "self") -> str | None:
    """The cgroup namespace inode; a cgroup path is only comparable from the same one."""

    try:
        return os.readlink(f"/proc/{pid}/ns/cgroup")
    except OSError:
        return None


def _unified_cgroup_path(text: str) -> str:
    """The single cgroup v2 `0::` path, or "" (cgroup v1 only, malformed, or ambiguous)."""

    paths = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    return paths[0] if len(paths) == 1 and paths[0].startswith("/") else ""


def process_unified_cgroup(pid: int | str) -> str:
    try:
        return _unified_cgroup_path((Path("/proc") / str(pid) / "cgroup").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def launch_cgroup_values(path: str, namespace: str) -> dict[str, str]:
    """Registry encoding: the path is urlsafe base64 (a cgroup path may hold `,`, `=`, tabs)."""

    if not (path.startswith("/") and namespace):
        return {}
    return {"launch_cgroup_b64": base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii"),
            "launch_cgroup_ns": namespace}


def process_launch_cgroup(pid: int) -> dict[str, str]:
    """Where a fenced, not-yet-released leader runs: its unified cgroup and the observer's cgroup namespace."""

    return launch_cgroup_values(process_unified_cgroup(pid), process_cgroup_namespace_identity() or "")


LAUNCH_LINEAGE_METADATA_KEYS = ("launch_lineage", "launch_lineage_ns")
_LINEAGE_MAX_DEPTH = 256


def _stat_parent_and_start(proc_root: Path, pid: int) -> tuple[int, str] | None:
    """``(ppid, start ticks)`` from one ``stat`` read, or None when it cannot be read."""

    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2:].split()
        ppid, start = int(tail[1]), tail[19]
    except (OSError, ValueError, IndexError):
        return None
    return (ppid, start) if start.isdigit() else None


def _verified_ancestry(proc_root: Path, pid: int, start: str) -> tuple[tuple[int, str], ...] | None:
    """The current parent chain above one exact process, outermost last, or None.

    Every hop is read child, parent, child again: the child must keep the same
    parent and start ticks across the parent read, and the parent must be born
    no later than the child. A parent that died between the reads reparents the
    child first, so its reused number can never pass as that parent. Any failed
    read, change, or a chain longer than the bound answers None."""

    chain: list[tuple[int, str]] = []
    child, child_start = pid, start
    for _ in range(_LINEAGE_MAX_DEPTH):
        first = _stat_parent_and_start(proc_root, child)
        if first is None or first[1] != child_start:
            return None
        if first[0] <= 0:
            return tuple(chain)
        parent = _stat_parent_and_start(proc_root, first[0])
        if parent is None or _stat_parent_and_start(proc_root, child) != first:
            return None
        if int(parent[1]) > int(child_start):
            return None
        chain.append((first[0], parent[1]))
        child, child_start = first[0], parent[1]
    return None


def _procfs_is_observer_namespace() -> bool:
    try:
        return os.readlink("/proc/self") == str(os.getpid())
    except OSError:
        return False


def process_launch_lineage(pid: int) -> dict[str, str]:
    """The fenced leader's parent chain at launch, as ``pid:start`` pairs joined by ``+``.

    Recorded by the launcher while its own child is fenced, so the chain is the
    leader's ancestry at birth: the launcher and everything above it. Only a
    leader whose parent is this process and a ``/proc`` of this PID namespace
    record anything; otherwise the row carries no lineage and keeps today's
    answers."""

    me = os.getpid()
    proc_root = Path("/proc")
    leader = _stat_parent_and_start(proc_root, pid)
    own = _stat_parent_and_start(proc_root, me)
    namespace = process_namespace_identity()
    if leader is None or leader[0] != me or own is None or not namespace or not _procfs_is_observer_namespace():
        return {}
    chain = _verified_ancestry(proc_root, me, own[1])
    if chain is None:
        return {}
    return {"launch_lineage": "+".join(f"{p}:{s}" for p, s in ((me, own[1]), *chain)),
            "launch_lineage_ns": namespace}


def _lineage_outside_attempt(ancestry, metadata) -> bool:
    """A live ancestor born before the leader and absent from its recorded launch lineage.

    A fork copies its parent's place in the process tree, and an orphan is only
    ever adopted by a live process on its own current chain, so a process's
    current ancestors are always a subset of its earlier ones. Every process
    descended from the leader therefore has current ancestors that are the
    leader's descendants (born no earlier than it), the leader, or members of
    the leader's ancestry at birth. An ancestor strictly older than the leader
    and not in that recorded ancestry proves the process is outside the attempt,
    whatever its group, session, cgroup, or readable environment. The first
    older ancestor decides: a recorded member's own ancestors are recorded too."""

    recorded, namespace = metadata.get("launch_lineage", ""), metadata.get("launch_lineage_ns", "")
    origin = metadata.get("pid_start", "")
    if not (ancestry and recorded and namespace and isinstance(origin, str) and origin.isdigit()):
        return False
    members = set(recorded.split("+"))
    if not all(p.isdigit() and s.isdigit() for p, sep, s in (m.partition(":") for m in members)):
        return False
    if process_namespace_identity() != namespace or not _procfs_is_observer_namespace():
        return False
    for pid, start in ancestry:
        if int(start) < int(origin):
            return f"{pid}:{start}" not in members
    return False


def _denied_process_ancestry(entry: Path, stable_start: str) -> tuple[tuple[int, str], ...]:
    if not stable_start:
        return ()
    return _verified_ancestry(entry.parent, int(entry.name), stable_start) or ()


def _recorded_launch_cgroup(metadata) -> tuple[str, str]:
    raw, namespace = metadata.get("launch_cgroup_b64", ""), metadata.get("launch_cgroup_ns", "")
    if not raw or not namespace:
        return "", ""
    try:
        encoded = raw.encode("ascii")
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).decode("ascii") != raw:
            return "", ""
        path = decoded.decode("utf-8")
    except (ValueError, UnicodeError):
        return "", ""
    return (path, namespace) if path.startswith("/") else ("", "")


def _runtime_ancestry_proc_stat(pid: int) -> dict[str, int] | None:
    """`ppid`/`start` for one PID -- same two /proc/<pid>/stat fields
    ``compute-hosts.py``'s own ``proc_stat()`` reads (SD-111 §3.2.1: same
    algorithm, reimplemented here because that name lives inside
    ``PROBE_SCRIPT``, a string template shipped to remote hosts for SSH
    execution, not an importable module-level function -- §3.2.1's "reuse the
    primitive unchanged" cannot mean a Python import of it. See the P2 dev
    log for this plan/reality mismatch)."""

    try:
        raw = Path("/proc") / str(pid) / "stat"
        text = raw.read_text(encoding="utf-8", errors="replace")
        rest = text[text.rfind(")") + 2:].split()
        return {"ppid": int(rest[1]), "start": int(rest[19])}
    except (OSError, ValueError, IndexError):
        return None


def _runtime_ancestry_harness_process(pid: int) -> str | None:
    """Same algorithm as ``compute-hosts.py``'s embedded ``harness_process()``
    (see ``_runtime_ancestry_proc_stat`` docstring for why this is a
    reimplementation, not an import)."""

    names = {"claude": "claude", "codex": "codex", "opencode": "opencode"}
    try:
        comm = (Path("/proc") / str(pid) / "comm").read_text().strip().lower()
    except OSError:
        comm = ""
    if comm in names:
        return names[comm]
    try:
        with (Path("/proc") / str(pid) / "cmdline").open("rb") as handle:
            argv0 = handle.read(4096).split(b"\0", 1)[0]
        base = os.path.basename(argv0.decode("utf-8", errors="ignore")).lower()
    except OSError:
        base = ""
    return names.get(base)


def runtime_ancestry_binding(pid: int) -> tuple[str, str, str] | None:
    """SD-111 P2 round 2 C-3: walk `pid`'s /proc ancestry for the nearest
    Claude runtime session process.

    Used both at launch time (2-a-5, `append_job()` records the launcher's
    own binding) and at carrier-1 claim time (P3, the hook re-derives its own
    binding and compares). Returns `(pid, start_ticks, pid_ns)` as strings,
    or `None` if no ancestor resolves to the "claude" harness -- callers must
    write all three fields or none (partial recording is forbidden).
    """

    current = pid
    seen: set[int] = set()
    depth = 0
    while current > 0 and current not in seen and depth < 128:
        seen.add(current)
        found = _runtime_ancestry_proc_stat(current)
        if found is None:
            return None
        if _runtime_ancestry_harness_process(current) == "claude":
            ns = process_namespace_identity(current)
            if not ns:
                return None
            return (str(current), str(found["start"]), ns)
        current = found["ppid"]
        depth += 1
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


def process_identity_disposition(pid: int, expected_start: str) -> str:
    """Return 'dead', 'live', or 'unresolved', distinguishing absence from denial.

    'dead' only for a positive determination: visibility 'missing', or
    'present' with a mismatched start tick (a different process reused the
    pid) or a zombie state. 'inaccessible' (permission/namespace/procfs
    denial) and any other non-determination is 'unresolved', never 'dead' --
    an expiry actor must not convert "cannot tell" into "session is gone"
    (SD-111 F-1, PRD §13.33.1-(7), A-10).
    """

    if not expected_start:
        return "unresolved"
    visibility, actual_start, state = _proc_observation(pid)
    if visibility == "missing":
        return "dead"
    if visibility != "present":
        return "unresolved"
    if actual_start != expected_start or state == "Z":
        return "dead"
    return "live"


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
        "opencode": "session-resume-supervised",
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
    # Observation can run inside a terminal writer's jobs lock. Reopening
    # that same lock would block this process on its own open description.
    # Lease acquisition still holds its separate exact flock and confirms
    # the declaration again after acquisition.
    from dispatch_registry_cache import registry_view
    try:
        records = registry_view(jobs).completion_attempt_records(attempt_id)
    except OSError as exc:
        raise DispatchContractError("supervisor-lease-registry-unreadable", str(exc)) from exc
    if len(records) != 1 or records[0].status not in {"open", "running"}:
        raise DispatchContractError("supervisor-lease-attempt-not-open", attempt_id)
    metadata = records[0].completion_metadata()
    if not _supervisor_lease_metadata_valid(jobs, metadata):
        raise DispatchContractError("supervisor-lease-declaration-invalid", attempt_id)
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
    # Older rows may carry the checked procfs-root mapping without the later
    # observer mirror. The same stored host proof already authorizes the PID
    # reader; it also authorizes a complete descendant scan in that namespace.
    return (metadata.get("pid_scope", "host-visible") != "namespace-local"
            or (metadata.get("pid_host_proof") == PID_HOST_NAMESPACE_PROOF
                and metadata.get("pid_host_ns") == current_namespace))


def _current_observer_is_host_like() -> bool:
    """Minimal, self-contained echo of ``dispatch_lifecycle.pid_namespace_evidence``.

    Duplicated rather than imported: ``dispatch_lifecycle`` imports from this
    module, so importing it back here would be circular. Only the host-like
    classification is needed, not the full evidence dict.
    """

    nested = False
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("NSpid:"):
                nested = len(line.split()) - 1 > 1
                break
    except OSError:
        return False
    if nested:
        return False
    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return comm in {"systemd", "init"}


def _procfs_root_namespace_member(path: Path, pid: int) -> bool:
    """A single NSpid column proves membership in the procfs root namespace.

    Linux fs/proc/array.c emits NSpid from the procfs mount namespace down to
    the process namespace. This is kernel evidence even when ptrace policy
    denies readlink(ns/pid) for a root-owned process; nested or hidden status
    remains unknown. Both the observer and candidate must pass this check.
    """
    try:
        entries = [line.split()[1:] for line in path.read_text().splitlines()
                   if line.startswith("NSpid:")]
    except (OSError, ValueError):
        return False
    return entries == [[str(pid)]] and pid > 0


def observer_namespace_extinct(metadata: dict[str, str]) -> str:
    """Whether the row's recorded observer PID namespace still exists on the host.

    Absence of the namespace is strictly stronger than an empty scan inside it:
    an empty scan only proves nothing tagged is visible from within that
    namespace, while a namespace's absence proves nothing in it can be running
    anywhere. An incomplete walk is never absence -- fails closed to
    ``"unverifiable"`` at every step, including on a ``/proc`` walk this
    observer cannot fully complete (e.g. a hidepid mount).

    PID-namespace inode residual (plan Risk 1): ``pid_observer_ns`` is a
    ``pid:[<inode>]`` string and the kernel recycles namespace inode numbers.
    A readlink match is therefore match-on-value, not match-on-identity -- if
    the kernel has recycled the recorded inode onto an unrelated live
    namespace, this deliberately answers ``"present"`` (safe direction: the row
    stays ineligible for cancellation) rather than trying to disambiguate an
    identity it cannot prove.
    """

    recorded_observer = metadata.get("pid_observer_ns", "")
    if (
        not recorded_observer
        or metadata.get("pid_scope") != "namespace-local"
        or metadata.get("registered_worker") != "1"
    ):
        return "unverifiable"
    return _recorded_pid_namespace_state(recorded_observer)


def _recorded_pid_namespace_state(recorded: str) -> str:
    """``observer_namespace_extinct``'s host walk for one recorded ``pid:[inode]`` value."""

    try:
        current = os.readlink("/proc/self/ns/pid")
    except OSError:
        return "unverifiable"
    if not current:
        return "unverifiable"
    if not _current_observer_is_host_like():
        return "unverifiable"
    if current == recorded:
        return "present"
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return "unverifiable"
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        try:
            candidate = os.readlink(f"/proc/{entry}/ns/pid")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as exc:
            # Ordinary users cannot read root-owned namespace links. A
            # readable single-column NSpid still proves that process belongs
            # to our procfs-root namespace, not the recorded foreign one.
            if (exc.errno in {errno.EACCES, errno.EPERM}
                    and _procfs_root_namespace_member(Path("/proc/self/status"), os.getpid())
                    and _procfs_root_namespace_member(Path(f"/proc/{entry}/status"), int(entry))):
                continue
            return "unverifiable"
        if candidate == recorded:
            return "present"
    return "extinct"


NAMESPACE_EXTINCT_REASON = "namespace-extinct"
_PID_NAMESPACE_LINK = re.compile(r"pid:\[[0-9]+\]\Z")


def namespace_gone(metadata: dict[str, str]) -> str:
    """Whether every PID namespace this row recorded has left the host.

    Both the launch observer's namespace (``pid_observer_ns``, where the wrapper
    ran) and the worker's (``pid_ns``) are checked with the
    ``observer_namespace_extinct`` rule: a host-like observer, a complete walk,
    and a value match that answers ``present`` even for a recycled inode. Any
    ``present`` wins, then any ``unverifiable``; only all-``extinct`` is
    ``extinct``. A process can never move to an ancestor PID namespace, so once
    the namespace that launched the attempt is gone, nothing it started can be
    running anywhere -- which is why a sandbox that died with its caller (the
    5th Codex run) is an exact death and not a guess. A sandboxed observer
    cannot see sibling namespaces and always answers ``unverifiable``; the host
    runtime decides those rows. A recorded value that is not a kernel
    ``pid:[<inode>]`` link was never observed and proves nothing.
    """

    recorded_observer = metadata.get("pid_observer_ns", "")
    if (
        not recorded_observer
        or metadata.get("pid_scope") != "namespace-local"
        or metadata.get("registered_worker") != "1"
    ):
        return "unverifiable"
    recorded_values = tuple(
        value
        for value in dict.fromkeys((recorded_observer, metadata.get("pid_ns", "")))
        if value
    )
    if any(_PID_NAMESPACE_LINK.fullmatch(value) is None for value in recorded_values):
        return "unverifiable"
    states = [_recorded_pid_namespace_state(recorded) for recorded in recorded_values]
    if "present" in states:
        return "present"
    if "unverifiable" in states:
        return "unverifiable"
    return "extinct"


def recorded_pid_namespace_state(recorded: str) -> str:
    """present | extinct | unverifiable for one recorded ``pid:[inode]`` value.

    The per-value rule `namespace_gone` applies to a row's own namespaces, for a
    caller that holds a different recorded namespace (a summary owner's).
    """

    if _PID_NAMESPACE_LINK.fullmatch(recorded or "") is None:
        return "unverifiable"
    return _recorded_pid_namespace_state(recorded)


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
    # A one-element vector identifies only the local procfs view.  It cannot
    # establish an outer PID, start time, namespace, or process-group mapping.
    # For a multi-level vector, absence of an independently observed procfs
    # root namespace remains unverifiable.
    if (
        len(namespace_pids) > 1
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
    scan = _PROCESS_TABLE_SCAN.get()
    if scan is not None:
        # Batch observer pass (`process_table_scan_scope`): same verdict, one
        # walk shared by every group of the pass instead of one per call.
        return _process_group_from_scan(scan, pgid)
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


def _parent_leader_pids(metadata: dict[str, str]) -> set[int]:
    """The recorded parent leader as an exact identity, else nothing.

    `parent_pid` counts only when `parent_pid_start` matches the live start
    ticks of that pid in this observer's namespace. `parent_pid_host` is a
    host-namespace number recorded only when the parent lives in another
    namespace, so it is comparable here only when the row's own namespace is
    the observer's and the parent is host-visible.
    """

    excluded: set[int] = set()
    raw = str(metadata.get("parent_pid", ""))
    start = str(metadata.get("parent_pid_start", ""))
    if raw.isdigit() and start and process_start_ticks(int(raw)) == start:
        excluded.add(int(raw))
    raw_host = str(metadata.get("parent_pid_host", ""))
    host_start = str(metadata.get("parent_pid_host_start", "") or start)
    if (
        raw_host.isdigit()
        and host_start
        and metadata.get("parent_pid_scope") == "host-visible"
        and metadata.get("pid_ns")
        and metadata.get("pid_ns") == metadata.get("pid_observer_ns")
        and process_start_ticks(int(raw_host)) == host_start
    ):
        excluded.add(int(raw_host))
    return excluded


@dataclass(frozen=True)
class ProcessTableScan:
    """One ``/proc`` walk, indexed by attempt tag and by process group.

    Shared by every observation of one observer pass (`process_table_scan_scope`).
    ``incomplete_reason`` follows `attempt_tagged_descendants`' rules for the
    tag index. ``group_errors`` follows `process_group_observation`'s rules: each
    entry is ``(walk_order, pgid_or_None, reason)`` -- ``None`` for a failure the
    single-shot probe hits before it can compare the group (unreadable ``stat``,
    unparsable pgid), the pgid for one it hits only while collecting that group
    (a parsed pgid whose start field is missing). A group query takes the last
    entry in walk order that applies to it, exactly as the single-shot loop's
    ``incomplete_reason`` is the last one it overwrote.
    """

    members_by_attempt: dict[str, tuple[tuple[int, str, str], ...]]
    members_by_pgid: dict[int, tuple[tuple[int, str, str], ...]]
    incomplete_reason: str = ""
    group_errors: tuple[tuple[int, int | None, str], ...] = ()
    error: str = ""
    tag_access_errors: tuple[tuple[int, str, str], ...] = ()
    tag_access_cgroups: tuple[tuple[tuple[int, str], str], ...] = ()
    tag_access_ancestry: tuple[tuple[tuple[int, str], tuple[tuple[int, str], ...]], ...] = ()


_PROCESS_TABLE_SCAN: contextvars.ContextVar = contextvars.ContextVar(
    "dispatch_process_table_scan", default=None
)


def scan_process_table() -> ProcessTableScan:
    """Walk ``/proc`` once and index live processes by attempt tag and by pgid.

    Per-process reads, skips, and incomplete reasons are exactly those of the
    two single-shot probes, evaluated independently on the same raw ``stat``
    line: the tag index needs ``tail[0]``/``tail[19]`` and then ``environ``
    (permission failures retain UID and adjacent birth observations); the group
    index needs ``int(tail[2])`` first (a failure there is a group-wide
    incomplete walk, like an unreadable ``stat``) and ``tail[19]`` only for the
    group that pgid names. The tag value is recorded for every process instead
    of being compared with one attempt id; parent-leader exclusion is per
    attempt and is applied later, in `_tagged_descendants_from_scan`.
    """

    prefix = f"{ATTEMPT_DESCENDANT_ENV}=".encode()
    by_attempt: dict[str, list[tuple[int, str, str]]] = {}
    by_pgid: dict[int, list[tuple[int, str, str]]] = {}
    incomplete_reason = ""
    group_errors: list[tuple[int, int | None, str]] = []
    tag_access_errors: list[tuple[int, str, str]] = []
    tag_access_cgroups: list[tuple[tuple[int, str], str]] = []
    tag_access_ancestry: list[tuple[tuple[int, str], tuple[tuple[int, str], ...]]] = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessTableScan({}, {}, error=f"procfs-enumeration:{exc.errno or 'error'}")
    for order, entry in enumerate(entries):
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            group_errors.append((order, None, f"procfs-member:{entry.name}:{exc.errno or 'error'}"))
            if exc.errno in {errno.EACCES, errno.EPERM}:
                _start, reason = _tag_access_observation(entry)
                if reason:
                    tag_access_errors.append((int(entry.name), "", reason))
            else:
                incomplete_reason = f"procfs-environ:{entry.name}:{exc.errno or 'error'}"
            continue
        except ValueError:
            # A comm that is not UTF-8 raises UnicodeDecodeError (a ValueError) out
            # of the read itself; both single-shot probes count that row as
            # malformed before any field is compared (review round 2, major 1).
            reason = f"procfs-member:{entry.name}:malformed"
            group_errors.append((order, None, reason))
            incomplete_reason = reason
            continue
        tail = raw[raw.rfind(")") + 2 :].split()
        pid = int(entry.name)
        # Group index: the single-shot probe compares `int(tail[2])` before it
        # touches anything else, so a pgid it cannot parse is an incomplete walk
        # for every group; a missing start field is seen only by that group.
        try:
            pgid = int(tail[2])
        except (IndexError, ValueError):
            group_errors.append((order, None, f"procfs-member:{entry.name}:malformed"))
        else:
            try:
                by_pgid.setdefault(pgid, []).append((pid, tail[19], tail[0]))
            except IndexError:
                group_errors.append((order, pgid, f"procfs-member:{entry.name}:malformed"))
        # Tag index: state and start, zombie skip, then environ.
        try:
            state, start = tail[0], tail[19]
        except IndexError:
            incomplete_reason = f"procfs-member:{entry.name}:malformed"
            continue
        if state == "Z":
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            if exc.errno in {errno.EACCES, errno.EPERM}:
                stable_start, reason = _tag_access_observation(entry, start)
                if reason:
                    tag_access_errors.append((pid, stable_start, reason))
                    cgroup = _denied_process_cgroup(entry, stable_start)
                    if cgroup:
                        tag_access_cgroups.append(((pid, stable_start), cgroup))
                    ancestry = _denied_process_ancestry(entry, stable_start)
                    if ancestry:
                        tag_access_ancestry.append(((pid, stable_start), ancestry))
                continue
            incomplete_reason = f"procfs-environ:{entry.name}:{exc.errno or 'error'}"
            continue
        # `tag in environ.split(...)` names a process once however many times the
        # same entry repeats; a process carrying two different tag values is
        # found by either single-shot query, so it is indexed under both.
        for item in dict.fromkeys(environ.split(b"\0")):
            if item.startswith(prefix):
                tag_value = item[len(prefix):].decode("utf-8", "replace")
                by_attempt.setdefault(tag_value, []).append((pid, start, state))
    return ProcessTableScan(
        {tag: tuple(sorted(rows, key=lambda member: member[0])) for tag, rows in by_attempt.items()},
        {pgid: tuple(sorted(rows, key=lambda member: member[0])) for pgid, rows in by_pgid.items()},
        incomplete_reason,
        tuple(group_errors),
        tag_access_errors=tuple(tag_access_errors),
        tag_access_cgroups=tuple(tag_access_cgroups),
        tag_access_ancestry=tuple(tag_access_ancestry),
    )


@contextmanager
def process_table_scan_scope():
    """Observe-only batch path: one ``/proc`` walk for observations in one scope.

    `attempt_tagged_descendants` and `process_group_observation` are single-shot
    probes for the moment before a quiescence verdict and are never meant for a
    hot path -- yet a monitor that classifies every registered attempt on every
    tick reached both once per row (2026-09-08 audit: 187 rows, 20 s of a ~40 s
    tick). Inside this scope every call answers from one walk taken on entry,
    with the same populated / unverifiable / empty verdict per attempt or group;
    the walk is dropped on exit, so the next pass reads ``/proc`` again. Only
    observe-only passes may hold this scope: a caller that waits for a process
    to disappear must stay outside it, which every existing quiescence caller
    does. A nested scope joins the enclosing pass.
    """

    if _PROCESS_TABLE_SCAN.get() is not None:
        yield
        return
    token = _PROCESS_TABLE_SCAN.set(scan_process_table())
    try:
        yield
    finally:
        _PROCESS_TABLE_SCAN.reset(token)


def _tag_scan_authority(metadata: dict[str, str], host_complete: bool) -> bool:
    """Who may read an empty tag scan as absence.

    ``host_complete`` is the extinct-namespace question only: a host-like
    observer walks every PID namespace on the machine, so finding no tag there
    is absence wherever the processes went. Every other caller keeps the
    recording-namespace rule of ``attempt_scan_namespace_authority``.
    """

    if host_complete:
        return _current_observer_is_host_like()
    return attempt_scan_namespace_authority(metadata)


def _tagged_descendants_from_scan(
    scan: ProcessTableScan, metadata: dict[str, str], attempt_id: str,
    *, host_complete: bool = False, preserved=frozenset(),
) -> ProcessGroupObservation:
    if scan.error:
        return ProcessGroupObservation("unverifiable", reason=scan.error)
    excluded_pids = _parent_leader_pids(metadata)
    ordered = tuple(
        member for member in scan.members_by_attempt.get(attempt_id, ())
        if member[0] not in excluded_pids and member[:2] not in preserved
    )
    if ordered:
        return ProcessGroupObservation("populated", ordered, scan.incomplete_reason)
    if scan.incomplete_reason:
        return ProcessGroupObservation("unverifiable", (), scan.incomplete_reason)
    if not _tag_scan_authority(metadata, host_complete):
        return ProcessGroupObservation(
            "unverifiable", (), "observer-namespace-mismatch"
        )
    cgroups = dict(scan.tag_access_cgroups)
    ancestries = dict(scan.tag_access_ancestry)
    access_reason = next((reason for pid, start, reason in scan.tag_access_errors
                          if pid not in excluded_pids and not _denied_process_outside_attempt(
                              start, metadata, host_complete=host_complete,
                              cgroup=cgroups.get((pid, start), ""),
                              ancestry=ancestries.get((pid, start), ()))), "")
    if access_reason:
        return ProcessGroupObservation("unverifiable", (), access_reason)
    return ProcessGroupObservation("empty")


def _process_group_from_scan(scan: ProcessTableScan, pgid: int) -> ProcessGroupObservation:
    if scan.error:
        return ProcessGroupObservation("unverifiable", reason=scan.error)
    ordered = scan.members_by_pgid.get(pgid, ())
    incomplete_reason = ""
    for _order, scope_pgid, reason in scan.group_errors:
        if scope_pgid is None or scope_pgid == pgid:
            incomplete_reason = reason          # last applicable entry in walk order wins
    if any(state != "Z" for _pid, _start, state in ordered):
        return ProcessGroupObservation("populated", ordered, incomplete_reason)
    if incomplete_reason:
        return ProcessGroupObservation("unverifiable", ordered, incomplete_reason)
    return ProcessGroupObservation("empty", ordered)


def _forked_across_uid_boundary(entry: Path, tail) -> bool:
    """Whether this process was forked by a live parent that runs under another real UID.

    Such a parent is outside every attempt by the other-UID rule below, and while it lives the
    child's environment came from it, not from an attempt (an orphan is reparented to its
    reaper instead, so init is never this parent). The parent must be older than the child,
    which rules out a reused parent number. A `sudo` parent keeps the caller's real UID, so
    an attempt's own sudo child is not excluded. Example: the non-dumpable `sshd: <user>@notty`
    session process under root's `sshd [priv]` (a VS Code Remote-SSH connection). `tail` is the
    child's own `/proc/<pid>/stat` fields after the command name, already read."""
    try:
        ppid, start = int(tail[1]), int(tail[19])
        if ppid <= 1:
            return False
        parent = entry.parent / str(ppid)
        parent_raw = (parent / "stat").read_text(encoding="utf-8")
        parent_start = int(parent_raw[parent_raw.rfind(")") + 2:].split()[19])
        status = (parent / "status").read_text(encoding="utf-8")
        real_uid = int(next(line.split()[1] for line in status.splitlines() if line.startswith("Uid:")))
    except (OSError, ValueError, IndexError, StopIteration):
        return False
    return real_uid != os.getuid() and parent_start <= start


def _in_user_manager_scope(entry: Path) -> bool:
    """Whether this process lives in the user's service manager scope.

    systemd starts `systemd --user` and its `(sd-pam)` helper from PID 1 straight into
    `user@<uid>.service/init.scope`, and both are non-dumpable. A fork inherits its parent's
    cgroup, so an attempt's descendant sits in its session or app scope, never there; the
    birth rule cannot help because the manager restarts on a later login."""
    try:
        cgroups = (entry / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return False
    scope = f"/user@{os.getuid()}.service/init.scope"
    return any(line.rpartition(":")[2].endswith(scope) for line in cgroups.splitlines())


def _tag_access_observation(entry: Path, start: str = "") -> tuple[str, str]:
    """Only another UID, a fork across a UID boundary, the user's service manager scope, or an
    adjacent stable birth observation may exclude denial."""
    try:
        if entry.stat().st_uid != os.getuid():
            return "", ""
    except FileNotFoundError:
        return "", ""
    except OSError:
        return "", f"procfs-environ:{entry.name}:uid-unobservable"
    try:
        raw = (entry / "stat").read_text(encoding="utf-8")
        tail = raw[raw.rfind(")") + 2:].split()
        flags = int(tail[6], 10)
        if tail[0] == "Z" or flags & 0x4:  # PF_EXITING
            # exit_mm can revoke environ before the process becomes a zombie.
            # A PF_EXITING task cannot run or fork either.
            return "", ""
        current = tail[19]
        stable = start if start.isdigit() and current == start else ""
    except (FileNotFoundError, ProcessLookupError):
        return "", ""
    except (OSError, IndexError, ValueError):
        stable, tail = "", None
    if tail is not None and _forked_across_uid_boundary(entry, tail):
        return "", ""
    if _in_user_manager_scope(entry):
        return "", ""
    return stable, f"procfs-environ:{entry.name}:same-uid-unobservable"


def _denied_process_cgroup(entry: Path, stable_start: str) -> str:
    """The unified cgroup of a denied same-UID process, only while its start ticks still match."""

    if not stable_start:
        return ""
    try:
        path = _unified_cgroup_path((entry / "cgroup").read_text(encoding="utf-8"))
        raw = (entry / "stat").read_text(encoding="utf-8")
        current = raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return ""
    return path if current == stable_start else ""


def _service_cgroup_outside_launch(cgroup: str, metadata) -> bool:
    """A denied process in a systemd service unit disjoint from the recorded launch cgroup is not a descendant."""

    launch, namespace = _recorded_launch_cgroup(metadata)
    if not (launch and cgroup.startswith("/") and namespace
            and process_cgroup_namespace_identity() == namespace):
        return False
    if not cgroup.rstrip("/").rsplit("/", 1)[-1].endswith(".service"):
        return False

    def inside(outer: str, inner: str) -> bool:
        return inner == outer or inner.startswith(outer.rstrip("/") + "/")

    return not inside(launch, cgroup) and not inside(cgroup, launch)


def _denied_process_outside_attempt(
    start: str, metadata: dict[str, str], *, host_complete=False, cgroup: str = "", ancestry=()
) -> bool:
    """A denied process born before this exact local child, running in a systemd service
    unit disjoint from the cgroup recorded at launch, or with a live ancestor older than the
    leader outside its recorded launch lineage (`_lineage_outside_attempt`), is not its
    descendant."""
    if not metadata.get('pid') and metadata.get('launch_outcome') in {'never-launched', 'reaped-before-publish'}:
        return True  # atomic fenced outcome: no payload could create a descendant
    if (metadata.get("launch_claimed") == "0"
            and metadata.get("launch_started", "") in {"", "0"}
            and metadata.get("registered_worker") == "1"
            and metadata.get("execution_surface") == "registered-headless"
            and not any(metadata.get(key) for key in (
                "pid", "pid_start", "pid_host", "pid_host_start", "pgid"))):
        return True  # atomic register-only row: no runner has claimed execution
    if _detached_group_drain_receipt(metadata):
        # The watcher sealed this receipt only from an empty tagged scan, and an
        # environment passes from parent to child only, so no process born since
        # can carry this attempt's tag. Readable tags still answer `live`.
        return True
    if _service_cgroup_outside_launch(cgroup, metadata):
        return True
    if _lineage_outside_attempt(ancestry, metadata):
        return True
    origin = metadata.get("pid_start", "")
    authority = attempt_scan_namespace_authority(metadata) or (
        host_complete and _current_observer_is_host_like() and namespace_gone(metadata) == 'extinct')
    return bool(isinstance(start, str) and isinstance(origin, str)
                and start.isdigit() and origin.isdigit() and int(start) < int(origin)
                and authority)


def attempt_tagged_descendants(
    metadata: dict[str, str], *, host_complete: bool = False, preserved=frozenset()
) -> ProcessGroupObservation:
    """Find live processes still tagged with this attempt, whatever group they left.

    The recorded leader and process group are the only things SD-79's quiescent
    verdict looks at, so a descendant that re-``setsid``'d out of that group
    reads as absence even while it runs. Every dispatched worker carries its
    attempt id in the environment, so scanning ``/proc/<pid>/environ`` for that
    tag finds the process wherever it went.

    Emptiness is evidence only from the namespace that recorded the identities;
    from anywhere else the tagged processes may simply be invisible, so that
    case is ``unverifiable`` rather than a false death. Only a verified different
    UID, a fork by a live parent under another real UID (`_forked_across_uid_boundary`),
    an adjacent stable birth earlier than this exact local child, or a systemd
    `.service` unit disjoint from the cgroup recorded at launch (`launch_cgroup_b64`,
    same cgroup namespace), or a live ancestor older than the leader outside the
    leader's recorded launch lineage (`launch_lineage`, same PID namespace,
    `_lineage_outside_attempt`), excludes a denied environment. Readable positive tags always win. ``host_complete`` is used only once the
    recorded namespaces are extinct (`attempt_process_quiescence`): then a
    complete walk by a host-like observer, which sees every namespace, is the
    one that may answer ``empty``.
    ``preserved`` excludes only already classified exact PID/start positives;
    the existing completeness and denied-environment checks still prove absence.
    Whole-attempt callers leave it empty.
    """

    attempt_id = metadata.get("attempt_id", "")
    if not attempt_id:
        return ProcessGroupObservation("unverifiable", reason="attempt-id-missing")
    scan = _PROCESS_TABLE_SCAN.get()
    if scan is not None:
        # Batch observer pass (`process_table_scan_scope`): same verdict, one
        # walk shared by every attempt of the pass instead of one per call.
        return _tagged_descendants_from_scan(
            scan, metadata, attempt_id, host_complete=host_complete, preserved=preserved
        )
    tag = f"{ATTEMPT_DESCENDANT_ENV}={attempt_id}".encode()
    # SD-OPEN-47 (H7-b): a child's liveness is its own process set. The
    # recorded parent leader (the owner's governed process) is an ancestor,
    # never a descendant, so it is excluded before any tag match -- but only
    # by the repository's process identity (pid + start ticks, same
    # namespace as the observer), never by pid number alone: a reused or
    # cross-namespace number is a different process (review finding 2).
    excluded_pids = _parent_leader_pids(metadata)
    members: list[tuple[int, str, str]] = []
    incomplete_reason = ""
    access_errors = []
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return ProcessGroupObservation(
            "unverifiable", reason=f"procfs-enumeration:{exc.errno or 'error'}"
        )
    for entry in entries:
        if not entry.name.isdigit():
            continue
        start = ""
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            tail = raw[raw.rfind(")") + 2 :].split()
            state, start = tail[0], tail[19]
            if state == "Z":
                continue
            environ = (entry / "environ").read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            if exc.errno in {errno.EACCES, errno.EPERM}:
                if int(entry.name) in excluded_pids:
                    continue
                stable_start, reason = _tag_access_observation(entry, start)
                if reason:
                    access_errors.append((stable_start, reason, _denied_process_cgroup(entry, stable_start),
                                          _denied_process_ancestry(entry, stable_start)))
                continue
            incomplete_reason = f"procfs-environ:{entry.name}:{exc.errno or 'error'}"
            continue
        except (IndexError, ValueError):
            incomplete_reason = f"procfs-member:{entry.name}:malformed"
            continue
        if int(entry.name) in excluded_pids:
            continue
        if tag in environ.split(b"\0") and (int(entry.name), start) not in preserved:
            members.append((int(entry.name), start, state))
    ordered = tuple(sorted(members, key=lambda member: member[0]))
    if ordered:
        return ProcessGroupObservation("populated", ordered, incomplete_reason)
    if incomplete_reason:
        return ProcessGroupObservation("unverifiable", (), incomplete_reason)
    if not _tag_scan_authority(metadata, host_complete):
        return ProcessGroupObservation(
            "unverifiable", (), "observer-namespace-mismatch"
        )
    access_reason = next((reason for start, reason, cgroup, ancestry in access_errors
                          if not _denied_process_outside_attempt(
                              start, metadata, host_complete=host_complete, cgroup=cgroup,
                              ancestry=ancestry)), '')
    if access_reason:
        return ProcessGroupObservation('unverifiable', (), access_reason)
    return ProcessGroupObservation("empty")


_ATTEMPT_RUNTIME_NAMES = {"claude": "claude", "codex": "codex", "opencode": "opencode"}
_ATTEMPT_INTERPRETERS = {"python", "node", "nodejs", "bun"}
_ATTEMPT_CARRIER_SCRIPTS = frozenset({
    "codex-managed-completion.py", "dispatch-attempt-ready.py",
    "dispatch-orphan-watch.py", "dispatch-progress.py", "dispatch-reap-watch.py",
    "dispatch-session-sweep.py", "dispatch-owner-rewake.py", "peer-steward.py",
})


def attempt_execution_observation(metadata: dict[str, str]) -> AttemptExecutionObservation:
    """Separate a surviving dispatch carrier from the attempt's runtime.

    The complete exact attempt tag census remains the authority for absence.
    Every positive identity is rechecked by PID and start time while its
    bounded command identity is read. Known carrier scripts count as helpers;
    an unclassified process, PID reuse, access failure, or partial census is
    unknown. The result is advisory input only and never narrows cleanup's
    process set.
    """

    attempt_id = metadata.get("attempt_id", "")
    harness = metadata.get("harness", "")
    if not attempt_id or harness not in _ATTEMPT_RUNTIME_NAMES:
        return AttemptExecutionObservation("unknown", "attempt-or-harness-unbound")
    tagged = attempt_tagged_descendants(metadata)
    if tagged.state == "unverifiable":
        return AttemptExecutionObservation("unknown", tagged.reason)
    if tagged.state == "empty":
        return AttemptExecutionObservation("absent", "attempt-tag-census-empty")

    active = []
    helpers = []
    unclassified = []
    uncertain = tagged.reason
    for pid, expected_start, _state in tagged.members:
        if process_start_ticks(pid) != expected_start:
            uncertain = uncertain or "attempt-process-identity-changed"
            continue
        proc = Path("/proc") / str(pid)
        try:
            raw = (proc / "cmdline").read_bytes()[:8192]
            comm = (proc / "comm").read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            uncertain = uncertain or "attempt-process-disappeared-during-observation"
            continue
        except OSError as exc:
            uncertain = uncertain or f"attempt-command-unreadable:{exc.errno or 'error'}"
            continue
        if process_start_ticks(pid) != expected_start:
            uncertain = uncertain or "attempt-process-identity-changed"
            continue
        argv = [os.fsdecode(part) for part in raw.rstrip(b"\0").split(b"\0") if part]
        if not argv:
            uncertain = uncertain or "attempt-command-empty"
            continue
        first = Path(argv[0]).name.lower()
        is_runtime = first == harness
        if (not is_runtime and first in _ATTEMPT_INTERPRETERS and len(argv) > 1):
            is_runtime = Path(argv[1]).name.lower() == harness
        if is_runtime or comm.lower() == harness:
            active.append((pid, expected_start, harness))
            continue
        script = next((Path(arg).name for arg in argv[1:3]
                       if Path(arg).name in _ATTEMPT_CARRIER_SCRIPTS), "")
        if script:
            helpers.append((pid, expected_start, script))
            continue
        unclassified.append((pid, expected_start,
                             "unclassified:" + (comm or first)[:96]))
        uncertain = uncertain or "attempt-process-role-unclassified"

    if active:
        return AttemptExecutionObservation("active", "runtime-command-live", tuple(active))
    if uncertain:
        return AttemptExecutionObservation("unknown", uncertain,
                                           tuple((*helpers, *unclassified)))
    if helpers:
        return AttemptExecutionObservation("absent", "carrier-only", tuple(helpers))
    return AttemptExecutionObservation("unknown", "attempt-execution-unclassified")


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


def _tagged_residue_receipt(metadata: dict[str, str]) -> bool:
    """Read historical residue diagnostics, without declaring those processes gone."""

    observer_namespace = metadata.get("pid_observer_ns", "")
    return bool(
        observer_namespace
        and metadata.get("pid_ns", "") == observer_namespace
        and metadata.get("launch_lifecycle") == "detached"
        and metadata.get("launch_outcome") == "governed-process-group-drained"
        and metadata.get("attempt_descendant_proof")
        == ATTEMPT_DESCENDANT_RESIDUE_PROOF
        and metadata.get("attempt_descendant_observer_ns") == observer_namespace
        and bool(metadata.get("attempt_descendant_residue"))
        and metadata.get("attempt_descendant_residue_basis")
        in {"registry-terminal", "terminal-envelope"}
    )


def tagged_residue_receipt(metadata: dict[str, str]) -> bool:
    """Public read-only view of ``_tagged_residue_receipt``."""

    return _tagged_residue_receipt(metadata)


_CANCELLATION_QUIESCENCE_BINDING_KEYS = (
    "attempt_id",
    "route_id",
    "route_hash",
    "route_node",
    "batch_route_id",
    "batch_route_node",
    "parallel_group",
    "parallel_leg_index",
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
    "launch_lifecycle",
    "launch_outcome",
    "group_reap_proof",
    "group_reap_pgid",
    "attempt_descendant_proof",
    "attempt_descendant_observer_ns",
)


def _canonical_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _cancellation_quiescence_binding_digest(metadata: dict[str, str]) -> str:
    return _canonical_sha256(
        {
            key: metadata.get(key, "")
            for key in _CANCELLATION_QUIESCENCE_BINDING_KEYS
        }
    )


def _portable_teardown_receipt_digest(metadata: dict[str, str]) -> str:
    """Digest the trusted watcher seal without depending on observer visibility."""

    return _canonical_sha256(
        {
            "receipt_type": NAMESPACE_PORTABLE_TEARDOWN_RECEIPT,
            "attempt_id": metadata.get("attempt_id", ""),
            "pid": metadata.get("pid", ""),
            "pid_start": metadata.get("pid_start", ""),
            "pgid": metadata.get("pgid", ""),
            "observer_namespace": metadata.get("pid_observer_ns", ""),
            "process_group_state": "empty",
            "process_group_proof": metadata.get("group_reap_proof", ""),
            "descendant_state": "empty",
            "descendant_proof": metadata.get("attempt_descendant_proof", ""),
        }
    )


def prove_attempt_quiescence(
    metadata: dict[str, str],
    *,
    max_wait_seconds: float = CANCELLATION_QUIESCENCE_MAX_WAIT_SECONDS,
    poll_seconds: float = CANCELLATION_QUIESCENCE_POLL_SECONDS,
    allow_namespace_extinct: bool = False,
) -> QuiescenceProof:
    """Prove exact cancellation quiescence with one explicitly bounded wait.

    The primary path requires a complete empty PGID observation and a complete
    empty attempt-tag scan from an authoritative namespace.  A trusted detached
    watcher receipt is the only portable alternative; a currently visible
    member in either set always wins over that older receipt.
    """

    attempt_id = metadata.get("attempt_id", "")
    raw_pid = metadata.get("pid", "")
    raw_pgid = metadata.get("pgid", "")
    binding_digest = _cancellation_quiescence_binding_digest(metadata)
    invalid_identity = bool(
        not attempt_id
        or not raw_pid.isdigit()
        or not metadata.get("pid_start")
        or not raw_pgid.isdigit()
        or raw_pgid != raw_pid
    )
    if invalid_identity:
        return QuiescenceProof(
            False,
            "cancellation-quiescence-unproven",
            attempt_id,
            "exact-teardown",
            int(raw_pgid) if raw_pgid.isdigit() else None,
            "unverifiable",
            "unverifiable",
            False,
            binding_digest,
        )
    if max_wait_seconds < 0 or poll_seconds <= 0:
        raise DispatchContractError("cancellation-quiescence-wait-invalid")

    pgid = int(raw_pgid)
    deadline = time.monotonic() + min(max_wait_seconds, 30.0)
    while True:
        group = process_group_observation(pgid)
        descendants = attempt_tagged_descendants(metadata)
        namespace_authority = attempt_scan_namespace_authority(metadata)
        portable = _detached_group_drain_receipt(metadata)

        if group.state == "empty" and descendants.state == "empty" and namespace_authority:
            return QuiescenceProof(
                True,
                "cancellation-quiescence-proven",
                attempt_id,
                "exact-teardown",
                pgid,
                "empty",
                "empty",
                True,
                binding_digest,
            )

        visible_live = group.state == "populated" or descendants.state == "populated"
        if not visible_live and portable:
            return QuiescenceProof(
                True,
                "cancellation-quiescence-proven",
                attempt_id,
                "authenticated-namespace-portable",
                pgid,
                "empty",
                "empty",
                True,
                binding_digest,
                _portable_teardown_receipt_digest(metadata),
            )

        if (
            group.state == "unverifiable"
            or descendants.state == "unverifiable"
            or not namespace_authority
            or time.monotonic() >= deadline
        ):
            if (
                allow_namespace_extinct
                and group.state == "empty"
                and (descendants.state == "empty" or (
                    descendants.state == "unverifiable"
                    and descendants.reason == "observer-namespace-mismatch"
                ))
                and observer_namespace_extinct(metadata) == "extinct"
            ):
                return QuiescenceProof(
                    True,
                    "cancellation-quiescence-proven",
                    attempt_id,
                    "namespace-extinct",
                    pgid,
                    "empty",
                    "empty",
                    True,
                    binding_digest,
                )
            return QuiescenceProof(
                False,
                "cancellation-quiescence-unproven",
                attempt_id,
                "exact-teardown",
                pgid,
                group.state,
                descendants.state,
                namespace_authority,
                binding_digest,
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def _artifact_proof_receipt(metadata: dict[str, str]) -> bool:
    """Read a historical output proof; it grants no process-cleanup authority."""

    raw_pid = metadata.get("pid", "")
    observer_namespace = metadata.get("pid_observer_ns", "")
    process_namespace = metadata.get("pid_ns", "")
    digest = metadata.get("artifact_proof_sha256", "")
    return bool(
        raw_pid.isdigit()
        and metadata.get("pid_start")
        and observer_namespace
        # The seal is only meaningful to an observer in the namespace that
        # recorded the PID: elsewhere "dead" was never provable in the first place.
        and process_namespace == observer_namespace
        and metadata.get("artifact_proof_observer_ns") == observer_namespace
        and metadata.get("post_exit_receipt_substitute") == ARTIFACT_PROOF_RECEIPT
        and metadata.get("artifact_proof_verdict") == "PASS"
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _post_exit_receipt_reason(metadata: dict[str, str]) -> str:
    if _foreground_reap_receipt(metadata):
        return "governed-process-group-reaped"
    if _detached_group_drain_receipt(metadata):
        return "governed-process-group-drained"
    return ""


def post_exit_receipt_reason(metadata: dict[str, str]) -> str:
    """Public view of which durable post-exit receipt this row already carries."""

    return _post_exit_receipt_reason(metadata)


def _cancellation_receipt_reason(metadata: dict[str, str]) -> str:
    """Whether this row already carries a sealed cancellation-quiescence receipt.

    Deliberately not merged into ``_post_exit_receipt_reason``: a cancellation
    receipt proves the *namespace* is gone, not that the governed process
    exited normally, and the SD-79 successor gate must never treat the two as
    interchangeable (see ``_post_exit_receipt_reason``'s own three receipts,
    unchanged).
    """

    digest = metadata.get("cancellation_receipt_digest", "")
    return (
        "cancellation-receipt-sealed"
        if (
            metadata.get("cancellation_quiescence_receipt")
            == ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT
            and digest.startswith("sha256:")
            and metadata.get("quiescence_pgid_proof") == GROUP_REAP_PROOF
            and metadata.get("quiescence_descendant_proof")
            == ATTEMPT_DESCENDANT_PROOF
        )
        else ""
    )


def cancellation_receipt_reason(metadata: dict[str, str]) -> str:
    """Public read-only view of ``_cancellation_receipt_reason``."""

    return _cancellation_receipt_reason(metadata)


CLEANUP_RECEIPT_TYPE = "attempt-cleanup-v1"


def _cleanup_receipt_reason(metadata: dict[str, str]) -> str:
    """Consume only exact cleanup proof; it is neither a verdict nor retry credit."""
    try:
        record = json.loads(base64.b64decode(metadata.get("cleanup_receipt_b64", ""), validate=True))
        if not isinstance(record, dict):
            return ""
        return "attempt-cleanup-proven" if (
            record.get("receipt_type") == CLEANUP_RECEIPT_TYPE
            and record.get("contract_version") == RECOVERY_CONTRACT_VERSION
            and record.get("attempt_id") == metadata.get("attempt_id")
            and record.get("route_id") == metadata.get("route_id", "")
            and record.get("route_hash") == metadata.get("route_hash", "")
            and record.get("node_or_group_leg") == (metadata.get("route_node", "") or metadata.get("batch_route_node", ""))
            and record.get("binding_digest") == _cancellation_quiescence_binding_digest(metadata)
            and _canonical_sha256(record) == metadata.get("cleanup_receipt_digest")
            and record.get("namespace_authority") is True
            and record.get("process_group", {}).get("state") == "empty"
            and record.get("process_group", {}).get("pgid") == metadata.get("pgid")
            and record.get("process_group", {}).get("proof") == GROUP_REAP_PROOF
            and record.get("attempt_tagged_descendants", {}).get("state") == "empty"
            and record.get("attempt_tagged_descendants", {}).get("attempt_id") == metadata.get("attempt_id")
            and record.get("attempt_tagged_descendants", {}).get("proof") == ATTEMPT_DESCENDANT_PROOF
            and record.get("proof_source") in {"exact-teardown", "authenticated-namespace-portable", "namespace-extinct"}
        ) else ""
    except (ValueError, TypeError, AttributeError):
        return ""


def _portable_cleanup_receipt(metadata: dict[str, str]) -> bool:
    """Reuse the existing exact portable seal, never a cached absence alone."""
    if not _cleanup_receipt_reason(metadata) or not _detached_group_drain_receipt(metadata):
        return False
    record = json.loads(base64.b64decode(metadata["cleanup_receipt_b64"], validate=True))
    return bool(
        record.get("proof_source") == "authenticated-namespace-portable"
        and record.get("portable_receipt_digest") == _portable_teardown_receipt_digest(metadata)
    )


def _settled_cleanup_receipt(metadata: dict[str, str]) -> bool:
    """A complete normal settlement or the existing authenticated watcher drain."""
    if not _cleanup_receipt_reason(metadata):
        return False
    record = json.loads(base64.b64decode(metadata["cleanup_receipt_b64"], validate=True))
    return record.get("proof_source") == "exact-teardown" or _portable_cleanup_receipt(metadata)


def _cleanup_observation_diagnostic(metadata, *, group=None, descendants=None):
    """Return observations for an existing guard result without publishing output.

    No proc environment or later re-observation is included. Diagnostics confer
    no authority and must never prevent the original refusal.
    """
    payload = {"attempt_id": metadata.get("attempt_id", ""),
               "binding_digest": _cancellation_quiescence_binding_digest(metadata),
               "observed_at": datetime.now(timezone.utc).isoformat(),
               "scope": "guard-time", "pid": metadata.get("pid", ""),
               "pid_start": metadata.get("pid_start", ""), "pgid": metadata.get("pgid", "")}
    for name, observation in (("process_group", group), ("attempt_tagged_descendants", descendants)):
        if observation is not None:
            payload[name] = {"state": observation.state, "reason": observation.reason[:240]}
    return payload


def _terminal_cleanup_values(metadata: dict[str, str]) -> dict[str, str]:
    """Preserve full cleanup at the existing first terminal write, without a gate.

    The caller holds the row lock and commits these exact-bound values together
    with the result and delivery identity. An unavailable proof adds no write.
    """
    if _cleanup_receipt_reason(metadata):
        return {}
    try:
        if attempt_governed_process_quiescence(metadata).state == "live":
            return {}
        proof = prove_attempt_quiescence(metadata, max_wait_seconds=0,
                                         allow_namespace_extinct=True)
        if not proof.proven:
            return {}
        record = _cancellation_quiescence_receipt_record(metadata, proof)
        record["receipt_type"] = CLEANUP_RECEIPT_TYPE
        return {"cleanup_receipt_b64": base64.b64encode(
                    json.dumps(record, sort_keys=True).encode()).decode(),
                "cleanup_receipt_digest": _canonical_sha256(record)}
    except (OSError, ValueError, DispatchContractError):
        return {}


def residue_live_pids(metadata: dict[str, str]) -> tuple[int, ...]:
    """Display-only: tagged survivors of a residue-sealed attempt that are still running."""
    if not _tagged_residue_receipt(metadata):
        return ()
    probe = attempt_tagged_descendants(metadata)
    return tuple(member[0] for member in probe.members) if probe.state == "populated" else ()


def terminal_receipt_incomplete(metadata, *, terminal_receipt=True):
    """The existing durable terminal gate for namespace-local registered workers."""
    return bool(terminal_receipt
                and metadata.get("registered_worker") == "1"
                and metadata.get("pid_scope") == "namespace-local"
                and not _post_exit_receipt_reason(metadata)
                and not _cancellation_receipt_reason(metadata)
                and not _cleanup_receipt_reason(metadata))


def attempt_process_quiescence(
    metadata: dict[str, str], *, terminal_receipt: bool = False
) -> ProcessQuiescence:
    """Classify the exact governed process, then prove no tagged descendant survives.

    A retained claim proving this host's past kernel settles the whole process
    tree before consulting potentially reused PIDs. Contradictory or foreign
    boot evidence stays unverifiable. Otherwise the leader/group verdict is
    post-processed on its way to ``quiescent``. That is the one verdict a false
    negative can turn into a duplicate launch, and it is also the rare one, so
    the ``/proc`` scan runs only at the moment quiescence is about to be
    declared and never on a hot path. ``live`` and ``unverifiable`` keep their
    previous meaning to the letter, with one exception: a receiptless row whose
    recorded namespaces are extinct on the host (`_namespace_extinct_quiescence`).
    """

    from resource_run_registry import (
        recorded_governor_identity, previous_boot_evidence, boot_identity, kernel_boot_id,
    )
    identity = recorded_governor_identity(metadata)
    if identity:
        if previous_boot_evidence(identity):
            return ProcessQuiescence("quiescent", "host-reboot")
        current_boot = boot_identity()
        # A local claim can legitimately contain only the kernel UUID (for
        # example inside a PID namespace without machine-id). Match that same
        # coordinate, then continue all normal process/receipt checks below.
        # A host-bound claim still requires the complete current host identity.
        if not current_boot and 'boot_host' not in identity:
            current_boot = {'boot_id': kernel_boot_id()}
        if not current_boot.get('boot_id') or (identity.get('boot_id') != current_boot['boot_id']
                or ('boot_host' in identity and identity['boot_host'] != current_boot['boot_host'])):
            return ProcessQuiescence("unverifiable", "process-boot-unverifiable")

    # A terminal namespace-local row can become visible before its wrapper has
    # finished publishing the portable post-exit receipt.  Local PID/group and
    # tagged-descendant observations prove that the process is quiet *here*,
    # but that evidence disappears with the namespace.  A namespace mismatch
    # can also make the governed process unverifiable from this observer.  Keep
    # either non-live result behind the same durable receipt gate; otherwise a
    # generic terminal fallback can resume the owner before the wrapper drains
    # and publishes its receipt.  Host-visible rows retain their established
    # behavior, and an actually live process remains live.
    result = _attempt_process_quiescence_impl(metadata)
    if result.state == "live":
        return result
    # D-1: a legacy row records no attempt id, so there is no tag to scan for.
    # Answering `unverifiable` for all of them would retroactively freeze every
    # successor, join, wait, and cleanup gate that reads an old row, so they
    # keep the verdict they already had instead.
    if not metadata.get("attempt_id"):
        if terminal_receipt_incomplete(metadata, terminal_receipt=terminal_receipt):
            return ProcessQuiescence("unverifiable", "post-exit-receipt-incomplete")
        return result
    probe = attempt_tagged_descendants(metadata)
    if probe.state == "populated":
        # Output evidence never settles a live process cleanup obligation.
        return ProcessQuiescence(
            "live", "attempt-descendant-live", probe.members[0][0]
        )
    extinct = _namespace_extinct_quiescence(metadata, result)
    if extinct is not None:
        # Nobody is left to publish a namespace-local receipt: the wrapper
        # died with the namespace. The receipt gate below never applies here.
        return extinct
    if terminal_receipt_incomplete(metadata, terminal_receipt=terminal_receipt):
        return ProcessQuiescence("unverifiable", "post-exit-receipt-incomplete")
    if result.state != "quiescent":
        # A non-quiescent leader verdict remains authoritative unless the
        # positive descendant branch above proves additional live evidence.
        return result
    if probe.state == "unverifiable":
        # Normal cleanup or the trusted watcher proved this exact attempt drained. A
        # later unrelated private environment must not undo that receipt and
        # make recovery reseal it forever. Current positive tags won above;
        # require the authoritative current group to remain empty as well.
        group = (process_group_observation(int(metadata["pgid"]))
                 if attempt_scan_namespace_authority(metadata)
                 and metadata.get("pgid", "").isdigit() else None)
        if group is not None and group.state == "populated":
            return ProcessQuiescence("live", "attempt-process-group-live", group.members[0][0] if group.members else None)
        if (terminal_receipt and _settled_cleanup_receipt(metadata)
                and attempt_scan_namespace_authority(metadata)
                and group is not None and group.state == "empty"):
            return result
        # SD-79/80/89: the observer that produced a complete post-exit receipt
        # may itself have disappeared before a successor or retry is launched.
        # Consume that receipt only at an exact terminal gate, and only for the
        # one unavailability it can explain. A visible tagged process already
        # returned above, while incomplete scans remain fail-closed.
        if (
            terminal_receipt
            and probe.reason == "observer-namespace-mismatch"
            and (_post_exit_receipt_reason(metadata) or (
                _cleanup_receipt_reason(metadata) and not _settled_cleanup_receipt(metadata)))
        ):
            return result
        return ProcessQuiescence("unverifiable", "attempt-descendant-unverifiable")
    return result


def _namespace_extinct_quiescence(
    metadata: dict[str, str], governed: ProcessQuiescence
) -> ProcessQuiescence | None:
    """Exact death of a row whose recorded namespaces have left the host.

    Only one branch is widened: a row with no authoritative PID and no receipt
    (``process-namespace-unverifiable``). It becomes ``quiescent`` when both
    recorded namespaces are extinct (`namespace_gone`) *and* a host-like
    observer's complete walk finds no process carrying the attempt tag; a
    tagged process found anywhere is ``live``. Any other answer -- a namespace
    still present, a sandboxed observer, an incomplete walk -- returns ``None``
    and the caller keeps its previous verdict to the letter.
    """

    if (
        governed.state != "unverifiable"
        or governed.reason != "process-namespace-unverifiable"
        or namespace_gone(metadata) != "extinct"
    ):
        return None
    probe = attempt_tagged_descendants(metadata, host_complete=True)
    if probe.state == "populated":
        return ProcessQuiescence(
            "live", "attempt-descendant-live", probe.members[0][0]
        )
    if probe.state == "empty":
        return ProcessQuiescence("quiescent", NAMESPACE_EXTINCT_REASON)
    return None


def attempt_governed_process_quiescence(
    metadata: dict[str, str],
) -> ProcessQuiescence:
    """Classify only the exact governed process, with no tagged-descendant probe.

    An operator surface that has to decide whether the *worker* died needs this
    verdict on its own: `attempt_process_quiescence` folds in the tagged-descendant
    scan, so a leaked tagged process reads back as `live` and hides the fact that
    the governed process itself is provably gone.
    """

    return _attempt_process_quiescence_impl(metadata)


def attempt_identity_lineage(metadata: Mapping[str, str]) -> set[tuple[str, str]]:
    """Return exact current and predecessor PID/start identities for one attempt."""
    identities: set[tuple[str, str]] = set()
    current_pid, current_start = str(metadata.get("pid", "")), str(metadata.get("pid_start", ""))
    if current_pid.isdigit() and current_start.isdigit():
        identities.add((current_pid, current_start))
    raw = metadata.get("owner_identity_lineage", "")
    if not isinstance(raw, str):
        return identities
    for entry in raw.split("+"):
        pid, separator, start = entry.partition(":")
        if separator and pid.isdigit() and start.isdigit():
            identities.add((pid, start))
    return identities


# The two notes only a pre-start, lock-held close wrote before it also recorded
# launch_outcome. Rows already on disk cannot be rewritten.
_UNSTARTED_SLICE_CLOSE_NOTES = frozenset({
    "subsession-batch-start-failed", "subsession-batch-registration-incomplete"})


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
        if (metadata.get("note") in _UNSTARTED_SLICE_CLOSE_NOTES and metadata.get("session_chain_id")
                and not launch_outcome and metadata.get("launch_claimed") == "0"
                and metadata.get("launch_started") != "1"
                and not _has_persisted_process_identity(metadata)):
            return ProcessQuiescence("quiescent", "never-launched")
        return ProcessQuiescence("unverifiable", "process-identity-missing")
    if not raw_pid.isdigit() or not metadata.get("pid_start"):
        return ProcessQuiescence("unverifiable", "process-identity-invalid")

    candidates = authoritative_process_identities(metadata)
    receipt_reason = _post_exit_receipt_reason(metadata) or _cleanup_receipt_reason(metadata)

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
    terminal_receipt_gate: bool = False,
) -> ObservedAttemptLiveness:
    """Combine registry state and exact process evidence without mutation.

    Consumers may supply only whether an exact final runtime envelope exists;
    its content remains private to the terminal classifier.  An open row whose
    governed process is gone is never synthesized back to alive.  It becomes a
    visible reconciliation obligation, whether or not the envelope survived.
    """

    process = attempt_process_quiescence(
        metadata,
        # Route rows intentionally stay open until their completion marker is
        # written.  The exact terminal envelope is therefore also a terminal
        # receipt gate: a namespace-local PID may disappear while its outer
        # wrapper is still publishing the durable reap receipt and marker.
        terminal_receipt=(
            status in {"done", "killed", "cancelled"}
            or terminal_envelope
            or terminal_receipt_gate
        ),
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
