#!/usr/bin/env python3
"""The question popup lets go when its question is answered elsewhere (11-27) — ``harness_behavior_scenarios.py``.

``question-bound-popup``
    The model registers a Hearting question (``questions ask`` from its shell tool) and ends its turn. The
    person opens the session shell's question popup (Ctrl+]) and the question is answered from another place
    while the popup is open (``questions answer``, the path the panel and Fleet share). Before: the empty popup
    kept the keys and the shell gave the session nothing, so on Claude the answer never arrived. Pass: the
    answer reaches the model exactly once without a typed prompt (a new person-role input, its delivery record
    acked), the question record closes, nothing waits for this session, and the next typed line is an ordinary
    prompt. Needs ``--session-shell``.

    python3 utilities/harness_behavior_scenarios.py --scenario question-bound-popup \
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
from harness_behavior_cases_b import OPTION_A, OPTION_B, QUESTION_TEXT, fixture_python, session_id  # noqa: E402

QUESTION_ID = re.compile(r'"id":\s*"(S4:[0-9a-f]{64})"')
ASK = ('python3 "$AGENT_HOME/utilities/questions.py" ask --json --choice ' + OPTION_A
       + ' --choice ' + OPTION_B + ' "' + QUESTION_TEXT + '"')
END_REPLY = "QBOUND my turn ends here."
WAKE_SECONDS = 60          # how long an answer may take to reach a resting session without a prompt

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
print(json.dumps({"lifecycle": (found or {}).get("lifecycle"), "waiting_for_this_session": waiting}))
"""

# The answer's own delivery record (session_notice key `question-answer:<id>`): acked once the session took it.
DELIVERY_CODE = r"""
import hashlib
from pathlib import Path
root = Path(os.environ["AGENT_DISPATCH_JOBS"]).parent
want = "delivery-" + hashlib.sha256(("notice\0question-answer:" + args["id"]).encode()).hexdigest()
rows = []
for path in sorted(root.glob("pending-delivery/*/" + want + ".json")):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    rows.append({"state": data.get("state")})
print(json.dumps({"records": rows}))
"""


def answer_carriers(requests, before):
    """The requests, from `before` on, that bring the answer as a new person-role input, whatever the request's
    last item is (a tool output or an earlier input repeating the answer does not count)."""
    found = []
    for index in range(max(before, 1), len(requests)):
        earlier = [row["text"] for row in requests[index - 1].get("user_inputs") or []]
        now = [row["text"] for row in requests[index].get("user_inputs") or []]
        new = now[len(earlier):] if now[:len(earlier)] == earlier else [text for text in now if text not in earlier]
        if any(OPTION_A in text for text in new):
            found.append(requests[index]["main_index"])
    return found


class QuestionBoundPopupScenario(Scenario):
    name = "question-bound-popup"
    summary = ("The session shell's question popup is open on a waiting question that is answered from "
               "another place; the answer must reach the session once and the popup must let go.")

    def script(self, run):
        def answer(record, earlier):
            asked = any(r["reply"]["shell"] == ASK for r in earlier)
            if not asked and record["last_kind"] != "tool" and "SCENARIO-QBOUND ask" in record["text"]:
                return Reply(text="I will ask the person.", shell=ASK)
            if asked and record["last_kind"] == "tool":
                return Reply(text=END_REPLY)
            return Reply(f"qbound noted {record['main_index']}")
        return answer

    @staticmethod
    def _question_id(run):
        for record in run.main_requests():
            if record["last_kind"] == "tool":
                found = QUESTION_ID.search(record.get("last_text") or "")
                if found:
                    return found.group(1)
        return None

    @staticmethod
    def _questions(run, *args):
        return subprocess.run([sys.executable, str(Path(run.home.env["AGENT_HOME"]) / "utilities/questions.py"), *args],
                              env=dict(run.home.env), cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=60)

    def _state(self, run, qid):
        return fixture_python(run, STATE_CODE, {"id": qid, "sid": run.evidence.get("session_id")})

    def _delivered(self, run, qid):
        records = (fixture_python(run, DELIVERY_CODE, {"id": qid}) or {}).get("records") or []
        return records if records and all(row.get("state") == "acked" for row in records) else None

    def drive(self, run):
        ev = run.evidence
        if not ev.get("session_shell") or ev["session_shell"].get("status") not in ("ready", "copied"):
            run.note("needs --session-shell with its assets")
            return "inconclusive"
        run.prompt("SCENARIO-QBOUND ask the person one question")
        if not run.wait_requests(1, 90):
            run.note("no first model request")
            return "inconclusive"
        ev["session_id"] = session_id(run)
        qid = wait_for(lambda: self._question_id(run), 90)
        ev["question_id"] = qid
        if not qid or not wait_for(lambda: any(r["reply"]["text"] == END_REPLY for r in run.main_requests()), 60):
            run.note("the question was not registered, or the turn never reached its last reply")
            return "inconclusive"
        shown = None
        for _ in range(3):
            run.tui.key("C-]")
            shown = wait_for(lambda: QUESTION_TEXT[:30] in run.tui.screen() and OPTION_B in run.tui.screen(), 10)
            if shown:
                break
        ev["popup_open"] = bool(shown)
        if not shown:
            run.note("the question popup never opened")
            return "inconclusive"
        # The board's own readiness: `questions answer` reads the board, which lists the question once collected.
        if not wait_for(lambda: qid in (self._questions(run, "list", "--json", "--all").stdout or ""), 45):
            run.note("the board never listed the question")
            return "inconclusive"
        before, started = len(run.main_requests()), time.time()
        done = self._questions(run, "answer", qid, "--choice", "1", "--json")
        ev["answer_exit"] = done.returncode
        if done.returncode:
            run.note("questions answer refused: " + (done.stdout or done.stderr)[-300:])
            return "fail"
        hit = wait_for(lambda: (answer_carriers(run.main_requests(), before) or [None])[0], WAKE_SECONDS)
        ev["answer_reached_after_s"] = round(time.time() - started, 1) if hit is not None else None
        # Readiness, not a quiet spell: the delivery record is acked and the question record closed.
        ev["delivery"] = wait_for(lambda: self._delivered(run, qid), 30) if hit is not None else None
        ev["state_after_answer"] = state = wait_for(lambda: (lambda value: value if value.get("lifecycle") == "answered"
                                                             else None)(self._state(run, qid)), 30) or self._state(run, qid)
        at = len(run.main_requests())
        run.prompt("SCENARIO-QBOUND-NEXT an ordinary line")
        wait_for(lambda: any("SCENARIO-QBOUND-NEXT" in (r.get("turn_text") or "") for r in run.main_requests()[at:]), 30)
        ev["next_prompt_reached"] = any(r["last_kind"] != "tool" and "SCENARIO-QBOUND-NEXT" in (r.get("turn_text") or "")
                                        for r in run.main_requests()[at:])
        ev["answer_carriers"] = answer_carriers(run.main_requests(), before)
        run.note(f"answer_after_s={ev['answer_reached_after_s']} carriers={len(ev['answer_carriers'])} "
                 f"lifecycle={state.get('lifecycle')} waiting={len(state.get('waiting_for_this_session') or [])} "
                 f"next_prompt={ev['next_prompt_reached']}")
        if hit is None:
            run.note("the answer did not reach the session without a typed prompt")
            return "fail"
        if len(ev["answer_carriers"]) != 1 or not ev["delivery"]:
            run.note("the answer did not reach the session exactly once")
            return "fail"
        if state.get("lifecycle") != "answered" or state.get("waiting_for_this_session"):
            run.note("the question record did not close")
            return "fail"
        if not ev["next_prompt_reached"]:
            run.note("the next typed line did not reach the model as an ordinary prompt")
            return "fail"
        return "pass"


SCENARIOS = (QuestionBoundPopupScenario,)
