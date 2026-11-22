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
        out["relayout-plan"] = RL.build_plan(self.root)
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
        RS._prune_empty_tree(folder)
        self.assertFalse(folder.exists())

    def test_a_rollback_never_removes_a_file_in_records_that_is_not_the_generators(self):
        import artifact_resplit as RS
        folder = self.root / "campaigns" / "made-by-the-run"
        (folder / "records").mkdir(parents=True)
        (folder / "records" / "pr-1.md").write_text("a person wrote this\n", encoding="utf-8")
        (folder / "records" / "pr-2.md").write_text(PAGE, encoding="utf-8")
        with self.assertRaises(RS.ResplitError):
            RS._prune_empty_tree(folder)
        self.assertEqual((folder / "records" / "pr-1.md").read_text(), "a person wrote this\n")

if __name__ == "__main__":
    unittest.main()
