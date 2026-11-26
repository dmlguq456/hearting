#!/usr/bin/env python3
"""Clear an exact Codex interaction marker after a successful tool call."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_STARTED = time.monotonic()
# Peer-context reading and claiming share this budget from hook start (the same
# one the Claude hooks use): rows a slow disk keeps it from reaching stay
# pending for the next tool call or prompt instead of stalling this one.
_PEER_BUDGET_S = 2.5

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _worker():
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def _find(payload):
    if not isinstance(payload, dict):
        return ""
    for key in ("session_id", "sessionID", "thread_id", "threadID"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    session = payload.get("session")
    if isinstance(session, dict) and isinstance(session.get("id"), str):
        return session["id"]
    for key in ("context", "workspace", "payload", "event", "input", "data"):
        value = _find(payload.get(key))
        if value:
            return value
    return ""


def main():
    try:
        payload = json.load(sys.stdin)
        if _worker():
            return 0
        session_id = _find(payload)
        if session_id:
            from fleet import interaction

            resumed = interaction.read_wait(session_id, "codex") is not None
            interaction.clear_wait(session_id, "codex")
            if resumed:
                # The approved tool ran: this session's shell border leaves `waiting`.
                try:
                    utilities = str(ROOT / "utilities")
                    if utilities not in sys.path:
                        sys.path.insert(0, utilities)
                    from session_shell import lifecycle

                    lifecycle("codex", "working", session_id)
                except Exception:
                    pass
            import importlib.util
            spec = importlib.util.spec_from_file_location("_peer_context", ROOT / "utilities/peer-message.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.emit_peer_context({"harness": "codex", "session_id": session_id}, "PostToolUse",
                                  deadline=_STARTED + _PEER_BUDGET_S)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
