#!/usr/bin/env python3
"""A unit route launched and finished (design 2b): the leg's start, its sealed tuple, its end.

Real compose, real route files and real unit heads. The model launch is the one injected piece (the
fake `run` below stands where `dispatch-owner.py` would start the worker); the three-harness run with a
fake model is `harness_behavior_cases_unit_route.py`.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import argparse
import base64
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


F = _load("framed_route_for_unit_launch", "framed_route.test.py")
T, R, L = F.T, F.R, F.L
import dispatch_contract as DC  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_replacement as REPLACEMENT  # noqa: E402
import dispatch_terminal_commit as DTC  # noqa: E402
import codex_dispatch_terminal as TERMINAL  # noqa: E402
import owner_route_binding as BINDING  # noqa: E402
import work_intent  # noqa: E402
import work_start as W  # noqa: E402
from capability_route_nodes import staged_unit_profile  # noqa: E402
from dispatch_contract_policy import headless_attempt_policy  # noqa: E402
from route_plan import is_depth0_leg  # noqa: E402

BRIEF = "Review PR #1 and say what blocks it.\n"


class UnitLegBase(F.FramedBase):
    def unit_route(self, unit="qa/code-review", **overrides):
        arguments = dict(capability=None, capability_mode=None, shape=None, graph=None, slug="unit-launch",
                         cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="unit-key",
                         unit=unit, registered_headless_evidence=T.registered_headless(),
                         work_request={"text": BRIEF, "owner_harness": None})
        arguments.update(overrides)
        route = R.compose_route(**arguments)
        return route, Path(L.admit_runtime_route(self.root, route).route_file)

    @staticmethod
    def leg_row(route, path, status="open", **extra):
        node = route["nodes"][0]
        fields = {"attempt_id": W.attempt_id(route, "unit"), "route_id": route["route_id"],
                  "route_hash": route["route_hash"], "route_node": "unit", "route_file": str(path),
                  "worker_type": node["worker_type"], "unit": node["unit"], "dispatch_depth": "1",
                  "registered_worker": "1", "attempt_schema_version": "2", "transport": "headless",
                  "execution_surface": "registered-headless", "harness": "claude", "capability": "unit",
                  "workflow_completion": "runtime-v1", "parent_completion_delivery": "claude-parent-runtime",
                  "parent_sid": "parent", **extra}
        meta = ",".join(f"{key}={value}" for key, value in fields.items())
        return f"2026-11-17T00:00:00Z\t{status}\t/w\t/w\tunit-launch-unit\t{meta}\n"


class StagedGradeTest(unittest.TestCase):
    def test_a_unit_gets_the_grade_its_staged_recipes_declare(self):
        registry = R.TOPO.load_registry()
        for unit, grade in (("qa/code-review", "light"), ("qa/security-review", "balanced-deep"),
                            ("research/plan-review", "balanced-deep"), ("qa/test", "light"),
                            ("qa/assumption-check", "light")):   # the last is only a parallel-group check leg
            with self.subTest(unit=unit):
                self.assertEqual(staged_unit_profile(registry, unit), grade)
        self.assertEqual(staged_unit_profile(registry, "qa/not-in-any-recipe"), "light")   # the staged default


class UnitGradeTest(UnitLegBase):
    def test_the_sealed_grade_is_the_staged_one_and_a_given_profile_wins(self):
        self.assertEqual(self.unit_route("qa/code-review")[0]["nodes"][0]["model_profile"], "light")
        self.assertEqual(self.unit_route("qa/security-review", slug="unit-sec")[0]["nodes"][0]["model_profile"],
                         "balanced-deep")
        given = self.unit_route("qa/code-review", slug="unit-given", profile="deep")[0]
        self.assertEqual(given["nodes"][0]["model_profile"], "deep")


class UnitCardTest(UnitLegBase):
    def test_the_card_counts_one_worker_and_no_owner_and_offers_no_other_intensity(self):
        route, _ = self.unit_route()
        lines = "\n".join(R.compose_decision_lines(route))
        self.assertIn("worker_dispatches=1 owner_dispatches=0", lines)
        self.assertNotIn("--intensity strong", lines)


class UnitStartTest(UnitLegBase):
    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.jobs = Path(self.root) / "jobs.log"
        self.jobs.write_text("")
        self.commands = []
        for patch in (mock.patch.object(W, "default_parent_session_id", return_value="parent"),
                      mock.patch.object(W, "prepare_task_request", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_launch(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 65, "check=failed\nreason=fixture\nchild_spawned=0\n", "")

    def test_the_leg_starts_with_the_brief_and_its_own_node_and_no_owner(self):
        result = W.start_work(self.route, self.path, self.jobs, run=self.run_launch)
        command, = self.commands
        self.assertEqual(command[command.index("--route-node") + 1], "unit")
        self.assertEqual(command[command.index("--prompt-text") + 1], BRIEF)      # nothing is added to the brief
        self.assertNotIn("--prompt-file", command)
        self.assertEqual(command[command.index("--attempt-id") + 1], W.attempt_id(self.route, "unit"))
        # A refused launch names itself and the way on; it is not the owner path's `quick-owner-node-missing`.
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "unit-launch-not-admitted"))
        self.assertNotIn("owner-launch-not-admitted", json.dumps(result))
        self.assertFalse(result["owner_started"])

    def test_a_registered_leg_is_followed_and_never_launched_again(self):
        self.jobs.write_text(self.leg_row(self.route, self.path))
        pending = {"state": "pending", "children": []}
        with mock.patch.object(W, "join_selected_attempts", return_value=pending):
            result = W.start_work(self.route, self.path, self.jobs, run=self.run_launch)
        self.assertEqual(self.commands, [])
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["unit_attempt_id"], W.attempt_id(self.route, "unit"))
        self.assertIn("parent_next", result)

    def test_start_reuses_the_registry_index_and_observes_launch_and_join_writes(self):
        earlier = "2026-11-17T00:00:00Z\tdone\t/w\t/w\told\tattempt_id=att-old\n"
        self.jobs.write_text(earlier)
        registry_reads = []
        read_text = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.jobs:
                registry_reads.append(path)
            return read_text(path, *args, **kwargs)

        def launch(command, **kwargs):
            self.commands.append(command)
            self.jobs.write_text(earlier + self.leg_row(self.route, self.path))
            return subprocess.CompletedProcess(command, 0, "check=ok\nchild_spawned=1\n", "")

        def join(**kwargs):
            new = self.jobs.with_suffix(".new")
            new.write_text(earlier + self.leg_row(
                self.route, self.path, parent_completion_delivery="codex-parent-runtime"))
            new.replace(self.jobs)
            return {"state": "pending", "children": []}

        with mock.patch.object(Path, "read_text", read), \
             mock.patch.object(W, "join_selected_attempts", side_effect=join), \
             mock.patch.object(W, "parent_next", return_value=("end-turn", "fixture", "")) as parent_next:
            result = W.start_work(self.route, self.path, self.jobs, run=launch)
        command, = self.commands
        attempt = W.attempt_id(self.route, "unit")
        self.assertEqual(command[command.index("--attempt-id") + 1], attempt)
        self.assertEqual((result["state"], result["unit_attempt_id"]), ("running", attempt))
        self.assertEqual(parent_next.call_args.args, ("codex-parent-runtime", attempt))
        self.assertLessEqual(len(registry_reads), 1)

    def test_registry_index_preserves_duplicate_and_legacy_rows(self):
        first = self.leg_row(self.route, self.path)
        later = self.leg_row(self.route, self.path, status="done", note="fixture")
        attempt = W.attempt_id(self.route, "unit")
        for sep in ("\n", "\r\n", "\u2028"):
            with self.subTest(separator=sep):
                self.jobs.write_text(sep.join((first.rstrip(), later.rstrip(), "")))
                status, metadata = W._rows(self.jobs)[attempt]
                self.assertEqual((status, metadata["note"]), ("done", "fixture"))

    def test_a_finished_leg_reports_its_outcome(self):
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done"))
        outcome = {"attempt_id": W.attempt_id(self.route, "unit"), "classification": "success"}
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            result = W.start_work(self.route, self.path, self.jobs, run=self.run_launch)
        self.assertEqual((result["state"], result["result"]), ("completed", outcome))
        self.assertEqual(self.commands, [])


    def test_cancelled_unit_receipts_offer_inspection_and_no_restart(self):
        import route_parent_close
        from start_receipt import next_line

        cancelled = JOIN.CurrentDeliveryState(
            None, "", "", "", "done", "CANCELLED", True, 0, False,
            cancelled=True, cancellation_requested=True)
        stopped = JOIN.CurrentDeliveryState(None, "", "", "", "done", "", True, 0, False)
        for end, state, row_metadata in (
            ("parent cancellation", cancelled, {"note": "cancelled-by-parent"}),
            ("Fleet stop", stopped, {"note": route_parent_close.STOP_NOTE, **route_parent_close.STOP_EVIDENCE}),
        ):
            with self.subTest(end=end):
                self.jobs.write_text(self.leg_row(self.route, self.path, status="done",
                                                  launch_started="1", **row_metadata))
                with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready"}), \
                        mock.patch.object(W, "current_delivery_state", return_value=state):
                    receipt = W.start_work(self.route, self.path, self.jobs, run=self.run_launch)
                self.assertEqual(receipt["state"], "cancelled", receipt)
                self.assertEqual(receipt["result"].get("cancellation_requested", False), state.cancellation_requested)
                self.assertEqual(receipt["result"].get("stopped", False), not state.cancellation_requested)
                self.assertEqual(receipt["result"]["required_action"], "")
                self.assertEqual(receipt["result"]["recovery_command"], "")
                self.assertEqual(receipt["resume_command"], "")
                self.assertEqual((receipt["parent_next"], receipt["parent_next_command"]), ("inspect-receipt", ""))
                self.assertIn("Inspect the unit cancellation receipt.", next_line(receipt))
                self.assertNotIn("start --route", next_line(receipt))
                self.assertEqual(self.commands, [])


class UnitAgainTest(UnitLegBase):
    def test_the_compose_again_command_of_an_autoclosed_unit_route_names_its_unit(self):
        route, _ = self.unit_route()
        argv = __import__("shlex").split(W._compose_again(route))
        self.assertEqual(argv[argv.index("--unit") + 1], "qa/code-review")
        self.assertNotIn("--capability", argv)
        # ... and the command it gives is one compose accepts (`--explain` writes nothing and starts nothing).
        explain = argv[:argv.index("--start")] + argv[argv.index("--start") + 1:] + ["--explain"]
        done = subprocess.run(explain, text=True, capture_output=True, check=False, cwd=str(R.ROOT),
                              env={**F.clean_environment(), "AGENT_HOME": str(R.ROOT)})
        self.assertNotIn("compose-unit-conflict", done.stderr)
        self.assertNotIn("compose-capability-unknown", done.stderr)


class UnitFailedLegTest(UnitLegBase):
    def test_a_leg_that_did_not_pass_says_what_to_run_instead_of_repeating_the_start(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text(UnitLegBase.leg_row(route, path, status="done"))
        outcome = {"attempt_id": W.attempt_id(route, "unit"), "classification": "failure"}
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            result = W.start_work(route, path, jobs, run=lambda *a, **k: self.fail("a finished leg is never launched"))
        self.assertEqual((result["state"], result["required_action"]), ("needs-attention", "compose-again"))
        self.assertIn("--unit qa/code-review", result["parent_next_command"])
        self.assertNotIn("start --route", result["parent_next_command"])


    def test_silent_unit_exit_has_no_automatic_replacement_and_can_compose_again(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        original = self.leg_row(route, path, status="done", note="dead-worker-silent-exit",
                                launch_started="1", failure_class="protocol", artifact_root=route["artifact_root"])
        jobs.write_text(original)
        aid = W.attempt_id(route, "unit")

        def unexpected_launch(*args, **kwargs):
            self.fail("a resultless unit must not start an automatic replacement")

        self.assertIsNone(REPLACEMENT.accept_terminal_delivery_duty(jobs, aid))
        self.assertEqual(REPLACEMENT.advance_batch(jobs, {aid}, run=unexpected_launch), ({aid}, [], []))
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready"}):
            receipt = W.start_work(route, path, jobs, run=unexpected_launch)
        self.assertEqual((receipt["state"], receipt["required_action"]), ("needs-attention", "compose-again"))
        self.assertIn("--unit qa/code-review", receipt["parent_next_command"])
        self.assertEqual(jobs.read_text(), original)
        self.assertIsNone(REPLACEMENT.source_reservation(jobs, aid))


class UnitCapacityStartTest(UnitLegBase):
    def test_governor_refusal_before_registration_keeps_the_original_slot(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        jobs.touch()

        def refuse(command, **kwargs):
            self.assertIn(W.attempt_id(route, "unit"), command)
            return subprocess.CompletedProcess(command, 75,
                "check=failed\nreason=model-worker-governor-denied\nretryable=1\nrefusal=rolling-start-budget\n"
                "worker_class=dispatch\nretry_after_seconds=30\nchild_spawned=0\n", "")

        with mock.patch.object(W, "prepare_task_request", return_value=None):
            receipt = W.start_work(route, path, jobs, run=refuse)
        self.assertEqual((receipt["state"], receipt["reason"]), ("waiting-capacity", "launch-capacity-wait"))
        self.assertFalse(receipt["spawned"])
        self.assertEqual(jobs.read_text(), "")

    def test_registered_capacity_pause_is_not_a_compose_again(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        aid = W.attempt_id(route, "unit")
        jobs.write_text(self.leg_row(route, path, status="done", launch_started="1",
                                    artifact_root=route["artifact_root"], failure_class="capacity",
                                    note="dead-capacity"))
        hold = {"state": "needs-attention", "reason": "replacement-capacity-wait",
                "harness": "claude", "source_attempt_id": aid, "retry_at": "2099-01-01T00:00:00Z"}
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value={"classification": "failure"}), \
                mock.patch("dispatch_replacement.advance", return_value=hold) as advance, \
                mock.patch("capacity_auto_resume.arm", return_value=None):
            receipt = W.start_work(route, path, jobs)
        self.assertEqual((receipt["state"], receipt["reason"]), ("waiting-capacity", "unit-capacity-wait"))
        self.assertEqual(receipt["source_attempt_id"], aid)
        self.assertEqual(receipt["retry_at"], hold["retry_at"])
        advance.assert_called_once()


class UnitTupleTest(UnitLegBase):
    def test_the_binding_takes_its_axes_from_the_recipe_and_the_unit_head(self):
        route, path = self.unit_route()
        binding = BINDING.derive_frame_route_binding(
            path, worktree=route["cwd"], capability="unit", capability_mode="default", intensity="quick",
            harness="claude", route_node="unit")
        self.assertEqual((binding.worker_type, binding.unit, binding.dispatch_depth, binding.route_node),
                         ("review", "qa/code-review", 1, "unit"))
        self.assertEqual(binding.completion_gate, "unit-complete")
        # A unit route's leg is not an owner: the quick owner binding still refuses it by name.
        with self.assertRaises(BINDING.OwnerRouteBindingError):
            BINDING.derive_quick_owner_binding(path, worktree=route["cwd"], capability="unit",
                                               capability_mode="default", intensity="quick", harness="claude")

    def test_the_attempt_policy_reads_the_sealed_leg_and_admits_one_attempt(self):
        route, path = self.unit_route()
        policy = headless_attempt_policy(
            route_file=str(path), route_node="unit", intensity="quick", harness="claude", dispatch_depth=1,
            parent_slug=None, execution_surface="registered-headless", registered_worker=True, fallback_hop=None,
            fallback_ordinal=0, parent_harness="claude", parent_transport="headless", parent_sandbox="default",
            launch_authority="conductor")
        self.assertTrue(policy["quick"])
        self.assertEqual(policy["fallback_hop"], "same-harness-headless")

    def test_a_leg_that_is_not_the_unit_heads_is_refused(self):
        route, path = self.unit_route()
        route["nodes"][0]["worker_type"] = "stage"       # not what the unit head makes of the leg
        forged = Path(self.root) / "forged.json"
        forged.write_text(json.dumps(route))
        self.assertIsNone(BINDING._unit_leg_axes({"unit": "qa/does-not-exist"}))
        with self.assertRaises(BINDING.OwnerRouteBindingError):
            BINDING.derive_frame_route_binding(
                forged, worktree=route["cwd"], capability="unit", capability_mode="default", intensity="quick",
                harness="claude", route_node="unit")


class BlockingReviewEndTest(UnitLegBase):
    """A review that ended FAIL with its report is finished work (the most common end of a PR review):
    the runtime reads it as a pass whose judgment on the subject is negative, as it does for an audit."""
    REPORT = ("# Review\n\nFinal verdict: changes requested — head abc1234\n\n## Blocking findings\n\n- HB-BLOCKER: empty list ignored\n\n"
              "## Verification\n\n- Read the diff\n")

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.jobs = Path(self.root) / "jobs.log"
        self.attempt = W.attempt_id(self.route, "unit")
        self.report = Path(self.root) / "report.md"
        self.report.write_text(self.REPORT)
        self.worktree = Path(self.route["cwd"])

    def write(self, text):
        self.report.write_text(text)

    def log(self, verdict="FAIL", blocker="HB-BLOCKER empty list ignored", artifact=None):
        text = f"artifact: {artifact or self.report}\nverdict: {verdict}\nblocker: {blocker}"
        log = Path(self.root) / "leg.jsonl"
        log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text}) + "\n")
        return log

    def binding(self, **extra):
        meta = DC.parse_registry_metadata(self.leg_row(self.route, self.path, status="done",
                                                        log_file=str(self.log()), **extra).split("\t")[5].strip())
        return meta

    def terminal(self, binding=None, **log):
        binding = binding or self.binding()
        return TERMINAL.inspect_terminal_attempt(
            self.log(**log), worktree=self.worktree, artifact_root_metadata=self.route["artifact_root"],
            worker_type=binding["worker_type"], artifact_binding=binding)

    def test_the_end_leg_with_a_report_reads_as_a_pass_with_a_negative_assessment(self):
        view = self.terminal()
        self.assertEqual((view["verdict"], view["assessment_verdict"], view["failure_class"]), ("PASS", "FAIL", "pass"))
        self.assertEqual((view["failure_note"], view["blocker_reason"]), ("", "none"))
        self.assertNotIn("review_verdict_conflict", view)      # the envelope said FAIL: no PASS contradicts its report
        carrier, extra = TERMINAL.carrier_terminal_note(
            self.log(), worktree=self.worktree, artifact_root_metadata=self.route["artifact_root"],
            worker_type="review", artifact_binding=self.binding())
        self.assertEqual((carrier["verdict"], extra["reconcile_reason"]), ("PASS", "completed-audit-with-findings"))

    def test_only_a_report_that_opens_on_a_closed_negative_judgment_is_promoted(self):
        promoted = {
            "changes requested": self.REPORT,
            "changes requested, Korean": "# 검토\n\n최종 판정: 수정 후 병합 — head abc1234\n\n- 빈 목록을 무시한다\n",
            "a held review": "# R\n\nFinal verdict: hold — head abc1234\n\n- A: bad\n",
            # Where there is no repository the review has no head to name; a unit leg's report still counts.
            "no head named": "# R\n\nFinal verdict: changes requested\n\n- A: bad\n",
            "no head named, Korean": "# R\n\n최종 판정: 수정 후 병합\n\n- A: bad\n",
            "a held review, no head": "# R\n\n최종 판정: 보류.\n\n- A: bad\n",
            "a hyphen for the dash": "# R\n\nFinal verdict: changes requested - head abc1234\n\n- A: bad\n",
            "an en dash for the dash": "# R\n\nFinal verdict: changes requested – head abc1234\n\n- A: bad\n",
            "the whole line in backticks": "# R\n\n`Final verdict: changes requested — head abc1234`\n\n- A: bad\n",
            "the placeholder left as written": "# R\n\nFinal verdict: changes requested — head <sha>\n\n- A: bad\n",
            # (라) The same verdict in both forms on one line, as the instruction names both (an observed review report, 11-26).
            "both forms on one line": ("## 📋 Code Review Results\n\nFinal verdict: changes requested — head "
                                       "08937a052edef9ca9fbd9d67fb3b4e7a3e26c4a9 · 최종 판정: 수정 후 병합 — head 08937a0\n\n- A: bad\n"),
            "both forms, no head": "# R\n\nFinal verdict: changes requested · 최종 판정: 수정 후 병합\n\n- A: bad\n",
        }
        for name, text in promoted.items():
            with self.subTest(name):
                self.write(text)
                view = self.terminal()
                self.assertEqual((view["verdict"], view["assessment_verdict"], view["failure_class"]),
                                 ("PASS", "FAIL", "pass"))
        # The same FAIL end whose report does not open on one: a failed end as before, with the
        # review_blocking note a sealed leg's reader answers with the confirm notice and `compose-again`.
        stays = {
            "a report that says no review was done": "# Review\n\nCould not perform the review: the diff was not available.\n",
            "an empty report": "",
            "a report that declares a pass": "# Review\n\n## verdict: PASS\n\nNo blocking issues.\n",
            "a report that declares BLOCKED": "# Review\n\n## verdict: BLOCKED\n\nThe diff was not available.\n",
            "a report that says mergeable": "# R\n\nFinal verdict: mergeable — head abc1234\n\n- fine\n",
            "a verdict of its own tokens only": "# Review\n\nFinal verdict: issues\n\n- A: bad\n",
            "a heading verdict that the completion gates read as no pass":
                "# R\n\n## verdict: FAIL\n\nFinal verdict: changes requested — head abc1234\n\n- A: bad\n",
            "a mergeable verdict without a head": "# R\n\nFinal verdict: mergeable\n\n- fine\n",
            "a verdict with a note after it": "# R\n\nFinal verdict: changes requested (2 blocking)\n\n- A: bad\n",
            "a line that is not a verdict": "# R\n\nI think this needs changes requested.\n\n- A: bad\n",
            "two different verdicts on one line":
                "# R\n\nFinal verdict: mergeable — head abc1234 · 최종 판정: 수정 후 병합 — head abc1234\n\n- A: bad\n",
            "two negative verdicts on one line":
                "# R\n\nFinal verdict: changes requested — head abc1234 · 최종 판정: 보류 — head abc1234\n\n- A: bad\n",
            "two heads on one line":
                "# R\n\nFinal verdict: changes requested — head abc1234 · 최종 판정: 수정 후 병합 — head def5678\n\n- A\n",
            "a verdict joined to a note": "# R\n\nFinal verdict: changes requested — head abc1234 · 2 blocking\n\n- A\n",
        }
        for name, text in stays.items():
            with self.subTest(name):
                self.write(text)
                view = self.terminal()
                self.assertNotIn("assessment_verdict", view)
                self.assertEqual(view["verdict"], "FAIL")
                self.assertNotEqual(view["failure_class"], "pass")
                self.assertEqual(view["failure_note"], TERMINAL.REVIEW_BLOCKING_NOTE)

    def test_an_audit_owner_still_needs_the_head(self):
        self.write("# R\n\nFinal verdict: changes requested\n\n- A: bad\n")
        self.assertIsNone(TERMINAL.closed_review_kind(self.report))
        self.write("# R\n\nFinal verdict: changes requested — head abc1234\n\n- A: bad\n")
        self.assertEqual(TERMINAL.closed_review_kind(self.report), "fix")

    def test_one_verdict_in_both_forms_reads_as_that_verdict_and_two_verdicts_as_none(self):
        both = {"fix": "Final verdict: changes requested — head abc1234 · 최종 판정: 수정 후 병합 — head abc1234",
                "merge": "최종 판정: 병합 가능 — head abc1234 · Final verdict: mergeable — head abc1234.",
                "hold": "Final verdict: hold — head abc1234 · 최종 판정: 보류 — head abc1234"}
        for kind, line in both.items():
            with self.subTest(kind):
                self.write(f"# R\n\n{line}\n\n- A\n")
                self.assertEqual(TERMINAL.closed_review_kind(self.report), kind)
        for line in ("Final verdict: mergeable — head abc1234 · 최종 판정: 수정 후 병합 — head abc1234",
                     "Final verdict: changes requested — head abc1234 · see below",
                     "Final verdict: changes requested — head abc1234 · 최종 판정: 수정 후 병합"):
            with self.subTest(line):
                self.write(f"# R\n\n{line}\n\n- A\n")
                self.assertIsNone(TERMINAL.closed_review_kind(self.report))

    def test_the_verdict_line_table(self):
        # 🟡1 (the PR #1859 review's table): what each line reads as. `kind` is the closed verdict an audit or a
        # disposition handoff reads (`closed_review_kind`); `negative` is what a unit review's FAIL end needs.
        h = "abc1234"
        fix, fix_ko = f"Final verdict: changes requested — head {h}", f"최종 판정: 수정 후 병합 — head {h}"
        merge, merge_ko = f"Final verdict: mergeable — head {h}", f"최종 판정: 병합 가능 — head {h}"
        table = [
            ("fix·fix (en·ko)", f"{fix} · {fix_ko}", "fix", True),
            ("merge·merge", f"{merge} · {merge_ko}", "merge", False),
            ("fix·hold (two negatives)", f"{fix} · 최종 판정: 보류 — head {h}", None, False),
            ("fix·merge", f"{fix} · {merge_ko}", None, False),
            ("merge·fix", f"{merge} · {fix_ko}", None, False),
            ("fix · note", f"{fix} · 2 blocking", None, False),
            ("fix heads differ", f"{fix} · 최종 판정: 수정 후 병합 — head def5678", None, False),
            ("fix·fix no head", "Final verdict: changes requested · 최종 판정: 수정 후 병합", None, True),
            ("fix·fix one head", f"{fix} · 최종 판정: 수정 후 병합", None, True),
            ("conditional", f"Final verdict: changes requested if CI fails — head {h}", None, False),
            ("U+2022 bullet", f"{fix} • {fix_ko}", None, False),
            ("U+30FB katakana", f"{fix} ・ {fix_ko}", None, False),
            ("no spaces ·", f"{fix}·{fix_ko}", None, False),
            ("quote >", f"> {fix} · {fix_ko}", None, False),
            ("backticked whole", f"`{fix} · {fix_ko}`", None, False),
            ("each backticked", f"`{fix}` · `{fix_ko}`", None, True),
            ("bold each", f"**{fix}** · **{fix_ko}**", "fix", True),
            ("trailing period each", f"{fix}. · {fix_ko}.", "fix", True),
            ("three parts", f"{fix} · {fix_ko} · {fix}", "fix", True),
            ("dash - no head ·", f"Final verdict: changes requested - head {h} · 최종 판정: 수정 후 병합", None, True),
            ("a short and a full head of one commit", f"Final verdict: changes requested — head {h}def0 · {fix_ko}",
             "fix", True),
            ("two lines ko/en", f"{fix_ko}\n{fix}", "fix", True),
            ("two lines merge ko/en", f"{merge_ko}\n{merge}", "merge", False),
        ]
        for name, line, kind, negative in table:
            with self.subTest(name):
                self.write(f"# R\n\n{line}\n\n- A\n")
                self.assertEqual((TERMINAL.closed_review_kind(self.report),
                                  TERMINAL._opens_on_negative_verdict(self.report)), (kind, negative))

    def test_the_verdict_conflict_check_still_runs_for_an_end_that_is_not_promoted(self):
        self.write("# Review\n\n## verdict: FAIL\n\n- A: bad\n")
        view = self.terminal(verdict="PASS", blocker="none")       # a PASS envelope over a FAIL report
        self.assertEqual(view["review_verdict_conflict"], "artifact-fail-envelope-pass")
        self.assertNotIn("assessment_verdict", view)

    def test_every_other_end_keeps_the_reading_it_had(self):
        cases = {
            "a staged review worker (no leg seal)": ({"workflow_completion": ""}, {}),
            "a depth-2 review": ({"dispatch_depth": "2"}, {}),
            "a frame leg": ({"worker_type": "frame"}, {}),
            "a dead leg": ({"note": "dead-worker-fail"}, {}),
            "the leg did not work (BLOCKED)": ({}, {"verdict": "BLOCKED", "blocker": "cannot read the diff"}),
            "no report named": ({}, {"artifact": "-"}),
            "a report that is not there": ({}, {"artifact": Path(self.root) / "absent.md"}),
        }
        for name, (extra, log) in cases.items():
            with self.subTest(name):
                view = self.terminal(self.binding(**extra), **log)
                self.assertNotIn("assessment_verdict", view)
                self.assertNotEqual(view.get("failure_class"), "pass")

    def test_a_review_of_another_route_is_not_promoted(self):
        # The route record is read from the sealed file: a staged route's review (not a unit route) stays a failure.
        staged = json.loads(self.path.read_text())
        staged["capability"] = "autopilot-code"
        other = Path(self.root) / "other-route.json"
        other.write_text(json.dumps(staged))
        view = self.terminal(self.binding(route_file=str(other)))
        self.assertNotIn("assessment_verdict", view)

    def test_the_sealed_summary_says_the_review_completed_with_findings(self):
        t = work_intent.ui_translator()
        meta = self.binding()
        slot = DTC.terminal_slot(Path(self.route["artifact_root"]), self.route["route_id"], self.attempt)
        slot.mkdir(parents=True)
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", log_file=str(self.log())))
        request = SimpleNamespace(route_file=self.path, jobs=self.jobs, owner_attempt_id=self.attempt)
        with mock.patch.object(DTC, "_completion_cost", return_value=None):
            DTC._seal_completion_summary(request, self.route, str(self.report), slot)
        text = work_intent.sealed_summary(meta, self.attempt)
        self.assertIn(t("completion.result_audit_findings"), text)
        self.assertIn(t("completion.verdict", verdict="changes requested — head abc1234"), text)
        self.assertIn("HB-BLOCKER: empty list ignored", text)
        self.assertIn(str(self.report), text)
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", log_file=str(self.log())))
        body = JOIN.completion_followup_text(
            {"children": [{"attempt_id": self.attempt, "required_action": "advance-completed"}]}, jobs=str(self.jobs))
        self.assertIn(text, body)
        self.assertNotIn("inspect-done-failure", body)
        for frame_word in ("current frame results", "a failed frame", "start --route"):
            self.assertNotIn(frame_word, body)

    def test_a_review_that_is_not_promoted_names_its_report_and_no_frame(self):
        self.write("# Review\n\nCould not perform the review: the diff was not available.\n")
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", log_file=str(self.log())))
        body = JOIN.completion_followup_text(
            {"children": [{"attempt_id": self.attempt, "required_action": "inspect-done-failure"}]}, jobs=str(self.jobs))
        self.assertIn(f"Report: {self.report}", body)
        self.assertIn("start --route", body)            # the same handle as before
        for frame_word in ("current frame results", "a failed frame"):
            self.assertNotIn(frame_word, body)
        meta = DC.parse_registry_metadata(self.leg_row(self.route, self.path, status="done",
                                                        log_file=str(self.log())).split("\t")[5].strip())
        self.assertEqual(JOIN.unit_leg_report_line(meta), f"Report: {self.report}")
        self.assertEqual(JOIN.unit_leg_report_line({**meta, "worker_type": "owner"}), "")
        self.assertEqual(JOIN.unit_leg_report_line({**meta, "workflow_completion": ""}), "")

    def test_a_dead_leg_still_offers_the_same_work_again(self):
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", note="dead-worker-fail"))
        outcome = {"attempt_id": self.attempt, "classification": "failure"}
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            result = W.start_work(self.route, self.path, self.jobs, run=lambda *a, **k: self.fail("never launched"))
        self.assertEqual((result["state"], result["required_action"]), ("needs-attention", "compose-again"))


class UnitFinishTest(UnitLegBase):
    def launch_args(self, route, path, **overrides):
        values = dict(capability="unit", dispatch_depth=1, route_file=str(path), route_node="unit",
                      worker_type="review", owner_route_binding=None)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_the_end_leg_is_sealed_runtime_v1_and_nothing_else_is(self):
        route, path = self.unit_route()
        self.assertEqual(DC.workflow_completion_receipt(self.launch_args(route, path)),
                         ",workflow_completion=runtime-v1")
        for overrides in ({"capability": "autopilot-code"}, {"dispatch_depth": 2}, {"route_node": "other"},
                          {"route_file": None}):
            with self.subTest(overrides=overrides):
                self.assertEqual(DC.workflow_completion_receipt(self.launch_args(route, path, **overrides)), "")
        owner = argparse.Namespace(worker_type="owner", dispatch_depth=1, route_file=str(path),
                                   owner_route_binding=None, capability="autopilot-code", route_node="one-shot")
        self.assertEqual(DC.workflow_completion_receipt(owner), ",workflow_completion=runtime-v1")

    def test_one_mark_says_which_attempt_finishes_its_route(self):
        self.assertTrue(DC.finishes_route({"worker_type": "owner"}))
        self.assertTrue(DC.finishes_route({"worker_type": "review", "workflow_completion": "runtime-v1",
                                           "dispatch_depth": "1"}))
        self.assertFalse(DC.finishes_route({"worker_type": "review", "dispatch_depth": "1"}))
        self.assertFalse(DC.finishes_route({"worker_type": "review", "workflow_completion": "runtime-v1",
                                            "dispatch_depth": "2"}))
        self.assertFalse(DC.finishes_route({"worker_type": "frame", "dispatch_depth": "1"}))

    def test_the_leg_row_names_its_route_and_node_for_the_parent_notice(self):
        route, path = self.unit_route()
        meta = DC.parse_registry_metadata(self.leg_row(route, path, status="done").split("\t")[5].strip())
        self.assertEqual(JOIN.pending_record_identity(meta), (route["route_id"], "unit", JOIN.NO_PARENT_ATTEMPT))

    def test_the_completion_notice_carries_the_sealed_summary_and_points_at_the_report(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text(self.leg_row(route, path, status="done"))
        attempt = W.attempt_id(route, "unit")
        receipt = {"children": [{"attempt_id": attempt, "required_action": "advance-completed"}]}
        before = JOIN.completion_followup_text(receipt, jobs=str(jobs))
        slot = DTC.terminal_slot(Path(route["artifact_root"]), route["route_id"], attempt)
        slot.mkdir(parents=True)
        summary = ("[Completion summary] unit · route " + route["route_id"] + "\nVerdict: issues\n"
                   "Unverified / remaining: one blocking finding\nArtifact: /r/units/unit/report.md")
        (slot / work_intent.SUMMARY_NAME).write_text(summary + "\n")
        text = JOIN.completion_followup_text(receipt, jobs=str(jobs))
        self.assertNotIn("Verdict: issues", before)
        self.assertIn("\n" + summary, text)
        self.assertIn("no harvest", text)
        self.assertNotIn("start --route", text)     # a finished leg asks for no restart

    def test_a_failed_leg_offers_the_route_start_not_a_summary(self):
        route, path = self.unit_route()
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text(self.leg_row(route, path, status="done", workflow_completion="runtime-v1"))
        attempt = W.attempt_id(route, "unit")
        text = JOIN.completion_followup_text(
            {"children": [{"attempt_id": attempt, "required_action": "inspect-done-failure"}]}, jobs=str(jobs))
        self.assertNotIn("Verdict", text)


class ReviewSectionTest(unittest.TestCase):
    def test_a_review_reports_blocking_findings_are_the_summarys_remaining_items(self):
        report = ("# Review of PR\n\nFinal verdict: issues\n\n## Blocking findings\n\n- A: empty list is ignored\n\n"
                  "## Verification\n\n- Read the diff\n")
        sections = work_intent.report_sections(report)
        self.assertEqual(sections["remaining"], ["A: empty list is ignored"])
        self.assertEqual(sections["verified"], ["Read the diff"])
        korean = "# 검토\n\n최종 판정: 병합 불가\n\n## 병합을 막는 것\n\n- 빈 목록을 무시한다\n"
        self.assertEqual(work_intent.report_sections(korean)["remaining"], ["빈 목록을 무시한다"])
        self.assertEqual(work_intent.report_verdict(korean), "병합 불가")
        clean = "# 검토\n\n최종 판정: 병합 가능\n\n## 병합을 막는 것\n\n없음\n"
        self.assertNotIn("remaining", work_intent.report_sections(clean))   # a none line is no remaining item

    def result(self, text):
        t = work_intent.ui_translator()
        sections = work_intent.report_sections("# Review\n\n" + text)
        return sections, work_intent._result_line({}, t, sections.get("remaining") or [],
                                                  limits=sections.get("limits") or [],
                                                  follow_up=sections.get("follow_up") or [],
                                                  verified=sections.get("verified") or [])

    NONE_ROWS = (   # a review that says it blocks nothing: no "미검증·남은 것: none/없음" line, result "완료"
        "## 막는 지적 없음\n\n막는 문제는 찾지 못했다.\n",
        "## 막는 지적\n\n없음. 막는 문제를 찾지 못했다.\n",
        "## 병합을 막는 것\n\n없음. 확인하지 못한 것은 아래에 따로 적었다.\n",
        "## Blocking\n\n- none (could not reproduce the reported failure)\n",
        "## No blocking issues\n\nThe change is fine.\n",
        "**Blocking**: none\n",
        "## 막는 지적\n\n없음\n",
        "## Blocking\n\n- None.\n",
        "## Blocking: none\n\n- A: this heading says there is none\n",
        "## Blocking findings\n\n| Blocking | none |\n",
    )

    def test_a_review_that_says_it_blocks_nothing_is_not_read_as_failed(self):
        t = work_intent.ui_translator()
        for text in self.NONE_ROWS:
            with self.subTest(text=text):
                sections, result = self.result(text)
                self.assertNotIn("remaining", sections)
                self.assertEqual(result, t("completion.result_done"))

    # When in doubt a finding stays: each of these reads as remaining, and none reads as "완료".
    OPEN_ROWS = (
        # the nine forms the closure check named
        ("## Blocking issues not fixed\n\n- A: the parser drops the last token\n", "A: the parser drops the last token"),
        ("## Blocking — not yet addressed\n\n- A: the parser drops the last token\n", "A: the parser drops the last token"),
        ("## Blockers still open (2 were fixed earlier)\n\n- A: the parser drops the last token\n", "A: the parser drops the last token"),
        ("## Blocking findings (none resolved)\n\n- A: the parser drops the last token\n", "A: the parser drops the last token"),
        ("## 막는 지적 — 수정했다고 했으나 그대로\n\n- 빈 목록을 무시한다\n", "빈 목록을 무시한다"),
        ("## Blocking findings\n\n- None of the callers handle the empty list\n", "None of the callers handle the empty list"),
        ("## Blocking findings\n\n- None-check is missing in parse()\n", "None-check is missing in parse()"),
        ("## Blocking findings\n\n- N/A values crash the importer\n", "N/A values crash the importer"),
        ("## Blocking findings\n\n- None found in parse(); but A: render() breaks on empty input\n",
         "None found in parse(); but A: render() breaks on empty input"),
        ("**Blocking**: None of the callers handle the empty list\n", "None of the callers handle the empty list"),
        ("| Blocking | None of the callers check it |\n", "None of the callers check it"),
        # what the closure check confirmed already stayed open
        ("## Unresolved blockers\n\n- A: bad\n", "A: bad"),
        ("## 해결하지 못한 막는 지적\n\n- A: bad\n", "A: bad"),
        ("## 막는 지적 (고쳐야 함)\n\n- A: bad\n", "A: bad"),
        ("## Blocking findings\n\n- No test covers the failure path\n", "No test covers the failure path"),
        ("## Blocking findings\n\n- 없음을 돌려주는 대신 예외를 던진다\n", "없음을 돌려주는 대신 예외를 던진다"),
        ("## Blocking findings\n\n- Without the lock the counter races\n", "Without the lock the counter races"),
        # a none marker that turns, or one with a sentence of English after it (these two stay in the
        # remaining list; the result line is the general reader's: "완료" for the last)
        ("## Blocking findings\n\n- none, however the cache is stale\n", "none, however the cache is stale"),
        ("## 막는 지적\n\n- 없음. 다만 캐시가 오래됐다.\n", "없음. 다만 캐시가 오래됐다."),
        ("## Blocking findings\n\n- None. The parser drops the last token.\n", "None. The parser drops the last token."),
        # a heading that says none and then turns opens its section
        ("## No blocking issues, but one regression\n\n- A: render() breaks on empty input\n",
         "A: render() breaks on empty input"),
        ("## Zero blockers, however one is open\n\n- A: render() breaks on empty input\n",
         "A: render() breaks on empty input"),
        ("## 막는 지적 없음. 다만 하나는 남았다\n\n- A: 빈 목록에서 죽는다\n", "A: 빈 목록에서 죽는다"),
    )

    def test_a_blocking_finding_is_never_read_as_none(self):
        t = work_intent.ui_translator()
        for text, item in self.OPEN_ROWS:
            with self.subTest(text=text):
                sections, result = self.result(text)
                self.assertEqual(sections.get("remaining"), [item])
                if not text.endswith("The parser drops the last token.\n"):
                    self.assertNotEqual(result, t("completion.result_done"))

    def test_the_first_review_tables_rows_after_the_fix(self):
        t = work_intent.ui_translator()
        done = t("completion.result_done")
        for text in self.NONE_ROWS[:4] + self.NONE_ROWS[4:7]:         # rows 1-4, 6, 7 (7 has two shapes)
            self.assertEqual(self.result(text)[1], done)
        sections, result = self.result("## Blocking issues fixed in this round\n\n- race in Y fixed\n")   # row 5
        self.assertIn(result, {done, done + " — " + t("completion.result_check")})   # either; never a failure
        self.assertNotEqual(result, t("completion.result_with_remaining"))

    def test_a_real_blocking_finding_still_counts_and_the_none_reading_is_this_sections_alone(self):
        sections = work_intent.report_sections("# R\n\n## Blocking findings\n\n- A: bad\n- 없는 파일을 읽는다\n")
        self.assertEqual(sections["remaining"], ["A: bad", "없는 파일을 읽는다"])
        other = work_intent.report_sections("# R\n\n## 남은 것\n\n없음. 다만 하나는 못 했다.\n")
        self.assertEqual(other["remaining"], ["없음. 다만 하나는 못 했다."])            # other headings read as before


class UnitPromptTest(UnitLegBase):
    def adapter(self):
        spec = importlib.util.spec_from_file_location(
            "claude_dispatch_headless_unit", HERE.parent / "adapters/claude/bin/dispatch-headless.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_unit_leg_carries_the_users_work_style_records_before_the_brief(self):
        route, path = self.unit_route()
        self.assertEqual(route["field"], "code")
        adapter = self.adapter()
        args = argparse.Namespace(
            capability="unit", capability_mode="default", worker_mode="qa/code-review", qa="quick",
            intensity="quick", dispatch_depth=1, worker_type="review", attempt_id="att-x", assigned_contract="unit",
            route_node="unit", route_file=str(path), route_id=route["route_id"], owner_route_binding=None,
            model_role="fast reviewer", model_profile="light", resolved_model_settings={}, parent_slug=None,
            parent_session_id=None, capability_owner="unit", owner_harness="claude", worktree=route["cwd"],
            artifact_root=route["artifact_root"], unit="qa/code-review", runtime_dir="", raw_data_dir="",
            parent_attempt_id=None, route_hash=route["route_hash"], launch_head="0" * 40, diff_attribution=None)
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text("")
        env = dict(T.P.prepare_route_artifact_env(path, start=True, jobs=jobs))
        store = Path(self.root) / "ws-store"
        store.mkdir()
        mem = Path(R.ROOT) / "tools" / "memory" / "mem.py"
        env["MEM_STORE"] = str(store)
        with mock.patch.dict(os.environ, env), mock.patch.object(adapter.DISPATCH_MAIN, "dispatch_metadata_prompt",
                                                                 return_value="Dispatch metadata:\n- unit: x\n"):
            def prompt():
                return adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, BRIEF)
            empty = prompt()
            self.assertNotIn("Unit bootstrap material", empty)
            for body, topic in (("WS-UNIT keep comments short.", "unit:qa/code-review"),
                                ("WS-FIELD name the failing command.", "field:code"),
                                ("WS-OTHER another unit's habit.", "unit:dev/backend")):
                subprocess.run([sys.executable, str(mem), "add", "durable", "preference", body, "--scope", "global",
                                "--topic", topic], cwd=route["cwd"], check=True, capture_output=True, env=dict(os.environ))
            carried = prompt()
        self.assertEqual(carried.count("Unit bootstrap material"), 1)
        self.assertLess(carried.index("WS-UNIT"), carried.index("WS-FIELD"))
        self.assertNotIn("WS-OTHER", carried)
        self.assertLess(carried.index("Unit: qa/code-review"), carried.index("Unit bootstrap material"))
        self.assertLess(carried.index("Unit bootstrap material"), carried.index("Assignment:\n" + BRIEF.rstrip()))
        self.assertEqual(carried.replace(carried[carried.index("Unit bootstrap material"):
                                                carried.index("Assignment:\n")], ""), empty)

    def test_the_unit_worker_is_told_no_qa_and_no_intensity_while_the_row_keeps_them(self):
        # 11-20: a unit route has no intensity, so its worker reads `- qa: -` / `- intensity: -` (an Opus review is not
        # handed the word `quick`). The args (and so the row, the environment and the budget) keep `quick`; every other
        # capability's metadata reads as it did.
        route, path = self.unit_route()
        adapter = self.adapter()
        args = argparse.Namespace(
            capability="unit", capability_mode="default", worker_mode="qa/code-review", qa="quick",
            intensity="quick", dispatch_depth=1, worker_type="review", attempt_id="att-x", assigned_contract="unit",
            route_node="unit", route_file=str(path), route_id=route["route_id"], owner_route_binding=None,
            model_role="fast reviewer", model_profile="light", resolved_model_settings={}, parent_slug=None,
            parent_session_id=None, capability_owner="unit", owner_harness="claude", worktree=route["cwd"],
            artifact_root=route["artifact_root"], unit="qa/code-review", runtime_dir="", raw_data_dir="",
            parent_attempt_id=None, route_hash=route["route_hash"], launch_head="0" * 40, diff_attribution=None)
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text("")
        env = dict(T.P.prepare_route_artifact_env(path, start=True, jobs=jobs))
        with mock.patch.dict(os.environ, env):
            prompt = adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, BRIEF)
            metadata = adapter.DISPATCH_MAIN.dispatch_metadata_prompt(adapter._ADAPTER, args)
            staged = adapter.DISPATCH_MAIN.dispatch_metadata_prompt(
                adapter._ADAPTER, argparse.Namespace(**{**vars(args), "capability": "autopilot-code", "intensity": "standard",
                                                        "qa": "standard"}))
        for text in (prompt, metadata):
            self.assertIn("\n- qa: -\n- intensity: -\n", text)
            self.assertNotIn("- qa: quick", text)
            self.assertNotIn("- intensity: quick", text)
        self.assertIn("\n- qa: standard\n- intensity: standard\n", staged)
        self.assertEqual((args.qa, args.intensity), ("quick", "quick"))

    def test_the_first_instruction_is_the_unit_the_brief_and_how_to_end(self):
        route, path = self.unit_route()
        adapter = self.adapter()
        args = argparse.Namespace(
            capability="unit", capability_mode="default", worker_mode="qa/code-review", qa="quick",
            intensity="quick", dispatch_depth=1, worker_type="review", attempt_id="att-x", assigned_contract="unit",
            route_node="unit", route_file=str(path), route_id=route["route_id"], owner_route_binding=None,
            model_role="fast reviewer", model_profile="light", resolved_model_settings={}, parent_slug=None,
            parent_session_id=None, capability_owner="unit", owner_harness="claude", worktree=route["cwd"],
            artifact_root=route["artifact_root"], unit="qa/code-review", runtime_dir="", raw_data_dir="",
            parent_attempt_id=None, route_hash=route["route_hash"], launch_head="0" * 40, diff_attribution=None)
        jobs = Path(self.root) / "jobs.log"
        jobs.write_text("")
        env = dict(T.P.prepare_route_artifact_env(path, start=True, jobs=jobs))   # what the launch binds the leg to
        with mock.patch.dict(os.environ, env), mock.patch.object(adapter.DISPATCH_MAIN, "dispatch_metadata_prompt",
                                                                 return_value="Dispatch metadata:\n- unit: x\n"):
            prompt = adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, BRIEF)
            other = adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, argparse.Namespace(**{**vars(args),
                                                          "capability": "autopilot-code"}), BRIEF)
        self.assertIsNone(other)
        self.assertIn("Unit: qa/code-review", prompt)                         # the unit's own text
        self.assertIn("Review adversarially by default", prompt)             # and its stance
        self.assertIn("Assignment:\n" + BRIEF.rstrip(), prompt)               # the brief, unchanged
        self.assertIn("exactly these three newline-delimited", prompt)         # how to end
        self.assertIn("units/unit/report.md", prompt)                          # where the report goes
        # The kernel's safety lines are in; the kernel document, the owner's rules and the route steps are not.
        for line in ("bounded worker, not the user-facing main session", "Preserve permission, safety and git-state guards",
                     "write only inside the assigned scope", "merge, release, installation, cleanup or UI/status publication",
                     "do not publish task branches or PRs", "retire only your own scratch",
                     "authorize cleanup of user worktrees, native HOME", "Do not implement fixes or widen the review"):
            self.assertIn(line, prompt)
        self.assertIn("`Final verdict: changes requested — head <sha>`", prompt)   # the form the runtime reads
        self.assertLess(prompt.index("Final verdict: changes requested"), prompt.index("For the terminal final response"))
        import worker_bootstrap as wb
        self.assertIn("where there is no git repository, leave out ` — head <sha>`", prompt)
        self.assertEqual(wb.unit_verdict_prompt("support"), "")      # only a review's judgment is read
        self.assertNotIn("dispatch another worker", prompt)     # a later route lets a worker call this one
        for owner_only in ("Portable Worker Kernel", "Worker Type", "supervised owner", "owner-execution",
                           "Route steps", "runtime_wait"):
            self.assertNotIn(owner_only, prompt)
        import worker_bootstrap
        self.assertLess(len(worker_bootstrap.unit_safety_prompt("review").encode()), 2048)
        self.assertLess(len(prompt), 15_000)   # the unit's own text grew on main; the first instruction stays one short block

    def test_a_lookup_leg_is_not_told_how_to_start_another_worker_and_other_legs_are_unchanged(self):
        # `general/explore` says "no further workers" and `roles/worker-types/support.md` says a support worker
        # dispatches none, so its first instruction omits the paragraph that teaches a unit worker how; the
        # others' instruction keeps it exactly where it was (right after the safety lines).
        import worker_bootstrap as wb
        call = wb.unit_call_prompt(R.ROOT) + "\n"
        prompts = {}
        for unit, worker_type, role in (("general/explore", "support", "fast tool worker"),
                                        ("qa/code-review", "review", "fast reviewer")):
            route, path = self.unit_route(unit=unit)
            adapter = self.adapter()
            args = argparse.Namespace(
                capability="unit", capability_mode="default", worker_mode=unit, qa="quick",
                intensity="quick", dispatch_depth=1, worker_type=worker_type, attempt_id="att-x",
                assigned_contract="unit", route_node="unit", route_file=str(path), route_id=route["route_id"],
                owner_route_binding=None, model_role=role, model_profile="light", resolved_model_settings={},
                parent_slug=None, parent_session_id=None, capability_owner="unit", owner_harness="claude",
                worktree=route["cwd"], artifact_root=route["artifact_root"], unit=unit, runtime_dir="",
                raw_data_dir="", parent_attempt_id=None, route_hash=route["route_hash"], launch_head="0" * 40,
                diff_attribution=None)
            jobs = Path(self.root) / "jobs.log"
            jobs.write_text("")
            env = dict(T.P.prepare_route_artifact_env(path, start=True, jobs=jobs))
            with mock.patch.dict(os.environ, env), mock.patch.object(
                    adapter.DISPATCH_MAIN, "dispatch_metadata_prompt", return_value="Dispatch metadata:\n- unit: x\n"):
                prompts[unit] = adapter.DISPATCH_MAIN.unit_leg_prompt(adapter._ADAPTER, args, BRIEF)
        self.assertNotIn("Starting another worker", prompts["general/explore"])
        self.assertNotIn("--ephemeral", prompts["general/explore"])
        self.assertIn("No further workers", prompts["general/explore"])
        self.assertIn(wb.unit_safety_prompt("review") + call, prompts["qa/code-review"])


if __name__ == "__main__":
    unittest.main()
