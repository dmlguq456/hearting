#!/usr/bin/env python3
"""Daily entry identity, real completion and consolidation/restore boundaries."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import artifact_journal as J
import artifact_journal_migration as M
import artifact_producer as P
import journal_finish as F
import inline_finish

spec = importlib.util.spec_from_file_location("journal_turn_tests", HERE / "turn_change_record.test.py")
turns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(turns)


class EntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".agent_reports"
        self.root.mkdir()
        self.binding = {"path": "campaigns/2026-11-06_example/journals/2026-11-06.md",
            "campaign_id": "camp_" + "1" * 32, "campaign_key": "example", "route_id": "rt-abcd",
            "route_hash": "hash", "at": "2026-11-06T23:59:58+09:00"}
        campaign = self.root / "campaigns/2026-11-06_example"
        campaign.mkdir(parents=True)
        (campaign / "campaign.json").write_text(json.dumps({"campaign_id": self.binding["campaign_id"]}))

    def add(self, key="a", **kw):
        return J.append(self.root, self.binding, origin={"turn": key}, summary="Work " + key,
                        body="Observed check exit 1; task verdict unknown\n", **kw)

    def test_A02_A03_stable_opaque_identity_and_prior_revision_after_append(self):
        one = self.add()
        self.assertTrue(J.is_well_formed(one["artifact_id"], "artifact"))
        self.assertEqual(one["revision_id"], self.add()["revision_id"])
        self.assertTrue(self.add()["replay"])
        self.add("b")
        self.assertEqual(J.revision_digest(one["reference"]), one["digest"])
        day = J.read_day(self.root / self.binding["path"])
        self.assertEqual(len(day["rows"]), 2)
        self.assertEqual(day["rows"][0]["checks"]["state"], "unknown")

    def test_correction_keeps_history_actor_and_manual_drift_is_not_an_event(self):
        first = self.add()
        with self.assertRaisesRegex(ValueError, "origin-conflict"):
            J.append(self.root, self.binding, origin={"turn": "a"}, summary="corrected", body="new")
        second = J.append(self.root, self.binding, origin={"turn": "a"}, summary="corrected", body="new",
            correction={"actor": "confirmed-user", "source": "native:turn-2", "reason": "user correction"})
        self.assertEqual(second["artifact_id"], first["artifact_id"])
        self.assertEqual(second["previous_revision"], first["revision_id"])
        self.assertEqual(J.revision_digest(first["reference"]), first["digest"])
        path = self.root / self.binding["path"]
        raw = path.read_text().replace("Observed check exit 1", "Observed check exit 0")
        path.write_text(raw)
        scanned = J.read_day(path)
        self.assertEqual(scanned["coverage"], "partial")
        self.assertEqual(len(scanned["rows"]), 1)
        self.assertEqual(scanned["rows"][0]["event_id"], second["event_id"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            J.read_reference(first["reference"])

    def test_A04_all_harnesses_concurrent_retry_no_lost_entries(self):
        def write(n):
            return self.add(str(n), session={"harness": ("claude", "codex", "opencode")[n % 3], "sid": str(n)})
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, list(range(30)) * 2))
        day = J.read_day(self.root / self.binding["path"])
        self.assertEqual(day["coverage"], "complete")
        self.assertEqual(len(day["rows"]), 30)
        self.assertEqual(len({r["artifact_id"] for r in day["rows"]}), 30)

    def test_A06_midnight_late_retry_keeps_original_day_and_offset(self):
        items = [{"id": "original", "began": 1793977198, "ended": 1793977202,
                  "section": "original turn", "source": []}]
        first = F.record_items(self.root, self.binding, {"harness": "codex", "sid": "sid"}, items)[0]
        binding = {**self.binding, "at": "2026-11-08T00:00:00+09:00"}
        replay = F.record_items(self.root, binding, {"harness": "codex", "sid": "sid"}, items)[0]
        self.assertEqual(first["reference"], replay["reference"])
        self.assertEqual(first["at"], replay["at"])

    def test_A08_failed_write_is_retryable_and_does_not_mangle_prior_bytes(self):
        first = self.add()
        with mock.patch.object(J, "atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.add("b")
        self.assertEqual(J.revision_digest(first["reference"]), first["digest"])
        self.assertEqual(self.add("b")["replay"], False)

    def test_A14_bounded_corrupt_and_permission_reads(self):
        self.add(); self.add("b")
        day = self.root / self.binding["path"]
        self.assertEqual(J.read_day(day, limit=1)["coverage"], "partial")
        self.assertEqual(J.read_day(day, byte_limit=40)["coverage"], "partial")
        with mock.patch.object(J.os, "open", side_effect=PermissionError):
            self.assertEqual(J.read_day(day)["coverage"], "unknown")
        day.write_text(day.read_text() + J.START + "{bad -->\nbroken\n" + J.END)
        self.assertEqual(J.read_day(day)["coverage"], "partial")

    def test_A15_result_locations_and_versions_not_mtime_inference(self):
        result = Path(self.tmp.name) / "paper.txt"
        result.write_text("paper")
        versions = F.result_versions(str(result.parent), [result.name, "absent"], [])
        entry = self.add(results=versions)
        self.assertEqual(result.read_text(), "paper")
        row = J.read_reference(entry["reference"])
        self.assertEqual(row["results"][0]["version"]["kind"], "sha256")
        self.assertEqual(row["results"][1]["version"], {"kind": "unknown"})
        self.assertEqual(row["results"][0]["path"], str(result))

    def test_A16_paths_reject_symlink_and_escape(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        import shutil
        shutil.rmtree(self.root / "campaigns")
        (self.root / "campaigns").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.add()
        self.assertEqual(list(outside.iterdir()), [])
        with self.assertRaises(ValueError):
            J.safe_path(self.root, self.root / "../escape")


class CompletionTest(turns.EndToEndTest):
    # Do not inherit superseded storage assertions. Attribution/race tests in
    # turn_change_record.test.py continue exercising the same public API.
    test_one_closed_unassigned_record_that_later_turns_append_to = None
    test_a_route_left_open_by_a_failed_close_is_closed_at_the_next_turn = None

    def direct(self, slug):
        work = self.tmp / slug
        work.mkdir()
        (work / "task.md").write_text("Known scoped direct request " + slug)
        (work / "evidence.md").write_text("Checks: synthetic command exit 0; scope verified\n")
        (work / "summary.md").write_text("Completed " + slug)
        env = turns.T._child_env(self.env, "claude", turns.SID)
        result = turns.T._run(turns.T._route_command("compose", "--shape", "direct", "--cwd", str(self.repo),
            "--artifact-root", str(self.root), "--campaign-key", "journal-example", "--slug", slug,
            "--parent-harness", "claude", "--prompt-file", str(work / "task.md"),
            "--spec-read", "fixture", "--drift-verdict", "within-spec", "--artifact-guard", "fixture"), env, 60)
        self.assertEqual(result.returncode, 0, result.stderr)
        route_file = turns.T._last_json(result.stdout)["route_file"]
        command = turns.T._route_command("finish", "--route", route_file, "--evidence", str(work / "evidence.md"),
                                         "--summary-file", str(work / "summary.md"))
        return route_file, work, env, command

    def test_A01_explicit_directs_same_day_share_journal_and_keep_proofs(self):
        receipts = []
        for slug in ("first-explicit", "second-explicit"):
            route, work, env, command = self.direct(slug)
            made = turns.T._run(command, env, 60)
            self.assertEqual(made.returncode, 0, made.stderr)
            receipts.append(turns.T._last_json(made.stdout))
        days = list((self.root / "campaigns").glob("*/journals/*.md"))
        self.assertEqual(len(days), 1)
        self.assertEqual(len(J.read_day(days[0])["rows"]), 2)
        self.assertEqual(list((self.root / "campaigns").glob("*/*/.cycle.json")), [])
        for receipt in receipts:
            self.assertEqual(J.revision_digest(receipt["entries"][0]["path"]), receipt["entries"][0]["digest"])

    def test_A08_new_journal_finish_resumes_exact_effects_without_duplicate_entries(self):
        for point in ("after-claim", "after-marker", "after-close-write", "after-close", "after-receipt"):
            with self.subTest(point=point):
                route, work, env, command = self.direct("crash-" + point)
                failed = turns.T._run(command, {**env, "HEARTING_INLINE_FINISH_CRASH_AT": point}, 60)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("fault-injected-" + point, failed.stderr)
                replay = turns.T._run(command, env, 60)
                self.assertEqual(replay.returncode, 0, replay.stderr)
                receipt = turns.T._last_json(replay.stdout)
                self.assertTrue(receipt["replay"])
                day = J.split_reference(receipt["entries"][0]["path"])[0]
                self.assertEqual(len([r for r in J.read_day(day)["rows"] if r["route_id"] == Path(route).stem]), 1)

    def test_A10_new_journal_reuses_source_commit_and_scope_refusals(self):
        route, work, env, command = self.direct("scope-check")
        # A different clone with the same HEAD cannot replace the execution
        # repository's common-directory identity (selection shared with v1).
        raw = json.loads(Path(route).read_text())
        args = type("Args", (), {"commit": "0" * 40})()
        refused = turns.T._run([*command, "--commit", args.commit], env, 60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("finish-commit", refused.stderr)
        self.assertIsNone(inline_finish.pending_state(self.root, raw["route_id"]))
        self.assertEqual(list((self.root / "campaigns").glob("*/journals/*.md")), [])

    def test_A08_automatic_partial_finish_keeps_work_until_receipt_resumes(self):
        self.env["HEARTING_INLINE_FINISH_CRASH_AT"] = "after-close-write"
        first = self.turn(lambda: (self.repo / "greet.py").write_text('print("Hello")\n'))
        entry = first["records"][str(self.root)]
        self.assertFalse(entry["closed"])
        work = turns.T._work(self.env, "claude", turns.SID, str(self.root))
        self.assertTrue((work / "evidence.md").exists())
        self.assertNotEqual(inline_finish.pending_state(self.root, entry["route_id"])["state"], "finished")
        self.env.pop("HEARTING_INLINE_FINISH_CRASH_AT")
        turns.T.turn_start("claude", turns.SID, str(self.repo), self.env)
        self.assertEqual(inline_finish.pending_state(self.root, entry["route_id"])["state"], "finished")
        self.assertFalse(work.exists())

    def test_A01_A09_two_turns_one_file_zero_cycles_real_finish_v2(self):
        first = self.turn(lambda: (self.repo / "greet.py").write_text('print("Hello")\n'))
        self.assertEqual(first["state"], "recorded", first)
        entry = first["records"][str(self.root)]
        state = inline_finish.pending_state(self.root, entry["route_id"])
        self.assertEqual(state["receipt"]["schema"], "finish_receipt_v2")
        self.assertNotIn("cycle_id", state["receipt"])
        old_ref = state["receipt"]["entries"][0]
        second = self.turn(lambda: (self.repo / "README.md").write_text("new notes\n"))
        self.assertEqual(second["state"], "appended", second)
        journals = list((self.root / "campaigns").glob("*/journals/*.md"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(len(J.read_day(journals[0])["rows"]), 2)
        self.assertEqual(list((self.root / "campaigns").glob("*/*/.cycle.json")), [])
        self.assertEqual(J.revision_digest(old_ref["path"]), old_ref["digest"])
        self.assertTrue(json.loads(Path(entry["route_file"]).with_suffix(".outcome.json").read_text())["terminal_gate_proven"])
        supplements = turns.T.supplementary_records("claude", turns.SID, str(self.repo), self.env)
        self.assertTrue(all(row["kind"] == "journal-entry" for row in supplements))
        self.assertTrue(all(row["record_target"] and row["result_targets"] for row in supplements))

    def test_A02_A08_finish_resume_after_each_effect(self):
        first = self.turn(lambda: (self.repo / "greet.py").write_text('print("Hello")\n'))
        entry = first["records"][str(self.root)]
        state = inline_finish.pending_state(self.root, entry["route_id"])
        before = J.read_reference(state["receipt"]["entries"][0]["path"])
        # A genuine replay after later entry appends proves the stored revision,
        # even when original temporary evidence no longer exists.
        route = json.loads(Path(entry["route_file"]).read_text())
        result = turns.T._run(turns.T._route_command("finish", "--route", entry["route_file"],
            "--evidence", str(self.tmp / "gone"), "--summary-file", str(self.tmp / "gone-summary")),
            turns.T._child_env(self.env, "claude", turns.SID), 60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(J.read_reference(state["receipt"]["entries"][0]["path"])["digest"], before["digest"])


class MigrationTest(turns.EndToEndTest):
    test_one_closed_unassigned_record_that_later_turns_append_to = None
    test_a_route_left_open_by_a_failed_close_is_closed_at_the_next_turn = None

    def legacy(self):
        real = turns.T._run
        def run(args, env, timeout):
            if len(args) > 1 and args[1] == "finish":
                route_path = Path(args[args.index("--route") + 1])
                route = json.loads(route_path.read_text())
                with mock.patch.dict(os.environ, env, clear=True):
                    P.begin(self.root, route_file=route_path, capability=route["capability"],
                            intensity=route["effective_intensity"], require_cycle=True)
            return real(args, env, timeout)
        with mock.patch.object(turns.T, "_run", side_effect=run):
            result = self.turn(lambda: (self.repo / "greet.py").write_text('print("Hello")\n'))
        self.assertEqual(result["state"], "recorded", result)
        record = next(row for row in P.list_cycle_records(self.root))
        self.assertEqual(inline_finish.pending_state(self.root, record["route_id"])["schema"], "inline_finish_v1")
        return record

    def test_M01_M04_shape_composition_and_dry_run_are_observational(self):
        record = self.legacy()
        source = P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)
        before = M.inventory(source)
        result = M.dry_run(self.root, self.env)
        self.assertTrue(result["candidates"][0]["eligible"], result)
        self.assertEqual(M.inventory(source), before)
        (source / "artifacts/plans").mkdir()
        (source / "artifacts/plans/plan.md").write_text("planned work")
        skipped = M.candidate(self.root, record["cycle_id"], self.env)
        self.assertFalse(skipped["eligible"])
        self.assertIn("composition", skipped["reason"])

    def test_M05_M09_M10_backup_receipt_resolution_restore_preserves_later_entries(self):
        record = self.legacy(); cid = record["cycle_id"]
        source = P.cycle_dir(self.root, record["campaign_id"], cid, record)
        image = M.inventory(source)
        receipt = inline_finish.pending_state(self.root, record["route_id"])["receipt"]
        route_file = Path(record["route_file"])
        outcome = route_file.with_suffix(".outcome.json").read_bytes()
        result = M.apply(self.root, cid, self.env)
        self.assertEqual(result["state"], "committed")
        self.assertFalse(source.exists())
        archive = P.cycle_dir(self.root, record["campaign_id"], cid)
        self.assertEqual(M.inventory(archive)["entries"], image["entries"])
        self.assertEqual(P.artifact_locator.locate(self.root, cid), archive)
        self.assertEqual(inline_finish.pending_state(self.root, record["route_id"])["receipt"], receipt)
        self.assertEqual(route_file.with_suffix(".outcome.json").read_bytes(), outcome)
        import artifact_workflow_group_review as review
        selected = review.select_targets(self.root, None, cycles=[cid], auto=True)
        self.assertIn({"cycle_id": cid, "reason": "automatic-record"}, selected.skipped)
        # The original public v1 finish can still consume its exact proof from
        # the backup; no marker/receipt/manifest is rewritten as a v2 object.
        repeated = turns.T._run(turns.T._route_command("finish", "--route", str(route_file),
            "--evidence", str(turns.T._work(self.env, "claude", turns.SID, str(self.root)) / "evidence.md"), "--summary-file", str(turns.T._work(self.env, "claude", turns.SID, str(self.root)) / "summary.txt")),
            turns.T._child_env(self.env, "claude", turns.SID), 60)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(turns.T._last_json(repeated.stdout)["schema"], "finish_receipt_v1")
        row = J.read_reference(result["entries"][0]["path"])
        binding = J.target(self.root, record["route_id"])
        later = J.append(self.root, binding, origin={"later": "different session"}, summary="later", body="keep me")
        P.reconcile_root(self.root)
        self.assertFalse(P.read_cycle_record(self.root, cid).get("deleted_at"))
        self.assertFalse(source.exists())
        with mock.patch.dict(os.environ, self.env, clear=True):
            checkpoint = P.checkpoint(self.root, cycle_id=cid)
        self.assertEqual(checkpoint["reason"], "consolidated-to-journal")
        restored = M.restore(self.root, cid, self.env)
        self.assertEqual(restored["state"], "restored")
        self.assertTrue(M._same_tree(source, image))
        self.assertEqual(J.revision_digest(later["reference"]), later["digest"])
        self.assertNotIn(row["revision_id"], (self.root / binding["path"]).read_text())
        self.assertTrue(M.restore(self.root, cid, self.env)["replay"])

    def test_M08_M17_resume_every_apply_and_restore_effect(self):
        record = self.legacy(); cid = record["cycle_id"]
        apply_points = ["after-backup", "after-entry-effect", "after-entry", "after-rebind-effect", "after-rebind", "after-retire-effect", "after-commit"]
        restore_points = ["after-restore-stage", "after-restore-publish-effect", "after-restore-publish",
            "after-restore-entry-effect", "after-restore-entry", "after-restore-metadata-effect",
            "after-restore-metadata", "after-restore-complete"]
        for point in apply_points:
            with self.subTest(apply=point):
                def crash(where):
                    if where == point:
                        raise RuntimeError(point)
                with mock.patch.object(M, "_fault", side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, point):
                        M.apply(self.root, cid, self.env)
                result = M.apply(self.root, cid, self.env)
                self.assertEqual(result["state"], "committed")
                self.assertEqual(M.restore(self.root, cid, self.env)["state"], "restored")
        for point in restore_points:
            with self.subTest(restore=point):
                M.apply(self.root, cid, self.env)
                def crash(where):
                    if where == point:
                        raise RuntimeError(point)
                with mock.patch.object(M, "_fault", side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, point):
                        M.restore(self.root, cid, self.env)
                self.assertEqual(M.restore(self.root, cid, self.env)["state"], "restored")

    def test_M18_same_bytes_external_directory_is_not_our_publish(self):
        record = self.legacy(); cid = record["cycle_id"]
        source = P.cycle_dir(self.root, record["campaign_id"], cid, record)
        M.apply(self.root, cid, self.env)
        def crash(where):
            if where == "after-restore-stage":
                raise RuntimeError("crash")
        with mock.patch.object(M, "_fault", side_effect=crash):
            with self.assertRaises(RuntimeError):
                M.restore(self.root, cid, self.env)
        path, op = M._operation(self.root, cid)
        import shutil
        shutil.copytree(path.parent / "backup", source)
        self.assertTrue(M._same_tree(source, op["candidate"]["image"]))
        with self.assertRaisesRegex(ValueError, "destination-conflict"):
            M.restore(self.root, cid, self.env)

    def test_M14_M16_late_turn_enqueue_during_each_apply_boundary_keeps_original_lock(self):
        record = self.legacy(); cid = record["cycle_id"]
        source = P.cycle_dir(self.root, record["campaign_id"], cid, record)
        for point in ("after-backup", "after-entry", "after-rebind", "after-retire-effect"):
            with self.subTest(point=point):
                turns.T.turn_start("claude", turns.SID, str(self.repo), self.env)
                queued = []
                def late(where):
                    if where != point:
                        return
                    (self.repo / "README.md").write_text("late " + point)
                    # The normal state enqueue is not held behind admission or
                    # the day lock; only its record pass waits for the owner.
                    result = turns.T.turn_end("claude", turns.SID, str(self.repo), self.env, _record_wait=0.0)
                    queued.append(result)
                    self.assertTrue(turns.T._read(turns.T.state_path(self.env, "claude", turns.SID))["pending"])
                with mock.patch.object(M, "_fault", side_effect=late):
                    M.apply(self.root, cid, self.env)
                self.assertEqual(len(queued), 1)
                turns.T._record_pass(turns.T.state_path(self.env, "claude", turns.SID), "claude", turns.SID,
                    self.env, turns.T.record, turns.T.append, 0, 0)
                self.assertFalse(source.exists())
                rows = J.rows(self.root, session={"harness": "claude", "sid": turns.SID})["rows"]
                self.assertTrue(any("Automatic record" in r["title"] for r in rows))
                self.assertFalse(turns.T._read(turns.T.state_path(self.env, "claude", turns.SID))["pending"])
                M.restore(self.root, cid, self.env)

    def test_M19_restore_merges_other_entries_metadata_and_pending(self):
        record = self.legacy(); cid = record["cycle_id"]
        M.apply(self.root, cid, self.env)
        binding = J.target(self.root, record["route_id"])
        foreign = []
        def interleave(point):
            if point == "after-restore-entry":
                foreign.append(J.append(self.root, binding, origin={"unrelated": "other operation"},
                    summary="keep", body="later writer"))
                current = P.read_cycle_record(self.root, cid)
                current["unrelated_metadata"] = "preserve"
                M._save(P.cycle_record_path(self.root, cid), current)
                state_path = turns.T.state_path(self.env, "claude", turns.SID)
                turns.T._update(state_path, lambda s: s.update(unrelated_pending=["new-turn-C"]))
        with mock.patch.object(M, "_fault", side_effect=interleave):
            M.restore(self.root, cid, self.env)
        self.assertEqual(P.read_cycle_record(self.root, cid)["unrelated_metadata"], "preserve")
        self.assertEqual(J.revision_digest(foreign[0]["reference"]), foreign[0]["digest"])
        self.assertEqual(turns.T._read(turns.T.state_path(self.env, "claude", turns.SID))["unrelated_pending"], ["new-turn-C"])

    def test_M02_M03_M13_skip_open_large_unknown_but_do_not_stop_other_cycles(self):
        record = self.legacy(); cid = record["cycle_id"]
        route, work, env, command = CompletionTest.direct(self, "unrelated-open")
        data = json.loads(Path(route).read_text())
        opened = P.begin(self.root, route_file=Path(route), capability=data["capability"], intensity="direct", require_cycle=True)
        before = P.cycle_record_path(self.root, opened["cycle_id"]).read_bytes()
        choices = {r["cycle_id"]: r for r in M.dry_run(self.root, self.env)["candidates"]}
        self.assertFalse(choices[opened["cycle_id"]]["eligible"])
        self.assertTrue(choices[cid]["eligible"])
        M.apply(self.root, cid, self.env)
        self.assertEqual(P.cycle_record_path(self.root, opened["cycle_id"]).read_bytes(), before)
        M.restore(self.root, cid, self.env)
        source = P.cycle_dir(self.root, record["campaign_id"], cid)
        evidence = next((source / "artifacts/finish-evidence").iterdir())
        evidence.write_bytes(b"x" * (M.MAX_BYTES + 1))
        self.assertFalse(M.candidate(self.root, cid, self.env)["eligible"])
        self.assertTrue(evidence.exists())

    def test_M06_changed_source_and_symlink_are_preserved_after_backup(self):
        record = self.legacy(); cid = record["cycle_id"]
        source = P.cycle_dir(self.root, record["campaign_id"], cid)
        outside = self.tmp / "outside"
        outside.mkdir(); (outside / "keep").write_text("unrelated")
        moved = self.tmp / "original-cycle"
        def swap(point):
            if point == "after-backup":
                source.rename(moved)
                source.symlink_to(outside, target_is_directory=True)
        with mock.patch.object(M, "_fault", side_effect=swap):
            with self.assertRaisesRegex(ValueError, "source-changed"):
                M.apply(self.root, cid, self.env)
        self.assertEqual((outside / "keep").read_text(), "unrelated")
        self.assertTrue(moved.is_dir())
        self.assertEqual(list((self.root / "campaigns").glob("*/journals/*.md")), [])

    def test_M07_backup_and_day_write_failures_resume_without_source_loss(self):
        record = self.legacy(); cid = record["cycle_id"]
        source = P.cycle_dir(self.root, record["campaign_id"], cid)
        image = M.inventory(source)
        with mock.patch.object(M.shutil, "copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                M.apply(self.root, cid, self.env)
        self.assertTrue(M._same_tree(source, image, identity=True))
        real = J.atomic
        def fail_day(path, raw):
            if Path(path).suffix == ".md":
                raise PermissionError("day write")
            return real(path, raw)
        with mock.patch.object(J, "atomic", side_effect=fail_day):
            with self.assertRaises(PermissionError):
                M.apply(self.root, cid, self.env)
        self.assertTrue(M._same_tree(source, image, identity=True))
        self.assertEqual(M.apply(self.root, cid, self.env)["state"], "committed")
        self.assertEqual(M.restore(self.root, cid, self.env)["state"], "restored")

    def test_M11_edited_migration_entry_refuses_without_overwriting_it(self):
        record = self.legacy(); cid = record["cycle_id"]
        result = M.apply(self.root, cid, self.env)
        path = J.split_reference(result["entries"][0]["path"])[0]
        path.write_text(path.read_text().replace("Consolidated", "User edited") + "external prose\n")
        # Even a valid new correction is a same-entry external change.
        binding = J.target(self.root, record["route_id"])
        old = J.read_day(path)["rows"][0]
        J.append(self.root, binding, origin=old["origin"], summary="correction", body="keep correction",
                 correction={"actor": "user", "source": "turn-2", "reason": "correction"})
        raw = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "entry-conflict"):
            M.restore(self.root, cid, self.env)
        self.assertEqual(path.read_bytes(), raw)

    def test_M12_legacy_root_is_supported_without_project_specific_paths(self):
        legacy = self.repo / ".claude_reports"
        self.root.rename(legacy); self.root = legacy
        record = self.legacy()
        result = M.apply(self.root, record["cycle_id"], self.env)
        self.assertEqual(result["state"], "committed")
        self.assertIn(".claude_reports", result["entries"][0]["path"])
        self.assertEqual(M.restore(self.root, record["cycle_id"], self.env)["state"], "restored")

    def test_M15_orphan_carrier_uses_original_session_lock_and_identity(self):
        import time
        record = self.legacy(); cid = record["cycle_id"]
        turns.T.turn_start("claude", turns.SID, str(self.repo), self.env)
        result = []
        def carry(point):
            if point != "after-backup":
                return
            (self.repo / "README.md").write_text("orphan's result")
            turns.T.turn_end("claude", turns.SID, str(self.repo), self.env, _record_wait=0)
            result.extend(turns.T._adopt_orphans(str(self.root), "codex", "carrier", self.env, time.time(),
                                               turns.T.record, turns.T.append))
        with mock.patch.object(turns.T, "ORPHAN_AFTER", 0), mock.patch.object(turns.T, "session_ended", return_value=True):
            with mock.patch.object(M, "_fault", side_effect=carry):
                M.apply(self.root, cid, self.env)
            self.assertTrue(result and result[0].endswith(":busy"), result)
            done = turns.T._adopt_orphans(str(self.root), "codex", "carrier", self.env, time.time(), turns.T.record, turns.T.append)
        self.assertEqual(done, ["claude:" + turns.SID + ":appended"])
        rows = J.rows(self.root)["rows"]
        automatic = [r for r in rows if "Automatic record" in r["title"]]
        self.assertEqual(len(automatic), 1)
        self.assertEqual(automatic[0]["session"], {"harness": "claude", "sid": turns.SID})


if __name__ == "__main__":
    unittest.main()
