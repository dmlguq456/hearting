"""Guard the production files exposed by the Claude Fleet runtime projection.

The adapter uses per-file symlinks to the canonical runtime. Fleet's test
inventory is maintained and run from tools/fleet/tests, not projected into the
adapter, so parity covers production files and does not require duplicate tests.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path

CANONICAL = Path(__file__).resolve().parents[1]           # tools/fleet
ADAPTER_RUNTIME = CANONICAL.parents[1] / "adapters" / "claude" / "tools" / "fleet"
SKIP_DIRS = {"__pycache__"}


def _runtime_tree(root: Path) -> dict:
    files = {}
    for p in root.rglob("*"):
        relative = p.relative_to(root)
        if (p.is_dir() or relative.parts[0] == "tests"
                or any(part in SKIP_DIRS for part in relative.parts)):
            continue
        files[str(relative)] = p.read_bytes()
    return files


class TestMirrorParity(unittest.TestCase):
    def test_adapter_runtime_matches_canonical_production_files(self):
        if not ADAPTER_RUNTIME.is_dir():
            self.skipTest("adapter runtime projection absent (non-repo install layout)")
        canon = _runtime_tree(CANONICAL)
        adapter = _runtime_tree(ADAPTER_RUNTIME)
        missing = sorted(set(canon) - set(adapter))
        extra = sorted(set(adapter) - set(canon))
        differing = sorted(k for k in set(canon) & set(adapter) if canon[k] != adapter[k])
        problems = []
        if missing:
            problems.append(f"adapter runtime missing canonical production files: {missing}")
        if extra:
            problems.append(f"adapter runtime has unexpected production files: {extra}")
        if differing:
            problems.append(f"adapter runtime differs from canonical production files: {differing}")
        self.assertFalse(
            problems,
            "Claude Fleet runtime projection mismatch: " + "; ".join(problems),
        )


if __name__ == "__main__":
    unittest.main()
