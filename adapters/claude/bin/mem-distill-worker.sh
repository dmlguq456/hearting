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

# --- harness root resolver (2026-08-13; d94ec2de 계열 재발 차단) -----------------
# 설치본은 이 파일에 디렉토리 심링크(~/.claude/bin) 또는 실파일 복사로 도달한다.
# 논리 cd+pwd 에 고정 3단 hop 을 더하면 두 형태 모두에서 엉뚱한 루트를 잡는다.
_hr_marked() { [ -f "$1/harness-manifest.json" ] && [ -f "$1/core/CORE.md" ]; }
_harness_root() {                      # $1 = self path, $2 = required root file
  _hr_self=$1
  _hr_required=$2
  # (1) A partial runtime projection is not a model-config source.
  if [ -n "${AGENT_HOME:-}" ] \
    && [ -f "$AGENT_HOME/core/CORE.md" ] \
    && [ -f "$AGENT_HOME/$_hr_required" ]; then
    printf '%s\n' "$AGENT_HOME"; return 0
  fi
  # (2) 물리 해석된 스크립트 디렉토리에서 마커 쌍 상향 walk — 번들 정체성 보존
  _hr_dir=$(CDPATH= cd -P -- "$(dirname -- "$_hr_self")" 2>/dev/null && pwd -P) || _hr_dir=
  _hr_p=$_hr_dir
  while [ -n "$_hr_p" ] && [ "$_hr_p" != "/" ]; do
    if _hr_marked "$_hr_p"; then printf '%s\n' "$_hr_p"; return 0; fi
    _hr_p=$(dirname -- "$_hr_p")
  done
  # (3) 형제 경로 agent-home.sh — 논리 경로로 호출해야 ~/.claude/utilities 를 탄다
  _hr_log=$(CDPATH= cd -- "$(dirname -- "$_hr_self")" && pwd)
  if [ -x "$_hr_log/../utilities/agent-home.sh" ]; then
    _hr_out=$(AGENT_HOME= "$_hr_log/../utilities/agent-home.sh" 2>/dev/null || true)
    if [ -n "$_hr_out" ] \
      && [ -f "$_hr_out/core/CORE.md" ] \
      && [ -f "$_hr_out/$_hr_required" ]; then
      printf '%s\n' "$_hr_out"; return 0
    fi
  fi
  # (4) 레거시 3단 hop (마커 없는 픽스처/부분 트리 호환)
  CDPATH= cd -- "$_hr_log/../../.." && pwd
}
# ------------------------------------------------------------------------------

# Concrete models come from the complete user config or shipped fallback.
# fast-distiller is the turn-nudge tier; deep-curator is the session-end tier.
_root=$(_harness_root "${BASH_SOURCE[0]}" utilities/model-config.sh)
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
