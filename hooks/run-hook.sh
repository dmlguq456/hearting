#!/bin/sh
# Run one harness hook from the release its session's tree runs on.
#
# A registered dispatch tree exports AGENT_HOME as the release its launch resolved
# (core/OPERATIONS.md §5.9a), so its hooks run that release as well; an interactive session
# has no AGENT_HOME and runs the hook beside this runner (the runtime projection, now).
#
# Usage: run-hook.sh <interpreter|exec> <hook> [arg...]   (exec: run the file itself)
interpreter=$1
hook=$2
shift 2
dir=$(dirname -- "$0")
if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
  dir=$AGENT_HOME/hooks
fi
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
if [ "$interpreter" = exec ]; then
  exec "$dir/$hook" "$@"
fi
exec "$interpreter" "$dir/$hook" "$@"
