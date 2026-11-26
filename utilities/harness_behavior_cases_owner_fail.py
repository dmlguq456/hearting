#!/usr/bin/env python3
"""What a parent can say to a route whose work ended without a PASS, on the three harnesses
(`harness_behavior_scenarios.py`). The owner is a real registered `solo·quick` owner started through the harness's
dispatch wrapper and supervisor, the opening session is a shell carrying one native session id (`HeadlessParent`),
and the model is the loopback fixture.

``owner-fail-close-headless-parent``
    The owner writes its report, commits it and ends FAIL. `start --route` offers `correction_command` and
    `close_command` and says that `close_command` ends the route as it is. `close_command` closes the route with
    its gate unproven: the cycle is sealed `abandoned`, the workflow ledger reads CANCELLED, the next
    `start --route` reads `route-closed-unproven`, and the same close run again changes nothing.

``owner-follow-up-fail-headless-parent``
    The owner ends PASS and the route closes. A later request (`correct --route`) runs turn 2, which ends FAIL.
    The parent's answer (`correct --route` again) continues turn 2 in a replacement owner that receives the answer
    first, the way a first turn's FAIL is answered; it ends PASS and the route closes on it.

``owner-follow-up-bad-handoff-headless-parent``
    The same, but turn 2 ends without its three-line handoff: `start --route` continues it once on the runtime's
    observation (as a first turn's format error is), that owner ends FAIL, and the parent's answer continues it
    again; the route closes on the answer's PASS.

``unit-continue-bilingual-verdict``
    The ``unit-continue`` run with the review's verdict line written in both forms on one line
    (`Final verdict: changes requested — head <sha> · 최종 판정: 수정 후 병합 — head <sha>`): the end reads as a
    finished review, the route closes, and `correct --route` takes the closure check as the route's next turn.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES, _base, registry_rows  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402
from harness_behavior_cases_owner_continue import HeadlessParent, OwnerContinueScenario  # noqa: E402
from harness_behavior_cases_unit_continue import UnitContinueScenario  # noqa: E402

WORK = "SCENARIO-OF-WORK"
SLUG = "hb-owner-fail"
CAMPAIGN = "hb-of-stream"
START, RESTART, CLOSE, CONT, ANSWER = "OF-START", "OF-RESTART", "OF-CLOSE", "OF-CONT", "OF-ANSWER"
T2_TEXT = "HB-OF-T2-TEXT also add a second line to hb_of_work.txt and report again."
ANSWER_TEXT = "HB-OF-ANSWER the blocker is cleared; add the line hb-of-final and report again."
BLOCKER = "HB-OF-BLOCKER the fixture check of the second line failed"
OUTPUT = re.compile(r"artifact_output_dir: (\S+)")
OBSERVED = "not an answer or an approval"     # the runtime's observation a continued format error is given


def _phase(text: str) -> str:
    """Which owner request this is: the newest of the markers its conversation carries."""
    marks = {"t2": text.rfind("HB-OF-T2-TEXT"), "answer": text.rfind("HB-OF-ANSWER"), "observed": text.rfind(OBSERVED)}
    best = max(marks, key=marks.get)
    return best if marks[best] >= 0 else "t1"


def _json_objects(text: str) -> list:
    found, decoder, index = [], json.JSONDecoder(), 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text[start:])
        except ValueError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = start + end


class OwnerFailCloseScenario(HeadlessParent, OwnerContinueScenario):
    name = "owner-fail-close-headless-parent"
    summary = ("A solo·quick owner ends FAIL; `close_command` closes its route unproven, seals the cycle abandoned "
               "and records the ledger CANCELLED; `start --route` then reads route-closed-unproven.")
    variant = "close"          # "close" | "follow-up-fail" | "follow-up-bad"
    WAIT = 420

    # -- the parent -------------------------------------------------------------------------------
    def parent_command(self, run, marker):
        files = self.route_files(run)
        route_id = files[0].stem if files else "none"

        def capability_route(*argv):
            return " ".join(shlex.quote(x) for x in [sys.executable, str(_base().SOURCE / "utilities" /
                                                                          "capability-route.py"), *argv])
        if marker == START:
            return capability_route(
                "compose", "--start", "--shape", "solo", "--intensity", "quick", "--capability",
                "autopilot-code", "--pin", f"owner={run.harness}", "--pin", f"worker={run.harness}",
                "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--campaign-key", CAMPAIGN,
                "--slug", SLUG)
        if marker == RESTART:
            return capability_route("start", "--route", route_id)
        if marker == CLOSE:
            for text in reversed(self.parent_outputs):          # the command the receipt printed, as given
                for obj in reversed(_json_objects(text)):
                    if obj.get("close_command"):
                        return obj["close_command"]
            return capability_route("close", "--route", str(files[0]) if files else "none")
        name = "answer.md" if marker == ANSWER else "cont.md"
        return capability_route("correct", "--route", route_id, "--message-file", str(run.home.root / name))

    # -- the fixture model ------------------------------------------------------------------------
    def script(self, run):
        self.worker_end = threading.Event()
        self.ran = set()
        self.parent_outputs = []
        cell = self

        def owner(record):
            text = record["text"]
            phase = _phase(text)
            outputs = OUTPUT.findall(text)
            target = (outputs[-1] if outputs else "/nonexistent") + "/of_report.md"
            if phase == "observed":                       # the runtime's observation alone: the owner judges again
                return Reply(f"artifact: {target}\nverdict: FAIL\nblocker: {BLOCKER}")
            if record["last_kind"] != "tool":
                line = {"t1": "first line", "t2": "second line HB-OF-T2-DONE", "answer": "hb-of-final"}[phase]
                return Reply(shell=(f"mkdir -p {shlex.quote(target.rsplit('/', 1)[0])} && "
                                    f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(target)} && "
                                    f"printf '%s\\n' {shlex.quote(line)} >> hb_of_work.txt && git add hb_of_work.txt && "
                                    f"git commit -q -m {shlex.quote(phase)} && git rev-parse --short HEAD"))
            if phase == "t1":
                verdict = "FAIL" if cell.variant == "close" else "PASS"
            elif phase == "t2":
                if cell.variant == "follow-up-bad":
                    return Reply("The second line is written; I stop here.")     # no three-line handoff
                verdict = "FAIL"
            else:
                verdict = "PASS"
            blocker = BLOCKER if verdict == "FAIL" else "none"
            return Reply(f"artifact: {target}\nverdict: {verdict}\nblocker: {blocker}")

        def answer(record, earlier):
            if unit.side_request(record):
                return Reply("TITLE: fixture owner fail\nNOW: fixture")
            if "Dispatch metadata:" in record["text"] and WORK in record["text"]:
                return owner(record)
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        unit.UnitRouteScenario.prepare(self, run)
        root = run.home.root
        (root / "brief.md").write_text(f"{WORK}: add one line to hb_of_work.txt in the repository, commit it and "
                                       "report.\n", encoding="utf-8")
        (root / "cont.md").write_text(T2_TEXT + "\n", encoding="utf-8")
        (root / "answer.md").write_text(ANSWER_TEXT + "\n", encoding="utf-8")

    # -- what the run reads -----------------------------------------------------------------------
    def unit_rows(self):
        return [row for row in registry_rows(self.jobs) if SLUG in row.get("slug", "")]

    def owner_requests(self, run):
        return [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and WORK in r["text"]]

    def row_view(self, row):
        keys = ("attempt_id", "status", "turn", "continues_attempt_id", "automatic_retry_of", "session", "note",
                "failure_class", "reconcile_reason", "parent_close_requested", "recovery_attention_reason")
        return {k: row.get(k) for k in keys if row.get(k) is not None}

    def wait_rows(self, count, appear=120):
        """Wait for `count` owner rows, the last one ended; False when the row never appears or never ends."""
        if not wait_for(lambda: len(self.owner_rows()) >= count, appear, 1):
            return False
        return wait_for(lambda: (lambda rows: len(rows) >= count and rows[-1]["status"] in TERMINAL_STATES)(
            self.owner_rows()), self.WAIT, 2)

    def receipt(self):
        text = self.parent_outputs[-1] if self.parent_outputs else ""
        objs = _json_objects(text)
        top = objs[0] if objs else {}
        keep = ("state", "reason", "required_action", "next_step", "owner_attempt_id", "close_command",
                "correction_command", "terminal_gate_proven", "retained", "request_id", "next_action", "retry_when",
                "turn", "session")
        view = {k: top.get(k) for k in keep if k in top}
        view["already_closed_notice"] = "route already closed" in text
        return view

    def cycles(self, run, route_id):
        sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import artifact_producer_store as store
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"])
        return [{k: rec.get(k) for k in ("cycle_id", "state", "cycle_state", "abandon_reason", "continues_cycle_id")}
                for rec in store.list_cycle_records(root, route_ids={route_id})]

    def ledger_state(self, route):
        sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import workflow_state as WS
        try:
            state = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=self.jobs).state()
        except Exception as exc:  # noqa: BLE001 -- evidence only
            return {"error": f"{type(exc).__name__}: {exc}"[:200]}
        return {"workflow_state": state.get("workflow_state"),
                "nodes": {k: (v or {}).get("state") for k, v in (state.get("nodes") or {}).items()}}

    def snapshot(self, run, label):
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"])
        files = self.route_files(run)
        route = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
        closure = self.closure(run) or {}
        snap = {"label": label, "owner_rows": [self.row_view(r) for r in self.owner_rows()],
                "closure": {k: closure.get(k) for k in ("terminal_gate_proven", "terminal_owner_attempt_id", "state",
                                                        "reason") if k in closure},
                "cycles": self.cycles(run, route["route_id"]) if route else [],
                "ledger": self.ledger_state(route) if route else {},
                "campaign_lines": [], "records": []}
        for md in sorted(root.glob("campaigns/*/campaign.md")):
            snap["campaign_lines"] += [line[:300] for line in md.read_text(encoding="utf-8").splitlines()
                                       if line.startswith(("- ", "## ", "지금 없음", "아직 없음"))][:12]
        for page in sorted(root.glob("campaigns/*/records/*.md"))[:4]:
            snap["records"].append({"file": page.name, "head": page.read_text(encoding="utf-8")[:500]})
        return snap

    def step(self, run, marker, ev, label, rows=None):
        self.ask(run, marker)
        if rows:
            self.wait_rows(rows, appear=90)
        wait_for(lambda: False, 6, 1)
        ev.setdefault("steps", []).append({"marker": marker, "receipt": self.receipt(),
                                           "after": self.snapshot(run, label)})
        return ev["steps"][-1]

    # -- the run ----------------------------------------------------------------------------------
    def drive(self, run):
        ev = run.evidence
        try:
            self.ask(run, START)
            if not self.wait_rows(1, appear=180):
                run.note("the first owner never ended")
                return "inconclusive"
            wait_for(lambda: False, 6, 1)
            return self.run_close(run, ev) if self.variant == "close" else self.run_follow_up(run, ev)
        finally:
            self.worker_end.set()
            ev["parent_outputs"] = [text[-2500:] for text in self.parent_outputs]

    def run_close(self, run, ev):
        ev["after_fail"] = self.snapshot(run, "after-fail")
        failed = self.step(run, RESTART, ev, "after-start")
        closed = self.step(run, CLOSE, ev, "after-close")
        after = self.step(run, RESTART, ev, "after-close-start")
        again = self.step(run, CLOSE, ev, "after-second-close")
        rows = self.owner_rows()
        cycle = (closed["after"]["cycles"] or [{}])[-1]
        offered = failed["receipt"]
        ok = (len(rows) == 1 and rows[0].get("failure_class") == "fail"
              and offered.get("reason") == "owner-failed" and bool(offered.get("close_command"))
              and "accept" in (offered.get("next_step") or "") and "close_command" in (offered.get("next_step") or "")
              and closed["after"]["closure"].get("terminal_gate_proven") is False
              and "abandoned" in (cycle.get("state"), cycle.get("cycle_state"))
              and closed["after"]["ledger"].get("workflow_state") == "CANCELLED"
              and after["receipt"].get("reason") == "route-closed-unproven"
              and again["after"]["closure"] == closed["after"]["closure"]
              and again["after"]["cycles"] == closed["after"]["cycles"]
              and again["receipt"].get("already_closed_notice")
              and len(self.owner_rows()) == 1)
        run.note(f"cycle={cycle.get('state')}/{cycle.get('cycle_state')} ledger={closed['after']['ledger'].get('workflow_state')} "
                 f"after_close_start={after['receipt'].get('reason')}")
        return "pass" if ok else "fail"

    def run_follow_up(self, run, ev):
        wait_for(lambda: (self.closure(run) or {}).get("terminal_gate_proven") is True, 90, 1)
        ev["first_closure"] = (self.closure(run) or {}).get("terminal_gate_proven")
        turn2 = self.step(run, CONT, ev, "after-turn-2", rows=2)
        count = 2
        if self.variant == "follow-up-bad":
            observed = self.step(run, RESTART, ev, "after-observed", rows=3)
            count = 3
            ev["observed_started"] = len(self.owner_rows()) >= 3
            ev["observed_receipt"] = observed["receipt"]
        answered = self.step(run, ANSWER, ev, "after-answer", rows=count + 1)
        wait_for(lambda: (self.closure(run) or {}).get("terminal_owner_attempt_id")
                 == (self.owner_rows() or [{}])[-1].get("attempt_id"), 90, 1)
        settled = self.step(run, RESTART, ev, "after-settled")
        rows = self.owner_rows()
        closure = settled["after"]["closure"]
        last = rows[-1] if rows else {}
        if len(rows) != count + 1:
            run.note(f"owners={len(rows)} answer={answered['receipt'].get('state')}/"
                     f"{answered['receipt'].get('reason')}: the answer opened no owner turn")
            return "fail"
        ok = (rows[1].get("turn") == "2"
              and rows[1].get("continues_attempt_id") == rows[0]["attempt_id"]
              and last.get("automatic_retry_of") == rows[-2]["attempt_id"]
              and last.get("failure_class") == "pass"
              and closure.get("terminal_gate_proven") is True
              and closure.get("terminal_owner_attempt_id") == last.get("attempt_id")
              and settled["receipt"].get("state") == "completed")
        if self.variant == "follow-up-bad":
            ok = ok and rows[2].get("automatic_retry_of") == rows[1]["attempt_id"]
        run.note(f"owners={len(rows)} answer={answered['receipt'].get('state')}/{answered['receipt'].get('reason')} "
                 f"turn2={turn2['receipt'].get('state')} closed_on={closure.get('terminal_owner_attempt_id')}")
        return "pass" if ok else "fail"


class OwnerFollowUpFailScenario(OwnerFailCloseScenario):
    name = "owner-follow-up-fail-headless-parent"
    summary = ("A PASS-closed route's later turn ends FAIL; the parent's answer continues that turn in a replacement "
               "owner, which ends PASS and closes the route again.")
    variant = "follow-up-fail"


class OwnerFollowUpBadHandoffScenario(OwnerFailCloseScenario):
    name = "owner-follow-up-bad-handoff-headless-parent"
    summary = ("A PASS-closed route's later turn ends without its handoff; `start --route` continues it once on the "
               "runtime's observation, and the parent's answer to that owner's FAIL closes the route on a PASS.")
    variant = "follow-up-bad"


class UnitContinueBilingualVerdictScenario(UnitContinueScenario):
    name = "unit-continue-bilingual-verdict"
    summary = ("A review ends FAIL on a verdict line written in both forms (`… changes requested — head <sha> · "
               "최종 판정: 수정 후 병합 — head <sha>`): the route closes as a finished review and takes another turn.")
    report_head = "Final verdict: changes requested — head 0123abc · 최종 판정: 수정 후 병합 — head 0123abc"

    def drive(self, run):
        ev = run.evidence
        try:
            first = self.run_unit_route(run, ev)
            ev["first_turn"] = first
            if first == "inconclusive":
                return first
            # The closure check is asked even when the first end did not read as a finished review: what the
            # receipt says then is the evidence.
            second = self.continue_route(run, ev)
            ev["correct_receipts"] = [r["last_text"][-900:] for r in unit.resting_session_requests(run)
                                      if r["last_kind"] == "tool" and '"request_id"' in r["last_text"]
                                      or "unit-continue-last-turn-no-verdict" in r["last_text"]][-2:]
            return second if first == "pass" else "fail"
        finally:
            self.worker_end.set()


SCENARIOS = [OwnerFailCloseScenario, OwnerFollowUpFailScenario, OwnerFollowUpBadHandoffScenario,
             UnitContinueBilingualVerdictScenario]
