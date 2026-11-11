#!/usr/bin/env python3
"""Turn-end follow-through and notice-once scenarios for ``harness_behavior_scenarios.py``.

They pair the follow-through pass (PG-44, PG-46) and the in-turn notice carrier
(PG-45) with the scenarios PG-32 asks for: what must happen once, and what must
not happen at all.

``peer-report-read-only``
    A verified peer report whose first line is ``[report]`` arrives (an idle
    wake, or the next prompt) and the model only reads it.  Pass: the turn ends
    once, with no follow-through continuation.  ``fail``: the session is asked
    again.

``peer-request-follow-through``
    The same verified peer message without a read-only first line, which the
    model answers without acting on it.  Pass: the session is asked again once
    and the action runs.  ``gap``: nothing asks again.  ``fail``: asked more
    than once.

``notice-mid-turn-once``
    An ordinary notice written during a shell step.  Pass: it reaches the model
    exactly once in the whole run (whatever the carrier) and its record is
    ACKed.  ``fail``: shown twice, or ACKed without ever being shown.

``notice-idle-once``
    The same notice written after the turn ended.  Pass: it reaches the model
    exactly once (a wake or the next prompt) and its record is ACKed.

Synthetic data stays under the fixture root: peer transfers through
``harness_behavior_cases_b.send_peer`` and notices through
``session_notice.notify``.
"""
from __future__ import annotations

from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
from harness_behavior_cases_a import _summary  # noqa: E402
from harness_behavior_cases_b import (fixture_python, long_shell, pending_state,  # noqa: E402
                                      send_peer, session_id)
from harness_behavior_cases_d import NOTICE_CODE, RECORD_CODE, _tool_result_index  # noqa: E402
from harness_behavior_cases_e import new_arrivals, prompt_until  # noqa: E402

PG = {"peer-report-read-only": "PG-46,PG-32", "peer-request-follow-through": "PG-44,PG-46",
      "notice-mid-turn-once": "PG-45,PG-32", "notice-idle-once": "PG-45,PG-32"}
NUDGE = "This is the only continuation for this turn"


def _nudges(requests, start=0):
    return [r["main_index"] for r in requests[start:] if NUDGE in r["last_text"]]


def _arrived_as_prompt(record) -> bool:
    text = record["last_text"]
    return "peer-from:" in text or "cross-session-message" in text


# --------------------------------------------------------------------------
# peer-report-read-only / peer-request-follow-through
# --------------------------------------------------------------------------

class PeerReportReadOnlyScenario(Scenario):
    name = "peer-report-read-only"
    summary = ("A verified [report] peer message arrives and is only read; the turn ends "
               "once with no follow-through continuation.")
    BODY = "[report] {token} worker finished task R-1; nothing else is needed."
    expect_nudge = False

    def __init__(self):
        self.token = "SCENARIO-PEER-RO-" + uuid.uuid4().hex[:8]
        self.next = "HB-PEER-NEXT-" + uuid.uuid4().hex[:6]

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            if kind == "tool" and self.next in last:
                return Reply("Step done.")
            if NUDGE in last and not any(self.next in (r["reply"].get("shell") or "")
                                         for r in earlier):
                return Reply(shell=f"echo {self.next}-$((20+22))")
            if self.token in last:
                # A weak model: it reads the message and answers without acting.
                return Reply(f"Read the message {self.token[-8:]}.")
            return Reply(f"Ready {record['main_index']}.")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-PEER turn one")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(6, 60)
        sid = session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        ev["send"] = send_peer(run, self.BODY.format(token=self.token))
        ev["flow"] = prompt_until(run, [self.token], 1, "SCENARIO-PEER anything new?")
        requests = run.main_requests()
        arrival = next((r for r in requests if self.token in r["text"]), None)
        if arrival is None:
            run.note("the peer message never reached the model")
            return "inconclusive"
        ev["arrival"] = _summary(arrival)
        ev["arrived_as_prompt"] = _arrived_as_prompt(arrival)
        time.sleep(20)
        run.wait_idle(6, 60)
        requests = run.main_requests()
        ev["requests_after_arrival"] = [_summary(r) for r in requests[arrival["main_index"] + 1:]]
        nudges = _nudges(requests, arrival["main_index"])
        ev["nudges"] = nudges
        ev["next_action_ran"] = any(f"{self.next}-42" in r["text"] for r in requests)
        if ev["send"].get("ref"):
            ev["pending_final"] = pending_state(run, ev["send"]["ref"])
        how = "as the prompt" if ev["arrived_as_prompt"] else "as context"
        run.note(f"message arrived {how}; asked again {len(nudges)} time(s)")
        return self.verdict(run, nudges)

    def verdict(self, run, nudges):
        extra = run.evidence["requests_after_arrival"]
        if nudges or extra:
            return "fail"
        return "pass"


class PeerRequestFollowThroughScenario(PeerReportReadOnlyScenario):
    name = "peer-request-follow-through"
    summary = ("A verified peer request without a read-only first line is answered without "
               "acting; the session is asked again once and acts.")
    BODY = "Please run the check {token} and tell me the result."

    def verdict(self, run, nudges):
        ev = run.evidence
        if len(nudges) > 1:
            return "fail"
        if not nudges:
            return "gap"
        return "pass" if ev["next_action_ran"] else "fail"


# --------------------------------------------------------------------------
# notice-mid-turn-once / notice-idle-once
# --------------------------------------------------------------------------

class NoticeMidTurnOnceScenario(Scenario):
    name = "notice-mid-turn-once"
    summary = ("An ordinary notice written during a shell step reaches the model exactly "
               "once and is ACKed (never twice, never ACKed unseen).")
    idle = False

    def __init__(self):
        nonce = uuid.uuid4().hex[:8]
        self.token = f"SCENARIO-NOTICE-ONCE-{nonce}"
        self.step = f"SCENARIO-NOTICE-ONCE-STEP-{nonce}"

    def script(self, run):
        def answer(record, earlier):
            shells = [r for r in earlier if r["reply"]["shell"]]
            if (not self.idle and not shells and record["last_kind"] != "tool"
                    and "SCENARIO-ONCE run" in record["text"]):
                return Reply(shell=long_shell(f"sleep 14; echo {self.step}"))
            return Reply(f"notice noted {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-ONCE run the step")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        sid = session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        if self.idle:
            run.wait_idle(6, 60)
        else:
            time.sleep(2)
        ev["notice"] = fixture_python(run, NOTICE_CODE, {
            "harness": run.harness, "sid": sid, "key": "once-" + self.token,
            "text": f"{self.token} worker finished task T-9"})
        if not self.idle:
            ev["tool_result_request"] = wait_for(lambda: _tool_result_index(run, self.step), 60)
        ev["flow"] = prompt_until(run, [self.token], 1, "SCENARIO-ONCE anything new?")
        # Anything still queued (a native queue, a courier) has time to show again.
        time.sleep(20)
        run.prompt("SCENARIO-ONCE last check")
        run.wait_idle(6, 60)
        requests = run.main_requests()
        arrivals = new_arrivals(requests, self.token)
        ev["arrivals"] = arrivals
        ev["records"] = fixture_python(run, RECORD_CODE)
        mine = [row for row in (ev["records"] or {}).get("records") or []
                if row.get("delivery_id") == (ev["notice"] or {}).get("delivery_id")]
        ev["record_state"] = mine[0].get("state") if mine else None
        run.note(f"notice shown {len(arrivals)} time(s); record {ev['record_state']}")
        if len(arrivals) > 1:
            return "fail"
        if not arrivals:
            return "fail" if ev["record_state"] == "acked" else "gap"
        return "pass" if ev["record_state"] == "acked" else "fail"


class NoticeIdleOnceScenario(NoticeMidTurnOnceScenario):
    name = "notice-idle-once"
    summary = ("An ordinary notice written after the turn ended reaches the model exactly "
               "once (wake or next prompt) and is ACKed.")
    idle = True


SCENARIOS = [PeerReportReadOnlyScenario, PeerRequestFollowThroughScenario,
             NoticeMidTurnOnceScenario, NoticeIdleOnceScenario]
