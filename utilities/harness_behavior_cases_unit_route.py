#!/usr/bin/env python3
"""A unit route, launched and finished on the three harnesses (`harness_behavior_scenarios.py`).

``unit-route``
    The depth-0 session (the TUI, with no ``AGENT_DISPATCH_JOBS`` in its environment, as in
    operation) runs ``capability-route.py compose --start --unit qa/code-review --prompt-file
    <brief>`` and ends its turn. The route has no owner: the harness's dispatch wrapper starts the
    one unit worker on the fixture model, the worker writes its review report and ends, the
    completion controller closes the route, and the resting session is woken with the route's
    sealed summary (the verdict line, the blocking findings and the report path).

``unit-route-blocking``
    The same, but the worker's review finds a blocking issue, opens its report on a closed
    negative verdict (``Final verdict: changes requested — head <sha>``, what its first instruction asks) and ends ``verdict: FAIL`` (the most
    common end of a PR review). The runtime reads that end as finished work: the row is a success
    whose terminal reading carries the negative assessment, the route closes like a pass's, the
    resting session is woken with the same sealed summary (its result line says the review completed
    with findings), no further follow-up reaches it afterwards (watched on every harness), the
    supervisor reads the route as closed and ``start --route`` reports it completed.

The oracle reads what the three harnesses share: the registry row (one depth-1 attempt, the
unit's worker type, ``workflow_completion=runtime-v1``), the closed route, and the request the
woken session sent to its model. Synthetic data stays under the fixture root.
"""
from __future__ import annotations

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
import harness_behavior_scenarios as base  # noqa: E402
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_a import _carrying  # noqa: E402
from harness_behavior_cases_e import (  # noqa: E402
    OwnerPublishRegisteredScenario, TERMINAL_STATES, fixture_model_id, fixture_models_conf, registry_rows,
    _base,
)

WORK = "SCENARIO-UNIT-WORK"
START = "SCENARIO-UNIT start the unit route and end your turn"
BLOCKER = "HB-UNIT-BLOCKER-42"
NEXT = "HB-UNIT-NEXT"
REPORT = (
    "# Review of the fixture change\n\n"
    "{head}\n\n"
    "## Blocking findings\n\n"
    f"- {BLOCKER}: the fixture function ignores an empty list\n\n"
    "## Verification\n\n"
    "- Read the fixture diff; no command was run\n"
)
# What the unit worker's first instruction asks a review to open its report with: the closed verdict the runtime reads.
FAIL_HEAD = "Final verdict: changes requested — head 0123abc"
PASS_HEAD = "Final verdict: issues"


def side_request(record) -> bool:
    """A session-title/summary side request (the TUI asks for `TITLE:`/`NOW:` lines). OpenCode sends it with
    tool definitions, so the fixture counts it among the main requests; it is not a turn of the session."""
    return "after the NOW: line" in record["last_text"]


def resting_session_requests(run):
    """The model requests of the depth-0 session: the unit worker's own requests carry its dispatch prompt."""
    return [r for r in run.main_requests() if "Dispatch metadata:" not in r["text"] and not side_request(r)]


class UnitRouteScenario(OwnerPublishRegisteredScenario):
    name = "unit-route"
    summary = ("A unit route (`compose --start --unit qa/code-review`) starts one registered worker with no "
               "owner; its end closes the route and the sealed summary wakes the resting session.")
    needs_tui = True
    WAIT = 240
    blocking = False   # the worker ends FAIL with its blocking findings instead of PASS
    report_head = PASS_HEAD   # the report's verdict line (the blocking scenario's closes a negative judgment)
    QUIET = 45         # seconds the woken session is watched for a further follow-up (blocking scenario)

    def side_script(self, run):
        return None

    def final_reply(self, target) -> Reply:
        if self.blocking:
            return Reply(f"artifact: {target}\nverdict: FAIL\nblocker: {BLOCKER} the fixture function ignores an empty list",
                         gate=self.worker_end)
        return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none", gate=self.worker_end)

    # -- the fixture model ----------------------------------------------------------------------
    def script(self, run):
        self.worker_end = threading.Event()   # the worker's last answer waits until the session rests

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if side_request(record):                                   # a title request is no turn: answer it as one
                return Reply("TITLE: fixture unit route\nNOW: fixture")
            if "Dispatch metadata:" in text and WORK in text:          # the unit worker
                output = re.search(r"artifact_output_dir: (\S+)", text)
                target = (output.group(1) if output else "/nonexistent") + "/units/unit/report.md"
                wrote = any("report.md" in str(r["reply"].get("shell") or "") for r in earlier)
                if record["last_kind"] != "tool" and not wrote:
                    run.evidence["worker_first_request_chars"] = len(text)
                    # printf, not a here-document: the shell makes a temp file for one, which a worker's
                    # sandbox refuses where the folder is not a repository.
                    return Reply(shell=f"mkdir -p \"$(dirname '{target}')\" && printf %s "
                                       f"{shlex.quote(REPORT.format(head=self.report_head))} > '{target}'")
                return self.final_reply(target)
            if record["last_kind"] == "tool":
                return Reply("The unit route runs; I will continue when its result arrives.")
            if (not self.blocking and BLOCKER in text
                    and not any(NEXT in str(r["reply"].get("shell") or "") for r in earlier)):
                return Reply(shell=f"echo {NEXT}-$((20+22))")
            if START in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=" ".join([
                    sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose", "--start",
                    "--unit", "qa/code-review", "--pin", f"worker={run.harness}",
                    "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                    "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned",
                    "--slug", "hb-unit-route"]))
            return Reply("Nothing new.")
        return answer

    # -- fixture --------------------------------------------------------------------------------
    def prepare(self, run):
        root, ws, env = run.home.root, run.home.workspace, run.home.env
        for args in (["config", "user.email", "fixture@example.invalid"],
                     ["config", "user.name", "fixture"], ["checkout", "-q", "-b", "hb-task"]):
            subprocess.run(["git", "-C", str(ws), *args], check=True, env=env, capture_output=True)
        (ws / "README.md").write_text("unit fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(ws), "add", "README.md"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "fixture"], check=True, env=env,
                       capture_output=True)
        (root / "brief.md").write_text(f"{WORK}: review the fixture change and report your findings.\n",
                                       encoding="utf-8")
        # The depth-0 session has no AGENT_DISPATCH_JOBS: the canonical registry is the one every carrier reads.
        self.jobs = Path(env.pop("AGENT_DISPATCH_JOBS"))
        env["HEARTING_PRIOR_ART"] = "off"
        sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import model_config
        model = fixture_model_id(run.harness)
        user = model_config.user_path(run.harness, environ=env)
        user.parent.mkdir(parents=True, exist_ok=True)
        shipped = _base().SOURCE / "adapters" / run.harness / "config" / "models.conf"
        user.write_text(fixture_models_conf(shipped.read_text(encoding="utf-8"), model), encoding="utf-8")
        if run.harness == "codex":
            auth = Path(env["CODEX_HOME"]) / "auth.json"
            if not auth.exists():
                auth.write_text(json.dumps({"OPENAI_API_KEY": "sk-hearting-fixture-not-a-key"}), encoding="utf-8")
                auth.chmod(0o600)
            run.evidence["runtime_projection"] = self.complete_codex_projection(run)
        if run.harness == "opencode":
            run.evidence["fixture_auth"] = self.opencode_fixture_auth(run)
            run.evidence["guard_render"] = self.render_opencode_guard(run)
        wrong = {name: value for name, value in self.resolved_models(run).items() if value != model}
        if wrong:
            raise RuntimeError(f"worker model would leave the fixture: {wrong}")

    def unit_rows(self):
        return [row for row in registry_rows(self.jobs) if "hb-unit-route" in row.get("slug", "")]

    def route_files(self, run):
        return sorted((Path(run.home.env["AGENT_ARTIFACT_ROOT"]) / ".runtime" / "routes").glob("rt-*.json"))

    # -- the run --------------------------------------------------------------------------------
    def start_route_again(self, run, route):
        """`start --route` for the route, as the resting session would run it after the end (its receipt's fields)."""
        env = dict(run.home.env)
        done = subprocess.run([sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "start",
                               "--route", route["route_id"]], env=env, cwd=str(run.home.workspace), text=True,
                              capture_output=True, timeout=120)
        text = done.stdout + done.stderr
        try:
            start = text.index("{")
            receipt, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            receipt = {}
        return {"state": receipt.get("state"), "reason": receipt.get("reason"),
                "required_action": receipt.get("required_action"), "next_step": receipt.get("next_step"),
                "has_summary": bool(receipt.get("summary")), "mentions_compose_again": "compose-again" in text,
                "exit": done.returncode, "output": text[:1800]}

    def supervisor_status(self, run, route):
        """`workflow-supervisor status` for the route, as a watcher would read it."""
        done = subprocess.run([sys.executable, str(_base().SOURCE / "utilities" / "workflow-supervisor.py"), "status",
                               "--route", route["route_id"], "--jobs", str(self.jobs), "--json"],
                              env=dict(run.home.env), cwd=str(run.home.workspace), text=True, capture_output=True,
                              timeout=120)
        try:
            view = json.loads(done.stdout)
        except ValueError:
            return {"exit": done.returncode}
        return {"exit": done.returncode, "workflow_state": view.get("workflow_state"),
                "next_stage": view.get("next_stage"), "route_closed": view.get("route_closed"),
                "terminal_gate": view.get("terminal_gate")}

    def terminal_reading(self, run, row, route):
        """What the runtime reads from the leg's own exact end: the verdict it acts on and the negative assessment."""
        import codex_dispatch_terminal as terminal
        view = terminal.inspect_terminal_attempt(
            row.get("log_file"), worktree=route.get("cwd"), artifact_root_metadata=route.get("artifact_root"),
            worker_type=row.get("worker_type"), artifact_binding=row)
        return {key: view.get(key) for key in ("state", "verdict", "assessment_verdict", "failure_class", "failure_note")}

    def drive(self, run):
        ev = run.evidence
        try:
            return self.run_unit_route(run, ev)
        finally:
            self.worker_end.set()

    def run_unit_route(self, run, ev):
        run.prompt(START)
        started = wait_for(lambda: self.unit_rows(), 120, 1)
        ev["rows_after_start"] = [{k: r.get(k) for k in ("status", "worker_type", "unit", "route_node", "dispatch_depth",
                                                          "workflow_completion", "harness", "model_profile")}
                                  for r in self.unit_rows()]
        if not started:
            run.note("the unit worker never registered")
            return "inconclusive"
        # The worker has not ended (its last answer is held): wait until the session has its start result
        # and rests, so the end reaches a resting session through the wake path as it does in operation.
        wait_for(lambda: any(r["last_kind"] == "tool" for r in resting_session_requests(run)), 90, 1)
        run.wait_idle(6, 60)
        before = len(resting_session_requests(run))
        ev["requests_before_completion"] = before
        ev["alive_before_end"] = [
            line.strip()[:260] for line in subprocess.run(["ps", "-eo", "pid,ppid,pgid,etimes,args"], text=True,
                                                          capture_output=True).stdout.splitlines()
            if str(run.home.root) in line and "ps -eo" not in line and "tmux" not in line][:25]
        self.worker_end.set()
        done = wait_for(lambda: (lambda rows: rows and rows[-1]["status"] in TERMINAL_STATES)(self.unit_rows()),
                        self.WAIT, 2)
        completed_at = time.time()
        woke = wait_for(lambda: _carrying(resting_session_requests(run), BLOCKER, before), self.WAIT, 1)
        first = None
        run.wait_idle(8, 60)
        requests = resting_session_requests(run)
        rows = self.unit_rows()
        route_file = next(iter(self.route_files(run)), None)
        route = json.loads(route_file.read_text(encoding="utf-8")) if route_file else {}
        outcome = route_file.with_name(route_file.stem + ".outcome.json") if route_file else None
        ev["unit_rows"] = len(rows)
        ev["row"] = {k: rows[-1].get(k) for k in ("status", "worker_type", "unit", "route_node", "workflow_completion",
                                                  "harness", "note", "dispatch_depth", "model_profile",
                                                  "failure_class") } if rows else None
        ev["route_nodes"] = [(n.get("id"), n.get("unit"), n.get("launch_authority")) for n in route.get("nodes", [])]
        ev["route_closed"] = bool(outcome and outcome.exists())
        ev["route_outcome"] = outcome.read_text(encoding="utf-8")[:600] if ev["route_closed"] else None
        ev["worker_done"] = bool(done)
        # The summary reached the session either as a wake after it rested, or (when the worker ended before the
        # start command returned) inside the start result; both are the delivery. Which one is evidence.
        carriers = woke or [r for r in requests if BLOCKER in r["text"]]
        ev["delivered_by"] = "wake" if woke else "start-receipt" if carriers else "none"
        if carriers:
            first = carriers[0]
            if woke:
                ev["wake_seconds"] = round(first["at"] - completed_at, 1)
            ev["wake_request_tail"] = first["last_text"][-1800:]
            ev["summary_in_wake"] = {name: name in first["text"] for name in
                                     (BLOCKER, "issues", "report.md", "route rt-")}
            ev["next_action_ran"] = any(f"{NEXT}-42" in r["text"] for r in requests[requests.index(first):])
        ok = (rows and len(rows) == 1 and rows[-1].get("worker_type") == "review"
              and rows[-1].get("workflow_completion") == "runtime-v1" and carriers
              and ev["summary_in_wake"]["report.md"] and ev["summary_in_wake"][BLOCKER])
        if self.blocking:
            tail = first["last_text"] if carriers else ""
            ev["row_note"] = rows[-1].get("note") if rows else None
            ev["terminal_reading"] = self.terminal_reading(run, rows[-1], route) if rows and route else {}
            ev["result_line"] = next((line for line in tail.splitlines() if line.startswith("Result:")), "")
            ev["wake_has_dead_or_frame_words"] = [w for w in ("current frame results", "a failed frame", "compose-again",
                                                              "inspect-done-failure") if w in tail]
            # The end is finished work, so nothing more is owed afterwards: the woken session answers in text
            # and its turn ends, so any later request of it is a follow-up the runtime pushed (watched for a while).
            wait_for(lambda: False, self.QUIET, 1)
            ev["followups_after_wake"] = [r["last_text"][:200] for r in resting_session_requests(run)
                                          if first and r["at"] > first["at"]]
            ev["start_route_after"] = self.start_route_again(run, route) if route else {}
            ev["supervisor_status"] = self.supervisor_status(run, route) if route else {}
            reading = ev["terminal_reading"]
            ok = (ok and ev["route_closed"] and ev["row"]["failure_class"] == "pass"
                  and ev["row_note"] != "completed-review-blocking"
                  and (reading.get("verdict"), reading.get("assessment_verdict")) == ("PASS", "FAIL")
                  and "completed with findings" in ev["result_line"] and not ev["wake_has_dead_or_frame_words"]
                  and not ev["followups_after_wake"]
                  and ev["start_route_after"].get("state") == "completed"
                  and not ev["start_route_after"].get("mentions_compose_again")
                  and ev["supervisor_status"].get("workflow_state") not in (None, "CREATED")
                  and not ev["supervisor_status"].get("next_stage"))
        else:
            ok = ok and ev["route_closed"]
        run.note(f"unit rows={len(rows)}; closed={ev['route_closed']}; delivered_by={ev['delivered_by']}"
                 + (f" after {ev['wake_seconds']}s" if woke else ""))
        if not rows or not done:
            return "inconclusive"
        return "pass" if ok and (ev.get("next_action_ran") or self.blocking) else "fail"


class UnitRouteBlockingScenario(UnitRouteScenario):
    name = "unit-route-blocking"
    summary = ("A unit route whose review finds a blocking issue and ends FAIL: the end is finished work like a "
               "pass's: the route closes, the resting session gets the sealed summary and no follow-up after it.")
    blocking = True
    report_head = FAIL_HEAD


class UnitRouteNoGitScenario(UnitRouteBlockingScenario):
    name = "unit-route-no-git"
    summary = ("The same blocking review run in a folder that is not a git repository: its closed verdict line has "
               "no head to name, and the end is still finished work (the route closes, the summary arrives).")
    report_head = "Final verdict: changes requested"

    def prepare(self, run):
        super().prepare(run)
        shutil.rmtree(run.home.workspace / ".git")   # the folder the review reads is no repository


SCENARIOS = [UnitRouteScenario, UnitRouteBlockingScenario, UnitRouteNoGitScenario]
