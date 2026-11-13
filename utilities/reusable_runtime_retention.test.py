"""Isolated checks for reusable runtime cache retention."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import reusable_runtime_retention as R  # noqa: E402

DAY = 86400 * 1_000_000_000
CYC = "cyc_" + "a" * 32
CYC2 = "cyc_" + "b" * 32


def stamp(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")


class ReusableRuntimeRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reusable-runtime-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name).resolve()
        self.jobs = self.state / "jobs.log"
        self.jobs.write_text("")
        # File ctime cannot be set back, so the test clock runs ahead of the real one.
        self.now = time.time_ns() + 30 * DAY
        self.rows = []

    def entry(self, cycle=CYC, name="venv-1", age_days=10, files=("lib/a.py", "bin/python")):
        path = self.state / "reusable-runtimes" / cycle / name
        for rel in files:
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * 100)
            old = (self.now - age_days * DAY) / 1e9
            os.utime(target, (old, old))
        return path

    def row(self, status, text, ended=None, attempt="att1"):
        meta = f"attempt_id={attempt},note={text}"
        self.rows.append(f"{stamp(self.now if ended is None else ended)}\t{status}\t/repo\t/wt\tjob\t{meta}")
        self.jobs.write_text("\n".join(self.rows) + "\n")

    def verdicts(self):
        return {Path(r["path"]).name: r for r in R.plan(self.jobs, now_ns=self.now)}

    def sweep(self, **kw):
        return R.sweep(self.jobs, now_ns=self.now, force=True, **kw)

    def test_idle_entry_is_removed_and_empty_cycle_dir_goes_with_it(self):
        path = self.entry()
        result = self.sweep()
        self.assertEqual(result["removed"], 1)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_recent_file_activity_keeps_but_directory_mtime_alone_does_not(self):
        path = self.entry(age_days=10)
        fresh = path / "lib" / "fresh.json"
        fresh.write_text("{}")
        os.utime(fresh, ((self.now - DAY) / 1e9,) * 2)
        self.assertEqual(self.verdicts()["venv-1"]["reason"], "recently-used")
        fresh.unlink()
        os.utime(path, None)
        os.utime(path / "lib", None)
        self.assertEqual(self.verdicts()["venv-1"]["verdict"], "eligible")

    def test_open_or_recent_registry_row_keeps_and_old_terminal_row_does_not(self):
        path = self.entry()
        self.row("running", CYC)
        self.assertEqual(self.verdicts()["venv-1"]["reason"], "open-run")
        # The same attempt's later terminal row replaces the open one (latest row wins).
        self.row("done", CYC, ended=self.now - 2 * DAY)
        self.assertEqual(self.verdicts()["venv-1"]["reason"], "recently-used")
        self.rows.clear()
        self.row("done", str(path), ended=self.now - 9 * DAY)
        self.assertEqual(self.verdicts()["venv-1"]["verdict"], "eligible")

    def test_unreadable_registry_keeps_everything(self):
        self.entry()
        self.jobs.write_text("torn line without newline")
        self.assertEqual(self.verdicts()["venv-1"]["reason"], "registry-unreadable")
        self.assertEqual(self.sweep()["removed"], 0)

    def test_a_live_process_inside_keeps_it(self):
        path = self.entry()
        child = subprocess.Popen(["sleep", "30"], cwd=path)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(self.verdicts()["venv-1"]["reason"], "process-uses-it")
        self.assertEqual(self.sweep()["removed"], 0)
        self.assertTrue(path.exists())

    def test_links_are_never_followed_and_foreign_shapes_stay(self):
        outside = self.state / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        path = self.entry()
        (path / "lib" / "link").symlink_to(outside)
        link_entry = self.state / "reusable-runtimes" / CYC2
        link_entry.mkdir()
        (link_entry / "linked").symlink_to(outside)
        (self.state / "reusable-runtimes" / "stray-dir").mkdir()
        (self.state / "reusable-runtimes" / "stray-file").write_text("x")
        result = self.sweep()
        self.assertEqual(result["removed"], 1)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue((link_entry / "linked").is_symlink())
        self.assertTrue((self.state / "reusable-runtimes" / "stray-dir").exists())
        self.assertTrue((self.state / "reusable-runtimes" / "stray-file").exists())

    def test_plan_changes_nothing_and_due_gate_and_limit(self):
        for index in range(R.REMOVE_LIMIT + 2):
            self.entry(name=f"e{index}")
        before = sorted(str(p) for p in (self.state / "reusable-runtimes").rglob("*"))
        R.plan(self.jobs, now_ns=self.now)
        self.assertEqual(before, sorted(str(p) for p in (self.state / "reusable-runtimes").rglob("*")))
        self.assertEqual(self.sweep()["removed"], R.REMOVE_LIMIT)
        self.assertEqual(R.sweep(self.jobs, now_ns=self.now + 1)["skipped"], "not-due")
        later = self.now + R.DUE_SECONDS * 1_000_000_000
        self.assertEqual(R.sweep(self.jobs, now_ns=later)["removed"], 2)

    def test_interrupted_removal_is_finished_and_missing_root_is_quiet(self):
        leftover = self.state / "reusable-runtimes" / CYC / (R.RETIRING_PREFIX + "ab")
        (leftover / "x").mkdir(parents=True)
        (leftover / "x" / "f").write_text("x")
        self.sweep()
        self.assertFalse(leftover.exists())
        empty = Path(self.tmp.name) / "none"
        empty.mkdir()
        self.assertEqual(R.sweep(empty / "jobs.log", force=True), {"skipped": "no-reusable-runtimes"})

    def test_cli_prints_the_plan_without_removing(self):
        path = self.entry()
        out = subprocess.run([sys.executable, str(ROOT / "utilities" / "reusable_runtime_retention.py"),
                              str(self.jobs)], capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out.splitlines()[0])["path"], str(path))
        self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
