#!/usr/bin/env sh
# Resolve and run a Codex hook bridge from a validated harness root.
set -eu

script="${1:-}"
case "$script" in
  sessionstart-lifecycle.py|sessionend-lifecycle.py|stop-lifecycle.py|userprompt-lifecycle.py|permissionrequest-lifecycle.py|posttooluse-interaction-clear.py|posttooluse-design-check.py|posttooluse-read-marker.py|posttooluse-frame-answer.py|worker-state-compact.py|core-write-guard.py|route-presence-gate.py|start-receipt-context.py)
    ;;
  *)
    printf '%s\n' "unsupported Codex hook bridge: ${script:-<empty>}" >&2
    exit 69
    ;;
esac

valid_root() {
  root="$1"
  [ -n "$root" ] \
    && [ -f "$root/core/CORE.md" ]
}

available_bridge() {
  valid_root "$1" && [ -x "$1/adapters/codex/hooks/$script" ]
}

if [ -n "${AGENT_HOME:-}" ] && valid_root "$AGENT_HOME"; then
  agent_root="$AGENT_HOME"
else
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  local_root="$(CDPATH= cd -- "$script_dir/../../.." && pwd)"
  if available_bridge "$local_root"; then
    agent_root="$local_root"
  elif available_bridge "${HOME:-}/.codex/hearting"; then
    agent_root="$HOME/.codex/hearting"
  elif available_bridge "${HOME:-}/hearting"; then
    agent_root="$HOME/hearting"
  elif available_bridge "${HOME:-}/agent_setting"; then
    agent_root="$HOME/agent_setting"
  else
    printf '%s\n' "agent harness root not found for Codex hook bridge: $script" >&2
    exit 69
  fi
fi

shift
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
AGENT_HOME="$agent_root" exec python3 "$agent_root/adapters/codex/hooks/$script" "$@"
