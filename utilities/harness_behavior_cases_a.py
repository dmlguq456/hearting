#!/usr/bin/env python3
"""Wake/stop/periodic behaviour scenarios for ``harness_behavior_scenarios.py``.

Three scenarios, each measured on the real TUI through the loopback model:

``completion-receipt``
    The parent registers one depth-1 child exactly as a launch leaves it in the
    registry (an open row bound to this session, a live child process), ends its
    turn, and the child then completes (process exits, row closes, pending
    delivery record materializes).  Pass: a new model request carrying the
    attempt's receipt arrives with no typed prompt, and the model's next action
    (a shell call) runs.

``stop-with-remaining-action``
    The in-turn carrier that was armed at launch is gone (Claude: its
    ``asyncRewake`` window lapsed, Codex: no queue sidecar, OpenCode: n/a, the
    plugin is in-process).  The child completes *during* the next turn, so when
    the model gives its final text answer a receipt is still owed.  Pass: the
    turn-end boundary asks the model again with the receipt.

``periodic-check-due``
    The session takes the steward role with the real ``peer-steward steward on``
    command, and the periodic steward-check duty is made due by moving its
    schedule anchor back past the configured interval.  Pass: the obligation
    runner's notice reaches the model with no typed prompt and the model runs
    ``peer-steward steward check``.  When no wake arrives the scenario types one
    prompt to record whether the notice rides the next prompt instead.

Synthetic data (only under the fixture root): one registry row in the fixture's
``AGENT_DISPATCH_JOBS``, a ``sleep`` process standing in for the child, the
pending-delivery record written by the production close helpers
(``dispatch_contract.close_attempt_row`` +
``dispatch_completion_join.materialize_after_terminal_close``), and, for the
periodic case, a rewritten ``anchor`` in the steward-check duty observation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_behavior_scenarios import Reply, Scenario, wait_for  # noqa: E402


# --------------------------------------------------------------------------
# The synthetic child helper.  Written into the fixture root and run either by
# the model's own shell call (register, as a launch would, from inside the
# parent session) or by the driver (complete, as the child's end would).
# --------------------------------------------------------------------------

FX_HELPER = r'''
import json, os, signal, subprocess, sys, time, hashlib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF = json.loads((HERE / "hb_fx.json").read_text())
home = Path(os.environ["AGENT_HOME"])
sys.path.insert(0, str(home / "utilities"))
jobs = Path(os.environ["AGENT_DISPATCH_JOBS"])
attempt = CONF["attempt"]
state_file = HERE / "hb_fx_state.json"


def session():
    import session_identity
    ident = session_identity.identity()
    harness, sid = ident.harness, ident.session_id
    if not sid and CONF["harness"] == "claude":
        # Older Claude builds export no id to Bash: use the live transcript name.
        found = sorted(Path(os.environ["CLAUDE_CONFIG_DIR"], "projects").glob("*/*.jsonl"),
                       key=lambda p: p.stat().st_mtime)
        sid = found[-1].stem if found else ""
        harness = "claude"
    return harness or CONF["harness"], sid, ident.source or "transcript"


def register(flags):
    from harness_capabilities import parent_completion
    harness, sid, source = session()
    carrier = parent_completion(harness).get("carrier", "")
    # Give every harness path a real sealed-summary fixture in the same terminal
    # slot read by work_intent.sealed_summary().
    import dispatch_terminal_commit, work_intent
    route_id = "rt-" + hashlib.sha256(attempt.encode()).hexdigest()[:16]
    artifact_root = HERE / "hb_fx_artifacts"
    route_file = HERE / "hb_fx_route.json"
    route_file.write_text(json.dumps({"route_id": route_id, "artifact_root": str(artifact_root)}),
                          encoding="utf-8")
    event = CONF.get("event", "success")
    event_tokens = {"failure": "HB-FAIL-CAUSE", "partial": "HB-UNRUN-CHECK",
                    "decision": "HB-DECISION-A-OR-B"}
    event_fact = event_tokens.get(event, "HB-SUCCESS")
    summary = (f"{work_intent.summary_heads()[0]}\nResult: simulated {event} completion.\n"
               f"{event_fact}\nRecord: HB-RECORD-PATH")
    summary_path = dispatch_terminal_commit.terminal_slot(
        artifact_root, route_id, attempt) / work_intent.SUMMARY_NAME
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")
    sealed = work_intent.sealed_summary({"route_file": str(route_file)}, attempt)
    if sealed != summary:
        raise RuntimeError("fixture summary did not pass sealed_summary validation")
    jobs.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(["sleep", "900"], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stat = Path(f"/proc/{child.pid}/stat").read_text()
    start = stat.rsplit(")", 1)[1].split()[19]
    ns = os.readlink(f"/proc/{child.pid}/ns/pid")
    meta = ["attempt_schema_version=2", "capability=autopilot-code", "dispatch_depth=1",
            "worker_type=review", "transport=headless", "execution_surface=registered-headless",
            "registered_worker=1", "fallback_hop=same-harness-headless", f"harness={harness}",
            f"parent_sid={sid}", f"parent_completion_delivery={carrier}",
            f"route_file={route_file}"]
    binding = None
    try:
        from dispatch_contract import runtime_ancestry_binding
        binding = runtime_ancestry_binding(os.getpid())
    except Exception:
        binding = None
    if binding:
        meta += [f"parent_runtime_pid={binding[0]}", f"parent_runtime_pid_start={binding[1]}",
                 f"parent_runtime_ns={binding[2]}"]
    meta += ["launch_claimed=1", f"pid={child.pid}", f"pgid={child.pid}", f"pid_start={start}",
             f"pid_ns={ns}", f"pid_observer_ns={ns}", "launch_started=1", f"attempt_id={attempt}"]
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with jobs.open("a", encoding="utf-8") as handle:
        handle.write("\t".join([stamp, "open", str(HERE), str(HERE), "hbfx-child", ",".join(meta)]) + "\n")
    sidecar = None
    if harness == "codex" and "nocarrier" not in flags:
        # The codex launch path starts this exact sidecar for a codex parent
        # (adapters/*/bin/dispatch-headless.py -> launch_codex_queue_completion_sidecar).
        try:
            from codex_queue_dispatch import launch_codex_queue_completion_sidecar, resolve_queue_socket
            socket = resolve_queue_socket(os.environ)
            car = launch_codex_queue_completion_sidecar(jobs=jobs, parent_session_id=sid,
                                                        attempt_ids={attempt})
            sidecar = {"pid": car.pid, "log": str(car.log_file), "socket": str(socket),
                       "socket_exists": socket.exists()}
        except Exception as exc:
            sidecar = {"error": f"{type(exc).__name__}: {exc}"}
    state = {"harness": harness, "session_id": sid, "session_source": source, "carrier": carrier,
             "runtime_binding": list(binding) if binding else None, "child_pid": child.pid,
             "sealed_summary": sealed, "sealed_summary_path": str(summary_path),
             "jobs": str(jobs), "sidecar": sidecar}
    state_file.write_text(json.dumps(state))
    print(f"HB-REGISTERED harness={harness} carrier={carrier} session_source={source} "
          f"binding={'yes' if binding else 'no'} sidecar={'yes' if sidecar and 'pid' in sidecar else 'no'}")


def complete(flags=()):
    import dispatch_contract, dispatch_completion_join
    state = json.loads(state_file.read_text())
    pid = int(state["child_pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(100):
        try:
            if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
        except OSError:
            break
        time.sleep(0.05)
    # `complete fail`: the child died, so its receipt owes inspect-done-failure.
    note, failure = (("dead-exit-nonzero", "fail") if "fail" in flags
                     else ("completed-marker", "pass"))
    closed = dispatch_contract.close_attempt_row(jobs, attempt, note,
                                                 evidence={"failure_class": failure})
    path = dispatch_completion_join.materialize_after_terminal_close(jobs, attempt) if closed else None
    print(json.dumps({"closed": closed, "record": str(path) if path else None, **state}))


def steward_due():
    import peer_obligations, steward_check
    harness, sid, source = session()
    store = peer_obligations.ObligationStore()
    duty = store.get(steward_check.duty_id(harness, sid))
    if not duty:
        steward_check.register(harness, sid, store=store)
        duty = store.get(steward_check.duty_id(harness, sid))
    interval = steward_check.interval_seconds()
    observation = dict(duty.get("observation") or {})
    observation["anchor"] = time.time() - interval - 60
    store.update(duty["id"], observation=observation)
    started = peer_obligations.ensure_normal_runner(store.state_root)
    state_file.write_text(json.dumps({"harness": harness, "session_id": sid,
                                      "duty_id": duty["id"], "state_root": str(store.state_root),
                                      "interval": interval}))
    print(f"HB-SCHEDULE-BACKDATED harness={harness} session_source={source} "
          f"interval={interval} runner={'yes' if started else 'no'}")


def probe():
    out = {}
    try:
        out.update(json.loads(state_file.read_text()))
    except (OSError, ValueError):
        pass
    root = jobs.parent
    records = []
    for path in sorted(root.glob("pending-delivery/*/delivery-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        records.append({k: data.get(k) for k in ("delivery_id", "recipient_kind", "state",
                                                  "claim_owner", "acked_by", "route_node")})
    out["records"] = records
    if out.get("duty_id"):
        import peer_obligations
        duty = peer_obligations.ObligationStore().get(out["duty_id"]) or {}
        out["duty"] = {"state": duty.get("state"), "observation": duty.get("observation")}
    couriers = []
    for path in sorted(root.glob("peer-steward/**/run-notice-*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        couriers.append({"state": data.get("state"), "observer_error": data.get("observer_error"),
                         "reason": (data.get("observation") or {}).get("reason")})
    out["notice_couriers"] = couriers
    arms = sorted(root.glob("rewake-arms/*.json*"))
    out["rewake_arms"] = {p.name: p.read_text()[-600:] for p in arms}
    print(json.dumps(out, default=str))


action = sys.argv[1]
if action == "register":
    register(sys.argv[2:])
elif action == "complete":
    complete(sys.argv[2:])
elif action == "steward-due":
    steward_due()
else:
    probe()
'''


def _install_helper(run, attempt: str, event: str = "success") -> Path:
    root = run.home.root
    (root / "hb_fx.json").write_text(json.dumps({"attempt": attempt, "harness": run.harness,
                                                        "event": event}),
                                     encoding="utf-8")
    helper = root / "hb_fx.py"
    helper.write_text(FX_HELPER, encoding="utf-8")
    # The harness scrubs every AGENT_DISPATCH* key from the fixture env; put the
    # fixture's own registry back so every carrier reads the fixture root (and
    # never `<AGENT_HOME>/.dispatch` in the source checkout).
    jobs = Path(run.home.env["HARNESS_STATE_ROOT"]) / "dispatch" / "jobs.log"
    jobs.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    jobs.parent.chmod(0o700)  # ensure_global_registry_writable refuses a group-writable root
    jobs.touch()
    jobs.chmod(0o600)
    run.home.env["AGENT_DISPATCH_JOBS"] = str(jobs)
    run.evidence["jobs"] = str(jobs)
    return helper


def _helper(run, *args: str, timeout: float = 60) -> dict:
    done = subprocess.run([sys.executable, str(run.home.root / "hb_fx.py"), *args],
                          env=run.home.env, cwd=str(run.home.root), text=True,
                          capture_output=True, timeout=timeout)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"exit": done.returncode, "stdout": done.stdout[-1500:], "stderr": done.stderr[-1500:]}


def _shell(run, *args: str) -> str:
    return "python3 " + shlex.quote(str(run.home.root / "hb_fx.py")) + " " + " ".join(args)


def _summary(record: dict) -> dict:
    return {"main_index": record["main_index"], "last_kind": record["last_kind"],
            "last_text": record["last_text"][-500:], "reply": record.get("reply")}


def _hits(text: str, marker) -> int:
    return len(marker.findall(text)) if hasattr(marker, "findall") else text.count(marker)


def _carrying(requests, marker, start: int):
    """Requests from ``start`` on that carry *more* ``marker`` occurrences than
    any earlier request did: new content (a receipt or notice) reached the
    model, not just history that already mentioned it."""
    baseline = max((_hits(r["text"], marker) for r in requests[:start]), default=0)
    return [record for record in requests[start:] if _hits(record["text"], marker) > baseline]


# --------------------------------------------------------------------------
# 1. completion-receipt
# --------------------------------------------------------------------------

class CompletionReceiptScenario(Scenario):
    name = "completion-receipt"
    summary = "A registered child completes while the parent is idle; the parent is woken."
    carrier = "parent_completion"
    PROMPT = "SCENARIO-RECEIPT start the registered child and end your turn"
    PROBE = "SCENARIO-RECEIPT-NEXT anything new?"
    NEXT = "HB-NEXT-ACTION"

    def __init__(self):
        self.attempt = "att-hbfx-" + uuid.uuid4().hex[:20]

    def register_args(self):
        return ("register",)

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            done_next = any(self.NEXT in (r["reply"].get("shell") or "") for r in earlier)
            if kind == "tool":
                if self.NEXT in last:
                    return Reply("Next action finished.")
                return Reply("The child is running; I will continue when its completion arrives.")
            if self.attempt in record["text"] and not done_next:
                return Reply(shell=f"echo {self.NEXT}-$((20+22))")
            if "SCENARIO-RECEIPT start" in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell=_shell(run, *self.register_args()))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        _install_helper(run, self.attempt)

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(8, 120)
        requests = run.main_requests()
        registered = any("HB-REGISTERED" in r["text"] for r in requests)
        run.evidence["register_output"] = next(
            (re.search(r"HB-REGISTERED[^\n\"]*", r["text"]).group(0)
             for r in requests if "HB-REGISTERED" in r["text"]), None)
        if not registered:
            run.note("the register shell call never reached the model")
            return "inconclusive"
        time.sleep(4)  # let any async carrier arm and settle into its wait
        before = len(run.main_requests())
        run.evidence["requests_before_completion"] = before
        completed_at = time.time()
        run.evidence["complete"] = _helper(run, "complete")
        woke = wait_for(lambda: _carrying(run.main_requests(), self.attempt, before), 120, 1)
        run.wait_idle(8, 60)
        requests = run.main_requests()
        run.evidence["probe_after_wait"] = _helper(run, "probe")
        if woke:
            first = woke[0]
            run.evidence["wake_request"] = _summary(first)
            run.evidence["seconds_after_completion"] = round(first["at"] - completed_at, 1)
            ran = any(f"{self.NEXT}-42" in r["text"] for r in requests[first["main_index"]:])
            run.evidence["next_action_ran"] = ran
            run.note(f"wake at main request #{first['main_index']} without a typed prompt"
                     + ("; next action ran" if ran else "; next action did not run"))
            return "pass" if ran else "fail"
        # No wake: record whether the receipt at least rides the next prompt.
        count = len(requests)
        run.prompt(self.PROBE)
        run.wait_idle(8, 60)
        later = _carrying(run.main_requests(), self.attempt, count)
        run.evidence["fallback_next_prompt"] = bool(later)
        if later:
            run.evidence["fallback_request"] = _summary(later[0])
        run.note("no wake without a prompt"
                 + ("; receipt delivered at the next typed prompt" if later
                    else "; receipt not delivered at the next prompt either"))
        return "gap"


# --------------------------------------------------------------------------
# 2. stop-with-remaining-action
# --------------------------------------------------------------------------

class StopWithRemainingActionScenario(CompletionReceiptScenario):
    name = "stop-with-remaining-action"
    summary = "The turn ends while a completion receipt is owed; the turn-end boundary asks again."
    PROMPT = "SCENARIO-RECEIPT start the registered child and end your turn"
    SECOND = "SCENARIO-STOP-TWO is anything left to do"
    PROBE = "SCENARIO-RECEIPT-NEXT anything new?"
    FINAL = "Nothing is left to do."

    def register_args(self):
        # No codex queue sidecar: the in-turn carrier is absent, as after its loss.
        return ("register", "nocarrier")

    def script(self, run):
        base = super().script(run)

        def answer(record, earlier):
            if record["last_kind"] != "tool" and "SCENARIO-STOP-TWO" in record["last_text"] \
                    and not run.evidence.get("complete"):
                # The child finishes while this turn is being answered: the
                # receipt is owed when the model gives its final answer.
                run.evidence["complete"] = _helper(run, "complete")
                return Reply(self.FINAL)
            return base(record, earlier)
        return answer

    def prepare(self, run):
        _install_helper(run, self.attempt)
        # Claude: the launch-time asyncRewake carrier lapses after 10s, so the
        # owed receipt can only come from the Stop path (registry_launch Stop
        # re-takes the existing lapsed claim).  No effect on Codex/OpenCode.
        run.home.env["AGENT_CLAUDE_REWAKE_INTERVAL_SECONDS"] = "1"
        run.home.env["AGENT_CLAUDE_REWAKE_MAX_SECONDS"] = "10"

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(8, 120)
        requests = run.main_requests()
        if not any("HB-REGISTERED" in r["text"] for r in requests):
            run.note("the register shell call never reached the model")
            return "inconclusive"
        run.evidence["register_output"] = next(
            re.search(r"HB-REGISTERED[^\n\"]*", r["text"]).group(0)
            for r in requests if "HB-REGISTERED" in r["text"])
        time.sleep(16)  # past the launch carrier's 10s window
        run.evidence["probe_before_second_turn"] = _helper(run, "probe")
        run.prompt(self.SECOND)
        wait_for(lambda: run.evidence.get("complete"), 60, 0.5)
        final = wait_for(lambda: next((r for r in run.main_requests()
                                       if r["reply"].get("text") == self.FINAL), None), 30, 0.5)
        if not final:
            run.note("second turn never reached the model")
            return "inconclusive"
        start = final["main_index"] + 1
        asked = wait_for(lambda: _carrying(run.main_requests(), self.attempt, start), 90, 1)
        run.wait_idle(8, 45)
        requests = run.main_requests()
        run.evidence["probe_after_wait"] = _helper(run, "probe")
        if asked:
            again = asked[0]
            run.evidence["asked_again_request"] = _summary(again)
            delay = round(again["at"] - final["at"], 1)
            run.evidence["seconds_after_final_answer"] = delay
            ran = any(f"{self.NEXT}-42" in r["text"] for r in requests[again["main_index"]:])
            run.evidence["next_action_ran"] = ran
            run.note(f"asked again {delay}s after the final answer (request #{again['main_index']})"
                     + ("; next action ran" if ran else "; next action did not run"))
            return "pass" if ran else "fail"
        count = len(requests)
        run.prompt(self.PROBE)
        run.wait_idle(8, 60)
        later = _carrying(run.main_requests(), self.attempt, count)
        run.evidence["fallback_next_prompt"] = bool(later)
        if later:
            run.evidence["fallback_request"] = _summary(later[0])
        run.note("turn ended with the receipt owed and nothing asked again"
                 + ("; receipt delivered at the next typed prompt" if later
                    else "; receipt not delivered at the next prompt either"))
        return "gap"


# --------------------------------------------------------------------------
# 3. periodic-check-due
# --------------------------------------------------------------------------

STEWARD_NOTICE = re.compile(r"Periodic steward check is due|Steward check: ")


class PeriodicCheckDueScenario(Scenario):
    name = "periodic-check-due"
    summary = "A due periodic steward check reaches the idle session, which runs it."
    PROMPT = "SCENARIO-PERIODIC take the steward role"
    PROBE = "SCENARIO-PERIODIC-NEXT anything new?"
    CHECK = 'python3 "$AGENT_HOME/utilities/peer-steward.py" steward check'

    def script(self, run):
        def answer(record, earlier):
            last, kind = record["last_text"], record["last_kind"]
            checked = any("steward check" in (r["reply"].get("shell") or "") for r in earlier)
            if kind == "tool":
                if "steward-check role=" in last or "steward-check=" in last:
                    return Reply("Steward check done; no change to act on.")
                return Reply("Steward role taken; ending the turn.")
            if STEWARD_NOTICE.search(record["text"]) and not checked:
                return Reply(shell=self.CHECK)
            if "SCENARIO-PERIODIC take" in last and not any(r["reply"].get("shell") for r in earlier):
                return Reply(shell='python3 "$AGENT_HOME/utilities/peer-steward.py" steward on; '
                                   + _shell(run, "steward-due"))
            return Reply("Nothing new.")
        return answer

    def prepare(self, run):
        _install_helper(run, "att-hbfx-unused")

    def drive(self, run):
        run.prompt(self.PROMPT)
        run.wait_idle(8, 120)
        requests = run.main_requests()
        tool = next((r for r in requests if "HB-SCHEDULE-BACKDATED" in r["text"]), None)
        if tool is None:
            run.note("steward on / schedule shell call never reached the model")
            return "inconclusive"
        run.evidence["steward_on_output"] = re.findall(
            r"steward=[^\n\"]*|HB-SCHEDULE-BACKDATED[^\n\"]*", tool["text"])[-2:]
        # The notice can arrive while the first turn is still settling; count
        # every request after the one that carried the scheduling tool result.
        before = tool["main_index"] + 1
        woke = wait_for(lambda: _carrying(run.main_requests(), STEWARD_NOTICE, before), 150, 1)
        run.wait_idle(8, 45)
        requests = run.main_requests()
        run.evidence["probe_after_wait"] = _helper(run, "probe")
        if woke:
            first = woke[0]
            run.evidence["wake_request"] = _summary(first)
            ran = any("steward-check role=" in r["text"] for r in requests[first["main_index"]:])
            run.evidence["check_ran"] = ran
            run.note(f"due notice woke the session at request #{first['main_index']}"
                     + ("; steward check ran" if ran else "; steward check did not run"))
            return "pass" if ran else "fail"
        count = len(requests)
        run.prompt(self.PROBE)
        run.wait_idle(8, 60)
        later = _carrying(run.main_requests(), STEWARD_NOTICE, count)
        run.evidence["fallback_next_prompt"] = bool(later)
        if later:
            run.evidence["fallback_request"] = _summary(later[0])
        run.evidence["probe_after_prompt"] = _helper(run, "probe")
        run.note("no due-check wake without a prompt"
                 + ("; notice delivered at the next typed prompt" if later
                    else "; notice not delivered at the next prompt either"))
        return "gap"


SCENARIOS = [CompletionReceiptScenario, StopWithRemainingActionScenario, PeriodicCheckDueScenario]
