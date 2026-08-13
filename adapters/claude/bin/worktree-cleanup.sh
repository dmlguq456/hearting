#!/usr/bin/env sh
set -eu

# --- harness root resolver (2026-08-13; d94ec2de 계열 재발 차단) -----------------
# 설치본은 이 파일에 디렉토리 심링크(~/.claude/bin) 또는 실파일 복사로 도달한다.
# 논리 cd+pwd 에 고정 3단 hop 을 더하면 두 형태 모두에서 엉뚱한 루트를 잡는다.
_hr_marked() { [ -f "$1/harness-manifest.json" ] && [ -f "$1/core/CORE.md" ]; }
_harness_root() {                      # $1 = self path, $2 = required root file
  _hr_self=$1
  _hr_required=$2
  # (1) A partial runtime projection cannot provide cleanup implementation.
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

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v git >/dev/null 2>&1 \
  && ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null); then
  :
else
  ROOT=$(_harness_root "$0" utilities/worktree-cleanup.py)
fi

exec python3 "$ROOT/utilities/worktree-cleanup.py" "$@"
