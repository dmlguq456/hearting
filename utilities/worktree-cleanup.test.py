#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import os
import importlib.util
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import json
import sys
import signal
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "utilities" / "worktree-cleanup.py"
_CLEANUP_SPEC = importlib.util.spec_from_file_location("worktree_cleanup_for_test", SCRIPT)
CLEANUP = importlib.util.module_from_spec(_CLEANUP_SPEC)
_CLEANUP_SPEC.loader.exec_module(CLEANUP)


def run(
    argv: list[str],
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
):
    result = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"{argv}: {result.stdout}\n{result.stderr}")
    return result


class CleanupFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.repo = self.root / "project"
        self.wt_root = self.root / "project-wt"
        self.jobs = self.root / "jobs.log"
        self.audit = self.root / "cleanup.jsonl"
        run(["git", "init", "--bare", "-q", str(self.remote)])
        run(["git", "clone", "-q", str(self.remote), str(self.repo)])
        run(["git", "-C", str(self.repo), "config", "user.name", "test"])
        run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"])
        (self.repo / "source.txt").write_text("base\n", encoding="utf-8")
        run(["git", "-C", str(self.repo), "add", "source.txt"])
        run(["git", "-C", str(self.repo), "commit", "-qm", "base"])
        run(["git", "-C", str(self.repo), "branch", "-M", "main"])
        run(["git", "-C", str(self.repo), "push", "-qu", "origin", "main"])
        run(["git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main"])
        run(["git", "-C", str(self.repo), "remote", "set-head", "origin", "--auto"])

    def close(self):
        self.temp.cleanup()

    def add_worktree(self, name: str, with_commit: bool = False) -> Path:
        path = self.wt_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", name, str(path), "main"])
        if with_commit:
            (path / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
            run(["git", "-C", str(path), "add", "."])
            run(["git", "-C", str(path), "commit", "-qm", name])
        return path

    def merge_and_push(self, name: str):
        run(["git", "-C", str(self.repo), "merge", "--no-ff", "-qm", f"merge {name}", name])
        run(["git", "-C", str(self.repo), "push", "-q", "origin", "main"])

    def merge_and_push_from_clone(self, name: str):
        run(["git", "-C", str(self.repo), "push", "-q", "origin", name])
        clone = self.root / "merge-clone"
        run(["git", "clone", "-q", str(self.remote), str(clone)])
        run(["git", "-C", str(clone), "config", "user.name", "test"])
        run(["git", "-C", str(clone), "config", "user.email", "test@example.com"])
        run(
            [
                "git", "-C", str(clone), "merge", "--no-ff", "-qm",
                f"merge {name}", f"origin/{name}",
            ]
        )
        run(["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"])

    def cleanup(
        self,
        path: Path,
        apply: bool = False,
        integration_ref: str | None = None,
        repository_mode: str | None = None,
    ):
        action = "--apply" if apply else "--check"
        argv = [str(SCRIPT), action, "--worktree", str(path)]
        if integration_ref is not None:
            argv.extend(["--integration-ref", integration_ref])
        if repository_mode is not None:
            argv.extend(["--repository-mode", repository_mode])
        argv.extend(["--jobs", str(self.jobs), "--audit", str(self.audit)])
        return run(argv, cwd=self.root, check=False)

    def cleanup_via(self, entrypoint: Path, path: Path, extra_env: dict[str, str] | None = None):
        if entrypoint.name == "preflight.sh":
            argv = [str(entrypoint), "worktree-cleanup"]
        else:
            argv = [str(entrypoint)]
        argv.extend(["--check", "--worktree", str(path), "--jobs", str(self.jobs), "--audit", str(self.audit)])
        env = {**os.environ, "AGENT_HOME": str(ROOT)}
        if extra_env:
            env.update(extra_env)
        return run(argv, cwd=self.root, check=False, env=env)

    def fail_git_fetch(self) -> dict[str, str]:
        shim_dir = self.root / "git-shim"
        shim_dir.mkdir()
        shim = shim_dir / "git"
        real_git = shutil.which("git")
        if not real_git:
            raise AssertionError("git executable is not available")
        shim.write_text(
            "#!/bin/sh\n"
            "for arg do\n"
            "  if [ \"$arg\" = fetch ]; then\n"
            "    echo 'injected fetch failure' >&2\n"
            "    exit 128\n"
            "  fi\n"
            "done\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return {"PATH": f"{shim_dir}:{os.environ.get('PATH', '')}"}

    def cleanup_all(self, apply: bool = False):
        action = "--apply" if apply else "--check"
        return run(
            [
                str(SCRIPT),
                action,
                "--all-eligible",
                "--repo",
                str(self.repo),
                "--jobs",
                str(self.jobs),
                "--audit",
                str(self.audit),
            ],
            cwd=self.root,
            check=False,
        )


class WorktreeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fx = CleanupFixture()

    def tearDown(self):
        self.fx.close()

    def test_read_only_status_passes_optional_lock_setting(self):
        path = self.fx.add_worktree("readonly-status")
        real_git = shutil.which("git")
        bindir = self.fx.root / "optional-lock-bin"
        bindir.mkdir()
        log = self.fx.root / "optional-lock-env"
        shim = bindir / "git"
        shim.write_text("#!/bin/sh\n"
                        'for arg do if [ "$arg" = status ]; then\n'
                        'printf "%s\\n" "$GIT_OPTIONAL_LOCKS" >> ' + shlex.quote(str(log)) + '\n'
                        'fi; done\nexec ' + shlex.quote(real_git) + ' "$@"\n')
        shim.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ["PATH"]}):
            CLEANUP.evaluate(path, self.fx.jobs, None)
        self.assertEqual(log.read_text().splitlines(), ["0"])

    def test_default_remote_accepts_merge_from_separate_clone_with_stale_main(self):
        path = self.fx.add_worktree("remote-merged", with_commit=True)
        target_head = run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
        local_main = run(["git", "-C", str(self.fx.repo), "rev-parse", "main"]).stdout.strip()
        self.fx.merge_and_push_from_clone("remote-merged")
        run(["git", "-C", str(self.fx.repo), "checkout", "-qb", "operator-work"])

        result = self.fx.cleanup(path)

        remote_main = run(
            ["git", "--git-dir", str(self.fx.remote), "rev-parse", "refs/heads/main"]
        ).stdout.strip()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=eligible", result.stdout)
        self.assertIn("integration_ref=origin/main", result.stdout)
        self.assertIn(f"integration_commit={remote_main}", result.stdout)
        self.assertEqual(
            run(["git", "-C", str(self.fx.repo), "rev-parse", "main"]).stdout.strip(),
            local_main,
        )
        self.assertEqual(
            run(["git", "-C", str(self.fx.repo), "branch", "--show-current"]).stdout.strip(),
            "operator-work",
        )
        ancestry = run(
            ["git", "-C", str(self.fx.repo), "merge-base", "--is-ancestor", target_head, remote_main],
            check=False,
        )
        self.assertEqual(ancestry.returncode, 0)

    def test_default_remote_works_without_a_local_default_branch(self):
        path = self.fx.add_worktree("without-local-main")
        run(["git", "-C", str(self.fx.repo), "checkout", "-qb", "operator-work"])
        run(["git", "-C", str(self.fx.repo), "branch", "-D", "main"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration_ref=origin/main", result.stdout)

    def test_default_remote_uses_advertised_branch_name(self):
        path = self.fx.add_worktree("alternate-default")
        run(["git", "-C", str(self.fx.repo), "push", "-q", "origin", "main:trunk"])
        run(["git", "--git-dir", str(self.fx.remote), "symbolic-ref", "HEAD", "refs/heads/trunk"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration_ref=origin/trunk", result.stdout)

    def test_remote_fetch_failure_does_not_use_cached_remote_ref(self):
        path = self.fx.add_worktree("fetch-failure", with_commit=True)
        self.fx.merge_and_push("fetch-failure")
        run(["git", "-C", str(self.fx.repo), "fetch", "-q", "origin", "main"])
        original_run = CLEANUP.run

        def fail_fetch(argv, **kwargs):
            if "fetch" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "network unavailable")
            return original_run(argv, **kwargs)

        with mock.patch.object(CLEANUP, "run", side_effect=fail_fetch):
            with self.assertRaisesRegex(RuntimeError, "cannot fetch origin/main"):
                CLEANUP.evaluate(path, self.fx.jobs, None)

    def test_remote_graph_and_ahead_count_errors_fail_closed(self):
        path = self.fx.add_worktree("invalid-git-evidence")
        original_run = CLEANUP.run

        def fail_merge_base(argv, **kwargs):
            if "merge-base" in argv:
                return subprocess.CompletedProcess(argv, 2, "", "invalid graph")
            return original_run(argv, **kwargs)

        with mock.patch.object(CLEANUP, "run", side_effect=fail_merge_base):
            with self.assertRaisesRegex(RuntimeError, "cannot verify worktree HEAD"):
                CLEANUP.evaluate(path, self.fx.jobs, None)

        def invalid_rev_list(argv, **kwargs):
            if "rev-list" in argv:
                return subprocess.CompletedProcess(argv, 0, "bad 0\n", "")
            return original_run(argv, **kwargs)

        with mock.patch.object(CLEANUP, "run", side_effect=invalid_rev_list):
            with self.assertRaisesRegex(RuntimeError, "invalid integration ahead/behind counts"):
                CLEANUP.evaluate(path, self.fx.jobs, None)

    def test_default_remote_query_failure_is_evidence_error(self):
        path = self.fx.add_worktree("remote-query-failure")
        run(["git", "-C", str(self.fx.repo), "remote", "set-url", "origin", str(self.fx.root / "missing.git")])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 3)
        self.assertIn("reasons=evidence-error", result.stdout)

    def test_explicit_local_ref_requires_selected_local_tip_to_include_target(self):
        path = self.fx.add_worktree("explicit-local", with_commit=True)
        self.fx.merge_and_push_from_clone("explicit-local")

        result = self.fx.cleanup(path, integration_ref="main")

        self.assertEqual(result.returncode, 3)
        self.assertIn("unmerged", result.stdout)

    def test_explicit_local_full_ref_uses_local_and_fresh_upstream_tips(self):
        path = self.fx.add_worktree("explicit-local-full", with_commit=True)
        self.fx.merge_and_push("explicit-local-full")

        result = self.fx.cleanup(path, integration_ref="refs/heads/main")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=eligible", result.stdout)

    def test_diverged_local_integration_is_blocked_when_remote_contains_target(self):
        path = self.fx.add_worktree("diverged-main", with_commit=True)
        self.fx.merge_and_push_from_clone("diverged-main")
        run(["git", "-C", str(self.fx.repo), "commit", "--allow-empty", "-qm", "local main ahead"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 3)
        self.assertIn("integration-not-pushed", result.stdout)

    def test_explicit_remote_short_and_full_refs_use_fresh_remote(self):
        for ref in ("origin/main", "refs/remotes/origin/main"):
            with self.subTest(ref=ref):
                fx = CleanupFixture()
                try:
                    path = fx.add_worktree("explicit-remote", with_commit=True)
                    fx.merge_and_push_from_clone("explicit-remote")
                    result = fx.cleanup(path, integration_ref=ref)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("status=eligible", result.stdout)
                finally:
                    fx.close()

    def test_default_remote_ignores_missing_or_stale_origin_head_cache(self):
        path = self.fx.add_worktree("origin-head-cache")
        run(["git", "-C", str(self.fx.repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/old-default"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration_ref=origin/main", result.stdout)

    def test_default_remote_works_when_origin_head_cache_is_missing(self):
        path = self.fx.add_worktree("missing-origin-head")
        run(["git", "-C", str(self.fx.repo), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration_ref=origin/main", result.stdout)

    def test_explicit_local_only_skips_remote_fetch(self):
        path = self.fx.add_worktree("local-only-no-network")
        run(["git", "-C", str(self.fx.repo), "remote", "set-url", "origin", str(self.fx.root / "missing-remote.git")])

        result = self.fx.cleanup(path, integration_ref="main", repository_mode="local-only")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration_upstream=local-only", result.stdout)

    def test_remote_evidence_failure_blocks_apply_without_removing_worktree(self):
        path = self.fx.add_worktree("fetch-failure-apply")
        original_run = CLEANUP.run

        def fail_fetch(argv, **kwargs):
            if "fetch" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "network unavailable")
            return original_run(argv, **kwargs)

        output = io.StringIO()
        with mock.patch.object(CLEANUP, "run", side_effect=fail_fetch):
            with contextlib.redirect_stdout(output):
                result = CLEANUP.main(
                    [
                        str(SCRIPT), "--apply", "--worktree", str(path),
                        "--jobs", str(self.fx.jobs), "--audit", str(self.fx.audit),
                    ]
                )

        self.assertEqual(result, 3)
        self.assertIn("reasons=evidence-error", output.getvalue())
        self.assertTrue(path.exists())

    def test_eligible_removes_worktree_preserves_branch_and_reconciles(self):
        path = self.fx.add_worktree("eligible", with_commit=True)
        self.fx.merge_and_push("eligible")
        self.fx.jobs.write_text(
            f"2026-07-14T00:00:00Z\topen\t{path}\t{path}\teligible\t"
            "attempt_schema_version=2,attempt_id=att-cleanup-current,"
            "dispatch_depth=1,transport=headless,execution_surface=registered-headless,"
            "registered_worker=1,fallback_hop=same-harness-headless,"
            "capability=autopilot-code,harness=codex\n"
            f"2026-07-14T00:00:01Z\topen\t{path}\t{path}\teligible-legacy\t"
            "capability=autopilot-code,harness=codex,depth=1\n",
            encoding="utf-8",
        )
        checked = self.fx.cleanup(path)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("status=eligible", checked.stdout)
        applied = self.fx.cleanup(path, apply=True)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("status=removed", applied.stdout)
        self.assertFalse(path.exists())
        branch = run(
            ["git", "-C", str(self.fx.repo), "show-ref", "--verify", "refs/heads/eligible"],
            check=False,
        )
        self.assertEqual(branch.returncode, 0)
        registry = self.fx.jobs.read_text(encoding="utf-8")
        current = next(line for line in registry.splitlines() if "att-cleanup-current" in line)
        legacy = next(line for line in registry.splitlines() if "eligible-legacy" in line)
        self.assertIn("\tdone\t", current)
        self.assertIn("note=cleanup-merged", current)
        self.assertIn("\topen\t", legacy)
        self.assertNotIn("note=cleanup-merged", legacy)
        self.assertIn('"artifact_harvest_required": false', self.fx.audit.read_text())

    def test_dirty_is_blocked(self):
        path = self.fx.add_worktree("dirty")
        (path / "untracked.txt").write_text("dirty\n", encoding="utf-8")
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 3)
        self.assertIn("dirty", result.stdout)
        self.assertTrue(path.exists())

    def test_unmerged_is_blocked(self):
        path = self.fx.add_worktree("unmerged", with_commit=True)
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 3)
        self.assertIn("unmerged", result.stdout)

    def test_commit_after_remote_merge_is_still_unmerged(self):
        path = self.fx.add_worktree("post-merge-commit", with_commit=True)
        self.fx.merge_and_push("post-merge-commit")
        (path / "after-merge.txt").write_text("not pushed\n", encoding="utf-8")
        run(["git", "-C", str(path), "add", "after-merge.txt"])
        run(["git", "-C", str(path), "commit", "-qm", "commit after remote merge"])

        result = self.fx.cleanup(path)

        self.assertEqual(result.returncode, 3)
        self.assertIn("unmerged", result.stdout)

    def test_locked_is_blocked(self):
        path = self.fx.add_worktree("locked")
        run(["git", "-C", str(self.fx.repo), "worktree", "lock", str(path)])
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 3)
        self.assertIn("locked", result.stdout)

    def test_active_process_is_blocked(self):
        path = self.fx.add_worktree("active")
        proc = subprocess.Popen(["sh", "-c", "while :; do sleep 1; done"], cwd=path)
        try:
            time.sleep(0.1)
            result = self.fx.cleanup(path)
            self.assertEqual(result.returncode, 3)
            self.assertIn("active-process", result.stdout)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_integration_not_pushed_is_blocked(self):
        path = self.fx.add_worktree("push-pending")
        run(["git", "-C", str(self.fx.repo), "commit", "--allow-empty", "-qm", "local main"])
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 3)
        self.assertIn("integration-not-pushed", result.stdout)

    def test_explicit_local_integration_ref_still_requires_upstream(self):
        path = self.fx.add_worktree("no-upstream")
        run(["git", "-C", str(self.fx.repo), "branch", "--unset-upstream", "main"])
        result = self.fx.cleanup(path, integration_ref="main")
        self.assertEqual(result.returncode, 3)
        self.assertIn("no-upstream-configured", result.stdout)

    def test_default_remote_does_not_require_local_upstream_configuration(self):
        path = self.fx.add_worktree("default-no-upstream")
        run(["git", "-C", str(self.fx.repo), "branch", "--unset-upstream", "main"])
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=eligible", result.stdout)

    def test_git_operation_is_still_blocked(self):
        path = self.fx.add_worktree("in-progress-operation")
        for operation in ("merge", "rebase", "cherry-pick"):
            with self.subTest(operation=operation):
                with mock.patch.object(CLEANUP, "git_operation", return_value=operation):
                    verdict = CLEANUP.evaluate(path, self.fx.jobs, None)
                self.assertFalse(verdict.eligible)
                self.assertIn(f"git-operation-{operation}", verdict.reasons)

    def test_unavailable_process_scan_is_still_blocked(self):
        path = self.fx.add_worktree("no-process-scan")
        with mock.patch.object(CLEANUP, "process_cwds", return_value=("unavailable", [])):
            verdict = CLEANUP.evaluate(path, self.fx.jobs, None)
        self.assertFalse(verdict.eligible)
        self.assertIn("process-scan-unavailable", verdict.reasons)

    def test_all_adapters_accept_fresh_remote_merge_with_stale_tracking_ref(self):
        entrypoints = (
            ROOT / "adapters" / "claude" / "bin" / "worktree-cleanup.sh",
            ROOT / "adapters" / "codex" / "bin" / "preflight.sh",
            ROOT / "adapters" / "opencode" / "bin" / "preflight.sh",
        )
        signatures = []
        path = self.fx.add_worktree("adapter-positive", with_commit=True)
        self.fx.merge_and_push_from_clone("adapter-positive")
        local_main = run(
            ["git", "-C", str(self.fx.repo), "rev-parse", "refs/remotes/origin/main"]
        ).stdout.strip()
        remote_main = run(
            ["git", "--git-dir", str(self.fx.remote), "rev-parse", "refs/heads/main"]
        ).stdout.strip()
        self.assertNotEqual(local_main, remote_main)
        run(["git", "-C", str(self.fx.repo), "checkout", "-qb", "operator-work"])

        for entrypoint in entrypoints:
            with self.subTest(entrypoint=entrypoint):
                run(["git", "-C", str(self.fx.repo), "update-ref", "refs/remotes/origin/main", local_main])
                result = self.fx.cleanup_via(entrypoint, path)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("status=eligible", result.stdout)
                fields = dict(
                    line.split("=", 1)
                    for line in result.stdout.splitlines()
                    if "=" in line
                )
                signatures.append(
                    (result.returncode,)
                    + tuple(fields.get(key) for key in (
                        "status", "reasons", "head", "integration_ref",
                        "integration_commit", "repository_mode",
                    ))
                )
        self.assertEqual(len(set(signatures)), 1)

    def test_all_adapters_share_unmerged_and_local_ahead_denials(self):
        entrypoints = (
            ROOT / "adapters" / "claude" / "bin" / "worktree-cleanup.sh",
            ROOT / "adapters" / "codex" / "bin" / "preflight.sh",
            ROOT / "adapters" / "opencode" / "bin" / "preflight.sh",
        )
        for entrypoint in entrypoints:
            for case in ("unmerged", "local-ahead"):
                with self.subTest(entrypoint=entrypoint, case=case):
                    fx = CleanupFixture()
                    try:
                        path = fx.add_worktree(f"adapter-{case}", with_commit=case == "unmerged")
                        if case == "local-ahead":
                            run(["git", "-C", str(fx.repo), "commit", "--allow-empty", "-qm", "local main"])
                        result = fx.cleanup_via(entrypoint, path)
                        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                        expected = "unmerged" if case == "unmerged" else "integration-not-pushed"
                        self.assertIn(expected, result.stdout)
                    finally:
                        fx.close()

    def test_all_adapters_block_when_fetch_fails_despite_cached_inclusion(self):
        entrypoints = (
            ROOT / "adapters" / "claude" / "bin" / "worktree-cleanup.sh",
            ROOT / "adapters" / "codex" / "bin" / "preflight.sh",
            ROOT / "adapters" / "opencode" / "bin" / "preflight.sh",
        )
        for entrypoint in entrypoints:
            with self.subTest(entrypoint=entrypoint):
                fx = CleanupFixture()
                try:
                    path = fx.add_worktree("adapter-fetch-failure", with_commit=True)
                    fx.merge_and_push("adapter-fetch-failure")
                    run(["git", "-C", str(fx.repo), "fetch", "-q", "origin", "main"])
                    result = fx.cleanup_via(entrypoint, path, fx.fail_git_fetch())
                    self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                    self.assertIn("reasons=evidence-error", result.stdout)
                    self.assertIn("injected fetch failure", result.stdout + result.stderr)
                    self.assertTrue(path.exists())
                finally:
                    fx.close()

    def test_explicit_local_only_skips_only_push_sync_gate(self):
        path = self.fx.add_worktree("local-only")
        run(["git", "-C", str(self.fx.repo), "branch", "--unset-upstream", "main"])
        result = run(
            [
                str(SCRIPT), "--check", "--worktree", str(path),
                "--integration-ref", "main", "--repository-mode", "local-only",
                "--jobs", str(self.fx.jobs), "--audit", str(self.fx.audit),
            ],
            cwd=self.fx.root,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("repository_mode=local-only", result.stdout)
        self.assertIn("integration_upstream=local-only", result.stdout)

    def test_primary_is_blocked(self):
        result = self.fx.cleanup(self.fx.repo)
        self.assertEqual(result.returncode, 3)
        self.assertIn("primary-worktree", result.stdout)

    def test_all_eligible_only_considers_job_registry_worktrees(self):
        registered = self.fx.add_worktree("registered")
        unrelated = self.fx.add_worktree("unrelated")
        self.fx.jobs.write_text(
            f"2026-07-14T00:00:00Z\tdone\t{self.fx.repo}\t{registered}\t"
            "registered\tattempt_schema_version=2,attempt_id=att-cleanup-all,"
            "dispatch_depth=1,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,"
            "capability=autopilot-code,harness=codex\n",
            encoding="utf-8",
        )
        result = self.fx.cleanup_all(apply=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(registered.exists())
        self.assertTrue(unrelated.exists())

    def test_all_eligible_legacy_row_cannot_authorize_removal(self):
        registered = self.fx.add_worktree("legacy-only")
        self.fx.jobs.write_text(
            f"2026-07-14T00:00:00Z\tdone\t{self.fx.repo}\t{registered}\t"
            "legacy-only\tcapability=autopilot-code,harness=codex,depth=1\n",
            encoding="utf-8",
        )
        result = self.fx.cleanup_all(apply=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=no-candidates", result.stdout)
        self.assertTrue(registered.exists())

    def test_recycled_pid_row_is_stale_not_active(self):
        # pid 1 is always live but its /proc starttime can never equal the recorded
        # pid_start=1 — a bare-pid probe would call this row active; identity must not.
        path = self.fx.add_worktree("recycled", with_commit=True)
        self.fx.merge_and_push("recycled")
        self.fx.jobs.write_text(
            f"2026-07-14T00:00:00Z\topen\t{path}\t{path}\trecycled\t"
            "capability=autopilot-code,harness=codex,depth=1,pid=1,pid_start=1\n",
            encoding="utf-8",
        )
        result = self.fx.cleanup(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=eligible", result.stdout)
        self.assertIn("stale_registry_rows=1", result.stdout)

    def test_matching_pid_identity_row_is_active(self):
        path = self.fx.add_worktree("identity", with_commit=True)
        self.fx.merge_and_push("identity")
        proc = subprocess.Popen(["sleep", "60"], cwd=self.fx.root)
        try:
            with open(f"/proc/{proc.pid}/stat") as f:
                data = f.read()
            start = data[data.rindex(")") + 1:].split()[19]
            self.fx.jobs.write_text(
                f"2026-07-14T00:00:00Z\topen\t{path}\t{path}\tidentity\t"
                f"capability=autopilot-code,harness=codex,depth=1,"
                f"pid={proc.pid},pid_start={start}\n",
                encoding="utf-8",
            )
            result = self.fx.cleanup(path)
            self.assertEqual(result.returncode, 3)
            self.assertIn("active-process", result.stdout)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_all_eligible_current_axes_without_attempt_cannot_authorize_removal(self):
        registered = self.fx.add_worktree("attemptless")
        self.fx.jobs.write_text(
            f"2026-07-14T00:00:00Z\tdone\t{self.fx.repo}\t{registered}\t"
            "attemptless\tattempt_schema_version=2,dispatch_depth=1,"
            "transport=headless,execution_surface=registered-headless,"
            "registered_worker=1,fallback_hop=same-harness-headless\n",
            encoding="utf-8",
        )
        result = self.fx.cleanup_all(apply=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("status=no-candidates", result.stdout)
        self.assertTrue(registered.exists())


class BatchCleanupTests(unittest.TestCase):
    def setUp(self):
        self.fx = CleanupFixture()
        self.addCleanup(self.fx.close)

    def register(self, paths):
        self.fx.jobs.write_text("".join(
            f"now\tdone\t{self.fx.repo}\t{p}\t{p.name}\t"
            f"attempt_schema_version=2,attempt_id=att-{p.name},dispatch_depth=1,"
            "transport=headless,execution_surface=registered-headless,registered_worker=1,"
            "fallback_hop=same-harness-headless,capability=autopilot-code,harness=codex\n"
            for p in paths))

    def apply(self, *, check=False):
        argv = [str(SCRIPT), "--check" if check else "--apply", "--all-eligible",
                "--repo", str(self.fx.repo), "--jobs", str(self.fx.jobs), "--audit", str(self.fx.audit)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = CLEANUP.main(argv)
        return result, output.getvalue()

    def test_three_removals_share_stable_census_fetch_registry_and_keep_branches(self):
        paths = [self.fx.add_worktree(f"batch-{i}") for i in range(3)]
        unrelated = self.fx.add_worktree("unregistered-keep")
        self.register(paths)
        import dispatch_registry_cache as registry
        registry.clear_cache()
        registry.reset_metrics()
        with mock.patch.object(CLEANUP, "worktree_entries", wraps=CLEANUP.worktree_entries) as census, \
                mock.patch.object(CLEANUP, "fetch_remote_commit", wraps=CLEANUP.fetch_remote_commit) as fetch, \
                mock.patch.object(CLEANUP, "process_snapshot", wraps=CLEANUP.process_snapshot) as proc, \
                mock.patch.object(CLEANUP, "run", wraps=CLEANUP.run) as commands:
            code, output = self.apply()
        self.assertEqual(code, 0, output)
        self.assertEqual((census.call_count, fetch.call_count, proc.call_count), (1, 1, 4))
        self.assertEqual(registry.metrics_snapshot()["content_reads"], 1)
        self.assertEqual(sum(c.args[0][-2:] == ["worktree", "prune"] for c in commands.call_args_list), 1)
        self.assertEqual(sum("merge-base" in c.args[0] for c in commands.call_args_list), 1)
        self.assertEqual(sum("rev-list" in c.args[0] for c in commands.call_args_list), 1)
        self.assertEqual(output.count("status=removed"), 3)
        self.assertEqual(len(self.fx.audit.read_text().splitlines()), 3)
        self.assertTrue(unrelated.exists())
        for path in paths:
            self.assertFalse(path.exists())
            run(["git", "-C", str(self.fx.repo), "show-ref", "--verify", f"refs/heads/{path.name}"])
        print("cleanup-batch-counts=" + json.dumps({"K": 3, "census": 1, "fetch": 1,
              "registry_content_reads": 1, "stable_proc": 1, "mutable_proc_rechecks": 3,
              "prune": 1, "private_worktrees_removed": 3, "branches_removed": 0}))

    def test_mid_pass_mutable_changes_preserve_target(self):
        for change in ("dirty", "untracked", "head", "branch", "index-lock", "operation",
                       "registry", "paused", "retained", "tracking", "local-ref", "remote-url", "new-cwd", "unknown-proc"):
            with self.subTest(change=change):
                path = self.fx.add_worktree("mutable-" + change)
                self.register([path])
                owned = []
                original = CLEANUP.CleanupPass.before_remove
                gitdir = Path(run(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"]).stdout.strip())

                def mutate(context, target, *args):
                    if change == "dirty":
                        (path / "source.txt").write_text("changed\n")
                    elif change == "untracked":
                        (path / "new.txt").write_text("new\n")
                    elif change == "head":
                        run(["git", "-C", str(path), "commit", "--allow-empty", "-qm", "new head"])
                    elif change == "branch":
                        run(["git", "-C", str(path), "checkout", "-qb", "changed-branch"])
                    elif change == "index-lock":
                        (gitdir / "index.lock").write_text("preserve\n")
                    elif change == "operation":
                        (gitdir / "MERGE_HEAD").write_text(run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout)
                    elif change == "registry":
                        with self.fx.jobs.open("a") as stream:
                            stream.write(f"now\topen\t{path}\t{path}\tnew\tpid={os.getpid()}\n")
                    elif change == "paused":
                        with self.fx.jobs.open("a") as stream:
                            stream.write(f"now\tdone\t{path}\t{path}\tpaused\tnote=dead-worker-blocked,failure_class=blocked\n")
                    elif change == "retained":
                        import dispatch_owner_input as owner_input
                        state = owner_input._path(self.fx.jobs, "att-" + path.name)
                        state.parent.mkdir(parents=True, exist_ok=True)
                        state.write_text(json.dumps({"schema_version": 1, "attempt_id": "att-" + path.name,
                                                    "requests": [{"state": "retained"}]}))
                    elif change == "tracking":
                        run(["git", "-C", str(self.fx.repo), "update-ref", "-d", "refs/remotes/origin/main"])
                    elif change == "local-ref":
                        run(["git", "-C", str(self.fx.repo), "commit", "--allow-empty", "-qm", "unpushed"])
                    elif change == "remote-url":
                        run(["git", "-C", str(self.fx.repo), "remote", "set-url", "origin", str(self.fx.root / "unknown-remote")])
                    elif change == "new-cwd":
                        owned.append(subprocess.Popen(["sleep", "30"], cwd=path))
                    if change == "unknown-proc":
                        with mock.patch.object(CLEANUP, "process_cwds", return_value=("procfs-unknown", [])):
                            return original(context, target, *args)
                    return original(context, target, *args)

                try:
                    with mock.patch.object(CLEANUP.CleanupPass, "before_remove", mutate):
                        code, output = self.apply()
                    self.assertEqual(code, 3, output)
                    self.assertTrue(path.exists(), output)
                    self.assertNotIn("status=removed", output)
                    self.assertFalse(self.fx.audit.exists())
                finally:
                    for child in owned:
                        child.terminate()
                        child.wait(timeout=5)
                # Each subtest owns a new independent fixture, including refs.
                self.fx.close()
                self.fx = CleanupFixture()
                self.addCleanup(self.fx.close)

    def test_pass_interrupt_after_first_remove_audits_and_prunes_once(self):
        paths = [self.fx.add_worktree(f"interrupt-{i}") for i in range(3)]
        self.register(paths)
        original = CLEANUP.CleanupPass.before_remove
        calls = []
        def interrupt(context, target, *args):
            calls.append(target)
            if len(calls) == 2:
                raise KeyboardInterrupt("fixture pass cancelled")
            return original(context, target, *args)
        with mock.patch.object(CLEANUP.CleanupPass, "before_remove", interrupt), \
                mock.patch.object(CLEANUP, "run", wraps=CLEANUP.run) as commands, \
                self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertFalse(paths[0].exists())
        self.assertTrue(all(p.exists() for p in paths[1:]))
        self.assertEqual(len(self.fx.audit.read_text().splitlines()), 1)
        self.assertEqual(sum(c.args[0][-2:] == ["worktree", "prune"] for c in commands.call_args_list), 1)

    def test_observation_timeout_drains_detached_owned_child(self):
        pidfile = self.fx.root / "probe.pid"
        program = ("import subprocess,sys,time; "
                   "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
                   "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                   "open(sys.argv[1],'w').write(str(p.pid)); time.sleep(30)")
        with mock.patch.object(CLEANUP, "OBSERVATION_TIMEOUT", 0.1), self.assertRaisesRegex(RuntimeError, "timed out"):
            CLEANUP.run([sys.executable, "-c", program, str(pidfile)])
        self.assertFalse(Path("/proc", pidfile.read_text()).exists())

    def test_observation_cancel_drains_owned_child_and_restores_handler(self):
        pidfile = self.fx.root / "cancel.pid"
        program = ("import os,sys,signal,time; open(sys.argv[1],'w').write(str(os.getpid())); "
                   "time.sleep(.1); os.kill(os.getppid(),signal.SIGTERM); time.sleep(30)")
        before = signal.getsignal(signal.SIGTERM)
        # The immediate parent is the private observer. Its cancellation
        # becomes an unavailable result after exact descendant teardown.
        with self.assertRaisesRegex(RuntimeError, "observation unavailable"):
            CLEANUP.run([sys.executable, "-c", program, str(pidfile)])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse(Path("/proc", pidfile.read_text()).exists())

    def test_observation_does_not_claim_callers_other_children(self):
        sibling = subprocess.Popen(["sleep", "30"], cwd=self.fx.root)
        try:
            CLEANUP.run([sys.executable, "-c", "pass"])
            self.assertIsNone(sibling.poll())
            self.assertEqual(os.readlink(f"/proc/{sibling.pid}/cwd"), str(self.fx.root))
        finally:
            sibling.terminate()
            sibling.wait(timeout=5)

    def test_unsettled_observation_stops_later_removals_and_retains_partial_audit(self):
        paths = [self.fx.add_worktree(f"unsettled-{i}") for i in range(3)]
        self.register(paths)
        original = CLEANUP.run
        def unknown(argv, **kwargs):
            if "status" in argv and str(paths[1]) in argv:
                raise CLEANUP.UnsettledObservation("fixture unreadable owned descendant")
            return original(argv, **kwargs)
        with mock.patch.object(CLEANUP, "run", side_effect=unknown) as calls:
            code, output = self.apply()
        self.assertEqual(code, 3, output)
        self.assertIn("observation-descendants-unresolved", output)
        self.assertFalse(paths[0].exists())
        self.assertTrue(all(p.exists() for p in paths[1:]))
        self.assertEqual(len(self.fx.audit.read_text().splitlines()), 1)
        self.assertFalse(any(c.args[0][-2:] == ["worktree", "prune"] for c in calls.call_args_list))

    def test_owner_input_unknown_or_unfinished_state_is_not_deletion_authority(self):
        import dispatch_owner_input as owner_input
        path = self.fx.add_worktree("input-guard")
        self.register([path])
        rows = CLEANUP.registry_rows(self.fx.jobs)
        state = owner_input._path(self.fx.jobs, "att-" + path.name)
        state.parent.mkdir(parents=True, exist_ok=True)
        for requests, protected in ((None, True), ([{}], True), ([{"state": "future-unknown"}], True),
                ([{"state": "accepted"}], True), ([{"state": "sending"}], True),
                ([{"state": "delivery-unknown"}], True), ([{"state": "retained"}], True),
                ([{"state": "turn-completed"}], False)):
            with self.subTest(requests=requests):
                state.write_text(json.dumps({"schema_version": 1, "attempt_id": "att-" + path.name,
                                             "requests": requests}))
                self.assertEqual(CLEANUP.registry_duty(self.fx.jobs, path, rows), protected)

    def test_proc_cwd_map_is_lexical_and_unreadable_same_uid_is_unknown(self):
        entry = self.fx.root / "123456"
        entry.mkdir()
        native_iterdir = Path.iterdir
        def entries(path):
            return iter([entry]) if path == Path("/proc") else native_iterdir(path)
        with mock.patch.object(Path, "iterdir", entries), \
                mock.patch.object(CLEANUP, "read_proc_start", return_value="100"), \
                mock.patch.object(CLEANUP.os, "readlink", return_value=str(self.fx.repo / "sub")), \
                mock.patch.object(CLEANUP, "normalize", side_effect=AssertionError("no NAS resolve")):
            kind, rows = CLEANUP.process_snapshot()
            self.assertEqual((kind, rows[0][2]), ("procfs", self.fx.repo / "sub"))
        with mock.patch.object(Path, "iterdir", entries), \
                mock.patch.object(CLEANUP, "read_proc_start", return_value="100"), \
                mock.patch.object(CLEANUP.os, "readlink", side_effect=PermissionError("unknown")):
            self.assertEqual(CLEANUP.process_snapshot()[0], "procfs-unknown")


if __name__ == "__main__":
    unittest.main()
