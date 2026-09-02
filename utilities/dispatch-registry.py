#!/usr/bin/env python3
"""Current-work filtering and guarded registry reconciliation (SD-60)."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities")]
from tools.fleet.model import ATTEMPT_CLASSIFIER_SOURCE, classify_attempt_evidence  # noqa: E402
from dispatch_contract import (ARTIFACT_PROOF_RECEIPT,
                               AUTOMATIC_RECEIPTLESS_CLASSIFIER,
                               DispatchContractError,
                               PARENT_EXTINCTION_TERMINAL_STATUSES,
                               agent_home_equivalent,
                               annotate_attempt_row_if,
                               attempt_governed_process_quiescence,
                               attempt_process_quiescence,
                               attempt_tagged_descendants,
                               authoritative_process_identities,
                               close_attempt_row,
                               close_attempt_row_if,
                               exact_process_group_signal_authority,
                               observer_namespace_extinct,
                               parse_registry_metadata,
                               process_group_observation,
                               process_observation,
                               process_state,
                               process_start_ticks,
                               prove_attempt_quiescence,
                               observed_attempt_liveness,
                               post_exit_receipt_reason,
                               recovery_id,
                               dispatch_state_roots,
                               claim_recovery_retry,
                               reconcile_local_registry, resolve_agent_home,
                               resolve_dispatch_state_root,
                               resolve_parent_extinction,
                               seal_cancellation_quiescence_receipt,
                               signal_exact_process_group,
                               validate_attempt_metadata)  # noqa: E402
from dispatch_continuation_budget import resolve_continuation_budget  # noqa: E402
from owner_route_binding import (  # noqa: E402
    OwnerRouteBinding,
    OwnerRouteBindingError,
    resolve_owner_route_lifecycle,
)
from dispatch_registry_inventory import (  # noqa: E402
    import_archive as inventory_import_archive,
    inventory_query,
)
from codex_dispatch_terminal import (  # noqa: E402
    REVIEW_BLOCKING_NOTE,
    carrier_terminal_note,
    inspect_terminal_attempt,
)
from dispatch_completion_join import (  # noqa: E402
    materialize_after_terminal_close,
    reconcile_pending_delivery,
)
from dispatch_summary import ensure_attempt_owner  # noqa: E402
_cleanup_spec = importlib.util.spec_from_file_location("worktree_cleanup", ROOT / "utilities/worktree-cleanup.py")
cleanup = importlib.util.module_from_spec(_cleanup_spec)
sys.modules[_cleanup_spec.name] = cleanup
_cleanup_spec.loader.exec_module(cleanup)

OPEN = {"open", "running"}


def _first_existing_dispatch_path(home, jobs, *parts):
    """Read-order lookup across the canonical dispatch state root and the
    legacy agent-home-relative tree (I-2 read-fallback, design constraint 3):
    a marker/heartbeat/watchdog file written before this cycle's resolver
    unification is still found at its legacy location."""

    if home is None:
        return None
    candidates = [root.joinpath(*parts) for root in dispatch_state_roots(home, jobs)]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def parse_meta(pipe):
    return dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)


def read_rows(jobs):
    rows = []
    if not jobs.is_file(): return rows
    for order, line in enumerate(jobs.read_text(encoding="utf-8", errors="replace").splitlines()):
        fields = line.split("\t")
        if len(fields) != 6: continue
        meta = parse_meta(fields[5])
        raw_schema = meta.get("attempt_schema_version")
        legacy = raw_schema in (None, "", "1")
        contract_status = (
            "legacy-read-only" if legacy
            else "current" if raw_schema == "2"
            else "invalid:attempt-schema-version"
        )
        if contract_status == "current":
            try:
                validate_attempt_metadata(meta)
            except DispatchContractError as exc:
                contract_status = f"invalid:{exc.reason}"
        rows.append({"order": order, "timestamp": fields[0], "status": fields[1],
                     "repo": fields[2], "worktree": fields[3], "slug": fields[4],
                     "pipe": fields[5], "meta": meta, "raw": line,
                     "legacy_read_only": legacy, "attempt_contract_status": contract_status})
    return rows


def fold_key(meta):
    """Keep declared sub-sessions first-class while folding ordinary retries."""
    return (
        meta.get("route_id"),
        meta.get("route_node"),
        meta.get("subsession_id") or "__stage__",
    )


def matches(row, args):
    meta = row["meta"]
    checks = ((args.session, meta.get("session_id") or meta.get("parent_sid")),
              (args.route, meta.get("route_id")), (args.node, meta.get("route_node")),
              (args.attempt, meta.get("attempt_id")), (args.job, row["slug"]))
    return all(not expected or expected == actual for expected, actual in checks)


def current(rows):
    newest = {}
    passthrough = []
    for row in rows:
        key = fold_key(row["meta"])
        if all(key[:2]) and row["meta"].get("attempt_id"):
            newest[key] = row
        else: passthrough.append(row)
    return passthrough + sorted(newest.values(), key=lambda row: row["order"])


def attempt_heartbeat(home, meta, jobs=None):
    attempt = (meta.get("attempt_id") or "").replace("/", "_")
    if home is None or not attempt:
        return None
    path = _first_existing_dispatch_path(home, jobs, "heartbeats", f"{attempt}.json")
    if path is None:
        return None
    try:
        if path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def attempt_terminal_observation(home, meta, jobs=None):
    attempt = (meta.get("attempt_id") or "").replace("/", "_")
    route, node = meta.get("route_id"), meta.get("route_node")
    if home is None or not attempt or not route or not node:
        return None
    path = _first_existing_dispatch_path(home, jobs, "watchdog", f"{attempt}.json")
    if path is None:
        return None
    try:
        if path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not value.get("terminal_action"):
        return None
    return {
        **value,
        "attempt_id": meta["attempt_id"],
        "route_id": route,
        "route_node": node,
    }


def proc_inputs(row, home=None, jobs=None, rows=None, args=None):
    meta = row["meta"]
    raw_local = meta.get("pid", "")
    local_pid = int(raw_local) if raw_local.isdigit() else None
    local_start = meta.get("pid_start", "")
    identities = authoritative_process_identities(meta)
    identity = identities[0] if len(identities) == 1 else None
    pid = identity.pid if identity is not None else local_pid
    expected = identity.expected_start if identity is not None else local_start
    actual = ""; alive = False
    if pid is not None and (identity is not None or meta.get("pid_scope") != "namespace-local"):
        actual = process_start_ticks(pid) or ""
        alive = bool(actual) and process_state(pid) != "Z"
    inputs = {"pid": pid, "proc_start": expected, "actual_proc_start": actual,
            "pid_alive": alive, "proc_start_match": bool(alive and expected == actual),
            "pid_authoritative": identity is not None,
            "pid_identity_source": identity.source if identity is not None else None,
            "pid_local": local_pid, "pid_local_start": local_start,
            "pid_host": int(meta["pid_host"]) if meta.get("pid_host", "").isdigit() else None,
            "pid_host_start": meta.get("pid_host_start", ""),
            "pid_scope": meta.get("pid_scope"),
            "attempt_descendants": attempt_tagged_descendants(meta).state,
            "attempt_id": meta.get("attempt_id"), "route_id": meta.get("route_id"),
            "route_node": meta.get("route_node"),
            "heartbeat": attempt_heartbeat(home, meta, jobs),
            "terminal_observation": attempt_terminal_observation(home, meta, jobs)}
    proof = parent_extinction_proof(row, rows, args)
    if proof.state == "proven":
        inputs["parent_extinction"] = {
            "state": proof.state, "reason": proof.reason,
            "parent_attempt_id": proof.parent_attempt_id,
        }
    return inputs


def parent_extinction_proof(child, rows, args=None):
    """Resolve one exact parent proof from the already-read registry snapshot."""
    meta = dict(child.get("meta") or {})
    meta.update({
        "repo": child.get("repo", ""),
        "worktree": child.get("worktree", ""),
        "parent": meta.get("parent") or child.get("parent", ""),
    })
    parent_rows = tuple((row["raw"].split("\t"), row["meta"]) for row in rows or ())
    raw_pid = str(getattr(args, "pid", "") or "")
    raw_start = str(getattr(args, "pid_start", "") or "")
    raw_observer = str(getattr(args, "pid_observer_ns", "") or "")
    observation = None
    if raw_pid and raw_start and raw_observer:
        observation = {
            "state": "extinct",
            "parent_attempt_id": meta.get("parent_attempt_id", ""),
            "pid": raw_pid,
            "pid_start": raw_start,
            "pid_observer_ns": raw_observer,
        }
    return resolve_parent_extinction(meta, parent_rows, observation)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (AttributeError, ValueError):
        return None


def terminal_marker(row, home, jobs=None):
    meta = row["meta"]
    route, node = meta.get("route_id"), meta.get("route_node")
    route_hash, gate = meta.get("route_hash"), meta.get("completion_gate")
    if not route or not node or not route_hash or not gate:
        return False, "terminal-row-contract-incomplete"
    marker_path = _first_existing_dispatch_path(home, jobs, "completion", route, f"{node}.json")
    if marker_path is None:
        return False, "terminal-marker-invalid"
    try: marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return False, "terminal-marker-invalid"
    if marker.get("schema_version") != 2 or not _marker_backed_repair(row, home, jobs):
        return False, "terminal-marker-attempt-link-invalid"
    evidence_record = marker.get("evidence") if isinstance(marker.get("evidence"), dict) else {}
    evidence = Path(str(evidence_record.get("path", "")))
    if (marker.get("route_id") != route or marker.get("route_hash") != route_hash
            or marker.get("node_id") != node or marker.get("completion_gate") != gate
            or not evidence.is_absolute() or not evidence.is_file()):
        return False, "terminal-marker-mismatch"
    try:
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    except OSError:
        return False, "terminal-evidence-unreadable"
    if not evidence_record.get("sha256") or digest != evidence_record.get("sha256"):
        return False, "terminal-evidence-changed"
    row_time = timestamp(row["timestamp"])
    marker_time = timestamp(marker.get("completed_at"))
    if row_time is None or marker_time is None:
        return False, "row-clock-ambiguous"
    if marker_time <= row_time or marker_path.stat().st_mtime <= row_time or evidence.stat().st_mtime <= row_time:
        return False, "terminal-marker-not-newer"
    updated = timestamp(meta.get("updated_at"))
    if updated is not None and updated > marker_time:
        return False, "newer-registry-transition"
    attempt = meta.get("attempt_id", "").replace("/", "_")
    heartbeat_path = _first_existing_dispatch_path(home, jobs, "heartbeats", f"{attempt}.json")
    try:
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8")) if heartbeat_path else {}
    except (OSError, ValueError):
        heartbeat = {}
    if float(heartbeat.get("updated_at", 0) or 0) > marker_time:
        return False, "newer-heartbeat"
    watchdog = _first_existing_dispatch_path(home, jobs, "watchdog", f"{attempt}.json")
    try: watch = json.loads(watchdog.read_text()) if watchdog else {}
    except (OSError, ValueError): watch = {}
    quiet = int(watch.get("quiet_windows", 0) or 0)
    observed = float(watch.get("observed_at", 0) or 0)
    last_progress = float(watch.get("last_progress_at", 0) or 0)
    proved = quiet >= 2 and observed > marker_time and last_progress <= marker_time
    return (proved, "stale-terminal-proved" if proved else "stale-terminal-dwell")


def _marker_backed_repair(row, home, jobs=None):
    """SD-70: was ``complete``'s marker written but its row-close step failed?

    ``complete_node`` (capability-route.py) writes an attempt-linkage sibling
    file next to the completion marker before attempting the row close, so a
    later-dead row can be repaired by exact marker linkage rather than folded
    into the generic dead-exact-pid path.
    """
    meta = row["meta"]
    route_id, node, attempt_id = meta.get("route_id"), meta.get("route_node"), meta.get("attempt_id")
    if not (route_id and node and attempt_id and home): return False
    safe_attempt = "".join(c if c.isalnum() or c in "._-" else "_" for c in attempt_id)
    linkage_path = _first_existing_dispatch_path(
        home, jobs, "completion", route_id, f"{node}.{safe_attempt}.attempt.json"
    )
    if linkage_path is None:
        return False
    directory = linkage_path.parent
    try:
        linkage = json.loads(linkage_path.read_text(encoding="utf-8"))
        history_path = Path(linkage["completion_marker_history"])
        if not history_path.is_file():
            # The recorded spelling may be a pointer-form path into a state
            # root that has since rotated away; look for the same basename
            # across every known state root before declaring it missing
            # (review Q-3, symmetric with capability-route.py:1494-1502's
            # N-1 fallback for the same dereference).
            #
            # Round-5 review (S-4): under this cycle's writer/reader
            # contract, `completion_marker` and `completion_marker_history`
            # are always written by the same call with the same directory
            # spelling, and `tools/install/distribution.py`'s release-rotation
            # carry-forward copies (and re-anchors) the whole `.dispatch` tree
            # -- history file, canonical marker, and attempt linkage -- as one
            # unit, never one without the others. No organically reachable
            # input has been found where this basename fallback finds a file
            # the `recorded_marker` identity check below (agent_home_equivalent
            # against `directory / f"{node}.json"`) does not already accept
            # through its own spelling tolerance. Kept intentionally
            # fail-closed rather than removed: it only ever widens what this
            # function accepts, so an input that reaches it and finds nothing
            # still falls through to `return False` below, and any future
            # writer/reader that stops moving these two keys in lockstep would
            # make this branch reachable again.
            for root in dispatch_state_roots(home, jobs):
                candidate = root / "completion" / route_id / history_path.name
                if candidate.is_file():
                    history_path = candidate
                    break
        marker = json.loads(history_path.read_text(encoding="utf-8"))
    except (KeyError, OSError, TypeError, ValueError):
        return False
    row_registered = str(meta.get("registered_worker", "")).lower() in {"1", "true"}
    expected_axes = {
        "dispatch_depth": int(meta["dispatch_depth"]),
        "transport": meta.get("transport"),
        "execution_surface": meta.get("execution_surface"),
        "registered_worker": row_registered,
        "fallback_hop": meta.get("fallback_hop") or None,
    }
    expected_link = {
        "schema_version": 2,
        "route_id": route_id,
        "node_id": node,
        "attempt_id": attempt_id,
        **expected_axes,
    }
    if any(linkage.get(key) != value for key, value in expected_link.items()):
        return False
    # Spelling-tolerant path identity (F-2): the linkage records the writer's
    # spelling of the marker directory, which may be pointer form while this
    # reader holds the resolved form (or vice versa).
    recorded_marker = linkage.get("completion_marker")
    if not isinstance(recorded_marker, str) or not agent_home_equivalent(
        recorded_marker, directory / f"{node}.json"
    ):
        return False
    expected_history = directory / f"{node}.{marker.get('sequence')}.json"
    if (
        not agent_home_equivalent(history_path, expected_history)
        or marker.get("schema_version") != 2
    ):
        return False
    if (
        marker.get("route_id") != route_id
        or marker.get("route_hash") != meta.get("route_hash")
        or marker.get("registry_digest") != meta.get("registry_digest")
        or marker.get("node_id") != node
        or marker.get("attempt_id") != attempt_id
        or marker.get("completion_gate") != meta.get("completion_gate")
        or any(marker.get(key) != value for key, value in expected_axes.items())
        or marker.get("evidence", {}).get("sha256") != linkage.get("evidence_sha256")
    ):
        return False
    try:
        evidence = Path(marker["evidence"]["path"])
        return (
            evidence.is_absolute()
            and evidence.is_file()
            and hashlib.sha256(evidence.read_bytes()).hexdigest()
            == linkage.get("evidence_sha256")
        )
    except (KeyError, OSError, TypeError):
        return False


def direct_child_rows(row, rows):
    """Return exact-attempt children, with legacy slug fallback only if safe."""

    owner_attempt = row["meta"].get("attempt_id")
    scoped = [
        other
        for other in rows or []
        if other is not row
        and other["repo"] == row["repo"]
        and other["worktree"] == row["worktree"]
        and other["meta"].get("parent") == row["slug"]
    ]
    exact = [
        other
        for other in scoped
        if owner_attempt
        and other["meta"].get("parent_attempt_id") == owner_attempt
    ]
    if exact:
        return exact
    if any(other["meta"].get("parent_attempt_id") for other in scoped):
        # A same-slug retry has started using exact bindings. Its children can
        # never provide route context or teardown authority for this owner.
        return []
    return scoped


def resolve_owner_route(row, rows=None, jobs=None):
    """Resolve an owner's immutable route from itself or its registered children.

    Real dispatch-depth-1 owner rows predate route compilation and therefore normally
    carry no route_id/route_file. Dispatch-depth-2 child rows do carry both. Derivation
    is limited to exact children in the same repo/worktree and fails closed on
    any disagreement, including disagreement with direct owner metadata.
    Terminal child rows remain valid provenance for an unstarted successor.
    """
    meta = row["meta"]
    sealed = (meta.get("owner_route_file"), meta.get("owner_route_id"), meta.get("owner_route_hash"))
    # Some read-only callers provide an in-memory legacy row and the canonical
    # jobs *location* before the registry file itself exists.  There can be no
    # durable attachment/advance state in that case, so preserve the existing
    # direct/child-derived route fallback.  Once the registry exists, lifecycle
    # evidence is authoritative and malformed/conflicting state still fails
    # closed below.
    if jobs and Path(str(jobs)).is_file() and meta.get("attempt_id"):
        try:
            anchor = OwnerRouteBinding(*sealed) if all(sealed) else None
            current, status = resolve_owner_route_lifecycle(
                jobs, owner_attempt_id=str(meta.get("attempt_id", "")),
                sealed_binding=anchor,
            )
            if status == "owner-route-advance-loop":
                return None, None, "route-context-conflict"
            if current is not None and status != "owner-route-advance-anchor-unresolvable":
                return current.route_id, current.route_file, "ok"
        except OwnerRouteBindingError:
            return None, None, "route-context-conflict"
    direct_id, direct_file = meta.get("route_id"), meta.get("route_file")
    candidates = set()
    if rows is not None:
        for other in direct_child_rows(row, rows):
            other_meta = other["meta"]
            child_id, child_file = other_meta.get("route_id"), other_meta.get("route_file")
            if child_id and child_file:
                candidates.add((child_id, child_file))
    if direct_id and direct_file:
        direct = (direct_id, direct_file)
        if candidates and candidates != {direct}:
            return None, None, "route-context-conflict"
        return direct_id, direct_file, "ok"
    if direct_id or direct_file:
        matches = {
            pair for pair in candidates
            if (not direct_id or pair[0] == direct_id)
            and (not direct_file or pair[1] == direct_file)
        }
        if len(matches) == 1 and matches == candidates:
            route_id, route_file = next(iter(matches))
            return route_id, route_file, "ok"
        return None, None, "route-context-conflict" if candidates else "no-route"
    if len(candidates) == 1:
        route_id, route_file = next(iter(candidates))
        return route_id, route_file, "ok"
    return None, None, "route-context-conflict" if candidates else "no-route"


def route_incomplete(row, home, rows=None, jobs=None):
    """SD-64/71: route nodes lacking a completion marker for a conductor row's route.

    Fails closed (returns an empty set) when the route record cannot be read
    safely, so an unreadable route never itself justifies an orphan claim.
    """
    route_id, route_file, context_status = resolve_owner_route(row, rows, jobs)
    if context_status != "ok" or not home: return set(), context_status
    try:
        record = json.loads(Path(route_file).read_text(encoding="utf-8")) if route_file else None
        if record and record.get("route_id") not in (None, route_id):
            return set(), "route-record-mismatch"
        node_ids = [n["id"] for n in record["nodes"]] if record else None
    except (OSError, ValueError, KeyError, TypeError):
        node_ids = None
    if node_ids is None:
        return set(), "route-record-unreadable"
    completion_roots = [root / "completion" / route_id for root in dispatch_state_roots(home, jobs)]
    missing = {
        node_id for node_id in node_ids
        if not any((root / f"{node_id}.json").is_file() for root in completion_roots)
    }
    return missing, "ok"


def has_orphaned_dependents(row, rows, incomplete_nodes, args):
    """SD-64/71: any registered open child, or an un-started successor node."""
    if not incomplete_nodes: return False
    children = direct_child_rows(row, rows)
    for other in children:
        if other["status"] not in OPEN: continue
        return True
    route_id, route_file, context_status = resolve_owner_route(row, rows, getattr(args, "jobs", None))
    if context_status != "ok": return False
    try:
        record = json.loads(Path(route_file).read_text(encoding="utf-8")) if route_file else None
        depends = {n["id"]: n.get("depends_on", []) for n in record["nodes"]} if record else None
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if depends is None: return False
    attempted_nodes = {
        r["meta"].get("route_node") for r in children
        if r["meta"].get("route_id") == route_id
    }
    for node_id in incomplete_nodes:
        if node_id in attempted_nodes: continue
        predecessors = depends.get(node_id, [])
        if all(predecessor not in incomplete_nodes for predecessor in predecessors): return True
    return False


def resume_boundary(route_file, incomplete_nodes):
    """SD-64/71: the first incomplete node in route order, or None if unreadable."""
    if not route_file or not incomplete_nodes: return None
    try:
        record = json.loads(Path(route_file).read_text(encoding="utf-8"))
        for node in record.get("nodes", []):
            if node.get("id") in incomplete_nodes: return node["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def carrier_terminal(row):
    """Shared carrier classification of one row's exact log (OPERATIONS §5.10)."""
    meta = row["meta"]
    return carrier_terminal_note(
        meta.get("log_file"),
        worktree=row.get("worktree"),
        artifact_root_metadata=meta.get("artifact_root"),
        worker_type=meta.get("worker_type"),
    )


def classify(row, args, newest_orders, rows=None):
    if row["status"] not in OPEN: return "terminal", "already-terminal", None
    meta = row["meta"]
    if row.get("legacy_read_only"):
        return "legacy-read-only", "legacy-attempt-row", None
    if row.get("attempt_contract_status") != "current":
        return "contract-invalid", row.get("attempt_contract_status", "invalid"), None
    # OPERATIONS §5.10: every post-hoc carrier classifies the exact log through
    # the shared helper, so a reviewer whose FAIL names a readable in-root
    # artifact is booked `completed-review-blocking` here exactly as the
    # wrapper tail, the supervisor join, and the progress watchdog book it.
    terminal, _ = carrier_terminal(row)
    if (
        meta.get("attempt_id")
        and meta.get("route_id")
        and meta.get("route_node")
        and terminal
    ):
        reason = f"{terminal['terminal_event']}:{terminal['verdict']}"
        if terminal.get("failure_note"):
            return "terminal-handoff", reason, terminal["failure_note"]
        # SD-70: a route-bound PASS completes only through its completion
        # marker. The envelope alone never proves completion, so a marker-less
        # PASS row is either a still-draining worker or a typed worker death —
        # never `completed-*`.
        if _marker_backed_repair(row, args.agent_home, args.jobs):
            return "marker-backed-stale", "completed-marker-linkage", "completed-marker"
        observed = observed_attempt_liveness(
            row["status"], meta, terminal_envelope=True
        )
        if observed.state == "alive":
            return "active", observed.reason, None
        if observed.state == "reconcile-needed":
            attempt_view = inspect_terminal_attempt(
                meta.get("log_file"),
                worktree=row.get("worktree"),
                artifact_root_metadata=meta.get("artifact_root"),
            )
            note = (
                "dead-missing-marker"
                if attempt_view.get("artifact_state") == "readable"
                else "dead-invalid-envelope"
            )
            return "terminal-handoff", f"{reason}:marker-missing", note
        return "terminal-draining", f"{reason}:marker-missing-{observed.reason}", None
    exact = classify_attempt_evidence(
        proc_inputs(row, args.agent_home, args.jobs, rows, args),
        getattr(args, "now", time.time()),
    )
    if exact and exact["state"] == "working": return "active", exact["rule"], None
    if exact and exact["state"] == "done": return "terminal-heartbeat", exact["rule"], "completed-terminal-heartbeat"
    if exact and exact["state"] == "dead":
        if _marker_backed_repair(row, args.agent_home, args.jobs):
            return "marker-backed-stale", "completed-marker-linkage", "completed-marker"
        if (rows is not None and meta.get("worker_type") == "owner"
                and not meta.get("route_node")):
            incomplete, record_status = route_incomplete(row, args.agent_home, rows, args.jobs)
            if record_status == "ok" and incomplete and has_orphaned_dependents(row, rows, incomplete, args):
                return "orphan", "dead-parent-orphaned", "dead-parent-orphaned"
        # Same closure, distinguishable cause: this row died with a fresh
        # heartbeat and no recorded PID we could read, so an audit that sees
        # `dead-exact-pid` would go looking for a PID that never meant anything.
        if exact["source"] == "parent":
            return "exact-dead", exact["rule"], "dead-parent-terminated"
        if exact["source"] == "namespace":
            return "exact-dead", exact["rule"], "dead-namespace-absent"
        return "exact-dead", exact["rule"], "dead-exact-pid"
    key = fold_key(meta)
    if all(key[:2]) and newest_orders.get(key) == row["order"]:
        proven, reason = terminal_marker(row, args.agent_home, args.jobs)
        if proven: return "stale-terminal", reason, "dead-stale-terminal"
    worktree = Path(row["worktree"])
    if worktree.is_absolute() and worktree.is_dir():
        try: verdict = cleanup.evaluate(worktree.resolve(), args.jobs, args.integration_ref)
        except (OSError, RuntimeError): verdict = None
        if verdict and verdict.eligible: return "merged", "sd29-safety-approved", "cleanup-merged"
        if verdict and verdict.reasons: return "unsafe", ",".join(verdict.reasons), None
    return "unsafe", "legacy-weak-or-unverifiable", None


def emit_current(rows, args):
    selected = [row for row in rows if matches(row, args)]
    if not args.all: selected = current(selected)
    payload = {"classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
               "filters": {"session": args.session, "route": args.route, "node": args.node,
                           "attempt": args.attempt, "job": args.job},
               "total": len(selected), "rows": selected}
    print(json.dumps(payload, sort_keys=True)); return 0


def emit_liveness(rows, args):
    """Emit a TSV view with superseded route/node attempts folded by default."""
    selected = [row for row in rows if matches(row, args)]
    if not args.all:
        selected = current(selected)
    for row in selected:
        print(row["raw"])
    return 0


def repair_stale_row(rows, args):
    """SD-70 follow-up: close one exact stale open/running row by marker
    evidence alone, bypassing the liveness classifier entirely -- for rows a
    release-rotation carry-forward (Phase 1 of this cycle's plan) should
    have already resolved but could not observe retroactively. Never widens
    `_marker_backed_repair`'s acceptance criteria: it is called unmodified,
    and every rejection below either runs strictly before it or reports one
    of its own axes without relaxing them.
    """

    # plan-check round-1 major-1: this typed rejection is reachable only
    # when --attempt is given together with another filter. When --attempt
    # is absent entirely, main()'s shared current-filter-required gate
    # returns exit 64 before this function is ever called.
    if not args.attempt or any((args.session, args.route, args.node, args.job, args.all)):
        print("decision=refused:exact-attempt-required")
        return 64

    matching = [row for row in rows if row["meta"].get("attempt_id") == args.attempt]
    if not matching:
        print("decision=refused:row-not-found")
        return 65
    if len(matching) > 1:
        print("decision=refused:row-not-unique")
        return 65
    row = matching[0]
    meta = row["meta"]

    def _record(decision, *, row_after=None, evidence=None, axis_skew=None):
        payload = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "operation": "repair-stale-row",
            "apply": bool(args.apply),
            "attempt_id": args.attempt,
            "decision": decision,
            "row_before": row["raw"],
            "row_after": row_after,
            "evidence": evidence or {},
            "axis_skew": axis_skew or [],
            "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
        }
        print(json.dumps(payload, sort_keys=True))
        audit_path = args.audit
        if audit_path is None and args.apply:
            audit_path = args.jobs.parent / "repair" / "registry-repair.jsonl"
        if audit_path is not None:
            cleanup.append_audit(audit_path, payload)
        return payload

    if row["status"] not in OPEN:
        _record("already-terminal")
        return 0

    route_id, route_node = meta.get("route_id"), meta.get("route_node")
    if not (route_id and route_node):
        _record("refused:row-route-identity-absent")
        return 65

    # `_marker_backed_repair` evaluates `int(meta["dispatch_depth"])` outside
    # its own try/except (dispatch-registry.py:307), so a row with route
    # identity but no (or a non-numeric) dispatch_depth raises an
    # unhandled KeyError/ValueError there. Validate before calling it rather
    # than changing the shared judge (Phase2 Step2.2 rule 6; mode=debug).
    try:
        int(meta.get("dispatch_depth"))
    except (TypeError, ValueError):
        _record("refused:row-metadata-invalid")
        return 65
    try:
        validate_attempt_metadata(meta)
    except DispatchContractError:
        _record("refused:row-metadata-invalid")
        return 65

    safe_attempt = "".join(c if c.isalnum() or c in "._-" else "_" for c in args.attempt)
    linkage_path = _first_existing_dispatch_path(
        args.agent_home, args.jobs, "completion", route_id,
        f"{route_node}.{safe_attempt}.attempt.json",
    )
    # `_first_existing_dispatch_path` returns its first candidate as a
    # best-guess default even when nothing exists (candidates[0] fallback)
    # -- only `None` (home unresolved) or a non-existent path means no
    # sidecar was actually found.
    if linkage_path is None or not linkage_path.is_file():
        _record("refused:marker-missing")
        return 65

    evidence = {"attempt_link": str(linkage_path)}
    try:
        linkage = json.loads(linkage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        linkage = {}
    evidence["completion_marker"] = linkage.get("completion_marker", "")
    evidence["completion_marker_history"] = linkage.get("completion_marker_history", "")

    # Read-only re-derivation of the axis comparison `_marker_backed_repair`
    # performs internally, purely to report *which* keys mismatched (Phase2
    # Step2.2 rule 9). This widens no acceptance criteria -- the judge below
    # still runs unmodified and has the final say.
    row_registered = str(meta.get("registered_worker", "")).lower() in {"1", "true"}
    expected_axes = {
        "dispatch_depth": int(meta["dispatch_depth"]),
        "transport": meta.get("transport"),
        "execution_surface": meta.get("execution_surface"),
        "registered_worker": row_registered,
        "fallback_hop": meta.get("fallback_hop") or None,
    }
    axis_skew = [key for key, value in expected_axes.items() if linkage.get(key) != value]
    if axis_skew:
        _record("refused:axis-skew", evidence=evidence, axis_skew=axis_skew)
        return 65

    try:
        history_path = Path(linkage["completion_marker_history"])
        marker = json.loads(history_path.read_text(encoding="utf-8"))
        evidence["evidence_path"] = marker.get("evidence", {}).get("path", "")
        evidence["evidence_sha256"] = marker.get("evidence", {}).get("sha256", "")
    except (KeyError, OSError, TypeError, ValueError):
        pass

    if not _marker_backed_repair(row, args.agent_home, args.jobs):
        _record("refused:marker-attempt-link-mismatch", evidence=evidence)
        return 65

    if not args.apply:
        _record("would-repair", evidence=evidence)
        return 0

    closed = close_attempt_row(
        args.jobs, args.attempt, "completed-marker",
        evidence={
            "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
            "reconcile_reason": "rotation-carry-stale-open",
            "detected_by": "registry-repair",
            "failure_class": "pass",
        },
    )
    if closed:
        materialize_after_terminal_close(args.jobs, args.attempt)
    fresh_rows = read_rows(args.jobs)
    fresh = next(
        (item for item in fresh_rows if item["meta"].get("attempt_id") == args.attempt),
        None,
    )
    if not closed:
        if fresh is not None and fresh["meta"].get("teardown_claim"):
            _record("refused:teardown-claimed", evidence=evidence)
            return 65
        _record("refused:close-rejected", evidence=evidence)
        return 65

    _record("repaired", row_after=fresh["raw"] if fresh else None, evidence=evidence)
    return 0


def reconcile(rows, args):
    selected = [row for row in rows if matches(row, args)]
    newest = {}
    for row in rows:
        key = fold_key(row["meta"])
        if all(key[:2]): newest[key] = row["order"]
    decisions = []
    for row in selected:
        category, reason, note = classify(row, args, newest, rows)
        closed = False
        cascade = []
        summary_owner = {"state": "not-applied", "reason": "dry-run"}
        revalidated = None
        if args.apply and note and row["meta"].get("attempt_id"):
            fresh_decision = {}

            def still_safe(_fields):
                fresh_rows = read_rows(args.jobs)
                fresh = next((item for item in fresh_rows
                              if item["meta"].get("attempt_id") == row["meta"]["attempt_id"]), None)
                if fresh is None:
                    fresh_decision.update(category="missing", reason="attempt-row-missing")
                    return False
                latest = {}
                for item in fresh_rows:
                    key = fold_key(item["meta"])
                    if all(key[:2]): latest[key] = item["order"]
                fresh_category, fresh_reason, fresh_note = classify(fresh, args, latest, fresh_rows)
                fresh_decision.update(category=fresh_category, reason=fresh_reason, note=fresh_note)
                return fresh_note == note and fresh_category == category

            reconcile_evidence = {"classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                                  "reconcile_reason": reason}
            if note == REVIEW_BLOCKING_NOTE:
                # Seal the artifact the reviewer named, as the join does, so the
                # owner-closure gate can re-verify it from the row.
                reconcile_evidence.update(carrier_terminal(row)[1])
            if note == "dead-invalid-envelope":
                # B47-3: this is the `classify()`-selected invalid-envelope
                # note, the second of the two producers §4 ②-A names.
                reconcile_evidence["failure_class"] = "invalid-envelope"
            closed = close_attempt_row_if(
                args.jobs, row["meta"]["attempt_id"], note, still_safe,
                evidence=reconcile_evidence,
            )
            revalidated = bool(closed)
            if closed:
                materialize_after_terminal_close(args.jobs, row["meta"]["attempt_id"])
            if not closed and fresh_decision:
                reason = f"revalidation-veto:{fresh_decision.get('category')}:{fresh_decision.get('reason')}"
            if closed and note == "dead-parent-orphaned":
                route_id, _, _ = resolve_owner_route(row, rows, args.jobs)
                cascade = cascade_orphan_children(row, route_id, args)
        if (args.apply and not closed and row["status"] in OPEN
                and row["attempt_contract_status"] == "current"
                and row["meta"].get("attempt_id")):
            summary_owner = ensure_attempt_owner(
                args.jobs, row["meta"]["attempt_id"])
        decisions.append({"attempt_id": row["meta"].get("attempt_id"), "slug": row["slug"],
                          "category": category, "reason": reason, "proposed_note": note,
                          "revalidated": revalidated, "closed": closed,
                          "cascade": cascade, "summary_owner": summary_owner})
    record = {"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "apply": args.apply, "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
              "attempted": len(selected), "closed": sum(item["closed"] for item in decisions),
              "decisions": decisions[:256]}
    if args.apply:
        # SD-111 P2 §2-b-2/§2-c: this `reconcile` call is the existing
        # bounded-cadence "dispatch reconcile path" -- the materialize
        # backstop and the single declared expiry actor share its cadence
        # rather than introducing a new driver process.
        record["pending_delivery"] = reconcile_pending_delivery(args.jobs)
    if args.audit:
        cleanup.append_audit(args.audit, record)
    print(json.dumps(record, sort_keys=True)); return 0


def _receiptless_namespace_cancel_reason(row, args):
    """Return ``""`` only for an explicit, extinct receiptless namespace row."""

    if row["status"] not in OPEN:
        return "attempt-already-terminal"
    if row.get("attempt_contract_status") != "current":
        return "attempt-contract-invalid"
    meta = row["meta"]
    if meta.get("registered_worker") != "1" or meta.get("pid_scope") != "namespace-local":
        return "not-registered-namespace-local"
    if not meta.get("pid", "").isdigit() or not meta.get("pid_start"):
        return "exact-process-identity-missing"
    try:
        observer_namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return "observer-namespace-unavailable"
    if meta.get("pid_observer_ns") in {None, "", observer_namespace}:
        return "namespace-not-foreign"
    if observer_namespace_extinct(meta) != "extinct":
        return "namespace-not-extinct"
    if _marker_backed_repair(row, args.agent_home, args.jobs):
        return "completion-marker-present"
    terminal = inspect_terminal_attempt(
        meta.get("log_file"),
        worktree=row.get("worktree"),
        artifact_root_metadata=meta.get("artifact_root"),
    )
    if terminal.get("state") != "absent":
        return f"terminal-envelope-{terminal.get('state') or 'unknown'}"
    descendants = attempt_tagged_descendants(meta)
    if descendants.state == "populated":
        return "attempt-descendant-live"
    process = attempt_process_quiescence(meta, terminal_receipt=True)
    if process.state == "live":
        return "process-alive"
    if process.state not in {"quiescent", "unverifiable"}:
        return "receiptless-namespace-not-proved"
    exact = classify_attempt_evidence(
        proc_inputs(row, args.agent_home, args.jobs), args.now
    )
    if exact and exact["state"] == "working":
        return "attempt-evidence-active"
    if exact and (
        exact["state"] == "done" or exact.get("source") == "terminal-observation"
    ):
        return "attempt-evidence-terminal"
    return ""


def _heartbeat_artifact_digest(row, args):
    """Return the sha256 the worker itself recorded at its last artifact heartbeat.

    `dispatch-progress.py heartbeat` is written by the worker under its own attempt
    id, so this is the one digest that is evidence about the worker rather than
    about whoever is reconciling it. Only a `phase=artifact` record counts, and its
    route binding must match the row.
    """

    meta = row["meta"]
    attempt = (meta.get("attempt_id") or "").replace("/", "_")
    if not attempt:
        return None, "attempt-id-missing"
    path = _first_existing_dispatch_path(
        args.agent_home, args.jobs, "heartbeats", f"{attempt}.json"
    )
    if path is None or not path.is_file():
        return None, "heartbeat-missing"
    try:
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "heartbeat-unreadable"
    if not isinstance(heartbeat, dict):
        return None, "heartbeat-unreadable"
    if heartbeat.get("attempt_id") != meta.get("attempt_id"):
        return None, "heartbeat-attempt-mismatch"
    if (
        heartbeat.get("route_id") != meta.get("route_id")
        or heartbeat.get("route_node") != meta.get("route_node")
    ):
        return None, "heartbeat-route-mismatch"
    if heartbeat.get("phase") != "artifact":
        return None, f"heartbeat-phase-{heartbeat.get('phase') or 'unknown'}"
    evidence = heartbeat.get("evidence")
    if not isinstance(evidence, str) or not evidence.startswith("sha256:"):
        return None, "heartbeat-evidence-not-a-digest"
    digest = evidence[len("sha256:"):].strip().lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None, "heartbeat-evidence-not-a-digest"
    return digest, ""


def _artifact_proof_seal_reason(row, args):
    """Return ``("", digest, observer_ns)`` only for a fully proved artifact seal.

    Every branch below is a refusal. The seal exists because a post-exit receipt
    can become permanently unissuable (a process that escaped the governed group
    while carrying the attempt tag), which leaves `reconcile` classifying the row
    `terminal-draining` forever and `dispatch_completion_join` pending forever --
    even for a worker that reported PASS and wrote its artifact. It must never
    fail open: an unprovable chain stays refused, exactly as today.
    """

    if row.get("legacy_read_only"):
        return "legacy-attempt-row", "", ""
    if row.get("attempt_contract_status") != "current":
        return "attempt-contract-invalid", "", ""
    meta = row["meta"]
    if meta.get("registered_worker") != "1" or meta.get("pid_scope") != "namespace-local":
        return "not-registered-namespace-local", "", ""
    if not (meta.get("route_id") and meta.get("route_node")):
        return "route-binding-missing", "", ""
    if post_exit_receipt_reason(meta):
        return "post-exit-receipt-present", "", ""
    if not meta.get("pid", "").isdigit() or not meta.get("pid_start"):
        return "exact-process-identity-missing", "", ""
    try:
        observer_namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        return "observer-namespace-unavailable", "", ""
    # "namespace-valid dead" is only observable from the namespace that recorded
    # the PID. From anywhere else the death was never provable, so there is
    # nothing to supersede the missing receipt with.
    if meta.get("pid_observer_ns") != observer_namespace:
        return "observer-namespace-mismatch", "", ""
    if meta.get("pid_ns") != observer_namespace:
        return "process-namespace-mismatch", "", ""
    governed = attempt_governed_process_quiescence(meta)
    if governed.state == "live":
        return f"governed-process-{governed.reason}", "", ""
    if governed.state != "quiescent":
        return f"governed-process-{governed.reason}", "", ""
    terminal = inspect_terminal_attempt(
        meta.get("log_file"),
        worktree=row.get("worktree"),
        artifact_root_metadata=meta.get("artifact_root"),
    )
    if terminal.get("state") != "valid":
        return f"terminal-envelope-{terminal.get('state') or 'unknown'}", "", ""
    if terminal.get("verdict") != "PASS":
        return f"terminal-verdict-{terminal.get('verdict') or 'unknown'}", "", ""
    if terminal.get("artifact_state") != "readable":
        return f"artifact-{terminal.get('artifact_state') or 'unknown'}", "", ""
    encoded = terminal.get("artifact_path_b64")
    if not isinstance(encoded, str) or not encoded:
        return "artifact-path-missing", "", ""
    try:
        artifact = Path(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError):
        return "artifact-path-undecodable", "", ""
    recorded, heartbeat_reason = _heartbeat_artifact_digest(row, args)
    if recorded is None:
        return heartbeat_reason, "", ""
    try:
        live = hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError:
        return "artifact-unreadable", "", ""
    if live != recorded:
        return "artifact-digest-mismatch", "", ""
    return "", live, observer_namespace


def seal_artifact_proof_receipt(rows, args):
    """Operator-only substitution of one permanently unissuable post-exit receipt.

    This IS completion evidence, unlike `--cancel-receiptless-namespace`: it
    records that the worker's own final artifact digest matches the artifact on
    disk, so the terminal gates can reach a terminal state. It writes no marker
    and no PASS verdict of its own -- the row still closes through the ordinary
    `reconcile` or `capability-route.py complete` path afterwards.
    """

    selected = [row for row in rows if matches(row, args)]
    if len(selected) != 1:
        reason = "attempt-row-not-unique"
        row = selected[0] if selected else None
        digest = observer_namespace = ""
    else:
        row = selected[0]
        reason, digest, observer_namespace = _artifact_proof_seal_reason(row, args)
    sealed = False
    revalidated = None
    if args.apply and row is not None and not reason:
        attempt_id = row["meta"].get("attempt_id", "")

        def still_provable(fields):
            fresh = {
                "status": fields[1],
                "repo": fields[2],
                "worktree": fields[3],
                "slug": fields[4],
                "meta": parse_registry_metadata(fields[5]),
                "attempt_contract_status": "current",
            }
            fresh_reason, fresh_digest, fresh_ns = _artifact_proof_seal_reason(fresh, args)
            return (
                not fresh_reason
                and fresh_digest == digest
                and fresh_ns == observer_namespace
            )

        sealed = annotate_attempt_row_if(
            args.jobs,
            attempt_id,
            {
                "post_exit_receipt_substitute": ARTIFACT_PROOF_RECEIPT,
                "artifact_proof_sha256": digest,
                "artifact_proof_observer_ns": observer_namespace,
                "artifact_proof_verdict": "PASS",
                "artifact_proof_sealed_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            still_provable,
            # The row this repairs is usually already closed: `complete` closes it
            # without receipt fields, which is exactly why the join stays pending.
            statuses=frozenset({"open", "running", "done"}),
        )
        revalidated = bool(sealed)
        if not sealed:
            reason = "revalidation-veto"
    decision = {
        "attempt_id": row["meta"].get("attempt_id") if row else args.attempt,
        "eligible": bool(row is not None and not reason),
        "reason": reason or "receipt-superseded-by-artifact-proof",
        "artifact_sha256": digest or None,
        "proposed_receipt": ARTIFACT_PROOF_RECEIPT,
        "revalidated": revalidated,
        "sealed": sealed,
    }
    print(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "apply": args.apply,
        "classifier_source": "operator-artifact-proof-seal-v1",
        "attempted": len(selected),
        "sealed": int(sealed),
        "decisions": [decision],
    }, sort_keys=True))
    return 0


def cancel_receiptless_namespace(rows, args):
    """Operator-only exact cancellation for extinct pre-receipt namespaces.

    This is deliberately not completion: it writes no marker, PASS verdict, or
    reap proof.  The terminal row disappears from Fleet's active set while
    successor readiness remains fail-closed on the missing receipt.
    """

    selected = [row for row in rows if matches(row, args)]
    if len(selected) != 1:
        reason = "attempt-row-not-unique"
        row = selected[0] if selected else None
    else:
        row = selected[0]
        reason = _receiptless_namespace_cancel_reason(row, args)
    closed = False
    revalidated = None
    receipt_digest = ""
    if args.apply and row is not None and not reason:
        attempt_id = row["meta"].get("attempt_id", "")
        proof = prove_attempt_quiescence(
            row["meta"],
            max_wait_seconds=args.cancellation_wait,
            allow_namespace_extinct=True,
        )
        if not proof.proven:
            reason = "cancellation-quiescence-unproven"
        else:
            try:
                receipt_digest = seal_cancellation_quiescence_receipt(
                    args.jobs, attempt_id, proof
                )
            except DispatchContractError as exc:
                reason = exc.reason

    if args.apply and row is not None and not reason:
        attempt_id = row["meta"].get("attempt_id", "")

        def still_receiptless(fields):
            fresh_meta = parse_registry_metadata(fields[5])
            fresh = {
                "status": fields[1],
                "repo": fields[2],
                "worktree": fields[3],
                "slug": fields[4],
                "meta": fresh_meta,
                "attempt_contract_status": "current",
            }
            return not _receiptless_namespace_cancel_reason(fresh, args)

        closed = close_attempt_row_if(
            args.jobs,
            attempt_id,
            "cancelled-receipt-unavailable",
            still_receiptless,
            evidence={
                "failure_class": "cancelled",
                "classifier_source": "operator-receiptless-cancel-v1",
                "reconcile_reason": "legacy-namespace-receipt-unavailable",
            },
        )
        revalidated = bool(closed)
        if closed:
            materialize_after_terminal_close(args.jobs, attempt_id)
        if not closed:
            reason = "revalidation-veto"
    decision = {
        "attempt_id": row["meta"].get("attempt_id") if row else args.attempt,
        "eligible": bool(row is not None and not reason),
        "reason": reason or "legacy-namespace-receipt-unavailable",
        "proposed_note": "cancelled-receipt-unavailable",
        "receipt_digest": receipt_digest or None,
        "revalidated": revalidated,
        "closed": closed,
    }
    print(json.dumps({
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "apply": args.apply,
        "classifier_source": "operator-receiptless-cancel-v1",
        "attempted": len(selected),
        "closed": int(closed),
        "decisions": [decision],
    }, sort_keys=True))
    return 0


def _automatic_receiptless_result(rows, args):
    """Prove and close one receiptless cancellation without projecting PASS."""

    selected = [row for row in rows if matches(row, args)]
    row = selected[0] if len(selected) == 1 else None
    reason = "attempt-row-not-unique" if row is None else _receiptless_namespace_cancel_reason(row, args)
    proof = None
    receipt_digest = ""
    closed = False
    revalidated = None
    if not reason:
        proof = prove_attempt_quiescence(
            row["meta"],
            max_wait_seconds=args.cancellation_wait,
            allow_namespace_extinct=True,
        )
        if not proof.proven:
            reason = "cancellation-quiescence-unproven"
    if args.apply and row is not None and not reason:
        attempt_id = row["meta"].get("attempt_id", "")
        try:
            receipt_digest = seal_cancellation_quiescence_receipt(
                args.jobs, attempt_id, proof
            )
        except DispatchContractError as exc:
            reason = exc.reason
        if not reason:
            def still_proven(fields):
                fresh = {
                    "status": fields[1],
                    "repo": fields[2],
                    "worktree": fields[3],
                    "slug": fields[4],
                    "meta": parse_registry_metadata(fields[5]),
                    "attempt_contract_status": "current",
                }
                if _receiptless_namespace_cancel_reason(fresh, args):
                    return False
                current = prove_attempt_quiescence(
                    fresh["meta"],
                    max_wait_seconds=args.cancellation_wait,
                    allow_namespace_extinct=True,
                )
                return bool(
                    current.proven
                    and proof is not None
                    and current.binding_digest == proof.binding_digest
                )

            closed = close_attempt_row_if(
                args.jobs,
                attempt_id,
                "cancelled-receipt-unavailable",
                still_proven,
                evidence={
                    "failure_class": "cancelled",
                    "note": "cancelled-receipt-unavailable",
                    "reconcile_reason": "automatic-cancelled-receipt-unavailable",
                    "classifier_source": AUTOMATIC_RECEIPTLESS_CLASSIFIER,
                    "receipt_state": "unavailable",
                    "marker_state": "absent",
                },
            )
            revalidated = bool(closed)
            if closed:
                materialize_after_terminal_close(args.jobs, attempt_id)
            if not closed:
                reason = "cancellation-quiescence-unproven"
    decision = {
        "attempt_id": row["meta"].get("attempt_id") if row else args.attempt,
        "eligible": bool(row is not None and not reason),
        "reason": reason or "automatic-cancelled-receipt-unavailable",
        "proposed_note": "cancelled-receipt-unavailable",
        "derived_gate": "BLOCKED",
        "retry_launch": 0,
        "marker_written": 0,
        "row_mutated": int(closed),
        "proof_source": proof.source if proof and proof.proven else None,
        "receipt_digest": receipt_digest or None,
        "revalidated": revalidated,
        "closed": closed,
    }
    return {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "apply": args.apply,
        "classifier_source": AUTOMATIC_RECEIPTLESS_CLASSIFIER,
        "attempted": len(selected),
        "closed": int(closed),
        "decisions": [decision],
    }


def automatic_cancel_receiptless(rows, args):
    record = _automatic_receiptless_result(rows, args)
    print(json.dumps(record, sort_keys=True))
    return 0


def recover_receiptless(rows, args):
    """Seal and claim one recovery retry; registration and spawn stay external."""

    try:
        route = json.loads(args.route_file.read_text(encoding="utf-8"))
        launch = route.get("launch_compatibility_tuple")
        jobs_binding = (
            (launch.get("jobs_path") or {}).get("path")
            if isinstance(launch, dict)
            else None
        )
    except (OSError, ValueError):
        jobs_binding = None
    if not isinstance(jobs_binding, str) or not jobs_binding:
        print(json.dumps({
            "apply": args.apply,
            "attempted": 0,
            "claimed": 0,
            "spawned": 0,
            "reason": "recovery-route-jobs-binding-missing",
        }, sort_keys=True))
        return 0
    if Path(jobs_binding).resolve(strict=False) != args.jobs.resolve(strict=False):
        print(json.dumps({
            "apply": args.apply,
            "attempted": 0,
            "claimed": 0,
            "spawned": 0,
            "reason": "recovery-route-jobs-mismatch",
        }, sort_keys=True))
        return 0

    selected = [row for row in rows if matches(row, args)]
    if len(selected) != 1:
        print(json.dumps({
            "apply": args.apply,
            "attempted": len(selected),
            "claimed": 0,
            "spawned": 0,
            "reason": "attempt-row-not-unique",
        }, sort_keys=True))
        return 0
    row = selected[0]
    metadata = row["meta"]
    existing_recovery = bool(
        metadata.get("recovery_id")
        and (
            metadata.get("retry_ordinal") == "1"
            or metadata.get("note") == "receipt-unavailable-retry-exhausted"
        )
    )
    already_cancelled = bool(
        row["status"] == "done"
        and metadata.get("classifier_source") == AUTOMATIC_RECEIPTLESS_CLASSIFIER
        and metadata.get("cancellation_receipt_digest")
        and (
            metadata.get("failure_class") == "cancelled"
            or existing_recovery
        )
    )
    cancellation = None
    if not already_cancelled:
        cancellation = _automatic_receiptless_result(rows, args)
        if not cancellation["closed"]:
            decision = cancellation["decisions"][0]
            print(json.dumps({
                "apply": args.apply,
                "attempted": 1,
                "claimed": 0,
                "spawned": 0,
                "reason": decision["reason"],
                "derived_gate": decision["derived_gate"],
                "retry_launch": 0,
                "cancellation": cancellation,
            }, sort_keys=True))
            return 0
        rows = read_rows(args.jobs)
        row = next(
            item for item in rows
            if item["meta"].get("attempt_id") == args.attempt
        )
        metadata = row["meta"]
    if not args.apply:
        print(json.dumps({
            "apply": False,
            "attempted": 1,
            "claimed": 0,
            "spawned": 0,
            "reason": "apply-required-for-recovery-claim",
        }, sort_keys=True))
        return 0

    source_route_id = metadata.get("route_id", "")
    source_route_hash = metadata.get("route_hash", "")
    node_or_group_leg = (
        metadata.get("route_node") or metadata.get("batch_route_node") or ""
    )
    cancellation_digest = metadata.get("cancellation_receipt_digest", "")
    recovery_identity = recovery_id(
        source_route_id=source_route_id,
        source_route_hash=source_route_hash,
        node_or_group_leg=node_or_group_leg,
        original_attempt_id=args.attempt,
        cancellation_receipt_digest=cancellation_digest,
    )
    budget = resolve_continuation_budget(
        route_file=args.route_file,
        route_id=source_route_id,
        route_hash=source_route_hash,
        expected_cwd=row["worktree"],
    )
    remaining = budget.retry_slots
    try:
        claim = claim_recovery_retry(
            args.jobs,
            recovery_id=recovery_identity,
            source_route_id=source_route_id,
            source_route_hash=source_route_hash,
            node_or_group_leg=node_or_group_leg,
            original_attempt_id=args.attempt,
            remaining_cascade=remaining,
        )
    except DispatchContractError as exc:
        print(json.dumps({
            "apply": True,
            "attempted": 1,
            "claimed": 0,
            "spawned": 0,
            "reason": exc.reason,
            "recovery_id": recovery_identity,
        }, sort_keys=True))
        return 0
    print(json.dumps({
        "apply": True,
        "attempted": 1,
        "claimed": int(claim.state == "claimed"),
        "spawned": 0,
        "reason": claim.reason,
        "recovery_id": claim.recovery_id,
        "retry_ordinal": claim.retry_ordinal,
        "retry_attempt_id": claim.retry_attempt_id or None,
        "start_permitted": claim.start_permitted,
        "remaining_cascade": remaining,
        "budget_source": budget.source,
    }, sort_keys=True))
    return 0


_CASCADE_TERMINAL_CATEGORIES = {
    "terminal-handoff",
    "terminal-heartbeat",
    "marker-backed-stale",
    "stale-terminal",
}


def _newest_orders(rows):
    newest = {}
    for row in rows:
        key = fold_key(row["meta"])
        if all(key[:2]):
            newest[key] = row["order"]
    return newest


def _cascade_terminal_note(row, rows, args):
    # A hash-bound marker wins even while the exact process is still alive and
    # flushing. The generic classifier checks process liveness first, so the
    # cascade must make this precedence explicit.
    if _marker_backed_repair(row, args.agent_home, args.jobs):
        return "completed-marker", "completed-marker-linkage"
    category, reason, note = classify(row, args, _newest_orders(rows), rows)
    if (
        category in _CASCADE_TERMINAL_CATEGORIES
        or (category == "exact-dead" and note == "dead-parent-terminated")
    ) and note:
        return note, reason
    return None, None


def _cascade_process_state(meta):
    """Classify only a process-group identity; never infer from a slug."""

    try:
        validate_attempt_metadata(meta)
    except DispatchContractError:
        return "contract-unverifiable", None, None

    local_pid = meta.get("pid", "")
    local_start = meta.get("pid_start", "")
    host_pid = meta.get("pid_host", "")
    host_start = meta.get("pid_host_start", "") or local_start
    if not local_pid and not host_pid:
        if meta.get("launch_claimed") == "0" or meta.get("launch_outcome") in {
            "never-launched", "reaped-before-publish"
        }:
            return "never-launched", None, None
        return "launch-indeterminate", None, None
    process = attempt_process_quiescence(meta)
    identity = process.identity
    if identity is None:
        syntactic_identity = (
            (local_pid.isdigit() and bool(local_start))
            or (host_pid.isdigit() and bool(host_start))
        )
        return (
            "scope-unverifiable" if syntactic_identity else "identity-missing",
            None,
            None,
        )
    pid, expected = identity.pid, identity.expected_start
    if process.state == "quiescent":
        return (
            "gone-pid-reused" if "reused" in process.reason else "gone",
            pid,
            expected,
        )
    if process.state != "live":
        return "group-unverifiable", pid, expected
    group_field = "pgid_host" if identity.source == "host" else "pgid"
    if meta.get(group_field) != str(pid):
        return "non-group-leader", pid, expected
    return "live-group", pid, expected


def _signal_exact_group(pid, expected_start, signum):
    """Revalidate immediately before killpg; return a closed status enum."""

    result = signal_exact_process_group(pid, expected_start, signum)
    return "gone" if result == "pid-reused" else result


def _wait_exact_group_end(pid, expected_start, timeout):
    deadline = time.monotonic() + max(timeout, 0.0)
    while time.monotonic() < deadline:
        visibility, actual, _state = process_observation(pid)
        if visibility == "present" and actual != expected_start:
            return True
        group = process_group_observation(pid)
        if group.state == "empty":
            return True
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    visibility, actual, _state = process_observation(pid)
    if visibility == "present" and actual != expected_start:
        return True
    return process_group_observation(pid).state == "empty"


def _teardown_claim_state(metadata):
    """Classify a durable cascade claim by its exact holder PID identity."""

    if not metadata.get("teardown_claim"):
        return "none"
    raw_pid = metadata.get("teardown_claim_pid", "")
    expected = metadata.get("teardown_claim_pid_start", "")
    if not raw_pid.isdigit() or not expected:
        return "unverifiable"
    visibility, actual, state = process_observation(int(raw_pid))
    if visibility == "inaccessible":
        return "unverifiable"
    if visibility == "missing" or actual != expected or state == "Z":
        return "stale"
    return "live"


def _close_cascade_child(
    args,
    owner,
    child_attempt,
    fallback_note,
    route_id,
    *,
    teardown_claim=None,
):
    """Close one child with marker/terminal precedence under the registry lock."""

    for _ in range(3):
        rows = read_rows(args.jobs)
        matches = [
            row for row in rows
            if row["meta"].get("attempt_id") == child_attempt
        ]
        if len(matches) != 1:
            return False, (
                "already-terminal" if not matches else "attempt-row-not-unique"
            )
        child = matches[0]
        if child["status"] not in OPEN:
            return False, "already-terminal"
        try:
            validate_attempt_metadata(child["meta"])
        except DispatchContractError:
            return False, "contract-unverifiable"
        recorded_claim = child["meta"].get("teardown_claim", "")
        if recorded_claim and recorded_claim != (teardown_claim or ""):
            claim_state = _teardown_claim_state(child["meta"])
            if claim_state == "stale" and not teardown_claim:
                if _release_cascade_claim(args.jobs, child_attempt, recorded_claim):
                    continue
                return False, "teardown-claim-revalidation-veto"
            return False, (
                "teardown-in-progress"
                if claim_state == "live"
                else "teardown-claim-unverifiable"
            )
        if teardown_claim and recorded_claim != teardown_claim:
            return False, "teardown-claim-lost"
        if (
            child["repo"] != owner["repo"]
            or child["worktree"] != owner["worktree"]
            or child["meta"].get("parent_attempt_id")
            != owner["meta"].get("attempt_id")
        ):
            return False, "parent-binding-changed"
        if route_id and child["meta"].get("route_id") not in {None, "", route_id}:
            return False, "route-context-conflict"
        terminal_note, terminal_reason = _cascade_terminal_note(child, rows, args)
        selected_note = terminal_note or fallback_note
        if not selected_note:
            return False, "no-terminal-evidence"
        decision = {}

        def still_safe(_fields):
            fresh_rows = read_rows(args.jobs)
            fresh_matches = [
                row for row in fresh_rows
                if row["meta"].get("attempt_id") == child_attempt
            ]
            if len(fresh_matches) != 1:
                decision["reason"] = (
                    "already-terminal" if not fresh_matches
                    else "attempt-row-not-unique"
                )
                return False
            fresh = fresh_matches[0]
            if fresh["status"] not in OPEN:
                decision["reason"] = "already-terminal"
                return False
            if fresh["attempt_contract_status"] != "current":
                decision["reason"] = "contract-unverifiable"
                return False
            fresh_claim = fresh["meta"].get("teardown_claim", "")
            if fresh_claim != (teardown_claim or ""):
                decision["reason"] = (
                    "teardown-in-progress" if fresh_claim else "teardown-claim-lost"
                )
                return False
            if (
                fresh["repo"] != owner["repo"]
                or fresh["worktree"] != owner["worktree"]
                or fresh["meta"].get("parent_attempt_id")
                != owner["meta"].get("attempt_id")
            ):
                decision["reason"] = "parent-binding-changed"
                return False
            if route_id and fresh["meta"].get("route_id") not in {None, "", route_id}:
                decision["reason"] = "route-context-conflict"
                return False
            fresh_terminal, _ = _cascade_terminal_note(fresh, fresh_rows, args)
            if fresh_terminal:
                decision["reason"] = f"terminal:{fresh_terminal}"
                return fresh_terminal == selected_note
            if selected_note != fallback_note:
                decision["reason"] = "terminal-evidence-changed"
                return False
            fresh_category, _, _ = classify(
                fresh, args, _newest_orders(fresh_rows), fresh_rows
            )
            state, _, _ = _cascade_process_state(fresh["meta"])
            decision["reason"] = state
            if fresh_category == "active":
                decision["reason"] = "stronger-child-live-evidence"
                return False
            return state in {"gone", "gone-pid-reused", "never-launched"}

        closed = close_attempt_row_if(
            args.jobs,
            child_attempt,
            selected_note,
            still_safe,
            evidence={
                "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                "parent_attempt_id": owner["meta"].get("attempt_id", ""),
                "reconcile_reason": terminal_reason or "post-exit-child-cascade",
            },
            teardown_claim=teardown_claim,
        )
        if closed:
            materialize_after_terminal_close(args.jobs, child_attempt)
            return True, selected_note
        if decision.get("reason", "").startswith("terminal:"):
            continue
        return False, decision.get("reason", "revalidation-veto")
    return False, "revalidation-retry-exhausted"


def _claim_cascade_signal(args, owner, child_attempt, route_id):
    """CAS one open child into exclusive teardown ownership before signalling."""

    owner_attempt = owner["meta"].get("attempt_id", "")
    token = f"cascade-{owner_attempt}-{uuid.uuid4().hex}"
    holder_pid = os.getpid()
    holder_start = process_start_ticks(holder_pid)
    if not holder_start:
        return None, {}, "teardown-claim-holder-unverifiable"
    decision = {}
    snapshot = {}

    def signal_safe(_fields):
        fresh_rows = read_rows(args.jobs)
        matches = [
            row
            for row in fresh_rows
            if row["meta"].get("attempt_id") == child_attempt
        ]
        if len(matches) != 1:
            decision["reason"] = "attempt-row-not-unique"
            return False
        fresh = matches[0]
        meta = fresh["meta"]
        if fresh["status"] not in OPEN:
            decision["reason"] = "already-terminal"
            return False
        if fresh["attempt_contract_status"] != "current":
            decision["reason"] = "contract-unverifiable"
            return False
        claim_state = _teardown_claim_state(meta)
        if claim_state in {"live", "unverifiable"}:
            decision["reason"] = (
                "teardown-in-progress"
                if claim_state == "live"
                else "teardown-claim-unverifiable"
            )
            return False
        if (
            fresh["repo"] != owner["repo"]
            or fresh["worktree"] != owner["worktree"]
            or meta.get("parent_attempt_id") != owner_attempt
        ):
            decision["reason"] = "parent-binding-changed"
            return False
        if route_id and meta.get("route_id") not in {None, "", route_id}:
            decision["reason"] = "route-context-conflict"
            return False
        terminal_note, _terminal_reason = _cascade_terminal_note(
            fresh, fresh_rows, args
        )
        if terminal_note:
            decision["reason"] = f"terminal:{terminal_note}"
            return False
        state, pid, expected = _cascade_process_state(meta)
        if state != "live-group" or pid is None or expected is None:
            decision["reason"] = state
            return False
        authority = exact_process_group_signal_authority(pid, expected)
        if authority != "authoritative":
            decision["reason"] = authority
            return False
        snapshot.update(pid=pid, expected=expected)
        decision["reason"] = "claimed"
        return True

    try:
        claimed = annotate_attempt_row_if(
            args.jobs,
            child_attempt,
            {
                "teardown_claim": token,
                "teardown_claimed_at": datetime.now(timezone.utc).isoformat(),
                "teardown_claim_pid": str(holder_pid),
                "teardown_claim_pid_start": holder_start,
            },
            signal_safe,
        )
    except DispatchContractError:
        return None, {}, "contract-unverifiable"
    if not claimed:
        return None, {}, decision.get("reason", "revalidation-veto")
    return token, snapshot, "claimed"


def _release_cascade_claim(jobs, child_attempt, token):
    """Release a still-owned teardown claim without touching a terminal row."""

    def still_owned(fields):
        return parse_registry_metadata(fields[5]).get("teardown_claim") == token

    try:
        return annotate_attempt_row_if(
            jobs,
            child_attempt,
            {
                "teardown_claim": "",
                "teardown_claimed_at": "",
                "teardown_claim_pid": "",
                "teardown_claim_pid_start": "",
            },
            still_owned,
        )
    except DispatchContractError:
        return False


def cascade_orphan_children(owner, route_id, args):
    """Bounded teardown of exact direct children for one dead owner attempt."""

    owner_attempt = owner["meta"].get("attempt_id")
    rows = read_rows(args.jobs)
    children = [
        row
        for row in direct_child_rows(owner, rows)
        if row["status"] in OPEN
        and row["meta"].get("parent_attempt_id") == owner_attempt
    ]
    decisions = []
    for child in children:
        attempt = child["meta"].get("attempt_id")
        if not attempt:
            decisions.append({"attempt_id": None, "status": "identity-missing"})
            continue
        if route_id and child["meta"].get("route_id") not in {None, "", route_id}:
            decisions.append({"attempt_id": attempt, "status": "route-context-conflict"})
            continue
        if not getattr(args, "apply", True):
            terminal_note, terminal_reason = _cascade_terminal_note(child, rows, args)
            if terminal_note:
                decisions.append({"attempt_id": attempt, "status": terminal_note,
                                  "closed": False, "plan": True})
                continue
            state, _pid, _expected = _cascade_process_state(child["meta"])
            category, _, _ = classify(child, args, _newest_orders(rows), rows)
            if category == "active" and state != "live-group":
                decisions.append({"attempt_id": attempt, "status": state,
                                  "closed": False, "plan": True})
                continue
            if state in {"gone", "gone-pid-reused", "never-launched"}:
                decisions.append({"attempt_id": attempt, "status": "dead-parent-exited",
                                  "closed": False, "plan": True})
                continue
            decisions.append({"attempt_id": attempt, "status": state,
                              "closed": False, "plan": True})
            continue
        closed, result = _close_cascade_child(args, owner, attempt, None, route_id)
        if closed:
            decisions.append({"attempt_id": attempt, "status": result, "closed": True})
            continue
        if result != "no-terminal-evidence":
            decisions.append(
                {"attempt_id": attempt, "status": result, "closed": False}
            )
            continue
        state, _pid, _expected = _cascade_process_state(child["meta"])
        category, _, _ = classify(child, args, _newest_orders(rows), rows)
        if category == "active" and state != "live-group":
            decisions.append({"attempt_id": attempt, "status": state, "closed": False})
            continue
        if state in {"gone", "gone-pid-reused", "never-launched"}:
            closed, result = _close_cascade_child(
                args, owner, attempt, "dead-parent-exited", route_id
            )
            decisions.append({"attempt_id": attempt, "status": result, "closed": closed})
            continue
        if state != "live-group":
            decisions.append({"attempt_id": attempt, "status": state, "closed": False})
            continue
        claim, snapshot, claim_status = _claim_cascade_signal(
            args, owner, attempt, route_id
        )
        if claim is None:
            decisions.append(
                {"attempt_id": attempt, "status": claim_status, "closed": False}
            )
            continue
        pid = snapshot["pid"]
        expected = snapshot["expected"]
        delivered_signal = False
        term = _signal_exact_group(pid, expected, signal.SIGTERM)
        if term == "signalled":
            delivered_signal = True
            ended = _wait_exact_group_end(pid, expected, args.cascade_grace)
        elif term in {"gone", "leader-gone"}:
            ended = _wait_exact_group_end(pid, expected, 0.0)
        else:
            _release_cascade_claim(args.jobs, attempt, claim)
            decisions.append(
                {"attempt_id": attempt, "status": term, "closed": False}
            )
            continue
        if not ended:
            killed = _signal_exact_group(pid, expected, signal.SIGKILL)
            if killed == "signalled":
                delivered_signal = True
                ended = _wait_exact_group_end(
                    pid, expected, args.cascade_kill_wait
                )
            elif killed in {"gone", "leader-gone"}:
                ended = _wait_exact_group_end(pid, expected, 0.0)
            else:
                _release_cascade_claim(args.jobs, attempt, claim)
                decisions.append(
                    {"attempt_id": attempt, "status": killed, "closed": False}
                )
                continue
        if not ended:
            _release_cascade_claim(args.jobs, attempt, claim)
            decisions.append(
                {
                    "attempt_id": attempt,
                    "status": "group-still-live",
                    "closed": False,
                }
            )
            continue
        closed, result = _close_cascade_child(
            args,
            owner,
            attempt,
            "dead-parent-terminated" if delivered_signal else "dead-parent-exited",
            route_id,
            teardown_claim=claim,
        )
        if not closed:
            _release_cascade_claim(args.jobs, attempt, claim)
        decisions.append({"attempt_id": attempt, "status": result, "closed": closed})
    return decisions


def emit_orphan_status(rows, args):
    """SD-64/71: single-attempt orphan verdict for liveness/preflight/Fleet surfaces.

    Reuses the same exact-attempt classifier and route_incomplete/
    has_orphaned_dependents primitives as ``reconcile`` so surfaces never
    re-derive the classification themselves. It is read-only by default;
    ``--apply`` conditionally closes only a revalidated orphan owner.
    """
    if not args.attempt:
        print("check=failed\nreason=attempt-required"); return 64
    matches = [
        r for r in rows if r["meta"].get("attempt_id") == args.attempt
    ]
    if not matches:
        print("check=ok\norphan=0\nreason=attempt-not-found"); return 0
    if len(matches) != 1:
        print("check=ok\norphan=0\nreason=attempt-row-not-unique"); return 0
    row = matches[0]
    if row["status"] in PARENT_EXTINCTION_TERMINAL_STATUSES and (
        row["meta"].get("note") == "dead-parent-orphaned"
        or row["meta"].get("worker_type") == "owner"
    ):
        try:
            validate_attempt_metadata(row["meta"])
        except DispatchContractError:
            print("check=ok\norphan=0\nreason=owner-contract-unverifiable")
            print("cascade_attempted=0\ncascade_closed=0\ncascade=[]")
            return 0
        if (
            row["meta"].get("dispatch_depth") != "1"
            or row["meta"].get("worker_type") != "owner"
        ):
            print("check=ok\norphan=0\nreason=owner-not-depth-one")
            print("cascade_attempted=0\ncascade_closed=0\ncascade=[]")
            return 0
        route_id, _, route_status = resolve_owner_route(row, rows, args.jobs)
        if route_status == "route-context-conflict":
            print("check=ok\norphan=0\nclosed=0")
            print("cascade_attempted=0\ncascade_closed=0")
            print('cascade=[{"attempt_id": null, "status": "route-context-conflict"}]')
            return 0
        cascade = cascade_orphan_children(row, route_id, args)
        print("check=ok\norphan=0\nclosed=0")
        print(f"cascade_attempted={len(cascade)}")
        print(f"cascade_closed={sum(bool(item.get('closed')) for item in cascade)}")
        print("cascade=" + json.dumps(cascade, sort_keys=True))
        return 0
    newest = {}
    for item in rows:
        key = fold_key(item["meta"])
        if all(key[:2]): newest[key] = item["order"]
    category, reason, note = classify(row, args, newest, rows)
    if note == "dead-parent-orphaned":
        route_id, route_file, _ = resolve_owner_route(row, rows, args.jobs)
        incomplete, _ = route_incomplete(row, args.agent_home, rows, args.jobs)
        boundary = resume_boundary(route_file, incomplete)
        closed = False
        cascade = []
        if args.apply and row["meta"].get("attempt_id"):
            def still_orphan(_fields):
                fresh_rows = read_rows(args.jobs)
                fresh = next(
                    (item for item in fresh_rows
                     if item["meta"].get("attempt_id") == row["meta"]["attempt_id"]),
                    None,
                )
                if fresh is None:
                    return False
                latest = {}
                for item in fresh_rows:
                    key = fold_key(item["meta"])
                    if all(key): latest[key] = item["order"]
                _, _, fresh_note = classify(fresh, args, latest, fresh_rows)
                return fresh_note == "dead-parent-orphaned"

            closed = close_attempt_row_if(
                args.jobs,
                row["meta"]["attempt_id"],
                "dead-parent-orphaned",
                still_orphan,
                evidence={
                    "classifier_source": ATTEMPT_CLASSIFIER_SOURCE,
                    "reconcile_reason": "post-exit-owner-watch",
                },
            )
            if closed:
                materialize_after_terminal_close(args.jobs, row["meta"]["attempt_id"])
                cascade = cascade_orphan_children(row, route_id, args)
        print("check=ok\norphan=1")
        print(f"route_id={route_id}")
        print(f"resume_boundary={boundary or '-'}")
        print(f"closed={int(closed)}")
        print(f"cascade_attempted={len(cascade)}")
        print(f"cascade_closed={sum(bool(item.get('closed')) for item in cascade)}")
        print("cascade=" + json.dumps(cascade, sort_keys=True))
    else:
        print("check=ok\norphan=0\nclosed=0\ncascade_attempted=0\ncascade_closed=0\ncascade=[]")
    return 0


def emit_orphan_scan(rows, args):
    """SD-64/71: fail-open registry-wide orphan count for preflight status.

    No filter is required (unlike ``reconcile``) since a status probe does
    not know a specific route ahead of time; it only ever reads.
    """
    newest = {}
    for item in rows:
        key = fold_key(item["meta"])
        if all(key[:2]): newest[key] = item["order"]
    orphans = []
    for row in rows:
        if row["status"] not in OPEN: continue
        meta = row["meta"]
        if meta.get("worker_type") != "owner" or meta.get("route_node"):
            continue
        _, _, note = classify(row, args, newest, rows)
        if note == "dead-parent-orphaned":
            route_id, route_file, _ = resolve_owner_route(row, rows, args.jobs)
            incomplete, _ = route_incomplete(row, args.agent_home, rows, args.jobs)
            boundary = resume_boundary(route_file, incomplete)
            orphans.append({"attempt_id": meta.get("attempt_id"), "route_id": route_id,
                            "slug": row["slug"], "resume_boundary": boundary or "-"})
    print(f"check=ok\norphaned_conductor_jobs={len(orphans)}")
    if orphans:
        print(f"orphaned_resume_boundary={orphans[0]['resume_boundary']}")
        print(json.dumps(orphans, sort_keys=True))
    return 0


def emit_archive_import(state_root, args):
    if not args.archive_source:
        print("check=failed\nreason=archive-source-required"); return 64
    archive_id, error, detail = inventory_import_archive(state_root, args.archive_source)
    if error:
        print(f"check=failed\nreason={error}\ndetail={detail}"); return 65
    from dispatch_registry_inventory import read_archive
    row_count = len(read_archive(state_root, archive_id))
    import hashlib as _hashlib
    content_digest = "sha256:" + _hashlib.sha256(Path(args.archive_source).read_bytes()).hexdigest()
    print(f"check=ok\narchive_id={archive_id}\nrow_count={row_count}\ncontent_digest={content_digest}")
    return 0


def emit_inventory(state_root, args):
    result = inventory_query(
        state_root, from_ts=args.from_ts, to_ts=args.to_ts, include_archive=args.include_archive,
    )
    print(
        "check=ok\n"
        f"inventory_complete={'true' if result.inventory_complete else 'false'}\n"
        f"reasons={','.join(result.reasons)}\n"
        f"rows={len(result.rows)}"
    )
    return 0


def main(argv):
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("operation", choices=("current", "liveness", "reconcile", "attempt-state", "orphan-status", "orphan-scan", "repair-stale-row", "archive-import", "inventory"))
    p.add_argument("--jobs", type=Path); p.add_argument("--global-jobs", type=Path); p.add_argument("--local-jobs", type=Path)
    p.add_argument("--session"); p.add_argument("--route")
    p.add_argument("--node"); p.add_argument("--attempt"); p.add_argument("--job"); p.add_argument("--all", action="store_true")
    p.add_argument("--apply", action="store_true"); p.add_argument("--audit", type=Path); p.add_argument("--integration-ref")
    p.add_argument("--cancel-receiptless-namespace", action="store_true")
    p.add_argument("--automatic-cancel-receiptless", action="store_true")
    p.add_argument("--recover-receiptless", action="store_true")
    p.add_argument("--seal-artifact-proof-receipt", action="store_true")
    p.add_argument("--route-file", type=Path)
    p.add_argument("--agent-home", type=Path); p.add_argument("--now", type=float, default=time.time(), help=argparse.SUPPRESS)
    p.add_argument("--pid", type=int); p.add_argument("--pid-start"); p.add_argument("--pid-scope")
    p.add_argument("--pid-observer-ns", help=argparse.SUPPRESS)
    p.add_argument("--cascade-grace", type=float, default=2.0, help=argparse.SUPPRESS)
    p.add_argument("--cascade-kill-wait", type=float, default=1.0, help=argparse.SUPPRESS)
    p.add_argument("--cancellation-wait", type=float, default=2.0, help=argparse.SUPPRESS)
    p.add_argument("--archive-source", type=Path); p.add_argument("--archive-id")
    p.add_argument("--from-ts"); p.add_argument("--to-ts")
    p.add_argument("--include-archive", action="store_true")
    args = p.parse_args(argv[1:]); args.agent_home = args.agent_home or resolve_agent_home()
    if args.cascade_grace < 0 or args.cascade_kill_wait < 0 or args.cancellation_wait < 0:
        print("check=failed\nreason=invalid-cascade-timeout"); return 64
    if args.operation in ("archive-import", "inventory"):
        state_root = resolve_dispatch_state_root(args.agent_home, explicit_jobs=args.jobs, environ=os.environ)
        if args.operation == "archive-import":
            return emit_archive_import(state_root, args)
        return emit_inventory(state_root, args)
    if args.operation == "attempt-state":
        if args.pid is None or not args.pid_start:
            print("check=failed\nreason=exact-identity-required"); return 64
        row = {"meta": {"pid": str(args.pid), "pid_start": args.pid_start,
                        "pid_scope": args.pid_scope, "attempt_id": args.attempt,
                        "route_id": args.route, "route_node": args.node}}
        verdict = classify_attempt_evidence(proc_inputs(row, args.agent_home, args.jobs), args.now)
        if verdict is None:
            print("check=failed\nreason=exact-identity-required"); return 65
        print("check=ok")
        print(f"state={verdict['state']}")
        print(f"source={verdict['source']}")
        print(f"rule={verdict['rule']}")
        print(f"classifier_source={verdict['classifier_source']}")
        print(f"pid={verdict['pid']}")
        print(f"proc_start={verdict['proc_start']}")
        print(f"actual_proc_start={verdict['actual_proc_start']}")
        return 0
    if args.global_jobs or args.local_jobs:
        if args.operation != "reconcile" or not args.global_jobs or not args.local_jobs:
            print("check=failed\nreason=legacy-reconcile-arguments-invalid"); return 64
        count, malformed = reconcile_local_registry(args.global_jobs.resolve(), args.local_jobs.resolve())
        print(f"check=ok\nglobal_registry={args.global_jobs.resolve()}\nlocal_registry={args.local_jobs.resolve()}\nreconciled={count}\nmalformed={malformed}")
        return 0
    if not args.jobs:
        print("check=failed\nreason=jobs-required"); return 64
    args.jobs = args.jobs.resolve()
    if args.operation not in ("liveness", "orphan-scan") and not any((args.session, args.route, args.node, args.attempt, args.job)):
        print("check=failed\nreason=current-filter-required"); return 64
    recovery_modes = sum(bool(value) for value in (
        args.cancel_receiptless_namespace,
        args.automatic_cancel_receiptless,
        args.recover_receiptless,
        args.seal_artifact_proof_receipt,
    ))
    if recovery_modes > 1:
        print("check=failed\nreason=receipt-recovery-mode-conflict"); return 64
    if args.cancel_receiptless_namespace and (
        args.operation != "reconcile"
        or not args.attempt
        or any((args.session, args.route, args.node, args.job, args.all))
    ):
        print("check=failed\nreason=exact-attempt-cancel-required"); return 64
    if args.seal_artifact_proof_receipt and (
        args.operation != "reconcile"
        or not args.attempt
        or any((args.session, args.route, args.node, args.job, args.all))
    ):
        print("check=failed\nreason=exact-attempt-seal-required"); return 64
    if args.automatic_cancel_receiptless and (
        args.operation != "reconcile"
        or not args.attempt
        or any((args.session, args.route, args.node, args.job, args.all))
    ):
        print("check=failed\nreason=exact-attempt-automatic-cancel-required"); return 64
    if args.recover_receiptless and (
        args.operation != "reconcile"
        or not args.attempt
        or not args.route_file
        or any((args.session, args.route, args.node, args.job, args.all))
    ):
        print("check=failed\nreason=exact-attempt-recovery-required"); return 64
    rows = read_rows(args.jobs)
    if args.operation == "current":
        return emit_current(rows, args)
    if args.operation == "liveness":
        return emit_liveness(rows, args)
    if args.operation == "orphan-status":
        return emit_orphan_status(rows, args)
    if args.operation == "orphan-scan":
        return emit_orphan_scan(rows, args)
    if args.operation == "repair-stale-row":
        return repair_stale_row(rows, args)
    if args.cancel_receiptless_namespace:
        return cancel_receiptless_namespace(rows, args)
    if args.automatic_cancel_receiptless:
        return automatic_cancel_receiptless(rows, args)
    if args.recover_receiptless:
        return recover_receiptless(rows, args)
    if args.seal_artifact_proof_receipt:
        return seal_artifact_proof_receipt(rows, args)
    return reconcile(rows, args)


if __name__ == "__main__": raise SystemExit(main(sys.argv))
