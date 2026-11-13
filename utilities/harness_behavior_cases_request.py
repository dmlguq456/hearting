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
    as one read-only line that does not say "stop". Codex's native queue counts a line as delivered
    at once (PG-59), so its cancel is judged as a cancel after delivery. Revision 2's body is read by B
    (`show`) once the line reaches it, and its final report closes the request. B remembers every request
line it received: after a report, a revision it was told of and has not opened is shown and reported.
Codex's busy step is shorter than its shell tool's 10s return; a revision 2 that went out after the
busy step ended did not test the late-report path and is ``inconclusive``.
``request-handoff``
    B is replaced by B' through a private session-tidy lineage row. Pass: the open request
    is B' 's and B' 's report is accepted. Counter-case: a session with no lineage row
    cannot report (``reporter-not-recipient``) and the request stays with B.

The panel request lines (the requester's ``→`` and the receiver's ``◂``, in the route box's own
form: mark, other session, the request's title (cut with `…` on a narrow panel), `` · ``, state) are read from both whole
terminal screens, which are written to the output folder; a panel that does not show them is a
``gap`` (outside the session shell there is no Hearting panel, so none is asserted: `panel: not-applicable`). Outside the session shell, the declared limits (PG-59: a Claude receiver at rest takes no
request; PG-4/33: an OpenCode requester is not woken by every report) are ``gap``, not ``fail``.
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


# A busy step every harness runs (Claude refuses a bare ``sleep N`` followed by another command).
def pause(seconds):
    return 'python3 -c "import time; time.sleep(%d)"' % seconds


def busy_step(seconds):
    return 'python3 -c "import time; time.sleep(%d); print(\'B-WORK-DONE\')"' % seconds


# The route box's request line: mark, the other session's `[tag]` (kept when the line is cut), the
# title (cut with `…` on a narrow panel), ` · `, the state.
PANEL_REQUEST_LINE = re.compile(r"│\s*([→◂✓✕])(?: (\[[^\]│]+\]))?(?: ([^│]*?))? · ([^│]*?)\s*│")


def turn(record):
    """Every input of this model turn (user, developer and tool items after the model's last
    output), else the newest item: Codex puts its guidance after the user line."""
    return record.get("turn_text") or record["last_text"]


def _title_matches(shown, titles):
    """The line's title is a recorded title, or its front part cut with ``…`` (the nonce at the end
    of a title does not survive a narrow panel)."""
    shown = " ".join((shown or "").split())
    if not shown:
        return False
    cut = shown.endswith("…")
    front = shown[:-1].rstrip() if cut else shown
    for title in ([titles] if isinstance(titles, str) else titles):
        title = " ".join((title or "").split())
        if title and (title == shown or (cut and front and title.startswith(front))):
            return True
    return False


def panel_request_lines(screen, titles, marks):
    """The route box's request lines of the given marks whose title is a recorded request title
    (whole, or cut with ``…``). ``titles`` is one title or several."""
    found = []
    for row in screen.splitlines():
        for match in PANEL_REQUEST_LINE.finditer(row):
            if match.group(1) in marks and _title_matches(match.group(3), titles):
                found.append(match.group(0).strip("│ ").strip())
    return found


SHOWN_COMMAND = re.compile(r"^report-(final|partial|reject): (.+)$", re.MULTILINE)


def shown_command(text, request_id, kind, result, *, first=False):
    """The report command a `show` output printed (`report-<kind>: <command>`), with the model's
    one line put in place of its `<one line>` placeholder, as a model uses the command it was
    given. ``first`` takes the earliest shown (the revision B began working on), else the newest.
    ``""`` when no `show` output in ``text`` printed one for this request."""
    found = [m.group(2) for m in SHOWN_COMMAND.finditer(text)
             if m.group(1) == kind and f" report {request_id} " in m.group(2)]
    if not found:
        return ""
    return (found[0] if first else found[-1]).replace("<one line>", result)


# The revision a request line names: `[요청 rq-…] … │ 판 N <label> │ …` (outside the bracket).
REVISION_LINE = re.compile(r"\[요청 (rq-[0-9a-f]{6})\][^\n]*? │ 판 (\d+)(?= |│|$)", re.MULTILINE)
# A `show` output: `request=<id> …` up to its `--- revision N (sealed body) ---`.
OPENED_BODY = re.compile(r"request=(rq-[0-9a-f]{6}) (?:(?!request=rq-).)*?--- revision (\d+) \(sealed body\) ---",
                         re.DOTALL)


def pending_revisions(inputs):
    """``{request id: [revision, …]}`` of the revisions B was told of by a request line in any input
    it has received but has not opened with `show` (no `--- revision N (sealed body) ---` yet)."""
    told = {}
    for request_id, number in REVISION_LINE.findall(inputs):
        told.setdefault(request_id, set()).add(int(number))
    opened = {}
    for request_id, number in OPENED_BODY.findall(inputs):
        opened.setdefault(request_id, set()).add(int(number))
    return {request_id: sorted(told[request_id] - opened.get(request_id, set()))
            for request_id in told if told[request_id] - opened.get(request_id, set())}


def _shells(records, mark):
    return [(r["reply"].get("shell") or "") for r in records if mark in r["text"]]


def answer(ctx, record, earlier):
    """The scripted model for both sessions. ``ctx`` carries nonce, target and file names."""
    text, last, kind = record["text"], turn(record), record["last_kind"]
    nonce = ctx["nonce"]
    if B_MARK in text and A_MARK not in text:
        # Everything B has received so far: the whole conversation, this turn, and every earlier turn.
        inputs = "\n".join([text, last] + [turn(r) for r in earlier if B_MARK in r["text"] and A_MARK not in r["text"]])
        return _answer_b(ctx, text, last, kind, _shells(earlier, B_MARK), nonce, inputs)
    return _answer_a(ctx, text, last, kind, _shells(earlier, A_MARK), nonce)


def _answer_b(ctx, text, last, kind, shells, nonce, inputs=""):
    sent = lambda needle: any(needle in shell for shell in shells)  # noqa: E731
    if kind == "tool":
        done = re.search(r"report=(rq-[0-9a-f]{6}) kind=(\w+)", last)
        if "(sealed body)" in last and f"BODY-KEEP-{nonce}" in last:
            return Reply("B holds this request open.")  # stays open for the cancel-after-delivery case
        if "(sealed body)" in last:
            request_id = re.findall(r"request=(rq-[0-9a-f]{6})", last)[-1]
            if ctx.get("hold") and not sent("time.sleep"):
                return Reply(shell=busy_step(ctx["hold"]))
            # B's report is the command this `show` printed for the revision it just read.
            if ctx.get("partial", True) and not sent("--partial"):
                command = shown_command(last, request_id, "partial", f'"PARTIAL-{nonce}"')
            else:
                command = shown_command(last, request_id, "final", f'"FINAL-{nonce}"')
            return Reply(shell=command) if command else Reply("B found no report command in show.")
        if "B-WORK-DONE" in last:
            # The long step ended: B reports the work it began, with the command the `show` of
            # that revision printed (its own `--rev`), not whatever line arrived meanwhile.
            request_id = ctx.get("report_id") or re.findall(r"rq-[0-9a-f]{6}", text)[0]
            command = shown_command(text, request_id, "final", f'"LATE-{nonce}"', first=True)
            return Reply(shell=command) if command else Reply("B found no report command in show.")
        if done and done.group(2) == "partial" and not sent('--result "FINAL'):
            command = shown_command(text, done.group(1), "final", f'"FINAL-{nonce}"')
            return Reply(shell=f"{pause(3)}; {command}") if command else Reply("B step done.")
        if done:
            # B finished a report. A revision it was told of by a line while it worked, and has not
            # opened, is its next work: `show` it (once per revision it was told of), then report with
            # the command that `show` prints.
            for request_id in pending_revisions(inputs):
                told = len(set(re.findall(rf"\[요청 {request_id}\][^\n]*? │ 판 (\d+)", inputs)) | {"1"})
                if sum(f"show {request_id}" in shell for shell in shells) < told:
                    return Reply(shell=f"{STEWARD} show {request_id}")
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
        if marker in last:  # the first (longest) marker is this prompt's step; never the next one's
            if not sent(command(ctx, text)):
                return Reply(shell=command(ctx, text))
            break
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
        """Built before the home, the receiver TUI and ``target_address`` exist (``Run.execute`` order):
        it only reads ``self.ctx``, which ``prepare`` fills once those are settled."""
        return self._scripted

    def _scripted(self, record, earlier):
        if self.ctx is None:  # the receiver's own start-up turn (inside prepare): nothing to act on yet
            return Reply("initialized.")
        return answer(self.ctx, record, earlier)

    def prepare(self, run):
        super().prepare(run)  # starts the receiving TUI and fixes target_address
        self.ctx = self.make_context(run)  # needs run.home.workspace and target_address

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

    def panels(self, run, tag):
        """Both terminals' whole screens (written to the output folder) and the request lines the
        route box shows: the requester's ``→`` (open) or ``✓`` (closed), the receiver's ``◂``."""
        a, b = run.tui.screen(), self.target_tui.screen()
        folder = Path(run.output) / f"{run.harness}-{self.name}"
        folder.mkdir(parents=True, exist_ok=True)
        files = {}
        for who, screen in (("requester", a), ("receiver", b)):
            path = folder / f"panel-{tag}-{who}.txt"
            path.write_text(screen, encoding="utf-8")
            files[who + "_screen_file"] = str(path)
        titles = [rec.get("title") or "" for rec in self.records(run).values()]
        return {"requester": panel_request_lines(a, titles, "→✓✕"),
                "receiver": panel_request_lines(b, titles, "◂"), **files}

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

    def outside_shell_limit(self, run):
        """The declared limits of a surface without the session shell (PG-59, PG-4/33)."""
        return run.harness in ("claude", "opencode") and "session_shell" not in run.evidence

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
                                           if f"[요청 {request_id}" in turn(r)), None), 90)
        ev["receiver_line_request"] = bool(line_seen)
        ev["body_in_line_request"] = bool(line_seen and body_nonce in line_seen["text"])
        consumed = wait_for(lambda: next((r for r in self.b_requests(run)
                                          if body_nonce in turn(r) and "(sealed body)" in turn(r)),
                                         None), 90)
        ev["body_consumed_by_model"] = bool(consumed)
        ev["panels_open"] = self.panels(run, "open")
        partial_wake = wait_for(lambda: next((r for r in self.a_requests(run)
                                              if f"[요청 답 {request_id}" in turn(r)
                                              and "일부" in turn(r)), None), 120)
        ev["partial_wake"] = bool(partial_wake)
        ev["state_after_partial"] = self.records(run).get(request_id, {}).get("state")
        final_state = self.wait_state(run, request_id, "final", 150)
        time.sleep(4)
        record = self.records(run).get(request_id, {})
        wakes = [r for r in self.a_requests(run) if f"[요청 답 {request_id}" in turn(r)
                 and r["main_index"] >= before]
        ev["wake_counts"] = {"partial": sum("일부" in turn(r) for r in wakes),
                             "final": sum("최종" in turn(r) for r in wakes)}
        recipient = (record.get("recipient") or {}).get("session_id")
        ev["reporter"] = [(report.get("reporter") or {}).get("session_id") for report in record.get("reports", [])]
        ev["report_kinds"] = [report.get("kind") for report in record.get("reports", [])]
        ev["final_state"] = record.get("state")
        ev["panels_closed"] = self.panels(run, "closed")
        # What is wrong whatever the surface: the body in the line, a body never consumed after
        # the line arrived, a report not attributed to B, a double wake. A request B never
        # received, or a wake that did not come, is a declared limit outside the session shell.
        wrong, limited = [], []
        if ev["body_in_line_request"]:
            wrong.append("the body was in the one-line request")
        if not ev["receiver_line_request"]:
            (limited if self.outside_shell_limit(run) else wrong).append("B never received the request line")
        else:
            if not ev["body_consumed_by_model"]:
                wrong.append("B received the line but its model never consumed the sealed body")
            if ev["report_kinds"] != ["partial", "final"] or not final_state:
                wrong.append("the record does not hold B's partial then final report")
            if not recipient or ev["reporter"] != [recipient, recipient]:
                wrong.append("a report is not attributed to B")
            if max(ev["wake_counts"].values()) > 1:
                wrong.append("a report woke A more than once")
            elif min(ev["wake_counts"].values()) < 1:
                (limited if self.outside_shell_limit(run) else wrong).append("a report did not wake A")
        if wrong:
            run.note("the request did not complete as one sealed, attributed, once-woken line: "
                     + "; ".join(wrong))
            return "fail"
        if limited:
            run.note("PG-59 outside the session shell: " + "; ".join(limited))
            return "gap"
        if "session_shell" not in run.evidence:
            # No session shell, no Hearting shell panel: the route box is not asserted there.
            ev["panel"] = "not-applicable (no session shell)"
            return "pass"
        shown = (ev["panels_open"]["requester"] and ev["panels_open"]["receiver"]
                 and any(line.startswith("✓") for line in ev["panels_closed"]["requester"]))
        if not shown:
            run.note("the request flow passed; the route box did not show the request on both sides "
                     "(open → and ◂, then the requester's ✓)")
            return "gap"
        return "pass"


class RequestRevisionCancelScenario(_RequestScenario):
    name = "request-revision-cancel"
    summary = ("Revision 2 supersedes revision 1 while B works; B's late report stays on revision 1; "
               "an undelivered cancelled revision never reaches B; a cancel after delivery is one "
               "read-only line.")
    HOLD = 25
    # Codex's shell tool returns `Process running with session ID …` after ~10s (r5), before the
    # step ends and `B-WORK-DONE` is printed: its busy step is shorter than that return.
    HOLD_BY_HARNESS = {"codex": 8}

    def hold_seconds(self, run):
        return self.HOLD_BY_HARNESS.get(getattr(run, "harness", None), self.HOLD)

    def make_context(self, run):
        rev1_body = self.body_file(run, "rev-1.txt", f"BODY-REV1-{self.nonce}\n")
        rev2_body = self.body_file(run, "rev-2.txt", f"BODY-REV2-{self.nonce}\n")
        other_body = self.body_file(run, "rev-other.txt", f"BODY-OTHER-{self.nonce}\n")
        keep_body = self.body_file(run, "rev-keep.txt", f"BODY-KEEP-{self.nonce}\n")
        target = self.target_address
        title = f"판 검토 {self.nonce}"

        def new(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote(title)} "
                    f"--rev {shlex.quote('판1-' + self.nonce)} --body-file {rev1_body}")

        def revise(ctx, text):
            return (f"{STEWARD} request {_ids(text)[0]} --rev {shlex.quote('판2-' + self.nonce)} "
                    f"--body-file {rev2_body}")

        def make_and_cancel(ctx, text):
            # One shell line: the request is created and cancelled before B can take it up. The
            # id comes from the first command's own output; only the cancel's output is shown.
            make = (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote('두번째 ' + self.nonce)} "
                    f"--body-file {other_body}")
            ident = "sed -n 's/^request=\\(rq-[0-9a-f]\\{6\\}\\) .*/\\1/p' | head -1"
            return f'rid=$({make} | {ident}) && test -n "$rid" && {STEWARD} request "$rid" --cancel'

        def third(ctx, text):
            return (f"{STEWARD} request {shlex.quote(target)} --title {shlex.quote('세번째 ' + self.nonce)} "
                    f"--body-file {keep_body}")

        def cancel_third(ctx, text):
            return f"{STEWARD} request {_ids(text)[-1]} --cancel --reason {shlex.quote('취소 ' + self.nonce)}"

        # Longer markers first: "send3" and "send2" contain "send".
        return {"nonce": self.nonce, "hold": self.hold_seconds(run), "partial": False,
                "a_steps": [(f"{A_MARK} send3", third), (f"{A_MARK} cancel3", cancel_third),
                            (f"{A_MARK} send2", make_and_cancel),
                            (f"{A_MARK} send", new), (f"{A_MARK} revise", revise)]}

    def duty(self, run, ref):
        """The message obligation of ``ref`` in the run's own state root (read-only)."""
        import peer_obligations
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(run.home.env)
        try:
            return peer_obligations.ObligationStore().get("message-" + ref) or {}
        except Exception:  # noqa: BLE001 - an unreadable duty is "not closed"
            return {}
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def drive(self, run):
        ev = run.evidence
        if self.target_tui is None or not self.target_address:
            return "inconclusive"
        self.start_a(run, "send")
        first = wait_for(lambda: next(iter(self.records(run)), None), 90)
        if not first:
            return "inconclusive"
        ev["request_id"] = first
        busy = wait_for(lambda: any("time.sleep" in (r["reply"].get("shell") or "")
                                    for r in self.b_requests(run)), 90)
        if not busy:
            run.note("B never started its long step")
            return "inconclusive"
        # While B works: revision 2 supersedes revision 1, and a second request is created and
        # cancelled by one shell line, so it is cancelled before B can take it up.
        self.start_a(run, f"revise {first}")
        wait_for(lambda: len(self.records(run).get(first, {}).get("revisions", [])) >= 2, 60)
        ev["revision_2_sent_while_busy"] = self.sent_while_busy(run, first)
        if ev["revision_2_sent_while_busy"] is False:
            run.note("revision 2 went out after B's busy step had ended, so the late-report path was not "
                     "exercised (not a failure): the busy step must outlast A's revise turn")
            return "inconclusive"
        self.start_a(run, "send2")
        other = wait_for(lambda: next((i for i in self.records(run) if i != first), None), 60)
        cancel_output = wait_for(lambda: next((m for m in (
            re.search(r"request=%s state=cancelled notice=(\S+)" % other, turn(r))
            for r in self.a_requests(run) if other and r["last_kind"] == "tool") if m), None), 60) if other else None
        ev["other_request"] = other
        ev["other_state"] = self.records(run).get(other, {}).get("state") if other else None
        ev["other_cancel_notice"] = cancel_output.group(1) if cancel_output else None
        late = wait_for(lambda: any(report.get("late") for report in
                                    self.records(run).get(first, {}).get("reports", [])), 150)
        record = self.records(run).get(first, {})
        ev["late_report_kept"] = bool(late)
        ev["state_after_late"] = record.get("state")
        ev["late_on_revision"] = [report.get("revision") for report in record.get("reports", []) if report.get("late")]
        final = self.wait_state(run, first, "final", 150)
        ev["final_state"] = self.records(run).get(first, {}).get("state")
        # Revision 2 reaches B once it is idle: B reads its sealed body and answers it.
        rev2_body = f"BODY-REV2-{self.nonce}"
        ev["rev2_body_consumed"] = bool(wait_for(lambda: any(
            rev2_body in turn(r) and "(sealed body)" in turn(r) for r in self.b_requests(run)), 90))
        # The cancelled request never reached B: not its line, not its body, in any model turn.
        other_body = f"BODY-OTHER-{self.nonce}"
        ev["cancelled_request_reached_b"] = bool(other and any(
            f"[요청 {other}" in turn(r) or other_body in turn(r) or f"[요청 {other}" in r["text"]
            or other_body in r["text"] for r in self.b_requests(run)))
        other_ref = (self.records(run).get(other, {}).get("revisions") or [{}])[0].get("transfer_ref") if other else None
        closed = wait_for(lambda: bool(other_ref) and (self.duty(run, other_ref).get("observation") or {})
                          .get("reason") == "request-withdrawn", 90) if other_ref and not self.queue_took_it(run, ev) else False
        ev["cancelled_delivery_closed_as_withdrawn"] = bool(closed)
        # A cancel after delivery: B received and showed a third request and holds it open.
        self.start_a(run, "send3")
        third = wait_for(lambda: next((i for i in self.records(run) if i not in (first, other)), None), 60)
        shown = wait_for(lambda: next((r for r in self.b_requests(run)
                                       if f"BODY-KEEP-{self.nonce}" in turn(r)), None), 90)
        ev["third_shown_to_model"] = bool(shown)
        if third and shown:
            self.start_a(run, f"cancel3 {third}")
        notice = wait_for(lambda: next((r for r in self.b_requests(run)
                                        if third and f"[요청 취소 {third}]" in turn(r)), None), 90)
        ev["cancel_notice_line"] = bool(notice)
        ev["stop_word_in_notice"] = bool(notice and "멈" in turn(notice))
        ev["third_state"] = self.records(run).get(third, {}).get("state") if third else None
        ev["panels"] = self.panels(run, "end")
        # A cancel before delivery needs no notice and the request never reaches B. Codex's native
        # queue counts a line as delivered the moment it takes it (PG-59), so there the cancel
        # follows as its own line: B gets the request line, then one `[요청 취소 <id>]` line without
        # "멈", and the request is `cancelled`. Claude and OpenCode keep the strict form.
        if self.queue_took_it(run, ev):
            ev["cancel_after_delivery"] = self.cancel_after_delivery(run, other)
            before = (ev["other_state"] == "cancelled" and ev["cancel_after_delivery"]["notice_after_request"]
                      and not ev["cancel_after_delivery"]["stop_word"])
            if before:
                run.note("codex native queue accepted it at once (PG-59): the cancel went as its own line")
        else:
            before = (ev["other_state"] == "cancelled" and ev["other_cancel_notice"] == "not-needed"
                      and not ev["cancelled_request_reached_b"] and ev["cancelled_delivery_closed_as_withdrawn"])
        ok = (ev["late_report_kept"] and ev["state_after_late"] in ("open", "partial")
              and ev["late_on_revision"] == [1] and final and ev["rev2_body_consumed"] and before
              and ev["cancel_notice_line"]
              and not ev["stop_word_in_notice"] and ev["third_state"] == "cancelled")
        return "pass" if ok else "fail"

    def sent_while_busy(self, run, request_id):
        """Whether revision 2 was created before B's busy step ended (B's model request that carries
        the step's tool result). ``None`` when the times are not recorded; ``True`` while B is
        still in the step."""
        revisions = self.records(run).get(request_id, {}).get("revisions") or []
        created = (revisions[1] if len(revisions) > 1 else {}).get("created")
        turns = self.b_requests(run)
        start = next((r for r in turns if "time.sleep" in (r["reply"].get("shell") or "")), None)
        if created is None or start is None:
            return None
        after = next((r for r in turns if r["main_index"] > start["main_index"] and r["last_kind"] == "tool"), None)
        if after is None or after.get("at") is None:
            return True
        return created < after["at"]

    def queue_took_it(self, run, ev):
        """Codex's cancel output names the delivery (`queued-not-delivered`/`delivered`) instead of
        `not-needed`: the native queue had already taken the line (PG-59)."""
        return (run.harness == "codex"
                and ev.get("other_cancel_notice") in ("queued-not-delivered", "delivered"))

    def cancel_after_delivery(self, run, other):
        """B got the request line, then one cancel line (no "멈") for ``other``."""
        got = {"request_index": None, "cancel_index": None, "stop_word": False}
        wait_for(lambda: any(f"[요청 취소 {other}]" in turn(r) for r in self.b_requests(run)), 90)
        for r in self.b_requests(run):
            if got["request_index"] is None and f"[요청 {other}" in turn(r):
                got["request_index"] = r["main_index"]
            if f"[요청 취소 {other}]" in turn(r):
                got["cancel_index"] = r["main_index"]
                got["stop_word"] = "멈" in turn(r)
        got["notice_after_request"] = (got["request_index"] is not None and got["cancel_index"] is not None
                                       and got["request_index"] <= got["cancel_index"])
        return got


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
        """One private session-tidy succession row, the lineage Hearting already confirms."""
        env = run.home.env
        base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".local" / "state")
        directory = Path(base) / "hearting" / "session-tidy" / "sessions"
        directory.mkdir(parents=True, exist_ok=True)
        row = {"event": "succession", "source": "tidy-clear", "harness": run.harness,
               "from": old, "sid": new, "ts": int(time.time())}
        with open(directory / f"scenario-{self.nonce}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def caller_env(self, run, sid):
        """The environment a session of this harness gives its commands: the one native id variable
        the shared resolver (``interactive_parent_identity``) reads, and the harness it names."""
        from harness_capabilities import session_env
        names = session_env().get(run.harness) or ()
        env = {key: value for key, value in run.home.env.items()
               if not any(key in variables for variables in session_env().values())}
        env.update({names[0]: sid, "AGENT_DISPATCH_CALLER_HARNESS": run.harness})
        return env

    def run_steward(self, run, *argv, sid):
        import subprocess
        return subprocess.run([sys.executable, str(Path(__file__).with_name("peer-steward.py")), *argv],
                              env=self.caller_env(run, sid), capture_output=True, text=True, timeout=60)

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
        # The successor uses the command the numberless `show` list printed for the request
        # (`report <rq> --rev N --result "<one line>"`), adding only `--partial` and its result.
        shown = next((line.strip() for line in (listing.stdout or "").splitlines()
                      if f" report {first} --rev " in line), "")
        ev["successor_listed_command"] = shown
        argv = []
        if shown:
            parts = shlex.split(shown)
            argv = [("HANDOFF-" + self.nonce) if part == "<one line>" else part
                    for part in parts[parts.index("report"):]]
            argv.insert(argv.index("--result"), "--partial")
        # A report answers a revision delivered to the receiver (or its confirmed predecessor):
        # wait for B to take the line in, then report as the successor.
        wait_for(lambda: any(f"[요청 {first}" in turn(r) for r in self.b_requests(run)), 90)
        accepted = None
        for _attempt in range(10):
            accepted = self.run_steward(run, *argv, sid=successor) if argv else listing
            if "kind=partial" in (accepted.stdout or "") or "report-revision-unreceived" not in (accepted.stdout or ""):
                break
            time.sleep(3)
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
