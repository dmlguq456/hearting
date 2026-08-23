#!/usr/bin/env sh
# Print the memory store directory, resolved exactly like tools/memory/mem.py.
#
# The store is runtime state, not harness source, so it must never resolve into
# a managed release tree. agent-home.sh answers a different question — where the
# harness code lives — and on a managed install it answers with the immutable
# release root, which is why store consumers resolve through this script instead
# of appending `/memory` to an agent-home result.
#
# Precedence mirrors mem.py: MEM_STORE, then AGENT_HOME, CLAUDE_HOME, the
# canonical/legacy linked checkouts, and finally the Claude runtime home; the
# chosen home keeps its `memory/` directory when that already exists, otherwise
# the store lives under XDG data home.
set -eu

if [ -n "${MEM_STORE:-}" ]; then
  printf '%s\n' "$MEM_STORE"
  exit 0
fi

if [ -n "${AGENT_HOME:-}" ]; then
  store_home=$AGENT_HOME
elif [ -n "${CLAUDE_HOME:-}" ]; then
  store_home=$CLAUDE_HOME
elif [ -e "$HOME/hearting" ]; then
  store_home=$HOME/hearting
elif [ -e "$HOME/agent_setting" ]; then
  store_home=$HOME/agent_setting
else
  store_home=$HOME/.claude
fi

store=$store_home/memory
if [ -e "$store" ] || [ -L "$store" ]; then
  printf '%s\n' "$store"
else
  printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/hearting/memory"
fi
