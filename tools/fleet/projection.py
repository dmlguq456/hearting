"""Single fail-closed work projection plus session/dispatch context for Fleet.

This module is deliberately source-agnostic: collectors supply already observed
entities and route evidence, while every display and JSON surface consumes the
result.  It never starts a provider and never writes harness state.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from .model import (
    ActiveNodeProjection,
    ContextEvidence,
    ContextProjection,
    DispatchJob,
    ProgressProjection,
    Session,
    WorkProjection,
)
from .token_budget import policy_band


ROUTE_RECORD_MISMATCH = "route-record-mismatch"
MULTIPLE_LEAF_CANDIDATES = "multiple-leaf-candidates"
MULTIPLE_CHILD_CWD_CANDIDATES = "multiple-child-cwd-candidates"
MULTIPLE_OWNER_ROUTES = "multiple-owner-routes"
OWNER_ROUTE_CONFLICT = "owner-route-conflict"
MULTIPLE_ARTIFACT_PLAN_DIRS = "multiple-artifact-plan-dirs"
MULTIPLE_SPEC_MARKERS = "multiple-spec-markers"
MARKER_ARTIFACT_TIE = "marker-artifact-mtime-tie"


def _realpath(value):
    return os.path.realpath(value) if value else ""


def _field(entity, name, default=None):
    if isinstance(entity, dict):
        return entity.get(name, default)
    return getattr(entity, name, default)


def _evidence(entity):
    value = _field(entity, "_context_evidence")
    if isinstance(value, ContextEvidence):
        return value
    if not isinstance(value, dict):
        pct = _field(entity, "ctx_pct")
        return ContextEvidence(used_pct=pct, source="legacy" if pct is not None else "unknown")
    return ContextEvidence(
        used_pct=value.get("used_pct"), source=value.get("source", "unknown"),
        sequence=value.get("sequence"), source_head_sequence=value.get("source_head_sequence"),
        observed_at=value.get("observed_at"), fresh_until=value.get("fresh_until"),
        invalid_reason=value.get("invalid_reason"),
    )


LIVE_CONTEXT_STATES = frozenset(("working", "idle", "blocked", "unused", "queued"))


def _is_live(entity):
    """F-62: does this row still describe a session that exists right now?"""
    return getattr(entity, "liveness", None) in LIVE_CONTEXT_STATES


def normalize_context(evidence, now=None, live=False):
    """Return ``(public, private)`` context values with exact ordering/freshness checks.

    F-62: `live` rows are exempt from the ELAPSED-time check. Context occupancy does not decay
    while a session sits quiet — a session at 33% is still at 33% until it runs another turn —
    so `fresh_until` expiry describes a rate-limit window, not this value. Claude liveness comes
    from the statusline registry while this evidence is stamped from transcript mtime, so a
    session quiet for 15 minutes stayed `idle` while its context silently blanked to `—`
    (2026-08-04 user report). Auto-compaction does not sneak past this: compaction writes to the
    transcript, so a quiet transcript means nothing changed. Rows that are NOT live keep the old
    expiry, and F-13 already drops the whole detail row for stale/dead sessions.
    """
    now = time.time() if now is None else now
    ev = evidence if isinstance(evidence, ContextEvidence) else _evidence({"_context_evidence": evidence})
    reason = ev.invalid_reason
    pct = ev.used_pct
    if reason is None and not isinstance(pct, (int, float)):
        reason = "missing-context"
    if reason is None and (isinstance(pct, bool) or pct < 0 or pct > 100):
        reason = "malformed-context"
    if reason is None and ev.observed_at is not None and ev.fresh_until is not None:
        # Observed after its own expiry is a self-contradictory record, not an aged one: that
        # stays rejected for live rows too, because it says the stamp itself cannot be trusted.
        if ev.observed_at > ev.fresh_until or (not live and now > ev.fresh_until):
            reason = "stale-context"
    if reason is None and ev.sequence is not None and ev.source_head_sequence is not None:
        try:
            if tuple(ev.sequence) < tuple(ev.source_head_sequence):
                reason = "selected-sequence-before-source-head"
        except TypeError:
            reason = "cross-source-sequence"
    if reason is not None:
        public = ContextProjection(None, "unknown", ev.source or "unknown")
        return public, ContextEvidence(**{**ev.__dict__, "used_pct": None, "invalid_reason": reason})
    value = int(round(pct))
    public = ContextProjection(value, policy_band(value), ev.source or "unknown")
    return public, ev


def _route_record_values(rid, path, expected_hash, route_records=None):
    """Load one exact route tuple without conflating owner and stage metadata."""
    from . import route

    if not rid:
        return None, None
    records = route_records or {}
    if isinstance(records, dict) and rid in records:
        record = records[rid]
        if (record.get("route_id") != rid
                or (expected_hash and record.get("route_hash") != expected_hash)):
            return None, ROUTE_RECORD_MISMATCH
        return record, None
    if not path:
        return None, ROUTE_RECORD_MISMATCH
    diagnostic = getattr(route, "load_diagnostic", None)
    if diagnostic:
        result = diagnostic(path, expect_hash=expected_hash, expect_id=rid)
        return result.record, (None if result.valid else ROUTE_RECORD_MISMATCH)
    record = route.load(path, expect_hash=expected_hash, expect_id=rid)
    return record, (None if record is not None else ROUTE_RECORD_MISMATCH)


def _route_record(entity, route_records=None):
    """Load one record, retaining a diagnostic reason without weakening old route.load()."""
    return _route_record_values(
        _field(entity, "route_id"), _field(entity, "route_file"),
        _field(entity, "route_hash"), route_records=route_records,
    )


def _active_node(node, state, job=None):
    progress = None
    raw_progress = node.get("progress")
    if isinstance(raw_progress, dict):
        progress = {"done": raw_progress.get("done", 0), "total": raw_progress.get("total", 0)}
    return ActiveNodeProjection(
        id=node.get("id"), depends_on=tuple(node.get("depends_on") or ()), level=node.get("level"),
        unit=node.get("unit"), unit_choices=tuple(node.get("unit_choices") or ()),
        gate=node.get("completion_gate", node.get("gate")),
        write_scope=node.get("write_scope"), state=state, progress=progress,
    )


def _record_view(record, route_id, jobs, node_evidence=None, now=None, degradations=None):
    """Use route.py's pure state resolver so projection and legacy route views agree.

    Resolve this record's gate marks (2026-07-24) and pass them through, so a
    completion-marked node that died on an earlier attempt renders `done` rather than
    `✕` on the owning session/dispatch row — parity with the group/process route views,
    which already resolve marks via `resolve_and_build_views`."""
    from . import route
    jobs = list(jobs)
    marks = route.resolve_gate_marks(
        {route_id: record}, jobs=jobs,
        node_evidence={route_id: node_evidence or {}},
    ).get(route_id)
    return route._record_view(record, route_id, jobs, node_evidence or {},
                              time.time() if now is None else now,
                              gate_marks_for_route=marks,
                              degradations_for_route=(degradations or {}).get(route_id, ()))


def _record_nodes(record, route_id, jobs, node_evidence=None, now=None, degradations=None):
    if not isinstance(record, dict):
        return (), None
    view = _record_view(record, route_id, jobs, node_evidence=node_evidence, now=now,
                        degradations=degradations)
    all_nodes = []
    for node in view.get("nodes", []):
        all_nodes.append(dict(node))
    projections = []
    for node in all_nodes:
        projections.append(ActiveNodeProjection(
            id=node.get("id"), depends_on=tuple(node.get("depends_on") or ()),
            level=node.get("level"), unit=node.get("unit"),
            unit_choices=tuple(node.get("unit_choices") or ()), gate=node.get("gate"),
            write_scope=node.get("write_scope"), state=node.get("state"),
            progress=node.get("progress"),
            parallel_group=node.get("parallel_group") or node.get("replica_group"),
            replica_group=node.get("replica_group"),
            model_profile=node.get("model_profile"), perspective=node.get("perspective"),
            degradation=node.get("degradation"),
        ))
    return tuple(projections), view.get("progress")


def _active_stage_label(active_nodes):
    """Describe every current owner node in the sealed record order.

    Leaf projections may use their validated assigned contract, but an owner row
    represents the whole current route level.  A depth-2 inline fallback has no
    child process row, but its exact degradation record still names the node that
    the owner is executing; callers therefore include both ``active`` and
    ``degraded`` nodes.  Keeping this derivation here makes
    the owner projection independent of collector/job iteration order.

    Parallel legs (shared ``parallel_group``; legacy ``replica_group`` alias)
    collapse into one ``<group>(N-way)``
    label — the individual legs already surface as their own dispatch rows
    (user 2026-07-24), so the owner label names the group once.
    """
    ids = []
    seen_groups = set()
    for node in active_nodes:
        if not node.id:
            continue
        group = getattr(node, "parallel_group", None) or getattr(node, "replica_group", None)
        if not group:
            ids.append(node.id)
            continue
        if group in seen_groups:
            continue
        seen_groups.add(group)
        members = [n for n in active_nodes
                   if (getattr(n, "parallel_group", None) or getattr(n, "replica_group", None)) == group and n.id]
        ids.append("%s(%d-way)" % (group, len(members)) if len(members) > 1
                   else node.id)
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    return "{%s}" % ",".join(ids)


def _owner_active_selection(active_nodes):
    """Derive the owner's scalar (route_node, node_state) from sealed current nodes.

    A single current node projects its own id/state.  Current legs that share one
    ``parallel_group`` collapse into the same ``<group>(N-way)`` id used by
    ``_active_stage_label``.  If any leg is an exact inline degradation, the
    group is degraded rather than falsely reported as fully active.  With no
    current node, or with more than one independent current node/group, a single
    node id cannot be selected, so this returns ``(None, "unknown")`` rather than
    picking one arbitrarily.
    """
    named = [node for node in active_nodes if node.id]
    if not named:
        return None, "unknown"
    groups = {getattr(node, "parallel_group", None) or getattr(node, "replica_group", None)
              for node in named}
    if len(groups) != 1:
        return None, "unknown"
    group = next(iter(groups))
    if group is None:
        if len(named) == 1:
            return named[0].id, named[0].state
        return None, "unknown"
    states = {node.state for node in named}
    group_state = "degraded" if "degraded" in states else "active"
    return ("%s(%d-way)" % (group, len(named)) if len(named) > 1 else named[0].id), group_state


def _load_evidence_records(node_evidence, route_records):
    """Load records named only by terminal jobs.log evidence, without inventing routes."""
    from . import route
    records = dict(route_records or {})
    for rid, nodes in (node_evidence or {}).items():
        if rid in records:
            continue
        for evidence in (nodes or {}).values():
            path = _field(evidence, "route_file")
            if not path:
                continue
            record = route.load(path, expect_hash=_field(evidence, "route_hash"), expect_id=rid)
            if record is not None:
                records[rid] = record
                break
    return records


def _evidence_owner_candidates(entity, node_evidence, route_records):
    """Return route IDs whose terminal node evidence names this owner/conductor."""
    owner_ids = {_field(entity, "session_id"), _field(entity, "slug")}
    owner_ids.discard(None)
    candidates = []
    for rid, nodes in (node_evidence or {}).items():
        if any(_field(ev, "parent") in owner_ids for ev in (nodes or {}).values()):
            record = route_records.get(rid)
            if record is not None:
                candidates.append((rid, record, nodes or {}))
    return candidates


def _owner_lineage_projection(entity, jobs, route_records, node_evidence, now, degradations):
    """Collapse owner-linked exact routes through a verified local lineage.

    This is deliberately fed only by the owner's children and exact terminal
    evidence.  It never discovers route files globally, and therefore cannot
    attach an abandoned compiled route merely because it is present on disk.
    """
    # Only depth-1 owners and interactive sessions own this attribution.  A
    # route-bearing stage child can itself have descendants, but must retain
    # the ordinary stage/leaf resolver rather than becoming a second owner.
    if _field(entity, "depth") not in (None, 1):
        return None, False
    candidates = {}
    for child in _owner_children(entity, jobs):
        rid = _field(child, "route_id")
        if not rid:
            continue
        record, failure = _route_record_values(
            rid, _field(child, "route_file"), _field(child, "route_hash"),
            route_records=route_records,
        )
        if record is None:
            return None, True
        candidates[rid] = record
    for rid, record, _evidence in _evidence_owner_candidates(
            entity, node_evidence, route_records or {}):
        if record is None:
            return None, True
        owner_ids = {_field(entity, "session_id"), _field(entity, "slug")}
        owner_ids.discard(None)
        exact_evidence = [
            ev for ev in (_evidence or {}).values()
            if _field(ev, "parent") in owner_ids
        ]
        if not exact_evidence or any(
            _field(ev, "route_hash")
            and _field(ev, "route_hash") != record.get("route_hash")
            for ev in exact_evidence
        ):
            return None, True
        candidates[rid] = record
    if not candidates:
        return None, False
    if not any(record.get("source_route_supersession") for record in candidates.values()):
        # Ordinary single-route projection and F-88 remain owned by the
        # established path below. Identity checks here apply only when Fleet
        # is being asked to prove a successor collapse.
        return None, False
    if any(
        _field(child, "route_id") and not _field(child, "route_hash")
        for child in _owner_children(entity, jobs)
    ):
        return None, True

    owner_attempt = _field(entity, "attempt_id")
    owner_worktree = _field(entity, "worktree") or _field(entity, "cwd")
    owner_capability = (
        _field(entity, "capability") or _field(entity, "capability_owner")
        or (_field(entity, "key") if str(_field(entity, "key") or "").startswith("autopilot-") else None)
    )
    owner_mode = _field(entity, "capability_mode")
    owner_artifact_root = _field(entity, "artifact_root")
    if not all((owner_attempt, owner_worktree, owner_capability, owner_mode)):
        return None, True
    for record in candidates.values():
        if record.get("owner_attempt_id") != owner_attempt:
            return None, True
        if owner_worktree and os.path.realpath(record.get("cwd") or "") != os.path.realpath(owner_worktree):
            return None, True
        if owner_capability and record.get("capability") != owner_capability:
            return None, True
        if owner_mode and record.get("capability_mode") != owner_mode:
            return None, True
        if (owner_artifact_root
                and os.path.realpath(record.get("artifact_root") or "")
                != os.path.realpath(owner_artifact_root)):
            return None, True

    outgoing = {}
    incoming = {}
    for rid, record in candidates.items():
        edge = record.get("source_route_supersession")
        if not edge:
            continue
        source_id, source_hash = edge.get("from_route_id"), edge.get("from_route_hash")
        target_id = record.get("route_id")
        source = candidates.get(source_id)
        if source is None or source.get("route_hash") != source_hash:
            return None, True
        if (record.get("source_route_id"), record.get("source_route_hash")) != (
                source_id, source_hash):
            return None, True
        if (
                edge.get("edge_version") != 1
                or edge.get("operation") != "continuation"
                or edge.get("source_verdict_preserved") is not True
                or edge.get("to_continuation_id") != record.get("continuation_id")):
            return None, True
        edges = record.get("supersession_edges") or []
        source_edges = source.get("supersession_edges") or []
        if edges != [*source_edges, edge]:
            return None, True
        if (
                not source.get("route_family_key")
                or source.get("route_family_key") != record.get("route_family_key")):
            return None, True
        reused = record.get("reused_nodes")
        if not isinstance(reused, list):
            return None, True
        evidence_digest = "sha256:" + hashlib.sha256(json.dumps(
            reused, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if record.get("source_evidence_digest") != evidence_digest:
            return None, True
        try:
            monotonic = int(record.get("advance_generation")) == int(
                source.get("advance_generation")) + 1
        except (TypeError, ValueError):
            monotonic = False
        if not monotonic:
            return None, True
        if source_id in outgoing and outgoing[source_id] != target_id:
            return None, True
        outgoing[source_id] = target_id
        incoming.setdefault(target_id, set()).add(source_id)

    if not outgoing:
        # Preserve the ordinary single-route/F-88 path when no lineage fact is
        # present; this helper owns only explicit supersession collapse.
        return None, False

    # Every candidate with a valid edge must be part of one chain.  A loop is
    # a typed ambiguity, as are competing terminal successors.
    terminals = [rid for rid in candidates if rid not in outgoing]
    if len(terminals) != 1:
        return None, True
    terminal = terminals[0]
    seen = set()
    current = terminal
    while current in incoming:
        predecessors = incoming[current]
        if len(predecessors) != 1 or current in seen:
            return None, True
        seen.add(current)
        current = next(iter(predecessors))
    if len(seen) != len(candidates) - 1:
        return None, True
    record = candidates[terminal]
    same_jobs = [j for j in jobs if _field(j, "route_id") == terminal]
    return _projection_from_record(
        entity, record, terminal, same_jobs,
        node_evidence=(node_evidence or {}).get(terminal, {}),
        now=now, owner=True, degradations=degradations,
    ), False


def _latest_exact_owner_evidence_route(entity, node_evidence, route_records):
    """Return the newest exact owner-linked route carried by registry evidence.

    A depth-1 owner's launch binding does not change when the owner re-plans onto
    a successor route.  F-88 therefore adopts the unanimous route of its live
    children.  Between two child attempts, however, the live set is empty and
    that rule used to snap the owner back to its launch route for a Fleet tick,
    briefly resurfacing an old failed plan/plan-check node (user report,
    2026-08-29).

    Terminal-surviving node evidence closes only that observation gap.  It must
    match the exact owner attempt and canonical registry, and registry row order
    (the stable registration order) selects the latest generation.  A timestamp
    cannot do this: an older generation's straggler may terminate later.  The
    selected route tuple is still id/hash verified by ``_route_record_values``;
    missing or conflicting proof stays fail-closed.
    """
    owner_attempt = _field(entity, "attempt_id")
    owner_registry = _realpath(_field(entity, "_registry_path"))
    if not owner_attempt or not owner_registry:
        return None

    ranked = []
    for rid, nodes in (node_evidence or {}).items():
        if not isinstance(rid, str) or not isinstance(nodes, dict):
            continue
        for evidence in nodes.values():
            if not isinstance(evidence, dict):
                continue
            order = evidence.get("registry_order")
            if (evidence.get("parent_attempt_id") != owner_attempt
                    or _realpath(evidence.get("_registry_path")) != owner_registry
                    or not isinstance(order, int) or isinstance(order, bool)):
                continue
            ranked.append((order, rid, evidence))
    if not ranked:
        return None

    newest_order = max(item[0] for item in ranked)
    newest = [item for item in ranked if item[0] == newest_order]
    route_ids = {item[1] for item in newest}
    if len(route_ids) != 1:
        return (None, None, OWNER_ROUTE_CONFLICT)
    _order, rid, carrier = newest[-1]
    record, failure = _route_record_values(
        rid, carrier.get("route_file"), carrier.get("route_hash"),
        route_records=route_records,
    )
    return (rid, record, failure)


def _terminal_route_projection(value):
    """Whether terminal-only evidence is history rather than current session work."""
    backing = getattr(value, "_route_view", None) or {}
    nodes = (backing.get("view") or {}).get("nodes") or ()
    return bool(nodes) and all(node.get("state") in {"done", "failed"} for node in nodes)


def _projection_from_record(entity, record, route_id, jobs, node_evidence=None, now=None,
                            route_node=None, owner=False, degradations=None):
    from . import route
    view = _record_view(record, route_id, jobs, node_evidence=node_evidence, now=now,
                        degradations=degradations)
    nodes = tuple(view.get("nodes") or ())
    projections = tuple(ActiveNodeProjection(
        id=node.get("id"), depends_on=tuple(node.get("depends_on") or ()),
        level=node.get("level"), unit=node.get("unit"),
        unit_choices=tuple(node.get("unit_choices") or ()), gate=node.get("gate"),
        write_scope=node.get("write_scope"), state=node.get("state"), progress=None,
        parallel_group=node.get("parallel_group") or node.get("replica_group"),
        replica_group=node.get("replica_group"),
        model_profile=node.get("model_profile"), perspective=node.get("perspective"),
        degradation=node.get("degradation"),
    ) for node in nodes)
    selected = next((node for node in projections if node.id == route_node), None)
    contract = _field(entity, "assigned_contract")
    active_nodes = tuple(node for node in projections if node.state == "active")
    owner_state = None
    if owner:
        # Inline work has no registered child row.  The exact degradation
        # tuple is nevertheless the current route frontier and must feed the
        # owner's stage column; otherwise Fleet looks frozen until completion.
        # Completion/failed evidence already wins in route._node_state(), so an
        # old degradation cannot keep a terminal node current here.
        active_nodes = tuple(
            node for node in projections if node.state in {"active", "degraded"}
        )
        label = _active_stage_label(active_nodes)
        route_node, owner_state = _owner_active_selection(active_nodes)
        selected = next((node for node in projections if node.id == route_node), None)
    elif contract and selected is not None:
        label = contract
    elif selected is not None:
        label = route_node
    elif len(active_nodes) == 1:
        label = active_nodes[0].id
    elif active_nodes:
        label = "{%s}" % ",".join(node.id for node in active_nodes)
    else:
        label = None
    node_state = owner_state if owner else (selected.state if selected else None)
    return WorkProjection(
        source="route-exact", route_id=record.get("route_id", route_id),
        route_hash=record.get("route_hash"), route_node=route_node,
        attempt_id=_field(entity, "attempt_id"), assigned_contract=contract,
        unit=selected.unit if selected else _field(entity, "unit"), stage_label=label,
        node_state=node_state, active_nodes=active_nodes,
        progress=ProgressProjection(**(view.get("progress") or {"done": 0, "total": len(nodes)})),
        _route_view={"record": record, "nodes": nodes, "view": view},
    )


_ARTIFACT_READER = None


def _artifact_reader():
    """`utilities/artifact_reader.py` (W7D read-side layout resolver), or None.

    Same lazy `sys.path` shape as `_grounding_home`. Fleet only reads the
    artifact root, so a checkout without the resolver degrades to the legacy
    top-level buckets instead of raising."""
    global _ARTIFACT_READER
    if _ARTIFACT_READER is None:
        _ARTIFACT_READER = False
        for candidate in Path(__file__).resolve().parents:
            utilities_dir = candidate / "utilities"
            if not (utilities_dir / "artifact_reader.py").is_file():
                continue
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            try:
                import artifact_reader
            except Exception:
                break
            _ARTIFACT_READER = artifact_reader
            break
    return _ARTIFACT_READER or None


def _artifact_candidates(entity, artifact_root=None):
    slug = _field(entity, "slug") or _field(entity, "key")
    if not slug:
        return []
    roots = []
    for value in (artifact_root, _field(entity, "artifact_root"), _field(entity, "cwd")):
        if value:
            value = os.path.realpath(os.path.expanduser(str(value)))
            roots.extend((value, os.path.join(value, ".agent_reports")))
    candidates = set()
    reader = _artifact_reader()
    for root in roots:
        # W7D: plan cycles live under campaigns/*/cycles/*/artifacts/plans; the
        # top-level plans/ stays a read-only fallback (both come back from
        # glob_bucket). `root` itself may already be the plans dir.
        matches = (reader.glob_bucket(Path(root), "plans", "*_%s" % slug) if reader is not None
                   else glob.glob(os.path.join(root, "plans", "*_%s" % slug)))
        for path in [str(m) for m in matches] + glob.glob(os.path.join(root, "*_%s" % slug)):
            if os.path.isdir(path):
                candidates.add(os.path.realpath(path))
    return sorted(candidates)


def exact_artifact_candidates(entity, artifact_root=None):
    """Expose exact-cardinality stage candidates for QA and hermetic tests."""
    return _artifact_candidates(entity, artifact_root=artifact_root)


_ARTIFACT_STAGE_MARKERS = (("report", ("report", "verification", "pipeline_summary")),
                           ("test", ("test",)),
                           ("exec", ("execute", "dev_log", "checklist")),
                           ("plan", ("plan",)))


def _artifact_stage_label(name):
    lowered = name.lower()
    for label, markers in _ARTIFACT_STAGE_MARKERS:
        if any(marker in lowered for marker in markers):
            return label
    return None


def _artifact_stage(path):
    """The stage this plan directory is CURRENTLY at — the one most recently written to.

    F-77 (user 2026-08-14, "메인에서 inline으로 도는 stages … 틀리는 경우도 종종 있고"):
    this used to answer from mere EXISTENCE, in a fixed report>test>exec>plan order. Every
    artifact of a finished stage survives into the next one, so a directory that had ever
    produced a `report` name reported `report` forever after — and a cycle that pre-created
    its output files reported its LAST stage from its first minute. Recency is the honest
    signal: whichever stage's files were touched last is where the work actually is.
    """
    best_label, best_mtime = None, None
    for item in glob.glob(os.path.join(path, "*")):
        name = os.path.basename(item)
        is_dir = os.path.isdir(item)
        if is_dir and name in {"dev_logs", "test_logs", "shards", "_internal"} and not _has_entries(item):
            continue
        label = _artifact_stage_label(name)
        if label is None:
            continue
        # A stage directory's recency is its CONTENT's recency: `dev_logs/` keeps the mtime
        # of the day it was created, while the log written into it just now is the evidence.
        mtime = _artifact_latest_mtime(item) if is_dir else _safe_mtime(item)
        if mtime is None:
            continue
        if best_mtime is None or mtime > best_mtime:
            best_label, best_mtime = label, mtime
    return best_label


def _safe_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _has_entries(path):
    try:
        with os.scandir(path) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def _grounding_home():
    """Agent home holding `.spec-grounding/`. Delegates to the one canonical
    resolver (utilities/dispatch_contract.resolve_agent_home) instead of a
    local reimplementation, matching `route._completion_home`. An explicit
    `AGENT_HOME`/`CLAUDE_HOME` env override is honored unconditionally; only
    the unset-env fallback chain goes through the validated resolver."""
    h = os.environ.get("AGENT_HOME") or os.environ.get("CLAUDE_HOME")
    if h:
        return h
    here = Path(__file__).resolve()
    for candidate in here.parents:
        utilities_dir = candidate / "utilities"
        if (utilities_dir / "dispatch_contract.py").is_file():
            if str(utilities_dir) not in sys.path:
                sys.path.insert(0, str(utilities_dir))
            from dispatch_contract import resolve_agent_home

            return str(resolve_agent_home())
    return os.path.expanduser("~/.claude")


_ENTRY_CAPABILITIES = frozenset((
    "autopilot-apply", "autopilot-code", "autopilot-design", "autopilot-draft",
    "autopilot-lab", "autopilot-refine", "autopilot-research",
    "autopilot-ship", "autopilot-spec",
))


def _capability_grounding_index(home):
    """Scan ``<home>/.capability-grounding`` into ``{sid: (mtime, {k:v})}``. Each file is a KV
    body (``capability=…``) named by session id. Missing dir / OSError -> empty index."""
    index = {}
    try:
        entries = list(os.scandir(os.path.join(home, ".capability-grounding")))
    except OSError:
        return {}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            mtime = entry.stat().st_mtime
            with open(entry.path, encoding="utf-8") as handle:
                body = handle.read()
        except OSError:
            continue
        fields = {}
        for line in body.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip():
                fields[key.strip()] = value.strip()
        if fields.get("capability") in _ENTRY_CAPABILITIES:
            index[entry.name] = (mtime, fields)
    return index


def _capability_grounding_for(entity, index, now=None):
    """The freshest capability-grounding record for this session's exact sid, or None. Freshness
    mirrors the spec marker (marker must postdate the session's estimated start), so a reused sid
    never inherits a prior process's tag."""
    sid = _field(entity, "session_id")
    if not sid or sid not in index:
        return None
    mtime, fields = index[sid]
    now = time.time() if now is None else now
    elapsed_min = _field(entity, "elapsed_min") or 0
    if mtime < now - elapsed_min * 60 - _SPEC_MARKER_FRESHNESS_SLACK_S:
        return None
    return fields


def _spec_marker_index(home):
    """Scan ``<home>/.spec-grounding`` once into ``{name: mtime}``. Missing dir or
    OSError degrades to an empty index rather than raising."""
    index = {}
    try:
        with os.scandir(os.path.join(home, ".spec-grounding")) as entries:
            for entry in entries:
                try:
                    index[entry.name] = entry.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        return {}
    return index


def _grounding_key(root):
    # spec-read-marker.sh: key=$(printf '%s' "$root" | sed 's#[/ ]#_#g')
    return root.replace("/", "_").replace(" ", "_")


def _spec_marker_match(entity, index, artifact_root=None):
    """Match this Session's exact ``session_id`` against the marker index.

    Candidate repo roots mirror ``_artifact_candidates``'s source set (the
    ``artifact_root`` parameter, ``entity.artifact_root``, ``entity.cwd``) so
    both inference paths agree on "this entity's repo root". In practice
    ``entity.artifact_root`` is always ``None`` here: this helper only runs for
    Session entities (resolve_work_projection gates it on the DispatchJob-only
    ``depth`` attribute), and ``Session`` carries no ``artifact_root`` field
    (only ``DispatchJob`` does) — so the parameter and ``entity.cwd`` are the
    real coverage, not the three-source list this echoes for structural parity.

    Marker names are forward-generated (``sid__key[__slug]``) and never
    reverse-parsed: ``key`` is a lossy escape and the ``__`` separators can
    nest when ``key`` itself starts with ``_`` (producing ``___``).
    """
    sid = _field(entity, "session_id")
    if not sid:
        return []
    roots = set()
    for value in (artifact_root, _field(entity, "artifact_root"), _field(entity, "cwd")):
        if not value:
            continue
        value = os.path.realpath(os.path.expanduser(str(value)))
        base = os.path.basename(value)
        roots.add(os.path.dirname(value) if base in (".agent_reports", ".claude_reports") else value)
    matches = []
    for root in roots:
        prefix = "%s__%s" % (sid, _grounding_key(root))
        if prefix in index:
            matches.append((root, None, index[prefix]))
        slug_prefix = prefix + "__"
        for name, mtime in index.items():
            if name.startswith(slug_prefix):
                matches.append((root, name[len(slug_prefix):], mtime))
    return matches


_SPEC_MARKER_FRESHNESS_SLACK_S = 120


def _fresh_spec_matches(matches, entity, now):
    """Reject a marker whose mtime predates this entity's estimated start minus
    slack (etime-minute truncation up to 59s + scan-delay absorption). This is
    freshness for sid *reuse* in general (e.g. ``claude --resume`` keeps the same
    sid across a new process), not a codex-specific rule."""
    elapsed_min = _field(entity, "elapsed_min") or 0
    cutoff = now - elapsed_min * 60 - _SPEC_MARKER_FRESHNESS_SLACK_S
    return [m for m in matches if m[2] >= cutoff]


def _select_spec_match(matches):
    """Pick the strictly freshest match; a tie among topics is not adopted."""
    if not matches:
        return None
    best = max(m[2] for m in matches)
    tied = [m for m in matches if m[2] == best]
    return tied[0] if len(tied) == 1 else None


def _strip_comment(value):
    idx = value.find("#")
    return (value if idx < 0 else value[:idx]).strip()


def _spec_pipeline_state_path(root, slug):
    reader = _artifact_reader()
    for reports_dir in (".agent_reports", ".claude_reports"):
        artifacts = os.path.join(root, reports_dir)
        # W7D: cycle spec trees and the latest shared/spec revision first, the
        # legacy top-level spec/ last as a read-only fallback.
        bases = ([str(b) for b, _ in reader.bucket_dirs(Path(artifacts), "spec")] if reader is not None
                 else [os.path.join(artifacts, "spec")])
        for base in bases:
            path = os.path.join(base, slug, "pipeline_state.yaml") if slug else os.path.join(base, "pipeline_state.yaml")
            if os.path.isfile(path):
                return path
    return None


def _spec_stage_parts(root, slug):
    """Zero-dep line parser for ``pipeline_state.yaml``. Every IO or shape failure
    degrades to topic-only (never raises) since this feeds a best-effort label:
    ``phases:`` map's first file-order ``in_progress`` key, else top-level
    ``status:`` value, else no phase. Unrecognized phase vocabulary (beyond
    done/pending/in_progress/deferred/n/a) auto-degrades since only the exact
    string ``in_progress`` is matched. Topic is the slug string for a slug
    marker, or ``project_name:`` (else ``None``) for a root marker."""
    topic, phase = slug, None
    path = _spec_pipeline_state_path(root, slug)
    if not path:
        return topic, phase
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return topic, phase
    project_name = None
    status = None
    in_phases = False
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if in_phases and indent == 0:
            in_phases = False
        if not slug and project_name is None and indent == 0 and stripped.startswith("project_name:"):
            project_name = _strip_comment(stripped[len("project_name:"):]).strip("'\"")
        if indent == 0 and stripped.startswith("phases:"):
            in_phases = True
            continue
        if in_phases and phase is None:
            key_part, sep, value_part = stripped.partition(":")
            if sep and _strip_comment(value_part) == "in_progress":
                phase = key_part.strip()
        if phase is None and status is None and indent == 0 and stripped.startswith("status:"):
            status = _strip_comment(stripped[len("status:"):]).strip("'\"")
    if phase is None:
        phase = status
    if not slug:
        topic = project_name
    return topic, phase


# Spec pipeline_state.yaml phase status vocabulary -> breadcrumb node state (2026-07-24).
_SPEC_PHASE_STATE = {
    "done": "done", "completed": "done", "complete": "done",
    "in_progress": "active", "in-progress": "active", "active": "active", "wip": "active",
    "pending": "pending", "todo": "pending", "planned": "pending", "not_started": "pending",
    "deferred": "skipped", "n/a": "skipped", "na": "skipped",
    "skip": "skipped", "skipped": "skipped",
}
_SPEC_PHASE_NUM = re.compile(r"^phase[\s_-]?(\d+)", re.I)


def _spec_phase_state(raw):
    return _SPEC_PHASE_STATE.get(_strip_comment(raw).strip().strip("'\"").lower(), "pending")


def _spec_phase_display(name, index):
    """Short breadcrumb label. Standard autopilot-spec phases (spec/scaffolding/design/dev)
    stay verbatim; a long custom name collapses to ``Phase<N>`` (user 2026-07-24: keep the
    breadcrumb bounded but readable — ``Phase1``, not ``p1``). The number comes from an
    explicit ``phase_<N>_...`` prefix when present, else the 1-based position."""
    m = _SPEC_PHASE_NUM.match(name)
    if m:
        return "Phase" + m.group(1)
    if len(name) <= 12:
        return name
    return "Phase%d" % (index + 1)


def _spec_mode(root, slug):
    """Compact spec mode(s) from ``pipeline_state.yaml`` ``mode:`` (flow ``[a, b]`` or a block
    list). Returns ``"a,b"`` or None. autopilot-spec modes are app/library/api/cli/research/
    update — shown in the breadcrumb lead so a spec row reads like a dispatch row's capability
    tag (user 2026-07-24 "spec은 mode가 따로 없나")."""
    path = _spec_pipeline_state_path(root, slug)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    modes = []
    in_block = False
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if in_block and indent == 0:
            in_block = False
        if indent == 0 and stripped.startswith("mode:"):
            rest = _strip_comment(stripped[len("mode:"):]).strip()
            if rest.startswith("[") and rest.endswith("]"):
                modes = [m.strip().strip("'\"") for m in rest[1:-1].split(",") if m.strip()]
                break
            if rest:
                modes = [rest.strip("'\"")]
                break
            in_block = True
            continue
        if in_block and stripped.startswith("-"):
            m = _strip_comment(stripped[1:]).strip().strip("'\"")
            if m:
                modes.append(m)
    return ",".join(modes) if modes else None


def _spec_phase_sequence(root, slug):
    """Ordered ``[(display, state), ...]`` of the spec's declared phases for a lit breadcrumb.
    Reads the same ``pipeline_state.yaml`` as ``_spec_stage_parts``; every IO/shape failure
    yields ``[]`` (the caller falls back to the flat topic label). Zero-dep line parser — the
    ``phases:`` block's indented ``key: status`` entries, in file order."""
    path = _spec_pipeline_state_path(root, slug)
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return []
    phases = []
    in_phases = False
    for raw in lines:
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if in_phases and indent == 0:
            in_phases = False
        if indent == 0 and stripped.startswith("phases:"):
            in_phases = True
            continue
        if in_phases and indent > 0 and not stripped.startswith("#"):
            key, sep, value = stripped.partition(":")
            if sep and key.strip():
                phases.append((key.strip(), _spec_phase_state(value)))
    return [(_spec_phase_display(name, i), state) for i, (name, state) in enumerate(phases)]


def _spec_marker_is_terminal(root, slug):
    """Terminal spec evidence is not live work for marker-only attribution."""
    phases = _spec_phase_sequence(root, slug)
    if phases:
        return all(state in {"done", "skipped"} for _, state in phases)
    _, phase = _spec_stage_parts(root, slug)
    return bool(phase) and _spec_phase_state(phase) in {"done", "skipped"}


def _artifact_latest_mtime(path):
    """Latest content mtime under an inferred plan dir (2-level glob covers
    ``plan/plan.md``, ``dev_logs/*``); falls back to the dir's own mtime."""
    mtimes = []
    for pattern in (os.path.join(path, "*"), os.path.join(path, "*", "*")):
        for item in glob.glob(pattern):
            try:
                mtimes.append(os.path.getmtime(item))
            except OSError:
                continue
    if mtimes:
        return max(mtimes)
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _spec_marker_projection(entity, spec_markers, artifact_root=None, now=None):
    """Resolve one entity's fail-closed spec-grounding marker attribution.

    Returns ``(projection, ambiguity, mtime)``. ``projection`` is populated only
    for a single, strictly-freshest, exact-sid marker; a tie among fresh matches
    yields ``(None, MULTIPLE_SPEC_MARKERS, None)`` so the caller may attach the
    diagnostic only once every other evidence source also comes up empty
    (mirrors the existing multi-candidate ambiguity handling below).
    """
    matches = _spec_marker_match(entity, spec_markers, artifact_root=artifact_root)
    if not matches:
        return None, None, None
    now = time.time() if now is None else now
    fresh = _fresh_spec_matches(matches, entity, now)
    if not fresh:
        return None, None, None
    selected = _select_spec_match(fresh)
    if selected is None:
        return None, MULTIPLE_SPEC_MARKERS, None
    root, slug, mtime = selected
    if _spec_marker_is_terminal(root, slug):
        return None, None, None
    topic, phase = _spec_stage_parts(root, slug)
    label = "spec"
    if topic:
        label += " %s" % topic
    if phase:
        label += " ·%s" % phase
    # Attach the ordered phase sequence so the row can render a lit breadcrumb
    # (Phase✓ › … › dev●) instead of only the flat topic label; the topic rides the
    # breadcrumb's own lead-in. `_route_view` is the same carrier route-exact uses.
    phases = _spec_phase_sequence(root, slug)
    route_view = ({"spec_phases": phases, "spec_topic": topic, "spec_mode": _spec_mode(root, slug)}
                  if phases else None)
    return (WorkProjection(source="artifact-inferred", stage_label=label,
                           _route_view=route_view),
            None, mtime)


def _explicit(entity):
    # An attempt identifies one launch, but it is not a route tuple.  Owners
    # carrying only attempt_id must still discover their children through the
    # explicit parent links below.
    return any(_field(entity, name) not in (None, "")
               for name in ("route_file", "route_id", "route_hash", "route_node"))


_OWNER_ROUTE_BINDING_MOD = None


def _owner_route_binding_module():
    """Lazily load utilities/owner_route_binding.py in-process.

    Read-only: the module only resolves an existing predecessor-keyed advance
    record under the jobs root, it never writes. Cached across ticks the same
    way the orphan-registry module is (`tools/fleet/collectors/dispatch.py`).
    """
    global _OWNER_ROUTE_BINDING_MOD
    if _OWNER_ROUTE_BINDING_MOD is None:
        import importlib.util
        path = os.path.join(
            os.path.dirname(__file__), "..", "..", "utilities", "owner_route_binding.py"
        )
        try:
            spec = importlib.util.spec_from_file_location("_fleet_owner_route_binding", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception:
            mod = False
        _OWNER_ROUTE_BINDING_MOD = mod
    return _OWNER_ROUTE_BINDING_MOD or None


def _owner_route_advance_binding(entity, owner_binding=None):
    """Resolve the owner's verified current route, if a durable advance exists.

    Returns ``(binding_dict_or_None, status)``. ``status`` is one of
    ``"absent-legacy"`` (no jobs path, no attempt id, or no durable record --
    F-88 heuristics remain authoritative), ``"current"`` (an advance chain was
    followed to a verified successor, possibly zero-length), or
    ``"conflict"`` (a present record failed verification -- tampered,
    downgraded, looping, or unrelated). A conflict must never be silently
    repaired by the legacy heuristics below it.
    """
    orb = _owner_route_binding_module()
    jobs_path = _field(entity, "_registry_path")
    attempt_id = _field(entity, "attempt_id")
    if orb is None or not jobs_path or not attempt_id:
        return None, "absent-legacy"
    try:
        anchor = (orb.OwnerRouteBinding(
            owner_binding["route_file"], owner_binding["route_id"], owner_binding["route_hash"],
        ) if owner_binding else None)
        current, resolved_status = orb.resolve_owner_route_lifecycle(
            jobs_path, owner_attempt_id=attempt_id, sealed_binding=anchor,
        )
    except orb.OwnerRouteBindingError as exc:
        if str(exc) in {"owner-route-jobs-unreadable", "owner-route-owner-row-not-unique"}:
            return None, "absent-legacy"
        if str(exc) == "owner-route-advance-competing-successor":
            return None, "multiple"
        return None, "conflict"
    if resolved_status == "owner-route-advance-loop":
        return None, "conflict"
    if resolved_status in (
        "owner-route-binding-absent", "owner-route-launch-binding",
        "owner-route-advance-absent", "owner-route-advance-anchor-unresolvable",
    ):
        return None, "absent-legacy"
    if current is None:
        return None, "absent-legacy"
    return {
        "route_file": current.route_file, "route_id": current.route_id,
        "route_hash": current.route_hash,
    }, "current"


def _owner_route_binding(entity):
    """Return a complete wrapper-validated owner binding or its fail-closed error."""
    values = {
        "route_file": _field(entity, "owner_route_file"),
        "route_id": _field(entity, "owner_route_id"),
        "route_hash": _field(entity, "owner_route_hash"),
    }
    present = [value not in (None, "") for value in values.values()]
    if not any(present):
        return None, None
    try:
        depth = int(_field(entity, "depth") or 1)
    except (TypeError, ValueError):
        depth = None
    if (not all(present)
            or depth != 1
            or _field(entity, "worker_type") != "owner"
            or _field(entity, "route_node") not in (None, "")):
        return None, "owner-route-binding-invalid"
    return values, None


def _owner_children(entity, jobs):
    """Return only children named by the stable owner-link contracts."""
    sid = _field(entity, "session_id")
    slug = _field(entity, "slug")
    children = []
    for child in jobs:
        if sid and _field(child, "parent_sid") == sid:
            children.append(child)
        elif slug and _field(child, "parent_slug") == slug:
            children.append(child)
    return children


def _candidate_projection(entity, candidate, jobs, route_records, node_evidence, now, degradations=None):
    """Adopt one registered leaf candidate without reopening it at render time."""
    rid = _field(candidate, "route_id")
    record, failure = _route_record(candidate, route_records=route_records)
    route_node = _field(candidate, "route_node")
    if record is None or not route_node:
        return WorkProjection(
            source="registry-exact", route_id=rid,
            route_hash=_field(candidate, "route_hash"), route_node=route_node,
            attempt_id=_field(entity, "attempt_id"), node_state="unknown",
            ambiguity=failure or ROUTE_RECORD_MISMATCH,
        )
    same_jobs = [j for j in jobs if _field(j, "route_id") == rid]
    return _projection_from_record(
        entity, record, rid, same_jobs,
        node_evidence=(node_evidence or {}).get(rid, {}),
        now=now, route_node=route_node, degradations=degradations,
    )


def resolve_work_projection(entity, jobs=(), route_records=None, node_evidence=None,
                            artifact_root=None, now=None, spec_markers=None,
                            cap_grounding=None, _seen=None, degradations=None):
    """Resolve one entity using the approved evidence precedence."""
    seen = set() if _seen is None else _seen
    ident = (id(entity), _field(entity, "slug"), _field(entity, "session_id"))
    if ident in seen:
        return WorkProjection(ambiguity=OWNER_ROUTE_CONFLICT)
    seen.add(ident)
    owner_binding, owner_binding_error = _owner_route_binding(entity)
    if owner_binding_error:
        return WorkProjection(
            source="registry-exact", route_id=_field(entity, "owner_route_id"),
            route_hash=_field(entity, "owner_route_hash"),
            attempt_id=_field(entity, "attempt_id"), node_state="unknown",
            ambiguity=owner_binding_error,
        )
    advance_binding, advance_status = _owner_route_advance_binding(entity, owner_binding)
    if advance_status == "multiple":
        return WorkProjection(
            source="none", attempt_id=_field(entity, "attempt_id"),
            node_state="unknown", ambiguity=MULTIPLE_OWNER_ROUTES,
        )
    if advance_status == "conflict":
        return WorkProjection(
            source="registry-exact",
            route_id=(owner_binding or {}).get("route_id"),
            route_hash=(owner_binding or {}).get("route_hash"),
            attempt_id=_field(entity, "attempt_id"), node_state="unknown",
            ambiguity="owner-route-advance-conflict",
        )
    if advance_status == "current" and advance_binding is not None:
        # A durable, verified post-launch attachment or successor advance is
        # authoritative even before the first child row exists.
        record, failure = _route_record_values(
            advance_binding["route_id"], advance_binding["route_file"],
            advance_binding["route_hash"], route_records=route_records,
        )
        if record is None:
            return WorkProjection(
                source="registry-exact", route_id=advance_binding["route_id"],
                route_hash=advance_binding["route_hash"],
                attempt_id=_field(entity, "attempt_id"), node_state="unknown",
                ambiguity=failure or ROUTE_RECORD_MISMATCH,
            )
        same_jobs = [j for j in jobs if _field(j, "route_id") == advance_binding["route_id"]]
        return _projection_from_record(
            entity, record, advance_binding["route_id"], same_jobs,
            node_evidence=(node_evidence or {}).get(advance_binding["route_id"], {}),
            now=now, owner=True, degradations=degradations,
        )
    if owner_binding:
        record, failure = _route_record_values(
            owner_binding["route_id"], owner_binding["route_file"],
            owner_binding["route_hash"], route_records=route_records,
        )
        if record is None:
            return WorkProjection(
                source="registry-exact", route_id=owner_binding["route_id"],
                route_hash=owner_binding["route_hash"],
                attempt_id=_field(entity, "attempt_id"), node_state="unknown",
                ambiguity=failure or ROUTE_RECORD_MISMATCH,
            )
        children = _owner_children(entity, jobs)
        route_children = [child for child in children if _field(child, "route_id")]
        child_conflict = any(
            (_field(child, "route_id") and _field(child, "route_id") != owner_binding["route_id"])
            or (_field(child, "route_hash")
                and _field(child, "route_hash") != owner_binding["route_hash"])
            for child in children
        )
        if child_conflict:
            # F-88: an owner may legally re-compile its route mid-attempt (re-plan
            # loops), so the launch-sealed owner binding can point at a superseded
            # route while every live child carries the successor (observed live
            # 2026-08-28: owner sealed to a continuation route, children on the
            # third re-compiled route, card blanked for the rest of the cycle).
            # When ALL route-carrying children named by the stable owner-link
            # contracts agree on ONE non-owner route and that record verifies,
            # the owner has demonstrably moved on — project the successor instead
            # of refusing. Mixed generations (a live child still on the sealed
            # route) or an unverifiable successor record stay fail-closed.
            child_routes = {_field(child, "route_id")
                            for child in children if _field(child, "route_id")}
            successor = None
            if (len(child_routes) == 1
                    and owner_binding["route_id"] not in child_routes):
                carrier = next(child for child in children
                               if _field(child, "route_id"))
                successor, _s_failure = _route_record_values(
                    _field(carrier, "route_id"), _field(carrier, "route_file"),
                    _field(carrier, "route_hash"), route_records=route_records,
                )
            if successor is None:
                return WorkProjection(source="none", node_state="unknown",
                                      ambiguity=OWNER_ROUTE_CONFLICT)
            successor_rid = successor.get("route_id")
            same_jobs = [j for j in jobs
                         if _field(j, "route_id") == successor_rid]
            return _projection_from_record(
                entity, successor, successor_rid, same_jobs,
                node_evidence=(node_evidence or {}).get(successor_rid, {}),
                now=now, owner=True, degradations=degradations,
            )
        if not route_children:
            # F-88 gap correction: a verified successor remains the route of
            # record while no live child row exists between attempts.  This is
            # evidence recovery, not UI hysteresis; a fresh --once snapshot can
            # reach the same answer from the canonical registry alone.
            latest = _latest_exact_owner_evidence_route(
                entity, node_evidence, route_records or {},
            )
            if latest is not None:
                successor_rid, successor, successor_failure = latest
                if successor_rid is None or successor_failure:
                    return WorkProjection(source="none", node_state="unknown",
                                          ambiguity=OWNER_ROUTE_CONFLICT)
                if successor_rid != owner_binding["route_id"]:
                    same_jobs = [j for j in jobs
                                 if _field(j, "route_id") == successor_rid]
                    return _projection_from_record(
                        entity, successor, successor_rid, same_jobs,
                        node_evidence=(node_evidence or {}).get(successor_rid, {}),
                        now=now, owner=True, degradations=degradations,
                    )
        same_jobs = [j for j in jobs
                     if _field(j, "route_id") == owner_binding["route_id"]]
        return _projection_from_record(
            entity, record, owner_binding["route_id"], same_jobs,
            node_evidence=(node_evidence or {}).get(owner_binding["route_id"], {}),
            now=now, owner=True, degradations=degradations,
        )

    # A registered owner is itself a process/cwd match for all of its children.
    # Collapse its exact successor lineage before the generic leaf/cwd adoption
    # heuristics, which are intended for otherwise-unattributed sessions and
    # would reduce a legitimate R0->R1->R2 owner to a cardinality error.
    lineage_projection, lineage_conflict = _owner_lineage_projection(
        entity, jobs, route_records, node_evidence, now, degradations,
    )
    if lineage_conflict:
        return WorkProjection(source="none", node_state="unknown",
                              ambiguity=MULTIPLE_OWNER_ROUTES)
    if lineage_projection is not None:
        return lineage_projection

    pid, proc_start = _field(entity, "pid"), _field(entity, "proc_start")
    identity_evidence_present = pid is not None and proc_start is not None
    if identity_evidence_present:
        leaf_candidates = [j for j in jobs
                           if _field(j, "pid") == pid
                           and _field(j, "proc_start") == proc_start
                           and _field(j, "route_id")]
        if len(leaf_candidates) > 1:
            return WorkProjection(source="none", node_state="unknown",
                                  ambiguity=MULTIPLE_LEAF_CANDIDATES)
        if len(leaf_candidates) == 1 and not _explicit(entity):
            return _candidate_projection(entity, leaf_candidates[0], jobs, route_records,
                                         node_evidence, now, degradations)
    elif not _explicit(entity):
        cwd = _realpath(_field(entity, "cwd"))
        harness = _field(entity, "harness")
        cwd_candidates = [j for j in jobs
                          if _field(j, "route_id") and harness
                          and _field(j, "harness") == harness
                          and cwd and _realpath(_field(j, "cwd")) == cwd]
        if len(cwd_candidates) > 1:
            return WorkProjection(source="none", node_state="unknown",
                                  ambiguity=MULTIPLE_CHILD_CWD_CANDIDATES)
        if len(cwd_candidates) == 1:
            return _candidate_projection(entity, cwd_candidates[0], jobs, route_records,
                                         node_evidence, now, degradations)

    record, failure = _route_record(entity, route_records=route_records)
    explicit = _explicit(entity)
    if record is not None:
        expected_node = _field(entity, "route_node")
        nodes = record.get("nodes") or []
        node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
        if not expected_node or expected_node not in node_ids:
            failure = ROUTE_RECORD_MISMATCH
            record = None
        else:
            same_jobs = [j for j in jobs if _field(j, "route_id") == _field(entity, "route_id")]
            own = _projection_from_record(
                entity, record, _field(entity, "route_id"), same_jobs,
                node_evidence=(node_evidence or {}).get(_field(entity, "route_id"), {}),
                now=now, route_node=expected_node,
                degradations=degradations,
                owner=bool(_owner_children(entity, jobs)))
            # A direct owner route is valid only when every linked child agrees
            # with it.  Never silently privilege the owner's tuple over a child.
            child_projections = [resolve_work_projection(
                child, jobs=jobs, route_records=route_records,
                node_evidence=node_evidence, artifact_root=artifact_root,
                now=now, spec_markers=spec_markers, _seen=seen, degradations=degradations)
                for child in _owner_children(entity, jobs)]
            child_keys = {(p.route_id, p.route_hash) for p in child_projections
                          if p.source == "route-exact" and p.route_id}
            own_key = (own.route_id, own.route_hash)
            if any(key != own_key for key in child_keys):
                return WorkProjection(source="none", node_state="unknown",
                                      ambiguity=OWNER_ROUTE_CONFLICT)
            return own
    if explicit:
        return WorkProjection(
            source="registry-exact", route_id=_field(entity, "route_id"),
            route_hash=_field(entity, "route_hash"), route_node=_field(entity, "route_node"),
            attempt_id=_field(entity, "attempt_id"), assigned_contract=_field(entity, "assigned_contract"),
            unit=_field(entity, "unit"), stage_label=_field(entity, "assigned_contract") or _field(entity, "route_node"),
            node_state="unknown",
            ambiguity=failure or ROUTE_RECORD_MISMATCH,
        )

    # Owner route attribution is explicit parent-link traversal only.
    children = _owner_children(entity, jobs)
    child_projections = [resolve_work_projection(child, jobs=jobs, route_records=route_records,
                                                  node_evidence=node_evidence,
                                                  artifact_root=artifact_root, now=now,
                                                  spec_markers=spec_markers, _seen=seen,
                                                  degradations=degradations)
                         for child in children]
    for rid, record, evidence in _evidence_owner_candidates(entity, node_evidence, route_records or {}):
        historical = _projection_from_record(
            entity, record, rid, [j for j in jobs if _field(j, "route_id") == rid],
            node_evidence=evidence, now=now, owner=True, degradations=degradations)
        # Terminal rows remain available to route.collect_views/process view.  They
        # must not reattach a finished child pipeline to a long-lived main session
        # as if it were the session's current unit of work.
        if not _terminal_route_projection(historical):
            child_projections.append(historical)
    exact = [p for p in child_projections if p.source == "route-exact"]
    route_keys = {(p.route_id, p.route_hash) for p in exact}
    if len(route_keys) > 1:
        return WorkProjection(source="none", ambiguity=MULTIPLE_OWNER_ROUTES)
    if len(route_keys) == 1 and exact:
        p = exact[0]
        active = p.active_nodes
        owner_node, owner_state = _owner_active_selection(active)
        return WorkProjection(source="route-exact", route_id=p.route_id, route_hash=p.route_hash,
                              route_node=owner_node, node_state=owner_state,
                              active_nodes=active, progress=p.progress,
                              stage_label=_active_stage_label(active),
                              _route_view=p._route_view)

    # Artifact inference is the final fallback and is legal only when no route
    # tuple exists anywhere on this entity.  Owner candidates above therefore
    # always win, even when a plausible plan directory is present.
    # Spec-grounding is a read marker, not proof that spec is the session's
    # current entry capability.  Adopt it only when the fresh exact-sid
    # capability marker says autopilot-spec (F-43); absence is safer than
    # pinning a long-lived main session to every PRD it happened to read.
    marker_projection = marker_ambiguity = marker_mtime = None
    if (spec_markers is not None and not hasattr(entity, "depth")
            and _field(cap_grounding, "capability") == "autopilot-spec"):
        marker_projection, marker_ambiguity, marker_mtime = _spec_marker_projection(
            entity, spec_markers, artifact_root=artifact_root, now=now)

    candidates = _artifact_candidates(entity, artifact_root=artifact_root)
    if marker_projection is not None:
        # A single named plans dir competes with the marker on freshness; two or
        # more candidates are already an ambiguous name-inference and lose to the
        # exact-sid marker outright (no plans-glob dir count wins by default).
        if len(candidates) == 1:
            artifact_mtime = _artifact_latest_mtime(candidates[0])
            if artifact_mtime > marker_mtime:
                return WorkProjection(source="artifact-inferred", stage_label=_artifact_stage(candidates[0]))
            if artifact_mtime == marker_mtime:
                return WorkProjection(source="none", ambiguity=MARKER_ARTIFACT_TIE)
        return marker_projection
    if len(candidates) == 1:
        return WorkProjection(source="artifact-inferred", stage_label=_artifact_stage(candidates[0]))
    if len(candidates) > 1:
        return WorkProjection(source="none", ambiguity=MULTIPLE_ARTIFACT_PLAN_DIRS)
    if marker_ambiguity is not None:
        return WorkProjection(source="none", ambiguity=marker_ambiguity)
    # Without a route tuple or exact artifact evidence there is no observed
    # stage to project.  The renderer may show its honest pre-boot track, but
    # must not echo a manually supplied legacy stage token as current truth.
    return WorkProjection(source="none")


def resolve_projection(*args, **kwargs):
    """Compatibility alias for callers using the shorter v16 name."""
    return resolve_work_projection(*args, **kwargs)


def attach_projections(sessions: Iterable[Session], jobs: Iterable[DispatchJob],
                      route_records=None, node_evidence=None, artifact_root=None, now=None,
                      spec_markers=None, spec_marker_home=None,
                      capability_groundings=None, degradations=None):
    """Attach work to every row and exact-owned context to live cards."""
    sessions, jobs = list(sessions), list(jobs)
    route_records = _load_evidence_records(node_evidence, route_records)
    home = spec_marker_home or _grounding_home()
    if spec_markers is None:
        spec_markers = _spec_marker_index(home)
    cap_index = (_capability_grounding_index(home) if capability_groundings is None
                 else capability_groundings)
    all_entities = sessions + jobs
    for session in sessions:
        public, private = normalize_context(_evidence(session), now=now, live=_is_live(session))
        session.context = public
        session._context_evidence = private
    for job in jobs:
        # F-50f plugin rows and F-65 registered attempts own their exact telemetry.
        # Arbitrary legacy/inferred job values remain fail-closed.
        owned = (job.source == "plugin-queue"
                 or getattr(job, "_dispatch_context_owned", False))
        if owned and job._context_evidence is not None:
            job.context, job._context_evidence = normalize_context(
                _evidence(job), now=now, live=_is_live(job))
            continue
        if owned:
            # Rendering still shows the honest unknown gauge for a live dispatch;
            # keep the JSON context object absent until an actual percentage exists.
            job.context = None
        else:
            job.context = None
            job._context_evidence = None
    # Resolve the current inline entry before artifact fallback.  A spec-read
    # marker is eligible only when this exact session is actively in autopilot-spec.
    for session in sessions:
        session.cap_grounding = _capability_grounding_for(session, cap_index, now=now)
    for entity in all_entities:
        entity.work_projection = resolve_work_projection(
            entity, jobs=jobs, route_records=route_records,
            node_evidence=node_evidence, artifact_root=artifact_root, now=now,
            spec_markers=spec_markers,
            cap_grounding=(entity.cap_grounding if isinstance(entity, Session) else None),
            degradations=degradations)
        entity.stage = entity.work_projection.stage_label if isinstance(entity, DispatchJob) else getattr(entity, "stage", None)
    return sessions, jobs


attach_work_projections = attach_projections


def route_summary_from_projections(entities):
    """Serialize route backing data already attached to rows; never reopens a route file."""
    out, seen = [], set()
    for entity in entities:
        projection = _field(entity, "work_projection")
        if not isinstance(projection, WorkProjection) or not projection.route_id:
            continue
        if projection.route_id in seen:
            continue
        seen.add(projection.route_id)
        backing = projection._route_view or {}
        record = backing.get("record") or {}
        legacy_view = backing.get("view") or {}
        nodes = []
        for node in legacy_view.get("nodes") or backing.get("nodes") or ():
            nodes.append(node.to_dict() if isinstance(node, ActiveNodeProjection) else dict(node))
        for node in nodes:
            # ``route._record_view`` keeps runtime job references for rendering;
            # public JSON is the additive route contract and must remain plain data.
            node.pop("job", None)
            node.pop("pid", None)
        ambiguity = projection.ambiguity
        if ambiguity is not None and not isinstance(ambiguity, list):
            ambiguity = [ambiguity]
        out.append({
            "route_id": projection.route_id,
            "route_hash": projection.route_hash or record.get("route_hash"),
            # Preserve the legacy route.summary values from the attached
            # backing view; v16 fields remain additive.
            "source": legacy_view.get("source", "record"),
            "capability": legacy_view.get("capability", record.get("capability")),
            "capability_mode": legacy_view.get("capability_mode", record.get("capability_mode")),
            "execution_topology": legacy_view.get("execution_topology", record.get("execution_topology")),
            "unit_catalog_digest": legacy_view.get("unit_catalog_digest", record.get("unit_catalog_digest")),
            "composed": bool(legacy_view.get("composed", record.get("composed"))),
            "effective_intensity": legacy_view.get("effective_intensity", record.get("effective_intensity")),
            "progress": legacy_view.get("progress", projection.progress.to_dict() if projection.progress else None),
            "ambiguity": ambiguity,
            "nodes": nodes,
        })
    return out
