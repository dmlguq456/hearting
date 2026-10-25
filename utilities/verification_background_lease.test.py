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
            f"subprocess.Popen(['setsid', 'env', '-i', 'sleep', '{marker}'])\n"
            "time.sleep(.3)\n" + then]


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

    def run_lease(self, command, timeout=30):
        return subprocess.run([sys.executable, str(LEASE), "--timeout", str(timeout), "--", *command],
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

    def test_a_stopped_lease_ends_its_scope(self):
        lease = subprocess.Popen([sys.executable, str(LEASE), "--timeout", "60", "--",
                                  *detaching(self.marker, then="time.sleep(60)")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 10
        while not marked(self.marker) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marked(self.marker))
        lease.send_signal(signal.SIGTERM)
        self.assertEqual(lease.wait(timeout=20), 128 + signal.SIGTERM)
        lease.stderr.close()
        self.assertEqual(marked(self.marker), [])

    def test_a_command_with_nothing_left_reports_nothing(self):
        done = self.run_lease([sys.executable, "-c", "print('ok')"])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"))
        self.assertNotIn("verification-scope-ended", done.stderr)

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

            def inspect_from_wait(process, timeout, _scope):
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
                }), mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
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
            }), mock.patch.object(OWNER_BINDING.ROUTE, "verify_route",
                                  side_effect=lambda raw, **_kwargs: raw):
                self.assertIsNone(L.lease_context(["python3", "verify"], 10))

    def test_the_scope_past_its_memory_budget_is_refused(self):
        process = subprocess.Popen(["sleep", self.marker], start_new_session=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        with mock.patch.object(L, "memory_budget_bytes", return_value=1), \
                self.assertRaisesRegex(MemoryError, "verification-memory-budget-exceeded"):
            L.wait_within_budget(process, 30, "unused-scope")
        self.assertIsNone(process.poll())  # the caller, not the check, ends the scope

    def test_the_budget_is_a_quarter_of_a_small_host(self):
        with mock.patch.object(L.os, "sysconf", side_effect=lambda name: {
                "SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": (8 * 1024 ** 3) // 4096}[name]):
            self.assertEqual(L.memory_budget_bytes(), 2 * 1024 ** 3)
        self.assertLessEqual(L.memory_budget_bytes(), 16 * 1024 ** 3)


if __name__ == "__main__":
    unittest.main()
