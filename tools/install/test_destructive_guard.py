#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import check_destructive_calls as guard


class DestructiveGuardTest(unittest.TestCase):
    def _scan(self, name: str, body: str) -> set[str]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / name
            path.write_text(body, encoding="utf-8")
            return {finding.category for finding in guard.scan_paths([path])}

    def test_rejects_unannotated_deletion(self) -> None:
        categories = self._scan("bad.py", "def clean(path):\n    path.unlink()\n")
        self.assertIn("unannotated-destructive", categories)

    def test_rejects_private_home_with_inherited_selector(self) -> None:
        categories = self._scan(
            "bad.sh", '#!/bin/sh\nHOME="$TMP/home"\nexport HOME\n'
        )
        self.assertIn("inherited-selector", categories)

    def test_rejects_unlink_then_write_rollback_even_when_annotated(self) -> None:
        categories = self._scan(
            "bad.py",
            "def _restore(path):\n"
            "    # destructive-ok: reason=synthetic exact leaf; boundary=one test leaf\n"
            "    path.unlink()\n"
            "    path.write_text('old')\n",
        )
        self.assertIn("unlink-then-write", categories)

    def test_rejects_unlink_then_symlink_rollback_even_when_annotated(self) -> None:
        categories = self._scan(
            "bad.py",
            "def _restore(path, prior):\n"
            "    # destructive-ok: reason=synthetic exact leaf; boundary=one test leaf\n"
            "    path.unlink()\n"
            "    path.symlink_to(prior)\n",
        )
        self.assertIn("unlink-then-write", categories)

    def test_accepts_narrow_internal_temp_annotation(self) -> None:
        categories = self._scan(
            "good.py",
            "def clean(temp):\n"
            "    # destructive-ok: reason=discard failed sibling temp; "
            "boundary=temp created by this invocation\n"
            "    temp.unlink()\n",
        )
        self.assertEqual(categories, set())


if __name__ == "__main__":
    unittest.main()
