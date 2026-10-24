#!/usr/bin/env bash
# recall — unified-memory retrieval (thin wrapper around mem recall).
#   Searches the unified store (durable + working), profile-tier records, and
#   raw conversations with --sessions rather than scanning legacy files.
#   Usage: recall.sh "<query>" [--tier working|durable] [--scope project|global]
#         [--all] [--sessions] [--full] [--limit 1..100] [--no-touch]
#   Retrieval does not consume handoffs. Explicit recall updates only
#   last_accessed; --no-touch leaves access metadata unchanged.
#   Details: tools/memory/README.md and MEMORY §7.4.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/../../utilities/agent-home.sh")}"
export AGENT_HOME
exec python3 "$AGENT_HOME/tools/memory/mem.py" recall "$@"
