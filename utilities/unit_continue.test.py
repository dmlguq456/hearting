#!/usr/bin/env python3
"""A unit route's next turn (`correct --route`): the kept text, the chain, when a turn may start, the session.

The three-harness run with a fake model is `harness_behavior_cases_unit_continue.py`; these cases pin the
decisions it cannot reach one by one (each branch of the start condition, the session reasons, the file a
transplant places, the home window).
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

ROUTE = {"route_id": "rt-abc", "route_hash": "h"}
NODE = "unit"
FIRST = "att-" + "1" * 32


def meta(attempt=FIRST, turn=None, **extra):
    value = {"attempt_id": attempt, "route_id": "rt-abc", "route_node": NODE, "harness": "claude",
             "worker_type": "review", "capability": "unit", "workflow_completion": "runtime-v1"}
    if turn:
        value["turn"] = str(turn)
    value.update(extra)
    return value


class TextStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.jobs = Path(self.dir.name) / "jobs.log"
        self.jobs.write_text("")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_same_text_twice_is_one_request(self):
        first = UC.submit(self.jobs, "rt-abc", "look again")
        again = UC.submit(self.jobs, "rt-abc", "look again")
        self.assertFalse(first["duplicate"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["entry"]["id"], again["entry"]["id"])
        self.assertEqual(len(UC.entries(self.jobs, "rt-abc")), 1)

    def test_one_id_never_carries_two_texts(self):
        UC.submit(self.jobs, "rt-abc", "one", "req-1")
        with self.assertRaisesRegex(UC.ContinueError, "correction-id-content-conflict"):
            UC.submit(self.jobs, "rt-abc", "two", "req-1")

    def test_empty_oversized_and_malformed_are_refused(self):
        for text, request_id, reason in (("  ", None, "empty"), ("x" * (UC.TEXT_BYTES + 1), None, "oversized"),
                                         ("ok", "bad id!", "correction-id-invalid")):
            with self.assertRaises(UC.ContinueError, msg=reason):
                UC.submit(self.jobs, "rt-abc", text, request_id)

    def test_routes_keep_separate_texts(self):
        UC.submit(self.jobs, "rt-abc", "a")
        self.assertEqual(UC.entries(self.jobs, "rt-other"), [])


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.jobs = Path(self.dir.name) / "jobs.log"
        self.jobs.write_text("")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_turn_id_depends_on_the_request(self):
        one, two = (UC.attempt_id_for("rt-abc", NODE, i) for i in ("a", "b"))
        self.assertNotEqual(one, two)
        self.assertEqual(one, UC.attempt_id_for("rt-abc", NODE, "a"))
        self.assertTrue(one.startswith("att-") and len(one) == 36)

    def test_next_turn_is_the_oldest_request_without_a_row(self):
        kept = UC.submit(self.jobs, "rt-abc", "first ask")["entry"]
        UC.submit(self.jobs, "rt-abc", "second ask")
        rows = {FIRST: ("done", meta())}
        turn = UC.next_turn(self.jobs, ROUTE, NODE, rows)
        self.assertEqual((turn.turn, turn.text, turn.prev_attempt_id), (2, "first ask", FIRST))
        rows[turn.attempt_id] = ("done", meta(turn.attempt_id, 2, continues_attempt_id=FIRST))
        later = UC.next_turn(self.jobs, ROUTE, NODE, rows)
        self.assertEqual((later.turn, later.text, later.prev_attempt_id), (3, "second ask", turn.attempt_id))
        self.assertEqual(kept["id"], turn.request_id)

    def test_nothing_to_start_without_a_first_turn_or_a_request(self):
        self.assertIsNone(UC.next_turn(self.jobs, ROUTE, NODE, {FIRST: ("done", meta())}))
        UC.submit(self.jobs, "rt-abc", "ask")
        self.assertIsNone(UC.next_turn(self.jobs, ROUTE, NODE, {}))

    def test_chain_orders_by_turn_and_skips_other_routes_and_owners(self):
        rows = {"b": ("done", meta("b", 2)), FIRST: ("done", meta()),
                "x": ("done", meta("x", route_id="rt-other")), "o": ("done", meta("o", worker_type="owner"))}
        self.assertEqual([a for a, _s, _m in UC.chain(rows, "rt-abc", NODE)], [FIRST, "b"])


class ReadyToContinueTest(unittest.TestCase):
    """The one judgment `_slot` and `_advance` share, branch by branch."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("")
        self.path = self.root / "rt-abc.json"
        self.path.write_text("{}")
        UC.submit(self.jobs, "rt-abc", "ask again")

    def tearDown(self):
        self.dir.cleanup()

    def ready(self, rows, *, closed=None, completion="complete"):
        with mock.patch("dispatch_notice_state.closed_outcome", return_value=closed), \
                mock.patch("dispatch_terminal_commit.owner_completion_state",
                           return_value=types.SimpleNamespace(state=completion)):
            return UC.ready_to_continue(self.jobs, ROUTE, self.path, NODE, rows)

    def set_aside(self, count=1):
        for index in range(count):
            (self.root / f"rt-abc.superseded-2026111{index}T000000Z.outcome.json").write_text("{}")

    def test_closed_and_settled_starts_the_turn(self):
        self.assertIsNotNone(self.ready({FIRST: ("done", meta())}, closed={"route_id": "rt-abc"}))

    def test_ended_but_not_closed_waits(self):
        self.assertIsNone(self.ready({FIRST: ("done", meta())}))

    def test_a_running_turn_waits_even_when_the_route_looks_closed(self):
        self.assertIsNone(self.ready({FIRST: ("running", meta())}, closed={"route_id": "rt-abc"}))

    def test_closed_with_completion_still_pending_waits(self):
        self.assertIsNone(self.ready({FIRST: ("done", meta())}, closed={"route_id": "rt-abc"}, completion="pending"))

    def test_an_unused_route_the_runtime_closed_is_not_reopened(self):
        self.assertIsNone(self.ready({FIRST: ("done", meta())}, closed={"autoclose": True}))

    def test_reopened_with_no_new_attempt_starts_the_turn(self):
        self.set_aside(1)
        self.assertIsNotNone(self.ready({FIRST: ("done", meta())}))

    def test_a_second_closure_set_aside_for_a_second_turn(self):
        self.set_aside(1)
        turn = UC.next_turn(self.jobs, ROUTE, NODE, {FIRST: ("done", meta())})
        rows = {FIRST: ("done", meta()), turn.attempt_id: ("done", meta(turn.attempt_id, 2))}
        UC.submit(self.jobs, "rt-abc", "a third ask")
        self.assertIsNone(self.ready(rows))               # one set aside, two turns: not reopened yet
        self.set_aside(2)
        self.assertIsNotNone(self.ready(rows))

    def test_once_the_turn_has_a_row_nothing_more_starts(self):
        self.set_aside(1)
        turn = UC.next_turn(self.jobs, ROUTE, NODE, {FIRST: ("done", meta())})
        rows = {FIRST: ("done", meta()), turn.attempt_id: ("running", meta(turn.attempt_id, 2))}
        self.assertIsNone(self.ready(rows))

    def test_without_a_kept_request_nothing_starts(self):
        (self.root / "x").mkdir()
        other = {"route_id": "rt-none", "route_hash": "h"}
        with mock.patch("dispatch_notice_state.closed_outcome", return_value={"route_id": "rt-none"}):
            self.assertIsNone(UC.ready_to_continue(self.jobs, other, self.path, NODE, {
                FIRST: ("done", meta(route_id="rt-none"))}))


class SessionPlanTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.home = self.root / "home"
        self.session = "5fa3ebc9-8aa3-4cf7-a1a4-f9a54721b128"

    def tearDown(self):
        self.dir.cleanup()

    def plan(self, previous, harness="claude", **kw):
        row = types.SimpleNamespace(metadata=previous)
        with mock.patch("dispatch_completion_join.exact_attempt_row", return_value=row):
            return UC.session_plan(self.root / "jobs.log", FIRST, harness, **kw)

    def claude_file(self):
        path = self.home / "projects" / "-work-tree" / f"{self.session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"sessionId": "x"}\n')
        return path

    def test_a_claude_turn_with_its_file_resumes(self):
        self.claude_file()
        plan = self.plan(meta(session_id=self.session, runtime_home=str(self.home)))
        self.assertEqual((plan.kind, plan.session_id, plan.row_value), ("resumed", self.session, "resumed"))
        self.assertEqual(plan.relative, f"projects/-work-tree/{self.session}.jsonl")

    def test_a_removed_home_is_session_missing(self):
        plan = self.plan(meta(session_id=self.session, runtime_home=str(self.home)))
        self.assertEqual((plan.kind, plan.row_value), ("fresh", "fresh:session-missing"))

    def test_another_harness_is_harness_changed(self):
        plan = self.plan(meta(session_id=self.session, runtime_home=str(self.home)), harness="codex")
        self.assertEqual(plan.row_value, "fresh:harness-changed")

    def test_a_codex_thread_is_read_from_the_log_and_found_by_its_rollout_file(self):
        thread = "019a-thread"
        rollout = self.home / "sessions" / "2026" / "11" / "19" / f"rollout-2026-11-19T01-02-03-{thread}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
        log = self.root / "log.jsonl"
        log.write_text(json.dumps({"type": "turn.completed", "thread_id": thread}) + "\n")
        plan = self.plan(meta(harness="codex", runtime_home=str(self.home), log_file=str(log)), harness="codex")
        self.assertEqual((plan.kind, plan.session_id), ("resumed", thread))

    def test_opencode_needs_the_session_to_exist_in_its_store(self):
        log = self.root / "log.jsonl"
        log.write_text('{"sessionID": "ses_abc"}\n')
        previous = meta(harness="opencode", log_file=str(log), runtime_home="shared-service")
        with mock.patch("opencode_runtime.is_v2", return_value=True):
            self.assertEqual(self.plan(previous, "opencode", exists=lambda s: True).row_value, "resumed")
            self.assertEqual(self.plan(previous, "opencode", exists=lambda s: False).row_value,
                             "fresh:session-missing")
        with mock.patch("opencode_runtime.is_v2", return_value=False):
            self.assertEqual(self.plan(previous, "opencode", exists=lambda s: True).row_value,
                             "fresh:store-unsupported")

    def test_an_unreadable_previous_row_is_session_missing(self):
        with mock.patch("dispatch_completion_join.exact_attempt_row", side_effect=ValueError("gone")):
            self.assertEqual(UC.session_plan(self.root / "jobs.log", FIRST, "claude").row_value,
                             "fresh:session-missing")


class TransplantTest(unittest.TestCase):
    def test_only_the_one_session_file_and_its_folder_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old", Path(tmp) / "new"
            session = "abc"
            file = old / "projects" / "-w" / f"{session}.jsonl"
            file.parent.mkdir(parents=True)
            file.write_text('{"a": 1}\n')
            (file.parent / "other.jsonl").write_text("{}\n")
            (file.parent / session).mkdir()
            (file.parent / session / "tool-results.txt").write_text("x")
            plan = UC.SessionPlan("resumed", "", session, str(file), f"projects/-w/{session}.jsonl")
            UC.transplant(plan, new)
            moved = sorted(str(p.relative_to(new)) for p in new.rglob("*") if p.is_file())
            self.assertEqual(moved, [f"projects/-w/{session}.jsonl", f"projects/-w/{session}/tool-results.txt"])
            self.assertTrue(file.exists())                      # the source stays where it was

    def test_a_fresh_plan_moves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            UC.transplant(UC.SessionPlan("fresh", "session-missing", "", ""), tmp)
            UC.transplant(None, tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TurnPromptTest(unittest.TestCase):
    args = types.SimpleNamespace(turn=2, continuation_text="please recheck", continues_attempt_id=FIRST)

    def test_a_woken_session_gets_this_turns_facts_and_text_without_the_brief(self):
        with mock.patch.object(UC, "previous_result", return_value="Verdict: changes requested"):
            text = UC.turn_prompt(self.args, UC.SessionPlan("resumed", "", "s", "f", "r"),
                                  "Dispatch metadata:\n- artifact_output_dir: /new\n", "THE BRIEF", "jobs")
        self.assertIn("artifact_output_dir: /new", text)
        self.assertIn("please recheck", text)
        self.assertIn("Verdict: changes requested", text)
        self.assertNotIn("THE BRIEF", text)

    def test_a_fresh_session_gets_the_brief_and_the_previous_result(self):
        with mock.patch.object(UC, "previous_result", return_value="Verdict: changes requested"):
            text = UC.turn_prompt(self.args, UC.SessionPlan("fresh", "session-missing", "", ""), "", "THE BRIEF", "jobs")
        self.assertIn("THE BRIEF", text)
        self.assertIn("session-missing", text)
        self.assertIn("Verdict: changes requested", text)


class KeepWindowTest(unittest.TestCase):
    DEFAULT = 3600

    def rows(self, *items):
        return {m["attempt_id"]: types.SimpleNamespace(meta=m, status=status) for status, m in items}

    def window(self, m, rows, state="unknown"):
        return UC.keep_window(m, rows, self.DEFAULT, state)

    def test_an_ordinary_home_keeps_the_ordinary_window(self):
        self.assertEqual(self.window(meta(capability="autopilot-code"), {}), self.DEFAULT)

    def test_the_last_turn_of_a_unit_route_waits_by_where_its_parent_is(self):
        m = meta(route_file="/nonexistent/rt.json")
        import route_autoclose
        self.assertEqual(self.window(m, {}, "live"), route_autoclose.IDLE_SECONDS)     # a live parent has an end too
        self.assertEqual(self.window(m, {}, "dead"), route_autoclose.QUIET_SECONDS)
        self.assertEqual(self.window(m, {}, "unknown"), route_autoclose.IDLE_SECONDS)

    def test_a_turn_a_later_turn_continued_goes_back_to_the_ordinary_window(self):
        m = meta(route_file="/nonexistent/rt.json")
        later = meta("att-2", 2, continues_attempt_id=FIRST)
        self.assertEqual(self.window(m, self.rows(("done", later)), "live"), self.DEFAULT)

    def test_a_route_the_runtime_closed_unused_goes_back_to_the_ordinary_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "rt-abc.json"
            route.with_name("rt-abc.outcome.json").write_text(json.dumps({"autoclose": True}))
            self.assertEqual(self.window(meta(route_file=str(route)), {}, "live"), self.DEFAULT)

    def test_a_leg_a_worker_started_waits_while_that_worker_runs(self):
        m = meta(route_file="/nonexistent/rt.json", caller_attempt_id="att-caller")
        caller = self.rows(("running", meta("att-caller")))
        import route_autoclose
        self.assertEqual(self.window(m, caller), route_autoclose.IDLE_SECONDS)
        ended = self.rows(("done", meta("att-caller")))
        self.assertEqual(self.window(m, ended), self.DEFAULT)


class AdapterSessionTest(unittest.TestCase):
    """A unit leg keeps its session in the worker home; every other one-shot worker keeps none."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "claude_dispatch_headless_unit_continue", HERE.parent / "adapters/claude/bin/dispatch-headless.py")
        cls.claude = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.claude)

    def test_claude_flags(self):
        args = types.SimpleNamespace
        self.assertEqual(self.claude._session_arguments(args()), ["--no-session-persistence"])
        self.assertEqual(self.claude._session_arguments(args(unit_session_id="s-1")), ["--session-id", "s-1"])
        woken = args(unit_session_id="s-1", session_plan=UC.SessionPlan("resumed", "", "s-1", "f", "r"))
        self.assertEqual(self.claude._session_arguments(woken), ["--resume", "s-1"])
        fresh = args(unit_session_id="s-2", session_plan=UC.SessionPlan("fresh", "session-missing", "", ""))
        self.assertEqual(self.claude._session_arguments(fresh), ["--session-id", "s-2"])

    def test_opencode_wakes_the_session_only_when_one_is_named(self):
        import opencode_runtime
        plain = opencode_runtime.run_argv(version=(2, 0, 32))
        woken = opencode_runtime.run_argv(session="ses_1", version=(2, 0, 32))
        self.assertNotIn("--session", plain)
        self.assertEqual(woken[woken.index("--session") + 1], "ses_1")
        self.assertEqual([x for x in woken if x not in ("--session", "ses_1")], plain)

    def test_the_woken_turn_names_its_harness_by_the_adapter_folder(self):
        import dispatch_headless_main as DHM
        import worker_bootstrap
        seen = []

        class Stop(Exception):
            pass

        def plan_for_args(args, jobs, harness):
            seen.append(harness)
            raise Stop

        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "rt.json"
            route.write_text("{}")
            args = types.SimpleNamespace(capability="unit", dispatch_depth=1, route_file=str(route), route_node=NODE,
                                         continues_attempt_id=FIRST, jobs_path="jobs.log")
            for harness in ("claude", "codex", "opencode"):
                adapter = types.SimpleNamespace(__file__=str(HERE.parent / "adapters" / harness / "bin" / "dispatch-headless.py"))
                with mock.patch.object(worker_bootstrap, "resolve_node_scope", return_value={}), \
                        mock.patch.object(worker_bootstrap, "_child_scope_environment", return_value={}), \
                        mock.patch.object(UC, "plan_for_args", side_effect=plan_for_args):
                    with self.assertRaises(Stop):
                        DHM.unit_leg_prompt(adapter, args, "brief")
        self.assertEqual(seen, ["claude", "codex", "opencode"])

    def test_the_row_carries_the_chain_only_for_a_unit_leg(self):
        import dispatch_contract_rows as ROWS
        plan = UC.SessionPlan("fresh", "session-missing", "", "")
        args = types.SimpleNamespace(unit_session_id="s-2", continues_attempt_id=FIRST, turn=2, session_plan=plan)
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=True):
            self.assertEqual(ROWS.unit_continue_receipt(args),
                             f",session_id=s-2,continues_attempt_id={FIRST},turn=2,session=fresh:session-missing")
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=False):
            self.assertEqual(ROWS.unit_continue_receipt(args), "")


class CorrectUnitRouteTest(unittest.TestCase):
    """`correct --route <unit route>`: what is refused, what is only kept, what starts."""

    def setUp(self):
        import work_start
        self.W = work_start
        self.dir = tempfile.TemporaryDirectory()
        self.jobs = Path(self.dir.name) / "jobs.log"
        self.jobs.write_text("")
        self.route = {**ROUTE, "capability": "unit", "artifact_root": self.dir.name,
                      "nodes": [{"id": NODE, "dispatch_depth": 1}]}
        self.patches = [mock.patch.object(work_start, "unit_leg", return_value={"id": NODE}),
                        mock.patch.object(work_start, "_rows", side_effect=lambda jobs: self.rows)]
        for patch in self.patches:
            patch.start()
        self.rows = {FIRST: ("done", meta(failure_class="pass"))}

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.dir.cleanup()

    def call(self, text, **kw):
        with mock.patch.object(self.W, "start_work", return_value={"state": "running", "next_step": "n"}) as start, \
                mock.patch.object(self.W, "require_route_parent", **kw):
            return self.W.correct_unit_route(self.route, self.dir.name + "/rt.json", self.jobs, text), start

    def test_no_text_shows_the_turns_and_changes_nothing(self):
        (code, receipt), start = self.call(None)
        self.assertEqual((code, receipt["state"], len(receipt["turns"])), (0, "unit-turns", 1))
        start.assert_not_called()
        self.assertEqual(UC.entries(self.jobs, "rt-abc"), [])

    def test_a_caller_that_is_not_the_parent_is_refused_and_keeps_nothing(self):
        from dispatch_contract import DispatchContractError
        (code, receipt), start = self.call("ask", side_effect=DispatchContractError("work-parent-recovery-required", "x"))
        self.assertEqual((code, receipt["state"]), (69, "not-admitted"))
        start.assert_not_called()
        self.assertEqual(UC.entries(self.jobs, "rt-abc"), [])

    def test_a_leg_of_an_approved_plan_is_refused(self):
        self.route["route_plan"] = {"decision": "d", "index": 0, "digest": "x"}
        (code, receipt), start = self.call("ask")
        self.assertEqual((code, receipt["reason"]), (69, "unit-continue-request-bound-route"))
        start.assert_not_called()
        self.assertEqual(UC.entries(self.jobs, "rt-abc"), [])

    def test_a_record_free_route_is_refused(self):
        with mock.patch("unit_ephemeral.is_scratch_root", return_value=True):
            (code, receipt), _ = self.call("ask")
        self.assertEqual((code, receipt["reason"]), (69, "correct-unit-ephemeral-unsupported"))

    def test_text_sent_while_a_turn_runs_is_kept_and_starts_nothing(self):
        self.rows = {FIRST: ("running", meta())}
        (code, receipt), start = self.call("ask")
        self.assertEqual((code, receipt["retained"], receipt["duplicate"]), (0, True, False))
        start.assert_called_once()
        self.assertEqual(len(UC.entries(self.jobs, "rt-abc")), 1)

    def test_a_text_that_already_ran_as_a_turn_is_not_answered_with_that_turns_result(self):
        def start(route, path, jobs, **kw):
            self.rows[UC.attempt_id_for("rt-abc", NODE, UC.entries(jobs, "rt-abc")[0]["id"])] = (
                "done", meta("att-2", 2, continues_attempt_id=FIRST, session="resumed", failure_class="pass"))
            return {"state": "running"}
        with mock.patch.object(self.W, "start_work", side_effect=start), \
                mock.patch.object(self.W, "require_route_parent"):
            self.W.correct_unit_route(self.route, self.dir.name + "/rt.json", self.jobs, "fixed, look again")
        with mock.patch.object(self.W, "start_work", return_value={"state": "completed"}) as again, \
                mock.patch.object(self.W, "require_route_parent"):
            code, receipt = self.W.correct_unit_route(self.route, self.dir.name + "/rt.json", self.jobs,
                                                      "fixed, look again")
        again.assert_not_called()
        self.assertEqual((code, receipt["state"], receipt["duplicate"], receipt["retained"]), (0, "already-asked", True, False))
        self.assertNotEqual(receipt["state"], "completed")
        self.assertEqual((receipt["turn"], receipt["unit_attempt_id"]),
                         (2, UC.attempt_id_for("rt-abc", NODE, receipt["request_id"])))
        from start_receipt import next_line
        line = next_line(receipt)
        self.assertIn("already ran as that turn", line)
        self.assertIn("send a different text or pass a new --request-id", line)
        self.assertNotIn("route is complete", line)

    def test_a_turn_that_ended_without_a_verdict_keeps_no_text_and_says_to_compose_again(self):
        self.rows = {FIRST: ("done", meta(failure_class="blocked"))}
        with mock.patch.object(self.W, "_compose_again", return_value="compose --unit x"):
            (code, receipt), start = self.call("ask")
        start.assert_not_called()
        self.assertEqual((code, receipt["state"], receipt["reason"]), (69, "not-admitted", "unit-continue-last-turn-no-verdict"))
        self.assertEqual((receipt["required_action"], receipt["parent_next_command"]), ("compose-again", "compose --unit x"))
        self.assertEqual(UC.entries(self.jobs, "rt-abc"), [])
        self.assertNotIn("retained", receipt)

    def test_the_turn_that_started_is_named_in_the_receipt(self):
        def start(route, path, jobs, **kw):
            self.rows[UC.attempt_id_for("rt-abc", NODE, UC.entries(jobs, "rt-abc")[0]["id"])] = (
                "running", meta("att-2", 2, continues_attempt_id=FIRST, session="resumed"))
            return {"state": "running"}
        with mock.patch.object(self.W, "start_work", side_effect=start), \
                mock.patch.object(self.W, "require_route_parent"):
            code, receipt = self.W.correct_unit_route(self.route, self.dir.name + "/rt.json", self.jobs, "ask")
        self.assertEqual((code, receipt["retained"], receipt["turn"], receipt["session"]), (0, False, 2, "resumed"))
        self.assertEqual(receipt["continues_attempt_id"], FIRST)


class RetentionPlumbingTest(unittest.TestCase):
    """`worker_home_retention` asks `keep_window` for a unit leg's row and keeps its one-hour window for every other."""

    def test_only_a_unit_row_gets_another_window(self):
        import worker_home_retention as RET
        fields = lambda m: ["2026-11-19T00:00:00Z", "done", "/r", "/w", "slug", ",".join(f"{k}={v}" for k, v in m.items())]
        snapshot = types.SimpleNamespace(rows={})
        plain = RET._Row("raw", fields({"attempt_id": "att-p", "capability": "autopilot-code"}))
        self.assertEqual(RET._window_seconds(snapshot, plain), RET.IDLE_SECONDS)
        self.assertEqual(RET._window_seconds(snapshot, None), RET.IDLE_SECONDS)
        unit = RET._Row("raw", fields({"attempt_id": "att-u", "capability": "unit", "workflow_completion": "runtime-v1",
                                       "route_file": "/nonexistent/rt.json"}))
        import route_autoclose
        with mock.patch.object(RET, "_main_runtime_state", return_value="live"):
            self.assertEqual(RET._window_seconds(snapshot, unit), route_autoclose.IDLE_SECONDS)
        with mock.patch.object(RET, "_main_runtime_state", return_value="unknown"):
            self.assertEqual(RET._window_seconds(snapshot, unit), route_autoclose.IDLE_SECONDS)


class WorkStartSlotTest(unittest.TestCase):
    """`_slot` hands out the next turn's id only when the one judgment says so; the summary is the last turn's."""

    def setUp(self):
        import work_start
        self.W = work_start
        self.route = {**ROUTE, "capability": "unit", "nodes": [{"id": NODE}]}
        self.rows = {FIRST: ("done", meta(route_hash="h"))}
        self.patch = mock.patch.object(work_start, "unit_leg", return_value={"id": NODE})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_a_ready_turn_gets_its_own_attempt_id(self):
        turn = UC.Turn("att-next", FIRST, 2, "ask", "req")
        with mock.patch.object(UC, "ready_to_continue", return_value=turn):
            self.assertEqual(self.W._slot(self.route, NODE, self.rows, Path("/j"), Path("/p")), "att-next")

    def test_without_a_ready_turn_the_slot_is_the_last_row(self):
        with mock.patch.object(UC, "ready_to_continue", return_value=None):
            self.assertEqual(self.W._slot(self.route, NODE, self.rows, Path("/j"), Path("/p")), FIRST)

    def test_a_caller_without_the_route_path_never_continues(self):
        with mock.patch.object(UC, "ready_to_continue", side_effect=AssertionError("asked")):
            self.assertEqual(self.W._slot(self.route, NODE, self.rows, Path("/j")), FIRST)

    def test_the_closed_summary_and_fields_are_the_last_turns(self):
        second = "att-2"
        rows = {FIRST: ("done", meta()),
                second: ("done", meta(second, 2, continues_attempt_id=FIRST, session="resumed"))}
        asked = []
        with mock.patch.object(self.W, "_rows", return_value=rows), \
                mock.patch.object(self.W, "_unit_summary", side_effect=lambda jobs, aid, **k: asked.append(aid) or {"summary": "s"}), \
                mock.patch("attempt_view.session_tokens", return_value=(7, True)):
            got = self.W._closed_unit_summary(self.route, Path("/j"))
        self.assertEqual(asked, [second])
        self.assertEqual((got["turn"], got["session"], got["turn_tokens"], got["turn_tokens_complete"]),
                         (2, "resumed", 7, True))
        self.assertEqual(got["continues_attempt_id"], FIRST)


class NoticeLinesTest(unittest.TestCase):
    """The end notice names the turn of a continued unit leg and the start a kept request waits for."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.jobs = Path(self.dir.name) / "jobs.log"
        self.jobs.write_text("")

    def tearDown(self):
        self.dir.cleanup()

    def test_nothing_is_added_for_a_first_turn_with_nothing_kept(self):
        self.assertEqual(UC.notice_lines(str(self.jobs), meta(route_file="/x/rt.json")), ("", ""))

    def test_a_continued_turn_names_itself(self):
        head, note = UC.notice_lines(str(self.jobs), meta("att-2", 2, continues_attempt_id=FIRST,
                                                          session="fresh:session-missing", route_file="/x/rt.json"))
        self.assertEqual(head, "Turn 2 of this unit route (session: fresh:session-missing).")
        self.assertEqual(note, "")

    def test_a_kept_request_without_a_turn_names_the_start(self):
        UC.submit(self.jobs, "rt-abc", "ask")
        _head, note = UC.notice_lines(str(self.jobs), meta(route_file="/x/rt.json"))
        self.assertIn("capability-route.py start --route /x/rt.json", note)
        UC.submit(self.jobs, "rt-abc", "second")
        with mock.patch("dispatch_completion_join.exact_attempt_row"):     # every kept request has its turn
            self.assertEqual(UC.notice_lines(str(self.jobs), meta(route_file="/x/rt.json"))[1], "")

    def test_a_continued_turn_that_ended_as_an_error_carries_the_recorded_reason(self):
        import dispatch_completion_join as JOIN
        refused = meta("att-2", 2, continues_attempt_id=FIRST, reconcile_reason="app-server-request-failed:thread/resume")
        self.assertEqual(JOIN.unit_leg_reason_line(refused), "Reason: app-server-request-failed:thread/resume")
        self.assertEqual(JOIN.unit_leg_reason_line(meta(reconcile_reason="app-server-eof")), "")      # a first turn
        self.assertEqual(JOIN.unit_leg_reason_line(meta("att-2", 2, continues_attempt_id=FIRST)), "")  # none recorded

    def test_other_capabilities_get_nothing(self):
        self.assertEqual(UC.notice_lines(str(self.jobs), meta(capability="autopilot-code", turn=2,
                                                              continues_attempt_id=FIRST)), ("", ""))


if __name__ == "__main__":
    unittest.main()
