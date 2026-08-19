#!/bin/sh
# PostToolUse(Read): write the current-session core/*.md read marker consumed by
# the paired core-first gate.
#
# Same wrapper contract, and the same self-exec hazard, as
# `core-first-guard.sh`: `$AGENT_HOME/hooks/` carries the portable marker writer
# in a repository checkout but this adapter's own projection in an installed
# runtime layout, where `$AGENT_HOME/hooks/core-read-marker.sh` is a symlink back
# to this file and the exec below re-entered this wrapper without bound. Resolve
# the delegation target first, fail over to the portable writer that ships
# physically beside this wrapper, and refuse loudly rather than loop.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"
# The portable writer creates `$AGENT_HOME/.core-grounding`. Export the value
# this wrapper resolved so the marker lands where the paired guard reads it under
# either delegation path and in both call modes.
export AGENT_HOME

# `readlink -f` is not POSIX. Where it is missing the raw paths are compared
# instead, which can only miss a failover, never invent one.
canonical() {
  readlink -f -- "$1" 2>/dev/null || printf '%s\n' "$1"
}

SELF=$(canonical "$0")
if [ "$(canonical "$AGENT_HOME/hooks/core-read-marker.sh")" = "$SELF" ]; then
  SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd -P)
  PORTABLE_MARKER="$SELF_DIR/../../../hooks/core-read-marker.sh"
  if [ ! -f "$PORTABLE_MARKER" ] || [ "$(canonical "$PORTABLE_MARKER")" = "$SELF" ]; then
    echo "core-read-marker: portable marker writer unreachable; no core read marker written (self=$SELF agent_home=$AGENT_HOME)" >&2
    exit 69
  fi
  exec "$PORTABLE_MARKER" "$@"
fi
exec "$AGENT_HOME/hooks/core-read-marker.sh" "$@"
