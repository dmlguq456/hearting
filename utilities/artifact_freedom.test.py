#!/usr/bin/env python3
"""Ordinary filesystem cleanup followed by actual producer/reader work.

All artifacts, registries, configuration and session state are temporary.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_history as history
import artifact_reader as reader

spec = importlib.util.spec_from_file_location("freedom_fixture", Path(__file__).with_name("artifact_producer.test.py"))
FX = importlib.util.module_from_spec(spec)
spec.loader.exec_module(FX)
P = FX.P


class ArtifactFreedomTest(FX.ProducerTestBase):
    def setUp(self):
        inherited = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("AGENT_")}
        self.addCleanup(os.environ.update, inherited)
        super().setUp()
        patcher = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(Path(self._tmp.name) / "config"),
            "XDG_STATE_HOME": str(Path(self._tmp.name) / "state"),
            "HEARTING_WORKFLOW_GROUP_REVIEW": "off",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.activate()

    def finished(self, key="freedom", slug="first"):
        route, route_file = self.route(slug=slug, campaign_key=key)
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(begun, "plans/cycle/REPORT.md", b"# Result\noriginal\n")
        self.close(route, route_file)
        P.finalize(self.root, cycle_id=begun["cycle_id"])
        return begun

    def events(self):
        return [json.loads(line) for path in (self.root / history.HISTORY_REL).glob("*/*.jsonl")
                for line in path.read_text().splitlines()]

    def next_work(self, parent):
        route, route_file = self.route(slug="next", campaign_key="freedom")
        begun = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct",
                        parent_cycle_id=parent["cycle_id"])
        self.write_output(begun)
        self.close(route, route_file)
        self.assertEqual(P.finalize(self.root, cycle_id=begun["cycle_id"])["status"], "sealed")
        return begun

    def test_reader_and_memory_read_a_cross_campaign_hand_move_before_listing(self):
        first = self.finished()
        other = self.finished("destination", "other")
        old = Path(first["cycle_dir"])
        moved = Path(other["cycle_dir"]).parent / old.name
        old.rename(moved)
        buckets = reader.bucket_dirs(self.root, "plans")
        self.assertIn(moved / "artifacts/plans", [path for path, _ in buckets])
        env = dict(os.environ, AGENT_ARTIFACT_ROOT=str(self.root))
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/memory/mem.py"), "curate-artifacts"]
        result = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        record_before = P.cycle_record_path(self.root, first["cycle_id"]).read_bytes()
        events_before = self.events()
        P.list_campaign_summaries(self.root)
        self.assertEqual(P.cycle_record_path(self.root, first["cycle_id"]).read_bytes(), record_before)
        self.assertEqual(self.events(), events_before)
        # The existing writer reconciliation owns move bookkeeping.
        P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["campaign_id"], other["campaign_id"])
        self.assertTrue(any(row["operation"] == "move" and row["target"]["id"] == first["cycle_id"]
                            for row in self.events()))

    def test_deleted_campaign_and_parent_allow_the_next_route(self):
        first = self.finished()
        shutil.rmtree(Path(first["cycle_dir"]).parent)
        next_cycle = self.next_work(first)
        self.assertNotEqual(next_cycle["campaign_id"], first["campaign_id"])
        self.assertIsNone(P.read_cycle_record(self.root, next_cycle["cycle_id"])["parent_cycle_id"])
        self.assertTrue(any(row["operation"] == "delete" and row["target"]["id"] == first["campaign_id"]
                            for row in self.events()))

    def test_entire_campaigns_directory_and_locator_cache_are_disposable(self):
        first = self.finished()
        shutil.rmtree(self.root / "campaigns")
        self.next_work(first)
        self.assertTrue(P.read_cycle_record(self.root, first["cycle_id"])["deleted_at"])
        self.assertTrue(any(row["operation"] == "delete" and row["target"]["id"] == first["cycle_id"]
                            for row in self.events()))

    def test_deleted_binding_does_not_block_refresh_or_reclose(self):
        first = self.finished()
        folder = Path(first["cycle_dir"])
        (folder / ".cycle.json").unlink()
        (folder / "artifacts/plans/cycle/REPORT.md").write_text("# Result\nedited\n")
        result = P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertTrue(result["refreshed"], result)
        self.assertFalse((folder / ".cycle.json").exists())
        self.assertTrue(any(row["kind"] == "artifact" and row["operation"] == "update" for row in self.events()))

    def test_deleted_binding_and_payload_can_then_be_moved_once(self):
        for cross_campaign in (False, True):
            with self.subTest(cross_campaign=cross_campaign):
                first = self.finished(slug=f"binding-move-{cross_campaign}")
                other = self.finished("destination", f"other-{cross_campaign}")
                old = Path(first["cycle_dir"])
                (old / ".cycle.json").unlink()
                (old / "artifacts/plans/cycle/REPORT.md").unlink()
                parent = Path(other["cycle_dir"]).parent if cross_campaign else old.parent
                moved = parent / f"renamed-{old.name}"
                old.rename(moved)
                record_before = P.cycle_record_path(self.root, first["cycle_id"]).read_bytes()
                P.list_campaign_summaries(self.root)
                self.assertEqual(P.cycle_record_path(self.root, first["cycle_id"]).read_bytes(), record_before)
                P.reconcile_root(self.root)
                P.deliver_pending_history(self.root)
                record = P.read_cycle_record(self.root, first["cycle_id"])
                self.assertEqual(record["locator"], moved.name)
                self.assertEqual(record["campaign_id"], other["campaign_id"] if cross_campaign else first["campaign_id"])
                moves = lambda: [row for row in self.events() if row["field"] == ("campaign" if cross_campaign else "path")
                                 and row["target"]["id"] == first["cycle_id"]]
                self.assertEqual(len(moves()), 1)
                P.list_campaign_summaries(self.root)
                P.reconcile_root(self.root)
                P.deliver_pending_history(self.root)
                self.assertEqual(len(moves()), 1)
                self.assertFalse((moved / "artifacts/plans/cycle/REPORT.md").exists())
                self.assertTrue(P.finalize(self.root, cycle_id=first["cycle_id"])["refreshed"])

    def control_events(self, first, name):
        return [row for row in self.events() if row["target"]["id"] == first["cycle_id"]
                and row.get("reason") == "filesystem-change" and row["target"]["path"].endswith(name)]

    def test_manifest_edit_is_logged_once_and_can_then_be_moved(self):
        first = self.finished()
        other = self.finished("destination", "other")
        path = Path(first["cycle_dir"]) / "manifest.json"
        document = json.loads(path.read_text())
        document["cycle"]["title"] = "edited freely"
        path.write_text(json.dumps(document))
        edited = path.read_bytes()
        P.checkpoint(self.root, cycle_id=first["cycle_id"], trigger="explicit")
        P.finalize(self.root, cycle_id=first["cycle_id"])
        P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(path.read_bytes(), edited)
        self.assertEqual(len(self.control_events(first, "manifest.json")), 1)
        result = P.cycle_move(self.root, first["cycle_id"], campaign=other["campaign_id"])
        self.assertEqual(result["status"], "moved")
        P.list_campaign_summaries(self.root)
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["cycle_state"], "completed")

    def test_deleted_runtime_record_uses_the_admitted_past_result_and_logs_each_deletion(self):
        first = self.finished()
        record_path = P.cycle_record_path(self.root, first["cycle_id"])
        for count in (1, 2):
            record_path.unlink()
            P.checkpoint(self.root, cycle_id=first["cycle_id"], trigger="explicit")
            self.assertEqual(P.finalize(self.root, cycle_id=first["cycle_id"])["cycle_state"], "completed")
            self.assertEqual(len(self.control_events(first, record_path.name)), count)
        self.next_work(first)

    def test_deleted_runtime_record_reads_the_index_head_not_the_full_index(self):
        first = self.finished()
        P.cycle_record_path(self.root, first["cycle_id"]).unlink()
        index = P.artifact_admission._index_path(self.root)
        time.sleep(0.15)  # past the index settle margin, so its head projection is current
        P.artifact_admission.load_index(self.root)  # an ordinary reader leaves the head
        P.artifact_admission.clear_index_cache()
        real = P._read_json
        read = []
        with mock.patch.object(P, "_read_json", side_effect=lambda path: read.append(Path(path)) or real(path)), \
                mock.patch.object(P.artifact_admission, "load_index", side_effect=AssertionError("full index read")):
            record = P.read_cycle_record(self.root, first["cycle_id"])
        self.assertEqual(record["cycle_id"], first["cycle_id"])
        self.assertEqual(record["manifest_digest"],
                         P.artifact_admission.load_index_head(self.root).manifests[first["cycle_id"]]["manifest_digest"])
        self.assertNotIn(index, read)

    def test_reconcile_skips_quiet_unchanged_cycles_and_still_sees_a_later_edit(self):
        cycles = [self.finished(), self.finished("second", "second")]
        records = {item["cycle_id"]: P.read_cycle_record(self.root, item["cycle_id"]) for item in cycles}
        earliest = min(records, key=lambda cid: (records[cid].get("started_on") or "", cid))
        later = next(item for item in cycles if item["cycle_id"] != earliest)
        later_record = P.cycle_record_path(self.root, later["cycle_id"])
        memo = P.producer_dir(self.root) / P.CONTROL_MEMO_NAME
        with mock.patch.object(P, "_CONTROL_MEMO_SETTLE_NS", -1):
            P.reconcile_root(self.root)
            self.assertIn(later["cycle_id"], json.loads(memo.read_text())["cycles"])
            real = P._read_json
            read = []
            with mock.patch.object(P, "_read_json", side_effect=lambda path: read.append(Path(path)) or real(path)):
                P.reconcile_root(self.root)
            self.assertNotIn(later_record, read)  # the quiet, unchanged cycle is not read again
            self.assertIn(P.cycle_record_path(self.root, earliest), read)  # the earliest also watches INDEX
            path = Path(later["cycle_dir"]) / "manifest.json"
            document = json.loads(path.read_text())
            document["cycle"]["title"] = "edited after a quiet look"
            path.write_text(json.dumps(document))
            P.reconcile_root(self.root)
            P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)
        self.assertEqual(len(self.control_events(later, "manifest.json")), 1)
        memo.write_text("not json")  # a broken memo only means every cycle is read
        P.reconcile_root(self.root)
        P.deliver_pending_history(self.root)
        self.assertEqual(len(self.control_events(later, "manifest.json")), 1)

    def test_history_delivery_does_not_reopen_records_the_look_just_saw_quiet(self):
        cycles = [self.finished(), self.finished("second", "second")]
        paths = {item["cycle_id"]: P.cycle_record_path(self.root, item["cycle_id"]) for item in cycles}
        real = Path.read_bytes
        opened = []

        def deliver():
            opened.clear()
            with mock.patch.object(Path, "read_bytes", autospec=True,
                                   side_effect=lambda path: opened.append(Path(path)) or real(path)):
                return P.deliver_pending_history(self.root)

        with mock.patch.object(P, "_CONTROL_MEMO_SETTLE_NS", -1):
            P.reconcile_root(self.root)
            vouched = json.loads((P.producer_dir(self.root) / P.HISTORY_QUIET_NAME).read_text())["records"]
            self.assertEqual(set(vouched), set(paths))
            deliver()
            self.assertFalse(set(paths.values()) & set(opened))
            P.reconcile_root(self.root)  # both quiet now: the memo answers
            deliver()
            self.assertFalse(set(paths.values()) & set(opened))
            # A record this process writes after the look is opened again.
            later = cycles[1]["cycle_id"]
            P._write_cycle_record(self.root, P.read_cycle_record(self.root, later), exclusive=False)
            deliver()
            self.assertIn(paths[later], opened)
            # A line another writer left before the next look is found by that look.
            P.reconcile_root(self.root)
            record = json.loads(paths[later].read_text())
            record["history_pending"] = [{"kind": "test"}]
            paths[later].write_text(json.dumps(record))
            P.reconcile_root(self.root)
            deliver()
            self.assertIn(paths[later], opened)
        # Without a look in this process every record is read, as before.
        P._QUIET_HISTORY.clear()
        deliver()
        self.assertTrue(set(paths.values()) <= set(opened))

    def test_quiet_records_are_read_once_per_identity_whoever_rewrites_the_memo(self):
        cycles = [self.finished(), self.finished("second", "second"), self.finished("third", "third")]
        paths = {P.cycle_record_path(self.root, item["cycle_id"]) for item in cycles}
        memo = P.producer_dir(self.root) / P.CONTROL_MEMO_NAME
        vouched = P.producer_dir(self.root) / P.HISTORY_QUIET_NAME
        real_bytes, real_json = Path.read_bytes, P._read_json
        opened = []

        def counted(action):
            opened.clear()
            with mock.patch.object(Path, "read_bytes", autospec=True,
                                   side_effect=lambda path: opened.append(Path(path)) or real_bytes(path)), \
                    mock.patch.object(P, "_read_json", side_effect=lambda path: opened.append(Path(path))
                                      or real_json(path)):
                action()
            return set(opened) & paths

        def next_start():
            P._QUIET_HISTORY.clear()  # a new process
            looked = counted(lambda: P.reconcile_root(self.root))
            return looked, counted(lambda: P.deliver_pending_history(self.root))

        with mock.patch.object(P, "_CONTROL_MEMO_SETTLE_NS", -1):
            P.reconcile_root(self.root)  # the memo knows every cycle now
            vouched.unlink()  # the first start after an install: nothing vouched yet
            looked, delivered = next_start()
            self.assertEqual(looked, paths)  # each quiet record is read once, for its identity
            self.assertFalse(delivered)
            looked, delivered = next_start()
            self.assertEqual(len(looked), 1)  # only the earliest cycle, which is always observed
            self.assertFalse(delivered)
            # An older release rewrites the memo whole; what vouches for the records is not in it.
            memo.write_text(json.dumps(json.loads(memo.read_text())))
            looked, delivered = next_start()
            self.assertEqual(len(looked), 1)
            self.assertFalse(delivered)
            # An observation older than the window answers nothing: every record is read.
            seen_at, quiet = P._QUIET_HISTORY[str(self.root.resolve())]
            P._QUIET_HISTORY[str(self.root.resolve())] = (seen_at - P._QUIET_HISTORY_SECONDS - 1, quiet)
            self.assertEqual(counted(lambda: P.deliver_pending_history(self.root)), paths)

    def test_begin_retry_looks_again_when_the_hand_change_look_was_skipped(self):
        # The look's own lock wait can run out (skipped); the cycle must not begin before it finished.
        first = self.finished()
        old = Path(first["cycle_dir"])
        old.rename(old.with_name("renamed-by-hand"))
        route, route_file = self.route(slug="after-hand-move", campaign_key="freedom")
        real_acquire, calls = P.artifact_admission._acquire_lock, []

        def acquire(*args, **kwargs):
            calls.append(1)
            if len(calls) <= 2:  # the look's lock, then the begin's lock
                raise P.artifact_admission.AdmissionBusy("busy")
            return real_acquire(*args, **kwargs)

        statuses, real_reconcile = [], P.reconcile_root

        def reconcile(root, **kwargs):
            result = real_reconcile(root, **kwargs)
            statuses.append(result["status"])
            return result

        with mock.patch.object(P.artifact_admission, "_acquire_lock", acquire), \
                mock.patch.object(P, "reconcile_root", reconcile):
            begun = P._begin_waiting_for_admission(self.root, route_file=route_file,
                                                   capability="autopilot-code", intensity="direct")
        self.assertEqual(begun["status"], "begun")
        self.assertEqual(statuses, ["skipped", "reconciled"])
        self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["locator"], "renamed-by-hand")
        self.assertEqual(P.reconcile_root(self.root)["status"], "unchanged")

    def test_deleted_generated_manifest_and_binding_are_logged_without_restoring_payload(self):
        first = self.finished()
        folder = Path(first["cycle_dir"])
        (folder / "manifest.json").unlink()
        (folder / ".cycle.json").unlink()
        P.finalize(self.root, cycle_id=first["cycle_id"])
        P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(len(self.control_events(first, "manifest.json")), 1)
        self.assertEqual(len(self.control_events(first, ".cycle.json")), 1)
        self.assertFalse((folder / "manifest.json").exists())
        self.assertFalse((folder / ".cycle.json").exists())
        self.next_work(first)

    def test_user_edit_after_an_interrupted_refresh_keeps_the_edit_and_needs_no_recovery(self):
        first = self.finished()
        folder = Path(first["cycle_dir"])
        (folder / "artifacts/plans/cycle/REPORT.md").write_text("updated payload\n")
        with self.assertRaises(P.artifact_admission.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=first["cycle_id"], crash_after_manifest=True)
        path = folder / "manifest.json"
        document = json.loads(path.read_text())
        document["cycle"]["title"] = "edited after interruption"
        path.write_text(json.dumps(document))
        edited = path.read_bytes()
        P.recover(self.root)
        self.assertEqual(path.read_bytes(), edited)
        self.assertFalse(P.journal_path(self.root, first["cycle_id"]).exists())
        self.next_work(first)

    def test_locator_cache_deletion_is_observed_once(self):
        first = self.finished()
        for name in ("INDEX.json", "INDEX.md"):
            (self.root / "campaigns" / name).unlink()
        # Pure list observes without repairing; the writer-side reconcile
        # owns the single heal, further pure lists stay write-free.
        P.reconcile_root(self.root)
        P.list_campaign_summaries(self.root)
        P.list_campaign_summaries(self.root)
        self.assertEqual(len(self.control_events(first, "INDEX.json")), 1)
        self.assertEqual(len(self.control_events(first, "INDEX.md")), 1)
        self.next_work(first)

    def test_interrupted_first_publication_keeps_its_original_state_after_a_hand_edit(self):
        route, route_file = self.route(slug="interrupted", campaign_key="freedom")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        with self.assertRaises(P.artifact_admission.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=first["cycle_id"], allow_open_route=True, crash_after_manifest=True)
        path = Path(first["cycle_dir"]) / "manifest.json"
        document = json.loads(path.read_text())
        self.assertEqual(document["cycle"]["state"], "active")
        original_digest = P.artifact_manifest.manifest_digest(document)
        document["cycle"]["state"] = "completed"
        path.write_text(json.dumps(document))
        edited = path.read_bytes()
        for _ in range(2):
            P.recover(self.root)
            record = P.read_cycle_record(self.root, first["cycle_id"])
            self.assertEqual(record["cycle_state"], "active")
            self.assertEqual(record["manifest_digest"], original_digest)
            self.assertEqual(P.artifact_admission.load_index(self.root).manifests[first["cycle_id"]]["manifest_digest"],
                             original_digest)
            self.assertEqual(path.read_bytes(), edited)
        self.next_work(first)

    def test_manifest_only_state_edit_with_no_preserved_publication_cannot_create_completion(self):
        route, route_file = self.route(slug="unprepared", campaign_key="freedom")
        first = P.begin(self.root, route_file=route_file, capability="autopilot-code", intensity="direct")
        self.write_output(first)
        with self.assertRaises(P.artifact_admission.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=first["cycle_id"], allow_open_route=True, crash_after_manifest=True)
        path = Path(first["cycle_dir"]) / "manifest.json"
        document = json.loads(path.read_text())
        P.journal_path(self.root, first["cycle_id"]).unlink()
        P.artifact_lifecycle.manifest_snapshot_path(self.root, first["cycle_id"],
                                                   document["manifest_revision_id"]).unlink()
        document["cycle"]["state"] = "completed"
        path.write_text(json.dumps(document))
        edited = path.read_bytes()
        for _ in range(2):
            P.recover(self.root)
            self.assertEqual(P.read_cycle_record(self.root, first["cycle_id"])["state"], "open")
            self.assertNotIn(first["cycle_id"], P.artifact_admission.load_index(self.root).manifests)
            self.assertEqual(path.read_bytes(), edited)
        self.next_work(first)

    def test_interrupted_refresh_keeps_pending_edit_history_until_the_recorder_returns(self):
        first = self.finished()
        folder = Path(first["cycle_dir"])
        (folder / "artifacts/plans/cycle/REPORT.md").write_text("updated payload\n")
        with self.assertRaises(P.artifact_admission.AdmissionRecoveryRequired):
            P.finalize(self.root, cycle_id=first["cycle_id"], crash_after_manifest=True)
        path = folder / "manifest.json"
        document = json.loads(path.read_text())
        document["cycle"]["title"] = "edited while recorder was unavailable"
        path.write_text(json.dumps(document))
        with mock.patch.object(P, "_history_module", return_value=None):
            P.finalize(self.root, cycle_id=first["cycle_id"])
            pending = P.read_cycle_record(self.root, first["cycle_id"])["history_pending"]
            self.assertTrue(any(row.get("reason") == "filesystem-change" and
                                row["target_path"].endswith("manifest.json") for row in pending))
        for _ in range(2):
            P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(len(self.control_events(first, "manifest.json")), 1)

    def test_valid_runtime_record_and_locator_edits_are_logged_once_without_own_write_noise(self):
        first = self.finished()
        record_path = P.cycle_record_path(self.root, first["cycle_id"])
        record = json.loads(record_path.read_text())
        record["title"] = "edited runtime title"
        record_path.write_text(json.dumps(record))
        index_json = self.root / "campaigns/INDEX.json"
        index_json.write_text(json.dumps(json.loads(index_json.read_text()), indent=4) + "\n")
        index_md = self.root / "campaigns/INDEX.md"
        index_md.write_text(index_md.read_text() + "\nUser annotation\n")
        for _ in range(2):
            # Writers observe external edits; listing leaves them unrepaired.
            P.reconcile_root(self.root)
            P.deliver_pending_history(self.root)
            P.checkpoint(self.root, cycle_id=first["cycle_id"], trigger="explicit")
            P.list_campaign_summaries(self.root)
            P.finalize(self.root, cycle_id=first["cycle_id"])
        self.assertEqual(len(self.control_events(first, record_path.name)), 1)
        self.assertEqual(len(self.control_events(first, "INDEX.json")), 1)
        self.assertEqual(len(self.control_events(first, "INDEX.md")), 1)
        self.next_work(first)


if __name__ == "__main__":
    unittest.main()
