#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cli=$root/utilities/cairn-artifact-read.sh
cairn=${CAIRN_W3A_ROOT:-/home/nas/user/Uihyeop/personal/cairn}
tsx=$cairn/node_modules/.bin/tsx
[ -x "$tsx" ] || { echo "missing Cairn W3a tsx runtime: $tsx" >&2; exit 1; }
git -C "$cairn" merge-base --is-ancestor 1fa0d99e4b714b5ce305f78c8f7c7773255e8f87 HEAD

tmp=$(mktemp -d)
server_pid=
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT HUP INT TERM

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
                "rows": [{"artifact_root_id": request["artifact_root_id"]}],
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

invoke() {
  expected_exit=$1
  expected_code=$2
  input=$3
  set +e
  printf '%s' "$input" | env CAIRN_ROOT="$cairn" CAIRN_READ_ENDPOINT="$endpoint" \
    CAIRN_READ_TOKEN=w3b-read-token-secret "$cli" >"$tmp/out" 2>"$tmp/err"
  status=$?
  set -e
  test "$status" -eq "$expected_exit" || { cat "$tmp/out" >&2; cat "$tmp/err" >&2; return 1; }
  assert_json_object "$tmp/out" "$expected_code"
  ! rg -q 'w3b-read-token-secret|secret-response-body|CAIRN_READ_ENDPOINT|database_url' "$tmp/out" "$tmp/err"
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
}, value
PY

while IFS="	" read -r code status; do
  invoke "$status" "$code" "{\"artifact_root_id\":\"root-a\",\"resolve_active\":true,\"query\":\"error:$code\"}"
done <"$tmp/codes.tsv"

invoke 4 INVALID_REQUEST '{not-json'
invoke 4 INVALID_REQUEST '{"artifact_root_id":"root-a","resolve_active":true,"nested":{"apply":true}}'

set +e
env -u CAIRN_ROOT "$cli" >"$tmp/out" 2>"$tmp/err" </dev/null
status=$?
set -e
test "$status" -eq 18
assert_json_object "$tmp/out" INTERNAL_FAILURE

set +e
env CAIRN_ROOT="$cairn" "$cli" --apply >"$tmp/out" 2>"$tmp/err" </dev/null
status=$?
set -e
test "$status" -eq 4
assert_json_object "$tmp/out" INVALID_REQUEST

echo 'cairn artifact read contract: ok'
