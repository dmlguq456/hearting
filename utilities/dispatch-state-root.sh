#!/usr/bin/env sh
# Print the canonical dispatch state root: the parent directory of the
# resolved canonical dispatch registry. Two override surfaces (SD-112
# §13.33.2-(8)): AGENT_DISPATCH_JOBS (registry-specifying, unchanged) and the
# installer-owned HARNESS_STATE_ROOT (install state root -- consumed inside
# stable_state_root(), never read directly here). This matches
# utilities/dispatch_contract.py's resolve_dispatch_state_root() chain
# (explicit jobs path arg -> inherited AGENT_DISPATCH_JOBS -> checked
# fallback, which defaults to the stable per-user root for a shared release
# or maintainer checkout, and to activation-owned state for a Codex bundle).
#
# Usage: dispatch-state-root.sh [jobs-path]
#   When needed, AGENT_HOME is resolved through agent-home.sh before the shared
#   Python contract selects or rejects the fallback.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
AGENT_HOME="${AGENT_HOME:-$("$SCRIPT_DIR/agent-home.sh")}"
python3 - "$SCRIPT_DIR" "$AGENT_HOME" "${1:-}" <<'PY'
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from dispatch_contract import DispatchContractError, resolve_dispatch_state_root

try:
    print(resolve_dispatch_state_root(
        Path(sys.argv[2]),
        explicit_jobs=sys.argv[3] or None,
        environ=os.environ,
    ))
except DispatchContractError as exc:
    print(f"dispatch-state-root: {exc.reason}: {exc.detail}", file=sys.stderr)
    raise SystemExit(65)
PY
