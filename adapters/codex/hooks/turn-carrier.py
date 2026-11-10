#!/usr/bin/env python3
"""Codex PostToolUse/Stop bridge for the shared in-turn carrier (utilities/codex_turn_carrier.py).

`post-tool` hands owed Hearting records to the running turn as additionalContext;
`stop` continues the turn (Codex Stop `decision: block`) with owed records, or once
with a received next action no tool has acted on. Fail-open and silent otherwise.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for name in ("utilities", "tools"):
    path = str(ROOT / name)
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        import codex_turn_carrier as carrier
        if mode == "post-tool":
            return carrier.post_tool(payload, sys.stdout)
        if mode == "stop":
            return carrier.stop(payload, sys.stdout)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
