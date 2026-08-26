#!/usr/bin/env python3
"""W7D: artifact_reader resolves cycle, shared, and legacy layouts in that order."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_reader as reader  # noqa: E402

CAMP = "camp_" + "a" * 32
CYC = "cyc_" + "b" * 32
REF = "ref_" + "c" * 32
RREV_OLD = "rrev_" + "d" * 32
RREV_NEW = "rrev_" + "e" * 32


def build(root: Path) -> None:
    cyc = root / "campaigns" / CAMP / "cycles" / CYC / "artifacts"
    (cyc / "plans" / "2026-08-26_cycle-plan").mkdir(parents=True)
    (cyc / "spec" / "component").mkdir(parents=True)
    (root / "plans" / "2026-01-01_legacy-plan").mkdir(parents=True)
    (root / "plans" / ".hidden").mkdir()
    (root / "spec" / "component" / "_internal").mkdir(parents=True)  # retirement exclusion, no prd
    ref = root / "shared" / "spec" / REF
    for rrev in (RREV_OLD, RREV_NEW):
        (ref / "revisions" / rrev / "component").mkdir(parents=True)
        (ref / "revisions" / rrev / "prd.md").write_text(rrev, encoding="utf-8")
    (ref / "reference.json").write_text(json.dumps({
        "latest_revision_id": RREV_NEW, "updated_on": "2026-08-26T00:00:00Z", "revisions": [RREV_OLD, RREV_NEW],
    }), encoding="utf-8")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".agent_reports"
        self.root.mkdir()
        build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bucket_dirs_order_is_cycle_shared_legacy(self):
        layouts = [meta["layout"] for _, meta in reader.bucket_dirs(self.root, "spec")]
        self.assertEqual(layouts, ["cycle", "shared", "legacy-readonly"])
        shared = [p for p, m in reader.bucket_dirs(self.root, "spec") if m["layout"] == "shared"][0]
        self.assertEqual(shared.name, RREV_NEW)

    def test_glob_spans_layouts_and_skips_hidden(self):
        names = [p.name for p in reader.glob_bucket(self.root, "plans", "*plan")]
        self.assertEqual(names, ["2026-08-26_cycle-plan", "2026-01-01_legacy-plan"])
        self.assertEqual(reader.glob_bucket(self.root, "plans", ".hidden"), [])

    def test_spec_dir_prefers_open_cycle_then_latest_shared(self):
        cycle_dir = self.root / "campaigns" / CAMP / "cycles" / CYC
        path, layout = reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir))
        self.assertEqual((path, layout), (cycle_dir / "artifacts" / "spec", "cycle"))
        path, layout = reader.spec_dir(self.root)
        self.assertEqual(layout, "shared")
        self.assertEqual(path.name, RREV_NEW)

    def test_legacy_spec_without_prd_is_not_the_spec_dir(self):
        import shutil
        shutil.rmtree(self.root / "shared")
        self.assertIsNone(reader.spec_dir(self.root))
        (self.root / "spec" / "prd.md").write_text("legacy", encoding="utf-8")
        self.assertEqual(reader.spec_dir(self.root)[1], "legacy-readonly")

    def test_missing_layouts_yield_empty_lists(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(reader.bucket_dirs(empty, "plans"), [])
        self.assertEqual(reader.glob_bucket(empty, "plans", "*"), [])

    def test_cli_round_trip(self):
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reader.main(["spec-dir", "--artifact-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["layout"], "shared")


if __name__ == "__main__":
    unittest.main()
