#!/bin/sh
# installed by herdr
# managed by herdr; reinstalling or updating the integration overwrites this file.
# add custom hooks beside this file instead of editing it.
# HERDR_INTEGRATION_ID=claude
# HERDR_INTEGRATION_VERSION=11

set -eu

action="${1:-}"
[ "$action" = session ] || exit 0
hook_input_file="$(mktemp "${TMPDIR:-/tmp}/herdr-claude-hook.XXXXXX")" || exit 0
trap 'rm -f "$hook_input_file"' EXIT HUP INT TERM
cat >"$hook_input_file" 2>/dev/null || true

[ "${HERDR_ENV:-}" = "1" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

HERDR_HOOK_INPUT_FILE="$hook_input_file" HERDR_HOOK_SCRIPT="$0" python3 - <<'PY'
import json
import os
import random
import socket
import sys
import time
from pathlib import Path

source = "herdr:claude"
pane_id = os.environ.get("HERDR_PANE_ID")
socket_path = os.environ.get("HERDR_SOCKET_PATH")
try:
    with open(os.environ["HERDR_HOOK_INPUT_FILE"], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    payload = {}
if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
    raise SystemExit(0)
if os.environ.get("CURSOR_VERSION") or "cursor_version" in payload or payload.get("agent_id"):
    raise SystemExit(0)
session_id = payload.get("session_id")
if not isinstance(session_id, str) or not session_id:
    raise SystemExit(0)

# Keep Fleet's exact runtime-root and registered-worker guard before reporting.
roots = []
script = os.environ.get("HERDR_HOOK_SCRIPT")
if script:
    roots.extend(parent / "tools" for parent in Path(script).resolve().parents)
home = os.environ.get("AGENT_HOME")
if home:
    roots.append(Path(home) / "tools")
allowed = False
for tools in roots:
    if not (tools / "fleet" / "herdr_projection.py").is_file():
        continue
    try:
        # A tree whose `fleet` package imported but whose projection failed stays
        # cached; drop it so this candidate loads its own copy.
        for name in [name for name in sys.modules if name == "fleet" or name.startswith("fleet.")]:
            del sys.modules[name]
        sys.path.insert(0, str(tools))
        from fleet.herdr_projection import may_report
        allowed = may_report("claude", session_id, pane_id=pane_id)
        break
    except Exception:
        continue
if not allowed:
    raise SystemExit(0)

params = {
    "pane_id": pane_id,
    "source": source,
    "agent": "claude",
    "seq": time.time_ns(),
    "agent_session_id": session_id,
}
transcript = payload.get("transcript_path")
if isinstance(transcript, str) and transcript.strip():
    params["agent_session_path"] = transcript
start_source = payload.get("source")
if isinstance(start_source, str) and start_source in {"startup", "resume", "clear", "compact", "fork"}:
    params["session_start_source"] = start_source
    try:
        from fleet.process_identity import current_launch_context, native_resume_wire_argv
        launch = current_launch_context("claude", session_id)
        resume_argv = (launch or {}).get("argv")
        wire_argv = native_resume_wire_argv(resume_argv, "claude", session_id)
        if wire_argv is not None:
            params["resume_argv"] = wire_argv
    except Exception:
        pass
request = {
    "id": f"{source}:{int(time.time() * 1000)}:{random.randrange(1_000_000):06d}",
    "method": "pane.report_agent_session",
    "params": params,
}
try:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.5)
    client.connect(socket_path)
    client.sendall((json.dumps(request) + "\n").encode())
    try:
        client.recv(4096)
    except Exception:
        pass
    client.close()
except Exception:
    pass
PY
