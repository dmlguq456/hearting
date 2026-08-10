#!/usr/bin/env sh
# Print the active agent harness root (managed release or linked checkout).
# Preferred override: AGENT_HOME
# Claude adapter compatibility: CLAUDE_HOME
# Managed-release default: $XDG_DATA_HOME/hearting/current
# Canonical linked-checkout fallback: $HOME/hearting
# Legacy linked-checkout fallback: $HOME/agent_setting
# Legacy fallback: $HOME/.claude
set -eu

if [ "${AGENT_HOME:-}" ]; then
  printf '%s\n' "$AGENT_HOME"
elif [ "${CLAUDE_HOME:-}" ]; then
  printf '%s\n' "$CLAUDE_HOME"
elif [ -d "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current" ]; then
  printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current"
elif [ -d "$HOME/hearting" ]; then
  printf '%s\n' "$HOME/hearting"
elif [ -d "$HOME/agent_setting" ]; then
  printf '%s\n' "$HOME/agent_setting"
else
  printf '%s\n' "$HOME/.claude"
fi
