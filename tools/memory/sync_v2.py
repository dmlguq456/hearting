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
from pathlib import Path
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
_TRUSTED_MIGRATION_TOKEN = object()

MIGRATION_PHASES = (
    "legacy",
    "membership-sealed",
    "capture-enabled",
    "snapshots-sealed",
    "seeds-built",
    "fence-armed",
    "barrier-held",
    "old-writers-fenced",
    "deltas-drained",
    "no-tail-proven",
    "evidence-sealed",
    "seeds-published",
    "folded",
    "equality-proven",
    "v2-only-enabled",
    "rollback-window",
    "closed",
)
WRITER_MODES = (
    "legacy-capture",
    "v2",
    "read-only-unsupported",
    "fenced",
)


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


class TrustedMigrationEvidence(Mapping[str, Any]):
    """Opaque migration proof re-issued only from checked durable rows."""

    __slots__ = ("_state", "_token")

    def __init__(self, state: Mapping[str, Any], token: object | None = None) -> None:
        if token is not _TRUSTED_MIGRATION_TOKEN:
            raise TypeError("trusted migration evidence must be database-issued")
        self._state = dict(state)
        self._token = token

    def __getitem__(self, key: str) -> Any:
        return self._state[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._state)

    def __len__(self) -> int:
        return len(self._state)

    def _is_trusted(self) -> bool:
        return self._token is _TRUSTED_MIGRATION_TOKEN


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
    CREATE TABLE IF NOT EXISTS sync_capture_clock (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        capture_seq TEXT NOT NULL DEFAULT '0'
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_state (
        epoch_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL,
        phase_seq INTEGER NOT NULL DEFAULT 0,
        current INTEGER NOT NULL DEFAULT 0 CHECK (current IN (0, 1)),
        membership_digest TEXT,
        evidence_digest TEXT,
        writer_mode TEXT NOT NULL DEFAULT 'legacy-capture'
            CHECK (writer_mode IN ('legacy-capture','v2','read-only-unsupported','fenced')),
        state_digest TEXT NOT NULL UNIQUE,
        last_receipt_digest TEXT,
        fence_capture_seq TEXT,
        equality_digest TEXT,
        rollback_bundle_digest TEXT,
        updated_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_receipts (
        epoch_id TEXT NOT NULL,
        phase_seq INTEGER NOT NULL,
        phase TEXT NOT NULL,
        expect_digest TEXT NOT NULL,
        state_digest TEXT NOT NULL,
        previous_receipt_digest TEXT,
        input_digest TEXT NOT NULL,
        membership_digest TEXT,
        evidence_digest TEXT,
        changed INTEGER NOT NULL CHECK (changed IN (0, 1)),
        receipt_bytes BLOB NOT NULL,
        receipt_digest TEXT NOT NULL UNIQUE,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, phase_seq),
        UNIQUE (epoch_id, phase)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_seals (
        epoch_id TEXT NOT NULL,
        seal_kind TEXT NOT NULL CHECK (seal_kind IN ('membership','evidence')),
        membership_digest TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        manifest_bytes BLOB NOT NULL,
        receipt_digest TEXT NOT NULL,
        sealed_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, seal_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_migration_members (
        epoch_id TEXT NOT NULL,
        replica_id TEXT NOT NULL,
        retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
        manifest_digest TEXT NOT NULL,
        retirement_digest TEXT,
        evidence_digest TEXT,
        PRIMARY KEY (epoch_id, replica_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_attestations (
        epoch_id TEXT NOT NULL,
        replica_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        payload_bytes BLOB NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, replica_id, kind)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_seed_reservations (
        epoch_id TEXT NOT NULL,
        replica_id TEXT NOT NULL,
        seed_kind TEXT NOT NULL CHECK (seed_kind IN ('snapshot','delta')),
        source_digest TEXT NOT NULL,
        membership_digest TEXT NOT NULL,
        activation_boundary TEXT NOT NULL,
        canonicalizer_version TEXT NOT NULL,
        identity_digest TEXT NOT NULL,
        counter_start TEXT,
        counter_end TEXT,
        item_count INTEGER NOT NULL,
        mapping_digest TEXT NOT NULL,
        reserved_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, replica_id, seed_kind, source_digest),
        UNIQUE (epoch_id, identity_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_migration_seed_map (
        epoch_id TEXT NOT NULL,
        replica_id TEXT NOT NULL,
        seed_kind TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        source_identity TEXT NOT NULL,
        source_ordinal INTEGER NOT NULL,
        counter TEXT NOT NULL,
        dot TEXT NOT NULL,
        op_id TEXT,
        PRIMARY KEY (epoch_id, replica_id, seed_kind, source_digest, source_identity),
        UNIQUE (replica_id, counter),
        UNIQUE (epoch_id, replica_id, seed_kind, source_digest, source_ordinal)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_capture_bindings (
        epoch_id TEXT NOT NULL,
        capture_seq TEXT NOT NULL,
        captured_op_id TEXT NOT NULL,
        seed_op_id TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, capture_seq),
        UNIQUE (epoch_id, captured_op_id),
        UNIQUE (epoch_id, seed_op_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_artifacts (
        epoch_id TEXT NOT NULL,
        artifact_kind TEXT NOT NULL,
        replica_id TEXT NOT NULL DEFAULT '',
        manifest_digest TEXT NOT NULL,
        inventory_digest TEXT,
        local_path TEXT NOT NULL,
        receipt_digest TEXT,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, artifact_kind, replica_id),
        UNIQUE (epoch_id, manifest_digest),
        UNIQUE (epoch_id, local_path)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_fold (
        epoch_id TEXT PRIMARY KEY,
        evidence_digest TEXT NOT NULL,
        accepted_set_digest TEXT NOT NULL,
        operation_tree_digest TEXT NOT NULL,
        materialized_digest TEXT NOT NULL,
        reducer_version TEXT NOT NULL,
        fold_digest TEXT NOT NULL UNIQUE,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_equality (
        epoch_id TEXT PRIMARY KEY,
        evidence_digest TEXT NOT NULL,
        report_set_digest TEXT NOT NULL,
        accepted_set_digest TEXT NOT NULL,
        operation_tree_digest TEXT NOT NULL,
        materialized_digest TEXT NOT NULL,
        authoritative_ref_oid TEXT NOT NULL,
        equality_digest TEXT NOT NULL UNIQUE,
        recorded_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_rollback (
        epoch_id TEXT PRIMARY KEY,
        equality_digest TEXT NOT NULL,
        bundle_digest TEXT NOT NULL UNIQUE,
        inventory_digest TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'prepared',
        recorded_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_rollback_targets (
        epoch_id TEXT NOT NULL,
        replica_id TEXT NOT NULL,
        bundle_digest TEXT NOT NULL,
        target_manifest_digest TEXT NOT NULL,
        backup_digest TEXT NOT NULL,
        projection_digest TEXT NOT NULL,
        expect_state_digest TEXT NOT NULL,
        receipt_bytes BLOB NOT NULL,
        receipt_digest TEXT NOT NULL UNIQUE,
        applied_at TEXT NOT NULL DEFAULT ({_NOW}),
        PRIMARY KEY (epoch_id, replica_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS sync_migration_failures (
        epoch_id TEXT PRIMARY KEY,
        phase TEXT NOT NULL,
        reason TEXT NOT NULL,
        state_digest TEXT NOT NULL,
        failed_at TEXT NOT NULL DEFAULT ({_NOW})
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS sync_migration_one_current
    ON sync_migration_epoch(current) WHERE current = 1
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS sync_migration_state_one_current
    ON sync_migration_state(current) WHERE current = 1
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
    """
    CREATE INDEX IF NOT EXISTS sync_migration_attestation_kind_idx
    ON sync_migration_attestations(epoch_id, kind, replica_id)
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
    object_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(sync_objects)")
    }
    if "capture_seq" not in object_columns:
        connection.execute("ALTER TABLE sync_objects ADD COLUMN capture_seq TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS sync_objects_capture_seq "
        "ON sync_objects(capture_seq) WHERE capture_seq IS NOT NULL"
    )
    connection.execute(
        "INSERT OR IGNORE INTO sync_capture_clock(singleton, capture_seq) "
        "VALUES (1, '0')"
    )


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise SyncInvariantError(
            "sync mutation requires a caller-owned BEGIN IMMEDIATE transaction"
        )


def _canonical_state_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode local protocol state without accepting floats or non-JSON values."""

    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if json.loads(encoded) != dict(value):
            raise ValueError("canonical JSON round trip changed the value")
        return encoded
    except (TypeError, ValueError) as exc:
        raise SyncInvariantError("migration state must be canonical JSON data") from exc


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_state_bytes(value)).hexdigest()


def _require_digest(value: str | None, name: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise SyncInvariantError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _allocate_capture_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT capture_seq FROM sync_capture_clock WHERE singleton=1"
    ).fetchone()
    current = _counter(int(row[0]) if row is not None else 0, allow_zero=True)
    if current == MAX_COUNTER:
        raise SyncInvariantError("capture sequence exhausted unsigned 64-bit space")
    allocated = current + 1
    connection.execute(
        "INSERT INTO sync_capture_clock(singleton, capture_seq) VALUES (1, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET capture_seq=excluded.capture_seq",
        (str(allocated),),
    )
    return allocated


def capture_frontier(connection: sqlite3.Connection) -> int:
    """Return the local semantic capture watermark without mutating it."""

    if "sync_capture_clock" not in _table_names(connection):
        return 0
    row = connection.execute(
        "SELECT capture_seq FROM sync_capture_clock WHERE singleton=1"
    ).fetchone()
    return _counter(int(row[0]) if row is not None else 0, allow_zero=True)


def captured_operations(
    connection: sqlite3.Connection,
    *,
    after: int = 0,
    through: int | None = None,
) -> list[dict[str, Any]]:
    """List a bounded interval's capture identities, never semantic bodies."""

    after = _counter(after, allow_zero=True)
    upper = capture_frontier(connection) if through is None else _counter(
        through, allow_zero=True
    )
    if upper < after:
        raise SyncInvariantError("capture interval cannot move backwards")
    return [
        {"capture_seq": int(seq), "op_id": str(op_id)}
        for seq, op_id in connection.execute(
            "SELECT capture_seq, op_id FROM sync_objects "
            "WHERE capture_seq IS NOT NULL "
            "ORDER BY length(capture_seq),capture_seq"
        )
        if after < int(seq) <= upper
    ]


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
    *,
    preallocated: bool = False,
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
    if operation_counter < current and not preallocated:
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
    seed_binding: Mapping[str, Any] | None = None,
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
    reserved_seed: tuple[Any, ...] | None = None
    if seed_binding is not None:
        binding = dict(seed_binding)
        required_binding = {
            "epoch_id",
            "seed_kind",
            "source_digest",
            "source_identity",
        }
        if set(binding) != required_binding:
            raise SyncInvariantError("seed binding identity is incomplete")
        _require_digest(binding["source_digest"], "source_digest")
        reserved_seed = connection.execute(
            "SELECT counter,op_id FROM sync_migration_seed_map WHERE epoch_id=? "
            "AND replica_id=? AND seed_kind=? AND source_digest=? "
            "AND source_identity=?",
            (
                binding["epoch_id"],
                replica_id,
                binding["seed_kind"],
                binding["source_digest"],
                binding["source_identity"],
            ),
        ).fetchone()
        if reserved_seed is None or int(reserved_seed[0]) != operation_counter:
            raise SyncInvariantError("operation dot is not the reserved seed dot")
        if reserved_seed[1] not in (None, op_id):
            raise SyncInvariantError("reserved seed source is bound to another operation")

    existing = connection.execute(
        "SELECT replica_id, counter, project_key, kind, object_path, payload_bytes, "
        "capture_seq "
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
        return {
            "capture_seq": int(existing[6]) if existing[6] is not None else None,
            "idempotent": True,
            "op_id": op_id,
            "state": str(outbox[0]),
        }

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
        connection,
        replica_id,
        operation_counter,
        installation_fingerprint,
        preallocated=reserved_seed is not None,
    )
    # Seed objects project an already-captured snapshot/delta.  Assigning a
    # fresh capture sequence here would manufacture an endless migration tail.
    capture_seq = None if reserved_seed is not None else _allocate_capture_seq(connection)
    connection.execute(
        "INSERT INTO sync_objects("
        "op_id, replica_id, counter, project_key, kind, object_path, payload_bytes, "
        "capture_seq"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            op_id,
            replica_id,
            str(operation_counter),
            project_key,
            kind,
            object_path,
            sqlite3.Binary(payload_bytes),
            str(capture_seq) if capture_seq is not None else None,
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
    if reserved_seed is not None:
        connection.execute(
            "UPDATE sync_migration_seed_map SET op_id=? WHERE epoch_id=? "
            "AND replica_id=? AND seed_kind=? AND source_digest=? "
            "AND source_identity=? AND (op_id IS NULL OR op_id=?)",
            (
                op_id,
                binding["epoch_id"],
                replica_id,
                binding["seed_kind"],
                binding["source_digest"],
                binding["source_identity"],
                op_id,
            ),
        )
    return {
        "capture_seq": capture_seq,
        "idempotent": False,
        "op_id": op_id,
        "state": "queued",
    }


def record_reserved_seed_operation(
    connection: sqlite3.Connection,
    operation: Mapping[str, Any],
    *,
    epoch_id: str,
    seed_kind: str,
    source_digest: str,
    source_identity: str,
    installation_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Record an authored seed object against its pre-reserved local dot."""

    return record_local_operation(
        connection,
        operation,
        installation_fingerprint=installation_fingerprint,
        seed_binding={
            "epoch_id": epoch_id,
            "seed_kind": seed_kind,
            "source_digest": source_digest,
            "source_identity": source_identity,
        },
    )


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


def _legacy_migration_state(epoch_id: str) -> dict[str, Any]:
    payload = {
        "epoch_id": epoch_id,
        "equality_digest": None,
        "evidence_digest": None,
        "fence_capture_seq": None,
        "membership_digest": None,
        "migration_state": "legacy",
        "phase_seq": 0,
        "protocol_major": PROTOCOL_MAJOR,
        "rollback_bundle_digest": None,
        "writer_mode": "legacy-capture",
    }
    return {**payload, "state_digest": _digest(payload), "last_receipt_digest": None}


def migration_current(
    connection: sqlite3.Connection, epoch_id: str
) -> dict[str, Any] | None:
    """Return one durable v28 epoch row, or ``None`` before its first CAS."""

    if "sync_migration_state" not in _table_names(connection):
        return None
    row = connection.execute(
        "SELECT phase,phase_seq,current,membership_digest,evidence_digest,"
        "writer_mode,state_digest,last_receipt_digest,fence_capture_seq,"
        "equality_digest,rollback_bundle_digest FROM sync_migration_state "
        "WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if row is None:
        return None
    result = {
        "epoch_id": epoch_id,
        "equality_digest": row[9],
        "evidence_digest": row[4],
        "fence_capture_seq": int(row[8]) if row[8] is not None else None,
        "last_receipt_digest": row[7],
        "membership_digest": row[3],
        "migration_state": str(row[0]),
        "phase_seq": int(row[1]),
        "protocol_major": PROTOCOL_MAJOR,
        "rollback_bundle_digest": row[10],
        "state_digest": str(row[6]),
        "writer_mode": str(row[5]),
        "current": bool(row[2]),
    }
    receipt = connection.execute(
        "SELECT input_digest,previous_receipt_digest,receipt_digest,state_digest "
        "FROM sync_migration_receipts WHERE epoch_id=? AND phase_seq=?",
        (epoch_id, result["phase_seq"]),
    ).fetchone()
    if receipt is None:
        raise SyncInvariantError("migration state has no matching phase receipt")
    state_payload = {
        "epoch_id": epoch_id,
        "equality_digest": result["equality_digest"],
        "evidence_digest": result["evidence_digest"],
        "fence_capture_seq": result["fence_capture_seq"],
        "membership_digest": result["membership_digest"],
        "migration_state": result["migration_state"],
        "phase_seq": result["phase_seq"],
        "previous_receipt_digest": receipt[1],
        "protocol_major": PROTOCOL_MAJOR,
        "rollback_bundle_digest": result["rollback_bundle_digest"],
        "transition_input_digest": receipt[0],
        "writer_mode": result["writer_mode"],
    }
    if (
        _digest(state_payload) != result["state_digest"]
        or receipt[2] != result["last_receipt_digest"]
        or receipt[3] != result["state_digest"]
    ):
        raise SyncInvariantError("migration state and receipt chain disagree")
    return result


def migration_status(connection: sqlite3.Connection, epoch_id: str) -> dict[str, Any]:
    """Return the CAS identity for an epoch, including its pre-row legacy state."""

    return migration_current(connection, epoch_id) or _legacy_migration_state(epoch_id)


def migration_receipt(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    phase: str | None = None,
    receipt_digest: str | None = None,
) -> dict[str, Any] | None:
    """Return and verify one exact durable receipt by phase or digest."""

    if (phase is None) == (receipt_digest is None):
        raise SyncInvariantError("receipt lookup requires exactly one identity")
    if "sync_migration_receipts" not in _table_names(connection):
        return None
    if receipt_digest is not None:
        _require_digest(receipt_digest, "receipt_digest")
        where, parameter = "receipt_digest=?", receipt_digest
    else:
        if not isinstance(phase, str) or not phase:
            raise SyncInvariantError("receipt phase is invalid")
        where, parameter = "phase=?", phase
    row = connection.execute(
        "SELECT expect_digest,state_digest,input_digest,receipt_bytes,receipt_digest "
        f"FROM sync_migration_receipts WHERE epoch_id=? AND {where}",
        (epoch_id, parameter),
    ).fetchone()
    if row is None:
        return None
    raw = bytes(row[3])
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncInvariantError("stored migration receipt is invalid JSON") from exc
    if not isinstance(parsed, dict) or _canonical_state_bytes(parsed) != raw:
        raise SyncInvariantError("stored migration receipt is not canonical")
    claimed = parsed.pop("receipt_digest", None)
    if (
        claimed != row[4]
        or _digest(parsed) != claimed
        or parsed.get("previous_state_digest") != row[0]
        or parsed.get("state_digest") != row[1]
        or parsed.get("input_digest") != row[2]
    ):
        raise SyncInvariantError("stored migration receipt digest disagrees")
    return {**parsed, "receipt_digest": claimed}


def _phase_index(phase: str) -> int:
    try:
        return MIGRATION_PHASES.index(phase)
    except ValueError as exc:
        raise SyncInvariantError(f"unknown migration state: {phase!r}") from exc


def migration_transition(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    phase: str,
    target_state: str | None = None,
    expect_digest: str,
    input_digest: str,
    membership_digest: str | None = None,
    evidence_digest: str | None = None,
    receipt_bytes: bytes | None = None,
    writer_mode: str | None = None,
    fence_capture_seq: int | None = None,
    equality_digest: str | None = None,
    rollback_bundle_digest: str | None = None,
) -> dict[str, Any]:
    """Advance exactly one migration state with a durable CAS receipt.

    ``phase`` names the operator action for the receipt; ``target_state`` is
    the normative monotonic state and defaults to ``phase``.  An exact retry
    returns the stored byte-identical receipt.  No counter, row, or fence is
    changed on stale, skipped, reverse, or equivocal input.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if not isinstance(epoch_id, str) or not epoch_id:
        raise SyncInvariantError("migration epoch_id is required")
    if not isinstance(phase, str) or not phase:
        raise SyncInvariantError("migration receipt phase is required")
    target = target_state or phase
    target_index = _phase_index(target)
    if target == "legacy":
        raise SyncInvariantError("legacy is an observed state, not a transition")
    expect_digest = _require_digest(expect_digest, "expect_digest")
    input_digest = _require_digest(input_digest, "input_digest")
    for value, name in (
        (membership_digest, "membership_digest"),
        (evidence_digest, "evidence_digest"),
        (equality_digest, "equality_digest"),
        (rollback_bundle_digest, "rollback_bundle_digest"),
    ):
        if value is not None:
            _require_digest(value, name)
    if writer_mode is not None and writer_mode not in WRITER_MODES:
        raise SyncInvariantError(f"unknown writer mode: {writer_mode!r}")
    if fence_capture_seq is not None:
        fence_capture_seq = _counter(fence_capture_seq, allow_zero=True)

    current = migration_status(connection, epoch_id)
    existing_row = connection.execute(
        "SELECT expect_digest,state_digest,input_digest,receipt_bytes "
        "FROM sync_migration_receipts WHERE epoch_id=? AND phase=?",
        (epoch_id, phase),
    ).fetchone()
    if existing_row is not None:
        if (
            str(existing_row[0]) != expect_digest
            or str(existing_row[2]) != input_digest
            or str(existing_row[1]) != current["state_digest"]
        ):
            raise SyncInvariantError("migration phase retry is equivocal or stale")
        try:
            stored = json.loads(bytes(existing_row[3]))
        except (TypeError, ValueError) as exc:
            raise SyncInvariantError("stored migration receipt is corrupt") from exc
        if (
            stored.get("migration_state") != target
            or (
                membership_digest is not None
                and stored.get("membership_digest") != membership_digest
            )
            or (
                evidence_digest is not None
                and stored.get("evidence_digest") != evidence_digest
            )
        ):
            raise SyncInvariantError("migration phase retry changes its durable target")
        return dict(stored)

    if expect_digest != current["state_digest"]:
        raise SyncInvariantError("migration state digest CAS failed")
    current_index = _phase_index(str(current["migration_state"]))
    if target_index != current_index + 1:
        raise SyncInvariantError(
            f"migration transition must be {MIGRATION_PHASES[current_index]} -> "
            f"{MIGRATION_PHASES[current_index + 1] if current_index + 1 < len(MIGRATION_PHASES) else 'none'}"
        )

    old_membership = current.get("membership_digest")
    next_membership = membership_digest or old_membership
    if old_membership is not None and next_membership != old_membership:
        raise SyncInvariantError("sealed migration membership is immutable")
    if target == "membership-sealed" and next_membership is None:
        raise SyncInvariantError("membership-sealed requires membership_digest")
    old_evidence = current.get("evidence_digest")
    next_evidence = evidence_digest or old_evidence
    if old_evidence is not None and next_evidence != old_evidence:
        raise SyncInvariantError("sealed migration evidence is immutable")
    if target == "evidence-sealed" and next_evidence is None:
        raise SyncInvariantError("evidence-sealed requires evidence_digest")
    next_equality = equality_digest or current.get("equality_digest")
    if target == "equality-proven" and next_equality is None:
        raise SyncInvariantError("equality-proven requires equality_digest")
    next_writer_mode = writer_mode or str(current["writer_mode"])
    next_rollback = rollback_bundle_digest or current.get("rollback_bundle_digest")
    if target == "rollback-window":
        if next_writer_mode != "fenced":
            raise SyncInvariantError("rollback-window requires writer_mode=fenced")
        if next_rollback is not None:
            raise SyncInvariantError(
                "rollback-window must fence writers before preparing its bundle"
            )
    if target == "closed":
        rollback = connection.execute(
            "SELECT bundle_digest,state FROM sync_migration_rollback WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        applied = rollback_apply_status(connection, epoch_id)
        if (
            next_rollback is None
            or rollback is None
            or rollback[0] != next_rollback
            or rollback[1] != "applied"
            or applied["bundle_digest"] != next_rollback
            or applied["state"] != "applied"
            or not applied["complete"]
        ):
            raise SyncInvariantError("rollback close requires every target applied")
    if target == "old-writers-fenced" and next_writer_mode != "fenced":
        raise SyncInvariantError("old-writers-fenced requires writer_mode=fenced")
    if target == "v2-only-enabled" and next_writer_mode != "v2":
        raise SyncInvariantError("v2-only-enabled requires writer_mode=v2")
    if target == "closed" and next_writer_mode != "fenced":
        raise SyncInvariantError("rollback close must retain the writer fence")
    next_fence_seq = (
        fence_capture_seq
        if fence_capture_seq is not None
        else current.get("fence_capture_seq")
    )
    if target == "old-writers-fenced" and next_fence_seq is None:
        raise SyncInvariantError("old-writers-fenced requires fence_capture_seq")

    next_seq = int(current["phase_seq"]) + 1
    state_payload = {
        "epoch_id": epoch_id,
        "equality_digest": next_equality,
        "evidence_digest": next_evidence,
        "fence_capture_seq": next_fence_seq,
        "membership_digest": next_membership,
        "migration_state": target,
        "phase_seq": next_seq,
        "previous_receipt_digest": current.get("last_receipt_digest"),
        "protocol_major": PROTOCOL_MAJOR,
        "rollback_bundle_digest": next_rollback,
        "transition_input_digest": input_digest,
        "writer_mode": next_writer_mode,
    }
    state_digest = _digest(state_payload)
    receipt_payload = {
        "changed": True,
        "current_state_digest": state_digest,
        "epoch_id": epoch_id,
        "evidence_digest": next_evidence,
        "input_digest": input_digest,
        "membership_digest": next_membership,
        "migration_state": target,
        "phase": phase,
        "previous_receipt_digest": current.get("last_receipt_digest"),
        "previous_state_digest": expect_digest,
        "protocol_major": PROTOCOL_MAJOR,
        "reason": "ok",
        "required_action": "none",
        "schema_version": 1,
        "state_digest": state_digest,
        "status": "local-only",
    }
    receipt_digest = _digest(receipt_payload)
    receipt = {**receipt_payload, "receipt_digest": receipt_digest}
    canonical_receipt = _canonical_state_bytes(receipt)
    if receipt_bytes is not None and bytes(receipt_bytes) != canonical_receipt:
        raise SyncInvariantError("caller receipt bytes disagree with durable receipt")

    other = connection.execute(
        "SELECT epoch_id FROM sync_migration_state WHERE current=1 AND epoch_id<>?",
        (epoch_id,),
    ).fetchone()
    if other is not None:
        raise SyncInvariantError("another migration epoch is already current")
    connection.execute(
        "INSERT INTO sync_migration_state("
        "epoch_id,phase,phase_seq,current,membership_digest,evidence_digest,"
        "writer_mode,state_digest,last_receipt_digest,fence_capture_seq,"
        "equality_digest,rollback_bundle_digest) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(epoch_id) DO UPDATE SET "
        "phase=excluded.phase,phase_seq=excluded.phase_seq,current=1,"
        "membership_digest=excluded.membership_digest,"
        "evidence_digest=excluded.evidence_digest,writer_mode=excluded.writer_mode,"
        "state_digest=excluded.state_digest,"
        "last_receipt_digest=excluded.last_receipt_digest,"
        "fence_capture_seq=excluded.fence_capture_seq,"
        "equality_digest=excluded.equality_digest,"
        f"rollback_bundle_digest=excluded.rollback_bundle_digest,updated_at={_NOW}",
        (
            epoch_id,
            target,
            next_seq,
            1,
            next_membership,
            next_evidence,
            next_writer_mode,
            state_digest,
            receipt_digest,
            str(next_fence_seq) if next_fence_seq is not None else None,
            next_equality,
            next_rollback,
        ),
    )
    connection.execute(
        "INSERT INTO sync_migration_receipts("
        "epoch_id,phase_seq,phase,expect_digest,state_digest,"
        "previous_receipt_digest,input_digest,membership_digest,evidence_digest,"
        "changed,receipt_bytes,receipt_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            next_seq,
            phase,
            expect_digest,
            state_digest,
            current.get("last_receipt_digest"),
            input_digest,
            next_membership,
            next_evidence,
            1,
            sqlite3.Binary(canonical_receipt),
            receipt_digest,
        ),
    )
    if target == "closed":
        connection.execute(
            "UPDATE sync_migration_rollback SET state='closed' "
            "WHERE epoch_id=? AND state='applied'",
            (epoch_id,),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise SyncInvariantError("rollback bundle changed during close CAS")
    return receipt


def record_migration_seal(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    seal_kind: str,
    manifest_bytes: bytes,
    receipt_digest: str,
    members: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Persist an immutable membership/evidence seal and normalized roster."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if seal_kind not in {"membership", "evidence"}:
        raise SyncInvariantError("seal_kind must be membership or evidence")
    if not isinstance(manifest_bytes, bytes):
        raise SyncInvariantError("seal manifest must be exact bytes")
    receipt_digest = _require_digest(receipt_digest, "receipt_digest")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncInvariantError("seal manifest is not canonical JSON") from exc
    if not isinstance(manifest, dict) or _canonical_state_bytes(manifest) != manifest_bytes:
        raise SyncInvariantError("seal manifest bytes are not canonical JSON")
    manifest_digest = _require_digest(
        manifest.get("manifest_digest"), "manifest_digest"
    )
    manifest_payload = dict(manifest)
    manifest_payload.pop("manifest_digest", None)
    if _digest(manifest_payload) != manifest_digest:
        raise SyncInvariantError("seal manifest self-digest is invalid")
    state = migration_current(connection, epoch_id)
    if state is None or not state.get("membership_digest"):
        raise SyncInvariantError("migration membership is not sealed")
    if seal_kind == "membership":
        expected_digest = str(state["membership_digest"])
    else:
        expected_digest = state.get("evidence_digest")
        if expected_digest is None:
            raise SyncInvariantError("migration evidence is not sealed")
    if manifest_digest != expected_digest:
        raise SyncInvariantError("seal bytes disagree with the durable seal digest")
    existing = connection.execute(
        "SELECT membership_digest,manifest_digest,manifest_bytes,receipt_digest "
        "FROM sync_migration_seals WHERE epoch_id=? AND seal_kind=?",
        (epoch_id, seal_kind),
    ).fetchone()
    expected = (
        str(state["membership_digest"]),
        manifest_digest,
        manifest_bytes,
        receipt_digest,
    )
    if existing is not None:
        actual = (existing[0], existing[1], bytes(existing[2]), existing[3])
        if actual != expected:
            raise SyncInvariantError("migration seal retry is equivocal")
        return {"idempotent": True, "manifest_digest": manifest_digest}
    if seal_kind == "evidence":
        membership = connection.execute(
            "SELECT manifest_digest FROM sync_migration_seals "
            "WHERE epoch_id=? AND seal_kind='membership'",
            (epoch_id,),
        ).fetchone()
        if membership is None or membership[0] != state["membership_digest"]:
            raise SyncInvariantError("evidence seal requires the unchanged membership seal")
    connection.execute(
        "INSERT INTO sync_migration_seals("
        "epoch_id,seal_kind,membership_digest,manifest_digest,manifest_bytes,"
        "receipt_digest) VALUES (?,?,?,?,?,?)",
        (
            epoch_id,
            seal_kind,
            state["membership_digest"],
            manifest_digest,
            sqlite3.Binary(manifest_bytes),
            receipt_digest,
        ),
    )
    if seal_kind == "membership":
        normalized: list[tuple[str, int, str, str | None]] = []
        for member in members:
            replica_id = _validate_replica_id(str(member.get("replica_id", "")))
            retired = 1 if member.get("retired") is True else 0
            member_digest = _require_digest(
                member.get("manifest_digest"), "member manifest_digest"
            )
            retirement_digest = member.get("retirement_digest")
            if retirement_digest is not None:
                _require_digest(retirement_digest, "retirement_digest")
            if retired and retirement_digest is None:
                raise SyncInvariantError("retired member requires retirement evidence")
            normalized.append(
                (replica_id, retired, member_digest, retirement_digest)
            )
        if len({row[0] for row in normalized}) != len(normalized):
            raise SyncInvariantError("membership contains duplicate replica IDs")
        connection.executemany(
            "INSERT INTO sync_migration_members("
            "epoch_id,replica_id,retired,manifest_digest,retirement_digest) "
            "VALUES (?,?,?,?,?)",
            ((epoch_id, *row) for row in sorted(normalized)),
        )
    return {"idempotent": False, "manifest_digest": manifest_digest}


def record_migration_attestation(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    replica_id: str,
    kind: str,
    payload_bytes: bytes,
) -> dict[str, Any]:
    """Record one immutable per-replica snapshot/fence/no-tail attestation."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    replica_id = _validate_replica_id(replica_id)
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise SyncInvariantError("attestation kind is invalid")
    if not isinstance(payload_bytes, bytes):
        raise SyncInvariantError("attestation payload must be exact bytes")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    existing = connection.execute(
        "SELECT manifest_digest,payload_bytes FROM sync_migration_attestations "
        "WHERE epoch_id=? AND replica_id=? AND kind=?",
        (epoch_id, replica_id, kind),
    ).fetchone()
    if existing is not None:
        if (existing[0], bytes(existing[1])) != (digest, payload_bytes):
            raise SyncInvariantError("replica attestation retry is equivocal")
        return {"idempotent": True, "manifest_digest": digest}
    member = connection.execute(
        "SELECT retired FROM sync_migration_members WHERE epoch_id=? AND replica_id=?",
        (epoch_id, replica_id),
    ).fetchone()
    if member is None or bool(member[0]):
        raise SyncInvariantError("attestation replica is not an active sealed member")
    connection.execute(
        "INSERT INTO sync_migration_attestations("
        "epoch_id,replica_id,kind,manifest_digest,payload_bytes) VALUES (?,?,?,?,?)",
        (epoch_id, replica_id, kind, digest, sqlite3.Binary(payload_bytes)),
    )
    connection.execute(
        "UPDATE sync_migration_members SET evidence_digest=? "
        "WHERE epoch_id=? AND replica_id=?",
        (digest, epoch_id, replica_id),
    )
    return {"idempotent": False, "manifest_digest": digest}


def migration_attestation(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    replica_id: str,
    kind: str,
) -> dict[str, Any] | None:
    """Return one hash-verified roster attestation with its exact bytes."""

    replica_id = _validate_replica_id(replica_id)
    if "sync_migration_attestations" not in _table_names(connection):
        return None
    row = connection.execute(
        "SELECT manifest_digest,payload_bytes FROM sync_migration_attestations "
        "WHERE epoch_id=? AND replica_id=? AND kind=?",
        (epoch_id, replica_id, kind),
    ).fetchone()
    if row is None:
        return None
    raw = bytes(row[1])
    if hashlib.sha256(raw).hexdigest() != row[0]:
        raise SyncInvariantError("stored migration attestation digest disagrees")
    return {
        "epoch_id": epoch_id,
        "kind": kind,
        "manifest_digest": str(row[0]),
        "payload_bytes": raw,
        "replica_id": replica_id,
    }


def record_migration_artifact(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    artifact_kind: str,
    manifest_digest: str,
    local_path: str | Path,
    replica_id: str | None = None,
    inventory_digest: str | None = None,
    receipt_digest: str | None = None,
) -> dict[str, Any]:
    """Register an immutable local cutover artifact for later rollback.

    The location is deliberately local protocol state and never enters an
    operation object or equality digest.  The caller remains responsible for
    artifact-format verification before registration.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if (
        not isinstance(artifact_kind, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", artifact_kind)
    ):
        raise SyncInvariantError("migration artifact kind is invalid")
    manifest_digest = _require_digest(manifest_digest, "manifest_digest")
    if inventory_digest is not None:
        _require_digest(inventory_digest, "inventory_digest")
    if receipt_digest is not None:
        _require_digest(receipt_digest, "receipt_digest")
        if migration_receipt(
            connection, epoch_id, receipt_digest=receipt_digest
        ) is None:
            raise SyncInvariantError("artifact receipt is not durable")
    owner = "" if replica_id is None else _validate_replica_id(replica_id)
    path = Path(local_path)
    if not path.is_absolute() or not path.exists() or path.is_symlink():
        raise SyncInvariantError("migration artifact path must be existing and absolute")
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe /= part
        if probe.is_symlink():
            raise SyncInvariantError("migration artifact path contains a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SyncInvariantError("migration artifact path cannot be resolved") from exc
    local_identity = str(resolved)
    existing = connection.execute(
        "SELECT manifest_digest,inventory_digest,local_path,receipt_digest "
        "FROM sync_migration_artifacts WHERE epoch_id=? AND artifact_kind=? "
        "AND replica_id=?",
        (epoch_id, artifact_kind, owner),
    ).fetchone()
    expected = (
        manifest_digest,
        inventory_digest,
        local_identity,
        receipt_digest,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise SyncInvariantError("migration artifact retry is equivocal")
        return {"idempotent": True, "manifest_digest": manifest_digest}
    connection.execute(
        "INSERT INTO sync_migration_artifacts("
        "epoch_id,artifact_kind,replica_id,manifest_digest,inventory_digest,"
        "local_path,receipt_digest) VALUES (?,?,?,?,?,?,?)",
        (
            epoch_id,
            artifact_kind,
            owner,
            manifest_digest,
            inventory_digest,
            local_identity,
            receipt_digest,
        ),
    )
    return {"idempotent": False, "manifest_digest": manifest_digest}


def migration_artifacts(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    artifact_kinds: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the local artifact inventory for an operator rollback bundle."""

    if "sync_migration_artifacts" not in _table_names(connection):
        return []
    kinds = sorted(set(artifact_kinds or ()))
    for kind in kinds:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind):
            raise SyncInvariantError("migration artifact kind is invalid")
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        query = (
            "SELECT artifact_kind,replica_id,manifest_digest,inventory_digest,"
            "local_path,receipt_digest FROM sync_migration_artifacts "
            f"WHERE epoch_id=? AND artifact_kind IN ({placeholders}) "
            "ORDER BY artifact_kind,replica_id"
        )
        parameters: tuple[Any, ...] = (epoch_id, *kinds)
    else:
        query = (
            "SELECT artifact_kind,replica_id,manifest_digest,inventory_digest,"
            "local_path,receipt_digest FROM sync_migration_artifacts "
            "WHERE epoch_id=? ORDER BY artifact_kind,replica_id"
        )
        parameters = (epoch_id,)
    return [
        {
            "artifact_kind": str(row[0]),
            "epoch_id": epoch_id,
            "inventory_digest": row[3],
            "local_path": str(row[4]),
            "manifest_digest": str(row[2]),
            "receipt_digest": row[5],
            "replica_id": str(row[1]) or None,
        }
        for row in connection.execute(query, parameters)
    ]


def reserve_seed_counters(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    replica_id: str,
    seed_kind: str,
    source_digest: str,
    source_identities: Sequence[str],
    membership_digest: str,
    activation_boundary: str,
    canonicalizer_version: str,
) -> dict[str, Any]:
    """Reserve one deterministic contiguous dot interval exactly once."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    replica_id = _validate_replica_id(replica_id)
    if seed_kind not in {"snapshot", "delta"}:
        raise SyncInvariantError("seed_kind must be snapshot or delta")
    source_digest = _require_digest(source_digest, "source_digest")
    membership_digest = _require_digest(membership_digest, "membership_digest")
    if not activation_boundary or not canonicalizer_version:
        raise SyncInvariantError("seed activation boundary and canonicalizer are required")
    identities = list(source_identities)
    if not identities or any(
        not isinstance(value, str) or not value for value in identities
    ):
        raise SyncInvariantError("seed source identities must be nonempty and unique")
    identities.sort()
    if len(set(identities)) != len(identities):
        raise SyncInvariantError("seed source identities must be nonempty and unique")
    identity_payload = {
        "activation_boundary": activation_boundary,
        "canonicalizer_version": canonicalizer_version,
        "epoch_id": epoch_id,
        "membership_digest": membership_digest,
        "replica_id": replica_id,
        "seed_kind": seed_kind,
        "source_digest": source_digest,
        "source_identities": identities,
    }
    identity_digest = _digest(identity_payload)
    existing = connection.execute(
        "SELECT identity_digest,counter_start,counter_end,item_count,mapping_digest "
        "FROM sync_migration_seed_reservations WHERE epoch_id=? AND replica_id=? "
        "AND seed_kind=? AND source_digest=?",
        (epoch_id, replica_id, seed_kind, source_digest),
    ).fetchone()
    if existing is not None:
        rows = connection.execute(
            "SELECT source_identity,counter,dot,op_id FROM sync_migration_seed_map "
            "WHERE epoch_id=? AND replica_id=? AND seed_kind=? AND source_digest=? "
            "ORDER BY source_ordinal",
            (epoch_id, replica_id, seed_kind, source_digest),
        ).fetchall()
        if existing[0] != identity_digest or [str(row[0]) for row in rows] != identities:
            raise SyncInvariantError("seed reservation retry is equivocal")
        return {
            "counter_end": int(existing[2]) if existing[2] is not None else None,
            "counter_start": int(existing[1]) if existing[1] is not None else None,
            "idempotent": True,
            "identity_digest": identity_digest,
            "mapping_digest": str(existing[4]),
            "mappings": [
                {
                    "counter": int(row[1]),
                    "dot": str(row[2]),
                    "op_id": row[3],
                    "source_identity": str(row[0]),
                }
                for row in rows
            ],
        }
    state = migration_current(connection, epoch_id)
    if state is None or state.get("membership_digest") != membership_digest:
        raise SyncInvariantError("seed reservation membership is not the sealed roster")
    replica = connection.execute(
        "SELECT counter,active FROM sync_replica WHERE replica_id=?",
        (replica_id,),
    ).fetchone()
    if replica is None or not bool(replica[1]):
        raise SyncInvariantError("seed reservation requires the active local replica")
    current = _counter(int(replica[0]), allow_zero=True)
    if len(identities) > MAX_COUNTER - current:
        raise SyncInvariantError("seed reservation exhausts replica counter space")
    counter_start = current + 1
    counter_end = current + len(identities)
    mappings = [
        {
            "counter": counter_start + ordinal,
            "dot": f"{replica_id}:{counter_start + ordinal}",
            "source_identity": source_identity,
        }
        for ordinal, source_identity in enumerate(identities)
    ]
    mapping_digest = _digest({"mappings": mappings})
    connection.execute(
        "UPDATE sync_replica SET counter=? WHERE replica_id=? AND counter=?",
        (str(counter_end), replica_id, str(current)),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise SyncInvariantError("replica counter changed during seed reservation")
    connection.execute(
        "INSERT INTO sync_migration_seed_reservations("
        "epoch_id,replica_id,seed_kind,source_digest,membership_digest,"
        "activation_boundary,canonicalizer_version,identity_digest,counter_start,"
        "counter_end,item_count,mapping_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            replica_id,
            seed_kind,
            source_digest,
            membership_digest,
            activation_boundary,
            canonicalizer_version,
            identity_digest,
            str(counter_start),
            str(counter_end),
            len(mappings),
            mapping_digest,
        ),
    )
    connection.executemany(
        "INSERT INTO sync_migration_seed_map("
        "epoch_id,replica_id,seed_kind,source_digest,source_identity,"
        "source_ordinal,counter,dot) VALUES (?,?,?,?,?,?,?,?)",
        (
            (
                epoch_id,
                replica_id,
                seed_kind,
                source_digest,
                mapping["source_identity"],
                ordinal,
                str(mapping["counter"]),
                mapping["dot"],
            )
            for ordinal, mapping in enumerate(mappings)
        ),
    )
    return {
        "counter_end": counter_end,
        "counter_start": counter_start,
        "idempotent": False,
        "identity_digest": identity_digest,
        "mapping_digest": mapping_digest,
        "mappings": mappings,
    }


def bind_seed_operation(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    replica_id: str,
    seed_kind: str,
    source_digest: str,
    source_identity: str,
    op_id: str,
) -> dict[str, Any]:
    """Bind a reserved source identity to its rendered immutable operation."""

    _require_transaction(connection)
    replica_id = _validate_replica_id(replica_id)
    source_digest = _require_digest(source_digest, "source_digest")
    op_id = _require_digest(op_id, "op_id")
    row = connection.execute(
        "SELECT counter,op_id FROM sync_migration_seed_map WHERE epoch_id=? "
        "AND replica_id=? AND seed_kind=? AND source_digest=? AND source_identity=?",
        (epoch_id, replica_id, seed_kind, source_digest, source_identity),
    ).fetchone()
    if row is None:
        raise SyncInvariantError("seed source identity was not reserved")
    if row[1] is not None:
        if row[1] != op_id:
            raise SyncInvariantError("seed source identity is already bound differently")
        return {"counter": int(row[0]), "idempotent": True, "op_id": op_id}
    duplicate = connection.execute(
        "SELECT source_identity FROM sync_migration_seed_map WHERE op_id=?",
        (op_id,),
    ).fetchone()
    if duplicate is not None:
        raise SyncInvariantError("seed operation is already bound to another source")
    connection.execute(
        "UPDATE sync_migration_seed_map SET op_id=? WHERE epoch_id=? "
        "AND replica_id=? AND seed_kind=? AND source_digest=? AND source_identity=? "
        "AND op_id IS NULL",
        (op_id, epoch_id, replica_id, seed_kind, source_digest, source_identity),
    )
    return {"counter": int(row[0]), "idempotent": False, "op_id": op_id}


def bind_captured_operation(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    capture_seq: int,
    captured_op_id: str,
    seed_op_id: str,
    source_digest: str,
) -> dict[str, Any]:
    """Bind one captured semantic mutation to exactly one delta seed object."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    capture_seq = _counter(capture_seq)
    captured_op_id = _require_digest(captured_op_id, "captured_op_id")
    seed_op_id = _require_digest(seed_op_id, "seed_op_id")
    source_digest = _require_digest(source_digest, "source_digest")
    captured = connection.execute(
        "SELECT op_id FROM sync_objects WHERE capture_seq=?",
        (str(capture_seq),),
    ).fetchone()
    if captured is None or captured[0] != captured_op_id:
        raise SyncInvariantError("capture sequence does not name the captured operation")
    seeded = connection.execute(
        "SELECT 1 FROM sync_migration_seed_map WHERE epoch_id=? AND op_id=?",
        (epoch_id, seed_op_id),
    ).fetchone()
    if seeded is None:
        raise SyncInvariantError("capture binding seed operation is not epoch-bound")
    existing = connection.execute(
        "SELECT captured_op_id,seed_op_id,source_digest "
        "FROM sync_migration_capture_bindings WHERE epoch_id=? AND capture_seq=?",
        (epoch_id, str(capture_seq)),
    ).fetchone()
    expected = (captured_op_id, seed_op_id, source_digest)
    if existing is not None:
        if tuple(existing) != expected:
            raise SyncInvariantError("capture binding retry is equivocal")
        return {"capture_seq": capture_seq, "idempotent": True}
    connection.execute(
        "INSERT INTO sync_migration_capture_bindings("
        "epoch_id,capture_seq,captured_op_id,seed_op_id,source_digest) "
        "VALUES (?,?,?,?,?)",
        (epoch_id, str(capture_seq), captured_op_id, seed_op_id, source_digest),
    )
    return {"capture_seq": capture_seq, "idempotent": False}


def record_captured_delta_operation(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    capture_seq: int,
    captured_op_id: str,
    source_digest: str,
) -> dict[str, Any]:
    """Register an existing captured op as its own deterministic delta seed.

    The semantic operation already owns a replica dot and outbox row.  Reusing
    those exact immutable bytes avoids allocating another counter or capture
    sequence while still making no-tail coverage explicit and queryable.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    capture_seq = _counter(capture_seq)
    captured_op_id = _require_digest(captured_op_id, "captured_op_id")
    source_digest = _require_digest(source_digest, "source_digest")
    state = migration_current(connection, epoch_id)
    if state is None or _phase_index(str(state["migration_state"])) < _phase_index(
        "membership-sealed"
    ):
        raise SyncInvariantError("captured delta requires a sealed migration epoch")
    row = connection.execute(
        "SELECT replica_id,counter FROM sync_objects "
        "WHERE capture_seq=? AND op_id=?",
        (str(capture_seq), captured_op_id),
    ).fetchone()
    if row is None:
        raise SyncInvariantError("captured delta identity is absent")
    replica_id, counter = str(row[0]), str(row[1])
    source_identity = f"capture:{capture_seq}:{captured_op_id}"
    existing = connection.execute(
        "SELECT epoch_id,seed_kind,source_digest,source_identity,op_id "
        "FROM sync_migration_seed_map WHERE replica_id=? AND counter=?",
        (replica_id, counter),
    ).fetchone()
    expected = (
        epoch_id,
        "delta",
        source_digest,
        source_identity,
        captured_op_id,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise SyncInvariantError("captured delta dot is already mapped differently")
        binding = bind_captured_operation(
            connection,
            epoch_id=epoch_id,
            capture_seq=capture_seq,
            captured_op_id=captured_op_id,
            seed_op_id=captured_op_id,
            source_digest=source_digest,
        )
        return {
            "capture_seq": capture_seq,
            "idempotent": True and bool(binding["idempotent"]),
            "op_id": captured_op_id,
            "source_identity": source_identity,
        }
    ordinal = int(
        connection.execute(
            "SELECT COUNT(*) FROM sync_migration_seed_map WHERE epoch_id=? "
            "AND replica_id=? AND seed_kind='delta' AND source_digest=?",
            (epoch_id, replica_id, source_digest),
        ).fetchone()[0]
    )
    connection.execute(
        "INSERT INTO sync_migration_seed_map("
        "epoch_id,replica_id,seed_kind,source_digest,source_identity,"
        "source_ordinal,counter,dot,op_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            replica_id,
            "delta",
            source_digest,
            source_identity,
            ordinal,
            counter,
            f"{replica_id}:{counter}",
            captured_op_id,
        ),
    )
    bind_captured_operation(
        connection,
        epoch_id=epoch_id,
        capture_seq=capture_seq,
        captured_op_id=captured_op_id,
        seed_op_id=captured_op_id,
        source_digest=source_digest,
    )
    return {
        "capture_seq": capture_seq,
        "idempotent": False,
        "op_id": captured_op_id,
        "source_identity": source_identity,
    }


def no_tail_status(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    after: int,
    through: int,
    limit: int = 8,
) -> dict[str, Any]:
    """Check exact captured-delta coverage and local seed/outbox readiness."""

    after = _counter(after, allow_zero=True)
    through = _counter(through, allow_zero=True)
    if through < after:
        raise SyncInvariantError("no-tail interval cannot move backwards")
    limit = max(1, min(int(limit), 8))
    observed_rows = [
        (row["capture_seq"], row["op_id"])
        for row in captured_operations(connection, after=after, through=through)
    ]
    missing: list[int] = []
    cursor = after + 1
    for sequence, _op_id in observed_rows:
        sequence = int(sequence)
        while cursor < sequence and len(missing) < limit:
            missing.append(cursor)
            cursor += 1
        cursor = max(cursor, sequence + 1)
    while cursor <= through and len(missing) < limit:
        missing.append(cursor)
        cursor += 1
    missing_count = max(0, (through - after) - len(observed_rows))
    all_unbound_rows = connection.execute(
        "SELECT o.capture_seq FROM sync_objects o "
        "LEFT JOIN sync_migration_capture_bindings b "
        "ON b.epoch_id=? AND b.capture_seq=o.capture_seq "
        "AND b.captured_op_id=o.op_id "
        "WHERE o.capture_seq IS NOT NULL AND b.capture_seq IS NULL "
        "ORDER BY length(o.capture_seq),o.capture_seq",
        (epoch_id,),
    ).fetchall()
    unbound_rows = [row for row in all_unbound_rows if after < int(row[0]) <= through]
    unbound = [int(row[0]) for row in unbound_rows[:limit]]
    seed_rows = connection.execute(
        "SELECT m.source_identity,m.op_id,o.state FROM sync_migration_seed_map m "
        "LEFT JOIN sync_outbox o ON o.op_id=m.op_id "
        "WHERE m.epoch_id=? AND (m.op_id IS NULL OR o.op_id IS NULL "
        "OR o.state='queued') ORDER BY m.source_identity",
        (epoch_id,),
    ).fetchall()
    unready_seeds = [
        str(row[1] or row[0]) for row in seed_rows[:limit]
    ]
    state = migration_current(connection, epoch_id)
    writer_fenced = state is not None and state.get("writer_mode") == "fenced"
    frontier_stable = capture_frontier(connection) == through
    proved = bool(
        missing_count == 0
        and not unbound_rows
        and not seed_rows
        and frontier_stable
        and writer_fenced
    )
    return {
        "after_capture_seq": after,
        "captured_count": len(observed_rows),
        "epoch_id": epoch_id,
        "frontier_stable": frontier_stable,
        "missing_capture_count": missing_count,
        "missing_capture_seq": missing,
        "proved": proved,
        "through_capture_seq": through,
        "unbound_capture_count": len(unbound_rows),
        "unbound_capture_seq": unbound,
        "unready_seed_count": len(seed_rows),
        "unready_seed_ids": unready_seeds,
        "writer_fenced": writer_fenced,
    }


def record_fold_identity(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    evidence_digest: str,
    accepted_set_digest: str,
    operation_tree_digest: str,
    materialized_digest: str,
    reducer_version: str,
) -> dict[str, Any]:
    """Persist one deterministic full-fold identity before equality."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    for value, name in (
        (evidence_digest, "evidence_digest"),
        (accepted_set_digest, "accepted_set_digest"),
        (operation_tree_digest, "operation_tree_digest"),
        (materialized_digest, "materialized_digest"),
    ):
        _require_digest(value, name)
    if not isinstance(reducer_version, str) or not reducer_version:
        raise SyncInvariantError("fold reducer version is required")
    state = migration_current(connection, epoch_id)
    if state is None or state.get("evidence_digest") != evidence_digest:
        raise SyncInvariantError("fold identity is not bound to sealed evidence")
    identity = {
        "accepted_set_digest": accepted_set_digest,
        "epoch_id": epoch_id,
        "evidence_digest": evidence_digest,
        "materialized_digest": materialized_digest,
        "operation_tree_digest": operation_tree_digest,
        "reducer_version": reducer_version,
    }
    fold_digest = _digest(identity)
    existing = connection.execute(
        "SELECT evidence_digest,accepted_set_digest,operation_tree_digest,"
        "materialized_digest,reducer_version,fold_digest "
        "FROM sync_migration_fold WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    expected = (
        evidence_digest,
        accepted_set_digest,
        operation_tree_digest,
        materialized_digest,
        reducer_version,
        fold_digest,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise SyncInvariantError("fold identity retry is equivocal")
        return {"fold_digest": fold_digest, "idempotent": True}
    connection.execute(
        "INSERT INTO sync_migration_fold("
        "epoch_id,evidence_digest,accepted_set_digest,operation_tree_digest,"
        "materialized_digest,reducer_version,fold_digest) VALUES (?,?,?,?,?,?,?)",
        (epoch_id, *expected),
    )
    return {"fold_digest": fold_digest, "idempotent": False}


def record_equality_identity(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    evidence_digest: str,
    report_digests: Sequence[str],
    accepted_set_digest: str,
    operation_tree_digest: str,
    materialized_digest: str,
    authoritative_ref_oid: str,
) -> dict[str, Any]:
    """Seal the normalized equality identity, excluding local telemetry."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    for value, name in (
        (evidence_digest, "evidence_digest"),
        (accepted_set_digest, "accepted_set_digest"),
        (operation_tree_digest, "operation_tree_digest"),
        (materialized_digest, "materialized_digest"),
    ):
        _require_digest(value, name)
    reports = sorted(report_digests)
    if not reports or len(set(reports)) != len(reports):
        raise SyncInvariantError("equality report digests must be nonempty and unique")
    for report in reports:
        _require_digest(report, "report_digest")
    if not _GIT_OBJECT_ID.fullmatch(authoritative_ref_oid or ""):
        raise SyncInvariantError("equality requires an authoritative Git object ID")
    state = migration_current(connection, epoch_id)
    if state is None or state.get("evidence_digest") != evidence_digest:
        raise SyncInvariantError("equality evidence does not match the sealed epoch")
    report_set_digest = _digest({"report_digests": reports})
    identity = {
        "accepted_set_digest": accepted_set_digest,
        "authoritative_ref_oid": authoritative_ref_oid,
        "epoch_id": epoch_id,
        "evidence_digest": evidence_digest,
        "materialized_digest": materialized_digest,
        "operation_tree_digest": operation_tree_digest,
        "report_set_digest": report_set_digest,
    }
    equality_digest = _digest(identity)
    existing = connection.execute(
        "SELECT evidence_digest,report_set_digest,accepted_set_digest,"
        "operation_tree_digest,materialized_digest,authoritative_ref_oid,"
        "equality_digest FROM sync_migration_equality WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    expected = (
        evidence_digest,
        report_set_digest,
        accepted_set_digest,
        operation_tree_digest,
        materialized_digest,
        authoritative_ref_oid,
        equality_digest,
    )
    if existing is not None:
        if tuple(existing) != expected:
            raise SyncInvariantError("equality identity retry is equivocal")
        return {"equality_digest": equality_digest, "idempotent": True}
    connection.execute(
        "INSERT INTO sync_migration_equality("
        "epoch_id,evidence_digest,report_set_digest,accepted_set_digest,"
        "operation_tree_digest,materialized_digest,authoritative_ref_oid,"
        "equality_digest) VALUES (?,?,?,?,?,?,?,?)",
        (epoch_id, *expected),
    )
    return {"equality_digest": equality_digest, "idempotent": False}


def record_rollback_identity(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    equality_digest: str,
    bundle_digest: str,
    inventory_digest: str,
) -> dict[str, Any]:
    """Persist the immutable identity of a complete post-v2 rollback bundle."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    for value, name in (
        (equality_digest, "equality_digest"),
        (bundle_digest, "bundle_digest"),
        (inventory_digest, "inventory_digest"),
    ):
        _require_digest(value, name)
    equality = connection.execute(
        "SELECT equality_digest FROM sync_migration_equality WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if equality is None or equality[0] != equality_digest:
        raise SyncInvariantError("rollback bundle is not bound to sealed equality")
    existing = connection.execute(
        "SELECT equality_digest,bundle_digest,inventory_digest,state "
        "FROM sync_migration_rollback WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing[:3]) != (
            equality_digest,
            bundle_digest,
            inventory_digest,
        ) or existing[3] not in {"prepared", "applied", "closed"}:
            raise SyncInvariantError("rollback bundle retry is equivocal")
        return {"bundle_digest": bundle_digest, "idempotent": True}
    current = migration_status(connection, epoch_id)
    if (
        current["migration_state"] != "rollback-window"
        or current["writer_mode"] != "fenced"
        or current.get("rollback_bundle_digest") is not None
    ):
        raise SyncInvariantError(
            "rollback bundle requires the durable fenced rollback window"
        )
    connection.execute(
        "INSERT INTO sync_migration_rollback("
        "epoch_id,equality_digest,bundle_digest,inventory_digest) VALUES (?,?,?,?)",
        (epoch_id, equality_digest, bundle_digest, inventory_digest),
    )
    return {"bundle_digest": bundle_digest, "idempotent": False}


def rollback_apply_status(
    connection: sqlite3.Connection, epoch_id: str
) -> dict[str, Any]:
    """Return hash-verified per-target rollback application coverage."""

    if "sync_migration_rollback_targets" not in _table_names(connection):
        return {
            "applied_replica_ids": [],
            "bundle_digest": None,
            "complete": False,
            "missing_replica_ids": [],
            "state": None,
        }
    rollback = connection.execute(
        "SELECT bundle_digest,state FROM sync_migration_rollback WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    active = [
        str(row[0])
        for row in connection.execute(
            "SELECT replica_id FROM sync_migration_members "
            "WHERE epoch_id=? AND retired=0 ORDER BY replica_id",
            (epoch_id,),
        )
    ]
    applied: list[str] = []
    target_digests: list[str] = []
    for row in connection.execute(
        "SELECT replica_id,bundle_digest,target_manifest_digest,backup_digest,"
        "projection_digest,expect_state_digest,receipt_bytes,receipt_digest "
        "FROM sync_migration_rollback_targets WHERE epoch_id=? ORDER BY replica_id",
        (epoch_id,),
    ):
        raw = bytes(row[6])
        try:
            receipt = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncInvariantError("rollback target receipt is invalid JSON") from exc
        if not isinstance(receipt, dict) or _canonical_state_bytes(receipt) != raw:
            raise SyncInvariantError("rollback target receipt is not canonical")
        claimed = receipt.pop("receipt_digest", None)
        if (
            claimed != row[7]
            or _digest(receipt) != claimed
            or receipt.get("bundle_digest") != row[1]
            or receipt.get("target_manifest_digest") != row[2]
            or receipt.get("backup_digest") != row[3]
            or receipt.get("projection_digest") != row[4]
            or receipt.get("previous_state_digest") != row[5]
            or receipt.get("replica_id") != row[0]
        ):
            raise SyncInvariantError("rollback target receipt digest disagrees")
        applied.append(str(row[0]))
        target_digests.append(str(row[7]))
    missing = sorted(set(active) - set(applied))
    complete = bool(
        rollback is not None
        and active
        and not missing
        and set(applied) == set(active)
        and all(
            row[0] == rollback[0]
            for row in connection.execute(
                "SELECT bundle_digest FROM sync_migration_rollback_targets "
                "WHERE epoch_id=?",
                (epoch_id,),
            )
        )
    )
    return {
        "applied_replica_ids": applied,
        "bundle_digest": rollback[0] if rollback is not None else None,
        "complete": complete,
        "missing_replica_ids": missing,
        "state": rollback[1] if rollback is not None else None,
        "target_receipt_set_digest": _digest(
            {"target_receipt_digests": target_digests}
        ),
    }


def record_migration_failure(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    phase: str,
    reason: str,
) -> dict[str, Any]:
    """Persist one bounded operational failure without advancing cutover state."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if not isinstance(epoch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", epoch_id):
        raise SyncInvariantError("migration failure epoch is invalid")
    if not isinstance(phase, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{0,95}", phase
    ):
        raise SyncInvariantError("migration failure phase is invalid")
    if not isinstance(reason, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_.:-]{0,159}", reason
    ):
        raise SyncInvariantError("migration failure reason is invalid")
    state = migration_status(connection, epoch_id)
    connection.execute(
        "INSERT INTO sync_migration_failures(epoch_id,phase,reason,state_digest) "
        "VALUES (?,?,?,?) ON CONFLICT(epoch_id) DO UPDATE SET "
        "phase=excluded.phase,reason=excluded.reason,state_digest=excluded.state_digest,"
        f"failed_at={_NOW}",
        (epoch_id, phase, reason, state["state_digest"]),
    )
    return {
        "failed_at": connection.execute(
            "SELECT failed_at FROM sync_migration_failures WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()[0],
        "phase": phase,
        "reason": reason,
        "state_digest": state["state_digest"],
    }


def migration_diagnostic_status(
    connection: sqlite3.Connection,
    epoch_id: str | None = None,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """Return bounded, body-free cutover evidence for status and doctor."""

    limit = max(1, min(int(limit), 8))
    names = _table_names(connection)
    empty = {
        "epoch_id": None,
        "migration_state": "legacy",
        "membership_digest": None,
        "evidence_digest": None,
        "writer_mode": "legacy-capture",
        "replicas": [],
        "replicas_omitted": 0,
        "capture_seq": "0",
        "unconfirmed_outbox": 0,
        "equality": None,
        "rollback": None,
        "last_failure": None,
    }
    if "sync_migration_state" not in names:
        return empty
    if epoch_id is None:
        row = connection.execute(
            "SELECT epoch_id FROM sync_migration_state WHERE current=1"
        ).fetchone()
        epoch_id = None if row is None else str(row[0])
    if epoch_id is None:
        return empty
    state = migration_status(connection, epoch_id)
    member_rows = list(connection.execute(
        "SELECT replica_id,retired,evidence_digest FROM sync_migration_members "
        "WHERE epoch_id=? ORDER BY replica_id",
        (epoch_id,),
    )) if "sync_migration_members" in names else []
    evidence_by_replica: dict[str, set[str]] = {}
    if "sync_migration_attestations" in names:
        for replica_id, kind in connection.execute(
            "SELECT replica_id,kind FROM sync_migration_attestations "
            "WHERE epoch_id=? ORDER BY replica_id,kind",
            (epoch_id,),
        ):
            evidence_by_replica.setdefault(str(replica_id), set()).add(str(kind))
    if "sync_migration_artifacts" in names:
        for replica_id, kind in connection.execute(
            "SELECT replica_id,artifact_kind FROM sync_migration_artifacts "
            "WHERE epoch_id=? AND replica_id<>'' ORDER BY replica_id,artifact_kind",
            (epoch_id,),
        ):
            evidence_by_replica.setdefault(str(replica_id), set()).add(str(kind))
    phase_order = (
        "membership", "snapshot", "graveyard", "snapshot-seed", "fence",
        "barrier", "delta", "delta-seed", "no-tail", "evidence",
        "equality", "activation", "rollback",
    )
    replicas = []
    for replica_id, retired, evidence_digest in member_rows[:limit]:
        kinds = evidence_by_replica.get(str(replica_id), set())
        latest = next((kind for kind in reversed(phase_order) if kind in kinds),
                      "membership")
        replicas.append({
            "replica_id": str(replica_id),
            "retired": bool(retired),
            "phase": latest,
            "evidence_digest": evidence_digest,
            "evidence_kinds": sorted(kinds)[:limit],
        })
    capture_seq = "0"
    if "sync_capture_clock" in names:
        row = connection.execute(
            "SELECT capture_seq FROM sync_capture_clock WHERE singleton=1"
        ).fetchone()
        if row is not None:
            capture_seq = str(row[0])
    unconfirmed = 0
    if "sync_outbox" in names:
        unconfirmed = int(connection.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE state<>'confirmed'"
        ).fetchone()[0])
    equality = None
    if "sync_migration_equality" in names:
        row = connection.execute(
            "SELECT equality_digest,recorded_at FROM sync_migration_equality "
            "WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        if row is not None:
            equality = {"digest": str(row[0]), "recorded_at": str(row[1])}
    rollback = (rollback_apply_status(connection, epoch_id)
                if "sync_migration_rollback" in names else None)
    last_failure = None
    if "sync_migration_failures" in names:
        row = connection.execute(
            "SELECT phase,reason,state_digest,failed_at "
            "FROM sync_migration_failures WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        if row is not None:
            last_failure = {
                "phase": str(row[0]),
                "reason": str(row[1]),
                "state_digest": str(row[2]),
                "failed_at": str(row[3]),
            }
    return {
        "epoch_id": epoch_id,
        "migration_state": state["migration_state"],
        "state_digest": state["state_digest"],
        "membership_digest": state.get("membership_digest"),
        "evidence_digest": state.get("evidence_digest"),
        "writer_mode": state["writer_mode"],
        "replicas": replicas,
        "replicas_omitted": max(0, len(member_rows) - len(replicas)),
        "capture_seq": capture_seq,
        "unconfirmed_outbox": unconfirmed,
        "equality": equality,
        "rollback": rollback,
        "last_failure": last_failure,
    }


def record_rollback_apply(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    expect_digest: str,
    bundle_digest: str,
    target_replica_id: str,
    target_manifest_digest: str,
    backup_digest: str,
    projection_digest: str,
) -> dict[str, Any]:
    """Record one target install under the prepared bundle's CAS identity."""

    _require_transaction(connection)
    ensure_sync_schema(connection)
    expect_digest = _require_digest(expect_digest, "expect_digest")
    bundle_digest = _require_digest(bundle_digest, "bundle_digest")
    target_replica_id = _validate_replica_id(target_replica_id)
    for value, name in (
        (target_manifest_digest, "target_manifest_digest"),
        (backup_digest, "backup_digest"),
        (projection_digest, "projection_digest"),
    ):
        _require_digest(value, name)
    current = migration_status(connection, epoch_id)
    if current["state_digest"] != expect_digest:
        raise SyncInvariantError("rollback target state digest CAS failed")
    if (
        current["migration_state"] != "rollback-window"
        or current["writer_mode"] != "fenced"
    ):
        raise SyncInvariantError("rollback target requires its prepared bundle window")
    rollback = connection.execute(
        "SELECT bundle_digest,state FROM sync_migration_rollback WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if rollback is None or rollback[0] != bundle_digest or rollback[1] not in {
        "prepared",
        "applied",
    }:
        raise SyncInvariantError("rollback bundle is not prepared for target apply")
    member = connection.execute(
        "SELECT retired FROM sync_migration_members WHERE epoch_id=? AND replica_id=?",
        (epoch_id, target_replica_id),
    ).fetchone()
    if member is None or bool(member[0]):
        raise SyncInvariantError("rollback target is not an active sealed member")
    local = connection.execute(
        "SELECT replica_id FROM sync_replica WHERE active=1"
    ).fetchone()
    if local is None or str(local[0]) != target_replica_id:
        raise SyncInvariantError("rollback apply target is not the local replica")
    payload = {
        "backup_digest": backup_digest,
        "bundle_digest": bundle_digest,
        "changed": True,
        "epoch_id": epoch_id,
        "migration_state": "rollback-window",
        "phase": "rollback.apply",
        "previous_state_digest": expect_digest,
        "projection_digest": projection_digest,
        "protocol_major": PROTOCOL_MAJOR,
        "replica_id": target_replica_id,
        "schema_version": 1,
        "status": "local-only",
        "target_manifest_digest": target_manifest_digest,
    }
    receipt_digest = _digest(payload)
    receipt = {**payload, "receipt_digest": receipt_digest}
    receipt_bytes = _canonical_state_bytes(receipt)
    existing = connection.execute(
        "SELECT bundle_digest,target_manifest_digest,backup_digest,"
        "projection_digest,expect_state_digest,receipt_bytes,receipt_digest "
        "FROM sync_migration_rollback_targets WHERE epoch_id=? AND replica_id=?",
        (epoch_id, target_replica_id),
    ).fetchone()
    expected = (
        bundle_digest,
        target_manifest_digest,
        backup_digest,
        projection_digest,
        expect_digest,
        receipt_bytes,
        receipt_digest,
    )
    if existing is not None:
        if (*existing[:5], bytes(existing[5]), existing[6]) != expected:
            raise SyncInvariantError("rollback target apply retry is equivocal")
        return json.loads(bytes(existing[5]))
    connection.execute(
        "INSERT INTO sync_migration_rollback_targets("
        "epoch_id,replica_id,bundle_digest,target_manifest_digest,backup_digest,"
        "projection_digest,expect_state_digest,receipt_bytes,receipt_digest) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            target_replica_id,
            bundle_digest,
            target_manifest_digest,
            backup_digest,
            projection_digest,
            expect_digest,
            sqlite3.Binary(receipt_bytes),
            receipt_digest,
        ),
    )
    status = rollback_apply_status(connection, epoch_id)
    if status["complete"]:
        connection.execute(
            "UPDATE sync_migration_rollback SET state='applied' "
            "WHERE epoch_id=? AND state='prepared'",
            (epoch_id,),
        )
    return receipt


def record_rollback_apply_receipt(
    connection: sqlite3.Connection,
    *,
    epoch_id: str,
    receipt: bytes | Mapping[str, Any],
) -> dict[str, Any]:
    """Import one other replica's canonical rollback-apply receipt.

    The receipt remains bound to the shared fenced rollback-window state and
    prepared bundle.  Exact retries are harmless; a second receipt for the
    same target with any changed install identity is rejected as equivocation.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    if isinstance(receipt, bytes):
        receipt_bytes = receipt
        try:
            parsed = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncInvariantError("rollback apply receipt is invalid JSON") from exc
        if not isinstance(parsed, dict) or _canonical_state_bytes(parsed) != receipt_bytes:
            raise SyncInvariantError("rollback apply receipt is not canonical")
        value = parsed
    elif isinstance(receipt, Mapping):
        value = dict(receipt)
        receipt_bytes = _canonical_state_bytes(value)
    else:
        raise SyncInvariantError("rollback apply receipt must be bytes or a mapping")

    required_keys = {
        "backup_digest",
        "bundle_digest",
        "changed",
        "epoch_id",
        "migration_state",
        "phase",
        "previous_state_digest",
        "projection_digest",
        "protocol_major",
        "receipt_digest",
        "replica_id",
        "schema_version",
        "status",
        "target_manifest_digest",
    }
    if set(value) != required_keys:
        raise SyncInvariantError("rollback apply receipt fields disagree")
    if (
        value.get("changed") is not True
        or value.get("epoch_id") != epoch_id
        or value.get("migration_state") != "rollback-window"
        or value.get("phase") != "rollback.apply"
        or value.get("protocol_major") != PROTOCOL_MAJOR
        or type(value.get("protocol_major")) is not int
        or value.get("schema_version") != 1
        or type(value.get("schema_version")) is not int
        or value.get("status") != "local-only"
    ):
        raise SyncInvariantError("rollback apply receipt protocol fields disagree")
    replica_id = _validate_replica_id(value.get("replica_id"))
    bundle_digest = _require_digest(value.get("bundle_digest"), "bundle_digest")
    expect_digest = _require_digest(
        value.get("previous_state_digest"), "previous_state_digest"
    )
    target_manifest_digest = _require_digest(
        value.get("target_manifest_digest"), "target_manifest_digest"
    )
    backup_digest = _require_digest(value.get("backup_digest"), "backup_digest")
    projection_digest = _require_digest(
        value.get("projection_digest"), "projection_digest"
    )
    claimed_digest = _require_digest(value.get("receipt_digest"), "receipt_digest")
    payload = dict(value)
    payload.pop("receipt_digest")
    if _digest(payload) != claimed_digest:
        raise SyncInvariantError("rollback apply receipt digest disagrees")

    current = migration_status(connection, epoch_id)
    if (
        current["state_digest"] != expect_digest
        or current["migration_state"] != "rollback-window"
        or current["writer_mode"] != "fenced"
        or current.get("rollback_bundle_digest") is not None
    ):
        raise SyncInvariantError("rollback apply receipt state binding disagrees")
    rollback = connection.execute(
        "SELECT bundle_digest,state FROM sync_migration_rollback WHERE epoch_id=?",
        (epoch_id,),
    ).fetchone()
    if rollback is None or rollback[0] != bundle_digest or rollback[1] not in {
        "prepared",
        "applied",
    }:
        raise SyncInvariantError("rollback apply receipt bundle is not prepared")
    member = connection.execute(
        "SELECT retired FROM sync_migration_members WHERE epoch_id=? AND replica_id=?",
        (epoch_id, replica_id),
    ).fetchone()
    if member is None or bool(member[0]):
        raise SyncInvariantError("rollback apply receipt target is not in the active roster")

    existing = connection.execute(
        "SELECT bundle_digest,target_manifest_digest,backup_digest,"
        "projection_digest,expect_state_digest,receipt_bytes,receipt_digest "
        "FROM sync_migration_rollback_targets WHERE epoch_id=? AND replica_id=?",
        (epoch_id, replica_id),
    ).fetchone()
    expected = (
        bundle_digest,
        target_manifest_digest,
        backup_digest,
        projection_digest,
        expect_digest,
        receipt_bytes,
        claimed_digest,
    )
    if existing is not None:
        if (*existing[:5], bytes(existing[5]), existing[6]) != expected:
            raise SyncInvariantError("rollback apply receipt retry is equivocal")
        return dict(value)
    connection.execute(
        "INSERT INTO sync_migration_rollback_targets("
        "epoch_id,replica_id,bundle_digest,target_manifest_digest,backup_digest,"
        "projection_digest,expect_state_digest,receipt_bytes,receipt_digest) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            epoch_id,
            replica_id,
            bundle_digest,
            target_manifest_digest,
            backup_digest,
            projection_digest,
            expect_digest,
            sqlite3.Binary(receipt_bytes),
            claimed_digest,
        ),
    )
    status = rollback_apply_status(connection, epoch_id)
    if status["complete"]:
        connection.execute(
            "UPDATE sync_migration_rollback SET state='applied' "
            "WHERE epoch_id=? AND state='prepared'",
            (epoch_id,),
        )
    return dict(value)


def register_writer_functions(
    connection: sqlite3.Connection,
    *,
    protocol_major: int = PROTOCOL_MAJOR,
    cutover_authority: bool = False,
) -> None:
    """Register per-connection identity used by persistent fence triggers."""

    if isinstance(protocol_major, bool) or not isinstance(protocol_major, int):
        raise SyncInvariantError("writer protocol major must be an integer")
    connection.create_function(
        "hearting_writer_protocol_major", 0, lambda: protocol_major
    )
    connection.create_function(
        "hearting_cutover_authority", 0, lambda: 1 if cutover_authority else 0
    )


def writer_capability(
    connection: sqlite3.Connection, *, protocol_major: int = PROTOCOL_MAJOR
) -> dict[str, Any]:
    """Return the current semantic-writer decision without mutating state."""

    row = None
    if "sync_migration_state" in _table_names(connection):
        row = connection.execute(
            "SELECT epoch_id,phase,writer_mode,state_digest "
            "FROM sync_migration_state WHERE current=1"
        ).fetchone()
    if row is None:
        mode, epoch_id, phase, state_digest = "legacy-capture", None, "legacy", None
    else:
        epoch_id, phase, mode, state_digest = row
    if mode == "fenced":
        allowed, reason = False, "writer-fenced"
    elif mode == "read-only-unsupported":
        allowed, reason = False, "writer-protocol-unsupported"
    elif mode == "v2" and protocol_major < PROTOCOL_MAJOR:
        allowed, reason = False, "writer-protocol-unsupported"
    elif mode == "legacy-capture" and protocol_major > PROTOCOL_MAJOR:
        allowed, reason = False, "writer-protocol-unsupported"
    else:
        allowed, reason = True, None
    return {
        "allowed": allowed,
        "epoch_id": epoch_id,
        "exit_code": 0 if allowed else 2,
        "migration_state": phase,
        "reason": reason,
        "state_digest": state_digest,
        "status": "local-only" if allowed else "hard-failure",
        "writer_mode": mode,
        "writer_protocol_major": protocol_major,
    }


def require_writer_allowed(
    connection: sqlite3.Connection, *, protocol_major: int = PROTOCOL_MAJOR
) -> dict[str, Any]:
    result = writer_capability(connection, protocol_major=protocol_major)
    if not result["allowed"]:
        raise RemoteSafetyError(str(result["reason"]))
    return result


def install_writer_fence(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    semantic_tables: Sequence[str] = ("records",),
) -> dict[str, Any]:
    """Install fail-closed semantic triggers after the fenced state CAS.

    Old binaries do not register the named SQLite functions, so their writes
    fail during trigger evaluation.  A migration connection explicitly
    registered with ``cutover_authority=True`` can still fold/install state.
    """

    _require_transaction(connection)
    ensure_sync_schema(connection)
    state = migration_current(connection, epoch_id)
    if (
        state is None
        or _phase_index(str(state["migration_state"]))
        < _phase_index("barrier-held")
        or state["writer_mode"] not in {"fenced", "v2"}
    ):
        raise SyncInvariantError("writer fence requires a durable held barrier")
    installed: list[str] = []
    names = _table_names(connection)
    for table in sorted(set(semantic_tables)):
        if not _IDENTIFIER.fullmatch(table):
            raise SyncInvariantError(f"unsafe semantic table name: {table!r}")
        if table not in names:
            continue
        for action in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"sync_cutover_{table}_{action.lower()}"
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS "{trigger}" '
                f'BEFORE {action} ON "{table}" '
                "WHEN hearting_cutover_authority() <> 1 AND ("
                "(SELECT writer_mode FROM sync_migration_state WHERE current=1) "
                "IN ('fenced','read-only-unsupported') OR ("
                "(SELECT writer_mode FROM sync_migration_state WHERE current=1)='v2' "
                "AND hearting_writer_protocol_major() < 2)) "
                "BEGIN SELECT RAISE(ABORT, 'writer-fenced'); END"
            )
            installed.append(trigger)
    return {"epoch_id": epoch_id, "triggers": installed, "writer_mode": state["writer_mode"]}


def remove_writer_fence(
    connection: sqlite3.Connection,
    evidence: TrustedMigrationEvidence,
    *,
    semantic_tables: Sequence[str] = ("records",),
) -> dict[str, Any]:
    """Remove triggers only with a DB-issued, closed rollback proof."""

    _require_transaction(connection)
    if not isinstance(evidence, TrustedMigrationEvidence) or not evidence._is_trusted():
        raise RemoteSafetyError("writer fence removal requires trusted rollback evidence")
    if evidence.get("kind") != "rollback" or evidence.get("migration_state") != "closed":
        raise RemoteSafetyError("writer fence removal requires a closed rollback epoch")
    removed: list[str] = []
    for table in sorted(set(semantic_tables)):
        if not _IDENTIFIER.fullmatch(table):
            raise SyncInvariantError(f"unsafe semantic table name: {table!r}")
        for action in ("insert", "update", "delete"):
            trigger = f"sync_cutover_{table}_{action}"
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
            removed.append(trigger)
    return {"epoch_id": evidence["epoch_id"], "triggers": removed}


def trusted_migration_evidence(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    kind: str,
) -> TrustedMigrationEvidence:
    """Re-issue a typed proof only from the required durable terminal rows."""

    state = migration_current(connection, epoch_id)
    if state is None:
        raise RemoteSafetyError("migration epoch has no durable state")
    current_index = _phase_index(str(state["migration_state"]))
    required = {
        "membership": "membership-sealed",
        "fence": "old-writers-fenced",
        "evidence": "evidence-sealed",
        "seed-seal": "equality-proven",
        "equality": "equality-proven",
        "rollback": "closed",
    }
    if kind not in required or current_index < _phase_index(required[kind]):
        raise RemoteSafetyError(f"durable {kind} migration evidence is unavailable")
    claims: dict[str, Any] = {
        "epoch_id": epoch_id,
        "kind": kind,
        "last_receipt_digest": state["last_receipt_digest"],
        "membership_digest": state["membership_digest"],
        "migration_state": state["migration_state"],
        "state_digest": state["state_digest"],
    }
    if kind in {"evidence", "seed-seal", "equality", "rollback"}:
        claims["evidence_digest"] = state.get("evidence_digest")
    if kind in {"seed-seal", "equality", "rollback"}:
        equality = connection.execute(
            "SELECT accepted_set_digest,materialized_digest,equality_digest "
            "FROM sync_migration_equality WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        if equality is None or equality[2] != state.get("equality_digest"):
            raise RemoteSafetyError("equality identity is absent or disagrees with state")
        no_tail = [
            str(row[0])
            for row in connection.execute(
                "SELECT manifest_digest FROM sync_migration_attestations "
                "WHERE epoch_id=? AND kind='no-tail' ORDER BY replica_id",
                (epoch_id,),
            )
        ]
        if not no_tail:
            raise RemoteSafetyError("no-tail attestations are absent")
        claims.update(
            {
                "accepted_set_digest": str(equality[0]),
                "equality_digest": str(equality[2]),
                "materialized_digest": str(equality[1]),
                "no_tail_digest": _digest({"no_tail_attestations": no_tail}),
            }
        )
    if kind == "rollback":
        rollback = connection.execute(
            "SELECT bundle_digest,inventory_digest,state FROM sync_migration_rollback "
            "WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        applied = rollback_apply_status(connection, epoch_id)
        if (
            rollback is None
            or state.get("writer_mode") != "fenced"
            or rollback[2] != "closed"
            or not applied["complete"]
            or applied["bundle_digest"] != rollback[0]
        ):
            raise RemoteSafetyError("closed rollback target evidence is absent")
        claims.update(
            {
                "applied_replica_ids": applied["applied_replica_ids"],
                "bundle_digest": str(rollback[0]),
                "inventory_digest": str(rollback[1]),
                "target_receipt_set_digest": applied["target_receipt_set_digest"],
            }
        )
    claims["proof_digest"] = _digest(claims)
    return TrustedMigrationEvidence(claims, _TRUSTED_MIGRATION_TOKEN)


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
    cutover = None
    if "sync_migration_state" in names:
        cutover = connection.execute(
            "SELECT phase,writer_mode,state_digest FROM sync_migration_state "
            "WHERE current=1"
        ).fetchone()
    cutover_ready = cutover is None or (
        _phase_index(str(cutover[0])) >= _phase_index("v2-only-enabled")
        and str(cutover[1]) == "v2"
    )
    semantic_state_covered = schema_ready and _semantic_state_covered(connection, names)
    result.update(
        {
            "schema_ready": schema_ready,
            "legacy_nonempty": legacy_nonempty,
            "object_count": object_count,
            "fresh_candidate": fresh_candidate,
            "seed_ready": seed_ready,
            "fence_ready": fence_ready,
            "cutover_state": str(cutover[0]) if cutover is not None else None,
            "cutover_state_digest": str(cutover[2]) if cutover is not None else None,
            "cutover_writer_mode": str(cutover[1]) if cutover is not None else None,
            "semantic_state_covered": semantic_state_covered,
            "remote_allowed": bool(
                schema_ready
                and seed_ready
                and fence_ready
                and cutover_ready
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
    evidence: TrustedMigrationEvidence | None = None,
) -> dict[str, Any]:
    """Seal a snapshot epoch only from DB-issued equality/no-tail evidence."""

    _require_transaction(connection)
    if not operator_authorized:
        raise RemoteSafetyError("snapshot seed sealing requires operator authority")
    if not isinstance(evidence, TrustedMigrationEvidence) or not evidence._is_trusted():
        raise RemoteSafetyError("snapshot seed sealing requires DB-issued evidence")
    if evidence.get("kind") != "seed-seal" or evidence.get("epoch_id") != epoch_id:
        raise RemoteSafetyError("snapshot seed evidence does not match the epoch")
    supplied = (no_tail_digest, accepted_set_digest, materialized_digest)
    proved = (
        evidence.get("no_tail_digest"),
        evidence.get("accepted_set_digest"),
        evidence.get("materialized_digest"),
    )
    if supplied != proved:
        raise RemoteSafetyError("snapshot seed digests disagree with DB-issued evidence")
    row = connection.execute(
        "SELECT state,seed_mode,seed_sealed FROM sync_migration_epoch "
        "WHERE epoch_id=? AND current=1",
        (epoch_id,),
    ).fetchone()
    if row is None or row[1] != "snapshot":
        raise RemoteSafetyError("snapshot seed epoch is not current")
    if row[0] == "sealed" and bool(row[2]):
        existing = connection.execute(
            "SELECT no_tail_digest,accepted_set_digest,materialized_digest "
            "FROM sync_migration_epoch WHERE epoch_id=?",
            (epoch_id,),
        ).fetchone()
        if tuple(existing) != supplied:
            raise RemoteSafetyError("sealed snapshot epoch disagrees with retry")
        return bootstrap_state(connection)
    if row[0] != "seeding" or bool(row[2]):
        raise RemoteSafetyError("snapshot seed epoch is not sealable")
    connection.execute(
        f"UPDATE sync_migration_epoch SET state='sealed',seed_sealed=1,"
        "no_tail_digest=?,accepted_set_digest=?,materialized_digest=?,"
        f"sealed_at={_NOW} WHERE epoch_id=? AND current=1",
        (no_tail_digest, accepted_set_digest, materialized_digest, epoch_id),
    )
    return bootstrap_state(connection)


def activate_v2_only_fence(
    connection: sqlite3.Connection,
    epoch_id: str,
    *,
    fence_proof: str,
    operator_authorized: bool = False,
    evidence: TrustedMigrationEvidence | None = None,
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
    if row[1] == "snapshot":
        if (
            not isinstance(evidence, TrustedMigrationEvidence)
            or not evidence._is_trusted()
            or evidence.get("kind") not in {"seed-seal", "equality"}
            or evidence.get("epoch_id") != epoch_id
        ):
            raise RemoteSafetyError(
                "snapshot fence activation requires DB-issued equality evidence"
            )
        cutover = migration_current(connection, epoch_id)
        if (
            cutover is None
            or _phase_index(str(cutover["migration_state"]))
            < _phase_index("v2-only-enabled")
            or cutover["writer_mode"] != "v2"
        ):
            raise RemoteSafetyError(
                "snapshot fence activation requires the v2-only cutover state"
            )
    elif row[1] != "fresh":
        raise RemoteSafetyError("unsupported seed mode for v2-only activation")
    state = bootstrap_state(connection)
    if row[1] == "fresh" and (state["legacy_nonempty"] or state["object_count"] != 0):
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
    elif state.get("cutover_state") is not None and (
        state.get("cutover_writer_mode") != "v2"
        or _phase_index(str(state["cutover_state"])) < _phase_index("v2-only-enabled")
    ):
        reason = "operational-cutover-not-v2-only"
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
    migration = migration_diagnostic_status(connection, limit=limit)
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
        "migration": migration,
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
ensure_migration_schema = ensure_sync_schema
assert_writer_allowed = require_writer_allowed


__all__ = [
    "MAX_COUNTER",
    "OUTBOX_STATES",
    "PROTOCOL_MAJOR",
    "RemoteSafetyError",
    "STATUS_VALUES",
    "SyncError",
    "SyncInvariantError",
    "TrustedBootstrapEvidence",
    "TrustedMigrationEvidence",
    "MIGRATION_PHASES",
    "WRITER_MODES",
    "activate_v2_only_fence",
    "allocate_counter",
    "assert_writer_allowed",
    "bind_seed_operation",
    "bind_captured_operation",
    "bootstrap_state",
    "capture_frontier",
    "captured_operations",
    "ensure_replica_identity",
    "ensure_migration_schema",
    "ensure_sync_schema",
    "initialize_fresh_v2_epoch",
    "install_writer_fence",
    "migration_current",
    "migration_diagnostic_status",
    "migration_attestation",
    "migration_artifacts",
    "migration_receipt",
    "migration_status",
    "migration_transition",
    "record_equality_identity",
    "record_fold_identity",
    "record_graveyard_evidence",
    "record_local_operation",
    "record_migration_attestation",
    "record_migration_artifact",
    "record_migration_failure",
    "record_migration_seal",
    "record_captured_delta_operation",
    "record_reserved_seed_operation",
    "record_rollback_identity",
    "record_rollback_apply",
    "record_rollback_apply_receipt",
    "record_seed_epoch",
    "register_writer_functions",
    "remote_policy",
    "remote_readiness",
    "remove_writer_fence",
    "require_remote_ready",
    "require_writer_allowed",
    "reserve_seed_counters",
    "rollback_apply_status",
    "rotate_replica_identity",
    "seal_seed_epoch",
    "sync_status",
    "trusted_bootstrap_evidence",
    "trusted_migration_evidence",
    "transition_outbox",
    "no_tail_status",
    "writer_capability",
]
