#!/usr/bin/env python3
"""The ledger-invariant check must catch the incident that motivated it."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_ledger_invariant", ROOT / "tools" / "check_ledger_invariant.py"
)
CHECK = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(CHECK)


class LedgerInvariantTest(unittest.TestCase):
    def _run(self, suite: Path, jobs: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools/check_ledger_invariant.py"),
             str(suite), "--jobs", str(jobs)],
            cwd=str(ROOT), capture_output=True, text=True,
        )

    def test_a_suite_that_appends_to_the_ledger_is_caught(self):
        # The 2026-09-05 incident in miniature: a suite that writes an attempt
        # row to the ledger it was handed.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            jobs = base / "jobs.log"
            jobs.write_text("2026-09-06T00:00:00Z\tdone\tr\tw\tslug\tattempt_id=att-real\n",
                            encoding="utf-8")
            suite = base / "offender.test.py"
            suite.write_text(
                "from pathlib import Path\n"
                f"p = Path({str(jobs)!r})\n"
                "with p.open('a', encoding='utf-8') as fh:\n"
                "    fh.write('2026-09-04T00:00:00Z\\tdone\\trepo\\tworktree\\tslug\\t"
                "attempt_id=att-fixture\\n')\n",
                encoding="utf-8")
            result = self._run(suite, jobs)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("ledger-invariant: FAIL", result.stderr)
            self.assertIn("+1 lines", result.stdout + result.stderr)
            self.assertIn("att-fixture", result.stderr, "the offending row must be shown")

    def test_a_clean_suite_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            jobs = base / "jobs.log"
            jobs.write_text("2026-09-06T00:00:00Z\tdone\tr\tw\tslug\tattempt_id=att-real\n",
                            encoding="utf-8")
            suite = base / "clean.test.py"
            suite.write_text("print('no ledger writes here')\n", encoding="utf-8")
            result = self._run(suite, jobs)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ledger-invariant: PASS", result.stdout)

    def test_a_rewrite_is_caught_too_not_only_an_append(self):
        # Truncation/rewrite is the shape an over-eager cleanup produces; the
        # digest catches it where a line count alone would not.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            jobs = base / "jobs.log"
            jobs.write_text("row-one\nrow-two\n", encoding="utf-8")
            suite = base / "rewriter.test.py"
            suite.write_text(
                "from pathlib import Path\n"
                f"Path({str(jobs)!r}).write_text('row-one\\nrow-CHANGED\\n')\n",
                encoding="utf-8")
            result = self._run(suite, jobs)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("ledger-invariant: FAIL", result.stderr)

    def test_an_absent_ledger_is_not_a_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            suite = base / "clean.test.py"
            suite.write_text("pass\n", encoding="utf-8")
            result = self._run(suite, base / "never-created.log")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_live_ledger_resolves_the_way_the_harness_does(self):
        # The check must watch the ledger the harness itself would pick with no
        # override -- the one the incident wrote to.
        self.assertEqual(CHECK.live_jobs_path().name, "jobs.log")
        self.assertTrue(CHECK.live_jobs_path().is_absolute())


if __name__ == "__main__":
    unittest.main()
