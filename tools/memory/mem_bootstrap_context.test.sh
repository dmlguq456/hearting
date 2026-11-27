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
# ...and no exchange bookkeeping: a pending sync-failure notice stays for the next foreground
# command (the read must not consume it), and no schedule lock appears in the store
printf '%s' '{"failure_notice": {"reason": "remote-unavailable"}}' > "$MEM_STORE/.exchange-state.json"
state_before=$(sha256sum < "$MEM_STORE/.exchange-state.json")
boot --query 'firstgroupmarker' --topic 'unit:qa/code-review' --topic 'field:code' > /dev/null
[ "$(sha256sum < "$MEM_STORE/.exchange-state.json")" = "$state_before" ]
[ ! -e "$MEM_STORE/.exchange-schedule.lock" ]
rm -f "$MEM_STORE/.exchange-state.json"

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

# 6) exact label lookup: only labelled records, unit before field, fences and caps
before=$(snapshot)
lu=$(add_a durable preference 'unitlabelmarker unit-level way of working.' --headline 'unit way' --topic 'unit:qa/code-review')
lf=$(add_a durable preference 'fieldlabelmarker field-level way of working.' --headline 'field way' --topic 'field:code')
lb=$(add_a durable user-correction 'bothlabelmarker covers unit and field.' --headline 'both way' --topic 'Field:Code' --topic 'unit:qa/code-review')
lx=$(add_a durable preference 'otherlabelmarker belongs to another field.' --headline 'other way' --topic 'field:docs')
lfact=$(add_a durable fact 'factlabelmarker is not an instruction record.' --headline 'fact way' --topic 'field:code')
lforeign=$(add_b durable preference 'foreignlabelmarker other project way.' --headline 'foreign way' --topic 'field:code')
(cd "$TMP/project-a" && python3 "$MEM" add durable preference 'plainmarker has no label at all.' --headline 'plain way' >/dev/null)
boot --topic 'field:code' --topic 'unit:qa/code-review' > "$TMP/topic.out"
for id in "$lu" "$lf" "$lb"; do grep -q "$id" "$TMP/topic.out"; done
if grep -Eq "$lx|$lfact|$lforeign|plainmarker" "$TMP/topic.out"; then echo "label leak" >&2; exit 1; fi
[ "$(grep -c '^## \[' "$TMP/topic.out")" -eq 3 ]
# both unit-labelled records come before the field-only record
[ "$(grep -n "$lf" "$TMP/topic.out" | head -1 | cut -d: -f1)" -gt "$(grep -n "$lu" "$TMP/topic.out" | head -1 | cut -d: -f1)" ]
[ "$(grep -n "$lf" "$TMP/topic.out" | head -1 | cut -d: -f1)" -gt "$(grep -n "$lb" "$TMP/topic.out" | head -1 | cut -d: -f1)" ]
# the same label puts the record in and finds it: ':' and '/' survive, case folds
[ "$(python3 - "$MEM_STORE/memory.db" "$lb" <<'PY'
import sqlite3, sys
con = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print(",".join(r[0] for r in con.execute("select topic from record_topics where record_id=? order by topic", (sys.argv[2],))))
PY
)" = "field:code,unit:qa/code-review" ]
boot --topic 'UNIT:QA/Code-Review' | grep -q "$lu"
# a label match outranks a query match, and a query still fills the remainder
boot --topic 'unit:qa/code-review' --query 'firstgroupmarker' > "$TMP/mixed.out"
[ "$(grep -n "$lu" "$TMP/mixed.out" | head -1 | cut -d: -f1)" -lt "$(grep -n "$first" "$TMP/mixed.out" | head -1 | cut -d: -f1)" ]
# caps: four bodies at most, 6144 bytes, and the explicit limit
for n in 1 2 3 4 5 6; do add_a durable preference "caplabelmarker number $n." --topic 'field:cap' >/dev/null; done
boot --topic 'field:cap' > "$TMP/captopic.out"
[ "$(grep -c '^## \[' "$TMP/captopic.out")" -eq 4 ]
[ "$(wc -c < "$TMP/captopic.out")" -le 6144 ]
boot --topic 'field:cap' --limit 2 | grep -c '^## \[' | grep -qx 2
add_a durable preference "$long_body" --topic 'field:long' >/dev/null
boot --topic 'field:long' > "$TMP/longtopic.out"
grep -q '… (truncated)' "$TMP/longtopic.out"
[ "$(wc -c < "$TMP/longtopic.out")" -le 1900 ]
# unknown label: empty, rc 0; broken store with a label: empty, rc 0
[ -z "$(boot --topic 'unit:no/such')" ]
out=$(MEM_STORE="$BROKEN" python3 "$MEM" bootstrap-context --cwd "$TMP/project-a" --topic field:code)
[ -z "$out" ]
# without --topic the output is byte-identical to before the labelled records existed
boot --query 'firstgroupmarker' --query 'secondgroupmarker' > "$TMP/order2.out"
cmp "$TMP/order.out" "$TMP/order2.out"
# the label path is read-only too (the new records were added after the baseline)
snapshot_after=$(snapshot)
boot --topic 'field:code' >/dev/null
[ "$(snapshot)" = "$snapshot_after" ]
[ ! -e "$MEM_RECALL_EVENTS" ]
[ ! -e "$MEM_RECALL_RECEIPTS" ]
# 7) the label path keeps the same fence as the query path, and orders by strength then update
# (rows are edited in the isolated test store only, to make states the CLI cannot add directly)
setdb() { python3 - "$MEM_STORE/memory.db" "$@" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute(sys.argv[2], tuple(sys.argv[3:]))
con.commit()
PY
}
fk=$(add_a durable preference 'fencekeepmarker is active and clean.' --topic 'field:fence')
fold=$(add_a durable preference 'fenceoldmarker will be superseded.' --topic 'field:fence')
(cd "$TMP/project-a" && python3 "$MEM" supersede "$fold" --by "$fk" >/dev/null)
finj=$(add_a durable preference 'fenceinjmarker carries an injection mark.' --topic 'field:fence')
setdb "UPDATE records SET injection_flag=1 WHERE id=?" "$finj"
fexp=$(add_a durable preference 'fenceexpmarker is past its expiry.' --topic 'field:fence')
setdb "UPDATE records SET expires='2000-01-01' WHERE id=?" "$fexp"
fok=$(add_a durable preference 'fencefuturemarker expires in the future.' --topic 'field:fence')
setdb "UPDATE records SET expires='2999-01-01' WHERE id=?" "$fok"
boot --topic 'field:fence' > "$TMP/fence.out"
grep -q "$fk" "$TMP/fence.out"
grep -q "$fok" "$TMP/fence.out"
if grep -Eq "$fold|$finj|$fexp" "$TMP/fence.out"; then echo "label fence leak" >&2; exit 1; fi
[ "$(grep -c '^## \[' "$TMP/fence.out")" -eq 2 ]
# one label, same unit rank: higher strength first, then the more recently updated
rlow=$(add_a durable preference 'rankmarker low strength.' --topic 'field:rank')
rhigh=$(add_a durable preference 'rankmarker high strength.' --topic 'field:rank')
rold=$(add_a durable preference 'rankmarker same strength, older update.' --topic 'field:rank')
rnew=$(add_a durable preference 'rankmarker same strength, newer update.' --topic 'field:rank')
setdb "UPDATE records SET strength=1,updated='2030-01-01' WHERE id=?" "$rlow"
setdb "UPDATE records SET strength=9,updated='2030-01-01' WHERE id=?" "$rhigh"
setdb "UPDATE records SET strength=5,updated='2030-01-01' WHERE id=?" "$rold"
setdb "UPDATE records SET strength=5,updated='2030-02-01' WHERE id=?" "$rnew"
boot --topic 'field:rank' --limit 4 > "$TMP/rank.out"
line() { grep -n "$1" "$TMP/rank.out" | head -1 | cut -d: -f1; }
[ "$(line "$rhigh")" -lt "$(line "$rnew")" ]
[ "$(line "$rnew")" -lt "$(line "$rold")" ]
[ "$(line "$rold")" -lt "$(line "$rlow")" ]
# lookup time on the test store
python3 - "$MEM" "$TMP/project-a" <<'PY'
import subprocess, sys, time
t = time.monotonic()
subprocess.run([sys.executable, sys.argv[1], "bootstrap-context", "--cwd", sys.argv[2],
                "--topic", "unit:qa/code-review", "--topic", "field:code"],
               check=True, capture_output=True)
print("label lookup incl. interpreter start: %.3fs" % (time.monotonic() - t))
PY

echo "mem bootstrap-context: ok"
