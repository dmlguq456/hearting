#!/usr/bin/env python3
"""A worker that starts a unit route, and the record-free (`--ephemeral`) form (design PR 2c).

Real compose and route files, real registry rows; the three-harness run with a fake model is
`harness_behavior_cases_unit_nested.py`. Each end is tested in both directions: a review that finished
with blocking findings is finished work, and a run that did no work (empty answer) is not.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


U = _load("unit_route_launch_for_nested", "unit_route_launch.test.py")
F, R, T, L, W = U.F, U.R, U.T, U.L, U.W
import dispatch_contract as DC  # noqa: E402
import dispatch_contract_rows as ROWS  # noqa: E402
import codex_dispatch_terminal as TERMINAL  # noqa: E402
import route_parent_close as CLOSE  # noqa: E402
import unit_ephemeral  # noqa: E402
import worker_bootstrap  # noqa: E402

CALLER, CALLED, DEEPER = "att-" + "a" * 32, "att-" + "b" * 32, "att-" + "c" * 32


class CallerReceiptTest(unittest.TestCase):
    """The row of a leg a worker started names that worker; a leg the depth-0 session started names none."""

    def args(self, **extra):
        values = dict(attempt_id=CALLED, capability="unit", dispatch_depth=1, route_file="/x/route.json", route_node="unit")
        values.update(extra)
        return type("Args", (), values)()

    def test_a_unit_leg_started_inside_a_worker_names_it(self):
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=True), \
                mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": CALLER}):
            self.assertEqual(ROWS.caller_attempt_receipt(self.args()), f",caller_attempt_id={CALLER}")

    def test_no_calling_attempt_no_field(self):
        env = {k: v for k, v in os.environ.items() if k != "AGENT_DISPATCH_ATTEMPT_ID"}
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=True), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ROWS.caller_attempt_receipt(self.args()), "")

    def test_only_a_unit_leg_and_never_itself_and_nothing_malformed(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": CALLER}):
            with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=False):
                self.assertEqual(ROWS.caller_attempt_receipt(self.args()), "")       # a stage, a frame, an owner
            with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=True):
                self.assertEqual(ROWS.caller_attempt_receipt(self.args(attempt_id=CALLER)), "")
        with mock.patch.object(ROWS, "is_unit_leg_launch", return_value=True), \
                mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": "att-x,evil=1"}):
            self.assertEqual(ROWS.caller_attempt_receipt(self.args()), "")           # never a way to add a registry key


def _row(attempt, status, caller=None, worktree="/w", repo="/r", **extra):
    meta = {"attempt_id": attempt, "attempt_schema_version": "2", **({"caller_attempt_id": caller} if caller else {}), **extra}
    return ["t", status, repo, worktree, attempt + "-slug", ""], meta


class CallerEndStopsWhatItStartedTest(unittest.TestCase):
    """A worker that ends leaves nothing running that it started (its called worker's result would reach no one)."""

    def test_the_called_chain_is_below_its_caller_wherever_its_route_reads(self):
        rows = {CALLER: _row(CALLER, "open"),
                CALLED: _row(CALLED, "open", caller=CALLER, worktree="/elsewhere", repo="/another"),
                DEEPER: _row(DEEPER, "open", caller=CALLED, worktree="/third")}
        self.assertEqual(CLOSE._owned_attempts(None, rows, CALLER), {CALLER, CALLED, DEEPER})
        self.assertEqual(CLOSE._owned_attempts(None, rows, DEEPER), {DEEPER})

    def stop(self, rows):
        planned = lambda jobs, current, aid: ([(a, rows[a][0], rows[a][1], None) for a in sorted(CLOSE._owned_attempts(None, current, aid))
                                               if rows[a][0][1] in CLOSE.OPEN], [], [])  # noqa: E731
        seen = {}

        def execute(jobs, work, settle, grace, kill_wait, evidence=None):
            seen.update(work=[item[0] for item in work], evidence=evidence)
            return [item[0] for item in work], []
        with mock.patch.object(CLOSE, "_rows", return_value=rows), mock.patch.object(CLOSE, "jobs_path", return_value=Path("/j")), \
                mock.patch.object(CLOSE, "_plan", side_effect=planned), mock.patch.object(CLOSE, "_execute", side_effect=execute):
            return CLOSE.stop_called_workers("/j", CALLER), seen

    def test_the_still_running_called_workers_are_stopped_with_the_caller_ended_reason(self):
        stopped, seen = self.stop({CALLER: _row(CALLER, "done"), CALLED: _row(CALLED, "open", caller=CALLER),
                                   DEEPER: _row(DEEPER, "open", caller=CALLED)})
        self.assertEqual(sorted(stopped), sorted([CALLED, DEEPER]))
        self.assertEqual(seen["evidence"]["reconcile_reason"], "caller-ended")
        self.assertEqual(seen["evidence"]["failure_class"], "cancelled")      # the typed cancellation a stop makes

    def test_a_called_worker_that_already_ended_stands_and_another_callers_is_left(self):
        stopped, _ = self.stop({CALLER: _row(CALLER, "done"), CALLED: _row(CALLED, "done", caller=CALLER),
                                DEEPER: _row(DEEPER, "open", caller="att-" + "d" * 32)})
        self.assertEqual(stopped, [])

    def test_an_unreadable_registry_stops_nothing_and_raises_nothing(self):
        with mock.patch.object(CLOSE, "_rows", side_effect=OSError("gone")), mock.patch.object(CLOSE, "jobs_path", return_value=Path("/j")):
            self.assertEqual(CLOSE.stop_called_workers("/j", CALLER), [])


class FollowingCalledWorkTest(U.UnitLegBase):
    """The worker that started a unit leg follows it (`start --route`): it is no session, its attempt is the caller."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.rows = {W.attempt_id(self.route, "unit"): (
            "open", DC.parse_registry_metadata(self.leg_row(self.route, self.path, caller_attempt_id=CALLER).split("\t")[5].strip()))}

    def slot(self):
        with mock.patch.object(W, "_owns", return_value=False), mock.patch.object(W, "_current_parent_session_id", return_value="other"):
            return W._slot(self.route, "unit", self.rows)

    def test_the_caller_follows_its_leg(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": CALLER}):
            self.assertEqual(self.slot(), W.attempt_id(self.route, "unit"))

    def test_nobody_else_adopts_a_live_leg_it_did_not_start(self):
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": DEEPER}):
            with self.assertRaises(DC.DispatchContractError) as raised:
                self.slot()
        self.assertEqual(raised.exception.reason, "work-parent-recovery-required")

    def test_a_capacity_pause_met_inside_a_worker_arms_no_later_start(self):
        import capacity_auto_resume
        result = {"state": "waiting-capacity", "reason": "launch-capacity-wait"}
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}), \
                mock.patch.object(capacity_auto_resume, "arm") as arm:
            self.assertIs(W._arm_capacity_resume(result, self.path, self.root), result)
        arm.assert_not_called()
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": ""}), \
                mock.patch.object(capacity_auto_resume, "arm", return_value=None) as arm:
            W._arm_capacity_resume(result, self.path, self.root)      # a depth-0 session's pause is armed as before
        arm.assert_called_once()


class CallerWaitWindowTest(U.UnitLegBase):
    """One wait is one start window: when it ends while the leg a worker started still runs, the receipt gives the
    same wait again (the running receipt's `parent_next_command`), and nothing is launched or composed anew. For a leg
    a Claude worker started that wait is, to the character, the one its refusal line gave (`worker_utility`, the form
    its allow rules name), and its window ends inside the Bash call it runs in; an ended receipt names no wait."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.jobs = Path(self.root) / "jobs.log"
        self.now, self.launches, self.windows = [1_000.0], [], []
        self.home = str(HERE.parent)     # the release the owner runs from (its allow rules name it)
        for patch in (mock.patch.object(W, "default_parent_session_id", return_value="parent"),
                      mock.patch.object(W, "prepare_task_request", return_value=None),
                      mock.patch.object(W, "join_selected_attempts", side_effect=self.join),
                      mock.patch.dict(os.environ, {"AGENT_DISPATCH_ATTEMPT_ID": CALLER, "AGENT_HOME": self.home})):
            patch.start()
            self.addCleanup(patch.stop)

    def join(self, **kwargs):
        self.windows.append(kwargs["timeout"])
        self.now[0] += kwargs["timeout"]          # the whole window passes on the fixture's clock
        return {"state": "timeout", "children": []}

    def wait(self, wait=True):
        return W.start_work(self.route, self.path, self.jobs, wait=wait, sleep=lambda seconds: None,
                            run=lambda *args, **kwargs: self.launches.append(args), clock=lambda: self.now[0])

    def legs(self, harness="claude", **leg):
        caller = f"2026-11-17T00:00:00Z\trunning\t/w\t/w\towner\tattempt_id={CALLER},worker_type=owner,harness={harness}\n"
        self.jobs.write_text(caller + self.leg_row(self.route, self.path, status="running", caller_attempt_id=CALLER,
                                                   parent_completion_delivery="poll-fallback", **leg))

    def test_a_claude_worker_s_wait_is_the_refusal_s_string_past_each_window(self):
        import native_subagent_redirect as redirect
        from start_receipt import next_line
        self.legs()
        refusal = redirect.reason_for(self.wait(wait=False), "light", "claude", worker=True)   # compose's receipt
        told = refusal.split(" run ", 1)[1].split(" to receive its answer.", 1)[0]
        del self.windows[:]
        receipts = [self.wait() for _ in range(3)]
        self.assertEqual(self.windows, [W.WORKER_WINDOW_SECONDS] * 3)    # each window ends inside its Bash call
        self.assertLess(W.WORKER_WINDOW_SECONDS, int(_headless().HEADLESS_BASH_TIMEOUT_DEFAULTS["BASH_DEFAULT_TIMEOUT_MS"]) / 1000)
        self.assertGreaterEqual(self.now[0] - 1_000.0, 2 * W.START_WINDOW_SECONDS)   # past the old window twice
        rules = _headless()._allowlist_rules(Path(self.home), "/w", self.root, "owner")
        for receipt in receipts:
            self.assertEqual((receipt["state"], receipt["parent_next"]), ("running", "bounded-wait"))
            self.assertEqual(receipt["parent_next_command"], told)                  # the same string, every time
            self.assertTrue(next_line(receipt).endswith(": " + told))               # and the CLI's last line
            self.assertTrue(any(told.startswith(rule[5:-3] + " ") for rule in rules if rule.endswith(" *)")))
            self.assertIn("run parent_next_command again", receipt["next_step"])
        self.assertEqual(self.launches, [])       # no new launch, no compose: the same route and attempt

    def test_an_ended_receipt_names_no_wait(self):
        from start_receipt import next_line
        self.legs()
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}):
            with mock.patch.object(W, "_outcome", return_value={"attempt_id": "a", "classification": "success"}):
                done = self.wait()
            with mock.patch.object(W, "_outcome", return_value={"attempt_id": "a", "classification": "missing"}):
                failed = self.wait()
        self.assertEqual((done["state"], done["parent_next_command"]), ("completed", ""))
        self.assertEqual(next_line(done), "next: the route is complete; report its result")
        self.assertEqual((failed["state"], failed["parent_next"]), ("needs-attention", "compose"))
        for receipt in (done, failed):
            self.assertNotIn("bounded wait", next_line(receipt))

    def test_a_leg_another_worker_started_keeps_the_receipt_s_own_form(self):
        self.legs(harness="codex")
        receipt = self.wait()
        self.assertEqual(receipt["parent_next_command"], receipt["resume_command"] + " --wait")
        self.assertEqual(self.windows, [W.START_WINDOW_SECONDS])

    def test_a_leg_no_worker_started_keeps_the_bounded_fallback_handoff(self):
        self.jobs.write_text(self.leg_row(self.route, self.path, status="running",
                                          parent_completion_delivery="poll-fallback"))
        receipt = self.wait()
        self.assertEqual((receipt["state"], receipt["reason"]), ("needs-attention", "parent-wait-deadline"))
        self.assertNotIn("parent_next_command", receipt)
        self.assertEqual(self.launches, [])

    def test_a_stopped_leg_s_receipt_is_held_and_refused_as_ended(self):
        # The receipts `start_work` really writes for a registered leg stopped from Fleet or cancelled by its parent
        # are held by the redirect (no native run) and refused before any command is built.
        import native_subagent_redirect as redirect
        import route_parent_close
        cancelled = U.JOIN.CurrentDeliveryState(None, "", "", "", "done", "CANCELLED", True, 0, False,
                                                cancelled=True, cancellation_requested=True)
        stopped = U.JOIN.CurrentDeliveryState(None, "", "", "", "done", "", True, 0, False)
        for state, row in ((cancelled, {"note": "cancelled-by-parent"}),
                           (stopped, {"note": route_parent_close.STOP_NOTE, **route_parent_close.STOP_EVIDENCE})):
            with self.subTest(row["note"]):
                self.jobs.write_text(self.leg_row(self.route, self.path, status="done", caller_attempt_id=CALLER,
                                                  launch_started="1", **row))
                with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready"}), \
                        mock.patch.object(W, "current_delivery_state", return_value=state):
                    receipt = W.start_work(self.route, self.path, self.jobs, run=self.launches.append)
                self.assertTrue(redirect.registered(receipt, worker=True), receipt)
                self.assertTrue(redirect.reason_for(receipt, "light", "claude", worker=True).endswith(
                    "It has already ended with nothing to read. Do not call it again."))
        self.assertEqual(self.launches, [])


def _headless():
    """The Claude adapter's launcher, for the allow rules it really gives an owner (`_allowlist_rules`)."""
    spec = importlib.util.spec_from_file_location("dispatch_headless_for_nested",
                                                  HERE.parent / "adapters" / "claude" / "bin" / "dispatch-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CalledResultTest(U.UnitLegBase):
    """What a waiting caller reads: finished work comes back with its summary, a run that did no work does not."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.jobs = Path(self.root) / "jobs.log"
        self.jobs.write_text(self.leg_row(self.route, self.path, status="done", caller_attempt_id=CALLER))
        self.aid = W.attempt_id(self.route, "unit")

    def follow(self, classification):
        outcome = {"attempt_id": self.aid, "classification": classification}
        with mock.patch.object(W, "join_selected_attempts", return_value={"state": "ready", "children": []}), \
                mock.patch.object(W, "_outcome", return_value=outcome):
            return W.start_work(self.route, self.path, self.jobs, wait=True, run=lambda *a, **k: None, sleep=lambda _s: None)

    def test_finished_work_comes_back_with_the_sealed_summary(self):
        import work_intent
        with mock.patch.object(work_intent, "sealed_summary", return_value="[Completion summary] unit\nVerdict: issues"):
            result = self.follow("success")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"], "[Completion summary] unit\nVerdict: issues")

    def test_the_summary_is_read_for_a_moment_because_the_seal_follows_the_close(self):
        import work_intent
        slept = []
        with mock.patch.object(work_intent, "sealed_summary", side_effect=["", "", "[Completion summary] unit"]):
            got = W._unit_summary(self.jobs, self.aid, sleep=slept.append)
        self.assertEqual(got, {"summary": "[Completion summary] unit"})
        self.assertEqual(len(slept), 2)
        with mock.patch.object(work_intent, "sealed_summary", return_value=""):
            self.assertEqual(W._unit_summary(self.jobs, self.aid, sleep=lambda _s: None), {})   # never waits for ever

    def test_a_run_that_did_no_work_does_not_come_back_as_finished(self):
        import work_intent
        with mock.patch.object(work_intent, "sealed_summary", return_value="[Completion summary] unit") as sealed:
            result = self.follow("attention")                      # an empty answer, an error, a usage limit
        self.assertEqual(result["state"], "needs-attention")
        self.assertEqual(result["reason"], "unit-outcome-needs-inspection")
        self.assertNotIn("summary", result)
        self.assertEqual(result["required_action"], "compose-again")     # the same work can be asked again
        sealed.assert_not_called()


class CallerDeliveryTest(unittest.TestCase):
    """A leg a worker started is delivered to its caller by the printed wait, even under a supervised owner."""

    def args(self, capability="unit"):
        return type("Args", (), dict(action="start", dispatch_depth=1, execution_surface="registered-headless",
                                     registered_worker=True, parent_session_id="s1", parent_harness="claude",
                                     capability=capability))()

    def test_a_worker_started_unit_leg_is_polled_whoever_supervises_its_caller(self):
        import dispatch_parent_completion as PARENT
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_CHILD": "1"}), \
                mock.patch.object(PARENT, "parent_supervisor_is_live", return_value=True):
            args = self.args()
            self.assertEqual(PARENT.resolve_parent_completion_delivery(args), "poll-fallback")
            self.assertEqual(args.parent_completion_reason, "caller-worker-waits")
            other = self.args(capability="autopilot-code")       # every other launch keeps the supervised carrier
            self.assertEqual(PARENT.resolve_parent_completion_delivery(other), "parent-runtime-supervised")


class ReadinessInsideAWorkerTest(unittest.TestCase):
    """A worker asking which registered children it can start (a unit route); every other caller keeps its refusal."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("readiness_for_nested", HERE / "dispatch-readiness.py")
        self.readiness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.readiness)

    def generate(self, **extra):
        seen = []
        with mock.patch.dict(os.environ, {"AGENT_DISPATCH_DEPTH": "1"}), \
                mock.patch.object(self.readiness.NESTED, "evaluate", side_effect=lambda a: seen.append(a) or {"status": "supported", "child_harness": a.child_harness}):
            result = self.readiness.generate(worktree=HERE, jobs=Path("/tmp/j.log"), owner_harnesses=["codex"],
                                             child_harnesses=["claude"], **extra)
        return result, seen

    def test_a_worker_may_ask_for_a_unit_route_and_is_not_checked_as_a_prospective_owner(self):
        result, seen = self.generate(worker_caller=True)
        self.assertEqual(result["candidates"][0]["status"], "supported")
        self.assertFalse(seen[0].prospective_standard_owner)

    def test_any_other_caller_inside_dispatch_is_still_refused(self):
        with self.assertRaises(self.readiness.ReadinessError) as raised:
            self.generate()
        self.assertEqual(str(raised.exception), "dispatch-readiness-inside-dispatch")


class EphemeralRootTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project" / ".agent_reports"
        self.project.mkdir(parents=True)

    def test_the_scratch_root_is_beside_the_runtime_directory_and_marked(self):
        root = unit_ephemeral.scratch_root(self.project)
        self.assertTrue(unit_ephemeral.is_scratch_root(root))
        self.assertFalse(unit_ephemeral.is_scratch_root(self.project))
        self.assertNotIn(str(self.project), str(root))               # nothing of it is in the project's artifact folder
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [])
        self.assertEqual(unit_ephemeral.scratch_root(self.project), root)    # one root for the project

    def test_a_scratch_root_given_back_is_the_same_root_and_never_a_new_one(self):
        root = unit_ephemeral.scratch_root(self.project)
        self.assertEqual(unit_ephemeral.scratch_root(root), root)             # a worker that starts another
        self.assertEqual(unit_ephemeral.scratch_root(unit_ephemeral.scratch_root(root)), root)
        self.assertEqual(sorted(p.name for p in root.parent.iterdir()), [".agent_reports"])

    def test_the_scratch_root_counts_under_the_projects_governor_and_no_other(self):
        root = unit_ephemeral.scratch_root(self.project)
        governor = self.project.resolve() / ".runtime" / "model-worker-governor"
        self.assertEqual(unit_ephemeral.counted_root(root), self.project.resolve())
        self.assertEqual(unit_ephemeral.counted_root(self.project), self.project)    # an ordinary root counts as itself
        self.assertEqual(DC.resolve_model_governor_root(root, environ={}), governor)
        self.assertEqual(DC.resolve_model_governor_root(self.project, environ={}), governor)
        # The worker's own environment names the project's governor: the scratch root accepts it, as it must.
        self.assertEqual(DC.resolve_model_governor_root(root, environ={"AGENT_MODEL_GOVERNOR_ROOT": str(governor)}), governor)
        with self.assertRaises(DC.DispatchContractError):                  # a governor of its own is still refused
            DC.resolve_model_governor_root(root, environ={"AGENT_MODEL_GOVERNOR_ROOT": str(root / ".runtime" / "model-worker-governor")})

    def test_a_full_project_governor_stops_a_record_free_start_too(self):
        root = unit_ephemeral.scratch_root(self.project)
        governor = HERE / "model-worker-governor.py"
        with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKER_CLASS_LIMIT_DISPATCH": "2"}):
            held = DC.resolve_model_governor_root(self.project, environ={})
            tokens = [DC.reserve_governor_token(governor, held, "dispatch")[0] for _ in range(2)]
            self.assertEqual(len(tokens), 2)
            with self.assertRaises(DC.DispatchContractError) as raised:      # the scratch root has no pool of its own
                DC.reserve_governor_token(governor, DC.resolve_model_governor_root(root, environ={}), "dispatch")
            self.assertEqual(raised.exception.reason, "model-worker-governor-denied")
        self.assertFalse((root / ".runtime" / "model-worker-governor").exists())

    def test_a_marker_without_a_project_is_filled_in_and_the_governor_default_follows_it(self):
        root = unit_ephemeral.scratch_root(self.project)
        (root / ".ephemeral").write_text("scratch artifact root of ephemeral unit routes; nothing here is a record\n")
        self.assertEqual(unit_ephemeral.counted_root(root), root)         # an old marker names no project
        self.assertEqual(unit_ephemeral.scratch_root(self.project), root)
        self.assertEqual(unit_ephemeral.counted_root(root), self.project.resolve())
        governor = _load("model_worker_governor_for_nested", "model-worker-governor.py")
        resolver = Path(self.tmp.name) / "artifact-root.sh"
        resolver.write_text(f"#!/bin/sh\nprintf %s '{root}'\n")
        resolver.chmod(0o755)
        with mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": ""}), \
                mock.patch.object(governor.Path, "with_name", return_value=resolver):
            self.assertEqual(governor.default_root(), self.project.resolve() / ".runtime" / "model-worker-governor")

    def test_the_answer_is_the_report_cut_with_the_place_to_read_the_rest(self):
        report = Path(self.tmp.name) / "report.md"
        route = {"capability": "unit", "route_id": "rt-1"}
        report.write_text("Final verdict: mergeable\n\n\nfound it in a.py\n")
        short = unit_ephemeral.answer_summary(route, report, head="[Completion summary]")
        self.assertEqual(short.splitlines()[:2], ["[Completion summary] unit · route rt-1 · ephemeral", "Answer:"])
        self.assertIn("found it in a.py", short)
        self.assertNotIn("\n\n", short)                              # a sealed summary is one block
        self.assertNotIn("read the rest", short)
        report.write_text("\n".join(f"line {n} " + "x" * 80 for n in range(200)))
        long = unit_ephemeral.answer_summary(route, report, head="[Completion summary]")
        self.assertIn("line 0 ", long)
        self.assertNotIn("line 199 ", long)
        self.assertIn(f"read the rest in {report}", long)            # cut: it says where the whole answer is
        self.assertLess(len(long), unit_ephemeral.ANSWER_CHARS + 400)

    def test_no_text_is_no_answer(self):
        report = Path(self.tmp.name) / "report.md"
        for text in ("", "\n  \n"):
            report.write_text(text)
            self.assertEqual(unit_ephemeral.answer_summary({"route_id": "rt-1"}, report, head="[x]"), "")
        self.assertEqual(unit_ephemeral.answer_summary({"route_id": "rt-1"}, report.with_name("none.md"), head="[x]"), "")


    def other_project(self, name="other"):
        other = Path(self.tmp.name) / name / ".agent_reports"
        other.mkdir(parents=True)
        return other

    def test_the_scratch_root_is_one_folder_per_project_named_by_a_key_of_its_path(self):
        import hashlib
        root = unit_ephemeral.scratch_root(self.project)
        key = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:12]
        self.assertEqual((root.name, root.parent.name, root.parent.parent.name), (".agent_reports", key, "ephemeral"))
        self.assertEqual((root / ".ephemeral").read_text().splitlines()[0], f"project_artifact_root: {self.project.resolve()}")

    def test_a_place_that_is_not_shaped_like_a_scratch_root_is_not_one_even_with_a_marker(self):
        for shape in ("ephemeral/.agent_reports", "ephemeral/abc/.agent_reports", "other/0123456789ab/.agent_reports",
                      "ephemeral/0123456789ab/reports", "ephemeral/0123456789AB/.agent_reports"):
            root = Path(self.tmp.name) / shape
            root.mkdir(parents=True)
            (root / ".ephemeral").write_text(f"project_artifact_root: {self.project}\n")
            self.assertFalse(unit_ephemeral.is_scratch_root(root), shape)
            self.assertEqual(unit_ephemeral.counted_root(root), root, shape)
        # the old place (no key folder) is neither read nor written: a call from the project makes a keyed root
        old = Path(self.tmp.name) / "project" / ".agent_runtime" / "ephemeral" / ".agent_reports"
        old.mkdir(parents=True)
        (old / ".ephemeral").write_text(f"project_artifact_root: {self.tmp.name}/elsewhere\n")
        before = (old / ".ephemeral").read_text()
        root = unit_ephemeral.scratch_root(self.project)
        self.assertNotEqual(root, old)
        self.assertEqual((old / ".ephemeral").read_text(), before)
        self.assertEqual(sorted(p.name for p in old.parent.iterdir()), sorted([".agent_reports", root.parent.name]))

    def test_a_marker_naming_another_or_a_missing_project_is_written_again_by_the_project_that_calls(self):
        root = unit_ephemeral.scratch_root(self.project)
        for named in (self.other_project(), Path(self.tmp.name) / "gone" / ".agent_reports"):
            (root / ".ephemeral").write_text(f"project_artifact_root: {named}\nscratch artifact root of ephemeral unit routes\n")
            self.assertEqual(unit_ephemeral.counted_root(root), root)         # it does not verify for this folder
            self.assertEqual(unit_ephemeral.scratch_root(self.project), root)
            self.assertEqual(unit_ephemeral.counted_root(root), self.project.resolve())
            self.assertEqual(DC.resolve_model_governor_root(root, environ={}),
                             self.project.resolve() / ".runtime" / "model-worker-governor")
        (root / ".ephemeral").write_bytes(b"\xff\xfe")                        # unreadable
        self.assertEqual(unit_ephemeral.counted_root(root), root)
        self.assertEqual(unit_ephemeral.scratch_root(self.project), root)
        self.assertEqual(unit_ephemeral.counted_root(root), self.project.resolve())

    def test_two_projects_on_one_inherited_runtime_have_their_own_scratch_root_and_governor(self):
        shared = Path(self.tmp.name) / "shared-runtime"
        other = self.other_project()
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME_DIR": str(shared)}):
            a, b = unit_ephemeral.scratch_root(self.project), unit_ephemeral.scratch_root(other)
            self.assertEqual(unit_ephemeral.scratch_root(self.project), a)    # nobody overwrites the other
            self.assertEqual(unit_ephemeral.scratch_root(other), b)
        self.assertNotEqual(a, b)
        self.assertEqual(a.parent.parent, b.parent.parent)                     # the one shared runtime directory
        self.assertEqual(a.parent.parent.parent, shared)
        self.assertEqual(DC.resolve_model_governor_root(a, environ={}), self.project.resolve() / ".runtime" / "model-worker-governor")
        self.assertEqual(DC.resolve_model_governor_root(b, environ={}), other.resolve() / ".runtime" / "model-worker-governor")

    def test_a_worker_on_a_scratch_root_whose_marker_names_another_project_is_refused_and_never_counted_there(self):
        root = unit_ephemeral.scratch_root(self.project)
        governor = self.project.resolve() / ".runtime" / "model-worker-governor"
        other = self.other_project()
        (root / ".ephemeral").write_text(f"project_artifact_root: {other}\nscratch artifact root of ephemeral unit routes\n")
        self.assertEqual(unit_ephemeral.scratch_root(root), root)             # a worker given the scratch root writes nothing
        self.assertEqual((root / ".ephemeral").read_text().splitlines()[0], f"project_artifact_root: {other}")
        env = {"AGENT_MODEL_GOVERNOR_ROOT": str(governor)}                    # the worker's environment names the project
        with self.assertRaises(DC.DispatchContractError) as raised:
            DC.resolve_model_governor_root(root, environ=env)
        self.assertEqual(raised.exception.reason, "noncanonical-model-governor-root")
        self.assertNotEqual(DC.resolve_model_governor_root(root, environ={}), other.resolve() / ".runtime" / "model-worker-governor")
        self.assertFalse((other / ".runtime").exists())

    def test_overlapping_calls_keep_one_scratch_root(self):
        first = unit_ephemeral.scratch_root(self.project)
        roots = {unit_ephemeral.scratch_root(self.project), unit_ephemeral.scratch_root(first),
                 unit_ephemeral.scratch_root(unit_ephemeral.scratch_root(first))}
        self.assertEqual(roots, {first})
        self.assertEqual(sorted(p.name for p in first.parent.parent.iterdir()), [first.parent.name])


class EmptyAnswerEndTest(U.UnitLegBase):
    """Both directions of the same end: a blocking review with its report is finished work, an empty answer is not."""
    REPORT = U.BlockingReviewEndTest.REPORT

    def setUp(self):
        super().setUp()
        self.route, self.path = self.unit_route()
        self.worktree = Path(self.route["cwd"])
        self.scratch = unit_ephemeral.scratch_root(Path(self.root) / "project" / ".agent_reports")

    def read(self, root, text, verdict="PASS", blocker="none"):
        report = Path(root) / "units" / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text)
        log = Path(self.root) / "leg2.jsonl"
        log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                   "result": f"artifact: {report}\nverdict: {verdict}\nblocker: {blocker}"}) + "\n")
        binding = DC.parse_registry_metadata(self.leg_row(
            self.route, self.path, status="done", log_file=str(log), artifact_root=str(root)).split("\t")[5].strip())
        return TERMINAL.inspect_terminal_attempt(log, worktree=self.worktree, artifact_root_metadata=str(root),
                                                 worker_type="review", artifact_binding=binding)

    def test_an_empty_answer_in_the_scratch_root_is_not_finished_work(self):
        for text in ("", "   \n\n"):
            view = self.read(self.scratch, text)
            self.assertEqual((view["state"], view["reason"]), ("invalid", "empty-answer"))

    def test_the_same_empty_report_in_an_ordinary_root_reads_as_before(self):
        view = self.read(self.root, "")
        self.assertEqual(view["state"], "valid")              # this PR changes no existing route's end

    def test_a_lookup_leg_that_ends_with_no_artifact_reads_as_no_answer(self):
        # The end a literal reading of "read-only" could produce: `artifact: -` and PASS. The unit's text tells the leg
        # its report is the only file it writes, because this end carries no answer to the caller.
        log = Path(self.root) / "leg-none.jsonl"
        log.write_text(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                   "result": "artifact: -\nverdict: PASS\nblocker: none"}) + "\n")
        binding = DC.parse_registry_metadata(self.leg_row(
            self.route, self.path, status="done", log_file=str(log), artifact_root=str(self.scratch),
            worker_type="support").split("\t")[5].strip())
        view = TERMINAL.inspect_terminal_attempt(log, worktree=self.worktree, artifact_root_metadata=str(self.scratch),
                                                 worker_type="support", artifact_binding=binding)
        self.assertEqual(view.get("artifact_state"), "none", view)
        self.assertFalse(view.get("artifact") or view.get("primary_artifact"), view)
        # nothing was written, so the caller is given no `Answer:` (the end is not read as a finished job)
        self.assertEqual(unit_ephemeral.answer_summary(self.route, self.scratch / "units" / "unit" / "report.md",
                                                       head="x"), "")

    def test_an_answer_with_text_is_finished_work_even_when_it_blocks(self):
        view = self.read(self.scratch, self.REPORT)
        self.assertEqual((view["state"], view["verdict"]), ("valid", "PASS"))
        blocked = self.read(self.scratch, self.REPORT, verdict="FAIL", blocker="HB-BLOCKER empty list ignored")
        self.assertEqual(blocked["state"], "valid")           # a blocking review stays finished work in this root too


class FirstInstructionTest(unittest.TestCase):
    def test_the_one_way_a_worker_starts_another_is_in_the_small_first_instruction(self):
        text = worker_bootstrap.unit_call_prompt(Path("/agent"))
        for part in ("compose --start --unit <family/name> --ephemeral --prompt-file <brief>", "Answer:",
                     "stopped when you end", "never start the same question again"):
            self.assertIn(part, text)
        self.assertLess(len(text.encode()), 1024)
        self.assertNotIn("PR", text)


class ComposeEphemeralTest(U.UnitLegBase):
    def run_compose(self, *args):
        brief = Path(self.root) / "brief.md"
        brief.write_text("find it\n")
        return subprocess.run([sys.executable, str(HERE / "capability-route.py"), "compose", "--prompt-file", str(brief), *args],
                              text=True, capture_output=True, check=False, cwd=str(R.ROOT),
                              env={**F.clean_environment(), "AGENT_HOME": str(R.ROOT)})

    def test_ephemeral_is_for_one_unit_and_takes_no_artifact_root(self):
        for args in (["--ephemeral"], ["--unit", "qa/code-review", "--ephemeral", "--artifact-root", self.root]):
            done = self.run_compose("--explain", *args)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("compose-ephemeral-refused", done.stderr + done.stdout)


class RefusedRecordFreeStartTest(unittest.TestCase):
    """A worker's record-free start that no worker follows (the governor's wait, a root it does not accept) is closed."""

    def closes(self, started, child="1"):
        import route_autoclose
        route = {"artifact_root": "/scratch/root", "route_id": "rt-x"}
        env = {k: v for k, v in os.environ.items() if k != "AGENT_DISPATCH_CHILD"}
        if child is not None:
            env["AGENT_DISPATCH_CHILD"] = child
        with mock.patch.object(route_autoclose, "close_one") as close, \
                mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules, {R.__name__: R}):
            R._close_unstarted_record_free(route, "/scratch/root/route.json", started)
        return close.called

    def test_a_refused_start_with_no_row_and_no_resume_is_closed(self):
        self.assertTrue(self.closes({"state": "waiting-capacity", "launches": []}))
        self.assertTrue(self.closes({"state": "needs-attention", "reason": "noncanonical-model-governor-root", "launches": []}))

    def test_a_start_with_a_row_a_resume_or_a_depth_zero_caller_is_left_alone(self):
        self.assertFalse(self.closes({"state": "needs-attention", "launches": [], "registered_attempts": ["att-1"]}))
        self.assertFalse(self.closes({"state": "waiting-capacity", "launches": [], "auto_resume": {"armed": True}}))
        self.assertFalse(self.closes({"state": "completed", "launches": []}))
        self.assertFalse(self.closes({"state": "needs-attention", "launches": []}, child=None))


if __name__ == "__main__":
    unittest.main()
