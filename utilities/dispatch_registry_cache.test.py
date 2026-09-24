#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dispatch_registry_cache as CACHE  # noqa: E402


class RegistryLinesCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = Path(self.tmp.name) / "jobs.log"
        # A distinct path per test keeps the process-wide cache from leaking
        # a stale entry from an earlier test into this one.
        CACHE._CACHE.clear()

    def test_unchanged_file_parses_only_once(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        with mock.patch.object(Path, "read_text", autospec=True,
                                side_effect=Path.read_text) as spy:
            first = CACHE.registry_lines(self.jobs)
            second = CACHE.registry_lines(self.jobs)
            third = CACHE.registry_lines(self.jobs)
        self.assertEqual(first, second)
        self.assertEqual(second, third)
        self.assertEqual(spy.call_count, 1)

    def test_appended_write_invalidates_the_cache(self):
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(first), 1)
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        second = CACHE.registry_lines(self.jobs)
        self.assertEqual(len(second), 2)

    def test_replaced_file_same_mtime_is_still_distinguished_by_size(self):
        # A same-second rewrite with a different byte count must not be
        # mistaken for the cached content merely because mtime resolution
        # (whole seconds on some filesystems) did not advance.
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        frozen_ns = self.jobs.stat().st_mtime_ns
        self.jobs.write_text(
            "a\tb\tc\td\te\tf=1\na\tb\tc\td\te\tf=2\n", encoding="utf-8"
        )
        os.utime(self.jobs, ns=(frozen_ns, frozen_ns))
        second = CACHE.registry_lines(self.jobs)
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 2)

    def test_missing_file_raises_like_a_direct_read(self):
        with self.assertRaises(FileNotFoundError):
            CACHE.registry_lines(self.jobs)

    def test_distinct_paths_cache_independently(self):
        other = Path(self.tmp.name) / "other.log"
        self.jobs.write_text("a\tb\tc\td\te\tf=1\n", encoding="utf-8")
        other.write_text("x\tb\tc\td\te\tf=9\n", encoding="utf-8")
        first = CACHE.registry_lines(self.jobs)
        second = CACHE.registry_lines(other)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
