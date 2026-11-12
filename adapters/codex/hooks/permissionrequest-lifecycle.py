#!/usr/bin/env python3
"""Publish a privacy-minimal Codex approval wait without owning the approval."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def first_string(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def nested_string(payload: dict[str, Any], *keys: str) -> str:
    direct = first_string(payload, *keys)
    if direct:
        return direct
    for key in ("context", "workspace", "session", "payload", "event", "input", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            found = nested_string(value, *keys)
            if found:
                return found
    return ""


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or is_worker_session():
            return 0
        session_id = nested_string(
            payload, "session_id", "sessionID", "thread_id", "threadID"
        )
        session = payload.get("session")
        if not session_id and isinstance(session, dict):
            session_id = first_string(session, "id")
        if not session_id:
            return 0
        from fleet import interaction

        interaction.set_wait(
            session_id, "codex", "approval", "codex-permissionrequest"
        )
        try:
            # This session's shell border shows the same wait now.
            utilities = str(ROOT / "utilities")
            if utilities not in sys.path:
                sys.path.insert(0, utilities)
            from session_shell import lifecycle

            lifecycle("codex", "blocked", session_id)
        except Exception:
            pass
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
