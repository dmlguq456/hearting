"""fleet.sh --window opens through utilities/pane.py and otherwise runs in place.

The interpreter is a recording stand-in: it answers for pane.py from the test's
JSON and exit status, logs a direct fleet.py run, and executes `-c` snippets for
real, so no terminal is touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools/fleet/fleet.sh"
_SURFACE_ENV = ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "TMUX", "TMUX_PANE")

_PYTHON = """#!{python}
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["-c"]:
    exec(compile(args[1], "<fleet-snippet>", "exec"))
    raise SystemExit(0)
with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args and args[0].endswith("utilities/pane.py"):
    print(os.environ["PANE_JSON"])
    raise SystemExit(int(os.environ.get("PANE_EXIT", "0")))
"""


class FleetWindowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.log = self.root / "calls.jsonl"
        self.python = self.root / "python"
        self.python.write_text(_PYTHON.format(python=sys.executable), encoding="utf-8")
        self.python.chmod(0o700)

    def run_case(self, payload, *args, code=0):
        env = {key: value for key, value in os.environ.items() if key not in _SURFACE_ENV}
        env.update(FLEET_PYTHON=str(self.python), CALL_LOG=str(self.log), PANE_JSON=json.dumps(payload),
                   PANE_EXIT=str(code), PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def kinds(self):
        return ["pane" if call[0].endswith("utilities/pane.py") else "direct" for call in self.calls()]

    def test_opened_pane_is_the_whole_run_and_argv_is_passed_through(self):
        payload = {"surface": "tmux", "pane": "%5", "placement": "split", "notes": [], "view_command": "x"}
        result = self.run_case(payload, "--window", "--filter", "two words", "it's")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kinds(), ["pane"])
        call = self.calls()[0]
        self.assertEqual(call[1:5], ["open", "--title", "fleet", "--"])
        self.assertEqual(call[5:7], [str(self.python), str(ROOT / "tools/fleet/fleet.py")])
        self.assertEqual(call[7:], ["--filter", "two words", "it's"])
        self.assertIn("opened in tmux pane %5", result.stderr)

    def test_no_surface_or_failed_open_runs_in_the_current_terminal(self):
        for payload, code in (({"surface": "none", "pane": None, "notes": ["no-surface"]}, 0),
                              ({"surface": "herdr", "pane": None, "notes": ["pane-split-failed"]}, 1)):
            with self.subTest(payload=payload):
                self.log.unlink(missing_ok=True)
                result = self.run_case(payload, "--window", code=code)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.kinds(), ["pane", "direct"])
                self.assertEqual(self.calls()[1], [str(ROOT / "tools/fleet/fleet.py")])

    def test_a_pane_that_exited_right_away_is_not_started_twice(self):
        for notes in (["pane-exited"], ["pane-list-unconfirmed"]):
            with self.subTest(notes=notes):
                self.log.unlink(missing_ok=True)
                payload = {"surface": "herdr", "pane": "w1:p3", "placement": "split", "notes": notes}
                result = self.run_case(payload, "--window")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.kinds(), ["pane"])

    def test_snapshot_modes_never_open_a_pane(self):
        for flag in ("--once", "--json"):
            with self.subTest(flag=flag):
                self.log.unlink(missing_ok=True)
                result = self.run_case({}, "--window", flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [[str(ROOT / "tools/fleet/fleet.py"), flag]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
