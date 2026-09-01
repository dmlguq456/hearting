#!/usr/bin/env python3
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

import session_summary_trigger as T  # noqa: E402

HOOK_PATH = ROOT / "adapters" / "codex" / "hooks" / "userprompt-lifecycle.py"
HOOK_SPEC = importlib.util.spec_from_file_location(
    "codex_userprompt_lifecycle_test", HOOK_PATH
)
HOOK = importlib.util.module_from_spec(HOOK_SPEC)
sys.modules[HOOK_SPEC.name] = HOOK
HOOK_SPEC.loader.exec_module(HOOK)


class SessionSummaryTriggerTest(unittest.TestCase):
    def test_codex_resolves_only_exact_rollout_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            sessions = home / "sessions" / "2026" / "08" / "04"
            sessions.mkdir(parents=True)
            exact = sessions / "rollout-2026-08-04T00-00-00-exact-sid.jsonl"
            foreign = sessions / "rollout-2026-08-04T00-00-01-not-exact-sid-extra.jsonl"
            exact.write_text("{}\n")
            foreign.write_text("{}\n")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
                self.assertEqual(T._codex_transcript("exact-sid"), exact)

    def test_trigger_maps_phase_to_ticket_and_priority(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "session.jsonl"
            transcript.write_text("{}\n")
            with mock.patch("fleet.refresh_title.maybe_spawn", return_value=True) as spawn:
                self.assertTrue(T.trigger(
                    "claude", "sid", "final", str(transcript)))
            kwargs = spawn.call_args.kwargs
            self.assertEqual(kwargs["quota_class"], "final")
            self.assertEqual(kwargs["debounce"], 0)
            self.assertTrue(kwargs["priority"])

    def test_trigger_forwards_current_user_context_to_refresher(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "session.jsonl"
            transcript.write_text("{}\n")
            with mock.patch("fleet.refresh_title.maybe_spawn", return_value=True) as spawn:
                self.assertTrue(T.trigger(
                    "codex", "sid", "initial", str(transcript),
                    anchor_text="current user query",
                ))
            self.assertEqual(spawn.call_args.kwargs["anchor_text"], "current user query")

    def test_codex_initial_waits_for_post_hook_rollout_write(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "session.jsonl"
            transcript.write_text("{}\n")
            boundary = transcript.stat().st_mtime_ns

            def append_prompt():
                time.sleep(0.05)
                transcript.write_text('{}\n{"role":"user"}\n')

            writer = threading.Thread(target=append_prompt)
            writer.start()
            try:
                with mock.patch("fleet.refresh_title.maybe_spawn", return_value=True) as spawn:
                    self.assertTrue(T.trigger(
                        "codex", "sid", "initial", str(transcript),
                        wait_seconds=1, after_mtime_ns=boundary,
                    ))
            finally:
                writer.join()
            self.assertEqual(spawn.call_count, 1)

    def test_codex_initial_does_not_spawn_before_rollout_advances(self):
        with tempfile.TemporaryDirectory() as td:
            transcript = Path(td) / "session.jsonl"
            transcript.write_text("{}\n")
            boundary = transcript.stat().st_mtime_ns
            with mock.patch("fleet.refresh_title.maybe_spawn", return_value=True) as spawn:
                self.assertFalse(T.trigger(
                    "codex", "sid", "initial", str(transcript),
                    wait_seconds=0.02, after_mtime_ns=boundary,
                ))
            spawn.assert_not_called()

    def test_codex_initial_launcher_passes_post_hook_boundary(self):
        with mock.patch.object(T.time, "time_ns", return_value=123456789), \
             mock.patch.object(T.subprocess, "Popen") as popen:
            self.assertTrue(T.launch_trigger("codex", "sid", "initial"))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--after-mtime-ns") + 1], "123456789")

    def test_final_launcher_has_no_post_hook_boundary(self):
        with mock.patch.object(T.subprocess, "Popen") as popen:
            self.assertTrue(T.launch_trigger("codex", "sid", "final"))
        self.assertNotIn("--after-mtime-ns", popen.call_args.args[0])

    def test_initial_launcher_pipes_context_without_process_metadata(self):
        class Sink:
            def __init__(self):
                self.value = ""

            def write(self, value):
                self.value += value

            def close(self):
                pass

        process = types.SimpleNamespace(stdin=Sink())
        with mock.patch.object(T.subprocess, "Popen", return_value=process) as popen:
            self.assertTrue(T.launch_trigger(
                "codex", "sid", "initial", anchor_text="private current query"
            ))
        argv = popen.call_args.args[0]
        env = popen.call_args.kwargs["env"]
        self.assertIn("--anchor-stdin", argv)
        self.assertNotIn("private current query", " ".join(argv))
        self.assertNotIn("private current query", " ".join(env.values()))
        self.assertEqual(process.stdin.value, "private current query")

    def test_trigger_main_reads_bounded_context_from_stdin(self):
        with mock.patch.object(T.sys, "stdin", io.StringIO("current query")), \
             mock.patch.object(T, "trigger") as trigger:
            self.assertEqual(T.main([
                "--harness", "codex", "--sid", "sid", "--phase", "initial",
                "--anchor-stdin",
            ]), 0)
        trigger.assert_called_once_with(
            "codex", "sid", "initial", None,
            wait_seconds=0.0, after_mtime_ns=None, anchor_text="current query",
        )

    def test_codex_userprompt_hook_forwards_exact_current_prompt(self):
        payload = {"session_id": "sid", "cwd": "/repo", "prompt": "current query"}
        projection = types.SimpleNamespace(project=lambda *_args, **_kwargs: None)
        with mock.patch.object(HOOK, "load_payload", return_value=payload), \
             mock.patch.object(HOOK, "is_worker_session", return_value=False), \
             mock.patch.object(HOOK, "launch_trigger") as launch, \
             mock.patch.object(HOOK, "sd111_first_prompt_sweep"), \
             mock.patch.object(HOOK, "candidate_context", return_value=""), \
             mock.patch.object(HOOK, "run_preflight", return_value=""), \
             mock.patch.object(HOOK, "token_budget_context", return_value=""), \
             mock.patch.object(HOOK, "emit_context"), \
             mock.patch.dict(sys.modules, {"herdr_session_projection": projection}), \
             mock.patch("fleet.interaction.clear_wait"):
            self.assertEqual(HOOK.main(), 0)
        launch.assert_called_once_with(
            "codex", "sid", "initial", anchor_text="current query"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
