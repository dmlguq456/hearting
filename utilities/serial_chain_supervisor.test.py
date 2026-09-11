#!/usr/bin/env python3
"""Acceptance seams for the shared serial-chain supervisor contract.

These tests deliberately exercise the real successor service and shared driver;
runtime subprocesses remain fixture-only and only the service's ``run_checked``
boundary is captured.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import textwrap
import unittest
from unittest import mock
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
import sys
import time
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_subsession_advance as ADVANCE  # noqa: E402
import dispatch_contract as CONTRACT  # noqa: E402
import dispatch_subsession_handoff as HANDOFF  # noqa: E402
from stage_session_contract import (  # noqa: E402
    StageSessionError,
    load_manifest,
    sealed_pointer_bytes,
    slice_files_sha256,
    slice_text_sha256,
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CLAUDE = _load("serial_chain_claude", "claude-session-supervisor.py")
CODEX = _load("serial_chain_codex", "codex-app-server-supervisor.py")
CHAIN = _load("serial_chain_stage", "stage-session-chain.py")
CLAUDE_HARNESS = _load("serial_chain_claude_harness", "claude_session_supervisor.test.py")
CODEX_HARNESS = _load("serial_chain_codex_harness", "codex_app_server_supervisor.test.py")


def _serial_rows(count: int, *, chain: str = "ssc-acceptance"):
    return [
        SimpleNamespace(
            attempt_id=f"att-{index}", status="done",
            metadata={
                "session_chain_id": chain, "subsession_mode": "serial",
                "subsession_index": str(index),
            },
        )
        for index in range(1, count + 1)
    ]


def _real_chain_fixture(harness, supervisor_module, count: int):
    """Create the sealed pointer and exact registry rows used by flow tests."""

    base = harness.base
    source = base / "source"
    source.mkdir(exist_ok=True)
    route = {
        "schema_version": 2,
        "cwd": str(base),
        "artifact_root": str(harness.artifact_root),
        "capability": "autopilot-code",
        "capability_mode": "debug",
        "effective_intensity": "strong",
        "nodes": [
            {
                "id": "execute", "kind": "pipeline-stage", "completion_gate": "code-execute",
                "write_scope": ["source/**"], "dispatch_depth": 2,
            },
            {"id": "report", "kind": "pipeline-stage", "terminal": True},
        ],
        "workflow_contract": {"terminal_nodes": ["report"]},
        "runtime_support": {"terminal_commit": True},
    }
    route["route_hash"] = CLAUDE.canonical_route_hash(route)
    route["route_id"] = CLAUDE.route_id_from_hash(route["route_hash"])
    route_path = base / "route.json"
    route_path.write_text(json.dumps(route), encoding="utf-8")
    brief_paths = []
    sessions = []
    for index in range(1, count + 1):
        brief = base / f"brief-{index}.md"
        brief.write_text(f"session {index}\n", encoding="utf-8")
        fixed = source / f"slice-{index}.py"
        fixed.write_text(f"# slice {index}\n", encoding="utf-8")
        brief_paths.append(brief)
        sessions.append({
            "subsession_id": f"ss-chain-{index:04d}",
            "attempt_id": f"att-chain-{index:04d}",
            "adapter": ("claude", "codex", "opencode")[(index - 1) % 3],
            "slug": f"execute-s{index}",
            "phase_brief": str(brief),
            "fixed_files": [str(fixed)],
            "narrow_verify": "true",
            "expected_round_trips": 1,
        })
    manifest_path = base / "chain.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1, "kind": "stage-session-chain",
        "chain_id": "ssc-acceptance-real", "mode": "serial",
        "worktree": str(base), "route_file": str(route_path),
        "route_id": route["route_id"], "route_hash": route["route_hash"],
        "route_node": "execute", "completion_gate": "code-execute",
        "sessions": sessions,
    }), encoding="utf-8")
    manifest = load_manifest(
        manifest_path, route=route, node=route["nodes"][0]
    )
    CHAIN.persist_chain_manifest(harness.jobs, manifest)
    metadata_lines = []
    for session in manifest["sessions"]:
        index = session["index"]
        metadata = {
            "attempt_schema_version": "2", "dispatch_depth": "2",
            "transport": "headless", "execution_surface": "registered-headless",
            "registered_worker": "1", "harness": session["adapter"],
            "attempt_id": session["attempt_id"], "parent_attempt_id": "att-parent",
            "parent": "owner", "parent_sid": "parent-session",
            "parent_cwd": str(base), "session_chain_id": manifest["chain_id"],
            "subsession_id": session["subsession_id"],
            "subsession_index": str(index), "subsession_count": str(count),
            "subsession_mode": "serial", "subsession_purpose": "planned",
            "stage_authority": "0", "route_id": route["route_id"],
            "route_hash": route["route_hash"], "route_node": "execute",
            "route_file": str(route_path), "phase_brief": session["phase_brief"],
            "fixed_files_sha256": slice_files_sha256(session["fixed_files"]),
            "narrow_verify_sha256": slice_text_sha256(session["narrow_verify"]),
            "expected_round_trips": str(session["expected_round_trips"]),
            "launch_claimed": "1" if index == 1 else "0",
        }
        if index == 1:
            metadata.update({"launch_started": "1", "pid": "4242"})
        encoded = ",".join(f"{key}={value}" for key, value in metadata.items())
        metadata_lines.append(
            f"2026-09-10T00:00:{index:02d}Z\topen\t/repo\t{base}\t"
            f"{session['slug']}\t{encoded}\n"
        )
    harness.jobs.write_text(
        harness_module_owner_row(harness) + "".join(metadata_lines), encoding="utf-8"
    )
    return route, manifest


def harness_module_owner_row(harness) -> str:
    if harness.__class__.__name__.startswith("Claude"):
        return CLAUDE_HARNESS.owner_row(harness.lease)
    return CODEX_HARNESS.owner_row(harness.lease)


def _run_real_supervisor_flow(
    runtime: str, count: int, *, mixed: bool = False, terminal: bool = False,
    proof_refusal: bool = False,
):
    harness_module = CLAUDE_HARNESS if runtime == "claude" else CODEX_HARNESS
    harness_class = (
        harness_module.ClaudeSessionSupervisorTest
        if runtime == "claude" else harness_module.CodexAppServerSupervisorTest
    )
    supervisor_module = CLAUDE if runtime == "claude" else CODEX
    harness_test = (
        "test_resume_uses_same_session_once_after_join"
        if runtime == "claude" else
        "test_runtime_wait_has_no_model_activity_until_exact_join_is_ready"
    )
    harness = harness_class(harness_test)
    harness.setUp()
    try:
        route, manifest = _real_chain_fixture(harness, supervisor_module, count)
        if mixed:
            sibling = (
                "2026-09-10T00:00:30Z\topen\t/repo\t" + str(harness.base)
                + "\tsibling\tattempt_schema_version=2,dispatch_depth=2,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,launch_started=1,"
                "attempt_id=att-sibling,parent_attempt_id=att-parent\n"
            )
            harness.jobs.write_text(harness.jobs.read_text(encoding="utf-8") + sibling, encoding="utf-8")
        if proof_refusal:
            ADVANCE.chain_manifest_pointer_path(harness.jobs, manifest["chain_id"]).unlink()
        calls = []
        claims = []

        def successor_run_checked(command, env=None):
            calls.append((list(command), dict(env or {})))
            action = command[command.index("--action") + 1]
            attempt_id = command[command.index("--attempt-id") + 1]
            if action == "start":
                lines = harness.jobs.read_text(encoding="utf-8").splitlines()
                updated = []
                for line in lines:
                    fields = line.split("\t")
                    metadata = CONTRACT.parse_registry_metadata(fields[5]) if len(fields) == 6 else {}
                    if metadata.get("attempt_id") == attempt_id:
                        metadata["launch_claimed"] = "1"
                        metadata["launch_started"] = "1"
                        metadata["pid"] = "4242"
                        fields[5] = ",".join(f"{key}={value}" for key, value in metadata.items())
                    updated.append("\t".join(fields))
                harness.jobs.write_text("\n".join(updated) + "\n", encoding="utf-8")
                stdout = (
                    f"check=ok\nattempt_id={attempt_id}\nregistered=1\nstarted=1\n"
                    "duplicate_attempt=0\nchild_spawned=1\n"
                )
            else:
                stdout = (
                    f"check=ok\nattempt_id={attempt_id}\nregistered=1\nstarted=0\n"
                    "duplicate_attempt=1\nchild_spawned=0\n"
                )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        def write_handoff(jobs, row, loaded_manifest, artifact_root):
            HANDOFF.flush_handoff(
                HANDOFF.handoff_path(Path(artifact_root), route["route_id"], manifest["chain_id"]),
                predecessor_attempt_id=row.attempt_id,
                predecessor_subsession_id=row.metadata["subsession_id"],
                manifest_sha256=loaded_manifest["_manifest_sha256"],
                completed_items=["done"], next_command="continue",
                invariants=["serial"], forbidden_files=["none"],
            )
            return "flushed"

        patches = [
            mock.patch.object(ADVANCE, "_load_chain_module", return_value=CHAIN),
            mock.patch.object(CHAIN, "run_checked", side_effect=successor_run_checked),
            mock.patch.object(ADVANCE, "_resolve_artifact_root", return_value=str(harness.artifact_root)),
            mock.patch.object(ADVANCE, "flush_chain_handoff_for_row", side_effect=write_handoff),
        ]
        if terminal:
            import dispatch_terminal_commit as TERMINAL
            slot = TERMINAL.terminal_slot(harness.artifact_root, route["route_id"], "att-parent")
            slot.mkdir(parents=True, exist_ok=True)
            envelope = "artifact: -\nverdict: PASS\nblocker: none\n"

            def settle(*unused):
                (slot / "terminal-commit.json").write_text(json.dumps({
                    "state": "owner-envelope-sealed", "owner_attempt_id": "att-parent",
                    "route_hash": route["route_hash"], "terminal_commit_id": "mixed-fixture",
                }), encoding="utf-8")
                return TERMINAL.TerminalCommitResult("completed", None, None, ("report",), envelope)

            patches.append(mock.patch.object(supervisor_module, "terminal_commit_adapter", side_effect=settle))
            real_claim = supervisor_module.budget_record.claim_terminal_handoff

            def capture_claim(*args, **kwargs):
                claims.append(tuple(kwargs.get("child_attempt_ids", ())))
                return real_claim(*args, **kwargs)

            patches.append(mock.patch.object(
                supervisor_module.budget_record,
                "claim_terminal_handoff",
                side_effect=capture_claim,
            ))
        for patcher in patches:
            patcher.start()
        try:
            command = harness.command()[2:]
            if terminal:
                command += ["--route-file", str(harness.base / "route.json"),
                            "--route-id", route["route_id"], "--route-hash", route["route_hash"],
                            "--enable-terminal-commit"]
            env = {**os.environ, "FAKE_TRACE": str(harness.trace),
                   "AGENT_ARTIFACT_ROOT": str(harness.artifact_root),
                   "AGENT_DISPATCH_ATTEMPT_ID": "att-parent"}
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(supervisor_module.sys, "stdin", io.StringIO("initial assignment")), \
                 redirect_stdout(output):
                return_code = supervisor_module.main(command)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        trace = [json.loads(line) for line in harness.trace.read_text(encoding="utf-8").splitlines()]
        census = CHAIN.chain_census(harness.jobs, manifest["chain_id"], count)
        return return_code, trace, calls, output.getvalue(), harness.jobs.read_text(encoding="utf-8"), route, manifest, census, claims
    finally:
        harness.doCleanups()


class SerialChainFlowMixin:
    def test_complete_3_6_16_serial_chains_in_one_driver_checkpoint(self):
        for count in (3, 6, 16):
            with self.subTest(count=count):
                rows = _serial_rows(count)
                steps = iter([
                    ADVANCE.ChainAdvanceStep(
                        "advanced", chain_id="ssc-acceptance",
                        predecessor_index=index, successor_index=index + 1,
                        attempt_id=f"att-{index + 1}",
                    )
                    for index in range(1, count)
                ] + [ADVANCE.ChainAdvanceStep("complete", chain_id="ssc-acceptance")])
                joins = []
                with mock.patch.object(ADVANCE, "advance_chain_step", side_effect=lambda *args: next(steps)):
                    result = ADVANCE.drive_serial_chain(
                        jobs=Path("/unused/jobs.log"), parent_attempt_id="att-owner",
                        attempts={"att-1"}, receipt={"state": "delivered", "children": []},
                        refresh=lambda attempts: [rows[int(next(iter(attempts)).split("-")[-1]) - 1]],
                        join=lambda attempts: joins.append(set(attempts)) or {"state": "delivered", "children": []},
                        reconcile=lambda joined, attempts: False, max_reparks=1,
                    )
                self.assertEqual(len(joins), count - 1)
                self.assertEqual(result.last_advanced_attempt_id, f"att-{count}")
                self.assertEqual(len(result.traversed), count - 1)

    def test_mixed_non_chain_sibling_is_carried_without_second_join(self):
        chain_row = _serial_rows(2)[0]
        sibling = SimpleNamespace(attempt_id="att-sibling", status="done", metadata={})
        steps = iter([
            ADVANCE.ChainAdvanceStep("advanced", chain_id="ssc-acceptance",
                                     predecessor_index=1, successor_index=2,
                                     attempt_id="att-2"),
            ADVANCE.ChainAdvanceStep("complete", chain_id="ssc-acceptance"),
        ])
        joined = {"att-1": chain_row, "att-sibling": sibling}
        joins = []
        with mock.patch.object(ADVANCE, "advance_chain_step", side_effect=lambda *args: next(steps)):
            result = ADVANCE.drive_serial_chain(
                jobs=Path("/unused/jobs.log"), parent_attempt_id="att-owner",
                attempts={"att-1", "att-sibling"},
                receipt={"state": "delivered", "children": [
                    {"attempt_id": "att-1"}, {"attempt_id": "att-sibling"}
                ]},
                refresh=lambda attempts: [joined.get(attempt, SimpleNamespace(
                    attempt_id=attempt, status="done", metadata={
                        "session_chain_id": "ssc-acceptance", "subsession_mode": "serial"
                    })) for attempt in attempts],
                join=lambda attempts: joins.append(set(attempts)) or {
                    "state": "delivered", "children": [{"attempt_id": next(iter(attempts))}]
                },
                reconcile=lambda joined, attempts: False, max_reparks=1,
            )
        self.assertEqual(len(joins), 1)
        self.assertEqual(result.attempts, frozenset({"att-2", "att-sibling"}))
        self.assertEqual({row.attempt_id for row in result.joined_rows}, {"att-2", "att-sibling"})


class RealSerialChainFlowMixin:
    runtime_name = ""

    def _assert_delivered_once(self, result):
        return_code, trace, _calls, output, _registry, _route, _manifest, _census, _claims = result
        self.assertEqual(return_code, 0, output)
        turns = [row for row in trace if row["event"] == "turn-start"]
        delivered = turns[-1]["delivered"]
        self.assertEqual(sorted(delivered), ["att-chain-0001", "att-sibling"])
        self.assertEqual(len(delivered), len(set(delivered)))

    def test_real_final_frontier_mixed_park_delivers_each_attempt_once(self):
        result = _run_real_supervisor_flow(self.runtime_name, 1, mixed=True)
        self._assert_delivered_once(result)

    def test_real_first_step_proof_refusal_mixed_park_delivers_notice_once(self):
        result = _run_real_supervisor_flow(
            self.runtime_name, 1, mixed=True, proof_refusal=True,
        )
        self._assert_delivered_once(result)
        turns = [row for row in result[1] if row["event"] == "turn-start"]
        self.assertIn("advance stopped before", turns[-1]["prompt"])
        self.assertIn("subsession-chain-proof-failed", turns[-1]["prompt"])


class ClaudeSerialChainFlowTest(RealSerialChainFlowMixin, SerialChainFlowMixin, unittest.TestCase):
    runtime_name = "claude"

    def test_real_3_session_supervisor_flow_has_one_final_wake(self):
        result = _run_real_supervisor_flow("claude", 3)
        return_code, trace, calls, output, registry, route, manifest, census, claims = result
        self.assertEqual(return_code, 0, output)
        self.assertEqual([row["event"] for row in trace if row["event"] == "turn-start"],
                         ["turn-start", "turn-start"])
        self.assertEqual(len([row for row in trace if row["event"] == "join-start"]), 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [command[command.index("--action") + 1] for command, _ in calls],
            ["register", "start", "register", "start"],
        )
        start_commands = [command for command, _ in calls if command[command.index("--action") + 1] == "start"]
        self.assertEqual(
            [command[command.index("--slug") + 1] for command in start_commands],
            [session["slug"] for session in manifest["sessions"][1:]],
        )
        self.assertTrue(all(command[command.index("--parent") + 1] == "owner" for command in start_commands))
        for command, env in calls:
            self.assertEqual(command[command.index("--parent") + 1], "owner")
            self.assertEqual(env["AGENT_DISPATCH_ATTEMPT_ID"], "att-parent")
        self.assertEqual(census["runtime_joins"], 1)
        self.assertEqual(census["subsession_advances"], 2)
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual(len([row for row in events if row.get("type") == "dispatch.supervisor.resumed"]), 1)

    def test_adapter_cycle_16_session_chain_advances_without_internal_wakes(self):
        return_code, trace, calls, output, registry, route, manifest, census, claims = _run_real_supervisor_flow("claude", 16)
        self.assertEqual(return_code, 0, output)
        self.assertEqual(len([row for row in trace if row["event"] == "turn-start"]), 2)
        self.assertEqual(len([row for row in trace if row["event"] == "join-start"]), 16)
        self.assertEqual(len(calls), 30)
        self.assertEqual(census["subsession_advances"], 15)
        self.assertEqual(census["runtime_joins"], 1)
        self.assertEqual(
            [command[command.index("--adapter") + 1] for command, _ in calls if command[command.index("--action") + 1] == "start"],
            [("claude", "codex", "opencode")[(index - 1) % 3] for index in range(2, 17)],
        )
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual(len([row for row in events if row.get("type") == "dispatch.supervisor.resumed"]), 1)

    def test_mixed_park_terminal_commit_claims_full_delivered_set(self):
        result = _run_real_supervisor_flow("claude", 2, mixed=True, terminal=True)
        return_code, trace, calls, output, registry, route, manifest, census, claims = result
        self.assertEqual(return_code, 0, output)
        self.assertEqual(len(calls), 2)
        self.assertEqual(claims[0], ("att-chain-0001", "att-sibling"))
        self.assertEqual(claims[1], ("att-chain-0002", "att-sibling"))


class CodexSerialChainFlowTest(RealSerialChainFlowMixin, SerialChainFlowMixin, unittest.TestCase):
    runtime_name = "codex"

    def test_real_3_session_codex_flow_has_one_final_wake(self):
        return_code, trace, calls, output, registry, route, manifest, census, claims = _run_real_supervisor_flow("codex", 3)
        self.assertEqual(return_code, 0, output)
        self.assertEqual(len([row for row in trace if row["event"] == "turn-start"]), 2)
        self.assertEqual(len([row for row in trace if row["event"] == "join-start"]), 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [command[command.index("--action") + 1] for command, _ in calls],
            ["register", "start", "register", "start"],
        )
        self.assertTrue(all(command[command.index("--parent") + 1] == "owner" for command, _ in calls))
        self.assertEqual(
            [command[command.index("--slug") + 1] for command, _ in calls if command[command.index("--action") + 1] == "start"],
            [session["slug"] for session in manifest["sessions"][1:]],
        )
        self.assertEqual(census["runtime_joins"], 1)
        self.assertEqual(census["subsession_advances"], 2)
        events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        self.assertEqual(len([row for row in events if row.get("type") == "dispatch.supervisor.resumed"]), 1)

    def test_adapter_cycle_16_session_chain_advances_without_internal_wakes(self):
        return_code, trace, calls, output, registry, route, manifest, census, claims = _run_real_supervisor_flow("codex", 16)
        self.assertEqual(return_code, 0, output)
        self.assertEqual(len([row for row in trace if row["event"] == "turn-start"]), 2)
        self.assertEqual(len([row for row in trace if row["event"] == "join-start"]), 16)
        self.assertEqual(len(calls), 30)
        self.assertEqual(census["subsession_advances"], 15)
        self.assertEqual(census["runtime_joins"], 1)
        self.assertEqual(
            [command[command.index("--adapter") + 1] for command, _ in calls if command[command.index("--action") + 1] == "start"],
            [("claude", "codex", "opencode")[(index - 1) % 3] for index in range(2, 17)],
        )


class SupervisorEdgeAcceptanceTest(unittest.TestCase):
    def _harness(self, runtime: str):
        module = CLAUDE_HARNESS if runtime == "claude" else CODEX_HARNESS
        cls = module.ClaudeSessionSupervisorTest if runtime == "claude" else module.CodexAppServerSupervisorTest
        name = (
            "test_resume_uses_same_session_once_after_join"
            if runtime == "claude"
            else "test_runtime_wait_has_no_model_activity_until_exact_join_is_ready"
        )
        harness = cls(name)
        harness.setUp()
        return harness

    @staticmethod
    def _refusal_row():
        return (
            "2026-09-10T00:00:01Z\tdone\t/repo\t/wt\trefused\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,launch_claimed=0,"
            "launch_outcome=never-launched,launch_started=0,"
            "classifier_source=subsession-chain-refusal-v1,failure_class=cancelled,"
            "note=subsession-chain-advance-refused,attempt_id=att-refused,"
            "parent_attempt_id=att-parent\n"
        )

    def test_refusal_settled_only_with_and_without_runtime_wait_never_joins(self):
        for runtime in ("claude", "codex"):
            for waits in (False, True):
                with self.subTest(runtime=runtime, waits=waits):
                    harness = self._harness(runtime)
                    try:
                        owner = (
                            CLAUDE_HARNESS.owner_row(harness.lease)
                            if runtime == "claude" else CODEX_HARNESS.owner_row(harness.lease)
                        )
                        harness.jobs.write_text(owner + self._refusal_row(), encoding="utf-8")
                        result = harness.run_supervisor(**({} if waits else {"FAKE_NO_CHILD": "1"}))
                        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                        trace = [
                            json.loads(line)
                            for line in harness.trace.read_text(encoding="utf-8").splitlines()
                        ]
                        self.assertFalse(any(row["event"] == "join-start" for row in trace))
                        if waits:
                            self.assertEqual(
                                len([row for row in trace if row["event"] == "turn-start"]), 2
                            )
                    finally:
                        harness.doCleanups()

    def test_repeated_deadlines_preserve_both_supervisors(self):
        verdicts = {}
        for runtime in ("claude", "codex"):
            harness = self._harness(runtime)
            try:
                owner = (
                    CLAUDE_HARNESS.owner_row(harness.lease)
                    if runtime == "claude" else CODEX_HARNESS.owner_row(harness.lease)
                )
                child = CLAUDE_HARNESS.child_row() if runtime == "claude" else CODEX_HARNESS.child_row()
                harness.jobs.write_text(owner + child, encoding="utf-8")
                timeout_join = harness.base / "always_timeout.py"
                timeout_join.write_text(textwrap.dedent("""
                    import json, sys
                    parent = sys.argv[sys.argv.index('--parent-attempt-id') + 1]
                    attempts = [sys.argv[i + 1] for i, value in enumerate(sys.argv) if value == '--attempt-id']
                    print(json.dumps({'schema_version': 2, 'state': 'timeout', 'parent_attempt_id': parent,
                        'children': [{'attempt_id': attempt, 'status': 'open', 'readiness': 'pending',
                                      'reason': 'process-alive', 'required_action': 'complete-open'}
                                     for attempt in attempts]}))
                """), encoding="utf-8")
                command = harness.command_with_join(timeout_join) + ["--max-join-reparks", "1"]
                env = (
                    harness.child_env(FAKE_TRACE=str(harness.trace))
                    if runtime == "claude" else {
                        **os.environ, "FAKE_TRACE": str(harness.trace),
                        "AGENT_ARTIFACT_ROOT": str(harness.artifact_root),
                    }
                )
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, env=env)
                try:
                    process.stdin.write("initial assignment")
                    process.stdin.close()
                    process.stdin = None
                    time.sleep(1.5)
                    self.assertIsNone(process.poll(), runtime)
                finally:
                    process.terminate()
                    out, err = process.communicate(timeout=5)
                verdicts[runtime] = out + err
                self.assertIn("dispatch.supervisor.reparked", out)
                self.assertNotIn("join-timeout-repark-exceeded", verdicts[runtime])
            finally:
                harness.doCleanups()
        self.assertEqual(
            ["join-timeout-repark-exceeded" in verdicts[name] for name in ("claude", "codex")],
            [False, False],
        )


class DispatchNodeMaterializationTest(unittest.TestCase):
    def test_real_successor_service_captures_only_run_checked_for_both_supervisors(self):
        for adapter in ("claude", "codex"):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as td:
                base = Path(td)
                brief = base / "brief.md"
                brief.write_text("brief\n", encoding="utf-8")
                session = {
                    "index": 2, "count": 3, "adapter": adapter,
                    "subsession_id": "ss-real-2", "attempt_id": "att-real-2",
                    "slug": "execute-s2", "phase_brief": str(brief),
                    "fixed_files": ["utilities/example.py"], "narrow_verify": "true",
                    "expected_round_trips": 2,
                }
                request = ADVANCE.SubsessionAdvanceRequest(
                    jobs=base / "jobs.log", route_id="rt-real", route_hash="sha256:real",
                    route_node="execute", chain_id="ssc-real", manifest_sha256="sha256:manifest",
                    predecessor_subsession_id="ss-real-1",
                    predecessor_terminal_attempt_id="att-owner", successor_subsession_index=2,
                    successor_session=session, parent_attempt_id="att-owner",
                    parent_slug="owner-slug", registered_parent_sid="owner-session",
                    registered_parent_cwd=str(base),
                )
                service = ADVANCE.RealSubsessionAdvanceServices({
                    "route_file": str(base / "route.json"), "route_node": "execute",
                    "mode": "serial", "chain_id": "ssc-real",
                })
                calls = []
                def run_checked(command, env=None):
                    calls.append((command, env))
                    return SimpleNamespace(
                        returncode=0,
                        stdout=("check=ok\nattempt_id=att-real-2\nregistered=1\nstarted=1\n"
                                "duplicate_attempt=0\nchild_spawned=1\n"), stderr="",
                    )
                service._chain.run_checked = run_checked
                with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-owner"}, clear=False):
                    result = service.start_successor(request, claim=None)
                self.assertTrue(result["child_spawned"])
                self.assertEqual(len(calls), 1)
                command, env = calls[0]
                self.assertEqual(command[command.index("--parent") + 1], "owner-slug")
                self.assertEqual(command[command.index("--adapter") + 1], adapter)
                self.assertEqual(env["AGENT_DISPATCH_PARENT_SESSION_ID"], "owner-session")
                self.assertEqual(env["AGENT_DISPATCH_PARENT_CWD"], str(base))


class ParentBindingTest(unittest.TestCase):
    def test_foreign_parent_is_refused_before_run_checked(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            request = SimpleNamespace(
                parent_attempt_id="att-owner", parent_slug="owner-slug",
                successor_session={"adapter": "claude"}
            )
            service = ADVANCE.RealSubsessionAdvanceServices({})
            with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "foreign"}, clear=False), \
                 mock.patch.object(service._chain, "run_checked") as run:
                result = service.start_successor(request, claim=None)
            self.assertEqual(result["reason"], "subsession-advance-parent-binding-invalid")
            run.assert_not_called()


class SuccessorIdentityTest(DispatchNodeMaterializationTest):
    pass


class SuccessorStartVerdictTest(unittest.TestCase):
    def test_malformed_and_foreign_receipts_are_not_success(self):
        for output in ("", "check=ok\nattempt_id=foreign\n"):
            self.assertFalse(CHAIN.parse_start_receipt(0, output, "att-real")["ok"])


class ProveSerialChainTest(unittest.TestCase):
    def test_registry_snapshot_is_lock_scoped_and_ignores_torn_lines(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            harness = SimpleNamespace(base=base, artifact_root=base / ".agent_reports", jobs=base / "jobs.log", lease=base / "lease")
            harness.artifact_root.mkdir()
            harness.jobs.touch()
            route, manifest = _real_chain_fixture(harness, CLAUDE, 3)
            # The proof must read one lock-held snapshot and ignore only the
            # torn final append, not discard the complete chain.
            harness.jobs.write_text(harness.jobs.read_text(encoding="utf-8") + "torn-final", encoding="utf-8")
            proof = ADVANCE.prove_serial_chain(harness.jobs, manifest["chain_id"], parent_attempt_id="att-parent")
            self.assertIsInstance(proof, ADVANCE.ProvenSerialChain)
            self.assertEqual(tuple(proof.rows_by_index), (1, 2, 3))


class SerialChainFrontierTest(unittest.TestCase):
    def test_frontier_proof_is_required_before_pending_rows_are_exempt(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            harness = SimpleNamespace(base=base, artifact_root=base / ".agent_reports", jobs=base / "jobs.log", lease=base / "lease")
            harness.artifact_root.mkdir()
            harness.jobs.touch()
            route, manifest = _real_chain_fixture(harness, CLAUDE, 3)
            rows = [
                JOIN.ChildRow(1, "open", f"execute-s{index}", f"att-chain-{index:04d}", "", CONTRACT.parse_registry_metadata(
                    harness.jobs.read_text(encoding="utf-8").splitlines()[index + 0].split("\t")[-1]
                ))
                for index in (1, 2, 3)
            ]
            frontiers = ADVANCE.serial_chain_frontiers(
                harness.jobs, "att-parent", rows, {row.attempt_id for row in rows}
            )
            self.assertEqual(len(frontiers), 1)
            self.assertEqual(frontiers[0].frontier_index, 1)
            self.assertEqual(frontiers[0].pending_attempt_ids, ("att-chain-0002", "att-chain-0003"))


class RefusedChainCloseTest(unittest.TestCase):
    def _fields(self, attempt: str, **values):
        metadata = {"attempt_id": attempt, "launch_started": "0", "launch_claimed": "0"}
        metadata.update(values)
        encoded = ",".join(f"{key}={value}" for key, value in metadata.items())
        return ["ts", "open", "/repo", "/wt", "slice", encoded]

    def test_reaped_claimed_pid_and_started_rows_are_not_never_started(self):
        self.assertTrue(CONTRACT.attempt_row_never_started(self._fields("att-open")))
        self.assertFalse(CONTRACT.attempt_row_never_started(
            self._fields("att-reaped", launch_outcome="reaped-before-publish")
        ))
        self.assertFalse(CONTRACT.attempt_row_never_started(
            self._fields("att-claimed", launch_claimed="1", launch_outcome="never-launched")
        ))
        self.assertFalse(CONTRACT.attempt_row_never_started(
            self._fields("att-pid", pid="123", launch_outcome="never-launched")
        ))
        self.assertFalse(CONTRACT.attempt_row_never_started(
            self._fields("att-started", launch_started="1")
        ))


class OwnerSupervisionProbeTest(unittest.TestCase):
    def test_probe_reason_mapping_is_shared_with_initial_start(self):
        self.assertEqual(CHAIN._supervision_refusal_reason("unsupervised"),
                         "subsession-chain-advance-unsupervised")
        self.assertEqual(CHAIN._supervision_refusal_reason("unproven"),
                         "subsession-chain-advance-supervision-unproven")


class SubsessionDeliveryClassificationTest(unittest.TestCase):
    def test_completed_subsession_classification_is_additive(self):
        state = JOIN.CurrentDeliveryState(
            marker=None, marker_digest="", row_revision="r", row_digest="d",
            status="done", verdict="PASS", quiescent=True, owned_children=0,
            advanced=False, completion_proven=True,
        )
        self.assertEqual(JOIN.delivery_classification(state), "success")


class DriveSerialChainTest(SerialChainFlowMixin, unittest.TestCase):
    pass


class ChainSerialRegisterAtomicityTest(unittest.TestCase):
    def test_guard_applies_to_every_serial_chain_with_two_or_more_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            harness = SimpleNamespace(base=base, artifact_root=base / ".agent_reports", jobs=base / "jobs.log", lease=base / "lease")
            harness.artifact_root.mkdir()
            harness.jobs.touch()
            route, manifest = _real_chain_fixture(harness, CLAUDE, 2)
            with mock.patch.object(CHAIN, "resolve_global_registry", return_value=SimpleNamespace(path=harness.jobs)), \
                 mock.patch.object(CHAIN, "probe_owner_supervision", return_value=CHAIN.SupervisionProbe("unsupervised", "fixture")), \
                 mock.patch.object(CHAIN.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                 mock.patch.object(CHAIN, "run_checked") as dispatch, \
                 mock.patch.object(CHAIN, "require_current_cleanup", create=True), \
                 mock.patch.object(CHAIN.sys, "argv", ["stage-session-chain.py", "register", "--manifest", str(base / "chain.json"), "--parent", "owner", "--jobs", str(harness.jobs)]):
                result = CHAIN.main()
            self.assertEqual(result, 65)
            dispatch.assert_not_called()


class RuntimeWaitPartitionTest(unittest.TestCase):
    def _row(self, attempt: str, *, refusal: bool = False, claimed: str = "0"):
        metadata = {
            "attempt_id": attempt, "launch_started": "0",
            "launch_claimed": claimed,
        }
        if refusal:
            metadata.update({
                "classifier_source": "subsession-chain-refusal-v1",
                "launch_outcome": "never-launched",
            })
        return JOIN.ChildRow(1, "done", attempt, attempt, "", metadata)

    def test_only_refusal_settled_is_not_joinable_and_claimed_refusal_is_retained(self):
        rows = [self._row("att-refused", refusal=True), self._row("att-claimed", refusal=True, claimed="1")]
        with mock.patch.object(JOIN.subsession_advance, "serial_chain_frontiers", return_value=()):
            partition = JOIN.partition_runtime_wait_children(
                Path("/unused/jobs.log"), "att-owner", rows,
                {"att-refused", "att-claimed"},
            )
        self.assertEqual(partition.refusal_settled, frozenset({"att-refused"}))
        self.assertEqual(partition.joinable, frozenset({"att-claimed"}))


class SerialLengthContractTest(unittest.TestCase):
    def _manifest(self, base: Path, mode: str, count: int) -> Path:
        worktree = base / "worktree"
        (worktree / "source").mkdir(parents=True)
        route = {
            "route_id": "rt-length", "route_hash": "sha256:" + "1" * 64,
            "cwd": str(worktree),
            "nodes": [{"id": "execute", "completion_gate": "code-execute",
                        "write_scope": ["source/**"],
                        "subdivision": {"max_slices": 4, "disjointness": "exact-fixed-files"}}],
        }
        route_path = base / "route.json"
        route_path.write_text(json.dumps(route), encoding="utf-8")
        sessions = []
        for index in range(1, count + 1):
            brief = base / f"brief-{index}.md"
            brief.write_text("brief\n", encoding="utf-8")
            file = worktree / "source" / f"slice-{index}.py"
            file.write_text("# slice\n", encoding="utf-8")
            sessions.append({
                "subsession_id": f"ss-length-{index}", "attempt_id": f"att-length-{index}",
                "adapter": "claude", "slug": f"slice-{index}",
                "phase_brief": str(brief), "narrow_verify": "true",
                "expected_round_trips": 1, "fixed_files": [str(file)],
            })
        path = base / f"{mode}-{count}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "kind": "stage-session-chain", "chain_id": f"ssc-length-{mode}",
            "mode": mode, "worktree": str(worktree), "route_file": str(route_path),
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "route_node": "execute", "completion_gate": "code-execute", "sessions": sessions,
        }), encoding="utf-8")
        return path

    def test_serial_1_through_16_are_valid_and_parallel_cap_remains_4(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for count in range(1, 17):
                path = self._manifest(base / f"s{count}", "serial", count)
                route = json.loads((path.parent / "route.json").read_text())
                route["_route_file"] = str(path.parent / "route.json")
                node = route["nodes"][0]
                self.assertEqual(len(load_manifest(path, route=route, node=node)["sessions"]), count)
            path = self._manifest(base / "parallel", "parallel", 5)
            route = json.loads((path.parent / "route.json").read_text())
            route["_route_file"] = str(path.parent / "route.json")
            node = route["nodes"][0]
            with self.assertRaisesRegex(StageSessionError, "parallel-session-count-invalid"):
                load_manifest(path, route=route, node=node)


class InitialStartTransactionTest(unittest.TestCase):
    def test_real_adapter_receipts_are_strict_and_initial_start_never_waits(self):
        success = "check=ok\nattempt_id=att-first\nregistered=1\nstarted=1\nduplicate_attempt=0\nchild_spawned=1\n"
        for adapter in ("claude", "codex", "opencode"):
            with self.subTest(adapter=adapter):
                self.assertTrue(CHAIN.parse_start_receipt(0, success, "att-first")["ok"])
                self.assertEqual(CHAIN.parse_start_receipt(0, success.replace("child_spawned=1", "child_spawned=0"), "att-first")["verdict"], "not-spawned")
        self.assertNotIn("runtime_wait", CHAIN.parse_start_receipt(65, "reason=failed\n", "att-first"))


if __name__ == "__main__":
    unittest.main()
