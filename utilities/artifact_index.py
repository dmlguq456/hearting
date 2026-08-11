from __future__ import annotations

"""D-7 uniqueness index: pure, rebuildable, no filesystem I/O.

The index is a derived accelerator over published manifests, never the
durable source of truth (that is the published manifest + its events). See
`artifact_admission.py` for the effect layer that owns reading/writing
`index.json` and calls into this module with data already in hand.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

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
    got = set(row.keys())
    if got != set(expected):
        raise ValueError(
            "index {0}[{1!r}] row keys {2} do not match the closed shape {3}".format(
                section, key, sorted(got), sorted(expected)
            )
        )


def parse(payload: Mapping[str, Any]) -> IndexDocument:
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
    return IndexDocument(
        schema_version=payload["schema_version"],
        artifact_root_id=payload["artifact_root_id"],
        stable_ids={k: dict(v) for k, v in payload["stable_ids"].items()},
        routes={k: {rk: dict(rv) for rk, rv in v.items()} for k, v in payload["routes"].items()},
        event_ids={k: dict(v) for k, v in payload["event_ids"].items()},
        streams={k: dict(v) for k, v in payload["streams"].items()},
        manifests={k: dict(v) for k, v in payload["manifests"].items()},
        cycles={k: dict(v) for k, v in payload["cycles"].items()},
    )


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
    return _manifest_canonical_bytes(to_payload(index))


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
) -> ValidationReport:
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

    if not is_idempotent_retry and cycle_id in index.cycles:
        violations.append(
            Violation(
                "index-cycle-id-duplicate",
                "$.cycle.cycle_id",
                "cycle_id already present in index",
            )
        )

    parent_cycle_id = cycle.get("parent_cycle_id")
    if (
        parent_cycle_id is not None
        and not is_idempotent_retry
        and parent_cycle_id not in index.cycles
    ):
        violations.append(
            Violation(
                "index-orphan-parent-cycle",
                "$.cycle.parent_cycle_id",
                "parent cycle is not an admitted cycle in this root",
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
        elif kind not in _REUSABLE_SAME_KIND:
            violations.append(
                Violation(
                    "index-stable-id-duplicate",
                    "$.<stable-ids>[{0!r}]".format(stable_id),
                    "stable id already present in index",
                )
            )

    for root_id, route_id in declared_routes(document):
        existing = index.routes.get(root_id, {}).get(route_id)
        if existing is not None and not is_idempotent_retry:
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
        if existing is not None and not is_idempotent_retry:
            violations.append(
                Violation(
                    "index-event-id-reused",
                    "$.events[?event_id={0!r}]".format(event_id),
                    "event id already present in index",
                )
            )

    if not is_idempotent_retry:
        for stream_id, (min_seq, max_seq) in declared_streams(document).items():
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
    if existing_manifest is not None and not is_idempotent_retry:
        if existing_manifest.get("manifest_digest") != manifest_digest:
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
    stable_ids = dict(index.stable_ids)
    routes = {k: dict(v) for k, v in index.routes.items()}
    event_ids = dict(index.event_ids)
    streams = dict(index.streams)
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
        stable_ids[stable_id] = {
            "kind": kind,
            "cycle_id": cycle_id,
            "manifest_id": manifest_id,
        }

    for root_id, route_id in declared_routes(document):
        bucket = dict(routes.get(root_id, {}))
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

    for stream_id, (min_seq, max_seq) in declared_streams(document).items():
        last_event_id = None
        for row in document.get("events", []) or []:
            if isinstance(row, dict) and row.get("stream_id") == stream_id and row.get(
                "stream_sequence"
            ) == max_seq:
                last_event_id = row.get("event_id")
        streams[stream_id] = {
            "last_sequence": max_seq,
            "cycle_id": cycle_id,
            "last_event_id": last_event_id,
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


def build(
    admitted: Iterable[Tuple[Mapping[str, Any], str, str, str]]
) -> IndexDocument:
    """Rebuild from published documents, refusing cross-manifest conflicts.

    Rebuild input order is directory order, not admission order, so per-item
    stream-continuity cursors cannot be enforced here; instead the union of
    each stream's sequences is verified for duplicates and gaps after the fold.
    Every other uniqueness rule is the same `check()` used incrementally —
    a conflict is a refusal, never a silent overwrite (D-7).
    """
    items = list(admitted)
    if not items:
        raise ValueError("build() requires at least one admitted document to seed artifact_root_id")
    root_id = items[0][0].get("artifact_root_id")
    index = empty(root_id)
    stream_sequences: Dict[str, Dict[int, int]] = {}
    for document, cycle_path, manifest_digest, idempotency_key in items:
        report = check(
            index,
            document,
            idempotency_key=idempotency_key,
            manifest_digest=manifest_digest,
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
        for stream_id, (min_seq, max_seq) in declared_streams(document).items():
            bucket = stream_sequences.setdefault(stream_id, {})
            for row in document.get("events", []) or []:
                if not isinstance(row, dict) or row.get("stream_id") != stream_id:
                    continue
                seq = row.get("stream_sequence")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    bucket[seq] = bucket.get(seq, 0) + 1
        index = apply(
            index,
            document,
            cycle_path=cycle_path,
            manifest_digest=manifest_digest,
            idempotency_key=idempotency_key,
        )
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
    for document, _cycle_path, _digest, idempotency_key in items:
        cycle = document.get("cycle") if isinstance(document.get("cycle"), dict) else {}
        parent_cycle_id = cycle.get("parent_cycle_id")
        if parent_cycle_id is not None and parent_cycle_id not in index.cycles:
            raise ValueError(
                "build-conflict for idempotency key {0!r}: orphan parent cycle {1!r}".format(
                    idempotency_key, parent_cycle_id
                )
            )
    return index
