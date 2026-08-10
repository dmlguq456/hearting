#!/usr/bin/env bash
# Regressions for the 2026-07-22 memory-audit repairs:
#   1) migrate v6 — legacy cwd_origin remap (audit W3) + canonical absorb path
#   2) CJK bigram retrieval (audit W4) — ranked FTS for Korean substrings
#   3) dump plain-commit mode + loud failure + maintenance squash
# All fixtures are MEM_STORE-isolated; the live store is never touched.
set -uo pipefail

MEM="$(cd "$(dirname "$0")" && pwd)/mem.py"
PASS=0 FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok  %s\n' "$*"; }
bad(){ FAIL=$((FAIL+1)); printf '  BAD %s\n' "$*"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$(cd "$(dirname "$0")/../.." && pwd)"
export MEM_PROFILE="$TMP/no-profile"   # keep fixture stores free of live profile absorbs
unset MEM_DUMP_PUSH MEM_DUMP_COMMIT    # hermetic dump-commit behavior

q(){ python3 - "$1" "$2" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(sys.argv[2]).fetchone()[0])
PY
}

echo "== storage default: local XDG store when source memory is absent =="
FALLBACK_HOME="$TMP/fallback-home"; FALLBACK_AGENT="$TMP/fallback-agent"
mkdir -p "$FALLBACK_HOME" "$FALLBACK_AGENT"
HOME="$FALLBACK_HOME" AGENT_HOME="$FALLBACK_AGENT" MEM_INIT=1 \
  python3 "$MEM" add durable note \
    "local data store fallback fixture record" --scope global >/dev/null 2>&1
[ -f "$FALLBACK_HOME/.local/share/hearting/memory/memory.db" ] \
  && ok "missing source memory defaults SQLite to the local data store" \
  || bad "local data-store fallback was not selected"

# ---------------------------------------------------------------- repair 1
echo "== repair 1a: migrate v6 remaps unambiguous legacy cwd_origin keys =="
PROJ="$TMP/proj"; mkdir -p "$PROJ"
git -C "$PROJ" init -q
git -C "$PROJ" remote add origin https://github.com/example/fixture-repo.git
ENC="$(python3 -c "import re,sys;print(re.sub(r'[/._]','-',sys.argv[1]))" "$PROJ")"
CANON="git:github.com/example/fixture-repo"
PLAIN="$TMP/plaindir"; mkdir -p "$PLAIN"
PENC="$(python3 -c "import re,sys;print(re.sub(r'[/._]','-',sys.argv[1]))" "$PLAIN")"
STORE="$TMP/store-remap"; export MEM_STORE="$STORE"
python3 "$MEM" add durable note \
  "seed record so the fixture store is not empty at migration time" \
  --scope global >/dev/null 2>&1
python3 - "$STORE/memory.db" "$ENC" "$PROJ" "$PENC" <<'PY'
import sqlite3, sys
db, enc, raw, penc = sys.argv[1:5]
c = sqlite3.connect(db)
rows = [
 ('leg_enc','durable','project','note',enc,'2026-07-01','2026-07-01',None,None,'[]','[]',
  'legacy encoded-key record body for v6 remap test',1,'2026-07-01',0,'ordinary'),
 ('leg_raw','durable','project','note',raw,'2026-07-01','2026-07-01',None,None,'[]','[]',
  'legacy raw-path-key record body for v6 remap test',1,'2026-07-01',0,'ordinary'),
 ('leg_plain','durable','project','note',penc,'2026-07-01','2026-07-01',None,None,'[]','[]',
  'non-git dir encoded key must be preserved untouched',1,'2026-07-01',0,'ordinary'),
 ('leg_dead','durable','project','note','-nonexistent-path-xyzq','2026-07-01','2026-07-01',
  None,None,'[]','[]','dead path encoded key must be preserved untouched',1,'2026-07-01',0,'ordinary'),
 ('leg_rename','durable','project','note','git:github.com/dmlguq456/claude_setting',
  '2026-07-01','2026-07-01',None,None,'[]','[]',
  'renamed project key remap depends on the live AGENT_HOME checkout',1,'2026-07-01',0,'ordinary'),
]
c.executemany('INSERT INTO records(id,tier,scope,type,cwd_origin,created,updated,expires,source,'
              'tags,links,body,strength,last_accessed,injection_flag,delivery_state) '
              'VALUES(' + ','.join('?'*16) + ')', rows)
c.execute('PRAGMA user_version=5'); c.commit(); c.close()
PY
python3 "$MEM" stats >/dev/null 2>"$TMP/mig6.err"
grep -q "\[migrate v6\] applied" "$TMP/mig6.err" \
  && ok "v6 migration ran and logged its apply line" || bad "v6 log missing: $(cat "$TMP/mig6.err")"
DB="$STORE/memory.db"
[ "$(q "$DB" "PRAGMA user_version")" = 7 ] && ok "user_version advanced to 7" || bad "user_version not 7"
[ "$(q "$DB" "SELECT cwd_origin FROM records WHERE id='leg_enc'")" = "$CANON" ] \
  && ok "encoded key remapped to canonical project key" || bad "leg_enc not remapped"
[ "$(q "$DB" "SELECT cwd_origin FROM records WHERE id='leg_raw'")" = "$CANON" ] \
  && ok "raw absolute-path key remapped to canonical project key" || bad "leg_raw not remapped"
[ "$(q "$DB" "SELECT cwd_origin FROM records WHERE id='leg_plain'")" = "$PENC" ] \
  && ok "non-git dir key preserved (no canonical form)" || bad "leg_plain mutated"
[ "$(q "$DB" "SELECT cwd_origin FROM records WHERE id='leg_dead'")" = "-nonexistent-path-xyzq" ] \
  && ok "dead-path key preserved untouched" || bad "leg_dead mutated"
LIVE_KEY="$(cd "$AGENT_HOME" && python3 - "$AGENT_HOME" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "memmod", __import__("pathlib").Path(sys.argv[1], "tools/memory/mem.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.project_key(m.Path(sys.argv[1]), seed=False))
PY
)"
RENAMED_NOW="$(q "$DB" "SELECT cwd_origin FROM records WHERE id='leg_rename'")"
if [ "$LIVE_KEY" = "git:github.com/dmlguq456/hearting" ]; then
  [ "$RENAMED_NOW" = "$LIVE_KEY" ] \
    && ok "claude_setting rename remapped to live-derived key" || bad "rename not applied: $RENAMED_NOW"
else
  [ "$RENAMED_NOW" = "git:github.com/dmlguq456/claude_setting" ] \
    && ok "rename skipped on foreign AGENT_HOME (guarded)" || bad "rename fired without live target: $RENAMED_NOW"
fi
[ "$(q "$DB" "SELECT COUNT(*) FROM records")" = 6 ] && ok "no rows created or deleted" || bad "row count drifted"

echo "== repair 1b: v6 rerun is idempotent =="
SNAP1="$(python3 -c "import sqlite3,sys;print('|'.join(f'{a}:{b}' for a,b in sqlite3.connect(sys.argv[1]).execute('SELECT id,cwd_origin FROM records ORDER BY id')))" "$DB")"
python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA user_version=5'); c.commit(); c.close()
PY
python3 "$MEM" stats >/dev/null 2>"$TMP/mig6b.err"
SNAP2="$(python3 -c "import sqlite3,sys;print('|'.join(f'{a}:{b}' for a,b in sqlite3.connect(sys.argv[1]).execute('SELECT id,cwd_origin FROM records ORDER BY id')))" "$DB")"
[ "$SNAP1" = "$SNAP2" ] && ok "forced rerun changes nothing (idempotent)" || bad "rerun drifted: $SNAP2"
[ "$(q "$DB" "PRAGMA user_version")" = 7 ] && ok "user_version restored to 7 after rerun" || bad "rerun version wrong"

echo "== repair 1c: absorb path emits canonical keys (regeneration fix) =="
STORE2="$TMP/store-absorb"; PROJECTS="$TMP/projects"
mkdir -p "$PROJECTS/$ENC/memory"
cat > "$PROJECTS/$ENC/memory/notes.md" <<'EOF'
---
type: lesson
---
absorb path regression: auto-memory record must carry the canonical project key
EOF
( cd "$TMP" && MEM_STORE="$STORE2" MEM_PROJECTS="$PROJECTS" \
    python3 "$MEM" migrate --apply --all-projects >/dev/null 2>&1 )
DB2="$STORE2/memory.db"
[ "$(q "$DB2" "SELECT cwd_origin FROM records WHERE source LIKE 'auto-memory:%'")" = "$CANON" ] \
  && ok "auto-memory absorb writes canonical cwd_origin" || bad "absorb still writes encoded key"
[ "$(q "$DB2" "SELECT COUNT(*) FROM records WHERE source = 'auto-memory:$ENC/notes.md'")" = 1 ] \
  && ok "auto-memory source namespace unchanged (idempotency key stable)" || bad "source key drifted"
mkdir -p "$PROJ/.agent_reports"
printf '## Decisions\n- fixture postit decision body over fourteen characters\n' \
  > "$PROJ/.agent_reports/post-it.md"
( cd "$PROJ" && MEM_STORE="$STORE2" MEM_PROJECTS="$PROJECTS" \
    python3 "$MEM" migrate --apply --all-projects >/dev/null 2>&1 )
[ "$(q "$DB2" "SELECT cwd_origin FROM records WHERE source LIKE 'post-it:%'")" = "$CANON" ] \
  && ok "post-it absorb writes canonical cwd_origin" || bad "post-it absorb key wrong"
[ "$(q "$DB2" "SELECT COUNT(*) FROM records WHERE source LIKE 'post-it:$ENC:%'")" = 1 ] \
  && ok "post-it source keeps encoded namespace (idempotency key stable)" || bad "post-it source drifted"

# ---------------------------------------------------------------- repair 2
echo "== repair 2: CJK bigram shadow gives ranked Korean substring recall =="
STORE3="$TMP/store-cjk"; PROJ3="$TMP/proj3"; mkdir -p "$PROJ3"
export MEM_STORE="$STORE3"
cli3(){ (cd "$PROJ3" && python3 "$MEM" "$@"); }
# Doc A (weak: one hit, inserted FIRST → lower rowid) vs doc B (strong: repeated
# term). Unranked LIKE returns rowid order (A first); bm25 must rank B first.
cli3 add durable note "실험 노트 항목: 스펙트로그램 관련 설정은 별도 문서에 있고 여기는 다른 잡다한 내용이 길게 이어진다" >/dev/null 2>&1
cli3 add durable note "스펙트로그램 창 설정 정본: 스펙트로그램 해상도와 스펙트로그램 hop 값 기록" >/dev/null 2>&1
cli3 add durable note "회의실 창문 상태 점검 완료 기록 남김" >/dev/null 2>&1
cli3 add durable note "pure english fixture record about dispatch windows and workers" >/dev/null 2>&1
OUT="$(cli3 recall "펙트로그" --no-touch 2>/dev/null)"
HITS="$(grep -c '스펙트로그램' <<<"$OUT" || true)"
[ "$HITS" = 2 ] && ok "substring query (no whole-token match) hits both Korean docs" \
  || bad "substring hits=$HITS: $OUT"
FIRST="$(sed -n 's/^  \[[^]]*\] \([^:]*\):.*/\1/p' <<<"$OUT" | head -1)"
grep -q "정본" <<<"$(cli3 show "$FIRST" --all 2>/dev/null)" \
  && ok "bm25 ranks the repeated-term doc first (LIKE could not rank)" \
  || bad "ranking wrong; first=$FIRST"
OUT2="$(cli3 recall "윈도" --no-touch 2>/dev/null)"
grep -q "no store matches" <<<"$OUT2" && ok "korean substring with no match stays empty" \
  || bad "unexpected 윈도 hits: $OUT2"
OUT3="$(cli3 recall "창" --no-touch 2>/dev/null)"
grep -q "창문" <<<"$OUT3" && ok "single-char CJK query prefix-matches indexed bigrams" \
  || bad "single-char query missed: $OUT3"
OUT4="$(cli3 recall "dispatch windows" --no-touch 2>/dev/null)"
grep -q "pure english fixture" <<<"$OUT4" && ok "english token recall unchanged" \
  || bad "english recall broke: $OUT4"
OUT5="$(cli3 recall "windo" --no-touch 2>/dev/null)"
grep -q "no store matches" <<<"$OUT5" && ok "english substrings still do not match (behavior unchanged)" \
  || bad "english substring suddenly matches: $OUT5"
OUT6="$(cli3 recall "쿼바뀨죨" --no-touch 2>/dev/null)"
grep -q "no store matches" <<<"$OUT6" && ok "nonsense korean query returns empty" \
  || bad "nonsense matched: $OUT6"
OUT7="$(MEM_NO_TRIGRAM=1 cli3 recall "펙트로그" --no-touch 2>/dev/null)"
grep -q "스펙트로그램" <<<"$OUT7" && ok "LIKE fallback still works when shadow disabled" \
  || bad "fallback lost: $OUT7"

echo "== repair 2b: shadow refresh is idempotent and self-healing =="
IDX1="$(cli3 index --rebuild 2>/dev/null)"
grep -q "cjk-bigram" <<<"$IDX1" && ok "[index] reports cjk-bigram shadow" || bad "index output: $IDX1"
PAR="$(q "$STORE3/memory.db" "SELECT (SELECT COUNT(*) FROM records) - (SELECT COUNT(*) FROM records_cjk)")"
[ "$PAR" = 0 ] && ok "shadow parity after rebuild" || bad "shadow drift: $PAR"
IDX2="$(cli3 index --rebuild 2>/dev/null)"
[ "$IDX1" = "$IDX2" ] && ok "rebuild rerun identical (idempotent)" || bad "rebuild drifted"
# Upgrade path: a store created WITHOUT the shadow gains it on next open.
STORE4="$TMP/store-upgrade"; export MEM_STORE="$STORE4"
( cd "$PROJ3" && MEM_NO_TRIGRAM=1 python3 "$MEM" add durable note \
    "업그레이드 경로 검증용 스펙트로그램 백필 레코드" >/dev/null 2>&1 )
[ "$(q "$STORE4/memory.db" "SELECT COUNT(*) FROM sqlite_master WHERE name='records_cjk'")" = 0 ] \
  && ok "shadow absent while disabled" || bad "shadow created despite MEM_NO_TRIGRAM"
OUT8="$( (cd "$PROJ3" && python3 "$MEM" recall "펙트로그" --no-touch 2>/dev/null) )"
grep -q "백필" <<<"$OUT8" && ok "reopen self-heals: backfilled shadow serves substring recall" \
  || bad "self-heal backfill failed: $OUT8"
export MEM_STORE="$STORE3"

# ---------------------------------------------------------------- repair 3
echo "== repair 3: dump commits are plain (non-amend) and failures warn =="
STORE5="$TMP/store-dump"; mkdir -p "$STORE5"; export MEM_STORE="$STORE5"
export MEM_PROJECTS="$TMP/no-projects"   # hermetic: no live auto-memory absorbs
git -C "$STORE5" init -q
git -C "$STORE5" config user.email mem-test@example.invalid
git -C "$STORE5" config user.name "mem test"
PROJ5="$TMP/proj5"; mkdir -p "$PROJ5"
( cd "$PROJ5" && python3 "$MEM" add durable note \
    "first dump fixture record body long enough for the gate" >/dev/null 2>&1 )
( cd "$PROJ5" && python3 "$MEM" sync >/dev/null 2>&1 )
( cd "$PROJ5" && python3 "$MEM" add durable note \
    "second dump fixture record body long enough for the gate" >/dev/null 2>&1 )
( cd "$PROJ5" && python3 "$MEM" sync >/dev/null 2>&1 )
CNT="$(git -C "$STORE5" rev-list --count HEAD)"
[ "$CNT" = 2 ] && ok "two syncs → two plain commits (amend removed)" || bad "commit count=$CNT"
git -C "$STORE5" log --format=%s | grep -vq "^chore: dump — auto-sync" \
  && bad "unexpected commit subject: $(git -C "$STORE5" log --format=%s)" \
  || ok "auto-sync message pattern kept on every commit"
touch "$STORE5/.git/index.lock"
( cd "$PROJ5" && python3 "$MEM" add durable note \
    "third dump fixture record body long enough for the gate" >/dev/null 2>&1 )
ERR="$( (cd "$PROJ5" && python3 "$MEM" sync >/dev/null) 2>&1 )"
grep -q "\[mem\] dump git-add failed" <<<"$ERR" \
  && ok "git failure prints a one-line stderr warning (was silent for 8 days)" \
  || bad "no warning: $ERR"
WCNT="$(grep -c "\[mem\] dump" <<<"$ERR")"
[ "$WCNT" = 1 ] && ok "exactly one warning line per failure" || bad "warning lines=$WCNT"
rm -f "$STORE5/.git/index.lock"
CNT2="$(git -C "$STORE5" rev-list --count HEAD)"
[ "$CNT2" = 2 ] && ok "failed sync stayed non-fatal (no commit, no crash)" || bad "count after lock=$CNT2"

echo "== repair 3a: local DB and external dump checkout stay split =="
STORE6="$TMP/store-split"; DREPO6="$TMP/dumprepo-split"
mkdir -p "$STORE6" "$DREPO6"
git -C "$DREPO6" init -q
git -C "$DREPO6" config user.email mem-test@example.invalid
git -C "$DREPO6" config user.name "mem test"
: > "$DREPO6/dump.jsonl"
ln -s "$DREPO6/dump.jsonl" "$STORE6/dump.jsonl"
export MEM_STORE="$STORE6"
( cd "$PROJ5" && python3 "$MEM" add durable note \
    "split local database and external dump repository fixture" >/dev/null 2>&1 )
( cd "$PROJ5" && python3 "$MEM" sync >/dev/null 2>&1 )
[ -L "$STORE6/dump.jsonl" ] && ok "sync preserves the local dump symlink" \
  || bad "sync replaced the local dump symlink"
[ -f "$STORE6/memory.db" ] && ok "SQLite database stays in the local store" \
  || bad "local SQLite database missing"
[ "$(git -C "$DREPO6" rev-list --count HEAD)" = 1 ] \
  && ok "sync commits through the symlink into the dump checkout" \
  || bad "external dump checkout did not receive one commit"
grep -q 'split local database' "$DREPO6/dump.jsonl" \
  && ok "external dump mirror contains the synchronized record" \
  || bad "external dump mirror was not refreshed"

echo "== repair 3b: maintenance squashes old history and preserves trees =="
DREPO="$TMP/dumprepo"; mkdir -p "$DREPO"; git -C "$DREPO" init -q
git -C "$DREPO" config user.email mem-test@example.invalid
git -C "$DREPO" config user.name "mem test"
mkc(){ printf '%s\n' "$2" > "$DREPO/dump.jsonl"; git -C "$DREPO" add dump.jsonl >/dev/null
  GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" \
    git -C "$DREPO" commit -qm "chore: dump — auto-sync ($1)"; }
mkc 2026-06-01T00:00:00 one
mkc 2026-06-02T00:00:00 two
mkc 2026-06-03T00:00:00 three
mkc "$(date -Iseconds)" fresh
TREE_BEFORE="$(git -C "$DREPO" rev-parse 'HEAD^{tree}')"
OUTD="$(MEM_STORE="$DREPO" python3 "$MEM" maintenance --squash-days 14 2>&1)"
grep -q "would squash 3 commits" <<<"$OUTD" && ok "dry-run reports the squash plan" || bad "dry-run: $OUTD"
[ "$(git -C "$DREPO" rev-list --count HEAD)" = 4 ] && ok "dry-run mutated nothing" \
  || bad "dry-run rewrote history"
OUTA="$(MEM_STORE="$DREPO" python3 "$MEM" maintenance --squash-days 14 --apply 2>&1)"
CNTA="$(git -C "$DREPO" rev-list --count HEAD)"
[ "$CNTA" = 2 ] && ok "history squashed 4→2 (squash root + kept commit)" || bad "count=$CNTA: $OUTA"
[ "$(git -C "$DREPO" rev-parse 'HEAD^{tree}')" = "$TREE_BEFORE" ] \
  && ok "HEAD tree preserved byte-identically" || bad "tree drift after squash"
git -C "$DREPO" status --porcelain | grep -q . && bad "worktree dirtied by squash" \
  || ok "worktree and index untouched"
OUTA2="$(MEM_STORE="$DREPO" python3 "$MEM" maintenance --squash-days 14 --apply 2>&1)"
grep -q "no history older" <<<"$OUTA2" && ok "maintenance rerun is idempotent" || bad "rerun: $OUTA2"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = 0 ]
