#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cli=$root/utilities/cairn-artifact-read.sh
cairn=${CAIRN_W3A_ROOT:-/home/example/cairn}
tsx=$cairn/node_modules/.bin/tsx

tmp=$(mktemp -d)
server_pid=
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT HUP INT TERM
mkdir -p "$tmp/fixture-root"

assert_json_object() {
  python3 - "$1" "$2" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
decoder = json.JSONDecoder()
value, end = decoder.raw_decode(raw.lstrip())
assert isinstance(value, dict)
assert not raw.lstrip()[end:].strip(), raw
expected = sys.argv[2]
if expected != "-":
    assert value.get("error", {}).get("code") == expected, value
PY
}

# The wrapper's own bootstrap contract needs no Cairn checkout, so it runs everywhere.
set +e
env -u CAIRN_ROOT "$cli" >"$tmp/out" 2>"$tmp/err" </dev/null
status=$?
set -e
test "$status" -eq 18
assert_json_object "$tmp/out" INTERNAL_FAILURE

mkdir -p "$tmp/no-runtime"
set +e
env -u CAIRN_TSX CAIRN_ROOT="$tmp/no-runtime" "$cli" >"$tmp/out" 2>"$tmp/err" </dev/null
status=$?
set -e
test "$status" -eq 18
assert_json_object "$tmp/out" INTERNAL_FAILURE

set +e
env CAIRN_ROOT="$tmp/no-runtime" "$cli" --apply >"$tmp/out" 2>"$tmp/err" </dev/null
status=$?
set -e
test "$status" -eq 4
assert_json_object "$tmp/out" INVALID_REQUEST

# The read contract itself runs against a real Cairn W3a checkout (CAIRN_W3A_ROOT).
# CI and isolated runs have none; say so instead of failing on a missing sibling repo.
if [ ! -x "$tsx" ]; then
  echo "cairn artifact read bootstrap contract: ok"
  echo "skip - Cairn W3a read contract: no Cairn checkout with node_modules at $cairn (set CAIRN_W3A_ROOT)"
  exit 0
fi
# Trust exactly that checkout (it may belong to another uid): the isolated test
# HOME trusts only hearting, and the client checks the Cairn revision with git.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$cairn"
git -C "$cairn" merge-base --is-ancestor 1fa0d99e4b714b5ce305f78c8f7c7773255e8f87 HEAD

cat >"$tmp/server.py" <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RETRYABLE = {"TIMEOUT", "STALE_SNAPSHOT", "STALE_PROJECTION", "UNAVAILABLE_PROJECTION"}

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(size))
        if self.headers.get("authorization") != "Bearer w3b-read-token-secret":
            code = "AUTH_FAILED"
        else:
            query = request.get("query", "")
            code = query.split(":", 1)[1] if query.startswith("error:") else ""
        if code:
            body = {"code": code, "detail": "secret-response-body", "retryable": code in RETRYABLE,
                    "observed_at": "2026-08-21T00:00:00Z"}
            encoded = json.dumps(body).encode()
            self.send_response(400)
        else:
            encoded = json.dumps({
                "namespace_id": "ns-a",
                "namespace_state": "active",
                "is_active": True,
                "envelope_id": "env-a",
                "snapshot_id": "snap-a",
                "retrieval_mode": "projection",
                "verify": "metadata",
                "total_estimate": 1,
                "next_cursor": None,
                "telemetry_ref": "telemetry-a",
                "rows": ([{"artifact_root_id": request["artifact_root_id"],
                           "stable_id": "art-fixture", "integrity": {"verified": True,
                           "expected_digest": "sha256:" + "a" * 64},
                           "freshness": {"stale": False}}]
                         if request.get("query") == "with-row" else
                         [{"artifact_root_id": request["artifact_root_id"]}]),
            }).encode()
            self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def log_message(self, *_args):
        pass

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w", encoding="utf-8") as out:
    out.write(str(server.server_port))
server.serve_forever()
PY

python3 "$tmp/server.py" "$tmp/port" &
server_pid=$!
i=0
while [ ! -s "$tmp/port" ]; do
  i=$((i + 1)); [ "$i" -lt 100 ] || { echo 'fixture server did not start' >&2; exit 1; }
  sleep 0.05
done
endpoint=http://127.0.0.1:$(cat "$tmp/port")/artifact-read

cat >"$tmp/codes.ts" <<EOF
import { READ_ERROR_CODES, EXIT_CODES } from "${cairn}/lib/artifact-projection/read/errors.ts";
for (const code of READ_ERROR_CODES) console.log(code + "\t" + EXIT_CODES[code]);
EOF
"$tsx" "$tmp/codes.ts" >"$tmp/codes.tsv"
test "$(wc -l <"$tmp/codes.tsv" | tr -d ' ')" = 17
test "$(cut -f1 "$tmp/codes.tsv" | paste -sd, -)" = "AUTH_FAILED,FORBIDDEN,INVALID_REQUEST,INVALID_CURSOR,MISSING_TARGET,UNREADABLE_TARGET,STALE_SNAPSHOT,STALE_PROJECTION,VERSION_MISMATCH,DIGEST_MISMATCH,BROKEN_POINTER,TIMEOUT,UNAVAILABLE_PROJECTION,LEGACY_MAPPING_MISSING,LEGACY_MAPPING_MISMATCH,CONFLICT_QUARANTINED,INTERNAL_FAILURE"
test "$(cut -f2 "$tmp/codes.tsv" | paste -sd, -)" = "2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18"

invoke() {
  expected_exit=$1
  expected_code=$2
  input=$3
  set +e
  printf '%s' "$input" | env AGENT_ARTIFACT_ROOT="$tmp/fixture-root" \
    AGENT_DISPATCH_JOBS="$tmp/fixture-jobs.log" CAIRN_ROOT="$cairn" \
    CAIRN_READ_ENDPOINT="$endpoint" CAIRN_READ_TOKEN=w3b-read-token-secret \
    "$cli" >"$tmp/out" 2>"$tmp/err"
  status=$?
  set -e
  test "$status" -eq "$expected_exit" || { cat "$tmp/out" >&2; cat "$tmp/err" >&2; return 1; }
  assert_json_object "$tmp/out" "$expected_code"
  ! grep -Eq 'w3b-read-token-secret|secret-response-body|CAIRN_READ_ENDPOINT|database_url' "$tmp/out" "$tmp/err"
}

invoke 0 - '{"artifact_root_id":"root-a","resolve_active":true,"query":"ok"}'
python3 - "$tmp/out" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value == {
    "namespace_id": "ns-a",
    "namespace_state": "active",
    "is_active": True,
    "envelope_id": "env-a",
    "snapshot_id": "snap-a",
    "retrieval_mode": "projection",
    "verify": "metadata",
    "total_estimate": 1,
    "next_cursor": None,
    "telemetry_ref": "telemetry-a",
    "rows": [{"artifact_root_id": "root-a"}],
    "hearting_verification_display": [],
}, value
PY

invoke 0 - '{"artifact_root_id":"root-a","resolve_active":true,"query":"with-row"}'
python3 - "$tmp/out" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["rows"][0]["stable_id"] == "art-fixture", value
display = value["hearting_verification_display"]
assert len(display) == 1 and display[0]["stable_id"] == "art-fixture", value
assert display[0]["payload"]["verification"]["verdict"] == "unresolved", value
assert display[0]["payload"]["verification"]["reason"] == "remote-root-binding-mismatch", value
PY

while IFS="	" read -r code status; do
  invoke "$status" "$code" "{\"artifact_root_id\":\"root-a\",\"resolve_active\":true,\"query\":\"error:$code\"}"
done <"$tmp/codes.tsv"

invoke 4 INVALID_REQUEST '{not-json'
invoke 4 INVALID_REQUEST '{"artifact_root_id":"root-a","resolve_active":true,"nested":{"apply":true}}'

echo 'cairn artifact read contract: ok'
