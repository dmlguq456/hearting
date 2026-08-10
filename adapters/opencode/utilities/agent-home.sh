#!/usr/bin/env sh
# Print the agent harness repository directory for the OpenCode adapter.
# Preferred override: valid AGENT_HOME
# Canonical linked checkout: $HOME/hearting
# Optional OpenCode runtime pointer: $HOME/.config/opencode/hearting
# Legacy linked checkout: $HOME/agent_setting
set -eu

if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
  printf '%s\n' "$AGENT_HOME"
elif [ -f "$HOME/.config/opencode/hearting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/.config/opencode/hearting"
elif [ -f "$HOME/hearting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/hearting"
elif [ -f "$HOME/agent_setting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/agent_setting"
else
  printf '%s\n' "$HOME/hearting"
fi
