#!/usr/bin/env python3
"""A steward surface must address the herdr session that holds the pane.

herdr runs one server per session, each with its own socket and its own pane
namespace, so a bare `herdr ...` call only ever reaches the default server.
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STEWARD = ROOT / "utilities/peer-steward.py"

FAKE_HERDR = """#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["HERDR_ARGV_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
# Only the named session knows the pane.
if argv[:2] == ["--session", "fourth"] and argv[2:4] == ["agent", "get"]:
    print(json.dumps({"result": {"agent": {
        "agent": "codex",
        "agent_session": {"value": "session-in-fourth"}}}}))
    sys.exit(0)
print(json.dumps({"error": {"code": "agent_not_found"}}))
sys.exit(1)
"""


class HerdrSessionTargeting(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="peer-steward-session-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        bindir = base / "bin"
        bindir.mkdir()
        fake = bindir / "herdr"
        fake.write_text(FAKE_HERDR)
        fake.chmod(0o755)
        self.log = base / "argv.log"
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ["PATH"]
        os.environ["HERDR_ARGV_LOG"] = str(self.log)
        self.addCleanup(os.environ.pop, "HERDR_ARGV_LOG", None)

    def load(self, session=None):
        if session is None:
            os.environ.pop("AGENT_HERDR_SESSION", None)
        else:
            os.environ["AGENT_HERDR_SESSION"] = session
        spec = importlib.util.spec_from_file_location("peer_steward_under_test", str(STEWARD))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_default_session_cannot_resolve_a_pane_of_another_session(self):
        module = self.load()
        self.assertEqual(module._resolve_target("w1:p3"), (None, None, None))
        self.assertNotIn("--session", self.log.read_text())

    def test_selected_session_resolves_and_is_passed_to_every_call(self):
        module = self.load("fourth")
        harness, sid, _ = module._resolve_target("w1:p3")
        self.assertEqual((harness, sid), ("codex", "session-in-fourth"))
        for line in self.log.read_text().splitlines():
            self.assertTrue(line.startswith('["--session", "fourth"'), line)

    def test_flag_sets_the_environment_a_detached_watcher_inherits(self):
        os.environ.pop("AGENT_HERDR_SESSION", None)
        result = subprocess.run(
            [sys.executable, str(STEWARD), "--herdr-session", "fourth", "status", "--json"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.environ.pop, "AGENT_HERDR_SESSION", None)


if __name__ == "__main__":
    unittest.main()
