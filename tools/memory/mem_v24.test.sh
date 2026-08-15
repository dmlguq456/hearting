#!/usr/bin/env bash
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
MEM="$ROOT/tools/memory/mem.py"
APPLIER="$ROOT/tools/memory/apply-distill-actions.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export MEM_STORE="$TMP/store"
export MEM_RECALL_EVENTS="$TMP/recall-events.jsonl"
export MEM_RECALL_RECEIPTS="$TMP/recall-opportunities"
export MEM_WRITE_EVENTS="$TMP/write-events.jsonl"
mkdir -p "$MEM_STORE"

old=$(python3 "$MEM" add durable decision \
  'The former retrieval decision is retained only as historical evidence.' \
  --headline 'Former retrieval policy' --alias legacy-capsule --topic retrieval \
  | sed -n 's/.*→ //p')
new=$(python3 "$MEM" add durable decision \
  'The current retrieval decision uses capsule-first ranking.' \
  --headline 'Current retrieval policy' --alias capsule-first \
  --entity memory-db --topic retrieval --artifact-ref '.agent_reports/spec/prd.md' \
  | sed -n 's/.*→ //p')
other=$(python3 "$MEM" add durable decision \
  'sharedword belongs to a different topic.' \
  --headline 'Other topic' --topic unrelated \
  | sed -n 's/.*→ //p')
body_only=$(python3 "$MEM" add durable decision \
  'bodyfallbacktoken appears only in the canonical body.' \
  --headline 'Compatibility fallback' \
  | sed -n 's/.*→ //p')
source_id=$(python3 "$MEM" add durable decision \
  'The original source-backed body.' --source 'v24:source-upsert' \
  | sed -n 's/.*→ //p')
python3 "$MEM" add durable decision 'The replacement source-backed body.' \
  --source 'v24:source-upsert' >/dev/null

python3 "$MEM" recall capsule-first --full | grep -q "$new"
python3 "$MEM" recall 'Current retrieval policy' | grep -q "$new"
python3 "$MEM" recall memory-db | grep -q "$new"
python3 "$MEM" recall 'agent_reports spec prd' | grep -q "$new"
python3 "$MEM" topics retrieval | grep -q "$new"
python3 "$MEM" recall sharedword --topic unrelated | grep -q "$other"
python3 "$MEM" recall sharedword --topic retrieval | grep -q '(no store matches)'
python3 "$MEM" recall bodyfallbacktoken | grep -q "$body_only"
python3 - "$MEM_STORE/memory.db" "$source_id" <<'PY'
import sqlite3, sys
row = sqlite3.connect(sys.argv[1]).execute(
    "select body,headline from records where id=?", (sys.argv[2],)).fetchone()
assert row == ("The replacement source-backed body.", "The replacement source-backed body."), row
PY
python3 "$MEM" index --rebuild >/dev/null
python3 "$MEM" recall capsule-first | grep -q "$new"
python3 "$MEM" topics retrieval | grep -q "$new"
python3 "$MEM" supersede "$old" --by "$new" >/dev/null
if python3 "$MEM" recall legacy-capsule | grep -q "$old"; then
  echo 'superseded record leaked into default recall' >&2
  exit 1
fi
python3 "$MEM" recall legacy-capsule --include-superseded | grep -q "$old"
if python3 "$MEM" show "$old" >/dev/null 2>&1; then
  echo 'superseded record leaked into default show' >&2
  exit 1
fi
python3 "$MEM" show "$old" --include-superseded | grep -q 'status: superseded'

# Supersession rejects pending, profile, cross-project, and malformed cycles.
pending=$(python3 "$MEM" add working unresolved-obligation \
  'This pending obligation must not enter supersession.' --requires-consume \
  | sed -n 's/.*→ //p')
profile=$(python3 "$MEM" add durable profile \
  'A profile is outside temporal-decision supersession.' | sed -n 's/.*→ //p')
foreign="$TMP/foreign-project"
mkdir -p "$foreign"
foreign_id=$(
  cd "$foreign"
  python3 "$MEM" add durable decision 'A foreign-project decision stays isolated.' \
    | sed -n 's/.*→ //p'
)
if python3 "$MEM" supersede "$pending" --by "$new" >/dev/null 2>&1; then
  echo 'pending supersession was not rejected' >&2
  exit 1
fi
if python3 "$MEM" supersede "$profile" --by "$new" >/dev/null 2>&1; then
  echo 'profile supersession was not rejected' >&2
  exit 1
fi
if python3 "$MEM" supersede "$new" --by "$foreign_id" >/dev/null 2>&1; then
  echo 'cross-project supersession was not rejected' >&2
  exit 1
fi
cycle_a=$(python3 "$MEM" add durable decision 'Cycle guard source.' | sed -n 's/.*→ //p')
cycle_b=$(python3 "$MEM" add durable decision 'Cycle guard target.' | sed -n 's/.*→ //p')
python3 - "$MEM_STORE/memory.db" "$cycle_a" "$cycle_b" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("update records set canonical_id=? where id=?", (sys.argv[2], sys.argv[3]))
con.commit()
PY
if python3 "$MEM" supersede "$cycle_a" --by "$cycle_b" >/dev/null 2>&1; then
  echo 'malformed-cycle supersession was not rejected' >&2
  exit 1
fi
python3 - "$MEM_STORE/memory.db" "$cycle_b" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("update records set canonical_id=id where id=?", (sys.argv[2],))
con.commit()
PY

# Recall opportunity telemetry is session/project bound and stores query hashes only.
export MEM_SID='mem-v24-session'
gate_output=$(python3 "$MEM" recall-gate --decision recall \
  --reason 'capsule regression check' --query capsule-first)
gate_id=$(printf '%s\n' "$gate_output" | sed -n 's/^\[recall-gate\] \([^ ]*\).*/\1/p')
python3 "$MEM" recall-gate --outcome applied --gate-id "$gate_id" \
  --record-id "$new" >/dev/null
python3 "$MEM" recall-gate --decision skip --reason 'isolated task has complete local context' \
  >/dev/null
python3 "$MEM" recall-gate --decision recall --reason 'verify miss telemetry' \
  --query zzzznomatchv24 >/dev/null
if MEM_SID='another-session' python3 "$MEM" recall-gate --outcome applied \
    --gate-id "$gate_id" --record-id "$new" >/dev/null 2>&1; then
  echo 'cross-session recall outcome was not rejected' >&2
  exit 1
fi
python3 - "$MEM_RECALL_EVENTS" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
gate = next(row for row in rows if row.get("event") == "recall-opportunity")
assert gate["decision"] == "recall" and gate["query_sha256"] and gate["sid"]
assert "capsule-first" not in open(sys.argv[1], encoding="utf-8").read()
assert "zzzznomatchv24" not in open(sys.argv[1], encoding="utf-8").read()
assert any(row.get("event") == "explicit-recall" and row.get("gate_id") == gate["gate_id"]
           for row in rows)
assert any(row.get("event") == "recall-opportunity" and row.get("decision") == "skip"
           for row in rows)
assert any(row.get("event") == "recall-outcome" and row.get("outcome") == "applied"
           and row.get("record_ids") for row in rows)
assert any(row.get("event") == "recall-outcome" and row.get("outcome") == "miss"
           for row in rows)
PY

# Claude's shell exposes CLAUDE_CODE_SESSION_ID. The documented no-argument
# recovery path must replace the current candidate receipt with explicit proof.
claude_sid='mem-v24-claude-session'
python3 "$MEM" candidates capsule-first --session-id "$claude_sid" \
  --turn-id 'claude-user-prompt' >/dev/null
python3 - "$MEM_RECALL_RECEIPTS" "$claude_sid" <<'PY'
import hashlib, json, pathlib, sys
key = hashlib.sha256(
    b"memory-recall-opportunity-v1\0" + sys.argv[2].encode()
).hexdigest()
value = json.loads((pathlib.Path(sys.argv[1]) / f"{key}.json").read_text())
assert value["source"] == "candidate-probe"
assert value["turn_digest"]
PY
env -u MEM_SID CLAUDE_CODE_SESSION_ID="$claude_sid" \
  python3 "$MEM" recall-gate --decision skip \
    --reason 'prompt hook recovery uses the native Claude session' --turn-id '' >/dev/null
python3 - "$MEM_RECALL_RECEIPTS" "$claude_sid" <<'PY'
import hashlib, json, pathlib, sys
key = hashlib.sha256(
    b"memory-recall-opportunity-v1\0" + sys.argv[2].encode()
).hexdigest()
value = json.loads((pathlib.Path(sys.argv[1]) / f"{key}.json").read_text())
assert value["source"] == "explicit-skip"
assert value["turn_digest"] == ""
PY

# v7 export/import round-trips source fields while derived indexes rebuild.
python3 "$MEM" export >/dev/null
roundtrip="$TMP/roundtrip"
mkdir -p "$roundtrip"
MEM_STORE="$roundtrip" python3 "$MEM" import "$MEM_STORE/dump.jsonl" >/dev/null
python3 - "$roundtrip/memory.db" "$new" <<'PY'
import json, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
row = con.execute("select headline,aliases,entities,topics,artifact_refs,status,canonical_id "
                  "from records where id=?", (sys.argv[2],)).fetchone()
assert row[0] == "Current retrieval policy"
assert json.loads(row[1]) == ["capsule-first"]
assert json.loads(row[2]) == ["memory-db"]
assert json.loads(row[3]) == ["retrieval"]
assert json.loads(row[4]) == [".agent_reports/spec/prd.md"]
assert row[5:] == ("active", sys.argv[2])
assert con.execute("select count(*) from records_capsule_fts").fetchone()[0] > 0
assert con.execute("select count(*) from record_topics where topic='retrieval'").fetchone()[0] > 0
PY

# Automatic ingress admits only the four storage purposes and requires a capsule.
cat > "$TMP/actions.jsonl" <<'EOF'
{"action":"add","tier":"durable","type":"lesson","body":"This legacy free-form type must be rejected.","headline":"Rejected","aliases":[],"entities":[],"topics":[],"artifact_refs":[]}
{"action":"add","tier":"durable","type":"artifact-pointer","body":"Read the spec when changing retrieval.","headline":"Spec pointer","aliases":[],"entities":[],"topics":["retrieval"],"artifact_refs":[]}
{"action":"add","tier":"durable","type":"user-correction","body":"The user requires an explicit recall opportunity decision.","headline":"Explicit recall gate","aliases":["recall opportunity"],"entities":["memory"],"topics":["retrieval"],"artifact_refs":[]}
EOF
before=$(python3 - "$MEM_STORE/memory.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("select count(*) from records").fetchone()[0])
PY
)
python3 "$APPLIER" "$TMP/actions.jsonl" "$MEM" --mode increment >/dev/null
after=$(python3 - "$MEM_STORE/memory.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("select count(*) from records").fetchone()[0])
PY
)
[ "$after" -eq $((before + 1)) ]

# A v6 database upgrades additively and backfills active/canonical capsule state.
legacy="$TMP/legacy"
mkdir -p "$legacy"
python3 - "$legacy/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("""create table records(
 id text primary key,tier text not null,scope text not null,type text not null,
 cwd_origin text,created text,updated text,expires text,source text,tags text,links text,
 body text not null,strength integer default 1,last_accessed text,
 injection_flag integer default 0,delivery_state text not null default 'ordinary')""")
con.execute("insert into records values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
 "legacy-id","durable","global","decision","global","2026-01-01","2026-01-01",
 None,None,"[]","[]","Legacy body becomes a retrieval headline.",1,"2026-01-01",0,"ordinary"))
con.execute("pragma user_version=6")
con.commit()
PY
MEM_STORE="$legacy" python3 "$MEM" stats >/dev/null
python3 - "$legacy/memory.db" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
assert con.execute("pragma user_version").fetchone()[0] == 10
row = con.execute("select status,canonical_id,headline from records where id='legacy-id'").fetchone()
assert row == ("active", "legacy-id", "Legacy body becomes a retrieval headline."), row
assert con.execute("select count(*) from records_capsule_fts").fetchone()[0] == 1
PY

# Routine migration scans only the current logical project; all-projects is explicit.
projects="$TMP/projects"
current_ns=$(python3 -c 'import re,sys; print(re.sub(r"[/._]", "-", sys.argv[1]))' "$ROOT")
foreign="$TMP/foreign-project"
mkdir -p "$foreign" "$projects/$current_ns/memory"
foreign_ns=$(python3 -c 'import re,sys; print(re.sub(r"[/._]", "-", sys.argv[1]))' "$foreign")
mkdir -p "$projects/$foreign_ns/memory"
printf '%s\n' 'Current project stray memory is eligible for routine recovery.' > "$projects/$current_ns/memory/current.md"
printf '%s\n' 'Foreign project stray memory requires the explicit all-projects path.' > "$projects/$foreign_ns/memory/foreign.md"
migration_store="$TMP/migration-store"
mkdir -p "$migration_store"
(
  cd "$ROOT"
  MEM_STORE="$migration_store" MEM_PROJECTS="$projects" python3 "$MEM" migrate --apply >/dev/null
)
python3 - "$migration_store/memory.db" <<'PY'
import sqlite3, sys
sources = {row[0] for row in sqlite3.connect(sys.argv[1]).execute("select source from records")}
assert any(source and source.endswith("/current.md") for source in sources), sources
assert not any(source and source.endswith("/foreign.md") for source in sources), sources
PY
(
  cd "$ROOT"
  MEM_STORE="$migration_store" MEM_PROJECTS="$projects" python3 "$MEM" migrate --apply --all-projects >/dev/null
)
python3 - "$migration_store/memory.db" <<'PY'
import sqlite3, sys
sources = {row[0] for row in sqlite3.connect(sys.argv[1]).execute("select source from records")}
assert any(source and source.endswith("/foreign.md") for source in sources), sources
PY

echo 'mem_v24: PASS'
