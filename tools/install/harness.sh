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
if [ ! -f "$INSTALLER_PY" ]; then echo "harness: installer.py was not found ($INSTALLER_PY)." >&2; exit 1; fi

exec "$PY" "$INSTALLER_PY" "$@"
