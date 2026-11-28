#!/usr/bin/env python3
"""Compatibility hook: enforce an explicit terminal cleanup scope only.

Batch delivery and waiting belong to the supervisor, not a tool allowlist.
Ordinary launch/write gates continue to enforce their own authorization.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import dispatch_state_roots, supervisor_lease_path  # noqa: E402


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
    agent_home = Path(os.environ.get("AGENT_HOME") or ROOT)
    return dispatch_state_roots(agent_home)[0] / "jobs.log"


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
    try:
        import dispatch_terminal_commit
        cleanup_scope = dispatch_terminal_commit.load_active_cleanup_scope(registry.parent, parent_attempt)
        if cleanup_scope is not None:
            verdict = dispatch_terminal_commit.cleanup_tool_permission(
                cleanup_scope, tool=payload.get("tool_name"), arguments=mapping(payload.get("tool_input")),
                cwd=payload.get("cwd") or os.getcwd(), owner_attempt_id=parent_attempt,
                route_id=os.environ.get("AGENT_ROUTE_ID", ""))
            if verdict.verdict != "allowed":
                return deny(f"cleanup-scope: {verdict.verdict}: {verdict.detail}")
            return 0
    except Exception:
        return deny("cleanup-scope: authority unavailable")
    return show_owner_input(registry, parent_attempt, payload)


def show_owner_input(registry: Path, attempt: str, payload: dict[str, Any]) -> int:
    """Copy queued owner corrections beside this tool's result only in an ordinary turn (OPERATIONS §5.10)."""
    if payload.get("agent_id") or not payload.get("tool_use_id"):
        return 0
    try:
        # The input state itself (replaced whole), not a count: no unshown queued correction ends here.
        value = json.loads(supervisor_lease_path(registry, attempt).with_suffix(".input.json").read_text())
        if not any(item.get("state") == "queued" and not item.get("shown") for item in value["requests"]):
            return 0
        import dispatch_owner_input
        from dispatch_completion_join import read_supervisor_phase_state
        state = read_supervisor_phase_state(Path(os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE", "")), attempt)
        if state is None or not state.ordinary_turn:
            return 0
        text = dispatch_owner_input.show_in_turn(registry, attempt, str(payload["tool_use_id"]))
    except Exception:
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
