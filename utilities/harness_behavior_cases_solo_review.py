#!/usr/bin/env python3
"""The middle cell `solo·standard`, run on the three harnesses (`harness_behavior_scenarios.py`).

One registered owner makes the work, starts the route's one declared review node with the command its
task gives, waits (the supervisor parks it), and is woken by the review's verdict in the same session.
The model is the loopback fixture; the owner and the review are real registered workers started through the
harness's dispatch wrapper, so the park, the join and the wake are the ones used in operation.

``solo-review-pass``      work, review `mergeable`, final handoff, the route closes, one completion notice.
``solo-review-fix``       review `changes requested`: the same owner session fixes, the closure check (second
                          round) is `mergeable`, the route closes.
``solo-review-spent``     the closure check blocks again: no third round, the owner is told to end `verdict: FAIL`.
``solo-review-missed``    the owner ends PASS without a review: the same session is woken with the start command.
``solo-review-stale``     the owner changes source after the review passed: its end is not accepted.
``solo-review-verdictless`` the review ends with no verdict: the run ends readable, never as a PASS.

The oracle reads what the harnesses share: the registry rows, the route's closed outcome, and the requests the
fixture model received (the owner's task paragraph, the wake text, the completion notice of the resting session).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, wait_for  # noqa: E402
from harness_behavior_cases_a import _carrying  # noqa: E402
from harness_behavior_cases_e import TERMINAL_STATES, registry_rows, _base  # noqa: E402
import harness_behavior_cases_unit_route as unit  # noqa: E402

WORK = "SCENARIO-SOLO-WORK"
START = "SCENARIO-SOLO start the solo route and end your turn"
SLUG = "hb-solo-review"
START_COMMAND = re.compile(r"`(python3 [^`]*stage-dispatch-fallback\.py --route [^`]*--node impl-review --start[^`]*)`")
SID = re.compile(r"guard_session_id: (att-\S+)")
OUTPUT = re.compile(r"artifact_output_dir: (\S+)")
FAIL_FINDING = "HB-SOLO-BLOCKER-77"


class SoloReviewScenario(unit.UnitRouteScenario):
    name = "solo-review-pass"
    summary = ("The middle cell: one owner makes the work, starts its review with the command its task gives, "
               "waits, and is woken by the verdict in the same session; the route closes after the final handoff.")
    # how the fixture review answers, round by round ('ok' mergeable, 'block' changes requested, 'none' no verdict)
    reviews = ("ok",)
    owner_skips_review = False
    owner_edits_after_pass = False
    WAIT = 420
    QUIET = 30

    def side_script(self, run):
        return None

    def final_reply(self, target):  # not used: the owner and the review script their own ends
        return None

    # -- the fixture model ----------------------------------------------------------------------
    def script(self, run):
        self.owner_steps = {}      # owner session id -> the steps it has taken
        self.review_order = []     # review session ids, in the order they first asked
        self.requests_by_session = {}
        cell = self

        def shell_edit(label):
            return (f"printf '%s\\n' {shlex.quote(label)} >> hb_solo_work.txt && git add hb_solo_work.txt "
                    f"&& git commit -q -m {shlex.quote(label)} && git rev-parse --short HEAD")

        def handoff(text, verdict, blocker="none"):
            output = OUTPUT.search(text)
            target = (output.group(1) if output else "/nonexistent") + "/solo_report.md"
            return target, f"artifact: {target}\nverdict: {verdict}\nblocker: {blocker}"

        def shell_step(record, command):
            """The owner's shell step; Codex's exec tool is given a yield long enough for the checked start."""
            if "exec_command" in (record.get("tools") or []) and "stage-dispatch-fallback" in command:
                return Reply(tool=("exec_command", {"cmd": command, "yield_time_ms": 120000}))
            return Reply(shell=command)

        def owner(record, session):
            text, last = record["text"], record["last_text"]
            steps = cell.owner_steps.setdefault(session, [])
            command = START_COMMAND.search(text)
            output = OUTPUT.search(text)
            target = (output.group(1) if output else "/nonexistent") + "/solo_report.md"
            running = re.search(r"Process running with session ID (\d+)", last)
            if record["last_kind"] == "tool" and running and "write_stdin" in (record.get("tools") or []):
                # a call the harness handed to the background: keep waiting for it, as its notice says
                return Reply(tool=("write_stdin", {"session_id": int(running.group(1)), "chars": "",
                                                   "yield_time_ms": 120000}))
            if "[workflow-completion-pending]" in last and START_COMMAND.search(last):
                found = START_COMMAND.search(last)
                steps.append("restart")
                return shell_step(record, found.group(1) if found else "echo no-start-command")
            if "Runtime wait was rejected" in last:
                # the runtime's own correction when a turn ended before the start receipt (a harness that hands a
                # long call to the background): run the same checked start again
                steps.append("restart")
                return shell_step(record, command.group(1) if command else "echo no-start-command")
            if record["last_kind"] == "tool":
                previous = steps[-1] if steps else ""
                poll = re.search(r"parent_next=bounded-wait[\s\S]*?parent_next_command=(\S+(?: --\S+ \S+)*)", last)
                if previous in ("start", "restart") and poll:
                    # a receipt that names its own bounded wait (no supervisor park on this harness): run it once
                    steps.append("poll")
                    return Reply(shell=poll.group(1))
                if previous == "poll":
                    # read the review's result as the task says, then act on it
                    steps.append("read")
                    review_file = (output.group(1) if output else "/nonexistent") + "/_internal/dev_reviews/phase_review.md"
                    return Reply(shell=f"cat {shlex.quote(review_file)} 2>&1 | head -5")
                if previous == "read":
                    if "mergeable" in last or "Final verdict: issues" in last:
                        if cell.owner_edits_after_pass and "edit-after-pass" not in steps:
                            steps.append("edit-after-pass")
                            return Reply(shell=shell_edit("changed after the review passed"))
                        steps.append("final")
                        return Reply(handoff(text, "PASS")[1])
                    if "fix" not in steps:
                        steps.append("fix")
                        return Reply(shell=shell_edit("solo fix"))
                    steps.append("fail")
                    return Reply(f"artifact: {target}\nverdict: FAIL\nblocker: {FAIL_FINDING} the closure check still blocks")
                if previous in ("work", "fix"):
                    if previous == "work" and cell.owner_skips_review and "skipped" not in steps:
                        steps.append("skipped")
                        return Reply(handoff(text, "PASS")[1])
                    steps.append("start")
                    return shell_step(record, command.group(1) if command else "echo no-start-command")
                if previous in ("start", "restart"):
                    steps.append("wait")
                    return Reply("runtime_wait: registered-children")
                if previous == "edit-after-pass":
                    steps.append("final")
                    return Reply(handoff(text, "PASS")[1])
                return Reply("Nothing to do.")
            # a request that carries no tool result: the first one, or a wake after a wait
            if not steps:
                steps.append("work")
                return Reply(shell=(f"mkdir -p {shlex.quote(target.rsplit('/', 1)[0])} && "
                                    f"printf '%s\\n' 'solo report' > {shlex.quote(target)} && "
                                    + shell_edit("solo work")))
            if "Do not correct further" in last:
                steps.append("fail")
                return Reply(f"artifact: {target}\nverdict: FAIL\nblocker: {FAIL_FINDING} the closure check still blocks")
            if "Fix every blocking finding" in last and "fix" not in steps:
                steps.append("fix")
                return Reply(shell=shell_edit("solo fix"))
            if steps[-1] == "wait" and cell.owner_edits_after_pass and "edit-after-pass" not in steps:
                steps.append("edit-after-pass")
                return Reply(shell=shell_edit("changed after the review passed"))
            if steps[-1] == "skipped":
                return Reply("Nothing to do.")
            steps.append("final")
            return Reply(f"artifact: {target}\nverdict: PASS\nblocker: none")

        def review(record, session):
            text = record["text"]
            if session not in cell.review_order:
                cell.review_order.append(session)
            round_number = cell.review_order.index(session)
            mode = cell.reviews[min(round_number, len(cell.reviews) - 1)]
            output = OUTPUT.search(text)
            target = (output.group(1) if output else "/nonexistent") + "/_internal/dev_reviews/phase_review.md"
            wrote = f"{session}-wrote" in cell.owner_steps
            if record["last_kind"] != "tool" and not wrote:
                cell.owner_steps[f"{session}-wrote"] = ["wrote"]
                if mode == "none":
                    return Reply("artifact: -\nverdict: BLOCKED\nblocker: fixture review reached no verdict")
                head = ("Final verdict: mergeable — head %s" if mode == "ok"
                        else "Final verdict: changes requested — head %s")
                body = ("\\n\\n## Blocking findings\\n\\n- " + FAIL_FINDING + ": the fixture ignores an empty list\\n"
                        if mode == "block" else "\\n\\nNo blocking findings.\\n")
                return Reply(shell=(f"mkdir -p \"$(dirname '{target}')\" && printf '%s{body}' "
                                    f"\"{head % '$(git rev-parse HEAD)'}\" > '{target}'"))
            if mode == "none":
                return Reply("artifact: -\nverdict: BLOCKED\nblocker: fixture review reached no verdict")
            verdict = "FAIL" if mode == "block" else "PASS"
            blocker = f"{FAIL_FINDING} the fixture ignores an empty list" if mode == "block" else "none"
            return Reply(f"artifact: {target}\nverdict: {verdict}\nblocker: {blocker}")

        def answer(record, earlier):
            text = record["text"]
            if unit.side_request(record):
                return Reply("TITLE: fixture solo review\nNOW: fixture")
            session = (SID.search(text) or [None, None])[1]
            if session is None and "Dispatch metadata:" in text:
                # OpenCode's task text carries no session id: the owner is the one owner session of the route; a
                # review process is a new one at each of its first (user-turn) requests
                if "- worker_type: review" in text:
                    if record["last_kind"] == "user":
                        cell.review_processes = getattr(cell, "review_processes", 0) + 1
                    session = f"review-{getattr(cell, 'review_processes', 1)}"
                else:
                    session = "owner"
            if session and "Dispatch metadata:" in text:
                cell.requests_by_session.setdefault(session, []).append(record["at"])
                if "- worker_type: review" in text:
                    return review(record, session)
                if WORK in text:
                    return owner(record, session)
            if record["last_kind"] == "tool":
                return Reply("The solo route runs; I will continue when its result arrives.")
            if START in record["last_text"] and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=" ".join([
                    sys.executable, str(_base().SOURCE / "utilities" / "capability-route.py"), "compose", "--start",
                    "--shape", "solo", "--intensity", "standard", "--capability", "autopilot-code",
                    "--pin", f"owner={run.harness}", "--pin", f"worker={run.harness}",
                    "--prompt-file", str(run.home.root / "brief.md"), "--cwd", str(run.home.workspace),
                    "--artifact-root", run.home.env["AGENT_ARTIFACT_ROOT"], "--unassigned", "--slug", SLUG]))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        super().prepare(run)
        (run.home.root / "brief.md").write_text(
            f"{WORK}: add one line to hb_solo_work.txt in the repository, check it, and commit it.\n", encoding="utf-8")

    # -- the run --------------------------------------------------------------------------------
    def unit_rows(self):
        return [row for row in registry_rows(self.jobs) if SLUG in row.get("slug", "")]

    def drive(self, run):
        try:
            return self.run_solo(run, run.evidence)
        finally:
            pass

    def run_solo(self, run, ev):
        run.prompt(START)
        started = wait_for(lambda: self.unit_rows(), 180, 1)
        if not started:
            run.note("the owner never registered")
            return "inconclusive"
        wait_for(lambda: any(r["last_kind"] == "tool" for r in unit.resting_session_requests(run)), 120, 1)
        before = len(unit.resting_session_requests(run))
        owner_rows = lambda: [r for r in self.unit_rows() if r.get("worker_type") == "owner"]  # noqa: E731
        done = wait_for(lambda: owner_rows() and owner_rows()[-1]["status"] in TERMINAL_STATES, self.WAIT, 2)
        route_file = next(iter(self.route_files(run)), None)
        route = json.loads(route_file.read_text(encoding="utf-8")) if route_file else {}
        outcome = route_file.with_name(route_file.stem + ".outcome.json") if route_file else None
        wait_for(lambda: outcome is not None and outcome.exists(), 60 if done else 5, 1)
        wait_for(lambda: False, self.QUIET, 1)
        run.wait_idle(8, 60)
        rows = self.unit_rows()
        owners = [r for r in rows if r.get("worker_type") == "owner"]
        reviews = [r for r in rows if r.get("worker_type") == "review"]
        requests = unit.resting_session_requests(run)
        route_id = route.get("route_id") or "rt-unknown"
        carriers = [r for r in requests[before:] if route_id in r["last_text"] and "HEARTING_RECEIPT_V1" in r["last_text"]
                    and "Dispatch metadata:" not in r["text"]]
        ev["route"] = {"effective_intensity": route.get("effective_intensity"),
                       "shape": (route.get("selection") or {}).get("shape"),
                       "nodes": [(n.get("id"), n.get("dispatch_depth"), n.get("model_profile")) for n in route.get("nodes", [])]}
        ev["owner_rows"] = [{k: r.get(k) for k in ("status", "route_node", "dispatch_depth", "harness", "note",
                                                   "failure_class", "workflow_completion", "model_profile")}
                            for r in owners]
        ev["review_rows"] = [{k: r.get(k) for k in ("status", "route_node", "dispatch_depth", "harness", "note",
                                                    "failure_class", "worker_type", "model_profile")} for r in reviews]
        ev["owner_sessions"] = {sid: steps for sid, steps in self.owner_steps.items() if not sid.endswith("-wrote")}
        ev["model_turns"] = {sid: len(times) for sid, times in self.requests_by_session.items()}
        ev["review_order"] = len(self.review_order)
        ev["route_closed"] = bool(outcome and outcome.exists())
        ev["outcome"] = json.loads(outcome.read_text(encoding="utf-8")) if ev["route_closed"] else None
        ev["completion_notices"] = len(carriers)
        ev["notice_tail"] = carriers[0]["last_text"][-900:] if carriers else ""
        ev["notice_full"] = carriers[0]["last_text"][:3500] if carriers else ""
        ev["after_requests"] = [r["last_text"][:260].replace("\n", " | ") for r in requests[before:]]
        ev["task_paragraph_seen"] = any("This route is one session that makes the work" in r["text"]
                                        for r in self.owner_requests(run))
        owner_requests = self.owner_requests(run)
        ev["owner_wake_texts"] = [r["last_text"][-700:] for r in owner_requests if r["last_kind"] != "tool"][1:]
        first = owner_requests[0]["text"] if owner_requests else ""
        mark = first.find("This route is one session that makes the work")
        ev["task_paragraph"] = first[mark:mark + 1500].split("\n\n")[0] if mark >= 0 else ""
        ev["verdict_texts"] = [r["last_text"][r["last_text"].index("Review FAIL"):][:700] for r in owner_requests
                               if "Review FAIL" in r["last_text"]]
        ev["pending_texts"] = [r["last_text"][r["last_text"].index("[workflow-completion-pending]"):][:2600]
                               for r in owner_requests if "[workflow-completion-pending]" in r["last_text"]]
        ev["owner_requests"] = len(owner_requests)
        ev["supervisor_turns"] = [self.supervisor_turns(row) for row in owners]
        # what each start command answered the owner (the receipts that show a refused or degraded start)
        ev["start_results"] = [r["last_text"][-900:] for r in owner_requests
                               if r["last_kind"] == "tool" and ("selected_hop" in r["last_text"]
                                                                or "check=failed" in r["last_text"]
                                                                or "reason=" in r["last_text"])]
        ev["resting_fail_notice"] = next((r["last_text"][:1400] for r in requests[before:]
                                          if "NOTIFICATION" in r["last_text"] or "HEARTING_RECEIPT_V1" in r["last_text"]
                                          or "AGENT_HARNESS_COMPLETION_V1" in r["last_text"]), "")
        ev["session_proof"] = self.session_proof(ev)
        ok = self.judge(run, ev, owners, reviews)
        run.note(f"owner rows={len(owners)} review rows={len(reviews)} closed={ev['route_closed']} "
                 f"notices={ev['completion_notices']} steps={list(ev['owner_sessions'].values())} "
                 f"same-session={ev['session_proof']}"
                 + (" (this harness leaves no supervisor turn record to prove it)" if ev["session_proof"] == "unproven" else ""))
        if not owners or not done:
            return "inconclusive"
        return "pass" if ok else "fail"

    @staticmethod
    def supervisor_turns(row):
        """The owner attempt's own supervisor records (its log is a receipt log): one entry per model turn the
        supervisor ran for it. Claude's and OpenCode's supervisor records `resume` (the turn continued the bound
        session); Codex's records the thread each turn ran in. Empty when no record can be read."""
        turns = []
        try:
            text = Path(row.get("log_file") or "").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return turns
        for line in text.splitlines():
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            if kind == "dispatch.supervisor.turn-started":
                turns.append({"resume": bool(record.get("resume")), "thread": None})
            elif kind == "dispatch.supervisor.turn.started":
                turns.append({"resume": None, "thread": record.get("thread_id")})
        return turns

    @staticmethod
    def session_proof(ev, wakes=True):
        """Whether the owner was woken in its one session, from the supervisor's own turn records and not from the
        fixture's session labels (OpenCode names every session `owner`): `proven` (two or more turns, every later
        one a resume of the bound session or in the same thread), `broken` (a later turn began a new session),
        `unproven` (no readable record on this harness, or no wake to prove)."""
        turns = next(iter(ev.get("supervisor_turns") or []), [])
        if not turns:
            return "unproven"
        if len(turns) < 2:
            return "unproven" if not wakes else "broken"
        if all(turn["thread"] is not None for turn in turns):
            return "proven" if len({turn["thread"] for turn in turns}) == 1 else "broken"
        return "proven" if all(turn["resume"] for turn in turns[1:]) else "broken"

    def owner_requests(self, run):
        return [r for r in run.main_requests() if "Dispatch metadata:" in r["text"] and WORK in r["text"]
                and "- worker_type: review" not in r["text"]]

    def closed(self, ev):
        return bool(ev["route_closed"] and (ev["outcome"] or {}).get("terminal_gate_proven") is True)

    def judge(self, run, ev, owners, reviews):
        steps = next(iter(ev["owner_sessions"].values()), [])
        ok = (len(owners) == 1 and ev["task_paragraph_seen"] and ev["route"]["effective_intensity"] == "standard"
              and ev["route"]["shape"] == "solo" and [n[0] for n in ev["route"]["nodes"]] == ["impl-review", "one-shot"])
        # one completion notice reaches the session that started the route (it is watched for QUIET seconds after)
        return (ok and self.closed(ev) and len(reviews) == 1 and steps[-1] == "final"
                and ev["completion_notices"] == 1 and ev["session_proof"] != "broken")


class SoloReviewFixScenario(SoloReviewScenario):
    name = "solo-review-fix"
    summary = ("The review blocks; the same owner session fixes in its worktree and starts the closure check, "
               "which is `mergeable`; the route closes after one correction.")
    reviews = ("block", "ok")

    def judge(self, run, ev, owners, reviews):
        steps = next(iter(ev["owner_sessions"].values()), [])
        # the runtime's verdict text reached the owner and said what to do, and the owner did it in its session
        verdict = any("Fix every blocking finding" in text and "start the closure check with the same command" in text
                      for text in ev["verdict_texts"])
        return (len(owners) == 1 and len(reviews) == 2 and verdict and ev["session_proof"] != "broken"
                and "fix" in steps and steps.count("start") == 2 and steps[-1] == "final"
                and self.closed(ev))


class SoloReviewSpentScenario(SoloReviewScenario):
    name = "solo-review-spent"
    summary = ("The closure check blocks again: no third round, no owner-closure, the owner ends `verdict: FAIL` "
               "and the end reads as a failure.")
    reviews = ("block", "block")

    def judge(self, run, ev, owners, reviews):
        steps = next(iter(ev["owner_sessions"].values()), [])
        # the second verdict text told the owner to stop correcting and end failed, and it did
        verdict = any("Do not correct further" in text and "`verdict: FAIL`" in text for text in ev["verdict_texts"])
        return (len(owners) == 1 and len(reviews) == 2 and verdict and steps[-1] == "fail"
                and ev["session_proof"] != "broken" and not self.closed(ev) and bool(ev["resting_fail_notice"]))


class SoloReviewSkipScenario(SoloReviewScenario):
    name = "solo-review-missed"
    summary = ("The owner ends PASS without starting the review: the same session is woken with the start "
               "command, runs it, and the route closes after the review.")
    owner_skips_review = True

    def judge(self, run, ev, owners, reviews):
        steps = next(iter(ev["owner_sessions"].values()), [])
        # the runtime's wake text reached the owner with the start command, the owner ran it, the review ran, the route closed
        woken = any(START_COMMAND.search(text) and "has not run" in text for text in ev["pending_texts"])
        return (len(owners) >= 1 and "skipped" in steps and woken and "restart" in steps and len(reviews) == 1
                and self.closed(ev) and ev["session_proof"] != "broken")


class SoloReviewStaleScenario(SoloReviewScenario):
    name = "solo-review-stale"
    summary = ("The owner changes source after the review passed: the review is stale for the new content, "
               "so the owner's end is not accepted.")
    owner_edits_after_pass = True

    def judge(self, run, ev, owners, reviews):
        # The owner's first final handoff, made after it changed source, is not accepted: the same session is woken
        # with a text that names why (the review passed work that has since changed) and the start command, runs a
        # new review, and only then does the route close.
        steps = next(iter(ev["owner_sessions"].values()), [])
        edited = steps.index("edit-after-pass") if "edit-after-pass" in steps else -1
        if edited < 0:
            return False
        woken = any("completion-review-source-stale" in text and "passed, but the work changed after it" in text
                    and START_COMMAND.search(text) for text in ev["pending_texts"])
        return (len(owners) >= 1 and steps[edited:edited + 3] == ["edit-after-pass", "final", "restart"] and woken
                and len(reviews) == 2 and self.closed(ev) and ev["session_proof"] != "broken")


class SoloReviewVerdictlessScenario(SoloReviewScenario):
    name = "solo-review-verdictless"
    summary = ("The review ends with no verdict twice: the sealed fallback (no inline hop) and, when it cannot run, "
               "a readable end that is not a PASS.")
    reviews = ("none",)

    def judge(self, run, ev, owners, reviews):
        outcome = ev["outcome"] or {}
        # The end is readable and is not a PASS: the owner's row ends failed (never `completed`), the route does not
        # close, and two verdict-less rounds were the most the review got. Whether the resting session was also
        # notified inside the watch window is evidence (`resting_fail_notice`), not a condition of this check.
        failed = any(row.get("failure_class") not in ("pass", None) for row in ev["owner_rows"])
        return (len(owners) >= 1 and len(reviews) >= 2 and failed
                and not (ev["route_closed"] and outcome.get("terminal_gate_proven") is True))


SCENARIOS = [SoloReviewScenario, SoloReviewFixScenario, SoloReviewSpentScenario, SoloReviewSkipScenario,
             SoloReviewStaleScenario, SoloReviewVerdictlessScenario]
