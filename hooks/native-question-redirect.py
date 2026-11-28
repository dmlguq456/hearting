#!/usr/bin/env python3
"""Claude PreToolUse(AskUserQuestion): carry the outcome of utilities/native_question_redirect.py.

Registered (a new or the same open Hearting question), already open natively, or unknown: the
native call is refused with its one line. Otherwise (registration failed before any write and no
same question was read, or the call is not a person's question) the native question goes through
with its Fleet decision marker (hooks/fleet-interaction-state.py), which keeps the call's private
reference the redirect left on it."""
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
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = None
    try:
        import native_question_redirect as redirect
    except Exception:  # noqa: BLE001 -- nothing can be registered or written: the native question shows
        redirect = None
    reason = redirect.redirect("claude", payload) if redirect is not None else None  # never raises
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
