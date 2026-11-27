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
import unicodedata
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


def cells(text):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def fill(text, width):
    return text + " " * max(0, width - cells(text))


def panel_of(*rows, width=40, label="ROUTE", under=()):
    """A side panel ``width`` cells wide as the shell draws it: its frame, a route box holding ``rows`` (box rows two
    cells in, the box's borders right inside the frame), then ``under`` (MESSAGES and the like) under the box."""
    assert all(cells(row) <= width - 8 for row in rows) and all(cells(row) <= width - 4 for row in under), (rows, under)
    return ["┌" + "─" * (width - 2) + "┐",
            f"│ ╭─ {label} " + "─" * (width - 12 - len(label)) + " ›─╮ │",
            *[f"│ │ {fill(row, width - 8)} │ │" for row in rows],
            "│ ╰" + "─" * (width - 6) + "╯ │",
            *[f"│ {fill(row, width - 4)} │" for row in under],
            "└" + "─" * (width - 2) + "┘"]


def on_screen(panel, talk=(), cols=200, left=False):
    """A whole terminal screen: the conversation and the panel two cells apart, the panel on the right (or the left)
    as ``native-display.cjs`` ``layout`` puts it; the bottom layout's panel (as wide as the window) under the talk."""
    width = cells(panel[0])
    if width >= cols:
        return "\n".join([*talk, *panel])
    native = cols - width - 2
    out = []
    for at in range(max(len(panel), len(talk))):
        said = talk[at] if at < len(talk) else ""
        shown = panel[at] if at < len(panel) else ""
        out.append((fill(shown, width) + "  " + said) if left else (fill(said, native) + "  " + shown))
    return "\n".join(row.rstrip() for row in out)


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
        panel = panel_of(f"→ [67] 검토 제목 {n} · open", "← [df] 다른 제목 · open", f"✓ [67] 검토 제목 {n} · final",
                         # 11-27: a received message wears the same `←` as a received request; MESSAGES is under the box.
                         under=["MESSAGES                         ›", f" ← [67] 검토 제목 {n} · open",
                                f" → [67] 검토 제목 {n} · final"])
        screen = on_screen(panel, [f"  [요청 rq-aaaaaa] [df] → [67] │ 검토 제목 {n} │ 판 1 │ 열기: show rq-aaaaaa",
                                   f"  conversation │ │ ← [67] 검토 제목 {n} · open │ │ quoted by the agent"])
        title = f"검토 제목 {n}"
        self.assertEqual(cases.panel_request_lines(screen, title, "→✓✕"),
                         [f"→ [67] 검토 제목 {n} · open", f"✓ [67] 검토 제목 {n} · final"])
        self.assertEqual(cases.panel_request_lines(screen, title, "←"), [])
        self.assertEqual(cases.panel_request_lines(screen, "zzzzzz", "→✓✕"), [])

    def test_s8_a_title_cut_on_a_narrow_panel_is_still_found_by_its_front_part(self):
        # The real r3 screens' request line (host-run r3, request-flow): the nonce is cut off with the title's end, so
        # the line is found by the recorded title's front part. The r3 panel stood at column 160 of 200.
        talk = ["  ▐▛███▛█   Claude Code"]
        screen = on_screen(panel_of("✓ 검토 제목 1c09… · final answer", "✓ 다른 제목 1c09… · final answer"), talk)
        self.assertEqual(cases.panel_request_lines(screen, ["검토 제목 1c09d3"], "→✓✕"),
                         ["✓ 검토 제목 1c09… · final answer"])
        self.assertEqual(cases.panel_request_lines(screen, "검토 제목 1c09d3", "←"), [])
        self.assertTrue(all(row[:160].strip() in ("", talk[0].strip()) for row in screen.splitlines()), screen)
        receiver = on_screen(panel_of("← 검토 제목 1c… · partial answer"))
        self.assertEqual(cases.panel_request_lines(receiver, ["검토 제목 1c0908"], "←"),
                         ["← 검토 제목 1c… · partial answer"])
        # With the other session's tag and a cut title: `← [b1] 검토 제목 9036… · open`.
        opened = on_screen(panel_of("← [b1] 검토 제목 9036… · open"))
        self.assertEqual(cases.panel_request_lines(opened, ["검토 제목 9036d9"], "←"),
                         ["← [b1] 검토 제목 9036… · open"])
        self.assertEqual(cases.panel_request_lines(opened, ["검토 제목 aaaaaa"], "←"), [])
        # A whole title and a state with a long word.
        whole = on_screen(panel_of("→ [6e] 짧은 일 · partial answer"))
        self.assertEqual(cases.panel_request_lines(whole, ["짧은 일"], "→"), ["→ [6e] 짧은 일 · partial answer"])
        # Outside a route box the same row is not a request line.
        boxless = on_screen(["┌" + "─" * 38 + "┐", "│ │ ✓ 검토 제목 1c09… · final answer │ │", "└" + "─" * 38 + "┘"])
        self.assertEqual(cases.panel_request_lines(boxless, ["검토 제목 1c09d3"], "→✓✕"), [])

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


# The panel the session shell draws (`panel-view.cjs`), for the reader below: a request, a message with the
# same title and a route, at the sizes a scenario's terminal gives the panel.
PANEL_SCRIPT = r"""
const path=require('node:path'),view=require(process.argv[1]);
const cells=s=>[...s].reduce((n,c)=>n+(/[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]/u.test(c)?2:1),0);
const t=text=>({text}),title='검토 제목 9c4f7d',paint={harness:t('codex'),model_text:t('GPT 5.5'),effort:t('high')};
const req=(glyph,kind,status,id='rq-aaaaaa')=>({id,way:glyph==='→'?'→':'←',glyph,who:'[b2]',title,status,age:'2m',kind,paint,details:[],body:[]});
const route=(state,glyph)=>({id:'rt-a',title,state,kind:state,shape:'one-shot',kind_word:'code',role:'owner',elapsed:'3m',
  paint:{glyph:t(glyph),harness:t('claude')}});
const message={way:'←',who:'[b2]',harness:'codex',when:'09:01',kind:'reply',text:title+' · open'};
// A received message whose three lines quote a route box with the request in it (the review's screen, 72 cells).
const quoting=['│ ╭─ ROUTE ─────────── ›─╮ │','│ │ ← [b2] '+title+' · open │ │','│ ╰────────────────────╯ │']
  .map((text,i)=>({...message,when:'09:0'+i,text}));
const cases={
 'message-only':[44,40,true,[],[message],[]],
 'message-quotes-a-box':[72,40,true,[],quoting,[]],
 'request-and-message':[44,40,true,[req('←','request-in','open')],[message],[]],
 'short-no-rule':[38,7,true,[req('←','request-in','open')],[message],[]],
 'narrow':[32,7,true,[req('←','request-in','open')],[message],[]],
 'bottom':[80,24,false,[req('←','request-in','open')],[message],[]],
 'requester':[44,40,true,[req('→','request-out','partial answer'),req('✓','request-done','final answer','rq-bbbbbb')],
   [{...message,way:'→'}],[route('done','✓')]],
 'requester-no-request':[44,40,true,[],[{...message,way:'→'}],[route('done','✓')]],
};
const out={};
for(const [name,[width,height,side,requests,messages,routes]] of Object.entries(cases)){
 const v=view.render({width,height,side,model:{route_count:routes.length,routes,requests,messages,styles:{}},state:'working',
   frame:1000,since:990,cellWidth:cells,configuration:{side_width:width},background:''});
 out[name]=v.rows.map(r=>r.map(p=>p.text).join(''));
}
console.log(JSON.stringify(out));
"""


class PanelRequestLinesTest(unittest.TestCase):
    """The scenario reads the request lines from the panel the session shell draws: the screen cut to the panel's
    columns first, then the route box right inside the panel's frame (11-27: a received message in MESSAGES wears the
    same `←` as a received request, and a conversation or a message may quote a whole route box)."""

    TITLE = "검토 제목 9c4f7d"
    BOX = ["│ ╭─ ROUTE ─────────── ›─╮ │", f"│ │ ← [b2] {TITLE} · open │ │", "│ ╰────────────────────╯ │"]
    TALK = [f"  에이전트가 적은 줄: │ │ ← [b2] {TITLE} · open │ │", "  ▐▛███▛█   Claude Code"]

    @classmethod
    def setUpClass(cls):
        import json
        found = subprocess.run(["node", "-e", PANEL_SCRIPT, str(HERE / "session-shell" / "panel-view.cjs")],
                               capture_output=True, text=True, timeout=60)
        if found.returncode:
            raise AssertionError(found.stderr)
        cls.panels = json.loads(found.stdout)

    def lines(self, name, marks="←", *, talk=None, left=False, cols=200):
        """The reader over ``name``'s panel on a whole screen ``cols`` wide beside ``talk`` (right of it, or left with
        ``left``), told the shell's side width and position as the host lays the panel out."""
        panel = self.panels[name]
        self.assertTrue(panel[0].startswith("┌") and panel[-1].startswith("└"), "\n".join(panel))
        width = cells(panel[0])
        screen = on_screen(panel, self.TALK if talk is None else talk, cols, left)
        configuration = {"side_width": width if width <= 80 else 40, "position": "left" if left else "right"}
        return cases.panel_request_lines(screen, [self.TITLE], marks, cols=cols if left else None,
                                         configuration=configuration)

    def test_a_received_message_with_the_request_title_is_not_a_request(self):
        # (The first review's screen: no request, a received message whose text reads like a request line.)
        self.assertTrue(any(f"← [b2] {self.TITLE} · open" in row for row in self.panels["message-only"]),
                        "\n".join(self.panels["message-only"]))
        self.assertEqual(self.lines("message-only"), [])

    def test_a_route_box_quoted_in_messages_or_the_conversation_is_not_the_panels(self):
        # (a) No request: three received messages in a 72-cell panel quote a route box with the request in it.
        rows = self.panels["message-quotes-a-box"]
        self.assertTrue(any("╭─ ROUTE" in row for row in rows) and not any(row.startswith("│ ╭─") for row in rows),
                        "\n".join(rows))
        self.assertEqual(self.lines("message-quotes-a-box"), [])
        # (b) No request: the conversation beside the panel quotes the same box.
        self.assertEqual(self.lines("message-only", talk=["  " + line for line in self.BOX]), [])
        # (c) A request: the conversation's quoted bottom border stands on the same screen row as the real box's
        # first inner row, which must not close the panel's box.
        panel = self.panels["request-and-message"]
        top = next(i for i, row in enumerate(panel) if row.startswith("│ ╭─ ROUTE"))
        talk = [""] * (top + 1) + ["  " + self.BOX[2]]
        self.assertEqual(self.lines("request-and-message", talk=talk), [f"← [b2] {self.TITLE} · open"])

    def test_the_request_is_counted_once_with_the_panel_on_either_side(self):
        rows = self.panels["request-and-message"]
        self.assertTrue(any(row.startswith("│ │ ← [b2] codex GPT 5.5 high") for row in rows), "\n".join(rows))
        self.assertEqual(self.lines("request-and-message"), [f"← [b2] {self.TITLE} · open"])
        self.assertEqual(self.lines("request-and-message", left=True, talk=self.TALK + ["  " + line for line in self.BOX]),
                         [f"← [b2] {self.TITLE} · open"])

    def test_a_short_or_narrow_panel_drops_the_rule_and_cuts_the_title(self):
        for name in ("short-no-rule", "narrow"):
            rows = self.panels[name]
            self.assertFalse(any("─ request ─" in row for row in rows), "\n".join(rows))
            self.assertTrue(any("…" in row and row.startswith("│ │ ← [b2]") for row in rows), "\n".join(rows))
            found = self.lines(name)
            self.assertEqual(len(found), 1, (name, found, rows))
            self.assertTrue(found[0].startswith("← [b2] 검") and "…" in found[0] and found[0].endswith(" · open"),
                            (name, found))

    def test_the_bottom_layout_reads_the_one_row_form_under_the_conversation(self):
        # An 80-column window: the panel stands under the native area, as wide as the window.
        self.assertEqual(self.lines("bottom", cols=80, talk=["  " + line for line in self.BOX]),
                         [f"← [b2] {self.TITLE} · open"])

    def test_a_finished_route_with_the_same_title_is_not_a_closed_request(self):
        self.assertEqual(self.lines("requester", "→✓✕"),
                         [f"→ [b2] {self.TITLE} · partial answer", f"✓ [b2] {self.TITLE} · final answer"])
        rows = self.panels["requester-no-request"]
        self.assertTrue(any(row.startswith("│ │ ✓ ") for row in rows), "\n".join(rows))
        self.assertEqual(self.lines("requester-no-request", "→✓✕"), [])


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
        shown = {"requester": ["→ [67] x · open"], "receiver": ["← [df] x · open"]} if panels else {"requester": [], "receiver": []}
        scenario.panels = lambda run, tag: ({**shown, "requester": ["✓ [67] x · final"]} if tag == "closed" and panels else shown)
        run = SimpleNamespace(harness=harness, evidence={"session_shell": {}} if shell else {}, notes=[],
                              main_requests=lambda: [], note=lambda text: run.notes.append(text))
        saved = cases.wait_for, cases.time.sleep
        cases.wait_for, cases.time.sleep = (lambda predicate, timeout, step=0.5: predicate()), (lambda _s: None)
        try:
            return scenario.drive(run), run.notes
        finally:
            self.last_evidence = run.evidence
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


    def test_s13_no_session_shell_means_no_panel_assertion(self):
        # r5 tmux-noshim Codex: every flow assertion passed and only the (absent) panel line made it a gap.
        for harness in ("claude", "codex", "opencode"):
            verdict, notes = self.run_flow(harness, shell=False, panels=False)
            self.assertEqual(verdict, "pass", (harness, notes))
        verdict, _notes = self.run_flow("codex", shell=False, panels=False)
        self.assertEqual(verdict, "pass")
        self.assertEqual(self.last_evidence["panel"], "not-applicable (no session shell)")
        # With the shell, the panel is still asserted.
        self.assertEqual(self.run_flow("codex", shell=True, panels=False)[0], "gap")
        self.assertNotIn("panel", self.last_evidence)


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


class HostRunR5Test(unittest.TestCase):
    """What host-run r5 showed in the scenarios themselves (S11~S13; S13 is in FlowVerdictTest)."""

    RQ = "rq-94a8a6"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def context(self, harness="claude"):
        run = SimpleNamespace(harness=harness, home=SimpleNamespace(workspace=Path(self.tmp.name), env={}))
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_address = "%1@/tmp/socket"
        return scenario, scenario.make_context(run)

    def test_s11_b_opens_and_reports_the_revision_2_line_it_already_received(self):
        scenario, ctx = self.context()
        nonce, rq = scenario.nonce, self.RQ
        line1 = (f"[요청 {rq}] [c1] → [a0] │ 판 검토 {nonce} │ 판 1 판1-{nonce} │ 본문 1줄 │ 열기: python3 "
                 f'"$AGENT_HOME/utilities/peer-steward.py" show {rq}\n\n(peer-from: claude [c1] ; ref=1)')
        line2 = line1.replace("판 1 판1-", "판 2 판2-")
        out1 = show_output(rq, 1, f"BODY-REV1-{nonce}")
        mark = "SCENARIO-WATCH-TARGET\n"
        # r5 Claude, in its real order. [3] revision 1's line -> show.
        t3 = cases.answer(ctx, record(mark + line1, line1), [])
        self.assertIn(f"show {rq}", t3.shell)
        # [4] the show output -> the busy step.
        text4 = mark + line1 + "\n" + out1
        t4 = cases.answer(ctx, record(text4, "toolu\ntool_result\n" + out1, "tool"),
                          [record(mark + line1, line1, reply={"shell": t3.shell}, turn=line1)])
        self.assertIn("time.sleep", t4.shell)
        earlier = [record(mark + line1, line1, reply={"shell": t3.shell}, turn=line1),
                   record(text4, out1, "tool", reply={"shell": t4.shell}, turn=out1)]
        # [9] `B-WORK-DONE` comes with the revision 2 line (a hook context, outside the turn's tool
        # result): the late report on revision 1, with revision 1's own command.
        text9 = text4 + f"\nB-WORK-DONE\nPostToolUse:Bash hook additional context: {line2}"
        t9 = cases.answer(ctx, record(text9, "toolu\ntool_result\nB-WORK-DONE", "tool", turn="B-WORK-DONE"), earlier)
        self.assertIn(f"report {rq} --rev 1 --result", t9.shell)
        earlier.append(record(text9, "B-WORK-DONE", "tool", reply={"shell": t9.shell}, turn="B-WORK-DONE"))
        # [10] the late report's result: nothing opens revision 2 yet -> now B shows the request.
        done = f"report={rq} kind=final revision=1 late=true state=open delivery=queued-not-delivered"
        text10 = text9 + "\n" + done
        t10 = cases.answer(ctx, record(text10, done, "tool", turn=done), earlier)
        self.assertEqual(t10.shell, f'{cases.STEWARD} show {rq}')
        earlier.append(record(text10, done, "tool", reply={"shell": t10.shell}, turn=done))
        # The show prints revision 2: B reports it with `--rev 2`.
        out2 = show_output(rq, 2, f"BODY-REV2-{nonce}")
        text11 = text10 + "\n" + out2
        t11 = cases.answer(ctx, record(text11, out2, "tool", turn=out2), earlier)
        self.assertIn(f"report {rq} --rev 2 --result", t11.shell)
        self.assertNotIn("--rev 1", t11.shell)
        earlier.append(record(text11, out2, "tool", reply={"shell": t11.shell}, turn=out2))
        # Revision 2's report done: nothing is left to open, B stops.
        done2 = f"report={rq} kind=final revision=2 late=false state=final"
        t12 = cases.answer(ctx, record(text11 + "\n" + done2, done2, "tool", turn=done2), earlier)
        self.assertFalse(t12.shell)

    def test_s11_a_report_with_no_unopened_revision_does_not_show_again(self):
        scenario, ctx = self.context()
        nonce, rq = scenario.nonce, self.RQ
        line1 = f"[요청 {rq}] [c1] → [a0] │ 판 검토 {nonce} │ 판 1 판1-{nonce} │ 본문 1줄 │ 열기: show {rq}"
        out1 = show_output(rq, 1, f"BODY-REV1-{nonce}")
        done = f"report={rq} kind=final revision=1 late=false state=final"
        reply = cases.answer(ctx, record("SCENARIO-WATCH-TARGET\n" + line1 + "\n" + out1 + "\n" + done, done, "tool",
                                         turn=done), [])
        self.assertFalse(reply.shell)

    def test_s11_the_unopened_revisions_are_read_from_the_lines_and_the_show_outputs(self):
        line = lambda rq, n: f"[요청 {rq}] [c1] → [a0] │ 판 검토 x │ 판 {n} 판{n}-x │ 열기: show {rq}"  # noqa: E731
        inputs = "\n".join([line("rq-aaaaaa", 1), line("rq-aaaaaa", 2), show_output("rq-aaaaaa", 1, "b"),
                            f"[요청 rq-bbbbbb] [c1] → [a0] │ 세번째 │ 열기: show rq-bbbbbb"])
        self.assertEqual(cases.pending_revisions(inputs), {"rq-aaaaaa": [2]})
        self.assertEqual(cases.pending_revisions(inputs + "\n" + show_output("rq-aaaaaa", 2, "c")), {})

    def test_s12_the_busy_step_of_codex_ends_before_its_shell_tool_returns(self):
        for harness, seconds in (("claude", 25), ("opencode", 25), ("codex", 8)):
            _scenario, ctx = self.context(harness)
            self.assertEqual(ctx["hold"], seconds, harness)
            out = show_output(self.RQ, 1, "BODY")
            busy = cases.answer(ctx, record("SCENARIO-WATCH-TARGET " + out, out, "tool"), [])
            self.assertIn(f"time.sleep({seconds})", busy.shell, harness)
        self.assertLess(8, 10)  # Codex's shell tool answers `Process running with session ID …` at 10s

    def run_busy(self, created, ended_at, *, with_times=True):
        scenario, _ctx = self.context()
        records = {"rq-111111": {"revisions": [{"created": 100.0}, {"created": created}]}}
        busy = {"text": "", "last_text": "", "turn_text": "", "main_index": 4, "last_kind": "tool",
                "reply": {"shell": "python3 -c time.sleep(25)"}, "at": 101.0}
        end = {"text": "", "last_text": "", "turn_text": "B-WORK-DONE", "main_index": 9, "last_kind": "tool",
               "reply": {}, "at": ended_at}
        scenario.records = lambda run: records
        scenario.b_requests = lambda run: [busy, end] if ended_at is not None else [busy]
        return scenario.sent_while_busy(SimpleNamespace(), "rq-111111")

    def test_s12_revision_2_after_the_busy_step_ended_is_inconclusive_not_a_failure(self):
        self.assertTrue(self.run_busy(created=110.0, ended_at=126.0))   # sent while B worked
        self.assertTrue(self.run_busy(created=110.0, ended_at=None))    # B still in the step
        self.assertFalse(self.run_busy(created=130.0, ended_at=126.0))  # sent after the step ended

    def drive_revision(self, created):
        scenario = cases.RequestRevisionCancelScenario()
        scenario.target_tui = SimpleNamespace(screen=lambda: "")
        scenario.target_address = "%1@/tmp/socket"
        first = "rq-111111"
        records = {first: {"state": "open", "revisions": [{"created": 100.0}, {"created": created}]}}
        busy = {"text": "", "last_text": "", "turn_text": "", "main_index": 4, "last_kind": "tool",
                "reply": {"shell": "python3 -c time.sleep(8)"}, "at": 101.0}
        end = {"text": "", "last_text": "", "turn_text": "Process running with session ID 1", "main_index": 9,
               "last_kind": "tool", "reply": {}, "at": 111.0}
        scenario.records = lambda run: records
        scenario.b_requests = lambda run: [busy, end]
        scenario.a_requests = lambda run: []
        scenario.start_a = lambda run, text: None
        run = SimpleNamespace(harness="codex", evidence={}, notes=[], main_requests=lambda: [],
                              note=lambda text: run.notes.append(text))
        saved = cases.wait_for, cases.time.sleep
        cases.wait_for, cases.time.sleep = (lambda predicate, timeout, step=0.5: predicate()), (lambda _s: None)
        try:
            return scenario.drive(run), run.notes
        finally:
            cases.wait_for, cases.time.sleep = saved

    def test_s12_drive_returns_inconclusive_with_a_note_when_revision_2_came_too_late(self):
        verdict, notes = self.drive_revision(created=120.0)
        self.assertEqual(verdict, "inconclusive")
        self.assertIn("late-report path was not exercised", " ".join(notes))


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
