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

    def start(self, source="startup"):
        payload = {"session_id": "synthetic-start", "cwd": str(self.cwd), "source": source}
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

    def test_unset_flag_injects_shared_summary_for_native_start_sources(self):
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                out = json.loads(self.start(source))["hookSpecificOutput"]
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

    def test_absent_store_emits_no_context(self):
        with patch.dict(os.environ, MEM_STORE=str(Path(self.tmp.name) / "absent-store")):
            self.assertEqual(self.start(), "")


if __name__ == "__main__":
    unittest.main()
