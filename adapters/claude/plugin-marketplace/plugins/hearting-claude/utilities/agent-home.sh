#!/usr/bin/env sh
# Print the active agent harness root (managed release or linked checkout).
# Preferred override: AGENT_HOME
# Claude adapter compatibility: CLAUDE_HOME
# Managed-release default: $XDG_DATA_HOME/hearting/current
# Canonical linked-checkout fallback: $HOME/hearting
# Legacy linked-checkout fallback: $HOME/agent_setting
# Legacy fallback: $HOME/.claude
set -eu

if [ -n "${AGENT_HOME:-}" ] && [ -f "$AGENT_HOME/core/CORE.md" ]; then
  printf '%s\n' "$AGENT_HOME"
elif [ -n "${CLAUDE_HOME:-}" ] && [ -f "$CLAUDE_HOME/core/CORE.md" ]; then
  printf '%s\n' "$CLAUDE_HOME"
elif [ -f "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current/core/CORE.md" ]; then
  printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/current"
elif [ -f "$HOME/hearting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/hearting"
elif [ -f "$HOME/agent_setting/core/CORE.md" ]; then
  printf '%s\n' "$HOME/agent_setting"
else
  printf '%s\n' "$HOME/.claude"
fi
