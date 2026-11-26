#!/usr/bin/env python3
"""Receipt -> guard -> harvest integration regression for SD-97."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

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

    def test_an_explained_none_blocker_keeps_a_pass_on_every_runtime(self):
        # RA-8: the shared rule both terminal readers use.
        for runtime in ("claude", "opencode"):
            with self.subTest(runtime=runtime):
                result = SUPERVISOR.classify_session_result(
                    {"result": "artifact: /a.md\nverdict: PASS\nblocker: none (scope-limited PASS)"},
                    0, runtime=runtime)
                self.assertEqual((result.note, result.failure_class), ("completed-supervisor", "pass"))
        refused = SUPERVISOR.classify_codex_result("artifact: /a.md\nverdict: PASS\nblocker: G1 skipped")
        self.assertEqual((refused.note, refused.reconcile_reason), ("dead-contract", "pass-blocker-not-none"))

    def test_a_fence_or_two_trailing_sentences_keep_the_verdict(self):
        # G1: the supervisor reader agrees with the other two readers — the
        # verdict comes from the block alone.
        def terminal(text):
            return SUPERVISOR._handoff_terminal(text, event="stop", process_exit=0)
        for verdict, blocker, note in (("PASS", "none", "completed-supervisor"),
                                       ("FAIL", "x", "dead-worker-fail"),
                                       ("BLOCKED", "x", "dead-worker-blocked")):
            base = f"artifact: -\nverdict: {verdict}\nblocker: {blocker}"
            for tail in ("", "\nAll done.", "\n```log\nx\n```",
                         "\n```log\nx\n```\nAll done.\nClosing out."):
                with self.subTest(verdict=verdict, tail=tail[:16]):
                    self.assertEqual(terminal(base + tail).note, note)
        for text in ("artifact: -\nverdict: PASS\nblocker: none\nOne.\nTwo.\nThree.",
                     "artifact: -\nverdict: PASS\nblocker: none\nverdict: FAIL",
                     "artifact: -\nverdict: PASS\nblocker: none\n## 평결: FAIL",
                     "artifact: -\nverdict: PASS\nblocker: none\nThe verdict: FAIL."):
            with self.subTest(text=text[:40]):
                result = terminal(text)
                self.assertEqual((result.note, result.reconcile_reason),
                                 ("dead-contract", "final-handoff-invalid"))

    def test_a_fenced_later_envelope_is_read_not_the_decoy(self):
        # PR #281 round 1: the fenced later FAIL block is the one read; a bare
        # field line inside a fence fails closed.
        decoy = ("Contract example:\nartifact: <path>\nverdict: PASS\nblocker: none\n"
                 "```\nartifact: /r.md\nverdict: FAIL\nblocker: 3 tests fail\n```")
        result = SUPERVISOR._handoff_terminal(decoy, event="stop", process_exit=0)
        self.assertEqual((result.note, result.reconcile_reason),
                         ("dead-worker-fail", "worker-reported-fail"))
        fenced_line = "artifact: -\nverdict: PASS\nblocker: none\n```\nverdict: FAIL\n```"
        result = SUPERVISOR._handoff_terminal(fenced_line, event="stop", process_exit=0)
        self.assertEqual((result.note, result.reconcile_reason),
                         ("dead-contract", "final-handoff-invalid"))

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

    def test_parent_deploy_wait_blocked_survives_post_exit_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "wait.jsonl"
            from dataclasses import asdict
            terminal = SUPERVISOR.classify_parent_deploy_wait_blocked("parent-deploy-wait-timeout")
            log.write_text(json.dumps({"type": "dispatch.supervisor.terminal",
                                       "terminal": asdict(terminal)}) + "\n")
            for harness in ("claude", "codex", "opencode"):
                with self.subTest(harness=harness):
                    terminal = SUPERVISOR.classify_supervisor_log(log, harness)
                    self.assertEqual((terminal.note, terminal.failure_class, terminal.process_exit),
                                     ("dead-worker-blocked", "blocked", "3"))


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

    def test_transient_overload_text_is_not_read_as_account_capacity(self):
        # The real `serverOverloaded` envelope carries diagnostic text
        # "Selected model is at capacity" -- transient congestion, not an
        # account quota. A structured non-account code is authoritative, so
        # the text must not reach `_CAPACITY_RE`'s `model.{0,80}at capacity`
        # alternative and seal the row `dead-capacity`, which a consumer then
        # blocks for an hour.
        overloaded = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70,
            structured_code="serverOverloaded",
            text="Selected model is at capacity",
        )
        self.assertEqual(overloaded.note, "dead-runtime-error")
        self.assertEqual(overloaded.failure_class, "runtime")
        # An account structured code still wins on its own, independently of
        # the same text (structured-first, unchanged).
        account = SUPERVISOR.classify_runtime_failure(
            "codex", event="turn.failed", process_exit=70,
            structured_code="usageLimitExceeded",
            text="Selected model is at capacity",
        )
        self.assertEqual(account.note, "dead-capacity")
        self.assertEqual(account.failure_class, "capacity")

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

    def test_an_edge_block_page_is_a_transient_runtime_end_and_a_real_refusal_stays_auth(self):
        # The envelope eight Claude owners received on 2026-11-13 (shape and
        # text exactly as logged): the edge network's page, not the provider's
        # judgment of a key or account.
        blocked = {"type": "result", "subtype": "success", "is_error": True,
                   "api_error_status": 403, "api_error_origin": "first_party",
                   "terminal_reason": "api_error",
                   "result": "Failed to authenticate. API Error: 403 Attention Required! | Cloudflare"}
        verdict = SUPERVISOR.classify_session_result(blocked, 1, runtime="claude")
        self.assertEqual((verdict.note, verdict.failure_class, verdict.reconcile_reason, verdict.api_status),
                         ("dead-runtime-error", "runtime", SUPERVISOR.EDGE_BLOCK_REASON, "403"))
        challenge = SUPERVISOR.classify_session_result(
            {"is_error": True, "error": {"status": 403, "message":
             "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"}},
            1, runtime="opencode")
        self.assertEqual(challenge.reconcile_reason, SUPERVISOR.EDGE_BLOCK_REASON)
        codex = SUPERVISOR.codex_turn_failure_terminal({
            "codex_error_info": "unauthorized",
            "message": "unexpected status 403 Forbidden: Attention Required! | Cloudflare"})
        self.assertEqual((codex.note, codex.reconcile_reason),
                         ("dead-runtime-error", SUPERVISOR.EDGE_BLOCK_REASON))
        # Any other 401/403 body is the provider's refusal and keeps the auth stop.
        for result in (
            {**blocked, "result": 'API Error: 403 {"type":"error","error":{"type":"permission_error",'
                                  '"message":"Your organization does not have access"}}'},
            {**blocked, "api_error_status": 401, "result": "Failed to authenticate. API Error: 401 "
                                                           '{"type":"error","error":{"type":"authentication_error"}}'},
            {**blocked, "result": "Failed to authenticate. API Error: 403 Forbidden"},
            {**blocked, "result": "Cloudflare"},
        ):
            with self.subTest(result=result["result"][:40]):
                stop = SUPERVISOR.classify_session_result(result, 1, runtime="claude")
                self.assertEqual((stop.note, stop.failure_class), ("dead-auth", "auth"))
        # The page title alone without a 401/403 is not read as anything new.
        other = SUPERVISOR.classify_session_result(
            {**blocked, "api_error_status": 500}, 1, runtime="claude")
        self.assertEqual((other.note, other.reconcile_reason), ("dead-runtime-error", "runtime-error-envelope"))

    def test_opencode_typed_provider_auth_stays_a_replaceable_runtime_death(self):
        # OpenCode 2 reports one provider's rejection by type, without a status.
        # It is not a whole-harness login failure: `dead-auth` would turn the
        # automatic replacement into a manual stop.
        sys.path.insert(0, str(ROOT / "utilities"))
        import dispatch_replacement
        result = {"type": "result", "runtime": "opencode", "session_id": "ses_x",
                  "subtype": "error_during_execution", "is_error": True, "result": "",
                  "error": {"type": "provider.auth", "message": "Request failed: 401"}}
        verdict = SUPERVISOR.classify_session_result(result, 1, runtime="opencode")
        self.assertEqual((verdict.note, verdict.failure_class, verdict.api_status),
                         ("dead-runtime-error", "runtime", ""))
        self.assertIn(verdict.note, dispatch_replacement.RUNTIME_DEATH_NOTES)

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


class TerminalCommitErrorClassificationTest(unittest.TestCase):
    """D2 (2026-09-27 owner decision): an escaped `TerminalCommitError` used
    to collapse into the generic `supervisor-internal-TerminalCommitError`
    note in both session supervisors' top-level `except Exception`,
    discarding `code`/`detail` -- the defect that let an owner die with
    every route node already PASS and no diagnosable reason on the row.
    `classify_terminal_commit_error`/`terminal_commit_error_event` provide
    bounded evidence that the registry accepts without adding route authority."""

    def test_registry_reason_is_the_closed_code_not_a_free_text_type_name(self):
        terminal = SUPERVISOR.classify_terminal_commit_error(
            "producer-binding-required", "some/path\t,producer-binding.json\nnext",
            terminal_slot="/root/.runtime/terminal-commits/v1/rt-x/att-y",
        )
        self.assertEqual(terminal.note, "dead-terminal-commit")
        self.assertEqual(terminal.reconcile_reason, "producer-binding-required")
        evidence = terminal.evidence()
        self.assertEqual(evidence["reconcile_reason"], "producer-binding-required")
        self.assertEqual(evidence["terminal_commit_detail"], "some/path producer-binding.json next")
        self.assertEqual(evidence["terminal_commit_slot"], "/root/.runtime/terminal-commits/v1/rt-x/att-y")
        self.assertNotIn("detail", evidence)
        self.assertNotIn("terminal_slot", evidence)
        self.assertNotIn("route_file", evidence)
        import dispatch_contract
        self.assertLessEqual(set(evidence), dispatch_contract.ATTEMPT_TERMINAL_EVIDENCE_KEYS)

    def test_detail_is_length_bounded_and_carries_no_optional_fields_when_absent(self):
        terminal = SUPERVISOR.classify_terminal_commit_error("transaction-conflict", "x" * 5000)
        self.assertEqual(len(terminal.evidence()["terminal_commit_detail"]), 240)
        self.assertNotIn("terminal_slot", terminal.evidence())
        self.assertNotIn("route_file", terminal.evidence())

    def test_event_payload_matches_between_both_supervisor_call_sites(self):
        event = SUPERVISOR.terminal_commit_error_event(
            "producer-binding-mismatch", "root-identity",
            terminal_slot="/root/.runtime/terminal-commits/v1/rt-x/att-y",
        )
        self.assertEqual(event, {
            "type": "dispatch.supervisor.error", "reason": "producer-binding-mismatch",
            "detail": "root-identity", "terminal_slot": "/root/.runtime/terminal-commits/v1/rt-x/att-y",
        })
        bare = SUPERVISOR.terminal_commit_error_event("recovery-unavailable", "")
        self.assertEqual(bare, {"type": "dispatch.supervisor.error", "reason": "recovery-unavailable", "detail": ""})

    def test_terminal_commit_evidence_closes_a_real_registry_row(self):
        fixture = SupervisorTerminalIntegrationTest()
        fixture.setUp()
        try:
            attempt = "att-terminal-commit-evidence"
            fixture.jobs.write_text(fixture.row("open", attempt, "PASS", "none") + "\n", encoding="utf-8")
            terminal = SUPERVISOR.classify_terminal_commit_error(
                "producer-binding-required", "binding unavailable\t,\nnext")
            self.assertEqual(SUPERVISOR.reconcile_supervisor_terminal(
                fixture.jobs, attempt, terminal), "closed")
            fields = fixture.jobs.read_text(encoding="utf-8").strip().split("\t")
            self.assertEqual(fields[1], "done")
            metadata = __import__("dispatch_contract").parse_registry_metadata(fields[5])
            self.assertEqual(metadata["note"], "dead-terminal-commit")
            self.assertEqual(metadata["reconcile_reason"], "producer-binding-required")
            self.assertEqual(metadata["terminal_commit_detail"], "binding unavailable next")
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
