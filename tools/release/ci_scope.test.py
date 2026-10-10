#!/usr/bin/env python3
"""Exercise change selection against real Git histories and release baselines."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ci_scope as SCOPE


class GitRepoCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-q")
        self.commit("core/CORE.md")
        self.git("tag", "v1.0.0")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.context = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def commit(self, path):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(p.read_text() + "change\n" if p.exists() else "change\n")
        self.git("add", "--", path)
        self.git("commit", "-qm", "fixture")

    def select(self, event=None, released_tag="v1.0.0", **context):
        return SCOPE.select(self.repo, event or {}, dict(self.context, **context),
                            released_tag=released_tag)[0]


class ScopeTest(GitRepoCase):
    def test_public_documents_and_project_bootstrap_skip_expensive_suites(self):
        for path in ("README.md", "docs/example.md", "AGENTS.md"):
            self.commit(path)
        (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
        self.git("add", "CLAUDE.md")
        self.git("commit", "-qm", "project bootstrap link")
        self.assertFalse(self.select())
        self.assertTrue(all(not SCOPE.PLAN.is_release_relevant(path)
                            for path in ("README.md", "docs/example.md", "AGENTS.md", "CLAUDE.md")))

    def test_docs_cannot_hide_unreleased_code_from_previous_push(self):
        self.commit("utilities/runtime.py")
        self.commit("README.md")
        self.assertTrue(self.select())
        self.git("tag", "v1.0.1")
        self.commit("README.md")
        self.assertTrue(self.select())  # Tag exists, but is not published yet.
        self.assertFalse(self.select(released_tag="v1.0.1"))

    def test_runtime_instructions_tests_and_unknown_paths_still_run_full(self):
        for path in ("core/CORE.md", "capabilities/test.md", "roles/README.md",
                     "adapters/codex/AGENTS.md", ".github/workflows/checks.yml",
                     "tools/example.test.py", "unknown.md", "docs/example.py", ".gitignore"):
            with self.subTest(path=path):
                self.assertFalse(SCOPE.documentation_path(path))

    def test_runtime_file_renamed_into_docs_does_not_skip_checks(self):
        (self.repo / "docs").mkdir()
        self.git("mv", "core/CORE.md", "docs/core.md")
        self.git("commit", "-qm", "move")
        self.assertTrue(self.select())

    def test_missing_tag_or_invalid_pr_base_runs_full(self):
        self.commit("README.md")
        self.git("tag", "-d", "v1.0.0")
        self.assertTrue(self.select())
        for event in ({}, {"pull_request": {"base": {"sha": "missing"}}}):
            self.assertTrue(self.select(event, GITHUB_EVENT_NAME="pull_request"))

    def test_pr_uses_merge_base_and_manual_tag_validation_is_full(self):
        self.commit("README.md")
        event = {"pull_request": {"base": {"sha": self.base}}}
        self.assertFalse(self.select(event, GITHUB_EVENT_NAME="pull_request"))
        self.assertTrue(self.select(GITHUB_EVENT_NAME="workflow_dispatch"))
        self.assertTrue(self.select(GITHUB_EVENT_NAME="workflow_call"))
        self.assertTrue(self.select(GITHUB_REF="refs/tags/v1.0.1"))
        self.commit("tools/runtime.py")
        self.assertTrue(self.select(event, GITHUB_EVENT_NAME="pull_request"))

    def test_cli_unknown_event_prints_full_validation(self):
        event = self.repo / "event.json"
        event.write_text(json.dumps({}))
        result = subprocess.run(
            ["python3", str(SCOPE.ROOT / "tools/release/ci_scope.py")], capture_output=True, text=True,
            env=dict(os.environ, GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_EVENT_PATH=str(event)),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("full_tests=true\n", result.stdout)


class ValidatedTreeTest(GitRepoCase):
    """PR/main trees already tested by a successful same-repository full PR run."""

    REPO = "owner/repo"

    def setUp(self):
        super().setUp()
        self.commit("tools/runtime.py")
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        self.context.update(GH_TOKEN="token", GITHUB_REPOSITORY=self.REPO, GITHUB_REPOSITORY_ID="7",
                            GITHUB_SHA=self.git("rev-parse", "HEAD").strip())
        self.calls = []
        self.artifacts = [self.artifact(11)]
        self.runs = {11: self.run_record()}

    def artifact(self, run_id, tree=None, **fields):
        return dict({"name": f"validated-tree-{tree or self.tree}", "expired": False,
                     "workflow_run": {"id": run_id}}, **fields)

    def run_record(self, **fields):
        base = {"id": 7, "full_name": self.REPO}
        return dict({"repository": base, "head_repository": dict(base),
                     "path": ".github/workflows/checks.yml", "event": "pull_request",
                     "status": "completed", "conclusion": "success", "run_attempt": 1}, **fields)

    def api(self, path):
        self.calls.append(path)
        if "/actions/artifacts?" in path:
            return {"artifacts": self.artifacts}
        return self.runs[int(path.rsplit("/", 1)[1])]

    def chosen(self, api=None, **context):
        return SCOPE.select(self.repo, {}, dict(self.context, **context), released_tag="v1.0.0",
                            api=api or self.api)

    def test_same_tree_from_a_successful_pr_run_skips_the_expensive_jobs(self):
        self.assertEqual(self.chosen(), (False, f"validated-pr-tree:{self.tree}"))
        self.assertIn(f"name=validated-tree-{self.tree}", self.calls[0])

    def test_rerun_that_ended_in_success_still_counts(self):
        self.runs[11] = self.run_record(run_attempt=2)
        self.assertFalse(self.chosen()[0])

    def test_a_later_candidate_can_vouch_when_the_first_does_not(self):
        self.artifacts = [self.artifact(12), self.artifact(11)]
        self.runs[12] = self.run_record(conclusion="failure")
        self.assertEqual(self.chosen()[0], False)

    def test_other_tree_or_no_candidate_runs_full(self):
        self.artifacts = [self.artifact(11, tree="0" * 40)]
        self.assertEqual(self.chosen(), (True, "runtime-test-or-unclassified-change"))
        self.artifacts = []
        self.assertTrue(self.chosen()[0])

    def test_run_that_is_not_a_clean_same_repository_pr_success_runs_full(self):
        other = {"id": 8, "full_name": "fork/repo"}
        changes = {
            "fork": {"head_repository": other},
            "other repository": {"repository": other, "head_repository": other},
            "failed": {"conclusion": "failure"},
            "cancelled": {"conclusion": "cancelled"},
            "no conclusion": {"conclusion": None},
            "running": {"status": "in_progress", "conclusion": None},
            "other workflow": {"path": ".github/workflows/release.yml"},
            "push event": {"event": "push"},
            "fork without repository": {"head_repository": None},
        }
        for name, fields in changes.items():
            with self.subTest(name):
                self.runs[11] = self.run_record(**fields)
                self.assertEqual(self.chosen(), (True, "runtime-test-or-unclassified-change"))

    def test_expired_or_renamed_artifact_is_not_evidence(self):
        for fields in ({"expired": True}, {"expired": None}, {"name": "validated-tree-" + "1" * 40}):
            with self.subTest(fields):
                self.artifacts = [self.artifact(11, **fields)]
                self.assertTrue(self.chosen()[0])

    def test_repository_id_must_match_when_given(self):
        self.assertTrue(self.chosen(GITHUB_REPOSITORY_ID="8")[0])
        self.context.pop("GITHUB_REPOSITORY_ID")
        self.assertFalse(self.chosen()[0])

    def test_api_permission_network_and_format_failures_run_full(self):
        errors = (subprocess.CalledProcessError(1, "gh"), subprocess.TimeoutExpired("gh", 15),
                  FileNotFoundError("gh"), ValueError("not json"), KeyError("artifacts"), TypeError("shape"))
        for error in errors:
            def broken(path, error=error):
                raise error
            with self.subTest(type(error).__name__):
                self.assertEqual(self.chosen(broken), (True, "runtime-test-or-unclassified-change"))
        for listing in ({}, {"artifacts": None}, {"artifacts": [{"name": "x"}]}, []):
            with self.subTest(listing=listing):
                self.assertTrue(self.chosen(lambda path, listing=listing: listing)[0])
        self.runs[11] = {"conclusion": "success"}
        self.assertTrue(self.chosen()[0])

    def test_missing_token_or_bad_repository_never_reaches_the_api(self):
        for context in ({"GH_TOKEN": ""}, {"GITHUB_REPOSITORY": "not a repo"}, {"GITHUB_REPOSITORY": ""}):
            with self.subTest(context):
                self.calls.clear()
                self.assertTrue(self.chosen(**context)[0])
                self.assertEqual(self.calls, [])

    def test_pr_rebase_reuses_the_same_authenticated_tree_without_new_full_ci(self):
        event = {"pull_request": {"base": {"sha": self.base}}}
        # An empty commit changes head identity while preserving all tested bytes.
        self.git("commit", "--allow-empty", "-qm", "rebased equivalent head")
        self.context["GITHUB_SHA"] = self.git("rev-parse", "HEAD").strip()
        pr = SCOPE.select(self.repo, event, dict(self.context, GITHUB_EVENT_NAME="pull_request"), api=self.api)
        self.assertEqual(pr, (False, f"validated-pr-tree:{self.tree}"))
        self.assertIn(f"name=validated-tree-{self.tree}", self.calls[0])

    def test_changed_pr_tree_failed_run_and_unavailable_lookup_retain_full_ci(self):
        event = {"pull_request": {"base": {"sha": self.base}}}
        context = dict(self.context, GITHUB_EVENT_NAME="pull_request")
        self.runs[11] = self.run_record(conclusion="failure")
        self.assertTrue(SCOPE.select(self.repo, event, context, api=self.api)[0])
        self.runs[11] = self.run_record()
        self.commit("tools/new-runtime.py")
        context["GITHUB_SHA"] = self.git("rev-parse", "HEAD").strip()
        self.assertTrue(SCOPE.select(self.repo, event, context, api=self.api)[0])
        self.assertTrue(SCOPE.select(self.repo, event, context, api=lambda _: {})[0])

    def test_manual_tag_and_release_validation_never_use_tree_reuse(self):
        for context in ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, {"GITHUB_EVENT_NAME": "workflow_call"},
                        {"GITHUB_REF": "refs/tags/v1.0.1"}):
            with self.subTest(context):
                self.assertEqual(self.chosen(**context), (True, "explicit-full-validation"))
        self.assertEqual(self.calls, [])

    def test_documentation_only_keeps_its_reason_without_a_lookup(self):
        self.git("tag", "v1.0.1")
        self.commit("README.md")
        result = SCOPE.select(self.repo, {}, dict(self.context, GITHUB_SHA=self.git("rev-parse", "HEAD").strip()),
                              released_tag="v1.0.1", api=self.api)
        self.assertEqual(result, (False, "documentation-only"))
        self.assertEqual(self.calls, [])

    def test_gh_api_bounds_the_call_and_parses_json(self):
        done = subprocess.CompletedProcess([], 0, stdout='{"artifacts": []}')
        with patch.object(SCOPE.subprocess, "run", return_value=done) as run:
            self.assertEqual(SCOPE.gh_api("repos/o/r/actions/artifacts"), {"artifacts": []})
        self.assertEqual(run.call_args.args[0], ["gh", "api", "repos/o/r/actions/artifacts"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertTrue(run.call_args.kwargs["check"])


if __name__ == "__main__":
    unittest.main()
