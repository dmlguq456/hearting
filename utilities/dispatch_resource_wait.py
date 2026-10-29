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


def _experiment_result_note(row):
    row = row if isinstance(row, dict) else {}
    run_id = row.get("run_id")
    artifact_root = row.get("artifact_root")
    path = None
    record = None
    if artifact_root and run_id:
        try:
            import resource_progress
            path = resource_progress.public_record_path(artifact_root, run_id)
            record = resource_progress.read_public_run_record(artifact_root, run_id)
        except (OSError, ValueError, TypeError):
            record = None
    if not isinstance(record, dict):
        return (f"\nExperiment result: run_id={run_id or 'unknown'}; metrics unavailable; "
                f"public run record={path or 'unavailable'}; ETA/planned time unavailable.")
    rows = record.get("rows") if isinstance(record.get("rows"), list) else []
    row = max((item for item in rows if isinstance(item, dict)),
              key=lambda item: ((item.get("last_checkpoint") or {}).get("at") or 0),
              default={})
    summary = row.get("metric_summary") if isinstance(row.get("metric_summary"), dict) \
        else record.get("metric_summary") if isinstance(record.get("metric_summary"), dict) else {}
    key = summary.get("metric")
    def points(field, fallback):
        values = summary.get(field) if isinstance(summary.get(field), dict) else {}
        names = list(values)
        if key in names:
            names.remove(key)
            names.insert(0, key)
        parts = []
        for name in names[:3]:
            value = values.get(name)
            if not isinstance(value, dict):
                continue
            number = "NaN/Inf" if value.get("nonfinite") else value.get("value")
            parts.append(f"{name}={number}@{value.get('epoch')}")
        if parts:
            return ",".join(parts)
        if not isinstance(fallback, dict) or not key:
            return "지표 없음"
        number = "NaN/Inf" if fallback.get("nonfinite") else fallback.get("value")
        return f"{key}={number}@{fallback.get('epoch')}"
    start, end = record.get("started_at"), record.get("ended_at")
    actual = row.get("elapsed_seconds")
    if not isinstance(actual, (int, float)) and isinstance(start, (int, float)) and isinstance(end, (int, float)):
        actual = max(0, int(end - start))
    elif (not isinstance(actual, (int, float)) and isinstance(start, str)
            and isinstance(end, str)):
        try:
            from datetime import datetime
            actual = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
        except ValueError:
            pass
    def duration(value):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return "미상"
        hours, remainder = divmod(max(0, int(value)), 3600)
        return f"{hours}h{remainder // 60:02d}m" if hours else f"{remainder // 60}m"
    return (f"\nExperiment result: run_id={run_id}; row={row.get('row_id') or 'run'}; "
            f"last={points('latest_metrics', summary.get('latest'))}; "
            f"best={points('best_metrics', summary.get('best'))}; trend={summary.get('trend') or '판정 불가'}; "
            f"actual={duration(actual)} vs plan={duration(row.get('planned_seconds', record.get('planned_seconds')))}; "
            f"public run record={path}.")


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
    return {"parent_attempt_id": attempt, "session_id": session,
        **({"harness": row.metadata["harness"]} if row.metadata.get("harness") else {}),
        "route_id": route["route_id"],
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
    if "resource_operation" in row:
        body["resource_operation"] = row["resource_operation"]
    if "execution" in row:
        body["execution"] = row["execution"]
    return RESUME.row_digest(body)


def resource_legacy_body_digest(row):
    """Digest the projection that v3.53.2's flat reader validates."""
    keys = ("run_id", "cwd", "log", "command", "route", "node", "parent_attempt_id", "jobs",
            "config_ref", "config_sha256", "source_commit", "source_dirty", "source_git_state", "config_layout",
            "resource_policy", "owner_wait")
    body = {key: row.get(key) for key in keys}
    if "launch_request" in row:
        body["launch_request"] = row["launch_request"]
    if "execution" in row:
        body["execution"] = row["execution"]
    return RESUME.row_digest(body)


def resource_binding_matches(armed, row):
    """Check the old reader projection and, when present, the full current row."""
    legacy = resource_legacy_body_digest(row)
    if armed.get("resource_binding") != legacy:
        return False
    current = armed.get("resource_current_binding")
    if current is not None or row.get("resource_operation") is not None:
        return current == resource_body_digest(row)
    return True


class HolderUnobserved(JOIN.JoinContractError):
    """A preserved run's holder, or the watches an ancestor kept, could not be read.

    This is "could not look", never "nothing moved": callers retry the same
    observation (the wait loop on its next poll, a new start as a retryable
    refusal) instead of treating the answer as no transfer.
    """


def _pin_holder(route_file, route_id, route_hash, node, run_id, *, kind, jobs):
    import route_parent_close
    origin = {"route_file": route_file, "route_id": route_id, "route_hash": route_hash}
    try:
        return route_parent_close.pin_handoff_holder(
            route_file, route_id, route_hash, node, run_id, kind=kind, jobs=jobs)
    except Exception as error:  # an unexpected failure is never "no transfer"
        return {"route_file": route_file, "route_id": route_id, "route_hash": route_hash,
                "inherited": False, "origin": origin, "chain": [], "observed": False,
                "unobserved_reason": "holder-unexpected:" + type(error).__name__}


def resource_holder(row, *, kind="resource"):
    """The route that currently holds one resource row, with its origin address."""
    row = row if isinstance(row, dict) else {}
    owner = row.get("owner_wait") or {}
    route_id = owner.get("route_id") or row.get("route_id")
    route_hash = owner.get("route_hash") or row.get("route_hash")
    return _pin_holder(row.get("route"), route_id, route_hash, row.get("node"),
                       row.get("run_id"), kind=kind, jobs=row.get("jobs"))


def compute_holder(provenance, *, run_id=None, jobs=None):
    """The route that currently holds one compute execution's provenance.

    Accepts both shapes the same record appears in: the nested
    ``meta.provenance`` (its ``resource`` sub-dict carries the route address)
    and the flat resource provenance that `compute-hosts.execution_record`
    already extracted. The record itself is never rewritten.
    """
    provenance = provenance if isinstance(provenance, dict) else {}
    nested = provenance.get("resource")
    resource = nested if isinstance(nested, dict) else provenance
    identity = run_id or provenance.get("compute_run_id")
    return _pin_holder(resource.get("route_file"), resource.get("route_id"),
                       resource.get("route_hash"), resource.get("node"), identity,
                       kind="compute", jobs=jobs or resource.get("jobs"))


def current_responsibility(row, *, route, owner_attempt_id, session_id, jobs, historical=False):
    """Does `route` hold `row`, and is `owner_attempt_id` bound to that route?

    Answers q2 (the route that holds the run) and q3 (the owner attempt bound
    to that route). It does NOT answer q4, the native conversation: the effect
    paths (context/wait/admit/aggregate/owner input) keep their own owner input
    target/thread/lease, phase-state session and `_owner_row_proof` checks, and
    an inherited row's `session_id` is deliberately not read here because the
    recorded session is the previous owner's.

    ``historical=True`` reads a recorded fact (marker currency, settlement
    snapshot): the owner attempt need not be running now. Effect paths leave it
    False, so for an inherited row the owner attempt must be open/running; that
    separates a past owner of the same route from the current one.

    q1 (which route/node/watch recorded the run) stays exact against
    ``holder["origin"]``. A row no proven handoff moved keeps its exact checks.
    A holder that could not be observed is not current.
    """
    route = route if isinstance(route, dict) else {}
    holder = resource_holder(row)
    if not holder.get("observed", True):
        return None
    if (holder.get("route_id") != route.get("route_id")
            or holder.get("route_hash") != route.get("route_hash")):
        return None
    owner = (row.get("owner_wait") or {}) if isinstance(row, dict) else {}
    expected_jobs = str(jobs) if jobs not in (None, "") else None
    if not holder.get("inherited"):
        # Older rows named only their route file; its sealed address (and the
        # row's own jobs record) is that row's origin. This fallback replaces
        # owner_wait.route_id/route_hash/jobs only when they are absent.
        row_route_id = owner.get("route_id") or holder.get("route_id")
        row_route_hash = owner.get("route_hash") or holder.get("route_hash")
        row_jobs = owner.get("jobs") or row.get("jobs")
        if (owner.get("parent_attempt_id") != owner_attempt_id
                or row.get("parent_attempt_id") != owner_attempt_id
                or owner.get("session_id") != session_id
                or row_route_id != route.get("route_id")
                or row_route_hash != route.get("route_hash")
                or (expected_jobs is not None
                    and (row_jobs or "") != expected_jobs)):
            return None
        return holder
    import owner_route_binding as OWNER
    selected = expected_jobs or owner.get("jobs")
    try:
        binding, _ = OWNER.resolve_owner_route_lifecycle(
            selected, owner_attempt_id=owner_attempt_id)
    except (OWNER.OwnerRouteBindingError, OSError, ValueError, TypeError):
        return None
    if binding is None or (binding.route_id, binding.route_hash, binding.route_file) != (
            holder.get("route_id"), holder.get("route_hash"),
            str(Path(holder.get("route_file") or "").resolve())):
        return None
    if not historical:
        try:
            if JOIN.exact_attempt_row(Path(selected), owner_attempt_id).status not in {
                    "open", "running"}:
                return None
        except (JOIN.JoinContractError, OSError, ValueError, TypeError):
            return None
    return holder


def held_watches(route, jobs, node=None):
    """Watches an ancestor route preserved for the node this successor resumes.

    Raises `HolderUnobserved` when the lineage, an ancestor ledger, a registry
    or a row's holder cannot be read: an incomplete list is never returned as a
    complete one, and an unreadable row is never dropped.
    """
    if (route.get("continuation_contract_version") != 1
            or not str(route.get("reason") or "").startswith("owner-pin-handoff:")):
        return []
    import route_lineage as RL
    try:
        lineage = RL.verified_route_lineage(route, observe=True)
    except (OSError, ValueError, TypeError) as error:
        raise HolderUnobserved("route-lineage-unobserved:" + type(error).__name__) from error
    sup = supervisor()
    result, seen = [], set()
    for ancestor in lineage[1:]:
        try:
            ledger = sup.ledger_for(ancestor, str(jobs))
            # `read_armed` skips a watch file it cannot parse; here that would
            # make a kept run vanish from the list, so read each one strictly.
            directory = sup.armed_dir(ledger)
            for watch_file in sorted(directory.glob("*.json")) if directory.is_dir() else ():
                json.loads(watch_file.read_text(encoding="utf-8"))
            watches = sup.read_resource_watches(ledger, node)
        except (OSError, ValueError, TypeError) as error:
            raise HolderUnobserved("ancestor-watches-unobserved:" + type(error).__name__) from error
        for armed in watches:
            try:
                runs = json.loads(Path(armed["resource_registry"]).read_text(encoding="utf-8"))["runs"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise HolderUnobserved("registry-unobserved:" + type(error).__name__) from error
            row = runs.get(armed.get("predecessor_id")) if isinstance(runs, dict) else None
            if not isinstance(row, dict):
                continue  # the watch names no row: nothing here to hold
            holder = resource_holder(row)
            if not holder.get("observed", True):
                raise HolderUnobserved("holder-unobserved:" + str(holder.get("unobserved_reason")))
            if not holder.get("inherited") or (
                    holder.get("route_id"), holder.get("route_hash")) != (
                    route.get("route_id"), route.get("route_hash")):
                continue
            key = sup.resource_watch_key(armed)
            if key in seen:
                continue
            seen.add(key)
            result.append((armed, row))
    return result


def resource_execution_finished(row):
    from route_authority import resource_predecessor_finished
    return resource_predecessor_finished(row)


def resource_execution_succeeded(row):
    """A past status word cannot substitute for the exact exit and sentinel."""
    return (not row.get("cancel_requested") and resource_execution_finished(row)
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
    body = {key: row.get(key) for key in keys}
    if "execution" in row:
        body["execution"] = row["execution"]
    return RESUME.row_digest(body)


NONTERMINAL_RESOURCE_REASON = "resource-observation-unsettled"


def resource_attention_delivery_key(key):
    """Identify the one advisory receipt for an unsettled execution."""
    return RESUME.row_digest({"kind": "nonterminal-resource-attention", "resource_key": key})


def resource_needs_attention(row, evidence):
    """Surface failures and uncertain observations without settling execution."""
    if evidence.get("terminal") is True and evidence.get("quiescent") is True:
        return False
    if row.get("cancel_requested") or row.get("status") in {"failed", "cancelled"}:
        return True
    if evidence.get("terminal") is True:
        # A terminal exit whose process group is not quiescent still has cleanup
        # work outstanding and must not be reported as a settled result.
        return True
    liveness = evidence.get("liveness")
    reason = evidence.get("reason")
    if liveness == "working":
        return False
    if reason == "resource-launching" and liveness in {None, "working"}:
        return False
    # Missing or ambiguous liveness is an owner-facing attention condition.
    return True


def context(args, control):
    """Read only exact armed records this owner route currently holds."""
    import owner_route_binding as OWNER
    import dispatch_contract as DC
    from resource_run_registry import (
        compute_execution_identity, resource_never_started,
    )
    route_file = getattr(args, "route_file", "")
    if not route_file or not Path(route_file).is_file():
        return None
    jobs = Path(args.jobs).resolve(strict=True)
    sup = supervisor()
    route = sup.load_route(route_file)
    ledger = sup.ledger_for(route, str(jobs))
    armed_rows = []
    seen = set()
    for node in route.get("nodes", []):
        if not isinstance(node, dict) or node.get("kind") != "resource-runner":
            continue
        try:
            predecessors = sup.resource_predecessors(ledger, node.get("id"))
        except (OSError, json.JSONDecodeError) as error:
            raise HolderUnobserved("registry-unobserved:" + type(error).__name__) from error
        for armed, _row in predecessors:
            key = sup.resource_watch_key(armed)
            if key not in seen:
                seen.add(key)
                armed_rows.append(armed)
    for armed, _row in held_watches(route, str(jobs)):
        key = sup.resource_watch_key(armed)
        if key not in seen:
            seen.add(key)
            armed_rows.append(armed)
    if not armed_rows:
        return None
    binding = None
    result = []
    expected_jobs = str(jobs)
    current_route = {"route_id": args.route_id, "route_hash": args.route_hash,
                     "route_file": str(Path(route_file).resolve())}
    for armed in sorted(armed_rows, key=lambda row: (str(row.get("node")),
                                                     str(row.get("predecessor_id")))):
        node = armed["node"]
        try:
            data = json.loads(Path(armed["resource_registry"]).read_text())
        except (OSError, ValueError) as error:
            raise HolderUnobserved("registry-unobserved:" + type(error).__name__) from error
        row = data.get("runs", {}).get(armed["predecessor_id"])
        if not isinstance(row, dict) or row.get("resource_policy") != "supervised-owner":
            continue
        owner = row.get("owner_wait") or {}
        holder = resource_holder(row)
        proven = bool(holder.get("observed", True) and holder.get("inherited")
                      and (holder.get("route_id"), holder.get("route_hash"))
                      == (current_route["route_id"], current_route["route_hash"]))
        if not proven and owner.get("parent_attempt_id") != args.parent_attempt_id:
            continue
        if not holder.get("observed", True):
            if str(holder.get("unobserved_reason")).startswith("route-identity-mismatch"):
                # The record names an address its own sealed file contradicts:
                # that is a bad binding, not a read to retry.
                raise JOIN.JoinContractError("resource-owner-binding-invalid")
            raise HolderUnobserved("holder-unobserved:" + str(holder.get("unobserved_reason")))
        if binding is None:
            binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=args.parent_attempt_id)
            if (binding is None or binding.route_file != current_route["route_file"]
                    or binding.route_id != args.route_id or binding.route_hash != args.route_hash):
                raise JOIN.JoinContractError("resource-owner-route-changed")
        responsibility = current_responsibility(
            row, route=current_route, owner_attempt_id=args.parent_attempt_id,
            session_id=control.thread_id, jobs=expected_jobs)
        if responsibility is None:
            raise JOIN.JoinContractError("resource-owner-binding-invalid")
        origin = holder["origin"]
        pinned_owner_identity = (str(owner.get("owner_pid", "")),
                                 str(owner.get("owner_start", "")))
        remote_identity = compute_execution_identity(row)
        execution = row.get("execution") if isinstance(row, dict) else None
        identity_valid = (bool(remote_identity) and isinstance(execution, dict)
                          and execution.get("receipt_state") in {"running", "finished"}
                          if remote_identity else
                          (controller_intent(row) or resource_never_started(row)
                           or (type(row.get("pid")) is int and row["pid"] > 0
                               and row.get("starttime"))))
        parent = JOIN.exact_attempt_row(jobs, args.parent_attempt_id)
        if parent.status not in {"open", "running"}:
            raise JOIN.JoinContractError("resource-owner-not-open")
        OWNER._owner_row_proof(parent.raw.split("\t"), parent.metadata, route=route, environ={})
        if (row.get("route") != origin.get("route_file")
                or row.get("route_id") not in (None, origin.get("route_id"))
                or row.get("node") != node
                or row.get("jobs") != expected_jobs or not row.get("command")
                or not identity_valid
                or (not remote_identity and row.get("pid_namespace") is not None and
                    row["pid_namespace"] != os.readlink("/proc/self/ns/pid"))
                or armed.get("route_id") != origin.get("route_id")
                or armed.get("route_hash") != origin.get("route_hash")
                or armed.get("route_file") != origin.get("route_file")
                or armed.get("jobs") != expected_jobs
                or not resource_binding_matches(armed, row)
                or armed.get("successor_external") is not True or armed.get("successor_command") is not None
                or (not holder["inherited"]
                    and (row.get("route") != current_route["route_file"]
                         or pinned_owner_identity not in DC.attempt_identity_lineage(parent.metadata)))):
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
                                 sandbox=sandbox, guard=guard, registry=armed["resource_registry"])
    # Tool receipts stay in the native tool response; the outer stream contains
    # only its existing typed controller events, not a second raw CLI receipt.
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            runner.main(runner.controller_argv(armed["resource_registry"], row), controller=controller)
        except (Exception, SystemExit):
            # The runner settles its own pre-release failures before raising.
            # Let the ordinary resource outbox carry that failure to this same
            # owner. Unsettled/foreign rows and errors after release still raise.
            from resource_run_registry import resource_never_started
            actual = json.loads(Path(armed["resource_registry"]).read_text())["runs"].get(row["run_id"])
            import route_authority
            decision = route_authority.decide_continuation(action="resource-failure", facts={
                "not_started": isinstance(actual, dict) and resource_never_started(actual),
                "bound": isinstance(actual, dict) and resource_body_digest(actual) == resource_body_digest(row)
                    and actual.get("share") == row.get("share") and actual.get("launch_controller") == identity})
            if not decision.allowed:
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
    row = None
    receipt = box["receipt"]
    if args is not None and control is not None:
        expected = {"parent_attempt_id": parent, "session_id": control.thread_id,
                    "route_id": args.route_id, "route_hash": args.route_hash,
                    "jobs": str(Path(args.jobs).resolve())}
        found = context(args, control)
        row = next((r for _, r in found[3] if resource_key(r) == box["key"]), None) if found else None
        if row is None and found and found[3]:
            sup, route, ledger, _ = found
            for armed, prior in sup.resource_predecessors(ledger, receipt["node"]):
                holder = resource_holder(prior)
                origin = holder["origin"]
                if (armed.get("route_id") == origin.get("route_id")
                        and armed.get("route_hash") == origin.get("route_hash")
                        and armed.get("route_file") == origin.get("route_file")
                        and armed.get("jobs") == expected["jobs"]
                        and armed.get("successor_external") is True
                        and armed.get("successor_command") is None
                        and prior.get("resource_policy") == "supervised-owner"
                        and current_responsibility(
                            prior, route=route, owner_attempt_id=parent,
                            session_id=control.thread_id, jobs=expected["jobs"]) is not None
                        and resource_key(prior) == box["key"]
                        and resource_execution_finished(prior)):
                    row = prior
                    break
        if (any(receipt.get(k) != v for k, v in expected.items()) or row is None
                or RESUME.row_digest(row) != receipt.get("resource_sha256")):
            raise JOIN.JoinContractError("resource-outbox-binding-changed")
    from resource_run_registry import resource_never_started
    # The bound receipt keeps outcome guidance identical on a returning turn
    # even when that turn has no row context. It grants no execution authority.
    next_work = ("The payload never started. Preserve this failed run, address its launch diagnostic, "
        "recheck current inputs through the existing smoke/config verification, "
        "and use the normal distinct __a<N> resource retry for unfinished authorized work. "
        "Do not rerun completed stages." if row is not None and resource_never_started(row) else
        ("The payload completed successfully. Preserve its result and do not rerun completed stages; "
         "continue through the declared successor's existing gate." if
         receipt.get("state") == "succeeded" and receipt.get("exit_code") == 0 else
         "Follow existing user instructions and pending corrections. This runtime receipt reports "
         "execution facts; it adds no retry authorization and no user prohibition. Preserve partial "
         "outputs and continue only within the existing approved scope."))
    return ("Runtime resource receipt (not a model child or verification PASS): "
        + json.dumps(box["receipt"], sort_keys=True, separators=(",", ":"))
        + (receipt["experiment_result"] if "experiment_result" in receipt
           else _experiment_result_note(row or receipt))
        + "\nUse pending user corrections first. " + next_work + " Exit is not workflow completion.")


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
        receipt = box["receipt"]
        delivered_key = (resource_attention_delivery_key(box["key"])
                         if receipt.get("reason") == NONTERMINAL_RESOURCE_REASON
                         else box["key"])
        if delivered_key not in resource["delivered"]:
            resource["delivered"] = resource["delivered"] + [delivered_key]
        resource["outbox"] = None
        JOIN._write_supervisor_state_unlocked(path, parent, set(state.delivered_attempt_ids),
            phase="running-turn", outbox=state.outbox, resource=resource)
        return True


def wait(args, path, control, delivered, emit, *, sleep=time.sleep):
    """Scan every exact resource and deliver one ready receipt through the existing outbox."""
    correction = "Continue the same work using the pending user correction; preserve the resource request."

    def look(observation):
        """An unobserved holder is looked at again on the next poll, never read as "nothing"."""
        while True:
            try:
                return observation(), False
            except HolderUnobserved:
                if control.pending():
                    return None, True
                sleep(1)

    pending, interrupted = look(lambda: pending_prompt(path, args.parent_attempt_id, args, control))
    if interrupted:
        return correction
    if pending:
        return pending
    found, interrupted = look(lambda: context(args, control))
    if interrupted:
        return correction
    if found is None:
        return None
    sup, route, ledger, candidates = found
    if control.pending():
        return correction

    def inherit():
        # The first wait after a proven owner-pin handoff records the inherited
        # watches once; later waits resume only what an interrupted write left.
        for armed, row in candidates:
            holder = resource_holder(row)
            if holder.get("inherited"):
                try:
                    sup.inherit_resource_watch(route, ledger, armed, holder)
                except sup.SupervisorError as error:
                    if str(error) == "resource-watch-inheritance-unavailable":
                        raise HolderUnobserved(str(error)) from error
                    raise

    _, interrupted = look(inherit)
    if interrupted:
        return correction
    for armed, row in candidates:
        if (row.get("resource_policy") in {"verified-resume", "supervised-owner"}
                and row.get("status") == "running"):
            # The parked owner reuses the existing identity-bound observer
            # recovery before waiting on the resource receipt or child set.
            sup.reattach_resource_watch(route, ledger, armed)
    state = JOIN.read_supervisor_phase_state(path, args.parent_attempt_id)
    resource = dict(state.resource or {}) if state else {}
    if resource and resource["session_id"] != control.thread_id:
        raise JOIN.JoinContractError("resource-native-session-changed")
    resource = resource or {"session_id": control.thread_id, "delivered": [], "outbox": None}
    if not any(resource_key(row) not in resource["delivered"] for _, row in candidates):
        return None

    for armed, row in sorted(candidates, key=lambda pair: (str(pair[0].get("node")),
                                                            str(pair[1].get("run_id")))):
        if not controller_intent(row) or row.get("status") != "launching":
            continue
        try:
            admit_controller_launch(args, control, armed, row, delivered)
        except sup.runner().LaunchDeferred as error:
            if str(error) == "resource-model-child-pending":
                continue
            return "Continue the same work using the pending user correction; preserve the resource request."
        refreshed, interrupted = look(lambda: context(args, control))
        if interrupted:
            return correction
        if refreshed is None:
            raise JOIN.JoinContractError("resource-owner-context-lost")
        actual = next((item for _, item in refreshed[3]
                       if item.get("run_id") == row.get("run_id")), None)
        emit({"type": "dispatch.supervisor.resource-admitted", "node": armed["node"],
              "run_id": row["run_id"], "payload_spawned": bool(actual and actual.get("launch_state") == "started"),
              "verification_pass": False, "workflow_complete": False})

    refreshed, interrupted = look(lambda: context(args, control))
    if interrupted:
        return correction
    if refreshed is None:
        raise JOIN.JoinContractError("resource-owner-context-lost")
    sup, route, ledger, candidates = refreshed
    if any(controller_intent(row) and row.get("status") == "launching"
           for _, row in candidates):
        return None
    _write(path, args.parent_attempt_id, delivered, resource, "parked")
    emit({"type": "dispatch.supervisor.resource-parked", "parent_attempt_id": args.parent_attempt_id,
          "route_id": getattr(args, "route_id", route.get("route_id")),
          "resource_count": len(candidates)})

    while True:
        if control.pending():
            return "Continue the same work using the pending user correction; preserve the active resource."
        current, interrupted = look(lambda: context(args, control))
        if interrupted:
            return "Continue the same work using the pending user correction; preserve the active resource."
        if current is None:
            raise JOIN.JoinContractError("resource-owner-context-lost")
        sup, route, ledger, current_rows = current
        outstanding = [(armed, row) for armed, row in current_rows
                       if resource_key(row) not in resource["delivered"]]
        if not outstanding:
            return None
        for armed, row in outstanding:
            key = resource_key(row)
            for child in getattr(args, "resource_children", {}).get(key, ()):
                child.poll()  # Only actual unreaped Popen children, never rediscovered PIDs.
        sup.poll_once(route, ledger)
        refreshed, interrupted = look(lambda: context(args, control))
        if interrupted:
            return "Continue the same work using the pending user correction; preserve the active resource."
        if refreshed is None:
            raise JOIN.JoinContractError("resource-owner-context-lost")
        ready = []
        for armed, row in refreshed[3]:
            key = resource_key(row)
            if key in resource["delivered"]:
                continue
            evidence = sup.resource_evidence(armed)
            settled = evidence.get("terminal") is True and evidence.get("quiescent") is True
            attention = not settled and resource_needs_attention(row, evidence)
            if not settled and not attention:
                continue
            if attention:
                if resource_attention_delivery_key(key) in resource["delivered"]:
                    continue
                outcome = "cancelled" if row.get("cancel_requested") else "needs-attention"
                reason = NONTERMINAL_RESOURCE_REASON
                exit_code = None
            else:
                succeeded = resource_execution_succeeded(row)
                artifacts = sup.artifact_evidence(armed)
                outcome = ("cancelled" if row.get("cancel_requested") else
                           "succeeded" if succeeded else "needs-attention")
                operation_set = sup.read_resource_watches(ledger, armed["node"])
                multi_operation = (len(operation_set) > 1
                                   or row.get("resource_operation") is not None)
                stage_state = ledger.state().get("nodes", {}).get(armed["node"], {}).get("state")
                reason = (("awaiting-resource-operations" if multi_operation else "awaiting-next-resource")
                          if succeeded and not artifacts.get("missing") else
                          "declared-output-pending" if succeeded else
                          stage_state if stage_state in {"FAILED_RETRYABLE", "FAILED_TERMINAL", "CANCELLED"} else
                          row.get("failure_class") or row.get("status"))
                exit_code = evidence.get("exit_code")
            receipt = {"type": "resource-completion", "parent_attempt_id": args.parent_attempt_id,
                "session_id": control.thread_id, "route_id": args.route_id, "route_hash": args.route_hash,
                "jobs": str(Path(args.jobs).resolve()), "node": armed["node"], "run_id": row["run_id"],
                "resource_key": key, "resource_sha256": RESUME.row_digest(row),
                "state": outcome, "exit_code": exit_code, "reason": reason,
                **({"launch_error": row["launch_error"]}
                   if outcome == "needs-attention" and row.get("launch_error") else {}),
                "verification_pass": False, "workflow_complete": False,
                "experiment_result": _experiment_result_note(row), "successors": []}
            ready.append((str(armed.get("node")), str(row.get("run_id")), key, receipt))
        if ready:
            _node, _run_id, key, receipt = sorted(ready, key=lambda item: (item[0], item[1]))[0]
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
