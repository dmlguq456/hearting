#!/usr/bin/env sh
# Print the agent harness repository directory for the Codex adapter.
# Preferred override: valid AGENT_HOME
# Canonical linked checkout: $HOME/hearting
# Optional Codex runtime pointer: $HOME/.codex/hearting
# Legacy linked checkout: $HOME/agent_setting
set -eu

if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
  printf '%s\n' "$AGENT_HOME"
elif [ -f "$HOME/.codex/hearting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/.codex/hearting"
elif [ -f "$HOME/hearting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/hearting"
elif [ -f "$HOME/agent_setting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/agent_setting"
else
  printf '%s\n' "$HOME/hearting"
fi
