#!/usr/bin/env python3
"""SD-111 P4 -- Claude carrier 2 wiring: durable-session-activation sweep.

Registered on ``SessionStart`` and ``UserPromptSubmit`` only (never ``Stop``,
§13.33.1-(5) / the 2026-07-10 measurement, CC #38651). Claude is measured
`measured-unsupported` for session-generation proof (§3.5), so every sweep
here is refused with ``pending-delivery-generation-unproven`` -- that
observable refusal, not a delivered receipt, is the point (plan §7 A-21).

The round-2 plan explicitly cut the "relay a pending count into
``additionalContext``" slice (§12-7): with no claim authority this carrier
surfaces *nothing* to the session. This hook always exits 0 with empty
stdout; it wires the surface and lets it refuse.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "utilities"))
from dispatch_contract import dispatch_state_roots, resolve_agent_home  # noqa: E402
from dispatch_session_sweep import sweep  # noqa: E402

RECIPIENT_KIND = "claude-parent-runtime"
SESSION_GENERATION = "unsupported"


def _session_id(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


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
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        try:
            sweep(root, RECIPIENT_KIND, session_id, SESSION_GENERATION)
        except Exception:  # noqa: BLE001 -- fail-open (§13.33.1-(3))
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
