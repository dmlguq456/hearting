#!/usr/bin/env python3
"""A worker that starts a unit route (`harness_behavior_scenarios.py`; PR 2c).

The depth-0 session starts a unit route (`unit-route` of PR 2b); its worker then starts a second one.
The fixture model plays both workers from the markers their briefs carry.

``unit-route-nested``
    The first worker starts a second unit route, runs the wait command its receipt prints, finds the
    second worker's finding in the result, writes its own report with it and ends. The result of the
    second worker reaches the first (the wait result), never the resting depth-0 session; that session
    is woken once, with the first worker's summary.

``unit-route-caller-first``
    The first worker starts the second and ends without waiting for it. The second worker is still
    running (its last answer is held): it is stopped when its caller ends, nothing is left running, and
    the depth-0 session still receives the first worker's summary.

``unit-route-limit``
    Every worker starts one more, as a runaway chain would. The governor's dispatch cap (lowered through
    its own override) stops the chain: the start that no slot fits is refused in the governor's own
    refusal form, a worker that meets it reports it, and every worker ends.

The oracle reads the registry (rows, `caller_attempt_id`, end), the routes and the requests the fixture
model received. Synthetic data stays under the fixture root.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_e import (  # noqa: E402
    OwnerPublishRegisteredScenario, TERMINAL_STATES, _base, registry_rows, last_json_object)
import harness_behavior_cases_unit_route as unit  # noqa: E402

CHILD = "SCENARIO-UNIT-CHILD"
CHILD_FINDING = "HB-UNIT-CHILD-FINDING-7"
LEVEL = "SCENARIO-UNIT-LEVEL-"
CAP = 3          # the dispatch class cap of the limit scenario
MAX_LEVELS = 8   # the fixture chain never goes past this, so an unenforced cap fails the oracle, not the machine


WORKER_PROCESS = ("claude -p", "codex exec", "opencode run", "model-worker-governor.py", "dispatch-headless.py")


def worker_processes(run):
    listing = subprocess.run(["ps", "-eo", "pid,args"], text=True, capture_output=True).stdout.splitlines()
    return [line.strip()[:200] for line in listing if str(run.home.root) in line and "ps -eo" not in line
            and any(word in line for word in WORKER_PROCESS)
            and "fleet-title-workdir" not in line and "workflow-group-review-workdir" not in line]   # Fleet's own helpers


def _shells(earlier, mark):
    return [str(r["reply"].get("shell") or "") for r in earlier if mark in r["text"]]


def _compose(run, brief, slug, *extra):
    return " ".join([
        sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose", "--start",
        "--unit", "qa/code-review", "--pin", f"worker={run.harness}", "--prompt-file", str(brief),
        "--cwd", str(run.home.workspace), "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned",
        "--slug", slug, *extra])


def _report_shell(record, head, body):
    output = re.search(r"artifact_output_dir: (\S+)", record["text"])
    target = (output.group(1) if output else "/nonexistent") + "/units/unit/report.md"
    return target, (f"mkdir -p \"$(dirname '{target}')\" && printf %s {shlex.quote(body.format(head=head))} > '{target}'")


class NestedScenario(unit.UnitRouteScenario):
    name = "unit-route-nested"
    summary = ("A unit worker starts a second unit route and waits for it: the second worker's result reaches the "
               "first (its wait result), and the resting session is woken once, with the first worker's summary.")
    first_waits = True     # the first worker waits for the second's result before it ends
    second_held = False    # the second worker's last answer is held until the driver releases it

    # -- the fixture model ------------------------------------------------------------------------
    def script(self, run):
        self.worker_end = threading.Event()    # the first worker's last answer waits until the session rests
        self.child_end = threading.Event()     # the second worker's last answer
        if not self.second_held:
            self.child_end.set()
        run.evidence["wait_results"] = []

        def first_worker(record, earlier):
            mark = unit.WORK
            shells = _shells(earlier, mark)
            last = record["last_text"]
            composed = any("hb-unit-child" in s for s in shells)
            waited = any(" start --route " in s and "--wait" in s for s in shells)
            wrote = any("report.md" in s for s in shells)
            if not composed:
                return Reply(shell=_compose(run, run.home.root / "brief-child.md", "hb-unit-child"))
            refused = bool(run.evidence.get("compose_refused")) or (composed and not waited and not wrote and (
                "quick-headless-unavailable" in last or "Process exited with code 64" in last))
            if refused and not run.evidence.get("compose_refused"):
                run.evidence["compose_refused"] = last[-600:]
            if self.first_waits and not waited and not refused and not wrote:
                route = re.search(r'"route_id": "(rt-[0-9a-f]+)"', last) or re.search(r"route (rt-[0-9a-f]+)", last)
                run.evidence["child_compose_result"] = last[-1500:]
                if not route:
                    return Reply("The second worker did not start.")
                return Reply(shell=" ".join([sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                                             "start", "--route", route.group(1), "--wait"]))
            if self.first_waits and waited and not wrote:
                if '"state": "completed"' not in last and sum(" start --route " in s and "--wait" in s for s in shells) < 8:
                    return Reply(shell=[s for s in shells if " start --route " in s and "--wait" in s][-1])   # the wait ran out: wait again
                run.evidence["wait_results"].append(last[-20000:])
            if not wrote:
                body = unit.REPORT + ("\n## Called worker\n\n- its result reached this worker: "
                                      f"{CHILD_FINDING if CHILD_FINDING in last else 'absent'}\n")
                target, shell = _report_shell(record, unit.PASS_HEAD, body)
                self.first_target = target
                return Reply(shell=shell)
            return Reply(f"artifact: {self.first_target}\nverdict: PASS\nblocker: none", gate=self.worker_end)

        def second_worker(record, earlier):
            shells = _shells(earlier, CHILD)
            if not any("report.md" in s for s in shells):
                body = ("# Lookup\n\n{head}\n\n## Blocking findings\n\n- " + CHILD_FINDING + ": the lookup found it\n")
                target, shell = _report_shell(record, unit.PASS_HEAD, body)
                self.second_target = target
                return Reply(shell=shell)
            return Reply(f"artifact: {self.second_target}\nverdict: PASS\nblocker: none", gate=self.child_end)

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture unit route\nNOW: fixture")
            # The first worker's later requests quote the second one's brief (its start result): it is the one
            # whose own brief is the first.
            if "Dispatch metadata:" in text and unit.WORK in text:
                return first_worker(record, earlier)
            if "Dispatch metadata:" in text and CHILD in text:
                return second_worker(record, earlier)
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if unit.START in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=_compose(run, run.home.root / "brief.md", "hb-unit-route"))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "brief-child.md").write_text(f"{CHILD}: look something up and report.\n", encoding="utf-8")

    # -- what the oracle reads --------------------------------------------------------------------
    def rows_by_slug(self):
        out = {}
        for row in registry_rows(self.jobs):
            out[row.get("slug", "")] = row
        return out

    def leftovers(self, run):
        """Worker processes of the fixture still alive: a model session, its wrapper or its governor lease."""
        return worker_processes(run)

    def settle(self, run, ev):
        """Both rows ended (or the wait ran out), the resting session idle: then read everything."""
        wait_for(lambda: all(r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)) and len(registry_rows(self.jobs)) >= 2,
                 self.WAIT, 2)
        routes_dir = Path(run.home.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes"
        wait_for(lambda: len(list(routes_dir.glob("rt-*.outcome.json"))) >= 2, 120, 2)
        wait_for(lambda: [r for r in unit.resting_session_requests(run)
                          if unit.BLOCKER in r["text"] and r["last_kind"] != "tool"], 120, 1)
        run.wait_idle(8, 60)
        rows = self.rows_by_slug()
        first, second = rows.get("hb-unit-route-unit"), rows.get("hb-unit-child-unit")
        keys = ("status", "attempt_id", "caller_attempt_id", "dispatch_depth", "worker_type", "route_id", "route_node",
                "parent_completion_delivery", "note", "failure_class", "workflow_completion", "write_scope", "harness")
        ev["first"] = {k: (first or {}).get(k) for k in keys}
        ev["second"] = {k: (second or {}).get(k) for k in keys}
        routes = {p.stem: p for p in (Path(run.home.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes").glob("rt-*.json")
                  if not p.stem.endswith(".outcome")}
        ev["routes_closed"] = {rid: p.with_name(p.stem + ".outcome.json").exists() for rid, p in routes.items()}
        return first, second

    def oracle(self, run, ev, first, second):
        requests = unit.resting_session_requests(run)
        carrying_child = [r for r in requests if CHILD_FINDING in r["text"] and r["last_kind"] != "tool"]
        wake = [r for r in requests if unit.BLOCKER in r["text"] and r["last_kind"] != "tool"]
        ev["root_wakes"] = len(wake)
        ev["child_finding_in_root_requests"] = len([r for r in requests if CHILD_FINDING in r["text"]])
        ev["leftover_processes"] = self.leftovers(run)
        ok = bool(first and second
                  and first["status"] == "done" and first.get("failure_class") == "pass"
                  and second["status"] == "done" and second.get("failure_class") == "pass"
                  and second.get("caller_attempt_id") == first.get("attempt_id")
                  and not first.get("caller_attempt_id")
                  and second.get("route_id") != first.get("route_id")
                  and all(ev["routes_closed"].values()) and len(ev["routes_closed"]) == 2
                  and any(CHILD_FINDING in text for text in ev["wait_results"])
                  and wake)
        return ok

    def drive(self, run):
        ev = run.evidence
        try:
            run.prompt(unit.START)
            started = wait_for(lambda: len(registry_rows(self.jobs)) >= 1, 120, 1)
            if not started:
                run.note("the first unit worker never registered")
                return "inconclusive"
            wait_for(lambda: any(r["last_kind"] == "tool" for r in unit.resting_session_requests(run)), 90, 1)
            run.wait_idle(6, 60)
            if self.refusal_expected(run):
                wait_for(lambda: self.first_done(), 150, 1)
                wait_for(lambda: self.first_closed(run), 90, 1)
                self.worker_end.set()
                return self.refusal_verdict(run, ev)
            wait_for(lambda: len(registry_rows(self.jobs)) >= 2, 150, 1)
            if not self.first_waits:
                self.worker_end.set()      # the first worker ends, the second still runs
                wait_for(lambda: self.first_done(), 120, 1)
            else:
                wait_for(lambda: all(r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)) or
                         any(CHILD_FINDING in t for t in ev["wait_results"]), 150, 1)
            self.worker_end.set()
            first, second = self.settle(run, ev)
            run.note(f"rows={len(registry_rows(self.jobs))}; first={ev['first'].get('status')}/{ev['first'].get('failure_class')}; "
                     f"second={ev['second'].get('status')}/{ev['second'].get('failure_class')}; wakes={len(unit.resting_session_requests(run))}")
            if not first or not second:
                return "inconclusive"
            return "pass" if self.oracle(run, ev, first, second) else "fail"
        finally:
            self.worker_end.set()
            self.child_end.set()

    def refusal_expected(self, run) -> bool:
        """A Codex worker runs in a sandbox with no network: the registered children it would start could not
        reach their model, so the readiness check refuses the start (its existing typed refusal)."""
        return run.harness == "codex"

    def first_closed(self, run) -> bool:
        routes = list((Path(run.home.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes").glob("rt-*.outcome.json"))
        return bool(routes)

    def refusal_verdict(self, run, ev):
        wait_for(lambda: unit.BLOCKER in "".join(r["text"] for r in unit.resting_session_requests(run)
                                                 if r["last_kind"] != "tool"), 90, 1)
        rows = registry_rows(self.jobs)
        ev["rows_total"] = len(rows)
        ev["first"] = {k: rows[0].get(k) for k in ("status", "failure_class", "note")} if rows else None
        ev["root_wakes"] = len([r for r in unit.resting_session_requests(run)
                                if unit.BLOCKER in r["text"] and r["last_kind"] != "tool"])
        ev["leftover_processes"] = self.leftovers(run)
        run.note(f"codex worker's start refused: {ev.get('compose_refused', '')[-60:]!r}; rows={len(rows)}")
        ok = (len(rows) == 1 and rows[0]["status"] == "done" and rows[0].get("failure_class") == "pass"
              and "quick-headless-unavailable" in ev.get("compose_refused", "") and ev["root_wakes"]
              and not ev["leftover_processes"])
        return "pass" if ok else "fail"

    def first_done(self):
        row = self.rows_by_slug().get("hb-unit-route-unit")
        return bool(row and row["status"] in TERMINAL_STATES)


class CallerFirstScenario(NestedScenario):
    name = "unit-route-caller-first"
    summary = ("The first worker starts a second unit route and ends without waiting: the second worker, still "
               "running, is stopped with its caller's end, nothing is left running, and the resting session "
               "still receives the first worker's summary.")
    first_waits = False
    second_held = True

    def oracle(self, run, ev, first, second):
        wake = wait_for(lambda: [r for r in unit.resting_session_requests(run)
                                 if unit.BLOCKER in r["text"] and r["last_kind"] != "tool"], 90, 1)
        requests = unit.resting_session_requests(run)
        ev["root_wakes"] = len(wake or [])
        stopped = wait_for(lambda: self.rows_by_slug().get("hb-unit-child-unit", {}).get("status") in TERMINAL_STATES, 90, 1)
        second = self.rows_by_slug().get("hb-unit-child-unit") or second
        ev["second_after"] = {k: second.get(k) for k in ("status", "note", "failure_class", "reconcile_reason",
                                                         "caller_attempt_id")}
        wait_for(lambda: not self.leftovers(run), 60, 2)
        ev["leftover_processes"] = self.leftovers(run)
        ev["child_finding_in_root_requests"] = len([r for r in requests if CHILD_FINDING in r["text"]])
        return bool(stopped and first["status"] == "done" and first.get("failure_class") == "pass"
                    and second.get("failure_class") == "cancelled" and second.get("note") == "fleet-kill"
                    and second.get("reconcile_reason") == "caller-ended"
                    and second.get("caller_attempt_id") == first.get("attempt_id")
                    and not ev["leftover_processes"] and wake and not ev["child_finding_in_root_requests"])

    def settle(self, run, ev):
        wait_for(lambda: self.first_done(), 120, 2)
        run.wait_idle(8, 60)
        rows = self.rows_by_slug()
        first, second = rows.get("hb-unit-route-unit"), rows.get("hb-unit-child-unit")
        keys = ("status", "attempt_id", "caller_attempt_id", "dispatch_depth", "route_id", "note", "failure_class")
        ev["first"] = {k: (first or {}).get(k) for k in keys}
        ev["second"] = {k: (second or {}).get(k) for k in keys}
        return first, second


ANSWER = "HB-EPHEMERAL-ANSWER-9"


class EphemeralScenario(unit.UnitRouteScenario):
    name = "unit-route-ephemeral"
    summary = ("A unit route opened with --ephemeral leaves nothing in the project's artifact folder; it shows in the "
               "registry while it runs and its answer comes back with its completion.")
    answer_lines = 3          # lines of answer the worker writes (the long variant writes more than one notice carries)
    empty = False             # the worker ends with an empty answer
    cap_expected = False

    def side_effects(self, run):
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"])
        # The model-worker governor's own state is written by whatever worker the session starts (Fleet's title
        # helper on Codex among them), not by the unit route.
        return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                      if p.is_file() and ".runtime/model-worker-governor/" not in str(p))

    def script(self, run):
        self.worker_end = threading.Event()

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture unit route\nNOW: fixture")
            if "Dispatch metadata:" in text and unit.WORK in text:
                body = "" if self.empty else "{head}\n\n" + "\n".join(
                    f"- {ANSWER} line {n}: " + "x" * (150 if self.cap_expected else 20) for n in range(self.answer_lines)) + "\n"
                target, shell = _report_shell(record, unit.PASS_HEAD, body)
                if not any("report.md" in s for s in _shells(earlier, unit.WORK)):
                    return Reply(shell=shell)
                return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none", gate=self.worker_end)
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if unit.START in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=" ".join([
                    sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose", "--start",
                    "--unit", "qa/code-review", "--ephemeral", "--pin", f"worker={run.harness}",
                    "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                    "--slug", "hb-unit-route"]))
            return Reply("Nothing new.")
        return answer

    def drive(self, run):
        ev = run.evidence
        try:
            before = self.side_effects(run)
            run.prompt(unit.START)
            started = wait_for(lambda: self.unit_rows(), 150, 1)
            ev["rows_while_running"] = [{k: r.get(k) for k in ("status", "worker_type", "unit", "route_node", "artifact_root")}
                                        for r in self.unit_rows()]
            if not started:
                run.note("the ephemeral worker never registered")
                return "inconclusive"
            wait_for(lambda: any(r["last_kind"] == "tool" for r in unit.resting_session_requests(run)), 90, 1)
            run.wait_idle(6, 60)
            self.worker_end.set()
            wait_for(lambda: (lambda rows: rows and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()), self.WAIT, 2)
            wake = wait_for(lambda: [r for r in unit.resting_session_requests(run)
                                     if "Answer:" in r["text"] and r["last_kind"] != "tool"], 120, 1)
            run.wait_idle(8, 60)
            after = self.side_effects(run)
            rows = self.unit_rows()
            ev["project_artifact_files_added"] = sorted(set(after) - set(before))
            ev["row"] = {k: rows[-1].get(k) for k in ("status", "note", "failure_class", "route_id", "artifact_root")} if rows else None
            scratch = Path(rows[-1].get("artifact_root", "")) if rows else None
            ev["scratch_root_is_marked"] = bool(scratch and (scratch / ".ephemeral").is_file())
            ev["scratch_files"] = len([p for p in scratch.rglob("*") if p.is_file()]) if scratch and scratch.is_dir() else 0
            notice = (wake or [None])[0]
            tail = notice["text"] if notice else ""
            at = tail.rfind("Answer:")
            ev["answer_in_notice"] = tail[at:at + 1500] if at >= 0 else ""
            # Every line of the answer arrived (a harness may echo the notice twice in one request).
            ev["answer_complete"] = sum(f"{ANSWER} line {n}:" in tail for n in range(self.answer_lines)) if notice else 0
            ev["cut_notice"] = "answer cut at" in tail
            run.note(f"rows={len(rows)}; added in project artifacts={len(ev['project_artifact_files_added'])}; "
                     f"notice={'yes' if notice else 'no'}; cut={ev['cut_notice']}")
            return self.verdict(run, ev, rows, notice)
        finally:
            self.worker_end.set()

    def verdict(self, run, ev, rows, notice):
        ok = (rows and len(rows) == 1 and rows[-1]["status"] == "done" and rows[-1].get("failure_class") == "pass"
              and not ev["project_artifact_files_added"] and ev["scratch_root_is_marked"] and ev["scratch_files"]
              and notice and ev["answer_complete"] == self.answer_lines and not ev["cut_notice"]
              and ev["rows_while_running"] and ev["rows_while_running"][0]["route_node"] == "unit")
        return "pass" if ok else "fail"


class EphemeralLongScenario(EphemeralScenario):
    name = "unit-route-ephemeral-long"
    summary = ("An ephemeral answer longer than one notice carries is cut, and the notice says where to read the rest.")
    answer_lines = 60
    cap_expected = True

    def verdict(self, run, ev, rows, notice):
        ok = (rows and rows[-1]["status"] == "done" and rows[-1].get("failure_class") == "pass" and notice
              and ev["cut_notice"] and "read the rest in" in notice["text"]
              and not ev["project_artifact_files_added"])
        return "pass" if ok else "fail"


class EphemeralEmptyScenario(EphemeralScenario):
    name = "unit-route-ephemeral-empty"
    summary = "An ephemeral worker that ends with an empty answer has not finished the work: nothing is delivered as done."
    empty = True

    def drive(self, run):
        ev = run.evidence
        try:
            run.prompt(unit.START)
            started = wait_for(lambda: self.unit_rows(), 150, 1)
            if not started:
                return "inconclusive"
            wait_for(lambda: any(r["last_kind"] == "tool" for r in unit.resting_session_requests(run)), 90, 1)
            run.wait_idle(6, 60)
            self.worker_end.set()
            wait_for(lambda: (lambda rows: rows and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()), self.WAIT, 2)
            run.wait_idle(20, 120)
            rows = self.unit_rows()
            ev["row"] = {k: rows[-1].get(k) for k in ("status", "note", "failure_class")} if rows else None
            texts = "\n".join(r["text"] for r in unit.resting_session_requests(run) if r["last_kind"] != "tool")
            ev["notice_says_answer"] = "Answer:" in texts
            ev["notice_says_complete"] = "required_action=advance-completed" in texts
            run.note(f"row={ev['row']}; answer_notice={ev['notice_says_answer']}; complete_notice={ev['notice_says_complete']}")
            ok = (rows and ev["row"]["status"] in TERMINAL_STATES and not ev["notice_says_answer"]
                  and not ev["notice_says_complete"] and not (ev["row"]["status"] == "done" and ev["row"].get("failure_class") == "pass"))
            return "pass" if ok else "fail"
        finally:
            self.worker_end.set()


class LimitScenario(NestedScenario):
    name = "unit-route-limit"
    summary = ("Every unit worker starts one more, as a runaway chain would: the governor's dispatch cap stops it, the "
               "start no slot fits is refused in the governor's refusal form, and every worker still ends.")

    def prepare(self, run):
        super().prepare(run)
        run.home.env["AGENT_MODEL_WORKER_CLASS_LIMIT_DISPATCH"] = str(CAP)

    def script(self, run):
        self.worker_end = threading.Event()
        self.worker_end.set()
        run.evidence["levels"] = {}

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture unit route\nNOW: fixture")
            if "Dispatch metadata:" in text and LEVEL in text:
                level = int(re.search(LEVEL + r"(\d+)", text).group(1))
                # Requests of this level only: a higher level's later requests quote its start result.
                shells = [str(r["reply"].get("shell") or "") for r in earlier
                          if "Dispatch metadata:" in r["text"]
                          and (re.search(LEVEL + r"(\d+)", r["text"]) or [None, ""])[1] == str(level)]
                composed = any(f"hb-unit-l{level + 1}" in s for s in shells)
                waited = any(" start --route " in s and "--wait" in s for s in shells)
                wrote = any("report.md" in s for s in shells)
                seen = run.evidence["levels"].setdefault(level, {})
                if not composed and not wrote and level < MAX_LEVELS:
                    brief = run.home.root / f"brief-l{level + 1}.md"
                    return Reply(shell=f"printf %s '{LEVEL}{level + 1}: go one level deeper' > '{brief}' && "
                                       + _compose(run, brief, f"hb-unit-l{level + 1}"))
                waits = [s for s in shells if " start --route " in s and "--wait" in s]
                if waits and not wrote and '"state": "completed"' not in last and len(waits) < 8:
                    return Reply(shell=waits[-1])   # the wait ran out: wait again
                if composed and not waited and not wrote:
                    route = re.search(r'"route_id": "(rt-[0-9a-f]+)"', last) or re.search(r"route (rt-[0-9a-f]+)", last)
                    refused = "waiting-capacity" in last or "launch-capacity-wait" in last
                    seen["next_start_result"] = "refused" if refused else "started" if route else "other"
                    if refused:
                        seen["refusal_text"] = last[-20000:]
                    if route and not refused:
                        return Reply(shell=" ".join([sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                                                     "start", "--route", route.group(1), "--wait"]))
                if not wrote:
                    target, shell = _report_shell(record, unit.PASS_HEAD, unit.REPORT)
                    seen["target"] = target
                    return Reply(shell=shell)
                return Reply(f"artifact: {seen['target']}\nverdict: PASS\nblocker: none")
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if unit.START in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=f"printf %s '{LEVEL}1: start the chain' > '{run.home.root / 'brief-l1.md'}' && "
                                   + _compose(run, run.home.root / "brief-l1.md", "hb-unit-l1"))
            return Reply("Nothing new.")
        return answer

    def drive(self, run):
        ev = run.evidence
        try:
            run.prompt(unit.START)
            wait_for(lambda: len(registry_rows(self.jobs)) >= 1, 150, 1)
            wait_for(lambda: len(registry_rows(self.jobs)) >= CAP and all(
                r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)), 420, 3)
            run.wait_idle(15, 90)
            rows = [r for r in registry_rows(self.jobs) if "hb-unit-l" in r.get("slug", "")]
            ev["rows"] = [{k: r.get(k) for k in ("slug", "status", "failure_class", "note", "caller_attempt_id", "attempt_id")}
                          for r in rows]
            ev["leftover_processes"] = self.leftovers(run)
            refused = [lvl for lvl, v in ev["levels"].items() if v.get("next_start_result") == "refused"]
            ev["levels_that_met_refusal"] = refused
            run.note(f"rows={len(rows)} (cap {CAP}); refused at level(s) {refused}; "
                     f"all ended={all(r['status'] in TERMINAL_STATES for r in rows)}")
            chain = [r.get("caller_attempt_id") for r in rows]
            if run.harness == "codex":
                # A Codex worker has no network in its sandbox: its start is refused by the readiness check
                # before the governor is asked, so the chain stops at the first worker.
                refusal = ev["levels"].get(1, {})
                ev["codex_chain_stop"] = refusal.get("next_start_result")
                ok = (len(rows) == 1 and rows[0]["status"] in TERMINAL_STATES and not ev["leftover_processes"]
                      and rows[0].get("failure_class") == "pass")
                return "pass" if ok else "fail"
            ok = (len(rows) == CAP and all(r["status"] in TERMINAL_STATES for r in rows)
                  and all(r.get("failure_class") == "pass" for r in rows) and refused == [CAP]
                  and "waiting-capacity" in ev["levels"][CAP].get("refusal_text", "")
                  and chain.count(None) == 1 and not ev["leftover_processes"])
            return "pass" if ok else "fail"
        finally:
            self.worker_end.set()


OWNER = "SCENARIO-OWNER-CALLS-UNIT"


class OwnerCallsScenario(OwnerPublishRegisteredScenario):
    name = "unit-route-owner"
    summary = ("A registered owner (quick, one session) starts a unit route, waits for it and reads its finding; the "
               "called worker hangs below the owner's attempt.")
    needs_tui = False
    SHAPES = ("calls",)
    OWNER_SECONDS = 300

    def script(self, run):
        run.evidence["wait_results"] = []

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if "Dispatch metadata:" in text and OWNER in text and "SCENARIO-UNIT-CHILD: look" not in text.split("Assignment:")[-1][:200]:
                shells = _shells(earlier, OWNER)
                composed = any("hb-unit-child" in s for s in shells)
                waits = [s for s in shells if " start --route " in s and "--wait" in s]
                if not composed:
                    return Reply(shell=_compose(run, run.home.root / "brief-child.md", "hb-unit-child"))
                if not waits:
                    route = re.search(r"route (rt-[0-9a-f]+)", last) or re.search(r'"route_id": "(rt-[0-9a-f]+)"', last)
                    run.evidence["compose_result"] = last[-1500:]
                    if not route:
                        return Reply("artifact: -\nverdict: BLOCKED\nblocker: the unit route did not start")
                    return Reply(shell=" ".join([sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                                                 "start", "--route", route.group(1), "--wait"]))
                if '"state": "completed"' not in last and len(waits) < 8:
                    return Reply(shell=waits[-1])
                run.evidence["wait_results"].append(last[-20000:])
                return Reply("artifact: -\nverdict: BLOCKED\nblocker: scenario probe only")
            if "Dispatch metadata:" in text and CHILD in text:
                shells = _shells(earlier, CHILD)
                if not any("report.md" in s for s in shells):
                    body = ("# Lookup\n\n{head}\n\n## Blocking findings\n\n- " + CHILD_FINDING + ": the lookup found it\n")
                    target, shell = _report_shell(record, unit.PASS_HEAD, body)
                    self.target = target
                    return Reply(shell=shell)
                return Reply(f"artifact: {self.target}\nverdict: PASS\nblocker: none")
            return Reply("OK")
        return answer

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "brief-child.md").write_text(f"{CHILD}: look something up and report.\n", encoding="utf-8")

    def start_owner(self, run, shape):
        task = run.home.root / "task-owner.md"
        task.write_text(f"{OWNER}: look something up through a unit worker, then report.\n", encoding="utf-8")
        env = run.home.env
        command = [sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                   "compose", "--start", "--shape", "solo", "--intensity", "quick",
                   "--capability", "autopilot-code", "--capability-mode", "dev",
                   "--pin", f"owner={run.harness}", "--prompt-file", str(task),
                   "--cwd", str(run.home.workspace), "--artifact-root", env["AGENT_ARTIFACT_ROOT"],
                   "--jobs", env["AGENT_DISPATCH_JOBS"], "--unassigned", "--slug", "hb-owner-calls"]
        done = subprocess.run(command, env=env, cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=300, stdin=subprocess.DEVNULL)
        return {"exit": done.returncode, "stdout_tail": done.stdout[-600:], "stderr_tail": done.stderr[-600:]}

    def drive(self, run):
        ev = run.evidence
        self.jobs = Path(run.home.env["AGENT_DISPATCH_JOBS"])
        launch = self.start_owner(run, "calls")
        ev["owner_start"] = launch
        wait_for(lambda: any(r.get("worker_type") == "owner" for r in registry_rows(self.jobs)), 120, 1)
        wait_for(lambda: len(registry_rows(self.jobs)) >= 2 and all(r["status"] in TERMINAL_STATES for r in registry_rows(self.jobs)),
                 300, 3)
        time.sleep(5)
        rows = registry_rows(self.jobs)
        owner = next((r for r in rows if r.get("worker_type") == "owner"), None)
        called = next((r for r in rows if r.get("worker_type") == "review"), None)
        keys = ("status", "attempt_id", "caller_attempt_id", "dispatch_depth", "worker_type", "failure_class", "note")
        ev["owner"] = {k: (owner or {}).get(k) for k in keys}
        ev["called"] = {k: (called or {}).get(k) for k in keys}
        ev["leftover_processes"] = worker_processes(run)
        run.note(f"owner={ev['owner'].get('status')}; called={ev['called'].get('status')}/{ev['called'].get('failure_class')}; "
                 f"started from {ev['owner'].get('worker_type')}")
        if not owner:
            return "inconclusive"
        if run.harness == "codex":
            # A Codex quick owner has no network in its sandbox, so the readiness check refuses the start (typed)
            # and nothing starts: no called worker, and the owner still ends.
            return "pass" if (called is None and owner["status"] in TERMINAL_STATES
                              and not ev["leftover_processes"]) else "fail"
        if not called:
            return "inconclusive"
        ok = (called["status"] == "done" and called.get("failure_class") == "pass"
              and called.get("caller_attempt_id") == owner.get("attempt_id")
              and any(CHILD_FINDING in t for t in ev["wait_results"]) and not ev["leftover_processes"])
        return "pass" if ok else "fail"


SCENARIOS = [NestedScenario, CallerFirstScenario, EphemeralScenario, EphemeralLongScenario, EphemeralEmptyScenario,
             LimitScenario, OwnerCallsScenario]
