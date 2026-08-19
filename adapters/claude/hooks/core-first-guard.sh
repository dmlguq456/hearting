#!/bin/sh
# PreToolUse(Edit/Write): require a current-session core/*.md read marker before
# editing adapters/**.
# S6 (2026-07-09): use a wrapper instead of a full copy. The paired read-marker
# wrapper writes under the repository `.core-grounding`; a copied guard resolved
# SCRIPT_DIR/.. to ~/.claude and always denied because it inspected a different
# marker directory. Executing the repository-owned portable guard keeps both
# sides on the same AGENT_HOME while preserving stdin-JSON and --file modes.
#
# That delegation holds only while `$AGENT_HOME/hooks/` carries the *portable*
# guard, which is true in a repository checkout. In an installed runtime layout
# `$AGENT_HOME/hooks/` is this adapter's own projection, so
# `$AGENT_HOME/hooks/core-first-guard.sh` is a symlink back to this very file and
# the exec below re-entered this wrapper without bound: the gate never returned a
# decision (so the core-first invariant went silently unenforced), every
# Edit/Write paid the registered hook timeout, and the spinning process outlived
# the dispatch process group that launched it. So resolve the delegation target
# first: when it is this same file, fail over to the portable guard that ships
# physically beside this wrapper (`adapters/<harness>/hooks/../../..` -> `hooks/`),
# and refuse loudly if even that is unreachable, rather than loop.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../utilities/agent-home.sh")}"
# The portable guard reads `$AGENT_HOME/.core-grounding`. Export the value this
# wrapper resolved so the delegation target inspects the same marker directory
# under either delegation path and in both call modes.
export AGENT_HOME

# `readlink -f` is not POSIX. Where it is missing the raw paths are compared
# instead, which can only miss a failover, never invent one.
canonical() {
  readlink -f -- "$1" 2>/dev/null || printf '%s\n' "$1"
}

SELF=$(canonical "$0")
if [ "$(canonical "$AGENT_HOME/hooks/core-first-guard.sh")" = "$SELF" ]; then
  SELF_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF")" && pwd -P)
  PORTABLE_GUARD="$SELF_DIR/../../../hooks/core-first-guard.sh"
  if [ ! -f "$PORTABLE_GUARD" ] || [ "$(canonical "$PORTABLE_GUARD")" = "$SELF" ]; then
    echo "core-first-guard: portable guard unreachable; core-first gate not evaluated (self=$SELF agent_home=$AGENT_HOME)" >&2
    exit 69
  fi
  exec "$PORTABLE_GUARD" "$@"
fi
exec "$AGENT_HOME/hooks/core-first-guard.sh" "$@"
