from __future__ import annotations

"""D-7 uniqueness index: pure, rebuildable, no filesystem I/O.

The index is a derived accelerator over published manifests, never the
durable source of truth (that is the published manifest + its events). See
`artifact_admission.py` for the effect layer that owns reading/writing
`index.json` and calls into this module with data already in hand.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple

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


def parse(payload: Mapping[str, Any]) -> IndexDocument:
    if not isinstance(payload, dict):
        raise ValueError("index payload must be an object")
    extra = set(payload.keys()) - _INDEX_KEYS
    if extra:
        raise ValueError("unknown index key(s): {0}".format(sorted(extra)))
    missing = _INDEX_KEYS - set(payload.keys())
    if missing:
        raise ValueError("missing index key(s): {0}".format(sorted(missing)))
    return IndexDocument(
        schema_version=payload["schema_version"],
        artifact_root_id=payload["artifact_root_id"],
        stable_ids=dict(payload["stable_ids"]),
        routes={k: dict(v) for k, v in payload["routes"].items()},
        event_ids=dict(payload["event_ids"]),
        streams=dict(payload["streams"]),
        manifests=dict(payload["manifests"]),
        cycles=dict(payload["cycles"]),
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


def check(
    index: IndexDocument,
    document: Mapping[str, Any],
    *,
    idempotency_key: str,
    manifest_digest: str,
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

    incoming_ids = declared_ids(document)
    for stable_id, kind in incoming_ids.items():
        if kind in ("manifest", "manifest_revision"):
            continue
        existing = index.stable_ids.get(stable_id)
        if existing is not None and not is_idempotent_retry:
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
        if kind in ("manifest", "manifest_revision"):
            continue
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
    items = list(admitted)
    if not items:
        raise ValueError("build() requires at least one admitted document to seed artifact_root_id")
    root_id = items[0][0].get("artifact_root_id")
    index = empty(root_id)
    for document, cycle_path, manifest_digest, idempotency_key in items:
        index = apply(
            index,
            document,
            cycle_path=cycle_path,
            manifest_digest=manifest_digest,
            idempotency_key=idempotency_key,
        )
    return index
