#!/usr/bin/env python3
"""Compatibility hook: enforce an explicit terminal cleanup scope only.

Batch delivery and waiting belong to the supervisor, not a tool allowlist.
Ordinary launch/write gates continue to enforce their own authorization.
It also shows queued owner corrections early in an ordinary turn (OPERATIONS §5.10):
run as `prompt` from UserPromptSubmit it only binds that turn's prompt_id.
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


def turn_record(registry: Path, attempt: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The supervisor's ordinary-turn record, read without the lock: only a cheap pre-check."""
    value = json.loads(supervisor_lease_path(registry, attempt).with_suffix(".input.json").read_text())
    return value, mapping(value.get("turn"))


def eligible_prompt(prompt: str) -> bool:
    # Claude's notifications have envelopes, but a scheduled or inbox turn can reuse plain text.
    return (not prompt.lstrip().startswith(("<", "[TASK NOTIFICATION - NOT USER INPUT]",
                                           "Continue from where you left off."))
            and os.environ.get("CLAUDE_CODE_DISABLE_CRON") == "1"
            and not any(os.environ.get(key) for key in (
                "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_RESUME_INTERRUPTED_TURN",
                "CLAUDE_CODE_RESUME_PROMPT", "CLAUDE_CODE_EXPERIMENTAL_CODE_MODE")))


def bind_prompt() -> int:
    """UserPromptSubmit: stdout stays empty (Claude adds its plain text to the model context)."""
    try:
        payload = json.load(sys.stdin)
        attempt = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
        if (os.environ.get("AGENT_DISPATCH_COMPLETION_MODE") == "supervised" and attempt
                and not payload.get("agent_id") and payload.get("prompt_id")
                and eligible_prompt(payload["prompt"])):
            registry = jobs_path()
            _, turn = turn_record(registry, attempt)
            if turn.get("digest") and not turn.get("prompt_id"):
                import dispatch_owner_input
                dispatch_owner_input.bind_prompt(registry, attempt, payload["prompt"], str(payload["prompt_id"]), turn)
    except Exception:
        pass
    return 0


def main() -> int:
    if sys.argv[1:] == ["prompt"]:
        return bind_prompt()
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
    """Copy queued owner corrections beside this tool's result only when its prompt_id is the
    one bound to the supervisor's ordinary turn; a subagent's hook carries its parent's."""
    if payload.get("agent_id") or not payload.get("tool_use_id") or not payload.get("prompt_id"):
        return 0
    try:
        value, turn = turn_record(registry, attempt)
        if turn.get("prompt_id") != payload["prompt_id"] or not any(
                item.get("state") == "queued" and not item.get("shown") for item in value["requests"]):
            return 0
        import dispatch_owner_input
        text = dispatch_owner_input.show_in_turn(registry, attempt, str(payload["tool_use_id"]),
                                                 str(payload["prompt_id"]))
    except Exception:
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": text}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
