#!/usr/bin/env python3
"""Portable SD-48/49 primitives used by headless dispatch adapters."""

from __future__ import annotations

import base64
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

from dispatch_pending_delivery import RECIPIENT_KINDS
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
    "launch_lifecycle_override",
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
    "post_exit_receipt_substitute",
    "artifact_proof_sha256",
    "artifact_proof_observer_ns",
    "artifact_proof_verdict",
    "artifact_proof_sealed_at",
    "recovery_id",
    "retry_ordinal",
    "retry_attempt_id",
    "retry_claimed_at",
    "start_permitted",
    "cancellation_quiescence_receipt",
    "cancellation_receipt_digest",
    "quiescence_pgid_proof",
    "quiescence_descendant_proof",
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
    "receipt_state",
    "marker_state",
    # SD-111 P2 (D-4): the delivery-intent 8-field allowlist. Stamped once by
    # `_delivery_intent_values()` at the one `open|running -> done` edge a row
    # actually takes (W1-W4); immutable afterward (§4.4) -- a later
    # `_updated_attempt_metadata` call with a *different* value for any of
    # these raises `attempt-immutable-metadata-mutation` the same as any
    # other terminal-evidence key.
    "delivery_intent",
    "delivery_id",
    "delivery_recipient_kind",
    "delivery_recipient_digest",
    "delivery_receipt_digest",
    "delivery_row_revision",
    "delivery_intent_at_ns",
    "delivery_receipt_b64",
    "delivery_persistence_refused",
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
RECOVERY_CONTRACT_VERSION = 1
ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT = (
    "attempt-cancellation-quiescence-receipt-v1"
)
NAMESPACE_PORTABLE_TEARDOWN_RECEIPT = "namespace-portable-teardown-receipt-v1"
AUTOMATIC_RECEIPTLESS_CLASSIFIER = "automatic-receipt-unavailable-v1"
CANCELLATION_QUIESCENCE_MAX_WAIT_SECONDS = 2.0
CANCELLATION_QUIESCENCE_POLL_SECONDS = 0.05
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
    "batch_leg_class",
    "batch_auxiliary_check",
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
        ]
    )
    for candidate in candidates:
        if _valid(candidate):
            return Path(candidate)
    # No candidate is marked: converge on utilities/agent-home.sh's final
    # fallback (the managed-release default path, unvalidated) so the two
    # resolver chains cannot silently diverge in a bare environment (review
    # F-4). ~/.claude is Claude Code's config dir, not a harness root, since
    # managed releases replaced the ~/.claude-as-AGENT_HOME layout (2026-08-25).
    # _MODULE_ROOT stays only for the pathological case where even $HOME is
    # undefined.
    try:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(xdg) / "hearting" / "current"
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


# SD-113 A47-2: the vocabulary `_delivery_intent_values()` recognizes as a
# record-creating `parent_completion_delivery` stamp must equal
# `dispatch_pending_delivery.RECIPIENT_KINDS` exactly -- documented here so a
# future edit to either set is caught at import time instead of silently
# drifting one guard's `{}` no-op out of sync with the other's acceptance.
_KNOWN_DELIVERY_RECIPIENT_KINDS = frozenset({
    "claude-parent-runtime", "codex-stop-hook", "codex-managed-gateway", "opencode-turn",
})
if _KNOWN_DELIVERY_RECIPIENT_KINDS != frozenset(RECIPIENT_KINDS):
    raise DispatchContractError(
        "delivery-intent-vocabulary-drift",
        f"expected={sorted(_KNOWN_DELIVERY_RECIPIENT_KINDS)} "
        f"actual={sorted(RECIPIENT_KINDS)}",
    )


def _recovery_identity_digest(identity: dict[str, str]) -> str:
    if any(not value for value in identity.values()):
        raise DispatchContractError("recovery-identity-incomplete")
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "rec-" + hashlib.sha256(encoded).hexdigest()


def recovery_id(
    *,
    source_route_id: str,
    source_route_hash: str,
    node_or_group_leg: str,
    original_attempt_id: str,
    cancellation_receipt_digest: str,
) -> str:
    """Return the stable identity for one receipt-unavailable recovery."""

    return _recovery_identity_digest(
        {
            "source_route_id": source_route_id,
            "source_route_hash": source_route_hash,
            "node_or_group_leg": node_or_group_leg,
            "original_attempt_id": original_attempt_id,
            "cancellation_receipt_digest": cancellation_receipt_digest,
        }
    )


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
class MarkerBoundDeliveryResult:
    """One exact marker/row/owned-child snapshot from the jobs lock."""

    marker: dict[str, object] | None
    marker_digest: str
    row_revision: str
    row_digest: str
    status: str
    verdict: str
    quiescent: bool
    owned_children: int
    advanced: bool
    supervisor_terminal: bool = False


@dataclass(frozen=True)
class MarkerBoundCompletionProof:
    """Full immutable marker-chain proof prepared before the jobs lock."""

    marker: dict[str, object]
    marker_path: Path
    marker_digest: str
    route_id: str
    route_hash: str
    node_id: str
    attempt_id: str
    immutable_file_digests: tuple[tuple[str, str], ...]


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
class QuiescenceProof:
    """Attempt-bound evidence that both cancellation process sets are empty."""

    proven: bool
    reason: str
    attempt_id: str
    source: str
    pgid: int | None
    process_group_state: str
    descendant_state: str
    namespace_authority: bool
    binding_digest: str
    portable_receipt_digest: str = ""


@dataclass(frozen=True)
class RecoveryRetryClaim:
    """Durable one-retry admission result for one recovery identity."""

    recovery_id: str
    original_attempt_id: str
    retry_ordinal: int
    retry_attempt_id: str
    state: str
    reason: str
    start_permitted: bool


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


class ParentExtinctionEvidence(NamedTuple):
    """Bounded, read-only proof that a foreground child lost its owner."""

    state: str
    reason: str
    parent_attempt_id: str = ""


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
            or Path(sibling_worktree).expanduser().resolve(strict=False)
            != Path(parent_worktree).expanduser().resolve(strict=False)
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
    try:
        current = os.readlink("/proc/self/ns/pid")
    except OSError:
        return "unverifiable"
    if not current:
        return "unverifiable"
    if not _current_observer_is_host_like():
        return "unverifiable"
    if current == recorded_observer:
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
        except OSError:
            return "unverifiable"
        if candidate == recorded_observer:
            return "present"
    return "extinct"


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
# Operator-sealed substitute for a post-exit receipt that can never be issued.
# `dispatch-registry.py reconcile --seal-artifact-proof-receipt` writes it only
# after re-deriving the whole evidence chain; nothing issues it automatically.
ARTIFACT_PROOF_RECEIPT = "artifact-proof-v1"

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
                and descendants.state == "empty"
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
    """Was an artifact-proof substitute sealed for this exact attempt and observer?

    A detached drain receipt needs `attempt-tagged-empty-v1`, so a single process
    that escaped the governed group while carrying the attempt tag makes the
    receipt unissuable forever -- and every gate that consumes it (`reconcile`,
    `dispatch_completion_join`) then has no terminal state to reach, even for a
    worker that finished and wrote its artifact.  This substitute is the recorded
    proof that the worker's own last `stage-heartbeat --phase artifact` digest
    matches the artifact on disk, so its output was already final when the
    governed process died.  `dispatch-registry.py reconcile
    --seal-artifact-proof-receipt` is the only writer, and it re-derives the whole
    chain before and after the write; this predicate only reads the seal back.
    """

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
    if _artifact_proof_receipt(metadata):
        return "receipt-superseded-by-artifact-proof"
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
        if (
            terminal_receipt
            and metadata.get("registered_worker") == "1"
            and metadata.get("pid_scope") == "namespace-local"
            and not _post_exit_receipt_reason(metadata)
            and not _cancellation_receipt_reason(metadata)
        ):
            return ProcessQuiescence("unverifiable", "post-exit-receipt-incomplete")
        return result
    probe = attempt_tagged_descendants(metadata)
    if probe.state == "populated":
        # A visible tagged process normally vetoes quiescence, because it may
        # still be writing this attempt's output. A sealed artifact proof settles
        # exactly that question the other way: the artifact on disk already
        # matches the digest the worker itself recorded at its final artifact
        # heartbeat, and the governed process is gone (checked above), so the
        # survivor is leaked residue rather than the worker. Only at a terminal
        # gate, and only with the operator-sealed proof -- an unsealed row keeps
        # the veto.
        if terminal_receipt and _artifact_proof_receipt(metadata):
            return result
        return ProcessQuiescence(
            "live", "attempt-descendant-live", probe.members[0][0]
        )
    if (
        terminal_receipt
        and metadata.get("registered_worker") == "1"
        and metadata.get("pid_scope") == "namespace-local"
        and not _post_exit_receipt_reason(metadata)
        and not _cancellation_receipt_reason(metadata)
    ):
        return ProcessQuiescence("unverifiable", "post-exit-receipt-incomplete")
    if result.state != "quiescent":
        # A non-quiescent leader verdict remains authoritative unless the
        # positive descendant branch above proves additional live evidence.
        return result
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


RESERVATION_CLAIM_TIMEOUT_DEFAULT = 60.0
RESERVATION_CLAIM_TIMEOUT_ENV = "AGENT_DISPATCH_RESERVATION_CLAIM_TIMEOUT"


def reservation_claim_timeout() -> float:
    """Claim-wait budget: env override clamped to [1, 600], else the default.

    A route-bound launch runs launch-fence route verification before the
    governed runner claims its reservation, and that verification is
    storage-latency-bound (an 18s wall-clock verify was measured on an
    NFS-backed artifact root). The budget must absorb that pre-claim work; a
    dead child still fails immediately regardless of this value because the
    wait loop exits as soon as the spawned process is gone.
    """

    raw = os.environ.get(RESERVATION_CLAIM_TIMEOUT_ENV, "").strip()
    if not raw:
        return RESERVATION_CLAIM_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return RESERVATION_CLAIM_TIMEOUT_DEFAULT
    return min(max(value, 1.0), 600.0)


def wait_governor_reservation_claim(
    governor: Path,
    root: Path,
    token: str,
    proc: subprocess.Popen,
    *,
    timeout: float | None = None,
    expected_reservation: dict[str, object] | None = None,
) -> dict[str, object]:
    """Observe reserve→runner transfer before the reserving process may exit."""

    if timeout is None:
        timeout = reservation_claim_timeout()
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


def state_root(environ: dict[str, str] | os._Environ[str]) -> Path:
    """The one per-user state root: `HARNESS_STATE_ROOT` (installer-owned) or
    `$XDG_STATE_HOME/hearting` or `$HOME/.local/state/hearting`, read only from
    the passed mapping so an isolation test's `environ={}` never touches the
    live process environment or creates a real user home (SD-112 §13.33.2-(1))."""

    harness_state_root = environ.get("HARNESS_STATE_ROOT")
    if harness_state_root:
        return _absolute(harness_state_root, "harness-state-root")
    xdg_state_home = environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        return _absolute(xdg_state_home, "xdg-state-home") / "hearting"
    home = environ.get("HOME")
    if not home:
        raise DispatchContractError(
            "dispatch-state-root-unresolved",
            "none of HARNESS_STATE_ROOT, XDG_STATE_HOME, HOME are set",
        )
    return _absolute(home, "home") / ".local" / "state" / "hearting"


def stable_state_root(environ: dict[str, str] | os._Environ[str]) -> Path:
    """The canonical release-independent dispatch state root (SD-112
    §13.33.2-(1)): `state_root(environ) / "dispatch"`."""

    return state_root(environ) / "dispatch"


def _fallback_registry(
    agent_home: Path, environ: dict[str, str] | os._Environ[str]
) -> Path:
    home = Path(agent_home).expanduser()
    resolved_home = home.resolve(strict=False)
    layout, runtime_home = _versioned_source_layout(resolved_home)
    if layout == "bundle":
        assert runtime_home is not None
        return (runtime_home / ".harness" / "dispatch" / "jobs.log").resolve(
            strict=False
        )
    # State-root chain (3) supersession (SD-112 §13.33.2-(8)): a shared managed
    # release and a maintainer checkout both default to the stable per-user
    # root now. Release succession moves file *contents*, not the `jobs_path`
    # *identity* that `revalidate_launch_compatibility` seals, which is exactly
    # the failure mode this migration closes. A checkout-local `.dispatch` is
    # reachable only through an explicit chain ①/② override (`explicit_jobs` or
    # `AGENT_DISPATCH_JOBS`), resolved by callers before they ever reach this
    # fallback, or as a legacy read candidate in `dispatch_state_roots()`.
    return stable_state_root(environ) / "jobs.log"


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
    fallback = _fallback_registry(agent_home, env).resolve(strict=False)
    source = "agent-home"
    if fallback.parent.name == "dispatch":
        try:
            is_stable = fallback.parent == stable_state_root(env)
        except DispatchContractError:
            is_stable = False
        source = "stable-state-root" if is_stable else "activation-runtime"
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
    return _fallback_registry(agent_home, env).parent


def dispatch_state_roots(
    agent_home: Path,
    jobs: str | Path | None = None,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[Path, ...]:
    """Read order for dispatch state (SD-112 §13.33.2-(6)): the canonical
    write root the resolver actually chose, then the stable per-user root (a
    no-op once the two coincide, which is the common case now that chain-3
    defaults to stable), then the legacy agent-home/active-release-relative
    tree. Deduplicated, read-only past index 0 -- the writer uses only
    `dispatch_state_roots(...)[0]`, which is always
    `resolve_dispatch_state_root()`'s result and never a hardcoded stable
    literal; stable-first is a *consequence* of that resolution, not an
    override applied here.
    """

    env = os.environ if environ is None else environ
    canonical = resolve_dispatch_state_root(agent_home, explicit_jobs=jobs, environ=env)
    candidates = [canonical]
    try:
        stable = stable_state_root(env)
    except DispatchContractError:
        stable = None
    if stable is not None and stable not in candidates:
        candidates.append(stable)
    legacy = Path(agent_home).expanduser().resolve(strict=False) / ".dispatch"
    if legacy not in candidates:
        candidates.append(legacy)
    return tuple(candidates)


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _ensure_new_directory_mode(path: Path, mode: int) -> None:
    """Force `mode` only when `path` doesn't exist yet. An existing symlink,
    non-directory, or mode wider than `mode` is a typed refusal -- never a
    chmod of something this process didn't just create (SD-112 §13.33.2-(7)).
    """

    if path.is_symlink():
        raise DispatchContractError(
            "dispatch-state-root-unwritable", f"{path}: refusing symlink"
        )
    if path.exists():
        if not path.is_dir():
            raise DispatchContractError(
                "dispatch-state-root-unwritable", f"{path}: not a directory"
            )
        existing = _mode_bits(path)
        if existing != mode:
            raise DispatchContractError(
                "dispatch-state-root-mode-violation",
                f"{path}: mode={oct(existing)} expected={oct(mode)}",
            )
        return
    path.mkdir(parents=True, exist_ok=False)
    os.chmod(path, mode)  # mkdir's `mode=` is masked by umask; force exact bits.


def _is_stable_dispatch_root(dispatch_root: Path) -> bool:
    """Only the stable per-user dispatch root is mode-enforced by
    `ensure_global_registry_writable` (SD-112 §13.33.2-(7)); every other
    dispatch root keeps its historical, mode-agnostic creation path. Compared
    lexically (`.absolute()`, not `.resolve()`): a symlinked stable root must
    still compare equal so the symlink refusal in `_ensure_new_directory_mode`
    actually fires instead of silently falling through as "not stable"."""

    try:
        stable = stable_state_root(os.environ)
    except DispatchContractError:
        return False
    return Path(dispatch_root).expanduser().absolute() == stable


def ensure_global_registry_writable(path: Path) -> None:
    """Open the global registry and its lock before any child spawn.

    Mode enforcement (`0700` on first creation, typed refusal -- never a
    chmod -- if the root already exists with a different mode, is a symlink,
    or is not a directory) applies only when `path.parent` *is* the stable
    per-user dispatch root (SD-112 §13.33.2-(7)). Every other dispatch root --
    Codex bundle, shared-release/checkout legacy, or any fixture-owned tree --
    keeps today's plain `mkdir(parents=True, exist_ok=True)` with no mode
    check: countless existing fixtures across this suite create or copy those
    directories with an ordinary umask-derived mode (commonly `0o775`, e.g.
    release-succession's `shutil.copytree`), and retrofitting strict mode
    equality onto every one of those call sites is out of this slice's fence.
    `jobs.log` itself stays append-only and mode-agnostic for the same reason.
    `state_root()`'s own ancestry (e.g. `~/.local/state`,
    `~/.local/state/hearting`) is never touched by this function.
    """

    try:
        dispatch_root = path.parent
        dispatch_root.parent.mkdir(parents=True, exist_ok=True)
        if _is_stable_dispatch_root(dispatch_root):
            _ensure_new_directory_mode(dispatch_root, 0o700)
        else:
            dispatch_root.mkdir(parents=True, exist_ok=True)
        lock_path = Path(f"{path}.lock")
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with path.open("a", encoding="utf-8") as registry:
                registry.flush()
                os.fsync(registry.fileno())
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise DispatchContractError("global-registry-unwritable", f"{path}: {exc}") from exc


def _ensure_new_file_mode(path: Path, mode: int) -> None:
    """Force `mode` only when `path` doesn't exist yet, mirroring
    `_ensure_new_directory_mode` for the migration journal and other stable
    per-user files (SD-112 §13.33.2-(7)). Never chmods an existing file."""

    if path.is_symlink():
        raise DispatchContractError(
            "dispatch-state-root-unwritable", f"{path}: refusing symlink"
        )
    if path.exists():
        if not path.is_file():
            raise DispatchContractError(
                "dispatch-state-root-unwritable", f"{path}: not a regular file"
            )
        existing = _mode_bits(path)
        if existing != mode:
            raise DispatchContractError(
                "dispatch-state-root-mode-violation",
                f"{path}: mode={oct(existing)} expected={oct(mode)}",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.close(fd)


MIGRATION_ALIAS_RECORD_VERSION = 1
MIGRATION_JOURNAL_FILENAME = "migration-journal.jsonl"


def migration_journal_path(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> Path:
    """The one canonical migration-alias journal location: fixed under the
    stable per-user root, never derived from a legacy route path (decision 1
    -- putting it under the legacy path it aliases *away from* would make
    finding the journal depend on the alias it is supposed to resolve)."""

    env = os.environ if environ is None else environ
    return stable_state_root(env) / MIGRATION_JOURNAL_FILENAME


def read_migration_journal(stable_root: Path) -> list[dict]:
    """Best-effort parse of the append-only migration-alias journal. A
    malformed line is skipped, not raised -- the journal is evidence consumed
    by a fail-closed validator, not a strict schema gate on its own."""

    path = Path(stable_root) / MIGRATION_JOURNAL_FILENAME
    if not path.is_file():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


MIGRATION_ALIAS_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


def _alias_digest_well_formed(value: object) -> bool:
    """`sha256:` + 64 lowercase hex, nothing else.

    Presence alone is not evidence: a record carrying `"content_digest": "x"`
    is as structurally "complete" as a real one, so a truthiness check lets a
    hand-written journal line relieve a sealed `jobs_path` mismatch. The
    recorded digests cannot be recomputed later -- the legacy registry is
    pruned by the migration that wrote the record, and the stable registry
    keeps appending rows afterwards, so its content digest legitimately
    diverges the moment the next attempt registers. Shape is therefore the
    strongest check available on the digest fields themselves; liveness of the
    target is checked separately, against the filesystem, in
    `resolve_dangling_registry`.
    """

    return isinstance(value, str) and MIGRATION_ALIAS_DIGEST_PATTERN.fullmatch(value) is not None


def _alias_record_valid(record: dict) -> bool:
    """Structural validity of one `completed` migration-alias record.
    Forged or partially-written records (missing, malformed, or
    non-absolute identities) never pass, so `resolve_completed_alias` stays
    fail-closed by construction."""

    if record.get("record_version") != MIGRATION_ALIAS_RECORD_VERSION:
        return False
    if record.get("status") != "completed":
        return False
    legacy = record.get("legacy_jobs_identity")
    target = record.get("stable_jobs_identity")
    if not isinstance(legacy, dict) or not isinstance(target, dict):
        return False
    for identity in (legacy, target):
        path = identity.get("path")
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            return False
        if not _alias_digest_well_formed(identity.get("content_digest")):
            return False
    if not _alias_digest_well_formed(record.get("source_digest")):
        return False
    if not _alias_digest_well_formed(record.get("target_digest")):
        return False
    # Optional by contract (SD-112 §13.33.2-(3): verified only "when present"),
    # but a present-and-malformed value is a broken record, not an absent one --
    # accepting it would let a forgery opt out of the extra verification simply
    # by writing garbage into the field.
    route_hash = record.get("route_hash")
    if route_hash is not None and not _alias_digest_well_formed(route_hash):
        return False
    return True


def resolve_completed_alias(stable_root: Path, legacy_jobs_path: str | Path) -> dict | None:
    """The most recent structurally-valid `completed` alias record whose
    `legacy_jobs_identity.path` matches `legacy_jobs_path`, or `None`. Route
    hash / attempt link, when present on the record, are returned for the
    caller to verify too (decision 1); this function only proves the record
    is a well-formed completed alias, not that any particular route may use
    it."""

    target_path = str(Path(legacy_jobs_path).expanduser().resolve(strict=False))
    match = None
    for record in read_migration_journal(stable_root):
        if not _alias_record_valid(record):
            continue
        if record["legacy_jobs_identity"]["path"] != target_path:
            continue
        match = record  # append-only journal; the latest completed record wins
    return match


class DanglingRegistryResolution(NamedTuple):
    status: str  # "exact" | "aliased" | "compat-window" | "unresolved"
    jobs_path: Path | None
    alias_record: dict | None


def resolve_dangling_registry(
    sealed_jobs: str | Path,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
    window_open: bool = True,
) -> DanglingRegistryResolution:
    """Classify one absolute sealed `jobs.log` path against current
    filesystem/journal state (SD-112 §13.33.2-(3)/(6), decision 4).
    `capability-route.py`'s continuation resolver and (Slice 2) Fleet's row
    mapping share this one judgement instead of deriving their own.

    exact          sealed_jobs's parent directory still exists -- use it
                    directly, no alias/window involved.
    aliased        parent is gone, but the stable migration journal has a
                    structurally-valid `completed` record mapping sealed_jobs
                    to a live stable jobs.log. Checked before compat-window
                    (decision 1): an unvalidated path substitution must never
                    look like the normal path.
    compat-window  parent is gone, no alias, and the legacy read window is
                    still open. This cycle never closes the window, so
                    `window_open` defaults `True`; a future cycle can pass
                    the real judgement once `legacy_read_window_may_close()`
                    is wired to an authoritative count.
    unresolved     parent is gone, no alias, and the window is closed.
    """

    sealed = Path(sealed_jobs).expanduser().resolve(strict=False)
    if sealed.parent.is_dir():
        return DanglingRegistryResolution("exact", sealed, None)
    env = os.environ if environ is None else environ
    try:
        stable_root = stable_state_root(env)
    except DispatchContractError:
        stable_root = None
    if stable_root is not None:
        record = resolve_completed_alias(stable_root, sealed)
        if record is not None:
            target = Path(record["stable_jobs_identity"]["path"])
            # The docstring's promise is a *live* stable jobs.log, so require
            # the file itself. A present parent directory proves nothing about
            # the alias: a stale or forged record naming any existing
            # directory would otherwise resurrect a registry that is not
            # there, and the caller would bind a route to it. Without the file
            # this is not an alias -- fall through to the compat window.
            if target.is_file():
                return DanglingRegistryResolution("aliased", target, record)
    if window_open:
        return DanglingRegistryResolution("compat-window", None, None)
    return DanglingRegistryResolution("unresolved", None, None)


LEGACY_READ_WINDOW_MIN_SUPPORTED_RELEASES = 2


def legacy_read_window_may_close(
    *,
    supported_releases_elapsed: int,
    legacy_bound_open_writers: int,
    delta: int,
    legacy_read_hits: int,
) -> bool:
    """Pure four-condition truth table (SD-112 §13.33.2-(6), B-13a). Never
    touches the filesystem and never calls `_succeed_dispatch_state()`; actual
    post-promotion delta reachability is Slice 3's B-13b, not this slice's."""

    return (
        supported_releases_elapsed >= LEGACY_READ_WINDOW_MIN_SUPPORTED_RELEASES
        and legacy_bound_open_writers == 0
        and delta == 0
        and legacy_read_hits == 0
    )


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
    _auxiliary_arbitration_gate(route, node, agent_home, jobs)
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


_ROUTE_MODULE: object | None = None


def _route_module():
    """Load `capability-route.py` lazily.

    The dependency has to stay one-way at import time -- capability-route
    imports this module at module scope -- so it is resolved on first call,
    when this module is fully initialized. There is exactly one implementation
    of the arbiter-resolution rule and both the writer and this gate read it.
    """
    global _ROUTE_MODULE
    if _ROUTE_MODULE is None:
        import importlib.util

        path = Path(__file__).resolve().parent / "capability-route.py"
        spec = importlib.util.spec_from_file_location("capability_route_gate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ROUTE_MODULE = module
    return _ROUTE_MODULE


def _auxiliary_arbitration_gate(
    route: dict[str, object],
    node: dict[str, object],
    agent_home: Path,
    jobs: Path | None,
) -> None:
    """G1/AC 5: no node starts over an unarbitrated owner-merge group.

    A predecessor that is a member of an auxiliary-bearing group whose arbiter
    is the owner's merge record carries a second obligation beyond its own
    completion marker: the owner must have registered the merge record with
    `capability-route.py arbitrate` after the group joined. Without this the
    auxiliary findings are advisory output nobody is required to have read, and
    `autopilot-code`'s `execute` would start on an unmerged `plan-check`.
    """
    dependencies = [dep for dep in node.get("depends_on", [])]
    if not dependencies:
        return
    route_module = _route_module()
    owner_merge = route_module.owner_merge_auxiliary_groups(route)
    if not owner_merge:
        return
    dependency_groups = {
        row.get("parallel_group")
        for row in route.get("nodes", [])
        if isinstance(row, dict)
        and row.get("id") in dependencies
        and row.get("parallel_group")
    }
    unarbitrated = []
    unresolved = []
    for group_id, error in sorted(owner_merge.items()):
        if group_id not in dependency_groups:
            continue
        if error is not None:
            # M3: a route-integrity failure is a different event from "the owner
            # has not merged yet", and `arbitrate` cannot resolve it -- it raises
            # at the same point with the same error. Naming it
            # `auxiliary-arbitration-missing` sent the operator to a command that
            # cannot help. `terminal_gate_observation` already calls this state
            # `auxiliary-arbiter-unresolved`; the two consumers now agree.
            unresolved.append(f"{group_id}:{error}")
            continue
        basename = Path(
            str(route_module.arbitration_path(route["route_id"], group_id))
        ).name
        found = next(
            (
                candidate
                for candidate in (
                    root / "completion" / route["route_id"] / basename
                    for root in dispatch_state_roots(agent_home, jobs)
                )
                if candidate.is_file()
            ),
            None,
        )
        if found is None:
            # Absent under the handed root means refused. `_arbitration_observation`
            # falls back to `arbitration_path()`, which re-resolves the state root
            # from the environment, so passing `path=None` here would open the
            # spawn on a record this gate was never handed -- fail-open, and the
            # exact opposite of the one-root discipline this call site exists to
            # keep. `agent_home`/`jobs` are explicit arguments precisely so the
            # writer and every reader are structurally forced to agree on one
            # root, and `dispatch_state_root_rotation` makes rotation real rather
            # than theoretical.
            unarbitrated.append(group_id)
            continue
        row = route_module._arbitration_observation(
            route, group_id, error, path=found
        )
        if not row["passed"]:
            unarbitrated.append(group_id)
    if unresolved:
        raise DispatchContractError(
            "auxiliary-arbiter-unresolved", ",".join(unresolved)
        )
    if unarbitrated:
        raise DispatchContractError(
            "auxiliary-arbitration-missing", ",".join(unarbitrated)
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


def _cancellation_quiescence_receipt_record(
    metadata: dict[str, str], proof: QuiescenceProof
) -> dict[str, object]:
    return {
        "contract_version": RECOVERY_CONTRACT_VERSION,
        "receipt_type": ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
        "attempt_id": proof.attempt_id,
        "route_id": metadata.get("route_id", ""),
        "route_hash": metadata.get("route_hash", ""),
        "node_or_group_leg": metadata.get("route_node", "")
        or metadata.get("batch_route_node", ""),
        "proof_source": proof.source,
        "binding_digest": proof.binding_digest,
        "process_group": {
            "pgid": str(proof.pgid or ""),
            "state": "empty",
            "proof": GROUP_REAP_PROOF,
        },
        "attempt_tagged_descendants": {
            "attempt_id": proof.attempt_id,
            "state": "empty",
            "proof": ATTEMPT_DESCENDANT_PROOF,
        },
        "namespace_authority": proof.namespace_authority,
        "portable_receipt_digest": proof.portable_receipt_digest,
    }


def seal_cancellation_quiescence_receipt(
    jobs: Path, attempt_id: str, proof: QuiescenceProof
) -> str:
    """Atomically seal one cancellation-only receipt and return its digest."""

    if (
        not proof.proven
        or proof.reason != "cancellation-quiescence-proven"
        or proof.attempt_id != attempt_id
        or proof.process_group_state != "empty"
        or proof.descendant_state != "empty"
        or not proof.namespace_authority
    ):
        raise DispatchContractError("cancellation-quiescence-unproven")
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
                "attempt-row-not-unique", f"attempt_id={attempt_id} rows={len(matches)}"
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)

        if proof.binding_digest != _cancellation_quiescence_binding_digest(metadata):
            raise DispatchContractError("cancellation-quiescence-proof-drift")
        current_pgid = metadata.get("pgid", "")
        if not current_pgid.isdigit() or proof.pgid != int(current_pgid):
            raise DispatchContractError("cancellation-quiescence-proof-drift")
        if proof.source == "authenticated-namespace-portable":
            if (
                not _detached_group_drain_receipt(metadata)
                or proof.portable_receipt_digest
                != _portable_teardown_receipt_digest(metadata)
            ):
                raise DispatchContractError("cancellation-portable-receipt-invalid")
        elif proof.source == "namespace-extinct":
            if proof.portable_receipt_digest:
                raise DispatchContractError("cancellation-quiescence-proof-source-invalid")
        elif proof.source != "exact-teardown" or proof.portable_receipt_digest:
            raise DispatchContractError("cancellation-quiescence-proof-source-invalid")

        receipt_digest = _canonical_sha256(
            _cancellation_quiescence_receipt_record(metadata, proof)
        )
        existing_type = metadata.get("cancellation_quiescence_receipt", "")
        existing_digest = metadata.get("cancellation_receipt_digest", "")
        if existing_type or existing_digest:
            if (
                existing_type == ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT
                and existing_digest == receipt_digest
                and metadata.get("quiescence_pgid_proof") == GROUP_REAP_PROOF
                and metadata.get("quiescence_descendant_proof")
                == ATTEMPT_DESCENDANT_PROOF
            ):
                return existing_digest
            raise DispatchContractError("cancellation-quiescence-receipt-conflict")
        fields[5] = _updated_attempt_metadata(
            fields[5],
            {
                "cancellation_quiescence_receipt":
                    ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
                "cancellation_receipt_digest": receipt_digest,
                "quiescence_pgid_proof": GROUP_REAP_PROOF,
                "quiescence_descendant_proof": ATTEMPT_DESCENDANT_PROOF,
            },
        )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return receipt_digest


def _stable_recovery_attempt_id(recovery_identity: str) -> str:
    encoded = f"{recovery_identity}\0retry_ordinal=1".encode("utf-8")
    return "att-retry-" + hashlib.sha256(encoded).hexdigest()[:32]


def _existing_recovery_claim(
    metadata: dict[str, str], original_attempt_id: str
) -> RecoveryRetryClaim:
    recovery_identity = metadata.get("recovery_id", "")
    if metadata.get("attempt_id") != original_attempt_id:
        raise DispatchContractError("recovery-claim-original-attempt-mismatch")
    if metadata.get("note") == "receipt-unavailable-retry-exhausted":
        retry_attempt_id = metadata.get("retry_attempt_id", "")
        retry_ordinal = 1 if metadata.get("retry_ordinal") == "1" else 0
        return RecoveryRetryClaim(
            recovery_identity,
            original_attempt_id,
            retry_ordinal,
            retry_attempt_id,
            "exhausted",
            "receipt-unavailable-retry-exhausted",
            False,
        )
    retry_attempt_id = metadata.get("retry_attempt_id", "")
    if metadata.get("retry_ordinal") != "1" or not retry_attempt_id:
        raise DispatchContractError("recovery-claim-incomplete")
    return RecoveryRetryClaim(
        recovery_identity,
        original_attempt_id,
        1,
        retry_attempt_id,
        "claimed",
        "recovery-retry-claimed",
        True,
    )


def claim_recovery_retry(
    jobs: Path,
    *,
    recovery_id: str,
    source_route_id: str,
    source_route_hash: str,
    node_or_group_leg: str,
    original_attempt_id: str,
    remaining_cascade: int,
) -> RecoveryRetryClaim:
    """CAS one stable retry claim, or seal its permanent exhausted terminal."""

    if (
        not recovery_id
        or not source_route_id
        or not source_route_hash
        or not node_or_group_leg
        or not original_attempt_id
        or type(remaining_cascade) is not int
    ):
        raise DispatchContractError("recovery-claim-identity-incomplete")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        rows: list[tuple[int, list[str], dict[str, str]]] = []
        existing: list[dict[str, str]] = []
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = parse_registry_metadata(fields[5])
            if metadata.get("attempt_id") == original_attempt_id:
                rows.append((index, fields, metadata))
            if metadata.get("recovery_id") == recovery_id:
                existing.append(metadata)

        if existing:
            if len(existing) != 1:
                raise DispatchContractError("recovery-claim-not-unique")
            return _existing_recovery_claim(existing[0], original_attempt_id)
        if len(rows) != 1:
            raise DispatchContractError(
                "attempt-row-not-unique",
                f"attempt_id={original_attempt_id} rows={len(rows)}",
            )
        index, fields, metadata = rows[0]
        validate_attempt_metadata(metadata)
        if metadata.get("recovery_id"):
            raise DispatchContractError("recovery-claim-conflict")
        if (
            metadata.get("cancellation_quiescence_receipt")
            != ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT
            or metadata.get("quiescence_pgid_proof") != GROUP_REAP_PROOF
            or metadata.get("quiescence_descendant_proof")
            != ATTEMPT_DESCENDANT_PROOF
            or not re.fullmatch(
                r"sha256:[0-9a-f]{64}",
                metadata.get("cancellation_receipt_digest", ""),
            )
        ):
            raise DispatchContractError("cancellation-quiescence-receipt-required")
        expected_recovery_id = _recovery_identity_digest(
            {
                "source_route_id": source_route_id,
                "source_route_hash": source_route_hash,
                "node_or_group_leg": node_or_group_leg,
                "original_attempt_id": original_attempt_id,
                "cancellation_receipt_digest":
                    metadata["cancellation_receipt_digest"],
            }
        )
        if recovery_id != expected_recovery_id:
            raise DispatchContractError("recovery-claim-identity-mismatch")
        if (
            metadata.get("route_id") != source_route_id
            or metadata.get("route_hash") != source_route_hash
        ):
            raise DispatchContractError("recovery-claim-source-route-mismatch")

        route_claims = {
            claim_metadata.get("recovery_id", "")
            for line in lines
            if len((claim_fields := line.split("\t"))) == 6
            and (claim_metadata := parse_registry_metadata(claim_fields[5])).get(
                "route_id"
            ) == source_route_id
            and claim_metadata.get("retry_ordinal") == "1"
            and claim_metadata.get("recovery_id")
            and claim_metadata.get("recovery_id") != recovery_id
        }
        effective_remaining = max(0, remaining_cascade - len(route_claims))
        claimed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if effective_remaining < 1:
            prior = {
                "prior_terminal_note": metadata.get("note", ""),
                "prior_classifier_source": metadata.get("classifier_source", ""),
                "prior_failure_class": metadata.get("failure_class", ""),
            }
            fields[1] = "done"
            fields[5] = _updated_attempt_metadata(
                fields[5],
                {
                    "recovery_id": recovery_id,
                    "retry_claimed_at": claimed_at,
                    "note": "receipt-unavailable-retry-exhausted",
                    "failure_class": "blocked",
                    "classifier_source": AUTOMATIC_RECEIPTLESS_CLASSIFIER,
                    "reconcile_reason": "receipt-unavailable-retry-exhausted",
                    **{key: value for key, value in prior.items() if value},
                },
                terminal=True,
            )
            result = RecoveryRetryClaim(
                recovery_id,
                original_attempt_id,
                0,
                "",
                "exhausted",
                "receipt-unavailable-retry-exhausted",
                False,
            )
        else:
            retry_attempt_id = _stable_recovery_attempt_id(recovery_id)
            fields[5] = _updated_attempt_metadata(
                fields[5],
                {
                    "recovery_id": recovery_id,
                    "retry_ordinal": "1",
                    "retry_attempt_id": retry_attempt_id,
                    "retry_claimed_at": claimed_at,
                },
            )
            result = RecoveryRetryClaim(
                recovery_id,
                original_attempt_id,
                1,
                retry_attempt_id,
                "claimed",
                "recovery-retry-claimed",
                True,
            )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return result


@dataclass(frozen=True)
class StageAdvanceRegistryClaim:
    """SD-110 claim result: no predecessor identity in `claim_key`
    (`§13.32.1-(3)C`) -- distinct from `RecoveryRetryClaim`, which mutates an
    EXISTING attempt row. A stage-advance successor has no row yet, so this
    claim lives in its own CAS store under the same canonical registry lock."""

    stage_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


def _stage_advance_claim_key_digest(claim_key: tuple) -> str:
    return hashlib.sha256(
        json.dumps(list(claim_key), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _stable_stage_advance_attempt_id(stage_advance_id: str, successor_node: str) -> str:
    encoded = f"{stage_advance_id}\0{successor_node}".encode("utf-8")
    return "att-stage-advance-" + hashlib.sha256(encoded).hexdigest()[:32]


def claim_stage_advance(
    jobs: Path,
    *,
    stage_advance_id: str,
    route_hash: str,
    successor_node: str,
    advance_generation: int,
    source_route_id: str,
    predecessor_attempt_id: str,
) -> StageAdvanceRegistryClaim:
    """CAS one stable stage-advance claim inside `<jobs>.lock`, or replay the
    identical claim for a repeated `stage_advance_id` (A-16). A distinct
    `claim_key` already claimed by a DIFFERENT `stage_advance_id` is
    `stage-advance-claim-conflict` (A-4) -- one generation, one successor, one
    claimant, no matter which predecessor closed first.
    """

    if (
        not stage_advance_id.startswith("sadv-")
        or not route_hash
        or not successor_node
        or not source_route_id
        or not predecessor_attempt_id
        or type(advance_generation) is not int
    ):
        raise DispatchContractError("stage-advance-claim-identity-incomplete")
    ensure_global_registry_writable(jobs)
    claim_key = (route_hash, successor_node, advance_generation)
    claims_dir = jobs.parent / "stage_advance" / "claims"
    claim_path = claims_dir / f"{_stage_advance_claim_key_digest(claim_key)}.json"
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if claim_path.is_file():
            try:
                existing = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DispatchContractError("stage-advance-claim-record-invalid") from exc
            if existing.get("stage_advance_id") == stage_advance_id:
                return StageAdvanceRegistryClaim(
                    stage_advance_id=stage_advance_id,
                    claim_key=claim_key,
                    successor_attempt_id=existing["successor_attempt_id"],
                    replayed=True,
                )
            raise DispatchContractError("stage-advance-claim-conflict")
        successor_attempt_id = _stable_stage_advance_attempt_id(
            stage_advance_id, successor_node
        )
        record = {
            "stage_advance_id": stage_advance_id,
            "claim_key": list(claim_key),
            "successor_attempt_id": successor_attempt_id,
            "source_route_id": source_route_id,
            "predecessor_attempt_id": predecessor_attempt_id,
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        claims_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".claim.", dir=str(claims_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, claim_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return StageAdvanceRegistryClaim(
        stage_advance_id=stage_advance_id,
        claim_key=claim_key,
        successor_attempt_id=successor_attempt_id,
        replayed=False,
    )


@dataclass(frozen=True)
class SubsessionAdvanceRegistryClaim:
    """SD-119 chain-advance claim result. `claim_key` is predecessor-free
    (`route_hash`, `route_node`, `chain_id`, `successor_subsession_index`,
    `advance_generation`) -- one generation, one chain, one index, one
    claimant, regardless of which predecessor index closed first."""

    subsession_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


def claim_subsession_advance(
    jobs: Path,
    *,
    subsession_advance_id: str,
    route_hash: str,
    route_node: str,
    chain_id: str,
    successor_subsession_index: int,
    advance_generation: int,
    successor_attempt_id: str,
) -> SubsessionAdvanceRegistryClaim:
    """CAS one stable sub-session chain-advance claim inside `<jobs>.lock`, or
    replay the identical claim for a repeated `subsession_advance_id` (A-6/A-7).
    A distinct `claim_key` already claimed by a DIFFERENT `subsession_advance_id`
    is `subsession-advance-claim-conflict` (A-6), spawn 0."""

    if (
        not subsession_advance_id.startswith("ssadv-")
        or not route_hash
        or not route_node
        or not chain_id
        or not successor_attempt_id
        or type(advance_generation) is not int
        or type(successor_subsession_index) is not int
    ):
        raise DispatchContractError("subsession-advance-claim-identity-incomplete")
    ensure_global_registry_writable(jobs)
    claim_key = (route_hash, route_node, chain_id, successor_subsession_index, advance_generation)
    claims_dir = jobs.parent / "subsession_advance" / "claims"
    claim_path = claims_dir / f"{_stage_advance_claim_key_digest(claim_key)}.json"
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if claim_path.is_file():
            try:
                existing = json.loads(claim_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise DispatchContractError("subsession-advance-claim-record-invalid") from exc
            if existing.get("subsession_advance_id") == subsession_advance_id:
                return SubsessionAdvanceRegistryClaim(
                    subsession_advance_id=subsession_advance_id,
                    claim_key=claim_key,
                    successor_attempt_id=existing["successor_attempt_id"],
                    replayed=True,
                )
            raise DispatchContractError("subsession-advance-claim-conflict")
        record = {
            "subsession_advance_id": subsession_advance_id,
            "claim_key": list(claim_key),
            "successor_attempt_id": successor_attempt_id,
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        claims_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".claim.", dir=str(claims_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, claim_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return SubsessionAdvanceRegistryClaim(
        subsession_advance_id=subsession_advance_id,
        claim_key=claim_key,
        successor_attempt_id=successor_attempt_id,
        replayed=False,
    )


def seal_recovery_blocked(
    jobs: Path,
    *,
    original_attempt_id: str,
    recovery_id: str,
    reason: str = "receipt-unavailable-retry-exhausted",
) -> bool:
    """Seal one recovery as a canonical permanent no-start terminal."""

    if not original_attempt_id or not recovery_id:
        raise DispatchContractError("recovery-block-identity-incomplete")
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
            if metadata.get("attempt_id") == original_attempt_id:
                matches.append((index, fields, metadata))
        if len(matches) != 1:
            raise DispatchContractError(
                "attempt-row-not-unique",
                f"attempt_id={original_attempt_id} rows={len(matches)}",
            )
        index, fields, metadata = matches[0]
        validate_attempt_metadata(metadata)
        if metadata.get("recovery_id") not in {None, "", recovery_id}:
            raise DispatchContractError("recovery-block-identity-conflict")
        if (
            fields[1] == "done"
            and metadata.get("recovery_id") == recovery_id
            and metadata.get("note") == "receipt-unavailable-retry-exhausted"
            and metadata.get("failure_class") == "blocked"
            and metadata.get("start_permitted") == "0"
        ):
            return False
        fields[1] = "done"
        fields[5] = _updated_attempt_metadata(
            fields[5],
            {
                "recovery_id": recovery_id,
                "note": "receipt-unavailable-retry-exhausted",
                "failure_class": "blocked",
                "classifier_source": AUTOMATIC_RECEIPTLESS_CLASSIFIER,
                "reconcile_reason": reason or "receipt-unavailable-retry-exhausted",
                "start_permitted": "0",
            },
            terminal=True,
        )
        lines[index] = "\t".join(fields)
        _atomic_registry_replace(jobs, lines)
        return True


_MARKER_BOUND_PROCESS_KEYS = (
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
    "post_exit_receipt_substitute",
)


def marker_bound_process_identity(
    metadata: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Return the process-evidence generation observed before jobs locking."""

    return tuple((key, metadata.get(key, "")) for key in _MARKER_BOUND_PROCESS_KEYS)


def _marker_bound_row_verdict(metadata: dict[str, str]) -> str:
    failure_class = metadata.get("failure_class", "").lower()
    if failure_class == "pass" or metadata.get("note") in {
        "completed-marker",
        "completed-supervisor",
    }:
        return "PASS"
    if failure_class == "fail":
        return "FAIL"
    if failure_class == "blocked":
        return "BLOCKED"
    return ""


def _marker_bound_prepare_marker_proof(
    metadata: dict[str, str], attempt_id: str
) -> MarkerBoundCompletionProof | None:
    """Prove the full immutable marker chain before taking the jobs lock."""

    raw_path = metadata.get("completion_marker", "")
    if not raw_path:
        return None
    marker_path = Path(raw_path)
    if not marker_path.is_absolute() or marker_path.is_symlink():
        return None
    try:
        marker_bytes = marker_path.read_bytes()
    except OSError:
        return None
    if not marker_bytes or len(marker_bytes) > 65_536:
        return None
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    expected = {
        "route_id": metadata.get("route_id", ""),
        "route_hash": metadata.get("route_hash", ""),
        "node_id": metadata.get("route_node", ""),
        "attempt_id": attempt_id,
    }
    if any(not value or marker.get(key) != value for key, value in expected.items()):
        return None
    route_path = Path(metadata.get("route_file", ""))
    if not route_path.is_absolute() or route_path.is_symlink():
        return None
    try:
        route_bytes = route_path.read_bytes()
        route = json.loads(route_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (
        not isinstance(route, dict)
        or route.get("route_id") != metadata.get("route_id")
        or route.get("route_hash") != metadata.get("route_hash")
        or not isinstance(route.get("nodes"), list)
    ):
        return None
    nodes = [
        node
        for node in route["nodes"]
        if isinstance(node, dict) and node.get("id") == metadata.get("route_node")
    ]
    evidence_record = marker.get("evidence")
    if not isinstance(evidence_record, dict):
        return None
    evidence_path = Path(str(evidence_record.get("path", "")))
    history_path = marker_path.parent / f"{metadata.get('route_node', '')}.{marker.get('sequence', 0)}.json"
    dependency_paths = [route_path, evidence_path, history_path]
    if marker.get("stage_authority") == "owner-chain":
        dependency_paths.append(Path(str(marker.get("subsession_manifest", ""))))
    elif nodes and nodes[0].get("kind") != "resource-runner":
        safe_attempt = "".join(
            character if character.isalnum() or character in "._-" else "_"
            for character in attempt_id
        )
        dependency_paths.append(
            marker_path.parent
            / f"{metadata.get('route_node', '')}.{safe_attempt}.attempt.json"
        )
    dependency_bytes: list[tuple[Path, bytes]] = []
    try:
        for dependency_path in dependency_paths:
            if not dependency_path.is_absolute():
                return None
            dependency_bytes.append((dependency_path, dependency_path.read_bytes()))
    except OSError:
        return None
    if dependency_bytes[0][1] != route_bytes:
        return None
    if len(nodes) != 1 or not completion_marker_is_current(
        route, nodes[0], marker_path, marker
    ):
        return None
    try:
        if marker_path.read_bytes() != marker_bytes:
            return None
        if any(path.read_bytes() != raw for path, raw in dependency_bytes):
            return None
    except OSError:
        return None
    return MarkerBoundCompletionProof(
        marker=marker,
        marker_path=marker_path,
        marker_digest=hashlib.sha256(marker_bytes).hexdigest(),
        route_id=metadata.get("route_id", ""),
        route_hash=metadata.get("route_hash", ""),
        node_id=metadata.get("route_node", ""),
        attempt_id=attempt_id,
        immutable_file_digests=tuple(
            (str(path), hashlib.sha256(raw).hexdigest())
            for path, raw in dependency_bytes
        ),
    )


def _marker_bound_current_marker(
    metadata: dict[str, str],
    attempt_id: str,
    proof: MarkerBoundCompletionProof | None,
) -> tuple[dict[str, object] | None, str]:
    """CAS only current marker bytes against a pre-lock immutable-chain proof."""

    if proof is None or (
        metadata.get("completion_marker") != str(proof.marker_path)
        or metadata.get("route_id") != proof.route_id
        or metadata.get("route_hash") != proof.route_hash
        or metadata.get("route_node") != proof.node_id
        or attempt_id != proof.attempt_id
    ):
        return None, ""
    try:
        marker_bytes = proof.marker_path.read_bytes()
    except OSError:
        return None, ""
    digest = hashlib.sha256(marker_bytes).hexdigest()
    if digest != proof.marker_digest:
        return None, ""
    try:
        marker = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ""
    if marker != proof.marker:
        return None, ""
    return marker, digest


def _marker_bound_latest_rows(lines: list[str]) -> dict[str, tuple[int, list[str], str]]:
    latest: dict[str, tuple[int, list[str], str]] = {}
    for index, raw in enumerate(lines):
        fields = raw.split("\t")
        if len(fields) != 6:
            continue
        attempt_id = parse_registry_metadata(fields[5]).get("attempt_id", "")
        if attempt_id:
            if attempt_id in latest:
                raise DispatchContractError(
                    "attempt-row-not-unique", f"attempt_id={attempt_id} rows=2+"
                )
            latest[attempt_id] = (index, fields, raw)
    return latest


def marker_bound_delivery_transaction(
    jobs: Path,
    attempt_id: str,
    *,
    parent_attempt_id: str,
    expected_row_revision: str,
    expected_process_identity: tuple[tuple[str, str], ...],
    process_observation: ProcessQuiescence,
    advance: bool = True,
) -> MarkerBoundDeliveryResult:
    """Classify and optionally advance one marker-bound attempt under one lock.

    Process inspection belongs to the caller.  This transaction compares that
    pre-lock observation with the exact row generation and process identity,
    reads only the canonical registry and its immutable marker while locked,
    and performs at most one in-process registry replacement.
    """

    if not attempt_id or not parent_attempt_id:
        raise DispatchContractError("delivery-identity-incomplete")
    try:
        prelock_lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        prelock_lines = []
    prelock_rows = _marker_bound_latest_rows(prelock_lines)
    prelock_current = prelock_rows.get(attempt_id)
    marker_proof = None
    if prelock_current is not None:
        marker_proof = _marker_bound_prepare_marker_proof(
            parse_registry_metadata(prelock_current[1][5]), attempt_id
        )
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        latest = _marker_bound_latest_rows(lines)
        current = latest.get(attempt_id)
        if current is None:
            return MarkerBoundDeliveryResult(None, "", "", "", "", "", False, 0, False)

        index, fields, raw = current
        metadata = parse_registry_metadata(fields[5])
        revision = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        observation_current = bool(
            expected_row_revision
            and revision == expected_row_revision
            and marker_bound_process_identity(metadata) == expected_process_identity
        )

        def owned_open_count(rows: dict[str, tuple[int, list[str], str]]) -> int:
            count = 0
            for _order, child_fields, _raw in rows.values():
                child_metadata = parse_registry_metadata(child_fields[5])
                if (
                    child_metadata.get("parent_attempt_id") == parent_attempt_id
                    and child_metadata.get("registered_worker") == "1"
                    and child_fields[1] in {"open", "running"}
                ):
                    count += 1
            return count

        owned_children = owned_open_count(latest)
        marker, marker_digest = _marker_bound_current_marker(
            metadata, attempt_id, marker_proof
        )
        verdict = _marker_bound_row_verdict(metadata)
        quiescent = observation_current and process_observation.state == "quiescent"
        advanced = False
        if (
            advance
            and observation_current
            and fields[1] in {"open", "running"}
            and marker is not None
            and quiescent
            and owned_children == 0
            and verdict in {"", "PASS"}
        ):
            fields[1] = "done"
            values = {
                "note": "completed-marker",
                "failure_class": "pass",
                "classifier_source": "marker-bound-delivery-v1",
            }
            values.update(_delivery_intent_values(fields, {**metadata, **values}))
            fields[5] = _updated_attempt_metadata(
                fields[5], values, terminal=True,
            )
            lines[index] = "\t".join(fields)
            _atomic_registry_replace(jobs, lines)
            advanced = True

        # Re-read exactly once before returning.  A concurrent writer cannot
        # enter until this lock is released, and our own replace is the sole
        # allowed mutation in this scope.
        refreshed_lines = jobs.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        refreshed = _marker_bound_latest_rows(refreshed_lines)
        current = refreshed.get(attempt_id)
        if current is None:
            return MarkerBoundDeliveryResult(None, "", "", "", "", "", False, 0, advanced)
        _index, refreshed_fields, refreshed_raw = current
        refreshed_metadata = parse_registry_metadata(refreshed_fields[5])
        refreshed_marker, refreshed_marker_digest = _marker_bound_current_marker(
            refreshed_metadata, attempt_id, marker_proof
        )
        refreshed_revision = hashlib.sha256(
            refreshed_raw.encode("utf-8")
        ).hexdigest()
        return MarkerBoundDeliveryResult(
            marker=refreshed_marker,
            marker_digest=refreshed_marker_digest,
            row_revision=refreshed_revision,
            row_digest=refreshed_revision,
            status=refreshed_fields[1],
            verdict=_marker_bound_row_verdict(refreshed_metadata),
            quiescent=quiescent,
            owned_children=owned_open_count(refreshed),
            advanced=advanced,
            supervisor_terminal=(
                refreshed_metadata.get("note") == "completed-supervisor"
                and refreshed_metadata.get("failure_class") == "pass"
            ),
        )


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


# SD-111 D-2/C-2: duplicated (not imported) from dispatch_completion_join's
# CANONICAL_RECEIPT_KEYS/CANONICAL_CHILD_KEYS/canonical_receipt_digest/
# seal_delivery_receipt -- dispatch_completion_join imports THIS module, so
# the reverse import would be circular. Keep every copy of this vocabulary
# (here, dispatch_completion_join.py, dispatch_pending_delivery.py)
# synchronized by hand; §11 forbids widening it.
_CANONICAL_RECEIPT_KEYS = frozenset({
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_classification",
})
_CANONICAL_CHILD_KEYS = frozenset({
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
})
_MAX_DELIVERY_RECEIPT_BYTES = 2048
_DELIVERY_INTENT_IMMUTABLE_KEYS = frozenset({
    "delivery_intent", "delivery_id", "delivery_recipient_digest", "delivery_receipt_digest",
})
# SD-111 §4.3.1: fixed markers for the two explicit delivery exclusions,
# found by grep against the actual writer (round 2 required this be resolved
# by full-value equality, never substring match, before P2 wiring):
#   - SD-105 cancellation: `utilities/dispatch-registry.py` closes the row via
#     `close_attempt_row_if(..., evidence={"note": "cancelled-receipt-unavailable", ...})`
#     (W4) -- exact `note` value, confirmed 2026-08-28.
#   - SD-110 eligible-linear success: NOT wired. `dispatch_stage_advance.py`
#     never mutates the predecessor row -- `claim_stage_advance` only writes a
#     separate claim-record file, and the predecessor's own terminal edge
#     (whichever of W1-W4 it took) happens *before* stage-advance is even
#     attempted, so no fixed marker exists on the row at intent-stamp time.
#     Per the plan's safe default (§4.3.1/§9 R-17) this exclusion is left
#     UNIMPLEMENTED: a record is created for these rows too. See the P2 dev
#     log for the full grep trail.
_SD105_CANCELLED_NOTE = "cancelled-receipt-unavailable"


def _delivery_intent_values(fields: list[str], metadata: dict[str, str]) -> dict[str, str]:
    """Compute the one-time delivery-intent stamp for a row that just took its
    `open|running -> done` edge (W1-W4), or {} if none is owed.

    Pure: no lock, no I/O, no registry read beyond the row already in hand.
    Called from inside the same `<jobs>.lock` the writer already holds, right
    before `_updated_attempt_metadata(..., terminal=True)`.
    """

    if not metadata.get("parent_completion_delivery") or not metadata.get("parent_sid"):
        return {}
    if metadata.get("parent_completion_delivery") not in RECIPIENT_KINDS:
        # A47-10: `parent-runtime-supervised`/`poll-fallback` rows are the
        # SD-78 supervisor outbox's own responsibility -- no stamp, no
        # pending-delivery record, no `delivery_persistence_refused` either.
        return {}
    if metadata.get("delivery_intent"):
        # Already stamped -- W1-W4 share one lock and this function only ever
        # runs once per row's single open|running->done edge, but a repaired/
        # conflict path re-invoking this defensively must still no-op.
        return {}
    if metadata.get("note") == _SD105_CANCELLED_NOTE:
        return {}

    attempt_id = metadata.get("attempt_id", "")
    if not attempt_id:
        return {}
    recipient_kind = metadata["parent_completion_delivery"]
    recipient_key = metadata["parent_sid"]
    parent_attempt_id = metadata.get("parent_attempt_id", "")
    route_id = metadata.get("route_id", "")
    route_node = metadata.get("route_node", "")
    is_success = (
        metadata.get("failure_class") == "pass"
        or metadata.get("note") in {"completed-marker", "completed-supervisor"}
    )
    child = {
        "attempt_id": attempt_id,
        "status": "done",
        "readiness": "ready",
        "harness": metadata.get("harness", ""),
        "required_action": "advance-completed" if is_success else "inspect-done-failure",
        "delivery_classification": "success" if is_success else "attention",
    }
    if not is_success:
        child["reason"] = "terminal-failure-or-unclosed"
    receipt = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": parent_attempt_id,
        "job_registry": "",
        "children": [child],
        "delivery_classification": child["delivery_classification"],
    }
    canonical = {key: value for key, value in receipt.items() if key in _CANONICAL_RECEIPT_KEYS}
    canonical["children"] = [
        {key: value for key, value in child.items() if key in _CANONICAL_CHILD_KEYS}
    ]
    canonical_bytes = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    receipt_digest = hashlib.sha256(canonical_bytes).hexdigest()

    receipt_bytes = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(receipt_bytes) > _MAX_DELIVERY_RECEIPT_BYTES:
        # Stamp failure must not block row closure (§4.4): the caller records
        # this typed refusal on its own evidence key and proceeds to close.
        return {"delivery_persistence_refused": "pending-delivery-oversized"}
    sealed = base64.standard_b64encode(receipt_bytes).decode("ascii").rstrip("=")

    row_revision = hashlib.sha256(
        "\t".join(fields).encode("utf-8")
    ).hexdigest()
    identity_material = json.dumps(
        {
            "recipient_key": recipient_key,
            "attempt_ids": [attempt_id],
            "receipt_digest": receipt_digest,
            "row_revisions": {attempt_id: row_revision},
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    delivery_id = "delivery-" + hashlib.sha256(identity_material).hexdigest()[:32]

    return {
        "delivery_intent": "1",
        "delivery_id": delivery_id,
        "delivery_recipient_kind": recipient_kind,
        "delivery_recipient_digest": hashlib.sha256(recipient_key.encode("utf-8")).hexdigest(),
        "delivery_receipt_digest": receipt_digest,
        "delivery_row_revision": row_revision,
        "delivery_intent_at_ns": str(time.monotonic_ns()),
        "delivery_receipt_b64": sealed,
    }


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
        if (
            key in _DELIVERY_INTENT_IMMUTABLE_KEYS
            and metadata.get(key)
            and metadata.get(key) != value
        ):
            # SD-111 §4.4: delivery_intent/delivery_id/delivery_recipient_digest/
            # delivery_receipt_digest are write-once. `_delivery_intent_values`
            # already refuses to re-stamp a row that has one (the ordinary
            # path never reaches here with a changed value); this is the
            # defense-in-depth the plan asks for at the low-level writer too.
            raise DispatchContractError(
                "pending-delivery-identity-conflict",
                f"key={key} existing={metadata.get(key)} requested={value}",
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
            values.update(_delivery_intent_values(fields, {**metadata, **values}))
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
        values.update(_delivery_intent_values(fields, {**metadata, **values}))
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
            values.update(_delivery_intent_values(fields, {**metadata, **values}))
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
    *,
    statuses: frozenset[str] = frozenset({"open", "running"}),
) -> bool:
    """Compare-and-set mutable metadata on one exact attempt row.

    Only open rows are eligible by default. A caller that repairs missing
    post-exit evidence has to name a terminal status explicitly, because the row
    it needs to annotate is precisely the one an ordinary annotate would skip.
    """

    if not attempt_id:
        raise DispatchContractError("attempt-id-required")
    ensure_global_registry_writable(jobs)
    with Path(f"{jobs}.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
        for index, line in enumerate(lines):
            fields = line.split("\t")
            if len(fields) != 6 or fields[1] not in statuses:
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
