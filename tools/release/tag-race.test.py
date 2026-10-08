#!/usr/bin/env python3
"""Run sequential release jobs against a real remote and stale tag snapshots."""
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("release_plan", HERE / "plan.py")
PLAN = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PLAN)


class TagRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        self.source = self.root / "source"
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        self.run_git(self.root, "init", "--bare", "-q", str(self.remote))
        self.run_git(self.root, "init", "-q", str(self.source))
        self.commit("initial")
        self.run_git(self.source, "tag", "v1.0.0")
        self.run_git(self.source, "remote", "add", "origin", str(self.remote))
        self.a = self.commit("fix: first merge")
        self.b = self.commit("fix: second merge")
        self.run_git(self.source, "push", "-q", "origin", "HEAD", "--tags")
        self.jobs = []
        for index in range(2):
            job = self.root / f"job-{index}"
            self.run_git(self.root, "clone", "-q", str(self.remote), str(job))
            self.jobs.append(job)
        self.published = {"v1.0.0"}
        self.created_refs = []
        self.collision = None

    def run_git(self, repo, *args, input=None):
        return subprocess.check_output(["git", "-C", str(repo), *args], input=input,
                                       text=True, env=self.env, stderr=subprocess.DEVNULL).strip()

    def commit(self, message):
        path = self.source / "core" / "contract.md"
        path.parent.mkdir(exist_ok=True)
        with path.open("a") as file:
            file.write(message + "\n")
        self.run_git(self.source, "add", ".")
        self.run_git(self.source, "commit", "-qm", message)
        return self.run_git(self.source, "rev-parse", "HEAD")

    def ref(self, version, head, *, publish=True):
        self.run_git(self.remote, "update-ref", f"refs/tags/{version}", head)
        if publish:
            self.published.add(version)

    def api(self, endpoint, *, method="GET", **fields):
        if endpoint.startswith("releases/tags/"):
            version = endpoint.rsplit("/", 1)[1]
            return {"tag_name": version, "draft": False, "prerelease": False} if version in self.published else None
        if endpoint.startswith("git/ref/tags/"):
            version = endpoint.rsplit("/", 1)[1]
            try:
                sha = self.run_git(self.remote, "rev-parse", "--verify", f"refs/tags/{version}")
            except subprocess.CalledProcessError:
                return None
            return {"object": {"type": self.run_git(self.remote, "cat-file", "-t", sha), "sha": sha}}
        if endpoint.startswith("git/tags/"):
            body = self.run_git(self.remote, "cat-file", "-p", endpoint.rsplit("/", 1)[1])
            fields = dict(line.split(" ", 1) for line in body.split("\n\n", 1)[0].splitlines())
            return {"object": {"type": fields["type"], "sha": fields["object"]}}
        if endpoint == "git/tags" and method == "POST":
            tag = (f"object {fields['object']}\ntype commit\ntag {fields['tag']}\n"
                   "tagger Fixture <fixture@example.invalid> 1700000000 +0000\n\n"
                   f"{fields['message']}\n")
            return {"sha": self.run_git(self.remote, "mktag", input=tag)}
        if endpoint == "git/refs" and method == "POST":
            version = fields["ref"].rsplit("/", 1)[1]
            if self.collision:
                callback, self.collision = self.collision, None
                callback(version)
            if self.api(f"git/ref/tags/{version}") is not None:
                raise PLAN.GitHubError("reference already exists", 422)
            self.run_git(self.remote, "update-ref", fields["ref"], fields["sha"])
            self.created_refs.append(version)
            return {}
        self.fail((endpoint, method, fields))

    def prepare(self, index, head, **kwargs):
        return PLAN.prepare(self.jobs[index], head, api=self.api, **kwargs)

    def test_two_merges_old_then_new_refresh_the_second_checkout(self):
        first = self.prepare(0, self.a)
        self.assertEqual(first["version"], "v1.0.1")
        self.published.add(first["version"])
        second = self.prepare(1, self.b)
        self.assertEqual(second["version"], "v1.0.2")
        self.assertEqual(PLAN.tag_commit(self.api, second["version"]), self.b)
        self.assertEqual(PLAN.tag_commit(self.api, first["version"]), self.a)

    def test_newer_merge_published_before_delayed_older_job_skips_successfully(self):
        first = self.prepare(0, self.b)
        self.published.add(first["version"])
        second = self.prepare(1, self.a)
        self.assertFalse(second["release"])
        self.assertEqual(second["reason"], "already-published")
        self.assertEqual(self.created_refs, ["v1.0.1"])

    def test_same_commit_published_twice_does_not_create_another_tag(self):
        first = self.prepare(0, self.a)
        self.published.add(first["version"])
        second = self.prepare(1, self.a)
        self.assertFalse(second["release"])
        self.assertEqual(self.created_refs, ["v1.0.1"])

    def test_reserved_tag_without_release_resumes_the_exact_version(self):
        first = self.prepare(0, self.a)
        second = self.prepare(1, self.a)
        self.assertTrue(second["release"])
        self.assertEqual(second["version"], first["version"])
        self.assertEqual(second["reason"], "resume-existing-tag")
        self.assertEqual(self.created_refs, ["v1.0.1"])

    def test_two_interrupted_publications_retry_newer_commit_before_older_commit(self):
        self.assertEqual(self.prepare(0, self.b)["version"], "v1.0.1")
        self.assertEqual(self.prepare(1, self.a)["version"], "v1.0.2")
        resumed_b = self.prepare(0, self.b)
        self.published.add(resumed_b["version"])
        result = self.prepare(1, self.a)
        self.assertFalse(result["release"])
        self.assertEqual(result["reason"], "already-published")
        self.assertEqual(self.created_refs, ["v1.0.1", "v1.0.2"])

    def test_higher_orphan_for_same_commit_does_not_hide_published_version(self):
        self.ref("v1.0.1", self.a)
        self.ref("v1.0.2", self.a, publish=False)
        self.assertFalse(self.prepare(0, self.a)["release"])
        self.assertEqual(self.created_refs, [])

    def test_unpublished_descendant_does_not_skip_exact_orphan_resume(self):
        self.ref("v1.0.1", self.b, publish=False)
        self.ref("v1.0.2", self.a, publish=False)
        result = self.prepare(0, self.a)
        self.assertTrue(result["release"])
        self.assertEqual(result["version"], "v1.0.2")
        self.assertEqual(result["reason"], "resume-existing-tag")

    def test_publication_lookup_failure_after_orphan_is_not_hidden_by_resume(self):
        self.ref("v1.0.1", self.b)
        self.ref("v1.0.2", self.a, publish=False)
        for status in (401, 403, 500):
            with self.subTest(status=status):
                def failed(endpoint, **fields):
                    if endpoint == "releases/tags/v1.0.1":
                        raise PLAN.GitHubError("lookup failed", status)
                    return self.api(endpoint, **fields)
                with self.assertRaises(PLAN.GitHubError):
                    PLAN.prepare(self.jobs[0], self.a, api=failed)

    def test_tag_created_by_other_commit_between_lookup_and_create_replans(self):
        self.collision = lambda version: self.ref(version, self.a)
        result = self.prepare(0, self.b)
        self.assertTrue(result["release"])
        self.assertEqual(result["version"], "v1.0.2")
        self.assertEqual(PLAN.tag_commit(self.api, "v1.0.1"), self.a)
        self.assertEqual(PLAN.tag_commit(self.api, "v1.0.2"), self.b)

    def test_same_commit_creation_race_resumes_without_moving_the_tag(self):
        self.collision = lambda version: self.ref(version, self.a, publish=False)
        result = self.prepare(0, self.a)
        self.assertTrue(result["release"])
        self.assertEqual(result["version"], "v1.0.1")
        self.assertEqual(result["reason"], "resume-existing-tag")
        self.assertEqual(self.created_refs, [])

    def test_unrelated_tag_sets_version_floor_without_replacing_its_commit(self):
        self.run_git(self.source, "checkout", "-q", "v1.0.0")
        foreign = self.commit("fix: other branch")
        self.run_git(self.source, "push", "-q", "origin", "HEAD:refs/heads/other")
        self.ref("v1.0.1", foreign)
        result = self.prepare(0, self.a)
        self.assertEqual(result["version"], "v1.0.2")
        self.assertEqual(PLAN.tag_commit(self.api, "v1.0.1"), foreign)

    def test_manual_prerelease_is_reused_and_mismatch_is_rejected(self):
        self.ref("v2.0.0-rc.1", self.a, publish=False)
        result = self.prepare(0, self.a, version="v2.0.0-rc.1")
        self.assertEqual(result["version"], "v2.0.0-rc.1")
        self.assertEqual(result["mode"], "tag")
        self.published.add("v2.0.0-rc.1")
        self.assertFalse(self.prepare(0, self.a, version="v2.0.0-rc.1")["release"])
        with self.assertRaises(PLAN.PlanError):
            self.prepare(0, self.b, version="v2.0.0-rc.1")

    def test_auth_failure_is_not_misclassified_as_a_tag_collision(self):
        api = self.api
        def denied(endpoint, **fields):
            if endpoint == "git/refs":
                raise PLAN.GitHubError("permission denied", 403)
            return api(endpoint, **fields)
        with self.assertRaises(PLAN.GitHubError):
            PLAN.prepare(self.jobs[0], self.a, api=denied)
        self.assertEqual(self.created_refs, [])

    def test_workflow_builds_and_publishes_the_reserved_version(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        self.assertLess(workflow.index('"$task_release_plan" prepare'), workflow.index("Build deterministic release assets"))
        self.assertNotIn("release tag already exists", workflow)
        self.assertNotIn("Create the planned tag", workflow)
        self.assertEqual(workflow.count("RELEASE_TAG: ${{ steps.plan.outputs.version }}"), 4)
        self.assertIn('--version "$RELEASE_TAG"', workflow)
        self.assertIn('--verify-tag', workflow)
        self.assertIn('--generate-notes', workflow)

    def test_delayed_candidate_uses_workflow_policy_without_replacing_asset_source(self):
        # The old tested source has no prepare command or planner file at all.
        target = self.source / "tools/release/plan.py"
        target.parent.mkdir(parents=True)
        target.write_bytes((HERE / "plan.py").read_bytes())
        self.run_git(self.source, "add", ".")
        self.run_git(self.source, "commit", "-qm", "fix: release policy")
        policy = self.run_git(self.source, "rev-parse", "HEAD")
        self.run_git(self.source, "push", "-q", "origin", "HEAD")
        job = self.jobs[0]
        self.run_git(job, "fetch", "origin")
        self.run_git(job, "checkout", "-q", self.a)
        self.assertFalse((job / "tools/release/plan.py").exists())
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        block = workflow.split('- name: Plan release\n', 1)[1].split('\n      - name:', 1)[0]
        script = block.split('        run: |\n', 1)[1].split('          python3 ', 1)[0]
        script = '\n'.join(line[10:] for line in script.splitlines())
        subprocess.run(['bash', '-e', '-c', script], cwd=job, check=True,
                       env=dict(self.env, RUNNER_TEMP=str(self.root), GITHUB_WORKFLOW_SHA=policy))
        copied = self.root / 'hearting-release-plan.py'
        spec = importlib.util.spec_from_file_location('pinned_policy', copied)
        pinned = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pinned)
        result = pinned.prepare(job, self.a, api=self.api)
        self.assertTrue(result['release'])
        self.assertEqual(result['head'], self.a)
        self.assertEqual(self.run_git(job, 'rev-parse', 'HEAD'), self.a)
        self.assertEqual(PLAN.tag_commit(self.api, result['version']), self.a)


if __name__ == "__main__":
    unittest.main()
