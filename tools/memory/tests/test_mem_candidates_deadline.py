#!/usr/bin/env python3
"""The per-prompt candidate probe ends its git and FTS reads at one deadline.

PEER-HOOK measured the probe at 72 s with the DB pages evicted and a slow NAS
cwd; Claude cancels the prompt hook at 10 s. Past the deadline the probe shows
nothing this turn and counts nothing as seen. Fake clocks only: nothing waits.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from helpers import MEMORY_DIR, git


MEM = MEMORY_DIR / "mem.py"


class CandidateProbeDeadlineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="probe-deadline-")
        cls.root = Path(cls.tempdir.name)
        cls.project = cls.root / "project"
        cls.project.mkdir()
        git(cls.project, "init")
        git(cls.project, "remote", "add", "origin", "https://example.invalid/team/project.git")
        (cls.root / "home").mkdir()
        config = cls.root / "config" / "hearting"
        config.mkdir(parents=True)
        (config / "memory-sync.json").write_text('{"enabled": false}\n')
        cls.env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("MEM_", "AGENT_", "CODEX_", "OPENCODE_", "FLEET_"))}
        cls.env.update({
            "HOME": str(cls.root / "home"),
            "XDG_CONFIG_HOME": str(cls.root / "config"),
            "XDG_STATE_HOME": str(cls.root / "state"),
            "MEM_STORE": str(cls.root / "store"),
            "MEM_PROJECTS": str(cls.root / "projects"),
            "MEM_EXCHANGE_AUTO": "0",
            "MEM_RECALL_EVENTS": str(cls.root / "events.jsonl"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for index in range(3):
            # Global scope: a wrong project key would still match these.
            added = subprocess.run(
                [sys.executable, str(MEM), "add", "durable", "lesson",
                 f"gadget note {index} explains the widget cache",
                 "--headline", f"gadget headline {index}", "--scope", "global"],
                cwd=cls.project, env=cls.env, text=True, capture_output=True, timeout=60)
            assert added.returncode == 0, added.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location(f"mem_probe_{self.id()}", MEM)
        self.mem = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mem)
        cwd = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, cwd)

    def seen_files(self):
        """Each saved display history with its bytes: a probe that showed nothing adds none."""
        seen = self.mem.CANDIDATE_SEEN
        return {path.name: path.read_bytes() for path in seen.glob("*.json")} if seen.exists() else {}

    def probe(self, *extra):
        output = io.StringIO()
        argv = ["mem", "candidates", "gadget widget",
                "--session-id", "probe-" + self.id().rsplit(".", 1)[-1],
                "--runtime", "test", "--hook", *extra]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.mem._cli_main()
        return output.getvalue()

    def test_a_normal_probe_still_shows_candidates(self):
        before = self.seen_files()
        self.assertIn("gadget headline", self.probe())
        self.assertNotEqual(self.seen_files(), before)

    def test_git_that_does_not_answer_by_the_deadline_shows_nothing(self):
        timeouts = []
        real_run = subprocess.run

        def slow_git(command, *args, **kwargs):
            if command and command[0] == "git":
                timeouts.append(kwargs.get("timeout"))
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return real_run(command, *args, **kwargs)

        # 4.8 s of the 5 s budget already went to imports under load.
        started = time.monotonic() - 4.8
        before = self.seen_files()
        with mock.patch.object(self.mem, "_PROCESS_STARTED", started, create=True), \
             mock.patch.object(self.mem.subprocess, "run", side_effect=slow_git):
            self.assertEqual(self.probe(), "")
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < value <= 0.25 for value in timeouts), timeouts)
        self.assertEqual(self.seen_files(), before)

    def test_no_git_runs_once_the_budget_is_spent(self):
        before = self.seen_files()
        with mock.patch.object(self.mem, "_PROCESS_STARTED", time.monotonic() - 6, create=True), \
             mock.patch.object(self.mem.subprocess, "run") as run:
            self.assertEqual(self.probe(), "")
        self.assertFalse([call for call in run.call_args_list if call.args[0][0] == "git"])
        self.assertEqual(self.seen_files(), before)

    def test_an_fts_query_still_running_at_the_deadline_is_interrupted(self):
        project = self.mem.project_key(Path.cwd())
        before = self.seen_files()
        ticks = iter([0.0])  # the check before connecting; every later read is past it
        output = io.StringIO()
        with mock.patch.object(self.mem, "_probe_clock", lambda: next(ticks, 100.0)), \
             mock.patch.object(self.mem, "_PROBE_PROGRESS_STEPS", 1), \
             contextlib.redirect_stdout(output):
            shown = self.mem.candidates("gadget widget", session_id="fts-session",
                                        runtime="test", hook=True, deadline=1.0,
                                        project=project)
        self.assertEqual((shown, output.getvalue()), ([], ""))
        self.assertEqual(self.seen_files(), before)


if __name__ == "__main__":
    unittest.main()
