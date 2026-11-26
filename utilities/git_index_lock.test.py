#!/usr/bin/env python3
"""Isolated regressions for bounded, read-only Git index-lock diagnosis."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import base64
import importlib.util
import os
from pathlib import Path
import subprocess
import stat
import threading
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).with_name("git_index_lock.py")
SPEC = importlib.util.spec_from_file_location("git_index_lock", MODULE_PATH)
git_index_lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_index_lock)


class GitIndexLockDiagnosisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hearting-lock-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock_fds = []
        self.addCleanup(lambda: [os.close(fd) for fd in self.lock_fds])
        self.repo = self.root / "space and quote ' repository"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q")
        self.git(self.repo, "config", "user.name", "Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-qm", "base")

    def git(self, cwd, *args, env=None, check=True):
        return subprocess.run(["git", "-C", str(cwd), *args], cwd=self.root, env=env,
                              capture_output=True, check=check)

    def stage_change(self, repo=None, *, env=None):
        repo = repo or self.repo
        (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self.git(repo, "add", "tracked.txt", env=env)

    def make_lock(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        fd = os.open(path, os.O_RDONLY)
        self.lock_fds.append(fd)
        path.chmod(0)
        return fd

    def fail_commit(self, repo=None, *, env=None, command=None, cwd=None):
        repo = repo or self.repo
        self.stage_change(repo, env=env)
        index = self.git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        index_lock = Path(index + ".lock")
        lock_fd = self.make_lock(index_lock, b"")
        index_before = Path(index).read_bytes() if Path(index).exists() else None
        head_before = self.git(repo, "rev-parse", "HEAD").stdout
        result = subprocess.run(command or ["git", "-C", str(repo), "commit", "-m", "fixture"],
                                cwd=cwd or self.root, env=env, capture_output=True, check=False)
        return result, index_lock, index_before, head_before, lock_fd

    def make_conflicting_history(self):
        base = self.git(self.repo, "rev-parse", "HEAD").stdout.decode().strip()
        self.git(self.repo, "branch", "-m", "main")
        self.git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "tracked.txt").write_text("feature\n", encoding="utf-8")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-qm", "feature")
        self.git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("main\n", encoding="utf-8")
        self.git(self.repo, "add", "tracked.txt")
        self.git(self.repo, "commit", "-qm", "main")
        self.git(self.repo, "checkout", "-q", "feature")
        return base

    def git_state(self):
        gitdir = Path(self.git(
            self.repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.decode().strip())
        rebase = []
        for directory in (gitdir / "rebase-merge", gitdir / "rebase-apply"):
            if directory.exists():
                rebase.append((str(directory.name), sorted(
                    (str(path.relative_to(directory)), path.read_bytes())
                    for path in directory.rglob("*") if path.is_file())))
        return (
            self.git(self.repo, "rev-parse", "HEAD").stdout,
            (gitdir / "index").read_bytes(),
            (self.repo / "tracked.txt").read_bytes(),
            self.git(self.repo, "status", "--porcelain=v1").stdout,
            rebase,
        )

    @staticmethod
    def fd_bytes(fd):
        return os.pread(fd, 4096, 0)

    @staticmethod
    def fd_identity(fd):
        value = os.fstat(fd)
        return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns

    @staticmethod
    def path_identity(path):
        value = os.lstat(path)
        return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns

    def diagnose(self, result, repo=None, *, env=None, command=None, cwd=None, **kwargs):
        return git_index_lock.diagnose_failure(
            command or ["git", "-C", str(repo or self.repo), "commit", "-m", "fixture"],
            str(cwd or self.root), result.returncode, result.stdout, result.stderr, env=env, **kwargs)

    def test_real_commit_collision_preserves_path_bytes_index_head_and_lock(self):
        result, lock, index_before, head_before, lock_fd = self.fail_commit()
        lock_identity = self.fd_identity(lock_fd)
        diagnosis = self.diagnose(result)
        self.assertEqual(result.returncode, 128)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["state"], "present")
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual(diagnosis["observation"]["mode"], "0o0")
        self.assertIn("Ownership: unknown", diagnosis["message"])
        self.assertEqual(diagnosis["cleanup"], {
            "outcome": "not-attempted", "reason": "read-only-diagnosis"})
        self.assertIn("Lock mutation: none", diagnosis["message"])
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertEqual(lock.stat().st_mode & 0o777, 0)
        self.assertEqual(self.path_identity(lock), lock_identity)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").stdout, head_before)
        self.assertEqual(Path(str(lock)[:-5]).read_bytes(), index_before)
        record = git_index_lock.failure_record(
            result.returncode, result.stdout, result.stderr, "utf-8")
        self.assertEqual(base64.b64decode(record["stdout_b64"]), result.stdout)
        self.assertEqual(base64.b64decode(record["stderr_b64"]), result.stderr)

    def test_real_rebase_start_collision_preserves_post_failure_state_during_diagnosis(self):
        self.make_conflicting_history()
        index = self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        lock = Path(index + ".lock")
        lock_fd = self.make_lock(lock, b"")
        lock_identity = self.fd_identity(lock_fd)
        command = ["git", "-C", str(self.repo), "rebase", "main"]
        result = subprocess.run(command, cwd=self.root, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        after_failure = self.git_state()
        diagnosis = self.diagnose(result, command=command)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual(self.git_state(), after_failure)
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertEqual(lock.stat().st_mode & 0o777, 0)
        self.assertEqual(self.path_identity(lock), lock_identity)

    def test_real_rebase_continue_collision_preserves_index_work_and_rebase_state(self):
        self.make_conflicting_history()
        started = self.git(self.repo, "rebase", "main", check=False)
        self.assertNotEqual(started.returncode, 0)
        self.assertTrue((self.repo / ".git/rebase-merge").is_dir())
        (self.repo / "tracked.txt").write_text("resolved\n", encoding="utf-8")
        self.git(self.repo, "add", "tracked.txt")
        index = self.git(self.repo, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        lock = Path(index + ".lock")
        lock_fd = self.make_lock(lock, b"")
        lock_identity = self.fd_identity(lock_fd)
        command = ["git", "-C", str(self.repo), "-c", "core.editor=true", "rebase", "--continue"]
        result = subprocess.run(command, cwd=self.root, capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        after_failure = self.git_state()
        diagnosis = self.diagnose(result, command=command)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual(self.git_state(), after_failure)
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertEqual(lock.stat().st_mode & 0o777, 0)
        self.assertEqual(self.path_identity(lock), lock_identity)
        self.assertTrue((self.repo / ".git/rebase-merge").is_dir())

    def test_repeated_git_c_and_explicit_git_dir_work_tree_bind_the_lock(self):
        self.stage_change()
        index = self.git(
            self.repo, "rev-parse", "--path-format=absolute", "--git-path", "index"
        ).stdout.decode().strip()
        lock_path = Path(index + ".lock")
        lock_fd = self.make_lock(lock_path, b"")
        lock_identity = self.fd_identity(lock_fd)
        command = [
            "git", "-C", str(self.root), "-C", self.repo.name,
            "--git-dir", str(self.repo / ".git"),
            "--work-tree", str(self.repo),
            "commit", "-m", "global context",
        ]
        result = subprocess.run(command, cwd=self.root, capture_output=True, check=False)
        diagnosis = self.diagnose(result, command=command)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock_path))
        self.assertEqual(diagnosis["observation"]["mode"], "0o0")
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertEqual(self.path_identity(lock_path), lock_identity)

    def test_linked_worktree_uses_its_own_gitdir_lock(self):
        linked = self.root / "linked worktree with ' quote"
        self.git(self.repo, "worktree", "add", "-q", "-b", "linked-fixture", str(linked), "HEAD")
        (linked / "tracked.txt").write_text("linked change\n", encoding="utf-8")
        self.git(linked, "add", "tracked.txt")
        index = self.git(linked, "rev-parse", "--path-format=absolute", "--git-path", "index").stdout.decode().strip()
        lock = Path(index + ".lock")
        lock_fd = self.make_lock(lock, b"")
        result = subprocess.run(["git", "-C", str(linked), "commit", "-m", "linked"],
                                cwd=self.root, capture_output=True, check=False)
        diagnosis = self.diagnose(result, linked, command=["git", "-C", str(linked), "commit", "-m", "linked"])
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertNotEqual(str(lock), str(self.repo / ".git/index.lock"))
        self.assertEqual(self.fd_bytes(lock_fd), b"")

    def test_explicit_relative_git_index_file_is_the_bound_context(self):
        custom = self.repo / "alt" / "index"
        custom.parent.mkdir()
        env = {**os.environ, "GIT_INDEX_FILE": "alt/index"}
        self.git(self.repo, "read-tree", "HEAD", env=env)
        self.stage_change(env=env)
        lock = self.repo / "alt/index.lock"
        lock_fd = self.make_lock(lock, b"")
        result = subprocess.run(["git", "commit", "-m", "custom index"], cwd=self.repo,
                                env=env, capture_output=True, check=False)
        diagnosis = git_index_lock.diagnose_failure(
            ["git", "commit", "-m", "custom index"], str(self.repo),
            result.returncode, result.stdout, result.stderr, env={"GIT_INDEX_FILE": "alt/index"})
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertTrue(custom.exists())

    def test_relative_index_after_git_c_binds_actual_path_and_rejects_prec_c_decoy(self):
        custom = self.repo / "alt" / "index"
        custom.parent.mkdir()
        env = {**os.environ, "GIT_INDEX_FILE": "alt/index"}
        command = ["git", "-C", str(self.repo), "commit", "-m", "relative after -C"]
        self.git(self.repo, "read-tree", "HEAD", env=env)
        self.stage_change(env=env)
        lock = self.repo / "alt" / "index.lock"
        lock_fd = self.make_lock(lock, b"selected index lock")
        custom_before = custom.read_bytes()
        result = subprocess.run(command, cwd=self.root, env=env,
                                capture_output=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(os.fsencode(str(lock)), result.stderr)
        state_after_failure = (custom.read_bytes(), self.git(self.repo, "rev-parse", "HEAD").stdout,
                               self.fd_bytes(lock_fd))

        diagnosis = git_index_lock.diagnose_failure(
            command, str(self.root), result.returncode, result.stdout, result.stderr,
            env={"GIT_INDEX_FILE": "alt/index"})
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual((custom.read_bytes(), self.git(self.repo, "rev-parse", "HEAD").stdout,
                          self.fd_bytes(lock_fd)), state_after_failure)
        self.assertEqual(custom.read_bytes(), custom_before)

        decoy = self.root / "alt" / "index.lock"
        decoy.parent.mkdir()
        decoy.write_bytes(b"must not be inspected")
        forged_stderr = result.stderr.replace(os.fsencode(str(lock)), os.fsencode(str(decoy)))
        observer = mock.Mock(side_effect=AssertionError("pre--C decoy reached lstat"))
        unbound = git_index_lock.diagnose_failure(
            command, str(self.root), result.returncode, result.stdout, forged_stderr,
            env={"GIT_INDEX_FILE": "alt/index"}, observer=observer)
        self.assertEqual(unbound["state"], "unbound")
        observer.assert_not_called()
        self.assertEqual(decoy.read_bytes(), b"must not be inspected")
        self.assertEqual(self.fd_bytes(lock_fd), b"selected index lock")

    def test_index_symlink_and_lock_symlink_are_observed_without_following_lock(self):
        real_index = self.root / "real-index"
        real_index.write_bytes((self.repo / ".git/index").read_bytes())
        (self.repo / ".git/index").unlink()
        (self.repo / ".git/index").symlink_to(real_index)
        self.stage_change()
        lock = real_index.with_name(real_index.name + ".lock")
        target = self.root / "lock-target"
        target.write_bytes(b"do not follow")
        lock.symlink_to(target)
        result = subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "symlink index"],
                                cwd=self.root, capture_output=True, check=False)
        diagnosis = self.diagnose(result)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis["lock_path"], str(lock))
        self.assertEqual(diagnosis["observation"]["kind"], "symlink")
        self.assertEqual(target.read_bytes(), b"do not follow")
        self.assertTrue((self.repo / ".git/index").is_symlink())

    def test_untrusted_path_mismatch_never_reaches_observer(self):
        stderr = b"fatal: Unable to create '/tmp/decoy/index.lock': File exists.\n"
        observer = mock.Mock(side_effect=AssertionError("unbound path must not be inspected"))
        diagnosis = git_index_lock.diagnose_failure(
            ["git", "-C", str(self.repo), "commit"], str(self.root), 128, b"", stderr,
            observer=observer)
        self.assertEqual(diagnosis["state"], "unbound")
        observer.assert_not_called()

    def test_missing_unavailable_replaced_and_permission_states_stay_distinct(self):
        result, lock, _, _, lock_fd = self.fail_commit()
        lock.unlink()
        after_failure_before_diagnosis = self.git_state()
        diagnosis = self.diagnose(result)
        self.assertEqual(diagnosis["state"], "not_found")
        self.assertFalse(lock.exists())
        self.assertEqual(self.git_state(), after_failure_before_diagnosis)
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        for observation, state in (
            ({"state": "unavailable", "reason": "permission-denied"}, "unavailable"),
            ({"state": "replaced"}, "replaced"),
        ):
            with self.subTest(state=state):
                diagnosis = self.diagnose(result, observer=lambda _path, _time: observation)
                self.assertEqual(diagnosis["state"], state)
                self.assertEqual(diagnosis["lock_path"], str(lock))
                self.assertIn("Ownership: unknown", diagnosis["message"])
                self.assertFalse(lock.exists())
                self.assertEqual(self.git_state(), after_failure_before_diagnosis)
                self.assertEqual(self.fd_bytes(lock_fd), b"")

    def test_metadata_signature_is_advisory_and_never_retries(self):
        result, lock, *_ = self.fail_commit()
        lock_fd = self.lock_fds[-1]
        identity = self.fd_identity(lock_fd)
        observation = {"state": "present", "kind": "regular-file", "mode": "0o0",
                       "size": 0, "mtime_ns": 1}
        runner = mock.Mock(return_value={"index": str(lock)[:-5], "cwd": str(self.repo)})
        diagnosis = self.diagnose(
            result, observer=lambda _path, _timeout: observation, git_runner=runner)
        self.assertEqual(diagnosis["cleanup"], {
            "outcome": "not-attempted", "reason": "read-only-diagnosis"})
        self.assertIn("Ownership: unknown", diagnosis["message"])
        self.assertIn("outstanding NFS client operation", diagnosis["message"])
        self.assertIn("Lock mutation: none", diagnosis["message"])
        self.assertNotIn("may retry", diagnosis["message"])
        self.assertEqual(self.fd_identity(lock_fd), identity)
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertTrue(lock.exists())
        runner.assert_called_once()

    def test_pending_nfs_create_setattr_candidate_stays_canonical_while_setattr_resumes(self):
        result, lock, *_ = self.fail_commit()
        lock_fd = self.lock_fds[-1]
        before = os.fstat(lock_fd)
        identity = (before.st_dev, before.st_ino)
        original_bytes = os.pread(lock_fd, 4096, 0)
        self.assertEqual(before.st_mode & 0o777, 0)
        self.assertEqual(original_bytes, b"")

        now_ns = time.time_ns()
        candidate_observation = {
            "state": "present", "kind": "regular-file", "mode": "0o0", "size": 0,
            "dev_major": os.major(before.st_dev), "dev_minor": os.minor(before.st_dev),
            "ino": before.st_ino, "mtime_ns": now_ns - 25 * 60 * 60 * 1_000_000_000,
            "ctime_ns": now_ns - 300 * 1_000_000_000,
            "nfs_v3": True, "cache_grace_seconds": 120,
        }
        self.assertGreaterEqual(candidate_observation["ctime_ns"] -
                                candidate_observation["mtime_ns"],
                                24 * 60 * 60 * 1_000_000_000)
        self.assertGreaterEqual(now_ns - candidate_observation["ctime_ns"],
                                candidate_observation["cache_grace_seconds"] * 1_000_000_000)

        ready = threading.Event()
        resume = threading.Event()
        completed = threading.Event()
        operation_errors = []

        def delayed_post_create_setattr():
            ready.set()
            if not resume.wait(2):
                operation_errors.append("delayed SETATTR was not resumed")
                completed.set()
                return
            try:
                os.fchmod(lock_fd, 0o640)
            except OSError as exc:
                operation_errors.append(exc)
            finally:
                completed.set()

        delayed_operation = threading.Thread(
            target=delayed_post_create_setattr, name="simulated-nfs-post-create-setattr")
        delayed_operation.start()
        try:
            self.assertTrue(ready.wait(1))
            with mock.patch.object(git_index_lock.os, "rename") as rename, \
                    mock.patch.object(git_index_lock.os, "unlink") as unlink, \
                    mock.patch.object(git_index_lock.os, "mkdir") as quarantine:
                diagnosis = self.diagnose(
                    result, observer=lambda _path, _timeout: candidate_observation,
                    git_runner=lambda *_args: {
                        "index": str(lock)[:-5], "logical_lock": str(lock),
                        "cwd": str(self.repo),
                    })
                self.assertEqual(diagnosis["state"], "present")
                self.assertEqual(diagnosis["observation"]["mode"], "0o0")
                self.assertEqual(diagnosis["observation"]["ino"], before.st_ino)
                self.assertEqual(diagnosis["observation"]["cache_grace_seconds"], 120)
                self.assertEqual(diagnosis["cleanup"]["outcome"], "not-attempted")
                self.assertEqual((os.lstat(lock).st_dev, os.lstat(lock).st_ino), identity)
                self.assertEqual(os.pread(lock_fd, 4096, 0), original_bytes)

                # Resume the operation through the already-open file handle while
                # the canonical directory entry is still being observed.
                resume.set()
                self.assertTrue(completed.wait(1))
                self.assertEqual(operation_errors, [])
                rename.assert_not_called()
                unlink.assert_not_called()
                quarantine.assert_not_called()

                after = os.lstat(lock)
                self.assertEqual((after.st_dev, after.st_ino), identity)
                self.assertEqual(after.st_mode & 0o777, 0o640)
                self.assertEqual(os.pread(lock_fd, 4096, 0), original_bytes)
        finally:
            resume.set()
            delayed_operation.join(timeout=1)
            self.assertFalse(delayed_operation.is_alive())

    def test_success_signal_permission_other_lock_and_unrecognized_locale_are_not_reclassified(self):
        result, lock, *_ = self.fail_commit()
        argv = ["git", "-C", str(self.repo), "commit"]
        kwargs = {"cwd": str(self.root), "stdout": result.stdout}
        observer = mock.Mock(side_effect=AssertionError("nonmatching failures do not inspect"))
        cases = [
            (0, b"fatal: Unable to create '/x/.git/index.lock': File exists."),
            (-9, b"fatal: Unable to create '/x/.git/index.lock': File exists."),
            (128, b"fatal: Unable to create '/x/.git/HEAD.lock': File exists."),
            (128, b"fatal: Unable to create '/x/.git/index.lock': Permission denied"),
            (128, b"fatal: No se pudo crear '/x/.git/index.lock': File exists."),
        ]
        for code, stderr in cases:
            with self.subTest(code=code, stderr=stderr):
                self.assertIsNone(git_index_lock.diagnose_failure(
                    argv, returncode=code, stderr=stderr, observer=observer, **kwargs))
        observer.assert_not_called()

    def test_shell_parser_only_accepts_single_git_and_literal_prefixes(self):
        parsed = git_index_lock.parse_shell_command(
            "cd 'dir with spaces' && GIT_INDEX_FILE='cache/index' git -C sub commit -m 'x'",
            str(self.root))
        self.assertEqual(parsed[0][-3:], ["commit", "-m", "x"])
        self.assertEqual(parsed[2], {"GIT_INDEX_FILE": "cache/index"})
        for command in (
            "git status && git commit", "git status | cat", "git commit; echo done",
            "git commit $(printf unsafe)", "echo before && git commit",
        ):
            with self.subTest(command=command):
                self.assertIsNone(git_index_lock.parse_shell_command(command, str(self.root)))

    def test_one_deadline_is_shared_between_git_resolution_and_observation(self):
        result, lock, *_ = self.fail_commit()
        time_values = iter((10.0, 10.0, 11.6))
        runner = mock.Mock(return_value=str(lock)[:-5])
        observer = mock.Mock(return_value={"state": "unavailable", "reason": "observer-timeout"})
        with mock.patch.object(git_index_lock.time, "monotonic", side_effect=lambda: next(time_values)):
            diagnosis = self.diagnose(result, git_runner=runner, observer=observer, budget_seconds=2.0)
        self.assertEqual(runner.call_args.args[3], 2.0)
        self.assertAlmostEqual(observer.call_args.args[1], 0.4)
        self.assertEqual(diagnosis["state"], "unavailable")
        self.assertIn("observer-timeout", diagnosis["message"])

    def test_real_consumer_returns_within_budget_when_git_resolution_stalls(self):
        result, lock, _, _, lock_fd = self.fail_commit()
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        pid_file = self.root / "slow-git.pid"
        fake_git = fake_bin / "git"
        fake_git.write_text(
            "#!/usr/bin/python3\n"
            "import os, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"}):
            diagnosis = git_index_lock.diagnose_failure(
                ["git", "-C", str(self.repo), "commit", "-m", "fixture"],
                str(self.root), result.returncode, result.stdout, result.stderr,
            )
        elapsed = time.monotonic() - started
        self.assertEqual(diagnosis["state"], "unavailable")
        self.assertEqual(diagnosis["observation"]["reason"], "diagnostic-timeout")
        self.assertLess(elapsed, git_index_lock.DIAGNOSIS_BUDGET_SECONDS + 0.75)
        self.assertNotIn("no file was changed", diagnosis["message"].lower())
        self.assertEqual(self.fd_bytes(lock_fd), b"")
        self.assertEqual(lock.stat().st_mode & 0o777, 0)

    def test_crlf_and_undecodable_bytes_have_compatible_text_and_lossless_side_channel(self):
        stdout, stderr = b"out\r\n\xff", b"err\r\n\xfe"
        self.assertEqual(git_index_lock.compatibility_text(stdout, "utf-8", failed=True), "out\n\ufffd")
        self.assertEqual(git_index_lock.compatibility_text(stderr, "utf-8", failed=True), "err\n\ufffd")
        record = git_index_lock.failure_record(128, stdout, stderr, "utf-8")
        self.assertEqual(base64.b64decode(record["stdout_b64"]), stdout)
        self.assertEqual(base64.b64decode(record["stderr_b64"]), stderr)
        self.assertEqual(record["returncode"], 128)


if __name__ == "__main__":
    unittest.main(verbosity=2)
