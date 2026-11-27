#!/usr/bin/env python3
"""A non-Git worker always has one writable temp folder, and only that one.

Regression (2026-11-27): in a folder that is not a Git repository every staged
worker failed with EROFS. The non-Git Codex profile is read-only with exact
write roots and had no temp folder, so `/tmp` was read-only for the Codex
worker and for every child it started: a child Codex wrapper's `mktemp`
(reported as `invalid-dispatch-capability`), a Claude child's
`/tmp/claude-<uid>`, an OpenCode child's temp files. A Git worker writes
`/tmp` through Codex's workspace profile and keeps its inherited temp.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
sys.path.insert(0, str(ROOT / "tools"))
import codex_permission_profile as profiles  # noqa: E402
import dispatch_contract  # noqa: E402
import dispatch_headless_main as DISPATCH_MAIN  # noqa: E402
import dispatch_wrapper_common as WRAPPER_COMMON  # noqa: E402
from execution_access import adapter_default_roots  # noqa: E402
import fixture_processes  # noqa: E402

HARNESSES = ("claude", "codex", "opencode")
NAME = f"hearting-{os.getuid()}"
TEMP_KEYS = ("TMPDIR", "TMP", "TEMP", "CLAUDE_CODE_TMPDIR")


def load_wrapper(harness):
    spec = importlib.util.spec_from_file_location(
        f"{harness}_worker_temp_fixture", ROOT / f"adapters/{harness}/bin/dispatch-headless.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    return wrapper


def write_route(path, source, artifact, source_commit="unversioned"):
    """The route fields the acceptance rule reads (a sealed route carries them too)."""
    path.write_text(json.dumps({"cwd": str(source), "source_commit": source_commit,
                                "artifact_root": str(artifact)}), encoding="utf-8")
    return path


class _Boundary:
    """A real non-Git source folder, its artifact root, its route and a fallback base.

    The fallback base stands in for `/tmp`, so no test makes the real
    `/tmp/hearting-<uid>`.
    """

    def make_boundary(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.base / "docs"
        self.artifact = self.source / ".agent_reports"
        self.artifact.mkdir(parents=True)
        self.route = write_route(self.base / "route.json", self.source, self.artifact)
        self.fallback = self.base / "fallback"
        self.fallback.mkdir()
        self.enterContext(mock.patch.object(WRAPPER_COMMON, "WORKER_TEMP_FALLBACK_BASE", self.fallback))

    def prepare(self, tmpdir, **kwargs):
        return WRAPPER_COMMON.prepare_worker_temp_dir(
            {"TMPDIR": str(tmpdir)}, source_root=str(self.source), route_file=str(self.route), **kwargs)


class PrepareWorkerTempDirTest(_Boundary, unittest.TestCase):
    def setUp(self):
        self.make_boundary()

    def test_creates_one_private_folder_under_the_inherited_temp(self):
        path = self.prepare(self.base)
        self.assertEqual(path, str(self.base / NAME))
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o700)

    def test_nested_launch_reuses_the_inherited_worker_folder(self):
        outer = self.prepare(self.base)
        self.assertEqual(self.prepare(outer), outer)
        self.assertFalse((Path(outer) / NAME).exists())

    def test_temp_inside_the_source_or_artifact_moves_outside(self):
        # The non-Git profile never grants a folder inside the source unless the
        # artifact root holds it, and a temp folder is never put in the artifacts.
        for inherited in (self.source / "tmp", self.source, self.source / "tmp" / NAME,
                          self.artifact / "tmp", self.artifact):
            with self.subTest(inherited=str(inherited)):
                self.assertEqual(self.prepare(inherited), str(self.fallback / NAME))
        self.assertEqual(sorted(path.name for path in self.source.rglob("*")), [".agent_reports"])
        self.assertEqual(profiles.recordish_write_root(
            self.fallback / NAME, self.source.resolve(), self.artifact.resolve()),
            str((self.fallback / NAME).resolve()))

    def test_no_place_outside_the_boundary_keeps_the_inherited_temp(self):
        with mock.patch.object(WRAPPER_COMMON, "WORKER_TEMP_FALLBACK_BASE", self.source / "fallback"):
            self.assertEqual(self.prepare(self.source / "tmp"), "")
        self.assertEqual(sorted(path.name for path in self.source.rglob("*")), [".agent_reports"])

    def test_existing_loose_folder_is_refused_and_left_unchanged(self):
        (self.base / NAME).mkdir()
        os.chmod(self.base / NAME, 0o755)
        self.assertEqual(self.prepare(self.base), "")
        self.assertEqual(stat.S_IMODE(os.lstat(self.base / NAME).st_mode), 0o755)

    def test_read_only_folder_is_refused(self):
        # A 0700 folder of this uid inside a read-only mount: access(W_OK) fails
        # with EROFS there, which an unprivileged fixture can only stand in for.
        target = self.base / NAME
        target.mkdir(mode=0o700)
        real_access = os.access

        def read_only_mount(path, mode, *args, **kwargs):
            if Path(path) == target and mode & os.W_OK:
                return False
            return real_access(path, mode, *args, **kwargs)

        with mock.patch.object(WRAPPER_COMMON.os, "access", side_effect=read_only_mount):
            self.assertEqual(self.prepare(self.base), "")
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o700)

    def test_writable_child_of_a_read_only_parent_is_taken(self):
        if os.geteuid() == 0:
            self.skipTest("root writes a read-only parent anyway")
        parent = self.base / "read-only-parent"
        (parent / NAME).mkdir(parents=True, mode=0o700)
        os.chmod(parent / NAME, 0o700)
        os.chmod(parent, 0o500)
        self.addCleanup(os.chmod, parent, 0o700)
        self.assertEqual(self.prepare(parent), str(parent / NAME))

    def test_new_folder_passes_the_same_check(self):
        previous = os.umask(0o277)
        try:
            self.assertEqual(self.prepare(self.base), "")
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(os.lstat(self.base / NAME).st_mode), 0o500)

    def test_symlink_file_or_foreign_folder_is_never_adopted(self):
        target = self.base / "elsewhere"
        target.mkdir(mode=0o700)
        (self.base / NAME).symlink_to(target)
        self.assertEqual(self.prepare(self.base), "")
        (self.base / NAME).unlink()
        (self.base / NAME).write_text("file")
        self.assertEqual(self.prepare(self.base), "")
        (self.base / NAME).unlink()
        (self.base / NAME).mkdir(mode=0o700)
        foreign = os.lstat(self.base / NAME)
        # A network share that shows the folder under another uid.
        with mock.patch.object(WRAPPER_COMMON.os, "lstat",
                               return_value=os.stat_result((foreign.st_mode, *foreign[1:4], os.getuid() + 1, *foreign[5:]))):
            self.assertEqual(self.prepare(self.base), "")

    def test_unusable_base_keeps_the_inherited_temp(self):
        self.assertEqual(self.prepare(self.base / "missing"), "")
        self.assertEqual(WRAPPER_COMMON.worker_temp_environment(""), {})

    def test_git_route_keeps_the_inherited_temp(self):
        write_route(self.route, self.source, self.artifact, source_commit="0" * 40)
        self.assertEqual(self.prepare(self.base), "")
        self.assertFalse((self.base / NAME).exists())

    def test_environment_names_the_same_folder(self):
        path = self.prepare(self.base)
        self.assertEqual(WRAPPER_COMMON.worker_temp_environment(path), {key: path for key in TEMP_KEYS})


class RecordedWorkerTempTest(_Boundary, unittest.TestCase):
    """Recovery reuses only the value the start sealed in its launch record."""

    def setUp(self):
        self.make_boundary()
        # Two folders of the same name, both acceptable: only the record tells them apart.
        for name in ("z-launcher-temp", "a-output"):
            (self.base / name).mkdir()
        self.recorded = self.prepare(self.base / "z-launcher-temp")
        self.same_name = self.prepare(self.base / "a-output")
        self.assertTrue(self.recorded and self.same_name)
        self.assertEqual(Path(self.recorded).name, Path(self.same_name).name)

    def recover(self, record, roots, route=None):
        return WRAPPER_COMMON.recorded_worker_temp_dir(
            record, roots, source_root=str(self.source), route_file=str(route or self.route))

    def test_recovery_takes_the_recorded_value_in_either_order(self):
        record = {"worker_temp_dir": self.recorded}
        for roots in ([str(self.artifact), self.same_name, self.recorded],
                      [str(self.artifact), self.recorded, self.same_name]):
            with self.subTest(roots=roots):
                self.assertEqual(self.recover(record, roots), self.recorded)

    def test_older_record_without_the_value_keeps_the_inherited_temp(self):
        self.assertEqual(self.recover({}, [str(self.artifact), self.recorded]), "")

    def test_git_record_keeps_the_inherited_temp(self):
        git_route = write_route(self.base / "git-route.json", self.source, self.artifact,
                                source_commit="0" * 40)
        self.assertEqual(self.recover({"worker_temp_dir": self.recorded},
                                      [str(self.artifact), self.recorded], git_route), "")

    def test_value_outside_the_recorded_grant_is_not_used(self):
        self.assertEqual(self.recover({"worker_temp_dir": self.recorded},
                                      [str(self.artifact), self.same_name]), "")

    def test_recovery_check_makes_and_changes_nothing(self):
        missing = self.base / "gone" / NAME
        self.assertEqual(self.recover({"worker_temp_dir": str(missing)},
                                      [str(self.artifact), str(missing)]), "")
        self.assertFalse(missing.parent.exists())
        os.chmod(self.recorded, 0o755)
        self.assertEqual(self.recover({"worker_temp_dir": self.recorded},
                                      [str(self.artifact), self.recorded]), "")
        self.assertEqual(stat.S_IMODE(os.lstat(self.recorded).st_mode), 0o755)


class LaunchBindingTest(unittest.TestCase):
    """The shared launch path gives all three harnesses the same folder."""

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {"TMPDIR": str(self.base)}))
        self.worktree = self.base / "docs"
        (self.worktree / ".agent_reports").mkdir(parents=True)
        self.route = write_route(self.base / "route.json", self.worktree, self.worktree / ".agent_reports")
        self.fallback = self.base / "fallback"
        self.fallback.mkdir()
        self.enterContext(mock.patch.object(WRAPPER_COMMON, "WORKER_TEMP_FALLBACK_BASE", self.fallback))

    def bind(self, wrapper, kind):
        args = SimpleNamespace(worktree=str(self.worktree), route_file=str(self.route))
        with mock.patch.object(dispatch_contract, "launch_repository_identity",
                               return_value=(str(self.worktree), kind)):
            self.assertIsNone(DISPATCH_MAIN.bind_repository(wrapper, args))
        return args

    def worker_env(self, wrapper, harness, args):
        args.__dict__.update(
            review_output_binding=None, parent_slug="", parent_session_id="", dispatch_depth=2,
            execution_surface="registered-headless", registered_worker=True,
            fallback_hop="same-harness-headless", intensity="quick", capability_mode="dev",
            slug="fixture", attempt_id="att-fixture", parent_attempt_id="", worker_type="stage",
            assigned_contract="fixture", capability_owner="", owner_harness="",
            artifact_root=str(self.worktree / ".agent_reports"), runtime_dir="", raw_data_dir="",
            report_bundle_root=None, route_id="", owner_route_binding=None, route_node="refs",
            agent_home=ROOT)
        return DISPATCH_MAIN.worker_environment(
            wrapper, args, harness=harness, jobs=self.base / "jobs.log",
            governor_root=self.base / "governor", reservation_token="", permission_env={},
            current_sandbox="fixture", supervised=False)

    def test_non_git_launch_gives_every_harness_one_private_temp(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                wrapper = load_wrapper(harness)
                args = self.bind(wrapper, "non-git")
                self.assertEqual(args.worker_temp_dir, str(self.base / NAME))
                env = self.worker_env(wrapper, harness, args)
                for key in TEMP_KEYS:
                    self.assertEqual(env[key], args.worker_temp_dir, key)
                self.assertIn(Path(args.worker_temp_dir), adapter_default_roots(args))

    def test_temp_inside_the_source_is_replaced_by_one_outside_value(self):
        inside = self.worktree / "tmp"
        with mock.patch.dict(os.environ, {"TMPDIR": str(inside)}):
            for harness in HARNESSES:
                with self.subTest(harness=harness):
                    wrapper = load_wrapper(harness)
                    args = self.bind(wrapper, "non-git")
                    self.assertEqual(args.worker_temp_dir, str(self.fallback / NAME))
                    env = self.worker_env(wrapper, harness, args)
                    for key in TEMP_KEYS:
                        self.assertEqual(env[key], args.worker_temp_dir, key)
                    self.assertIn(Path(args.worker_temp_dir), adapter_default_roots(args))
        self.assertFalse(inside.exists())

    def test_git_launch_keeps_its_inherited_temp(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                wrapper = load_wrapper(harness)
                args = self.bind(wrapper, "git")
                self.assertEqual(args.worker_temp_dir, "")
                env = self.worker_env(wrapper, harness, args)
                self.assertEqual(env["TMPDIR"], str(self.base))
                self.assertNotIn("CLAUDE_CODE_TMPDIR", env)
        self.assertFalse((self.base / NAME).exists())


class _CodexFixture:
    @classmethod
    def setUpClass(cls):
        cls.wrapper = load_wrapper("codex")

    def make_fixture(self, base):
        self.base = base
        self.cwd = self.base / "docs"
        (self.cwd / ".agent_reports").mkdir(parents=True)
        (self.base / "runtime").mkdir()
        (self.base / "state" / "heartbeats").mkdir(parents=True)
        self.route = write_route(self.base / "route.json", self.cwd, self.cwd / ".agent_reports")
        self.temp = self.prepare_temp(self.base)

    def prepare_temp(self, tmpdir):
        return WRAPPER_COMMON.prepare_worker_temp_dir(
            {"TMPDIR": str(tmpdir)}, source_root=str(self.cwd), route_file=str(self.route))

    def args(self, kind, **extra):
        values = dict(
            worktree=str(self.cwd), artifact_root=str(self.cwd / ".agent_reports"),
            runtime_dir=str(self.base / "runtime"), nested_headless_network=False,
            agent_home=ROOT, dispatch_depth=1, route_id="rt-1", route_hash="sha256:1",
            attempt_id="att-1", command_attempt_id="att-1", jobs_path=self.base / "state/jobs.log",
            sandbox="workspace-write", repository_kind=kind, route_file=str(self.route),
            route_node="refs", resolved_model_settings={"source": "inherit", "context_window": "800000"},
            approval="inherit", worker_temp_dir=self.temp if kind == "non-git" else "")
        values.update(extra)
        return SimpleNamespace(**values)

    def command(self, args):
        with mock.patch.object(dispatch_contract, "launch_repository_identity",
                               return_value=(str(self.cwd), args.repository_kind)), \
             mock.patch.object(profiles, "named_profiles_available", return_value=True):
            return self.wrapper.shell_command(args, self.base / "prompt", self.base / "log.jsonl")

    @staticmethod
    def profile_filesystem(command):
        argv = shlex.split(command.split(" < ")[0])
        for index, value in enumerate(argv):
            if value == "-c" and argv[index + 1].startswith(
                    "permissions." + profiles.RECORDISH_PROFILE_NAME + "="):
                import tomllib
                body = tomllib.loads("p=" + argv[index + 1].split("=", 1)[1])["p"]
                return body["extends"], body["filesystem"]
        raise AssertionError("no recordish profile in " + command)


class CodexGrantTest(_CodexFixture, unittest.TestCase):
    """Only the Codex non-Git profile changes: it gains exactly the temp folder."""

    def setUp(self):
        self.make_fixture(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def test_non_git_exec_profile_grants_only_the_worker_temp(self):
        command = self.command(self.args("non-git"))
        self.assertIn(f"--add-dir {self.temp}", command)
        extends, filesystem = self.profile_filesystem(command)
        self.assertEqual(extends, ":read-only")
        self.assertEqual(filesystem[self.temp], "write")
        self.assertNotIn("/tmp", filesystem)
        self.assertNotIn(str(self.base), filesystem)

    def test_non_git_app_server_passes_the_worker_temp_as_a_write_root(self):
        command = self.command(self.args("non-git", resolved_completion_delivery="app-server-supervised"))
        self.assertIn(f"--writable-root {self.temp}", command)

    def test_temp_inside_the_source_is_granted_outside_with_one_value(self):
        # TMPDIR inside the source: the profile would drop that folder, so the
        # launch takes the outside fallback, and the four variables, exec's
        # --add-dir, the profile, App Server's --writable-root and the write
        # record all carry that one value.
        fallback = self.base / "fallback"
        fallback.mkdir()
        with mock.patch.object(WRAPPER_COMMON, "WORKER_TEMP_FALLBACK_BASE", fallback):
            temp = self.prepare_temp(self.cwd / "tmp")
        self.assertEqual(temp, str(fallback / NAME))
        args = self.args("non-git", worker_temp_dir=temp)
        exec_command = self.command(args)
        self.assertIn(f"--add-dir {temp}", exec_command)
        _extends, filesystem = self.profile_filesystem(exec_command)
        self.assertEqual(filesystem[temp], "write")
        self.assertFalse(any(path.startswith(str(self.cwd / "tmp")) for path in filesystem))
        app_server = self.command(self.args("non-git", worker_temp_dir=temp,
                                            resolved_completion_delivery="app-server-supervised"))
        self.assertIn(f"--writable-root {temp}", app_server)
        self.assertEqual(set(WRAPPER_COMMON.worker_temp_environment(temp).values()), {temp})
        self.assertIn(Path(temp), adapter_default_roots(args))
        self.assertFalse((self.cwd / "tmp").exists())

    def test_git_command_is_unchanged(self):
        for extra in ({}, {"resolved_completion_delivery": "app-server-supervised"}):
            with self.subTest(**extra):
                command = self.command(self.args("git", **extra))
                self.assertNotIn(NAME, command)


def require_native_codex(testcase):
    if sys.platform != "linux" or not shutil.which("codex"):
        testcase.skipTest("Codex CLI is unavailable")
    if os.environ.get("CODEX_PERMISSION_PROFILE") or b"codex-linux-sandbox" in Path("/proc/1/cmdline").read_bytes():
        testcase.skipTest("native Codex sandbox tests cannot nest inside an active Codex sandbox")
    if not profiles.named_profiles_available():
        testcase.skipTest("Codex CLI does not support named permission profiles")


class NativeNonGitTempTest(_CodexFixture, unittest.TestCase):
    """The real Codex OS sandbox with the profile the wrapper builds (no model call)."""

    def setUp(self):
        require_native_codex(self)
        # bubblewrap mounts a fresh /dev, so a /dev/shm fixture is invisible in the
        # sandbox; the non-Git profile never writes /tmp, so a /tmp fixture still
        # proves that only the granted folders are writable. A checkout on a
        # uid-mapping network share fails the owner check by design.
        self.make_fixture(Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="hearting-wt-", dir="/tmp"))))
        self.assertTrue(self.temp, "the fixture temp folder was not adopted")
        self.addCleanup(fixture_processes.reap, str(self.base))
        # Codex itself keeps lock folders in its own TMPDIR; keep them in the fixture.
        self.codex_tmp = self.base / "codex-tmp"
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.base / "home"),
                    "CODEX_HOME": str(self.base / "codex-home"), "TMPDIR": str(self.codex_tmp)}
        for key in ("HOME", "CODEX_HOME", "TMPDIR"):
            Path(self.env[key]).mkdir()

    def sandbox(self, script, *, temp):
        command = self.command(self.args("non-git", worker_temp_dir=temp))
        argv = shlex.split(command.split(" < ")[0])
        config = []
        for index, value in enumerate(argv):
            if value == "-c" and argv[index + 1].startswith(("default_permissions=", "permissions.")):
                config += ["-c", argv[index + 1]]
        env = {**self.env, **WRAPPER_COMMON.worker_temp_environment(temp)}
        # Without a worker temp the command sees no temp variable, as before the fix.
        inner = ["sh", "-c", script] if temp else [
            "env", *(f"-u{key}" for key in TEMP_KEYS), "sh", "-c", script]
        return subprocess.run(
            ["codex", "sandbox", "-P", profiles.RECORDISH_PROFILE_NAME, *config, "-C", str(self.cwd),
             "--", *inner], env=env, cwd=self.cwd, text=True, capture_output=True, timeout=60)

    PROBES = {
        # A child Codex wrapper's capability check (capability-map.sh mktemp).
        "codex-wrapper-preflight": f"{shlex.quote(str(ROOT))}/adapters/codex/bin/preflight.sh capability-info autopilot-design >/dev/null",
        # A Claude child's per-uid temp folder.
        "claude-bash-temp": f'd="${{CLAUDE_CODE_TMPDIR:-/tmp}}/claude-{os.getuid()}/probe-$$" && mkdir -p "$d" && rmdir "$d"',
        # OpenCode (Bun os.tmpdir()) and generic tools.
        "opencode-temp": 'd=$(mktemp -d) && rmdir "$d"',
        "python-tempfile": 'python3 -c "import tempfile; tempfile.TemporaryFile().close()"',
    }

    def test_non_git_workers_can_write_temp_files(self):
        for name, script in self.PROBES.items():
            with self.subTest(probe=name):
                result = self.sandbox(script, temp=self.temp)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_without_the_worker_temp_the_same_probes_fail_read_only(self):
        result = self.sandbox(self.PROBES["opencode-temp"], temp="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Read-only file system", result.stderr)

    def test_slash_tmp_and_the_source_folder_stay_read_only(self):
        probe = f"/tmp/{NAME}-erofs-probe-{os.getpid()}"
        self.addCleanup(lambda: os.path.lexists(probe) and os.unlink(probe))
        result = self.sandbox(f": > {shlex.quote(probe)}", temp=self.temp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Read-only file system", result.stderr)
        self.assertFalse(os.path.lexists(probe))
        result = self.sandbox(f": > {shlex.quote(str(self.cwd / 'source.txt'))}", temp=self.temp)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.cwd / "source.txt").exists())


if __name__ == "__main__":
    unittest.main()
