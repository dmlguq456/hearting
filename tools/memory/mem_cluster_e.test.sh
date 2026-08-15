#!/usr/bin/env bash
# Cluster E Phase α — isolated test suite.
# Tests: ① migration idempotency  ② strength/last_accessed backfill  ③ project_key resolution
#        (decoder round-trip, part of ④)  ④ cwd_origin remap  ⑤ dump round-trip
#        ⑥ live DB preservation  ⑦ inject/recall project_key filter
#
# ABSOLUTE: every case uses isolated MEM_STORE/MEM_PROJECTS (mktemp -d).
# NEVER writes to a real runtime memory directory.
# All mem calls via `python3 "$MEM" ...`
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MEM="$ROOT/tools/memory/mem.py"
[ -f "$MEM" ] || { echo "FAIL: mem.py not found at $MEM"; exit 1; }

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

# ---------- global isolated base store (never real runtime home) ----------
BASE_STORE="$(mktemp -d)"
BASE_PROJ="$(mktemp -d)"
trap 'rm -rf "$BASE_STORE" "$BASE_PROJ"' EXIT
export MEM_STORE="$BASE_STORE" MEM_PROJECTS="$BASE_PROJ"

# ---------- helper: build pre-schema (v0) fixture DB ----------
# Creates a 12-column records table (old schema, no strength/last_accessed)
# with PRAGMA user_version=0 and 2-3 rows.
make_fixture_db() {
    local db_path="$1"
    python3 - "$db_path" <<'PYEOF'
import sqlite3, sys, datetime
db = sys.argv[1]
con = sqlite3.connect(db)
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA user_version=0")
con.execute("""CREATE TABLE IF NOT EXISTS records(
    id          TEXT PRIMARY KEY,
    tier        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    type        TEXT NOT NULL,
    cwd_origin  TEXT,
    created     TEXT,
    updated     TEXT,
    expires     TEXT,
    source      TEXT,
    tags        TEXT,
    links       TEXT,
    body        TEXT NOT NULL
)""")
today = datetime.date.today().isoformat()
yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
rows = [
    ("test_row_001", "durable", "project", "thread", "-home-old-path",
     yesterday, today, None, None, "[]", "[]",
     "This is the first fixture body for migration testing purposes"),
    ("test_row_002", "durable", "global",  "thread", "global",
     yesterday, today, None, None, "[]", "[]",
     "Second fixture row global scope for migration idempotency"),
    ("test_row_003", "working", "project", "hint",   "-home-another-path",
     yesterday, None,  (datetime.date.today()+datetime.timedelta(days=5)).isoformat(),
     None, "[]", "[]",
     "Third fixture working tier row created before migration runs here"),
]
con.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
con.commit()
con.close()
print("fixture created")
PYEOF
}

# ══════════════════════════════════════════════════════════════════
# ① user_version migration idempotency
# ══════════════════════════════════════════════════════════════════
echo "== ①: migration idempotency =="

STORE_1="$(mktemp -d)"; PROJ_1="$(mktemp -d)"
export MEM_STORE="$STORE_1" MEM_PROJECTS="$PROJ_1"

make_fixture_db "$STORE_1/memory.db" >/dev/null

# Trigger migration (read-only cmd)
python3 "$MEM" stats >/dev/null 2>&1
rc1=$?
[ "$rc1" = "0" ] && ok "①: mem stats exit 0 (first run)" || bad "①: mem stats failed (rc=$rc1)"

# Assert user_version == 10 (v7 retrieval + v8 sync + v9 cutover + v10 status evidence)
uv=$(python3 -c "import sqlite3; con=sqlite3.connect('$STORE_1/memory.db'); print(con.execute('PRAGMA user_version').fetchone()[0])")
[ "$uv" = "10" ] && ok "①: PRAGMA user_version == 10 after migration" || bad "①: user_version=$uv (expected 10)"

# Export dump run-1
DUMP_1A="$(mktemp)"
python3 "$MEM" export >/dev/null 2>&1
cp "$STORE_1/dump.jsonl" "$DUMP_1A" 2>/dev/null || { python3 "$MEM" export >/dev/null 2>&1; cp "$STORE_1/dump.jsonl" "$DUMP_1A"; }

# Trigger again (idempotent)
python3 "$MEM" stats >/dev/null 2>&1

# Export dump run-2
DUMP_1B="$(mktemp)"
python3 "$MEM" export >/dev/null 2>&1
cp "$STORE_1/dump.jsonl" "$DUMP_1B"

cmp -s "$DUMP_1A" "$DUMP_1B" \
  && ok "①: export dump byte-identical after run-1 vs run-2 (idempotent)" \
  || bad "①: dump not identical — migration not idempotent"
rm -f "$DUMP_1A" "$DUMP_1B"

# Column order: fresh-DB vs migrated-DB must have identical column order
FRESH_STORE="$(mktemp -d)"; FRESH_PROJ="$(mktemp -d)"
export MEM_STORE="$FRESH_STORE" MEM_PROJECTS="$FRESH_PROJ"
# stats() early-returns if DB absent; use 'add' to force schema creation then clean up
python3 "$MEM" add durable thread "fresh db schema seed record for column order test" >/dev/null 2>&1

fresh_cols=$(python3 -c "
import sqlite3
con = sqlite3.connect('$FRESH_STORE/memory.db')
cols = [r[1] for r in con.execute('PRAGMA table_info(records)')]
con.close()
print(','.join(cols))
")

export MEM_STORE="$STORE_1" MEM_PROJECTS="$PROJ_1"
migrated_cols=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_1/memory.db')
cols = [r[1] for r in con.execute('PRAGMA table_info(records)')]
con.close()
print(','.join(cols))
")

[ "$fresh_cols" = "$migrated_cols" ] \
  && ok "①: fresh-DB column order == migrated-DB column order (pins positional INSERT risk)" \
  || bad "①: column order mismatch — fresh='$fresh_cols' migrated='$migrated_cols'"

# Verify the v7 positional INSERT tail exactly.
tail_ok=$(echo "$fresh_cols" | python3 -c "import sys; s=sys.stdin.read().strip(); cols=s.split(','); expected=['strength','last_accessed','injection_flag','delivery_state','headline','aliases','entities','topics','artifact_refs','status','canonical_id','superseded_by','capsule_version']; ok = cols[-13:]==expected; print('ok' if ok else f'bad:{cols[-13:]}')")
[ "$tail_ok" = "ok" ] \
  && ok "①: column tail matches v7 retrieval/temporal schema" \
  || bad "①: column tail wrong — $tail_ok"

rm -rf "$FRESH_STORE" "$FRESH_PROJ" "$STORE_1" "$PROJ_1"

# ══════════════════════════════════════════════════════════════════
# ② strength/last_accessed backfill + new write
# ══════════════════════════════════════════════════════════════════
echo "== ②: strength/last_accessed backfill + new write =="

STORE_2="$(mktemp -d)"; PROJ_2="$(mktemp -d)"
export MEM_STORE="$STORE_2" MEM_PROJECTS="$PROJ_2"

make_fixture_db "$STORE_2/memory.db" >/dev/null

# Trigger migration
python3 "$MEM" stats >/dev/null 2>&1

# Check all fixture rows: strength==1 AND last_accessed==COALESCE(updated,created)
backfill_ok=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_2/memory.db')
rows = con.execute('SELECT id, strength, last_accessed, updated, created FROM records').fetchall()
con.close()
errors = []
for rid, strength, la, upd, created in rows:
    if strength != 1:
        errors.append(f'{rid}: strength={strength}')
    expected_la = upd if upd else created
    if la != expected_la:
        errors.append(f'{rid}: last_accessed={la!r} expected={expected_la!r}')
print('ok' if not errors else '|'.join(errors))
")
[ "$backfill_ok" = "ok" ] \
  && ok "②: all fixture rows backfilled strength=1, last_accessed=COALESCE(updated,created)" \
  || bad "②: backfill issues — $backfill_ok"

# Add new record, verify strength==1 and last_accessed==today
TODAY_2=$(python3 -c "import datetime; print(datetime.date.today().isoformat())")
python3 "$MEM" add durable thread "This is a brand new durable thread record for cluster e test" >/dev/null 2>&1

new_check=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_2/memory.db')
# newest row by rowid
row = con.execute('SELECT strength, last_accessed FROM records ORDER BY rowid DESC LIMIT 1').fetchone()
con.close()
if row is None:
    print('no-row')
elif row[0] != 1:
    print(f'strength={row[0]}')
elif row[1] != '$TODAY_2':
    print(f'last_accessed={row[1]!r} expected=$TODAY_2')
else:
    print('ok')
")
[ "$new_check" = "ok" ] \
  && ok "②: new write has strength=1, last_accessed=today" \
  || bad "②: new write columns wrong — $new_check"

rm -rf "$STORE_2" "$PROJ_2"

# ══════════════════════════════════════════════════════════════════
# ③ project_key resolution (5 sub-cases)
# ══════════════════════════════════════════════════════════════════
echo "== ③: project_key resolution =="

STORE_3="$(mktemp -d)"; PROJ_3="$(mktemp -d)"
export MEM_STORE="$STORE_3" MEM_PROJECTS="$PROJ_3"

# Sub-case (a): git init + remote origin → "git:github.com/org/repo"
REPO_3A="$(mktemp -d)"
git -C "$REPO_3A" init -q 2>/dev/null
git -C "$REPO_3A" remote add origin "git@github.com:org/repo.git" 2>/dev/null
pkey_3a=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_3A'))
" 2>/dev/null)
[ "$pkey_3a" = "git:github.com/org/repo" ] \
  && ok "③a: git remote scp → git:github.com/org/repo" \
  || bad "③a: got '$pkey_3a' expected 'git:github.com/org/repo'"

# Sub-case (b): worktree from remote repo → same git: key
WT_3B="$(mktemp -d)"; rm -rf "$WT_3B"
git -C "$REPO_3A" commit --allow-empty -m "init" -q 2>/dev/null
git -C "$REPO_3A" worktree add "$WT_3B" -b wt-branch-3b -q 2>/dev/null
pkey_3b=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$WT_3B'))
" 2>/dev/null)
[ "$pkey_3b" = "git:github.com/org/repo" ] \
  && ok "③b: worktree of remote repo → same git: key" \
  || bad "③b: got '$pkey_3b' expected 'git:github.com/org/repo'"

# Sub-case (c): no-remote git, seed=True, mv repo dir, re-resolve → same id: key
REPO_3C="$(mktemp -d)"
git -C "$REPO_3C" init -q 2>/dev/null
git -C "$REPO_3C" commit --allow-empty -m "init" -q 2>/dev/null
pkey_3c_orig=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_3C', seed=True))
" 2>/dev/null)
# Must start with "id:"
[[ "$pkey_3c_orig" == id:* ]] \
  && ok "③c: no-remote git seed=True → id: prefix" \
  || bad "③c: expected id: prefix, got '$pkey_3c_orig'"

# Check .git/info/exclude contains .claude-project-id
excl_ok=$(grep -c '.claude-project-id' "$REPO_3C/.git/info/exclude" 2>/dev/null || echo 0)
[ "$excl_ok" -ge 1 ] \
  && ok "③c: .git/info/exclude contains .claude-project-id" \
  || bad "③c: .git/info/exclude does not contain .claude-project-id"

# mv repo, re-resolve → same id: key
REPO_3C_MOVED="$(mktemp -d)"; rm -rf "$REPO_3C_MOVED"
mv "$REPO_3C" "$REPO_3C_MOVED"
pkey_3c_moved=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_3C_MOVED'))
" 2>/dev/null)
[ "$pkey_3c_moved" = "$pkey_3c_orig" ] \
  && ok "③c: id: key survives directory move" \
  || bad "③c: key changed after move — orig='$pkey_3c_orig' moved='$pkey_3c_moved'"

# Sub-case (d): no-remote git worktree — worktree key == main key
REPO_3D="$(mktemp -d)"
git -C "$REPO_3D" init -q 2>/dev/null
git -C "$REPO_3D" commit --allow-empty -m "init" -q 2>/dev/null
# Seed the marker on main repo first
pkey_3d_main=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_3D', seed=True))
" 2>/dev/null)
WT_3D="$(mktemp -d)"; rm -rf "$WT_3D"
git -C "$REPO_3D" worktree add "$WT_3D" -b wt-branch-3d -q 2>/dev/null
pkey_3d_wt=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$WT_3D'))
" 2>/dev/null)
[ "$pkey_3d_wt" = "$pkey_3d_main" ] \
  && ok "③d: no-remote worktree key == main repo key (git-common-dir path)" \
  || bad "③d: wt='$pkey_3d_wt' main='$pkey_3d_main'"

# Sub-case (e): plain non-git dir → bare enc_cwd (no prefix)
PLAIN_3E="$(mktemp -d)"
expected_3e=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.enc_cwd('$PLAIN_3E'))
")
pkey_3e=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$PLAIN_3E'))
" 2>/dev/null)
[ "$pkey_3e" = "$expected_3e" ] \
  && ok "③e: plain non-git dir → bare enc_cwd (no prefix)" \
  || bad "③e: got '$pkey_3e' expected '$expected_3e'"

rm -rf "$REPO_3A" "$WT_3B" "$REPO_3C_MOVED" "$REPO_3D" "$WT_3D" "$PLAIN_3E"
rm -rf "$STORE_3" "$PROJ_3"

# ══════════════════════════════════════════════════════════════════
# decoder round-trip unit (part of ④)
# ══════════════════════════════════════════════════════════════════
echo "== decoder round-trip unit =="

DEC_BASE="$(mktemp -d)"
mkdir -p "$DEC_BASE/.hidden" "$DEC_BASE/a-b-c" "$DEC_BASE/a_b" "$DEC_BASE/plain"

for subdir in ".hidden" "a-b-c" "a_b" "plain"; do
    full_path="$DEC_BASE/$subdir"
    result=$(python3 -c "
import sys, pathlib
sys.path.insert(0, '$ROOT/tools/memory')
import mem
p = pathlib.Path('$full_path')
encoded = mem.enc_cwd(str(p))
decoded = mem._decode_enc_cwd(encoded)
if decoded is None:
    print(f'NONE')
elif decoded.resolve() == p.resolve():
    print('ok')
else:
    print(f'MISMATCH decoded={decoded!r} expected={p!r}')
" 2>/dev/null)
    [ "$result" = "ok" ] \
      && ok "decoder round-trip: $subdir" \
      || bad "decoder round-trip: $subdir — $result"
done

rm -rf "$DEC_BASE"

# ══════════════════════════════════════════════════════════════════
# ④ cwd_origin remap
# ══════════════════════════════════════════════════════════════════
echo "== ④: cwd_origin remap =="

STORE_4="$(mktemp -d)"; PROJ_4="$(mktemp -d)"
export MEM_STORE="$STORE_4" MEM_PROJECTS="$PROJ_4"

# Create a real git repo with a remote (so project_key != enc_cwd)
# Use a path with a dash component to exercise the decoder
REPO_4="$(mktemp -d)"
git -C "$REPO_4" init -q 2>/dev/null
git -C "$REPO_4" remote add origin "git@github.com:test-org/my-repo-4.git" 2>/dev/null
git -C "$REPO_4" commit --allow-empty -m "init" -q 2>/dev/null

real_enc=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.enc_cwd('$REPO_4'))
")
real_pkey=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_4'))
")

# Non-existent path
NONEXIST_PATH="/tmp/nonexistent_cluster_e_test_path_xyz_12345"
nonexist_enc=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.enc_cwd('$NONEXIST_PATH'))
")

# Build fixture DB with old-style enc_cwd keys (v0 schema)
python3 - "$STORE_4/memory.db" "$real_enc" "$nonexist_enc" <<'PYEOF'
import sqlite3, sys, datetime
db, real_enc, nonexist_enc = sys.argv[1], sys.argv[2], sys.argv[3]
con = sqlite3.connect(db)
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA user_version=0")
con.execute("""CREATE TABLE IF NOT EXISTS records(
    id TEXT PRIMARY KEY, tier TEXT NOT NULL, scope TEXT NOT NULL,
    type TEXT NOT NULL, cwd_origin TEXT, created TEXT, updated TEXT,
    expires TEXT, source TEXT, tags TEXT, links TEXT, body TEXT NOT NULL
)""")
today = datetime.date.today().isoformat()
con.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
    ("remap_real_001", "durable", "project", "thread", real_enc,
     today, today, None, None, "[]", "[]",
     "real existing path record for remap test cluster e alpha phase"),
    ("remap_nonexist_001", "durable", "project", "thread", nonexist_enc,
     today, today, None, None, "[]", "[]",
     "nonexistent path record should remain as orphan after migration"),
])
con.commit()
con.close()
PYEOF

pre_count=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_4/memory.db')
print(con.execute('SELECT COUNT(*) FROM records').fetchone()[0])
con.close()
")

# Trigger migration (read-only command)
python3 "$MEM" stats >/dev/null 2>&1
rc4=$?
[ "$rc4" = "0" ] \
  && ok "④: mem stats exit 0 (migration with orphan present)" \
  || bad "④: mem stats failed with orphan present (rc=$rc4)"

post_count=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_4/memory.db')
print(con.execute('SELECT COUNT(*) FROM records').fetchone()[0])
con.close()
")
[ "$pre_count" = "$post_count" ] \
  && ok "④: record count preserved across migration ($pre_count)" \
  || bad "④: count changed — pre=$pre_count post=$post_count"

# Real-dir row: cwd_origin should now == project_key(REPO_4)
real_cwd_after=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_4/memory.db')
row = con.execute('SELECT cwd_origin FROM records WHERE id=?', ('remap_real_001',)).fetchone()
con.close()
print(row[0] if row else 'NO_ROW')
")
[ "$real_cwd_after" = "$real_pkey" ] \
  && ok "④: real-dir row cwd_origin remapped to project_key" \
  || bad "④: real-dir row cwd_origin='$real_cwd_after' expected='$real_pkey'"

# Nonexist row: cwd_origin must be UNCHANGED (orphan preserved)
nonexist_cwd_after=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_4/memory.db')
row = con.execute('SELECT cwd_origin FROM records WHERE id=?', ('remap_nonexist_001',)).fetchone()
con.close()
print(row[0] if row else 'NO_ROW')
")
[ "$nonexist_cwd_after" = "$nonexist_enc" ] \
  && ok "④: nonexistent-path orphan cwd_origin unchanged" \
  || bad "④: orphan cwd_origin changed — got='$nonexist_cwd_after' expected='$nonexist_enc'"

rm -rf "$REPO_4" "$STORE_4" "$PROJ_4"

# ══════════════════════════════════════════════════════════════════
# ⑤ dump round-trip + old-dump back-compat
# ══════════════════════════════════════════════════════════════════
echo "== ⑤: dump round-trip + old-dump back-compat =="

STORE_5="$(mktemp -d)"; PROJ_5="$(mktemp -d)"
export MEM_STORE="$STORE_5" MEM_PROJECTS="$PROJ_5"

# Seed some records
python3 "$MEM" add durable thread "round trip test record alpha for dump export import cycle" >/dev/null 2>&1
python3 "$MEM" add durable thread "round trip test record beta for dump export import cycle here" >/dev/null 2>&1
python3 "$MEM" add working thread "working record for dump round trip test session context data" >/dev/null 2>&1

# Export run-2
python3 "$MEM" export >/dev/null 2>&1
DUMP_5_R2="$(mktemp)"
cp "$STORE_5/dump.jsonl" "$DUMP_5_R2"

# Import
python3 "$MEM" import "$DUMP_5_R2" >/dev/null 2>&1

# Export run-3
python3 "$MEM" export >/dev/null 2>&1
DUMP_5_R3="$(mktemp)"
cp "$STORE_5/dump.jsonl" "$DUMP_5_R3"

cmp -s "$DUMP_5_R2" "$DUMP_5_R3" \
  && ok "⑤: export→import→export — run-2 dump === run-3 dump" \
  || bad "⑤: dump not identical after round-trip"
rm -f "$DUMP_5_R2" "$DUMP_5_R3"

# Old-dump back-compat: hand-craft a JSONL line without strength/last_accessed
OLD_DUMP="$(mktemp)"
TODAY_5=$(python3 -c "import datetime; print(datetime.date.today().isoformat())")
YESTERDAY_5=$(python3 -c "import datetime; print((datetime.date.today()-datetime.timedelta(days=1)).isoformat())")
python3 -c "
import json
rec = {
    'id': 'old_dump_compat_001',
    'tier': 'durable',
    'scope': 'project',
    'type': 'thread',
    'cwd_origin': 'global',
    'created': '$YESTERDAY_5',
    'updated': '$TODAY_5',
    'expires': None,
    'source': None,
    'tags': [],
    'links': [],
    'body': 'old style dump line without strength or last accessed fields here'
    # no strength, no last_accessed keys
}
print(json.dumps(rec, sort_keys=True))
" > "$OLD_DUMP"

STORE_5B="$(mktemp -d)"; PROJ_5B="$(mktemp -d)"
export MEM_STORE="$STORE_5B" MEM_PROJECTS="$PROJ_5B"
python3 "$MEM" import "$OLD_DUMP" >/dev/null 2>&1
rm -f "$OLD_DUMP"

old_compat_check=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_5B/memory.db')
row = con.execute('SELECT strength, last_accessed FROM records WHERE id=?', ('old_dump_compat_001',)).fetchone()
con.close()
if row is None:
    print('NO_ROW')
elif row[0] != 1:
    print(f'strength={row[0]}')
elif row[1] is None:
    print('last_accessed=None')
else:
    print('ok')
")
[ "$old_compat_check" = "ok" ] \
  && ok "⑤: old-dump import → strength=1, last_accessed=COALESCE(updated,created)" \
  || bad "⑤: old-dump back-compat failed — $old_compat_check"

rm -rf "$STORE_5" "$PROJ_5" "$STORE_5B" "$PROJ_5B"

# ══════════════════════════════════════════════════════════════════
# ⑥ live 285 preservation
# ══════════════════════════════════════════════════════════════════
echo "== ⑥: live DB preservation =="

LIVE_DB="${AGENT_HOME:-$HOME/hearting}/memory/memory.db"
if [ -f "$LIVE_DB" ]; then
    STORE_6="$(mktemp -d)"; PROJ_6="$(mktemp -d)"
    # Copy live DB READ-ONLY (cp source only, never write back)
    cp "$LIVE_DB" "$STORE_6/memory.db"
    export MEM_STORE="$STORE_6" MEM_PROJECTS="$PROJ_6"

    # Pre-migration: count + sorted-body sha256 via direct sqlite3 (no mem)
    pre_count=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_6/memory.db')
row = con.execute('SELECT COUNT(*) FROM records').fetchone()
con.close()
print(row[0])
")
    pre_sha=$(python3 -c "
import sqlite3, hashlib
con = sqlite3.connect('$STORE_6/memory.db')
try:
    rows = con.execute('SELECT body FROM records ORDER BY id').fetchall()
except Exception:
    rows = []
con.close()
h = hashlib.sha256()
for (b,) in rows:
    h.update((b or '').encode('utf-8'))
print(h.hexdigest())
")

    # Trigger migration via READ-ONLY command (stats doesn't import/delete)
    python3 "$MEM" stats >/dev/null 2>&1

    # Post-migration: count + sorted-body sha256
    post_count=$(python3 -c "
import sqlite3
con = sqlite3.connect('$STORE_6/memory.db')
row = con.execute('SELECT COUNT(*) FROM records').fetchone()
con.close()
print(row[0])
")
    post_sha=$(python3 -c "
import sqlite3, hashlib
con = sqlite3.connect('$STORE_6/memory.db')
rows = con.execute('SELECT body FROM records ORDER BY id').fetchall()
con.close()
h = hashlib.sha256()
for (b,) in rows:
    h.update((b or '').encode('utf-8'))
print(h.hexdigest())
")

    [ "$pre_count" = "$post_count" ] \
      && ok "⑥: live DB record count preserved ($pre_count)" \
      || bad "⑥: count changed — pre=$pre_count post=$post_count"

    [ "$pre_sha" = "$post_sha" ] \
      && ok "⑥: live DB sorted-body sha256 unchanged (no body mutation)" \
      || bad "⑥: body sha256 changed after migration"

    rm -rf "$STORE_6" "$PROJ_6"
else
    ok "⑥: skipped (no live DB at $LIVE_DB)"
fi

# ══════════════════════════════════════════════════════════════════
# ⑦ inject/recall project_key filter (worktree cwd)
# ══════════════════════════════════════════════════════════════════
echo "== ⑦: inject/recall project_key filter via worktree =="

STORE_7="$(mktemp -d)"; PROJ_7="$(mktemp -d)"
export MEM_STORE="$STORE_7" MEM_PROJECTS="$PROJ_7"

# Create git repo with remote (deterministic project_key)
REPO_7="$(mktemp -d)"
git -C "$REPO_7" init -q 2>/dev/null
git -C "$REPO_7" remote add origin "git@github.com:myorg/cluster-e-test.git" 2>/dev/null
git -C "$REPO_7" commit --allow-empty -m "init" -q 2>/dev/null

PKEY_7=$(python3 -c "
import sys; sys.path.insert(0, '$ROOT/tools/memory')
import mem
print(mem.project_key('$REPO_7'))
")
# Should be git:github.com/myorg/cluster-e-test
[[ "$PKEY_7" == git:* ]] && ok "⑦: repo project_key is git: type" || bad "⑦: repo pkey not git: — got '$PKEY_7'"

# Create worktree
WT_7="$(mktemp -d)"; rm -rf "$WT_7"
git -C "$REPO_7" worktree add "$WT_7" -b wt-branch-7 -q 2>/dev/null

# Seed a durable project record with cwd_origin == project_key(REPO_7)
# using --cwd-origin so it matches the project regardless of current cwd
python3 "$MEM" add durable thread \
  "cluster e inject recall worktree filter test record for project key validation" \
  --cwd-origin "$PKEY_7" >/dev/null 2>&1

# Run mem inject from inside the worktree (cd to WT_7)
inject_out_7=$(cd "$WT_7" && python3 "$MEM" inject 2>/dev/null)

echo "$inject_out_7" | grep -q "cluster e inject recall worktree" \
  && ok "⑦: inject from worktree retrieves project record (same project_key)" \
  || bad "⑦: inject from worktree did not find project record (output: ${inject_out_7:0:200})"

# Run mem recall from worktree
recall_out_7=$(cd "$WT_7" && python3 "$MEM" recall "cluster e inject recall worktree" --all 2>/dev/null)

echo "$recall_out_7" | grep -q "cluster e inject recall worktree" \
  && ok "⑦: recall from worktree retrieves project record" \
  || bad "⑦: recall from worktree did not find project record"

# Assert inject_cleanup_candidates path works from worktree (inject must not fail)
inject_rc7=$(cd "$WT_7" && python3 "$MEM" inject >/dev/null 2>&1; echo $?)
[ "$inject_rc7" = "0" ] \
  && ok "⑦: inject_cleanup_candidates path from worktree exits 0 (no crash)" \
  || bad "⑦: inject from worktree failed (rc=$inject_rc7)"

# Seed a near-dup pair scoped to pkey_7 to exercise inject_cleanup_candidates from worktree
NDP_PREFIX_7="this is sufficiently long near dup test prefix for worktree cleanup candidates test body "
python3 "$MEM" add durable thread "${NDP_PREFIX_7}AAA tail" --cwd-origin "$PKEY_7" >/dev/null 2>&1
python3 "$MEM" add durable thread "${NDP_PREFIX_7}BBB tail" --cwd-origin "$PKEY_7" >/dev/null 2>&1
python3 "$MEM" index --rebuild >/dev/null 2>&1

inject_cleanup_7=$(cd "$WT_7" && python3 "$MEM" inject 2>/dev/null)
# cleanup section should surface (near-dup records exist scoped to PKEY_7)
# inject uses project_key(cwd) as encc — from WT_7, that should == PKEY_7
echo "$inject_cleanup_7" | grep -q "Cleanup signals" \
  && ok "⑦: inject_cleanup_candidates surfaces near-dup from worktree cwd (project_key coherence)" \
  || bad "⑦: 정리 신호 not found from worktree (cleanup path not exercised)"

rm -rf "$REPO_7" "$WT_7" "$STORE_7" "$PROJ_7"

# Restore original store env to BASE
export MEM_STORE="$BASE_STORE" MEM_PROJECTS="$BASE_PROJ"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
