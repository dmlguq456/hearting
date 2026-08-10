#!/bin/bash
# hard: 실배선 e2e — mem-distill-dispatch(argument 모드) 가 케이스 세션 transcript 를 delta 로
#   추출해 worker 를 분사하고, 격리 MEM_STORE 에 marker 를 전진시킴. 커버: worker 경로 해석 ·
#   PROJECTS transcript 해석 · applier · marker (2026-07-03 migration 파손 회귀 방지).
# soft: 증류 레코드에 대화의 고유 표식(ZEPHYR-77)이 실제 반영됐나 (distiller 판단 차는 WARN).
# 주의: claude 배선 전용 — 비클로드 adapter 런은 claude transcript 가 없어 SKIP(PASS) 처리.
set -u
WORK=$1; T=$2
enc=$(cat "$WORK/.pre/enc_cwd" 2>/dev/null || echo "")
SESS_DIR="$HOME/.claude/projects/$enc"
sid_file=$(ls -t "$SESS_DIR"/*.jsonl 2>/dev/null | head -1)
if [ -z "$sid_file" ]; then
  echo "SKIP: claude 세션 transcript 없음 (비클로드 adapter 런) — claude 배선 전용 케이스"
  exit 0
fi
sid=$(basename "$sid_file" .jsonl)

# 격리 store — 실 DB 무오염. dispatch·worker·applier 전부 MEM_STORE 를 상속한다.
STORE=$(mktemp -d /tmp/drill-memstore-XXXX)
export MEM_STORE="$STORE" MEM_DISTILL_ENABLE=1

DISPATCH="$HOME/.claude/hooks/mem-distill-dispatch.sh"
[ -f "$DISPATCH" ] || DISPATCH="${DRILL_MARKER_HOME:-$HOME/hearting}/adapters/claude/hooks/mem-distill-dispatch.sh"

fail=0
bash "$DISPATCH" distill "$sid" "$WORK/repo" >/dev/null 2>&1
ok=""
for i in $(seq 1 60); do
  [ -f "$STORE/.distill-state-$sid" ] && { ok=1; break; }
  sleep 3
done
if [ -n "$ok" ]; then
  echo "PASS-hard: dispatch → worker 분사 → marker 전진 (실배선 e2e)"
else
  echo "FAIL: marker 미전진 — worker 미분사/실패 (worker 경로·PROJECTS 해석 회귀 의심)"
  fail=1
fi

if python3 - "$STORE/memory.db" <<'PY'
import sqlite3, sys
try:
    db = sqlite3.connect(sys.argv[1])
    n = db.execute("SELECT count(*) FROM records WHERE body LIKE '%ZEPHYR-77%'").fetchone()[0]
except Exception:
    n = 0
sys.exit(0 if n > 0 else 1)
PY
then
  echo "PASS-soft: 증류 레코드에 대화 표식(ZEPHYR-77) 반영"
else
  echo "WARN: marker 전진했으나 ZEPHYR-77 레코드 없음 — distiller 판단 차 (growing 튜닝 후보)"
fi

rm -rf "$STORE"
exit $fail
