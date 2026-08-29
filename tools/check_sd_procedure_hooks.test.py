#!/usr/bin/env python3
"""Regression fixtures for the SD procedure-hook ledger checker."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).with_name("check-sd-procedure-hooks.py")
TSV_HEADER = "sd\tcaller_kind\tanchor\tstatus"


class HookCheckerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tools").mkdir()
        shutil.copy2(SOURCE, self.root / "tools/check-sd-procedure-hooks.py")

    def tearDown(self):
        self.tmp.cleanup()

    def write_tsv(self, rows):
        path = self.root / "tools/sd-procedure-hooks.tsv"
        path.write_text("\n".join([TSV_HEADER, *rows]) + "\n", encoding="utf-8")
        return path

    def run_checker(self, *args):
        return subprocess.run(
            [sys.executable, "tools/check-sd-procedure-hooks.py", *args],
            cwd=self.root, capture_output=True, text=True,
        )

    def test_default_battery_succeeds_without_prd(self):
        source_tsv = (SOURCE.parent / "sd-procedure-hooks.tsv").read_text(encoding="utf-8")
        (self.root / "tools/sd-procedure-hooks.tsv").write_text(source_tsv, encoding="utf-8")
        (self.root / "core").mkdir()
        (self.root / "core/HOOKS.md").write_text("## Invariant Catalog\nSD-111\n", encoding="utf-8")
        result = self.run_checker("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check=ok", result.stdout)

    def test_prd_sd_missing_from_tsv_fails(self):
        self.write_tsv(["SD-48\tprocedure-step\t-\tbaseline"])
        prd = self.root / "prd.md"
        prd.write_text("## New rule SD-999\n", encoding="utf-8")
        result = self.run_checker("--prd", str(prd))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PRD SDs missing", result.stderr)

    def test_baseline_increase_fails_and_decrease_passes(self):
        path = self.write_tsv([
            "SD-48\tprocedure-step\t-\tbaseline",
            "SD-49\tprocedure-step\t-\tbaseline",
        ])
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], cwd=self.root, check=True)
        path.write_text("\n".join([TSV_HEADER, "SD-48\tprocedure-step\t-\tbaseline", ""]), encoding="utf-8")
        self.assertEqual(self.run_checker("--check").returncode, 0)
        path.write_text("\n".join([TSV_HEADER, "SD-48\tprocedure-step\t-\tbaseline", "SD-49\tprocedure-step\t-\tbaseline", "SD-50\tprocedure-step\t-\tbaseline", ""]), encoding="utf-8")
        result = self.run_checker("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline increased", result.stderr)

    def test_each_anchor_kind_has_success_and_failure(self):
        (self.root / "docs").mkdir()
        (self.root / "docs/procedure.md").write_text("## Step\nSD-201\n", encoding="utf-8")
        (self.root / "producer.py").write_text("def publish():\n    return 'SD-202'\n", encoding="utf-8")
        (self.root / "fixture.py").write_text("def gate():\n    return 'SD-203'\n", encoding="utf-8")
        self.write_tsv([
            "SD-201\tprocedure-step\tdocs/procedure.md#Step\twired",
            "SD-202\tproducer-symbol\tproducer.py::publish\twired",
            "SD-203\tgate-fixture\tfixture.py::gate\twired",
        ])
        self.assertEqual(self.run_checker("--check").returncode, 0)
        self.write_tsv(["SD-201\tprocedure-step\tdocs/procedure.md#Missing\twired"])
        self.assertNotEqual(self.run_checker("--check").returncode, 0)

    def test_write_mode_does_not_change_tsv(self):
        (self.root / "core").mkdir()
        (self.root / "core/HOOKS.md").write_text("## Invariant Catalog\nSD-111\n", encoding="utf-8")
        source_tsv = (SOURCE.parent / "sd-procedure-hooks.tsv").read_text(encoding="utf-8")
        path = self.root / "tools/sd-procedure-hooks.tsv"
        path.write_text(source_tsv, encoding="utf-8")
        before = (path.stat().st_mtime_ns, path.read_bytes())
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
