#!/usr/bin/env python3
"""A one-shot OpenCode V2 stream closes through the shared terminal reader."""
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
SPEC = importlib.util.spec_from_file_location("opencode_run_terminal", ROOT / "utilities/opencode-run-terminal.py")
TERMINAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TERMINAL)
SESSION = "ses_v2fixture01"


def stream(final_text, *, error=None, session=SESSION):
    """The 2.0.32 shape: tool steps, then a last step that ends at its text, no stop."""
    rows = [
        {"type": "step_start", "sessionID": session, "part": {"type": "step-start"}},
        {"type": "tool_use", "sessionID": session,
         "part": {"type": "tool", "tool": "write", "state": {"status": "completed"}}},
        {"type": "step_finish", "sessionID": session, "part": {"type": "step-finish", "reason": "tool-calls"}},
        {"type": "step_start", "sessionID": session, "part": {"type": "step-start"}},
        {"type": "text", "sessionID": session, "part": {"type": "text", "text": final_text}},
    ]
    if error is not None:
        rows.append({"type": "error", "sessionID": session, "error": error})
    return "".join(json.dumps(row) + "\n" for row in rows)


class OpenCodeRunTerminalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opencode-run-terminal-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.artifacts = self.root / ".agent_reports"
        self.artifacts.mkdir()
        self.artifact = self.artifacts / "handoff.md"
        self.artifact.write_text("# handoff\n", encoding="utf-8")
        self.handoff = f"artifact: {self.artifact}\nverdict: PASS\nblocker: none"
        self.log = self.root / "attempt.opencode.jsonl"

    def append(self, text, code):
        self.log.write_text(text + "permission note from the CLI\n", encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "utilities/opencode-run-terminal.py"),
                        "--log", str(self.log), "--exit-code", str(code)], check=True)
        return json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])

    def check(self):
        return subprocess.run(
            [sys.executable, str(ROOT / "utilities/codex_dispatch_terminal.py"), "--worktree",
             str(self.worktree), "--artifact-root-metadata", str(self.artifacts), str(self.log)],
            capture_output=True, text=True,
            env={**os.environ, "AGENT_ARTIFACT_ROOT": str(self.artifacts)})

    def test_v2_stream_without_stop_is_absent_until_the_result_row_closes_it(self):
        self.log.write_text(stream(self.handoff), encoding="utf-8")
        self.assertNotEqual(self.check().returncode, 0)
        row = self.append(stream(self.handoff), 0)
        self.assertEqual((row["subtype"], row["is_error"], row["session_id"]),
                         ("success", False, SESSION))
        checked = self.check()
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("\tvalid\texact-opencode-result\tPASS", checked.stdout)

    def test_failure_shapes_close_as_errors(self):
        cases = (
            ("nonzero exit", stream(self.handoff), 1, "opencode-exit-nonzero"),
            ("empty final text", stream(""), 0, "opencode-final-text-missing"),
            ("no session", "plain diagnostics only\n", 0, "opencode-session-not-observed"),
        )
        for label, text, code, reason in cases:
            with self.subTest(label):
                row = self.append(text, code)
                self.assertEqual((row["subtype"], row["is_error"], row["error"]["code"]),
                                 ("error_during_execution", True, reason))
        row = self.append(stream(self.handoff, error={"name": "APIError", "message": "boom"}), 0)
        self.assertTrue(row["is_error"])
        self.assertIn("error", row)

    def test_a_second_session_in_one_log_is_not_a_success(self):
        mixed = stream(self.handoff) + stream(self.handoff, session="ses_other01")
        row = self.append(mixed, 0)
        self.assertEqual(row["error"]["code"], "opencode-session-identity-mismatch")


if __name__ == "__main__":
    unittest.main()
