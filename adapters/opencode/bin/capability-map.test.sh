#!/usr/bin/env sh
# A direct run re-enters under tools/run-tests.py isolation (no real HOME, state, herdr or tmux).
[ -n "${HEARTING_TEST_ISOLATED:-}" ] || exec python3 "$(dirname -- "$0")/../../../tools/run-tests.py" --direct-run bash "$0" "$@"
# A failed topology summary must leave nothing behind in the temp dir.
#
# capability-map.sh used to stage the summary in /tmp/opencode-capability-topology.$$
# and remove it only on the success branch, so every capability whose summary
# failed left one file behind for good (6,527 of them on one host).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

# A python3 that always fails makes the summary step fail deterministically.
printf '%s\n' '#!/bin/sh' 'exit 1' > "$TMP/python3"
chmod +x "$TMP/python3"

# `exec` keeps the pid, so the script's own `$$` is the one printed here.
pid=$(PATH="$TMP:$PATH" sh -c 'echo $$; exec sh "$0" autopilot-code >"$1" 2>&1' \
  "$SCRIPT_DIR/capability-map.sh" "$TMP/out")

grep -q '^note=' "$TMP/out" || {
  echo "not ok - capability map stopped before its note line" >&2
  exit 1
}
for leftover in "/tmp/opencode-capability-topology.$pid" \
  "${TMPDIR:-/tmp}/opencode-capability-topology.$pid"; do
  if [ -e "$leftover" ]; then
    rm -f "$leftover"
    echo "not ok - a failed topology summary left $leftover behind" >&2
    exit 1
  fi
done
echo "ok - a failed topology summary leaves no temp file"

# The summary itself still reaches stdout when it succeeds.
printf '%s\n' '#!/bin/sh' 'echo topology_summary=present' > "$TMP/python3"
PATH="$TMP:$PATH" sh "$SCRIPT_DIR/capability-map.sh" autopilot-code > "$TMP/out" 2>&1
grep -q '^topology_summary=present$' "$TMP/out" || {
  echo "not ok - a successful topology summary was not printed" >&2
  exit 1
}
echo "ok - a successful topology summary is printed"
