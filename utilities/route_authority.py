#!/usr/bin/env python3
"""Route authority: the one place that answers four questions about a route.

1. Who continues it (the parent session and its successor).
2. On which harness (sealed selection pins and the parent's recorded changes).
3. How many attempts of which kind (sub-session standing, retry links,
   round budget, the result envelope).
4. What it may access (execution access grant).

Every judgment here used to be made at its call site, several of them in
more than one copy. The old names stay where they were, as imports or thin
wrappers, so existing callers and patches keep working. Recovery separates
stored work from current selection; consumers do not rebuild historical inputs.

Top-level imports stay light (stdlib and two policy modules) so that
`dispatch_contract`, `model_profile` and the adapters can import this module
without a cycle; heavier collaborators are imported where they are used.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import NamedTuple

from dispatch_attempt_policy import committed_outcome, readable_result
import review_round_cap as _ROUND
from session_identity import identity as session_identity, session_label


def _contract_error(reason, detail=""):
    from dispatch_contract import DispatchContractError
    return DispatchContractError(reason, detail)


# ---------------------------------------------------------------------------
# 1. Who continues the route
# ---------------------------------------------------------------------------

def caller_identity(environ=None) -> tuple[str, str]:
    """Resolve the caller's native identity, independently of the child adapter.

    `session_identity.identity()` reads it; a launch refuses a caller it cannot name.
    """
    found = session_identity(environ)
    if found.confidence == "invalid":
        raise _contract_error("caller-harness-invalid")
    if found.confidence == "ambiguous":
        raise _contract_error("caller-harness-ambiguous")
    return found.harness, found.session_id


def default_parent_session_id(environ=None) -> str | None:
    env = os.environ if environ is None else environ
    # A directly running interactive host is the authority for its own TUI
    # thread. Managed entry used to export a second parent id after observing
    # an App Server sibling, which could override the real Codex thread and
    # strand every completion. Preserve the explicit binding only for nested
    # workers, whose environment marks that dispatch boundary.
    if env.get("AGENT_DISPATCH_CHILD") != "1":
        native_session = caller_identity(env)[1]
        if native_session:
            return native_session
    return env.get("AGENT_DISPATCH_PARENT_SESSION_ID") or caller_identity(env)[1] or None


def default_parent_harness(fallback: str, environ=None) -> str:
    """A selected child's runtime never replaces its caller's identity."""
    env = os.environ if environ is None else environ
    return caller_identity(env)[0] or env.get("AGENT_DISPATCH_OWNER_HARNESS") or fallback


def bind_runtime_parent(args, *, honor_force: bool = False, environ=None) -> None:
    """Bind a dispatch-depth-1 job to the actual calling session, on any harness.

    Callers historically supplied a synthetic ``--parent-session-id``; the
    running session overrides it. The caller is read by
    `session_identity.identity()`; an ambiguous or unnamed caller binds
    nothing. Dispatch-depth-2 workers keep their explicit conductor/owner
    envelope. Only the Codex wrapper honors the legacy
    ``CODEX_DISPATCH_PARENT_CURRENT_FORCE`` switch (``honor_force``).
    """
    env = os.environ if environ is None else environ
    force_current = honor_force and env.get("CODEX_DISPATCH_PARENT_CURRENT_FORCE") == "1"
    caller = session_identity(env)
    if args.dispatch_depth == 1:
        if caller.known and caller.session_id:
            args.parent_session_id = caller.session_id
            args.parent_harness = caller.harness
            args.parent_slug = None
        elif force_current:
            args.parent_slug = None
    elif force_current and caller.harness == "codex" and caller.session_id:
        args.parent_session_id = caller.session_id


def correction_source_session(environ=None) -> str:
    """The session recorded as the sender of an owner correction (a label, not a check)."""
    return session_label(environ)


def owns(meta, session, jobs) -> bool:
    """The launching session, or its confirmed same-seat successor after a /clear (seat handover)."""
    if session and meta.get("parent_sid") == session:
        return True
    if not session or jobs is None:
        return False
    from dispatch_seat_handover import owns as seat_owns
    return seat_owns(meta, session, jobs)


def require_replacement_parent(jobs, rows, meta, *, current_session) -> None:
    """Only the attempt's parent may start its replacement.

    Dispatch depth 2: the caller's own attempt is the registered, live parent
    attempt. Otherwise: the registered parent session or its same-seat
    successor. ``current_session`` is called only on that second path.
    """
    if meta.get('dispatch_depth') == '2':
        import dispatch_contract as DC
        parent = meta.get('parent_attempt_id')
        if not parent or os.environ.get('AGENT_DISPATCH_ATTEMPT_ID') != parent or parent not in rows:
            raise DC.DispatchContractError('replacement-parent-identity-unproven')
        fields, parent_meta = rows[parent]
        if fields[1] not in {'open', 'running'} or not DC._parent_liveness_evidence(Path(jobs), parent_meta)[0]:
            raise DC.DispatchContractError('replacement-parent-not-live')
    elif not meta.get('parent_sid') or not owns(meta, current_session(), jobs):
        raise _contract_error('replacement-parent-identity-unproven')


def replacement_parent_matches(source, candidate, jobs, *, lineage=False) -> bool:
    """Keep the parent attempt exact; a depth-1 session may be its recorded successor."""
    if candidate.get('parent_attempt_id') != source.get('parent_attempt_id'):
        return False
    if candidate.get('parent_sid') == source.get('parent_sid'):
        return True
    if source.get('dispatch_depth') != '1':
        return False
    if owns(source, candidate.get('parent_sid'), jobs):
        return True
    if lineage and candidate.get('dispatch_depth') == '1':
        # A registered A -> B edge survives a later B -> C handover. Admission
        # still accepts only the registered parent or the current successor.
        from dispatch_seat_handover import effective_parent
        current = effective_parent(source, jobs)
        return bool(current) and effective_parent(candidate, jobs) == current
    return False


def lineage_parent_matches(metadata, *, thread_id, parent_attempt_id) -> bool:
    """A replacement lineage row belongs to this receipt's parent: the exact
    registered session at depth 1, the exact parent attempt below it."""
    if metadata.get("dispatch_depth") == "1":
        return metadata.get("parent_sid") == thread_id
    return metadata.get("parent_attempt_id") == parent_attempt_id


LIVE_ROW_STATUSES = frozenset({"open", "running"})


def review_owner_authority(route, jobs, author_attempt_id) -> None:
    """Prove the current registered owner without taking or creating locks."""
    from dispatch_contract import parse_registry_metadata
    from owner_route_binding import resolve_owner_route_lifecycle
    if not author_attempt_id:
        raise ValueError("review-input-revision-owner-required")
    caller = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if caller and caller != author_attempt_id:
        raise ValueError("review-input-revision-owner-caller-mismatch")
    matches = []
    for line in Path(jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            meta = parse_registry_metadata(fields[5])
            if meta.get("attempt_id") == author_attempt_id:
                matches.append((fields, meta))
    if len(matches) != 1:
        raise ValueError("review-input-revision-owner-not-exact")
    fields, meta = matches[0]
    if (fields[1] not in LIVE_ROW_STATUSES or meta.get("worker_type") != "owner"
            or meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"):
        raise ValueError("review-input-revision-owner-invalid")
    binding, _ = resolve_owner_route_lifecycle(jobs, owner_attempt_id=author_attempt_id)
    if binding is None or (binding.route_id, binding.route_hash) != (route["route_id"], route["route_hash"]):
        raise ValueError("review-input-revision-owner-route-mismatch")


# ---------------------------------------------------------------------------
# 2. On which harness
# ---------------------------------------------------------------------------

# `compose --pin owner|frame|worker=<harness>[:<model>[:<effort>]]` seals one
# pin per target in the route; every launch, resume, replacement and stage
# fallback reads it from the route file the launch is bound to.
PIN_TARGETS = ("owner", "frame", "worker")


def selection_pin_rows(route) -> dict:
    """Copied pins in force for a descendant route, without the schema envelope."""
    pins = route_in_force(route).get("selection_pins") if isinstance(route, dict) else None
    return {target: dict(pins[target]) for target in PIN_TARGETS
            if isinstance(pins, dict) and isinstance(pins.get(target), dict)}


def pin_target(worker_type: str | None) -> str:
    return "frame" if worker_type == "frame" else "owner" if worker_type == "owner" else "worker"


def sealed_pin_harness(route, *, worker_type: str | None) -> str | None:
    """The harness pinned for the launch's pin target, or None: the sealed pin,
    or the route parent's later recorded change (`route_in_force`)."""

    pins = route_in_force(route).get("selection_pins") if isinstance(route, dict) else None
    pin = pins.get(pin_target(worker_type)) if isinstance(pins, dict) else None
    return (pin.get("harness") or None) if isinstance(pin, dict) else None


# A sealed route never changes; its parent may later move one of its pins
# (`capability-route.py start --route <file> --pin owner|frame|worker=<harness>[:<model>[@<effort>]]`).
# Each change is one append-only row beside the route: the new pin, the pin it replaced, who
# changed it and when, what the runtime knew about where the instruction came from, and the
# checked launch tuples the runtime probed for the new pin at that moment. Launch decisions
# read the sealed route through `route_in_force`, after its hash is verified; an attempt that
# already launched keeps what it launched with.
PIN_CHANGE_TARGETS = ("owner", "frame", "worker")
PIN_CHANGE_SCHEMA = 1


def pin_changes_path(route) -> Path | None:
    """`<artifact_root>/.runtime/routes/<route_id>.pin-changes.jsonl`, beside the route file."""
    root = route.get("artifact_root") if isinstance(route, dict) else None
    route_id = route.get("route_id") if isinstance(route, dict) else None
    if not isinstance(root, str) or not root or not isinstance(route_id, str) \
            or not re.fullmatch(r"rt-[A-Za-z0-9-]{1,64}", route_id):
        return None
    return Path(root).resolve() / ".runtime" / "routes" / f"{route_id}.pin-changes.jsonl"


def pin_changes(route) -> list[dict]:
    """This route's recorded pin changes, oldest first; rows for another route or hash are ignored."""
    path = pin_changes_path(route)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        pin = row.get("pin") if isinstance(row, dict) else None
        if (isinstance(row, dict) and row.get("schema") == PIN_CHANGE_SCHEMA
                and row.get("route_id") == route.get("route_id")
                and row.get("route_hash") == route.get("route_hash")
                and row.get("target") in PIN_CHANGE_TARGETS
                and isinstance(pin, dict) and pin.get("harness") in PIN_HARNESSES):
            rows.append(row)
    return rows


PIN_HARNESSES = ("claude", "codex", "opencode")


def _tuple_key(row: dict) -> tuple:
    return tuple(row.get(key) for key in (
        "parent_harness", "parent_transport", "parent_sandbox", "child_harness", "launch_authority"))


def route_in_force(route):
    """The sealed route with its recorded pin changes applied: the pin in force, and the
    checked tuples probed for each new pin added to the route's dispatch evidence,
    each depth-2 node's same/cross-harness hops and the registered-headless candidates.

    The sealed route is returned unchanged when nothing was recorded; otherwise a copy is
    returned, so the caller's sealed dict (and its hash) is never touched."""

    changes = pin_changes(route) if isinstance(route, dict) else []
    if not changes:
        return route
    view = json.loads(json.dumps(route))
    pins = dict(view.get("selection_pins") or {"contract_version": 1})
    evidence = view.get("dispatch_evidence") if isinstance(view.get("dispatch_evidence"), dict) else None
    tuples = evidence.setdefault("tuples", []) if evidence is not None else None
    candidates = view.get("registered_headless_candidates")
    for change in changes:
        pins[change["target"]] = dict(change["pin"])
        for row in change.get("tuples") or []:
            if not isinstance(row, dict) or tuples is None:
                continue
            if all(_tuple_key(row) != _tuple_key(old) for old in tuples):
                tuples.append(row)
            if row.get("launch_authority") != "conductor":
                continue
            ordinal = 1 if row.get("child_harness") == row.get("parent_harness") else 2
            for node in view.get("nodes") or []:
                for hop in node.get("fallback_hops") or []:
                    if hop.get("ordinal") == ordinal and isinstance(hop.get("candidates"), list) \
                            and all(_tuple_key(row) != _tuple_key(old) for old in hop["candidates"]):
                        hop["candidates"].append(dict(row))
        for row in change.get("candidates") or []:
            if isinstance(candidates, list) and isinstance(row, dict) and row not in candidates:
                candidates.append(row)
    view["selection_pins"] = pins
    return view


def changed_pin_harness(route, target: str) -> str | None:
    """The harness the route's parent last moved `target` to, or None when it never moved it."""
    changes = [row for row in pin_changes(route) if row.get("target") == target]
    return changes[-1]["pin"]["harness"] if changes else None


def moved_owner_harness(route, launched_harness: str | None) -> str | None:
    """The owner harness the route's parent moved to after an owner launched on
    `launched_harness`, or None. This is a historical observation; replacement launch
    selection reads all current pins through `route_in_force`."""
    harness = changed_pin_harness(route, "owner")
    return harness if harness and harness != launched_harness else None


def record_pin_change(route, *, target: str, pin: dict, by: dict, source: str,
                      tuples: list, candidates: list, now: float | None = None) -> dict | None:
    """Append one pin change for this route; None when the pin in force is already `pin`."""
    import fcntl
    import time
    if target not in PIN_CHANGE_TARGETS or pin.get("harness") not in PIN_HARNESSES:
        raise ValueError(f"pin-change-target-unsupported:{target}")
    path = pin_changes_path(route)
    if path is None:
        raise ValueError("pin-change-route-unlocated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = (route_in_force(route).get("selection_pins") or {}).get(target)
        if isinstance(previous, dict) and {k: previous.get(k) for k in ("harness", "model", "effort")} \
                == {k: pin.get(k) for k in ("harness", "model", "effort")}:
            return None
        row = {"schema": PIN_CHANGE_SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
               "target": target, "pin": {k: pin.get(k) for k in ("harness", "model", "effort")},
               "previous": previous, "by": dict(by), "source": source,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "tuples": list(tuples), "candidates": list(candidates)}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row


# The route's parent may also hand its next owner another execution access request: the
# existing `AGENT_DISPATCH_EXECUTION_ACCESS_FILE` when it runs `start` or `correct`. Each one is
# an `access` row in the same append-only record: the validated request, its digest, the digest
# it replaced, who gave it, and what the runtime knew about where it came from. A node's first
# preparation that derived roots from the approved task adds one row with source `derived`.
# The latest row is the request in force (`access_in_force`); the pin readers above skip these.
ACCESS_CHANGE_TARGET = "access"


def access_changes(route) -> list[dict]:
    """This route's recorded access requests, oldest first; rows for another route or hash are ignored."""
    path = pin_changes_path(route)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("schema") == PIN_CHANGE_SCHEMA
                and row.get("target") == ACCESS_CHANGE_TARGET
                and row.get("route_id") == route.get("route_id")
                and row.get("route_hash") == route.get("route_hash")
                and isinstance(row.get("request"), dict) and isinstance(row.get("source"), str)
                and row.get("request_sha256") == request_digest(row["request"])):
            rows.append(row)
    return rows


def request_digest(request: dict) -> str:
    """The digest of a normalized execution access request (`execution_access.normalized_request`)."""
    import hashlib
    return hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def access_in_force(route) -> dict | None:
    """The latest access row of this route, or None while its first prepared request stands."""
    rows = access_changes(route) if isinstance(route, dict) else []
    return rows[-1] if rows else None


def record_access_change(route, *, request: dict, request_sha256: str, by: dict, source: str,
                         now: float | None = None) -> dict | None:
    """Append one access request for this route; None when the request in force is already it."""
    import fcntl
    import time
    path = pin_changes_path(route)
    if path is None:
        raise ValueError("access-change-route-unlocated")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = access_in_force(route)
        if previous is not None and previous["request_sha256"] == request_sha256:
            return None
        row = {"schema": PIN_CHANGE_SCHEMA, "route_id": route["route_id"], "route_hash": route["route_hash"],
               "target": ACCESS_CHANGE_TARGET, "request": request, "request_sha256": request_sha256,
               "previous_sha256": (previous or {}).get("request_sha256"), "by": dict(by), "source": source,
               "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return row


def pinned_launch_harness(route, *, worker_type: str | None, requested: str | None, available) -> tuple[str | None, str | None]:
    """A sealed pin beats the requested harness while the pinned one is available.

    Returns `(harness, overridden_request)`; the second item is the request the pin replaced
    (None when nothing was replaced), so the caller can record it.  An unavailable pin, or no
    pin, leaves the request alone.  `available` is the caller's own hard-eligibility test.
    """

    pinned = sealed_pin_harness(route, worker_type=worker_type)
    if not pinned or (pinned != requested and not available(pinned)):
        return requested, None
    return pinned, (requested if requested not in (None, pinned) else None)


REPLACEMENT_FIXED_KEYS = ("harness", "jobs", "worktree")

# The task and scope survive recovery. Parent/owner/pin/model are current
# execution choices; the ordinary launcher validates their realization.
RECOVERY_SCOPE_KEYS = (
    "route_node", "worker_type", "dispatch_depth", "session_chain_id",
    "subsession_id", "subsession_index", "subsession_count", "subsession_mode",
    "stage_authority", "fixed_inputs_sha256", "narrow_verify_sha256", "phase_brief_sha256",
)
RECOVERY_SELECTION_OPTIONS = frozenset({
    "--parent-session-id", "--parent-harness", "--parent-completion-delivery",
    "--route-file", "--route-id", "--route-hash", "--owner-route-file",
    "--owner-route-id", "--owner-route-hash", "--model", "--reasoning",
    "--model-profile", "--execution-access-file", "--reviewed-evidence",
})
RECOVERY_SELECTION_VALUES = frozenset({
    "parent_completion_delivery",
    "resolved_completion_delivery", "model", "reasoning", "resolved_model_settings",
    "execution_surface", "fallback_hop", "model_role",
})


def recovery_task(record, source, history):
    """Read the recorded work; a moved owner receives its bound continuation request."""
    if (source.get("worker_type") == "owner"
            and (source.get("owner_route_id") or source.get("route_id")) != record["route_id"]):
        route = json.loads(Path(record["route_file"]).read_text(encoding="utf-8"))
        if route.get("route_hash") != record["route_hash"]:
            raise _contract_error("replacement-route-drift")
        text = (route.get("work_request") or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise _contract_error("replacement-current-route-input-unproven")
        return text
    return history["task"]


def require_recovery_binding(record, source, candidate, jobs):
    """The same lineage check at task loading, registration and actual launch.

    The caller has already read and digest-checked the claim and original input.
    The record's current route was resolved through the registered owner lineage,
    rather than through candidate-supplied ancestor IDs.
    """
    route_key = "owner_route_id" if source.get("owner_route_id") else "route_id"
    hash_key = "owner_route_hash" if source.get("owner_route_id") else "route_hash"
    if (candidate.get(route_key) != record["route_id"]
            or candidate.get(hash_key) != record["route_hash"]
            or not replacement_parent_matches(source, candidate, jobs)
            or (source.get("dispatch_depth") != "1" and candidate.get("parent") != source.get("parent"))
            or any(candidate.get(key) != source.get(key) for key in RECOVERY_SCOPE_KEYS)):
        raise _contract_error("replacement-launch-binding-mismatch", source.get("attempt_id", ""))
    if source.get("session_chain_id"):
        from dispatch_replacement_subsession import SCOPE_KEYS
        if any(candidate.get(key) != source.get(key) for key in SCOPE_KEYS):
            raise _contract_error("replacement-subsession-scope-mismatch")
    if source.get("review_input_digest") or candidate.get("review_input_digest"):
        from review_input import read_binding
        original = read_binding(jobs, source, verify_current=True)
        successor = read_binding(jobs, candidate, verify_current=True)
        if (any(successor.get(key) != original.get(key) for key in ("path", "sha256", "producer"))
                or successor.get("source") != {
                    "attempt_id": source["attempt_id"], "binding_digest": source["review_input_digest"]}):
            raise _contract_error("reviewed-evidence-replacement-mismatch")


def recovery_node_task(record, source, history, *, attempt_id, route, node, harness, parent, jobs):
    """A node loader consumes the same claim binding as admission, before sealing its input."""
    if record.get("replacement_attempt_id") != attempt_id:
        raise _contract_error("replacement-launch-binding-mismatch")
    candidate = dict(source, attempt_id=attempt_id, route_node=node["id"], parent=parent)
    route_key = "owner_route_id" if source.get("owner_route_id") else "route_id"
    hash_key = "owner_route_hash" if source.get("owner_route_id") else "route_hash"
    candidate.update({route_key: route["route_id"], hash_key: route["route_hash"]})
    if source.get("parent") != parent:
        candidate["parent_sid"] = default_parent_session_id()
    require_recovery_binding(record, source, candidate, jobs)
    selected = sealed_pin_harness(route, worker_type=source.get("worker_type")) or source.get("harness")
    if harness != selected:
        raise _contract_error("replacement-launch-binding-mismatch")
    return recovery_task(record, source, history)


def _work_options(argv):
    """Read stored semantic options, without synthesizing a new original argv."""
    values = []; index = 0
    while index < len(argv):
        token = argv[index]; key = token.split("=", 1)[0]
        group = token.split("=", 1) if "=" in token else [token]
        if "=" not in token and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            index += 1; group.append(argv[index])
        if key not in RECOVERY_SELECTION_OPTIONS:
            values.append(group)
        index += 1
    return sorted(values, key=lambda value: value[0])


def require_recovery_work(candidate, history, *, route=None, worker_type=None,
                          transition=None, access=None, parent_values=None):
    """Bind the candidate to stored work and grants, while selection follows current authority.

    A route-less call is the compatibility check for an old standalone launch;
    with a verified current route, its normal launcher owns model realization.
    """
    owner = worker_type == "owner"
    pinned = sealed_pin_harness(route, worker_type=worker_type) if route else None
    keys = ("jobs", "worktree") if owner or pinned else REPLACEMENT_FIXED_KEYS
    for key in keys:
        if candidate.get(key) != history.get(key):
            raise _contract_error("replacement-input-tuple-mismatch", key)
    if pinned and candidate.get("harness") != pinned:
        raise _contract_error("pin-ignored-for-replacement", pinned)
    drift = candidate.get("launch_home") != history.get("launch_home")
    old, new = history.get("resolved") or {}, candidate.get("resolved") or {}
    if transition and new.get("model_profile") != transition["to"]:
        raise _contract_error("replacement-input-tuple-mismatch", "resolved")
    ignored = RECOVERY_SELECTION_VALUES if route else frozenset()
    if owner and route and candidate.get("harness") != history.get("harness"):
        ignored = ignored | {"sandbox", "permission_mode", "parent_sandbox", "parent_transport"}
    for key in sorted(set(old) | set(new)):
        expected = (parent_values or {}).get(key, old.get(key))
        if expected == new.get(key) or key in ignored:
            continue
        if (transition and key in {"model_profile", "model", "reasoning", "resolved_model_settings"}
                and old.get("model_profile") == transition["from"]
                and new.get("model_profile") == transition["to"]
                and (key != "resolved_model_settings" or (new.get(key) or {}).get("profile") == transition["to"])):
            continue
        if not (drift and key in RELEASE_DERIVED_VALUES):
            raise _contract_error("replacement-input-tuple-mismatch", "resolved")
    # Sealing includes the raw argv as history. Check what the candidate will
    # actually receive against its own checked current binding, rather than
    # reconstructing the original command with today's values.
    options = {}
    argv = candidate.get("argv", []); index = 0
    while index < len(argv):
        token = argv[index]; key = token.split("=", 1)[0]
        value = token.split("=", 1)[1] if "=" in token else None
        if value is None and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            index += 1; value = argv[index]
        options.setdefault(key, []).append(value); index += 1
    current = {"--" + key.replace("_", "-"): value for key, value in new.items()}
    if route:
        current.update({"--route-id": route["route_id"], "--route-hash": route["route_hash"]})
    for flag, expected in current.items():
        if flag in RECOVERY_SELECTION_OPTIONS and flag in options and options[flag] != [str(expected)]:
            raise _contract_error("replacement-argv-mismatch", flag)
    granted = granted_permissions(candidate.get("applied_permissions"), candidate.get("launch_home"), candidate.get("worktree"))
    sealed = granted_permissions(history.get("applied_permissions"), history.get("launch_home"), history.get("worktree"))
    cross_owner = owner and candidate.get("harness") != history.get("harness")
    if cross_owner:
        require_recovery_grant_addresses(candidate, history, route=route, access=access)
    if access:
        if (granted.get("execution_access") or {}).get("request_sha256") != access["request_sha256"]:
            raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
        granted, sealed = _without_recovery_access(granted), _without_recovery_access(sealed)
    # A cross-harness owner goes through the ordinary access-grant launcher.
    # Its runtime permission spelling differs, while the exact approved request
    # and addresses above remain bound. Same-harness grants stay comparable.
    if not cross_owner and granted != sealed:
        if not drift:
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
        from hearting_gates import same_work_or_refuse
        same_work_or_refuse("replacement-runtime-drift", "applied_permissions")
    if not owner and _work_options(candidate.get("argv", [])) != _work_options(history.get("argv", [])):
        raise _contract_error("replacement-argv-mismatch")


def require_recovery_grant_addresses(candidate, history, *, route, access=None):
    """Translate adapter grant spellings to the existing approved task addresses.

    Legacy inputs without an execution-access envelope use the sealed worktree,
    artifact root and the same task-target resolver as the normal launcher.
    Nothing is persisted and no additional input is requested.
    """
    import execution_access as EA
    import fnmatch
    applied = candidate.get("applied_permissions") or {}
    old_permissions = history.get("applied_permissions") or {}
    original = old_permissions.get("execution_access") or {}
    network_allowed = (original.get("network") in {"enforced", "granted-unenforced"}
                       or old_permissions.get("nested_headless_network") is True)
    if candidate.get("harness") == "codex":
        from dispatch_contract import codex_standard_owner_network_enabled
        selected = candidate.get("resolved") or {}
        # This is the normal standard+ owner's existing dispatch support grant.
        # Its intensity/depth are already bound to the approved work above.
        network_allowed = network_allowed or codex_standard_owner_network_enabled(
            dispatch_depth=int(selected.get("dispatch_depth") or 0),
            worker_type=selected.get("worker_type", ""),
            intensity=selected.get("intensity", ""), sandbox=selected.get("sandbox", ""))
    writes = [history.get("worktree"), (route or {}).get("artifact_root"), *original.get("writable_roots", [])]
    reads = [*writes, *original.get("read_roots", [])]
    if route:
        context = EA.AccessContext.build(worktree=history["worktree"], artifact_root=route["artifact_root"],
            dispatch_state_root=Path(history["jobs"]).parent,
            agent_home=candidate.get("launch_home") or Path(__file__).resolve().parents[1])
        # Ordinary launchers grant installed contracts and utilities read
        # visibility with edit denies. Recovery uses the same source addresses.
        reads.extend(contract_read_roots(context.agent_home, route))
        targets = EA.resolve_task_targets(route)
        if targets:
            writes.extend(targets.writable_roots)
        derived = EA.derive_task_access(route, context, writable=tuple(Path(value) for value in writes if value))
        writes.extend(derived.writable_roots); reads.extend(derived.read_roots)
        if access:
            request = EA.load_request(Path(access["request_path"]), context=context)
            if request.request_sha256 != access["request_sha256"]:
                raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
            writes.extend(request.writable_roots); reads.extend(request.read_roots)
            network_allowed = request.network_required
    writes = [Path(value).resolve(strict=False) for value in writes if value]
    reads = [*writes, *(Path(value).resolve(strict=False) for value in reads if value)]

    def require(value, roots, *, relative_to=None):
        value = str(value)
        path = Path(value.removesuffix("/**").removesuffix("/*"))
        if not path.is_absolute() and relative_to is not None:
            path = Path(relative_to) / path
        path = path.resolve(strict=False)
        if "*" in str(path) or not any(path == root or root in path.parents for root in roots):
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")

    grant = applied.get("execution_access") or {}
    if (not access and original.get("request_sha256")
            and grant.get("request_sha256") != original["request_sha256"]):
        raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
    if (grant.get("network") in {"enforced", "granted-unenforced"}
            or applied.get("nested_headless_network") is True) and not network_allowed:
        raise _contract_error("replacement-input-tuple-mismatch", "execution_access")
    for key in ("writable_roots", "additional_writable_roots", "absorbed_writable_roots"):
        for value in grant.get(key, []):
            require(value, writes)
    for value in grant.get("read_roots", []):
        require(value, reads)
    permissions = applied.get("opencode_permission") or {}
    for category, roots in (("external_directory", reads), ("edit", writes)):
        rules = permissions.get(category) or {}
        if isinstance(rules, str):
            if rules != "deny" and category != "edit":
                raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
            rules = {"*": rules}
        for pattern, effect in rules.items():
            if effect == "allow" and not (category == "edit" and pattern in {"*", "**"}):
                require(pattern, roots, relative_to=history["worktree"] if category == "edit" else None)
        if category == "edit":
            # Native edit defaults apply within the externally scoped roots.
            # Preserve the normal launcher's '*' allow, while requiring its
            # read-only roots to retain their effective edit deny.
            external = permissions.get("external_directory") or {}
            if any(effect == "allow" for effect in rules.values()) and not isinstance(external, dict):
                raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
            for root in reads:
                if any(root == write or write in root.parents for write in writes):
                    continue
                visible = "deny"
                for pattern, candidate_effect in external.items():
                    if fnmatch.fnmatchcase(str(root), pattern):
                        visible = candidate_effect
                if visible != "allow":
                    continue
                for path in (root, root / "__recovery_read_only_child__"):
                    forms = (str(path), os.path.relpath(path, history["worktree"]))
                    effect = "allow"
                    for pattern, candidate_effect in rules.items():
                        if any(fnmatch.fnmatchcase(form, pattern) for form in forms):
                            effect = candidate_effect
                    if effect != "deny":
                        raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
    for rule in (applied.get("claude") or {}).get("allowed_tools") or []:
        match = re.fullmatch(r"(Read|Edit|Write)(?:\(([^)]+)\))?", rule)
        if not match:
            continue
        tool, path = match.groups()
        if not path:
            raise _contract_error("replacement-input-tuple-mismatch", "applied_permissions")
        require(path[1:] if path.startswith("//") else path,
                reads if tool == "Read" else writes, relative_to=history["worktree"])


def _without_recovery_access(permissions):
    result = {key: value for key, value in permissions.items() if key != "execution_access"}
    if isinstance(result.get("opencode_permission"), dict):
        result["opencode_permission"] = {key: value for key, value in result["opencode_permission"].items()
                                         if key not in ("external_directory", "edit")}
    return result


def continuation_attempt_state(metadata):
    """Execution adopts a successor; preparation and an unconsumed claim do not.

    A legacy row lacking launch evidence remains unknown, not an invented
    no-spawn proof. PID-bound or unresolved claimed launches remain attached.
    """
    if metadata.get("launch_started") == "1":
        return "started"
    if metadata.get("pid"):
        return "unknown"
    if metadata.get("launch_outcome") == "never-launched":
        return "unstarted"
    if metadata.get("launch_claimed") == "0" and metadata.get("launch_started") != "1":
        return "unstarted"
    return "unknown"


def resource_predecessor_finished(row):
    """A resource's exact ended execution, independent of its scientific verdict."""
    from resource_run_registry import classify_identity, resource_never_started, reboot_evidence
    from dispatch_resource_wait import supervisor
    if resource_never_started(row):
        return True
    if (row.get("status") == "launching" or row.get("cancel_requested")
            or row.get("parent_close_requested") or classify_identity(row)[0] != "exited"):
        return False
    if (row.get('status') != 'succeeded' and row.get('exit_code') is None
            and reboot_evidence(row)):
        return True
    code = supervisor().runner().read_sentinel(row.get("sentinel"))
    return (code is not None and row.get("exit_code", code) == code
            and not (row.get("status") == "failed" and code == 0)
            and not (row.get("status") == "succeeded" and code != 0))


def require_resource_predecessors(rows, candidate):
    """Recheck execution and evidence-address protection within the existing reservation lock."""
    from dispatch_resource_wait import resource_evidence_paths_conflict
    if any(not resource_predecessor_finished(row) or resource_evidence_paths_conflict(row, candidate)
           for row in rows):
        raise ValueError("resource-route-body-conflict")


# Same work, another launcher. Never-started work may be taken over by a launcher that runs
# somewhere else: inside the parent's OS sandbox (a Codex owner's tool shell) or on the host
# beside it (the session supervisor advancing a serial chain). Where a launcher runs decides
# these realized values -- the wrapper's lifetime scope and, for Codex, whether its own sandbox
# nests inside the parent's -- while the work and the permissions granted to it (argv with
# `--sandbox` and the parent tuple, network, the access grant, Claude/OpenCode permissions)
# stay the same and are still compared.
LAUNCH_LOCATION_VALUES = frozenset({"launch_lifecycle", "runtime_sandbox"})

# The sealed launch input of one attempt: the work and its granted permissions.
RESEAL_STABLE_KEYS = ("schema", "attempt_id", "harness", "jobs", "worktree", "argv", "task", "retry_brief",
                      "route_id", "route_node", "owner_route_id")

# The registry row of one never-started attempt. Per-launch values (lease nonce, release home,
# parent runtime pid, sealed input digest, lifetime scope) may differ.
RELAUNCH_STABLE_KEYS = (
    "attempt_id", "attempt_schema_version", "harness", "worker_type", "dispatch_depth",
    "capability", "parent_sid", "parent_attempt_id", "route_id", "route_hash", "route_node",
    "owner_route_id", "owner_route_hash", "automatic_retry_of",
    "replacement_original_attempt_id", "replacement_family_id", "replacement_claim_digest",
)


# A path into the harness's own tree inside a permission value (`<root>/utilities/...`).
_RELEASE_TREE_PATH = re.compile(r"(/[^\s\"'()*]*?)/(?:core|capabilities|roles|utilities|adapters|docs|hooks|profiles|tools)(?=/|[\"'])")
RELEASE_ROOT_TOKEN = "<launch_home>"


def granted_permissions(applied, launch_home=None, worktree=None) -> dict:
    """``applied_permissions`` without the values the launcher's location decides.

    With ``launch_home`` (the release the launch ran from), a path into the harness's own tree
    reads the same however its root was spelled: that release directory, or, for a managed
    release, another release of the same install or its ``<share>/hearting/current`` pointer
    (which may point at a newer release by now). The spelling depends only on how the launcher
    was invoked, and the release is ``launch_home``'s axis. Any other harness tree, such as a
    development checkout, stays a different path."""
    granted = {key: value for key, value in (applied or {}).items() if key not in LAUNCH_LOCATION_VALUES}
    if not launch_home:
        return granted
    text = json.dumps(granted, sort_keys=True)
    home = os.path.realpath(str(launch_home))
    releases = os.path.dirname(home)
    managed = os.path.basename(releases) == "releases"
    pointer = os.path.join(os.path.dirname(releases), "current")
    roots = {root for root in _RELEASE_TREE_PATH.findall(text)
             if root == str(launch_home) or os.path.realpath(root) == home
             or (managed and (root == pointer or os.path.dirname(os.path.realpath(root)) == releases))}
    # OpenCode edit rules check the same directory relative to the worktree.
    # Normalize only aliases of the identified launch roots, never arbitrary
    # relative task paths or another development checkout.
    if worktree:
        roots.update(os.path.relpath(root, worktree) for root in tuple(roots))
    for root in sorted(roots, key=len, reverse=True):
        text = re.sub(r"(?<![^\s\"'(])" + re.escape(root) + "/", RELEASE_ROOT_TOKEN + "/", text)
    return json.loads(text)


# A sealed fact and the same value computed again now. The release a launch ran from is where it
# ran, not what work it is: its row and sealed input keep the `launch_home` it ran from, a
# continuation records its own, and a different current value on that axis is neither a refusal
# nor a diagnostic. That covers a replacement's `launch_home` and the values the runtime derives
# from its release, and the launch roots of a managed release that moved to another verified
# managed release copy (`release_moved`). The launcher's location is `LAUNCH_LOCATION_VALUES`; the
# process table yields to the drain receipt its watcher sealed
# (`dispatch_contract._denied_process_outside_attempt`). The work, the permissions granted to
# it and the route's own seal are still compared. The portable model profile
# is approved work quality, not a value derived from the release.
RELEASE_DERIVED_VALUES = frozenset({
    "model", "reasoning", "resolved_model_settings", "resolved_completion_delivery",
    "parent_completion_delivery", "execution_surface", "fallback_hop", "model_role"})
RELEASE_LAUNCH_ROOTS = frozenset({"registry_root", "launch_home", "runtime_root", "wrapper_root"})
_RELEASE_IDENTITY_FIELDS = frozenset({"release_id", "content_digest", "binding_digest"})
_MANAGED_RELEASE_ID = re.compile(r"release:[A-Za-z0-9][A-Za-z0-9._-]{0,63}:[0-9a-f]{12}")


def release_moved(sealed: dict, current: dict, *, managed_release) -> frozenset:
    """The sealed launch roots that differ from the current ones only because the installed
    release moved: from one managed release (as sealed) to another verified managed release
    copy (``managed_release(path)``). Each release root must lie in its own side's runtime
    root, and the registry (`jobs_path`) may differ only by the release fields its runtime
    resolved. A runtime home or other projection tree, a development checkout, and a
    malformed root are never a release move."""

    def root(roots, name):
        value = roots.get(name) if isinstance(roots, dict) else None
        if (isinstance(value, dict) and value.get("kind") == name
                and isinstance(value.get("path"), str) and os.path.isabs(value["path"])):
            return value
        return None

    def inside(value, runtime):
        path, top = Path(value["path"]), Path(runtime["path"])
        return path == top or top in path.parents

    old, new = root(sealed, "runtime_root"), root(current, "runtime_root")
    if (old is None or new is None or not _MANAGED_RELEASE_ID.fullmatch(str(old.get("release_id") or ""))
            or not managed_release(new["path"])):
        return frozenset()
    moved = {name for name in RELEASE_LAUNCH_ROOTS
             if root(sealed, name) is not None and root(current, name) is not None
             and inside(root(sealed, name), old) and inside(root(current, name), new)}
    before, after = root(sealed, "jobs_path"), root(current, "jobs_path")
    if (before is not None and after is not None
            and {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            <= _RELEASE_IDENTITY_FIELDS):
        moved.add("jobs_path")
    return frozenset(moved)


def same_sealed_work(previous, current, *, recovery=None) -> bool:
    """Whether two sealed launch inputs describe the same work with the same granted permissions."""
    exact = (all(previous.get(key) == current.get(key) for key in RESEAL_STABLE_KEYS)
            and granted_permissions(previous.get("applied_permissions"), previous.get("launch_home"), previous.get("worktree"))
            == granted_permissions(current.get("applied_permissions"), current.get("launch_home"), current.get("worktree")))
    if exact or recovery is None:
        return exact
    record, source, history, route = recovery
    if (source.get("worker_type") != "owner"
            or any(previous.get(key) != current.get(key) for key in RESEAL_STABLE_KEYS
                   if key not in {"harness", "argv"})
            or current.get("task") != recovery_task(record, source, history)):
        return False
    from dispatch_contract import DispatchContractError
    try:
        require_recovery_work(current, history, route=route, worker_type="owner", access=record.get("execution_access"))
    except DispatchContractError:
        return False
    return True


# ---------------------------------------------------------------------------
# 3. How many attempts, of which kind
# ---------------------------------------------------------------------------

def no_stage_authority(meta) -> bool:
    """A row the full-stage round census leaves out (`stage_authority=0`)."""
    return str(meta.get("stage_authority", "1")).lower() in {"0", "false"}


def subsession_row(meta) -> bool:
    """A row that is a sub-session by either mark; it never closes a stage gate itself."""
    return bool(meta.get("subsession_id")) or no_stage_authority(meta)


def linked_worktree_slice(meta) -> bool:
    """A sub-session slice (`stage_authority=0`) runs in a linked worktree while its
    owner row keeps the route cwd, so only a slice may differ from its parent's
    worktree; every other identity comparison stays exact."""
    return bool(meta.get("subsession_id")) and str(meta.get("stage_authority", "")) == "0"


def subsession_launch(subsession_id, stage_authority) -> bool:
    """A launch declared as a sub-session or without stage authority (parsed arguments)."""
    return bool(subsession_id) or stage_authority == 0


def declared_subsession(args) -> bool:
    """The one stage-session declaration judgment, read by `bind` and the dry-run preview.

    True for a complete sub-session declaration (every axis, `stage_authority=0`,
    bound to a dispatch-depth-2 route node), False for an ordinary full-stage
    launch. A partial or contradictory declaration is refused, so no caller can
    turn one raw flag into a sub-session.
    """

    def value(name):
        return getattr(args, name, None)

    values = tuple(value(name) for name in (
        "subsession_id", "subsession_index", "subsession_count", "subsession_mode",
        "session_chain_id", "phase_brief", "narrow_verify", "expected_round_trips",
    ))
    if any(item is not None for item in values) and not all(item is not None for item in values):
        raise _contract_error("subsession-arguments-incomplete", "all stage-session axes are required")
    stage_authority = getattr(args, "stage_authority", 1)
    if not value("subsession_id"):
        if stage_authority != 1:
            raise _contract_error(
                "stage-authority-zero-without-subsession", str(value("route_node") or "")
            )
        return False
    if stage_authority != 0:
        raise _contract_error("subsession-stage-authority-forbidden", value("subsession_id"))
    if value("dispatch_depth") != 2 or not value("route_id") or not value("route_node"):
        raise _contract_error("subsession-route-binding-invalid", value("subsession_id"))
    return True


def new_owner_request(previous_row, *, jobs=None, task=None):
    """A changed sealed assignment after a settled failure, without rewriting it."""
    if (jobs is None or not isinstance(task, str) or not task.strip()
            or previous_row.get("_status") not in {"done", "closed"}
            or committed_outcome(previous_row["_status"], previous_row) != "failed"):
        return False
    from dispatch_contract import (
        DispatchContractError, attempt_process_quiescence, terminal_conflict_pending,
    )
    from dispatch_replacement import launch_input
    try:
        previous = launch_input(jobs, previous_row["attempt_id"], previous_row)["task"]
        return (isinstance(previous, str) and bool(previous.strip())
                and " ".join(previous.split()) != " ".join(task.split())
                and not terminal_conflict_pending(previous_row)
                and attempt_process_quiescence(
                    previous_row, terminal_receipt=True).state == "quiescent")
    except (DispatchContractError, OSError, ValueError, KeyError, TypeError):
        # Missing history cannot manufacture a new request boundary.
        return False


def retry_predecessor(prior_rows, *, jobs=None, task=None):
    """The transport retry a new launch of this node continues, or "".

    Only a transport failure (a death, a runtime error) is retried in place.
    A worker's readable FAIL or BLOCKED is its result, on a capped node or
    not: the next launch is new work, so it never inherits a retry link and
    never spends the node's one replacement. Capped nodes still count it as a
    round through the shared round admission. A different explicit request on
    an uncapped stage is also new work once the old execution is quiescent.
    Callers keep capped rounds on their existing admission path. No original
    row or automatic replacement allowance is changed.
    """
    if not prior_rows:
        return ""
    latest = prior_rows[-1]
    status = latest["_status"]
    if committed_outcome(status, latest) == "failed":
        if readable_result(latest):
            return ""
        if new_owner_request(latest, jobs=jobs, task=task):
            return ""
        return latest.get("attempt_id", "")
    if status == "open" and latest.get("launch_claimed") == "0":
        # Register/start reuse the same unlaunched transport successor. A
        # semantic round has no such link and keeps its own round identity.
        return latest.get("automatic_retry_of", "")
    return ""


def _blocked_round_items(route, metadata):
    """The unmet `done_when` ids a BLOCKED round left, from the items file beside its artifact;
    None when that round's items cannot be judged (no plan items, no readable file, or a file
    bound to another route, node or attempt)."""
    if readable_result(metadata) != "BLOCKED":
        return None
    import base64
    import route_plan as RP
    from codex_dispatch_terminal import inspect_terminal_attempt
    try:
        terminal = inspect_terminal_attempt(
            metadata.get("log_file"), worktree=metadata.get("cwd") or route.get("cwd"),
            artifact_root_metadata=metadata.get("artifact_root") or route.get("artifact_root"))
        encoded = terminal.get("artifact_path_b64")
        if not encoded:
            return None
        artifact = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        items = RP.read_leg_items(route, artifact)
    except (OSError, ValueError, TypeError):
        return None
    if (items is None or items.get("attempt_id") != metadata.get("attempt_id")
            or items.get("node") != metadata.get("route_node")):
        return None
    return frozenset(items["unmet"])


def blocked_progress(route, rows, *, worker_type="test") -> int:
    """How many of the newest rounds, all without a verdict, count toward the verdict-less bound
    once progress is taken into account (RA-5).

    Two BLOCKED rounds in a row bind a node. A BLOCKED round whose unmet `done_when` items are a
    strict subset of the previous BLOCKED round's is progress: the count starts again at it. When
    either round's items cannot be judged (no plan items), nothing resets, exactly as before.
    `worker_type` is the node's own, as `round_budget` reads it, for a row that names none."""
    tail = []
    for status, metadata in reversed(list(rows)):
        kind = classify_round_row(status, metadata, worker_type=metadata.get("worker_type") or worker_type)
        if kind != "verdict-less":
            break
        tail.append(metadata)
    tail.reverse()
    streak, previous = 0, None
    for metadata in tail:
        current = _blocked_round_items(route, metadata) if route.get("route_plan") is not None else None
        streak = 1 if previous is not None and current is not None and current < previous else streak + 1
        previous = current
    return streak


def runtime_owner_can_resume(status, meta) -> bool:
    """A launched supervised owner's runtime end still has its same-route work.

    Admission also checks actual process quiescence; this metadata judgment
    keeps the route's sealed contract available while it awaits continuation.
    """
    return (status == "done" and meta.get("worker_type") == "owner"
            and meta.get("launch_started") == "1"
            and meta.get("supervisor_lease") == "flock-v1"
            and bool(meta.get("supervisor_lease_file"))
            and (meta.get("note") in {"dead-runtime-exit", "dead-runtime-error"}
                 or (meta.get("note") == "dead-protocol"
                     and meta.get("reconcile_reason") == "terminal-event-missing"
                     and meta.get("terminal_event") == "dispatch.supervisor.error"
                     and not readable_result(meta))))


def answerable_owner_end(status, meta) -> str:
    """How an ended owner's result waits for a person, or "".

    `BLOCKED`: it waits for an answer no declared gate carries. `FAIL`: its own final
    envelope reported a FAIL a person may answer with an approved fix (`readable_result`).
    Either answer is kept (`dispatch_owner_input`) and a replacement owner continues the
    same route with it first (`dispatch_replacement` 'corrected'). A death, an unverifiable
    end or an invalid envelope keeps its own classification, and a readable FAIL still gets
    no automatic retry (`retry_predecessor`): only a person's kept answer continues it."""
    if status != "done":
        return ""
    if meta.get("note") == "dead-worker-blocked":
        return "BLOCKED"
    return "FAIL" if readable_result(meta) == "FAIL" else ""


def fix_answers(route, lines, jobs=None) -> tuple[list[str], list[str]]:
    """What a person's approved fix for a FAIL-ended owner answers: `(answers, spent)`.

    For every round-capped node whose last full-stage verdict round is a blocking FAIL, the
    fix answers that FAIL attempt, which gives the node the one closure-check round SD-154
    admits for a revision that answers its last FAIL, within the verdict ceiling cap + 1
    (SD-161). A node whose closure-check that answer would not admit (already at the ceiling,
    or bound by BLOCKED rounds without progress) is `spent` as `<node>:<state>`: the fix makes
    no round for it."""
    from dispatch_contract import parse_registry_metadata
    answers, spent = [], []
    for node in route.get("nodes") or []:
        if not is_round_capped_node(node):
            continue
        worker_type = node.get("worker_type") or ("review" if node.get("kind") == "review-worker" else "test")
        census = []
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            meta = parse_registry_metadata(fields[5])
            if ((meta.get("route_id") or meta.get("route")) == route.get("route_id")
                    and meta.get("route_node") == node.get("id") and not no_stage_authority(meta)):
                census.append((fields, meta))
        rows = [(fields[1], meta) for fields, meta in _ROUND.logical_round_records(census, jobs=jobs)]
        if not rows or not last_verdict_blocking(rows, worker_type):
            continue
        verdicts = [meta for status, meta in rows if classify_round_row(
            status, meta, worker_type=meta.get("worker_type") or worker_type) == "verdict"]
        failed = verdicts[-1].get("attempt_id", "")
        # Admission's own rule decides: the fix answers this FAIL only when that answer is
        # what admits the closure-check round (not at cap + 1, not bound by BLOCKED rounds).
        budget = round_budget(route, node, rows, revisions=[{"answers": [failed]}])
        if not failed or budget.round_kind != "closure-check":
            spent.append(f"{node.get('id')}:{budget.state}")
            continue
        answers.append(failed)
    return answers, spent


# The round budget already has one implementation (`review_round_cap`); these
# are its names in this module, not copies.
classify_round_row = _ROUND.classify_round_row
round_budget = _ROUND.round_budget
is_round_capped_node = _ROUND.is_round_capped_node
last_verdict_blocking = _ROUND.last_verdict_blocking
gate_unmet = _ROUND.gate_unmet


# The worker's final three lines. Every terminal reader parses them with this
# one pattern, so no surface reads a child as finished while another calls it
# malformed.
#
# G1: a fenced code block and/or up to two plain sentences may follow the block
# (the fence attaches to the envelope; closing sentences follow the fence).
# The verdict still comes from the block alone.
#
# A tail line is ambiguous when it mentions a verdict at all -- `verdict`/
# `평결` anywhere (any case), or a bare uppercase PASS/FAIL/BLOCKED -- so
# those fail closed, as do lines shaped like envelope fields; `Done.` and
# `All tests pass.` stay ordinary sentences. The same check guards the
# fence body, so an earlier decoy can never swallow a later envelope
# (PR #281 review rounds 1-3). Tail elements start at newlines only
# (`_TAIL_GAP`; the fence's own indentation is part of the fence element),
# which keeps the failing search linear.
_TAIL_EDGE = (
    r"(?![ \t#*\-]*(?i:artifact|verdict|blocker|평결)[ \t]*:)"
    r"(?![^\n]*(?i:verdict|평결))"
    r"(?![^\n]*\b(?:PASS|FAIL|BLOCKED)\b)"
)
_TAIL_GAP = r"\n+"
_TAIL_BLANK = r"(?:\n[ \t]*)*"
_TAIL_LINE = _TAIL_EDGE + r"[^\n]+"
_TAIL_FENCE_BODY = r"(?:" + _TAIL_EDGE + r"[^\n]*\n)*?"
_TAIL_FENCE = r"[ \t]*```[^\n]*\n" + _TAIL_FENCE_BODY + r"[ \t]*```"
HANDOFF_RE = re.compile(
    r"(?:\A|\n)artifact: (?P<artifact>[^\n]+)\n"
    r"verdict: (?P<verdict>PASS|FAIL|BLOCKED)\n"
    r"blocker: (?P<blocker>[^\n]+)"
    + r"(?:" + _TAIL_GAP
    + r"(?:" + _TAIL_FENCE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r")?)?"
    + r"|" + _TAIL_LINE + r"(?:" + _TAIL_GAP + _TAIL_LINE + r")?"
    + r")"
    + r")?"
    + _TAIL_BLANK
    + r"\Z"
)


_PASS_NOTE_RE = re.compile(r"none\s*\((?P<note>.+)\)\s*")


def pass_blocker_note(blocker) -> str | None:
    """What a PASS envelope's `blocker: none (...)` adds: "" for a bare `none`, the
    parenthesized note for `none (...)`, None for any other blocker text. The note
    is kept as a remark; it never changes the verdict (RA-8)."""
    if blocker == "none":
        return ""
    match = _PASS_NOTE_RE.fullmatch(blocker) if isinstance(blocker, str) else None
    return match.group("note").strip() if match else None


def pass_blocker_violation(verdict, blocker) -> str | None:
    """A PASS envelope's blocker is `none`, optionally with a note in parentheses;
    any other blocker text breaks the contract."""
    if verdict == "PASS" and pass_blocker_note(blocker) is None:
        return "pass-blocker-not-none"
    return None


# A worker that died at a usage limit, an auth failure or a provider's capacity
# prints one terse line at the end of its log. This one ordered table classifies it
# for every harness, at launch (the wrapper's early watch) and later (liveness);
# the first match wins and the label becomes the `dead-<label>` note.
DEATH_PATTERNS = (
    ("capacity", r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b"),
    ("network-operation-not-permitted", r"operation not permitted|network is unreachable|network access denied"),
    ("session-limit", r"hit your (?:session|usage) limit|session limit reached"),
    ("usage-limit", r"usage[_ ]limit[_ ]reached|usage limit reached|weekly limit|"
     r"rate limit(?:ed)?|provider rate limit|exceeded retry limit|\b429\b"),
    ("auth", r"invalid api key|authentication_error|not logged in|please run /login|unauthorized|\b401\b"),
    ("credit", r"credit balance is too low|insufficient (?:credit|quota|funds)"),
    ("permission-reject", r"permission requested:.*auto-rejecting"),
)
_RESET_RE = re.compile(
    r"resets?(?:\s+at)?\s+([0-9]{1,2}:[0-9]{2}\s*(?:am|pm)?|[0-9]{1,2}\s*(?:am|pm))",
    re.I,
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CAPACITY_TERMINAL_RE = re.compile(
    r"(?:error\s*[:\-]\s*)?(?:selected\s+)?model(?:\s+[A-Za-z0-9._:/-]+)?\s+"
    r"(?:is\s+)?at\s+capacity[.!]?",
    re.I,
)


def scan_death(text: str) -> tuple[str, str] | None:
    """``(label, reset)`` when the text shows a limit/auth death, else None.

    ``reset`` is a best-effort human string ('3pm', '15:45', ...) or '' when absent.
    """
    low = text.lower()
    label = next((name for name, pattern in DEATH_PATTERNS if re.search(pattern, low)), "")
    if not label:
        return None
    match = _RESET_RE.search(text)
    return label, (re.sub(r"\s+", "", match.group(1)) if match else "")


def anchored_capacity_failure(text: str) -> bool:
    """Accept only a terminal capacity error, never prose discussing one.

    Adapters may emit either a plain CLI line or a JSON event.  The bounded
    last-three-line rule is shared by the early wrapper watch and the SD-58
    foreground watchdog so delayed failures receive the same classification.
    """

    def terminal(value: str) -> bool:
        return bool(_CAPACITY_TERMINAL_RE.fullmatch(value.strip()))

    lines = [line.strip() for line in text.splitlines() if line.strip()][-3:]
    for line in lines:
        if len(line) > 200:
            continue
        if terminal(line):
            return True
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pending = [payload]
        while pending:
            item = pending.pop()
            for key, value in item.items():
                if isinstance(value, dict):
                    pending.append(value)
                elif key in {"message", "error", "detail"} and isinstance(value, str) and terminal(value):
                    return True
    return False


def scan_anchored_death(text: str) -> tuple[str, str] | None:
    """Inspect only terse terminal CLI lines, never completion-report prose: the last
    three non-empty lines, each at most 200 characters once colors are removed."""
    for line in [line.strip() for line in text.splitlines() if line.strip()][-3:]:
        if len(ANSI_RE.sub("", line)) > 200:
            continue
        death = scan_death(line)
        if death:
            if death[0] == "capacity" and not anchored_capacity_failure(line):
                continue
            return death
    return None


# ---------------------------------------------------------------------------
# 4. What it may access
# ---------------------------------------------------------------------------

def bind_access_request(*args, **kwargs):
    """Resolve, validate, constrain and grade an explicit execution access request."""
    from execution_access import bind_request
    return bind_request(*args, **kwargs)


def access_grant(*args, **kwargs):
    """The effective explicit grant for one runtime; never creates runtime argv."""
    from execution_access import build_grant
    return build_grant(*args, **kwargs)


def contract_read_roots(agent_home: str | Path, route: dict | None = None) -> tuple[Path, ...]:
    """Contract directories for an already validated launch binding.

    Use the same sealed launch-home field the installer's in-use judgment
    retains. A moving current pointer never replaces the route's contract
    source. These are read addresses, not additional task write grants.
    """
    from execution_access import harness_source_read_roots
    homes = [Path(agent_home)]
    sealed = (route or {}).get("launch_compatibility_tuple") or {}
    identity = sealed.get("launch_home") if isinstance(sealed, dict) else None
    value = identity.get("path") if isinstance(identity, dict) else None
    if isinstance(value, str) and os.path.isabs(value):
        homes.append(Path(value))
    roots = []
    for home in homes:
        roots.extend(harness_source_read_roots(home))
    return tuple(dict.fromkeys(roots))


def bind_launch_access(args, *, runtime: str, default_roots, network_available: bool = False,
                       parent_network: bool = False, effective_sandbox: str = "workspace-write",
                       gpu_resource_scope: bool = False, inherit_parent_sandbox: bool = False):
    """One launch's execution access, for every adapter: the request, the exact live
    parent's effective grant for a dispatch-depth-2 child, and the graded grant (None
    without a request). The adapter passes only what its own launch realizes."""
    import subprocess
    from dispatch_contract import (
        DispatchContractError, dispatch_state_root, parent_lookup_worktree,
        resolve_live_parent_attempt,
    )
    from execution_access import (AccessContext, ExecutionAccessError, bind_request,
                                  load_parent_effective_grant, request_path)
    context = AccessContext.build(worktree=args.worktree, artifact_root=args.artifact_root,
                                  dispatch_state_root=dispatch_state_root(args.jobs_path),
                                  agent_home=args.agent_home, environ=os.environ)
    parent = None
    compute_scope = None
    route_file = getattr(args, "route_file", None)
    owner_binding = getattr(args, "owner_route_binding", None)
    if not route_file and owner_binding is not None:
        route_file = owner_binding.route_file
    # Both owner and node bindings have passed the wrappers' existing route
    # validation before access binding; contract reads do not need a request.
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8")) if route_file else None
    except (OSError, ValueError) as exc:
        raise ExecutionAccessError("route-record-unreadable", str(exc)) from exc
    args.contract_read_roots = contract_read_roots(args.agent_home, route)
    if route_file and request_path(args.execution_access_file, os.environ) is not None:
        from gpu_execution_sandbox import select as gpu_selection
        compute_scope = gpu_selection(route, node=getattr(args, "route_node", None),
                                      owner=args.dispatch_depth == 1, environ={})["gpu_scope"]
    if args.dispatch_depth >= 2 and request_path(args.execution_access_file, os.environ) is not None:
        if args.parent_binding is None:
            # Prospective probes without an access request need no live owner.
            # A requested grant, including in dry-run, uses start's exact lookup.
            try:
                repo = subprocess.check_output(
                    ["git", "-C", str(args.worktree), "rev-parse", "--show-toplevel"],
                    text=True, stderr=subprocess.DEVNULL,
                ).strip()
                args.parent_binding = resolve_live_parent_attempt(
                    args.jobs_path, parent_slug=getattr(args, "parent_slug", None) or "",
                    repo=repo,
                    worktree=parent_lookup_worktree(
                        args.worktree, getattr(args, "route_file", None),
                        subsession=bool(getattr(args, "subsession_id", None)),
                        parent_attempt_id=getattr(args, "parent_attempt_id", None)),
                    expected_attempt_id=getattr(args, "parent_attempt_id", None),
                    expected_harness=getattr(args, "parent_harness", None),
                    expected_transport=getattr(args, "parent_transport", None),
                    expected_sandbox=getattr(args, "parent_sandbox", None),
                )
                args.parent_attempt_id = args.parent_binding.attempt_id
            except (DispatchContractError, subprocess.CalledProcessError, OSError) as exc:
                raise ExecutionAccessError(
                    "execution-access-exceeds-parent:parent-grant-unknown",
                    getattr(exc, "detail", str(exc)),
                ) from exc
        parent = load_parent_effective_grant(jobs=args.jobs_path, parent_attempt_id=args.parent_binding.attempt_id,
                                             context=context)
    return bind_request(
        args.execution_access_file, environ=os.environ, context=context,
        is_child=args.dispatch_depth >= 2, parent=parent, runtime=runtime,
        default_writable_roots=default_roots,
        network_available=network_available or bool(parent_network and parent is not None
                                                     and parent.network_allowed),
        effective_sandbox=effective_sandbox, gpu_resource_scope=gpu_resource_scope,
        inherit_parent_sandbox=inherit_parent_sandbox, compute_execution_scope=compute_scope)


# ---------------------------------------------------------------------------
# Launch admission shared by the three adapter wrappers
# ---------------------------------------------------------------------------

def prelaunch_registry(args) -> Path:
    """The registry the route's completion gate reads before the authoritative one
    is resolved: explicit, else inherited, else the agent home's first state root.
    The launch revalidates the authoritative registry immediately before its claim."""
    explicit_or_inherited = args.jobs or os.environ.get("AGENT_DISPATCH_JOBS", "")
    if explicit_or_inherited:
        return Path(explicit_or_inherited)
    from dispatch_contract import dispatch_state_roots
    return dispatch_state_roots(args.agent_home)[0] / "jobs.log"


def completion_gate_fail_fields(error, route_file, route_node) -> dict:
    """SD-154/B-2: a route-state refusal (13.59.3 rule 6) carries a supported
    `next_action` so a caller stops instead of misreading it as a transient
    runtime-unavailable and descending to inline."""
    from dispatch_contract import ROUTE_STATE_REFUSAL_REASONS, route_state_next_action
    fields = {"detail": error.detail, "child_spawned": "0"}
    if error.reason in ROUTE_STATE_REFUSAL_REASONS:
        fields["next_action"] = error.next_action or route_state_next_action(
            error.reason, error.detail, str(route_file), route_node,
        )
    return fields


def completion_gate(args, action: str, agent_home, jobs, *, gate, before=()):
    """The route's completion and preview gate for one launch, the same in every wrapper.

    `gate` is the wrapper's `completion_marker_gate`; `before` are checks that share its
    refusal handling. A refusal returns `(reason, exit code, receipt fields)` with the
    preview-gate recovery detail attached; None admits the launch."""
    from dispatch_contract import (DispatchContractError, PRELAUNCH_PROCESS_BLOCK_REASONS,
                                   recover_preview_gate_after_refusal)
    from review_input import preview_request_nodes
    try:
        for check in before:
            check()
        gate(args.route_file, args.route_node, action, agent_home, jobs, attempt_id=args.attempt_id,
             planned_revision_nodes=preview_request_nodes(args, jobs))
    except DispatchContractError as error:
        error.detail = recover_preview_gate_after_refusal(
            args.route_file, args.route_node, action, agent_home, jobs, error)
        return (error.reason, 78 if error.reason in PRELAUNCH_PROCESS_BLOCK_REASONS else 65,
                completion_gate_fail_fields(error, args.route_file, args.route_node))
    return None


class RelocationDecision(NamedTuple):
    allowed: bool
    reason: str | None
    route_ids: tuple[str, ...]


def relocation_admission(root, records, directories, *, selected_route_ids=(), evidence=None) -> RelocationDecision:
    """Read-only no-live history decision; never grants continuation ownership.

    Unknown evidence protects the source. Target unrelated work is outside this scope.
    Uses autoclose's existing process, attempt, lease and resource observations.
    """
    import route_autoclose as ac
    import artifact_producer as producer
    route_ids = set(selected_route_ids)
    def ids(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"route_id", "owner_route_id", "batch_route_id", "parent_route_id"} and isinstance(item, str):
                    route_ids.add(item)
                else:
                    ids(item)
        elif isinstance(value, list):
            for item in value:
                ids(item)
    for record in records:
        ids(record)
    try:
        observed = evidence or ac._Evidence(Path(root), producer)
        # Check payload holders even when a cycle has no route metadata.
        for directory in directories:
            if any(ac._under(item, directory) for item in observed.resource_paths):
                return RelocationDecision(False, "resource-run", tuple(sorted(route_ids)))
            if any(ac._under(item, directory) for item in observed.open_paths):
                return RelocationDecision(False, "cycle-in-use", tuple(sorted(route_ids)))
        pending = list(route_ids)
        checked = set()
        while pending:
            route_id = pending.pop()
            if route_id in checked:
                continue
            checked.add(route_id)
            path = Path(root) / ".runtime/routes" / (route_id + ".json")
            route = json.loads(path.read_bytes()) if path.exists() else None
            ids(route)
            pending.extend(route_ids - checked)
            if hasattr(observed, "owners"):
                # Closure settlement and intact history relocation have different
                # authority. A missing old absolute completion path is not live
                # work. Keep positive pending settlement and every other guard.
                import copy
                from dispatch_terminal_commit import owner_completion_state
                relocation = copy.copy(observed)
                owners = []
                for row in observed.owners.get(route_id, ()):
                    state = owner_completion_state(Path(row["jobs"]), row["status"], row["metadata"])
                    if state.state == "unknown" and state.reason == "FileNotFoundError":
                        continue
                    owners.append(row)
                relocation.owners = {route_id: owners}
                reason = relocation.kept(route_id, lambda: None, route)
            else:
                reason = observed.kept(route_id, lambda: None, route)
            if reason:
                return RelocationDecision(False, reason, tuple(sorted(route_ids)))
    except Exception as exc:
        return RelocationDecision(False, "evidence-unreadable:" + str(exc), tuple(sorted(route_ids)))
    return RelocationDecision(True, None, tuple(sorted(route_ids)))


def require_batch_reservation(
    payload: dict[str, object], expected: dict[str, object] | None
) -> None:
    """The current launch grant; a historical manifest is read separately.

    Called at registration and consumption of the existing atomic reservation.
    Canonical digest checks compare the stored manifest to its stored digest.
    """
    import hashlib
    from replica_batch_contract import DIGEST, ReplicaBatchContractError, verify_manifest
    if expected is None:
        if payload.get("reservation_kind") in {"replica-batch", "parallel-batch"}:
            raise _contract_error(
                "parallel-group-reservation-mismatch",
                "parallel batch token cannot authorize a non-group start",
            )
        return
    public_expected = {
        key: value for key, value in expected.items() if not key.startswith("_")
    }
    mismatches = {
        key: (value, payload.get(key))
        for key, value in public_expected.items()
        if payload.get(key) != value
    }
    for key in ("batch_manifest_sha256", "batch_leg_sha256"):
        value = payload.get(key)
        if not isinstance(value, str) or not DIGEST.fullmatch(value):
            mismatches[key] = ("sha256:<64 lowercase hex>", value)
    manifest = payload.get("batch_manifest")
    try:
        verified, manifest_digest, leg_digests = verify_manifest(manifest)
    except ReplicaBatchContractError as exc:
        mismatches["batch_manifest"] = ("valid canonical manifest", str(exc))
        verified, manifest_digest, leg_digests = {}, "", {}
    if manifest_digest and payload.get("batch_manifest_sha256") != manifest_digest:
        mismatches["batch_manifest_sha256"] = (
            manifest_digest,
            payload.get("batch_manifest_sha256"),
        )
    if verified:
        common = {
            "route_id": public_expected.get("batch_route_id"),
            "parent_attempt_id": public_expected.get("batch_parent_attempt_id"),
        }
        manifest_group = verified.get("parallel_group") or verified.get("replica_group")
        if manifest_group != public_expected.get("batch_group"):
            mismatches["manifest.parallel_group"] = (
                public_expected.get("batch_group"), manifest_group
            )
        for key, value in common.items():
            if verified.get(key) != value:
                mismatches[f"manifest.{key}"] = (value, verified.get(key))
        route_nodes = sorted(str(member.get("route_node", "")) for member in verified["members"])
        if route_nodes != expected.get("_batch_route_nodes"):
            mismatches["manifest.route_nodes"] = (
                expected.get("_batch_route_nodes"), route_nodes
            )
        allowed = expected.get("_batch_allowed_members", {})
        for manifest_member in verified["members"]:
            # A partial reservation launches only its selected gap. The other
            # members are digest-bound historical peers already checked by the
            # source reader and peer census, not new current launch candidates.
            if (payload.get("batch_admission_count") == 1
                    and manifest_member.get("attempt_id") != public_expected.get("batch_attempt_id")):
                continue
            member_node = str(manifest_member.get("route_node", ""))
            profile_fields = expected.get("_batch_profile_selections", {}).get(member_node)
            if profile_fields is not None:
                for key, value in profile_fields.items():
                    if manifest_member.get(key) != value:
                        mismatches[f"manifest.member.{member_node}.{key}"] = (value, manifest_member.get(key))
            allowed_for_member = (
                allowed.get(member_node, []) if isinstance(allowed, dict) else []
            )
            member_tuple = {
                "harness": manifest_member.get("harness"),
                "fallback_hop": manifest_member.get("fallback_hop"),
                "fallback_ordinal": manifest_member.get("fallback_ordinal"),
            }
            if member_tuple not in allowed_for_member:
                mismatches[f"manifest.member.{member_node}.route_binding"] = (
                    allowed_for_member, member_tuple
                )
        selected = [
            member for member in verified["members"]
            if member.get("attempt_id") == public_expected.get("batch_attempt_id")
        ]
        if len(selected) != 1:
            mismatches["manifest.selected_member"] = (
                public_expected.get("batch_attempt_id"), len(selected)
            )
        else:
            member = selected[0]
            member_expected = {
                "route_node": public_expected.get("batch_route_node"),
                "harness": public_expected.get("batch_harness"),
                "fallback_hop": public_expected.get("batch_fallback_hop"),
                "fallback_ordinal": public_expected.get("batch_fallback_ordinal"),
            }
            if int(verified.get("schema_version", 1)) >= 2:
                member_expected.update({
                    "model_profile": public_expected.get("batch_model_profile"),
                    "perspective": public_expected.get("batch_perspective"),
                    "parallel_leg_index": public_expected.get("batch_parallel_leg_index"),
                })
            for key, value in member_expected.items():
                if member.get(key) != value:
                    mismatches[f"manifest.member.{key}"] = (value, member.get(key))
            expected_assignment = public_expected.get("batch_assignment_sha256")
            if expected_assignment and member.get("assignment_sha256") != expected_assignment:
                mismatches["manifest.member.assignment_sha256"] = (
                    expected_assignment, member.get("assignment_sha256")
                )
            attempt = str(member.get("attempt_id", ""))
            if payload.get("batch_leg_sha256") != leg_digests.get(attempt):
                mismatches["batch_leg_sha256"] = (
                    leg_digests.get(attempt), payload.get("batch_leg_sha256")
                )
        if payload.get("batch_independence") != verified.get("independence"):
            mismatches["batch_independence"] = (
                verified.get("independence"), payload.get("batch_independence")
            )
    declared_size = public_expected.get("batch_declared_size")
    admission = payload.get("batch_admission_count")
    if (isinstance(declared_size, bool) or not isinstance(declared_size, int)
            or not 2 <= declared_size <= 4):
        mismatches["batch_declared_size"] = ("integer 2..4", declared_size)
        declared_size = 0
    if isinstance(admission, bool) or admission not in {1, declared_size}:
        mismatches["batch_admission_count"] = (f"1|{declared_size}", admission)
    elif admission == 1:
        selected_attempt = str(public_expected.get("batch_attempt_id", ""))
        peer_members = (
            [
                member for member in verified.get("members", [])
                if str(member.get("attempt_id", "")) != selected_attempt
            ]
            if verified
            else []
        )
        expected_peers = sorted(str(member.get("attempt_id", "")) for member in peer_members)
        proof_keys = {
            "agent_home", "attempt_id", "jobs", "manifest_sha256",
            "reason", "route", "state",
        }
        proofs = payload.get("batch_peer_set")
        if payload.get("batch_peer_count") != len(expected_peers):
            mismatches["batch_peer_count"] = (len(expected_peers), payload.get("batch_peer_count"))
        if not isinstance(proofs, list) or len(proofs) != len(expected_peers):
            mismatches["batch_peer_set"] = ("exact N-1 canonical proofs", proofs)
        else:
            actual_peers=[]
            for index, proof in enumerate(proofs):
                label=f"batch_peer_set[{index}]"
                if not isinstance(proof, dict) or set(proof) != proof_keys:
                    mismatches[label] = ("canonical peer proof", proof)
                    continue
                actual_peers.append(str(proof.get("attempt_id", "")))
                if proof.get("manifest_sha256") != manifest_digest:
                    mismatches[f"{label}.manifest_sha256"] = (manifest_digest, proof.get("manifest_sha256"))
                if proof.get("state") not in {"active", "completed"}:
                    mismatches[f"{label}.state"] = ("active|completed", proof.get("state"))
                for key in ("agent_home", "jobs", "route"):
                    value=proof.get(key)
                    if not isinstance(value,str) or not Path(value).is_absolute():
                        mismatches[f"{label}.{key}"] = ("absolute path", value)
                if not isinstance(proof.get("reason"),str) or not proof.get("reason"):
                    mismatches[f"{label}.reason"] = ("non-empty observation reason", proof.get("reason"))
            if actual_peers != expected_peers:
                mismatches["batch_peer_set.attempts"] = (expected_peers, actual_peers)
            encoded=json.dumps(proofs,separators=(",",":"),sort_keys=True).encode("utf-8")
            proof_digest="sha256:"+hashlib.sha256(encoded).hexdigest()
            if payload.get("batch_peer_set_sha256") != proof_digest:
                mismatches["batch_peer_set_sha256"] = (proof_digest,payload.get("batch_peer_set_sha256"))
    elif admission == declared_size:
        for key in (
            "batch_peer_count", "batch_peer_set", "batch_peer_set_sha256",
            "batch_peer_attempt_id", "batch_peer_state",
            "batch_peer_proof", "batch_peer_proof_sha256",
        ):
            if key in payload:
                mismatches[key] = ("absent for full batch", payload.get(key))
    if mismatches:
        detail = ";".join(
            f"{key}:expected={wanted}:actual={actual}"
            for key, (wanted, actual) in sorted(mismatches.items())
        )
        raise _contract_error("parallel-group-reservation-mismatch", detail)
