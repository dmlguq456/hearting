#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import importlib.util
import json
import os
import select
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import shared_readiness as READINESS  # noqa: E402
from dispatch_contract import process_start_ticks  # noqa: E402

_ACCEPTANCE_SPEC = importlib.util.spec_from_file_location(
    "shared_readiness_acceptance_reader",
    HERE / "shared_readiness_acceptance.test.py")
if _ACCEPTANCE_SPEC is None or _ACCEPTANCE_SPEC.loader is None:
    raise RuntimeError("cannot load the shared-readiness acceptance reader")
ACCEPTANCE = importlib.util.module_from_spec(_ACCEPTANCE_SPEC)
_ACCEPTANCE_SPEC.loader.exec_module(ACCEPTANCE)


class SharedReadinessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "peer-steward" / "obligations").mkdir(parents=True)
        self.start = str(process_start_ticks(os.getpid()))
        self.identity = READINESS.attempt_identity(
            self.root / "jobs.log", "att-one", parent="parent", slug="worker",
            consumer_pid=os.getpid(), consumer_start=self.start,
            request_token="test-request")
        READINESS.publish_runner_incarnation(
            self.root / "peer-steward" / "obligations", os.getpid(), self.start,
            "runner-test")

    def projection(self, *, result=None, now=None, producer_pid=None, producer_start=None):
        pid = os.getpid() if producer_pid is None else producer_pid
        start = self.start if producer_start is None else producer_start
        return READINESS.make_projection(
            self.identity, result or {"state": "ready", "children": []},
            producer_pid=pid, producer_start=start, incarnation="runner-test",
            generation=[1, 2, 3], now=now)

    def generated_normal_authority(self, row_count=72):
        rows = []
        extensions = {f"extension_{index:03d}": f"generated-value-{index:03d}"
                      for index in range(127)}
        for index in range(row_count):
            metadata = {
                "attempt_id": f"att-generated-related-{index:03d}",
                "attempt_schema_version": "2",
                "dispatch_depth": "1",
                "worker_type": "review",
                "registered_worker": "1",
                "execution_surface": "registered-headless",
                "harness": "claude",
                "parent_sid": "generated-parent-session",
                "parent_harness": "claude",
                "route_id": "rt-generated-normal-shape",
                "route_hash": "sha256:" + "a" * 64,
                "route_node": "review",
                "note": "completed-marker",
                "failure_class": "pass",
                "receipt_state": "settled",
                "cleanup_pending": "0",
                "generated_extension_detail": "x" * 2400,
                **extensions,
            }
            raw_metadata = ",".join(f"{key}={value}" for key, value in metadata.items())
            fields = ["2026-10-20T00:00:00Z", "done", "/generated/repo",
                      "/generated/worktree", f"review-{index:03d}", raw_metadata]
            rows.append({"status": "done", "metadata": metadata,
                         "completion_metadata": dict(metadata), "fields": fields})
        return {"rows": rows, "handover_keys": []}

    def test_request_identity_separates_consumer_and_attempt_selection(self):
        other_consumer = READINESS.attempt_identity(
            self.root / "jobs.log", "att-one", parent="parent", slug="worker",
            consumer_pid=os.getpid(), consumer_start=self.start,
            request_token="other-request")
        other_attempt = READINESS.attempt_identity(
            self.root / "jobs.log", "att-two", consumer_pid=os.getpid(),
            consumer_start=self.start, request_token="test-request")
        self.assertNotEqual(READINESS.duty_id(self.identity), READINESS.duty_id(other_consumer))
        self.assertNotEqual(READINESS.duty_id(self.identity), READINESS.duty_id(other_attempt))

    def test_projection_is_small_and_requires_exact_fresh_live_bindings(self):
        value = self.projection(result={
            "state": "pending", "children": [{"attempt_id": "att-one", "readiness": "pending",
                "status": "running", "metadata": {"large": "private row"}}]})
        self.assertTrue(READINESS.projection_is_current(value, self.identity))
        self.assertNotIn("metadata", value["result"]["children"][0])
        self.assertFalse(READINESS.projection_is_current(value, {**self.identity, "request_token": "other"}))
        self.assertFalse(READINESS.projection_is_current(
            value, self.identity, now=value["observed_at"] + READINESS.MAX_AGE_SECONDS + 1))

    def test_target_projection_is_shared_only_for_exact_shell_and_generation(self):
        jobs = self.root / "jobs.log"
        jobs.write_text("registry\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="server-a", endpoint="/tmp/herdr.sock", pane="w1:p1",
            harness="claude", session_id="sid-a", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        changed = READINESS.publish_target_projection(
            self.root / "peer-steward" / "obligations", identity,
            {"state": "pending", "reason": "bound-registered-work-pending"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation)
        self.assertTrue(changed)
        result = READINESS.target_result(
            self.root / "peer-steward" / "obligations", identity)
        self.assertEqual(result["state"], "pending")
        jobs.write_text("registry changed\n", encoding="utf-8")
        self.assertIsNone(READINESS.target_result(
            self.root / "peer-steward" / "obligations", identity))

    def test_registry_absence_is_explicit_and_invalidated_when_work_registry_appears(self):
        jobs = self.root / "dispatch" / "jobs.log"
        jobs.parent.mkdir()
        identity = READINESS.target_identity(
            server="server-a", endpoint="/tmp/herdr.sock", pane="w1:p1",
            harness="claude", session_id="sid-a", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        projection_root = self.root / "peer-steward" / "obligations"
        with mock.patch.dict(os.environ, {
                "AGENT_HOME": str(HERE.parent),
                "AGENT_DISPATCH_JOBS": str(jobs)}):
            self.assertFalse(READINESS._generation_current(None),
                             "an unspecified generation is not a wildcard")
            absent = READINESS.registry_generation(jobs, None, selected=True)
            self.assertIsNotNone(absent, "a freshly observed absent registry is explicit")
            self.assertTrue(READINESS._generation_current(absent))

            READINESS.publish_target_projection(
                projection_root, identity, {"state": "ready", "reason": "native-ready"},
                producer_pid=os.getpid(), producer_start=self.start,
                incarnation="runner-test", generation=absent,
                lineage={"shell_pid": os.getpid(), "shell_start": self.start})
            self.assertEqual(READINESS.target_result(projection_root, identity)["state"],
                             "ready")

            jobs.write_text("registry\n", encoding="utf-8")
            self.assertIsNone(READINESS.target_result(projection_root, identity),
                              "creating the selected registry invalidates observed absence")

            self.assertIsNone(READINESS.registry_generation(jobs, None),
                              "a present but unread registry is not observed absence")
            directory = self.root / "dispatch" / "not-a-registry"
            directory.mkdir()
            self.assertIsNone(READINESS.registry_generation(directory, None),
                              "a non-file registry path is unknown")
            with mock.patch.object(Path, "lstat", side_effect=PermissionError("denied")):
                self.assertIsNone(READINESS.registry_generation(
                    self.root / "dispatch" / "unreadable.log", None),
                    "permission errors remain unknown")

    def test_runner_wake_is_an_event_on_the_existing_state_root(self):
        root = self.root / "peer-steward" / "obligations"
        self.assertFalse(READINESS.wake_runner(root))
        wake_fd = READINESS.open_runner_wake(root)
        self.addCleanup(os.close, wake_fd)
        self.assertTrue(READINESS.wake_runner(root))
        readable, _, _ = select.select([wake_fd], [], [], 0.1)
        self.assertEqual(readable, [wake_fd])
        self.assertEqual(os.read(wake_fd, 4096), b"\0")

    def test_readiness_recovery_does_not_recreate_a_removed_state_root(self):
        state_root = self.root / "removed-state"
        obligations = state_root / "peer-steward" / "obligations"
        (obligations / "readiness-v1").mkdir(parents=True)
        state_root.rename(state_root.with_name("removed-state-held"))

        with self.assertRaises(OSError):
            READINESS.publish_runner_incarnation(obligations, os.getpid(), self.start,
                                                 "old-runner")
        with self.assertRaises(OSError):
            READINESS.open_runner_wake(obligations)
        self.assertFalse(state_root.exists())

    def test_unverifiable_producer_and_oversized_result_never_publish_ready(self):
        bad = self.projection(producer_start="1")
        self.assertFalse(READINESS.projection_is_current(bad, self.identity))
        large = self.projection(result={"state": "ready", "children": [
            {"attempt_id": str(index), "reason": "x" * 100_000} for index in range(20)
        ]})
        self.assertEqual(large["result"]["state"], "unknown")
        self.assertEqual(large["result"]["reason"], "readiness-projection-too-large")

    def test_realistic_selected_authority_round_trips_over_small_result_limit(self):
        authority = self.generated_normal_authority()
        sample = authority["rows"][0]
        encoded = json.dumps(authority, sort_keys=True, separators=(",", ":")).encode("ascii")
        self.assertGreater(len(sample["metadata"]), 64)
        self.assertGreater(len(sample["fields"][5]), 4096)
        self.assertGreater(max(map(len, sample["metadata"].values())), 2048)
        self.assertEqual(sample["metadata"], sample["completion_metadata"])
        self.assertGreater(len(encoded), 65536)
        self.assertLess(len(encoded), READINESS.MAX_WATCH_AUTHORITY_BYTES)
        self.assertGreater(len(authority["rows"]), 64)
        self.assertTrue(READINESS.valid_watch_authority(authority))

        jobs = self.root / "jobs.log"
        jobs.write_text("generated registry fixture\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p1",
            harness="claude", session_id="sid-generated", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        root = self.root / "peer-steward" / "obligations"
        changed = READINESS.publish_watch_transition(
            root, "watch-generated-large-authority", identity, "generated-token",
            {"state": "ready", "reason": "native-turn-ready"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation,
            lineage={"shell_pid": os.getpid(), "shell_start": self.start},
            authority=authority)
        self.assertTrue(changed)
        path = READINESS._watch_transition_path(root, "watch-generated-large-authority")
        self.assertGreater(path.stat().st_size, 65536)
        result = READINESS.watch_transition_result(
            root, "watch-generated-large-authority", identity, "generated-token")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["authority"], authority)

    def test_authority_over_total_transport_bound_stays_unknown(self):
        oversized = {"rows": [{
            "status": "done",
            "metadata": {"generated_extension_detail": "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES},
            "completion_metadata": {"generated_extension_detail": "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES},
            "fields": ["now", "done", "/repo", "/worktree", "review",
                       "generated_extension_detail=" + "x" * READINESS.MAX_WATCH_AUTHORITY_BYTES],
        }], "handover_keys": []}
        self.assertFalse(READINESS.valid_watch_authority(oversized))
        jobs = self.root / "jobs.log"
        jobs.write_text("generated registry fixture\n", encoding="utf-8")
        info = jobs.stat()
        generation = {"path": str(jobs), "resolved": str(jobs.resolve()),
                      "signature": [info.st_dev, info.st_ino, info.st_size,
                                    info.st_mtime_ns, info.st_ctime_ns]}
        identity = READINESS.target_identity(
            server="fixture", endpoint="/tmp/fixture.sock", pane="w1:p1",
            harness="claude", session_id="sid-generated", shell_pid=os.getpid(),
            shell_start=self.start, native_state="idle")
        root = self.root / "peer-steward" / "obligations"
        READINESS.publish_watch_transition(
            root, "watch-overlarge-authority", identity, "oversized-token",
            {"state": "ready", "reason": "native-turn-ready"},
            producer_pid=os.getpid(), producer_start=self.start,
            incarnation="runner-test", generation=generation,
            lineage={"shell_pid": os.getpid(), "shell_start": self.start},
            authority=oversized)
        result = READINESS.watch_transition_result(
            root, "watch-overlarge-authority", identity, "oversized-token")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["reason"], "shared-readiness-authority-unbounded")
        self.assertNotIn("authority", result)


class SharedReadinessAcceptanceReaderTest(unittest.TestCase):
    @staticmethod
    def valid_six_watch_fixture():
        watch_ids = [f"watch-{index}" for index in range(6)]
        return {
            "case": ACCEPTANCE.SIX_WATCH_LIFECYCLE_CASE,
            "watch_count": 6,
            "same_target_count": 5,
            "distinct_stewards": 6,
            "runner_process_count": 1,
            "runner_session_snapshot_calls": 4,
            "native_result_counts": {"idle": 5, "done": 1},
            "observation_groups": [
                {"pane": "w1:p1", "session_id": "sid-a", "native_state": "done",
                 "watch_count": 1, "watch_ids": [watch_ids[0]]},
                {"pane": "w1:p1", "session_id": "sid-a", "native_state": "idle",
                 "watch_count": 4, "watch_ids": watch_ids[1:5]},
                {"pane": "w1:p2", "session_id": "sid-b", "native_state": "idle",
                 "watch_count": 1, "watch_ids": [watch_ids[5]]},
            ],
            "receipt_records": {watch_id: True for watch_id in watch_ids},
            "notice_records": {watch_id: 1 for watch_id in watch_ids},
            "ack_records": {watch_id: True for watch_id in watch_ids},
            "receipt_count": 6,
            "ack_count": 6,
            "first_ack_isolation": True,
            "remaining_independent_receipts": 6,
            "notice_transfer_refs_present": {watch_id: True for watch_id in watch_ids},
            "acknowledged_watch": watch_ids[0],
            "target_state_before_after": ["working", "idle"],
            "jobs_sha256_before_after": ["unchanged-jobs", "unchanged-jobs"],
        }

    @classmethod
    def lifecycle_evidence(cls, fixtures):
        return {
            "tests_run": 34,
            "suite_exit_code": 0,
            "failures": [],
            "errors": [],
            "fixtures": fixtures,
            "selected_regressions": [
                "SharedReadinessRunnerTest.test_runner_reads_once_for_five_stewards_and_separates_another_pane"
            ],
        }

    @staticmethod
    def lifecycle_cleanup():
        return {
            "status": "pass",
            "results": [{
                "runner_pid": 123,
                "runner_reaped": True,
                "runner_lock_fd_closed": True,
                "watchers": [{"reaped": True} for _ in range(6)],
            }],
        }

    def test_selects_six_watch_case_after_unrelated_fixture_and_reports_it(self):
        target = self.valid_six_watch_fixture()
        unrelated = {"case": "stale-runner-snapshot-wakes-current-detached-observer",
                     "receipt_count": 1}
        lifecycle = self.lifecycle_evidence([unrelated, target])

        self.assertEqual(ACCEPTANCE._lifecycle_errors(lifecycle), [])
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            baseline.mkdir()
            report, _acceptance_path, handoff_path = ACCEPTANCE._assemble_report(
                root / "evidence", baseline, {"exit_code": 0}, {"exit_code": 0},
                {}, {"status": "pass"}, lifecycle, self.lifecycle_cleanup(),
                {"status": "pass", "readings": {}, "fixture": {}})

            selected = report["d3_independent_delivery"]["fixture"]
            self.assertEqual(selected["case"], ACCEPTANCE.SIX_WATCH_LIFECYCLE_CASE)
            self.assertEqual(report["d3_independent_delivery"]["validation_errors"], [])
            self.assertEqual(report["d2_shared_observation"]["runner_session_snapshot_calls"], 4)
            self.assertEqual(report["d2_shared_observation"]["observation_groups"],
                             target["observation_groups"])
            handoff = handoff_path.read_text(encoding="utf-8")
            self.assertIn("Lifecycle target groups:", handoff)
            self.assertIn("It recorded 6 watches across 6 stewards.", handoff)

    def test_missing_or_duplicate_six_watch_identity_is_rejected(self):
        target = self.valid_six_watch_fixture()
        unrelated = {"case": "unrelated-lifecycle-case"}
        with self.subTest("missing target"):
            errors = ACCEPTANCE._lifecycle_errors(
                self.lifecycle_evidence([unrelated]))
            self.assertIn("six-watch lifecycle fixture is missing", errors)
        with self.subTest("duplicate target"):
            errors = ACCEPTANCE._lifecycle_errors(
                self.lifecycle_evidence([target, dict(target)]))
            self.assertIn("six-watch lifecycle fixture identity is duplicated or ambiguous",
                          errors)

        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            baseline = root / "baseline"
            baseline.mkdir()
            for name, fixtures in (("missing", [unrelated]),
                                   ("duplicate", [target, dict(target)])):
                with self.subTest("report remains incomplete", selection=name):
                    report, _acceptance_path, _handoff_path = ACCEPTANCE._assemble_report(
                        root / name, baseline, {"exit_code": 0}, {"exit_code": 0},
                        {}, {"status": "pass"}, self.lifecycle_evidence(fixtures),
                        self.lifecycle_cleanup(),
                        {"status": "pass", "readings": {}, "fixture": {}})
                    self.assertEqual(report["d3_independent_delivery"]["status"],
                                     "incomplete")
                    self.assertEqual(report["d3_independent_delivery"]["fixture"], {})

    def test_selected_target_still_has_strict_six_watch_field_checks(self):
        target = self.valid_six_watch_fixture()
        target["watch_count"] = 5
        target["runner_process_count"] = 2
        target["receipt_records"]["watch-0"] = False
        errors = ACCEPTANCE._lifecycle_errors(
            self.lifecycle_evidence([{"case": "unrelated"}, target]))

        self.assertIn("lifecycle fixture did not exercise five shared watches and one control",
                      errors)
        self.assertIn("lifecycle fixture did not observe exactly one shared runner", errors)
        self.assertIn("six independent receipt files were not verified", errors)


if __name__ == "__main__":
    unittest.main()
