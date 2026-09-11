"""Portable decisions over a committed attempt and its execution evidence.

This module owns no processes or storage. Callers provide the exact registry
snapshot and the shared process proof; runtime labels never affect the policy.
The semantic result is kept separate from the action still owed. In particular,
success with incomplete cleanup remains success with a cleanup obligation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

OPEN_STATES = frozenset({"open", "running"})
TERMINAL_STATES = frozenset({"done", "killed", "cancelled"})
SUBSESSION_NOTE = "completed-subsession"
REVIEW_BLOCKING_NOTE = "completed-review-blocking"
SUCCESS_NOTES = frozenset({"completed-marker", "completed-supervisor", SUBSESSION_NOTE})


def committed_outcome(status: str, metadata: Mapping[str, str]) -> str:
    """Interpret only a committed terminal row, never cached watchdog output."""
    if status in OPEN_STATES:
        return "pending"
    if status not in TERMINAL_STATES:
        return "unknown"
    note = metadata.get("note", "")
    if note == REVIEW_BLOCKING_NOTE:
        return "review-blocked"
    if note in SUCCESS_NOTES or metadata.get("failure_class") == "pass":
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


def decide_attempt(
    status: str, metadata: Mapping[str, str], *, process_state: str,
    process_reason: str = "", terminal_observed: bool = False,
) -> AttemptDecision:
    """Select the next obligation without changing a committed outcome.

    A quiescent process with an open row needs the terminal writer. A process
    that cannot be observed needs recovery or parent intervention, never a
    fabricated terminal result. Only a closed failure plus quiescence may
    authorize fallback; a completed review with findings remains a review.
    """
    outcome = committed_outcome(status, metadata)
    if status not in OPEN_STATES | TERMINAL_STATES:
        return AttemptDecision(outcome, "recover", "supervision-controller", "registry-status-invalid")
    if process_state == "live":
        return AttemptDecision(outcome, "wait", "execution-boundary", process_reason or "process-alive")
    if process_state != "quiescent":
        return AttemptDecision(outcome, "recover", "supervision-controller", process_reason or "process-unverifiable")
    if status in OPEN_STATES:
        return AttemptDecision(outcome, "reconcile", "terminal-writer",
                               "terminal-observed" if terminal_observed else "process-exited")
    if outcome == "succeeded":
        return AttemptDecision(outcome, "advance", "completion-controller", "registry-closed")
    if outcome == "review-blocked":
        return AttemptDecision(outcome, "review", "workflow-owner", REVIEW_BLOCKING_NOTE)
    if outcome == "failed":
        note = metadata.get("note", "")
        # Cancellation is an explicit disposition, not automatic permission
        # to resurrect the cancelled work. Its recovery claim has a separate
        # user/route-authorized admission boundary.
        retry = "capacity" if note == "dead-capacity" else "fallback" if note.startswith("dead-") else ""
        return AttemptDecision(outcome, "inspect-failure", "workflow-owner", note or status, retry)
    return AttemptDecision(outcome, "inspect-failure", "workflow-owner", "terminal-outcome-unclassified")


def required_action(status: str, metadata: Mapping[str, str]) -> str:
    """The terminal writer/harvest instruction, before transport formatting."""
    if status in OPEN_STATES:
        return "complete-open"
    if committed_outcome(status, metadata) == "succeeded":
        return "advance-completed"
    return "inspect-done-failure"
