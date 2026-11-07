#!/usr/bin/env python3
"""Claude Stop bridge for the shared session-tidy idle observer; always silent."""
from __future__ import annotations

import json
import os
import sys

from pathlib import Path

HERE = Path(__file__).resolve().parents[1]


def active_root() -> Path:
    candidate = Path(os.environ.get("AGENT_HOME", "")) if os.environ.get("AGENT_HOME") else None
    if candidate and (candidate / "core" / "CORE.md").is_file():
        return candidate
    return HERE


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        root = active_root()
        utilities = str(root / "utilities")
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        if isinstance(payload, dict) and not payload.get("agent_id"):
            from session_shell import lifecycle
            lifecycle("claude", "stop", payload.get("session_id", ""))
        from session_tidy_idle import launch_observer
        launch_observer("claude", payload)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
