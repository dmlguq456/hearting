#!/usr/bin/env python3
"""Tests for the one-shot Claude interactive owner completion bridge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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

    def test_exact_successful_quick_dispatch_node_start_is_armed(self) -> None:
        payload = self.payload()
        payload["tool_input"]["command"] = (
            "python3 utilities/dispatch-node.py --route route.json --node one-shot "
            "--action start --slug quick --adapter claude"
        )
        launch = rewake.parse_launch(payload)
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")

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
        echoed = self.payload(
            tool_input={
                "command": "echo utilities/dispatch-node.py --action start"
            }
        )
        self.assertIsNone(rewake.parse_launch(echoed))

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
        marker = self.root / "plan.json"
        marker.write_text(
            json.dumps(
                {
                    "route_id": "rt-owner",
                    "route_hash": "sha256:owner",
                    "node_id": "plan",
                    "attempt_id": "att-owner-1",
                }
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass,"
            "note=completed-marker,route_id=rt-owner,route_hash=sha256:owner,"
            f"route_node=plan,completion_marker={marker},"
            "launch_outcome=never-launched\n",
            encoding="utf-8",
        )
        message = rewake.receipt(launch, "ready", "terminal-quiescent", self.root)
        self.assertIn("attempt_id=att-owner-1", message)
        self.assertIn("Do not start or re-arm Background Bash", message)
        self.assertIn("required_action=inspect-done-failure", message)
        self.assertIn("state=attention", message)
        self.assertIn("Hearting dispatch requires attention", message)
        self.assertIn("--status done --failure-detail", message)

    def write_marker_bound_owner(self, *, status: str = "open", child: bool = False):
        evidence = self.root / "report.md"
        evidence.write_text("fixture report\n", encoding="utf-8")
        route = self.root / "route.json"
        route_value = {
            "route_id": "rt-owner",
            "route_hash": "sha256:owner",
            "registry_digest": "sha256:registry",
            "nodes": [{
                "id": "report",
                "completion_gate": "code-report",
                "dispatch_depth": 1,
            }],
        }
        route.write_text(json.dumps(route_value), encoding="utf-8")
        marker = self.root / "owner-marker.json"
        marker_value = {
            "schema_version": 2,
            "sequence": 1,
            "route_id": "rt-owner",
            "route_hash": "sha256:owner",
            "registry_digest": "sha256:registry",
            "node_id": "report",
            "completion_gate": "code-report",
            "attempt_id": "att-owner-1",
            "dispatch_depth": 1,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": True,
            "fallback_hop": "same-harness-headless",
            "evidence": {
                "path": str(evidence),
                "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            },
        }
        marker.write_text(json.dumps(marker_value), encoding="utf-8")
        (self.root / "report.1.json").write_text(
            json.dumps(marker_value), encoding="utf-8"
        )
        (self.root / "report.att-owner-1.attempt.json").write_text(
            json.dumps({
                "schema_version": 2,
                "route_id": "rt-owner",
                "node_id": "report",
                "attempt_id": "att-owner-1",
                "dispatch_depth": 1,
                "transport": "headless",
                "execution_surface": "registered-headless",
                "registered_worker": True,
                "fallback_hop": "same-harness-headless",
                "evidence_sha256": marker_value["evidence"]["sha256"],
                "completion_marker": str(marker),
                "completion_marker_history": str(self.root / "report.1.json"),
            }),
            encoding="utf-8",
        )
        terminal = (
            ",failure_class=pass,note=completed-marker" if status == "done" else ""
        )
        target = (
            f"2026-08-25T00:00:00Z\t{status}\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,attempt_id=att-owner-1,"
            "route_id=rt-owner,route_hash=sha256:owner,route_node=report,"
            f"route_file={route},completion_marker={marker},"
            f"launch_outcome=never-launched{terminal}\n"
        )
        owned = ""
        if child:
            owned = (
                "2026-08-25T00:00:01Z\topen\t/repo\t/wt\tchild\t"
                "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,attempt_id=att-child-1,"
                "parent_attempt_id=att-owner-1\n"
            )
        self.jobs.write_text(target + owned, encoding="utf-8")
        return marker

    def test_marker_open_row_advances_once_then_renders_success(self) -> None:
        self.write_marker_bound_owner()
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        state, message = rewake.classified_receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertEqual(state, "success")
        self.assertIn("advanced=1", message)
        self.assertIn("marker_current=1", message)
        self.assertIn("state=success", message)
        self.assertIn("\tdone\t", self.jobs.read_text(encoding="utf-8"))
        self.assertEqual(
            self.jobs.read_text(encoding="utf-8").count("classifier_source=marker-bound-delivery-v1"),
            1,
        )

    def test_marker_open_row_failed_advance_is_nonblocking_attention(self) -> None:
        self.write_marker_bound_owner()
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        delivery = rewake.CurrentDeliveryState(
            marker={
                "route_id": "rt-owner",
                "route_hash": "sha256:owner",
                "node_id": "report",
                "attempt_id": "att-owner-1",
            },
            marker_digest="a" * 64,
            row_revision="b" * 64,
            row_digest="b" * 64,
            status="open",
            verdict="",
            quiescent=False,
            owned_children=0,
            advanced=False,
        )
        with mock.patch.object(rewake, "current_delivery_state", return_value=delivery):
            state, message = rewake.classified_receipt(
                launch, "attention", "terminal-failure-or-unclosed", self.root
            )
        self.assertEqual(state, "attention")
        self.assertIn("advanced=0", message)
        self.assertIn("owned_children=0", message)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            sys, "stderr", stderr
        ):
            rc = rewake.emit_receipt(
                state, message, block=rewake._attention_has_open_child(message)
            )
        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["systemMessage"], message)

    def test_only_a_real_open_owned_child_blocks_attention(self) -> None:
        self.write_marker_bound_owner(status="done", child=True)
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        state, message = rewake.classified_receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertEqual(state, "attention")
        self.assertIn("owned_children=1", message)
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(
                rewake.emit_receipt(
                    state, message, block=rewake._attention_has_open_child(message)
                ),
                2,
            )

    def test_delivery_transaction_error_without_open_child_is_nonblocking_attention(self) -> None:
        self.write_marker_bound_owner(status="done")
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        with mock.patch.object(
            rewake,
            "current_delivery_state",
            side_effect=rewake.DispatchContractError("delivery-cas-race"),
        ):
            state, message = rewake.classified_receipt(
                launch, "attention", "terminal-failure-or-unclosed", self.root
            )
        self.assertEqual(state, "attention")
        self.assertIn("owned_children=0", message)
        self.assertIn("reason=delivery-transaction-failed-delivery-cas-race", message)
        self.assertFalse(rewake._attention_has_open_child(message))

    def test_delivery_transaction_error_with_real_open_child_still_blocks(self) -> None:
        self.write_marker_bound_owner(status="done", child=True)
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        with mock.patch.object(
            rewake,
            "current_delivery_state",
            side_effect=rewake.DispatchContractError("delivery-cas-race"),
        ):
            state, message = rewake.classified_receipt(
                launch, "attention", "terminal-failure-or-unclosed", self.root
            )
        self.assertEqual(state, "attention")
        self.assertIn("owned_children=1", message)
        self.assertTrue(rewake._attention_has_open_child(message))

    def test_stop_failure_remains_api_error_notification_only(self) -> None:
        payload = self.payload(hook_event_name="StopFailure")
        with mock.patch.object(
            rewake.sys, "stdin", io.StringIO(json.dumps(payload))
        ), mock.patch.object(rewake.sys, "stdout", io.StringIO()) as stdout, mock.patch.object(
            rewake.sys, "stderr", io.StringIO()
        ) as stderr:
            self.assertEqual(rewake.main(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_attention_snapshot_promotes_from_current_completed_supervisor_row(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass,"
            "note=completed-supervisor,launch_outcome=never-launched\n",
            encoding="utf-8",
        )
        state, message = rewake.classified_receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertEqual(state, "success")
        self.assertIn("state=success", message)
        self.assertIn("reason=row-advanced", message)
        self.assertNotIn("requires attention", message)

    def test_unsealed_pass_row_stays_attention(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass\n",
            encoding="utf-8",
        )
        state, message = rewake.classified_receipt(
            launch, "ready", "terminal-quiescent", self.root
        )
        self.assertEqual(state, "attention")
        self.assertIn("reason=terminal-failure-or-unclosed", message)
        self.assertIn("required_action=inspect-done-failure", message)
        self.assertIn(f"--jobs {self.jobs}", message)

    def test_success_is_structured_notification_and_attention_is_warning(self) -> None:
        success_stdout = io.StringIO()
        success_stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", success_stdout), mock.patch.object(
            sys, "stderr", success_stderr
        ):
            success_rc = rewake.emit_receipt("success", "completed")
        self.assertEqual(success_rc, 0)
        self.assertEqual(success_stderr.getvalue(), "")
        rendered = json.loads(success_stdout.getvalue())
        self.assertEqual(rendered["systemMessage"], "completed")
        self.assertIn("Hearting dispatch completed", rendered["terminalSequence"])

        attention_stdout = io.StringIO()
        attention_stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", attention_stdout), mock.patch.object(
            sys, "stderr", attention_stderr
        ):
            attention_rc = rewake.emit_receipt("attention", "warning")
        self.assertEqual(attention_rc, 2)
        self.assertEqual(attention_stdout.getvalue(), "")
        self.assertEqual(attention_stderr.getvalue(), "warning\n")

    def test_promoted_success_renders_exit_zero_notification_and_attention_exits_two(
        self,
    ) -> None:
        # SD-97 end to end: an earlier `attention/terminal-failure-or-unclosed`
        # snapshot plus the current sealed `done/completed-supervisor/pass` row must
        # reach the terminal as one exit-0 structured notification. Only a row that
        # is genuinely unresolved may reach it as an exit-2 warning, and that warning
        # must name this exact registry.
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass,"
            "note=completed-supervisor,launch_outcome=never-launched\n",
            encoding="utf-8",
        )
        state, message = rewake.classified_receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertEqual(state, "success")
        self.assertIn("state=success", message)
        self.assertIn("reason=row-advanced", message)
        self.assertIn("required_action=advance-completed", message)
        self.assertIn("Hearting dispatch completed", message)
        self.assertNotIn("harvest --jobs", message)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            sys, "stderr", stderr
        ):
            self.assertEqual(rewake.emit_receipt(state, message), 0)
        self.assertEqual(stderr.getvalue(), "")
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered["systemMessage"], message)
        self.assertIn("Hearting dispatch completed", rendered["terminalSequence"])

        self.jobs.write_text(
            "2026-08-06T00:00:00Z\topen\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1\n",
            encoding="utf-8",
        )
        state, message = rewake.classified_receipt(
            launch, "attention", "terminal-failure-or-unclosed", self.root
        )
        self.assertEqual(state, "attention")
        self.assertIn("Hearting dispatch requires attention", message)
        self.assertIn("required_action=complete-open", message)
        self.assertIn(f"--jobs {self.jobs} ", message)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
            sys, "stderr", stderr
        ):
            self.assertEqual(rewake.emit_receipt(state, message), 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), message + "\n")

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
        self.assertIn(f"--jobs {self.jobs}", message)
        self.assertIn("--attempt-id att-owner-1", message)
        self.assertIn("--status done --failure-detail", message)

    def test_complete_open_receipt_names_the_exact_registry(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\topen\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1\n",
            encoding="utf-8",
        )
        message = rewake.receipt(launch, "attention", "terminal-quiescent", self.root)
        self.assertIn("required_action=complete-open", message)
        # A guard-rejected harvest command an owner cannot act on (SD-97) is the
        # incident this reproduces: the instruction must name this exact
        # registry rather than let dispatch-harvest.py fall back to its default.
        self.assertIn(f"--jobs {self.jobs}", message)
        self.assertIn("--attempt-id att-owner-1", message)
        self.assertIn("--status open --mark-done", message)

    def test_receipt_prefers_the_sealed_launch_home_over_a_mutable_root(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        sealed_home = self.root / "sealed-release"
        (sealed_home / "adapters" / "codex" / "bin").mkdir(parents=True)
        (sealed_home / "adapters" / "codex" / "bin" / "preflight.sh").write_text("#!/bin/sh\n")
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\topen\t/repo\t/wt\towner\t"
            f"attempt_schema_version=2,attempt_id=att-owner-1,launch_home={sealed_home}\n",
            encoding="utf-8",
        )
        mutable_root = self.root / "mutable-checkout"
        mutable_root.mkdir()
        message = rewake.receipt(launch, "attention", "terminal-quiescent", mutable_root)
        self.assertIn(
            str(sealed_home / "adapters" / "codex" / "bin" / "preflight.sh"),
            message,
        )
        self.assertNotIn(str(mutable_root), message)

    def test_receipt_falls_back_to_root_when_launch_home_is_absent(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\topen\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1\n",
            encoding="utf-8",
        )
        message = rewake.receipt(launch, "attention", "terminal-quiescent", self.root)
        self.assertIn(
            str(self.root / "adapters" / "codex" / "bin" / "preflight.sh"),
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
        self.assertIn("reason=terminal-failure-or-unclosed", message)
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
        ), mock.patch.object(rewake.sys, "stdout", io.StringIO()):
            self.assertEqual(rewake.main(), 0)
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
        ) as wait, mock.patch.object(rewake.sys, "stdout", io.StringIO()) as stdout:
            self.assertEqual(rewake.main(), 0)
            message = json.loads(stdout.getvalue())["systemMessage"]
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

    def test_missing_all_sealed_registry_sources_does_not_reconstruct_agent_home(self) -> None:
        payload = self.payload(stdout="check=ok")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(rewake.registry_launch(payload))

    def test_a_start_free_dispatch_owner_command_never_arms(self) -> None:
        payload = self.payload()
        payload["tool_input"]["command"] = "python3 utilities/dispatch-owner.py --status"
        self.assertIsNone(rewake.registry_launch(payload))

    def test_quick_dispatch_node_start_arms_but_dry_run_does_not(self) -> None:
        payload = self.payload(stdout="check=ok")
        payload["tool_input"]["command"] = (
            "python3 utilities/dispatch-node.py --route route.json --node one-shot "
            "--action=start --slug quick --adapter claude | tail -5"
        )
        launch = rewake.registry_launch(payload)
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")

        payload["tool_input"]["command"] = payload["tool_input"]["command"].replace(
            "--action=start", "--action=dry-run"
        )
        self.assertIsNone(rewake.registry_launch(payload))

    def test_space_delimited_registry_metadata_is_tolerated(self) -> None:
        self.jobs.write_text(self.row().replace(",", " "), encoding="utf-8")
        launch = rewake.registry_launch(self.payload())
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")


if __name__ == "__main__":
    unittest.main()


class RegistryCanonicalJobsFallbackTest(RegistryConfirmArmTest):
    """An unexpanded `--jobs "$J"` plus no AGENT_DISPATCH_JOBS still arms from the
    canonical registry the wrapper actually wrote (2026-08-26 incident)."""

    def setUp(self) -> None:
        super().setUp()
        self.environment.stop()
        self.no_env = mock.patch.dict(os.environ, {}, clear=False)
        self.no_env.start()
        os.environ.pop("AGENT_DISPATCH_JOBS", None)
        self.addCleanup(self.no_env.stop)
        self.canonical = mock.patch.object(rewake, "_canonical_jobs", return_value=str(self.jobs))
        self.canonical.start()
        self.addCleanup(self.canonical.stop)

    def payload(self, *, stdout: str = "status=start\nattempt_id=att-owner-1", **replacements):
        payload = super().payload(stdout=stdout, **replacements)
        payload["tool_input"] = {
            "command": 'cd $AGENT_HOME && python3 utilities/dispatch-owner.py --start '
                       '--jobs "$J" --slug owner 2>&1 | tee out.txt | grep -E "^(status|attempt_id)="'
        }
        return payload

    def test_unexpanded_jobs_variable_falls_back_to_canonical_registry(self) -> None:
        launch = rewake.registry_launch(self.payload())
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")
        self.assertEqual(launch.jobs, self.jobs)
        self.assertEqual(launch.armed, "registry")

    def test_without_canonical_registry_it_still_never_arms(self) -> None:
        self.canonical.stop()
        with mock.patch.object(rewake, "_canonical_jobs", return_value=None):
            self.assertIsNone(rewake.registry_launch(self.payload()))
        self.canonical.start()


class PreflightLaunchSurfaceTest(unittest.TestCase):
    """`preflight.sh dispatch-owner --start` is a recognized owner-start surface."""

    def test_preflight_wrapper_start_is_recognized(self) -> None:
        self.assertEqual(
            rewake._start_surface(
                '"$AGENT_HOME/adapters/codex/bin/preflight.sh" dispatch-owner '
                "--start --slug s --jobs /tmp/j.log"
            ),
            "dispatch-owner",
        )
        self.assertEqual(
            rewake._start_surface("preflight.sh dispatch-node --action start --node plan"),
            "dispatch-node",
        )

    def test_unrelated_mentions_still_never_arm(self) -> None:
        self.assertIsNone(rewake._start_surface("grep dispatch-owner --start jobs.log"))
        self.assertIsNone(rewake._start_surface("cat preflight.sh dispatch-owner"))
