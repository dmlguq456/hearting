#!/usr/bin/env python3
"""W7D/W7G: artifact_reader resolves cycle, shared, and legacy layouts in that
order, and (D-77-a) surfaces a nonterminal resplit journal hold."""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_cutover as C  # noqa: E402
import artifact_reader as reader  # noqa: E402

CAMP = "camp_" + "a" * 32
CYC = "cyc_" + "b" * 32
REF = "ref_" + "c" * 32
RREV_OLD = "rrev_" + "d" * 32
RREV_NEW = "rrev_" + "e" * 32


def build(root: Path) -> None:
    campaign = root / "campaigns" / CAMP
    campaign.mkdir(parents=True)
    (campaign / "campaign.json").write_text(json.dumps({
        "campaign_id": CAMP, "title": "legacy fixture", "cycles": [CYC],
    }), encoding="utf-8")
    cycle = campaign / "cycles" / CYC
    cyc = cycle / "artifacts"
    (cyc / "plans" / "2026-08-26_cycle-plan").mkdir(parents=True)
    (cyc / "spec" / "component").mkdir(parents=True)
    (cycle / "manifest.json").write_text(json.dumps({
        "cycle": {"campaign_id": CAMP, "cycle_id": CYC},
    }), encoding="utf-8")
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

    def test_cycle_buckets_use_record_ids_across_readable_and_legacy_paths(self):
        campaign_id = "camp_" + "2" * 32
        cycle_id = "cyc_" + "3" * 32
        campaign = self.root / "campaigns" / "2026-09-04_readable-campaign"
        cycle = campaign / "renamed-sealed-cycle"
        (cycle / "artifacts" / "plans" / "readable-plan").mkdir(parents=True)
        (campaign / "campaign.json").write_text(json.dumps({
            "campaign_id": campaign_id, "title": "Readable campaign", "cycles": [cycle_id],
        }), encoding="utf-8")
        (cycle / "manifest.json").write_text(json.dumps({
            "cycle": {"campaign_id": campaign_id, "cycle_id": cycle_id},
        }), encoding="utf-8")

        rows = reader.cycle_bucket_dirs(self.root, "plans")
        by_name = {path.parent.parent.name: meta for path, meta in rows}
        self.assertEqual(by_name[CYC]["campaign_id"], CAMP)
        self.assertEqual(by_name[CYC]["cycle_id"], CYC)
        self.assertEqual(by_name["renamed-sealed-cycle"]["campaign_id"], campaign_id)
        self.assertEqual(by_name["renamed-sealed-cycle"]["cycle_id"], cycle_id)

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


class HoldTests(unittest.TestCase):
    """A-16.7 reader hold: `resplit_hold` is a thin re-export over the same
    on-disk journal `artifact_resplit.resplit_hold` reads (D-77-a: never
    process liveness)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".agent_reports"
        self.root.mkdir()
        build(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_journal(self, *, phase: str, lump_cycle_id: str = "cyc_" + "9" * 32):
        run_dir = C.migrations_dir(self.root) / f"20260903T000000Z-resplit-{lump_cycle_id}"
        run_dir.mkdir(parents=True)
        journal = {"schema_version": 1, "kind": "w7g-resplit-journal", "gate": "r2",
                  "phase": phase, "lump_cycle_id": lump_cycle_id, "started_at": "2026-09-03T00:00:00Z"}
        (run_dir / "journal-r2.json").write_text(json.dumps(journal), encoding="utf-8")
        return run_dir

    def test_a16_7_reader_hold_from_disk_journal(self):
        self.assertIsNone(reader.resplit_hold(self.root))
        self._write_journal(phase="renamed")
        hold = reader.resplit_hold(self.root)
        self.assertIsNotNone(hold)
        self.assertEqual(hold["code"], "resplit-in-progress")
        self.assertEqual(hold["gate"], "r2")
        self.assertEqual(hold["phase"], "renamed")

    def test_a16_7_reader_hold_is_none_once_terminal(self):
        self._write_journal(phase="complete")
        self.assertIsNone(reader.resplit_hold(self.root))

    def test_a16_7_reader_hold_cli_exit_code(self):
        import io
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reader.main(["hold", "--artifact-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out.getvalue())["hold"])
        self._write_journal(phase="renamed")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reader.main(["hold", "--artifact-root", str(self.root)])
        self.assertEqual(code, reader.HOLD_EXIT)
        self.assertEqual(json.loads(out.getvalue())["hold"]["code"], "resplit-in-progress")

    def test_a16_7_reader_public_shapes_unchanged(self):
        expected = {
            "bucket_dirs": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("bucket", "POSITIONAL_OR_KEYWORD", "str"),
                 ("include_shared", "KEYWORD_ONLY", "bool"), ("include_legacy", "KEYWORD_ONLY", "bool")],
                "List[Tuple[Path, Dict[str, str]]]"),
            "cycle_bucket_dirs": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("bucket", "POSITIONAL_OR_KEYWORD", "str")],
                "List[Tuple[Path, Dict[str, str]]]"),
            "iter_bucket_children": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("bucket", "POSITIONAL_OR_KEYWORD", "str"),
                 ("kw", "VAR_KEYWORD", "<class 'inspect._empty'>")],
                "Iterator[Tuple[Path, Dict[str, str]]]"),
            "glob_bucket": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("bucket", "POSITIONAL_OR_KEYWORD", "str"),
                 ("pattern", "POSITIONAL_OR_KEYWORD", "str"), ("kw", "VAR_KEYWORD", "<class 'inspect._empty'>")],
                "List[Path]"),
            "spec_dir": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("open_cycle_dir", "KEYWORD_ONLY", "Optional[str]")],
                "Optional[Tuple[Path, str]]"),
            "resolve_path": (
                [("root", "POSITIONAL_OR_KEYWORD", "Path"), ("rel", "POSITIONAL_OR_KEYWORD", "str")],
                "Dict[str, object]"),
        }
        for name, (params, ret) in expected.items():
            sig = inspect.signature(getattr(reader, name))
            actual = [(p.name, p.kind.name, str(p.annotation)) for p in sig.parameters.values()]
            self.assertEqual(actual, params, name)
            self.assertEqual(str(sig.return_annotation), ret, name)


if __name__ == "__main__":
    unittest.main()
