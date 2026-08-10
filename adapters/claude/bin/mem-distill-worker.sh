#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
usage: mem-distill-worker.sh <mode> <model> <prompt-file>

Claude Code realization of the portable memory distillation worker contract.
Reads a prompt file and writes JSON-lines proposals to stdout.
EOF
}

[ "${1:-}" != "-h" ] && [ "${1:-}" != "--help" ] || { usage; exit 0; }
[ "$#" -eq 3 ] || { usage >&2; exit 64; }

mode=$1
model=$2
prompt_file=$3

case "$mode" in
  increment|curate) ;;
  *) echo "mem-distill-worker: unknown mode: $mode" >&2; exit 64 ;;
esac

# Concrete models come from the complete user config or shipped fallback.
# fast-distiller is the turn-nudge tier; deep-curator is the session-end tier.
_mmdir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
_root=$(CDPATH= cd -- "$_mmdir/../../.." && pwd)
eval "$("$_root/utilities/model-config.sh" --adapter claude --source-root "$_root")"
_tier_model() {
  case "$1" in
    deep) printf '%s' "$CFG_TIER_DEEP_MODEL" ;;
    mini) printf '%s' "$CFG_TIER_MINI_MODEL" ;;
    *) printf '%s' "$CFG_TIER_LIGHT_MODEL" ;;
  esac
}
case "$model" in
  fast-distiller)
    model="${CLAUDE_MEM_DISTILL_MODEL:-$(_tier_model "$CFG_LIFECYCLE_NUDGE")}"
    ;;
  deep-curator)
    model="${CLAUDE_MEM_DISTILL_MODEL_SESSIONEND:-$(_tier_model "$CFG_LIFECYCLE_CURATE")}"
    ;;
esac

[ -f "$prompt_file" ] || { echo "mem-distill-worker: prompt file not found: $prompt_file" >&2; exit 64; }
command -v claude >/dev/null 2>&1 || exit 0

# Timeouts vary by mode. Curate uses a deep curator with a large prompt
# (delta + snapshot + artifacts), and measured runs exceeded 120 seconds,
# causing termination before generation and advancing the marker with zero
# actions. setsid detaches the worker, so a longer timeout does not block
# session shutdown and remains below dispatch's 60-minute stale-GC window.
case "$mode" in
  curate) worker_timeout="${MEM_DISTILL_TIMEOUT_CURATE:-600}" ;;
  *)      worker_timeout="${MEM_DISTILL_TIMEOUT:-120}" ;;
esac
if command -v timeout >/dev/null 2>&1; then
  timeout_cmd=(timeout "$worker_timeout")
else
  timeout_cmd=()
fi

DISALLOW='Bash Read Write Edit Glob Grep Agent NotebookEdit WebFetch WebSearch Task'

AGENT_SESSION_ROLE=worker MEM_DISTILL=1 setsid "${timeout_cmd[@]}" claude -p "$(cat "$prompt_file")" \
  --model "$model" \
  --disallowedTools "$DISALLOW"
