#!/usr/bin/env python3
"""Peer-request scenarios (stage 2) for ``harness_behavior_scenarios.py``.

Two real sessions of one harness (the run's TUI is the requester A; a second TUI under its
own private tmux server is the receiver B) share the loopback model. The scripted model
answers each by its own conversation marker. The verdicts read what the model consumed and
what the request record says, never a helper's success line:

``request-flow``
    A asks B for work with ``peer-steward.py request``. Pass: B's conversation carries the
    one-line request but not the body nonce; after B's own ``show`` tool call the body nonce
    is in B's next model request; B's partial report wakes A once with no typed input and
    leaves the request ``partial``; B's final report wakes A once more, the record is
    ``final`` and names B as the reporter; no report wakes A twice.
``request-revision-cancel``
    Revision 2 supersedes revision 1 while B works; B's late report on revision 1 is kept on
    revision 1 (``late``) and leaves the request open; revision 2's final report closes it;
    a revision cancelled before delivery never reaches B; a cancel after delivery reaches B
    as one read-only line that does not say "stop".
``request-handoff``
    B is replaced by B' through a private session-tidy lineage row. Pass: the open request
    is B' 's and B' 's report is accepted. Counter-case: a session with no lineage row
    cannot report (``reporter-not-recipient``) and the request stays with B.

The panel lines (the requester's ``→`` and the receiver's ``◂``) are captured from both
terminals into the evidence; a panel that does not show them is a ``gap``.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_d import WatchWakeScenario  # noqa: E402
from dispatch_contract_roots import stable_state_root  # noqa: E402

STEWARD = 'python3 "$AGENT_HOME/utilities/peer-steward.py"'
REQUEST_LINE = re.compile(r"\[요청 (rq-[0-9a-f]{6})(?: [^\]]*)?\]")
REPORT_LINE = re.compile(r"\[요청 답 (rq-[0-9a-f]{6})(?: [^\]]*)?\]([^\n]*)")
CANCEL_LINE = re.compile(r"\[요청 취소 (rq-[0-9a-f]{6})\]")
A_MARK = "SCENARIO-REQ-A"
B_MARK = "SCENARIO-WATCH-TARGET"  # the second TUI keeps the watch scenarios' receiver marker


def _shells(records, mark):
    return [(r["reply"].get("shell") or "") for r in records if mark in r["text"]]


def answer(ctx, record, earlier):
    """The scripted model for both sessions. ``ctx`` carries nonce, target and file names."""
    text, last, kind = record["text"], record["last_text"], record["last_kind"]
    nonce = ctx["nonce"]
    if B_MARK in text and A_MARK not in text:
        return _answer_b(ctx, text, last, kind, _shells(earlier, B_MARK), nonce)
    return _answer_a(ctx, text, last, kind, _shells(earlier, A_MARK), nonce)


def _answer_b(ctx, text, last, kind, shells, nonce):
    sent = lambda needle: any(needle in shell for shell in shells)  # noqa: E731
    if kind == "tool":
        done = re.search(r"report=(rq-[0-9a-f]{6}) kind=(\w+)", last)
        if "(sealed body)" in last and f"BODY-KEEP-{nonce}" in last:
            return Reply("B holds this request open.")  # stays open for the cancel-after-delivery case
        if "(sealed body)" in last:
            request_id = re.findall(r"request=(rq-[0-9a-f]{6})", last)[-1]
            if ctx.get("hold") and not sent("sleep"):
                return Reply(shell="sleep %d; echo B-WORK-DONE" % ctx["hold"])
            if ctx.get("partial", True) and not sent("--partial"):
                return Reply(shell=f'{STEWARD} report {request_id} --partial --result "PARTIAL-{nonce}"')
            return Reply(shell=f'{STEWARD} report {request_id} --result "FINAL-{nonce}"')
        if "B-WORK-DONE" in last:
            # The long step ended: answer the earlier revision (the one B actually received).
            request_id = ctx.get("report_id") or re.findall(r"rq-[0-9a-f]{6}", text)[0]
            return Reply(shell=f'{STEWARD} report {request_id} --result "LATE-{nonce}"')
        if done and done.group(2) == "partial" and not sent('--result "FINAL'):
            return Reply(shell=f'sleep 3; {STEWARD} report {done.group(1)} --result "FINAL-{nonce}"')
        return Reply("B step done.")
    cancelled = CANCEL_LINE.search(last)
    if cancelled:
        return Reply("B noted the cancel.")
    requested = REQUEST_LINE.search(last)
    if requested:
        return Reply(shell=f"{STEWARD} show {requested.group(1)}")
    return Reply("B initialized.")


def _answer_a(ctx, text, last, kind, shells, nonce):
    sent = lambda needle: any(needle in shell for shell in shells)  # noqa: E731
    if kind == "tool":
        return Reply("A step done.")
    reported = REPORT_LINE.search(last)
    if reported:
        return Reply("A saw the report.")
    for marker, command in ctx["a_steps"]:
        if marker in last and not sent(command(ctx, text)):
            return Reply(shell=command(ctx, text))
    return Reply("A idle.")


def _ids(text):
    return re.findall(r"request=(rq-[0-9a-f]{6})", text)


class _RequestScenario(WatchWakeScenario):
    surface = "tmux"
    HOLD = 0

    def __init__(self):
        super().__init__()
        self.nonce = uuid.uuid4().hex[:6]
        self.ctx = None

    # --- script ------------------------------------------------------------
    def script(self, run):
        self.ctx = self.make_context(run)
        return lambda record, earlier: answer(self.ctx, record, earlier)

    def make_context(self, run):
        raise NotImplementedError

    def body_file(self, run, name, body):
        path = Path(run.home.workspace) / name
        path.write_text(body, encoding="utf-8")
        return path

    # --- evidence ----------------------------------------------------------
    def records(self, run):
        env = run.home.env
        override = env.get("AGENT_PEER_LEDGER_ROOT")
        directory = (Path(override) if override else stable_state_root(env)) / "peer-messages" / "requests"
        found = {}
        for path in sorted(directory.glob("rq-*.json")) if directory.is_dir() else ():
            with contextlib.suppress(OSError, ValueError):
                found[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        return found

    def b_requests(self, run):
        return [r for r in run.main_requests() if B_MARK in r["text"] and A_MARK not in r["text"]]

    def a_requests(self, run):
        return [r for r in run.main_requests() if A_MARK in r["text"]]

    def panels(self, run):
        """Both terminals' panel lines for the request (the requester's ``→``, the receiver's ``◂``)."""
        a, b = run.tui.screen(), self.target_tui.screen()
        return {"requester": [l for l in a.splitlines() if "→" in l and self.nonce in l][:3],
                "receiver": [l for l in b.splitlines() if "◂" in l and self.nonce in l][:3],
                "requester_screen": a[-1500:], "receiver_screen": b[-1500:]}

    def start_a(self, run, text):
        return run.prompt(f"{A_MARK} {text}")

    def wait_state(self, run, request_id, state, timeout=90):
        return wait_for(lambda: self.records(run).get(request_id, {}).get("state") == state, timeout, 1)


class RequestFlowScenario(_RequestScenario):
    name = "request-flow"
    summary = ("A requests work from B: one line in B's conversation, the body only through B's own "
               "show call, a partial and a final report each waking A once, both panels showing it.")

    def make_context(self, run):
        body = self.body_file(run, "request-body-1.txt", f"BODY-1-{self.nonce}\nsecond body line\n")
        title = f"검토 제목 {self.nonce}"
        target = self.target_address

        def send(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote(title)} "
                    f"--rev {shlex.quote('판1-' + self.nonce)} --body-file {shlex.quote(str(body))}")

        return {"nonce": self.nonce, "partial": True,
                "a_steps": [(f"{A_MARK} send", send)]}

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            run.note("private tmux receiver was not created")
            return "inconclusive"
        before = len(run.main_requests())
        self.start_a(run, "send the request")
        sent = wait_for(lambda: self.records(run), 90)
        if not sent:
            run.note("no request record was written")
            return "inconclusive"
        request_id = next(iter(sent))
        ev["request_id"] = request_id
        body_nonce = f"BODY-1-{self.nonce}"
        line_seen = wait_for(lambda: next((r for r in self.b_requests(run)
                                           if f"[요청 {request_id}" in r["last_text"]), None), 90)
        ev["receiver_line_request"] = bool(line_seen)
        ev["body_in_line_request"] = bool(line_seen and body_nonce in line_seen["text"])
        consumed = wait_for(lambda: next((r for r in self.b_requests(run)
                                          if body_nonce in r["last_text"] and "(sealed body)" in r["last_text"]),
                                         None), 90)
        ev["body_consumed_by_model"] = bool(consumed)
        ev["panels_open"] = self.panels(run)
        partial_wake = wait_for(lambda: next((r for r in self.a_requests(run)
                                              if f"[요청 답 {request_id}" in r["last_text"]
                                              and "일부" in r["last_text"]), None), 120)
        ev["partial_wake"] = bool(partial_wake)
        ev["state_after_partial"] = self.records(run).get(request_id, {}).get("state")
        final_state = self.wait_state(run, request_id, "final", 150)
        time.sleep(4)
        record = self.records(run).get(request_id, {})
        wakes = [r for r in self.a_requests(run) if f"[요청 답 {request_id}" in r["last_text"]
                 and r["main_index"] >= before]
        ev["wake_counts"] = {"partial": sum("일부" in r["last_text"] for r in wakes),
                             "final": sum("최종" in r["last_text"] for r in wakes)}
        ev["reporter"] = [(report.get("reporter") or {}).get("session_id") for report in record.get("reports", [])]
        ev["final_state"] = record.get("state")
        ev["panels_closed"] = self.panels(run)
        required = (ev["receiver_line_request"] and not ev["body_in_line_request"]
                    and ev["body_consumed_by_model"] and ev["partial_wake"]
                    and ev["state_after_partial"] == "partial" and final_state
                    and ev["wake_counts"] == {"partial": 1, "final": 1})
        if not required:
            run.note("the request did not complete as one sealed, attributed, once-woken line")
            return "fail"
        panels_shown = bool(ev["panels_open"]["requester"] and ev["panels_open"]["receiver"])
        if not panels_shown:
            run.note("the request flow passed; the panel did not show the open request on both sides")
            return "gap"
        return "pass"


class RequestRevisionCancelScenario(_RequestScenario):
    name = "request-revision-cancel"
    summary = ("Revision 2 supersedes revision 1 while B works; B's late report stays on revision 1; "
               "an undelivered cancelled revision never reaches B; a cancel after delivery is one "
               "read-only line.")
    HOLD = 25

    def make_context(self, run):
        first = self.body_file(run, "rev-1.txt", f"BODY-REV1-{self.nonce}\n")
        second = self.body_file(run, "rev-2.txt", f"BODY-REV2-{self.nonce}\n")
        target = self.target_address
        title = f"판 검토 {self.nonce}"

        def new(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote(title)} "
                    f"--rev {shlex.quote('판1-' + self.nonce)} --body-file {first}")

        def revise(ctx, text):
            return (f"{STEWARD} request {_ids(text)[0]} --rev {shlex.quote('판2-' + self.nonce)} "
                    f"--body-file {second}")

        def cancel(ctx, text):
            return f"{STEWARD} request {_ids(text)[0]} --cancel --reason {shlex.quote('필요 없음 ' + self.nonce)}"

        def second(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote('두번째 ' + self.nonce)} "
                    f"--body-file {second_body}")

        def cancel_second(ctx, text):
            return f"{STEWARD} request {_ids(text)[-1]} --cancel"

        keep_body = self.body_file(run, "rev-keep.txt", f"BODY-KEEP-{self.nonce}\n")

        def third(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote('세번째 ' + self.nonce)} "
                    f"--body-file {keep_body}")

        def cancel_third(ctx, text):
            return f"{STEWARD} request {_ids(text)[-1]} --cancel --reason {shlex.quote('취소 ' + self.nonce)}"

        second_body = self.body_file(run, "rev-other.txt", f"BODY-OTHER-{self.nonce}\n")
        # Longer markers first: "send2" and "cancel2" contain "send" and "cancel".
        return {"nonce": self.nonce, "hold": self.HOLD, "partial": False,
                "a_steps": [(f"{A_MARK} send3", third), (f"{A_MARK} cancel3", cancel_third),
                            (f"{A_MARK} send2", second), (f"{A_MARK} cancel2", cancel_second),
                            (f"{A_MARK} send", new), (f"{A_MARK} revise", revise)]}

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            return "inconclusive"
        self.start_a(run, "send")
        first = wait_for(lambda: next(iter(self.records(run)), None), 90)
        if not first:
            return "inconclusive"
        ev["request_id"] = first
        busy = wait_for(lambda: any("sleep" in (r["reply"].get("shell") or "") for r in self.b_requests(run)), 90)
        if not busy:
            run.note("B never started its long step")
            return "inconclusive"
        # While B works: revision 2 supersedes revision 1, and a second request is cancelled
        # before B can receive it.
        self.start_a(run, f"revise {first}")
        wait_for(lambda: len(self.records(run).get(first, {}).get("revisions", [])) >= 2, 60)
        self.start_a(run, "send2")
        other = wait_for(lambda: next((i for i in self.records(run) if i != first), None), 60)
        if other:
            self.start_a(run, f"cancel2 {other}")
            self.wait_state(run, other, "cancelled", 60)
        ev["other_request"] = other
        ev["other_state"] = self.records(run).get(other, {}).get("state") if other else None
        late = wait_for(lambda: any(report.get("late") for report in
                                    self.records(run).get(first, {}).get("reports", [])), 150)
        record = self.records(run).get(first, {})
        ev["late_report_kept"] = bool(late)
        ev["state_after_late"] = record.get("state")
        ev["late_on_revision"] = [report.get("revision") for report in record.get("reports", []) if report.get("late")]
        final = self.wait_state(run, first, "final", 150)
        ev["final_state"] = self.records(run).get(first, {}).get("state")
        ev["cancelled_request_reached_b"] = bool(other and any(other in r["text"] for r in self.b_requests(run)))
        # A cancel after delivery: B received and showed a third request and holds it open.
        self.start_a(run, "send3")
        third = wait_for(lambda: next((i for i in self.records(run) if i not in (first, other)), None), 60)
        shown = wait_for(lambda: next((r for r in self.b_requests(run)
                                       if f"BODY-KEEP-{self.nonce}" in r["last_text"]), None), 90)
        ev["third_shown_to_model"] = bool(shown)
        if third and shown:
            self.start_a(run, f"cancel3 {third}")
        notice = wait_for(lambda: next((r for r in self.b_requests(run)
                                        if third and f"[요청 취소 {third}]" in r["last_text"]), None), 90)
        ev["cancel_notice_line"] = bool(notice)
        ev["stop_word_in_notice"] = bool(notice and "멈" in notice["last_text"])
        ev["third_state"] = self.records(run).get(third, {}).get("state") if third else None
        ev["panels"] = self.panels(run)
        ok = (ev["late_report_kept"] and ev["state_after_late"] in ("open", "partial")
              and ev["late_on_revision"] == [1] and final and ev["other_state"] == "cancelled"
              and not ev["cancelled_request_reached_b"] and ev["cancel_notice_line"]
              and not ev["stop_word_in_notice"] and ev["third_state"] == "cancelled")
        return "pass" if ok else "fail"


class RequestHandoffScenario(_RequestScenario):
    name = "request-handoff"
    summary = ("B is replaced by B' through a private lineage row: the open request follows and B' may "
               "report; a session with no lineage row may not.")

    def make_context(self, run):
        body = self.body_file(run, "handoff-body.txt", f"BODY-HANDOFF-{self.nonce}\n")
        target = self.target_address

        def send(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote('인계 ' + self.nonce)} "
                    f"--body-file {body}")

        return {"nonce": self.nonce, "hold": 40, "partial": False, "a_steps": [(f"{A_MARK} send", send)]}

    def lineage_row(self, run, old, new):
        env = run.home.env
        state = Path(env["AGENT_PEER_LEDGER_ROOT"]).parent if env.get("AGENT_PEER_LEDGER_ROOT") \
            else stable_state_root(env).parent
        directory = state / "session-tidy" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        row = {"event": "succession", "source": "tidy-clear", "harness": run.harness,
               "from": old, "sid": new, "ts": int(time.time())}
        with open(directory / f"scenario-{self.nonce}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def run_steward(self, run, *argv, sid):
        import subprocess
        env = {**run.home.env, "AGENT_SESSION_ID": sid}
        return subprocess.run([sys.executable, str(Path(__file__).with_name("peer-steward.py")), *argv],
                              env=env, capture_output=True, text=True, timeout=60)

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            return "inconclusive"
        self.start_a(run, "send")
        first = wait_for(lambda: next(iter(self.records(run)), None), 90)
        if not first:
            return "inconclusive"
        record = self.records(run)[first]
        old_sid = record["recipient"]["session_id"]
        stranger = "sid-no-lineage-" + self.nonce
        refused = self.run_steward(run, "report", first, "--result", "x", sid=stranger)
        ev["stranger_report"] = (refused.stdout or "")[:300]
        ev["stranger_refused"] = "reporter-not-recipient" in (refused.stdout or "")
        successor = "sid-successor-" + self.nonce
        self.lineage_row(run, old_sid, successor)
        listing = self.run_steward(run, "show", sid=successor)
        ev["successor_lists_request"] = first in (listing.stdout or "")
        accepted = self.run_steward(run, "report", first, "--partial", "--result", "HANDOFF-" + self.nonce, sid=successor)
        ev["successor_report"] = (accepted.stdout or "")[:300]
        ev["successor_accepted"] = "kind=partial" in (accepted.stdout or "")
        after = self.records(run).get(first, {})
        ev["reporter"] = [(r.get("reporter") or {}).get("session_id") for r in after.get("reports", [])]
        ev["stranger_has_no_report"] = not any(
            (r.get("reporter") or {}).get("session_id") == stranger for r in after.get("reports", []))
        ok = (ev["stranger_refused"] and ev["successor_lists_request"] and ev["successor_accepted"]
              and ev["stranger_has_no_report"] and ev["reporter"] == [successor])
        return "pass" if ok else "fail"


SCENARIOS = [RequestFlowScenario, RequestRevisionCancelScenario, RequestHandoffScenario]
