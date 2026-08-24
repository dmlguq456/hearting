#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Pending drain (2026-07-22 plan) — mem doctor stale-pending age ordering +
# mem maintenance --drain-pending (consumed cleanup, pending human gate).
# ABSOLUTE: every case uses isolated MEM_STORE/MEM_PROJECTS/MEM_WRITE_EVENTS (mktemp -d).
# NEVER writes real runtime memory. This suite spawns NO `claude` (ISO-2).
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

BASE_STORE="$(mktemp -d)"; BASE_PROJ="$(mktemp -d)"; WORKDIR="$(mktemp -d)"
trap 'rm -rf "$BASE_STORE" "$BASE_PROJ" "$WORKDIR"' EXIT
export MEM_STORE="$BASE_STORE" MEM_PROJECTS="$BASE_PROJ" MEM_WRITE_EVENTS="$BASE_STORE/write-events.jsonl"
cd "$WORKDIR"   # non-git → project_key = bare enc_cwd (repo-independent)

PKEY="$(PYTHONPATH="$ROOT/tools/memory" python3 -c 'import mem; print(mem.project_key())')"

initdb() { PYTHONPATH="$ROOT/tools/memory" python3 -c 'import mem; mem.get_con().close()' >/dev/null 2>&1; }
sql() { python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.execute("PRAGMA busy_timeout=5000")
cur = con.execute(sys.argv[2], sys.argv[3:] if len(sys.argv) > 3 else [])
rows = cur.fetchall(); con.commit()
for r in rows: print("|".join("" if x is None else str(x) for x in r))
con.close()
PY
}
seed() { # id tier scope type cwd_origin strength last_accessed body [expires] [delivery_state] [created]
  python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys, datetime
db, rid, tier, scope, rtype, cwd, strg, la, body = sys.argv[1:10]
exp = sys.argv[10] if len(sys.argv) > 10 and sys.argv[10] else None
delivery = sys.argv[11] if len(sys.argv) > 11 and sys.argv[11] else "ordinary"
created = sys.argv[12] if len(sys.argv) > 12 and sys.argv[12] else datetime.date.today().isoformat()
con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
today = datetime.date.today().isoformat()
con.execute("INSERT OR REPLACE INTO records(id,tier,scope,type,cwd_origin,created,updated,"
            "expires,source,tags,links,body,strength,last_accessed,injection_flag,delivery_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (rid, tier, scope, rtype, cwd, created, today, exp, None, "[]", "[]",
             body, int(strg), la or today, delivery))
try: con.execute("INSERT INTO records_fts(id, body) VALUES(?,?)", (rid, body))
except Exception: pass
con.commit(); con.close()
PY
}
raw_delete_like() { # pattern — 3-table delete by id LIKE pattern (fixture cleanup only)
  python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.execute("PRAGMA busy_timeout=5000")
pat = sys.argv[2]
ids = [r[0] for r in con.execute("SELECT id FROM records WHERE id LIKE ?", (pat,)).fetchall()]
for rid in ids:
    con.execute("DELETE FROM records WHERE id=?", (rid,))
    try: con.execute("DELETE FROM records_fts WHERE id=?", (rid,))
    except Exception: pass
    try: con.execute("DELETE FROM records_cjk WHERE id=?", (rid,))
    except Exception: pass
con.commit(); con.close()
PY
}
record_count() { sql "SELECT COUNT(*) FROM records"; }
events() { [ -f "$MEM_WRITE_EVENTS" ] && cat "$MEM_WRITE_EVENTS" || true; }
graveyard() { [ -f "$MEM_STORE/deleted-records.jsonl" ] && cat "$MEM_STORE/deleted-records.jsonl" || true; }

initdb
TODAY="$(python3 -c 'import datetime;print(datetime.date.today().isoformat())')"
DAYS_AGO() { python3 -c "import datetime,sys;print((datetime.date.today()-datetime.timedelta(days=int(sys.argv[1]))).isoformat())" "$1"; }

# =====================================================================
echo "== mem doctor: stale-pending 나이순 노출 =="

D40="$(DAYS_AGO 40)"; D30="$(DAYS_AGO 30)"; D25="$(DAYS_AGO 25)"
# 삽입 순서를 뒤섞어도 출력은 created 오름차순(최고령 선두)이어야 한다.
seed dp_mid working project handoff "$PKEY" 1 "$TODAY" "mid age pending" "" pending "$D30"
seed dp_oldest working project handoff "$PKEY" 1 "$TODAY" "oldest pending" "" pending "$D40"
seed dp_newest working project handoff "$PKEY" 1 "$TODAY" "newest of the three" "" pending "$D25"

OUT="$(python3 "$MEM" doctor 2>&1)"
echo "$OUT" | grep -q "\[WARN\] stale-pending" && ok "doctor: stale-pending WARN 존재" \
  || bad "doctor stale-pending WARN 누락: $OUT"
echo "$OUT" | grep -q "oldest $D40" && ok "doctor: oldest {날짜} 표기 확인" \
  || bad "doctor oldest 날짜 표기 누락: $OUT"
LINE="$(echo "$OUT" | grep '\[WARN\] stale-pending')"
FIRST_ID="$(echo "$LINE" | sed -n 's/.*: \([a-zA-Z0-9_-]*\)(.*/\1/p')"
[ "$FIRST_ID" = "dp_oldest" ] && ok "doctor: 최고령 id가 목록 선두" \
  || bad "doctor 나이순 정렬 실패 (선두=$FIRST_ID): $LINE"
echo "$LINE" | grep -qE 'dp_oldest\([0-9?]+d\)' && ok "doctor: ({age}d) 주석 확인" \
  || bad "doctor age(d) 주석 누락: $LINE"
raw_delete_like 'dp_%' >/dev/null

# =====================================================================
echo "== mem doctor: stale pending 0건 → 기존 OK 계약 무회귀 =="

OUT="$(python3 "$MEM" doctor 2>&1)"
echo "$OUT" | grep -q "\[OK\] stale-pending: 0 records" && ok "doctor: stale 0건 → OK 0 records 유지" \
  || bad "doctor stale-pending OK 회귀: $OUT"
python3 "$MEM" doctor >/dev/null 2>&1; RC=$?
[ "$RC" = 0 ] && ok "doctor: clean pending 상태 → exit 0" || bad "doctor exit != 0 (got $RC)"

# =====================================================================
echo "== maintenance --drain-pending: dry-run 비파괴 =="

D22="$(DAYS_AGO 22)"; D26="$(DAYS_AGO 26)"
seed dr_c1 durable project note "$PKEY" 1 "$TODAY" "consumed one body" "" consumed
seed dr_c2 working project handoff "$PKEY" 1 "$TODAY" "consumed two body" "" consumed
seed dr_stale1 working project handoff "$PKEY" 1 "$TODAY" "stale pending one" "" pending "$D26"
seed dr_stale2 working project handoff "$PKEY" 1 "$TODAY" "stale pending two" "" pending "$D22"
seed dr_fresh working project handoff "$PKEY" 1 "$TODAY" "fresh pending body" "" pending "$TODAY"

BEFORE="$(record_count)"
OUT="$(python3 "$MEM" maintenance --drain-pending 2>&1)"
AFTER="$(record_count)"
[ "$BEFORE" = "$AFTER" ] && ok "drain dry-run: 레코드 총계 불변 ($BEFORE)" \
  || bad "drain dry-run 레코드 수 변화: before=$BEFORE after=$AFTER"
[ "$(echo "$OUT" | grep -c '\[consumed\]')" = 2 ] && ok "drain dry-run: [consumed] 2건 표시" \
  || bad "drain dry-run consumed 표시 오류: $OUT"
[ "$(echo "$OUT" | grep -c '\[stale-pending\]')" = 2 ] && ok "drain dry-run: [stale-pending] 2건 표시" \
  || bad "drain dry-run stale-pending 표시 오류: $OUT"
echo "$OUT" | grep -q "dr_fresh" && bad "drain dry-run: fresh pending 이 노출됨" \
  || ok "drain dry-run: fresh pending 미표시"
echo "$OUT" | grep -q "would delete" && ok "drain dry-run: 'would delete' 문구 확인" \
  || bad "drain dry-run would-delete 문구 누락: $OUT"
echo "$OUT" | grep -q "dry-run; use --apply" && ok "drain dry-run: dry-run 안내 문구 확인" \
  || bad "drain dry-run 안내 문구 누락: $OUT"

# =====================================================================
echo "== maintenance --drain-pending --apply: consumed 삭제 + 저널/graveyard =="

: > "$MEM_WRITE_EVENTS"
OUT="$(python3 "$MEM" maintenance --drain-pending --apply 2>&1)"
N_CONSUMED="$(sql "SELECT COUNT(*) FROM records WHERE id IN ('dr_c1','dr_c2')")"
[ "$N_CONSUMED" = 0 ] && ok "drain --apply: consumed 2건 DB에서 소멸" \
  || bad "drain --apply consumed 잔존 (n=$N_CONSUMED): $OUT"
N_FTS="$(sql "SELECT COUNT(*) FROM records_fts WHERE id IN ('dr_c1','dr_c2')")"
[ "$N_FTS" = 0 ] && ok "drain --apply: FTS 행 소멸" || bad "drain --apply FTS 잔존 (n=$N_FTS)"
GRAVE_N="$(graveyard | grep -c '"_action": *"drain-consumed"\|"_action":"drain-consumed"')"
[ "$GRAVE_N" -ge 2 ] 2>/dev/null && ok "graveyard: drain-consumed 2줄 이상" \
  || bad "graveyard drain-consumed 라인 부족 (n=$GRAVE_N)"
EVT_N="$(events | grep -c '"action": *"drain-consumed"\|"action":"drain-consumed"')"
[ "$EVT_N" -ge 2 ] 2>/dev/null && ok "write-events: drain-consumed 2줄 이상" \
  || bad "write-events drain-consumed 라인 부족 (n=$EVT_N)"

# =====================================================================
echo "== maintenance --drain-pending --apply: pending 인간 게이트 =="

N_PENDING="$(sql "SELECT COUNT(*) FROM records WHERE id IN ('dr_stale1','dr_stale2','dr_fresh') AND delivery_state='pending'")"
[ "$N_PENDING" = 3 ] && ok "drain --apply: pending 3건 전건 생존 + delivery_state 불변" \
  || bad "drain --apply pending 소멸/변형 (n=$N_PENDING)"
echo "$OUT" | grep -q "never auto-deleted" && ok "drain --apply: 'never auto-deleted' 문구 확인" \
  || bad "drain --apply human-gate 안내 문구 누락: $OUT"

raw_delete_like 'dr_%' >/dev/null

# =====================================================================
echo "== --pending-stale-days 경계 =="

D6="$(DAYS_AGO 6)"; D4="$(DAYS_AGO 4)"
seed pb_over working project handoff "$PKEY" 1 "$TODAY" "boundary over body" "" pending "$D6"
seed pb_under working project handoff "$PKEY" 1 "$TODAY" "boundary under body" "" pending "$D4"

OUT="$(python3 "$MEM" maintenance --drain-pending --pending-stale-days 5 2>&1)"
echo "$OUT" | grep -q "pb_over" && ok "boundary: 6d(>5) → 후보 포함" \
  || bad "boundary: 6d pending 후보 누락: $OUT"
echo "$OUT" | grep -q "pb_under" && bad "boundary: 4d(<5) 가 후보로 노출됨" \
  || ok "boundary: 4d(<5) → 비후보"

raw_delete_like 'pb_%' >/dev/null

# =====================================================================
echo "== 옵션 없는 maintenance: squash 경로 무회귀 =="

OUT="$(python3 "$MEM" maintenance 2>&1)"; RC=$?
echo "$OUT" | grep -q "store is not a git repository" && ok "maintenance: 비-git store → 기존 메시지 유지" \
  || bad "maintenance squash 경로 회귀: $OUT"
[ "$RC" = 0 ] && ok "maintenance: exit 0" || bad "maintenance exit != 0 (got $RC)"

# =====================================================================
echo "== consumed 레코드: 접속 정규화 후에도 재출현하지 않음 =="

seed cv_handoff working project handoff "$PKEY" 1 "$TODAY" "consumed reappear check" "" consumed
python3 "$MEM" doctor >/dev/null 2>&1
STATE="$(sql "SELECT delivery_state FROM records WHERE id='cv_handoff'")"
[ "$STATE" = "consumed" ] && ok "consumed: 접속 정규화 이후에도 consumed 유지 (ordinary-only 승격 가드)" \
  || bad "consumed 레코드가 재출현/변형됨 (state=$STATE)"
raw_delete_like 'cv_%' >/dev/null

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
