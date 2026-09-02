#!/usr/bin/env python3
"""SD-122 peer-message ledger recorder.

Two subcommands, dispatched by argv[1]:
  post-tool  -- PostToolUse hook. Records one peer_message_v1 for each
                SendMessage tool call.
  prompt     -- UserPromptSubmit hook. Records one `notice` peer_message_v1
                when the injected prompt carries a cross-session message.

Fail-soft over its entire body: any exception is swallowed and the process
exits 0. Never prints permissionDecision/deny. The prompt path never writes
to stdout (UserPromptSubmit stdout is injected into the prompt).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent
_UTILITIES_DIR = _HOOKS_DIR.parent / "utilities"
_PEER_MESSAGE_PY = _UTILITIES_DIR / "peer-message.py"

_PREFIX_KIND = {
    "[steer]": "steer",
    "[handoff]": "handoff",
    "[gate]": "gate-relay",
}


def _read_stdin_json():
    data = sys.stdin.read()
    if not data:
        return {}
    return json.loads(data)


def _project_of(cwd):
    if not cwd:
        return ""
    return os.path.basename(str(cwd).rstrip("/"))


def _kind_for_body(body, notify_when_idle):
    if notify_when_idle:
        return "watch"
    first_line = (body or "").splitlines()[0].strip() if body else ""
    for prefix, kind in _PREFIX_KIND.items():
        if first_line.startswith(prefix):
            return kind
    return "steer"


def handle_post_tool(payload):
    if payload.get("tool_name") != "SendMessage":
        return
    tool_input = payload.get("tool_input") or {}
    to = tool_input.get("to")
    if to is None:
        return
    message = tool_input.get("message") or ""
    notify_when_idle = bool(tool_input.get("notify_when_idle"))
    kind = _kind_for_body(message, notify_when_idle)

    from_session_id = payload.get("session_id") or ""
    from_project = _project_of(payload.get("cwd"))

    args_list = [
        "--from-harness", "claude",
        "--from-session-id", from_session_id,
        "--from-project", from_project,
        "--to-harness", "claude",
        "--kind", kind,
        "--surface", "claude-native",
        "--status", "sent",
    ]
    if isinstance(to, dict):
        if to.get("session_id"):
            args_list += ["--to-session-id", str(to["session_id"])]
        if to.get("name"):
            args_list += ["--to-name", str(to["name"])]
    else:
        args_list += ["--to-name", str(to)]

    subprocess.run(
        [sys.executable, str(_PEER_MESSAGE_PY), "record"] + args_list,
        input=message.encode("utf-8", "replace"),
        timeout=5,
        capture_output=True,
    )


_CROSS_SESSION_RE = re.compile(r"<cross-session-message\s+from=\"([^\"]*)\"")


def handle_prompt(payload):
    prompt = payload.get("prompt") or ""
    match = _CROSS_SESSION_RE.search(prompt)
    if not match:
        return
    session_id = payload.get("session_id")
    if not session_id:
        # M1: the receiving session's own id is the only verifiable identity
        # source here. Without it we cannot claim an exact to.session_id, and
        # a name fallback would defeat F-98b's exact-session-id join contract.
        return
    from_name = match.group(1)
    args_list = [
        "--from-harness", "claude",
        "--from-session-id", "",
        "--from-project", _project_of(payload.get("cwd")),
        "--to-harness", "claude",
        "--to-session-id", str(session_id),
        "--kind", "notice",
        "--surface", "claude-native",
        "--status", "received",
    ]
    if from_name:
        args_list += ["--to-name", from_name]
    body = "cross-session message received"
    subprocess.run(
        [sys.executable, str(_PEER_MESSAGE_PY), "record"] + args_list,
        input=body.encode("utf-8"),
        timeout=5,
        capture_output=True,
    )


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = _read_stdin_json()
    if mode == "post-tool":
        handle_post_tool(payload)
    elif mode == "prompt":
        handle_prompt(payload)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
