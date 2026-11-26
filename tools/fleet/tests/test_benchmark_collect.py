"""Small contract tests for live and registry benchmark summaries."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).with_name("benchmark_collect.py")
SPEC = importlib.util.spec_from_file_location("benchmark_collect", MODULE_PATH)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class BenchmarkCollectTest(unittest.TestCase):
    def test_summary_keeps_each_wall_and_cpu_sample(self):
        result = benchmark._summary([
            {"wall_ns": 2_000_000_000, "cpu_ns": 1_000_000_000},
            {"wall_ns": 4_000_000_000, "cpu_ns": 3_000_000_000},
        ])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["wall_seconds"], [2.0, 4.0])
        self.assertEqual(result["cpu_seconds"], [1.0, 3.0])
        self.assertEqual(result["wall_median_seconds"], 3.0)
        self.assertEqual(result["cpu_median_seconds"], 2.0)

    def test_source_selection_puts_requested_tree_first(self):
        original = list(__import__("sys").path)
        try:
            benchmark._source("/tmp/fleet-benchmark-tree")
            self.assertEqual(__import__("sys").path[0], "/tmp/fleet-benchmark-tree/tools")
        finally:
            __import__("sys").path[:] = original

    def test_generation_alone_does_not_make_snapshot_ready(self):
        class Snapshot:
            generation = 9
            observations = {}

        readiness = benchmark._readiness(Snapshot())
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["all_new"])
        self.assertIn("required-details", readiness["pending"])

    def test_pending_required_source_keeps_ready_false(self):
        class Snapshot:
            observations = {
                "basic": {"state": "complete", "all_new": True},
                "required-details": {"state": "pending"},
                "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
                "remote-host": {"state": "success", "attempted": True},
                "usage:claude": {"state": "complete"},
                "usage:codex": {"state": "complete"},
                "usage:opencode": {"state": "complete"},
            }

        readiness = benchmark._readiness(Snapshot())
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["all_new"])
        self.assertEqual(readiness["pending"], ["required-details"])

    def test_runtime_policy_is_used_and_unsupported_options_are_disclosed(self):
        class Runtime:
            def collection_options(self, first_complete):
                return {"fast_first": not first_complete,
                        "include_report_verification": False}

        def baseline_collector(harness_filter=None, fast_first=False):
            del harness_filter, fast_first

        options, unsupported = benchmark._collection_policy(
            baseline_collector, Runtime(), generation=0)
        self.assertEqual(options, {"fast_first": False})
        self.assertEqual(unsupported, ["include_report_verification"])

    def test_sample_phase_uses_observed_readiness(self):
        pending = {"ready": False, "all_new": False,
                   "sources": {"required-details": {"state": "pending"}}}
        ready = {"ready": True, "all_new": True,
                 "sources": {"required-details": {"state": "complete"}}}
        self.assertEqual(benchmark._sample_phase(pending, generation=5), "required-detail")
        self.assertEqual(benchmark._sample_phase(ready, generation=5), "all-new")


if __name__ == "__main__":
    unittest.main()
