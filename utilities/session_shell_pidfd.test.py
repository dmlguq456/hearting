"""The session shell owner runs, and leaves the terminal usable, on Python builds that lack the pidfd calls."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
from pathlib import Path
import platform
import pty
import select
import signal
import subprocess
import tempfile
import time
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


# Runs pty_owner.run on a real PTY in a child Python with the named pidfd calls removed.
OWNER = r"""
import os,sys,termios,traceback
mode,util,host=sys.argv[1:4]
if mode in('no-open','no-open-no-send'):delattr(os,'pidfd_open') if hasattr(os,'pidfd_open') else None
if mode=='no-open-no-send':
    import signal
    if hasattr(signal,'pidfd_send_signal'):del signal.pidfd_send_signal
sys.path[:0]=[util,os.path.join(util,'session-shell')]
import pty_owner
env={'HEARTING_SHELL_SOCKET':'/nonexistent','HEARTING_SHELL_TOKEN':'x'}
try:
    code=pty_owner.run('claude',['/bin/sh','-c','exit 0'],['/bin/sh','-c','exit 0'],sys.executable,host,env,os.getcwd())
    print('OWNER_RETURNED',code,flush=True)
except BaseException as exc:
    print('OWNER_RAISED',type(exc).__name__,exc,flush=True)
    print('AT',traceback.extract_tb(exc.__traceback__)[-1].lineno,flush=True)
print('TTY_CANONICAL' if termios.tcgetattr(0)[3]&termios.ICANON else 'TTY_LEFT_RAW',flush=True)
"""

# A fake display: it reports STARTED, then outlives the native, so the owner has to terminate it.
DISPLAY = r"""
import os,sys,time
open(sys.argv[-1],'w').write(str(os.getpid()))
os.write(int(os.environ['HEARTING_SHELL_START_FD']),b'STARTED\n')
time.sleep(30)
"""


def drive_owner(mode):
    with tempfile.TemporaryDirectory(prefix="pidfd-") as directory:
        owner = Path(directory) / "owner.py"
        owner.write_text(OWNER)
        pidfile = Path(directory) / "display.pid"
        display = Path(directory) / "display.py"
        display.write_text(DISPLAY.replace("sys.argv[-1]", repr(str(pidfile))))
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(directory)
            os.execv(sys.executable, [sys.executable, "-I", str(owner), mode, str(ROOT / "utilities"), str(display)])
        out = b""
        deadline = time.monotonic() + 40
        try:
            while time.monotonic() < deadline:
                if not select.select([fd], [], [], 0.5)[0]:
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        finally:
            for target in (pid,):  # only the processes this test started
                try:
                    os.kill(target, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os.waitpid(pid, 0)
            os.close(fd)
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return out.decode(errors="replace")


class OwnerWithoutPidfdCallsTest(unittest.TestCase):
    """The owner returns 0 and restores the terminal, whichever pidfd calls the build has."""

    def check(self, mode):
        out = drive_owner(mode)
        self.assertIn("OWNER_RETURNED 0", out, out)
        self.assertIn("TTY_CANONICAL", out, out)

    def test_a_normal_python(self):
        self.check("normal")

    def test_a_build_without_os_pidfd_open(self):
        self.check("no-open")  # also guards the spawn_display call site

    def test_a_build_without_either_pidfd_call(self):
        self.check("no-open-no-send")  # terminate_display must not need signal.pidfd_send_signal


if __name__ == "__main__":
    unittest.main()
