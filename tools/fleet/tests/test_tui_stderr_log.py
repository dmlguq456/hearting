"""The live TUI keeps stray stderr (library diagnostics, child processes) off the
curses screen: it goes to a log file while the TUI runs, and fd 2 comes back after."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import subprocess
import sys
import tempfile
import unittest

_TOOLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render               # noqa: E402


class TuiStderrLogTest(unittest.TestCase):
    def test_python_and_child_stderr_go_to_the_log_and_fd2_is_restored(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "agent-fleet", "fleet-stderr.log")
            before = os.fstat(2)
            with render._StderrToLog(log):
                print("from-python", file=sys.stderr)
                sys.stderr.flush()
                subprocess.run([sys.executable, "-c",
                                "import sys; sys.stderr.write('from-child\\n')"], check=True)
            after = os.fstat(2)
            with open(log, encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn("from-python", text)
        self.assertIn("from-child", text)
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))

    def test_unwritable_log_leaves_stderr_alone(self):
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "file")
            open(blocker, "w").close()
            before = os.fstat(2)
            with render._StderrToLog(os.path.join(blocker, "sub", "fleet-stderr.log")):
                during = os.fstat(2)
        self.assertEqual((before.st_dev, before.st_ino), (during.st_dev, during.st_ino))

    def test_log_path_follows_xdg_state_home(self):
        old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = "/x/state"
        try:
            self.assertEqual(render._stderr_log_path(), "/x/state/agent-fleet/fleet-stderr.log")
        finally:
            if old is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old


if __name__ == "__main__":
    unittest.main()
