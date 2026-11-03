#!/usr/bin/env python3
"""Parent deploy uses only temporary fixtures; never any unit, service or DB."""
import json
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import signal
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ship_parent_deploy as S
from review_round_cap import round_budget


class ParentDeployTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.jobs = self.root / "dispatch/jobs.log"
        self.jobs.parent.mkdir()
        self.jobs.write_text("")
        self.route = {"route_id": "rt-test", "route_hash": "sha256:sealed", "capability": "autopilot-ship",
                      "entry_scope_contract_version": 1, "entry_execution_scope": "complete",
                      "nodes": [{"id": "deploy", "completion_gate": "ship-deploy", "depends_on": ["security-review", "release-review"]},
                                {"id": "security-review", "kind": "review-worker", "completion_gate": "security-pass"},
                                {"id": "release-review", "kind": "review-worker", "completion_gate": "release-pass"}]}
        self.meta = {"parent_sid": "parent", "parent_harness": "codex", "worker_type": "owner",
                     "dispatch_depth": "1", "registered_worker": "1", "note": "dead-worker-blocked"}
        self.record = self.root / "cycle/artifacts/release-config/deploy-record.json"
        self.record.parent.mkdir(parents=True)
        self.record.write_text(json.dumps({"rollback": "retained", "private_preimage": "existing"}))
        self.script = self.root / "deploy.py"
        self.counter = self.root / "effects"
        self.script.write_text("from pathlib import Path\np=Path('effects')\np.write_text((p.read_text() if p.exists() else '')+'x')\n")
        self.command = self.root / "command.json"
        self.command.write_text(json.dumps({"argv": [sys.executable, str(self.script)], "cwd": str(self.root), "timeout_seconds": 3}))
        self.proof = {"security-review": {"passed": True, "attempt_id": "security", "marker_digest": "s"},
                      "release-review": {"passed": True, "attempt_id": "release", "marker_digest": "r"}}
        self.markers = self.root / "markers"
        self.markers.mkdir()
        for node in self.proof:
            S.write(self.markers / (node + ".json"), {"review_independence": "independent"})
        self.cr = SimpleNamespace(_current_owner_attempt=lambda jobs, attempt: "owner",
                                  valid_entry_execution_scope_marker=lambda v, s: (v, s) == (None, None) or (v == 1 and s in ("complete", "report")),
                                  owner_terminal_prerequisites=lambda *args: {},
                                  _exclusive_lock=lambda path: S.locked(path),
                                  completion_dir=lambda *args, **kwargs: self.markers,
                                  _marker_identity_row=lambda r, n, nid, gate, **kw: dict(self.proof[nid]),
                                  terminal_gate_observation=lambda *args, **kwargs: {})
        self.real_decide = S.authority.decide_continuation
        decision = patch.object(S.authority, "decide_continuation", return_value=
            S.authority.ContinuationDecision(False, "answer", "owner-blocked", "correct"))
        self.decider = decision.start()
        self.addCleanup(decision.stop)
        for obj, name, value in [
            (S.DC, "_route_module", lambda: self.cr),
            (S, "exact_attempt_row", lambda jobs, aid: SimpleNamespace(status="done", raw=datetime.now(timezone.utc).isoformat()+"\t", metadata={"worker_type":"review"} if aid in {p["attempt_id"] for p in self.proof.values()} else self.meta)),
            (S, "_route", lambda *args: (self.root / "route.json", self.route)),
            (S.DC, "owner_operation_fence", lambda *args, **kwargs: None),
            (S.DC, "human_gate_entry_fence", lambda *args, **kwargs: None),
            (S, "record_path", lambda route: self.record),
        ]:
            mock = patch.object(obj, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        # Native identity resolution itself is real, and used for all three harnesses.
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": "parent"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        host = patch.object(S, "pid_namespace_evidence", return_value={"lifecycle_selector_source": "host-like"})
        host.start()
        self.addCleanup(host.stop)

    def prepared(self):
        return Path(S.prepare(self.jobs, "owner", self.command)["request"])

    def test_success_preserves_record_and_hands_back_exact_route(self):
        request = self.prepared()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(S.run(request, check=True)["execution_state"], "not-run")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        result = S.run(request)
        self.assertEqual(result["state"], "success")
        self.assertEqual(self.counter.read_text(), "x")
        record = S.read(self.record)
        self.assertEqual(record["rollback"], "retained")
        self.assertEqual(record["parent_deploy"]["executor"]["session_id"], "parent")
        S.validate_completion(self.route, self.jobs, self.record)
        self.assertIn("correct", result["correction_command"])
        message = S.paths(self.jobs, "rt-test")[1].with_suffix(".message.md").read_text()
        self.assertIn("complete --inline --route", message)
        self.assertIn(str(self.root / "route.json"), message)
        # Simulate a lost reply: a repeated run must only republish its result.
        S.run(request)
        self.assertEqual(self.counter.read_text(), "x")

    def test_all_native_harnesses_apply_same_parent_boundary(self):
        for harness, variable in [("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: "parent"}, clear=True):
                self.meta["parent_harness"] = harness
                self.assertEqual(S.parent(self.meta, self.jobs)["harness"], harness)
                with patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}):
                    with self.assertRaisesRegex(ValueError, "dispatch-child-forbidden"):
                        S.parent(self.meta, self.jobs)

    def test_wrong_parent_or_unknown_host_cannot_prepare_or_run(self):
        request = self.prepared()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "foreign"}, clear=True):
            with self.assertRaisesRegex(ValueError, "actual-parent-required"):
                S.run(request)
        with patch.object(S, "pid_namespace_evidence", return_value={"lifecycle_selector_source": "pid1-class"}):
            with self.assertRaisesRegex(ValueError, "host-unverified"):
                S.run(request)
        self.assertFalse(self.counter.exists())

    def test_registered_current_owner_can_prepare_but_cannot_execute(self):
        with patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "owner", "AGENT_DISPATCH_CHILD": "1"}), patch.object(S.authority, "review_owner_authority") as auth:
            request = self.prepared()
            auth.assert_called_once()
            self.assertIsNone(S.read(request)["parent_availability"])
            self.assertEqual(self.decider.call_args.kwargs["facts"], {})
            with self.assertRaisesRegex(ValueError, "dispatch-child-forbidden"):
                S.run(request)

    def owner_row(self, status="done", note="dead-worker-blocked"):
        meta = {**self.meta, "attempt_id": "owner", "note": note}
        self.jobs.write_text(datetime.now(timezone.utc).isoformat()+"\t"+status+"\trepo\tworktree\towner\t" +
                             ",".join(k+"="+v for k, v in meta.items())+"\n")

    def test_common_owner_admission_observes_exact_process_and_same_actions(self):
        self.decider.side_effect = self.real_decide
        cases = [("open", "", "live", None, True),
                 ("done", "dead-worker-blocked", "quiescent", None, False),
                 ("done", "dead-worker-blocked", "unverifiable", None, False),
                 ("done", "dead-worker-blocked", "live", None, False),
                 ("open", "", "quiescent", "same-command", True),
                 ("cancelled", "", "quiescent", "settle", False),
                 ("done", "", "quiescent", "settle", True)]
        for status, note, process, action, observes_process in cases:
            with self.subTest(status=status, process=process):
                self.owner_row(status, note)
                with patch("route_parent_close.owner_continuation_processes", return_value=(
                        SimpleNamespace(state=process, reason="fixture observation"), [])) as observed:
                    if action is None:
                        S.context(self.jobs, "owner")
                    else:
                        with self.assertRaises(S.DC.DispatchContractError) as refused:
                            S.context(self.jobs, "owner")
                        self.assertEqual(refused.exception.next_action, action)
                    if observes_process:
                        observed.assert_called_once()
                    else:
                        # Keeping a blocked answer and honoring cancellation
                        # are independent of uncertain execution observations.
                        observed.assert_not_called()
        self.assertFalse(self.counter.exists())

    def test_common_admission_binds_real_parent_identity_in_all_harnesses(self):
        self.decider.side_effect = self.real_decide
        for harness, variable in [("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"), ("opencode", "OPENCODE_SESSION_ID")]:
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: "parent"}, clear=True):
                self.meta["parent_harness"] = harness
                self.owner_row()
                with patch("route_parent_close.owner_continuation_processes", return_value=(
                        SimpleNamespace(state="quiescent", reason="fixture observation"), [])):
                    S.context(self.jobs, "owner")
                    self.assertEqual(self.decider.call_args.kwargs["facts"], {"actor": "parent"})
                    self.assertEqual(self.decider.call_args.kwargs["owner_attempt_id"], "owner")
                    with patch.dict(os.environ, {variable: "foreign"}):
                        with self.assertRaisesRegex(S.DC.DispatchContractError, "work-parent-recovery-required"):
                            S.context(self.jobs, "owner")

    def test_cli_preserves_common_wait_action_and_observation_condition(self):
        self.decider.side_effect = self.real_decide
        self.owner_row("open", "")
        with patch("route_parent_close.owner_continuation_processes", return_value=(
                SimpleNamespace(state="unverifiable", reason="fixture observation"), [])), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = S.main(["prepare", "--jobs", str(self.jobs), "--attempt-id", "owner",
                             "--command-file", str(self.command)])
        self.assertEqual(result, 69)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["next_action"], "same-command")
        self.assertTrue(failure["retry_when"])
        self.assertEqual(failure["detail"], failure["reason"] + ":fixture observation")
        self.assertFalse(S.paths(self.jobs, "rt-test")[0].exists())

    def test_pending_or_overridden_review_stops_before_spawn(self):
        request = self.prepared()
        self.cr.owner_terminal_prerequisites = lambda *args: {"release-review": "blocked"}
        with self.assertRaisesRegex(ValueError, "reviews-pending"):
            S.run(request)
        self.cr.owner_terminal_prerequisites = lambda *args: {}
        self.proof["release-review"]["review_independence"] = "owner-overridden"
        with self.assertRaisesRegex(ValueError, "independent-review-required"):
            S.run(request)
        self.assertFalse(S.paths(self.jobs, "rt-test")[1].exists())

    def review_row(self, attempt="security", node="security-review"):
        return datetime.now(timezone.utc).isoformat()+"\tdone\trepo\tworktree\treview\t" + ",".join(
            ["attempt_id="+attempt,"route_id="+self.route["route_id"],"route_node="+node,"worker_type=review"]) + "\n"

    def test_changed_request_rejects_existing_and_inflight_reviews_within_same_second(self):
        request = self.prepared()
        old = S.read(request)
        self.jobs.write_text(self.review_row()+self.review_row("release", "release-review"))
        # Same normalized request preserves its binding, even after review began.
        self.prepared()
        self.assertEqual(S.read(request)["request_digest"], old["request_digest"])
        self.assertEqual(S.read(request)["prepared_at"], old["prepared_at"])
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('unreviewed')\n")
        self.prepared()
        changed = S.read(request)
        self.assertNotEqual(changed["request_digest"], old["request_digest"])
        self.assertIn("security", changed["prior_review_attempts"])
        with self.assertRaisesRegex(ValueError, "review-precedes-request"):
            S.run(request)
        self.assertFalse(self.counter.exists())
        self.proof["security-review"]["attempt_id"] = "new-security"
        with self.assertRaisesRegex(ValueError, "review-precedes-request:release-review"):
            S.run(request)
        self.proof["release-review"]["attempt_id"] = "new-release"
        with patch.object(S, "exact_attempt_row", side_effect=lambda jobs, aid: SimpleNamespace(
                status="done", metadata={"worker_type":"review"} if aid in ("new-security","new-release") else self.meta,
                raw=datetime.now(timezone.utc).isoformat()+"\t")):
            self.assertEqual(S.run(request)["state"], "success")

    def test_raw_registered_review_override_and_parallel_leg_are_consumed(self):
        request = self.prepared()
        S.write(self.markers / "release-review.json", {"review_independence":"owner-overridden"})
        with self.assertRaisesRegex(ValueError, "independent-review-required"):
            S.run(request)
        S.write(self.markers / "release-review.json", {"review_independence":"independent"})
        self.route["nodes"].append({"id":"security-alternative","kind":"review-worker","completion_gate":"security-pass"})
        self.route["parallel_groups"] = [{"id":"security","members":["security-review","security-alternative"]}]
        self.proof["security-alternative"] = {"passed":True,"attempt_id":"alternative","marker_digest":"a"}
        S.write(self.markers / "security-alternative.json", {"review_independence":"owner-overridden"})
        with self.assertRaisesRegex(ValueError, "independent-review-required:security-alternative"):
            S.run(request)

    def test_older_registered_review_start_is_refused(self):
        request = self.prepared()
        with patch.object(S, "exact_attempt_row", side_effect=lambda jobs, aid: SimpleNamespace(
                status="done", metadata=self.meta if aid == "owner" else {"worker_type":"review"},
                raw="2000-01-01T00:00:00Z\t")):
            with self.assertRaisesRegex(ValueError, "review-precedes-request"):
                S.run(request)

    def functional_route(self, inputs=True):
        """A functional check a plan placed before the readiness reviews, as in [35] wK:p9."""
        self.route["nodes"] = [
            {"id": "release-setup", "kind": "capability-owner", "unit": "_kernel/owner", "dispatch_depth": 1,
             "completion_gate": "ship-setup", "depends_on": [], "outputs": ["release-config/**", "ship-checklist"]},
            {"id": "functional-check", "kind": "review-worker", "unit": "qa/test",
             "completion_gate": "ship-post-deploy-verify", "depends_on": ["release-setup"],
             "inputs": ["release-config/**"],
             "outputs": ["reviews/parts/plan/functional-check/reviews/post-deploy-verdict.json"],
             "plan_stage": {"id": "functional-check", "template": "post-deploy-verify"}},
            {"id": "security-review", "kind": "review-worker", "unit": "qa/security-review",
             "completion_gate": "ship-security", "depends_on": ["functional-check"],
             "outputs": ["reviews/security-verdict.json"]},
            {"id": "release-review", "kind": "review-worker", "unit": "qa/code-review",
             "completion_gate": "ship-release", "depends_on": ["security-review"],
             "outputs": ["reviews/release-verdict.json"]},
            {"id": "deploy", "kind": "capability-owner", "completion_gate": "ship-deploy",
             "depends_on": ["release-review"],
             "inputs": ["reviews/security-verdict.json", "reviews/release-verdict.json", "release-config/**"]}]
        self.proof["functional-check"] = {"passed": True, "attempt_id": "functional", "marker_digest": "f"}
        S.write(self.markers / "functional-check.json", {"review_independence": "independent"})
        self.product = self.root / "product/app.py"
        self.product.parent.mkdir()
        self.product.write_text("print('verified product')\n")
        command = {"argv": [sys.executable, str(self.script)], "cwd": str(self.root), "timeout_seconds": 3}
        if inputs:
            command["inputs"] = [str(self.product)]
        self.command.write_text(json.dumps(command))

    def registered(self, *rows):
        self.jobs.write_text(self.jobs.read_text() + "".join(self.review_row(a, n) for a, n in rows))

    def renewed(self, *nodes):
        for node in nodes:
            self.proof[node]["attempt_id"] = "new-" + node
            self.registered(("new-" + node, node))

    def change_command_only(self):
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('race-fixed')\n")

    def test_command_only_reprepare_keeps_capped_unchanged_functional_review(self):
        # [35] wK:p9: only the deploy script changes and the functional check's ceiling is spent.
        self.functional_route()
        request = self.prepared()
        first = S.read(request)
        self.registered(("functional", "functional-check"), ("security", "security-review"),
                        ("release", "release-review"))
        functional = next(n for n in self.route["nodes"] if n["id"] == "functional-check")
        spent = [("done", {"attempt_id": aid, "note": "completed-marker", "worker_type": "review",
                           "route_node": "functional-check"}) for aid in ("functional-round-1", "functional")]
        with patch.object(S.authority, "decide_continuation", self.real_decide):
            self.assertEqual(round_budget({**self.route, "effective_intensity": "standard"}, functional,
                                          spent).state, "exhausted")
        self.change_command_only()
        self.prepared()
        changed = S.read(request)
        self.assertNotEqual(changed["request_digest"], first["request_digest"])
        self.assertIn("functional", changed["prior_review_attempts"])
        self.renewed("security-review", "release-review")
        checked = S.run(request, check=True)
        self.assertEqual(changed["inputs_unchanged_since"], {
            "prepared_at": first["prepared_at"], "prior_review_attempts": first["prior_review_attempts"]})
        self.assertEqual(checked["reviews"]["functional-check"]["inputs_unchanged_since"], first["prepared_at"])
        for node in ("security-review", "release-review"):
            self.assertEqual(checked["reviews"][node], self.proof[node])
        for harness, variable in [("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"),
                                  ("opencode", "OPENCODE_SESSION_ID")]:
            with self.subTest(harness=harness), patch.dict(os.environ, {variable: "parent"}, clear=True):
                self.meta["parent_harness"] = harness
                self.assertEqual(S.run(request, check=True)["reviews"], checked["reviews"])
        self.meta["parent_harness"] = "codex"
        # The carried PASS keeps every existing independence requirement.
        S.write(self.markers / "functional-check.json", {"review_independence": "owner-overridden"})
        with self.assertRaisesRegex(ValueError, "independent-review-required:functional-check"):
            S.run(request, check=True)
        S.write(self.markers / "functional-check.json", {"review_independence": "independent"})
        self.assertEqual(S.run(request)["state"], "success")
        self.assertEqual(self.counter.read_text(), "race-fixed")
        S.validate_completion(self.route, self.jobs, self.record)

    def test_carried_functional_review_never_approves_the_changed_command(self):
        self.functional_route()
        request = self.prepared()
        self.registered(("functional", "functional-check"), ("security", "security-review"),
                        ("release", "release-review"))
        self.change_command_only()
        self.prepared()
        with self.assertRaisesRegex(ValueError, "review-precedes-request:(security|release)-review"):
            S.run(request, check=True)
        self.renewed("release-review")
        with self.assertRaisesRegex(ValueError, "review-precedes-request:security-review"):
            S.run(request, check=True)
        self.assertFalse(self.counter.exists())

    def test_changed_functional_input_requires_fresh_review(self):
        for command_too in (True, False):
            with self.subTest(command_too=command_too):
                self.setUp()
                self.functional_route()
                request = self.prepared()
                self.registered(("functional", "functional-check"), ("security", "security-review"),
                                ("release", "release-review"))
                self.product.write_text("print('changed product')\n")
                if command_too:
                    self.change_command_only()
                self.prepared()
                self.assertNotIn("inputs_unchanged_since", S.read(request))
                self.renewed("security-review", "release-review")
                with self.assertRaisesRegex(ValueError, "review-precedes-request:functional-check"):
                    S.run(request, check=True)
                self.renewed("functional-check")
                self.assertEqual(S.run(request, check=True)["state"], "checked")

    def test_functional_review_without_observed_inputs_is_not_carried(self):
        cases = {"before-first-request": False, "older-registry-start": False, "no-listed-inputs": True}
        for case, empty in cases.items():
            with self.subTest(case=case):
                self.setUp()
                self.functional_route(inputs=not empty)
                if case == "before-first-request":
                    self.registered(("functional", "functional-check"))
                request = self.prepared()
                if case != "before-first-request":
                    self.registered(("functional", "functional-check"))
                self.change_command_only()
                self.prepared()
                self.assertEqual("inputs_unchanged_since" in S.read(request), not empty)
                self.renewed("security-review", "release-review")
                fresh = {p["attempt_id"] for p in self.proof.values()} - {"functional"}
                started = "2000-01-01T00:00:00Z" if case == "older-registry-start" else datetime.now(timezone.utc).isoformat()
                with patch.object(S, "exact_attempt_row", side_effect=lambda jobs, aid: SimpleNamespace(
                        status="done", metadata={"worker_type": "review"} if aid in fresh | {"functional"} else self.meta,
                        raw=(started if aid == "functional" else datetime.now(timezone.utc).isoformat()) + "\t")):
                    with self.assertRaisesRegex(ValueError, "review-precedes-request:functional-check"):
                        S.run(request, check=True)

    def test_same_request_and_repeated_command_changes_keep_first_input_generation(self):
        self.functional_route()
        request = self.prepared()
        first = S.read(request)
        self.registered(("functional", "functional-check"))
        self.change_command_only()
        self.prepared()
        second = S.read(request)
        self.prepared()
        self.assertEqual(S.read(request), second)
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('second-fix')\n")
        self.prepared()
        third = S.read(request)
        self.assertNotEqual(third["request_digest"], second["request_digest"])
        self.assertEqual(third["inputs_unchanged_since"]["prepared_at"], first["prepared_at"])
        self.assertEqual(third["inputs_unchanged_since"], second["inputs_unchanged_since"])
        self.renewed("security-review", "release-review")
        self.assertEqual(S.run(request, check=True)["reviews"]["functional-check"]["inputs_unchanged_since"],
                         first["prepared_at"])

    def test_readiness_family_and_legs_judge_the_command(self):
        cases = ["security-delta", "parallel-leg", "unclassified-route"]
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                self.functional_route()
                nodes = {n["id"]: n for n in self.route["nodes"]}
                if case == "security-delta":
                    # A plan's security stage keeps the readiness gate, even with a relocated verdict.
                    extra = {"id": "plan-security-delta", "kind": "review-worker", "unit": "qa/security-review",
                             "completion_gate": "ship-security", "depends_on": ["functional-check"],
                             "outputs": ["reviews/parts/plan/security-delta/reviews/security-verdict.json"],
                             "plan_stage": {"id": "security-delta", "template": "security-review"}}
                    nodes["security-review"]["depends_on"] = ["plan-security-delta"]
                elif case == "parallel-leg":
                    extra = {"id": "security-review-alternative", "kind": "review-worker",
                             "unit": "qa/security-review", "completion_gate": "ship-security",
                             "parallel_anchor": "security-review", "depends_on": ["functional-check"],
                             "outputs": ["reviews-alternative/security-verdict.json"]}
                    nodes["deploy"]["depends_on"].append(extra["id"])
                    self.route["parallel_groups"] = [{"id": "security-review",
                                                      "members": ["security-review", extra["id"]]}]
                else:
                    extra = None
                    nodes["deploy"].pop("inputs")
                if extra:
                    self.route["nodes"].insert(2, extra)
                    self.proof[extra["id"]] = {"passed": True, "attempt_id": extra["id"], "marker_digest": "x"}
                    S.write(self.markers / (extra["id"] + ".json"), {"review_independence": "independent"})
                request = self.prepared()
                self.registered(*((p["attempt_id"], node) for node, p in self.proof.items()))
                self.change_command_only()
                self.prepared()
                self.renewed("security-review", "release-review")
                refused = extra["id"] if extra else "functional-check"
                with self.assertRaisesRegex(ValueError, "review-precedes-request:" + refused + "$"):
                    S.run(request, check=True)
                self.renewed(refused)
                self.assertEqual(S.run(request, check=True)["state"], "checked")

    def test_legacy_changed_request_without_input_generation_stays_refused(self):
        # A request prepared before this record (the live [35] request) never carries a PASS.
        self.functional_route()
        request = self.prepared()
        self.registered(("functional", "functional-check"), ("security", "security-review"),
                        ("release", "release-review"))
        self.change_command_only()
        self.prepared()
        legacy = S.read(request)
        legacy.pop("inputs_unchanged_since", None)
        bound = {k: v for k, v in legacy.items() if k not in {"request_digest", "parent_availability"}}
        legacy["request_digest"] = S.digest(bound)
        S.write(request, legacy)
        self.prepared()
        self.assertEqual(S.read(request), legacy)
        self.renewed("security-review", "release-review")
        with self.assertRaisesRegex(ValueError, "review-precedes-request:functional-check"):
            S.run(request, check=True)
        # A later command-only change observes inputs from that request onward, not before it.
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('later-fix')\n")
        self.prepared()
        self.assertEqual(S.read(request)["inputs_unchanged_since"]["prepared_at"], legacy["prepared_at"])
        with self.assertRaisesRegex(ValueError, "review-precedes-request:(security|release)-review"):
            S.run(request, check=True)
        for node in ("security-review", "release-review"):
            self.proof[node]["attempt_id"] = "later-" + node
            self.registered(("later-" + node, node))
        with self.assertRaisesRegex(ValueError, "review-precedes-request:functional-check"):
            S.run(request, check=True)

    def test_command_source_drift_and_route_report_scope_refuse(self):
        request = self.prepared()
        self.script.write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "command-input-drift"):
            S.run(request)
        self.route["entry_execution_scope"] = "report"
        with self.assertRaisesRegex(ValueError, "approved-ship-scope-required"):
            S.run(request)

    def test_stop_and_closed_route_do_not_execute(self):
        request = self.prepared()
        with patch.object(S.DC, "owner_operation_fence", side_effect=S.DC.DispatchContractError("cancelled-by-parent")):
            with self.assertRaises(S.DC.DispatchContractError):
                S.run(request)
        with patch.object(S, "_route", side_effect=S.DC.DispatchContractError("replacement-route-closed")):
            with self.assertRaises(S.DC.DispatchContractError):
                S.run(request)
        self.assertFalse(self.counter.exists())

    def test_owner_replacement_retains_exact_request_and_reviews(self):
        self.cr._current_owner_attempt = lambda jobs, attempt: "replacement-owner"
        request = self.prepared()
        self.assertEqual(S.read(request)["owner_attempt_id"], "owner")
        self.assertEqual(self.decider.call_args.kwargs["owner_attempt_id"], "owner")
        S.run(request)
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertEqual(result["owner_attempt_id"], "replacement-owner")
        self.assertEqual(result["reviews"], self.proof)
        self.assertEqual(S.read(request)["owner_attempt_id"], "owner")

    def test_failed_and_timeout_outcomes_never_repeat(self):
        self.script.write_text("from pathlib import Path\nPath('effects').write_text('once')\nraise SystemExit(7)\n")
        request = self.prepared()
        self.assertEqual(S.run(request)["state"], "failed")
        with self.assertRaisesRegex(ValueError, "successful-result-required"):
            S.validate_completion(self.route, self.jobs, self.record)
        S.run(request)
        self.assertEqual(self.counter.read_text(), "once")
        with self.assertRaisesRegex(ValueError, "execution-already-recorded"):
            self.prepared()

    def test_timeout_cleans_owned_process_group(self):
        self.script.write_text("import time\ntime.sleep(60)\n")
        self.command.write_text(json.dumps({"argv": [sys.executable, str(self.script)], "cwd": str(self.root), "timeout_seconds": 0.15}))
        request = self.prepared()
        self.assertEqual(S.run(request)["state"], "failed")
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertEqual(result["detail"], "TimeoutExpired")
        self.assertFalse(S.DC.process_identity_is_live(result["pid"], result["pid_start"]))
        with patch.object(S.subprocess, "Popen", side_effect=AssertionError("must not retry")):
            S.run(request)

    def test_parent_interruption_retains_result_and_cleans_its_command(self):
        self.script.write_text("import time\ntime.sleep(60)\n")
        request = self.prepared()
        timer = threading.Timer(0.15, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            self.assertEqual(S.run(request)["state"], "interrupted")
        finally:
            timer.cancel()
            timer.join()
        result = S.read(S.paths(self.jobs, "rt-test")[1])
        self.assertFalse(S.DC.process_identity_is_live(result["pid"], result["pid_start"]))
        with patch.object(S.subprocess, "Popen", side_effect=AssertionError("must not retry")):
            S.run(request)

    def test_running_intent_and_result_record_loss_do_not_repeat(self):
        request = self.prepared()
        value, result_path = S.load_request(request)
        S.write(result_path, {"state": "running", "request_digest": value["request_digest"]})
        self.assertEqual(S.run(request)["state"], "running-or-interrupted")
        self.assertFalse(self.counter.exists())

    def test_completion_refuses_changed_result_or_review_and_normal_deploy_stays_unchanged(self):
        S.validate_completion(self.route, self.jobs, self.root / "missing-normal-record")
        request = self.prepared()
        S.run(request)
        self.proof["release-review"]["marker_digest"] = "changed"
        with self.assertRaisesRegex(ValueError, "review-evidence-drift"):
            S.validate_completion(self.route, self.jobs, self.record)
        self.proof["release-review"]["marker_digest"] = "r"
        record = S.read(self.record)
        record["parent_deploy"]["exit_code"] = 9
        S.write(self.record, record)
        with self.assertRaisesRegex(ValueError, "successful-result-required"):
            S.validate_completion(self.route, self.jobs, self.record)

    def test_exact_command_schema_and_private_log(self):
        for value in [{"argv": ["python3"], "cwd": str(self.root)},
                      {"argv": [sys.executable], "cwd": str(self.root), "timeout_seconds": float("nan")},
                      {"argv": [sys.executable], "cwd": str(self.root), "env": {"UNSAFE": "1"}}]:
            with self.assertRaises(ValueError):
                S.command(value)
        result = S.run(self.prepared())
        log = Path(S.read(result["result"])["output_log"])
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_printed_complete_command_uses_real_route_writer_and_keeps_owner_open(self):
        # Reuse the existing compile fixture, while exercising the production
        # CLI/parser/marker writer. No registered owner is closed by an inline
        # deploy completion: that owner still owns post-deploy verification.
        spec = importlib.util.spec_from_file_location("ship_route_fixture", Path(__file__).with_name("capability_route.test.py"))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        t = fixture.TestRoute()
        cr = fixture.R
        self.route = cr.compile_route(**t.args(capability="autopilot-ship", capability_mode="default",
            artifact_root=self.root, requested_intensity="standard", predicates=[], signals=[],
            transport="headless", inline_reason=None, dispatch_evidence=t.dispatch(t.nested()),
            registered_headless_evidence=t.registered_headless()))
        route_file = self.root / "route.json"
        route_file.write_text(json.dumps(self.route))
        self.jobs.write_text("time\topen\trepo\tworktree\towner\tattempt_schema_version=2,attempt_id=owner,worker_type=owner\n")
        original = self.jobs.read_bytes()
        result = S.run(self.prepared())
        message = Path(result["result"]).with_suffix(".message.md").read_text()
        command_line = next(line.split(": ", 1)[1] for line in message.splitlines() if line.startswith("If success,"))
        import shlex
        args = shlex.split(command_line)[2:]
        with patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_DISPATCH_ATTEMPT_ID": "owner",
                                     "AGENT_ARTIFACT_CHECKPOINT": "off"}), patch.object(sys, "argv", [str(cr.__file__), *args]), contextlib.redirect_stdout(io.StringIO()) as output:
            cr.main()
        marker = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(marker["node_id"], "deploy")
        self.assertFalse(marker["registered_worker"])
        self.assertEqual(self.jobs.read_bytes(), original)


class ExactSelfDispositionAdmissionTest(unittest.TestCase):
    def test_real_prepared_request_retains_strict_origin_and_terminal_admission(self):
        spec=importlib.util.spec_from_file_location("strict_ship_coupled_fixture",Path(__file__).with_name("work_start.test.py"))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        f=module.CoupledReviewSelfDispositionTest();f.setUp();self.addCleanup(f.doCleanups)
        f.test_public_correction_replacement_disposition_and_unchanged_strict_ship_consumption()
        deploy=next(n for n in f.f.route["nodes"] if n["id"]=="deploy")
        origin=f.f.directory/"release-review.1.json"
        before=origin.read_bytes();origin.write_bytes(before+b" ")
        with self.assertRaisesRegex(ValueError,"reviews-pending"):
            S.reviews(f.f.route,deploy,f.jobs,f.request)
        origin.write_bytes(before)
        lines=f.jobs.read_text().splitlines()
        for i,line in enumerate(lines):
            if f.d.parse_registry_metadata(line.split("\t")[5]).get("attempt_id")=="att-review-self":
                lines[i]+=",failure_class=fail"
        f.jobs.write_text("\n".join(lines)+"\n")
        with self.assertRaisesRegex(ValueError,"reviews-pending"):
            S.reviews(f.f.route,deploy,f.jobs,f.request)
        self.assertEqual(f.request_path.read_bytes(),f.request_bytes)

if __name__ == "__main__":
    unittest.main()
