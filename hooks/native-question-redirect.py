#!/usr/bin/env python3
"""Claude PreToolUse(AskUserQuestion): register the question as this session's Hearting question
and refuse the native call (utilities/native_question_redirect.py).

When nothing was registered the native question goes through and keeps its Fleet decision
marker (hooks/fleet-interaction-state.py), so the person always sees the question."""
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))


def _decision_marker(raw: str) -> None:
    spec = importlib.util.spec_from_file_location("fleet_interaction_state", ROOT / "hooks" / "fleet-interaction-state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.stdin, sys.argv = io.StringIO(raw), ["fleet-interaction-state.py", "set", "--kind", "decision"]
    module.main()


def main() -> int:
    raw = sys.stdin.read()
    try:
        import native_question_redirect as redirect
        payload = json.loads(raw or "{}")
        reason = redirect.redirect("claude", payload)
    except Exception:  # noqa: BLE001 -- a redirect that cannot run never affects the question
        reason = None
    if reason is not None:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}},
                         ensure_ascii=False, separators=(",", ":")))
        return 0
    try:
        _decision_marker(raw)
    except Exception:  # noqa: BLE001 -- the marker is advisory
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
