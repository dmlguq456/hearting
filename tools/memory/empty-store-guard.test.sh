#!/usr/bin/env sh
# Regression: empty-store creation guard (2026-07-22 memory audit P2).
# A DERIVED store path (AGENT_HOME/default) with no memory.db must FAIL LOUD and
# create nothing; explicit MEM_STORE or MEM_INIT=1 may create. Uncovered before:
# a worktree-style AGENT_HOME export silently fabricated an empty DB and reported
# "aspect not found" as if the knowledge did not exist.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
MEM="$ROOT/tools/memory/mem.py"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

fail() { echo "not ok - $*" >&2; exit 1; }

# 1. Derived path (AGENT_HOME → empty checkout): refuse, mention the resolved path,
#    create no store.
mkdir -p "$TMP/fake-home"
if AGENT_HOME="$TMP/fake-home" XDG_DATA_HOME="$TMP/fake-data" \
    python3 "$MEM" profile 01_paper_figure_style \
    >"$TMP/out1" 2>"$TMP/err1"; then
  fail "derived empty store was accepted (guard missing)"
fi
grep -q "refusing to create" "$TMP/err1" || fail "refusal message missing"
grep -q "$TMP/fake-home" "$TMP/err1" || fail "resolved path missing from error"
test ! -e "$TMP/fake-data/hearting/memory/memory.db" \
  || fail "empty DB was still created"

# 2. Explicit MEM_STORE: creation allowed (isolated envs and tests depend on this).
if ! MEM_STORE="$TMP/store2" python3 "$MEM" add durable note \
    "empty-store guard regression fixture: explicit MEM_STORE must be allowed to create a brand-new isolated store for tests and isolated environments." \
    --scope global >"$TMP/out2" 2>"$TMP/err2"; then
  cat "$TMP/err2" >&2; fail "explicit MEM_STORE creation was refused"
fi
test -f "$TMP/store2/memory.db" || fail "MEM_STORE store not created"

# 3. MEM_INIT=1 escape hatch on a derived path: creation allowed.
mkdir -p "$TMP/fresh-home"
if ! AGENT_HOME="$TMP/fresh-home" XDG_DATA_HOME="$TMP/fresh-data" MEM_INIT=1 \
    python3 "$MEM" add durable note \
    "empty-store guard regression fixture: MEM_INIT=1 is the documented escape hatch for a genuine first install on a derived path." \
    --scope global >"$TMP/out3" 2>"$TMP/err3"; then
  cat "$TMP/err3" >&2; fail "MEM_INIT=1 first install was refused"
fi
test -f "$TMP/fresh-data/hearting/memory/memory.db" \
  || fail "MEM_INIT store not created"

# 4. The real primary store still opens read paths normally (no regression).
python3 "$MEM" profile 01_paper_figure_style >/dev/null 2>&1 \
  || fail "primary store profile read broke"

echo "empty-store-guard: PASS"
