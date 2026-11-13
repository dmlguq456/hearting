#!/usr/bin/env python3
"""Hermetic OpenCode dispatch CLI and scoped worker-home projection fixture."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from codex_dispatch_terminal import inspect_terminal_attempt
from dispatch_supervisor_terminal import opencode_terminal_boundary

SPEC = importlib.util.spec_from_file_location(
    "opencode_dispatch_scope_fixture",
    ROOT / "adapters/opencode/bin/dispatch-headless.py",
)
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)


def _terminal_session_matches(rows, expected_sid):
    terminal_index, _text = opencode_terminal_boundary(rows)
    if terminal_index is None:
        return False
    terminal_row = rows[terminal_index]
    final_text_row = next((row for row in reversed(rows[:terminal_index])
                           if row.get("type") == "text"), None)
    return (terminal_row.get("sessionID") == expected_sid
            and isinstance(final_text_row, dict)
            and final_text_row.get("sessionID") == expected_sid)


class OpenCodeDispatchScopeFixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-opencode-dispatch-scope-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.worktree = self.base / "worktree"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q", str(self.worktree)], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.email",
                        "fixture@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "config", "user.name", "Fixture"],
                       check=True)
        (self.worktree / "fixture.txt").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.worktree), "add", "fixture.txt"], check=True)
        subprocess.run(["git", "-C", str(self.worktree), "commit", "-qm", "fixture"], check=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.artifact_root = self.base / "dispatch-artifact-root" / ".agent_reports"
        self.artifact_root.mkdir(parents=True)
        self.outside_root = self.base / "dispatch-outside"
        self.outside_root.mkdir()
        self.state_root = self.base / "state"
        self.state_root.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "AGENT_HOME": str(ROOT),
            "AGENT_ARTIFACT_ROOT": str(self.artifact_root),
            "AGENT_DISPATCH_JOBS": str(self.state_root / "jobs.log"),
            "HARNESS_STATE_ROOT": str(self.state_root),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local/share"),
            "XDG_STATE_HOME": str(self.state_root),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "OPENCODE_RUNTIME_VERSION": "2.0.32",
            "OPENCODE_CONFIG_CONTENT": "{}",
            "FLEET_USAGE_STATE_DIR": str(self.state_root / "usage-state"),
        }

    def test_real_dispatch_surface_builds_worker_scope_and_native_identity(self):
        argv = [
            "dispatch-headless.py", "--dry-run", "--worktree", str(self.worktree),
            "--slug", "native-hook-fixture", "--capability", "autopilot-code",
            "--capability-mode", "dev", "--worker-mode", "dev/backend",
            "--worker-type", "stage", "--assigned-contract", "code-execute",
            "--unit", "dev/backend", "--dispatch-depth", "1", "--intensity", "standard",
            "--registered-worker", "1", "--launch-lifecycle", "foreground-scoped",
            "--foreground-timeout", "240", "--model", "openai/model-x",
            "--variant", "runtime-default", "--prompt-text",
            "Run only the isolated guard fixture.",
        ]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(DISPATCH.main(argv), 0)
        receipt = output.getvalue()
        self.assertIn("status=dry-run", receipt)
        self.assertIn(f"worktree={self.worktree}", receipt)
        self.assertIn(f"artifact_root={self.artifact_root}", receipt)
        self.assertIn("worker_mode=dev/backend", receipt)
        self.assertIn("dispatch_depth=1", receipt)
        self.assertFalse(self.artifact_root.is_relative_to(self.worktree))
        artifact_probe = self.artifact_root / "native-write-probe.txt"
        blocked_probe = self.outside_root / "blocked.txt"
        self.assertTrue(artifact_probe.is_relative_to(self.artifact_root))
        self.assertFalse(artifact_probe.is_relative_to(self.worktree))

        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertFalse(hasattr(DISPATCH, "prepare_nested_runtime"))
            scoped = DISPATCH.scoped_external_directory_config(
                str(self.artifact_root), agent_home=str(ROOT), worktree=str(self.worktree),
                selected_agent="build",
            )
            launch_env = {
                **self.env,
                "OPENCODE_RUNTIME_VERSION": "1.19.0",
                "AGENT_SESSION_ROLE": "worker",
                "AGENT_DISPATCH_CHILD": "1",
                "AGENT_DISPATCH_DEPTH": "1",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-native-fixture",
                "AGENT_DISPATCH_WORKTREE": str(self.worktree),
                "OPENCODE_CONFIG_CONTENT": scoped,
            }
            projected = DISPATCH.prepare_worker_home(
                ROOT, "opencode", "stage", "att-native-fixture", env=launch_env,
            )
        effective = {**launch_env, **projected}
        self.assertTrue(Path(effective["HEARTING_WORKER_HOME"]).is_relative_to(self.state_root))
        self.assertIsNone(projected.get("OPENCODE_DB"))
        config = json.loads(effective["OPENCODE_CONFIG_CONTENT"])
        permission = config.get("permission", config.get("permissions", {}))
        if isinstance(permission, list):
            external = [row for row in permission if row.get("action") == "external_directory"]
            self.assertTrue(external)
            self.assertIn({"action": "external_directory", "resource": "*", "effect": "deny"},
                          external)
            self.assertIn({"action": "external_directory", "resource": str(self.artifact_root),
                           "effect": "allow"}, external)
            self.assertNotIn({"action": "external_directory", "resource": str(self.outside_root),
                              "effect": "allow"}, external)
        else:
            external = permission.get("external_directory", {})
            self.assertEqual(external.get("*"), "deny")
            self.assertEqual(external.get(str(self.artifact_root)), "allow")
            self.assertNotEqual(external.get(str(self.outside_root)), "allow")

        bundle_path = os.environ.get("HEARTING_OPENCODE_SUPERVISOR_BUNDLE")
        if bundle_path:
            bundle = Path(bundle_path).read_text(encoding="utf-8")
            self.assertIn('ARTIFACT_ROOT="$WORK/dispatch-artifact-root/.agent_reports"', bundle)
            self.assertIn('HANDOFF_ARTIFACT="$ARTIFACT_ROOT/native-dispatch-handoff.md"', bundle)
            self.assertIn("Finish with exactly Hearting's normal three-line worker handoff", bundle)
            self.assertNotIn("DISPATCH_GUARD_DONE", bundle)
            self.assertNotIn("Use the write tool exactly once", bundle)
            self.assertIn('artifact_probe.read_bytes() != b"hearting-artifact-probe-v2\\n"', bundle)
            self.assertIn("native session log has no failed outside-scope write tool call", bundle)
            self.assertIn("native session log has no completed environment-capture tool result", bundle)
            self.assertIn("terminal text/step_finish session mismatch", bundle)
            self.assertIn("dispatch_attempt_log_binding=exact", bundle)
            self.assertIn("codex_dispatch_terminal.inspect_terminal_attempt", bundle)
            self.assertNotIn('"allowed_write": True', bundle)

        prompt = self.state_root / "probe.prompt"
        log = self.state_root / "probe.jsonl"
        command_args = type("DispatchArgs", (), {
            "worktree": str(self.worktree), "agent": "build", "attempt_id": "att-native-fixture",
            "resolved_model_settings": {"source": "inherit"},
        })()
        with mock.patch.dict(os.environ, self.env, clear=True):
            command = DISPATCH.shell_command(command_args, prompt, log)
        self.assertIn("hearting-worker-att-native-fixture", command)
        self.assertIn("--format json", command)

    def _terminal_log(self, sid, text, *, terminal_sid=None):
        report = self.artifact_root / "native-dispatch-handoff.md"
        report.write_text("Observed probe evidence.\n", encoding="utf-8")
        terminal_sid = terminal_sid or sid
        rows = [
            {"type": "step_start", "sessionID": sid, "part": {"type": "step-start"}},
            {"type": "text", "sessionID": sid,
             "part": {"type": "text", "text": text}},
            {"type": "step_finish", "sessionID": terminal_sid,
             "part": {"type": "step-finish", "reason": "stop"}},
        ]
        log = self.state_root / "native-attempt.jsonl"
        log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return log, report, rows

    def test_shared_terminal_parser_accepts_readable_handoff_from_exact_session(self):
        sid = "ses-native-probe"
        report = self.artifact_root / "native-dispatch-handoff.md"
        log, _report, rows = self._terminal_log(
            sid, f"artifact: {report}\nverdict: PASS\nblocker: none")
        result = inspect_terminal_attempt(
            log, worktree=self.worktree, artifact_root_metadata=self.artifact_root,
            artifact_binding={"attempt_id": "att-native-probe"})
        self.assertTrue(_terminal_session_matches(rows, sid))
        self.assertEqual(result["state"], "valid")
        self.assertEqual(result["source"], "exact-step-finish-stop")
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["artifact_state"], "readable")

    def test_marker_only_malformed_handoff_and_wrong_session_are_rejected(self):
        sid = "ses-native-probe"
        for name, text in (
            ("marker-only", "DISPATCH_GUARD_DONE"),
            ("malformed", "artifact: report.md\nverdict: COMPLETE\nblocker: none"),
        ):
            with self.subTest(result=name):
                log, _report, rows = self._terminal_log(sid, text)
                result = inspect_terminal_attempt(
                    log, worktree=self.worktree, artifact_root_metadata=self.artifact_root,
                    artifact_binding={"attempt_id": "att-native-probe"})
                self.assertEqual(result["state"], "invalid")
                self.assertEqual(result["reason"], "malformed-handoff")
                self.assertTrue(_terminal_session_matches(rows, sid))

        handoff = f"artifact: {self.artifact_root / 'native-dispatch-handoff.md'}\nverdict: PASS\nblocker: none"
        log, _report, rows = self._terminal_log(sid, handoff, terminal_sid="ses-other")
        result = inspect_terminal_attempt(
            log, worktree=self.worktree, artifact_root_metadata=self.artifact_root,
            artifact_binding={"attempt_id": "att-native-probe"})
        self.assertEqual(result["state"], "valid")
        self.assertFalse(_terminal_session_matches(rows, sid))


if __name__ == "__main__":
    unittest.main()
