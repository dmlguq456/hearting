#!/usr/bin/env bash
# mem-turn-nudge — deterministic distillation turn counter
# (DESIGN_PRINCIPLES §0.5, spec v5 Cluster B/B2).
#   Increment a per-session counter on each UserPromptSubmit. At N turns, emit
#   no main-session context; call the sibling dispatcher in argument mode to
#   launch a detached distiller when MEM_DISTILL_ENABLE is on, then reset the
#   counter (D-13). Adapter-native hook settings own registration.
set -euo pipefail
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh")}"

# D-42 main/worker boundary. Place this before stdin parsing and every counter
# write. Draining stdin avoids SIGPIPE/nonzero exits in pipefail callers.
if [ "${AGENT_SESSION_ROLE:-}" = "worker" ] \
  || [ "${AGENT_DISPATCH_CHILD:-}" = "1" ] \
  || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
  || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
  || [ "${FLEET_TITLE_REFRESH:-}" = "1" ] \
  || [ "${MEM_DISTILL:-}" = "1" ]; then
  cat >/dev/null 2>&1
  exit 0
fi

input=$(cat 2>/dev/null || true)
eval "$(printf '%s' "$input" | python3 -c '
import json, sys, shlex
try: d = json.load(sys.stdin)
except Exception: d = {}
print("EVENT="+shlex.quote(d.get("hook_event_name","") or ""))
print("SID="+shlex.quote(d.get("session_id","") or "default"))
' 2>/dev/null || true)"
EVENT="${EVENT:-}"; SID="${SID:-default}"
[ "$EVENT" = "UserPromptSubmit" ] || exit 0

N="${MEM_NUDGE_INTERVAL:-10}"
STORE=$(sh "$HOOK_DIR/../utilities/memory-store.sh")
DB="$STORE/memory.db"
STATE="$STORE/.turn-state-$SID"

counter=0
if [ -f "$STATE" ]; then counter=$(sed -n '1p' "$STATE" 2>/dev/null || echo 0); fi
case "$counter" in (*[!0-9]*|"") counter=0 ;; esac

# The counter resets only when the dispatcher fires. It measures turns since
# the previous distillation request, which approximates accumulated transcript
# delta. An explicit `mem add` does not advance the shared distillation marker,
# so it does not reset this counter; the transcript still captures that action.
counter=$((counter + 1))

fire=0
if [ "$counter" -ge "$N" ]; then fire=1; counter=0; fi

mkdir -p "$STORE" 2>/dev/null || true
printf '%s\n' "$counter" > "$STATE" 2>/dev/null || true

# Remove state for sessions inactive for more than three days. Fail open.
find "$STORE" -maxdepth 1 -name '.turn-state-*' -mmin +4320 -delete 2>/dev/null || true

# Fire action (D6 self-location): call the sibling dispatcher in argument mode.
# Self-location keeps worktree hooks paired with worktree dispatchers and live
# hooks paired with live dispatchers. The dispatcher owns the opt-in gate. All
# output is suppressed and failures remain fail-open. Skip an empty/default SID
# so unrelated SID-less sessions cannot share one marker or lock (QA-②); the
# counter reset and state persistence above still stand.
if [ "$fire" = "1" ] && [ -n "$SID" ] && [ "$SID" != "default" ]; then
  HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
  bash "$HOOK_DIR/mem-distill-dispatch.sh" distill "$SID" "$PWD" </dev/null >/dev/null 2>&1 || true
fi
exit 0
