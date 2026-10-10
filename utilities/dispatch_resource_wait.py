"""Resource leg of the existing owner controller, outside any model turn.

The workflow watch records evidence; it does not launch model successors for
this leg. Delivery lives in the existing private supervisor phase/outbox file.
"""
from __future__ import annotations
import contextlib
import importlib.util
import io
import os
from functools import lru_cache
import json
from pathlib import Path
from types import SimpleNamespace
import time

import dispatch_completion_join as JOIN
import resource_resume as RESUME


@lru_cache(maxsize=1)
def supervisor():
    spec = importlib.util.spec_from_file_location("owner_resource_supervisor", Path(__file__).with_name("workflow-supervisor.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_binding(route, route_file, args, environ):
    """Use the normal registered owner identity, never labels or a guessed SID."""
    import route_parent_close
    if route_parent_close.intent(route, args.jobs or environ.get("AGENT_DISPATCH_JOBS")):
        raise ValueError("cancelled-by-parent")
    import owner_route_binding as OWNER
    from dispatch_contract import resolve_live_parent_attempt
    from dispatch_owner_input import inspect, PLACEHOLDER_THREAD
    if environ.get("AGENT_DISPATCH_COMPLETION_MODE") != "supervised":
        return None
    attempt = OWNER._registered_owner_attempt(environ)
    if not attempt:
        return None
    if args.parent_attempt_id and args.parent_attempt_id != attempt:
        raise ValueError("resource-owner-attempt-conflict")
    jobs = Path(args.jobs or environ.get("AGENT_DISPATCH_JOBS", ""))
    if not jobs.is_absolute() or not jobs.is_file():
        raise ValueError("resource-owner-jobs-unavailable")
    jobs = jobs.resolve()
    if environ.get("AGENT_DISPATCH_JOBS") and Path(environ["AGENT_DISPATCH_JOBS"]).resolve() != jobs:
        raise ValueError("resource-owner-jobs-conflict")
    binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=attempt)
    if (binding is None or binding.route_file != str(route_file)
            or binding.route_id != route["route_id"] or binding.route_hash != route["route_hash"]):
        raise ValueError("resource-owner-route-conflict")
    row = JOIN.exact_attempt_row(jobs, attempt)
    fields = row.raw.split("\t")
    parent = resolve_live_parent_attempt(jobs, parent_slug=row.slug, repo=fields[2],
        worktree=fields[3], expected_attempt_id=attempt)
    current = inspect(jobs, attempt)
    session = current["thread_id"]
    if not current["supervisor_live"] or not session or session == PLACEHOLDER_THREAD:
        raise ValueError("resource-owner-session-unavailable")
    from dispatch_contract import dispatch_state_root
    state_path = dispatch_state_root(jobs) / "supervisor-state" / (attempt + ".json")
    if Path(environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE", "")).resolve() != state_path.resolve():
        raise ValueError("resource-owner-state-conflict")
    with JOIN._supervisor_state_lock(state_path):
        state = JOIN.read_supervisor_phase_state(state_path, attempt)
        if state is None:
            raise ValueError("resource-owner-state-unavailable")
        resource = state.resource or {"session_id": session, "delivered": [], "outbox": None}
        if resource["session_id"] != session:
            raise ValueError("resource-owner-session-conflict")
        JOIN._write_supervisor_state_unlocked(state_path, attempt, set(state.delivered_attempt_ids),
            phase=state.phase, outbox=state.outbox, resource=resource)
    args.jobs, args.parent_attempt_id = str(jobs), attempt
    return {"parent_attempt_id": attempt, "session_id": session, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "jobs": str(jobs),
        "owner_pid": parent.pid, "owner_start": parent.pid_start,
        **({"launch_scope": "codex-owner-controller"} if row.metadata.get("harness") == "codex" else {})}


def resource_body_digest(row):
    keys = ("run_id", "cwd", "log", "command", "route", "node", "parent_attempt_id", "jobs",
            "config_ref", "config_sha256", "source_commit", "source_dirty", "source_git_state", "config_layout",
            "resource_policy", "owner_wait")
    body = {key: row.get(key) for key in keys}
    if "launch_request" in row:
        body["launch_request"] = row["launch_request"]
    return RESUME.row_digest(body)


def resource_execution_finished(row):
    from route_authority import resource_predecessor_finished
    return resource_predecessor_finished(row)


def resource_execution_succeeded(row):
    """A past status word cannot substitute for the exact exit and sentinel."""
    return (resource_execution_finished(row)
            and supervisor().runner().read_sentinel(row.get("sentinel")) == 0)


def resource_evidence_paths_conflict(left, right):
    """Protect prior evidence across path roles, symlinks, and hard links."""
    def paths(row):
        values = [row.get(key) for key in ("log", "sentinel", "progress_file") if row.get(key)]
        return [Path(path).resolve(strict=False) for value in values
                for path in (value, value + ".partial")]
    a, b = paths(left), paths(right)
    if set(a) & set(b):
        return True
    def inodes(values):
        result = set()
        for path in values:
            try:
                stat = path.stat()
                result.add((stat.st_dev, stat.st_ino))
            except FileNotFoundError:
                pass
        return result
    return bool(inodes(a) & inodes(b))


def controller_intent(row):
    return ((row.get("owner_wait") or {}).get("launch_scope") == "codex-owner-controller"
            and row.get("launch_state") in {"queued", "claimed"}
            and row.get("resource_policy") == "supervised-owner"
            and isinstance(row.get("launch_request"), dict))


def resource_key(row):
    keys = ("run_id", "pid", "starttime", "command", "route", "node", "jobs", "owner_wait", "command_hash", "launch_argv", "process_group")
    return RESUME.row_digest({key: row.get(key) for key in keys})


def context(args, control):
    """Read only exact armed records under the current owner route and registry."""
    import owner_route_binding as OWNER
    from resource_run_registry import resource_never_started
    route_file = getattr(args, "route_file", "")
    if not route_file or not Path(route_file).is_file():
        return None
    jobs = Path(args.jobs).resolve(strict=True)
    sup = supervisor()
    route = sup.load_route(route_file)
    ledger = sup.ledger_for(route, str(jobs))
    armed_rows = sup.read_armed(ledger)
    if not any(a.get("predecessor_kind") == "resource" for a in armed_rows.values()):
        return None
    binding = None
    result = []
    for node, armed in armed_rows.items():
        if armed.get("predecessor_kind") != "resource":
            continue
        data = json.loads(Path(armed["resource_registry"]).read_text())
        row = data.get("runs", {}).get(armed["predecessor_id"])
        if not isinstance(row, dict) or row.get("resource_policy") != "supervised-owner":
            continue
        owner = row.get("owner_wait") or {}
        if owner.get("parent_attempt_id") != args.parent_attempt_id:
            continue
        if binding is None:
            binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=args.parent_attempt_id)
            if (binding is None or binding.route_file != str(Path(route_file).resolve())
                    or binding.route_id != args.route_id or binding.route_hash != args.route_hash):
                raise JOIN.JoinContractError("resource-owner-route-changed")
        expected = {"parent_attempt_id": args.parent_attempt_id, "session_id": control.thread_id,
            "route_id": args.route_id, "route_hash": args.route_hash, "jobs": str(jobs)}
        parent = JOIN.exact_attempt_row(jobs, args.parent_attempt_id)
        if parent.status not in {"open", "running"}:
            raise JOIN.JoinContractError("resource-owner-not-open")
        OWNER._owner_row_proof(parent.raw.split("\t"), parent.metadata, route=route, environ={})
        if (any(owner.get(key) != value for key, value in expected.items())
                or str(owner.get("owner_pid")) != parent.metadata.get("pid")
                or owner.get("owner_start") != parent.metadata.get("pid_start")
                or row.get("parent_attempt_id") != args.parent_attempt_id
                or row.get("route") != binding.route_file or row.get("node") != node
                or row.get("jobs") != str(jobs) or not row.get("command")
                or not (controller_intent(row) or resource_never_started(row) or
                        (type(row.get("pid")) is int and row["pid"] > 0 and row.get("starttime")))
                or (row.get("pid_namespace") is not None and
                    row["pid_namespace"] != os.readlink("/proc/self/ns/pid"))
                or armed.get("route_id") != args.route_id or armed.get("route_hash") != args.route_hash
                or armed.get("route_file") != binding.route_file or armed.get("jobs") != str(jobs)
                or armed.get("resource_binding") != resource_body_digest(row)
                or armed.get("successor_external") is not True or armed.get("successor_command") is not None):
            raise JOIN.JoinContractError("resource-owner-binding-invalid")
        result.append((armed, row))
    return sup, route, ledger, result


def model_legs_pending(args, delivered):
    rows = JOIN.current_children(Path(args.jobs), args.parent_attempt_id,
        route_id=getattr(args, "route_id", None), route_hash=getattr(args, "route_hash", None))
    candidates = {row.attempt_id for row in rows}.difference(delivered)
    partition = JOIN.partition_runtime_wait_children(Path(args.jobs), args.parent_attempt_id, rows, candidates)
    return bool(partition.joinable or partition.unstarted or partition.chain_pending)


def admit_controller_launch(args, control, armed, row, delivered=()):
    """The existing leased controller, not the native tool, owns the real launch."""
    if not controller_intent(row) or row.get("status") != "launching":
        return
    from resource_run_registry import proc_identity
    runner = supervisor().runner()
    command_builder = getattr(args, "resource_launch_command", None)
    owner = row["owner_wait"]
    identity = proc_identity(os.getpid())
    try:
        namespace = os.readlink("/proc/self/ns/pid")
        same_scope = namespace == os.readlink(f'/proc/{owner["owner_pid"]}/ns/pid')
    except (OSError, KeyError):
        same_scope = False
    if command_builder is None or not identity or not same_scope:
        raise JOIN.JoinContractError("resource-controller-scope-unavailable")
    identity["pid_namespace"] = namespace
    import dispatch_owner_input as INPUT
    @contextlib.contextmanager
    def guard():
        with INPUT._locked(args.jobs, args.parent_attempt_id) as (_, value):
            parent, target = INPUT._target(args.jobs, args.parent_attempt_id)
            import route_parent_close
            if route_parent_close.row_requested(parent.metadata, args.jobs):
                raise runner.LaunchDeferred("cancelled-by-parent")
            live = proc_identity(owner["owner_pid"])
            if (value is None or value.get("target") != target or
                    value.get("thread_id") != control.thread_id or parent.status not in {"open", "running"}
                    or parent.metadata.get("harness") != "codex"
                    or parent.metadata.get("pid") != str(owner["owner_pid"])
                    or parent.metadata.get("pid_start") != owner.get("owner_start")
                    or not live or live["starttime"] != owner.get("owner_start")):
                raise JOIN.JoinContractError("resource-owner-input-binding-changed")
            if (args.parent_attempt_id != owner.get("parent_attempt_id") or
                    str(Path(args.jobs).resolve()) != owner.get("jobs")):
                raise JOIN.JoinContractError("resource-owner-input-binding-changed")
            if any(item.get("state") == "queued" for item in value["requests"]):
                raise runner.LaunchDeferred("resource-owner-correction-pending")
            if model_legs_pending(args, delivered):
                raise runner.LaunchDeferred("resource-model-child-pending")
            yield
    if row["launch_state"] == "claimed":
        # A crash after claim is not a known-never-started request. The private
        # fence prevents payload release, but observation never authorizes retry.
        failed = {**row, "status": "failed", "workflow_state": "FAILED_RETRYABLE",
                  "failure_class": "resource-launch-incomplete"}
        runner.publish_verified_run(armed["resource_registry"], row["run_id"], row, failed)
        return
    command, sandbox = command_builder(row)
    controller = SimpleNamespace(expected=row, identity=identity, command=command,
                                 sandbox=sandbox, guard=guard)
    # Tool receipts stay in the native tool response; the outer stream contains
    # only its existing typed controller events, not a second raw CLI receipt.
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            runner.main(runner.controller_argv(armed["resource_registry"], row), controller=controller)
        except Exception:
            # The runner settles its own pre-release failures before raising.
            # Let the ordinary resource outbox carry that failure to this same
            # owner. Unsettled/foreign rows and errors after release still raise.
            from resource_run_registry import resource_never_started
            actual = json.loads(Path(armed["resource_registry"]).read_text())["runs"].get(row["run_id"])
            if (not isinstance(actual, dict) or not resource_never_started(actual)
                    or resource_body_digest(actual) != resource_body_digest(row)
                    or actual.get("share") != row.get("share")
                    or actual.get("launch_controller") != identity):
                raise
    if hasattr(controller, "children"):
        owned = getattr(args, "resource_children", None)
        if owned is None:
            args.resource_children = owned = {}
        owned[resource_key(controller.row)] = controller.children


def _write(path, parent, delivered, resource, phase):
    if path is None:
        raise JOIN.JoinContractError("resource-supervisor-state-required")
    with JOIN._supervisor_state_lock(path):
        state = JOIN.read_supervisor_phase_state(path, parent)
        if state is not None and state.outbox is not None:
            raise JOIN.JoinContractError("resource-model-outbox-pending")
        JOIN._write_supervisor_state_unlocked(path, parent, delivered, phase=phase, resource=resource)


def pending_prompt(path, parent, args=None, control=None):
    state = JOIN.read_supervisor_phase_state(path, parent)
    box = (state.resource or {}).get("outbox") if state else None
    if not box:
        return None
    if args is not None and control is not None:
        receipt = box["receipt"]
        expected = {"parent_attempt_id": parent, "session_id": control.thread_id,
                    "route_id": args.route_id, "route_hash": args.route_hash,
                    "jobs": str(Path(args.jobs).resolve())}
        found = context(args, control)
        row = next((r for _, r in found[3] if resource_key(r) == box["key"]), None) if found else None
        if row is None and found and found[3]:
            sup, route, ledger, _ = found
            for armed, prior in sup.resource_predecessors(ledger, receipt["node"]):
                owner = prior.get("owner_wait") or {}
                if (armed.get("route_id") == args.route_id and armed.get("route_hash") == args.route_hash
                        and armed.get("route_file") == str(Path(args.route_file).resolve())
                        and armed.get("jobs") == expected["jobs"]
                        and armed.get("successor_external") is True
                        and armed.get("successor_command") is None
                        and prior.get("resource_policy") == "supervised-owner"
                        and prior.get("parent_attempt_id") == parent
                        and owner.get("parent_attempt_id") == parent
                        and owner.get("session_id") == control.thread_id
                        and resource_key(prior) == box["key"]
                        and resource_execution_finished(prior)):
                    row = prior
                    break
        if (any(receipt.get(k) != v for k, v in expected.items()) or row is None
                or RESUME.row_digest(row) != receipt.get("resource_sha256")):
            raise JOIN.JoinContractError("resource-outbox-binding-changed")
    return ("Runtime resource receipt (not a model child or verification PASS): "
        + json.dumps(box["receipt"], sort_keys=True, separators=(",", ":"))
        + "\nUse pending user corrections first. Continue only the already authorized next work; "
        "do not restart the resource. Exit is not workflow completion.")


def acknowledge(path, parent, receipt_id):
    """Only the returning receiving turn acknowledges this exact resource receipt."""
    if not receipt_id:
        return False
    with JOIN._supervisor_state_lock(path):
        state = JOIN.read_supervisor_phase_state(path, parent)
        resource = dict(state.resource or {}) if state else {}
        box = resource.get("outbox")
        if not box or box["receipt_id"] != receipt_id:
            return False
        resource["delivered"] = resource["delivered"] + [box["key"]]
        resource["outbox"] = None
        JOIN._write_supervisor_state_unlocked(path, parent, set(state.delivered_attempt_ids),
            phase="running-turn", outbox=state.outbox, resource=resource)
        return True


def wait(args, path, control, delivered, emit, *, sleep=time.sleep):
    """Admit queued owner intent once, then wait outside model turns/budgets."""
    pending = pending_prompt(path, args.parent_attempt_id, args, control)
    if pending:
        return pending
    found = context(args, control)
    if found is None:
        return None
    sup, route, ledger, candidates = found
    if control.pending():
        return "Continue the same work using the pending user correction; preserve the resource request."
    for armed, row in candidates:
        if controller_intent(row) and row.get("status") == "launching":
            try:
                admit_controller_launch(args, control, armed, row, delivered)
            except sup.runner().LaunchDeferred as error:
                if str(error) == "resource-model-child-pending":
                    return None
                return "Continue the same work using the pending user correction; preserve the resource request."
            found = context(args, control)
            sup, route, ledger, candidates = found
            actual = next((r for _, r in candidates if r["run_id"] == row["run_id"]), None)
            emit({"type": "dispatch.supervisor.resource-admitted", "node": armed["node"],
                  "run_id": row["run_id"], "payload_spawned": bool(actual and actual.get("launch_state") == "started"),
                  "verification_pass": False, "workflow_complete": False})
            break
    state = JOIN.read_supervisor_phase_state(path, args.parent_attempt_id)
    resource = dict(state.resource or {}) if state else {}
    if resource and resource["session_id"] != control.thread_id:
        raise JOIN.JoinContractError("resource-native-session-changed")
    resource = resource or {"session_id": control.thread_id, "delivered": [], "outbox": None}
    candidates = [(a, r) for a, r in candidates if resource_key(r) not in resource["delivered"]]
    if not candidates:
        return None
    # A model stage admits at most one next resource per receiving turn. Every
    # additional exact resource stays armed and will be collected subsequently.
    armed, original = candidates[0]
    key = resource_key(original)
    _write(path, args.parent_attempt_id, delivered, resource, "parked")
    emit({"type": "dispatch.supervisor.resource-parked", "parent_attempt_id": args.parent_attempt_id,
          "node": armed["node"], "run_id": original["run_id"]})
    while True:
        if control.pending():
            return "Continue the same work using the pending user correction; preserve the active resource."
        current = context(args, control)
        if current is None:
            raise JOIN.JoinContractError("resource-owner-context-lost")
        exact = next(((a, r) for a, r in current[3] if resource_key(r) == key), None)
        if exact is None:
            raise JOIN.JoinContractError("resource-owner-row-changed")
        armed, row = exact
        # Owner watches use the existing external successor surface. Polling can
        # record/claim evidence, but cannot spawn the owner's next model leg.
        if control.pending():
            continue
        for child in getattr(args, "resource_children", {}).get(key, ()):
            child.poll()  # Only actual unreaped Popen children, never rediscovered PIDs.
        sup.poll_once(route, ledger)
        evidence = sup.resource_evidence(armed)
        stage = ledger.state().get("nodes", {}).get(armed["node"], {})
        lost_watch = not RESUME.supervisor_alive(row.get("supervision"))
        if lost_watch and evidence.get("liveness") == "working":
            recovery = sup.reattach_resource_watch(route, ledger, armed)
            lost_watch = not (recovery and recovery.get("supervisor_alive"))
        intermediate = (stage.get("state") == "RUNNING"
                        and (stage.get("evidence") or {}).get("awaiting_next_resource") is True)
        if intermediate or stage.get("state") == "STAGE_SUCCEEDED" or stage.get("state") in {"FAILED_RETRYABLE", "FAILED_TERMINAL", "CANCELLED"} or lost_watch:
            current_rows = context(args, control)[3]
            receipt_row = next((r for _, r in current_rows if resource_key(r) == key), None)
            if receipt_row is None:
                raise JOIN.JoinContractError("resource-owner-row-changed")
            artifact = sup.artifact_evidence(armed)
            proven = (stage.get("state") == "STAGE_SUCCEEDED" and evidence.get("succeeded")
                and evidence.get("liveness") == "exited" and evidence.get("exit_code") == 0
                and sup.runner().read_sentinel(receipt_row.get("sentinel")) == 0
                and (stage.get("evidence") or {}).get("resource_sha256") == RESUME.row_digest(receipt_row)
                and not artifact.get("missing"))
            execution_only = (intermediate and evidence.get("succeeded")
                and resource_execution_succeeded(receipt_row)
                and (stage.get("evidence") or {}).get("resource_sha256") == RESUME.row_digest(receipt_row))
            outcome = ("cancelled" if receipt_row.get("cancel_requested") else
                       "succeeded" if proven or execution_only else "needs-attention")
            receipt = {"type": "resource-completion", "parent_attempt_id": args.parent_attempt_id,
                "session_id": control.thread_id, "route_id": args.route_id, "route_hash": args.route_hash,
                "jobs": str(Path(args.jobs).resolve()), "node": armed["node"], "run_id": row["run_id"],
                "resource_key": key, "resource_sha256": RESUME.row_digest(receipt_row), "state": outcome, "exit_code": evidence.get("exit_code"),
                "reason": "awaiting-next-resource" if execution_only else
                    "resource-watch-lost" if lost_watch and not evidence.get("terminal") else stage.get("state"),
                **({"launch_error": receipt_row["launch_error"]} if receipt_row.get("launch_error") else {}),
                "verification_pass": False, "workflow_complete": False,
                "successors": list(armed["successors"]) if proven else []}
            digest = RESUME.row_digest(receipt)
            resource["outbox"] = {"receipt_id": "resource-" + digest[:32], "digest": digest,
                                  "key": key, "receipt": receipt}
            _write(path, args.parent_attempt_id, delivered, resource, "deliverable")
            return pending_prompt(path, args.parent_attempt_id, args, control)
        sleep(1)


def needs_recovery(path, parent, failed=False):
    state = JOIN.read_supervisor_phase_state(path, parent)
    return bool(state and state.resource and
                (failed or state.phase == "parked" or state.resource.get("outbox")))


def durable_native_session(args):
    """The already sealed owner route, before starting its native session."""
    path = getattr(args, "route_file", "")
    if not path:
        return False
    route = supervisor().load_route(path)
    return (not RESUME.route_selected(route) and any(
        n.get("kind") == "resource-runner" and
        (n.get("continuation") or {}).get("kind") == "supervised"
        for n in route.get("nodes", [])))
