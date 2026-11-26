#!/bin/sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../tools/run-tests.py" --direct-run bash "$0" "$@"
set -eu
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
W5_STARTUP_TMP=$(mktemp -d)
cleanup() {
  rc=$?
  rm -rf -- "$W5_STARTUP_TMP" || rc=1
  test ! -e "$W5_STARTUP_TMP" || rc=1
  trap - EXIT HUP INT TERM
  exit "$rc"
}
trap cleanup EXIT HUP INT TERM

export XDG_DATA_HOME="$W5_STARTUP_TMP/data"
export XDG_STATE_HOME="$W5_STARTUP_TMP/state"
export MEM_STORE="$W5_STARTUP_TMP/data/hearting/memory"
case "$MEM_STORE" in "$W5_STARTUP_TMP"/*) ;; *) exit 2;; esac
mkdir -p "$XDG_DATA_HOME" "$XDG_STATE_HOME"

# Both startup readers need a real project-scoped row to produce a nonempty
# command-owned JSON sentinel. The store is synthetic and contained above.
(cd "$ROOT" && python3 "$ROOT/tools/memory/mem.py" add working note \
  "startup sentinel project record" --scope project --source startup-seed \
  --headline "startup sentinel" --entity startup >/dev/null)

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
  # AF_UNIX lookups such as glibc's optional nscd probe are local runtime
  # plumbing, not an artifact transport. Keep the boundary exact: any IP
  # socket attempt is forbidden here.
  if rg -q 'connect\(.*sa_family=AF_INET' "$trace"*; then
    rg -n 'connect\(.*sa_family=AF_INET' "$trace"* >&2 || :
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

rm -rf -- "$W5_STARTUP_TMP"
test ! -e "$W5_STARTUP_TMP"
trap - EXIT HUP INT TERM
echo "startup boundary: PASS (two real paths, no IP socket, isolated store, explicit cleanup)"
