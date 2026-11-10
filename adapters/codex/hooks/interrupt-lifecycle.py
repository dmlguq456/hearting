#!/usr/bin/env python3
"""Codex Interrupt bridge: an interrupted turn still gets its change record.

Codex runs no Stop hook for a turn the user interrupts; its Interrupt event
(https://learn.chatgpt.com/docs/hooks#interrupt) carries the same session id,
cwd and turn id. This starts the same detached end step Stop starts and clears
the session's Fleet wait, since the interrupted turn no longer waits on anyone.
Silent: Interrupt output cannot steer the turn, and plain text is invalid.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in ("utilities", "tools"):
    path = str(ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)


def _worker():
    return (
        os.environ.get("AGENT_SESSION_ROLE", "").lower() == "worker"
        or os.environ.get("AGENT_DISPATCH_CHILD") == "1"
        or bool(os.environ.get("AGENT_DISPATCH_DEPTH"))
        or os.environ.get("FLEET_TITLE_REFRESH") == "1"
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        import turn_change_record

        turn_change_record.launch("end", "codex", dict(payload, interrupted=True))
    except Exception:  # noqa: BLE001 -- a turn never depends on its record
        pass
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id and not _worker():
        try:
            from fleet import interaction

            interaction.clear_wait(session_id, "codex")
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
