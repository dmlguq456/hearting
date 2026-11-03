"""Portable decisions over a committed attempt and its execution evidence.

This module owns no processes or storage. Callers provide the exact registry
snapshot and the shared process proof; runtime labels never affect the policy.
The semantic result is kept separate from the action still owed. In particular,
success with incomplete cleanup remains success with a cleanup obligation.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from typing import Mapping

OPEN_STATES = frozenset({"open", "running"})
TERMINAL_STATES = frozenset({"done", "killed", "cancelled"})
SUBSESSION_NOTE = "completed-subsession"
REVIEW_BLOCKING_NOTE = "completed-review-blocking"
RETAINED_REVIEW_RESOLUTION = "retained-independent-review-v1"
SUCCESS_NOTES = frozenset({"completed-marker", "completed-supervisor", SUBSESSION_NOTE})

# A completion-budget row closed typed-deferred (not `dead-*`, not a plain
# success note) because the transport that would have proved success timed
# out repeatedly, never because the work itself failed. `SUCCESS_NOTES`
# deliberately excludes this note: without a published marker the row is not
# yet a semantic success, so every consumer must go through
# `deferred_completion`/`verdict_pass`/`success_note` instead of reading
# `note`/`failure_class` directly.
DEFERRED_COMPLETION_NOTE = "completion-deferred"
DEFERRED_COMPLETION_SOURCE = "registered-wrapper-completion-transient-v1"


def terminal_conflict_identity(metadata: Mapping[str, str]) -> str:
    keys = ("note", "failure_class", "classifier_source", "conflicting_terminal_note",
            "conflicting_failure_class", "conflicting_classifier_source")
    value = {key: metadata.get(key, "") for key in keys}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def terminal_conflicts(metadata: Mapping[str, str]) -> dict:
    """All contradictory observations and their exact review dispositions.

    Keeping only the latest observation loses earlier unresolved evidence,
    and keeping only the latest review reopens old, already reviewed evidence.
    Both belong to the same attempt-owned record.
    """
    encoded = metadata.get("terminal_conflicts_b64", "")
    if encoded:
        value = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(value, dict) or not value or any(
            not isinstance(key, str) or not isinstance(entry, dict)
            for key, entry in value.items()
        ):
            raise ValueError("terminal-conflicts-invalid")
        return value
    if metadata.get("terminal_conflict") == "1":
        return {terminal_conflict_identity(metadata): {
            "note": metadata.get("conflicting_terminal_note", ""),
            "failure_class": metadata.get("conflicting_failure_class", ""),
            "classifier_source": metadata.get("conflicting_classifier_source", ""),
        }}
    return {}


def _conflict_reviewed(entry: Mapping[str, str]) -> bool:
    # Legacy readers only know review_sha256. Keep it absent for a new
    # semantic recovery so those readers retain the hold instead of PASS.
    return bool(entry.get("review_sha256") or (
        entry.get("resolution") == RETAINED_REVIEW_RESOLUTION
        and entry.get("resolved_failure_class") == "blocked"
        and entry.get("resolution_review_sha256") and entry.get("recovery_marker_sha256")))


def terminal_conflict_digest(metadata: Mapping[str, str]) -> str:
    pending = sorted(key for key, entry in terminal_conflicts(metadata).items()
                     if not _conflict_reviewed(entry))
    return hashlib.sha256(json.dumps(pending).encode()).hexdigest()


def terminal_conflict_pending(metadata: Mapping[str, str]) -> bool:
    return any(not _conflict_reviewed(entry) for entry in terminal_conflicts(metadata).values())


def deferred_completion(metadata: Mapping[str, str]) -> str:
    """Whether this row was closed by the completion budget's own deferral.

    ``""`` when the row was never deferred (an ordinary terminal writer
    closed it). ``"pending"`` once the budget closed it typed-deferred but no
    marker has been published for it yet. ``"completed"`` once the marker
    exists -- the only state in which a deferred row is a semantic success.
    This is the one place that reads ``classifier_source``/``completion_marker``
    to answer that question; every other consumer goes through this function
    (or `verdict_pass`/`success_note`, which call it) instead of re-deriving it.
    """
    if metadata.get("classifier_source") != DEFERRED_COMPLETION_SOURCE:
        return ""
    return "completed" if metadata.get("completion_marker") else "pending"


def reviewed_blocked_completion(metadata: Mapping[str, str]) -> bool:
    """A reviewed bad success retains its historical row but never grants PASS."""
    return any(entry.get("resolution") == RETAINED_REVIEW_RESOLUTION
               and _conflict_reviewed(entry) and entry.get("resolved_failure_class") == "blocked"
               for entry in terminal_conflicts(metadata).values())


def verdict_pass(metadata: Mapping[str, str]) -> bool:
    """The one place a caller asks "did this attempt succeed"."""
    if reviewed_blocked_completion(metadata):
        return False
    state = deferred_completion(metadata)
    if state:
        return state == "completed"
    return metadata.get("failure_class") == "pass"


def readable_result(metadata: Mapping[str, str]) -> str:
    """``"FAIL"`` or ``"BLOCKED"`` when the worker's own final envelope said so, else ``""``.

    That is the worker's result for its owner to act on, never a transport
    death: only a death, a capacity stop or a runtime error is retried
    automatically (ROUTE-AUTHORITY RA-4). A reviewer's finished FAIL is
    `completed-review-blocking` (its readable review artifact); a bare
    `dead-worker-fail` on a review row stays a death, as the round census reads it.
    """
    if reviewed_blocked_completion(metadata):
        return "BLOCKED"
    note, failure_class = metadata.get("note", ""), metadata.get("failure_class")
    if note == "dead-worker-fail" and failure_class == "fail" and metadata.get("worker_type") != "review":
        return "FAIL"
    if note == "dead-worker-blocked" and failure_class == "blocked":
        return "BLOCKED"
    return ""


def success_note(metadata: Mapping[str, str]) -> bool:
    """The one place a caller asks "does this row's note read as success"."""
    if reviewed_blocked_completion(metadata):
        return False
    state = deferred_completion(metadata)
    if state:
        return state == "completed"
    return metadata.get("note", "") in SUCCESS_NOTES


def committed_outcome(status: str, metadata: Mapping[str, str]) -> str:
    """Interpret only a committed terminal row, never cached watchdog output."""
    if status in OPEN_STATES:
        return "pending"
    if status not in TERMINAL_STATES:
        return "unknown"
    if reviewed_blocked_completion(metadata):
        return "failed"
    note = metadata.get("note", "")
    if note in {"cancelled-by-parent", "fleet-kill"} and metadata.get("failure_class") == "cancelled":
        return "cancelled"
    if note == REVIEW_BLOCKING_NOTE:
        return "review-blocked"
    if deferred_completion(metadata) == "pending":
        return DEFERRED_COMPLETION_NOTE
    if success_note(metadata) or verdict_pass(metadata):
        return "succeeded"
    if note.startswith("dead-") or status in {"killed", "cancelled"}:
        return "failed"
    return "unknown"


@dataclass(frozen=True)
class AttemptDecision:
    outcome: str
    action: str
    responsible: str
    reason: str
    retry_kind: str = ""

    @property
    def retry_allowed(self) -> bool:
        return bool(self.retry_kind) and self.action == "inspect-failure"


@dataclass(frozen=True)
class RegisteredWorkObservation:
    """One exact registered attempt plus its existing policy and closure evidence."""

    attempt_id: str
    decision: AttemptDecision
    process_state: str
    evidence_state: str
    owner_completion_state: str = "settled"
    cleanup_state: str = "settled"
    provenance: tuple[tuple[str, str], ...] = ()
    pane_relation: str = "executor"
    delivery_state: str = "unknown"


@dataclass(frozen=True)
class PaneObservation:
    """Native pane evidence; it does not invent a registered attempt or result."""

    server: str
    pane: str
    harness: str
    session_id: str
    pid_birth: str
    identity_verified: bool
    native_turn: str
    form_state: str = "unknown"
    draft_state: str = "unknown"
    bound_work: tuple[RegisteredWorkObservation, ...] = ()
    bindings_state: str = "observed"
    provenance: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CompletionReadiness:
    """Shared readiness projection; outcome, next action and observation source stay distinct."""

    state: str
    scope: str
    outcome: str | None
    next_owed: str
    reason: str
    provenance: tuple[tuple[str, str], ...] = ()
    identity: tuple[tuple[str, str], ...] = ()
    form_state: str = "unknown"
    draft_state: str = "unknown"


_SETTLED_WORK_OUTCOMES = frozenset({
    "succeeded", "failed", "review-blocked", "cancelled",
})
_NATIVE_READY_TURNS = frozenset({"idle", "done"})
_NATIVE_TURN_STATES = frozenset({
    "idle", "done", "busy", "working", "finalizing", "blocked", "unknown",
})


def _registered_readiness(observation: RegisteredWorkObservation) -> CompletionReadiness:
    decision = observation.decision
    provenance = (("attempt_id", observation.attempt_id),
                  ("process_state", observation.process_state),
                  ("evidence_state", observation.evidence_state),
                  ("owner_completion_state", observation.owner_completion_state),
                  ("cleanup_state", observation.cleanup_state),
                  ("pane_relation", observation.pane_relation),
                  ("delivery_state", observation.delivery_state),
                  *observation.provenance)
    if (not observation.attempt_id or observation.owner_completion_state not in
            {"settled", "pending", "unknown"} or observation.cleanup_state not in
            {"settled", "pending", "unknown"}):
        return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                   decision.action, "registered-observation-invalid",
                                   provenance)
    if observation.pane_relation == "parent":
        if observation.delivery_state not in {
                "unknown", "pending", "claimed", "queued", "unverified",
                "sent-ambiguous", "acked", "not-required", "rejected", "expired"}:
            return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                       decision.action, "registered-observation-invalid",
                                       provenance)
        if decision.action == "inspect-conflict" or decision.outcome == "unknown":
            return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                       decision.action, decision.reason, provenance)
        if decision.action in {"wait", "reconcile", "complete", "recover",
                               "cancel", "settle-cancellation"}:
            state = "unknown" if decision.action == "recover" else "pending"
            return CompletionReadiness(state, "registered-work", decision.outcome,
                                       decision.action, decision.reason, provenance)
        if (decision.action in {"advance", "review", "inspect-failure"}
                and decision.outcome in _SETTLED_WORK_OUTCOMES):
            if observation.delivery_state in {"pending", "claimed", "queued",
                                               "unverified", "sent-ambiguous"}:
                return CompletionReadiness("pending", "registered-work", decision.outcome,
                                           decision.action, "bound-registered-work-pending",
                                           provenance)
            if observation.delivery_state == "unknown":
                return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                           decision.action, "registered-closure-unknown",
                                           provenance)
            if observation.delivery_state in {"rejected", "expired"}:
                # One readable delivery record says that notice ended; the settled result itself
                # stays in the registry row and marker, so nothing is lost by calling it known.
                return CompletionReadiness("ready", "registered-work", decision.outcome,
                                           decision.action,
                                           f"registered-delivery-ended:{observation.delivery_state}", provenance)
            return CompletionReadiness("ready", "registered-work", decision.outcome,
                                       decision.action, decision.reason, provenance)
    if observation.owner_completion_state == "unknown" or observation.cleanup_state == "unknown":
        return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                   decision.action, "registered-closure-unknown",
                                   provenance)
    if observation.owner_completion_state != "settled" or observation.cleanup_state != "settled":
        return CompletionReadiness("pending", "registered-work", decision.outcome,
                                   decision.action, "registered-follow-through-pending",
                                   provenance)
    if decision.action == "inspect-conflict" or decision.outcome == "unknown":
        return CompletionReadiness("unknown", "registered-work", decision.outcome,
                                   decision.action, decision.reason, provenance)
    if (decision.action in {"advance", "review", "inspect-failure"}
            and decision.outcome in _SETTLED_WORK_OUTCOMES):
        return CompletionReadiness("ready", "registered-work", decision.outcome,
                                   decision.action, decision.reason, provenance)
    if decision.action in {"wait", "reconcile", "complete", "recover",
                           "cancel", "settle-cancellation"}:
        state = "unknown" if decision.action == "recover" else "pending"
        return CompletionReadiness(state, "registered-work", decision.outcome,
                                   decision.action, decision.reason, provenance)
    return CompletionReadiness("unknown", "registered-work", decision.outcome,
                               decision.action, "registered-action-unclassified",
                               provenance)


def completion_readiness(
    observation: RegisteredWorkObservation | PaneObservation,
    *, purpose: str = "completion",
) -> CompletionReadiness:
    """Project registered work or a native pane into one provenance-preserving result.

    Native idle/done means only that the interactive turn is ready. A bound
    registered attempt is evaluated through its already computed policy decision
    and can keep the pane pending or unknown. Input considers only executor
    bindings; completion and retirement also consider registered children.
    """
    if purpose not in {"completion", "input"}:
        raise ValueError("readiness-purpose-invalid")
    if isinstance(observation, RegisteredWorkObservation):
        return _registered_readiness(observation)
    if not isinstance(observation, PaneObservation):
        raise TypeError("completion-readiness-observation-invalid")
    identity = (("server", observation.server), ("pane", observation.pane),
                ("harness", observation.harness), ("session_id", observation.session_id),
                ("pid_birth", observation.pid_birth))
    provenance = (("native_turn", observation.native_turn),
                  ("bindings_state", observation.bindings_state),
                  *observation.provenance)
    # A harness that has not assigned a session yet (a fresh Codex before its
    # first input) is identified by server, pane and shell birth alone; no
    # registered work can be bound to it.
    required = (identity if observation.session_id or observation.bound_work
                else tuple(pair for pair in identity if pair[0] != "session_id"))
    if (not observation.identity_verified or any(not value for _, value in required)):
        return CompletionReadiness("unknown", "native-turn", None, "observe-identity",
                                   "pane-identity-unverified", provenance, identity,
                                   observation.form_state, observation.draft_state)
    if observation.bindings_state != "observed":
        return CompletionReadiness("unknown", "native-turn", None, "observe-bound-work",
                                   "registered-bindings-unavailable", provenance, identity,
                                   observation.form_state, observation.draft_state)
    if observation.native_turn not in _NATIVE_TURN_STATES:
        return CompletionReadiness("unknown", "native-turn", None, "observe-native-turn",
                                   "native-turn-unclassified", provenance, identity,
                                   observation.form_state, observation.draft_state)
    if observation.native_turn not in _NATIVE_READY_TURNS:
        state = "unknown" if observation.native_turn == "unknown" else "pending"
        turn = "busy" if observation.native_turn == "working" else observation.native_turn
        return CompletionReadiness(state, "native-turn", None, "wait-native-turn",
                                   "native-turn-" + turn, provenance,
                                   identity, observation.form_state, observation.draft_state)
    work_results = [completion_readiness(item) for item in observation.bound_work
                    if purpose == "completion" or item.pane_relation != "parent"]
    if any(result.state == "unknown" for result in work_results):
        return CompletionReadiness("unknown", "native-turn", None, "resolve-bound-work",
                                   "bound-registered-work-unknown", provenance + tuple(
                                       pair for result in work_results for pair in result.provenance),
                                   identity, observation.form_state, observation.draft_state)
    if any(result.state != "ready" for result in work_results):
        return CompletionReadiness("pending", "native-turn", None, "finish-bound-work",
                                   "bound-registered-work-pending", provenance + tuple(
                                       pair for result in work_results for pair in result.provenance),
                                   identity, observation.form_state, observation.draft_state)
    outcomes = {result.outcome for result in work_results}
    outcome = next(iter(outcomes)) if len(outcomes) == 1 and len(work_results) == 1 else None
    next_owed = (work_results[0].next_owed if len(work_results) == 1
                 else "native-turn")
    return CompletionReadiness("ready", "native-turn", outcome, next_owed,
                               "native-turn-ready", provenance + tuple(
                                   pair for result in work_results for pair in result.provenance),
                               identity, observation.form_state, observation.draft_state)


def decide_attempt(status: str, metadata: Mapping[str, str], *, process_state: str,
                   process_reason: str = "", terminal_observed: bool = False,
                   result_state: str | None = None) -> AttemptDecision:
    from route_authority import decide_continuation
    return decide_continuation(action="attempt", status=status, metadata=metadata,
        facts=dict(process_state=process_state, process_reason=process_reason,
                   terminal_observed=terminal_observed, result_state=result_state)).census


def required_action(status: str, metadata: Mapping[str, str]) -> str:
    from route_authority import decide_continuation
    return decide_continuation(action="attempt-action", status=status, metadata=metadata).next_action
