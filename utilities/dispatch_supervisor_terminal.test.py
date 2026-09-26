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


    def test_attention_and_abandonment_terminals_stay_distinct(self):
        # D-6b: three constructors, three notes -- assert all three together
        # so no future edit can collapse them.
        attention = SUPERVISOR.classify_supervisor_attention_terminal(
            "codex", "guard-and-supervisor-command-vocabulary-mismatch"
        )
        abandonment = SUPERVISOR.classify_supervisor_abandonment_terminal(
            "codex", "identical-redelivery-bound"
        )
        error = SUPERVISOR.classify_supervisor_error("codex", "protocol-mismatch")
        self.assertEqual(attention.note, "owner-attention-unactionable")
        self.assertEqual(attention.failure_class, "protocol")
        self.assertEqual(abandonment.note, "owner-redelivery-abandoned")
        self.assertEqual(abandonment.failure_class, "runtime")
        self.assertEqual(error.note, "dead-protocol")
        notes = {attention.note, abandonment.note, error.note}
        self.assertEqual(len(notes), 3, notes)
        # classify_supervisor_error cannot be tricked into either new note by
        # feeding it a new reason string -- it derives note only from
        # _PROTOCOL_REASON_RE.
        for reason in ("owner-attention-unactionable", "owner-redelivery-abandoned"):
            drifted = SUPERVISOR.classify_supervisor_error("codex", reason)
            self.assertIn(drifted.note, {"dead-protocol", "dead-runtime-exit"})


class RuntimeFailureClassifierTest(unittest.TestCase):
    """plan.md item 4: one shared classify_runtime_failure, structured-code-first."""

    def test_runtime_failure_structured_usage_limit_is_capacity(self):
        capacity = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70,
            structured_code="usageLimitExceeded",
        )
        self.assertEqual(capacity.note, "dead-capacity")
        self.assertEqual(capacity.failure_class, "capacity")
        bad_request = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70, structured_code="badRequest",
        )
        self.assertEqual(bad_request.note, "dead-runtime-error")
        self.assertEqual(bad_request.failure_class, "runtime")
        # serverOverloaded is transient overload, not a rate/usage limit --
        # deliberately excluded from the capacity structured codes (plan.md
        # §2 "8번" table judgment carried over to item 4).
        overloaded = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70, structured_code="serverOverloaded",
        )
        self.assertEqual(overloaded.failure_class, "runtime")
        via_status = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70, status="429",
        )
        self.assertEqual(via_status.note, "dead-capacity")
        auth = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70, structured_code="unauthorized",
        )
        self.assertEqual(auth.note, "dead-auth")
        self.assertEqual(auth.failure_class, "auth")

    def test_session_result_behaviour_unchanged(self):
        # Pinning guard: classify_session_result's claude/opencode behaviour
        # must not move a single case after the failure branch was factored
        # out into classify_runtime_failure.
        capacity = SUPERVISOR.classify_session_result(
            {"is_error": True, "result": "You've hit your usage limit"}, 1, runtime="claude",
        )
        self.assertEqual((capacity.note, capacity.failure_class), ("dead-capacity", "capacity"))
        status_capacity = SUPERVISOR.classify_session_result(
            {"is_error": True, "error": {"status": 429}}, 1, runtime="opencode",
        )
        self.assertEqual(status_capacity.note, "dead-capacity")
        self.assertEqual(status_capacity.api_status, "429")
        auth = SUPERVISOR.classify_session_result(
            {"is_error": True, "result": "invalid api key"}, 1, runtime="claude",
        )
        self.assertEqual((auth.note, auth.failure_class), ("dead-auth", "auth"))
        generic = SUPERVISOR.classify_session_result(
            {"is_error": True, "result": "something else broke"}, 1, runtime="opencode",
        )
        self.assertEqual((generic.note, generic.failure_class), ("dead-runtime-error", "runtime"))
        passed = SUPERVISOR.classify_session_result(
            {"is_error": False, "subtype": "success",
             "result": "artifact: -\nverdict: PASS\nblocker: none"},
            0, runtime="claude",
        )
        self.assertEqual(passed.note, "completed-supervisor")

    def test_supervisor_log_reader_agrees_on_codex_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "att.codex.jsonl"
            rows = [
                {
                    "type": "dispatch.supervisor.turn.failed",
                    "turn_id": "turn-1",
                    "codex_error_info": "usageLimitExceeded",
                    "message": "You've hit your usage limit",
                    "additional_details": "Resets in 13 days",
                },
                {"type": "dispatch.supervisor.error", "reason": "app-server-turn-failed"},
            ]
            log.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
            )
            result = SUPERVISOR.classify_supervisor_log(str(log), "codex")
            self.assertEqual(result.note, "dead-capacity")
            self.assertEqual(result.failure_class, "capacity")
            # The live raiser's own answer, from the same payload dict, must
            # be byte-identical -- the one-classifier guarantee this exists
            # for.
            live = SUPERVISOR.codex_turn_failure_terminal(rows[0])
            self.assertEqual(result, live)

    def test_missing_result_opencode_capacity(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            attempt_log = base / "att.opencode.jsonl"
            attempt_log.write_text(
                json.dumps({"type": "step_start", "sessionID": "ses_abc123"}) + "\n",
                encoding="utf-8",
            )
            server_log_dir = base / ".dispatch" / "opencode-runtime" / "att-1" / "data" / "opencode" / "log"
            server_log_dir.mkdir(parents=True)
            server_log = server_log_dir / "opencode.log"
            server_log.write_text(
                "\n".join(json.dumps(row) for row in [
                    {"level": "ERROR", "time": "2026-09-24T00:00:05Z",
                     "session": {"id": "ses_abc123"},
                     "error": {"error": "Monthly usage limit reached. Resets in 13 days"}},
                ]) + "\n",
                encoding="utf-8",
            )
            metadata = {
                "harness": "opencode", "attempt_id": "att-1", "worktree": str(base),
                "log_file": str(attempt_log), "started_at": "2026-09-24T00:00:00Z",
            }
            result = SUPERVISOR.missing_result_terminal(metadata)
            self.assertEqual(result.note, "dead-capacity")
            self.assertEqual(result.failure_class, "capacity")
            self.assertEqual(result.capacity_log, str(server_log))
            # No matching evidence at all -- stays the conservative default.
            fallback = SUPERVISOR.missing_result_terminal(
                {"harness": "opencode", "attempt_id": "att-1", "worktree": str(base),
                 "log_file": str(attempt_log), "started_at": "2099-01-01T00:00:00Z"}
            )
            self.assertEqual(fallback.note, "dead-missing-result")
            self.assertEqual(fallback.capacity_log, "")


if __name__ == "__main__":
    unittest.main()
