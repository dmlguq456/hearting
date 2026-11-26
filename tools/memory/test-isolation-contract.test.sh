#!/usr/bin/env sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../run-tests.py" --direct-run bash "$0" "$@"
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
. "$ROOT/tools/memory/test-isolation.sh"
ROOT_FIX=$(mktemp -d "${TMPDIR:-/tmp}/hearting-isolation-contract.XXXXXX")
trap 'rm -rf "$ROOT_FIX"' EXIT HUP INT TERM

sentinel=$(mktemp -d "${TMPDIR:-/tmp}/hearting-ambient.XXXXXX")
printf '%s\n' sentinel >"$sentinel/memory.db"
before=$(cksum "$sentinel/memory.db")
MEM_STORE="$sentinel"
export MEM_STORE
# An ambient config dir whose policy turns a remote on, plus ambient env
# overrides: none of them may survive isolation.
ambient_cfg="$sentinel/config"
mkdir -p "$ambient_cfg/hearting"
printf '%s\n' '{"enabled": true, "remote_url": "git@example.invalid:x/y.git"}' \
  >"$ambient_cfg/hearting/memory-sync.json"
cfg_before=$(cksum "$ambient_cfg/hearting/memory-sync.json")
XDG_CONFIG_HOME="$ambient_cfg"
MEM_SYNC_REMOTE=1
MEM_SYNC_REMOTE_URL="git@example.invalid:x/y.git"
MEM_DUMP_PUSH=1
MEM_EXCHANGE_AUTO=on
export XDG_CONFIG_HOME MEM_SYNC_REMOTE MEM_SYNC_REMOTE_URL MEM_DUMP_PUSH MEM_EXCHANGE_AUTO
hearting_test_isolate "$ROOT_FIX/fixture"
[ "$MEM_STORE" = "$ROOT_FIX/fixture/store" ]
[ "$HOME" = "$ROOT_FIX/fixture/home" ]
[ "$XDG_DATA_HOME" = "$ROOT_FIX/fixture/data" ]
[ "$XDG_STATE_HOME" = "$ROOT_FIX/fixture/state" ]
[ "$XDG_CONFIG_HOME" = "$ROOT_FIX/fixture/config" ]
[ "$TMPDIR" = "$ROOT_FIX/fixture/tmp" ]
[ "$(cksum "$ambient_cfg/hearting/memory-sync.json")" = "$cfg_before" ]
[ -z "${MEM_SYNC_REMOTE:-}${MEM_SYNC_REMOTE_URL:-}${MEM_DUMP_PUSH:-}" ]
# No detached exchange worker unless a test turns the scheduler on itself.
[ "$MEM_EXCHANGE_AUTO" = "0" ]
grep -q '"enabled": false' "$XDG_CONFIG_HOME/hearting/memory-sync.json"
# The isolated policy resolves to remote off through the real reader.
resolved=$(python3 - "$ROOT/tools/memory" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import mem
env = mem._sync_environment()
print(env.get("MEM_SYNC_REMOTE", ""), env.get("MEM_SYNC_REMOTE_URL", "-"))
PY
)
[ "$resolved" = "0 -" ]
[ "$(cksum "$sentinel/memory.db")" = "$before" ]

derived=$(hearting_test_derived_env python3 -c 'import os; print(os.environ.get("MEM_STORE", ""))')
[ -z "$derived" ]
case "$HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_DATA_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_STATE_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_CONFIG_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$TMPDIR" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac

count=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' -exec sh -c \
  'grep -q "test-isolation\.sh" "$1"' sh {} \; -print | wc -l | tr -d ' ')
total=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' | wc -l | tr -d ' ')
[ "$count" -eq "$total" ]

# Without a usable TMPDIR the suite stops instead of running on the caller's roots.
stopped_rc=0
stopped_out=$( (TMPDIR="$ROOT_FIX/missing/tmp"; hearting_test_isolate; echo continued) 2>/dev/null) || stopped_rc=$?
[ "$stopped_rc" -eq 70 ]
[ -z "$stopped_out" ]

printf '%s\n' 'test isolation contract: PASS'
