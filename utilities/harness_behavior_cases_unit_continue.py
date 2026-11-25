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

``unit-continue-chain``
    The same, then a third turn (``correct`` again): every turn ends with a verdict, so each end notice is the
    success receipt with the turn's summary, and ``start --route`` reads the route as completed after turns 2
    and 3 (an earlier turn the next one continued does not hold the closed route back).

``unit-continue-blocked``
    The second turn ends without a verdict (``BLOCKED``): its end stays an attention notice and the route reads as
    not finished (``start --route`` offers ``compose-again``), as before.

``unit-continue-missing``
    The first turn's worker home is gone when the text is sent: the turn starts a fresh session
    (``session=fresh:session-missing``) that is told the original brief and the previous result.

``unit-continue-retained``
    The text is sent while the first turn still runs: nothing starts (``retained``, one row); once the turn has
    ended and the route has closed, the session's next ``start --route`` starts the second turn, once.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
T3_TEXT = "HB-CONT-T3-TEXT now also check the single-item case and report once more."
CONT3 = "SCENARIO-CONT3 correct the unit route once more and end your turn"
WAITING = "A request kept for this unit route is waiting for its next turn"


def _turn_index(text: str) -> int:
    """Which turn of the unit route a worker request belongs to (a resumed session still carries the earlier texts)."""
    return 3 if "HB-CONT-T3-TEXT" in text else 2 if "HB-CONT-T2-TEXT" in text else 1


def _tool_text(record) -> str:
    return record["last_text"] if record["last_kind"] == "tool" else ""


class UnitContinueScenario(unit.UnitRouteBlockingScenario):
    name = "unit-continue"
    summary = ("A closed unit route asked for another turn with `correct --route`: a new attempt in a new cycle "
               "wakes the first turn's session, ends and closes the route again.")
    variant = "resume"
    turns = 2            # how many turns the scenario asks for
    blocked_turn = 0     # the turn that ends BLOCKED, without a verdict (0: none)
    moved_head = False   # the work tree's head moves on between turn 1 and turn 2 (the sealed head stays an ancestor)

    # -- the fixture model ------------------------------------------------------------------------
    def write_report_from_fixture(self, run, target, body):
        return False

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
                turn = _turn_index(text)
                wrote = any("report.md" in str(r["reply"].get("shell") or "")
                            and _turn_index(r["text"]) == turn and "Dispatch metadata:" in r["text"]
                            for r in earlier)
                body = (report.format(head=self.report_head) if turn == 1 else
                        unit.REPORT.format(head="Final verdict: issues") + f"\nHB-CONT-T{turn}-DONE\n")
                if turn == self.blocked_turn:       # this turn ends without a verdict and without a report
                    return Reply("artifact: -\nverdict: BLOCKED\nblocker: HB-CONT-BLOCKED the fixture is not reachable")
                if record["last_kind"] != "tool" and not wrote:
                    if not self.write_report_from_fixture(run, Path(target), body):
                        return Reply(shell=f"mkdir -p \"$(dirname '{target}')\" && printf %s {shlex.quote(body)} > '{target}'")
                if turn > 1:
                    return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none")
                return self.final_reply(target)
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if WAITING in last and WAITING not in self.ran:      # the end notice names the start: do what it says
                self.ran.add(WAITING)
                return Reply(shell=" ".join(shlex.quote(x) for x in [
                    sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "start",
                    "--route", route_id()]))
            for marker, command in ((unit.START, None), (CONT, "correct"), (CONT3, "correct3"), (AGAIN, "start")):
                if marker in last and marker not in self.ran:
                    self.ran.add(marker)
                    if command is None:
                        return Reply(shell=" ".join([
                            sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose",
                            "--start", "--unit", "qa/code-review", "--pin", f"worker={run.harness}",
                            "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                            "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned",
                            "--slug", "hb-unit-route"]))
                    cli = [sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                           command.rstrip("3"), "--route", route_id()]
                    if command.startswith("correct"):
                        cli += ["--message-file", str(run.home.root / ("cont3.md" if command == "correct3" else "cont.md"))]
                    return Reply(shell=" ".join(shlex.quote(x) for x in cli))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "cont.md").write_text(T2_TEXT + "\n", encoding="utf-8")
        (run.home.root / "cont3.md").write_text(T3_TEXT + "\n", encoding="utf-8")

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

    def end_notices(self, run, attempt_id):
        """What the resting session was told about one turn's end: each place its attempt id appears in the
        session's latest request, with the receipt words around it (state/classification/reason) and whether a
        completion summary follows."""
        requests = unit.resting_session_requests(run)
        text = requests[-1]["text"] if requests else ""
        found = []
        for hit in re.finditer(re.escape(f"attempt_id={attempt_id}"), text):
            around = text[max(0, hit.start() - 160):hit.start() + 900]
            words = {}
            for key in ("state", "classification", "reason", "required_action"):
                match = re.search(rf"\b{key}=([\w-]+)", around[150:])
                words[key] = match.group(1) if match else None
            words["has_summary"] = "[Completion summary]" in around[150:]
            if words["state"] or words["classification"]:
                found.append(words)
        return found

    def move_head(self, run):
        """Move the work tree's head on past the sealed one the way a merge of the finished work does: a new commit
        on an unrelated line that merges the sealed head in as its second parent (the sealed head is an ancestor of
        the new head, not on its first-parent line)."""
        ws, env = run.home.workspace, run.home.env
        def git(*args):
            return subprocess.run(["git", "-C", str(ws), *args], check=True, env=env, capture_output=True, text=True)
        sealed = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "--orphan", "hb-trunk")
        git("rm", "-rqf", ".")
        (ws / "trunk.txt").write_text("trunk moved on\n", encoding="utf-8")
        git("add", "trunk.txt")
        git("commit", "-q", "-m", "trunk")
        git("merge", "-q", "--no-ff", "--allow-unrelated-histories", "-m", "merge the finished work", "hb-task")
        return {"sealed": sealed, "head": git("rev-parse", "HEAD").stdout.strip()}

    def lineage_reading(self, sealed):
        """The lineage the route guard reads between the sealed head and the work tree's head now (its shared probe)."""
        sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import capability_route_continuation as continuation
        verdict = continuation.source_lineage_verdict(self.run_workspace, sealed)
        return {"kind": verdict.kind, "distance": verdict.distance}

    def terminal_inspect(self, run, attempt_id):
        """`dispatch_terminal_commit.py inspect` for one attempt, as a recovery reader would run it."""
        done = subprocess.run([sys.executable, str(_base().SOURCE / "utilities" / "dispatch_terminal_commit.py"),
                               "inspect", "--jobs", str(self.jobs), "--attempt", attempt_id],
                              env=dict(run.home.env), cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=120)
        try:
            view = json.loads(done.stdout)
        except ValueError:
            return {"exit": done.returncode, "output": (done.stdout + done.stderr)[-300:]}
        return {k: view.get(k) for k in ("state", "reason", "checkpoint", "detail")}

    def earlier_delivery(self, run, rows, n):
        """How the end of each turn before turn `n` reads now that turn `n` has run: the delivery classification and
        the completion state a late receipt for it (a rewake armed by that turn's start) would carry."""
        code = ("import importlib.util,json,sys\nfrom pathlib import Path\n"
                "import dispatch_completion_join as j\n"
                "s=j.current_delivery_state(Path(sys.argv[1]),sys.argv[2],parent_attempt_id=sys.argv[2],advance=False)\n"
                "spec=importlib.util.spec_from_file_location('rewake',sys.argv[3]+'/hooks/dispatch-owner-rewake.py')\n"
                "hook=importlib.util.module_from_spec(spec); sys.modules['rewake']=hook; spec.loader.exec_module(hook)\n"
                "launch=hook.Launch(sys.argv[2],Path(sys.argv[1]),'fixture-session','registry')\n"
                "_,text=hook.classified_receipt(launch,'ready','x',Path(sys.argv[3]))\n"
                "print(json.dumps({'classification':j.delivery_classification(s),'required_action':j.delivery_required_action(s),"
                "'workflow_state':s.workflow_state,'workflow_reason':s.workflow_reason,'receipt':text[:420],"
                "'receipt_has_summary':'[Completion summary]' in text}))\n")
        env = dict(run.home.env, PYTHONPATH=str(_base().SOURCE / "utilities"))
        out = []
        for row in rows[:n - 1]:
            done = subprocess.run([sys.executable, "-c", code, str(self.jobs), row["attempt_id"], str(_base().SOURCE)], env=env,
                                  cwd=str(run.home.workspace), text=True, capture_output=True, timeout=120)
            try:
                out.append(json.loads(done.stdout.strip().splitlines()[-1]))
            except (ValueError, IndexError):
                out.append({"error": (done.stdout + done.stderr)[-300:]})
        return out

    def after_turn(self, run, route, rows, n):
        """What a reader sees once turn `n` has ended: its end notice, `start --route`, the supervisor's view and
        the closure reading of every turn so far."""
        attempt = rows[n - 1]["attempt_id"]
        return {"turn": n, "attempt_id": attempt, "notices": self.end_notices(run, attempt),
                "start_route": self.start_route_again(run, route),
                "supervisor": self.supervisor_status(run, route),
                "earlier_delivery": self.earlier_delivery(run, rows, n),
                "inspect": [self.terminal_inspect(run, row["attempt_id"]) for row in rows[:n]]}

    def ask_turn(self, run, ev, n, marker):
        """Send turn `n`'s request through the resting session and wait for the turn to end and the route to settle."""
        run.prompt(marker)
        wait_for(lambda: len(self.unit_rows()) >= n, 120, 1)
        done = wait_for(lambda: (lambda rows: len(rows) >= n and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()),
                        self.WAIT, 2)
        run.wait_idle(8, 60)
        return done

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
        if self.moved_head:
            ev["moved_head"] = self.move_head(run)
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
        route = json.loads(route_file.read_text(encoding="utf-8")) if route_file else {}
        ev["after_turn2"] = self.after_turn(run, route, rows, 2) if route else {}
        if self.moved_head and route_file:
            self.run_workspace = run.home.workspace
            ev["lineage_after_move"] = self.lineage_reading(ev["moved_head"]["sealed"])
            ev["turn2_row_lineage"] = {k: rows[1].get(k) for k in ("launch_head", "source_commit_sealed",
                                                                  "source_commit_distance", "source_commit_branch")}
        second = rows[1]
        blocked = self.blocked_turn == 2
        ok = (len(rows) == 2 and second.get("turn") == "2" and second.get("continues_attempt_id") == rows[0]["attempt_id"]
              and second["attempt_id"] != rows[0]["attempt_id"] and ev["first_report_kept"]
              and second.get("workflow_completion") == "runtime-v1")
        if not blocked:     # a turn without a verdict writes no report and does not close the route again
            ok = ok and (ev["one_cycle"] and len(after) == 2 and ev["second_report_has_marker"]
                         and ev["route_closed_again"] and len(ev["superseded_closures"]) == 1)
        ev["same_authority"] = {k: (rows[0].get(k), second.get(k)) for k in ("permission_mode", "runtime_sandbox")}
        ok = ok and all(a == b for a, b in ev["same_authority"].values())
        ok = ok and ev["session_store_leak"] in ([], "shared")
        ok = ok and self.turn_reading(ev["after_turn2"], blocked=blocked)
        if self.moved_head:
            reading = ev.get("lineage_after_move") or {}
            ok = ok and reading.get("kind") == "descendant" and isinstance(reading.get("distance"), int)
        if self.turns >= 3 and ok:
            ok = self.third_turn(run, ev)
        if self.variant == "missing":
            ok = ok and second.get("session") == "fresh:session-missing" and ev["turn2_request_has_brief"] \
                and not ev["turn2_request_has_turn1_history"]
        else:
            ok = ok and second.get("session") == "resumed" and ev["turn2_request_has_turn1_history"]
        return "pass" if ok else "fail"

    def turn_reading(self, after, *, blocked) -> bool:
        """A turn that ended with a verdict reads as finished: the success receipt with its summary and a completed
        `start --route`. A turn that ended without one stays an attention notice and offers `compose-again`."""
        notice = after.get("notices") or []
        start = after.get("start_route") or {}
        earlier = after.get("earlier_delivery") or []
        if any(d.get("classification") != "success" for d in earlier):
            return False
        if blocked:
            return (not any(n.get("classification") == "success" or n.get("state") == "success" for n in notice)
                    and start.get("state") != "completed")
        return (bool(notice)
                and all((n.get("classification") == "success" or n.get("state") == "success")
                        and n.get("state") != "attention" and n.get("classification") != "attention" for n in notice)
                and any(n.get("has_summary") for n in notice)
                and start.get("state") == "completed" and not start.get("mentions_compose_again")
                and (after.get("supervisor") or {}).get("workflow_state") == "COMPLETE")

    def third_turn(self, run, ev) -> bool:
        before = len(self.reports(run))
        done = self.ask_turn(run, ev, 3, CONT3)
        rows = self.unit_rows()
        route_file = next(iter(self.route_files(run)), None)
        ev["rows3"] = [self.row_view(r) for r in rows]
        ev["superseded_closures3"] = self.superseded(run)
        route = json.loads(route_file.read_text(encoding="utf-8")) if route_file else {}
        if not done or len(rows) < 3 or not route:
            return False
        ev["after_turn3"] = self.after_turn(run, route, rows, 3)
        third = rows[2]
        return (len(rows) == 3 and third.get("turn") == "3" and third.get("continues_attempt_id") == rows[1]["attempt_id"]
                and len(self.reports(run)) == before + 1 and len(ev["superseded_closures3"]) == 2
                and self.turn_reading(ev["after_turn3"], blocked=False))


class UnitContinueChainScenario(UnitContinueScenario):
    name = "unit-continue-chain"
    summary = ("Three turns, each ended with a verdict: every end is the success notice with its summary and "
               "`start --route` reads the route as completed after each of them.")
    turns = 3


class UnitContinueBlockedScenario(UnitContinueScenario):
    name = "unit-continue-blocked"
    summary = ("The second turn ends without a verdict (BLOCKED): its end stays an attention notice and `start --route` "
               "does not read the route as completed.")
    blocked_turn = 2


class UnitContinueMovedScenario(UnitContinueScenario):
    name = "unit-continue-moved"
    summary = ("The work tree's head moved on between the turns (a merge that carries the sealed head as its second "
               "parent): the guard reads the sealed head as an ancestor, not as `diverged`.")
    moved_head = True


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



CAPACITY_CHECK = "SCENARIO-CAPACITY check the same unit route with start"
CAPACITY_REPLY = "The worker stopped at a usage limit"


class UnitCapacityContinueScenario(UnitContinueScenario):
    name = "unit-capacity-continue"
    summary = ("A recording unit hits a loopback 429. The existing capacity wait continues that turn "
               "once with the original permissions, preserving any earlier review.")
    QUIET = 3
    WAIT = 185

    def extra_argv(self, harness):
        # The private test pane owns its server directly. Its socket path is
        # already near the Unix-domain length limit inside the fixture HOME.
        return ["--no-daemon"] if harness == "codex" else []

    def prepare(self, run):
        super().prepare(run)
        if run.harness == "codex":
            # The fixture provider does not use an OpenAI account. Answer the
            # offline login probe locally and leave all model calls to Codex.
            bin_dir = run.home.root / "fixture-bin"
            bin_dir.mkdir()
            command = bin_dir / "codex"
            command.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = login ] && [ "$2" = status ]; then\n'
                "  echo 'Logged in using the loopback fixture'\n  exit 0\nfi\n"
                f'exec {shlex.quote(run.binary)} "$@"\n', encoding="utf-8")
            command.chmod(0o700)
            run.home.env["PATH"] = f"{bin_dir}{os.pathsep}{run.home.env['PATH']}"

    def write_report_from_fixture(self, run, target, body):
        if run.harness != "codex":
            return False
        # This host's nested Codex sandbox cannot start its tool socket in
        # the temporary HOME. Emit the synthetic artifact in the fixture,
        # while the real worker still negotiates its ordinary permission scope.
        if not target.resolve().is_relative_to(Path(run.home.env["AGENT_ARTIFACT_ROOT"])):
            raise ValueError("fixture report escaped its artifact root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return True

    def script(self, run):
        normal = super().script(run)
        self.release_limit = threading.Event()
        self.reset = None
        self.checks = 0

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            interrupted_request = unit.WORK if run.harness == "codex" else T2_TEXT
            if ("Dispatch metadata:" in text and interrupted_request in text
                    and "- capability: unit" in text and CAPACITY_REPLY not in text):
                if self.reset is None:
                    self.reset = int(time.time()) + 42
                    if run.harness == "codex":
                        # The native error renders only hours and minutes.
                        self.reset = ((self.reset + 59) // 60) * 60
                reset = datetime.fromtimestamp(self.reset, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                kind = "rate_limit_error" if record["protocol"] == "anthropic" else "usage_limit_reached"
                error = {"type": kind, "code": kind, "message": f"Usage limit reached. It resets {reset} (UTC).",
                         "resets_at": self.reset}
                return Reply(gate=self.release_limit, http_error=(429, {"type": "error", "error": error}))
            if CAPACITY_CHECK in last and record["last_kind"] == "user":
                self.checks += 1
                route = next(path.stem for path in self.route_files(run) if "." not in path.stem)
                return Reply(shell=shlex.join([sys.executable, str(_base().SOURCE / "utilities/capability-route.py"),
                                               "start", "--route", route]))
            return normal(record, earlier)
        return answer

    def wait_receipt(self, run, after):
        from harness_behavior_cases_e import last_json_object
        for request in unit.resting_session_requests(run)[after:]:
            if request["last_kind"] != "tool":
                continue
            value = last_json_object(request["last_text"])
            if value.get("route_id") and value.get("state"):
                return value
        return None

    def continue_route(self, run, ev):
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.reports(run)}
        run.prompt(CONT)
        return self.observe_capacity_resume(run, ev, before)

    def observe_capacity_resume(self, run, ev, before):
        interrupted_turn = len(before) + 1
        if not wait_for(lambda: self.reset, 85, 0.5):
            run.note("the interrupted turn did not reach the loopback model")
            return "fail"
        wait_for(lambda: len(self.unit_rows()) >= interrupted_turn, 20, 0.5)
        self.release_limit.set()
        terminal = wait_for(lambda: len(self.unit_rows()) >= interrupted_turn
                            and self.unit_rows()[interrupted_turn - 1]["status"] in TERMINAL_STATES, 215, 0.5)
        rows = self.unit_rows()
        if len(rows) < interrupted_turn:
            return "fail"
        source = rows[interrupted_turn - 1]
        ev["interrupted"] = {key: source.get(key) for key in (
            "attempt_id", "status", "launch_started", "note", "failure_class", "reconcile_reason", "reset")}
        ev["fixture_429s"] = sum(r["reply"].get("http_status") == 429 for r in run.main_requests())
        if not terminal or source.get("failure_class") != "capacity":
            run.note("the loopback 429 did not settle as a capacity end")
            log = Path(source["log_file"])
            if log.is_file():
                ev["native_error"] = log.read_text(encoding="utf-8", errors="replace")[-1400:]
            return "fail"
        ev["fixture_reset"] = datetime.fromtimestamp(self.reset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for index in range(2):
            prior = len(unit.resting_session_requests(run))
            run.wait_idle(1, 15)
            run.prompt(f"{CAPACITY_CHECK} ({index + 1})")
            receipt = wait_for(lambda: self.wait_receipt(run, prior), 22, 0.5) or {}
            ev.setdefault("before_reset", []).append({
                key: receipt.get(key) for key in ("state", "reason", "required_action", "retry_at", "auto_resume")})
        if not any(check["state"] == "waiting-capacity" for check in ev["before_reset"]):
            run.note(f"the existing route did not return a capacity wait: {ev['before_reset']}")
            return "fail"
        ev["rows_before_reset"] = len(self.unit_rows())
        expected_rows = interrupted_turn + 1
        done = wait_for(lambda: len(self.unit_rows()) == expected_rows
                        and self.unit_rows()[-1]["status"] in TERMINAL_STATES, self.WAIT, 1)
        rows = self.unit_rows()
        ev["capacity_rows"] = [self.row_view(r) | {key: r.get(key) for key in (
            "failure_class", "note", "route_id", "route_hash", "write_scope", "artifact_cycle_id")}
            for r in rows]
        ev["previous_report_kept"] = all(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
                                         for path, digest in before.items())
        if not done or len(rows) != expected_rows:
            run.note(f"capacity retry did not finish once, unit rows={len(rows)}")
            return "fail"
        successor = rows[-1]
        new_report = next((path for path in self.reports(run) if path not in before), None)
        delivered = wait_for(lambda: new_report and any(str(new_report) in r["text"]
                             for r in unit.resting_session_requests(run)), 65, 1)
        ev["returned_report"] = bool(delivered)
        ev["reports_after"] = len(self.reports(run))
        ev["delivery"] = {key: successor.get(key) for key in (
            "parent_completion_delivery", "parent_completion_reason", "launch_lifecycle", "delivery_intent")}
        if not delivered:
            ev["parent_notice_tails"] = [r["last_text"][-800:] for r in unit.resting_session_requests(run)[-5:]]
        context = (T1_MARK, T2_TEXT) if before else (unit.WORK,)
        ev["retained_context"] = any(CAPACITY_REPLY in r["text"] and all(marker in r["text"] for marker in context)
                                      for r in run.main_requests())
        return "pass" if (ev["rows_before_reset"] == interrupted_turn and ev["previous_report_kept"] and delivered
                          and ev["reports_after"] == interrupted_turn
                          and ev["retained_context"] and successor.get("session") == "resumed"
                          and successor.get("turn") == str(interrupted_turn)
                          and successor.get("continues_attempt_id") == source["attempt_id"]
                          and successor.get("failure_class") == "pass"
                          and all(successor.get(key) == source.get(key) for key in (
                              "route_id", "route_hash", "route_node", "write_scope", "permission_mode",
                              "runtime_sandbox"))) else "fail"

    def drive(self, run):
        try:
            run.prompt(unit.START)
            started = wait_for(lambda: self.unit_rows(), 60, 0.5)
            self.worker_end.set()
            if not started:
                for request in unit.resting_session_requests(run):
                    if request["last_kind"] == "tool":
                        run.note(request["last_text"][-650:])
                run.note("the initial unit did not register")
                run.note(run.tui.screen().strip()[-2500:])
                if run.harness == "codex":
                    probe = subprocess.run([sys.executable,
                                            str(_base().SOURCE / "utilities/dispatch-readiness.py"),
                                            "--owner-harness", "codex", "--child-harness", "codex",
                                            "--jobs", str(self.jobs), "--worktree", str(run.home.workspace),
                                            "--output", str(run.home.root / "readiness.json")],
                                           env=run.home.env, cwd=run.home.workspace,
                                           text=True, capture_output=True, timeout=30)
                    run.note((probe.stdout + probe.stderr)[-1200:])
                return "fail"
            if run.harness == "codex":
                return self.observe_capacity_resume(run, run.evidence, {})
            closed = wait_for(lambda: any(p.stem.endswith(".outcome") for p in self.route_files(run)), 45, 0.5)
            run.evidence["first_review_completed"] = bool(closed)
            if not closed or self.unit_rows()[0].get("failure_class") != "pass":
                first = self.unit_rows()[0]
                ev = run.evidence
                ev["row"] = {key: first.get(key) for key in (
                    "status", "note", "failure_class", "reconcile_reason", "launch_started", "runtime_sandbox")}
                path = Path(first["log_file"])
                if path.is_file():
                    ev["native_error"] = path.read_text(encoding="utf-8", errors="replace")[-1800:]
                return "fail"
            run.wait_idle(2, 20)
            return self.continue_route(run, run.evidence)
        finally:
            self.worker_end.set()
            self.release_limit.set()


SCENARIOS = [UnitContinueScenario, UnitContinueChainScenario, UnitContinueBlockedScenario,
             UnitContinueMovedScenario, UnitContinueMissingScenario, UnitContinueRetainedScenario,
             UnitCapacityContinueScenario]
