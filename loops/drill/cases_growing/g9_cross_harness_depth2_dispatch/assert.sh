#!/bin/bash
# Full-chain positive contract: bounded N-way Codex+Claude launch and Fleet visibility.
set -eu
CASE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HARNESS_ROOT=$(git -C "$CASE_DIR" rev-parse --show-toplevel)

PYTHONDONTWRITEBYTECODE=1 python3 "$HARNESS_ROOT/utilities/dispatch-batch.test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$HARNESS_ROOT/utilities/model_worker_governor.test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$HARNESS_ROOT/utilities/dispatch_contract.test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$HARNESS_ROOT/utilities/dispatch_node.test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$HARNESS_ROOT/utilities/stage_dispatch_fallback.test.py"

PYTHONDONTWRITEBYTECODE=1 HARNESS_ROOT="$HARNESS_ROOT" python3 - <<'PY'
import importlib.util
import os
from pathlib import Path

root = Path(os.environ["HARNESS_ROOT"])
path = root / "utilities" / "dispatch-batch.py"
spec = importlib.util.spec_from_file_location("dispatch_batch_drill", path)
batch = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(batch)

assert batch.SUPPORTED_BATCH_HARNESSES == ("codex", "claude")
assert "opencode" not in batch.SUPPORTED_BATCH_HARNESSES

source = path.read_text(encoding="utf-8")
assert '"reserve"' in source and '"--count"' in source
assert '"--batch-manifest"' in source and '"--batch-attempt-id"' in source
assert "as_completed" in source
assert '"parallel-group-cardinality"' in source
assert '"parallel-group-dependency-mismatch"' in source
assert '"concurrent_launch"' in source
contract = (root / "utilities" / "dispatch_contract.py").read_text(encoding="utf-8")
assert '"parallel-group-batch-required"' in contract
assert '"parallel-group-reservation-mismatch"' in contract
assert 'GROUP_REAP_PROOF = "pgid-empty-v1"' in contract
for adapter in ("codex", "claude"):
    wrapper = (root / "adapters" / adapter / "bin" / "dispatch-headless.py").read_text(encoding="utf-8")
    assert "replica_batch_expectation" in wrapper
    assert "REPLICA_RESERVATION_ROW_KEYS" in wrapper
    assert 'parallel_group=' in wrapper
    assert 'model_profile=' in wrapper
    assert '"launch_outcome": "governed-process-reaped"' in wrapper
print("PASS: bounded N-way Codex+Claude batch, 3-way live overlap/Fleet, profiles, provenance, bypass, and reap contract")
PY
