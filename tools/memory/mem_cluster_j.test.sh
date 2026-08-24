#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Cluster J (v15, D-37/D-38/D-39) — write-events 저널 + mem log + mem doctor.
# ABSOLUTE: every case uses isolated MEM_STORE/MEM_PROJECTS/MEM_WRITE_EVENTS (mktemp -d).
# NEVER writes real runtime memory. This suite spawns NO `claude` (ISO-2).
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
APPLIER="$ROOT/tools/memory/apply-distill-actions.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }
run_checked() {
  "$@"
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    bad "command failed ($rc): $*"
    return "$rc"
  fi
  return 0
}

snapshot_path() {
  python3 - "$1" <<'PY'
import hashlib, os, sys
from pathlib import Path

root = Path(sys.argv[1])
h = hashlib.sha256()
if not root.exists() and not root.is_symlink():
    h.update(b"<missing>\0")
elif root.is_symlink():
    h.update(b"L\0" + os.readlink(root).encode() + b"\0")
elif root.is_file():
    h.update(b"F\0" + root.read_bytes() + b"\0")
else:
    entries = [root] + sorted(root.rglob("*"), key=lambda p: str(p))
    for path in entries:
        rel = "." if path == root else str(path.relative_to(root))
        if path.is_symlink():
            value = b"L\0" + os.readlink(path).encode()
        elif path.is_dir():
            value = b"D\0"
        elif path.is_file():
            value = b"F\0" + path.read_bytes()
        else:
            value = b"O\0"
        h.update(rel.encode() + b"\0" + value + b"\0")
print(h.hexdigest())
PY
}

runtime_snapshot() {
  local output="$1"
  local real_home="${HOME}/agent_setting"
  local real_xdg="${XDG_STATE_HOME:-$HOME/.local/state}"
  {
    printf 'runtime-memory '
    snapshot_path "$real_home/memory"
    printf 'runtime-profile '
    snapshot_path "$real_home/user_profile"
    printf 'runtime-journal '
    snapshot_path "$real_xdg/agent-memory/write-events.jsonl"
    printf 'runtime-dump '
    snapshot_path "$real_home/memory/dump.jsonl"
    printf 'worktree-status '
    git -C "$ROOT" status --porcelain=v1 -z | sha256sum | cut -d' ' -f1
    printf 'worktree-unstaged '
    git -C "$ROOT" diff --binary | sha256sum | cut -d' ' -f1
    printf 'worktree-staged '
    git -C "$ROOT" diff --cached --binary | sha256sum | cut -d' ' -f1
  } > "$output"
}

BASE_STORE="$(mktemp -d)"; BASE_PROJ="$(mktemp -d)"; WORKDIR="$(mktemp -d)"
ABSORB_TMP="$(mktemp -d)"
trap 'rm -rf "$BASE_STORE" "$BASE_PROJ" "$WORKDIR" "$ABSORB_TMP"' EXIT
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
            "expires,source,tags,links,body,strength,last_accessed,injection_flag,delivery_state,"
            "headline,status,canonical_id,capsule_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (rid, tier, scope, rtype, cwd, created, today, exp, None, "[]", "[]",
             body, int(strg), la or today, delivery, body, "active", rid, 1))
try: con.execute("INSERT INTO records_fts(id, body) VALUES(?,?)", (rid, body))
except Exception: pass
try: con.execute("INSERT INTO records_capsule_fts(id,headline,aliases,entities,topics,artifact_refs,canonical_id) VALUES(?,?,?,?,?,?,?)", (rid, body, "", "", "", "", rid))
except Exception: pass
con.commit(); con.close()
PY
}
raw_delete() { # id — 3-table delete (records+fts+trig), 테스트 fixture cleanup 전용
  python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.execute("PRAGMA busy_timeout=5000")
rid = sys.argv[2]
con.execute("DELETE FROM records WHERE id=?", (rid,))
try: con.execute("DELETE FROM records_fts WHERE id=?", (rid,))
except Exception: pass
try: con.execute("DELETE FROM records_trig WHERE id=?", (rid,))
except Exception: pass
try: con.execute("DELETE FROM records_capsule_fts WHERE id=?", (rid,))
except Exception: pass
try: con.execute("DELETE FROM record_topics WHERE record_id=?", (rid,))
except Exception: pass
con.commit(); con.close()
PY
}
raw_delete_like() { # pattern — 3-table delete by id LIKE pattern
  python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); con.execute("PRAGMA busy_timeout=5000")
pat = sys.argv[2]
ids = [r[0] for r in con.execute("SELECT id FROM records WHERE id LIKE ?", (pat,)).fetchall()]
for rid in ids:
    con.execute("DELETE FROM records WHERE id=?", (rid,))
    try: con.execute("DELETE FROM records_fts WHERE id=?", (rid,))
    except Exception: pass
    try: con.execute("DELETE FROM records_trig WHERE id=?", (rid,))
    except Exception: pass
    try: con.execute("DELETE FROM records_capsule_fts WHERE id=?", (rid,))
    except Exception: pass
    try: con.execute("DELETE FROM record_topics WHERE record_id=?", (rid,))
    except Exception: pass
con.commit(); con.close()
PY
}
events() { [ -f "$MEM_WRITE_EVENTS" ] && cat "$MEM_WRITE_EVENTS" || true; }
event_count() { events | grep -c . || true; }
last_event_field() { # field
  events | tail -1 | python3 -c "import json,sys; print(json.load(sys.stdin).get(sys.argv[1],''))" "$1"
}

initdb
TODAY="$(python3 -c 'import datetime;print(datetime.date.today().isoformat())')"

# =====================================================================
echo "== D-37: 전 변이 경로 저널 append =="

: > "$MEM_WRITE_EVENTS"
python3 "$MEM" add working thread "journal add body one" >/dev/null
[ "$(last_event_field action)" = add ] && [ "$(last_event_field actor)" = manual ] \
  && ok "add → action=add actor=manual" || bad "add journal mismatch: $(events | tail -1)"

python3 "$MEM" note "journal note body two" >/dev/null
[ "$(last_event_field action)" = note ] && ok "note → action=note" || bad "note journal mismatch"

seed j_consume working project handoff "$PKEY" 1 "$TODAY" "consume me body" "" pending
python3 "$MEM" consume j_consume >/dev/null
[ "$(last_event_field action)" = consume ] && [ "$(last_event_field id)" = j_consume ] \
  && ok "consume → action=consume" || bad "consume journal mismatch: $(events | tail -1)"

seed j_reinforce durable project lesson "$PKEY" 1 "$TODAY" "reinforce me body"
python3 "$MEM" reinforce j_reinforce >/dev/null
[ "$(last_event_field action)" = reinforce ] && ok "reinforce → action=reinforce" || bad "reinforce journal mismatch"

seed j_prune durable project note "$PKEY" 1 "$TODAY" "prune me body"
python3 "$MEM" prune j_prune >/dev/null
[ "$(last_event_field action)" = prune ] && ok "prune → action=prune" || bad "prune journal mismatch"

seed j_mc durable project lesson "$PKEY" 1 "$TODAY" "merge canonical body"
seed j_mb durable project lesson "$PKEY" 1 "$TODAY" "merge fold-in body"
python3 "$MEM" merge --canonical j_mc j_mc j_mb >/dev/null
[ "$(last_event_field action)" = merge ] && [ "$(last_event_field id)" = j_mc ] \
  && ok "merge → action=merge id=canonical" || bad "merge journal mismatch: $(events | tail -1)"

seed j_grad working project thread "$PKEY" 1 "$TODAY" "graduate me body"
python3 "$MEM" graduate j_grad >/dev/null
[ "$(last_event_field action)" = graduate ] && ok "graduate → action=graduate" || bad "graduate journal mismatch"

seed j_ra working project thread "-orphan-dir-nonexistent" 1 "$TODAY" "reattribute me body"
python3 "$MEM" reattribute j_ra >/dev/null
[ "$(last_event_field action)" = reattribute ] && ok "reattribute → action=reattribute" || bad "reattribute journal mismatch"

seed j_del durable project note "$PKEY" 1 "$TODAY" "delete me body"
python3 "$MEM" delete j_del >/dev/null
[ "$(last_event_field action)" = delete ] && ok "delete → action=delete" || bad "delete journal mismatch"

python3 "$MEM" restore j_del >/dev/null
[ "$(last_event_field action)" = restore ] && [ "$(last_event_field actor)" = restore ] \
  && ok "restore → action=restore actor=restore" || bad "restore journal mismatch: $(events | tail -1)"

OLD="$(python3 -c 'import datetime;print((datetime.date.today()-datetime.timedelta(days=5)).isoformat())')"
seed j_exp working project thread "$PKEY" 1 "$TODAY" "expire me body" "$OLD"
python3 "$MEM" lifecycle --apply >/dev/null
[ "$(last_event_field action)" = lifecycle-expire ] && [ "$(last_event_field actor)" = lifecycle ] \
  && ok "lifecycle --apply → action=lifecycle-expire actor=lifecycle" \
  || bad "lifecycle journal mismatch: $(events | tail -1)"

N_ACTIONS=11
[ "$(event_count)" -ge "$N_ACTIONS" ] && ok "저널 라인수 >= $N_ACTIONS (전 변이 경로 커버)" \
  || bad "저널 라인수 부족: $(event_count)"

# =====================================================================
echo "== D-37: actor 결정론 (env MEM_DISTILL / MEM_ACTOR) =="

: > "$MEM_WRITE_EVENTS"
MEM_DISTILL=1 python3 "$MEM" add working thread "distiller-origin body" >/dev/null
[ "$(last_event_field actor)" = distiller ] && ok "MEM_DISTILL=1 → actor=distiller" \
  || bad "distiller actor mismatch: $(last_event_field actor)"

seed j_curate durable project note "$PKEY" 1 "$TODAY" "curator prune target"
MEM_DISTILL=1 MEM_ACTOR=curator python3 "$MEM" prune j_curate >/dev/null
[ "$(last_event_field actor)" = curator ] \
  && ok "MEM_ACTOR=curator overrides MEM_DISTILL=1 → actor=curator" \
  || bad "curator override failed: $(last_event_field actor)"

# apply-distill-actions.py --mode curate wires MEM_ACTOR=curator itself
seed j_curate2 durable project note "$PKEY" 1 "$TODAY" "applier curator target"
OUT="$WORKDIR/actions.jsonl"; SNAP="$WORKDIR/snap_ids.txt"
printf '{"action":"prune","id":"j_curate2"}\n' > "$OUT"
printf 'j_curate2\n' > "$SNAP"
: > "$MEM_WRITE_EVENTS"
MEM_DISTILL=1 python3 "$APPLIER" "$OUT" "$MEM" --mode curate --snapshot-ids "$SNAP" >/dev/null
[ "$(last_event_field actor)" = curator ] \
  && ok "apply-distill-actions.py --mode curate → actor=curator (env wiring)" \
  || bad "applier curator wiring failed: $(events | tail -1)"

# =====================================================================
echo "== D-37: fail-open (저널 write 실패가 mutation을 막지 않음) =="

BAD_DIR="$(mktemp -d)"; chmod 000 "$BAD_DIR"
MEM_WRITE_EVENTS="$BAD_DIR/nested/write-events.jsonl" python3 "$MEM" add working thread "fail-open body should still write" >/dev/null
COUNT="$(sql "SELECT COUNT(*) FROM records WHERE body LIKE 'fail-open body%'")"
[ "$COUNT" = 1 ] && ok "저널 append 실패해도 DB write 성공 (fail-open)" \
  || bad "fail-open 위반: mutation 이 저널 실패에 막힘 (count=$COUNT)"
chmod 755 "$BAD_DIR"; rm -rf "$BAD_DIR"

# =====================================================================
echo "== D-37: rotation (256KB/500줄 bound) =="

: > "$MEM_WRITE_EVENTS"
python3 - "$MEM_WRITE_EVENTS" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, "w", encoding="utf-8") as f:
    for i in range(3000):
        f.write(json.dumps({"ts": "t", "action": "add", "id": f"pad_{i}", "tier": "working",
                             "scope": "project", "type": "note", "actor": "manual", "sid": "",
                             "snippet": "x" * 100}) + "\n")
PY
SZ_BEFORE="$(wc -c < "$MEM_WRITE_EVENTS")"
[ "$SZ_BEFORE" -gt 262144 ] || bad "rotation fixture setup: pre-size too small ($SZ_BEFORE)"
python3 "$MEM" add working thread "rotation trigger body" >/dev/null
LINES_AFTER="$(event_count)"
[ "$LINES_AFTER" -le 501 ] && ok "rotation: append 후 줄수 <= 501 (최근 500 + 신규1), got $LINES_AFTER" \
  || bad "rotation 미작동: 줄수=$LINES_AFTER"
[ "$(last_event_field id)" != "" ] && [ "$(events | tail -1 | grep -c 'rotation trigger')" = 1 ] \
  && ok "rotation: 최신 이벤트 보존" || bad "rotation: 최신 이벤트 유실"

# =====================================================================
echo "== D-38: mem log 필터 =="

: > "$MEM_WRITE_EVENTS"
python3 "$MEM" add working thread "log-fixture-one-body" >/dev/null
seed j_log2 durable project note "$PKEY" 1 "$TODAY" "log-fixture-two-body"
python3 "$MEM" prune j_log2 >/dev/null
python3 "$MEM" add durable thread "log-fixture-three-body" >/dev/null

OUT="$(python3 "$MEM" log --limit 20)"
echo "$OUT" | grep -q "log-fixture-three-body" && ok "mem log: 기본 출력에 최근 이벤트 포함" \
  || bad "mem log 기본 출력 누락"

OUT_ACTION="$(python3 "$MEM" log --action prune)"
LC="$(echo "$OUT_ACTION" | grep -c '  prune')"
[ "$LC" = 1 ] && ok "mem log --action prune → prune 1건만" || bad "mem log --action 필터 실패 (n=$LC)"

OUT_JSON="$(python3 "$MEM" log --json --limit 5)"
python3 -c "import json,sys; d=json.loads(sys.argv[1]); assert 'events' in d and isinstance(d['events'], list)" "$OUT_JSON" \
  && ok "mem log --json → 파싱 가능한 events 배열" || bad "mem log --json 형식 오류"

OUT_LIMIT="$(python3 "$MEM" log --json --limit 1)"
N="$(python3 -c "import json,sys; print(len(json.loads(sys.argv[1])['events']))" "$OUT_LIMIT")"
[ "$N" = 1 ] && ok "mem log --limit 1 → 1건" || bad "mem log --limit 무시됨 (n=$N)"

# =====================================================================
echo "== D-39: mem doctor — clean store → exit 0 =="

: > "$MEM_WRITE_EVENTS"; rm -f "$MEM_STORE/deleted-records.jsonl" "$MEM_STORE/dump.jsonl"
python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
con = mem.get_con()
con.execute('DELETE FROM records')
try: con.execute('DELETE FROM records_fts')
except Exception: pass
try: con.execute('DELETE FROM records_trig')
except Exception: pass
try: con.execute('DELETE FROM records_capsule_fts')
except Exception: pass
try: con.execute('DELETE FROM record_topics')
except Exception: pass
con.commit(); con.close()
"
MEM_DISTILL=1 python3 "$MEM" add working thread "clean doctor fixture body" >/dev/null
python3 "$MEM" export --target dump >/dev/null
python3 "$MEM" doctor >/tmp/doctor_clean.out; RC=$?
[ "$RC" = 0 ] && ok "doctor: clean store → exit 0" || bad "doctor clean rc=$RC: $(cat /tmp/doctor_clean.out)"
grep -q '\[FAIL\]' /tmp/doctor_clean.out && bad "doctor clean: FAIL 항목 존재해선 안 됨" \
  || ok "doctor clean: FAIL 항목 없음"

# =====================================================================
echo "== D-39: mem doctor — 위반 fixture 별 WARN/FAIL =="

# ③ schema invariant violation — pending 아닌 working 인데 expires NULL
seed viol_expires working project thread "$PKEY" 1 "$TODAY" "missing expires body" "" ordinary
sql "UPDATE records SET expires=NULL WHERE id='viol_expires'" >/dev/null
python3 "$MEM" doctor >/tmp/doctor_schema.out; RC=$?
grep -q '\[FAIL\] schema-invariants' /tmp/doctor_schema.out \
  && ok "doctor: non-pending working NULL expires → schema-invariants FAIL" \
  || bad "doctor schema-invariants 미검출: $(cat /tmp/doctor_schema.out)"
[ "$RC" = 2 ] && ok "doctor: FAIL 존재 → exit 2" || bad "doctor exit code != 2 (got $RC)"
raw_delete viol_expires >/dev/null

# ⑤ stale pending
OLD22="$(python3 -c 'import datetime;print((datetime.date.today()-datetime.timedelta(days=25)).isoformat())')"
seed viol_pending working project handoff "$PKEY" 1 "$TODAY" "stale pending body" "" pending "$OLD22"
python3 "$MEM" doctor >/tmp/doctor_pending.out; RC=$?
grep -q '\[WARN\] stale-pending' /tmp/doctor_pending.out \
  && ok "doctor: 21일+ pending 미소비 → stale-pending WARN" \
  || bad "doctor stale-pending 미검출: $(cat /tmp/doctor_pending.out)"
raw_delete viol_pending >/dev/null

# ④ working bloat
for i in $(seq 1 151); do seed "bloat_$i" working project thread "$PKEY" 1 "$TODAY" "bloat body $i" >/dev/null; done
python3 "$MEM" doctor >/tmp/doctor_bloat.out
grep -q '\[WARN\] working-bloat' /tmp/doctor_bloat.out \
  && ok "doctor: working 151건 > ceiling → working-bloat WARN" \
  || bad "doctor working-bloat 미검출: $(cat /tmp/doctor_bloat.out)"
raw_delete_like 'bloat_%' >/dev/null

# ⑥ durable soft-ceiling
for i in $(seq 1 81); do seed "dur_$i" durable project lesson "$PKEY" 1 "$TODAY" "durable body $i" >/dev/null; done
python3 "$MEM" doctor >/tmp/doctor_durable.out
grep -q '\[WARN\] durable-ceiling' /tmp/doctor_durable.out \
  && ok "doctor: durable 81건 > soft-ceiling(80) → durable-ceiling WARN" \
  || bad "doctor durable-ceiling 미검출: $(cat /tmp/doctor_durable.out)"
raw_delete_like 'dur_%' >/dev/null

# ⑦ graveyard parity — graveyard 에 있는 id 가 DB 에도 생존
seed viol_grave durable project note "$PKEY" 1 "$TODAY" "graveyard-parity body"
python3 -c "
import sys, json, datetime; sys.path.insert(0, '$ROOT/tools/memory')
import mem
with open(mem.GRAVEYARD, 'a', encoding='utf-8') as f:
    rec = {c: None for c in mem.RECORD_COLS}
    rec.update(id='viol_grave', tier='durable', scope='project', type='note', tags=[], links=[])
    rec['_deleted_at'] = 'x'; rec['_action'] = 'prune'; rec['_canonical'] = None
    f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + '\n')
"
python3 "$MEM" doctor >/tmp/doctor_grave.out
grep -q '\[WARN\] graveyard-parity' /tmp/doctor_grave.out \
  && ok "doctor: graveyard id 가 DB 에 생존 → graveyard-parity WARN" \
  || bad "doctor graveyard-parity 미검출: $(cat /tmp/doctor_grave.out)"
raw_delete viol_grave >/dev/null

# ⑧ dump freshness — DB updated 가 dump.jsonl 의 max(updated) 보다 최신
python3 "$MEM" export --target dump >/dev/null
FUTURE="$(python3 -c 'import datetime;print((datetime.date.today()+datetime.timedelta(days=1)).isoformat())')"
seed viol_dump durable project note "$PKEY" 1 "$TODAY" "dump-fresh body"
sql "UPDATE records SET updated='$FUTURE' WHERE id='viol_dump'" >/dev/null
python3 "$MEM" doctor >/tmp/doctor_dump.out
grep -q '\[WARN\] dump-freshness' /tmp/doctor_dump.out \
  && ok "doctor: DB 최신 updated 가 dump 보다 최신 → dump-freshness WARN" \
  || bad "doctor dump-freshness 미검출: $(cat /tmp/doctor_dump.out)"
raw_delete viol_dump >/dev/null

# ⑨ worker health — 활성 project 인데 저널에 distiller/curator 무소식
: > "$MEM_WRITE_EVENTS"
seed viol_worker working project thread "$PKEY" 1 "$TODAY" "worker-health body"
python3 "$MEM" doctor >/tmp/doctor_worker.out
grep -q '\[WARN\] worker-health' /tmp/doctor_worker.out \
  && ok "doctor: 활성 프로젝트 + 저널 무소식 → worker-health WARN" \
  || bad "doctor worker-health 미검출: $(cat /tmp/doctor_worker.out)"

# ⑩ 저널 경로 격리 — MEM_STORE override + MEM_WRITE_EVENTS 미설정 → 저널은 그 store 옆으로
# (fixture DB 테스트가 실 XDG 저널을 오염시키지 않는 계약 — 2026-07-11 실유출 회귀 고정)
ISO_STORE="$(mktemp -d)"
env -u MEM_WRITE_EVENTS MEM_STORE="$ISO_STORE" MEM_PROJECTS="$BASE_PROJ" \
  python3 "$MEM" add working hint "journal isolation probe" >/dev/null 2>&1
if [ -f "$ISO_STORE/write-events.jsonl" ] && grep -q "journal isolation probe" "$ISO_STORE/write-events.jsonl"; then
  ok "저널 격리: MEM_STORE override 시 write-events 가 store 옆에 생성"
else
  bad "저널 격리 실패: $ISO_STORE/write-events.jsonl 부재 또는 미기록"
fi
rm -rf "$ISO_STORE"

# =====================================================================
echo "== D-37: sync/migrate prospective absorption attribution =="

# Manual callers retain ambient cwd fallback and all three journal branches.
MAN_STORE="$ABSORB_TMP/manual-store"; MAN_PROJ="$ABSORB_TMP/manual-projects"
MAN_PROFILE="$ABSORB_TMP/manual-profile"; MAN_PROC="$ABSORB_TMP/manual-process"
MAN_ENV="$ABSORB_TMP/manual-env"
mkdir -p "$MAN_PROJ" "$MAN_PROFILE" "$MAN_PROC" "$MAN_ENV"
MAN_JOURNAL="$MAN_STORE/write-events.jsonl"
(
  cd "$MAN_PROC"
  MEM_STORE="$MAN_STORE" MEM_PROJECTS="$MAN_PROJ" MEM_PROFILE="$MAN_PROFILE" \
  MEM_WRITE_EVENTS="$MAN_JOURNAL" MEM_CWD="$MAN_ENV" \
    python3 "$MEM" add durable lesson "manual source initial body" --source manual-source >/dev/null
  MEM_STORE="$MAN_STORE" MEM_PROJECTS="$MAN_PROJ" MEM_PROFILE="$MAN_PROFILE" \
  MEM_WRITE_EVENTS="$MAN_JOURNAL" MEM_CWD="$MAN_ENV" \
    python3 "$MEM" add durable lesson "manual source updated body" --source manual-source >/dev/null
  env -u MEM_CWD MEM_STORE="$MAN_STORE" MEM_PROJECTS="$MAN_PROJ" MEM_PROFILE="$MAN_PROFILE" \
  MEM_WRITE_EVENTS="$MAN_JOURNAL" python3 "$MEM" add durable lesson \
    "manual process fallback body" >/dev/null
  MEM_STORE="$MAN_STORE" MEM_PROJECTS="$MAN_PROJ" MEM_PROFILE="$MAN_PROFILE" \
  MEM_WRITE_EVENTS="$MAN_JOURNAL" MEM_CWD="$MAN_ENV" \
    python3 "$MEM" add durable lesson "manual source initial body" --source manual-dedup >/dev/null
)
if python3 - "$MAN_JOURNAL" "$MAN_ENV" "$MAN_PROC" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert len(events) == 4, events
assert events[0]["cwd"] == sys.argv[2], events[0]
assert events[1]["id"] == events[0]["id"] and events[1]["action"] == "add", events
assert events[2]["cwd"] == sys.argv[3], events[2]
assert events[3]["id"] == events[0]["id"], events
assert all(event["actor"] == "manual" for event in events), events
print("manual ambient fallback/upsert/dedup assertions: 4")
PY
then
  ok "manual add retains MEM_CWD/process fallback and upsert/dedup events"
else
  bad "manual ambient fallback/upsert/dedup assertions failed"
fi

# Hostile ambient values cannot override the logical cwd or literal sync actor.
AUTO_STORE="$ABSORB_TMP/auto-store"; AUTO_PROJECTS="$ABSORB_TMP/auto-projects"
AUTO_PROFILE="$ABSORB_TMP/auto-profile"; AUTO_ROOT="$ABSORB_TMP/auto-logical"
AUTO_WRONG="$ABSORB_TMP/auto-wrong"; mkdir -p "$AUTO_PROFILE" "$AUTO_ROOT" "$AUTO_WRONG"
AUTO_ENC="$(PYTHONPATH="$ROOT/tools/memory" python3 - "$AUTO_ROOT" <<'PY'
import sys
import mem
print(mem.enc_cwd(sys.argv[1]))
PY
)"
mkdir -p "$AUTO_PROJECTS/$AUTO_ENC/memory"
printf '%s\n' '---' 'type: lesson' '---' \
  'auto-memory hostile environment absorption body is valid' \
  > "$AUTO_PROJECTS/$AUTO_ENC/memory/hostile.md"
AUTO_JOURNAL="$AUTO_STORE/write-events.jsonl"
(
  cd "$AUTO_WRONG"
  run_checked env MEM_STORE="$AUTO_STORE" MEM_PROJECTS="$AUTO_PROJECTS" MEM_PROFILE="$AUTO_PROFILE" \
    MEM_WRITE_EVENTS="$AUTO_JOURNAL" MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" migrate --apply --all-projects >/dev/null
)
if [ "$?" = 0 ] && python3 - "$AUTO_JOURNAL" "$AUTO_ROOT" <<'PY'
import json, sys
events = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
assert len(events) == 1, events
event = events[0]
assert event["action"] == "add" and event["actor"] == "sync", event
assert event["cwd"] == sys.argv[2], event
print("hostile auto-memory assertions: 3")
PY
then
  ok "hostile auto-memory emits exactly one literal-sync event with logical cwd"
else
  bad "hostile auto-memory assertions failed"
fi

AUTO_ID="$(python3 - "$AUTO_STORE/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT id FROM records WHERE source='auto-memory:' || ?", (sys.argv[1].split('/')[-1] + '/hostile.md',)).fetchone()
if row is None:
    row = con.execute("SELECT id FROM records WHERE source LIKE 'auto-memory:%/hostile.md'").fetchone()
if row is None:
    raise SystemExit("auto-memory absorption row missing")
print(row[0])
con.close()
PY
)" || { bad "auto-memory absorption id lookup failed"; AUTO_ID=""; }
AUTO_MANUAL_OUT="$ABSORB_TMP/auto-manual.out"
if run_checked env MEM_STORE="$AUTO_STORE" MEM_PROJECTS="$AUTO_PROJECTS" MEM_PROFILE="$AUTO_PROFILE" \
  MEM_WRITE_EVENTS="$AUTO_JOURNAL" MEM_CWD="/wrong/repo" MEM_ACTOR=manual \
  python3 "$MEM" add durable lesson "mixed manual journal event" --source mixed-manual-log \
  >"$AUTO_MANUAL_OUT"; then
  AUTO_MANUAL_ID="$(python3 - "$AUTO_STORE/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT id FROM records WHERE source='mixed-manual-log'").fetchone()
if row is None:
    raise SystemExit("manual mixed-journal row missing")
print(row[0])
con.close()
PY
  )" || { bad "mixed manual journal id lookup failed"; AUTO_MANUAL_ID=""; }
  AUTO_LOG_OUT="$ABSORB_TMP/auto-sync-log.json"
  if run_checked env MEM_STORE="$AUTO_STORE" MEM_PROJECTS="$AUTO_PROJECTS" MEM_PROFILE="$AUTO_PROFILE" \
    MEM_WRITE_EVENTS="$AUTO_JOURNAL" python3 "$MEM" log --json --actor sync \
    >"$AUTO_LOG_OUT"; then
    if python3 - "$AUTO_LOG_OUT" "$AUTO_ID" "$AUTO_MANUAL_ID" <<'PY'
import json, sys
payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
events = payload["events"]
expected = {sys.argv[2]}
manual_id = sys.argv[3]
assert payload["count"] == len(expected), payload
assert {event["id"] for event in events} == expected, events
assert all(event["action"] == "add" and event["actor"] == "sync" for event in events), events
assert manual_id not in {event["id"] for event in events}, events
print("public sync actor filter assertions: 4")
PY
    then
      ok "mem log --json --actor sync returns only the absorption ID"
    else
      bad "mem log --json --actor sync returned an unexpected mixed-journal set"
    fi
  fi
fi
AUTO_DB_BEFORE="$(python3 - "$AUTO_STORE/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print("\n".join("|".join("" if value is None else str(value) for value in row)
                for row in con.execute("SELECT id, source, strength FROM records ORDER BY id")))
con.close()
PY
)"
AUTO_LINES="$(wc -l < "$AUTO_JOURNAL")"
AUTO_REPEAT_OK=0
if (
  cd "$AUTO_WRONG"
  run_checked env MEM_STORE="$AUTO_STORE" MEM_PROJECTS="$AUTO_PROJECTS" MEM_PROFILE="$AUTO_PROFILE" \
    MEM_WRITE_EVENTS="$AUTO_JOURNAL" MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" migrate --apply --all-projects >/dev/null
); then
  AUTO_REPEAT_OK=1
else
  bad "repeat migrate command did not complete"
fi
AUTO_DB_AFTER="$(python3 - "$AUTO_STORE/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
print("\n".join("|".join("" if value is None else str(value) for value in row)
                for row in con.execute("SELECT id, source, strength FROM records ORDER BY id")))
con.close()
PY
)"
[ "$AUTO_REPEAT_OK" = 1 ] && [ -f "$AUTO_JOURNAL" ] \
  && [ "$(wc -l < "$AUTO_JOURNAL")" = "$AUTO_LINES" ] \
  && [ "$AUTO_DB_AFTER" = "$AUTO_DB_BEFORE" ] \
  && ok "repeat migrate preserves DB id/source/strength and journal count" \
  || bad "repeat migrate changed DB snapshot or journal line count"

# A fenced sync owns a newly-created, empty, non-Git store.  It snapshots every
# real runtime target and the worktree before/after, then checks the isolated
# dump and both SQLite indexes; no ambient runtime path is a valid fixture.
SYNC_STORE="$(mktemp -d "$ABSORB_TMP/sync-store.XXXXXX")"
SYNC_PROJECTS="$(mktemp -d "$ABSORB_TMP/sync-projects.XXXXXX")"
SYNC_PROFILE="$(mktemp -d "$ABSORB_TMP/sync-profile.XXXXXX")"
SYNC_ROOT="$(mktemp -d "$ABSORB_TMP/sync-root.XXXXXX")"
SYNC_WRONG="$(mktemp -d "$ABSORB_TMP/sync-wrong.XXXXXX")"
SYNC_JOURNAL="$SYNC_STORE/write-events.jsonl"
if [ -n "$(find "$SYNC_STORE" -mindepth 1 -print -quit)" ]; then
  bad "fenced sync store was not empty at allocation"
fi
if git -C "$SYNC_STORE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  bad "fenced sync store unexpectedly belongs to a Git worktree"
else
  ok "fenced sync store is fresh and non-Git"
fi
SYNC_ENC="$(PYTHONPATH="$ROOT/tools/memory" python3 - "$SYNC_ROOT" <<'PY'
import sys
import mem
print(mem.enc_cwd(sys.argv[1]))
PY
)"
mkdir -p "$SYNC_PROJECTS/$SYNC_ENC/memory"
printf '%s\n' '---' 'type: lesson' '---' \
  'fenced sync isolated export and index proof' \
  > "$SYNC_PROJECTS/$SYNC_ENC/memory/fenced.md"
SYNC_BEFORE="$ABSORB_TMP/sync-before"; SYNC_AFTER="$ABSORB_TMP/sync-after"
runtime_snapshot "$SYNC_BEFORE"
if (
  cd "$SYNC_ROOT"
  run_checked env MEM_STORE="$SYNC_STORE" MEM_PROJECTS="$SYNC_PROJECTS" MEM_PROFILE="$SYNC_PROFILE" \
    MEM_WRITE_EVENTS="$SYNC_JOURNAL" MEM_DUMP_COMMIT=0 MEM_DUMP_PUSH=0 \
    MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" sync >/dev/null
); then
  runtime_snapshot "$SYNC_AFTER"
  if cmp -s "$SYNC_BEFORE" "$SYNC_AFTER"; then
    printf 'fenced sync snapshot digests before=%s after=%s\n' \
      "$(sha256sum "$SYNC_BEFORE" | cut -d' ' -f1)" \
      "$(sha256sum "$SYNC_AFTER" | cut -d' ' -f1)"
    ok "fenced sync leaves real memory/profile/journal/dump/worktree snapshots unchanged"
  else
    bad "fenced sync changed a real runtime target or worktree state"
  fi
  if python3 - "$SYNC_STORE/memory.db" "$SYNC_STORE/dump.jsonl" <<'PY'
import json, sqlite3, sys
db, dump = sys.argv[1:]
con = sqlite3.connect(db)
assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
record_ids = {row[0] for row in con.execute("SELECT id FROM records")}
fts_ids = {row[0] for row in con.execute("SELECT id FROM records_fts")}
assert record_ids == fts_ids, (record_ids, fts_ids)
dump_ids = {json.loads(line)["id"] for line in open(dump, encoding="utf-8") if line.strip()}
assert dump_ids == record_ids and dump_ids, (dump_ids, record_ids)
print(f"fenced sync isolated DB/FTS/dump assertions: {len(record_ids)} record")
con.close()
PY
  then
    ok "fenced sync isolated dump and SQLite index mirrors are consistent"
  else
    bad "fenced sync isolated dump or SQLite index mirrors are inconsistent"
  fi
fi
# Two distinct post-it source keys with the same normalized body yield one INSERT event;
# the Fleet collector consumes that real producer row under an sample-note-like repo key.
POST_STORE="$ABSORB_TMP/post-store"; POST_PROJECTS="$ABSORB_TMP/post-projects"
POST_PROFILE="$ABSORB_TMP/post-profile"; POST_ROOT="$ABSORB_TMP/sample-note"
POST_WRONG="$ABSORB_TMP/post-wrong"; mkdir -p "$POST_PROJECTS" "$POST_PROFILE" \
  "$POST_ROOT/.agent_reports" "$POST_WRONG"
printf '%s\n' '## Decisions' \
  '- post-it normalized duplicate body for absorption event proof' \
  '- post-it  normalized duplicate body for absorption event proof' \
  > "$POST_ROOT/.agent_reports/post-it.md"
mkdir -p "$POST_STORE"
printf '%s\n' "$POST_ROOT/.agent_reports/post-it.md" > "$POST_STORE/.postit-roots"
POST_JOURNAL="$POST_STORE/write-events.jsonl"
POST_OK=0
if (
  cd "$POST_WRONG"
  run_checked env MEM_STORE="$POST_STORE" MEM_PROJECTS="$POST_PROJECTS" MEM_PROFILE="$POST_PROFILE" \
    MEM_WRITE_EVENTS="$POST_JOURNAL" MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" migrate --apply --all-projects >/dev/null
); then
  POST_OK=1
fi
if [ "$POST_OK" = 1 ] && python3 - "$POST_STORE/memory.db" "$POST_JOURNAL" "$POST_ROOT" <<'PY'
import json, sqlite3, sys
db, journal, root = sys.argv[1:]
events = [json.loads(line) for line in open(journal, encoding="utf-8") if line.strip()]
assert len(events) == 1, events
event = events[0]
assert event["action"] == "add" and event["actor"] == "sync", event
assert event["cwd"] == root, event
con = sqlite3.connect(db)
row = con.execute("SELECT strength, COUNT(*) OVER() FROM records WHERE source LIKE 'post-it:%'").fetchone()
con.close()
assert row == (2, 1), row
print("post-it insert/dedup assertions: 4")
PY
then
  ok "post-it INSERT/dedup event assertions pass"
else
  bad "post-it INSERT/dedup event assertions failed"
fi
if PYTHONPATH="$ROOT/tools" MEM_STORE="$POST_STORE" MEM_WRITE_EVENTS="$POST_JOURNAL" \
  python3 - <<'PY'
from fleet.collectors.memory import collect
result = collect()
rows = result["by_repo"].get("sample-note", [])
assert len(rows) == 1 and rows[0]["action"] == "add" and rows[0]["actor"] == "sync", result
print("Fleet sample-note grouping assertions: 1")
PY
then
  ok "post-it insert-only event groups in Fleet under sample-note"
else
  bad "Fleet sample-note grouping assertion failed"
fi

# Existing source plus a sentinel journal row proves prospective-only/no-backfill.
NB_STORE="$ABSORB_TMP/no-backfill-store"; NB_PROJECTS="$ABSORB_TMP/no-backfill-projects"
NB_PROFILE="$ABSORB_TMP/no-backfill-profile"; NB_ROOT="$ABSORB_TMP/no-backfill-root"
NB_WRONG="$ABSORB_TMP/no-backfill-wrong"; mkdir -p "$NB_PROJECTS" "$NB_PROFILE" "$NB_ROOT" "$NB_WRONG"
NB_ENC="$(PYTHONPATH="$ROOT/tools/memory" python3 - "$NB_ROOT" <<'PY'
import sys
import mem
print(mem.enc_cwd(sys.argv[1]))
PY
)"
mkdir -p "$NB_PROJECTS/$NB_ENC/memory"
printf '%s\n' '---' 'type: lesson' '---' \
  'preexisting source must never be backfilled into the journal' \
  > "$NB_PROJECTS/$NB_ENC/memory/no-backfill.md"
NB_JOURNAL="$NB_STORE/write-events.jsonl"; NB_SOURCE="auto-memory:$NB_ENC/no-backfill.md"
MEM_STORE="$NB_STORE" MEM_PROJECTS="$NB_PROJECTS" MEM_PROFILE="$NB_PROFILE" \
  MEM_WRITE_EVENTS="$NB_JOURNAL" PYTHONPATH="$ROOT/tools/memory" \
  python3 -c 'import mem; mem.get_con().close()' >/dev/null
NB_CWD="$(PYTHONPATH="$ROOT/tools/memory" python3 - "$NB_ROOT" <<'PY'
import sys
import mem
print(mem.project_key(sys.argv[1]))
PY
)"
MEM_STORE="$NB_STORE" seed no_backfill durable project lesson "$NB_CWD" 1 "$TODAY" \
  "preexisting source must never be backfilled into the journal"
python3 - "$NB_STORE/memory.db" "$NB_SOURCE" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("UPDATE records SET source=? WHERE id='no_backfill'", (sys.argv[2],))
con.commit(); con.close()
PY
mkdir -p "$NB_STORE"
printf '%s\n' '{"sentinel":"no-backfill"}' > "$NB_JOURNAL"
if (
  cd "$NB_WRONG"
  run_checked env MEM_STORE="$NB_STORE" MEM_PROJECTS="$NB_PROJECTS" MEM_PROFILE="$NB_PROFILE" \
    MEM_WRITE_EVENTS="$NB_JOURNAL" MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" migrate --apply --all-projects >/dev/null
); then
  NB_MIGRATE_OK=1
else
  NB_MIGRATE_OK=0
fi
if [ "$NB_MIGRATE_OK" = 1 ] && [ -f "$NB_JOURNAL" ] && [ "$(cat "$NB_JOURNAL")" = '{"sentinel":"no-backfill"}' ] \
  && python3 - "$NB_STORE/memory.db" "$NB_SOURCE" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("SELECT source, body FROM records WHERE id='no_backfill'").fetchone()
assert row == (sys.argv[2], "preexisting source must never be backfilled into the journal"), row
print("no-backfill row/source preservation assertions: 2")
con.close()
PY
then
  ok "existing source preserves row/source and sentinel with no historical backfill"
else
  bad "existing source row/source or sentinel changed, or migrate failed"
fi

# Global, undecodable, and invalid legacy sources omit cwd; valid legacy origin survives.
OM_STORE="$ABSORB_TMP/omission-store"; OM_PROJECTS="$ABSORB_TMP/omission-projects"
OM_PROFILE="$ABSORB_TMP/omission-profile"; OM_ROOT="$ABSORB_TMP/legacy-valid"
OM_WRONG="$ABSORB_TMP/omission-wrong"; OM_BAD_ENC="-cannot-decode-this-project-xyz"
mkdir -p "$OM_STORE" "$OM_PROJECTS/$OM_BAD_ENC/memory" "$OM_PROFILE" "$OM_ROOT" "$OM_WRONG"
printf '%s\n' '---' 'type: lesson' '---' 'undecodable auto-memory source omits ambient cwd' \
  > "$OM_PROJECTS/$OM_BAD_ENC/memory/undecodable.md"
printf '%s\n' 'global profile source omits ambient cwd' > "$OM_PROFILE/global-note.md"
printf '%s\n' '---' 'id: legacy-valid' 'tier: durable' 'scope: project' 'type: lesson' \
  "cwd_origin: $OM_ROOT" '---' 'valid legacy origin keeps its logical cwd' \
  > "$OM_STORE/valid.md"
printf '%s\n' '---' 'id: legacy-invalid' 'tier: durable' 'scope: project' 'type: lesson' \
  'cwd_origin: /does/not/exist/legacy' '---' 'invalid legacy origin omits cwd' \
  > "$OM_STORE/invalid.md"
printf '%s\n' 'legacy markdown without frontmatter omits cwd' > "$OM_STORE/missing.md"
OM_JOURNAL="$OM_STORE/write-events.jsonl"
OM_OK=0
if (
  cd "$OM_WRONG"
  run_checked env MEM_STORE="$OM_STORE" MEM_PROJECTS="$OM_PROJECTS" MEM_PROFILE="$OM_PROFILE" \
    MEM_WRITE_EVENTS="$OM_JOURNAL" MEM_CWD="/wrong/repo" MEM_DISTILL=1 MEM_ACTOR=curator \
    python3 "$MEM" migrate --apply --all-projects >/dev/null
); then
  OM_OK=1
fi
if [ "$OM_OK" = 1 ] && python3 - "$OM_STORE/memory.db" "$OM_JOURNAL" "$OM_ROOT" <<'PY'
import json, sqlite3, sys
db, journal, valid_root = sys.argv[1:]
events = [json.loads(line) for line in open(journal, encoding="utf-8") if line.strip()]
con = sqlite3.connect(db)
sources = {rid: source for rid, source in con.execute("SELECT id, source FROM records")}
con.close()
by_source = {sources[event["id"]]: event for event in events}
expected = {
    "auto-memory:-cannot-decode-this-project-xyz/undecodable.md": None,
    "user-profile:global-note": None,
    "md-file:valid.md": valid_root,
    "md-file:invalid.md": None,
    "md-file:missing.md": None,
}
assert set(by_source) == set(expected), (set(by_source), expected)
for source, cwd in expected.items():
    event = by_source[source]
    assert event["action"] == "add" and event["actor"] == "sync", event
    if cwd is None:
        assert "cwd" not in event, (source, event)
    else:
        assert event["cwd"] == cwd, (source, event)
print("cwd omission/valid legacy assertions: 10")
PY
then
  ok "global and decode-impossible sources omit cwd; valid legacy cwd is preserved"
else
  bad "cwd omission/valid legacy assertions failed"
fi

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
