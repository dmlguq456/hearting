#!/usr/bin/env python3
"""A one-shot owner continued by a later owner of the same route, on the three harnesses
(`harness_behavior_scenarios.py`). The owner is a real registered `solo·quick` owner started through the harness's
dispatch wrapper and supervisor; the model is the loopback fixture.

``owner-continue``
    The owner ends PASS and the route closes. The session that opened it runs ``capability-route.py correct
    --route <id> --message-file <text>``: the route's next owner turn starts as a new attempt in a new cycle and
    wakes the first turn's session (its first model request carries what turn 1 said, and its newest message is
    the short turn text, not the whole assignment); it ends PASS and the route closes again. The same text sent
    again starts nothing (``already-asked``); the same text from another caller is refused and kept nowhere.
    Afterwards ``start --route`` reads the route as completed and the first turn reads as settled.

``owner-continue-missing``
    The same, with the first turn's session gone: the turn starts fresh (``session=fresh:session-missing``) and
    is told the whole assignment, the earlier result and why.

``owner-continue-answer``
    The owner ends BLOCKED. The session's ``start`` continues it once (``observed``) in the same session with
    the runtime's observation only; it ends BLOCKED again. The session's answer (``correct --message-file``)
    continues it in the same session; it ends PASS and the route closes.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES, _base  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402
from harness_behavior_cases_solo_review import SoloReviewScenario  # noqa: E402
from harness_behavior_cases_unit_continue import UnitContinueScenario  # noqa: E402

WORK = "SCENARIO-OC-WORK"
SLUG = "hb-owner-cont"
START = "SCENARIO-OC start the solo route and end your turn"
CONT = "SCENARIO-OC continue the closed route and end your turn"
AGAIN = "SCENARIO-OC send the same continuation again and end your turn"
RESTART = "SCENARIO-OC start the route again and end your turn"
ANSWER = "SCENARIO-OC answer the blocked owner and end your turn"
T1_MARK = "HB-OC-T1-MARK"
T2_TEXT = "HB-OC-T2-TEXT also add a second line to hb_oc_work.txt and report again."
ANSWER_TEXT = "HB-OC-ANSWER use the name hb-oc-final for the second line."
NEED = "HB-OC-NEED-CHOICE which name the second line uses"
OTHER_TEXT = "HB-OC-OTHER a request from another caller."
KERNEL = "# Portable Worker Kernel"
OUTPUT = re.compile(r"artifact_output_dir: (\S+)")


def _phase(text: str) -> str:
    """Which owner turn a request belongs to: the newest of the markers its conversation carries."""
    marks = {"t2": text.rfind("HB-OC-T2-TEXT"), "answer": text.rfind("HB-OC-ANSWER"),
             "observed": text.rfind("not an answer or an approval")}
    best = max(marks, key=marks.get)
    return best if marks[best] >= 0 else "t1"


class OwnerContinueScenario(SoloReviewScenario):
    name = "owner-continue"
    summary = ("A solo·quick owner ends PASS and its route closes; `correct --route` from the opening session "
               "starts the next owner turn in a new cycle that wakes the first turn's session; the route closes again.")
    intensity = "quick"
    qa = "quick"
    legs = ()
    variant = "resume"         # "resume" | "missing" | "answer"
    WAIT = 420

    # -- the fixture model ------------------------------------------------------------------------
    def parent_command(self, run, marker):
        """The command the session that opened the route runs for one scenario step."""
        files = self.route_files(run)
        route_id = files[0].stem if files else "none"

        def capability_route(*argv):
            return " ".join(shlex.quote(x) for x in [sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"),
                                                    *argv])
        if marker == START:
            return capability_route(
                "compose", "--start", "--shape", "solo", "--intensity", "quick", "--capability",
                "autopilot-code", "--pin", f"owner={run.harness}", "--pin", f"worker={run.harness}",
                "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned", "--slug", SLUG)
        if marker == RESTART:
            return capability_route("start", "--route", route_id)
        name = "answer.md" if marker == ANSWER else "cont.md"
        return capability_route("correct", "--route", route_id, "--message-file", str(run.home.root / name))

    def ask(self, run, marker):
        """One step of the opening session: typed into its TUI, or run as that session (`headless_parent`)."""
        if self.needs_tui:
            run.prompt(marker)
            return
        env = {key: value for key, value in {**run.home.env, **self.parent_identity}.items() if value}
        done = subprocess.run(["bash", "-c", self.parent_command(run, marker)], env=env, cwd=str(run.home.workspace),
                              text=True, capture_output=True, timeout=900)
        self.parent_outputs.append(done.stdout + done.stderr)

    def idle(self, run, *args):
        if self.needs_tui:
            run.wait_idle(*args)

    def script(self, run):
        self.worker_end = threading.Event()
        self.ran = set()
        self.parent_outputs = []
        cell = self

        def owner(record):
            text, last = record["text"], record["last_text"]
            phase = _phase(text)
            outputs = OUTPUT.findall(text)
            target = (outputs[-1] if outputs else "/nonexistent") + "/oc_report.md"
            line = {"t1": "first line " + T1_MARK, "t2": "second line HB-OC-T2-DONE",
                    "answer": "second line hb-oc-final", "observed": ""}[phase]
            if record["last_kind"] != "tool":
                if phase == "observed":       # the runtime's observation alone: still nothing a person answered
                    return Reply(f"artifact: -\nverdict: BLOCKED\nblocker: {NEED}")
                if run.harness == "codex":
                    # This host's nested Codex sandbox cannot start its tool socket in the temporary HOME: the
                    # fixture writes the report line in the attempt's own artifact folder, the owner ends as scripted.
                    report = Path(target)
                    if not report.resolve().is_relative_to(Path(run.home.env["AGENT_ARTIFACT_ROOT"]).resolve()):
                        raise ValueError("fixture report escaped its artifact root")
                    report.parent.mkdir(parents=True, exist_ok=True)
                    with report.open("a", encoding="utf-8") as stream:
                        stream.write(line + "\n")
                    verdict = "BLOCKED" if phase == "t1" and cell.variant == "answer" else "PASS"
                    blocker = NEED if verdict == "BLOCKED" else "none"
                    return Reply(f"artifact: {target}\nverdict: {verdict}\nblocker: {blocker}")
                return Reply(shell=(f"mkdir -p {shlex.quote(target.rsplit('/', 1)[0])} && "
                                    f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(target)} && "
                                    f"printf '%s\\n' {shlex.quote(line)} >> hb_oc_work.txt && git add hb_oc_work.txt && "
                                    f"git commit -q -m {shlex.quote(phase)} && git rev-parse --short HEAD"))
            if phase == "t1" and cell.variant == "answer":
                return Reply(f"artifact: {target}\nverdict: BLOCKED\nblocker: {NEED}")
            return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none")

        def answer(record, earlier):
            text, last = record["text"], record["last_text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture owner continue\nNOW: fixture")
            if "Dispatch metadata:" in text and WORK in text:
                return owner(record)
            if record["last_kind"] == "tool":
                return Reply("The route runs; I will continue when its result arrives.")
            for marker in (START, CONT, AGAIN, RESTART, ANSWER):
                if marker in last and marker not in cell.ran:
                    cell.ran.add(marker)
                    return Reply(shell=cell.parent_command(run, marker))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        unit.UnitRouteScenario.prepare(self, run)
        root = run.home.root
        (root / "brief.md").write_text(f"{WORK}: add one line to hb_oc_work.txt in the repository, commit it and "
                                       "report.\n", encoding="utf-8")
        (root / "cont.md").write_text(T2_TEXT + "\n", encoding="utf-8")
        (root / "answer.md").write_text(ANSWER_TEXT + "\n", encoding="utf-8")
        (root / "other.md").write_text(OTHER_TEXT + "\n", encoding="utf-8")
        if run.harness == "codex":
            # The fixture provider uses no OpenAI account: answer the offline login probe locally (as
            # `unit-capacity-continue` does) and leave every model call to Codex.
            bin_dir = root / "fixture-bin"
            bin_dir.mkdir(exist_ok=True)
            command = bin_dir / "codex"
            command.write_text("#!/bin/sh\n"
                               'if [ "$1" = login ] && [ "$2" = status ]; then\n'
                               "  echo 'Logged in using the loopback fixture'\n  exit 0\nfi\n"
                               f'exec {shlex.quote(run.binary)} "$@"\n', encoding="utf-8")
            command.chmod(0o700)
            run.home.env["PATH"] = f"{bin_dir}:{run.home.env['PATH']}"

    # -- what the run reads -----------------------------------------------------------------------
    def unit_rows(self):
        from harness_behavior_cases_e import registry_rows
        return [row for row in registry_rows(self.jobs) if SLUG in row.get("slug", "")]

    def owner_rows(self):
        return [r for r in self.unit_rows() if r.get("worker_type") == "owner"]

    def row_view(self, row):
        keys = ("attempt_id", "status", "turn", "continues_attempt_id", "automatic_retry_of", "session", "harness",
                "permission_mode", "runtime_sandbox", "failure_class", "note", "workflow_completion")
        return {k: row.get(k) for k in keys}

    def reports(self, run):
        root = Path(run.home.env["AGENT_ARTIFACT_ROOT"])
        return sorted(root.glob("campaigns/*/*/artifacts/oc_report.md")) + sorted(root.glob("plans/*/oc_report.md"))

    def owner_requests(self, run):
        return [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and WORK in r["text"]]

    def first_request_of(self, run, phase):
        """The first model request of the owner turn `phase` (what it was woken or started with)."""
        found = [r for r in self.owner_requests(run) if _phase(r["text"]) == phase]
        return found[0] if found else None

    def request_view(self, request, first_attempt=""):
        """What an owner turn's first request carried. Turn 1's history is its marker (what turn 1 ran) or turn 1's
        own dispatch line `guard_session_id: <turn 1 attempt>`, which only turn 1's prompt holds."""
        if request is None:
            return {}
        last = request["last_text"]
        own = bool(first_attempt) and f"guard_session_id: {first_attempt}" in request["text"]
        return {"has_t1_history": T1_MARK in request["text"] or own, "newest_has_kernel": KERNEL in last,
                "newest_has_brief": WORK in last, "newest_chars": len(last), "newest_head": last[:600],
                "newest_has_recovery": "Verified recovery context" in last,
                "newest_says_fresh": "could not be woken" in last}

    def wait_owner(self, count):
        wait_for(lambda: len(self.owner_rows()) >= count, 180, 1)
        return wait_for(lambda: (lambda rows: len(rows) >= count and rows[-1]["status"] in TERMINAL_STATES)(
            self.owner_rows()), self.WAIT, 2)

    def closure(self, run):
        files = self.route_files(run)
        if not files:
            return None
        path = files[0].with_name(files[0].stem + ".outcome.json")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def tool_outputs(self, run, needle):
        if not self.needs_tui:
            return [text[-1200:] for text in self.parent_outputs if needle in text]
        return [r["last_text"][-1200:] for r in unit.resting_session_requests(run)
                if r["last_kind"] == "tool" and needle in r["last_text"]]

    def store_entries(self, run, route_id):
        sys.path.insert(0, str(_base().SOURCE / "utilities"))
        import unit_continue
        return [e["id"] for e in unit_continue.entries(self.jobs, route_id)]

    # -- the run ----------------------------------------------------------------------------------
    def drive(self, run):
        try:
            if self.variant == "answer":
                return self.run_answer(run, run.evidence)
            return self.run_follow_up(run, run.evidence)
        finally:
            self.worker_end.set()
            if not self.needs_tui:
                run.evidence["parent_outputs"] = [text[-1500:] for text in self.parent_outputs]

    def start_first(self, run, ev):
        if not self.needs_tui:   # the readiness the compose below reads, kept beside the run
            probe = ("import importlib.util,json,sys\n"
                     "spec=importlib.util.spec_from_file_location('ndе',sys.argv[1]); m=importlib.util.module_from_spec(spec)\n"
                     "spec.loader.exec_module(m); print(json.dumps([m.auth_check(sys.argv[2],sys.argv[3]),"
                     "m.command_check(sys.argv[2],sys.argv[3])]))\n")
            env = {key: value for key, value in {**run.home.env, **self.parent_identity}.items() if value}
            done = subprocess.run([sys.executable, "-c", probe, str(_base().SOURCE / "utilities/nested-dispatch-eligibility.py"),
                                   run.harness, str(run.home.workspace)], env=env, cwd=str(run.home.workspace),
                                  text=True, capture_output=True, timeout=300)
            ev["readiness_probe"] = (done.stdout + done.stderr)[-1200:]
        self.ask(run, START)
        done = self.wait_owner(1)
        wait_for(lambda: False, 5, 1)
        self.idle(run, 8, 90)
        ev["first_rows"] = [self.row_view(r) for r in self.owner_rows()]
        return done

    def run_follow_up(self, run, ev):
        if not self.start_first(run, ev):
            run.note("the first owner never ended")
            return "inconclusive"
        wait_for(lambda: self.closure(run) is not None, 90, 1)
        ev["first_closure"] = {k: (self.closure(run) or {}).get(k) for k in ("terminal_gate_proven", "terminal_owner_attempt_id")}
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.reports(run)}
        ev["reports_before"] = list(before)
        first = self.owner_rows()
        route_file = next(iter(self.route_files(run)), None)
        route = json.loads(route_file.read_text(encoding="utf-8")) if route_file else {}
        if self.variant == "missing" and first:
            if run.harness == "opencode":
                sys.path.insert(0, str(_base().SOURCE / "utilities"))
                import unit_continue
                done = subprocess.run(["opencode", "session", "delete", unit_continue.session_id_of(first[-1])],
                                      env=dict(run.home.env), cwd=str(run.home.workspace), capture_output=True,
                                      text=True, timeout=60, stdin=subprocess.DEVNULL)
                ev["opencode_session_delete"] = {"rc": done.returncode, "out": (done.stdout + done.stderr)[-300:]}
            elif first[-1].get("runtime_home"):
                shutil.rmtree(first[-1]["runtime_home"], ignore_errors=True)
        # Another caller (this runner, not the session that opened the route) is refused and keeps nothing.
        other = subprocess.run([sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "correct",
                                "--route", route.get("route_id", "none"), "--message-file",
                                str(run.home.root / "other.md")], env=dict(run.home.env), cwd=str(run.home.workspace),
                               text=True, capture_output=True, timeout=120)
        ev["other_caller"] = (other.stdout + other.stderr)[-600:]
        ev["kept_after_other"] = self.store_entries(run, route.get("route_id", "none"))
        self.ask(run, CONT)
        done = self.wait_owner(2)
        wait_for(lambda: (self.closure(run) or {}).get("terminal_owner_attempt_id") == (self.owner_rows() or [{}])[-1].get("attempt_id"), 90, 1)
        self.idle(run, 8, 90)
        rows = self.owner_rows()
        ev["rows"] = [self.row_view(r) for r in rows]
        ev["superseded_closures"] = UnitContinueScenario.superseded(self, run)
        ev["closure"] = {k: (self.closure(run) or {}).get(k) for k in ("terminal_gate_proven", "terminal_owner_attempt_id")}
        after = self.reports(run)
        ev["reports_after"] = [str(p) for p in after]
        ev["first_report_kept"] = bool(before) and all(
            Path(p).exists() and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h for p, h in before.items())
        ev["second_report_has_marker"] = any("HB-OC-T2-DONE" in p.read_text(encoding="utf-8") for p in after)
        ev["turn2_request"] = self.request_view(self.first_request_of(run, "t2"), (first or [{}])[0].get("attempt_id", ""))
        ev["turn2_model_requests"] = sum(_phase(r["text"]) == "t2" for r in self.owner_requests(run))
        ev["correct_receipt"] = (self.tool_outputs(run, '"request_id"') or [""])[0]
        if not done or len(rows) < 2:
            return "inconclusive"
        # The same text again, from the same session: nothing new starts.
        self.ask(run, AGAIN)
        wait_for(lambda: bool(self.tool_outputs(run, "already-asked")), 120, 1)
        self.idle(run, 8, 60)
        ev["again_receipt"] = (self.tool_outputs(run, "already-asked") or [""])[0][:600]
        ev["rows_after_again"] = len(self.owner_rows())
        ev["kept"] = self.store_entries(run, route.get("route_id", "none"))
        rows = self.owner_rows()
        ev["start_route"] = self.start_route_again(run, route) if route else {}
        ev["earlier_delivery"] = UnitContinueScenario.earlier_delivery(self, run, rows, 2)
        ev["inspect"] = [UnitContinueScenario.terminal_inspect(self, run, r["attempt_id"]) for r in rows[:2]]
        ev["supervisor"] = self.supervisor_status(run, route) if route else {}
        second = rows[1]
        ev["same_authority"] = {k: (rows[0].get(k), second.get(k)) for k in ("permission_mode", "runtime_sandbox")}
        ok = (len(rows) == 2 and second.get("turn") == "2" and second.get("continues_attempt_id") == rows[0]["attempt_id"]
              and ev["first_report_kept"] and ev["second_report_has_marker"] and len(after) == 2
              and len(ev["superseded_closures"]) == 1 and (ev["closure"] or {}).get("terminal_gate_proven") is True
              and ev["closure"].get("terminal_owner_attempt_id") == second["attempt_id"]
              and all(a == b for a, b in ev["same_authority"].values())
              and '"not-admitted"' in ev["other_caller"] and ev["kept_after_other"] == ev["kept"][:len(ev["kept_after_other"])]
              and len(ev["kept"]) == 1 and ev["rows_after_again"] == 2 and "already-asked" in ev["again_receipt"]
              and ev["start_route"].get("state") == "completed"
              and all(d.get("classification") == "success" for d in ev["earlier_delivery"])
              and (ev["inspect"][0] or {}).get("state") == "not-applicable")
        request = ev["turn2_request"]
        if self.variant == "missing":
            ok = ok and second.get("session") == "fresh:session-missing" and request.get("newest_has_brief") \
                and request.get("newest_says_fresh") and not request.get("has_t1_history")
        else:
            ok = ok and second.get("session") == "resumed" and request.get("has_t1_history") \
                and not request.get("newest_has_kernel")
        run.note(f"rows={len(rows)} session={second.get('session')} closures_set_aside={len(ev['superseded_closures'])} "
                 f"reports={len(after)} start={ev['start_route'].get('state')}")
        return "pass" if ok else "fail"

    def run_answer(self, run, ev):
        if not self.start_first(run, ev):
            run.note("the first owner never ended")
            return "inconclusive"
        self.ask(run, RESTART)                           # the runtime continues a first BLOCKED once (observed)
        observed = self.wait_owner(2)
        self.idle(run, 8, 90)
        ev["restart_receipt"] = (self.tool_outputs(run, '"route_id"') or [""])[-1][:900]
        self.ask(run, ANSWER)                            # the second stop goes to a person: the session answers it
        answered = self.wait_owner(3)
        wait_for(lambda: self.closure(run) is not None, 90, 1)
        self.idle(run, 8, 90)
        rows = self.owner_rows()
        ev["rows"] = [self.row_view(r) for r in rows]
        ev["answer_receipt"] = (self.tool_outputs(run, "correction") or [""])[-1][:900]
        first_attempt = rows[0]["attempt_id"] if rows else ""
        ev["observed_request"] = self.request_view(self.first_request_of(run, "observed"), first_attempt)
        ev["answer_request"] = self.request_view(self.first_request_of(run, "answer"), first_attempt)
        ev["closure"] = {k: (self.closure(run) or {}).get(k) for k in ("terminal_gate_proven", "terminal_owner_attempt_id")}
        if not observed or not answered or len(rows) < 3:
            return "inconclusive"
        ok = (len(rows) == 3 and rows[1].get("automatic_retry_of") == rows[0]["attempt_id"]
              and rows[2].get("automatic_retry_of") == rows[1]["attempt_id"]
              and all(r.get("session") == "resumed" for r in rows[1:])
              and ev["observed_request"].get("has_t1_history") and ev["observed_request"].get("newest_has_recovery")
              and not ev["observed_request"].get("newest_has_kernel")
              and ev["answer_request"].get("has_t1_history") and not ev["answer_request"].get("newest_has_kernel")
              and (ev["closure"] or {}).get("terminal_gate_proven") is True)
        run.note(f"rows={len(rows)} sessions={[r.get('session') for r in rows]} closed={bool(ev['closure'])}")
        return "pass" if ok else "fail"


class OwnerContinueMissingScenario(OwnerContinueScenario):
    name = "owner-continue-missing"
    summary = "The same with the first turn's session gone: the next turn starts fresh and says why."
    variant = "missing"


class OwnerContinueAnswerScenario(OwnerContinueScenario):
    name = "owner-continue-answer"
    summary = ("A BLOCKED owner is continued once by the runtime's observation and then by the person's answer, "
               "both in the first owner's session; the route closes on the answer's PASS.")
    variant = "answer"


class HeadlessParent:
    """The opening session as a shell that carries one native session id instead of a TUI: the owner, its
    adapter, supervisor and fixture model run as in the TUI cases. A Codex TUI's tool sandbox has no network,
    so its readiness probe refuses a quick owner (`quick-headless-unavailable`) before anything starts."""
    needs_tui = False
    parent_identity = {"CODEX_THREAD_ID": "019a0000-0000-7000-8000-0000000c0de1",
                       "CLAUDE_CODE_SESSION_ID": "", "OPENCODE_SESSION_ID": ""}


class OwnerContinueHeadlessScenario(HeadlessParent, OwnerContinueScenario):
    name = "owner-continue-headless-parent"
    summary = OwnerContinueScenario.summary + " (opening session: a shell with one native session id)"


class OwnerContinueMissingHeadlessScenario(HeadlessParent, OwnerContinueMissingScenario):
    name = "owner-continue-missing-headless-parent"
    summary = OwnerContinueMissingScenario.summary + " (opening session: a shell with one native session id)"


class OwnerContinueAnswerHeadlessScenario(HeadlessParent, OwnerContinueAnswerScenario):
    name = "owner-continue-answer-headless-parent"
    summary = OwnerContinueAnswerScenario.summary + " (opening session: a shell with one native session id)"


SCENARIOS = [OwnerContinueScenario, OwnerContinueMissingScenario, OwnerContinueAnswerScenario,
             OwnerContinueHeadlessScenario, OwnerContinueMissingHeadlessScenario, OwnerContinueAnswerHeadlessScenario]
