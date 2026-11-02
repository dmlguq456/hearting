"""Isolated safety and cadence checks for dispatch log retention."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
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

        def slow(ctx, attempt, *, probe_processes):
            clock[0] += R.MAX_PASS_SECONDS + 1
            return actual(ctx, attempt, probe_processes=probe_processes)

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

        second = self.sweep()
        self.assertEqual(second["removed"], 1)
        self.assertFalse(old.exists())
        self.assertFalse(list(self.logs.glob(SHARED.QUARANTINE_PREFIX + "*/*")))


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
