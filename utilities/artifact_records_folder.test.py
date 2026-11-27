#!/usr/bin/env python3
"""`campaigns/<campaign>/records/` (the campaign record pages) is not a cycle folder.

The generator writes `records/<key>.md` beside `campaign.md`. Every place that walks a campaign
folder must treat it as it treats `journals/` and `campaign.md`: not a cycle, not residue, not a
block, not something to move or index. The test builds one root through the real begin/finalize
and compares what each place answers before and after the folder is there (and with `journals/`
in its place, the existing non-cycle name it must behave like).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
import importlib.util
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import artifact_campaign as C  # noqa: E402
import artifact_locator as LOC  # noqa: E402
import artifact_producer as P  # noqa: E402
import artifact_producer_cycle_ops as OPS  # noqa: E402
import artifact_relayout as RL  # noqa: E402
import artifact_residue as RES  # noqa: E402
import campaign_document as CD  # noqa: E402

spec = importlib.util.spec_from_file_location("records_folder_producer_fixture", HERE / "artifact_producer.test.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

RECORD_MARKER = "<!-- hearting:campaign-record v1 -->"
PAGE = f"{RECORD_MARKER}\n# 일\n\n#1 · 기록 1 · 파일 0개\n\n- 구현 · 11-21 · 일 · 파일 0개\n"


def tree_digest(folder):
    rows = {}
    for current, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(current, name)
            rows[os.path.relpath(path, folder)] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return rows


class RecordsFolderTest(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.first = self.begin(campaign_key="records")
        self.write_output(self.first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.first["cycle_id"])
        self.wait_for_entrance()   # the entrance worker may already have written the campaign's own pages
        self.campaign_dir = Path(self.first["cycle_dir"]).parent
        self.campaign_json = self.campaign_dir / "campaign.json"

    def make_records(self, name="records"):
        folder = self.campaign_dir / name
        folder.mkdir(exist_ok=True)
        (self.campaign_dir / "campaign.md").write_text(f"{CD.MARKER}\n# t\n", encoding="utf-8")
        if name == "records":
            (folder / "pr-1.md").write_text(PAGE, encoding="utf-8")
            (folder / "k-2.md").write_text(PAGE, encoding="utf-8")
        else:
            (folder / "2026-11-21.md").write_text("journal\n", encoding="utf-8")
        return folder

    def observe(self):
        """What each place that walks a campaign folder answers, as text (the root's own path is not in it)."""
        out = {}
        out["locator-dirs"] = sorted(p.name for p, _how in LOC.iter_cycle_dirs(self.campaign_dir))
        rows = LOC.scan_campaign(self.root, self.campaign_dir)
        out["locator-scan"] = sorted(rows) if rows else rows
        layout = OPS._scan_layout(self.root)
        out["layout"] = {"complete": layout.complete, "blockers": sorted(map(str, layout.blockers)),
                         "cycles": sorted(layout.cycles), "duplicates": sorted(layout.duplicates)}
        status = C.status(self.root, self.campaign_json)
        out["campaign-status"] = {"closable": status.get("closable"), "refusal": status.get("close_refusal"),
                                  "cycles": status.get("cycles")}
        plan = RL.build_plan(self.root)
        del plan["generated_at"]  # Compare the layout across observations at different times.
        out["relayout-plan"] = plan
        out["relayout-status"] = RL.status(self.root)
        out["residue-plan"] = RES.build_plan(self.root)
        out["residue-status"] = RES.status(self.root)
        text = json.dumps(out, sort_keys=True, default=str, ensure_ascii=False)
        return text.replace(str(self.root), "<root>")

    def test_the_constants_agree(self):
        self.assertEqual(CD.RECORDS_DIR, LOC.CAMPAIGN_RECORDS_DIR)

    def test_records_is_left_alone_like_journals_by_every_place_that_walks_a_campaign(self):
        baseline = self.observe()
        journals = self.make_records("journals")
        with_journals = self.observe()
        self.assertEqual(with_journals, baseline)
        shutil.rmtree(journals)
        records = self.make_records()
        before = tree_digest(self.campaign_dir)
        self.assertEqual(self.observe(), baseline)
        self.assertEqual(tree_digest(self.campaign_dir), before, "no place moved, wrote or removed a page")
        self.assertNotIn("records", json.dumps(self.observe()))
        self.assertTrue(records.is_dir())

    def test_the_locator_and_the_indexes_do_not_count_records_as_a_cycle(self):
        self.make_records()
        self.assertNotIn("records", [p.name for p, _how in LOC.iter_cycle_dirs(self.campaign_dir)])
        before = tree_digest(self.root)
        LOC.rebuild_indexes(self.root)
        after = tree_digest(self.root)
        self.assertEqual({k: v for k, v in after.items() if k.startswith("campaigns/") and "/records/" in k},
                         {k: v for k, v in before.items() if k.startswith("campaigns/") and "/records/" in k})

    def test_a_rollback_prunes_a_created_campaign_folder_that_holds_only_generated_pages(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "made-by-the-run"
        (folder / "records").mkdir(parents=True)
        (folder / "campaign.md").write_text(f"{CD.MARKER}\n# t\n", encoding="utf-8")
        (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
        RS._prune_empty_tree(folder, campaign_dir=folder)
        self.assertFalse(folder.exists())

    def test_a_rollback_never_removes_a_file_in_records_that_is_not_the_generators(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "made-by-the-run"
        (folder / "records").mkdir(parents=True)
        (folder / "records" / "pr-1.md").write_text("a person wrote this\n", encoding="utf-8")
        (folder / "records" / "pr-2.md").write_text(PAGE, encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(folder, campaign_dir=folder)
        self.assertEqual((folder / "records" / "pr-1.md").read_text(), "a person wrote this\n")

    def test_a_records_folder_inside_a_cycle_folder_is_not_the_campaigns_and_is_not_removed(self):
        import artifact_resplit as RS
        cycle = self.root / "campaigns" / "made-by-the-run" / "2026-11-20_cycle"
        (cycle / "artifacts" / "records").mkdir(parents=True)
        (cycle / "artifacts" / "records" / "note.md").write_text(PAGE, encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(cycle)
        self.assertEqual((cycle / "artifacts" / "records" / "note.md").read_text(), PAGE)
        # a marked copy of a campaign document beside it does not make that folder a campaign's
        (cycle / "artifacts" / "campaign.md").write_text(f"{CD.MARKER}\n# t\n", encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(cycle)
        self.assertEqual((cycle / "artifacts" / "records" / "note.md").read_text(), PAGE)
        with self.assertRaises(RS.ResplitError):   # nor does the caller naming the campaign folder above the cycle
            RS._prune_empty_tree(cycle, campaign_dir=cycle.parent)
        self.assertEqual((cycle / "artifacts" / "records" / "note.md").read_text(), PAGE)

    def test_the_records_pages_are_not_removed_before_a_blocker_beside_them_is_known(self):
        import artifact_resplit as RS
        for sibling in ("0-first-cycle", "z-last-cycle"):   # whichever the walk reaches first
            folder = self.root / "campaigns" / f"made-by-the-run-{sibling}"
            (folder / "records").mkdir(parents=True)
            (folder / "campaign.md").write_text(f"{CD.MARKER}\n# t\n", encoding="utf-8")
            (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
            (folder / sibling).mkdir()
            (folder / sibling / "kept.txt").write_text("a person's file\n", encoding="utf-8")
            with self.assertRaises(RS.ResplitError):
                RS._prune_empty_tree(folder, campaign_dir=folder)
            self.assertEqual((folder / "records" / "pr-1.md").read_text(), PAGE)
            self.assertEqual((folder / sibling / "kept.txt").read_text(), "a person's file\n")

    def test_the_records_pages_stay_while_the_campaign_record_is_still_there(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "made-by-the-run"
        (folder / "records").mkdir(parents=True)
        (folder / "campaign.json").write_text("{}", encoding="utf-8")
        (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(folder, campaign_dir=folder)
        self.assertEqual((folder / "records" / "pr-1.md").read_text(), PAGE)

    def test_a_folder_no_caller_names_keeps_its_records_pages_whatever_files_are_beside_them(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "only-records"
        (folder / "records").mkdir(parents=True)
        (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(folder)
        self.assertEqual((folder / "records" / "pr-1.md").read_text(), PAGE)
        (folder / "campaign.md").write_text(f"{CD.MARKER}\n# t\n", encoding="utf-8")   # a document file is no name
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(folder)
        self.assertEqual((folder / "records" / "pr-1.md").read_text(), PAGE)

    def test_a_rollback_of_a_campaign_whose_pages_were_written_before_its_document_goes_to_the_end(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "made-by-the-run"   # a pass wrote the pages and stopped before `campaign.md`
        (folder / "records").mkdir(parents=True)
        (folder / "campaign.json").write_text("{}", encoding="utf-8")
        (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
        run_dir = self.root.parent / "resplit-run"
        run_dir.mkdir()
        journal = {"pre_image": {"campaign_records": [
            {"path": "campaigns/made-by-the-run/campaign.json", "created_by_this_run": True}]}}
        RS._r2_rollback(self.root, journal, run_dir)
        self.assertFalse(folder.exists())
        self.assertEqual(json.loads((run_dir / "journal-r2.json").read_text())["phase"], "rolled-back")

    def test_a_residue_rollback_prunes_the_campaign_folder_it_made_with_its_pages(self):
        folder = self.root / "campaigns" / "made-by-the-residue"
        (folder / "records").mkdir(parents=True)
        (folder / "campaign.json").write_text("{}", encoding="utf-8")
        (folder / "records" / "pr-1.md").write_text(PAGE, encoding="utf-8")
        run_dir = self.root.parent / "residue-run"
        run_dir.mkdir()
        journal = {"pre_image": {"created_campaigns": ["camp_" + "9" * 32]}}
        with mock.patch.object(RES.artifact_locator, "find_path_by_id", return_value=folder):
            RES._rollback(self.root, run_dir, journal)
        self.assertFalse(folder.exists())
        self.assertEqual(journal["phase"], "rolled-back")

if __name__ == "__main__":
    unittest.main()
