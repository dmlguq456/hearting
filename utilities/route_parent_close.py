"""The existing close's shared process cleanup, for every owner harness.

Intent and replay live in the existing workflow journal and attempt registry,
outside producer artifacts. The journal is not an artifact protection scheme.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime

import dispatch_contract as DC
import workflow_state as WS
from dispatch_registry_cache import registry_view

NOTE = "cancelled-by-parent"
OPEN = {"open", "running"}


def jobs_path(jobs=None):
    return Path(jobs) if jobs else DC.resolve_global_registry(
        DC.resolve_agent_home(), None, 0, "close").path


def requested(metadata):
    return metadata.get("parent_close_requested") == "1"


def intent(route, jobs=None):
    if not route.get("route_id") or not route.get("route_hash"):
        return None
    jobs = jobs_path(jobs)
    # Before the first dispatch there is no registry and no cancellation.
    if not jobs.exists():
        return None
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    return ledger_intent(route, ledger)


def ledger_intent(route, ledger, *, journal=None):
    # ``journal``: entries the caller already read from this ledger, checked as that one read.
    entries = ledger.journal() if journal is None else journal
    return next((e["evidence"]["parent_close"] for e in entries
                 if e.get("route_hash") == route["route_hash"]
                 and isinstance(e.get("evidence"), dict)
                 and isinstance(e["evidence"].get("parent_close"), dict)), None)


def settled_result(route, ledger, *, journal=None):
    entries = ledger.journal() if journal is None else journal
    return next((entry["evidence"]["parent_close_result"] for entry in reversed(entries)
                 if entry.get("route_hash") == route["route_hash"]
                 and isinstance(entry.get("evidence"), dict)
                 and isinstance(entry["evidence"].get("parent_close_result"), dict)), None)


def row_requested(metadata, jobs, *, registry_rows=None):
    if requested(metadata):
        return True
    route = {"route_id": metadata.get("parent_close_route_id") or metadata.get("owner_route_id") or metadata.get("route_id"),
             "route_hash": metadata.get("parent_close_route_hash") or metadata.get("owner_route_hash") or metadata.get("route_hash")}
    value = intent(route, jobs) or _current_owner_intent(
        metadata, jobs, registry_rows=registry_rows)
    return bool(value and metadata.get("attempt_id") in value["attempts"])


def cancellation_settled(metadata, jobs):
    """Observe the whole close, without driving cleanup from a queue consumer.

    An individual terminal row can precede resource/process settlement. The
    existing journal, rather than removable producer artifacts, owns that end.
    None is ordinary work; False leaves the existing observer responsible.
    """
    route = {"route_id": metadata.get("parent_close_route_id") or metadata.get("owner_route_id") or metadata.get("route_id"),
             "route_hash": metadata.get("parent_close_route_hash") or metadata.get("owner_route_hash") or metadata.get("route_hash")}
    value = intent(route, jobs) or _current_owner_intent(metadata, jobs)
    if not requested(metadata) and not (value and metadata.get("attempt_id") in value["attempts"]):
        return None
    if not value or metadata.get("parent_close_settled") != "1":
        return False
    route = value["route"]
    result = settled_result(route, WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs))
    return bool(result and result.get("state") == "cancelled")


def _current_owner_intent(metadata, jobs, *, registry_rows=None):
    # Attachments and adopted advances leave launch rows unchanged. Resolve
    # their current owner before accepting an old child's late result.
    try:
        attempt = metadata.get("attempt_id")
        rows = _rows(Path(jobs), {attempt} if attempt else set(),
                     registry_rows=registry_rows)
    except (OSError, ValueError):
        return None  # Ordinary join owns its registry-shape refusal.
    current, seen = metadata, set()
    while current.get("worker_type") != "owner":
        parent = current.get("parent_attempt_id")
        if parent in seen or parent not in rows:
            return None
        seen.add(parent)
        current = rows[parent][1]
    import owner_route_binding as OWNER
    try:
        binding, _ = OWNER.resolve_owner_route_lifecycle(
            jobs, owner_attempt_id=current["attempt_id"], registry_rows=registry_rows)
    except OWNER.OwnerRouteBindingError:
        return None  # An unbound ordinary owner is not cancellation evidence.
    return intent({"route_id": binding.route_id, "route_hash": binding.route_hash}, jobs) if binding else None


def _rows(jobs, attempts=None, *, registry_rows=None):
    if not jobs.exists():
        return {}
    if registry_rows is not None:
        selected = [(list(fields), dict(metadata)) for fields, metadata in registry_rows]
        ids = [metadata.get("attempt_id") for _fields, metadata in selected
               if metadata.get("attempt_id")]
        if len(ids) != len(set(ids)):
            raise ValueError("parent-close-attempt-not-unique")
    else:
        view = registry_view(jobs)
        if view.has_duplicate_attempts:
            raise ValueError("parent-close-attempt-not-unique")
        if attempts is None:
            selected = ((list(record.fields), record.completion_metadata()) for record in view.records)
        else:
            pending = list(dict.fromkeys(attempts))
            seen = set()
            selected = []
            while pending:
                aid = pending.pop()
                if aid in seen:
                    continue
                seen.add(aid)
                candidates = view.completion_attempt_records(aid)
                if len(candidates) > 1:
                    raise ValueError("parent-close-attempt-not-unique")
                if not candidates:
                    continue
                record = candidates[0]
                metadata = record.completion_metadata()
                selected.append((list(record.fields), metadata))
                parent = metadata.get("parent_attempt_id")
                if parent not in (None, "", "-"):
                    pending.append(parent)
    rows = {}
    for fields, metadata in selected:
        aid = metadata.get("attempt_id")
        if aid:
            if aid in rows:
                raise ValueError("parent-close-attempt-not-unique")
            rows[aid] = (fields, metadata)
    return rows


def _owner(route, path, jobs, rows):
    import owner_route_binding as OWNER
    import route_authority as AUTH
    candidates = []
    for aid, (fields, meta) in rows.items():
        if meta.get("worker_type") != "owner":
            continue
        if str(Path(fields[3]).resolve()) != str(Path(route["cwd"]).resolve()):
            continue
        # `start --route` registers its owner with the route's own keys (route_id/route_hash)
        # rather than an owner_route_* binding; with no current binding that row is this route's owner.
        launched = (not any(meta.get(k) for k in ("owner_route_file", "owner_route_id", "owner_route_hash"))
                    and (meta.get("route_id"), meta.get("route_hash")) == (route["route_id"], route["route_hash"]))
        relevant = (meta.get("owner_route_id") == route["route_id"]
                    or meta.get("owner_route_file") == str(path.resolve())
                    or route.get("owner_attempt_id") == aid)
        owned = AUTH.owns(meta, AUTH.default_parent_session_id(), jobs)
        if launched and not relevant:
            if fields[1] in OPEN and aid == os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"):
                continue  # the owner closing its own route finishes it; nothing to stop
            # Launch keys never move after an attachment/advance, so the current binding
            # decides first. Only two readings stop this owner: a clean current binding that
            # is exactly this route, or no binding record at all for it. Every other reading
            # (another route, a read error, any non-current status, known or future) stops nothing.
            try:
                binding, reason = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=aid)
            except OWNER.OwnerRouteBindingError:
                continue
            if binding is None:
                stops = reason == "owner-route-binding-absent"
            else:
                stops = reason in OWNER.CURRENT_ROUTE_STATUSES and (
                    binding.route_id, binding.route_hash, binding.route_file) == (
                    route["route_id"], route["route_hash"], str(path.resolve()))
            if stops:
                if fields[1] in OPEN and not AUTH.require_known(owned, meta, jobs):
                    raise ValueError("parent-close-owner-not-owned")
                candidates.append((aid, fields, meta))
            continue
        if relevant and fields[1] in OPEN:
            # Only who holds this route's live owner changes the close; for it an unreadable
            # lineage is a wait. Another route's owner in the same folder is skipped as before.
            owned = AUTH.require_known(owned, meta, jobs)
        # Ordinary route-free owners and unrelated historical rows coexist in
        # this registry. Their absent/old binding is not current-route evidence.
        if (not owned or fields[1] not in OPEN) and not relevant:
            continue
        try:
            binding, reason = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=aid)
        except OWNER.OwnerRouteBindingError as exc:
            if not owned:
                raise ValueError("parent-close-owner-not-owned") from exc
            raise ValueError("parent-close-current-owner-unobservable:" + str(exc)) from exc
        if not owned:
            if binding and (binding.route_id, binding.route_hash, binding.route_file) == (
                    route["route_id"], route["route_hash"], str(path.resolve())) and fields[1] in OPEN:
                raise ValueError("parent-close-owner-not-owned")
            continue
        if binding is None and reason == "owner-route-binding-absent":
            continue
        matches = binding and (binding.route_id, binding.route_hash, binding.route_file) == (
                route["route_id"], route["route_hash"], str(path.resolve()))
        if fields[1] in OPEN and (matches or relevant) and (
                not binding or "unresolvable" in reason or "conflict" in reason):
            raise ValueError("parent-close-current-owner-unobservable:" + reason)
        if matches:
            candidates.append((aid, fields, meta))
    active = [c for c in candidates if c[1][1] in OPEN]
    if len(active) > 1:
        raise ValueError("parent-close-current-owner-ambiguous")
    if not active:
        # A previously committed PASS wins over a later parent request.
        if any(DC.verdict_pass(c[2]) for c in candidates):
            return None, True
        return None, False
    aid, fields, meta = active[0]
    if aid == os.environ.get("AGENT_DISPATCH_ATTEMPT_ID"):
        raise ValueError("parent-close-requires-parent")
    if os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        # A dispatched worker is no session: it inherits its parent's session id,
        # which would let it close another route's live owner. Only the parent
        # closes a live owner's route, as for a one-attempt stop (_require_stopper).
        raise ValueError("parent-close-requires-parent")
    AUTH.require_owner(meta, AUTH.default_parent_session_id(), jobs, "parent-close-owner-not-owned")
    OWNER._owner_row_proof(fields, meta, route=route, environ={})
    return aid, False


def _owned_attempts(route, rows, owner):
    selected = {owner} if owner else set()
    while True:
        children = {aid for aid, (fields, meta) in rows.items()
                    if meta.get("parent_attempt_id") in selected
                    and fields[2] == rows[owner][0][2]
                    and (fields[3] == rows[owner][0][3] or DC.is_linked_worktree_slice(meta))}
        if children <= selected:
            return selected
        selected |= children


# Stopping one attempt is Fleet's user kill (#675) run from `close`: the same typed
# cancellation, so every reader already treats it as a committed cancellation that
# neither retries nor advances.
STOP_NOTE = "fleet-kill"
STOP_EVIDENCE = {"failure_class": "cancelled", "classifier_source": "route-close-attempt-v1",
                 "reconcile_reason": "user-terminated"}


def _user_stopped(metadata):
    return metadata.get("note") == STOP_NOTE and metadata.get("failure_class") == "cancelled"


def _ancestors(rows, aid):
    """``aid``'s registered parent attempts, nearest first, and its depth-1 attempt's row."""
    chain, current, seen = [], rows[aid][1], set()
    while current.get("dispatch_depth") != "1":
        parent = current.get("parent_attempt_id")
        if parent in seen or parent not in rows:
            raise ValueError("parent-close-owner-not-owned")
        seen.add(parent)
        chain.append(parent)
        current = rows[parent][1]
    return chain, current


def _require_stopper(rows, aid, jobs):
    """Only a parent stops one attempt: the session that answers for its depth-1
    attempt (owner or frame leg), or an attempt above it for its own descendant.
    A dispatched worker is no session, so it stops only its own descendants.
    An unreadable lineage waits for the same command (``require_owner``)."""
    import route_authority as AUTH
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller == aid:
        raise ValueError("parent-close-requires-parent")
    ancestors, root = _ancestors(rows, aid)
    if caller and caller in ancestors:
        return
    if os.environ.get("AGENT_DISPATCH_CHILD") == "1":
        raise ValueError("parent-close-requires-parent")
    AUTH.require_owner(root, AUTH.default_parent_session_id(), jobs, "parent-close-owner-not-owned")


def _known_resources(metadata, jobs, attempts):
    """Every known resource run, preserved exactly as route close preserves it; None when unread."""
    route = {"route_id": metadata.get("route_id") or metadata.get("owner_route_id"),
             "route_hash": metadata.get("route_hash") or metadata.get("owner_route_hash"), "nodes": []}
    path = Path(metadata.get("route_file") or metadata.get("owner_route_file") or os.devnull)
    try:
        return known_resources(route, path, jobs, set(attempts))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _drain_attempt(metadata, resources, grace, kill_wait):
    """TERM, then KILL after the grace, the attempt's own exact processes; True once none is left."""
    deadline, sent = time.monotonic() + grace + kill_wait, set()
    while True:
        try:
            processes, observed = _agent_processes(metadata, resources)
        except OSError:
            processes, observed = [], False
        if observed and not processes:
            return True
        if time.monotonic() >= deadline:
            return False
        escalated = time.monotonic() >= deadline - kill_wait
        for pid, start in processes:
            if (pid, start, escalated) not in sent:
                _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                sent.add((pid, start, escalated))
        time.sleep(0.02)


def _plan(jobs, rows, aid):
    """What stopping ``aid`` (and its registered descendants) still has to do, before any
    authority check or effect: ``work`` (open, or stopped with processes still observed),
    ``settle`` (stopped, nothing left running) and ``unknown`` (processes not observable,
    with whether the row is still open). A stop that already settled needs no parent."""
    attempts = _owned_attempts(None, rows, aid)
    work, settle, unknown = [], [], []
    # Children first: a stopped parent never leaves a running child behind it.
    for current in sorted(attempts - {aid}) + [aid]:
        fields, meta = rows[current]
        if fields[1] not in OPEN and not _user_stopped(meta):
            continue  # its own committed result stands
        resources = _known_resources(meta, jobs, attempts)
        try:
            processes, observed = ([], False) if resources is None else _agent_processes(meta, resources)
        except OSError:
            processes, observed = [], False
        if not observed:
            unknown.append((current, fields[1] in OPEN))
        elif fields[1] in OPEN or processes:
            work.append((current, fields, meta, resources))
        else:
            settle.append(current)
    return work, settle, unknown


def _settle_stopped(jobs, aid):
    """Once nothing of it runs: its cleanup receipt, then its parent's notice (both idempotent)."""
    DC.resolve_attempt_cleanup(jobs, aid, apply=True)
    from dispatch_completion_join import materialize_after_terminal_close
    materialize_after_terminal_close(jobs, aid)


def _execute(jobs, work, settle, grace, kill_wait):
    stopped, pending = [], []
    for current, fields, meta, resources in work:
        if fields[1] in OPEN:
            # Committed before any signal, so a late result or launch cannot replace it. Only
            # the same open row closes: a result committed first, a route close that owns the
            # row, or a changed process identity leaves it untouched.
            identity = ("pid", "pid_start", "launch_claimed")
            closed = DC.close_attempt_row_if(
                jobs, current, STOP_NOTE,
                lambda fresh, meta=meta: all(DC.parse_registry_metadata(fresh[5]).get(key, "")
                                             == meta.get(key, "") for key in identity),
                evidence=dict(STOP_EVIDENCE))
            if not closed:
                fresh = _rows(jobs, {current}).get(current)
                if fresh and fresh[0][1] in OPEN:
                    pending.append(current)  # a route close owns it, or its process changed
                    continue
                if not (fresh and _user_stopped(fresh[1])):
                    continue  # a result committed first stands
        stopped.append(current)
        if _drain_attempt(meta, resources, grace, kill_wait):
            settle.append(current)
        else:
            pending.append(current)  # the same close again finishes it; nobody is told yet
    for current in settle:
        _settle_stopped(jobs, current)
    return stopped, pending


def stop_attempt(attempt_id, *, jobs=None, grace=0.3, kill_wait=0.5):
    """`close` naming one attempt that is not a route owner: stop exactly that attempt.

    Its route, owner and siblings keep running; its parent receives the cancelled
    result like any other child result once nothing of it runs. None for an owner,
    whose stop is the route close. Repeating the same close finishes a stop left pending.
    """
    jobs = jobs_path(jobs)
    rows = _rows(jobs)
    if attempt_id not in rows or rows[attempt_id][1].get("worker_type") == "owner":
        return None
    work, settle, unknown = _plan(jobs, rows, attempt_id)
    if work:
        _require_stopper(rows, attempt_id, jobs)
    stopped, pending = _execute(jobs, work, settle, grace, kill_wait)
    pending += [aid for aid, _open in unknown]
    fields, meta = _rows(jobs, {attempt_id})[attempt_id]
    return {"attempt_id": attempt_id, "route_id": meta.get("route_id"), "route_node": meta.get("route_node"),
            "route_closed": False, "worker_type": meta.get("worker_type", ""),
            "state": "termination-pending" if pending else "cancelled" if _user_stopped(meta) else "terminal",
            "status": fields[1], "note": meta.get("note", ""), "stopped_attempts": stopped,
            "pending_attempts": pending}


def explain_stop(receipt, translate):
    """Add the relaunch fact to a receipt of a frame leg that is actually stopped.

    It is an explanation, never ``next_step``: a main session follows ``next_step``
    first, and nothing is to be done now. A terminal or still-pending stop gets nothing.
    """
    if receipt.get("worker_type") == "frame" and receipt.get("state") == "cancelled":
        receipt["explanation"] = translate("route.close.stopped_leg_next")
    return receipt


def stop_route_legs(route, *, jobs=None, grace=0.3, kill_wait=0.5):
    """Route close of a route whose running work is its frame legs: stop those legs first.

    Only a leg that still runs needs the parent, and every such leg's authority is
    checked before any leg is stopped. Legs already stopped hold nothing up.
    """
    jobs = jobs_path(jobs)
    if not jobs.exists():
        return None
    rows = _rows(jobs)
    plans = [(aid, _plan(jobs, rows, aid)) for aid, (fields, meta) in rows.items()
             if meta.get("worker_type") == "frame" and meta.get("dispatch_depth") == "1"
             and (meta.get("route_id"), meta.get("route_hash")) == (route["route_id"], route["route_hash"])
             and (fields[1] in OPEN or _user_stopped(meta))]
    for aid, (work, _settle, _unknown) in plans:
        if work:
            _require_stopper(rows, aid, jobs)
    stopped, pending = [], []
    for aid, (work, settle, unknown) in plans:
        done, waiting = _execute(jobs, work, settle, grace, kill_wait)
        stopped += done
        # A leg that may still run holds the record open; a stopped one does not.
        pending += waiting + [current for current, still_open in unknown if still_open]
    if not stopped and not pending:
        return None
    return {"state": "termination-pending" if pending else "cancelled", "reason": STOP_NOTE,
            "route_id": route["route_id"], "stopped_attempts": stopped, "pending_attempts": pending}


def request(route, path, *, jobs=None, stop_resources=False, summary=None, commit=None):
    """Serialize the intent with PASS and actual launch claims; no signal here."""
    jobs, path = jobs_path(jobs), Path(path)
    if not jobs.exists():
        return None
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    with ledger.lock():
        existing = intent(route, jobs)
        if existing:
            return existing
        outcome = path.with_suffix(".outcome.json")
        if outcome.exists() and json.loads(outcome.read_text()).get("terminal_gate_proven") is True:
            return None
        jobs.parent.mkdir(parents=True, exist_ok=True)
        with Path(str(jobs) + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = _rows(jobs)
            owner, succeeded = _owner(route, path, jobs, rows)
            if succeeded or not owner:
                return None
            attempts = _owned_attempts(route, rows, owner)
            value = {"route": route, "route_file": str(path.resolve()),
                     "owner_attempt_id": owner, "attempts": sorted(attempts),
                     "stop_resources": bool(stop_resources), "at": WS.now_iso(),
                     "summary": summary, "head_commit": commit}
            value["resources"] = linked_resources(route, path, jobs, attempts)
            value["protected_resources"] = known_resources(route, path, jobs, attempts)
            # Capture escaped payloads before stopping their launcher or agent.
            value["resource_branches"] = resource_branches(value["resources"])
            value["workflow_processes"] = sorted(_workflow_processes(route, ledger)[0])
            value["observer_namespace"] = DC.process_namespace_identity()
            # The intent is durable before either the launch fence or any signal.
            ledger._append({"at": value["at"], "route_id": route["route_id"],
                            "route_hash": route["route_hash"], "actor": "parent-close",
                            "evidence": {"parent_close": value}})
            updates = {}
            for aid, (fields, meta) in rows.items():
                if aid in attempts and fields[1] in OPEN:
                    fields[5] = DC._updated_attempt_metadata(fields[5], {
                        "parent_close_requested": "1", "parent_close_route_id": route["route_id"],
                        "parent_close_route_hash": route["route_hash"],
                        "parent_close_stop_resources": str(int(stop_resources)),
                    })
                updates[aid] = "\t".join(fields)
            if rows:
                lines = []
                for line in jobs.read_text().splitlines():
                    fields = line.split("\t")
                    aid = DC.parse_registry_metadata(fields[5]).get("attempt_id") if len(fields) == 6 else None
                    lines.append(updates.get(aid, line))
                DC._atomic_registry_replace(jobs, lines)
            return value


def linked_resources(route, path, jobs, attempts, *, all_known=False):
    """Discover existing run records, never GPU census or a project-name match."""
    import resource_run_registry as RR
    registries, _ = RR.indexed_paths()
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    armed_dir = ledger.root / "armed"
    named = set()
    for record in [*armed_dir.glob("*.json"), *armed_dir.glob("resource-runs/**/*.json")]:
        try:
            armed = json.loads(record.read_text())
        except (OSError, ValueError):
            continue
        # Position is ownership: a watch in this ledger holds its run even
        # though its own route fields may still name the route that recorded it.
        if armed.get("resource_registry"):
            registries.append(Path(armed["resource_registry"]))
            if armed.get("predecessor_kind") == "resource" and armed.get("predecessor_id"):
                named.add((str(Path(armed["resource_registry"])), armed["predecessor_id"]))
    import dispatch_resource_wait as OWNER_RESOURCE
    result = []
    for registry in sorted(set(registries)):
        try:
            data = json.loads(registry.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("runs"), dict):
            continue
        for rid, run in data.get("runs", {}).items():
            if not isinstance(run, dict):
                continue
            owner = run.get("owner_wait") or {}
            exact = not (run.get("route") != str(path.resolve())
                    or run.get("parent_attempt_id") not in attempts
                    or run.get("jobs") not in {None, "", str(jobs)}
                    or owner.get("route_hash", route["route_hash"]) != route["route_hash"]
                    or run.get("node") not in {n["id"] for n in route.get("nodes", [])})
            held = (str(registry), rid) in named
            holder = (OWNER_RESOURCE.resource_holder(run)
                      if not exact and not held and not all_known else None)
            inherited = bool(holder and holder.get("observed", True) and holder.get("inherited")
                             and (holder.get("route_id"), holder.get("route_hash"))
                             == (route["route_id"], route["route_hash"]))
            if not all_known and not exact and not held and not inherited:
                continue
            result.append({"kind": "resource", "run_id": rid, "registry": str(registry),
                           "row": {**run, "_registry": str(registry)}})
    # compute-hosts already records route + starting attempt in each run's meta.
    compute = _compute()
    try:
        config = compute.load_config()
    except compute.ConfigError:
        return result
    if compute._namespace_self(config):
        return result
    for meta_path in config["run_root"].glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        provenance = meta.get("provenance") or {}
        binding = provenance.get("route") or {}
        exact = (binding.get("route_id") == route["route_id"]
                 and binding.get("route_file") == str(path.resolve())
                 and provenance.get("attempt_id") in attempts)
        holder = None if exact or all_known else OWNER_RESOURCE.compute_holder(
            provenance, run_id=meta_path.parent.name)
        inherited = bool(holder and holder.get("observed", True) and holder.get("inherited")
                         and (holder.get("route_id"), holder.get("route_hash"))
                         == (route["route_id"], route["route_hash"]))
        if all_known or exact or inherited:
            state = compute._run_state(config, meta_path.parent.name)
            result.append({"kind": "compute", "run_id": meta_path.parent.name,
                           "state": "stopped" if state["stop_reason"] else state["state"],
                           "config": str(compute.config_path()), "record": str(meta_path)})
    return result


def known_resources(route, path, jobs, attempts):
    return linked_resources(route, path, jobs, attempts, all_known=True)


def _compute():
    spec = importlib.util.spec_from_file_location("parent_close_compute", Path(__file__).with_name("compute-hosts.py"))
    compute = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compute)
    return compute


def _namespace_authoritative(run):
    try:
        return run.get("pid_namespace", os.readlink("/proc/self/ns/pid")) == os.readlink("/proc/self/ns/pid")
    except OSError:
        return False


def _branch(run, seeds=()):
    """Exact root/descendant identities, including descendants in another group."""
    if not _namespace_authoritative(run):
        return set(), False
    selected = set()
    for pid, start in seeds:
        visible, actual, state = DC.process_observation(int(pid))
        if visible == "inaccessible":
            return selected, False
        if visible == "present" and actual == str(start) and state != "Z":
            selected.add((int(pid), str(start)))
    try:
        pid, start = int(run["pid"]), str(run["starttime"])
    except (KeyError, ValueError, TypeError):
        return selected, False
    visible, actual, state = DC.process_observation(pid)
    if visible == "inaccessible":
        return selected, False
    if visible == "present" and actual == start and state != "Z":
        selected.add((pid, start))
    children = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            tail = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            if tail[0] != "Z":
                children.append((int(entry.name), tail[19], int(tail[1])))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError):
            return selected, False
        if run.get("run_id") and run.get("_registry"):
            try:
                env = (entry / "environ").read_bytes().split(b"\0")
                if (b"HEARTING_RESOURCE_RUN_ID=" + run["run_id"].encode() in env
                        and b"HEARTING_RESOURCE_REGISTRY=" + run["_registry"].encode() in env):
                    selected.add((int(entry.name), tail[19]))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            except OSError:
                return selected, False
    while True:
        descendants = {(p, s) for p, s, parent in children if parent in {p for p, _ in selected}}
        if descendants <= selected:
            return selected, True
        selected |= descendants


def resource_branches(resources):
    return {r["run_id"]: sorted(_branch(r["row"])[0])
            for r in resources if r["kind"] == "resource"}


def _protected_records(resources):
    protected = {}
    for resource in resources:
        if resource["kind"] != "resource":
            continue
        run = resource["row"]
        if not _namespace_authoritative(run):
            continue  # A foreign namespace's PID is never a local PID authority.
        branch, _ = _branch(run)
        protected.update(dict.fromkeys(branch, resource))
        supervision = run.get("supervision") or {}
        if supervision.get("pid") and supervision.get("starttime"):
            watcher = (int(supervision["pid"]), str(supervision["starttime"]))
            if resource.get("parent_close_linked"):
                visible, actual, state = DC.process_observation(watcher[0])
                if visible == "present" and actual == watcher[1] and state != "Z":
                    protected[watcher] = resource
            else:
                protected.update(dict.fromkeys(
                    _branch({**run, "pid": watcher[0], "starttime": watcher[1]})[0], resource))
    return protected


def _protected(resources):
    return set(_protected_records(resources))


def _workflow_processes(route, ledger, seeds=()):
    selected = set()
    for pid, start in seeds:
        visible, actual, state = DC.process_observation(int(pid))
        if visible == "inaccessible":
            return selected, False
        if visible == "present" and actual == str(start) and state != "Z":
            selected.add((int(pid), str(start)))
    claims = {key: row for key, row in ledger.claims().items()
              if row.get("route_id") == route["route_id"]}
    if not claims:
        return selected, True
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        parent = None
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            tail = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
            parent = int(tail[1])
            env = dict(item.split(b"=", 1) for item in (entry / "environ").read_bytes().split(b"\0")
                       if b"=" in item)
            key = env.get(b"AGENT_WORKFLOW_CLAIM", b"").decode(errors="replace")
            claim = claims.get(key)
            if (claim and env.get(b"AGENT_WORKFLOW_ROUTE_ID") == route["route_id"].encode()
                    and env.get(b"AGENT_WORKFLOW_NODE") == str(claim["successor"]).encode()):
                visible, birth, state = DC.process_observation(int(entry.name))
                if visible == "inaccessible":
                    return selected, False
                if visible == "present" and state != "Z":
                    selected.add((int(entry.name), birth))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, KeyError):
            # Unrelated same-user processes need not expose their environment.
            # Unreadability inside the exact captured branch remains pending.
            if int(entry.name) in {p for p, _ in selected} or (
                    parent in {p for p, _ in selected}):
                return selected, False
            continue
    return selected, True


def _drain_workflow(value, ledger, resources, grace, kill_wait):
    if value.get("observer_namespace", DC.process_namespace_identity()) != DC.process_namespace_identity():
        return False
    deadline, sent = time.monotonic() + grace + kill_wait, set()
    while True:
        processes, observed = _workflow_processes(value["route"], ledger, value.get("workflow_processes", []))
        if not observed:
            return False
        processes -= _protected(resources)
        if not processes:
            return True
        escalated = time.monotonic() >= deadline - kill_wait
        for pid, birth in processes:
            if escalated or (pid, birth) not in sent:
                _signal(pid, birth, signal.SIGKILL if escalated else signal.SIGTERM)
                sent.add((pid, birth))
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _classify_agent_processes(meta, resources, *, terminal=False):
    """Filter resource branches before selecting exact agent PID/start pairs."""
    if meta.get("launch_claimed") == "0" and not meta.get("pid"):
        return [], True, {}
    identities = DC.authoritative_process_identities(meta)
    identity = identities[0] if identities else None
    if identity is None:
        return [], DC.attempt_process_quiescence(meta, terminal_receipt=terminal).state == "quiescent", {}
    visibility, actual, leader_state = DC.process_observation(identity.pid)
    if visibility == "inaccessible":
        return [], False, {}
    # Reuse is absence of the old leader, never authority over the new group.
    reused = visibility == "present" and actual != identity.expected_start
    group = (DC.ProcessGroupObservation("empty") if reused else
             DC.process_group_observation(identity.pid))
    tagged = DC.attempt_tagged_descendants(meta)
    members = {(p, s) for p, s, state in (*group.members, *tagged.members) if state != "Z"}
    if visibility == "present" and actual == identity.expected_start and leader_state != "Z":
        members.add((identity.pid, actual))
    ambiguous = {int(r["row"]["pid"]) for r in resources if r["kind"] == "resource"
                 and not _namespace_authoritative(r["row"]) and str(r["row"].get("pid", "")).isdigit()}
    if any(pid in ambiguous for pid, _ in members):
        if (visibility == "present" and actual == identity.expected_start
                and leader_state != "Z" and identity.pid not in ambiguous):
            return [(identity.pid, actual)], True, {}
        return [], False, {}  # An ambiguous resource cannot become an agent target.
    preserved = {pair: record for pair, record in _protected_records(resources).items()
                 if pair in members}
    members -= preserved.keys()
    # Resource-run environment tags also identify a re-setsid branch.
    run_ids = {r["run_id"] for r in resources}
    compute_records = {r["run_id"]: r for r in resources if r["kind"] == "compute"}
    agents = []
    for pid, start in sorted(members, reverse=True):
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        except OSError:
            return [], False, {}
        matches = sorted(rid for rid in run_ids
                         if b"HEARTING_COMPUTE_RUN_ID=" + rid.encode() in env)
        if matches:
            # Keep cleanup's existing selection. Continuation preserves only a
            # registered compute record, never a resource-only ID used as a compute tag.
            for rid in matches:
                if rid in compute_records:
                    preserved[(pid, start)] = compute_records[rid]
                    break
            continue
        agents.append((pid, start))
    return agents, group.state != "unverifiable" and tagged.state != "unverifiable", preserved


def _agent_processes(meta, resources, *, terminal=False):
    agents, observed, _ = _classify_agent_processes(meta, resources, terminal=terminal)
    return agents, observed


def owner_resources(meta, jobs):
    """Registered resources linked to one owner attempt, or None when unreadable."""
    import owner_route_binding as OWNER
    try:
        binding, _ = OWNER.resolve_owner_route_lifecycle(jobs, owner_attempt_id=meta["attempt_id"])
        if binding is None:
            return None
        _, route = OWNER._verified_binding(binding)
        return linked_resources(route, Path(binding.route_file), Path(jobs), {meta["attempt_id"]})
    except (OWNER.OwnerRouteBindingError, OSError, ValueError, KeyError, TypeError):
        return None


def _session_of(pid):
    try:
        return os.getsid(pid)
    except OSError:
        return None


def owner_consumer_session(meta, resources):
    """The governed process's session when it holds every unregistered owner process.

    Registered payloads run in sessions of their own. When they are running and
    nothing else of the owner is outside this session, stopping it leaves
    exactly the state consumer recovery restores.
    """
    identities = DC.authoritative_process_identities(meta)
    if not identities or not DC.attempt_scan_namespace_authority(meta):
        return None
    visibility, actual, state = DC.process_observation(identities[0].pid)
    if visibility != "present" or actual != identities[0].expected_start or state == "Z":
        return None
    session = _session_of(identities[0].pid)
    agents, observed, preserved = _classify_agent_processes(meta, resources)
    if (session is None or not observed or not preserved
            or any(_session_of(pid) != session for pid, _ in agents)):
        return None
    return session


def stop_owner_consumer(meta, resources, session, *, grace=2.0, kill_wait=1.0):
    """Stop one open owner's consumer session; nothing is cancelled or settled.

    The same attempt continues through consumer recovery, and only members of
    that session are signalled, so registered payloads keep running.
    """
    deadline = time.monotonic() + grace + kill_wait
    sent = set()
    while True:
        agents, _, _ = _classify_agent_processes(meta, resources)
        targets = [(pid, start) for pid, start in agents if _session_of(pid) == session]
        if not targets:
            # Its summary observer follows the stopped identity; the restored
            # consumer attaches its own.
            pid, start = meta.get("summary_owner_pid", ""), meta.get("summary_owner_pid_start", "")
            if pid.isdigit() and start:
                _signal(int(pid), start, signal.SIGTERM)
            return True
        if time.monotonic() >= deadline:
            return False
        escalated = time.monotonic() >= deadline - kill_wait
        for pid, start in targets:
            if (pid, start, escalated) not in sent:
                _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                sent.add((pid, start, escalated))
        time.sleep(0.05)


def owner_continuation_processes(meta, jobs):
    """Owner execution may end while its registered payloads keep running.

    Whole-attempt quiescence and cleanup retain their meaning. This observation
    grants no resource launch or signal authority and writes no run record.
    """
    proof = DC.attempt_process_quiescence(meta, terminal_receipt=True)
    if (meta.get("worker_type") != "owner" or jobs is None
            or proof.state != "live" or proof.reason != "attempt-descendant-live"):
        return proof, []
    governed = DC.attempt_governed_process_quiescence(meta)
    if governed.state != "quiescent":
        return governed, []
    if not DC.attempt_scan_namespace_authority(meta):
        return DC.ProcessQuiescence("unverifiable", "observer-namespace-mismatch"), []
    if DC.terminal_receipt_incomplete(meta):
        return DC.ProcessQuiescence("unverifiable", "post-exit-receipt-incomplete"), []
    resources = owner_resources(meta, jobs)
    if resources is None:
        # Without resource evidence, retain the already observed live residue.
        # Failed classification cannot grant continuation or erase that positive.
        return proof, []
    agents, observed, preserved = _classify_agent_processes(meta, resources)
    if agents:
        return DC.ProcessQuiescence("live", "attempt-descendant-live", agents[0][0]), []
    if not observed:
        return DC.ProcessQuiescence("unverifiable", "owner-process-unverifiable"), []
    remaining = DC.attempt_tagged_descendants(meta, preserved=set(preserved))
    if remaining.state == "populated":
        return DC.ProcessQuiescence("live", "attempt-descendant-live", remaining.members[0][0]), []
    if remaining.state != "empty":
        return DC.ProcessQuiescence("unverifiable", "attempt-descendant-unverifiable"), []
    # Locations are recovery context, not another admission condition. Only
    # records with actually preserved positive identities appear in this list.
    records = {}
    for resource in preserved.values():
        pointer = {key: resource[key] for key in ("kind", "run_id", "registry", "record") if key in resource}
        records[(pointer["kind"], pointer["run_id"])] = pointer
    return DC.ProcessQuiescence("quiescent", "registered-resources-preserved"), [
        records[key] for key in sorted(records)]


def _signal(pid, start, signum):
    # Adjacent exact checks; no killpg that could include a preserved run.
    for _ in range(2):
        visibility, actual, state = DC.process_observation(pid)
        if visibility == "missing" or (visibility == "present" and (actual != start or state == "Z")):
            return
        if visibility != "present":
            return
    try:
        os.kill(pid, signum)
    except OSError:
        pass


def _non_resource_targets(meta, identity, candidates):
    targets = []
    for pid, start in candidates:
        if not start.isdigit() or int(start) < int(identity.expected_start):
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if (f"{DC.ATTEMPT_DESCENDANT_ENV}={meta['attempt_id']}".encode() not in env
                or any(item.startswith((b"HEARTING_RESOURCE_RUN_ID=", b"HEARTING_RESOURCE_REGISTRY=",
                                        b"HEARTING_COMPUTE_RUN_ID=")) for item in env)):
            continue
        targets.append((pid, start))
    return targets


def reap_terminal_descendants(jobs, fields, *, grace=0.3, kill_wait=0.5):
    """Drain exact post-exit leftovers, retaining the parent-close resource policy.

    This is shared by the post-exit watcher and terminal reconcile. A result
    alone cannot stop a live leader, and a tag never grants group-wide signals.
    """
    from codex_dispatch_terminal import terminal_envelope_observed

    meta = DC.parse_registry_metadata(fields[5])
    if not DC.attempt_scan_namespace_authority(meta):
        return
    identities = DC.authoritative_process_identities(meta)
    if not identities:
        return
    identity = identities[0]
    visible, birth, state = DC.process_observation(identity.pid)
    if visible == "inaccessible" or (visible == "present" and birth == identity.expected_start and state != "Z"):
        return
    route = {"route_id": meta.get("owner_route_id") or meta.get("route_id") or meta["attempt_id"],
             "route_hash": meta.get("owner_route_hash") or meta.get("route_hash", "")}
    path = Path(meta.get("owner_route_file") or meta.get("route_file") or fields[3])
    resources = known_resources(route, path, jobs, {meta["attempt_id"]})
    deadline, sent = time.monotonic() + grace + kill_wait, set()
    while True:
        current = _rows(jobs, {meta["attempt_id"]}).get(meta["attempt_id"])
        if current is None or current[0] != fields:
            return  # A successor/result writer changed the selected row.
        if fields[1] not in {"done", "killed", "cancelled"} and not terminal_envelope_observed(meta.get("log_file")):
            return
        visible, birth, state = DC.process_observation(identity.pid)
        if visible == "inaccessible" or (visible == "present" and birth == identity.expected_start and state != "Z"):
            return
        tagged = DC.attempt_tagged_descendants(meta)
        if tagged.state != "populated":
            return
        # Reuse the complete parent-close decision, including uncertain
        # resource namespaces and branches born during this drain pass.
        agents, observed = _agent_processes(meta, resources)
        if not observed:
            return
        owned = set(agents)
        targets = _non_resource_targets(meta, identity, [
            (pid, start) for pid, start, state in tagged.members
            if state != "Z" and pid != identity.pid and (pid, start) in owned])
        if not targets:
            return
        escalated = time.monotonic() >= deadline - kill_wait
        for pid, start in targets:
            if escalated or (pid, start) not in sent:
                _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                sent.add((pid, start))
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def stop_failed_launch(jobs, fields, *, grace=0.3, kill_wait=0.5):
    """Stop only the exact launch whose failure the wrapper just committed.

    Preserve resource branches and keep their governor alive. The ordinary
    post-exit resolver remains responsible for descendant receipts.
    """
    import resource_run_registry as RR
    meta = DC.parse_registry_metadata(fields[5])
    identities = DC.authoritative_process_identities(meta)
    if fields[1] != "done" or not identities or not DC.attempt_scan_namespace_authority(meta):
        return
    identity = identities[0]
    route = {"route_id": meta.get("owner_route_id") or meta.get("route_id") or meta["attempt_id"],
             "route_hash": meta.get("owner_route_hash") or meta.get("route_hash", "")}
    path = Path(meta.get("owner_route_file") or meta.get("route_file") or fields[3])
    deadline, sent = time.monotonic() + grace + kill_wait, set()
    while True:
        current = _rows(jobs, {meta["attempt_id"]}).get(meta["attempt_id"])
        if (current is None or current[0] != fields or not DC.attempt_scan_namespace_authority(meta)
                or DC.exact_process_group_signal_authority(identity.pid, identity.expected_start) != "authoritative"):
            return
        # Reuse the registry's existing typed diagnostics. An incomplete
        # inventory is not permission to turn a hidden resource into an agent.
        _, diagnostics = RR.scan()
        if any(item.get("blocking", True) for item in diagnostics):
            return "cleanup-resource-unverifiable"
        resources = known_resources(route, path, jobs, {meta["attempt_id"]})
        group = DC.process_group_observation(identity.pid)
        agents, observed = _agent_processes(meta, resources)
        if not observed or group.state == "unverifiable":
            return
        targets = _non_resource_targets(meta, identity, agents)
        if targets is None:
            return
        descendants = [pair for pair in targets if pair[0] != identity.pid]
        group_pairs = {(p, s) for p, s, state in group.members if state != "Z"}
        # Stop descendants first so the governor can reap/release normally.
        # A protected or unowned group member keeps its witness holder alive.
        targets = descendants or (targets if group_pairs <= set(targets) else [])
        if not targets:
            return
        escalated = time.monotonic() >= deadline - kill_wait
        for pid, start in targets:
            current = _rows(jobs, {meta["attempt_id"]}).get(meta["attempt_id"])
            if (current is None or current[0] != fields or not DC.attempt_scan_namespace_authority(meta)
                    or DC.exact_process_group_signal_authority(identity.pid, identity.expected_start) != "authoritative"):
                return
            if escalated or (pid, start) not in sent:
                _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                sent.add((pid, start))
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _saved_branches(value, ledger):
    branches = {rid: {tuple(pair) for pair in pairs}
                for rid, pairs in value.get("resource_branches", {}).items()}
    for event in ledger.journal():
        if event.get("route_hash") != value["route"]["route_hash"]:
            continue
        for rid, pairs in (event.get("evidence") or {}).get("parent_close_resource_branches", {}).items():
            branches.setdefault(rid, set()).update(tuple(pair) for pair in pairs)
    return branches


def _resource_liveness(run):
    import resource_run_registry as RR
    if not _namespace_authoritative(run):
        return "termination-pending"
    live, _, reason = RR.classify_identity(run)
    if live in {"stale", "reaping"}:
        try:
            pid, start = int(run["pid"]), str(run["starttime"])
        except (KeyError, ValueError, TypeError):
            return "termination-pending"
        visible, actual, state = DC.process_observation(pid)
        if visible == "missing" or (visible == "present" and (actual != start or state == "Z")):
            return "exited"
        return "termination-pending"
    return live


def _save_branches(value, ledger, branches):
    with ledger.lock():
        prior = _saved_branches(value, ledger)
        merged = {rid: prior.get(rid, set()) | pairs for rid, pairs in branches.items()}
        if any(merged[rid] != prior.get(rid, set()) for rid in merged):
            ledger._append({"at": WS.now_iso(), "route_id": value["route"]["route_id"],
                "route_hash": value["route"]["route_hash"], "actor": "parent-close",
                "evidence": {"parent_close_resource_branches":
                    {rid: sorted(pairs) for rid, pairs in merged.items()}}})
    return merged


def _stop_resources(value, resources, protected_resources, ledger):
    import resource_run_registry as RR
    target_ids = {(r["kind"], r["run_id"], r.get("registry")) for r in resources}
    foreign = _protected([r for r in protected_resources
        if (r["kind"], r["run_id"], r.get("registry")) not in target_ids])
    saved = _saved_branches(value, ledger)
    pending = set()
    for resource in resources:
        rid = resource["run_id"]
        if resource["kind"] == "compute":
            if resource["state"] != "running":
                continue
            try:
                subprocess.run([sys.executable, str(Path(__file__).with_name("compute-hosts.py")),
                    "stop", rid], capture_output=True, text=True, timeout=30,
                    env={**os.environ, "COMPUTE_HOSTS_CONFIG": resource["config"]})
            except (OSError, subprocess.TimeoutExpired):
                pending.add(rid)
            continue
        run = resource["row"]
        live = _resource_liveness(run)
        if live == "termination-pending":
            pending.add(rid)
            continue
        branch, observed = _branch(run, saved.get(rid, ()))
        branch -= foreign
        saved = _save_branches(value, ledger, {**saved, rid: saved.get(rid, set()) | branch})
        if not observed:
            pending.add(rid)
            continue
        # Reuse existing stop only when its group is entirely the selected
        # branch. Shared groups use the same exact TERM cleanup locally.
        pid, start = int(run["pid"]), str(run["starttime"])
        group = DC.process_group_observation(pid)
        group_pairs = {(p, s) for p, s, state in group.members if state != "Z"}
        if (live == "working" and group.state != "unverifiable" and group_pairs <= branch
                and DC.exact_process_group_signal_authority(pid, start) == "authoritative"):
            try:
                subprocess.run([sys.executable, str(Path(__file__).with_name("resource-runner.py")),
                    "--registry", resource["registry"], "stop", "--run-id", rid],
                    capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
        deadline = time.monotonic() + 0.8
        term_sent = set()
        while True:
            branch, observed = _branch(run, saved.get(rid, ()))
            branch -= foreign
            saved = _save_branches(value, ledger, {**saved, rid: saved.get(rid, set()) | branch})
            if not observed:
                pending.add(rid)
                break
            if not branch:
                break
            escalated = time.monotonic() >= deadline - 0.5
            for member, birth in sorted(branch, reverse=True):
                if escalated or (member, birth) not in term_sent:
                    _signal(member, birth, signal.SIGKILL if escalated else signal.SIGTERM)
                    term_sent.add((member, birth))
            if time.monotonic() >= deadline:
                pending.add(rid)
                break
            time.sleep(0.02)
    return pending


def continue_close(value, *, jobs=None, grace=0.3, kill_wait=0.5):
    """Same controller for close, join and post-exit recovery; bounded per pass."""
    jobs = jobs_path(jobs)
    route, path = value["route"], Path(value["route_file"])
    ledger = WS.WorkflowLedger(route["route_id"], route["route_hash"], jobs=jobs)
    prior = settled_result(route, ledger)
    if prior:
        _finish_existing_cycle(route, jobs=jobs)
        return prior
    resources = _resources_for_close(value, jobs)
    protected = {(r["kind"], r["run_id"], r.get("registry")): r
                 for r in value.get("protected_resources", [])}
    protected.update({(r["kind"], r["run_id"], r.get("registry")): r
        for r in known_resources(route, path, jobs, set(value["attempts"]))})
    protected.update({(r["kind"], r["run_id"], r.get("registry")):
        {**r, "parent_close_linked": True} for r in resources})
    protected_resources = list(protected.values())
    rows = _rows(jobs, set(value["attempts"]))
    pending = []
    # Reconstruct annotations after a crash between durable intent and registry.
    for aid in value["attempts"]:
        if aid not in rows:
            continue
        fields, meta = rows[aid]
        DC.validate_attempt_metadata(meta)
        if fields[1] not in OPEN and meta.get("parent_close_settled") == "1":
            continue
        if fields[1] in OPEN and not requested(meta):
            DC.annotate_attempt_row(jobs, aid, {
                "parent_close_requested": "1", "parent_close_route_id": route["route_id"],
                "parent_close_route_hash": route["route_hash"],
                "parent_close_stop_resources": str(int(value["stop_resources"]))})
            meta = _rows(jobs, {aid})[aid][1]
        deadline = time.monotonic() + grace + kill_wait
        sent = set()
        settled = False
        while True:
            try:
                processes, observed = _agent_processes(
                    meta, protected_resources, terminal=fields[1] not in OPEN)
            except OSError:
                processes, observed = [], False
            if not observed:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                continue
            if not processes:
                if fields[1] not in OPEN:
                    # Keep a child's earlier PASS/FAIL while remembering that
                    # its physical cleanup was also observed for this close.
                    DC.annotate_attempt_row(jobs, aid, {"parent_close_settled": "1"})
                    settled = True
                else:
                    never = DC.attempt_row_never_started(fields)
                    settled = DC.close_attempt_row_if(jobs, aid, NOTE,
                        lambda fresh: requested(DC.parse_registry_metadata(fresh[5])) and
                            _agent_processes(DC.parse_registry_metadata(fresh[5]), protected_resources) == ([], True),
                        evidence={"failure_class": "cancelled", "parent_close_settled": "1",
                                  **({"launch_outcome": "never-launched"} if never else {})})
                    if not settled:
                        current = _rows(jobs, {aid}).get(aid)
                        settled = bool(current and current[0][1] not in OPEN
                                       and current[1].get("parent_close_settled") == "1")
                if settled or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
                continue
            escalated = time.monotonic() >= deadline - kill_wait
            for pid, start in processes:
                key = (pid, start, escalated)
                if key not in sent:
                    _signal(pid, start, signal.SIGKILL if escalated else signal.SIGTERM)
                    sent.add(key)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not settled:
            pending.append(aid)
    if not _drain_workflow(value, ledger, protected_resources, grace, kill_wait):
        pending.append("workflow:" + route["route_id"])
    resource_pending = (_stop_resources(value, resources, protected_resources, ledger)
                        if value["stop_resources"] else set())
    resources = _resources_for_close(value, jobs)
    resource_states = []
    import resource_run_registry as RR
    for resource in resources:
        if resource["kind"] == "resource":
            live = RR.classify_identity(resource["row"])[0]
            if value["stop_resources"]:
                live = _resource_liveness(resource["row"])
                if resource["run_id"] in resource_pending:
                    live = "termination-pending"
        else:
            live = resource["state"]
        resource_states.append({"run_id": resource["run_id"], "kind": resource["kind"], "state": live,
                                "preserved": not value["stop_resources"]})
        if value["stop_resources"] and live in {"working", "reaping", "running", "termination-pending"}:
            pending.append("resource:" + resource["run_id"])
    if pending:
        return {"state": "termination-pending", "reason": NOTE, "pending_attempts": pending,
                "resources": resource_states}
    with ledger.lock():
        prior = settled_result(route, ledger)
        if prior:
            return prior
        state = ledger.state()
        for node in route.get("nodes", []):
            previous = (state["nodes"].get(node["id"]) or {}).get("state")
            if previous not in {"STAGE_SUCCEEDED", "CANCELLED", "FAILED_TERMINAL"}:
                ledger.record(node["id"], "CANCELLED", evidence={"reason": NOTE}, actor="parent-close")
        if ledger.state()["workflow_state"] != "CANCELLED":
            ledger.set_workflow_state("CANCELLED", evidence={"reason": NOTE}, actor="parent-close")
        target = path.with_suffix(".outcome.json")
        if target.exists():
            existing = json.loads(target.read_text())
            if existing.get("terminal_gate_proven") is True:
                return existing
            if existing.get("reason") == NOTE:
                outcome = existing
            else:
                outcome = None
        else:
            outcome = None
        outcome = outcome or {"schema_version": 4, "route_id": route["route_id"], "route_hash": route["route_hash"],
                   "route_file": str(path), "cwd": route["cwd"], "capability": route["capability"],
                   "effective_intensity": route["effective_intensity"], "closed_at": WS.now_iso(),
                   "terminal_gate_proven": False, "state": "cancelled", "reason": NOTE,
                   "disposition": "cancelled", "summary": value.get("summary"), "head_commit": value.get("head_commit"),
                   "owner_attempt_id": value["owner_attempt_id"], "resources": resource_states}
        ledger._append({"at": WS.now_iso(), "route_id": route["route_id"],
                        "route_hash": route["route_hash"], "actor": "parent-close",
                        "evidence": {"parent_close_result": outcome}})
        WS._atomic_write(target, json.dumps(outcome, indent=2) + "\n")
    _finish_existing_cycle(route, jobs=jobs)
    return outcome


def _finish_existing_cycle(route, *, jobs=None):
    # This is ordinary producer closure, never a dependency on retaining its
    # files. Absence/deletion leaves the cancellation result fully replayable.
    import artifact_producer as AP
    root = Path(route["artifact_root"])
    try:
        if verified_owner_pin_handoff_close(route, jobs=jobs):
            return
        cycle = AP.route_cycle_for(root, route)
        if cycle and cycle.get("state") == "open":
            AP.finalize(root, cycle_id=cycle["cycle_id"], state="abandoned",
                        abandon_reason="operator-decision", lock_timeout=0.5)
    except (OSError, ValueError, AP.ProducerError):
        pass


def verified_owner_pin_handoff_close(route, *, jobs=None):
    """Return durable owner-pin handoff evidence, including its successor when prepared.

    The summary is only a selector. Authority comes from the immutable route,
    its exact close intent/result, the cancelled owner row, and a recorded move
    of the owner pin away from that launch. A missing successor is a valid
    pending handoff because close precedes continuation publication.
    """
    try:
        import route_authority as AUTH
        import route_identity as RI
        import route_lineage as RL

        root = Path(route["artifact_root"]).resolve()
        route_id, route_hash = route.get("route_id"), route.get("route_hash")
        if not isinstance(route_id, str) or not isinstance(route_hash, str):
            return None
        path = RL.canonical_route_path(root, route_id)
        path.lstat()
        if not path.is_file() or path.is_symlink() or path.resolve() != path:
            return None
        sealed = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(sealed, dict) or sealed.get("route_id") != route_id
                or sealed.get("route_hash") != route_hash
                or RI.route_hash(sealed) != route_hash):
            return None

        jobs = jobs_path(jobs)
        ledger = WS.WorkflowLedger(route_id, route_hash, jobs=jobs)
        # One read of the journal answers every journal check below (intent, result,
        # their parent-close entries, workflow state) instead of one read per check.
        journal = ledger.journal()
        close_intent = ledger_intent(sealed, ledger, journal=journal)
        result = settled_result(sealed, ledger, journal=journal)
        if (not isinstance(close_intent, dict) or not isinstance(result, dict)
                or close_intent.get("summary") != "owner-pin-handoff"
                or close_intent.get("route_file") != str(path)
                or close_intent.get("owner_attempt_id") != result.get("owner_attempt_id")
                or close_intent.get("route", {}).get("route_id") != route_id
                or close_intent.get("route", {}).get("route_hash") != route_hash
                or result.get("route_id") != route_id or result.get("route_hash") != route_hash
                or result.get("summary") != "owner-pin-handoff"
                or result.get("state") != "cancelled" or result.get("reason") != NOTE
                or result.get("terminal_gate_proven") is not False):
            return None
        if (not any(entry.get("actor") == "parent-close"
                    and (entry.get("evidence") or {}).get("parent_close") == close_intent
                    for entry in journal)
                or not any(entry.get("actor") == "parent-close"
                    and (entry.get("evidence") or {}).get("parent_close_result") == result
                    for entry in journal)):
            return None
        owner_attempt = close_intent.get("owner_attempt_id")
        attempts = close_intent.get("attempts")
        if (not isinstance(owner_attempt, str) or not isinstance(attempts, list)
                or owner_attempt not in attempts or result.get("owner_attempt_id") != owner_attempt):
            return None

        outcome_path = path.with_suffix(".outcome.json")
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        if (not isinstance(outcome, dict) or outcome.get("route_id") != route_id
                or outcome.get("route_hash") != route_hash
                or outcome.get("summary") != "owner-pin-handoff"
                or outcome.get("state") != "cancelled"
                or outcome.get("owner_attempt_id") != owner_attempt
                or outcome.get("terminal_gate_proven") is not False):
            return None

        rows = _rows(jobs, {owner_attempt})
        owner = rows.get(owner_attempt)
        if owner is None:
            return None
        fields, metadata = owner
        if (metadata.get("worker_type") != "owner" or fields[1] in OPEN
                or metadata.get("failure_class") != "cancelled"
                or metadata.get("parent_close_route_id") != route_id
                or metadata.get("parent_close_route_hash") != route_hash
                or metadata.get("parent_close_settled") != "1"
                or metadata.get("owner_route_id") != route_id
                or metadata.get("owner_route_hash") != route_hash
                or Path(metadata.get("owner_route_file", "")).resolve() != path
                or metadata.get("harness") not in AUTH.PIN_HARNESSES):
            return None

        workflow = ledger.read_only_state(entries=journal)
        if workflow.get("journal_unreadable") or workflow.get("workflow_state") != "CANCELLED":
            return None

        launch_harness = metadata["harness"]
        successor_path_value = result.get("successor_route")
        successor = None
        target_harness = AUTH.sealed_pin_harness(sealed, worker_type="owner")
        if successor_path_value:
            successor_path = Path(successor_path_value)
            if successor_path.resolve() != RL.canonical_route_path(root, successor_path.stem):
                return None
            successor_path.lstat()
            if (not successor_path.is_file() or successor_path.is_symlink()
                    or successor_path.resolve() != successor_path):
                return None
            successor = json.loads(successor_path.read_text(encoding="utf-8"))
            successor_pins = successor.get("selection_pins") if isinstance(successor, dict) else None
            sealed_owner_pin = (successor_pins or {}).get("owner") if isinstance(successor_pins, dict) else None
            target_harness = (sealed_owner_pin or {}).get("harness") if isinstance(sealed_owner_pin, dict) else None
            if (not isinstance(successor, dict)
                    or successor.get("route_id") != successor_path.stem
                    or RI.route_hash(successor) != successor.get("route_hash")
                    or successor.get("source_route_id") != route_id
                    or successor.get("source_route_hash") != route_hash
                    or target_harness not in AUTH.PIN_HARNESSES
                    or successor.get("reason") != f"owner-pin-handoff:{owner_attempt}:{target_harness}"
                    or successor.get("continuation_contract_version") != 1):
                return None
            lineage = RL.verified_route_lineage(successor, artifact_root=root, observe=True)
            if len(lineage) < 2 or lineage[1].get("route_id") != route_id:
                return None
            recorded_pins = result.get("handoff_pins")
            if (not isinstance(recorded_pins, dict)
                    or (recorded_pins.get("owner") or {}).get("harness") != target_harness
                    or result.get("successor_route") != str(successor_path)):
                return None

        changes = [row for row in AUTH.pin_changes(sealed)
                   if row.get("target") == "owner"
                   and (row.get("previous") or {}).get("harness") in (None, launch_harness)
                   and (row.get("pin") or {}).get("harness") == target_harness
                   and target_harness != launch_harness]
        close_at = datetime.fromisoformat(str(close_intent.get("at", "")).replace("Z", "+00:00"))
        # The writer proved its authority at record time
        # (`capability-route._change_pins`); a later seat handover must not
        # re-judge a pin the route's parent already recorded.
        changes = [row for row in changes
                   if isinstance(row.get("by"), dict)
                   and row["by"].get("harness") in AUTH.PIN_HARNESSES
                   and datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00")) <= close_at]
        if not target_harness or not changes:
            return None
        return {"route": sealed, "route_file": str(path), "owner_attempt_id": owner_attempt,
                "launch_harness": launch_harness, "target_harness": target_harness,
                "successor": successor, "successor_route": str(successor_path_value or "")}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        return None


def pin_handoff_holder(route_file, route_id, route_hash, node, run_id, *, kind="resource", jobs=None):
    """The route that now holds a run recorded under one route.

    Identity unless that route was closed by a verified owner-pin handoff whose
    result preserved this run and whose successor resumes this node; then the
    successor holds it, hop by hop to the end of the lineage.

    ``observed`` separates "nothing moved" from "could not look": an unclosed
    route, a close that is not an owner-pin handoff, a run that was not
    preserved, an unproven handoff, or a successor that resumes elsewhere is an
    observed identity. An unreadable or mismatched route file or outcome, a
    cycle, or any other failure returns the last proven address with
    ``observed: False`` and ``unobserved_reason``; callers must not read that as
    "no transfer".
    """
    if route_file and (not route_id or not route_hash):
        # A legacy registry row may omit its own route id. The sealed file it
        # names is the same address, so read that identity instead of guessing.
        try:
            sealed_identity = json.loads(Path(route_file).read_text(encoding="utf-8"))
            route_id = route_id or sealed_identity.get("route_id")
            route_hash = route_hash or sealed_identity.get("route_hash")
        except (OSError, ValueError, TypeError, AttributeError) as error:
            origin = {"route_file": route_file, "route_id": route_id, "route_hash": route_hash}
            return {**origin, "inherited": False, "origin": origin, "chain": [], "observed": False,
                    "unobserved_reason": "route-unreadable:" + type(error).__name__}
    origin = {"route_file": route_file, "route_id": route_id, "route_hash": route_hash}
    current = {"route_file": route_file, "route_id": route_id, "route_hash": route_hash,
               "inherited": False, "origin": origin, "chain": [],
               "observed": True, "unobserved_reason": None}
    if not route_file:
        return current

    def unobserved(reason):
        return {**current, "observed": False, "unobserved_reason": reason}

    def preserved(outcome):
        resources = outcome.get("resources")
        return isinstance(resources, list) and any(
            isinstance(item, dict) and item.get("run_id") == run_id
            and item.get("kind") == kind and item.get("preserved") is True
            for item in resources)

    seen = set()
    try:
        while True:
            identity = (current["route_id"], current["route_hash"])
            if not all(identity) or not current["route_file"]:
                return unobserved("route-address-incomplete")
            if identity in seen:
                return unobserved("lineage-cycle")
            seen.add(identity)
            path = Path(current["route_file"])
            try:
                sealed = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError) as error:
                return unobserved("route-unreadable:" + type(error).__name__)
            if (not isinstance(sealed, dict) or sealed.get("route_id") != current["route_id"]
                    or sealed.get("route_hash") != current["route_hash"]):
                return unobserved("route-identity-mismatch")
            try:
                outcome = json.loads(path.with_suffix(".outcome.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                return current  # the route was never closed: nothing moved
            except (OSError, ValueError, UnicodeError) as error:
                return unobserved("outcome-unreadable:" + type(error).__name__)
            if not isinstance(outcome, dict):
                return unobserved("outcome-invalid")
            # Most rows end here: the cheap sidecar says this route kept nothing.
            if outcome.get("summary") != "owner-pin-handoff" or not preserved(outcome):
                return current
            proof = verified_owner_pin_handoff_close(sealed, jobs=jobs)
            successor = (proof or {}).get("successor")
            if not isinstance(successor, dict):
                return current
            nodes = [item.get("id") for item in successor.get("nodes") or [] if isinstance(item, dict)]
            reused = [item.get("node_id") for item in successor.get("reused_nodes") or []
                      if isinstance(item, dict)]
            if successor.get("resume_from_node") != node or node not in nodes or node in reused:
                return current
            successor_file = proof.get("successor_route")
            current = {"route_file": successor_file,
                       "route_id": successor.get("route_id"),
                       "route_hash": successor.get("route_hash"),
                       "inherited": True, "origin": current["origin"],
                       "chain": [*current["chain"], {
                           "route_id": successor.get("route_id"),
                           "route_hash": successor.get("route_hash"),
                           "route_file": successor_file,
                           "owner_attempt_id": proof.get("owner_attempt_id"),
                       }],
                       "observed": True, "unobserved_reason": None}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError) as error:
        return unobserved("holder-unexpected:" + type(error).__name__)


def recover_attempt(jobs, metadata):
    route = {"route_id": metadata.get("parent_close_route_id") or metadata.get("owner_route_id") or metadata.get("route_id"),
             "route_hash": metadata.get("parent_close_route_hash") or metadata.get("owner_route_hash") or metadata.get("route_hash")}
    value = intent(route, jobs) or _current_owner_intent(metadata, jobs)
    return continue_close(value, jobs=jobs) if value else None


def _resources_for_close(value, jobs):
    recorded = {(r["kind"], r["run_id"]): r for r in value.get("resources", [])}
    current = linked_resources(value["route"], Path(value["route_file"]), jobs, set(value["attempts"]))
    recorded.update({(r["kind"], r["run_id"]): r for r in current})
    return list(recorded.values())


def close(route, path, *, jobs=None, stop_resources=False, summary=None, commit=None):
    value = request(route, path, jobs=jobs, stop_resources=stop_resources, summary=summary, commit=commit)
    return continue_close(value, jobs=jobs) if value else None
