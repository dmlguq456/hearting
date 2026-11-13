#!/usr/bin/env python3
"""Long-lived helpers hand their work to the installed release, in place.

Two managed releases of one fixture install stand in for an update: a helper
started from release A keeps working while the installer's `current` pointer
moves to B, then continues as the same process on B's code. Nothing here
signals or replaces a process from outside.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import parent_next_directive  # noqa: E402
import peer_obligations  # noqa: E402

_HELPER_MODULES = ("peer_obligations.py", "dispatch_attempt_policy.py", "parent_next_directive.py",
                   "capacity_auto_resume.py")
_UTILITY_IGNORE = shutil.ignore_patterns("*.test.py", "*.test.sh", "__pycache__", "fixtures",
                                         "test-fixtures")

# A helper whose job is one flock-guarded counter: it yields when another
# instance already holds the job, and offers itself to the installed release
# between steps, the way a real watcher does before each wait.
FIXTURE_HELPER = r'''
import fcntl, json, os, sys, time
from pathlib import Path
import peer_obligations

work, lock_path, home = sys.argv[1], sys.argv[2], sys.argv[3]
lock = open(lock_path, "a+")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    with open(work + ".yielded", "a") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "code": __file__}) + "\n")
    raise SystemExit(0)
counter = Path(work)
while True:
    step = int(counter.read_text() or 0) + 1 if counter.exists() else 1
    counter.write_text(str(step))
    with open(work + ".log", "a") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "step": step, "code": __file__, "home": home,
                                 "agent_home": os.environ.get("AGENT_HOME"), "cwd": os.getcwd(),
                                 "at": time.monotonic()}) + "\n")
    peer_obligations.follow_installed_release(__file__, sys.argv[1:])
    time.sleep(0.05)
'''

# The resume a capacity wait runs once: report which code ran it, from where.
FIXTURE_ROUTE_START = r'''
import json, os, sys
from pathlib import Path
parent = os.getppid()
command = Path(f"/proc/{parent}/cmdline").read_bytes().split(b"\0")
with open(os.environ["FIXTURE_RESUME_MARKER"], "a") as handle:
    handle.write(json.dumps({"parent": parent, "parent_script": command[1].decode(),
                             "parent_args": [part.decode() for part in command[2:] if part],
                             "start": __file__, "argv": sys.argv[1:]}) + "\n")
print(json.dumps({"state": "running", "owner_attempt_id": "att-resumed"}))
'''


def proc_start(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return raw[raw.rfind(")") + 2:].split()[19]


def cmdline(pid):
    try:
        return [part.decode() for part in
                Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    except OSError:
        return []


def wait_until(predicate, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


class FakeInstall:
    """One managed install with releases A and B, laid out as the installer does."""

    def __init__(self, base: Path, *, full_utilities=False):
        self.base = base.resolve()
        self.home = self.base / "home"
        self.data = self.base / "data"
        self.releases = self.data / "releases"
        self.state = self.base / "state"
        for path in (self.home, self.releases, self.state):
            path.mkdir(parents=True, exist_ok=True)
        (self.state / "distribution.lock").touch()
        self.a = self._release("A", full_utilities)
        self.b = self._release("B", full_utilities, like=self.a if full_utilities else None)
        self.current = self.data / "current"
        self.point(self.a)

    def _release(self, version, full_utilities, like=None):
        root = self.releases / version
        if full_utilities:
            # The release's own code is a real tree, since its location names the
            # release; every other top-level entry is read from this checkout.
            if like is not None:
                # Same filesystem: link the copied tree instead of reading it again.
                shutil.copytree(like / "utilities", root / "utilities", copy_function=os.link)
            else:
                shutil.copytree(ROOT / "utilities", root / "utilities", ignore=_UTILITY_IGNORE)
            for entry in ROOT.iterdir():
                if entry.name not in {"utilities", "RELEASE_VERSION"} and not entry.name.startswith("."):
                    (root / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
            (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
            return root
        else:
            (root / "utilities").mkdir(parents=True)
            for name in _HELPER_MODULES:
                shutil.copy2(ROOT / "utilities" / name, root / "utilities" / name)
            (root / "utilities/peer-steward.py").write_text("", encoding="utf-8")
            (root / "utilities/fixture-helper.py").write_text(FIXTURE_HELPER, encoding="utf-8")
            (root / "utilities/capability-route.py").write_text(FIXTURE_ROUTE_START, encoding="utf-8")
        (root / "core").mkdir()
        (root / "core/CORE.md").write_text("fixture\n", encoding="utf-8")
        (root / "tools/install").mkdir(parents=True)
        shutil.copy2(ROOT / "tools/install/distribution.py", root / "tools/install/distribution.py")
        (root / "RELEASE_VERSION").write_text(version + "\n", encoding="utf-8")
        (root / "harness-manifest.json").write_text("{}\n", encoding="utf-8")
        return root

    def point(self, target):
        """Swap the pointer atomically, as the installer's commit does."""
        staged = self.data / ".current.next"
        staged.unlink(missing_ok=True)
        staged.symlink_to(target, target_is_directory=True)
        os.replace(staged, self.current)

    def environment(self, code_root, **extra):
        return {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""),
                "HARNESS_DATA_ROOT": str(self.data), "HARNESS_STATE_ROOT": str(self.state),
                "XDG_DATA_HOME": str(self.base / "xdg-data"),
                "XDG_STATE_HOME": str(self.base / "xdg-state"),
                "AGENT_HOME": str(code_root), "PYTHONDONTWRITEBYTECODE": "1", **extra}


class HelperProcesses:
    def start(self, argv, environment, **kwargs):
        process = subprocess.Popen(argv, env=environment, cwd=kwargs.pop("cwd", "/"),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=kwargs.pop("stderr", subprocess.DEVNULL),
                                   start_new_session=True, **kwargs)

        def reap():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        self.addCleanup(reap)
        return process


class FollowInstalledReleaseTest(HelperProcesses, unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.install = FakeInstall(Path(temp.name))
        self.work = self.install.base / "job" / "counter"
        self.work.parent.mkdir()
        self.lock = self.install.base / "job" / "job.lock"

    def helper(self, release, *, cwd="/"):
        return self.start([sys.executable, str(release / "utilities/fixture-helper.py"),
                           str(self.work), str(self.lock), str(self.install.a)],
                          self.install.environment(release), cwd=cwd)

    def steps(self):
        try:
            return [json.loads(line) for line in
                    Path(str(self.work) + ".log").read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def test_old_release_helper_moves_in_place_and_keeps_working(self):
        process = self.helper(self.install.a, cwd=str(self.install.a / "utilities"))
        start = wait_until(lambda: len(self.steps()) >= 3 and proc_start(process.pid))
        self.assertTrue(start)
        self.install.point(self.install.b)
        moved = wait_until(lambda: any(step["code"].startswith(str(self.install.b))
                                       for step in self.steps()[-3:]))
        self.assertTrue(moved, self.steps()[-3:])
        wait_until(lambda: len(self.steps()) >= 12)
        steps = self.steps()
        self.assertEqual(cmdline(process.pid)[1], str(self.install.b / "utilities/fixture-helper.py"))
        self.assertEqual(proc_start(process.pid), start, "the same process, not a successor")
        self.assertEqual({step["pid"] for step in steps}, {process.pid})
        # The work went on: one counter, no step lost or repeated, no long stall.
        self.assertEqual([step["step"] for step in steps], list(range(1, len(steps) + 1)))
        self.assertLess(max(b["at"] - a["at"] for a, b in zip(steps, steps[1:])), 5.0)
        after = [step for step in steps if step["code"].startswith(str(self.install.b))]
        self.assertTrue(after)
        last = after[-1]
        self.assertEqual(last["home"], str(self.install.b), "release paths in arguments follow")
        self.assertEqual(last["agent_home"], str(self.install.b))
        self.assertEqual(last["cwd"], str(self.install.b / "utilities"))
        # The installed release does not move again.
        time.sleep(0.3)
        self.assertEqual(proc_start(process.pid), start)
        self.assertIsNone(process.poll())

    def test_one_job_converges_to_one_instance_on_the_installed_release(self):
        old = self.helper(self.install.a)
        self.assertTrue(wait_until(lambda: len(self.steps()) >= 3))
        self.install.point(self.install.b)
        # Something on the new release starts the same job at the same moment.
        new = self.helper(self.install.b)

        def converged():
            alive = [process for process in (old, new) if process.poll() is None]
            return (len(alive) == 1
                    and cmdline(alive[0].pid)[1:2] == [str(self.install.b / "utilities/fixture-helper.py")]
                    and alive[0])
        survivor = wait_until(converged)
        self.assertTrue(survivor, [(p.pid, p.poll(), cmdline(p.pid)) for p in (old, new)])
        before = len(self.steps())
        self.assertTrue(wait_until(lambda: len(self.steps()) >= before + 5))
        steps = self.steps()
        self.assertEqual([step["step"] for step in steps], list(range(1, len(steps) + 1)))
        self.assertEqual({step["pid"] for step in steps[-5:]}, {survivor.pid})
        # The instance that lost the job said so and ended; it never ran the job twice.
        yielded = Path(str(self.work) + ".yielded").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(yielded), 1)

    def test_an_install_in_flight_is_never_entered(self):
        process = self.helper(self.install.a)
        self.assertTrue(wait_until(lambda: len(self.steps()) >= 2))
        with open(self.install.state / "distribution.lock", "rb") as installer:
            fcntl.flock(installer, fcntl.LOCK_EX)
            self.install.point(self.install.b)
            count = len(self.steps())
            self.assertTrue(wait_until(lambda: len(self.steps()) >= count + 5))
            self.assertTrue(all(step["code"].startswith(str(self.install.a))
                                for step in self.steps()))
            # The install rolls back before committing.
            self.install.point(self.install.a)
        count = len(self.steps())
        self.assertTrue(wait_until(lambda: len(self.steps()) >= count + 5))
        self.assertEqual(cmdline(process.pid)[1], str(self.install.a / "utilities/fixture-helper.py"))
        self.install.point(self.install.b)
        self.assertTrue(wait_until(
            lambda: cmdline(process.pid)[1:2] == [str(self.install.b / "utilities/fixture-helper.py")]))


class FollowDecisionTest(unittest.TestCase):
    """The decision itself, without replacing the test process."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.install = FakeInstall(Path(temp.name))

    def follow(self, code_root, script="utilities/fixture-helper.py", arguments=(), **env):
        environment = self.install.environment(code_root, **env)
        with mock.patch.object(peer_obligations, "__file__",
                               str(code_root / "utilities/peer_obligations.py")), \
             mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(os, "execve") as execve:
            result = peer_obligations.follow_installed_release(code_root / script, arguments)
        return result, execve

    def test_moved_pointer_is_detected_cheaply_and_only_for_managed_releases(self):
        self.assertFalse(parent_next_directive.release_moved(self.install.a))
        self.install.point(self.install.b)
        self.assertTrue(parent_next_directive.release_moved(self.install.a))
        self.assertFalse(parent_next_directive.release_moved(self.install.b))
        self.assertFalse(parent_next_directive.release_moved(ROOT))
        self.assertFalse(parent_next_directive.release_moved(""))

    def test_a_moved_release_execs_the_same_script_renaming_only_the_old_root(self):
        self.install.point(self.install.b)
        # State may live beneath the old tree (a release-local .dispatch); it stays put.
        local_jobs = str(self.install.a / ".dispatch/jobs.log")
        local_record = str(self.install.a / ".dispatch/capacity-resume/rt-x.json")
        result, execve = self.follow(self.install.a, arguments=(
            "--agent-home", str(self.install.a) + "/", "--jobs", local_jobs, "--record",
            local_record, "--other", "/elsewhere/jobs.log", "plain"))
        self.assertTrue(result)
        executable, argv, environment = execve.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(argv, [sys.executable, str(self.install.b / "utilities/fixture-helper.py"),
                                "--agent-home", str(self.install.b), "--jobs", local_jobs,
                                "--record", local_record, "--other", "/elsewhere/jobs.log",
                                "plain"])
        self.assertEqual(environment["AGENT_HOME"], str(self.install.b))

    def test_current_release_checkout_mismatched_home_and_missing_script_stay(self):
        result, execve = self.follow(self.install.a)
        self.assertFalse(result)
        execve.assert_not_called()
        self.install.point(self.install.b)
        (self.install.a / "utilities/only-in-a.py").write_text("", encoding="utf-8")
        for code_root, script, env in (
                (self.install.b, "utilities/fixture-helper.py", {}),
                (self.install.a, "utilities/fixture-helper.py", {"AGENT_HOME": str(self.install.b)}),
                (self.install.a, "utilities/only-in-a.py", {})):
            with self.subTest(code_root=code_root.name, script=script, env=env):
                result, execve = self.follow(code_root, script, **env)
                self.assertFalse(result)
                execve.assert_not_called()
        with mock.patch.object(os, "execve") as execve:
            self.assertFalse(peer_obligations.follow_installed_release(__file__, ()))
        execve.assert_not_called()

    def test_an_install_that_cannot_be_judged_is_not_entered_and_nothing_waits(self):
        self.install.point(self.install.b)
        lock = self.install.state / "distribution.lock"
        for kind in ("missing", "directory", "fifo", "symlink", "held"):
            if lock.is_dir() and not lock.is_symlink():
                lock.rmdir()
            else:
                lock.unlink(missing_ok=True)
            holder = None
            if kind == "directory":
                lock.mkdir()
            elif kind == "fifo":
                os.mkfifo(lock)
            elif kind == "symlink":
                (self.install.state / "elsewhere.lock").touch()
                lock.symlink_to(self.install.state / "elsewhere.lock")
            elif kind == "held":
                lock.touch()
                holder = open(lock, "rb")
                fcntl.flock(holder, fcntl.LOCK_EX)
            outcome = []
            worker = threading.Thread(
                target=lambda: outcome.append(self.follow(self.install.a)), daemon=True)
            worker.start()
            worker.join(timeout=5)
            blocked = worker.is_alive()
            if blocked and kind == "fifo":
                # Give the stuck reader a writer so the thread can end.
                os.close(os.open(lock, os.O_WRONLY | os.O_NONBLOCK))
                worker.join(timeout=5)
            if holder is not None:
                holder.close()
            with self.subTest(kind=kind):
                self.assertFalse(blocked, "deciding whether to move must never wait")
                result, execve = outcome[0]
                self.assertFalse(result)
                execve.assert_not_called()

    def test_failed_exec_restores_working_directory_and_descriptors(self):
        self.install.point(self.install.b)
        here = os.getcwd()
        fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        try:
            os.chdir(self.install.a / "utilities")
            with mock.patch.dict(os.environ, self.install.environment(self.install.a), clear=True), \
                 mock.patch.object(os, "execve", side_effect=OSError("fixture")):
                self.assertFalse(peer_obligations._exec_in_release(
                    self.install.a, self.install.b, ["x"], keep_fds=(fd,)))
                self.assertEqual(Path(os.getcwd()).resolve(), self.install.a / "utilities")
                self.assertFalse(os.get_inheritable(fd))
        finally:
            os.chdir(here)


class RealHelpersFollowTest(HelperProcesses, unittest.TestCase):
    """A capacity wait moves and finishes; an orphan watcher stays with its live attempt.

    Both keep their state beneath the old release (`.dispatch`), as a
    release-local registry does; that state stays where it is.
    """

    def test_capacity_wait_resumes_once_on_the_installed_release_with_its_own_record(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = FakeInstall(Path(temp.name))
        marker = install.base / "resumed.jsonl"
        jobs = install.a / ".dispatch" / "jobs.log"
        jobs.parent.mkdir()
        jobs.write_text("", encoding="utf-8")
        record = jobs.parent / "capacity-resume" / "rt-fixture-0.json"
        record.parent.mkdir()
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3))
        record.write_text(json.dumps({
            "schema": "capacity-resume-v1", "route_id": "rt-fixture", "state": "armed",
            "route_file": str(install.base / "route.json"), "jobs": str(jobs),
            "retry_at": retry_at, "chain": 1, "cause": "answer", "harness": "",
            "source_attempt_id": "", "usage_state": ""}), encoding="utf-8")
        log = open(install.base / "resume.log", "wb")
        self.addCleanup(log.close)
        process = self.start(
            [sys.executable, str(install.a / "utilities/capacity_auto_resume.py"), "run",
             "--record", str(record)],
            install.environment(install.a, FIXTURE_RESUME_MARKER=str(marker)), stderr=log)
        start = wait_until(lambda: proc_start(process.pid))
        install.point(install.b)
        self.assertEqual(process.wait(timeout=30), 0, (install.base / "resume.log").read_text())
        resumed = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(resumed), 1, "the wait resumed its route exactly once")
        self.assertEqual(resumed[0]["parent"], process.pid)
        self.assertEqual(resumed[0]["parent_script"],
                         str(install.b / "utilities/capacity_auto_resume.py"))
        self.assertEqual(resumed[0]["parent_args"], ["run", "--record", str(record)])
        self.assertFalse((install.b / ".dispatch").exists())
        self.assertTrue(start)
        settled = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((settled["state"], settled["owner_attempt_id"]),
                         ("resumed", "att-resumed"))

    def test_orphan_watch_stays_with_its_live_attempt_and_settles_it_on_one_release(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        install = FakeInstall(Path(temp.name), full_utilities=True)
        owner = self.start(["sleep", "120"], install.environment(install.a))
        owner_start = proc_start(owner.pid)
        namespace = Path("/proc/self/ns/pid").readlink()
        jobs = install.a / ".dispatch" / "jobs.log"
        jobs.parent.mkdir()
        jobs.write_text(
            "2026-07-19T00:00:00Z\topen\t/r\t/w\towner\t"
            "attempt_schema_version=2,transport=headless,execution_surface=registered-headless,"
            "registered_worker=1,fallback_hop=same-harness-headless,dispatch_depth=1,"
            "worker_type=owner,attempt_id=att-move,parent_sid=parent-test,"
            "parent_completion_delivery=claude-parent-runtime,"
            f"pid={owner.pid},pid_start={owner_start},pgid={owner.pid},"
            f"pid_observer_ns={namespace},pid_ns={namespace}\n", encoding="utf-8")
        script = install.a / "utilities/dispatch-orphan-watch.py"
        watcher = self.start(
            [sys.executable, str(script), "--jobs", str(jobs), "--agent-home", str(install.a),
             "--attempt-id", "att-move", "--pid", str(owner.pid), "--pid-start", owner_start,
             "--interval", "0.05"],
            install.environment(install.a))
        watcher_start = wait_until(lambda: proc_start(watcher.pid))
        time.sleep(0.5)
        install.point(install.b)
        # Twenty-odd idle waits with B installed: the live attempt keeps its watcher.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            self.assertEqual(cmdline(watcher.pid)[1], str(script))
            time.sleep(0.05)
        self.assertEqual(proc_start(watcher.pid), watcher_start)
        self.assertIn("\topen\t", jobs.read_text(encoding="utf-8"))
        owner.kill()
        owner.wait(timeout=5)
        self.assertEqual(watcher.wait(timeout=30), 0)
        text = jobs.read_text(encoding="utf-8")
        self.assertIn("\tdone\t/r\t/w\towner\t", text)
        self.assertIn("note=dead-exact-pid", text)
        self.assertFalse((install.b / ".dispatch").exists())


if __name__ == "__main__":
    unittest.main()
