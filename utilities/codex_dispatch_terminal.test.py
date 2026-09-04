#!/usr/bin/env python3
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
        # A block that is not at the end is still not a handoff.
        trailing_prose = self.inspect(
            self.write_log(final_text="artifact: -\nverdict: PASS\nblocker: none\nRAW_AGENT_SENTINEL")
        )
        self.assertEqual(trailing_prose["reason"], "malformed-handoff")
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
        # A directory holding only empty subdirectories is the same case.
        (empty / "nested" / "deeper").mkdir(parents=True)
        self.assertEqual(
            self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=empty, sandbox=False)
            )["reason"],
            "artifact-empty-directory",
        )
        # One readable regular file anywhere in the tree is enough.
        (empty / "nested" / "REPORT.md").write_text("body\n", encoding="utf-8")
        self.assertEqual(
            self.inspect(
                self.write_log(verdict="PASS", blocker="none", artifact=empty, sandbox=False)
            )["artifact_state"],
            "readable",
        )

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


if __name__ == "__main__":
    unittest.main()
