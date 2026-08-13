#!/usr/bin/env sh
set -eu
# Concrete model IDs and default efforts come from the user copy when valid,
# with the shipped adapter file as a whole-file fallback.
# Role->tier grouping is ALSO config-owned (CFG_ROLES_DEEP/LIGHT): membership is
# derived here instead of hardcoded case labels (2026-07-22 단일원천화 — 종전엔
# case 라벨이 conf grouping을 중복 보유해 역할 이동 시 두 곳을 고쳐야 했다).
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

root=$(_harness_root "$0" utilities/model-config.sh)
eval "$("$root/utilities/model-config.sh" --adapter claude --source-root "$root")"
role=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | tr '_-' '  ' | awk '{$1=$1; print}')
family=claude
case "|$CFG_ROLES_DEEP|" in
  *"|$role|"*)
    model=${CLAUDE_MODEL_DEEP:-$CFG_TIER_DEEP_MODEL}; effort=${CLAUDE_EFFORT_DEEP:-$CFG_TIER_DEEP_EFFORT};;
  *)
    case "|$CFG_ROLES_LIGHT|" in
      *"|$role|"*)
        model=${CLAUDE_MODEL_BALANCED:-$CFG_TIER_LIGHT_MODEL}; effort=${CLAUDE_EFFORT_BALANCED:-$CFG_TIER_LIGHT_EFFORT};;
      *) echo "claude model-map: unknown role: ${1:-}" >&2; exit 64;;
    esac;;
esac
printf 'adapter=claude\nfamily=%s\nexact_model_id=%s\nreasoning=%s\nprobe=opt-in:claude -p --no-session-persistence --permission-mode plan --max-turns 1 --model <id> --effort <level>\n' "$family" "$model" "$effort"
