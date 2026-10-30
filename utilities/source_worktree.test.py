#!/usr/bin/env python3
"""Focused worktree preparation and compose --start failure/retry boundary tests."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import re
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
        # A direct test invocation can inherit repository redirection from a
        # hook or caller. Isolate Git before the first fixture Git operation,
        # not only before the production helper is called.
        fixture_home = base / "home"
        fixture_home.mkdir()
        fixture_env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_",
                                  "AGENT_ROUTE_", "AGENT_ARTIFACT_"))
        }
        fixture_env.update({
            "HOME": str(fixture_home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(base),
            "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(base / "jobs.log"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
        })
        self.env = mock.patch.dict(os.environ, fixture_env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
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
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
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

    def start_work(self, route, path, jobs, **_kwargs):
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

class FramedStartPolicyTest(SourceWorktreeFixture):
    def linked_at(self, branch, ref):
        path = Path(self.tmp.name) / f"linked-{branch}"
        git(self.primary, "worktree", "add", "-q", "-b", branch, str(path), ref)
        git(path, "config", "user.name", "Fixture")
        git(path, "config", "user.email", "fixture@example.test")
        return path

    def test_implicit_dirty_or_unmerged_linked_checkout_is_retained(self):
        base_sha = git(self.writer, "rev-parse", "HEAD~1")
        dirty = self.linked_at("dirty-linked", base_sha)
        (dirty / "untracked.txt").write_text("keep me\n", encoding="utf-8")
        dirty_head = git(dirty, "rev-parse", "HEAD")
        result = R.prepare_isolated_worktree(dirty, "framed-dirty", artifact_root=self.artifact_root,
                                             preparation_key={"test": "dirty"}, framed_start=True)
        self.assertEqual(result["state"], "retained-linked")
        self.assertEqual(result["reason"], "linked-worktree-dirty")
        self.assertEqual(result["cwd"], str(dirty.resolve()))
        self.assertEqual(git(dirty, "rev-parse", "HEAD"), dirty_head)
        self.assertTrue((dirty / "untracked.txt").exists())

        unmerged = self.linked_at("unmerged-linked", base_sha)
        (unmerged / "local.txt").write_text("local\n", encoding="utf-8")
        git(unmerged, "add", "local.txt")
        git(unmerged, "commit", "-qm", "unmerged local work")
        unmerged_head = git(unmerged, "rev-parse", "HEAD")
        result = R.prepare_isolated_worktree(unmerged, "framed-unmerged", artifact_root=self.artifact_root,
                                             preparation_key={"test": "unmerged"}, framed_start=True)
        self.assertEqual(result["state"], "retained-linked")
        self.assertEqual(result["reason"], "linked-worktree-unmerged")
        self.assertEqual(git(unmerged, "rev-parse", "HEAD"), unmerged_head)

    def test_clean_merged_linked_checkout_allocates_from_the_fetched_remote_sha_once(self):
        base_sha = git(self.writer, "rev-parse", "HEAD~1")
        linked = self.linked_at("merged-linked", base_sha)
        real = R._worktree_git
        calls = []

        def record(cwd, *args):
            calls.append(args)
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=record):
            result = R.prepare_isolated_worktree(linked, "framed-merged", artifact_root=self.artifact_root,
                                                 preparation_key={"test": "merged"}, framed_start=True)
        self.assertEqual(result["state"], "created")
        self.assertEqual(Path(result["cwd"]).parent, self.primary.parent / "repo-wt")
        self.assertEqual(git(result["cwd"], "rev-parse", "HEAD"), self.remote_sha)
        self.assertEqual(sum(args[:1] == ("fetch",) for args in calls), 1)

    def test_explicit_linked_checkout_keeps_approved_meaning_without_fetch(self):
        linked = self.linked_at("explicit-linked", git(self.writer, "rev-parse", "HEAD~1"))
        with mock.patch.object(R, "_remote_default_base", side_effect=AssertionError("explicit cwd is approved")):
            result = R.prepare_isolated_worktree(linked, "explicit", artifact_root=self.artifact_root,
                                                 preparation_key={"test": "explicit"},
                                                 explicitly_approved=True, framed_start=True)
        self.assertEqual(result["state"], "reused")
        self.assertEqual(result["reason"], "approved-linked-task")
        self.assertEqual(result["cwd"], str(linked.resolve()))

    def test_framed_compose_start_from_a_stale_merged_branch_reads_the_fetched_remote_default(self):
        git(self.primary, "switch", "-q", "-c", "merged-long-ago", "HEAD~1")
        before = self.primary_fingerprint()
        argv = self.argv()
        for flag in ("--shape", "--capability"):
            del argv[argv.index(flag):argv.index(flag) + 2]
        started = []

        def start(route, path, jobs):
            started.append(route)
            return {"state": "running", "owner_started": True, "launches": []}

        def readiness(cwd, jobs, parent_harness, children, **kwargs):
            self.probe_cwds.append(str(Path(cwd).resolve()))
            # The caller's harness follows the session running the test; seal a sandbox its wrapper exports.
            return {"tuples": [{
                "parent_harness": parent_harness, "parent_transport": "headless",
                "parent_sandbox": {"codex": "workspace-write"}.get(parent_harness, "adapter-default"),
                "child_harness": "codex",
                "launch_authority": "conductor", "status": "supported",
                "probe_source": "fixture", "probe_time": "2026-10-15T00:00:00Z",
                "failure_class": "", "checked_worktree": str(Path(cwd).resolve()),
                "failure_scope": "none", "codex_command": "ok", "retry_on_isolated_worktree": 0,
            }], **candidates()}

        notice = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(W, "start_work", side_effect=start), \
                mock.patch.object(R, "_compose_readiness", side_effect=readiness), \
                contextlib.redirect_stderr(notice):
            self.assertEqual(R.main(), 0)
        route, = started
        expected = str((self.primary.parent / "repo-wt" / "retry-task").resolve())
        self.assertEqual((route["capability"], route["selection"]["shape"]), ("route-frame", "framed"))
        self.assertEqual((route["cwd"], route["source_commit"]), (expected, self.remote_sha))
        self.assertEqual((route["framed_worktree"]["state"], route["framed_worktree"]["branch"]),
                         ("created", "retry-task"))
        self.assertEqual(git(expected, "branch", "--show-current"), "retry-task")
        self.assertEqual(set(self.probe_cwds), {expected})
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertIn("worktree_prepared=created", notice.getvalue())
        self.assertIn(f"Frame read basis: prepared {expected} at {self.remote_sha}.", W.frame_task_text(route))

    def test_framed_compose_start_fetch_failure_seals_old_cwd_and_continues(self):
        argv = self.argv()
        argv[argv.index("solo")] = "framed"
        self.evidence.write_text(json.dumps({"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless",
            "parent_sandbox": "adapter-default", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture", "probe_time": "2026-10-15T00:00:00Z",
            "failure_class": "", "checked_worktree": str(self.primary.resolve()),
            "failure_scope": "none", "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}), encoding="utf-8")
        real = R._worktree_git

        def fail_fetch(cwd, *args):
            if args[:1] == ("fetch",):
                return subprocess.CompletedProcess(["git", *args], 128, "", "injected\nfetch failure")
            return real(cwd, *args)

        started = []
        def start(route, path, jobs):
            started.append((route, Path(path)))
            return {"state": "running", "owner_started": True, "launches": []}

        notice = io.StringIO()
        with mock.patch.object(R, "_worktree_git", side_effect=fail_fetch), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(W, "start_work", side_effect=start), \
                contextlib.redirect_stderr(notice):
            self.assertEqual(R.main(), 0)
        self.assertEqual(len(started), 1)
        route_files = self.route_files()
        self.assertEqual(len(route_files), 1)
        route = json.loads(route_files[0].read_text(encoding="utf-8"))
        self.assertEqual(route["cwd"], str(self.primary.resolve()))
        self.assertEqual(route["source_commit"], git(self.primary, "rev-parse", "HEAD"))
        self.assertEqual(route["framed_worktree"]["state"], "failed")
        self.assertEqual(route["selection"]["shape"], "framed")
        self.assertEqual(notice.getvalue().count("framed_worktree_fallback="), 1)
        self.assertNotIn("worktree_prepared=failed", notice.getvalue())
        self.assertEqual(notice.getvalue().count("framed_worktree_fallback="),
                         sum(line.startswith("framed_worktree_fallback=") for line in notice.getvalue().splitlines()))
        self.assertNotIn("\nfetch failure", notice.getvalue())


class ComposeStartPreparationRetryTest(SourceWorktreeFixture):
    def test_non_git_start_with_direct_starts_inline_without_git_or_worker(self):
        project = Path(self.tmp.name) / "non-git-project"
        project.mkdir()
        prompt = Path(self.tmp.name) / "non-git-task.md"
        prompt.write_text("기술 동향 조사 결과를 정리한다.\n", encoding="utf-8")
        for shape_args in (["--shape", "direct"],):
            with self.subTest(shape=shape_args):
                argv = ["capability-route.py", "compose", "--start", "--slug", "non-git-start",
                        *shape_args, "--prompt-file", str(prompt), "--cwd", str(project),
                        "--artifact-root", str(self.artifact_root), "--unassigned", "--jobs", str(self.jobs)]
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self.main(argv), 0)
                route, _path = self.start_calls[-1]
                self.assertEqual((route["capability"], route["effective_intensity"], route["source_commit"]),
                                 ("autopilot-research", "direct", "unversioned"))
                self.assertEqual([node["id"] for node in route["nodes"]], ["inline"])
                self.assertIn("git 아닌 폴더 — 이 세션이 직접 진행. 작업자가 필요하면 --shape 없이 다시 시작(원본 수정은 git init 필요)",
                              err.getvalue())
                self.assertEqual(err.getvalue().count("git 아닌 폴더 — 이 세션이 직접 진행"), 1)
                self.assertFalse((project / ".git").exists())
                self.assertFalse((self.artifact_root / ".runtime" / "worktree-preparation").exists())
                self.assertEqual(self.owner_attempt_rows(), [])
        self.assertEqual(len(self.route_files()), 1)

    def test_non_git_mutating_worker_and_registered_direct_refuse_before_route_write(self):
        project = Path(self.tmp.name) / "non-git-refusal"
        project.mkdir()
        prompt = Path(self.tmp.name) / "non-git-refusal.md"
        prompt.write_text("Implement a reusable driver.\n", encoding="utf-8")
        self.evidence.write_text(json.dumps({"tuples": [{
            "parent_harness": "claude", "parent_transport": "headless",
            "parent_sandbox": "adapter-default", "child_harness": "codex",
            "launch_authority": "conductor", "status": "supported",
            "probe_source": "fixture", "probe_time": "2026-10-15T00:00:00Z",
            "failure_class": "", "checked_worktree": str(project.resolve()),
            "failure_scope": "none", "codex_command": "ok", "retry_on_isolated_worktree": 0,
        }], "native_subagent": []}), encoding="utf-8")
        for extra in (["--shape", "solo"], ["--shape", "staged"]):
            with self.subTest(extra=extra):
                argv = ["capability-route.py", "compose", "--start", "--slug", "refused",
                        *extra, "--prompt-file", str(prompt), "--cwd", str(project),
                        "--dispatch-evidence", str(self.evidence),
                        "--artifact-root", str(self.artifact_root), "--unassigned", "--jobs", str(self.jobs)]
                with self.assertRaisesRegex(ValueError, "git 저장소 아님: .*source-mutating worker route"):
                    self.main(argv)
        framed_argv = ["capability-route.py", "compose", "--start", "--slug", "framed-recordish",
                       "--shape", "framed", "--prompt-file", str(prompt), "--cwd", str(project),
                       "--dispatch-evidence", str(self.evidence), "--artifact-root", str(self.artifact_root),
                       "--unassigned", "--jobs", str(self.jobs)]
        self.assertEqual(self.main(framed_argv), 0)
        frame_route = self.start_calls[-1][0]
        self.assertEqual((frame_route["source_commit"], frame_route["selection"]["shape"],
                          frame_route["cwd"]), ("unversioned", "framed", str(project.resolve())))
        omitted_argv = [arg for arg in framed_argv if arg not in ("--shape", "framed")]
        omitted_argv[omitted_argv.index("framed-recordish")] = "omitted-shape"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.main(omitted_argv), 0)
        omitted_route = self.start_calls[-1][0]
        self.assertEqual((omitted_route["source_commit"], omitted_route["selection"]["shape"],
                          omitted_route["cwd"]), ("unversioned", "framed", str(project.resolve())))
        self.assertNotIn("이 세션이 직접 진행", err.getvalue())
        self.assertFalse(project.with_name(project.name + "-wt").exists())
        from dispatch_contract import launch_repository_identity
        self.assertEqual(launch_repository_identity(
            project, self.start_calls[-1][1]), (str(project.resolve()), "non-git"))
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "1"}):
            with self.assertRaisesRegex(ValueError, "git 저장소 아님: .*depth-0 부모"):
                self.main(["capability-route.py", "compose", "--start", "--slug", "depth-refused",
                           "--shape", "direct", "--prompt-file", str(prompt), "--cwd", str(project),
                           "--artifact-root", str(self.artifact_root), "--unassigned", "--jobs", str(self.jobs)])
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
            with self.assertRaisesRegex(ValueError, "git 저장소 아님: .*depth-0 부모"):
                self.main(["capability-route.py", "compose", "--start", "--slug", "registered-refused",
                           "--shape", "direct", "--prompt-file", str(prompt), "--cwd", str(project),
                           "--artifact-root", str(self.artifact_root), "--unassigned", "--jobs", str(self.jobs)])
        self.assertEqual(len(self.route_files()), 2)
        self.assertEqual(len(self.start_calls), 2)
        # Only the two framed routes' own frame legs launched; the refused routes wrote none.
        frame_legs = [node for route in (frame_route, omitted_route) for node in route["nodes"]
                      if node["id"] != "route-decision"]
        self.assertEqual(len(self.owner_attempt_rows()), len(frame_legs))
        launched = {re.search(r"(?:^|,)route_id=([^,\s]+)", row).group(1)
                    for row in self.owner_attempt_rows()}
        self.assertEqual(launched, {frame_route["route_id"], omitted_route["route_id"]})
        self.assertFalse(project.with_name(project.name + "-wt").exists())

    def test_non_git_direct_cleans_dispatch_hints_and_preserves_explicit_capability(self):
        project = Path(self.tmp.name) / "non-git-selection"
        project.mkdir()
        prompt = Path(self.tmp.name) / "non-git-selection.md"
        prompt.write_text("Compare model code outputs as a technology research task.\n", encoding="utf-8")
        demands = Path(self.tmp.name) / "demands.json"
        demands.write_text("{}\n", encoding="utf-8")
        profiles = Path(self.tmp.name) / "profiles.json"
        profiles.write_text("{}\n", encoding="utf-8")
        argv = ["capability-route.py", "compose", "--start", "--slug", "non-git-selection", "--shape", "direct",
                "--prompt-file", str(prompt), "--cwd", str(project), "--artifact-root", str(self.artifact_root),
                "--unassigned", "--jobs", str(self.jobs), "--graph", "execute", "--intensity", "standard",
                "--signal", "shared-contract", "--profile", "light", "--pin", "worker=codex",
                "--profile-demands", str(demands), "--explicit-profiles", str(profiles),
                "--dispatch-evidence", str(self.evidence), "--capability", "autopilot-research",
                "--capability-mode", "technology"]
        self.assertEqual(self.main(argv), 0)
        route, _path = self.start_calls[-1]
        self.assertEqual((route["capability"], route["effective_intensity"]), ("autopilot-research", "direct"))
        self.assertEqual(route["selection"]["promotion_signals"], [])
        self.assertIsNone(route.get("dispatch_evidence"))
        self.assertIn(route.get("profile_demands"), (None, {}))
        self.assertIn(route.get("explicit_profiles"), (None, {}))
        self.assertEqual([node["id"] for node in route["nodes"]], ["inline"])
        self.assertEqual(self.probe_cwds, [])
        self.assertEqual(self.owner_attempt_rows(), [])
        prompt.write_text("계속 진행\n", encoding="utf-8")
        mode_only = ["capability-route.py", "compose", "--start", "--slug", "non-git-mode-only", "--shape", "direct",
                     "--prompt-file", str(prompt), "--cwd", str(project), "--artifact-root", str(self.artifact_root),
                     "--unassigned", "--jobs", str(self.jobs), "--capability-mode", "eval"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.main(mode_only), 0)
        mode_route, _path = self.start_calls[-1]
        self.assertEqual((mode_route["capability"], mode_route["capability_mode"]), ("autopilot-code", "dev"))
        self.assertEqual(mode_route["work_request"]["routing_hints"], {"capability_mode": "eval"})
        self.assertIn("작업 종류 미정 — `--capability`로 지정", err.getvalue())

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

    def test_nonstart_direct_then_start_route_records_the_existing_cwd_bypass(self):
        compose_argv = ["capability-route.py", "compose", "--slug", "nonstart-direct",
                        "--shape", "direct", "--capability", "autopilot-code",
                        "--prompt-file", str(self.prompt), "--cwd", str(self.primary),
                        "--artifact-root", str(self.artifact_root), "--unassigned",
                        "--jobs", str(self.jobs), "--spec-read", "fixture"]
        self.assertEqual(self.main(compose_argv), 0)
        route_file, = self.route_files()
        route = json.loads(route_file.read_text(encoding="utf-8"))
        self.assertEqual(self.start_calls, [])

        start_argv = ["capability-route.py", "start", "--route", str(route_file),
                      "--jobs", str(self.jobs)]
        self.assertEqual(self.main(start_argv), 0)
        self.assertEqual(len(self.start_calls), 1)
        started_route, _route_path = self.start_calls[0]
        observation = {
            "route_file": str(route_file),
            "compose_cwd": route["cwd"],
            "start_cwd": started_route["cwd"],
            "worktree_prepared": bool((self.primary.parent / "repo-wt").exists()),
            "primary_cwd": str(self.primary.resolve()),
        }
        print("direct-compose-start-route-probe=" + json.dumps(observation, sort_keys=True))
        # This records what a sealed route does today without asserting that
        # the unprepared primary cwd is the desired contract. The follow-up
        # remains outside this execute scope.
        self.assertEqual(started_route["cwd"], route["cwd"])


    def test_explicit_worktree_add_failure_removes_its_new_unchanged_branch(self):
        before = self.primary_fingerprint()
        real_git = R._worktree_git

        def explicit_add_failure(cwd, *args):
            if args[:2] == ("worktree", "add"):
                return subprocess.CompletedProcess(["git", *args], 128, "", "explicit fixture failure")
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=explicit_add_failure), \
                self.assertRaisesRegex(ValueError, "source-worktree-preparation-failed"):
            self.main(self.argv())
        record_file, = (self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "reserved")
        self.assertFalse(Path(record["path"]).exists())
        branch = subprocess.run(["git", "-C", str(self.primary), "show-ref", "--verify", "--quiet",
                                 f"refs/heads/{record['branch']}"], capture_output=True)
        self.assertEqual(branch.returncode, 1)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((self.route_files(), self.start_calls, self.owner_attempt_rows()), ([], [], []))

    def test_uncertain_worktree_add_keeps_its_branch_for_normal_resume(self):
        real_git = R._worktree_git

        def uncertain_add(cwd, *args):
            if args[:2] == ("worktree", "add"):
                interrupted = subprocess.CompletedProcess(
                    ["git", *args], 128, "", "simulated interruption after branch creation")
                interrupted.git_uncertain = True
                return interrupted
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=uncertain_add), \
                self.assertRaisesRegex(ValueError, "source-worktree-preparation-failed"):
            self.main(self.argv())
        record_file, = (self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "reserved")
        self.assertEqual(git(self.primary, "rev-parse", f"refs/heads/{record['branch']}"), self.remote_sha)
        self.assertEqual(self.main(self.argv()), 0)
        route, = [json.loads(path.read_text(encoding="utf-8")) for path in self.route_files()]
        self.assertEqual(route["cwd"], record["path"])

    def test_allocator_avoids_tag_dwim_and_default_branch_names(self):
        git(self.primary, "tag", "opencode")
        tag_collision = R.prepare_isolated_worktree(
            self.primary, "opencode", artifact_root=self.artifact_root,
            preparation_key={"kind": "tag-collision"},
        )
        self.assertEqual(tag_collision["state"], "created")
        self.assertEqual(tag_collision["branch"], "opencode-2")
        default_collision = R.prepare_isolated_worktree(
            self.primary, "main", artifact_root=self.artifact_root,
            preparation_key={"kind": "default-collision"},
        )
        self.assertEqual(default_collision["state"], "created")
        self.assertEqual(default_collision["branch"], "main-2")

    def test_failed_branch_cleanup_preserves_moved_and_checked_out_refs(self):
        moved = "moved-task"
        git(self.primary, "branch", moved, "HEAD")
        moved_result = R._cleanup_failed_worktree_branch(self.primary, moved, "0" * len(self.remote_sha))
        self.assertEqual(moved_result["state"], "preserved")
        self.assertEqual(git(self.primary, "rev-parse", f"refs/heads/{moved}"),
                         git(self.primary, "rev-parse", "HEAD"))

        occupied = "occupied-task"
        local_head = git(self.primary, "rev-parse", "HEAD")
        git(self.primary, "branch", occupied, local_head)
        occupied_path = self.primary.parent / "occupied-worktree"
        git(self.primary, "worktree", "add", str(occupied_path), f"refs/heads/{occupied}")
        git(occupied_path, "checkout", "-B", occupied, f"refs/heads/{occupied}")
        occupied_result = R._cleanup_failed_worktree_branch(self.primary, occupied, local_head)
        self.assertEqual(occupied_result["state"], "preserved")
        self.assertEqual(occupied_result["reason"], "branch-worktree-used-or-unobservable")
        self.assertEqual(git(self.primary, "rev-parse", f"refs/heads/{occupied}"), local_head)

    def test_compare_and_swap_preserves_a_ref_moved_during_cleanup(self):
        branch = "cas-race"
        base = git(self.primary, "rev-parse", "HEAD")
        moved = git(self.primary, "rev-parse", "HEAD^")
        git(self.primary, "branch", branch, base)
        real_git = R._worktree_git

        def move_before_cas(cwd, *args):
            if args[:2] == ("update-ref", "-d"):
                git(cwd, "branch", "-f", branch, moved)
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=move_before_cas):
            result = R._cleanup_failed_worktree_branch(self.primary, branch, base)
        self.assertEqual((result["state"], result["reason"]), ("preserved", "compare-and-swap-refused"))
        self.assertEqual(git(self.primary, "rev-parse", f"refs/heads/{branch}"), moved)


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

        def fail_after_ref_allocation(cwd, *args):
            if args[:2] == ("worktree", "add"):
                return subprocess.CompletedProcess(
                    ["git", *args], 128, "", "simulated worktree add failure")
            return real_git(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=fail_after_ref_allocation), \
                self.assertRaisesRegex(ValueError, "source-worktree-preparation-failed"):
            self.main(argv)
        self.assertEqual(self.primary_fingerprint(), before)
        self.assertEqual((self.route_files(), self.start_calls, self.owner_attempt_rows()), ([], [], []))
        record_file, = (self.artifact_root / ".runtime" / "worktree-preparation").glob("*.json")
        record = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertEqual((record["state"], record["base_sha"]), ("reserved", self.remote_sha))
        self.assertFalse(Path(record["path"]).exists())
        branch_state = subprocess.run(
            ["git", "-C", str(self.primary), "show-ref", "--verify", "--quiet",
             f"refs/heads/{record['branch']}"], capture_output=True, text=True)
        self.assertNotEqual(branch_state.returncode, 0)
        resumed_adds = []

        def resume_existing_branch(cwd, *args):
            self.assertNotIn(args[:1], (("ls-remote",), ("fetch",)))
            if args[:2] == ("worktree", "add"):
                self.assertEqual(args, ("worktree", "add", record["path"], f"refs/heads/{record['branch']}"))
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


class PreparationObservationTest(SourceWorktreeFixture):
    def test_same_tip_checks_objects_and_uses_one_inventory_lazily(self):
        git(self.primary, "fetch", "-q", "origin")
        real = R._worktree_git
        calls = []

        def observe(cwd, *args):
            calls.append(args)
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=observe):
            made = R.prepare_isolated_worktree(self.primary, "same-tip", artifact_root=self.artifact_root)
        self.assertEqual(made["state"], "created", made)
        self.assertEqual(made["base_sha"], self.remote_sha)
        self.assertFalse(any(a[0] == "fetch" for a in calls))
        self.assertEqual(sum(a[0] == "rev-list" for a in calls), 1)
        self.assertEqual(sum(a[0] == "for-each-ref" for a in calls), 1)
        self.assertEqual(calls.count(("worktree", "list", "--porcelain")), 1)
        self.assertLess(sum(a[0] == "check-ref-format" for a in calls), 8)
        print("prepare-normal-counts=" + json.dumps({
            "fetch": 0, "refs": 1, "worktrees": 1,
            "check_ref_format": sum(a[0] == "check-ref-format" for a in calls)}))

    def test_linked_identity_does_not_probe_unrelated_worktrees(self):
        linked = self.primary.parent / "linked"
        git(self.primary, "worktree", "add", "-q", "-b", "linked", str(linked))
        real = R._worktree_git
        with mock.patch.object(R, "_worktree_git", wraps=real) as observed:
            made = R.prepare_isolated_worktree(linked, "from-linked", artifact_root=self.artifact_root)
        self.assertEqual(made["state"], "created", made)
        self.assertEqual(sum(c.args[1:] == ("worktree", "list", "--porcelain")
                             for c in observed.call_args_list), 1)

    def test_changed_tip_fetches_and_pins_current_remote(self):
        with mock.patch.object(R, "_worktree_git", wraps=R._worktree_git) as observed:
            made = R.prepare_isolated_worktree(self.primary, "changed-tip", artifact_root=self.artifact_root)
        self.assertEqual(made["state"], "created", made)
        self.assertEqual(made["base_sha"], self.remote_sha)
        self.assertEqual(sum(c.args[1] == "fetch" for c in observed.call_args_list), 1)

    def test_same_tip_missing_blob_uses_fetch_and_refuses_when_fetch_fails(self):
        # Remove only a fixture-owned loose blob after refs agree. rev-parse
        # still resolves the commit, which alone must not authorize reuse.
        git(self.primary, "fetch", "-q", "origin")
        blob = git(self.primary, "rev-parse", self.remote_sha + ":remote-tip.txt")
        common = Path(R._git_common_dir(self.primary))
        (common / "objects" / blob[:2] / blob[2:]).unlink()
        real = R._worktree_git
        calls = []

        def fail_fetch(cwd, *args):
            calls.append(args)
            if args[0] == "fetch":
                return subprocess.CompletedProcess([], 128, "", "fixture missing-object fetch refused")
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=fail_fetch):
            result = R.prepare_isolated_worktree(self.primary, "missing-blob", artifact_root=self.artifact_root)
        self.assertEqual(result["reason"], "remote-default-fetch-failed", result)
        self.assertEqual(sum(a[0] == "fetch" for a in calls), 1)
        self.assertFalse(any(a[:2] == ("worktree", "add") for a in calls))

    def test_unknown_advertised_oid_keeps_existing_fetch_path(self):
        git(self.primary, "fetch", "-q", "origin")
        real = R._worktree_git

        def no_oid(cwd, *args):
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess([], 0, "ref: refs/heads/main\tHEAD\n", "")
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=no_oid) as observed:
            base, reason, _ = R._remote_default_base(self.primary)
        self.assertIsNone(reason)
        self.assertEqual(base["sha"], self.remote_sha)
        self.assertEqual(sum(c.args[1] == "fetch" for c in observed.call_args_list), 1)

    def test_initializing_partial_add_never_becomes_ready(self):
        real = R._worktree_git

        def partial_add(cwd, *args):
            result = real(cwd, *args)
            if args[:2] == ("worktree", "add") and result.returncode == 0:
                path = Path(args[2])
                gitdir = Path(git(path, "rev-parse", "--absolute-git-dir"))
                (gitdir / "locked").write_text("initializing\n")
                (gitdir / "index").unlink()
                result = subprocess.CompletedProcess([], 127, "", "interrupted initializing checkout")
                result.git_uncertain = True
            return result

        with mock.patch.object(R, "_worktree_git", side_effect=partial_add):
            failed = R.prepare_isolated_worktree(self.primary, "partial", artifact_root=self.artifact_root)
        self.assertEqual(failed["state"], "failed", failed)
        with mock.patch.object(R, "_remote_default_base", side_effect=AssertionError("reserved identity")):
            retry = R.prepare_isolated_worktree(self.primary, "partial", artifact_root=self.artifact_root)
        self.assertEqual(retry["reason"], "preparation-identity-conflict", retry)
        record, = (self.artifact_root / ".runtime/worktree-preparation").glob("*.json")
        self.assertEqual(json.loads(record.read_text())["state"], "reserved")
        self.assertTrue((Path(git(failed["path"], "rev-parse", "--absolute-git-dir")) / "locked").exists())

    def test_interruption_in_slow_observation_allocates_no_branch(self):
        real = R._worktree_git

        def interrupted(cwd, *args):
            if args[0] == "ls-remote":
                raise KeyboardInterrupt("fixture slow advertisement cancelled")
            return real(cwd, *args)

        before = git(self.primary, "for-each-ref", "--format=%(refname)")
        with mock.patch.object(R, "_worktree_git", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            R.prepare_isolated_worktree(self.primary, "cancelled", artifact_root=self.artifact_root)
        self.assertEqual(git(self.primary, "for-each-ref", "--format=%(refname)"), before)
        self.assertEqual(list((self.artifact_root / ".runtime/worktree-preparation").glob("*.json")), [])


class GitRefNamespacePreparationTest(SourceWorktreeFixture):
    def test_native_common_git_ref_collision_uses_a_suffix_and_preserves_marker(self):
        common = Path(git(self.primary, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        local_sha = git(self.primary, "rev-parse", "HEAD")
        (common / "opencode").write_text(local_sha + "\n", encoding="utf-8")
        git(self.primary, "tag", "opencode", local_sha)
        git(self.primary, "pack-refs", "--all")
        ambiguous_path = Path(self.tmp.name) / "ambiguous-opencode"
        probe = subprocess.run(
            ["git", "-C", str(self.primary), "worktree", "add", "-b", "opencode",
             str(ambiguous_path), local_sha], capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("ambiguous", probe.stderr.lower())

        result = R.prepare_isolated_worktree(
            self.primary, "opencode", artifact_root=self.artifact_root,
            preparation_key={"request": "native common ref collision"})
        self.assertEqual(result["branch"], "opencode-2")
        self.assertEqual((common / "opencode").read_text(encoding="utf-8"), local_sha + "\n")
        remote = R.prepare_isolated_worktree(
            self.primary, "origin", artifact_root=self.artifact_root,
            preparation_key={"request": "remote ref namespace collision"})
        self.assertEqual(remote["branch"], "origin-2")
        git(self.primary, "symbolic-ref", "refs/tags/symbolic-only", "refs/heads/main")
        symbolic = R.prepare_isolated_worktree(
            self.primary, "symbolic-only", artifact_root=self.artifact_root,
            preparation_key={"request": "symbolic tag collision"})
        self.assertEqual(symbolic["branch"], "symbolic-only-2")
        git(self.primary, "update-ref", "refs/pseudo-name", local_sha)
        pseudo = R.prepare_isolated_worktree(
            self.primary, "pseudo-name", artifact_root=self.artifact_root,
            preparation_key={"request": "refs root collision"})
        self.assertEqual(pseudo["branch"], "pseudo-name-2")
        git(self.primary, "update-ref", "refs/heads/treeish/child", local_sha)
        directory_conflict = R.prepare_isolated_worktree(
            self.primary, "treeish", artifact_root=self.artifact_root,
            preparation_key={"request": "branch d-f collision"})
        self.assertEqual(directory_conflict["branch"], "treeish-2")
        git(self.primary, "branch", "branch-only")
        branch_conflict = R.prepare_isolated_worktree(
            self.primary, "branch-only", artifact_root=self.artifact_root,
            preparation_key={"request": "existing head ref"})
        self.assertEqual(branch_conflict["branch"], "branch-only-2")
        occupied = Path(self.tmp.name) / "repo-wt" / "path-only"
        occupied.mkdir(parents=True)
        (occupied / "preserve.txt").write_text("occupied path\n", encoding="utf-8")
        path_conflict = R.prepare_isolated_worktree(
            self.primary, "path-only", artifact_root=self.artifact_root,
            preparation_key={"request": "occupied worktree path"})
        self.assertEqual(path_conflict["branch"], "path-only-2")
        self.assertEqual((occupied / "preserve.txt").read_text(encoding="utf-8"), "occupied path\n")

    def test_concurrent_distinct_preparations_allocate_distinct_refs(self):
        from concurrent.futures import ThreadPoolExecutor

        local_sha = git(self.primary, "rev-parse", "HEAD")
        preparations = ({"request": "parallel request one"}, {"request": "parallel request two"})
        with mock.patch.object(R, "_remote_default_base",
                               return_value=({"branch": "main", "sha": local_sha}, None, None)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda key: R.prepare_isolated_worktree(
                        self.primary, "parallel", artifact_root=self.artifact_root,
                        preparation_key=key), preparations))
        self.assertEqual({item["state"] for item in results}, {"created"})
        self.assertEqual({item["branch"] for item in results}, {"parallel", "parallel-2"})
        self.assertNotEqual(results[0]["path"], results[1]["path"])
        self.assertEqual({git(item["path"], "rev-parse", "HEAD") for item in results}, {local_sha})

    def test_failed_branch_cleanup_restores_a_concurrently_attached_ref(self):
        branch = "concurrent-cleanup"
        local_sha = git(self.primary, "rev-parse", "HEAD")
        git(self.primary, "update-ref", f"refs/heads/{branch}", local_sha, "0" * len(local_sha))
        worktree = Path(self.tmp.name) / "concurrent-cleanup-worktree"
        real = R._worktree_git
        attached = {"done": False}

        def attach_between_scan_and_cas(cwd, *args):
            if args[:2] == ("rev-parse", "--verify") and not attached["done"]:
                attached["done"] = True
                added = real(self.primary, "worktree", "add", str(worktree), branch)
                self.assertEqual(added.returncode, 0, added.stderr)
            return real(cwd, *args)

        with mock.patch.object(R, "_worktree_git", side_effect=attach_between_scan_and_cas):
            removed = R._release_owned_branch(self.primary, branch, local_sha)

        self.assertFalse(removed)
        self.assertTrue(attached["done"])
        self.assertEqual(git(self.primary, "rev-parse", "--verify", f"refs/heads/{branch}"), local_sha)
        self.assertEqual(git(worktree, "symbolic-ref", "--short", "HEAD"), branch)


if __name__ == "__main__":
    unittest.main()
