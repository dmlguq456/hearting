#!/usr/bin/env python3
"""Claude PreToolUse enforcement for supervised registered-headless owners."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    classify_supervised_shell_command,
    classify_supervised_shell_command_reason,
    current_children,
    pending_attempt_ids,
    read_supervisor_phase_state,
    required_action_for_attempt,
    supervisor_guarded_attempt_ids,
    supervisor_outbox_row_state,
)


def deny(reason: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            separators=(",", ":"),
        )
    )
    return 0


def jobs_path() -> Path:
    override = os.environ.get("AGENT_DISPATCH_JOBS")
    if override:
        return Path(override)
    agent_home = os.environ.get("AGENT_HOME")
    if agent_home and (Path(agent_home) / "core" / "CORE.md").is_file():
        return Path(agent_home) / ".dispatch" / "jobs.log"
    return ROOT / ".dispatch" / "jobs.log"


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def main() -> int:
    if os.environ.get("AGENT_DISPATCH_COMPLETION_MODE") != "supervised":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return deny("runtime-supervised-parent: native hook payload is invalid")
    if not isinstance(payload, dict):
        return deny("runtime-supervised-parent: native hook payload is invalid")

    parent_attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not parent_attempt:
        return deny("runtime-supervised-parent: exact parent attempt is missing")
    registry = jobs_path()
    if not registry.is_file():
        return deny("runtime-supervised-parent: canonical child registry is unavailable")
    try:
        rows = current_children(registry, parent_attempt)
    except JoinContractError:
        return deny("runtime-supervised-parent: exact child registry contract is invalid")
    # A terminal+quiescent process can still have an open registry row that the
    # delivered receipt requires the parent to harvest.  Process liveness alone
    # therefore cannot release the guard at the exact closure boundary.
    open_attempts = {
        row.attempt_id for row in rows if row.status in {"open", "running"}
    }
    open_attempts.update(pending_attempt_ids(rows))
    raw_state = os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE", "")
    state = read_supervisor_phase_state(
        Path(raw_state) if raw_state else None,
        parent_attempt,
    )
    outbox_attempts = (
        set(state.outbox.attempt_ids).difference(
            state.outbox.consumed_attempt_ids
        )
        if state is not None and state.outbox is not None
        else set()
    )
    # One derivation shared with the supervisors' D2a precheck: a precheck that
    # computed this set differently would report a command satisfiable that this
    # guard then denies (plan SS3.4 D2a, fixture D-6c).
    guarded_attempts = supervisor_guarded_attempt_ids(rows, state.outbox if state else None)
    if not guarded_attempts:
        return 0
    tool_name = payload.get("tool_name")
    tool_args = mapping(payload.get("tool_input"))
    command = tool_args.get("command")
    action = None
    if tool_name == "Bash" and isinstance(command, str):
        action = classify_supervised_shell_command(
            base=Path(str(payload.get("cwd") or os.getcwd())),
            command=command,
            open_attempt_ids=guarded_attempts,
            parent_slug=os.environ.get("AGENT_DISPATCH_SELF_SLUG", ""),
            jobs=registry,
            parent_attempt_id=parent_attempt,
            route_file=(
                Path(os.environ["AGENT_ROUTE_FILE"])
                if os.environ.get("AGENT_ROUTE_FILE")
                else None
            ),
            route_id=os.environ.get("AGENT_ROUTE_ID", ""),
        )

    indexed = {row.attempt_id: row for row in rows}
    delivered_open = open_attempts.intersection(
        set(state.delivered_attempt_ids) if state is not None else set()
    )
    row_state = supervisor_outbox_row_state(state, rows) if state is not None else "absent"
    current_actions = {
        attempt: required_action_for_attempt(
            indexed[attempt].status, indexed[attempt].metadata
        )
        for attempt in outbox_attempts
        if attempt in indexed
    }
    actionable = {
        attempt: required
        for attempt, required in current_actions.items()
        if required != "advance-completed"
    }
    if state is None:
        allowed = bool(
            action
            and action.kind == "harvest"
            and action.attempt_id in open_attempts
            and action.status in {"open", "all"}
        )
    elif state.phase in {"deliverable", "recovery"} and state.outbox is not None:
        if not actionable:
            allowed = True
        else:
            required = actionable.get(action.attempt_id) if action else None
            allowed = bool(
                action
                and action.kind == "harvest"
                and required
                and (
                    (required == "complete-open" and action.status in {"open", "all"} and action.mark_done)
                    or (
                        required == "inspect-done-failure"
                        and action.status in {"done", "all"}
                        and action.failure_detail
                    )
                )
            )
    elif delivered_open:
        allowed = bool(
            action
            and action.kind == "harvest"
            and action.attempt_id in delivered_open
            and action.status in {"open", "all"}
        )
    else:
        allowed = bool(action and action.kind in {"dispatch", "dispatch-batch"})
    if allowed:
        return 0

    # D4: name the ACTUAL registry status per attempt, not "open" for every
    # one -- a done row with a typed dead-worker note was previously
    # misreported as open (`open_attempts` unioned everything, and the text
    # then lied about it).
    statuses = ",".join(
        f"{attempt}:{indexed[attempt].status if attempt in indexed else 'unknown'}"
        for attempt in sorted(guarded_attempts)
    )
    if state is None:
        admitted_shape = (
            "another exact parent-bound dispatch-node start or checked "
            "dispatch-batch start (no batch delivered yet)"
        )
    elif state.phase in {"deliverable", "recovery"} and state.outbox is not None:
        if actionable:
            admitted_shape = "exact preflight harvest for " + ",".join(
                f"{attempt}:{required}" for attempt, required in sorted(actionable.items())
            )
        else:
            admitted_shape = "advance the route (no attempt requires harvest)"
    elif delivered_open:
        admitted_shape = (
            "exact preflight harvest --status open --mark-done for "
            + ",".join(sorted(delivered_open))
        )
    else:
        admitted_shape = "another exact parent-bound dispatch-node start or checked dispatch-batch start"
    reason_suffix = ""
    if tool_name == "Bash" and isinstance(command, str) and action is None:
        rejection_reason = classify_supervised_shell_command_reason(
            base=Path(str(payload.get("cwd") or os.getcwd())),
            command=command,
            open_attempt_ids=guarded_attempts,
            parent_slug=os.environ.get("AGENT_DISPATCH_SELF_SLUG", ""),
            jobs=registry,
            parent_attempt_id=parent_attempt,
            route_file=(
                Path(os.environ["AGENT_ROUTE_FILE"])
                if os.environ.get("AGENT_ROUTE_FILE")
                else None
            ),
            route_id=os.environ.get("AGENT_ROUTE_ID", ""),
        )
        if rejection_reason:
            reason_suffix = f"; command_rejection_reason={rejection_reason}"
    return deny(
        "runtime-supervised-parent: guarded registered child attempt(s) "
        f"{statuses}; the current phase admits only {admitted_shape}; "
        f"outbox_row_state={row_state}{reason_suffix}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
