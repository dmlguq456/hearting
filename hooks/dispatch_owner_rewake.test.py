#!/usr/bin/env python3
"""Tests for the one-shot Claude interactive owner completion bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("dispatch-owner-rewake.py")
SPEC = importlib.util.spec_from_file_location("dispatch_owner_rewake", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
rewake = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rewake
SPEC.loader.exec_module(rewake)


class DispatchOwnerRewakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("fixture\n", encoding="utf-8")

    def payload(self, **replacements):
        output = "\n".join(
            (
                "status=eligible",
                "check=ok",
                "status=start",
                "dispatch_depth=1",
                "worker_type=owner",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_session_id=session-1",
                f"job_registry={self.jobs}",
                "attempt_id=att-owner-1",
                "registered=1",
                "started=1",
            )
        )
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-1",
            "tool_input": {
                "command": "python3 utilities/dispatch-owner.py --start --slug owner"
            },
            "tool_response": {"stdout": output, "stderr": ""},
        }
        payload.update(replacements)
        return payload

    def test_exact_successful_owner_start_is_armed(self) -> None:
        launch = rewake.parse_launch(self.payload())
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")
        self.assertEqual(launch.jobs, self.jobs)
        self.assertEqual(launch.session_id, "session-1")

    def test_foreign_or_incomplete_output_is_ignored(self) -> None:
        foreign = self.payload(session_id="session-2")
        self.assertIsNone(rewake.parse_launch(foreign))
        ordinary = self.payload(tool_input={"command": "git status"})
        self.assertIsNone(rewake.parse_launch(ordinary))
        incomplete = self.payload()
        incomplete["tool_response"]["stdout"] = incomplete["tool_response"]["stdout"].replace(
            "started=1", "started=0"
        )
        self.assertIsNone(rewake.parse_launch(incomplete))

    def test_symlink_registry_is_rejected(self) -> None:
        link = self.root / "jobs-link.log"
        link.symlink_to(self.jobs)
        payload = self.payload()
        payload["tool_response"]["stdout"] = payload["tool_response"]["stdout"].replace(
            str(self.jobs), str(link)
        )
        self.assertIsNone(rewake.parse_launch(payload))

    def test_wait_is_one_process_until_exact_attempt_is_ready(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        ready = subprocess.CompletedProcess([], 0, stdout="ready")
        with mock.patch.object(
            rewake.subprocess, "run", side_effect=[pending, ready]
        ) as run, mock.patch.object(rewake.time, "sleep") as sleep, mock.patch.object(
            rewake.time, "monotonic", side_effect=[0.0, 1.0]
        ), mock.patch.dict(
                os.environ,
                {
                    "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "3",
                    "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "60",
                },
            ):
            state, reason = rewake.wait_for_attempt(launch, self.root / "ready.py")
        self.assertEqual((state, reason), ("ready", "terminal-quiescent"))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(3)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[-2:], ["--attempt-id", "att-owner-1"])
        self.assertIn(str(self.jobs), command)

    def test_receipt_forbids_visible_monitor_rearming(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass\n",
            encoding="utf-8",
        )
        message = rewake.receipt(launch, "ready", "terminal-quiescent", self.root)
        self.assertIn("attempt_id=att-owner-1", message)
        self.assertIn("Do not start or re-arm Background Bash", message)
        self.assertIn("required_action=advance-completed", message)
        self.assertIn("No harvest command is required", message)
        self.assertNotIn("harvest --attempt-id", message)

    def test_terminal_failure_receipt_uses_matching_status(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,note=dead-worker-fail\n",
            encoding="utf-8",
        )
        message = rewake.receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertIn("required_action=inspect-done-failure", message)
        self.assertIn(
            "harvest --attempt-id att-owner-1 --status done --failure-detail",
            message,
        )

    def test_stale_ready_snapshot_uses_current_done_failure_action(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,note=dead-worker-fail\n",
            encoding="utf-8",
        )
        message = rewake.receipt(launch, "ready", "terminal-quiescent", self.root)
        self.assertIn("reason=row-advanced", message)
        self.assertIn("required_action=inspect-done-failure", message)
        self.assertNotIn("--status open", message)

    def test_unrelated_hook_payload_is_a_silent_noop(self) -> None:
        payload = self.payload(tool_name="Read")
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(__import__("json").dumps(payload))):
            self.assertEqual(rewake.main(), 0)

    def test_intact_stdout_never_consults_the_registry(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.assertEqual(launch.armed, "stdout")
        payload = self.payload()
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload))), (
            mock.patch.object(rewake, "registry_launch")
        ) as fallback, mock.patch.object(
            rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")
        ), mock.patch.object(rewake.sys, "stderr", io.StringIO()):
            self.assertEqual(rewake.main(), 2)
        fallback.assert_not_called()


class RegistryConfirmArmTest(unittest.TestCase):
    """A filtered `dispatch-owner --start` stdout still arms from the registry."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text(self.row(), encoding="utf-8")
        self.environment = mock.patch.dict(
            os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}, clear=False
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def stamp(self, age_seconds: float = 0.0) -> str:
        moment = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        return moment.isoformat().replace("+00:00", "Z")

    def row(
        self,
        *,
        attempt_id: str = "att-owner-1",
        status: str = "open",
        parent_sid: str = "session-1",
        age_seconds: float = 0.0,
        **overrides: str,
    ) -> str:
        metadata = {
            "capability": "autopilot-code",
            "dispatch_depth": "1",
            "worker_type": "owner",
            "parent_sid": parent_sid,
            "parent_completion_delivery": "claude-parent-runtime",
            "launch_claimed": "1",
            "launch_started": "1",
            "attempt_id": attempt_id,
        }
        metadata.update(overrides)
        pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
        columns = [self.stamp(age_seconds), status, "/repo", "/repo", "slug", pipe]
        return "\t".join(columns) + "\n"

    def payload(self, *, stdout: str = "check=ok", **replacements):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-1",
            "tool_input": {
                "command": "python3 utilities/dispatch-owner.py --start --slug owner | tail -5"
            },
            "tool_response": {"stdout": stdout, "stderr": ""},
        }
        payload.update(replacements)
        return payload

    def test_filtered_stdout_arms_from_the_single_matching_row(self) -> None:
        launch = rewake.registry_launch(self.payload())
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")
        self.assertEqual(launch.jobs, self.jobs)
        self.assertEqual(launch.session_id, "session-1")
        self.assertEqual(launch.armed, "registry")

    def test_registry_armed_run_reaches_the_unchanged_wait_path(self) -> None:
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), (
            mock.patch.object(
                rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")
            )
        ) as wait, mock.patch.object(rewake.sys, "stderr", io.StringIO()) as stderr:
            self.assertEqual(rewake.main(), 2)
            message = stderr.getvalue()
        self.assertEqual(wait.call_args.args[0].attempt_id, "att-owner-1")
        self.assertIn("armed=registry", message)
        self.assertIn("attempt_id=att-owner-1", message)

    def test_foreign_stale_ambiguous_or_sessionless_input_never_arms(self) -> None:
        self.jobs.write_text(self.row(parent_sid="session-2"), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(self.row(age_seconds=4_000), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(
            self.row() + self.row(attempt_id="att-owner-2"), encoding="utf-8"
        )
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(self.row(), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload(session_id="")))
        self.assertIsNone(rewake.registry_launch(self.payload(session_id=None)))

    def test_closed_or_non_owner_rows_never_arm(self) -> None:
        self.jobs.write_text(
            self.row() + self.row(status="done", note="completed-marker"), encoding="utf-8"
        )
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(self.row(dispatch_depth="2"), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(self.row(launch_started="0"), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload()))
        self.jobs.write_text(
            self.row(parent_completion_delivery="one-shot"), encoding="utf-8"
        )
        self.assertIsNone(rewake.registry_launch(self.payload()))

    def test_command_jobs_argument_wins_and_symlinks_are_rejected(self) -> None:
        other = self.root / "explicit.log"
        other.write_text(self.row(attempt_id="att-owner-explicit"), encoding="utf-8")
        payload = self.payload()
        payload["tool_input"]["command"] = (
            f"python3 utilities/dispatch-owner.py --start --jobs {other} --slug owner | tail -5"
        )
        launch = rewake.registry_launch(payload)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-explicit")
        self.assertEqual(launch.jobs, other)
        link = self.root / "jobs-link.log"
        link.symlink_to(other)
        payload["tool_input"]["command"] = (
            f"python3 utilities/dispatch-owner.py --start --jobs={link} --slug owner | tail -5"
        )
        linked = rewake.registry_launch(payload)
        assert linked is not None
        self.assertEqual(linked.jobs, self.jobs)

    def test_a_start_free_dispatch_owner_command_never_arms(self) -> None:
        payload = self.payload()
        payload["tool_input"]["command"] = "python3 utilities/dispatch-owner.py --status"
        self.assertIsNone(rewake.registry_launch(payload))

    def test_space_delimited_registry_metadata_is_tolerated(self) -> None:
        self.jobs.write_text(self.row().replace(",", " "), encoding="utf-8")
        launch = rewake.registry_launch(self.payload())
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")


if __name__ == "__main__":
    unittest.main()
