#!/bin/bash
# Weekly study loop comparing external developments with the current harness.
set -u
AGENT_HOME="${AGENT_HOME:-$(sh "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/../utilities/agent-home.sh" 2>/dev/null || printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current")}"
LOOP_DIR="$AGENT_HOME/loops"
LOG="$LOOP_DIR/study.log"
source "$LOOP_DIR/lib.sh"   # PATH correction and retry wrapper.
# Temporary hold guard: .hold contains YYYY-MM-DD and resumes automatically after expiry.
if [ -f "$LOOP_DIR/.hold" ]; then _h=$(cat "$LOOP_DIR/.hold" 2>/dev/null); _t=$(date +%F);
  if [ -z "$_h" ] || [[ "$_t" < "$_h" ]] || [ "$_t" = "$_h" ]; then
    echo "[held until ${_h:-indefinite}] $(date -Iseconds)" >> "$LOG" 2>/dev/null || true; exit 0;
  fi;
fi

mkdir -p "$AGENT_NOTES_ROOT"/study

{
  echo "=== study run $(date -Iseconds) ==="
  cd "$AGENT_SCAN_ROOT" || exit 1
  _prompt="$(render_prompt "$LOOP_DIR/study.md")"
  run_claude_retry 2400 "$_prompt" \
    --allowedTools "Bash,Read,Glob,Grep,Write,WebSearch,WebFetch"
  echo "=== exit $? $(date -Iseconds) ==="
} >> "$LOG"

tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
