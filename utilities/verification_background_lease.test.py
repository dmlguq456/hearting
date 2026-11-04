#!/usr/bin/env python3
"""A verification lease ends everything its command started, and caps its memory."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
LEASE = HERE / "verification-background-lease.py"
sys.path.insert(0, str(HERE))
import foreground_activity as ACTIVITY
import owner_route_binding as OWNER_BINDING
from split_module_fixture import patch_split
spec = importlib.util.spec_from_file_location("verification_background_lease", LEASE)
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)


def owner_registry_fixture(root: Path, *, conflicting_stage_identity=False):
    attempt = "att-owner-lease"
    route = {
        "route_id": "rt-owner-lease",
        "route_hash": "sha256:rt-owner-lease",
        "advance_generation": 0,
        "route_family_key": "family-owner-lease",
        "owner_attempt_id": attempt,
        "cwd": str(root),
        "repo": str(root),
        "capability": "autopilot-code",
        "capability_mode": "debug",
        "effective_intensity": "standard",
        "artifact_root": str(root / "artifacts"),
        "reused_nodes": [],
    }
    route_path = root / "route.json"
    route_path.write_text(json.dumps(route), encoding="utf-8")
    metadata = {
        "attempt_id": attempt,
        "worker_type": "owner",
        "unit": "_kernel/owner",
        "attempt_schema_version": "2",
        "dispatch_depth": "1",
        "registered_worker": "1",
        "execution_surface": "registered-headless",
        "owner_harness": "codex",
        "capability": route["capability"],
        "capability_mode": route["capability_mode"],
        "intensity": "standard",
        "artifact_root": route["artifact_root"],
        "parent_sid": "launch-parent-session",
        "owner_route_file": str(route_path),
        "owner_route_id": route["route_id"],
        "owner_route_hash": route["route_hash"],
        "log_file": str(root / "owner.log"),
    }
    if conflicting_stage_identity:
        metadata.update({"route_id": "rt-conflict", "route_node": "fabricated"})
    jobs = root / "jobs.log"
    row = "\t".join(("2026-10-25T00:00:00Z", "running", str(root), str(root), "owner",
                      ",".join(f"{key}={value}" for key, value in metadata.items())))
    jobs.write_text(row + "\n", encoding="utf-8")
    (root / "owner.log").write_text("owner\n", encoding="utf-8")
    return attempt, route, jobs


def marked(marker):
    """Live `sleep <marker>` processes (not the lease, whose argv also names it)."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            argv = Path(f"/proc/{name}/cmdline").read_bytes().split(b"\0")
            if argv[:2] == [b"sleep", marker.encode()]:
                state = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
                if state not in (b"Z", b"X"):
                    found.append(int(name))
        except (OSError, IndexError):
            continue
    return found


def detaching(marker, *, then="pass"):
    """A command that leaves a setsid, environment-stripped sleeper behind."""
    return [sys.executable, "-c",
            "import subprocess, time\n"
            f"subprocess.Popen(['setsid', 'env', '-i', 'sleep', '{marker}'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "time.sleep(.3)\n" + then]


def forked_sharing(hold=3):
    code = (
        "import os, sys, time\n"
        "buffer = bytearray(8 * 1024 * 1024)\n"
        "for offset in range(0, len(buffer), 4096): buffer[offset] = 1\n"
        "children = []\n"
        "for _ in range(3):\n"
        " child = os.fork()\n"
        " if child == 0: time.sleep(float(sys.argv[1])); os._exit(0)\n"
        " children.append(child)\n"
        "print('ready', flush=True)\n"
        "for child in children: os.waitpid(child, 0)\n"
    )
    return [sys.executable, "-c", code, str(hold)]


class ScratchLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / "inherited-tmp"
        self.parent.mkdir()
        self.sibling = self.parent / "sibling"
        self.sibling.mkdir()
        (self.sibling / "keep").write_text("sibling", encoding="utf-8")
        self.child_pids: list[int] = []
        self.addCleanup(self.kill_children)

    def kill_children(self):
        for pid in self.child_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def lease_env(self):
        return {**os.environ, "TMPDIR": str(self.parent)}

    def run_lease(self, code, *args, timeout=5, env=None):
        environment = self.lease_env()
        if env:
            environment.update(env)
        command = [sys.executable, "-c", code, *(str(arg) for arg in args)]
        return subprocess.run([sys.executable, str(LEASE), "--timeout", str(timeout),
                               "--", *command], env=environment, capture_output=True,
                              text=True, timeout=30)

    def run_driver(self, code, *args, timeout=30):
        return subprocess.run([sys.executable, "-c", code, str(LEASE),
                               *(str(arg) for arg in args)], env=self.lease_env(),
                              capture_output=True, text=True, timeout=timeout)

    def assert_parent_preserved(self):
        self.assertTrue((self.sibling / "keep").exists())
        self.assertEqual(list(self.parent.iterdir()), [self.sibling])

    def test_success_nonzero_and_timeout_remove_only_the_private_root(self):
        marker = self.root / "scratch.path"
        command = (
            "import os,pathlib,sys,time\n"
            "scratch=pathlib.Path(os.environ['TMPDIR'])\n"
            "(scratch/'sentinel').write_text('owned',encoding='utf-8')\n"
            "pathlib.Path(sys.argv[1]).write_text(str(scratch),encoding='utf-8')\n"
            "if sys.argv[2]=='timeout': time.sleep(60)\n"
            "if sys.argv[2]=='nonzero': raise SystemExit(7)\n"
        )
        for outcome, expected, timeout in (("success", 0, 5), ("nonzero", 7, 5),
                                           ("timeout", 124, .3)):
            with self.subTest(outcome=outcome):
                done = self.run_lease(command, marker, outcome, timeout=timeout)
                self.assertEqual(done.returncode, expected, done.stderr)
                scratch = Path(marker.read_text(encoding="utf-8"))
                self.assertFalse(scratch.exists())
                self.assert_parent_preserved()

    def test_sigint_and_sigterm_remove_scratch_after_descendant_settlement(self):
        marker = self.root / "scratch.path"
        command = (
            "import os,pathlib,sys,time\n"
            "scratch=pathlib.Path(os.environ['TMPDIR'])\n"
            "(scratch/'sentinel').write_text('owned',encoding='utf-8')\n"
            "pathlib.Path(sys.argv[1]).write_text(str(scratch),encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                lease = subprocess.Popen(
                    [sys.executable, str(LEASE), "--timeout", "60", "--", sys.executable,
                     "-c", command, str(marker)], env=self.lease_env(),
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                scratch = Path(marker.read_text(encoding="utf-8"))
                lease.send_signal(signum)
                _stdout, stderr = lease.communicate(timeout=20)
                self.assertIn(lease.returncode, (128 + signum, -signum), stderr)
                self.assertFalse(scratch.exists())
                self.assert_parent_preserved()

    def test_descendant_last_scratch_use_finishes_before_reap_and_removal(self):
        ready = self.root / "child.ready"
        last_use = self.root / "child.last-use"
        scratch_path = self.root / "scratch.path"
        child = (
            "import os,pathlib,signal,sys,time\n"
            "ready,last_use,scratch_path=sys.argv[1:]\n"
            "scratch=pathlib.Path(os.environ['TMPDIR'])\n"
            "def finish(_signal,_frame):\n"
            " (scratch/'last-use').write_text('finished',encoding='utf-8')\n"
            " pathlib.Path(last_use).write_text('reaped',encoding='utf-8')\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,finish)\n"
            "pathlib.Path(ready).write_text('ready',encoding='utf-8')\n"
            "pathlib.Path(scratch_path).write_text(str(scratch),encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        command = (
            "import pathlib,subprocess,sys,time\n"
            "child,ready,last_use,scratch_path=sys.argv[1:]\n"
            "subprocess.Popen([sys.executable,'-c',child,ready,last_use,scratch_path])\n"
            "deadline=time.monotonic()+8\n"
            "while not pathlib.Path(ready).exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "if not pathlib.Path(ready).exists(): raise SystemExit('child did not become ready')\n"
        )
        done = self.run_lease(command, child, ready, last_use, scratch_path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(last_use.read_text(encoding="utf-8"), "reaped")
        scratch = Path(scratch_path.read_text(encoding="utf-8"))
        self.assertFalse(scratch.exists())
        self.assert_parent_preserved()

    def test_owned_live_child_keeps_scratch_and_reports_the_path(self):
        pid_file = self.root / "owned.pid"
        scratch_file = self.root / "scratch.path"
        driver = r'''import importlib.util,os,pathlib,sys
lease_path,pid_file,scratch_file=sys.argv[1:]
sys.path.insert(0,str(pathlib.Path(lease_path).parent))
spec=importlib.util.spec_from_file_location("scratch_live_lease",lease_path)
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
real_kill=lease.os.kill
def ignore_owned(pid,signum):
    try: owned=int(pathlib.Path(pid_file).read_text())
    except (OSError,ValueError): owned=-1
    if pid==owned: return
    return real_kill(pid,signum)
lease.os.kill=ignore_owned
real_end=lease.end_scope
lease.end_scope=lambda scope,**kwargs: real_end(scope,grace=.02,**kwargs)
child="import time; time.sleep(60)"
command="import os,pathlib,subprocess,sys,time; child=subprocess.Popen(['setsid',sys.executable,'-c',sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); pathlib.Path(sys.argv[2]).write_text(str(child.pid)); pathlib.Path(sys.argv[3]).write_text(os.environ['TMPDIR']); time.sleep(60)"
raise SystemExit(lease.main(["--timeout",".4","--",sys.executable,"-c",command,child,pid_file,scratch_file]))
'''
        done = self.run_driver(driver, pid_file, scratch_file, timeout=20)
        self.assertEqual(done.returncode, L.SCOPE_CLEANUP_FAILED_EXIT, done.stderr)
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.child_pids.append(pid)
        scratch = Path(scratch_file.read_text(encoding="utf-8"))
        self.assertTrue(scratch.is_dir())
        self.assertIn(f"verification-scratch-preserved path={scratch}", done.stderr)
        self.assertIn(f"{pid}:live", done.stderr)

    def test_owned_unreadable_child_keeps_scratch(self):
        pid_file = self.root / "owned.pid"
        scratch_file = self.root / "scratch.path"
        driver = r'''import importlib.util,os,pathlib,sys
lease_path,pid_file,scratch_file=sys.argv[1:]
sys.path.insert(0,str(pathlib.Path(lease_path).parent))
spec=importlib.util.spec_from_file_location("scratch_unknown_lease",lease_path)
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
real_read=lease._read_process_stat
real_end=lease.end_scope
def hide_owned(scope,observed=None,**kwargs):
    pid=int(pathlib.Path(pid_file).read_text())
    def unreadable(candidate):
        if candidate==pid: raise PermissionError("fixture unreadable owned pid")
        return real_read(candidate)
    lease._read_process_stat=unreadable
    return real_end(scope,observed=observed,grace=.02,**kwargs)
lease.end_scope=hide_owned
child="import time; time.sleep(60)"
command="import os,pathlib,subprocess,sys,time; child=subprocess.Popen(['setsid',sys.executable,'-c',sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); pathlib.Path(sys.argv[2]).write_text(str(child.pid)); pathlib.Path(sys.argv[3]).write_text(os.environ['TMPDIR']); time.sleep(60)"
raise SystemExit(lease.main(["--timeout",".4","--",sys.executable,"-c",command,child,pid_file,scratch_file]))
'''
        done = self.run_driver(driver, pid_file, scratch_file, timeout=20)
        self.assertEqual(done.returncode, L.SCOPE_CLEANUP_FAILED_EXIT, done.stderr)
        pid = int(pid_file.read_text(encoding="utf-8"))
        self.child_pids.append(pid)
        scratch = Path(scratch_file.read_text(encoding="utf-8"))
        self.assertTrue(scratch.is_dir())
        self.assertIn(f"verification-scratch-preserved path={scratch}", done.stderr)
        self.assertIn(f"{pid}:unreadable", done.stderr)

    def test_unrelated_unreadable_process_does_not_keep_scratch(self):
        unrelated = subprocess.Popen(["sleep", "60"])
        def stop_unrelated():
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)
        self.addCleanup(stop_unrelated)
        marker = self.root / "scratch.path"
        driver = r'''import importlib.util,os,pathlib,sys
lease_path,unrelated,marker=sys.argv[1:]
sys.path.insert(0,str(pathlib.Path(lease_path).parent))
spec=importlib.util.spec_from_file_location("scratch_unrelated_lease",lease_path)
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
real_read=lease._read_process_stat
def unreadable(candidate):
    if candidate==int(unrelated): raise PermissionError("fixture unrelated pid")
    return real_read(candidate)
lease._read_process_stat=unreadable
code="import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(os.environ['TMPDIR'])"
raise SystemExit(lease.main(["--timeout","5","--",sys.executable,"-c",code,marker]))
'''
        done = self.run_driver(driver, unrelated.pid, marker)
        self.assertEqual(done.returncode, 0, done.stderr)
        scratch = Path(marker.read_text(encoding="utf-8"))
        self.assertFalse(scratch.exists())
        self.assertIsNone(unrelated.poll())
        self.assert_parent_preserved()

    def test_replaced_root_symlink_and_external_target_are_preserved(self):
        marker = self.root / "scratch.path"
        external = self.root / "external"
        external.mkdir()
        target = external / "keep"
        target.write_text("external", encoding="utf-8")
        command = (
            "import os,pathlib,sys\n"
            "scratch=pathlib.Path(os.environ['TMPDIR'])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(scratch),encoding='utf-8')\n"
            "scratch.rmdir()\n"
            "scratch.symlink_to(sys.argv[2],target_is_directory=True)\n"
        )
        done = self.run_lease(command, marker, external)
        self.assertEqual(done.returncode, L.SCOPE_CLEANUP_FAILED_EXIT, done.stderr)
        scratch = Path(marker.read_text(encoding="utf-8"))
        self.assertTrue(scratch.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "external")
        self.assertIn("reason=root-replaced", done.stderr)

    def test_spawn_and_lease_publication_failures_finalize_private_scratch(self):
        marker = self.root / "scratch.path"
        spawn_driver = r'''import importlib.util,pathlib,sys
lease_path,marker=sys.argv[1:]
sys.path.insert(0,str(pathlib.Path(lease_path).parent))
spec=importlib.util.spec_from_file_location("scratch_spawn_failure",lease_path)
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
import verification_runtime
real_prepare=verification_runtime.verification_command
def prepare(command):
    resolved,env=real_prepare(command)
    def fail(command,**kwargs):
        pathlib.Path(marker).write_text(kwargs["env"]["TMPDIR"])
        raise OSError("fixture spawn failure")
    lease.subprocess.Popen=fail
    return resolved,env
verification_runtime.verification_command=prepare
try: lease.main(["--timeout","5","--",sys.executable,"-c","pass"])
except OSError: raise SystemExit(0)
raise SystemExit("spawn unexpectedly succeeded")
'''
        failed_spawn = self.run_driver(spawn_driver, marker)
        self.assertEqual(failed_spawn.returncode, 0, failed_spawn.stderr)
        self.assertFalse(Path(marker.read_text(encoding="utf-8")).exists())

        lease_file = self.root / "verification-lease.json"
        publish_driver = r'''import importlib.util,pathlib,sys
lease_path,marker,lease_file=sys.argv[1:]
sys.path.insert(0,str(pathlib.Path(lease_path).parent))
spec=importlib.util.spec_from_file_location("scratch_publish_failure",lease_path)
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
lease.lease_context=lambda *_args:(pathlib.Path(lease_file),{})
def fail_write(*_args): raise OSError("fixture publication failure")
lease.atomic_json=fail_write
code="import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(os.environ['TMPDIR']); time.sleep(60)"
try: lease.main(["--timeout","5","--",sys.executable,"-c",code,marker])
except OSError: raise SystemExit(0)
raise SystemExit("lease publication unexpectedly succeeded")
'''
        failed_publish = self.run_driver(publish_driver, marker, lease_file)
        self.assertEqual(failed_publish.returncode, 0, failed_publish.stderr)
        self.assertFalse(Path(marker.read_text(encoding="utf-8")).exists())
        self.assertFalse(lease_file.exists())
        self.assert_parent_preserved()


class LeaseScopeTest(unittest.TestCase):
    def setUp(self):
        self.marker = f"{300 + os.getpid() % 1000}.{time.monotonic_ns() % 10**6}"
        self.addCleanup(self.kill_marked)

    def kill_marked(self):
        for pid in marked(self.marker):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def run_lease(self, command, timeout=30, options=()):
        return subprocess.run([sys.executable, str(LEASE), "--timeout", str(timeout), *options, "--", *command],
                              capture_output=True, text=True, timeout=60)

    def test_a_detached_descendant_ends_with_its_command(self):
        done = self.run_lease(detaching(self.marker))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(marked(self.marker), [])
        self.assertIn("verification-scope-ended", done.stderr)

    def test_expiry_returns_124_and_ends_the_detached_descendant(self):
        done = self.run_lease(detaching(self.marker, then="time.sleep(60)"), timeout=2)
        self.assertEqual(done.returncode, 124, done.stderr)
        self.assertEqual(marked(self.marker), [])
        self.assertIn("verification-memory-peak", done.stderr)

    def test_a_stopped_lease_ends_its_scope(self):
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                      *detaching(self.marker, then="time.sleep(60)")],
                                     stdout=subprocess.DEVNULL, stderr=stderr_file)
            deadline = time.monotonic() + 10
            while not marked(self.marker) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marked(self.marker))
            lease.send_signal(signal.SIGTERM)
            self.assertEqual(lease.wait(timeout=20), 128 + signal.SIGTERM)
            stderr_file.seek(0)
            stderr = stderr_file.read()
        self.assertIn("verification-memory-peak", stderr)
        self.assertEqual(marked(self.marker), [])

    def test_sigint_ends_scope_and_prints_peak(self):
        with tempfile.TemporaryFile(mode="w+t") as stderr_file:
            lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                      *detaching(self.marker, then="time.sleep(60)")],
                                     stdout=subprocess.DEVNULL, stderr=stderr_file)
            deadline = time.monotonic() + 10
            while not marked(self.marker) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marked(self.marker))
            lease.send_signal(signal.SIGINT)
            lease.wait(timeout=20)
            stderr_file.seek(0)
            stderr = stderr_file.read()
        self.assertIn(lease.returncode, (-signal.SIGINT, 128 + signal.SIGINT))
        self.assertIn("verification-memory-peak", stderr)
        self.assertEqual(marked(self.marker), [])

    def test_a_command_with_nothing_left_reports_nothing(self):
        done = self.run_lease([sys.executable, "-c", "print('ok')"])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"))
        self.assertNotIn("verification-scope-ended", done.stderr)
        self.assertRegex(done.stderr, r"verification-memory-peak bytes=none measure=none .*scope=tree-aggregate samples=0")

    def test_peak_line_on_sampled_success(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        done = self.run_lease([sys.executable, "-c", "import time; time.sleep(1.5)"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stderr, r"verification-memory-peak bytes=\d+ measure=pss-sum")
        self.assertRegex(done.stderr, r"samples=[1-9]\d* .*budget_bytes=" +
                         str(L.memory_budget_bytes()) + r" budget_source=default")

    def test_term_and_hup_during_spawn_and_lease_publication_end_the_scope(self):
        for timing in ("spawn", "publication"):
            for signum in (signal.SIGTERM, signal.SIGHUP):
                with self.subTest(timing=timing, signum=signum), tempfile.TemporaryDirectory() as td:
                    base = Path(td)
                    identity = base / "child.identity"
                    detached = base / "detached.pid"
                    lease_file = base / "lease.json"
                    command = [sys.executable, "-c",
                        "import subprocess,pathlib,time\n"
                        f"child=subprocess.Popen(['setsid','env','-i','sleep',{self.marker!r}])\n"
                        f"pathlib.Path({str(detached)!r}).write_text(str(child.pid))\n"
                        "time.sleep(60)\n"]
                    driver = f'''import importlib.util,os,pathlib,signal,sys,time
sys.path.insert(0,{str(HERE)!r})
spec=importlib.util.spec_from_file_location("signal_window_lease",{str(LEASE)!r})
lease=importlib.util.module_from_spec(spec);spec.loader.exec_module(lease)
real_spawn=lease.subprocess.Popen
def spawn(*args,**kwargs):
    child=real_spawn(*args,**kwargs)
    start=lease.process_launch_identity(child.pid).get("pid_start","")
    pathlib.Path({str(identity)!r}).write_text(str(child.pid)+":"+start)
    if {timing!r}=="spawn": os.kill(os.getpid(),{int(signum)})
    return child
lease.subprocess.Popen=spawn
if {timing!r}=="publication":
    real_write=lease.atomic_json
    lease.lease_context=lambda *args:(pathlib.Path({str(lease_file)!r}),{{}})
    def write(path,value):
        real_write(path,value)
        deadline=time.monotonic()+5
        while not pathlib.Path({str(detached)!r}).exists():
            if time.monotonic()>=deadline: raise RuntimeError("fixture child did not start")
            time.sleep(.01)
        os.kill(os.getpid(),{int(signum)})
    lease.atomic_json=write
raise SystemExit(lease.main(["--timeout","20","--",*{command!r}]))
'''
                    try:
                        done = subprocess.run([sys.executable, "-c", driver],
                            capture_output=True, text=True, timeout=30)
                        self.assertEqual(done.returncode, 128 + signum, done.stderr)
                        pid, _start = identity.read_text().split(":")
                        self.assertFalse(Path(f"/proc/{pid}").exists())
                        if detached.exists():
                            self.assertFalse(Path(f"/proc/{detached.read_text()}").exists())
                        self.assertFalse(lease_file.exists())
                        self.assertEqual(marked(self.marker), [])
                    finally:
                        if identity.exists():
                            pid, start = identity.read_text().split(":")
                            current = L.process_launch_identity(int(pid))
                            if start and current.get("pid_start") == start:
                                try:
                                    os.killpg(int(pid), signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                        self.kill_marked()

    def test_required_subreaper_failure_does_not_start_the_command(self):
        with mock.patch.object(L, "become_subreaper", return_value=False), \
                mock.patch.object(L.subprocess, "Popen") as spawn, \
                mock.patch.object(L.sys, "stderr", new=io.StringIO()):
            status = L.main(["--timeout", "1", "--require-subreaper", "--", "fixture"])
        self.assertEqual(status, L.SUBREAPER_UNAVAILABLE_EXIT)
        spawn.assert_not_called()

    def test_registered_owner_lease_round_trips_through_writer_and_observer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            attempt, route, jobs = owner_registry_fixture(root)
            lease = root / "verification-leases" / f"{attempt}.json"
            command = [sys.executable, "-c", "import time; time.sleep(0.2)"]
            observed = {}

            def inspect_from_wait(process, timeout, _scope, budget, source, peak, on_peak, _observed):
                self.assertEqual(budget, L.memory_budget_bytes())
                self.assertEqual(source, "default")
                self.assertIsInstance(peak, L.MemoryPeak)
                self.assertTrue(callable(on_peak))
                record = json.loads(lease.read_text(encoding="utf-8"))
                observed["record"] = record
                stat = ACTIVITY._stat(Path("/proc") / str(process.pid) / "stat")
                context = ACTIVITY.ForegroundActivityContext(
                    window_seconds=7200, harness="codex", log_path=root / "owner.log",
                    metadata={"attempt_id": attempt, "route_id": route["route_id"]},
                    jobs=jobs, owner_turn=True,
                )
                monitor = ACTIVITY.ForegroundActivityMonitor(process.pid, stat["start"], context)
                observed["active"] = monitor._lease_active()
                observed["monitor"] = monitor
                return process.wait(timeout=timeout)

            signals = (signal.SIGTERM, signal.SIGHUP, signal.SIGCHLD)
            handlers = {signum: signal.getsignal(signum) for signum in signals}
            try:
                with mock.patch.dict(os.environ, {
                    "AGENT_DISPATCH_ATTEMPT_ID": attempt,
                    "AGENT_DISPATCH_JOBS": str(jobs),
                }), patch_split(OWNER_BINDING.ROUTE, "verify_route",
                                      side_effect=lambda raw, **_kwargs: raw), \
                     mock.patch.object(L, "wait_within_budget", side_effect=inspect_from_wait):
                    status = L.main(["--timeout", "3", "--", *command])
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)

            record = observed["record"]
            self.assertEqual(status, 0)
            self.assertTrue(observed["active"])
            self.assertEqual(record["schema_version"], 1)
            self.assertEqual(record["attempt_id"], attempt)
            self.assertEqual(record["route_id"], route["route_id"])
            self.assertEqual(record["route_node"], "_owner")
            self.assertEqual(record["command_digest"], hashlib.sha256(
                "\0".join(command).encode("utf-8")).hexdigest())
            self.assertGreater(record["deadline"], time.time())
            self.assertEqual(record["pgid"], record["pid"])
            self.assertTrue(record["pid_start"])
            self.assertFalse(lease.exists())
            self.assertFalse(observed["monitor"]._lease_active())

    def test_registered_owner_with_conflicting_stage_identity_gets_no_lease(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            attempt, _route, jobs = owner_registry_fixture(
                root, conflicting_stage_identity=True)
            with mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_ATTEMPT_ID": attempt,
                "AGENT_DISPATCH_JOBS": str(jobs),
            }), patch_split(OWNER_BINDING.ROUTE, "verify_route",
                                  side_effect=lambda raw, **_kwargs: raw):
                self.assertIsNone(L.lease_context(["python3", "verify"], 10))

    def test_the_scope_past_its_memory_budget_is_refused(self):
        process = subprocess.Popen(["sleep", self.marker], start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with mock.patch.object(L, "memory_budget_bytes", return_value=1), \
                self.assertRaisesRegex(MemoryError, "verification-memory-budget-exceeded.*budget_source=default"):
            L.wait_within_budget(process, 30, "unused-scope")
        self.assertIsNone(process.poll())  # the caller, not the check, ends the scope

    def test_default_budget_is_unchanged(self):
        self.assertEqual(L.resolve_memory_budget(None), (L.memory_budget_bytes(), "default"))

    def test_size_grammar(self):
        expected = {"1048576": 1048576, "1K": 1024, "1KiB": 1024, "1k": 1024,
                    "1KB": 1000, "140GB": 140 * 10**9, "8GiB": 8 * 1024**3,
                    "1.5GiB": 1610612736, "2T": 2 * 1024**4}
        for raw, value in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(L.parse_size(raw), value)
        for raw in ("", "abc", "0", "-1", "1.5", "1XB", "nan", "inf", "0.0001K"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                L.parse_size(raw)

    def test_budget_above_host_memory_is_refused(self):
        with mock.patch.object(L, "host_memory_bytes", return_value=8 * 1024**3):
            with self.assertRaisesRegex(ValueError, "9GiB"):
                L.resolve_memory_budget("9GiB")
            self.assertEqual(L.resolve_memory_budget("8GiB"), (8 * 1024**3, "option"))
        with mock.patch.object(L, "host_memory_bytes", return_value=0):
            self.assertEqual(L.resolve_memory_budget("9GiB"), (9 * 1024**3, "option"))

    def test_bad_budget_is_a_usage_error_before_the_command_runs(self):
        host = L.host_memory_bytes()
        for option in ("0", "-5", "lots", f"{host * 2}B"):
            with self.subTest(option=option):
                done = self.run_lease([sys.executable, "-c", "print('ran')"],
                                      options=("--memory-budget", option))
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("--memory-budget", done.stderr)
                self.assertNotIn("ran", done.stdout)

    def test_option_applies_and_the_message_names_its_source(self):
        done = self.run_lease(["sleep", self.marker], options=("--memory-budget", "1"))
        self.assertEqual(done.returncode, 125, done.stderr)
        self.assertIn("verification-memory-budget-exceeded", done.stderr)
        self.assertIn("budget_bytes=1", done.stderr)
        self.assertIn("budget_source=option", done.stderr)
        self.assertRegex(done.stderr, r"measure=(?:pss-sum|rss-sum|mixed)")
        self.assertIn("verification-memory-peak", done.stderr)
        self.assertIn("budget_source=option", done.stderr.split("verification-memory-peak", 1)[1])
        self.assertEqual(marked(self.marker), [])

    def test_fork_shared_buffer_is_counted_once(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        process = subprocess.Popen(forked_sharing(), stdout=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        table = L.process_table()
        pids = {pid for pid, (ppid, _rss) in table.items()
                if pid == process.pid or ppid == process.pid}
        rss = sum(table[pid][1] for pid in pids)
        measured, measure = L.measure_scope(table, pids)
        self.assertEqual(measure, "pss-sum")
        self.assertGreaterEqual(rss - measured, 12 * 1024**2)
        process.wait()

    def test_budget_between_pss_and_rss_passes_but_rss_fallback_refuses(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        process = subprocess.Popen(forked_sharing(), stdout=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        process.stdout.close()
        table = L.process_table()
        pids = {pid for pid, (ppid, _rss) in table.items()
                if pid == process.pid or ppid == process.pid}
        rss = sum(table[pid][1] for pid in pids)
        pss, _measure = L.measure_scope(table, pids)
        budget = (rss + pss) // 2
        self.assertEqual(L.wait_within_budget(process, 10, "unused-scope", budget=budget), 0)

        fallback = subprocess.Popen(forked_sharing(hold=10), stdout=subprocess.PIPE, text=True,
                                    start_new_session=True)
        self.assertEqual(fallback.stdout.readline().strip(), "ready")
        fallback.stdout.close()
        try:
            with mock.patch.object(L, "pss_bytes", return_value=None), \
                    self.assertRaisesRegex(MemoryError, "budget_source=default measure=rss-sum"):
                L.wait_within_budget(fallback, 30, "unused-scope", budget=budget)
        finally:
            os.killpg(fallback.pid, signal.SIGKILL)
            fallback.wait()

    def test_unreadable_pss_falls_back_per_pid(self):
        processes = [subprocess.Popen(["sleep", "10"]) for _ in range(2)]
        for process in processes:
            self.addCleanup(process.kill)
            self.addCleanup(process.wait)
        table = L.process_table()
        pids = {process.pid for process in processes}
        with mock.patch.object(L, "pss_bytes", side_effect=lambda pid: None if pid == processes[0].pid else 100):
            _value, measure = L.measure_scope(table, pids)
            self.assertEqual(measure, "mixed")
        with mock.patch.object(L, "pss_bytes", return_value=None):
            _value, measure = L.measure_scope(table, pids)
            self.assertEqual(measure, "rss-sum")
            missing = max(table) + 100000
            value, measure = L.measure_scope({missing: (1, 999)}, {missing})
            self.assertEqual((value, measure), (0, "pss-sum"))

    def test_pss_read_permission_error_is_none(self):
        if L.pss_bytes(os.getpid()) is None:
            self.skipTest("/proc smaps_rollup PSS is unavailable")
        with mock.patch("builtins.open", side_effect=PermissionError):
            self.assertIsNone(L.pss_bytes(os.getpid()))

    def test_running_peak_is_recorded_only_when_it_grows(self):
        peak = L.MemoryPeak()
        peak.record(100, "pss-sum", 1.0)
        self.assertTrue(peak.json_due(1600))
        peak.written = peak.bytes
        self.assertFalse(peak.json_due(1600))
        peak.record(199, "pss-sum", 1.0)
        self.assertFalse(peak.json_due(1600))
        peak.record(200, "mixed", 2.0)
        self.assertTrue(peak.json_due(1600))

    def test_the_budget_is_a_quarter_of_a_small_host(self):
        with mock.patch.object(L.os, "sysconf", side_effect=lambda name: {
                "SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": (8 * 1024 ** 3) // 4096}[name]):
            self.assertEqual(L.memory_budget_bytes(), 2 * 1024 ** 3)
        self.assertLessEqual(L.memory_budget_bytes(), 16 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
