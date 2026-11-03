"""Isolated safety and cadence checks for dispatch log retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import contextlib
import importlib.util
import threading
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_log_retention as R  # noqa: E402
import dispatch_observation_log as OBS  # noqa: E402
import dispatch_contract as DC  # noqa: E402
import dispatch_pending_delivery as PD  # noqa: E402
import dispatch_retention as SHARED  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("g03_home_fixtures", ROOT / "utilities/worker_home_retention.test.py")
_HOME_FIXTURES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HOME_FIXTURES)
VirtualDeliveries = _HOME_FIXTURES.VirtualDeliveries

DAY = 86400 * 1_000_000_000


def stamp(value):
    return datetime.fromtimestamp(value / 1_000_000_000, timezone.utc).isoformat().replace("+00:00", "Z")


class DispatchLogRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dispatch-log-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.jobs = self.root / "jobs.log"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.now = time.time_ns()
        self.rows = []

    def add_row(self, attempt, log_file, *, status="done", ended=None, harness="claude",
                worker_type="stage", **extra):
        meta = {"attempt_id": attempt, "harness": harness, "worker_type": worker_type,
                "launch_outcome": "never-launched", "log_file": str(log_file)}
        meta.update(extra)
        self.rows.append(f"{stamp(ended if ended is not None else self.now)}\t{status}\t/repo\t/wt\tjob\t"
                         + ",".join(f"{key}={value}" for key, value in meta.items()))
        self.jobs.write_text("\n".join(self.rows) + "\n", encoding="utf-8")

    def log(self, name, body=b"transcript\n"):
        path = self.logs / name
        path.write_bytes(body)
        return path

    def sweep(self, *, at=None):
        return R.sweep(self.jobs, now_ns=self.now if at is None else at, force=True)

    def test_only_verified_logs_older_than_fourteen_days_are_removed(self):
        old = self.log("old.jsonl")
        recent = self.log("recent.jsonl")
        self.add_row("att-old-log", old, ended=self.now - 15 * DAY)
        self.add_row("att-recent-log", recent, ended=self.now - 13 * DAY)

        result = self.sweep()

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual((result["removed"], result["eligible"]), (1, 2))
        self.assertEqual(result["over_limit_bytes"], 0)

    def test_live_unsettled_review_and_unattributable_logs_are_kept(self):
        live = self.log("live.jsonl")
        review = self.log("review.jsonl")
        delivery = self.log("delivery.jsonl")
        unbound = self.log("unbound.jsonl")
        outside = self.root / "outside.jsonl"
        outside.write_text("outside\n", encoding="utf-8")
        self.add_row("att-live-log", live, status="open", ended=self.now - 30 * DAY)
        self.add_row("att-review-log", review, ended=self.now - 30 * DAY,
                     worker_type="review", failure_class="fail")
        self.add_row("att-delivery-log", delivery, ended=self.now - 30 * DAY,
                     delivery_intent="1", delivery_id="delivery-" + "a" * 32,
                     delivery_recipient_digest=PD.recipient_digest("sess-log-fixture"),
                     parent_sid="sess-log-fixture", parent_attempt_id="att-parent",
                     dispatch_depth="2", route_id="rt-" + "a" * 16, route_node="execute")
        self.add_row("att-outside-log", outside, ended=self.now - 30 * DAY)

        with mock.patch.object(R, "MAX_LOG_BYTES", 1):
            result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(all(path.exists() for path in (live, review, delivery, unbound, outside)))
        self.assertGreater(result["protected_bytes"], 0)
        self.assertGreater(result["unknown_bytes"], 0)
        self.assertGreater(result["over_limit_bytes"], 0)
        self.assertEqual(result["kept_reasons"].get("delivery-pending"), 1)
        self.assertEqual(result["kept_reasons"].get("review-consumer-open"), 1)

    def test_observation_logs_are_not_attempt_log_candidates(self):
        for index, name in enumerate(("dispatch-session-sweep.log", "dispatch-pending-delivery.log")):
            path = self.log(name)
            self.add_row(f"att-observation-{index}", path, ended=self.now - 30 * DAY)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.logs / "dispatch-session-sweep.log").exists())
        self.assertTrue((self.logs / "dispatch-pending-delivery.log").exists())

    def test_owned_prompt_sidecars_from_each_adapter_age_out(self):
        old = self.now - 30 * DAY
        prompts = []
        for harness in ("codex", "claude", "opencode"):
            attempt = f"att-{harness}-prompt-old"
            log_name = f"job.{attempt}.{harness}.jsonl"
            prompt_name = f"job.{attempt}.{harness}.prompt.txt"
            prompts.append(self.log(prompt_name, f"{harness} prompt\n".encode()))
            self.add_row(attempt, self.logs / log_name, harness=harness, ended=old)

        result = self.sweep()

        self.assertEqual(result["removed"], 3, result)
        self.assertEqual(result["eligible"], 3)
        self.assertTrue(all(not path.exists() for path in prompts))

    def test_prompt_sidecars_keep_active_unsettled_unknown_and_unbound_attempts(self):
        old = self.now - 30 * DAY
        prompts = []
        unknown_process_attempts = set()
        for harness in ("codex", "claude", "opencode"):
            active = f"att-{harness}-active-prompt"
            active_path = self.log(f"job.{active}.{harness}.prompt.txt")
            self.add_row(active, self.logs / f"job.{active}.{harness}.jsonl",
                         harness=harness, status="running", ended=old)
            prompts.append(active_path)

            pending = f"att-{harness}-delivery-prompt"
            pending_path = self.log(f"job.{pending}.{harness}.prompt.txt")
            self.add_row(
                pending, self.logs / f"job.{pending}.{harness}.jsonl",
                harness=harness, ended=old, delivery_intent="1",
                delivery_id="delivery-" + {"codex": "a", "claude": "b", "opencode": "c"}[harness] * 32,
                delivery_recipient_digest=PD.recipient_digest("sess-prompt-fixture"),
                parent_sid="sess-prompt-fixture", parent_attempt_id="att-parent",
                dispatch_depth="2", route_id="rt-" + "b" * 16, route_node="execute",
            )
            prompts.append(pending_path)

            unknown = f"att-{harness}-unknown-process-prompt"
            unknown_path = self.log(f"job.{unknown}.{harness}.prompt.txt")
            self.add_row(unknown, self.logs / f"job.{unknown}.{harness}.jsonl",
                         harness=harness, ended=old)
            unknown_process_attempts.add(unknown)
            prompts.append(unknown_path)

            review = f"att-{harness}-review-prompt"
            review_path = self.log(f"job.{review}.{harness}.prompt.txt")
            self.add_row(review, self.logs / f"job.{review}.{harness}.jsonl",
                         harness=harness, ended=old, worker_type="review", failure_class="fail")
            prompts.append(review_path)

        unbound = self.log("orphan.codex.prompt.txt")
        with mock.patch.object(
                DC, "attempt_process_quiescence",
                side_effect=lambda meta, **_kwargs: (
                    DC.ProcessQuiescence("unverifiable", "fixture-unknown")
                    if meta["attempt_id"] in unknown_process_attempts
                    else DC.ProcessQuiescence("quiescent", "fixture-quiet"))):
            result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(all(path.exists() for path in [*prompts, unbound]))
        self.assertEqual(result["kept_reasons"].get("delivery-pending"), 3)
        self.assertEqual(result["kept_reasons"].get("quiescence-unverifiable"), 3)
        self.assertEqual(result["kept_reasons"].get("review-consumer-open"), 3)

    def test_live_and_unverifiable_processes_keep_attempt_logs(self):
        live = self.log("live-process.jsonl")
        unknown = self.log("unknown-process.jsonl")
        old = self.now - 30 * DAY
        self.add_row("att-live-process", live, ended=old)
        self.add_row("att-unknown-process", unknown, ended=old)

        def quiescence(meta, *, terminal_receipt=False):
            if meta["attempt_id"] == "att-live-process":
                return DC.ProcessQuiescence("live", "attempt-descendant-live")
            return DC.ProcessQuiescence("unverifiable", "process-namespace-unverifiable")

        with mock.patch.object(DC, "attempt_process_quiescence", side_effect=quiescence):
            result = self.sweep()
        self.assertEqual(result["removed"], 0)
        self.assertTrue(live.exists() and unknown.exists())
        self.assertEqual(result["kept_reasons"].get("not-quiescent"), 1)
        self.assertEqual(result["kept_reasons"].get("quiescence-unverifiable"), 1)

    def test_shared_log_waits_for_every_bound_attempt(self):
        shared = self.log("shared.jsonl")
        old = self.now - 30 * DAY
        self.add_row("att-shared-done", shared, ended=old)
        self.add_row("att-shared-live", shared, status="running", ended=old)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(shared.exists())

    def test_symlinks_and_hardlinks_are_never_unlinked(self):
        target = self.root / "target.jsonl"
        target.write_text("target\n", encoding="utf-8")
        symlink = self.logs / "linked.jsonl"
        symlink.symlink_to(target)
        hard_source = self.log("hard-source.jsonl")
        hard = self.logs / "hard.jsonl"
        os.link(hard_source, hard)
        old = self.now - 30 * DAY
        self.add_row("att-symlink", symlink, ended=old)
        self.add_row("att-hardlink", hard, ended=old)

        result = self.sweep()

        self.assertEqual(result["removed"], 0)
        self.assertTrue(symlink.is_symlink())
        self.assertTrue(hard.exists())
        self.assertTrue(hard_source.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "target\n")

    def test_size_pressure_removes_oldest_eligible_log_first(self):
        old = self.log("oldest.jsonl", b"o" * 8)
        recent = self.log("recent.jsonl", b"r" * 8)
        self.add_row("att-oldest", old, ended=self.now - 2 * DAY)
        self.add_row("att-recent", recent, ended=self.now - DAY)

        with mock.patch.object(R, "MAX_LOG_BYTES", 10):
            result = self.sweep()

        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(result["removed_bytes"], 8)
        self.assertEqual(result["over_limit_bytes"], 0)

    def test_interrupted_unlink_retries_on_the_next_normal_pass(self):
        old = self.log("retry.jsonl")
        self.add_row("att-retry-log", old, ended=self.now - 30 * DAY)
        with mock.patch.object(R, "_unlink_if_same", return_value=False):
            first = self.sweep()
        self.assertEqual(first["removed"], 0)
        self.assertTrue(old.exists())

        second = self.sweep()
        self.assertEqual(second["removed"], 1)
        self.assertFalse(old.exists())

    def test_fair_cursor_visits_more_than_one_bounded_batch(self):
        attempts = []
        for index in range(70):
            attempt = f"att-cursor-{index:03d}"
            path = self.log(f"cursor-{index:03d}.jsonl")
            self.add_row(attempt, path, ended=self.now - DAY)
            attempts.append(attempt)
        seen = []
        actual = R._log_protection

        def record(ctx, attempt, *, probe_processes):
            seen.append(attempt)
            return actual(ctx, attempt, probe_processes=probe_processes)

        with mock.patch.object(R, "_log_protection", side_effect=record):
            first = self.sweep()
            second = self.sweep()
        self.assertEqual((first["examined"], second["examined"]), (64, 64))
        self.assertEqual(set(seen), set(attempts))

    def test_examination_stops_once_the_removal_budget_is_full(self):
        count = R.REMOVE_LIMIT + 4
        paths = []
        for index in range(count):
            path = self.log(f"expired-{index:03d}.jsonl")
            self.add_row(f"att-expired-{index:03d}", path, ended=self.now - 30 * DAY)
            paths.append(path)

        first = self.sweep()
        self.assertEqual((first["removed"], first["examined"]), (R.REMOVE_LIMIT, R.REMOVE_LIMIT))
        second = self.sweep()
        self.assertEqual(second["removed"], 4)
        self.assertFalse(any(path.exists() for path in paths))

    def test_files_still_inside_retention_do_not_end_examination_early(self):
        recent = []
        for index in range(R.REMOVE_LIMIT + 2):
            path = self.log(f"a-recent-{index:03d}.jsonl")
            self.add_row(f"att-a-recent-{index:03d}", path, ended=self.now - DAY)
            recent.append(path)
        old = self.log("z-expired.jsonl")
        self.add_row("att-z-expired", old, ended=self.now - 30 * DAY)

        result = self.sweep()

        self.assertEqual(result["removed"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(all(path.exists() for path in recent))

    def test_a_slow_examination_does_not_use_up_the_removal_time(self):
        paths = []
        for index in range(3):
            path = self.log(f"slow-{index}.jsonl")
            self.add_row(f"att-slow-{index}", path, ended=self.now - 30 * DAY)
            paths.append(path)
        clock = [0.0]
        actual = R._log_protection
        examined = set()

        def slow(ctx, attempt, *, probe_processes):
            verdict = actual(ctx, attempt, probe_processes=probe_processes)
            # The examination is slow after it has read its evidence (the
            # delivery observation shares the examine deadline). The removal's
            # own recheck of the same attempt runs inside the removal budget.
            if attempt not in examined:
                examined.add(attempt)
                clock[0] += R.MAX_PASS_SECONDS + 1
            return verdict

        with mock.patch.object(R.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(R, "_log_protection", side_effect=slow):
            result = self.sweep()

        # Examination stops after its own budget; the one file it reached is
        # still removed, and the cursor resumes after it.
        self.assertEqual((result["examined"], result["removed"]), (1, 1))
        state = json.loads((self.root / R.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(state["cursor"]) > 0, True)
        self.assertEqual(sum(path.exists() for path in paths), 2)

    def test_next_pass_starts_sooner_only_while_removable_logs_are_left_behind(self):
        for index in range(R.REMOVE_LIMIT + 1):
            path = self.log(f"more-{index:03d}.jsonl")
            self.add_row(f"att-more-{index:03d}", path, ended=self.now - 30 * DAY)
        first = R.sweep(self.jobs, now_ns=self.now)
        self.assertEqual((first["removed"], first["more"]), (R.REMOVE_LIMIT, 1))
        gap = R.BACKLOG_DUE_SECONDS * 1_000_000_000
        self.assertEqual(R.sweep(self.jobs, now_ns=self.now + gap - 1_000_000_000),
                         {"skipped": "not-due"})
        second = R.sweep(self.jobs, now_ns=self.now + gap)
        self.assertEqual((second["removed"], second["more"]), (1, 0))
        self.assertEqual(R.sweep(self.jobs, now_ns=self.now + 2 * gap),
                         {"skipped": "not-due"})

    def test_short_interval_continues_while_removing_and_still_over_the_limit(self):
        paths = []
        for index in range(4):
            path = self.log(f"pressure-{index}.jsonl", b"p" * 8)
            self.add_row(f"att-pressure-{index}", path, ended=self.now - (4 - index) * DAY)
            paths.append(path)
        with mock.patch.object(R, "MAX_LOG_BYTES", 8):
            first = self.sweep()
            self.assertEqual((first["removed"], first["over_limit_bytes"], first["more"]), (3, 0, 0))
        # Still over the limit after removing something: come back soon.
        for index in range(R.REMOVE_LIMIT - 1):
            path = self.log(f"bulk-{index:03d}.jsonl", b"b" * 8)
            self.add_row(f"att-bulk-{index:03d}", path, ended=self.now - 40 * DAY)
        with mock.patch.object(R, "MAX_LOG_BYTES", 8), mock.patch.object(R, "EXAMINE_LIMIT", 4):
            second = self.sweep()
        self.assertGreater(second["removed"], 0)
        self.assertGreater(second["over_limit_bytes"], 0)
        self.assertEqual(second["more"], 1)

    def test_over_the_limit_with_nothing_removable_keeps_the_normal_interval(self):
        path = self.log("held.jsonl", b"h" * 32)
        self.add_row("att-held", path, status="open", ended=self.now - 40 * DAY)
        with mock.patch.object(R, "MAX_LOG_BYTES", 8):
            result = self.sweep()
        self.assertEqual((result["removed"], result["more"]), (0, 0))
        self.assertGreater(result["over_limit_bytes"], 0)

    def test_one_registry_read_serves_every_removal_while_jobs_log_is_unchanged(self):
        import worker_home_retention as WHR

        for index in range(3):
            path = self.log(f"quiet-{index}.jsonl")
            self.add_row(f"att-quiet-{index}", path, ended=self.now - 30 * DAY)
        reads = []
        original = WHR._Snapshot.__init__

        def counting(snapshot, jobs):
            reads.append(jobs)
            original(snapshot, jobs)

        with mock.patch.object(WHR._Snapshot, "__init__", counting):
            result = self.sweep()
        self.assertEqual(result["removed"], 3)
        self.assertEqual(len(reads), 1)
        self.assertEqual(R._LOG_DIR_CHECKED, {})

    def test_a_registry_append_during_the_pass_is_read_again_before_each_removal(self):
        import worker_home_retention as WHR

        for index in range(3):
            path = self.log(f"busy-{index}.jsonl")
            self.add_row(f"att-busy-{index}", path, ended=self.now - 30 * DAY)
        reads = []
        original = WHR._Snapshot.__init__
        unlink = R._unlink_if_same
        appended = []

        def counting(snapshot, jobs):
            reads.append(jobs)
            original(snapshot, jobs)

        def unlink_then_append(layout, name, expected):
            done = unlink(layout, name, expected)
            if not appended:
                appended.append(name)
                with open(self.jobs, "a", encoding="utf-8") as handle:
                    handle.write(f"{stamp(self.now)}\tdone\t/repo\t/wt\tjob\t"
                                 "attempt_id=att-late-unrelated,harness=claude\n")
            return done

        with mock.patch.object(WHR._Snapshot, "__init__", counting), \
                mock.patch.object(R, "_unlink_if_same", unlink_then_append):
            result = self.sweep()
        self.assertEqual(result["removed"], 3)
        # The pass read, then one more per removal after the registry changed.
        self.assertEqual(len(reads), 3)

    def test_inode_replacement_between_scan_and_unlink_is_preserved(self):
        path = self.log("swap.jsonl")
        info = os.stat(path, follow_symlinks=False)
        expected = R._stamp(info)
        replacement = self.logs / "replacement.jsonl"
        replacement.write_text("replacement\n", encoding="utf-8")
        os.replace(replacement, path)

        with R._LogLayout(self.root) as layout:
            self.assertFalse(R._unlink_if_same(layout, path.name, expected))
        self.assertEqual(path.read_text(encoding="utf-8"), "replacement\n")

    def test_new_writer_at_original_path_survives_quarantine_removal(self):
        path = self.log("swap-during-unlink.jsonl")
        expected = R._stamp(os.stat(path, follow_symlinks=False))
        original_unlink = os.unlink
        created = []

        def create_new_writer(name, *args, dir_fd=None, **kwargs):
            if (name == path.name and dir_fd is not None
                    and os.fstat(dir_fd).st_ino != self.logs.stat().st_ino and not created):
                path.write_text("new writer\n", encoding="utf-8")
                created.append(True)
            return original_unlink(name, *args, dir_fd=dir_fd, **kwargs)

        with R._LogLayout(self.root) as layout, \
                mock.patch.object(SHARED.os, "unlink", side_effect=create_new_writer):
            self.assertTrue(R._unlink_if_same(layout, path.name, expected))

        self.assertTrue(created)
        self.assertEqual(path.read_text(encoding="utf-8"), "new writer\n")

    def test_mismatched_quarantined_inode_is_restored(self):
        path = self.log("swap-before-quarantine.jsonl")
        expected = R._stamp(os.stat(path, follow_symlinks=False))
        replacement = self.root / "replacement.jsonl"
        replacement.write_text("replacement\n", encoding="utf-8")
        original_rename = os.rename
        swapped = []

        def swap_before_move(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if src == path.name and src_dir_fd == layout.logs_fd and not swapped:
                os.replace(replacement, path)
                swapped.append(True)
            return original_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with R._LogLayout(self.root) as layout, \
                mock.patch.object(SHARED.os, "rename", side_effect=swap_before_move):
            self.assertFalse(R._unlink_if_same(layout, path.name, expected))

        self.assertTrue(swapped)
        self.assertEqual(path.read_text(encoding="utf-8"), "replacement\n")
        self.assertFalse(list(self.logs.glob(SHARED.QUARANTINE_PREFIX + "*")))

    def test_new_writer_is_never_overwritten_when_mismatch_restore_runs(self):
        path = self.log("swap-new-writer.jsonl")
        expected = R._stamp(os.stat(path, follow_symlinks=False))
        replacement = self.root / "replacement.jsonl"
        replacement.write_text("moved replacement\n", encoding="utf-8")
        original_rename = os.rename
        original_recover = SHARED._recover_quarantine
        swapped = []
        wrote_new = []

        def swap_before_move(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if src == path.name and src_dir_fd == layout.logs_fd and not swapped:
                os.replace(replacement, path)
                swapped.append(True)
            return original_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        def create_new_writer(parent_fd, quarantine_name):
            if not wrote_new:
                path.write_text("new writer\n", encoding="utf-8")
                wrote_new.append(True)
            return original_recover(parent_fd, quarantine_name)

        with R._LogLayout(self.root) as layout, \
                mock.patch.object(SHARED.os, "rename", side_effect=swap_before_move), \
                mock.patch.object(SHARED, "_recover_quarantine", side_effect=create_new_writer):
            self.assertFalse(R._unlink_if_same(layout, path.name, expected))

        self.assertTrue(swapped and wrote_new)
        self.assertEqual(path.read_text(encoding="utf-8"), "new writer\n")
        quarantined = list(self.logs.glob(SHARED.QUARANTINE_PREFIX + "*/*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "moved replacement\n")

    def test_interrupted_quarantine_is_restored_and_retried_next_pass(self):
        old = self.log("quarantine-retry.jsonl")
        self.add_row("att-quarantine-retry", old, ended=self.now - 30 * DAY)
        original_unlink = os.unlink
        interrupted = []

        def interrupt_first_quarantine_unlink(name, *args, dir_fd=None, **kwargs):
            if name == old.name and dir_fd is not None and not interrupted:
                interrupted.append(True)
                raise OSError("simulated interruption after quarantine move")
            return original_unlink(name, *args, dir_fd=dir_fd, **kwargs)

        with mock.patch.object(SHARED.os, "unlink", side_effect=interrupt_first_quarantine_unlink):
            first = self.sweep()
        self.assertEqual(first["removed"], 0)
        self.assertFalse(old.exists())
        quarantined = list(self.logs.glob(SHARED.QUARANTINE_PREFIX + "*/*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "transcript\n")

        second = R.sweep(self.jobs, now_ns=self.now + 301 * 10**9)
        self.assertEqual(second["removed"], 1)
        self.assertFalse(old.exists())
        self.assertFalse(list(self.logs.glob(SHARED.QUARANTINE_PREFIX + "*/*")))


    def publish(self, attempt, *, relation="attempt", recipient="g03-parent", number=1):
        receipt = {"schema_version": 2, "fixture": "g03-publication"}
        return PD.create(self.root, recipient_kind="codex-native-queue", recipient_key=recipient,
                         delivery_id=f"delivery-{number:032x}", session_generation="",
                         session_generation_supported="0",
                         attempt_ids=[attempt if relation == "attempt" else "att-other-child"],
                         parent_attempt_id=attempt if relation == "parent" else "-",
                         route_id="route-free", route_node="execute", receipt=receipt,
                         receipt_digest=PD._canonical_receipt_digest(receipt), row_revisions={})

    def test_g03_large_history_late_open_keeps_related_log_prompt_across_adapters(self):
        for harness in ("claude", "codex", "opencode"):
            for groups in (1, 3):
                with self.subTest(harness=harness, groups=groups):
                    case = DispatchLogRetentionTest(); case.setUp()
                    try:
                        paths = {}
                        for suffix in ("free", "open"):
                            attempt = f"att-g03-{suffix}"
                            log = case.log(f"job.{attempt}.{harness}.jsonl")
                            prompt = case.log(f"job.{attempt}.{harness}.prompt.txt")
                            paths[suffix] = (log, prompt)
                            case.add_row(attempt, log, harness=harness, ended=case.now - 30 * DAY)
                        with VirtualDeliveries(case.root, case.now - 40 * DAY, groups=groups,
                                open_refs=[(["att-child"], "att-g03-open")]) as history:
                            first = R.sweep(case.jobs, now_ns=case.now)
                            first_reads = history.reads
                            self.assertEqual((first["eligible"], first["removed"]), (0, 0), first)
                            self.assertGreater(first["deferred"], 0)
                            last = R.sweep(case.jobs, now_ns=case.now + 301 * 10**9)
                            self.assertEqual(last["removed"], 2, last)
                            self.assertTrue(all(p.exists() for p in paths["open"]))
                            self.assertTrue(all(not p.exists() for p in paths["free"]))
                            self.assertEqual(history.reads, 4098)
                            print(json.dumps({"g03_log": harness, "groups": groups,
                                "reads_by_pass": [first_reads, history.reads-first_reads],
                                "removed_log_and_prompt": last["removed"]}))
                    finally:
                        case.doCleanups()

    def test_g03_pinned_and_current_publisher_first_after_resumed_scan(self):
        import worker_home_retention as home
        import dispatch_attempt_reference as reference
        for pinned in (True, False):
            for new_group in (False, True):
                for relation in ("attempt", "parent"):
                    with self.subTest(pinned=pinned, new_group=new_group, relation=relation):
                        case = DispatchLogRetentionTest(); case.setUp()
                        try:
                            attempt = "att-g03-log-final-publication"
                            log = case.log(f"job.{attempt}.codex.jsonl")
                            prompt = case.log(f"job.{attempt}.codex.prompt.txt")
                            case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
                            recipients = ["g03-a", "g03-b", "g03-c"]
                            with VirtualDeliveries(case.root, case.now - 40 * DAY,
                                    groups=3, recipient_keys=recipients):
                                self.assertGreater(R.sweep(case.jobs, now_ns=case.now)["deferred"], 0)
                                before = PD.reference_token(case.root, attempt)
                                original, published = home._late_candidate_delivery_change, []
                                recipient = "g03-new" if new_group else recipients[0]
                                def publish_then_check(ctx, selected, deadline, binding=None):
                                    if not published:
                                        self.assertNotIn(attempt, reference.held_attempts())
                                        with _HOME_FIXTURES.normal_delivery_writer(case.root, recipient, attempt,
                                                pinned=pinned, relation=relation):
                                            pass
                                        published.append(True)
                                    return original(ctx, selected, deadline, binding)
                                with mock.patch.object(home, "_late_candidate_delivery_change", publish_then_check):
                                    result = R.sweep(case.jobs, now_ns=case.now + 301 * 10**9)
                                self.assertEqual(result["removed"], 0, result)
                                self.assertTrue(log.exists() and prompt.exists())
                                self.assertEqual(published, [True])
                                self.assertEqual(before == PD.reference_token(case.root, attempt), pinned)
                                self.assertEqual(PD.read(case.root, recipient, "delivery-" + "f" * 32)["state"], "pending")
                                print(json.dumps({"g03_log_publisher_first": True, "pinned": pinned,
                                    "new_group": new_group, "relation": relation, "result": result}))
                        finally:
                            case.doCleanups()

    def test_g03_pinned_and_current_deletion_first_fences_quarantine_and_unlink(self):
        for pinned in (True, False):
            for new_group in (False, True):
                for relation in ("attempt", "parent"):
                    for kind in ("log", "prompt"):
                        with self.subTest(pinned=pinned, new_group=new_group, relation=relation, kind=kind):
                            case = DispatchLogRetentionTest(); case.setUp()
                            try:
                                attempt = "att-g03-log-delete-first"
                                log = case.logs / f"job.{attempt}.codex.jsonl"
                                target = case.log(log.name if kind == "log" else f"job.{attempt}.codex.prompt.txt")
                                case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
                                recipients = ["g03-a", "g03-b", "g03-c"]
                                with VirtualDeliveries(case.root, case.now - 40 * DAY,
                                        groups=3, recipient_keys=recipients):
                                    self.assertGreater(R.sweep(case.jobs, now_ns=case.now)["deferred"], 0)
                                    recipient = "g03-new" if new_group else recipients[0]
                                    real_move, real_unlink, order = SHARED.os.rename, SHARED.os.unlink, []
                                    with _HOME_FIXTURES.normal_delivery_writer(case.root, recipient, attempt,
                                            pinned=pinned, relation=relation, gated=True) as child:
                                        def move(name, *args, **kwargs):
                                            if name == target.name:
                                                child.stdin.write("go\n"); child.stdin.flush()
                                                _HOME_FIXTURES.wait_for_reference_attempt(child)
                                                self.assertIsNone(child.poll())
                                                order.append("quarantine")
                                            return real_move(name, *args, **kwargs)
                                        def unlink(name, *args, **kwargs):
                                            result = real_unlink(name, *args, **kwargs)
                                            if name == target.name:
                                                self.assertIsNone(child.poll(), "publisher passed actual unlink")
                                                order.append("unlink")
                                            return result
                                        with mock.patch.object(SHARED.os, "rename", move), \
                                                mock.patch.object(SHARED.os, "unlink", unlink):
                                            result = R.sweep(case.jobs, now_ns=case.now + 301 * 10**9)
                                    self.assertEqual(order, ["quarantine", "unlink"])
                                    self.assertEqual(result["removed"], 1, result)
                                    self.assertFalse(target.exists())
                                    self.assertEqual(PD.read(case.root, recipient, "delivery-" + "f" * 32)["state"], "pending")
                            finally:
                                case.doCleanups()

    def test_g03_idempotent_unrelated_create_preserves_multi_pass_progress(self):
        import worker_home_retention as home
        for groups, writer_group in ((1, 0), (3, 0), (3, 2)):
            with self.subTest(groups=groups, writer_group=writer_group):
                case = DispatchLogRetentionTest(); case.setUp()
                try:
                    paths = {}
                    for suffix in ("free", "open"):
                        attempt = "att-g03-cadence-" + suffix
                        log = case.log(f"job.{attempt}.codex.jsonl")
                        prompt = case.log(f"job.{attempt}.codex.prompt.txt")
                        paths[suffix] = (log, prompt)
                        case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
                    recipients = [f"g03-cadence-{i}" for i in range(groups)]
                    with VirtualDeliveries(case.root, case.now - 40 * DAY, groups=groups,
                            recipient_keys=recipients, open_refs=[(["att-child"], "att-g03-cadence-open")]):
                        recipient = recipients[writer_group]
                        def noop():
                            case.publish("att-unrelated", recipient=recipient, number=2**128 - 1)
                        noop()
                        stamps = [home._stamp(case.root / "pending-delivery"),
                                  home._stamp(PD.record_directory(case.root, recipient))]
                        passes = []
                        for i in range(3):
                            noop()
                            self.assertEqual(stamps, [home._stamp(case.root / "pending-delivery"),
                                                     home._stamp(PD.record_directory(case.root, recipient))])
                            result = R.sweep(case.jobs, now_ns=case.now + i * 301 * 10**9)
                            passes.append(result)
                            self.assertLessEqual(result["delivery_reads"], home.DELIVERY_READ_BUDGET)
                            self.assertTrue(all(p.exists() for p in paths["open"]))
                            if not any(p.exists() for p in paths["free"]):
                                break
                        self.assertEqual(len(passes), 2, passes)
                        self.assertGreater(passes[0]["deferred"], 0)
                        self.assertEqual(passes[1]["removed"], 2, passes)
                        self.assertLess(passes[1]["delivery_reads"], 32)
                        print(json.dumps({"g03_log_cadence": groups, "writer_group": writer_group, "passes": passes}))
                finally:
                    case.doCleanups()

    def test_g03_publication_before_admission_keeps_log_and_prompt(self):
        for kind, relation in (("log", "attempt"), ("prompt", "parent")):
            with self.subTest(kind=kind, relation=relation):
                case = DispatchLogRetentionTest(); case.setUp()
                try:
                    attempt = "att-g03-before"
                    log = case.logs / f"job.{attempt}.codex.jsonl"
                    target = case.log(log.name if kind == "log" else f"job.{attempt}.codex.prompt.txt")
                    case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
                    original = R._fresh_eligible
                    @contextlib.contextmanager
                    def publish_then_admit(*args, **kwargs):
                        case.publish(attempt, relation=relation)
                        with original(*args, **kwargs) as admitted:
                            yield admitted
                    with mock.patch.object(R, "_fresh_eligible", publish_then_admit):
                        result = case.sweep()
                    self.assertEqual(result["removed"], 0, result)
                    self.assertTrue(target.exists())
                finally:
                    case.doCleanups()

    def test_g03_reference_exclusion_lasts_through_quarantine_and_unlink(self):
        import dispatch_attempt_reference as reference
        for kind, relation in (("log", "attempt"), ("prompt", "parent")):
            with self.subTest(kind=kind, relation=relation):
                case = DispatchLogRetentionTest(); case.setUp()
                try:
                    attempt = "att-g03-delete-first"
                    log = case.logs / f"job.{attempt}.codex.jsonl"
                    target = case.log(log.name if kind == "log" else f"job.{attempt}.codex.prompt.txt")
                    case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
                    start, attempted, done = threading.Event(), threading.Event(), threading.Event()
                    errors, order = [], []
                    real_open, real_move, real_unlink = reference._open_locked, SHARED.os.rename, SHARED.os.unlink
                    def observed_open(path, *, blocking):
                        if threading.current_thread().name == "g03-publisher":
                            attempted.set()
                        return real_open(path, blocking=blocking)
                    def publisher():
                        start.wait()
                        try:
                            case.publish(attempt, relation=relation)
                            order.append("published")
                        except Exception as exc:
                            errors.append(repr(exc))
                        finally:
                            done.set()
                    writer = threading.Thread(target=publisher, name="g03-publisher")
                    def move(name, *args, **kwargs):
                        if name == target.name:
                            start.set()
                            self.assertTrue(attempted.wait(3))
                            self.assertFalse(done.is_set())
                            # An unrelated publisher completes while the exact
                            # candidate's reference admission remains held.
                            with ThreadPoolExecutor(max_workers=1) as pool:
                                pool.submit(case.publish, "att-g03-unrelated",
                                            recipient="g03-other", number=2).result(timeout=3)
                            order.append("quarantine")
                        return real_move(name, *args, **kwargs)
                    def unlink(name, *args, **kwargs):
                        result = real_unlink(name, *args, **kwargs)
                        if name == target.name:
                            self.assertFalse(done.is_set(), "related publication passed before unlink")
                            order.append("unlink")
                        return result
                    writer.start()
                    try:
                        with mock.patch.object(reference, "_open_locked", observed_open), \
                                mock.patch.object(SHARED.os, "rename", move), \
                                mock.patch.object(SHARED.os, "unlink", unlink):
                            result = case.sweep()
                    finally:
                        start.set(); writer.join(5)
                    self.assertFalse(writer.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(result.get("removed"), 1, result)
                    self.assertEqual(order, ["quarantine", "unlink", "published"])
                    self.assertFalse(target.exists())
                finally:
                    case.doCleanups()

    def test_g03_related_terminal_activity_extends_only_bound_log_and_prompt(self):
        for harness in ("claude", "codex", "opencode"):
            for state in ("acked", "expired", "rejected"):
                with self.subTest(harness=harness, state=state):
                    case = DispatchLogRetentionTest(); case.setUp()
                    try:
                        paths = {}
                        for suffix in ("related", "free"):
                            attempt = "att-g03-terminal-" + suffix
                            log = case.log(f"job.{attempt}.{harness}.jsonl")
                            prompt = case.log(f"job.{attempt}.{harness}.prompt.txt")
                            case.add_row(attempt, log, harness=harness, ended=case.now - 30 * DAY)
                            paths[suffix] = (log, prompt)
                        with VirtualDeliveries(case.root, case.now, count=1,
                                terminal_ref="att-g03-terminal-related", terminal_state=state):
                            first = R.sweep(case.jobs, now_ns=case.now)
                            self.assertEqual(first["removed"], 2, first)
                            self.assertTrue(all(p.exists() for p in paths["related"]))
                            self.assertTrue(all(not p.exists() for p in paths["free"]))
                            second = R.sweep(case.jobs, now_ns=case.now + 15 * DAY)
                            self.assertEqual(second["removed"], 2, second)
                    finally:
                        case.doCleanups()

    def _r1_log_case(self):
        case = DispatchLogRetentionTest(); case.setUp()
        attempt = "att-r1-log-matrix"
        log = case.log(f"job.{attempt}.codex.jsonl")
        prompt = case.log(f"job.{attempt}.codex.prompt.txt")
        case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
        return case, attempt, [log, prompt]

    def _r1_assert(self, outcome):
        self.assertEqual(outcome["exists"], [True, True], outcome)
        self.assertEqual(outcome["record_state"], "pending", outcome)
        self.assertEqual(outcome["second"].get("removed"), 0, outcome)
        self.assertEqual(outcome["token_unchanged"], outcome["pinned"], outcome)

    def test_g03_r1_publication_timing_matrix_log(self):
        """R1: an old (pinned) or current writer publishing around a bounded observation keeps log and prompt."""
        for timing in ("T1", "T2", "T3", "T4"):
            for pinned in (True, False):
                for relation in ("attempt", "parent"):
                    for new_group in (False, True):
                        with self.subTest(timing=timing, pinned=pinned, relation=relation, new_group=new_group):
                            case, attempt, paths = self._r1_log_case()
                            try:
                                outcome = _HOME_FIXTURES.r1_publication_run(
                                    case, attempt=attempt, paths=paths, sweep=R.sweep, at=case.now,
                                    history_time=case.now - 40 * DAY, timing=timing, pinned=pinned,
                                    relation=relation, new_group=new_group)
                                self._r1_assert(outcome)
                                print(json.dumps({"r1_log": outcome}))
                            finally:
                                case.doCleanups()

    def test_g03_r1_large_single_pass_keeps_log_and_prompt(self):
        for pinned in (True, False):
            for relation in ("attempt", "parent"):
                with self.subTest(pinned=pinned, relation=relation):
                    case, attempt, paths = self._r1_log_case()
                    try:
                        outcome = _HOME_FIXTURES.r1_publication_run(
                            case, attempt=attempt, paths=paths, sweep=R.sweep, at=case.now,
                            history_time=case.now, timing="L1", pinned=pinned, relation=relation,
                            new_group=False, real_history=120)
                        self.assertGreater(outcome["second"].get("delivery_reads", 0), 33, outcome)
                        self._r1_assert(outcome)
                        print(json.dumps({"r1_log_large_single": outcome}))
                    finally:
                        case.doCleanups()

    def test_g03_r1_unrelated_publications_never_stretch_the_log_observation(self):
        home = _HOME_FIXTURES
        case = DispatchLogRetentionTest(); case.setUp()
        try:
            paths = {}
            for suffix in ("free", "open"):
                attempt = f"att-r1-log-{suffix}"
                log = case.log(f"job.{attempt}.codex.jsonl")
                prompt = case.log(f"job.{attempt}.codex.prompt.txt")
                paths[suffix] = (log, prompt)
                case.add_row(attempt, log, harness="codex", ended=case.now - 30 * DAY)
            keys = [f"r1-log-{i}" for i in range(3)]
            unrelated = ["r1-log-unrelated-0", "r1-log-unrelated-1"]
            for index, key in enumerate(unrelated):
                home.Fixture.publish_delivery(case, "delivery-" + f"{index + 1:032x}", key, ["att-unrelated-seed"], "-")
            serial = [100]

            def publish(count=1):
                for _ in range(count):
                    serial[0] += 1
                    home.Fixture.publish_delivery(case, "delivery-" + f"{serial[0]:032x}",
                                                  unrelated[serial[0] % 2], [f"att-unrelated-{serial[0]}"], "-")

            with VirtualDeliveries(case.root, case.now - 40 * DAY, groups=3, recipient_keys=keys,
                                   open_refs=[(["att-child"], "att-r1-log-open")]) as history:
                real_page, real_scan = home.R._directory_page, home.R._admission_scan

                def page(path, cookie, limit, deadline):
                    entries, eof, stamp = real_page(path, cookie, limit, deadline)
                    if Path(path) in history.paths and eof:
                        publish()
                    return entries, eof, stamp

                scans = []

                def scan(ctx, deadline):
                    writer = threading.Thread(target=publish)
                    writer.start(); writer.join(5)
                    self.assertFalse(writer.is_alive())
                    scans.append(real_scan(ctx, deadline))
                    return scans[-1]

                passes = []
                with mock.patch.object(home.R, "_directory_page", page), \
                        mock.patch.object(home.R, "_admission_scan", scan):
                    for i in range(5):
                        result = R.sweep(case.jobs, now_ns=case.now + i * 301 * 10**9, force=True)
                        passes.append(result)
                        self.assertLessEqual(result["delivery_reads"], home.R.DELIVERY_READ_BUDGET, result)
                        self.assertTrue(all(p.exists() for p in paths["open"]))
                        if not paths["free"][0].exists():
                            break
                        publish(5)
                self.assertTrue(all(not p.exists() for p in paths["free"]), passes)
                self.assertLessEqual(len(passes), 3, passes)
                self.assertEqual(scans, [None, None], scans)
        finally:
            case.doCleanups()

    def test_g03_r1_log_state_keeps_one_mark_for_hundreds_of_recipient_groups(self):
        target = self.log("r1-groups.jsonl")
        self.add_row("att-r1-log-groups", target, ended=self.now - 30 * DAY)
        with _HOME_FIXTURES.VirtualRecipientGroups(self.root, 416):
            first = R.sweep(self.jobs, now_ns=self.now, force=True)
            saved = json.loads((self.root / R.STATE_NAME).read_text())
            observation = saved.get("delivery_observation")
            if observation is not None:
                self.assertIsInstance(observation["mark"], int)
                self.assertNotIn("groups", observation)
                self.assertLess(len(json.dumps(observation)), 12288)
            second = first
            for i in range(1, 6):
                if not target.exists():
                    break
                second = R.sweep(self.jobs, now_ns=self.now + i * 301 * 10**9, force=True)
        self.assertFalse(target.exists(), (first, second))
        self.assertEqual(second["kept_reasons"].get("delivery-groups-over-budget", 0), 0)

    def test_g03_log_final_admission_does_not_scan_history_or_processes(self):
        home = _HOME_FIXTURES.R
        target = self.log("g03-critical.jsonl")
        self.add_row("att-g03-log-critical", target, ended=self.now - 30 * DAY)
        with VirtualDeliveries(self.root, self.now - 40 * DAY) as history:
            first = R.sweep(self.jobs, now_ns=self.now)
            self.assertGreater(first["deferred"], 0)
            active = [False]
            original_lock, original_page, original_read = home._registry_lock_until, home._directory_page, home._read_json
            original_probe = DC.attempt_process_quiescence
            @contextlib.contextmanager
            def lock(*args):
                with original_lock(*args) as admitted:
                    active[0] = True
                    try:
                        yield admitted
                    finally:
                        active[0] = False
            def page(*args):
                self.assertFalse(active[0], "log final admission enumerated history")
                return original_page(*args)
            def read(path, *args, **kwargs):
                if history._virtual(path):
                    self.assertFalse(active[0], "log final admission read history body")
                return original_read(path, *args, **kwargs)
            def probe(*args, **kwargs):
                self.assertFalse(active[0], "log final admission scanned processes")
                return original_probe(*args, **kwargs)
            with mock.patch.object(home, "_registry_lock_until", lock), \
                    mock.patch.object(home, "_directory_page", page), mock.patch.object(home, "_read_json", read), \
                    mock.patch.object(DC, "attempt_process_quiescence", probe):
                second = R.sweep(self.jobs, now_ns=self.now + 301 * 10**9)
            self.assertEqual(second["removed"], 1, second)
            self.assertFalse(target.exists())


class ObservationLogRotationTest(unittest.TestCase):
    def test_concurrent_appends_rotate_only_complete_json_records(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-observation-") as td:
            root = Path(td)
            with mock.patch.object(OBS, "MAX_ACTIVE_BYTES", 160), \
                    mock.patch.object(OBS, "MAX_RETAINED_BYTES", 100_000):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(
                        lambda index: OBS.append_record(root, "dispatch-session-sweep.log",
                                                        {"record": index, "payload": "x" * 18}),
                        range(120)))
            self.assertTrue(all(results))
            files = [root / "logs" / name for name in os.listdir(root / "logs")
                     if name.startswith("dispatch-session-sweep.log")
                     and not name.endswith(".lock")]
            records = []
            for path in files:
                for line in path.read_text(encoding="utf-8").splitlines():
                    records.append(json.loads(line))
            self.assertEqual(sorted(row["record"] for row in records), list(range(120)))

    def test_first_append_safely_rotates_existing_backlog_and_prunes_old_segments(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-observation-") as td:
            root = Path(td)
            logs = root / "logs"
            logs.mkdir()
            active = logs / "dispatch-pending-delivery.log"
            active.write_bytes(b"old backlog\n" * 20)
            old_segment = logs / "dispatch-pending-delivery.log.segment.old"
            old_segment.write_text("expired segment\n", encoding="utf-8")
            expired = time.time_ns() - 20 * DAY
            os.utime(old_segment, ns=(expired, expired))

            with mock.patch.object(OBS, "MAX_ACTIVE_BYTES", 64):
                self.assertTrue(OBS.append_record(root, "dispatch-pending-delivery.log",
                                                  {"event": "new"}))

            self.assertFalse(old_segment.exists())
            segments = list(logs.glob("dispatch-pending-delivery.log.segment.*"))
            self.assertTrue(any(b"old backlog" in path.read_bytes() for path in segments))
            self.assertEqual(json.loads(active.read_text(encoding="utf-8")), {"event": "new"})

    def test_segment_swap_during_quarantine_keeps_new_writer(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-observation-swap-") as td:
            logs = Path(td) / "logs"
            logs.mkdir()
            segment = logs / "dispatch-session-sweep.log.segment.expired"
            segment.write_text("old segment\n", encoding="utf-8")
            expected = OBS._stamp(os.stat(segment, follow_symlinks=False))
            logs_fd = os.open(logs, os.O_RDONLY | os.O_DIRECTORY)
            self.addCleanup(os.close, logs_fd)
            original_unlink = os.unlink
            created = []

            def create_new_segment(name, *args, dir_fd=None, **kwargs):
                if (name == segment.name and dir_fd is not None
                        and os.fstat(dir_fd).st_ino != os.fstat(logs_fd).st_ino and not created):
                    segment.write_text("new segment\n", encoding="utf-8")
                    created.append(True)
                return original_unlink(name, *args, dir_fd=dir_fd, **kwargs)

            with mock.patch.object(SHARED.os, "unlink", side_effect=create_new_segment):
                self.assertTrue(OBS._unlink_segment(logs_fd, segment.name, expected))

            self.assertTrue(created)
            self.assertEqual(segment.read_text(encoding="utf-8"), "new segment\n")


if __name__ == "__main__":
    unittest.main()
