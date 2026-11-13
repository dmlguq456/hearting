#!/usr/bin/env bash
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../tools/run-tests.py" --direct-run bash "$0" "$@"
# Regression: design-postwrite.sh in hook mode must stay silent for non-design edits and
# for an unusable node, and surface only the checker's exit 2.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
HOOK="$ROOT/hooks/design-postwrite.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
ok() { PASS=$((PASS+1)); printf '  ok  %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  BAD %s\n' "$1"; }

# A PATH holding only what the hook itself needs, with no node.
BASE="$TMP/base"
mkdir -p "$BASE"
for tool in bash dirname cat; do ln -s "$(command -v "$tool")" "$BASE/$tool"; done

# A stub node that exits with a chosen status and prints a marker on stderr.
stub_node() { # <dir> <exit-code>
  mkdir -p "$1"
  printf '#!/bin/sh\ncat >/dev/null\necho "stub-node ran" >&2\nexit %s\n' "$2" > "$1/node"
  chmod +x "$1/node"
}
stub_node "$TMP/node2" 2
stub_node "$TMP/node1" 1
stub_node "$TMP/node0" 0

DESIGN='{"tool_name":"Write","tool_input":{"file_path":"/p/designs/home/preview.html","content":"x"}}'
PLAIN='{"tool_name":"Write","tool_input":{"file_path":"/p/src/notes.md","content":"x"}}'
UPPER='{"tool_name":"Write","tool_input":{"file_path":"/p/designs/Home.HTML","content":"x"}}'

# run <extra-path-dir|-> <payload> -> sets rc, out, err
run() {
  local path="$BASE"
  [ "$1" = "-" ] || path="$1:$BASE"
  printf '%s' "$2" | env -i PATH="$path" HOME="$TMP/home" AGENT_HOME="$ROOT" \
    bash "$HOOK" >"$TMP/out" 2>"$TMP/err"
  rc=$?
}

run - "$PLAIN"
if [ "$rc" = 0 ] && [ ! -s "$TMP/out" ] && [ ! -s "$TMP/err" ]; then
  ok "non-design payload without node exits 0 silently"
else
  bad "non-design payload without node: rc=$rc err=$(cat "$TMP/err")"
fi

run "$TMP/node2" "$PLAIN"
if [ "$rc" = 0 ] && [ ! -s "$TMP/err" ]; then
  ok "non-design payload never starts node"
else
  bad "non-design payload started node: rc=$rc err=$(cat "$TMP/err")"
fi

run - "$DESIGN"
if [ "$rc" = 0 ] && [ ! -s "$TMP/out" ] && [ ! -s "$TMP/err" ]; then
  ok "design payload with node missing exits 0 silently"
else
  bad "design payload with node missing: rc=$rc err=$(cat "$TMP/err")"
fi

run "$TMP/node2" "$DESIGN"
if [ "$rc" = 2 ] && grep -q 'stub-node ran' "$TMP/err"; then
  ok "node exit 2 surfaces its message with exit 2"
else
  bad "node exit 2 should surface: rc=$rc err=$(cat "$TMP/err")"
fi

run "$TMP/node2" "$UPPER"
if [ "$rc" = 2 ]; then
  ok "uppercase .HTML payload still reaches node"
else
  bad "uppercase .HTML payload skipped: rc=$rc"
fi

run "$TMP/node1" "$DESIGN"
if [ "$rc" = 0 ] && [ ! -s "$TMP/err" ]; then
  ok "node exit 1 (cannot run checker) exits 0 silently"
else
  bad "node exit 1: rc=$rc err=$(cat "$TMP/err")"
fi

run "$TMP/node0" "$DESIGN"
if [ "$rc" = 0 ] && [ ! -s "$TMP/err" ]; then
  ok "node exit 0 passes through"
else
  bad "node exit 0: rc=$rc err=$(cat "$TMP/err")"
fi

# --file stays loud: missing node is a hard failure, node's status is returned as is.
env -i PATH="$BASE" HOME="$TMP/home" AGENT_HOME="$ROOT" bash "$HOOK" --file "$TMP/x.html" </dev/null >/dev/null 2>&1
if [ "$?" -ne 0 ]; then
  ok "--file without node still fails"
else
  bad "--file without node should fail"
fi
env -i PATH="$TMP/node1:$BASE" HOME="$TMP/home" AGENT_HOME="$ROOT" bash "$HOOK" --file "$TMP/x.html" </dev/null >/dev/null 2>&1
if [ "$?" = 1 ]; then
  ok "--file returns node's exit status unchanged"
else
  bad "--file should return node's exit status"
fi

printf 'design-postwrite: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
