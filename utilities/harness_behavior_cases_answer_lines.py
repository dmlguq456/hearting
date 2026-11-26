#!/usr/bin/env python3
"""A written answer of several lines reaches the asking session as written (11-26) — ``harness_behavior_scenarios.py``.

``answer-lines``
    The model asks the person through Hearting (``questions ask`` from its own shell tool, the way a session asks).
    The person's answer is several lines — an empty line, a line led by a tab, Korean, one line of 900 bytes — and goes
    the existing way (``questions answer --text-file``). Pass: a model request carries the whole answer exactly as
    written, the question is answered, and the delivery's record is settled. ``fail``: the answer arrived changed
    (its lines joined, its tab or empty line lost) or not at all.

    python3 utilities/harness_behavior_scenarios.py --scenario answer-lines \
        --harness claude,codex,opencode --output-dir DIR [--session-shell]

Synthetic data stays under the fixture root; the model is the loopback fixture.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_b import fixture_python, session_id  # noqa: E402
from harness_behavior_cases_d import RECORD_CODE  # noqa: E402
from harness_behavior_cases_e import prompt_until  # noqa: E402

QUESTION_ID = re.compile(r'"id":\s*"([^"\s]+)"')


class AnswerLinesScenario(Scenario):
    name = "answer-lines"
    summary = ("A written answer of several lines (an empty line, a tab, Korean, a long line) reaches the "
               "asking session exactly as written, once, and the question is answered.")

    def __init__(self):
        self.token = "SCENARIO-ANSWER-LINES-" + uuid.uuid4().hex[:8]
        self.answer = (self.token + " 첫 줄입니다\n\n\t탭으로 시작하는 줄\n한글 줄: 가나다 라마바\n"
                       + "긴" * 300 + "\n끝 줄")

    def script(self, run):
        def answer(record, earlier):
            asked = any(r["reply"]["shell"] for r in earlier)
            if not asked and record["last_kind"] != "tool" and "SCENARIO-ANSWER-LINES ask" in record["text"]:
                return Reply(text="I will ask the person.",
                             shell='python3 "$AGENT_HOME/utilities/questions.py" ask --json '
                                   '"여러 줄로 답해 주세요: 무엇을 고칠까요?"')
            return Reply(f"answer-lines noted {record['main_index']}")
        return answer

    def _question_id(self, run):
        for record in run.main_requests():
            if record["last_kind"] == "tool":
                found = QUESTION_ID.search(record.get("last_text") or "")
                if found:
                    return found.group(1)
        return None

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-ANSWER-LINES ask the person one question")
        if not run.wait_requests(1, 90):
            run.note("no first model request")
            return "inconclusive"
        ev["session_id"] = session_id(run)
        qid = wait_for(lambda: self._question_id(run), 90)
        ev["question_id"] = qid
        if not qid:
            tool = [r.get("last_text", "")[-600:] for r in run.main_requests() if r["last_kind"] == "tool"]
            ev["tool_results"] = tool
            run.note("the question was not registered from the session's shell")
            return "inconclusive"
        run.wait_idle(6, 90)
        answer_file = Path(run.home.workspace) / "answer-lines.txt"
        answer_file.write_text(self.answer, encoding="utf-8")
        done = subprocess.run([sys.executable, str(Path(run.home.env["AGENT_HOME"]) / "utilities/questions.py"),
                               "answer", qid, "--text-file", str(answer_file), "--json"],
                              env=dict(run.home.env), cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=60)
        ev["answer_exit"] = done.returncode
        ev["answer_result"] = (done.stdout or done.stderr)[-600:]
        if done.returncode:
            run.note("questions answer refused: " + ev["answer_result"])
            return "fail"
        ev["flow"] = prompt_until(run, [self.token], 2, "SCENARIO-ANSWER-LINES anything new?")
        requests = run.main_requests()
        carrying = [r["main_index"] for r in requests if self.token in r["text"]]
        exact = [r["main_index"] for r in requests if self.answer in r["text"]]
        flat = " ".join(self.answer.split())
        joined = [r["main_index"] for r in requests if flat in " ".join(r["text"].split())]
        ev.update(carrying=carrying, exact=exact, flattened_only=sorted(set(joined) - set(exact)))
        records = fixture_python(run, RECORD_CODE).get("records") or []
        ev["records"] = [row for row in records if str(row.get("delivery_id", "")).startswith("delivery-")]
        ev["question_after"] = fixture_python(run, QUESTION_CODE, {"id": qid})
        run.note(f"carrying={carrying} exact={exact} lifecycle={ev['question_after'].get('lifecycle')}")
        if not carrying:
            return "fail"
        if not exact:
            run.note("the answer arrived changed")
            return "fail"
        return "pass" if ev["question_after"].get("lifecycle") == "answered" else "fail"


QUESTION_CODE = r"""
import human_wait_state as waits
found = None
for path in sorted(waits.state_root().glob("*.json")):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(value, dict) and value.get("id") == args["id"]:
        found = value
print(json.dumps({"lifecycle": (found or {}).get("lifecycle"),
                  "actor": ((found or {}).get("completion") or {}).get("actor")}))
"""

SCENARIOS = (AnswerLinesScenario,)
