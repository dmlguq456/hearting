"""Artifact producer: route-to-cycle binding, admission and lineage.

Which cycle a route writes to, whether a route may bind it, the producer
binding check, the route-lineage handover, and the finalize route evidence.

Split out of `artifact_producer.py`, which re-exports every name defined here.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Tuple

import artifact_admission
import artifact_lifecycle
import artifact_locator
import route_identity
import route_lineage
import directory_record_index
import dispatch_contract
import dispatch_terminal_commit

from artifact_producer_store import (
    _canonical, _digest, _ensure_dir, _json_bytes, _read_json, _rfc3339, _write_atomic,
    _write_cycle_record, campaign_or_tombstone, CLOSED_BINDABLE_STATES, INTENSITIES,
    journal_path, list_cycle_records, ProducerError, read_cycle_record,
)


# ---------------------------------------------------------------------------
# D-120: route lineage binding -- "route W may write cycle C" is judged fresh
# from the sealed route file and the cycle record's begin values every time,
# never from the producer's own audit copy (`route_bindings[]`, which
# `bind_cycle_route` writes and only readers consume). SD-155's
# `verified_route_lineage` is the one hash-verified walk this admission and
# `route_cycle_for` both stand on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Admission:
    allow: bool
    reason: Optional[str]
    detail: str
    path: List[Dict[str, Any]]
    next_action: Optional[str]
    # Never-run siblings this judgment passed over (D-120's verified-handoff
    # exception); recorded on the seal so the difference is not silently lost.
    skipped: Tuple[str, ...] = ()


_D120_NEXT_ACTION = {
    "route-lineage-unverified": ("restore the sealed route file, or start a new route; a continuation that "
                                  "changed capability needs a new child cycle (--parent-cycle)"),
    "route-hash-drift": "restore the sealed route file",
    "cycle-route-binding-mismatch": "start a new cycle (--parent-cycle to keep it linked)",
    "cycle-route-binding-mismatch:material-input": "start a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:lineage-fork": "continue this branch in a new child cycle (--parent-cycle)",
    "cycle-route-binding-mismatch:superseded-route": "close and seal from the end route instead",
}


def _d120_next_action(reason: str) -> str:
    return _D120_NEXT_ACTION.get(reason, _D120_NEXT_ACTION.get(reason.split(":", 1)[0], "retry"))


def _routes_dir(root: Path) -> Path:
    return route_lineage.canonical_route_path(root, "x").parent


def _qualified_continuation(entry_stem: str, candidate: Any, route_id: Optional[str] = None,
                            route_hash_value: Optional[str] = None) -> bool:
    """Whether a route file is a sealed continuation edge (of ``route_id`` when given).

    The one qualification rule shared by `_lineage_children` and
    `closed_lineage_handover`: not its own source, contract version 1, source id
    and hash match, and the route hash recomputes -- a tampered sibling proves
    nothing and is not part of any lineage.
    """
    if not isinstance(candidate, dict) or candidate.get("continuation_contract_version") != 1:
        return False
    source_id = candidate.get("source_route_id")
    if entry_stem == source_id or (route_id is not None and source_id != route_id):
        return False
    if route_hash_value is not None and candidate.get("source_route_hash") != route_hash_value:
        return False
    return bool(directory_record_index.route_keys(entry_stem + ".json", candidate))


class _RouteEdges:
    """What one read of a routes directory found: its sealed continuation edges by source.

    `files` keeps, per file name, the (size, mtime_ns) it was parsed at and the edge it held, so a
    rescan parses only what is new or changed.  `children` maps (source route id, source route
    hash) to the edges naming it, in file-name order.  `trusted` says the whole answer may be
    served again without listing the directory: its signature is the one before and after the read,
    old enough that a write in the same clock tick cannot hide behind it, and every file parsed."""
    __slots__ = ("signature", "files", "children", "trusted")

    def __init__(self, signature, files, children, trusted) -> None:
        self.signature, self.files, self.children, self.trusted = signature, files, children, trusted


_ROUTE_EDGES: Dict[str, _RouteEdges] = {}
_ROUTE_EDGES_SETTLE_NS = 2_000_000_000


def _routes_dir_signature(directory: Path) -> Optional[Tuple[int, int, int]]:
    try:
        info = os.stat(str(directory))
    except OSError:
        return None
    return (info.st_ino, info.st_mtime_ns, info.st_size)


def _edge_stamps_hold(directory: Path, held: _RouteEdges) -> bool:
    """The files that held an edge still have the size and mtime they were parsed at.

    A file rewritten in place leaves the directory's signature as it was; route files are written
    once (`write_once`), so this only looks at the few that held an edge."""
    for name, (size, mtime_ns, edge) in held.files.items():
        if edge is None:
            continue
        try:
            info = os.stat(str(directory / name))
        except OSError:
            return False
        if (info.st_size, info.st_mtime_ns) != (size, mtime_ns):
            return False
    return True


def _route_edges(root: Path) -> Mapping[Tuple[str, Any], List[Dict[str, Any]]]:
    """The routes directory's continuation edges, read at most once per process while it is unchanged.

    D-120 allows a child index bound to the directory listing ("구현은 디렉터리 목록 digest에
    결속된 자식 색인을 캐시로 둘 수 있다"); correctness never depends on one.  A directory whose
    signature moved, or one too fresh to trust, is read again, and a file whose size and mtime
    are what the last read saw is not parsed again.  A missing or unreadable cache is a fresh scan."""
    directory = _routes_dir(root)
    key = str(directory)
    before = _routes_dir_signature(directory)
    if before is None:
        _ROUTE_EDGES.pop(key, None)
        return {}
    held = _ROUTE_EDGES.get(key)
    if held is not None and held.trusted and held.signature == before and _edge_stamps_hold(directory, held):
        return held.children
    known = held.files if held is not None else {}
    files: Dict[str, Tuple[int, int, Optional[Dict[str, Any]]]] = {}
    children: Dict[Tuple[str, Any], List[Dict[str, Any]]] = {}
    complete = True
    try:
        names = sorted(entry.name for entry in os.scandir(str(directory)) if entry.name.endswith(".json"))
    except OSError:
        _ROUTE_EDGES.pop(key, None)
        return {}
    for name in names:
        entry = directory / name
        try:
            info = os.stat(str(entry))
        except OSError:
            complete = False
            continue
        stamp = (info.st_size, info.st_mtime_ns)
        remembered = known.get(name)
        if remembered is not None and remembered[:2] == stamp:
            edge = remembered[2]
        else:
            candidate = _read_json(entry)
            if candidate is None:
                complete = False
            edge = candidate if _qualified_continuation(entry.stem, candidate) else None
        files[name] = (*stamp, edge)
        if edge is not None:
            children.setdefault((edge.get("source_route_id"), edge.get("source_route_hash")), []).append(edge)
    after = _routes_dir_signature(directory)
    trusted = (complete and after == before
               and time.time_ns() - before[1] > _ROUTE_EDGES_SETTLE_NS)
    if len(_ROUTE_EDGES) >= 8:
        _ROUTE_EDGES.clear()   # a process rarely reads more than one root
    _ROUTE_EDGES[key] = _RouteEdges(before, files, children, trusted)
    return children


def _lineage_children(root: Path, route_id: str, route_hash_value: str) -> List[Dict[str, Any]]:
    """Every continuation whose sealed edge names ``route_id``/``route_hash_value`` as its source.

    A listing-bound disposable index survives CLI processes. Only the selected
    sealed child files are parsed on a hit; missing/stale/corrupt indexes scan
    the original records and repair themselves."""
    return [copy.deepcopy(child)
            for child in _indexed_route_children(root, route_id, route_hash_value)
            if not _closed_unexecuted_continuation(root, child)]


def _indexed_route_children(root: Path, route_id: str, route_hash_value: str):
    def classify(name, candidate):
        return ([directory_record_index.route_key(candidate["source_route_id"], candidate.get("source_route_hash"))]
                if _qualified_continuation(Path(name).stem, candidate) else [])
    try:
        return directory_record_index.select(
            _routes_dir(root), [directory_record_index.route_key(route_id, route_hash_value)],
            kind="route-children", classify=classify, read_json=_read_json,
            ignored=directory_record_index.ROUTE_SIDECARS)
    except OSError:
        return list(_route_edges(root).get((route_id, route_hash_value), []))


def _closed_unexecuted_continuation(root: Path, route: Mapping[str, Any]) -> bool:
    """A closed, unused candidate cannot take an executed predecessor's cycle."""
    outcome = _read_json(route_lineage.canonical_route_path(root, route["route_id"]).with_suffix(".outcome.json"))
    if (not isinstance(outcome, dict)
            or outcome.get("route_id") != route["route_id"]
            or outcome.get("route_hash") != route["route_hash"]
            or outcome.get("terminal_gate_proven") is not False):
        return False
    return _unexecuted_continuation(root, route)


def _unexecuted_continuation(root: Path, route: Mapping[str, Any]) -> bool:
    """No attempt of this continuation ever started, and nothing continues it.

    Audit bindings and unconsumed registrations are history, not execution.
    Read the exact route registry and markers afresh; missing or unreadable
    evidence stays unknown. Started or unresolved launches stay attached.
    """
    if _indexed_route_children(root, route["route_id"], route["route_hash"]):
        return False
    try:
        module = artifact_lifecycle._load_capability_route()
        sealed_jobs = ((route.get("launch_compatibility_tuple") or {}).get("jobs_path") or {}).get("path")
        if not isinstance(sealed_jobs, str) or not Path(sealed_jobs).is_absolute():
            return False
        resolution = module.resolve_dangling_registry(Path(sealed_jobs))
        if resolution.status not in ("exact", "aliased"):
            return False
        jobs = resolution.jobs_path
        # The registry is shared and busy; a stat change during the read is
        # ordinary contention, not "unknown" evidence. Re-read a bounded
        # number of times (three) and only then stay unknown.
        for _attempt in range(4):
            before = jobs.stat()
            lines = jobs.read_text(encoding="utf-8").splitlines()
            after = jobs.stat()
            if all(getattr(before, key) == getattr(after, key)
                   for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
                break
        else:
            return False
        for line in lines:
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 6:
                return False
            meta = dispatch_contract.parse_registry_metadata(fields[5])
            if route["route_id"] in {meta.get("route_id"), meta.get("route"), meta.get("owner_route_id")}:
                from route_authority import continuation_attempt_state
                if continuation_attempt_state(meta) != "unstarted":
                    return False
        markers = module.completion_dir(route["route_id"], jobs=jobs)
        if markers.exists() and any(markers.iterdir()):
            return False
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _closed_for_successor(source: Mapping[str, Any], successor: Mapping[str, Any]) -> bool:
    """``source`` was closed by its parent for a handoff whose prepared successor is ``successor``."""
    sealed_jobs = ((source.get("launch_compatibility_tuple") or {}).get("jobs_path") or {}).get("path")
    if not isinstance(sealed_jobs, str) or not Path(sealed_jobs).is_absolute():
        return False
    try:
        import route_parent_close
        proof = route_parent_close.verified_owner_pin_handoff_close(dict(source), jobs=Path(sealed_jobs))
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        return False
    prepared = (proof or {}).get("successor")
    return (isinstance(prepared, Mapping) and prepared.get("route_id") == successor.get("route_id")
            and prepared.get("route_hash") == successor.get("route_hash"))


class LineageHandover(NamedTuple):
    """`closed`: the cycle's whole qualifying lineage tree has an outcome.
    `handed_over`: routes of that tree that begin another cycle (empty unless closed)."""
    closed: bool
    handed_over: frozenset


def closed_lineage_handover(root: Path, record: Mapping[str, Any]) -> LineageHandover:
    """Whether ``record``'s lineage is closed, and which routes it handed to other cycles.

    The tree is every material-input-qualifying continuation below the cycle's
    begin route, read through `_route_edges` (one scan of the routes directory). It is closed only
    when every route in it has an outcome; a live tree keeps D-120's "which cycle
    continues" judgment untouched (`(False, frozenset())`). In a closed tree the
    routes that begin a *different* cycle belong to that cycle, so this cycle
    seals on its own stretch, up to just before them.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    tree = {begin_route["route_id"]}
    queue = [begin_route]
    while queue:
        node = queue.pop()
        if not route_is_closed(root, node):
            return LineageHandover(False, frozenset())
        for child in _lineage_children(root, node["route_id"], node["route_hash"]):
            if (child.get("capability") != record.get("capability")
                    or child.get("effective_intensity") != record.get("intensity")):
                continue
            if child["route_id"] in tree:
                return LineageHandover(False, frozenset())  # a cycle in the lineage is never closed
            tree.add(child["route_id"])
            queue.append(child)
    others = {rec.get("route_id") for rec in list_cycle_records(root, route_ids=tree)
              if rec.get("cycle_id") != record.get("cycle_id")}
    return LineageHandover(True, frozenset(tree & others) - {begin_route["route_id"]})


def _handed_over_routes(root: Path, record: Mapping[str, Any]) -> frozenset:
    try:
        return closed_lineage_handover(root, record).handed_over
    except (ProducerError, KeyError, OSError):
        return frozenset()


def route_cycle_for(root: Path, route: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The one cycle begun by a route in ``route``'s verified lineage, if any.

    Shared by every "find the cycle for this route" call site (env resolution,
    begin resume, checkpoint, `require_cycle_output`). The cycle's state does not
    decide whether it can be found (§45 D-123): a continuation binds a closed
    cycle again and writes into it. An open cycle is the one the lineage is
    working in now; two or more open cycles whose begin route sits in the same
    verified lineage is the existing `route-cycle-binding-ambiguous` refusal.
    With none open, the closed cycle begun nearest to ``route`` in the lineage
    is the one, provided D-120's judgment still admits the route to it (a
    continuation that changed capability or intensity does not bind it and
    begins a child cycle instead).
    """
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        raise ProducerError(exc.code, exc.detail) from exc
    nearness = {r.get("route_id"): index for index, r in enumerate(lineage)}  # [route, parent, ..., begin route]
    matches = list_cycle_records(root, route_ids=nearness)
    opened = [rec for rec in matches if rec.get("state") == "open" and not rec.get("deleted_at")]
    if len(opened) > 1:
        raise ProducerError("route-cycle-binding-ambiguous", route.get("route_id", ""))
    if opened:
        return opened[0]
    # A cycle deleted while its route ran is still the route's cycle until the route ends (D-126):
    # `finalize` closes it as a cycle with no output.  A live one, begun since, takes precedence.
    deleted_open = [rec for rec in matches if rec.get("state") == "open" and rec.get("deleted_at")]
    if len(deleted_open) == 1:
        return deleted_open[0]
    closed = sorted((rec for rec in matches if rec.get("state") in CLOSED_BINDABLE_STATES),
                    key=lambda rec: nearness[rec.get("route_id")])
    if closed and cycle_route_admission(root, closed[0], route).allow:
        return closed[0]
    return None


def cycle_route_admission(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                          *, finalize: bool = False, validation_only: bool = False,
                          sealing: bool = False) -> Admission:
    """D-120's one lineage judgment for a cycle route.

    Steps 0-4 as spelled out in artifact-path-contract §42: ``route``'s
    hash-verified lineage, the begin route's membership and hash in that
    lineage, material-input (capability/intensity) parity along the path, and
    sibling-fork detection at every path node but ``route`` itself. The cycle's
    state is not a step (§45 D-123): a closed cycle admits its lineage's routes
    again. ``finalize=True`` adds D-120's finalize-only rule: a route with a
    still-attached T(C) continuation child cannot seal
    (`...:superseded-route`). ``validation_only`` is kept for callers that
    read a manifest back; it changes nothing here.
    """
    try:
        lineage = route_lineage.verified_route_lineage(dict(route), artifact_root=root)
    except route_lineage.RouteLineageError as exc:
        reason = "route-lineage-unverified"
        return Admission(False, reason, str(exc), [], _d120_next_action(reason))
    by_id = {r["route_id"]: r for r in lineage}
    begin_id = record.get("route_id")
    if begin_id not in by_id:
        reason = "cycle-route-binding-mismatch"
        return Admission(False, reason, f"begin={begin_id} not in lineage of {route.get('route_id')}",
                         [], _d120_next_action(reason))
    if by_id[begin_id].get("route_hash") != record.get("route_hash"):
        reason = "route-hash-drift"
        return Admission(False, reason, str(record.get("cycle_id", "")), [], _d120_next_action(reason))
    # lineage is [W, parent, ..., A]; the admitted path P runs begin (A) -> W.
    begin_index = next(i for i, r in enumerate(lineage) if r["route_id"] == begin_id)
    path = list(reversed(lineage[: begin_index + 1]))
    for node in path:
        if node.get("capability") != record.get("capability") or node.get("effective_intensity") != record.get("intensity"):
            reason = "cycle-route-binding-mismatch:material-input"
            return Admission(False, reason, f"route={node['route_id']}", path, _d120_next_action(reason))
    handed_over: Optional[frozenset] = None  # computed only when a refusal is about to be issued
    skipped_siblings: List[str] = []
    for node in path[:-1]:
        siblings = _lineage_children(root, node["route_id"], node["route_hash"])
        qualifying = [s for s in siblings
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        for sibling in qualifying:
            if sibling["route_id"] not in by_id:
                if handed_over is None:
                    handed_over = _handed_over_routes(root, record)
                if sibling["route_id"] in handed_over:
                    continue  # closed lineage: that branch belongs to another cycle
                if _unexecuted_continuation(root, sibling) and (
                        finalize or sealing or _closed_for_successor(node, path[path.index(node) + 1])):
                    # Sealing: a sibling that never ran and that nothing continues holds no result
                    # and runs no writer, so it is not a branch whatever closed its parent. Beginning
                    # a route in the cycle still needs the parent's verified handoff to the next
                    # route on the path (write-time fork protection).
                    skipped_siblings.append(sibling["route_id"])
                    continue
                reason = "cycle-route-binding-mismatch:lineage-fork"
                return Admission(False, reason, f"branch={node['route_id']} siblings={sibling['route_id']},{path[path.index(node)+1]['route_id']}",
                                 path, _d120_next_action(reason))
    if finalize:
        qualifying = [s for s in _lineage_children(root, route["route_id"], route["route_hash"])
                      if s.get("capability") == record.get("capability")
                      and s.get("effective_intensity") == record.get("intensity")]
        if qualifying:
            if handed_over is None:
                handed_over = _handed_over_routes(root, record)
            qualifying = [s for s in qualifying if s["route_id"] not in handed_over]
        if qualifying:
            reason = "cycle-route-binding-mismatch:superseded-route"
            return Admission(False, reason, route["route_id"], path, _d120_next_action(reason))
    return Admission(True, None, "", path, None, tuple(sorted(skipped_siblings)))


def _inline_producer_binding_check(root: Path, cycle_id: str,
                                   binding: Mapping[str, Any]) -> None:
    """Check a direct finish against D-120's verified route lineage.

    The cycle record names its *begin* route. A continuation may be the route
    that closes and seals it, so comparing the two route IDs would reject a
    valid finish and would bypass the common lineage predicate.
    """
    record = read_cycle_record(root, cycle_id)
    # A campaign deleted since is the campaign it was (§45 D-126): its runtime record stands in.
    campaign = campaign_or_tombstone(root, record["campaign_id"]) if record else None
    identity = artifact_lifecycle.read_root_identity(root)
    route_id = binding.get("route_id")
    if not isinstance(route_id, str) or not _ROUTE_ID_RE.fullmatch(route_id):
        raise ProducerError("inline-producer-binding-mismatch", "route-id")
    path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProducerError("inline-producer-binding-mismatch", "route-missing") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or path.resolve() != path:
        raise ProducerError("inline-producer-binding-mismatch", "route-kind")
    route = load_route(root, path)
    # The binding's campaign id/key are the campaign the cycle belonged to when it
    # was bound; a cycle moved since is the same cycle (§45 D-123), so identity
    # is judged by cycle, producer, route and the stored identity digest.
    if (not record or not campaign or not identity
            or binding.get("kind") != "inline_producer_binding_v1"
            or binding.get("cycle_id") != cycle_id
            or binding.get("producer_id") != record.get("producer_id")
            or binding.get("artifact_root_id") != identity.artifact_root_id
            or binding.get("route_hash") != route.get("route_hash")
            or not dispatch_terminal_commit.cycle_identity_matches(
                record, binding.get("cycle_record_digest"), campaign_id=binding.get("campaign_id"))
            or not binding.get("inline_finish_id")
            or not binding.get("terminal_marker_digest")
            or not binding.get("evidence_sha256")):
        raise ProducerError("inline-producer-binding-mismatch", cycle_id)
    admission = cycle_route_admission(
        root, record, route, finalize=True, validation_only=record.get("state") == "sealed")
    if not admission.allow:
        raise ProducerError(admission.reason or "inline-producer-binding-mismatch", admission.detail)
    import inline_finish
    finish = inline_finish.pending_state(root, route_id)
    intent = (finish or {}).get("intent") or {}
    if (not finish or finish.get("inline_finish_id") != binding["inline_finish_id"]
            or finish.get("terminal_marker_digest") != binding["terminal_marker_digest"]
            or intent.get("route_id") != route_id
            or intent.get("route_hash") != binding["route_hash"]
            or intent.get("cycle_id") != cycle_id
            or intent.get("producer_id") != record["producer_id"]
            or intent.get("artifact_root_id") != identity.artifact_root_id
            or intent.get("evidence_sha256") != binding["evidence_sha256"]):
        raise ProducerError("inline-producer-binding-mismatch", "finish-intent")


def resolve_cycle_manifest_route(root: Path, record: Mapping[str, Any],
                                 document: Mapping[str, Any],
                                 route_id: Optional[str] = None) -> Tuple[Path, Dict[str, Any]]:
    """Resolve and read-only admit the route a cycle manifest sealed.

    The cycle record's route tuple remains the begin identity.  A manifest may
    name its verified continuation leaf, so its route row is resolved
    through the canonical route directory and admitted against that begin
    identity.  A closed cycle a later route closed again carries one row per
    closing route (§45 D-127); `route_id` names the one a caller finished
    with, and the latest is meant when none is named.  This helper is safe for
    both pre-seal recovery and sealed replay; it never changes cycle state or
    consults ``route_bindings``.
    """
    rows = document.get("routes") if isinstance(document, Mapping) else None
    if (not isinstance(rows, list) or not rows or not all(isinstance(r, Mapping) for r in rows)
            or (len(rows) != 1 and not record.get("sealed_on") and record.get("state") != "sealed")):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    if route_id is not None:
        named = [r for r in rows if r.get("route_id") == route_id]
        if len(named) != 1:
            raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
        row = named[0]
    else:
        row = rows[-1]
    identity = artifact_lifecycle.read_root_identity(Path(root))
    if (identity is None or row.get("artifact_root_id") != identity.artifact_root_id
            or document.get("artifact_root_id") != identity.artifact_root_id
            or document.get("cycle", {}).get("cycle_id") != record.get("cycle_id")):
        raise ProducerError("completion-route-composite-mismatch", str(record.get("cycle_id", "")))
    route_id = row.get("route_id")
    if not isinstance(route_id, str) or not re.fullmatch(r"rt-[A-Za-z0-9][A-Za-z0-9._-]{0,126}", route_id):
        raise ProducerError("completion-route-composite-mismatch", "route-id")
    route_path = route_lineage.canonical_route_path(root, route_id)
    try:
        info = route_path.lstat()
    except OSError as exc:
        raise ProducerError("route-lineage-unverified", f"canonical-route-missing:{route_id}") from exc
    if not stat.S_ISREG(info.st_mode) or route_path.is_symlink() or route_path.resolve() != route_path:
        raise ProducerError("route-lineage-unverified", f"canonical-route-kind:{route_id}")
    target_check = artifact_lifecycle.validate_route_target(route_path, Path(root), route_id)
    if not target_check.ok:
        reason = target_check.reasons[0]
        raise ProducerError(reason.code, reason.detail)
    route = load_route(Path(root), route_path, expected_identity=row)
    # Only the latest row has to be the lineage's leaf; an earlier closing route is
    # judged by its place in the lineage, not by being the last one.
    admission = cycle_route_admission(Path(root), record, route, finalize=row is rows[-1], validation_only=True)
    if not admission.allow:
        raise ProducerError(admission.reason or "route-lineage-unverified", admission.detail)
    return route_path, route


def _route_binding_entry(route_row: Mapping[str, Any], root: Path, *, is_begin: bool) -> Dict[str, Any]:
    return {
        "route_id": route_row["route_id"],
        "route_hash": route_row["route_hash"],
        "route_file": str(route_lineage.canonical_route_path(root, route_row["route_id"])),
        "basis": "begin" if is_begin else "continuation",
        "continuation_id": None if is_begin else route_row["route_id"],
        "source_route_id": None if is_begin else route_row.get("source_route_id"),
    }


def _binding_core(entry: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: entry.get(key) for key in
            ("route_id", "route_hash", "route_file", "basis", "continuation_id", "source_route_id")}


def _bind_cycle_route_locked(root: Path, record: Mapping[str, Any], route: Mapping[str, Any],
                             *, sealing: bool = False) -> Dict[str, Any]:
    """`bind_cycle_route`'s body, for a caller that already holds the admission lock."""
    admission = cycle_route_admission(root, record, route, sealing=sealing)
    if not admission.allow:
        raise ProducerError(admission.reason, admission.detail)
    expected = [_route_binding_entry(row, root, is_begin=(i == 0)) for i, row in enumerate(admission.path)]
    # A record that never had `route_bindings` at all reads as the compat
    # single-element view (begin route only) -- a legitimate, common state.
    # A record whose `route_bindings` field is *present but empty* is not:
    # every real writer only ever stores a non-empty list (at least the begin
    # entry), so an explicit `[]` can only be tampering (A-25.9 (k)) and must
    # not be silently absorbed into the same "nothing bound yet" prefix match.
    if "route_bindings" not in record:
        stored = [{**_route_binding_entry(admission.path[0], root, is_begin=True),
                  "bound_at": record.get("started_on")}]
    else:
        stored = list(record.get("route_bindings") or [])
    stored_core = [_binding_core(e) for e in stored]
    expected_core = [_binding_core(e) for e in expected]
    advisory = None
    if stored and stored_core == expected_core[: len(stored_core)]:
        new_list = stored + [dict(e, bound_at=_rfc3339()) for e in expected[len(stored_core):]]
    else:
        index = 0
        field = "route_id"
        for index in range(min(len(stored_core), len(expected_core))):
            mismatched = [k for k in expected_core[index] if stored_core[index].get(k) != expected_core[index].get(k)]
            if mismatched:
                field = mismatched[0]
                break
        else:
            index = min(len(stored_core), len(expected_core))
        advisory = f"route-binding-record-drift:index={index};field={field}"
        new_list = [dict(e, bound_at=(stored[i].get("bound_at") if i < len(stored) else _rfc3339()))
                    for i, e in enumerate(expected)]
    written = new_list != stored
    if written:
        _write_cycle_record(root, {**record, "route_bindings": new_list}, exclusive=False)
    return {"written": written, "advisory": advisory}


def bind_cycle_route(root: Path, cycle_id: str, route: Mapping[str, Any]) -> Dict[str, Any]:
    """D-120's one audit-record writer: `route_bindings[]` derived from the sealed lineage.

    Re-judges admission under the admission lock and only writes on allow.
    The write is append-if-prefix, replace-with-advisory otherwise -- never an
    input to any judgment (D-120 "권한은 봉인 파일에서만 나온다"). Returns
    ``{"written": bool, "advisory": str|None}``.
    """
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        return _bind_cycle_route_locked(root, record, route)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _write_journal(root: Path, cycle_id: str, **fields: Any) -> None:
    path = journal_path(root, cycle_id)
    _ensure_dir(path.parent)
    payload = {"schema_version": 1, "cycle_id": cycle_id, "updated_at": _rfc3339()}
    previous = _read_json(path) or {}
    if isinstance(previous, Mapping) and previous.get("cycle_id", cycle_id) == cycle_id:
        payload.update(previous)
    payload.update(fields)
    payload["updated_at"] = _rfc3339()
    _write_atomic(path, _json_bytes(payload), 0o600)


def _remove_journal(root: Path, cycle_id: str) -> None:
    try:
        journal_path(root, cycle_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# route helpers
# ---------------------------------------------------------------------------


_ROUTE_ID_RE = re.compile(r"^rt-[0-9a-f]{6,}$")


def resolve_route_argument(root: Path, value: "str | Path") -> Path:
    """Accept either a route file path or a bare route id.

    A `quick` dispatch-depth-1 owner is handed only the route **id** — the
    prompt and the wrapper args carry `--route-id`, never the file path — so an
    owner that passed that id straight through used to die with
    `route-unreadable` naming the id it was given. A bare id is unambiguous: it
    resolves to exactly one canonical location under the artifact root. Resolve
    it instead of refusing. Anything else is returned untouched and is read as
    the path it is, so an explicit path always wins.
    """

    text = str(value)
    if _ROUTE_ID_RE.match(text):
        candidate = artifact_lifecycle.canonical_route_path(Path(root), text)
        if candidate.is_file():
            return candidate
    return Path(value)


def load_route(root: Path, route_file: Path, *,
               expected_identity: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    route_file = resolve_route_argument(root, route_file)
    route = _read_json(Path(route_file))
    if route is None:
        raise ProducerError("route-unreadable", str(route_file))
    for key in ("route_id", "route_hash", "capability", "effective_intensity", "artifact_root", "nodes"):
        if key not in route:
            raise ProducerError("route-invalid", f"missing {key}")
    if Path(str(route["artifact_root"])).resolve() != Path(root).resolve():
        raise ProducerError("route-artifact-root-mismatch")
    if route["effective_intensity"] not in INTENSITIES:
        raise ProducerError("route-invalid", "effective_intensity")
    if route.get("route_hash") != route_identity.route_hash(route):
        raise ProducerError("route-invalid", "stale or modified route hash")
    if "resplit_cycle_key" in route:
        nonce = route.get("resplit_route_nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", nonce):
            raise ProducerError("route-invalid", "resplit route nonce")
        expected_route_id = "rt-" + nonce.split(":", 1)[-1][:16]
    else:
        expected_route_id = "rt-" + str(route["route_hash"]).split(":", 1)[-1][:16]
    if route.get("route_id") != expected_route_id:
        raise ProducerError("route-invalid", "route id/hash mismatch")
    if "slug" in route:
        if not isinstance(route.get("slug"), str) or not isinstance(route.get("slug_truncated"), bool):
            raise ProducerError("route-invalid", "slug metadata")
        try:
            canonical_slug, _truncated = artifact_locator.slugify(route["slug"])
        except artifact_locator.LocatorError as exc:
            raise ProducerError("route-invalid", "slug metadata") from exc
        if canonical_slug != route["slug"]:
            raise ProducerError("route-invalid", "slug is not canonical")
    elif "slug_truncated" in route:
        raise ProducerError("route-invalid", "slug metadata incomplete")
    if expected_identity is not None and (
            route.get("route_id") != expected_identity.get("route_id")
            or route.get("route_hash") != expected_identity.get("route_hash")):
        raise ProducerError("completion-route-hash-mismatch", str(expected_identity.get("route_id", "")))
    return route


def route_is_closed(root: Path, route: Mapping[str, Any]) -> bool:
    try:
        outcome = artifact_lifecycle.canonical_outcome_path(root, route["route_id"])
    except artifact_lifecycle.LifecycleError:
        return False
    return outcome.is_file()


def _route_node(route: Mapping[str, Any], node_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not node_id or node_id == "-":
        return None
    for row in route.get("nodes", []):
        if isinstance(row, dict) and row.get("id") == node_id:
            return row
    raise ProducerError("route-node-unknown", node_id)


def _finalize_route(root: Path, record: Mapping[str, Any], *, deadline: Optional[float] = None) -> Dict[str, Any]:
    """D-120 finalize: R is the unique T(C) leaf -- the route with no
    material-input-qualifying continuation child. In a closed lineage the
    children that begin another cycle are that cycle's, so R stops before them
    (`closed_lineage_handover`). `--cycle`-only finalize has
    no other way to name R; a completion controller that already knows its
    exact route can seal it directly by checking `cycle_route_admission(...,
    finalize=True)` itself instead of calling this walk.  A background sweep passes its
    `deadline` (`time.monotonic()`): the walk stops before a level it has no time for and
    raises `scan-in-progress`, so the cycle stays open for the next sweep.
    """
    begin_route = load_route(root, Path(record["route_file"]))
    if begin_route["route_hash"] != record["route_hash"]:
        raise ProducerError("route-hash-drift", record["cycle_id"])
    current = begin_route
    visited = {current["route_id"]}
    handed_over: Optional[frozenset] = None  # computed once, and only when a child begins another cycle
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            raise ProducerError("scan-in-progress")
        candidates = [c for c in _lineage_children(root, current["route_id"], current["route_hash"])
                      if c.get("capability") == record.get("capability")
                      and c.get("effective_intensity") == record.get("intensity")]
        if candidates and handed_over is None:
            begin_ids = {rec.get("route_id") for rec in list_cycle_records(
                root, route_ids={c["route_id"] for c in candidates})
                         if rec.get("cycle_id") != record.get("cycle_id")}
            if any(c["route_id"] in begin_ids for c in candidates):
                handed_over = _handed_over_routes(root, record)
        if handed_over:
            candidates = [c for c in candidates if c["route_id"] not in handed_over]
        if not candidates:
            return current
        if len(candidates) > 1:
            # The same narrowed rule `cycle_route_admission` applies: when every
            # other candidate never ran, the remaining one is the branch (when none
            # ran, the parent's verified handoff names it). Two executed branches
            # remain the symmetric fork this refusal describes.
            chosen = [c for c in candidates
                      if all(other["route_id"] == c["route_id"]
                             or _unexecuted_continuation(root, other) for other in candidates)]
            if len(chosen) > 1:
                # Nothing executed: only the parent's verified handoff names the one to follow.
                chosen = [c for c in chosen if _closed_for_successor(current, c)]
            if len(chosen) == 1:
                candidates = chosen
            else:
                raise ProducerError(
                    "cycle-route-binding-mismatch:lineage-fork",
                    f"{current['route_id']}:{','.join(sorted(c['route_id'] for c in candidates))}",
                )
        nxt = candidates[0]
        if nxt["route_id"] in visited:
            raise ProducerError("route-lineage-unverified", f"cycle:{nxt['route_id']}")
        visited.add(nxt["route_id"])
        current = nxt


def finalize_lineage_evidence(root: Path, record: Mapping[str, Any]) -> Optional[str]:
    """Digest of this cycle's lineage routes and their outcome files.

    Only durable route/outcome shapes enter it: the jobs registry is shared and
    changes for every other attempt in the root, so it must not decide that a
    held finalize refusal is fresh. Bounded to the cycle's own starting tree.

    The begin route file's own (size, mtime_ns) is always a row, so a refusal
    caused by that file being replaced or damaged still has evidence to hold
    on; an unreadable or hash-drifted begin route falls back to that row alone.
    A broken record and a tree walk that cannot read its children stay unknown
    (`None`), never a refusal.
    """
    try:
        route_file = Path(record["route_file"])
        route_hash = record["route_hash"]
    except (KeyError, TypeError):
        return None
    if not isinstance(route_hash, str):
        return None
    try:
        info = route_file.stat()
        route_row: Optional[List[int]] = [info.st_size, info.st_mtime_ns]
    except OSError:
        route_row = None
    try:
        begin_route = load_route(root, route_file)
        readable = begin_route.get("route_hash") == route_hash
    except (OSError, KeyError, ValueError, TypeError, ProducerError):
        readable = False
    if not readable:
        return _digest(_canonical([("begin-route-file", route_row)]))
    try:
        rows: List[Tuple[str, Optional[List[int]]]] = [("begin-route-file", route_row)]
        queue = [begin_route]
        seen = {begin_route["route_id"]}
        while queue:
            node = queue.pop()
            outcome_path = route_lineage.canonical_route_path(
                root, node["route_id"]).with_suffix(".outcome.json")
            try:
                info = outcome_path.stat()
                outcome: Optional[List[int]] = [info.st_size, info.st_mtime_ns]
            except OSError:
                outcome = None
            rows.append((node["route_id"], outcome))
            for child in _indexed_route_children(root, node["route_id"], node["route_hash"]):
                if child["route_id"] not in seen:
                    seen.add(child["route_id"])
                    queue.append(child)
        return _digest(_canonical(sorted(rows, key=lambda row: row[0])))
    except (OSError, KeyError, ValueError, TypeError, ProducerError):
        return None
