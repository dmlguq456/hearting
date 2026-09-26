#!/usr/bin/env python3
"""tools/fixture_processes.py — a detached writer is dead before its temp dir goes."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_processes  # noqa: E402

# Detached, marker only in the ENVIRONMENT (like a `__watch-run` watcher), and it keeps
# writing into the fixture directory until killed.
_WRITER = (
    "import os,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "open(os.path.join(d,'ready'),'w').close()\n"
    "i=0\n"
    "while True:\n"
    "    i+=1; open(os.path.join(d,'w%d'%i),'w').close(); time.sleep(0.005)\n"
)


class ReapTest(unittest.TestCase):
    def _spawn(self, root, *, detached=True):
        env = dict(os.environ, FIXTURE_DIR=str(root))
        proc = subprocess.Popen([sys.executable, "-c", _WRITER], env=env,
                                start_new_session=detached, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (root / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return proc

    def test_an_environ_only_writer_is_killed_and_the_dir_removes_cleanly(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        proc = self._spawn(root)
        self.assertNotIn(str(root).encode(), Path(f"/proc/{proc.pid}/cmdline").read_bytes())
        killed = fixture_processes.reap(str(root))
        self.assertIn(proc.pid, killed)
        proc.wait(timeout=5)
        tmp.cleanup()  # raises OSError(ENOTEMPTY) if a writer were still alive
        self.assertFalse(root.exists())

    def test_a_marked_child_in_the_callers_group_is_killed_without_killing_the_caller(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            proc = self._spawn(root, detached=False)
            self.assertEqual(os.getpgid(proc.pid), os.getpgrp())
            self.assertEqual(fixture_processes.reap(str(root)), [proc.pid])
            self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL)

    def test_nothing_marked_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(fixture_processes.reap(name), [])


if __name__ == "__main__":
    unittest.main()
