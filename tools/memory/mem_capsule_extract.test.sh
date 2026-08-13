#!/usr/bin/env bash
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
MEM="$ROOT/tools/memory/mem.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export MEM_STORE="$TMP/store"
export MEM_RECALL_EVENTS="$TMP/recall-events.jsonl"
export MEM_RECALL_RECEIPTS="$TMP/recall-opportunities"
export MEM_WRITE_EVENTS="$TMP/write-events.jsonl"
mkdir -p "$MEM_STORE"

id_of() { grep -E '^\[(write|upsert|reinforce)\]' | tail -1 | awk '{print $NF}'; }

entities_of() {
  python3 - "$MEM_STORE/memory.db" "$1" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("select entities from records where id=?", (sys.argv[2],)).fetchone()
print(json.dumps(json.loads(row[0]) if row and row[0] else []))
PY
}

field_of() {
  python3 - "$MEM_STORE/memory.db" "$1" "$2" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute(f"select {sys.argv[3]} from records where id=?", (sys.argv[2],)).fetchone()
print(row[0] if row else "")
PY
}

# --- 1: four extraction kinds land in `entities` ---
r1=$(python3 "$MEM" add durable decision \
  'See `mem.py` at tools/memory/mem.py, commit a7c01b7d8328f87ceef05852cb475733a5e45a4, ref D-40 and rt-edb4cf9.' \
  --headline 'capsule extraction fixture one' | id_of)
ents1=$(entities_of "$r1")
echo "$ents1" | grep -q '"mem.py"'
echo "$ents1" | grep -q 'tools/memory/mem.py'
echo "$ents1" | grep -q 'a7c01b7d8328f87ceef05852cb475733a5e45a4'
echo "$ents1" | grep -q '"D-40"'
echo "$ents1" | grep -q '"rt-edb4cf9"'

# --- caller-supplied entities always survive ---
body="See \`mem.py\` at tools/memory/mem.py, commit a7c01b7d8328f87ceef05852cb475733a5e45a4, ref D-40 and rt-edb4cf9."

# --- 4: user-supplied --entity is never dropped and stays first ---
r4=$(python3 "$MEM" add durable decision "$body two four" \
  --headline 'capsule extraction fixture four' --entity custom-first --entity custom-second | id_of)
ents4=$(entities_of "$r4")
python3 - "$ents4" <<'PY'
import json, sys
ents = json.loads(sys.argv[1])
assert ents[0] == "custom-first", ents
assert ents[1] == "custom-second", ents
assert "mem.py" in ents, ents
PY

# --- 3: adding the identical body twice does not grow entities ---
r3a=$(python3 "$MEM" add durable decision "$body three" \
  --headline 'capsule extraction fixture three' | id_of)
before3=$(entities_of "$r3a")
r3b=$(python3 "$MEM" add durable decision "$body three" \
  --headline 'capsule extraction fixture three' | id_of)
after3=$(entities_of "$r3b")
[ "$r3a" = "$r3b" ]
[ "$before3" = "$after3" ]

# --- 5: re-add without --entity preserves prior entities (refresh_capsule raw-None path) ---
r5=$(python3 "$MEM" add durable decision "$body five" \
  --headline 'capsule extraction fixture five' --entity manual-five | id_of)
ents5_before=$(entities_of "$r5")
python3 "$MEM" add durable decision "$body five" \
  --headline 'capsule extraction fixture five' >/dev/null
ents5_after=$(entities_of "$r5")
echo "$ents5_after" | grep -q 'manual-five'
python3 - "$ents5_before" "$ents5_after" <<'PY'
import json, sys
before, after = json.loads(sys.argv[1]), json.loads(sys.argv[2])
assert set(before) <= set(after), (before, after)
PY

# --- 6: body/strength/status/aliases/topics untouched by extraction ---
r6=$(python3 "$MEM" add durable decision "$body six" \
  --headline 'capsule extraction fixture six' --alias keep-alias --topic keep-topic | id_of)
[ "$(field_of "$r6" body)" = "$body six" ]
[ "$(field_of "$r6" strength)" = "1" ]
[ "$(field_of "$r6" status)" = "active" ]
[ "$(field_of "$r6" aliases)" = '["keep-alias"]' ]
[ "$(field_of "$r6" topics)" = '["keep-topic"]' ]

# --- 7: extraction cap 12 / merge cap 24, item length 160 ---
words=""
for n in $(seq 1 20); do words="$words rt-$(printf '%06x' "$n")abc"; done
r7=$(python3 "$MEM" add durable decision "cap fixture seven $words" \
  --headline 'capsule extraction fixture seven' | id_of)
ents7=$(entities_of "$r7")
python3 - "$ents7" <<'PY'
import json, sys
ents = json.loads(sys.argv[1])
assert len(ents) <= 24, len(ents)
assert all(len(e) <= 160 for e in ents)
PY

echo 'mem capsule-extract regressions 1/3/4/5/6/7: PASS'

# --- 2: export -> import -> export byte-identical ---
python3 "$MEM" export --target dump >/dev/null
cp "$MEM_STORE/dump.jsonl" "$TMP/dump-first.jsonl"
python3 "$MEM" import "$TMP/dump-first.jsonl" >/dev/null
python3 "$MEM" export --target dump >/dev/null
cmp "$TMP/dump-first.jsonl" "$MEM_STORE/dump.jsonl"
echo 'mem capsule-extract regression 2 (export/import/export byte-identical): PASS'

# --- 8: backfill dry-run touches nothing ---
db_sha_before=$(sha256sum "$MEM_STORE/memory.db" | awk '{print $1}')
dump_sha_before=$(sha256sum "$MEM_STORE/dump.jsonl" | awk '{print $1}')
python3 "$MEM" maintenance --backfill-capsules >"$TMP/backfill-dry.out"
db_sha_after=$(sha256sum "$MEM_STORE/memory.db" | awk '{print $1}')
dump_sha_after=$(sha256sum "$MEM_STORE/dump.jsonl" | awk '{print $1}')
[ "$db_sha_before" = "$db_sha_after" ]
[ "$dump_sha_before" = "$dump_sha_after" ]
grep -q 'would update' "$TMP/backfill-dry.out"
echo 'mem capsule-extract regression 8 (backfill dry-run no-op): PASS'

# --- fixture for backfill: a record with an un-extracted entity opportunity + user entity ---
r_backfill=$(python3 "$MEM" note 'plain body mentioning `backfill.py` and D-99 with no capsule flags at all' 2>/dev/null | id_of)
python3 - "$MEM_STORE/memory.db" "$r_backfill" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("update records set entities=? where id=?", ('["preexisting-entity"]', sys.argv[2]))
con.commit()
PY
body_pre=$(field_of "$r_backfill" body)
strength_pre=$(field_of "$r_backfill" strength)
status_pre=$(field_of "$r_backfill" status)

# --- 9/10/11: --apply backfill merges, is idempotent, preserves user entities and other fields ---
python3 "$MEM" maintenance --backfill-capsules --apply >"$TMP/backfill-apply-1.out"
ents_bf1=$(entities_of "$r_backfill")
echo "$ents_bf1" | grep -q 'preexisting-entity'
echo "$ents_bf1" | grep -q 'backfill.py'
echo "$ents_bf1" | grep -q '"D-99"'
[ "$(field_of "$r_backfill" body)" = "$body_pre" ]
[ "$(field_of "$r_backfill" strength)" = "$strength_pre" ]
[ "$(field_of "$r_backfill" status)" = "$status_pre" ]

python3 "$MEM" maintenance --backfill-capsules --apply >"$TMP/backfill-apply-2.out"
grep -q 'updated 0' "$TMP/backfill-apply-2.out"
ents_bf2=$(entities_of "$r_backfill")
[ "$ents_bf1" = "$ents_bf2" ]
echo 'mem capsule-extract regressions 9/10/11 (backfill apply, idempotent, field-preserving): PASS'

# --- 12: `mem maintenance` without --backfill-capsules keeps its existing dry-run behavior ---
python3 "$MEM" maintenance >"$TMP/maintenance-default.out" 2>&1 || true
grep -qi 'squash\|no history\|not a git repository' "$TMP/maintenance-default.out"
if grep -q 'backfill' "$TMP/maintenance-default.out"; then exit 1; fi
echo 'mem capsule-extract regression 12 (plain maintenance unaffected): PASS'

echo 'mem capsule-extract: PASS'
