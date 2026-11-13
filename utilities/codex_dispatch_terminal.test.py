#!/usr/bin/env python3
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import codex_dispatch_terminal as terminal
from codex_dispatch_terminal import inspect_terminal_attempt, inspect_terminal_log, wire_record

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "opencode"


class CodexDispatchTerminalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.worktree = self.base / "repo with spaces"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        self.root = self.base / ".agent_reports"
        self.root.mkdir()
        self.old_root = os.environ.get("AGENT_ARTIFACT_ROOT")
        os.environ["AGENT_ARTIFACT_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENT_ARTIFACT_ROOT", None)
        else:
            os.environ["AGENT_ARTIFACT_ROOT"] = self.old_root
        self.temp.cleanup()

    def write_log(
        self,
        *,
        verdict="BLOCKED",
        blocker="blocked",
        artifact="-",
        sandbox=True,
        completed=True,
        final_text=None,
        suffix=None,
        diagnostic=None,
        prefix=None,
        between=None,
    ):
        path = self.base / f"attempt-{len(list(self.base.glob('attempt-*.jsonl')))}.jsonl"
        rows = [{"type": "turn.started"}]
        if prefix:
            rows.extend(prefix)
        if sandbox or diagnostic is not None:
            output = diagnostic if diagnostic is not None else (
                "bwrap: Can't bind mount /bindfile123 on "
                "/newroot/work/repo/.codex: Unable to mount source on "
                "destination: No such file or directory\n"
            )
            rows.append({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "exit_code": 1,
                    "aggregated_output": output,
                },
            })
        text = final_text
        if text is None:
            text = f"artifact: {artifact}\nverdict: {verdict}\nblocker: {blocker}"
        rows.append({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": text},
        })
        if between:
            rows.extend(between)
        if completed:
            rows.append({"type": "turn.completed"})
        if suffix:
            rows.extend(suffix)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        return path

    def inspect(self, path, *, metadata=None, detail=False):
        return inspect_terminal_attempt(
            path,
            worktree=self.worktree,
            artifact_root_metadata=metadata or self.root,
            include_failure_detail=detail,
        )

    def test_recorded_bucket_move_keeps_exact_handoff_readable(self):
        original = self.root / "campaigns/cycle/artifacts/plan.md"
        moved = self.root / "campaigns/cycle/artifacts/plans/plan.md"
        moved.parent.mkdir(parents=True)
        moved.write_text("finished plan\n")
        log = self.write_log(verdict="PASS", blocker="none", artifact=str(original), sandbox=False)
        self.assertEqual(self.inspect(log)["reason"], "artifact-missing")
        with mock.patch("artifact_producer.resolve_placed_output", return_value=moved):
            result = self.inspect(log)
        self.assertEqual(result["state"], "valid")
        self.assertEqual(result["artifact_state"], "readable")
        self.assertEqual(
            Path(base64.urlsafe_b64decode(result["artifact_path_b64"] + "=" *
                                       (-len(result["artifact_path_b64"]) % 4)).decode()), moved)
        self.assertEqual(
            Path(base64.urlsafe_b64decode(result["artifact_origin_path_b64"] + "=" *
                                       (-len(result["artifact_origin_path_b64"]) % 4)).decode()), original)

    def test_bucket_move_cannot_escape_artifact_root(self):
        original = self.root / "campaigns/cycle/artifacts/plan.md"
        outside = self.base / "other.md"
        outside.write_text("unrelated\n")
        log = self.write_log(verdict="PASS", blocker="none", artifact=str(original), sandbox=False)
        with mock.patch("artifact_producer.resolve_placed_output", return_value=outside):
            result = self.inspect(log)
        self.assertEqual(result["reason"], "artifact-outside-root")

    def _path(self, encoded):
        return Path(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())

    def test_a_missing_path_naming_one_file_by_the_same_tail_is_that_file(self):
        # RA-8, BC C route plan-check: the handoff dropped one parent directory.
        real = self.root / "campaigns/cycle/artifacts/plans/plan-check.md"
        real.parent.mkdir(parents=True)
        real.write_text("review\n")
        typo = self.base / "elsewhere" / ".agent_reports" / "campaigns/cycle/artifacts/plans/plan-check.md"
        for verdict, blocker in (("PASS", "none"), ("FAIL", "G1 wrong padding")):
            with self.subTest(verdict=verdict):
                result = self.inspect(self.write_log(verdict=verdict, blocker=blocker, artifact=str(typo), sandbox=False))
                self.assertEqual((result["state"], result["verdict"], result["artifact_state"]),
                                 ("valid", verdict, "readable"))          # the verdict is never changed
                self.assertEqual(self._path(result["artifact_path_b64"]), real)
                self.assertEqual(self._path(result["artifact_named_path_b64"]), typo)
                self.assertEqual(result["envelope_normalized"], "artifact-same-tail")
                self.assertNotIn("artifact_origin_path_b64", result)  # not a producer-recorded move
        # No file with that tail, or two of them, leaves the handoff outside the root.
        absent = self.base / "elsewhere" / ".agent_reports" / "campaigns/cycle/artifacts/absent.md"
        (self.root / "a/.agent_reports").mkdir(parents=True)
        (self.root / "a/.agent_reports/twice.md").write_text("one\n")
        (self.root / "twice.md").write_text("two\n")
        twice = self.base / "elsewhere" / ".agent_reports" / "a" / ".agent_reports" / "twice.md"
        for path in (absent, twice):
            with self.subTest(path=path.name):
                result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact=str(path), sandbox=False))
                self.assertEqual(result["reason"], "artifact-outside-root")

    def test_a_relative_path_is_read_against_the_root_then_the_worktree(self):
        real = self.root / "campaigns/cycle/artifacts/plans/plan.md"
        real.parent.mkdir(parents=True)
        real.write_text("plan\n")
        relative = "campaigns/cycle/artifacts/plans/plan.md"
        for verdict, blocker in (("PASS", "none"), ("FAIL", "G1 wrong padding")):
            with self.subTest(verdict=verdict):
                result = self.inspect(self.write_log(verdict=verdict, blocker=blocker, artifact=relative, sandbox=False))
                self.assertEqual((result["state"], result["verdict"], result["artifact_state"]),
                                 ("valid", verdict, "readable"))
                self.assertEqual(self._path(result["artifact_path_b64"]), real)
                self.assertEqual(str(self._path(result["artifact_named_path_b64"])), relative)
                self.assertEqual(result["envelope_normalized"], "artifact-relative")

    def test_an_unusable_relative_path_names_the_root_and_every_path_tried(self):
        # missing everywhere
        result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact="plans/absent.md", sandbox=False))
        self.assertEqual((result["state"], result["reason"], result["artifact_state"]),
                         ("invalid", "artifact-outside-root", "missing"))
        self.assertEqual(self._path(result["artifact_base_root_b64"]), self.root)
        self.assertEqual(str(self._path(result["artifact_candidates_b64"])).split("\n"),
                         [str(self.root / "plans/absent.md"), str(self.worktree / "plans/absent.md")])
        # present only in the worktree, outside the root
        (self.worktree / "notes.md").write_text("draft\n")
        result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact="notes.md", sandbox=False))
        self.assertEqual((result["reason"], result["artifact_state"]), ("artifact-outside-root", "outside-root"))
        # two different in-root targets
        (self.root / "twice.md").write_text("one\n")
        (self.worktree / "twice.md").symlink_to(self.root / "campaigns")
        (self.root / "campaigns").mkdir(exist_ok=True)
        result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact="twice.md", sandbox=False))
        self.assertEqual((result["reason"], result["artifact_state"]), ("artifact-outside-root", "unchecked"))
        self.assertEqual(len(str(self._path(result["artifact_candidates_b64"])).split("\n")), 2)
        # parent steps never leave through a relative path
        result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact="../x.md", sandbox=False))
        self.assertEqual(result["reason"], "artifact-outside-root")

    def test_an_absolute_path_outside_the_root_names_the_root(self):
        outside = self.base / "other.md"
        outside.write_text("x\n")
        result = self.inspect(self.write_log(verdict="PASS", blocker="none", artifact=str(outside), sandbox=False))
        self.assertEqual(result["reason"], "artifact-outside-root")
        self.assertEqual(self._path(result["artifact_base_root_b64"]), self.root)
        self.assertEqual(self._path(result["artifact_candidates_b64"]), outside)

    def test_a_pass_with_an_explained_none_blocker_is_a_pass_and_keeps_the_note(self):
        # RA-8, BC C route test: `blocker: none (...)` was discarded and rerun for ~13 min.
        artifact = self.root / "envelope.md"
        artifact.write_text("ok\n")
        result = self.inspect(self.write_log(
            verdict="PASS", blocker="none (범위 한정 PASS; G5 별도)", artifact=str(artifact), sandbox=False))
        self.assertEqual((result["state"], result["verdict"], result["blocker_reason"], result["artifact_state"]),
                         ("valid", "PASS", "none", "readable"))
        self.assertEqual(result["envelope_normalized"], "pass-blocker-note")
        self.assertEqual(result["pass_note_excerpt"], "범위 한정 PASS; G5 별도")
        for blocker in ("not-none", "none, but G1 was skipped", "nonexistent (x)", "none ()"):
            with self.subTest(blocker=blocker):
                result = self.inspect(self.write_log(verdict="PASS", blocker=blocker, artifact=str(artifact), sandbox=False))
                self.assertEqual(result["reason"], "pass-blocker-not-none")

    def test_a_review_artifact_that_declares_fail_contradicts_a_pass_envelope(self):
        # P-4: the round that shipped as a pass. Envelope PASS, artifact body
        # opening with `## 평결: FAIL`. The envelope keeps its authority (the
        # verdict field is untouched) but the contradiction is named.
        artifact = self.root / "phase_review.md"
        artifact.write_text("## 평결: FAIL\n\nblocking: one\n", encoding="utf-8")
        result = inspect_terminal_attempt(
            self.write_log(verdict="PASS", blocker="none", artifact=str(artifact), sandbox=False),
            worktree=self.worktree,
            artifact_root_metadata=self.root,
            worker_type="review",
        )
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["review_artifact_verdict"], "FAIL")
        self.assertEqual(result["review_verdict_conflict"], "artifact-fail-envelope-pass")

    def test_structured_review_verdict_cannot_hide_behind_a_pass_envelope(self):
        artifact = self.root / "review.json"
        for value,declared in (({"verdict":"BLOCKED"},"BLOCKED"),
                               ({"verdict":"PASS","io":{"verdict":"FAIL"}},"FAIL"),
                               ({"verdict":"PASS"},"PASS")):
            with self.subTest(value=value):
                artifact.write_text(json.dumps(value),encoding="utf-8")
                result=inspect_terminal_attempt(
                    self.write_log(verdict="PASS",blocker="none",artifact=str(artifact),sandbox=False),
                    worktree=self.worktree,artifact_root_metadata=self.root,worker_type="review")
                self.assertEqual(result["review_artifact_verdict"],declared)
                self.assertEqual(bool(result.get("review_verdict_conflict")),declared!="PASS")

    def test_only_a_review_worker_and_only_the_harmful_direction_conflict(self):
        fail_artifact = self.root / "review-fail.md"
        fail_artifact.write_text("## 평결: FAIL\n", encoding="utf-8")
        pass_artifact = self.root / "review-pass.md"
        pass_artifact.write_text("## 평결: PASS\n", encoding="utf-8")
        quiet_artifact = self.root / "review-quiet.md"
        quiet_artifact.write_text("no verdict heading at all\n", encoding="utf-8")

        # a non-review worker is not read for a verdict at all
        stage = inspect_terminal_attempt(
            self.write_log(verdict="PASS", blocker="none", artifact=str(fail_artifact), sandbox=False),
            worktree=self.worktree, artifact_root_metadata=self.root, worker_type="stage")
        self.assertNotIn("review_artifact_verdict", stage)
        self.assertNotIn("review_verdict_conflict", stage)

        # an artifact that declares nothing changes nothing
        quiet = inspect_terminal_attempt(
            self.write_log(verdict="PASS", blocker="none", artifact=str(quiet_artifact), sandbox=False),
            worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review")
        self.assertEqual(quiet["review_artifact_verdict"], "unstated")
        self.assertNotIn("review_verdict_conflict", quiet)

        # agreement is not a conflict
        agreed = inspect_terminal_attempt(
            self.write_log(verdict="PASS", blocker="none", artifact=str(pass_artifact), sandbox=False),
            worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review")
        self.assertNotIn("review_verdict_conflict", agreed)

        # a FAIL envelope over a PASS artifact already blocks on its own
        blocking = inspect_terminal_attempt(
            self.write_log(verdict="FAIL", blocker="one", artifact=str(pass_artifact), sandbox=False),
            worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review")
        self.assertEqual(blocking["failure_note"], terminal.REVIEW_BLOCKING_NOTE)
        self.assertNotIn("review_verdict_conflict", blocking)

    def test_the_declared_verdict_reads_one_heading_not_review_prose(self):
        artifact = self.root / "prose.md"
        # a bare line, an inline mention, and a verdict word far past the head
        # must all read as `unstated`: this is a declared field, not a scan.
        artifact.write_text(
            "평결: FAIL\n\nthe reviewer wrote verdict FAIL in a sentence\n"
            + ("filler line\n" * 500)
            + "## 평결: FAIL\n",
            encoding="utf-8")
        self.assertEqual(terminal.artifact_declared_verdict(artifact), "unstated")
        heading = self.root / "heading.md"
        heading.write_text("# 평결 : **BLOCKED**\nrest\n", encoding="utf-8")
        self.assertEqual(terminal.artifact_declared_verdict(heading), "BLOCKED")
        missing = self.root / "nope.md"
        self.assertEqual(terminal.artifact_declared_verdict(missing / "deeper"), "unreadable")

    def test_a_rate_limit_death_is_not_reported_as_a_broken_envelope(self):
        # 2026-09-10, att-2164dce2: a `top` review that simply ran out of Fable
        # quota was reported as a malformed handoff, because the live
        # supervisor and this post-hoc reader classified the same result row
        # differently. They now share one classifier.
        def result_log(name, text):
            path = self.base / name
            path.write_text(json.dumps({
                "type": "result", "subtype": "error", "is_error": True, "result": text,
            }) + "\n", encoding="utf-8")
            return path

        capacity = self.inspect(result_log("cap.jsonl", "You've reached your Fable limit"))
        self.assertEqual(capacity["reason"], "claude-result-capacity")
        self.assertEqual(capacity["failure_note"], "dead-capacity")
        self.assertEqual(capacity["failure_class"], "capacity")

        auth = self.inspect(result_log("auth.jsonl", "authentication_error"))
        self.assertEqual(auth["reason"], "claude-result-auth")
        self.assertEqual(auth["failure_note"], "dead-auth")

        # an actually broken envelope keeps the name every consumer already reads
        broken = self.inspect(result_log("broken.jsonl", "unexpected token in json"))
        self.assertEqual(broken["reason"], "claude-result-runtime-error")
        self.assertEqual(broken["failure_note"], "dead-runtime-error")
        for view, blocker in ((capacity, "capacity"), (auth, "auth"), (broken, "contract-violation")):
            self.assertEqual((view["state"], view["blocker_reason"]), ("invalid", blocker))

    def test_compatibility_failure_notes_remain_stable(self):
        blocked = inspect_terminal_log(self.write_log())
        generic = inspect_terminal_log(self.write_log(sandbox=False))
        failed = inspect_terminal_log(
            self.write_log(verdict="FAIL", blocker="failed", sandbox=False)
        )
        passed = inspect_terminal_log(
            self.write_log(verdict="PASS", blocker="none", sandbox=False)
        )
        self.assertEqual(blocked["failure_note"], "dead-sandbox-init")
        self.assertEqual(generic["failure_note"], "dead-worker-blocked")
        self.assertEqual(failed["failure_note"], "dead-worker-fail")
        self.assertEqual(passed["failure_note"], "")

    # OPERATIONS §5.10 "Review verdict is a result, not a worker death" ---------
    def test_review_worker_fail_with_readable_artifact_is_a_completed_review(self):
        review = self.root / "round_1.md"
        review.write_text("## Plan Review Results\n1 blocking finding\n", encoding="utf-8")
        path = self.write_log(verdict="FAIL", blocker="1 blocking finding",
                              artifact=str(review), sandbox=False)
        result = inspect_terminal_attempt(
            path, worktree=self.worktree, artifact_root_metadata=self.root,
            worker_type="review",
        )
        self.assertEqual(result["state"], "valid")
        self.assertEqual(result["artifact_state"], "readable")
        self.assertEqual(result["failure_note"], terminal.REVIEW_BLOCKING_NOTE)
        # only the completion note changes: the verdict axis is untouched
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(result["failure_class"], "fail")
        self.assertTrue(terminal.review_blocking_handoff(result, "review"))

    def test_review_note_needs_a_review_worker_and_a_readable_artifact(self):
        review = self.root / "round_1.md"
        review.write_text("findings\n", encoding="utf-8")
        readable = self.write_log(verdict="FAIL", blocker="x", artifact=str(review), sandbox=False)
        for worker_type in (None, "stage", "owner", "support"):
            with self.subTest(worker_type=worker_type):
                result = inspect_terminal_attempt(
                    readable, worktree=self.worktree, artifact_root_metadata=self.root,
                    worker_type=worker_type,
                )
                self.assertEqual(result["failure_note"], "dead-worker-fail")
                self.assertFalse(terminal.review_blocking_handoff(result, worker_type))
        none = self.write_log(verdict="FAIL", blocker="x", artifact="-", sandbox=False)
        result = inspect_terminal_attempt(
            none, worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review",
        )
        self.assertEqual(result["artifact_state"], "none")
        self.assertEqual(result["failure_note"], "dead-worker-fail")
        missing = self.write_log(verdict="FAIL", blocker="x",
                                 artifact=str(self.root / "absent.md"), sandbox=False)
        result = inspect_terminal_attempt(
            missing, worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review",
        )
        self.assertEqual(result["state"], "invalid")
        self.assertNotEqual(result.get("failure_note"), terminal.REVIEW_BLOCKING_NOTE)
        blocked = self.write_log(verdict="BLOCKED", blocker="x", artifact=str(review), sandbox=False)
        result = inspect_terminal_attempt(
            blocked, worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review",
        )
        self.assertEqual(result["failure_note"], "dead-worker-blocked")
        passed = self.write_log(verdict="PASS", blocker="none", artifact=str(review), sandbox=False)
        result = inspect_terminal_attempt(
            passed, worktree=self.worktree, artifact_root_metadata=self.root, worker_type="review",
        )
        self.assertEqual(result["failure_note"], "")

    def test_supervisor_turn_boundary_prevents_cross_turn_diagnostic_bleed(self):
        old_failure = {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "exit_code": 1,
                "aggregated_output": (
                    "bwrap: Can't bind mount /bindfile123 on "
                    "/newroot/work/repo/.codex: Unable to mount source on "
                    "destination: No such file or directory\n"
                ),
            },
        }
        boundary = {
            "type": "dispatch.supervisor.turn.started",
            "turn_id": "turn-final",
        }
        result = inspect_terminal_log(
            self.write_log(sandbox=False, prefix=[old_failure, boundary])
        )
        self.assertEqual(result["failure_note"], "dead-worker-blocked")
        self.assertEqual(result["failure_class"], "blocked")
        self.assertNotIn("diagnostic", result)

    def test_claude_stream_result_uses_the_same_handoff_contract(self):
        for verdict, blocker, note in (
            ("PASS", "none", ""),
            ("FAIL", "fixture failure", "dead-worker-fail"),
            ("BLOCKED", "fixture blocker", "dead-worker-blocked"),
        ):
            with self.subTest(verdict=verdict):
                path = self.base / f"claude-{verdict.lower()}.jsonl"
                path.write_text(
                    json.dumps({
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": (
                            f"artifact: -\nverdict: {verdict}\nblocker: {blocker}"
                        ),
                    }) + "\n",
                    encoding="utf-8",
                )
                compatibility = inspect_terminal_log(path)
                self.assertEqual(compatibility["terminal_event"], "result")
                self.assertEqual(compatibility["failure_note"], note)
                result = self.inspect(path)
                self.assertEqual(result["source"], "exact-claude-result")
                self.assertEqual(result["verdict"], verdict)

    def test_claude_runtime_error_cannot_promote_valid_looking_pass(self):
        path = self.base / "claude-runtime-error.jsonl"
        path.write_text(json.dumps({
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "artifact: -\nverdict: PASS\nblocker: none",
        }) + "\n", encoding="utf-8")
        parsed = terminal._read_terminal(path)
        self.assertEqual(parsed["state"], "invalid")
        self.assertEqual(parsed["source"], "exact-claude-result")
        self.assertEqual(parsed["reason"], "claude-result-runtime-error")

    def test_valid_verdict_matrix_and_readable_artifact_reference(self):
        artifact = self.root / "plans" / "final report.md"
        artifact.parent.mkdir()
        artifact.write_text("ARTIFACT_BODY_SENTINEL")
        for verdict, blocker, reason in (
            ("PASS", "none", "none"),
            ("FAIL", "none", "none"),
            ("FAIL", "private failure", "worker-reported"),
            ("BLOCKED", "none", "none"),
            ("BLOCKED", "private blocker", "worker-reported"),
        ):
            with self.subTest(verdict=verdict, blocker=blocker):
                result = self.inspect(
                    self.write_log(
                        verdict=verdict,
                        blocker=blocker,
                        artifact=artifact,
                        sandbox=False,
                    )
                )
                self.assertEqual(result["state"], "valid")
                self.assertEqual(result["artifact_state"], "readable")
                self.assertEqual(result["blocker_reason"], reason)
                encoded = result["artifact_path_b64"]
                decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                self.assertEqual(decoded.decode(), str(artifact))
                self.assertNotIn("ARTIFACT_BODY_SENTINEL", repr(result))

    def test_absent_malformed_decoy_suffix_and_pass_blocker(self):
        absent = self.inspect(self.write_log(completed=False, sandbox=False))
        self.assertEqual((absent["exit_code"], absent["state"]), (2, "absent"))
        # A prepended summary sentence is the one deviation prompts cannot
        # prevent (two 2026-07-28 pipelines), so the trailing block still counts
        # — but nothing outside the captured fields may reach the result.
        prefixed = self.inspect(
            self.write_log(final_text="RAW_AGENT_SENTINEL\nartifact: -\nverdict: FAIL\nblocker: x")
        )
        self.assertEqual(prefixed["state"], "valid")
        self.assertEqual(prefixed["verdict"], "FAIL")
        self.assertNotIn("RAW_AGENT_SENTINEL", repr(prefixed))
        # G1: one trailing sentence is tolerated — the verdict still stands,
        # but nothing outside the captured fields may reach the result.
        trailing_prose = self.inspect(
            self.write_log(final_text="artifact: -\nverdict: PASS\nblocker: none\nRAW_AGENT_SENTINEL")
        )
        self.assertEqual(trailing_prose["state"], "valid")
        self.assertEqual(trailing_prose["verdict"], "PASS")
        self.assertNotIn("RAW_AGENT_SENTINEL", repr(trailing_prose))
        # An in-message decoy never outranks the trailing block.
        two_blocks = self.inspect(
            self.write_log(
                final_text=(
                    "artifact: -\nverdict: PASS\nblocker: none\n\n"
                    "artifact: -\nverdict: FAIL\nblocker: real"
                )
            )
        )
        self.assertEqual((two_blocks["state"], two_blocks["verdict"]), ("valid", "FAIL"))
        # PR #281 round 1: a fenced later envelope is not swallowed by an
        # earlier decoy; a bare field line inside a fence fails closed.
        fenced_real = self.inspect(self.write_log(final_text=(
            "Contract example:\nartifact: <path>\nverdict: PASS\nblocker: none\n"
            "```\nartifact: -\nverdict: FAIL\nblocker: real\n```"
        )))
        self.assertEqual((fenced_real["state"], fenced_real["verdict"]), ("valid", "FAIL"))
        fenced_decoy = self.inspect(self.write_log(final_text=(
            "artifact: -\nverdict: PASS\nblocker: none\n```\nverdict: FAIL\n```"
        )))
        self.assertEqual(fenced_decoy["reason"], "malformed-handoff")
        # PR #281 rounds 2-3: a dressed or prose-carrying opposite verdict in
        # the tail fails closed.
        for tail in ("## 평결: FAIL", "The verdict: FAIL."):
            with self.subTest(tail=tail):
                dressed = self.inspect(self.write_log(final_text=(
                    "artifact: -\nverdict: PASS\nblocker: none\n" + tail
                )))
                self.assertEqual(dressed["reason"], "malformed-handoff")
        # A line that merely ends with the field name does not open a block.
        glued = self.inspect(
            self.write_log(final_text="see artifact: -\nverdict: PASS\nblocker: none")
        )
        self.assertEqual(glued["reason"], "malformed-handoff")
        decoy = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "artifact: -\nverdict: PASS\nblocker: none"},
        }
        later = self.inspect(
            self.write_log(
                verdict="FAIL", blocker="real", sandbox=False, prefix=[decoy],
                suffix=[{"type": "item.completed", "item": {"type": "agent_message", "text": "AFTER"}}],
            )
        )
        self.assertEqual(later["verdict"], "FAIL")
        bad_pass = self.inspect(
            self.write_log(verdict="PASS", blocker="not-none", sandbox=False)
        )
        self.assertEqual(bad_pass["reason"], "pass-blocker-not-none")
        interposed = self.write_log(verdict="PASS", blocker="none", sandbox=False)
        rows = [json.loads(line) for line in interposed.read_text().splitlines()]
        rows.insert(-1, {"type": "item.completed", "item": {
            "type": "command_execution", "exit_code": 0,
            "aggregated_output": "INTERPOSED_RAW_SENTINEL"}})
        interposed.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        rejected = self.inspect(interposed)
        self.assertEqual(rejected["reason"], "missing-final-agent-message")
        self.assertNotIn("INTERPOSED_RAW_SENTINEL", repr(rejected))

    # Supervisor observation rows between the final message and the terminal --
    # measured live: 25 of 60 owner logs put `agent_message` directly before
    # `turn.completed`, 10 put `dispatch.supervisor.token_usage` in between,
    # and a valid handoff already existed in every one of those 10.
    def test_supervisor_token_usage_between_final_message_and_terminal_is_read(self):
        report = self.root / "telemetry_report.md"
        report.write_text("# report\n", encoding="utf-8")
        usage = {
            "type": "dispatch.supervisor.token_usage",
            "thread_id": "th1",
            "turn_id": "t1",
            "token_usage": {"input_tokens": 11, "output_tokens": 7},
        }
        single = self.inspect(
            self.write_log(
                verdict="PASS", blocker="none", artifact=str(report),
                sandbox=False, between=[usage],
            )
        )
        self.assertEqual(single["reason"], "none")
        self.assertEqual(
            (single["exit_code"], single["verdict"], single["artifact_state"]),
            (0, "PASS", "readable"),
        )
        repeated = self.inspect(
            self.write_log(
                verdict="FAIL", blocker="one blocking finding", artifact=str(report),
                sandbox=False, between=[usage, dict(usage, turn_id="t1b"), usage],
            )
        )
        self.assertEqual(repeated["reason"], "none")
        # a worker-reported FAIL with a readable artifact is a legal wire row
        self.assertEqual(
            (repeated["exit_code"], repeated["verdict"], repeated["blocker_reason"]),
            (0, "FAIL", "worker-reported"),
        )

    def test_receiving_turn_boundary_preserves_only_completed_handoff(self):
        report = self.root / "turn_report.md"
        report.write_text("# report\n", encoding="utf-8")
        boundary = {"type": "dispatch.supervisor.turn.completed", "thread_id": "th1",
                    "turn_id": "t1", "status": "completed"}
        for event, valid in [(boundary, True), (dict(boundary, status="failed"), False),
                             (dict(boundary, turn_id=None), False)]:
            with self.subTest(event=event):
                result = self.inspect(self.write_log(verdict="PASS", blocker="none",
                    artifact=str(report), sandbox=False, between=[event]))
                self.assertEqual(result["exit_code"] == 0, valid, result)

    def test_terminal_walkback_stops_at_work_items_boundaries_and_unknown_rows(self):
        report = self.root / "walkback_report.md"
        report.write_text("# report\n", encoding="utf-8")
        usage = {"type": "dispatch.supervisor.token_usage", "token_usage": {}}
        blockers = {
            # a real tool item after the message: the message was not final
            "command-item-completed": [{
                "type": "item.completed",
                "item": {"type": "command_execution", "exit_code": 0,
                         "aggregated_output": "WALKBACK_RAW_SENTINEL"},
            }],
            "command-item-started": [{
                "type": "item.started",
                "item": {"type": "command_execution"},
            }],
            # never skip a turn boundary back onto an older turn's handoff
            "previous-turn-boundary": [
                {"type": "dispatch.supervisor.turn.started", "turn_id": "t2"}
            ],
            # an unknown supervisor/observation row is not silently skipped
            "unknown-supervisor-row": [
                {"type": "dispatch.supervisor.owner-boundary", "new_count": 1}
            ],
            "unknown-event": [{"type": "some.future.event"}],
            # malformed rows fail closed rather than walking further back
            "malformed-item": [{"type": "item.completed", "item": "not-a-dict"}],
            "telemetry-then-work-item": [
                usage,
                {"type": "item.completed",
                 "item": {"type": "command_execution", "exit_code": 0}},
            ],
        }
        for name, between in blockers.items():
            with self.subTest(interposed=name):
                rejected = self.inspect(
                    self.write_log(
                        verdict="PASS", blocker="none", artifact=str(report),
                        sandbox=False, between=between,
                    )
                )
                self.assertEqual(rejected["reason"], "missing-final-agent-message")
                self.assertEqual(rejected["exit_code"], 3)
                self.assertNotIn("WALKBACK_RAW_SENTINEL", repr(rejected))

    def test_completed_owner_input_receipt_keeps_exact_pass_handoff(self):
        # rt-73b900d0187892c7: the supervisor acknowledged four accepted
        # corrections after the final PASS and before its terminal event.
        report = self.root / "owner_report.md"
        report.write_text("# verified spec transaction\n")
        boundary = {"type": "dispatch.supervisor.turn.completed", "thread_id": "th1",
                    "turn_id": "t1", "status": "completed"}
        receipt = {"type": "dispatch.supervisor.input", "attempt_id": "att-owner",
                   "turn_id": "t1", "state": "turn-completed",
                   "request_ids": ["input-1", "input-2", "input-3", "input-4"]}
        log = self.write_log(verdict="PASS", blocker="none", artifact=str(report), sandbox=False,
                             between=[{"type": "dispatch.supervisor.token_usage"}, boundary, receipt])
        result = self.inspect(log)
        self.assertEqual((result["state"], result["verdict"], result["artifact_state"]),
                         ("valid", "PASS", "readable"), result)
        self.assertEqual(result["artifact_path_b64"],
                         base64.urlsafe_b64encode(str(report).encode()).decode().rstrip("="))

        invalid_windows = {
            "no-completed-boundary": [receipt],
            "receipt-before-boundary": [receipt, boundary],
            "failed-boundary": [dict(boundary, status="failed"), receipt],
            "foreign-turn": [boundary, dict(receipt, turn_id="t2")],
            "foreign-thread": [boundary, dict(receipt, thread_id="th2")],
            "unsettled-input": [boundary, dict(receipt, state="accepted")],
            "missing-attempt": [boundary, dict(receipt, attempt_id=None)],
            "mixed-attempts": [boundary, receipt, dict(receipt, attempt_id="att-other")],
            "missing-requests": [boundary, dict(receipt, request_ids=[])],
            "bad-requests": [boundary, dict(receipt, request_ids=[{}])],
            "duplicate-requests": [boundary, dict(receipt, request_ids=["input-1", "input-1"])],
            "later-work": [boundary, receipt, {"type": "item.completed",
                                                "item": {"type": "command_execution"}}],
            "later-turn": [boundary, receipt, {"type": "dispatch.supervisor.turn.started"}],
        }
        for case, between in invalid_windows.items():
            with self.subTest(case=case):
                result = self.inspect(self.write_log(verdict="PASS", blocker="none",
                    artifact=str(report), sandbox=False, between=between))
                self.assertEqual(result["reason"], "missing-final-agent-message", result)

        # Even a valid receipt cannot manufacture an absent final message or
        # hide an unreadable artifact / contradictory PASS envelope.
        for changes, reason in [({"artifact": str(self.root / "missing.md")}, "artifact-missing"),
                                ({"blocker": "still blocked"}, "pass-blocker-not-none"),
                                ({"final_text": "no handoff"}, "malformed-handoff")]:
            args = dict(verdict="PASS", blocker="none", artifact=str(report), sandbox=False,
                        between=[boundary, receipt])
            result = self.inspect(self.write_log(**(args | changes)))
            self.assertEqual(result["reason"], reason, result)

    def test_walkback_never_promotes_an_older_agent_message(self):
        stale = {
            "type": "item.completed",
            "item": {"type": "agent_message",
                     "text": "artifact: -\nverdict: PASS\nblocker: none"},
        }
        # the newest agent_message has no text: fail closed on it rather than
        # letting the previous turn's stale handoff pass the gate
        textless = self.write_log(
            verdict="FAIL", blocker="real", sandbox=False, prefix=[stale],
            between=[{"type": "dispatch.supervisor.token_usage", "token_usage": {}}],
        )
        rows = [json.loads(line) for line in textless.read_text().splitlines()]
        rows[-3] = {"type": "item.completed", "item": {"type": "agent_message"}}
        textless.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
        rejected = self.inspect(textless)
        self.assertEqual(rejected["reason"], "missing-final-agent-message")

    def test_artifact_missing_and_symlink_escape_fail_closed(self):
        missing = self.inspect(
            self.write_log(
                verdict="FAIL", blocker="x", artifact=self.root / "missing.md", sandbox=False
            )
        )
        self.assertEqual((missing["exit_code"], missing["artifact_state"]), (3, "missing"))
        outside = self.base / "outside.md"
        outside.write_text("RAW_OUTSIDE_SENTINEL")
        link = self.root / "escape.md"
        link.symlink_to(outside)
        escaped = self.inspect(
            self.write_log(verdict="FAIL", blocker="x", artifact=link, sandbox=False)
        )
        self.assertEqual(escaped["artifact_state"], "outside-root")
        self.assertNotIn("RAW_OUTSIDE_SENTINEL", repr(escaped))

    def test_a_directory_artifact_is_a_valid_deliverable(self):
        # 2026-09-04: three depth-1 codex owners emitted a well-formed
        # `verdict: PASS` envelope naming a cycle document directory and were all
        # recorded `dead-invalid-envelope / terminal-invalid:artifact-missing`.
        # The contract says "an in-root artifact", never "a regular file".
        bucket = self.root / "documents" / "2026-09-04_cmdset_v6_final"
        bucket.mkdir(parents=True)
        (bucket / "CONTRACT.md").write_text("one\n", encoding="utf-8")
        (bucket / "EVIDENCE.md").write_text("two\n", encoding="utf-8")
        result = self.inspect(
            self.write_log(verdict="PASS", blocker="none", artifact=bucket, sandbox=False)
        )
        self.assertEqual(result["state"], "valid")
        self.assertEqual(result["artifact_state"], "readable")
        self.assertEqual(result["artifact_shape"], "directory")
        decoded = base64.urlsafe_b64decode(
            result["artifact_path_b64"] + "=" * (-len(result["artifact_path_b64"]) % 4)
        ).decode("utf-8")
        self.assertEqual(Path(decoded), bucket.resolve())

    def test_a_single_file_artifact_is_unchanged(self):
        single = self.root / "report.md"
        single.write_text("body\n", encoding="utf-8")
        result = self.inspect(
            self.write_log(verdict="PASS", blocker="none", artifact=single, sandbox=False)
        )
        self.assertEqual(
            (result["state"], result["artifact_state"], result["artifact_shape"]),
            ("valid", "readable", "file"),
        )

    def test_an_empty_directory_is_not_a_deliverable(self):
        # The harness pre-creates the cycle's `artifacts/` directory before the
        # worker's first write and exports it as AGENT_ARTIFACT_OUTPUT_DIR, so a
        # worker that produced nothing could otherwise name the path it was handed
        # and pass. Existence is not evidence of work.
        empty = self.root / "documents" / "empty_bucket"
        empty.mkdir(parents=True)
        result = self.inspect(
            self.write_log(verdict="PASS", blocker="none", artifact=empty, sandbox=False)
        )
        self.assertEqual(result["state"], "invalid")
        self.assertEqual(result["reason"], "artifact-empty-directory")
        # A directory holding only subdirectories is not empty, but it holds no
        # content the digest can attest, so it gets its own reason.
        (empty / "nested" / "deeper").mkdir(parents=True)
        self.assertEqual(
            self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=empty, sandbox=False)
            )["reason"],
            "artifact-no-regular-file",
        )
        # One readable regular file anywhere in the tree is enough.
        (empty / "nested" / "REPORT.md").write_text("body\n", encoding="utf-8")
        self.assertEqual(
            self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=empty, sandbox=False)
            )["artifact_state"],
            "readable",
        )

    def test_one_unreadable_member_refuses_at_classification_not_at_completion(self):
        # Review round 3 (a): returning valid as soon as ONE readable file exists
        # let a bucket classify valid and then raise `evidence-member-unreadable`
        # at completion — the disagreement this shared rule exists to remove.
        bucket = self.root / "documents" / "partly_unreadable"
        bucket.mkdir(parents=True)
        (bucket / "REPORT.md").write_text("body\n", encoding="utf-8")
        blocked = bucket / "locked.md"
        blocked.write_text("secret\n", encoding="utf-8")
        os.chmod(blocked, 0o000)
        try:
            if os.access(blocked, os.R_OK):
                self.skipTest("cannot drop file access as this user")
            result = self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=bucket, sandbox=False)
            )
            self.assertEqual(result["reason"], "artifact-unreadable-member")
        finally:
            os.chmod(blocked, 0o600)

    def test_a_directory_member_may_not_escape_the_artifact_root(self):
        # The completion marker attests this tree; a consumer that later copies or
        # publishes it would follow a link pointing out of the root.
        outside = self.base / "outside.md"
        outside.write_text("RAW_OUTSIDE_SENTINEL", encoding="utf-8")
        bucket = self.root / "documents" / "escaping"
        bucket.mkdir(parents=True)
        (bucket / "REPORT.md").write_text("body\n", encoding="utf-8")
        (bucket / "escape.md").symlink_to(outside)
        result = self.inspect(
            self.write_log(verdict="PASS", blocker="none", artifact=bucket, sandbox=False)
        )
        self.assertEqual(result["reason"], "artifact-member-outside-root")
        self.assertNotIn("RAW_OUTSIDE_SENTINEL", repr(result))

    def test_present_but_unreadable_is_told_apart_from_absent(self):
        # "the path is not there" and "the path is there but this reader will not
        # take it" are different operator actions, so they get different reasons.
        absent = self.inspect(
            self.write_log(
                verdict="PASS", blocker="none", artifact=self.root / "nope.md", sandbox=False
            )
        )
        self.assertEqual(absent["reason"], "artifact-missing")
        locked = self.root / "locked"
        locked.mkdir()
        (locked / "inner.md").write_text("x\n", encoding="utf-8")
        os.chmod(locked, 0o000)
        try:
            if os.access(locked, os.R_OK | os.X_OK):
                self.skipTest("cannot drop directory access as this user")
            result = self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=locked, sandbox=False)
            )
            self.assertEqual(result["reason"], "artifact-unreadable")
        finally:
            os.chmod(locked, 0o700)

    def test_failure_detail_is_opt_in_escaped_independently_bounded_and_never_pass(self):
        blocker = "B\t" + "한" * 400
        diagnostic = "D\n\x01" + "界" * 400
        path = self.write_log(
            verdict="FAIL", blocker=blocker, sandbox=False, diagnostic=diagnostic
        )
        default = self.inspect(path)
        self.assertNotIn("excerpt", repr(default))
        detailed = self.inspect(path, detail=True)
        for key, truncated_key in (
            ("blocker_detail_excerpt", "blocker_detail_truncated"),
            ("failure_diagnostic_excerpt", "failure_diagnostic_truncated"),
        ):
            self.assertLessEqual(len(detailed[key].encode("utf-8")), 512)
            self.assertNotIn("\n", detailed[key])
            self.assertNotIn("\t", detailed[key])
            self.assertEqual(detailed[truncated_key], 1)
        passed = self.inspect(
            self.write_log(verdict="PASS", blocker="none", sandbox=False), detail=True
        )
        self.assertNotIn("excerpt", repr(passed))

    def test_agent_messages_never_source_failure_diagnostics(self):
        result = self.inspect(
            self.write_log(
                verdict="FAIL",
                blocker="RAW_AGENT_DIAGNOSTIC_SENTINEL",
                sandbox=False,
            ),
            detail=True,
        )
        self.assertNotIn("failure_diagnostic_excerpt", result)

    def test_every_legal_wire_tuple_and_fallback_record_are_closed(self):
        for row in terminal._LEGAL_WIRE:
            with self.subTest(row=row):
                result = dict(
                    exit_code=row[0], state=row[1], source=row[2], verdict=row[3],
                    artifact_state=row[4], blocker_reason=row[5], reason="fixture",
                )
                record = terminal.wire_record(result)
                self.assertEqual(len(record.rstrip("\n").split("\t")), 6)
                self.assertNotIn(str(self.root), record)
        illegal = dict(
            exit_code=0, state="valid", source="none", verdict="PASS",
            artifact_state="unsafe-root", blocker_reason="worker-reported", reason="bad",
        )
        self.assertEqual(
            terminal.wire_record(illegal),
            "codex-terminal-v1\terror\truntime-error\t-\tunchecked\tcontract-violation\n",
        )

    def test_cli_exit_and_single_record_matrix(self):
        script = Path(terminal.__file__)
        valid = self.write_log(verdict="PASS", blocker="none", sandbox=False)
        absent = self.write_log(completed=False, sandbox=False)
        invalid = self.write_log(final_text="bad", sandbox=False)
        for expected, path in ((0, valid), (2, absent), (3, invalid)):
            result = subprocess.run(
                [sys.executable, str(script), "--worktree", str(self.worktree),
                 "--artifact-root-metadata", str(self.root), str(path)],
                text=True, capture_output=True,
                env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.root)},
            )
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(len(result.stdout.rstrip().split("\t")), 6)
        error = subprocess.run(
            [sys.executable, str(script), "--worktree", str(self.worktree),
             "--artifact-root-metadata", str(self.root), str(valid)],
            text=True, capture_output=True,
            env={**os.environ, "AGENT_ARTIFACT_ROOT": "relative-root"},
        )
        self.assertEqual(error.returncode, 4)
        self.assertIn("\tunsafe-root\t", error.stdout)
        misuse = subprocess.run([sys.executable, str(script)], text=True, capture_output=True)
        self.assertEqual(misuse.returncode, 64)
        self.assertEqual(misuse.stdout, "")

    def test_unsafe_root_variants_share_one_public_outcome(self):
        log = self.write_log(verdict="PASS", blocker="none", sandbox=False)
        variants = []
        missing = self.base / "missing-root"
        non_directory = self.base / "root-file"
        non_directory.write_text("x")
        real = self.base / "real-root"
        real.mkdir()
        symlink = self.base / "root-link"
        symlink.symlink_to(real, target_is_directory=True)
        variants.extend(("relative-root", missing, non_directory, self.base, symlink))
        for value in variants:
            with self.subTest(root=value), mock.patch.dict(
                os.environ, {"AGENT_ARTIFACT_ROOT": str(value)}, clear=False
            ):
                result = self.inspect(log, metadata=value)
                self.assertEqual(
                    (result["exit_code"], result["state"], result["source"],
                     result["verdict"], result["artifact_state"], result["blocker_reason"]),
                    (4, "error", "runtime-error", "-", "unsafe-root", "contract-violation"),
                )
        mismatch = self.inspect(log, metadata=self.base / "other-root")
        self.assertEqual(mismatch["artifact_state"], "unsafe-root")

    def test_control_worktree_and_oversized_tail_fail_without_raw_leakage(self):
        path = self.write_log(verdict="PASS", blocker="none", sandbox=False)
        controlled = inspect_terminal_attempt(
            path,
            worktree=str(self.worktree) + "\nRAW_PATH_SENTINEL",
            artifact_root_metadata=self.root,
        )
        self.assertEqual(controlled["artifact_state"], "unsafe-root")
        self.assertNotIn("RAW_PATH_SENTINEL", repr(controlled))
        with path.open("r+", encoding="utf-8") as handle:
            original = handle.read()
            handle.seek(0)
            handle.write("X" * (terminal._MAX_TAIL_BYTES + 100) + "\n" + original)
        self.assertEqual(self.inspect(path)["verdict"], "PASS")

    def test_linked_worktree_shadow_root_is_fixed_unsafe_root(self):
        primary = self.base / "primary"
        linked = self.base / "linked"
        primary.mkdir()
        subprocess.run(["git", "init", "-q", str(primary)], check=True)
        subprocess.run(["git", "-C", str(primary), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(primary), "config", "user.name", "Fixture"], check=True)
        (primary / "x").write_text("x")
        subprocess.run(["git", "-C", str(primary), "add", "x"], check=True)
        subprocess.run(["git", "-C", str(primary), "commit", "-qm", "init"], check=True)
        (primary / ".agent_reports").mkdir()
        subprocess.run(
            ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked-fixture", str(linked)],
            check=True,
        )
        shadow = linked / ".agent_reports"
        shadow.mkdir()
        log = self.write_log(verdict="PASS", blocker="none", sandbox=False)
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(shadow)}, clear=False):
            result = inspect_terminal_attempt(
                log, worktree=linked, artifact_root_metadata=shadow
            )
        self.assertEqual(
            (result["exit_code"], result["state"], result["artifact_state"]),
            (4, "error", "unsafe-root"),
        )
        with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(primary)}, clear=False):
            over_broad = inspect_terminal_attempt(
                log, worktree=linked, artifact_root_metadata=primary
            )
        self.assertEqual(
            (over_broad["exit_code"], over_broad["state"], over_broad["artifact_state"]),
            (4, "error", "unsafe-root"),
        )


class PrimaryWorktreeOnceTest(unittest.TestCase):
    """HOOK-LATENCY: an OpenCode turn's status inspected 16 attempts and ran
    `git worktree list` for each; 8 of 9 named one NAS repository (0.65 s each)."""

    def test_one_listing_per_repository_and_a_failure_is_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "main"
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
            def git(*args):
                subprocess.run(["git", *args], check=True, env=env, capture_output=True)
            git("init", "-q", str(main))
            git("-C", str(main), "-c", "user.name=t", "-c", "user.email=t@e",
                "commit", "-q", "--allow-empty", "-m", "base")
            linked = [Path(tmp) / f"wt-{index}" for index in range(2)]
            for index, path in enumerate(linked):
                git("-C", str(main), "worktree", "add", "-q", "-b", f"b{index}", str(path))
            listings = []
            real_run = subprocess.run

            def counted(command, *args, **kwargs):
                if command[-3:] == ["worktree", "list", "--porcelain"]:
                    listings.append(command[2])
                return real_run(command, *args, **kwargs)

            cache = getattr(terminal, "_PRIMARY_WORKTREES", {})
            with mock.patch.dict(cache, clear=True), \
                    mock.patch.object(terminal.subprocess, "run", side_effect=counted):
                answers = [terminal._primary_worktree(path.resolve())
                           for path in (*linked, linked[0], main)]
                self.assertEqual(answers, [main.resolve()] * 4)
                self.assertEqual(len(listings), 1, listings)
                cache.clear()
                with mock.patch.object(terminal.subprocess, "run",
                                       side_effect=OSError("git unavailable")):
                    self.assertIsNone(terminal._primary_worktree(linked[1].resolve()))
                self.assertEqual(cache, {})
                self.assertEqual(terminal._primary_worktree(linked[1].resolve()), main.resolve())


class OpencodeTerminalRecognitionTest(unittest.TestCase):
    """Gap 1 (C2): codex_dispatch_terminal now recognizes the opencode
    step_finish/stop terminal via the shared opencode_terminal_boundary
    helper (R1 wired into codex_dispatch_terminal._read_terminal)."""

    def test_terminal_stop_fixture_is_verdict_pass(self):
        parsed = inspect_terminal_log(str(FIXTURES / "terminal-stop.jsonl"))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["verdict"], "PASS")
        self.assertEqual(parsed["terminal_event"], "step_finish")

    def test_terminal_stop_wire_record_is_not_silently_overwritten_to_error(self):
        # R1: exact-step-finish-stop must be registered in _TERMINAL_SOURCES,
        # or wire_record() falls through to the catch-all (4, error, ...) row.
        # Go through inspect_terminal_attempt (as real callers do) so
        # artifact_state is resolved ("none" here) instead of "unchecked" —
        # needs a real git worktree + artifact root for _resolve_safe_root.
        with tempfile.TemporaryDirectory() as base_dir:
            worktree = Path(base_dir) / "repo"
            worktree.mkdir()
            subprocess.run(["git", "init", "-q", str(worktree)], check=True)
            root = Path(base_dir) / ".agent_reports"
            root.mkdir()
            with mock.patch.dict(os.environ, {"AGENT_ARTIFACT_ROOT": str(root)}, clear=False):
                result = inspect_terminal_attempt(
                    str(FIXTURES / "terminal-stop.jsonl"),
                    worktree=worktree,
                    artifact_root_metadata=root,
                )
        self.assertEqual(result["state"], "valid")
        record = wire_record(result)
        self.assertNotIn("error\trunt", record)
        self.assertIn("valid", record)
        self.assertIn("PASS", record)

    def test_broken_stream_fixture_has_no_terminal_event(self):
        parsed = inspect_terminal_log(str(FIXTURES / "broken-stream.jsonl"))
        self.assertIsNone(parsed)

    def test_truncated_permission_reject_fixture_has_no_terminal_event(self):
        # R2 (dead-permission-reject) is not wired into this reader in C2 —
        # only the post-exit watcher (dispatch_supervisor_terminal) gains it
        # in C3. Here the fixture correctly reads as "no exact terminal".
        parsed = inspect_terminal_log(str(FIXTURES / "truncated-permission-reject.jsonl"))
        self.assertIsNone(parsed)


class ExactBoundaryTableTest(unittest.TestCase):
    """Each harness's own turn boundary is named once; readers ask the table."""

    def test_each_harness_recognizes_only_its_own_boundary(self):
        envelope = "artifact: -\nverdict: FAIL\nblocker: tests failed"
        logs = {
            "codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": envelope}},
                      {"type": "turn.completed"}],
            "claude": [{"type": "result", "subtype": "success", "is_error": False, "result": envelope}],
            "opencode": [{"type": "text", "part": {"type": "text", "text": envelope}},
                         {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            for harness, rows in logs.items():
                path = Path(tmp) / f"{harness}.jsonl"
                path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                parsed = terminal._read_terminal(path)
                if harness == "claude":
                    parsed = {**parsed, "native_completed": True}
                with self.subTest(harness=harness):
                    self.assertEqual(parsed["verdict"], "FAIL")
                    self.assertEqual({h: terminal.exact_boundary(parsed, h) for h in logs},
                                     {h: h == harness for h in logs})
                    self.assertNotEqual(terminal.boundary_label(parsed["source"]), parsed["source"])
        self.assertFalse(terminal.exact_boundary({"source": "exact-claude-result"}, "claude"))
        self.assertEqual(set(terminal.EXACT_BOUNDARY_SOURCES), {"claude", "codex", "opencode"})


if __name__ == "__main__":
    unittest.main()
