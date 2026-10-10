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
``additionalContext``. A real UserPromptSubmit acknowledges after rendering;
SessionStart only observes and reconnects the existing courier, since startup
context does not itself start inference. The Claude Code `asyncRewake` hook only
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
from dispatch_session_sweep import (  # noqa: E402
    activate,
    ack_delivered,
    delivery_context,
    sweep_deliver,
)

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
    batches: list[tuple[Path, list[dict]]] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        if not Path(root).is_dir():
            # A read-only fallback root (legacy <agent-home>/.dispatch) that
            # does not exist holds nothing to sweep; touching it would create it.
            continue
        try:
            if _event_name(payload) == "SessionStart":
                records = activate(root, RECIPIENT_KIND, session_id)
            else:
                records, _entries = sweep_deliver(root, RECIPIENT_KIND, session_id)
        except Exception:  # noqa: BLE001 -- fail-open (§13.33.1-(3))
            continue
        if records:
            batches.append((root, records))
    # Gate notices reach the session here too (SD-123 (8)(b) carrier 2): whether the
    # `asyncRewake` hook process survives an interrupt or a compaction is unmeasured
    # (SD-OPEN-29/32), so the next prompt is the required fallback.
    #
    # The same activation also reconnects retained registered-batch observers:
    # lock-guarded, completed duties are never replayed and the original
    # assignment is never relaunched. Fail-open like every other path here.
    try:
        from dispatch_batch_obligations import ensure_observers
    except Exception:
        ensure_observers = None
    if ensure_observers is not None:
        for root in seen:
            try:
                jobs_log = Path(root) / "jobs.log"
                if jobs_log.is_file():
                    ensure_observers(jobs_log)
            except Exception:  # noqa: BLE001
                continue
    context = delivery_context(batches)
    if not context:
        return 0
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
    if _event_name(payload) == "UserPromptSubmit":
        for root, records in batches:
            try:
                ack_delivered(root, session_id, records, acked_by=f"session-sweep:{session_id}")
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
