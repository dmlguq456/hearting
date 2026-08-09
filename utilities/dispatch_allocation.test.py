#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "utilities" / "dispatch_allocation.py"


class DispatchAllocationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("dispatch_allocation", MODULE)
        cls.allocation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.allocation)

    def test_recent_exact_attempt_counts_and_least_used_ranking(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            rows = []
            for index, harness in enumerate(
                ("claude", "claude", "codex", "claude", "opencode", "codex")
            ):
                rows.append(
                    f"2026-08-09T00:00:0{index}Z\tdone\t/r\t/w\tn{index}\t"
                    "attempt_schema_version=2,registered_worker=1,"
                    f"attempt_id=att-{index:012d},harness={harness}"
                )
            rows.append(
                "2026-08-09T00:00:07Z\tdone\t/r\t/w\tlegacy\t"
                "harness=opencode,note=completed-marker"
            )
            jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")
            counts = self.allocation.attempt_counts(jobs, window=6)
            self.assertEqual(counts, {"claude": 3, "codex": 2, "opencode": 1})
            ranked = self.allocation.rank_harnesses(
                ["claude", "codex", "opencode"],
                counts,
                declared_order=["claude", "codex", "opencode"],
            )
            self.assertEqual(ranked, ["opencode", "codex", "claude"])

    def test_window_and_declared_order_make_ties_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            jobs.write_text(
                "2026-08-09T00:00:00Z\tdone\t/r\t/w\ta\t"
                "attempt_schema_version=2,registered_worker=1,"
                "attempt_id=att-000000000001,harness=claude\n"
                "2026-08-09T00:00:01Z\tdone\t/r\t/w\tb\t"
                "attempt_schema_version=2,registered_worker=1,"
                "attempt_id=att-000000000002,harness=codex\n",
                encoding="utf-8",
            )
            counts = self.allocation.attempt_counts(jobs, window=1)
            self.assertEqual(counts, {"claude": 0, "codex": 1, "opencode": 0})
            self.assertEqual(
                self.allocation.rank_harnesses(
                    ["opencode", "claude", "codex"],
                    counts,
                    declared_order=["claude", "codex", "opencode"],
                ),
                ["claude", "opencode", "codex"],
            )


if __name__ == "__main__":
    unittest.main()
