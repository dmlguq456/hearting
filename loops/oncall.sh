#!/bin/bash
# Nightly reconnaissance loop, invoked from crontab.
# Produces one notes/oncall report and may ingest bounded proposal evidence;
# it never edits or commits source, runtime config, plugins, or memory.
set -u
AGENT_HOME="${AGENT_HOME:-$(sh "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/../utilities/agent-home.sh" 2>/dev/null || printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current")}"
LOOP_DIR="$AGENT_HOME/loops"
LOG="$LOOP_DIR/oncall.log"
source "$LOOP_DIR/lib.sh"   # PATH correction and retry wrapper.
# Temporary hold guard: .hold contains YYYY-MM-DD and resumes automatically after expiry.
if [ -f "$LOOP_DIR/.hold" ]; then _h=$(cat "$LOOP_DIR/.hold" 2>/dev/null); _t=$(date +%F);
  if [ -z "$_h" ] || [[ "$_t" < "$_h" ]] || [ "$_t" = "$_h" ]; then
    echo "[held until ${_h:-indefinite}] $(date -Iseconds)" >> "$LOG" 2>/dev/null || true; exit 0;
  fi;
fi

# NAS mount guard (2026-07-15 incident): if /home/nas is not mounted, the mkdir
# below would create shadow dirs on the root fs and cd would "succeed" against
# an empty tree. Fail loudly instead; the retry lands after remount.
if [ -n "${AGENT_SCAN_MOUNT:-}" ] && ! mountpoint -q "$AGENT_SCAN_MOUNT"; then
  echo "[oncall] SKIP: $AGENT_SCAN_MOUNT not mounted — exit 1 $(date -Iseconds)" >> "$LOG" 2>/dev/null || true
  exit 1
fi
mkdir -p "$AGENT_NOTES_ROOT"/oncall

{
  echo "=== oncall run $(date -Iseconds) ==="
  cd "$AGENT_SCAN_ROOT" || exit 1
  _prompt="$(render_prompt "$LOOP_DIR/oncall.md")"
  run_claude_retry 900 "$_prompt" \
    --model sonnet \
    --allowedTools "Bash,Read,Glob,Grep,Write"
  rc=$?
  # Success requires today's heartbeat report file: a clean exit without the
  # file is a silent failure (e.g. an empty-prompt run), not a pass.
  today_report="$AGENT_NOTES_ROOT/oncall/$(date +%F).md"
  if [ "$rc" -eq 0 ] && [ ! -f "$today_report" ]; then
    echo "=== FAIL: exit 0 but heartbeat report missing ($today_report) ==="
    rc=1
  fi
  echo "=== exit $rc $(date -Iseconds) ==="
} >> "$LOG"

# Bound the log to the most recent 2,000 lines.
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
