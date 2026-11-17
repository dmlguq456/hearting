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
import dispatch_terminal_commit as DTC  # noqa: E402
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

    def test_a_finished_leg_reports_its_outcome(self):
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done"))
        outcome = {"attempt_id": W.attempt_id(self.route, "unit"), "classification": "success"}
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            result = W.start_work(self.route, self.path, self.jobs, run=self.run_launch)
        self.assertEqual((result["state"], result["result"]), ("completed", outcome))
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
    """A review that ended FAIL with its report is finished work (the most common end of a PR review)."""
    REPORT = ("# Review\n\nFinal verdict: issues\n\n## Blocking findings\n\n- HB-BLOCKER: empty list ignored\n\n"
              "## Verification\n\n- Read the diff\n")

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.jobs = Path(self.root) / "jobs.log"
        self.attempt = W.attempt_id(self.route, "unit")
        self.report = Path(self.root) / "report.md"
        self.report.write_text(self.REPORT)
        encoded = base64.urlsafe_b64encode(str(self.report).encode()).decode().rstrip("=")
        terminal = {"state": "valid", "verdict": "FAIL", "artifact_state": "readable", "artifact_path_b64": encoded}
        patch = mock.patch("codex_dispatch_terminal.inspect_terminal_attempt", return_value=terminal)
        patch.start()
        self.addCleanup(patch.stop)

    def row(self, **extra):
        return self.leg_row(self.route, self.path, status="done", note="completed-review-blocking",
                            log_file=str(self.report), **extra)

    def test_the_summary_says_the_review_completed_with_findings_not_that_the_work_died(self):
        t = work_intent.ui_translator()
        text = work_intent.blocked_review_summary(
            DC.parse_registry_metadata(self.row().split("\t")[5].strip()), self.jobs)
        self.assertIn(t("completion.result", result=t("completion.result_audit_findings")), text)
        self.assertNotIn(t("completion.result_with_remaining"), text)
        self.assertIn(t("completion.verdict", verdict="issues"), text)
        self.assertIn(t("completion.remaining", items="HB-BLOCKER: empty list ignored"), text)
        self.assertIn(str(self.report), text)

    def test_the_notice_carries_that_summary_and_no_frame_words(self):
        t = work_intent.ui_translator()
        self.jobs.write_text(self.row())
        text = JOIN.completion_followup_text(
            {"children": [{"attempt_id": self.attempt, "required_action": "inspect-done-failure"}]}, jobs=str(self.jobs))
        self.assertIn("Owner finished; no harvest.", text)
        self.assertIn(t("completion.result_audit_findings"), text)
        self.assertIn("HB-BLOCKER", text)
        for frame_word in ("current frame results", "a failed frame", "Continue the existing work", "start --route"):
            self.assertNotIn(frame_word, text)

    def test_the_same_end_of_other_workers_is_unchanged(self):
        # A staged review worker, a frame leg and an owner: no runtime-v1 leg seal, so no summary is made.
        for extra in ({"workflow_completion": ""}, {"worker_type": "frame", "workflow_completion": ""}):
            with self.subTest(extra=extra):
                self.jobs.write_text(self.row(**extra))
                with mock.patch.object(work_intent, "blocked_review_summary", side_effect=AssertionError("read")):
                    text = JOIN.completion_followup_text(
                        {"children": [{"attempt_id": self.attempt, "required_action": "inspect-done-failure"}]},
                        jobs=str(self.jobs))
                self.assertNotIn(work_intent.ui_translator()("completion.result_audit_findings"), text)

    def test_a_dead_leg_without_a_readable_report_keeps_the_notice_it_had(self):
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", note="dead-worker-fail"))
        text = JOIN.completion_followup_text(
            {"children": [{"attempt_id": self.attempt, "required_action": "inspect-done-failure"}]}, jobs=str(self.jobs))
        self.assertNotIn(work_intent.ui_translator()("completion.result_audit_findings"), text)

    def test_start_route_reports_the_verdict_and_does_not_offer_the_same_review_again(self):
        self.jobs.write_text(self.row())
        outcome = {"attempt_id": self.attempt, "classification": "failure", "required_action": "inspect-done-failure"}
        with mock.patch.object(W, "default_parent_session_id", return_value="parent"), \
                mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            result = W.start_work(self.route, self.path, self.jobs,
                                  run=lambda *a, **k: self.fail("a finished leg is never launched"))
        self.assertEqual((result["state"], result["reason"]), ("completed", "unit-review-blocking"))
        self.assertIn(work_intent.ui_translator()("completion.result_audit_findings"), result["summary"])
        self.assertNotIn("compose-again", json.dumps(result))
        self.assertNotIn("parent_next_command", result)


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

    ROWS = (   # (report, the result line after the fix); each read "완료" or worse before (see the cycle report)
        ("## 막는 지적 없음\n\n막는 문제는 찾지 못했다.\n", "완료"),
        ("## 막는 지적\n\n없음. 막는 문제를 찾지 못했다.\n", "완료"),
        ("## 병합을 막는 것\n\n없음. 확인하지 못한 것은 아래에 따로 적었다.\n", "완료"),
        ("## Blocking\n\n- none (could not reproduce the reported failure)\n", "완료"),
        ("## Blocking issues fixed in this round\n\n- race in Y fixed\n", "완료"),
        ("## No blocking issues\n\nThe change is fine.\n", "완료"),
        ("**Blocking**: none\n", "완료"),
        ("## 막는 지적\n\n없음\n", "완료"),
    )

    def test_a_review_that_says_it_blocks_nothing_is_not_read_as_failed(self):
        t = work_intent.ui_translator()
        for text, expected in self.ROWS:
            with self.subTest(text=text):
                sections = work_intent.report_sections("# Review\n\n" + text)
                self.assertNotIn("remaining", sections)      # no "미검증·남은 것: none/없음" line is made of it
                result = work_intent._result_line({}, t, sections.get("remaining") or [],
                                                  limits=sections.get("limits") or [],
                                                  follow_up=sections.get("follow_up") or [],
                                                  verified=sections.get("verified") or [])
                self.assertEqual(result, t("completion.result_done"))

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
        self.assertNotIn("dispatch another worker", prompt)     # a later route lets a worker call this one
        for owner_only in ("Portable Worker Kernel", "Worker Type", "supervised owner", "owner-execution",
                           "Route steps", "runtime_wait"):
            self.assertNotIn(owner_only, prompt)
        import worker_bootstrap
        self.assertLess(len(worker_bootstrap.unit_safety_prompt("review").encode()), 2048)
        self.assertLess(len(prompt), 14_000)


if __name__ == "__main__":
    unittest.main()
