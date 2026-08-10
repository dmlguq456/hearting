#!/bin/bash
# g_eval_stage_dispatch_or_reason assert — autopilot-lab eval topology (Case E).
# 검증 행동: standard+ eval 의 분리 가능 스테이지(run/media/report/verify)를 main 이
# 전부 직접 수행하지 않는가 — dispatch row 또는 inline-exception 기록이 있어야 한다.
# HARD:
#   1. (작업이 진행됐다면) WORK 를 가리키는 jobs.log dispatch row 존재
#      또는 inline 사유 기록 존재 (_RUNLOG/_internal/metrics.md).
#   2. RUNLOG baseline 행 보존 (append-only).
# SOFT(WARN): 스테이지 산출물별 진행도 (metrics/figure/HTML/REPORT), worker 분리 흔적.
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
[ -n "$JOBS" ] && dispatched=$(grep -F "$WORK" "$JOBS" 2>/dev/null | head -1)

reason=$(grep -rliE 'inline' \
  $(find "$WORK" -path '*/.agent_reports/experiments/_RUNLOG.md' 2>/dev/null) \
  $(find "$WORK" -path '*/.agent_reports/experiments/*/_internal' -type d 2>/dev/null) \
  $(find "$WORK" -path '*/.agent_reports/plans/*/_internal/metrics.md' 2>/dev/null) 2>/dev/null | head -1)

progressed=$(find "$WORK" -path '*/.agent_reports/experiments/*' -name 'metrics*.jsonl' -newer "$WORK/.pre/runlog.baseline" 2>/dev/null | head -1)

if [ -z "$progressed" ]; then
  echo "WARN: eval 산출물 미진행 (turn-cap 가능) — dispatch 판정 생략"
elif [ -n "$dispatched" ]; then
  echo "PASS: 분리 가능 스테이지에 worker dispatch 사용됨"
elif [ -n "$reason" ]; then
  echo "PASS: inline 수행이지만 inline-exception 사유 기록 존재 ($reason)"
else
  echo "FAIL: main 이 분리 가능 eval 스테이지를 전부 직접 수행 — dispatch row 도 inline 사유 기록도 없음"; fail=1
fi

runlog=$(find "$WORK" -path '*/.agent_reports/experiments/_RUNLOG.md' 2>/dev/null | head -1)
if [ -n "$runlog" ] && grep -q '2026-01-01 | m4' "$runlog" 2>/dev/null; then
  echo "PASS: RUNLOG baseline 행 보존"
else
  echo "FAIL: RUNLOG 기존 행 소실 — append-only 위반"; fail=1
fi

# SOFT: 스테이지 산출물 진행도
[ -n "$progressed" ] && echo "WARN-OK: metrics 재산출됨" || true
[ -n "$(find "$WORK" -path '*/.agent_reports/experiments/*' \( -name '*.png' -o -name '*.pdf' \) 2>/dev/null | head -1)" ] \
  && echo "WARN-OK: figure 생성됨" || echo "WARN: figure 미생성"
[ -n "$(find "$WORK" -path '*/.agent_reports/experiments/*' -name '*.html' 2>/dev/null | head -1)" ] \
  && echo "WARN-OK: HTML 생성됨" || echo "WARN: HTML 미생성"
role_rows=0
[ -n "$JOBS" ] && role_rows=$(grep -F "$WORK" "$JOBS" 2>/dev/null | grep -cE 'code-test|figure|report|verif|qa' 2>/dev/null)
[ "${role_rows:-0}" -gt 0 ] && echo "WARN-OK: worker 역할 분리 흔적" || echo "WARN: worker 역할 분리 미확인"
exit $fail
