"""Small contract tests for live and registry benchmark summaries."""
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


if __name__ == "__main__":
    unittest.main()
