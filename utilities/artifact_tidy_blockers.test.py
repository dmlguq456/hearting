#!/usr/bin/env python3
"""States the tools could not leave: a stale member list, an abandon that refused a folder's files,
a close refusal that named no cycle, and an entrance that did not follow `cycle-move`.

Each state is built on a fixture root first; the commands a person would run answer it, and the
refusals that must stay refusals (no record, a live member not listed, a normal close, a closed
campaign, an abandon the runtime makes by itself) are asserted after the same fix.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import ast
import contextlib
import io
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_identity as I
import artifact_locator as LOC
import artifact_producer as P
import route_autoclose

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

    def begun(self, slug, campaign=None):
        route = F.compile_for("direct", self.root, slug=slug, gate_source=slug)
        binding = F.L.admit_runtime_route(self.root, route)
        return P.begin(self.root, route_file=Path(binding.route_file), capability="autopilot-code",
                       intensity="direct", campaign_id=campaign or self.campaign)

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

    @staticmethod
    def drift(refusal):
        """The drift refusal's detail (status shows it as text of the dict)."""
        return ast.literal_eval(refusal["detail"])

    def begun_open(self, slug, campaign=None, key="tidy"):
        """A cycle begun in a campaign whose route is still open, and what closes that route."""
        route, route_file = self.route(slug=slug, campaign_key=key)
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        campaign_id=campaign or self.campaign)
        return cycle, lambda: self.close(route, route_file)


class StaleMemberList(TidyBase):
    """1: the list names a cycle whose record is detached; the close reads the list without it and
    never writes it back."""

    PROJECTED = {"state", "satisfied_on", "satisfaction_event_id"}  # what a closed campaign's record gains

    def stale_abandoned(self):
        # What an older abandon left: the record is abandoned, the id stayed on the list.
        gone = self.begun("stale-abandoned")
        P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertNotIn(gone["cycle_id"], self.listed())
        self.list_as(self.listed() + [gone["cycle_id"]])
        return gone["cycle_id"]

    def assert_only_the_close_changed_the_record(self, before):
        after = json.loads(self.path.read_text())
        changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
        self.assertLessEqual(changed, self.PROJECTED)
        self.assertEqual(after["cycles"], before["cycles"])

    def goal_intent(self):
        return {"head": C.completion_head(self.root, self.path), "goal": {"reason": "goal met"}}

    def test_stale_abandoned_id_is_left_out_and_the_list_is_not_rewritten(self):
        cycle = self.stale_abandoned()
        before = json.loads(self.path.read_text())
        status = C.status(self.root, self.path)
        self.assertTrue(status["closable"], status)
        self.assertEqual([row["cycle_id"] for row in status["cycles"]], [self.first["cycle_id"]])
        self.assertIn(cycle, [row["cycle_id"] for row in status["detached_cycles"]])
        record = P.read_cycle_record(self.root, cycle)
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assert_only_the_close_changed_the_record(before)
        self.assertEqual(self.listed(), [self.first["cycle_id"], cycle])
        self.assertEqual(P.read_cycle_record(self.root, cycle), record)  # the record is not rewritten
        event = json.loads((self.path.parent / C.EVENTS_DIR / "000001.json").read_text())
        self.assertEqual([row["cycle_id"] for row in event["payload"]["snapshot"]["cycles"]], [self.first["cycle_id"]])
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")  # the closed list still verifies

    def test_abandon_that_keeps_its_folder_no_longer_blocks_the_close(self):
        # The abandon of a cycle whose only file is excluded keeps the folder and the list entry (as it always did).
        gone = self.begun("kept-folder")
        folder = Path(gone["cycle_dir"])
        (folder / "artifacts" / ".scratch").write_text("kept")
        out = P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(out["status"], "no-lineage")
        self.assertIn(gone["cycle_id"], self.listed())
        self.assertTrue(folder.is_dir())
        self.assertTrue(C.status(self.root, self.path)["closable"])
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(self.listed(), [self.first["cycle_id"], gone["cycle_id"]])
        self.assertEqual((folder / "artifacts" / ".scratch").read_text(), "kept")

    def test_two_folders_naming_the_same_detached_id_are_still_a_duplicate(self):
        gone = self.begun("copied-folder")
        folder = Path(gone["cycle_dir"])
        (folder / "artifacts" / ".scratch").write_text("kept")
        P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertTrue(C.status(self.root, self.path)["closable"])  # one kept folder is left out of the close
        shutil.copytree(folder, folder.with_name(folder.name + "-copy"))
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-cycle-binding-duplicate", refusal)
        with self.assertRaises(C.CampaignError) as caught:
            self.close_campaign()
        self.assertEqual(caught.exception.code, "campaign-cycle-binding-duplicate")

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
        before = json.loads(self.path.read_text())
        self.assertTrue(C.status(self.root, self.path)["closable"])
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assert_only_the_close_changed_the_record(before)

    def test_an_id_with_no_record_is_still_refused(self):
        ghost = "cyc_" + "f" * 32
        self.list_as(self.listed() + [ghost])
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        detail = self.drift(refusal)
        self.assertEqual(detail["unrecorded_listed_cycle_ids"], [ghost])
        self.assertNotIn(OLD_NEXT_STEP, detail["next_step"])
        self.assertIn("no command settles it", detail["next_step"])
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
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        self.assertEqual(self.drift(refusal)["unlisted_member_cycle_ids"], [second["cycle_id"]])
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

    def test_a_goal_close_retried_after_a_refusal_still_closes(self):
        # A goal close keeps the campaign head it was prepared against. The first attempt is refused for an
        # open cycle; once that cycle is sealed the same intent must still close, as it does with no stale id.
        self.stale_abandoned()
        live, close_route = self.begun_open("still-open")
        intent = self.goal_intent()
        with self.assertRaises(C.CampaignError) as caught:
            C.close_for_completion(self.root, self.path, intent=intent, before_publish=lambda event: None)
        self.assertEqual(caught.exception.code, "campaign-cycle-provisional-active")
        self.assertEqual(C.completion_head(self.root, self.path), intent["head"])  # nothing was written
        self.write_output(live)
        close_route()
        P.finalize(self.root, cycle_id=live["cycle_id"])
        self.assertEqual(C.close_for_completion(self.root, self.path, intent=intent,
                                                before_publish=lambda event: None)["status"], "satisfied")

    def test_a_goal_close_replayed_from_its_saved_event_still_closes(self):
        self.stale_abandoned()
        intent = self.goal_intent()
        saved = []

        def stop(event):
            saved.append(event)
            raise OSError("interrupted before publish")

        with self.assertRaises(OSError):
            C.close_for_completion(self.root, self.path, intent=intent, before_publish=stop)
        self.assertEqual(C.completion_head(self.root, self.path), intent["head"])
        replay = C.close_for_completion(self.root, self.path, intent=dict(intent, event=saved[0]),
                                        before_publish=lambda event: None)
        self.assertEqual(replay["status"], "satisfied")
        self.assertEqual(C.campaign_state(self.root, self.path).last_sequence, 1)

    def test_a_whole_campaign_move_to_another_root_takes_the_kept_folder_along(self):
        # The move chooses what to carry from the list; a list the close does not rewrite still names the
        # abandoned cycle, so its folder (a person's file in it) goes with the campaign.
        gone = self.begun("kept-and-moved")
        folder = Path(gone["cycle_dir"])
        (folder / "artifacts" / ".scratch").write_text("kept")
        P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertIn(gone["cycle_id"], self.listed())
        import artifact_cross_root_move as X
        target = Path(self._tmp.name) / "target"
        target.mkdir()
        P.activate(target, repository_id="repo_" + "d" * 32, artifact_root_id="root_" + "e" * 32,
                   w7={"campaign_id": "camp_" + "c" * 32})
        with mock.patch.object(X, "_same_device", return_value=False):
            out = P.cycle_move(self.root, source_campaign=self.campaign, target_artifact_root=target)
        self.assertEqual(sorted(out["cycle_ids"]), sorted([self.first["cycle_id"], gone["cycle_id"]]))
        self.assertFalse(folder.exists())  # nothing of the campaign is left at the old root


class EveryListedCycleIsDetached(TidyBase):
    """2: nothing is left for a close to account for; the refusal says so, and what it points at works."""

    def alone(self):
        route, route_file = self.route(slug="alone", campaign_key="alone")
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        (Path(cycle["cycle_dir"]) / "artifacts" / ".scratch").write_text("kept")
        self.close(route, route_file)
        out = P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned", abandon_reason="operator-decision")
        self.assertEqual(out["status"], "no-lineage")
        path = Path(cycle["cycle_dir"]).parent / "campaign.json"
        self.assertEqual(json.loads(path.read_text())["cycles"], [cycle["cycle_id"]])
        return cycle, path

    def test_the_refusal_does_not_promise_a_close_that_refuses_again(self):
        cycle, path = self.alone()
        status = C.status(self.root, path)
        self.assertFalse(status["closable"])
        refusal = status["close_refusal"]
        self.assertEqual(refusal["reason"], "campaign-membership-drift")
        detail = self.drift(refusal)
        self.assertEqual(detail["detached_listed_cycle_ids"], [cycle["cycle_id"]])
        text = detail["next_step"]
        self.assertIn("every-listed-cycle-is-detached", text)
        self.assertNotIn("campaign-close takes", text)
        before = path.read_bytes()
        with self.assertRaises(C.CampaignError) as caught:
            C.close(self.root, path, reason="tidy")
        self.assertEqual(caught.exception.code, "campaign-membership-drift")  # the close does refuse
        self.assertEqual(path.read_bytes(), before)

    def test_beginning_a_cycle_in_it_is_what_settles_it(self):
        cycle, path = self.alone()
        campaign = json.loads(path.read_text())["campaign_id"]
        later, close_route = self.begun_open("alone-later", campaign=campaign, key="alone")  # the action the refusal names
        self.write_output(later)
        close_route()
        P.finalize(self.root, cycle_id=later["cycle_id"])
        status = C.status(self.root, path)
        self.assertTrue(status["closable"], status)
        self.assertEqual(C.close(self.root, path, reason="tidy")["status"], "satisfied")
        self.assertEqual(json.loads(path.read_text())["cycles"], [cycle["cycle_id"], later["cycle_id"]])
        self.assertTrue((Path(cycle["cycle_dir"]) / "artifacts" / ".scratch").is_file())


class AbandonWithFiles(TidyBase):
    """3: a person's own abandon does not refuse files left outside `artifacts/`; nothing else changes."""

    def stray(self, cycle, name="logs/run.log", body="a person's file\n"):
        target = Path(cycle["cycle_dir"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def abandon(self, cycle, **kwargs):
        """What `finalize --state abandoned` does when a person types it (the command line sets `leave_outside`)."""
        return P.finalize(self.root, cycle_id=cycle["cycle_id"], state="abandoned",
                          abandon_reason="operator-decision", leave_outside=True, **kwargs)

    def command_line(self, cycle):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = P.main(["finalize", "--artifact-root", str(self.root), "--cycle", cycle["cycle_id"],
                           "--state", "abandoned", "--abandon-reason", "operator-decision"])
        return code, out.getvalue()

    def test_abandon_leaves_a_subfolder_file_where_it_is_and_the_campaign_closes(self):
        gone = self.begun("abandon-logs")
        target = self.stray(gone)
        out = self.abandon(gone)
        self.assertEqual(out["status"], "no-lineage")
        self.assertEqual(P.read_cycle_record(self.root, gone["cycle_id"])["state"], "abandoned")
        self.assertEqual(target.read_text(), "a person's file\n")  # neither moved nor removed
        self.assertEqual(self.close_campaign()["status"], "satisfied")
        self.assertEqual(target.read_text(), "a person's file\n")

    def test_the_typed_command_is_the_person_s_abandon(self):
        gone = self.begun("abandon-typed")
        target = self.stray(gone)
        code, _printed = self.command_line(gone)
        self.assertEqual(code, 0)
        self.assertEqual(P.read_cycle_record(self.root, gone["cycle_id"])["state"], "abandoned")
        self.assertEqual(target.read_text(), "a person's file\n")

    def test_abandon_with_output_seals_it_and_keeps_the_outside_file_out_of_the_manifest(self):
        gone = self.begun("abandon-output")
        self.write_output(gone)
        target = self.stray(gone)
        out = self.abandon(gone, allow_open_route=True)
        self.assertEqual(out["status"], "sealed", out)
        manifest = json.loads((Path(gone["cycle_dir"]) / "manifest.json").read_text())
        self.assertEqual(manifest["cycle"]["state"], "abandoned")
        self.assertFalse([row for row in manifest["artifact_revisions"] if "run.log" in row["locator"]["path"]])
        self.assertEqual(target.read_text(), "a person's file\n")
        again = self.abandon(gone, allow_open_route=True)  # the refresh reads it again
        self.assertEqual((again.get("status"), again.get("refreshed")), ("already-sealed", False), again)  # not refused, not changed
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
        with self.assertRaises(P.ProducerError) as caught:  # `leave_outside` is for an abandon only
            P.finalize(self.root, cycle_id=cycle["cycle_id"], leave_outside=True)
        self.assertEqual(caught.exception.code, "output-invalid")

    def test_abandon_still_refuses_what_is_not_a_plain_file(self):
        gone = self.begun("abandon-fifo")
        os.mkfifo(str(Path(gone["cycle_dir"]) / "pipe"))
        with self.assertRaises(P.ProducerError) as caught:
            self.abandon(gone)
        self.assertEqual(caught.exception.code, "output-invalid")
        self.assertIn("non-regular-file:pipe", caught.exception.detail)

    def test_an_abandon_the_runtime_makes_by_itself_still_refuses_the_outside_file(self):
        # A sweep, a cancel and a finish all call `finalize(state="abandoned")` with no `leave_outside`:
        # the same refusal as before, the cycle stays open with the file untouched.
        gone = self.begun("abandon-by-runtime")
        target = self.stray(gone)
        with self.assertRaises(P.ProducerError) as caught:
            P.finalize(self.root, cycle_id=gone["cycle_id"], state="abandoned", abandon_reason="route-unrecoverable")
        self.assertEqual(caught.exception.code, "output-invalid")
        self.assertIn("file-outside-artifacts:logs/run.log", caught.exception.detail)
        self.assertEqual(P.read_cycle_record(self.root, gone["cycle_id"])["state"], "open")
        self.assertEqual(target.read_text(), "a person's file\n")


class AutomaticSweepKeepsTheCycleOpen(TidyBase):
    """4: the sweep's seal of a cycle (`route_autoclose._seal_cycle`, reached from `close_one` and from the
    open-cycle sweep) is what it was: a file outside `artifacts/` leaves the cycle open and the reason on record,
    never a proven job sealed as abandoned."""

    def sealing(self, *, proven):
        route, route_file = self.route(slug="sweep-" + ("proven" if proven else "unproven"), campaign_key="tidy")
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(cycle)
        stray = Path(cycle["cycle_dir"]) / "logs/run.log"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("a person's file\n")
        if proven:
            self.close(route, route_file)
        else:
            raw = json.loads(route_file.read_text())
            checked = F.R.verify_route(dict(raw), None, allow_stale_registry=True)
            F.R.close_route(checked, route_file, None, "fixture", allow_unproven=True,
                            autoclose={"reason": "idle", "trigger": "compose", "closed_by": "runtime",
                                       "proof": "not-claimed", "at": "2026-09-30T00:00:00Z"})
        record = P.read_cycle_record(self.root, cycle["cycle_id"])
        answer = route_autoclose._seal_cycle(self.root, route, proven, record=record)
        after = P.read_cycle_record(self.root, cycle["cycle_id"])
        self.assertEqual(after["state"], "open", answer)
        self.assertFalse((Path(cycle["cycle_dir"]) / "manifest.json").exists())
        self.assertEqual(stray.read_text(), "a person's file\n")
        return answer

    def test_a_proven_route_with_a_file_outside_artifacts_is_left_open(self):
        answer = self.sealing(proven=True)
        self.assertTrue(answer.startswith("cycle-left-open:output-invalid"), answer)
        self.assertIn("file-outside-artifacts:logs/run.log", answer)

    def test_an_unproven_route_with_a_file_outside_artifacts_is_left_open_too(self):
        # Nobody decided to abandon it; the person who may is the one who types `finalize --state abandoned`.
        answer = self.sealing(proven=False)
        self.assertTrue(answer.startswith("cycle-left-open:output-invalid"), answer)

    def test_the_same_cycle_without_the_file_is_sealed_as_it_always_was(self):
        route, route_file = self.route(slug="sweep-clean", campaign_key="tidy")
        cycle = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(cycle)
        self.close(route, route_file)
        record = P.read_cycle_record(self.root, cycle["cycle_id"])
        self.assertEqual(route_autoclose._seal_cycle(self.root, route, True, record=record), "cycle-completed")


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
        # The condition under which the close does it by itself is the layout scan's, root-wide ...
        for word in ("any campaign", "symbolic link", "folder it cannot read", "names no cycle or campaign",
                     "an id seen twice", "old cycles/ layout"):
            self.assertIn(word, refusal["detail"])
        # ... and the delete it names is final, so look for a folder that may only be out of sight first.
        self.assertIn("stays deleted even if its folder comes back", refusal["detail"])
        self.assertIn("look for the folder before deleting", refusal["detail"])

    def test_close_records_a_vanished_folder_itself_when_every_folder_can_be_read(self):
        self.sealed_without_folder()
        self.assertEqual(self.refusal()["reason"], "campaign-cycle-bindings-incomplete")  # status only reads
        self.assertEqual(self.close_campaign()["status"], "satisfied")

    def test_a_folder_naming_no_campaign_anywhere_under_campaigns_holds_the_close_back(self):
        # The text says "any campaign": a plain folder next to the campaigns (not in this one) is such a folder.
        ids = self.sealed_without_folder()
        notes = self.root / "campaigns" / "notes"
        notes.mkdir()
        with self.assertRaises(C.CampaignError) as caught:
            self.close_campaign()
        self.assertEqual(caught.exception.code, "campaign-cycle-bindings-incomplete")
        for cycle_id in ids:
            self.assertIn(cycle_id, caught.exception.detail)
        notes.rmdir()
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

    def test_moving_the_frame_out_of_a_closed_campaign_leaves_it_satisfied(self):
        # The closed campaign holds the legs' rows; moving the request's frame to another campaign afterwards
        # (the cycle-move that keeps a closed campaign's evidence) must not undo that close.
        frame, first, second = self.two_closed_legs()
        self.assertEqual(C.close(self.root, self.campaign_path, reason="tidy")["status"], "satisfied")
        route = S.T.compile_for("direct", self.root, slug="target", campaign_key="target-campaign")
        route_path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        other = self.begin_route(route, route_path)
        self.write_output(other, "plans/final_report.md", b"target\n")
        self.close(route, route_path)
        P.finalize(self.root, cycle_id=other["cycle_id"])
        moved = P.cycle_move(self.root, frame["cycle_id"], campaign=other["campaign_id"], reason="frame elsewhere")
        self.assertEqual(moved["status"], "moved")
        self.assertEqual(C.status(self.root, self.campaign_path)["state"], "satisfied")
        self.assertEqual(C.close(self.root, self.campaign_path, reason="again")["status"], "satisfied")

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
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-bindings-incomplete")
        self.assertIn(second["cycle_id"], status["close_refusal"]["detail"])
        # What the close's own reconcile then does with a folder at no known place is the existing
        # "vanished" handling, the same as for any cycle: the leg is recorded as gone (it is left out of the
        # close), and its folder and manifest stay exactly as they are.
        self.assertEqual(C.close(self.root, self.campaign_path, reason="tidy")["status"], "satisfied")
        self.assertTrue(P.read_cycle_record(self.root, second["cycle_id"]).get("deleted_at"))
        event = json.loads((self.campaign_path.parent / "campaign.events" / "000001.json").read_text())
        self.assertEqual({row["cycle_id"] for row in event["payload"]["snapshot"]["cycles"]},
                         {frame["cycle_id"], first["cycle_id"]})
        self.assertEqual((other / "manifest.json").read_bytes(), before)

    def test_a_renamed_frame_folder_is_followed_for_its_legs(self):
        frame, first, second = self.two_closed_legs()
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        frame_dir.rename(frame_dir.with_name("2026-11-19_renamed-request"))
        P.reconcile_root(self.root)
        self.assertTrue(C.status(self.root, self.campaign_path)["closable"])
        self.assertEqual(C.close(self.root, self.campaign_path, reason="tidy")["status"], "satisfied")

    def test_status_reads_only_after_a_frame_folder_is_renamed_by_hand(self):
        # `status` promises a pure read: with the frame renamed (the index hint is stale) it must not draw
        # the index again to find a leg. The cycle ids, and so their order, are random per fixture.
        frame, first, second = self.two_closed_legs()
        frame_dir = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        frame_dir.rename(frame_dir.with_name("2026-11-19_renamed-by-hand"))
        before = {str(path): path.read_bytes() for path in self.root.rglob("INDEX.*")}
        self.assertTrue(before)
        status = C.status(self.root, self.campaign_path)
        self.assertEqual({str(path): path.read_bytes() for path in self.root.rglob("INDEX.*")}, before,
                         "status rewrote an index")
        # The frame's own record still names its old folder, so the frame is what is refused; its legs, found
        # inside the renamed folder, are read as in their place (a reconcile then adopts the new name).
        self.assertEqual(status["close_refusal"], {"reason": "campaign-cycle-locator-invalid", "detail": frame["cycle_id"]})

    def test_a_leg_moved_out_by_cycle_move_is_read_as_before(self):
        # `cycle-move <leg>` makes the leg a folder of its own in the target campaign, keeping its
        # request binding; that place was accepted before and still is. Pulling it out by hand is not.
        frame, first, second = self.two_closed_legs()
        route = S.T.compile_for("direct", self.root, slug="target", campaign_key="target-campaign")
        route_path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        other = self.begin_route(route, route_path)
        self.write_output(other, "plans/final_report.md", b"target\n")
        self.close(route, route_path)
        P.finalize(self.root, cycle_id=other["cycle_id"])
        P.cycle_move(self.root, first["cycle_id"], campaign=other["campaign_id"], reason="one leg elsewhere")
        target_path = P.campaign_dir(self.root, other["campaign_id"]) / "campaign.json"
        self.assertTrue(P.read_cycle_record(self.root, first["cycle_id"]).get("request_binding"))
        status = C.status(self.root, target_path)
        self.assertTrue(status["closable"], status)
        self.assertEqual({row["cycle_id"] for row in status["cycles"]}, {other["cycle_id"], first["cycle_id"]})
        self.assertEqual(C.close(self.root, target_path, reason="tidy")["status"], "satisfied")

    def test_a_leg_record_with_a_broken_binding_is_just_out_of_place(self):
        # What `status` reads must not raise for a damaged record: the place is not accepted, nothing more,
        # and no frame is looked up (the folder's own shape names the frame).
        campaign = Path("/c")
        cid = "cyc_" + "a" * 32
        folder = campaign / "frame" / "_internal" / "executions" / cid
        for binding in ({}, {"cycle_id": 7}, {"path": "x"}):
            record = {"cycle_id": cid, "locator": cid, "request_binding": binding}
            with mock.patch.object(P, "cycle_dir", side_effect=AssertionError("a frame was looked up")):
                self.assertFalse(C._in_its_place(self.root, campaign, folder, record, cid))

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
