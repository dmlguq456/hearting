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
# The registry columns a registered headless owner row carries (as `dispatch_contract.test.py`'s rows do).
REGISTERED = ("attempt_schema_version=2,dispatch_depth=1,transport=headless,execution_surface=registered-headless,"
              "registered_worker=1,fallback_hop=same-harness-headless")


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

    def test_after_the_runtime_ended_an_attempt_without_a_result_the_next_one_starts_fresh(self):
        # (가) Killed, a supervisor error (a failed wake among them) or a silent end: the session is not woken
        # again, whether that attempt had woken it or not. Every end of its own keeps waking it.
        self.transcript()
        home = {"log_file": str(self.log), "runtime_home": str(self.home)}
        died = [{"note": "dead-runtime-exit", "failure_class": "runtime", "terminal_event": "dispatch.supervisor.error",
                 "session": "resumed"},
                {"note": "dead-runtime-exit", "failure_class": "runtime"},
                {"note": "dead-runtime-error"}, {"note": "dead-exact-pid"}, {"note": "dead-worker-silent-exit"}]
        for extra in died:
            plan = self.plan(owner(**home, **extra), launch(automatic_retry_of=FIRST))
            self.assertEqual((plan.row_value, plan.source), ("fresh:resume-refused", ""), extra)
        ended = [{"note": "dead-worker-blocked", "failure_class": "blocked"}, {"note": "dead-capacity"},
                 {"failure_class": "capacity", "terminal_event": "dispatch.supervisor.error"},
                 {"note": "completed-marker", "failure_class": "pass"},
                 {"note": "dead-worker-fail", "failure_class": "fail"}]
        for extra in ended:
            self.assertEqual(self.plan(owner(**home, **extra), launch(automatic_retry_of=FIRST)).row_value,
                             "resumed", extra)

    def test_a_unit_turn_after_a_usage_limit_still_wakes_its_session(self):
        # #1824: the unit route's capacity resume goes through the same decision and keeps its session.
        self.transcript()
        unit = {"attempt_id": FIRST, "harness": "claude", "capability": "unit", "route_node": "unit",
                "worker_type": "support", "session_id": SESSION, "runtime_home": str(self.home),
                "note": "dead-capacity", "failure_class": "capacity"}
        args = launch(worker_type="support", continues_attempt_id=FIRST, automatic_retry_of=FIRST)
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=types.SimpleNamespace(metadata=unit)):
            self.assertEqual(UC.plan_for_args(args, self.root / "jobs.log", "claude").row_value, "resumed")
        died = {**unit, "note": "dead-runtime-exit", "failure_class": "runtime"}
        args = launch(worker_type="support", continues_attempt_id=FIRST, automatic_retry_of=FIRST)
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=types.SimpleNamespace(metadata=died)):
            self.assertEqual(UC.plan_for_args(args, self.root / "jobs.log", "claude").row_value, "fresh:resume-refused")

    def test_a_failed_observed_replacement_is_replaced_again_in_a_fresh_session(self):
        # The once-per-lineage `observed` rule does not hold a runtime end back: B (created as observed) died
        # with no result, an explicit start replaces it, and C starts fresh instead of waking B's session again.
        import dispatch_replacement as DR
        import route_authority as RA
        self.transcript()
        b = owner("att-" + "b" * 32, log_file=str(self.log), runtime_home=str(self.home), automatic_retry_of=FIRST,
                  replacement_original_attempt_id=FIRST, replacement_family_id="f" * 64, launch_started="1",
                  note="dead-runtime-exit", failure_class="runtime", terminal_event="dispatch.supervisor.error",
                  session="resumed")
        jobs = self.root / "jobs.log"
        jobs.write_text("")
        with mock.patch.object(DR, "created_as", return_value=DR.OBSERVED), \
                mock.patch.object(DR, "_retained_corrections", return_value=[]), \
                mock.patch("route_parent_close.row_requested", return_value=False):
            self.assertEqual(RA._replacement_kind("done", b, jobs=jobs), "runtime")
        choice = RA.decide_continuation(action="replacement-progress", status="done", metadata=b,
                                        facts={"kind": "runtime", "resume_capacity": True, "registry_rows": {}})
        self.assertEqual((choice.allowed, choice.state), (True, "launch"))
        plan = self.plan(b, launch(automatic_retry_of=b["attempt_id"]))
        self.assertEqual(plan.row_value, "fresh:resume-refused")

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


    def test_a_woken_owner_is_told_this_launchs_commit_policy_and_declared_scope(self):
        import dispatch_headless_main as DHM
        args = launch(automatic_retry_of=FIRST, session_plan=self.RESUMED, jobs_path="jobs.log")
        with mock.patch.object(DHM, "dispatch_metadata_prompt", return_value="Dispatch metadata:\n- x\n"), \
                mock.patch("commit_policy.prompt_clause", return_value="Git operation in progress (merge/resolve state):\n"), \
                mock.patch("worker_bootstrap.declared_scope_prompt", return_value="This node's declared scope: outputs=[]\n"):
            woken = DHM.owner_continuation_prompt(None, args, "FULL", self.RECOVERY)
            plain = DHM.launch_policy_prompt(args)
        self.assertNotIn("FULL", woken)
        self.assertIn("Git operation in progress", woken)
        self.assertIn("This node's declared scope: outputs=[]", woken)
        self.assertLess(woken.index("declared scope"), woken.index("Verified recovery context"))
        self.assertTrue(plain.startswith("Git operation in progress"))
        with mock.patch("commit_policy.prompt_clause", return_value=""), \
                mock.patch("worker_bootstrap.declared_scope_prompt", return_value=""):
            self.assertIn("commit as the owner contract says", DHM.launch_policy_prompt(args))
        fresh = launch(automatic_retry_of=FIRST, session_plan=self.FRESH, jobs_path="jobs.log")
        with mock.patch.object(UC, "taken_over_turn", return_value=(1, "")), \
                mock.patch.object(DHM, "dispatch_metadata_prompt", return_value="META"), \
                mock.patch.object(DHM, "launch_policy_prompt", side_effect=AssertionError("fresh reads the full prompt")):
            self.assertTrue(DHM.owner_continuation_prompt(None, fresh, "FULL", self.RECOVERY).startswith("FULL"))


class CarrySessionTest(unittest.TestCase):
    """The session file is carried after the claim; when it is gone by then, the launch starts fresh instead."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.source = self.root / "old" / "projects" / "-w" / f"{SESSION}.jsonl"
        self.source.parent.mkdir(parents=True)
        self.source.write_text('{"sessionId": "x"}\n')
        self.home = self.root / "new"
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("WOKEN PROMPT")
        self.annotated = []
        self.h = types.SimpleNamespace(
            annotate_attempt_row=lambda jobs, aid, values: self.annotated.append((aid, values)) or True,
            frame_repository=types.SimpleNamespace(execution_path=lambda path: path))

    def tearDown(self):
        self.dir.cleanup()

    def carry(self, args):
        import dispatch_headless_main as DHM
        rendered = []

        def fresh_prompt():
            rendered.append(args.session_plan.row_value)       # read after the plan changed
            return "FULL PROMPT"
        DHM.carry_session(self.h, args, self.home, jobs=self.root / "jobs.log", prompt_path=self.prompt,
                          fresh_prompt=fresh_prompt)
        return rendered

    def args(self):
        plan = UC.SessionPlan("resumed", "", SESSION, str(self.source), f"projects/-w/{SESSION}.jsonl")
        return launch(automatic_retry_of=FIRST, session_plan=plan, attempt_id="att-" + "9" * 32)

    def test_a_present_session_file_is_carried_and_the_prompt_stays(self):
        args = self.args()
        self.assertEqual(self.carry(args), [])
        self.assertTrue((self.home / "projects" / "-w" / f"{SESSION}.jsonl").is_file())
        self.assertEqual((args.session_plan.kind, self.prompt.read_text(), self.annotated), ("resumed", "WOKEN PROMPT", []))

    def test_a_session_file_gone_after_the_claim_starts_this_launch_fresh(self):
        args = self.args()
        self.source.unlink()
        self.assertEqual(self.carry(args), ["fresh:session-missing"])
        self.assertEqual(args.session_plan.row_value, "fresh:session-missing")
        self.assertEqual(self.prompt.read_text(), "FULL PROMPT")
        self.assertEqual(self.annotated, [(args.attempt_id, {"session": "fresh:session-missing"})])
        import dispatch_headless_main as DHM
        self.assertEqual(DHM.owner_session_arguments(args), [])       # no --resume-session for the supervisor

    def test_a_partial_copy_is_removed_so_a_fresh_session_can_take_its_id(self):
        args = self.args()
        destination = self.home / "projects" / "-w" / f"{SESSION}.jsonl"

        def partial(source, target, *a, **kw):
            Path(target).write_text("{")
            raise OSError("disk full")
        with mock.patch("shutil.copy2", side_effect=partial):
            self.carry(args)
        self.assertFalse(destination.exists())
        self.assertEqual(args.session_plan.row_value, "fresh:session-missing")

    def test_the_rows_session_can_be_corrected_after_the_claim(self):
        import dispatch_contract as D
        jobs = self.root / "jobs.log"
        aid = "att-" + "8" * 32
        row = f"2026-11-26T00:00:00Z\topen\t/r\t/w\tquick\t{REGISTERED},attempt_id={aid},session=resumed"
        self.assertTrue(D.claim_attempt_row(jobs, aid, row))
        self.assertTrue(D.annotate_attempt_row(jobs, aid, {"session": "fresh:session-missing"}))
        self.assertIn("session=fresh:session-missing", jobs.read_text())
        self.assertNotIn("session=resumed", jobs.read_text())


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


    def test_after_a_blocked_turn_its_observed_replacement_passed_the_later_turn_is_claimed(self):
        # (나) A BLOCKED, B (observed) PASS closed the route; the later request's turn follows B as turn 2 and
        # its claim counts A and B as one turn, so it is admitted under the one-shot limit (1 + turn - 1).
        import dispatch_contract as D
        replaced = "att-" + "2" * 32
        rows = {FIRST: ("done", owner(note="dead-worker-blocked", failure_class="blocked")),
                replaced: ("done", owner(replaced, automatic_retry_of=FIRST, replacement_original_attempt_id=FIRST,
                                         note="completed-marker", failure_class="pass"))}
        UC.submit(self.jobs, ROUTE_ID, "also fix the README")
        turn = UC.next_turn(self.jobs, ROUTE, UC.OWNER, rows)
        self.assertEqual((turn.prev_attempt_id, turn.turn), (replaced, 2))
        base = (f"{REGISTERED},worker_type=owner,route_id={ROUTE_ID},route_node=one-shot")

        def line(aid, status, meta):
            pipe = "".join(f",{key}={value}" for key, value in meta.items()
                           if key in {"automatic_retry_of", "replacement_original_attempt_id", "note"})
            return f"2026-11-26T00:00:00Z\t{status}\t/r\t/w\tquick\t{base},attempt_id={aid}{pipe},launch_claimed=1"
        registry = self.root / "registry.log"
        registry.write_text("".join(line(aid, status, meta) + "\n" for aid, (status, meta) in rows.items()))
        claim = (f"2026-11-26T00:00:01Z\topen\t/r\t/w\tquick\t{base},attempt_id={turn.attempt_id},"
                 f"continues_attempt_id={turn.prev_attempt_id},turn={turn.turn}")
        self.assertTrue(D.claim_attempt_row(registry, turn.attempt_id, claim, launch=True,
                                            exclusive_live_metadata={"route_id": ROUTE_ID, "route_node": "one-shot"},
                                            terminal_attempt_limit=1 + turn.turn - 1))


class RefusedLaterTurnTest(unittest.TestCase):
    """(다) A later turn whose launch is not admitted leaves the earlier PASS closure where it was."""

    def setUp(self):
        import work_start
        self.W = work_start
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.path = self.root / f"{ROUTE_ID}.json"
        self.path.write_text("{}")
        self.closure = self.root / f"{ROUTE_ID}.outcome.json"
        self.closure.write_text(json.dumps({"route_id": ROUTE_ID, "terminal_gate_proven": True}))
        self.rows = {FIRST: ("done", owner(failure_class="pass"))}
        self.turn = UC.Turn("att-" + "4" * 32, FIRST, 2, "also fix the README", "r1")

    def tearDown(self):
        self.dir.cleanup()

    def advance_raw(self):
        route = {**ROUTE, "artifact_root": self.dir.name}
        with mock.patch.object(self.W, "continuation_work_request", return_value={}), \
                mock.patch.object(self.W, "resume_command", return_value="resume"), \
                mock.patch("inline_finish.pending_state", return_value=None), \
                mock.patch.object(self.W, "unit_leg", return_value=None), \
                mock.patch.object(self.W, "follow_up_owner_route", return_value=True), \
                mock.patch.object(self.W, "_rows", side_effect=lambda jobs: self.rows), \
                mock.patch.object(self.W, "require_route_parent"), \
                mock.patch.object(UC, "ready_to_continue", return_value=self.turn):
            return self.W._advance(route, self.path, self.jobs, {})

    def advance(self, receipt, *, registers=False):
        def open_route(*_a, **_kw):
            self.assertFalse(self.closure.exists())        # the launch runs on the reopened route
            if registers:
                self.rows[self.turn.attempt_id] = ("running", owner(self.turn.attempt_id, continues_attempt_id=FIRST))
            return dict(receipt)
        with mock.patch.object(self.W, "_advance_open", side_effect=open_route):
            return self.advance_raw()

    def set_aside(self):
        return sorted(self.root.glob(f"{ROUTE_ID}.superseded-*.outcome.json"))

    def test_a_refused_claim_puts_the_closure_back(self):
        result = self.advance({"state": "needs-attention", "reason": "owner-launch-not-admitted"})
        self.assertEqual(result["reason"], "owner-launch-not-admitted")
        self.assertEqual(json.loads(self.closure.read_text())["terminal_gate_proven"], True)
        self.assertEqual(self.set_aside(), [])

    def test_a_started_turn_keeps_the_route_reopened(self):
        self.advance({"state": "running"}, registers=True)
        self.assertFalse(self.closure.exists())
        self.assertEqual(len(self.set_aside()), 1)

    def test_a_usage_limit_wait_keeps_the_route_reopened_for_the_start_it_resumes(self):
        self.advance({"state": "waiting-capacity", "reason": "launch-capacity-wait"})
        self.assertFalse(self.closure.exists())
        self.assertEqual(len(self.set_aside()), 1)

    def test_a_launch_that_raises_puts_the_closure_back_too(self):
        with self.assertRaises(ValueError):
            with mock.patch.object(self.W, "_advance_open", side_effect=ValueError("launcher-broke")):
                self.advance_raw()
        self.assertTrue(self.closure.is_file())
        self.assertEqual(self.set_aside(), [])

    def test_a_newer_closure_is_never_overwritten(self):
        retired = UC.reopen(self.path)
        self.closure.write_text('{"newer": true}')
        self.assertFalse(UC.restore_closure(self.path, retired))
        self.assertEqual(json.loads(self.closure.read_text()), {"newer": True})
        self.assertTrue(retired.is_file())


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

    def test_an_unreadable_seat_lineage_waits_for_the_same_command(self):
        from dispatch_contract import DispatchContractError
        error = DispatchContractError("handover-unreadable", "attempt x: lock busy")
        error.retry_when = "The seat lineage can be read again: nothing was changed, so run the same command then."
        (code, receipt), start = self.call(side_effect=error)
        self.assertEqual((code, receipt["reason"], receipt["retry_when"]), (69, "handover-unreadable", error.retry_when))
        self.assertNotIn("Only the session", receipt["next_step"])
        start.assert_not_called()

    def test_a_route_not_closed_on_a_pass_keeps_the_owner_input_path_and_keeps_nothing_here(self):
        # A BLOCKED owner's answer keeps its own continuation; a stopped route takes no later request.
        cases = [("done", {"failure_class": "blocked"}, self.closed), ("cancelled", {}, self.closed),
                 ("done", {"failure_class": "pass", "parent_close_requested": "1"}, self.closed),
                 ("done", {"failure_class": "pass"}, {**self.closed, "terminal_gate_proven": False}),
                 ("done", {"failure_class": "pass"}, {**self.closed, "autoclose": True})]
        for status, extra, closed in cases:
            self.rows, self.closed = {FIRST: (status, owner(**extra))}, closed
            result, start = self.call()
            self.assertIsNone(result, (status, extra, closed))
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
    def test_close_command_names_the_parent_close_of_this_route_and_its_registry(self):
        import work_start
        command = work_start._close_command(Path("/r/rt-0123456789abcdef.json"), Path("/state/jobs.log"))
        self.assertTrue(command.startswith("env AGENT_DISPATCH_JOBS=/state/jobs.log "))
        self.assertIn("capability-route.py close --route /r/rt-0123456789abcdef.json", command)
        handoff = work_start._close_command(Path("/r/rt.json"), Path("/state/jobs.log"), "--summary",
                                            work_start.PIN_HANDOFF_SUMMARY)
        self.assertTrue(handoff.endswith("close --route /r/rt.json --summary owner-pin-handoff"))


if __name__ == "__main__":
    unittest.main()
