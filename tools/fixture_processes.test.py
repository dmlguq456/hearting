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
from unittest import mock

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

_DESCENDANT = (
    "import os,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "open(os.path.join(d,'descendant-ready'),'w').write(str(os.getpid()))\n"
    "i=0\n"
    "while True:\n"
    "    i+=1; open(os.path.join(d,'descendant-%d'%i),'w').close(); time.sleep(0.005)\n"
)

_PARENT = (
    "import os,subprocess,sys,time\n"
    "d=os.environ['FIXTURE_DIR']\n"
    "release=os.environ.get('FIXTURE_RELEASE_PATH')\n"
    "while release and not os.path.exists(release): time.sleep(0.005)\n"
    "p=subprocess.Popen([sys.executable,'-c',os.environ['FIXTURE_DESCENDANT']], "
    "env=os.environ.copy(),start_new_session=True,stdin=subprocess.DEVNULL)\n"
    "open(os.path.join(d,'descendant-pid'),'w').write(str(p.pid))\n"
    "deadline=time.monotonic()+10\n"
    "while not os.path.exists(os.path.join(d,'descendant-ready')) and time.monotonic()<deadline: "
    "time.sleep(0.005)\n"
    "if not os.path.exists(os.path.join(d,'descendant-ready')): raise SystemExit(4)\n"
)


class ReapDeadlineTest(unittest.TestCase):
    """A deadline is an error only while a pinned fixture process survives."""

    def setUp(self):
        patches = {
            "marked_processes": mock.patch.object(fixture_processes, "marked_processes"),
            "start": mock.patch.object(fixture_processes, "_start_ticks", return_value="101"),
            "state": mock.patch.object(fixture_processes, "_state", return_value="S"),
            "clock": mock.patch.object(fixture_processes.time, "monotonic", side_effect=[0, 10, 10]),
            "sleep": mock.patch.object(fixture_processes.time, "sleep"),
            "own_group": mock.patch.object(fixture_processes.os, "getpgrp", return_value=1),
            "group": mock.patch.object(fixture_processes.os, "getpgid", return_value=2),
            "kill": mock.patch.object(fixture_processes.os, "kill"),
            "killpg": mock.patch.object(fixture_processes.os, "killpg"),
        }
        for name, patch in patches.items():
            setattr(self, name, patch.start())
            self.addCleanup(patch.stop)

    def test_empty_initial_batch_at_deadline_is_a_no_op(self):
        self.marked_processes.return_value = {}
        self.assertEqual(fixture_processes.reap("private-marker"), [])
        self.kill.assert_not_called()
        self.killpg.assert_not_called()
        self.sleep.assert_not_called()

    def test_process_gone_at_deadline_is_not_a_survivor(self):
        self.marked_processes.side_effect = [{101: "101"}, {}]
        self.start.side_effect = ["101", None, None]
        self.assertEqual(fixture_processes.reap("private-marker"), [101])
        self.killpg.assert_called_once_with(2, signal.SIGKILL)
        self.sleep.assert_not_called()

    def test_live_writer_at_deadline_still_raises(self):
        self.marked_processes.return_value = {101: "101"}
        with self.assertRaisesRegex(TimeoutError, r"survived SIGKILL: \[101\]"):
            fixture_processes.reap("private-marker")
        self.killpg.assert_called_once_with(2, signal.SIGKILL)

    def test_pid_reused_before_signal_is_not_killed(self):
        self.marked_processes.side_effect = [{101: "old"}, {}]
        self.assertEqual(fixture_processes.reap("private-marker"), [])
        self.group.assert_not_called()
        self.kill.assert_not_called()
        self.killpg.assert_not_called()

    def test_pid_reused_after_signal_is_not_a_survivor(self):
        self.marked_processes.side_effect = [{101: "101"}, {}]
        self.start.side_effect = ["101", "new", "new"]
        self.assertEqual(fixture_processes.reap("private-marker"), [101])
        self.killpg.assert_called_once_with(2, signal.SIGKILL)

    def test_zombie_at_deadline_cannot_keep_writing(self):
        self.marked_processes.side_effect = [{101: "101"}, {}]
        self.state.return_value = "Z"
        self.assertEqual(fixture_processes.reap("private-marker"), [101])

    def test_callers_group_uses_only_the_pinned_process_signal(self):
        self.marked_processes.side_effect = [{101: "101"}, {}]
        self.group.return_value = 1
        self.start.side_effect = ["101", None, None]
        self.assertEqual(fixture_processes.reap("private-marker"), [101])
        self.kill.assert_called_once_with(101, signal.SIGKILL)
        self.killpg.assert_not_called()

    def test_second_scan_live_child_still_raises_after_first_process_exits(self):
        self.marked_processes.side_effect = [{101: "101"}, {102: "102"}]
        self.clock.side_effect = [0, 1, 10]
        # Pin the first snapshot before its process disappears.
        starts = iter(["101", None, "102", None, "102"])
        self.start.side_effect = lambda pid: next(starts)
        with self.assertRaisesRegex(TimeoutError, r"survived SIGKILL: \[102\]"):
            fixture_processes.reap("private-marker")
        self.assertEqual(self.marked_processes.call_count, 2)
        self.assertEqual(self.killpg.call_count, 2)


class ReapTest(unittest.TestCase):
    @staticmethod
    def _wait_for(path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return True
            time.sleep(0.01)
        return path.exists()

    def _spawn(self, root, *, detached=True):
        env = dict(os.environ, FIXTURE_DIR=str(root))
        proc = subprocess.Popen([sys.executable, "-c", _WRITER], env=env,
                                start_new_session=detached, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.monotonic() + 10
        while not (root / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return proc

    def _spawn_parent(self, root, *, release=None):
        env = dict(os.environ, FIXTURE_DIR=str(root), FIXTURE_DESCENDANT=_DESCENDANT)
        if release is not None:
            env["FIXTURE_RELEASE_PATH"] = str(release)
        return subprocess.Popen([sys.executable, "-c", _PARENT], env=env,
                                start_new_session=True, stdin=subprocess.DEVNULL)

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

    def test_detached_grandchild_is_gone_before_fixture_directory_removal(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        parent = self._spawn_parent(root)
        try:
            self.assertTrue(self._wait_for(root / "descendant-ready"), "detached grandchild did not start")
            child_pid = int((root / "descendant-ready").read_text())
            child_start = fixture_processes._start_ticks(child_pid)
            self.assertIsNotNone(child_start)
            self.assertNotIn(str(root).encode(), Path(f"/proc/{child_pid}/cmdline").read_bytes())
            self.assertEqual(parent.wait(timeout=5), 0)

            killed = fixture_processes.reap(str(root))

            self.assertIn(child_pid, killed)
            self.assertTrue(fixture_processes._gone(child_pid, child_start))
            self.assertEqual(fixture_processes.marked_processes(str(root)), {})
            tmp.cleanup()
            self.assertFalse(root.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            fixture_processes.reap(str(root))
            if root.exists():
                tmp.cleanup()

    def test_second_scan_reaps_a_detached_descendant_created_after_first_snapshot(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        release = root / "release-parent"
        parent = self._spawn_parent(root, release=release)
        original_scan = fixture_processes.marked_processes
        first_snapshot = {}
        descendant = {}
        scans = 0

        def delayed_scan(marker):
            nonlocal scans
            batch = original_scan(marker)
            scans += 1
            if scans == 1:
                first_snapshot.update(batch)
                release.touch()
                self.assertTrue(self._wait_for(root / "descendant-ready"),
                                "delayed descendant did not cross the barrier")
                child_pid = int((root / "descendant-ready").read_text())
                child_start = fixture_processes._start_ticks(child_pid)
                self.assertIsNotNone(child_start)
                descendant.update(pid=child_pid, start=child_start)
                self.assertEqual(parent.wait(timeout=5), 0)
            return batch

        try:
            with mock.patch.object(fixture_processes, "marked_processes", side_effect=delayed_scan):
                killed = fixture_processes.reap(str(root))

            child_pid = descendant["pid"]
            self.assertIn(parent.pid, first_snapshot)
            self.assertNotIn(child_pid, first_snapshot)
            self.assertGreaterEqual(scans, 2)
            self.assertIn(child_pid, killed)
            self.assertTrue(fixture_processes._gone(child_pid, descendant["start"]))
            self.assertEqual(original_scan(str(root)), {})
            tmp.cleanup()
            self.assertFalse(root.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            fixture_processes.reap(str(root))
            if root.exists():
                tmp.cleanup()

    def test_reap_does_not_kill_a_writer_owned_by_another_fixture_root(self):
        first_tmp = tempfile.TemporaryDirectory()
        second_tmp = tempfile.TemporaryDirectory()
        first_root, second_root = Path(first_tmp.name), Path(second_tmp.name)
        first = self._spawn(first_root)
        second = self._spawn(second_root)
        second_start = fixture_processes._start_ticks(second.pid)
        try:
            self.assertTrue((first_root / "ready").exists())
            self.assertTrue((second_root / "ready").exists())
            killed = fixture_processes.reap(str(first_root))
            self.assertIn(first.pid, killed)
            self.assertNotIn(second.pid, killed)
            self.assertFalse(fixture_processes._gone(second.pid, second_start))
            self.assertIn(second.pid, fixture_processes.marked_processes(str(second_root)))
            self.assertEqual(first.wait(timeout=5), -signal.SIGKILL)
            self.assertIn(second.pid, fixture_processes.reap(str(second_root)))
            self.assertEqual(second.wait(timeout=5), -signal.SIGKILL)
        finally:
            for proc, root in ((first, first_root), (second, second_root)):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                fixture_processes.reap(str(root))
            first_tmp.cleanup()
            second_tmp.cleanup()

    def test_nothing_marked_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(fixture_processes.reap(name), [])


if __name__ == "__main__":
    unittest.main()
