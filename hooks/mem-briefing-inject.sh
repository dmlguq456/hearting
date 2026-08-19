#!/usr/bin/env bash
# mem-briefing-inject — morning discussion desk (Cluster F D-26).
#   On each UserPromptSubmit, inject today's on-call report plus an overnight
#   processing summary when the current directory is the dedicated desk, the
#   report exists, and today's briefing has not yet been emitted. This is
#   limited to once per day. Using the first interaction after cron is robust
#   for long-lived sessions where SessionStart does not run each morning.
#
#   Guards:
#     - MEM_DISTILL=1 → exit 0 (prevent recursion in distiller sessions)
#     - hook_event_name ≠ UserPromptSubmit → exit 0
#     - cwd ≠ ${MEM_BRIEFING_DESK:-$HOME/.claude} → exit 0
#     - no report for today → exit 0
#     - already briefed today → exit 0 (`.briefing-<date>` state file)
#   Read-only: inspect notes and the graveyard, then emit additionalContext.
#   The hook must never block the user flow.
#   Portable CLI:
#     mem-briefing-inject.sh --cwd <dir> [--format text|claude-json]
#
#   Adapter-native hook settings own registration.
set -euo pipefail
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
AGENT_HOME="${AGENT_HOME:-$("$HOOK_DIR/../utilities/agent-home.sh")}"

usage() {
  cat <<'EOF'
usage: mem-briefing-inject.sh --cwd <dir> [--format text|claude-json]

Without arguments, reads Claude hook JSON from stdin and emits Claude hook JSON.
EOF
}

# D-42: the morning desk is main-session context, never worker bootstrap.
if [ "${AGENT_SESSION_ROLE:-}" = "worker" ] \
  || [ "${AGENT_DISPATCH_CHILD:-}" = "1" ] \
  || [ -n "${AGENT_DISPATCH_DEPTH:-}" ] \
  || [ -n "${OPENCODE_DISPATCH_SLUG:-}" ] \
  || [ "${FLEET_TITLE_REFRESH:-}" = "1" ] \
  || [ "${MEM_DISTILL:-}" = "1" ]; then
  cat >/dev/null 2>&1
  exit 0
fi

EVENT="UserPromptSubmit"
CWD=""
FORMAT="claude-json"

if [ "$#" -gt 0 ]; then
  FORMAT="text"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --cwd)
        [ "$#" -ge 2 ] || { echo "mem-briefing-inject: --cwd requires a dir" >&2; exit 64; }
        CWD=$2
        shift 2
        ;;
      --format)
        [ "$#" -ge 2 ] || { echo "mem-briefing-inject: --format requires a value" >&2; exit 64; }
        case "$2" in text|claude-json) FORMAT=$2 ;; *) echo "mem-briefing-inject: unknown format: $2" >&2; exit 64 ;; esac
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "mem-briefing-inject: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$CWD" ] || { echo "mem-briefing-inject: --cwd is required" >&2; exit 64; }
else
  input=$(cat 2>/dev/null || true)
  eval "$(printf '%s' "$input" | python3 -c '
import json, sys, shlex
try: d = json.load(sys.stdin)
except Exception: d = {}
print("EVENT="+shlex.quote(d.get("hook_event_name","") or ""))
print("CWD="+shlex.quote(d.get("cwd","") or ""))
' 2>/dev/null || true)"
  EVENT="${EVENT:-}"; CWD="${CWD:-}"
fi

[ "$EVENT" = "UserPromptSubmit" ] || exit 0
# Dedicated-desk gate. The default is the Claude runtime desk (`$HOME/.claude`).
# The harness repository may be an ordinary workspace, so it is not the default.
DEFAULT_BRIEFING_DESK="${HOME:-}/.claude"
BRIEFING_DESK="${MEM_BRIEFING_DESK:-$DEFAULT_BRIEFING_DESK}"
[ "$CWD" = "$BRIEFING_DESK" ] || exit 0

TODAY="$(date +%F)"
# MEM_BRIEFING_ONCALL is a test-isolation override; the production default
# resolves through the machine-local loop env (personal roots stay untracked).
AGENT_LOOP_ENV="${AGENT_LOOP_ENV:-${HOME:-}/.config/hearting/loops.env}"
[ -f "$AGENT_LOOP_ENV" ] && . "$AGENT_LOOP_ENV"
AGENT_NOTES_ROOT="${AGENT_NOTES_ROOT:-${HOME:-}/agent-notes}"
ONCALL="${MEM_BRIEFING_ONCALL:-$AGENT_NOTES_ROOT/oncall/$TODAY.md}"
STORE=$(sh "$HOOK_DIR/../utilities/memory-store.sh")
STATE="$STORE/.briefing-$TODAY"

[ -f "$ONCALL" ] || exit 0      # No report yet: skip.
[ -f "$STATE" ] && exit 0       # Already briefed today: skip.

mkdir -p "$STORE" 2>/dev/null || true
: > "$STATE"                    # Mark first to prevent duplicates and races.
# Remove briefing markers older than seven days.
find "$STORE" -maxdepth 1 -name '.briefing-*' -mtime +7 -delete 2>/dev/null || true

# Overnight summary (D-25): today's graveyard prune count, which remains recoverable.
GY="$STORE/deleted-records.jsonl"
PRUNED=0
[ -f "$GY" ] && PRUNED="$(grep -c "$TODAY" "$GY" 2>/dev/null || echo 0)"

# Institutionalization review candidates (D-28). The command exposes visible
# durable records as evidence; the agent decides contextually whether any item
# belongs in a durable harness artifact. MEM_PY is a test-isolation override.
PROMO="$(cd "$AGENT_HOME" 2>/dev/null && python3 "${MEM_PY:-$AGENT_HOME/tools/memory/mem.py}" promote-candidates 2>/dev/null || true)"

# Emit additionalContext or plain text. JSON escaping avoids shell interpolation (R4).
ONCALL_FILE="$ONCALL" PRUNED="$PRUNED" PROMO="$PROMO" FORMAT="$FORMAT" python3 -c '
import os, json
try:
    body = open(os.environ["ONCALL_FILE"], encoding="utf-8").read()
except Exception:
    body = "(failed to read the on-call report)"
pruned = os.environ.get("PRUNED", "0").strip()
promo = os.environ.get("PROMO", "").strip()
msg = "# \U0001f305 Morning discussion desk (today\u2019s on-call briefing)\n\n"
msg += ("Before answering the user, summarize this briefing in the user\u2019s communication language "
        "and work through its items. Handle clear, reversible actions directly and report them; "
        "discuss only decisions that need user judgment (Cluster F D-25). If the user has a more "
        "urgent request, prioritize it and defer the briefing without dropping its items.\n\n")
if pruned and pruned != "0":
    msg += f"- Overnight memory pruning (recoverable from the graveyard): {pruned}\n\n"
msg += "## Today\u2019s on-call report\n" + body
if promo:
    msg += ("\n\n## Institutionalization review candidates\n"
            "The records below are evidence, not automatic promotion decisions. Judge each item in "
            "context. If one belongs in a runtime bootstrap, CONVENTIONS, DESIGN_PRINCIPLES, a hook, "
            "or a drill case, discuss the destination with the user, apply and verify the change, "
            "then decide whether the memory record should be pruned (D-28).\n" + promo)
if os.environ.get("FORMAT") == "text":
    print(msg)
    raise SystemExit
out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": msg}}
print(json.dumps(out, ensure_ascii=False))
' || true

exit 0
