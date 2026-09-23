#!/bin/sh
# PreToolUse(Write): block direct writes to built-in file memory at projects/<cwd>/memory/*.md.
# Portable CLI: builtin-memory-guard.sh --file <path>
#   memory.db is the single source of truth, so all memory writes go through
#   the mem CLI. Python file I/O inside mem.py and projections do not use the
#   Write tool and are unaffected. Verifiable hard gate; POSIX sh, no jq.

reason='Direct writes to built-in file memory (projects/<cwd>/memory/*.md) are forbidden. memory.db is the single source of truth. Write memories through the mem CLI: python3 <agent-home>/tools/memory/mem.py add <tier> <type> "<body>" or note "<body>"; use /post-it for user-controlled notes. (Adapter memory policy; MEMORY §7)'

usage() {
  cat <<'EOF'
usage: builtin-memory-guard.sh --file <path>

Without arguments, reads Claude hook JSON from stdin.
EOF
}

check_file() {
  fp=$1
  case "$fp" in
    */projects/*/memory/*.md) return 2 ;;
    *) return 0 ;;
  esac
}

if [ "$#" -gt 0 ]; then
  fp=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --file)
        [ "$#" -ge 2 ] || { echo "builtin-memory-guard: --file requires a path" >&2; exit 64; }
        fp=$2
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "builtin-memory-guard: unknown argument: $1" >&2
        usage >&2
        exit 64
        ;;
    esac
  done
  [ -n "$fp" ] || { echo "builtin-memory-guard: --file is required" >&2; exit 64; }
  check_file "$fp"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    exit 0
  fi
  [ "$rc" -eq 2 ] && printf '%s\n' "$reason" >&2
  exit "$rc"
fi

input=$(cat 2>/dev/null)
[ -z "$input" ] && exit 0

fp=$(printf '%s' "$input" | grep -o '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"file_path"[[:space:]]*:[[:space:]]*"//; s/"$//')

check_file "$fp"
rc=$?
if [ "$rc" -eq 0 ]; then
  exit 0
fi
if [ "$rc" -eq 2 ]; then
  # The reason quotes a mem command line. Claude Code treats unparseable hook
  # output as a non-blocking error and lets the write through, so an unescaped
  # quote here turns the deny into an allow.
  reason_json=$(printf '%s' "$reason" | sed 's/\\/\\\\/g; s/"/\\"/g')
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason_json"
fi
exit 0
