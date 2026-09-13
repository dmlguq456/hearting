#!/usr/bin/env sh
# Compatibility CLI: native quota and legacy reset semantics share one reader.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/dispatch_capacity_evidence.py" states "$@"
