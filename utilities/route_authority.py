#!/usr/bin/env python3
"""Route authority: the one place that answers four questions about a route.

1. Who continues it (the parent session and its successor).
2. On which harness (sealed selection pins and the parent's recorded changes).
3. How many attempts of which kind (sub-session standing, retry links,
   round budget, the result envelope).
4. What it may access (execution access grant).

Every judgment here used to be made at its call site, several of them in
more than one copy. The old names stay where they were, as imports or thin
wrappers, so existing callers and patches keep working. Recovery separates
stored work from current selection; consumers do not rebuild historical inputs.

Top-level imports stay light (stdlib and two policy modules) so that
`dispatch_contract`, `model_profile` and the adapters can import this module
without a cycle; heavier collaborators are imported where they are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Mapping, NamedTuple

from dispatch_attempt_policy import committed_outcome, readable_result
import review_round_cap as _ROUND
from session_identity import identity as session_identity, session_label


def _contract_error(reason, detail=""):
    from dispatch_contract import DispatchContractError
    return DispatchContractError(reason, detail)


class ContinuationDecision(NamedTuple):
    allowed: bool
    state: str
    reason: str = ""
    next_action: str = "continue"
    retry_when: str = ""
    detail: str = ""
    budget: object = None
    answers: tuple = ()
    fallback_nodes: tuple = ()
    census: object = None


PROVEN_BLOCKED_GATE_REASONS = frozenset({
    "completion-attempt-not-current", "completion-evidence-revised-unrecorded",
})


def _replacement_kind(status, meta, *, jobs=None, lines=None):
    import dispatch_replacement as DR
    import route_parent_close
    if (meta.get('parent_close_requested') == '1' or
            jobs is not None and route_parent_close.row_requested(meta, jobs)):
        return None
    if meta.get('worker_type') == 'owner' and DR.never_started(status, meta):
        # The launch fence is the execution boundary. A reservation may carry a
        # claim, but its absence of launch_started means it has no worker result.
        return 'unlaunched'
    if jobs is not None and meta.get('note') == 'dead-worker-blocked':
        found = DR.owner_parked_gate(jobs, meta.get('attempt_id'), lines=lines)
        if found and found['status'] == 'proceed':
            return 'parked'
        if found is not None:
            return None  # a declared gate keeps its own answer path
    if (jobs is not None and meta.get('worker_type') == 'owner'
            and answerable_owner_end(status, meta)
            and DR._retained_corrections(jobs, meta.get('attempt_id'))):
        return DR.CORRECTED  # an answered stop is a pause; no automatic retry of an unanswered FAIL
    if (jobs is not None and runtime_owner_can_resume(status, meta)
            and DR._retained_corrections(jobs, meta.get('attempt_id'))):
        return DR.CORRECTED
    if (meta.get('note') == 'cancelled-receipt-unavailable'
            and meta.get('classifier_source') == DR.DC.AUTOMATIC_RECEIPTLESS_CLASSIFIER):
        return 'silent'
    if (meta.get('worker_type') == 'owner'
            and meta.get('note') == 'dead-governor-reservation-transfer'):
        # This terminal disposition means the replacement never launched. If
        # its exact cleanup proof is missing, it is unknown rather than a new
        # silent-death edge; the branch above already returned `unlaunched`
        # when the complete proof was available.
        return None
    if meta.get('note') in DR.DEATH_NOTES and status not in {'cancelled', 'killed'}:
        return 'silent'
    if (meta.get('worker_type') == 'owner' and status == 'done'
            and (meta.get('note') == 'dead-capacity' or meta.get('failure_class') == 'capacity')):
        return 'capacity'
    if meta.get('worker_type') == 'owner' and status == 'done' and meta.get('note') in DR.RUNTIME_DEATH_NOTES:
        return 'runtime'  # the owner's runtime died under it; only an explicit `start` replaces it
    if (jobs is not None and meta.get('worker_type') == 'frame' and status == 'done'
            and (meta.get('note') == 'dead-capacity' or meta.get('failure_class') == 'capacity')
            and DR._frame_rule_top(jobs, meta)):
        return DR.FRAME_CAPACITY
    if (jobs is not None and meta.get('worker_type') == 'frame' and status == 'done'
            and (meta.get('note') == 'dead-capacity' or meta.get('failure_class') == 'capacity')
            and DR._frame_pair_rotation_verified(jobs, meta)):
        # A verified rotated alternative uses ordinary capacity recovery; it
        # does not claim the frame rule's top-to-deep transition.
        return 'capacity'
    return None


def _replacement_terminal_absent(fields, meta, *, capacity=False, runtime=False):
    import dispatch_replacement as replacement
    terminal = replacement.terminal_observation(fields, meta)
    return decide_continuation(action="replacement-result", facts={"terminal": terminal,
        "kind": "capacity" if capacity else "runtime" if runtime else "silent"}).allowed


def _attempt_decision(status: str, metadata: Mapping[str, str], *, process_state: str | None,
                      process_reason="", terminal_observed=False, result_state=None, record_only=False,
                      require_quiescent=False):
    """Select the attempt obligation and its record action from the same disposition.

    A record-only projection has no process observation and grants no execution.
    Runtime consumers keep committed results while observing their physical cleanup.
    """
    from dispatch_attempt_policy import (AttemptDecision, OPEN_STATES, TERMINAL_STATES,
        DEFERRED_COMPLETION_NOTE, REVIEW_BLOCKING_NOTE, terminal_conflict_pending)
    outcome = committed_outcome(status, metadata)
    def result(action, responsible, reason, retry="", record="inspect-done-failure"):
        return record if record_only else AttemptDecision(outcome, action, responsible, reason, retry)
    if not record_only:
        if status not in OPEN_STATES | TERMINAL_STATES:
            return result("recover", "supervision-controller", "registry-status-invalid")
        if metadata.get("parent_close_requested") == "1" and (not require_quiescent or
                outcome == "pending" and process_state == "quiescent"):
            return result("settle-cancellation" if outcome == "cancelled" else "cancel",
                          "execution-boundary", "cancelled-by-parent")
        if process_state == "live":
            return result("wait", "execution-boundary", process_reason or "process-alive")
        if process_state != "quiescent":
            return result("recover", "supervision-controller", process_reason or "process-unverifiable")
    if status in OPEN_STATES:
        if result_state == "unverifiable":
            return result("recover", "supervision-controller", "terminal-evidence-unverifiable", record="complete-open")
        return result("reconcile", "terminal-writer",
            "terminal-observed" if result_state == "settleable" or terminal_observed else
            "terminal-invalid" if result_state == "invalid" else "process-exited", record="complete-open")
    if terminal_conflict_pending(metadata):
        return result("inspect-conflict", "workflow-owner", "terminal-evidence-conflict",
                      record="advance-completed" if outcome == "cancelled" else "inspect-done-failure")
    if outcome == "cancelled":
        return result("inspect-failure", "workflow-owner", "terminal-outcome-unclassified", record="advance-completed")
    if outcome == "succeeded":
        return result("advance", "completion-controller", "registry-closed", record="advance-completed")
    if outcome == DEFERRED_COMPLETION_NOTE:
        return result("complete", "completion-controller", DEFERRED_COMPLETION_NOTE, record="complete-deferred")
    if outcome == "review-blocked":
        return result("review", "workflow-owner", REVIEW_BLOCKING_NOTE)
    if outcome == "failed":
        note = metadata.get("note", "")
        retry = ("" if status == "cancelled" or metadata.get("failure_class") == "cancelled" else
                 "capacity" if note == "dead-capacity" else "" if readable_result(metadata) else
                 "fallback" if note.startswith("dead-") else "")
        return result("inspect-failure", "workflow-owner", note or status, retry)
    return result("inspect-failure", "workflow-owner", "terminal-outcome-unclassified")


def _attempt_action(status: str, metadata: Mapping[str, str]) -> str:
    return _attempt_decision(status, metadata, process_state=None, record_only=True)


def _round_action(state, node_kind):
    review = node_kind == "review-worker"
    return {"admit": "dispatch", "blocked-live": "wait", "blocked-unsettled": "wait",
            "exhausted": "owner-report" if review else "report",
            "verdictless-bound": "native-subagent" if review else "owner-inline"}[state]


def completion_satisfied(state):
    return state in {"complete", "not-applicable"}


def require_continuation(decision):
    """Translate the one decision into the existing typed error protocol."""
    if not decision.allowed:
        error = _contract_error(decision.reason, decision.reason + ":" + decision.detail if decision.detail else "")
        error.retry_when = decision.retry_when
        error.next_action = decision.next_action
        raise error
    return decision


def decide_continuation(route=None, *, action, jobs=None, owner_attempt_id=None,
                        status=None, metadata=None, node=None, rows=(), revisions=(), facts=None):
    """Decide continuation from observations; never launch, wait or rewrite evidence.

    A round limit chooses a fallback, not a veto of its owner's remaining work.
    Unknown execution names the condition for replaying the same command. Transport,
    locking and immutable publication stay with the existing consumers.
    """
    route, facts, metadata = route or {}, facts or {}, metadata or {}
    if action in {"finish", "finish-registry"}:
        registered_reason = "finish-registered-route-ineligible" if facts.get("registered_route") else ""
        if action == "finish-registry":
            return ContinuationDecision(not registered_reason, "protected" if registered_reason else "continue", registered_reason)
    if action == "finish":
        nodes = route.get("nodes") or []
        shape = (len(nodes) == 1 and isinstance(nodes[0], dict) and nodes[0].get("id")
                 and nodes[0].get("execution_surface") == "inline"
                 and nodes[0].get("dispatch_depth") == 0 and nodes[0].get("registered_worker") is False
                 and nodes[0].get("terminal") is True)
        modern = bool(shape and route.get("effective_intensity") == "direct"
                      and nodes[0].get("id") == "inline" and nodes[0].get("kind") == "capability-owner")
        reason = ("finish-route-not-direct" if route.get("effective_intensity") != "direct"
                  else "finish-inline-owner-sentinel-required") if not modern and not (shape and facts.get("allow_legacy")) else ""
        reason = reason or facts.get("identity_error", "") or (
            "finish-current-session-missing" if not facts.get("sid") else
            "finish-registered-caller-ineligible" if facts.get("registered_caller") else registered_reason)
        return ContinuationDecision(not reason, "modern" if modern else "legacy", reason, census=facts.get("sid"))
    if action == "finish-outcome":
        closure = facts.get("closure")
        if closure is None or facts.get("prior_state"):
            return ContinuationDecision(True, "continue")
        if facts.get("legacy"):
            return ContinuationDecision(False, "closed", "finish-route-already-closed")
        if closure.get("autoclose") and closure.get("route_hash") == route["route_hash"]:
            return ContinuationDecision(False, "autoclosed")
        historical = closure.get("terminal_gate_proven") is False and closure.get("disposition") not in {
            "abandoned", "operator-decision", "cancelled"}
        return ContinuationDecision(historical, "historical" if historical else "closed",
                                    "" if historical else "finish-route-already-closed")
    if action == "finish-cycle":
        if facts.get("deleted") and not facts.get("finished"):
            return ContinuationDecision(True, "deleted")
        live = facts.get("review_live", False)
        return ContinuationDecision(not live, "wait" if live else "continue",
            "finish-active-review-lease" if live else "", "same-command" if live else "continue",
            "The exact independent review process has ended and its lease is released." if live else "")
    if action == "completion-owner":
        from dispatch_attempt_policy import verdict_pass
        applicable = not facts.get("closing") and metadata.get("workflow_completion") == "runtime-v1"
        applicable = applicable and (status in {"open", "running"} if facts.get("executing") else
            metadata.get("worker_type") == "owner" and metadata.get("dispatch_depth") == "1"
            and status == "done" and verdict_pass(metadata))
        return ContinuationDecision(bool(applicable), "applicable" if applicable else "not-applicable")
    if action == "completion-state":
        reason, state = facts.get("error", ""), "unknown"
        if not reason:
            if not facts.get("commit_exists") or facts.get("gate_only"):
                missing = {node: proof.get("reason", "unproven") for node, proof in facts.get("gates", {}).items() if not proof.get("passed")}
                reasons = set(missing.values())
                blocked = reasons & PROVEN_BLOCKED_GATE_REASONS
                state = "blocked" if blocked else "pending"
                reason = sorted(blocked or {r for r in reasons if r})[0] if blocked or any(reasons) else ""
                if facts.get("gate_only"):
                    return ContinuationDecision(not missing, "proved" if not missing else state, reason,
                        "continue" if not missing else "needs-owner", census=missing)
            elif facts.get("checkpoint") != "owner-envelope-sealed":
                state, reason = "pending", str(facts.get("checkpoint") or "")
            elif not (facts.get("settlement_verified") or facts.get("forward_verified")):
                state, reason = "pending", "forward-recovery-not-proved"
            elif facts.get("publication_pending"):
                state, reason = "pending", "shared-spec-publication-pending"
            elif facts.get("goal_pending"):
                state, reason = "pending", "campaign-goal-close-pending"
            else:
                state = "complete"
        return ContinuationDecision(completion_satisfied(state), state, reason,
            "inspect-recovery" if state == "blocked" else "advance-completed" if state == "complete" else "same-command",
            "The exact terminal evidence, sealed transaction and pending publication/goal are settled."
            if state in {"pending", "unknown"} else "")
    if action == "terminal-child":
        if facts.get("contradiction"):
            return ContinuationDecision(False, "protected", "terminal-attempt-not-pass", "inspect-recovery")
        if facts.get("never_started"):
            return ContinuationDecision(True, "proved")
        process = facts["process"]
        decision = _attempt_decision(status, metadata, process_state=process.state,
                                     process_reason=process.reason, require_quiescent=True)
        reason = {"inspect-conflict": "transaction-conflict", "reconcile": "child-not-terminal",
                  "cancel": "child-not-terminal", "complete": "child-not-terminal",
                  "wait": "child-not-quiescent", "recover": "child-not-quiescent"}.get(decision.action, "")
        return ContinuationDecision(not reason, "wait" if reason else "proved", reason, "same-command" if reason else "continue",
            "The exact related child is quiescent and its terminal result is committed." if reason else "",
            "child-terminal-conflict" if decision.action == "inspect-conflict" else process.reason if reason else "")
    if action == "stage-settlement":
        process = facts["process"]
        decision = _attempt_decision(status, metadata, process_state=process.state, process_reason=process.reason)
        eligible = metadata.get("worker_type") not in {"owner", "frame"} and decision.action == "reconcile"
        if eligible:
            return decide_continuation(action="terminal-evidence", metadata=metadata, facts=facts)
        return ContinuationDecision(False, "wait", decision.reason, decision.action,
            "The exact stage process is quiescent with its terminal result readable.")
    if action == "owner-terminal":
        from dispatch_contract import SUPERVISOR_LEASE_KIND
        identity = (metadata.get("worker_type") == "owner" and metadata.get("supervisor_lease") == SUPERVISOR_LEASE_KIND
            and metadata.get("supervisor_lease_file") and metadata.get("registered_worker") == "1"
            and metadata.get("launch_started") == "1" and metadata.get("pid_start") and metadata.get("pid")
            and metadata.get("log_file"))
        if not identity or status not in {"open", "running"}:
            return ContinuationDecision(False, "not-applicable")
        if facts.get("lease_held"):
            return ContinuationDecision(False, "wait", "supervisor-live", "same-command")
        decision = _attempt_decision(status, metadata, process_state=facts["process"].state,
                                    process_reason=facts["process"].reason)
        return ContinuationDecision(decision.action == "reconcile", "continue" if decision.action == "reconcile" else "wait",
            "" if decision.action == "reconcile" else facts["process"].reason, "same-command",
            "The exact supervisor lease is released and its owner process is quiescent.")
    if action == "delivery":
        state = facts["state"]
        if state.cancellation_requested and not state.cancelled:
            return ContinuationDecision(False, "wait", "termination-pending", "complete-open",
                "The exact cancellation and its owned processes are settled.")
        complete = state.workflow_complete and not facts.get("blocked_reason")
        success = (state.quiescent and state.owned_children == 0 and complete and
                   (state.cancelled or state.completion_proven and state.status == "done"
                    and state.verdict == "PASS" and not state.terminal_conflict))
        return ContinuationDecision(success, "success" if success else "attention",
            facts.get("blocked_reason", ""), "inspect-recovery" if facts.get("blocked_reason") else "advance-completed" if success else
            "finish-workflow" if not complete else "complete-open" if state.status in {"open", "running"}
            else "inspect-done-failure")
    if action == "join":
        observed, completion = facts["observed"], facts["completion"]
        decision = _attempt_decision(status, metadata, process_state=observed.process_state,
            process_reason=observed.process_reason, terminal_observed=observed.reason == "terminal-observed")
        workflow_pending, blocked = completion.state in {"pending", "unknown"}, completion.state == "blocked"
        reason = {"wait": "process-alive", "recover": "process-unverifiable", "complete": "completion-deferred"}.get(
            decision.action, "registry-closed" if status == "done" else "terminal-observed")
        ready = decision.action not in {"wait", "recover", "complete"}
        settle = bool(facts.get("settlement_available")) and (
            ready and status in {"open", "running"} or workflow_pending or decision.action == "complete") and decision.action != "inspect-conflict"
        recover = reason == "process-unverifiable" or (settle and status in {"open", "running"}
                    and metadata.get("pid", "").isdigit() and bool(metadata.get("pid_start")))
        return ContinuationDecision(ready and not workflow_pending and not blocked,
            "pending" if workflow_pending or blocked or not ready or settle else "ready",
            "workflow-completion-pending" if workflow_pending or blocked else "completion-deferred" if settle and decision.action == "complete"
            else "terminal-commit-pending" if settle else reason,
            "finish-workflow" if workflow_pending or blocked else _attempt_action(status, metadata),
            "The exact process, terminal writer and workflow settlement are observed complete." if not ready or workflow_pending else "",
            census={"attempt": decision, "workflow_pending": workflow_pending, "blocked": blocked,
                    "closure_eligible": blocked or ready and not workflow_pending, "settle": settle, "recover": recover})
    if action == "terminal-evidence":
        from codex_dispatch_terminal import review_blocking_handoff
        from dispatch_attempt_policy import REVIEW_BLOCKING_NOTE
        terminal = facts["terminal"]
        state, verdict = str(terminal.get("state") or "absent"), str(terminal.get("verdict") or "")
        if state != "valid":
            reason = "terminal-" + state
            if terminal.get("reason"):
                reason += ":" + str(terminal["reason"])
            return ContinuationDecision(False, state, reason, "typed-close", detail="dead-invalid-envelope")
        if verdict in {"FAIL", "BLOCKED"}:
            blocking = review_blocking_handoff(terminal, metadata.get("worker_type"))
            return ContinuationDecision(False, "typed", "typed-review-blocking" if blocking else "typed-" + verdict.lower(),
                "typed-close", detail=REVIEW_BLOCKING_NOTE if blocking else "dead-worker-" + verdict.lower(),
                census={"review_artifact_b64": str(terminal.get("artifact_path_b64") or "")} if blocking else None)
        if verdict != "PASS":
            return ContinuationDecision(False, "invalid", "terminal-verdict-invalid", "typed-close", detail="dead-invalid-envelope")
        if terminal.get("artifact_state") != "readable":
            return ContinuationDecision(False, "evidence", "evidence-" + str(terminal.get("artifact_state") or "absent"),
                                        "typed-close", detail="dead-invalid-envelope")
        if terminal.get("review_verdict_conflict"):
            return ContinuationDecision(False, "evidence", "evidence-review-verdict-conflict:" +
                                        str(terminal["review_verdict_conflict"]), "inspect-recovery", detail="dead-invalid-envelope")
        return ContinuationDecision(True, "pass")
    if action == "terminal-result":
        reason = facts.get("reason", "")
        recoverable = reason in {"transaction-conflict", "child-not-quiescent", "child-not-terminal",
                                "route-close-failed", "producer-finalize-failed", "recovery-unavailable"}
        return ContinuationDecision(False, "recoverable" if recoverable else "ineligible", reason,
            "same-command" if recoverable else "inspect-recovery",
            "The exact process and terminal transaction can be reobserved and settled." if recoverable else "",
            facts.get("detail", ""))
    if action == "attempt":
        decision = _attempt_decision(status, metadata or {}, **facts)
        return ContinuationDecision(decision.action not in {"wait", "recover", "inspect-conflict"},
                                    decision.outcome, decision.reason, decision.action, census=decision)
    if action == "attempt-action":
        return ContinuationDecision(True, committed_outcome(status, metadata or {}),
                                    next_action=_attempt_action(status, metadata or {}))
    if action == "harvest":
        candidates = [fields for fields in rows if fields[1] in {"open", "running"}]
        return ContinuationDecision(len(candidates) <= 1, "open" if candidates else "settled",
            "ambiguous-selector" if len(candidates) > 1 else "", census=candidates)
    if owner_attempt_id is not None:
        from dispatch_replacement import effective_attempts, _rows
        try:
            attempts, _ = effective_attempts(jobs, {owner_attempt_id})
            found = _rows(Path(jobs).read_text().splitlines())
        except OSError as error:
            return ContinuationDecision(False, "wait", "replacement-source-missing", "same-command",
                "The exact owner row is readable in the canonical registry.", str(error))
        except Exception as error:
            from dispatch_contract import DispatchContractError
            if not isinstance(error, DispatchContractError):
                raise
            return ContinuationDecision(False, "protected", error.reason, "settle", detail=error.detail)
        if len(attempts) != 1 or next(iter(attempts)) not in found:
            return ContinuationDecision(False, "wait", "replacement-source-missing", "same-command",
                                        "The exact owner row is readable in the canonical registry.")
        fields, metadata = found[next(iter(attempts))]
        status = fields[1]
    metadata = metadata or {}
    if action == "handover" and not facts.get("eligible", False):
        return ContinuationDecision(False, "settled", next_action="settle")
    if (action == "handover" or facts.get("actor")) and not owns(
            metadata, facts.get("actor"), jobs, read_context=facts.get("read_context")):
        return ContinuationDecision(False, "protected", "work-parent-recovery-required", "report")
    if facts.get("history_conflict"):
        return ContinuationDecision(False, "protected", facts["history_conflict"], "report")
    if action == "identity-refusal-reentry":
        eligible = (status == "done" and metadata.get("worker_type") == "owner"
                    and facts.get("exact_claim") is True
                    and facts.get("identity_refusal") is True
                    and facts.get("source_replacement_eligible") is True
                    and facts.get("successor_unstarted_or_absent") is True)
        return ContinuationDecision(eligible, "resume" if eligible else "settled",
            "" if eligible else "replacement-duty-settled", "start" if eligible else "settle")
    if action == "replacement-progress":
        from dispatch_contract import verdict_pass
        from dispatch_replacement import _plain_retry_successor, _replacement_in_flight
        if facts.get("record") is not None:
            kind = _replacement_kind(status, metadata, jobs=jobs)
            if status not in {"open", "running"}:
                if verdict_pass(metadata):
                    return ContinuationDecision(True, "reused", next_action="observe")
                continuing = bool(kind)
                return ContinuationDecision(continuing, "advance" if continuing else "not-applicable",
                                            next_action="start" if continuing else "settle")
            if metadata.get("launch_claimed") == "1":
                pin = facts.get("pin")
                if pin and metadata.get("harness") != pin:
                    return ContinuationDecision(False, "protected", "pin-ignored-for-replacement", "report",
                                                detail="replacement already launched; its execution identity is retained")
                return ContinuationDecision(True, "running", next_action="observe")
            return ContinuationDecision(True, "launch", next_action="start")
        if metadata.get("replacement_original_attempt_id") and status in {"open", "running"} and metadata.get("launch_claimed") != "1":
            return ContinuationDecision(True, "advance", next_action="start")
        kind = facts.get("kind")
        if kind is None:
            return ContinuationDecision(False, "not-applicable", next_action="observe")
        if _plain_retry_successor(facts.get("registry_rows") or {}, metadata.get("attempt_id")):
            # An ordinary retry already continues this attempt; a claim would only compete with it.
            return ContinuationDecision(False, "not-applicable", next_action="observe")
        if not facts.get("resume_capacity") and not _replacement_in_flight(jobs, facts["registry_rows"], metadata):
            if kind == "capacity":
                return ContinuationDecision(False, "capacity", "replacement-capacity-wait", "same-command")
            if kind in {"runtime", "unlaunched", "corrected"}:
                return ContinuationDecision(False, "not-applicable", next_action="observe")
        return ContinuationDecision(True, "launch", next_action="start")
    if action == "replacement-result":
        terminal, kind = facts["terminal"], facts["kind"]
        absent = terminal.get("state") == "absent" or (terminal.get("state") == "invalid" and
            terminal.get("failure_class") == ("capacity" if kind == "frame-capacity" else kind)
            and kind in {"capacity", "frame-capacity", "runtime"})
        return ContinuationDecision(absent, "continue" if absent else "protected",
            "" if absent else "replacement-result-settlement-required", "start" if absent else "settle")
    if action == "owner-start":
        # The existing atomic registry claim still protects the launch boundary.
        registered = facts.get("registered", False)
        return ContinuationDecision(True, "existing" if registered else "start",
                                    next_action="observe" if registered else "start")
    stopped = answerable_owner_end(status, metadata) if action in {"owner", "owner-input"} else ""
    if action == "owner-input":
        # Keeping an answer cannot spawn an owner or disturb uncertain transport.
        if stopped:
            return ContinuationDecision(True, "retained", next_action="correct")
        if status in {"open", "running"} and facts.get("accepting", True) and facts.get("reachable"):
            return ContinuationDecision(True, "queued", next_action="handback")
    parked, cancellation = facts.get("parked"), None
    if action == "owner":
        from route_parent_close import row_requested
        closing = (metadata.get("parent_close_requested") == "1"
                                         or jobs is not None and row_requested(metadata, jobs))
        if closing or facts.get("cancelled") or status in {"cancelled", "killed"}:
            cancellation = ContinuationDecision(False, "protected", "cancelled-by-parent", "settle")
    if action == "owner":
        if cancellation is not None:
            return cancellation
        if parked is None and jobs is not None and metadata.get("attempt_id"):
            from dispatch_replacement import owner_parked_gate
            parked = owner_parked_gate(jobs, metadata["attempt_id"])
        if parked and parked["status"] != "proceed":
            gate_state = parked["status"]
            return ContinuationDecision(False, gate_state,
                {"blocked": "owner-parked-at-human-gate", "stop": "human-gate-stop",
                 "revise": "human-gate-revise-owner-parked"}[gate_state],
                {"blocked": "answer-human-gate", "stop": "report", "revise": "report-gate-revision"}[gate_state])
        if stopped and not parked and not facts.get("answered"):
            return ContinuationDecision(False, "answer", "owner-" +
                ("blocked" if stopped == "BLOCKED" else "failed"), "correct")
    process = facts.get("process")
    if action in {"replacement", "owner-input", "owner"}:
        if process is None and jobs is not None and metadata.get("attempt_id"):
            from route_parent_close import owner_continuation_processes
            process, _ = owner_continuation_processes(metadata, jobs)
        if process is not None and process.state != "quiescent":
            if action == "owner" and process.state == "live" and status in {"open", "running"}:
                return ContinuationDecision(True, "running", next_action="handback")
            return ContinuationDecision(False, "wait", ("owner-process-" if action == "owner" else
                "replacement-process-") + process.state,
                "same-command", "The exact owner and unregistered children are observed quiescent; "
                "registered payloads remain bound and running.", process.reason)
    if action in {"replacement", "replacement-budget"}:
        if facts.get("budget_exhausted") or facts.get("competing_successor"):
            return ContinuationDecision(False, "protected", "automatic-replacement-exhausted", "report")
        if facts.get("child_unsettled"):
            return ContinuationDecision(False, "wait", "replacement-owner-child-unsettled", "same-command",
                                        "The exact child is settled and quiescent.", facts["child_unsettled"])
        if action == "replacement-budget":
            return ContinuationDecision(True, "continue", next_action="start")
        if facts.get("terminal_conflict"):
            return ContinuationDecision(False, "protected", "replacement-terminal-conflict", "settle")
        if status not in {"done", "cancelled", "killed"}:
            return ContinuationDecision(False, "wait", "replacement-terminal-unsettled", "same-command",
                                        "The exact attempt has settled its terminal result.")
        if not facts.get("death_kind"):
            return ContinuationDecision(False, "protected", "replacement-not-silent-death", "settle")
        kind = facts["death_kind"]
        if facts.get("fields") and kind not in {"parked", "unlaunched", "corrected"}:
            from dispatch_replacement import terminal_observation
            choice = decide_continuation(action="replacement-result", facts={"kind": kind,
                "terminal": terminal_observation(facts["fields"], metadata)})
            if not choice.allowed:
                return choice
        return ContinuationDecision(True, "continue", next_action="start")
    if action in {"owner", "owner-input"}:
        if cancellation is not None:
            return cancellation
        if parked and parked["status"] == "proceed":
            return ContinuationDecision(True, "continue", next_action="start")
        if stopped and action == "owner" or runtime_owner_can_resume(status, metadata):
            if action == "owner-input":
                return ContinuationDecision(True, "retained", next_action="correct")
            if facts.get("answered"):
                return ContinuationDecision(True, "continue", next_action="start")
            return ContinuationDecision(False, "answer", "owner-" +
                ("blocked" if stopped == "BLOCKED" else "failed"), "correct")
        if action == "owner-input":
            return ContinuationDecision(False, "wait" if status in {"open", "running"} else "settle",
                "owner-input-unsupported", "same-command" if status in {"open", "running"} else "settle",
                "The same owner's input consumer accepts input or its terminal result settles."
                if status in {"open", "running"} else "")
        if status == "done":
            return ContinuationDecision(False, "settle", "owner-settlement-pending", "settle")
        if action == "owner" and process is not None and process.state == "quiescent":
            return ContinuationDecision(False, "wait", "owner-settlement-pending", "same-command",
                "The exact owner's terminal observation is committed to the canonical registry.", process.reason)
        if action == "owner" and facts.get("reachable"):
            return ContinuationDecision(True, "running", next_action="handback")
        return ContinuationDecision(False, "wait", "owner-input-unsupported", "same-command",
                                    "The same owner's input consumer is reachable or its terminal result settles.")
    if action == "handover":
        allowed = status in {"open", "running"}
        if status in {"done", "cancelled", "killed"}:
            import dispatch_seat_handover as handover
            allowed = (facts.get("delivery_open") if "delivery_open" in facts else
                       handover._delivery_open(jobs, metadata)) or (
                       facts.get("route_open") if "route_open" in facts else handover._route_open(jobs, metadata))
        return ContinuationDecision(bool(allowed), "handover" if allowed else "settled",
                                    next_action="handover" if allowed else "settle")
    if action == "resource-failure":
        allowed = facts.get("not_started", False) and facts.get("bound", True) and not facts.get("deferred", False)
        return ContinuationDecision(bool(allowed), "not-started" if allowed else "unconfirmed",
                                    next_action="return-to-owner" if allowed else "observe")
    if action == "fix":
        from dispatch_contract import parse_registry_metadata, owner_closure_shape
        answers, fallback = [], []
        for candidate in route.get("nodes") or []:
            if not _ROUND.is_round_capped_node(candidate):
                continue
            census = []
            for line in facts["lines"]:
                fields = line.split("\t")
                if len(fields) != 6:
                    continue
                meta = parse_registry_metadata(fields[5])
                if ((meta.get("route_id") or meta.get("route")) == route.get("route_id")
                        and meta.get("route_node") == candidate["id"] and not no_stage_authority(meta)):
                    census.append((fields, meta))
            history = [(f[1], m) for f, m in _ROUND.logical_round_records(census, jobs=jobs)]
            worker = candidate.get("worker_type") or ("review" if candidate.get("kind") == "review-worker" else "test")
            source_digest = None
            current_history = history
            from review_source_subject import has_review_subject
            if candidate.get("kind") == "review-worker" or has_review_subject(route, candidate, jobs):
                from review_source_subject import SourceSubjectError, annotate_rows, has_review_subject, observe
                if has_review_subject(route, candidate, jobs):
                    try:
                        source_digest = observe(route, candidate, worktree=route.get("cwd"), jobs=jobs)["digest"]
                        current_history = [(status, metadata) for status, metadata in
                            annotate_rows(jobs, history)
                            if metadata.get("_review_subject_digest") == source_digest
                            or metadata.get("review_subject_digest") == source_digest]
                    except SourceSubjectError as exc:
                        # This is an owner fix handoff, not a current-PASS or
                        # fresh-review admission. Preserve legacy failure
                        # answers when an old fixture/worktree has no Git tree.
                        if exc.reason not in {"review-source-git-unavailable",
                                              "review-source-worktree-unavailable"}:
                            raise
                        source_digest = None
                        current_history = history
            if not current_history or not _ROUND.last_verdict_blocking(current_history, worker):
                continue
            last = next(m for st, m in reversed(current_history) if _ROUND.classify_round_row(st, m,
                        worker_type=m.get("worker_type") or worker) == "verdict")
            failed = last.get("attempt_id", "")
            choice = decide_continuation(route, action="round", node=candidate, rows=history,
                jobs=jobs if source_digest is not None else None,
                revisions=[{"answers": [failed]}],
                facts=({"source_subject_digest": source_digest}
                       if source_digest is not None else None))
            if failed and choice.budget.round_kind == "closure-check":
                answers.append(failed)
            elif owner_closure_shape(last) != "registered-review":
                fallback.append(candidate["id"] + ":" + choice.state)
        return ContinuationDecision(True, "continue", next_action="start",
                                    answers=tuple(answers), fallback_nodes=tuple(fallback))
    if action == "round-recovery":
        state = facts["state"]
        return ContinuationDecision(True, state, next_action=_round_action(state, facts["node_kind"]))
    if action not in {"round", "owner-closure", "complete-inline", "review-revision", "marker"}:
        raise ValueError("unknown continuation action: " + action)
    budget = facts.get("budget")
    if budget is None:
        cap = (route.get("continuation_budget") or {}).get("review_round_cap")
        if cap is None:
            try:
                cap = _ROUND.max_review_rounds(route["effective_intensity"])
            except ValueError:
                if action == "owner-closure":
                    raise _contract_error("owner-closure-intensity-unknown", "owner-closure-intensity-unknown:" + route["effective_intensity"])
                raise
        worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
        # Strict terminal no-start rows remain in the physical registry and in
        # dispatch-node's lineage view, but are not launched rounds. Derive the
        # exclusion from the same launch-fence proof used by retry admission.
        from dispatch_contract import terminal_attempt_never_started
        rows = [(status, meta) for status, meta in rows
                if not terminal_attempt_never_started(status, meta)]
        kinds = [_ROUND.classify_round_row(s, m, worker_type=m.get("worker_type") or worker_type) for s, m in rows]
        source_digest = facts.get("source_subject_digest")
        source_aware = False
        budget_rows = rows
        budget_kinds = kinds
        from review_source_subject import has_review_subject
        if node.get("kind") == "review-worker" or has_review_subject(route, node, jobs):
            from review_source_subject import annotate_rows, observe
            if source_digest is not None or has_review_subject(route, node, jobs):
                if source_digest is None and jobs is not None:
                    source_digest = observe(route, node, worktree=route.get("cwd"), jobs=jobs)["digest"]
                    rows = annotate_rows(jobs, rows)
                    kinds = [_ROUND.classify_round_row(s, m,
                        worker_type=m.get("worker_type") or worker_type) for s, m in rows]
                if source_digest is not None:
                    source_aware = True
                    budget_rows = [(status, meta) for status, meta in rows
                                   if meta.get("_review_subject_digest") == source_digest
                                   or meta.get("review_subject_digest") == source_digest]
                    budget_kinds = [_ROUND.classify_round_row(s, m,
                        worker_type=m.get("worker_type") or worker_type) for s, m in budget_rows]
        verdicts = [i for i, kind in enumerate(budget_kinds) if kind == "verdict"]
        subject_attempts = {m.get("attempt_id") for _status, m in budget_rows if m.get("attempt_id")}
        budget_revisions = [revision for revision in revisions if any(
            attempt in subject_attempts
            for attempt in (*tuple(revision.get("answers") or ()), *tuple(revision.get("supersedes") or ()))
        )] if source_aware else revisions
        budget_kinds_for_streak = budget_kinds if source_aware else kinds
        budget_rows_for_progress = budget_rows if source_aware else rows
        streak = 0
        for kind in reversed(budget_kinds_for_streak):
            if kind != "verdict-less":
                break
            streak += 1
        counted = blocked_progress(route, budget_rows_for_progress, worker_type=worker_type) if (
            streak >= _ROUND.VERDICTLESS_BOUND and route.get("route_plan") is not None) else streak
        state = ("blocked-live" if "live" in kinds else "blocked-unsettled" if "unsettled" in kinds else
                 "verdictless-bound" if counted >= _ROUND.VERDICTLESS_BOUND else
                 "exhausted" if len(verdicts) >= cap else "admit")
        # A source-bound implementation review keeps its correction/closure
        # check inside that subject's verdict cap. Legacy review and test
        # nodes retain their existing cap + 1 closure allowance.
        last_row = budget_rows[verdicts[-1]] if verdicts else None
        last_id = last_row[1].get("attempt_id") if last_row else None
        closure_limit = cap if source_aware else cap + 1
        closure = bool(state in {"admit", "exhausted"} and len(verdicts) < closure_limit and last_id and (
            (_ROUND._last_round_blocking_verdict([last_row], worker_type) and
             any(last_id in (r.get("answers") or ()) for r in budget_revisions)) or
            any(last_id in (r.get("supersedes") or ()) for r in budget_revisions)))
        if closure:
            state = "admit"
        # Attempt history does not establish a prior judgment. A crash,
        # capacity death, or invalid envelope may advance the lineage's round
        # number, but until a substantive verdict exists for this subject the
        # next admitted pass is still a full first review.
        round_kind = "closure-check" if closure else "correction" if verdicts else "first"
        budget = _ROUND.RoundBudget(len(verdicts), counted, budget_kinds_for_streak.count("verdict-less"), cap, state,
            len(rows) + 1, len(rows) + 1 if rows else 0,
            round_kind,
            _round_action(state, node.get("kind")), closure, counted < streak)
    state = budget.state
    if action == "marker":
        site = facts["site"]
        if site == "registered":
            closure = "closure-check" if budget.round_kind == "closure-check" else "registered-verdict"
        elif site in {"owner-closure", "revision"}:
            closure = site
        elif site == "inline":
            worker = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
            closure = ("owner-override-unlinked" if not facts.get("independently_reviewed") and
                       _ROUND._last_round_blocking_verdict(rows, worker) else
                       ("review-verdictless-bound" if node.get("kind") == "review-worker" else "owner-run-verdictless")
                       if state == "verdictless-bound" else "inline-no-unresolved-fail")
        else:
            raise ValueError("unknown round-census site: " + site)
        return ContinuationDecision(True, state, budget=budget, census={
            "verdict_rounds": budget.verdict_rounds + (site == "registered"),
            "verdictless_rounds": budget.verdictless_rounds, "cap": budget.cap, "closure_class": closure})
    if action == "owner-closure" and node.get("completion_gate") == "code-test":
        return ContinuationDecision(False, "not-review", "owner-closure-node-not-review",
                                    "settle", detail="code-test remains a test-owned gate")
    if action == "owner-closure":
        allowed = state in {"exhausted", "verdictless-bound"}
        reason = {"blocked-live": "owner-closure-round-still-open", "blocked-unsettled": "owner-closure-round-unsettled",
                  "admit": "owner-closure-round-budget-not-exhausted"}.get(state, "")
    elif action in {"review-revision", "complete-inline"}:
        allowed = state not in {"blocked-live", "blocked-unsettled"}
        reason = ("review-input-revision-" + state if action == "review-revision" else "round-unsettled") if not allowed else ""
    else:
        allowed = state == "admit"
        reason = {"blocked-live": "prior-attempt-still-live", "blocked-unsettled": "round-unsettled",
                  "exhausted": "review-round-budget-exhausted", "verdictless-bound": "review-verdictless-bound"}.get(state, "")
    detail = ""
    if action == "owner-closure" and not allowed:
        if state == "admit":
            detail = f"rounds={budget.verdict_rounds};max_round={budget.cap};verdictless_streak={budget.verdictless_streak};bound={_ROUND.VERDICTLESS_BOUND}"
        elif state == "blocked-live":
            worker = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
            detail = "attempt=" + "|".join(m.get("attempt_id") or "-" for s, m in rows
                if _ROUND.classify_round_row(s, m, worker_type=worker) == "live")
    return ContinuationDecision(allowed, state, reason, "same-command" if state.startswith("blocked-") else budget.next_action,
        "The exact active round settles without overwriting its result." if state.startswith("blocked-") else "",
        detail, budget)


# ---------------------------------------------------------------------------
# 1. Who continues the route
# ---------------------------------------------------------------------------

def caller_identity(environ=None) -> tuple[str, str]:
    """Resolve the caller's native identity, independently of the child adapter.

    `session_identity.identity()` reads it; a launch refuses a caller it cannot name.
    """
    found = session_identity(environ)
    if found.confidence == "invalid":
        raise _contract_error("caller-harness-invalid")
    if found.confidence == "ambiguous":
        raise _contract_error("caller-harness-ambiguous")
    return found.harness, found.session_id


def default_parent_session_id(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    # A directly running interactive host is the authority for its own TUI
    # thread. Managed entry used to export a second parent id after observing
    # an App Server sibling, which could override the real Codex thread and
    # strand every completion. Preserve the explicit binding only for nested
    # workers, whose environment marks that dispatch boundary.
    if env.get("AGENT_DISPATCH_CHILD") != "1":
        native_session = caller_identity(env)[1]
        if native_session:
            return native_session
    return env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or caller_identity(env)[1] or None


def default_parent_harness(fallback: str, environ=None) -> str:
    """A selected child's runtime never replaces its caller's identity."""
    env = os.environ if environ is None else environ
    return caller_identity(env)[0] or env.get("AGENT_DISPATCH_OWNER_HARNESS") or fallback


def bind_runtime_parent(args, *, honor_force: bool = False, environ=None) -> None:
    """Bind a dispatch-depth-1 job to the actual calling session, on any harness.

    Callers historically supplied a synthetic ``--parent-session-id``; the
    running session overrides it. The caller is read by
    `session_identity.identity()`; an ambiguous or unnamed caller binds
    nothing. Dispatch-depth-2 workers keep their explicit conductor/owner
    envelope. Only the Codex wrapper honors the legacy
    ``CODEX_DISPATCH_PARENT_CURRENT_FORCE`` switch (``honor_force``).
    """
    env = os.environ if environ is None else environ
    force_current = honor_force and env.get("CODEX_DISPATCH_PARENT_CURRENT_FORCE") == "1"
    caller = session_identity(env)
    if args.dispatch_depth == 1:
        if caller.known and caller.session_id:
            args.parent_session_id = caller.session_id
            args.parent_harness = caller.harness
            args.parent_slug = None
        elif force_current:
            args.parent_slug = None
    elif force_current and caller.harness == "codex" and caller.session_id:
        args.parent_session_id = caller.session_id


def correction_source_session(environ=None) -> str:
    """The session recorded as the sender of an owner correction (a label, not a check)."""
    return session_label(environ)


def owns(meta, session, jobs, *, read_context=None) -> bool:
    """Only the session that answers for the attempt now (``effective_parent``).

    That is the registered parent before a confirmed handover and the end of its
    lineage after one. Unreadable handover state retains the existing
    registered-parent fallback.
    """
    if not session:
        return False
    from dispatch_seat_handover import owns as seat_owns
    if read_context is None:
        return seat_owns(meta, session, jobs)
    return seat_owns(meta, session, jobs, read_context=read_context)


def require_replacement_parent(jobs, rows, meta, *, current_session) -> None:
    """Only the attempt's parent may start its replacement.

    Dispatch depth 2: the caller's own attempt is the registered, live parent
    attempt. Otherwise: the registered parent session or its same-seat
    successor. ``current_session`` is called only on that second path.
    """
    if meta.get('dispatch_depth') == '2':
        import dispatch_contract as DC
        parent = meta.get('parent_attempt_id')
        if not parent or os.environ.get('AGENT_DISPATCH_ATTEMPT_ID') != parent or parent not in rows:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        fields, parent_meta = rows[parent]
        if fields[1] not in {'open', 'running'} or not DC._parent_liveness_evidence(Path(jobs), parent_meta)[0]:
            raise DC.DispatchContractError('replacement-parent-not-live')
    elif not meta.get('parent_sid') or not owns(meta, current_session(), jobs):
        raise _contract_error('replacement-parent-identity-unproven')


def replacement_parent_matches(source, candidate, jobs, *, lineage=False) -> bool:
    """Keep the parent attempt exact; a depth-1 session may be its recorded successor."""
    if candidate.get('parent_attempt_id') != source.get('parent_attempt_id'):
        return False
    if candidate.get('parent_sid') == source.get('parent_sid'):
        return True
    if source.get('dispatch_depth') != '1':
        return False
    if owns(source, candidate.get('parent_sid'), jobs):
        return True
    if lineage and candidate.get('dispatch_depth') == '1':
        # A registered A -> B edge survives a later B -> C handover. Admission
        # still accepts only the source's current effective parent.
        from dispatch_seat_handover import effective_parent
        current = effective_parent(source, jobs)
        return bool(current) and effective_parent(candidate, jobs) == current
    return False


def lineage_parent_matches(metadata, *, thread_id, parent_attempt_id) -> bool:
    """A replacement lineage row belongs to this receipt's parent: the exact
    registered session at depth 1, the exact parent attempt below it."""
    if metadata.get("dispatch_depth") == "1":
        return metadata.get("parent_sid") == thread_id
    return metadata.get("parent_attempt_id") == parent_attempt_id


LIVE_ROW_STATUSES = frozenset({"open", "running"})


def review_owner_authority(route, jobs, author_attempt_id) -> None:
    """Prove the current registered owner without taking or creating locks."""
    from dispatch_contract import parse_registry_metadata
    from owner_route_binding import resolve_owner_route_lifecycle
    if not author_attempt_id:
        raise ValueError("review-input-revision-owner-required")
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller and caller != author_attempt_id:
        raise ValueError("review-input-revision-owner-caller-mismatch")
    matches = []
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == author_attempt_id:
                matches.append((fields, meta))
    if len(matches) != 1:
        raise ValueError("review-input-revision-owner-not-exact")
    fields, meta = matches[0]
    if (fields[1] not in LIVE_ROW_STATUSES or meta.get("worker_type") != "owner"
            or meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"):
        raise ValueError("review-input-revision-owner-invalid")
    binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=author_attempt_id)
    if binding is None or (binding.route_id, binding.route_hash) != (route["route_id"], route["route_hash"]):
        raise ValueError("review-input-revision-owner-route-mismatch")


# ---------------------------------------------------------------------------
# 2. On which harness
# ---------------------------------------------------------------------------

# `compose --pin owner|frame|worker=<harness>[:<model>[:<effort>]]` seals one
# pin per target in the route; every launch, resume, replacement and stage
# fallback reads it from the route file the launch is bound to.
PIN_TARGETS = ("owner", "frame", "worker")


def selection_pin_rows(route) -> dict:
    """Copied pins in force for a descendant route, without the schema envelope."""
    pins = route_in_force(route).get("selection_pins") if isinstance(route, dict) else None
    return {target: dict(pins[target]) for target in PIN_TARGETS
            if isinstance(pins, dict) and isinstance(pins.get(target), dict)}


def pin_target(worker_type: str | None) -> str:
    return "frame" if worker_type == "frame" else "owner" if worker_type == "owner" else "worker"


def sealed_pin_harness(route, *, worker_type: str | None) -> str | None:
    """The harness pinned for the launch's pin target, or None: the sealed pin,
    or the route parent's later recorded change (`route_in_force`)."""

    pins = route_in_force(route).get("selection_pins") if isinstance(route, dict) else None
    pin = pins.get(pin_target(worker_type)) if isinstance(pins, dict) else None
    return (pin.get("harness") or None) if isinstance(pin, dict) else None


# A sealed route never changes; its parent may later move one of its pins
# (`capability-route.py start --route <file> --pin owner|frame|worker=<harness>[:<model>[@<effort>]]`).
# Each change is one append-only row beside the route: the new pin, the pin it replaced, who
# changed it and when, what the runtime knew about where the instruction came from, and the
# checked launch tuples the runtime probed for the new pin at that moment. Launch decisions
# read the sealed route through `route_in_force`, after its hash is verified; an attempt that
# already launched keeps what it launched with.
PIN_CHANGE_TARGETS = ("owner", "frame", "worker")
PIN_CHANGE_SCHEMA = 1


def pin_changes_path(route) -> Path | None:
    """`<artifact_root>/.runtime/routes/<route_id>.pin-changes.jsonl`, beside the route file."""
    root = route.get("artifact_root") if isinstance(route, dict) else None
    route_id = route.get("route_id") if isinstance(route, dict) else None
    if not isinstance(root, str) or not root or not isinstance(route_id, str) \
            or not re.fullmatch(r"rt-[A-Za-z0-9-]{1,64}", route_id):
        return None
    return Path(root).resolve() / ".runtime" / "routes" / f"{route_id}.pin-changes.jsonl"


def pin_changes(route) -> list[dict]:
    """This route's recorded pin changes, oldest first; rows for another route or hash are ignored."""
    path = pin_changes_path(route)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        pin = row.get("pin") if isinstance(row, dict) else None
        if (isinstance(row, dict) and row.get("schema") == PIN_CHANGE_SCHEMA
                and row.get("route_id") == route.get("route_id")
                and row.get("route_hash") == route.get("route_hash")
                and row.get("target") in PIN_CHANGE_TARGETS
                and isinstance(pin, dict) and pin.get("harness") in PIN_HARNESSES):
            rows.append(row)
    return rows


PIN_HARNESSES = ("claude", "codex", "opencode")


def _tuple_key(row: dict) -> tuple:
    return tuple(row.get(key) for key in (
        "parent_harness", "parent_transport", "parent_sandbox", "child_harness", "launch_authority"))


def _merge_checked_tuple(rows, row):
    for index, old in enumerate(rows):
        if _tuple_key(row) == _tuple_key(old):
            if row.get("status") == "supported" and (
                    old.get("status") != "supported"
                    or row.get("checked_worktree") != old.get("checked_worktree")):
                rows[index] = dict(row)
            return
    rows.append(dict(row))


def route_in_force(route):
    """The sealed route with its recorded pin changes applied: the pin in force, and the
    checked tuples probed for each new pin added to the route's dispatch evidence,
    each depth-2 node's same/cross-harness hops and the registered-headless candidates.

    The sealed route is returned unchanged when nothing was recorded; otherwise a copy is
    returned, so the caller's sealed dict (and its hash) is never touched."""

    changes = pin_changes(route) if isinstance(route, dict) else []
    if not changes:
        return route
    view = json.loads(json.dumps(route))
    pins = dict(view.get("selection_pins") or {"contract_version": 1})
    evidence = view.get("dispatch_evidence") if isinstance(view.get("dispatch_evidence"), dict) else None
    tuples = evidence.setdefault("tuples", []) if evidence is not None else None
    candidates = view.get("registered_headless_candidates")
    for change in changes:
        pins[change["target"]] = dict(change["pin"])
        for row in change.get("tuples") or []:
            if not isinstance(row, dict) or tuples is None:
                continue
            _merge_checked_tuple(tuples, row)
            if row.get("launch_authority") != "conductor":
                continue
            ordinal = 1 if row.get("child_harness") == row.get("parent_harness") else 2
            for node in view.get("nodes") or []:
                for hop in node.get("fallback_hops") or []:
                    if hop.get("ordinal") == ordinal and isinstance(hop.get("candidates"), list):
                        _merge_checked_tuple(hop["candidates"], row)
        for row in change.get("candidates") or []:
            if isinstance(candidates, list) and isinstance(row, dict) and row not in candidates:
                candidates.append(row)
    view["selection_pins"] = pins
    return view


def changed_pin_harness(route, target: str) -> str | None:
    """The harness the route's parent last moved `target` to, or None when it never moved it."""
    changes = [row for row in pin_changes(route) if row.get("target") == target
               and (row.get("previous") or {}).get("harness") != row["pin"]["harness"]]
    return changes[-1]["pin"]["harness"] if changes else None


def moved_owner_harness(route, launched_harness: str | None) -> str | None:
    """The owner harness the route's parent moved to after an owner launched on
    `launched_harness`, or None. This is a historical observation; replacement launch
    selection reads all current pins through `route_in_force`."""
    harness = changed_pin_harness(route, "owner")
    return harness if harness and harness != launched_harness else None


def record_pin_change(route, *, target: str, pin: dict, by: dict, source: str,
                      tuples: list, candidates: list, now: float | None = None) -> dict | None:
    """Append a changed pin or its missing checked evidence; exact replay is None."""
    import fcntl
    import time
    if target not in PIN_CHANGE_TARGETS or pin.get("harness") not in PIN_HARNESSES:
        raise ValueError(f"pin-change-target-unsupported:{target}")
    path = pin_changes_path(route)
    if path is None:
        raise ValueError("pin-change-route-unlocated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        view = route_in_force(route)
        previous = (view.get("selection_pins") or {}).get(target)
        if isinstance(previous, dict) and {k: previous.get(k) for k in ("harness", "model", "effort")} \
                == {k: pin.get(k) for k in ("harness", "model", "effort")}:
            existing = (view.get("dispatch_evidence") or {}).get("tuples", [])
            current_candidates = view.get("registered_headless_candidates") or []
            missing_tuple = any(row.get("status") == "supported" and not any(
                _tuple_key(row) == _tuple_key(old) and old.get("status") == "supported"
                and row.get("checked_worktree") == old.get("checked_worktree") for old in existing)
                for row in tuples)
            missing_candidate = any(row.get("status") == "supported" and not any(
                row.get("harness") == old.get("harness") and old.get("status") == "supported"
                for old in current_candidates) for row in candidates)
            if not missing_tuple and not missing_candidate:
                return None
        row = {"schema": PIN_CHANGE_SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
               "target": target, "pin": {k: pin.get(k) for k in ("harness", "model", "effort")},
               "previous": previous, "by": dict(by), "source": source,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "tuples": list(tuples), "candidates": list(candidates)}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row


# The route's parent may also hand its next owner another execution access request: the
# existing `AGENT_DISPATCH_EXECUTION_ACCESS_FILE` when it runs `start` or `correct`. Each one is
# an `access` row in the same append-only record: the validated request, its digest, the digest
# it replaced, who gave it, and what the runtime knew about where it came from. A node's first
# preparation that derived roots from the approved task adds one row with source `derived`.
# The latest row is the request in force (`access_in_force`); the pin readers above skip these.
ACCESS_CHANGE_TARGET = "access"


def access_changes(route) -> list[dict]:
    """This route's recorded access requests, oldest first; rows for another route or hash are ignored."""
    path = pin_changes_path(route)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("schema") == PIN_CHANGE_SCHEMA
                and row.get("target") == ACCESS_CHANGE_TARGET
                and row.get("route_id") == route.get("route_id")
                and row.get("route_hash") == route.get("route_hash")
                and isinstance(row.get("request"), dict) and isinstance(row.get("source"), str)
                and row.get("request_sha256") == request_digest(row["request"])):
            rows.append(row)
    return rows


def request_digest(request: dict) -> str:
    """The digest of a normalized execution access request (`execution_access.normalized_request`)."""
    import hashlib
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def access_in_force(route) -> dict | None:
    """The latest access row of this route, or None while its first prepared request stands."""
    rows = access_changes(route) if isinstance(route, dict) else []
    return rows[-1] if rows else None


def record_access_change(route, *, request: dict, request_sha256: str, by: dict, source: str,
                         now: float | None = None) -> dict | None:
    """Append one access request for this route; None when the request in force is already it."""
    import fcntl
    import time
    path = pin_changes_path(route)
    if path is None:
        raise ValueError("access-change-route-unlocated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = access_in_force(route)
        if previous is not None and previous["request_sha256"] == request_sha256:
            return None
        row = {"schema": PIN_CHANGE_SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
               "target": ACCESS_CHANGE_TARGET, "request": request, "request_sha256": request_sha256,
               "previous_sha256": (previous or {}).get("request_sha256"), "by": dict(by), "source": source,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row


def pinned_launch_harness(route, *, worker_type: str | None, requested: str | None, available) -> tuple[str | None, str | None]:
    """A sealed pin beats the requested harness while the pinned one is available.

    Returns `(harness, overridden_request)`; the second item is the request the pin replaced
    (None when nothing was replaced), so the caller can record it.  An unavailable pin, or no
    pin, leaves the request alone.  `available` is the caller's own hard-eligibility test.
    """

    pinned = sealed_pin_harness(route, worker_type=worker_type)
    if not pinned or (pinned != requested and not available(pinned)):
        return requested, None
    return pinned, (requested if requested not in (None, pinned) else None)


REPLACEMENT_FIXED_KEYS = ("harness", "jobs", "worktree")

# The task and scope survive recovery. Parent/owner/pin/model are current
# execution choices; the ordinary launcher validates their realization.
RECOVERY_SCOPE_KEYS = (
    "route_node", "worker_type", "dispatch_depth", "session_chain_id",
    "subsession_id", "subsession_index", "subsession_count", "subsession_mode",
    "stage_authority", "fixed_inputs_sha256", "narrow_verify_sha256", "phase_brief_sha256",
)
RECOVERY_SELECTION_OPTIONS = frozenset({
    "--parent-session-id", "--parent-harness", "--parent-completion-delivery",
    "--route-file", "--route-id", "--route-hash", "--owner-route-file",
    "--owner-route-id", "--owner-route-hash", "--model", "--reasoning",
    "--model-profile", "--execution-access-file", "--reviewed-evidence",
})
RECOVERY_SELECTION_VALUES = frozenset({
    "parent_completion_delivery",
    "resolved_completion_delivery", "model", "reasoning", "resolved_model_settings",
    "execution_surface", "fallback_hop", "model_role",
})


def recovery_task(record, source, history):
    """Read the recorded work; a moved owner receives its bound continuation request."""
    if (source.get("worker_type") == "owner"
            and (source.get("owner_route_id") or source.get("route_id")) != record["route_id"]):
        route = json.loads(Path(record["route_file"]).read_text(encoding="utf-8"))
        if route.get("route_hash") != record["route_hash"]:
            raise _contract_error("replacement-route-drift")
        text = (route.get("work_request") or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise _contract_error("replacement-current-route-input-unproven")
        return text
    return history["task"]


def permission_mismatch_detail(previous, current):
    """Describe a historical permission difference with its exact evidence source."""
    old, new = _granted_permission_pair(previous, current)
    if old == new:
        return None

    def first_difference(left, right, prefix=()):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left or key not in right:
                    return (*prefix, key), left.get(key, "<missing>"), right.get(key, "<missing>")
                found = first_difference(left[key], right[key], (*prefix, key))
                if found:
                    return found
        elif left != right:
            return prefix, left, right
        return None

    difference = first_difference(old, new) or ((), old, new)
    path, before, after = difference
    labels = ".".join(map(str, path))
    if any(key in labels for key in ("read_roots", "read_enforcement")):
        axis = "read"
    elif any(key in labels for key in ("network", "network_allowed", "nested_headless_network")):
        axis = "network"
    elif any(key in labels for key in ("writable_roots", "file_enforcement", "runtime_sandbox")):
        axis = "file"
    elif "execution_access" in labels and not any(
            key in labels for key in ("writable_roots", "read_roots", "network")):
        old_access = old.get("execution_access") or {}
        new_access = new.get("execution_access") or {}
        if not old_access.get("writable_roots") and (old_access.get("read_roots") or new_access.get("read_roots")):
            axis = "read"
        else:
            axis = "file"
    else:
        axis = "adapter-permission"
    evidence = _historical_permission_evidence(previous)
    return (f"axis={axis} field={labels or 'applied_permissions'} "
            f"predecessor={before!r} candidate={after!r} {evidence} child_spawned=0")


def _historical_permission_evidence(previous):
    """Name only canonical predecessor files whose bytes can be verified read-only."""
    import hashlib
    import stat
    jobs = previous.get("jobs")
    attempt = previous.get("attempt_id")
    if not isinstance(jobs, str) or not isinstance(attempt, str) \
            or re.fullmatch(r"att-[A-Za-z0-9._-]{1,240}", attempt) is None:
        return "evidence=historical-input-or-effective-unavailable"
    state_root = Path(jobs).resolve(strict=False).parent
    sealed = state_root / "automatic-replacements" / "inputs" / f"{attempt}.json"
    effective = state_root / "execution-access" / "attempts" / attempt / "effective.json"
    facts = []
    for label, path in (("sealed_input", sealed), ("effective", effective)):
        descriptor = None
        try:
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_size > 4 * 1024 * 1024:
                continue
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                                 | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or opened.st_size > 4 * 1024 * 1024):
                continue
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > 4 * 1024 * 1024:
                    chunks = []
                    break
                chunks.append(chunk)
            if total > 4 * 1024 * 1024:
                continue
            raw = b"".join(chunks)
            decoded = json.loads(raw)
            if label == "sealed_input" and decoded != previous:
                continue
            if label == "effective":
                route_id = previous.get("route_id") or previous.get("owner_route_id")
                if (decoded.get("attempt_id") != attempt or not route_id
                        or decoded.get("route_id") != route_id
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}",
                                            str(decoded.get("route_hash") or ""))):
                    continue
            digest = hashlib.sha256(raw).hexdigest()
            facts.append(f"{label}={path}#sha256:{digest}")
        except OSError:
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return "evidence=" + (";".join(facts) if facts else "historical-input-or-effective-unavailable")


def require_recovery_binding(record, source, candidate, jobs):
    """The same lineage check at task loading, registration and actual launch.

    The caller has already read and digest-checked the claim and original input.
    The record's current route was resolved through the registered owner lineage,
    rather than through candidate-supplied ancestor IDs.
    """
    route_key = "owner_route_id" if source.get("owner_route_id") else "route_id"
    hash_key = "owner_route_hash" if source.get("owner_route_id") else "route_hash"
    if (candidate.get(route_key) != record["route_id"]
            or candidate.get(hash_key) != record["route_hash"]
            or not replacement_parent_matches(source, candidate, jobs)
            or (source.get("dispatch_depth") != "1" and candidate.get("parent") != source.get("parent"))
            or any(candidate.get(key) != source.get(key) for key in RECOVERY_SCOPE_KEYS)):
        raise _contract_error("replacement-launch-binding-mismatch", source.get("attempt_id", ""))
    if source.get("session_chain_id"):
        from dispatch_replacement_subsession import SCOPE_KEYS
        if any(candidate.get(key) != source.get(key) for key in SCOPE_KEYS):
            raise _contract_error("replacement-subsession-scope-mismatch")
    if source.get("review_input_digest") or candidate.get("review_input_digest"):
        from review_input import read_binding
        original = read_binding(jobs, source, verify_current=True)
        successor = read_binding(jobs, candidate, verify_current=True)
        if (any(successor.get(key) != original.get(key) for key in ("path", "sha256", "producer"))
                or successor.get("source") != {
                    "attempt_id": source["attempt_id"], "binding_digest": source["review_input_digest"]}):
            raise _contract_error("reviewed-evidence-replacement-mismatch")


def recovery_node_task(record, source, history, *, attempt_id, route, node, harness, parent, jobs):
    """A node loader consumes the same claim binding as admission, before sealing its input."""
    if record.get("replacement_attempt_id") != attempt_id:
        raise _contract_error("replacement-launch-binding-mismatch")
    candidate = dict(source, attempt_id=attempt_id, route_node=node["id"], parent=parent)
    route_key = "owner_route_id" if source.get("owner_route_id") else "route_id"
    hash_key = "owner_route_hash" if source.get("owner_route_id") else "route_hash"
    candidate.update({route_key: route["route_id"], hash_key: route["route_hash"]})
    if source.get("parent") != parent:
        candidate["parent_sid"] = default_parent_session_id()
    require_recovery_binding(record, source, candidate, jobs)
    selected = sealed_pin_harness(route, worker_type=source.get("worker_type")) or source.get("harness")
    if harness != selected:
        raise _contract_error("replacement-launch-binding-mismatch")
    return recovery_task(record, source, history)


def _work_options(argv, *, harness=None):
    """Read stored semantic options, without synthesizing a new original argv."""
    values = []; index = 0
    while index < len(argv):
        token = argv[index]; key = token.split("=", 1)[0]
        group = token.split("=", 1) if "=" in token else [token]
        if "=" not in token and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            index += 1; group.append(argv[index])
        if (key not in RECOVERY_SELECTION_OPTIONS
                and not (harness == "opencode" and key == "--agent")):
            values.append(group)
        index += 1
    return sorted(values, key=lambda value: value[0])


def require_recovery_work(candidate, history, *, route=None, worker_type=None,
                          transition=None, access=None, parent_values=None):
    """Bind the candidate to stored work and grants, while selection follows current authority.

    A route-less call is the compatibility check for an old standalone launch;
    with a verified current route, its normal launcher owns model realization.
    """
    owner = worker_type == "owner"
    pinned = sealed_pin_harness(route, worker_type=worker_type) if route else None
    keys = ("jobs", "worktree") if owner or pinned else REPLACEMENT_FIXED_KEYS
    for key in keys:
        if candidate.get(key) != history.get(key):
            raise _contract_error("replacement-input-tuple-mismatch", key)
    if pinned and candidate.get("harness") != pinned:
        raise _contract_error("pin-ignored-for-replacement", pinned)
    drift = candidate.get("launch_home") != history.get("launch_home")
    old, new = history.get("resolved") or {}, candidate.get("resolved") or {}
    if transition and new.get("model_profile") != transition["to"]:
        raise _contract_error("replacement-input-tuple-mismatch", "resolved")
    ignored = RECOVERY_SELECTION_VALUES if route else frozenset()
    if owner and route and candidate.get("harness") != history.get("harness"):
        ignored = ignored | {"sandbox", "permission_mode", "parent_sandbox", "parent_transport"}
    for key in sorted(set(old) | set(new)):
        expected = (parent_values or {}).get(key, old.get(key))
        if expected == new.get(key) or key in ignored:
            continue
        if (transition and key in {"model_profile", "model", "reasoning", "resolved_model_settings"}
                and old.get("model_profile") == transition["from"]
                and new.get("model_profile") == transition["to"]
                and (key != "resolved_model_settings" or (new.get(key) or {}).get("profile") == transition["to"])):
            continue
        if not (drift and key in RELEASE_DERIVED_VALUES):
            raise _contract_error("replacement-input-tuple-mismatch", "resolved")
    # Sealing includes the raw argv as history. Check what the candidate will
    # actually receive against its own checked current binding, rather than
    # reconstructing the original command with today's values.
    options = {}
    argv = candidate.get("argv", []); index = 0
    while index < len(argv):
        token = argv[index]; key = token.split("=", 1)[0]
        value = token.split("=", 1)[1] if "=" in token else None
        if value is None and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            index += 1; value = argv[index]
        options.setdefault(key, []).append(value); index += 1
    current = {"--" + key.replace("_", "-"): value for key, value in new.items()}
    if route:
        current.update({"--route-id": route["route_id"], "--route-hash": route["route_hash"]})
    for flag, expected in current.items():
        if ((flag in RECOVERY_SELECTION_OPTIONS
             or (candidate.get("harness") == "opencode" and flag == "--agent"))
                and flag in options and options[flag] != [str(expected)]):
            raise _contract_error("replacement-argv-mismatch", flag)
    sealed, granted = _granted_permission_pair(history, candidate)
    cross_owner = owner and candidate.get("harness") != history.get("harness")
    if cross_owner:
        require_recovery_grant_addresses(candidate, history, route=route, access=access)
    elif (candidate.get("harness") == "opencode"
          and "opencode_selected_agent_permission" not in
              (history.get("applied_permissions") or {})
          and _opencode_selected_permission_view(
              candidate.get("applied_permissions") or {}).get("rules")):
        # A legacy launch did not seal selected-agent policy. Do not infer it
        # from today's config; bound any present candidate file grants by the
        # same approved worktree/read roots before allowing the optional field
        # to be absent from one side of the equality comparison.
        require_recovery_grant_addresses(candidate, history, route=route, access=access)
    if access:
        if (granted.get("execution_access") or {}).get("request_sha256") != access["request_sha256"]:
            raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
        granted, sealed = _without_recovery_access(granted), _without_recovery_access(sealed)
    # A cross-harness owner goes through the ordinary access-grant launcher.
    # Its runtime permission spelling differs, while the exact approved request
    # and addresses above remain bound. Same-harness grants stay comparable.
    if not cross_owner and granted != sealed:
        detail = permission_mismatch_detail(history, candidate)
        raise _contract_error("replacement-input-tuple-mismatch", detail or "applied_permissions")
    if (not owner and _work_options(candidate.get("argv", []), harness=candidate.get("harness"))
            != _work_options(history.get("argv", []), harness=history.get("harness"))):
        raise _contract_error("replacement-argv-mismatch")


def _opencode_permission_view(applied):
    import opencode_runtime
    if not isinstance(applied, dict) or "opencode_permission" not in applied:
        try:
            return _opencode_selected_permission_view(applied or {}).get("rules", [])
        except (AttributeError, TypeError, ValueError):
            return []
    try:
        view = opencode_runtime.opencode_permission_ordered_view(
            applied.get("opencode_permission"))
        return view or _opencode_selected_permission_view(applied).get("rules", [])
    except ValueError:
        return []


def _opencode_policy_view(permission):
    """Normalize either a raw policy or its sealed ordered-view form."""
    import opencode_runtime
    if (isinstance(permission, dict) and set(permission) == {"rules", "precedence"}
            and isinstance(permission.get("rules"), list)
            and isinstance(permission.get("precedence"), list)):
        rules = []
        for row in permission["rules"]:
            if (not isinstance(row, (list, tuple)) or len(row) != 3
                    or not all(isinstance(value, str) and value for value in row)):
                raise ValueError("invalid ordered OpenCode rule")
            rules.append(tuple(row))
        known = set(rules)
        precedence = []
        for edge in permission["precedence"]:
            if (not isinstance(edge, (list, tuple)) or len(edge) != 2
                    or any(not isinstance(row, (list, tuple)) or len(row) != 3
                           for row in edge)):
                raise ValueError("invalid ordered OpenCode precedence")
            before, after = tuple(edge[0]), tuple(edge[1])
            if before not in known or after not in known:
                raise ValueError("unbound OpenCode precedence")
            precedence.append((before, after))
        return {
            "rules": [list(row) for row in sorted(set(rules))],
            "precedence": [[list(before), list(after)]
                           for before, after in sorted(set(precedence))],
        }
    return opencode_runtime.opencode_permission_ordered_view(permission)


def _opencode_selected_permission_view(applied):
    """Return selected rules without the agent label, which is not a grant."""
    if not isinstance(applied, dict) or "opencode_selected_agent_permission" not in applied:
        return {}
    record = applied.get("opencode_selected_agent_permission")
    if not isinstance(record, dict) or "permission" not in record:
        raise ValueError("invalid selected OpenCode policy record")
    permission = record.get("local_policy", record.get("permission"))
    view = _opencode_policy_view(permission)
    return view if isinstance(view, dict) else {"rules": [], "precedence": []}


def _effective_opencode_policy(applied):
    """Compose the sealed global policy followed by the selected local override."""
    import opencode_runtime
    global_view = _opencode_policy_view((applied or {}).get("opencode_permission"))
    selected_view = _opencode_selected_permission_view(applied or {})
    global_rows = [tuple(row) for row in global_view.get("rules", [])]
    selected_rows = [tuple(row) for row in selected_view.get("rules", [])]
    rows = global_rows + selected_rows
    precedence = {
        (tuple(edge[0]), tuple(edge[1]))
        for view in (global_view, selected_view)
        for edge in view.get("precedence", [])
    }
    # The native launcher applies selected-agent rows after the global rows.
    # Add only precedence that can decide a shared action/resource; unrelated
    # rules remain in their own policy order.
    for before in global_rows:
        for after in selected_rows:
            if (before[2] != after[2]
                    and opencode_runtime._opencode_patterns_overlap(before[0], after[0])
                    and opencode_runtime._opencode_patterns_overlap(before[1], after[1])):
                precedence.add((before, after))
    return rows, precedence


def _opencode_effect_from_policy(rows, precedence, action, resource, default):
    import opencode_runtime
    matching = [row for row in rows
                if opencode_runtime._opencode_pattern_matches(row[0], action)
                and opencode_runtime._opencode_pattern_matches(row[1], resource)]
    if not matching:
        return default
    applicable = set(matching)
    final = [row for row in matching
             if not any(before == row and after in applicable
                        for before, after in precedence)]
    effects = {row[2] for row in final}
    # A malformed or ambiguous order must be treated as potentially allowed
    # by the caller's recovery deny check.
    return next(iter(effects)) if len(effects) == 1 else "allow"


def require_recovery_grant_addresses(candidate, history, *, route, access=None):
    """Translate adapter grant spellings to the existing approved task addresses.

    Legacy inputs without an execution-access envelope use the sealed worktree,
    artifact root and the same task-target resolver as the normal launcher.
    Nothing is persisted and no additional input is requested.
    """
    import execution_access as EA
    applied = candidate.get("applied_permissions") or {}
    old_permissions = history.get("applied_permissions") or {}
    original = old_permissions.get("execution_access") or {}
    network_allowed = (original.get("network") in {"enforced", "granted-unenforced"}
                       or old_permissions.get("nested_headless_network") is True)
    if candidate.get("harness") == "codex":
        from dispatch_contract import codex_standard_owner_network_enabled
        selected = candidate.get("resolved") or {}
        # This is the normal standard+ owner's existing dispatch support grant.
        # Its intensity/depth are already bound to the approved work above.
        network_allowed = network_allowed or codex_standard_owner_network_enabled(
            dispatch_depth=int(selected.get("dispatch_depth") or 0),
            worker_type=selected.get("worker_type", ""),
            intensity=selected.get("intensity", ""), sandbox=selected.get("sandbox", ""))
    writes = [history.get("worktree"), (route or {}).get("artifact_root"), *original.get("writable_roots", [])]
    reads = [*writes, *original.get("read_roots", [])]
    if route:
        context = EA.AccessContext.build(worktree=history["worktree"], artifact_root=route["artifact_root"],
            dispatch_state_root=Path(history["jobs"]).parent,
            agent_home=candidate.get("launch_home") or Path(__file__).resolve().parents[1])
        # Ordinary launchers grant installed contracts and utilities read
        # visibility with edit denies. Recovery uses the same source addresses.
        reads.extend(contract_read_roots(context.agent_home, route))
        targets = EA.resolve_task_targets(route)
        if targets:
            writes.extend(targets.writable_roots)
        derived = EA.derive_task_access(route, context, writable=tuple(Path(value) for value in writes if value))
        writes.extend(derived.writable_roots); reads.extend(derived.read_roots)
        if access:
            request = EA.load_request(Path(access["request_path"]), context=context)
            if request.request_sha256 != access["request_sha256"]:
                raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
            writes.extend(request.writable_roots); reads.extend(request.read_roots)
            network_allowed = request.network_required
    writes = [Path(value).resolve(strict=False) for value in writes if value]
    reads = [*writes, *(Path(value).resolve(strict=False) for value in reads if value)]

    def require(value, roots, *, relative_to=None):
        value = str(value)
        path = Path(value.removesuffix("/**").removesuffix("/*"))
        if not path.is_absolute() and relative_to is not None:
            path = Path(relative_to) / path
        path = path.resolve(strict=False)
        if "*" in str(path) or not any(path == root or root in path.parents for root in roots):
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")

    grant = applied.get("execution_access") or {}
    if (not access and original.get("request_sha256")
            and grant.get("request_sha256") != original["request_sha256"]):
        raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
    if (grant.get("network") in {"enforced", "granted-unenforced"}
            or applied.get("nested_headless_network") is True) and not network_allowed:
        raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
    for key in ("writable_roots", "additional_writable_roots", "absorbed_writable_roots"):
        for value in grant.get(key, []):
            require(value, writes)
    for value in grant.get("read_roots", []):
        require(value, reads)
    import opencode_runtime
    permissions = {}
    policy_rows, policy_precedence = [], set()
    if candidate.get("harness") == "opencode":
        try:
            permissions = opencode_runtime.normalize_opencode_permission(
                applied.get("opencode_permission"), field="opencode_permission")
            if not opencode_runtime.opencode_permission_ordered_view(
                    applied.get("opencode_permission")):
                raise ValueError("empty permission policy")
            policy_rows, policy_precedence = _effective_opencode_policy(applied)
        except (AttributeError, TypeError, ValueError):
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
    for category, roots in (("external_directory", reads), ("edit", writes)):
        rules = [(row[1], row[2]) for row in policy_rows
                 if opencode_runtime._opencode_pattern_matches(row[0], category)]
        for pattern, effect in rules:
            if effect == "allow" and not (category == "edit" and pattern in {"*", "**"}):
                require(pattern, roots, relative_to=history["worktree"] if category == "edit" else None)
        if category == "edit":
            # Native edit defaults apply within the externally scoped roots.
            # Preserve the normal launcher's '*' allow, while requiring its
            # read-only roots to retain their effective edit deny.
            for root in reads:
                if any(root == write or write in root.parents for write in writes):
                    continue
                visible = _opencode_effect_from_policy(
                    policy_rows, policy_precedence, "external_directory", str(root), "deny")
                if visible != "allow":
                    continue
                for path in (root, root / "__recovery_read_only_child__"):
                    forms = (str(path), os.path.relpath(path, history["worktree"]))
                    for form in forms:
                        effect = _opencode_effect_from_policy(
                            policy_rows, policy_precedence, "edit", form, "allow")
                        if effect != "deny":
                            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
    for rule in (applied.get("claude") or {}).get("allowed_tools") or []:
        match = re.fullmatch(r"(Read|Edit|Write)(?:\(([^)]+)\))?", rule)
        if not match:
            continue
        tool, path = match.groups()
        if not path:
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
        require(path[1:] if path.startswith("//") else path,
                reads if tool == "Read" else writes, relative_to=history["worktree"])


def _without_recovery_access(permissions):
    result = {key: value for key, value in permissions.items() if key != "execution_access"}
    def without_file_rules(policy):
        if not isinstance(policy, dict) or not isinstance(policy.get("rules"), list):
            return policy
        removed = {tuple(row) for row in policy["rules"]
                   if isinstance(row, list) and row and row[0] in ("external_directory", "edit")}
        kept = [row for row in policy["rules"] if tuple(row) not in removed]
        edges = []
        for edge in policy.get("precedence", []):
            if (isinstance(edge, list) and len(edge) == 2
                    and tuple(edge[0]) not in removed and tuple(edge[1]) not in removed):
                edges.append(edge)
        return {
            "rules": kept,
            "precedence": edges,
        }
    policy = result.get("opencode_permission")
    if isinstance(policy, dict) and isinstance(policy.get("rules"), list):
        result["opencode_permission"] = without_file_rules(policy)
    elif isinstance(policy, dict):
        result["opencode_permission"] = {key: value for key, value in policy.items()
                                         if key not in ("external_directory", "edit")}
    selected = result.get("opencode_selected_agent_permission")
    if isinstance(selected, dict):
        local = without_file_rules(selected.get("permission"))
        if not isinstance(local, dict) or not local.get("rules"):
            result.pop("opencode_selected_agent_permission", None)
        else:
            result["opencode_selected_agent_permission"] = {"permission": local}
    return result


def continuation_attempt_state(metadata):
    """Execution adopts a successor; preparation and an unconsumed claim do not.

    A legacy row lacking launch evidence remains unknown, not an invented
    no-spawn proof. PID-bound or unresolved claimed launches remain attached.
    """
    if metadata.get("launch_started") == "1":
        return "started"
    if metadata.get("pid"):
        return "unknown"
    if metadata.get("launch_outcome") == "never-launched":
        return "unstarted"
    if metadata.get("launch_claimed") == "0" and metadata.get("launch_started") != "1":
        return "unstarted"
    return "unknown"


def _recorded_pid_reused(row, identity):
    """The recorded PID now names a later process in the same PID namespace.

    A later start tick for the same PID in the recorded namespace means the
    recorded process has ended. A same-start mismatch (an exec), an earlier or
    unparsable start tick, or an unrecorded or unreadable namespace proves
    nothing and stays stale.
    """
    state, current, reason = identity
    if state != "stale" or reason != "process-identity-mismatch" or not current:
        return False
    if row.get("pid_namespace") is None:
        return False
    try:
        if row["pid_namespace"] != os.readlink("/proc/self/ns/pid"):
            return False
        recorded, observed = int(row.get("starttime")), int(current.get("starttime"))
    except (OSError, TypeError, ValueError):
        return False
    return observed > recorded


def resource_predecessor_finished(row, *, observation=None):
    """A resource's exact ended execution, independent of its scientific verdict."""
    from resource_run_registry import (
        classify_identity, resource_never_started, resource_process_group_quiescent,
        reboot_evidence,
    )
    from dispatch_resource_wait import supervisor
    execution = row.get("execution") if isinstance(row, dict) else None
    compute = isinstance(execution, dict) and execution.get("kind") == "compute-hosts"
    if row.get("cancel_requested"):
        if (row.get("cancel_requested") is not True
                or row.get("resource_policy") not in {"verified-resume", "supervised-owner"}
                or row.get("status") != "failed" or row.get("ended_at") is None
                or row.get("parent_close_requested")
                or row.get("launch_state") in {"queued", "claimed", "launching"}):
            return False
        if compute:
            return (row.get("resource_policy") == "supervised-owner"
                    and (observation.get("state") == "quiescent" if observation is not None
                         else resource_process_group_quiescent(row)))
        # Cancellation may leave no sentinel. Require positive group evidence;
        # the legacy PID-only fallback cannot prove that children were reaped.
        return (bool(row.get("pid_namespace")) and row.get("process_group") is not None
                and classify_identity(row)[0] == "exited"
                and resource_process_group_quiescent(row))
    if compute:
        if (row.get("resource_policy") != "supervised-owner"
                or row.get("parent_close_requested")
                or row.get("status") not in {"failed", "succeeded"}):
            return False
        code = supervisor().runner().read_sentinel(row.get("sentinel"))
        return (code is not None and row.get("exit_code") == code
                and not (row.get("status") == "failed" and code == 0)
                and not (row.get("status") == "succeeded" and code != 0)
                and (observation.get("state") == "quiescent" if observation is not None
                     else resource_process_group_quiescent(row)))
    if resource_never_started(row):
        return True
    identity = classify_identity(row)
    # A foreign namespace prevents PID observation, not the recorded exit.
    # The sentinel/exit consistency checks below still prove that exact end.
    recorded_foreign_exit = (identity == ("stale", None, "process-namespace-mismatch")
                             and row.get("status") in {"failed", "succeeded"}
                             and row.get("exit_code") is not None)
    if (row.get("status") == "launching" or row.get("cancel_requested")
            or row.get("parent_close_requested")
            or (identity[0] != "exited" and not _recorded_pid_reused(row, identity)
                and not recorded_foreign_exit)):
        return False
    if (row.get('status') != 'succeeded' and row.get('exit_code') is None
            and reboot_evidence(row)):
        return True
    code = supervisor().runner().read_sentinel(row.get("sentinel"))
    # A foreign recorded exit proves the leader's result only. Supervised
    # resources still need the independent process-group quiescence check.
    return (code is not None and row.get("exit_code", code) == code
            and not (row.get("status") == "failed" and code == 0)
            and not (row.get("status") == "succeeded" and code != 0)
            and (row.get("resource_policy") != "supervised-owner"
                 or resource_process_group_quiescent(row)))


def require_resource_predecessors(rows, candidate, *, observations=None):
    """Recheck execution and evidence-address protection within the existing reservation lock."""
    from dispatch_resource_wait import resource_evidence_paths_conflict
    observations = observations or {}
    for row in rows:
        execution = row.get("execution") if isinstance(row, dict) else None
        if isinstance(execution, dict) and execution.get("kind") == "compute-hosts":
            captured = observations.get(row.get("run_id"))
            if (not captured or captured.get("row") != row
                    or not resource_predecessor_finished(row, observation=captured)):
                raise ValueError("resource-route-body-conflict")
        elif not resource_predecessor_finished(row):
            raise ValueError("resource-route-body-conflict")
        if resource_evidence_paths_conflict(row, candidate):
            raise ValueError("resource-route-body-conflict")


# Same work, another launcher. Never-started work may be taken over by a launcher that runs
# somewhere else: inside the parent's OS sandbox (a Codex owner's tool shell) or on the host
# beside it (the session supervisor advancing a serial chain). Where a launcher runs decides
# these realized values -- the wrapper's lifetime scope and, for Codex, whether its own sandbox
# nests inside the parent's -- while the work and the permissions granted to it (argv with
# `--sandbox` and the parent tuple, network, the access grant, Claude/OpenCode permissions)
# stay the same and are still compared.
LAUNCH_LOCATION_VALUES = frozenset({"launch_lifecycle"})

# The sealed launch input of one attempt: the work and its granted permissions.
RESEAL_STABLE_KEYS = ("schema", "attempt_id", "harness", "jobs", "worktree", "argv", "task", "retry_brief",
                      "route_id", "route_node", "owner_route_id")

# The registry row of one never-started attempt. Per-launch values (lease nonce, release home,
# parent runtime pid, sealed input digest, lifetime scope) may differ.
RELAUNCH_STABLE_KEYS = (
    "attempt_id", "attempt_schema_version", "harness", "worker_type", "dispatch_depth",
    "capability", "parent_sid", "parent_attempt_id", "route_id", "route_hash", "route_node",
    "owner_route_id", "owner_route_hash", "automatic_retry_of",
    "replacement_original_attempt_id", "replacement_family_id", "replacement_claim_digest",
)


# A path into the harness's own tree inside a permission value (`<root>/utilities/...`).
_RELEASE_TREE_PATH = re.compile(r"(/[^\s\"'()*]*?)/(?:core|capabilities|roles|utilities|adapters|docs|hooks|profiles|tools)(?=/|[\"'])")
RELEASE_ROOT_TOKEN = "<launch_home>"


def granted_permissions(applied, launch_home=None, worktree=None) -> dict:
    """``applied_permissions`` without the values the launcher's location decides.

    With ``launch_home`` (the release the launch ran from), a path into the harness's own tree
    reads the same however its root was spelled: that release directory, or, for a managed
    release, another release of the same install or its ``<share>/hearting/current`` pointer
    (which may point at a newer release by now). The spelling depends only on how the launcher
    was invoked, and the release is ``launch_home``'s axis. Any other harness tree, such as a
    development checkout, stays a different path."""
    granted = {key: value for key, value in (applied or {}).items() if key not in LAUNCH_LOCATION_VALUES}
    if launch_home:
        text = json.dumps(granted, sort_keys=False)
        home = os.path.realpath(str(launch_home))
        releases = os.path.dirname(home)
        managed = os.path.basename(releases) == "releases"
        pointer = os.path.join(os.path.dirname(releases), "current")
        roots = {root for root in _RELEASE_TREE_PATH.findall(text)
                 if root == str(launch_home) or os.path.realpath(root) == home
                 or (managed and (root == pointer or os.path.dirname(os.path.realpath(root)) == releases))}
        # OpenCode edit rules check the same directory relative to the worktree.
        # Normalize only aliases of the identified launch roots, never arbitrary
        # relative task paths or another development checkout.
        if worktree:
            roots.update(os.path.relpath(root, worktree) for root in tuple(roots))
        patterns = [re.compile(r"(?<![^\s\"'(])" + re.escape(root) + "/")
                    for root in sorted(roots, key=len, reverse=True)]

        def normalize_paths(value):
            if isinstance(value, str):
                for pattern in patterns:
                    value = pattern.sub(RELEASE_ROOT_TOKEN + "/", value)
                return value
            if isinstance(value, list):
                return [normalize_paths(item) for item in value]
            if isinstance(value, dict):
                result = {}
                for key, item in value.items():
                    normalized_key = normalize_paths(key) if isinstance(key, str) else key
                    if normalized_key in result:
                        del result[normalized_key]
                    result[normalized_key] = normalize_paths(item)
                return result
            return value

        granted = normalize_paths(granted)
    if "opencode_permission" in granted:
        try:
            import opencode_runtime
            granted["opencode_permission"] = opencode_runtime.opencode_permission_ordered_view(
                granted["opencode_permission"])
        except ValueError:
            granted["opencode_permission"] = []
    if "opencode_selected_agent_permission" in granted:
        try:
            granted["opencode_selected_agent_permission"] = {
                "permission": _opencode_selected_permission_view(granted)
            }
        except (AttributeError, TypeError, ValueError):
            # Keep malformed policy evidence unequal and omit only the agent
            # label, which is not itself a permission grant.
            selected = dict(granted["opencode_selected_agent_permission"] or {})
            selected.pop("agent", None)
            granted["opencode_selected_agent_permission"] = selected
    return granted


def _same_verified_workspace_write_parent(previous, current) -> bool:
    """Whether both sealed launches name the same known Codex workspace-write parent.

    The current launch's parent tuple is revalidated by the existing wrapper before
    launch-input sealing. Requiring the complete tuple to match its predecessor keeps
    this normalization tied to that exact boundary; absent, unknown, or changed parent
    evidence leaves the launcher sandbox values distinct.
    """
    if previous.get("harness") != "codex" or current.get("harness") != "codex":
        return False
    old, new = previous.get("resolved") or {}, current.get("resolved") or {}
    depth = str(old.get("dispatch_depth") or "")
    if depth not in {"1", "2"} or str(new.get("dispatch_depth") or "") != depth:
        return False
    for key in ("parent_harness", "parent_transport", "parent_sandbox", "launch_authority"):
        value = old.get(key)
        if (not isinstance(value, str) or not value.strip() or value.lower() == "unknown"
                or value != new.get(key)):
            return False
    if old.get("parent_harness") != "codex" or old.get("parent_sandbox") != "workspace-write":
        return False
    for key in ("parent_session_id", "parent_attempt_id"):
        identity = old.get(key)
        if identity != new.get(key):
            return False
        if (identity is not None and
                (not isinstance(identity, str) or not identity.strip() or identity.lower() == "unknown")):
            return False
    identity_key = "parent_session_id" if depth == "1" else "parent_attempt_id"
    identity = old.get(identity_key)
    if (not isinstance(identity, str) or not identity.strip() or identity.lower() == "unknown"
            or identity != new.get(identity_key)):
        return False
    return True


def _legacy_selected_nonfile_grants_bounded(old_applied, new_applied) -> bool:
    """Prove candidate selected non-file allows were already granted globally."""
    import opencode_runtime
    try:
        selected = _opencode_selected_permission_view(new_applied).get("rules", [])
        global_policy = _opencode_policy_view((old_applied or {}).get("opencode_permission"))
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(global_policy, dict):
        global_policy = {"rules": [], "precedence": []}
    global_rows = [tuple(row) for row in global_policy.get("rules", [])]
    precedence = {
        (tuple(edge[0]), tuple(edge[1]))
        for edge in global_policy.get("precedence", [])
    }

    def covers(cover, pattern):
        # OpenCode's legacy `tool *` action spelling also matches bare `tool`.
        if cover.endswith(" *") and pattern == cover[:-2]:
            return True
        return opencode_runtime._opencode_pattern_covers(cover, pattern)

    def overlaps(left, right):
        return (opencode_runtime._opencode_patterns_overlap(left[0], right[0])
                and opencode_runtime._opencode_patterns_overlap(left[1], right[1]))

    for row in selected:
        action, resource, effect = tuple(row)
        if effect != "allow" or action in {"edit", "external_directory"}:
            continue
        covers_all = [global_row for global_row in global_rows
                      if global_row[2] == "allow"
                      and covers(global_row[0], action)
                      and covers(global_row[1], resource)]
        blockers = [global_row for global_row in global_rows
                    if global_row[2] != "allow" and overlaps(global_row, row)]
        if not any(all((blocker, global_row) in precedence for blocker in blockers)
                   for global_row in covers_all):
            return False
    return True


def _granted_permission_pair(previous, current) -> tuple[dict, dict]:
    """Compare effective grants, collapsing only a proven nested Codex sandbox layer."""
    old_applied = previous.get("applied_permissions") or {}
    new_applied = current.get("applied_permissions") or {}
    old = granted_permissions(old_applied, previous.get("launch_home"), previous.get("worktree"))
    new = granted_permissions(new_applied, current.get("launch_home"), current.get("worktree"))
    selected_key = "opencode_selected_agent_permission"
    if selected_key not in old_applied and selected_key in new_applied:
        same_opencode_history = (previous.get("harness") == "opencode"
                                 and current.get("harness") == "opencode")
        if (not same_opencode_history
                or _legacy_selected_nonfile_grants_bounded(old_applied, new_applied)):
            # The immutable legacy input did not record this field. Omit only
            # non-file grants already covered by its global policy; file grants
            # retain their separate approved-address check.
            old.pop(selected_key, None)
            new.pop(selected_key, None)
    elif selected_key not in new_applied:
        # Preserve compatibility when the current launch omits the optional
        # record; that absence supplies no candidate selected policy to compare.
        old.pop(selected_key, None)
        new.pop(selected_key, None)
    if previous.get("harness") == "opencode" and not _opencode_permission_view(old_applied):
        old = dict(old)
        old["__opencode_permission_authority__"] = "missing-predecessor"
    if current.get("harness") == "opencode" and not _opencode_permission_view(new_applied):
        new = dict(new)
        new["__opencode_permission_authority__"] = "missing-candidate"
    if _same_verified_workspace_write_parent(previous, current):
        if (old_applied.get("launch_lifecycle") == "foreground-scoped"
                and new_applied.get("launch_lifecycle") == "detached"
                and old.get("runtime_sandbox") == "danger-full-access"):
            old = dict(old)
            old["runtime_sandbox"] = "workspace-write"
        elif (new_applied.get("launch_lifecycle") == "foreground-scoped"
                and old_applied.get("launch_lifecycle") == "detached"
                and new.get("runtime_sandbox") == "danger-full-access"):
            new = dict(new)
            new["runtime_sandbox"] = "workspace-write"
    return old, new


# A sealed fact and the same value computed again now. The release a launch ran from is where it
# ran, not what work it is: its row and sealed input keep the `launch_home` it ran from, a
# continuation records its own, and a different current value on that axis is neither a refusal
# nor a diagnostic. That covers a replacement's `launch_home` and the values the runtime derives
# from its release, and the launch roots of a managed release that moved to another verified
# managed release copy (`release_moved`). The launcher's location is `LAUNCH_LOCATION_VALUES`; the
# process table yields to the drain receipt its watcher sealed
# (`dispatch_contract._denied_process_outside_attempt`). The work, the permissions granted to
# it and the route's own seal are still compared. The portable model profile
# is approved work quality, not a value derived from the release.
RELEASE_DERIVED_VALUES = frozenset({
    "model", "reasoning", "resolved_model_settings", "resolved_completion_delivery",
    "parent_completion_delivery", "execution_surface", "fallback_hop", "model_role"})
RELEASE_LAUNCH_ROOTS = frozenset({"registry_root", "launch_home", "runtime_root", "wrapper_root"})
_RELEASE_IDENTITY_FIELDS = frozenset({"release_id", "content_digest", "binding_digest"})
_MANAGED_RELEASE_ID = re.compile(r"release:[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[0-9a-f]{12}")


def release_moved(sealed: dict, current: dict, *, managed_release) -> frozenset:
    """The sealed launch roots that differ from the current ones only because the installed
    release moved: from one managed release (as sealed) to another verified managed release
    copy (``managed_release(path)``). Each release root must lie in its own side's runtime
    root, and the registry (`jobs_path`) may differ only by the release fields its runtime
    resolved. A runtime home or other projection tree, a development checkout, and a
    malformed root are never a release move."""

    def root(roots, name):
        value = roots.get(name) if isinstance(roots, dict) else None
        if (isinstance(value, dict) and value.get("kind") == name
                and isinstance(value.get("path"), str) and os.path.isabs(value["path"])):
            return value
        return None

    def inside(value, runtime):
        path, top = Path(value["path"]), Path(runtime["path"])
        return path == top or top in path.parents

    old, new = root(sealed, "runtime_root"), root(current, "runtime_root")
    if (old is None or new is None or not _MANAGED_RELEASE_ID.fullmatch(str(old.get("release_id") or ""))
            or not managed_release(new["path"])):
        return frozenset()
    moved = {name for name in RELEASE_LAUNCH_ROOTS
             if root(sealed, name) is not None and root(current, name) is not None
             and inside(root(sealed, name), old) and inside(root(current, name), new)}
    before, after = root(sealed, "jobs_path"), root(current, "jobs_path")
    if (before is not None and after is not None
            and {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            <= _RELEASE_IDENTITY_FIELDS):
        moved.add("jobs_path")
    return frozenset(moved)


def same_sealed_work(previous, current, *, recovery=None) -> bool:
    """Whether two sealed launch inputs describe the same work with the same granted permissions."""
    old_grants, new_grants = _granted_permission_pair(previous, current)
    old_applied = previous.get("applied_permissions") or {}
    new_applied = current.get("applied_permissions") or {}
    selected_key = "opencode_selected_agent_permission"
    if (current.get("harness") == "opencode"
            and selected_key not in old_applied and selected_key in new_applied):
        try:
            selected_view = _opencode_selected_permission_view(new_applied)
            has_candidate_allow = any(row[2] == "allow" for row in selected_view.get("rules", []))
        except (AttributeError, TypeError, ValueError):
            return False
        if has_candidate_allow:
            route = recovery[3] if recovery is not None else None
            access = recovery[0].get("execution_access") if recovery is not None else None
            from dispatch_contract import DispatchContractError
            try:
                require_recovery_grant_addresses(
                    current, previous, route=route, access=access)
            except DispatchContractError:
                return False
    exact = (all(previous.get(key) == current.get(key) for key in RESEAL_STABLE_KEYS
                 if key != "argv")
            and _same_reseal_argv(previous, current)
            and old_grants == new_grants)
    if exact or recovery is None:
        return exact
    record, source, history, route = recovery
    if (source.get("worker_type") != "owner"
            or any(previous.get(key) != current.get(key) for key in RESEAL_STABLE_KEYS
                   if key not in {"harness", "argv"})
            or current.get("task") != recovery_task(record, source, history)):
        return False
    from dispatch_contract import DispatchContractError
    try:
        require_recovery_work(current, history, route=route, worker_type="owner", access=record.get("execution_access"))
    except DispatchContractError:
        return False
    return True


def _same_reseal_argv(previous, current) -> bool:
    """Ignore only OpenCode's agent label; its recorded policy is compared separately."""
    old = previous.get("argv", [])
    new = current.get("argv", [])
    if previous.get("harness") != "opencode" or current.get("harness") != "opencode":
        return old == new

    def without_agent(argv):
        result = []
        index = 0
        while index < len(argv):
            token = argv[index]
            if token.startswith("--agent="):
                index += 1
                continue
            if (token == "--agent" and index + 1 < len(argv)
                    and not argv[index + 1].startswith("--")):
                index += 2
                continue
            result.append(token)
            index += 1
        return result

    return without_agent(old) == without_agent(new)


# ---------------------------------------------------------------------------
# 3. How many attempts, of which kind
# ---------------------------------------------------------------------------

def no_stage_authority(meta) -> bool:
    """A row the full-stage round census leaves out (`stage_authority=0`)."""
    return str(meta.get("stage_authority", "1")).lower() in {"0", "false"}


def subsession_row(meta) -> bool:
    """A row that is a sub-session by either mark; it never closes a stage gate itself."""
    return bool(meta.get("subsession_id")) or no_stage_authority(meta)


def linked_worktree_slice(meta) -> bool:
    """A sub-session slice (`stage_authority=0`) runs in a linked worktree while its
    owner row keeps the route cwd, so only a slice may differ from its parent's
    worktree; every other identity comparison stays exact."""
    return bool(meta.get("subsession_id")) and str(meta.get("stage_authority", "")) == "0"


def subsession_launch(subsession_id, stage_authority) -> bool:
    """A launch declared as a sub-session or without stage authority (parsed arguments)."""
    return bool(subsession_id) or stage_authority == 0


def declared_subsession(args) -> bool:
    """The one stage-session declaration judgment, read by `bind` and the dry-run preview.

    True for a complete sub-session declaration (every axis, `stage_authority=0`,
    bound to a dispatch-depth-2 route node), False for an ordinary full-stage
    launch. A partial or contradictory declaration is refused, so no caller can
    turn one raw flag into a sub-session.
    """

    def value(name):
        return getattr(args, name, None)

    values = tuple(value(name) for name in (
        "subsession_id", "subsession_index", "subsession_count", "subsession_mode",
        "session_chain_id", "phase_brief", "narrow_verify", "expected_round_trips",
    ))
    if any(item is not None for item in values) and not all(item is not None for item in values):
        raise _contract_error("subsession-arguments-incomplete", "all stage-session axes are required")
    stage_authority = getattr(args, "stage_authority", 1)
    if not value("subsession_id"):
        if stage_authority != 1:
            raise _contract_error(
                "stage-authority-zero-without-subsession", str(value("route_node") or "")
            )
        return False
    if stage_authority != 0:
        raise _contract_error("subsession-stage-authority-forbidden", value("subsession_id"))
    if value("dispatch_depth") != 2 or not value("route_id") or not value("route_node"):
        raise _contract_error("subsession-route-binding-invalid", value("subsession_id"))
    return True


def new_owner_request(previous_row, *, jobs=None, task=None):
    """A changed sealed assignment after a settled attempt, without rewriting it."""
    if (jobs is None or not isinstance(task, str) or not task.strip()
            or previous_row.get("_status") not in {"done", "closed"}
            or committed_outcome(previous_row["_status"], previous_row) not in {"failed", "succeeded"}):
        return False
    from dispatch_contract import (
        DispatchContractError, attempt_process_quiescence, terminal_conflict_pending,
    )
    from dispatch_replacement import launch_input
    try:
        previous = launch_input(jobs, previous_row["attempt_id"], previous_row)["task"]
        return (isinstance(previous, str) and bool(previous.strip())
                and " ".join(previous.split()) != " ".join(task.split())
                and not terminal_conflict_pending(previous_row)
                and attempt_process_quiescence(
                    previous_row, terminal_receipt=True).state == "quiescent")
    except (DispatchContractError, OSError, ValueError, KeyError, TypeError):
        # Missing history cannot manufacture a new request boundary.
        return False


def retry_predecessor(prior_rows, *, jobs=None, task=None):
    """The transport retry a new launch of this node continues, or "".

    Only a transport failure (a death, a runtime error) is retried in place.
    A worker's readable FAIL or BLOCKED is its result, on a capped node or
    not: the next launch is new work, so it never inherits a retry link and
    never becomes automatic transport replacement. Capped nodes still count it as a
    round through the shared round admission. A different explicit request on
    an uncapped stage is also new work once the old execution is quiescent.
    Callers keep capped rounds on their existing admission path. No original
    row or automatic replacement allowance is changed.
    """
    if not prior_rows:
        return ""
    latest = prior_rows[-1]
    status = latest["_status"]
    if committed_outcome(status, latest) == "failed":
        if readable_result(latest):
            return ""
        if new_owner_request(latest, jobs=jobs, task=task):
            return ""
        return latest.get("attempt_id", "")
    if status == "open" and latest.get("launch_claimed") == "0":
        # Register/start reuse the same unlaunched transport successor. A
        # semantic round has no such link and keeps its own round identity.
        return latest.get("automatic_retry_of", "")
    return ""


def _blocked_round_items(route, metadata):
    """The unmet `done_when` ids a BLOCKED round left, from the items file beside its artifact;
    None when that round's items cannot be judged (no plan items, no readable file, or a file
    bound to another route, node or attempt)."""
    if readable_result(metadata) != "BLOCKED":
        return None
    import base64
    import route_plan as RP
    from codex_dispatch_terminal import inspect_terminal_attempt
    try:
        terminal = inspect_terminal_attempt(
            metadata.get("log_file"), worktree=metadata.get("cwd") or route.get("cwd"),
            artifact_root_metadata=metadata.get("artifact_root") or route.get("artifact_root"),
            artifact_binding=metadata)
        encoded = terminal.get("artifact_path_b64")
        if not encoded:
            return None
        artifact = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        items = RP.read_leg_items(route, artifact)
    except (OSError, ValueError, TypeError):
        return None
    if (items is None or items.get("attempt_id") != metadata.get("attempt_id")
            or items.get("node") != metadata.get("route_node")):
        return None
    return frozenset(items["unmet"])


def blocked_progress(route, rows, *, worker_type="test") -> int:
    """How many of the newest rounds, all without a verdict, count toward the verdict-less bound
    once progress is taken into account (RA-5).

    Two BLOCKED rounds in a row bind a node. A BLOCKED round whose unmet `done_when` items are a
    strict subset of the previous BLOCKED round's is progress: the count starts again at it. When
    either round's items cannot be judged (no plan items), nothing resets, exactly as before.
    `worker_type` is the node's own, as `round_budget` reads it, for a row that names none."""
    tail = []
    for status, metadata in reversed(list(rows)):
        kind = classify_round_row(status, metadata, worker_type=metadata.get("worker_type") or worker_type)
        if kind != "verdict-less":
            break
        tail.append(metadata)
    tail.reverse()
    streak, previous = 0, None
    for metadata in tail:
        current = _blocked_round_items(route, metadata) if route.get("route_plan") is not None else None
        streak = 1 if previous is not None and current is not None and current < previous else streak + 1
        previous = current
    return streak


def runtime_owner_can_resume(status, meta) -> bool:
    """A launched supervised owner's runtime end still has its same-route work.

    Admission also checks actual process quiescence; this metadata judgment
    keeps the route's sealed contract available while it awaits continuation.
    """
    return (status == "done" and meta.get("worker_type") == "owner"
            and meta.get("launch_started") == "1"
            and meta.get("supervisor_lease") == "flock-v1"
            and bool(meta.get("supervisor_lease_file"))
            and (meta.get("note") in {"dead-runtime-exit", "dead-runtime-error"}
                 or (meta.get("note") == "dead-protocol"
                     and meta.get("reconcile_reason") == "terminal-event-missing"
                     and meta.get("terminal_event") == "dispatch.supervisor.error"
                     and not readable_result(meta))))


def answerable_owner_end(status, meta) -> str:
    """How an ended owner's result waits for a person, or "".

    `BLOCKED`: it waits for an answer no declared gate carries. `FAIL`: its own final
    envelope reported a FAIL a person may answer with an approved fix (`readable_result`).
    Either answer is kept (`dispatch_owner_input`) and a replacement owner continues the
    same route with it first (`dispatch_replacement` 'corrected'). A death, an unverifiable
    end keeps its own classification, and a readable FAIL still gets
    no automatic retry (`retry_predecessor`): only a person's kept answer continues it."""
    if status != "done":
        return ""
    if owner_result_format_only_end(status, meta):
        return "BLOCKED"
    if meta.get("note") == "dead-worker-blocked":
        return "BLOCKED"
    return "FAIL" if readable_result(meta) == "FAIL" else ""


def owner_result_format_only_end(status, meta) -> bool:
    """Whether a normally exited owner ended only with a supervisor handoff-format error.

    This is deliberately metadata-only so standalone installers can preserve the same
    answer-waiting route contract without importing runtime log readers.
    """
    return (status == "done" and meta.get("worker_type") == "owner"
            and meta.get("note") == "dead-contract"
            and meta.get("failure_class") == "contract"
            and meta.get("classifier_source") == "supervisor-terminal-v1"
            and meta.get("process_exit") == "0"
            and meta.get("reconcile_reason") in {"final-handoff-invalid", "pass-blocker-not-none"}
            and meta.get("terminal_event") in {
                "claude-result", "opencode-result", "turn.completed", "step_finish.stop"})


def fix_answers(route, lines, jobs=None):
    choice = decide_continuation(route, action="fix", jobs=jobs, facts={"lines": lines})
    return list(choice.answers), list(choice.fallback_nodes)


# Compatibility names; the round decision above is shared by every adapter.
classify_round_row = _ROUND.classify_round_row
round_budget = _ROUND.round_budget
is_round_capped_node = _ROUND.is_round_capped_node
last_verdict_blocking = _ROUND.last_verdict_blocking
gate_unmet = _ROUND.gate_unmet


# The worker's final three lines. Every terminal reader parses them with this
# one pattern, so no surface reads a child as finished while another calls it
# malformed.
#
# G1: a fenced code block and/or up to two plain sentences may follow the block
# (the fence attaches to the envelope; closing sentences follow the fence).
# The verdict still comes from the block alone.
#
# A tail line is ambiguous when it mentions a verdict at all -- `verdict`/
# `평결` anywhere (any case), or a bare uppercase PASS/FAIL/BLOCKED -- so
# those fail closed, as do lines shaped like envelope fields; `Done.` and
# `All tests pass.` stay ordinary sentences. The same check guards the
# fence body, so an earlier decoy can never swallow a later envelope
# (PR #281 review rounds 1-3). Tail elements start at newlines only
# (`_TAIL_GAP`; the fence's own indentation is part of the fence element),
# which keeps the failing search linear.
_TAIL_EDGE = (
    r"(?![ \t#*\-]*(?i:artifact|verdict|blocker|평결)[ \t]*:)"
    r"(?![^\n]*(?i:verdict|평결))"
    r"(?![^\n]*\b(?:PASS|FAIL|BLOCKED)\b)"
)
_TAIL_GAP = r"\n+"
_TAIL_BLANK = r"(?:\n[ \t]*)*"
_TAIL_LINE = _TAIL_EDGE + r"[^\n]+"
_TAIL_FENCE_BODY = r"(?:" + _TAIL_EDGE + r"[^\n]*\n)*?"
_TAIL_FENCE = r"[ \t]*```[^\n]*\n" + _TAIL_FENCE_BODY + r"[ \t]*```"
HANDOFF_RE = re.compile(
    r"(?:\A|\n)artifact: (?P<artifact>[^\n]+)\n"
    r"verdict: (?P<verdict>PASS|FAIL|BLOCKED)\n"
    r"blocker: (?P<blocker>[^\n]+)"
    + r"(?:" + _TAIL_GAP
    + r"(?:" + _TAIL_FENCE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r")?)?"
    + r"|" + _TAIL_LINE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r")?"
    + r")"
    + r")?"
    + _TAIL_BLANK
    + r"\Z"
)


_PASS_NOTE_RE = re.compile(r"none\s*\((?P<note>.+)\)\s*")


def pass_blocker_note(blocker) -> str | None:
    """What a PASS envelope's `blocker: none (...)` adds: "" for a bare `none`, the
    parenthesized note for `none (...)`, None for any other blocker text. The note
    is kept as a remark; it never changes the verdict (RA-8)."""
    if blocker == "none":
        return ""
    match = _PASS_NOTE_RE.fullmatch(blocker) if isinstance(blocker, str) else None
    return match.group("note").strip() if match else None


def pass_blocker_violation(verdict, blocker) -> str | None:
    """A PASS envelope's blocker is `none`, optionally with a note in parentheses;
    any other blocker text breaks the contract."""
    if verdict == "PASS" and pass_blocker_note(blocker) is None:
        return "pass-blocker-not-none"
    return None


# A worker that died at a usage limit, an auth failure or a provider's capacity
# prints one terse line at the end of its log. This one ordered table classifies it
# for every harness, at launch (the wrapper's early watch) and later (liveness);
# the first match wins and the label becomes the `dead-<label>` note.
DEATH_PATTERNS = (
    ("capacity", r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b"),
    ("network-operation-not-permitted", r"operation not permitted|network is unreachable|network access denied"),
    ("session-limit", r"hit your (?:session|usage) limit|session limit reached"),
    ("usage-limit", r"usage[_ ]limit[_ ]reached|usage limit reached|weekly limit|"
     r"rate limit(?:ed)?|provider rate limit|exceeded retry limit|\b429\b"),
    ("auth", r"invalid api key|authentication_error|not logged in|please run /login|unauthorized|\b401\b"),
    ("credit", r"credit balance is too low|insufficient (?:credit|quota|funds)"),
    ("permission-reject", r"permission requested:.*auto-rejecting"),
)
_RESET_RE = re.compile(
    r"resets?(?:\s+at)?\s+([0-9]{1,2}:[0-9]{2}\s*(?:am|pm)?|[0-9]{1,2}\s*(?:am|pm))",
    re.I,
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CAPACITY_TERMINAL_RE = re.compile(
    r"(?:error\s*[:\-]\s*)?(?:selected\s+)?model(?:\s+[A-Za-z0-9._:/-]+)?\s+"
    r"(?:is\s+)?at\s+capacity[.!]?",
    re.I,
)


def scan_death(text: str) -> tuple[str, str] | None:
    """``(label, reset)`` when the text shows a limit/auth death, else None.

    ``reset`` is a best-effort human string ('3pm', '15:45', ...) or '' when absent.
    """
    low = text.lower()
    label = next((name for name, pattern in DEATH_PATTERNS if re.search(pattern, low)), "")
    if not label:
        return None
    match = _RESET_RE.search(text)
    return label, (re.sub(r"\s+", "", match.group(1)) if match else "")


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


def scan_anchored_death(text: str) -> tuple[str, str] | None:
    """Inspect only terse terminal CLI lines, never completion-report prose: the last
    three non-empty lines, each at most 200 characters once colors are removed."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][-3:]:
        if len(ANSI_RE.sub("", line)) > 200:
            continue
        death = scan_death(line)
        if death:
            if death[0] == "capacity" and not anchored_capacity_failure(line):
                continue
            return death
    return None


# ---------------------------------------------------------------------------
# 4. What it may access
# ---------------------------------------------------------------------------

def bind_access_request(*args, **kwargs):
    """Resolve, validate, constrain and grade an explicit execution access request."""
    from execution_access import bind_request
    return bind_request(*args, **kwargs)


def access_grant(*args, **kwargs):
    """The effective explicit grant for one runtime; never creates runtime argv."""
    from execution_access import build_grant
    return build_grant(*args, **kwargs)


def contract_read_roots(agent_home: str | Path, route: dict | None = None) -> tuple[Path, ...]:
    """Contract directories for an already validated launch binding.

    Use the same sealed launch-home field the installer's in-use judgment
    retains. A moving current pointer never replaces the route's contract
    source. These are read addresses, not additional task write grants.
    """
    from execution_access import harness_source_read_roots
    homes = [Path(agent_home)]
    sealed = (route or {}).get("launch_compatibility_tuple") or {}
    identity = sealed.get("launch_home") if isinstance(sealed, dict) else None
    value = identity.get("path") if isinstance(identity, dict) else None
    if isinstance(value, str) and os.path.isabs(value):
        homes.append(Path(value))
    roots = []
    for home in homes:
        roots.extend(harness_source_read_roots(home))
    return tuple(dict.fromkeys(roots))


def bind_launch_access(args, *, runtime: str, default_roots, network_available: bool = False,
                       parent_network: bool = False, effective_sandbox: str = "workspace-write",
                       gpu_resource_scope: bool = False, inherit_parent_sandbox: bool = False):
    """One launch's execution access, for every adapter: the request, the exact live
    parent's effective grant for a dispatch-depth-2 child, and the graded grant (None
    without a request). The adapter passes only what its own launch realizes."""
    import subprocess
    from dispatch_contract import (
        DispatchContractError, dispatch_state_root, parent_lookup_worktree,
        resolve_live_parent_attempt,
    )
    from execution_access import (AccessContext, ExecutionAccessError, bind_request,
                                  load_parent_effective_grant, request_path)
    context = AccessContext.build(worktree=args.worktree, artifact_root=args.artifact_root,
                                  dispatch_state_root=dispatch_state_root(args.jobs_path),
                                  agent_home=args.agent_home, environ=os.environ)
    parent = None
    compute_scope = None
    route_file = getattr(args, "route_file", None)
    owner_binding = getattr(args, "owner_route_binding", None)
    if not route_file and owner_binding is not None:
        route_file = owner_binding.route_file
    # Both owner and node bindings have passed the wrappers' existing route
    # validation before access binding; contract reads do not need a request.
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8")) if route_file else None
    except (OSError, ValueError) as exc:
        raise ExecutionAccessError("route-record-unreadable", str(exc)) from exc
    args.contract_read_roots = contract_read_roots(args.agent_home, route)
    if route_file and request_path(args.execution_access_file, os.environ) is not None:
        from execution_access import compute_execution_scope
        compute_scope = compute_execution_scope(
            route, node=getattr(args, "route_node", None), owner=args.dispatch_depth == 1)
    if args.dispatch_depth >= 2 and request_path(args.execution_access_file, os.environ) is not None:
        if args.parent_binding is None:
            # Prospective probes without an access request need no live owner.
            # A requested grant, including in dry-run, uses start's exact lookup.
            try:
                from dispatch_contract import work_root_identity
                repo = work_root_identity(args.worktree, route=route)
                args.parent_binding = resolve_live_parent_attempt(
                    args.jobs_path, parent_slug=getattr(args, "parent_slug", None) or "",
                    repo=repo,
                    worktree=parent_lookup_worktree(
                        args.worktree, getattr(args, "route_file", None),
                        subsession=bool(getattr(args, "subsession_id", None)),
                        parent_attempt_id=getattr(args, "parent_attempt_id", None)),
                    expected_attempt_id=getattr(args, "parent_attempt_id", None),
                    expected_harness=getattr(args, "parent_harness", None),
                    expected_transport=getattr(args, "parent_transport", None),
                    expected_sandbox=getattr(args, "parent_sandbox", None),
                )
                args.parent_attempt_id = args.parent_binding.attempt_id
            except (DispatchContractError, subprocess.CalledProcessError, OSError) as exc:
                raise ExecutionAccessError(
                    "execution-access-exceeds-parent:parent-grant-unknown",
                    getattr(exc, "detail", str(exc)),
                ) from exc
        parent = load_parent_effective_grant(jobs=args.jobs_path, parent_attempt_id=args.parent_binding.attempt_id,
                                             context=context)
    return bind_request(
        args.execution_access_file, environ=os.environ, context=context,
        is_child=args.dispatch_depth >= 2, parent=parent, runtime=runtime,
        default_writable_roots=default_roots,
        network_available=network_available or bool(parent_network and parent is not None
                                                     and parent.network_allowed),
        effective_sandbox=effective_sandbox, gpu_resource_scope=gpu_resource_scope,
        inherit_parent_sandbox=inherit_parent_sandbox, compute_execution_scope=compute_scope)


# ---------------------------------------------------------------------------
# Launch admission shared by the three adapter wrappers
# ---------------------------------------------------------------------------

def prelaunch_registry(args) -> Path:
    """The registry the route's completion gate reads before the authoritative one
    is resolved: explicit, else inherited, else the agent home's first state root.
    The launch revalidates the authoritative registry immediately before its claim."""
    explicit_or_inherited = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS", "")
    if explicit_or_inherited:
        return Path(explicit_or_inherited)
    from dispatch_contract import dispatch_state_roots
    return dispatch_state_roots(args.agent_home)[0] / "jobs.log"


def completion_gate_fail_fields(error, route_file, route_node) -> dict:
    """SD-154/B-2: a route-state refusal (13.59.3 rule 6) carries a supported
    `next_action` so a caller stops instead of misreading it as a transient
    runtime-unavailable and descending to inline."""
    from dispatch_contract import ROUTE_STATE_REFUSAL_REASONS, route_state_next_action
    fields = {"detail": error.detail, "child_spawned": "0"}
    if error.reason in ROUTE_STATE_REFUSAL_REASONS:
        fields["next_action"] = error.next_action or route_state_next_action(
            error.reason, error.detail, str(route_file), route_node,
        )
    return fields


def _admit_supervised_owner_resource_start(args, agent_home, jobs):
    """Use the normal child start as the owner-controlled resource completion edge."""
    route_file = getattr(args, "route_file", None)
    route_node = getattr(args, "route_node", None)
    if not route_file or not route_node:
        return False
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
        target = next((row for row in route.get("nodes", [])
                       if isinstance(row, dict) and row.get("id") == route_node), None)
    except (OSError, ValueError, TypeError):
        return False
    if target is None:
        return False
    supervised_resources = {
        row.get("id") for row in route.get("nodes", [])
        if isinstance(row, dict) and row.get("kind") == "resource-runner"
        and (row.get("continuation") or {}).get("kind") == "supervised"
    }
    # Ordinary stages never open the workflow ledger here: only a start that
    # follows a supervised resource node can be its owner-controlled edge.
    if not supervised_resources.intersection(target.get("depends_on") or []):
        return False
    import dispatch_resource_wait as OWNER_RESOURCE
    supervisor = OWNER_RESOURCE.supervisor()
    ledger = supervisor.ledger_for(route, str(jobs))
    armed_rows = supervisor.read_armed(ledger)
    candidates = []
    for predecessor in target.get("depends_on") or []:
        node = next((row for row in route.get("nodes", [])
                    if isinstance(row, dict) and row.get("id") == predecessor), None)
        armed = armed_rows.get(predecessor)
        if (node is None or node.get("kind") != "resource-runner"
                or (node.get("continuation") or {}).get("kind") != "supervised"
                or not armed or armed.get("predecessor_kind") != "resource"
                or route_node not in (armed.get("successors") or [])):
            continue
        try:
            registry = json.loads(Path(armed["resource_registry"]).read_text(encoding="utf-8"))
            row = registry["runs"][armed["predecessor_id"]]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if row.get("resource_policy") == "supervised-owner":
            candidates.append((predecessor, armed))
    if not candidates:
        return False
    if getattr(args, "dispatch_depth", None) != 2:
        raise _contract_error("resource-owner-start-requires-live-parent", str(route_node))
    from dispatch_contract import (
        DispatchContractError, parent_lookup_worktree, resolve_live_parent_attempt,
        work_root_identity,
    )
    import dispatch_owner_input as INPUT
    import subprocess
    try:
        repo = work_root_identity(args.worktree, route=route)
        owner = resolve_live_parent_attempt(
            Path(jobs),
            parent_slug=getattr(args, "parent_slug", None) or "",
            repo=repo,
            worktree=parent_lookup_worktree(
                args.worktree, route_file,
                subsession=bool(getattr(args, "subsession_id", None)),
                parent_attempt_id=getattr(args, "parent_attempt_id", None),
            ),
            expected_attempt_id=getattr(args, "parent_attempt_id", None),
            expected_harness=getattr(args, "parent_harness", None),
            expected_transport=getattr(args, "parent_transport", None),
            expected_sandbox=getattr(args, "parent_sandbox", None),
        )
        owner_input = INPUT.inspect(jobs, owner.attempt_id)
        if (owner_input.get("supervisor_live") is not True
                or not owner_input.get("thread_id")
                or owner_input.get("thread_id") == INPUT.PLACEHOLDER_THREAD):
            raise DispatchContractError("resource-owner-session-unavailable", owner.attempt_id)
        try:
            supervisor.admit_owner_successor_start(
                route, route_node, jobs, owner.attempt_id, owner_input["thread_id"],
            )
        except supervisor.SupervisorError as error:
            raise DispatchContractError("resource-owner-stage-admission-blocked", str(error)) from error
    except (DispatchContractError, INPUT.InputError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, DispatchContractError):
            raise
        raise _contract_error("resource-owner-start-authority-unavailable", str(error)) from error
    return True


def completion_gate(args, action: str, agent_home, jobs, *, gate, before=()):
    """The route's completion and preview gate for one launch, the same in every wrapper.

    `gate` is the wrapper's `completion_marker_gate`; `before` are checks that share its
    refusal handling. A refusal returns `(reason, exit code, receipt fields)` with the
    preview-gate recovery detail attached; None admits the launch."""
    from dispatch_contract import (DispatchContractError, PRELAUNCH_PROCESS_BLOCK_REASONS,
                                   recover_preview_gate_after_refusal)
    from review_input import preview_request_nodes
    try:
        for check in before:
            check()
        if action == "start":
            _admit_supervised_owner_resource_start(args, agent_home, jobs)
        gate(args.route_file, args.route_node, action, agent_home, jobs, attempt_id=args.attempt_id,
             planned_revision_nodes=preview_request_nodes(args, jobs))
    except DispatchContractError as error:
        error.detail = recover_preview_gate_after_refusal(
            args.route_file, args.route_node, action, agent_home, jobs, error)
        return (error.reason, 78 if error.reason in PRELAUNCH_PROCESS_BLOCK_REASONS else 65,
                completion_gate_fail_fields(error, args.route_file, args.route_node))
    return None


class RelocationDecision(NamedTuple):
    allowed: bool
    reason: str | None
    route_ids: tuple[str, ...]


def relocation_admission(root, records, directories, *, selected_route_ids=(), evidence=None) -> RelocationDecision:
    """Read-only no-live history decision; never grants continuation ownership.

    Unknown evidence protects the source. Target unrelated work is outside this scope.
    Uses autoclose's existing process, attempt, lease and resource observations.
    """
    import route_autoclose as ac
    import artifact_producer as producer
    route_ids = set(selected_route_ids)
    def ids(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"route_id", "owner_route_id", "batch_route_id", "parent_route_id"} and isinstance(item, str):
                    route_ids.add(item)
                else:
                    ids(item)
        elif isinstance(value, list):
            for item in value:
                ids(item)
    for record in records:
        ids(record)
    try:
        observed = evidence or ac._Evidence(Path(root), producer)
        # Check payload holders even when a cycle has no route metadata.
        for directory in directories:
            if any(ac._under(item, directory) for item in observed.resource_paths):
                return RelocationDecision(False, "resource-run", tuple(sorted(route_ids)))
            if any(ac._under(item, directory) for item in observed.open_paths):
                return RelocationDecision(False, "cycle-in-use", tuple(sorted(route_ids)))
        pending = list(route_ids)
        checked = set()
        while pending:
            route_id = pending.pop()
            if route_id in checked:
                continue
            checked.add(route_id)
            path = Path(root) / ".runtime/routes" / (route_id + ".json")
            route = json.loads(path.read_bytes()) if path.exists() else None
            ids(route)
            pending.extend(route_ids - checked)
            if hasattr(observed, "owners"):
                # Closure settlement and intact history relocation have different
                # authority. A missing old absolute completion path is not live
                # work. Keep positive pending settlement and every other guard.
                import copy
                from dispatch_terminal_commit import owner_completion_state
                relocation = copy.copy(observed)
                owners = []
                for row in observed.owners.get(route_id, ()):
                    state = owner_completion_state(Path(row["jobs"]), row["status"], row["metadata"])
                    if state.state == "unknown" and state.reason == "FileNotFoundError":
                        continue
                    owners.append(row)
                relocation.owners = {route_id: owners}
                reason = relocation.kept(route_id, lambda: None, route)
            else:
                reason = observed.kept(route_id, lambda: None, route)
            if reason:
                return RelocationDecision(False, reason, tuple(sorted(route_ids)))
    except Exception as exc:
        return RelocationDecision(False, "evidence-unreadable:" + str(exc), tuple(sorted(route_ids)))
    return RelocationDecision(True, None, tuple(sorted(route_ids)))


def require_batch_reservation(
    payload: dict[str, object], expected: dict[str, object] | None
) -> None:
    """The current launch grant; a historical manifest is read separately.

    Called at registration and consumption of the existing atomic reservation.
    Canonical digest checks compare the stored manifest to its stored digest.
    """
    import hashlib
    from replica_batch_contract import DIGEST, ReplicaBatchContractError, verify_manifest
    if expected is None:
        if payload.get("reservation_kind") in {"replica-batch", "parallel-batch"}:
            raise _contract_error(
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
            # A partial reservation launches only its selected gap. The other
            # members are digest-bound historical peers already checked by the
            # source reader and peer census, not new current launch candidates.
            if (payload.get("batch_admission_count") == 1
                    and manifest_member.get("attempt_id") != public_expected.get("batch_attempt_id")):
                continue
            member_node = str(manifest_member.get("route_node", ""))
            profile_fields = expected.get("_batch_profile_selections", {}).get(member_node)
            if profile_fields is not None:
                for key, value in profile_fields.items():
                    if manifest_member.get(key) != value:
                        mismatches[f"manifest.member.{member_node}.{key}"] = (value, manifest_member.get(key))
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
            if int(verified.get("schema_version", 1)) >= 2:
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
        raise _contract_error("parallel-group-reservation-mismatch", detail)
