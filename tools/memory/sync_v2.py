#!/usr/bin/env python3
"""Local SQLite state for immutable protocol-v2 memory operations.

This module deliberately owns no transaction boundary.  Semantic writers open
one ``BEGIN IMMEDIATE`` transaction, make their record/graveyard changes, and
call the helpers below before committing once.  Remote transport and the pure
fold live in sibling modules; this file only provides the durable local ledger
and the fail-closed bootstrap policy they share.

The implementation is stdlib-only and never opens the live memory store by
itself.  Every database operation uses the caller-provided connection.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import importlib
import json
import re
import secrets
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence


PROTOCOL_MAJOR = 2
MAX_COUNTER = (1 << 64) - 1
OUTBOX_STATES = ("queued", "rendered", "committed", "confirmed")
STATUS_VALUES = {
    "not-configured",
    "local-only",
    "queued-offline",
    "fetched",
    "folded",
    "conflict",
    "quarantined",
    "push-retry-exhausted",
    "remote-confirmed",
    "hard-failure",
}
_HEX_32 = re.compile(r"^[0-9a-f]{32,}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_INSTALLATION_FINGERPRINT = re.compile(r"^[0-9a-f]{32,128}$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
_TRUSTED_BOOTSTRAP_TOKEN = object()


class SyncError(RuntimeError):
    """Base class for durable sync-state failures."""


class SyncInvariantError(SyncError):
    """Raised when durable state would violate the v2 protocol."""


class RemoteSafetyError(SyncError):
    """Raised before remote I/O when bootstrap/fence proof is incomplete."""


class TrustedBootstrapEvidence(Mapping[str, Any]):
    """Opaque, read-only bootstrap evidence issued from a checked database.

    Plain mappings are intentionally not accepted by :func:`remote_policy`:
    booleans supplied by a caller are not migration/fence proof.  Tests and
    non-database adapters may carry checked evidence through this type, but can
    obtain it only from :func:`trusted_bootstrap_evidence`.
    """

    __slots__ = ("_state", "_token")

    def __init__(self, state: Mapping[str, Any], token: object | None = None) -> None:
        if token is not _TRUSTED_BOOTSTRAP_TOKEN:
            raise TypeError("trusted bootstrap evidence must be database-issued")
        self._state = dict(state)
        self._token = token

    def __getitem__(self, key: str) -> Any:
        return self._state[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._state)

    def __len__(self) -> int:
        return len(self._state)

    def _is_trusted(self) -> bool:
        return self._token is _TRUSTED_BOOTSTRAP_TOKEN


_SCHEMA = (
    f"""
    CREATE TABLE IF NOT EXISTS sync_replica (
        replica_id TEXT PRIMARY KEY,
        counter TEXT NOT NULL DEFAULT '0',
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        installation_fingerprint TEXT,
        predecessor_replica_id TEXT,
        activated_at TEXT NOT NULL DEFAULT ({_NOW}),
        retired_at TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_objects (
        op_id TEXT PRIMARY KEY,
        replica_id TEXT NOT NULL,
        counter TEXT NOT NULL,
        project_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        object_path TEXT NOT NULL UNIQUE,
        payload_bytes BLOB NOT NULL,
        classification TEXT NOT NULL DEFAULT 'local',
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        UNIQUE (replica_id, counter)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_parents (
        op_id TEXT NOT NULL,
        parent_op_id TEXT NOT NULL,
        parent_ordinal INTEGER NOT NULL,
        PRIMARY KEY (op_id, parent_op_id),
        UNIQUE (op_id, parent_ordinal),
        FOREIGN KEY (op_id) REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_outbox (
        op_id TEXT PRIMARY KEY,
        payload_bytes BLOB NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued'
            CHECK (state IN ('queued','rendered','committed','confirmed')),
        transition_seq INTEGER NOT NULL DEFAULT 0,
        queued_at TEXT NOT NULL DEFAULT ({_NOW}),
        state_at TEXT NOT NULL DEFAULT ({_NOW}),
        rendered_path TEXT,
        local_commit TEXT,
        confirmed_remote_tip TEXT,
        confirmation_fetched_at TEXT,
        evidence_json TEXT,
        FOREIGN KEY (op_id) REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_applied (
        op_id TEXT PRIMARY KEY,
        result TEXT NOT NULL,
        diagnostic_id TEXT,
        applied_at TEXT NOT NULL DEFAULT ({_NOW}),
        FOREIGN KEY (op_id) REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_frontier (
        project_key TEXT NOT NULL,
        record_id TEXT NOT NULL,
        op_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'local',
        updated_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (project_key, record_id, op_id),
        FOREIGN KEY (op_id) REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_conflicts (
        project_key TEXT NOT NULL,
        record_id TEXT NOT NULL,
        op_id TEXT NOT NULL,
        diagnostic_id TEXT NOT NULL,
        provisional INTEGER NOT NULL DEFAULT 0 CHECK (provisional IN (0, 1)),
        variant_bytes BLOB,
        resolved_by TEXT,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (project_key, record_id, op_id),
        FOREIGN KEY (op_id) REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_peer_state (
        peer_id TEXT PRIMARY KEY,
        remote_ref TEXT NOT NULL,
        fetched_tip TEXT,
        folded_tip TEXT,
        last_confirmed_tip TEXT,
        object_set_digest TEXT,
        materialized_digest TEXT,
        status TEXT,
        fetched_at TEXT,
        folded_at TEXT,
        confirmed_at TEXT,
        updated_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_quarantine (
        op_id TEXT PRIMARY KEY,
        classification TEXT NOT NULL,
        diagnostic_id TEXT NOT NULL,
        detail_code TEXT,
        payload_bytes BLOB,
        cleared_by TEXT,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_epoch (
        epoch_id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'preparing'
            CHECK (state IN ('preparing','seeding','sealed','active','retired','rollback')),
        seed_mode TEXT NOT NULL DEFAULT 'none'
            CHECK (seed_mode IN ('none','fresh','snapshot')),
        seed_sealed INTEGER NOT NULL DEFAULT 0 CHECK (seed_sealed IN (0, 1)),
        old_writer_fence_active INTEGER NOT NULL DEFAULT 0
            CHECK (old_writer_fence_active IN (0, 1)),
        v2_only INTEGER NOT NULL DEFAULT 0 CHECK (v2_only IN (0, 1)),
        current INTEGER NOT NULL DEFAULT 0 CHECK (current IN (0, 1)),
        roster_json TEXT,
        no_tail_digest TEXT,
        accepted_set_digest TEXT,
        materialized_digest TEXT,
        fence_proof TEXT,
        created_at TEXT NOT NULL DEFAULT ({_NOW}),
        sealed_at TEXT,
        fence_activated_at TEXT,
        retired_at TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_transactional_graveyard (
        destructive_op_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        action TEXT NOT NULL,
        prior_state_bytes BLOB NOT NULL,
        evidence_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (destructive_op_id, record_id),
        FOREIGN KEY (destructive_op_id)
            REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_graveyard (
        destructive_op_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        tombstone_bytes BLOB NOT NULL,
        effective INTEGER NOT NULL DEFAULT 0 CHECK (effective IN (0, 1)),
        restored_by TEXT,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (destructive_op_id, record_id),
        FOREIGN KEY (destructive_op_id)
            REFERENCES sync_objects(op_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS sync_migration_one_current
    ON sync_migration_epoch(current) WHERE current = 1
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS sync_replica_one_active
    ON sync_replica(active) WHERE active = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS sync_outbox_state_idx
    ON sync_outbox(state, queued_at, op_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS sync_frontier_record_idx
    ON sync_frontier(project_key, record_id, op_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS sync_parents_parent_idx
    ON sync_parents(parent_op_id, op_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS sync_conflicts_open_idx
    ON sync_conflicts(resolved_by, project_key, record_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS sync_quarantine_open_idx
    ON sync_quarantine(cleared_by, op_id)
    """,
)


def ensure_sync_schema(connection: sqlite3.Connection) -> None:
    """Create the additive v2 tables without committing the caller's work.

    ``executescript`` is intentionally avoided because it can force an implicit
    commit in Python's sqlite3 wrapper.  This helper is therefore safe both at
    database bootstrap and inside an already-open migration transaction.
    """

    for statement in _SCHEMA:
        connection.execute(statement)
    replica_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(sync_replica)")
    }
    if "installation_fingerprint" not in replica_columns:
        connection.execute(
            "ALTER TABLE sync_replica ADD COLUMN installation_fingerprint TEXT"
        )


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise SyncInvariantError(
            "sync mutation requires a caller-owned BEGIN IMMEDIATE transaction"
        )


def _validate_replica_id(replica_id: str) -> str:
    if not isinstance(replica_id, str) or not _HEX_32.fullmatch(replica_id):
        raise SyncInvariantError(
            "replica_id must be lowercase hexadecimal with at least 128 bits"
        )
    return replica_id


def _validate_installation_fingerprint(fingerprint: str) -> str:
    if (
        not isinstance(fingerprint, str)
        or not _INSTALLATION_FINGERPRINT.fullmatch(fingerprint)
    ):
        raise SyncInvariantError(
            "installation fingerprint must be 128..512 bits of lowercase hex"
        )
    return fingerprint


def _verify_or_bind_installation(
    connection: sqlite3.Connection,
    replica_id: str,
    stored_fingerprint: str | None,
    installation_fingerprint: str | None,
) -> None:
    if installation_fingerprint is None:
        return
    fingerprint = _validate_installation_fingerprint(installation_fingerprint)
    if stored_fingerprint is None:
        connection.execute(
            "UPDATE sync_replica SET installation_fingerprint=? "
            "WHERE replica_id=? AND installation_fingerprint IS NULL",
            (fingerprint, replica_id),
        )
        return
    if stored_fingerprint != fingerprint:
        raise SyncInvariantError(
            "copied replica state detected: installation fingerprint changed; "
            "rotate_replica_identity is required before writes"
        )


def _counter(value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncInvariantError("operation counter must be an integer")
    lower = 0 if allow_zero else 1
    if value < lower or value > MAX_COUNTER:
        raise SyncInvariantError("operation counter is outside unsigned 64-bit range")
    return value


def allocate_counter(
    connection: sqlite3.Connection,
    replica_id: str,
    *,
    installation_fingerprint: str | None = None,
) -> int:
    """Allocate the next replica counter inside the caller's transaction."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    replica_id = _validate_replica_id(replica_id)
    row = connection.execute(
        "SELECT counter, active, installation_fingerprint "
        "FROM sync_replica WHERE replica_id=?",
        (replica_id,),
    ).fetchone()
    current = int(row[0]) if row is not None else 0
    _counter(current, allow_zero=True)
    if current == MAX_COUNTER:
        raise SyncInvariantError("replica counter exhausted unsigned 64-bit space")
    allocated = current + 1
    if row is None:
        active = connection.execute(
            "SELECT replica_id FROM sync_replica WHERE active=1"
        ).fetchone()
        if active is not None:
            raise SyncInvariantError(
                f"active replica is {active[0]}; rotate before allocating another identity"
            )
        connection.execute(
            "INSERT INTO sync_replica("
            "replica_id, counter, installation_fingerprint"
            ") VALUES (?, ?, ?)",
            (
                replica_id,
                str(allocated),
                _validate_installation_fingerprint(installation_fingerprint)
                if installation_fingerprint is not None
                else None,
            ),
        )
    else:
        if not bool(row[1]):
            raise SyncInvariantError("cannot allocate a counter for a retired replica")
        _verify_or_bind_installation(
            connection, replica_id, row[2], installation_fingerprint
        )
        connection.execute(
            "UPDATE sync_replica SET counter=? WHERE replica_id=? AND counter=?",
            (str(allocated), replica_id, str(current)),
        )
    return allocated


def ensure_replica_identity(
    connection: sqlite3.Connection,
    replica_id: str | None = None,
    *,
    installation_fingerprint: str | None = None,
) -> str:
    """Return/create a local replica identity without committing it."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if replica_id is not None:
        replica_id = _validate_replica_id(replica_id)
        row = connection.execute(
            "SELECT active, installation_fingerprint FROM sync_replica "
            "WHERE replica_id=?",
            (replica_id,),
        ).fetchone()
        if row is not None:
            if not bool(row[0]):
                raise SyncInvariantError("retired replica cannot be reactivated in place")
            _verify_or_bind_installation(
                connection, replica_id, row[1], installation_fingerprint
            )
            return replica_id
        active = connection.execute(
            "SELECT replica_id FROM sync_replica WHERE active=1"
        ).fetchone()
        if active is not None:
            raise SyncInvariantError(
                f"active replica is {active[0]}; explicit rotation is required"
            )
        connection.execute(
            "INSERT INTO sync_replica("
            "replica_id, counter, installation_fingerprint"
            ") VALUES (?, '0', ?)",
            (
                replica_id,
                _validate_installation_fingerprint(installation_fingerprint)
                if installation_fingerprint is not None
                else None,
            ),
        )
        return replica_id
    rows = connection.execute(
        "SELECT replica_id, installation_fingerprint FROM sync_replica "
        "WHERE active=1 ORDER BY activated_at, replica_id"
    ).fetchall()
    if len(rows) == 1:
        _verify_or_bind_installation(
            connection, str(rows[0][0]), rows[0][1], installation_fingerprint
        )
        return str(rows[0][0])
    if len(rows) > 1:
        raise SyncInvariantError("multiple active local replica identities")
    replica_id = secrets.token_hex(16)
    connection.execute(
        "INSERT INTO sync_replica("
        "replica_id, counter, installation_fingerprint"
        ") VALUES (?, '0', ?)",
        (
            replica_id,
            _validate_installation_fingerprint(installation_fingerprint)
            if installation_fingerprint is not None
            else None,
        ),
    )
    return replica_id


def rotate_replica_identity(
    connection: sqlite3.Connection,
    predecessor_replica_id: str,
    new_replica_id: str | None = None,
    *,
    installation_fingerprint: str | None = None,
) -> str:
    """Create a new activation boundary while preserving predecessor history."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    predecessor_replica_id = _validate_replica_id(predecessor_replica_id)
    predecessor = connection.execute(
        "SELECT active FROM sync_replica WHERE replica_id=?", (predecessor_replica_id,)
    ).fetchone()
    if predecessor is None:
        raise SyncInvariantError("predecessor replica identity is unknown")
    if not bool(predecessor[0]):
        raise SyncInvariantError("predecessor replica is not the active identity")
    new_replica_id = _validate_replica_id(new_replica_id or secrets.token_hex(16))
    if new_replica_id == predecessor_replica_id:
        raise SyncInvariantError("replica rotation requires a new identity")
    connection.execute(
        f"UPDATE sync_replica SET active=0, retired_at={_NOW} "
        "WHERE replica_id=? AND active=1",
        (predecessor_replica_id,),
    )
    connection.execute(
        "INSERT INTO sync_replica("
        "replica_id, counter, installation_fingerprint, predecessor_replica_id"
        ") VALUES (?, '0', ?, ?)",
        (
            new_replica_id,
            _validate_installation_fingerprint(installation_fingerprint)
            if installation_fingerprint is not None
            else None,
            predecessor_replica_id,
        ),
    )
    return new_replica_id


def _protocol_module() -> Any:
    try:
        return importlib.import_module("protocol_v2")
    except ImportError as exc:
        raise SyncInvariantError("protocol_v2 is required for local operation capture") from exc


def _as_mapping(value: Any, *, name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("to_dict", "as_dict", "to_wire"):
        method = getattr(value, method_name, None)
        if callable(method):
            mapped = method()
            if isinstance(mapped, Mapping):
                return dict(mapped)
    raise SyncInvariantError(f"{name} must be a mapping")


def _canonical_payload(payload: Any) -> tuple[dict[str, Any], bytes]:
    protocol = _protocol_module()
    if isinstance(payload, (bytes, bytearray, memoryview)):
        supplied = bytes(payload)
        parser = next(
            (
                getattr(protocol, name, None)
                for name in (
                    "parse_canonical_json",
                    "canonical_loads",
                    "decode_canonical_json",
                )
                if callable(getattr(protocol, name, None))
            ),
            None,
        )
        if parser is None:
            raise SyncInvariantError("protocol_v2 has no canonical payload parser")
        payload_map = _as_mapping(parser(supplied), name="payload")
    else:
        payload_map = _as_mapping(payload, name="payload")
        supplied = b""

    encoder = next(
        (
            getattr(protocol, name, None)
            for name in (
                "canonical_json_bytes",
                "canonical_bytes",
                "canonical_dumps",
            )
            if callable(getattr(protocol, name, None))
        ),
        None,
    )
    if encoder is None:
        raise SyncInvariantError("protocol_v2 has no canonical payload encoder")
    encoded = encoder(payload_map)
    if isinstance(encoded, str):
        encoded = encoded.encode("utf-8")
    if not isinstance(encoded, bytes):
        raise SyncInvariantError("protocol_v2 canonical encoder returned non-bytes")
    if supplied and supplied != encoded:
        raise SyncInvariantError("supplied payload bytes are not exact canonical bytes")
    return payload_map, encoded


def _operation_path(protocol: Any, op_id: str) -> str:
    for name in ("operation_path", "op_path"):
        function = getattr(protocol, name, None)
        if callable(function):
            return str(function(op_id))
    return f"protocol/v2/ops/{op_id[:2]}/{op_id}.json"


def _frontiers(payload: Mapping[str, Any]) -> list[tuple[str, tuple[str, ...]]]:
    raw = payload.get("frontiers")
    if not isinstance(raw, list):
        raise SyncInvariantError("payload frontiers must be a list")
    result: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for item in raw:
        row = _as_mapping(item, name="frontier")
        record_id = row.get("record_id")
        heads = row.get("heads")
        if not isinstance(record_id, str) or not record_id or record_id in seen:
            raise SyncInvariantError("frontier record_id is empty or duplicated")
        if not isinstance(heads, list) or any(
            not isinstance(head, str) or not _HEX_64.fullmatch(head) for head in heads
        ):
            raise SyncInvariantError("frontier heads must be lowercase operation IDs")
        if heads != sorted(set(heads)):
            raise SyncInvariantError("frontier heads must be sorted and duplicate-free")
        seen.add(record_id)
        result.append((record_id, tuple(heads)))
    return result


def _parents(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("parents")
    if not isinstance(raw, list) or any(
        not isinstance(parent, str) or not _HEX_64.fullmatch(parent) for parent in raw
    ):
        raise SyncInvariantError("parents must be lowercase operation IDs")
    if raw != sorted(set(raw)):
        raise SyncInvariantError("parents must be sorted and duplicate-free")
    return tuple(raw)


def _advance_replica_counter(
    connection: sqlite3.Connection,
    replica_id: str,
    operation_counter: int,
    installation_fingerprint: str | None = None,
) -> None:
    row = connection.execute(
        "SELECT counter, active, installation_fingerprint "
        "FROM sync_replica WHERE replica_id=?",
        (replica_id,),
    ).fetchone()
    if row is None:
        active = connection.execute(
            "SELECT replica_id FROM sync_replica WHERE active=1"
        ).fetchone()
        if active is not None:
            raise SyncInvariantError(
                f"local operation replica {replica_id} is not active; "
                f"current identity is {active[0]}"
            )
        connection.execute(
            "INSERT INTO sync_replica("
            "replica_id, counter, installation_fingerprint"
            ") VALUES (?, ?, ?)",
            (
                replica_id,
                str(operation_counter),
                _validate_installation_fingerprint(installation_fingerprint)
                if installation_fingerprint is not None
                else None,
            ),
        )
        return
    if not bool(row[1]):
        raise SyncInvariantError("local operation uses a retired replica identity")
    _verify_or_bind_installation(
        connection, replica_id, row[2], installation_fingerprint
    )
    current = int(row[0])
    _counter(current, allow_zero=True)
    if operation_counter < current:
        raise SyncInvariantError(
            "local operation counter cannot move behind the active replica counter"
        )
    if operation_counter > current:
        connection.execute(
            "UPDATE sync_replica SET counter=? WHERE replica_id=?",
            (str(operation_counter), replica_id),
        )


def record_local_operation(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    installation_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Atomically add one already-authored operation to all local ledgers.

    The caller must already hold ``BEGIN IMMEDIATE``.  Exact bytes come from
    protocol_v2's canonical encoder; reserializing with stdlib JSON is never a
    fallback.  A repeated identical operation is a no-op, while a duplicate
    dot, stale frontier, or partial prior ledger fails closed.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    operation_map = _as_mapping(operation, name="operation")
    if set(operation_map) != {"op_id", "payload"}:
        raise SyncInvariantError("operation envelope must contain only op_id and payload")
    op_id = operation_map.get("op_id")
    if not isinstance(op_id, str) or not _HEX_64.fullmatch(op_id):
        raise SyncInvariantError("op_id must be lowercase SHA-256 hexadecimal")
    payload, payload_bytes = _canonical_payload(operation_map["payload"])
    computed = hashlib.sha256(payload_bytes).hexdigest()
    if computed != op_id:
        raise SyncInvariantError("op_id does not match exact canonical payload bytes")

    protocol = _protocol_module()
    validator = getattr(protocol, "validate_operation", None)
    if not callable(validator):
        raise SyncInvariantError("protocol_v2 has no operation validator")
    validated = validator({"op_id": op_id, "payload": payload})
    if getattr(validated, "supported", None) is not True:
        reason = getattr(validated, "unsupported_reason", None) or "unsupported"
        raise SyncInvariantError(f"cannot author unsupported local operation: {reason}")

    if payload.get("protocol_major") != PROTOCOL_MAJOR:
        raise SyncInvariantError("local operation must use protocol_major=2")
    replica_id = _validate_replica_id(payload.get("replica_id"))
    operation_counter = _counter(payload.get("counter"))
    project_key = payload.get("project_key")
    kind = payload.get("kind")
    if not isinstance(project_key, str) or not project_key:
        raise SyncInvariantError("project_key must be a non-empty string")
    if not isinstance(kind, str) or not kind:
        raise SyncInvariantError("kind must be a non-empty string")
    parents = _parents(payload)
    frontiers = _frontiers(payload)
    object_path = _operation_path(protocol, op_id)

    existing = connection.execute(
        "SELECT replica_id, counter, project_key, kind, object_path, payload_bytes "
        "FROM sync_objects WHERE op_id=?",
        (op_id,),
    ).fetchone()
    if existing is not None:
        expected = (
            replica_id,
            str(operation_counter),
            project_key,
            kind,
            object_path,
            payload_bytes,
        )
        actual = (*existing[:5], bytes(existing[5]))
        if actual != expected:
            raise SyncInvariantError("same op_id already exists with different durable bytes")
        outbox = connection.execute(
            "SELECT state, payload_bytes FROM sync_outbox WHERE op_id=?", (op_id,)
        ).fetchone()
        applied = connection.execute(
            "SELECT result FROM sync_applied WHERE op_id=?", (op_id,)
        ).fetchone()
        if (
            outbox is None
            or bytes(outbox[1]) != payload_bytes
            or applied is None
            or not (
                applied[0] in {"local", "folded"}
                or str(applied[0]).startswith("blocked")
            )
        ):
            raise SyncInvariantError("existing local operation has a partial ledger")
        return {"idempotent": True, "op_id": op_id, "state": str(outbox[0])}

    duplicate_dot = connection.execute(
        "SELECT op_id FROM sync_objects WHERE replica_id=? AND counter=?",
        (replica_id, str(operation_counter)),
    ).fetchone()
    if duplicate_dot is not None:
        raise SyncInvariantError(
            f"replica dot is already bound to operation {duplicate_dot[0]}"
        )

    for record_id, declared_heads in frontiers:
        actual_heads = tuple(
            row[0]
            for row in connection.execute(
                "SELECT op_id FROM sync_frontier "
                "WHERE project_key=? AND record_id=? ORDER BY op_id",
                (project_key, record_id),
            )
        )
        if actual_heads != declared_heads:
            raise SyncInvariantError(
                f"stale local frontier for record {record_id}: exact heads required"
            )

    _advance_replica_counter(
        connection, replica_id, operation_counter, installation_fingerprint
    )
    connection.execute(
        "INSERT INTO sync_objects("
        "op_id, replica_id, counter, project_key, kind, object_path, payload_bytes"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            op_id,
            replica_id,
            str(operation_counter),
            project_key,
            kind,
            object_path,
            sqlite3.Binary(payload_bytes),
        ),
    )
    connection.executemany(
        "INSERT INTO sync_parents(op_id, parent_op_id, parent_ordinal) "
        "VALUES (?, ?, ?)",
        ((op_id, parent, ordinal) for ordinal, parent in enumerate(parents)),
    )
    for record_id, declared_heads in frontiers:
        if declared_heads:
            connection.executemany(
                "DELETE FROM sync_frontier "
                "WHERE project_key=? AND record_id=? AND op_id=?",
                ((project_key, record_id, head) for head in declared_heads),
            )
        connection.execute(
            "INSERT INTO sync_frontier(project_key, record_id, op_id, source) "
            "VALUES (?, ?, ?, 'local')",
            (project_key, record_id, op_id),
        )
    connection.execute(
        "INSERT INTO sync_applied(op_id, result) VALUES (?, 'local')", (op_id,)
    )
    connection.execute(
        "INSERT INTO sync_outbox(op_id, payload_bytes, state) VALUES (?, ?, 'queued')",
        (op_id, sqlite3.Binary(payload_bytes)),
    )
    return {"idempotent": False, "op_id": op_id, "state": "queued"}


def transition_outbox(
    connection: sqlite3.Connection,
    op_id: str,
    state: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance one outbox row by exactly one monotonic state.

    Confirmation proof is established by the transport layer's fresh fetch;
    this ledger records that attestation but never performs remote I/O itself.
    Repeating the current state is idempotent.  Skips and regressions fail.
    """

    _require_transaction(connection)
    if not isinstance(op_id, str) or not _HEX_64.fullmatch(op_id):
        raise SyncInvariantError("invalid outbox operation ID")
    if state not in OUTBOX_STATES:
        raise SyncInvariantError(f"invalid outbox state: {state!r}")
    row = connection.execute(
        "SELECT state, confirmed_remote_tip, confirmation_fetched_at, evidence_json "
        "FROM sync_outbox WHERE op_id=?",
        (op_id,),
    ).fetchone()
    if row is None:
        raise SyncInvariantError("outbox operation is unknown")
    current = str(row[0])
    if current == state:
        if state == "confirmed":
            try:
                stored = json.loads(row[3]) if row[3] else {}
            except (TypeError, ValueError) as exc:
                raise SyncInvariantError("confirmed outbox evidence is invalid") from exc
            tip, fetched_at = _confirmation_evidence(stored)
            if row[1] != tip or row[2] != fetched_at:
                raise SyncInvariantError("confirmed outbox proof columns disagree")
        return {"idempotent": True, "op_id": op_id, "state": state}
    if current == OUTBOX_STATES[-1]:
        raise SyncInvariantError("confirmed outbox state cannot regress")
    expected = OUTBOX_STATES[OUTBOX_STATES.index(current) + 1]
    if state != expected:
        raise SyncInvariantError(
            f"outbox transition must be {current} -> {expected}, not {state}"
        )
    evidence_map = dict(evidence or {})
    if state == "rendered":
        expected_path = f"protocol/v2/ops/{op_id[:2]}/{op_id}.json"
        if (
            evidence_map.get("rendered_path") != expected_path
            or not isinstance(evidence_map.get("rendered_commit"), str)
            or not _GIT_OBJECT_ID.fullmatch(evidence_map["rendered_commit"])
        ):
            raise SyncInvariantError(
                "rendered transition requires exact path and reachable commit evidence"
            )
    elif state == "committed":
        if (
            not isinstance(evidence_map.get("local_commit"), str)
            or not _GIT_OBJECT_ID.fullmatch(evidence_map["local_commit"])
        ):
            raise SyncInvariantError(
                "committed transition requires integration commit evidence"
            )
    elif state == "confirmed":
        _confirmation_evidence(evidence_map)
    evidence_json = (
        json.dumps(
            evidence_map,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if evidence_map
        else None
    )
    rendered_path = evidence_map.get("rendered_path") if state == "rendered" else None
    local_commit = evidence_map.get("local_commit") if state == "committed" else None
    confirmed_tip = evidence_map.get("remote_tip") if state == "confirmed" else None
    fetched_at = evidence_map.get("fetched_at") if state == "confirmed" else None
    connection.execute(
        f"UPDATE sync_outbox SET state=?, transition_seq=transition_seq+1, "
        f"state_at={_NOW}, "
        "rendered_path=COALESCE(?, rendered_path), "
        "local_commit=COALESCE(?, local_commit), "
        "confirmed_remote_tip=COALESCE(?, confirmed_remote_tip), "
        "confirmation_fetched_at=COALESCE(?, confirmation_fetched_at), "
        "evidence_json=COALESCE(?, evidence_json) "
        "WHERE op_id=? AND state=?",
        (
            state,
            rendered_path,
            local_commit,
            confirmed_tip,
            fetched_at,
            evidence_json,
            op_id,
            current,
        ),
    )
    return {"idempotent": False, "op_id": op_id, "state": state}


def _confirmation_evidence(evidence: Mapping[str, Any]) -> tuple[str, str]:
    """Validate fresh authoritative-ref evidence for a confirmed transition."""

    remote_tip = evidence.get("remote_tip")
    fetched_at = evidence.get("fetched_at")
    if not isinstance(remote_tip, str) or not _GIT_OBJECT_ID.fullmatch(remote_tip):
        raise SyncInvariantError(
            "confirmed transition requires a lowercase 40/64-hex remote_tip"
        )
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise SyncInvariantError("confirmed transition requires nonempty fetched_at")
    if evidence.get("fresh_fetch") is not True:
        raise SyncInvariantError("confirmed transition requires fresh_fetch=true")
    return remote_tip, fetched_at


def record_graveyard_evidence(
    connection: sqlite3.Connection,
    destructive_op_id: str,
    record_id: str,
    action: str,
    prior_state_bytes: bytes,
    tombstone_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Append idempotent graveyard evidence before a semantic deletion."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if not _HEX_64.fullmatch(destructive_op_id or ""):
        raise SyncInvariantError("invalid destructive operation ID")
    if not record_id or not action or not isinstance(prior_state_bytes, bytes):
        raise SyncInvariantError("graveyard evidence is incomplete")
    digest = hashlib.sha256(prior_state_bytes).hexdigest()
    existing = connection.execute(
        "SELECT action, prior_state_bytes, evidence_digest "
        "FROM sync_transactional_graveyard "
        "WHERE destructive_op_id=? AND record_id=?",
        (destructive_op_id, record_id),
    ).fetchone()
    if existing is not None:
        if (existing[0], bytes(existing[1]), existing[2]) != (
            action,
            prior_state_bytes,
            digest,
        ):
            raise SyncInvariantError("graveyard retry conflicts with prior evidence")
        return {"idempotent": True, "evidence_digest": digest}
    connection.execute(
        "INSERT INTO sync_transactional_graveyard("
        "destructive_op_id, record_id, action, prior_state_bytes, evidence_digest"
        ") VALUES (?, ?, ?, ?, ?)",
        (
            destructive_op_id,
            record_id,
            action,
            sqlite3.Binary(prior_state_bytes),
            digest,
        ),
    )
    if tombstone_bytes is not None:
        connection.execute(
            "INSERT INTO sync_graveyard("
            "destructive_op_id, record_id, tombstone_bytes"
            ") VALUES (?, ?, ?)",
            (destructive_op_id, record_id, sqlite3.Binary(tombstone_bytes)),
        )
    return {"idempotent": False, "evidence_digest": digest}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _nonempty_table(connection: sqlite3.Connection, table: str) -> bool:
    if not _IDENTIFIER.fullmatch(table):
        raise SyncInvariantError(f"unsafe semantic table name: {table!r}")
    return connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None


def _semantic_state_covered(connection: sqlite3.Connection, names: set[str]) -> bool:
    """Prove that every non-local record field equals the immutable full fold."""

    if "records" not in names:
        return True
    protocol = _protocol_module()
    required_fields = set(protocol.RECORD_STATE_FIELDS)
    columns = [str(row[1]) for row in connection.execute("PRAGMA table_info(records)")]
    if set(columns) != required_fields:
        return not _nonempty_table(connection, "records")
    try:
        operations = [
            {"op_id": str(op_id), "payload": protocol.canonical_loads(bytes(payload))}
            for op_id, payload in connection.execute(
                "SELECT op_id,payload_bytes FROM sync_objects ORDER BY op_id"
            )
        ]
        folded = protocol.fold_operations(operations)
        if folded.classification.hard_failures:
            return False
        expected = {
            record_id: dict(state)
            for record_id, state in folded.records.items()
            if record_id not in folded.conflicts
        }
        actual: dict[str, dict[str, Any]] = {}
        select_columns = ",".join(f'"{column}"' for column in columns)
        for row in connection.execute(f"SELECT {select_columns} FROM records"):
            state = dict(zip(columns, row))
            for key in protocol.RECORD_LIST_FIELDS:
                value = state[key]
                state[key] = sorted(
                    set(json.loads(value) if isinstance(value, str) else (value or [])),
                    key=protocol.canonical_bytes,
                )
            actual[str(state["id"])] = state
        if set(actual) != set(expected):
            return False
        for record_id, live in actual.items():
            wire = expected[record_id]
            # Access recency is explicitly server-local and authors no operation.
            live["last_accessed"] = wire.get("last_accessed")
            if protocol.canonical_bytes(live) != protocol.canonical_bytes(wire):
                return False
        return True
    except (KeyError, TypeError, ValueError, sqlite3.Error, SyncError):
        return False


def bootstrap_state(
    connection: sqlite3.Connection,
    semantic_tables: Sequence[str] = ("records",),
) -> dict[str, Any]:
    """Return bounded proof state used before any remote exchange."""

    names = _table_names(connection)
    required = {
        "sync_replica",
        "sync_objects",
        "sync_outbox",
        "sync_applied",
        "sync_frontier",
        "sync_conflicts",
        "sync_peer_state",
        "sync_quarantine",
        "sync_migration_epoch",
        "sync_parents",
        "sync_transactional_graveyard",
    }
    schema_ready = required <= names
    legacy_nonempty = any(
        table in names and _nonempty_table(connection, table)
        for table in semantic_tables
    )
    object_count = (
        int(connection.execute("SELECT COUNT(*) FROM sync_objects").fetchone()[0])
        if "sync_objects" in names
        else 0
    )
    epoch = None
    if "sync_migration_epoch" in names:
        epoch = connection.execute(
            "SELECT epoch_id, state, seed_mode, seed_sealed, "
            "old_writer_fence_active, v2_only "
            "FROM sync_migration_epoch WHERE current=1"
        ).fetchone()
    fresh_candidate = schema_ready and not legacy_nonempty and object_count == 0
    if epoch is None:
        result = {
            "epoch_id": None,
            "epoch_state": None,
            "seed_mode": "none",
            "seed_sealed": False,
            "old_writer_fence_active": False,
            "v2_only": False,
        }
    else:
        result = {
            "epoch_id": str(epoch[0]),
            "epoch_state": str(epoch[1]),
            "seed_mode": str(epoch[2]),
            "seed_sealed": bool(epoch[3]),
            "old_writer_fence_active": bool(epoch[4]),
            "v2_only": bool(epoch[5]),
        }
    seed_ready = result["seed_sealed"] and result["seed_mode"] in {
        "fresh",
        "snapshot",
    }
    fence_ready = result["old_writer_fence_active"] and result["v2_only"]
    semantic_state_covered = schema_ready and _semantic_state_covered(connection, names)
    result.update(
        {
            "schema_ready": schema_ready,
            "legacy_nonempty": legacy_nonempty,
            "object_count": object_count,
            "fresh_candidate": fresh_candidate,
            "seed_ready": seed_ready,
            "fence_ready": fence_ready,
            "semantic_state_covered": semantic_state_covered,
            "remote_allowed": bool(
                schema_ready
                and seed_ready
                and fence_ready
                and semantic_state_covered
                and result["epoch_state"] == "active"
            ),
        }
    )
    return result


def initialize_fresh_v2_epoch(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    proof: str,
    semantic_tables: Sequence[str] = ("records",),
) -> dict[str, Any]:
    """Seal a provably empty store as fresh; leave its writer fence inactive."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if not epoch_id or not proof:
        raise RemoteSafetyError("fresh-store epoch and proof are required")
    state = bootstrap_state(connection, semantic_tables)
    if not state["fresh_candidate"] or state["legacy_nonempty"]:
        raise RemoteSafetyError("store is not a provably fresh v2 store")
    if state["epoch_id"] not in (None, epoch_id):
        raise RemoteSafetyError("another migration epoch is already current")
    connection.execute(
        "INSERT INTO sync_migration_epoch("
        "epoch_id, state, seed_mode, seed_sealed, current, no_tail_digest"
        ") VALUES (?, 'sealed', 'fresh', 1, 1, ?) "
        "ON CONFLICT(epoch_id) DO UPDATE SET "
        "state='sealed', seed_mode='fresh', seed_sealed=1, "
        "current=1, no_tail_digest=excluded.no_tail_digest, "
        f"sealed_at={_NOW}",
        (epoch_id, proof),
    )
    return bootstrap_state(connection, semantic_tables)


def record_seed_epoch(
    connection: sqlite3.Connection,
    epoch_id: str,
    roster: Iterable[str],
) -> dict[str, Any]:
    """Prepare an all-replica seed epoch without sealing or enabling it."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    replicas = sorted(set(roster))
    if not epoch_id or not replicas:
        raise RemoteSafetyError("seed epoch requires an ID and nonempty roster")
    for replica_id in replicas:
        _validate_replica_id(replica_id)
    roster_json = json.dumps(replicas, separators=(",", ":"))
    existing = connection.execute(
        "SELECT state, seed_mode, seed_sealed, roster_json "
        "FROM sync_migration_epoch WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if existing is not None:
        if existing == ("seeding", "snapshot", 0, roster_json):
            return bootstrap_state(connection)
        raise RemoteSafetyError("seed epoch already exists in a different state")
    if connection.execute(
        "SELECT 1 FROM sync_migration_epoch WHERE current=1 AND epoch_id<>?",
        (epoch_id,),
    ).fetchone() is not None:
        raise RemoteSafetyError("another migration epoch is already current")
    connection.execute(
        "INSERT INTO sync_migration_epoch("
        "epoch_id, state, seed_mode, seed_sealed, current, roster_json"
        ") VALUES (?, 'seeding', 'snapshot', 0, 1, ?) "
        "ON CONFLICT(epoch_id) DO UPDATE SET "
        "state='seeding', seed_mode='snapshot', current=1, "
        "roster_json=excluded.roster_json",
        (epoch_id, roster_json),
    )
    return bootstrap_state(connection)


def seal_seed_epoch(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    no_tail_digest: str,
    accepted_set_digest: str,
    materialized_digest: str,
    operator_authorized: bool = False,
) -> dict[str, Any]:
    """Refuse snapshot sealing until the reviewed all-server verifier exists."""

    _require_transaction(connection)
    raise RemoteSafetyError(
        "snapshot seed sealing is unavailable in this release; "
        "use the separately reviewed all-server cutover verifier"
    )


def activate_v2_only_fence(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    fence_proof: str,
    operator_authorized: bool = False,
) -> dict[str, Any]:
    """Activate the old-writer fence after a sealed seed/fresh proof."""

    _require_transaction(connection)
    if not operator_authorized:
        raise RemoteSafetyError("v2-only fence activation requires operator authority")
    if not fence_proof:
        raise RemoteSafetyError("v2-only fence activation requires proof")
    row = connection.execute(
        "SELECT state, seed_mode, seed_sealed,old_writer_fence_active,v2_only "
        "FROM sync_migration_epoch "
        "WHERE epoch_id=? AND current=1",
        (epoch_id,),
    ).fetchone()
    if row is None or row[0] not in {"sealed", "active"} or not bool(row[2]):
        raise RemoteSafetyError("migration seed is not sealed")
    if row[0] == "active" and bool(row[3]) and bool(row[4]):
        return bootstrap_state(connection)
    if row[1] != "fresh":
        raise RemoteSafetyError(
            "snapshot fence activation is unavailable in this release"
        )
    state = bootstrap_state(connection)
    if state["legacy_nonempty"] or state["object_count"] != 0:
        raise RemoteSafetyError(
            "fresh v2 fence must be activated before the first semantic write"
        )
    connection.execute(
        f"UPDATE sync_migration_epoch SET state='active', "
        "old_writer_fence_active=1, v2_only=1, fence_proof=?, "
        f"fence_activated_at={_NOW} WHERE epoch_id=? AND current=1",
        (fence_proof, epoch_id),
    )
    return bootstrap_state(connection)


def remote_readiness(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return the checked remote safety gate without changing the store."""

    state = bootstrap_state(connection)
    if state["remote_allowed"]:
        return {**state, "allowed": True, "reason": None, "exit_code": 0}
    if not state["schema_ready"]:
        reason = "sync-schema-unavailable"
    elif state["legacy_nonempty"] and not state["seed_ready"]:
        reason = "legacy-store-seed-required"
    elif not state["seed_ready"]:
        reason = "fresh-or-sealed-seed-required"
    elif not state["fence_ready"]:
        reason = "v2-only-old-writer-fence-required"
    elif not state["semantic_state_covered"]:
        reason = "semantic-state-without-v2-objects"
    else:
        reason = "migration-epoch-not-active"
    return {**state, "allowed": False, "reason": reason, "exit_code": 2}


def require_remote_ready(connection: sqlite3.Connection) -> dict[str, Any]:
    """Fail before remote phases unless the bootstrap and fence are proved."""

    result = remote_readiness(connection)
    if not result["allowed"]:
        raise RemoteSafetyError(str(result["reason"]))
    return result


def trusted_bootstrap_evidence(
    connection: sqlite3.Connection,
) -> TrustedBootstrapEvidence:
    """Issue opaque evidence from the database-backed readiness check."""

    return TrustedBootstrapEvidence(
        remote_readiness(connection), _TRUSTED_BOOTSTRAP_TOKEN
    )


def _flag(environ: Mapping[str, str], name: str) -> tuple[bool, bool, str | None]:
    if name not in environ:
        return False, False, None
    raw = str(environ[name]).strip()
    if raw == "1":
        return True, True, None
    if raw in {"", "0"}:
        return True, False, None
    return True, False, f"invalid-{name.lower()}"


def remote_policy(
    environ: Mapping[str, str],
    connection: sqlite3.Connection | None = None,
    *,
    bootstrap: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve explicit remote intent and its independent safety gate.

    ``enabled`` means the operator requested immutable v2 exchange.  Callers
    must gate every remote phase on ``allowed``.  This split keeps the legacy
    alias observable (and warnable) without ever interpreting it as permission
    for the retired mutable dump-push behavior.
    """

    canonical_present, canonical_on, canonical_error = _flag(
        environ, "MEM_SYNC_REMOTE"
    )
    legacy_present, legacy_on, legacy_error = _flag(environ, "MEM_DUMP_PUSH")
    deprecated_alias = not canonical_present and legacy_on
    requested = canonical_on if canonical_present else legacy_on
    error = canonical_error if canonical_present else legacy_error
    warning = (
        "MEM_DUMP_PUSH is deprecated; it selects immutable protocol-v2 exchange "
        "and never pushes dump.jsonl"
        if deprecated_alias
        else None
    )
    if not requested:
        return {
            "allowed": False,
            "deprecated_alias": False,
            "dump_push": False,
            "enabled": False,
            "exit_code": 2 if error else 0,
            "reason": error,
            "requested": False,
            "status": "hard-failure" if error else "not-configured",
            "status_schema": 1,
            "transport": "v2",
            "warning": None,
        }

    bootstrap_error = None
    if connection is not None:
        state = remote_readiness(connection)
    elif isinstance(bootstrap, TrustedBootstrapEvidence) and bootstrap._is_trusted():
        state = dict(bootstrap)
    elif bootstrap is not None:
        state = {}
        bootstrap_error = "untrusted-bootstrap-evidence"
    else:
        state = {}
    fresh_or_seed = bool(
        state.get("seed_ready")
        or state.get("fresh_v2")
        or state.get("provably_fresh")
        or state.get("sealed_seed")
    )
    fence_ready = bool(
        state.get("fence_ready")
        or (
            state.get("old_writer_fence_active")
            and state.get("v2_only")
        )
    )
    epoch_active = state.get("epoch_state", "active") == "active"
    semantic_state_covered = bool(state.get("semantic_state_covered"))
    allowed = (
        fresh_or_seed
        and fence_ready
        and epoch_active
        and semantic_state_covered
        and not error
        and not bootstrap_error
    )
    if error:
        reason = error
    elif bootstrap_error:
        reason = bootstrap_error
    elif not fresh_or_seed:
        reason = "fresh-or-sealed-seed-required"
    elif not fence_ready:
        reason = "v2-only-old-writer-fence-required"
    elif not epoch_active:
        reason = "migration-epoch-not-active"
    elif not semantic_state_covered:
        reason = "semantic-state-without-v2-objects"
    else:
        reason = None
    return {
        "allowed": allowed,
        "deprecated_alias": deprecated_alias,
        "dump_push": False,
        "enabled": True,
        "exit_code": 0 if allowed and warning is None else (1 if allowed else 2),
        "reason": reason,
        "requested": True,
        "status": "local-only" if allowed else "hard-failure",
        "status_schema": 1,
        "transport": "v2",
        "warning": warning,
    }


def _bounded_ids(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any],
    limit: int,
    byte_limit: int = 8192,
) -> tuple[list[str], int]:
    selected: list[str] = []
    used, total = 0, 0
    for row in connection.execute(query, parameters):
        total += 1
        value = str(row[0])
        encoded = len(value.encode("utf-8"))
        if len(selected) < limit and used + encoded <= byte_limit:
            selected.append(value)
            used += encoded
    return selected, max(0, total - len(selected))


def sync_status(
    connection: sqlite3.Connection,
    *,
    policy: Mapping[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Return bounded, body-free local status for sync/doctor integration."""

    limit = max(1, min(int(limit), 8))
    names = _table_names(connection)
    required = {
        "sync_objects",
        "sync_outbox",
        "sync_applied",
        "sync_frontier",
        "sync_conflicts",
        "sync_quarantine",
        "sync_migration_epoch",
    }
    if not required <= names:
        return {
            "exit_code": 2,
            "protocol_major": PROTOCOL_MAJOR,
            "reason": "sync-schema-unavailable",
            "status": "hard-failure",
            "status_schema": 1,
        }
    policy_map = dict(policy or remote_policy({}))
    outbox = Counter(
        str(state)
        for (state,) in connection.execute(
            "SELECT state FROM sync_outbox ORDER BY state, op_id"
        )
    )
    pending_ids, pending_omitted = _bounded_ids(
        connection,
        "SELECT op_id FROM sync_outbox WHERE state<>'confirmed' "
        "ORDER BY queued_at, op_id",
        (),
        limit,
    )
    conflict_ids, conflict_omitted = _bounded_ids(
        connection,
        "SELECT DISTINCT record_id FROM sync_conflicts WHERE resolved_by IS NULL "
        "ORDER BY record_id",
        (),
        limit,
    )
    quarantine_ids, quarantine_omitted = _bounded_ids(
        connection,
        "SELECT op_id FROM sync_quarantine WHERE cleared_by IS NULL ORDER BY op_id",
        (),
        limit,
    )
    blocked_ids, blocked_omitted = _bounded_ids(
        connection,
        "SELECT op_id FROM sync_applied WHERE result LIKE 'blocked:%' ORDER BY op_id",
        (),
        limit,
    )
    deferred_ids, deferred_omitted = _bounded_ids(
        connection,
        "SELECT op_id FROM sync_objects WHERE classification LIKE 'deferred%' "
        "ORDER BY op_id",
        (),
        limit,
    )
    invalid_confirmed: list[str] = []
    for op_id, remote_tip, fetched_at, evidence_json in connection.execute(
        "SELECT op_id, confirmed_remote_tip, confirmation_fetched_at, evidence_json "
        "FROM sync_outbox WHERE state='confirmed' ORDER BY op_id"
    ):
        try:
            evidence = json.loads(evidence_json) if evidence_json else {}
            proved_tip, proved_at = _confirmation_evidence(evidence)
            if remote_tip != proved_tip or fetched_at != proved_at:
                raise SyncInvariantError("confirmed proof columns disagree")
        except (SyncInvariantError, TypeError, ValueError):
            invalid_confirmed.append(str(op_id))
    bootstrap = bootstrap_state(connection)
    peer_row = connection.execute(
        "SELECT remote_ref,fetched_tip,folded_tip,last_confirmed_tip,status,"
        "fetched_at,folded_at,confirmed_at FROM sync_peer_state "
        "WHERE peer_id='origin'"
    ).fetchone()
    peer = None if peer_row is None else {
        "remote_ref": peer_row[0],
        "fetched_tip": peer_row[1],
        "folded_tip": peer_row[2],
        "last_confirmed_tip": peer_row[3],
        "status": peer_row[4],
        "fetched_at": peer_row[5],
        "folded_at": peer_row[6],
        "confirmed_at": peer_row[7],
    }
    if invalid_confirmed:
        status, exit_code = "hard-failure", 2
        reason = "confirmed-proof-invalid"
    elif policy_map.get("enabled") and not policy_map.get("allowed"):
        status, exit_code = "hard-failure", 2
        reason = policy_map.get("reason") or "remote-safety-gate-closed"
    elif quarantine_ids:
        status, exit_code, reason = "quarantined", 1, "unsupported-operations"
    elif conflict_ids:
        status, exit_code, reason = "conflict", 1, "unresolved-conflicts"
    elif deferred_ids:
        status, exit_code, reason = "fetched", 1, "deferred-operations"
    elif blocked_ids:
        status, exit_code, reason = "fetched", 1, "blocked-operations"
    elif not policy_map.get("enabled"):
        status = "local-only" if outbox else "not-configured"
        exit_code, reason = 0, None
    elif pending_ids:
        status, exit_code, reason = "queued-offline", 1, "unconfirmed-outbox"
    elif outbox and outbox.get("confirmed", 0) == sum(outbox.values()):
        status = "remote-confirmed"
        if policy_map.get("warning"):
            exit_code, reason = 1, "deprecated-remote-alias"
        else:
            exit_code, reason = 0, None
    elif policy_map.get("enabled") and peer is not None:
        status = "remote-confirmed" if peer.get("last_confirmed_tip") else "folded"
        exit_code, reason = 0, None
    elif policy_map.get("enabled"):
        status, exit_code, reason = "local-only", 0, None
    else:
        status, exit_code, reason = "not-configured", 0, None
    return {
        "bootstrap": bootstrap,
        "blocked_ids": blocked_ids,
        "blocked_ids_omitted": blocked_omitted,
        "conflict_ids": conflict_ids,
        "conflict_ids_omitted": conflict_omitted,
        "deferred_ids": deferred_ids,
        "deferred_ids_omitted": deferred_omitted,
        "exit_code": exit_code,
        "outbox": {state: outbox.get(state, 0) for state in OUTBOX_STATES},
        "outbox_ids": pending_ids,
        "outbox_ids_omitted": pending_omitted,
        "peer": peer,
        "protocol_major": PROTOCOL_MAJOR,
        "quarantine_ids": quarantine_ids,
        "quarantine_ids_omitted": quarantine_omitted,
        "reason": reason,
        "status": status,
        "status_schema": 1,
        "invalid_confirmed_ids": invalid_confirmed[:limit],
        "invalid_confirmed_ids_omitted": max(0, len(invalid_confirmed) - limit),
    }


# Narrow compatibility spellings for sibling integration while the v2 modules
# are new.  They retain exactly the same fail-closed behavior.
initialize_schema = ensure_sync_schema
init_sync_schema = ensure_sync_schema
next_counter = allocate_counter
enqueue_local_operation = record_local_operation
capture_local_operation = record_local_operation
advance_outbox = transition_outbox
resolve_remote_policy = remote_policy
remote_mode = remote_policy


__all__ = [
    "MAX_COUNTER",
    "OUTBOX_STATES",
    "PROTOCOL_MAJOR",
    "RemoteSafetyError",
    "STATUS_VALUES",
    "SyncError",
    "SyncInvariantError",
    "TrustedBootstrapEvidence",
    "activate_v2_only_fence",
    "allocate_counter",
    "bootstrap_state",
    "ensure_replica_identity",
    "ensure_sync_schema",
    "initialize_fresh_v2_epoch",
    "record_graveyard_evidence",
    "record_local_operation",
    "record_seed_epoch",
    "remote_policy",
    "remote_readiness",
    "require_remote_ready",
    "rotate_replica_identity",
    "seal_seed_epoch",
    "sync_status",
    "trusted_bootstrap_evidence",
    "transition_outbox",
]
