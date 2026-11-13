#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../run-tests.py" --direct-run bash "$0" "$@"
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
MEM="$ROOT/tools/memory/mem.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export MEM_STORE="$TMP/store"
export MEM_RECALL_EVENTS="$TMP/recall-events.jsonl"
export MEM_RECALL_RECEIPTS="$TMP/receipts"
mkdir -p "$MEM_STORE" "$TMP/project-a" "$TMP/project-b"

add_a() { (cd "$TMP/project-a" && python3 "$MEM" add "$@" | sed -n 's/.*→ //p'); }
add_b() { (cd "$TMP/project-b" && python3 "$MEM" add "$@" | sed -n 's/.*→ //p'); }
boot() { (cd "$TMP/project-a" && python3 "$MEM" bootstrap-context --cwd "$TMP/project-a" "$@"); }

long_body=$(python3 -c "print('longmarker ' + 'x' * 3000)")
first=$(add_a durable feedback 'firstgroupmarker report layout prefers html tables.' --headline 'first group pref')
second=$(add_a durable decision 'secondgroupmarker project decision on figures.' --headline 'second group decision')
long=$(add_a durable feedback "$long_body" --headline 'long body pref longmarker')
foreign=$(add_b durable feedback 'firstgroupmarker foreign project preference.' --headline 'first group foreign')
fact=$(add_a durable fact 'firstgroupmarker a plain fact record.' --headline 'first group fact')
old=$(add_a durable feedback 'firstgroupmarker superseded preference.' --headline 'first group old')
(cd "$TMP/project-a" && python3 "$MEM" supersede "$old" --by "$first" >/dev/null)
for n in 1 2 3 4 5 6 7; do
  add_a durable feedback "capmarker preference number $n." --headline "capmarker pref $n" >/dev/null
done

# Baseline of everything a read path must not change.
snapshot() {
  python3 - "$MEM_STORE/memory.db" <<'PY'
import hashlib, sqlite3, sys
con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
rows = con.execute("select id,last_accessed,strength,status from records order by id").fetchall()
print(hashlib.sha256(repr(rows).encode()).hexdigest())
PY
}
before=$(snapshot)

# 1) query groups rank in order, scope/type/status fences hold
boot --query 'firstgroupmarker' --query 'secondgroupmarker' > "$TMP/order.out"
grep -q "$first" "$TMP/order.out"
grep -q "$second" "$TMP/order.out"
first_line=$(grep -n "$first" "$TMP/order.out" | head -1 | cut -d: -f1)
second_line=$(grep -n "$second" "$TMP/order.out" | head -1 | cut -d: -f1)
[ "$first_line" -lt "$second_line" ]
if grep -Eq "$foreign|$fact|$old" "$TMP/order.out"; then echo "fence leak" >&2; exit 1; fi
head -1 "$TMP/order.out" | grep -q '^# Saved preferences'
grep -q 'firstgroupmarker report layout' "$TMP/order.out"

# 2) limit and byte caps; long body is truncated
boot --query 'capmarker' --limit 3 > "$TMP/limit.out"
[ "$(grep -c '^## \[' "$TMP/limit.out")" -le 3 ]
boot --query 'longmarker' > "$TMP/long.out"
grep -q '… (truncated)' "$TMP/long.out"
[ "$(wc -c < "$TMP/long.out")" -le 1900 ]
boot --query 'capmarker' --query 'longmarker' --query 'firstgroupmarker' --limit 6 --max-bytes 500 > "$TMP/bytes.out"
[ "$(wc -c < "$TMP/bytes.out")" -le 500 ]
boot --query 'capmarker' --limit 99 --max-bytes 999999 > "$TMP/clamp.out"
[ "$(grep -c '^## \[' "$TMP/clamp.out")" -le 6 ]
[ "$(wc -c < "$TMP/clamp.out")" -le 8192 ]

# 3) no side effects: no rows touched, no events, no receipts
[ "$(snapshot)" = "$before" ]
[ ! -e "$MEM_RECALL_EVENTS" ]
[ ! -e "$MEM_RECALL_RECEIPTS" ]

# 4) no query, no match: silent
[ -z "$(boot)" ]
[ -z "$(boot --query 'zzznomatchzzz')" ]

# 5) missing / broken / capsule-less store: silent, rc 0, nothing created
EMPTY="$TMP/empty-store"; mkdir -p "$EMPTY"
out=$(MEM_STORE="$EMPTY" python3 "$MEM" bootstrap-context --cwd "$TMP/project-a" --query firstgroupmarker)
[ -z "$out" ]
[ ! -e "$EMPTY/memory.db" ]
BROKEN="$TMP/broken-store"; mkdir -p "$BROKEN"
head -c 4096 /dev/urandom > "$BROKEN/memory.db"
out=$(MEM_STORE="$BROKEN" python3 "$MEM" bootstrap-context --cwd "$TMP/project-a" --query firstgroupmarker)
[ -z "$out" ]
NOFTS="$TMP/nofts-store"; mkdir -p "$NOFTS"
python3 - "$NOFTS/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("create table records(id text primary key, body text)")
con.commit()
PY
out=$(MEM_STORE="$NOFTS" python3 "$MEM" bootstrap-context --cwd "$TMP/project-a" --query firstgroupmarker)
[ -z "$out" ]

echo "mem bootstrap-context: ok"
