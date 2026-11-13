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


def show_output(rq, rev, body):
    """What B's `show <rq>` prints for revision ``rev``: the report commands carry that `--rev`."""
    base = f'python3 "$AGENT_HOME/utilities/peer-steward.py" report {rq} --rev {rev}'
    return (f"request={rq} state=open\nrevision={rev} state=current\n"
            f'report-final: {base} --result "<one line>"\n'
            f'report-partial: {base} --partial --result "<one line>"\n'
            f'report-reject: {base} --reject "<reason>"\n'
            f"--- revision {rev} (sealed body) ---\n{body}\n--- end ---")


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
        out = show_output(self.rq, 1, f"BODY-1-{self.nonce}")
        partial = self.ask("SCENARIO-WATCH-TARGET INIT\n" + line + out, out, kind="tool")
        # B uses the command `show` printed, `--rev 1` and all.
        self.assertIn("report %s --rev 1 --partial --result" % self.rq, partial.shell)
        self.assertIn("PARTIAL-" + self.nonce, partial.shell)
        done = f"report={self.rq} kind=partial revision=1 late=false state=partial"
        final = self.ask("SCENARIO-WATCH-TARGET " + out + done, done, kind="tool",
                         earlier=[record("SCENARIO-WATCH-TARGET INIT", "x", reply={"shell": partial.shell})])
        self.assertIn(f"report {self.rq} --rev 1 --result", final.shell)
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
        out = show_output(self.rq, 1, f"BODY-REV1-{nonce}")
        busy = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out, out, "tool"), [])
        self.assertIn("time.sleep", busy.shell)
        earlier = [record("SCENARIO-WATCH-TARGET " + out, out, reply={"shell": busy.shell})]
        # Revision 2's guidance (its own `--rev 2` command) reached B meanwhile, but B reports the
        # work it began with the command revision 1's `show` printed.
        arrived = f"{out}\nPeer request {self.rq} (revision 2): report {self.rq} --rev 2 --result \"<one line>\"\nB-WORK-DONE"
        late = cases.answer(ctx, record(f"SCENARIO-WATCH-TARGET {arrived}", "B-WORK-DONE", "tool"), earlier)
        self.assertIn("LATE-" + nonce, late.shell)
        self.assertIn(f"report {self.rq} --rev 1 --result", late.shell)
        self.assertNotIn("--rev 2", late.shell)
        out2 = show_output(self.rq, 2, f"BODY-REV2-{nonce}")
        final = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out + out2, out2, "tool"), earlier)
        self.assertIn(f"report {self.rq} --rev 2 --result", final.shell)
        # Revision 2's line reaches the idle B: it opens the body and reports the final answer.
        line2 = f"[요청 {self.rq} 판2-{nonce}] [e9] │ 판 검토 │ 열기: show {self.rq}"
        shown2 = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + line2, line2), earlier)
        self.assertIn(f"show {self.rq}", shown2.shell)
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
        out = show_output(self.rq, 1, f"BODY-REV1-{scenario.nonce}")
        busy = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out, out, "tool"), [])
        self.assertFalse(self.BLOCKED_SLEEP.search(busy.shell), busy.shell)
        self.assertIn("B-WORK-DONE", busy.shell)
        partial = f"report={self.rq} kind=partial revision=1 late=false state=partial"
        final = cases.answer(self.ctx, record("SCENARIO-WATCH-TARGET " + out + partial, partial, "tool",
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
        title = f"검토 제목 {n}"
        self.assertEqual(cases.panel_request_lines(screen, title, "→✓✕"),
                         [f"→ [67] 검토 제목 {n} · open", f"✓ [67] 검토 제목 {n} · final"])
        self.assertEqual(cases.panel_request_lines(screen, title, "◂"), [])
        self.assertEqual(cases.panel_request_lines(screen, "zzzzzz", "→✓✕"), [])

    def test_s8_a_title_cut_on_a_narrow_panel_is_still_found_by_its_front_part(self):
        # The real r3 screens' request line (host-run r3, request-flow, panel width 38): the nonce is
        # cut off with the title's end, so the line is found by the recorded title's front part.
        closed = "│ │ ✓ 검토 제목 1c09… · final answer │ │"
        other = "│ │ ✓ 다른 제목 1c09… · final answer │ │"
        screen = "  ▐▛███▛█   Claude Code   ┌ ● [b6] claude ───┐\n" + closed + "\n" + other
        self.assertEqual(cases.panel_request_lines(screen, ["검토 제목 1c09d3"], "→✓✕"),
                         ["✓ 검토 제목 1c09… · final answer"])
        self.assertEqual(cases.panel_request_lines(screen, "검토 제목 1c09d3", "◂"), [])
        pad = " " * 160  # row 9 of the r3 screens, whole (the left pane is blank there)
        real_closed = pad + "│ │ ✓ 검토 제목 1c09… · final answer │ │"
        real_receiver = pad + "│ │ ◂ 검토 제목 1c… · partial answer │ │"
        self.assertEqual(cases.panel_request_lines(real_closed, ["검토 제목 1c0908"], "→✓✕"),
                         ["✓ 검토 제목 1c09… · final answer"])
        self.assertEqual(cases.panel_request_lines(real_receiver, ["검토 제목 1c0908"], "◂"),
                         ["◂ 검토 제목 1c… · partial answer"])
        # With the other session's tag and a cut title: `◂ [b1] 검토 제목 9036… · open`.
        opened = "│ │ ◂ [b1] 검토 제목 9036… · open │ │"
        self.assertEqual(cases.panel_request_lines(opened, ["검토 제목 9036d9"], "◂"),
                         ["◂ [b1] 검토 제목 9036… · open"])
        self.assertEqual(cases.panel_request_lines(opened, ["검토 제목 aaaaaa"], "◂"), [])
        # A whole title and a state with a long word.
        whole = "│ │ → [6e] 짧은 일 · partial answer │ │"
        self.assertEqual(cases.panel_request_lines(whole, ["짧은 일"], "→"), ["→ [6e] 짧은 일 · partial answer"])

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


class RevisionCancelVerdictTest(unittest.TestCase):
    """The cancel-before-delivery verdict (S9) and revision 2's body consumption (P5): Claude and
    OpenCode keep the strict form; Codex's native queue takes a line at once (PG-59), so its cancel
    is judged as a cancel after delivery."""

    def run_it(self, harness, notice, *, reached=False, cancel_line=True, stop_word=False, rev2=True,
               closed=True, other_state="cancelled"):
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_tui = SimpleNamespace(screen=lambda: "")
        scenario.target_address = "%1@/tmp/socket"
        first, other, third = "rq-111111", "rq-222222", "rq-333333"
        nonce = scenario.nonce
        index = iter(range(1, 100))

        def b(text, kind="user"):
            return {"text": "", "last_text": "", "turn_text": text, "main_index": next(index),
                    "last_kind": kind, "reply": {"shell": "time.sleep"} if "sleep" in text else {}}

        b_turns = [b("python3 time.sleep"), b(f"(sealed body)\nBODY-KEEP-{nonce}", "tool")]
        if rev2:
            b_turns.append(b(f"(sealed body)\nBODY-REV2-{nonce}", "tool"))
        if reached:
            b_turns.append(b(f"[요청 {other} 판1] x"))
        if reached and cancel_line:
            b_turns.append(b(f"[요청 취소 {other}] x" + (" 멈춰" if stop_word else "")))
        b_turns.append(b(f"[요청 취소 {third}] x"))
        records = {
            first: {"state": "open", "revisions": [{}, {}], "reports": [{"late": True, "revision": 1, "kind": "final"}]},
            other: {"state": other_state, "revisions": [{"transfer_ref": "r" * 32}]},
            third: {"state": "cancelled", "revisions": [{}]}}
        a_turns = [{"text": "", "last_text": "", "turn_text":
                    f"request={other} state=cancelled notice={notice}", "main_index": 90,
                    "last_kind": "tool", "reply": {}}]
        scenario.records = lambda run: records
        scenario.b_requests = lambda run: b_turns
        scenario.a_requests = lambda run: a_turns
        scenario.start_a = lambda run, text: None
        scenario.wait_state = lambda run, request_id, state, timeout=90: True
        scenario.panels = lambda run, tag: {}
        scenario.duty = lambda run, ref: {"observation": {"reason": "request-withdrawn" if closed else ""}}
        run = SimpleNamespace(harness=harness, evidence={}, notes=[], main_requests=lambda: [],
                              note=lambda text: run.notes.append(text))
        saved = cases.wait_for, cases.time.sleep
        cases.wait_for, cases.time.sleep = (lambda predicate, timeout, step=0.5: predicate()), (lambda _s: None)
        try:
            return scenario.drive(run), run.notes, run.evidence
        finally:
            cases.wait_for, cases.time.sleep = saved

    def test_claude_and_opencode_judge_a_cancel_before_delivery_strictly(self):
        for harness in ("claude", "opencode"):
            self.assertEqual(self.run_it(harness, "not-needed")[0], "pass", harness)
            self.assertEqual(self.run_it(harness, "queued-not-delivered", reached=True)[0], "fail", harness)
            self.assertEqual(self.run_it(harness, "not-needed", reached=True)[0], "fail", harness)

    def test_s9_codex_native_queue_makes_it_a_cancel_after_delivery(self):
        verdict, notes, ev = self.run_it("codex", "queued-not-delivered", reached=True)
        self.assertEqual(verdict, "pass", ev)
        self.assertIn("codex native queue accepted it at once (PG-59)", " ".join(notes))
        self.assertEqual(self.run_it("codex", "delivered", reached=True)[0], "pass")
        # The three assertions: the cancel line after the request line, `cancelled`, no "멈".
        self.assertEqual(self.run_it("codex", "queued-not-delivered", reached=True, cancel_line=False)[0], "fail")
        self.assertEqual(self.run_it("codex", "queued-not-delivered", reached=True, stop_word=True)[0], "fail")
        self.assertEqual(self.run_it("codex", "queued-not-delivered", reached=True, other_state="open")[0], "fail")
        # Codex that did withdraw it before delivery keeps the strict form too.
        self.assertEqual(self.run_it("codex", "not-needed")[0], "pass")

    def test_revision_2_body_must_be_consumed_after_the_late_report(self):
        verdict, _notes, ev = self.run_it("claude", "not-needed", rev2=False)
        self.assertEqual(verdict, "fail")
        self.assertFalse(ev["rev2_body_consumed"])


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
