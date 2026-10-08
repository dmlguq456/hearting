#!/usr/bin/env python3
"""Route authority: the one place that answers four questions about a route.

1. Who continues it (the parent session and its successor).
2. On which harness (sealed selection pins).
3. How many attempts of which kind (sub-session standing, retry links,
   round budget, the result envelope).
4. What it may access (execution access grant).

Every judgment here used to be made at its call site, several of them in
more than one copy. The old names stay where they were, as imports or thin
wrappers, so existing callers and patches keep working. Copies that
historically disagree keep distinct names here instead of being merged, so
one judgment changes in one place.

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
    `launched_harness`, or None. Only a recorded change moves a replacement; a sealed pin the
    original launch did not follow (a usage-limit fallback) keeps today's same-harness replay."""
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


# A replacement replays its source on the same harness, registry and worktree -- unless the
# route's parent moved the owner pin before the claim (`moved_owner_harness`): that owner
# replacement takes the ordinary owner launch on the new harness with the same work.
REPLACEMENT_FIXED_KEYS = ("harness", "jobs", "worktree")


# Same work, another launcher. Never-started work may be taken over by a launcher that runs
# somewhere else: inside the parent's OS sandbox (a Codex owner's tool shell) or on the host
# beside it (the session supervisor advancing a serial chain). Where a launcher runs decides
# these realized values -- the wrapper's lifetime scope and, for Codex, whether its own sandbox
# nests inside the parent's -- while the work and the permissions granted to it (argv with
# `--sandbox` and the parent tuple, network, the access grant, Claude/OpenCode permissions)
# stay the same and are still compared.
LAUNCH_LOCATION_VALUES = frozenset({"launch_lifecycle", "runtime_sandbox"})

# The sealed launch input of one attempt: the work and its granted permissions.
RESEAL_STABLE_KEYS = ("schema", "attempt_id", "harness", "jobs", "worktree", "argv", "task",
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
_RELEASE_TREE_PATH = re.compile(r"(/[^\s\"'()*]*?)/(?:utilities|adapters|hooks|tools)/")
RELEASE_ROOT_TOKEN = "<launch_home>"


def granted_permissions(applied, launch_home=None) -> dict:
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
# it and the route's own seal are still compared.
RELEASE_DERIVED_VALUES = frozenset({
    "model", "reasoning", "resolved_model_settings", "resolved_completion_delivery",
    "parent_completion_delivery", "execution_surface", "fallback_hop", "model_role", "model_profile"})
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


def same_sealed_work(previous, current) -> bool:
    """Whether two sealed launch inputs describe the same work with the same granted permissions."""
    return (all(previous.get(key) == current.get(key) for key in RESEAL_STABLE_KEYS)
            and granted_permissions(previous.get("applied_permissions"), previous.get("launch_home"))
            == granted_permissions(current.get("applied_permissions"), current.get("launch_home")))


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


def retry_predecessor(prior_rows):
    """The transport retry a new launch of this node continues, or "".

    Only a transport failure (a death, a runtime error) is retried in place.
    A worker's readable FAIL or BLOCKED is its result, on a capped node or
    not: the next launch is new work, so it never inherits a retry link and
    never spends the node's one replacement. Capped nodes still count it as a
    round through the shared round admission. No original row is changed.
    """
    if not prior_rows:
        return ""
    latest = prior_rows[-1]
    status = latest["_status"]
    if committed_outcome(status, latest) == "failed":
        return "" if readable_result(latest) else latest.get("attempt_id", "")
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
        inherit_parent_sandbox=inherit_parent_sandbox)


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
