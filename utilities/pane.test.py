#!/usr/bin/env python3
"""Tests for `hearting run pane open` (utilities/pane.py).

Every child gets a minimal environment and a PATH whose `herdr` and `tmux` only
record their argv, so even a realistic inherited pane, socket and `$TMUX` can
reach nothing but those recorders.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_SURFACE_ENV = ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
                "TMUX", "TMUX_PANE", "AGENT_HERDR_SESSION")
_MUTATIONS = (("pane", "split"), ("tab", "create"), ("pane", "run"), ("pane", "send-text"),
              ("pane", "rename"), ("pane", "report-metadata"), ("split-window",), ("new-window",),
              ("send-keys",), ("select-pane",), ("set-option",))

_RECORDER = """#!{python}
import json, os, sys
with open(os.environ["RECORD"], "a", encoding="utf-8") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
args = [a for a in sys.argv[1:] if not a.startswith("-S")]
if os.path.basename(sys.argv[0]) == "tmux" and "display-message" in args:
    print("%0\\t$0")
    raise SystemExit(int(os.environ.get("TMUX_ANSWERS", "1") != "1"))
if "process-info" in args:
    print(json.dumps({{"result": {{"process_info": {{"pane_id": "w1:p0", "shell_pid": 1,
        "foreground_process_group_id": 1, "foreground_processes": [{{"pid": 1}}]}}}}}}))
    raise SystemExit(0)
raise SystemExit(1)
"""


class PaneCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in _SURFACE_ENV:
            os.environ.pop(key, None)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("herdr", "tmux"):
            path = self.bin / name
            path.write_text(_RECORDER.format(python=sys.executable), encoding="utf-8")
            path.chmod(0o700)
        self.record = self.root / "record.jsonl"

    def env(self, **extra):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root / "home"),
               "LANG": "C.UTF-8", "RECORD": str(self.record),
               "AGENT_PEER_LEDGER_ROOT": str(self.root / "ledger"), "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def open(self, *command, entry=None, **extra):
        argv = entry or [sys.executable, str(_HERE / "pane.py"), "open"]
        return subprocess.run([*argv, "--", *command], cwd=self.root, env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]

    def mutations(self):
        found = []
        for call in self.calls():
            words = [word for word in call[1:] if not word.startswith("-")]
            if any(tuple(words[:len(verb)]) == verb for verb in _MUTATIONS):
                found.append(call)
        return found

    def test_no_surface_prints_one_line_with_the_command_and_exits_zero(self):
        result = self.open("printf", "%s\n", "two words")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        value = json.loads(result.stdout)
        self.assertEqual((value["surface"], value["pane"], value["placement"], value["notes"]),
                         ("none", None, "none", ["no-surface"]))
        self.assertEqual(value["view_command"], f"cd {self.root} && printf '%s\n' 'two words'")
        self.assertEqual(self.calls(), [])

    def test_hearting_run_reaches_the_same_cli(self):
        entry = ["sh", str(_ROOT / "tools/install/harness.sh"), "run", "pane", "open"]
        result = self.open("echo", "hi", entry=entry, AGENT_HOME=str(_ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["surface"], "none")

    def test_a_command_is_required(self):
        result = subprocess.run([sys.executable, str(_HERE / "pane.py"), "open", "--"], env=self.env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)

    def test_registered_worker_with_inherited_panes_touches_no_terminal(self):
        realistic = {"HERDR_PANE_ID": "w1:p0", "HERDR_SOCKET_PATH": str(self.root / "herdr.sock"),
                     "TMUX": f"{self.root}/tmux.sock,4242,0", "TMUX_PANE": "%0", "TMUX_ANSWERS": "1"}
        for marker in ({"AGENT_SESSION_ROLE": "worker"}, {"AGENT_DISPATCH_CHILD": "1"},
                       {"AGENT_DISPATCH_DEPTH": "2"}):
            with self.subTest(marker=marker):
                result = self.open("echo", "hi", **realistic, **marker)
                self.assertEqual(json.loads(result.stdout)["surface"], "none", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_unproven_herdr_caller_opens_nothing(self):
        # A harness identity whose process is not the pane's foreground gets no pane.
        result = self.open("echo", "hi", HERDR_PANE_ID="w1:p0", CLAUDECODE="1",
                           CLAUDE_CODE_SESSION_ID="00000000-0000-4000-8000-000000000000")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["surface"], "none")
        self.assertEqual(self.mutations(), [])

    def test_tmux_that_does_not_answer_opens_nothing(self):
        result = self.open("echo", "hi", TMUX=f"{self.root}/tmux.sock,4242,0", TMUX_PANE="%0",
                           TMUX_ANSWERS="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["surface"], "none")
        self.assertTrue(self.calls())
        self.assertEqual(self.mutations(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
