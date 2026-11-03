from __future__ import annotations

"""D-7 uniqueness index: pure, rebuildable, no filesystem I/O.

The index is a derived accelerator over published manifests, never the
durable source of truth (that is the published manifest + its events). See
`artifact_admission.py` for the effect layer that owns reading/writing
`index.json` and calls into this module with data already in hand.
"""

import os.path
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, Iterable, Mapping, Optional, Tuple

from artifact_manifest import (
    ValidationReport,
    Violation,
    canonical_bytes as _manifest_canonical_bytes,
    declared_ids,
    declared_routes,
    declared_streams,
)

INDEX_SCHEMA_VERSION = 1

_INDEX_KEYS = frozenset(
    {
        "schema_version",
        "artifact_root_id",
        "stable_ids",
        "routes",
        "event_ids",
        "streams",
        "manifests",
        "cycles",
    }
)


@dataclass(frozen=True)
class IndexDocument:
    schema_version: int
    artifact_root_id: str
    stable_ids: Mapping[str, Mapping[str, Any]]
    routes: Mapping[str, Mapping[str, Mapping[str, Any]]]
    event_ids: Mapping[str, Mapping[str, Any]]
    streams: Mapping[str, Mapping[str, Any]]
    manifests: Mapping[str, Mapping[str, Any]]
    cycles: Mapping[str, Mapping[str, Any]]


def empty(artifact_root_id: str) -> IndexDocument:
    return IndexDocument(
        schema_version=INDEX_SCHEMA_VERSION,
        artifact_root_id=artifact_root_id,
        stable_ids={},
        routes={},
        event_ids={},
        streams={},
        manifests={},
        cycles={},
    )


_ROW_SHAPES = {
    "stable_ids": frozenset({"kind", "cycle_id", "manifest_id"}),
    "event_ids": frozenset({"stream_id", "stream_sequence", "cycle_id"}),
    "streams": frozenset({"last_sequence", "cycle_id", "last_event_id"}),
    "manifests": frozenset(
        {"manifest_id", "manifest_revision_id", "cycle_id", "manifest_digest", "idempotency_key"}
    ),
    "cycles": frozenset({"campaign_id", "cycle_path", "manifest_digest"}),
}
_ROUTE_ROW_SHAPE = frozenset({"cycle_id", "route_hash"})


def _check_row_shape(section: str, key: Any, row: Any, expected: frozenset) -> None:
    """The index is a closed document at every depth, not only its top level.

    A forged or hand-edited nested row must fail parse loudly instead of being
    trusted by `idempotent_match()`/`check()` downstream.
    """
    if not isinstance(key, str) or not key:
        raise ValueError("index {0} key must be a non-empty string".format(section))
    if not isinstance(row, dict):
        raise ValueError("index {0}[{1!r}] row must be an object".format(section, key))
    if row.keys() != expected:
        raise ValueError(
            "index {0}[{1!r}] row keys {2} do not match the closed shape {3}".format(
                section, key, sorted(row), sorted(expected)
            )
        )
    for field_name, value in row.items():
        if field_name in ("stream_sequence", "last_sequence"):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    "index {0}[{1!r}].{2} must be an integer".format(
                        section, key, field_name
                    )
                )
        elif value is not None and not isinstance(value, str):
            raise ValueError(
                "index {0}[{1!r}].{2} must be a string or null".format(
                    section, key, field_name
                )
            )
    cycle_path = row.get("cycle_path")
    if cycle_path is not None:
        normalized = os.path.normpath(cycle_path)
        if (
            os.path.isabs(cycle_path)
            or normalized.startswith("..")
            or normalized != cycle_path.rstrip("/")
        ):
            raise ValueError(
                "index {0}[{1!r}].cycle_path must be a normalized "
                "root-relative path".format(section, key)
            )


def _parse(payload: Mapping[str, Any], *, owned: bool) -> IndexDocument:
    if not isinstance(payload, dict):
        raise ValueError("index payload must be an object")
    extra = set(payload.keys()) - _INDEX_KEYS
    if extra:
        raise ValueError("unknown index key(s): {0}".format(sorted(extra)))
    missing = _INDEX_KEYS - set(payload.keys())
    if missing:
        raise ValueError("missing index key(s): {0}".format(sorted(missing)))
    if not isinstance(payload["schema_version"], int) or isinstance(payload["schema_version"], bool):
        raise ValueError("index schema_version must be an integer")
    if not isinstance(payload["artifact_root_id"], str) or not payload["artifact_root_id"]:
        raise ValueError("index artifact_root_id must be a non-empty string")
    for section, expected in _ROW_SHAPES.items():
        rows = payload[section]
        if not isinstance(rows, dict):
            raise ValueError("index {0} must be an object".format(section))
        for key, row in rows.items():
            _check_row_shape(section, key, row, expected)
    routes = payload["routes"]
    if not isinstance(routes, dict):
        raise ValueError("index routes must be an object")
    for root_id, bucket in routes.items():
        if not isinstance(root_id, str) or not root_id:
            raise ValueError("index routes key must be a non-empty string")
        if not isinstance(bucket, dict):
            raise ValueError("index routes[{0!r}] must be an object".format(root_id))
        for route_id, row in bucket.items():
            _check_row_shape("routes[{0!r}]".format(root_id), route_id, row, _ROUTE_ROW_SHAPE)
    copy_rows = (lambda rows: rows) if owned else (lambda rows: {k: dict(v) for k, v in rows.items()})
    copy_routes = ((lambda rows: rows) if owned else
                   (lambda rows: {k: {rk: dict(rv) for rk, rv in v.items()} for k, v in rows.items()}))
    return IndexDocument(
        schema_version=payload["schema_version"],
        artifact_root_id=payload["artifact_root_id"],
        stable_ids=copy_rows(payload["stable_ids"]),
        routes=copy_routes(payload["routes"]),
        event_ids=copy_rows(payload["event_ids"]),
        streams=copy_rows(payload["streams"]),
        manifests=copy_rows(payload["manifests"]),
        cycles=copy_rows(payload["cycles"]),
    )


def parse(payload: Mapping[str, Any]) -> IndexDocument:
    """Validate and copy a public payload so its mutable rows stay independent."""
    return _parse(payload, owned=False)


def _parse_owned(payload: Dict[str, Any]) -> IndexDocument:
    """Validate a private JSON-loader result and adopt its already-owned rows."""
    return _parse(payload, owned=True)


def to_payload(index: IndexDocument) -> Dict[str, Any]:
    return {
        "schema_version": index.schema_version,
        "artifact_root_id": index.artifact_root_id,
        "stable_ids": dict(index.stable_ids),
        "routes": {k: dict(v) for k, v in index.routes.items()},
        "event_ids": dict(index.event_ids),
        "streams": dict(index.streams),
        "manifests": dict(index.manifests),
        "cycles": dict(index.cycles),
    }


def canonical_bytes(index: IndexDocument) -> bytes:
    sections = (index.stable_ids, index.routes, index.event_ids, index.streams,
                index.manifests, index.cycles)
    if all(type(section) is dict for section in sections) and all(
        type(bucket) is dict for bucket in index.routes.values()
    ):
        return _manifest_canonical_bytes({
            "schema_version": index.schema_version,
            "artifact_root_id": index.artifact_root_id,
            "stable_ids": index.stable_ids,
            "routes": index.routes,
            "event_ids": index.event_ids,
            "streams": index.streams,
            "manifests": index.manifests,
            "cycles": index.cycles,
        })
    return _manifest_canonical_bytes(to_payload(index))


def copy_section(section: Mapping[str, Any]) -> Any:
    """A writable copy of one section that shares nothing a caller can change.

    A plain section is copied as a dict.  A section that keeps most of its rows outside
    `index.json` (`artifact_admission`'s per-cycle rows) copies only what it holds in
    memory, so changing one cycle's rows never costs a copy of every other cycle's."""
    copier = getattr(section, "copy_section", None)
    return copier() if callable(copier) else dict(section)


def idempotent_match(
    index: IndexDocument,
    document: Mapping[str, Any],
    *,
    idempotency_key: str,
    manifest_digest: str,
) -> bool:
    existing = index.manifests.get(idempotency_key)
    if existing is None:
        return False
    return existing.get("manifest_digest") == manifest_digest


# Kinds that legitimately recur across cycle manifests: one campaign owns many
# cycles (D-1), and canonical shared references with their immutable revisions
# are recorded by every cycle that used them (D-3). Same id + same kind is a
# re-reference, never a collision; every other kind is single-owner.
_REUSABLE_SAME_KIND = frozenset(
    {"campaign", "shared_reference", "shared_reference_revision"}
)


def check(
    index: IndexDocument,
    document: Mapping[str, Any],
    *,
    idempotency_key: str,
    manifest_digest: str,
    repository_id: Optional[str] = None,
    known_parent_cycle_ids: Optional[Collection[str]] = None,
    replaces_manifest_digest: Optional[str] = None,
) -> ValidationReport:
    """Judge ``document`` against the index.

    ``known_parent_cycle_ids`` names cycles that exist as producer records of
    the same root but are not (yet) in the index -- a parent still open, or
    one deleted since.  A parent is a reference, not an ordering or campaign
    constraint (§45 D-123): it only has to be a cycle of this root.

    ``replaces_manifest_digest`` makes ``document`` the same cycle's next
    manifest (§45 D-124): the cycle's current index row must still carry that
    digest (the compare-and-swap), IDs, routes and events the cycle already owns
    are references to what it has, and only its new events advance a stream
    cursor.  Every ID another cycle owns is still refused.
    """
    violations = []

    if document.get("artifact_root_id") != index.artifact_root_id:
        violations.append(
            Violation(
                "index-root-identity-mismatch",
                "$.artifact_root_id",
                "manifest artifact_root_id does not match frozen root identity",
            )
        )

    if repository_id is not None and document.get("repository_id") != repository_id:
        violations.append(
            Violation(
                "index-repository-identity-mismatch",
                "$.repository_id",
                "manifest repository_id does not match frozen root identity",
            )
        )

    cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
    cycle_id = cycle.get("cycle_id")
    is_idempotent_retry = idempotent_match(
        index, document, idempotency_key=idempotency_key, manifest_digest=manifest_digest
    )

    replacing = replaces_manifest_digest is not None and not is_idempotent_retry

    def owned_by_this_cycle(row: Any) -> bool:
        return replacing and isinstance(row, dict) and row.get("cycle_id") == cycle_id

    if not is_idempotent_retry and cycle_id in index.cycles and not replacing:
        violations.append(
            Violation(
                "index-cycle-id-duplicate",
                "$.cycle.cycle_id",
                "cycle_id already present in index",
            )
        )

    parent_cycle_id = cycle.get("parent_cycle_id")
    if parent_cycle_id is not None and not is_idempotent_retry and not replacing:
        if parent_cycle_id == cycle_id:
            violations.append(
                Violation(
                    "index-self-parent-cycle",
                    "$.cycle.parent_cycle_id",
                    "cycle cannot be its own parent",
                )
            )
        elif parent_cycle_id not in index.cycles and parent_cycle_id not in (known_parent_cycle_ids or ()):
            violations.append(
                Violation(
                    "index-orphan-parent-cycle",
                    "$.cycle.parent_cycle_id",
                    "parent cycle is not a cycle of this root",
                )
            )

    incoming_ids = declared_ids(document)
    for stable_id, kind in incoming_ids.items():
        existing = index.stable_ids.get(stable_id)
        if existing is None or is_idempotent_retry:
            continue
        existing_kind = existing.get("kind") if isinstance(existing, dict) else None
        if existing_kind != kind:
            violations.append(
                Violation(
                    "index-stable-id-kind-conflict",
                    "$.<stable-ids>[{0!r}]".format(stable_id),
                    "stable id already present with kind {0!r}".format(existing_kind),
                )
            )
        elif kind not in _REUSABLE_SAME_KIND and not owned_by_this_cycle(existing):
            violations.append(
                Violation(
                    "index-stable-id-duplicate",
                    "$.<stable-ids>[{0!r}]".format(stable_id),
                    "stable id already present in index",
                )
            )

    for root_id, route_id in declared_routes(document):
        existing = index.routes.get(root_id, {}).get(route_id)
        if existing is not None and not is_idempotent_retry and not owned_by_this_cycle(existing):
            violations.append(
                Violation(
                    "index-route-composite-duplicate",
                    "$.routes[{0!r},{1!r}]".format(root_id, route_id),
                    "(artifact_root_id, route_id) already present in index",
                )
            )

    for event_id in [
        row.get("event_id")
        for row in document.get("events", []) or []
        if isinstance(row, dict)
    ]:
        existing = index.event_ids.get(event_id)
        if existing is not None and not is_idempotent_retry and not owned_by_this_cycle(existing):
            violations.append(
                Violation(
                    "index-event-id-reused",
                    "$.events[?event_id={0!r}]".format(event_id),
                    "event id already present in index",
                )
            )

    if not is_idempotent_retry:
        incoming = document
        if replacing:
            # A re-referenced event is the cycle's own; only a new one moves a cursor.
            incoming = {"events": [row for row in document.get("events", []) or []
                                   if isinstance(row, dict) and row.get("event_id") not in index.event_ids]}
        for stream_id, (min_seq, max_seq) in declared_streams(incoming).items():
            existing_stream = index.streams.get(stream_id)
            if existing_stream is None:
                if min_seq != 1:
                    violations.append(
                        Violation(
                            "index-stream-sequence-discontinuous",
                            "$.events[?stream_id={0!r}]".format(stream_id),
                            "new stream must start at sequence 1",
                        )
                    )
            else:
                last_sequence = existing_stream.get("last_sequence", 0)
                if min_seq != last_sequence + 1:
                    violations.append(
                        Violation(
                            "index-stream-sequence-discontinuous",
                            "$.events[?stream_id={0!r}]".format(stream_id),
                            "incoming minimum sequence does not continue from index cursor",
                        )
                    )

    existing_manifest = index.manifests.get(idempotency_key)
    if replacing and existing_manifest is None:
        violations.append(
            Violation(
                "manifest-revision-append-out-of-scope",
                "$.manifest_id",
                "no current index row to replace for this idempotency key",
            )
        )
    if existing_manifest is not None and not is_idempotent_retry:
        if replacing:
            expected_digest = replaces_manifest_digest
        else:
            expected_digest = manifest_digest
        if existing_manifest.get("manifest_digest") != expected_digest:
            violations.append(
                Violation(
                    "manifest-revision-append-out-of-scope",
                    "$.manifest_id",
                    "existing manifest for this idempotency key has a different digest",
                )
            )

    sorted_violations = tuple(
        sorted(violations, key=lambda v: (v.code, v.path, v.detail))
    )
    return ValidationReport(ok=(len(sorted_violations) == 0), violations=sorted_violations)


def apply(
    index: IndexDocument,
    document: Mapping[str, Any],
    *,
    cycle_path: str,
    manifest_digest: str,
    idempotency_key: str,
) -> IndexDocument:
    stable_ids = copy_section(index.stable_ids)
    routes = {k: dict(v) for k, v in index.routes.items()}
    event_ids = copy_section(index.event_ids)
    streams = copy_section(index.streams)
    manifests = dict(index.manifests)
    cycles = dict(index.cycles)

    cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
    cycle_id = cycle.get("cycle_id")
    campaign_id = cycle.get("campaign_id")
    manifest_id = document.get("manifest_id")

    for stable_id, kind in declared_ids(document).items():
        # manifest/manifest_revision ids are tracked here too: a manifest
        # revision id is immutable and single-use (D-4/D-6), so its reuse by a
        # different cycle or key must be refusable from the same uniqueness
        # surface as every other stable id.
        if kind in _REUSABLE_SAME_KIND:
            # A shared entity has no single owning cycle; storing the last
            # declaring cycle would make the row depend on processing order
            # and break rebuild-vs-incremental byte equality (D-7).
            stable_ids[stable_id] = {
                "kind": kind,
                "cycle_id": None,
                "manifest_id": None,
            }
        else:
            stable_ids[stable_id] = {
                "kind": kind,
                "cycle_id": cycle_id,
                "manifest_id": manifest_id,
            }

    for root_id, route_id in declared_routes(document):
        bucket = routes.get(root_id)
        if bucket is None:
            bucket = {}
        bucket[route_id] = {"cycle_id": cycle_id, "route_hash": None}
        routes[root_id] = bucket

    for row in document.get("events", []) or []:
        if not isinstance(row, dict):
            continue
        event_id = row.get("event_id")
        if event_id is None:
            continue
        event_ids[event_id] = {
            "stream_id": row.get("stream_id"),
            "stream_sequence": row.get("stream_sequence"),
            "cycle_id": cycle_id,
        }

    # One pass over the events, not one per stream: a cycle can declare tens of
    # thousands of single-event streams. The last matching row wins, as before.
    event_at: Dict[Tuple[Any, Any], Any] = {}
    for row in document.get("events", []) or []:
        if (isinstance(row, dict) and isinstance(row.get("stream_id"), str)
                and isinstance(row.get("stream_sequence"), (int, float))):
            event_at[(row["stream_id"], row["stream_sequence"])] = row.get("event_id")
    for stream_id, (min_seq, max_seq) in declared_streams(document).items():
        streams[stream_id] = {
            "last_sequence": max_seq,
            "cycle_id": cycle_id,
            "last_event_id": event_at.get((stream_id, max_seq)),
        }

    manifests[idempotency_key] = {
        "manifest_id": manifest_id,
        "manifest_revision_id": document.get("manifest_revision_id"),
        "cycle_id": cycle_id,
        "manifest_digest": manifest_digest,
        "idempotency_key": idempotency_key,
    }

    cycles[cycle_id] = {
        "campaign_id": campaign_id,
        "cycle_path": cycle_path,
        "manifest_digest": manifest_digest,
    }

    return IndexDocument(
        schema_version=index.schema_version,
        artifact_root_id=index.artifact_root_id,
        stable_ids=stable_ids,
        routes=routes,
        event_ids=event_ids,
        streams=streams,
        manifests=manifests,
        cycles=cycles,
    )


def retire(index: IndexDocument, cycle_id: str) -> IndexDocument:
    """§45 D-126: a deleted cycle has no current row; every ID its documents declared stays owned.

    The cycle's `manifests` and `cycles` rows go; `stable_ids`, `routes`, `event_ids` and `streams`
    keep naming it, so an ID it used is never issued again and an earlier reference still resolves."""
    if cycle_id not in index.manifests and cycle_id not in index.cycles:
        return index
    manifests = {k: v for k, v in index.manifests.items() if v.get("cycle_id") != cycle_id and k != cycle_id}
    cycles = {k: v for k, v in index.cycles.items() if k != cycle_id}
    return IndexDocument(
        schema_version=index.schema_version, artifact_root_id=index.artifact_root_id,
        stable_ids=index.stable_ids, routes=index.routes, event_ids=index.event_ids,
        streams=index.streams, manifests=manifests, cycles=cycles)


def build(
    admitted: Iterable[Tuple[Mapping[str, Any], str, str, str]],
    *,
    known_parent_cycle_ids: Optional[Collection[str]] = None,
    preserved: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    retired: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    artifact_root_id: Optional[str] = None,
) -> IndexDocument:
    """Rebuild from published documents, refusing cross-manifest conflicts.

    ``preserved`` maps a cycle ID to that cycle's earlier documents (§45 D-124).
    They never replace the current row: the current document is folded last and
    carries every earlier event, while an ID only an earlier document declared
    (a removed artifact or revision) stays owned by its cycle, so a rebuilt
    index refuses to reuse it just as the incrementally updated one does.

    ``retired`` maps a deleted cycle's ID to its preserved documents (§45 D-126): they own their IDs
    again after the rebuild, but the cycle has no current row.

    Rebuild input order is directory order, not admission order, so per-item
    stream-continuity cursors cannot be enforced here; instead the union of
    each stream's sequences is verified for duplicates and gaps after the fold.
    Every other uniqueness rule is the same `check()` used incrementally —
    a conflict is a refusal, never a silent overwrite (D-7).
    """
    items = list(admitted)
    if not items and not artifact_root_id:
        raise ValueError("build() requires at least one admitted document to seed artifact_root_id")
    root_id = items[0][0].get("artifact_root_id") if items else artifact_root_id
    index = empty(root_id)
    stream_sequences: Dict[str, Dict[int, int]] = {}
    for document, cycle_path, manifest_digest, idempotency_key in items:
        report = check(
            index,
            document,
            idempotency_key=idempotency_key,
            manifest_digest=manifest_digest,
            known_parent_cycle_ids=known_parent_cycle_ids,
        )
        conflict_codes = sorted(
            {
                v.code
                for v in report.violations
                if v.code != "index-stream-sequence-discontinuous"
                and v.code != "index-orphan-parent-cycle"
            }
        )
        if conflict_codes:
            raise ValueError(
                "build-conflict for idempotency key {0!r}: {1}".format(
                    idempotency_key, ", ".join(conflict_codes)
                )
            )
        earlier_documents = [
            old for old in (preserved or {}).get(
                (document.get("cycle") or {}).get("cycle_id") if isinstance(document.get("cycle"), dict) else None, ())
            if old.get("manifest_revision_id") != document.get("manifest_revision_id")
        ]
        declared = declared_streams(document)
        for stream_id in declared:
            stream_sequences.setdefault(stream_id, {})
        for row in document.get("events", []) or []:
            if (not isinstance(row, dict) or not isinstance(row.get("stream_id"), str)
                    or row["stream_id"] not in declared):
                continue
            seq = row.get("stream_sequence")
            if isinstance(seq, int) and not isinstance(seq, bool):
                bucket = stream_sequences[row["stream_id"]]
                bucket[seq] = bucket.get(seq, 0) + 1
        for old in earlier_documents:
            index = apply(
                index,
                old,
                cycle_path=cycle_path,
                manifest_digest=manifest_digest,
                idempotency_key=idempotency_key,
            )
        index = apply(
            index,
            document,
            cycle_path=cycle_path,
            manifest_digest=manifest_digest,
            idempotency_key=idempotency_key,
        )
    for retired_id, old_documents in sorted((retired or {}).items()):
        for old in old_documents:
            index = apply(index, old, cycle_path="", manifest_digest="", idempotency_key=retired_id)
        index = retire(index, retired_id)
    for stream_id, bucket in sorted(stream_sequences.items()):
        duplicates = sorted(seq for seq, count in bucket.items() if count > 1)
        if duplicates:
            raise ValueError(
                "build-conflict for stream {0!r}: duplicate sequence(s) {1}".format(
                    stream_id, duplicates
                )
            )
        expected = set(range(1, (max(bucket) if bucket else 0) + 1))
        missing = sorted(expected - set(bucket))
        if missing:
            raise ValueError(
                "build-conflict for stream {0!r}: missing sequence(s) {1}".format(
                    stream_id, missing
                )
            )
    # Parent linkage is order-independent: verify against the fully folded set.
    parent_of: Dict[str, str] = {}
    for document, _cycle_path, _digest, idempotency_key in items:
        cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
        cid = cycle.get("cycle_id")
        parent_cycle_id = cycle.get("parent_cycle_id")
        if parent_cycle_id is None:
            continue
        if parent_cycle_id not in index.cycles and parent_cycle_id not in (known_parent_cycle_ids or ()):
            raise ValueError(
                "build-conflict for idempotency key {0!r}: orphan parent cycle {1!r}".format(
                    idempotency_key, parent_cycle_id
                )
            )
        parent_of[cid] = parent_cycle_id
    # A parent chain from tampered on-disk input could be circular; incremental
    # admission cannot create one (a parent must already exist), so this is a
    # rebuild-only defence.
    for start in sorted(parent_of):
        seen = set()
        node = start
        while node in parent_of:
            if node in seen:
                raise ValueError(
                    "build-conflict: circular parent chain through cycle {0!r}".format(
                        start
                    )
                )
            seen.add(node)
            node = parent_of[node]
    return index
