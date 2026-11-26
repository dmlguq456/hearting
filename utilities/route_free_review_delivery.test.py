#!/usr/bin/env python3
"""Ordinary registered reviews reach successful parent delivery after drain."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import dispatch_contract as D
import dispatch_completion_join as JOIN

HERE = Path(__file__).resolve().parent


class OrdinaryReviewDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / ".agent_reports"
        self.root.mkdir()
        # A registered reviewer inherits the caller's canonical artifact root.
        # Bind both the inspector and real reaper to this fixture instead.
        environment = mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.report = self.root / "review.md"
        self.report.write_text("## Verdict: PASS\nNo blocking findings.\n")
        self.log = self.base / "review.jsonl"
        self.jobs = self.base / "jobs.log"
        self.attempt = "att-ordinary-review"
        self.write_log()

    def write_log(self, verdict="PASS", artifact=None, completed=True):
        rows = [{"type": "item.completed", "item": {
            "type": "agent_message", "text":
            f"artifact: {artifact or self.report}\nverdict: {verdict}\nblocker: none"}}]
        if completed:
            rows.append({"type": "turn.completed"})
        self.log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    def write_row(self, *, harness="codex", identity=None, **extra):
        metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "1",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "fallback_hop": "same-harness-headless",
            "worker_type": "review", "launch_lifecycle": "detached",
            "attempt_id": self.attempt, "harness": harness,
            "parent_attempt_id": "att-review-parent",
            "parent_sid": "session-review-parent", "parent_cwd": str(self.base),
            "parent_completion_delivery": "codex-managed-gateway",
            "artifact_root": str(self.root), "log_file": str(self.log),
            "pid": "99999997", "pid_start": "456", "pgid": "99999997",
            "pid_ns": os.readlink("/proc/self/ns/pid"),
            "pid_observer_ns": os.readlink("/proc/self/ns/pid"),
            **(identity or {}), **extra,
        }
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        self.jobs.write_text(f"2026-10-08T00:00:00Z\topen\t{self.base}\t{self.base}\treview\t{pipe}\n")
        return JOIN.exact_attempt_row(self.jobs, self.attempt)

    def classify(self, row, state="quiescent"):
        return JOIN.classify_exact_route_free_review_outcome(
            row, jobs=self.jobs, expected_attempt_id=self.attempt,
            expected_pid=int(row.metadata["pid"]),
            expected_pid_start=row.metadata["pid_start"],
            expected_pgid=int(row.metadata["pgid"]),
            quiescence=D.ProcessQuiescence(state, "fixture"))

    def test_ordinary_pass_completes_without_watchdog_or_producer_binding(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                result = self.classify(self.write_row(harness=harness))
                self.assertEqual((result.note, result.close_action), ("completed-review", "wrapper-pass"))

    def test_live_and_unobservable_reviews_stay_pending_even_with_pass(self):
        for state in ("live", "unverifiable"):
            result = self.classify(self.write_row(), state)
            self.assertEqual(result.close_action, "pending")

    def test_watchdog_and_partial_binding_still_require_process_outcome(self):
        for key in ("review_admission", "review_cycle_id", "review_producer_id",
                    "review_output_locator_b64", "review_watchdog_budget_digest"):
            with self.subTest(key=key):
                result = self.classify(self.write_row(**{key: "prepared"}))
                self.assertEqual(result.note, "dead-review-process-outcome-missing")
                self.assertEqual(result.close_action, "typed-close")

    def test_malformed_watchdog_outcome_cannot_be_an_ordinary_pass(self):
        result = self.classify(self.write_row(review_process_outcome_b64="bad"))
        self.assertEqual(result.note, "dead-foreground-outcome-malformed")

    def test_foreground_review_still_waits_for_the_sealed_exit(self):
        result = self.classify(self.write_row(launch_lifecycle="foreground-scoped"))
        self.assertEqual(result.close_action, "pending")

    def test_readable_blocking_review_keeps_its_fail_verdict(self):
        self.report.write_text("## Verdict: FAIL\nBlocking finding.\n")
        self.write_log("FAIL")
        result = self.classify(self.write_row())
        self.assertEqual(result.note, "completed-review-blocking")
        self.assertEqual(result.close_action, "typed-close")

    def test_absent_incomplete_or_unreadable_result_cannot_pass(self):
        for kind in ("absent", "incomplete", "unreadable", "conflicting-artifact"):
            with self.subTest(kind=kind):
                self.report.write_text("## Verdict: PASS\n")
                self.write_log(completed=kind != "incomplete")
                if kind == "absent":
                    self.log.write_text("")
                if kind == "unreadable":
                    self.report.unlink()
                if kind == "conflicting-artifact":
                    self.report.write_text("## Verdict: FAIL\n")
                result = self.classify(self.write_row())
                self.assertNotEqual(result.note, "completed-review")

    def test_real_reaper_delivers_success_for_all_three_harness_rows(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                worker = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
                identity = D.process_launch_identity(worker.pid)
                self.write_row(harness=harness, identity=identity)
                worker.wait(timeout=5)
                result = subprocess.run([sys.executable, str(HERE / "dispatch-reap-watch.py"),
                    "--jobs", str(self.jobs), "--attempt-id", self.attempt,
                    "--pid", identity["pid"], "--pid-start", identity["pid_start"],
                    "--pgid", identity["pgid"], "--interval", "0.001"],
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = JOIN.exact_attempt_row(self.jobs, self.attempt)
                self.assertEqual((row.status, row.metadata["note"]), ("done", "completed-review"))
                self.assertEqual(row.metadata["group_reap_proof"], D.GROUP_REAP_PROOF)
                state = JOIN.current_delivery_state(self.jobs, self.attempt, parent_attempt_id="att-review-parent")
                self.assertEqual(JOIN.delivery_classification(state), "success")
                self.assertEqual(JOIN.delivery_required_action(state), "advance-completed")


if __name__ == "__main__":
    unittest.main()
