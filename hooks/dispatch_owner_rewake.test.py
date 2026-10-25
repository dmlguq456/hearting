#!/usr/bin/env python3
"""Tests for the one-shot Claude interactive owner completion bridge."""

from __future__ import annotations

import ast
import contextlib
from dataclasses import replace
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
import inspect
import time
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).with_name("dispatch-owner-rewake.py")
ROOT = MODULE_PATH.parents[1]
# A holder that is provably dead *from this process's own PID namespace*:
# pid 4194304 is above Linux's pid_max, so `/proc/<pid>` never exists, and
# the namespace is ours so the hook may judge it (a holder recorded from
# another namespace is unobservable and counts as alive -- top review M3).
DEAD_HOLDER = ["4194304", "0", os.readlink(f"/proc/{os.getpid()}/ns/pid")]
SPEC = importlib.util.spec_from_file_location("dispatch_owner_rewake", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
rewake = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rewake
SPEC.loader.exec_module(rewake)
FIXTURE_PROCESSES_SPEC = importlib.util.spec_from_file_location(
    "dispatch_fixture_processes", ROOT / "tools/fixture_processes.py"
)
assert FIXTURE_PROCESSES_SPEC is not None and FIXTURE_PROCESSES_SPEC.loader is not None
fixture_processes = importlib.util.module_from_spec(FIXTURE_PROCESSES_SPEC)
FIXTURE_PROCESSES_SPEC.loader.exec_module(fixture_processes)

_JOIN_PATH = Path(__file__).resolve().parents[1] / "utilities" / "dispatch_completion_join.py"
_JOIN_SPEC = importlib.util.spec_from_file_location("dispatch_completion_join", _JOIN_PATH)
assert _JOIN_SPEC is not None and _JOIN_SPEC.loader is not None
JOIN = importlib.util.module_from_spec(_JOIN_SPEC)
sys.modules[_JOIN_SPEC.name] = JOIN
_JOIN_SPEC.loader.exec_module(JOIN)


def _gate_authority(jobs, recipient, delivery_id):
    """An outstanding receipt alone is not proof that a human gate exists."""
    from route_identity import route_hash
    from workflow_state import WorkflowLedger
    from dispatch_contract import parse_registry_metadata
    record = rewake.pending_delivery.read(jobs.parent, recipient, delivery_id)
    child = record["receipt"]["children"][0]
    Path(child["reason"]).write_text("{}")
    aid = child["attempt_id"]
    route = {"route_id": record["route_id"], "nodes": [{"id": record["route_node"],
        "continuation": {"kind": "human-gate", "gate": "frame-review"}}]}
    route["route_hash"] = route_hash(route)
    path = jobs.parent / (route["route_id"] + ".json")
    path.write_text(json.dumps(route))
    lines = jobs.read_text().splitlines()
    found = False
    for index, line in enumerate(lines):
        fields = line.split("\t")
        meta = parse_registry_metadata(fields[5])
        if meta.get("attempt_id") != aid:
            continue
        found = True
        meta.update(route_file=str(path), route_id=route["route_id"], route_hash=route["route_hash"])
        fields[5] = ",".join(k+"="+v for k,v in meta.items())
        lines[index] = "\t".join(fields)
    if not found:
        lines.append("now\tdone\t/r\t/w\tgate\t" + ",".join(f"{k}={v}" for k,v in dict(
            attempt_id=aid, parent_sid=recipient, route_file=str(path), route_id=route["route_id"],
            route_hash=route["route_hash"]).items()))
    jobs.write_text("\n".join(lines) + "\n")
    journal = WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs).journal_path
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"workflow_state": "BLOCKED_HUMAN_GATE", "evidence": {
        "gate": "frame-review", "artifact": child["reason"], "delivery": str(
            rewake.pending_delivery.record_path(jobs.parent, recipient, delivery_id))}}) + "\n")


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


class ReplacementCarrierTest(unittest.TestCase):
    def test_authority_requires_native_binding_and_current_arm_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory) / "jobs.log"
            jobs.write_text("")
            claim = rewake.ArmClaim(Path(directory) / "arm.json", "att-old", "sid", 1, ("7", "8", "ns"))
            launch = rewake.Launch("att-old", jobs, "sid")
            held = {"holder": list(claim.holder), "state": "waiting", "attempt_id": "att-old", "session_id": "sid"}
            metadata = {**rewake.REGISTRY_DEPTH1_START, "parent_sid": "sid", "worker_type": "owner"}
            with mock.patch.object(rewake, "_trusted_jobs", return_value=jobs), \
                 mock.patch.object(rewake, "_read_arm", return_value=held), \
                 mock.patch.object(rewake, "_process_identity", return_value=claim.holder), \
                 mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True) as native:
                self.assertTrue(rewake.replacement_authority(launch, claim, jobs, "att-old", metadata))
                self.assertFalse(rewake.replacement_authority(launch, claim, jobs, "att-old", dict(metadata, parent_sid="foreign")))
                held["state"] = "ended"
                self.assertFalse(rewake.replacement_authority(launch, claim, jobs, "att-old", metadata))
                held["state"] = "waiting"
                native.return_value = False
                self.assertFalse(rewake.replacement_authority(launch, claim, jobs, "att-old", metadata))

    def test_restarted_carrier_follows_same_registered_successor(self):
        import dispatch_replacement as replacement
        jobs = Path("/tmp/jobs.log")
        source = rewake.Launch("att-old", jobs, "sid")
        claim = rewake.ArmClaim(Path("/tmp/old.json"), "att-old", "sid", 2, ("7", "8", "ns"))
        target = rewake.ArmClaim(Path("/tmp/new.json"), "att-new", "sid", 2, claim.holder)
        edge = {"original_attempt_id": "att-old", "replacement_attempt_id": "att-new"}
        from types import SimpleNamespace
        with mock.patch.object(replacement, "advance_batch", return_value=({"att-new"}, [edge], [])), \
             mock.patch.object(rewake, "current_attempt_row", return_value=SimpleNamespace(status="open", metadata={"launch_claimed": "1"})), \
             mock.patch.object(rewake, "claim_arm", return_value=target):
            launch, held = rewake.follow_replacement(source, claim)
            self.assertEqual(launch.attempt_id, "att-new")
            self.assertEqual(held.predecessors, (claim,))


    def test_lost_reply_register_only_retains_original_authority_until_spawn(self):
        from types import SimpleNamespace
        import dispatch_replacement as replacement
        jobs = Path("/tmp/jobs.log")
        source = rewake.Launch("att-old", jobs, "sid")
        claim = rewake.ArmClaim(Path("/tmp/old.json"), "att-old", "sid", 2, ("7", "8", "ns"))
        target = rewake.ArmClaim(Path("/tmp/new.json"), "att-new", "sid", 1, claim.holder)
        edge = {"original_attempt_id": "att-old", "replacement_attempt_id": "att-new"}
        row = SimpleNamespace(status="open", metadata={"launch_claimed": "0"})
        with mock.patch.object(replacement, "advance_batch", return_value=({"att-new"}, [edge], [])) as advance, \
             mock.patch.object(rewake, "current_attempt_row", return_value=row), \
             mock.patch.object(rewake, "claim_arm", return_value=target) as arm:
            self.assertIsInstance(rewake.follow_replacement(source, claim), rewake.ReplacementPending)
            arm.assert_not_called()
            row.metadata["launch_claimed"] = "1"
            launch, held = rewake.follow_replacement(source, claim)
            self.assertEqual(launch.attempt_id, "att-new")
            self.assertEqual(held.predecessors, (claim,))
            self.assertEqual([call.args[1] for call in advance.call_args_list], [{"att-old"}, {"att-old"}])


class DispatchOwnerRewakeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text(self.row(), encoding="utf-8")
        # `main()` checks `agent_home()/utilities/dispatch-attempt-ready.py`
        # before it ever waits: without a home that has the helper it takes the
        # `readiness-helper-missing` lapse path and returns 0, so every wake
        # assertion below silently stopped testing the wake. The runner strips
        # AGENT_HOME on purpose, which is why this must be set here rather than
        # inherited (CI 2026-09-10; the peer classes already do this).
        environment = mock.patch.dict(
            os.environ,
            {"AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_HOME": str(MODULE_PATH.parents[1])},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def row(*, attempt_id="att-owner-1", status="open", parent_sid="session-1", age_seconds=0.0, **overrides):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
        metadata = {"capability": "autopilot-code", "dispatch_depth": "1", "worker_type": "owner",
                    "parent_sid": parent_sid, "parent_completion_delivery": "claude-parent-runtime",
                    "launch_claimed": "1", "launch_started": "1", "attempt_id": attempt_id}
        metadata.update(overrides)
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        return "\t".join([stamp, status, "/repo", "/repo", "slug", pipe]) + "\n"

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

    def test_session_owner_rows_accepts_depth1_review_and_rejects_registry_negatives(self) -> None:
        self.jobs.write_text(self.row(worker_type="review"), encoding="utf-8")
        self.assertEqual(
            [attempt_id for attempt_id, _age in rewake._session_owner_rows(self.jobs, "session-1")],
            ["att-owner-1"],
        )
        for key, value in (
            ("dispatch_depth", "2"),
            ("parent_completion_delivery", "codex-stop-hook"),
            ("launch_claimed", "0"),
            ("launch_started", "0"),
        ):
            with self.subTest(key=key):
                self.jobs.write_text(self.row(worker_type="review", **{key: value}), encoding="utf-8")
                self.assertEqual(rewake._session_owner_rows(self.jobs, "session-1"), [])

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
        incomplete = self.payload()
        incomplete["tool_response"]["stdout"] = incomplete["tool_response"]["stdout"].replace(
            "started=1", "started=0"
        )
        self.assertIsNone(rewake.parse_launch(incomplete))
        unrelated_tool = self.payload(tool_name="Read")
        self.assertIsNone(rewake.parse_launch(unrelated_tool))

    def test_the_receipt_decides_and_the_command_text_is_never_read(self) -> None:
        # 2026-09-09: the hook identifies the owner from what the launch
        # wrote (receipt + registry), never from the Bash command string --
        # six reviews in a row had found holes in the shell parsing. Any
        # command whose stdout carries the exact same-session start receipt
        # arms, prefixes and all; `tool_input.command` is not even consulted.
        for command in (
            "git status",
            "echo utilities/dispatch-node.py --action start",
            "time nohup python3 -u utilities/dispatch-owner.py --start | tail -3",
            "bash -c 'python3 utilities/dispatch-owner.py --start'",
        ):
            with self.subTest(command=command):
                launch = rewake.parse_launch(self.payload(tool_input={"command": command}))
                assert launch is not None
                self.assertEqual((launch.attempt_id, launch.armed), ("att-owner-1", "stdout"))
        payload = self.payload()
        payload["tool_input"] = {}
        self.assertEqual(rewake.parse_launch(payload).attempt_id, "att-owner-1")

    def test_a_receipt_names_a_candidate_and_only_the_registry_row_proves_it(self) -> None:
        # Review R1 B1: with the command-surface check gone, any Bash output
        # shaped like a start receipt would otherwise arm an invented, foreign,
        # depth-2, unstarted, or stale attempt. The row is the identity.
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.assertIsNotNone(rewake.receipt_row_age(launch))
        for label, rows in (
            ("no row", ""),
            ("foreign session", self.row(parent_sid="session-2")),
            ("depth-2", self.row(dispatch_depth="2")),
            ("not started", self.row(launch_started="0")),
            ("not an owner", self.row(worker_type="stage")),
            ("other attempt", self.row(attempt_id="att-owner-9")),
        ):
            with self.subTest(label=label):
                self.jobs.write_text(rows, encoding="utf-8")
                self.assertIsNone(rewake.receipt_row_age(launch))
                with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
                     mock.patch.object(rewake.sys, "stdout", io.StringIO()) as out, \
                     mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
                    self.assertEqual(rewake.main(), 2)
                self.assertIn("reason=row-identity-mismatch", err.getvalue())
                self.assertFalse(rewake.arm_path(self.jobs, "att-owner-1").exists())
        # a short owner that already ran to done is still a proven identity
        self.jobs.write_text(self.row(status="done"), encoding="utf-8")
        self.assertIsNotNone(rewake.receipt_row_age(launch))
        # but not when its row is older than the arm window (an echoed old receipt)
        self.jobs.write_text(self.row(status="done", age_seconds=4_000), encoding="utf-8")
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("reason=not-fresh", err.getvalue())

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

    def test_canonical_wait_consumes_runner_projection_without_helper_process(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        store = mock.Mock()
        store.get.return_value = {"id": "readiness-request"}
        source_root = MODULE_PATH.parents[1]
        with mock.patch.object(rewake, "agent_home", return_value=source_root), \
                mock.patch.object(rewake.peer_obligations, "ObligationStore", return_value=store), \
                mock.patch.object(rewake.peer_obligations, "ensure_normal_runner", return_value=True), \
                mock.patch.object(rewake.shared_readiness, "current_result",
                                  return_value={"state": "ready", "children": []}) as result, \
                mock.patch.object(rewake.subprocess, "run", side_effect=AssertionError("helper spawned")):
            outcome = rewake.wait_for_attempt(
                launch, source_root / "utilities" / "dispatch-attempt-ready.py")
        self.assertEqual(outcome, ("ready", "terminal-quiescent"))
        result.assert_called_once()
        store.create.assert_called_once()
        self.assertEqual(store.update.call_args.kwargs["state"], "complete")

    def test_probe_timeout_keeps_the_exact_owner_wait_armed(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        for code, expected in ((0, ("ready", "terminal-quiescent")),
                               (3, ("attention", "terminal-failure-or-unclosed"))):
            with self.subTest(code=code), mock.patch.object(
                    rewake.subprocess, "run", side_effect=[
                        subprocess.TimeoutExpired("readiness", 30),
                        subprocess.CompletedProcess([], code)]
                ) as run, mock.patch.object(rewake.time, "sleep") as sleep, \
                    mock.patch.object(rewake.time, "monotonic", side_effect=[0.0, 31.0]), \
                    mock.patch.dict(os.environ, {"AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "3",
                                                 "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "60"}):
                self.assertEqual(rewake.wait_for_attempt(launch, self.root / "ready.py"), expected)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0].args, run.call_args_list[1].args)
                sleep.assert_called_once_with(3)

    def test_probe_timeout_preserves_the_original_wait_deadline(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        with mock.patch.object(rewake.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("readiness", 30)) as run, \
                mock.patch.object(rewake.time, "sleep") as sleep, \
                mock.patch.object(rewake.time, "monotonic", return_value=60.0):
            self.assertEqual(rewake.wait_for_attempt(launch, self.root / "ready.py", deadline=60.0),
                             ("bridge-error", "TimeoutExpired"))
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

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
        self.assertIn("--status done", message)
        self.assertNotIn("--failure-detail", message)

    def parked_owner_receipt(self, status: str) -> str:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=blocked,"
            "note=dead-worker-blocked,worker_type=owner,route_id=rt-owner,route_hash=sha256:owner,"
            "launch_outcome=never-launched\n",
            encoding="utf-8",
        )
        parked = {"gate": "full-run-authorization", "status": status, "epoch": 1,
                  "artifact": "/tmp/gate.md", "route_file": "/tmp/rt-owner.json",
                  "route_id": "rt-owner", "route_hash": "sha256:owner", "gated_nodes": ["full-run"]}
        with mock.patch("dispatch_replacement.owner_parked_gate", return_value=parked):
            return rewake.receipt(launch, "ready", "terminal-quiescent", self.root)

    def test_parked_owner_wake_names_the_gate_not_a_failure(self) -> None:
        message = self.parked_owner_receipt("blocked")
        self.assertIn("required_action=human-gate:full-run-authorization", message)
        self.assertIn("reason=owner-parked-at-human-gate", message)
        self.assertIn("this is not a failure", message)
        self.assertIn("workflow-supervisor.py release --route /tmp/rt-owner.json", message)
        self.assertIn("--gate full-run-authorization", message)
        self.assertIn("--decision proceed", message)
        self.assertIn(f"--jobs {self.jobs}", message)
        self.assertNotIn("inspect-done-failure", message)
        self.assertIn("state=attention", message)
        from frame_interview import PENDING_ANSWER_RULE
        self.assertIn(PENDING_ANSWER_RULE, message)

    def test_released_parked_owner_wake_gives_the_start_handle(self) -> None:
        message = self.parked_owner_receipt("proceed")
        self.assertIn("reason=owner-parked-gate-released", message)
        self.assertIn("capability-route.py start --route /tmp/rt-owner.json", message)
        self.assertIn(f"--jobs {self.jobs}", message)
        self.assertNotIn("--status done", message)  # the start handle replaces the harvest command

    def test_unparked_failure_keeps_the_harvest_receipt(self) -> None:
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=blocked,"
            "note=dead-worker-blocked,worker_type=owner,launch_outcome=never-launched\n",
            encoding="utf-8",
        )
        with mock.patch("dispatch_replacement.owner_parked_gate", return_value=None):
            message = rewake.receipt(launch, "ready", "terminal-quiescent", self.root)
        self.assertIn("required_action=inspect-done-failure", message)
        self.assertNotIn("owner-parked", message)

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
        marker = self.root / "report.json"
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
            "note=completed-supervisor,worker_type=owner,dispatch_depth=1,"
            "launch_outcome=never-launched\n",
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
        # reach the terminal as one exit-2 structured notification. A row that
        # is genuinely unresolved reaches it as an exit-2 warning, and that warning
        # must name this exact registry.
        launch = rewake.parse_launch(self.payload())
        assert launch is not None
        self.jobs.write_text(
            "2026-08-06T00:00:00Z\tdone\t/repo\t/wt\towner\t"
            "attempt_schema_version=2,attempt_id=att-owner-1,failure_class=pass,"
            "note=completed-supervisor,worker_type=owner,dispatch_depth=1,"
            "launch_outcome=never-launched\n",
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
        self.assertIn("--status done", message)
        self.assertNotIn("--failure-detail", message)

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
        commands = JOIN.harvest_command_lines(message)
        self.assertEqual(len(commands), 1, message)
        harvest_line = commands[0]
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
            self.assertEqual(rewake.main(), 2)  # a real row behind the receipt: terminal wake
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

    def arm(self, payload=None):
        resolved = rewake.registry_launch(payload or self.payload())
        if resolved is None or isinstance(resolved, rewake.ArmRefusal):
            return None
        launch, claim = resolved
        self.assertEqual(claim.attempt_id, launch.attempt_id)
        self.assertTrue(claim.path.is_file())
        return launch

    def ledger(self, attempt_id: str) -> dict:
        return json.loads(rewake.arm_path(self.jobs, attempt_id).read_text(encoding="utf-8"))

    def holder_exited(self, attempt_id: str) -> None:
        """The real hook process exits right after settling; this test process
        lives on, so mark the ledger holder dead the way exit would."""
        path = rewake.arm_path(self.jobs, attempt_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["holder"] = DEAD_HOLDER
        path.write_text(json.dumps(record), encoding="utf-8")

    def test_filtered_stdout_arms_from_the_single_matching_row(self) -> None:
        launch = self.arm()
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")
        self.assertEqual(launch.jobs, self.jobs)
        self.assertEqual(launch.session_id, "session-1")
        self.assertEqual(launch.armed, "registry")
        record = self.ledger("att-owner-1")
        self.assertEqual((record["state"], record["arms"], record["session_id"]), ("waiting", 1, "session-1"))
        self.assertEqual(record["holder"][0], str(os.getpid()))

    def test_codex_and_opencode_owner_attempts_arm_the_original_claude_parent(self) -> None:
        for harness in ("codex", "opencode"):
            attempt_id = f"att-{harness}-owner"
            with self.subTest(harness=harness):
                self.jobs.write_text(self.row(attempt_id=attempt_id, harness=harness,
                    parent_sid="session-1", parent_completion_delivery="claude-parent-runtime"),
                    encoding="utf-8")
                launch = self.arm()
                assert launch is not None
                self.assertEqual((launch.attempt_id, launch.session_id), (attempt_id, "session-1"))
                ledger = self.ledger(attempt_id)
                self.assertEqual((ledger["state"], ledger["session_id"]), ("waiting", "session-1"))

    def test_a_wave_of_starts_is_armed_one_row_per_call_oldest_first(self) -> None:
        # Five concurrent fleet owners (2026-09-01) made every same-session
        # candidate window ambiguous for the old command-slug narrowing. The
        # ledger makes the order irrelevant: each Bash call's hook takes one
        # unclaimed row, so N starts in N calls arm N waiters, and an N+1th
        # call finds nothing left to take.
        self.jobs.write_text(
            self.row(attempt_id="att-owner-a", slug="cleanup-a", age_seconds=20)
            + self.row(attempt_id="att-owner-b", slug="cleanup-b", age_seconds=10),
            encoding="utf-8",
        )
        first = self.arm(self.payload(tool_input={"command": "python3 dispatch-owner.py --start --slug cleanup-b | grep x"}))
        second = self.arm(self.payload(tool_input={"command": "ls"}))
        self.assertEqual([first.attempt_id, second.attempt_id], ["att-owner-a", "att-owner-b"])
        self.assertIsNone(self.arm())

    def test_a_second_claim_for_a_live_holder_is_refused(self) -> None:
        # Two parallel tool calls (or one call's stdout path racing a later
        # call's registry path) converge on one waiter per attempt.
        self.assertIsInstance(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True), rewake.ArmClaim)
        refusal = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        self.assertEqual((refusal.reason, refusal.watched), ("held-live", True))
        self.assertIsNone(self.arm())

    def test_eight_processes_racing_for_one_attempt_produce_one_claim(self) -> None:
        # The flock is what makes arming exactly-once under parallel tool
        # calls; a sequential second claim cannot prove that (review R1 minor).
        # Every racer must still be *alive* while the others try, or this
        # asserts something the contract does not promise: a claim whose holder
        # has exited is legitimately re-takable, so short-lived children give
        # two honest winners whenever the first finishes before the last
        # starts. That is what made this case flaky under load rather than a
        # real mutual-exclusion failure (CI 2026-09-10: `['held-live', 'won',
        # ..., 'won', 'held-live']`). Each child reports its verdict and then
        # waits for the parent's release file, so all eight attempts land
        # inside the winner's lifetime; the child's own deadline keeps a lost
        # release from hanging the suite. A real file, not `-c`: the wait needs
        # a `while` statement, which a semicolon-joined one-liner cannot hold.
        release = self.root / "race-release"
        racer = self.root / "racer.py"
        racer.write_text(
            "import importlib.util, sys, pathlib, time\n"
            f"spec = importlib.util.spec_from_file_location('h', {str(MODULE_PATH)!r})\n"
            "m = importlib.util.module_from_spec(spec)\n"
            "sys.modules['h'] = m\n"
            "spec.loader.exec_module(m)\n"
            f"r = m.claim_arm(pathlib.Path({str(self.jobs)!r}), 'att-owner-1', 'session-1', fresh=True)\n"
            "print('won' if isinstance(r, m.ArmClaim) else r.reason, flush=True)\n"
            f"gate = pathlib.Path({str(release)!r})\n"
            "deadline = time.monotonic() + 60\n"
            "while not gate.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n",
            encoding="utf-8",
        )
        procs = [subprocess.Popen([sys.executable, str(racer)], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) for _ in range(8)]
        try:
            results = [(proc.stdout.readline() or "").strip() for proc in procs]
        finally:
            release.write_text("go", encoding="utf-8")
            for proc in procs:
                proc.communicate()
        self.assertEqual(results.count("won"), 1, results)
        self.assertEqual(set(results) - {"won"}, {"held-live"}, results)

    def test_a_dead_holder_is_reclaimed_but_an_alive_one_is_not(self) -> None:
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sleeper.kill)
        path = rewake.arm_path(self.jobs, "att-owner-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        alive = list(rewake._process_identity(sleeper.pid))
        record = {"schema": 1, "attempt_id": "att-owner-1", "session_id": "session-1",
                  "holder": alive, "state": "waiting", "arms": 1, "gate_delivery_id": None}
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertIsNone(self.arm())
        sleeper.kill()
        sleeper.wait()
        launch = self.arm()
        assert launch is not None
        self.assertEqual((launch.attempt_id, launch.armed), ("att-owner-1", "registry-rearm"))
        self.assertEqual(self.ledger("att-owner-1")["arms"], 2)

    def test_a_stale_row_is_refused_first_time_but_a_lapsed_claim_is_rearmed_regardless_of_age(self) -> None:
        self.jobs.write_text(self.row(age_seconds=4_000), encoding="utf-8")
        self.assertIsNone(self.arm())
        path = rewake.arm_path(self.jobs, "att-owner-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "attempt_id": "att-owner-1", "session_id": "session-1",
                                    "holder": DEAD_HOLDER, "state": "lapsed", "arms": 3,
                                    "gate_delivery_id": None}), encoding="utf-8")
        launch = self.arm()
        assert launch is not None
        self.assertEqual(launch.armed, "registry-rearm")
        self.assertEqual(self.ledger("att-owner-1")["arms"], 4)

    def test_ended_exhausted_foreign_or_unreadable_claims_never_rearm(self) -> None:
        path = rewake.arm_path(self.jobs, "att-owner-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        base = {"schema": 1, "attempt_id": "att-owner-1", "session_id": "session-1",
                "holder": DEAD_HOLDER, "state": "lapsed", "arms": 1, "gate_delivery_id": None}
        for label, record, reason in (
            ("ended", {**base, "state": "ended"}, "ended"),
            ("exhausted", {**base, "arms": rewake.ARM_LIMIT}, "exhausted"),
            ("foreign-session", {**base, "session_id": "session-2"}, "foreign-session"),
            ("unknown-state", {**base, "state": "armed"}, "unreadable"),
            ("wrong-schema", {**base, "schema": 99}, "unreadable"),
        ):
            with self.subTest(label=label):
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertIsNone(self.arm())
                self.assertEqual(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True).reason, reason)
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.arm())
        self.assertEqual(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True).reason, "unreadable")

    def test_a_live_pid_whose_identity_cannot_be_read_counts_as_alive(self) -> None:
        # Review R1 M1: a transient /proc read failure is not a death.
        claim = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        assert isinstance(claim, rewake.ArmClaim)
        with mock.patch.object(rewake, "_process_identity", return_value=None):
            self.assertTrue(rewake._holder_alive(list(claim.holder)))
        self.assertFalse(rewake._holder_alive(DEAD_HOLDER))                  # pid does not exist
        self.assertFalse(rewake._holder_alive([str(os.getpid()), "0", DEAD_HOLDER[2]]))  # live pid, wrong start: reuse
        self.assertTrue(rewake._holder_alive([str(os.getpid()), "0", "pid:[0]"]))       # another namespace: unobservable

    def test_a_holder_from_another_pid_namespace_is_never_declared_dead(self) -> None:
        # Top review M3: a pid is a coordinate in the holder's namespace; from
        # another one the number means nothing, so the holder counts as alive.
        foreign = ["4194304", "0", "pid:[4026531999]"]
        self.assertTrue(rewake._holder_alive(foreign))
        path = rewake.arm_path(self.jobs, "att-owner-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "attempt_id": "att-owner-1", "session_id": "session-1",
                                    "holder": foreign, "state": "waiting", "arms": 1, "gate_delivery_id": None}), encoding="utf-8")
        self.assertEqual(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True).reason, "held-live")

    def test_a_symlink_swapped_in_after_validation_is_not_followed(self) -> None:
        # Top review N1: the registry is read through O_NOFOLLOW, so a link
        # placed at the trusted path between validation and read is refused.
        other = self.root / "other.log"
        other.write_text(self.row(attempt_id="att-replaced"), encoding="utf-8")
        self.assertEqual([a for a, _ in rewake._session_owner_rows(self.jobs, "session-1")], ["att-owner-1"])
        self.jobs.unlink()
        self.jobs.symlink_to(other)
        self.assertIsNone(rewake._read_registry_lines(self.jobs))
        self.assertEqual(rewake._session_owner_rows(self.jobs, "session-1"), [])

    def test_a_proven_start_whose_ledger_is_unreadable_says_so(self) -> None:
        # Review R1 M2: a claim failure that leaves nobody watching is a loss,
        # and a loss is loud; only a watched attempt stays silent.
        path = rewake.arm_path(self.jobs, "att-owner-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        stdout = "\n".join(("check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                            "parent_completion_delivery=claude-parent-runtime", "registered=1", "started=1",
                            "attempt_id=att-owner-1", "parent_session_id=session-1", f"job_registry={self.jobs}"))
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload(stdout=stdout)))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("reason=unreadable", err.getvalue())

    def test_a_receipt_naming_a_forged_registry_binds_nothing(self) -> None:
        # Review R2 B1: the receipt may name the trusted registry (env, else
        # canonical), never replace it -- a writable file full of
        # self-described rows is not a registry.
        forged = self.root / "forged.log"
        forged.write_text(self.row(attempt_id="att-forged"), encoding="utf-8")
        self.jobs.write_text("", encoding="utf-8")
        stdout = "\n".join(("check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                            "parent_completion_delivery=claude-parent-runtime", "registered=1", "started=1",
                            "attempt_id=att-forged", "parent_session_id=session-1", f"job_registry={forged}"))
        self.assertIsNone(rewake.parse_launch(self.payload(stdout=stdout)))
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload(stdout=stdout)))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("state=not-armed", err.getvalue())
        self.assertFalse((forged.parent / rewake.ARM_DIRECTORY / "att-forged.json").exists())
        # the same receipt naming the trusted registry, whose row proves it, arms
        self.jobs.write_text(self.row(attempt_id="att-forged"), encoding="utf-8")
        launch = rewake.parse_launch(self.payload(stdout=stdout.replace(str(forged), str(self.jobs))))
        assert launch is not None
        self.assertEqual((launch.attempt_id, launch.jobs), ("att-forged", self.jobs))

    def test_an_unusable_inherited_registry_trusts_nothing(self) -> None:
        # Review R3 B1: when AGENT_DISPATCH_JOBS is set but unusable, the hook
        # must not quietly trust the canonical registry in its place.
        link = self.root / "inherited-link.log"
        link.symlink_to(self.jobs)
        for bad in (str(link), str(self.root / "absent.log"), ""):
            with self.subTest(inherited=bad), \
                 mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": bad}), \
                 mock.patch.object(rewake, "_canonical_jobs", return_value=str(self.jobs)):
                self.assertIsNone(rewake._trusted_jobs())
                self.assertIsNone(rewake.parse_launch(self.payload(stdout=f"check=ok\njob_registry={self.jobs}")))
                self.assertIsNone(self.arm())
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(rewake, "_canonical_jobs", return_value=str(self.jobs)):
            self.assertEqual(rewake._trusted_jobs(), self.jobs)  # absent variable: canonical

    def test_ended_is_sealed_only_after_the_receipt_went_out(self) -> None:
        # Review R2 B2: a crash between classification and emission must leave
        # the claim re-armable, or the wake is lost forever.
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")), \
             mock.patch.object(rewake, "classified_receipt", side_effect=RuntimeError("crash")), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()):
            with self.assertRaises(RuntimeError):
                rewake.main()
        self.assertEqual(self.ledger("att-owner-1")["state"], "waiting")
        self.holder_exited("att-owner-1")
        launch = self.arm(self.payload(tool_input={"command": "ls"}))
        assert launch is not None
        self.assertEqual(launch.armed, "registry-rearm")

    def test_the_registry_path_keeps_the_receipt_named_attempts_refusal(self) -> None:
        # Review R2 M2: a partially filtered receipt whose row is stale says
        # `not-fresh`, not a generic `unclaimed`.
        self.jobs.write_text(self.row(attempt_id="att-owner-a", age_seconds=5)
                             + self.row(attempt_id="att-owner-b", age_seconds=4_000), encoding="utf-8")
        self.assertIsInstance(rewake.claim_arm(self.jobs, "att-owner-a", "session-1", fresh=True), rewake.ArmClaim)
        stdout = "check=ok\nstatus=start\nregistered=1\nstarted=1\nattempt_id=att-owner-b\nparent_session_id=session-1"
        refusal = rewake.registry_launch(self.payload(stdout=stdout))
        self.assertEqual(refusal.reason, "not-fresh")
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload(stdout=stdout)))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("reason=not-fresh", err.getvalue())

    def test_retention_prunes_old_ended_and_ancient_records_only(self) -> None:
        directory = rewake.arm_directory(self.jobs)
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        live = list(rewake._process_identity(os.getpid()))
        cases = {
            "att-ended-old": ({"state": "ended"}, 8 * 86_400, False),
            "att-ended-recent": ({"state": "ended"}, 86_400, True),
            "att-lapsed-old": ({"state": "lapsed"}, 8 * 86_400, True),
            "att-lapsed-ancient": ({"state": "lapsed"}, 31 * 86_400, False),
            "att-waiting-dead-ancient": ({"state": "waiting"}, 31 * 86_400, False),
            # review R2 M1: a live (or unobservable) holder's claim is never pruned
            "att-waiting-live-ancient": ({"state": "waiting", "holder": live}, 31 * 86_400, True),
            "att-gate-live-ancient": ({"state": "gate-wake-sent", "holder": live}, 31 * 86_400, True),
        }
        for name, (record, age, _kept) in cases.items():
            path = directory / f"{name}.json"
            path.write_text(json.dumps({"schema": 1, "attempt_id": name, "session_id": "session-1",
                                        "holder": DEAD_HOLDER, "arms": 1, **record}), encoding="utf-8")
            os.utime(path, (now - age, now - age))
        lock = directory / ".lock"; lock.write_text("", encoding="utf-8"); os.utime(lock, (now - 40 * 86_400,) * 2)
        self.assertIsInstance(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True), rewake.ArmClaim)
        for name, (_record, _age, kept) in cases.items():
            with self.subTest(name=name):
                self.assertEqual((directory / f"{name}.json").exists(), kept)
        self.assertTrue(lock.exists())

    def test_a_preserved_prefix_cannot_starve_expired_records_behind_it(self) -> None:
        # Review R3 M2: the scan window rotates, so a full window of live
        # holders in front does not keep an expired record behind it forever.
        directory = rewake.arm_directory(self.jobs)
        directory.mkdir(parents=True, exist_ok=True)
        now = time.time()
        live = list(rewake._process_identity(os.getpid()))
        for index in range(rewake.ARM_PRUNE_SCAN_LIMIT):
            path = directory / f"att-a{index:04d}.json"
            path.write_text(json.dumps({"schema": 1, "attempt_id": path.stem, "session_id": "session-1",
                                        "holder": live, "state": "waiting", "arms": 1}), encoding="utf-8")
            os.utime(path, (now - 31 * 86_400,) * 2)
        expired = directory / "att-zzz-lapsed.json"
        expired.write_text(json.dumps({"schema": 1, "attempt_id": "att-zzz-lapsed", "session_id": "session-1",
                                       "holder": DEAD_HOLDER, "state": "lapsed", "arms": 1}), encoding="utf-8")
        os.utime(expired, (now - 31 * 86_400,) * 2)
        rewake._prune_arm_directory(directory, now)   # first window: the live prefix only
        self.assertTrue(expired.exists())
        rewake._prune_arm_directory(directory, now)   # next window starts after the cursor
        self.assertFalse(expired.exists())
        self.assertEqual(len(list(directory.glob("att-a*.json"))), rewake.ARM_PRUNE_SCAN_LIMIT)

    def test_settle_records_the_outcome_only_for_the_holder(self) -> None:
        claim = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        assert isinstance(claim, rewake.ArmClaim)
        self.assertTrue(rewake.settle_arm(claim, "gate-wake-sent", gate_delivery_id="delivery-x"))
        record = self.ledger("att-owner-1")
        self.assertEqual((record["state"], record["gate_delivery_id"]), ("gate-wake-sent", "delivery-x"))
        stranger = rewake.ArmClaim(path=claim.path, attempt_id="att-owner-1", session_id="session-1",
                                   arms=1, holder=tuple(DEAD_HOLDER))
        self.assertFalse(rewake.settle_arm(stranger, "ended"))
        self.assertFalse(rewake.settle_arm(claim, "bogus"))
        self.assertEqual(self.ledger("att-owner-1")["state"], "gate-wake-sent")

    def test_a_partially_filtered_receipt_still_arms_from_the_registry(self) -> None:
        # grep-filtered stdout: started=1 survives but the fields the stdout
        # fast path needs do not. Silence here lost four fleet wakes on
        # 2026-09-01; the ledger arms the rows one call at a time instead.
        self.jobs.write_text(
            self.row(attempt_id="att-owner-a", slug="cleanup-a", age_seconds=5)
            + self.row(attempt_id="att-owner-b", slug="cleanup-b"),
            encoding="utf-8",
        )
        stdout = "check=ok\nstatus=start\nregistered=1\nstarted=1\nattempt_id=att-owner-b"
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload(stdout=stdout)))), \
             mock.patch.object(rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")) as wait, \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertEqual(wait.call_args.args[0].attempt_id, "att-owner-a")
        self.assertNotIn("state=not-armed", err.getvalue())
        self.assertEqual(self.ledger("att-owner-a")["state"], "ended")

    def test_a_started_receipt_with_no_registry_emits_one_typed_notice(self) -> None:
        stdout = "\n".join(("check=ok", "status=start", "registered=1", "started=1",
                            "attempt_id=att-owner-b", "parent_session_id=session-1"))
        payload = self.payload(stdout=stdout)
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(rewake, "_canonical_jobs", return_value=None), \
             mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.main(), 2)
        self.assertIn("state=not-armed", err.getvalue())
        self.assertIn("attempt_id=att-owner-b", err.getvalue())
        self.assertIn("state=not-armed", json.loads(out.getvalue())["systemMessage"])
        # The fallback it names is an absolute command, never a bare name off PATH.
        self.assertRegex(err.getvalue(), r" /\S+/utilities/dispatch-wait\.sh --attempt-id att-owner-b --max \d+")

    def test_a_started_receipt_whose_attempt_is_already_watched_stays_silent(self) -> None:
        self.assertIsInstance(rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True), rewake.ArmClaim)
        stdout = "\n".join(("check=ok", "status=start", "registered=1", "started=1",
                            "attempt_id=att-owner-1", "parent_session_id=session-1"))
        with mock.patch.object(rewake.sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rewake.no_arm_notice(self.payload(stdout=stdout)), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

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
        self.assertEqual(self.ledger("att-owner-1")["state"], "ended")

    def test_a_missing_readiness_helper_lapses_the_claim(self) -> None:
        # Review R1 B3: a helper missing from this call's agent home is a lapse
        # the next Bash call may recover from, never a permanent end.
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake, "agent_home", return_value=self.root / "no-such-home"), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()):
            self.assertEqual(rewake.main(), 0)
        self.assertEqual(self.ledger("att-owner-1")["state"], "lapsed")

    def test_a_timed_out_wait_lapses_and_the_next_call_rearms(self) -> None:
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake, "wait_for_attempt", return_value=("timeout", "owner-not-quiescent-after-1s")), \
             mock.patch.object(rewake.sys, "stdout", io.StringIO()), \
             mock.patch.object(rewake.sys, "stderr", io.StringIO()):
            self.assertEqual(rewake.main(), 0)
        self.assertEqual(self.ledger("att-owner-1")["state"], "lapsed")
        self.assertIsNone(self.arm())  # the settling process is still alive for an instant
        self.holder_exited("att-owner-1")
        launch = self.arm(self.payload(tool_input={"command": "git status"}))
        assert launch is not None
        self.assertEqual(launch.armed, "registry-rearm")

    def test_foreign_stale_or_sessionless_input_never_arms(self) -> None:
        self.jobs.write_text(self.row(parent_sid="session-2"), encoding="utf-8")
        self.assertIsNone(self.arm())
        self.jobs.write_text(self.row(age_seconds=4_000), encoding="utf-8")
        self.assertIsNone(self.arm())
        self.jobs.write_text(self.row(), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.payload(session_id="")))
        self.assertIsNone(rewake.registry_launch(self.payload(session_id=None)))
        self.assertIsNone(rewake.registry_launch(self.payload(tool_name="Read")))

    def test_closed_or_non_owner_rows_never_arm(self) -> None:
        self.jobs.write_text(
            self.row() + self.row(status="done", note="completed-marker"), encoding="utf-8"
        )
        self.assertIsNone(self.arm())
        self.jobs.write_text(self.row(dispatch_depth="2"), encoding="utf-8")
        self.assertIsNone(self.arm())
        self.jobs.write_text(self.row(launch_started="0"), encoding="utf-8")
        self.assertIsNone(self.arm())
        self.jobs.write_text(
            self.row(parent_completion_delivery="one-shot"), encoding="utf-8"
        )
        self.assertIsNone(self.arm())

    def test_the_command_jobs_literal_is_not_read_and_symlinked_receipts_are_rejected(self) -> None:
        other = self.root / "explicit.log"
        other.write_text(self.row(attempt_id="att-owner-explicit"), encoding="utf-8")
        payload = self.payload()
        payload["tool_input"]["command"] = (
            f"python3 utilities/dispatch-owner.py --start --jobs {other} --slug owner | tail -5"
        )
        launch = self.arm(payload)
        assert launch is not None
        self.assertEqual((launch.attempt_id, launch.jobs), ("att-owner-1", self.jobs))
        link = self.root / "jobs-link.log"
        link.symlink_to(other)
        self.jobs.write_text(self.row(attempt_id="att-owner-2"), encoding="utf-8")
        # a receipt naming a symlink (or any file other than the trusted registry) binds nothing
        self.assertIsNone(self.arm(self.payload(stdout=f"check=ok\njob_registry={link}",
                                                tool_input={"command": "x"})))
        self.assertIsNone(self.arm(self.payload(stdout=f"check=ok\njob_registry={other}",
                                                tool_input={"command": "x"})))

    def test_missing_all_sealed_registry_sources_does_not_reconstruct_agent_home(self) -> None:
        payload = self.payload(stdout="check=ok")
        # Keep this negative fixture hermetic: a maintainer machine may have a
        # live fallback registry under its stable per-user state root.
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            rewake, "_canonical_jobs", return_value=None
        ):
            self.assertIsNone(rewake.registry_launch(payload))

    def test_space_delimited_registry_metadata_is_tolerated(self) -> None:
        self.jobs.write_text(self.row().replace(",", " "), encoding="utf-8")
        launch = self.arm()
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")

    def test_every_launcher_surface_arms_the_fresh_row_once(self) -> None:
        # Whatever launched the owner -- `preflight.sh dispatch-owner`, a
        # `time`/`nohup`/`bash -c`/`python3 -u` prefix (the R8 majors the
        # parser never closed), or a foreign command that merely mentions the
        # utility -- the fresh same-session row arms exactly once. Arming is
        # the row's debt to this session, not a property of the command
        # surface; the ledger, not the parser, keeps a foreign mention from
        # re-arming a prior attempt.
        commands = (
            '"$AGENT_HOME/adapters/codex/bin/preflight.sh" dispatch-owner --start --slug s',
            "time nohup python3 -u utilities/dispatch-owner.py --start --slug s > /dev/null 2>&1",
            "bash -c 'python3 utilities/dispatch-owner.py --start --slug s'",
            "grep dispatch-owner --start jobs.log",
            "cat preflight.sh dispatch-owner",
        )
        for index, command in enumerate(commands):
            with self.subTest(command=command):
                self.jobs.write_text(self.row(attempt_id=f"att-owner-{index}"), encoding="utf-8")
                launch = self.arm(self.payload(tool_input={"command": command}))
                assert launch is not None
                self.assertEqual(launch.attempt_id, f"att-owner-{index}")
                self.assertIsNone(self.arm(self.payload(tool_input={"command": command})))


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
        self._fixture_child_handles = []
        self.root = Path(self.temporary.name)
        fixture_root = str(self.root)
        child_handles = self._fixture_child_handles

        def reap_fixture_processes():
            fixture_processes.reap(fixture_root)
            for child in child_handles:
                child.wait(timeout=10)

        self.addCleanup(reap_fixture_processes)
        self.jobs = self.root / "jobs.log"
        self.agent_home = MODULE_PATH.parents[1]
        home = self.root / "home"
        artifacts = self.root / "artifacts"
        runtime = self.root / "runtime"
        for path in (home, artifacts / "cycle" / "artifacts", runtime,
                     self.root / "xdg-state", self.root / "xdg-config",
                     self.root / "xdg-data", self.root / "xdg-cache",
                     self.root / "peer-ledger", self.root / "harness-state"):
            path.mkdir(parents=True, exist_ok=True)
        fixture_env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TERM")
                       if key in os.environ}
        fixture_env.update({
            "AGENT_HOME": str(self.agent_home),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_PEER_LEDGER_ROOT": str(self.root / "peer-ledger"),
            "AGENT_RUNTIME_DIR": str(runtime),
            "AGENT_ARTIFACT_ROOT": str(artifacts),
            "AGENT_ARTIFACT_CYCLE_DIR": str(artifacts / "cycle"),
            "AGENT_ARTIFACT_OUTPUT_DIR": str(artifacts / "cycle" / "artifacts"),
            "AGENT_ARTIFACT_CAMPAIGN_ID": "campaign-fixture",
            "AGENT_ARTIFACT_CYCLE_ID": "cycle-fixture",
            "AGENT_ARTIFACT_PRODUCER_ID": "producer-fixture",
            "HOME": str(home),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
            "XDG_CACHE_HOME": str(self.root / "xdg-cache"),
            "HARNESS_STATE_ROOT": str(self.root / "harness-state"),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        })
        self.environment = mock.patch.dict(os.environ, fixture_env, clear=True)
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
            "worker_type=owner,launch_claimed=1,launch_started=1,"
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
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            f"{stamp}\topen\t/repo\t/wt\towner\t{pipe}\n", encoding="utf-8"
        )

    def _close_and_materialize(self, note="completed-marker"):
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", note))
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

    def test_lost_carrier_completion_reaches_existing_runner_without_user_prompt(self):
        self._check_lost_carrier_completion()

    def test_successor_carrier_crash_preserves_original_storage_recipient(self):
        self._check_lost_carrier_completion(crash_sid="session-2", recipient_sid="session-2")

    def test_original_retained_duty_follows_confirmed_handover(self):
        self._check_lost_carrier_completion(recipient_sid="session-2")

    def test_changed_pane_does_not_receive_or_complete_retained_duty(self):
        self._check_lost_carrier_completion(changed_pane=True)

    def test_a_live_native_holder_cannot_suppress_wake_after_its_parent_died(self):
        self._check_lost_carrier_completion(live_holder=True)

    def test_restored_parent_uses_existing_courier_without_user_prompt_all_harnesses(self):
        self._check_restored_courier(immediate=True)

    def test_queued_restored_wake_waits_for_exact_receipt_without_another_send(self):
        self._check_restored_courier(immediate=False)

    def test_a_notice_consumed_by_another_carrier_suppresses_its_deferred_peer_payload(self):
        self._check_restored_courier(immediate=False, other_carrier=True)

    def test_repeated_received_callback_repairs_ack_after_transient_failure(self):
        self._check_restored_courier(immediate=True, recovery="repeat-receive")

    def test_retained_courier_repairs_ack_after_reclaim_without_resend(self):
        self._check_restored_courier(immediate=True, recovery="courier-reentry",
                                     close_note="dead-worker-fail")

    def test_new_courier_instance_repairs_ack_after_receiver_interruption(self):
        self._check_restored_courier(immediate=True, recovery="crash-before-ack")

    def _check_restored_courier(self, *, immediate, other_carrier=False, recovery=None,
                                close_note="completed-marker"):
        spec = importlib.util.spec_from_file_location("restored_steward_test", ROOT / "utilities/peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(steward)
        import codex_queue_delivery
        import codex_queue_dispatch
        from dispatch_session_sweep import activate, sweep_deliver
        self.enterContext(mock.patch.dict(os.environ, {"AGENT_PEER_LEDGER_ROOT": str(self.root)}))
        self.enterContext(mock.patch.object(steward.peer_obligations, "ensure_normal_runner"))
        # The recipient is a restorable native session: activation happens
        # while its agent is unavailable, then the same session becomes
        # available below. Keep the recorded parent process live for this
        # scenario; the class-level synthetic ancestry is positively dead and
        # would correctly trigger the separate recipient-death expiry path.
        restorable_parent = rewake._process_identity(os.getpid())
        self.assertIsNotNone(restorable_parent)
        for harness, carrier in (("claude", "claude-parent-runtime"),
                                 ("codex", "codex-native-queue"), ("opencode", "opencode-turn")):
            with self.subTest(harness=harness):
                self._open_row(ancestry=restorable_parent)
                self.jobs.write_text(self.jobs.read_text().replace("claude-parent-runtime", carrier))
                path = self._close_and_materialize(close_note)
                with mock.patch.object(rewake.peer_obligations, "ensure_normal_runner"), \
                        mock.patch.object(steward.peer_obligations, "request_dispatch_reconciliation") as reconcile:
                    activate(self.root, carrier, "session-1")
                    reconcile.assert_called_once_with(self.root)
                store = rewake.peer_obligations.ObligationStore(self.root)
                duty = next(d for d in store.list() if d["intent"]["identity"]["harness"] == harness)
                self.assertEqual(json.loads(path.read_text())["attempts"], 0)
                agents = {"result": {"agents": []}}
                recipient_agent = {"agent": harness, "pane_id": "w1:p9", "name": "fixture",
                                   "agent_status": "idle",
                                   "agent_session": {"kind": "id", "value": "session-1"}}
                process_pid = 73101
                process_info = {"pane_id": "w1:p9", "shell_pid": 73100,
                                "foreground_process_group_id": process_pid,
                                "foreground_processes": [{"pid": process_pid, "argv": [harness]}]}
                observed_commands = []
                native_identity_calls = []
                codex_thread_calls = []

                def external_observation(argv, **_kwargs):
                    command = list(argv)
                    if command[:2] == ["herdr", "--session"]:
                        command = command[3:]
                    elif command[:1] == ["herdr"]:
                        command = command[1:]
                    observed_commands.append(tuple(command))
                    if command[:2] == ["agent", "list"]:
                        payload = agents
                    elif command[:2] == ["agent", "get"]:
                        self.assertEqual(command[2], "w1:p9")
                        payload = {"result": {"agent": recipient_agent}}
                    elif command[:2] == ["pane", "list"]:
                        payload = {"result": {"panes": [{
                            "pane_id": "w1:p9", "agent": harness, "agent_status": "idle",
                            "agent_session": {"kind": "id", "value": "session-1"},
                            "title": "fixture", "cwd": str(self.root),
                        }]}}
                    elif command[:2] == ["pane", "process-info"]:
                        self.assertEqual(command[command.index("--pane") + 1], "w1:p9")
                        payload = {"result": {"process_info": process_info}}
                    else:
                        self.fail(f"unexpected herdr observation request: {command!r}")
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=json.dumps(payload), stderr="")

                from types import SimpleNamespace
                from fleet import process_identity as native_identity
                import pane_ownership

                def process_identity(pid, observed_harness):
                    native_identity_calls.append((pid, observed_harness))
                    if pid == process_pid and observed_harness == harness:
                        return SimpleNamespace(confidence=native_identity.PROVEN,
                                               session_id="session-1")
                    return SimpleNamespace(confidence=native_identity.HARNESS_ONLY,
                                           session_id="")

                def current_codex_thread(pane, pid=None, server=None, **_kwargs):
                    codex_thread_calls.append((pane, pid, server))
                    return "session-1" if pane == "w1:p9" and pid == process_pid else ""

                real_prompt = steward.cmd_prompt
                delivered = []
                sent_texts = []
                prepared_refs = []
                real_write = rewake.pending_delivery._write_unlocked
                real_receiver_ack = steward.peer_message._ack_registered_delivery
                # Retain a storage outage for the whole initial native callback,
                # including its exact duplicate receipts. The next callback or
                # courier reentry runs outside this prompt's write patch.
                ack_failure = recovery in {"repeat-receive", "courier-reentry"}
                crash_failure = {"remaining": 1 if recovery == "crash-before-ack" else 0}
                def write_record(path, value):
                    if ack_failure and value.get("state") == "acked":
                        raise OSError("simulated received-delivery ACK write failure")
                    return real_write(path, value)
                def receiver_ack(received):
                    if crash_failure["remaining"]:
                        crash_failure["remaining"] -= 1
                        raise SystemExit("simulated interruption after received persistence")
                    return real_receiver_ack(received)
                real_bind = steward.peer_message.bind_dispatch_notice
                def bind(ref, binding):
                    prepared_refs.append(ref)
                    return real_bind(ref, binding)

                def receive_native(text):
                    sent_texts.append(text)
                    record = json.loads(path.read_text())
                    self.assertGreater(record["claim_deadline_ns"], time.monotonic_ns())
                    self.assertEqual(sweep_deliver(self.root, carrier, "session-1")[0], [])
                    ref = steward.peer_message.parse_peer_trailer(
                        text, {"harness": harness, "session_id": "session-1"}, include_ref=True
                    )["transfer_ref"]
                    if harness == "claude":
                        # Claude's existing UserPromptSubmit context carrier emits the exact
                        # pending row; context emission is still not receiver acknowledgement.
                        stream = io.StringIO()
                        steward.peer_message.emit_peer_context(
                            {"harness": harness, "session_id": "session-1"},
                            "UserPromptSubmit", stream=stream)
                        emitted = json.loads(stream.getvalue())["hookSpecificOutput"]["additionalContext"]
                        self.assertIn(text, emitted)
                        text = emitted
                    elif harness == "opencode":
                        # Match the plugin's existing promptAsync acceptance transition.
                        message_id = "opencode-item-" + ref
                        queued = steward.peer_message.argparse.Namespace(
                            to_harness=harness, to_session_id="session-1", claim=None,
                            queued=ref, resuming=None, received=None, message_id=message_id)
                        self.assertEqual(steward.peer_message.cmd_pending(queued), 0)
                    self.assertEqual(steward.peer_message.receive_peer_message(text, {
                        "harness": harness, "session_id": "session-1"}), 0)
                    if harness == "opencode":
                        received = steward.peer_message.argparse.Namespace(
                            to_harness=harness, to_session_id="session-1", claim=None,
                            queued=None, resuming=None, received=ref, message_id=message_id)
                        self.assertEqual(steward.peer_message.cmd_pending(received), 0)
                    delivered.append(text)

                native_sends = []
                def codex_native_send(_socket, *, thread_id, client_message_id, message,
                                      before_add, **_options):
                    native_sends.append((thread_id, client_message_id, message))
                    self.assertEqual(thread_id, "session-1")
                    self.assertEqual(client_message_id, "delivery-peer-" + prepared_refs[0])
                    before_add()
                    if not immediate:
                        return {"status": "queued", "queued_submission_id": "queue-item-1"}
                    receive_native(message)
                    return {"status": "consumed", "queued_submission_id": None}

                def prompt(args, **kwargs):
                    # Keep native claim/receiver accounting real. Only Codex's external
                    # socket API is replaced; Claude/OpenCode callbacks use their existing
                    # native context/pending transitions and exact-session receiver.
                    with mock.patch.object(rewake.pending_delivery, "_write_unlocked",
                                           side_effect=write_record), \
                            mock.patch.object(steward.peer_message, "_ack_registered_delivery",
                                              side_effect=receiver_ack), \
                            mock.patch.object(steward.peer_message, "bind_dispatch_notice", side_effect=bind), \
                            mock.patch.object(steward, "_current_session_identity", return_value=("sender-1", "claude")), \
                            mock.patch.object(steward, "_from_name", return_value="fixture"), \
                            mock.patch.object(steward, "_pane_readiness", return_value=type(
                                "Ready", (), {"state": "ready", "reason": ""})()), \
                            mock.patch.object(steward, "_flush_pending_for_target", return_value=(0, 0)), \
                            mock.patch.object(steward, "_schedule_message_obligation") as scheduled, \
                            mock.patch.object(steward, "_herdr_prompt",
                                              side_effect=AssertionError("native courier used PTY prompt")), \
                            mock.patch.object(codex_queue_dispatch, "resolve_queue_socket",
                                              return_value=self.root / "codex.sock"), \
                            mock.patch.object(codex_queue_delivery, "send_at_least_once",
                                              side_effect=codex_native_send), \
                            mock.patch.object(steward, "_record"):
                        code = real_prompt(args, **kwargs)
                        if harness == "codex":
                            scheduled.assert_not_called()
                        else:
                            scheduled.assert_called_once()
                        if immediate and harness in {"claude", "opencode"}:
                            receive_native(steward.peer_message._read_pending(prepared_refs[0])["text"])
                        if immediate and recovery is None:
                            received = steward.peer_message._read_pending(prepared_refs[0])
                            with mock.patch.object(
                                    steward.peer_message, "_observe_continue_receipt",
                                    wraps=steward.peer_message._observe_continue_receipt) as observe, \
                                    mock.patch.object(steward.peer_message, "_ack_registered_delivery",
                                                      wraps=real_receiver_ack) as repair_ack:
                                self.assertEqual(
                                    steward.peer_message.deliver_pending_codex(prepared_refs[0])["status"],
                                    "received")
                            observe.assert_called_once_with(
                                steward.peer_message.pending_delivery_recipient(received) or {},
                                prepared_refs[0])
                            repair_ack.assert_called_once_with(received)
                        return code
                with mock.patch.object(steward.shutil, "which", return_value="/usr/bin/herdr"), \
                        mock.patch.object(steward.subprocess, "run", side_effect=external_observation), \
                        mock.patch.object(native_identity, "process_identity", side_effect=process_identity), \
                        mock.patch.object(pane_ownership, "current_codex_thread",
                                          side_effect=current_codex_thread), \
                        mock.patch.object(rewake.pending_delivery, "claim",
                                          wraps=rewake.pending_delivery.claim) as claim_spy, \
                        mock.patch.object(steward, "cmd_prompt", side_effect=prompt) as prompt_call:
                    steward._resume_registered_obligation(duty, store)
                    prompt_call.assert_not_called()
                    self.assertEqual(json.loads(path.read_text())["state"], "pending")
                    self.assertEqual(store.get(duty["id"])["observation"]["reason"], "recipient-unavailable")
                    agents["result"]["agents"] = [{"agent": harness, "pane_id": "w1:p9",
                                                    "agent_session": {"value": "session-1"}}]
                    if recovery == "crash-before-ack":
                        with self.assertRaisesRegex(SystemExit, "after received persistence"):
                            steward._resume_registered_obligation(store.get(duty["id"]), store)
                    else:
                        steward._resume_registered_obligation(store.get(duty["id"]), store)
                    self.assertEqual(prompt_call.call_count, 1)
                    self.assertEqual(prompt_call.call_args.kwargs["expected_recipient"], (harness, "session-1"))
                    self.assertTrue(any(call.kwargs.get("jobs") == self.jobs
                                        for call in claim_spy.call_args_list))
                    self.assertIn(("agent", "get", "w1:p9"), observed_commands)
                    self.assertTrue(any(command[:2] == ("pane", "process-info")
                                        for command in observed_commands))
                    if harness == "codex":
                        self.assertTrue(codex_thread_calls)
                        self.assertTrue(all(pane == "w1:p9" and pid == process_pid
                                            for pane, pid, _server in codex_thread_calls))
                    else:
                        self.assertTrue(native_identity_calls)
                        self.assertTrue(all((pid, observed_harness) == (process_pid, harness)
                                            for pid, observed_harness in native_identity_calls))
                    if not immediate:
                        queued = store.get(duty["id"])
                        self.assertEqual(queued["state"], "pending")
                        self.assertEqual(json.loads(path.read_text())["state"], "sent-ambiguous")
                        transfer = steward.peer_message._read_pending(queued["observation"]["transfer_ref"])
                        text = transfer["text"]
                        if harness == "codex":
                            self.assertEqual(transfer["state"], "queued")
                            self.assertEqual(len(native_sends), 1)
                        steward._resume_registered_obligation(queued, store)
                        self.assertEqual(prompt_call.call_count, 1)
                        self.assertEqual(len(native_sends), 1 if harness == "codex" else 0)
                        # An unrelated session and an altered body cannot consume the notice.
                        steward.peer_message.receive_peer_message(text, {"harness": harness, "session_id": "stranger"})
                        steward.peer_message.receive_peer_message("altered " + text, {"harness": harness, "session_id": "session-1"})
                        self.assertEqual(json.loads(path.read_text())["state"], "sent-ambiguous")
                        if other_carrier:
                            rewake.pending_delivery.ack(self.root, "session-1", queued["observation"]["delivery_id"],
                                                         acked_by="other-native-carrier")
                            self.assertEqual(steward.peer_message.pending_messages({
                                "harness": harness, "session_id": "session-1"}), [])
                            self.assertIsNone(steward.peer_message.claim_pending_herdr(transfer["ref"], transfer["to"]))
                            self.assertEqual(steward.peer_message.deliver_pending_codex(transfer["ref"])["status"], "not-required")
                            msg = store.get("message-" + transfer["ref"])
                            if harness == "codex":
                                # The exact-thread queue is the retry owner for Codex;
                                # this route does not mint a separate peer duty.
                                self.assertIsNone(msg)
                                send_count = len(native_sends)
                                steward.peer_message.retry_pending_codex({
                                    "harness": harness, "session_id": "session-1"})
                                self.assertEqual(len(native_sends), send_count)
                            else:
                                self.assertIsNotNone(msg)
                                with mock.patch.object(steward, "_flush_pending_for_target") as resend:
                                    steward._resume_message_obligation(msg, store)
                                    resend.assert_not_called()
                            if msg is not None:
                                self.assertEqual(store.get(msg["id"])["state"], "complete")
                            self.assertEqual(
                                steward.peer_message._read_pending(transfer["ref"])["state"],
                                "queued" if harness == "codex" else "pending")
                            steward._resume_registered_obligation(queued, store)
                            self.assertEqual(prompt_call.call_count, 1)
                            self.assertEqual(store.get(duty["id"])["state"], "complete")
                            self.jobs.write_text("")
                            continue
                        receive_native(text)
                    elif recovery != "crash-before-ack":
                        self.assertEqual(len(delivered), 1)
                    if recovery == "repeat-receive":
                        self.assertIn("required_action=advance-completed", sent_texts[-1])
                        self.assertEqual(json.loads(path.read_text())["state"], "sent-ambiguous")
                        self.assertEqual(steward.peer_message.receive_peer_message(sent_texts[-1], {
                            "harness": harness, "session_id": "session-1"}), 0)
                        self.assertEqual(json.loads(path.read_text())["state"], "acked")
                        self.assertEqual(prompt_call.call_count, 1)
                        self.assertEqual(len(prepared_refs), 1)
                    elif recovery == "courier-reentry":
                        self.assertIn("required_action=inspect-done-failure", sent_texts[-1])
                        current = json.loads(path.read_text())
                        current_duty = store.get(duty["id"])
                        rewake.pending_delivery.reclaim(self.root, "session-1",
                            current_duty["observation"]["delivery_id"],
                            now_ns=current["claim_deadline_ns"] + 1)
                        steward._resume_registered_obligation(current_duty, store)
                        self.assertEqual(json.loads(path.read_text())["state"], "acked")
                        self.assertEqual(prompt_call.call_count, 1)
                        self.assertEqual(len(prepared_refs), 1)
                        self.assertEqual(len(native_sends), 0 if harness != "codex" else 1)
                    elif recovery == "crash-before-ack":
                        self.assertEqual(json.loads(path.read_text())["state"], "claimed")
                        fresh_spec = importlib.util.spec_from_file_location(
                            "restarted_steward_test", ROOT / "utilities/peer-steward.py")
                        fresh = importlib.util.module_from_spec(fresh_spec)
                        fresh_spec.loader.exec_module(fresh)
                        with mock.patch.object(fresh.subprocess, "run",
                                               side_effect=AssertionError("received work must not rerun transport")), \
                                mock.patch.object(fresh, "cmd_prompt",
                                                  side_effect=AssertionError("received notice must not be resent")):
                            fresh._resume_registered_obligation(store.get(duty["id"]), store)
                        self.assertEqual(json.loads(path.read_text())["state"], "acked")
                        self.assertEqual(prompt_call.call_count, 1)
                        self.assertEqual(len(prepared_refs), 1)
                        self.assertEqual(len(native_sends), 0 if harness != "codex" else 1)
                        self.assertEqual(store.get(duty["id"])["state"], "complete")
                    else:
                        self.assertEqual(json.loads(path.read_text())["state"], "acked")
                        self.assertEqual(store.get(duty["id"])["state"], "complete")
                        self.assertEqual(len(native_sends), 0 if harness != "codex" else 1)
                with mock.patch.object(steward, "cmd_prompt") as prompt:
                    steward._resume_registered_obligation(store.get(duty["id"]), store)
                    prompt.assert_not_called()
                # The next subcase uses a distinct notice identity.
                self.jobs.write_text("")

    def _check_lost_carrier_completion(self, crash_sid="session-1", recipient_sid="session-1",
                                       changed_pane=False, live_holder=False):
        self._open_row()
        spec = importlib.util.spec_from_file_location("retained_steward_test", ROOT / "utilities/peer-steward.py")
        steward = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(steward)
        launch = rewake.Launch("att-owner-1", self.jobs, crash_sid)
        claim = rewake.ArmClaim(rewake.arm_path(self.jobs, launch.attempt_id), launch.attempt_id,
                                launch.session_id, 1, tuple(DEAD_HOLDER))
        with mock.patch.object(rewake.peer_obligations, "ensure_normal_runner"), \
                mock.patch.object(rewake.seat_handover, "effective_parent", return_value=crash_sid), \
                mock.patch.object(rewake, "_run_carrier", side_effect=RuntimeError("carrier crashed")):
            with self.assertRaisesRegex(RuntimeError, "carrier crashed"):
                rewake._observe_carrier(launch, claim, {})
        store = rewake.peer_obligations.ObligationStore(self.root)
        duty, = store.list()
        sent = []
        real_prompt = steward.cmd_prompt
        def prompt(args, *, expected_recipient, on_prepared=None, producer_binding=None):
            self.assertEqual(expected_recipient, ("claude", recipient_sid))
            self.assertIsInstance(producer_binding, dict)
            delivery = json.loads(path.read_text())
            self.assertEqual(producer_binding["schema"], steward.peer_message._RUNTIME_PRODUCER_SCHEMA)
            self.assertEqual(producer_binding["duty_id"], duty["id"])
            self.assertEqual(producer_binding["jobs"], str(self.jobs))
            self.assertEqual(producer_binding["attempt_id"], duty["intent"]["identity"]["attempt_id"])
            self.assertEqual(producer_binding["carrier"], duty["intent"]["carrier"])
            self.assertEqual(producer_binding["delivery_id"], delivery["delivery_id"])
            self.assertEqual(producer_binding["receipt_digest"], delivery["receipt_digest"])
            self.assertEqual(producer_binding["recipient"], {
                "harness": expected_recipient[0], "session_id": expected_recipient[1]})
            self.assertEqual(producer_binding["route_id"], "rt-owner")
            self.assertEqual(producer_binding["route_node"], "report")
            self.assertEqual(args.ref, [duty["id"], duty["intent"]["identity"]["attempt_id"],
                                        delivery["delivery_id"]])
            if changed_pane:
                # The real courier must refuse before sealing any payload if
                # its own target read no longer matches the retained identity.
                with mock.patch.object(steward, "_herdr_missing", return_value=False), \
                        mock.patch.object(steward, "_resolve_target", return_value=("claude", "stranger", None)), \
                        mock.patch.object(steward.peer_message, "prepare_peer_message") as sealed:
                    code = real_prompt(args, expected_recipient=expected_recipient,
                                       on_prepared=on_prepared, producer_binding=producer_binding)
                    sealed.assert_not_called()
                    return code
            sent.append(Path(args.body_file).read_text())
            return 0
        agents = {"result": {"agents": [{"agent": "claude", "pane_id": "w1:p9",
                                        "agent_session": {"value": recipient_sid}}]}}
        holder_alive = (lambda holder: holder == list(DEAD_HOLDER)) if live_holder else rewake._holder_alive
        with mock.patch.dict(sys.modules, {"_retained_rewake": rewake}), \
                mock.patch.object(rewake, "_holder_alive", side_effect=holder_alive), \
                mock.patch.object(steward.subprocess, "run", return_value=type("Reply", (), {
                    "stdout": json.dumps(agents), "returncode": 0})()), \
                mock.patch.object(rewake.seat_handover, "effective_parent", return_value=recipient_sid), \
                mock.patch.object(steward, "_resolve_target", return_value=("claude", recipient_sid, None)), \
                mock.patch.object(steward, "cmd_prompt", side_effect=prompt):
            # A dead carrier is not completed work and must wait for its owner.
            steward._resume_registered_obligation(duty, store)
            self.assertEqual(sent, [])
            path = self._close_and_materialize()
            steward._resume_registered_obligation(duty, store)
            if changed_pane:
                self.assertEqual(sent, [])
                self.assertEqual(store.get(duty["id"])["state"], "pending")
                self.assertEqual(json.loads(path.read_text())["state"], "pending")
                return
            self.assertEqual(len(sent), 1)
            self.assertIn("att-owner-1", sent[0])
            self.assertEqual(store.get(duty["id"])["state"], "pending")
            self.assertEqual(json.loads(path.read_text())["state"], "sent-ambiguous")
            # Acknowledged notices retain their original delivery identity.
            rewake.pending_delivery.ack(self.root, "session-1", json.loads(path.read_text())["delivery_id"],
                                         acked_by="session-sweep:session-1")
            steward._resume_registered_obligation(duty, store)
            self.assertEqual(len(sent), 1)

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
        # it itself (§4.4) and must go silent, not raise, once its bounded
        # record grace is spent (grace 0 here).
        self._open_row()
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", "completed-marker"))
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY), \
             mock.patch.dict(rewake.os.environ, {"AGENT_CLAUDE_REWAKE_RECORD_GRACE_SECONDS": "0"}):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_a_record_that_materializes_during_the_grace_is_claimed_and_wakes(self):
        # 2026-09-17: an owner's supervisor closed its row while alive; the
        # record appeared only after the process group drained. The carrier
        # must wait for it instead of treating "not yet" as "someone else".
        self._open_row()
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", "completed-marker"))
        fields = self.jobs.read_text(encoding="utf-8").splitlines()[0].split("\t")
        calls = {"n": 0}
        real_sleep = rewake.time.sleep

        def late_materialize(_seconds):
            calls["n"] += 1
            if calls["n"] == 2:
                self.assertIsNotNone(JOIN.materialize_pending_delivery(self.jobs, fields))
            real_sleep(0)

        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY), \
             mock.patch.object(rewake.time, "sleep", side_effect=late_materialize), \
             mock.patch.dict(rewake.os.environ, {
                 "AGENT_CLAUDE_REWAKE_RECORD_GRACE_SECONDS": "60",
                 "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
             }):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 2)
        self.assertIn("systemMessage", stdout)
        self.assertGreaterEqual(calls["n"], 2)

    def test_a_record_claimed_elsewhere_does_not_wait_out_the_grace(self):
        self._open_row()
        record_path = self._close_and_materialize()
        record = json.loads(record_path.read_text(encoding="utf-8"))
        root = self.jobs.resolve(strict=False).parent
        rewake.pending_delivery.claim(
            root, "session-1", record["delivery_id"],
            claim_owner="someone-else", lease_seconds=30.0,
        )
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY), \
             mock.patch.object(rewake.time, "sleep", side_effect=AssertionError("must not wait")), \
             mock.patch.dict(rewake.os.environ, {"AGENT_CLAUDE_REWAKE_RECORD_GRACE_SECONDS": "60"}):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")

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
        #
        # A still-open owner is no longer announced as `attention` by a stale
        # `ready` readout; the wait lapses at its deadline and the typed
        # timeout notice goes out ungated, with the arm left re-armable.
        self._open_row()
        with mock.patch.dict(os.environ, {"AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
                                          "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "1"}), \
             mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, stdout, stderr = self._run_main()
        self.assertEqual(code, 0)
        self.assertIn("state=timeout", stdout)
        self.assertEqual(rewake._read_arm(rewake.arm_path(self.jobs, "att-owner-1"))["state"], "lapsed")


class CarrierFixtureCleanupBoundaryTest(unittest.TestCase):
    def _run_probe(self, fail_body):
        observations = {}
        original_cleanup = tempfile.TemporaryDirectory.cleanup
        writer = (
            "import os,time\n"
            "d=os.environ['FIXTURE_DIR']\n"
            "open(os.path.join(d,'writer-ready'),'w').write(str(os.getpid()))\n"
            "i=0\n"
            "while True:\n"
            "    i+=1; open(os.path.join(d,'writer-%d'%i),'w').close(); time.sleep(0.005)\n"
        )

        class Probe(CarrierOneClaimGateTest):
            def __init__(self):
                super().__init__("runTest")

            def setUp(self):
                super().setUp()
                env = dict(os.environ, FIXTURE_DIR=str(self.root))
                child = subprocess.Popen([sys.executable, "-c", writer], env=env,
                                         start_new_session=True, stdin=subprocess.DEVNULL)
                self._fixture_child_handles.append(child)
                deadline = time.monotonic() + 10
                ready = self.root / "writer-ready"
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "fixture writer did not start")
                pid = int(ready.read_text(encoding="utf-8"))
                start = fixture_processes._start_ticks(pid)
                self.assertIsNotNone(start)
                observations.update(root=str(self.root), pid=pid, start=start)

            def runTest(self):
                self.assertIn(observations["pid"],
                              fixture_processes.marked_processes(observations["root"]))
                if fail_body:
                    self.fail("intentional carrier fixture assertion failure")

        def observe_before_delete(temporary_directory):
            if temporary_directory.name == observations.get("root"):
                pid, start = observations["pid"], observations["start"]
                observations["remaining"] = fixture_processes.marked_processes(temporary_directory.name)
                observations["gone_before_delete"] = fixture_processes._gone(pid, start)
                observations["root_present_before_delete"] = Path(temporary_directory.name).is_dir()
            return original_cleanup(temporary_directory)

        result = unittest.TestResult()
        with mock.patch.object(tempfile.TemporaryDirectory, "cleanup", observe_before_delete):
            Probe().run(result)
        return result, observations

    def _assert_cleanup_evidence(self, observations):
        self.assertTrue(observations["root_present_before_delete"])
        self.assertTrue(observations["gone_before_delete"])
        self.assertEqual(observations["remaining"], {})
        self.assertFalse(Path(observations["root"]).exists())

    def test_carrier_fixture_drains_writer_after_normal_body(self):
        result, observations = self._run_probe(False)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self._assert_cleanup_evidence(observations)

    def test_carrier_fixture_preserves_body_failure_and_drains_writer(self):
        result, observations = self._run_probe(True)
        self.assertEqual(len(result.failures), 1)
        self.assertIn("intentional carrier fixture assertion failure", result.failures[0][1])
        self.assertEqual(result.errors, [])
        self._assert_cleanup_evidence(observations)


class LosingCarrierLeavesGatesTest(CarrierOneClaimGateTest):
    """Top review B1: a hook that lost the completion claim must not consume
    the recipient's gate records on its way to a silent exit, and a winning
    hook acks a folded gate only after its receipt went out."""

    def _gate(self, delivery_id="delivery-independent-gate", owner="att-parallel-owner"):
        receipt = {"schema_version": 2, "state": "attention", "parent_attempt_id": owner,
                   "job_registry": str(self.jobs), "delivery_classification": "attention",
                   "children": [{"attempt_id": owner, "status": "open", "readiness": "human-gate",
                                 "reason": str(self.root / "interview.json"),
                                 "required_action": "human-gate:frame-review", "harness": "claude",
                                 "delivery_classification": "attention"}]}
        rewake.pending_delivery.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key="session-1",
            delivery_id=delivery_id, session_generation="unsupported", session_generation_supported="0",
            attempt_ids=[owner], parent_attempt_id=owner, route_id="rt-parallel", route_node="frame",
            receipt=receipt, receipt_digest=rewake.pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={owner: "human-gate:frame-review"})
        _gate_authority(self.jobs, "session-1", delivery_id)
        rewake._RECIPIENT_KEY_CACHE.clear()
        return delivery_id

    def test_a_losing_carrier_leaves_an_undelivered_gate_for_the_sweep(self):
        from dispatch_session_sweep import sweep_deliver, ack_delivered
        self._open_row()
        self._close_and_materialize()
        claimed, _ = sweep_deliver(self.root, "claude-parent-runtime", "session-1")
        self.assertEqual(len(claimed), 1)
        gate_id = self._gate()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, out, err = self._run_main()
        self.assertEqual((code, out, err), (0, "", ""))
        gate = rewake.pending_delivery.read(self.root, "session-1", gate_id)
        self.assertEqual(gate["state"], "pending")   # untouched, not acked
        ack_delivered(self.root, "session-1", claimed, acked_by="test-sweep")
        later, _ = sweep_deliver(self.root, "claude-parent-runtime", "session-1")
        self.assertEqual([r["delivery_id"] for r in later], [gate_id])

    def _run_main_patched(self, **patches):
        """`_run_main` pins the wait to `ready`; these cases need the failure
        states, so they drive `main` themselves."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake.sys, "stdout", out), \
             mock.patch.object(rewake.sys, "stderr", err), \
             mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            stack = [mock.patch.object(rewake, key, value) for key, value in patches.items()]
            for patch in stack:
                patch.start()
            try:
                code = rewake.main()
            finally:
                for patch in stack:
                    patch.stop()
        return code, out.getvalue(), err.getvalue()

    def test_another_owners_gate_never_ends_a_non_terminal_wait(self):
        # Top review R2-M1: a timeout or a missing readiness helper lapses the
        # claim; folding in someone else's gate changes only what the receipt
        # displays, never whether this owner finished.
        for label, patches in (
            ("timeout", {"wait_for_attempt": mock.Mock(return_value=("timeout", "owner-not-quiescent"))}),
            ("no-helper", {"agent_home": mock.Mock(return_value=self.root / "no-such-home")}),
        ):
            with self.subTest(label=label):
                self._open_row()                      # still open: the owner never finished
                self._gate(f"delivery-foreign-{label}", owner="att-someone-else")
                code, _out, err = self._run_main_patched(**patches)
                self.assertEqual(code, 2)             # the gate is worth waking for
                self.assertIn("required_action=human-gate:frame-review", err)
                ledger = rewake._read_arm(rewake.arm_path(self.jobs, "att-owner-1"))
                self.assertEqual(ledger["state"], "lapsed")   # not `ended`
                rewake.arm_path(self.jobs, "att-owner-1").unlink()

        # and with no foreign gate the same failures lapse exactly as before
        self._open_row()
        code, _out, _err = self._run_main_patched(
            wait_for_attempt=mock.Mock(return_value=("timeout", "owner-not-quiescent")))
        self.assertEqual(code, 0)
        self.assertEqual(rewake._read_arm(rewake.arm_path(self.jobs, "att-owner-1"))["state"], "lapsed")

    def test_a_winning_carrier_keeps_a_folded_gate_ambiguous_after_output(self):
        self._open_row()
        self._close_and_materialize()
        gate_id = self._gate()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY):
            code, out, err = self._run_main()
        self.assertEqual(code, 2)
        self.assertIn("required_action=human-gate:frame-review", err)
        gate = rewake.pending_delivery.read(self.root, "session-1", gate_id)
        self.assertEqual(gate["state"], "sent-ambiguous")
        from dispatch_recipient_receipt import receipt_marker
        self.assertIn(receipt_marker(gate, "session-1"), err)
        # and when the emit itself fails, the gate stays retryable
        rewake.arm_path(self.jobs, "att-owner-1").unlink()   # a fresh claim for the second run
        self._open_row()
        self._close_and_materialize()
        gate_id = self._gate("delivery-independent-gate-2")
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY), \
             mock.patch.object(rewake, "emit_receipt", side_effect=RuntimeError("no emit")):
            with self.assertRaises(RuntimeError):
                self._run_main()
        self.assertEqual(rewake.pending_delivery.read(self.root, "session-1", gate_id)["state"], "sent-ambiguous")


class TransientAttentionCompletionTest(unittest.TestCase):
    """A transient `attention`/`ready` readout while the exact owner row is
    still open must not spend the arm's one wake or seal it `ended`: the real
    completion still has to wake the session. The fixtures are borrowed from
    the claim-gate suite so this class adds no inherited tests."""

    ANCESTRY = CarrierOneClaimGateTest.ANCESTRY
    setUp = CarrierOneClaimGateTest.setUp
    payload = CarrierOneClaimGateTest.payload
    _open_row = CarrierOneClaimGateTest._open_row
    _close_and_materialize = CarrierOneClaimGateTest._close_and_materialize

    def setUp(self) -> None:  # noqa: F811 -- extends the borrowed fixture
        CarrierOneClaimGateTest.setUp(self)
        env = mock.patch.dict(os.environ, {
            "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
            "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "5",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = 0
        self.on_sleep = None
        self.offset = 0.0
        real_monotonic = time.monotonic

        def fake_sleep(seconds):
            self.sleeps += 1
            self.offset += seconds
            if self.on_sleep is not None:
                self.on_sleep(self.sleeps)

        for patch in (
            mock.patch.object(rewake.time, "sleep", fake_sleep),
            mock.patch.object(rewake.time, "monotonic", lambda: real_monotonic() + self.offset),
            mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, waits):
        stdout, stderr = io.StringIO(), io.StringIO()
        wait = mock.Mock(side_effect=list(waits) if isinstance(waits, list) else None,
                         return_value=None if isinstance(waits, list) else waits)
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(self.payload()))), \
             mock.patch.object(rewake.sys, "stdout", stdout), \
             mock.patch.object(rewake.sys, "stderr", stderr), \
             mock.patch.object(rewake, "wait_for_attempt", wait):
            code = rewake.main()
        return code, stdout.getvalue(), stderr.getvalue(), wait

    def _arm_state(self):
        return rewake._read_arm(rewake.arm_path(self.jobs, "att-owner-1"))["state"]

    def test_open_attention_never_seals_arm_ended(self):
        self._open_row()
        attention = ("attention", "terminal-failure-or-unclosed")
        code, out, err, wait = self._run(attention)
        # The owner never finished: keep waiting on the original deadline, then
        # lapse with no completion notice, no claim, and no `ended` seal.
        self.assertNotEqual(self._arm_state(), "ended")
        self.assertEqual(self._arm_state(), "lapsed")
        self.assertEqual(code, 0)
        self.assertNotIn("state=attention", out + err)
        self.assertGreater(wait.call_count, 1)
        self.assertGreaterEqual(self.sleeps, 1)
        self.assertLessEqual(self.sleeps, 6)   # bounded by the original deadline

    def test_ready_snapshot_races_back_to_open_keeps_waiting(self):
        # Readiness said terminal-quiescent, but the current delivery snapshot
        # reads the row open again.
        self._open_row()
        code, out, err, wait = self._run(("ready", "terminal-quiescent"))
        self.assertEqual(self._arm_state(), "lapsed")
        self.assertEqual(code, 0)
        self.assertNotIn("state=success", out + err)
        self.assertGreater(wait.call_count, 1)

    def test_transient_attention_then_terminal_wakes_once(self):
        self._open_row()
        record_path = []

        def finish(count):
            if count == 2 and not record_path:
                record_path.append(self._close_and_materialize())

        self.on_sleep = finish
        attention = ("attention", "terminal-failure-or-unclosed")
        code, out, err, wait = self._run(attention)
        self.assertEqual(code, 2)
        self.assertEqual(out.count('"systemMessage"'), 1)   # exactly one wake
        self.assertIn("status=done", out)
        self.assertEqual(self._arm_state(), "ended")
        self.assertEqual(json.loads(record_path[0].read_text(encoding="utf-8"))["state"], "sent-ambiguous")
        self.assertGreaterEqual(wait.call_count, 3)

    def test_terminal_failure_still_wakes_and_ends(self):
        self._open_row()
        record_path = self._close_and_materialize()
        code, out, err, wait = self._run(("attention", "terminal-failure-or-unclosed"))
        self.assertEqual(code, 2)
        self.assertIn("Hearting dispatch requires attention", err)
        self.assertIn("status=done", out)
        self.assertEqual(self._arm_state(), "ended")
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(self.sleeps, 0)
        self.assertEqual(json.loads(record_path.read_text(encoding="utf-8"))["state"], "sent-ambiguous")

    def test_unreadable_evidence_is_never_sealed_ended(self):
        self._open_row()
        broken = mock.Mock(side_effect=rewake.DispatchContractError("snapshot-unreadable"))
        with mock.patch.object(rewake, "current_delivery_state", broken):
            code, out, err, wait = self._run(("attention", "terminal-failure-or-unclosed"))
        self.assertEqual(self._arm_state(), "lapsed")   # the notice may go out, the arm stays re-armable
        self.assertNotEqual(self._arm_state(), "ended")

    def test_an_open_gate_still_wakes_while_the_owner_is_open(self):
        self._open_row()

        def notices(launch, *, announced, **_kwargs):
            announced.append("delivery-gate-1")
            return ["gate notice"]

        with mock.patch.object(rewake, "_open_gate_pending", return_value=True), \
             mock.patch.object(rewake, "_gate_notices", side_effect=notices):
            code, out, err, wait = self._run(("attention", "terminal-failure-or-unclosed"))
        self.assertEqual(code, 2)
        self.assertEqual(self._arm_state(), "gate-wake-sent")


class CarrierExitRecordTest(unittest.TestCase):
    def setUp(self):
        self.fixture = LosingCarrierLeavesGatesTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = rewake.arm_path(self.fixture.jobs, "att-owner-1")

    def rows(self):
        return [json.loads(line) for line in self.path.with_suffix(".exits.jsonl").read_text().splitlines()]

    def test_normal_wake_keeps_original_exit_two_and_records_reason(self):
        self.fixture._open_row()
        self.fixture._close_and_materialize()
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.fixture.ANCESTRY):
            code, out, err = self.fixture._run_main()
        self.assertEqual(code, 2)
        self.assertIn("Hearting dispatch requires attention", err)
        self.assertTrue(out)
        row, = self.rows()
        self.assertEqual((row["wait_state"], row["reason"], row["exit_code"], row["exit_text"]),
                         ("ready", "terminal-quiescent", 2, "2"))
        self.assertEqual(row["arms"], 1)
        self.assertEqual(row["holder"], rewake._read_arm(self.path)["holder"])
        self.assertIsNotNone(datetime.fromisoformat(row["ended_at"]).tzinfo)

    def test_timeout_and_bridge_error_keep_raw_reason_and_existing_outcome(self):
        for wait_state in ("timeout", "bridge-error"):
            with self.subTest(wait_state=wait_state):
                self.fixture._open_row()
                code, _out, _err = self.fixture._run_main_patched(
                    wait_for_attempt=mock.Mock(return_value=(wait_state, "original probe detail")))
                row = self.rows()[-1]
                self.assertEqual((row["wait_state"], row["reason"], row["exit_code"]),
                                 (wait_state, "original probe detail", code))
                self.assertEqual(rewake._read_arm(self.path)["state"], "lapsed")
                self.path.unlink()

    def test_old_carrier_and_rearm_append_without_overwriting_current_arm(self):
        self.path.parent.mkdir()
        self.path.write_text('current successor arm\n')
        before = self.path.read_bytes()
        first = rewake.ArmClaim(self.path, "att-owner-1", "session-1", 1, ("7", "8", "ns"))
        second = replace(first, arms=2, holder=("9", "10", "ns"))
        rewake._record_carrier_exit(first, {"reason": "first", "exit_code": 0})
        first_bytes = self.path.with_suffix(".exits.jsonl").read_bytes()
        rewake._record_carrier_exit(second, {"reason": "second", "exit_code": 2})
        self.assertTrue(self.path.with_suffix(".exits.jsonl").read_bytes().startswith(first_bytes))
        self.assertEqual([(r["arms"], r["holder"][0]) for r in self.rows()], [(1, "7"), (2, "9")])
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_replacements_record_all_earlier_arms(self):
        self.path.parent.mkdir()
        first = rewake.ArmClaim(self.path, "att-owner-1", "session-1", 1, ("7", "8", "ns"))
        second = replace(first, path=self.path.with_name("att-owner-2.json"),
                         attempt_id="att-owner-2", predecessors=(first,))
        third = replace(first, path=self.path.with_name("att-owner-3.json"),
                        attempt_id="att-owner-3", predecessors=(second,))
        for claim in (first, second, third):
            claim.path.write_text("original arm")
        with mock.patch.object(rewake, "_run_carrier", return_value=2):
            self.assertEqual(rewake._observe_carrier(
                rewake.Launch(third.attempt_id, self.fixture.jobs, third.session_id), third, {}), 2)
        for claim in (first, second, third):
            row, = [json.loads(line) for line in claim.path.with_suffix(".exits.jsonl").read_text().splitlines()]
            self.assertEqual((row["attempt_id"], row["exit_code"]), (claim.attempt_id, 2))
            self.assertEqual(claim.path.read_text(), "original arm")

    def test_exception_is_recorded_and_still_raised(self):
        self.path.parent.mkdir()
        claim = rewake.ArmClaim(self.path, "att-owner-1", "session-1", 1, ("7", "8", "ns"))
        with mock.patch.object(rewake, "_run_carrier", side_effect=RuntimeError("raw failure")):
            with self.assertRaisesRegex(RuntimeError, "raw failure"):
                rewake._observe_carrier(rewake.Launch(claim.attempt_id, self.fixture.jobs, claim.session_id), claim, {})
        row, = self.rows()
        self.assertEqual((row["exit_kind"], row["reason"], row["exit_code"], row["exit_text"]),
                         ("exception", "exception:RuntimeError", 1, "raw failure"))

    def test_diagnostic_write_failure_never_changes_return_or_foreign_file(self):
        self.path.parent.mkdir()
        foreign = self.fixture.root / "foreign"
        foreign.write_text("keep me")
        self.path.with_suffix(".exits.jsonl").symlink_to(foreign)
        claim = rewake.ArmClaim(self.path, "att-owner-1", "session-1", 1, ("7", "8", "ns"))
        with mock.patch.object(rewake, "_run_carrier", return_value=2):
            self.assertEqual(rewake._observe_carrier(rewake.Launch(claim.attempt_id, self.fixture.jobs, claim.session_id), claim, {}), 2)
        self.assertEqual(foreign.read_text(), "keep me")

    def test_append_failure_preserves_completed_claim_and_folded_gate_recovery(self):
        self.fixture._open_row()
        record_path = self.fixture._close_and_materialize()
        gate_id = self.fixture._gate()
        self.path.parent.mkdir(exist_ok=True)
        self.path.with_suffix(".exits.jsonl").symlink_to(self.fixture.root / "missing-target")
        with mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.fixture.ANCESTRY):
            code, out, err = self.fixture._run_main()
        self.assertEqual(code, 2)
        self.assertTrue(out)
        self.assertIn("required_action=human-gate:frame-review", err)
        self.assertEqual(json.loads(record_path.read_text())["state"], "sent-ambiguous")
        gate = rewake.pending_delivery.read(self.fixture.root, "session-1", gate_id)
        self.assertEqual(gate["state"], "sent-ambiguous")
        from dispatch_recipient_receipt import receipt_marker
        self.assertIn(receipt_marker(gate, "session-1"), err)
        self.assertEqual(rewake._read_arm(self.path)["state"], "ended")

    def test_term_and_hup_record_then_keep_native_signal_exit(self):
        import select
        import signal
        import subprocess
        script = '''import importlib.util, pathlib, signal, sys
spec = importlib.util.spec_from_file_location("carrier_fixture", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
path = pathlib.Path(sys.argv[2])
claim = mod.ArmClaim(path, "att-owner-1", "session-1", 1, ("7", "8", "ns"))
def wait(*args):
    print("ready", flush=True)
    signal.pause()
mod._run_carrier = wait
mod._observe_carrier(mod.Launch(claim.attempt_id, path.parent.parent / "jobs.log", claim.session_id), claim, {})
'''
        self.path.parent.mkdir()
        self.path.write_text("original arm")
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                proc = subprocess.Popen([sys.executable, "-c", script, str(MODULE_PATH), str(self.path)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
                    self.assertEqual(proc.stdout.readline().strip(), "ready")
                    proc.send_signal(sig)
                    out, err = proc.communicate(timeout=5)
                    self.assertEqual((proc.returncode, out, err), (-sig, "", ""))
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                row = self.rows()[-1]
                self.assertEqual((row["exit_kind"], row["reason"], row["exit_code"]),
                                 ("signal", f"signal:{sig.name}", -sig))
                self.assertEqual(self.path.read_text(), "original arm")


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
            "fallback_hop=same-harness-headless,worker_type=owner,launch_claimed=1,launch_started=1,"
            f"attempt_id={self.ATTEMPT},parent_attempt_id=att-a12-parent,"
            "parent_completion_delivery=claude-parent-runtime,parent_sid=session-a12,"
            "route_id=rt-a12-fixture,route_node=report,harness=claude"
        )
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            f"{stamp}\topen\t/repo\t/wt\towner\t{pipe}\n", encoding="utf-8"
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

    def test_condition_3_unrecognized_launcher_now_arms_from_the_receipt(self):
        # Pre-2026-09-09 this condition was "zero carrier": the hook did not
        # recognize `bash -c` as an owner-start command. The receipt is the
        # identity now, so the same call arms and the wake is delivered;
        # the row still closes and materializes independently (§4.4 -- the
        # hook never materializes, see the static import test below).
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
        with mock.patch.object(rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")) as wait, \
             mock.patch.object(rewake, "_owner_status", return_value="done"), \
             mock.patch.dict(os.environ, {"AGENT_HOME": str(MODULE_PATH.parents[1])}):
            code, stdout, stderr = self._run_main_with(payload, env={"AGENT_DISPATCH_JOBS": str(self.jobs)})
        self.assertEqual(code, 2)
        self.assertEqual(wait.call_args.args[0].attempt_id, self.ATTEMPT)
        self.assertIn("armed=stdout", stderr)
        ledger = json.loads(rewake.arm_path(self.jobs, self.ATTEMPT).read_text(encoding="utf-8"))
        self.assertEqual(ledger["state"], "ended")
        self._terminal_edge_and_recover()


class RegistryCanonicalJobsFallbackTest(RegistryConfirmArmTest):
    """An unexpanded `--jobs "$J"` plus no AGENT_DISPATCH_JOBS still arms from the
    canonical registry the wrapper actually wrote (2026-08-26 incident)."""

    def setUp(self) -> None:
        super().setUp()
        self.environment.stop()
        # This subclass removes only the registry env var; it still needs the
        # checkout's real readiness helper under isolated `env -i` runs.
        self.no_env = mock.patch.dict(
            os.environ, {"AGENT_HOME": str(self.agent_home)}, clear=False
        )
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
        launch = self.arm()
        assert launch is not None
        self.assertEqual(launch.attempt_id, "att-owner-1")
        self.assertEqual(launch.jobs, self.jobs)
        self.assertEqual(launch.armed, "registry")

    def test_without_canonical_registry_it_still_never_arms(self) -> None:
        self.canonical.stop()
        with mock.patch.object(rewake, "_canonical_jobs", return_value=None):
            self.assertIsNone(rewake.registry_launch(self.payload()))
        self.canonical.start()


class GateCarrierTest(unittest.TestCase):
    """SD-123 (8)(b) carrier 1. This file had zero references to `human-gate`
    before, which is exactly why the missing `now_ns` in `_gate_notices`'
    `reclaim` call shipped: nothing here ever executed that branch."""

    def test_supervision_wake_type_does_not_depend_on_rendered_words(self):
        from dispatch_session_sweep import is_human_gate_record
        for reason in ("workflow-completion-pending", "closure-blocked", "watch-deadline",
                       "receiver-unavailable", "terminal-evidence-conflict"):
            record = {"receipt": {"kind": "supervision", "reason": reason,
                                  "required_action": "inspect-recovery"}}
            notice = "A closure diagnostic whose wording may change."
            text = rewake.gate_wake_message(self._launch(), [notice],
                                             human_gate=is_human_gate_record(record))
            self.assertEqual(text, notice)
            self.assertNotIn("human-gate", text)
            self.assertNotIn("AskUserQuestion", text)
        gate = {"receipt": {"children": [{"required_action": "human-gate:frame-review"}]}}
        text = rewake.gate_wake_message(self._launch(), [notice, "actual gate"],
                                         human_gate=is_human_gate_record(gate))
        self.assertIn("AskUserQuestion", text)
        self.assertIn(notice, text)

    def setUp(self) -> None:
        CarrierOneClaimGateTest.setUp(self)
        self.state = self.root / "dispatch"
        self.state.mkdir()
        self.jobs = self.state / "jobs.log"
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.state)
        # `_gate_notices` finds the recipient through the owner row's `parent_sid`,
        # so an empty registry means it returns [] and exercises nothing.
        meta = ",".join([
            "attempt_schema_version=2", "dispatch_depth=1", "transport=headless",
            "execution_surface=registered-headless", "registered_worker=1",
            "worker_type=owner", "launch_claimed=1", "launch_started=1",
            "attempt_id=att-gate-owner", "parent_sid=session-gate",
            "parent_completion_delivery=claude-parent-runtime",
            "route_id=rt-gate", "route_node=frame", "harness=claude",
        ])
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs.write_text(
            "\t".join([stamp, "open", "/repo", "/wt", "owner", meta])
            + "\n",
            encoding="utf-8",
        )

    def _gate_record(self, *, delivery_id="delivery-gate-0001", state=None):
        rewake._PROBE_DIRECTORY_MTIME.clear()
        rewake._PROBE_NEXT_DEADLINE_NS.clear()
        rewake._RECIPIENT_KEY_CACHE.clear()
        receipt = {
            "schema_version": 2, "state": "attention",
            "parent_attempt_id": "att-gate-owner", "job_registry": str(self.jobs),
            "delivery_classification": "attention",
            "children": [{
                "attempt_id": "att-gate-owner", "status": "open",
                "readiness": "human-gate", "reason": str(self.root / "frame-summary.json"),
                "required_action": "human-gate:frame-review", "harness": "claude",
                "delivery_classification": "attention",
            }],
        }
        rewake.pending_delivery.create(
            self.state,
            recipient_kind="claude-parent-runtime", recipient_key="session-gate",
            delivery_id=delivery_id, session_generation="unsupported",
            session_generation_supported="0", attempt_ids=["att-gate-owner"],
            parent_attempt_id="att-gate-owner", route_id="rt-gate",
            route_node="frame", receipt=receipt,
            receipt_digest=rewake.pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={"att-gate-owner": "human-gate:frame-review"},
        )
        _gate_authority(self.jobs, "session-gate", delivery_id)
        if state in {"claimed", "sent-ambiguous"}:
            rewake.pending_delivery.claim(
                self.state, "session-gate", delivery_id,
                claim_owner="someone-else", lease_seconds=0.001,
            )
            if state == "sent-ambiguous":
                rewake.pending_delivery.mark_sent_ambiguous(
                    self.state, "session-gate", delivery_id,
                    claim_owner="someone-else",
                )
        return delivery_id

    def test_a_gate_record_is_recognised_as_one(self):
        record = json.loads(
            rewake.pending_delivery.record_path(
                self.state, "session-gate", self._gate_record()
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(rewake.is_human_gate_record(record))

    def _launch(self):
        return rewake.Launch(
            attempt_id="att-gate-owner", jobs=self.jobs, session_id="session-gate",
        )

    def test_gate_notices_surfaces_an_open_gate(self):
        self._gate_record()
        notices = rewake._gate_notices(self._launch())
        self.assertEqual(len(notices), 1)
        self.assertIn("human-gate:frame-review", notices[0])

    def test_supervision_is_never_introduced_as_a_human_gate(self):
        notice = "Hearting supervision needs attention. reason=supervisor-exited required_action=inspect-recovery"
        with mock.patch.object(rewake, "_gate_notices", return_value=[notice]), \
                mock.patch.object(rewake, "emit_receipt", return_value=2) as emit, \
                mock.patch.object(rewake, "_ended", return_value=2):
            rewake._emit_with_gates(self._launch(), None, "success", "Finished.", block=False)
        message = emit.call_args.args[1]
        self.assertIn(notice, message)
        self.assertNotIn("human gate", message)
        self.assertNotIn("your decision", message)

    def test_an_answer_awaiting_this_parent_is_started_by_the_wake_that_carries_it(self):
        # D1: the parent's wake schedules a start handoff; the existing start
        # checks remain authoritative and the notice does not claim launch.
        import dispatch_supervision as supervision
        kept = [{"id": "input-1", "digest": "d1", "text": "use the smaller batch"}]
        armed = []
        with mock.patch("dispatch_owner_input.blocked_owner_answers", return_value=kept), \
             mock.patch("dispatch_replacement._replacement_in_flight", return_value=False), \
             mock.patch("dispatch_replacement._route", return_value=(self.root / "route.json", {"route_id": "rt-gate"})), \
             mock.patch("capacity_auto_resume.arm", side_effect=lambda result, route_file, jobs, environ:
                        armed.append(environ) or {"state": "armed", "cause": "answer"}):
            supervision.materialize(self.jobs, {"att-gate-owner"}, reason=supervision.ANSWER_AWAITING_PARENT)
            rewake._RECIPIENT_KEY_CACHE.clear()
            notices = rewake._gate_notices(self._launch())
        self.assertEqual(len(notices), 1)
        self.assertIn("scheduled the route's start under the parent's identity", notices[0])
        self.assertIn("existing launch checks still decide whether a new owner starts", notices[0])
        self.assertIn("This is a handoff, not completion", notices[0])
        self.assertNotIn("started the route's continuation", notices[0])
        self.assertEqual([env["AGENT_DISPATCH_CALLER_HARNESS"] for env in armed], ["claude"])

    def test_gate_notices_survives_an_expired_claim(self):
        """The regression, executed rather than inspected. `reclaim` is
        keyword-only in `now_ns`; omitting it raised TypeError, which is not a
        `PendingDeliveryError`, so it escaped the surrounding except and took the
        whole hook down — the parent then got no completion receipt at all, not
        merely no gate. Reached when a sweep claimed the record first, or after
        `mark_sent_ambiguous`."""
        for state in ("claimed", "sent-ambiguous"):
            with self.subTest(state=state):
                self._gate_record(delivery_id=f"delivery-gate-{state[:4]}",
                                  state=state)
                time.sleep(0.01)  # let the 1 ms lease lapse
                notices = rewake._gate_notices(self._launch())
                self.assertTrue(notices, "the hook produced no notice")
                self.assertTrue(any("human-gate:frame-review" in n for n in notices))

    def test_a_live_claim_is_left_to_its_holder(self):
        """The other half of the same branch: an unexpired lease belongs to
        whoever holds it, so this carrier stays quiet rather than reclaiming."""
        delivery_id = self._gate_record(delivery_id="delivery-gate-live")
        rewake.pending_delivery.claim(
            self.state, "session-gate", delivery_id,
            claim_owner="the-sweep", lease_seconds=120.0,
        )
        self.assertEqual(rewake._gate_notices(self._launch()), [])
        record = json.loads(
            rewake.pending_delivery.record_path(
                self.state, "session-gate", delivery_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(record["claim_owner"], "the-sweep")

    def test_reclaim_is_keyword_only_so_the_omission_was_a_type_error(self):
        """Why the omission was fatal rather than a refusal: the signature makes
        it a TypeError, and only `PendingDeliveryError` was being caught."""
        signature = inspect.signature(rewake.pending_delivery.reclaim)
        self.assertEqual(signature.parameters["now_ns"].kind,
                         inspect.Parameter.KEYWORD_ONLY)
        with self.assertRaises(TypeError):
            rewake.pending_delivery.reclaim(self.state, "session-gate", "delivery-x")

    # -- SD-129: the gate wakes the person while the owner is still alive ------

    def test_the_wait_ends_on_an_open_gate_for_this_attempt(self):
        self._gate_record()
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        with mock.patch.object(rewake.subprocess, "run", return_value=pending), \
                mock.patch.object(rewake.time, "sleep") as sleep:
            state, reason = rewake.wait_for_attempt(
                self._launch(), self.root / "ready.py",
                gate_probe=lambda: rewake._open_gate_pending(self._launch()),
            )
        self.assertEqual((state, reason), ("gate", "human-gate-open"))
        sleep.assert_not_called()

    def test_another_attempts_gate_does_not_end_this_wait(self):
        self._gate_record()
        other = rewake.Launch(attempt_id="att-other-owner", jobs=self.jobs,
                              session_id="session-gate")
        # The registry row is the gate owner's; `current_attempt_row` for the
        # other attempt finds no row, so the probe sees nothing.
        self.assertFalse(rewake._open_gate_pending(other))
        self.assertTrue(rewake._open_gate_pending(self._launch()))

    def test_a_record_already_taken_by_the_sweep_is_not_probed_open(self):
        delivery_id = self._gate_record()
        rewake.pending_delivery.claim(
            self.state, "session-gate", delivery_id,
            claim_owner="session-sweep:claude-parent-runtime:1:1", lease_seconds=120.0,
        )
        self.assertFalse(rewake._open_gate_pending(self._launch()))
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id,
                                    acked_by="session-sweep:x")
        self.assertFalse(rewake._open_gate_pending(self._launch()))
        self.assertEqual(rewake._gate_notices(self._launch(), attempt_only=True), [])

    def test_attempt_only_notices_skip_a_foreign_owners_gate(self):
        receipt = {
            "schema_version": 2, "state": "attention",
            "parent_attempt_id": "att-foreign", "job_registry": str(self.jobs),
            "delivery_classification": "attention",
            "children": [{
                "attempt_id": "att-foreign", "status": "open",
                "readiness": "human-gate", "reason": str(self.root / "foreign.json"),
                "required_action": "human-gate:frame-review", "harness": "claude",
                "delivery_classification": "attention",
            }],
        }
        rewake.pending_delivery.create(
            self.state, recipient_kind="claude-parent-runtime", recipient_key="session-gate",
            delivery_id="delivery-foreign-gate", session_generation="unsupported",
            session_generation_supported="0", attempt_ids=["att-foreign"],
            parent_attempt_id="att-foreign", route_id="rt-foreign", route_node="frame",
            receipt=receipt,
            receipt_digest=rewake.pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={"att-foreign": "human-gate:frame-review"},
        )
        _gate_authority(self.jobs, "session-gate", "delivery-foreign-gate")
        self.assertEqual(rewake._gate_notices(self._launch(), attempt_only=True), [])
        # the terminal wake still folds every open gate for the recipient in
        self.assertEqual(len(rewake._gate_notices(self._launch())), 1)

    def test_the_probe_rescans_only_on_a_directory_write_or_an_expired_lease(self):
        """review round 2, N1 / N4: the mtime gate is exercised, not cleared."""
        delivery_id = self._gate_record()
        launch = self._launch()
        self.assertTrue(rewake._open_gate_pending(launch))          # first scan: pending
        with mock.patch.object(rewake, "_recipient_gate_records") as scan:
            self.assertFalse(rewake._open_gate_pending(launch))     # no write: no scan
            scan.assert_not_called()
        claimed = rewake.pending_delivery.claim(self.state, "session-gate", delivery_id,
                                                claim_owner="session-sweep:x:1:1",
                                                lease_seconds=120.0)
        self.assertFalse(rewake._open_gate_pending(launch))         # write: scanned, lease live
        # no directory write since, but the lease seen at the last scan expired;
        # the clock is stepped past the deadline, not slept past a short lease
        # that a stalled claim fsync can outlive before the scan above runs
        with mock.patch.object(rewake.time, "monotonic_ns",
                               return_value=claimed["claim_deadline_ns"] + 1):
            self.assertTrue(rewake._open_gate_pending(launch))
        # the registry was read once for the whole sequence
        with mock.patch.object(rewake, "current_attempt_row") as row:
            rewake._open_gate_pending(launch)
            row.assert_not_called()

    def test_repeated_claim_failure_still_wakes_once_when_delivery_recovers(self):
        delivery_id = self._gate_record()
        for _ in range(10):
            rewake.pending_delivery.claim(self.state, "session-gate", delivery_id,
                                          claim_owner="x", lease_seconds=0.001)
            rewake.pending_delivery.reclaim(self.state, "session-gate", delivery_id,
                                            now_ns=time.monotonic_ns() + 10**12)
        rewake._PROBE_DIRECTORY_MTIME.clear()
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "session-gate",
            "tool_input": {"command": "python3 utilities/dispatch-owner.py --start --slug owner"},
            "tool_response": {"stdout": "\n".join((
                "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_session_id=session-gate", f"job_registry={self.jobs}",
                "attempt_id=att-gate-owner", "registered=1", "started=1")), "stderr": ""},
        }
        (self.root / "utilities").mkdir()
        (self.root / "utilities" / "dispatch-attempt-ready.py").write_text("", encoding="utf-8")
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        clock = {"t": 0.0}

        def monotonic():
            clock["t"] += 1.0
            return clock["t"]

        with mock.patch.object(rewake.subprocess, "run", return_value=pending) as run, \
                mock.patch.object(rewake, "agent_home", return_value=self.root), \
                mock.patch.object(rewake.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(rewake.time, "sleep") as sleep, \
                mock.patch.dict(os.environ, {"AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
                                             "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "6"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()) as stderr:
            code = rewake.main()
        self.assertEqual(code, 2)
        self.assertIn("owner=waiting-or-parked", stderr.getvalue())
        self.assertLessEqual(run.call_count, 1)

    def test_probe_recovers_expired_claims_without_an_eight_attempt_dead_end(self):
        delivery_id = self._gate_record()
        for _ in range(10):
            rewake.pending_delivery.claim(self.state, "session-gate", delivery_id,
                                          claim_owner="x", lease_seconds=0.001)
            rewake.pending_delivery.reclaim(self.state, "session-gate", delivery_id,
                                            now_ns=time.monotonic_ns() + 10**12)
        record = rewake.pending_delivery.read(self.state, "session-gate", delivery_id)
        self.assertEqual(record["attempts"], 10)
        rewake._PROBE_DIRECTORY_MTIME.clear()
        self.assertTrue(rewake._open_gate_pending(self._launch()))

    def test_an_unannounced_probe_sleeps_and_the_wait_stays_bounded(self):
        """review round 1, B3: when the probe fires but nothing can be announced the
        loop sleeps one interval, and the overall deadline is the caller's."""
        self._gate_record()
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "session-gate",
            "tool_input": {"command": "python3 utilities/dispatch-owner.py --start --slug owner"},
            "tool_response": {"stdout": "\n".join((
                "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_session_id=session-gate", f"job_registry={self.jobs}",
                "attempt_id=att-gate-owner", "registered=1", "started=1")), "stderr": ""},
        }
        (self.root / "utilities").mkdir()
        (self.root / "utilities" / "dispatch-attempt-ready.py").write_text("", encoding="utf-8")
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        clock = {"t": 0.0}

        def monotonic():
            clock["t"] += 1.0
            return clock["t"]

        with mock.patch.object(rewake.subprocess, "run", return_value=pending) as run, \
                mock.patch.object(rewake, "_open_gate_pending", return_value=True), \
                mock.patch.object(rewake, "_gate_notices", return_value=[]), \
                mock.patch.object(rewake, "agent_home", return_value=self.root), \
                mock.patch.object(rewake.time, "monotonic", side_effect=monotonic), \
                mock.patch.object(rewake.time, "sleep") as sleep, \
                mock.patch.dict(os.environ, {"AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
                                             "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "6"}), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            code = rewake.main()
        self.assertEqual(code, 0)              # timeout: a non-terminal bridge state
        # one sleep per empty announce; the last probe hits the deadline and
        # returns timeout without sleeping
        self.assertEqual(sleep.call_count, run.call_count - 1)
        self.assertLessEqual(run.call_count, 8)              # bounded by the deadline

    def test_main_wakes_once_on_the_gate_and_leaves_the_record_for_the_sweep(self):
        delivery_id = self._gate_record()
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "session-gate",
            "tool_input": {"command": "python3 utilities/dispatch-owner.py --start --slug owner"},
            "tool_response": {"stdout": "\n".join((
                "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_session_id=session-gate", f"job_registry={self.jobs}",
                "attempt_id=att-gate-owner", "registered=1", "started=1")), "stderr": ""},
        }
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        stdout, stderr = io.StringIO(), io.StringIO()
        (self.root / "utilities").mkdir()
        (self.root / "utilities" / "dispatch-attempt-ready.py").write_text("", encoding="utf-8")
        with mock.patch.object(rewake.subprocess, "run", return_value=pending), \
                mock.patch.object(rewake, "agent_home", return_value=self.root), \
                mock.patch.object(rewake.time, "sleep"), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            code = rewake.main()
        self.assertEqual(code, 2)
        text = stderr.getvalue()
        self.assertIn("human gate awaiting your decision", text)
        self.assertIn("owner=waiting-or-parked", text)
        self.assertIn("or has paused at the gate", text)
        self.assertIn("human-gate:frame-review", text)
        self.assertIn("await-release", text)
        self.assertIn("AskUserQuestion", text)
        self.assertNotIn("harvest --jobs", text)
        # review round 1, M6: the in-wait wake is speculative, so the record is
        # left `sent-ambiguous` -- the next-prompt sweep can still deliver it if
        # this wake was lost, and the release retires it either way.
        record = rewake.pending_delivery.read(self.state, "session-gate", delivery_id)
        self.assertEqual(record["state"], "sent-ambiguous")
        self.assertTrue(record["claim_owner"].startswith("claude-async-rewake-gate:"))
        rewake._PROBE_DIRECTORY_MTIME.clear()
        self.assertFalse(rewake._open_gate_pending(self._launch()))   # lease still live
        # the spent wake is on the ledger: no Bash call re-arms while this
        # gate record is open, and the first one after it closes does
        ledger = json.loads(rewake.arm_path(self.jobs, "att-gate-owner").read_text(encoding="utf-8"))
        self.assertEqual((ledger["state"], ledger["gate_delivery_id"]), ("gate-wake-sent", delivery_id))
        # the terminal wake still acks
        from dispatch_session_sweep import sweep_deliver
        records, _n = sweep_deliver(self.state, "claude-parent-runtime", "session-gate",
                                    now_ns=time.monotonic_ns() + 10**12)
        self.assertEqual([r["delivery_id"] for r in records], [delivery_id])

    def test_a_probe_raced_by_the_sweep_resumes_waiting_for_the_terminal(self):
        delivery_id = self._gate_record()
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "session-gate",
            "tool_input": {"command": "python3 utilities/dispatch-owner.py --start --slug owner"},
            "tool_response": {"stdout": "\n".join((
                "check=ok", "status=start", "dispatch_depth=1", "worker_type=owner",
                "parent_completion_delivery=claude-parent-runtime",
                "parent_session_id=session-gate", f"job_registry={self.jobs}",
                "attempt_id=att-gate-owner", "registered=1", "started=1")), "stderr": ""},
        }
        (self.root / "utilities").mkdir()
        (self.root / "utilities" / "dispatch-attempt-ready.py").write_text("", encoding="utf-8")
        pending = subprocess.CompletedProcess([], 2, stdout="pending")
        ready = subprocess.CompletedProcess([], 0, stdout="ready")

        def sweep_takes_it():
            rewake.pending_delivery.claim(self.state, "session-gate", delivery_id,
                                          claim_owner="session-sweep:x:1:1",
                                          lease_seconds=120.0)
            rewake.pending_delivery.ack(self.state, "session-gate", delivery_id,
                                        acked_by="session-sweep:x")
            return True

        with mock.patch.object(rewake.subprocess, "run", side_effect=[pending, ready]) as run, \
                mock.patch.object(rewake, "_open_gate_pending", side_effect=[sweep_takes_it(), False]), \
                mock.patch.object(rewake, "_owner_status", return_value="done"), \
                mock.patch.object(rewake, "agent_home", return_value=self.root), \
                mock.patch.object(rewake.time, "sleep"), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()) as stderr:
            code = rewake.main()
        # the wait resumed to the terminal receipt: two readiness probes, and
        # the notice on stderr is the ordinary attempt receipt, not a gate wake
        self.assertNotIn("owner=waiting-or-parked", stderr.getvalue())
        self.assertIn("attempt_id=att-gate-owner", stderr.getvalue())
        self.assertIn(code, (0, 2))
        self.assertEqual(run.call_count, 2)


    def test_a_non_gate_record_is_left_to_the_ordinary_path(self):
        receipt = {
            "schema_version": 2, "state": "success",
            "parent_attempt_id": "att-gate-owner", "job_registry": str(self.jobs),
            "delivery_classification": "success",
            "children": [{
                "attempt_id": "att-gate-owner", "status": "done",
                "readiness": "ready", "reason": "terminal-complete",
                "required_action": "advance-completed", "harness": "claude",
                "delivery_classification": "success",
            }],
        }
        rewake.pending_delivery.create(
            self.state, recipient_kind="claude-parent-runtime",
            recipient_key="session-gate", delivery_id="delivery-ordinary-1",
            session_generation="unsupported", session_generation_supported="0",
            attempt_ids=["att-gate-owner"], parent_attempt_id="att-gate-owner",
            route_id="rt-gate", route_node="report", receipt=receipt,
            receipt_digest=rewake.pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={"att-gate-owner": "rev"},
        )
        record = json.loads(
            rewake.pending_delivery.record_path(
                self.state, "session-gate", "delivery-ordinary-1"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(rewake.is_human_gate_record(record))


class SupervisionWakeRearmTest(unittest.TestCase):
    """An attention wake must hand its notice to the immediate prompt sweep,
    so that turn can arm a second carrier for the same owner's success."""

    ANCESTRY = CarrierOneClaimGateTest.ANCESTRY
    payload = CarrierOneClaimGateTest.payload

    def setUp(self):
        CarrierOneClaimGateTest.setUp(self)
        CarrierOneClaimGateTest._open_row(self)
        self.jobs.write_text(self.jobs.read_text().replace(
            "parent_attempt_id=att-owner-parent,", ""), encoding="utf-8")
        rewake._RECIPIENT_KEY_CACHE.clear()
        rewake._PROBE_DIRECTORY_MTIME.clear()
        rewake._PROBE_NEXT_DEADLINE_NS.clear()
        from dispatch_supervision import materialize
        self.notice = materialize(self.jobs, {"att-owner-1"}, reason="join-deadline")[0]

    def _run(self, wait_state, payload=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(rewake.sys, "stdin", io.StringIO(json.dumps(payload or self.payload()))), \
             mock.patch.object(rewake.sys, "stdout", stdout), \
             mock.patch.object(rewake.sys, "stderr", stderr), \
             mock.patch.object(rewake, "runtime_ancestry_binding", return_value=self.ANCESTRY), \
             mock.patch.object(rewake, "wait_for_attempt",
                               side_effect=wait_state if callable(wait_state) else None,
                               return_value=(wait_state, "test-readiness")):
            code = rewake.main()
        return code, stderr.getvalue()

    def _parent_call(self):
        payload = self.payload()
        payload["tool_input"] = {"command": "git status"}
        payload["tool_response"] = {"stdout": "", "stderr": ""}
        return payload

    def _dead_holder(self):
        path = rewake.arm_path(self.jobs, "att-owner-1")
        record = json.loads(path.read_text())
        record["holder"] = DEAD_HOLDER
        path.write_text(json.dumps(record), encoding="utf-8")

    def _complete_owner(self):
        namespace = DEAD_HOLDER[2]
        drained = (
            f",failure_class=pass,pid=4194304,pid_start=1,pgid=4194304,"
            f"pid_ns={namespace},pid_observer_ns={namespace},pid_scope=namespace-local,"
            "launch_lifecycle=detached,launch_outcome=governed-process-group-drained,"
            "group_reap_pgid=4194304,group_reap_proof=pgid-empty-v1,"
            f"attempt_descendant_proof=attempt-tagged-empty-v1,attempt_descendant_observer_ns={namespace}"
        )
        self.jobs.write_text(self.jobs.read_text().rstrip() + drained + "\n", encoding="utf-8")
        self.assertTrue(D.close_attempt_row(self.jobs, "att-owner-1", "completed-supervisor"))
        path = JOIN.materialize_pending_delivery(self.jobs, self.jobs.read_text().splitlines()[0].split("\t"))
        self.assertIsNotNone(path)
        return path

    def test_attention_sweep_rearm_success_without_waiting_for_a_lease(self):
        from dispatch_session_sweep import sweep_deliver, ack_delivered
        code, text = self._run("gate")
        self.assertEqual(code, 2)
        self.assertIn("Hearting supervision needs attention", text)
        self.assertNotIn("human gate", text)
        self._dead_holder()  # the first async hook process exited
        records, _ = sweep_deliver(self.root, "claude-parent-runtime", "session-1")
        self.assertEqual([r["delivery_id"] for r in records], [self.notice["delivery_id"]])
        self.assertEqual(ack_delivered(self.root, "session-1", records, acked_by="parent-prompt"), 1)
        # The ordinary Bash call in that awakened turn arms again while open.
        launch, claim = rewake.registry_launch(self._parent_call())
        self.assertEqual((launch.armed, claim.arms), ("registry-rearm", 2))
        self.assertFalse(rewake._open_gate_pending(launch))
        self._dead_holder()
        completion = self._complete_owner()
        code, text = self._run("ready", self._parent_call())
        self.assertEqual(code, 2)
        self.assertIn("state=success", text)
        self.assertIn("advance-completed", text)
        self.assertEqual(json.loads(completion.read_text())["state"], "sent-ambiguous")
        self.assertEqual(rewake._read_arm(claim.path)["state"], "ended")
        self.assertEqual(rewake.registry_launch(self._parent_call()).reason, "ended")

    def test_native_supervision_receipt_settles_delivery_but_not_open_owner(self):
        import dispatch_recipient_receipt as recipient_receipt
        code, text = self._run("gate")
        self.assertEqual(code, 2)
        record = rewake.pending_delivery.read(
            self.root, "session-1", self.notice["delivery_id"])
        self.assertEqual(record["receipt"]["kind"], "supervision")
        evidence = recipient_receipt.claude_feedback_evidence([{
            "type": "user", "sessionId": "session-1", "summary": "Stop hook feedback",
            "message": {"content": text},
        }], "session-1")
        settled = recipient_receipt.settle_native_receipt(
            self.root, "session-1", "claude", "session-1", record, evidence,
            acked_by="native-receipt:claude:session-1")
        self.assertTrue(settled["received"])
        self.assertTrue(settled["settled"])
        self.assertEqual(rewake.pending_delivery.read(
            self.root, "session-1", self.notice["delivery_id"])["state"], "acked")
        from dispatch_session_sweep import sweep_deliver
        self.assertEqual(sweep_deliver(
            self.root, "claude-parent-runtime", "session-1")[0], [])
        duty, = [item for item in rewake.peer_obligations.ObligationStore(self.root).list()
                 if item["intent"]["identity"].get("attempt_id") == "att-owner-1"]
        self.assertEqual(duty["state"], "pending")
        owner = JOIN.current_attempt_row(self.jobs, "att-owner-1")
        self.assertEqual(owner.status, "open")
        self.assertEqual(json.loads(rewake.arm_path(
            self.jobs, "att-owner-1").read_text())["state"], "gate-wake-sent")

        self._dead_holder()
        launch, claim = rewake.registry_launch(self._parent_call())
        self.assertEqual(launch.armed, "registry-rearm")
        self.assertEqual(claim.arms, 2)
        self.assertEqual(JOIN.current_attempt_row(self.jobs, "att-owner-1").status, "open")
        self._dead_holder()
        completion = self._complete_owner()
        self.assertNotEqual(completion.stem, self.notice["delivery_id"])
        code, completion_text = self._run("ready", self._parent_call())
        self.assertEqual(code, 2)
        self.assertIn("state=success", completion_text)
        completion_record = rewake.pending_delivery.read(
            self.root, "session-1", completion.stem)
        self.assertEqual(completion_record["state"], "sent-ambiguous")
        self.assertIn(recipient_receipt.receipt_marker(completion_record, "session-1"),
                      completion_text)

    def test_supervision_ack_write_failure_keeps_watch_until_native_retry(self):
        import dispatch_recipient_receipt as recipient_receipt
        code, text = self._run("gate")
        self.assertEqual(code, 2)
        did = self.notice["delivery_id"]
        record = rewake.pending_delivery.read(self.root, "session-1", did)
        evidence = recipient_receipt.claude_feedback_evidence([{
            "type": "user", "sessionId": "session-1", "summary": "Stop hook feedback",
            "message": {"content": text},
        }], "session-1")
        with mock.patch.object(rewake.pending_delivery, "ack_received",
                               side_effect=OSError("simulated receipt write failure")):
            first = recipient_receipt.settle_native_receipt(
                self.root, "session-1", "claude", "session-1", record, evidence,
                acked_by="native-receipt:claude:session-1")
        self.assertTrue(first["received"])
        self.assertFalse(first["settled"])
        self.assertEqual(rewake.pending_delivery.read(self.root, "session-1", did)["state"],
                         "pending")
        self.assertEqual(JOIN.current_attempt_row(self.jobs, "att-owner-1").status, "open")
        duty, = [item for item in rewake.peer_obligations.ObligationStore(self.root).list()
                 if item["intent"]["identity"].get("attempt_id") == "att-owner-1"]
        self.assertEqual(duty["state"], "pending")
        self._dead_holder()
        self.assertEqual(rewake.registry_launch(self._parent_call()).reason, "gate-open")

        retry = recipient_receipt.settle_native_receipt(
            self.root, "session-1", "claude", "session-1", record, evidence,
            acked_by="native-receipt:claude:session-1")
        self.assertTrue(retry["received"])
        self.assertTrue(retry["settled"])
        self.assertEqual(rewake.pending_delivery.read(self.root, "session-1", did)["state"],
                         "acked")
        from dispatch_session_sweep import sweep_deliver
        self.assertEqual(sweep_deliver(
            self.root, "claude-parent-runtime", "session-1")[0], [])
        self.assertEqual(JOIN.current_attempt_row(self.jobs, "att-owner-1").status, "open")
        duty, = [item for item in rewake.peer_obligations.ObligationStore(self.root).list()
                 if item["intent"]["identity"].get("attempt_id") == "att-owner-1"]
        self.assertEqual(duty["state"], "pending")

        self._dead_holder()
        launch, claim = rewake.registry_launch(self._parent_call())
        self.assertEqual(launch.armed, "registry-rearm")
        self.assertEqual(claim.arms, 2)
        self._dead_holder()
        completion = self._complete_owner()
        self.assertNotEqual(completion.stem, did)
        code, completion_text = self._run("ready", self._parent_call())
        self.assertEqual(code, 2)
        self.assertIn("state=success", completion_text)
        completion_record = rewake.pending_delivery.read(
            self.root, "session-1", completion.stem)
        self.assertEqual(completion_record["state"], "sent-ambiguous")
        self.assertIn(recipient_receipt.receipt_marker(completion_record, "session-1"),
                      completion_text)

    def test_lost_wake_is_not_acked_and_successor_claim_is_preserved(self):
        pending = rewake.pending_delivery
        delivery_id = self.notice["delivery_id"]

        def other_carrier_claims(*args, **kwargs):
            pending.reclaim(self.root, "session-1", delivery_id, now_ns=time.monotonic_ns() + 10**12)
            pending.claim(self.root, "session-1", delivery_id, claim_owner="successor", lease_seconds=120)
            return 2

        with mock.patch.object(rewake, "emit_receipt", side_effect=other_carrier_claims):
            self.assertEqual(self._run("gate")[0], 2)
        record = pending.read(self.root, "session-1", delivery_id)
        self.assertEqual((record["state"], record["claim_owner"]), ("claimed", "successor"))
        self.assertIsNone(record["acked_by"])

    def test_emit_failure_keeps_the_notice_recoverable(self):
        with mock.patch.object(rewake, "emit_receipt", side_effect=RuntimeError("emit failed")):
            with self.assertRaisesRegex(RuntimeError, "emit failed"):
                self._run("gate")
        record = rewake.pending_delivery.read(self.root, "session-1", self.notice["delivery_id"])
        self.assertEqual(record["state"], "sent-ambiguous")
        self.assertIsNone(record["acked_by"])

    def test_handoff_io_failure_preserves_the_native_wake_and_retryable_notice(self):
        with mock.patch.object(rewake.pending_delivery, "release_claim", side_effect=OSError("disk error")):
            self.assertEqual(self._run("gate")[0], 2)
        record = rewake.pending_delivery.read(self.root, "session-1", self.notice["delivery_id"])
        self.assertEqual(record["state"], "sent-ambiguous")
        self.assertIsNone(record["acked_by"])

    def _wake_and_sweep(self):
        from dispatch_session_sweep import sweep_deliver, ack_delivered
        self.assertEqual(self._run("gate")[0], 2)
        self._dead_holder()
        records, _ = sweep_deliver(self.root, "claude-parent-runtime", "session-1")
        self.assertEqual(ack_delivered(self.root, "session-1", records, acked_by="parent-prompt"), 1)

    def test_stop_keeps_watching_after_an_attention_turn_with_no_bash(self):
        self._wake_and_sweep()
        completion = []

        def finish_during_wait(*args, **kwargs):
            arm = rewake._read_arm(rewake.arm_path(self.jobs, "att-owner-1"))
            self.assertEqual((arm["arms"], arm["state"]), (2, "waiting"))
            completion.append(self._complete_owner())
            return "ready", "terminal-quiescent"

        stop = {"hook_event_name": "Stop", "session_id": "session-1", "stop_hook_active": True}
        code, text = self._run(finish_during_wait, stop)
        self.assertEqual(code, 2)
        self.assertIn("state=success", text)
        self.assertEqual(json.loads(completion[0].read_text())["state"], "sent-ambiguous")
        self.assertEqual(rewake.registry_launch(stop).reason, "ended")

    def test_stop_delivers_a_completion_that_preceded_the_turn_end(self):
        self._wake_and_sweep()
        completion = self._complete_owner()
        stop = {"hook_event_name": "Stop", "session_id": "session-1"}
        self.assertEqual(self._run("ready", stop)[0], 2)
        self.assertEqual(json.loads(completion.read_text())["state"], "sent-ambiguous")

    def test_stop_never_first_arms_or_takes_a_foreign_or_live_arm(self):
        stop = {"hook_event_name": "Stop", "session_id": "session-1"}
        self.assertIsNone(rewake.registry_launch(stop))  # no ledger yet
        claim = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        self.assertEqual(rewake.registry_launch(stop).reason, "held-live")
        self._dead_holder()
        self.assertIsNone(rewake.registry_launch({**stop, "session_id": "foreign"}))
        self.assertEqual(rewake._read_arm(claim.path)["arms"], 1)
        self.assertIsNone(rewake.registry_launch({**stop, "hook_event_name": "SubagentStop"}))

    def test_claude_stop_registers_the_same_async_completion_carrier(self):
        settings = json.loads((ROOT / "adapters/claude/settings.json").read_text())
        by_event = {}
        for event in ("PostToolUse", "Stop"):
            by_event[event] = [hook for entry in settings["hooks"][event]
                               for hook in entry["hooks"]
                               if "dispatch-owner-rewake.py" in hook.get("command", "")]
        self.assertEqual(len(by_event["Stop"]), 1)
        self.assertEqual(by_event["Stop"], by_event["PostToolUse"])
        self.assertTrue(by_event["Stop"][0]["asyncRewake"])


class GateCloseRearmTest(GateCarrierTest):
    """SD-129 without command parsing: the wake spent on a gate is re-armed by
    the first same-session Bash call after that gate record closes -- the
    release retires it (`retire_gate_delivery`), or the next-prompt sweep
    acks it -- whichever command that call happens to be. The release command
    itself is such a call, so "the release is the second arming event" still
    holds; a release from another pane, and SD-OPEN-48's refused-as-already-
    released case (record already closed), are covered by the same rule."""

    def setUp(self) -> None:
        super().setUp()
        self.environment = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def spend_wake_on(self, delivery_id) -> rewake.ArmClaim:
        claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-gate", fresh=True)
        assert isinstance(claim, rewake.ArmClaim)
        self.assertTrue(rewake.settle_arm(claim, "gate-wake-sent", gate_delivery_id=delivery_id))
        path = rewake.arm_path(self.jobs, "att-gate-owner")  # that hook process has exited
        record = json.loads(path.read_text(encoding="utf-8"))
        record["holder"] = DEAD_HOLDER
        path.write_text(json.dumps(record), encoding="utf-8")
        return claim

    def call(self, command="python3 utilities/workflow-supervisor.py release --route r.json --gate frame-review --decision proceed",
             session="session-gate"):
        return {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session,
                "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}}

    def test_no_call_rearms_while_the_announced_gate_is_open(self):
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        for command in ("cat shards/frame/interview.md", "git status",
                        self.call()["tool_input"]["command"]):
            with self.subTest(command=command):
                self.assertNotIsInstance(rewake.registry_launch(self.call(command)), tuple)

    def test_stop_rearms_only_after_the_human_gate_record_closes(self):
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        stop = {"hook_event_name": "Stop", "session_id": "session-gate"}
        self.assertEqual(rewake.registry_launch(stop).reason, "gate-open")
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
        launch, claim = rewake.registry_launch(stop)
        self.assertEqual((launch.attempt_id, claim.arms), ("att-gate-owner", 2))

    def test_the_first_call_after_the_gate_closes_rearms_the_owner(self):
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        # what `retire_gate_delivery` (release) or the prompt sweep does
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
        resolved = rewake.registry_launch(self.call())
        assert resolved is not None
        launch, claim = resolved
        self.assertEqual((launch.attempt_id, launch.armed, claim.arms), ("att-gate-owner", "registry-rearm", 2))
        self.assertNotIsInstance(rewake.registry_launch(self.call("ls")), tuple)  # one waiter again

    def test_an_owner_that_finished_right_after_the_release_is_still_rearmed(self):
        # Top review M1: the release closed the gate, and the owner ran to
        # `done` before this Bash call's hook ran; the spent claim is still
        # owed its wake, so the done row is a candidate and the wait ends at
        # once with the terminal receipt.
        import dispatch_contract as D
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
        text = self.jobs.read_text(encoding="utf-8").replace(
            "attempt_schema_version=2,", "attempt_schema_version=2,fallback_hop=same-harness-headless,")
        self.jobs.write_text(text, encoding="utf-8")
        self.assertTrue(D.close_attempt_row(self.jobs, "att-gate-owner", "completed-marker"))
        resolved = rewake.registry_launch(self.call())
        assert isinstance(resolved, tuple), resolved
        launch, claim = resolved
        self.assertEqual((launch.attempt_id, launch.armed, claim.arms), ("att-gate-owner", "registry-rearm", 2))
        # a done row with no claim of this session's is never a new candidate
        self.jobs.write_text(self.jobs.read_text(encoding="utf-8").replace("att-gate-owner", "att-other-done"), encoding="utf-8")
        self.assertIsNone(rewake.registry_launch(self.call()))

    def test_the_chain_end_rearms_what_the_cleared_arming_session_left_after_two_clears(self):
        # Registered parent A (`session-gate`) -> B -> C by same-seat /clear. B armed the wake and spent it on
        # a gate record stored under A's key (the owner row's recipient key); B no longer answers for the owner.
        delivery_id = self._gate_record(state="sent-ambiguous")
        claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-B", fresh=True)
        assert isinstance(claim, rewake.ArmClaim)
        self.assertTrue(rewake.settle_arm(claim, "gate-wake-sent", gate_delivery_id=delivery_id))
        path = rewake.arm_path(self.jobs, "att-gate-owner")
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(holder=DEAD_HOLDER, arms=2)
        path.write_text(json.dumps(record), encoding="utf-8")
        resolver = lambda meta, jobs=None, **kw: "session-C" if meta.get("parent_sid") == "session-gate" else meta.get("parent_sid")
        lineage = (mock.patch.object(rewake.seat_handover, "effective_parent", side_effect=resolver),
                   mock.patch.object(rewake.seat_handover, "owns",
                                     side_effect=lambda meta, session, jobs=None, **kw:
                                     meta.get("parent_sid") == session or resolver(meta, jobs) == session))
        with lineage[0], lineage[1]:
            existing = rewake._read_arm(path)
            # Safety unchanged: while the gate record under A's key is open, nobody re-arms.
            self.assertEqual(rewake._reclaim_refusal(existing, self.jobs, "session-C"), "gate-open")
            rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
            self.assertIsNone(rewake._reclaim_refusal(existing, self.jobs, "session-C"))
            self.assertEqual(rewake._reclaim_refusal(existing, self.jobs, "session-B"), "foreign-session")
            self.assertEqual([a for a, _age in rewake._session_owner_rows(self.jobs, "session-C")], ["att-gate-owner"])
            self.assertEqual(rewake._session_owner_rows(self.jobs, "session-B"), [])
            taken = rewake.claim_arm(self.jobs, "att-gate-owner", "session-C", fresh=False)
            self.assertIsInstance(taken, rewake.ArmClaim)
            self.assertEqual((taken.session_id, taken.arms), ("session-C", 3))

    def test_a_missing_or_unnamed_gate_record_counts_as_open(self):
        # Review R1 B2: only a record that was read and is closed re-arms; an
        # unnamed or *missing* record is no closing evidence. The holder is
        # marked dead so this branch, not liveness, is what refuses.
        for delivery_id in (None, "delivery-never-written"):
            with self.subTest(delivery_id=delivery_id):
                self.spend_wake_on(delivery_id)
                refusal = rewake.claim_arm(self.jobs, "att-gate-owner", "session-gate", fresh=False)
                self.assertEqual((refusal.reason, refusal.watched), ("gate-open", True))
                self.assertNotIsInstance(rewake.registry_launch(self.call()), tuple)
                rewake.arm_path(self.jobs, "att-gate-owner").unlink()

    def test_a_foreign_sessions_call_never_rearms_this_owner(self):
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
        self.assertNotIsInstance(rewake.registry_launch(self.call(session="someone-else")), tuple)

    def test_a_registered_but_never_started_owner_never_arms(self):
        """review round 1, M2: the row the fence leaves behind (registered, refused
        at start) must not arm a six-hour wait on nothing."""
        lines = self.jobs.read_text(encoding="utf-8").replace("launch_started=1", "launch_started=0")
        self.jobs.write_text(lines, encoding="utf-8")
        self.assertNotIsInstance(rewake.registry_launch(self.call()), tuple)

    def test_main_rearms_through_the_release_call_and_waits_to_the_terminal(self):
        delivery_id = self._gate_record(state="sent-ambiguous")
        self.spend_wake_on(delivery_id)
        rewake.pending_delivery.ack(self.state, "session-gate", delivery_id, acked_by="gate-release")
        ready = subprocess.CompletedProcess([], 0, stdout="ready")
        (self.root / "utilities").mkdir()
        (self.root / "utilities" / "dispatch-attempt-ready.py").write_text("", encoding="utf-8")
        with mock.patch.object(rewake.subprocess, "run", return_value=ready), \
                mock.patch.object(rewake, "_owner_status", return_value="done"), \
                mock.patch.object(rewake, "agent_home", return_value=self.root), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.call()))), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(sys, "stderr", io.StringIO()) as stderr:
            rewake.main()
        self.assertIn("attempt_id=att-gate-owner", stderr.getvalue())
        self.assertIn("armed=registry-rearm", stderr.getvalue())
        ledger = json.loads(rewake.arm_path(self.jobs, "att-gate-owner").read_text(encoding="utf-8"))
        self.assertEqual(ledger["state"], "ended")


class Depth1WorkerTypeProjectionTest(unittest.TestCase):
    def test_only_owner_frame_review_are_depth1(self):
        for worker_type in ("owner", "frame", "review"):
            self.assertTrue(rewake._worker_type_is_depth1(worker_type))
        for worker_type in ("stage", "dev/backend", ""):
            self.assertFalse(rewake._worker_type_is_depth1(worker_type))
        self.assertTrue(rewake._worker_type_is_depth1({"review", "other"}))
        self.assertFalse(rewake._worker_type_is_depth1({"stage", "other"}))

    def test_depth1_projection_is_not_duplicated_in_adapters(self):
        for adapter in ("claude", "codex", "opencode"):
            source = (ROOT / "adapters" / adapter / "bin" / "dispatch-headless.py").read_text(encoding="utf-8")
            self.assertNotIn("def _worker_type_is_depth1", source)
            self.assertNotIn("DEPTH1_WORKER_TYPES", source)

    def test_stdout_review_worker_uses_the_same_depth_one_gate(self):
        payload = {
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "session_id": "session-1",
            "tool_input": {"command": "python3 utilities/dispatch-headless.py --start"},
            "tool_response": {"stdout": "\n".join((
                "check=ok", "status=start", "dispatch_depth=1",
                "worker_type=review", "parent_completion_delivery=claude-parent-runtime",
                "registered=1", "started=1", "attempt_id=att-review",
                "parent_session_id=session-1", "job_registry=/tmp/jobs.log",
            ))},
        }
        with mock.patch.object(rewake, "_trusted_jobs", return_value=Path("/tmp/jobs.log")), \
                mock.patch.object(rewake, "_validated_jobs", return_value=Path("/tmp/jobs.log")):
            launch = rewake.parse_launch(payload)
        self.assertIsNotNone(launch)
        self.assertEqual(launch.attempt_id, "att-review")


class FrameWorkerTypeArmingTest(unittest.TestCase):
    """W2 (frame-bootstrap-layer, 2026-09-10): a depth-1 `frame` worker must
    arm the same wait a depth-1 `owner` worker does, through both arming
    paths, via the shared `DEPTH1_WORKER_TYPES` constant and the shared
    `_worker_type_is_depth1` comparison helper both paths call."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        environment = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def row(*, attempt_id="att-frame-1", status="open", parent_sid="session-1",
             worker_type="frame", age_seconds=0.0, **overrides):
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
        metadata = {"capability": "autopilot-code", "dispatch_depth": "1", "worker_type": worker_type,
                    "parent_sid": parent_sid, "parent_completion_delivery": "claude-parent-runtime",
                    "launch_claimed": "1", "launch_started": "1", "attempt_id": attempt_id}
        metadata.update(overrides)
        pipe = ",".join(f"{k}={v}" for k, v in metadata.items())
        return "\t".join([stamp, status, "/repo", "/repo", "slug", pipe]) + "\n"

    def stdout_payload(self, *, worker_type="frame", attempt_id="att-frame-1", session_id="session-1"):
        output = "\n".join((
            "check=ok", "status=start", "dispatch_depth=1", f"worker_type={worker_type}",
            "parent_completion_delivery=claude-parent-runtime",
            f"parent_session_id={session_id}", f"job_registry={self.jobs}",
            f"attempt_id={attempt_id}", "registered=1", "started=1",
        ))
        return {
            "hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session_id,
            "tool_input": {"command": "python3 utilities/dispatch-owner.py --start --worker-type frame"},
            "tool_response": {"stdout": output, "stderr": ""},
        }

    def test_frame_stdout_start_receipt_arms_through_parse_launch(self) -> None:
        # Item 1 of W2's required tests: the real stdout fast path (parse_launch,
        # the module's *only* entry to the "worker_type=frame" stdout receipt)
        # recognizes and arms a frame launch exactly as it does an owner one.
        self.jobs.write_text(self.row(), encoding="utf-8")
        launch = rewake.parse_launch(self.stdout_payload())
        self.assertIsNotNone(launch)
        assert launch is not None
        self.assertEqual((launch.attempt_id, launch.armed), ("att-frame-1", "stdout"))

    def test_frame_registry_row_arms_through_registry_launch(self) -> None:
        # Item 2: a filtered stdout (no worker_type/status fields survive) still
        # arms from the registry row alone, through the real registry-path
        # function `registry_launch` -> `_session_owner_rows`.
        self.jobs.write_text(self.row(), encoding="utf-8")
        filtered = self.stdout_payload()
        filtered["tool_response"]["stdout"] = "check=ok"
        resolved = rewake.registry_launch(filtered)
        self.assertIsInstance(resolved, tuple)
        launch, claim = resolved
        self.assertEqual((launch.attempt_id, launch.armed), ("att-frame-1", "registry"))
        self.assertEqual(claim.attempt_id, "att-frame-1")

    def test_registry_consumer_uses_membership_not_equality_against_the_shared_set(self) -> None:
        # Item 3: the structural regression test. If `worker_type` were ever
        # folded back into `REGISTRY_DEPTH1_START`'s equality dict (comparing
        # a scalar with `!=` against `DEPTH1_WORKER_TYPES`, a set), a real
        # frame row could never match -- `metadata.get(key) != value` is true
        # for every string against a frozenset -- and `_session_owner_rows`
        # would silently return nothing for every frame launch. This exercises
        # the literal consumer function directly.
        self.jobs.write_text(self.row(worker_type="frame"), encoding="utf-8")
        rows = rewake._session_owner_rows(self.jobs, "session-1")
        self.assertEqual([attempt_id for attempt_id, _age in rows], ["att-frame-1"])
        # The vocabulary itself must be a real collection checked by
        # membership, and worker_type must not sit inside the equality dict.
        self.assertIsInstance(rewake.DEPTH1_WORKER_TYPES, frozenset)
        self.assertEqual(rewake.DEPTH1_WORKER_TYPES, frozenset({"owner", "frame", "review"}))
        self.assertNotIn("worker_type", rewake.REGISTRY_DEPTH1_START)

    def test_owner_worker_type_still_arms_unchanged(self) -> None:
        # The pre-existing vocabulary member must keep working after the widening.
        self.jobs.write_text(self.row(worker_type="owner", attempt_id="att-owner-frame-sibling"), encoding="utf-8")
        rows = rewake._session_owner_rows(self.jobs, "session-1")
        self.assertEqual([attempt_id for attempt_id, _age in rows], ["att-owner-frame-sibling"])

    def test_a_worker_type_outside_the_vocabulary_still_never_arms(self) -> None:
        for worker_type in ("stage", "support"):
            with self.subTest(worker_type=worker_type):
                self.jobs.write_text(self.row(worker_type=worker_type), encoding="utf-8")
                self.assertEqual(rewake._session_owner_rows(self.jobs, "session-1"), [])
                stdout_launch = rewake.parse_launch(self.stdout_payload(worker_type=worker_type))
                self.assertIsNone(stdout_launch)


class SeatHandoverRewakeTest(unittest.TestCase):
    """After a /clear the same pane's successor takes over an owner's wake; the old hook goes quiet."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.jobs = Path(self.temporary.name) / "jobs.log"
        self.jobs.write_text(DispatchOwnerRewakeTest.row(attempt_schema_version="2"), encoding="utf-8")   # parent_sid=session-1 (the old window)
        environment = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)

    def handed_to(self, successor):
        resolver = lambda meta, jobs=None: successor if meta.get("parent_sid") == "session-1" else meta.get("parent_sid")
        return (mock.patch.object(rewake.seat_handover, "effective_parent", side_effect=resolver),
                mock.patch.object(rewake.seat_handover, "owns",
                                  side_effect=lambda meta, session, jobs=None: meta.get("parent_sid") == session
                                  or resolver(meta, jobs) == session))

    def test_the_successor_arms_the_row_the_old_window_launched_and_a_stranger_does_not(self) -> None:
        self.assertEqual(rewake._session_owner_rows(self.jobs, "session-2"), [])
        effective, owns = self.handed_to("session-2")
        with effective, owns:
            self.assertEqual([a for a, _ in rewake._session_owner_rows(self.jobs, "session-2")], ["att-owner-1"])
            self.assertEqual(rewake._session_owner_rows(self.jobs, "session-9"), [])
            self.assertEqual([a for a, _ in rewake._session_owner_rows(self.jobs, "session-1")], ["att-owner-1"])

    def test_the_successor_takes_the_wait_over_even_while_the_old_hook_is_alive(self) -> None:
        old = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        assert isinstance(old, rewake.ArmClaim)            # this very process stands in for the old hook: alive
        self.assertEqual(rewake.claim_arm(self.jobs, "att-owner-1", "session-2", fresh=True).reason, "foreign-session")
        effective, owns = self.handed_to("session-2")
        with effective, owns:
            self.assertTrue(rewake._holds_arm(old))
            new = rewake.claim_arm(self.jobs, "att-owner-1", "session-2", fresh=True)
            self.assertIsInstance(new, rewake.ArmClaim)
            self.assertEqual((new.session_id, new.arms), ("session-2", 2))
            held = json.loads(rewake.arm_path(self.jobs, "att-owner-1").read_text(encoding="utf-8"))
            self.assertEqual(held["session_id"], "session-2")
            # The old hook no longer holds the claim, so it must not speak into the new conversation.
            # (Same process here: only the session differs.)
            self.assertFalse(rewake._holds_arm(old))

    def test_the_old_hook_is_quiet_once_the_attempt_belongs_to_another_session(self) -> None:
        launch = rewake.Launch(attempt_id="att-owner-1", jobs=self.jobs, session_id="session-1", armed="registry")
        self.assertFalse(rewake._handed_away(launch))
        effective, owns = self.handed_to("session-2")
        with effective, owns:
            self.assertTrue(rewake._handed_away(launch))
            self.assertFalse(rewake._handed_away(replace_session(launch, "session-2")))

    def test_the_wake_still_belongs_to_the_registered_session_without_a_handover(self) -> None:
        claim = rewake.claim_arm(self.jobs, "att-owner-1", "session-1", fresh=True)
        assert isinstance(claim, rewake.ArmClaim)
        launch = rewake.Launch(attempt_id="att-owner-1", jobs=self.jobs, session_id="session-1", armed="registry")
        self.assertTrue(rewake._holds_arm(claim))
        self.assertFalse(rewake._handed_away(launch))


class BatchWakeCarrierTest(unittest.TestCase):
    """A ready sibling success batch wakes the parent once through this carrier;
    a held batch wakes nobody and leaves every record pending; failures and
    already-received members never ride the batch."""

    RECIPIENT = "session-1"
    SIZE = 24

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.pd = rewake.pending_delivery
        for name, value in (("AGENT_HOME", str(MODULE_PATH.parents[1])),
                            ("AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS", "1"),
                            ("AGENT_CLAUDE_REWAKE_MAX_SECONDS", "1")):
            self.enterContext(mock.patch.dict(os.environ, {name: value}))
        self.ids = [(f"att-{i:032x}", f"delivery-{i:032x}") for i in range(self.SIZE)]
        for attempt_id, delivery_id in self.ids:
            self._create(attempt_id, delivery_id, success=True)
        self.batch_ids = tuple(sorted(d for _, d in self.ids))
        self.held = False
        self.retired: set[str] = set()
        self.enterContext(mock.patch.object(
            self.pd, "claim_ready_batch", wraps=self.pd.claim_ready_batch))
        self.enterContext(mock.patch("dispatch_notice_state.resolve_delivery_batch", side_effect=self._resolve))
        self.enterContext(mock.patch("dispatch_notice_state.keep_claim", side_effect=self._keep))

    def _create(self, attempt_id, delivery_id, *, success):
        receipt = {
            "schema_version": 2, "state": "ready", "parent_attempt_id": "att-parent",
            "job_registry": str(self.jobs),
            "children": [{"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                          "reason": "terminal-complete" if success else "terminal-failure-or-unclosed",
                          "required_action": "advance-completed" if success else "inspect-done-failure",
                          "harness": "claude",
                          "delivery_classification": "success" if success else "attention"}],
            "delivery_classification": "success" if success else "attention",
        }
        self.pd.create(
            self.root, recipient_kind="claude-parent-runtime", recipient_key=self.RECIPIENT,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[attempt_id], parent_attempt_id="att-parent", route_id="rt-batch",
            route_node="frame", receipt=receipt, receipt_digest=self.pd._canonical_receipt_digest(receipt),
            row_revisions={attempt_id: "rev"})

    def _resolve(self, jobs, record, **_options):
        if record["delivery_id"] not in self.batch_ids:
            return None
        return {"delivery_ids": self.batch_ids, "held": self.held}

    def _keep(self, root, recipient, delivery_id, record, claim_owner, *, jobs=None):
        if delivery_id in self.retired:
            self.pd.reject_claimed(root, recipient, delivery_id, claim_owner=claim_owner,
                                   reason="notice-obligation-resolved")
            return False
        return True

    def _wake(self, attempt_id, delivery_id):
        """One sibling's carrier from the ready wait to its exit; returns (code, stdout, stderr)."""
        launch = rewake.Launch(attempt_id=attempt_id, jobs=self.jobs, session_id=self.RECIPIENT, armed="registry")
        owing = {"delivery_id": delivery_id, "parent_sid": self.RECIPIENT}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(rewake.sys, "stdout", stdout), mock.patch.object(rewake.sys, "stderr", stderr), \
                mock.patch.object(rewake, "wait_for_attempt", return_value=("ready", "terminal-quiescent")), \
                mock.patch.object(rewake, "follow_replacement", return_value=None), \
                mock.patch.object(rewake, "_owner_status", return_value="done"), \
                mock.patch.object(rewake, "classified_receipt", side_effect=lambda l, st, r, root: (
                    "success", f"RECEIPT {l.attempt_id} state=success status=done")), \
                mock.patch.object(rewake, "_delivery_owing_row", return_value=owing), \
                mock.patch.object(rewake, "_incarnation_binding_matches", return_value=True), \
                mock.patch.object(rewake, "_holds_arm", return_value=True), \
                mock.patch.object(rewake, "_handed_away", return_value=False), \
                mock.patch.object(rewake, "_open_gate_pending", return_value=False), \
                mock.patch.object(rewake, "_gate_notices", return_value=[]), \
                mock.patch.object(rewake, "settle_arm", return_value=True), \
                mock.patch.object(rewake.time, "sleep"):
            code = rewake._run_carrier(launch, mock.sentinel.arm, {}, {}, {"claim": mock.sentinel.arm})
        return code, stdout.getvalue(), stderr.getvalue()

    def _states(self):
        return {d: self.pd.read(self.root, self.RECIPIENT, d)["state"] for _, d in self.ids}

    def test_ready_batch_of_24_wakes_once_with_every_receipt_and_marker(self):
        results = [self._wake(a, d) for a, d in self.ids]
        emissions = [r for r in results if r[0] == 2]
        self.assertEqual(len(emissions), 1)
        self.assertTrue(all(r == (0, "", "") for r in results if r[0] != 2))
        text = emissions[0][2]
        self.assertEqual(text.count("HEARTING_RECEIPT_V1 "), self.SIZE)
        for attempt_id, delivery_id in self.ids:
            self.assertIn(f"RECEIPT {attempt_id} ", text)
            self.assertIn(f"HEARTING_RECEIPT_V1 {delivery_id} ", text)
        self.assertEqual(set(self._states().values()), {"sent-ambiguous"})
        print(f"batch24 emissions={len(emissions)} wakes={len(emissions)} chars={len(text)}")

    def test_held_batch_emits_nothing_and_keeps_every_record_pending(self):
        self.held = True
        for attempt_id, delivery_id in self.ids[:3]:
            self.assertEqual(self._wake(attempt_id, delivery_id), (0, "", ""))
        self.assertEqual(set(self._states().values()), {"pending"})
        for _, delivery_id in self.ids:
            self.assertEqual(self.pd.read(self.root, self.RECIPIENT, delivery_id)["attempts"], 0)
        self.held = False
        code, _out, text = self._wake(*self.ids[0])
        self.assertEqual((code, text.count("HEARTING_RECEIPT_V1 ")), (2, self.SIZE))

    def test_a_failure_record_is_delivered_immediately_on_its_own(self):
        self.held = True
        failed = ("att-" + "f" * 32, "delivery-" + "f" * 32)
        self._create(*failed, success=False)
        code, _out, text = self._wake(*failed)
        self.assertEqual((code, text.count("HEARTING_RECEIPT_V1 ")), (2, 1))
        self.assertIn(f"HEARTING_RECEIPT_V1 {failed[1]} ", text)
        self.assertEqual(self.pd.read(self.root, self.RECIPIENT, failed[1])["state"], "sent-ambiguous")
        self.assertEqual(set(self._states().values()), {"pending"})

    def test_an_already_received_member_is_not_emitted_again(self):
        received = self.ids[5][1]
        self.pd.claim(self.root, self.RECIPIENT, received, claim_owner="earlier", lease_seconds=30)
        self.pd.ack(self.root, self.RECIPIENT, received, acked_by="earlier")
        self.retired.add(received)
        results = [self._wake(a, d) for a, d in self.ids]
        emissions = [r for r in results if r[0] == 2]
        self.assertEqual(len(emissions), 1)
        text = emissions[0][2]
        self.assertEqual(text.count("HEARTING_RECEIPT_V1 "), self.SIZE - 1)
        self.assertNotIn(received, text)
        self.assertEqual(self.pd.read(self.root, self.RECIPIENT, received)["state"], "acked")


def replace_session(launch, session):
    return rewake.Launch(attempt_id=launch.attempt_id, jobs=launch.jobs, session_id=session, armed=launch.armed)


class SeatGateChainTest(unittest.TestCase):
    """Real session-tidy seat lineage and delivery records across A → B → C."""

    def setUp(self) -> None:
        GateCarrierTest.setUp(self)
        self.pane = "test:seat-gate-chain-" + self.root.name
        self.agent_home = self.root / "agent-home"
        (self.agent_home / "core").mkdir(parents=True)
        (self.agent_home / "core" / "CORE.md").write_text("isolated hook fixture\n", encoding="utf-8")
        readiness = self.agent_home / "utilities" / "dispatch-attempt-ready.py"
        readiness.parent.mkdir(parents=True)
        self.probe = self.root / "readiness-probed"
        readiness.write_text(
            "#!/usr/bin/env python3\nfrom pathlib import Path\n"
            f"Path({str(self.probe)!r}).touch()\nraise SystemExit(2)\n", encoding="utf-8")
        self.env_patch = mock.patch.dict(os.environ, {
            "AGENT_HOME": str(self.agent_home),
            "AGENT_DISPATCH_JOBS": str(self.jobs),
            "HARNESS_STATE_ROOT": str(self.state),
            "HERDR_PANE_ID": self.pane,
            "AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS": "1",
            "AGENT_CLAUDE_REWAKE_MAX_SECONDS": "8",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        import pane_ownership
        import dispatch_seat_handover
        import session_tidy
        self.tidy = session_tidy
        self.handover = dispatch_seat_handover
        # Match TidyCase's fixture for the platform pane-verification boundary;
        # snapshot, adoption, effective_parent and storage lookup remain real.
        self.pane_proof = mock.patch.object(
            pane_ownership, "verified_pane", side_effect=lambda pane, *args, **kwargs: pane or "")
        self.pane_proof.start()
        self.addCleanup(self.pane_proof.stop)
        self._prepare_owner_route()

    def _prepare_owner_route(self):
        from route_identity import route_hash
        route = {"route_id": "rt-gate", "nodes": [{"id": "frame", "continuation": {
            "kind": "human-gate", "gate": "frame-review"}}]}
        route["route_hash"] = route_hash(route)
        route_path = self.root / "rt-gate.json"
        route_path.write_text(json.dumps(route), encoding="utf-8")
        fields = self.jobs.read_text(encoding="utf-8").strip().split("\t")
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        metadata.update(route_hash=route["route_hash"], route_file=str(route_path))
        fields[5] = ",".join(f"{key}={value}" for key, value in metadata.items())
        self.jobs.write_text("\t".join(fields) + "\n", encoding="utf-8")

    def _create_gate(self):
        self.delivery_id = GateCarrierTest._gate_record(self, state=None)

    def _snapshot(self, sid):
        seat = self.tidy.resolve_seat("claude", str(self.root), os.environ)
        with self.tidy.seat_lock(seat.key):
            self.tidy.record_event(seat, "claude", sid, "start", cwd=str(self.root))
            self.assertEqual(self.handover.write_snapshot_locked(
                seat, "claude", sid, jobs=self.jobs), 1)

    def _clear_to(self, sid):
        self.tidy.run_hook("claude", "start", sid, source="clear", cwd=str(self.root),
                           env=os.environ, emit=lambda _text: None)

    def _adopt_b(self):
        self._snapshot("session-gate")
        self._clear_to("session-B")
        owner = rewake._row_metadata(self.jobs, "att-gate-owner")
        self.assertEqual(rewake.seat_handover.effective_parent(owner, self.jobs), "session-B",
                         rewake.seat_handover.handover_rows(
                             self.tidy.resolve_seat("claude", str(self.root), {"HERDR_PANE_ID": self.pane})))

    def _adopt_c(self):
        self._snapshot("session-B")
        self._clear_to("session-C")
        owner = rewake._row_metadata(self.jobs, "att-gate-owner")
        self.assertEqual(rewake.seat_handover.effective_parent(owner, self.jobs), "session-C",
                         rewake.seat_handover.handover_rows(
                             self.tidy.resolve_seat("claude", str(self.root), {"HERDR_PANE_ID": self.pane})))

    def _carrier(self, session, claim):
        launch = rewake.Launch("att-gate-owner", self.jobs, session, armed="registry")
        payload = {"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": session,
                   "tool_input": {"command": "python3 utilities/workflow-supervisor.py"},
                   "tool_response": {"stdout": "", "stderr": ""}}
        observation = {}
        holder = {"claim": claim}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rewake._run_carrier(launch, claim, payload, observation, holder)
        return code, out.getvalue(), err.getvalue(), observation

    def _start_live_b_carrier(self):
        helper = self.root / "live-b-carrier.py"
        helper.write_text("""
import contextlib, importlib.util, io, json, os, sys, time
from pathlib import Path
module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('live_b_rewake', module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
jobs = Path(os.environ['AGENT_DISPATCH_JOBS'])
claim = module.claim_arm(jobs, 'att-gate-owner', 'session-B', fresh=True)
ready = Path(sys.argv[2])
if not isinstance(claim, module.ArmClaim):
    ready.write_text(json.dumps({'error': getattr(claim, 'reason', 'claim-refused')}))
    raise SystemExit(1)
ready.write_text(json.dumps({'holder': list(claim.holder), 'session_id': claim.session_id}))
release = Path(sys.argv[3])
deadline = time.monotonic() + 10
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not release.exists():
    raise SystemExit(2)
payload = {'hook_event_name': 'PostToolUse', 'tool_name': 'Bash', 'session_id': 'session-B',
           'tool_input': {'command': 'python3 utilities/workflow-supervisor.py'},
           'tool_response': {'stdout': '', 'stderr': ''}}
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = module._run_carrier(module.Launch('att-gate-owner', jobs, 'session-B', armed='registry'),
                               claim, payload, {}, {'claim': claim})
print(json.dumps({'code': code, 'stdout': out.getvalue(), 'stderr': err.getvalue()}), flush=True)
""", encoding="utf-8")
        ready = self.root / "b-claimed.json"
        release = self.root / "release-b"
        process = subprocess.Popen(
            [sys.executable, str(helper), str(MODULE_PATH), str(ready), str(release)],
            cwd=str(self.root), env=dict(os.environ), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "B carrier did not publish its real arm claim")
        claim = json.loads(ready.read_text(encoding="utf-8"))
        self.assertNotIn("error", claim, claim)
        self.addCleanup(self._stop_b_carrier, process, release)
        return process, release, claim

    @staticmethod
    def _stop_b_carrier(process, release):
        if process.poll() is None:
            release.touch(exist_ok=True)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=2)

    def test_handed_away_gate_carrier_is_silent_and_c_can_wake(self):
        self._adopt_b()
        process, release, b_claim = self._start_live_b_carrier()
        release.touch()
        deadline = time.monotonic() + 5
        while not self.probe.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        self.assertTrue(self.probe.exists(), "B carrier did not begin its real readiness wait")
        self._snapshot("session-B")
        self._adopt_c()
        self._create_gate()
        c_claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-C", fresh=True)
        self.assertIsInstance(c_claim, rewake.ArmClaim)

        output, error = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, error)
        stale = json.loads(output)
        self.assertEqual((stale["code"], stale["stdout"], stale["stderr"]), (0, "", ""))
        record = rewake.pending_delivery.read(self.state, "session-gate", self.delivery_id)
        self.assertEqual(record["state"], "pending")
        self.assertEqual(rewake._read_arm(rewake.arm_path(self.jobs, "att-gate-owner"))["session_id"],
                         "session-C")
        arm = rewake._read_arm(rewake.arm_path(self.jobs, "att-gate-owner"))
        self.assertEqual((arm["state"], arm["session_id"], arm["holder"]),
                         ("waiting", "session-C", list(c_claim.holder)))

        code, stdout, stderr, _observation = self._carrier("session-C", c_claim)
        self.assertEqual(code, 2)
        self.assertIn("human-gate:frame-review", stdout + stderr)
        self.assertEqual(rewake.pending_delivery.read(self.state, "session-gate", self.delivery_id)["state"],
                         "sent-ambiguous")
        self.assertEqual(rewake._read_arm(rewake.arm_path(self.jobs, "att-gate-owner"))["state"],
                         "gate-wake-sent")

    def test_first_c_prompt_surfaces_gate_written_while_b_owned_the_seat(self):
        self._adopt_b()
        b_claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-B", fresh=True)
        self.assertIsInstance(b_claim, rewake.ArmClaim)
        self._create_gate()
        code, stdout, stderr, _observation = self._carrier("session-B", b_claim)
        self.assertEqual(code, 2)
        self.assertIn("human-gate:frame-review", stdout + stderr)
        record = rewake.pending_delivery.read(self.state, "session-gate", self.delivery_id)
        self.assertEqual(record["state"], "sent-ambiguous")

        # Advance only this isolated record's real delivery lease as the short
        # transport fixture; the following operation is the actual prompt hook.
        rewake.pending_delivery.reclaim(
            self.state, "session-gate", self.delivery_id, now_ns=time.monotonic_ns() + 60_000_000_000)
        self._adopt_c()
        sweep_payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session-C",
                         "cwd": str(self.root), "prompt": ""}
        sweep_spec = importlib.util.spec_from_file_location(
            "dispatch_session_sweep_hook", ROOT / "hooks" / "dispatch-session-sweep.py")
        dispatch_session_sweep = importlib.util.module_from_spec(sweep_spec)
        sweep_spec.loader.exec_module(dispatch_session_sweep)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "session-C",
                                          "CODEX_THREAD_ID": "", "OPENCODE_SESSION_ID": ""},
                             clear=False), contextlib.redirect_stdout(output):
            result = dispatch_session_sweep._process_session(sweep_payload, "session-C")
        self.assertEqual(result, 0)
        context = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(self.delivery_id, context)
        self.assertIn("human-gate:frame-review", context)
        self.assertEqual(rewake.pending_delivery.read(
            self.state, "session-gate", self.delivery_id)["state"], "acked")

        resumed = rewake.registry_launch({
            "hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": "session-C",
            "tool_input": {"command": "git status"}, "tool_response": {"stdout": "", "stderr": ""},
        })
        self.assertIsInstance(resumed, tuple, resumed)
        self.assertEqual(resumed[0].session_id, "session-C")
        self.assertEqual(resumed[1].arms, 2)

    def test_first_cross_pane_retire_preserves_gate_for_successor(self):
        self._create_gate()
        owner = rewake._row_metadata(self.jobs, "att-gate-owner")
        source_seat = self.tidy.resolve_seat(
            "claude", str(self.root), {"HERDR_PANE_ID": self.pane})
        self.assertIsNone(self.handover.read_snapshot(source_seat.key))
        a_claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-gate", fresh=True)
        self.assertIsInstance(a_claim, rewake.ArmClaim)

        target_seat = self.tidy.resolve_seat(
            "claude", str(self.root), {**os.environ,
                                       "HERDR_PANE_ID": "fixture:retire-successor-" + self.root.name})
        self.assertIsNone(self.handover.read_snapshot(target_seat.key))
        row = self.handover.record_retire_handover(
            "session-gate", "claude", "session-C", "claude",
            jobs=self.jobs, successor_seat=target_seat)
        self.assertIsNotNone(row)
        self.assertEqual(self.handover.effective_parent(owner, self.jobs), "session-C")

        with mock.patch.object(rewake, "wait_for_attempt",
                               return_value=("gate", "human-gate-open")):
            code, stdout, stderr, _ = self._carrier("session-gate", a_claim)
        self.assertEqual((code, stdout, stderr), (0, "", ""))
        self.assertEqual(rewake.pending_delivery.read(
            self.state, "session-gate", self.delivery_id)["state"], "pending")
        arm = rewake._read_arm(rewake.arm_path(self.jobs, "att-gate-owner"))
        self.assertEqual((arm["session_id"], arm["state"]),
                         ("session-gate", "waiting"))

        c_claim = rewake.claim_arm(self.jobs, "att-gate-owner", "session-C", fresh=True)
        self.assertIsInstance(c_claim, rewake.ArmClaim)
        with mock.patch.object(rewake, "wait_for_attempt",
                               return_value=("gate", "human-gate-open")):
            code, stdout, stderr, _ = self._carrier("session-C", c_claim)
        self.assertEqual(code, 2)
        self.assertIn("human-gate:frame-review", stdout + stderr)
        self.assertEqual(rewake.pending_delivery.read(
            self.state, "session-gate", self.delivery_id)["state"], "sent-ambiguous")
        self.assertEqual(rewake._read_arm(
            rewake.arm_path(self.jobs, "att-gate-owner"))["state"], "gate-wake-sent")


class GateCheckEffectRaceTest(unittest.TestCase):
    """A real C adoption commits after B's final pre-effect ownership read."""

    def setUp(self):
        self.fixture = SeatGateChainTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_adoption_invalidates_b_before_gate_claim_and_emission(self):
        f = self.fixture
        f._adopt_b()
        f._create_gate()
        helper = f.root / "controlled-b-gate.py"
        signal_path, release_path = f.root / "b-read-owner", f.root / "continue-b"
        helper.write_text("""
import contextlib, importlib.util, io, json, os, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('controlled_rewake', Path(sys.argv[1]))
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
jobs = Path(os.environ['AGENT_DISPATCH_JOBS'])
claim = module.claim_arm(jobs, 'att-gate-owner', 'session-B', fresh=True)
if not isinstance(claim, module.ArmClaim): raise SystemExit(10)
signal_path, release_path = Path(sys.argv[2]), Path(sys.argv[3])
real_parent = module.seat_handover.effective_parent
def gate_wait(*args, **kwargs): return ('gate', 'human-gate-open')
def paused_parent(meta, registry, **kwargs):
    current = real_parent(meta, registry, **kwargs)
    if current == 'session-B' and not signal_path.exists():
        signal_path.touch()
        deadline = time.monotonic() + 5
        while not release_path.exists() and time.monotonic() < deadline: time.sleep(0.01)
        if not release_path.exists(): raise SystemExit(11)
    return current
module.wait_for_attempt = gate_wait
module.seat_handover.effective_parent = paused_parent
launch = module.Launch('att-gate-owner', jobs, 'session-B', armed='registry')
payload = {'hook_event_name':'PostToolUse','tool_name':'Bash','session_id':'session-B',
 'tool_input':{'command':'python3 utilities/workflow-supervisor.py'},'tool_response':{'stdout':'','stderr':''}}
out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
 code = module._run_carrier(launch, claim, payload, {}, {'claim':claim})
print(json.dumps({'code':code,'stdout':out.getvalue(),'stderr':err.getvalue()}), flush=True)
""", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, str(helper), str(MODULE_PATH), str(signal_path), str(release_path)],
            cwd=str(f.root), env=dict(os.environ), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not signal_path.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(signal_path.exists(), "B did not reach the ownership/effect seam")
            f._adopt_c()
            c_claim = rewake.claim_arm(f.jobs, "att-gate-owner", "session-C", fresh=True)
            self.assertIsInstance(c_claim, rewake.ArmClaim)
            release_path.touch()
            output, error = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, error)
            stale = json.loads(output)
            self.assertEqual((stale["code"], stale["stdout"], stale["stderr"]), (0, "", ""))
            record = rewake.pending_delivery.read(f.state, "session-gate", f.delivery_id)
            self.assertEqual(record["state"], "pending")
            arm = rewake._read_arm(rewake.arm_path(f.jobs, "att-gate-owner"))
            self.assertEqual((arm["state"], arm["session_id"]), ("waiting", "session-C"))
            code, stdout, stderr, _ = f._carrier("session-C", c_claim)
            self.assertEqual(code, 2)
            self.assertIn("human-gate:frame-review", stdout + stderr)
            self.assertEqual(rewake.pending_delivery.read(
                f.state, "session-gate", f.delivery_id)["state"], "sent-ambiguous")
        finally:
            if process.poll() is None:
                release_path.touch(exist_ok=True)
                process.communicate(timeout=5)

    def test_first_cross_pane_binding_waits_behind_gate_effect(self):
        import threading
        f = self.fixture
        f._create_gate()
        owner = rewake._row_metadata(f.jobs, "att-gate-owner")
        source = f.tidy.resolve_seat("claude", str(f.root), os.environ)
        self.assertIsNone(f.handover.read_snapshot(source.key))
        claim = rewake.claim_arm(f.jobs, "att-gate-owner", "session-gate", fresh=True)
        self.assertIsInstance(claim, rewake.ArmClaim)
        successor = "session-retired-first-binding"
        target = f.tidy.resolve_seat("claude", str(f.root), {
            **os.environ, "HERDR_PANE_ID": "fixture:retire-race-" + f.root.name,
        })
        self.assertIsNone(f.handover.read_snapshot(target.key))
        order = []
        writer_lock_attempt = threading.Event()
        adopted = threading.Event()
        local = threading.local()
        effect_thread = threading.get_ident()
        real_lock = f.tidy.seat_lock
        real_parent = f.handover.effective_parent
        reads = {"count": 0}

        def retire():
            self.assertIsNotNone(f.handover.record_retire_handover(
                "session-gate", "claude", successor, "claude",
                jobs=f.jobs, successor_seat=target))
            order.append("adopted")
            adopted.set()

        @contextlib.contextmanager
        def observed_lock(key, *, wait=None):
            if (key.startswith("lineage-binding-")
                    and threading.get_ident() != effect_thread):
                writer_lock_attempt.set()
            with real_lock(key, wait=wait):
                previous = getattr(local, "binding", False)
                if key.startswith("lineage-binding-"):
                    local.binding = True
                try:
                    yield
                finally:
                    local.binding = previous

        def synchronized_real_read(meta, registry, **kwargs):
            current = real_parent(meta, registry, **kwargs)
            if meta.get("attempt_id") == "att-gate-owner" and current == "session-gate":
                reads["count"] += 1
                if reads["count"] == 2:
                    # The first read is the pre-wait ownership check. The
                    # second is immediately before gate claim/output. A stable
                    # binding transaction makes the real retire writer wait;
                    # the old snapshot-selected lock has no key yet.
                    if getattr(local, "binding", False):
                        def writer():
                            retire()
                        threading.Thread(target=writer, daemon=True).start()
                        self.assertTrue(writer_lock_attempt.wait(2),
                                        "retire writer did not reach the real binding lock")
                    else:
                        retire()
            return current

        real_settle = rewake.settle_arm

        def mark_gate_settlement(current_claim, state, **kwargs):
            result = real_settle(current_claim, state, **kwargs)
            if state == "gate-wake-sent":
                order.append("gate-effect")
            return result

        with mock.patch.object(f.tidy, "seat_lock", side_effect=observed_lock), \
             mock.patch.object(f.handover, "effective_parent",
                               side_effect=synchronized_real_read), \
             mock.patch.object(rewake, "wait_for_attempt",
                               return_value=("gate", "human-gate-open")), \
             mock.patch.object(rewake, "settle_arm", side_effect=mark_gate_settlement):
            code, stdout, stderr, _ = f._carrier("session-gate", claim)
        self.assertEqual(code, 2)
        self.assertIn("human-gate:frame-review", stdout + stderr)
        self.assertTrue(adopted.wait(3), "retire did not commit after the gate effect released its mutex")
        self.assertLess(order.index("gate-effect"), order.index("adopted"), order)
        self.assertEqual(f.handover.effective_parent(owner, f.jobs), successor)
        record = rewake.pending_delivery.read(f.state, "session-gate", f.delivery_id)
        self.assertEqual(record["state"], "sent-ambiguous")
        self.assertEqual(rewake._read_arm(
            rewake.arm_path(f.jobs, "att-gate-owner"))["state"], "gate-wake-sent")


if __name__ == "__main__":
    # Unit fixtures exercise records without observers racing their cleanup;
    # runner startup is exercised by peer_obligations.test.py.
    with mock.patch.object(rewake.peer_obligations, "ensure_normal_runner", return_value=True):
        unittest.main()
