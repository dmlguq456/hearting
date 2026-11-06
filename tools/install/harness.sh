#!/usr/bin/env sh
# harness.sh — thin POSIX sh launcher. Resolve the AGENT_HOME symlink and delegate
# to the installer.py command tree. Pass all options through unchanged.
# Installation exposes this script as the `harness` command on PATH.
set -eu

# Resolve the real script location even when invoked through a symlink.
SOURCE=$0
while [ -h "$SOURCE" ]; do
  DIR=$(CDPATH= cd -P "$(dirname "$SOURCE")" && pwd)
  SOURCE=$(readlink "$SOURCE")
  case $SOURCE in
    /*) ;;
    *) SOURCE="$DIR/$SOURCE" ;;
  esac
done
SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$SOURCE")" && pwd)
INSTALLER_PY="$SCRIPT_DIR/installer.py"
HARNESS_ROOT=$(CDPATH= cd -P "$SCRIPT_DIR/../.." && pwd)

# A managed release has no .git directory. Anchor AGENT_HOME to the launcher
# source unless the caller deliberately selected another harness root.
if [ -z "${AGENT_HOME:-}" ]; then
  AGENT_HOME=$HARNESS_ROOT
  export AGENT_HOME
fi

# A managed release must stay byte-identical to what it was built from, but
# Python writes `__pycache__` beside every module it imports, so running this CLI
# with AGENT_HOME at a release drops bytecode into it. Keep the caching, put the
# artifacts in state. An explicit caller preference always wins.
if [ -z "${PYTHONPYCACHEPREFIX:-}" ] && [ -z "${PYTHONDONTWRITEBYTECODE:-}" ]; then
  _state_root=${HARNESS_STATE_ROOT:-${XDG_STATE_HOME:+$XDG_STATE_HOME/hearting}}
  if [ -z "$_state_root" ] && [ -n "${HOME:-}" ]; then
    _state_root=$HOME/.local/state/hearting
  fi
  if [ -n "$_state_root" ]; then
    PYTHONPYCACHEPREFIX=$_state_root/pycache
    export PYTHONPYCACHEPREFIX
  fi
  unset _state_root
fi

PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then echo "harness: python3 is required." >&2; exit 1; fi

# `hearting run <utility> [args...]` runs one utility from the active root:
# AGENT_HOME when the caller set one (a checkout, a worker's sealed release),
# else the release installed right now. It needs no environment, so the same
# documented or printed command works in a new session of every harness.
if [ "${1:-}" = run ]; then
  shift
  if [ $# -eq 0 ]; then echo "usage: hearting run <utility> [args...]" >&2; exit 2; fi
  name=$1
  shift
  case $name in
    ""|*/*|.*) echo "harness: not a utility name: $name" >&2; exit 2 ;;
  esac
  for entry in "$AGENT_HOME/utilities/$name" "$AGENT_HOME/utilities/$name.py" "$AGENT_HOME/utilities/$name.sh"; do
    [ -f "$entry" ] || continue
    case $entry in
      *.py) exec "$PY" "$entry" "$@" ;;
    esac
    if [ -x "$entry" ]; then exec "$entry" "$@"; fi
    exec sh "$entry" "$@"
  done
  echo "harness: no utility named $name under $AGENT_HOME/utilities" >&2
  exit 2
fi

# `hearting reports` / `hearting open`: the artifact read entrance, the same
# launcher in every runtime (`bootstrap.py` links `hearting` and `harness` to
# this one script). No adapter-specific copy exists.
case "${1:-}" in
  reports|open)
    if [ ! -f "$AGENT_HOME/utilities/artifact_entrance.py" ]; then
      echo "harness: the artifact entrance is missing ($AGENT_HOME/utilities/artifact_entrance.py)" >&2
      exit 2
    fi
    exec "$PY" "$AGENT_HOME/utilities/artifact_entrance.py" "$@"
    ;;
esac
if [ ! -f "$INSTALLER_PY" ]; then echo "harness: installer.py was not found ($INSTALLER_PY)." >&2; exit 1; fi

exec "$PY" "$INSTALLER_PY" "$@"
