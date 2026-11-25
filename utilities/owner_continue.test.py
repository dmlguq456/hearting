#!/usr/bin/env python3
"""An owner that takes over from an earlier owner of its route: the session it wakes, what it is told, and a
later request on a closed one-shot route (`correct` after PASS).

The unit route's own turns are `unit_continue.test.py`; the three-harness runs with a fake model are
`harness_behavior_cases_owner_continue.py`. These cases pin the decisions one by one.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import unit_continue as UC  # noqa: E402

ROUTE_ID = "rt-0123456789abcdef"
ROUTE = {"route_id": ROUTE_ID, "route_hash": "h"}
FIRST = "att-" + "1" * 32
SESSION = "5fa3ebc9-8aa3-4cf7-a1a4-f9a54721b128"


def owner(attempt=FIRST, **extra):
    value = {"attempt_id": attempt, "route_id": ROUTE_ID, "owner_route_id": ROUTE_ID, "route_node": "one-shot",
             "harness": "claude", "worker_type": "owner", "dispatch_depth": "1", "capability": "autopilot-code",
             "workflow_completion": "runtime-v1"}
    value.update(extra)
    return value


def launch(**extra):
    value = {"worker_type": "owner", "dispatch_depth": 1, "automatic_retry_of": None, "continues_attempt_id": None,
             "session_plan": None, "turn": 0, "continuation_text": ""}
    value.update(extra)
    return types.SimpleNamespace(**value)


class OwnerSessionPlanTest(unittest.TestCase):
    """The same decision a unit turn uses, read from the owner it takes over from."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.home = self.root / "home"
        self.log = self.root / "owner.log"
        # A supervised owner's receipt log: the session is announced before the first turn.
        self.log.write_text(json.dumps({"type": "dispatch.supervisor.continuation-budget", "limit": 9},
                                       separators=(",", ":")) + "\n"
                            + json.dumps({"type": "dispatch.supervisor.session", "parent_attempt_id": FIRST,
                                          "session_id": SESSION, "cwd": "/w"}, separators=(",", ":")) + "\n")

    def tearDown(self):
        self.dir.cleanup()

    def plan(self, previous, args, harness="claude", **kw):
        row = types.SimpleNamespace(metadata=previous)
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=row):
            return UC.owner_plan_for_args(args, self.root / "jobs.log", harness, **kw)

    def transcript(self):
        path = self.home / "projects" / "-w" / f"{SESSION}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"sessionId": "x"}\n')

    def test_a_first_owner_and_any_other_worker_have_no_plan(self):
        self.assertIsNone(self.plan(owner(), launch()))
        self.assertIsNone(self.plan(owner(), launch(worker_type="stage", dispatch_depth=2, automatic_retry_of=FIRST)))

    def test_a_replacement_wakes_the_announced_session_when_its_file_is_in_the_home(self):
        self.transcript()
        previous = owner(log_file=str(self.log), runtime_home=str(self.home))
        args = launch(automatic_retry_of=FIRST)
        plan = self.plan(previous, args)
        self.assertEqual((plan.kind, plan.session_id, plan.relative), ("resumed", SESSION, f"projects/-w/{SESSION}.jsonl"))
        self.assertIs(args.session_plan, plan)                     # decided once and kept on the launch

    def test_a_later_turn_reads_the_turn_it_follows(self):
        self.transcript()
        plan = self.plan(owner(log_file=str(self.log), runtime_home=str(self.home)), launch(continues_attempt_id=FIRST))
        self.assertEqual(plan.row_value, "resumed")

    def test_no_session_another_harness_or_no_supervisor_start_fresh_with_the_reason(self):
        previous = owner(log_file=str(self.log), runtime_home=str(self.home))
        self.assertEqual(self.plan(previous, launch(automatic_retry_of=FIRST)).row_value, "fresh:session-missing")
        self.transcript()
        self.assertEqual(self.plan(previous, launch(automatic_retry_of=FIRST), "codex").row_value,
                         "fresh:harness-changed")
        refused = self.plan(previous, launch(automatic_retry_of=FIRST), keeps_session=False)
        self.assertEqual((refused.row_value, refused.source), ("fresh:resume-refused", ""))

    def test_an_opencode_owner_session_is_read_from_the_announcement_and_checked_in_its_store(self):
        self.log.write_text(json.dumps({"type": "dispatch.supervisor.session", "runtime": "opencode",
                                        "parent_attempt_id": FIRST, "session_id": "ses_abc", "cwd": "/w"},
                                       separators=(",", ":")) + "\n")
        previous = owner(harness="opencode", log_file=str(self.log), runtime_home="shared-service")
        with mock.patch("opencode_runtime.is_v2", return_value=True), \
                mock.patch.object(UC, "opencode_session_exists", return_value=True):
            plan = self.plan(previous, launch(automatic_retry_of=FIRST), "opencode")
        self.assertEqual((plan.kind, plan.session_id), ("resumed", "ses_abc"))


class OwnerTurnPromptTest(unittest.TestCase):
    RESUMED = UC.SessionPlan("resumed", "", SESSION, "f", "r")
    FRESH = UC.SessionPlan("fresh", "harness-changed", "", "")
    RECOVERY = "\n\n## Verified recovery context\nThe previous owner ended BLOCKED and a person has answered it.\n"

    def test_a_woken_replacement_gets_this_attempts_facts_and_the_recovery_context_only(self):
        text = UC.owner_turn_prompt(launch(automatic_retry_of=FIRST), self.RESUMED, "Dispatch metadata:\n- x\n",
                                    "FULL KERNEL AND ASSIGNMENT", self.RECOVERY, "jobs.log")
        self.assertNotIn("FULL KERNEL AND ASSIGNMENT", text)
        self.assertIn("Dispatch metadata:", text)
        self.assertIn("Verified recovery context", text)
        self.assertIn("nothing the earlier attempt was granted carries over", text)

    def test_a_fresh_replacement_gets_the_whole_prompt_the_context_and_why(self):
        with mock.patch.object(UC, "taken_over_turn", return_value=(1, "")):
            text = UC.owner_turn_prompt(launch(automatic_retry_of=FIRST), self.FRESH, "META", "FULL", self.RECOVERY,
                                        "jobs.log")
        self.assertTrue(text.startswith("FULL" + self.RECOVERY))
        self.assertIn("could not be woken (harness-changed)", text)
        self.assertNotIn("META", text)

    def test_a_fresh_replacement_of_a_later_turn_carries_that_turns_request(self):
        with mock.patch.object(UC, "taken_over_turn", return_value=(2, "also fix the README")):
            text = UC.owner_turn_prompt(launch(automatic_retry_of=FIRST), self.FRESH, "META", "FULL", self.RECOVERY,
                                        "jobs.log")
        self.assertIn("turn 2 of this route", text)
        self.assertIn("also fix the README", text)

    def test_a_later_turn_names_its_number_its_request_and_its_scope(self):
        args = launch(continues_attempt_id=FIRST, turn=2, continuation_text="also fix the README")
        with mock.patch.object(UC, "previous_result", return_value="최종 판정: 통과"):
            woken = UC.owner_turn_prompt(args, self.RESUMED, "META\n", "FULL", "", "jobs.log")
            fresh = UC.owner_turn_prompt(args, self.FRESH, "META\n", "FULL", "", "jobs.log")
        self.assertNotIn("FULL", woken)
        for text in (woken, fresh):
            self.assertIn("turn 2", text)
            self.assertIn("also fix the README", text)
            self.assertIn("not an approval of work beyond it", text)
            self.assertIn("최종 판정: 통과", text)
            self.assertIn("review records and closure stay as they are", text)
        self.assertTrue(fresh.startswith("FULL"))
        self.assertIn("could not be woken (harness-changed)", fresh)

    def test_the_adapter_helper_keeps_every_other_launch_as_it_was(self):
        import dispatch_headless_main as DHM
        args = launch(worker_type="stage", dispatch_depth=2, automatic_retry_of=FIRST, jobs_path="jobs.log")
        self.assertEqual(DHM.owner_continuation_prompt(None, args, "P", "R"), "PR")
        self.assertEqual(DHM.owner_continuation_prompt(None, launch(jobs_path="jobs.log"), "P", "R"), "PR")
        self.assertEqual(DHM.owner_session_arguments(launch(automatic_retry_of=FIRST, session_plan=self.RESUMED)),
                         ["--resume-session", SESSION])
        self.assertEqual(DHM.owner_session_arguments(launch(automatic_retry_of=FIRST, session_plan=self.FRESH)), [])


class OwnerRowAndNoticeTest(unittest.TestCase):
    def test_the_row_records_the_session_and_a_later_turns_place(self):
        import dispatch_contract_rows as ROWS
        plan = UC.SessionPlan("fresh", "session-missing", "", "")
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=False):
            self.assertEqual(ROWS.unit_continue_receipt(launch(automatic_retry_of=FIRST, session_plan=plan)),
                             ",session=fresh:session-missing")
            self.assertEqual(ROWS.unit_continue_receipt(launch(continues_attempt_id=FIRST, turn=2, session_plan=plan)),
                             f",continues_attempt_id={FIRST},turn=2,session=fresh:session-missing")
            self.assertEqual(ROWS.unit_continue_receipt(launch()), "")

    def test_the_end_notice_names_the_session(self):
        self.assertEqual(UC.notice_lines("", owner(session="resumed", automatic_retry_of=FIRST))[0],
                         f"This owner took over from {FIRST} (session: resumed).")
        self.assertEqual(UC.notice_lines("", owner(session="fresh:harness-changed", continues_attempt_id=FIRST,
                                                   turn="2"))[0],
                         "Turn 2 of this route (session: fresh:harness-changed).")
        self.assertEqual(UC.notice_lines("", owner()), ("", ""))


class OwnerChainTest(unittest.TestCase):
    """A one-shot route's owners as turns, and when a later request may start."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.path = self.root / f"{ROUTE_ID}.json"
        self.path.write_text("{}")

    def tearDown(self):
        self.dir.cleanup()

    def ready(self, rows, *, closed=None, completion="complete"):
        with mock.patch("dispatch_notice_state.closed_outcome", return_value=closed), \
                mock.patch("dispatch_terminal_commit.owner_completion_state",
                           return_value=types.SimpleNamespace(state=completion)):
            return UC.ready_to_continue(self.jobs, ROUTE, self.path, UC.OWNER, rows)

    def test_replacements_stay_in_their_turn_and_the_newest_owner_is_followed(self):
        replacement = "att-" + "2" * 32
        rows = {FIRST: ("done", owner()), replacement: ("done", owner(replacement, automatic_retry_of=FIRST)),
                "att-stage": ("done", {**owner("att-stage"), "worker_type": "stage", "dispatch_depth": "2"})}
        UC.submit(self.jobs, ROUTE_ID, "also fix the README")
        turn = UC.next_turn(self.jobs, ROUTE, UC.OWNER, rows)
        self.assertEqual((turn.prev_attempt_id, turn.turn), (replacement, 2))
        self.assertEqual(turn.attempt_id, UC.attempt_id_for(ROUTE_ID, "owner", turn.request_id))

    def test_a_later_request_starts_only_after_a_proven_settled_pass(self):
        UC.submit(self.jobs, ROUTE_ID, "also fix the README")
        rows = {FIRST: ("done", owner())}
        proven = {"route_id": ROUTE_ID, "terminal_gate_proven": True}
        self.assertIsNotNone(self.ready(rows, closed=proven))
        self.assertIsNone(self.ready(rows, closed={"route_id": ROUTE_ID, "terminal_gate_proven": False}))
        self.assertIsNone(self.ready(rows, closed=proven, completion="not-applicable"))   # not a PASS it settled
        self.assertIsNone(self.ready(rows, closed=proven, completion="pending"))
        self.assertIsNone(self.ready({FIRST: ("cancelled", owner())}, closed=proven))      # stopped by the person

    def test_a_turn_a_later_turn_continued_has_nothing_left_to_settle(self):
        later = "att-" + "3" * 32
        record = types.SimpleNamespace(completion_metadata=lambda: {"continues_attempt_id": FIRST})
        view = types.SimpleNamespace(route_alias_candidates=lambda route_id: [record])
        with mock.patch("dispatch_registry_cache.registry_view", return_value=view) as read:
            self.assertFalse(UC.continued_by_later_turn(str(self.jobs), owner()))   # no later request kept: no read
            read.assert_not_called()
            UC.submit(self.jobs, ROUTE_ID, "also fix the README")
            self.assertTrue(UC.continued_by_later_turn(str(self.jobs), owner()))
            self.assertFalse(UC.continued_by_later_turn(str(self.jobs), owner(later)))
        import route_authority
        choice = route_authority.decide_continuation(action="completion-owner", status="done",
            metadata={**owner(), "failure_class": "pass"}, facts={"continued": True})
        self.assertEqual(choice.state, "not-applicable")


class CorrectOwnerRouteTest(unittest.TestCase):
    """`correct` with a message on a closed one-shot route: what is refused, what is kept, what starts."""

    def setUp(self):
        import work_start
        self.W = work_start
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.path = self.root / f"{ROUTE_ID}.json"
        self.path.write_text("{}")
        self.route = {**ROUTE, "capability": "autopilot-code", "artifact_root": self.dir.name,
                      "nodes": [{"id": "one-shot", "kind": "capability-owner", "dispatch_depth": 1,
                                 "unit": "_kernel/owner", "worker_type": "owner", "terminal": True}]}
        self.rows = {FIRST: ("done", owner(failure_class="pass"))}
        self.closed = {"route_id": ROUTE_ID, "terminal_gate_proven": True}
        self.patches = [mock.patch.object(work_start, "_rows", side_effect=lambda jobs: self.rows),
                        mock.patch.object(work_start, "_restore_archived_route"),
                        mock.patch("dispatch_notice_state.closed_outcome", side_effect=lambda *a: self.closed),
                        mock.patch("route_parent_close.row_requested", return_value=False)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.dir.cleanup()

    def call(self, text="also fix the README", route=None, **kw):
        def started(route, path, jobs, **_):
            return {"state": "running", "parent_next": "end-turn", "next_step": "n"}
        with mock.patch.object(self.W, "start_work", side_effect=started) as start, \
                mock.patch.object(self.W, "require_route_parent", **kw):
            return self.W.correct_owner_route(route or self.route, self.path, self.jobs, text), start

    def test_an_open_route_keeps_the_owner_input_path(self):
        self.closed = None
        with mock.patch.object(UC, "ready_to_continue", return_value=None):
            self.assertIsNone(self.call()[0])
        self.assertEqual(UC.entries(self.jobs, ROUTE_ID), [])

    def test_a_closed_pass_route_keeps_the_text_once_and_starts_the_next_turn(self):
        result, start = self.call()
        code, receipt = result
        self.assertEqual((code, receipt["state"], receipt["duplicate"], receipt["continues_attempt_id"]),
                         (0, "running", False, FIRST))
        start.assert_called_once()
        self.assertEqual(len(UC.entries(self.jobs, ROUTE_ID)), 1)

    def test_the_same_text_after_its_turn_ran_starts_nothing(self):
        self.call()
        entry = UC.entries(self.jobs, ROUTE_ID)[0]
        turn_id = UC.attempt_id_for(ROUTE_ID, UC.OWNER, entry["id"])
        self.rows[turn_id] = ("done", owner(turn_id, failure_class="pass", continues_attempt_id=FIRST, turn="2",
                                            session="resumed"))
        (code, receipt), start = self.call()
        self.assertEqual((code, receipt["state"], receipt["turn"], receipt["session"]), (0, "already-asked", 2, "resumed"))
        start.assert_not_called()
        self.assertEqual(len(UC.entries(self.jobs, ROUTE_ID)), 1)

    def test_another_session_is_refused_and_nothing_is_kept(self):
        from dispatch_contract import DispatchContractError
        (code, receipt), start = self.call(side_effect=DispatchContractError("work-parent-recovery-required", FIRST))
        self.assertEqual((code, receipt["reason"]), (69, "work-parent-recovery-required"))
        start.assert_not_called()
        self.assertEqual(UC.entries(self.jobs, ROUTE_ID), [])

    def test_a_route_that_did_not_end_pass_or_was_stopped_takes_nothing(self):
        for status, extra in (("done", {"failure_class": "blocked"}), ("cancelled", {}),
                              ("done", {"failure_class": "pass", "parent_close_requested": "1"})):
            self.rows = {FIRST: (status, owner(**extra))}
            (code, receipt), start = self.call()
            self.assertEqual((code, receipt["reason"]), (69, "owner-continue-not-passed"), status)
            start.assert_not_called()
        self.assertEqual(UC.entries(self.jobs, ROUTE_ID), [])

    def test_a_route_with_a_review_node_keeps_its_earlier_review_and_takes_nothing(self):
        review = {**self.route, "nodes": [*self.route["nodes"],
                                          {"id": "impl-review", "dispatch_depth": 2, "worker_type": "review"}]}
        (code, receipt), start = self.call(route=review)
        self.assertEqual((code, receipt["reason"]), (69, "owner-continue-route-shape"))
        start.assert_not_called()
        planned = {**self.route, "route_plan": {"index": 1}}
        self.assertEqual(self.call(route=planned)[0][1]["reason"], "owner-continue-route-shape")
        self.assertEqual(UC.entries(self.jobs, ROUTE_ID), [])


class NonPassReceiptTest(unittest.TestCase):
    def test_close_command_names_the_parent_close_of_this_route(self):
        import work_start
        command = work_start._close_command(Path("/r/rt-0123456789abcdef.json"))
        self.assertIn("capability-route.py close --route /r/rt-0123456789abcdef.json", command)


if __name__ == "__main__":
    unittest.main()
