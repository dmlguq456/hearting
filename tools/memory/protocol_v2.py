#!/usr/bin/env python3
"""Canonical immutable operations and a pure memory protocol-v2 reducer.

The module intentionally has no database, filesystem, Git, clock, or runtime
configuration dependency.  Arrival order and transport topology cannot affect
its classification or fold results.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

PROTOCOL_MAJOR = 2
SUPPORTED_SCHEMA_MINORS = frozenset({0})
MAX_ENVELOPE_BYTES = 1024 * 1024
MAX_MUTATIONS = 128
MAX_PARENTS = 256
MAX_FRONTIER = 64
MAX_DIAGNOSTIC_IDS = 100
MAX_HARD_DIAGNOSTICS = 100
MAX_NESTING = 32
MAX_STRING_BYTES = 512 * 1024
MAX_IDENTIFIER_BYTES = 1024
# These mirror the physical Git exchange-tree admission bounds.  They are not
# semantic retention/fold limits; every operation admitted by that transport
# remains eligible for the complete-set fold.
MAX_CLASSIFY_OPERATIONS = 1_000_000
MAX_CLASSIFY_BYTES = 256 * 1024 * 1024
# Operations at or below this size use the compact bitset reachability
# accelerator.  It is a tranche threshold, not a retention or protocol cap.
MAX_FOLD_OPERATIONS = 4096
MAX_FOLD_WORK = 50_000_000
MAX_RESOLUTION_WORK = 10_000_000
UINT64_MAX = (1 << 64) - 1

KNOWN_KINDS = frozenset({
    "put", "consume", "supersede", "tombstone", "force-tombstone",
    "restore", "resolve", "merge",
})
DESTRUCTIVE_KINDS = frozenset({"supersede", "tombstone", "force-tombstone", "merge"})
COVERAGE_SENSITIVE_KINDS = DESTRUCTIVE_KINDS | frozenset({"restore"})
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
REPLICA_RE = re.compile(r"^[0-9a-f]{32,128}$")
INJECTION_RE = re.compile(
    r"(ignore (all |the )?previous|disregard (all|previous)|you must now|"
    r"system prompt|<\|.*?\|>|act as (an? )?(admin|root)|"
    r"override (the )?instruction)",
    re.I,
)
PAYLOAD_FIELDS = frozenset({
    "protocol_major", "schema_minor", "replica_id", "counter", "parents",
    "project_key", "kind", "frontiers", "mutations", "provenance",
})
MUTATION_FIELDS = frozenset({
    "record_id", "mutation_ordinal", "post_state", "tombstone",
    "target_op_id", "edge",
})
PROVENANCE_FIELDS = frozenset({
    "actor", "authority", "reason", "graveyard_evidence", "source",
    "timestamp", "migration_epoch",
})
EDGE_FIELDS = frozenset({"source", "target", "scope"})
RECORD_STATE_FIELDS = frozenset({
    "id", "tier", "scope", "type", "cwd_origin", "created", "updated",
    "expires", "source", "tags", "links", "body", "strength",
    "last_accessed", "injection_flag", "delivery_state", "headline",
    "aliases", "entities", "topics", "artifact_refs", "status",
    "canonical_id", "superseded_by", "capsule_version",
})
RECORD_LIST_FIELDS = frozenset({
    "tags", "links", "aliases", "entities", "topics", "artifact_refs",
})
RECORD_NULLABLE_TEXT_FIELDS = frozenset({
    "cwd_origin", "created", "updated", "expires", "source",
    "last_accessed", "headline", "superseded_by",
})
TOMBSTONE_FIELDS = frozenset({"action", "pending", "prior_digest", "record_id"})


class ProtocolError(ValueError):
    """Closed, machine-readable hard protocol failure."""

    def __init__(self, code: str, message: str, *, op_id: str | None = None,
                 path: str | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.op_id, self.path = code, message, op_id, path

    def as_dict(self) -> dict[str, Any]:
        out = {"code": self.code, "message": self.message}
        if self.op_id is not None:
            out["op_id"] = self.op_id
        if self.path is not None:
            out["path"] = self.path
        return out


class Disposition(str, Enum):
    ACCEPTED = "accepted"
    DEFERRED = "deferred"
    QUARANTINED = "quarantined-unsupported"
    HARD_FAILURE = "hard-failure"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    op_id: str | None = None
    related_ids: tuple[str, ...] = ()
    diagnostic_id: str = field(init=False)

    def __post_init__(self) -> None:
        ids = tuple(sorted(set(self.related_ids)))[:MAX_DIAGNOSTIC_IDS]
        object.__setattr__(self, "related_ids", ids)
        raw = _canonical_unbounded({"code": self.code, "op_id": self.op_id,
                                    "related_ids": list(ids)})
        object.__setattr__(self, "diagnostic_id", hashlib.sha256(raw).hexdigest()[:24])

    def as_dict(self) -> dict[str, Any]:
        out = {"code": self.code, "diagnostic_id": self.diagnostic_id,
               "related_ids": list(self.related_ids)}
        if self.op_id is not None:
            out["op_id"] = self.op_id
        return out


@dataclass(frozen=True)
class ValidatedOperation:
    op_id: str
    payload: Mapping[str, Any]
    envelope: Mapping[str, Any]
    raw: bytes
    path: str
    supported: bool
    unsupported_reason: str | None = None

    @property
    def dot(self) -> tuple[str, int]:
        return str(self.payload["replica_id"]), int(self.payload["counter"])

    @property
    def key(self) -> tuple[int, bytes, str]:
        return (int(self.payload["counter"]),
                str(self.payload["replica_id"]).encode("utf-8"), self.op_id)

    @property
    def parents(self) -> tuple[str, ...]:
        return tuple(self.payload["parents"])

    def mutation_for(self, record_id: str) -> Mapping[str, Any] | None:
        return next((m for m in self.payload["mutations"]
                     if m["record_id"] == record_id), None)


@dataclass(frozen=True)
class Classification:
    operations: Mapping[str, ValidatedOperation]
    accepted: tuple[str, ...]
    deferred: Mapping[str, Diagnostic]
    quarantined: Mapping[str, Diagnostic]
    hard_failures: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.hard_failures

    @property
    def dispositions(self) -> dict[str, str]:
        out = {key: Disposition.ACCEPTED.value for key in self.accepted}
        out.update({key: Disposition.DEFERRED.value for key in self.deferred})
        out.update({key: Disposition.QUARANTINED.value for key in self.quarantined})
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "deferred": {k: v.as_dict() for k, v in sorted(self.deferred.items())},
            "quarantined": {k: v.as_dict() for k, v in sorted(self.quarantined.items())},
            "hard_failures": [item.as_dict() for item in self.hard_failures],
        }


@dataclass(frozen=True)
class Conflict:
    record_id: str
    variants: Mapping[str, Mapping[str, Any]]
    provisional_op_id: str

    def as_dict(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "variant_op_ids": sorted(self.variants),
                "variants": {k: self.variants[k] for k in sorted(self.variants)},
                "provisional_op_id": self.provisional_op_id}


@dataclass(frozen=True)
class FoldResult(Mapping[str, Any]):
    classification: Classification
    frontiers: Mapping[str, tuple[str, ...]]
    records: Mapping[str, Mapping[str, Any]]
    conflicts: Mapping[str, Conflict]
    blocked: Mapping[str, Diagnostic]
    tombstones: Mapping[str, str]
    supersession_graph: Mapping[str, str]
    accepted_set_digest: str
    materialized_digest: str

    @property
    def accepted(self) -> tuple[str, ...]:
        return self.classification.accepted

    @property
    def deferred(self) -> Mapping[str, Diagnostic]:
        return self.classification.deferred

    @property
    def quarantined(self) -> Mapping[str, Diagnostic]:
        return self.classification.quarantined

    def as_dict(self) -> dict[str, Any]:
        return {**self.classification.as_dict(),
                "frontiers": {k: list(v) for k, v in sorted(self.frontiers.items())},
                "records": {k: self.records[k] for k in sorted(self.records)},
                "conflicts": {k: self.conflicts[k].as_dict() for k in sorted(self.conflicts)},
                "blocked": {k: v.as_dict() for k, v in sorted(self.blocked.items())},
                "tombstones": dict(sorted(self.tombstones.items())),
                "supersession_graph": dict(sorted(self.supersession_graph.items())),
                "accepted_set_digest": self.accepted_set_digest,
                "materialized_digest": self.materialized_digest}

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _check_string(value: str, *, identifier: bool = False) -> None:
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ProtocolError("invalid-unicode", "Unicode surrogate is forbidden") from exc
    limit = MAX_IDENTIFIER_BYTES if identifier else MAX_STRING_BYTES
    if len(encoded) > limit:
        raise ProtocolError("string-limit", f"UTF-8 string exceeds {limit} bytes")


def _json_string(value: str) -> str:
    _check_string(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encode(value: Any, depth: int = 0) -> str:
    if depth > MAX_NESTING:
        raise ProtocolError("nesting-limit", "canonical JSON nesting limit exceeded")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise ProtocolError("float-forbidden", "floats, NaN and Infinity are forbidden")
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item, depth + 1) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise ProtocolError("non-string-key", "object keys must be strings")
        for key in keys:
            _check_string(key)
        keys.sort(key=lambda key: key.encode("utf-8"))
        return "{" + ",".join(_json_string(key) + ":" + _encode(value[key], depth + 1)
                              for key in keys) + "}"
    raise ProtocolError("invalid-json-type", f"unsupported type: {type(value).__name__}")


def _canonical_unbounded(value: Any) -> bytes:
    return (_encode(value) + "\n").encode("utf-8")


def _canonical_string_array_digest(values: Iterable[str]) -> str:
    """Hash canonical string-array bytes without an aggregate encoder cap."""
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, value in enumerate(sorted(values)):
        if index:
            digest.update(b",")
        digest.update(_json_string(value).encode("utf-8"))
    digest.update(b"]\n")
    return digest.hexdigest()


def canonical_bytes(value: Any, *, max_bytes: int = MAX_ENVELOPE_BYTES) -> bytes:
    """Exact Unicode canonical JSON with one trailing LF."""
    raw = _canonical_unbounded(value)
    if len(raw) > max_bytes:
        raise ProtocolError("envelope-limit", f"canonical JSON exceeds {max_bytes} bytes")
    return raw


canonical_json_bytes = canonical_bytes


def _reject_number(value: str) -> Any:
    raise ProtocolError("float-forbidden", f"non-integer JSON number is forbidden: {value}")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ProtocolError("duplicate-key", f"duplicate object key: {key!r}")
        out[key] = value
    return out


def canonical_loads(raw: bytes | bytearray | memoryview | str, *,
                    require_canonical: bool = True,
                    max_bytes: int = MAX_ENVELOPE_BYTES) -> Any:
    """Parse strict UTF-8, integer-only JSON and authenticate canonical bytes."""
    try:
        encoded = raw.encode("utf-8", "strict") if isinstance(raw, str) else bytes(raw)
    except UnicodeEncodeError as exc:
        raise ProtocolError("invalid-unicode", "input contains a surrogate") from exc
    if len(encoded) > max_bytes:
        raise ProtocolError("envelope-limit", f"JSON exceeds {max_bytes} bytes")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("bom-forbidden", "UTF-8 BOM is forbidden")
    try:
        text = encoded.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid-utf8", "input is not strict UTF-8") from exc
    try:
        value = json.loads(text, parse_float=_reject_number, parse_constant=_reject_number,
                           object_pairs_hook=_unique_object)
    except ProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolError("invalid-json", "input is not valid bounded JSON") from exc
    canonical = canonical_bytes(value, max_bytes=max_bytes)
    if require_canonical and encoded != canonical:
        raise ProtocolError("noncanonical-json", "input is not exact canonical JSON")
    return value


parse_canonical_json = canonical_loads


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], where: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ProtocolError("closed-schema", f"{where} missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")


def _uint(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= UINT64_MAX:
        raise ProtocolError("invalid-integer", f"{name} must be an unsigned 64-bit integer")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid-identifier", f"{name} must be a non-empty string")
    _check_string(value, identifier=True)
    return value


def _op_id(value: Any, name: str = "op_id") -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise ProtocolError("invalid-op-id", f"{name} must be lower-case SHA-256 hex")
    return value


def _set_ids(value: Any, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ProtocolError("array-limit", f"{name} exceeds {maximum}")
    checked = tuple(_op_id(item, name) for item in value)
    expected = tuple(sorted(set(checked), key=lambda item: canonical_bytes(item)))
    if checked != expected:
        raise ProtocolError("noncanonical-set", f"{name} is not sorted and duplicate-free")
    return checked


def operation_path(op_id: str) -> str:
    checked = _op_id(op_id)
    return f"protocol/v2/ops/{checked[:2]}/{checked}.json"


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_NESTING:
        raise ProtocolError("nesting-limit", "operation nesting limit exceeded")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            _check_string(value)
        return
    if isinstance(value, float):
        raise ProtocolError("float-forbidden", "floats are forbidden")
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProtocolError("non-string-key", "operation object keys must be strings")
        for key, item in value.items():
            _check_string(key)
            _json_value(item, depth + 1)
        return
    raise ProtocolError("invalid-json-type", "operation contains a non-JSON value")


def _mutation(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("invalid-mutation", "mutation must be an object")
    keys = frozenset(value)
    required = frozenset({"record_id", "mutation_ordinal"})
    if not required.issubset(keys) or not keys.issubset(MUTATION_FIELDS) or keys == required:
        raise ProtocolError("closed-schema", "mutation has missing or unknown fields")
    rid = _identifier(value["record_id"], "record_id")
    _uint(value["mutation_ordinal"], "mutation_ordinal")
    if "post_state" in value:
        state = value["post_state"]
        if not isinstance(state, Mapping):
            raise ProtocolError("invalid-post-state", "post_state must be an object")
        _json_value(state)
        if state.get("id", rid) != rid:
            raise ProtocolError("record-id-mismatch", "post_state.id differs from record_id")
    if "tombstone" in value:
        if not isinstance(value["tombstone"], Mapping):
            raise ProtocolError("invalid-tombstone", "tombstone must be an evidence object")
        _json_value(value["tombstone"])
    if "target_op_id" in value:
        _op_id(value["target_op_id"], "target_op_id")
    if "edge" in value:
        edge = value["edge"]
        if not isinstance(edge, Mapping):
            raise ProtocolError("invalid-edge", "edge must be an object")
        _exact_keys(edge, EDGE_FIELDS, "edge")
        for key in EDGE_FIELDS:
            _identifier(edge[key], f"edge.{key}")
    return value


def _mutation_shape(value: Mapping[str, Any], expected: frozenset[str], kind: str) -> None:
    if frozenset(value) != expected:
        raise ProtocolError(
            "mutation-shape",
            f"{kind} mutation must contain exactly {sorted(expected)}",
        )


def _validate_record_state(state: Mapping[str, Any], record_id: str) -> None:
    """Validate the exact dependency-free RECORD_COLS wire projection."""
    _exact_keys(state, RECORD_STATE_FIELDS, "post_state")
    if state["id"] != record_id:
        raise ProtocolError("record-id-mismatch", "post_state.id differs from record_id")
    _identifier(state["id"], "post_state.id")
    _identifier(state["type"], "post_state.type")
    _identifier(state["canonical_id"], "post_state.canonical_id")
    if state["tier"] not in {"working", "durable"}:
        raise ProtocolError("invalid-record-state", "post_state.tier is invalid")
    if state["scope"] not in {"project", "global"}:
        raise ProtocolError("invalid-record-state", "post_state.scope is invalid")
    if state["scope"] == "project":
        _identifier(state["cwd_origin"], "post_state.cwd_origin")
    if state["delivery_state"] not in {"ordinary", "pending", "consumed"}:
        raise ProtocolError("invalid-record-state", "post_state.delivery_state is invalid")
    if state["status"] not in {"active", "superseded"}:
        raise ProtocolError("invalid-record-state", "post_state.status is invalid")
    for name in ("body",):
        if not isinstance(state[name], str):
            raise ProtocolError("invalid-record-state", f"post_state.{name} must be text")
        _check_string(state[name])
    for name in RECORD_NULLABLE_TEXT_FIELDS:
        value = state[name]
        if value is not None and not isinstance(value, str):
            raise ProtocolError(
                "invalid-record-state", f"post_state.{name} must be text or null"
            )
        if isinstance(value, str):
            _check_string(value)
    for name in RECORD_LIST_FIELDS:
        value = state[name]
        if not isinstance(value, list):
            raise ProtocolError("invalid-record-state", f"post_state.{name} must be a list")
        encoded: list[bytes] = []
        for item in value:
            if not isinstance(item, str):
                raise ProtocolError(
                    "invalid-record-state", f"post_state.{name} items must be text"
                )
            _check_string(item)
            encoded.append(canonical_bytes(item))
        if encoded != sorted(set(encoded)):
            raise ProtocolError(
                "noncanonical-set",
                f"post_state.{name} must be canonical sorted and duplicate-free",
            )
    strength = state["strength"]
    if isinstance(strength, bool) or not isinstance(strength, int) or not 1 <= strength <= UINT64_MAX:
        raise ProtocolError("invalid-record-state", "post_state.strength must be a positive integer")
    injection = state["injection_flag"]
    if isinstance(injection, bool) or not isinstance(injection, int) or injection not in {0, 1}:
        raise ProtocolError("invalid-record-state", "post_state.injection_flag must be 0 or 1")
    if injection == 0 and INJECTION_RE.search(state["body"]):
        raise ProtocolError(
            "invalid-injection-flag",
            "post_state body matches the injection guard but injection_flag is zero",
        )
    capsule_version = state["capsule_version"]
    if (
        isinstance(capsule_version, bool)
        or not isinstance(capsule_version, int)
        or not 1 <= capsule_version <= UINT64_MAX
    ):
        raise ProtocolError(
            "invalid-record-state", "post_state.capsule_version must be a positive integer"
        )


def _record_namespace(state: Mapping[str, Any]) -> str:
    if state["scope"] == "global":
        return "global"
    return str(state["cwd_origin"])


def _validate_tombstone_evidence(value: Mapping[str, Any], record_id: str) -> None:
    _exact_keys(value, TOMBSTONE_FIELDS, "tombstone")
    if value["record_id"] != record_id:
        raise ProtocolError("record-id-mismatch", "tombstone.record_id differs from record_id")
    _identifier(value["record_id"], "tombstone.record_id")
    _identifier(value["action"], "tombstone.action")
    if not isinstance(value["pending"], bool):
        raise ProtocolError("invalid-tombstone", "tombstone.pending must be boolean")
    _op_id(value["prior_digest"], "tombstone.prior_digest")


def _validate_kind_mutations(
    kind: str,
    mutations: Sequence[Mapping[str, Any]],
    provenance: Mapping[str, Any],
    project_key: str,
) -> None:
    """Enforce the schema-minor-0 discriminated mutation union."""
    base = frozenset({"record_id", "mutation_ordinal"})
    for mutation in mutations:
        if "post_state" in mutation:
            _validate_record_state(mutation["post_state"], mutation["record_id"])
            if _record_namespace(mutation["post_state"]) != project_key:
                raise ProtocolError(
                    "project-key-mismatch",
                    "post_state logical namespace differs from payload.project_key",
                )
        if "tombstone" in mutation:
            _validate_tombstone_evidence(mutation["tombstone"], mutation["record_id"])
        if "edge" in mutation and mutation["edge"]["scope"] != project_key:
            raise ProtocolError(
                "project-key-mismatch",
                "edge.scope differs from payload.project_key",
            )
    if kind in {"put", "consume", "resolve"}:
        for mutation in mutations:
            _mutation_shape(mutation, base | {"post_state"}, kind)
            if kind == "consume" and mutation["post_state"]["delivery_state"] != "consumed":
                raise ProtocolError(
                    "invalid-consume-state", "consume post_state must be consumed"
                )
        if kind == "resolve":
            if not provenance.get("reason"):
                raise ProtocolError("resolve-evidence-missing", "resolve requires provenance.reason")
    elif kind == "restore":
        for mutation in mutations:
            _mutation_shape(mutation, base | {"post_state", "target_op_id"}, kind)
    elif kind in {"tombstone", "force-tombstone"}:
        for mutation in mutations:
            _mutation_shape(mutation, base | {"tombstone"}, kind)
    elif kind == "supersede":
        if len(mutations) != 2:
            raise ProtocolError(
                "mutation-shape", "supersede requires one source and one target mutation"
            )
        sources = [mutation for mutation in mutations if "edge" in mutation]
        if len(sources) != 1:
            raise ProtocolError("invalid-edge", "supersede requires exactly one source edge")
        source = sources[0]
        _mutation_shape(source, base | {"post_state", "edge"}, kind)
        edge = source["edge"]
        if edge["source"] != source["record_id"] or edge["source"] == edge["target"]:
            raise ProtocolError(
                "invalid-edge", "supersede edge source must match record_id and differ from target"
            )
        target = next(
            (mutation for mutation in mutations if mutation["record_id"] == edge["target"]),
            None,
        )
        if target is None or target is source:
            raise ProtocolError("invalid-edge", "supersede must include its exact target record")
        _mutation_shape(target, base | {"post_state"}, kind)
        source_state = source["post_state"]
        target_state = target["post_state"]
        if (
            source_state["status"] != "superseded"
            or source_state["canonical_id"] != edge["target"]
            or source_state["superseded_by"] != edge["target"]
            or target_state["status"] != "active"
            or target_state["canonical_id"] != edge["target"]
            or target_state["superseded_by"] is not None
        ):
            raise ProtocolError(
                "invalid-supersede-state",
                "supersede post_states do not encode the exact edge transition",
            )
    elif kind == "merge":
        targets = [mutation for mutation in mutations if "post_state" in mutation]
        sources = [mutation for mutation in mutations if "tombstone" in mutation]
        if len(targets) != 1 or not sources or len(sources) + 1 != len(mutations):
            raise ProtocolError(
                "mutation-shape",
                "merge requires one target post_state and one or more source tombstones",
            )
        target = targets[0]
        _mutation_shape(target, base | {"post_state"}, kind)
        target_id = target["record_id"]
        scopes: set[str] = set()
        source_ids: set[str] = set()
        for source in sources:
            _mutation_shape(source, base | {"edge", "tombstone"}, kind)
            edge = source["edge"]
            source_id = source["record_id"]
            if (
                edge["source"] != source_id
                or edge["target"] != target_id
                or source_id == target_id
            ):
                raise ProtocolError(
                    "invalid-edge", "merge source edges must share the exact target"
                )
            if source_id in source_ids:
                raise ProtocolError("invalid-edge", "merge source records must be unique")
            source_ids.add(source_id)
            scopes.add(edge["scope"])
        if len(scopes) != 1:
            raise ProtocolError("invalid-edge", "all merge source edges must share one scope")


def _validate_payload(payload: Any) -> tuple[bool, str | None]:
    if not isinstance(payload, Mapping):
        raise ProtocolError("invalid-payload", "payload must be an object")
    _json_value(payload)
    version_fields = frozenset({"protocol_major", "schema_minor"})
    if not version_fields.issubset(payload):
        raise ProtocolError(
            "routing-fields-missing", "payload lacks protocol version routing fields"
        )
    major = _uint(payload["protocol_major"], "protocol_major")
    if major != PROTOCOL_MAJOR:
        raise ProtocolError("unknown-major", f"unsupported protocol major {major}")
    minor = _uint(payload["schema_minor"], "schema_minor")
    routing_fields = frozenset({
        "replica_id", "counter", "parents", "project_key",
    })
    if not routing_fields.issubset(payload):
        raise ProtocolError(
            "routing-fields-missing", "payload lacks causal routing fields"
        )
    if not isinstance(payload["replica_id"], str) or not REPLICA_RE.fullmatch(payload["replica_id"]):
        raise ProtocolError("invalid-replica-id", "replica_id needs >=128 bits of lower-case hex")
    _uint(payload["counter"], "counter")
    parents = _set_ids(payload["parents"], "parents", MAX_PARENTS)
    project_key = _identifier(payload["project_key"], "project_key")
    if minor not in SUPPORTED_SCHEMA_MINORS:
        return False, "unknown-schema-minor"
    _exact_keys(payload, PAYLOAD_FIELDS, "payload")
    kind = _identifier(payload["kind"], "kind")
    frontiers = payload["frontiers"]
    if not isinstance(frontiers, list) or len(frontiers) > MAX_MUTATIONS:
        raise ProtocolError("invalid-frontiers", "frontiers must be a bounded list")
    union: set[str] = set()
    frontier_ids: list[str] = []
    for frontier in frontiers:
        if not isinstance(frontier, Mapping):
            raise ProtocolError("invalid-frontiers", "each frontier must be an object")
        _exact_keys(frontier, frozenset({"record_id", "heads"}), "frontier")
        rid, heads = frontier["record_id"], frontier["heads"]
        _identifier(rid, "frontier record_id")
        frontier_ids.append(rid)
        union.update(_set_ids(heads, f"frontiers[{rid}]", MAX_FRONTIER))
    encoded_ids = [item.encode("utf-8") for item in frontier_ids]
    if encoded_ids != sorted(encoded_ids) or len(frontier_ids) != len(set(frontier_ids)):
        raise ProtocolError("frontier-order", "frontiers must be record_id sorted and duplicate-free")
    if parents != tuple(sorted(union, key=lambda item: canonical_bytes(item))):
        raise ProtocolError("parent-frontier-mismatch", "parents are not the exact frontier union")
    mutations = payload["mutations"]
    if not isinstance(mutations, list) or not 1 <= len(mutations) <= MAX_MUTATIONS:
        raise ProtocolError("mutation-limit", "mutations must contain 1..128 objects")
    checked = [_mutation(item) for item in mutations]
    order = [(item["record_id"].encode("utf-8"), item["mutation_ordinal"]) for item in checked]
    if order != sorted(order) or len(order) != len(set(order)):
        raise ProtocolError("mutation-order", "mutations are not unique record/ordinal sorted")
    if {item["record_id"] for item in checked} != set(frontier_ids):
        raise ProtocolError("frontier-record-mismatch", "frontiers do not exactly cover mutations")
    provenance = payload["provenance"]
    if not isinstance(provenance, Mapping) or not set(provenance).issubset(PROVENANCE_FIELDS):
        raise ProtocolError("closed-schema", "provenance contains an unknown field")
    for name, item in provenance.items():
        if not isinstance(item, str):
            raise ProtocolError("invalid-provenance", f"provenance.{name} must be text")
        _check_string(item)
    if kind not in KNOWN_KINDS:
        return False, "unknown-kind"
    _validate_kind_mutations(kind, checked, provenance, project_key)
    if kind == "resolve" and len(parents) < 2:
        raise ProtocolError("resolve-frontier", "resolve must descend at least two maximal heads")
    if kind == "force-tombstone":
        for required in ("authority", "reason", "graveyard_evidence"):
            if not provenance.get(required):
                raise ProtocolError("force-evidence-missing", f"force requires {required}")
    return True, None


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if isinstance(out.get("parents"), Sequence):
        out["parents"] = sorted(set(out["parents"]), key=lambda item: canonical_bytes(item))
    if isinstance(out.get("frontiers"), Mapping):
        out["frontiers"] = [{"record_id": rid,
                              "heads": sorted(set(heads), key=lambda item: canonical_bytes(item))}
                             for rid, heads in out["frontiers"].items()]
    if isinstance(out.get("frontiers"), list):
        out["frontiers"] = sorted(
            ({"record_id": item["record_id"],
              "heads": sorted(set(item["heads"]), key=lambda value: canonical_bytes(value))}
             for item in out["frontiers"]),
            key=lambda item: item["record_id"].encode("utf-8"),
        )
    if isinstance(out.get("mutations"), Sequence):
        out["mutations"] = sorted((dict(item) for item in out["mutations"]),
                                  key=lambda item: (str(item.get("record_id", "")).encode("utf-8"),
                                                    item.get("mutation_ordinal", -1)))
    if isinstance(out.get("provenance"), Mapping):
        out["provenance"] = dict(out["provenance"])
    return out


def build_operation(payload: Mapping[str, Any] | None = None, /, **fields: Any) -> dict[str, Any]:
    """Build ``{op_id,payload}`` from a payload mapping or keyword fields."""
    if payload is not None and fields:
        raise TypeError("pass a payload or keyword fields, not both")
    supplied = fields if payload is None else payload
    normalized = (
        dict(supplied)
        if supplied.get("schema_minor") not in SUPPORTED_SCHEMA_MINORS
        else _normalize_payload(supplied)
    )
    _validate_payload(normalized)
    envelope = {"op_id": hashlib.sha256(canonical_bytes(normalized)).hexdigest(),
                "payload": normalized}
    validate_operation(envelope)
    return envelope


def validate_operation(operation: ValidatedOperation | Mapping[str, Any] | bytes |
                       bytearray | memoryview | str,
                       path: str | None = None) -> ValidatedOperation:
    """Validate canonical bytes, closed types, payload hash and exchange path."""
    if isinstance(operation, ValidatedOperation):
        if path is not None and path != operation.path:
            raise ProtocolError("path-mismatch", "path does not match op_id",
                                op_id=operation.op_id, path=path)
        return operation
    if isinstance(operation, Mapping):
        envelope, raw = dict(operation), canonical_bytes(operation)
    else:
        raw = operation.encode("utf-8") if isinstance(operation, str) else bytes(operation)
        envelope = canonical_loads(raw)
    if not isinstance(envelope, Mapping):
        raise ProtocolError("invalid-envelope", "envelope must be an object")
    _exact_keys(envelope, frozenset({"op_id", "payload"}), "envelope")
    embedded = _op_id(envelope["op_id"])
    supported, reason = _validate_payload(envelope["payload"])
    computed = hashlib.sha256(canonical_bytes(envelope["payload"])).hexdigest()
    if embedded != computed:
        raise ProtocolError("hash-mismatch", "op_id differs from payload digest",
                            op_id=embedded, path=path)
    expected = operation_path(embedded)
    if path is not None and path != expected:
        raise ProtocolError("path-mismatch", "filename/path differs from op_id",
                            op_id=embedded, path=path)
    return ValidatedOperation(embedded, envelope["payload"], envelope, raw,
                              expected, supported, reason)


def _input(item: Any) -> tuple[Any, str | None]:
    if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
        return item[1], item[0]
    return item, None


def classify_operations(operations: Iterable[Any] | Mapping[str, Any]) -> Classification:
    """Validate and causally classify a complete candidate operation set."""
    iterable = operations.items() if isinstance(operations, Mapping) else operations
    validated: dict[str, ValidatedOperation] = {}
    hard: list[Diagnostic] = []

    def add_hard(diagnostic: Diagnostic) -> None:
        if len(hard) < MAX_HARD_DIAGNOSTICS:
            hard.append(diagnostic)

    dots: dict[tuple[str, int], str] = {}
    input_count = 0
    input_bytes = 0
    for item in iterable:
        input_count += 1
        if input_count > MAX_CLASSIFY_OPERATIONS:
            raise ProtocolError(
                "classification-object-limit",
                f"physical ingress exceeds {MAX_CLASSIFY_OPERATIONS} operations",
            )
        raw, path = _input(item)
        try:
            op = validate_operation(raw, path)
        except ProtocolError as exc:
            add_hard(Diagnostic(exc.code, exc.op_id))
            continue
        input_bytes += len(op.raw)
        if input_bytes > MAX_CLASSIFY_BYTES:
            raise ProtocolError(
                "classification-byte-limit",
                f"physical ingress exceeds {MAX_CLASSIFY_BYTES} envelope bytes",
            )
        prior = validated.get(op.op_id)
        if prior is not None and prior.raw != op.raw:
            add_hard(Diagnostic("immutable-path-mutation", op.op_id))
            continue
        validated[op.op_id] = op
        prior_id = dots.get(op.dot)
        if prior_id is not None and prior_id != op.op_id:
            add_hard(Diagnostic("duplicate-dot-equivocation", prior_id, (op.op_id,)))
            add_hard(Diagnostic("duplicate-dot-equivocation", op.op_id, (prior_id,)))
        else:
            dots[op.dot] = op.op_id
    record_projects: dict[str, dict[str, set[str]]] = {}
    for op_id, op in validated.items():
        project = str(op.payload["project_key"])
        if op.supported:
            for mutation in op.payload["mutations"]:
                record_projects.setdefault(mutation["record_id"], {}).setdefault(
                    project, set()
                ).add(op_id)
        for parent in op.parents:
            parent_op = validated.get(parent)
            if parent_op is not None and parent_op.payload["project_key"] != project:
                add_hard(Diagnostic("cross-project-parent", op_id, (parent,)))
    for projects in record_projects.values():
        if len(hard) >= MAX_HARD_DIAGNOSTICS:
            break
        if len(projects) <= 1:
            continue
        involved = tuple(sorted(op_id for ids in projects.values() for op_id in ids))
        remaining = MAX_HARD_DIAGNOSTICS - len(hard)
        related_pool = involved[:MAX_DIAGNOSTIC_IDS + 1]
        for op_id in involved[:remaining]:
            related = tuple(
                candidate for candidate in related_pool if candidate != op_id
            )[:MAX_DIAGNOSTIC_IDS]
            add_hard(
                Diagnostic(
                    "cross-project-record-id-collision",
                    op_id,
                    related,
                )
            )
    if hard:
        return Classification(validated, (), {}, {}, tuple(sorted(
            hard, key=lambda item: (item.code, item.op_id or "", item.diagnostic_id))))
    quarantine = {op_id: Diagnostic(op.unsupported_reason or "unsupported", op_id)
                  for op_id, op in validated.items() if not op.supported}
    unavailable = set(quarantine)
    children: dict[str, list[str]] = {op_id: [] for op_id in validated}
    for op_id, op in validated.items():
        for parent in op.parents:
            if parent in validated:
                children[parent].append(op_id)
    deferred_ids: set[str] = set()
    queue = list(sorted(unavailable))
    for op_id, op in sorted(validated.items()):
        if op_id in unavailable:
            continue
        if any(parent not in validated for parent in op.parents):
            deferred_ids.add(op_id)
            unavailable.add(op_id)
            queue.append(op_id)
    cursor = 0
    while cursor < len(queue):
        unavailable_parent = queue[cursor]
        cursor += 1
        for child in children[unavailable_parent]:
            if child in unavailable:
                continue
            unavailable.add(child)
            deferred_ids.add(child)
            queue.append(child)
    deferred: dict[str, Diagnostic] = {}
    for op_id in sorted(deferred_ids):
        missing = tuple(
            parent for parent in validated[op_id].parents
            if parent not in validated or parent in unavailable
        )
        code = (
            "missing-parent"
            if any(parent not in validated for parent in missing)
            else "unavailable-parent"
        )
        deferred[op_id] = Diagnostic(code, op_id, missing)
    return Classification(validated, tuple(sorted(set(validated) - unavailable)),
                          deferred, quarantine)


class _WorkBudget:
    """Cheap deterministic guard against adversarial aggregate fold work."""

    __slots__ = ("remaining",)

    def __init__(self, limit: int = MAX_FOLD_WORK) -> None:
        self.remaining = limit

    def spend(self, amount: int) -> None:
        if amount < 0 or amount > self.remaining:
            raise ProtocolError("fold-work-limit", "fold work budget exceeded")
        self.remaining -= amount


class _AncestryIndex:
    """Iterative DAG index with bounded bitsets and linear-space fallback."""

    __slots__ = (
        "bits", "bit_for", "budget", "children", "compact", "ids", "order",
        "parents", "position", "tree_intervals", "word_cost",
    )

    def __init__(self, ops: Mapping[str, ValidatedOperation], budget: _WorkBudget) -> None:
        self.ids = tuple(sorted(ops))
        self.budget = budget
        self.compact = len(ops) <= MAX_FOLD_OPERATIONS
        self.bit_for = (
            {op_id: 1 << index for index, op_id in enumerate(self.ids)}
            if self.compact else {}
        )
        self.word_cost = (
            max(1, (len(self.ids) + 63) // 64) if self.compact else 1
        )
        indegree = {op_id: 0 for op_id in ops}
        children: dict[str, list[str]] = {op_id: [] for op_id in ops}
        self.parents = {
            op_id: tuple(parent for parent in op.parents if parent in ops)
            for op_id, op in ops.items()
        }
        for op_id, op in ops.items():
            for parent in self.parents[op_id]:
                indegree[op_id] += 1
                children[parent].append(op_id)
        budget.spend(len(ops) + sum(indegree.values()) * self.word_cost)
        def topology_key(op_id: str) -> tuple[int, bytes, str]:
            key = getattr(ops[op_id], "key", None)
            return key if isinstance(key, tuple) else (0, b"", op_id)

        ready = [(topology_key(op_id), op_id)
                 for op_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        ancestry_bits: dict[str, int] = {}
        order: list[str] = []
        while ready:
            _, op_id = heapq.heappop(ready)
            if self.compact:
                bits = 0
                for parent in self.parents[op_id]:
                    bits |= ancestry_bits[parent] | self.bit_for[parent]
                ancestry_bits[op_id] = bits
            order.append(op_id)
            for child in children[op_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    heapq.heappush(ready, (topology_key(child), child))
        if len(order) != len(ops):
            cyclic = min(op_id for op_id, degree in indegree.items() if degree)
            raise ProtocolError(
                "parent-cycle", "parent DAG contains a cycle", op_id=cyclic
            )
        self.bits = ancestry_bits
        self.children = {
            op_id: tuple(values) for op_id, values in children.items()
        }
        self.order = tuple(order)
        self.position = {op_id: index for index, op_id in enumerate(self.order)}
        self.tree_intervals: dict[str, tuple[int, int]] | None = None
        if not self.compact and all(len(values) <= 1 for values in self.parents.values()):
            intervals: dict[str, tuple[int, int]] = {}
            entered: dict[str, int] = {}
            clock = 0
            roots = [op_id for op_id in self.order if not self.parents[op_id]]
            budget.spend(len(ops) + sum(len(v) for v in self.children.values()))
            for root in roots:
                stack: list[tuple[str, bool]] = [(root, False)]
                while stack:
                    op_id, exiting = stack.pop()
                    if exiting:
                        clock += 1
                        intervals[op_id] = (entered[op_id], clock)
                        continue
                    clock += 1
                    entered[op_id] = clock
                    stack.append((op_id, True))
                    for child in reversed(self.children[op_id]):
                        stack.append((child, False))
            self.tree_intervals = intervals

    def is_ancestor(self, left: str, right: str) -> bool:
        if not self.compact:
            self.budget.spend(1)
        if left == right or self.position[left] >= self.position[right]:
            return False
        if self.compact:
            return bool(self.bits[right] & self.bit_for[left])
        if left in self.parents[right]:
            return True
        if self.tree_intervals is not None:
            left_in, left_out = self.tree_intervals[left]
            right_in, right_out = self.tree_intervals[right]
            return left_in < right_in and right_out < left_out
        stack = list(self.parents[right])
        seen: set[str] = set()
        while stack:
            self.budget.spend(1)
            candidate = stack.pop()
            if candidate == left:
                return True
            if candidate in seen or self.position[candidate] <= self.position[left]:
                continue
            seen.add(candidate)
            stack.extend(self.parents[candidate])
        return False

    def mask(self, ids: Iterable[str]) -> Any:
        if not self.compact:
            return frozenset(ids)
        out = 0
        for op_id in ids:
            out |= self.bit_for[op_id]
        return out

    def ids_from_bits(self, bits: int) -> tuple[str, ...]:
        found: list[str] = []
        while bits and len(found) < MAX_DIAGNOSTIC_IDS:
            low = bits & -bits
            found.append(self.ids[low.bit_length() - 1])
            bits ^= low
        return tuple(found)

    def maximal(self, ids: Iterable[str], budget: _WorkBudget) -> tuple[str, ...]:
        selected = tuple(sorted(set(ids)))
        if not self.compact:
            budget.spend(len(selected))
            if len(selected) > MAX_FRONTIER:
                selected_set = set(selected)
                has_selected_descendant: set[str] = set()
                for op_id in reversed(self.order):
                    children = self.children[op_id]
                    budget.spend(1 + len(children))
                    if any(
                        child in selected_set or child in has_selected_descendant
                        for child in children
                    ):
                        has_selected_descendant.add(op_id)
                return tuple(
                    item for item in selected
                    if item not in has_selected_descendant
                )
            return tuple(
                item for item in selected
                if not any(
                    item != other and self.is_ancestor(item, other)
                    for other in selected
                )
            )
        budget.spend(2 * len(selected) * self.word_cost)
        selected_bits = self.mask(selected)
        nonmaximal = 0
        for op_id in selected:
            nonmaximal |= self.bits[op_id] & selected_bits
        return tuple(op_id for op_id in selected
                     if not (nonmaximal & self.bit_for[op_id]))

    def frontier_matches(
        self,
        op_id: str,
        record_mask: Any,
        heads: Sequence[str],
        budget: _WorkBudget,
    ) -> tuple[bool, tuple[str, ...]]:
        if not self.compact:
            candidates = tuple(
                candidate for candidate in record_mask
                if self.is_ancestor(candidate, op_id)
            )
            expected = self.maximal(candidates, budget)
            declared = tuple(heads)
            if declared != expected:
                return False, tuple(sorted(set(declared) ^ set(expected)))
            return True, ()
        budget.spend((2 * len(heads) + 2) * self.word_cost)
        prior = self.bits[op_id] & record_mask
        declared = self.mask(heads)
        if declared & ~prior:
            return False, self.ids_from_bits(declared ^ prior)
        nonmaximal = 0
        coverage = declared
        for head in heads:
            nonmaximal |= self.bits[head] & declared
            coverage |= self.bits[head]
        missing = prior & ~coverage
        if nonmaximal or missing:
            return False, self.ids_from_bits(nonmaximal | missing)
        return True, ()


def _ancestry(
    ops: Mapping[str, ValidatedOperation], budget: _WorkBudget
) -> _AncestryIndex:
    return _AncestryIndex(ops, budget)


def _maximal(
    ids: Iterable[str], ancestry: _AncestryIndex, budget: _WorkBudget
) -> tuple[str, ...]:
    return ancestry.maximal(ids, budget)


def _state(op: ValidatedOperation, rid: str) -> Mapping[str, Any] | None:
    mutation = op.mutation_for(rid)
    # Destructiveness is mutation-local: a merge source tombstone removes only
    # that source, while post-states in the same atomic operation stay live.
    if mutation is None or "tombstone" in mutation:
        return None
    state = mutation.get("post_state")
    return state if isinstance(state, Mapping) else None


def _frontier_heads(op: ValidatedOperation, rid: str) -> tuple[str, ...]:
    for frontier in op.payload["frontiers"]:
        if frontier["record_id"] == rid:
            return tuple(frontier["heads"])
    raise ProtocolError("frontier-record-mismatch", f"missing frontier for {rid}")


def _pending_state(state: Mapping[str, Any] | None) -> bool:
    return bool(state and (state.get("pending") is True or
                           state.get("delivery_state") == "pending"))


def _has_tombstone(op: ValidatedOperation, rid: str) -> bool:
    mutation = op.mutation_for(rid)
    return bool(mutation and "tombstone" in mutation)


def _cycle(graph: Mapping[str, str], source: str, target: str) -> bool:
    cursor, seen = target, set()
    if source == target:
        return True
    while cursor in graph and cursor not in seen:
        seen.add(cursor)
        cursor = graph[cursor]
        if cursor == source:
            return True
    return False


def _materialize_projection(
    ops: Mapping[str, ValidatedOperation],
    by_record: Mapping[str, Sequence[str]],
    frontiers: Mapping[str, tuple[str, ...]],
    blocked: Mapping[str, Diagnostic],
    effective_tombstones: set[tuple[str, str]],
    ancestry: _AncestryIndex,
    budget: _WorkBudget,
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Conflict],
    dict[str, str],
]:
    records: dict[str, Mapping[str, Any]] = {}
    conflicts: dict[str, Conflict] = {}
    tombstones: dict[str, str] = {}
    for rid, heads in frontiers.items():
        candidates: set[str] = set()
        visible_tombstones: set[str] = set()
        record_tombstones = {
            op_id for op_id, record_id in effective_tombstones
            if record_id == rid and op_id not in blocked
        }
        live_ops = {
            op_id
            for op_id in by_record[rid]
            if op_id not in blocked and _state(ops[op_id], rid) is not None
        }
        budget.spend(
            ancestry.word_cost * (
                len(live_ops) * max(1, len(heads)) * (1 + len(record_tombstones))
                + len(record_tombstones) * max(1, len(heads)) * (1 + len(live_ops))
            )
        )
        for candidate in live_ops:
            for head in heads:
                if candidate != head and not ancestry.is_ancestor(candidate, head):
                    continue
                removed_on_branch = any(
                    ancestry.is_ancestor(candidate, tomb)
                    and (tomb == head or ancestry.is_ancestor(tomb, head))
                    for tomb in record_tombstones
                )
                if not removed_on_branch:
                    candidates.add(candidate)
                    break
        for tomb in record_tombstones:
            for head in heads:
                if tomb != head and not ancestry.is_ancestor(tomb, head):
                    continue
                restored_on_branch = any(
                    ancestry.is_ancestor(tomb, live)
                    and (live == head or ancestry.is_ancestor(live, head))
                    for live in live_ops
                )
                if not restored_on_branch:
                    visible_tombstones.add(tomb)
                    break
        live = _maximal(candidates, ancestry, budget)
        variants = {candidate: _state(ops[candidate], rid) for candidate in live}
        variants = {key: value for key, value in variants.items() if value is not None}
        if variants:
            provisional = max(variants, key=lambda item: ops[item].key)
            records[rid] = variants[provisional]
            # Seed replicas author distinct operations/dots even when their
            # complete post-state bytes are identical. Preserve those ops and
            # frontier heads, but do not manufacture a semantic conflict.
            distinct_states = {_canonical_unbounded(value) for value in variants.values()}
            if len(distinct_states) > 1:
                conflicts[rid] = Conflict(rid, variants, provisional)
        elif visible_tombstones:
            tombstones[rid] = max(visible_tombstones, key=lambda item: ops[item].key)
    return records, conflicts, tombstones


def _graph_endpoint_allowed(state: Mapping[str, Any] | None) -> bool:
    return bool(
        state
        and state.get("status") == "active"
        and state.get("type") != "profile"
        and not _pending_state(state)
    )


def _fold_operations_unchecked(operations: Iterable[Any] | Mapping[str, Any] |
                               Classification) -> FoldResult:
    """Pure full fold preserving concurrent variants and blocked deletions."""
    classified = operations if isinstance(operations, Classification) else classify_operations(operations)
    empty = hashlib.sha256(canonical_bytes([])).hexdigest()
    if classified.hard_failures:
        return FoldResult(classified, {}, {}, {}, {}, {}, {}, empty, empty)
    ops = {key: classified.operations[key] for key in classified.accepted}
    budget = _WorkBudget()
    ancestry = _ancestry(ops, budget)
    by_record: dict[str, list[str]] = {}
    record_projects: dict[str, str] = {}
    for op_id, op in ops.items():
        for mutation in op.payload["mutations"]:
            by_record.setdefault(mutation["record_id"], []).append(op_id)
            record_projects[mutation["record_id"]] = str(op.payload["project_key"])
    budget.spend(
        sum(len(ids) for ids in by_record.values()) * ancestry.word_cost
    )
    record_masks = {rid: ancestry.mask(ids) for rid, ids in by_record.items()}
    frontiers = {
        rid: _maximal(ids, ancestry, budget)
        for rid, ids in sorted(by_record.items())
    }
    if any(len(heads) > MAX_FRONTIER for heads in frontiers.values()):
        raise ProtocolError(
            "computed-frontier-limit",
            f"computed record frontier exceeds {MAX_FRONTIER} heads",
        )
    blocked: dict[str, Diagnostic] = {}
    effective_tombstones: set[tuple[str, str]] = set()
    linear_projection: dict[
        tuple[str, str], tuple[Mapping[str, Any] | None, str | None]
    ] = {}

    def observed_prior_projection(
        op: ValidatedOperation, rid: str
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        heads = _frontier_heads(op, rid)
        if not heads:
            return None, None
        if len(heads) == 1:
            cached = linear_projection.get((heads[0], rid))
            if cached is not None:
                return cached
        prior_records, _, prior_tombstones = _materialize_projection(
            ops,
            by_record,
            {rid: heads},
            blocked,
            effective_tombstones,
            ancestry,
            budget,
        )
        return prior_records.get(rid), prior_tombstones.get(rid)

    for op_id in ancestry.order:
        op = ops[op_id]
        kind, problem = op.payload["kind"], None
        observed_states: dict[str, Mapping[str, Any] | None] = {}
        observed_projections: dict[
            str, tuple[Mapping[str, Any] | None, str | None]
        ] = {}
        for mutation in op.payload["mutations"]:
            rid = mutation["record_id"]
            declared = _frontier_heads(op, rid)
            linear_parent = (
                len(op.payload["mutations"]) == 1
                and tuple(op.parents) == declared
                and (
                    not declared
                    or (
                        len(declared) == 1
                        and (declared[0], rid) in linear_projection
                    )
                )
            )
            if linear_parent:
                matches, related = True, ()
            else:
                matches, related = ancestry.frontier_matches(
                    op_id, record_masks[rid], declared, budget
                )
            if not matches:
                problem = Diagnostic("blocked-frontier-precondition", op_id,
                                     related)
                break
            expected = declared
            prior_projection = observed_prior_projection(op, rid)
            prior_state = prior_projection[0]
            observed_projections[rid] = prior_projection
            observed_states[rid] = prior_state
            post_state = _state(op, rid)
            if kind == "consume" and (
                prior_state is None or prior_state.get("delivery_state") != "pending"
            ):
                problem = Diagnostic("blocked-consume-precondition", op_id, declared)
                break
            if (
                _pending_state(prior_state)
                and post_state is not None
                and post_state.get("delivery_state") != "pending"
                and not (
                    kind == "consume"
                    and post_state.get("delivery_state") == "consumed"
                )
            ):
                problem = Diagnostic("blocked-pending-transition", op_id, declared)
                break
            if kind in DESTRUCTIVE_KINDS and kind != "force-tombstone":
                pending = tuple(
                    head for head in expected if _pending_state(_state(ops[head], rid))
                )
                if _pending_state(prior_state):
                    problem = Diagnostic("blocked-pending", op_id, pending or declared)
                    break
            if "tombstone" in mutation:
                evidence = mutation["tombstone"]
                if prior_state is None:
                    problem = Diagnostic("blocked-prior-evidence", op_id, declared)
                    break
                prior_digest = hashlib.sha256(canonical_bytes(prior_state)).hexdigest()
                if (
                    evidence["prior_digest"] != prior_digest
                    or evidence["pending"] != _pending_state(prior_state)
                ):
                    problem = Diagnostic("blocked-prior-evidence", op_id, declared)
                    break
            if kind == "restore":
                target = mutation.get("target_op_id")
                target_op = ops.get(target) if isinstance(target, str) else None
                if (
                    target_op is None
                    or not ancestry.is_ancestor(target, op_id)
                    or target not in expected
                    or target_op.payload["kind"] not in {"tombstone", "force-tombstone"}
                    or not _has_tombstone(target_op, rid)
                    or (target, rid) not in effective_tombstones
                    or target in blocked
                ):
                    problem = Diagnostic("blocked-stale-restore", op_id,
                                         (target,) if isinstance(target, str) else ())
                    break
                target_mutation = target_op.mutation_for(rid)
                assert target_mutation is not None
                restore_digest = hashlib.sha256(canonical_bytes(post_state)).hexdigest()
                if target_mutation["tombstone"]["prior_digest"] != restore_digest:
                    problem = Diagnostic("blocked-restore-content", op_id, (target,))
                    break
        if problem is None and kind == "supersede":
            source_mutation = next(
                mutation for mutation in op.payload["mutations"] if "edge" in mutation
            )
            edge = source_mutation["edge"]
            source_id, target_id = edge["source"], edge["target"]
            source_prior = observed_states.get(source_id)
            target_prior = observed_states.get(target_id)
            source_post = source_mutation["post_state"]
            target_post = op.mutation_for(target_id)["post_state"]
            expected_source = dict(source_prior or {})
            expected_source.update(
                {
                    "canonical_id": target_id,
                    "status": "superseded",
                    "superseded_by": target_id,
                    "updated": source_post["updated"],
                }
            )
            if (
                not _graph_endpoint_allowed(source_prior)
                or not _graph_endpoint_allowed(target_prior)
                or source_prior.get("canonical_id") != source_id
                or source_prior.get("superseded_by") is not None
                or target_prior.get("canonical_id") != target_id
                or target_prior.get("superseded_by") is not None
                or source_post != expected_source
                or target_post != target_prior
            ):
                problem = Diagnostic("blocked-supersession", op_id)
        if problem is None and kind in COVERAGE_SENSITIVE_KINDS:
            concurrent: set[str] = set()
            for mutation in op.payload["mutations"]:
                rid = mutation["record_id"]
                budget.spend(2 * len(by_record[rid]) * ancestry.word_cost)
                concurrent.update(
                    candidate
                    for candidate in by_record[rid]
                    if candidate != op_id
                    and not ancestry.is_ancestor(candidate, op_id)
                    and not ancestry.is_ancestor(op_id, candidate)
                )
            if concurrent:
                problem = Diagnostic(
                    "blocked-concurrency",
                    op_id,
                    _maximal(concurrent, ancestry, budget),
                )
        if problem is not None:
            blocked[op_id] = problem
        else:
            for mutation in op.payload["mutations"]:
                if "tombstone" in mutation:
                    effective_tombstones.add((op_id, mutation["record_id"]))
        mutations = op.payload["mutations"]
        if len(mutations) == 1 and kind not in {"supersede", "merge"}:
            mutation = mutations[0]
            rid = mutation["record_id"]
            heads = _frontier_heads(op, rid)
            cacheable = (
                tuple(op.parents) == heads
                and (
                    not heads
                    or (
                        len(heads) == 1
                        and (heads[0], rid) in linear_projection
                    )
                )
            )
            if problem is None:
                if "tombstone" in mutation:
                    projection = (None, op_id)
                else:
                    projection = (_state(op, rid), None)
                linear_projection[(op_id, rid)] = projection
            elif cacheable and rid in observed_projections:
                projection = observed_projections[rid]
                linear_projection[(op_id, rid)] = projection
    graph: dict[str, str] = {}
    edge_ops = [
        op for op in ops.values() if op.payload["kind"] in {"supersede", "merge"}
    ]
    prior_state_cache: dict[tuple[str, str], Mapping[str, Any] | None] = {}

    def prior_state(op: ValidatedOperation, rid: str) -> Mapping[str, Any] | None:
        cache_key = (op.op_id, rid)
        if cache_key not in prior_state_cache:
            prior_records, _, _ = _materialize_projection(
                ops,
                by_record,
                {rid: _frontier_heads(op, rid)},
                blocked,
                effective_tombstones,
                ancestry,
                budget,
            )
            prior_state_cache[cache_key] = prior_records.get(rid)
        return prior_state_cache[cache_key]

    for op in sorted(edge_ops, key=lambda item: item.key, reverse=True):
        if op.op_id in blocked:
            continue
        edges_by_key = {
            (m["edge"]["source"], m["edge"]["target"], m["edge"]["scope"]): m["edge"]
            for m in op.payload["mutations"]
            if "edge" in m
        }
        edges = [edges_by_key[key] for key in sorted(edges_by_key)]
        trial, valid = dict(graph), len({edge["scope"] for edge in edges}) == 1
        for edge in edges:
            source, target = edge["source"], edge["target"]
            left, right = prior_state(op, source), prior_state(op, target)
            if (not _graph_endpoint_allowed(left) or
                    not _graph_endpoint_allowed(right) or
                    record_projects.get(source) != op.payload["project_key"] or
                    record_projects.get(target) != op.payload["project_key"] or
                    source in trial or _cycle(trial, source, target)):
                valid = False
                break
            trial[source] = target
        if valid:
            graph = trial
        else:
            blocked[op.op_id] = Diagnostic("blocked-supersession", op.op_id)
    effective_tombstones = {
        (op_id, mutation["record_id"])
        for op_id, op in ops.items()
        if op_id not in blocked
        for mutation in op.payload["mutations"]
        if "tombstone" in mutation
    }
    records: dict[str, Mapping[str, Any]] = {}
    conflicts: dict[str, Conflict] = {}
    tombstones: dict[str, str] = {}
    remaining_frontiers: dict[str, tuple[str, ...]] = {}
    for rid, heads in frontiers.items():
        cached = (
            linear_projection.get((heads[0], rid)) if len(heads) == 1 else None
        )
        if cached is None:
            remaining_frontiers[rid] = heads
        elif cached[0] is not None:
            records[rid] = cached[0]
        elif cached[1] is not None:
            tombstones[rid] = cached[1]
    if remaining_frontiers:
        remaining_records, remaining_conflicts, remaining_tombstones = (
            _materialize_projection(
                ops,
                by_record,
                remaining_frontiers,
                blocked,
                effective_tombstones,
                ancestry,
                budget,
            )
        )
        records.update(remaining_records)
        conflicts.update(remaining_conflicts)
        tombstones.update(remaining_tombstones)
    accepted_digest = _canonical_string_array_digest(ops)
    materialized = {
        "frontiers": {key: list(value) for key, value in sorted(frontiers.items())},
        "records": {key: records[key] for key in sorted(records)},
        "conflicts": {key: conflicts[key].as_dict() for key in sorted(conflicts)},
        "blocked": {key: blocked[key].code for key in sorted(blocked)},
        "tombstones": dict(sorted(tombstones.items())),
        "supersession_graph": dict(sorted(graph.items())),
    }
    return FoldResult(classified, frontiers, records, conflicts, blocked, tombstones,
                      graph, accepted_digest,
                      hashlib.sha256(_canonical_unbounded(materialized)).hexdigest())


def fold_operations(operations: Iterable[Any] | Mapping[str, Any] |
                    Classification) -> FoldResult:
    """Pure bounded full fold preserving conflicts and blocked deletions."""
    classified = (
        operations if isinstance(operations, Classification)
        else classify_operations(operations)
    )
    empty = hashlib.sha256(canonical_bytes([])).hexdigest()
    if classified.hard_failures:
        return FoldResult(classified, {}, {}, {}, {}, {}, {}, empty, empty)
    try:
        return _fold_operations_unchecked(classified)
    except ProtocolError as error:
        if error.code == "fold-work-limit":
            raise
        exc = error
    failed = Classification(
        classified.operations,
        (),
        classified.deferred,
        classified.quarantined,
        (Diagnostic(exc.code, exc.op_id),),
    )
    return FoldResult(failed, {}, {}, {}, {}, {}, {}, empty, empty)


def resolved_blocked_by(
    result: Any, *, work_limit: int = MAX_RESOLUTION_WORK
) -> dict[str, str]:
    """Map blocked ops to final explicit descendants using one DAG index."""
    classification = result.classification
    if getattr(classification, "hard_failures", ()):
        return {}
    all_operations = classification.operations
    accepted_ids = tuple(getattr(result, "accepted", tuple(all_operations)))
    operations = {
        op_id: all_operations[op_id]
        for op_id in accepted_ids
        if op_id in all_operations
    }
    blocked_ids = set(result.blocked) & set(operations)
    if not blocked_ids:
        return {}
    budget = _WorkBudget(work_limit)
    try:
        ancestry = _ancestry(operations, budget)
    except ProtocolError as error:
        if error.code == "fold-work-limit":
            return {}
        raise
    resolved: dict[str, str] = {}
    for blocked_op_id in sorted(blocked_ids):
        blocked_op = operations[blocked_op_id]
        record_ids = {
            mutation["record_id"] for mutation in blocked_op.payload["mutations"]
        }
        head_sets = [result.frontiers.get(rid, ()) for rid in record_ids]
        if not head_sets or any(len(heads) != 1 for heads in head_sets):
            continue
        candidate_ids = {heads[0] for heads in head_sets}
        if len(candidate_ids) != 1:
            continue
        candidate_id = next(iter(candidate_ids))
        if candidate_id in blocked_ids or candidate_id not in operations:
            continue
        candidate_records = {
            mutation["record_id"]
            for mutation in operations[candidate_id].payload["mutations"]
        }
        if not record_ids <= candidate_records:
            continue
        try:
            descends = ancestry.is_ancestor(blocked_op_id, candidate_id)
        except ProtocolError as error:
            if error.code == "fold-work-limit":
                break
            raise
        if descends:
            resolved[blocked_op_id] = candidate_id
    return resolved


__all__ = [
    "Classification", "Conflict", "Diagnostic", "Disposition", "FoldResult",
    "KNOWN_KINDS", "MAX_CLASSIFY_BYTES", "MAX_CLASSIFY_OPERATIONS",
    "MAX_HARD_DIAGNOSTICS",
    "MAX_ENVELOPE_BYTES", "MAX_FOLD_OPERATIONS", "MAX_FOLD_WORK",
    "MAX_RESOLUTION_WORK",
    "PROTOCOL_MAJOR", "ProtocolError",
    "SUPPORTED_SCHEMA_MINORS", "ValidatedOperation", "build_operation",
    "canonical_bytes", "canonical_json_bytes", "canonical_loads",
    "classify_operations", "fold_operations", "operation_path",
    "parse_canonical_json", "resolved_blocked_by", "validate_operation",
]
