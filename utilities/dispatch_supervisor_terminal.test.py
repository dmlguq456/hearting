#!/usr/bin/env python3
"""Receipt -> guard -> harvest integration regression for SD-97."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dispatch_completion_join_e2e", ROOT / "utilities" / "dispatch_completion_join.py"
)
JOIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = JOIN
SPEC.loader.exec_module(JOIN)
HARVEST = ROOT / "adapters" / "codex" / "bin" / "dispatch-harvest.py"

SUPERVISOR_SPEC = importlib.util.spec_from_file_location(
    "dispatch_supervisor_terminal", ROOT / "utilities" / "dispatch_supervisor_terminal.py"
)
SUPERVISOR = importlib.util.module_from_spec(SUPERVISOR_SPEC)
sys.modules[SUPERVISOR_SPEC.name] = SUPERVISOR
SUPERVISOR_SPEC.loader.exec_module(SUPERVISOR)
FIXTURES = ROOT / "utilities" / "fixtures" / "opencode"


class SupervisorTerminalIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.jobs = self.base / "jobs.log"
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.artifacts = self.base / ".agent_reports"
        self.artifacts.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def terminal_log(self, verdict: str, blocker: str) -> Path:
        artifact = self.artifacts / f"{verdict.lower()}.md"
        artifact.write_text(verdict + "\n", encoding="utf-8")
        log = self.base / f"{verdict.lower()}.codex.jsonl"
        rows = [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        f"artifact: {artifact}\nverdict: {verdict}\n"
                        f"blocker: {blocker}"
                    ),
                },
            },
            {"type": "turn.completed"},
        ]
        log.write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )
        return log

    def row(self, status: str, attempt: str, verdict: str, blocker: str) -> str:
        log = self.terminal_log(verdict, blocker)
        meta = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,harness=codex,"
            f"attempt_id={attempt},parent_attempt_id=att-parent,"
            f"artifact_root={self.artifacts},log_file={log}"
        )
        if status == "done":
            failure = "fail" if verdict == "FAIL" else "pass"
            meta += f",note=dead-worker-fail,failure_class={failure},launch_outcome=never-launched"
        return (
            f"2026-08-06T00:00:00Z\t{status}\t{self.repo}\t{self.repo}"
            f"\tstage\t{meta}\n"
        )

    def test_open_pass_receipt_is_guard_admitted_and_harvested(self):
        self.jobs.write_text(
            self.row("open", "att-open", "PASS", "none"), encoding="utf-8"
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.01,
            timeout=0.2,
        )
        child = receipt["children"][0]
        self.assertEqual(child["required_action"], "complete-open")
        command = (
            "adapters/codex/bin/preflight.sh harvest --attempt-id att-open "
            "--status open --mark-done"
        )
        self.assertEqual(
            JOIN.classify_supervised_shell_command(
                base=ROOT,
                command=command,
                open_attempt_ids={"att-open"},
                parent_slug="owner",
            ),
            JOIN.SupervisorShellAction(
                "harvest", "att-open", status="open", mark_done=True
            ),
        )
        result = subprocess.run(
            [
                sys.executable, str(HARVEST), "--jobs", str(self.jobs),
                "--attempt-id", "att-open", "--status", "open", "--mark-done",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.artifacts)},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("matched=1", result.stdout)
        self.assertIn("\tdone\t", self.jobs.read_text(encoding="utf-8"))

    def test_done_failure_receipt_selects_done_inspection(self):
        self.jobs.write_text(
            self.row("done", "att-done", "FAIL", "typed-failure"),
            encoding="utf-8",
        )
        receipt = JOIN.join_batch(
            jobs=self.jobs,
            parent_attempt_id="att-parent",
            interval=0.01,
            timeout=0.2,
        )
        self.assertEqual(
            receipt["children"][0]["required_action"], "inspect-done-failure"
        )
        result = subprocess.run(
            [
                sys.executable, str(HARVEST), "--jobs", str(self.jobs),
                "--attempt-id", "att-done", "--status", "done",
                "--failure-detail",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.artifacts)},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("matched=1", result.stdout)
        self.assertIn("terminal_verdict=FAIL", result.stdout)


class OpencodeTerminalVocabularyTest(unittest.TestCase):
    """Gap 1 prep (C1): the two new helpers, action-neutral until C2 wires them in."""

    def test_terminal_stop_boundary_extracts_the_handoff_text(self):
        rows, _raw = SUPERVISOR._tail_rows(FIXTURES / "terminal-stop.jsonl")
        index, text = SUPERVISOR.opencode_terminal_boundary(rows)
        self.assertIsNotNone(index)
        self.assertEqual(text, "artifact: -\nverdict: PASS\nblocker: none")
        self.assertEqual(SUPERVISOR.opencode_last_step_finish_reason(rows), "stop")

    def test_truncated_permission_reject_has_no_stop_boundary_but_has_evidence(self):
        rows, raw = SUPERVISOR._tail_rows(FIXTURES / "truncated-permission-reject.jsonl")
        self.assertEqual(SUPERVISOR.opencode_terminal_boundary(rows), (None, None))
        self.assertEqual(SUPERVISOR.opencode_last_step_finish_reason(rows), "tool-calls")
        evidence = SUPERVISOR.opencode_truncation_evidence(raw)
        self.assertIn("permission requested: external_directory", evidence)
        self.assertIn("auto-rejecting", evidence)
        self.assertNotIn("\x1b", evidence)

    def test_broken_stream_has_no_boundary_no_reason_no_evidence(self):
        rows, raw = SUPERVISOR._tail_rows(FIXTURES / "broken-stream.jsonl")
        self.assertEqual(SUPERVISOR.opencode_terminal_boundary(rows), (None, None))
        self.assertIsNone(SUPERVISOR.opencode_last_step_finish_reason(rows))
        self.assertEqual(SUPERVISOR.opencode_truncation_evidence(raw), "")

class OpencodeGap1TerminalClassificationTest(unittest.TestCase):
    """Gap 1 (C2): classify_supervisor_log now recognizes the opencode
    step_finish/stop terminal (R1) via the harness=="opencode" branch."""

    def test_terminal_stop_classifies_as_completed_supervisor_pass(self):
        result = SUPERVISOR.classify_supervisor_log(
            str(FIXTURES / "terminal-stop.jsonl"), "opencode"
        )
        self.assertEqual(result.note, "completed-supervisor")
        self.assertEqual(result.failure_class, "pass")
        self.assertEqual(result.reconcile_reason, "exact-final-handoff")

    def test_broken_stream_still_classifies_as_dead_protocol(self):
        result = SUPERVISOR.classify_supervisor_log(
            str(FIXTURES / "broken-stream.jsonl"), "opencode"
        )
        self.assertEqual(result.note, "dead-protocol")
        self.assertEqual(result.reconcile_reason, "terminal-event-missing")

    def test_truncated_permission_reject_classifies_as_dead_permission_reject(self):
        # R2 (item 1(a), C3): no stop boundary, but the retained tail shows
        # the auto-reject line -> typed dead-permission-reject rather than
        # the generic dead-protocol.
        result = SUPERVISOR.classify_supervisor_log(
            str(FIXTURES / "truncated-permission-reject.jsonl"), "opencode"
        )
        self.assertEqual(result.note, "dead-permission-reject")
        self.assertEqual(result.failure_class, "permission")
        self.assertEqual(result.reconcile_reason, "permission-auto-reject")

    def test_claude_and_codex_classification_is_unaffected(self):
        # The opencode branch is gated on harness=="opencode"; feeding the
        # same opencode-shaped fixture through the claude/codex harness
        # labels must still fall through to the pre-existing loop untouched.
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                result = SUPERVISOR.classify_supervisor_log(
                    str(FIXTURES / "terminal-stop.jsonl"), harness
                )
                self.assertEqual(result.note, "dead-protocol")
                self.assertEqual(result.reconcile_reason, "terminal-event-missing")


if __name__ == "__main__":
    unittest.main()
