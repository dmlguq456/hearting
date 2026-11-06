#!/usr/bin/env python3
"""Same-seat handover: who now answers for a registered depth-1 attempt.

A cleared window (``/clear``, ``/new``) is a new session id at the same herdr pane.  The
registry row of a depth-1 owner/frame the old session (A) launched still says
``parent_sid=A`` -- that stays, it is the registered identity the receipt, digest, worker
environment and gate paths are signed with.  What changes is *who may resume it and who
receives its completion*: the next session at the same pane (B).

The relation is one fact in two files under the session-tidy state folder:

``handover/<seat>.json``   the snapshot ``session_tidy.py enqueue`` takes: the registered
                           depth-1 attempts the tidying session answers for.  A card's
                           text never grants anything; only this runtime-made snapshot does.
``sessions/<seat>.jsonl``  the seat ledger; ``run_hook`` appends one ``event=handover`` row
                           (A -> B, with the bindings) when it sees B's confirmed start.

Rules: one direction (A -> B), never A -> B and A -> C, B -> C only through B's own next
tidy.  The session at the end of a chain answers for every attempt an earlier row bound unless
a later row handed it elsewhere.  Two ways in: a confirmed clear at the same pane and harness (a ``clear`` start source
or the clear booking's own observation; OpenCode has no start hook, so its first message in
a new session after the snapshot), or the official seat change across panes and harnesses
(``peer-steward start --beside`` -> ACK -> ``retire`` from the pane started beside the
predecessor, :func:`record_retire_handover`).  Everything here is read-only except
:func:`record_locked` / :func:`write_snapshot_locked` (caller holds the seat lock) and
:func:`record_retire_handover`.

Two questions, two functions:

* :func:`effective_parent` / :func:`owns` -- which session may resume the attempt (B).
* :func:`storage_recipients` -- which pending-delivery directories (A's, keyed by the
  registered ``parent_sid``) hold records addressed to B, and for which attempts.

Both answers come from one lineage judgment (:func:`_lineage_holder`), so delivery and resume
authority never disagree. Both answer "the registered parent" when usable state has no
relation. Unavailable lineage leaves authority unresolved until observation recovers.
:func:`record_retire_handover` reports an unreadable registry typed so its caller
keeps the accepted handover pending instead of treating unknown ownership as empty.
"""

from __future__ import annotations
from collections import OrderedDict
import threading

import contextlib
from dataclasses import dataclass, replace
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import time
from types import MappingProxyType
from typing import Iterable, Optional, NamedTuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

ROOT = HERE.parent
SCHEMA = 1
ROW_EVENT = "handover"
WORKER_TYPES = frozenset({"owner", "frame", "review"})
OPEN_ROW_STATUSES = frozenset({"open", "running"})
LINE_MAX_BYTES = 520
KEEP_ROWS = 40
LINEAGE_WRITE_KEY = "lineage-write"
_LINEAGE_GRAPH_CACHE = {}


@dataclass(frozen=True)
class LineageGraph:
    """Immutable normalized lineage snapshot for one sessions-ledger generation."""
    edges: tuple[tuple[str, str, str], ...]
    unresolved: frozenset[tuple[str, str]]
    ledger_keys: object
    ledger_rows: object
    binding_rows: object
    generation: tuple
    seat_edges: tuple
    seat_unresolved: frozenset
    binding_conflicts: frozenset = frozenset()


def _lineage_generation(info):
    return (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns,
            info.st_size, info.st_mode)


def _lineage_signature(directory: Path):
    """Identify file and directory incarnations, including permission changes."""
    location = str(directory.resolve(strict=False))
    try:
        directory_info = directory.stat()
    except FileNotFoundError:
        return location, (), None
    if not stat.S_ISDIR(directory_info.st_mode):
        raise OSError("lineage-directory-unavailable")
    files = []
    # glob/is_file can suppress observation failures and resemble an empty scan.
    for path in sorted(directory.iterdir()):
        if path.suffix != ".jsonl":
            continue
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("lineage-ledger-unavailable")
        files.append((path.name, *_lineage_generation(info)))
    generation = _lineage_generation(directory_info)
    if _lineage_generation(directory.stat()) != generation:
        raise OSError("lineage-directory-changed")
    return location, tuple(files), generation


def _appended_since(observed, current) -> bool:
    """Whether a ledger only grew since ``observed``: the same file and mode, no fewer bytes.

    Seat ledgers are append-only (`O_APPEND`) and are only ever replaced whole (a rename),
    so their first ``observed`` size bytes are still exactly what that observation saw.
    A second replace can reuse the inode number (ext4 does), so this check alone does not
    see a replacement: the directory-generation check every reader makes catches it.
    """
    observed, current = tuple(observed), tuple(current)
    return ((observed[0], observed[1], observed[5]) == (current[0], current[1], current[5])
            and current[4] >= observed[4])


def _observed_ledger_bytes(path: Path, file_generation) -> bytes:
    """The ledger bytes its stat observed. Another session appending meanwhile is not a fault;
    a replaced, shrunk or unreadable ledger still is."""
    expected = tuple(file_generation)
    raw = _st().read_bytes(path)
    if (raw is not None and len(raw) >= expected[4]
            and _appended_since(expected, _lineage_generation(path.lstat()))):
        return raw[:expected[4]]
    raise OSError("lineage-ledger-unavailable")


def _observation_holds(observed, current) -> bool:
    """Whether a later stat still describes what ``observed`` read: the same directory and
    ledgers, each unchanged or only appended to."""
    (location, files, generation), (now_location, now_files, now_generation) = observed, current
    if location != now_location or generation != now_generation or len(files) != len(now_files):
        return False
    return all(old[0] == new[0] and _appended_since(old[1:], new[1:])
               for old, new in zip(files, now_files))


def _snapshot_signature():
    """Stat-only identity of every handover snapshot the validator reads."""
    try:
        names = sorted(entry.name for entry in os.scandir(_snapshot_dir())
                       if entry.name.endswith(".json"))
    except FileNotFoundError:
        return ()
    observed = []
    for name in names:
        info = os.lstat(_snapshot_dir() / name)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("handover-snapshot-unsafe")
        observed.append((name, *_lineage_generation(info)))
    return tuple(observed)


def _readable_lineage_signature(directory: Path):
    """Observe current readable bytes once and retain them for the parser.

    Stat alone cannot expose transient read failures. Use the normal no-follow
    reader even on cache hits, binding its bytes to the observed generation.
    """
    location, files, generation = _lineage_signature(directory)
    observed, contents = [], {}
    for filename, *file_generation in files:
        raw = _observed_ledger_bytes(directory / filename, file_generation)
        observed.append((filename, *file_generation, hashlib.sha256(raw).hexdigest()))
        contents[filename] = raw
    if generation is not None and _lineage_generation(directory.stat()) != generation:
        raise OSError("lineage-directory-changed")
    return (location, tuple(observed), generation), contents


def _build_lineage_graph(read_context=None) -> LineageGraph:
    directory = _st().state_root() / "sessions"
    try:
        if getattr(read_context, "compact_lineage", False):
            compact = _build_compact_lineage_graph(directory, read_context)
            if compact is not None:
                return compact
        # Snapshots are not a second lineage reader; their unreadable schema
        # still prevents a current authority judgment.
        _all_snapshots()
        signature, contents = _readable_lineage_signature(directory)
        metadata = (signature[0], tuple(item[:-1] for item in signature[1]), signature[2])
        cached = _LINEAGE_GRAPH_CACHE.get(signature)

        def load():
            if cached is not None:
                return cached
            graph = _build_lineage_graph_uncached(signature, contents)
            # A replacement while the parser was reading must not publish a
            # graph under the earlier observation's identity.
            if not _observation_holds(metadata, _lineage_signature(directory)):
                raise OSError("lineage-observation-changed")
            _LINEAGE_GRAPH_CACHE.clear()
            _LINEAGE_GRAPH_CACHE[signature] = graph
            return graph

        graph = load()
        if read_context is not None:
            # A normal observation replaces the pass, even if only readable
            # bytes changed. Metadata never substitutes for this observation.
            read_context.invalidate_lineage_graph()
            read_context.lineage_graph(metadata, lambda: graph)
        return graph
    except (OSError, ValueError, TypeError):
        _LINEAGE_GRAPH_CACHE.clear()
        if read_context is not None:
            read_context.invalidate_lineage_graph()
        raise


def _build_compact_lineage_graph(directory: Path, read_context=None):
    """Reuse published compact rows for every unchanged ledger, stat only.

    The effect path re-observes current lineage per claim group. An unchanged
    ledger's recorded generation tuple is exactly the bytes already parsed into
    ``lineage-rows.json``, so a transient read fault on it cannot block a
    delivery (approved notice 3 in the plan). A changed or unlisted ledger keeps
    the normal no-follow read and still fails closed. Returns ``None`` when the
    derived file is missing or unusable so the caller runs the full read.
    """
    import dispatch_recipient_inbox as inbox

    try:
        payload = inbox.read_lineage_rows(inbox.dispatch_inbox_root())
    except (OSError, ValueError, TypeError):
        return None
    if payload is None:
        return None
    try:
        current_snapshots = [list(item) for item in _snapshot_signature()]
    except OSError:
        return None
    if payload.get("snapshots") != current_snapshots:
        # Changed or unpublished snapshots keep the full fail-closed read.
        _all_snapshots()
    location, files, generation = _lineage_signature(directory)
    if payload.get("directory") != location:
        return None
    ledgers = payload.get("ledgers") if isinstance(payload.get("ledgers"), dict) else {}
    contents, compact_rows = {}, {}
    for filename, *file_generation in files:
        reusable = _compact_rows_from_record(ledgers.get(Path(filename).stem), file_generation)
        if reusable is not None:
            compact_rows[filename] = reusable
            continue
        raw = _observed_ledger_bytes(directory / filename, file_generation)
        rows = _st()._read_ledger_lines(_st().Seat("pane", Path(filename).stem), raw=raw)
        if not getattr(rows, "usable", True):
            raise OSError("lineage-ledger-unavailable")
        # Changed ledgers need a fresh readable observation, but activity rows
        # need not flow through the relation graph's repeated normalization.
        compact_rows[filename] = [(index, rows[index])
                                  for index in sorted(_compact_row_indexes(rows))]
    if not _observation_holds((location, files, generation), _lineage_signature(directory)):
        raise OSError("lineage-observation-changed")
    graph = _build_lineage_graph_uncached((location, files, generation), contents,
                                          compact_rows=compact_rows)
    if read_context is not None:
        read_context.invalidate_lineage_graph()
        read_context.lineage_graph((location, files, generation), lambda: graph)
    return graph


def _compact_rows_from_record(record, generation):
    """Indexed rows for one unchanged ledger, or ``None`` to read its bytes."""
    if not isinstance(record, dict):
        return None
    stored = record.get("generation")
    if (not isinstance(stored, (list, tuple)) or len(stored) != len(generation)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in stored)
            or tuple(stored) != tuple(generation)):
        return None
    rows = record.get("rows")
    if not isinstance(rows, list):
        return None
    indexed = []
    for item in rows:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not isinstance(item[0], int) or isinstance(item[0], bool) or item[0] < 0
                or not isinstance(item[1], dict)):
            return None
        indexed.append((item[0], item[1]))
    return indexed


def _compact_row_indexes(rows) -> set[int]:
    """Rows any lineage resolver reads; the rest only narrows activity history.

    Union of rows carrying bindings, rows either edge helper accepts, explicit
    ``handover`` events, and each ledger's first row per ``(harness, sid)``.
    The last entry keeps ``ledger_keys``/``keys_by_vertex`` identical without
    every activity row.
    """
    st = _st()
    keep, first_vertices = set(), set()
    for index, row in enumerate(rows):
        if row.get("bindings") or row.get("event") == "handover":
            keep.add(index)
        elif st.tidy_lineage_edge(row) is not None or st._handover_origin(row) is not None:
            keep.add(index)
        harness, sid = row.get("harness"), row.get("sid")
        if isinstance(harness, str) and harness and isinstance(sid, str) and sid:
            vertex = (harness, sid)
            if vertex not in first_vertices:
                first_vertices.add(vertex)
                keep.add(index)
    return keep


def build_lineage_rows_payload(directory=None) -> dict:
    """The compact relation rows ``lineage-rows.json`` must carry right now."""
    st = _st()
    directory = Path(directory) if directory is not None else st.state_root() / "sessions"
    location, files, generation = _lineage_signature(directory)
    ledgers, skipped = {}, []
    for filename, *file_generation in files:
        ledger_key = Path(filename).stem
        raw = _observed_ledger_bytes(directory / filename, file_generation)
        rows = st._read_ledger_lines(st.Seat("pane", ledger_key), raw=raw)
        if not getattr(rows, "usable", True):
            # No entry means the effect path re-reads this ledger and fails
            # closed exactly as the full observation does.
            skipped.append(ledger_key)
            continue
        keep = _compact_row_indexes(rows)
        ledgers[ledger_key] = {
            "generation": list(file_generation),
            "rows": [[index, dict(rows[index])] for index in sorted(keep)],
        }
    if not _observation_holds((location, files, generation), _lineage_signature(directory)):
        raise OSError("lineage-observation-changed")
    # The effect path may skip re-reading unchanged snapshots only when the
    # runner validated exactly these incarnations in this same publication.
    snapshot_before = _snapshot_signature()
    _all_snapshots()
    snapshot_after = _snapshot_signature()
    if snapshot_before != snapshot_after:
        raise OSError("handover-snapshot-changed")
    return {"directory": location,
            "generated_at": time.time(),
            "ledgers": ledgers,
            "skipped": skipped,
            "snapshots": [list(item) for item in snapshot_after]}


def publish_lineage_rows(dispatch_root=None, *, expected_root_identity=None) -> dict:
    """Atomically publish the compact rows; only the existing runner calls this."""
    import dispatch_recipient_inbox as inbox

    root = Path(dispatch_root) if dispatch_root is not None else inbox.dispatch_inbox_root()
    payload = build_lineage_rows_payload()
    inbox.write_lineage_rows(root, payload,
                             expected_root_identity=expected_root_identity)
    return {"path": str(inbox.lineage_rows_path(root)),
            "ledgers": len(payload["ledgers"]),
            "skipped": len(payload["skipped"]),
            "rows": sum(len(entry["rows"]) for entry in payload["ledgers"].values())}


def _pass_lineage_graph(read_context=None) -> LineageGraph:
    """Resolve read-only records from this pass; effects observe without it.

    Detect writer changes during a pass, but do not reopen every ledger for
    every record. A normal lineage_graph observation reads each ledger's bytes as of
    that ledger's own stat.
    """
    if read_context is None:
        return lineage_graph()
    try:
        signature = read_context.lineage_signature(
            lambda: _lineage_signature(_st().state_root() / "sessions"))
        return read_context.lineage_graph(signature, lambda: lineage_graph(read_context=read_context))
    except (OSError, ValueError, TypeError):
        _LINEAGE_GRAPH_CACHE.clear()
        read_context.invalidate_lineage_graph()
        raise


def _build_lineage_graph_uncached(signature, contents, compact_rows=None) -> LineageGraph:
    st = _st()
    edges, keys_by_vertex, rows_by_key, latest_bindings = set(), {}, {}, {}
    for filename, *_generation in signature[1]:
        ledger_key = Path(filename).stem
        if compact_rows is not None and filename in compact_rows:
            # Original indexes travel with the published rows so the latest
            # binding order stays ``(ts, index, ledger_key)``.
            indexed = compact_rows[filename]
        else:
            seat = st.Seat("pane", ledger_key)
            # Bytes were freshly observed above. These graph builders only read
            # row records, so a passive scan can reuse their pure ledger parse.
            with st.seat_ledger_readonly_scope():
                rows = st._read_ledger_lines(seat, raw=contents[filename])
            if not getattr(rows, "usable", True):
                raise OSError("lineage-ledger-unavailable")
            indexed = list(enumerate(rows))
        rows_by_key[ledger_key] = [row for _index, row in indexed]
        for index, row in indexed:
            row_harness, row_sid = row.get("harness"), row.get("sid")
            if isinstance(row_harness, str) and row_harness and isinstance(row_sid, str) and row_sid:
                keys_by_vertex.setdefault((row_harness, row_sid), set()).add(ledger_key)
            edge = st.tidy_lineage_edge(row)
            if edge is None:
                pass
            else:
                harness, origin, target = edge
                edges.add((harness, origin, target))
                keys_by_vertex.setdefault((harness, origin), set()).add(ledger_key)
                keys_by_vertex.setdefault((harness, target), set()).add(ledger_key)
            for binding in row.get("bindings") or ():
                if not isinstance(binding, dict):
                    continue
                key = tuple(binding.get(field) for field in ("jobs", "route", "hash", "node"))
                if not all(isinstance(value, str) for value in key):
                    continue
                order = (float(row.get("ts", 0) or 0), index, ledger_key)
                if key not in latest_bindings or order > latest_bindings[key][0]:
                    latest_bindings[key] = (order, row)
    unresolved = _unresolved_vertices({((h, origin), (h, target))
                                       for h, origin, target in edges})
    frozen_rows = MappingProxyType({key: tuple(MappingProxyType(dict(row)) for row in rows)
                                    for key, rows in rows_by_key.items()})
    frozen_bindings = MappingProxyType({key: MappingProxyType(dict(value[1]))
                                        for key, value in latest_bindings.items()})
    seat_edges, seat_unresolved = _seat_graph(rows_by_key, edges)
    graph = LineageGraph(tuple(sorted(edges)), frozenset(unresolved),
                        MappingProxyType({vertex: tuple(sorted(keys))
                                          for vertex, keys in keys_by_vertex.items()}), frozen_rows,
                        frozen_bindings, signature, seat_edges, seat_unresolved)
    return replace(graph, binding_conflicts=_binding_conflicts(graph))



def _seat_graph(rows_by_key, edges):
    qualified = {((h, origin), (h, target)) for h, origin, target in edges}
    merge_edges = set(qualified)
    vertices = {vertex for pair in qualified for vertex in pair}
    legacy = set()
    for rows in rows_by_key.values():
        for row in rows:
            if row.get("event") != "handover" or row.get("source") != "retire":
                continue
            origin = _st()._handover_origin(row)
            if origin is None:
                raise ValueError("handover-origin-invalid")
            target = (row["harness"], row["sid"])
            vertices.add(target)
            if isinstance(row.get("from"), str):
                legacy.add((origin[1], target))
            else:
                qualified.add((origin, target))
                vertices.add(origin)
    for sid, target in legacy:
        origins = {vertex for vertex in vertices if vertex[1] == sid}
        pairs = {(origin, target) for origin in origins or {("", sid)}}
        qualified.update(pairs)
        if len(origins) > 1:
            # An unqualified legacy source cannot choose between harnesses.
            merge_edges.update(pairs)
    # Retired work may join an existing holder and its normal tidy suffix.
    # This converges notices; it does not merge native conversation history.
    # Forks/cycles still use every edge, and exact jobs keep their own bindings.
    return tuple(sorted(qualified)), _unresolved_vertices(qualified, merge_edges=merge_edges)


def _unresolved_vertices(qualified, *, merge_edges=None):
    """Forks block their ancestors; merges and cycles block their component.

    Kahn elimination visits every edge, including cycles through a fork. Any
    remainder belongs to a component containing a cycle; propagation below
    intentionally invalidates that entire component, including its tails.
    The notice overlay limits merge detection to conversation edges and
    ambiguous legacy origins, allowing accepted retire fan-in only.
    """
    outgoing, incoming, adjacent = {}, {}, {}
    for origin, target in qualified:
        outgoing.setdefault(origin, set()).add(target)
        incoming.setdefault(target, set()).add(origin)
        adjacent.setdefault(origin, set()).add(target)
        adjacent.setdefault(target, set()).add(origin)
    degree = {v: len(incoming.get(v, ())) for v in adjacent}
    ready = [v for v in degree if degree[v] == 0]
    while ready:
        for target in outgoing.get(ready.pop(), ()):
            degree[target] -= 1
            if degree[target] == 0:
                ready.append(target)
    merge_incoming = incoming
    if merge_edges is not None:
        merge_incoming = {}
        for origin, target in merge_edges:
            merge_incoming.setdefault(target, set()).add(origin)
    invalid = {v for v in adjacent if degree[v] or len(merge_incoming.get(v, ())) > 1}
    pending = list(invalid)
    while pending:
        for neighbor in adjacent.get(pending.pop(), ()):
            if neighbor not in invalid:
                invalid.add(neighbor)
                pending.append(neighbor)
    pending = [v for v in adjacent if len(outgoing.get(v, ())) > 1]
    invalid.update(pending)
    while pending:
        for origin in incoming.get(pending.pop(), ()):
            if origin not in invalid:
                invalid.add(origin)
                pending.append(origin)
    return frozenset(invalid)


def _binding_conflicts(graph):
    """Compare every exact handoff, not just the chronologically latest row.

    Duplicate logical/physical publications collapse to one transfer. Explicit
    transfers take priority over implicit succession, including cross-harness
    retire. Multiple targets are safe only if their exact transfers and normal
    suffixes converge; a timestamp cannot choose between different fork arms.
    """
    transfers = {}
    for rows in graph.ledger_rows.values():
        for row in rows:
            origin = _st()._handover_origin(row)
            target = (row.get("harness"), row.get("sid"))
            for binding in row.get("bindings") or ():
                if not isinstance(binding, dict):
                    continue
                key = tuple(binding.get(field) for field in ("jobs", "route", "hash", "node"))
                if all(isinstance(value, str) for value in key) and key in graph.binding_rows:
                    legacy = isinstance(row.get("from"), str) and _st().tidy_lineage_edge(row) is None
                    transfers.setdefault(key, set()).add((origin, target, legacy))
    implicit = {(h, origin): (h, target) for h, origin, target in graph.edges
                if (h, origin) not in graph.unresolved}
    vertices_by_sid = {}
    for pair in graph.seat_edges:
        for vertex in pair:
            vertices_by_sid.setdefault(vertex[1], set()).add(vertex)
    conflicts = set()
    for key, pairs in transfers.items():
        targets = {target for _origin, target, _legacy in pairs}
        if len(targets) < 2:
            continue
        exact, targets_by_sid = {}, {}
        for target in targets:
            targets_by_sid.setdefault(target[1], set()).add(target)
        for origin, target, legacy in pairs:
            if legacy and origin:
                # Schema-1 strings have no source harness. Like _seat_graph,
                # qualify only a unique known SID, including earlier exact
                # holders whose legacy handoffs are not implicit tidy edges.
                candidates = {v for v in vertices_by_sid.get(origin[1], set())
                              | targets_by_sid.get(origin[1], set()) if v[0]}
                if len(candidates) == 1:
                    origin = next(iter(candidates))
            if origin and origin != target:
                exact.setdefault(origin, set()).add(target)
        ends, seen, pending = set(), set(), list(targets)
        # Resolve the union once per exact job. A directed cycle or branching
        # with distinct terminal holders cannot produce unique authority.
        links = set()
        while pending:
            vertex = pending.pop()
            if vertex in seen:
                continue
            seen.add(vertex)
            following = exact.get(vertex)
            if following is None:
                following = {implicit[vertex]} if vertex in implicit else set()
            if not following:
                ends.add(vertex)
            links.update((vertex, target) for target in following)
            pending.extend(following - seen)
        degree = dict.fromkeys(seen, 0)
        adjacency = {}
        for origin, target in links:
            degree[target] += 1
            adjacency.setdefault(origin, set()).add(target)
        ready = [v for v in degree if degree[v] == 0]
        while ready:
            for target in adjacency.get(ready.pop(), ()):
                degree[target] -= 1
                if degree[target] == 0:
                    ready.append(target)
        if len(ends) != 1 or any(degree.values()):
            conflicts.add(key)
    return frozenset(conflicts)


def _seat_end(graph, harness, session):
    vertices = {vertex for pair in graph.seat_edges for vertex in pair}
    start = (harness, session)
    if not harness:
        candidates = {vertex for vertex in vertices if vertex[1] == session}
        if len(candidates) > 1:
            raise ValueError("notice-lineage-unresolved")
        start = next(iter(candidates), start)
    elif start not in vertices and ("", session) in vertices:
        start = ("", session)  # Legacy accepted retire with no source harness.
    if start in graph.seat_unresolved:
        raise ValueError("notice-lineage-unresolved")
    outgoing = dict(graph.seat_edges)
    current = start
    while current in outgoing:
        current = outgoing[current]
    return (current[1], current[0] or harness,
            "successor" if current != start else "self")


def lineage_graph(*, read_context=None) -> LineageGraph:
    """Observe current readable lineage and seed this read-only pass's graph."""
    return _build_lineage_graph(read_context)


def chain_end(harness: str, sid: str, *, read_context=None) -> tuple[str, str]:
    """Return the unique end, or unresolved for ambiguity or unavailable observation."""
    try:
        return _chain_end(_pass_lineage_graph(read_context), harness, sid)
    except (OSError, ValueError, TypeError):
        return sid, "unresolved"


def _chain_end(graph: LineageGraph, harness: str, sid: str, *, traversed=None) -> tuple[str, str]:
    """Resolve once, optionally retaining exact edges for the transition writer."""
    vertex = (harness, sid)
    if vertex in graph.unresolved:
        return sid, "unresolved"
    outgoing = {}
    for edge_harness, origin, target in graph.edges:
        if edge_harness == harness:
            outgoing.setdefault(origin, set()).add(target)
    current, seen, moved = sid, {sid}, False
    while outgoing.get(current):
        targets = outgoing[current]
        if len(targets) != 1:
            return sid, "unresolved"
        target = next(iter(targets))
        if target in seen:
            return sid, "unresolved"
        if traversed is not None:
            traversed.add((harness, current, target))
        current, moved = target, True
        seen.add(current)
        if (harness, current) in graph.unresolved:
            return sid, "unresolved"
    return current, "successor" if moved else "self"


def seat_chain_contains(harness: str, origin: str, sid: str, *, read_context=None) -> bool:
    """Whether ``sid`` is ``origin`` or a confirmed successor on its same-harness chain.

    A fork, cycle or unreadable observation answers False, so a caller keeps its
    current disposition rather than reading an ambiguous seat as the same one."""
    if not origin or not sid:
        return False
    if sid == origin:
        return True
    traversed = set()
    try:
        _end, state = _chain_end(_pass_lineage_graph(read_context), harness, origin,
                                 traversed=traversed)
    except (OSError, ValueError, TypeError):
        return False
    return state != "unresolved" and any(target == sid for _h, _o, target in traversed)


class LineageAttempts(frozenset):
    """Legacy set-compatible attempt scope with separate route-free notice access."""
    lineage_notice = True


class RegistryUnreadable(OSError):
    """The jobs registry exists but its ownership rows could not be read.

    Distinct from "nothing currently owned": a caller keeps its acceptance
    continuation pending and retries the same observation instead of settling
    a transfer that never reached the seat ledger. ``reason`` names the state
    for callers that carry a string receipt rather than this class."""

    reason = "registry-unreadable"


def _st():
    import session_tidy
    return session_tidy


def _meta(pipe: str) -> dict:
    pairs = {}
    for part in pipe.split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            pairs[key.strip()] = value
    return pairs


def _jobs_key(jobs) -> str:
    return str(Path(jobs).resolve(strict=False))


def route_identity(meta: dict) -> tuple:
    """(route id, route hash, node) of a registry row: owner rows carry the route as ``route_id`` too."""
    return (meta.get("owner_route_id") or meta.get("route_id") or "",
            meta.get("owner_route_hash") or meta.get("route_hash") or "",
            meta.get("route_node") or "")


def eligible_row(meta: dict) -> bool:
    """A route-bound depth-1 owner/frame; depth-2, route-free and unregistered rows never qualify."""
    route, digest, _ = route_identity(meta)
    return (meta.get("dispatch_depth") == "1" and meta.get("worker_type") in WORKER_TYPES
            and bool(meta.get("parent_sid")) and bool(meta.get("attempt_id")) and bool(route) and bool(digest))


def latest_rows(jobs, attempts=None) -> dict:
    """Latest rows, optionally limited to addressed attempts through the existing index."""
    try:
        from dispatch_registry_cache import registry_view
        view = registry_view(Path(jobs))
        if attempts is None:
            return view.handover_latest()
        return {attempt: row for attempt in attempts
                if (row := view.handover_attempt(attempt)) is not None}
    except OSError:
        return {}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _snapshot_dir() -> Path:
    return _st().state_root() / "handover"


def _snapshot_path(seat_key: str) -> Path:
    return _snapshot_dir() / f"{seat_key}.json"


def read_snapshot(seat_key: str) -> Optional[dict]:
    data = _st().read_json(_snapshot_path(seat_key))
    if isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(data.get("bindings"), list) \
            and isinstance(data.get("from"), dict):
        return data
    return None


def _all_snapshots(read_context=None) -> list[dict]:
    if read_context is not None:
        return list(read_context.handover_snapshots(_all_snapshots_uncached))
    return _all_snapshots_uncached()


def _bindings_readable(bindings):
    return isinstance(bindings, list) and all(
        isinstance(binding, dict)
        and all(isinstance(binding.get(key), str) and bool(binding[key])
                for key in ("jobs", "route", "hash", "attempt"))
        and isinstance(binding.get("node"), str)
        and ("parent" not in binding or isinstance(binding["parent"], str) and bool(binding["parent"]))
        for binding in bindings)


def _snapshot_data_usable(data):
    seat = data.get('seat') if isinstance(data, dict) else None
    return (isinstance(seat, dict) and bool(seat.get('key'))
            and seat.get('kind') in {'pane', 'native', 'project'}
            and isinstance(data['from'].get('sid'), str) and bool(data['from']['sid'])
            and _bindings_readable(data.get('bindings')))


_PASSIVE_SNAPSHOT_SEATS = OrderedDict()
_PASSIVE_SNAPSHOT_LOCK = threading.RLock()


def _snapshot_seat_keys():
    from dispatch_supervision import passive_read_context
    if passive_read_context() is None:
        return {str(snapshot['seat']['key']) for snapshot in _all_snapshots()}
    directory = _snapshot_dir()
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith('.json'))
    except FileNotFoundError:
        return set()
    keys = set()
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('handover-snapshot-unsafe')
        def signature():
            st = path.stat()
            return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns,
                    st.st_ctime_ns, read_snapshot, _snapshot_data_usable, SCHEMA)
        before = signature()
        key = str(path)
        with _PASSIVE_SNAPSHOT_LOCK:
            held = _PASSIVE_SNAPSHOT_SEATS.get(key)
            if held is not None and held[0] == before:
                _PASSIVE_SNAPSHOT_SEATS.move_to_end(key)
                seat_key = held[1]
            else:
                held = None
        if held is None:
            data = read_snapshot(path.stem)
            seat_key = str(data['seat']['key']) if _snapshot_data_usable(data) else None
            if before == signature() and (seat_key is None or len(seat_key) <= 4096):
                with _PASSIVE_SNAPSHOT_LOCK:
                    _PASSIVE_SNAPSHOT_SEATS[key] = (before, seat_key)
                    _PASSIVE_SNAPSHOT_SEATS.move_to_end(key)
                    while len(_PASSIVE_SNAPSHOT_SEATS) > 128:
                        _PASSIVE_SNAPSHOT_SEATS.popitem(last=False)
        if seat_key is None:
            raise ValueError('handover-snapshot-invalid')
        keys.add(seat_key)
    return keys


def _all_snapshots_uncached() -> list[dict]:
    try:
        with os.scandir(_snapshot_dir()) as entries:
            names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        path = _snapshot_dir() / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("handover-snapshot-unsafe")
        data = read_snapshot(path.stem)
        if not _snapshot_data_usable(data):
            raise ValueError("handover-snapshot-invalid")
        out.append(data)
    return out


def handover_rows(seat, *, read_context=None) -> list[dict]:
    """The seat ledger's handover rows, oldest first."""
    def load():
        st = _st()
        with st.seat_ledger_readonly_scope():
            return [e for e in st._read_ledger_lines(seat, strict=True)
                    if e.get("event") == ROW_EVENT]

    rows = (read_context.handover_rows(seat, load) if read_context is not None else load())
    return sorted(rows, key=lambda r: float(r.get("ts", 0) or 0))


def _peer_message():
    """Load the shared steward-evidence reader without duplicating its rule."""
    path = HERE / "peer-message.py"
    existing = sys.modules.get("peer_message")
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location("peer_message", str(path))
    if spec is None or spec.loader is None:
        raise ImportError("peer-message-reader-unavailable")
    module = importlib.util.module_from_spec(spec)
    # Watch resolution calls this for each retained message. Cache code, never
    # its state reads: every call below still opens the current arm and ledger.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _transition_rows(seat_key: Optional[str] = None) -> list[tuple[str, dict]]:
    """Read only confirmed steward transitions and releases from the existing seat ledgers."""
    st = _st()
    directory = st.state_root() / "sessions"
    paths = ([directory / f"{seat_key}.jsonl"] if seat_key else
             sorted(path for path in directory.glob("*.jsonl") if path.is_file() and not path.is_symlink()))
    out = []
    for path in paths:
        if path.is_symlink():
            continue
        key = path.stem
        with st.seat_ledger_readonly_scope():
            for row in st._read_ledger_lines(st.Seat("pane", key)):
                if row.get("event") in {ROW_EVENT, "steward-release"}:
                    out.append((key, row))
    return out


def _role_identity(relation: dict) -> str:
    """Role evidence, independent of the ledger that happens to carry it."""
    return json.dumps({field: relation.get(field) for field in (
        "seat_key", "generation", "origin", "holder", "marker_digest", "targets", "lineage")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _role_follows(before: dict, after: dict) -> bool:
    """Only an exact extension of the recorded same-harness chain supersedes a role."""
    if any(before.get(field) != after.get(field) for field in
           ("seat_key", "generation", "origin", "marker_digest", "targets")):
        return False
    prefix, lineage = before.get("lineage") or [], after.get("lineage") or []
    if len(prefix) >= len(lineage) or lineage[:len(prefix)] != prefix:
        return False
    holder = before.get("holder") or {}
    successor = after.get("holder") or {}
    if holder.get("harness") != successor.get("harness"):
        return False
    sid = holder.get("sid")
    for edge in lineage[len(prefix):]:
        if (not isinstance(edge, dict) or edge.get("from") != sid
                or not edge.get("to") or not edge.get("snapshot_id")):
            return False
        sid = edge["to"]
    return sid == successor.get("sid")


def _role_transition_matches(identity: tuple, relation: dict) -> bool:
    lineage = relation.get("lineage") or []
    if identity[1] == "marker" or not lineage:
        return True  # physical-only legacy evidence remains readable
    edge = json.loads(identity[1])
    last = lineage[-1]
    return (isinstance(last, dict)
            and (edge.get("from"), edge.get("sid"), edge.get("snapshot_id"))
            == (last.get("from"), last.get("to"), last.get("snapshot_id"))
            and edge.get("harness") == (relation.get("holder") or {}).get("harness"))


def _current_roles(states: dict) -> dict:
    eligible = {identity: _role_transition_matches(identity, relation)
                for identity, relation in states.items()}
    groups = {}
    unhashable = object()
    generation_key = {}
    for identity, relation in states.items():
        generation = relation.get('generation')
        try:
            hash(generation)
        except TypeError:
            generation = unhashable
        generation_key[identity] = generation
        if eligible[identity]:
            groups.setdefault(generation, []).append(relation)
    return {identity: relation for identity, relation in states.items()
            if not (eligible[identity] and any(_role_follows(relation, other)
                    for other in groups.get(generation_key[identity], ())))}


def _role_actor(relation: dict, harness: str, sid: str) -> bool:
    return any(relation.get(field) == {"harness": harness, "sid": sid}
               for field in ("origin", "holder"))


def _role_states(seat_key: Optional[str] = None, *, rows=None, marker_relations=()) -> tuple[dict, set]:
    states, released = {}, set()
    canonical = {}
    for relation in marker_relations:
        identity = (_role_identity(relation), "marker")
        states[identity] = relation
        if relation.get("seat_key"):
            canonical.setdefault(relation["seat_key"], {})[identity] = relation
    for key, row in (_transition_rows(seat_key) if rows is None else rows):
        state = row.get("steward") if isinstance(row.get("steward"), dict) else {}
        for relation in state.get("roles") or ():
            if not isinstance(relation, dict) or not relation.get("generation"):
                continue
            item = dict(relation)
            item["seat_key"] = item.get("seat_key") or key
            # The confirmed edge also belongs to identity. A conflicting alias
            # cannot borrow authority merely by copying a generation or payload.
            edge = json.dumps({field: row.get(field) for field in
                               ("seat_key", "harness", "from", "sid", "snapshot_id", "source")},
                              sort_keys=True, separators=(",", ":"))
            identity = (_role_identity(item), edge)
            states[identity] = item
            if key == item["seat_key"]:
                canonical.setdefault(key, {})[identity] = item
        if (row.get("event") == "steward-release" and row.get("generation")
                and row.get("seat_key", key) == key):
            # Old release rows have no full payload. Bind them to the unique
            # actor-compatible relation at this point in their canonical ledger.
            history = {identity: relation for identity, relation in canonical.get(key, {}).items()
                       if identity[1] != "marker"
                       and str(relation.get("generation")) == str(row["generation"])}
            basis = history or {identity: relation for identity, relation in canonical.get(key, {}).items()
                                if str(relation.get("generation")) == str(row["generation"])}
            candidates = {identity: relation for identity, relation in _current_roles(basis).items()
                          if _role_actor(relation, row.get("harness"), row.get("sid"))}
            if len(candidates) == 1:
                released.update(candidates)
    return _current_roles(states), released


def steward_marker_generation(marker: dict) -> str:
    """Stable evidence-generation key, including for source-less legacy markers."""
    encoded = json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return str(marker.get("generation") or digest)


def _marker_relation(harness: str, sid: str, marker: dict, evidence: list[dict], seat_key=None) -> dict:
    encoded = json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return {
        "generation": steward_marker_generation(marker),
        "marker_digest": digest,
        "origin": {"harness": harness, "sid": sid},
        "holder": {"harness": harness, "sid": sid},
        "targets": [dict(target) for target in evidence],
        "lineage": [],
        "seat_key": marker.get("seat_key") or seat_key,
        "released": False,
    }


def resolved_steward_relations(harness: Optional[str] = None, sid: Optional[str] = None, *,
                               seat_key: Optional[str] = None, include_released: bool = False) -> list[dict]:
    """Resolve valid original role evidence to its confirmed current same-seat holder.

    This is a read-only projection. It never creates or copies a marker. Ordinary
    handover rows and session aliases are not role evidence.
    """
    try:
        pm = _peer_message()
        roots, _source = pm.steward_marker_roots()
        markers = pm.read_steward_markers(roots)
        if seat_key is not None:
            rows = _transition_rows(seat_key)
        else:
            keys = _snapshot_seat_keys()
            legacy_marker = False
            for marker in markers.values():
                key = marker.get("seat_key") if isinstance(marker, dict) else None
                if key:
                    keys.add(str(key))
                else:
                    legacy_marker = True
            if legacy_marker:
                directory = _st().state_root() / "sessions"
                keys.update(path.stem for path in directory.glob("*.jsonl")
                            if path.is_file() and not path.is_symlink())
            rows = [entry for key in sorted(keys) for entry in _transition_rows(key)]
        marker_relations = []
        marker_reader = getattr(pm, "steward_evidence_targets", None)
        for (marker_harness, marker_sid), marker in markers.items():
            if marker_reader is None:
                continue
            evidence = marker_reader(marker)
            if not evidence:
                continue
            relation = _marker_relation(marker_harness, marker_sid, marker, evidence, seat_key)
            if not relation.get("seat_key"):
                # Legacy markers lack a seat. Infer one only from matching
                # original evidence, never from a generation-only last winner.
                keys = {saved.get("seat_key") or key for key, row in rows
                        for saved in (row.get("steward") or {}).get("roles") or ()
                        if isinstance(saved, dict) and all(saved.get(field) == relation.get(field)
                            for field in ("generation", "origin", "marker_digest", "targets"))}
                if len(keys) == 1:
                    relation["seat_key"] = keys.pop()
            marker_relations.append(relation)
        states, releases = _role_states(rows=rows, marker_relations=marker_relations)
    except Exception:  # noqa: BLE001 - unknown authority is never a new role
        return []
    out = []
    for identity, saved in sorted(states.items()):
        relation = dict(saved)
        relation["released"] = relation.get("released") is True or identity in releases
        holder = relation.get("holder") or {}
        if ((harness is not None and holder.get("harness") != harness)
                or (sid is not None and holder.get("sid") != sid)
                or (seat_key is not None and relation.get("seat_key") not in {None, seat_key})
                or (relation["released"] and not include_released)):
            continue
        out.append(relation)
    return out


def _watch_states(seat_key: Optional[str] = None) -> dict[str, tuple[str, dict]]:
    states = {}
    for key, row in _transition_rows(seat_key):
        steward = row.get("steward") if isinstance(row.get("steward"), dict) else {}
        for watch in steward.get("watches") or ():
            if isinstance(watch, dict) and watch.get("watch_id"):
                value = dict(watch)
                value.setdefault("seat_key", key)
                states[str(watch["watch_id"])] = (key, value)
    return states


def _watch_roots(pm) -> list[Path]:
    try:
        roots, _source = pm.steward_marker_roots()
    except Exception:
        roots = []
    own = pm.peer_state_root()
    return list(dict.fromkeys([Path(own), *(Path(root) for root in roots)]))


def watch_ack_valid(path: Path, watch_id: str, harness: str, sid: str) -> bool:
    """An ACK settles only when its readable record names this exact recipient."""
    data = _st().read_json(path)
    if not isinstance(data, dict):
        return False
    return (data.get("watch_id") in {None, watch_id}
            and data.get("session_id") == sid
            and data.get("harness") in {None, harness})


def _write_fsynced_temp(path: Path, payload: bytes) -> Path:
    """Write a complete sibling file and fsync it before any canonical publication."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = None
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short staged state write")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = None
    except BaseException:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return tmp


def _ack_inode(path: Path, watch_id: str, harness: str, sid: str):
    """Read one regular ACK inode without following links; classify torn vs stale data."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", None
    identity = (before.st_dev, before.st_ino)
    if not stat.S_ISREG(before.st_mode):
        return "unsafe", identity
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unsafe", identity
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != identity or not stat.S_ISREG(opened.st_mode):
            return "unsafe", identity
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "torn", identity
    if not isinstance(data, dict):
        return "torn", identity
    if (data.get("watch_id") in {None, watch_id}
            and data.get("session_id") == sid
            and data.get("harness") in {None, harness}):
        return "current", identity
    return "other", identity


def _unlink_torn_ack(path: Path, identity) -> bool:
    """Remove only the same unreadable regular inode observed under the seat lock."""
    if identity is None:
        return False
    try:
        current = path.lstat()
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != identity):
            return False
        os.unlink(path)
        return True
    except OSError:
        return False


def resolved_watch_references(harness: Optional[str] = None, sid: Optional[str] = None, *,
                              seat_key: Optional[str] = None) -> list[dict]:
    """Return exact, unresolved watch duties and their current confirmed recipient."""
    try:
        pm = _peer_message()
        states = _watch_states(seat_key)
        roots = _watch_roots(pm)
    except Exception:  # noqa: BLE001
        return []
    arms: dict[str, dict] = {}
    for root in roots:
        watch_root = root / "peer-watches"
        try:
            paths = sorted(watch_root.glob("*.json"))
        except OSError:
            continue
        for path in paths:
            if path.name.endswith((".receipt.json", ".ack.json", ".observation.json")) or path.is_symlink():
                continue
            arm = _st().read_json(path)
            if not isinstance(arm, dict) or not arm.get("watch_id"):
                continue
            watch_id = str(arm["watch_id"])
            ack = watch_root / f"{watch_id}.ack.json"
            original = arm.get("steward") or {}
            seat = arm.get("steward_seat") or {}
            value = {
                "watch_id": watch_id,
                "origin": {"harness": original.get("harness"), "sid": original.get("session_id")},
                "holder": {"harness": original.get("harness"), "sid": original.get("session_id")},
                "seat_key": seat.get("key"),
                "storage_root": str(root),
                "server": arm.get("server") or "default",
                "target": arm.get("target"),
                "agent": dict(arm.get("agent") or {}),
                "until": list(arm.get("until") or []),
                "armed_ts": arm.get("armed_ts"),
                "deadline": arm.get("deadline"),
            }
            previous = states.get(watch_id)
            ack_holder = ((previous[1].get("holder") or {}) if previous else value["holder"])
            if watch_ack_valid(ack, watch_id, ack_holder.get("harness"), ack_holder.get("sid")):
                continue
            states.setdefault(watch_id, (str(value.get("seat_key") or ""), value))
            arms[watch_id] = value
    out = []
    for watch_id, (key, saved) in states.items():
        value = dict(saved)
        if not value.get("seat_key") and key:
            value["seat_key"] = key
        root = Path(str(value.get("storage_root") or pm.peer_state_root()))
        arm = arms.get(watch_id)
        if arm is not None:
            identity = {
                field: arm.get(field) for field in
                ("storage_root", "server", "target", "agent", "until", "armed_ts",
                 "deadline", "origin", "seat_key")
            }
            if any(value.get(field) != expected for field, expected in identity.items()):
                continue
        holder = value.get("holder") or {}
        if watch_ack_valid(root / "peer-watches" / f"{watch_id}.ack.json", watch_id,
                           holder.get("harness"), holder.get("sid")):
            continue
        if ((harness is not None and holder.get("harness") != harness)
                or (sid is not None and holder.get("sid") != sid)
                or (seat_key is not None and value.get("seat_key") != seat_key)):
            continue
        out.append(value)
    return out


def _watch_reference_from_arm(watch_id: str, root: Path, arm: dict) -> dict:
    steward = arm.get("steward") or {}
    seat = arm.get("steward_seat") or {}
    return {
        "watch_id": watch_id,
        "origin": {"harness": steward.get("harness"), "sid": steward.get("session_id")},
        "holder": {"harness": steward.get("harness"), "sid": steward.get("session_id")},
        "seat_key": seat.get("key"), "storage_root": str(root),
        "server": arm.get("server") or "default", "target": arm.get("target"),
        "agent": dict(arm.get("agent") or {}), "until": list(arm.get("until") or []),
        "armed_ts": arm.get("armed_ts"), "deadline": arm.get("deadline"),
    }


def resolve_watch(watch_id: str, *, seat_key: Optional[str] = None,
                  include_acked: bool = False) -> Optional[dict]:
    """Resolve one duty by its exact arm and seat ledger without scanning all watches."""
    try:
        pm = _peer_message()
        primary = Path(pm.peer_state_root())
        roots = [primary]
        primary_path = primary / "peer-watches" / f"{watch_id}.json"
        if not primary_path.is_file():
            roots.extend(Path(root) for root in _watch_roots(pm) if Path(root) != primary)
        for root in roots:
            watch_root = root / "peer-watches"
            arm_path = watch_root / f"{watch_id}.json"
            arm = _st().read_json(arm_path)
            if isinstance(arm, dict) and arm.get("watch_id") == watch_id:
                value = _watch_reference_from_arm(watch_id, root, arm)
                key = value.get("seat_key")
                if seat_key is not None and key != seat_key:
                    continue
                if key:
                    saved = _watch_states(key).get(watch_id)
                    if saved:
                        row_key, relation = saved
                        if row_key != key or any(relation.get(field) != value.get(field) for field in
                                ("origin", "seat_key", "storage_root", "server", "target", "agent",
                                 "until", "armed_ts", "deadline")):
                            return None
                        value = dict(relation)
                holder = value.get("holder") or {}
                acked = watch_ack_valid(watch_root / f"{watch_id}.ack.json", watch_id,
                                        holder.get("harness"), holder.get("sid"))
                if acked and not include_acked:
                    return None
                return value
        return None
    except Exception:  # noqa: BLE001 - unreadable lineage remains pending, not retargeted
        return None


def watch_delivery_binding(watch_ids: Iterable[str], *, include_acked: bool = False) -> Optional[dict]:
    """Snapshot the current holders and confirmed handover lineages for exact watches."""
    ids = list(watch_ids)
    if not ids or any(not isinstance(watch_id, str) or not watch_id for watch_id in ids):
        return None
    watches = []
    recipient = None
    for watch_id in ids:
        relation = resolve_watch(watch_id, include_acked=include_acked)
        if not isinstance(relation, dict) or relation.get("watch_id") != watch_id:
            return None
        holder = relation.get("holder") or {}
        current = {"harness": holder.get("harness"), "session_id": holder.get("sid")}
        seat_key = relation.get("seat_key")
        lineage = relation.get("lineage") or []
        if (current["harness"] not in {"claude", "codex", "opencode"}
                or not isinstance(current["session_id"], str) or not current["session_id"]
                or not isinstance(seat_key, str) or not seat_key
                or not isinstance(lineage, list)
                or any(not isinstance(item, dict) for item in lineage)):
            return None
        if recipient is not None and recipient != current:
            return None
        recipient = current
        watches.append({
            "watch_id": watch_id,
            "seat_key": seat_key,
            "holder": current,
            # The writer appends each confirmed A -> B transition here. Keep
            # that lineage as the generation; do not infer a second owner.
            "handover_generation": list(lineage),
        })
    return {"recipient": recipient, "watches": watches}


@contextlib.contextmanager
def watch_delivery_binding_lock(binding: dict):
    """Hold the existing seat writer locks while rechecking a watch binding."""
    watches = binding.get("watches") if isinstance(binding, dict) else None
    if not isinstance(watches, list) or not watches:
        yield False
        return
    ids, keys = [], set()
    for watch in watches:
        if not isinstance(watch, dict):
            yield False
            return
        watch_id, key = watch.get("watch_id"), watch.get("seat_key")
        if (not isinstance(watch_id, str) or not watch_id
                or not isinstance(key, str) or not key):
            yield False
            return
        ids.append(watch_id)
        keys.add(key)
    stack = contextlib.ExitStack()
    try:
        import session_tidy
        for key in sorted(keys):
            stack.enter_context(session_tidy.seat_lock(key))
        current = watch_delivery_binding(ids, include_acked=True)
    except (OSError, ValueError, RuntimeError):
        stack.close()
        yield False
        return
    try:
        yield current == binding
    finally:
        stack.close()


def watch_acknowledgements_valid(binding: dict) -> bool:
    """Confirm every exact watch in a locked current binding has a durable ACK."""
    watches = binding.get("watches") if isinstance(binding, dict) else None
    recipient = binding.get("recipient") if isinstance(binding, dict) else None
    if not isinstance(watches, list) or not watches or not isinstance(recipient, dict):
        return False
    for watch in watches:
        if not isinstance(watch, dict):
            return False
        watch_id, seat_key = watch.get("watch_id"), watch.get("seat_key")
        if (not isinstance(watch_id, str) or not watch_id
                or not isinstance(seat_key, str) or not seat_key):
            return False
        relation = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
        if not isinstance(relation, dict) or relation.get("watch_id") != watch_id:
            return False
        holder = relation.get("holder") or {}
        if (holder.get("harness") != recipient.get("harness")
                or holder.get("sid") != recipient.get("session_id")):
            return False
        root = Path(str(relation.get("storage_root") or ""))
        if (not root.is_absolute()
                or not watch_ack_valid(root / "peer-watches" / f"{watch_id}.ack.json",
                                       watch_id, recipient.get("harness"),
                                       recipient.get("session_id"))):
            return False
    return True


def acknowledge_watch(watch_id: str, harness: str, sid: str, carrier: str) -> str:
    """Create the original watch ACK only for its revalidated current recipient."""
    try:
        row = resolve_watch(watch_id, include_acked=True)
        if row is None:
            return "unknown"
        holder = row.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            return "not-recipient"
        root = Path(str(row.get("storage_root") or ""))
        if not root.is_absolute():
            return "unknown"
        path = root / "peer-watches" / f"{watch_id}.ack.json"
        seat_key = row.get("seat_key")
        lock = _st().seat_lock(str(seat_key)) if seat_key else contextlib.nullcontext()
        with lock:
            if root.is_symlink() or path.parent.is_symlink():
                return "unknown"
            identity_fields = ("seat_key", "storage_root", "server", "target", "agent",
                               "until", "armed_ts", "deadline", "origin")
            for _attempt in range(4):
                current = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
                if not current:
                    return "unknown"
                current_holder = current.get("holder") or {}
                if current_holder != holder:
                    return "not-recipient"
                if any(current.get(field) != row.get(field) for field in identity_fields):
                    return "unknown"

                status, inode = _ack_inode(path, watch_id, harness, sid)
                if status == "current":
                    return "already"
                if status == "torn":
                    # Re-resolve immediately before removing the exact corrupt inode. A
                    # stale predecessor cannot clear a successor's competing ACK.
                    current = resolve_watch(watch_id, seat_key=seat_key, include_acked=True)
                    if (not current or (current.get("holder") or {}) != holder
                            or any(current.get(field) != row.get(field)
                                   for field in identity_fields)):
                        return "not-recipient" if current else "unknown"
                    if not _unlink_torn_ack(path, inode):
                        continue
                    continue
                if status != "missing":
                    return "unknown"

                payload = (json.dumps({"watch_id": watch_id, "carrier": str(carrier)[:80],
                                      "session_id": sid, "harness": harness,
                                      "ts": _st().now_epoch()}, sort_keys=True) + "\n").encode("utf-8")
                tmp = _write_fsynced_temp(path, payload)
                try:
                    try:
                        # A hard link claims the canonical name atomically without
                        # replacing a competing valid ACK.
                        os.link(tmp, path, follow_symlinks=False)
                    except FileExistsError:
                        continue
                    return "created"
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
            return "unknown"
    except Exception:  # noqa: BLE001 - uncertainty does not consume the watch
        return "unknown"


def collect_snapshot_steward(seat, harness: str, sid: str) -> Optional[dict]:
    roles = resolved_steward_relations(harness, sid, seat_key=seat.key)
    watches = resolved_watch_references(harness, sid, seat_key=seat.key)
    if not roles and not watches:
        return None
    return {"schema": 1, "roles": roles, "watches": watches}


def _revalidate_snapshot_steward(seat, harness: str, sid: str, payload: dict) -> Optional[dict]:
    """Recheck collected role/watch identities inside the seat writer transaction."""
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise RuntimeError("snapshot-steward-payload-invalid")
    current_roles = resolved_steward_relations(seat_key=seat.key, include_released=True)
    roles = []
    for candidate in payload.get("roles") or ():
        if not isinstance(candidate, dict) or not candidate.get("generation"):
            raise RuntimeError("snapshot-steward-role-invalid")
        matches = [row for row in current_roles if _role_identity(row) == _role_identity(candidate)]
        if len(matches) != 1:
            raise RuntimeError("snapshot-steward-role-unresolved")
        current = matches[0]
        if current.get("released") is True:
            continue
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            raise RuntimeError("snapshot-steward-holder-changed")
        roles.append(current)

    current_watches = {str(row.get("watch_id")): row for row in
                       resolved_watch_references(seat_key=seat.key)}
    watches = []
    for candidate in payload.get("watches") or ():
        if not isinstance(candidate, dict) or not candidate.get("watch_id"):
            raise RuntimeError("snapshot-watch-invalid")
        watch_id = str(candidate["watch_id"])
        current = current_watches.get(watch_id)
        if current is None:
            root = Path(str(candidate.get("storage_root") or ""))
            holder = candidate.get("holder") or {}
            if (root.is_absolute() and watch_ack_valid(
                    root / "peer-watches" / f"{watch_id}.ack.json", watch_id,
                    holder.get("harness"), holder.get("sid"))):
                continue
            raise RuntimeError("snapshot-watch-unresolved")
        if any(candidate.get(field) != current.get(field) for field in
               ("seat_key", "storage_root", "server", "target", "agent", "until", "deadline", "origin")):
            raise RuntimeError("snapshot-watch-identity-changed")
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != sid:
            raise RuntimeError("snapshot-watch-holder-changed")
        watches.append(current)
    if not roles and not watches:
        return None
    return {"schema": 1, "roles": roles, "watches": watches}


def _append_transition_row(seat, row: dict) -> None:
    """Atomically publish one fsynced transition before any predecessor is retired.

    Rewriting the bounded seat ledger from its complete newline-terminated prefix also
    discards a torn tail left by an older append writer. A failed staged write/fsync never
    changes the canonical ledger, so a retry still sees the predecessor as holder.
    """
    st = _st()
    path = st._ledger_path(seat)
    st.ensure_dir(path.parent)
    st._reject_symlink(path)
    payload = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    current = st.read_bytes(path)
    if current is None:
        if path.exists() or path.is_symlink():
            raise OSError("unable to read seat ledger before transition")
        current = b""
    complete_end = current.rfind(b"\n") + 1
    replacement = current[:complete_end] + payload
    tmp = _write_fsynced_temp(path, replacement)
    try:
        st._reject_symlink(path)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    # Folding is compaction after the durable transition boundary. If it fails, the
    # canonical row is already committed and must not be reported as an uncommitted hop.
    with contextlib.suppress(Exception):
        st._fold_ledger(seat)


def _bump_dispatch_edge_generation(jobs=None) -> None:
    """A confirmed edge change invalidates derived recipient inboxes.

    Only discovery reads the generation, and a missed bump costs one periodic
    runner reconciliation; it never grants or revokes authority, which every
    record re-reads. The canonical ledger row is already durable here, so a
    failure to record this derived observation never fails the writer.
    """
    try:
        import dispatch_recipient_inbox as inbox
        inbox.bump_edge_generation(inbox.dispatch_inbox_root(jobs=jobs))
    except Exception:  # noqa: BLE001 - derived observation only
        return


def record_steward_release(seat, harness: str, sid: str, generation: str, now=None, *, relation=None) -> bool:
    """Revoke the unique actor-bound canonical relation under the caller's seat lock."""
    if not generation:
        return False
    current = [row for row in resolved_steward_relations(seat_key=seat.key, include_released=True)
               if str(row.get("generation")) == str(generation) and _role_actor(row, harness, sid)]
    if relation is not None:
        if (len(current) != 1 or not (
                _role_identity(current[0]) == _role_identity(relation)
                or _role_follows(relation, current[0]))):
            return False
    elif len(current) > 1:
        return False
    if current and current[0].get("released"):
        return True
    # Preserve the legacy physical-only writer. A bare old release is not
    # idempotence evidence for a different relation that later reuses its label.
    if not current and relation is not None:
        return False
    if not current and any(row.get("event") == "steward-release"
                           and str(row.get("generation")) == str(generation)
                           and row.get("seat_key", key) == seat.key
                           and (row.get("harness"), row.get("sid")) == (harness, sid)
                           for key, row in _transition_rows(seat.key)):
        return True
    _append_transition_row(seat, {
        "ts": _st().now_epoch() if now is None else now,
        "harness": harness, "sid": sid, "event": "steward-release",
        "seat_key": seat.key, "generation": generation,
    })
    return True


def _seat_of(key: str, snapshot: Optional[dict] = None):
    st = _st()
    data = (snapshot or {}).get("seat") if snapshot else None
    if isinstance(data, dict) and data.get("key") == key:
        return st.seat_from_dict(data)
    return st.Seat("pane", key)


def pane_seat(env=None, harness: Optional[str] = None, sid: Optional[str] = None):
    """The proven pane or exact native seat; project seats never hand over."""
    st = _st()
    env = os.environ if env is None else env
    detected, own_sid = st.session_from_env(harness, env)
    seat = st.resolve_seat(harness or detected, env=env, sid=sid or own_sid, project=False)
    return seat if seat.kind in ("pane", "native") else None


@contextlib.contextmanager
def readiness_transition_lock(pane: str, server: str, socket_path: Optional[str] = None,
                             *, wait: Optional[float] = None):
    """Use the existing pane-seat lock while a watch checks and publishes readiness.

    Handover and tidy-claim writers for this pane already take this lock. The
    watch lock is per-watch and does not coordinate those writers.
    """
    if not isinstance(pane, str) or not pane or pane == "-":
        raise ValueError("readiness-pane-identity-invalid")
    st = _st()
    env = {}
    if isinstance(server, str) and server and server != "default":
        env["AGENT_HERDR_SESSION"] = server
    if isinstance(socket_path, str) and socket_path:
        env["HERDR_SOCKET_PATH"] = socket_path
    endpoint = st._endpoint_scope(env)
    seat_key = (st._digest("pane", endpoint, pane) if endpoint
                else st._digest("pane", pane))
    with st.seat_lock(seat_key, wait=wait):
        yield


# ---------------------------------------------------------------------------
# Bindings: which registered attempts a session answers for
# ---------------------------------------------------------------------------

def binding_of(jobs, meta: dict) -> dict:
    """``parent`` is the registered parent, the key its pending records stay stored under."""
    route, digest, node = route_identity(meta)
    return {"jobs": _jobs_key(jobs), "attempt": meta["attempt_id"], "route": route, "hash": digest, "node": node,
            "parent": str(meta.get("parent_sid") or "")}


def _binds(bindings: Iterable[dict], jobs, meta: dict) -> bool:
    """A binding is exact on registry file, route id, route hash and node.  A replacement
    attempt of the same logical node shares all four, so the lineage follows it; the attempt
    id in the binding is the audit record, not a key."""
    key = _jobs_key(jobs)
    route, digest, node = route_identity(meta)
    return any(b.get("jobs") == key and b.get("route") == route and b.get("hash") == digest
               and b.get("node") == node for b in bindings if isinstance(b, dict))


class RecipientResolution(NamedTuple):
    session_id: str
    harness: str
    state: str
    registered_session: str
    registered_harness: str
    reason: str = ""


def recipient_continuation(result, actor=None):
    from route_authority import ContinuationDecision, lineage_unreadable_wait
    if result.state == "unreadable":
        return lineage_unreadable_wait({"parent_sid": result.registered_session}, cause=result.reason)
    if actor is not None and result.session_id != actor:
        return ContinuationDecision(False, "protected", "work-parent-recovery-required", "report")
    return ContinuationDecision(True, "continue")



def _lineage_index(read_context=None):
    """Compatibility rows projection; graph health owns the observation."""
    try:
        return _lineage_seats(read_context=read_context), ""
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return {}, "handover-unreadable:" + str(exc)


def resolve_recipient(question, *, metadata=None, jobs=None, session="", harness="", read_context=None):
    """Answer each recipient question from the common qualified observation."""
    metadata = metadata or {}
    notice = question in {"provenance", "attempt-parent", "owner-session"}
    registered = str((metadata.get("session_id") if notice else metadata.get("parent_sid")) or session or "")
    original_harness = str((metadata.get("harness") if notice else _registered_parent_harness(metadata)) or harness or "")
    try:
        if notice and question != "provenance":
            from dispatch_completion_join import current_attempt_row, read_supervisor_phase_state
            from dispatch_contract import dispatch_state_root
            from harness_capabilities import parent_completion
            row = current_attempt_row(jobs, metadata["attempt_id"], read_context=read_context) if jobs is not None else None
            if row is None or row.status not in {"open", "running", "done"}:
                raise ValueError("notice-attempt-unavailable")
            if question == "owner-session":
                if row.metadata.get("harness") != original_harness:
                    raise ValueError("notice-consumer-harness-mismatch")
                state = read_supervisor_phase_state(dispatch_state_root(jobs) / "supervisor-state" /
                    (metadata["attempt_id"] + ".json"), metadata["attempt_id"])
                if state is None or not isinstance(state.resource, dict) or state.resource.get("session_id") != registered:
                    raise ValueError("notice-consumer-unavailable")
                return RecipientResolution(registered, original_harness, "no-handover", registered, original_harness)
            if (row.metadata.get("parent_sid") != registered
                    or (_registered_parent_harness(row.metadata) or original_harness) != original_harness
                    or row.metadata.get("parent_completion_delivery") != parent_completion(original_harness).get("carrier")):
                raise ValueError("notice-parent-mismatch")
            return resolve_recipient("job-holder", metadata=row.metadata, jobs=jobs,
                session=registered, harness=original_harness, read_context=read_context)
        graph = _pass_lineage_graph(read_context)
        if question in {"seat-successor", "provenance"}:
            current, current_harness, disposition = _seat_end(graph, original_harness, registered)
        elif question == "job-holder":
            if not registered or jobs is None or not eligible_row(metadata):
                current, current_harness, disposition = registered, original_harness, "self"
            else:
                jobs_key = read_context.jobs_key(jobs) if read_context is not None else _jobs_key(jobs)
                current, current_harness, disposition = _lineage_holder(graph,
                    (jobs_key, *route_identity(metadata)), registered, original_harness)
        else:
            raise ValueError("unknown-recipient-question")
        if disposition == "unresolved":
            raise ValueError("handover-lineage-unresolved")
        return RecipientResolution(current, current_harness,
            "confirmed" if (current, current_harness) != (registered, original_harness) else "no-handover",
            registered, original_harness)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return RecipientResolution("", "", "unreadable", registered, original_harness,
                                   "handover-unreadable:" + str(exc))


def resolve_attempt_recipient(jobs, attempt, registered, harness="", *, read_context=None):
    """Missing exact rows still consult ledger bindings; damaged input waits."""
    from dispatch_registry_cache import registry_snapshot
    try:
        try:
            snapshot = (read_context.registry_snapshot(Path(jobs))
                        if read_context is not None else registry_snapshot(Path(jobs)))
        except FileNotFoundError:
            snapshot = None
        if snapshot is not None and not snapshot.complete:
            raise ValueError(snapshot.reason)
        row = snapshot.view.handover_attempt(attempt) if snapshot is not None else None
        if row is not None:
            if row[1].get("parent_sid") != registered:
                raise ValueError("registered-parent-mismatch")
            return resolve_recipient("job-holder", metadata=row[1], jobs=jobs,
                                     session=registered, harness=harness, read_context=read_context)
        graph = _pass_lineage_graph(read_context)
        bindings = set()
        legacy_membership_unknown = False
        for rows in graph.ledger_rows.values():
            for row in rows:
                origin = _st()._handover_origin(row)
                for binding in row.get("bindings") or ():
                    parent = str(binding.get("parent") or (origin[1] if origin else ""))
                    if (binding.get("jobs") == _jobs_key(jobs) and parent == registered
                            and not binding.get("attempt")):
                        legacy_membership_unknown = True
                    if (binding.get("jobs") == _jobs_key(jobs) and binding.get("attempt") == attempt
                            and parent == registered):
                        bindings.add((_logical_key(binding), parent))
        owners = {_lineage_holder(graph, key, parent, harness) for key, parent in bindings}
        if len(owners) > 1 or any(state == "unresolved" for _sid, _harness, state in owners):
            raise ValueError("handover-conflicting-attempt")
        if owners:
            current, current_harness, _state = next(iter(owners))
        else:
            # With no binding this is still a job question, not seat succession.
            if legacy_membership_unknown or _chain_end(graph, harness, registered)[1] != "self":
                raise ValueError("missing-attempt-binding")
            current, current_harness = registered, harness
        return RecipientResolution(current, current_harness,
            "confirmed" if (current, current_harness) != (registered, harness) else "no-handover", registered, harness)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        return RecipientResolution("", "", "unreadable", registered, harness,
                                   "handover-unreadable:" + str(exc))


def _lineage_seats(extra=(), *, read_context=None) -> dict:
    """Compatibility projection over the single normalized graph snapshot."""
    graph = _pass_lineage_graph(read_context)
    return {key: [dict(row) for row in rows if row.get("event") in {"handover", "succession"}]
            for key, rows in graph.ledger_rows.items()}


def _lineage_successor(rows_by_seat: dict, sid: str, harness: str) -> str:
    """Legacy private wrapper; unresolved is explicitly the original SID."""
    end, state = chain_end(harness, sid)
    return sid if state == "unresolved" else end


def _lineage_holder(graph: LineageGraph, key: tuple, registered: str, harness: str, *,
                    traversed=None) -> tuple[str, str, str]:
    """Who answers for one logical job (jobs, route, hash, node) now.

    The latest row that binds the job names its holder (a later hand-off wins).  From there the
    job follows the holder's confirmed same-harness graph even without another
    binding; a cross-pane ``retire`` row moves it only when it binds it. The
    queried session therefore answers only while it is the end of that chain.
    Keep the selected holder's disposition: an unresolved explicit handoff
    cannot restore the registered parent or authorize the ambiguous holder."""
    if key in graph.binding_conflicts:
        return registered, harness, "unresolved"
    latest = graph.binding_rows.get(key)
    holder = registered
    if latest is not None:
        row = latest
        holder = str(row.get("sid") or registered)
        harness = str(row.get("harness") or harness)
    # Explicit bindings are authority independent of the row's source (in
    # particular, legacy schema-1 and cross-harness retire handovers). Only
    # the shared source-qualified graph can advance that exact holder without
    # another binding. A later legacy binding for a different job is not an
    # unbound edge, nor can chronological rows bypass a fork.
    end, state = _chain_end(graph, harness, holder, traversed=traversed)
    return end, harness, state


def session_successor(session: str, *, harness=None, read_context=None) -> tuple[str, str]:
    result = resolve_recipient("seat-successor", session=session, harness=harness or "", read_context=read_context)
    if result.state == "unreadable":
        raise OSError(result.reason)
    return (result.session_id, result.harness) if result.state == "confirmed" else ("", "")

def _effective(meta: dict, jobs=None, *, read_context=None, extra_seats=()) -> tuple[str, str]:
    result = resolve_recipient("job-holder", metadata=meta, jobs=jobs, read_context=read_context)
    return result.session_id, result.harness

def _registered_parent_harness(meta: dict) -> str:
    """Recover the parent harness from real depth-1 registry rows.

    Current rows commonly omit ``parent_harness`` while retaining the runtime
    harness and completion carrier. Keep the explicit value authoritative, then
    use the carrier table as the stable fallback.
    """
    explicit = str(meta.get("parent_harness") or meta.get("main_runtime_harness") or "")
    if explicit:
        return explicit
    carrier = meta.get("parent_completion_delivery")
    if not isinstance(carrier, str) or not carrier:
        return ""
    try:
        from harness_capabilities import HARNESSES, parent_completion
        return next((harness for harness in HARNESSES
                     if parent_completion(harness).get("carrier") == carrier), "")
    except (ImportError, KeyError, TypeError, ValueError):
        return ""


def _lineage_identity_key(binding: dict) -> tuple[str, str, str, str]:
    return (str(binding.get("jobs") or ""), str(binding.get("route") or ""),
            str(binding.get("hash") or ""), str(binding.get("node") or ""))


@contextlib.contextmanager
def binding_mutexes(bindings: Iterable[dict], *, wait: Optional[float] = None):
    """Lock exact logical bindings using the existing seat-lock backend.

    The key exists before a snapshot does; it is synchronization only. Writers
    enter while holding their seat transaction, so effect readers must not take
    a seat lock here (seat -> binding is the sole nested order).
    """
    keys = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        identity = _lineage_identity_key(binding)
        if not identity[0] or not identity[1]:
            continue
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        keys.add("lineage-binding-" + digest)
    with contextlib.ExitStack() as stack:
        for key in sorted(keys):
            stack.enter_context(_st().seat_lock(key, wait=wait))
        yield


@contextlib.contextmanager
def lineage_effect_lock(meta: dict, jobs, expected_parent: str, *, wait: float = 0.05):
    """Serialize an effect with every writer of this binding, including its first one."""
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(binding_mutexes([{"jobs": _jobs_key(jobs), "route": route_identity(meta)[0],
            "hash": route_identity(meta)[1], "node": route_identity(meta)[2]}], wait=wait))
        # No pass cache crosses the effect boundary.
        current = resolve_recipient("job-holder", metadata=meta, jobs=jobs)
        allowed = recipient_continuation(current, expected_parent).allowed
    except (OSError, ValueError, TypeError, RuntimeError):
        stack.close()
        yield False
        return
    try:
        yield allowed
    finally:
        stack.close()

def effective_parent(meta: dict, jobs=None, *, read_context=None) -> str:
    """The session that answers for ``meta``'s attempt now: the registered ``parent_sid``, or
    the end of its handover lineage. Unresolved or unavailable lineage has no authority."""
    return _effective(meta, jobs, read_context=read_context)[0]


def effective_parent_harness(meta: dict, jobs=None, *, read_context=None) -> str:
    """The harness of :func:`effective_parent` (the registered ``parent_harness`` without a handover)."""
    return _effective(meta, jobs, read_context=read_context)[1]


def record_recipient(record: dict, storage_key: str, *, harness: str,
                     jobs=None, read_context=None) -> Optional[str]:
    """SID-only compatibility projection of the common current recipient binding."""
    binding = record_recipient_binding(record, storage_key, harness=harness,
                                       jobs=jobs, read_context=read_context)
    return binding[1] if binding is not None else None


def _notice_recipient_binding(lineage, jobs, source_harness, *, read_context=None):
    """Compatibility pair projection of the question-specific common answer."""
    result = resolve_recipient(lineage.get("kind"), metadata=lineage, jobs=jobs,
        session=lineage.get("session_id", ""), harness=source_harness or "", read_context=read_context)
    return (result.harness, result.session_id) if result.state != "unreadable" and result.session_id else None

def record_recipient_binding(record: dict, storage_key: str, *, harness: str,
                             jobs=None, read_context=None) -> Optional[tuple[str, str]]:
    """Validate storage identity, then project every member's common answer."""
    if not isinstance(record, dict) or not isinstance(storage_key, str) or not storage_key:
        return None
    receipt = record.get("receipt")
    if "receipt" in record and not isinstance(receipt, dict):
        return None
    if isinstance(receipt, dict) and receipt.get("kind") == "notice":
        try:
            import session_notice
            from harness_capabilities import HARNESSES, parent_completion
            source_harness = next((name for name in HARNESSES
                if parent_completion(name).get("carrier") == record.get("recipient_kind")), None)
            jobs, lineage = session_notice.pending_lineage(record, source_harness, storage_key, jobs)
            return _notice_recipient_binding(lineage, jobs, source_harness, read_context=read_context)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            return None
    ids = record.get("attempt_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids):
        return None
    jobs = Path(jobs) if jobs is not None else _default_jobs()
    if jobs is None:
        return None
    from peer_read_context import PeerReadContext
    context = read_context or PeerReadContext()
    answers = [resolve_attempt_recipient(jobs, attempt, storage_key, harness, read_context=context) for attempt in ids]
    if any(answer.state == "unreadable" for answer in answers):
        return None
    targets = {(answer.harness, answer.session_id) for answer in answers}
    return next(iter(targets)) if len(targets) == 1 else None

_OWN_SEATS: dict = {}


def _own_seat(session: str):
    """The caller's own seat, only when ``session`` is the caller; resolved once per process.

    Another session's seats come from handover snapshots, so a check over many registry rows
    never probes panes for sessions that cannot be proven from here."""
    try:
        if _st().session_from_env(None, os.environ)[1] != session:
            return None
        key = (session, os.environ.get("HERDR_PANE_ID", ""), os.environ.get("HERDR_SOCKET_PATH", ""))
        if key not in _OWN_SEATS:
            _OWN_SEATS[key] = pane_seat(None, None, session)
        return _OWN_SEATS[key]
    except Exception:  # noqa: BLE001
        return None


def owns(meta: dict, session: str, jobs=None, *, read_context=None) -> Optional[bool]:
    """Whether this exact session is the shared reader's confirmed current holder.

    Three answers: True, False (another session holds it, or no session was named), and
    None when the lineage cannot be read. A registered parent always has a holder while the
    lineage reads (itself or its successor), so no holder for one is that unknown. Unknown
    is neither owner nor not-owner: a caller that refuses on False maps it through
    ``route_authority.require_owner`` to the recipient judgment's wait (LOOP §4).
    """
    if not session:
        return False
    holder = effective_parent(meta, jobs, read_context=read_context)
    if holder:
        return holder == session
    return None if meta.get("parent_sid") else False


class RecipientScopes(list):
    """``(storage_key, attempts)`` scopes plus whether enumeration finished.

    Existing consumers unpack the list exactly as before. ``complete=False``
    marks a partial enumeration: it is never published as a current inbox, and
    the caller keeps the previous inbox plus the session's own scope instead.
    """

    __slots__ = ("complete", "edges")

    def __init__(self, items=(), *, complete: bool = True, edges=None):
        super().__init__(items)
        self.complete = complete
        # The confirmed edges of the lineage observation these scopes came
        # from, or None when no graph was read. A published inbox digests this
        # set, never a graph observed later than the scopes.
        self.edges = edges


def storage_recipients(session: str, env=None, harness: Optional[str] = None, *,
                       read_context=None, deadline_ns: Optional[int] = None) -> RecipientScopes:
    """Original storage scopes for the current qualified recipient; never move rows.

    ``deadline_ns`` is a caller-side monotonic bound (the hook effect path never
    calls this; the inbox prefill and discovery fallback do). Past it the
    enumeration stops and returns the scopes gathered so far with
    ``complete=False``; the graph read itself is not interrupted mid-file.
    """
    from peer_read_context import PeerReadContext
    context = read_context or PeerReadContext()

    def expired() -> bool:
        return deadline_ns is not None and time.monotonic_ns() >= deadline_ns

    observed = {}

    def scopes(owed, complete):
        return RecipientScopes([(session, None), *((key, LineageAttempts(attempts))
                             for key, attempts in sorted(owed.items()) if key != session)],
                             complete=complete, edges=observed.get("edges"))

    try:
        with context.pinned_lineage():
            if expired():
                return scopes({}, False)
            graph = _pass_lineage_graph(context)
            observed["edges"] = tuple(getattr(graph, "edges", ()) or ())
            vertex = (harness, session)
            if (harness and not any(target == vertex for _origin, target in graph.seat_edges)
                    and not any((name, target) == vertex for name, _origin, target in graph.edges)
                    and not any((row.get("harness"), row.get("sid")) == vertex
                                for row in graph.binding_rows.values())):
                # Activity rows alone grant no inherited scope. Every confirmed
                # successor or logical holder occurs in an edge or binding row.
                return scopes({}, True)
            candidates = set(graph.ledger_keys)
            owed = {}
            complete = True
            for rows in graph.ledger_rows.values():
                for row in rows:
                    if expired():
                        complete = False
                        break
                    origin = _st()._handover_origin(row)
                    if origin is not None:
                        candidates.add(origin)
                    for binding in row.get("bindings") or ():
                        parent = str(binding.get("parent") or (origin[1] if origin else ""))
                        meta = {"dispatch_depth": "1", "worker_type": "owner", "attempt_id": binding.get("attempt") or "candidate",
                                "parent_sid": parent, "parent_harness": origin[0] if origin else harness or "",
                                "route_id": binding["route"], "route_hash": binding["hash"], "route_node": binding["node"]}
                        answer = resolve_recipient("job-holder", metadata=meta, jobs=binding["jobs"], read_context=context)
                        if (answer.state != "unreadable" and answer.session_id == session
                                and (not harness or answer.harness == harness)):
                            owed.setdefault(parent, set())
                            if binding.get("attempt"):
                                owed[parent].add(binding["attempt"])
                if not complete:
                    break
            if complete:
                for source_harness, origin in candidates:
                    if expired():
                        complete = False
                        break
                    answer = resolve_recipient("seat-successor", session=origin, harness=source_harness, read_context=context)
                    if (answer.state == "confirmed" and answer.session_id == session
                            and (not harness or answer.harness == harness)):
                        owed.setdefault(origin, set())
        return scopes(owed, complete)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        # Unreadable authority is not an empty, complete enumeration.
        return RecipientScopes(complete=False)

def _chain_to(rows: list, session: str, *, graph=None) -> list:
    """Authority handovers plus tidy edges on the unique ancestry to ``session``.

    Binding-carrying handover rows are explicit authority regardless of source.
    Tidy succession/handover edges remain available where a row has no binding.
    """
    graph = graph if graph is not None else lineage_graph()
    candidates = []
    for ledger_key, ledger_rows in graph.ledger_rows.items():
        for index, row in enumerate(ledger_rows):
            if row.get("event") != "handover":
                continue
            edge = _st().tidy_lineage_edge(row)
            has_bindings = any(isinstance(binding, dict)
                               for binding in row.get("bindings") or ())
            if edge is None and not has_bindings:
                continue
            source = _st()._handover_origin(row)
            harness, origin, target = edge or (
                row.get("harness"), source[1] if source else None, row.get("sid"))
            if all(isinstance(value, str) and value for value in (harness, origin, target)):
                candidates.append((harness, origin, target, ledger_key, index, row))
    # Succession is a tidy edge with no handover row counterpart.
    handover_pairs = {(h, origin, target) for h, origin, target, *_ in candidates}
    for harness, origin, target in graph.edges:
        if (harness, origin, target) in handover_pairs:
            continue
        matches = []
        for ledger_key, ledger_rows in graph.ledger_rows.items():
            for index, row in enumerate(ledger_rows):
                if _st().tidy_lineage_edge(dict(row)) == (harness, origin, target):
                    matches.append((harness, origin, target, ledger_key, index, row))
        if matches:
            candidates.append(max(matches, key=lambda item: (float(item[5].get("ts", 0) or 0), item[4])))
    harnesses = {harness for harness, _origin, target, *_ in candidates if target == session}
    if not harnesses:
        harnesses = {str(row.get("harness")) for row in rows
                     if row.get("sid") == session and row.get("harness")}
    if len(harnesses) != 1:
        return []
    harness = next(iter(harnesses))
    if (harness, session) in graph.unresolved:
        return []
    reverse = {}
    for edge_harness, origin, target, ledger_key, index, row in candidates:
        if edge_harness == harness:
            reverse.setdefault(target, {}).setdefault(origin, []).append(
                (float(row.get("ts", 0) or 0), index, ledger_key, row))
    chain, cursor, seen = [], session, {session}
    if len(reverse.get(cursor, {})) > 1:
        return []
    while len(reverse.get(cursor, {})) == 1:
        origin = next(iter(reverse[cursor]))
        if ((harness, origin) in graph.unresolved
                or (harness, origin) in graph.seat_unresolved):
            break
        if origin in seen:
            return []
        matches = reverse[cursor][origin]
        # Publications on one edge can carry disjoint exact binding scopes.
        # Recency resolves a logical binding's holder in _lineage_holder, not
        # which scopes discovery may see. Keep every row on this ancestry;
        # candidate admission still rechecks each binding's current authority.
        chain.extend(dict(match[3]) for match in matches)
        seen.add(origin)
        cursor = origin
        if len(reverse.get(cursor, {})) > 1:
            return []
    return chain


def record_for_session(record: dict, allowed: Optional[frozenset], *, session=None, harness=None,
                       storage_key=None, read_context=None, jobs=None) -> bool:
    """An enumeration scope never substitutes for current recipient authority."""
    if session is not None:
        if storage_key is not None:
            pair = record_recipient_binding(record, storage_key, harness=str(harness or ""), jobs=jobs, read_context=read_context)
            return pair is not None and (pair == (harness, session) if harness else pair[1] == session)
        from peer_read_context import PeerReadContext
        context = read_context or PeerReadContext()
        ids = record.get("attempt_ids") or []
        for attempt in ids:
            row = None
            try:
                snapshot = context.registry_snapshot(Path(jobs))
                row = snapshot.view.handover_attempt(attempt) if snapshot is not None else None
                registered = row[1].get("parent_sid") if row else session
            except FileNotFoundError:
                registered = session
            except (OSError, ValueError, TypeError):
                return False
            # Missing-row inherited scopes still identify their original parent.
            if row is None and allowed is not None:
                graph = _pass_lineage_graph(context)
                parents = {str(b.get("parent") or (_st()._handover_origin(r) or ("", ""))[1])
                           for rows in graph.ledger_rows.values() for r in rows for b in r.get("bindings") or ()
                           if b.get("jobs") == _jobs_key(jobs) and b.get("attempt") == attempt}
                if len(parents) != 1:
                    return False
                registered = next(iter(parents))
            answer = resolve_attempt_recipient(jobs, attempt, registered, harness or "", read_context=context)
            if answer.state == "unreadable" or answer.session_id != session:
                return False
    if allowed is None:
        return True
    receipt = record.get("receipt") if isinstance(record, dict) else None
    receipt_kind = receipt.get("kind") if isinstance(receipt, dict) else None
    if isinstance(allowed, dict):
        if (allowed.get("lineage_notice") and
                receipt_kind == "notice" and
                not record.get("attempt_ids")):
            return True
        allowed = allowed.get("attempts")
    if (getattr(allowed, "lineage_notice", False)
            and receipt_kind == "notice"
            ):
        ids = record.get("attempt_ids")
        if isinstance(ids, list) and ids and all(
                isinstance(item, str) and item.startswith("notice-") for item in ids):
            try:
                import session_notice
                session_notice.validate_pending_record(record)
                return True
            except (ImportError, ValueError, TypeError, KeyError):
                return False
    ids = record.get("attempt_ids")
    return isinstance(ids, list) and bool(ids) and all(i in allowed for i in ids)

# ---------------------------------------------------------------------------
# Snapshot (at enqueue) and the ledger row (at the successor's confirmed start)
# ---------------------------------------------------------------------------

def _default_jobs() -> Optional[Path]:
    env = os.environ.get("AGENT_DISPATCH_JOBS")
    if env:
        return Path(env)
    try:
        from dispatch_contract import resolve_dispatch_state_root
        return resolve_dispatch_state_root(ROOT, None) / "jobs.log"
    except Exception:  # noqa: BLE001
        return None


def _delivery_open(jobs, meta: dict) -> bool:
    """A finished attempt still owes its completion when a pending record for it is open."""
    try:
        import dispatch_pending_delivery as pending
        directory = pending.record_directory(Path(jobs).resolve(strict=False).parent, meta["parent_sid"])
        for path in directory.glob("delivery-*.json"):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and meta["attempt_id"] in (value.get("attempt_ids") or ()) \
                    and value.get("state") in pending.OPEN_STATES:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _route_open(jobs, meta: dict) -> bool:
    """An acknowledged terminal attempt can still own an unfinished route."""
    route, digest, _ = route_identity(meta)
    filename = meta.get("owner_route_file") or meta.get("route_file")
    try:
        if meta.get("worker_type") == "owner" and meta.get("owner_route_file"):
            # Follow the existing verified owner generation, not an old closed anchor.
            from owner_route_binding import resolve_owner_route_lifecycle
            binding, status = resolve_owner_route_lifecycle(jobs, owner_attempt_id=meta["attempt_id"])
            if binding is None or status not in {"owner-route-launch-binding",
                    "owner-route-post-launch-attachment", "owner-route-advance-current"}:
                return False
            filename, route, digest = binding.route_file, binding.route_id, binding.route_hash
        if not filename:
            return False
        path = Path(filename)
        value = json.loads(path.read_text(encoding="utf-8"))
        return (isinstance(value, dict) and value.get("route_id") == route
                and value.get("route_hash") == digest
                and not os.path.lexists(path.with_suffix(".outcome.json")))
    except Exception:  # noqa: BLE001 - unreadable identity grants no new ownership
        return False


def _logical_key(binding: dict) -> tuple:
    """The stable logical-job key shared by all attempt and parent bindings."""
    return (_jobs_key(binding.get("jobs") or ""), str(binding.get("route") or ""),
            str(binding.get("hash") or ""), str(binding.get("node") or ""))


def _binding_identity(binding: dict) -> tuple:
    """Exact flat identity: only identical attempt/parent rows are duplicates."""
    return (*_logical_key(binding), str(binding.get("attempt") or ""), str(binding.get("parent") or ""))


def _deduplicate_bindings(bindings: Iterable[dict]) -> list[dict]:
    seen = set()
    result = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        identity = _binding_identity(binding)
        if identity not in seen:
            seen.add(identity)
            result.append(binding)
    return result


def _select_binding_rows(rows: Iterable[tuple[dict, Optional[str]]]) -> list[dict]:
    """Preserve every exact binding; logical ownership has no display limit."""
    return _deduplicate_bindings(binding for binding, _status in rows)


def _handover_binding_keys_for_session(jobs, sid: str, *, read_context=None) -> tuple[tuple[str, str, str], ...]:
    """Enumerate candidates using the same job answer as current admission."""
    from peer_read_context import PeerReadContext
    context = read_context or PeerReadContext()
    try:
        graph = _pass_lineage_graph(context)
    except (OSError, ValueError, TypeError) as exc:
        raise RegistryUnreadable(str(exc)) from exc
    keys = set()
    for key, row in graph.binding_rows.items():
        if key[0] != _jobs_key(jobs):
            continue
        origin = _st()._handover_origin(row)
        meta = {"dispatch_depth": "1", "worker_type": "owner", "attempt_id": "candidate",
                "parent_sid": origin[1] if origin else "", "parent_harness": origin[0] if origin else "",
                "route_id": key[1], "route_hash": key[2], "route_node": key[3]}
        answer = resolve_recipient("job-holder", metadata=meta, jobs=jobs, read_context=context)
        if answer.state != "unreadable" and answer.session_id == sid:
            keys.add((key[1], key[2], key[3]))
    return tuple(keys)
def _handover_candidate_rows(jobs, sid: str, *, read_context=None):
    from dispatch_registry_cache import registry_view
    view = registry_view(
        Path(jobs),
        append_only=bool(getattr(read_context, "registry_append_only", False)),
    )
    if view.malformed_rows or view.has_invalid_utf8 or view.has_partial_tail:
        raise RegistryUnreadable("registry-unreadable")
    keys = _handover_binding_keys_for_session(jobs, sid, read_context=read_context)
    return view.handover_candidates(sid, keys)


def _handover_candidates(harness: str, sid: str, jobs, rows=None, *, read_context=None) -> list[tuple[dict, str]]:
    """Collect every candidate still allowed by the existing route authority."""
    if read_context is None:
        from peer_read_context import PeerReadContext
        read_context = PeerReadContext()     # one pass reads each seat ledger once, however many candidates
    if rows is None:
        try:
            candidate_rows = ((status, record.handover_metadata())
                              for status, record in _handover_candidate_rows(
                                  jobs, sid, read_context=read_context))
        except OSError as exc:
            raise RegistryUnreadable(str(exc)) from exc
    else:
        candidate_rows = rows.values()
    found = []
    import route_authority
    for status, meta in candidate_rows:
        choice = route_authority.decide_continuation(action="handover", status=status, metadata=meta, jobs=jobs,
            facts={"actor": sid, "eligible": eligible_row(meta), "read_context": read_context})
        if choice.allowed:
            found.append((binding_of(jobs, meta), status))
    return found


def _bindings_with_current_status(bindings: Iterable[dict]) -> list[tuple[dict, Optional[str]]]:
    """Attach status only when the current registry row exactly matches a kept binding."""
    cache: dict[str, object] = {}
    result = []
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        jobs = str(binding.get("jobs") or "")
        if jobs not in cache:
            try:
                from dispatch_registry_cache import registry_view
                cache[jobs] = registry_view(Path(jobs)) if jobs else None
            except OSError:
                cache[jobs] = None
        view = cache[jobs]
        current = view.handover_attempt(str(binding.get("attempt") or "")) if view else None
        status = None
        if current:
            current_status, meta = current
            try:
                if _binding_identity(binding) == _binding_identity(binding_of(jobs, meta)):
                    status = current_status
            except (KeyError, TypeError):
                pass
        result.append((binding, status))
    return result


def current_bindings(harness: str, sid: str, jobs=None, *, read_context=None) -> list:
    """The selected depth-1 attempts ``sid`` answers for, whatever harness registered them.
    Every logical job keeps all eligible flat bindings.
    A finished (done, cancelled or killed) attempt counts while its route or completion record is
    still open; node-less owner bindings count like node bindings.
    ``harness`` is kept for callers; ownership alone decides."""
    jobs = jobs or _default_jobs()
    if jobs is None or not Path(jobs).is_file():
        return []
    return _select_binding_rows(_handover_candidates(harness, sid, jobs, read_context=read_context))


def write_snapshot_locked(seat, harness: str, sid: str, *, now: Optional[float] = None, jobs=None) -> int:
    """Take (or drop) the snapshot; return its flat binding count.  Caller holds the seat lock."""
    bindings = collect_snapshot_bindings(harness, sid, jobs=jobs)
    steward = collect_snapshot_steward(seat, harness, sid)
    return publish_snapshot_locked(seat, harness, sid, bindings, now=now, jobs=jobs,
                                   steward_payload=steward)


def collect_snapshot_bindings(harness: str, sid: str, jobs=None) -> list[dict]:
    """Read the registry outside a seat lock; publication performs the identity recheck."""
    return current_bindings(harness, sid, jobs)


def publish_snapshot_locked(seat, harness: str, sid: str, bindings: list[dict], *,
                            now: Optional[float] = None, expected_prompt_seq: Optional[int] = None,
                            expected_card_generation: Optional[int] = None, jobs=None,
                            steward_payload: Optional[dict] = None) -> int:
    """Publish a previously collected snapshot only if the seat's card/input window still matches."""
    st = _st()
    now = st.now_epoch() if now is None else now
    if seat.kind not in ("pane", "native"):
        return 0
    if expected_prompt_seq is not None and st.read_prompt_seq(seat) != expected_prompt_seq:
        raise RuntimeError("snapshot-input-changed")
    card = st.read_latest_card(seat)
    generation = int(card.get("generation", 0) or 0) if card else 0
    if expected_card_generation is not None and generation != expected_card_generation:
        raise RuntimeError("snapshot-card-changed")
    if steward_payload:
        steward_payload = _revalidate_snapshot_steward(seat, harness, sid, steward_payload)
    old_snapshot = read_snapshot(seat.key)
    affected = [*(old_snapshot.get("bindings") or [] if old_snapshot else []), *bindings]
    with binding_mutexes(affected):
        if read_snapshot(seat.key) != old_snapshot:
            raise RuntimeError("snapshot-binding-changed")
        if not bindings and not steward_payload and not card and not handover_rows(seat):
            with contextlib.suppress(OSError):
                os.unlink(_snapshot_path(seat.key))
            return 0
        path = _snapshot_path(seat.key)
        st._reject_symlink(path)
        snapshot = {
            "schema": SCHEMA, "seat": seat.as_dict(), "from": {"harness": harness, "sid": sid},
            "at": now, "prompt_seq": st.read_prompt_seq(seat), "card_generation": generation,
            "bindings": bindings, "snapshot_id": os.urandom(16).hex()}
        if steward_payload:
            snapshot["steward"] = steward_payload
        st.atomic_write_json(_snapshot_path(seat.key), snapshot)
        return len(bindings)


@contextlib.contextmanager
def source_session_mutex(harness, sid):
    """Shared source decision even when there are no jobs or destination seats differ."""
    key = "lineage-source-" + hashlib.sha256(json.dumps([harness, sid]).encode()).hexdigest()
    with _st().seat_lock(key):
        yield


def record_locked(seat, harness: str, sid: str, event: str, source: str, now: float) -> Optional[dict]:
    # Caller holds the physical seat. All edge writers then take the same
    # mutation lock before source/binding locks: reachability and publication
    # must see one graph even when concurrent edges have disjoint endpoints.
    with _st().seat_lock(LINEAGE_WRITE_KEY):
        snap = read_snapshot(seat.key)
        if not snap:
            return None
        with source_session_mutex(str(snap["from"].get("harness") or ""), str(snap["from"].get("sid") or "")):
            if read_snapshot(seat.key) != snap:
                return None
            old = str(snap["from"].get("sid") or "")
            result = resolve_recipient("seat-successor", session=old, harness=harness)
            if result.state == "unreadable" or (result.state == "confirmed" and result.session_id != sid):
                return None
            target = resolve_recipient("seat-successor", session=sid, harness=harness)
            if target.state == "unreadable" or target.session_id == old:
                return None
            return _record_locked(seat, harness, sid, event, source, now)


def _record_locked(seat, harness: str, sid: str, event: str, source: str, now: float) -> Optional[dict]:
    """Append the A -> B row when ``sid`` is the confirmed successor of the snapshot's session
    (caller holds the seat lock).  Returns the row, or None (nothing to do / not confirmed)."""
    st = _st()
    if seat.kind not in ("pane", "native") or event not in ("start", "prompt"):
        return None
    snap = read_snapshot(seat.key)
    if not snap:
        return None
    origin = snap["from"]
    old = str(origin.get("sid") or "")
    if not old or old == sid or origin.get("harness") != harness:
        return None
    rows = handover_rows(seat)
    prior = next((r for r in rows if r.get("from") == old and r.get("sid") == sid), None)
    if prior is not None:
        return prior                                   # duplicate callback is the same transition
    summary = st.session_summary(seat).get((harness, sid))
    if summary is None or float(summary.get("first_seen", 0) or 0) + 1.0 < float(snap.get("at", 0) or 0):
        return None                                    # a session older than the snapshot is not its successor
    if not _confirmed_clear(seat, harness, old, sid, event, source):
        return None
    taken = {(b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) for r in rows
             if r.get("from") == old for b in r.get("bindings") or () if isinstance(b, dict)}
    snapshot_bindings = [b for b in snap["bindings"] if isinstance(b, dict)]
    bindings = [b for b in snapshot_bindings
                if (b.get("jobs"), b.get("route"), b.get("hash"), b.get("node")) not in taken]
    if len(bindings) != len(snap["bindings"]):
        return None                                    # A -> C while A -> B stands: refused

    # Resolve each exact role again inside the seat transaction. A copied
    # snapshot is not authority after a release or a newer confirmed holder.
    snapshot_steward = snap.get("steward") if isinstance(snap.get("steward"), dict) else {}
    current_roles = resolved_steward_relations(seat_key=seat.key, include_released=True)
    steward_roles = []
    for candidate in snapshot_steward.get("roles") or ():
        if not isinstance(candidate, dict) or not candidate.get("generation"):
            continue
        matches = [row for row in current_roles if _role_identity(row) == _role_identity(candidate)]
        if len(matches) != 1 or matches[0].get("released"):
            continue
        current = matches[0]
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != old:
            continue
        adopted = dict(current)
        adopted["holder"] = {"harness": harness, "sid": sid}
        adopted["seat_key"] = seat.key
        adopted["lineage"] = [*(current.get("lineage") or []), {
            "from": old, "to": sid, "snapshot_id": snap.get("snapshot_id") or "legacy"}]
        steward_roles.append(adopted)

    current_watches = {str(row.get("watch_id")): row for row in
                       resolved_watch_references(seat_key=seat.key)}
    steward_watches = []
    for candidate in snapshot_steward.get("watches") or ():
        if not isinstance(candidate, dict) or not candidate.get("watch_id"):
            continue
        current = current_watches.get(str(candidate["watch_id"]))
        if not current:
            continue
        holder = current.get("holder") or {}
        if holder.get("harness") != harness or holder.get("sid") != old:
            continue
        if any(candidate.get(field) != current.get(field) for field in
               ("seat_key", "storage_root", "server", "target", "agent", "until", "deadline", "origin")):
            continue
        adopted = dict(current)
        adopted["holder"] = {"harness": harness, "sid": sid}
        adopted["seat_key"] = seat.key
        adopted["lineage"] = [*(current.get("lineage") or []), {
            "from": old, "to": sid, "snapshot_id": snap.get("snapshot_id") or "legacy"}]
        steward_watches.append(adopted)
    steward_payload = ({"schema": 1, "roles": steward_roles, "watches": steward_watches}
                       if steward_roles or steward_watches else None)

    # A selected job can acquire a permitted retry after enqueue but before the
    # confirmed clear hook.  Refresh only those selected logical keys, retaining
    # the snapshot rows as the original authority boundary.
    selected_keys = {_logical_key(binding) for binding in snapshot_bindings}
    graph = _pass_lineage_graph()
    transition = (harness, old, sid)
    # The matched booking may have published this exact succession already.
    # New retries use that same current holder's normal admission check.
    refresh_actor = sid if transition in graph.edges else old
    refreshed = []
    for jobs_path in dict.fromkeys(str(binding.get("jobs") or "") for binding in snapshot_bindings):
        if not jobs_path or not Path(jobs_path).is_file():
            continue
        try:
            # Only attempts the snapshot does not bind yet are asked about: the bound ones are
            # kept anyway, and each answer may verify a whole route inside this start hook.
            jobs_key = _jobs_key(jobs_path)
            bound = {str(b.get("attempt") or "") for b in snapshot_bindings
                     if _jobs_key(b.get("jobs") or "") == jobs_key}
            from dispatch_registry_cache import registry_view
            view = registry_view(Path(jobs_path))
            route_keys = {(route, digest, node) for selected_jobs, route, digest, node in selected_keys
                          if selected_jobs == jobs_key}
            fresh = {}
            for status, record in view.handover_candidates(old, route_keys):
                meta = record.handover_metadata()
                aid = meta.get("attempt_id", "")
                if (aid and aid not in bound
                        and (jobs_key, *route_identity(meta)) in selected_keys):
                    fresh[aid] = (status, meta)
            refreshed.extend(binding for binding, _status in _handover_candidates(
                harness, refresh_actor, jobs_path, rows=fresh))
        except Exception:  # noqa: BLE001 - the original snapshot remains a safe fallback
            continue
    bindings = _deduplicate_bindings([*bindings, *refreshed])
    row = {"ts": now, "harness": harness, "sid": sid, "event": ROW_EVENT, "from": old,
           "seat_key": seat.key, "snapshot_id": snap.get("snapshot_id"),
           "source": source or ("first-message" if harness == "opencode" else "reservation"),
           "bindings": bindings, "epoch": int((summary or {}).get("epoch", 0) or 0)}
    if steward_payload:
        row["steward"] = steward_payload
    with binding_mutexes(snapshot_bindings):
        if read_snapshot(seat.key) != snap:
            return None
        try:
            graph = _pass_lineage_graph()
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return None
        kept = []
        for binding in bindings:
            traversed = set()
            holder, holder_harness, disposition = _lineage_holder(
                graph, _logical_key(binding), str(binding.get("parent") or old), harness,
                traversed=traversed)
            if (disposition != "unresolved" and holder_harness == harness
                    and (holder == old or holder == sid and transition in traversed)):
                kept.append(binding)
        bindings = kept
        row["bindings"] = bindings
        _append_transition_row(seat, row)
        _bump_dispatch_edge_generation()
        refreshed_snapshot = dict(snap)
        refreshed_snapshot["bindings"] = bindings
        if steward_payload:
            refreshed_snapshot["steward"] = steward_payload
        try:
            st.atomic_write_json(_snapshot_path(seat.key), refreshed_snapshot)
        except Exception:  # noqa: BLE001 - the durable ledger row remains the handover authority
            pass
    return row


def record_retire_handover(predecessor_sid: str, predecessor_harness: str, successor_sid: str,
                           successor_harness: str, *, env=None, jobs=None,
                           now: Optional[float] = None, successor_seat=None) -> Optional[dict]:
    """Record the current holder's transfer to its exact seat successor.

    A validated typed acceptance may call this before reply delivery or physical
    retirement; retained retire settlement also calls it for legacy and missed
    transitions. The predecessor's unfinished depth-1 work (registered or
    inherited) is bound in the successor's existing seat ledger. Bindings retain
    the registered parent under which pending records stay stored, and registry
    rows remain untouched. ``source=retire`` keeps its exact binding-selection
    meaning for both callers. Returns the existing row on replay, or None when
    there is nothing currently owned to hand over, including attempts already
    handed to another session. When the registry file exists but its rows cannot
    be read, ownership is unknown rather than empty, so this raises
    :class:`RegistryUnreadable` instead of returning None; callers keep their
    existing continuation pending and observe again. Deferred retire uses the
    seat proved by its immutable requester and original successor mark; its
    observer need not own that physical pane.
    """
    if not predecessor_sid or not successor_sid or predecessor_sid == successor_sid:
        return None
    st = _st()
    seat = successor_seat or pane_seat(env, successor_harness, successor_sid)
    if seat is None or seat.kind not in {"pane", "native", "project"}:
        return None
    if seat.kind == "project":
        if st.session_identity(seat) != (successor_harness, successor_sid):
            return None  # A shared project/inherited pane is not an exact native receiver.
        import dataclasses
        seat = dataclasses.replace(st.session_seat(seat, successor_harness, successor_sid),
                                   key=st._native_session_key(successor_harness, successor_sid))
    jobs = jobs or _default_jobs()
    now = st.now_epoch() if now is None else now
    # Never acquire another physical seat after the common mutation lock.
    # Lock order: destination seat -> lineage write -> source -> sorted bindings.
    with st.seat_lock(seat.key), st.seat_lock(LINEAGE_WRITE_KEY), \
            source_session_mutex(predecessor_harness, predecessor_sid):
        seats, error = _lineage_index()
        if error:
            raise RegistryUnreadable(error)
        # A replay row is history, not an alternative current-recipient
        # judgment. Observe the common answer before using that history.
        target = resolve_recipient("seat-successor", session=successor_sid, harness=successor_harness)
        if target.state == "unreadable":
            raise RegistryUnreadable(target.reason)
        if target.session_id == predecessor_sid:
            return None
        source = resolve_recipient("seat-successor", session=predecessor_sid, harness=predecessor_harness)
        if source.state == "unreadable":
            raise RegistryUnreadable(source.reason)
        prior = [r for values in seats.values() for r in values
                 if (st.tidy_lineage_edge(r) is not None or r.get("source") == "retire")
                 and (st._handover_origin(r) == (predecessor_harness, predecessor_sid)
                      or isinstance(r.get("from"), str) and r["from"] == predecessor_sid
                      and r.get("source") == "retire")]
        if prior:
            return next((r for r in prior if (r["sid"], r["harness"]) ==
                         (successor_sid, successor_harness)), None)
        if source.session_id != predecessor_sid:
            return None
        rows = {}
        if jobs:
            try:
                from dispatch_registry_cache import registry_view
                view = registry_view(Path(jobs))
                if view.malformed_rows or view.has_invalid_utf8 or view.has_partial_tail:
                    raise RegistryUnreadable("registry-schema-invalid")
                rows = {record.handover_metadata().get("attempt_id", ""): (status, record.handover_metadata())
                        for status, record in _handover_candidate_rows(jobs, predecessor_sid)}
            except FileNotFoundError:
                pass  # A verified absent registry has zero jobs.
            except (OSError, ValueError) as exc:
                raise RegistryUnreadable(str(exc)) from exc
        possible_rows = _handover_candidates(predecessor_harness, predecessor_sid, jobs, rows=rows)
        own_before = read_snapshot(seat.key)
        affected = [*(own_before.get("bindings") or [] if own_before else []),
                    *(binding for binding, _status in possible_rows)]
        with binding_mutexes(affected):
            if read_snapshot(seat.key) != own_before:
                return None
            source = resolve_recipient("seat-successor", session=predecessor_sid, harness=predecessor_harness)
            if source.state == "unreadable":
                raise RegistryUnreadable(source.reason)
            if source.session_id != predecessor_sid:
                return None
            bindings = _deduplicate_bindings(binding for binding, _status in possible_rows
                if effective_parent(rows[binding["attempt"]][1], jobs) == predecessor_sid)
            # If a job changed while waiting, transfer only the still-owned bindings.
            kept = own_before["bindings"] if own_before and own_before["from"].get("sid") == successor_sid else []
            union = _deduplicate_bindings([*kept, *bindings])
            row = {"ts": now, "harness": successor_harness, "sid": successor_sid,
                   "event": ROW_EVENT, "from": {"harness": predecessor_harness, "sid": predecessor_sid},
                   "source": "retire", "bindings": bindings}
            seat_data = seat.as_dict()
            if seat.kind == "project":
                seat_data["session"] = {"harness": successor_harness, "sid": successor_sid}
            st.atomic_write_json(_snapshot_path(seat.key), {
                "schema": SCHEMA, "seat": seat_data,
                "from": {"harness": successor_harness, "sid": successor_sid}, "at": now,
                "bindings": union})
            _append_transition_row(seat, row)
            _bump_dispatch_edge_generation(jobs)
            return row


def _confirmed_clear(seat, harness: str, old: str, sid: str, event: str, source: str) -> bool:
    origins = {str(row.get("source") if row.get("event") == "start" else row.get("start_source") or "")
               for row in _st()._read_ledger_lines(seat, strict=True)
               if (row.get("harness"), row.get("sid")) == (harness, sid)}
    if source == "fork" or "fork" in origins:
        return False
    if event == "start" and source == "clear":
        return True
    try:
        import session_tidy_clear as clear
        booking = clear.read_reservation(seat.key)
    except (OSError, ValueError, TypeError):
        booking = None
    if bool(booking) and booking.get("sid") == old and booking.get("harness") == harness and (
            (booking.get("observed") or {}).get("sid") == sid or booking.get("new_session") == sid):
        return True
    if harness == "opencode" and event == "prompt":
        return not (origins | {source}) & {"startup", "resume", "select", "new"}
    return False


# ---------------------------------------------------------------------------
# The card line: the verified route and the existing resume command
# ---------------------------------------------------------------------------

def resume_line(seat, harness: str, sid: str) -> str:
    """One line for the successor's card: the routes it took over and the existing resume command.
    Only attempts the ledger shows handed to ``sid`` and the registry still shows open count."""
    try:
        rows = [r for r in handover_rows(seat) if r.get("sid") == sid]
        if not rows:
            return ""
        wanted = [b for r in rows for b in r.get("bindings") or () if isinstance(b, dict)]
        # Each registry is read once and each logical job looked at once: the card hook has
        # seconds, and a handover carries one binding per attempt of the same job.
        registries: dict = {}
        looked: set = set()
        shown: dict = {}
        for b in wanted:
            jobs = b.get("jobs")
            identity = (b.get("route"), b.get("hash"), b.get("node"))
            if not jobs or (jobs, identity) in looked or not Path(jobs).is_file():
                continue
            looked.add((jobs, identity))
            if jobs not in registries:
                grouped: dict = {}
                if _jobs_key(jobs) == jobs:            # a binding names its registry by resolved path
                    for status, meta in latest_rows(jobs).values():
                        if eligible_row(meta):
                            grouped.setdefault(route_identity(meta), []).append((status, meta))
                registries[jobs] = grouped
            for status, meta in registries[jobs].get(identity, ()):
                if status not in OPEN_ROW_STATUSES and status != "done":
                    continue
                # A finished attempt is shown only while its notice is owed and its route is
                # still open: a closed route has nothing to resume, whatever record lingers.
                # An unreadable lineage (unknown) is not shown as this session's work yet.
                if owns(meta, sid, jobs) is not True or (status == "done" and not (
                        _delivery_open(jobs, meta) and _route_open(jobs, meta))):
                    continue
                shown.setdefault((jobs, route_identity(meta)[0]), meta.get("route_file", ""))
        parts = []
        for (jobs, route), route_file in list(shown.items())[:2]:
            if route_file:
                from parent_next_directive import resume_command
                command = resume_command(route_file, jobs, agent_home=ROOT)
                parts.append(f"route={route} 이어서: {command}")
        if not parts:
            return ""
        text = "[이어받은 진행 작업] " + " | ".join(parts)
        return _st()._cut_bytes(text, LINE_MAX_BYTES)[0]
    except Exception:  # noqa: BLE001
        return ""
