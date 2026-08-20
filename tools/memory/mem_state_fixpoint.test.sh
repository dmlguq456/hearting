#!/usr/bin/env bash
# Regressions for the 2026-08-20 v2 cutover: a stored record must equal what a
# later normalization pass would derive from it, because a protocol-v2
# post-state is compared byte-for-byte against the row it describes.
#   1) headline normalization is a fixpoint (strip, truncate, strip)
#   2) a caller-supplied injection_flag of zero is recomputed from the body
# Both fixtures are MEM_STORE-isolated; the live store is never touched.
set -uo pipefail

MEM="$(cd "$(dirname "$0")" && pwd)/mem.py"
PASS=0 FAIL=0
ok(){ PASS=$((PASS+1)); printf '  ok  %s\n' "$*"; }
bad(){ FAIL=$((FAIL+1)); printf '  BAD %s\n' "$*"; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
export AGENT_HOME="$(cd "$(dirname "$0")/../.." && pwd)"
export MEM_PROFILE="$TMP/no-profile"
unset MEM_DUMP_PUSH MEM_DUMP_COMMIT

echo "== headline normalization reaches a fixpoint =="
python3 - "$MEM" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("memmod", sys.argv[1])
mem = importlib.util.module_from_spec(spec); spec.loader.exec_module(mem)
# 240 characters ending on a space: truncating after the strip used to leave a
# trailing space that the next pass removed, so the value kept changing.
body = ("word " * 60).strip() + " " + "x" * 400
first = mem._default_headline(body)
second = mem._normalize_headline(first, body)
third = mem._normalize_headline(second, body)
assert first == second == third, (len(first), len(second), repr(first[-5:]))
assert len(first) <= 240
assert first == first.strip()
edge = "a" * 239 + "  tail"
once = mem._normalize_headline(edge, edge)
assert once == mem._normalize_headline(once, edge), repr(once[-5:])
print("fixpoint-ok")
PY
[ $? -eq 0 ] && ok "re-normalizing a stored headline returns the same bytes" \
  || bad "headline normalization is not a fixpoint"

echo "== a zero injection_flag is recomputed from the body =="
STORE="$TMP/injection-store"
MEM_STORE="$STORE" MEM_INIT=1 python3 "$MEM" add durable note \
  "guarded fixture body that mentions the system prompt on purpose" \
  --scope global >/dev/null 2>&1
FLAG="$(python3 - "$STORE/memory.db" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
    "SELECT injection_flag FROM records WHERE body LIKE '%system prompt%'"
).fetchone()[0])
PY
)"
[ "$FLAG" = "1" ] && ok "guarded body stores injection_flag=1" \
  || bad "guarded body stored injection_flag=$FLAG"

python3 - "$MEM" "$STORE" <<'PY'
import importlib.util, os, sqlite3, sys
os.environ["MEM_STORE"] = sys.argv[2]
spec = importlib.util.spec_from_file_location("memmod", sys.argv[1])
mem = importlib.util.module_from_spec(spec); spec.loader.exec_module(mem)
body = "another guarded body that says system prompt out loud"
params = mem._meta_to_params({"id": "fixture", "tier": "durable",
    "scope": "global", "type": "note", "injection_flag": 0}, body)
index = mem.RECORD_COLS.index("injection_flag")
assert params[index] == 1, params[index]
print("recompute-ok")
PY
[ $? -eq 0 ] && ok "an explicit zero does not survive a guarded body" \
  || bad "explicit injection_flag=0 was trusted for a guarded body"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
