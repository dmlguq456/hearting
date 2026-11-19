#!/usr/bin/env python3
"""States the tools could not leave: a stale member list, an abandon that refused a folder's files,
a close refusal that named no cycle, and an entrance that did not follow `cycle-move`.

Each state is built on a fixture root first; the commands a person would run answer it, and the
refusals that must stay refusals (no record, a live member not listed, a normal close, a closed
campaign) are asserted after the same fix.
"""
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
import artifact_identity as I
import artifact_locator as LOC
import artifact_producer as P

spec = importlib.util.spec_from_file_location("tidy_producer_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)

HERE = Path(__file__).resolve().parent
# The frame/leg fixture and the request-folder helpers come from the suites that already build them
# through the real begin/finalize code; nothing about a member cycle is written by hand here.
spec = importlib.util.spec_from_file_location("tidy_framed_fixture", HERE / "framed_start.test.py")
S = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = S
spec.loader.exec_module(S)
spec = importlib.util.spec_from_file_location("tidy_request_fixture", HERE / "artifact_request.test.py")
Q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = Q
spec.loader.exec_module(Q)

OLD_NEXT_STEP = "reconcile-campaign-membership-with-producer-records"


class TidyBase(F.ProducerTestBase):
    def setUp(self):
        super().setUp()
        route, route_file, self.first = self.begin(campaign_key="tidy")
        self.write_output(self.first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.first["cycle_id"])
        self.campaign = self.first["campaign_id"]
        self.path = Path(self.first["cycle_dir"]).parent / "campaign.json"

    def begun(self, slug):
        route = F.compile_for("direct", self.root, slug=slug, gate_source=slug)
        binding = F.L.admit_runtime_route(self.root, route)
        return P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                       intensity="direct", campaign_id=self.campaign)

    def listed(self):
        return json.loads(self.path.read_text())["cycles"]

    def list_as(self, cycles):
        record = json.loads(self.path.read_text())
        P._write_campaign(self.root, dict(record, cycles=cycles), exclusive=False)

    def refusal(self):
        status = C.status(self.root, self.path)
        self.assertFalse(status["closable"], status)
        return status["close_refusal"]

    def close_campaign(self):
        return C.close(self.root, self.path, reason="tidy")


class StaleMemberList(TidyBase):
    """1: the list names a cycle whose record is detached."""

    def stale_abandoned(self):
        # What an older abandon left: the record is abandoned, the id stayed on the list.
        gone = self.begun("stale-abandoned")
        P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertNotIn(gone["cycle_id"], self.listed())
        self.list_as(self.listed() + [gone["cycle_id"]])
        return gone["cycle_id"]

    def test_stale_abandoned_id_is_refused_then_close_takes_it_off_the_list(self):
        cycle = self.stale_abandoned()
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        self.assertIn(cycle, refusal["detail"])
        self.assertNotIn(OLD_NEXT_STEP, refusal["detail"])
        self.assertIn("campaign-close", refusal["detail"])
        record = P.read_cycle_record(self.root, cycle)
        # `cycle-mark` is not what settles it; the ordinary close is.
        P.cycle_mark(self.root, cycle, discard=True)
        self.assertEqual(self.refusal()["reason"], "campaign-membership-drift")
        record = P.read_cycle_record(self.root, cycle)
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.listed(), [self.first["cycle_id"]])
        self.assertEqual(P.read_cycle_record(self.root, cycle), record)  # the record is not rewritten

    def test_abandon_that_keeps_its_folder_no_longer_blocks_the_close(self):
        # The abandon of a cycle whose only file is excluded keeps the folder and the list entry (as it always did).
        gone = self.begun("kept-folder")
        folder = Path(gone["cycle_dir"])
        (folder / "artifacts" / ".scratch").write_text("kept")
        out = P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(out["status"], "no-lineage")
        self.assertIn(gone["cycle_id"], self.listed())
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.refusal()["reason"], "campaign-membership-drift")
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.listed(), [self.first["cycle_id"]])
        self.assertEqual((folder / "artifacts" / ".scratch").read_text(), "kept")

    def test_deleted_and_moved_away_records_are_detached_too(self):
        deleted = self.begun("deleted-one")
        P.finalize(self.root, cycle_id=deleted["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        record = P.read_cycle_record(self.root, deleted["cycle_id"])
        P._write_cycle_record(self.root, dict(record, deleted_at="2026-11-01T00:00:00Z", deleted_by="delete"),
                              exclusive=False)
        moved = self.begun("moved-one")
        P.finalize(self.root, cycle_id=moved["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        record = P.read_cycle_record(self.root, moved["cycle_id"])
        P._write_cycle_record(self.root, dict(record, relocation={"artifact_root": "/elsewhere"}), exclusive=False)
        self.list_as(self.listed() + [deleted["cycle_id"], moved["cycle_id"]])
        self.assertEqual(self.refusal()["reason"], "campaign-membership-drift")
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.listed(), [self.first["cycle_id"]])

    def test_an_id_with_no_record_is_still_refused(self):
        ghost = "cyc_" + "f" * 32
        self.list_as(self.listed() + [ghost])
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        self.assertIn(ghost, refusal["detail"])
        self.assertNotIn(OLD_NEXT_STEP, refusal["detail"])
        before = self.path.read_bytes()
        with self.assertRaises(C.CampaignError) as caught:
            self.close_campaign()
        self.assertEqual(caught.exception.code, "campaign-membership-drift")
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_member_the_list_lacks_is_still_refused(self):
        second = self.begun("second-member")
        self.write_output(second)
        P.finalize(self.root, cycle_id=second["cycle_id"], allow_open_route=True)
        self.list_as([self.first["cycle_id"]])
        self.assertEqual(self.refusal()["reason"], "campaign-membership-drift")
        with self.assertRaises(C.CampaignError) as caught:
            self.close_campaign()
        self.assertEqual(caught.exception.code, "campaign-membership-drift")
        self.assertEqual(self.listed(), [self.first["cycle_id"]])

    def test_a_closed_campaign_list_is_not_rewritten(self):
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        events = sorted((self.path.parent / C.EVENTS_DIR).glob("*.json"))
        event_bytes = [path.read_bytes() for path in events]
        stale = "cyc_" + "e" * 32
        self.list_as(self.listed() + [stale])
        before = self.path.read_bytes()
        self.assertEqual(C.status(self.root, self.path)["status"], "satisfied")
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([path.read_bytes() for path in sorted((self.path.parent / C.EVENTS_DIR).glob("*.json"))],
                         event_bytes)


class AbandonWithFiles(TidyBase):
    """2: an abandon does not refuse files a person left outside `artifacts/`."""

    def stray(self, cycle, name="logs/run.log", body="a person's file\n"):
        target = Path(cycle["cycle_dir"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def test_abandon_leaves_a_subfolder_file_where_it_is_and_the_campaign_closes(self):
        gone = self.begun("abandon-logs")
        target = self.stray(gone)
        out = P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(out["status"], "no-lineage")
        self.assertEqual(P.read_cycle_record(self.root, gone["cycle_id"])["state"], "abandoned")
        self.assertEqual(target.read_text(), "a person's file\n")  # neither moved nor removed
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(target.read_text(), "a person's file\n")

    def test_abandon_with_output_seals_it_and_keeps_the_outside_file_out_of_the_manifest(self):
        gone = self.begun("abandon-output")
        self.write_output(gone)
        target = self.stray(gone)
        out = P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned",
                         abandon_reason="operator-decision", allow_open_route=True)
        self.assertEqual(out["status"], "sealed", out)
        manifest = json.loads((Path(gone["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(manifest["cycle"]["state"], "abandoned")
        self.assertFalse([row for row in manifest["artifact_revisions"] if "run.log" in row["locator"]["path"]])
        self.assertEqual(target.read_text(), "a person's file\n")
        again = P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned",
                           abandon_reason="operator-decision", allow_open_route=True)  # the refresh reads it again
        self.assertNotEqual(again.get("status"), "blocked")
        self.assertEqual(target.read_text(), "a person's file\n")
        self.assertEqual(self.close_campaign()["status"], "satisfied")

    def test_a_normal_close_still_refuses_a_file_outside_artifacts(self):
        route, route_file = self.route(slug="normal-close", campaign_key="tidy")
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(cycle)
        self.stray(cycle)
        self.close(route, route_file)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=cycle["cycle_id"])
        self.assertEqual(caught.exception.code, "output-invalid")
        self.assertIn("file-outside-artifacts:logs/run.log", caught.exception.detail)
        self.assertEqual(P.read_cycle_record(self.root, cycle["cycle_id"])["state"], "open")

    def test_abandon_still_refuses_what_is_not_a_plain_file(self):
        gone = self.begun("abandon-fifo")
        os.mkfifo(str(Path(gone["cycle_dir"]) / "pipe"))
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(caught.exception.code, "output-invalid")
        self.assertIn("non-regular-file:pipe", caught.exception.detail)


class CloseRefusalNamesTheCycle(TidyBase):
    """3: `campaign-cycle-bindings-incomplete` says which cycle, and `delete --cycle` settles it."""

    def sealed_without_folder(self, count=2):
        ids = []
        for index in range(count):
            member = self.begun("vanished-%d" % index)
            self.write_output(member)
            P.finalize(self.root, cycle_id=member["cycle_id"], allow_open_route=True)
            ids.append(member["cycle_id"])
        for cycle_id in ids:
            shutil.rmtree(P.cycle_dir(self.root, self.campaign, cycle_id, P.read_cycle_record(self.root, cycle_id)))
        return ids

    def test_status_names_the_cycles_without_a_folder(self):
        ids = self.sealed_without_folder()
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-cycle-bindings-incomplete")
        for cycle_id in ids:
            self.assertIn(cycle_id, refusal["detail"])
        self.assertIn("folder-missing=", refusal["detail"])
        self.assertIn("delete --artifact-root", refusal["detail"])
        self.assertIn("campaign-close", refusal["detail"])

    def test_close_records_a_vanished_folder_itself_when_every_folder_can_be_read(self):
        self.sealed_without_folder()
        self.assertEqual(self.refusal()["reason"], "campaign-cycle-bindings-incomplete")  # status only reads
        self.assertEqual(self.close_campaign()["status"], "satisfied")

    def test_delete_settles_each_cycle_when_the_close_cannot_read_every_folder(self):
        ids = self.sealed_without_folder()
        (self.path.parent / "folder-with-no-id").mkdir()  # a folder that names no cycle: nothing is taken for gone
        with self.assertRaises(C.CampaignError) as caught:
            self.close_campaign()
        self.assertEqual(caught.exception.code, "campaign-cycle-bindings-incomplete")
        for cycle_id in ids:
            self.assertIn(cycle_id, caught.exception.detail)
        for index, cycle_id in enumerate(ids):
            self.assertEqual(P.delete_cycle(self.root, cycle_id)["status"], "deleted")
            if index + 1 < len(ids):
                self.assertNotIn(cycle_id, self.refusal()["detail"])
                self.assertIn(ids[-1], self.refusal()["detail"])
        self.assertTrue(C.status(self.root, self.path)["closable"])
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.listed(), [self.first["cycle_id"]])

    def test_an_unlisted_folder_is_named_and_stays_a_refusal(self):
        stray = I.IdAllocator().allocate("cycle")
        folder = self.path.parent / "2026-11-19_unlisted"
        folder.mkdir()
        (folder / LOC.CYCLE_BINDING).write_bytes(LOC.cycle_binding_bytes(
            self.campaign, stray, started_on="2026-11-19T00:00:00Z"))
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-cycle-bindings-incomplete")
        self.assertIn("folder-not-listed=" + stray, refusal["detail"])
        self.assertNotIn("folder-missing", refusal["detail"])
        with self.assertRaises(C.CampaignError):
            self.close_campaign()

    def test_the_other_empty_details_say_what_is_wrong(self):
        record = json.loads(self.path.read_text())
        for cycles, word in (([], "empty"), ([self.first["cycle_id"]] * 2, "duplicate-id")):
            P._write_campaign(self.root, dict(record, cycles=cycles), exclusive=False)
            refusal = self.refusal()
            self.assertEqual(refusal["reason"], "campaign-membership-invalid")
            self.assertIn(word, refusal["detail"])


class EntranceFollowsCycleWrites(TidyBase):
    """4: a write that changes what the entrance shows asks for one best-effort refresh."""

    def other_campaign(self):
        self.other_count = getattr(self, "other_count", 0) + 1
        key = "destination-%d" % self.other_count
        route, route_file = self.route(slug=key, campaign_key=key)
        other = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(other)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=other["cycle_id"])
        return other

    def test_cycle_move_requests_a_refresh_and_a_failing_entrance_does_not_stop_it(self):
        other = self.other_campaign()
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            moved = P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(moved["status"], "moved")
        refresh.assert_called_once()
        with mock.patch("artifact_entrance.request_refresh", side_effect=RuntimeError("entrance is down")):
            back = P.cycle_move(self.root, self.first["cycle_id"], campaign=self.campaign)
        self.assertEqual(back["status"], "moved")
        self.assertEqual(P.read_cycle_record(self.root, self.first["cycle_id"])["campaign_id"], self.campaign)

    def test_an_unchanged_move_does_not_ask(self):
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            same = P.cycle_move(self.root, self.first["cycle_id"], campaign=self.campaign)
        self.assertEqual(same["status"], "unchanged")
        refresh.assert_not_called()

    def test_delete_asks_for_a_refresh(self):
        other = self.other_campaign()
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            self.assertEqual(P.delete_cycle(self.root, other["cycle_id"])["status"], "deleted")
        refresh.assert_called_once()
        again = self.other_campaign()
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            self.assertEqual(P.delete_campaign(self.root, again["campaign_id"])["status"], "deleted")
        refresh.assert_called_once()


class MemberCycleClose(S.SourceRepoStartBase):
    """A frame folder holds its legs' cycles at `<frame>/_internal/executions/<id>`, all closed."""

    selected = Q.RequestFolderTest.selected
    cycle = Q.RequestFolderTest.cycle
    next_execution = Q.RequestFolderTest.next_execution
    begin_route = Q.RequestFolderTest.begin_route

    def two_closed_legs(self):
        leg, path, _ = self.selected([S.CODE_STAGED, S.CODE_STAGED])
        first = self.cycle(leg)
        first_dir = P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"])
        self.write_output({"cycle_dir": str(first_dir)}, "plans/final_report.md", b"first\n")
        self.close(leg, path)
        P.finalize(self.root, cycle_id=first["cycle_id"])
        route, route_path, second = self.next_execution(1, first)
        self.write_output(second, "plans/final_report.md", b"second\n")
        self.close(route, route_path)
        P.finalize(self.root, cycle_id=second["cycle_id"], state="completed")
        frame = self.cycle(self.route)
        self.campaign_path = P.campaign_dir(self.root, frame["campaign_id"]) / "campaign.json"
        return frame, first, second

    def refusal(self):
        status = C.status(self.root, self.campaign_path)
        self.assertFalse(status["closable"], status)
        return status["close_refusal"]

    def test_closed_legs_inside_a_frame_let_the_campaign_close(self):
        frame, first, second = self.two_closed_legs()
        # Both legs are sealed, nothing is open, and each lives at the place artifact_request records.
        for leg in (first, second):
            record = P.read_cycle_record(self.root, leg["cycle_id"])
            self.assertEqual(Path(P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"])).parts[-3:-1],
                             ("_internal", "executions"))
            self.assertNotEqual(record["locator"], leg["cycle_id"])
        status = C.status(self.root, self.campaign_path)
        self.assertTrue(status["closable"], status)
        closed = C.close(self.root, self.campaign_path, reason="tidy")
        self.assertEqual(closed["status"], "satisfied")
        event = json.loads((self.campaign_path.parent / "campaign.events" / "000001.json").read_text())
        rows = {row["cycle_id"]: row for row in event["payload"]["snapshot"]["cycles"]}
        # One row per cycle, the frame and both legs, each bound to its own manifest.
        self.assertEqual(set(rows), {frame["cycle_id"], first["cycle_id"], second["cycle_id"]})
        self.assertTrue(all(row["manifest_digest"] for row in rows.values()))

    def test_a_closed_campaign_with_member_cycles_reads_back_satisfied(self):
        frame, first, second = self.two_closed_legs()
        C.close(self.root, self.campaign_path, reason="tidy")
        raw = (self.campaign_path.parent / "campaign.events" / "000001.json").read_bytes()
        self.assertEqual(C.status(self.root, self.campaign_path)["state"], "satisfied")
        self.assertEqual(C.close(self.root, self.campaign_path, reason="again")["status"], "satisfied")
        folded = C.campaign_state(self.root, self.campaign_path)
        self.assertEqual((folded.state, folded.last_sequence), ("satisfied", 1))
        self.assertEqual((self.campaign_path.parent / "campaign.events" / "000001.json").read_bytes(), raw)
        # A renamed root binds each row to a surviving manifest; the legs' manifests are found without
        # the preserved snapshots too, because the campaign folder walk reaches them.
        for cycle in (frame, first, second):
            shutil.rmtree(self.root / C.lifecycle.MANIFEST_SNAPSHOT_REL / cycle["cycle_id"], ignore_errors=True)
        target = Path(self.source_tmp.name) / "renamed-root"
        shutil.move(str(self.root), str(target))
        folded = C.campaign_state(target, target / self.campaign_path.relative_to(self.root))
        self.assertEqual((folded.state, folded.last_sequence), ("satisfied", 1))

    def test_reopen_then_close_again_with_member_cycles(self):
        self.two_closed_legs()
        C.close(self.root, self.campaign_path, reason="tidy")
        self.assertEqual(C.reopen(self.root, self.campaign_path, reason="more work")["status"], "active")
        self.assertTrue(C.status(self.root, self.campaign_path)["closable"])
        self.assertEqual(C.close(self.root, self.campaign_path, reason="tidy again")["status"], "satisfied")
        self.assertEqual(C.status(self.root, self.campaign_path)["state"], "satisfied")

    def test_a_leg_folder_pulled_out_of_its_frame_is_still_refused(self):
        frame, first, second = self.two_closed_legs()
        leg_dir = P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"])
        out = leg_dir.parents[3] / leg_dir.name
        leg_dir.rename(out)
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-cycle-locator-invalid", refusal)
        self.assertEqual(refusal["detail"], first["cycle_id"])
        # Only that leg is named; the other leg, still in its frame, is read as in place. (The close's
        # own reconcile then treats a hand-moved folder as any cycle's: it adopts the new place.)
        self.assertNotIn(second["cycle_id"], str(refusal))

    def test_a_leg_folder_under_some_other_cycle_is_still_refused(self):
        frame, first, second = self.two_closed_legs()
        leg_dir = P.cycle_dir(self.root, second["campaign_id"], second["cycle_id"])
        other = P.cycle_dir(self.root, first["campaign_id"], first["cycle_id"]) / "_internal/executions" / second["cycle_id"]
        other.parent.mkdir(parents=True, exist_ok=True)
        leg_dir.rename(other)
        before = (other / "manifest.json").read_bytes()
        status = C.status(self.root, self.campaign_path)
        self.assertFalse(status["closable"], status)
        # What the close's own reconcile then does with a folder at no known place is the existing
        # "vanished" handling, the same as for any cycle; the folder and its manifest stay as they are.
        try:
            C.close(self.root, self.campaign_path, reason="tidy")
        except C.CampaignError:
            pass
        self.assertEqual((other / "manifest.json").read_bytes(), before)

    def test_a_renamed_frame_folder_is_followed_for_its_legs(self):
        frame, first, second = self.two_closed_legs()
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        frame_dir.rename(frame_dir.with_name("2026-11-19_renamed-request"))
        P.reconcile_root(self.root)
        self.assertTrue(C.status(self.root, self.campaign_path)["closable"])
        self.assertEqual(C.close(self.root, self.campaign_path, reason="tidy")["status"], "satisfied")

    def test_a_leg_moved_out_by_cycle_move_is_read_as_before(self):
        # `cycle-move <leg>` makes the leg a folder of its own in the target campaign, keeping its
        # request binding; that place was accepted before and still is. Pulling it out by hand is not.
        frame, first, second = self.two_closed_legs()
        other = Q.RequestFolderTest.target_campaign(self)
        P.cycle_move(self.root, first["cycle_id"], campaign=other["campaign_id"], reason="one leg elsewhere")
        target_path = P.campaign_dir(self.root, other["campaign_id"]) / "campaign.json"
        self.assertTrue(P.read_cycle_record(self.root, first["cycle_id"]).get("request_binding"))
        status = C.status(self.root, target_path)
        self.assertNotEqual((status.get("close_refusal") or {}).get("reason"), "campaign-cycle-locator-invalid", status)

    def test_a_plain_cycle_keeps_the_old_check(self):
        # No request binding: only `<campaign>/<locator>` (or `cycles/<id>`) is the place, as before.
        campaign = Path("/c")
        record = {"locator": "2026-11-19_elsewhere", "cycle_id": "cyc_" + "a" * 32}
        self.assertTrue(C._in_its_place(self.root, campaign, campaign / "2026-11-19_elsewhere", record, record["cycle_id"]))
        self.assertFalse(C._in_its_place(self.root, campaign, campaign / "frame" / "_internal" / "executions" / record["cycle_id"],
                                         record, record["cycle_id"]))
        bare = {"cycle_id": "cyc_" + "b" * 32}
        self.assertTrue(C._in_its_place(self.root, campaign, campaign / "cycles" / bare["cycle_id"], bare, bare["cycle_id"]))


if __name__ == "__main__":
    unittest.main()
