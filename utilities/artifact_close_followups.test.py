#!/usr/bin/env python3
"""A campaign close that reads what the producer wrote, names what holds it back, and the follow-ups of
the cycle-move pieces: a manifest larger than the old read limit, the folders that keep the layout scan
incomplete (named by path with what to do), a hand-edited binding and a leg in another frame (typed
refusals, no exception), the entrance after a reconcile, and the place check for the oldest layouts.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import hashlib
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
import artifact_manifest as M
import artifact_producer as P
import artifact_producer_cycle_ops as OPS

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


F = _load("followups_producer_fixture", "artifact_producer.test.py")
S = _load("followups_framed_fixture", "framed_start.test.py")
Q = _load("followups_request_fixture", "artifact_request.test.py")


class Sealed(F.ProducerTestBase):
    """A campaign with one sealed cycle (`self.first`) and the helpers to begin and seal more."""

    def setUp(self):
        super().setUp()
        route, route_file, self.first = self.begin(campaign_key="followups")
        self.write_output(self.first)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=self.first["cycle_id"])
        self.campaign = self.first["campaign_id"]
        self.path = Path(self.first["cycle_dir"]).parent / "campaign.json"

    def sealed_in(self, slug, campaign=None, key=None):
        route, route_file = self.route(slug=slug, campaign_key=key or "followups")
        kw = {"campaign_id": campaign} if campaign else {}
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct", **kw)
        self.write_output(begun)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        return begun

    def refusal(self):
        status = C.status(self.root, self.path)
        self.assertFalse(status["closable"], status)
        return status["close_refusal"]


# ---------------------------------------------------------------------------------------------------
# 1: a manifest the producer wrote that is larger than the old read limit
# ---------------------------------------------------------------------------------------------------


class BigManifestRead(Sealed):
    FILES = 4800
    PARTS = 3
    PART_BYTES = 240

    def setUp(self):
        super().setUp()
        self.big = self.big_cycle()
        self.manifest = Path(self.big["cycle_dir"]) / "manifest.json"
        self.assertGreater(self.manifest.stat().st_size, C.MAX_JSON, "the fixture must be larger than the old limit")

    def big_cycle(self):
        # Long paths, not many files, make the bytes: the manifest names every file about four times.
        route, route_file = self.route(slug="big-two", campaign_key="followups")
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        campaign_id=self.campaign)
        deep = Path(begun["cycle_dir"]) / "artifacts" / "/".join(
            ("p%d" % k) + "x" * self.PART_BYTES for k in range(self.PARTS))
        deep.mkdir(parents=True)
        for index in range(self.FILES):
            (deep / ("f%05d.md" % index)).write_bytes(b"x%d\n" % index)
        self.write_output(begun)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        return begun

    def parsed_manifests(self):
        seen = []
        real = C.read_json

        def spy(root, path, reads=None, limit=None):
            if Path(path).name == "manifest.json":
                seen.append(Path(path))
            return real(root, path, reads=reads, limit=limit)
        return seen, mock.patch.object(C, "read_json", side_effect=spy)

    def test_status_and_close_read_a_manifest_larger_than_the_old_limit_without_parsing_it(self):
        seen, spy = self.parsed_manifests()
        with spy:
            status = C.status(self.root, self.path)
            self.assertTrue(status["closable"], status)
            rows = {row["cycle_id"]: row for row in status["cycles"]}
            self.assertEqual(set(rows), {self.first["cycle_id"], self.big["cycle_id"]})
            on_disk = "sha256:" + hashlib.sha256(self.manifest.read_bytes()).hexdigest()
            self.assertEqual(rows[self.big["cycle_id"]]["manifest_digest"], on_disk)
            self.assertEqual(rows[self.big["cycle_id"]]["index_digest"], on_disk)
            self.assertEqual(C.close(self.root, self.path, reason="big")["status"], "satisfied")
        # The close and status read the same rows the long way would give (the small cycle is read in full).
        self.assertNotIn(self.manifest, [path for path in seen])
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")

    def test_the_fast_row_is_the_row_a_full_read_gives(self):
        fast = {row["cycle_id"]: row for row in C._snapshot(self.root, self.path)["cycles"]}
        with mock.patch.object(C, "_sealed_as_recorded", return_value=None):
            slow = {row["cycle_id"]: row for row in C._snapshot(self.root, self.path)["cycles"]}
        self.assertEqual(fast, slow)

    def test_bytes_that_changed_or_a_record_that_does_not_agree_are_read_and_checked_in_full(self):
        raw = self.manifest.read_bytes()
        record_path = P.cycle_record_path(self.root, self.big["cycle_id"])
        record_raw = record_path.read_bytes()
        other = "camp_" + "e" * 32
        self.assertIn(self.campaign.encode(), raw)
        # 1: the same size, a campaign that is not this one's.
        self.manifest.write_bytes(raw.replace(self.campaign.encode(), other.encode()))
        self.assertGreater(self.manifest.stat().st_size, C.MAX_JSON)
        seen, spy = self.parsed_manifests()
        with spy:
            refusal = self.refusal()
        self.assertIn(self.manifest, seen)  # parsed, not taken as recorded
        self.assertIn(refusal["reason"], {"campaign-seal-mismatch", "campaign-cycle-binding-mismatch",
                                          "campaign-manifest-invalid"}, refusal)
        with self.assertRaises(C.CampaignError):
            C.close(self.root, self.path, reason="big")
        # 2: no longer JSON.
        self.manifest.write_bytes(raw[:len(raw) // 2])
        self.assertEqual(self.refusal()["reason"], "campaign-input-invalid")
        self.manifest.write_bytes(raw)
        self.assertTrue(C.status(self.root, self.path)["closable"])
        # 3: the bytes are as sealed but the record's own content digest no longer holds (a hand edit).
        record = json.loads(record_raw)
        record["producer_id"] = "prod_" + "0" * 32
        record_path.write_text(json.dumps(record))
        seen, spy = self.parsed_manifests()
        with spy:
            refusal = self.refusal()
        self.assertIn(self.manifest, seen)
        self.assertEqual(refusal["reason"], "campaign-seal-mismatch", refusal)
        record_path.write_bytes(record_raw)
        self.assertTrue(C.status(self.root, self.path)["closable"])


class ReadLimit(Sealed):
    def test_one_limit_is_every_reader_s_and_it_covers_what_the_writer_can_make(self):
        # The writer has no cap of its own (it seals one manifest whatever the file count), so the reader's
        # limit is one named value and a read past it is the same typed refusal as before.
        self.assertGreaterEqual(M.MAX_MANIFEST_BYTES, 256 * 1024 * 1024)
        manifest_path = Path(self.first["cycle_dir"]) / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")  # changed bytes: the long way
        with mock.patch.object(M, "MAX_MANIFEST_BYTES", 1024):
            refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-input-kind-or-size")
        self.assertEqual(Path(refusal["detail"]).name, "manifest.json")

    def test_a_small_old_limit_still_refuses_other_documents(self):
        with mock.patch.object(C, "MAX_JSON", 16):
            with self.assertRaises(C.CampaignError) as caught:
                C.read_json(self.root, self.path)
        self.assertEqual(caught.exception.code, "campaign-input-kind-or-size")


# ---------------------------------------------------------------------------------------------------
# 2: what holds the close back is named
# ---------------------------------------------------------------------------------------------------


class NamedBlockers(Sealed):
    def vanished(self):
        member = self.sealed_in("vanished-member")
        folder = P.cycle_dir(self.root, self.campaign, member["cycle_id"], P.read_cycle_record(self.root, member["cycle_id"]))
        shutil.rmtree(folder)
        return member["cycle_id"]

    def rel(self, path):
        return Path(path).relative_to(self.root).as_posix()

    def blockers(self):
        return OPS._scan_layout(self.root).blockers

    def detail(self):
        refusal = self.refusal()
        self.assertEqual(refusal["reason"], "campaign-cycle-bindings-incomplete")
        return refusal["detail"]

    def test_a_complete_layout_names_nothing(self):
        self.vanished()
        self.assertEqual(self.blockers(), [])
        self.assertNotIn("held-back-by", self.detail())

    def test_a_folder_without_campaign_json_next_to_the_campaigns_is_named(self):
        # What a person sees in the field: a folder whose name differs from a real campaign folder by one
        # character, with evidence files in it.
        self.vanished()
        real = self.path.parent.name
        stray = self.root / "campaigns" / real.replace("-", "_", 1) if "-" in real else self.root / "campaigns" / (real + "x")
        stray.mkdir()
        (stray / "evidence.txt").write_text("kept")
        self.assertFalse(OPS._scan_layout(self.root).complete)
        self.assertIn((self.rel(stray), "no-campaign-id"), self.blockers())
        detail = self.detail()
        self.assertIn("held-back-by=" + self.rel(stray) + " (no-campaign-id)", detail)
        self.assertIn("move it out of campaigns/", detail)
        self.assertIn("renaming the folder changes nothing", detail)
        self.assertNotIn("delete the", detail)
        with self.assertRaises(C.CampaignError) as caught:
            C.close(self.root, self.path, reason="x")
        self.assertIn(self.rel(stray), caught.exception.detail)
        # Moved out of campaigns/, it holds nothing back, and the close takes the vanished folder for gone.
        outside = self.root / "kept-outside"
        shutil.move(str(stray), str(outside))
        self.assertEqual(self.blockers(), [])
        self.assertEqual(C.close(self.root, self.path, reason="x")["status"], "satisfied")
        self.assertTrue((outside / "evidence.txt").is_file())

    def test_a_folder_inside_a_campaign_that_is_not_a_cycle_is_named(self):
        self.vanished()
        feedback = self.path.parent / "feedback"
        feedback.mkdir()
        (feedback / "note.md").write_text("n")
        self.assertIn((self.rel(feedback), "not-a-cycle"), self.blockers())
        detail = self.detail()
        self.assertIn(self.rel(feedback) + " (not-a-cycle)", detail)
        self.assertIn("move it out of campaigns/", detail)
        self.assertIn("renaming it changes nothing", detail)

    def test_a_dated_folder_without_a_cycle_json_is_named(self):
        self.vanished()
        dated = self.path.parent / "2026-11-19_something"
        dated.mkdir()
        (dated / "notes").mkdir()
        self.assertIn((self.rel(dated), "not-a-cycle"), self.blockers())
        self.assertIn(self.rel(dated) + " (not-a-cycle)", self.detail())

    def test_a_folder_with_only_artifacts_and_no_cycle_json_is_named(self):
        self.vanished()
        shaped = self.path.parent / "2026-11-19_artifacts-only"
        (shaped / "artifacts").mkdir(parents=True)
        (shaped / "artifacts" / "a.md").write_text("a")
        self.assertIn((self.rel(shaped), "not-a-cycle"), self.blockers())
        detail = self.detail()
        self.assertIn(self.rel(shaped) + " (not-a-cycle)", detail)
        self.assertIn("needs that file again to count as one", detail)

    def test_files_and_hidden_names_hold_nothing_back(self):
        # Of the shapes one may suspect, these do not make the scan incomplete.
        self.vanished()
        (self.root / "campaigns" / "README.txt").write_text("a file")
        (self.root / "campaigns" / ".hidden").mkdir()
        (self.path.parent / "notes.md").write_text("a file inside a campaign")
        (self.path.parent / ".scratch").mkdir()
        self.assertEqual(self.blockers(), [])
        self.assertNotIn("held-back-by", self.detail())

    def test_a_symbolic_link_is_named(self):
        self.vanished()
        link = self.root / "campaigns" / "link-to-elsewhere"
        link.symlink_to(self.root)
        self.assertIn((self.rel(link), "symbolic-link"), self.blockers())
        detail = self.detail()
        self.assertIn(self.rel(link) + " (symbolic-link)", detail)
        self.assertIn("never followed", detail)

    def test_a_link_inside_a_campaign_is_named(self):
        self.vanished()
        link = self.path.parent / "inner-link"
        link.symlink_to(self.root)
        self.assertIn((self.rel(link), "symbolic-link"), self.blockers())

    def test_an_id_seen_twice_names_both_folders(self):
        self.vanished()
        copy = self.root / "campaigns" / "copy-of-the-campaign"
        shutil.copytree(self.path.parent, copy)
        found = dict(self.blockers())
        self.assertTrue(any(where == self.rel(copy) and reason.startswith("id-seen-twice camp_") for where, reason in self.blockers()),
                        found)
        detail = self.detail()
        self.assertIn(self.rel(copy), detail)
        self.assertIn(self.path.parent.name, detail)  # "(also <the other folder>)"
        self.assertIn("move the copy out of campaigns/", detail)

    def test_the_oldest_cycles_layout_is_named(self):
        self.vanished()
        old = self.path.parent / "cycles"
        old.mkdir()
        self.assertIn((self.rel(old), "old-cycles-layout"), self.blockers())
        detail = self.detail()
        self.assertIn(self.rel(old) + " (old-cycles-layout)", detail)
        self.assertIn("no cycles/ folder is left", detail)

    def test_an_unreadable_folder_is_named(self):
        self.vanished()
        campaigns = self.root / "campaigns"
        real_listdir = os.listdir
        stray = self.path.parent

        def listdir(path="."):
            if Path(str(path)) == stray:
                raise PermissionError(13, "denied")
            return real_listdir(path)
        with mock.patch.object(OPS.os, "listdir", side_effect=listdir):
            self.assertIn((self.rel(stray), "unreadable"), self.blockers())
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(str(path)).name == "will-not-stat":
                raise OSError(5, "io")
            return real_lstat(path, *args, **kwargs)
        (campaigns / "will-not-stat").mkdir()
        with mock.patch.object(OPS.os, "lstat", side_effect=lstat):
            self.assertIn((self.rel(campaigns / "will-not-stat"), "unreadable"), self.blockers())
        # The base itself unreadable.
        with mock.patch.object(OPS.os, "listdir", side_effect=PermissionError(13, "denied")):
            self.assertIn(("campaigns", "unreadable"), self.blockers())

    def test_many_are_cut_to_the_first_five_and_a_count(self):
        self.vanished()
        for index in range(8):
            (self.path.parent / ("stray-%d" % index)).mkdir()
        detail = self.detail()
        self.assertEqual(detail.count("(not-a-cycle)"), 5)
        self.assertIn("+3 more", detail)

    def test_status_names_the_same_folders(self):
        self.vanished()
        stray = self.root / "campaigns" / "stray-folder"
        stray.mkdir()
        status = C.status(self.root, self.path)
        self.assertIn("campaigns/stray-folder (no-campaign-id)", status["close_refusal"]["detail"])

    def test_a_folder_holding_things_back_is_left_exactly_as_it_is(self):
        self.vanished()
        stray = self.root / "campaigns" / "stray-folder"
        stray.mkdir()
        (stray / "a.txt").write_text("a")
        before = sorted(p.relative_to(self.root).as_posix() for p in stray.rglob("*"))
        self.refusal()
        with self.assertRaises(C.CampaignError):
            C.close(self.root, self.path, reason="x")
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in stray.rglob("*")), before)
        self.assertEqual((stray / "a.txt").read_text(), "a")


# ---------------------------------------------------------------------------------------------------
# 3: the follow-ups of the cycle-move pieces
# ---------------------------------------------------------------------------------------------------


class NoMemberRefusal(unittest.TestCase):
    """(a) `manifest-not-kept` is gone; a member is kept as the cycle itself is and the move goes on
    (the move itself is tested in artifact_request.test.py)."""

    def test_the_refusal_code_is_gone(self):
        source = (HERE / "artifact_producer_cycle_ops.py").read_text()
        self.assertNotIn("manifest-not-kept", source)
        self.assertNotIn("_campaign_is_closed", source)


class OneEntranceAfterReconcile(Sealed):
    """(b) A reconcile that set something right asks for the entrance to be drawn again, whatever the move then does."""

    def other(self):
        return self.sealed_in("elsewhere", key="elsewhere-campaign")

    def test_a_first_move_after_a_hand_move_asks_although_it_is_unchanged(self):
        other = self.other()
        source = Path(self.first["cycle_dir"])
        target_dir = P.campaign_dir(self.root, other["campaign_id"])
        source.rename(target_dir / source.name)
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            moved = P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(moved["status"], "unchanged")
        refresh.assert_called_once()

    def test_a_move_that_follows_a_reconcile_asks_once_each_for_the_reconcile_and_the_move(self):
        other = self.other()
        third = self.sealed_in("third", key="third-campaign")
        source = Path(self.first["cycle_dir"])
        source.rename(P.campaign_dir(self.root, other["campaign_id"]) / source.name)
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            moved = P.cycle_move(self.root, self.first["cycle_id"], campaign=third["campaign_id"])
        self.assertEqual(moved["status"], "moved")
        self.assertEqual(refresh.call_count, 2)

    def test_a_plain_unchanged_move_does_not_ask(self):
        with mock.patch("artifact_entrance.request_refresh") as refresh:
            self.assertEqual(P.cycle_move(self.root, self.first["cycle_id"], campaign=self.campaign)["status"], "unchanged")
        refresh.assert_not_called()


class PlaceHoldsForOldLayouts(Sealed):
    """(c) The one-folder check of a cycle's place is true for the oldest layout and for a journal backup,
    so a move of such a cycle does not run a reconcile of the whole root first."""

    def test_the_oldest_cycles_layout_holds(self):
        campaign_dir = self.path.parent
        cycle_id = self.first["cycle_id"]
        old = campaign_dir / "cycles" / cycle_id
        old.parent.mkdir()
        shutil.move(self.first["cycle_dir"], str(old))
        record = dict(P.read_cycle_record(self.root, cycle_id))
        record.pop("locator")
        with mock.patch.object(OPS, "cycle_dir", return_value=old):
            self.assertTrue(OPS._record_place_holds(self.root, record))
            # Under another campaign's folder it does not.
            other = self.sealed_in("other-campaign-for-old", key="old-other")
            wrong = dict(record, campaign_id=other["campaign_id"])
            self.assertFalse(OPS._record_place_holds(self.root, wrong))

    def test_a_journal_backup_record_holds_without_looking_at_campaigns(self):
        record = dict(P.read_cycle_record(self.root, self.first["cycle_id"]), journal_migration={"operation_id": "op"})
        with mock.patch.object(OPS, "cycle_dir", side_effect=AssertionError("looked")):
            self.assertTrue(OPS._record_place_holds(self.root, record))

    def test_a_plain_cycle_still_holds_and_a_moved_one_does_not(self):
        record = P.read_cycle_record(self.root, self.first["cycle_id"])
        self.assertTrue(OPS._record_place_holds(self.root, record))
        other = self.sealed_in("elsewhere-again", key="again-campaign")
        source = Path(self.first["cycle_dir"])
        source.rename(P.campaign_dir(self.root, other["campaign_id"]) / source.name)
        self.assertFalse(OPS._record_place_holds(self.root, record))


class HandEditedBinding(Sealed):
    """(d) A `.cycle.json` whose `cycle_id` is not a string is a typed refusal from `status` and `close`."""

    def test_status_and_close_refuse_in_type(self):
        binding = Path(self.first["cycle_dir"]) / LOC.CYCLE_BINDING
        document = json.loads(binding.read_text())
        for bad in ([document["cycle_id"]], {"id": 1}, 7, None):
            binding.write_text(json.dumps(dict(document, cycle_id=bad)))
            status = C.status(self.root, self.path)
            self.assertFalse(status["closable"], status)
            self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-binding-duplicate", bad)
            with self.assertRaises(C.CampaignError) as caught:
                C.close(self.root, self.path, reason="x")
            self.assertEqual(caught.exception.code, "campaign-cycle-binding-duplicate")


class LegInAnotherFrame(S.SourceRepoStartBase):
    """(e) A leg that sits in some other frame of the same campaign: a typed refusal, not a `LocatorError`."""

    selected = Q.RequestFolderTest.selected
    cycle = Q.RequestFolderTest.cycle
    next_execution = Q.RequestFolderTest.next_execution
    begin_route = Q.RequestFolderTest.begin_route
    two_closed_legs = None

    def closed_legs(self):
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
        return frame, first, second

    def test_status_and_close_refuse_in_type(self):
        import route_plan
        frame, first, second = self.closed_legs()
        campaign_path = P.campaign_dir(self.root, frame["campaign_id"]) / "campaign.json"
        # A second "frame" in the same campaign: an ordinary cycle whose record says it is a frame.
        route = S.T.compile_for("direct", self.root, slug="other-frame", campaign_key="framed-key")
        route_path = Path(S.L.admit_runtime_route(self.root, route).route_file)
        stray = P.begin(self.root, route_file=route_path, capability="autopilot-code", intensity="direct",
                        campaign_id=frame["campaign_id"])
        self.write_output(stray, "plans/final_report.md", b"stray\n")
        self.close(route, route_path)
        P.finalize(self.root, cycle_id=stray["cycle_id"])
        record = dict(P.read_cycle_record(self.root, stray["cycle_id"]), route_capability=route_plan.CAPABILITY)
        P._write_cycle_record(self.root, record, exclusive=False)
        leg_dir = P.cycle_dir(self.root, second["campaign_id"], second["cycle_id"])
        into = Path(stray["cycle_dir"]) / "_internal" / "executions" / leg_dir.name
        into.parent.mkdir(parents=True)
        leg_dir.rename(into)
        status = C.status(self.root, campaign_path)
        self.assertFalse(status["closable"], status)
        self.assertEqual(status["close_refusal"]["reason"], "request-binding-invalid", status)
        # The folder that is in the wrong frame is named from the root.
        self.assertIn(into.relative_to(self.root).as_posix(), status["close_refusal"]["detail"])
        with self.assertRaises(C.CampaignError) as caught:
            C.close(self.root, campaign_path, reason="x")
        self.assertEqual(caught.exception.code, "request-binding-invalid")


if __name__ == "__main__":
    unittest.main()
