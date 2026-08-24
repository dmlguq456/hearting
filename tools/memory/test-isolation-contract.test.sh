#!/usr/bin/env sh
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
hearting_test_isolate "$ROOT_FIX/fixture"
[ "$MEM_STORE" = "$ROOT_FIX/fixture/store" ]
[ "$HOME" = "$ROOT_FIX/fixture/home" ]
[ "$XDG_DATA_HOME" = "$ROOT_FIX/fixture/data" ]
[ "$XDG_STATE_HOME" = "$ROOT_FIX/fixture/state" ]
[ "$(cksum "$sentinel/memory.db")" = "$before" ]

derived=$(hearting_test_derived_env python3 -c 'import os; print(os.environ.get("MEM_STORE", ""))')
[ -z "$derived" ]
case "$HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_DATA_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac
case "$XDG_STATE_HOME" in "$HEARTING_TEST_ROOT"/*) ;; *) exit 1 ;; esac

count=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' -exec sh -c \
  'rg -q "test-isolation\.sh" "$1"' sh {} \; -print | wc -l | tr -d ' ')
total=$(find "$ROOT/tools/memory" -maxdepth 1 -name '*.test.sh' -type f \
  ! -name 'test-isolation-contract.test.sh' | wc -l | tr -d ' ')
[ "$count" -eq "$total" ]

printf '%s\n' 'test isolation contract: PASS'
