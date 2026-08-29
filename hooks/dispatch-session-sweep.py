#!/usr/bin/env python3
"""SD-111 P4 -- Claude carrier 2: durable-session-activation sweep.

Registered on ``SessionStart`` and ``UserPromptSubmit`` only (never ``Stop``,
§13.33.1-(5) / the 2026-07-10 measurement, CC #38651).

2026-08-29 decision (supersedes the A-21 "surface nothing" slice for Claude,
PRD correction pending): this carrier now DELIVERS. Claude is
measured-unsupported for a session-generation proof, so the claim is made
without one and the accepted trade is bounded at-least-once re-delivery over
never-delivered. For every record addressed to this session that is pending
or lease-expired, one bounded receipt line is injected as
``additionalContext`` and the record is acked -- the injection is synchronous
with this session's next inference, which is the consumption token the
async rewake carrier can never prove. The Claude Code `asyncRewake` hook only
wakes an idle session on exit code 2 and its exit-0 output waits for the next
user interaction, so this sweep is the path that guarantees a completion is
seen at the latest on the user's next prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import dispatch_state_roots, resolve_agent_home  # noqa: E402
from dispatch_session_sweep import _bounded_receipt_text, ack_delivered, sweep_deliver  # noqa: E402

RECIPIENT_KIND = "claude-parent-runtime"
SESSION_GENERATION = "unsupported"


def _session_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _event_name(payload: object) -> str:
    if isinstance(payload, dict):
        value = payload.get("hook_event_name")
        if isinstance(value, str) and value:
            return value
    return "UserPromptSubmit"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError):
        return 0
    session_id = _session_id(payload)
    if session_id is None:
        return 0
    try:
        agent_home = resolve_agent_home()
        roots = dispatch_state_roots(agent_home)
    except Exception:  # noqa: BLE001 -- fail-open, never block the session
        return 0
    seen: set[Path] = set()
    lines: list[str] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            records, _entries = sweep_deliver(root, RECIPIENT_KIND, session_id)
        except Exception:  # noqa: BLE001 -- fail-open (§13.33.1-(3))
            continue
        if not records:
            continue
        for record in records:
            lines.append(_bounded_receipt_text(record))
        try:
            ack_delivered(root, session_id, records, acked_by=f"session-sweep:{session_id}")
        except Exception:  # noqa: BLE001
            pass
    if not lines:
        return 0
    context = (
        "Hearting dispatch completion delivery (SD-111 durable pending records for this "
        "session; a record may repeat one earlier async notice -- if that attempt was already "
        "harvested, ignore it). Harvest each attempt with the exact checked surface "
        "(`dispatch-attempt-ready.py --attempt <id>` or `preflight.sh harvest`), then continue "
        "the owning route. Do not start Monitor, dispatch-wait, or a polling loop.\n"
        + "\n".join(f"- {line}" for line in lines)
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": _event_name(payload),
                    "additionalContext": context,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
