#!/usr/bin/env python3
"""A question answered on the board while a question stands on the session's screen (11-27) —
``harness_behavior_scenarios.py``.

The person said: "질문을 띄운채로 있을때 하팅에서 답하면 계속 묶이는 문제가 있음". Each scenario puts
a question on the session's screen, answers it from another Hearting place (``questions answer``, the
path the panel and Fleet share: ``submit_selected_response``) and records what the session then does:
whether a native question window stood, whether the answer reached the model without a typed prompt
(and when), whether the question record closed, whether the session still counts a waiting question,
and whether the next typed line reaches the model as an ordinary prompt.

``question-bound-same``
    The model registers a Hearting question (``questions ask`` from its shell tool) and, in the same
    turn, asks the same question through the native question tool (the 11-27 scene).
``question-bound-other``
    The same, but the native call asks a different question. As on main it shows in a native window and
    the open question stays as it was (several open questions per session are a separate design); the
    board answer given then is recorded, not judged: it waits behind that window, the bind that remains.
``question-bound-popup``
    The model registers a Hearting question and ends its turn; the person opens the session shell's
    question popup (Ctrl+]) and the question is answered from another place while the popup is open.
    Needs ``--session-shell``.

Pass (same, popup): no native window stands beside the waiting question, the answer reaches the model once
without a typed prompt, the question is answered, nothing waits for this session, and the next typed line
is an ordinary prompt. Pass (other): the native window shows and the open question is kept. ``fail`` names
what did not happen.

    python3 utilities/harness_behavior_scenarios.py --scenario question-bound-same,question-bound-other \
        --harness claude,codex,opencode --session-shell --output-dir DIR [--source CHECKOUT]

Synthetic data stays under the fixture root; the model is the loopback fixture.
"""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_b import (  # noqa: E402
    NativeCall, OPTION_A, OPTION_B, QUESTION_TEXT, QuestionAnswerScenario, fixture_python,
    native_question, session_id)

QUESTION_ID = re.compile(r'"id":\s*"(S4:[0-9a-f]{64})"')
ASK = ('python3 "$AGENT_HOME/utilities/questions.py" ask --json --choice ' + OPTION_A
       + ' --choice ' + OPTION_B + ' "' + QUESTION_TEXT + '"')
OTHER_TEXT = "Which colour should the scenario paint?"
OTHER_A, OTHER_B = "Crimson-tone", "Cobalt-tone"
WAKE_SECONDS = 60          # how long an answer may take to reach a resting session without a prompt


def other_question() -> NativeCall:
    def build(protocol, names):
        options = [{"label": OTHER_A, "description": "a warm colour"},
                   {"label": OTHER_B, "description": "a cold colour"}]
        if protocol == "anthropic":
            return "AskUserQuestion", {"questions": [{"question": OTHER_TEXT, "header": "Colour",
                                                      "multiSelect": False, "options": options}]}
        if protocol == "responses":
            return "request_user_input", {"questions": [{"id": "colour", "header": "Colour",
                                                         "question": OTHER_TEXT, "options": options}]}
        return "question", {"questions": [{"question": OTHER_TEXT, "header": "Colour", "multiple": False,
                                           "options": options}]}
    return NativeCall("native-other-question", build)


STATE_CODE = r"""
import human_wait_state as waits
found = None
for path in sorted(waits.state_root().glob("*.json")):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(value, dict) and value.get("id") == args["id"]:
        found = value
waiting = [s["id"] for s in waits.read_signals() if s.get("mode") == "message"
           and s.get("session_id") == args["sid"]]
selected = waits.read_selected_request({"id": args["id"], "harness": args["harness"], "session_id": args["sid"]})
print(json.dumps({"lifecycle": (found or {}).get("lifecycle"),
                  "actor": ((found or {}).get("completion") or {}).get("actor"),
                  "waiting_for_this_session": waiting,
                  "request_state": (selected or {}).get("state")}))
"""


class QuestionBoundBase(Scenario):
    native = None            # the native call the model makes after `questions ask`, or None
    native_labels = ()       # words only that native window shows
    expects_window = False   # a different question: shown natively, as on main

    def prepare(self, run):
        QuestionAnswerScenario.prepare(self, run)   # Codex's Default-mode question tool, as a release enables it

    def script(self, run):
        native = self.native() if self.native else None

        def answer(record, earlier):
            asked = any(r["reply"]["shell"] == ASK for r in earlier)
            called = native is not None and any(r["reply"]["shell"] == str(native) for r in earlier)
            if not asked and record["last_kind"] != "tool" and "SCENARIO-QBOUND ask" in record["text"]:
                return Reply(text="I will ask the person.", shell=ASK)
            if asked and native is not None and not called and record["last_kind"] == "tool":
                return Reply(text="Asking in the native window too.", shell=native)
            if asked and record["last_kind"] == "tool":
                return Reply(text="QBOUND my turn ends here.")
            return Reply(f"qbound noted {record['main_index']}")
        return answer

    # -- observations ------------------------------------------------------
    @staticmethod
    def _question_id(run):
        for record in run.main_requests():
            if record["last_kind"] == "tool":
                found = QUESTION_ID.search(record.get("last_text") or "")
                if found:
                    return found.group(1)
        return None

    def _native_result(self, run):
        """The tool result the model got for the native call (its refusal), or None while it stands."""
        requests = run.main_requests()
        label = str(self.native()) if self.native else None
        for index, record in enumerate(requests):
            if record["reply"]["shell"] == label:
                for later in requests[index + 1:]:
                    if later["last_kind"] == "tool":
                        return later["last_text"][-400:]
        return None

    @staticmethod
    def _listed(run, qid):
        done = subprocess.run([sys.executable, str(Path(run.home.env["AGENT_HOME"]) / "utilities/questions.py"),
                               "list", "--json", "--all"], env=dict(run.home.env), cwd=str(run.home.workspace),
                              text=True, capture_output=True, timeout=60)
        return qid in (done.stdout or "")

    def _state(self, run, qid):
        return fixture_python(run, STATE_CODE, {"id": qid, "sid": run.evidence.get("session_id"),
                                                "harness": run.harness})

    def put_question_up(self, run):
        """Make the question stand on the session's screen; return False when it cannot be judged."""
        return True

    def _beside_window(self, run, qid):
        """A different question shows natively, as on main; what a board answer then does is only recorded."""
        ev = run.evidence
        shown = ev.get("native_refusal") is None and bool(
            wait_for(lambda: any(word in run.tui.screen() for word in self.native_labels), 20))
        ev["native_window_stood"] = shown
        ev["question_kept"] = kept = self._state(run, qid)
        if wait_for(lambda: self._listed(run, qid), 45):
            started = time.time()
            done = subprocess.run([sys.executable, str(Path(run.home.env["AGENT_HOME"]) / "utilities/questions.py"),
                                   "answer", qid, "--choice", "1", "--json"], env=dict(run.home.env),
                                  cwd=str(run.home.workspace), text=True, capture_output=True, timeout=60)
            before = len(run.main_requests()) if not done.returncode else None
            hit = before is not None and wait_for(lambda: any(
                OPTION_A in (r.get("turn_text") or "") for r in run.main_requests()[before:]), WAKE_SECONDS)
            # The bind that remains with two different questions: recorded, not judged here.
            ev["board_answer_reached_after_s"] = round(time.time() - started, 1) if hit else None
        run.note(f"native_window={shown} kept={kept.get('lifecycle')} "
                 f"board_answer_after_s={ev.get('board_answer_reached_after_s')}")
        if not shown:
            run.note("the different question was not shown natively")
            return "fail"
        if kept.get("lifecycle") != "open" or kept.get("waiting_for_this_session") != [qid]:
            run.note("the open question did not stay as it was")
            return "fail"
        return "pass"

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-QBOUND ask the person one question")
        if not run.wait_requests(1, 90):
            run.note("no first model request")
            return "inconclusive"
        ev["session_id"] = session_id(run)
        qid = wait_for(lambda: self._question_id(run), 90)
        ev["question_id"] = qid
        if not qid:
            run.note("the question was not registered from the session's shell")
            return "inconclusive"
        if self.native is not None:
            label = str(self.native())
            if not wait_for(lambda: any(r["reply"]["shell"] == label for r in run.main_requests()), 60):
                run.note("the model never made the native call")
                return "inconclusive"
            refusal = wait_for(lambda: self._native_result(run), 20)
            ev["native_refusal"] = refusal
            screen = run.tui.screen()
            ev["native_window_stood"] = refusal is None and any(word in screen for word in self.native_labels)
            ev["screen_after_native"] = screen[-1800:]
        if self.expects_window:
            return self._beside_window(run, qid)
        if not self.put_question_up(run):
            return "inconclusive"
        run.wait_idle(6, 60)
        # The board's own readiness: `questions answer` reads the board, which lists the question once collected.
        ev["listed_on_board"] = bool(wait_for(lambda: self._listed(run, qid), 45))
        before = len(run.main_requests())
        started = time.time()
        done = subprocess.run([sys.executable, str(Path(run.home.env["AGENT_HOME"]) / "utilities/questions.py"),
                               "answer", qid, "--choice", "1", "--json"],
                              env=dict(run.home.env), cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=60)
        ev["answer_exit"] = done.returncode
        ev["answer_result"] = (done.stdout or done.stderr)[-600:]
        if done.returncode:
            run.note("questions answer refused: " + ev["answer_result"])
            return "fail"

        def reached():
            # What reached the model since its last output (the whole text also carries the ask itself).
            return next((r["main_index"] for r in run.main_requests()[before:]
                         if r["last_kind"] != "tool" and OPTION_A in (r.get("turn_text") or "")), None)
        hit = wait_for(reached, WAKE_SECONDS)
        ev["answer_request"] = hit
        ev["answer_reached_after_s"] = round(time.time() - started, 1) if hit is not None else None
        run.wait_idle(6, 60)
        ev["state_after_answer"] = self._state(run, qid)
        ev["screen_after_answer"] = run.tui.screen()[-1800:]
        if self.native is not None:
            screen = run.tui.screen()
            ev["native_window_after_answer"] = any(word in screen for word in self.native_labels) \
                and self._native_result(run) is None
        # The next typed line: an ordinary prompt, never an answer to the question.
        at = len(run.main_requests())
        run.prompt("SCENARIO-QBOUND-NEXT an ordinary line")
        wait_for(lambda: any("SCENARIO-QBOUND-NEXT" in r["text"] for r in run.main_requests()[at:]), 30)
        run.wait_idle(6, 60)
        # Reached as what the person typed (after a tool result when it cut a native window short).
        ev["next_prompt_reached"] = any("SCENARIO-QBOUND-NEXT" in (r.get("turn_text") or "")
                                        for r in run.main_requests()[at:])
        ev["next_prompt_cut_native_call"] = any(r["last_kind"] == "tool" and "SCENARIO-QBOUND-NEXT"
                                                in (r.get("turn_text") or "") for r in run.main_requests()[at:])
        ev["state_after_next"] = self._state(run, qid)
        carrying = [r["main_index"] for r in run.main_requests()[before:]
                    if r["last_kind"] != "tool" and OPTION_A in (r.get("turn_text") or "")]
        ev["answer_carriers"] = carrying
        state = ev["state_after_answer"] or {}
        run.note(f"native_window={ev.get('native_window_stood')} answer_after_s={ev['answer_reached_after_s']} "
                 f"lifecycle={state.get('lifecycle')} waiting={len(state.get('waiting_for_this_session') or [])} "
                 f"next_prompt={ev['next_prompt_reached']}")
        if ev.get("native_window_stood"):
            run.note("a native question window stood beside the waiting Hearting question")
            return "fail"
        if hit is None:
            run.note("the answer did not reach the session without a typed prompt")
            return "fail"
        if state.get("lifecycle") != "answered" or state.get("waiting_for_this_session"):
            run.note("the question record did not close")
            return "fail"
        if not ev["next_prompt_reached"]:
            run.note("the next typed line did not reach the model as a prompt")
            return "fail"
        return "pass"


class QuestionBoundSameScenario(QuestionBoundBase):
    name = "question-bound-same"
    summary = ("`questions ask`, then the same question through the native tool in the same turn; the board "
               "answer must reach the session and no native window may stand beside it.")
    native = staticmethod(native_question)
    native_labels = ("first scenario branch", "second scenario branch")   # the native options' descriptions


class QuestionBoundOtherScenario(QuestionBoundBase):
    name = "question-bound-other"
    summary = ("`questions ask`, then a different question through the native tool in the same turn: shown "
               "natively as on main, the open question kept; the board answer behind that window is recorded.")
    native = staticmethod(other_question)
    native_labels = (OTHER_A, OTHER_B)
    expects_window = True


class QuestionBoundPopupScenario(QuestionBoundBase):
    name = "question-bound-popup"
    summary = ("The session shell's question popup is open on a waiting question that is answered from "
               "another place; the answer must reach the session and the popup must let go.")

    def put_question_up(self, run):
        ev = run.evidence
        run.wait_idle(6, 60)
        if not ev.get("session_shell") or ev["session_shell"].get("status") not in ("ready", "copied"):
            run.note("needs --session-shell with its assets")
            return False
        shown = None
        for _ in range(3):
            run.tui.key("C-]")
            shown = wait_for(lambda: QUESTION_TEXT[:30] in run.tui.screen()
                             and OPTION_B in run.tui.screen(), 10)
            if shown:
                break
        ev["popup_open"] = bool(shown)
        ev["screen_popup"] = run.tui.screen()[-1800:]
        if not shown:
            run.note("the question popup never opened")
            return False
        return True


SCENARIOS = (QuestionBoundSameScenario, QuestionBoundOtherScenario, QuestionBoundPopupScenario)
