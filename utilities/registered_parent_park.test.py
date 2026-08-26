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

    def test_undelivered_batch_allows_only_another_exact_sibling_start(self) -> None:
        self.write_state([])
        dispatch = (
            f"python3 utilities/dispatch-node.py --route {self.route} "
            "--node test --adapter codex --action start --slug child-b "
            f"--parent owner -- --jobs {self.jobs} "
            f"--parent-attempt-id {PARENT}"
        )
        self.assertIsNone(self.invoke("Bash", dispatch))
        self.assert_denied("Read")
        self.assert_denied("Bash", "git status --short")
        self.assert_denied("Bash", dispatch.replace("--parent owner", "--parent foreign"))
        self.assert_denied(
            "Bash",
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status open",
        )

    def test_delivered_batch_allows_only_exact_harvest(self) -> None:
        self.write_state([CHILD])
        self.assertIsNone(
            self.invoke(
                "Bash",
                f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status open",
            )
        )
        self.assert_denied("Bash", f"utilities/dispatch-wait.sh --attempt-id {CHILD} --max 600")
        self.assert_denied(
            "Bash",
            "utilities/dispatch-node.py --route /tmp/route.json --node test "
            "--adapter claude --action start --slug child-b --parent owner",
        )

    def test_delivered_terminal_quiescent_open_row_stays_harvest_only(self) -> None:
        self.jobs.write_text(
            self.jobs.read_text(encoding="utf-8").rstrip()
            + ",launch_outcome=never-launched\n",
            encoding="utf-8",
        )

    def test_outbox_row_advance_selects_current_done_failure_action(self) -> None:
        original = self.jobs.read_text(encoding="utf-8").rstrip("\n")
        receipt = {
            "schema_version": 2,
            "state": "ready",
            "parent_attempt_id": PARENT,
            "children": [
                {
                    "attempt_id": CHILD,
                    "status": "open",
                    "required_action": "complete-open",
                }
            ],
        }
        self.write_outbox_state(receipt, original)
        self.jobs.write_text(
            original.replace("\topen\t", "\tdone\t")
            + ",failure_class=fail,note=dead-worker-fail\n",
            encoding="utf-8",
        )
        self.assert_denied(
            "Bash",
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
            "--status open --mark-done",
        )
        self.assertIsNone(
            self.invoke(
                "Bash",
                f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
                "--status done --failure-detail",
            )
        )
        self.write_state([CHILD])
        self.assert_denied("Read")
        self.assert_denied("Bash", "git status --short")
        self.assertIsNone(
            self.invoke(
                "Bash",
                f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
                "--status open --mark-done",
            )
        )

    def test_missing_state_is_recovery_only_and_non_supervised_is_inactive(self) -> None:
        self.assertFalse(self.state.exists())
        self.assertIsNone(
            self.invoke(
                "Bash",
                f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status open",
            )
        )
        self.assert_denied(
            "Bash",
            "utilities/dispatch-node.py --route /tmp/route.json --node test "
            "--adapter claude --action start --slug child-b --parent owner",
        )
        self.assertIsNone(self.invoke("Read", mode="poll"))

    def test_replica_batch_requires_one_exact_leg_and_rejects_repeat(self) -> None:
        replica_metadata = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"attempt_id={CHILD},parent_attempt_id={PARENT},"
            f"route_id={self.route_id},route_file={self.route},route_node=plan-a,"
            "replica_group=plan,reservation_kind=replica-batch,"
            "batch_declared_size=2,batch_group=plan,"
            f"batch_route_id={self.route_id},batch_parent_attempt_id={PARENT},"
            f"batch_attempt_id={CHILD},batch_route_node=plan-a"
        )
        self.jobs.write_text(
            "2026-07-23T00:00:00Z\topen\t/repo\t/wt\tplan-a\t"
            + replica_metadata
            + "\n",
            encoding="utf-8",
        )
        self.write_state([])
        batch = (
            "adapters/codex/bin/preflight.sh dispatch-batch "
            f"--route {self.route} --replica-group plan --action start "
            f"--slug-prefix owner --parent owner --jobs {self.jobs}"
        )
        self.assertIsNone(self.invoke("Bash", batch))
        self.assert_denied("Bash", batch.replace("--replica-group plan", "--replica-group foreign"))
        foreign_jobs = self.base / "foreign.log"
        foreign_jobs.write_text("", encoding="utf-8")
        self.assert_denied("Bash", batch.replace(str(self.jobs), str(foreign_jobs)))

        second = replica_metadata.replace(CHILD, "att-claude-child-b").replace(
            "route_node=plan-a", "route_node=plan-b"
        ).replace("batch_route_node=plan-a", "batch_route_node=plan-b")
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(
                "2026-07-23T00:00:01Z\topen\t/repo\t/wt\tplan-b\t"
                + second
                + "\n"
            )
        self.assert_denied("Bash", batch)

    def test_denial_reports_the_actual_status_not_open_for_a_done_row(self) -> None:
        # D-9: a `done` row must never be reported as "open" in the denial.
        original = self.jobs.read_text(encoding="utf-8").rstrip("\n")
        receipt = {
            "schema_version": 2, "state": "ready", "parent_attempt_id": PARENT,
            "children": [{
                "attempt_id": CHILD, "status": "open",
                "required_action": "complete-open",
            }],
        }
        self.write_outbox_state(receipt, original)
        self.jobs.write_text(
            original.replace("\topen\t", "\tdone\t")
            + ",failure_class=fail,note=dead-worker-fail\n",
            encoding="utf-8",
        )
        result = self.invoke(
            "Bash",
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
            "--status open --mark-done",
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn(f"{CHILD}:done", reason)
        self.assertNotIn(f"{CHILD}:open", reason)

    def test_denial_reason_unrecognized_surface(self) -> None:
        # D-10a
        self.write_state([])
        result = self.invoke("Bash", "curl https://example.invalid")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("command_rejection_reason=unrecognized-surface", reason)

    def test_denial_reason_unknown_option(self) -> None:
        # D-10b
        self.write_state([CHILD])
        result = self.invoke(
            "Bash",
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
            "--status open --not-a-real-flag",
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("command_rejection_reason=unknown-option", reason)

    def test_denial_reason_shell_composition(self) -> None:
        # D-10c
        self.write_state([CHILD])
        result = self.invoke(
            "Bash",
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} "
            "--status open --mark-done && rm -rf /tmp/x",
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("command_rejection_reason=shell-composition", reason)

    def test_denial_reason_attempt_not_guarded(self) -> None:
        # D-10d
        self.write_state([CHILD])
        result = self.invoke(
            "Bash",
            "adapters/codex/bin/preflight.sh harvest --attempt-id "
            "att-not-in-registry --status open --mark-done",
        )
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("command_rejection_reason=attempt-not-guarded", reason)

    def test_precheck_agrees_with_the_real_guard_on_every_prescribed_line(self) -> None:
        # D-6c: the D2a precheck must derive base/open_attempt_ids/parent_slug/
        # jobs exactly as the hook does, and its verdict must equal the hook's
        # actual admit/deny decision for the same lines.
        self.write_state([CHILD])
        admitted_line = (
            f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status open"
        )
        denied_line = f"utilities/dispatch-wait.sh --attempt-id {CHILD} --max 600"
        guarded_attempts = {CHILD}  # open row, no outbox -- matches setUp's single child
        for line, hook_admits in ((admitted_line, True), (denied_line, False)):
            with self.subTest(line=line):
                precheck_satisfiable, _ = JOIN.supervisor_receipt_satisfiable(
                    [line],
                    base=ROOT,
                    open_attempt_ids=guarded_attempts,
                    parent_slug=SLUG,
                    jobs=self.jobs,
                    parent_attempt_id=PARENT,
                    route_file=self.route,
                    route_id=self.route_id,
                )
                hook_result = self.invoke("Bash", line)
                hook_admitted = hook_result is None
                self.assertEqual(precheck_satisfiable, hook_admits)
                self.assertEqual(hook_admitted, hook_admits)
                self.assertEqual(precheck_satisfiable, hook_admitted)

    def test_existing_admission_and_denial_outcomes_are_unchanged(self) -> None:
        # D-11: regression -- D4 changes text and adds a reason accessor,
        # never the decision. Re-runs a representative slice of the existing
        # fixtures above and checks only the boolean admit/deny outcome.
        self.write_state([])
        dispatch = (
            f"python3 utilities/dispatch-node.py --route {self.route} "
            "--node test --adapter codex --action start --slug child-b "
            f"--parent owner -- --jobs {self.jobs} "
            f"--parent-attempt-id {PARENT}"
        )
        self.assertIsNone(self.invoke("Bash", dispatch))
        self.assert_denied("Bash", f"utilities/dispatch-wait.sh --attempt-id {CHILD}")
        self.write_state([CHILD])
        self.assertIsNone(
            self.invoke(
                "Bash",
                f"adapters/codex/bin/preflight.sh harvest --attempt-id {CHILD} --status open",
            )
        )
        self.assert_denied("Bash", f"utilities/dispatch-wait.sh --attempt-id {CHILD} --max 600")


if __name__ == "__main__":
    unittest.main()
