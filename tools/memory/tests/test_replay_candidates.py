#!/usr/bin/env python3
"""The candidate replay tool: three variants of one prompt sequence, no prompt text left behind."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from helpers import MEMORY_DIR, git


MEM = MEMORY_DIR / "mem.py"
TOOL = MEMORY_DIR / "replay-candidates.py"
SECRET = "SECRETPROMPTMARKER9271"


class ReplayCandidatesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="replay-", dir="/var/tmp")
        cls.root = Path(cls.tempdir.name)
        cls.project = cls.root / "project"
        cls.project.mkdir()
        git(cls.project, "init")
        git(cls.project, "remote", "add", "origin", "https://example.invalid/team/project.git")
        (cls.root / "home").mkdir()
        config = cls.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')
        cls.store = cls.root / "source-store"
        cls.tooltmp = cls.root / "tooltmp"
        cls.tooltmp.mkdir()
        for index in range(8):
            result = subprocess.run(
                [sys.executable, str(MEM), "add", "durable", "lesson",
                 f"gadget note {index} explains the widget cache layout",
                 "--headline", f"gadget headline {index} widget cache"],
                cwd=cls.project, env=cls.environment(), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            assert result.returncode == 0, result.stderr
        cls.prompts = cls.root / "prompts.txt"
        cls.prompts.write_text("\n".join([
            f"{SECRET} gadget widget cache",
            f"{SECRET} gadget headline",
            f"{SECRET} widget cache layout gadget",
            f"{SECRET} gadget again",
            "gadget widget cache once more",
        ]) + "\n")

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    @classmethod
    def environment(cls):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({
            "HOME": str(cls.root / "home"),
            "XDG_CONFIG_HOME": str(cls.root / "config"),
            "XDG_STATE_HOME": str(cls.root / "state"),
            "MEM_STORE": str(cls.store),
            "MEM_EXCHANGE_AUTO": "0",
            "TMPDIR": str(cls.tooltmp),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return env

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOL), "--db", str(self.store), "--cwd", str(self.project),
             *args], env=self.environment(), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

    def test_the_three_variants_and_the_verdict_lines(self):
        out = self.root / "report.json"
        before = hashlib.sha256((self.store / "memory.db").read_bytes()).hexdigest()
        result = self.run_tool("--prompts", str(self.prompts), "--json-out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_text())
        a, b, c = (report["variants"][key] for key in "abc")
        self.assertEqual(report["prompts"], 5)
        self.assertEqual(a["calls"], 5)
        self.assertGreater(a["total_bytes"], b["total_bytes"], "dedup must remove repeats")
        self.assertLess(b["distinct_ids"] + 1, a["ids_shown"])
        self.assertEqual(a["distinct_ids"], b["distinct_ids"])
        self.assertGreaterEqual(a["most_repeated_count"], 5)
        self.assertEqual(b["most_repeated_count"], 1)
        self.assertTrue(report["verdicts"]["b_lt_a"])
        self.assertEqual(report["verdicts"]["c_lt_a"], c["total_bytes"] < a["total_bytes"])
        for line in ("(b) < (a): PASS", "(c) <= (b):", "(c) < (a):"):
            self.assertIn(line, result.stdout)
        self.assertEqual(
            before, hashlib.sha256((self.store / "memory.db").read_bytes()).hexdigest(),
            "the source database is never written")

    def test_no_prompt_text_reaches_stdout_the_report_or_leftover_files(self):
        out = self.root / "report2.json"
        shown = self.run_tool("--prompts", str(self.prompts), "--json-out", str(out))
        as_json = self.run_tool("--prompts", str(self.prompts), "--json")
        for text in (shown.stdout, shown.stderr, as_json.stdout, as_json.stderr,
                     out.read_text()):
            self.assertNotIn(SECRET, text)
        self.assertIn(hashlib.sha256(
            f"{SECRET} gadget widget cache".encode()).hexdigest(), out.read_text())
        self.assertEqual(list(self.tooltmp.iterdir()), [], "the temporary store is removed")
        leftovers = [p for p in self.root.rglob("*")
                     if p.is_file() and SECRET.encode() in p.read_bytes()
                     and p.name not in ("prompts.txt", "session.jsonl")]
        self.assertEqual(leftovers, [])

    def test_a_claude_transcript_supplies_the_user_prompts_in_order(self):
        rows = [
            {"type": "user", "cwd": str(self.project),
             "message": {"role": "user", "content": f"{SECRET} gadget cache"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "gadget"}]}},
            {"type": "user", "isMeta": True,
             "message": {"role": "user", "content": "gadget meta"}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "gadget widget again"}]}},
            "not json",
        ]
        path = self.root / "session.jsonl"
        path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows))
        result = self.run_tool("--transcript", str(path), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["prompts"], 2)
        self.assertEqual(report["prompt_sha256"][0],
                         hashlib.sha256(f"{SECRET} gadget cache".encode()).hexdigest())
        self.assertNotIn(SECRET, result.stdout)

    def test_no_prompts_is_an_input_error_not_a_crash(self):
        empty = self.root / "empty.txt"
        empty.write_text("\n\n")
        result = self.run_tool("--prompts", str(empty))
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
