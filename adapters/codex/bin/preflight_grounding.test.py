#!/usr/bin/env python3
"""Codex capability router -> Fleet inline grounding bridge (F-43)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[3]
PREFLIGHT = ROOT / "adapters" / "codex" / "bin" / "preflight.sh"


class PreflightGroundingTest(unittest.TestCase):
    def run_route(self, worker=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        agent_home = base / "agent-home"
        (agent_home / "core").mkdir(parents=True)
        (agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        cwd = base / "repo"
        cwd.mkdir()
        env = {**os.environ, "AGENT_HOME": str(agent_home), "HOME": str(base)}
        env.pop("XDG_STATE_HOME", None)
        env.pop("FLEET_CAPABILITY_GROUNDING_DIR", None)
        if worker:
            env["AGENT_SESSION_ROLE"] = "worker"
        result = subprocess.run(
            [str(PREFLIGHT), "route", "autopilot-code", str(cwd), "sid-f43",
             "debug", "direct"],
            text=True, capture_output=True, env=env, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((agent_home / ".capability-grounding").exists())
        return base / ".local" / "state" / "agent-fleet" / "capability-grounding" / "sid-f43"

    def test_main_route_records_exact_capability_mode_and_intensity(self):
        marker = self.run_route()
        self.assertEqual(
            marker.read_text(encoding="utf-8").splitlines(),
            ["capability=autopilot-code", "mode=debug", "intensity=direct",
             "cwd=" + str(marker.parents[4] / "repo")],
        )

    def test_worker_route_does_not_create_inline_main_marker(self):
        self.assertFalse(self.run_route(worker=True).exists())


if __name__ == "__main__":
    unittest.main()
