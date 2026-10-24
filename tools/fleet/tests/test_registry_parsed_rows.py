"""File-generation reuse contract for parsed registry rows."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from fleet.collectors import dispatch


class ParsedRegistryRowsTest(unittest.TestCase):
    def test_reuses_immutable_rows_until_file_generation_changes(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(path)
                first_misses = dispatch._parse_pipe_meta_cached.cache_info().misses
                second = dispatch.parsed_registry_rows(path)
            self.assertIs(first, second)
            self.assertEqual(dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses)
            self.assertEqual(first[0][0], 0)
            self.assertEqual(len(first[0][1]), 6)
            with self.assertRaises(TypeError):
                first[0][2]["mode"] = "changed"

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("bad\trow\n")
            with path.open("a") as stream:
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                third = dispatch.parsed_registry_rows(path)
            self.assertIsNot(first, third)
            self.assertEqual(
                dispatch._parse_pipe_meta_cached.cache_info().misses, first_misses + 1)
            self.assertEqual(len(third), 2)
            self.assertEqual(third[-1][0], 3)
            self.assertEqual(third[0], first[0])

    def test_registry_order_keeps_physical_zero_based_line_positions(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            valid = "open\tslug\t/repo\t/work\t2026-01-01\tcapability=code\n"
            path.write_text("\nmalformed\n" + valid)
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])

            with path.open("a") as stream:
                stream.write("\n")
                stream.write("also\tmalformed\n")
                stream.write("open\tslug2\t/repo\t/work\t2026-01-02\tmode=dev\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                appended = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in appended], [2, 5])

    def test_multiple_appends_reuse_each_prefix_without_dropping_or_repeating_rows(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.log"
            path.write_text("\nmalformed\ndate\topen\t/repo\t/work\tslug0\tcapability=cap0\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                rows = dispatch.parsed_registry_rows(path)
            self.assertEqual([row[0] for row in rows], [2])
            misses = dispatch._parse_pipe_meta_cached.cache_info().misses

            expected_orders = [2]
            for index in range(1, 4):
                with path.open("a") as stream:
                    stream.write(f"date\topen\t/repo\t/work\tslug{index}\tcapability=cap{index}\n")
                with dispatch._dispatch_registry_cache.registry_read_scope():
                    rows = dispatch.parsed_registry_rows(path)
                expected_orders.append(index + 2)
                self.assertEqual([row[0] for row in rows], expected_orders)
                self.assertEqual(
                    [row[1][4] for row in rows], [f"slug{i}" for i in range(index + 1)])
                self.assertEqual(
                    dispatch._parse_pipe_meta_cached.cache_info().misses, misses + index)

    def test_cache_is_isolated_by_path_and_file_generation(self):
        dispatch._parse_pipe_meta_cached.cache_clear()
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.log"
            second_path = Path(directory) / "second.log"
            first_path.write_text("date\topen\t/repo\t/work\tfirst\tcapability=first\n")
            second_path.write_text("date\topen\t/repo\t/work\tsecond\tcapability=second\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                first = dispatch.parsed_registry_rows(first_path)
                second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual(first[0][1][4], "first")
            self.assertEqual(second[0][1][4], "second")

            first_path.write_text(
                "date\topen\t/repo\t/work\treplaced\tcapability=replaced\n"
                "date\topen\t/repo\t/work\tnew\tcapability=new\n")
            with dispatch._dispatch_registry_cache.registry_read_scope():
                replaced = dispatch.parsed_registry_rows(first_path)
                unchanged_second = dispatch.parsed_registry_rows(second_path)
            self.assertEqual([row[1][4] for row in replaced], ["replaced", "new"])
            self.assertIs(second, unchanged_second)

    def test_missing_registry_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(dispatch.parsed_registry_rows(Path(directory) / "missing"), ())


if __name__ == "__main__":
    unittest.main()
