#!/usr/bin/env python3
"""A defunct harness process is not a session.

The shared Codex app-server daemon never reaps the app-server children it replaced, so
`ps` keeps listing `[codex] <defunct>` rows whose cwd and environ are gone. The session
loop turned each into a `(unknown)/` row with status `?`. State `Z`/`X` is terminal; an
unreadable stat is no evidence either way and keeps the process.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet.collectors import procscan  # noqa: E402


def _state(pid):
    try:
        with open("/proc/%d/stat" % pid) as handle:
            raw = handle.read()
        return raw[raw.rindex(")") + 1:].split()[0]
    except (OSError, ValueError, IndexError):
        return None


@unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
class ZombieHarnessProcessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # comm is taken from the executed file name: a link named `codex` to the interpreter.
        self.codex = Path(self._tmp.name) / "codex"
        self.codex.symlink_to(sys.executable)

    def _spawn(self, code):
        proc = subprocess.Popen([str(self.codex), "-c", code])
        self.addCleanup(proc.wait)
        return proc

    def _zombie(self):
        proc = self._spawn("pass")
        deadline = time.time() + 10
        while _state(proc.pid) != "Z" and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(_state(proc.pid), "Z", "fixture did not become a zombie")
        return proc

    def _scan(self, *pids):
        lines = ["%d codex 00:05 codex" % pid for pid in pids]
        with mock.patch.object(procscan, "_ps_lines", return_value=lines), \
             mock.patch.object(procscan, "_pid_ttys", return_value={}), \
             mock.patch.object(procscan, "_detached_ttys", return_value=set()), \
             mock.patch.object(procscan, "proc_tree", return_value={}), \
             mock.patch.object(procscan, "_orca_dead_socks", return_value=set()):
            return procscan.scan({"codex"})

    def test_a_defunct_codex_process_is_not_a_session(self):
        zombie = self._zombie()
        live = self._spawn("import time; time.sleep(60)")
        self.addCleanup(live.kill)
        self.assertEqual(_state(live.pid) in ("R", "S"), True)
        sessions = self._scan(zombie.pid, live.pid)
        self.assertEqual([s.pid for s in sessions], [live.pid])

    def test_an_unreadable_stat_keeps_the_process(self):
        # No /proc entry at all is "no evidence": never hide a process on missing evidence.
        gone = self._spawn("pass")
        gone.wait()
        sessions = self._scan(gone.pid)
        self.assertEqual([s.pid for s in sessions], [gone.pid])

    def test_the_state_helper_reads_terminal_states_only(self):
        zombie = self._zombie()
        self.assertTrue(procscan.is_terminal_state(zombie.pid))
        self.assertFalse(procscan.is_terminal_state(os.getpid()))
        self.assertFalse(procscan.is_terminal_state(2 ** 22 + 12345))


if __name__ == "__main__":
    unittest.main()
