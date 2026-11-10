#!/usr/bin/env python3
"""Codex main SessionStart uses the shared summary; opt-outs and workers stay silent."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/install"))
import fixture_env

spec = importlib.util.spec_from_file_location("codex_start_memory", ROOT / "adapters/codex/hooks/sessionstart-lifecycle.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
import codex_start_summary  # the bridge put its hooks directory on sys.path


class SessionStartMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-start-memory-")
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name) / "project"
        self.cwd.mkdir()
        env = fixture_env.build_environment(self.tmp.name, ROOT, base={"PATH": os.environ["PATH"]})
        env["MEM_EXCHANGE_AUTO"] = "0"
        fixture_env.prepare_environment(env)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for tier in ("working", "durable"):
            subprocess.run([sys.executable, str(ROOT / "tools/memory/mem.py"), "add", tier, "thread",
                            "SYNTHETIC_START_MEMORY 사용자 선택과 요약 상한을 보존한다"],
                           env=env, cwd=self.cwd, check=True, capture_output=True, text=True)
        self.expected = bridge.run_preflight("memory", str(self.cwd)).strip()
        self.assertIn("SYNTHETIC_START_MEMORY", self.expected)

    def start(self, source="startup", sid="synthetic-start"):
        payload = {"session_id": sid, "cwd": str(self.cwd), "source": source}
        output = io.StringIO()
        # Only the unrelated live pane/recovery collaborators are stubbed. The
        # installed preflight and memory summary generator run for real.
        with patch.dict(sys.modules, {
                "herdr_session_projection": SimpleNamespace(project=lambda *a, **kw: None),
                "session_tidy": SimpleNamespace(run_hook=lambda *a, **kw: None)}), \
             patch.object(bridge, "request_lifecycle_recovery"), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(output):
            self.assertEqual(bridge.main(), 0)
        return output.getvalue()

    def context(self, source, sid="synthetic-start"):
        raw = self.start(source, sid)
        return json.loads(raw)["hookSpecificOutput"]["additionalContext"] if raw else ""

    def prompt(self, sid="synthetic-start"):
        """Run the real Codex prompt bridge as the native UserPromptSubmit would."""
        payload = {"session_id": sid, "cwd": str(self.cwd), "prompt": "hello", "hook_event_name": "UserPromptSubmit"}
        subprocess.run([sys.executable, str(ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")],
                       input=json.dumps(payload), env=dict(os.environ), cwd=self.cwd,
                       capture_output=True, text=True, timeout=60, check=False)

    def test_unset_flag_injects_shared_summary_for_native_start_sources(self):
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                out = json.loads(self.start(source, sid=f"synthetic-{source}"))["hookSpecificOutput"]
                self.assertEqual(out["hookEventName"], "SessionStart")
                self.assertEqual(out["additionalContext"], self.expected)
                self.assertLessEqual(len(out["additionalContext"]), 2000)

    def test_explicit_enable_keeps_existing_flag_meanings(self):
        for flag in ("1", "true", "yes", "on", "TRUE"):
            with self.subTest(flag=flag), patch.dict(os.environ, CODEX_SESSION_MEMORY_INJECT=flag):
                self.assertEqual(json.loads(self.start())["hookSpecificOutput"]["additionalContext"], self.expected)

    def test_every_explicit_non_truthy_flag_keeps_injection_disabled(self):
        for flag in ("0", "false", "no", "off", "", "unknown"):
            with self.subTest(flag=flag), patch.dict(os.environ, CODEX_SESSION_MEMORY_INJECT=flag):
                self.assertEqual(self.start(), "")

    def test_workers_skip_memory_even_with_explicit_enable(self):
        for key, value in (("AGENT_SESSION_ROLE", "worker"), ("AGENT_DISPATCH_CHILD", "1"),
                           ("AGENT_DISPATCH_DEPTH", "1"), ("OPENCODE_DISPATCH_SLUG", "synthetic"),
                           ("FLEET_TITLE_REFRESH", "1"), ("MEM_DISTILL", "1")):
            with self.subTest(marker=key), patch.dict(os.environ, {key: value, "CODEX_SESSION_MEMORY_INJECT": "1"}), \
                 patch.object(bridge, "run_preflight", side_effect=AssertionError("worker read memory")):
                self.assertEqual(self.start(), "")

    def test_shared_cap_override_is_preserved(self):
        with patch.dict(os.environ, MEM_INJECT_MAX_CHARS="400"):
            actual = json.loads(self.start())["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(actual, bridge.run_preflight("memory", str(self.cwd)).strip())
            self.assertLessEqual(len(actual), 400)

    def test_resume_then_compact_before_first_request_carries_one_summary(self):
        # Codex 0.163 drains queued start sources back to back before the
        # prompt hook and keeps both contexts in that request
        # (core/tests/suite/hooks.rs resumed_thread_runs_resume_then_compact_session_start_hooks).
        for first in ("resume", "startup", "clear"):
            with self.subTest(first=first):
                sid = f"synthetic-pair-{first}"
                request = [self.context(first, sid), self.context("compact", sid)]
                self.assertEqual(sum(part.count(self.expected) for part in request), 1)
                self.assertEqual(request[0], self.expected)

    def test_compact_after_a_prompt_restores_the_summary(self):
        sid = "synthetic-later-compact"
        self.assertEqual(self.context("resume", sid), self.expected)
        self.assertEqual(self.context("compact", sid), "")
        self.prompt(sid)
        self.assertEqual(self.context("compact", sid), self.expected)
        # A second compact in the same turn follows a compact summary, which
        # that compaction has replaced: it needs the summary again.
        self.assertEqual(self.context("compact", sid), self.expected)

    def test_stale_or_unemitted_start_does_not_suppress_compact(self):
        sid = "synthetic-stale"
        self.assertEqual(self.context("resume", sid), self.expected)
        mark = codex_start_summary._mark_path(sid, os.environ)
        data = json.loads(mark.read_text())
        data["at"] -= codex_start_summary.PAIR_WINDOW_SECONDS + 1
        mark.write_text(json.dumps(data))
        self.assertEqual(self.context("compact", sid), self.expected)
        sid = "synthetic-unemitted"
        with patch.dict(os.environ, CODEX_SESSION_MEMORY_INJECT="0"):
            self.assertEqual(self.start("resume", sid), "")
        self.assertEqual(self.context("compact", sid), self.expected)

    def test_failed_output_leaves_no_mark_and_the_compact_emits(self):
        # Review R2 order A: the resume hook's JSON never reached the native
        # side, so the compact start is the only chance for this request.
        class BrokenOutput:
            def write(self, data):
                raise BrokenPipeError("synthetic closed hook output")

            def flush(self):
                pass

        sid = "synthetic-broken-output"
        payload = {"session_id": sid, "cwd": str(self.cwd), "source": "resume"}
        with patch.dict(sys.modules, {
                "herdr_session_projection": SimpleNamespace(project=lambda *a, **kw: None),
                "session_tidy": SimpleNamespace(run_hook=lambda *a, **kw: None)}), \
             patch.object(bridge, "request_lifecycle_recovery"), \
             patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(BrokenOutput()):
            with self.assertRaises(BrokenPipeError):
                bridge.main()
        self.assertFalse(codex_start_summary._mark_path(sid, os.environ).exists())
        self.assertEqual(self.context("compact", sid), self.expected)

    def test_prompt_that_cannot_remove_the_mark_still_restores_the_compact_summary(self):
        # Review R2 order B: a mark the prompt could not delete must not pass
        # for a summary still waiting for its first request.
        sid = "synthetic-undeletable-mark"
        self.assertEqual(self.context("resume", sid), self.expected)
        with patch.object(codex_start_summary.Path, "unlink", side_effect=OSError("synthetic refusal")):
            codex_start_summary.note_prompt(sid)
        self.assertEqual(self.context("compact", sid), self.expected)

    def test_mark_write_failure_emits_again_and_leaves_no_temporary_file(self):
        sid = "synthetic-replace-failure"
        with patch.object(codex_start_summary.os, "replace", side_effect=OSError("synthetic refusal")):
            self.assertEqual(self.context("resume", sid), self.expected)
        mark = codex_start_summary._mark_path(sid, os.environ)
        self.assertEqual([p.name for p in mark.parent.iterdir() if p.name.startswith(".")], [])
        self.assertEqual(self.context("compact", sid), self.expected)

    def test_old_marks_of_other_sessions_are_pruned_on_write(self):
        old = codex_start_summary._mark_path("synthetic-abandoned", os.environ)
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text(json.dumps({"source": "resume", "at": 0}))
        leftover = old.with_name(f".{old.name}.1")
        leftover.write_text("{}")
        for path in (old, leftover):
            os.utime(path, (0, 0))
        self.assertEqual(self.context("resume", "synthetic-fresh"), self.expected)
        self.assertFalse(old.exists())
        self.assertFalse(leftover.exists())
        self.assertTrue(codex_start_summary._mark_path("synthetic-fresh", os.environ).exists())

    def policy(self, value):
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "hearting" / "memory-sync.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"codex_session_memory_inject": value}) if value is not None else "{broken")
        self.addCleanup(path.unlink, missing_ok=True)

    def test_shared_policy_file_switch_reaches_a_daemon_environment_without_the_flag(self):
        # A shared app-server daemon hook inherits the daemon's environment, not
        # the client's; the memory policy file is read at every start instead.
        for value in (False, "0", "off"):
            with self.subTest(value=value):
                self.policy(value)
                self.assertEqual(self.start(sid=f"synthetic-policy-{value}"), "")
        self.policy(True)
        self.assertEqual(self.context("startup", "synthetic-policy-on"), self.expected)
        self.policy(None)  # unreadable policy keeps the default
        self.assertEqual(self.context("startup", "synthetic-policy-broken"), self.expected)

    def test_process_flag_still_wins_over_the_policy_file(self):
        self.policy(False)
        with patch.dict(os.environ, CODEX_SESSION_MEMORY_INJECT="1"):
            self.assertEqual(self.context("startup", "synthetic-flag-on"), self.expected)
        self.policy(True)
        with patch.dict(os.environ, CODEX_SESSION_MEMORY_INJECT="0"):
            self.assertEqual(self.start(sid="synthetic-flag-off"), "")

    def test_workers_skip_memory_even_with_policy_enable(self):
        self.policy(True)
        with patch.dict(os.environ, AGENT_DISPATCH_CHILD="1"), \
             patch.object(bridge, "run_preflight", side_effect=AssertionError("worker read memory")):
            self.assertEqual(self.start(sid="synthetic-worker-policy"), "")

    def test_absent_store_emits_no_context(self):
        with patch.dict(os.environ, MEM_STORE=str(Path(self.tmp.name) / "absent-store")):
            self.assertEqual(self.start(), "")


if __name__ == "__main__":
    unittest.main()
