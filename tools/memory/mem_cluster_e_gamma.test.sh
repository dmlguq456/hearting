#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
# Cluster E Phase γ — isolated test suite (curator subcommands + anti-bloat + graduate).
# Maps to Verification ②③⑤⑥⑦ (dispatch ①④⑧ live in mem-distill-dispatch.test.sh).
#
# ABSOLUTE: every case uses isolated MEM_STORE/MEM_PROJECTS (mktemp -d). NEVER writes real runtime memory.
# This suite spawns NO `claude` (ISO-2): it exercises mem.py subcommands directly via `python3 "$MEM"`.
# project_key is made repo-independent by cd-ing into a non-git temp WORKDIR (project_key→bare enc_cwd).
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

BASE_STORE="$(mktemp -d)"; BASE_PROJ="$(mktemp -d)"; WORKDIR="$(mktemp -d)"
trap 'rm -rf "$BASE_STORE" "$BASE_PROJ" "$WORKDIR"' EXIT
export MEM_STORE="$BASE_STORE" MEM_PROJECTS="$BASE_PROJ"
cd "$WORKDIR"   # non-git → project_key = bare enc_cwd (repo-independent)

# current project_key (records "in current project" must carry this cwd_origin)
PKEY="$(PYTHONPATH="$ROOT/tools/memory" python3 -c 'import mem; print(mem.project_key())')"

# ---- helpers (all honor the CURRENT $MEM_STORE — sub-store sections switch it) ------
gfile() { printf '%s' "$MEM_STORE/deleted-records.jsonl"; }
# create schema for the current MEM_STORE (mem stats early-returns w/o creating — use get_con)
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
# seed a record by direct INSERT (full control over cwd_origin/strength/last_accessed/scope/type)
seed() { # id tier scope type cwd_origin strength last_accessed body [expires]
  python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys, datetime
db, rid, tier, scope, rtype, cwd, strg, la, body = sys.argv[1:10]
exp = sys.argv[10] if len(sys.argv) > 10 else None
con = sqlite3.connect(db); con.execute("PRAGMA busy_timeout=5000")
today = datetime.date.today().isoformat()
con.execute("INSERT OR REPLACE INTO records(id,tier,scope,type,cwd_origin,created,updated,"
            "expires,source,tags,links,body,strength,last_accessed,injection_flag) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (rid, tier, scope, rtype, cwd, today, today, exp, None, "[]", "[]",
             body, int(strg), la or today))
# keep FTS mirror coherent so recall finds it
try: con.execute("INSERT INTO records_fts(id, body) VALUES(?,?)", (rid, body))
except Exception: pass
con.commit(); con.close()
PY
}
graves() { local g="$MEM_STORE/deleted-records.jsonl"; [ -f "$g" ] && wc -l < "$g" | tr -d ' ' || echo 0; }
exists() { sql "SELECT COUNT(*) FROM records WHERE id=?" "$1"; }

# initialize DB/schema (migrate to v5)
initdb
DAY40="$(python3 -c 'import datetime;print((datetime.date.today()-datetime.timedelta(days=40)).isoformat())')"
TODAY="$(python3 -c 'import datetime;print(datetime.date.today().isoformat())')"

# =====================================================================
echo "== ② action execution + DB state =="
seed own_w1 working project thread "$PKEY" 1 "$TODAY" "working item alpha resolved candidate one"
seed own_d1 durable  project lesson "$PKEY" 1 "$TODAY" "durable lesson alpha to reinforce here"
# reinforce
python3 "$MEM" reinforce own_d1 >/dev/null; rc=$?
[ "$rc" = 0 ] && [ "$(sql 'SELECT strength FROM records WHERE id=?' own_d1)" = 2 ] \
  && ok "② reinforce: strength 1→2 + rc0" || bad "② reinforce failed (rc=$rc strength=$(sql 'SELECT strength FROM records WHERE id=?' own_d1))"
[ "$(sql 'SELECT last_accessed FROM records WHERE id=?' own_d1)" = "$TODAY" ] \
  && ok "② reinforce: last_accessed=today" || bad "② reinforce last_accessed mismatch"

# graduate
python3 "$MEM" graduate own_w1 >/dev/null; rc=$?
[ "$rc" = 0 ] && [ "$(sql "SELECT tier FROM records WHERE id=?" own_w1)" = durable ] \
  && [ -z "$(sql 'SELECT expires FROM records WHERE id=?' own_w1)" ] \
  && ok "⑦ graduate working→durable (tier flip, expires NULL)" \
  || bad "⑦ graduate failed (rc=$rc tier=$(sql 'SELECT tier FROM records WHERE id=?' own_w1))"
# graduate refuses non-working
python3 "$MEM" graduate own_d1 >/dev/null 2>&1; [ "$?" = 1 ] \
  && ok "⑦ graduate refuses non-working (rc1)" || bad "⑦ graduate should refuse durable"

# =====================================================================
echo "== ② prune graveyard exact-match (DEST-1) =="
seed own_p1 durable project note "$PKEY" 3 "$TODAY" "prunable durable note body content here xyz"
PRE_BODY="$(sql 'SELECT body FROM records WHERE id=?' own_p1)"
G0=$(graves)
python3 "$MEM" prune own_p1 >/dev/null; rc=$?
[ "$rc" = 0 ] && [ "$(exists own_p1)" = 0 ] && ok "② prune: deleted + rc0" || bad "② prune failed"
[ "$(graves)" = "$((G0+1))" ] && ok "② prune: graveyard +1 line" || bad "② prune graveyard line count"
# DEST-1: parse last graveyard line, assert v7 record + recovery metadata
python3 - "$MEM_STORE/deleted-records.jsonl" "$PRE_BODY" <<'PY' && ok "DEST-1 graveyard = exact v7 record + recovery metadata" || bad "DEST-1 graveyard shape/content mismatch"
import json, sys
line = open(sys.argv[1]).read().splitlines()[-1]
rec = json.loads(line)
COLS = {"id","tier","scope","type","cwd_origin","created","updated","expires","source",
        "tags","links","body","strength","last_accessed","injection_flag","delivery_state",
        "headline","aliases","entities","topics","artifact_refs","status","canonical_id",
        "superseded_by","capsule_version",
        "_deleted_at","_action","_canonical"}
assert set(rec) == COLS, set(rec) ^ COLS
assert json.dumps(rec, sort_keys=True, ensure_ascii=False) == line, "not sort_keys canonical"
assert rec["id"] == "own_p1" and rec["body"] == sys.argv[2]
assert rec["tier"] == "durable" and rec["strength"] == 3
PY

# =====================================================================
echo "== ② merge (sum + canonical-preserve + graveyard count DEST-3) =="
seed m_can durable project lesson "$PKEY" 2 "$TODAY" "merge canonical body content stays alpha"
seed m_b   durable project lesson "$PKEY" 3 "$TODAY" "merge near dup body beta to fold in"
seed m_c   durable project lesson "$PKEY" 5 "$TODAY" "merge near dup body gamma to fold in"
G0=$(graves)
# include a duplicate canonical id in the list — must not double-count or self-delete (C1)
python3 "$MEM" merge --canonical m_can m_can m_can m_b m_c >/dev/null; rc=$?
[ "$rc" = 0 ] && [ "$(exists m_can)" = 1 ] && [ "$(exists m_b)" = 0 ] && [ "$(exists m_c)" = 0 ] \
  && ok "② merge: canonical kept, non-canonical deleted" || bad "② merge delete state wrong (rc=$rc)"
[ "$(sql 'SELECT strength FROM records WHERE id=?' m_can)" = 10 ] \
  && ok "② merge: strength sum 2+3+5=10 (canonical counted once despite dup input)" \
  || bad "② merge strength = $(sql 'SELECT strength FROM records WHERE id=?' m_can) (want 10)"
[ "$(graves)" = "$((G0+2))" ] && ok "DEST-3 merge graveyard += len(set(ids))-1 = 2" || bad "DEST-3 merge graveyard count"
# canonical must NOT be in graveyard
grep -q '"id": "m_can"' "$MEM_STORE/deleted-records.jsonl" && bad "DEST-3 canonical wrongly graveyarded" || ok "DEST-3 canonical not graveyarded"

# =====================================================================
echo "== DEST-5 graveyard → import recovery smoke =="
# take own_p1's graveyard line, import it, assert restored
python3 - "$MEM_STORE/deleted-records.jsonl" "$BASE_STORE/recover.jsonl" <<'PY'
import sys
for l in open(sys.argv[1]):
    if '"id": "own_p1"' in l:
        open(sys.argv[2], "w").write(l); break
PY
# import replaces whole store; do it on a SEPARATE isolated store to avoid clobbering this test's DB
REC_STORE="$(mktemp -d)"
MEM_STORE="$REC_STORE" python3 "$MEM" import "$BASE_STORE/recover.jsonl" >/dev/null 2>&1
[ "$(MEM_STORE="$REC_STORE" sql 'SELECT body FROM records WHERE id=?' own_p1 2>/dev/null)" = "$PRE_BODY" ] \
  && ok "DEST-5 graveyard line re-imports to a restored record" || bad "DEST-5 recovery failed"
rm -rf "$REC_STORE"

# =====================================================================
echo "== ③ rejection (other-project / profile / global / nonexistent) — zero deletion + zero graveyard (DEST-2) =="
seed other_d durable project lesson "git:github.com/other/repo" 1 "$TODAY" "another project durable record body"
seed prof_r  durable global  profile "global"                    1 "$TODAY" "aspect: coding profile body content"
seed glob_d  durable global  pref    "global"                    1 "$TODAY" "a global non-profile durable pref body"
for id in other_d prof_r glob_d does_not_exist; do
  G0=$(graves)
  python3 "$MEM" prune "$id" >/dev/null 2>&1; rc=$?
  [ "$rc" = 1 ] && ok "③ prune reject rc1: $id" || bad "③ prune $id rc=$rc (want 1)"
  [ "$(graves)" = "$G0" ] && ok "③ prune $id: graveyard unchanged" || bad "③ prune $id appended graveyard!"
done
[ "$(exists other_d)" = 1 ] && [ "$(exists prof_r)" = 1 ] && [ "$(exists glob_d)" = 1 ] \
  && ok "③ rejected records all still present (zero deletion)" || bad "③ a rejected record was deleted!"

echo "== DEST-4 merge ATOMIC rejection (own+foreign bundle → all deletion 0, graveyard 0) =="
seed a_own1 durable project lesson "$PKEY" 1 "$TODAY" "atomic merge own record one body here"
seed a_own2 durable project lesson "$PKEY" 1 "$TODAY" "atomic merge own record two body here"
G0=$(graves)
python3 "$MEM" merge --canonical a_own1 a_own1 a_own2 other_d >/dev/null 2>&1; rc=$?
[ "$rc" = 1 ] && ok "DEST-4 merge with one foreign id → rc1" || bad "DEST-4 merge rc=$rc (want 1)"
[ "$(exists a_own1)" = 1 ] && [ "$(exists a_own2)" = 1 ] && [ "$(exists other_d)" = 1 ] \
  && ok "DEST-4 ALL ids (incl. owned) survive — no partial destruction" || bad "DEST-4 partial destruction!"
[ "$(graves)" = "$G0" ] && ok "DEST-4 graveyard unchanged" || bad "DEST-4 graveyard appended on rejected merge!"

# =====================================================================
echo "== ⑥ E-1 strength reinforce via re-add + working TTL refresh =="
python3 "$MEM" add working hint "exact reinforce target body for E1 re-add test" >/dev/null
RID="$(sql "SELECT id FROM records WHERE body LIKE 'exact reinforce target%'")"
# backdate last_accessed + expires to prove refresh
sql "UPDATE records SET last_accessed='2000-01-01', strength=1, expires='2000-01-01' WHERE id=?" "$RID" >/dev/null
python3 "$MEM" add working hint "exact reinforce target body for E1 re-add test" >/dev/null
[ "$(sql 'SELECT strength FROM records WHERE id=?' "$RID")" = 2 ] \
  && ok "⑥ re-add exact → strength 1→2" || bad "⑥ re-add strength = $(sql 'SELECT strength FROM records WHERE id=?' "$RID")"
[ "$(sql 'SELECT last_accessed FROM records WHERE id=?' "$RID")" = "$TODAY" ] \
  && ok "⑥ re-add → last_accessed=today" || bad "⑥ re-add last_accessed not refreshed"
[ "$(sql 'SELECT expires FROM records WHERE id=?' "$RID")" != "2000-01-01" ] \
  && ok "⑥ re-add (working) → expires refreshed (F1)" || bad "⑥ re-add working expires not refreshed"

# =====================================================================
echo "== ⑤ anti-bloat: curate-snapshot ceiling + cold-decay + IDS membership =="
COLD_STORE="$(mktemp -d)"; export MEM_STORE="$COLD_STORE"
initdb
# cold candidate: durable, last_accessed 40d ago, strength 1
seed cold_1 durable project lesson "$PKEY" 1 "$DAY40" "cold decay candidate durable body untouched long"
# warm durable: recent, strength high → NOT cold
seed warm_1 durable project lesson "$PKEY" 9 "$TODAY" "warm durable recently accessed strong body here"
SNAP="$(python3 "$MEM" curate-snapshot)"
printf '%s' "$SNAP" | grep -q "cold-prune-candidate: .*cold_1" \
  && ok "⑤ cold-decay: cold_1 surfaced (40d + strength1)" || bad "⑤ cold_1 not in cold SIGNAL"
printf '%s' "$SNAP" | grep -q "cold-prune-candidate:.*warm_1" \
  && bad "⑤ warm_1 wrongly flagged cold" || ok "⑤ warm_1 not cold-flagged"
printf '%s' "$SNAP" | grep -q "^IDS: .*cold_1.*warm_1\|^IDS: .*warm_1.*cold_1" \
  && ok "⑤ IDS membership line lists durable ids" || bad "⑤ IDS line missing ids"
# ceiling: batch-insert >80 durable
python3 - "$COLD_STORE/memory.db" "$PKEY" "$TODAY" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1]); pk, t = sys.argv[2], sys.argv[3]
rows = [(f"ceil_{i}","durable","project","note",pk,t,t,None,None,"[]","[]",
         f"ceiling filler durable record number {i} body content", 1, t, 0, "ordinary") for i in range(85)]
con.executemany("INSERT OR REPLACE INTO records(id,tier,scope,type,cwd_origin,created,updated,"
                "expires,source,tags,links,body,strength,last_accessed,injection_flag,delivery_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
con.commit(); con.close()
PY
python3 "$MEM" curate-snapshot | grep -q "ceiling: durable .* > 80 — aggressive consolidate" \
  && ok "⑤ ceiling SIGNAL fires at >80 durable" || bad "⑤ ceiling SIGNAL missing"
export MEM_STORE="$BASE_STORE"; rm -rf "$COLD_STORE"

# =====================================================================
echo "== ⑤ inject strength top-K ordering =="
TOPK_STORE="$(mktemp -d)"; export MEM_STORE="$TOPK_STORE"
initdb
seed lowk  durable project lesson "$PKEY" 1  "2026-06-17" "low strength durable should rank later body"
seed highk durable project lesson "$PKEY" 50 "2026-06-01" "high strength durable should rank first body"
INJ="$(python3 "$MEM" inject)"
# high-strength line must appear before low-strength line despite older 'updated'
python3 - <<PY && ok "⑤ inject durable order = strength top-K (high before low)" || bad "⑤ inject strength ordering wrong"
import sys
inj = """$INJ"""
hi = inj.find("high strength durable"); lo = inj.find("low strength durable")
sys.exit(0 if (hi != -1 and lo != -1 and hi < lo) else 1)
PY
export MEM_STORE="$BASE_STORE"; rm -rf "$TOPK_STORE"

# =====================================================================
echo "== RA-2/RA-3/RA-4 last_accessed side-effects (recall/inject) =="
LA_STORE="$(mktemp -d)"; export MEM_STORE="$LA_STORE"
initdb
seed la_dur  durable project lesson "$PKEY"  1 "2000-01-01" "recall side effect durable zeta keyword body"
seed la_prof durable global  profile "global" 1 "2000-01-01" "aspect: writing profile body must stay frozen"
# RA-2: recall a term → hit's last_accessed = today
python3 "$MEM" recall "zeta" >/dev/null 2>&1
[ "$(sql 'SELECT last_accessed FROM records WHERE id=?' la_dur)" = "$TODAY" ] \
  && ok "RA-2 recall → hit last_accessed=today" || bad "RA-2 recall last_accessed=$(sql 'SELECT last_accessed FROM records WHERE id=?' la_dur)"
# reset then inject
sql "UPDATE records SET last_accessed='2000-01-01' WHERE id=?" la_dur >/dev/null
python3 "$MEM" inject >/dev/null 2>&1
[ "$(sql 'SELECT last_accessed FROM records WHERE id=?' la_dur)" = "$TODAY" ] \
  && ok "RA-3 inject → emitted durable last_accessed=today" || bad "RA-3 inject last_accessed not bumped"
# RA-4: profile last_accessed unchanged by inject
[ "$(sql 'SELECT last_accessed FROM records WHERE id=?' la_prof)" = "2000-01-01" ] \
  && ok "RA-4 inject → profile last_accessed unchanged" || bad "RA-4 profile last_accessed wrongly bumped"
export MEM_STORE="$BASE_STORE"; rm -rf "$LA_STORE"

# =====================================================================
echo "== ⑦ reattribute orphan (reattach) + refusals =="
RE_STORE="$(mktemp -d)"; export MEM_STORE="$RE_STORE"
initdb
# orphan: bare enc_cwd that does NOT resolve to a live dir
seed orph_1 durable project lesson "-home-nonexistent-orphan-xyz-path" 1 "$TODAY" "orphan record to reattribute body here"
python3 "$MEM" reattribute orph_1 >/dev/null; rc=$?
[ "$rc" = 0 ] && [ "$(sql 'SELECT cwd_origin FROM records WHERE id=?' orph_1)" = "$PKEY" ] \
  && ok "⑦ reattribute orphan → cwd_origin=current pkey" || bad "⑦ reattribute failed (rc=$rc cwd=$(sql 'SELECT cwd_origin FROM records WHERE id=?' orph_1))"
# refuse: cwd_origin resolves to a LIVE dir (encode an existing dir, e.g. WORKDIR)
LIVEENC="$(PYTHONPATH="$ROOT/tools/memory" python3 -c "import mem,os; print(mem.enc_cwd('$WORKDIR'))")"
seed live_1 durable project lesson "$LIVEENC" 1 "$TODAY" "record whose origin resolves to a live dir body"
python3 "$MEM" reattribute live_1 >/dev/null 2>&1; [ "$?" = 1 ] \
  && ok "⑦ reattribute refuses live-resolving cwd_origin" || bad "⑦ reattribute should refuse live dir"
# refuse: git: key (live-unknown)
seed git_1 durable project lesson "git:github.com/x/y" 1 "$TODAY" "record with git remote key live unknown body"
python3 "$MEM" reattribute git_1 >/dev/null 2>&1; [ "$?" = 1 ] \
  && ok "⑦ reattribute refuses git: key (live-unknown)" || bad "⑦ reattribute should refuse git: key"
# refuse: self (already current pkey)
seed self_1 durable project lesson "$PKEY" 1 "$TODAY" "record already in current project body content"
python3 "$MEM" reattribute self_1 >/dev/null 2>&1; [ "$?" = 1 ] \
  && ok "⑦ reattribute refuses self (already current)" || bad "⑦ reattribute should refuse self"
# refuse: profile
seed pr_1 durable global profile "global" 1 "$TODAY" "aspect: domain profile must not reattribute body"
python3 "$MEM" reattribute pr_1 >/dev/null 2>&1; [ "$?" = 1 ] \
  && ok "⑦ reattribute refuses profile/global" || bad "⑦ reattribute should refuse profile"
export MEM_STORE="$BASE_STORE"; rm -rf "$RE_STORE"

# =====================================================================
echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
