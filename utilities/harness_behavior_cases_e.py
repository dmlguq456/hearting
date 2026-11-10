#!/usr/bin/env python3
"""Behaviour scenarios D: the four axes the delivery scenarios did not measure.

Loaded by ``harness_behavior_scenarios.py`` (``_load_extra``).  Each scenario runs on
the real Claude, Codex and OpenCode in the base harness's throwaway HOME with the
loopback model; the oracle is what reached the model plus the Hearting state the
hooks and carriers wrote.  Register rows (``core/ADAPTATION.md`` §10) are named per
scenario in ``PG``.

Loss after a burst, a truncation or an error (PG-31)
    ``report-burst``          20 notices land at once, more than one carrier budget
                              holds; every notice recorded delivered must have
                              reached the model.
    ``report-long``           one 8,000-character peer report; its tail must reach
                              the model, or the report must not be recorded received.
    ``report-after-error``    a report becomes pending while a tool that ends in
                              error runs; it must not be recorded received unseen.
    ``subsession-completion`` a native sub-session (Claude ``Agent``, Codex
                              ``spawn_agent``, OpenCode ``subagent``) registers a
                              child; its completion must wake someone.

Continuation or injection that must not happen (PG-32)
    ``quoted-peer-from``      a file the model reads quotes ``peer-from:`` and
                              ``runtime-from:``; the turn must end once.
    ``foreign-report``        a report addressed to another session must never reach
                              this one; the own report arrives exactly once.
    ``completion-notice-same-root``  a completion this session cannot ACK and an
                              independent notice share one root; the notice must
                              still arrive in the same turn.

Real Herdr pane injection (PG-33, PG-25)
    ``herdr-peer-prompt``     the TUI runs in a pane of a private Herdr server
                              (Claude prompt suggestions on); ``peer-steward
                              prompt`` must reach the model, Herdr must detect the
                              agent, and ``herdr agent wait`` must return.

Owner execution posture (PG-34)
    ``owner-publish``         the realized registered-owner posture (Codex: the
                              permission profile and network grant the dispatch
                              wrapper computes; Claude/OpenCode: their headless
                              posture) runs a task-branch push to a loopback git
                              daemon and a loopback network step.

Native custom agents (PG-28)
    ``custom-agent-spawn``    the model spawns Hearting's ``memory-scout`` custom
                              agent in the TUI and headless; discovery, the
                              sub-agent's shell step without an approval prompt,
                              and the result reaching the parent are measured.

Synthetic data stays under the fixture root.  Herdr runs only as a private server
whose config, socket and session name live under a fresh ``/tmp`` directory; the
operator's Herdr server, tmux server and shared services are never addressed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_behavior_scenarios as base  # noqa: E402
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402
import harness_behavior_cases_a as ca  # noqa: E402
import harness_behavior_cases_b as cb  # noqa: E402

def _base():
    """The running harness module (``__main__`` when run as a script) for its globals."""
    main = sys.modules.get("__main__")
    return main if hasattr(main, "Run") and hasattr(main, "SOURCE") else base


# Register rows each scenario measures (core/ADAPTATION.md §10).
PG = {
    "report-burst": "PG-31", "report-long": "PG-31", "report-after-error": "PG-31",
    "subsession-completion": "PG-31", "quoted-peer-from": "PG-32",
    "foreign-report": "PG-32", "completion-notice-same-root": "PG-32",
    "herdr-peer-prompt": "PG-33,PG-25", "owner-publish": "PG-34",
    "custom-agent-spawn": "PG-28",
}


# --------------------------------------------------------------------------
# Native calls the base fixture does not know
# --------------------------------------------------------------------------

NAMESPACE_SEP = "::"


def _patch_namespaces(module):
    """Emit a Responses ``function_call`` for a namespaced tool (Codex ``multi_agent_v1``).

    A NativeCall names such a tool ``<namespace>::<name>``; the encoded call carries
    ``name`` and ``namespace`` separately, as the Responses API does.
    """
    fixture = getattr(module, "ModelFixture", None)
    if fixture is None or getattr(fixture, "_cases_e_namespaces", False):
        return
    original = fixture._responses

    def responses(self, reply, names):
        body, ctype = original(self, reply, names)
        return split_namespaced(body), ctype
    fixture._responses = responses
    fixture._cases_e_namespaces = True


def split_namespaced(body: bytes) -> bytes:
    def fix(match):
        namespace, name = match.group(1).decode(), match.group(2).decode()
        return f'"name":"{name}","namespace":"{namespace}"'.encode()
    return re.sub(rb'"name":"([A-Za-z0-9_]+)' + NAMESPACE_SEP.encode() + rb'([A-Za-z0-9_]+)"',
                  fix, body)


for _module in list(sys.modules.values()):
    if _module is not None and hasattr(_module, "ModelFixture"):
        _patch_namespaces(_module)


def spawn_call(agent: str, prompt: str) -> cb.NativeCall:
    """The harness's native sub-agent tool with ``agent`` as the agent type."""
    def build(protocol, names):
        if protocol == "anthropic":
            return "Agent", {"description": "scenario subtask", "prompt": prompt,
                             "subagent_type": agent}
        if protocol == "responses":
            return f"multi_agent_v1{NAMESPACE_SEP}spawn_agent", {"agent_type": agent,
                                                                  "message": prompt}
        return "subagent", {"agent": agent, "description": "scenario subtask", "prompt": prompt}
    return cb.NativeCall(f"spawn:{agent}", build)


def wait_call(agent_id: str) -> cb.NativeCall:
    """Codex ``wait_agent`` for one spawned agent (the parent waits for its result)."""
    def build(protocol, names):
        return f"multi_agent_v1{NAMESPACE_SEP}wait_agent", {"targets": [agent_id],
                                                             "timeout_ms": 90000}
    return cb.NativeCall(f"wait:{agent_id}", build)


MISSING_FILE = "hb-missing-input.txt"


def error_tool(workspace: Path) -> cb.NativeCall:
    """A tool call that ends in error: a read of a missing file in the workspace.

    Claude and OpenCode report it as a tool error; Codex has no read tool and no
    error status, so its closest call is a shell read that exits non-zero.
    """
    missing = str(workspace / MISSING_FILE)

    def build(protocol, names):
        if protocol == "anthropic":
            return "Read", {"file_path": missing}
        if protocol == "chat":
            return "read", {"filePath": missing}
        return cb._plain_shell(protocol, names, f"cat {shlex.quote(missing)}")
    return cb.NativeCall("error-tool", build)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

NOTIFY_CODE = r"""
import session_notice, dispatch_pending_delivery as pd
from pathlib import Path
out = []
for item in args["items"]:
    record = session_notice.notify(args["harness"], args["sid"], key=item["key"],
                                   subject=item["subject"], text=item["text"])
    out.append(None if record is None else record.get("delivery_id"))
print(json.dumps({"delivery_ids": out}))
"""

NOTICE_STATE_CODE = r"""
import dispatch_pending_delivery as pd, os
from pathlib import Path
root = Path(os.environ["AGENT_DISPATCH_JOBS"]).parent
states = []
for delivery_id in args["ids"]:
    record = pd.read(root, args["sid"], delivery_id) if delivery_id else None
    states.append(None if record is None else record.get("state"))
print(json.dumps({"states": states}))
"""


def notify(run, sid: str, items: list[dict]) -> list[str | None]:
    result = cb.fixture_python(run, NOTIFY_CODE, {"harness": run.harness, "sid": sid,
                                                   "items": items})
    run.evidence.setdefault("notify_raw", []).append({k: v for k, v in result.items()
                                                       if k != "delivery_ids"})
    return result.get("delivery_ids") or []


def notice_states(run, sid: str, ids: list) -> list:
    return cb.fixture_python(run, NOTICE_STATE_CODE, {"sid": sid, "ids": ids}).get("states") or []


def new_arrivals(requests, marker: str) -> list[int]:
    """Indexes of requests that carry *more* copies of ``marker`` than the one before.

    History repeats earlier content in every later request; a rise is a new
    injection.  Used to assert a report arrives exactly once.
    """
    arrivals, previous = [], 0
    for record in requests:
        count = record["text"].count(marker)
        if count > previous:
            arrivals.append(record["main_index"])
        previous = max(previous, count)
    return arrivals


def seen_markers(requests, markers, start: int = 0) -> set:
    found = set()
    for record in requests[start:]:
        for marker in markers:
            if marker in record["text"]:
                found.add(marker)
    return found


def prompt_until(run, markers, prompts: int, typed: str, wake: float = 40) -> dict:
    """Wait for ``markers``: first without a prompt, then with up to ``prompts`` typed ones."""
    before = len(run.main_requests())
    wait_for(lambda: seen_markers(run.main_requests(), markers, before), wake)
    run.wait_idle(6, 60)
    rounds = []
    woken = sorted(seen_markers(run.main_requests(), markers, before))
    for number in range(prompts):
        if seen_markers(run.main_requests(), markers, before) >= set(markers):
            break
        at = len(run.main_requests())
        run.prompt(f"{typed} {number + 1}")
        run.wait_idle(6, 60)
        wait_for(lambda: len(seen_markers(run.main_requests(), markers, at))
                 > 0 or False, 10)
        rounds.append(len(seen_markers(run.main_requests(), markers, at)))
    return {"before": before, "woken_without_prompt": len(woken), "typed_rounds": rounds}


def _flat(text: str) -> str:
    return " ".join(text.split())


def seen_bodies(requests, bodies, start: int = 0) -> set:
    """Indexes of ``bodies`` whose whole text (whitespace-normalised) reached one request.

    A marker alone is not delivery: a preview that keeps only each notice's tail
    must not count as the notice reaching the model.
    """
    wanted = [_flat(body) for body in bodies]
    found = set()
    for record in requests[start:]:
        text = _flat(record["text"])
        for index, body in enumerate(wanted):
            if body in text:
                found.add(index)
    return found


def _is_sub(record, main_marker: str, sub_marker: str) -> bool:
    return sub_marker in record["text"] and main_marker not in record["text"]


# --------------------------------------------------------------------------
# PG-31: loss after a burst, a truncation, an error, a sub-session
# --------------------------------------------------------------------------

class ReportBurstScenario(Scenario):
    name = "report-burst"
    summary = ("20 notices at once: every notice recorded delivered reached the model "
               "with its whole body, not only its tail marker.")
    carrier = "parent_completion"
    COUNT = 20

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.tails = [f"HB-TAIL-{self.nonce}-{i:02d}" for i in range(self.COUNT)]

    def prepare(self, run):
        ca._install_helper(run, "att-hbfx-unused")

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-BURST turn one")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(6, 60)
        sid = cb.session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        filler = ("worker progress line " * 40)[:520]
        items = [{"key": f"hb-burst-{self.nonce}-{i}", "subject": f"burst {i}",
                  "text": f"HB-BURST-{self.nonce}-{i:02d} {filler} {tail}"}
                 for i, tail in enumerate(self.tails)]
        ids = notify(run, sid, items)
        ev["notified"] = sum(1 for value in ids if value)
        if not ev["notified"]:
            run.note("no notice could be stored for this harness")
            return "inconclusive"
        ev["flow"] = prompt_until(run, self.tails, 3, "SCENARIO-BURST anything new?")
        states = notice_states(run, sid, ids)
        seen = seen_markers(run.main_requests(), self.tails, ev["flow"]["before"])
        whole = seen_bodies(run.main_requests(), [item["text"] for item in items],
                            ev["flow"]["before"])
        ev["states"] = {state: states.count(state) for state in set(states)}
        ev["seen"] = len(seen)
        ev["bodies_seen"] = len(whole)
        ev["tail_without_body"] = [i for i, tail in enumerate(self.tails)
                                   if tail in seen and i not in whole]
        lost = [i for i, state in enumerate(states) if state == "acked" and i not in whole]
        ev["acked_unseen"] = lost
        ev["spilled_to_file"] = any(
            ("persisted-output" in r["text"] or "Full hook output saved to" in r["text"])
            and f"HB-BURST-{self.nonce}" in r["text"]
            for r in run.main_requests()[ev["flow"]["before"]:])
        undelivered = [i for i in range(self.COUNT) if i not in whole]
        run.note(f"seen {len(seen)}/{self.COUNT} tails, {len(whole)}/{self.COUNT} whole bodies; "
                 f"states {ev['states']}; "
                 f"woken without prompt {ev['flow']['woken_without_prompt']}")
        if lost:
            run.note(f"{len(lost)} notices recorded acked but never reached the model whole"
                     + ("; the native hook output limit saved the rest to a file and showed "
                        "a preview" if ev["spilled_to_file"] else ""))
            return "fail"
        if not undelivered:
            return "pass"
        return "gap" if len(seen) == 0 and not whole else "fail"


class ReportLongScenario(Scenario):
    name = "report-long"
    summary = "One 8,000-character peer report: its tail reaches the model or it stays unreceived."
    carrier = "parent_completion"
    LENGTH = 8000

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.head = f"HB-LONG-HEAD-{self.nonce}"
        self.tail = f"HB-LONG-TAIL-{self.nonce}"

    def body(self) -> str:
        lead = f"[report] {self.head} long worker report follows. "
        end = f" {self.tail} end of report."
        line = "finding: the scenario fills this report to its measured length. "
        filler = (line * (self.LENGTH // len(line) + 1))[:self.LENGTH - len(lead) - len(end)]
        return lead + filler + end

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-LONG turn one")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(6, 60)
        sid = cb.session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        body = self.body()
        ev["body_chars"] = len(body)
        ev["send"] = cb.send_peer(run, body)
        ref = ev["send"].get("ref")
        if not ref:
            run.note("the peer writer stored no report")
            return "inconclusive"
        ev["flow"] = prompt_until(run, [self.head], 2, "SCENARIO-LONG anything new?")
        start = ev["flow"]["before"]
        requests = run.main_requests()
        head = seen_markers(requests, [self.head], start)
        tail = seen_markers(requests, [self.tail], start)
        delivered = next((r for r in requests[start:] if self.head in r["text"]), None)
        ev["head_seen"], ev["tail_seen"] = bool(head), bool(tail)
        if delivered:
            segment = delivered["text"][delivered["text"].find(self.head):]
            ev["delivered_chars_from_head"] = len(segment.split(self.tail)[0]) if tail \
                else len(segment)
        ev["pending_final"] = cb.pending_state(run, ref)
        state = ev["pending_final"].get("state")
        run.note(f"head={'seen' if head else 'unseen'} tail={'seen' if tail else 'unseen'} "
                 f"state={state}")
        if head and tail:
            return "pass"
        if state == "received":
            run.note("recorded received while the model never saw the whole report")
            return "fail"
        return "gap" if not head else "fail"


class ReportAfterErrorScenario(Scenario):
    name = "report-after-error"
    summary = "A report pending while a tool ends in error is never recorded received unseen."
    carrier = "parent_completion"

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.marker = f"HB-ERRTOOL-REPORT-{self.nonce}"

    def script(self, run):
        def answer(record, earlier):
            if record["last_kind"] == "tool":
                return Reply(f"error tool returned {record['main_index']}")
            if "SCENARIO-ERRTOOL go" in record["last_text"] and not run.evidence.get("send"):
                sid = cb.session_id(run, timeout=10)
                if sid:
                    # Pending before the error tool starts: its end is the first boundary.
                    run.evidence["send"] = cb.send_peer(
                        run, f"[report] {self.marker} worker finished task E-1")
                return Reply(shell=error_tool(run.home.workspace))
            return Reply(f"noted {record['main_index']}")
        return answer

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-ERRTOOL go")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(8, 90)
        ref = (ev.get("send") or {}).get("ref")
        if not ref:
            run.note("no report was stored before the error tool")
            return "inconclusive"
        requests = run.main_requests()
        tool_result = next((r for r in requests if r["last_kind"] == "tool"), None)
        ev["error_tool_result"] = ca._summary(tool_result) if tool_result else None
        ev["pending_after_turn"] = cb.pending_state(run, ref)
        at_boundary = bool(tool_result and self.marker in tool_result["text"])
        in_turn = cb.first_with(run, self.marker)
        ev["delivered_at_error_boundary"] = at_boundary
        ev["delivered_in_turn_request"] = in_turn
        state_after = ev["pending_after_turn"].get("state")
        if in_turn is None and state_after == "received":
            run.note("recorded received after the error tool, never seen by the model")
            return "fail"
        if in_turn is None:
            count = len(run.main_requests())
            run.prompt("SCENARIO-ERRTOOL anything new?")
            run.wait_idle(6, 60)
            wait_for(lambda: cb.first_with(run, self.marker, count) is not None, 20)
            later = cb.first_with(run, self.marker, count)
            ev["delivered_next_prompt"] = later
            ev["pending_final"] = cb.pending_state(run, ref)
            run.note("not delivered at the error boundary or later in the turn; "
                     + ("delivered at the next prompt (no loss, no carrier after a failed tool)"
                        if later is not None else "not delivered at the next prompt either"))
            return "gap" if later is not None else "fail"
        run.note(f"delivered {'at the error-tool boundary' if at_boundary else 'later in the turn'}"
                 f" (request #{in_turn}); state {state_after}")
        return "pass"


class SubsessionCompletionScenario(Scenario):
    name = "subsession-completion"
    summary = ("A native sub-session registers a child; its completion reaches the root "
               "session, which runs the next action.")
    carrier = "parent_completion"
    # A built-in general agent on each harness: the sub-session is measured, not a
    # custom agent's model pin (custom agents are custom-agent-spawn, PG-28).
    AGENTS = {"claude": "general-purpose", "codex": "default", "opencode": "general"}
    NEXT = "HB-SUB-NEXT"

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.attempt = "att-hbfx-" + uuid.uuid4().hex[:20]
        self.main = f"SCENARIO-SUB-MAIN-{self.nonce}"
        self.sub = f"SCENARIO-SUBTASK-{self.nonce}"

    def script(self, run):
        def answer(record, earlier):
            text, kind = record["text"], record["last_kind"]
            if _is_sub(record, self.main, self.sub):
                if kind == "tool":
                    return Reply("sub-task registered the child; done.")
                if not any("hb_fx.py" in (r["reply"].get("shell") or "") for r in earlier):
                    return Reply(shell=ca._shell(run, "register"))
                return Reply("sub-task done.")
            done_next = any(self.NEXT in (r["reply"].get("shell") or "") for r in earlier)
            if self.attempt in text and not done_next and \
                    text.count(self.attempt) > max((r["text"].count(self.attempt)
                                                    for r in earlier), default=0):
                return Reply(shell=f"echo {self.NEXT}-$((20+22))")
            if kind == "tool":
                return Reply("Sub-task started; I will continue when it reports.")
            if self.main in record["last_text"] and not any(
                    str(r["reply"].get("shell") or "").startswith("spawn:") for r in earlier):
                return Reply(shell=spawn_call(self.AGENTS[run.harness],
                                              f"{self.sub} register the child"))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        ca._install_helper(run, self.attempt)

    def drive(self, run):
        ev = run.evidence
        run.prompt(f"{self.main} start the sub-task")
        run.wait_idle(10, 150)
        requests = run.main_requests()
        ev["sub_requests"] = sum(1 for r in requests if _is_sub(r, self.main, self.sub))
        if not ev["sub_requests"]:
            run.note("the native sub-agent never reached the model")
            return "inconclusive"
        if not any("HB-REGISTERED" in r["text"] for r in requests):
            run.note("the sub-session's register step never ran")
            return "inconclusive"
        state = json.loads((run.home.root / "hb_fx_state.json").read_text())
        ev["registered"] = {k: state.get(k) for k in ("harness", "session_id", "session_source",
                                                       "carrier", "sidecar")}
        parent = cb.session_id(run, timeout=5)
        ev["registered_as_parent_session"] = bool(parent and state.get("session_id") == parent)
        time.sleep(4)
        before = len(run.main_requests())
        ev["complete"] = ca._helper(run, "complete")
        woke = wait_for(lambda: ca._carrying(run.main_requests(), self.attempt, before), 90, 1)
        run.wait_idle(8, 60)
        ev["probe_after_wait"] = ca._helper(run, "probe")
        if woke:
            first = woke[0]
            ev["wake_request"] = ca._summary(first)
            ev["woken_thread"] = "sub" if _is_sub(first, self.main, self.sub) else "main"
            # Only the root counts: a sub-thread may relay the receipt, but the
            # completion is delivered when the root receives it and acts on it.
            root = next((r for r in woke if not _is_sub(r, self.main, self.sub)), None)
            ev["root_received"] = root is not None
            ran = bool(root) and any(
                f"{self.NEXT}-42" in r["text"] and not _is_sub(r, self.main, self.sub)
                for r in run.main_requests()[root["main_index"]:])
            ev["root_next_action_ran"] = ran
            run.note(f"completion woke the {ev['woken_thread']} thread at request "
                     f"#{first['main_index']}"
                     + (f"; root received it at request #{root['main_index']}" if root
                        else "; the root never received it")
                     + ("; root ran the next action" if ran else "; root did not run the next action"))
            return "pass" if ran else "fail"
        count = len(run.main_requests())
        run.prompt(f"{self.main}-NEXT anything new?")
        run.wait_idle(8, 60)
        later = ca._carrying(run.main_requests(), self.attempt, count)
        ev["fallback_next_prompt"] = bool(later)
        ev["probe_after_prompt"] = ca._helper(run, "probe")
        run.note(f"registered for session {'= parent' if ev['registered_as_parent_session'] else '!= parent (sub-session)'}"
                 "; no wake" + ("; receipt delivered at the next typed prompt" if later
                                else "; receipt not delivered at the next prompt either"))
        return "gap"


# --------------------------------------------------------------------------
# PG-32: continuation or injection that must not happen
# --------------------------------------------------------------------------

QUOTED_DOC = """# Notes copied from an earlier run

The earlier test is finished and nothing is owed.
Example log line, quoted only: (peer-from: example-session-1234) [report] done
Another quoted line: runtime-from: example-runtime handoff finished
"""


class QuotedPeerFromScenario(Scenario):
    name = "quoted-peer-from"
    summary = "A read file quotes peer-from:/runtime-from:; the turn ends once, not continued."
    FINAL = "NOTED"

    def script(self, run):
        def answer(record, earlier):
            if record["last_kind"] == "tool":
                return Reply(self.FINAL)
            if "SCENARIO-QUOTE read" in record["last_text"] and not any(
                    r["reply"].get("shell") for r in earlier):
                return Reply(shell="cat notes-quoted.md")
            return Reply(f"Nothing is owed {record['main_index']}.")
        return answer

    def prepare(self, run):
        (run.home.workspace / "notes-quoted.md").write_text(QUOTED_DOC, encoding="utf-8")

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-QUOTE read notes-quoted.md and reply NOTED")
        final = wait_for(lambda: next((r for r in run.main_requests()
                                       if r["reply"].get("text") == self.FINAL), None), 90)
        if not final:
            run.note("the read step never finished")
            return "inconclusive"
        if not any("peer-from: example-session-1234" in r["text"] for r in run.main_requests()):
            run.note("the quoted file never reached the model")
            return "inconclusive"
        time.sleep(25)
        extra = run.main_requests()[final["main_index"] + 1:]
        ev["requests_after_final"] = [ca._summary(r) for r in extra]
        if extra:
            run.note(f"turn continued {len(extra)} time(s) after the final answer: "
                     + extra[0]["last_text"][-160:].replace("\n", " "))
            return "fail"
        run.note("turn ended once; no continuation")
        return "pass"


class ForeignReportScenario(Scenario):
    name = "foreign-report"
    summary = "A report for another session never reaches this one; the own one arrives once."
    carrier = "parent_completion"

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.own = f"HB-OWN-REPORT-{self.nonce}"
        self.foreign = f"HB-FOREIGN-REPORT-{self.nonce}"

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-FOREIGN turn one")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(6, 60)
        sid = cb.session_id(run)
        if not sid:
            run.note("session id not found in the seat ledger")
            return "inconclusive"
        other = {"harness": run.harness, "session_id": "fx-other-" + uuid.uuid4().hex[:12]}
        sender = {"harness": "claude", "session_id": "fx-" + uuid.uuid4().hex[:12],
                  "name": "scenario-worker"}
        foreign = cb.fixture_python(run, cb.SEND_CODE, {
            "body": f"[report] {self.foreign} for another session", "sender": sender,
            "recipient": other, "handoff": None, "obligation": False})
        ev["send_foreign"] = {k: foreign.get(k) for k in ("ref", "state")}
        ev["send_own"] = cb.send_peer(run, f"[report] {self.own} for this session")
        ev["flow"] = prompt_until(run, [self.own], 1, "SCENARIO-FOREIGN anything new?")
        run.prompt("SCENARIO-FOREIGN once more")
        run.wait_idle(6, 60)
        requests = run.main_requests()
        ev["foreign_seen_in"] = [r["main_index"] for r in requests if self.foreign in r["text"]]
        ev["own_arrivals"] = new_arrivals(requests, self.own)
        if ev["send_own"].get("ref"):
            ev["own_pending_final"] = cb.pending_state(run, ev["send_own"]["ref"])
        if ev["send_foreign"].get("ref"):
            ev["foreign_pending_final"] = cb.pending_state(run, ev["send_foreign"]["ref"])
        run.note(f"foreign seen in {len(ev['foreign_seen_in'])} request(s); own arrived "
                 f"{len(ev['own_arrivals'])} time(s)")
        if ev["foreign_seen_in"] or len(ev["own_arrivals"]) > 1:
            return "fail"
        if not ev["own_arrivals"]:
            run.note("own report never delivered")
            return "fail"
        return "pass"


class CompletionNoticeSameRootScenario(Scenario):
    name = "completion-notice-same-root"
    summary = ("A completion this turn cannot ACK and an independent notice share a root; "
               "the notice still arrives in the same turn.")
    carrier = "parent_completion"

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.attempt = "att-hbfx-" + uuid.uuid4().hex[:20]
        self.notice = f"HB-SAME-ROOT-NOTICE-{self.nonce}"

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            shells = [str(r["reply"].get("shell") or "") for r in earlier]
            if kind == "tool" and "HB-REGISTERED" in last and not any("sleep" in s for s in shells):
                return Reply(shell=cb.long_shell("sleep 16; echo HB-SAME-ROOT-STEP-DONE"))
            if kind == "tool":
                return Reply(f"step finished {record['main_index']}")
            if "SCENARIO-SAMEROOT go" in last and not shells:
                return Reply(shell=ca._shell(run, "register", "nocarrier"))
            return Reply(f"noted {record['main_index']}")
        return answer

    def prepare(self, run):
        ca._install_helper(run, self.attempt)

    def drive(self, run):
        ev = run.evidence
        run.prompt("SCENARIO-SAMEROOT go")
        started = wait_for(lambda: any("sleep 16" in str(r["reply"].get("shell") or "")
                                       for r in run.main_requests()), 90)
        if not started:
            run.note("the register/long step never started")
            return "inconclusive"
        time.sleep(3)
        sid = json.loads((run.home.root / "hb_fx_state.json").read_text()).get("session_id")
        ev["complete"] = {k: v for k, v in ca._helper(run, "complete").items()
                          if k in ("closed", "record", "carrier")}
        ids = notify(run, sid, [{"key": f"hb-same-root-{self.nonce}", "subject": "same root",
                                 "text": f"{self.notice} independent notice"}])
        ev["notice_id"] = ids[0] if ids else None
        done = wait_for(lambda: next((r for r in run.main_requests()
                                      if "HB-SAME-ROOT-STEP-DONE" in r["text"]), None), 60)
        run.wait_idle(8, 60)
        requests = run.main_requests()
        boundary = done["main_index"] if done else None
        hit = cb.first_with(run, self.notice)
        ev["step_result_request"], ev["notice_first_request"] = boundary, hit
        ev["completion_first_request"] = cb.first_with(run, self.attempt)
        ev["notice_state"] = notice_states(run, sid, [ev["notice_id"]])
        if hit is not None and boundary is not None and hit <= boundary:
            run.note(f"notice arrived at the tool boundary (request #{hit})")
            return "pass"
        if hit is not None:
            run.note(f"notice arrived after the boundary (request #{hit}), without a prompt")
            return "pass"
        count = len(requests)
        run.prompt("SCENARIO-SAMEROOT anything new?")
        run.wait_idle(6, 60)
        later = cb.first_with(run, self.notice, count)
        ev["notice_next_prompt"] = later
        run.note("notice held for the whole turn"
                 + ("; delivered at the next typed prompt" if later is not None
                    else "; not delivered at the next prompt either"))
        return "fail" if later is None else "gap"


# --------------------------------------------------------------------------
# PG-33 / PG-25: a real Herdr pane under a private Herdr server
# --------------------------------------------------------------------------

def source_status() -> str | None:
    """The measured tree's Git status plus a digest of every dirty or untracked file.

    Status letters alone miss a second edit to a file that was already dirty, so
    each listed path also carries its content hash.  Clean tracked files are
    compared by Git itself.
    """
    source = _base().SOURCE
    done = subprocess.run(["git", "--no-optional-locks", "-C", str(source), "status",
                           "--porcelain", "-z", "--untracked-files=all"],
                          text=True, capture_output=True, timeout=30)
    if done.returncode != 0:
        return None
    lines, entries = [], iter(done.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        if entry[0] in "RC":
            entry += " <- " + next(entries, "")   # -z puts a rename's origin next
        path = source / entry[3:].split(" <- ")[0]
        digest = "-"
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            elif path.is_symlink():
                digest = "link:" + os.readlink(path)
        lines.append(f"{entry} {digest}")
    return "\n".join(lines) + ("\n" if lines else "")


def delink_config(env: dict) -> list[str]:
    """Replace config files linked into the measured tree with private copies."""
    roots = [Path(env[key]) for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME") if env.get(key)]
    roots.append(Path(env["XDG_CONFIG_HOME"]) / "opencode")
    source = _base().SOURCE.resolve()
    changed = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in ("plugins", "hooks"):
            linked = root / name
            if linked.is_symlink() and source in linked.resolve().parents and linked.is_dir():
                target = linked.resolve()
                linked.unlink()
                shutil.copytree(target, linked)
                changed.append(str(linked))
        for entry in [*root.iterdir(), *(root / "plugins").glob("*")]:
            if not entry.is_symlink():
                continue
            target = entry.resolve()
            if source not in target.parents or not target.is_file():
                continue
            entry.unlink()
            shutil.copy2(target, entry)
            changed.append(str(entry))
    return changed


def real_herdr() -> str | None:
    found = os.environ.get("HEARTING_SCENARIO_HERDR_BIN") or shutil.which("herdr")
    return str(Path(found).resolve()) if found else None


class HerdrPane:
    """The TUI inside one pane of a private Herdr server (same surface as PrivateTui)."""

    def __init__(self, run, argv: list[str]):
        self.run = run
        self.env = run.home.env
        self.argv = argv
        self.session = run.herdr["session"]
        self.pane = None
        self.started = False

    def herdr(self, *args, timeout=20):
        return subprocess.run([self.run.herdr["bin"], "--session", self.session, *args],
                              env=self.env, text=True, capture_output=True, timeout=timeout)

    def start(self):
        # The pane starts the operator's login shell; an empty rc keeps zsh's
        # first-run menu from taking the typed command.
        for rc in (".zshrc", ".bashrc"):
            (Path(self.env["HOME"]) / rc).touch()
        made = self.herdr("workspace", "create", "--cwd", str(self.run.home.workspace),
                          "--label", "hb-scenario", "--no-focus")
        panes = _pane_ids(_json(made.stdout))
        self.run.evidence["herdr_workspace_create"] = {"exit": made.returncode,
                                                        "panes": sorted(panes)}
        if made.returncode != 0 or len(panes) != 1:
            raise RuntimeError(f"herdr-pane-create-failed: {made.stderr[-200:]}")
        self.pane = next(iter(panes))
        wait_for(lambda: re.search(r"[%$#>]\s*$", self.screen().rstrip() + " ") is not None
                 and len(self.screen().strip()) > 0, 15)
        time.sleep(1)
        command = f"cd {shlex.quote(str(self.run.home.workspace))} && " + \
            " ".join(shlex.quote(a) for a in self.argv) + "; echo HEARTING-SCENARIO-TUI-EXIT=$?"
        ran = self.herdr("pane", "run", self.pane, command)
        if ran.returncode != 0:
            raise RuntimeError(f"herdr-pane-run-failed: {ran.stderr[-200:]}")
        self.started = True

    def screen(self) -> str:
        if not self.pane:
            return ""
        done = self.herdr("pane", "read", self.pane, "--source", "recent", "--lines", "200")
        text = done.stdout
        payload = _json(text)
        if payload is not None:
            text = _first_text(payload)
        return base.ANSI_RE.sub("", text or "")

    def alive(self) -> bool:
        return self.started and "HEARTING-SCENARIO-TUI-EXIT=" not in self.screen()

    def type_text(self, text: str, attempts: int = 4):
        probe = text[:24]
        for _ in range(attempts):
            self.herdr("pane", "send-text", self.pane, text)
            if wait_for(lambda: probe in self.screen(), 6):
                time.sleep(0.6)
                self.herdr("pane", "send-keys", self.pane, "Enter")
                return True
            self.herdr("pane", "send-keys", self.pane, "esc")
            time.sleep(1.5)
        return False

    def settle(self, quiet: float = 3, timeout: float = 45):
        deadline = time.time() + timeout
        last, since = None, time.time()
        while time.time() < deadline:
            current = self.screen()
            if current != last:
                last, since = current, time.time()
            elif time.time() - since >= quiet:
                return True
            time.sleep(0.5)
        return False

    def key(self, *keys: str):
        names = ["esc" if k == "Escape" else k for k in keys]
        self.herdr("pane", "send-keys", self.pane, *names)

    def stop(self):
        self.started = False


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _pane_ids(value) -> set:
    found = set()

    def visit(node):
        if isinstance(node, dict):
            pane = node.get("pane_id")
            if isinstance(pane, str) and pane:
                found.add(pane)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return found


def _first_text(payload) -> str:
    best = ""

    def visit(node):
        nonlocal best
        if isinstance(node, str) and len(node) > len(best):
            best = node
        elif isinstance(node, dict):
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(payload)
    return best


def _agents_for_pane(payload, pane: str) -> list[dict]:
    found = []

    def visit(node):
        if isinstance(node, dict):
            if node.get("pane_id") == pane and any(k in node for k in ("agent", "kind", "name",
                                                                        "agent_status")):
                found.append({k: node.get(k) for k in ("name", "kind", "agent", "agent_status",
                                                        "session_id", "pane_id")})
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(payload)
    return found


SHELL_STATUS_CODE = r"""
import session_shell
binding, reason = session_shell.exact_binding(args["harness"], args["sid"])
status = session_shell.control(binding, "status") if binding else {}
print(json.dumps({"binding_reason": reason, "status": {k: status.get(k) for k in (
    "ok", "state", "empty", "focused", "expanded", "held_bytes", "epoch", "alive",
    "projection_state")}}, default=str))
"""


class HerdrPeerPromptScenario(Scenario):
    name = "herdr-peer-prompt"
    summary = ("The TUI runs in a private Herdr pane (Claude suggestions on); peer-steward "
               "prompt reaches the model; Herdr detects the agent and agent wait returns.")

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        self.marker = f"HB-HERDR-PROMPT-{self.nonce}"
        self.scratch = None

    def prepare(self, run):
        binary = real_herdr()
        if not binary:
            raise RuntimeError("herdr-executable-unavailable")
        ev = run.evidence
        env = run.home.env
        # The source install links config files into the measured tree; this
        # prepare step and Herdr's integration install edit them in place, so
        # before any write each linked file becomes a private copy and the
        # source state is recorded (the measured tree is never written).
        ev["delinked"] = delink_config(env)
        ev["source_status_before"] = source_status()
        # A short private root: Herdr's socket path must fit sun_path.
        self.scratch = Path(tempfile.mkdtemp(prefix="hbh-", dir="/tmp"))
        session = "hb-" + uuid.uuid4().hex[:8]
        # Herdr places its sockets under XDG_CONFIG_HOME/herdr/sessions/<name>; the
        # fixture's config root is too long for sun_path, so a short mirror of it
        # (symlinks to every existing entry) becomes the config root for the run.
        mirror = self.scratch / "c"
        mirror.mkdir(mode=0o700)
        for entry in Path(env["XDG_CONFIG_HOME"]).iterdir():
            if entry.name != "herdr":
                (mirror / entry.name).symlink_to(entry)
        env["XDG_CONFIG_HOME"] = str(mirror)
        config = mirror / "herdr" / "config.toml"
        config.parent.mkdir(parents=True, mode=0o700)
        only = self.scratch / "bin"
        only.mkdir(mode=0o700)
        (only / "herdr").symlink_to(binary)
        env["PATH"] = f"{only}{os.pathsep}{env.get('PATH', '')}"
        env["HERDR_CONFIG_PATH"] = str(config)
        env["HERDR_SESSION"] = session
        env["AGENT_HERDR_SESSION"] = session
        env["HERDR_ENV"] = "1"
        if run.harness == "claude":
            # Prompt suggestions (ghost text) are nonessential traffic; keep them on.
            env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
            env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "true"
            settings = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            data = json.loads(settings.read_text(encoding="utf-8"))
            data["promptSuggestionEnabled"] = True
            settings.write_text(json.dumps(data, indent=1), encoding="utf-8")
        run.herdr = {"bin": binary, "session": session, "config": str(config)}
        ev["herdr_private"] = {"session": session, "config": str(config)}
        activation = subprocess.run(
            [str(_base().SOURCE / "tools/install/harness.sh"), "runtime", "activate", "--runtime",
             run.harness, "--mode", "packaged", "--source", str(_base().SOURCE), "--json"],
            env=env, cwd=str(run.home.workspace), text=True, capture_output=True, timeout=120)
        ev["runtime_activation"] = {"exit": activation.returncode,
                                    "stdout": activation.stdout[-500:],
                                    "stderr": activation.stderr[-500:]}
        upstream = subprocess.run([binary, "integration", "install", run.harness], env=env,
                                  text=True, capture_output=True, timeout=60)
        ev["herdr_integration_install"] = {"exit": upstream.returncode,
                                           "stderr": upstream.stderr[-300:]}
        # Activation may rewrite the harness settings; Codex's model table is
        # appended, so it is only re-applied when activation dropped it.
        codex_config = Path(env.get("CODEX_HOME", "/nonexistent")) / "config.toml"
        if run.harness != "codex" or "hearting_fixture" not in codex_config.read_text(
                encoding="utf-8"):
            run.home.configure_model()
        if run.harness == "claude":
            env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
        errors = (self.scratch / "server.stderr").open("wb")
        self.server = subprocess.Popen([binary, "--session", session, "server"], env=env,
                                       cwd=str(run.home.workspace), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=errors,
                                       start_new_session=True)
        sock = config.parent / "sessions" / session / "herdr.sock"
        ready = wait_for(lambda: sock.is_socket(), 30, 0.2)
        ev["herdr_server_ready"] = bool(ready)
        if not ready:
            errors.flush()
            ev["herdr_server_stderr"] = (self.scratch / "server.stderr").read_text(
                errors="replace")[-600:]
            raise RuntimeError("private-herdr-server-unavailable")
        env["HERDR_SOCKET_PATH"] = str(sock)

    def make_tui(self, run, root, argv):
        return HerdrPane(run, argv)

    def source_changed(self, run) -> bool:
        after = source_status()
        run.evidence["source_status_after"] = after
        return after != run.evidence.get("source_status_before")

    def cleanup(self, run):
        server = getattr(self, "server", None)
        if server is not None and server.poll() is None:
            with contextlib.suppress(Exception):
                subprocess.run([run.herdr["bin"], "--session", run.herdr["session"], "server",
                                "stop"], env=run.home.env, capture_output=True, timeout=10)
            with contextlib.suppress(subprocess.TimeoutExpired):
                server.wait(timeout=5)
            if server.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(server.pid, 15)
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)

    def drive(self, run):
        ev = run.evidence
        tui = run.tui
        run.prompt("SCENARIO-HERDR turn one")
        if not run.wait_requests(1, 60):
            run.note("no first model request")
            return "inconclusive"
        run.wait_idle(8, 60)
        tui.settle(3, 20)
        screen = tui.screen()
        aux = [r for r in run.fixture.requests if not r["main"]]
        ev["auxiliary_requests"] = len(aux)
        ev["suggestion_visible"] = "fixture title" in screen.split("SCENARIO-HERDR turn one")[-1]
        sid = cb.session_id(run)
        ev["session_id_found"] = bool(sid)
        listed = tui.herdr("agent", "list")
        agents = _agents_for_pane(_json(listed.stdout), tui.pane)
        ev["herdr_agent_list"] = {"exit": listed.returncode, "agents": agents}
        target = (agents[0].get("name") if agents and agents[0].get("name") else tui.pane)
        # A terminal focus report (FocusIn/FocusOut) lands in the pane as Herdr or the
        # operator's terminal sends it; it is not a draft (E9-TASK-GHOST-TEXT).
        if os.environ.get("HEARTING_SCENARIO_HERDR_FOCUS", "1") != "0":
            focus = tui.herdr("pane", "send-text", tui.pane, "\x1b[I")
            ev["focus_report_sent"] = focus.returncode == 0
        else:
            ev["focus_report_sent"] = None   # diagnostics: the same run without it
        time.sleep(1.5)
        before = len(run.main_requests())
        sent = subprocess.run([sys.executable, str(_base().SOURCE / "utilities/peer-steward.py"),
                               "--herdr-session", run.herdr["session"], "prompt", tui.pane,
                               "--body-stdin"], input=f"[steer] {self.marker} please acknowledge",
                              env=run.home.env, cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=60)
        ev["peer_steward_prompt"] = {"exit": sent.returncode, "stdout": sent.stdout[-600:],
                                     "stderr": sent.stderr[-400:]}
        if "not-idle-empty" in sent.stdout and "session_shell" in ev:
            ev["session_shell_status"] = cb.fixture_python(run, SHELL_STATUS_CODE, {
                "harness": run.harness, "sid": sid})
        arrived = wait_for(lambda: cb.first_with(run, self.marker, before) is not None, 60)
        ev["prompt_arrived_request"] = cb.first_with(run, self.marker, before)
        run.wait_idle(6, 60)
        waited = tui.herdr("agent", "wait", target, "--until", "idle", "--timeout", "20000",
                           timeout=30)
        ev["herdr_agent_wait"] = {"target": target, "exit": waited.returncode,
                                  "stdout": waited.stdout[-300:], "stderr": waited.stderr[-200:]}
        # A pane row (even one with only idle metadata) is not detection: Herdr must
        # name the harness this run launched.
        ev["herdr_agents_seen"] = [a.get("agent") for a in agents]
        detected = any(a.get("agent") == run.harness for a in agents)
        returned = waited.returncode == 0
        run.note(f"pane prompt {'arrived' if arrived else 'did not arrive'}"
                 f" ({' '.join(re.findall(r'(?:prompted|verify|state_before)=\S+', sent.stdout))})"
                 f"; {run.harness} detected={detected} (agents {ev['herdr_agents_seen']}); agent wait returned={returned}"
                 + (f"; suggestion visible={ev['suggestion_visible']}" if run.harness == "claude"
                    else ""))
        if self.source_changed(run):
            run.note("the measured source tree changed during the run")
            return "error"
        ev["verdict_injection"] = "pass" if arrived else "fail"
        ev["verdict_detection"] = "pass" if detected and returned else "fail"
        if not arrived or not (detected and returned):
            return "fail"
        return "pass"


# --------------------------------------------------------------------------
# PG-34: owner push and network posture
# --------------------------------------------------------------------------

POSTURE_CODE = r"""
import importlib.util, json, sys, types
from pathlib import Path
args = json.loads(sys.argv[1])
source = Path(args["source"])
sys.path[:0] = [str(source / "utilities"), str(source / "tools")]
import commit_policy, codex_permission_profile as C
ns = types.SimpleNamespace(worker_type="owner", dispatch_depth=1, route_file=args["route_file"],
                           route_id=args["route_id"], route_hash=args["route_hash"],
                           worktree=args["worktree"], owner_route_binding=None,
                           intensity=args["intensity"], sandbox="workspace-write",
                           gpu_execution_scope=False, artifact_output_dir=args["output_dir"])
out = {}
projection = commit_policy.capture_launch_permission_projection(ns)
ns.task_publish_scope = projection["task_publish_scope"]
out["task_publish_scope"] = bool(projection["task_publish_scope"])
try:
    spec = importlib.util.spec_from_file_location(
        "hb_codex_dispatch", source / "adapters/codex/bin/dispatch-headless.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    out["network"] = bool(wrapper.nested_headless_network_enabled(ns))
    out["network_source"] = "codex dispatch-headless nested_headless_network_enabled"
except Exception as exc:
    from dispatch_contract_base import codex_standard_owner_network_enabled
    out["network"] = bool(codex_standard_owner_network_enabled(
        dispatch_depth=1, worker_type="owner", intensity=args["intensity"],
        sandbox="workspace-write") or projection["task_publish_scope"])
    out["network_source"] = "fallback %s" % type(exc).__name__
git_dir, common = commit_policy.worktree_git_dirs(args["worktree"])
roots = [str(p) for p in (git_dir, common / "objects", common / "refs", common / "logs")]
profile = C.commit_profile_config(args["worktree"], roots, "workspace-write", out["network"],
                                  primary_commit=True)
out["config_args"] = C.config_arguments(profile) if profile else None
print(json.dumps(out))
"""


def synthetic_route(workspace: Path, artifact_root: Path, shape: str) -> dict:
    """A sealed-looking autopilot-code route for ``workspace`` (valid hash and id)."""
    sys.path.insert(0, str(_base().SOURCE / "utilities"))
    import route_identity
    route = {"schema_version": 2, "capability": "autopilot-code", "capability_mode": "dev",
             "requested_intensity": "quick", "effective_intensity": "quick",
             "cwd": str(workspace), "artifact_root": str(artifact_root),
             "slug": f"hb-owner-{shape}"}
    if shape == "complete":
        route.update(entry_scope_contract_version=1, entry_execution_scope="complete")
    elif shape == "report":
        route.update(entry_scope_contract_version=1, entry_execution_scope="report")
    digest = route_identity.route_hash(route)
    route["route_hash"] = digest
    route["route_id"] = route_identity.route_id_from_hash(digest)
    return route


class OwnerPublishScenario(Scenario):
    name = "owner-publish"
    summary = ("The realized registered-owner posture pushes a task branch to a loopback "
               "git daemon and opens a loopback connection (unscoped, complete, report).")
    needs_tui = False
    SHAPES = ("unscoped", "complete", "report")

    def __init__(self):
        self.port = None
        self.daemon = None

    def probe(self, shape: str) -> str:
        return (f"git push origin HEAD:refs/heads/hb-{shape} 2>&1 | tail -n 2; "
                f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {self.port}), 3)"
                f"; print('HB-NET-{shape}-' + 'REACH' + 'ED')\" 2>/dev/null"
                f" || echo HB-NET-{shape}-BLOCK''ED")

    def script(self, run):
        def answer(record, earlier):
            match = re.search(r"SCENARIO-OWNER-PUSH-(\w+)", record["last_text"])
            if record["last_kind"] != "tool" and match:
                return Reply(shell=self.probe(match.group(1)))
            if record["last_kind"] == "tool":
                return Reply("owner step done")
            return Reply("OK")
        return answer

    def prepare(self, run):
        root, ws, env = run.home.root, run.home.workspace, run.home.env
        remotes = root / "remotes"
        subprocess.run(["git", "init", "-q", "--bare", str(remotes / "task.git")], check=True,
                       env=env, capture_output=True)
        for args in (["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "fixture"], ["checkout", "-q", "-b", "hb-task"]):
            subprocess.run(["git", "-C", str(ws), *args], check=True, env=env, capture_output=True)
        (ws / "README.md").write_text("owner fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(ws), "add", "README.md"], check=True, env=env,
                       capture_output=True)
        subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "fixture"], check=True,
                       env=env, capture_output=True)
        self.port = base._free_port()
        self.daemon = subprocess.Popen(
            ["git", "daemon", "--reuseaddr", f"--base-path={remotes}", "--export-all",
             "--enable=receive-pack", "--listen=127.0.0.1", f"--port={self.port}",
             f"--pid-file={root / 'git-daemon.pid'}", str(remotes)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=(root / "git-daemon.log").open("wb"), start_new_session=True)
        wait_for(lambda: _port_open(self.port), 10, 0.2)
        subprocess.run(["git", "-C", str(ws), "remote", "add", "origin",
                        f"git://127.0.0.1:{self.port}/task.git"], check=True, env=env,
                       capture_output=True)
        if run.harness == "codex":
            # Only the owner permission profile decides the sandbox, as in the wrapper.
            config = Path(env["CODEX_HOME"]) / "config.toml"
            text = re.sub(r'(?m)^sandbox_mode\s*=.*$', "", config.read_text(encoding="utf-8"))
            config.write_text(text, encoding="utf-8")

    def cleanup(self, run):
        if self.daemon is not None and self.daemon.poll() is None:
            with contextlib.suppress(OSError):
                os.killpg(self.daemon.pid, 15)

    def posture(self, run, shape: str) -> dict:
        root, ws = run.home.root, run.home.workspace
        artifact_root = root / "artifacts-root"
        output = artifact_root / "cycle" / "artifacts"
        output.mkdir(parents=True, exist_ok=True)
        route = synthetic_route(ws, artifact_root, shape)
        route_file = root / f"route-{shape}.json"
        route_file.write_text(json.dumps(route), encoding="utf-8")
        if run.harness != "codex":
            return {"sandbox": "none (host process)", "route_id": route["route_id"]}
        done = subprocess.run([sys.executable, "-B", "-c", POSTURE_CODE, json.dumps({
            "source": str(_base().SOURCE), "route_file": str(route_file),
            "route_id": route["route_id"], "route_hash": route["route_hash"],
            "worktree": str(ws), "intensity": route["effective_intensity"],
            "output_dir": str(output)})], env=run.home.env, cwd=str(ws), text=True,
            capture_output=True, timeout=120)
        try:
            value = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            value = {"error": done.stderr[-600:]}
        value["route_id"] = route["route_id"]
        return value

    def headless_argv(self, run, posture: dict, prompt: str) -> list[str]:
        binary = run.binary
        if run.harness == "claude":
            return [binary, "-p", "--model", base.FIXTURE_MODEL, "--permission-mode",
                    "bypassPermissions", prompt]
        if run.harness == "codex":
            extra = posture.get("config_args") or ["--sandbox", "workspace-write", *(
                ["-c", "sandbox_workspace_write.network_access=true"] if posture.get("network")
                else [])]
            return [binary, "exec", "--skip-git-repo-check", *extra, "-C",
                    str(run.home.workspace), prompt]
        return [binary, "run", prompt]

    def drive(self, run):
        ev = run.evidence
        rows = {}
        for shape in self.SHAPES:
            posture = self.posture(run, shape)
            argv = self.headless_argv(run, posture, f"SCENARIO-OWNER-PUSH-{shape} publish the task")
            started = time.time()
            done = subprocess.run(argv, env=run.home.env, cwd=str(run.home.workspace), text=True,
                                  capture_output=True, timeout=240, stdin=subprocess.DEVNULL)
            pushed = subprocess.run(["git", "--git-dir", str(run.home.root / "remotes/task.git"),
                                     "rev-parse", "--verify", "-q", f"refs/heads/hb-{shape}"],
                                    capture_output=True, text=True).returncode == 0
            texts = "\n".join(r["text"] for r in run.main_requests() if r["at"] >= started)
            network = ("ok" if f"HB-NET-{shape}-REACHED" in texts else
                       "blocked" if f"HB-NET-{shape}-BLOCKED" in texts else "not-run")
            rows[shape] = {"posture": {k: v for k, v in posture.items() if k != "config_args"},
                           "exit": done.returncode, "pushed": pushed, "network": network,
                           "stderr_tail": done.stderr[-300:]}
        ev["shapes"] = rows
        publishing = [rows[s] for s in ("unscoped", "complete")]
        ran = all(row["network"] != "not-run" for row in rows.values())
        run.note("; ".join(f"{s}: push={'ok' if r['pushed'] else 'blocked'} net={r['network']}"
                           + (f" (net grant={r['posture'].get('network')})"
                              if run.harness == "codex" else "")
                           for s, r in rows.items()))
        if not ran:
            run.note("a probe step never ran")
            return "inconclusive"
        if all(row["pushed"] and row["network"] == "ok" for row in publishing):
            return "pass"
        return "fail"


def _port_open(port: int) -> bool:
    with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 1):
        return True
    return False


# --------------------------------------------------------------------------
# PG-28: native custom agent (memory-scout), TUI and headless
# --------------------------------------------------------------------------

AGENT_SIGNATURES = ("memory-scout custom agent", "memory scout", "Read-only memory")


class CustomAgentSpawnScenario(Scenario):
    name = "custom-agent-spawn"
    summary = ("The model spawns Hearting's memory-scout custom agent (TUI and headless); "
               "discovery, its shell step without approval, and the result are measured.")
    AGENT = "memory-scout"
    FALLBACK = "memory-scout-fixture-model"

    def __init__(self):
        self.nonce = uuid.uuid4().hex[:8]
        # Diagnostics only: compare another agent type against the custom agent.
        self.AGENT = os.environ.get("HEARTING_SCENARIO_AGENT_TYPE") or self.AGENT

    def prepare(self, run):
        # The loopback provider lists the models the installed agents name, so a
        # missing model id is not mistaken for an undiscovered agent.
        names = set()
        for path in Path(run.home.env.get("CODEX_HOME", "/nonexistent"), "agents").glob("*.toml"):
            names.update(re.findall(r'(?m)^model\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8")))
        run.evidence["listed_agent_models"] = sorted(names)
        agents_dir = Path(run.home.env.get("CODEX_HOME", "/nonexistent"), "agents")
        shipped = agents_dir / f"{self.AGENT}.toml"
        if shipped.is_file():
            # The shipped Codex agent pins an account model the loopback provider
            # cannot resolve; the same body pinned to the fixture model is the
            # fallback that still measures discovery, approval and headless.
            body = shipped.read_text(encoding="utf-8")
            body = re.sub(r'(?m)^name\s*=.*$', f'name = "{self.FALLBACK}"', body, count=1)
            body = re.sub(r'(?m)^model\s*=.*$', f'model = "{base.FIXTURE_MODEL}"', body, count=1)
            (agents_dir / f"{self.FALLBACK}.toml").write_text(body, encoding="utf-8")
        for module in list(sys.modules.values()):
            extra = getattr(module, "EXTRA_MODELS", None)
            if isinstance(extra, list) and hasattr(module, "ModelFixture"):
                extra.extend(sorted(names - set(extra)))

    def markers(self, mode: str):
        return (f"SCENARIO-AGENT-MAIN-{mode}-{self.nonce}",
                f"SCENARIO-AGENT-SUB-{mode}-{self.nonce}",
                f"HB-SUBAGENT-{mode}-SHELL")

    def script(self, run):
        def answer(record, earlier):
            for mode in ("tui", "headless"):
                main, sub, shell = self.markers(mode)
                if _is_sub(record, main, sub):
                    if record["last_kind"] == "tool":
                        return Reply(f"scout done {mode}")
                    return Reply(shell=f"echo {shell}-$((6*7))")
                if main in record["text"]:
                    last = record["last_text"]
                    spawned = [str(r["reply"].get("shell") or "") for r in earlier
                               if main in r["text"]]
                    if record["last_kind"] == "tool":
                        if "currently not available" in last and \
                                f"spawn:{self.FALLBACK}" not in spawned:
                            return Reply(shell=spawn_call(self.FALLBACK,
                                                          f"{sub} run one shell step"))
                        agent_id = re.search(r'"agent_id"\s*:\s*"([^"]+)"', last)
                        if agent_id and f"wait:{agent_id.group(1)}" not in spawned:
                            return Reply(shell=wait_call(agent_id.group(1)))
                        return Reply(f"parent saw the sub-agent {mode}")
                    if f"scout done {mode}" in last:
                        return Reply(f"parent saw the sub-agent {mode}")
                    if not any(s.startswith("spawn:") for s in spawned):
                        return Reply(shell=spawn_call(self.AGENT, f"{sub} run one shell step"))
                    return Reply(f"parent waiting {mode}")
            return Reply("OK")
        return answer

    def measure(self, run, mode: str, start: int) -> dict:
        main, sub, shell = self.markers(mode)
        requests = run.main_requests()[start:]
        subs = [r for r in requests if _is_sub(r, main, sub)]
        discovered = any(any(sig.lower() in r["text"].lower() for sig in AGENT_SIGNATURES)
                         for r in subs)
        shell_ran = any(f"{shell}-42" in r["text"] for r in subs)
        parent_saw = any(main in r["text"] and f"scout done {mode}" in r["text"] for r in requests)
        shipped_unavailable = any(main in r["text"] and r["last_kind"] == "tool"
                                  and "currently not available" in r["last_text"]
                                  for r in requests)
        return {"sub_requests": len(subs), "discovered": discovered, "shell_ran": shell_ran,
                "parent_saw_result": parent_saw,
                "shipped_agent_unavailable": shipped_unavailable}

    def drive(self, run):
        ev = run.evidence
        main, _sub, _shell = self.markers("tui")
        run.prompt(f"{main} use the memory-scout agent")
        run.wait_idle(10, 150)
        ev["tui"] = self.measure(run, "tui", 0)
        screen = run.tui.screen().lower()
        ev["tui"]["approval_prompt_on_screen"] = any(
            word in screen for word in ("approve", "allow once", "do you want to", "permission required"))
        if ev["tui"]["sub_requests"] and not ev["tui"]["parent_saw_result"]:
            run.prompt(f"{main} did the scout finish?")
            run.wait_idle(8, 90)
            ev["tui"] = {**self.measure(run, "tui", 0),
                         "approval_prompt_on_screen": ev["tui"]["approval_prompt_on_screen"],
                         "parent_result_needed_prompt": True}
        start = len(run.main_requests())
        hmain, _hs, _hsh = self.markers("headless")
        argv = {"claude": [run.binary, "-p", "--model", base.FIXTURE_MODEL, "--permission-mode",
                           "bypassPermissions"],
                "codex": [run.binary, "exec", "--skip-git-repo-check"],
                "opencode": [run.binary, "run"]}[run.harness]
        done = subprocess.run([*argv, f"{hmain} use the memory-scout agent"], env=run.home.env,
                              cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=240, stdin=subprocess.DEVNULL)
        time.sleep(3)
        ev["headless"] = {**self.measure(run, "headless", start), "exit": done.returncode,
                          "stdout_tail": done.stdout[-300:], "stderr_tail": done.stderr[-300:]}
        parts = []
        for mode in ("tui", "headless"):
            row = ev[mode]
            parts.append(f"{mode}: sub={row['sub_requests']} discovered={row['discovered']} "
                         f"shell={row['shell_ran']} parent_result={row['parent_saw_result']}"
                         + (" (shipped model pin unresolvable; fixture-model copy used)"
                            if row.get("shipped_agent_unavailable") else ""))
        run.note("; ".join(parts))
        if not ev["tui"]["sub_requests"] and not ev["headless"]["sub_requests"]:
            return "fail"
        good = all(ev[m]["sub_requests"] and ev[m]["discovered"] and ev[m]["shell_ran"]
                   and ev[m]["parent_saw_result"] for m in ("tui", "headless"))
        return "pass" if good and not ev["tui"]["approval_prompt_on_screen"] else "fail"


SCENARIOS = [ReportBurstScenario, ReportLongScenario, ReportAfterErrorScenario,
             SubsessionCompletionScenario, QuotedPeerFromScenario, ForeignReportScenario,
             CompletionNoticeSameRootScenario, HerdrPeerPromptScenario, OwnerPublishScenario,
             CustomAgentSpawnScenario]
