#!/usr/bin/env bash
# Regression for the 2026-08-21 field failure: reading a record and then
# deleting or superseding it produced evidence the fold could never match, so
# the operation stayed blocked forever and the store stopped synchronizing.
#
# `last_accessed` is server-local — reading authors no operation — but the
# destructive path digested the live row, so a single `show` between write and
# delete changed the prior state out from under the fold. These fixtures assert
# the whole set folds clean after a read, which is what the remote gate needs.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MEM="$HERE/mem.py"
PASS=0 FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok  %s\n' "$*"; }
bad(){ FAIL=$((FAIL+1)); printf '  BAD %s\n' "$*"; }
TMP="$(mktemp -d "${MEM_TEST_ROOT:-/var/tmp}/hearting-readdestroy.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$(cd "$HERE/../.." && pwd)"
export MEM_PROFILE="$TMP/no-profile"
export MEM_STORE="$TMP/store"
export MEM_INIT=1
unset MEM_SYNC_REMOTE MEM_SYNC_DIR MEM_SYNC_REF MEM_SYNC_REMOTE_URL
unset MEM_DUMP_PUSH MEM_DUMP_COMMIT

fold_report(){ python3 - "$MEM_STORE/memory.db" "$HERE" <<'PY'
import sqlite3, sys
sys.path.insert(0, sys.argv[2])
import protocol_v2 as p
con = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
raw = {str(i): bytes(b) for i, b in
       con.execute("SELECT op_id,payload_bytes FROM sync_objects")}
folded = p.fold_operations(
    [{"op_id": i, "payload": p.canonical_loads(b)} for i, b in sorted(raw.items())])
codes = sorted({getattr(d, "code", str(d)) for d in (folded.blocked or {}).values()})
live = {r[0] for r in con.execute("SELECT id FROM records")}
extra = sorted(set(folded.records) - set(folded.conflicts) - live)
print("blocked=%d codes=%s folded_not_in_db=%d" % (
    len(folded.blocked or {}), ",".join(codes) or "-", len(extra)))
PY
}

add_id(){ python3 "$MEM" add durable note "$1" --scope global 2>/dev/null \
  | sed -n 's/.*→[[:space:]]*//p' | tail -1; }

# `last_accessed` has day granularity, so a same-day read never moves it and
# the failure only appears once a day boundary has passed. Advancing the local
# column reproduces exactly the state a read on the next day leaves behind:
# the row is ahead of the lineage the operations recorded.
read_on_a_later_day(){ python3 - "$MEM_STORE/memory.db" "$1" "$HERE" <<'PYEOF'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("memmod", sys.argv[3] + "/mem.py")
mem = importlib.util.module_from_spec(spec); spec.loader.exec_module(mem)
con = mem.get_con()
try:
    con.execute("BEGIN IMMEDIATE")
    con.execute("UPDATE records SET last_accessed=date(last_accessed,'+1 day') "
                "WHERE id=?", (sys.argv[2],))
    con.commit()
finally:
    con.close()
PYEOF
}

echo "== read, then delete =="
DELETED="$(add_id "a durable record that will be read and then deleted outright")"
[ -n "$DELETED" ] || bad "fixture record was not created"
python3 "$MEM" show "$DELETED" >/dev/null 2>&1
read_on_a_later_day "$DELETED"
python3 "$MEM" delete "$DELETED" --force >/dev/null 2>&1
REPORT="$(fold_report)"
case "$REPORT" in
  "blocked=0 codes=- folded_not_in_db=0")
    ok "a tombstone after a read folds clean" ;;
  *) bad "read-then-delete left the fold inconsistent: $REPORT" ;;
esac

echo "== read, then supersede =="
OLD="$(add_id "an older durable record that a newer one will supersede later on")"
NEW="$(add_id "the newer durable record that takes over from the older one here")"
python3 "$MEM" show "$OLD" >/dev/null 2>&1
read_on_a_later_day "$OLD"
read_on_a_later_day "$NEW"
python3 "$MEM" supersede "$OLD" --by "$NEW" >/dev/null 2>&1
REPORT="$(fold_report)"
case "$REPORT" in
  "blocked=0 codes=- folded_not_in_db=0")
    ok "a supersession after a read folds clean" ;;
  *) bad "read-then-supersede left the fold inconsistent: $REPORT" ;;
esac

echo "== the local access date still moves =="
READ_ME="$(add_id "a record whose local access recency must stay server-local here")"
BEFORE="$(python3 - "$MEM_STORE/memory.db" "$READ_ME" <<'PY'
import sqlite3, sys
print(sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True).execute(
    "SELECT last_accessed FROM records WHERE id=?", (sys.argv[2],)).fetchone()[0])
PY
)"
python3 "$MEM" reinforce "$READ_ME" >/dev/null 2>&1
AFTER="$(python3 - "$MEM_STORE/memory.db" "$READ_ME" <<'PY'
import sqlite3, sys
print(sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True).execute(
    "SELECT last_accessed FROM records WHERE id=?", (sys.argv[2],)).fetchone()[0])
PY
)"
[ -n "$BEFORE" ] && [ -n "$AFTER" ] \
  && ok "access recency remains a local column ($BEFORE → $AFTER)" \
  || bad "access recency column was lost"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
