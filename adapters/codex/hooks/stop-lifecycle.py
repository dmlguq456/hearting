#!/usr/bin/env python3
"""Silent Codex Stop boundary with no completion or lifecycle authority."""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = ROOT / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))


def _worker():
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or bool(os.environ.get("OPENCODE_DISPATCH_SLUG"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def _sid(value):
    if not isinstance(value, dict):
        return ""
    for key in ("session_id", "sessionID", "thread_id", "threadID"):
        item = value.get(key)
        if isinstance(item, str) and item:
            return item
    item = value.get("session")
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    for key in ("context", "workspace", "payload", "event", "input", "data"):
        item = _sid(value.get(key))
        if item:
            return item
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        from session_shell import lifecycle
        lifecycle("codex", "stop", _sid(payload))
    except Exception:
        pass
    try:
        session_id = "" if _worker() else _sid(payload)
        if session_id:
            try:
                import session_tidy_idle
                session_tidy_idle.launch_observer("codex", payload)
            except Exception:
                pass
            from fleet import interaction
            from session_summary_trigger import launch_trigger

            interaction.clear_wait(session_id, "codex")
            launch_trigger("codex", session_id, "final")
    except Exception:
        pass
    try:
        # The automatic change record (utilities/turn_change_record.py) compares this
        # turn's work tree with its start; detached and silent, as for Claude and OpenCode.
        import turn_change_record

        turn_change_record.launch("end", "codex", payload)
    except Exception:
        pass
    try:
        # Observe this session's (or dispatched worker's) cycle: the open cycle's
        # interim manifest, or a closed cycle's changed files. Detached,
        # rate-limited, silent -- the launcher Claude Stop and OpenCode share.
        import artifact_checkpoint_trigger

        artifact_checkpoint_trigger.launch_for_session("codex", _sid(payload))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
