#!/usr/bin/env python3
"""Codex SessionEnd bridge: Fleet wait cleanup and the final session summary.

Memory does nothing when a session ends; it exchanges after writes and reads (D-82/D-83).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))


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


def load_payload() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def is_worker_session() -> bool:
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def main() -> int:
    # SessionEnd owns the Fleet cleanup and the final summary. Stop is a
    # separate short bridge and never enters this code path.
    if is_worker_session():
        return 0
    payload = load_payload()
    event_session_id = nested_string(
        payload, "session_id", "sessionID", "thread_id", "threadID"
    )
    session = payload.get("session")
    if not event_session_id and isinstance(session, dict):
        event_session_id = first_string(session, "id")
    if event_session_id:
        try:
            tools = ROOT / "tools"
            if str(tools) not in sys.path:
                sys.path.insert(0, str(tools))
            from fleet import interaction
            from session_summary_trigger import launch_trigger

            from fleet.collectors import codex
            from peer_steward_base import peer_message
            # A delayed daemon unload must not close a newly resumed TUI.
            if codex.process_for_thread(event_session_id, fresh=True) is None:
                peer_message.observe_steward_end("codex", event_session_id, reason="thread-unloaded")
            interaction.clear_wait(event_session_id, "codex")
            launch_trigger("codex", event_session_id, "final")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
