#!/bin/sh
# PostToolUse(Read): write a session marker after core/*.md is actually read.
# Portable CLI: core-read-marker.sh --file <core-doc.md> [--session <id>] [--agent-home <dir>]

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"

usage() {
  cat <<'EOF'
usage: core-read-marker.sh --file <core-doc.md> [--session <id>] [--agent-home <dir>]

Without arguments, reads Claude hook JSON from stdin.
EOF
}

mark_read() {
  fp=$1
  sid=$2

  case "$fp" in
    /*) ;;
    *) fp="$PWD/$fp" ;;
  esac
  [ -f "$fp" ] || return 0

  repo=$(git -C "$(dirname "$fp")" rev-parse --show-toplevel 2>/dev/null || true)
  [ -n "$repo" ] || return 0
  case "$fp" in
    "$repo"/core/*.md) ;;
    *) return 0 ;;
  esac

  rel=${fp#"$repo"/}
  key=$(printf '%s' "$repo" | sed 's#[/ ]#_#g')
  doc=$(printf '%s' "$rel" | sed 's#[/ ]#_#g')
  mtime=$(stat -c %Y "$fp" 2>/dev/null || echo 0)

  mkdir -p "$AGENT_HOME/.core-grounding"
  printf 'repo=%s\nfile=%s\nmtime=%s\n' "$repo" "$rel" "$mtime" > "$AGENT_HOME/.core-grounding/${sid}__${key}__${doc}"
}

if [ "$#" -gt 0 ]; then
  fp=""
  sid="nosession"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "core-read-marker: --file requires a path" >&2; exit 64; }
        fp=$2
        shift 2
        ;;
      --session)
        [ "$#" -ge 2 ] || { echo "core-read-marker: --session requires an id" >&2; exit 64; }
        sid=$2
        shift 2
        ;;
      --agent-home)
        [ "$#" -ge 2 ] || { echo "core-read-marker: --agent-home requires a dir" >&2; exit 64; }
        AGENT_HOME=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "core-read-marker: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$fp" ] || { echo "core-read-marker: --file is required" >&2; exit 64; }
  mark_read "$fp" "$sid"
  exit 0
fi

input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0

fp=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"//; s/"$//')
sid=$(printf '%s' "$input" | grep -o '"session_id"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"session_id"[[:space:]]*:[[:space:]]*"//; s/"$//')
[ -z "$sid" ] && sid="nosession"

mark_read "$fp" "$sid"
exit 0
