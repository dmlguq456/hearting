#!/usr/bin/env python3
"""Resume a route paused at a usage limit once its reset time passes.

A `start` that stops because the owner's harness is at a usage limit returns
`waiting-capacity` with a `retry_at`. The parent used to have to remember that
time and run `resume_command` itself; nothing woke it. `arm()` now leaves one
record per route and reset time and starts one detached process that sleeps
until then and runs the same `start` once, from the parent's own environment,
so the resumed owner is the parent's as before.

An owner whose launch did not start (`resume-later`: the admission lock stayed
busy, or the launcher closed the row before spawning) is the same kind of pause:
its receipt's own remedy is to run `resume_command` again after about a minute,
so the same record runs it `LATER_SECONDS` from now.

A worker-class start budget that is still full after the parent's one bounded
wait (`launch-capacity-wait`, `report-capacity-wait`) is a pause too: nothing
was created or failed, and the refusal names when a slot frees (`retry_at`).
The record runs `start` then instead of handing the parent a capacity report.
Inside an automatic resume no parent is waiting, so the first refusal of that
`start` arms the next record the same way.

A BLOCKED owner whose answer another session sent (`answer-awaiting-parent`) is
paused the same way: only the route's parent launches its continuation, so the
parent's own carrier arms a record when it hands that notice over, and the
record runs `start` at once.

An identity refusal can also leave an exact resultless replacement for its
parent. The carrier checks that existing lineage read-only and arms the same
start path at once. Its record is keyed by the exact source edge, so consuming
the same notice later does not create another run.

A usage-limit pause does not always last until its reset: another account may be
signed in, or the quota may come back earlier. Every `RECHECK_SECONDS` of the wait
asks the usage gate as a launch decision would (`usage_states(..., recheck=True)`
for the record's harness), and resumes at once when the gate reports that harness
`ok`. An early resume frees the record's name, so a `start` that pauses again arms
a new one.

Bounded on purpose: one resume per record, a sleep of at most `MAX_SLEEP_SECONDS`,
and at most `MAX_CHAIN` automatic resumes in a row for one route. A usage-limit
pause whose reset time is unknown, or farther than `MAX_SLEEP_SECONDS`, is not
left to a person: it waits until the longest wait (one record per route and
source attempt) and the `start` there looks at the usage gate again -- time
never grants a launch, so a still-limited gate arms the next wait. A known reset
time stays the time to wait for even past the chain limit: the record is re-armed
at that time (or at a floor that doubles per step past the limit when the reset
already passed), still within the longest wait. The same holds for a worker-start
budget pause (`launch-capacity`: nothing was created or failed), whose stable
record name is freed after its one resume so a later pause may arm again.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_SLEEP_SECONDS = 7 * 24 * 3600
MAX_CHAIN = 3
SLACK_SECONDS = 60
POLL_SECONDS = 60
LATER_SECONDS = 60
# Past the chain limit the re-arm floor doubles per step; this many doublings
# already exceed the longest wait, so a larger chain value changes nothing.
MAX_FLOOR_DOUBLINGS = (MAX_SLEEP_SECONDS // LATER_SECONDS).bit_length()
# The usage gate's own probe interval (dispatch_capacity_evidence.PROBE_INTERVAL).
RECHECK_SECONDS = 600
LATER_REASONS = ("owner-launch-not-admitted", "owner-launch-not-started")
LAUNCH_CAPACITY_REASON = "launch-capacity-wait"
ANSWER_REASON = "answer-awaiting-parent"
NO_PROGRESS_REASON = "no-progress"
CHAIN_ENV = "AGENT_CAPACITY_RESUME_CHAIN"


def _epoch(retry_at: str) -> float | None:
    try:
        return datetime.strptime(retry_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _waited_to_cap(record: dict) -> bool:
    """Whether the record's own retry_at is the longest wait, not a known reset.

    A stable-key record is also written for a known reset past the chain limit;
    only the record that actually waited to the cap reports the longest wait."""
    armed = _epoch(record.get("armed_at") or "")
    resume = _epoch(record.get("retry_at") or "")
    if armed is None or resume is None:
        return False
    return resume - armed >= MAX_SLEEP_SECONDS - SLACK_SECONDS


def record_path(jobs: Path, route_id: str, retry_at: str) -> Path:
    key = hashlib.sha256(f"{route_id}\0{retry_at}".encode()).hexdigest()[:16]
    return Path(jobs).resolve().parent / "capacity-resume" / f"{route_id}-{key}.json"


def replacement_record_path(jobs: Path, route_id: str, route_hash: str, source_attempt_id: str) -> Path:
    """One stable resume record for an exact replacement edge, independent of arm time."""
    payload = json.dumps([route_id, route_hash, source_attempt_id],
                         ensure_ascii=False, separators=(",", ":")).encode()
    obligation_key = hashlib.sha256(payload).hexdigest()
    return record_path(jobs, route_id, "replacement:" + obligation_key)


def _pause(result: dict, now, *, automatic: bool = False) -> tuple[str | None, str] | None:
    """`(retry_at, cause)` for a pause this module resumes, else None."""
    if (result.get("state") == "waiting-capacity" and result.get("reason") == "owner-capacity-wait"
            and result.get("required_action") == "resume-after-capacity"):
        return result.get("retry_at"), "capacity"
    if (result.get("state") == "waiting-capacity" and result.get("reason") == LAUNCH_CAPACITY_REASON
            and (result.get("required_action") == "report-capacity-wait"
                 or (automatic and result.get("required_action") == "resume-after-capacity"))):
        return result.get("retry_at"), "launch-capacity"
    if result.get("required_action") == "resume-later" and result.get("reason") in LATER_REASONS:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now() + LATER_SECONDS)), "launch-not-started"
    if result.get("reason") == ANSWER_REASON:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())), "answer"
    return None


def arm(result: dict, route_file, jobs, *, environ=None, spawn=subprocess.Popen, now=time.time) -> dict | None:
    """Arm one automatic resume for a pause `_pause` names; `None` when it does not apply."""
    env = dict(os.environ if environ is None else environ)
    reason = result.get("reason")
    eligibility = None
    if reason in {NO_PROGRESS_REASON, ANSWER_REASON} and result.get("source_attempt_id"):
        try:
            from dispatch_replacement import parent_resume_eligibility
            eligibility = parent_resume_eligibility(
                jobs, result["source_attempt_id"], route_id=result.get("route_id"), environ=env,
                require_parent=True)
        except (OSError, ValueError):
            eligibility = None
    if reason == NO_PROGRESS_REASON:
        if eligibility is None:
            return None
        pause = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())), "replacement")
    elif reason == ANSWER_REASON and result.get("source_attempt_id"):
        if eligibility is None:
            return None
        pause = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now())), "replacement")
    else:
        pause = _pause(result, now, automatic=bool(env.get(CHAIN_ENV)))
    if pause is None:
        return None
    retry_at, cause = pause
    epoch = _epoch(retry_at)
    try:
        chain = int(env.get(CHAIN_ENV) or 0)
    except ValueError:
        chain = MAX_CHAIN
    waits_to_cap = False
    known_reset = epoch is not None and epoch - now() <= MAX_SLEEP_SECONDS
    if chain >= MAX_CHAIN and known_reset and cause in {"capacity", "launch-capacity"}:
        # A known reset time stays the time to wait for even past the chain limit.
        # The floor doubles per step past the limit so an already-passed (stale)
        # reset cannot re-arm in the same second; every step still stays inside
        # the longest wait.
        floor = LATER_SECONDS * 2 ** min(chain - MAX_CHAIN, MAX_FLOOR_DOUBLINGS)
        delay = min(max(epoch - now(), floor), MAX_SLEEP_SECONDS - SLACK_SECONDS)
        epoch = now() + delay
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        waits_to_cap = True
    elif epoch is None or chain >= MAX_CHAIN or epoch - now() > MAX_SLEEP_SECONDS:
        if cause != "capacity":
            return None
        # A usage-limit pause the reset time cannot time (unknown or too far, or a
        # cause this rule does not time past the chain limit) waits to the longest
        # wait; the `start` there re-asks the usage gate.
        epoch = now() + MAX_SLEEP_SECONDS - SLACK_SECONDS
        retry_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        waits_to_cap = True
    if cause == "replacement":
        path = replacement_record_path(Path(jobs), eligibility["route_id"],
                                       eligibility["route_hash"], eligibility["source_attempt_id"])
        route_file = eligibility["route_file"]
    elif waits_to_cap:
        path = record_path(Path(jobs), result.get("route_id") or "",
                           "capacity-wait:" + (result.get("source_attempt_id") or result.get("route_id") or ""))
    else:
        path = record_path(Path(jobs), result.get("route_id") or "", retry_at)
    record = {"schema": "capacity-resume-v1", "route_id": result.get("route_id"),
              "route_file": str(Path(route_file).resolve()), "jobs": str(Path(jobs).resolve()),
              "retry_at": retry_at, "chain": chain + 1, "state": "armed", "cause": cause,
              "harness": result.get("harness") or "", "source_attempt_id": result.get("source_attempt_id") or "",
              "usage_state": result.get("usage_state") or "",
              "armed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
    except FileExistsError:
        return {"record": str(path), "resume_at": retry_at, "state": "already-armed", "cause": cause}
    except OSError:
        return None
    log = path.with_suffix(".log")
    with open(os.devnull, "rb") as stdin, log.open("ab") as out:
        spawn([sys.executable, str(Path(__file__).resolve()), "run", "--record", str(path)],
              stdin=stdin, stdout=out, stderr=subprocess.STDOUT, env={**env, CHAIN_ENV: str(chain + 1)},
              start_new_session=True, close_fds=True)
    return {"record": str(path), "resume_at": retry_at, "state": "armed", "cause": cause}


def _write(path: Path, record: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _cleared(record: dict) -> bool:
    """True once the usage gate, asked as a launch decision, reports the record's harness `ok` for a
    paused usage limit. The allocation usage gate (not a usage limit) and a harness whose reader
    cannot say `ok` keep waiting for the deadline."""
    harness = record.get("harness")
    if (record.get("cause") != "capacity" or not harness
            or record.get("usage_state") == "allocation-usage-gate"):
        return False
    try:
        from dispatch_capacity_evidence import attempt_model, usage_states
        model = attempt_model(record["jobs"], record.get("source_attempt_id") or "")
        if not model:
            # A model-free reader omits family/exact-model holds. Older or
            # incomplete records keep their reset deadline instead of treating
            # that incomplete view as proof the paused worker can run.
            return False
        states = usage_states(record["jobs"], models={harness: model}, recheck=True)
        return states.get(harness) == "ok"
    except Exception:  # noqa: BLE001 -- a failed check keeps the wait for retry_at
        return False


def follow_installed_release(path: Path) -> None:
    """Hand this wait to the installed release before it resumes anything (same PID).

    A wait can last days, and its usage recheck and notice would otherwise run
    the code of the release that armed it. The record is the wait's only
    state, so the installed release's `run` takes the same record from its
    start; the log stream stays. A steady wait only reads the pointer.
    """
    from parent_next_directive import release_moved
    if not release_moved(ROOT):
        return
    try:
        import peer_obligations
        peer_obligations.follow_installed_release(__file__, ["run", "--record", str(path)])
    except Exception:  # noqa: BLE001 -- a failed move keeps waiting with this code
        pass


def _route_finished(record: dict) -> bool:
    """Whether the waited route is closed as proven finished work.

    Only a closure whose terminal gate is proven, with no inline finish
    pending, not closed automatically and not a framed route qualifies;
    anything unreadable does not.
    """
    try:
        route_file = Path(record["route_file"])
        route = json.loads(route_file.read_text(encoding="utf-8"))
        if not isinstance(route, dict) or route.get("route_id") != record.get("route_id"):
            return False
        from dispatch_notice_state import closed_outcome
        import route_plan
        closed = closed_outcome(route_file, route)
    except Exception:  # noqa: BLE001 -- unknown keeps the existing start
        return False
    return bool(isinstance(closed, dict) and closed.get("terminal_gate_proven") is True
                and not closed.get("autoclose") and not closed.get("finish_pending")
                and not route_plan.is_framed_route(route))


def run(path: Path, *, sleep=time.sleep, now=time.time, call=subprocess.run, cleared=_cleared,
        follow=follow_installed_release) -> int:
    record = json.loads(path.read_text(encoding="utf-8"))
    epoch = _epoch(record.get("retry_at"))
    if record.get("state") != "armed" or epoch is None:
        return 0
    slack = 0 if record.get("cause") in {"answer", "replacement"} else SLACK_SECONDS
    deadline = now() + min(MAX_SLEEP_SECONDS, max(0.0, epoch + slack - now()))
    recheck_at = now() + RECHECK_SECONDS
    early = False
    while now() < deadline:
        # Short steps: a record removed (or disarmed) meanwhile ends the wait.
        sleep(min(POLL_SECONDS, max(0.0, deadline - now())))
        if not path.is_file():
            return 0
        follow(path)
        if deadline > now() >= recheck_at:
            recheck_at = now() + RECHECK_SECONDS
            if cleared(record):
                early = True
                break
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("state") != "armed":
        return 0
    capped = (record.get("cause") in {"capacity", "launch-capacity"}
              and path != record_path(Path(record["jobs"]), record.get("route_id") or "", record["retry_at"]))
    if early or capped:
        # A start that pauses again would name this same record; free the name.
        moved = path.with_name(f"{path.stem}.{'early' if early else 'resumed'}-{int(now())}.json")
        try:
            os.replace(path, moved)
            path = moved
        except OSError:
            pass
    # The installed release at resume time, not the one that armed this wait hours
    # earlier (the printed-command rule, `parent_next_directive.entrypoint`).
    from parent_next_directive import entrypoint
    argv = [sys.executable, entrypoint(ROOT, "utilities/capability-route.py"), "start",
            "--route", record["route_file"], "--jobs", record["jobs"]]
    done = call(argv, text=True, capture_output=True, check=False)
    lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
    try:
        receipt = json.loads(lines[-1]) if lines else {}
    except ValueError:
        receipt = {}
    # `start` always runs: on a closed route it still retries settlement and
    # shared-spec publication and reports what is left. Only a plain
    # `completed` of a route that finished as proven work while this wait
    # slept has nothing to tell its session.
    quiet = (done.returncode == 0 and receipt.get("state") == "completed"
             and not receipt.get("next_leg") and _route_finished(record))
    record.update(state="resumed", resumed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  exit_code=done.returncode, result_state=receipt.get("state"),
                  owner_attempt_id=receipt.get("owner_attempt_id"), **({"early": True} if early else {}),
                  **({"notice": "skipped-route-completed"} if quiet else {}))
    _write(path, record)
    if not quiet:
        _notify(record, receipt, capped=capped and not early)
    return 0


def _notify(record: dict, receipt: dict, capped: bool = False) -> None:
    """One notice to the session that owns the route; a resume that paused again says so, and
    one whose `start` failed or left no receipt carries the command to run it again."""
    try:
        from parent_next_directive import resume_command
        from session_identity import identity
        from session_notice import notify
        caller = identity()
        if not caller.known or not caller.session_id:
            return
        state = receipt.get("state") or "unknown"
        again = state == "waiting-capacity"
        failed = not again and (record.get("exit_code") not in (None, 0) or not receipt.get("state"))
        why = {"launch-not-started": "started again after a launch that did not start",
               "launch-capacity": "started again after the worker start budget freed",
               "answer": "continued with the answer another session sent"}.get(record.get("cause"))
        if record.get("cause") == "replacement":
            why = ("the parent took over the remaining replacement work" if state in {"running", "completed"}
                   else "the parent's replacement start paused again" if again
                   else "the parent's replacement start needs attention" if state == "needs-attention"
                   else "the parent could not start the remaining replacement work" if failed
                   else "the parent's replacement start returned no settled result")
        elif why is None:
            why = ("resumed before its recorded reset because the usage gate cleared it"
                   if record.get("early") else
                   "re-checked capacity at the end of its longest wait" if capped and _waited_to_cap(record)
                   else "resumed after the usage limit reset")
        text = (f"route {record['route_id']} {why}: state {state}"
                + (f", owner {receipt['owner_attempt_id']}" if receipt.get("owner_attempt_id") else "")
                + (f", reason {receipt['reason']}" if state == "needs-attention" and receipt.get("reason") else "")
                + (f", a question is waiting for the person: required action {receipt['required_action']}"
                   if state == "needs-interview" and receipt.get("required_action") else "")
                + (f"; paused again until {receipt.get('retry_at', 'an unknown time')}" if again else "")
                + (f"; start exited {record.get('exit_code')}, run it again: "
                   f"{resume_command(record['route_file'], record['jobs'], agent_home=ROOT)}" if failed else ""))
        action = "report-pause" if again else "run-resume-command" if failed else "report-to-user"
        notify(caller.harness, caller.session_id, key=f"capacity-resume:{record['route_id']}:{record['retry_at']}",
               subject="capacity resume", text=text, route_id=record["route_id"], jobs=record["jobs"],
               required_action=action)
    except Exception:  # noqa: BLE001 -- the resume itself already happened and is recorded
        return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="sleep until the record's retry_at, then start the route once")
    runner.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    return run(args.record)


if __name__ == "__main__":
    raise SystemExit(main())
