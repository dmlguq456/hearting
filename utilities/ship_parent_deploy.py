"""Execute one approved ship command in its actual interactive parent.

The command is an explicit host action, never an owner permission expansion.
Ordinary owner input/continuation and completion remain the route's lifecycle.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

import dispatch_contract as DC
from dispatch_completion_join import exact_attempt_row, JoinContractError
from dispatch_lifecycle import pid_namespace_evidence
from dispatch_replacement import _route
import route_authority as authority
try:  # ends a detached child with the test that started it; no-op outside tests
    from detached_lifetime import bind as end_with_test
except ImportError:  # a partial tree without it (e.g. a fixture release): no record
    def end_with_test(child):
        return child

SCHEMA = "ship-parent-deploy/v1"


def owner_input(jobs, attempt):
    from dispatch_owner_input import inspect, InputError
    try:
        return inspect(Path(jobs), attempt)
    except (InputError, JoinContractError, OSError, ValueError):
        return {}


def parked_request(jobs, attempt, request_digest):
    from dispatch_completion_join import read_supervisor_phase_state
    path = DC.dispatch_state_root(Path(jobs)) / "supervisor-state" / (attempt + ".json")
    state = read_supervisor_phase_state(path, attempt)
    return bool(state and state.phase == "parked" and
                (state.resource or {}).get("parent_deploy") == request_digest)


def waiting_request(args, control):
    """An exact open request held by the current supervised owner, including replacements."""
    route_file = getattr(args, "route_file", "")
    if not route_file:
        return None
    jobs = Path(args.jobs)
    attempt = args.parent_attempt_id
    request_path, result_path = paths(jobs, args.route_id)
    if not request_path.exists():
        return None
    value, _ = load_request(request_path)
    cr = DC._route_module()
    if cr._current_owner_attempt(jobs, value["owner_attempt_id"]) != attempt:
        return None
    row = exact_attempt_row(jobs, attempt)
    actual_file, route = _route(jobs, attempt, row.metadata)
    if (row.status not in {"open", "running"}
            or row.metadata.get("worker_type") != "owner"
            or row.metadata.get("registered_worker") != "1"
            or (value["route_id"], value["route_hash"], value["route_file"]) !=
               (args.route_id, args.route_hash, str(Path(route_file).resolve()))
            or (route["route_id"], route["route_hash"], str(actual_file.resolve())) !=
               (value["route_id"], value["route_hash"], value["route_file"])):
        raise ValueError("parent-deploy-owner-binding-changed")
    channel = owner_input(jobs, attempt)
    if not (channel.get("accepting") and channel.get("supervisor_live")
            and channel.get("thread_id") == control.thread_id):
        raise ValueError("parent-deploy-owner-input-unavailable")
    # Keep a parked request through result publication: the result is durable
    # before its correction is submitted, so that small window is still a wait.
    if (result_path.exists() and read(result_path).get("state") != "running"
            and not parked_request(jobs, attempt, value["request_digest"])):
        return None
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("parent-deploy-object-required")
    return value


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def paths(jobs, route_id):
    if not isinstance(route_id, str) or not route_id.startswith("rt-") or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in route_id):
        raise ValueError("parent-deploy-route-id-invalid")
    directory = Path(jobs).resolve().parent / "ship-parent-deploy"
    return directory / (route_id + ".json"), directory / (route_id + ".result.json")


@contextmanager
def locked(request):
    lock = Path(str(request) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("parent-deploy-busy") from exc
        yield


def context(jobs, attempt, *, owner_prepare=False):
    cr = DC._route_module()
    current = cr._current_owner_attempt(jobs, attempt)
    row = exact_attempt_row(Path(jobs), current)
    meta = row.metadata
    if (meta.get("worker_type") != "owner" or meta.get("registered_worker") != "1"
            or meta.get("dispatch_depth") != "1"):
        raise ValueError("parent-deploy-registered-owner-required")
    route_file, route = _route(Path(jobs), current, meta)
    nodes = [n for n in route["nodes"] if n.get("completion_gate") == "ship-deploy"]
    if (route.get("capability") != "autopilot-ship" or len(nodes) != 1
            or route.get("entry_execution_scope") == "report"
            or not cr.valid_entry_execution_scope_marker(route.get("entry_scope_contract_version"),
                                                         route.get("entry_execution_scope"))):
        raise ValueError("parent-deploy-approved-ship-scope-required")
    node = nodes[0]
    facts = {} if owner_prepare else {"actor": authority.caller_identity()[1]}
    channel = owner_input(jobs, current)
    if channel.get("supervisor_live") and channel.get("accepting"):
        facts["reachable"] = True
    decision = authority.decide_continuation(route, action="owner", jobs=Path(jobs),
                                             owner_attempt_id=attempt, facts=facts)
    if not ((decision.allowed and decision.next_action == "handback")
            or (decision.state == "answer" and decision.next_action == "correct")):
        # Preparing/executing the blocked owner's answer is not an owner start.
        # All other states keep the common decision's existing continuation.
        authority.require_continuation(decision._replace(
            allowed=False, reason=decision.reason or "parent-deploy-owner-not-ready"))
    DC.owner_operation_fence(route, node, jobs=Path(jobs))
    DC.human_gate_entry_fence(route, node, jobs=Path(jobs))
    return current, meta, route_file, route, node


def parent(meta, jobs):
    harness, sid = authority.caller_identity()
    authority.require_owner(meta, sid, Path(jobs), "parent-deploy-actual-parent-required")
    if sid == meta.get("parent_sid") and meta.get("parent_harness") not in (None, "", harness):
        raise ValueError("parent-deploy-parent-harness-mismatch")
    if (os.environ.get("AGENT_DISPATCH_CHILD") == "1"
            or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
            or os.environ.get("AGENT_DISPATCH_DEPTH", "0") not in ("", "0")):
        raise ValueError("parent-deploy-dispatch-child-forbidden")
    host = pid_namespace_evidence()
    if host["lifecycle_selector_source"] != "host-like":
        raise ValueError("parent-deploy-host-unverified")
    return {"harness": harness, "session_id": sid, "namespace": host,
            "enforcement": "explicit-parent-host-action"}


def command(value):
    if set(value) - {"argv", "cwd", "timeout_seconds", "inputs"}:
        raise ValueError("parent-deploy-command-unknown-field")
    argv = value.get("argv")
    if not isinstance(argv, list) or not argv or any(
            not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("parent-deploy-argv-invalid")
    executable = Path(argv[0])
    cwd = Path(value.get("cwd", ""))
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("parent-deploy-absolute-executable-required")
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("parent-deploy-absolute-cwd-required")
    timeout = value.get("timeout_seconds", 300)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 86400):
        raise ValueError("parent-deploy-finite-timeout-required")
    inputs = value.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(p, str) or "\0" in p for p in inputs):
        raise ValueError("parent-deploy-inputs-invalid")
    files = {}
    for name in argv + inputs:
        path = Path(name) if Path(name).is_absolute() else cwd / name
        if path.is_file():
            files[str(path.absolute())] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif name in inputs:
            raise ValueError("parent-deploy-input-unreadable")
    return {"argv": argv, "cwd": str(cwd.resolve()), "timeout_seconds": timeout, "inputs": files}


def listed_inputs(cmd):
    """Hashed files other than those argv names: what the command reads, not the command."""
    named = {str((Path(a) if Path(a).is_absolute() else Path(cmd["cwd"]) / a).absolute()) for a in cmd["argv"]}
    return {path: sha for path, sha in cmd["inputs"].items() if path not in named}


def unchanged_inputs_since(previous, value):
    """The earliest preparation that observed the same listed inputs, for a command-only change."""
    if previous is None or any(previous.get(k) != v for k, v in value.items() if k != "command"):
        return None
    listed = listed_inputs(value["command"])
    if not listed or listed_inputs(previous["command"]) != listed:
        return None  # No observation, or a changed input: every review starts again.
    return previous.get("inputs_unchanged_since") or {
        "prepared_at": previous["prepared_at"], "prior_review_attempts": previous["prior_review_attempts"]}


def record_path(route):
    from artifact_producer import route_cycle_for, cycle_dir, require_cycle_output, ProducerError
    root = Path(route["artifact_root"])
    try:
        cycle = route_cycle_for(root, route)
        if not cycle:
            raise ValueError("parent-deploy-cycle-required")
        target = cycle_dir(root, cycle["campaign_id"], cycle["cycle_id"], cycle) / "artifacts/release-config/deploy-record.json"
        require_cycle_output(root, target, route_id=route["route_id"])
    except ProducerError as exc:
        raise ValueError(str(exc)) from exc
    return target


def prepare(jobs, attempt, command_file):
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    current, meta, route_file, route, node = context(jobs, attempt, owner_prepare=bool(caller))
    availability = None
    if caller:
        if caller != current:
            raise ValueError("parent-deploy-current-owner-required")
        authority.review_owner_authority(route, jobs, caller)
    else:
        availability = parent(meta, jobs)
    cmd = command(read(command_file))
    request_path, result_path = paths(jobs, route["route_id"])
    with locked(request_path), DC._route_module()._exclusive_lock(Path(str(jobs) + ".lock")):
        if result_path.exists():
            raise ValueError("parent-deploy-execution-already-recorded")
        value = {"schema": SCHEMA, "jobs": str(Path(jobs).resolve()), "route_id": route["route_id"],
                 "route_hash": route["route_hash"], "route_file": str(route_file.resolve()), "owner_attempt_id": attempt,
                 "parent_session_id": meta["parent_sid"], "command": cmd,
                 "deploy_record": str(record_path(route)), "node_id": node["id"]}
        previous = None
        if request_path.exists():
            previous, _ = load_request(request_path)
            # An ordinary owner replacement keeps exactly this request.
            value["owner_attempt_id"] = previous["owner_attempt_id"]
            value["parent_session_id"] = previous["parent_session_id"]
            if availability is None:
                availability = previous.get("parent_availability")
            unchanged = all(previous.get(k) == v for k, v in value.items())
        else:
            unchanged = False
        if unchanged:
            value["prepared_at"] = previous["prepared_at"]
            value["prior_review_attempts"] = previous["prior_review_attempts"]
            if "inputs_unchanged_since" in previous:
                value["inputs_unchanged_since"] = previous["inputs_unchanged_since"]
        else:
            # The existing registry lock orders request publication before new
            # review registration. A changed request cannot consume a review
            # that was already registered, even within the same timestamp second.
            since = unchanged_inputs_since(previous, value)
            value["prepared_at"] = datetime.now(timezone.utc).isoformat()
            value["prior_review_attempts"] = review_attempts(route, jobs)
            if since is not None:
                value["inputs_unchanged_since"] = since
        value["request_digest"] = digest(value)
        value["parent_availability"] = availability
        write(request_path, value)
    run_command = shlex.join([sys.executable, str(Path(__file__).resolve().with_name("ship-parent-deploy.py")),
                             "run", "--request", str(request_path)])
    from session_notice import notify
    try:
        notice = notify(meta.get("parent_harness"), meta.get("parent_sid"), jobs=jobs,
               key="parent-deploy-" + value["request_digest"], subject="ship deploy ready",
               text=f"route {route['route_id']}: {run_command} --check, then {run_command}",
               required_action="run-parent-deploy", route_id=route["route_id"],
               lineage={"kind": "attempt-parent", "harness": meta["parent_harness"],
                        "session_id": meta["parent_sid"], "attempt_id": current})
    except (OSError, ValueError):
        notice = None
    channel = owner_input(jobs, current)
    runtime_wait = bool(notice and channel.get("supervisor_live") and channel.get("accepting"))
    return {"state": "prepared" if notice else "blocked", "request": str(request_path), **value,
            "run_command": run_command, "runtime_wait": runtime_wait,
            **({"reason": "parent-deploy-notice-unavailable"} if not notice else {}),
            "next_step": ("Finish readiness reviews, then yield runtime_wait: registered-children."
                          if runtime_wait else
                          "Use the ordinary BLOCKED handoff with this request and run_command; "
                          "the actual parent checks and executes it, then uses the printed correction_command.")}


def load_request(path):
    value = read(path)
    expected, result = paths(value["jobs"], value["route_id"])
    if Path(path).resolve() != expected or value.get("schema") != SCHEMA:
        raise ValueError("parent-deploy-request-location-invalid")
    bound = {k: v for k, v in value.items() if k not in {"request_digest", "parent_availability"}}
    if digest(bound) != value.get("request_digest"):
        raise ValueError("parent-deploy-request-drift")
    return value, result


def review_attempts(route, jobs):
    result = []
    review_ids = {n["id"] for n in route["nodes"] if n.get("kind") == "review-worker"}
    for line in Path(jobs).read_text().splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = DC.parse_registry_metadata(fields[5])
        if (meta.get("route_id") == route["route_id"] and meta.get("route_node") in review_ids
                and meta.get("attempt_id")):
            result.append(meta["attempt_id"])
    return sorted(set(result))


def command_reviewers(route, node, review_ids):
    """Reviews that judge the deploy command.

    These hand their verdict to deploy, hold the same gate (a plan's readiness
    stage) or are their realized legs. If deploy names no such verdict, every
    review judges the command.
    """
    nodes = {n["id"]: n for n in route["nodes"]}
    wanted = node.get("inputs") or []
    direct = {i for i in review_ids if any(fnmatch.fnmatchcase(out, name) or fnmatch.fnmatchcase(name, out)
                                           for out in nodes[i].get("outputs") or [] for name in wanted)}
    if not direct:
        return set(review_ids)
    gates = {nodes[i].get("completion_gate") for i in direct}
    result = direct | {i for i in review_ids if nodes[i].get("completion_gate") in gates}
    for group in route.get("parallel_groups", []):
        if result & set(group.get("members", [])):
            result |= set(group["members"])
    return result | {i for i in review_ids if nodes[i].get("parallel_anchor") in result}


def registered_after(began, attempt_id, preparation):
    prepared = datetime.fromisoformat(preparation["prepared_at"])
    return (began.tzinfo is not None and attempt_id not in preparation["prior_review_attempts"]
            and began >= prepared.replace(microsecond=0))


def reviews(route, node, jobs, request=None):
    cr = DC._route_module()
    missing = cr.owner_terminal_prerequisites(route, node, Path(jobs))
    if missing:
        raise ValueError("parent-deploy-reviews-pending:" + json.dumps(missing, sort_keys=True))
    proof = {}
    pending = list(node.get("depends_on", []))
    nodes = {n["id"]: n for n in route["nodes"]}
    seen = set()
    review_ids = []
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        n = nodes[node_id]
        pending.extend(n.get("depends_on", []))
        for group in route.get("parallel_groups", []):
            if n["id"] in group.get("members", []):
                pending.extend(group["members"])
        if n.get("kind") == "review-worker":
            review_ids.append(node_id)
    judges_command = command_reviewers(route, node, review_ids)
    for node_id in review_ids:
        n = nodes[node_id]
        row = cr._marker_identity_row(route, n, n["id"], n.get("completion_gate"),
                                      jobs=Path(jobs), exact_terminal=True)
        if not row.get("passed") or row.get("review_independence") == "owner-overridden":
            raise ValueError("parent-deploy-independent-review-required:" + n["id"])
        marker = read(cr.completion_dir(route["route_id"], jobs=Path(jobs)) / (n["id"] + ".json"))
        reviewer = exact_attempt_row(Path(jobs), row.get("attempt_id", ""))
        if (reviewer.metadata.get("worker_type") == "owner"
                or marker.get("review_independence") != "independent"):
            raise ValueError("parent-deploy-independent-review-required:" + n["id"])
        if request is not None:
            began = datetime.fromisoformat(reviewer.raw.split("\t")[0].replace("Z", "+00:00"))
            if not registered_after(began, row["attempt_id"], request):
                # Only a review that does not judge the command keeps its PASS,
                # and only while the inputs observed before it are unchanged.
                since = request.get("inputs_unchanged_since")
                if (n["id"] in judges_command or since is None
                        or not registered_after(began, row["attempt_id"], since)):
                    raise ValueError("parent-deploy-review-precedes-request:" + n["id"])
                row = {**row, "inputs_unchanged_since": since["prepared_at"]}
        proof[n["id"]] = row
    for key, row in cr.terminal_gate_observation(route, jobs=Path(jobs), exact_terminal=True).items():
        if key.startswith("parallel_group:"):
            if not row.get("passed"):
                raise ValueError("parent-deploy-arbitration-pending:" + key)
            proof[key] = row
    return proof


def checked(value):
    current, meta, route_file, route, node = context(value["jobs"], value["owner_attempt_id"])
    if (value["route_id"], value["route_hash"], value["route_file"], value["node_id"], value["deploy_record"]) != (
            route["route_id"], route["route_hash"], str(route_file.resolve()), node["id"], str(record_path(route))):
        raise ValueError("parent-deploy-route-binding-drift")
    executor = parent(meta, value["jobs"])
    for path, sha in value["command"]["inputs"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
            raise ValueError("parent-deploy-command-input-drift:" + path)
    proof = reviews(route, node, value["jobs"], value)
    return current, executor, proof


def handback(value, result_path):
    message = result_path.with_suffix(".message.md")
    installed = Path(__file__).resolve().with_name("capability-route.py")
    complete = shlex.join([sys.executable, str(installed), "complete", "--inline", "--route", value["route_file"],
                          "--node", value["node_id"], "--evidence", value["deploy_record"]])
    write_text = (f"Same approved ship route {value['route_id']}; parent deploy result: {result_path}.\n"
                  f"Read the full result and existing deploy record {value['deploy_record']}.\n"
                  "Do not rerun deployment. A non-success result stays BLOCKED; assess the project's rollback.\n"
                  f"If success, complete deploy using the installed common receipt check: {complete}\n"
                  "Then continue the existing post-deploy verification with unchanged independent review/attempt ceilings.\n")
    message.write_text(write_text)
    return shlex.join([sys.executable, str(installed), "correct", "--jobs", value["jobs"],
                       "--attempt-id", value["owner_attempt_id"], "--message-file", str(message),
                       "--request-id", "parent-deploy-" + value["request_digest"]])


def publish(value, result, result_path):
    target = Path(value["deploy_record"])
    existing = read(target) if target.exists() else {}
    prior = existing.get("parent_deploy")
    if prior is not None and prior != result:
        raise ValueError("parent-deploy-record-conflict")
    existing["parent_deploy"] = result
    write(target, existing)
    correction = handback(value, result_path)
    reply = {"state": result["state"], "result": str(result_path), "deploy_record": str(target),
             "correction_command": correction}
    current = DC._route_module()._current_owner_attempt(Path(value["jobs"]), value["owner_attempt_id"])
    channel = owner_input(value["jobs"], current)
    if (parked_request(value["jobs"], current, value["request_digest"])
            and channel.get("supervisor_live") and channel.get("accepting")):
        # The same idempotent correct command follows replacement lineage. A
        # failed delivery retains the command; it never retries deployment.
        sent = subprocess.run(shlex.split(correction), text=True, capture_output=True)
        reply["owner_resume"] = {"submitted": sent.returncode == 0, "exit_code": sent.returncode}
    return reply


def run(request_path, *, check=False):
    value, result_path = load_request(request_path)
    if check:
        current, executor, proof = checked(value)
        return {"state": "checked", "executed": False, "request": str(request_path),
                "owner_attempt_id": current, "executor": executor, "reviews": proof,
                "execution_state": read(result_path)["state"] if result_path.exists() else "not-run"}
    with locked(request_path):
        value, result_path = load_request(request_path)
        # Return a retained outcome after a lost reply; never execute a second time.
        if result_path.exists():
            _, meta, _, route, node = context(value["jobs"], value["owner_attempt_id"])
            parent(meta, value["jobs"])
            result = read(result_path)
            if result.get("request_digest") != value["request_digest"]:
                raise ValueError("parent-deploy-result-binding-drift")
            if result["state"] == "running":
                return {"state": "running-or-interrupted", "result": str(result_path), "executed": False,
                        "next_step": "Inspect retained PID/start and project effects; do not rerun deployment."}
            return publish(value, result, result_path)
        current, executor, proof = checked(value)
        # Refuse an unreadable existing record before the irreversible command.
        target = Path(value["deploy_record"])
        if target.exists():
            existing = read(target)
            if existing.get("parent_deploy") is not None:
                raise ValueError("parent-deploy-record-conflict")
        result = {"schema": SCHEMA, "state": "running", "request_digest": value["request_digest"],
                  "route_id": value["route_id"], "route_hash": value["route_hash"],
                  "owner_attempt_id": current, "executor": executor, "reviews": proof,
                  "started_at": datetime.now(timezone.utc).isoformat()}
        write(result_path, result)  # durable intent BEFORE spawn; a crash cannot cause an automatic retry
        log = result_path.with_suffix(".output.log")
        proc = None
        start = None
        old_handlers = {}
        received_signal = []

        def interrupted(signum, frame):
            # Do not raise between Popen's fork and publishing its owned PID.
            received_signal.append(signum)

        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                old_handlers[sig] = signal.signal(sig, interrupted)
            with log.open("xb") as output:
                os.chmod(log, 0o600)
                cmd = value["command"]
                if received_signal:
                    raise InterruptedError("parent-deploy-interrupted")
                proc = subprocess.Popen(cmd["argv"], cwd=cmd["cwd"], stdout=output,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
                end_with_test(proc)
                start = DC.process_start_ticks(proc.pid)
                result.update(pid=proc.pid, pid_start=start, pgid=proc.pid)
                write(result_path, result)
                deadline = time.monotonic() + cmd["timeout_seconds"]
                while True:
                    if received_signal:
                        raise InterruptedError("parent-deploy-interrupted")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd["argv"], cmd["timeout_seconds"])
                    try:
                        exit_code = proc.wait(timeout=min(0.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                group = DC.process_group_observation(proc.pid)
                result.update(exit_code=exit_code, state="success" if exit_code == 0 and group.state == "empty" else "failed")
                if group.state != "empty":
                    result["detail"] = "process-group-not-quiescent; inspect retained identity"
        except (OSError, subprocess.TimeoutExpired, InterruptedError) as exc:
            result.update(state="interrupted" if isinstance(exc, InterruptedError) else "failed",
                          detail=type(exc).__name__)
            if proc is not None and start:
                result["teardown"] = DC.signal_exact_process_group(proc.pid, start, signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    result["teardown"] = "not-quiescent"
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        result.update(finished_at=datetime.now(timezone.utc).isoformat(), output_log=str(log),
                      output_sha256=hashlib.sha256(log.read_bytes()).hexdigest() if log.exists() else None)
        write(result_path, result)
        return publish(value, result, result_path)


def validate_completion(route, jobs, evidence):
    """Normal deploys are unchanged; a prepared parent deploy requires its real result."""
    request_path, result_path = paths(jobs, route["route_id"])
    if not request_path.exists():
        return
    value, _ = load_request(request_path)
    result = read(result_path)
    if (value["route_id"], value["route_hash"], str(Path(evidence).resolve())) != (
            route["route_id"], route["route_hash"], value["deploy_record"]):
        raise ValueError("parent-deploy-completion-binding-mismatch")
    if (result.get("state") != "success" or result.get("exit_code") != 0
            or result.get("request_digest") != value["request_digest"]
            or (result.get("route_id"), result.get("route_hash")) != (route["route_id"], route["route_hash"])
            or read(evidence).get("parent_deploy") != result):
        raise ValueError("parent-deploy-successful-result-required")
    executor = result.get("executor", {})
    owner = exact_attempt_row(Path(jobs), result.get("owner_attempt_id", "")).metadata
    if (executor.get("enforcement") != "explicit-parent-host-action"
            or executor.get("namespace", {}).get("lifecycle_selector_source") != "host-like"):
        raise ValueError("parent-deploy-executor-unverified")
    authority.require_owner(owner, executor.get("session_id"), Path(jobs), "parent-deploy-executor-unverified")
    node = next(n for n in route["nodes"] if n["id"] == value["node_id"])
    if result.get("reviews") != reviews(route, node, jobs, value):
        raise ValueError("parent-deploy-review-evidence-drift")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare", allow_abbrev=False)
    prep.add_argument("--attempt-id", default=os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"))
    prep.add_argument("--jobs", type=Path)
    prep.add_argument("--command-file", required=True, type=Path)
    execute = sub.add_parser("run", allow_abbrev=False)
    execute.add_argument("--request", required=True, type=Path)
    execute.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            if not args.attempt_id:
                raise ValueError("parent-deploy-owner-attempt-required")
            jobs = args.jobs or DC._route_module()._compose_default_jobs()
            result = prepare(Path(jobs), args.attempt_id, args.command_file)
        else:
            result = run(args.request, check=args.check)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] in {"prepared", "checked", "success"} else 3
    except (ValueError, OSError, KeyError, TypeError, DC.DispatchContractError, JoinContractError) as exc:
        failure = {"state": "not-admitted", "reason": getattr(exc, "reason", str(exc))}
        failure.update({key: getattr(exc, key) for key in ("next_action", "retry_when", "detail")
                        if getattr(exc, key, None)})
        print(json.dumps(failure, ensure_ascii=False))
        return 69


if __name__ == "__main__":
    raise SystemExit(main())
