#!/usr/bin/env python3
"""A folder that a cycle record names as its own place is not a folder nobody names.

The field shape: a closed campaign owns a cycle whose record is `no-lineage` (it ended with no output) and whose
`locator` is the name of a folder inside that campaign's folder.  The folder holds nothing but an empty
`artifacts/plans/<name>/` (no `.cycle.json`, no `manifest.json`).  Another campaign's close names its missing
member and what holds the vanished folder back."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_producer as P

_spec = importlib.util.spec_from_file_location(
    "campaign_named_folder_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


class NamedFolderTest(F.ProducerTestBase):
    def _member(self, slug, *, campaign=None, key=None, output=True, finalize=True, gate=None):
        route = F.compile_for("direct", self.root, slug=slug, gate_source=gate or slug)
        binding = F.L.admit_runtime_route(self.root, route)
        child = P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                        intensity="direct", campaign_key=key, campaign_id=campaign)
        if output:
            self.write_output(child, data=(slug + "\n").encode())
        self.close(route, Path(binding.route_file))
        if finalize:
            P.finalize(self.root, cycle_id=child["cycle_id"])
        return child

    def setUp(self):
        super().setUp()
        self.activate()
        # B: closed; one sealed cycle and one that ended with no output (`no-lineage`)
        first = self._member("b-sealed", key="campaign-b")
        self.b_id = first["campaign_id"]
        self.b_path = Path(first["cycle_dir"]).parent / "campaign.json"
        empty = self._member("b-empty", campaign=self.b_id, output=False)
        self.empty_id = empty["cycle_id"]
        C.close(self.root, self.b_path, reason="b is done")
        self.b_event = (self.b_path.parent / C.EVENTS_DIR / "000001.json").read_bytes()
        # C: active; two sealed cycles, the second of which is carried out of campaigns/ (a vanished folder)
        c_first = self._member("c-one", key="campaign-c")
        self.c_id = c_first["campaign_id"]
        self.c_path = Path(c_first["cycle_dir"]).parent / "campaign.json"
        self.c_second = self._member("c-two", campaign=self.c_id)
        # the field shape: the empty cycle's own folder holds only an empty artifacts/plans/<name>/
        self.record = P.read_cycle_record(self.root, self.empty_id)
        self.locator = self.record["locator"]
        self.named = self.b_path.parent / self.locator
        (self.named / "artifacts" / "plans" / self.locator).mkdir(parents=True)
        self.carried = Path(self._tmp.name) / "carried"
        shutil.move(self.c_second["cycle_dir"], self.carried)

    def _record(self):
        record = P.read_cycle_record(self.root, self.empty_id)
        return {key: record.get(key) for key in ("state", "campaign_id", "locator", "deleted_at", "deleted_by")}

    def _refusal(self):
        with self.assertRaises(C.CampaignError) as caught:
            C.close(self.root, self.c_path, reason="c is done")
        self.assertEqual(caught.exception.code, "campaign-cycle-bindings-incomplete")
        return str(caught.exception.detail)

    def test_the_fixture_has_the_field_shape(self):
        self.assertEqual(self.record["state"], "no-lineage")
        self.assertEqual(self.record["campaign_id"], self.b_id)
        self.assertEqual(C.campaign_state(self.root, self.b_path).state, "satisfied")
        self.assertEqual(sorted(p.relative_to(self.named).as_posix() for p in self.named.rglob("*")),
                         ["artifacts", "artifacts/plans", "artifacts/plans/" + self.locator])
        self.assertFalse((self.named / ".cycle.json").exists() or (self.named / "manifest.json").exists())

    def test_a_folder_a_record_names_as_its_place_does_not_hold_back_a_vanished_one(self):
        blockers = P._scan_layout(self.root).blockers
        self.assertEqual(blockers, [])
        # the close records the vanished cycle as gone by itself and the shell is neither moved nor touched
        self.assertEqual(C.close(self.root, self.c_path, reason="c is done")["status"], "satisfied")
        self.assertTrue(self.named.is_dir())
        self.assertEqual(self._record()["state"], "no-lineage")

    def test_moving_the_shell_out_of_campaigns_changes_no_record(self):
        before = self._record()
        away = Path(self._tmp.name) / "away"
        shutil.move(str(self.named), str(away))
        self.assertEqual(P.reconcile_root(self.root)["cycle_gone"], [self.c_second["cycle_id"]])
        self.assertEqual(self._record(), before)
        self.assertEqual(C.campaign_state(self.root, self.b_path).state, "satisfied")
        self.assertEqual((self.b_path.parent / C.EVENTS_DIR / "000001.json").read_bytes(), self.b_event)
        self.assertEqual(json.loads(self.b_path.read_text())["cycles"], [
            cid for cid in json.loads(self.b_path.read_text())["cycles"] if cid != self.empty_id])
        self.assertEqual(sorted(row["state"] for row in P.list_campaign_summaries(self.root, active_only=False)),
                         ["active", "satisfied"])
        shutil.move(str(away), str(self.named))
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")
        self.assertEqual(self._record(), before)

    def test_the_same_name_in_another_campaign_still_holds_back_and_says_whose_it_is(self):
        stray = self.c_path.parent / self.locator
        (stray / "artifacts" / "plans").mkdir(parents=True)
        shutil.rmtree(self.named)  # B's own place is empty of it: the only folder with that name is C's
        detail = self._refusal()
        self.assertIn("held-back-by=campaigns/%s/%s (not-a-cycle)" % (self.c_path.parent.name, self.locator), detail)
        self.assertIn("named-by=campaigns/%s/%s: cycle %s state=no-lineage campaign=%s" % (
            self.c_path.parent.name, self.locator, self.empty_id, self.b_id), detail)
        self.assertIn("leaves that record exactly as it is", detail)
        self.assertEqual(self._record()["state"], "no-lineage")

    def test_a_folder_that_holds_a_file_still_holds_back_and_says_whose_it_is(self):
        (self.named / "artifacts" / "plans" / self.locator / "note.txt").write_text("kept\n")
        detail = self._refusal()
        self.assertIn("held-back-by=campaigns/%s/%s (not-a-cycle)" % (self.b_path.parent.name, self.locator), detail)
        self.assertIn("named-by=", detail)
        self.assertIn("cycle %s state=no-lineage" % self.empty_id, detail)

    def test_a_link_in_the_folder_is_something_and_the_folder_stays_a_blocker(self):
        os.symlink("/nonexistent", self.named / "artifacts" / "link")
        self.assertEqual([reason for _where, reason in P._scan_layout(self.root).blockers],
                         ["not-a-cycle"])

    def test_a_sealed_cycle_that_lost_its_identity_files_is_never_passed_over(self):
        # the safety the exclusion must keep: the folder of a cycle with a published row, stripped of the
        # files that name it, is still unknown, and the close does not take that cycle for gone
        first = P.read_cycle_record(self.root, json.loads(self.c_path.read_text())["cycles"][0])
        folder = self.c_path.parent / first["locator"]
        (folder / ".cycle.json").unlink()
        (folder / "manifest.json").unlink()
        shutil.rmtree(folder / "artifacts")
        (folder / "artifacts" / "plans" / first["locator"]).mkdir(parents=True)
        self.assertEqual([(where.split("/")[-1], reason) for where, reason in P._scan_layout(self.root).blockers],
                         [(first["locator"], "not-a-cycle")])
        detail = self._refusal()
        self.assertIn("named-by=", detail)
        self.assertIn("state=sealed", detail)
        self.assertIn("would make campaign-close record that cycle as deleted for good", detail)
        self.assertIsNone(P.read_cycle_record(self.root, first["cycle_id"]).get("deleted_at"))
        self.assertEqual(C.campaign_state(self.root, self.c_path).state, "active")

    def _strip_files(self, folder):
        for path in sorted(Path(folder).rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_symlink() or path.is_file():
                path.unlink()

    def _assert_an_emptied_folder_of_a_live_cycle_holds_back(self, again):
        """`again` is a cycle that is still there (sealed) and shares its locator with a detached record of its
        campaign; its folder then loses every file.  That folder is still unknown, the unrelated hand change is
        still reconciled, and the refusal does not say that nothing holds the close back."""
        self._strip_files(again["cycle_dir"])
        self.assertEqual(sorted(p.relative_to(again["cycle_dir"]).as_posix() for p in Path(again["cycle_dir"]).rglob("*")),
                         ["artifacts", "artifacts/plans", "artifacts/plans/cycle"])
        locator = P.read_cycle_record(self.root, again["cycle_id"])["locator"]
        self.assertEqual([(where.split("/")[-1], reason) for where, reason in P._scan_layout(self.root).blockers],
                         [(locator, "not-a-cycle")])
        # an unrelated hand-made rename in the same root is carried into the records, not dropped with the scan
        b_sealed = json.loads(self.b_path.read_text())["cycles"][0]
        shutil.move(str(self.b_path.parent / P.read_cycle_record(self.root, b_sealed)["locator"]),
                    str(self.b_path.parent / "renamed-by-hand"))
        result = P.reconcile_root(self.root)
        self.assertNotEqual(result.get("status"), "skipped", result)
        self.assertEqual(P.read_cycle_record(self.root, b_sealed)["locator"], "renamed-by-hand")
        self.assertIsNone(P.read_cycle_record(self.root, again["cycle_id"]).get("deleted_at"))
        detail = self._refusal()
        self.assertIn("held-back-by=campaigns/%s/%s (not-a-cycle)" % (self.c_path.parent.name, locator), detail)
        self.assertNotIn("nothing under campaigns/ holds it back", detail)
        self.assertIn("state=sealed", detail)
        self.assertIn("would make campaign-close record that cycle as deleted for good", detail)
        self.assertIsNone(P.read_cycle_record(self.root, again["cycle_id"]).get("deleted_at"))

    def test_a_restart_under_the_name_of_a_cycle_that_ended_with_no_output_is_never_passed_over(self):
        # the first cycle of the name ended with no output (its own folder is gone, its record says no-lineage);
        # the restart of the same name the same day gets the same locator and is sealed
        self._member("retry", campaign=self.c_id, output=False)
        again = self._member("retry", campaign=self.c_id, gate="retry-second")
        records = [r for r in P.list_cycle_records(self.root) if r.get("campaign_id") == self.c_id
                   and r["locator"].endswith("_retry")]
        self.assertEqual(sorted(r["state"] for r in records), ["no-lineage", "sealed"])
        self.assertEqual(len({r["locator"] for r in records}), 1)
        self._assert_an_emptied_folder_of_a_live_cycle_holds_back(again)

    def test_a_restart_under_the_name_of_a_deleted_cycle_is_never_passed_over(self):
        old = self._member("dup", campaign=self.c_id)
        P.delete_cycle(self.root, old["cycle_id"])
        again = self._member("dup", campaign=self.c_id, gate="dup-second")
        records = [r for r in P.list_cycle_records(self.root) if r.get("campaign_id") == self.c_id
                   and r["locator"].endswith("_dup")]
        self.assertEqual(sorted(bool(r.get("deleted_at")) for r in records), [False, True])
        self.assertEqual(len({r["locator"] for r in records}), 1)
        self._assert_an_emptied_folder_of_a_live_cycle_holds_back(again)

    def test_the_scan_reads_only_the_records_of_cycles_whose_folder_it_did_not_find(self):
        # a root with many cycles and a folder nobody names: the records of the cycles the scan found by their
        # folders are not read again for it, and the refusal's named-by lines come from what the scan read
        import artifact_producer_cycle_ops as OPS
        read = []
        real = OPS._read_json
        with mock.patch.object(OPS, "_read_json", side_effect=lambda path: (read.append(Path(path).name), real(path))[1]):
            scan = P._scan_layout(self.root)
        self.assertEqual(scan.blockers, [])
        # of the cycles of this root only the two without a folder (the empty one, the carried one) were read
        self.assertEqual(sorted(name for name in read if name.startswith("cyc_")),
                         sorted([self.empty_id + ".json", self.c_second["cycle_id"] + ".json"]))
        (self.named / "artifacts" / "plans" / self.locator / "note.txt").write_text("kept\n")
        scan = P._scan_layout(self.root)
        self.assertEqual([r["cycle_id"] for r in scan.named[self.locator]], [self.empty_id])
        with mock.patch.object(OPS, "_read_json", side_effect=AssertionError("read the records again")):
            phrases = C._named_by_records(scan, scan.blockers)
        self.assertEqual(len(phrases), 1)
        self.assertIn(self.empty_id, phrases[0])

    def test_the_words_for_a_deleted_record_do_not_say_it_ended_with_no_output(self):
        # a folder with a file in it, in the closed campaign, has the name that a deleted cycle of the other
        # campaign had as its locator (it is there before the delete, which would reconcile the carried folder)
        name = self.locator.split("_")[0] + "_dup"
        stray = self.b_path.parent / name
        (stray / "artifacts" / "plans").mkdir(parents=True)
        (stray / "artifacts" / "plans" / "note.txt").write_text("kept\n")
        old = self._member("dup", campaign=self.c_id)
        self.assertEqual(P.read_cycle_record(self.root, old["cycle_id"])["locator"], name)
        P.delete_cycle(self.root, old["cycle_id"])
        detail = self._refusal()
        self.assertIn("named-by=campaigns/%s/%s: cycle %s state=deleted" % (self.b_path.parent.name, name, old["cycle_id"]), detail)
        self.assertIn("this cycle is already recorded as deleted; moving the folder out of campaigns/ changes no record", detail)
        self.assertNotIn("the cycle ended with no output", detail)

    def test_the_general_not_a_cycle_sentence_defers_to_a_named_by_line(self):
        (self.named / "artifacts" / "plans" / self.locator / "note.txt").write_text("kept\n")
        detail = self._refusal()
        self.assertIn("if a named-by line above names this folder, that line says which record it is and what moving it does; "
                      "with no such line, if it is a cycle that lost its .cycle.json", detail)


if __name__ == "__main__":
    unittest.main()
