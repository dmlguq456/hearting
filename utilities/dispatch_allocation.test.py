#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
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

    def test_inert_keys_name_every_configured_key_the_strategy_never_reads(self):
        inert = self.allocation.inert_allocation_keys
        full = {
            "strategy": "capacity-aware", "window": 30, "usage_gate_used_percent": 85,
            "depth_affinity": {"owner": "claude", "worker": "codex"},
            "depth_affinity_weight": 0.65, "usage_headroom_exponent": 2,
        }
        # The 2026-08-29 finding: three of four optional keys were dead on the
        # user's legacy strategy while the file validated cleanly.
        capacity_aware = inert(full)
        self.assertEqual(
            sorted(capacity_aware),
            ["depth_affinity_weight", "usage_gate_used_percent", "usage_headroom_exponent"],
        )
        self.assertIn("ignored under capacity-aware", capacity_aware["usage_headroom_exponent"])
        self.assertIn("ignored under capacity-aware", capacity_aware["usage_gate_used_percent"])
        self.assertIn("tie-break", capacity_aware["depth_affinity_weight"])
        self.assertEqual(inert({**full, "strategy": "balanced"}), {})
        legacy = inert({**full, "strategy": "least-recent-attempts"})
        self.assertEqual(
            sorted(legacy),
            ["depth_affinity", "depth_affinity_weight", "usage_gate_used_percent", "usage_headroom_exponent"],
        )

    def test_harness_weights_is_read_by_balanced_only(self):
        inert = self.allocation.inert_allocation_keys
        base = {"window": 30, "harness_weights": {"opencode": 0.3}}
        self.assertEqual(inert({**base, "strategy": "balanced"}), {})
        self.assertEqual(
            inert({**base, "strategy": "capacity-aware"}),
            {"harness_weights": "ignored under capacity-aware"},
        )
        self.assertEqual(
            inert({**base, "strategy": "least-recent-attempts"}),
            {"harness_weights": "ignored under least-recent-attempts"},
        )

    def test_field_weights_is_read_by_balanced_only(self):
        inert = self.allocation.inert_allocation_keys
        base = {"window": 30, "field_weights": {"claude": 0.95, "codex": 0.85, "opencode": 1.0}}
        self.assertEqual(inert({**base, "strategy": "balanced"}), {})
        self.assertEqual(inert({**base, "strategy": "capacity-aware"}),
                         {"field_weights": "ignored under capacity-aware"})

    def test_effective_weights_are_the_per_harness_product(self):
        effective = self.allocation.effective_harness_weights
        row = {"claude": 0.95, "codex": 0.85, "opencode": 1.0}
        got = effective({"harness_weights": {"opencode": 0.4, "codex": 0.7}, "field_weights": row})
        self.assertEqual(sorted(got), ["claude", "codex", "opencode"])
        self.assertAlmostEqual(got["claude"], 0.95)
        self.assertAlmostEqual(got["codex"], 0.595)
        self.assertAlmostEqual(got["opencode"], 0.4)
        # the field row alone, and the harness weights alone
        self.assertEqual(effective({"field_weights": row}), row)
        self.assertEqual(effective({"harness_weights": {"opencode": 0.4}}), {"opencode": 0.4})

    def test_a_route_without_field_weights_reads_neutral_exactly_as_before(self):
        effective = self.allocation.effective_harness_weights
        weights = {"opencode": 0.3}
        # same object back: a route sealed before the key existed, or with no field, is untouched
        self.assertIs(effective({"harness_weights": weights}), weights)
        self.assertIsNone(effective({"strategy": "balanced"}))
        self.assertIsNone(effective({"field_weights": {}}))
        self.assertIsNone(effective(None))
        self.assertEqual(effective({"harness_weights": weights, "field_weights": {}}), weights)

    def test_every_caller_that_hands_weights_to_the_selector_goes_through_the_one_product(self):
        # The product lives in one place; a caller that passes `harness_weights` straight from the
        # sealed allocation would silently skip the field row. (The Fleet title worker has no field.)
        import re
        direct = re.compile(r"harness_weights\s*=\s*allocation\.get\(")
        for name in ("stage-dispatch-fallback.py", "dispatch-owner.py", "dispatch-batch.py",
                     "dispatch_replacement.py"):
            text = (ROOT / "utilities" / name).read_text(encoding="utf-8")
            self.assertIsNone(direct.search(text), name)
            self.assertIn("effective_harness_weights(", text, name)

    def test_inert_keys_only_report_keys_that_are_actually_present(self):
        inert = self.allocation.inert_allocation_keys
        self.assertEqual(inert({"strategy": "capacity-aware", "window": 30}), {})
        self.assertEqual(
            inert({"strategy": "capacity-aware", "window": 30, "usage_headroom_exponent": 2}),
            {"usage_headroom_exponent": "ignored under capacity-aware"},
        )
        self.assertEqual(inert({"strategy": "no-such-strategy", "usage_headroom_exponent": 2}), {})
        self.assertEqual(inert(None), {})
        self.assertEqual(inert("balanced"), {})


if __name__ == "__main__":
    unittest.main()
