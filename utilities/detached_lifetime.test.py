#!/usr/bin/env python3
"""A detached helper a test starts ends with that test (utilities/detached_lifetime.py).

Each case runs a small fake test file (``*.test.py``) as its own process. The fake test
starts a helper through a real detaching spawn site, ``resource-observers.ensure_watch``
(the one ``compute-hosts watch-run`` goes through), then ends without any cleanup. On a
tree without the lifetime record the helper survives the fake test; that is the leak
these cases pin. Every process a case learns about was started by this file's own
processes; tearDown kills any that is still the same process.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

UTILITIES = Path(__file__).resolve().parent
sys.path.insert(0, str(UTILITIES))

import detached_lifetime as lifetime  # noqa: E402

WAIT_S = 20.0

# argv: token, seconds. A token ending in "-chain" makes this helper start a second one
# once its own spawner is gone (CHAIN below), as a runner starting its observer does.
SLEEPER = textwrap.dedent("""\
    import importlib.util, os, sys, time
    if sys.argv[1].endswith("-chain"):
        utilities, out = os.environ["DL_UTILITIES"], os.environ["DL_OUT"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                parent = open("/proc/%d/cmdline" % os.getppid(), "rb").read()
            except OSError:
                parent = b""
            if b"ensure_watch" not in parent:
                break
            time.sleep(0.02)
        sys.path.insert(0, utilities)
        spec = importlib.util.spec_from_file_location(
            "resource_observers", os.path.join(utilities, "resource-observers.py"))
        observers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(observers)
        token = sys.argv[1][:-len("-chain")] + "-second"
        pid = observers.ensure_watch([sys.executable, sys.argv[0], token, "600"],
                                     os.path.join(os.path.dirname(out), token + ".lock"))["pid"]
        ticks = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[19]
        with open(out + ".second", "w") as fh:
            fh.write("%d %s" % (pid, ticks))
    time.sleep(float(sys.argv[2]))
""")

# argv: utilities, sleeper, token, out-file [, mode]
FAKE_TEST = textwrap.dedent("""\
    import importlib.util, os, signal, subprocess, sys, time
    utilities, sleeper, token, out = sys.argv[1:5]
    mode = sys.argv[5] if len(sys.argv) > 5 else ""
    sys.path.insert(0, utilities)
    spec = importlib.util.spec_from_file_location(
        "resource_observers", os.path.join(utilities, "resource-observers.py"))
    observers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observers)

    def lock(name):
        return os.path.join(os.path.dirname(out), name + ".lock")

    def ticks(pid):
        return open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[19]

    def start_here(name):
        return observers.ensure_watch([sys.executable, sleeper, name, "600"], lock(name))["pid"]

    def start_from_a_cli(name):
        # A child that starts the helper and exits at once, as a CLI the test runs would.
        code = ("import importlib.util, os, sys; sys.path.insert(0, sys.argv[1]); "
                "spec = importlib.util.spec_from_file_location('ro', os.path.join(sys.argv[1], "
                "'resource-observers.py')); m = importlib.util.module_from_spec(spec); "
                "spec.loader.exec_module(m); print(m.ensure_watch([sys.executable, sys.argv[2], "
                "sys.argv[3], '600'], sys.argv[4])['pid'])")
        env = dict(os.environ, DL_UTILITIES=utilities, DL_OUT=out)
        return int(subprocess.run([sys.executable, "-c", code, utilities, sleeper, name, lock(name)],
                                  capture_output=True, text=True, check=True, env=env).stdout)

    if mode in ("spawner-exits", "chain"):
        pid = start_from_a_cli(token + ("-chain" if mode == "chain" else "-cli"))
    elif mode == "fork-worker":
        # A forked worker of the test (same command line) starts the helper and exits.
        read_end, write_end = os.pipe()
        worker = os.fork()
        if worker == 0:
            os.close(read_end)
            os.write(write_end, str(start_here(token + "-forked")).encode())
            os._exit(0)
        os.close(write_end)
        pid = int(os.read(read_end, 64))
        os.waitpid(worker, 0)
    else:
        pid = start_here(token + "-helper")
    with open(out, "w") as fh:
        fh.write("%d %s" % (pid, ticks(pid)))
    if mode in ("spawner-exits", "fork-worker"):
        time.sleep(0.5)
        with open(out + ".alive", "w") as fh:
            fh.write("1" if os.path.exists("/proc/%d" % pid) else "0")
    if mode == "chain":
        deadline = time.monotonic() + 20
        while not os.path.exists(out + ".second") and time.monotonic() < deadline:
            time.sleep(0.05)
    if mode == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
""")

# Kill this fake test's own reaper, then end: its helper is left with no reaper.
KILL_OWN_REAPER = textwrap.dedent("""\
    import detached_lifetime
    record = detached_lifetime._record_dir(create=False)
    mine = detached_lifetime._name(detached_lifetime.test_anchor())
    deadline = time.monotonic() + 20
    reaper = None
    while reaper is None and time.monotonic() < deadline:
        for name in os.listdir("/proc"):
            argv = detached_lifetime._argv(int(name)) if name.isdigit() else []
            if argv[-3:] == ["--reap", record, mine]:
                reaper = int(name)
        time.sleep(0.02)
    os.kill(reaper, signal.SIGKILL)
    while os.path.exists("/proc/%d" % reaper) and time.monotonic() < deadline:
        time.sleep(0.02)
""")

# Not a test, and started outside this test's ancestry: it starts a helper and exits.
OUTSIDE = textwrap.dedent("""\
    import importlib.util, os, sys, time
    utilities, sleeper, token, out = sys.argv[1:5]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            parent = open("/proc/%d/cmdline" % os.getppid(), "rb").read()
        except OSError:
            parent = b""
        if b"posix_spawn" not in parent:
            break
        time.sleep(0.02)
    sys.path.insert(0, utilities)
    import detached_lifetime
    spec = importlib.util.spec_from_file_location(
        "resource_observers", os.path.join(utilities, "resource-observers.py"))
    observers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(observers)
    anchor = detached_lifetime.test_anchor()
    pid = observers.ensure_watch([sys.executable, sleeper, token, "600"],
                                 os.path.join(os.path.dirname(out), token + ".lock"))["pid"]
    ticks = open("/proc/%d/stat" % pid).read().rsplit(")", 1)[1].split()[19]
    with open(out, "w") as fh:
        fh.write("%d %s %s" % (pid, ticks, "none" if anchor is None else "anchor"))
""")


def identity(pid: int) -> tuple[int, str] | None:
    info = lifetime._stat(pid)
    return None if info is None or info[0] == "Z" else (pid, info[3])


class DetachedLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="detached-lifetime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sleeper = self.root / "sleeper.py"
        self.sleeper.write_text(SLEEPER)
        # A tree that carries the module, as a checkout, worktree or release does: the
        # fake tests live in it, so they are Hearting tests.
        self.tree = self.root / "tree" / "utilities"
        self.tree.mkdir(parents=True)
        (self.tree / "detached_lifetime.py").symlink_to(UTILITIES / "detached_lifetime.py")
        self.token = "dl-" + uuid.uuid4().hex[:12]
        self.started: list[tuple[int, str]] = []
        self.addCleanup(self.stop_started)

    def stop_started(self):
        for pid, ticks in self.started:
            if identity(pid) == (pid, ticks):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def remember(self, path: Path) -> tuple[int, str]:
        pid, ticks = path.read_text().split()[:2]
        process = (int(pid), ticks)
        self.started.append(process)
        return process

    def wait_until(self, predicate, what: str) -> None:
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(what)

    def gone(self, process: tuple[int, str]) -> bool:
        return identity(process[0]) != process

    def run_fake_test(self, mode: str = "", *, name: str = "leak.test.py", extra: str = "",
                      directory: Path | None = None, utilities: Path = UTILITIES) -> Path:
        script = (self.tree if directory is None else directory) / name
        script.write_text(FAKE_TEST + extra)
        out = self.root / (name + ".pid")
        result = subprocess.run(
            [sys.executable, str(script), str(utilities), str(self.sleeper), self.token, str(out),
             mode], capture_output=True, text=True, timeout=120, cwd=str(self.root))
        for path in (out, Path(str(out) + ".second")):
            if path.exists() and len(path.read_text().split()) >= 2:
                self.remember(path)  # stopped in tearDown even if this case fails below
        if mode != "sigkill":
            self.assertEqual(result.returncode, 0, result.stderr)
        return out

    # --- which process is the test ---

    def test_a_hearting_test_is_a_test_shape_located_in_a_hearting_tree(self):
        tree = str(UTILITIES.parent)
        outside = str(self.root)
        shapes = [
            ["python3", "utilities/x.test.py"], ["/usr/bin/python3", "-u", "utilities/x.test.py"],
            ["python3", "-X", "dev", "tools/fleet/tests/test_foo.py"],
            ["python3.12", "-W", "error", "utilities/foo_test.py"], ["bash", "hooks/x.test.sh"],
            ["bash", "-o", "pipefail", "hooks/x.test.sh"], ["node", "adapters/x.test.mjs"],
        ]
        runners = [
            ["python3", "-m", "unittest", "-v"], ["python3", "-munittest"],
            ["python3", "-m", "pytest"], ["pytest", "-q"],
            ["/usr/bin/python3", "/home/u/.local/bin/pytest", "-q"],  # pytest via its shebang
            ["node", "--test", "a.mjs"], ["node", "--test"],
        ]
        not_tests = [
            ["python3", "-c", "print('x.test.py')"], ["python3", "-uc", "code", "x.test.py"],
            ["python3", "utilities/peer-steward.py", "watch"],
            ["python3", "tools/run-tests.py", "--select", "x.test.py"],
            ["python3", "-m", "unittest.mock"], ["python3", "-m", "pytest_cov"],
            ["bash", "-c", "python3 x.test.py"], ["zsh", "-ic", "x.test.sh"],
            ["node", "-e", "x.test.js"], ["node", "/srv/app.js", "--test"],
            ["node", "--require", "r.test.js", "app.js"],
            ["claude", "-p", "run utilities/x.test.py"], ["x_test.py"],
            ["vim", "x.test.py"], ["git", "diff", "x.test.py"], ["bash"], ["python3"], [],
        ]
        for argv in shapes + runners:
            self.assertTrue(lifetime.is_test_command(argv, tree), argv)
            self.assertFalse(lifetime.is_test_command(argv, outside), argv)
        for argv in not_tests:
            self.assertFalse(lifetime.is_test_command(argv, tree), argv)
        # A user's test-shaped driver outside any Hearting tree is not a Hearting test,
        # wherever it runs from.
        for name in ("run_test.py", "test_runner.py", "speed_test.py", "eval.test.py"):
            driver = str(self.root / "project" / name)
            self.assertFalse(lifetime.is_test_command(["python3", driver, "--days", "3"], tree))
            self.assertFalse(lifetime.is_test_command(["python3", name], outside))
        self.assertFalse(lifetime.is_test_command(["python3", "utilities/x.test.py"], None))
        # An absolute test file path in the tree counts from any working directory.
        self.assertTrue(lifetime.is_test_command([sys.executable, str(UTILITIES / "x.test.py")],
                                                 outside))

    def test_bind_ignores_what_is_not_a_child_of_the_caller(self):
        fake = mock.Mock(pid=mock.Mock())
        self.assertIs(lifetime.bind(fake), fake)
        parent = os.getppid()
        self.assertEqual(lifetime.bind(parent), parent)
        record = lifetime._record_dir(create=False)
        if record is not None:
            self.assertFalse(any(name.startswith(f"{parent}-") for name in os.listdir(record)))

    # --- the leak cases (each fails on a tree without the record) ---

    def test_a_helper_started_by_a_direct_test_run_ends_when_the_test_ends(self):
        helper = self.remember(self.run_fake_test())
        self.wait_until(lambda: self.gone(helper),
                        f"helper {helper[0]} outlived the test that started it")

    def test_a_helper_ends_with_a_test_killed_before_any_cleanup(self):
        helper = self.remember(self.run_fake_test("sigkill"))
        self.wait_until(lambda: self.gone(helper),
                        f"helper {helper[0]} outlived the SIGKILLed test that started it")

    def test_a_helper_whose_spawner_exits_lives_until_the_test_ends(self):
        out = self.run_fake_test("spawner-exits")
        helper = self.remember(out)
        self.assertEqual(Path(str(out) + ".alive").read_text(), "1",
                         "the helper must keep running while its test runs")
        self.wait_until(lambda: self.gone(helper),
                        f"helper {helper[0]} outlived the test that started it")

    def test_a_helper_started_by_a_forked_worker_of_the_test_lives_until_the_test_ends(self):
        out = self.run_fake_test("fork-worker")
        helper = self.remember(out)
        self.assertEqual(Path(str(out) + ".alive").read_text(), "1",
                         "a fork of the test is not the test: its exit must not end the helper")
        self.wait_until(lambda: self.gone(helper),
                        f"helper {helper[0]} outlived the test that started it")

    def test_a_helper_started_by_a_recorded_helper_ends_with_the_same_test(self):
        out = self.run_fake_test("chain")
        first = self.remember(out)
        second = self.remember(Path(str(out) + ".second"))
        self.wait_until(lambda: self.gone(first) and self.gone(second),
                        f"helpers {first[0]} and {second[0]} outlived their test")

    def test_the_next_reaper_finishes_a_test_whose_own_reaper_was_killed(self):
        abandoned = self.remember(self.run_fake_test(name="first.test.py", extra=KILL_OWN_REAPER))
        time.sleep(1.0)
        self.assertFalse(self.gone(abandoned), "with its reaper killed, nothing ended the helper yet")
        self.token += "-next"
        self.remember(self.run_fake_test(name="second.test.py"))
        self.wait_until(lambda: self.gone(abandoned),
                        f"the next reaper did not finish the abandoned helper {abandoned[0]}")

    # --- production lifetimes do not change ---

    def test_a_test_shaped_driver_outside_any_hearting_tree_does_not_end_its_work(self):
        project = self.root / "project"
        project.mkdir()
        # Not a Hearting test: the job it starts must outlive it (RV-BC-1293).
        out = self.run_fake_test(name="run_test.py", directory=project)
        helper = self.remember(out)
        time.sleep(1.0)
        self.assertFalse(self.gone(helper), "a user's run_test.py is not a Hearting test")

    def test_a_spawn_site_in_a_partial_tree_without_the_module_still_imports(self):
        # A fixture release may copy only a few modules (release_follow does). The spawn
        # site's guarded import then gives a no-op instead of an ImportError.
        partial = self.root / "partial" / "utilities"
        partial.mkdir(parents=True)
        (partial / "git_index_lock.py").write_text((UTILITIES / "git_index_lock.py").read_text())
        result = subprocess.run(
            [sys.executable, "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import git_index_lock as g; "
             "print(g.end_with_test.__module__, g.end_with_test(12345))", str(partial)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["git_index_lock", "12345"])

    def test_outside_a_test_a_detached_helper_keeps_its_production_lifetime(self):
        script = self.root / "outside.py"
        script.write_text(OUTSIDE)
        out = self.root / "outside.out"
        # An intermediate starts it in its own session and exits at once, so the
        # process is no descendant of this test when it starts its helper.
        subprocess.run([sys.executable, "-c",
                        "import os, sys; os.posix_spawn(sys.executable, [sys.executable, *sys.argv[1:]],"
                        " {'PATH': os.defpath}, setsid=True)",
                        str(script), str(UTILITIES), str(self.sleeper), self.token, str(out)],
                       check=True, timeout=60)
        self.wait_until(out.exists, "the outside process did not report its helper")
        self.wait_until(lambda: len(out.read_text().split()) == 3, "incomplete report")
        helper = self.remember(out)
        self.assertEqual(out.read_text().split()[2], "none")
        time.sleep(1.0)
        self.assertFalse(self.gone(helper), "a helper started outside any test must keep running")
        record = lifetime._record_dir(create=False)
        if record is not None:
            self.assertNotIn(lifetime._name(helper) + ".child", os.listdir(record))


if __name__ == "__main__":
    unittest.main()
