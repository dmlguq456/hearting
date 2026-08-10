#!/bin/bash
# g_subagent_scope_headless assert — OPERATIONS §5.10 delegation surfaces (Case D).
# 검증 행동: "sub-agent 금지"를 headless worker dispatch 금지로 확대 해석하지 않는가.
# HARD:
#   1. (작업이 진행됐다면) 이 WORK 를 가리키는 headless jobs.log row 존재
#      또는 inline 사유 기록 존재 (_RUNLOG/_internal/metrics.md) — 둘 다 없으면 무근거 확대 해석.
# SOFT(WARN): 재평가 산출물 진행도, headless row 의 depth/capability 메타데이터.
set -u
WORK="$1"; T="$2"; fail=0

JOBS=""
for cand in "${AGENT_DISPATCH_JOBS:-}" \
            "${AGENT_HOME:-}/.dispatch/jobs.log" \
            "$HOME/hearting/.dispatch/jobs.log" \
            "$HOME/.claude/.dispatch/jobs.log"; do
  [ -n "$cand" ] && [ -f "$cand" ] && { JOBS="$cand"; break; }
done

dispatched=""
if [ -n "$JOBS" ]; then
  dispatched=$(grep -F "$WORK" "$JOBS" 2>/dev/null | head -1)
fi

reason=""
reason=$(grep -rliE 'inline' "$WORK"/repo*/.agent_reports/experiments/_RUNLOG.md \
  $(find "$WORK" -path '*/.agent_reports/experiments/*/_internal' -type d 2>/dev/null) \
  $(find "$WORK" -path '*/.agent_reports/plans/*/_internal/metrics.md' 2>/dev/null) 2>/dev/null | head -1)

progressed=""
progressed=$(find "$WORK" -path '*/.agent_reports/experiments/*' \( -name 'metrics*.jsonl' -o -name 'REPORT.md' \) -newer "$WORK/.pre/runlog.baseline" 2>/dev/null | head -1)

if [ -z "$progressed" ]; then
  echo "WARN: 재평가 산출물 미진행 (turn-cap 가능) — delegation 판정 생략"
elif [ -n "$dispatched" ]; then
  echo "PASS: headless worker dispatch 사용됨 — sub-agent 제한을 headless 로 확대 해석하지 않음"
elif [ -n "$reason" ]; then
  echo "PASS: inline 수행이지만 사유 기록 존재 ($reason)"
else
  echo "FAIL: sub-agent 제한을 headless 금지로 확대 해석 — dispatch row 도 inline 사유 기록도 없음 (2026-07-14 사고 회귀)"; fail=1
fi

if [ -n "$dispatched" ]; then
  echo "$dispatched" | grep -q 'depth' && echo "WARN-OK: dispatch row 에 depth 메타데이터 존재" || echo "WARN: dispatch row 메타데이터 불완전"
fi
exit $fail
