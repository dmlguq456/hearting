#!/usr/bin/env python3
"""Offline checks of the peer-request scenarios: registration and the scripted model.

The real-TUI measurement is the scenarios' own run (host-run). These tests run the
scripted model against synthetic request records so a script that could never produce
the specified flow fails here, not in a 40-minute measurement.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import harness_behavior_cases_request as cases
import harness_behavior_scenarios as hb

HERE = Path(__file__).resolve().parent


def record(text, last, kind="user", reply=None, turn=None):
    found = {"text": text, "last_text": last, "last_kind": kind, "reply": reply or {}, "main_index": 0}
    if turn is not None:
        found["turn_text"] = turn
    return found


class RegistrationTest(unittest.TestCase):
    def test_the_scenarios_load_and_are_listed(self):
        hb._load_extra()
        for cls in cases.SCENARIOS:
            self.assertIn(cls.name, hb.SCENARIOS)
        listed = subprocess.run([sys.executable, str(HERE / "harness_behavior_scenarios.py"), "--list"],
                                capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(listed.returncode, 0, listed.stderr)
        for cls in cases.SCENARIOS:
            self.assertIn(cls.name + "\t", listed.stdout)


class ScriptedModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        run = SimpleNamespace(home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        self.scenario = cases.RequestFlowScenario()
        self.scenario.target_address = "%1@/tmp/socket"
        self.ctx = self.scenario.make_context(run)
        self.nonce = self.scenario.nonce
        self.rq = "rq-0a1b2c"

    def ask(self, text, last, kind="user", earlier=()):
        return cases.answer(self.ctx, record(text, last, kind), list(earlier))

    def test_a_sends_the_request_once_with_the_title_label_and_body_file(self):
        reply = self.ask("SCENARIO-REQ-A send the request", "SCENARIO-REQ-A send the request")
        self.assertIn("peer-steward.py\" request %1@/tmp/socket", reply.shell)
        self.assertIn("--title", reply.shell)
        self.assertIn("--body-file", reply.shell)
        again = self.ask("SCENARIO-REQ-A send the request", "SCENARIO-REQ-A send the request",
                         earlier=[record("SCENARIO-REQ-A send the request", "x", reply={"shell": reply.shell})])
        self.assertFalse(again.shell)

    def test_b_opens_the_body_only_through_its_own_show_call_then_reports(self):
        line = f"[요청 {self.rq}] [e9] → [e7] │ 검토 제목 {self.nonce} │ 열기: x show {self.rq}"
        shown = self.ask("SCENARIO-WATCH-TARGET INIT\n" + line, line)
        self.assertIn(f"show {self.rq}", shown.shell)
        self.assertNotIn(f"BODY-1-{self.nonce}", line)
        out = (f"request={self.rq} state=open\n--- revision 1 (sealed body) ---\nBODY-1-{self.nonce}\n--- end ---")
        partial = self.ask("SCENARIO-WATCH-TARGET INIT\n" + line + out, out, kind="tool")
        self.assertIn("report %s --partial" % self.rq, partial.shell)
        done = f"report={self.rq} kind=partial revision=1 late=false state=partial"
        final = self.ask("SCENARIO-WATCH-TARGET " + done, done, kind="tool",
                         earlier=[record("SCENARIO-WATCH-TARGET INIT", "x", reply={"shell": partial.shell})])
        self.assertIn(f"report {self.rq} --result", final.shell)
        self.assertIn("FINAL-" + self.nonce, final.shell)

    def test_a_is_woken_by_a_report_line_without_acting(self):
        wake = f"[요청 답 {self.rq}] [e7] │ 검토 │ 일부 │ PARTIAL-{self.nonce} │ 3s │ 본문: show"
        reply = self.ask("SCENARIO-REQ-A send\n" + wake, wake)
        self.assertFalse(reply.shell)
        self.assertIn("saw the report", reply.text)

    def test_the_revision_script_holds_b_busy_then_reports_late_then_final(self):
        run = SimpleNamespace(home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_address = "%1@/tmp/socket"
        ctx = scenario.make_context(run)
        nonce = scenario.nonce
        out = f"request={self.rq} state=open\n--- revision 1 (sealed body) ---\nBODY-REV1-{nonce}\n--- end ---"
        busy = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out, out, "tool"), [])
        self.assertIn("time.sleep", busy.shell)
        earlier = [record("SCENARIO-WATCH-TARGET " + out, out, reply={"shell": busy.shell})]
        late = cases.answer(ctx, record(f"SCENARIO-WATCH-TARGET {self.rq} B-WORK-DONE", "B-WORK-DONE", "tool"), earlier)
        self.assertIn("LATE-" + nonce, late.shell)
        out2 = f"request={self.rq} state=open\n--- revision 2 (sealed body) ---\nBODY-REV2-{nonce}\n--- end ---"
        final = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out2, out2, "tool"), earlier)
        self.assertIn(f"report {self.rq} --result", final.shell)
        keep = f"request=rq-ffffff state=open\n(sealed body)\nBODY-KEEP-{nonce}"
        held = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + keep, keep, "tool"), earlier)
        self.assertFalse(held.shell)
        notice = f"[요청 취소 rq-ffffff] [e9] │ 세번째 │ 이유 취소 {nonce}"
        self.assertFalse(cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + notice, notice), earlier).shell)
        step = cases.answer(ctx, record("SCENARIO-REQ-A cancel3 rq-ffffff request=rq-ffffff delivery=x",
                                        "SCENARIO-REQ-A cancel3 rq-ffffff"), [])
        self.assertIn("--cancel", step.shell)
        self.assertIn("rq-ffffff", step.shell)


class HostRunR2Test(ScriptedModelTest):
    """What host-run r2 showed in the scenarios themselves (S1~S7)."""

    BLOCKED_SLEEP = re.compile(r"(^|[;&|]\s*)sleep\s+\d")  # Claude: "Blocked: sleep 25 followed by: ..."

    def test_s1_b_sees_the_request_line_when_codex_puts_its_guidance_after_it(self):
        line = f"[요청 {self.rq}] [e9] → [e7] │ 검토 제목 {self.nonce} │ 열기: x show {self.rq}"
        guidance = f"Peer request {self.rq} from another session. Open its sealed body with this shell command"
        shown = self.ask("SCENARIO-WATCH-TARGET INIT\n" + line + "\n" + guidance, guidance, kind="developer")
        self.assertFalse(shown.shell, "the newest item alone is the guidance, not the line")
        reply = cases.answer(self.ctx, record("SCENARIO-WATCH-TARGET INIT\n" + line + "\n" + guidance, guidance,
                                              kind="developer", turn=line + "\n" + guidance), [])
        self.assertIn(f"show {self.rq}", reply.shell)
        notice = f"[요청 취소 {self.rq}] [e9] │ 제목"
        quiet = cases.answer(self.ctx, record("SCENARIO-WATCH-TARGET x", guidance, kind="developer",
                                              turn=notice + "\n" + guidance), [])
        self.assertIn("noted the cancel", quiet.text)

    def test_s1_a_is_woken_by_a_report_line_that_is_not_the_newest_item(self):
        wake = f"[요청 답 {self.rq}] [e7] │ 검토 │ 최종 │ FINAL-{self.nonce} │ 3s │ 본문: show"
        reply = cases.answer(self.ctx, record("SCENARIO-REQ-A send\n" + wake, "guidance", kind="developer",
                                              turn=wake + "\nguidance"), [])
        self.assertFalse(reply.shell)
        self.assertIn("saw the report", reply.text)

    def test_s3_the_busy_step_is_one_every_harness_runs(self):
        for seconds in (3, 25):
            for shell in (cases.busy_step(seconds), cases.pause(seconds)):
                self.assertFalse(self.BLOCKED_SLEEP.search(shell), shell)
                self.assertTrue(shell.startswith("python3 -c"), shell)
        self.assertIn("print('B-WORK-DONE')", cases.busy_step(25))
        done = subprocess.run(["python3", "-c", "import time; time.sleep(1); print('B-WORK-DONE')"],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.stdout.strip(), "B-WORK-DONE")
        run = SimpleNamespace(home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_address = "%1@/tmp/socket"
        ctx = scenario.make_context(run)
        out = f"request={self.rq} state=open\n--- revision 1 (sealed body) ---\nBODY-REV1-{scenario.nonce}\n--- end ---"
        busy = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out, out, "tool"), [])
        self.assertFalse(self.BLOCKED_SLEEP.search(busy.shell), busy.shell)
        self.assertIn("B-WORK-DONE", busy.shell)
        partial = f"report={self.rq} kind=partial revision=1 late=false state=partial"
        final = cases.answer(self.ctx, record("SCENARIO-WATCH-TARGET " + partial, partial, "tool",
                                              reply={}), [record("x", "x", reply={"shell": "--partial"})])
        self.assertFalse(self.BLOCKED_SLEEP.search(final.shell), final.shell)

    def test_s2_the_second_revision_body_path_is_a_path_not_a_function(self):
        run = SimpleNamespace(home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_address = "%1@/tmp/socket"
        ctx = scenario.make_context(run)
        revise = cases.answer(ctx, record(f"SCENARIO-REQ-A revise {self.rq} request={self.rq}",
                                          f"SCENARIO-REQ-A revise {self.rq} request={self.rq}"), []).shell
        self.assertIn(f"request {self.rq} --rev", revise)
        self.assertIn(str(Path(self.tmp.name) / "rev-2.txt"), revise)
        self.assertNotIn("function", revise)
        self.assertNotIn("<", revise)
        parsed = subprocess.run(["bash", "-n", "-c", revise], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)

    def test_s4_the_cancel_before_delivery_is_one_shell_line_with_the_id_from_the_first_output(self):
        run = SimpleNamespace(home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_address = "%1@/tmp/socket"
        ctx = scenario.make_context(run)
        text = f"SCENARIO-REQ-A send2 {self.rq}"
        step = cases.answer(ctx, record(text, text), []).shell
        self.assertEqual(len(step.splitlines()), 1)
        self.assertIn("request %1@/tmp/socket", step)
        self.assertIn("rev-other.txt", step)
        self.assertIn('request "$rid" --cancel', step)
        self.assertIn("&&", step)
        parsed = subprocess.run(["bash", "-n", "-c", step], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        # The id extraction reads the first command's own output (`next:` lines and all).
        first_output = "next: nothing to resend\nrequest=rq-abcdef delivery=queued-not-delivered to=[?]\n"
        sed = step[step.index("sed -n"):step.index(") && test")]
        picked = subprocess.run(["bash", "-c", sed], input=first_output, capture_output=True, text=True)
        self.assertEqual(picked.stdout.strip(), "rq-abcdef")
        again = cases.answer(ctx, record(text, text), [record(text, text, reply={"shell": step})])
        self.assertFalse(again.shell)

    def test_s5_only_the_route_boxs_request_line_counts_not_messages_or_conversation(self):
        n = self.nonce
        screen = "\n".join([
            f"  [요청 rq-aaaaaa] [df] → [67] │ 검토 제목 {n} │ 판 1 │ 열기: show rq-aaaaaa      │ MESSAGES │",
            f"  conversation → [67] {n} claude · 01:02 · delivered                              │ → [67] claude · 01:02 · delivered │",
            f"                                                              │ │ → [67] 검토 제목 {n} · open │ │",
            f"                                                              │ │ ◂ [df] 다른 제목 · open │ │",
            f"                                                              │ │ ✓ [67] 검토 제목 {n} · final │ │",
        ])
        self.assertEqual(cases.panel_request_lines(screen, n, "→✓✕"),
                         [f"→ [67] 검토 제목 {n} · open", f"✓ [67] 검토 제목 {n} · final"])
        self.assertEqual(cases.panel_request_lines(screen, n, "◂"), [])
        self.assertEqual(cases.panel_request_lines(screen, "zzzzzz", "→✓✕"), [])

    def test_s7_the_handoff_steward_runs_under_the_environment_the_shared_resolver_reads(self):
        import dispatch_parent_completion as parent
        for harness in ("claude", "codex", "opencode"):
            scenario = cases.RequestHandoffScenario()
            run = SimpleNamespace(harness=harness, home=SimpleNamespace(
                env={"HOME": "/h", "AGENT_SESSION_ID": "stale", "CLAUDE_CODE_SESSION_ID": "foreign"}))
            env = scenario.caller_env(run, "sid-successor-1")
            self.assertEqual(parent.interactive_parent_identity(env), (harness, "sid-successor-1"), harness)
            self.assertEqual(env["HOME"], "/h")
            # The steward itself reads the same identity (r2: `AGENT_SESSION_ID` alone gave
            # harness `unknown`, so every handoff report was `sender-unknown`).
            script = ("import importlib.util,sys;"
                      "s=importlib.util.spec_from_file_location('ps',sys.argv[1]);m=importlib.util.module_from_spec(s);"
                      "sys.modules['ps']=m;s.loader.exec_module(m);print(m._current_session_identity())")
            found = subprocess.run([sys.executable, "-I", "-c", script, str(HERE / "peer-steward.py")],
                                   env={**env, "PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=60)
            self.assertEqual(found.stdout.strip(), str(("sid-successor-1", harness)), found.stderr)


class FlowVerdictTest(unittest.TestCase):
    """The request-flow verdict, from scripted evidence (S6): declared limits are ``gap`` only
    outside the session shell for Claude and OpenCode; the body and the attribution never are."""

    def run_flow(self, harness, *, shell, line=True, consumed=True, wakes=(1, 1), kinds=("partial", "final"),
                 reporters=("B", "B"), body_in_line=False, panels=True):
        scenario = cases.RequestFlowScenario()
        scenario.target_tui = SimpleNamespace(screen=lambda: "")
        scenario.target_address = "%1@/tmp/socket"
        rid, body = "rq-0a1b2c", f"BODY-1-{scenario.nonce}"
        b_line = {"text": body if body_in_line else "x", "last_text": "", "turn_text": f"[요청 {rid}] x",
                  "main_index": 1, "last_kind": "user", "reply": {}}
        b_body = {"text": "", "last_text": "", "turn_text": f"(sealed body)\n{body}", "main_index": 2,
                  "last_kind": "tool", "reply": {}}
        a_wakes = ([{"text": "", "last_text": "", "turn_text": f"[요청 답 {rid}] │ 일부", "main_index": 3,
                     "last_kind": "user", "reply": {}}] * wakes[0]
                   + [{"text": "", "last_text": "", "turn_text": f"[요청 답 {rid}] │ 최종", "main_index": 4,
                       "last_kind": "user", "reply": {}}] * wakes[1])
        record_ = {"state": "final", "recipient": {"session_id": "B"},
                   "reports": [{"kind": kind, "reporter": {"session_id": who}} for kind, who in zip(kinds, reporters)]}
        scenario.records = lambda run: {rid: record_}
        scenario.b_requests = lambda run: ([b_line] if line else []) + ([b_body] if consumed else [])
        scenario.a_requests = lambda run: a_wakes
        scenario.start_a = lambda run, text: None
        scenario.wait_state = lambda run, request_id, state, timeout=90: True
        shown = {"requester": ["→ [67] x · open"], "receiver": ["◂ [df] x · open"]} if panels else {"requester": [], "receiver": []}
        scenario.panels = lambda run, tag: ({**shown, "requester": ["✓ [67] x · final"]} if tag == "closed" and panels else shown)
        run = SimpleNamespace(harness=harness, evidence={"session_shell": {}} if shell else {}, notes=[],
                              main_requests=lambda: [], note=lambda text: run.notes.append(text))
        saved = cases.wait_for, cases.time.sleep
        cases.wait_for, cases.time.sleep = (lambda predicate, timeout, step=0.5: predicate()), (lambda _s: None)
        try:
            return scenario.drive(run), run.notes
        finally:
            cases.wait_for, cases.time.sleep = saved

    def test_a_complete_flow_passes(self):
        for harness in ("claude", "codex", "opencode"):
            self.assertEqual(self.run_flow(harness, shell=True)[0], "pass", harness)

    def test_a_receiver_at_rest_outside_the_shell_is_the_declared_limit_for_claude_and_opencode(self):
        verdict, notes = self.run_flow("claude", shell=False, line=False, consumed=False, wakes=(0, 0), kinds=())
        self.assertEqual(verdict, "gap")
        self.assertIn("PG-59 outside the session shell", " ".join(notes))
        self.assertEqual(self.run_flow("opencode", shell=False, line=False, consumed=False,
                                       wakes=(0, 0), kinds=())[0], "gap")

    def test_the_same_miss_is_a_fail_for_codex_and_on_the_shell(self):
        self.assertEqual(self.run_flow("codex", shell=False, line=False, consumed=False, wakes=(0, 0), kinds=())[0], "fail")
        self.assertEqual(self.run_flow("claude", shell=True, line=False, consumed=False, wakes=(0, 0), kinds=())[0], "fail")
        self.assertEqual(self.run_flow("opencode", shell=True, wakes=(1, 0))[0], "fail")

    def test_an_opencode_requester_not_woken_by_the_final_report_is_a_gap_outside_the_shell(self):
        self.assertEqual(self.run_flow("opencode", shell=False, wakes=(1, 0))[0], "gap")

    def test_the_body_and_the_attribution_are_never_a_declared_limit(self):
        self.assertEqual(self.run_flow("claude", shell=False, consumed=False)[0], "fail")
        self.assertEqual(self.run_flow("claude", shell=False, body_in_line=True)[0], "fail")
        self.assertEqual(self.run_flow("claude", shell=False, reporters=("B", "someone"))[0], "fail")
        self.assertEqual(self.run_flow("opencode", shell=False, wakes=(2, 1))[0], "fail")

    def test_a_flow_without_the_route_box_lines_is_a_gap(self):
        self.assertEqual(self.run_flow("codex", shell=True, panels=False)[0], "gap")


class RunExecuteOrderTest(unittest.TestCase):
    """``Run.execute`` calls ``script(run)`` before the home exists and ``prepare(run)`` (which starts the
    receiving TUI and fixes ``target_address``) after it. The scenarios must work in that order."""

    ADDRESS = "%7@/tmp/receiver-socket"

    def run_in_execute_order(self, scenario, startup_probe):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        run = SimpleNamespace(evidence={})  # no home yet, as when ModelFixture(script(run)) is built
        script = scenario.script(run)  # must not touch run.home or target_address
        run.home = SimpleNamespace(workspace=Path(tmp.name), env={})

        def fake_watch_prepare(self_, run_):  # WatchWakeScenario.prepare without a real TUI
            startup_probe.append(script(record("SCENARIO-WATCH-TARGET INIT", "SCENARIO-WATCH-TARGET INIT"), []))
            self_.target_tui = SimpleNamespace(screen=lambda: "")
            self_.target_address = self.ADDRESS

        original = cases.WatchWakeScenario.prepare
        cases.WatchWakeScenario.prepare = fake_watch_prepare
        try:
            scenario.prepare(run)
        finally:
            cases.WatchWakeScenario.prepare = original
        return script, Path(tmp.name)

    def test_every_request_scenario_follows_the_execute_order(self):
        for cls in cases.SCENARIOS:
            with self.subTest(scenario=cls.name):
                scenario = cls()
                probe = []
                script, workspace = self.run_in_execute_order(scenario, probe)
                self.assertEqual(len(probe), 1)
                self.assertFalse(probe[0].shell)  # the receiver's start-up turn, answered before ctx exists
                reply = script(record("SCENARIO-REQ-A send the request", "SCENARIO-REQ-A send the request"), [])
                self.assertIn('peer-steward.py" request ' + self.ADDRESS, reply.shell)
                self.assertIn("--body-file " + str(workspace), reply.shell)
                self.assertTrue(any(workspace.glob("*.txt")))


if __name__ == "__main__":
    unittest.main()
