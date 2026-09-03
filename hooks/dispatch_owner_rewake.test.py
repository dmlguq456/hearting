#!/usr/bin/env python3
"""Tests for the one-shot Claude interactive owner completion bridge."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
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

_JOIN_PATH = Path(__file__).resolve().parents[1] / "utilities" / "dispatch_completion_join.py"
_JOIN_SPEC = importlib.util.spec_from_file_location("dispatch_completion_join", _JOIN_PATH)
assert _JOIN_SPEC is not None and _JOIN_SPEC.loader is not None
JOIN = importlib.util.module_from_spec(_JOIN_SPEC)
sys.modules[_JOIN_SPEC.name] = JOIN
_JOIN_SPEC.loader.exec_module(JOIN)


def _wait_for_attempt_bridge_states() -> frozenset[str]:
    """Walk the module source for every state literal `wait_for_attempt`
    can hand to `classified_receipt`/`emit_receipt`. Reading the AST instead
    of hardcoding the list means a new bridge state added to the function
    later is picked up by the exhaustive sweep automatically (A47-1)."""

    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    states: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "wait_for_attempt":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Tuple):
                    first = inner.value.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        states.add(first.value)
    return frozenset(states)


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
        # Non-blocking attention is still a terminal receipt: exit 2 wakes
        # the idle session, the receipt is mirrored on stderr.
        self.assertEqual(rc, 2)
        self.assertIn("owned_children=0", stderr.getvalue())
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
        # 2026-08-29: success exits 2 too (only exit 2 wakes an idle session).
        self.assertEqual(success_rc, 2)
        self.assertEqual(success_stderr.getvalue(), "completed\n")
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

    def test_a47_1_exit_code_matches_terminal_state_sweep(self) -> None:
        # A47-1: exhaustively sweep the module's real state vocabulary
        # (TERMINAL_STATES plus every literal `wait_for_attempt` can return,
        # read from source so a new state added later is covered without a
        # test edit) and assert exit_code(state) == 2 for every
        # state in TERMINAL_STATES and == 0 for every state that is not.
        bridge_states = _wait_for_attempt_bridge_states()
        vocabulary = bridge_states | rewake.TERMINAL_STATES
        # Sanity: the vocabulary must actually contain both terminal and
        # non-terminal members, otherwise this sweep would vacuously pass.
        self.assertTrue(vocabulary & rewake.TERMINAL_STATES)
        self.assertTrue(vocabulary - rewake.TERMINAL_STATES)

        for state in sorted(vocabulary):
            message = f"fixture message state={state} owned_children=0"
            if state in rewake.TERMINAL_STATES:
                # Terminal must exit 2 regardless of the caller-supplied
                # block value, and regardless of the default-derived value.
                for block in (None, True, False):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
                        sys, "stderr", stderr
                    ):
                        if block is None:
                            rc = rewake.emit_receipt(state, message)
                        else:
                            rc = rewake.emit_receipt(state, message, block=block)
                    self.assertEqual(
                        rc, 2, f"terminal state {state!r} with block={block!r} must exit 2"
                    )
            else:
                # Non-terminal bridge states (timeout, bridge-error, the
                # unclassified `ready` snapshot) never carry an open owned
                # child (classified_receipt forces owned_children=0 on this
                # path), so `_attention_has_open_child` resolves block=False
                # exactly as `main()` computes it -- the real call shape.
                block = rewake._attention_has_open_child(message)
                self.assertFalse(block)
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "stdout", stdout), mock.patch.object(
                    sys, "stderr", stderr
                ):
                    rc = rewake.emit_receipt(state, message, block=block)
                self.assertEqual(
                    rc, 0, f"non-terminal state {state!r} must exit 0 on the real call path"
                )

    def test_promoted_success_and_attention_both_exit_two_to_wake(
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
            # 2026-08-29: a terminal success must exit 2 -- Claude Code only
            # wakes an idle session for an asyncRewake hook on exit code 2.
            self.assertEqual(rewake.emit_receipt(state, message), 2)
        self.assertIn("Hearting dispatch completed", stderr.getvalue())
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

    def test_sealed_launch_home_harvest_is_admitted_after_release_rotation(self) -> None:
        # SD-115 axis 4 R1 (conditional, plan.md §8): once (a) seals a row's
        # `launch_home` to the resolved OLD release, a rotation that moves
        # `current`/AGENT_HOME/ROOT to a NEW release means the harvest
        # command this hook renders (naming the OLD sealed release's
        # preflight.sh) must still be admitted by the supervisor's own
        # command guard -- `dispatch_completion_join.py`'s
        # `_local_contract_path` -- not just rendered by this hook.
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        sealed_home = self.root / "sealed-old-release"
        (sealed_home / "adapters" / "codex" / "bin").mkdir(parents=True)
        (sealed_home / "adapters" / "codex" / "bin" / "preflight.sh").write_text("#!/bin/sh\n")
        (sealed_home / "core").mkdir()
        (sealed_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\topen\t/repo\t/wt\towner\t"
            f"attempt_schema_version=2,attempt_id=att-owner-1,launch_home={sealed_home}\n",
            encoding="utf-8",
        )
        new_release = self.root / "new-release"
        (new_release / "core").mkdir(parents=True)
        (new_release / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")

        message = rewake.receipt(launch, "attention", "terminal-quiescent", new_release)
        match = re.search(r"checked harvest command: (.+?)\. Do not", message)
        assert match is not None, message
        harvest_line = match.group(1)
        # The command this hook renders does name the sealed OLD release --
        # that half of the pipeline already works (see the sibling
        # `test_receipt_prefers_the_sealed_launch_home_over_a_mutable_root`).
        self.assertIn(str(sealed_home), harvest_line)

        with mock.patch.dict(os.environ, {"AGENT_HOME": str(new_release)}, clear=False), \
             mock.patch.object(JOIN, "ROOT", new_release):
            action = JOIN.classify_supervised_shell_command(
                base=new_release,
                command=harvest_line.strip(),
                open_attempt_ids={"att-owner-1"},
                parent_slug="owner",
                jobs=self.jobs,
            )
        if action is None:
            raise AssertionError(
                "R1 REPRODUCED: _local_contract_path rejects the sealed OLD "
                "release's harvest command once ROOT/AGENT_HOME rotate to a "
                "NEW release -- plan.md §8 R1/§9 decision: "
                "_local_contract_path's roots must be extended to accept a "
                "valid harness root named by the row's own sealed launch_home."
            )
        self.assertEqual(action.attempt_id, "att-owner-1")

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
        # AGENT_HOME must resolve to a real checkout that carries
        # utilities/dispatch-attempt-ready.py -- main() checks
        # `readiness.is_file()` before calling the (here mocked)
        # wait_for_attempt, so an isolated *empty* AGENT_HOME (unlike
        # AGENT_DISPATCH_JOBS, which is a bare path) sends this suite down
        # the readiness-helper-missing bridge-error branch instead of the
        # path each test's `code == 2` assertion expects. This repo
        # checkout is that real, self-contained checkout.
        self.agent_home = MODULE_PATH.parents[1]
        self.environment = mock.patch.dict(
            os.environ,
            {"AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_HOME": str(self.agent_home)},
            clear=False,
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
        slug: str = "slug",
        worktree: str = "/repo",
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
        columns = [self.stamp(age_seconds), status, "/repo", worktree, slug, pipe]
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

    def test_wave_of_starts_narrows_by_the_exact_command_slug(self) -> None:
        # Five concurrent fleet owners (2026-09-01) made every same-session
        # candidate window ambiguous; the observed command's own literal
        # `--slug` names which row this exact start created.
        self.jobs.write_text(
            self.row(attempt_id="att-owner-a", slug="cleanup-a")
            + self.row(attempt_id="att-owner-b", slug="cleanup-b"),
            encoding="utf-8",
        )
        payload = self.payload()
        payload["tool_input"] = {
            "command": (
                "python3 utilities/dispatch-owner.py --start --slug cleanup-b | grep -E 'x'"
            )
        }
        launch = rewake.registry_launch(payload)
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-b")
        self.assertEqual(launch.armed, "registry")

    def test_wave_narrowing_never_matches_an_unexpanded_variable_or_same_slug(self) -> None:
        rows = self.row(attempt_id="att-owner-a", slug="cleanup-a") + self.row(
            attempt_id="att-owner-b", slug="cleanup-a"
        )
        self.jobs.write_text(rows, encoding="utf-8")
        # Same slug on both rows: still ambiguous after narrowing.
        payload = self.payload()
        payload["tool_input"] = {
            "command": "python3 utilities/dispatch-owner.py --start --slug cleanup-a"
        }
        self.assertIsNone(rewake.registry_launch(payload))
        # An unexpanded shell variable is not a literal and narrows nothing.
        payload["tool_input"] = {
            "command": 'python3 utilities/dispatch-owner.py --start --slug "$SLUG"'
        }
        self.assertIsNone(rewake.registry_launch(payload))

    def test_a_lone_candidate_still_arms_regardless_of_the_command_slug(self) -> None:
        # Narrowing is an ambiguity tie-break only: a single exact candidate
        # keeps arming even when the recorded command's slug spells the
        # display slug differently (the pre-2026-09-01 contract, unchanged).
        launch = rewake.registry_launch(self.payload())
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")

    def test_started_receipt_that_arms_nothing_emits_one_typed_notice(self) -> None:
        # Partial grep-filtered stdout: started=1 survives the filter but the
        # dispatch_depth/worker_type fields the stdout fast path needs do
        # not, and two same-session rows keep the registry fallback
        # ambiguous.  Silence here lost four fleet wakes on 2026-09-01.
        self.jobs.write_text(
            self.row(attempt_id="att-owner-a", slug="cleanup-a")
            + self.row(attempt_id="att-owner-b", slug="cleanup-b"),
            encoding="utf-8",
        )
        stdout = "check=ok\nstatus=start\nregistered=1\nstarted=1\nattempt_id=att-owner-b"
        payload = self.payload(stdout=stdout)
        payload["tool_input"] = {
            "command": "python3 utilities/dispatch-owner.py --start | grep -E 'status='"
        }
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("state=not-armed", err.getvalue())
        self.assertIn("attempt_id=att-owner-b", err.getvalue())
        self.assertIn("state=not-armed", json.loads(out.getvalue())["systemMessage"])

    def test_a_failed_start_stays_silent_without_a_notice(self) -> None:
        # No registered row exists (the start failed before registration) and
        # stdout says so itself -- the notice must not shout over it.
        self.jobs.write_text("", encoding="utf-8")
        stdout = "check=failed\nreason=invalid-dispatch-capability-mode"
        payload = self.payload(stdout=stdout)
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_registry_armed_run_reaches_the_unchanged_wait_path(self) -> None:
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), (
            mock.patch.object(
                rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")
            )
        ) as wait, mock.patch.object(rewake.sys, "stdout", io.StringIO()) as stdout, \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()):
            self.assertEqual(rewake.main(), 2)
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


import dispatch_contract as D  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402


class CarrierOneClaimGateTest(unittest.TestCase):
    """SD-111 P3 (C-3): carrier 1 only emits when it wins a claim on an
    already-materialized record, gated first by the incarnation-ancestry
    binding. Never materializes -- DispatchOwnerRewakeMaterializeAbsenceTest
    statically asserts that."""

    ANCESTRY = ("111", "222", "pid:[333]")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        # This suite's env was previously ambient (only exercised correctly
        # because the invoking shell already had a real AGENT_HOME); under
        # tools/run-tests.py isolation AGENT_HOME/HOME are unset. main()
        # checks `(agent_home() / "utilities" / "dispatch-attempt-ready.py"
        # ).is_file()` before the (here mocked) wait_for_attempt, so
        # AGENT_HOME must resolve to a real checkout carrying that file --
        # this repo checkout is that checkout.
        self.agent_home = MODULE_PATH.parents[1]
        self.environment = mock.patch.dict(
            os.environ, {"AGENT_HOME": str(self.agent_home)}, clear=False
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def payload(self):
        output = "\n".join((
            "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
            "parent_completion_delivery=claude-parent-runtime",
            "parent_session_id=session-1", f"job_registry={self.jobs}",
            "attempt_id=att-owner-1", "registered=1", "started=1",
        ))
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-1",
            "tool_input": {
                "command": "python3 utilities/dispatch-owner.py --start --slug owner"
            },
            "tool_response": {"stdout": output, "stderr": ""},
        }

    def _open_row(self, *, ancestry=ANCESTRY, with_ancestry=True):
        pipe = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,attempt_id=att-owner-1,"
            "parent_attempt_id=att-owner-parent,"
            "parent_completion_delivery=claude-parent-runtime,parent_sid=session-1,"
            "route_id=rt-owner,route_node=report,harness=claude"
        )
        if with_ancestry:
            pipe += (
                f",parent_runtime_pid={ancestry[0]}"
                f",parent_runtime_pid_start={ancestry[1]}"
                f",parent_runtime_ns={ancestry[2]}"
            )
        self.jobs.write_text(
            f"2026-08-25T00:00:00Z\topen\t/repo\t/wt\towner\t{pipe}\n", encoding="utf-8"
        )

    def _close_and_materialize(self):
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", "completed-marker"))
        fields = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")
        path = JOIN.materialize_pending_delivery(self.jobs, fields)
        self.assertIsNotNone(path)
        return path

    def _run_main(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake.sys, "stdout", stdout), \
             mock.patch.object(rewake.sys, "stderr", stderr), \
             mock.patch.object(
                 rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")
             ):
            code = rewake.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_claim_win_emits_and_transitions_to_sent_ambiguous(self):
        self._open_row()
        record_path = self._close_and_materialize()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        # Terminal receipt -> exit 2 (the only exit code that wakes an idle
        # Claude session), receipt mirrored on stderr.
        self.assertEqual(code, 2)
        # No completion-marker fixture here, so classification lands on
        # "attention" (required_action=inspect-done-failure), not "success"
        # -- the point of this test is that the notice is emitted at all
        # (byte-identical to the pre-claim-gate `classified_receipt` output)
        # and the record transitions, not which of the two it is.
        self.assertIn("Hearting dispatch requires attention", stdout)
        self.assertIn("systemMessage", stdout)
        self.assertIn("Hearting dispatch requires attention", stderr)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "sent-ambiguous")

    def test_claim_lost_when_already_claimed_is_silent(self):
        self._open_row()
        record_path = self._close_and_materialize()
        record = json.loads(record_path.read_text(encoding="utf-8"))
        root = self.jobs.resolve(strict=False).parent
        rewake.pending_delivery.claim(
            root, "session-1", record["delivery_id"],
            claim_owner="someone-else", lease_seconds=30.0,
        )
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_acked_record_is_silent(self):
        self._open_row()
        record_path = self._close_and_materialize()
        record = json.loads(record_path.read_text(encoding="utf-8"))
        root = self.jobs.resolve(strict=False).parent
        rewake.pending_delivery.claim(
            root, "session-1", record["delivery_id"],
            claim_owner="someone-else", lease_seconds=30.0,
        )
        rewake.pending_delivery.ack(
            root, "session-1", record["delivery_id"], acked_by="codex-managed-gateway",
        )
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_record_not_yet_materialized_crash_window_is_silent(self):
        # Intent stamped (row closed), but neither trigger 1 nor trigger 2
        # has materialized a record yet -- the hook must never materialize
        # it itself (§4.4) and must go silent, not raise.
        self._open_row()
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", "completed-marker"))
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_second_incarnation_mismatch_reads_claims_and_injects_nothing(self):
        # Same session_id, different runtime process (pid, start) than the
        # one recorded at launch -- carrier 1 must not claim or emit.
        self._open_row(ancestry=self.ANCESTRY)
        record_path = self._close_and_materialize()
        other_incarnation = ("999", "888", "pid:[777]")
        with mock.patch.object(
            rewake, "runtime_ancestry_binding", return_value=other_incarnation
        ):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertIsNone(record["claim_owner"])

    def test_missing_ancestry_fields_is_silent(self):
        # A row from before 2-a-5 (or a non-Claude harness) carries no
        # parent_runtime_* fields at all -- fail closed, not "trust it".
        self._open_row(with_ancestry=False)
        record_path = self._close_and_materialize()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "pending")

    def test_still_open_row_is_ungated_by_the_claim_mechanism(self):
        # No terminal edge has happened yet (still open) -- SD-111's claim
        # gate must not silence this, or a live timeout/bridge-error
        # diagnostic could vanish forever with no future trigger to recover
        # it. Regression guard for the claim-gate addition itself.
        self._open_row()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 2)
        self.assertIn("state=attention", stdout)


class DispatchOwnerRewakeMaterializeAbsenceTest(unittest.TestCase):
    def test_hook_module_never_imports_the_materializer(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("materialize_pending_delivery", source)


class A12ArmingFailureFixture(unittest.TestCase):
    """SD-111 A-12 (round 2 C-4 full replacement, plan §7).

    Reproduces the three real arming failures -- stdout truncation, missing
    ``AGENT_DISPATCH_JOBS`` with no other jobs source, and an unrecognized
    launcher string -- then counts *independently*: carrier invocations
    (this hook's own emitted notices), terminal edges (the row closing),
    and pending records. The row closes and materializes exactly like the
    real launcher process would (§2-b-1) -- this fixture calls
    `close_attempt_row`/`materialize_after_terminal_close` directly to model
    that separate process, never through the hook, which never
    materializes (§4.4).
    """

    ATTEMPT = "att-a12-fixture"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        pipe = (
            "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,"
            f"attempt_id={self.ATTEMPT},parent_attempt_id=att-a12-parent,"
            "parent_completion_delivery=claude-parent-runtime,parent_sid=session-a12,"
            "route_id=rt-a12-fixture,route_node=report,harness=claude"
        )
        self.jobs.write_text(
            f"2026-08-25T00:00:00Z\topen\t/repo\t/wt\towner\t{pipe}\n", encoding="utf-8"
        )

    def _run_main_with(self, payload, *, env=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        clean_env = {
            k: v for k, v in os.environ.items() if k != "AGENT_DISPATCH_JOBS"
        }
        clean_env.update(env or {})
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(rewake.sys, "stdout", stdout), \
             mock.patch.object(rewake.sys, "stderr", stderr), \
             mock.patch.object(rewake.os, "environ", clean_env), \
             mock.patch.object(rewake, "_canonical_jobs", return_value=None):
            code = rewake.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def _terminal_edge_and_recover(self):
        """Models the real launcher: close the row, materialize (trigger 1),
        then recover by exact harvest -- entirely independent of the hook."""

        self.assertTrue(D.close_attempt_row(self.jobs, self.ATTEMPT, "completed-marker"))
        record_path = JOIN.materialize_after_terminal_close(self.jobs, self.ATTEMPT)
        self.assertIsNotNone(record_path)
        records = list((self.root / "pending-delivery").glob("*/*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        claimed = rewake.pending_delivery.claim(
            self.root, "session-a12", record["delivery_id"],
            claim_owner="exact-harvest", lease_seconds=30.0,
        )
        self.assertEqual(claimed["state"], "claimed")
        acked = rewake.pending_delivery.ack(
            self.root, "session-a12", record["delivery_id"], acked_by="exact-harvest",
        )
        self.assertEqual(acked["state"], "acked")

    def test_condition_1_stdout_truncation_zero_carrier_one_terminal_edge_one_record(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-a12",
            "tool_input": {
                "command": "python3 utilities/dispatch-owner.py --start --slug owner"
            },
            # Piped through `| tail` or `> /dev/null` in practice -- the
            # receipt fast path (parse_launch) never even sees a candidate.
            "tool_response": {"stdout": "", "stderr": ""},
        }
        code, stdout, stderr = self._run_main_with(payload)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self._terminal_edge_and_recover()

    def test_condition_2_no_jobs_source_one_typed_notice_one_terminal_edge_one_record(self):
        # stdout is present but the one field registry_launch would need
        # (job_registry) is missing -- and no other jobs source (env,
        # --jobs, canonical) resolves either.  A-12's sealed outcome (writer
        # invocation 1, pending record 1, loss 0) is unchanged; since
        # 2026-09-01 the *arming* loss itself is additionally loud: a start
        # that reported started=1 but armed neither path emits one typed
        # not-armed notice instead of silence (five fleet owners launched
        # with grep-filtered stdout lost their wakes silently).
        output = "\n".join((
            "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
            "parent_completion_delivery=claude-parent-runtime",
            "parent_session_id=session-a12", f"attempt_id={self.ATTEMPT}",
            "registered=1", "started=1",
        ))
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-a12",
            "tool_input": {
                "command": "python3 utilities/dispatch-owner.py --start --slug owner"
            },
            "tool_response": {"stdout": output, "stderr": ""},
        }
        code, stdout, stderr = self._run_main_with(payload)
        self.assertEqual(code, 2)
        self.assertIn("state=not-armed", stderr)
        self.assertIn(f"attempt_id={self.ATTEMPT}", stderr)
        self.assertIn("state=not-armed", json.loads(stdout)["systemMessage"])
        self._terminal_edge_and_recover()

    def test_condition_3_unrecognized_launcher_zero_carrier_one_terminal_edge_one_record(self):
        output = "\n".join((
            "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
            "parent_completion_delivery=claude-parent-runtime",
            "parent_session_id=session-a12", f"job_registry={self.jobs}",
            f"attempt_id={self.ATTEMPT}", "registered=1", "started=1",
        ))
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "session_id": "session-a12",
            "tool_input": {"command": "bash -c 'echo hi'"},
            "tool_response": {"stdout": output, "stderr": ""},
        }
        code, stdout, stderr = self._run_main_with(payload)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self._terminal_edge_and_recover()


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
