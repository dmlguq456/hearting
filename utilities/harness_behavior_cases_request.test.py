#!/usr/bin/env python3
"""Offline checks of the peer-request scenarios: registration and the scripted model.

The real-TUI measurement is the scenarios' own run (host-run). These tests run the
scripted model against synthetic request records so a script that could never produce
the specified flow fails here, not in a 40-minute measurement.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import harness_behavior_cases_request as cases
import harness_behavior_scenarios as hb

HERE = Path(__file__).resolve().parent


def record(text, last, kind="user", reply=None):
    return {"text": text, "last_text": last, "last_kind": kind, "reply": reply or {}, "main_index": 0}


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
        self.assertIn("sleep", busy.shell)
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


if __name__ == "__main__":
    unittest.main()
