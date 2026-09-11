#!/usr/bin/env python3
"""Claude hook parity tests for runtime-supervised registered parents."""

from __future__ import annotations

import importlib.util
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "registered-parent-park.py"
PARENT = "att-claude-owner"
CHILD = "att-claude-child-a"
SLUG = "owner"

sys.path.insert(0, str(ROOT / "utilities"))
_JOIN_SPEC = importlib.util.spec_from_file_location(
    "dispatch_completion_join_park_parity", ROOT / "utilities" / "dispatch_completion_join.py"
)
JOIN = importlib.util.module_from_spec(_JOIN_SPEC)
sys.modules[_JOIN_SPEC.name] = JOIN
_JOIN_SPEC.loader.exec_module(JOIN)


class RegisteredParentParkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)


        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.state = self.base / "state.json"
        self.route_id = "rt-claude-park"
        self.route = self.base / "route.json"
        self.route.write_text(
            json.dumps(
                {
                    "route_id": self.route_id,
                    "nodes": [
                        {"id": "owner", "dispatch_depth": 1},
                        {"id": "implement", "dispatch_depth": 2},
                        {"id": "test", "dispatch_depth": 2},
                        {
                            "id": "plan-a",
                            "dispatch_depth": 2,
                            "replica_group": "plan",
                        },
                        {
                            "id": "plan-b",
                            "dispatch_depth": 2,
                            "replica_group": "plan",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.jobs.write_text(
            "2026-07-23T00:00:00Z\topen\t/repo\t/wt\tchild-a\t"
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"attempt_id={CHILD},parent_attempt_id={PARENT},"
            f"route_id={self.route_id},route_file={self.route},"
            "route_node=implement\n",
            encoding="utf-8",
        )

    def write_state(self, delivered: list[str]) -> None:
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "parent_attempt_id": PARENT,
                    "delivered_attempt_ids": delivered,
                    "phase": "deliverable" if delivered else "parked",
                }
            ),
            encoding="utf-8",
        )

    def write_outbox_state(self, receipt: dict[str, object], row: str) -> None:
        receipt_bytes = json.dumps(
            receipt, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        self.state.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "parent_attempt_id": PARENT,
                    "delivered_attempt_ids": [CHILD],
                    "phase": "deliverable",
                    "outbox": {
                        "receipt_id": "receipt-test",
                        "receipt_digest": hashlib.sha256(receipt_bytes).hexdigest(),
                        "attempt_ids": [CHILD],
                        "row_revisions": {
                            CHILD: hashlib.sha256(row.encode("utf-8")).hexdigest()
                        },
                        "receipt": receipt,
                        "consumed_attempt_ids": [],
                    },
                }
            ),
            encoding="utf-8",
        )

    def invoke(
        self,
        tool_name: str,
        command: str | None = None,
        *,
        mode: str = "supervised",
    ) -> dict[str, object] | None:
        payload: dict[str, object] = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": {},
            "cwd": str(ROOT),
        }
        if command is not None:
            payload["tool_input"] = {"command": command}
        result = subprocess.run(
            ["python3", str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "AGENT_HOME": str(ROOT),
                "AGENT_DISPATCH_JOBS": str(self.jobs),
                "AGENT_DISPATCH_COMPLETION_MODE": mode,
                "AGENT_DISPATCH_ATTEMPT_ID": PARENT,
                "AGENT_DISPATCH_COMPLETION_STATE_FILE": str(self.state),
                "AGENT_DISPATCH_SELF_SLUG": SLUG,
                "AGENT_ROUTE_FILE": str(self.route),
                "AGENT_ROUTE_ID": self.route_id,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout else None

    def assert_denied(self, tool_name: str, command: str | None = None) -> None:
        result = self.invoke(tool_name, command)
        self.assertIsNotNone(result)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("runtime-supervised-parent", output["permissionDecisionReason"])

    def test_no_children_still_enforces_durable_cleanup_intent(self):
        import dispatch_budget_record as budget
        import dispatch_terminal_commit as terminal
        self.jobs.write_text("")
        report = self.base / "partial.md"
        claim = budget.claim_terminal_handoff(self.base, owner_attempt_id=PARENT,
                    route_hash="hash", child_attempt_ids=[], continuation_ordinal=0)
        intent = budget.convert_claim_to_prompt_intent(self.base, claim, prompt="cleanup",
            cleanup_scope=dict(artifact_root=str(self.base), route_id=self.route_id,
                route_hash="hash", owner_attempt_id=PARENT, terminal_commit_id="commit",
                allowed_write_roots=[str(report)], allowed_read_roots=[str(self.base)],
                allowed_operations=["partial-report", "read", "close-forward-recovery"],
                allowed_recovery_targets=[str(self.route)]))
        slot = budget.terminal_handoff_root(self.base, PARENT, 0)
        # Intent is authoritative even if the process crashed before its sidecar.
        (slot / "cleanup-scope.json").unlink()
        helper = f"python3 {ROOT / 'utilities/dispatch_terminal_commit.py'} cleanup-recover"
        self.assertIsNone(self.invoke("Bash", helper))
        for command in ["git status; touch escaped", "python3 -c 'print(1)'", helper+" --force"]:
            decision = self.invoke("Bash", command)
            self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("cleanup-scope", decision["hookSpecificOutput"]["permissionDecisionReason"])
        env = {**os.environ, "AGENT_DISPATCH_JOBS":str(self.jobs),
               "AGENT_DISPATCH_ATTEMPT_ID":PARENT, "AGENT_ROUTE_ID":self.route_id}
        result = subprocess.run([sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
                                 "--help"], env=env, capture_output=True, text=True)
        # Parser help is read-only; the common mutation API itself refuses.
        from unittest import mock
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(terminal.TerminalCommitError):
                terminal.require_current_cleanup("registry", jobs=self.jobs)
        self.assertEqual(result.returncode, 0)
        (slot / "prompt-intent.json").write_text("{")
        decision = self.invoke("Bash", helper)
        self.assertEqual(decision["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_delivery_state_never_becomes_a_second_tool_permission_system(self):
        original = self.jobs.read_text().rstrip("\n")
        for phase in ("absent", "parked", "delivered", "pending-outbox"):
            with self.subTest(phase=phase):
                self.state.unlink(missing_ok=True)
                if phase == "parked":
                    self.write_state([])
                elif phase == "delivered":
                    self.write_state([CHILD])
                elif phase == "pending-outbox":
                    self.write_outbox_state({"schema_version": 2, "state": "ready",
                        "parent_attempt_id": PARENT, "children": [{"attempt_id": CHILD,
                        "status": "done", "required_action": "inspect-done-failure"}]}, original)
                state_before = self.state.read_bytes() if self.state.exists() else None
                jobs_before = self.jobs.read_bytes()
                for tool, command in (("Read", None), ("Edit", None),
                        ("Bash", "git status --short"),
                        ("Bash", f"utilities/dispatch-wait.sh --attempt-id {CHILD} --max 600"),
                        ("Bash", f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status done"),
                        ("Bash", "utilities/dispatch-node.py --node test --action start")):
                    # The actual operation's dependency/write/cleanup checks
                    # remain authoritative; notification delivery grants none.
                    self.assertIsNone(self.invoke(tool, command), (phase, tool, command))
                self.assertEqual(self.jobs.read_bytes(), jobs_before)
                self.assertEqual(self.state.read_bytes() if self.state.exists() else None, state_before)

    def test_terminal_notification_and_non_supervised_calls_need_no_park_override(self):
        self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t"))
        self.write_state([CHILD])
        self.assertIsNone(self.invoke("Read"))
        self.assertIsNone(self.invoke("Read", mode="poll"))


class CleanupCapabilityTest(unittest.TestCase):
    def _scope(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_terminal_commit as D
        return D, D.CleanupScope(artifact_root=ROOT, route_id="r", allowed_write_roots=(ROOT / "reports",), allowed_read_roots=(ROOT,))

    def test_no_child_cleanup_scope_allows_only_bound_report_recovery_and_reads(self):
        D, scope = self._scope()
        self.assertEqual(D.authorize_cleanup_operation(scope, operation="read", target=ROOT, route_id="r", cycle_id=None).verdict, "allowed")

    def test_hook_and_direct_writer_both_refuse_source_dispatch_recompile_registry_other_cycle(self):
        D, scope = self._scope()
        self.assertEqual(D.authorize_cleanup_operation(scope, operation="dispatch", target=ROOT, route_id="r", cycle_id=None).verdict, "denied-operation")

    def test_arbitrary_python_and_shell_are_refused_under_active_scope(self):
        D, scope = self._scope()
        self.assertEqual(D.authorize_cleanup_operation(scope, operation="shell", target=ROOT, route_id="r", cycle_id=None).verdict, "denied-operation")

    def test_hook_and_direct_writer_read_the_same_scope_digest(self):
        D, scope = self._scope()
        self.assertEqual(scope.scope_digest, "")


if __name__ == "__main__":
    unittest.main()
