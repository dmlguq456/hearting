#!/bin/sh
# PreToolUse(Edit/Write): require a real core/*.md Read marker before editing adapters/**.
# Portable CLI: core-first-guard.sh --file <path> [--session <id>] [--agent-home <dir>]

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"

usage() {
  cat <<'EOF'
usage: core-first-guard.sh --file <path> [--session <id>] [--agent-home <dir>]

Without arguments, reads Claude hook JSON from stdin.
EOF
}

deny_json() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$1"
  exit 0
}

is_adapter_target() {
  fp=$1
  repo=$2
  case "$fp" in
    "$repo"/adapters/*) return 0 ;;
    *) return 1 ;;
  esac
}

check_gate() {
  fp=$1
  sid=$2

  case "$fp" in
    /*) ;;
    *) fp="$PWD/$fp" ;;
  esac

  probe=$(dirname "$fp")
  while [ ! -d "$probe" ] && [ "$probe" != "/" ]; do
    probe=$(dirname "$probe")
  done
  repo=$(git -C "$probe" rev-parse --show-toplevel 2>/dev/null || true)
  [ -n "$repo" ] || return 0
  is_adapter_target "$fp" "$repo" || return 0

  key=$(printf '%s' "$repo" | sed 's#[/ ]#_#g')
  dir="$AGENT_HOME/.core-grounding"
  found=0
  stale=0

  for marker in "$dir/${sid}__${key}__"core_*.md; do
    [ -f "$marker" ] || continue
    rel=$(sed -n 's/^file=//p' "$marker" | head -1)
    read_mtime=$(sed -n 's/^mtime=//p' "$marker" | head -1)
    [ -n "$rel" ] || continue
    [ -f "$repo/$rel" ] || continue
    cur=$(stat -c %Y "$repo/$rel" 2>/dev/null || echo 0)
    if [ "$cur" -gt "${read_mtime:-0}" ]; then
      stale=1
      continue
    fi
    found=1
    break
  done

  if [ "$found" -eq 1 ]; then
    return 0
  fi
  if [ "$stale" -eq 1 ]; then
    reason="Core-first gate: core documentation changed after the latest core/*.md Read. Read the relevant core contract again before editing adapters/**."
  else
    reason="Core-first gate: before editing adapters/**, read the relevant core contract (CORE.md, DESIGN_PRINCIPLES.md, ADAPTATION.md, or the applicable core/*.md) and determine whether core must change first."
  fi
  return 2
}

if [ "$#" -gt 0 ]; then
  fp=""
  sid="nosession"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "core-first-guard: --file requires a path" >&2; exit 64; }
        fp=$2
        shift 2
        ;;
      --session)
        [ "$#" -ge 2 ] || { echo "core-first-guard: --session requires an id" >&2; exit 64; }
        sid=$2
        shift 2
        ;;
      --agent-home)
        [ "$#" -ge 2 ] || { echo "core-first-guard: --agent-home requires a dir" >&2; exit 64; }
        AGENT_HOME=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "core-first-guard: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$fp" ] || { echo "core-first-guard: --file is required" >&2; exit 64; }
  reason=""
  check_gate "$fp" "$sid"
  rc=$?
  [ "$rc" -eq 0 ] && exit 0
  [ "$rc" -eq 2 ] && printf '%s\n' "$reason" >&2
  exit "$rc"
fi

input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0

fp=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"//; s/"$//')
sid=$(printf '%s' "$input" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"//; s/"$//')
[ -z "$sid" ] && sid="nosession"

reason=""
check_gate "$fp" "$sid"
rc=$?
[ "$rc" -eq 0 ] && exit 0
[ "$rc" -eq 2 ] && deny_json "$reason"
exit 0
