#!/usr/bin/env python3
"""A campaign close that reads what the producer wrote, names what holds it back, and the follow-ups of
the cycle-move pieces: a manifest larger than the old read limit, the folders that keep the layout scan
incomplete (named by path with what to do), a hand-edited binding and a leg in another frame (typed
refusals, no exception), the entrance after a reconcile, and the place check for the oldest layouts.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import io
import shutil
import tracemalloc
import unittest
from unittest import mock

os.environ.setdefault("AGENT_ARTIFACT_CHECKPOINT", "off")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_identity as I
import artifact_lifecycle as LC
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


def peak_of(function):
    """`(result, peak bytes)` of what the Python allocator held at most while `function` ran."""
    tracemalloc.start()
    try:
        result = function()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


SMALL = 4 * 1024 * 1024  # what a read that does not hold a manifest may use (it holds ~2.4 MiB); the fixtures' manifests are 9 MiB and up


def outcome(function):
    """`closable`, or the reason a close refuses, or the exception, of a status-like read."""
    try:
        status = function()
        return "closable" if status.get("closable") else (status.get("close_refusal") or {}).get("reason")
    except C.CampaignError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__


def whole_read_rows(root, path):
    """The rows the long way gives.  A record that does not carry its own content digest is not taken as
    written by the producer, so every manifest is read and judged whole; the records are put back after."""
    records = sorted((Path(root) / ".runtime/artifact-producer/v1/cycles").glob("*.json"))
    saved = {record: record.read_bytes() for record in records}
    try:
        for record, raw in saved.items():
            document = json.loads(raw)
            document.pop("control_record_digest", None)
            record.write_text(json.dumps(document))
        return {row["cycle_id"]: row for row in C._snapshot(root, path)["cycles"]}
    finally:
        for record, raw in saved.items():
            record.write_bytes(raw)


# ---------------------------------------------------------------------------------------------------
# 1: a manifest the producer wrote that is larger than the old read limit
# ---------------------------------------------------------------------------------------------------


class BigFixture(Sealed):
    """The campaign of `Sealed` and, beside its small cycle, one whose manifest is over the old read limit."""
    FILES = 2400
    PARTS = 3
    PART_BYTES = 240

    def setUp(self):
        super().setUp()
        self.big = self.big_cycle()
        self.manifest = Path(self.big["cycle_dir"]) / "manifest.json"
        self.assertGreater(self.manifest.stat().st_size, 2 * SMALL, "the fixture must be well over what a read may hold")

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


class BigManifestRead(BigFixture):
    FILES = 4800  # a manifest larger than the old read limit

    def setUp(self):
        super().setUp()
        self.assertGreater(self.manifest.stat().st_size, C.MAX_JSON, "the fixture must be larger than the old limit")

    def test_status_and_close_do_not_hold_a_manifest_larger_than_the_old_limit(self):
        # What is observed is what the reader holds, not how it is called: a read that parses the 17 MiB
        # document holds several times that (see the changed-bytes test), this one holds a chunk.
        status, peak = peak_of(lambda: C.status(self.root, self.path))
        self.assertTrue(status["closable"], status)
        self.assertLess(peak, SMALL)
        rows = {row["cycle_id"]: row for row in status["cycles"]}
        self.assertEqual(set(rows), {self.first["cycle_id"], self.big["cycle_id"]})
        on_disk = "sha256:" + hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.assertEqual(rows[self.big["cycle_id"]]["manifest_digest"], on_disk)
        self.assertEqual(rows[self.big["cycle_id"]]["index_digest"], on_disk)
        closed, peak = peak_of(lambda: C.close(self.root, self.path, reason="big"))
        self.assertEqual(closed["status"], "satisfied")
        self.assertLess(peak, SMALL)
        self.assertEqual(C.status(self.root, self.path)["state"], "satisfied")

    def test_the_fast_row_is_the_row_a_full_read_gives(self):
        fast = {row["cycle_id"]: row for row in C._snapshot(self.root, self.path)["cycles"]}
        self.assertEqual(fast, whole_read_rows(self.root, self.path))

    def test_bytes_that_changed_or_a_record_that_does_not_agree_are_read_and_checked_in_full(self):
        raw = self.manifest.read_bytes()
        record_path = P.cycle_record_path(self.root, self.big["cycle_id"])
        record_raw = record_path.read_bytes()
        other = "camp_" + "e" * 32
        self.assertIn(self.campaign.encode(), raw)
        # 1: the same size, a campaign that is not this one's.
        self.manifest.write_bytes(raw.replace(self.campaign.encode(), other.encode()))
        refusal, peak = peak_of(self.refusal)
        self.assertGreater(peak, len(raw))  # the document was parsed, not taken as recorded
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
        refusal, peak = peak_of(self.refusal)
        self.assertGreater(peak, len(raw))
        self.assertEqual(refusal["reason"], "campaign-seal-mismatch", refusal)
        record_path.write_bytes(record_raw)
        self.assertTrue(C.status(self.root, self.path)["closable"])

    def test_a_file_that_changes_while_it_is_hashed_is_read_the_long_way(self):
        real = LOC.hashlib.sha256

        class Touch:
            def __init__(self, path):
                self.inner, self.path, self.done = real(), path, False

            def update(self, data):
                if not self.done:
                    self.done = True
                    with open(self.path, "ab") as handle:
                        handle.write(b" ")
                self.inner.update(data)

            def hexdigest(self):
                return self.inner.hexdigest()
        with mock.patch.object(LOC.hashlib, "sha256", lambda: Touch(self.manifest)):
            digest, fields, _size = LOC.read_sealed_head(self.manifest, ("cycle",))
        self.assertIsNone(digest)
        # The file is not the sealed bytes any more (one space was added): the document is read and judged whole.
        status, peak = peak_of(lambda: C.status(self.root, self.path))
        self.assertTrue(status["closable"], status)
        self.assertGreater(peak, self.manifest.stat().st_size)


class BigActiveCycle(BigFixture):
    """A cycle sealed while its route was open (`active`), the route ended since: a normal state, and the
    shortcut answers for the route from the bytes (the whole read held every kept copy besides)."""

    def big_cycle(self):
        route, route_file = self.route(slug="big-active", campaign_key="followups")
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        campaign_id=self.campaign)
        deep = Path(begun["cycle_dir"]) / "artifacts" / "/".join(
            ("p%d" % k) + "x" * self.PART_BYTES for k in range(self.PARTS))
        deep.mkdir(parents=True)
        for index in range(self.FILES):
            (deep / ("f%05d.md" % index)).write_bytes(b"x%d\n" % index)
        self.write_output(begun)
        P.finalize(self.root, cycle_id=begun["cycle_id"], allow_open_route=True)
        self.route_args = (route, route_file)
        return begun

    def test_an_open_route_is_refused_and_an_ended_one_is_closable_without_holding_the_manifest(self):
        status, peak = peak_of(lambda: C.status(self.root, self.path))
        self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-provisional-active", status)
        self.assertLess(peak, SMALL)
        self.close(*self.route_args)
        status, peak = peak_of(lambda: C.status(self.root, self.path))
        self.assertTrue(status["closable"], status)
        self.assertLess(peak, SMALL)
        self.assertEqual(status["unproven_cycles"]["count"], 1)
        self.assertEqual({row["cycle_id"]: row for row in status["cycles"]}[self.big["cycle_id"]],
                         whole_read_rows(self.root, self.path)[self.big["cycle_id"]] | {"disposition": "sealed-unproven"})

    def test_the_fast_row_is_the_row_a_full_read_gives(self):
        self.close(*self.route_args)
        fast = {row["cycle_id"]: row for row in C._snapshot(self.root, self.path)["cycles"]}
        self.assertEqual(fast, whole_read_rows(self.root, self.path))


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
        self.assertIn("no command writes that file back", detail)
        self.assertNotIn("needs that file again", detail)

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
        # What is said is what happens (see `test_moving_the_oldest_layout_out_deletes_what_it_held`): no promise of a way back.
        self.assertIn("no command settles it today", detail)
        self.assertIn("do not come back when it is moved back", detail)
        self.assertNotIn("until it comes back", detail)
        self.assertNotIn("Then run campaign-close again", detail.split("old-cycles-layout:")[1].split("Without waiting")[0])

    def test_moving_the_oldest_layout_out_deletes_what_it_held(self):
        # The fact the advice rests on: out of campaigns/ the cycles of `cycles/` are recorded as deleted by the
        # close, and the deletion stays when the folder comes back.
        cycle_id = self.first["cycle_id"]
        old = self.path.parent / "cycles"
        (old).mkdir()
        shutil.move(self.first["cycle_dir"], str(old / cycle_id))
        record = dict(P.read_cycle_record(self.root, cycle_id))
        record.pop("locator", None)
        P._write_cycle_record(self.root, record, exclusive=False)
        member = self.sealed_in("second-member")
        outside = self.root / "outside"
        outside.mkdir()
        shutil.move(str(old), str(outside / "cycles"))
        self.assertEqual(self.blockers(), [])
        try:
            C.close(self.root, self.path, reason="x")
        except C.CampaignError:
            pass
        self.assertTrue(P.read_cycle_record(self.root, cycle_id).get("deleted_at"))
        shutil.move(str(outside / "cycles"), str(old))
        P.reconcile_root(self.root)
        self.assertTrue(P.read_cycle_record(self.root, cycle_id).get("deleted_at"))
        self.assertIsNone(P.read_cycle_record(self.root, member["cycle_id"]).get("deleted_at"))

    def test_a_folder_that_cannot_be_opened_is_unreadable_not_a_folder_without_a_campaign(self):
        if os.geteuid() == 0:
            self.skipTest("a superuser opens it")
        self.vanished()
        locked = self.root / "campaigns" / "locked"
        locked.mkdir()
        (locked / "campaign.json").write_text("{}")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        self.assertIn((self.rel(locked), "unreadable"), self.blockers())
        self.assertIn(self.rel(locked) + " (unreadable)", self.detail())

    def test_a_folder_inside_the_campaign_that_cannot_be_read_is_a_refusal_not_an_exception(self):
        if os.geteuid() == 0:
            self.skipTest("a superuser opens it")
        locked = self.path.parent / "locked-inside"
        locked.mkdir()
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        status = C.status(self.root, self.path)
        self.assertFalse(status["closable"], status)
        self.assertEqual(status["close_refusal"]["reason"], "campaign-directory-unreadable")
        self.assertIn(self.rel(locked), status["close_refusal"]["detail"])
        self.assertNotIn(str(self.root), status["close_refusal"]["detail"])
        with self.assertRaises(C.CampaignError) as caught:
            C.close(self.root, self.path, reason="x")
        self.assertEqual(caught.exception.code, "campaign-directory-unreadable")

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
    """(d) A `.cycle.json` whose `cycle_id` is not a string is a typed refusal from `status` and `close`, named for
    what it is (a binding of the wrong shape), not as an id seen twice."""

    def test_status_and_close_refuse_in_type(self):
        binding = Path(self.first["cycle_dir"]) / LOC.CYCLE_BINDING
        document = json.loads(binding.read_text())
        for bad in ([document["cycle_id"]], {"id": 1}, 7, None):
            binding.write_text(json.dumps(dict(document, cycle_id=bad)))
            status = C.status(self.root, self.path)
            self.assertFalse(status["closable"], status)
            # Not an overlap, so not called one: the binding's shape is wrong, and the folder is named from the root.
            self.assertEqual(status["close_refusal"]["reason"], "campaign-cycle-binding-mismatch", bad)
            self.assertIn("cycle_id is not a string", status["close_refusal"]["detail"])
            self.assertIn(Path(self.first["cycle_dir"]).relative_to(self.root).as_posix(), status["close_refusal"]["detail"])
            self.assertNotIn(str(self.root), status["close_refusal"]["detail"])
            with self.assertRaises(C.CampaignError) as caught:
                C.close(self.root, self.path, reason="x")
            self.assertEqual(caught.exception.code, "campaign-cycle-binding-mismatch")


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


# ---------------------------------------------------------------------------------------------------
# 4: the few top-level values of a canonical document, read as a stream
# ---------------------------------------------------------------------------------------------------


class ScanTopFields(unittest.TestCase):
    DOC = {"artifact_revisions": [{"a": 'x"[{', "b": [1, 2, {"c": "}]\\"}], "\u00e9": "\u00fc\n"} for _ in range(5)],
           "artifact_root_id": "r", "campaign": {"campaign_id": "c"}, "cycle": {"state": "completed"},
           "events": [1, 2, [3], "\\"], "producer": {"producer_id": "p"}, "routes": [{"r": 1}], "zz": 1.5}

    def scan(self, raw, names, chunk=1 << 20):
        with mock.patch.object(M, "_SCAN_CHUNK", chunk):
            return M.scan_top_fields(io.BytesIO(raw), names)

    def test_the_named_values_and_only_those_are_answered_whatever_the_chunking(self):
        raw = M.canonical_bytes(self.DOC)
        for chunk in (1, 2, 3, 7, 64, 1 << 20):
            for names in (("cycle", "producer"), ("artifact_root_id", "campaign", "cycle", "producer", "routes", "zz"),
                          ("zz",), ("nope",), ()):
                self.assertEqual(self.scan(raw, names, chunk), {k: self.DOC[k] for k in names if k in self.DOC},
                                 (chunk, names))

    def test_random_documents_agree_with_a_parse(self):
        import random
        rng = random.Random(7)

        def value(depth=0):
            kind = rng.random()
            if depth > 3 or kind < 0.3:
                return rng.choice([1, -2, 3.5, True, None, "s", 'a"b', "}{][", "\\", '\\"', "\u00fc", '"\\"', "x\\"])
            if kind < 0.65:
                return [value(depth + 1) for _ in range(rng.randint(0, 4))]
            return {rng.choice("abcdefgh") + str(i): value(depth + 1) for i in range(rng.randint(0, 4))}
        for _ in range(400):
            document = {rng.choice("abcdefghijk") + str(i): value() for i in range(rng.randint(1, 6))}
            names = rng.sample(sorted(document), rng.randint(1, len(document)))
            self.assertEqual(self.scan(M.canonical_bytes(document), names, rng.choice([1, 3, 8, 100, 1 << 20])),
                             {k: document[k] for k in names}, document)

    def test_a_document_that_is_not_the_compact_sorted_form_is_not_answered(self):
        raw = M.canonical_bytes(self.DOC)
        names = ("cycle", "producer", "zz")
        for other in (json.dumps(self.DOC).encode(), json.dumps(self.DOC, sort_keys=True, indent=1).encode(),
                      b"[1]", raw.replace(b'"cycle"', b'"bycle"', 1), raw.replace(b'"zz"', b'"campaign"', 1)):
            self.assertIsNone(self.scan(other, names), other[:40])

    def test_what_is_held_does_not_grow_with_the_document(self):
        class Endless:
            """About 96 MiB of one canonical document, made as it is read."""
            def __init__(self):
                def pieces():
                    yield b'{"artifact_revisions":['
                    for i in range(130000):
                        yield b'{"p":"%s","n":%d},' % (b"x" * 700, i)
                    yield (b'{"p":"end"}],"artifact_root_id":"r","campaign":{"campaign_id":"c"},'
                           b'"cycle":{"state":"completed"},"producer":{"producer_id":"p"}}\n')
                self.parts = pieces()
                self.rest = b""

            def read(self, count):
                while len(self.rest) < count:
                    try:
                        self.rest += next(self.parts)
                    except StopIteration:
                        break
                out, self.rest = self.rest[:count], self.rest[count:]
                return out
        found, peak = peak_of(lambda: M.scan_top_fields(Endless(), ("cycle", "producer", "artifact_root_id")))
        self.assertEqual(found, {"artifact_root_id": "r", "cycle": {"state": "completed"}, "producer": {"producer_id": "p"}})
        self.assertLess(peak, SMALL)


# ---------------------------------------------------------------------------------------------------
# 5: the shortcut reads the manifest's bytes, not the record's copy of them
# ---------------------------------------------------------------------------------------------------


class ShortcutReadsTheManifest(Sealed):
    def provisional(self):
        route, route_file = self.route(slug="prov", campaign_key="followups")
        child = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        campaign_id=self.campaign)
        self.write_output(child)
        P.finalize(self.root, cycle_id=child["cycle_id"], allow_open_route=True)
        return child

    def edit_record(self, cycle_id, **changes):
        record_path = P.cycle_record_path(self.root, cycle_id)
        record = json.loads(record_path.read_text())
        record.update(changes)
        record_path.write_text(json.dumps(record, indent=2))

    def launder(self, cycle_id):
        """A producer command that rewrites the record: its own digest is made new over what was edited."""
        OPS.cycle_mark(self.root, cycle_id, discard=True, reason="test")
        OPS.cycle_mark(self.root, cycle_id, clear=True)

    def whole_outcome(self):
        try:
            whole_read_rows(self.root, self.path)
            return "closable"
        except C.CampaignError as exc:
            return exc.code

    def test_a_state_edited_into_the_record_and_laundered_is_not_believed(self):
        child = self.provisional()
        cycle_id = child["cycle_id"]
        self.assertEqual(self.refusal()["reason"], "campaign-cycle-provisional-active")
        self.edit_record(cycle_id, cycle_state="completed")
        self.assertEqual(self.refusal()["reason"], "campaign-cycle-provisional-active")  # its digest is stale
        self.launder(cycle_id)
        record = json.loads(P.cycle_record_path(self.root, cycle_id).read_text())
        self.assertEqual(record["cycle_state"], "completed")
        self.assertEqual(record["control_record_digest"], P._record_content_digest(record))  # the edit is now "as written"
        self.assertEqual(self.refusal()["reason"], "campaign-cycle-provisional-active")
        self.assertEqual(self.whole_outcome(), "campaign-cycle-provisional-active")
        with self.assertRaises(C.CampaignError):
            C.close(self.root, self.path, reason="x")

    def test_a_producer_edited_into_the_record_and_laundered_is_not_believed(self):
        cycle_id = self.first["cycle_id"]
        self.edit_record(cycle_id, producer_id="prod_" + "0" * 32)
        self.assertEqual(self.refusal()["reason"], "campaign-seal-mismatch")
        self.launder(cycle_id)
        self.assertEqual(self.refusal()["reason"], "campaign-seal-mismatch")
        self.assertEqual(self.whole_outcome(), "campaign-seal-mismatch")

    def test_a_cycle_a_person_abandoned_is_the_same_row_both_ways(self):
        route, route_file = self.route(slug="to-abandon", campaign_key="followups")
        member = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                         campaign_id=self.campaign)
        self.write_output(member)
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=member["cycle_id"], state="abandoned", abandon_reason="operator-decision",
                   leave_outside=True)
        record = P.read_cycle_record(self.root, member["cycle_id"])
        self.assertEqual((record["state"], record["cycle_state"]), ("sealed", "abandoned"))
        fast = {row["cycle_id"]: row for row in C._snapshot(self.root, self.path)["cycles"]}
        self.assertEqual(fast, whole_read_rows(self.root, self.path))
        self.assertEqual(fast[member["cycle_id"]]["state"], "abandoned")

    def test_a_lone_surrogate_in_the_record_is_read_the_long_way_not_raised(self):
        record_path = P.cycle_record_path(self.root, self.first["cycle_id"])
        text = record_path.read_text().rstrip()
        record_path.write_text(text[:-1] + ', "note": "\\ud800"}\n')
        self.assertEqual(outcome(lambda: C.status(self.root, self.path)), "closable")
        self.assertEqual(self.whole_outcome(), "closable")
        self.assertEqual(C.close(self.root, self.path, reason="x")["status"], "satisfied")

    def test_bytes_sealed_under_a_rule_since_tightened_are_not_judged_again_and_changed_bytes_are(self):
        # The rule, on purpose: a document that is still the sealed bytes is not refused by a `validate()`
        # tightened since (no person can mend a file that must not change); once the bytes differ, every rule
        # of today applies.
        rejects = M.ValidationReport(ok=False, violations=(M.Violation("new-rule", "$", "x"),))
        with mock.patch.object(M, "validate", return_value=rejects):
            self.assertEqual(outcome(lambda: C.status(self.root, self.path)), "closable")
            manifest_path = Path(self.first["cycle_dir"]) / "manifest.json"
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
            self.assertEqual(outcome(lambda: C.status(self.root, self.path)), "campaign-manifest-invalid")

    def test_a_row_the_index_holds_twice_is_not_taken_as_recorded(self):
        import artifact_admission as A
        record = P.read_cycle_record(self.root, self.first["cycle_id"])
        manifest_path = Path(self.first["cycle_dir"]) / "manifest.json"
        index = A.load_index(self.root)
        root_id = LC.read_root_identity(self.root)
        got = C._sealed_as_recorded(self.root, manifest_path, self.first["cycle_id"], record, self.campaign,
                                    C._IndexView(index), root_id)
        self.assertIsNotNone(got)
        row = index.manifests[self.first["cycle_id"]]
        twice = dataclasses.replace(index, manifests=dict(index.manifests, other_key=dict(row)))
        self.assertIsNone(C._sealed_as_recorded(self.root, manifest_path, self.first["cycle_id"], record, self.campaign,
                                                C._IndexView(twice), root_id))


# ---------------------------------------------------------------------------------------------------
# 6: a root whose path changed, and what a capture keeps
# ---------------------------------------------------------------------------------------------------


class RenamedRoot(BigFixture):
    """A closed campaign whose root now has another path: the evidence a close is bound to by its identity is
    hashed as a stream and shows its identity from the same pass, whatever its size."""

    def setUp(self):
        super().setUp()
        self.assertEqual(C.close(self.root, self.path, reason="r")["status"], "satisfied")
        self.wait_for_entrance()
        self.moved = self.root.with_name(self.root.name + "-moved")
        os.rename(self.root, self.moved)
        self.addCleanup(lambda: os.rename(self.moved, self.root) if self.moved.exists() else None)
        self.new_path = self.moved / self.path.relative_to(self.root)

    def test_status_and_export_of_a_closed_campaign_do_not_hold_its_manifests(self):
        status, peak = peak_of(lambda: C.status(self.moved, self.new_path))
        self.assertEqual(status["state"], "satisfied", status)
        self.assertLess(peak, SMALL)
        export, peak = peak_of(lambda: C.export_current(self.moved))
        self.assertEqual((export["status"], export["reason"]), ("valid", None), export)
        self.assertLess(peak, SMALL)
        rows = [row for row in export["campaigns"] if row["campaign_id"] == self.campaign]
        self.assertEqual(rows[0]["state"], "satisfied")
        self.assertTrue(any(item.get("bytes", 0) > 2 * SMALL for item in rows[0]["inputs"]), rows[0]["inputs"])

    def evidence(self):
        return LC.manifest_snapshot_dir(self.moved, self.big["cycle_id"])

    def test_evidence_that_is_not_what_was_closed_is_still_tampered(self):
        kept = sorted(self.evidence().glob("*.json"))
        self.assertTrue(kept)
        target = max(kept, key=lambda path: path.stat().st_size)
        raw = target.read_bytes()
        target.write_bytes(raw.replace(self.campaign.encode(), ("camp_" + "d" * 32).encode()))
        for reader in (lambda: C.status(self.moved, self.new_path), lambda: C.export_current(self.moved)):
            try:
                result = reader()
                reason = (result.get("campaigns") or [{}])[0].get("reason") or (result.get("close_refusal") or {}).get("reason")
            except C.CampaignError as exc:
                reason = exc.detail
            self.assertIn("tampered", str(reason))

    def test_evidence_too_large_to_read_is_not_called_tampered(self):
        # A copy that is not in the compact form is parsed, under the one limit: past it, the answer says so.
        target = max(self.evidence().glob("*.json"), key=lambda path: path.stat().st_size)
        target.write_bytes(json.dumps(json.loads(target.read_bytes()), indent=1).encode())
        with mock.patch.object(M, "MAX_MANIFEST_BYTES", 1024):
            with self.assertRaises(C.CampaignError) as caught:
                C.campaign_state(self.moved, self.new_path)
        self.assertEqual(caught.exception.context["detail"], "rename-evidence-kind-or-size")


class CaptureKeepsHashesOfBigFiles(Sealed):
    def test_a_head_capture_keeps_a_digest_and_not_the_bytes_and_still_catches_a_change(self):
        reads = C.CampaignReads(self.root)
        target = Path(self.first["cycle_dir"]) / "manifest.json"
        raw = target.read_bytes()
        digest, head = reads.read_head(self.root, target, ("cycle",))
        self.assertEqual(digest, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(head["cycle"]["cycle_id"], self.first["cycle_id"])
        info = reads.files[target.relative_to(self.root).as_posix()]
        self.assertIsNone(info["raw"])
        row = next(item for item in reads.inputs() if item["path"].endswith("manifest.json"))
        self.assertEqual((row["sha256"], row["bytes"]), (digest, len(raw)))
        self.assertEqual(reads.check_mutation(), [])
        target.write_bytes(raw.replace(b"a", b"b", 1))
        self.assertIn(target.relative_to(self.root).as_posix(), reads.check_mutation())

    def test_the_first_read_s_limit_does_not_decide_a_later_read_that_allows_more(self):
        reads = C.CampaignReads(self.root)
        with self.assertRaises(C.CampaignError):
            reads.read_raw(self.root, self.path, limit=16)
        self.assertTrue(reads.read_raw(self.root, self.path, limit=1 << 20))
        self.assertTrue(reads.read_raw(self.root, self.path, limit=1 << 20))
        with self.assertRaises(C.CampaignError):  # and a smaller one is still refused what was read
            reads.read_raw(self.root, self.path, limit=16)


# ---------------------------------------------------------------------------------------------------
# 7: close, reconcile and the locator do not parse a manifest whose bytes are the recorded ones
# ---------------------------------------------------------------------------------------------------


class CloseDoesNotParseSealedBytes(BigFixture):
    def test_reconcile_and_the_locator_update_hold_a_chunk(self):
        result, peak = peak_of(lambda: P.reconcile_root(self.root))
        self.assertNotEqual(result["status"], "skipped", result)
        self.assertLess(peak, SMALL)
        update, peak = peak_of(lambda: LOC.prepare_index_update(self.root, [self.campaign]))
        self.assertLess(peak, SMALL)
        _row, peak = peak_of(lambda: LOC.scan_campaign(self.root, self.path.parent))
        self.assertLess(peak, SMALL)

    def test_a_manifest_that_is_not_the_recorded_bytes_is_still_read_for_its_cycle(self):
        # The identity check between the folder's binding and the manifest still holds: bytes that changed are parsed.
        raw = self.manifest.read_bytes()
        other = "cyc_" + "f" * 32
        self.manifest.write_bytes(raw.replace(self.big["cycle_id"].encode(), other.encode()))
        with self.assertRaises(LOC.LocatorError) as caught:
            LOC.scan_campaign(self.root, self.path.parent)
        self.assertEqual(caught.exception.code, "locator-cycle-binding-id-mismatch")

    def test_a_changed_manifest_is_still_logged_as_a_change(self):
        record = P.read_cycle_record(self.root, self.big["cycle_id"])
        raw = self.manifest.read_bytes()
        self.assertIn(b'"title":"', raw)
        self.manifest.write_bytes(raw.replace(b'"title":"', b'"title":"edited ', 1))  # same size class, other content
        P.reconcile_root(self.root)
        after = P.read_cycle_record(self.root, self.big["cycle_id"])
        self.assertNotEqual(after.get("control_observations"), record.get("control_observations"))
        self.assertTrue(any(line.get("reason") == "filesystem-change"
                            for line in after.get("history_pending") or []) or after.get("control_observations"))


# ---------------------------------------------------------------------------------------------------
# 8: a move looks at the documents it will write before it moves anything
# ---------------------------------------------------------------------------------------------------


def tree_state(root):
    """Every file under `root` (path, mode, bytes) except what a lock leaves, to compare a disk before and after."""
    state = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            state[path.relative_to(root).as_posix()] = ("link", os.readlink(path))
        elif path.is_file() and path.name not in {".lock", "producer.lock"} and "lock" not in path.parent.name:
            state[path.relative_to(root).as_posix()] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir():
            state[path.relative_to(root).as_posix() + "/"] = ()
    return state


class MoveChecksFirst(Sealed):
    def other(self):
        return self.sealed_in("elsewhere", key="elsewhere-campaign")

    def manifest(self):
        return Path(self.first["cycle_dir"]) / "manifest.json"

    def test_a_cycle_whose_manifest_a_person_re_saved_moves_whole(self):
        other = self.other()
        path = self.manifest()
        path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2))
        moved = P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(moved["status"], "moved", moved)
        record = P.read_cycle_record(self.root, self.first["cycle_id"])
        self.assertEqual(record["campaign_id"], other["campaign_id"])
        document = json.loads((Path(moved["cycle_dir"]) / "manifest.json").read_bytes())
        self.assertEqual(document["cycle"]["campaign_id"], other["campaign_id"])
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")
        self.assertEqual(P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])["status"], "unchanged")

    def test_a_manifest_with_a_key_added_and_no_kept_copy_is_refused_before_anything_moves(self):
        other = self.other()
        path = self.manifest()
        document = json.loads(path.read_bytes())
        path.write_text(json.dumps(dict(document, edited_by_hand=True)))
        shutil.rmtree(LC.manifest_snapshot_dir(self.root, self.first["cycle_id"]))
        before = tree_state(self.root)
        with self.assertRaises(P.ProducerError) as caught:
            P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(caught.exception.code, "manifest-invalid")
        detail = caught.exception.detail
        self.assertIn("nothing was moved", detail)
        self.assertIn("next=put manifest.json back as it was sealed", detail)
        self.assertIn("unknown-key", detail)
        self.assertIn(Path(self.first["cycle_dir"]).relative_to(self.root).as_posix(), detail)
        self.assertEqual(tree_state(self.root), before)

    def test_a_kept_copy_that_holds_another_document_is_refused_before_anything_moves(self):
        other = self.other()
        path = self.manifest()
        path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2))  # same document
        kept = sorted(LC.manifest_snapshot_dir(self.root, self.first["cycle_id"]).glob("*.json"))[-1]
        document = json.loads(kept.read_bytes())
        document["campaign"] = dict(document["campaign"], title="not what was sealed")
        kept.write_bytes(M.canonical_bytes(document))
        before = tree_state(self.root)
        with self.assertRaises(P.ProducerError) as caught:
            P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(caught.exception.code, "manifest-snapshot-conflict")
        self.assertIn("next=put manifest.json back as that kept copy holds it", caught.exception.detail)
        self.assertEqual(tree_state(self.root), before)

    def test_a_new_parent_is_checked_the_same_way(self):
        other = self.other()
        path = self.manifest()
        document = json.loads(path.read_bytes())
        path.write_text(json.dumps(dict(document, edited_by_hand=True)))
        shutil.rmtree(LC.manifest_snapshot_dir(self.root, self.first["cycle_id"]))
        before = tree_state(self.root)
        with self.assertRaises(P.ProducerError):
            P.cycle_move(self.root, self.first["cycle_id"], parent=other["cycle_id"])
        self.assertEqual(tree_state(self.root), before)

    def test_a_stopped_move_is_finished_by_the_next_look(self):
        other = self.other()
        with mock.patch.object(OPS, "_publish_document_locked", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(P.read_cycle_record(self.root, self.first["cycle_id"])["campaign_id"], self.campaign)
        self.assertEqual(P.reconcile_root(self.root)["status"], "reconciled")
        self.assertEqual(P.read_cycle_record(self.root, self.first["cycle_id"])["campaign_id"], other["campaign_id"])
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")

    def test_no_lifecycle_exception_leaves_the_move(self):
        other = self.other()
        # A copy kept for this revision that is a link: the keeping refuses, as a refusal.
        real = LC.preserve_manifest_snapshot

        def refuse(root, cycle_id, raw):
            raise LC.LifecycleError("manifest-snapshot-invalid", "unreadable manifest document")
        with mock.patch.object(LC, "preserve_manifest_snapshot", side_effect=refuse):
            with self.assertRaises((P.ProducerError, C.CampaignError)):
                P.cycle_move(self.root, self.first["cycle_id"], campaign=other["campaign_id"])
        self.assertIs(real, LC.preserve_manifest_snapshot)


class MemberMove(Q.RequestFolderTest):
    """The same for the members of a request folder, which move with their frame."""

    def member_manifest(self, frame, record):
        source = P.cycle_dir(self.root, frame["campaign_id"], frame["cycle_id"])
        return source, source / "_internal/executions" / record["cycle_id"] / "manifest.json"

    def test_a_member_re_saved_by_a_person_moves_with_its_frame(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        _source, member = self.member_manifest(frame, record)
        member.write_text(json.dumps(json.loads(member.read_bytes()), indent=2))
        moved = P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"])
        self.assertEqual(moved["status"], "moved", moved)
        for cycle_id in (frame["cycle_id"], record["cycle_id"]):
            self.assertEqual(P.read_cycle_record(self.root, cycle_id)["campaign_id"], target["campaign_id"])
        self.assertIn(record["cycle_id"], P.read_campaign(self.root, target["campaign_id"])["cycles"])
        self.assertNotIn(record["cycle_id"], P.read_campaign(self.root, frame["campaign_id"])["cycles"])
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")

    def test_a_member_with_a_key_added_and_no_kept_copy_stops_the_move_before_the_rename(self):
        frame, record = self.completed_request()
        target = self.target_campaign()
        source, member = self.member_manifest(frame, record)
        document = json.loads(member.read_bytes())
        member.write_text(json.dumps(dict(document, edited_by_hand=True)))
        shutil.rmtree(S.L.manifest_snapshot_dir(self.root, record["cycle_id"]))
        before = tree_state(self.root)
        with self.assertRaises(P.ProducerError) as caught:
            P.cycle_move(self.root, frame["cycle_id"], campaign=target["campaign_id"])
        self.assertEqual(caught.exception.code, "manifest-invalid")
        self.assertIn(member.parent.relative_to(self.root).as_posix(), caught.exception.detail)
        self.assertIn("nothing was moved", caught.exception.detail)
        self.assertTrue(source.is_dir())
        self.assertEqual(tree_state(self.root), before)


if __name__ == "__main__":
    unittest.main()
