#!/usr/bin/env python3
"""Publish privacy-minimal Claude interaction waits for Fleet.

This side-effect hook accepts the Claude hook payload but delegates state
semantics to the runtime-neutral ``fleet.interaction`` codec. It never returns
a Claude decision and never writes stdout.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _worker_session():
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
        or os.environ.get("MEM_DISTILL") == "1"
    )


def _payload():
    try:
        value = json.load(sys.stdin)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _shell_state(event, session_id):
    """Tell this session's shell border the same change (its existing lifecycle report)."""
    try:
        root = Path(os.environ["AGENT_HOME"]) if os.environ.get("AGENT_HOME") else ROOT
        if not (root / "core" / "CORE.md").is_file():
            root = ROOT
        utilities = str(root / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from session_shell import lifecycle
        lifecycle("claude", event, session_id)
    except Exception:
        pass


def main():
    try:
        payload = _payload()
        if _worker_session() or payload.get("agent_id"):
            return 0
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return 0
        from fleet import interaction

        action = sys.argv[1] if len(sys.argv) > 1 else ""
        harness = "claude"
        if "--harness" in sys.argv:
            index = sys.argv.index("--harness")
            if index + 1 < len(sys.argv) and sys.argv[index + 1] in {"claude", "codex", "opencode"}:
                harness = sys.argv[index + 1]
        if action == "clear":
            # OpenCode clears a question or permission by its exact request id; the
            # plugin sends scope=session only for a wait without one (tool finished,
            # turn ended).
            if (harness == "opencode" and payload.get("request_id") is None
                    and payload.get("scope") != "session"):
                return 0
            # A Claude tool that ran after its wait resumes the turn: the border
            # leaves `waiting` now, not at the turn's end.
            resumed = (harness == "claude"
                       and payload.get("hook_event_name") in ("PostToolUse", "PostToolUseFailure")
                       and interaction.read_wait(session_id, harness) is not None)
            interaction.clear_wait(session_id, harness, request_id=payload.get("request_id"))
            if resumed:
                _shell_state("working", session_id)
            return 0
        if action != "set" or "--kind" not in sys.argv:
            return 0
        kind_index = sys.argv.index("--kind")
        if kind_index + 1 >= len(sys.argv):
            return 0
        kind = sys.argv[kind_index + 1]
        if (harness == "claude" and kind == "permission"
                and payload.get("tool_name") == "AskUserQuestion"):
            # Claude also raises PermissionRequest for its question tool; the
            # PreToolUse decision wait already describes it, so keep that.
            return 0
        source = {
            "decision": "claude-asktool",
            "permission": "claude-permission",
        }.get(kind)
        if harness == "opencode":
            source = {"decision": "opencode-question", "permission": "opencode-permission"}.get(kind)
            if kind == "decision" and payload.get("source") == "opencode-form":
                source = "opencode-form"
        if source:
            interaction.set_wait(session_id, harness, kind, source,
                                 request_id=payload.get("request_id"),
                                 turn_id=payload.get("turn_id"), item_id=payload.get("item_id"))
            if harness == "claude":
                _shell_state("blocked", session_id)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
