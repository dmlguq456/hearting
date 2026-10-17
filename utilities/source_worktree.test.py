#!/usr/bin/env python3
"""Focused worktree preparation and compose --start failure/retry boundary tests."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("route_for_source_worktree", HERE / "capability-route.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
import work_start as W  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def candidates():
    return {"candidates": [{
        "harness": harness, "transport": "headless", "surface": "registered-headless",
        "status": "supported", "probe_source": "fixture", "probe_time": "2026-10-15T00:00:00Z",
    } for harness in ("claude", "codex")]}


class SourceWorktreeFixture(unittest.TestCase):
    object_format = "sha1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        self.primary = base / "repo"
        self.writer = base / "writer"
        git(base, "init", "-q", "--bare", "--object-format=" + self.object_format,
            "-b", "main", str(self.origin))
        git(base, "clone", "-q", str(self.origin), str(self.primary))
        git(self.primary, "config", "user.name", "Fixture")
        git(self.primary, "config", "user.email", "fixture@example.test")
        (self.primary / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.primary, "add", "base.txt")
        git(self.primary, "commit", "-qm", "base")
        git(self.primary, "push", "-q", "-u", "origin", "main")
        git(self.primary, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
            "commit", "--allow-empty", "-qm", "primary-only")
        git(base, "clone", "-q", str(self.origin), str(self.writer))
        git(self.writer, "config", "user.name", "Fixture")
        git(self.writer, "config", "user.email", "fixture@example.test")
        (self.writer / "remote-tip.txt").write_text("remote tip\n", encoding="utf-8")
        git(self.writer, "add", "remote-tip.txt")
        git(self.writer, "commit", "-qm", "remote tip")
        git(self.writer, "push", "-q", "origin", "main")
        self.remote_sha = git(self.writer, "rev-parse", "HEAD")
        (self.primary / "base.txt").write_text("staged change\n", encoding="utf-8")
        git(self.primary, "add", "base.txt")
        (self.primary / "base.txt").write_text("unstaged change\n", encoding="utf-8")
        (self.primary / "untracked.txt").write_text("preserve\n", encoding="utf-8")
        self.artifact_root = base / ".agent_reports"
        self.artifact_root.mkdir()
        self.jobs = base / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        self.prompt = base / "request.md"
        self.prompt.write_text("Implement the approved source change.\n", encoding="utf-8")
        self.evidence = base / "primary-evidence.json"
        self.evidence.write_text(json.dumps({"tuples": [{"checked_worktree": str(self.primary)}]}), encoding="utf-8")
        fixture_env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_",
                                              "AGENT_ROUTE_", "AGENT_ARTIFACT_"))}
        fixture_env.update({
            "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
        })
        self.env = mock.patch.dict(os.environ, fixture_env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.start_calls = []
        self.transport_calls = []
        self.real_start_work = W.start_work
        self.probe_cwds = []
        self.prep_argv = None
        self._readiness_patch = mock.patch.object(
            R, "_compose_readiness", side_effect=self.readiness)
        self._readiness_patch.start()
        self.addCleanup(self._readiness_patch.stop)

    def primary_fingerprint(self):
        return (git(self.primary, "rev-parse", "HEAD"), git(self.primary, "branch", "--show-current"),
                git(self.primary, "write-tree"), git(self.primary, "diff", "--binary"),
                git(self.primary, "diff", "--cached", "--binary"), git(self.primary, "status", "--porcelain"),
                (self.primary / "base.txt").read_bytes(), (self.primary / "untracked.txt").read_bytes())

    def readiness(self, cwd, jobs, parent_harness, children, **kwargs):
        self.probe_cwds.append(str(Path(cwd).resolve()))
        return {"tuples": [], **candidates()}

    def argv(self):
        return ["capability-route.py", "compose", "--start", "--slug", "retry-task",
                "--shape", "solo", "--capability", "autopilot-code", "--prompt-file", str(self.prompt),
                "--cwd", str(self.primary), "--artifact-root", str(self.artifact_root), "--unassigned",
                "--jobs", str(self.jobs), "--dispatch-evidence", str(self.evidence), "--spec-read", "fixture"]

    def main(self, argv):
        self.prep_argv = list(argv)
        with mock.patch.object(sys, "argv", list(argv)), \
                mock.patch.object(W, "start_work", side_effect=self.start_work):
            return R.main()

    def start_work(self, route, path, jobs):
        if route.get("effective_intensity") == "direct":
            self.start_calls.append((route, Path(path)))
            return {"state": "inline", "required_action": "execute-inline", "owner_started": False,
                    "launches": []}

        def fake_transport(command, **kwargs):
            self.transport_calls.append(command)
            value = lambda flag: command[command.index(flag) + 1]
            published = json.loads(Path(value("--route-evidence")).read_text(encoding="utf-8"))
            attempt = value("--attempt-id")
            pid = os.getpid()
            pid_start = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[21]
            metadata = {"attempt_schema_version": "2", "attempt_id": attempt,
                        "parent_sid": "parent", "launch_started": "1", "worker_type": "owner",
                        "dispatch_depth": "1", "route_id": published["route_id"],
                        "route_hash": published["route_hash"], "owner_route_id": published["route_id"],
                        "owner_route_hash": published["route_hash"], "pid": str(pid), "pid_start": pid_start,
                        "parent_completion_delivery": "codex-managed-gateway"}
            with Path(value("--jobs")).open("a", encoding="utf-8") as stream:
                stream.write("now\topen\t" + str(route["cwd"]) + "\t" + str(route["cwd"])
                             + "\towner\t" + ",".join(f"{key}={item}" for key, item in metadata.items()) + "\n")
            return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "timeout", "children": []}), \
                mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")):
            result = self.real_start_work(route, path, jobs, run=fake_transport)
        self.start_calls.append((route, Path(path)))
        return result

    def route_files(self):
        return sorted(R.canonical_routes_dir(self.artifact_root).glob("rt-*.json"))

    def owner_attempt_rows(self):
        return [line for line in self.jobs.read_text(encoding="utf-8").splitlines()
                if line.strip() and "worker_type=owner" in line]

    def assert_failure_then_retry(self, failure):
        argv = self.argv()
        before = self.primary_fingerprint()
        real = R._worktree_git
        injected = {"done": False}

        def fail_once(cwd, *args):
            is_target = (failure == "fetch" and args[:1] == ("fetch",)
                         or failure == "add" and args[:2] == ("worktree", "add"))
            if is_target and not injected["done"]:
                injected["done"] = True
                return subprocess.CompletedProcess(["git", *args], 128, "", f"injected {failure} failure")
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=fail_once), self.assertRaisesRegex(
                ValueError, "source-worktree-preparation-failed"):
            self.main(argv)
        self.assertTrue(injected["done"])
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual(self.route_files(), [])
        self.assertEqual((self.start_calls, self.probe_cwds), ([], []))
        self.assertEqual(self.owner_attempt_rows(), [])
        if failure == "add":
            records = list((self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["state"], "reserved")

        # The identical parser invocation resumes the same request without new answers or input.
        original_prompt = self.prompt.read_bytes()
        result = self.main(argv)
        self.assertEqual(result, 0)
        self.assertEqual(self.prep_argv, argv)
        self.assertEqual(self.prompt.read_bytes(), original_prompt)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual(len(self.route_files()), 1)
        self.assertEqual((len(self.start_calls), len(self.probe_cwds)), (1, 1))
        self.assertEqual(len(self.owner_attempt_rows()), 1)
        route = json.loads(self.route_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(route["cwd"], str(Path(self.start_calls[0][0]["cwd"]).resolve()))
        self.assertEqual(git(route["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        self.assertEqual(self.probe_cwds, [route["cwd"]])
        self.assertEqual(self.start_calls[0][0]["work_request"]["text"], original_prompt.decode())
        self.assertEqual(self.start_calls[0][0]["artifact_root"], str(self.artifact_root.resolve()))

class ComposeStartPreparationRetryTest(SourceWorktreeFixture):
    def test_read_only_changed_file_status_disables_optional_index_refresh(self):
        result = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(R.subprocess, "run", return_value=result) as run:
            self.assertEqual(R._git_changed_files(self.primary), set())
        self.assertEqual(run.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")

    def test_compose_start_fetch_failure_retries_same_request_once(self):
        self.assert_failure_then_retry("fetch")

    def test_repository_without_remotes_uses_primary_head_and_reuses_reservation(self):
        git(self.primary, "remote", "remove", "origin")
        before = self.primary_fingerprint()
        real_git = R._worktree_git
        calls = []

        def record_git(cwd, *args):
            calls.append(args)
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=record_git):
            self.assertEqual(self.main(self.argv()), 0)
        route, _ = self.start_calls[0]
        self.assertEqual(git(route["cwd"], "rev-parse", "HEAD"), git(self.primary, "rev-parse", "HEAD"))
        self.assertNotIn(("ls-remote", "--symref", "origin", "HEAD"), calls)
        self.assertFalse(any(args[:1] == ("fetch",) for args in calls))
        self.assertEqual(self.primary_fingerprint(), before)
        allocated = route["cwd"]

        calls.clear()
        with mock.patch.object(R, "_worktree_git", side_effect=record_git):
            self.assertEqual(self.main(self.argv()), 0)
        self.assertEqual(len(self.route_files()), 1)
        self.assertEqual(self.start_calls[-1][0]["cwd"], allocated)
        self.assertNotIn(("ls-remote", "--symref", "origin", "HEAD"), calls)
        self.assertFalse(any(args[:1] == ("fetch",) for args in calls))

    def test_repository_without_remotes_or_commit_fails_before_publication(self):
        empty = Path(self.tmp.name) / "empty-repo"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "main")
        base, reason, _ = R._remote_default_base(empty)
        self.assertIsNone(base)
        self.assertEqual(reason, "local-default-unresolved")
        argv = self.argv()
        argv[argv.index("--cwd") + 1] = str(empty)
        self.evidence.write_text(json.dumps({"tuples": [{"checked_worktree": str(empty)}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source-worktree-preparation-failed"):
            self.main(argv)
        self.assertEqual((self.route_files(), self.start_calls, self.owner_attempt_rows()), ([], [], []))

    def test_compose_start_worktree_add_failure_retries_same_request_once(self):
        self.assert_failure_then_retry("add")

    def test_worktree_add_before_ready_record_interruption_reuses_same_allocation(self):
        argv = self.argv()
        before = self.primary_fingerprint()
        real_atomic_write = R.atomic_write
        interrupted = {"done": False}

        def interrupt_after_add(path, payload):
            if isinstance(payload, dict) and payload.get("state") == "ready" and not interrupted["done"]:
                interrupted["done"] = True
                raise OSError("simulated interruption after worktree add")
            return real_atomic_write(path, payload)

        with mock.patch.object(R, "atomic_write", side_effect=interrupt_after_add), self.assertRaisesRegex(
                ValueError, "source-worktree-preparation-failed"):
            self.main(argv)
        self.assertTrue(interrupted["done"])
        self.assertEqual((self.primary_fingerprint(), self.route_files(), self.start_calls), (before, [], []))
        self.assertEqual(self.owner_attempt_rows(), [])
        records = list((self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "reserved")
        allocated_path = record["path"]
        self.assertTrue(Path(allocated_path).is_dir())

        real_git = R._worktree_git

        def no_second_allocation(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            self.assertNotEqual(args[:2], ("worktree", "add"))
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=no_second_allocation):
            self.assertEqual(self.main(argv), 0)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual(len(self.route_files()), 1)
        self.assertEqual((len(self.start_calls), self.probe_cwds), (1, [self.start_calls[0][0]["cwd"]]))
        self.assertEqual(len(self.owner_attempt_rows()), 1)
        route = json.loads(self.route_files()[0].read_text(encoding="utf-8"))
        self.assertEqual(route["cwd"], allocated_path)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual((record["state"], record["path"]), ("ready", allocated_path))

    def test_direct_source_and_omitted_cwd_use_the_same_preparation_boundary(self):
        argv = ["capability-route.py", "compose", "--start", "--slug", "direct-task",
                "--shape", "direct", "--capability", "autopilot-code", "--prompt-file", str(self.prompt),
                "--artifact-root", str(self.artifact_root), "--unassigned", "--jobs", str(self.jobs),
                "--spec-read", "fixture"]
        before = os.getcwd()
        try:
            os.chdir(self.primary)
            self.main(argv)
        finally:
            os.chdir(before)
        (route, path), = self.start_calls
        self.assertNotEqual(route["cwd"], str(self.primary))
        self.assertEqual(git(route["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        self.assertEqual(route["artifact_root"], str(self.artifact_root.resolve()))

    def test_nonstart_explain_does_not_prepare_a_worktree(self):
        argv = ["capability-route.py", "compose", "--explain", "--slug", "preview-task",
                "--shape", "solo", "--capability", "autopilot-code", "--prompt-file", str(self.prompt),
                "--cwd", str(self.primary), "--artifact-root", str(self.artifact_root), "--unassigned",
                "--jobs", str(self.jobs), "--spec-read", "fixture"]
        real = R._worktree_git
        with mock.patch.object(R, "_worktree_git", side_effect=AssertionError("preview must not prepare")), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(R.main(), 0)
        self.assertEqual(self.start_calls, [])
        self.assertFalse((self.primary.parent / "repo-wt").exists())

    def test_nonstart_compose_does_not_prepare_a_worktree(self):
        argv = ["capability-route.py", "compose", "--slug", "nonstart-task",
                "--shape", "solo", "--capability", "autopilot-code", "--prompt-file", str(self.prompt),
                "--cwd", str(self.primary), "--artifact-root", str(self.artifact_root), "--unassigned",
                "--jobs", str(self.jobs), "--spec-read", "fixture"]
        with mock.patch.object(R, "_worktree_git", side_effect=AssertionError("non-start compose must not prepare")), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(R.main(), 0)
        self.assertEqual(self.start_calls, [])
        route, = [json.loads(path.read_text(encoding="utf-8")) for path in self.route_files()]
        self.assertEqual(route["cwd"], str(self.primary.resolve()))
        self.assertFalse((self.primary.parent / "repo-wt").exists())


class Sha256PreparationTest(SourceWorktreeFixture):
    object_format = "sha256"

    def test_fresh_allocation_keeps_fetched_sha256_commit(self):
        before = self.primary_fingerprint()
        self.assertEqual(len(self.remote_sha), 64)
        self.assertEqual(self.main(self.argv()), 0)
        route_file, = self.route_files()
        route = json.loads(route_file.read_text(encoding="utf-8"))
        self.assertEqual(git(route["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual(self.probe_cwds, [route["cwd"]])
        self.assertEqual(len(self.owner_attempt_rows()), 1)

    def test_reserved_sha256_branch_retries_without_fetch_or_new_allocation(self):
        argv = self.argv()
        before = self.primary_fingerprint()
        real_git = R._worktree_git

        def interrupt_after_branch(cwd, *args):
            if args[:3] == ("worktree", "add", "-b"):
                branch, _, base_sha = args[3:]
                git(cwd, "branch", branch, base_sha)
                return subprocess.CompletedProcess(
                    ["git", *args], 128, "", "simulated interruption after branch creation")
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=interrupt_after_branch), \
                self.assertRaisesRegex(ValueError, "source-worktree-preparation-failed"):
            self.main(argv)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((self.route_files(), self.start_calls, self.owner_attempt_rows()), ([], [], []))
        record_file, = (self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual((record["state"], record["base_sha"]), ("reserved", self.remote_sha))
        self.assertFalse(Path(record["path"]).exists())
        self.assertEqual(git(self.primary, "rev-parse", record["branch"]), self.remote_sha)
        resumed_adds = []

        def resume_existing_branch(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            if args[:2] == ("worktree", "add"):
                self.assertEqual(args, ("worktree", "add", record["path"], record["branch"]))
                resumed_adds.append(args)
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=resume_existing_branch):
            self.assertEqual(self.main(argv), 0)
        self.assertEqual(len(resumed_adds), 1)
        route_file, = self.route_files()
        route = json.loads(route_file.read_text(encoding="utf-8"))
        self.assertEqual(route["cwd"], record["path"])
        self.assertEqual(git(route["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((len(self.start_calls), len(self.owner_attempt_rows())), (1, 1))
        ready = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual((ready["state"], ready["path"]), ("ready", record["path"]))


if __name__ == "__main__":
    unittest.main()
