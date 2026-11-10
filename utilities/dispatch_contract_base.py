"""Dispatch contract: constants, contract types and registry-row helpers.

The bottom layer of the dispatch contract modules: the shared constants and
key sets, agent-home resolution, the contract error and record types,
registry-metadata parsing and the pure registry-row helpers, repository
identity, the governance environment and diagnostics, and the lazy
`capability-route.py` loader. It imports no other dispatch contract module.

Split out of `dispatch_contract.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import unicodedata
from typing import Mapping, NamedTuple
from dispatch_attempt_policy import committed_outcome
from dispatch_receipt_identity import receipt_digest as shared_receipt_digest, unseal_receipt
from dispatch_pending_delivery import RECIPIENT_KINDS


try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child


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
    # Published with the actual child environment after the admission claim.
    # A duplicate start has not prepared that environment yet.
    "runtime_home",
    "parent_close_requested", "parent_close_route_id", "parent_close_route_hash",
    "parent_close_stop_resources", "parent_close_settled",
    "resolved_model",
    # The launch snapshot may move the effective variant after the claim; the
    # original request and the observer ride along with it.
    "variant", "variant_fit_from", "catalog_fit_observer",
    # Observational caller provenance, never parent execution/delivery authority.
    "caller_sid", "caller_harness", "caller_pane", "caller_cwd",
    "launch_claimed",
    "launch_cgroup_b64",
    "launch_cgroup_ns",
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
    "attempt_descendant_residue",
    "attempt_descendant_residue_count",
    "attempt_descendant_residue_basis",
    "attempt_descendant_residue_at",
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
    "recovery_exhausted",
    "recovery_exhaustion_reason",
    "start_permitted",
    "cleanup_receipt_b64",
    "cleanup_receipt_digest",
    "cancellation_quiescence_receipt",
    "cancellation_receipt_digest",
    "quiescence_pgid_proof",
    "quiescence_descendant_proof",
    # Launch-root diagnostics, written once on the fence-failure path. They must be
    # MUTABLE, not terminal evidence: `_immutable_attempt_identity` folds every key
    # outside this set into the row's identity, so recording them as evidence would
    # make two observations of one attempt disagree (`attempt-identity-conflict`).
    "launch_mismatch_roots",
    "launch_mismatch_fields",
    "launch_mismatch_expected",
    "launch_mismatch_observed",
}
ATTEMPT_TERMINAL_EVIDENCE_KEYS = {
    "api_status",
    # Where an invalid artifact path was looked for (codex_dispatch_terminal.artifact_hint).
    "artifact_base_root_b64",
    "artifact_candidates_b64",
    "capacity_log",
    "classifier_source",
    "detected_by",
    "failure_class",
    "process_exit",
    "quota_model_scope",
    "quota_window",
    "reconcile_reason",
    "reset",
    "terminal_event",
    "watchdog_windows",
    "terminal_conflict",
    "prior_terminal_note",
    "prior_classifier_source",
    "prior_failure_class",
    "prior_reconcile_reason",
    "terminal_correction_b64",
    "conflicting_classifier_source",
    "conflicting_failure_class",
    "conflicting_terminal_note",
    "terminal_conflicts_b64",
    "receipt_state",
    "marker_state",
    # Bounded, sanitized diagnostics written only when an escaped terminal
    # commit error is classified; these carry no route authority.
    "terminal_commit_detail",
    "terminal_commit_slot",
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
    # OPERATIONS §5.10 "Review verdict is a result, not a worker death": the
    # in-root review artifact a `completed-review-blocking` row named in its
    # FAIL handoff (urlsafe base64, padding stripped), sealed at close time so
    # the owner-closure gate in capability-route.py can re-verify it.
    "review_artifact_b64",
    # Owner-closure seal (capability-route.py, OPERATIONS §5.10): written only
    # through `_updated_attempt_metadata(..., terminal=True)` so the operator-
    # chosen evidence path is sanitized like every other terminal value.
    "gate_closure",
    # SD-OPEN-41(b): verdict provenance for a `review-worker` node's completion.
    # `reviewer_kind` is one of capability-route's REVIEWER_KINDS,
    # `review_independence` is independent|degraded|owner-overridden, and the
    # last two are present only on the corresponding close. Written once, beside
    # `note=completed-marker`, at the row's one close -- but like `gate_closure`
    # and every other member of this set except `launch_outcome` and the
    # delivery-intent keys, that is a property of the only writer, NOT enforced
    # by `_updated_attempt_metadata`: a later `terminal=True` write can still
    # change them. Do not cite this list as a write-once guarantee.
    "reviewer_kind",
    "review_independence",
    "reviewer_downgrade_reason",
    "review_gate_closure",
    "owner_closure",
}

# Foreground review completion is authoritative only for a deliberately
# route-free registered depth-1 review.  Keep this list exhaustive: adding a
# provenance field here would silently turn a route-bound row into a new
# authority surface.
ROUTE_IDENTITY_METADATA_KEYS = (
    "route_file",
    "route_id",
    "route_hash",
    "route_node",
    "registry_digest",
    "write_scope",
    "completion_gate",
    "owner_route_file",
    "owner_route_id",
    "owner_route_hash",
    "batch_route_id",
    "batch_route_node",
)
FOREGROUND_OUTCOME_KEYS = (
    "foreground_outcome_schema",
    "foreground_outcome_ready",
    "foreground_process_exit",
    "foreground_process_failure",
    "foreground_group_empty",
    "foreground_outcome_source",
)
FOREGROUND_OUTCOME_SOURCE = "wait-foreground-v1"
# `interrupted`: the wrapper itself received SIGINT/SIGTERM/SIGHUP and tore the
# worker down (its caller stopped). A worker that died of a signal on its own
# stays `signal-N`.
FOREGROUND_FAILURES = frozenset({
    "none", "timeout", "parent-terminated", "process-identity-unavailable",
    "interrupted", "no-progress",
})
# The fixed failure words; `exit-N`/`signal-N` are validated separately.
_FOREGROUND_FIXED_FAILURES = FOREGROUND_FAILURES - {"none"}
_MODULE_ROOT = Path(__file__).resolve().parents[1]


def codex_owner_network_posture(
    *, dispatch_depth: int, worker_type: str, sandbox: str,
    gpu_resource_owner: bool = False, observation_only: bool = False,
) -> bool:
    """The one rule for a Codex launch's general (non-loopback) network grant.

    On: a dispatch-depth-1 owner in the `workspace-write` sandbox (or a GPU
    resource owner in `danger-full-access`), whatever its capability (audit
    included), intensity or entry scope, the posture a Claude or OpenCode
    depth-1 owner process has (PG-44). A `report` scope is kept by the owner
    instruction on all three harnesses.
    Off: every dispatch-depth-2 (or other non-owner) worker, a `read-only`
    sandbox, and the observation-only fence while the worktree is mid-merge,
    rebase or cherry-pick or its Git state is unknown.
    Nothing else widens it: a task publication scope adds no network of its own.
    Approved-test localhost is decided by
    `verification_runtime.local_network_projection`.
    """
    return (
        dispatch_depth == 1
        and worker_type == "owner"
        and (sandbox == "workspace-write"
             or (gpu_resource_owner and sandbox == "danger-full-access"))
        and not observation_only
    )


def codex_standard_owner_network_enabled(
    *, dispatch_depth: int, worker_type: str, intensity: str, sandbox: str,
    gpu_resource_owner: bool = False,
) -> bool:
    """The route's stable network capacity, before the Git-state fence.

    Kept under this name for its existing callers; ``intensity`` no longer
    narrows it (`codex_owner_network_posture`).
    """
    del intensity
    return codex_owner_network_posture(
        dispatch_depth=dispatch_depth, worker_type=worker_type, sandbox=sandbox,
        gpu_resource_owner=gpu_resource_owner,
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
    # The managed-release pointer honours XDG_DATA_HOME exactly as agent-home.sh does
    # (`${XDG_DATA_HOME:-$HOME/.local/share}`), so the shell and Python hooks agree (GA-A5-015).
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    candidates.extend(
        [
            str(Path(data_home) / "hearting" / "current"),
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


def sealed_launch_home(agent_home: str | Path) -> Path:
    """The concrete release path to seal into a dispatch row.

    Fixes a mutable pointer like `<share>/hearting/current` **at record
    time**. Prune resolves realpath again at judgment time, so a row that
    carries the raw symlink can never say which release it was actually
    sealed against (SD-115 axis 4 (a)).

    A resolve failure or a result that isn't a harness root is returned
    unchanged -- a wrong record beats a missing one, but a wrong record is
    still better than one that goes silently missing.
    """
    candidate = Path(agent_home)
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return candidate
    if not (resolved / "core" / "CORE.md").is_file():
        return candidate
    return resolved


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
    """Structured dispatch-contract failure.

    ``next_action`` is optional and set only by SD-154 route-state refusals
    (``ROUTE_STATE_REFUSAL_REASONS``) -- every other raiser leaves it ``None``
    and callers must tolerate that.
    """

    def __init__(self, reason: str, detail: str = "", *, next_action: str | None = None):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason
        self.next_action = next_action


@dataclass(frozen=True)
class ReviewHolderDisposition:
    """Closed report-lease liveness result.

    Timestamp expiry is deliberately absent from this decision.  It is only a
    stale/unobservable ceiling; exact process evidence or the namespace-safe
    governed witness is the authority for a live holder.
    """

    state: str
    reason: str


@dataclass(frozen=True)
class ReviewAdmissionCleanup:
    """Closed proof returned by watchdog admission cleanup."""

    watchdog_group: str
    fenced_child_group: str
    readiness: str
    review_lease: str
    governed_witness: str
    payload_marker: str
    status: str


class PostClaimAdmission:
    """Compatible post-claim transaction object for review admission.

    The common transaction owns registry mutation.  The callbacks own only
    external resources and are intentionally idempotent, so an abort never
    acquires or mutates the jobs lock.
    """

    __slots__ = (
        "metadata", "_abort_callback", "_commit_callback", "_abort_result",
        "_committed", "_aborted", "_commit_started",
    )

    def __init__(self, metadata: Mapping[str, str], *, abort: Callable[[str], ReviewAdmissionCleanup], commit: Callable[[], None]):
        self.metadata = dict(metadata)
        self._abort_callback = abort
        self._commit_callback = commit
        self._abort_result: ReviewAdmissionCleanup | None = None
        self._committed = False
        self._aborted = False
        self._commit_started = False

    def abort(self, reason: str) -> ReviewAdmissionCleanup:
        if self._committed:
            raise DispatchContractError("review-admission-abort-after-commit")
        if self._abort_result is None:
            self._aborted = True
            try:
                self._abort_result = self._abort_callback(reason)
            except BaseException:
                self._abort_result = ReviewAdmissionCleanup(
                    watchdog_group="unverified", fenced_child_group="unverified",
                    readiness="unverified", review_lease="unverified",
                    governed_witness="unverified", payload_marker="unverified",
                    status="unverified",
                )
        return self._abort_result

    def commit(self) -> None:
        if self._committed:
            return
        if self._aborted:
            raise DispatchContractError("review-admission-commit-after-abort")
        self._commit_started = True
        self._commit_callback()
        self._committed = True


@dataclass(frozen=True)
class SealedForegroundOutcome:
    """The lock-scoped foreground result and its seal-moment row evidence."""

    values: Mapping[str, str]
    attempt_raw_row_sha256: str


def foreground_review_eligible(
    metadata: Mapping[str, object],
    *,
    expected_attempt_id: str,
    expected_pid: int,
    expected_pid_start: str,
    expected_pgid: int,
) -> bool:
    """Return the one exact route-free foreground-review predicate."""

    return _foreground_review_eligibility_reason(
        metadata,
        expected_attempt_id=expected_attempt_id,
        expected_pid=expected_pid,
        expected_pid_start=expected_pid_start,
        expected_pgid=expected_pgid,
    ) == ""


def _foreground_review_eligibility_reason(
    metadata: Mapping[str, object],
    *,
    expected_attempt_id: str,
    expected_pid: int,
    expected_pid_start: str,
    expected_pgid: int,
) -> str:
    """Give the seal/classifier callers a stable refusal family."""

    if not (
        metadata.get("transport") == "headless"
        and metadata.get("execution_surface") == "registered-headless"
        and metadata.get("registered_worker") == "1"
        and metadata.get("dispatch_depth") == "1"
        and metadata.get("worker_type") == "review"
        and metadata.get("launch_lifecycle") == "foreground-scoped"
        and not any(metadata.get(key) for key in ROUTE_IDENTITY_METADATA_KEYS)
        and metadata.get("pgid", "").isdigit()
        and metadata.get("pid", "").isdigit()
        and metadata.get("pid_start")
        and metadata.get("pgid") == metadata.get("pid")
        and metadata.get("pid_ns")
        and metadata.get("pid_ns") == metadata.get("pid_observer_ns")
    ):
        return "eligibility"
    if not (
        metadata.get("attempt_id") == expected_attempt_id
        and metadata.get("pid") == str(expected_pid)
        and metadata.get("pid_start") == expected_pid_start
        and metadata.get("pgid") == str(expected_pgid)
    ):
        return "binding"
    return ""


def foreground_review_launch_identity(args: object) -> dict[str, object]:
    """Project the route identity that the adapter will actually record.

    Flat CLI values cover the ordinary route fields; owner and replica binding
    objects carry the remaining fields.  Keeping this projection next to the
    shared twelve-key predicate prevents an adapter from treating a missing
    flat attribute as proof that the eventual registry row is route-free.
    """

    identity: dict[str, object] = {
        key: getattr(args, key, None) or ""
        for key in ROUTE_IDENTITY_METADATA_KEYS
    }
    owner = getattr(args, "owner_route_binding", None)
    if owner is not None:
        identity["owner_route_file"] = getattr(owner, "route_file", None) or ""
        identity["owner_route_id"] = getattr(owner, "route_id", None) or ""
        identity["owner_route_hash"] = getattr(owner, "route_hash", None) or ""
    reservation = getattr(args, "replica_batch_reservation", None) or {}
    for key in ("batch_route_id", "batch_route_node"):
        if key in reservation:
            identity[key] = reservation.get(key) or ""
    return identity


# SD-113 A47-2: the vocabulary `_delivery_intent_values()` recognizes as a
# record-creating `parent_completion_delivery` stamp must equal
# `dispatch_pending_delivery.RECIPIENT_KINDS` exactly -- documented here so a
# future edit to either set is caught at import time instead of silently
# drifting one guard's `{}` no-op out of sync with the other's acceptance.
_KNOWN_DELIVERY_RECIPIENT_KINDS = frozenset({
    "claude-parent-runtime", "codex-stop-hook", "codex-managed-gateway",
    "codex-native-queue", "opencode-turn",
})
if _KNOWN_DELIVERY_RECIPIENT_KINDS != frozenset(RECIPIENT_KINDS):
    raise DispatchContractError(
        "delivery-intent-vocabulary-drift",
        f"expected={sorted(_KNOWN_DELIVERY_RECIPIENT_KINDS)} "
        f"actual={sorted(RECIPIENT_KINDS)}",
    )


@dataclass(frozen=True)
class RegistrySelection:
    path: Path
    source: str
    inherited: bool


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
    repository_path: str = ""


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
    completion_proven: bool = False
    terminal_conflict: bool = False


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
class AttemptExecutionObservation:
    """Execution-only evidence, kept separate from governed cleanup liveness."""

    state: str  # active / absent / unknown
    reason: str = ""
    identities: tuple[tuple[int, str, str], ...] = ()


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


_REGISTRY_METADATA_CACHE_SIZE = 32768
# Row text the parse cache may keep, about one live registry: a full scan still
# hits on its second pass, but a resident observer no longer keeps the rows of
# every older registry generation it ever read (32768 rows of ~5 KB text plus
# their parsed pairs were ~0.9 GB per process).
_REGISTRY_METADATA_CACHE_BYTES = 64 * 1024 * 1024
# Values this short repeat across rows (flags, harness and status words, small
# numbers). They are shared through a table owned by the cache, never through
# sys.intern, whose strings are never freed.
_REGISTRY_METADATA_SHARED_VALUE_CHARS = 32
_REGISTRY_METADATA_SHARED_MAX = 1 << 16


class _RegistryCacheInfo(NamedTuple):
    hits: int
    misses: int
    maxsize: int
    currsize: int


class _RegistryMetadataCache:
    """Row-text keyed metadata parse bounded by row count and by row text bytes.

    Same results and ``cache_info``/``cache_clear`` surface as the
    ``functools.lru_cache`` it replaces. Field names and short values are
    shared, which roughly halves the memory a cached row costs.
    """

    def __init__(self, maxsize: int, maxbytes: int):
        import threading
        from collections import OrderedDict
        self.maxsize, self.maxbytes = maxsize, maxbytes
        self._entries: OrderedDict = OrderedDict()
        self._bytes = 0
        self._shared: dict[str, str] = {}
        self._hits = self._misses = 0
        self._lock = threading.Lock()

    def _share(self, value: str) -> str:
        shared = self._shared.get(value)
        if shared is None:
            if len(self._shared) >= _REGISTRY_METADATA_SHARED_MAX:
                self._shared.clear()
            shared = self._shared[value] = value
        return shared

    def __call__(self, pipe: str) -> tuple[tuple[str, str], ...]:
        with self._lock:
            items = self._entries.get(pipe)
            if items is not None:
                self._entries.move_to_end(pipe)
                self._hits += 1
                return items
            self._misses += 1
            parsed = dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)
            items = tuple(
                (self._share(key),
                 self._share(value) if len(value) <= _REGISTRY_METADATA_SHARED_VALUE_CHARS else value)
                for key, value in parsed.items())
            self._entries[pipe] = items
            self._bytes += len(pipe)
            while self._entries and (len(self._entries) > self.maxsize
                                     or self._bytes > self.maxbytes):
                oldest, _items = self._entries.popitem(last=False)
                self._bytes -= len(oldest)
            return items

    def cache_info(self) -> _RegistryCacheInfo:
        with self._lock:
            return _RegistryCacheInfo(self._hits, self._misses, self.maxsize, len(self._entries))

    def cache_clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._shared.clear()
            self._bytes = 0
            self._hits = self._misses = 0


_registry_metadata_items = _RegistryMetadataCache(
    _REGISTRY_METADATA_CACHE_SIZE, _REGISTRY_METADATA_CACHE_BYTES)


def parse_registry_metadata(pipe: str) -> dict[str, str]:
    """Parse the stable six-column registry's comma-delimited metadata.

    One prompt-hook sweep reads the same rows many times; the parse is cached by
    the row text and every caller still gets its own dict.
    """

    return dict(_registry_metadata_items(pipe))


# SD-156: attempt registration's source-lineage fields. One name per fact, so
# the three wrapper scripts merge the same keys into a row's metadata instead
# of each inventing their own -- and any future reader of an attempt row knows
# these are the only lineage keys a registered launch may carry.
SOURCE_LINEAGE_ROW_FIELD_KEYS = frozenset({
    "launch_head", "source_commit_sealed", "source_commit_distance", "source_commit_branch",
})


def source_lineage_row_fields(route_validation_json: dict[str, object] | None) -> dict[str, str]:
    """Attempt-registration fields from a `worker-route-guard.py validate` JSON.

    `route_validation_json` is the parsed stdout of that command (the wrappers
    already capture it as `args.route_validation`). Reads its top-level
    `source_lineage` object (added by SD-156) and returns only the fields that
    have a value -- a route validated before this change, or one whose cwd
    could not verify (`kind` == `unverifiable`), carries no `observed`/
    `distance`/`branch` and contributes nothing here.
    """
    lineage = (route_validation_json or {}).get("source_lineage")
    if not isinstance(lineage, dict):
        return {}
    fields: dict[str, str] = {}
    if lineage.get("observed") is not None:
        fields["launch_head"] = str(lineage["observed"])
    if lineage.get("sealed") is not None:
        fields["source_commit_sealed"] = str(lineage["sealed"])
    if lineage.get("distance") is not None:
        fields["source_commit_distance"] = str(lineage["distance"])
    if lineage.get("branch") is not None:
        fields["source_commit_branch"] = str(lineage["branch"])
    return fields


def _diff_attribution_downstream_of_execute(route: dict[str, object], node_id: str) -> bool:
    if node_id == "execute":
        return False
    by_id = {str(row.get("id")): row for row in route.get("nodes", []) if isinstance(row, dict)}
    seen: set[str] = set()
    stack = list((by_id.get(node_id, {}) or {}).get("depends_on", []) or [])
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        if dep == "execute":
            return True
        stack.extend((by_id.get(dep, {}) or {}).get("depends_on", []) or [])
    return False


def _diff_attribution_execute_launch_head(jobs: Path, route: dict[str, object]) -> str | None:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    latest: str | None = None
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6 or fields[1] != "done":
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("route_id") != route.get("route_id") or metadata.get("route_node") != "execute":
            continue
        if metadata.get("launch_head"):
            latest = metadata["launch_head"]
    return latest


def diff_attribution_lines(route: dict[str, object], node: dict[str, object], jobs: Path) -> list[str]:
    """`diff_base`/`pre_node_commits` prompt lines for a node downstream of `execute`.

    SD-156: `execute` is the mutation node, so an impl-review/test/report
    prompt needs to know exactly which commits are that node's own diff --
    not everything since the sealed `source_commit`, which may include a prior
    package's commits in a serial execute chain. `diff_base` is `execute`'s
    most recent terminal attempt's `launch_head` (the observed HEAD SD-156
    recorded on that attempt's row); `pre_node_commits` is the first-parent
    commits from the sealed `source_commit` up to and including `diff_base`,
    read off the same shared `source_lineage_verdict` every other SD-156
    consumer uses. Empty for a node that is not downstream of `execute`, or
    when `execute` has no terminal attempt with a recorded `launch_head` yet.
    """
    if not _diff_attribution_downstream_of_execute(route, str(node.get("id"))):
        return []
    diff_base = _diff_attribution_execute_launch_head(Path(jobs), route)
    if not diff_base:
        return []
    sealed = route.get("source_commit")
    cwd = route.get("cwd")
    pre_node_commits: tuple[str, ...] = (diff_base,)
    if isinstance(sealed, str) and cwd:
        verdict = _route_module().source_lineage_verdict(cwd, sealed)
        if verdict.kind in ("descendant", "diverged") and diff_base in verdict.commits:
            pre_node_commits = tuple(verdict.commits[: verdict.commits.index(diff_base) + 1])
    return [
        f"diff_base={diff_base}",
        f"pre_node_commits={','.join(pre_node_commits)}",
    ]


def _parse_review_metadata(pipe: str) -> dict[str, str]:
    """Parse the path-safe subset used by the report authorization tuple."""
    result: dict[str, str] = {}
    for part in pipe.split(","):
        if not part or "=" not in part:
            raise DispatchContractError("review-metadata-encoding-invalid", part)
        key, value = part.split("=", 1)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]{0,63}", key):
            raise DispatchContractError("review-metadata-key-invalid", key)
        if not value or any(char in value for char in ",\t\r\n"):
            raise DispatchContractError("review-metadata-value-invalid", key)
        if key in result:
            raise DispatchContractError("review-metadata-duplicate", key)
        result[key] = value
    return result


REVIEW_OUTPUT_BINDING_SCHEMA_VERSION = 2
REVIEW_GOVERNED_LEASE_KIND = "summary-flock-v1"
REVIEW_GOVERNED_LEASE_NONCE_RE = re.compile(r"[0-9a-f]{64}")
REVIEW_POST_CLAIM_METADATA_KEYS = frozenset({
    "review_lease_acquired_at",
    "review_lease_deadline",
    "review_lease_record_digest",
    "review_admission",
    "review_watchdog_budget_digest",
    "review_readiness_digest",
    "review_fence_pid",
    "review_fence_pid_start",
    "review_fence_pgid",
    "review_fence_pid_ns",
    "review_fence_pid_observer_ns",
    "review_governed_lease_nonce",
})


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


def diagnose_repository_start(path: str | Path) -> dict[str, str]:
    """Distinguish a normal non-Git folder from a broken/unavailable Git probe."""
    candidate = Path(path).expanduser().resolve(strict=False)
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env.pop("LANGUAGE", None)
    try:
        result = subprocess.run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                                text=True, capture_output=True, timeout=60, env=env)
        if result.returncode == 0:
            bare = subprocess.run(["git", "-C", str(candidate), "rev-parse", "--is-bare-repository"],
                                  text=True, capture_output=True, timeout=60, env=env)
            if bare.returncode or bare.stdout.strip() == "true":
                return {"state": "error", "detail": (bare.stderr or "bare Git repository").strip()}
            return {"state": "git", "root": result.stdout.strip()}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
    stderr = result.stderr or ""
    if ("not a git repository (or any" in stderr.lower()
            and not os.path.lexists(candidate / ".git")):
        return {"state": "non-git", "detail": stderr}
    return {"state": "error", "detail": stderr or f"git exited {result.returncode}"}


def validated_unversioned_route(path: str | Path, *, environ=None) -> dict | None:
    """Return a currently valid bound unversioned route, otherwise no authority."""
    if diagnose_repository_start(path).get("state") != "non-git":
        return None
    env = os.environ if environ is None else environ
    route_file = env.get("AGENT_ROUTE_FILE") or env.get("AGENT_OWNER_ROUTE_FILE")
    if not route_file:
        return None
    try:
        route_path = Path(route_file).expanduser().resolve(strict=True)
        route = json.loads(route_path.read_text(encoding="utf-8"))
        sealed = _route_module().verify_route(route, expected_cwd=str(path))
        if sealed.get("source_commit") != "unversioned":
            return None
        expected_id = env.get("AGENT_ROUTE_ID") or env.get("AGENT_OWNER_ROUTE_ID")
        expected_hash = env.get("AGENT_ROUTE_HASH") or env.get("AGENT_OWNER_ROUTE_HASH")
        if expected_id and expected_id != sealed.get("route_id"):
            return None
        if expected_hash and expected_hash != sealed.get("route_hash"):
            return None
        return sealed
    except (OSError, ValueError, TypeError, DispatchContractError, json.JSONDecodeError):
        return None


def work_root_identity(path: str | Path, *, route: dict | None = None,
                       route_file: str | Path | None = None,
                       allow_sealed_repo_fallback: bool = False) -> str:
    """Git toplevel identity, or canonical cwd for a validated unversioned route."""
    candidate = Path(path).expanduser().resolve(strict=False)
    if route_file:
        return launch_repository_identity(candidate, route_file)[0]
    if isinstance(route, dict) and route.get("source_commit") == "unversioned":
        try:
            sealed = _route_module().verify_route(route, expected_cwd=str(candidate))
        except (ValueError, TypeError) as exc:
            raise DispatchContractError("non-git-route-identity-unbound", str(exc)) from exc
        if sealed.get("source_commit") == "unversioned":
            return str(candidate)
        raise DispatchContractError("non-git-route-identity-unbound")
    if isinstance(route, dict):
        try:
            result = subprocess.check_output(
                ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
                text=True, stderr=subprocess.DEVNULL,
            ).strip()
            if result:
                return str(Path(result).resolve(strict=False))
        except (OSError, subprocess.SubprocessError) as exc:
            if allow_sealed_repo_fallback and route.get("repo"):
                diagnosis = diagnose_repository_start(candidate)
                if diagnosis.get("state") in {"error", "non-git"}:
                    return str(Path(route["repo"]).expanduser().resolve(strict=False))
            raise DispatchContractError("repository-identity-unavailable", str(exc)) from exc
        raise DispatchContractError("repository-identity-unavailable", "empty Git toplevel")
    diagnosis = diagnose_repository_start(candidate)
    if diagnosis["state"] == "git":
        return str(Path(diagnosis["root"]).resolve(strict=False))
    if diagnosis["state"] != "non-git":
        raise DispatchContractError("repository-identity-unavailable", diagnosis.get("detail", ""))
    sealed = validated_unversioned_route(candidate)
    if not isinstance(sealed, dict) or sealed.get("source_commit") != "unversioned":
        raise DispatchContractError("non-git-route-identity-unbound")
    return str(candidate)


def launch_repository_identity(path: str | Path, route_file: str | Path | None) -> tuple[str, str]:
    """Resolve launch identity while admitting only a validated artifact-only non-Git route."""
    candidate = Path(path).expanduser().resolve(strict=False)
    diagnosis = diagnose_repository_start(candidate)
    if diagnosis["state"] == "git":
        return str(Path(diagnosis["root"]).resolve(strict=False)), "git"
    if diagnosis["state"] != "non-git" or not route_file:
        raise DispatchContractError("not-a-git-worktree", diagnosis.get("detail", ""))
    try:
        route = json.loads(Path(route_file).expanduser().read_text(encoding="utf-8"))
        sealed = _route_module().verify_route(route, expected_cwd=str(candidate))
        if sealed.get("source_commit") != "unversioned":
            raise ValueError("route is not sealed unversioned")
        if _route_module()._isolates_worktree(sealed):
            raise ValueError("source-mutating route")
        expected_id = os.environ.get("AGENT_ROUTE_ID") or os.environ.get("AGENT_OWNER_ROUTE_ID")
        expected_hash = os.environ.get("AGENT_ROUTE_HASH") or os.environ.get("AGENT_OWNER_ROUTE_HASH")
        if expected_id and expected_id != sealed.get("route_id"):
            raise ValueError("route id binding mismatch")
        if expected_hash and expected_hash != sealed.get("route_hash"):
            raise ValueError("route hash binding mismatch")
    except (OSError, ValueError, TypeError, DispatchContractError, json.JSONDecodeError) as exc:
        raise DispatchContractError("non-git-route-invalid", str(exc)) from exc
    return str(candidate), "non-git"


ATTEMPT_DESCENDANT_ENV = "AGENT_DISPATCH_ATTEMPT_ID"


def governance_environment(environ=None):
    """Copy an environment for governance helpers without the caller attempt tag."""
    source = os.environ if environ is None else environ
    result = dict(source)
    result.pop(ATTEMPT_DESCENDANT_ENV, None)
    return result


ATTEMPT_DESCENDANT_PROOF = "attempt-tagged-empty-v1"
# SD-OPEN-47 (H7): the detached drain observer seals this instead of the
# empty proof when the governed leader and its process group are gone, the
# attempt already holds semantic terminal evidence (terminal row status or a
# final runtime envelope), and tagged residue -- a worker-spawned background
# shell, a `herdr agent wait`, anything that inherited the tag and outlived
# the worker -- still survives past the residue grace. The residue pids are
# recorded beside it; they are leftovers of a finished worker, never the
# worker, so they must not veto quiescence forever.
ATTEMPT_DESCENDANT_RESIDUE_PROOF = "attempt-tagged-residue-v1"
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
# A runtime that retries one of these itself keeps to the reap watcher's own
# post-exit drain bounds: the backoff ceiling and the grace before it gives up.
PRELAUNCH_WAIT_GRACE_SECONDS = 30.0
PRELAUNCH_WAIT_INTERVAL_MAX_SECONDS = 2.0

# SD-154/B-2 (defect #2): every reason that means "this route's own recorded
# state needs a person or a `revise` call, not another launch attempt". A
# fallback surface that reads one of these off a wrapper's failure output
# stops with `child_spawned=0` instead of descending to an inline hop --
# `runtime-unavailable` was never true for the luna route this defect
# observed (13.59.3 "관측"), and a new member here must be added by name so
# an unclassified reason cannot silently fall through to the descent path.
ROUTE_STATE_REFUSAL_REASONS = (
    "unsafe-git-operation",
    "unsafe-git-state",
    "completion-marker-missing",
    "completion-marker-integrity-broken",
    "completion-evidence-superseded",
    "completion-evidence-revised-unrecorded",
    "review-round-budget-exhausted",
    "review-subject-unchanged",
    "review-verdictless-bound",
    "round-unsettled",
    "resource-owner-stage-admission-blocked",
)

# Receipts and the ledger carry the same bounded facts, never a clipped copy
# of a child's output. Keep this at their shared boundary, including callers
# which write candidate evidence directly without going through fallback.
CANDIDATE_PAYLOAD_BYTES = 32 * 1024
_DIAGNOSTIC_COUNT_MAX = 2**31 - 1
_DIAGNOSTIC_DETAILS = frozenset({
    "", "merge", "rebase", "cherry-pick", "HEAD cannot be resolved",
    "wrapper timed out", "capacity retry was stopped", "capacity death",
    "existing retry is terminal", "existing retry has no attempt ID",
    "duplicate attempt claimed", "duplicate claim has no row",
    "open", "running", "done", "unknown", "watchdog fallback",
    "the call was stopped, so the worker was stopped with it",
    "sealed parent identity is not the live dispatch-depth-1 owner",
    "OSError", "ValueError", "TimeoutExpired", "PermissionError", "FileNotFoundError",
    "[detail omitted]", "detached HEAD",
})


def diagnostic_line(value: object, limit: int = 512) -> str:
    """A display line cannot introduce receipt keys or terminal controls."""
    if not isinstance(value, str):
        return ""
    value = "".join(" " if unicodedata.category(ch).startswith("C") else ch
                    for ch in value[:limit * 2])
    return " ".join(value.split())[:limit]


def diagnostic_token(value: object, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    value = value[:limit]
    return value if re.fullmatch(r"[A-Za-z0-9._:/-]*", value) else ""


# The one line the projection check reports for a failed link: `check=<name>:failed
# reason=<token>` (the token may name the expected release path), or `exit-<n>`.
_PROJECTION_DETAIL = re.compile(
    r"check=[A-Za-z0-9._:-]{1,64}:failed reason=[A-Za-z0-9._:/-]{1,240}|exit-[0-9]{1,3}")


def candidate_detail(reason: str, value: object) -> str:
    """Only finite local facts may persist; unknown free text is omitted."""
    if not isinstance(value, str):
        return ""
    if value in _DIAGNOSTIC_DETAILS:
        return value
    if reason == "unsafe-git-state" and value.startswith("detached HEAD in "):
        return "detached HEAD"
    if reason == "codex-runtime-projection-mismatch" and _PROJECTION_DETAIL.fullmatch(value):
        return value
    return "[detail omitted]"


def candidate_count(value: object) -> int:
    return min(max(value, 0), _DIAGNOSTIC_COUNT_MAX) if type(value) is int else 0


def bounded_candidate_outcomes(values: object, omitted: object = 0) -> tuple[list, int]:
    """Normalize untrusted candidate input and bound its serialized size."""
    count = candidate_count(omitted)
    if not isinstance(values, list):
        return [], count
    count = candidate_count(count + max(0, len(values) - 32))
    rows = []
    for item in values[:32]:
        if not isinstance(item, dict):
            count = candidate_count(count + 1)
            continue
        attempted = item.get("attempted") is True
        reason = diagnostic_token(item.get("reason", ""))
        exit_code = item.get("exit", "")
        if type(exit_code) is int:
            exit_code = str(exit_code) if -999 <= exit_code <= 999 else ""
        exit_code = diagnostic_token(exit_code, 16)
        rows.append({
            "ordinal": (item["ordinal"] if type(item.get("ordinal")) is int
                        and 0 <= item["ordinal"] <= _DIAGNOSTIC_COUNT_MAX else 0),
            "tuple": diagnostic_token(item.get("tuple", "")),
            "attempted": attempted,
            "attempt_id": diagnostic_token(item.get("attempt_id", ""), 120) if attempted else "",
            "exit": exit_code if attempted else "",
            "reason": reason,
            "detail": candidate_detail(reason, item.get("detail", "")),
        })
    # ensure_ascii=True is the larger of the receipt and ledger encodings.
    while len(json.dumps(rows).encode("utf-8")) > CANDIDATE_PAYLOAD_BYTES:
        rows.pop()
        count = candidate_count(count + 1)
    return rows, count


def route_state_next_action(reason: str, detail: str, route_file: str | None,
                             node, *, evidence: str | None = None) -> str:
    """The supported next action a route-state refusal carries (13.59.3 rule 6).

    ``node`` may be a node dict or a bare node id string; only its id is used.
    ``detail`` is the raiser's detail string (often the dependency node id for
    `completion-marker-missing`/`completion-evidence-revised-unrecorded`).
    ``evidence``, when the caller holds the marker, fills the evidence path.
    """
    node_id = node.get("id") if isinstance(node, Mapping) else node
    target = detail or node_id or "<node>"
    route_arg = f"--route {route_file}" if route_file else "--route <route-file>"
    evidence_arg = shlex.quote(evidence) if evidence else "<evidence-path>"
    if reason == "completion-evidence-revised-unrecorded":
        return (
            f"capability-route.py revise {route_arg} --node {target} "
            f"--evidence {evidence_arg} "
            "--basis <review-findings|user-direction|owner-correction> "
            "[--answers <downstream review/test verdict attempt ids>] "
            "[--direction <gate-release-id|memo-path>] [--reason <text>] "
            "--author-attempt-id <attempt that made the change>"
        )
    if reason == "completion-marker-missing":
        return (
            f"wait for node {target} to publish a completion marker, or if its "
            f"gate evidence already changed run: capability-route.py revise {route_arg} "
            f"--node {target} --evidence {evidence_arg} "
            "--basis review-findings|user-direction|owner-correction"
        )
    if reason == "completion-evidence-superseded":
        return (
            f"rerun dependency node {target} to publish fresh completion evidence "
            "after its upstream revision, then retry this launch"
        )
    if reason == "completion-review-subject-unknown":
        return (
            f"rerun review node {target} with its existing start to publish a PASS "
            "bound to the current inputs, then retry this launch"
        )
    if reason == "review-round-budget-exhausted":
        return "close the node via owner-closure (review) or report the FAIL and open an SD-116 continuation (test)"
    if reason == "review-subject-unchanged":
        return "correct the blocking findings together, then repeat the same start for the closure check"
    if reason == "review-verdictless-bound":
        return "route the round through the sealed fallback chain's remaining unregistered hop (native-subagent, then owner-inline)"
    if reason == "round-unsettled":
        return "wait for the live round to settle before starting the next one"
    if reason == "resource-owner-stage-admission-blocked":
        return ("inspect the typed resource route-state reason and next_action; "
                "use the current route owner's existing start/correct path")
    return "-"


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


def owner_closure_shape(metadata: Mapping[str, object]) -> str | None:
    """The one judgement of which owner-closure a marker (or row) records.

    B-1 (SD-153 defect #1): a review the owner ruled over exists in two
    marker shapes that used to be told apart by separate call sites each
    re-deriving the same fact -- `_marker_identity_row`'s exact_terminal
    branch and `completion_attempt_readiness` now both call this instead of
    repeating `marker.get("stage_authority") == "owner-closure"`.
    `completed_marker_verdict_contradicts` reads the registry ROW, not the
    marker, and keeps its own narrower `gate_closure == "owner-closure"`
    check deliberately: a row can carry that one field (and be exempt from
    the FAIL/`completed-marker` contradiction) without the fuller
    `review_gate_closure`/`review_independence` pair a real `_complete_node_
    locked` write always adds alongside it.

    ``"continuation"``: a synthetic marker `capability-route.py`'s
    `_publish_continuation_owner_closure` writes for a continuation's own
    node, identified by `stage_authority == "owner-closure"`.

    ``"registered-review"``: the *same* review-worker row that recorded a
    real blocking verdict, closed in place by the owner instead of a third
    registered round -- `resolve_review_identity`'s `owner_override` branch
    stamps `review_gate_closure == "owner-closure"` and
    `review_independence == "owner-overridden"` onto the marker (and, via
    `_complete_node_locked`, onto the row).

    ``None`` for an ordinary marker/row -- not owner-closed at all.
    """
    if metadata.get("stage_authority") == "owner-closure":
        return "continuation"
    if (metadata.get("review_gate_closure") == "owner-closure"
            and metadata.get("review_independence") == "owner-overridden"):
        return "registered-review"
    return None


def row_has_attempt(pipe: str, attempt_id: str) -> bool:
    # A row of this attempt holds the literal `attempt_id=<id>`; every other row
    # is ruled out by that text alone, before its metadata is parsed.
    if f"attempt_id={attempt_id}" not in pipe:
        return False
    metadata = parse_registry_metadata(pipe)
    return metadata.get("attempt_id") == attempt_id


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

# The one definition of "this row's note says it succeeded". It lives in this
# module because `dispatch_completion_join` imports this one, never the reverse.
# Consumers that ask "did this attempt succeed?" use this set; the two that ask
# "*which producer* closed this row" (SD-94 marker eligibility and
# legacy terminal proof below) keep their narrow literal on purpose.


def row_is_subsession(metadata: dict[str, str]) -> bool:
    """One definition of "this row is a sub-session slice, not a stage owner"."""

    return bool(metadata.get("subsession_id")) or str(
        metadata.get("stage_authority", "1")
    ).lower() in {"0", "false"}


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


def _terminal_delivery_receipt(metadata: dict[str, str]) -> dict:
    """The terminal writer and every carrier use this same semantic result."""
    outcome = committed_outcome("done", metadata)
    is_success = outcome in {"succeeded", "cancelled"}
    child = {
        "attempt_id": metadata.get("attempt_id", ""),
        "status": "done",
        "readiness": "ready",
        "harness": metadata.get("harness", ""),
        "required_action": "advance-completed" if is_success else "inspect-done-failure",
        "delivery_classification": "success" if is_success else "attention",
    }
    if not is_success:
        child["reason"] = "terminal-failure-or-unclosed"
    elif outcome == "cancelled":
        child["reason"] = "cancelled-by-parent"
    receipt = {
        "schema_version": 2,
        "state": "delivered",
        "parent_attempt_id": metadata.get("parent_attempt_id", ""),
        "job_registry": "",
        "children": [child],
        "delivery_classification": child["delivery_classification"],
    }
    return receipt


def _delivery_commit_proven(metadata: dict[str, str]) -> bool:
    """Validate the existing committed receipt without repeating worker checks.

    Write authorization and runtime classification were proved before close.
    Re-running those checks after a lease/cycle closes would reject a valid
    completion. Current cleanup, conflicts and row CAS remain separate duties.
    """
    if metadata.get("delivery_intent") != "1" or not metadata.get("parent_sid"):
        return False
    try:
        receipt = unseal_receipt(metadata.get("delivery_receipt_b64", ""))
    except ValueError:
        return False
    return (
        receipt == _terminal_delivery_receipt(metadata)
        and metadata.get("delivery_recipient_kind") == metadata.get("parent_completion_delivery")
        and metadata.get("delivery_recipient_digest") == hashlib.sha256(metadata["parent_sid"].encode()).hexdigest()
        and shared_receipt_digest(receipt) == metadata.get("delivery_receipt_digest")
        and receipt.get("delivery_classification") == "success"
    )


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
    receipt = _terminal_delivery_receipt(metadata)
    receipt_digest = shared_receipt_digest(receipt)

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


_NO_START_PROCESS_IDENTITY_KEYS = (
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
    "launch_cgroup_b64",
    "launch_cgroup_ns",
    "group_reap_proof",
    "group_reap_pgid",
    "attempt_descendant_proof",
    "attempt_descendant_observer_ns",
    "attempt_descendant_residue",
    "attempt_descendant_residue_count",
    "attempt_descendant_residue_basis",
    "attempt_descendant_residue_at",
    "reap_watch",
    "reap_watch_pid",
    "post_exit_receipt_substitute",
)


def _has_persisted_process_identity(metadata: Mapping[str, str]) -> bool:
    """Any child identity or descendant/reap evidence contradicts no-start."""
    return any(metadata.get(key) for key in _NO_START_PROCESS_IDENTITY_KEYS)


def attempt_row_never_started(fields: list[str]) -> bool:
    """Prove that a row has never crossed the durable launch fence."""

    if len(fields) != 6:
        return False
    metadata = parse_registry_metadata(fields[5])
    # A claimed row, any persisted child identity, and a reaped-before-publish
    # row remain supervisor-reconciliation evidence. Parent lineage and
    # registration metadata are deliberately not execution identity.
    return (
        metadata.get("launch_started") != "1"
        and metadata.get("launch_claimed") == "0"
        and not _has_persisted_process_identity(metadata)
        and metadata.get("launch_outcome", "") in {"", "never-launched"}
    )
