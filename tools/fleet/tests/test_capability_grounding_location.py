"""F-43a marker relocation — `utilities/capability-grounding.sh` writes outside the release
tree (F-<next> fleet-route-chain-r2, plan §3 Phase D). Stdlib unittest + subprocess only."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_ROOT, "utilities", "capability-grounding.sh")


class CapabilityGroundingLocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cg-location-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, env, *extra_args):
        args = [_SCRIPT, "record", "--sid", "sid-f43", "--capability", "autopilot-code",
                "--mode", "dev", "--intensity", "standard", *extra_args]
        return subprocess.run(args, env=env, text=True, capture_output=True, timeout=20)

    def test_record_writes_state_dir_not_agent_home(self):
        release = os.path.join(self.tmp, "release")
        state = os.path.join(self.tmp, "state")
        os.makedirs(release)
        with open(os.path.join(release, "seed.txt"), "w") as fh:
            fh.write("seed")
        before = {p: os.stat(p).st_mtime for p in glob.glob(release + "/**", recursive=True)}
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "XDG_STATE_HOME": state}
        result = self._run(env, "--agent-home", release)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = {p: os.stat(p).st_mtime for p in glob.glob(release + "/**", recursive=True)}
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(os.path.join(release, ".capability-grounding")))
        marker = os.path.join(state, "agent-fleet", "capability-grounding", "sid-f43")
        self.assertTrue(os.path.isfile(marker))
        with open(marker) as fh:
            self.assertEqual(fh.read().splitlines(),
                              ["capability=autopilot-code", "mode=dev", "intensity=standard"])

    def test_env_override_dir(self):
        override = os.path.join(self.tmp, "custom-cg")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp,
               "FLEET_CAPABILITY_GROUNDING_DIR": override}
        result = self._run(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(os.path.join(override, "sid-f43")))

    def test_no_agent_home_still_records(self):
        state = os.path.join(self.tmp, "state")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "XDG_STATE_HOME": state}
        result = self._run(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(os.path.isfile(
            os.path.join(state, "agent-fleet", "capability-grounding", "sid-f43")))

    def test_non_entry_capability_is_skipped(self):
        state = os.path.join(self.tmp, "state")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "XDG_STATE_HOME": state}
        args = [_SCRIPT, "record", "--sid", "sid-skip", "--capability", "not-an-entry-capability"]
        result = subprocess.run(args, env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(
            os.path.join(state, "agent-fleet", "capability-grounding", "sid-skip")))


if __name__ == "__main__":
    unittest.main()
