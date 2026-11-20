"""The session shell owner gets a pidfd even where the Python build lacks os.pidfd_open."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import platform
import signal
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "utilities/session-shell")]
import pty_owner


class PidfdOpenTest(unittest.TestCase):
    def test_the_native_call_is_used_when_the_build_has_it(self):
        with mock.patch.object(pty_owner.os, "pidfd_open", return_value=42, create=True) as native:
            self.assertEqual(pty_owner._pidfd_open(123), 42)
        native.assert_called_once_with(123)

    @unittest.skipUnless(sys.platform == "linux" and platform.machine() == "x86_64"
                         and hasattr(signal, "pidfd_send_signal"),
                         "the syscall fallback is checked only for Linux x86_64")
    def test_a_build_without_os_pidfd_open_still_gets_a_descriptor_for_the_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with mock.patch.object(pty_owner.os, "pidfd_open", None, create=True):
            fd = pty_owner._pidfd_open(child.pid)
        self.addCleanup(os.close, fd)
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
