#!/usr/bin/env python3
"""A unit route asked for another turn, on the three harnesses (`harness_behavior_scenarios.py`).

``unit-continue``
    The ``unit-route-blocking`` run (the review ends FAIL with a blocking finding and the route closes), then
    the depth-0 session runs ``capability-route.py correct --route <id> --message-file <text>``: a second turn
    of the same route and node starts as a new attempt in a new cycle and wakes the first turn's session in
    its own worker home (the model's request of turn 2 carries what turn 1 said), ends and closes the route
    again. The oracle reads the registry (two rows, ``continues_attempt_id``, ``turn``, ``session``), the
    set-aside closure record, the two cycles' reports (the first left as it was), the second cycle sealed as a
    child of the first, and what the second turn's model received.

``unit-continue-missing``
    The first turn's worker home is gone when the text is sent: the turn starts a fresh session
    (``session=fresh:session-missing``) that is told the original brief and the previous result.

``unit-continue-retained``
    The text is sent while the first turn still runs: nothing starts (``retained``, one row); once the turn has
    ended and the route has closed, the session's next ``start --route`` starts the second turn, once.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES, registry_rows, _base  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402

CONT = "SCENARIO-CONT correct the unit route and end your turn"
AGAIN = "SCENARIO-CONT start the unit route again and end your turn"
T1_MARK = "HB-CONT-T1-MARK"
T2_TEXT = "HB-CONT-T2-TEXT now also check the empty-list case and report again."
WAITING = "A request kept for this unit route is waiting for its next turn"


def _tool_text(record) -> str:
    return record["last_text"] if record["last_kind"] == "tool" else ""


class UnitContinueScenario(unit.UnitRouteBlockingScenario):
    name = "unit-continue"
    summary = ("A closed unit route asked for another turn with `correct --route`: a new attempt in a new cycle "
               "wakes the first turn's session, ends and closes the route again.")
    variant = "resume"

    # -- the fixture model ------------------------------------------------------------------------
    def script(self, run):
        self.worker_end = threading.Event()
        self.ran = set()
        report = unit.REPORT.replace("# Review of the fixture change", f"# Review of the fixture change {T1_MARK}")   # in the model's own first answer, not in the sealed summary

        def route_id():
            files = self.route_files(run)
            return files[0].stem if files else "none"

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture unit route\nNOW: fixture")
            if "Dispatch metadata:" in text and unit.WORK in text:                 # a unit worker, any turn
                outputs = re.findall(r"artifact_output_dir: (\S+)", text)
                target = (outputs[-1] if outputs else "/nonexistent") + "/units/unit/report.md"
                turn2 = "HB-CONT-T2-TEXT" in text
                wrote = any("report.md" in str(r["reply"].get("shell") or "")
                            and ("HB-CONT-T2-TEXT" in r["text"]) == turn2 and "Dispatch metadata:" in r["text"]
                            for r in earlier)
                body = (report.format(head=self.report_head) if not turn2 else
                        unit.REPORT.format(head="Final verdict: issues") + "\nHB-CONT-T2-DONE\n")
                if record["last_kind"] != "tool" and not wrote:
                    return Reply(shell=f"mkdir -p \"$(dirname '{target}')\" && printf %s {shlex.quote(body)} > '{target}'")
                if turn2:
                    return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none")
                return self.final_reply(target)
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if WAITING in last and WAITING not in self.ran:      # the end notice names the start: do what it says
                self.ran.add(WAITING)
                return Reply(shell=" ".join(shlex.quote(x) for x in [
                    sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "start",
                    "--route", route_id()]))
            for marker, command in ((unit.START, None), (CONT, "correct"), (AGAIN, "start")):
                if marker in last and marker not in self.ran:
                    self.ran.add(marker)
                    if command is None:
                        return Reply(shell=" ".join([
                            sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose",
                            "--start", "--unit", "qa/code-review", "--pin", f"worker={run.harness}",
                            "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                            "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned",
                            "--slug", "hb-unit-route"]))
                    cli = [sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), command,
                           "--route", route_id()]
                    if command == "correct":
                        cli += ["--message-file", str(run.home.root / "cont.md")]
                    return Reply(shell=" ".join(shlex.quote(x) for x in cli))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "cont.md").write_text(T2_TEXT + "\n", encoding="utf-8")

    # -- the run ----------------------------------------------------------------------------------
    def row_view(self, row):
        keys = ("attempt_id", "status", "turn", "continues_attempt_id", "session", "session_id", "harness",
                "permission_mode", "runtime_sandbox", "runtime_home", "route_node", "workflow_completion")
        return {k: row.get(k) for k in keys}

    def reports(self, run):
        """The route's report files, one per turn's cycle."""
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"])
        return sorted(root.glob("campaigns/*/*/artifacts/units/unit/report.md"))

    def superseded(self, run):
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes"
        return sorted(p.name for p in root.glob("rt-*.superseded-*.outcome.json"))

    def session_store_leak(self, run, rows):
        """Worker sessions that show up in the main session's own store (the user's real one in operation): none may.
        OpenCode keeps every session in one shared store, so it is reported as `shared` rather than counted."""
        import sys as _sys
        _sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import unit_continue
        env = run.home.env
        if run.harness == "opencode":
            return "shared"
        main = Path(env["CLAUDE_CONFIG_DIR"] if run.harness == "claude" else env["CODEX_HOME"])
        found = []
        for row in rows:
            sid = unit_continue.session_id_of(row)
            if sid:
                found += [str(p) for p in main.rglob(f"*{sid}*") if "projects" in p.parts or "sessions" in p.parts]
        return found

    def session_turns(self, run, marker, *needles):
        return [r for r in unit.resting_session_requests(run) if marker in r["last_text"] and all(n in r["text"] for n in needles)]

    def drive(self, run):
        ev = run.evidence
        try:
            first = self.run_unit_route(run, ev)
            if first != "pass":
                run.note(f"the first turn did not pass ({first})")
                return first
            return self.continue_route(run, ev)
        finally:
            self.worker_end.set()

    def continue_route(self, run, ev):
        reports = self.reports(run)
        ev["reports_before"] = [str(p) for p in reports]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in reports}
        first_rows = self.unit_rows()
        if self.variant == "missing":
            if run.harness == "opencode":
                # OpenCode's sessions live in one shared store, not in the worker home: delete the session itself.
                sys.path.insert(0, str(_base().SOURCE / "utilities"))
                import unit_continue
                done = subprocess.run(["opencode", "session", "delete", unit_continue.session_id_of(first_rows[0])],
                                      env=dict(run.home.env), cwd=str(run.home.workspace), capture_output=True,
                                      text=True, timeout=60, stdin=subprocess.DEVNULL)
                run.evidence["opencode_session_delete"] = {"rc": done.returncode, "out": (done.stdout + done.stderr)[-300:]}
            elif first_rows[0].get("runtime_home"):
                shutil.rmtree(first_rows[0]["runtime_home"], ignore_errors=True)
        run.prompt(CONT)
        two = wait_for(lambda: len(self.unit_rows()) >= 2, 120, 1)
        done = wait_for(lambda: (lambda rows: len(rows) >= 2 and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()),
                        self.WAIT, 2)
        run.wait_idle(8, 60)
        rows = self.unit_rows()
        ev["rows"] = [self.row_view(r) for r in rows]
        ev["superseded_closures"] = self.superseded(run)
        route_file = next(iter(self.route_files(run)), None)
        ev["route_closed_again"] = bool(route_file and route_file.with_name(route_file.stem + ".outcome.json").exists())
        after = self.reports(run)
        ev["reports_after"] = [str(p) for p in after]
        ev["first_report_kept"] = all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in before.items() if Path(p).exists())
        ev["one_cycle"] = len({p.parent for p in after}) == 2     # one cycle (folder) per turn
        ev["second_report_has_marker"] = any("HB-CONT-T2-DONE" in p.read_text(encoding="utf-8") for p in after)
        ev["turn2_summary_names_report"] = any("units/unit/report.md" in r["text"] for r in
                                               unit.resting_session_requests(run) if "Turn 2 of this unit route" in r["text"])
        requests = [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and "HB-CONT-T2-TEXT" in r["text"]]
        ev["turn2_model_requests"] = len(requests)
        first_request = requests[:1]          # the turn's first request: what it was woken or started with
        ev["turn2_request_has_turn1_history"] = any(T1_MARK in r["text"] for r in first_request)
        ev["turn2_request_has_brief"] = any(unit.WORK in r["text"] for r in first_request)
        ev["turn2_request_tail"] = requests[0]["last_text"][-900:] if requests else ""
        ev["session_store_leak"] = self.session_store_leak(run, rows)
        if not two or not done or len(rows) < 2:
            return "inconclusive"
        second = rows[1]
        ok = (len(rows) == 2 and second.get("turn") == "2" and second.get("continues_attempt_id") == rows[0]["attempt_id"]
              and second["attempt_id"] != rows[0]["attempt_id"] and ev["first_report_kept"] and ev["one_cycle"] and len(after) == 2
              and ev["second_report_has_marker"] and ev["route_closed_again"] and len(ev["superseded_closures"]) == 1
              and second.get("workflow_completion") == "runtime-v1")
        ev["same_authority"] = {k: (rows[0].get(k), second.get(k)) for k in ("permission_mode", "runtime_sandbox")}
        ok = ok and all(a == b for a, b in ev["same_authority"].values())
        ok = ok and ev["session_store_leak"] in ([], "shared")
        if self.variant == "missing":
            ok = ok and second.get("session") == "fresh:session-missing" and ev["turn2_request_has_brief"] \
                and not ev["turn2_request_has_turn1_history"]
        else:
            ok = ok and second.get("session") == "resumed" and ev["turn2_request_has_turn1_history"]
        return "pass" if ok else "fail"


class UnitContinueMissingScenario(UnitContinueScenario):
    name = "unit-continue-missing"
    summary = "The first turn's worker home is gone: the next turn starts a fresh session with the brief."
    variant = "missing"


class UnitContinueRetainedScenario(UnitContinueScenario):
    name = "unit-continue-retained"
    summary = ("Text sent while the first turn runs is kept, starts nothing, and starts the second turn once "
               "after the turn has ended and the route has closed.")
    variant = "retained"

    def drive(self, run):
        ev = run.evidence
        try:
            run.prompt(unit.START)
            if not wait_for(lambda: self.unit_rows(), 120, 1):
                return "inconclusive"
            wait_for(lambda: any(r["last_kind"] == "tool" for r in unit.resting_session_requests(run)), 90, 1)
            run.wait_idle(6, 60)
            run.prompt(CONT)                                  # the first turn is still running (its answer is held)
            run.wait_idle(8, 60)
            ev["rows_while_running"] = len(self.unit_rows())
            ev["retained_receipt"] = any('"retained": true' in _tool_text(r) for r in unit.resting_session_requests(run))
            self.worker_end.set()
            wait_for(lambda: (lambda rows: rows and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()), self.WAIT, 2)
            route_file = next(iter(self.route_files(run)), None)
            wait_for(lambda: bool(route_file and route_file.with_name(route_file.stem + ".outcome.json").exists()), 90, 1)
            run.wait_idle(8, 60)
            ev["rows_after_first_end"] = len(self.unit_rows())
            ev["notice_names_start"] = any(WAITING in r["last_text"] for r in unit.resting_session_requests(run))
            if not wait_for(lambda: len(self.unit_rows()) >= 2, 60, 1):     # the notice's own start did not run it
                run.prompt(AGAIN)
            wait_for(lambda: len(self.unit_rows()) >= 2, 120, 1)
            wait_for(lambda: (lambda rows: len(rows) >= 2 and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()),
                     self.WAIT, 2)
            run.wait_idle(8, 60)
            rows = self.unit_rows()
            ev["rows"] = [self.row_view(r) for r in rows]
            ok = (ev["rows_while_running"] == 1 and ev["retained_receipt"] and ev["notice_names_start"] and len(rows) == 2
                  and rows[1].get("turn") == "2" and rows[1].get("continues_attempt_id") == rows[0]["attempt_id"])
            return "pass" if ok else "fail"
        finally:
            self.worker_end.set()


SCENARIOS = [UnitContinueScenario, UnitContinueMissingScenario, UnitContinueRetainedScenario]
