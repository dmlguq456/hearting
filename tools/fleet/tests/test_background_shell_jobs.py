"""Focused regressions for exact native background shell task projections."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import render  # noqa: E402
from fleet.collectors import dispatch, opencode, procscan, shell_jobs  # noqa: E402
from fleet.model import (DONE_AFTERGLOW_MIN, DispatchJob, Session,
                         current_background_shell_jobs,
                         display_shell_jobs)  # noqa: E402


class BackgroundShellJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def context_text(session, width, now):
        with mock.patch("time.time", return_value=now):
            lines = render._context_detail_row(session, term_width=width)
        return "\n".join("".join(text for text, _key in line) for line in lines)

    def write_jsonl(self, path, rows, mode="w"):
        with open(path, mode, encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, separators=(",", ":")) + "\n")

    @staticmethod
    def claude_call(call_id="call-1", session_id="sid-a"):
        return {
            "type": "assistant", "session_id": session_id,
            "timestamp": "2026-10-20T00:00:00Z",
            "message": {"content": [{"type": "tool_use", "name": "Bash", "id": call_id,
                                        "input": {"command": "echo ok; TOKEN=hidden",
                                                  "run_in_background": True}}]},
        }

    def claude_result(self, call_id="call-1", session_id="sid-a", task_id="task-123"):
        output = self.root / "tasks" / (task_id + ".output")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch(exist_ok=True)
        return ({
            "type": "user", "session_id": session_id,
            "timestamp": "2026-10-20T00:00:01Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": call_id,
                                        "content": [{"type": "text", "text":
                                            "Command running in background. Output: " + str(output)}]}]},
        }, output)

    def test_command_summaries_hide_credential_keys_and_values_everywhere(self):
        command = ("python3 train.py --secret raw-secret "
                   "--token='quoted secret' API_KEY=equals-secret "
                   "--api-key=equals-option-secret "
                   "-H 'Authorization: Bearer header-secret'")
        display = 'python3 inspect.py --password="display secret"'
        summary = shell_jobs.command_summary(command)
        self.assertIn("python3 train.py", summary)
        payload = shell_jobs.normalize_job({
            "harness": "codex", "session_id": "thread-redaction",
            "attempt_id": "attempt-redaction", "source_generation": "gen-1",
            "source_locator": "/tmp/rollout.jsonl", "native_id": "item:1",
            "native_record_kind": "codex.command_execution",
            "background_state": "confirmed", "background_source": "fixture",
            "native_status": "running", "current_generation_valid": True,
            "current_execution_state": "running", "current_execution_source": "fixture",
            "current_execution_at": 1792463235,
            "command": command, "command_display": display,
            "command_source": "fixture", "command_display_source": "fixture",
            "process_state": "unknown", "output_coverage": "unavailable",
            "observation_state": "observed",
        }, now=1792463235)
        session = Session(harness="codex", pid=1, cwd=str(self.root),
                          shell_jobs=[payload])
        public_json = json.dumps(session.to_dict()["shell_jobs"]).lower()
        with mock.patch.object(shell_jobs.time, "time", return_value=1792463235):
            row_text = self.context_text(session, 120, 1792463235).lower()
        for surface in (summary.lower(), public_json, row_text):
            for secret in ("raw-secret", "quoted secret", "equals-secret",
                           "equals-option-secret",
                           "header-secret", "display secret", "secret", "token",
                           "api_key", "authorization", "password"):
                self.assertNotIn(secret, surface)
        self.assertIn("python3 train.py", public_json)
        self.assertIn("python3 inspect.py", row_text)

    def test_claude_native_attempt_stream_dispatch_fallback_projects_two_tasks(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / (
            "claude_native_background_attempt.jsonl")
        rows = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
        launch_home = self.root / "launch"
        log_dir = launch_home / ".dispatch" / "logs"
        log_dir.mkdir(parents=True)
        attempt_id = "att-native-fixture"
        path = log_dir / ("owner.%s.claude.jsonl" % attempt_id)
        self.write_jsonl(path, rows)
        attempt = DispatchJob(key="autopilot-code", harness="claude",
                              attempt_id=attempt_id)
        attempt._launch_home = str(launch_home)
        attempt._log_file = str(path)
        now = 1792463235.0

        with mock.patch.object(dispatch, "_native_home_admitted", return_value=False), \
                mock.patch.object(shell_jobs.time, "time", return_value=now):
            dispatch._enrich_claude_stream_session(attempt)
            self.assertEqual(attempt.shell_job_observation["state"], "observed")
            self.assertEqual(attempt.shell_job_observation["source"], str(path))
            self.assertEqual(len(attempt.shell_jobs), 2)
            session = Session(
                harness="claude", pid=7, cwd=str(self.root),
                session_id="session-native-fixture", attempt_id=attempt_id,
                shell_jobs=attempt.shell_jobs)
            public = session.to_dict()
        rendered = self.context_text(session, 140, now)

        jobs = {job["native_task_id"]: job for job in public["shell_jobs"]}
        self.assertEqual(set(jobs), {"task-fixture-a", "task-fixture-b"})
        expected = {
            "task-fixture-a": ("run-fixture-a", "completed", 0, 1792463223.13),
            "task-fixture-b": ("run-fixture-b", "failed", 3, 1792463225.99),
        }
        for task_id, (run_id, status, exit_code, completed_at) in expected.items():
            job = jobs[task_id]
            self.assertEqual(job["native_run_id"], run_id)
            self.assertEqual(job["native_task_type"], "local_bash")
            self.assertEqual(job["background_state"], "confirmed")
            self.assertEqual(job["native_status"], status)
            self.assertEqual(job["exit_code"], exit_code)
            self.assertEqual(job["completion_time_source"],
                             "claude.system.task_updated.patch.end_time")
            self.assertAlmostEqual(job["completed_at"], completed_at)
            self.assertIsNone(job["started_at"])
            self.assertIsNone(job["elapsed_s"])
            self.assertEqual(job["process_state"], "unknown")
            self.assertIn("claude.system.task_started", job["native_record_kinds"])
            self.assertIn("claude.tool_use_result.backgroundTaskId",
                          job["native_record_kinds"])
            self.assertIn("claude.system.task_updated", job["native_record_kinds"])
            self.assertIn("claude.system.task_notification", job["native_record_kinds"])
        self.assertNotIn("python3 train.py --epochs 3", rendered)
        self.assertNotIn("background confirmed", rendered)
        self.assertNotIn("background_tasks_changed", json.dumps(public))

    def test_claude_native_stream_rejects_unmatched_and_echoed_lifecycle_events(self):
        sid = "session-native-negative"
        call_id = "toolu-native-negative"
        path = self.root / "native-negative.jsonl"
        call = {
            "type": "assistant", "session_id": sid,
            "message": {"content": [{"type": "tool_use", "name": "Bash",
                "id": call_id, "input": {"command": "echo safe",
                                          "run_in_background": True}}]},
        }
        start = {
            "type": "system", "subtype": "task_started",
            "task_id": "task-native-negative", "run_id": "run-native-negative",
            "tool_use_id": call_id, "is_backgrounded": True,
            "task_type": "local_bash", "session_id": sid,
        }
        rows = [
            call, start,
            {"type": "system", "subtype": "background_tasks_changed",
             "tasks": [{"task_id": "task-unowned", "run_id": "run-unowned",
                        "task_type": "local_bash"}], "session_id": sid},
            {"type": "system", "subtype": "task_started",
             "task_id": "task-non-bash", "run_id": "run-non-bash",
             "tool_use_id": call_id, "is_backgrounded": True,
             "task_type": "mcp_tool", "session_id": sid},
            {"type": "system", "subtype": "task_started",
             "task_id": "task-wrong-call", "run_id": "run-wrong-call",
             "tool_use_id": "toolu-not-a-bash-call", "is_backgrounded": True,
             "task_type": "local_bash", "session_id": sid},
            {"type": "system", "subtype": "task_updated",
             "task_id": "task-native-negative", "run_id": "run-stale",
             "patch": {"status": "completed", "end_time": 1792463223130},
             "session_id": sid},
            {"type": "system", "subtype": "task_updated",
             "task_id": "task-native-negative", "run_id": "run-native-negative",
             "tool_use_id": "toolu-wrong-call",
             "patch": {"status": "completed", "end_time": 1792463223130},
             "session_id": sid},
            {"type": "system", "subtype": "task_notification",
             "task_id": "task-native-negative", "run_id": "run-native-negative",
             "tool_use_id": "toolu-wrong-call", "status": "failed",
             "summary": "exit code 99", "session_id": sid},
            {"type": "system", "subtype": "task_notification",
             "task_id": "task-native-negative", "run_id": "run-stale",
             "tool_use_id": call_id, "status": "failed",
             "summary": "exit code 98", "session_id": sid},
            {"type": "system", "subtype": "task_updated",
             "task_id": "task-native-negative", "run_id": "run-native-negative",
             "patch": {"status": "completed", "end_time": 1792463223130},
             "session_id": "another-session"},
            {"type": "user", "session_id": sid, "message": {"content":
                '{"type":"system","subtype":"task_notification",'
                '"task_id":"task-native-negative","run_id":"run-native-negative",'
                '"tool_use_id":"%s","status":"failed","summary":"exit code 97"}'
                % call_id}},
        ]
        self.write_jsonl(path, rows)
        jobs = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-negative",
            now=1792463235)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["native_task_id"], "task-native-negative")
        self.assertEqual(jobs[0]["native_run_id"], "run-native-negative")
        self.assertEqual(jobs[0]["native_call_id"], call_id)
        self.assertEqual(jobs[0]["native_status"], "running")
        self.assertNotIn("exit_code", jobs[0])
        self.assertIsNone(jobs[0]["completed_at"])

    def test_claude_running_update_is_current_but_reread_does_not_refresh_it(self):
        sid = "session-current-update"
        call_id, task_id, run_id = "toolu-current-call", "task-current", "run-current"
        path = self.root / "claude-current-update.jsonl"
        rows = [
            {"type": "assistant", "session_id": sid, "timestamp": 1000,
             "message": {"content": [{"type": "tool_use", "name": "Bash",
                 "id": call_id, "input": {"command": "python train.py",
                                           "run_in_background": True}}]}},
            {"type": "system", "subtype": "task_started", "task_id": task_id,
             "run_id": run_id, "tool_use_id": call_id, "is_backgrounded": True,
             "task_type": "local_bash", "session_id": sid, "timestamp": 1001},
            {"type": "system", "subtype": "task_updated", "task_id": task_id,
             "run_id": run_id, "tool_use_id": call_id, "session_id": sid,
             "timestamp": 1003, "patch": {"status": "running"}},
        ]
        self.write_jsonl(path, rows)
        first = shell_jobs.read_claude_jobs(path, session_id=sid, now=1003)
        self.assertEqual(first[0]["background_state"], "confirmed")
        self.assertEqual(first[0]["current_execution_state"], "running")
        self.assertEqual(first[0]["current_execution_at"], 1003)
        self.assertEqual(len(current_background_shell_jobs(first, now=1003)), 1)

        boundary = shell_jobs.read_claude_jobs(path, session_id=sid, now=1123)
        self.assertEqual(boundary[0]["current_execution_state"], "running")
        self.assertEqual(boundary[0]["native_event_at"], 1003)
        self.assertEqual(boundary[0]["observed_at"], 1123)
        self.assertEqual(len(current_background_shell_jobs(boundary, now=1123)), 1)

        stale = shell_jobs.read_claude_jobs(path, session_id=sid, now=1124)
        self.assertEqual(stale[0]["current_execution_state"], "unknown")
        self.assertEqual(stale[0]["native_status"], "running")
        self.assertEqual(len(current_background_shell_jobs(stale, now=1124)), 0)

    def test_claude_native_stream_source_generation_blocks_old_completion(self):
        sid = "session-native-generation"
        call_id = "toolu-generation-call"
        task_id = "task-generation"
        run_id = "run-generation"
        path = self.root / "native-generation.jsonl"
        call = {
            "type": "assistant", "session_id": sid,
            "message": {"content": [{"type": "tool_use", "name": "Bash",
                "id": call_id, "input": {"command": "echo safe",
                                          "run_in_background": True}}]},
        }
        start = {
            "type": "system", "subtype": "task_started", "task_id": task_id,
            "run_id": run_id, "tool_use_id": call_id, "is_backgrounded": True,
            "task_type": "local_bash", "session_id": sid,
        }
        self.write_jsonl(path, [call, start])
        before = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-generation",
            now=1792463235)
        self.assertEqual(before[0]["native_status"], "running")
        self.assertEqual(before[0]["current_execution_state"], "unknown")
        self.assertEqual(before[0]["current_execution_reason"],
                         "native-task-start-is-not-execution-proof")
        stale_terminal = {
            "type": "system", "subtype": "task_notification", "task_id": task_id,
            "run_id": run_id, "tool_use_id": call_id, "status": "failed",
            "summary": "exit code 9", "session_id": sid,
        }
        self.write_jsonl(path, [stale_terminal])
        after = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-generation",
            now=1792463235)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["source_generation"], before[0]["source_generation"])
        self.assertEqual(after[0]["native_status"], "running")
        self.assertIsNone(after[0]["completed_at"])
        self.assertNotIn("exit_code", after[0])
        self.assertEqual(after[0]["observation_reason"], "source-generation-changed")
        self.assertIs(after[0]["current_generation_valid"], False)
        self.assertEqual(after[0]["current_execution_state"], "unknown")
        self.assertEqual(shell_jobs.normalize_job(after[0], now=1792463235)[
            "current_execution_reason"], "source-generation-retired")
        self.assertEqual(current_background_shell_jobs(after, now=1792463235), [])

    def test_claude_native_end_time_keeps_invalid_future_and_missing_clocks_uncertain(self):
        for label, patch, expected_state in (
                ("invalid", {"status": "completed", "end_time": "bad"}, "invalid"),
                ("future", {"status": "completed", "end_time": 1792463240000}, "future"),
                ("missing", {"status": "completed"}, "missing")):
            with self.subTest(clock=label):
                sid = "session-native-clock-" + label
                call_id = "toolu-native-clock-" + label
                task_id = "task-native-clock-" + label
                run_id = "run-native-clock-" + label
                path = self.root / (label + "-clock.jsonl")
                call = {
                    "type": "assistant", "session_id": sid,
                    "message": {"content": [{"type": "tool_use", "name": "Bash",
                        "id": call_id, "input": {"command": "echo safe",
                                                  "run_in_background": True}}]},
                }
                start = {
                    "type": "system", "subtype": "task_started", "task_id": task_id,
                    "run_id": run_id, "tool_use_id": call_id, "is_backgrounded": True,
                    "task_type": "local_bash", "session_id": sid,
                }
                update = {
                    "type": "system", "subtype": "task_updated", "task_id": task_id,
                    "run_id": run_id, "patch": patch, "session_id": sid,
                }
                notification = {
                    "type": "system", "subtype": "task_notification", "task_id": task_id,
                    "run_id": run_id, "tool_use_id": call_id, "status": "completed",
                    "summary": "completed without a usable clock", "session_id": sid,
                }
                self.write_jsonl(path, [call, start, update, notification])
                jobs = shell_jobs.read_claude_jobs(
                    path, session_id=sid, attempt_id="attempt-" + label,
                    now=1792463235)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["completion_time_state"], expected_state)
                self.assertIsNone(jobs[0]["elapsed_s"])
                self.assertIsNone(jobs[0]["started_at"])

    def test_claude_exact_ack_command_redaction_and_bad_attribution(self):
        path = self.root / "claude.jsonl"
        result, output = self.claude_result()
        self.write_jsonl(path, [self.claude_call(), result])

        jobs = shell_jobs.read_claude_jobs(path, session_id="sid-a", now=1792454405)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["native_task_id"], "task-123")
        self.assertEqual(job["native_record_kind"], "claude.background-bash-task-ack")
        self.assertEqual(job["background_state"], "unknown")
        self.assertIsNone(job["background_source"])
        self.assertEqual(job["background_reason"],
                         "claude-native-background-task-metadata-unavailable")
        self.assertEqual(job["native_status"], "acknowledged")
        self.assertEqual(job["process_state"], "unknown")
        self.assertEqual(job["output_path"], str(output))
        self.assertIn("[redacted]", job["command"])
        self.assertNotIn("hidden", job["command"])

        other = self.root / "other.jsonl"
        wrong_call = dict(self.claude_call(session_id="sid-other"))
        wrong_result, _ = self.claude_result(session_id="sid-other", task_id="task-other")
        self.write_jsonl(other, [wrong_call, wrong_result])
        self.assertEqual(shell_jobs.read_claude_jobs(other, session_id="sid-a"), [])

    def test_claude_echoed_native_ack_is_unknown_with_or_without_request_flag(self):
        now = shell_jobs.parse_time("2026-10-20T00:00:05Z")
        for requested in (True, False):
            with self.subTest(run_in_background=requested):
                task_id = "task-echoed-true" if requested else "task-echoed-false"
                call_id = "call-echo-true" if requested else "call-echo-false"
                output = self.root / "tasks" / (task_id + ".output")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.touch()
                call = self.claude_call(call_id=call_id)
                call["message"]["content"][0]["input"]["run_in_background"] = requested
                result = {
                    "type": "user", "session_id": "sid-a",
                    "timestamp": "2026-10-20T00:00:01Z",
                    "message": {"content": [{
                        "type": "tool_result", "tool_use_id": call_id,
                        "content": [{"type": "text", "text":
                            "Command running in background. Task ID: %s. Output: %s"
                            % (task_id, output)}],
                    }]},
                }
                path = self.root / ("echoed-%s.jsonl" % requested)
                self.write_jsonl(path, [call, result])

                jobs = shell_jobs.read_claude_jobs(
                    path, session_id="sid-a", attempt_id="attempt-echo",
                    now=now)
                self.assertEqual(len(jobs), 1)
                job = jobs[0]
                self.assertEqual(job["native_task_id"], task_id)
                self.assertEqual(job["output_path"], str(output))
                self.assertEqual(job["process_state"], "unknown")
                self.assertEqual(job["background_state"], "unknown")
                self.assertIsNone(job["background_source"])
                self.assertEqual(job["background_reason"],
                                 "claude-native-background-task-metadata-unavailable")

                observed = shell_jobs.observe_jobs(jobs, now=now)
                session = Session(harness="claude", pid=1, cwd=str(self.root),
                                  shell_jobs=observed)
                with mock.patch("time.time", return_value=now):
                    public = session.to_dict()
                json_job = public["shell_jobs"][0]
                self.assertEqual(json_job["native_task_id"], task_id)
                self.assertEqual(json_job["background_state"], "unknown")
                self.assertIsNone(json_job["background_source"])
                self.assertEqual(json_job["background_reason"], job["background_reason"])
                row_text = self.context_text(session, 100, now)
                self.assertNotIn(task_id, row_text)
                self.assertNotIn("background unknown", row_text)

    def test_claude_native_task_metadata_confirms_without_request_flag(self):
        path = self.root / "native-metadata.jsonl"
        call_id = "toolu-native-background-call"
        task_id = "native-task-1234"
        echoed_id = "task-echoed-mismatch"
        call = self.claude_call(call_id=call_id)
        call["message"]["content"][0]["input"]["run_in_background"] = False
        result = {
            "type": "user", "sessionId": "sid-a", "session_id": "runtime-other",
            "timestamp": "2026-10-20T00:00:01Z",
            "toolUseResult": {"backgroundTaskId": task_id},
            "message": {"content": [{"type": "tool_result", "tool_use_id": call_id,
                "content": "Command running in background. Task ID: %s. Output: %s"
                % (echoed_id, self.root / "tasks" / (echoed_id + ".output"))}]},
        }
        self.write_jsonl(path, [call, result])

        jobs = shell_jobs.read_claude_jobs(
            path, session_id="sid-a", attempt_id="attempt-native",
            now=shell_jobs.parse_time("2026-10-20T00:00:05Z"))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["native_task_id"], task_id)
        self.assertEqual(job["native_call_id"], call_id)
        self.assertEqual(job["session_id"], "sid-a")
        self.assertEqual(job["attempt_id"], "attempt-native")
        self.assertEqual(job["source_locator"], os.path.abspath(path))
        self.assertTrue(job["source_generation"])
        self.assertEqual(job["native_record_kind"],
                         "claude.toolUseResult.backgroundTaskId")
        self.assertEqual(job["background_state"], "confirmed")
        self.assertEqual(job["background_source"],
                         "claude.toolUseResult.backgroundTaskId")
        self.assertIsNone(job["background_reason"])
        self.assertIsNone(job["output_path"])

        observed = shell_jobs.observe_jobs(jobs, now=job["observed_at"])
        session = Session(harness="claude", pid=1, cwd=str(self.root),
                          shell_jobs=observed)
        with mock.patch("time.time", return_value=job["observed_at"]):
            public = session.to_dict()
        json_job = public["shell_jobs"][0]
        self.assertEqual(json_job["native_task_id"], task_id)
        self.assertEqual(json_job["background_state"], "confirmed")
        self.assertEqual(json_job["background_source"], job["background_source"])
        self.assertIsNone(json_job["background_reason"])
        row_text = self.context_text(session, 100, job["observed_at"])
        self.assertNotIn(task_id, row_text)
        self.assertNotIn(echoed_id, row_text)

    def test_claude_task_metadata_needs_valid_id_and_exact_result_pair(self):
        cases = (
            ("invalid-id", "task/invalid", "call-invalid", "call-invalid", "sid-a", 1),
            ("wrong-call", "task-wrong-call", "call-wrong-call", "other-call", "sid-a", 1),
            ("wrong-session", "task-wrong-session", "call-wrong-session",
             "call-wrong-session", "sid-other", 1),
            ("ambiguous-results", "task-ambiguous", "call-ambiguous",
             "call-ambiguous", "sid-a", 2),
        )
        for label, task_id, call_id, result_call_id, result_sid, result_count in cases:
            with self.subTest(case=label):
                path = self.root / (label + ".jsonl")
                call = self.claude_call(call_id=call_id)
                result_parts = [{"type": "tool_result", "tool_use_id": result_call_id,
                                 "content": ""}]
                if result_count == 2:
                    result_parts.append({"type": "tool_result", "tool_use_id": "other-result",
                                         "content": ""})
                result = {
                    "type": "user", "sessionId": result_sid,
                    "timestamp": "2026-10-20T00:00:01Z",
                    "toolUseResult": {"backgroundTaskId": task_id},
                    "message": {"content": result_parts},
                }
                self.write_jsonl(path, [call, result])
                self.assertEqual(shell_jobs.read_claude_jobs(
                    path, session_id="sid-a", attempt_id="attempt-negative",
                    now=shell_jobs.parse_time("2026-10-20T00:00:05Z")), [])

    def test_claude_native_session_binding_and_trusted_task_notifications(self):
        path = self.root / "054aa76e-7697-468f-b91c-783e9bd8fd01.jsonl"
        sid = "054aa76e-7697-468f-b91c-783e9bd8fd01"
        runtime_sid = "81c65afa-e494-43ba-ab98-9650ed4c9e49"
        task_a, task_b, task_c, task_d = (
            "task-native-a", "task-native-b", "task-native-c", "task-native-d")
        call_a, call_b, call_c, call_d = (
            "toolu_native_call_a", "toolu_native_call_b", "toolu_native_call_c",
            "toolu_native_call_d")
        stamps = {
            "start_a": "2026-10-20T00:00:00Z", "start_b": "2026-10-20T00:01:00Z",
            "start_c": "2026-10-20T00:02:00Z", "ack": "2026-10-20T00:02:01Z",
            "done_a": "2026-10-20T00:00:30Z", "done_c": "2026-10-20T00:02:45Z",
        }

        def call(call_id, task_start):
            return {"type": "assistant", "sessionId": sid, "session_id": runtime_sid,
                    "timestamp": stamps[task_start], "message": {"content": [{
                        "type": "tool_use", "name": "Bash", "id": call_id,
                        "input": {"command": "sleep 30", "run_in_background": True}}]}}

        def ack(call_id, task_id):
            return {"type": "user", "sessionId": sid, "session_id": runtime_sid,
                    "timestamp": stamps["ack"], "toolUseResult": {
                        "backgroundTaskId": task_id}, "message": {"content": [{
                            "type": "tool_result", "tool_use_id": call_id,
                            "content": ""}]}}

        def notification(task_id, call_id, status, stamp, summary="", output_path=None,
                         *, origin=True, native_sid=sid, runtime_session=runtime_sid):
            output = output_path or str(self.root / "tasks" / (task_id + ".output"))
            body = ("<task-notification><task-id>%s</task-id><tool-use-id>%s</tool-use-id>"
                    "<output-file>%s</output-file><status>%s</status><summary>%s</summary>"
                    "</task-notification>" % (task_id, call_id, output, status, summary))
            row = {"type": "user", "sessionId": native_sid,
                   "session_id": runtime_session, "timestamp": stamp,
                   "message": {"content": body}}
            if isinstance(origin, dict):
                row.update(origin=origin, promptSource="system",
                           turnOrigin="task_notification")
            elif origin:
                row.update(origin={"kind": "task-notification", "producer": "session-task"},
                           promptSource="system", turnOrigin="task_notification")
            return row

        output_a = str(self.root / "tasks" / (task_a + ".output"))
        output_b = str(self.root / "tasks" / (task_b + ".output"))
        output_c = str(self.root / "tasks" / (task_c + ".output"))
        output_d = str(self.root / "tasks" / (task_d + ".output"))
        rows = [
            call(call_a, "start_a"), ack(call_a, task_a),
            call(call_b, "start_b"), ack(call_b, task_b),
            call(call_c, "start_c"), ack(call_c, task_c),
            call(call_d, "start_c"), ack(call_d, task_d),
            notification(task_a, call_a, "completed", stamps["done_a"],
                         "Background command completed (exit code 0)", output_a),
            notification(task_b, call_a, "failed", "2026-10-20T00:01:30Z",
                         "Background command failed (exit code 2)", output_b),
            notification(task_c, call_c, "stopped", stamps["done_c"], output_path=output_c),
            notification(task_d, call_d, "failed", "2026-10-20T00:03:00Z",
                         "Background command failed (exit code 2)", output_d),
            notification(task_b, call_b, "failed", "2026-10-20T00:01:40Z",
                         "Background command failed (exit code 2)", output_b, origin=False),
            notification(task_b, call_b, "failed", "2026-10-20T00:01:45Z",
                         "Background command failed (exit code 2)", output_b,
                         origin={"kind": "task-notification", "producer": "untrusted-copy"}),
            {"type": "assistant", "api_request": {"messages": [
                "<task-notification><task-id>%s</task-id><tool-use-id>%s</tool-use-id>"
                "<status>failed</status></task-notification>" % (task_b, call_b)]},
             "message": {"content": [{"type": "text", "text":
                 "<task-notification><task-id>%s</task-id><tool-use-id>%s</tool-use-id>"
                 "<status>failed</status></task-notification>" % (task_b, call_b)}]}},
            notification(task_b, call_b, "failed", "2026-10-20T00:02:00Z",
                         "Background command failed (exit code 2)", output_b,
                         native_sid="neighbor-session"),
            {"type": "user", "sessionId": sid, "session_id": runtime_sid,
             "timestamp": "2026-10-20T00:02:10Z",
             "message": {"content": [{"type": "text", "text":
                 "<task-notification><task-id>%s</task-id></task-notification>" % task_b}]}},
        ]
        continuation = notification(task_b, call_b, "failed", "2026-10-20T00:02:20Z",
                                    "Background command failed (exit code 2)", output_b,
                                    native_sid="continued-source-session")
        continuation["continuedInSessionId"] = sid
        rows.append(continuation)
        # The API request copy above is nested text, not a native task-notification
        # envelope, and therefore cannot finish the acknowledged task.
        self.write_jsonl(path, rows)

        now = shell_jobs.parse_time("2026-10-20T00:10:00Z")
        jobs = shell_jobs.read_claude_jobs(path, session_id=sid, attempt_id="attempt-a", now=now)
        by_task = {job["native_task_id"]: job for job in jobs}
        self.assertEqual(set(by_task), {task_a, task_b, task_c, task_d})
        self.assertEqual(by_task[task_a]["native_status"], "completed")
        self.assertEqual(by_task[task_a]["exit_code"], 0)
        self.assertEqual(by_task[task_a]["elapsed_s"], 30)
        self.assertEqual(by_task[task_a]["output_path"], output_a)
        self.assertEqual(by_task[task_a]["process_state"], "unknown")
        self.assertEqual(by_task[task_b]["native_status"], "acknowledged")
        self.assertIsNone(by_task[task_b].get("exit_code"))
        self.assertEqual(by_task[task_c]["native_status"], "stopped")
        self.assertIsNone(by_task[task_c].get("exit_code"))
        self.assertEqual(by_task[task_d]["native_status"], "failed")
        self.assertEqual(by_task[task_d]["exit_code"], 2)
        later = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-a", now=now + 3600)
        self.assertEqual({job["native_task_id"]: job["elapsed_s"] for job in later}[task_a], 30)
        self.assertEqual({job["native_task_id"]: job["lifecycle_state"]
                          for job in later}[task_b], "unknown")
        self.assertIn(task_b, {job["native_task_id"] for job in shell_jobs.observe_jobs(
            later, now=now + 3600)})

        # The conflicting runtime SID cannot select this native transcript's tasks.
        self.assertEqual(shell_jobs.read_claude_jobs(
            path, session_id=runtime_sid, attempt_id="attempt-a", now=now), [])
        copied = self.root / "copied.jsonl"
        self.write_jsonl(copied, [notification(
            task_a, call_a, "completed", stamps["done_a"],
            "Background command completed (exit code 0)", output_a)])
        self.assertEqual(shell_jobs.read_claude_jobs(
            copied, session_id=sid, attempt_id="attempt-a", now=now), [])

    def test_claude_automatic_background_ack_remains_supported_without_flag(self):
        path = self.root / "automatic-background.jsonl"
        self.write_jsonl(path, [
            {"type": "assistant", "sessionId": "sid-auto",
             "timestamp": "2026-10-20T00:00:00Z", "message": {"content": [{
                 "type": "tool_use", "name": "Bash", "id": "toolu_auto_call",
                 "input": {"command": "long-running-command"}}]}},
            {"type": "user", "sessionId": "sid-auto",
             "timestamp": "2026-10-20T00:00:01Z", "message": {"content": [{
                 "type": "tool_result", "tool_use_id": "toolu_auto_call",
                 "content": "Command running in background. Output: "
                            "/tmp/tasks/task-auto-123.output"}]}}
        ])
        jobs = shell_jobs.read_claude_jobs(
            path, session_id="sid-auto",
            now=shell_jobs.parse_time("2026-10-20T00:01:00Z"))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["native_task_id"], "task-auto-123")
        self.assertEqual(jobs[0]["native_status"], "acknowledged")
        self.assertEqual(jobs[0]["background_state"], "unknown")
        self.assertEqual(jobs[0]["background_reason"],
                         "claude-native-background-task-metadata-unavailable")

    def test_claude_notification_cannot_cross_source_generation(self):
        sid = "native-session-12345"
        call_id, task_id = "toolu-generation-call", "task-generation"
        path = self.root / (sid + ".jsonl")
        rows = [
            {"type": "assistant", "sessionId": sid,
             "timestamp": "2026-10-20T00:00:00Z", "message": {"content": [{
                 "type": "tool_use", "name": "Bash", "id": call_id,
                 "input": {"command": "sleep 2", "run_in_background": True}}]}},
            {"type": "user", "sessionId": sid,
             "timestamp": "2026-10-20T00:00:01Z", "toolUseResult": {
                 "backgroundTaskId": task_id}, "message": {"content": [{
                     "type": "tool_result", "tool_use_id": call_id, "content": ""}]}},
        ]
        self.write_jsonl(path, rows)
        before = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-a",
            now=shell_jobs.parse_time("2026-10-20T00:00:02Z"))
        self.assertEqual(before[0]["native_status"], "acknowledged")

        replacement = self.root / "replacement.jsonl"
        self.write_jsonl(replacement, [
            {"type": "user", "sessionId": sid,
             "timestamp": "2026-10-20T00:00:03Z",
             "origin": {"kind": "task-notification", "producer": "session-task"},
             "promptSource": "system", "turnOrigin": "task_notification",
             "message": {"content": "<task-notification><task-id>%s</task-id>"
                 "<tool-use-id>%s</tool-use-id><status>completed</status>"
                 "</task-notification>" % (task_id, call_id)}}])
        os.replace(replacement, path)
        after = shell_jobs.read_claude_jobs(
            path, session_id=sid, attempt_id="attempt-a",
            now=shell_jobs.parse_time("2026-10-20T00:00:04Z"))
        self.assertTrue(after)
        self.assertFalse(any(job["native_status"] == "completed" for job in after))

    def test_completed_elapsed_uses_only_valid_native_completion_time(self):
        base = 1792454400
        valid = shell_jobs.normalize_job({
            "harness": "codex", "session_id": "thread-a", "native_id": "done",
            "native_status": "completed", "started_at": base,
            "completed_at": base + 30}, now=base + 400)
        self.assertEqual(valid["elapsed_s"], 30)
        self.assertEqual(valid["completion_time_state"], "valid")
        self.assertEqual(shell_jobs.normalize_job({
            "native_status": "completed", "started_at": base}, now=base + 400)["elapsed_s"], None)
        invalid = shell_jobs.normalize_job({
            "native_status": "completed", "started_at": base,
            "completed_at": "not-a-time"}, now=base + 400)
        self.assertEqual(invalid["completion_time_state"], "invalid")
        self.assertEqual(invalid["completion_time_reason"], "invalid-completion-time")
        self.assertIsNone(invalid["elapsed_s"])
        invalid_after_enrichment = shell_jobs.observe_jobs([invalid], now=base + 400)[0]
        self.assertEqual(invalid_after_enrichment["completion_time_state"], "invalid")
        self.assertEqual(invalid_after_enrichment["completion_time_reason"],
                         "invalid-completion-time")
        future = shell_jobs.normalize_job({
            "native_status": "completed", "started_at": base,
            "completed_at": base + 401}, now=base + 400)
        self.assertEqual(future["completion_time_state"], "future")
        self.assertIsNone(future["elapsed_s"])
        reversed_time = shell_jobs.normalize_job({
            "native_status": "completed", "started_at": base + 1,
            "completed_at": base}, now=base + 400)
        self.assertEqual(reversed_time["completion_time_reason"], "completion-before-start")
        self.assertIsNone(reversed_time["elapsed_s"])
        active = shell_jobs.normalize_job({
            "native_status": "running", "started_at": base}, now=base + 401)
        self.assertEqual(active["elapsed_s"], 401)

    def test_shell_job_afterglow_history_is_not_rendered_as_child_rows(self):
        self.assertEqual(DONE_AFTERGLOW_MIN, 15)
        now = 10000.0
        def job(native_id, state, completed_at=None, started_at=9000):
            return {"harness": "codex", "session_id": "s", "native_id": native_id,
                    "command": "echo " + native_id, "lifecycle_state": state,
                    "completion_time_state": "valid" if completed_at is not None else "missing",
                    "completed_at": completed_at, "started_at": started_at,
                    "elapsed_s": 1, "native_status": "completed" if state == "completed" else "running",
                    "process_state": "unknown", "output_coverage": "unavailable"}
        jobs = [job("at-boundary", "completed", now - 900),
                job("outside", "completed", now - 901),
                job("untimed", "completed"),
                job("future", "completed", now + 1),
                job("active-old", "running", started_at=1),
                job("unknown-old", "unknown", started_at=1)]
        selected = {item["native_id"] for item in display_shell_jobs(jobs, now=now)}
        self.assertEqual(selected, {"at-boundary", "untimed", "future", "active-old", "unknown-old"})

        live_now = time.time()
        visible = [job("recent", "completed", live_now - 30, live_now - 60),
                   job("expired", "completed", live_now - 901, live_now - 1000),
                   job("active-long", "running", started_at=live_now - 5000),
                   job("unknown-long", "unknown", started_at=live_now - 5000),
                   job("completion-unknown", "completed")]
        enriched = shell_jobs.observe_jobs(visible, now=live_now)
        session = Session(harness="codex", pid=1, cwd=str(self.root), shell_jobs=enriched)
        public_ids = {item["native_id"] for item in session.to_dict()["shell_jobs"]}
        detail = self.context_text(session, 180, live_now)
        self.assertNotIn("echo ", detail)
        self.assertNotIn("completion ?", detail)
        self.assertNotIn("expired", public_ids)
        self.assertIn("completion-unknown", public_ids)

    def test_live_and_unknown_shell_jobs_precede_stable_untimed_history(self):
        now = 10000.0
        terminal = [{"harness": "codex", "session_id": "s", "native_id": "done-%02d" % index,
                     "command": "echo done-%02d" % index, "lifecycle_state": "completed",
                     "native_status": "completed", "completion_time_state": "missing",
                     "completed_at": None, "started_at": 9000, "elapsed_s": None,
                     "process_state": "unknown", "output_coverage": "unavailable"}
                    for index in range(40)]
        pending = {"harness": "codex", "session_id": "s", "native_id": "pending-later",
                   "command": "echo pending-later", "lifecycle_state": "unknown",
                   "native_status": "unknown", "started_at": 9900,
                   "process_state": "unknown", "output_coverage": "unavailable"}
        unresolved = {"harness": "codex", "session_id": "s", "native_id": "unknown-later",
                      "command": "echo unknown-later", "lifecycle_state": "unknown",
                      "native_status": "unknown", "started_at": 9901,
                      "process_state": "unknown", "output_coverage": "unavailable"}
        live = {"harness": "codex", "session_id": "s", "native_id": "live-later",
                "command": "echo live-later", "lifecycle_state": "running",
                "native_status": "running", "started_at": 9950,
                "source_generation": "gen-1", "source_locator": "/tmp/codex.jsonl",
                "background_state": "confirmed", "background_source": "fixture",
                "current_generation_valid": True, "current_execution_state": "running",
                "current_execution_source": "codex.continuation",
                "current_execution_at": now - 1,
                "process_state": "unknown", "output_coverage": "sampled"}
        jobs = terminal + [pending, unresolved, live]
        session = Session(harness="codex", pid=1, session_id="s", shell_jobs=jobs)
        with mock.patch("time.time", return_value=now):
            public = session.to_dict()
        screen = self.context_text(session, 180, now)
        expected = ["live-later", "pending-later", "unknown-later"] + [
            "done-%02d" % index for index in range(40)]
        self.assertEqual([job["native_id"] for job in public["shell_jobs"]], expected)
        self.assertIn("echo live-later", screen)
        self.assertNotIn("pending-later", screen)
        self.assertNotIn("unknown-later", screen)
        self.assertNotIn("done-00", screen)
        self.assertEqual(sum(job["lifecycle_state"] == "completed"
                             for job in public["shell_jobs"]), 40)

    def test_codex_nested_handles_multiple_jobs_exit_and_json_render(self):
        path = self.root / "rollout.jsonl"
        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "exec-101", "name": "exec_command",
                "input": {"cmd": "sleep 10"}}},
            {"timestamp": "2026-10-20T00:00:05Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "exec-101",
                "output": {"session_id": 101, "output": ""}}},
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "exec-202", "name": "exec_command",
                "input": {"cmd": "sleep 12"}}},
            {"timestamp": "2026-10-20T00:00:05Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "exec-202",
                "output": {"session_id": 202, "output": ""}}},
        ])
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454405)
        self.assertEqual({item["continuation_handle"] for item in jobs}, {"101", "202"})
        self.assertEqual(len(jobs), 2)
        self.assertEqual({item["continuation_handle"]: item["command"] for item in jobs},
                         {"101": "sleep 10", "202": "sleep 12"})
        self.assertTrue(all(item["process_state"] == "unknown" for item in jobs))
        self.assertTrue(all(item["lifecycle_state"] == "running" for item in jobs))
        self.assertTrue(all(item["native_record_kind"] == "codex.exec_command" for item in jobs))
        self.assertTrue(all(item["background_state"] == "confirmed" for item in jobs))
        self.assertTrue(all(item["background_source"] ==
                            "codex.exec_command.native-continuation-handle" for item in jobs))
        self.assertTrue(all(item["elapsed_s"] == 5 for item in jobs))

        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:06Z", "payload": {
                "type": "custom_tool_call", "call_id": "status-poll", "name": "write_stdin",
                "input": {"session_id": 101}}},
            {"timestamp": "2026-10-20T00:00:07Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "status-poll",
                "output": {"session_id": 101, "output": "", "status": "running"}}},
        ], mode="a")
        status_only = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454407)
        self.assertEqual({item["continuation_handle"]: item["lifecycle_state"]
                          for item in status_only}["101"], "running")
        self.assertTrue(all(item["background_state"] == "confirmed" for item in status_only))

        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:06Z", "payload": {
                "type": "custom_tool_call", "call_id": "poll", "name": "write_stdin",
                "input": {"session_id": 101}}},
            {"timestamp": "2026-10-20T00:00:07Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "poll",
                "output": {"session_id": 101, "output": "", "exit_code": 2}}},
        ], mode="a")
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454407)
        by_handle = {item["continuation_handle"]: item for item in jobs}
        self.assertEqual(by_handle["101"]["lifecycle_state"], "completed")
        self.assertEqual(by_handle["101"]["exit_code"], 2)
        self.assertEqual(by_handle["202"]["lifecycle_state"], "running")

        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:08Z", "payload": {
                "type": "custom_tool_call", "call_id": "late-poll", "name": "write_stdin",
                "input": {"session_id": 101}}},
            {"timestamp": "2026-10-20T00:00:09Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "late-poll",
                "output": {"session_id": 101, "output": "", "status": "running"}}},
        ], mode="a")
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-a", now=1792454409)
        by_handle = {item["continuation_handle"]: item for item in jobs}
        self.assertEqual(by_handle["101"]["native_status"], "completed")
        self.assertEqual(by_handle["101"]["lifecycle_state"], "completed")
        self.assertEqual(by_handle["101"]["current_execution_state"], "terminal")
        self.assertEqual([item["continuation_handle"] for item in
                          current_background_shell_jobs(jobs, now=1792454409)], ["202"])

        session = Session(harness="codex", pid=10, session_id="thread-a",
                          liveness="idle", shell_jobs=jobs)
        with mock.patch("time.time", return_value=1792454407):
            public = session.to_dict()
        self.assertEqual(public["liveness"], "idle")
        self.assertEqual(len(public["shell_jobs"]), 2)
        self.assertTrue(all(item["background_state"] == "confirmed"
                            for item in public["shell_jobs"]))
        with mock.patch("time.time", return_value=1792454407), \
                mock.patch("os.stat", side_effect=AssertionError("render attempted observation I/O")):
            by_width = {width: render._context_detail_row(session, term_width=width)
                        for width in (60, 100, 120, 168)}
        for width, rows in by_width.items():
            for line in rows:
                self.assertLessEqual(render._dw("".join(text for text, _key in line)), width)
        rendered = "".join(text for line in by_width[120] for text, _key in line)
        self.assertIn("sleep 12", rendered)
        self.assertNotIn("sleep 10", rendered)
        self.assertNotIn("native exit 2", rendered)
        self.assertNotIn("background confirmed", rendered)

        worker = DispatchJob(key="autopilot-code", slug="worker-shell-jobs",
                             harness="codex", liveness="working", shell_jobs=jobs)
        with mock.patch("time.time", return_value=1792454407):
            group_lines = render._build_lines([], [worker], "dispatch", False, 0,
                                              term_width=120)
            group_screen = "\n".join(render._plain(line) for line in group_lines if line)
            process_lines, _meta = render._degrade_card(worker, {}, term_width=120)
            process_screen = "\n".join(render._plain(line) for line in process_lines if line)
        self.assertIn("worker-shell-jobs", group_screen)
        self.assertIn("sleep 12", group_screen)
        self.assertIn("sleep 12", process_screen)
        self.assertNotIn("native exit 2", group_screen)
        self.assertNotIn("native exit 2", process_screen)

    def test_codex_native_command_events_project_running_and_exit_without_stdout_parsing(self):
        path = self.root / "native-events.jsonl"
        echoed = json.dumps({"session_id": 91919, "exit_code": 0})
        self.write_jsonl(path, [
            {"type": "item.started", "thread_id": "thread-other",
             "timestamp": "2026-10-20T00:00:00Z", "item": {
                "type": "command_execution", "id": "foreign-item", "command": "sleep 99",
                "status": "inProgress", "exit_code": None, "aggregated_output": None,
                "background": True}},
            {"type": "item.started", "timestamp": "2026-10-20T00:00:00Z", "item": {
                "type": "command_execution", "id": "item-1", "command": "sleep 5",
                "status": "inProgress", "exit_code": None, "aggregated_output": None,
                "background": True}},
            {"type": "item.completed", "timestamp": "2026-10-20T00:00:05Z", "item": {
                "type": "command_execution", "id": "item-1", "command": "sleep 5",
                "status": "completed", "exit_code": 3, "aggregated_output": echoed,
                "background": True}},
        ])
        jobs = shell_jobs.read_codex_jobs(
            path, session_id="thread-native", attempt_id="attempt-a", now=1792454405)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["native_item_id"], "item-1")
        self.assertEqual(jobs[0]["attempt_id"], "attempt-a")
        self.assertEqual(jobs[0]["command"], "sleep 5")
        self.assertEqual(jobs[0]["native_record_kind"], "codex.command_execution")
        self.assertEqual(jobs[0]["background_state"], "unknown")
        self.assertIsNone(jobs[0]["background_source"])
        self.assertEqual(jobs[0]["background_reason"],
                         "native-command-event-does-not-expose-background-mode")
        self.assertEqual(jobs[0]["lifecycle_state"], "completed")
        self.assertEqual(jobs[0]["exit_code"], 3)
        self.assertEqual(jobs[0]["process_state"], "unknown")
        self.assertEqual(jobs[0]["elapsed_s"], 5)
        session = Session(harness="codex", pid=10, session_id="thread-native",
                          shell_jobs=jobs)
        with mock.patch("time.time", return_value=1792454405):
            public = session.to_dict()
        self.assertEqual(public["shell_jobs"][0]["background_state"], "unknown")
        self.assertEqual(public["shell_jobs"][0]["native_record_kind"],
                         "codex.command_execution")
        rendered = self.context_text(session, 100, 1792454405)
        self.assertNotIn("sleep 5", rendered)
        self.assertNotIn("background unknown", rendered)
        later = shell_jobs.read_codex_jobs(
            path, session_id="thread-native", attempt_id="attempt-a", now=1792455005)
        self.assertEqual(later[0]["elapsed_s"], 5)
        self.assertEqual(shell_jobs.source_status(
            path, ("codex", os.path.abspath(path), "thread-native", "attempt-a"))["coverage"],
            "complete")

    def test_codex_wrapper_commands_keep_meaning_and_metadata_at_common_widths(self):
        path = self.root / "wrapper-commands.jsonl"
        commands = [
            ('/usr/bin/zsh -lc "python3 -m tools.fleet.worker --queue review --limit 16"',
             "python3 -m tools.fleet.worker --queue review --limit 16"),
            ('/usr/bin/bash -lc "python3 -m tools.fleet.report --mode final --strict"',
             "python3 -m tools.fleet.report --mode final --strict"),
            ("/usr/bin/zsh -lc \"python3 utilities/verification-background-lease.py "
             "--timeout 40 --memory-budget 140GB -- python3 -c 'print(1)'\"", "python3 -c 'print(1)'"),
        ]
        rows = []
        for index, (command, _display) in enumerate(commands):
            rows.append({"type": "item.started", "thread_id": "thread-wrapper",
                         "timestamp": "2026-10-20T00:00:00Z", "item": {
                             "type": "command_execution", "id": "wrapper-%d" % index,
                             "command": command, "status": "inProgress",
                             "exit_code": None, "aggregated_output": None}})
        self.write_jsonl(path, rows)
        now = 1792454410
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-wrapper", now=now)
        self.assertEqual([job["command_display"] for job in jobs],
                         [display for _command, display in commands])
        self.assertTrue(all(job["command"].startswith(("/usr/bin/zsh", "/usr/bin/bash"))
                            for job in jobs))
        self.assertTrue(all(job["background_state"] == "unknown" for job in jobs))
        self.assertTrue(all(job["native_status"] == "running" for job in jobs))
        self.assertTrue(all(job["process_state"] == "unknown" for job in jobs))
        self.assertTrue(all(job["process_reason"] ==
                            "native-command-event-has-no-process-identity" for job in jobs))
        self.assertTrue(all(job["output_coverage"] == "sampled" for job in jobs))
        self.assertTrue(all(job["output_reason"] == "native-output-not-present" for job in jobs))
        session = Session(harness="codex", pid=10, session_id="thread-wrapper",
                          shell_jobs=jobs)
        with mock.patch("time.time", return_value=now):
            public = session.to_dict()
        self.assertEqual([job["command_display"] for job in public["shell_jobs"]],
                         [display for _command, display in commands])
        for width in (80, 100):
            with mock.patch("time.time", return_value=now), \
                    mock.patch("os.stat", side_effect=AssertionError(
                        "render attempted observation I/O")):
                rendered = render._context_detail_row(session, term_width=width)
            texts = ["".join(text for text, _key in line) for line in rendered]
            self.assertTrue(all(render._dw(line) <= width for line in texts))
            joined = "\n".join(texts)
            for _command, display in commands:
                self.assertNotIn(display, joined)
        self.assertTrue(all(job["command_display_source"] ==
                            "codex.native-shell-command.argv" for job in jobs))

    def test_verification_lease_display_walks_its_own_options(self):
        cases = (
            ("python3 utilities/verification-background-lease.py --memory-budget=8GiB --timeout=40 -- ls -la", "ls -la"),
            ("python3 utilities/verification-background-lease.py --memory-budget 140GB --timeout 40 -- ls", "ls"),
            ("python3 utilities/verification-background-lease.py --timeout 40 -- ls", "ls"),
        )
        for inner, expected in cases:
            wrapped = "/usr/bin/zsh -lc " + __import__("shlex").quote(inner)
            self.assertEqual(shell_jobs._codex_shell_command_display(wrapped),
                             shell_jobs.command_summary(expected))
        unchanged = (
            "--foo 1 --timeout 40 -- ls", "--timeout 1 --timeout 2 -- ls",
            "--memory-budget 1G -- ls", "--timeout 40 --",
            "--timeout x -- ls", "--timeout 40 --memory-budget -- ls",
        )
        for options in unchanged:
            inner = "python3 utilities/verification-background-lease.py " + options
            wrapped = "/usr/bin/zsh -lc " + __import__("shlex").quote(inner)
            self.assertEqual(shell_jobs._codex_shell_command_display(wrapped),
                             shell_jobs.command_summary(inner))

    def test_codex_direct_result_without_exact_handle_stays_unobserved(self):
        path = self.root / "missing-native-continuation.jsonl"
        self.write_jsonl(path, [
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "exec-missing-handle",
                "name": "exec_command", "input": {"cmd": "sleep 5"}}},
            {"timestamp": "2026-10-20T00:00:01Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "exec-missing-handle",
                "output": {"status": "running", "background": True,
                           "session_id": "not-an-integer-handle"}}},
        ])
        jobs = shell_jobs.read_codex_jobs(path, session_id="thread-missing-handle")
        self.assertEqual(jobs, [])
        status = shell_jobs.source_status(
            path, ("codex", os.path.abspath(path), "thread-missing-handle", None))
        self.assertEqual(status["coverage"], "partial")
        self.assertEqual(status["reason"], "codex-native-result-lifecycle-unavailable")

    def test_codex_outer_exec_echoed_quoted_and_unrelated_output_is_unobserved(self):
        path = self.root / "outer-exec.jsonl"
        rows = []
        outputs = [
            [{"type": "input_text", "text": json.dumps(
                {"session_id": 38555, "exit_code": 0, "output": "echoed"})}],
            json.dumps({"session_id": 58254, "exit_code": 3, "output": "quoted"}),
            [{"type": "input_text", "text": "ordinary unrelated output"}],
        ]
        for index, output in enumerate(outputs):
            call_id = "outer-" + str(index)
            rows.extend((
                {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                    "type": "custom_tool_call", "call_id": call_id,
                    "name": "functions.exec", "input": "opaque script body"}},
                {"timestamp": "2026-10-20T00:00:01Z", "payload": {
                    "type": "custom_tool_call_output", "call_id": call_id,
                    "output": output}},
            ))
        self.write_jsonl(path, rows)
        self.assertEqual(shell_jobs.read_codex_jobs(path, session_id="thread-outer"), [])
        status = shell_jobs.source_status(
            path, ("codex", os.path.abspath(path), "thread-outer", None))
        self.assertEqual(status["state"], "partial")
        self.assertEqual(status["reason"], "outer-functions-exec-output-untrusted")
        self.assertEqual(status["coverage"], "partial")

        direct = self.root / "direct-quoted-output.jsonl"
        self.write_jsonl(direct, [
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "direct", "name": "exec_command",
                "input": {"cmd": "echo quoted JSON"}}},
            {"timestamp": "2026-10-20T00:00:01Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "direct",
                "output": json.dumps({"session_id": 38555, "exit_code": 0})}},
        ])
        self.assertEqual(shell_jobs.read_codex_jobs(direct, session_id="thread-direct"), [])
        direct_status = shell_jobs.source_status(
            direct, ("codex", os.path.abspath(direct), "thread-direct", None))
        self.assertEqual(direct_status["reason"], "codex-native-result-not-structured")

        poll = self.root / "outer-write-stdin.jsonl"
        self.write_jsonl(poll, [
            {"timestamp": "2026-10-20T00:00:00Z", "payload": {
                "type": "custom_tool_call", "call_id": "start", "name": "exec_command",
                "input": {"cmd": "sleep 5"}}},
            {"timestamp": "2026-10-20T00:00:01Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "start",
                "output": {"session_id": 38555, "output": ""}}},
            {"timestamp": "2026-10-20T00:00:02Z", "payload": {
                "type": "custom_tool_call", "call_id": "outer-poll",
                "name": "functions.write_stdin", "input": "opaque script body"}},
            {"timestamp": "2026-10-20T00:00:03Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "outer-poll",
                "output": [{"type": "input_text", "text": json.dumps(
                    {"session_id": 38555, "exit_code": 0})}]}},
        ])
        poll_jobs = shell_jobs.read_codex_jobs(poll, session_id="thread-poll")
        self.assertEqual(len(poll_jobs), 1)
        self.assertEqual(poll_jobs[0]["lifecycle_state"], "running")
        self.assertEqual(poll_jobs[0]["background_state"], "confirmed")
        self.assertIsNone(poll_jobs[0]["exit_code"])
        poll_status = shell_jobs.source_status(
            poll, ("codex", os.path.abspath(poll), "thread-poll", None))
        self.assertEqual(poll_status["reason"], "outer-functions-write-stdin-output-untrusted")

    def test_codex_pending_call_correlation_is_bounded_and_recent_pairing_survives(self):
        path = self.root / "many-calls.jsonl"
        rows = [{"timestamp": "2026-10-20T00:00:00Z", "payload": {
            "type": "custom_tool_call", "call_id": "call-%04d" % index,
            "name": "exec_command", "input": {}}}
            for index in range(shell_jobs.MAX_PENDING_CALLS + 1)]
        rows.extend((
            {"timestamp": "2026-10-20T00:00:01Z", "payload": {
                "type": "custom_tool_call_output", "call_id": "call-0000",
                "output": {"session_id": 700, "output": ""}}},
            {"timestamp": "2026-10-20T00:00:02Z", "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-%04d" % shell_jobs.MAX_PENDING_CALLS,
                "output": {"session_id": 999, "output": ""}}},
        ))
        self.write_jsonl(path, rows)
        jobs = []
        for _ in range(4):
            jobs = shell_jobs.read_codex_jobs(path, session_id="thread-many")
            if shell_jobs.source_status(
                    path, ("codex", os.path.abspath(path), "thread-many", None))["state"] \
                    != "catching-up":
                break
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["native_call_id"],
                         "call-%04d" % shell_jobs.MAX_PENDING_CALLS)
        self.assertEqual(jobs[0]["continuation_handle"], "999")
        state = shell_jobs._TASKS[("codex", os.path.abspath(path), "thread-many", None)]
        self.assertLessEqual(len(state["calls"]), shell_jobs.MAX_PENDING_CALLS)
        coverage = shell_jobs.source_status(
            path, ("codex", os.path.abspath(path), "thread-many", None))
        self.assertEqual(coverage["state"], "partial")
        self.assertEqual(coverage["reason"], "pending-call-correlation-limit")

    def test_bounded_catchup_and_same_size_source_rewrite_invalidation(self):
        path = self.root / "large.jsonl"
        with open(path, "wb") as stream:
            stream.write((b'{"type":"other"}\n' * 18000))
            stream.write((json.dumps(self.claude_call()).encode() + b"\n"))
            result, _output = self.claude_result()
            stream.write((json.dumps(result).encode() + b"\n"))
        first = shell_jobs.read_claude_jobs(path, session_id="sid-a")
        self.assertEqual(first, [])
        source_key = ("claude", os.path.abspath(path), "sid-a", None)
        self.assertEqual(shell_jobs.source_status(path, source_key)["state"], "catching-up")
        second = []
        for _ in range(8):
            second = shell_jobs.read_claude_jobs(path, session_id="sid-a")
            if shell_jobs.source_status(path, source_key)["state"] == "observed":
                break
        self.assertEqual(len(second), 1)
        self.assertEqual(shell_jobs.source_status(path, source_key)["state"], "observed")

        small = self.root / "rewrite.jsonl"
        raw_a = b'{"type":"other","key":"a"}\n'
        raw_b = b'{"type":"other","key":"b"}\n'
        self.assertEqual(len(raw_a), len(raw_b))
        small.write_bytes(raw_a)
        before = shell_jobs.jsonl_delta(small, "rewrite-source")
        small.write_bytes(raw_b)
        after = shell_jobs.jsonl_delta(small, "rewrite-source")
        self.assertTrue(after["reset"])
        self.assertNotEqual(before["generation"], after["generation"])

    def test_quiet_boundaries_blank_output_rotation_and_completion(self):
        path = self.root / "output.log"
        path.write_text("first\n", encoding="utf-8")
        base = path.stat().st_mtime + 1
        job = {"harness": "claude", "session_id": "sid-q", "native_id": "task-q",
               "native_status": "acknowledged", "process_state": "alive",
               "started_at": base - 5,
               "output_path": str(path)}
        first = shell_jobs.normalize_job(job, now=base)
        self.assertFalse(first["quiet_suspected"])
        self.assertEqual(first["output_coverage"], "sampled")
        before = shell_jobs.normalize_job(job, now=base + 599)
        self.assertFalse(before["quiet_suspected"])
        at_boundary = shell_jobs.normalize_job(job, now=base + 600)
        self.assertTrue(at_boundary["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(job, now=base + 601)["quiet_suspected"])

        with path.open("a", encoding="utf-8") as stream:
            stream.write("second\n")
        os.utime(path, (base + 629, base + 629))
        growth = shell_jobs.normalize_job(job, now=base + 630)
        self.assertEqual(growth["output_coverage"], "continuous-file")
        self.assertEqual(growth["last_output_at"], base + 629)
        self.assertFalse(growth["quiet_suspected"])

        path.write_text("", encoding="utf-8")
        truncated = shell_jobs.normalize_job(job, now=base + 601)
        self.assertIsNone(truncated["last_output_at"])
        self.assertFalse(truncated["quiet_suspected"])
        blank_later = shell_jobs.normalize_job(job, now=base + 1200)
        self.assertFalse(blank_later["quiet_suspected"])
        os.replace(path, self.root / "rotated.log")
        job["output_path"] = str(self.root / "rotated.log")
        rotated = shell_jobs.normalize_job(job, now=base + 602)
        self.assertEqual(rotated["output_coverage"], "sampled")

        done = dict(job, native_id="task-done", native_status="completed", exit_code=0,
                    completed_at=base + 1)
        shell_jobs.normalize_job(done, now=base + 2)
        completed_later = shell_jobs.normalize_job(done, now=base + 900)
        self.assertFalse(completed_later["quiet_suspected"])

        future = shell_jobs.normalize_job({"harness": "codex", "session_id": "s",
                                           "native_id": "future", "started_at": base + 100},
                                          now=base)
        self.assertIsNone(future["elapsed_s"])
        self.assertEqual(future["time_reason"], "future-start")
        future_output = self.root / "future-output"
        future_output.write_text("x", encoding="utf-8")
        os.utime(future_output, (base + 10, base + 10))
        invalid_output = shell_jobs.normalize_job(
            {"harness": "claude", "session_id": "sid-q", "native_id": "future-output",
             "output_path": str(future_output)}, now=base)
        self.assertEqual(invalid_output["output_time_state"], "future")
        self.assertIsNone(invalid_output["last_output_at"])
        missing_output = shell_jobs.normalize_job(
            {"harness": "claude", "session_id": "sid-q", "native_id": "missing-output",
             "output_path": str(self.root / "missing")}, now=base)
        self.assertEqual(missing_output["output_coverage"], "unavailable")
        self.assertEqual(missing_output["output_reason"], "FileNotFoundError")

        transition = self.root / "quiet-transitions.log"
        transition.write_text("x", encoding="utf-8")
        os.utime(transition, (base, base))
        transition_job = {"harness": "claude", "session_id": "sid-q",
                          "native_id": "quiet-transitions", "native_status": "acknowledged",
                          "process_state": "alive", "started_at": base - 1,
                          "output_path": str(transition)}
        self.assertFalse(shell_jobs.normalize_job(transition_job, now=base)["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(
            transition_job, now=base + 600)["quiet_suspected"])

        os.utime(transition, (base + 700, base + 700))
        future_after_quiet = shell_jobs.normalize_job(transition_job, now=base + 601)
        self.assertEqual(future_after_quiet["output_time_state"], "future")
        self.assertIsNone(future_after_quiet["last_output_at"])
        self.assertFalse(future_after_quiet["quiet_suspected"])
        session = Session(harness="claude", pid=10, session_id="sid-q", liveness="idle",
                          shell_jobs=[future_after_quiet])
        with mock.patch("os.stat", side_effect=AssertionError("render attempted observation I/O")):
            public = session.to_dict()
        self.assertFalse(public["shell_jobs"][0]["quiet_suspected"])
        rendered = self.context_text(session, 100, base + 601).lower()
        self.assertNotIn("quiet", rendered)

        os.utime(transition, (base + 701, base + 701))
        valid_again = shell_jobs.normalize_job(transition_job, now=base + 702)
        self.assertFalse(valid_again["quiet_suspected"])
        self.assertEqual(valid_again["quiet_since"], base + 702)
        self.assertFalse(shell_jobs.normalize_job(
            transition_job, now=base + 1301)["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(
            transition_job, now=base + 1302)["quiet_suspected"])

        invalid_stat = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=1, st_ino=2,
                                       st_size=1, st_mtime=float("nan"), st_mtime_ns=123)
        with mock.patch.object(shell_jobs.os, "stat", return_value=invalid_stat):
            invalid_after_quiet = shell_jobs.normalize_job(transition_job, now=base + 1303)
        self.assertEqual(invalid_after_quiet["output_time_state"], "invalid")
        self.assertEqual(invalid_after_quiet["output_time_reason"], "invalid-output-mtime")
        self.assertFalse(invalid_after_quiet["quiet_suspected"])
        reestablished = shell_jobs.normalize_job(transition_job, now=base + 1304)
        self.assertFalse(reestablished["quiet_suspected"])
        self.assertFalse(shell_jobs.normalize_job(
            transition_job, now=base + 1903)["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(
            transition_job, now=base + 1904)["quiet_suspected"])

        os.utime(transition, (base + 700, base + 700))
        mtime_clock_back = shell_jobs.normalize_job(transition_job, now=base + 1905)
        self.assertEqual(mtime_clock_back["output_time_reason"], "output-mtime-went-backwards")
        self.assertFalse(mtime_clock_back["quiet_suspected"])
        os.utime(transition, (base + 1906, base + 1906))
        shell_jobs.normalize_job(transition_job, now=base + 1907)
        observer_clock_back = shell_jobs.normalize_job(transition_job, now=base + 1900)
        self.assertEqual(observer_clock_back["output_time_reason"],
                         "observer-clock-went-backwards")
        self.assertFalse(observer_clock_back["quiet_suspected"])
        recovered_clock = shell_jobs.normalize_job(transition_job, now=base + 1908)
        self.assertFalse(recovered_clock["quiet_suspected"])
        self.assertFalse(shell_jobs.normalize_job(
            transition_job, now=base + 2507)["quiet_suspected"])
        self.assertTrue(shell_jobs.normalize_job(
            transition_job, now=base + 2508)["quiet_suspected"])

    def test_pid_reuse_namespace_and_exact_output_writer(self):
        path = self.root / "writer.log"
        with open(path, "wb") as writer:
            writer.write(b"running")
            writer.flush()
            pid = os.getpid()
            start = shell_jobs._proc_start(pid)
            namespace = shell_jobs._pid_namespace(pid)
            candidate = {"pid": pid, "root_pid": pid, "proc_start": start,
                         "ownership_verified": True,
                         "ancestry": [{"pid": pid, "ppid": os.getppid(), "start": start}]}
            exact = {"harness": "claude", "session_id": "sid-p", "native_id": "task-p",
                     "source_generation": "1", "output_path": str(path)}
            live = shell_jobs.correlate_output_writers(
                exact, [candidate], namespace=namespace, root_pid=pid)
            self.assertEqual(live["process_state"], "alive")

            verified = shell_jobs._VERIFIED_PROCESSES[shell_jobs._job_key(exact)]
            self.assertEqual(verified["observer_context"],
                             shell_jobs._observer_context().value)
            with mock.patch.object(shell_jobs, "_observer_context",
                                   return_value=shell_jobs.ProcObservation(
                                       "observed", verified["observer_context"])), \
                    mock.patch.object(shell_jobs, "_observe_proc_value",
                                      return_value=shell_jobs.ProcObservation(
                                          "unavailable", reason="PermissionError")):
                eacces = shell_jobs.correlate_output_writers(
                    exact, [], namespace=namespace)
            self.assertEqual(eacces["process_state"], "unknown")
            self.assertIn("PermissionError", eacces["process_reason"])

            reuse = dict(exact, native_id="task-reuse")
            wrong_generation = dict(candidate, proc_start="0")
            self.assertEqual(shell_jobs.correlate_output_writers(
                reuse, [wrong_generation], namespace=namespace, root_pid=pid)
                ["process_state"], "unknown")
            self.assertEqual(shell_jobs.correlate_output_writers(
                dict(exact, native_id="task-ns"), [candidate], namespace="pid:[different]",
                root_pid=pid)
                ["process_state"], "unknown")

            key = shell_jobs._job_key(reuse)
            shell_jobs._VERIFIED_PROCESSES[key] = {
                "pid": pid, "start": "0", "namespace": namespace,
                "observer_context": shell_jobs._observer_context().value,
                "ancestry": candidate["ancestry"],
            }
            dead_generation = shell_jobs.correlate_output_writers(
                reuse, [], namespace=namespace)
            self.assertEqual(dead_generation["process_state"], "dead")
        static_file = shell_jobs.correlate_output_writers(
            exact, [], namespace=namespace, root_pid=pid)
        self.assertEqual(static_file["process_state"], "unknown")

    def test_previous_writer_death_requires_same_observer_positive_evidence(self):
        path = self.root / "previous-writer.log"
        path.touch()
        job = {"harness": "claude", "session_id": "sid-old", "native_id": "task-old",
               "output_path": str(path)}
        key = shell_jobs._job_key(job)
        saved_context = (9000, "observer-start", "pid:[observer-pid]", "mnt:[observer-mnt]")
        previous = {"pid": 4242, "start": "100", "namespace": "pid:[target]",
                    "observer_context": saved_context, "ancestry": []}

        def classify(start, namespace, observer=saved_context):
            shell_jobs._VERIFIED_PROCESSES[key] = dict(previous)
            values = {"start": start, "pid_namespace": namespace}
            with mock.patch.object(shell_jobs, "_observer_context",
                                   return_value=shell_jobs.ProcObservation("observed", observer)), \
                    mock.patch.object(shell_jobs, "_observe_proc_value",
                                      side_effect=lambda _pid, kind: values[kind]):
                return shell_jobs.correlate_output_writers(job, [], namespace="pid:[target]")

        for failure in ("PermissionError", "OSError"):
            unreadable = classify(
                shell_jobs.ProcObservation("unavailable", reason=failure),
                shell_jobs.ProcObservation("unavailable", reason="PermissionError"))
            self.assertEqual(unreadable["process_state"], "unknown")
            self.assertIn(failure, unreadable["process_reason"])

        invisible_namespace = classify(
            shell_jobs.ProcObservation("observed", "100"),
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"))
        self.assertEqual(invisible_namespace["process_state"], "unknown")

        changed_observer = classify(
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"),
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"),
            observer=(9000, "observer-start", "pid:[changed]", "mnt:[observer-mnt]"))
        self.assertEqual(changed_observer["process_state"], "unknown")
        self.assertEqual(changed_observer["process_reason"], "observer-context-changed")

        changed_observer_process = classify(
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"),
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"),
            observer=(9001, "different-start", "pid:[observer-pid]", "mnt:[observer-mnt]"))
        self.assertEqual(changed_observer_process["process_state"], "unknown")
        self.assertEqual(changed_observer_process["process_reason"], "observer-context-changed")

        replaced = classify(
            shell_jobs.ProcObservation("observed", "101"),
            shell_jobs.ProcObservation("observed", "pid:[other]"))
        self.assertEqual(replaced["process_state"], "dead")
        self.assertEqual(replaced["process_reason"], "verified-process-generation-replaced")

        disappeared = classify(
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"),
            shell_jobs.ProcObservation("missing", reason="FileNotFoundError"))
        self.assertEqual(disappeared["process_state"], "dead")
        self.assertEqual(disappeared["process_reason"], "verified-process-disappeared")

        namespace_changed = classify(
            shell_jobs.ProcObservation("observed", "100"),
            shell_jobs.ProcObservation("observed", "pid:[other]"))
        self.assertEqual(namespace_changed["process_state"], "unknown")
        self.assertEqual(namespace_changed["process_reason"],
                         "verified-process-namespace-changed")

    def test_procscan_enumerates_multiple_verified_branches(self):
        tree = {1: (0, 100, "claude"), 2: (1, 5, "bash"),
                3: (2, 3, "python"), 4: (1, 2, "node")}
        kids = {1: [2, 4], 2: [3], 3: [], 4: []}
        identities = {1: (0, "1"), 2: (1, "2"), 3: (2, "3"), 4: (1, "4")}
        with mock.patch.object(procscan, "_exec_identity", side_effect=identities.get), \
                mock.patch.object(procscan, "_exec_is_helper", return_value=False), \
                mock.patch.object(procscan, "_exec_is_wrapper", side_effect=lambda _pid, comm: comm == "bash"):
            found = procscan.exec_children(1, tree=tree, kids=kids, min_age=0,
                                           expected_start="1")
        self.assertEqual({item["pid"] for item in found}, {3, 4})
        self.assertTrue(all(item["ownership_verified"] for item in found))

    def test_opencode_v2_documented_background_input_and_mutable_row(self):
        path = self.root / "opencode.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE session_v2 (
                id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER,
                title TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL,
                tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                parent_id TEXT
            );
            CREATE TABLE session_message (
                session_id TEXT, type TEXT, seq INTEGER, data TEXT,
                time_created INTEGER, time_updated INTEGER
            );
        """)
        sid = "ses_backgroundfixture123"
        connection.execute(
            "INSERT INTO session_v2(id,directory,time_created,time_updated,model) VALUES(?,?,?,?,?)",
            (sid, str(self.root), 1000, 1000, "{}"))

        def message(status, updated):
            return {"content": [{"type": "tool", "id": "part-shell-1", "name": "shell",
                                 "state": {"status": status, "input": {
                                     "command": "sleep 20", "background": True}},
                                 "time": {"created": 1000, "ran": 1000,
                                          "completed": 1001 if status == "completed" else None}}]}

        connection.execute(
            "INSERT INTO session_message VALUES(?,?,?,?,?,?)",
            (sid, "assistant", 1, json.dumps(message("completed", 1001)), 1000, 1001))
        connection.commit()
        connection.close()

        observed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=0)
        self.assertEqual(observed["generation"], "v2")
        self.assertEqual(len(observed["shell_tasks"]), 1)
        session = SimpleNamespace(pid=None, proc_start=None, attempt_id=None)
        opencode._attach_v2_shell_tasks(session, observed, path, sid)
        self.assertEqual(session.shell_jobs[0]["native_status"], "acknowledged")
        self.assertEqual(session.shell_jobs[0]["native_record_kind"],
                         "assistant-shell-tool-part")
        self.assertEqual(session.shell_jobs[0]["background_state"], "confirmed")
        self.assertEqual(session.shell_jobs[0]["background_source"],
                         "opencode.v2.assistant-shell.input-background-true")
        self.assertEqual(session.shell_jobs[0]["process_state"], "unknown")

        connection = sqlite3.connect(path)
        connection.execute("UPDATE session_message SET data=?,time_updated=? WHERE session_id=? AND seq=1",
                           (json.dumps(message("running", 2000)), 2000, sid))
        connection.commit()
        connection.close()
        updated = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=1)
        opencode._attach_v2_shell_tasks(session, updated, path, sid)
        self.assertEqual(len(session.shell_jobs), 1)
        self.assertEqual(session.shell_jobs[0]["invocation_status"], "running")
        self.assertEqual(session.shell_jobs[0]["native_status"], "acknowledged")

        connection = sqlite3.connect(path)
        connection.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?)",
                           (sid, "assistant", 2, "{invalid-json", 2000, 2001))
        connection.commit()
        connection.close()
        failed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v2", shell_after_seq=1)
        self.assertEqual(failed["shell_cursor"]["state"], "partial")
        opencode._attach_v2_shell_tasks(session, failed, path, sid)
        self.assertEqual(session.shell_job_observation["state"], "partial")
        self.assertEqual(session.shell_job_observation["coverage"], "partial")

    def test_opencode_native_shell_rows_flow_to_common_jobs_render_and_json(self):
        runtime = opencode._opencode_v2_runtime()
        snapshot_dir = self.root / "private-snapshot"
        snapshot_dir.mkdir()
        db = snapshot_dir / "opencode.db"
        native_root = self.root / "original-opencode-data"
        output_dir = native_root / "shell" / "native-location-key"
        output_dir.mkdir(parents=True)
        ok_output = output_dir / "sh-fixture-ok.out"
        ok_output.write_text("native output\n", encoding="utf-8")
        # A copied snapshot's parent is deliberately not the native shell root.
        decoy_dir = snapshot_dir / "shell" / "copied-location"
        decoy_dir.mkdir(parents=True)
        (decoy_dir / "sh-fixture-fail.out").write_text("stale copied output\n", encoding="utf-8")
        sid = "ses_nativefixture123"
        other_sid = "ses_otherfixture123"
        attempt_id = "attempt-opencode-fixture"
        now_ms = int(time.time() * 1000)

        def shell_row(shell_id, command, *, background=True, status="running",
                      completed=None, exit_code=None, output=None):
            row = {"metadata": {"background": background},
                   "time": {"created": now_ms - 5 * 60 * 60 * 1000},
                   "shellID": shell_id, "command": command, "status": status}
            if completed is not None:
                row["time"]["completed"] = completed
            if exit_code is not None:
                row["exit"] = exit_code
            if output is not None:
                row["output"] = output
            return row

        with sqlite3.connect(db) as connection:
            connection.executescript("""
                CREATE TABLE session_v2 (id TEXT, directory TEXT, time_created INTEGER,
                                         time_updated INTEGER);
                CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER,
                                              time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            connection.executemany("INSERT INTO session_v2 VALUES(?,?,?,?)", [
                (sid, str(self.root), now_ms - 10000, now_ms),
                (other_sid, str(self.root), now_ms - 10000, now_ms),
            ])
            rows = [
                ("m1", sid, "shell", 1, now_ms - 5 * 60 * 60 * 1000,
                 now_ms - 5 * 60 * 60 * 1000,
                 shell_row("sh-fixture-ok", "python3 -c 'print(1)'")),
                ("m2", sid, "shell", 2, now_ms - 5 * 60 * 60 * 1000 + 1000,
                 now_ms - 5 * 60 * 60 * 1000 + 1000,
                 shell_row("sh-fixture-fail", "python3 -c 'raise SystemExit(3)'")),
                ("m3", sid, "shell", 3, now_ms - 3000, now_ms - 3000,
                 shell_row("sh-ordinary", "echo ordinary", background=False,
                           status="exited", completed=now_ms - 2900, exit_code=0)),
                ("m4", sid, "shell", 4, now_ms - 2000, now_ms - 2000,
                 shell_row("sh-promoted", "echo promoted", background=False)),
                ("other", other_sid, "shell", 1, now_ms - 1000, now_ms - 1000,
                 shell_row("sh-other-session", "echo another session")),
            ]
            connection.executemany(
                "INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                [(mid, row_sid, kind, seq, created, updated, json.dumps(data))
                 for mid, row_sid, kind, seq, created, updated, data in rows])

        with mock.patch.dict(os.environ, {"OPENCODE_DATA_DIR": str(native_root)}):
            opencode._NATIVE_SHELL_OUTPUT_LOCATORS.clear()
            observed = runtime.read_session_observation(
                sid, db_path=db, generation="v2", shell_after_seq=0)
            self.assertEqual([item["native_id"] for item in observed["shell_tasks"]],
                             ["sh-fixture-ok", "sh-fixture-fail"])
            self.assertEqual(observed["shell_cursor"]["caught_up"], True)

            session = Session(harness="opencode", pid=4242, cwd=str(self.root),
                              proc_start="1", attempt_id=attempt_id)
            session.session_id = sid
            opencode._attach_v2_shell_tasks(session, observed, db, sid)
            self.assertEqual(len(session.shell_jobs), 2)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(jobs["sh-fixture-ok"]["native_status"], "running")
            self.assertEqual(jobs["sh-fixture-ok"]["current_execution_state"], "running")
            self.assertEqual(jobs["sh-fixture-ok"]["current_execution_source"],
                             "opencode.v2.session-message.exact-query")
            self.assertGreaterEqual(jobs["sh-fixture-ok"]["elapsed_s"], 4 * 60 * 60)
            self.assertEqual(jobs["sh-fixture-ok"]["background_state"], "confirmed")
            self.assertEqual(jobs["sh-fixture-ok"]["background_source"],
                             "opencode.v2.session-message.background-true")
            self.assertEqual(jobs["sh-fixture-ok"]["process_state"], "unknown")
            self.assertEqual(jobs["sh-fixture-ok"]["output_path"], str(ok_output))
            self.assertEqual(jobs["sh-fixture-fail"]["output_path"], None)
            self.assertEqual(jobs["sh-fixture-fail"]["output_reason"],
                             "native-shell-output-not-found")
            self.assertEqual(jobs["sh-fixture-fail"]["process_state"], "unknown")
            self.assertEqual(jobs["sh-fixture-fail"]["current_execution_state"], "running")
            row_text = self.context_text(session, 140, time.time())
            self.assertIn("python3 -c 'raise SystemExit(3)'", row_text)
            self.assertIn("(+1)", row_text)
            self.assertNotIn("background confirmed", row_text)
            self.assertNotIn("native running", row_text)
            public_json = session.to_dict()
            self.assertEqual(len(public_json["shell_jobs"]), 2)
            self.assertEqual(public_json["shell_jobs"][0]["native_record_kind"],
                             "session-message-shell")

            absent_query = dict(observed, shell_tasks=[], shell_cursor={
                "state": "partial", "reason": "fixture-row-not-returned"})
            opencode._attach_v2_shell_tasks(session, absent_query, db, sid,
                                            native_data_root=native_root)
            retained = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(retained["sh-fixture-ok"]["native_status"], "running")
            self.assertEqual(retained["sh-fixture-ok"]["current_execution_state"], "unknown")
            self.assertEqual(retained["sh-fixture-ok"]["current_execution_reason"],
                             "native-row-not-refreshed-this-collection")
            self.assertEqual(len(current_background_shell_jobs(session.shell_jobs)), 0)

            old_observation = observed
            terminal_time = int(time.time() * 1000)
            terminal_output = {"output": "fixture-secret-output", "cursor": 23,
                               "size": 23, "truncated": False}
            exited_ok = shell_row("sh-fixture-ok", "python3 -c 'print(1)'",
                                  status="exited", completed=terminal_time,
                                  exit_code=0, output=terminal_output)
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE session_message SET data=?, time_updated=? "
                                   "WHERE session_id=? AND seq=1",
                                   (json.dumps(exited_ok), terminal_time, sid))
            refresh = opencode._v2_shell_refresh_seqs(db, sid, attempt_id)
            updated = runtime.read_session_observation(
                sid, db_path=db, generation="v2",
                shell_after_seq=opencode._v2_shell_cursor(db, sid, attempt_id),
                shell_refresh_seqs=refresh)
            opencode._attach_v2_shell_tasks(session, updated, db, sid)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(jobs["sh-fixture-ok"]["lifecycle_state"], "completed")
            self.assertEqual(jobs["sh-fixture-ok"]["exit_code"], 0)
            self.assertEqual(jobs["sh-fixture-ok"]["process_state"], "dead")
            self.assertEqual(jobs["sh-fixture-ok"]["native_output_cursor"], 23)
            self.assertEqual(jobs["sh-fixture-ok"]["native_output_size"], 23)
            self.assertEqual(jobs["sh-fixture-ok"]["output_state"], "observed")
            terminal_elapsed = jobs["sh-fixture-ok"]["elapsed_s"]
            self.assertGreaterEqual(terminal_elapsed, 4 * 60 * 60)
            self.assertTrue(ok_output.exists())  # retained file did not revive the exited task

            # Replaying the older running snapshot must not resurrect a completed result.
            opencode._attach_v2_shell_tasks(session, old_observation, db, sid)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(jobs["sh-fixture-ok"]["native_status"], "exited")
            self.assertEqual(jobs["sh-fixture-ok"]["process_state"], "dead")
            self.assertEqual(jobs["sh-fixture-ok"]["elapsed_s"], terminal_elapsed)

            terminal_time = int(time.time() * 1000)
            exited_fail = shell_row("sh-fixture-fail", "python3 -c 'raise SystemExit(3)'",
                                    status="exited", completed=terminal_time, exit_code=3,
                                    output={"output": "", "cursor": 0, "size": 0,
                                            "truncated": False})
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE session_message SET data=?, time_updated=? "
                                   "WHERE session_id=? AND seq=2",
                                   (json.dumps(exited_fail), terminal_time, sid))
            refresh = opencode._v2_shell_refresh_seqs(db, sid, attempt_id)
            updated = runtime.read_session_observation(
                sid, db_path=db, generation="v2",
                shell_after_seq=opencode._v2_shell_cursor(db, sid, attempt_id),
                shell_refresh_seqs=refresh)
            opencode._attach_v2_shell_tasks(session, updated, db, sid)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(jobs["sh-fixture-fail"]["exit_code"], 3)
            self.assertEqual(jobs["sh-fixture-fail"]["lifecycle_state"], "completed")
            self.assertEqual(jobs["sh-fixture-fail"]["native_output_truncated"], False)

            # A row can become a background task in place; an ordinary shell row above stays hidden.
            terminal_time = int(time.time() * 1000)
            promoted = shell_row("sh-promoted", "echo promoted", status="exited",
                                 completed=terminal_time, exit_code=0,
                                 output={"output": "", "cursor": 0, "size": 0,
                                         "truncated": False})
            promoted["metadata"]["background"] = True
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE session_message SET data=?, time_updated=? "
                                   "WHERE session_id=? AND seq=4",
                                   (json.dumps(promoted), terminal_time, sid))
            refresh = opencode._v2_shell_refresh_seqs(db, sid, attempt_id)
            updated = runtime.read_session_observation(
                sid, db_path=db, generation="v2",
                shell_after_seq=opencode._v2_shell_cursor(db, sid, attempt_id),
                shell_refresh_seqs=refresh)
            opencode._attach_v2_shell_tasks(session, updated, db, sid)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertIn("sh-promoted", jobs)
            self.assertNotIn("sh-ordinary", jobs)
            self.assertEqual(jobs["sh-promoted"]["exit_code"], 0)
            self.assertNotIn("fixture-secret-output", json.dumps(session.to_dict()))

    def test_opencode_native_id_namespace_collision_is_visible_and_not_merged(self):
        runtime = opencode._opencode_v2_runtime()
        db = self.root / "opencode-collision.db"
        sid = "ses_collisionfixture123"
        now_ms = int(time.time() * 1000)
        with sqlite3.connect(db) as connection:
            connection.executescript("""
                CREATE TABLE session_v2 (id TEXT, directory TEXT, time_created INTEGER,
                                         time_updated INTEGER);
                CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER,
                                              time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            connection.execute("INSERT INTO session_v2 VALUES(?,?,?,?)",
                               (sid, str(self.root), now_ms, now_ms))
            shell = {"metadata": {"background": True}, "time": {"created": now_ms},
                     "shellID": "same-native-token", "command": "echo native",
                     "status": "running"}
            assistant = {"content": [{"type": "tool", "name": "shell",
                                      "id": "same-native-token",
                                      "state": {"status": "running", "input": {
                                          "background": True, "command": "echo assistant"}}}]}
            connection.executemany("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)", [
                ("shell", sid, "shell", 1, now_ms, now_ms, json.dumps(shell)),
                ("assistant", sid, "assistant", 2, now_ms, now_ms, json.dumps(assistant)),
            ])
        observed = runtime.read_session_observation(
            sid, db_path=db, generation="v2", shell_after_seq=0)
        session = Session(harness="opencode", pid=0, attempt_id="attempt-collision")
        session.session_id = sid
        opencode._attach_v2_shell_tasks(session, observed, db, sid)
        self.assertEqual(len(session.shell_jobs), 2)
        self.assertEqual({job["native_id_kind"] for job in session.shell_jobs},
                         {"session-message-shellID", "assistant-tool-part"})
        self.assertTrue(all(job["identity_link_state"] == "ambiguous-cross-namespace-id"
                            for job in session.shell_jobs))
        self.assertEqual(session.shell_job_observation["state"], "partial")
        rendered = self.context_text(session, 140, time.time())
        self.assertIn("echo native", rendered)
        self.assertNotIn("echo assistant", rendered)
        self.assertNotIn("ambiguous", rendered)

    def test_opencode_database_identity_change_retires_running_and_keeps_terminal_result(self):
        runtime = opencode._opencode_v2_runtime()
        db = self.root / "opencode-generation.db"
        sid = "ses_generationfixture123"
        attempt_id = "attempt-generation"
        now_ms = int(time.time() * 1000)
        with sqlite3.connect(db) as connection:
            connection.executescript("""
                CREATE TABLE session_v2 (id TEXT, directory TEXT, time_created INTEGER,
                                         time_updated INTEGER);
                CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER,
                                              time_created INTEGER, time_updated INTEGER, data TEXT);
            """)
            connection.execute("INSERT INTO session_v2 VALUES(?,?,?,?)",
                               (sid, str(self.root), now_ms, now_ms))
            rows = [
                ("done", sid, "shell", 1, now_ms, now_ms, json.dumps({
                    "metadata": {"background": True}, "time": {"created": now_ms - 2000,
                                                                  "completed": now_ms},
                    "shellID": "sh-generation-done", "command": "echo done",
                    "status": "exited", "exit": 0,
                })),
                ("pending", sid, "shell", 2, now_ms, now_ms, json.dumps({
                    "metadata": {"background": True}, "time": {"created": now_ms - 1000},
                    "shellID": "sh-generation-pending", "command": "sleep 20",
                    "status": "running",
                })),
            ]
            connection.executemany("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)", rows)
        native_root = self.root / "generation-data"
        with mock.patch.dict(os.environ, {"OPENCODE_DATA_DIR": str(native_root)}):
            opencode._NATIVE_SHELL_OUTPUT_LOCATORS.clear()
            observed = runtime.read_session_observation(
                sid, db_path=db, generation="v2", shell_after_seq=0)
            session = SimpleNamespace(pid=None, proc_start=None, attempt_id=attempt_id)
            opencode._attach_v2_shell_tasks(session, observed, db, sid)
            self.assertEqual(len(session.shell_jobs), 2)
            replacement = dict(observed)
            replacement["database_identity"] = {
                "device": observed["database_identity"]["device"] + 1,
                "inode": observed["database_identity"]["inode"] + 1,
            }
            replacement["shell_tasks"] = []
            replacement["shell_cursor"] = {"state": "observed", "next_seq": 0,
                                            "caught_up": True, "mutable_seqs": [],
                                            "refreshed_seqs": []}
            opencode._attach_v2_shell_tasks(session, replacement, db, sid)
            jobs = {item["native_id"]: item for item in session.shell_jobs}
            self.assertEqual(jobs["sh-generation-done"]["exit_code"], 0)
            self.assertEqual(jobs["sh-generation-done"]["lifecycle_state"], "completed")
            self.assertEqual(jobs["sh-generation-pending"]["process_state"], "unknown")
            self.assertEqual(jobs["sh-generation-pending"]["observation_reason"],
                             "database-generation-changed")
            copied_source = dict(jobs["sh-generation-done"],
                                 source_locator=str(self.root / "copied-snapshot.db"))
            self.assertEqual(len(shell_jobs.merge_jobs(
                [jobs["sh-generation-done"]], [copied_source])), 2)

    def test_opencode_native_output_locator_reports_duplicate_and_scan_limit(self):
        native_root = self.root / "native-locator-root"
        shell_root = native_root / "shell"
        (shell_root / "location-a").mkdir(parents=True)
        (shell_root / "location-b").mkdir()
        (shell_root / "location-a" / "sh-duplicate.out").write_text("a", encoding="utf-8")
        (shell_root / "location-b" / "sh-duplicate.out").write_text("b", encoding="utf-8")
        with mock.patch.dict(os.environ, {"OPENCODE_DATA_DIR": str(native_root)}):
            opencode._NATIVE_SHELL_OUTPUT_LOCATORS.clear()
            private_root = self.root / "attempt-original-data"
            private_file = private_root / "shell" / "attempt-location" / "sh-explicit-root.out"
            private_file.parent.mkdir(parents=True)
            private_file.write_text("attempt output", encoding="utf-8")
            explicit_root = opencode._native_shell_output_locator(
                "sh-explicit-root", data_root=private_root)
            self.assertEqual(explicit_root["path"], str(private_file))
            ambiguous = opencode._native_shell_output_locator("sh-duplicate")
            self.assertEqual(ambiguous["state"], "ambiguous")
            self.assertEqual(ambiguous["reason"], "native-shell-output-id-ambiguous")

            for index in range(opencode._NATIVE_SHELL_LOCATION_LIMIT + 1):
                (shell_root / ("location-%03d" % index)).mkdir()
            opencode._NATIVE_SHELL_OUTPUT_LOCATORS.clear()
            limited = opencode._native_shell_output_locator("sh-absent")
            self.assertEqual(limited["state"], "partial")
            self.assertEqual(limited["reason"], "native-shell-location-scan-limit")

    def test_opencode_v1_does_not_inherit_v2_background_semantics(self):
        path = self.root / "opencode-v1.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE session (
                id TEXT, directory TEXT, time_created INTEGER, time_updated INTEGER,
                title TEXT, slug TEXT, agent TEXT, model TEXT, cost REAL,
                tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                parent_id TEXT
            );
            CREATE TABLE message (
                id TEXT, session_id TEXT, role TEXT, time_created INTEGER,
                time_updated INTEGER, data TEXT
            );
            CREATE TABLE part (session_id TEXT, message_id TEXT, data TEXT);
        """)
        sid = "ses_v1fixture123"
        connection.execute("INSERT INTO session(id,directory,time_created,time_updated) VALUES(?,?,?,?)",
                           (sid, str(self.root), 1000, 1000))
        connection.execute("INSERT INTO message VALUES(?,?,?,?,?,?)",
                           ("m1", sid, "assistant", 1000, 1000, "{}"))
        connection.execute("INSERT INTO part VALUES(?,?,?)",
                           (sid, "m1", json.dumps({"type": "tool", "tool": "bash",
                                                   "state": {"status": "running"}})))
        connection.commit()
        connection.close()
        observed = opencode._opencode_v2_runtime().read_session_observation(
            sid, db_path=path, generation="v1")
        self.assertEqual(observed["generation"], "v1")
        self.assertEqual(observed["shell_tasks"], [])
        self.assertEqual(observed["shell_cursor"]["state"], "unobserved")

    def test_current_selector_filters_stale_retired_terminal_and_deduplicates_exact_identity(self):
        now = 1000.0

        def current(native_id, **changes):
            job = {
                "harness": "codex", "session_id": "thread-current", "attempt_id": None,
                "source_generation": "gen-a", "source_locator": "/tmp/current.jsonl",
                "native_id": native_id, "native_status": "running",
                "lifecycle_state": "running", "background_state": "confirmed",
                "background_source": "codex.exec_command.native-continuation-handle",
                "current_generation_valid": True, "current_execution_state": "running",
                "current_execution_source": "codex.exec_command.native-continuation-handle",
                "current_execution_at": now - 120, "started_at": now - 900,
                "process_state": "unknown", "command": "echo " + native_id,
            }
            job.update(changes)
            return job

        boundary = current("boundary", observation_state="partial")
        duplicate = dict(boundary, command="ignored duplicate")
        terminal_live = current("terminal-collision")
        terminal_record = dict(terminal_live, native_status="completed",
                               lifecycle_state="completed", completed_at=now - 1)
        no_start = current("no-start", started_at=None, current_execution_at=now - 1)
        stale = current("stale", current_execution_at=now - 121)
        future = current("future", current_execution_at=now + 1)
        invalid = current("invalid", current_execution_at=True)
        too_large = current("too-large", current_execution_at=10 ** 500)
        retired = current("retired", current_generation_valid=False)
        unknown_background = current("unknown-background", background_state="unknown")
        missing_background_source = current("missing-background-source", background_source=None)
        invalid_attempt = current("invalid-attempt", attempt_id=7)
        missing_identity = current("missing")
        missing_identity["native_id"] = ""
        selected = current_background_shell_jobs([
            boundary, duplicate, terminal_live, terminal_record, no_start, stale,
            future, invalid, too_large, retired, unknown_background,
            missing_background_source, invalid_attempt, missing_identity,
        ], now=now)
        self.assertEqual([job["native_id"] for job in selected], ["no-start", "boundary"])
        self.assertEqual(selected[0]["elapsed_s"], None)
        self.assertEqual(selected[1]["elapsed_s"], 900)
        self.assertEqual(current_background_shell_jobs([boundary], now=10 ** 500), [])

    def test_codex_continuation_evidence_survives_reread_but_expires_after_120_seconds(self):
        path = self.root / "continuation-evidence.jsonl"
        self.write_jsonl(path, [
            {"timestamp": 1000, "payload": {
                "type": "custom_tool_call", "call_id": "exec-quiet",
                "name": "exec_command", "input": {"cmd": "python train.py"}}},
            {"timestamp": 19000, "payload": {
                "type": "custom_tool_call_output", "call_id": "exec-quiet",
                "output": {"session_id": 44, "output": ""}}},
        ])
        first = shell_jobs.read_codex_jobs(path, session_id="thread-quiet", now=19000)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["current_execution_state"], "running")
        self.assertEqual(first[0]["current_execution_at"], 19000)
        self.assertEqual(first[0]["native_event_at"], 19000)
        self.assertEqual(first[0]["elapsed_s"], 18000)
        self.assertEqual(first[0]["last_output_at"], None)

        at_boundary = shell_jobs.read_codex_jobs(path, session_id="thread-quiet", now=19120)
        self.assertEqual(at_boundary[0]["current_execution_state"], "running")
        self.assertEqual(at_boundary[0]["current_execution_at"], 19000)
        self.assertEqual(at_boundary[0]["native_event_at"], 19000)
        self.assertEqual(at_boundary[0]["observed_at"], 19120)
        self.assertEqual(len(current_background_shell_jobs(at_boundary, now=19120)), 1)

        stale = shell_jobs.read_codex_jobs(path, session_id="thread-quiet", now=19121)
        self.assertEqual(stale[0]["current_execution_state"], "unknown")
        self.assertEqual(stale[0]["current_execution_reason"],
                         "current-execution-evidence-stale")
        self.assertEqual(stale[0]["lifecycle_state"], "running")
        self.assertEqual(stale[0]["native_event_at"], 19000)
        self.assertEqual(stale[0]["observed_at"], 19121)
        self.assertEqual(current_background_shell_jobs(stale, now=19121), [])

    def test_exact_writer_evidence_refreshes_per_collection_and_old_alive_is_not_reused(self):
        now = 20000.0
        output = self.root / "writer.out"
        output.write_text("quiet", encoding="utf-8")
        job = {
            "harness": "claude", "session_id": "sid-writer", "attempt_id": None,
            "source_generation": "gen-writer", "source_locator": "/tmp/writer.jsonl",
            "native_id": "task-writer", "native_status": "acknowledged",
            "background_state": "confirmed", "background_source": "native-task-id",
            "current_generation_valid": True, "current_execution_state": "unknown",
            "current_execution_reason": "background-acknowledgement-is-not-execution-proof",
            "started_at": now - 18000, "output_path": str(output),
            "process_state": "unknown", "output_coverage": "sampled",
        }
        exact = {"process_state": "alive",
                 "process_reason": "exact-descendant-writable-output-fd",
                 "process_identity": {"pid": 9, "start": "99", "namespace": "pid:[1]"}}
        with mock.patch.object(shell_jobs, "correlate_output_writers", return_value=exact):
            observed = shell_jobs.observe_jobs([job], now=now)
        self.assertEqual(observed[0]["current_execution_state"], "running")
        self.assertEqual(observed[0]["current_execution_at"], now)
        self.assertEqual(len(current_background_shell_jobs(observed, now=now)), 1)

        with mock.patch.object(shell_jobs, "correlate_output_writers", return_value={
                "process_state": "unknown", "process_reason": "exact-writer-not-observed"}):
            reread = shell_jobs.observe_jobs(observed, now=now + 1)
        self.assertEqual(reread[0]["process_state"], "unknown")
        self.assertEqual(reread[0]["current_execution_state"], "unknown")
        self.assertEqual(reread[0]["current_execution_reason"],
                         "exact-writer-not-observed-this-collection")
        self.assertEqual(current_background_shell_jobs(reread, now=now + 1), [])

    def test_now_tail_shrinks_command_then_count_then_omits_without_adding_rows(self):
        now = 30000.0
        shell_jobs = []
        for index, started in enumerate((now - 600, now - 300, now - 100)):
            shell_jobs.append({
                "harness": "codex", "session_id": "thread-width", "attempt_id": None,
                "source_generation": "gen-width", "source_locator": "/tmp/width.jsonl",
                "native_id": "job-%d" % index, "native_id_kind": "exec-handle",
                "native_status": "running", "lifecycle_state": "running",
                "background_state": "confirmed", "background_source": "fixture",
                "current_generation_valid": True, "current_execution_state": "running",
                "current_execution_source": "codex.exec_command.native-continuation-handle",
                "current_execution_at": now, "started_at": started,
                "process_state": "unknown", "command": "python train.py --run " + "x" * 70,
            })
        session = Session(harness="codex", pid=5, session_id="thread-width", liveness="idle",
                          summary="the existing NOW sentence remains first", shell_jobs=shell_jobs)
        latest = current_background_shell_jobs(shell_jobs, now=now)[0]
        full = render._background_shell_tail(session, now=now, width=100)
        clipped = render._background_shell_tail(session, now=now, width=18)
        count_width = render._dw("⚙ 3")
        count_only = render._background_shell_tail(session, now=now, width=count_width)
        omitted = render._background_shell_tail(session, now=now, width=count_width - 1)
        self.assertEqual(latest["native_id"], "job-2")
        self.assertIn("python train.py", full)
        self.assertIn("(+2)", full)
        self.assertIn("1m", clipped)
        self.assertIn("(+2)", clipped)
        self.assertIn("…", clipped)
        self.assertEqual(count_only, "⚙ 3")
        self.assertIsNone(omitted)

        with mock.patch("time.time", return_value=now):
            baseline_lines = render._context_detail_row(
                Session(harness="codex", pid=5, session_id="thread-width", liveness="idle",
                       summary=session.summary), term_width=100)
            with_jobs_lines = render._context_detail_row(session, term_width=100)
        self.assertEqual(len(baseline_lines), 1)
        self.assertEqual(len(with_jobs_lines), 1)
        baseline = "".join(text for text, _key in baseline_lines[0])
        rendered = "".join(text for text, _key in with_jobs_lines[0])
        self.assertEqual(rendered, baseline)
        self.assertNotIn("(+2)", rendered)
        self.assertLessEqual(render._dw(rendered), 100)
        self.assertEqual(session.to_dict()["liveness"], "idle")

        no_start_job = dict(shell_jobs[0], native_id="no-start", started_at=None,
                            current_execution_at=now - 1)
        no_start_session = Session(harness="codex", pid=5, session_id="thread-width",
                                   liveness="idle", shell_jobs=[no_start_job])
        no_start_tail = render._background_shell_tail(no_start_session, now=now, width=100)
        self.assertIn(" ?", no_start_tail)

        cjk_jobs = [dict(job) for job in shell_jobs]
        cjk_jobs[-1]["command"] = "漢字 train " + "界" * 50
        resource_owner = Session(harness="codex", pid=5, session_id="thread-width",
                                 liveness="idle", summary="ordinary summary",
                                 shell_jobs=cjk_jobs)
        resource_base = Session(harness="codex", pid=5, session_id="thread-width",
                                liveness="idle", summary="ordinary summary")
        with mock.patch("time.time", return_value=now), \
                mock.patch.object(render, "_resource_now_text", return_value="GPU train status"):
            base_lines = render._context_detail_row(resource_base, term_width=168)
            resource_lines = render._context_detail_row(resource_owner, term_width=168)
        self.assertEqual(len(base_lines), 1)
        self.assertEqual(len(resource_lines), 1)
        base_text = "".join(text for text, _key in base_lines[0])
        resource_text = "".join(text for text, _key in resource_lines[0])
        self.assertTrue(resource_text.startswith(base_text))
        self.assertIn("GPU train status", resource_text)
        self.assertIn("⚙", resource_text)
        self.assertIn("…", resource_text)
        self.assertLessEqual(render._dw(resource_text), 168)


if __name__ == "__main__":
    unittest.main()
