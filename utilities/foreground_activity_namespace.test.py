#!/usr/bin/env python3
"""Nested PID visibility checks under Codex sandbox and bubblewrap."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = r"""import json, os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dispatch_contract import process_start_ticks
from dispatch_lifecycle import wait_foreground
from foreground_activity import ForegroundActivityContext
mode, output, bwrap = sys.argv[2:]
pidns = os.readlink('/proc/self/ns/pid')
nspid = next((x.strip() for x in Path('/proc/self/status').read_text().splitlines() if x.startswith('NSpid:')), '')
payload = r'''import os, sys, time
pid = os.fork()
if pid == 0:
    os.setsid()
    task = "import os,time; open(os.environ['FOREGROUND_ACTIVITY_MARKER']+'.started','w').write('1'); end=time.monotonic()+1.8\nwhile time.monotonic()<end: pass\nopen(os.environ['FOREGROUND_ACTIVITY_MARKER'],'w').write('done')" if sys.argv[1]=='busy' else "import os; open(os.environ['FOREGROUND_ACTIVITY_MARKER']+'.started','w').write('1'); os.read(0, 1)"
    marker_root = os.path.dirname(os.environ['FOREGROUND_ACTIVITY_MARKER'])
    os.execvp(sys.argv[2], [sys.argv[2], '--die-with-parent', '--unshare-pid', '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--bind', marker_root, marker_root, sys.executable, '-c', task])
os.waitpid(pid, 0)
'''
os.environ['FOREGROUND_ACTIVITY_MARKER'] = output + '.marker'
errfile = open(output + '.err', 'w')
child = subprocess.Popen([sys.executable, '-c', payload, mode, bwrap], start_new_session=True,
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errfile)
start = process_start_ticks(child.pid)
context = ForegroundActivityContext(0.7, os.path.basename(sys.executable), Path(output), {}, runtime_executable='/bin/false')
t0 = time.monotonic()
try:
    outcome = wait_foreground(child, 4, poll_interval=0.03, activity_context=context)
    result = {'pid_namespace': pidns, 'nspid': nspid, 'failure': outcome.failure,
              'elapsed': time.monotonic()-t0, 'child_pid': child.pid,
              'child_start': start, 'group_empty': outcome.group_empty}
finally:
    if child.poll() is None:
        from dispatch_lifecycle import _bounded_group_stop
        _bounded_group_stop(child, start or '', poll_interval=0.02)
errfile.close()
result['inner_started'] = Path(output + '.marker.started').exists()
result['inner_completed'] = Path(output + '.marker').exists()
result['inner_error'] = Path(output + '.err').read_text()
print(json.dumps(result, sort_keys=True))
"""


# Inner bubblewrap flags; keep in sync with the os.execvp argument list in
# RUNNER. The probes run this same two-layer shape, because a runner image can
# allow the outer bwrap yet refuse the nested one (user namespaces confined).
INNER_BWRAP_ARGS = ["--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/",
                    "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]


class ForegroundActivityNamespaceTests(unittest.TestCase):
    def _run_cell(self, wrapper: list[str], mode: str, env=None):
        with tempfile.TemporaryDirectory(prefix="foreground-activity-", dir=ROOT) as td:
            base = Path(td)
            runner = base / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            log = base / "attempt.log"
            command = [*wrapper, sys.executable, str(runner), str(ROOT / "utilities"), mode,
                       str(log), shutil.which("bwrap") or "bwrap"]
            return subprocess.run(command, text=True, capture_output=True, timeout=15, env=env)

    def _require_or_skip(self, reason):
        if os.environ.get("FOREGROUND_ACTIVITY_REQUIRE_NAMESPACE") == "1":
            self.fail(reason)
        self.skipTest(reason)

    def _runtime_skip(self, reason):
        self.skipTest(reason)

    def test_codex_sandbox_nested_pid_activity(self):
        codex = shutil.which("codex")
        bwrap = shutil.which("bwrap")
        if not codex or not bwrap:
            self._runtime_skip("Codex sandbox cell unavailable: codex or bwrap executable missing")
        with tempfile.TemporaryDirectory(prefix="codex-home-") as home, \
             tempfile.TemporaryDirectory(prefix="codex-runtime-") as runtime:
            os.chmod(home, 0o700)
            os.chmod(runtime, 0o700)
            codex_home = Path(home) / ".codex"
            socket_dir = codex_home / "app-server"
            socket_dir.mkdir(mode=0o700, parents=True)
            os.chmod(codex_home, 0o700)
            env = os.environ.copy()
            env.update(HOME=home, CODEX_HOME=str(codex_home), XDG_RUNTIME_DIR=runtime)
            probe = subprocess.run([codex, "sandbox", "-P", ":workspace", "--", bwrap,
                                    *INNER_BWRAP_ARGS, "true"],
                                   text=True, capture_output=True, timeout=10, env=env)
            if probe.returncode:
                self._runtime_skip("nested bubblewrap under Codex sandbox unavailable: "
                                   + probe.stderr.strip())
            for mode in ("busy", "quiet"):
                result = self._run_cell([codex, "sandbox", "-P", ":workspace", "--"], mode, env)
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertTrue(evidence["inner_started"], evidence.get("inner_error") or evidence)
                self.assertIn("pid:[", evidence["pid_namespace"])
                self.assertTrue(evidence["nspid"].startswith("NSpid:"))
                self.assertTrue(evidence["group_empty"])
                if mode == "busy":
                    self.assertEqual(evidence["failure"], "")
                    self.assertGreaterEqual(evidence["elapsed"], 1.4)
                else:
                    self.assertEqual(evidence["failure"], "no-progress")

    def test_bubblewrap_outer_pid_namespace_cell(self):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            self._require_or_skip("bubblewrap PID namespace cell unavailable: bwrap executable missing")
        wrapper = [bwrap, "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/",
                   "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                   "--bind", str(ROOT), str(ROOT)]
        probe = subprocess.run([*wrapper, bwrap, *INNER_BWRAP_ARGS, "true"],
                               text=True, capture_output=True, timeout=10)
        if probe.returncode:
            self._require_or_skip("bubblewrap PID namespace unavailable: " + probe.stderr.strip())
        for mode in ("busy", "quiet"):
            result = self._run_cell(wrapper, mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(evidence["inner_started"], evidence.get("inner_error") or evidence)
            self.assertTrue(evidence["nspid"].startswith("NSpid:"))
            self.assertTrue(evidence["group_empty"])
            self.assertEqual(evidence["failure"], "" if mode == "busy" else "no-progress", evidence)


if __name__ == "__main__":
    unittest.main()
