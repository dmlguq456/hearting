#!/usr/bin/env sh
# Print the canonical dispatch state root: the parent directory of the
# resolved canonical dispatch registry. No new env var -- the only override
# surface remains AGENT_DISPATCH_JOBS, matching
# utilities/dispatch_contract.py's resolve_dispatch_state_root() chain
# (explicit jobs path arg -> inherited AGENT_DISPATCH_JOBS -> AGENT_HOME/.dispatch).
#
# Usage: dispatch-state-root.sh [jobs-path]
#   AGENT_HOME must already be set by the caller (agent-home.sh) when no
#   explicit jobs path is given and AGENT_DISPATCH_JOBS is unset.
set -eu
JOBS="${1:-${AGENT_DISPATCH_JOBS:-${AGENT_HOME:-.}/.dispatch/jobs.log}}"
dirname -- "$JOBS"
