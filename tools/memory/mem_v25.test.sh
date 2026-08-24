#!/usr/bin/env bash
. "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/test-isolation.sh"
hearting_test_isolate
set -euo pipefail

ROOT=$(git rev-parse --show-toplevel)
MEM="$ROOT/tools/memory/mem.py"
HOOK="$ROOT/hooks/mem-recall-inject.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export MEM_STORE="$TMP/store"
export MEM_RECALL_EVENTS="$TMP/recall-events.jsonl"
export MEM_RECALL_RECEIPTS="$TMP/receipts"
export MEM_PY="$MEM"
mkdir -p "$MEM_STORE" "$TMP/project-a" "$TMP/project-b"

add_a() { (cd "$TMP/project-a" && python3 "$MEM" add "$@"); }
add_b() { (cd "$TMP/project-b" && python3 "$MEM" add "$@"); }

current=$(add_a durable decision 'This durable record keeps body-marker-current out of prompt context.' \
  --headline 'Current capsule recall policy' --alias 'automatic candidate recall' \
  --entity memory-db --topic retrieval | sed -n 's/.*→ //p')
korean=$(add_a durable user-correction 'This durable record keeps body-marker-korean hidden.' \
  --headline '이전 결정을 자동 후보로 먼저 보여준다' --alias '과거 결정 회수' \
  | sed -n 's/.*→ //p')
body_only=$(add_a durable decision 'This record contains a private bodyonlyv25 body-marker-only only in body.' \
  --headline 'Unrelated capsule headline' | sed -n 's/.*→ //p')
old=$(add_a durable decision 'This durable record keeps body-marker-old private.' --headline 'Old automatic candidate recall' \
  --alias 'automatic candidate recall' | sed -n 's/.*→ //p')
(cd "$TMP/project-a" && python3 "$MEM" supersede "$old" --by "$current" >/dev/null)
foreign=$(add_b durable decision 'This durable record keeps body-marker-foreign private.' --headline 'Foreign automatic candidate recall' \
  --alias 'automatic candidate recall' | sed -n 's/.*→ //p')
for number in 1 2 3 4 5 6 7; do
  add_a durable decision "This durable record keeps body-marker-cap-$number private." \
    --headline "Candidate cap record $number" --alias 'candidate cap shared' >/dev/null
done

before=$(python3 - "$MEM_STORE/memory.db" "$current" <<'PY'
import sqlite3, sys
con=sqlite3.connect(sys.argv[1])
con.execute("update records set last_accessed='2000-01-01' where id=?", (sys.argv[2],))
con.commit()
print(con.execute("select last_accessed from records where id=?", (sys.argv[2],)).fetchone()[0])
PY
)

(cd "$TMP/project-a" && python3 "$MEM" candidates 'automatic candidate recall' \
  --session-id session-a --turn-id turn-a --runtime test > "$TMP/english.out")
grep -q "$current" "$TMP/english.out"
if grep -Eq "$(printf '%s|%s|body-marker' "$old" "$foreign")" "$TMP/english.out"; then exit 1; fi
[ "$(grep -c '^- \[' "$TMP/english.out")" -le 6 ]
[ "$(wc -c < "$TMP/english.out")" -le 2401 ]

(cd "$TMP/project-a" && python3 "$MEM" candidates '이전에 과거 결정 회수는 어떻게 했지' \
  --session-id session-a --turn-id turn-b --runtime test > "$TMP/korean.out")
grep -q "$korean" "$TMP/korean.out"
if grep -q 'body-marker-korean' "$TMP/korean.out"; then exit 1; fi

(cd "$TMP/project-a" && python3 "$MEM" candidates bodyonlyv25 \
  --session-id session-a --turn-id turn-c --runtime test > "$TMP/body-only.out")
[ ! -s "$TMP/body-only.out" ]
if grep -q "$body_only" "$TMP/body-only.out"; then exit 1; fi

(cd "$TMP/project-a" && python3 "$MEM" candidates 'candidate cap shared' \
  --limit 99 --max-bytes 99999 --session-id session-a --turn-id turn-d \
  > "$TMP/cap.out")
[ "$(grep -c '^- \[' "$TMP/cap.out")" -eq 6 ]
[ "$(wc -c < "$TMP/cap.out")" -le 2401 ]

after=$(python3 - "$MEM_STORE/memory.db" "$current" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
    "select last_accessed from records where id=?", (sys.argv[2],)).fetchone()[0])
PY
)
[ "$before" = "$after" ]

python3 - "$MEM_RECALL_EVENTS" "$MEM_RECALL_RECEIPTS" <<'PY'
import json, pathlib, sys
raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
rows = [json.loads(line) for line in raw.splitlines()]
probes = [row for row in rows if row.get("event") == "candidate-probe"]
assert len(probes) == 4
assert all(row["query_sha256"] and row["output_utf8_bytes"] <= 2400 for row in probes)
assert "automatic candidate recall" not in raw and "과거 결정 회수" not in raw
receipts = list(pathlib.Path(sys.argv[2]).glob("*.json"))
assert len(receipts) == 1
receipt = json.loads(receipts[0].read_text())
assert receipt["schema_version"] == 1 and receipt["turn_digest"]
assert receipt["source"] == "candidate-probe" and len(receipt["result_ids"]) <= 6
PY

printf '{"hook_event_name":"UserPromptSubmit","prompt":"automatic candidate recall","cwd":"%s","session_id":"hook-session","turn_id":"hook-turn"}\n' "$TMP/project-a" \
  | "$HOOK" > "$TMP/hook.out"
python3 - "$TMP/hook.out" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
output=value["hookSpecificOutput"]
assert output["hookEventName"] == "UserPromptSubmit"
assert "Current capsule recall policy" in output["additionalContext"]
assert "body-marker" not in output["additionalContext"]
PY

printf 'not-json' | "$HOOK" > "$TMP/malformed.out" 2> "$TMP/malformed.err"
[ ! -s "$TMP/malformed.out" ] && [ ! -s "$TMP/malformed.err" ]
printf '{"hook_event_name":"UserPromptSubmit","prompt":"automatic candidate recall","cwd":"%s","session_id":"worker"}\n' "$TMP/project-a" \
  | AGENT_SESSION_ROLE=worker "$HOOK" > "$TMP/worker.out"
[ ! -s "$TMP/worker.out" ]

mkdir -p "$TMP/corrupt-store" "$TMP/corrupt-receipts"
printf 'not-a-sqlite-db\n' > "$TMP/corrupt-store/memory.db"
MEM_STORE="$TMP/corrupt-store" MEM_RECALL_EVENTS="$TMP/corrupt-events.jsonl" \
  MEM_RECALL_RECEIPTS="$TMP/corrupt-receipts" python3 "$MEM" candidates \
    'automatic candidate recall' --session-id corrupt-session --turn-id corrupt-turn \
    > "$TMP/corrupt.out"
[ ! -s "$TMP/corrupt.out" ]
[ -z "$(find "$TMP/corrupt-receipts" -type f -name '*.json' -print -quit)" ]
grep -q '"event": "candidate-probe-error"' "$TMP/corrupt-events.jsonl"

mkdir -p "$TMP/legacy-store" "$TMP/legacy-receipts"
python3 - "$TMP/legacy-store/memory.db" <<'PY'
import sqlite3, sys
con=sqlite3.connect(sys.argv[1])
con.execute("create table legacy_only(id text)")
con.commit()
PY
MEM_STORE="$TMP/legacy-store" MEM_RECALL_EVENTS="$TMP/legacy-events.jsonl" \
  MEM_RECALL_RECEIPTS="$TMP/legacy-receipts" python3 "$MEM" candidates \
    'automatic candidate recall' --session-id legacy-session --turn-id legacy-turn \
    > "$TMP/legacy.out"
[ ! -s "$TMP/legacy.out" ]
[ -z "$(find "$TMP/legacy-receipts" -type f -name '*.json' -print -quit)" ]
grep -q '"event": "candidate-probe-error"' "$TMP/legacy-events.jsonl"

echo 'memory v25 candidate recall: PASS'
