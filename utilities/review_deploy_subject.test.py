#!/usr/bin/env python3
"""Pre-deploy reviews count their round cap per reviewed deploy-request inputs."""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import review_round_cap as ROUND  # noqa: E402
import review_source_subject as SUBJECT  # noqa: E402


class DeployReviewSubjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.jobs = root / "state" / "jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("", encoding="utf-8")
        self.artifacts = root / "cycle" / "artifacts"
        (self.artifacts / "release-config").mkdir(parents=True)
        self.script = self.artifacts / "release-config" / "deploy.py"
        self.script.write_text("print('deploy A')\n", encoding="utf-8")
        self.app = root / "app" / "server.py"
        self.app.parent.mkdir()
        self.app.write_text("app = 1\n", encoding="utf-8")
        self.route = {
            "route_id": "rt-deployreview0001", "route_hash": "sha256:deploy",
            "cwd": str(root / "app"), "continuation_budget": {"review_round_cap": 2},
            "nodes": [
                {"id": "release-setup", "kind": "capability-owner", "completion_gate": "ship-setup",
                 "outputs": ["release-config/**"], "depends_on": []},
                {"id": "delta-check", "kind": "review-worker", "unit": "qa/test",
                 "completion_gate": "ship-post-deploy-verify", "inputs": ["release-config/**"],
                 "depends_on": ["release-setup"]},
                {"id": "deploy", "kind": "capability-owner", "completion_gate": "ship-deploy",
                 "depends_on": ["delta-check"]},
                {"id": "post-deploy-verify", "kind": "review-worker", "unit": "qa/test",
                 "completion_gate": "ship-post-deploy-verify", "inputs": ["release-config/**"],
                 "depends_on": ["deploy"]},
            ],
        }
        self.review = self.route["nodes"][1]

    def write_request(self):
        inputs = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (self.script, self.app)}
        request = self.jobs.parent / "ship-parent-deploy" / f"{self.route['route_id']}.json"
        request.parent.mkdir(exist_ok=True)
        request.write_text(json.dumps({
            "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
            "deploy_record": str(self.artifacts / "release-config" / "deploy-record.json"),
            "command": {"argv": ["python3", str(self.script)], "inputs": inputs},
            "request_digest": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
        }), encoding="utf-8")

    def subject(self):
        return SUBJECT.observe(self.route, self.review, jobs=self.jobs)["digest"]

    def budget(self, rows, digest):
        return ROUND.round_budget(self.route, self.review, rows, source_subject_digest=digest)

    def test_only_reviews_before_deploy_are_deploy_reviews(self):
        self.assertTrue(SUBJECT.is_deploy_review(self.route, self.review))
        self.assertFalse(SUBJECT.is_deploy_review(self.route, self.route["nodes"][3]))

    def test_no_request_keeps_the_plain_node_count(self):
        self.assertFalse(SUBJECT.has_review_subject(self.route, self.review, self.jobs))
        self.assertFalse(SUBJECT.has_review_subject(self.route, self.review))

    def test_request_without_scoped_inputs_keeps_the_plain_completion_identity(self):
        self.write_request()
        self.review["inputs"] = ["unrelated/**"]
        self.assertFalse(SUBJECT.has_review_subject(self.route, self.review, self.jobs))
        self.assertIsNone(SUBJECT.marker_subject(self.route, self.review,
            attempt_id="att-no-scoped-input", attempt_metadata={"registered_worker": "1"},
            jobs=self.jobs, completion_dir=self.jobs.parent / "completion"))

    def test_subject_follows_in_scope_input_content_only(self):
        self.write_request()
        self.assertTrue(SUBJECT.has_review_subject(self.route, self.review, self.jobs))
        first = SUBJECT.observe(self.route, self.review, jobs=self.jobs)
        self.assertEqual([row[0] for row in first["manifest"]], ["release-config/deploy.py"])
        self.app.write_text("app = 2\n", encoding="utf-8")  # outside the review's inputs
        self.write_request()
        self.assertEqual(self.subject(), first["digest"])
        self.write_request()  # re-prepared, same bytes: same subject, no budget bypass
        self.assertEqual(self.subject(), first["digest"])
        self.script.write_text("print('deploy B')\n", encoding="utf-8")
        self.write_request()
        self.assertNotEqual(self.subject(), first["digest"])

    def test_changed_inputs_get_one_first_review_and_the_same_inputs_stay_capped(self):
        legacy = [("done", {"attempt_id": f"att-old-{i}", "note": "completed-marker",
                            "worker_type": "review"}) for i in (1, 2)]
        self.assertEqual(self.budget(legacy, None).state, "exhausted")
        self.write_request()
        digest = self.subject()
        fresh = self.budget(legacy, digest)
        self.assertEqual((fresh.state, fresh.round_kind), ("admit", "first"))
        bound = [(status, {**meta, "_review_subject_digest": digest}) for status, meta in legacy]
        self.assertEqual(self.budget(bound, digest).state, "exhausted")
        once = legacy + [("done", {"attempt_id": "att-new-1", "note": "completed-marker",
                                   "worker_type": "review", "_review_subject_digest": digest})]
        self.assertEqual(self.budget(once, digest).state, "admit")

    def test_launch_binding_records_and_rechecks_the_deploy_subject(self):
        self.write_request()
        route_file = Path(self.temp.name) / "route.json"
        route_file.write_text(json.dumps(self.route), encoding="utf-8")
        values = SUBJECT.seal_attempt(self.jobs, self.route, self.review, "att-delta-3",
                                      worktree=self.route["cwd"])
        self.assertEqual(values["review_subject_digest"], self.subject())
        metadata = {"route_file": str(route_file), "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"], "route_node": self.review["id"],
                    "completion_gate": self.review["completion_gate"], "unit": self.review["unit"],
                    "attempt_id": "att-delta-3", **values}
        fields = ["now", "open", self.route["cwd"], self.route["cwd"], "delta", ""]
        self.assertIsNotNone(SUBJECT.validate_attempt(self.jobs, fields, metadata, require_new=True))
        self.script.write_text("print('deploy C')\n", encoding="utf-8")
        self.write_request()
        with self.assertRaises(SUBJECT.SourceSubjectError) as refused:
            SUBJECT.validate_attempt(self.jobs, fields, metadata)
        self.assertEqual(refused.exception.reason, "review-source-subject-changed-before-launch")


if __name__ == "__main__":
    unittest.main()
