#!/usr/bin/env python3
"""W7D: artifact_reader resolves cycle, shared, and legacy layouts in that order."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_cutover as cutover  # noqa: E402
import artifact_producer as producer  # noqa: E402
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

    def test_spec_dir_prefers_an_open_cycle_only_once_it_carries_a_spec(self):
        cycle_dir = self.root / "campaigns" / CAMP / "cycles" / CYC
        cycle_spec = cycle_dir / "artifacts" / "spec"
        # The producer creates `artifacts/spec` before anything is written into
        # it; an empty bucket must not hide the revision that still governs.
        path, layout = reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir))
        self.assertEqual(layout, "shared")
        self.assertEqual(path.name, RREV_NEW)
        (cycle_spec / "prd.md").write_text("in flight", encoding="utf-8")
        self.assertEqual(reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir)),
                         (cycle_spec, "cycle"))
        # a component-only cycle tree is a spec tree too
        (cycle_spec / "prd.md").unlink()
        (cycle_spec / "component" / "prd.md").write_text("in flight", encoding="utf-8")
        self.assertEqual(reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir)),
                         (cycle_spec, "cycle"))
        path, layout = reader.spec_dir(self.root)
        self.assertEqual(layout, "shared")
        self.assertEqual(path.name, RREV_NEW)

    def test_spec_dir_and_the_spec_gate_rank_the_same_governing_tree(self):
        """The reader and `artifact_cutover.prd_candidates` must never disagree
        about which tree governs; the gate would otherwise force a read of one
        tree while every reader consulted another."""
        producer.activate(self.root, repository_id="repo_" + "1" * 32,
                          artifact_root_id="root_" + "2" * 32)
        cycle_dir = self.root / "campaigns" / CAMP / "cycles" / CYC
        cycle_spec = cycle_dir / "artifacts" / "spec"
        (self.root / "spec" / "prd.md").write_text("stale legacy", encoding="utf-8")
        previous = os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
        self.addCleanup(lambda: os.environ.__setitem__("AGENT_ARTIFACT_CYCLE_DIR", previous)
                        if previous is not None else os.environ.pop("AGENT_ARTIFACT_CYCLE_DIR", None))
        os.environ["AGENT_ARTIFACT_CYCLE_DIR"] = str(cycle_dir)
        cases = [
            # (label, populate, expect_cycle)
            ("empty cycle", lambda: None, False),
            # a tree that carries only pipeline_state.yaml has no governing prd,
            # so it must not outrank the revision that does
            ("pipeline-state only", lambda: (cycle_spec / "pipeline_state.yaml").write_text("x", encoding="utf-8"), False),
            ("cycle prd", lambda: (cycle_spec / "prd.md").write_text("in flight", encoding="utf-8"), True),
        ]
        for label, populate, expect_cycle in cases:
            with self.subTest(case=label):
                populate()
                tree = reader.spec_dir(self.root)[0]
                self.assertEqual(cutover.prd_candidates(self.root),
                                 cutover.tree_prd_candidates(tree))
                self.assertEqual(tree == cycle_spec, expect_cycle)

    def test_a_pipeline_state_only_tree_answers_only_when_no_prd_exists(self):
        """The W7D pipeline-state fallback stays, but it can never contradict the
        spec gate: it only answers when no layout carries a prd.md at all."""
        import shutil
        cycle_dir = self.root / "campaigns" / CAMP / "cycles" / CYC
        cycle_spec = cycle_dir / "artifacts" / "spec"
        (cycle_spec / "pipeline_state.yaml").write_text("x", encoding="utf-8")
        # shared still has a prd.md -> shared governs for both resolvers
        self.assertEqual(reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir))[1], "shared")
        shutil.rmtree(self.root / "shared")
        self.assertEqual(reader.spec_dir(self.root, open_cycle_dir=str(cycle_dir)),
                         (cycle_spec, "cycle"))
        self.assertEqual(cutover.prd_candidates(self.root), [])

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
