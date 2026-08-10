#!/usr/bin/env sh
# Resolve and run a Codex hook bridge from a validated harness root.
set -eu

script="${1:-}"
case "$script" in
  sessionstart-lifecycle.py|sessionend-lifecycle.py|stop-lifecycle.py|userprompt-lifecycle.py|permissionrequest-lifecycle.py|posttooluse-interaction-clear.py|pretooluse-write-guard.py|posttooluse-design-check.py|posttooluse-read-marker.py|worker-state-compact.py)
    ;;
  *)
    printf '%s\n' "unsupported Codex hook bridge: ${script:-<empty>}" >&2
    exit 69
    ;;
esac

valid_root() {
  root="$1"
  [ -n "$root" ] \
    && [ -f "$root/core/CORE.md" ] \
    && [ -x "$root/adapters/codex/hooks/$script" ]
}

if [ -n "${AGENT_HOME:-}" ] && valid_root "$AGENT_HOME"; then
  agent_root="$AGENT_HOME"
else
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  local_root="$(CDPATH= cd -- "$script_dir/../../.." && pwd)"
  if valid_root "$local_root"; then
    agent_root="$local_root"
  elif valid_root "${HOME:-}/.codex/hearting"; then
    agent_root="$HOME/.codex/hearting"
  elif valid_root "${HOME:-}/hearting"; then
    agent_root="$HOME/hearting"
  elif valid_root "${HOME:-}/agent_setting"; then
    agent_root="$HOME/agent_setting"
  else
    printf '%s\n' "agent harness root not found for Codex hook bridge: $script" >&2
    exit 69
  fi
fi

shift
AGENT_HOME="$agent_root" exec python3 "$agent_root/adapters/codex/hooks/$script" "$@"
