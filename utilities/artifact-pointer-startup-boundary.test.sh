#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
W5_STARTUP_TMP=$(mktemp -d)
http_pid=
cleanup() {
  rc=$?
  if [ -n "$http_pid" ] && kill -0 "$http_pid" 2>/dev/null; then
    kill "$http_pid" 2>/dev/null || rc=1
    wait "$http_pid" 2>/dev/null || :
  fi
  rm -rf -- "$W5_STARTUP_TMP" || rc=1
  test ! -e "$W5_STARTUP_TMP" || rc=1
  trap - EXIT HUP INT TERM
  exit "$rc"
}
trap cleanup EXIT HUP INT TERM

export XDG_DATA_HOME="$W5_STARTUP_TMP/data"
export XDG_STATE_HOME="$W5_STARTUP_TMP/state"
export MEM_STORE="$W5_STARTUP_TMP/data/hearting/memory"
export W5_STARTUP_TMP
case "$MEM_STORE" in "$W5_STARTUP_TMP"/*) ;; *) exit 2;; esac
mkdir -p "$XDG_DATA_HOME" "$XDG_STATE_HOME"

# Both startup readers need a real project-scoped row to produce a nonempty
# command-owned JSON sentinel. The store is synthetic and contained above.
(cd "$ROOT" && python3 "$ROOT/tools/memory/mem.py" add working note \
  "startup sentinel project record" --scope project --source startup-seed \
  --headline "startup sentinel" --entity startup >/dev/null)

path_count="$W5_STARTUP_TMP/path-transport.count"
printf '0\n' > "$path_count"
fake="$W5_STARTUP_TMP/cairn-artifact-read.sh"
cat > "$fake" <<'EOF'
#!/bin/sh
n=$(cat "$W5_STARTUP_TMP/path-transport.count")
printf '%s\n' "$((n+1))" > "$W5_STARTUP_TMP/path-transport.count"
exit 99
EOF
chmod 700 "$fake"
export PATH="$W5_STARTUP_TMP:$PATH"

# A reachable isolated HTTP fixture is the second active transport oracle.
# Its process is joined and reaped before this test reports success.
http_count="$W5_STARTUP_TMP/http.count"
: > "$http_count"
cat > "$W5_STARTUP_TMP/http-server.py" <<'PY'
import http.server, pathlib, sys
port_file, count_file = map(pathlib.Path, sys.argv[1:])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        with count_file.open("a", encoding="utf-8") as stream:
            stream.write("1\n")
        self.send_response(500); self.end_headers()
    def log_message(self, *_args):
        pass
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port_file.write_text(str(server.server_address[1]), encoding="utf-8")
server.serve_forever()
PY
python3 "$W5_STARTUP_TMP/http-server.py" "$W5_STARTUP_TMP/http.port" "$http_count" &
http_pid=$!
i=0
while [ ! -s "$W5_STARTUP_TMP/http.port" ]; do
  kill -0 "$http_pid" 2>/dev/null
  i=$((i+1)); [ "$i" -lt 500 ] || exit 1
  sleep 0.01
done
export CAIRN_READ_ENDPOINT="http://127.0.0.1:$(cat "$W5_STARTUP_TMP/http.port")/read"
export CAIRN_READ_TOKEN="w5-startup-fixture-token"

# The distiller uses a deterministic, no-network worker and a synchronous
# governor shim. The dispatcher still exercises its real detached-child path;
# strace follows it and the worker-owned call log proves completion.
cat > "$W5_STARTUP_TMP/governor.py" <<'PY'
import subprocess, sys
cut = sys.argv.index("--")
raise SystemExit(subprocess.run(sys.argv[cut + 1:]).returncode)
PY
cat > "$W5_STARTUP_TMP/distill-worker" <<'SH'
#!/bin/sh
printf '%s\n' "$1" >> "$W5_STARTUP_TMP/worker.calls"
exit 0
SH
chmod 700 "$W5_STARTUP_TMP/distill-worker"
export MEM_DISTILL_ENABLE=1 MEM_PERIODIC_CURATE_ENABLE=1
export MEM_DISTILL_WORKER="$W5_STARTUP_TMP/distill-worker"
export MODEL_WORKER_GOVERNOR="$W5_STARTUP_TMP/governor.py"
export MEM_PY="$ROOT/tools/memory/mem.py" AGENT_HOME="$ROOT"
export MEM_SESSION_SOURCE=codex CODEX_SESSIONS="$W5_STARTUP_TMP/codex-sessions"
export MEM_PROJECTS="$W5_STARTUP_TMP/projects"
export MEM_PERIODIC_CURATE_MAX_PROJECTS=1
export MEM_PERIODIC_CURATE_PROJECT_TIMEOUT=10 MEM_PERIODIC_CURATE_TIMEOUT=10
mkdir -p "$CODEX_SESSIONS" "$MEM_PROJECTS"
cat > "$CODEX_SESSIONS/w5-startup.jsonl" <<'JSONL'
{"timestamp":"2026-08-24T00:00:00Z","type":"event_msg","payload":{"type":"user_message","id":"w5-startup-user","message":"startup distill sentinel"}}
JSONL
encoded=$(printf '%s' "$ROOT" | sed 's#/#-#g')
mkdir -p "$MEM_PROJECTS/$encoded"

run_real() {
  mode=$1; shift
  out="$W5_STARTUP_TMP/$mode.out"
  err="$W5_STARTUP_TMP/$mode.err"
  trace="$W5_STARTUP_TMP/$mode.trace"
  /usr/bin/strace -ff -e trace=execve,connect -o "$trace" \
    env -u AGENT_SESSION_ROLE -u AGENT_DISPATCH_CHILD -u AGENT_DISPATCH_DEPTH \
        -u OPENCODE_DISPATCH_SLUG -u FLEET_TITLE_REFRESH -u MEM_DISTILL \
        "$@" >"$out" 2>"$err"
  set -- "$trace"*
  test -e "$1"
  if rg -q 'utilities/cairn-artifact-read\.sh|connect\(' "$trace"*; then
    echo "startup transport observed for $mode" >&2
    exit 1
  fi
}

run_real inject python3 "$ROOT/tools/memory/mem.py" inject --hook
python3 - "$W5_STARTUP_TMP/inject.out" <<'PY'
import json, pathlib, sys
row=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert row["hookSpecificOutput"]["hookEventName"] == "SessionStart"
assert "startup sentinel project record" in row["hookSpecificOutput"]["additionalContext"]
PY

run_real candidates python3 "$ROOT/tools/memory/mem.py" candidates startup --hook
python3 - "$ROOT/tools/memory/mem.py" "$W5_STARTUP_TMP/candidates.out" <<'PY'
import importlib.util, json, pathlib, sys
spec=importlib.util.spec_from_file_location("mem",sys.argv[1])
mem=importlib.util.module_from_spec(spec); spec.loader.exec_module(mem)
data=pathlib.Path(sys.argv[2]).read_bytes(); row=json.loads(data)
context=row["hookSpecificOutput"]["additionalContext"]
candidates=[line for line in context.splitlines() if line.startswith("- [")]
assert 0 < len(candidates) <= mem.CANDIDATE_MAX_RESULTS
assert len(data) <= mem.CANDIDATE_MAX_UTF8_BYTES
PY

run_real distill bash "$ROOT/hooks/mem-distill-dispatch.sh" distill w5-startup "$ROOT"
test "$(grep -c '^increment$' "$W5_STARTUP_TMP/worker.calls")" = 1
test ! -d "$MEM_STORE/.distill-lock-w5-startup"

run_real periodic-curate bash "$ROOT/utilities/mem-periodic-curate.sh"
rg -q '^mem-periodic-curate project=.* status=complete$' "$W5_STARTUP_TMP/periodic-curate.err"
test "$(grep -c '^curate$' "$W5_STARTUP_TMP/worker.calls")" = 1

# Command-owned sentinels are all established before the zero-call assertions.
test "$(cat "$path_count")" = 0
test ! -s "$http_count"
telemetry="$XDG_STATE_HOME/hearting/artifact-projection/read-telemetry.jsonl"
test ! -e "$telemetry" || test ! -s "$telemetry"
hook_hits="$W5_STARTUP_TMP/hook-hits"
if (cd "$ROOT" && rg -l 'cairn-artifact-read|artifact-projection-read' adapters/*/hooks/ hooks) > "$hook_hits"; then
  echo "startup hook unexpectedly references artifact projection read" >&2
  exit 1
fi
test ! -s "$hook_hits"

# Explicitly join the HTTP fixture and prove cleanup before printing PASS.
kill -0 "$http_pid" 2>/dev/null
kill "$http_pid"
wait "$http_pid" 2>/dev/null || :
http_pid=
rm -rf -- "$W5_STARTUP_TMP"
test ! -e "$W5_STARTUP_TMP"
trap - EXIT HUP INT TERM
echo "startup boundary: PASS (four real paths, three transport oracles, isolated telemetry, explicit cleanup)"
