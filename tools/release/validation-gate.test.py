#!/usr/bin/env python3
"""Exercise release admission and the workflow's prerequisite wiring."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("validation_gate", HERE / "validation-gate.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)
HEAD = "a" * 40
TIP = "b" * 40
REPO = {"full_name": "owner/repo", "id": 42}


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.context = {
            "GITHUB_REPOSITORY": "owner/repo", "GITHUB_REPOSITORY_ID": "42",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": HEAD,
            "GITHUB_REF": "refs/heads/main",
        }
        self.event = {"repository": copy.deepcopy(REPO)}

    def checks_run(self, **changes):
        run = {"name": "Checks", "path": ".github/workflows/checks.yml",
               "event": "push", "status": "completed", "conclusion": "success",
               "head_branch": "main", "head_sha": HEAD, "head_commit": {"id": HEAD},
               "repository": copy.deepcopy(REPO), "head_repository": copy.deepcopy(REPO)}
        run.update(changes)
        return {"workflow_runs": [run]}

    def test_exact_successful_main_checks_are_reused(self):
        calls = []
        result = GATE.admit(self.event, self.context,
                            api=lambda endpoint: calls.append(endpoint) or self.checks_run())
        self.assertEqual(result, {"head": HEAD, "validation_required": "false"})
        self.assertEqual(len(calls), 1)
        self.assertIn("head_sha=" + HEAD, calls[0])
        self.assertIn("per_page=20", calls[0])

    def test_malformed_or_untrusted_checks_fall_back_to_validation(self):
        bad_runs = [
            self.checks_run(head_sha=TIP),
            self.checks_run(head_commit={"id": TIP}),
            self.checks_run(conclusion="failure"),
            self.checks_run(status="in_progress"),
            self.checks_run(repository={"full_name": "fork/repo", "id": 42}),
            self.checks_run(repository={"full_name": "owner/repo", "id": 99}),
            self.checks_run(head_repository={"full_name": "fork/repo", "id": 42}),
            self.checks_run(event="pull_request"),
            self.checks_run(head_branch="feature"),
            self.checks_run(path=".github/workflows/other.yml"),
            self.checks_run(name="Other"),
        ]
        for response in (None, {}, {"workflow_runs": []}, {"workflow_runs": [None]},
                         *bad_runs):
            with self.subTest(response=response):
                result = GATE.admit(self.event, self.context, api=lambda _: response)
                self.assertEqual(result["validation_required"], "true")
        for failure in (RuntimeError("403"), TimeoutError(), ValueError("bad JSON")):
            result = GATE.admit(self.event, self.context,
                                api=lambda _, failure=failure: (_ for _ in ()).throw(failure))
            self.assertEqual(result["validation_required"], "true")

    def test_invalid_request_is_rejected_before_actions_api_call(self):
        context = dict(self.context, GITHUB_REF="refs/heads/feature")
        calls = []
        with self.assertRaises(GATE.Rejected):
            GATE.admit(self.event, context, api=lambda endpoint: calls.append(endpoint))
        self.assertEqual(calls, [])

    def test_workflow_run_is_never_release_authority(self):
        context = dict(self.context, GITHUB_EVENT_NAME="workflow_run")
        with self.assertRaises(GATE.Rejected):
            GATE.admit(self.event, context, api=lambda _: self.checks_run())

    def test_tag_push_requires_fresh_validation_and_dispatch_falls_back(self):
        for kind, ref in (("push", "refs/tags/v1.2.3"),
                          ("workflow_dispatch", "refs/heads/main")):
            with self.subTest(kind=kind, ref=ref):
                context = dict(self.context, GITHUB_EVENT_NAME=kind, GITHUB_REF=ref)
                event = {"repository": REPO, "ref": ref, "after": TIP, "deleted": False}
                self.assertEqual(GATE.admit(event, context, api=lambda _: {}),
                                 {"head": HEAD, "validation_required": "true"})

    def test_tag_ref_dispatch_requires_full_checks_without_api_reuse(self):
        for ref in ("refs/tags/v1.2.3", "refs/tags/v1.2.0-rc.1"):
            with self.subTest(ref=ref):
                context = dict(self.context, GITHUB_REF=ref)
                calls = []
                event = {"repository": REPO}
                result = GATE.admit(
                    event, context,
                    api=lambda endpoint: calls.append(endpoint) or self.checks_run(),
                )
                self.assertEqual(result, {"head": HEAD, "validation_required": "true"})
                self.assertEqual(calls, [])

    def test_annotated_tag_object_does_not_replace_runner_commit(self):
        context = dict(self.context, GITHUB_EVENT_NAME="push", GITHUB_REF="refs/tags/v1.2.3")
        event = {"repository": REPO, "ref": "refs/tags/v1.2.3", "after": HEAD, "deleted": False}
        self.assertEqual(GATE.admit(event, context)["head"], HEAD)

    def test_unsupported_events_refs_and_deleted_tags_fail(self):
        for kind, ref, after, deleted in (
                ("push", "refs/heads/main", TIP, False),
                ("pull_request", "refs/heads/main", TIP, False),
                ("workflow_dispatch", "refs/heads/feature", TIP, False),
                ("push", "refs/tags/v1.2.3", TIP, True)):
            with self.subTest(kind=kind, ref=ref, after=after, deleted=deleted):
                context = dict(self.context, GITHUB_EVENT_NAME=kind, GITHUB_REF=ref)
                event = {"repository": REPO, "ref": ref, "after": after, "deleted": deleted}
                with self.assertRaises(GATE.Rejected):
                    GATE.admit(event, context)

    def test_cli_emits_no_outputs_on_denial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            env = dict(os.environ, **self.context, GITHUB_EVENT_PATH=str(path))
            for success in (True, False):
                context = dict(env, GITHUB_EVENT_NAME="workflow_run" if success else "pull_request")
                path.write_text(json.dumps(self.event))
                result = subprocess.run([sys.executable, str(HERE / "validation-gate.py")],
                                        env=context, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")

    def test_actual_publish_condition_requires_admission_and_validation(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        release = workflow.split("\n  release:\n", 1)[1]
        condition = re.search(r"    if: >-\n((?:      .+\n)+)", release).group(1).strip()
        for admitted in ("success", "failure", "skipped", "cancelled"):
            for validation in ("success", "failure", "skipped", "cancelled"):
                for required in ("true", "false", ""):
                    for cancelled in (False, True):
                        with self.subTest(admitted=admitted, validation=validation,
                                          required=required, cancelled=cancelled):
                            expression = condition.replace("always()", "True")
                            expression = expression.replace("!cancelled()", str(not cancelled))
                            expression = expression.replace("needs.admission.result", repr(admitted))
                            expression = expression.replace("needs.validation.result", repr(validation))
                            expression = expression.replace("needs.admission.outputs.validation_required", repr(required))
                            expression = expression.replace("&&", " and ").replace("||", " or ")
                            actual = eval(" ".join(expression.split()), {"__builtins__": {}})
                            expected = (not cancelled and admitted == "success"
                                        and (required == "false" or
                                             (required == "true" and validation == "success")))
                            self.assertEqual(actual, expected)
        self.assertIn("needs: [admission, validation]", release)
        self.assertNotIn("${{ github.sha }}", release)
        self.assertIn("uses: ./.github/workflows/checks.yml", workflow)
        checks = (HERE.parents[1] / ".github/workflows/checks.yml").read_text()
        self.assertIn("  workflow_call:", checks)
        self.assertEqual(checks.count("ref: ${{ github.sha }}"), 6)

    def test_denied_checks_never_enter_publish_serialization(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        before_publish, publish = workflow.split("\n  release:\n", 1)
        self.assertNotRegex(before_publish, r"(?m)^\s*concurrency:")
        self.assertRegex(publish, r"    concurrency:\n      group: release\n      cancel-in-progress: false")
        # GitHub's concurrency queue replaces an older pending job even with
        # cancel-in-progress=false. Failed admission must therefore stop before
        # the only job owning that queue; the expression matrix above exercises
        # failure/cancellation against that actual job's condition.
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        self.assertNotIn("workflow_run:", workflow)

    def test_actual_checkout_guard_rejects_foreign_commit(self):
        workflow = (HERE.parents[1] / ".github/workflows/release.yml").read_text()
        step = workflow.split("- name: Verify the checked out commit\n", 1)[1].split("\n      - name:", 1)[0]
        script = re.search(r"        run: (.+)", step).group(1)
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                       GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
            subprocess.run(["git", "init", "-q", directory], env=env, check=True)
            subprocess.run(["git", "-C", directory, "commit", "--allow-empty", "-qm", "fixture"], env=env, check=True)
            actual = subprocess.check_output(["git", "-C", directory, "rev-parse", "HEAD"], env=env, text=True).strip()
            for expected in (actual, HEAD):
                result = subprocess.run(["bash", "-e", "-c", script], cwd=directory,
                                        env=dict(env, RELEASE_HEAD=expected))
                self.assertEqual(result.returncode, 0 if expected == actual else 1)


def job_block(workflow: str, name: str) -> str:
    """The text of one top-level job, without a YAML dependency."""
    jobs = workflow.split("\njobs:\n", 1)[1]
    match = re.search(rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  \S+:\n|\Z)", jobs)
    assert match, name
    return match.group(1)


class ChecksWorkflowContractTest(unittest.TestCase):
    """The sharded Checks keep the names, permissions and gating Release relies on."""

    def setUp(self):
        root = HERE.parents[1] / ".github/workflows"
        self.checks = (root / "checks.yml").read_text()
        self.release = (root / "release.yml").read_text()

    def checks_run(self):
        return {"workflow_runs": [{"name": "Checks", "path": ".github/workflows/checks.yml",
                "event": "push", "status": "completed", "conclusion": "success",
                "head_branch": "main", "head_sha": HEAD, "head_commit": {"id": HEAD},
                "repository": dict(REPO), "head_repository": dict(REPO)}]}

    def test_required_check_name_survives_as_a_single_aggregate_job(self):
        aggregate = job_block(self.checks, "full-test-suite")
        self.assertIn("    name: full-test-suite\n", aggregate)
        self.assertIn("    needs: [adaptation-boundary, full-suite-shard]\n", aggregate)
        # always() so a failed or cancelled shard turns the aggregate red instead of skipping it.
        self.assertIn("    if: always() && needs.adaptation-boundary.result == 'success'\n", aggregate)
        self.assertNotIn("strategy:", aggregate)
        # full_tests=false: every working step is skipped, so the job still succeeds.
        steps = aggregate.split("    steps:\n", 1)[1].split("\n      - name:")
        self.assertEqual(len(steps), 4)
        for step in steps:
            self.assertIn("if: needs.adaptation-boundary.outputs.full_tests == 'true'", step)
        self.assertIn('"$SHARDS" != success', aggregate)
        self.assertIn("--verify-shards", aggregate)
        self.assertIn("actions/download-artifact@", aggregate)

    def test_eight_shards_run_in_parallel_within_the_old_time_budget(self):
        shard = job_block(self.checks, "full-suite-shard")
        self.assertIn("    needs: adaptation-boundary\n", shard)
        self.assertIn("    if: needs.adaptation-boundary.outputs.full_tests == 'true'\n", shard)
        self.assertIn("      fail-fast: false\n", shard)
        self.assertIn("        shard: [1, 2, 3, 4, 5, 6, 7, 8]\n", shard)
        self.assertIn("    timeout-minutes: 40\n", shard)
        for option in ("--shard ${{ matrix.shard }}/8", "--suite-list", "--retries 1", "--xpass-nonfatal",
                       "--strict-leak-sweep", "--diagnostics-dir", "--seed-baseline"):
            self.assertIn(option, shard)
        self.assertIn("bash tools/release/install-ci-fixtures.sh ripgrep strace", shard)

    def test_shard_and_marker_uploads_replace_an_earlier_attempt(self):
        shard = job_block(self.checks, "full-suite-shard")
        upload = shard.split("- name: Upload shard report", 1)[1]
        self.assertIn("if: always()", upload)
        self.assertIn("name: run-tests-report-shard-${{ matrix.shard }}-of-8", upload)
        self.assertIn("overwrite: true", upload)
        marker = job_block(self.checks, "validated-tree-marker")
        self.assertIn("name: validated-tree-${{ steps.tree.outputs.tree }}", marker)
        self.assertIn("overwrite: true", marker)
        self.assertIn("git rev-parse 'HEAD^{tree}'", marker)

    def test_marker_needs_a_fully_green_pull_request_run(self):
        marker = job_block(self.checks, "validated-tree-marker")
        self.assertIn("needs: [adaptation-boundary, install-lifecycle, dispatch-pid-namespace, full-test-suite]", marker)
        condition = re.search(r"    if: >-\n((?:      .+\n)+)", marker).group(1)
        for needed in ("github.event_name == 'pull_request'",
                       "needs.adaptation-boundary.outputs.full_tests == 'true'",
                       "needs.adaptation-boundary.result == 'success'",
                       "needs.install-lifecycle.result == 'success'",
                       "needs.dispatch-pid-namespace.result == 'success'",
                       "needs.full-test-suite.result == 'success'"):
            self.assertIn(needed, condition)
        self.assertNotIn("always()", condition)

    def test_only_the_scope_job_may_read_actions(self):
        self.assertIn("      actions: read\n", job_block(self.checks, "adaptation-boundary"))
        self.assertEqual(self.checks.count("actions: read"), 1)
        self.assertRegex(self.checks, r"(?m)^permissions:\n  contents: read\n")
        self.assertIn("GH_TOKEN: ${{ github.token }}", job_block(self.checks, "adaptation-boundary"))

    def test_release_grants_the_called_checks_what_they_read_and_nothing_to_publish(self):
        validation = job_block(self.release, "validation")
        self.assertIn("      contents: read\n      # The called Checks job", validation)
        self.assertIn("      actions: read\n", validation)
        self.assertNotIn("write", validation)
        admission = job_block(self.release, "admission")
        self.assertIn("      contents: read\n      actions: read\n", admission)
        self.assertIn('GH_TOKEN: ${{ github.token }}', admission)
        self.assertIn("    permissions:\n      contents: write\n", job_block(self.release, "release"))
        self.assertRegex(self.release, r"(?m)^permissions:\n  contents: read\n")

    def test_every_action_is_pinned_to_a_full_commit(self):
        for name, text in (("checks", self.checks), ("release", self.release)):
            for reference in re.findall(r"(?m)^\s+uses: (\S+)", text):
                if reference.startswith("./"):
                    continue
                with self.subTest(workflow=name, reference=reference):
                    self.assertRegex(reference, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

    def test_successful_main_checks_can_be_reused_even_if_scope_skipped_suites(self):
        # A main push whose expensive jobs were skipped (docs-only or a tree a
        # PR already validated) still concludes success; admission reads only
        # that conclusion, so no gate change is needed for the skip.
        result = GATE.admit(self.event(), self.context(), api=lambda _: self.checks_run())
        self.assertEqual(result["validation_required"], "false")

    def context(self):
        return {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_REPOSITORY_ID": "42",
                "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": HEAD, "GITHUB_REF": "refs/heads/main"}

    def event(self):
        return {"repository": dict(REPO)}


if __name__ == "__main__":
    unittest.main()
