#!/usr/bin/env python3
"""A closed cycle of a closed campaign moves to another campaign: the folder leaves the old campaign
only after the evidence its close reads is kept, and a stopped or hand-made move is finished by the
next look.  Isolated fixture roots only."""
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import artifact_campaign as C
import artifact_producer as P
import artifact_producer_cycle_ops as OPS
import artifact_locator as LOC
import artifact_manifest as AM
import artifact_producer_history as H

spec = importlib.util.spec_from_file_location("closed_move_fixture", Path(__file__).with_name("artifact_producer.test.py"))
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


class Stop(Exception):
    """The run stopped here (the seam is an existing function of the move)."""


# How the old campaign's close reads its evidence.
CLOSES = ("current-v2", "renamed-root-v2", "v2-without-repository-id", "v1")
# How the move happened or stopped.
WAYS = ("normal", "stopped-after-rename", "stopped-after-rename-no-copy", "reported-state", "hand-moved")
EVIDENCE_CLOSES = CLOSES[1:]


class ClosedCampaignFixture(F.ProducerTestBase):
    def setUp(self):
        inherited = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, inherited)
        super().setUp()
        self.activate()

    # -- fixture ---------------------------------------------------------------------------
    def finished(self, key, slug):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(begun, "plans/cycle/REPORT.md", b"# Result\noriginal\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        return begun

    def _v1_close(self, path):
        record = json.loads(path.read_text())
        snapshot = C._snapshot(self.root, path)
        snapshot.pop("repository_id", None)  # actual v1 snapshots predate repository_id
        harness, session = "codex", "legacy-session"
        actor_id = "native-user:" + hashlib.sha256((harness + ":" + session).encode()).hexdigest()
        approval = {"harness": harness, "session_id": session, "actor_id": actor_id,
                    "statement": "campaign-" + "satisfy " + record["campaign_id"] + " " + C.digest(snapshot),
                    "decision": "accepted", "native_message_digest": "sha256:" + "a" * 64}
        first = snapshot["cycles"][0]
        event = {"stream_id": "strm_" + "1" * 32, "stream_sequence": 1,
                 "event_type": "campaign.satisfied", "target_id": record["campaign_id"],
                 "actor": {"kind": "user", "id": actor_id}, "recorded_at": "2026-09-26T00:00:00Z",
                 "provenance": {"source_manifest_id": first["manifest_id"],
                                "source_revision_id": first["manifest_revision_id"],
                                "producer_route_id": first["route_id"], "schema_version": 1,
                                "algorithm_version": C.CONTRACT_V1, "source_digest": C.digest(snapshot)},
                 "evidence_ids": [],
                 "payload": {"contract": C.CONTRACT_V1, "root": str(self.root),
                             "snapshot": snapshot, "approval": approval}}
        event["event_id"] = C._id("evt", event)
        (path.parent / C.LEGACY_EVENT_NAME).write_bytes(C.canonical(event) + b"\n")

    def build(self, close, cycles=1):
        """Campaign A holds `cycles` closed cycles and is satisfied; campaign B is open."""
        self.cycle = self.finished("source-key", "moved")
        self.second = self.finished("source-key", "moved-too") if cycles > 1 else None
        self.other = self.finished("destination-key", "stays")
        self.cycle_id = self.cycle["cycle_id"]
        self.cycle_ids = sorted(item["cycle_id"] for item in (self.cycle, self.second) if item)
        self.old_id, self.new_id = self.cycle["campaign_id"], self.other["campaign_id"]
        old_path = Path(self.cycle["cycle_dir"]).parent / "campaign.json"
        if close == "v1":
            self._v1_close(old_path)
        elif close == "v2-without-repository-id":
            real = C._snapshot

            def without_repository(*args, **kwargs):
                snapshot = real(*args, **kwargs)
                snapshot.pop("repository_id", None)
                return snapshot
            with mock.patch.object(C, "_snapshot", side_effect=without_repository):
                C.close(self.root, old_path, reason="done")
        else:
            C.close(self.root, old_path, reason="done")
        if close == "renamed-root-v2":
            target = Path(self._tmp.name) / "renamed-root"
            shutil.move(str(self.root), str(target))
            self.root = target
        self.old_path = self.campaign_json(self.old_id)
        self.closing_rows = self.close_rows()
        # A cycle closed before manifest copies existed has none: the folder's manifest is all there is.
        self.drop_copies()

    def campaign_json(self, campaign_id):
        directory = LOC.find_path_by_id(self.root, campaign_id, heal=False)
        return directory / "campaign.json"

    def close_rows(self):
        folded = C.campaign_state(self.root, self.old_path)
        self.assertEqual(folded.state, "satisfied")
        if (self.old_path.parent / C.LEGACY_EVENT_NAME).exists():
            event = json.loads((self.old_path.parent / C.LEGACY_EVENT_NAME).read_text())
        else:
            event = json.loads(sorted((self.old_path.parent / C.EVENTS_DIR).glob("*.json"))[0].read_text())
        return {row["cycle_id"]: row for row in event["payload"]["snapshot"]["cycles"]}

    def copies(self, cycle_id=None):
        return self.root / P.artifact_lifecycle.MANIFEST_SNAPSHOT_REL / (cycle_id or self.cycle_id)

    def drop_copies(self, cycle_id=None):
        for each in ([cycle_id] if cycle_id else (getattr(self, "cycle_ids", None) or [self.cycle_id])):
            shutil.rmtree(self.copies(each), ignore_errors=True)

    def folder(self, cycle_id=None):
        cycle_id = cycle_id or self.cycle_id
        record = P.read_cycle_record(self.root, cycle_id)
        return P.cycle_dir(self.root, record["campaign_id"], cycle_id, record)

    # -- the ways a move happens -----------------------------------------------------------
    def move(self, way, cycle_id=None, folder=None):
        """`folder`: where the cycle sits now, read before the first cycle left (a campaign that two
        cycles have left cannot be read to find the second)."""
        cycle_id = cycle_id or self.cycle_id
        if way == "normal":
            return P.cycle_move(self.root, cycle_id, campaign=self.new_id)
        if way in ("stopped-after-rename", "stopped-after-rename-no-copy"):
            with mock.patch.object(OPS, "_adopt_location_locked", side_effect=Stop):
                with self.assertRaises(Stop):
                    P.cycle_move(self.root, cycle_id, campaign=self.new_id)
            if way.endswith("no-copy"):
                self.drop_copies(cycle_id)  # what an older run left: the folder moved and nothing kept
            return None
        old = folder or self.folder(cycle_id)
        moved = self.campaign_json(self.new_id).parent / old.name
        shutil.move(str(old), str(moved))
        if way == "reported-state":
            # the binding already names the new campaign; record, manifest and old list still the old
            (moved / ".cycle.json").write_bytes(LOC.cycle_binding_bytes(
                self.new_id, cycle_id, started_on=P.read_cycle_record(self.root, cycle_id)["started_on"]))
            self.drop_copies(cycle_id)
        return None

    def settle(self, way):
        """The next look: a listing, then (where a listing does not reach it) the writers' reconcile."""
        if way in ("normal",):
            return
        P.list_campaign_summaries(self.root)
        result = P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)
        self.assertNotEqual(result.get("status"), "skipped", result)

    def reported_state(self, close, hand_copy):
        self.build(close)
        old = self.folder()
        moved = self.campaign_json(self.new_id).parent / old.name
        shutil.move(str(old), str(moved))
        (moved / ".cycle.json").write_bytes(LOC.cycle_binding_bytes(
            self.new_id, self.cycle_id, started_on=P.read_cycle_record(self.root, self.cycle_id)["started_on"]))
        if hand_copy:  # what the person on site did to read the old campaign again
            P.artifact_lifecycle.preserve_manifest_snapshot(
                self.root, self.cycle_id, (moved / "manifest.json").read_bytes())
        self.assertEqual(P.read_cycle_record(self.root, self.cycle_id)["campaign_id"], self.old_id)

    # -- what must be true afterwards -------------------------------------------------------
    def assert_cycle_moved(self, cycle_id):
        """The cycle's own records, lists, manifest and indexes all say it is under the new campaign."""
        record = P.read_cycle_record(self.root, cycle_id)
        self.assertEqual(record["campaign_id"], self.new_id)
        folder = self.folder(cycle_id)
        self.assertEqual(folder.parent, self.campaign_json(self.new_id).parent)
        binding = LOC.read_cycle_binding(folder)
        self.assertEqual((binding["campaign_id"], binding["cycle_id"]), (self.new_id, cycle_id))
        self.assertNotIn(cycle_id, json.loads(self.campaign_json(self.old_id).read_text())["cycles"])
        self.assertIn(cycle_id, json.loads(self.campaign_json(self.new_id).read_text())["cycles"])
        document = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(document["cycle"]["campaign_id"], self.new_id)
        self.assertEqual(document["campaign"]["campaign_id"], self.new_id)
        index = P.artifact_admission.load_index(self.root)
        self.assertEqual(index.cycles[cycle_id]["cycle_path"], str(folder.relative_to(self.root)))
        self.assertEqual(index.manifests[cycle_id]["manifest_digest"], record["manifest_digest"])
        self.assertEqual(LOC._load_index(self.root)[cycle_id], str(folder.relative_to(self.root)))

    def assert_moved(self, close, cycle_ids=None):
        cycle_ids = cycle_ids or getattr(self, "cycle_ids", None) or [self.cycle_id]
        for cycle_id in cycle_ids:
            self.assert_cycle_moved(cycle_id)
        # the old campaign is a valid satisfied one, with no exception on the way
        folded = C.campaign_state(self.root, self.campaign_json(self.old_id))
        self.assertEqual(folded.state, "satisfied")
        listed = {row["campaign_id"]: row for row in P.list_campaign_summaries(self.root, active_only=False)}
        self.assertNotEqual(listed[self.old_id].get("state"), "invalid", listed[self.old_id])
        if close != "current-v2":
            for cycle_id in cycle_ids:
                row = self.closing_rows[cycle_id]
                kept = self.copies(cycle_id) / (row["manifest_revision_id"] + ".json")
                self.assertTrue(kept.is_file(), "the manifest the close names was not kept")
                self.assertEqual("sha256:" + hashlib.sha256(kept.read_bytes()).hexdigest(), row["manifest_digest"])

class ClosedCampaignMoveTest(ClosedCampaignFixture):
    def run_case(self, close, way):
        self.build(close)
        self.move(way)
        self.settle(way)
        self.assert_moved(close)


class ClosedCampaignOtherWaysTest(ClosedCampaignFixture):
    """The other ways a closed cycle's folder leaves a closed campaign (same evidence, same one place)."""

    def kept_copy_matches_close(self):
        row = self.closing_rows[self.cycle_id]
        kept = self.copies() / (row["manifest_revision_id"] + ".json")
        self.assertTrue(kept.is_file())
        self.assertEqual("sha256:" + hashlib.sha256(kept.read_bytes()).hexdigest(), row["manifest_digest"])

    def delete_case(self, close):
        self.build(close)
        self.assertEqual(P.delete_cycle(self.root, self.cycle_id)["status"], "deleted")
        self.assertEqual(C.campaign_state(self.root, self.old_path).state, "satisfied")
        self.kept_copy_matches_close()

    def hand_delete_case(self, close):
        """A folder removed by hand is taken for gone; where a copy of its manifest was kept the campaign stays valid."""
        self.build(close)
        folder = self.folder()
        P.artifact_lifecycle.preserve_manifest_snapshot(
            self.root, self.cycle_id, (folder / "manifest.json").read_bytes())
        shutil.rmtree(folder)
        result = P.reconcile_root(self.root)
        self.assertEqual(result.get("status"), "reconciled", result)
        self.assertTrue(P.read_cycle_record(self.root, self.cycle_id).get("deleted_at"))
        self.assertEqual(C.campaign_state(self.root, self.old_path).state, "satisfied")

    def test_evidence_is_not_taken_from_a_manifest_that_is_not_the_recorded_document(self):
        # a folder carrying other bytes than the record names is not kept as the close's evidence
        self.build("v2-without-repository-id")
        folder = self.folder()
        document = json.loads((folder / "manifest.json").read_text())
        document["manifest_revision_id"] = "mrev_" + "9" * 32
        (folder / "manifest.json").write_text(json.dumps(document))
        record = P.read_cycle_record(self.root, self.cycle_id)
        P._preserve_current_manifest(self.root, record, folder, match_record=True)
        self.assertFalse(self.copies().exists() and any(self.copies().iterdir()))


class ClosedCampaignRerunTest(ClosedCampaignFixture):
    """The reported half-moved state (the folder under the new campaign, its binding the new one's, the
    record, manifest and lists the old one's), with the preserved copy put there by hand or not: the same
    `cycle-move` typed again finishes it, and so does any command that looks the root over."""

    def rerun_case(self, close, hand_copy):
        self.reported_state(close, hand_copy)
        # the look before the lock finishes the half-moved cycle: it is already where it was asked to be
        result = P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)
        self.assertEqual(result["status"], "unchanged", result)
        self.assert_moved(close)
        self.assertEqual(P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)["status"], "unchanged")

    def rerun_way_case(self, close, way):
        """The same `cycle-move` typed again after the older run stopped just after the rename (nothing
        kept), or after the folder was carried by hand."""
        self.build(close)
        self.move(way)
        result = P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)
        self.assertEqual(result["status"], "unchanged", result)
        self.assert_moved(close)
        self.assertEqual(P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)["status"], "unchanged")

    def command_case(self, close, hand_copy, command):
        self.reported_state(close, hand_copy)
        command(self)
        self.assert_moved(close)

    def third_campaign_case(self, close, hand_copy):
        third = self.finished("third-key", "third")
        self.reported_state(close, hand_copy)
        result = P.cycle_move(self.root, self.cycle_id, campaign=third["campaign_id"])
        self.assertEqual(result["status"], "moved", result)
        self.new_id = third["campaign_id"]
        self.assert_moved(close)
        self.assertNotIn(self.cycle_id, json.loads(self.campaign_json(self.other["campaign_id"]).read_text())["cycles"])


class ClosedCampaignTwoCyclesTest(ClosedCampaignFixture):
    """Two closed cycles of one closed campaign have left its folder: one look finishes both, whichever the
    order of their IDs, and the campaign is a valid satisfied one again."""

    def folders(self):
        return {cycle_id: self.folder(cycle_id) for cycle_id in self.cycle_ids}

    def reconcile_once(self):
        result = P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)
        self.assertEqual(result.get("status"), "reconciled", result)
        return result

    def hand_moved_case(self, close):
        self.build(close, cycles=2)
        for cycle_id, folder in self.folders().items():
            self.move("hand-moved", cycle_id, folder)
        self.reconcile_once()
        self.assert_moved(close)

    def stopped_and_hand_moved_case(self, close, stopped):
        self.build(close, cycles=2)
        left, by_hand = self.cycle_ids[stopped], self.cycle_ids[1 - stopped]
        folders = self.folders()
        self.move("stopped-after-rename-no-copy", left)
        self.move("hand-moved", by_hand, folders[by_hand])
        self.reconcile_once()
        self.assert_moved(close)

    def other_cycle_while_one_is_half_moved_case(self, close, half):
        """A cycle of the same campaign is moved while another is left half-moved: the half-moved one is
        finished first, and the cycle asked for goes on."""
        self.build(close, cycles=2)
        left, asked = self.cycle_ids[half], self.cycle_ids[1 - half]
        self.move("reported-state", left, self.folders()[left])
        result = P.cycle_move(self.root, asked, campaign=self.new_id)
        self.assertEqual(result["status"], "moved", result)
        self.assert_moved(close)

    def same_cycle_move_case(self, close, which):
        self.build(close, cycles=2)
        for cycle_id, folder in self.folders().items():
            self.move("hand-moved", cycle_id, folder)
        # the cycle typed is accepted where it is, and so is the other one that left the same campaign
        result = P.cycle_move(self.root, self.cycle_ids[which], campaign=self.new_id)
        self.assertEqual(result["status"], "unchanged", result)
        self.assert_moved(close)
        self.assertEqual(P.cycle_move(self.root, self.cycle_ids[which], campaign=self.new_id)["status"], "unchanged")


class ClosedCampaignOneReadTest(ClosedCampaignFixture):
    """A closed cycle moved once is read once and hashed once (the admission lock is held through it)."""

    def work(self, close):
        self.build(close)
        old = json.loads((self.folder() / "manifest.json").read_text())["manifest_revision_id"]
        reads, hashed = [], []

        def counting_read(real):
            def read(directory):
                reads.append(str(directory))
                return real(directory)
            return read
        real_digest = AM.manifest_digest

        def counting_digest(document):
            hashed.append(document.get("manifest_revision_id"))
            return real_digest(document)
        with mock.patch.object(OPS, "_read_manifest_raw", counting_read(OPS._read_manifest_raw)), \
                mock.patch.object(H, "_read_manifest_raw", counting_read(H._read_manifest_raw)), \
                mock.patch.object(AM, "manifest_digest", counting_digest):
            self.assertEqual(P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)["status"], "moved")
        self.assert_moved(close)
        return len(reads), hashed.count(old)

    def test_one_read_one_hash_when_the_evidence_is_kept(self):
        self.assertEqual(self.work("v1"), (1, 1))

    def test_one_read_one_hash_when_nothing_needs_keeping(self):
        self.assertEqual(self.work("current-v2"), (1, 1))


class ClosedCampaignCommandsFinishTest(ClosedCampaignFixture):
    """`campaign-close` and `campaign-reopen` look the root over first, so they finish a half-moved cycle."""

    def test_move_refuses_as_before_when_the_look_cannot_finish(self):
        self.reported_state("v1", False)
        with mock.patch.object(OPS, "reconcile_root", return_value={"status": "skipped", "reason": "CampaignError"}):
            with self.assertRaises((P.ProducerError, LOC.LocatorError)):
                P.cycle_move(self.root, self.cycle_id, campaign=self.new_id)
        self.assertEqual(P.read_cycle_record(self.root, self.cycle_id)["campaign_id"], self.old_id)  # nothing was changed
        under_new = [LOC.read_cycle_binding(child) for child in self.campaign_json(self.new_id).parent.iterdir()
                     if child.is_dir()]
        self.assertIn(self.cycle_id, [binding["cycle_id"] for binding in under_new if binding])  # the folder stayed

    def test_campaign_close_finishes_the_half_moved_cycle(self):
        self.reported_state("v1", False)
        C.close(self.root, self.new_id, reason="done")
        self.assert_moved("v1")
        self.assertEqual(C.campaign_state(self.root, self.campaign_json(self.new_id)).state, "satisfied")

    def test_campaign_reopen_finishes_the_half_moved_cycle(self):
        self.reported_state("v1", False)
        C.reopen(self.root, self.old_id, reason="again")  # by ID, as the command takes it: the old campaign cannot be located yet
        self.assert_cycle_moved(self.cycle_id)
        self.assertEqual(C.campaign_state(self.root, self.campaign_json(self.old_id)).state, "active")


def _recover(test):
    P.recover(test.root)


def _begin(test):
    route, route_file = test.route(slug="after", campaign_key="destination-key")
    P.begin(test.root, route_file=route_file, capability="autopilot-code", intensity="direct")


def _case(close, way):
    def test(self):
        self.run_case(close, way)
    return test


for _close in EVIDENCE_CLOSES:
    setattr(ClosedCampaignOtherWaysTest, "test_cycle_delete__%s" % _close.replace("-", "_"),
            (lambda close: lambda self: self.delete_case(close))(_close))
    setattr(ClosedCampaignOtherWaysTest, "test_hand_deleted_with_copy__%s" % _close.replace("-", "_"),
            (lambda close: lambda self: self.hand_delete_case(close))(_close))

for _close in EVIDENCE_CLOSES:
    for _copy in (False, True):
        _tag = "%s__%s" % (_close.replace("-", "_"), "hand_copy" if _copy else "no_copy")
        setattr(ClosedCampaignRerunTest, "test_same_cycle_move_again__" + _tag,
                (lambda c, h: lambda self: self.rerun_case(c, h))(_close, _copy))
        setattr(ClosedCampaignRerunTest, "test_recover__" + _tag,
                (lambda c, h: lambda self: self.command_case(c, h, _recover))(_close, _copy))
        setattr(ClosedCampaignRerunTest, "test_begin__" + _tag,
                (lambda c, h: lambda self: self.command_case(c, h, _begin))(_close, _copy))
        setattr(ClosedCampaignRerunTest, "test_other_target_after_adopting__" + _tag,
                (lambda c, h: lambda self: self.third_campaign_case(c, h))(_close, _copy))

for _close in EVIDENCE_CLOSES:
    _name = _close.replace("-", "_")
    setattr(ClosedCampaignTwoCyclesTest, "test_two_hand_moved__" + _name,
            (lambda c: lambda self: self.hand_moved_case(c))(_close))
    for _stopped in (0, 1):
        setattr(ClosedCampaignTwoCyclesTest, "test_one_stopped_one_hand_moved__%s__stopped_%d" % (_name, _stopped),
                (lambda c, i: lambda self: self.stopped_and_hand_moved_case(c, i))(_close, _stopped))
        setattr(ClosedCampaignTwoCyclesTest, "test_other_cycle_while_one_is_half_moved__%s__half_%d" % (_name, _stopped),
                (lambda c, i: lambda self: self.other_cycle_while_one_is_half_moved_case(c, i))(_close, _stopped))
        setattr(ClosedCampaignTwoCyclesTest, "test_same_cycle_move_again__%s__id_%d" % (_name, _stopped),
                (lambda c, i: lambda self: self.same_cycle_move_case(c, i))(_close, _stopped))
    for _way in ("stopped-after-rename-no-copy", "hand-moved"):
        setattr(ClosedCampaignRerunTest, "test_same_cycle_move_again__%s__%s" % (_name, _way.replace("-", "_")),
                (lambda c, w: lambda self: self.rerun_way_case(c, w))(_close, _way))

for _close in CLOSES:
    for _way in WAYS:
        setattr(ClosedCampaignMoveTest, "test_%s__%s" % (_close.replace("-", "_"), _way.replace("-", "_")),
                _case(_close, _way))


if __name__ == "__main__":
    unittest.main()
