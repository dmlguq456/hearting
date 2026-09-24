#!/bin/sh
# installed by herdr
# managed by herdr; reinstalling or updating the integration overwrites this file.
# add custom hooks beside this file instead of editing it.
# HERDR_INTEGRATION_ID=claude
# HERDR_INTEGRATION_VERSION=4

set -eu

# Whether this process may report at all — a registered worker (D-42), or a process that
# is not the runtime of the payload's session (a test feeding a fake session id while it
# inherits this pane's HERDR_*) — is decided once, by `may_report()` in
# tools/fleet/herdr_projection.py, the same check the pane-header projection uses.

action="${1:-}"
hook_input_file="$(mktemp "${TMPDIR:-/tmp}/herdr-claude-hook.XXXXXX")" || exit 0
trap 'rm -f "$hook_input_file"' EXIT HUP INT TERM
cat >"$hook_input_file" 2>/dev/null || true

case "$action" in
  working|idle|blocked|release) ;;
  *) exit 0 ;;
esac

[ "${HERDR_ENV:-}" = "1" ] || exit 0
[ -n "${HERDR_SOCKET_PATH:-}" ] || exit 0
[ -n "${HERDR_PANE_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

HERDR_ACTION="$action" HERDR_HOOK_INPUT_FILE="$hook_input_file" HERDR_HOOK_SCRIPT="$0" python3 - <<'PY'
import json
import os
import random
import socket
import sys
import time
from pathlib import Path

source = "herdr:claude"
action = os.environ.get("HERDR_ACTION", "")
pane_id = os.environ.get("HERDR_PANE_ID")
socket_path = os.environ.get("HERDR_SOCKET_PATH")
hook_input_file = os.environ.get("HERDR_HOOK_INPUT_FILE")

if not pane_id or not socket_path:
    raise SystemExit(0)

hook_input = {}
if hook_input_file:
    try:
        with open(hook_input_file, encoding="utf-8") as handle:
            content = handle.read()
        if content.strip():
            hook_input = json.loads(content)
    except Exception:
        hook_input = {}

hook_event_name = str(hook_input.get("hook_event_name") or "")
is_subagent = bool(hook_input.get("agent_id"))
if hook_event_name == "SubagentStop":
    # SubagentStop is a completion event. Older Herdr integrations mapped it
    # to durable working, but Claude recap/away-summary can emit it after the
    # main turn has already stopped. Never let it revive an idle pane.
    raise SystemExit(0)
if is_subagent and action in ("idle", "release"):
    # Subagent completion must not make the parent pane look done early.
    raise SystemExit(0)

def _may_report(session):
    """tools/fleet/herdr_projection.may_report, or False when it cannot be found."""
    # This hook's own release first: an older AGENT_HOME may predate may_report.
    roots = []
    script = os.environ.get("HERDR_HOOK_SCRIPT")
    if script:
        roots += [parent / "tools" for parent in Path(script).resolve().parents]
    home = os.environ.get("AGENT_HOME")
    if home:
        roots.append(Path(home) / "tools")
    for tools in roots:
        if not (tools / "fleet" / "herdr_projection.py").is_file():
            continue
        try:
            sys.path.insert(0, str(tools))
            from fleet.herdr_projection import may_report
            return may_report("claude", session)
        except Exception:
            return False
    return False


request_id = f"{source}:{int(time.time() * 1000)}:{random.randrange(1_000_000):06d}"
report_seq = time.time_ns()
session_id = hook_input.get("session_id")
agent_session_id = session_id if isinstance(session_id, str) and session_id else None
if not _may_report(agent_session_id):
    raise SystemExit(0)
if action == "release":
    request = {
        "id": request_id,
        "method": "pane.release_agent",
        "params": {
            "pane_id": pane_id,
            "source": source,
            "agent": "claude",
            "seq": report_seq,
        },
    }
else:
    request = {
        "id": request_id,
        "method": "pane.report_agent",
        "params": {
            "pane_id": pane_id,
            "source": source,
            "agent": "claude",
            "state": action,
            "seq": report_seq,
        },
    }
    if agent_session_id:
        request["params"]["agent_session_id"] = agent_session_id

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
