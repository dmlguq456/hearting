#!/usr/bin/env python3
"""Hermetic transaction and artifact primitives for protocol-v2 migration.

The module performs no discovery, network, Git, or credential work.  Callers
must explicitly name a SQLite database and an absolute contained output path.
Dry runs create neither SQLite schema nor files; apply operations are CAS or
content-addressed and idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

PROTOCOL_MAJOR = 2
MANIFEST_VERSION = 1
CANONICALIZER_VERSION = 1
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
REPLICA_RE = re.compile(r"^[0-9a-f]{32,128}$")
EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{31,127}$")

MIGRATION_STATES = (
    "legacy", "membership-sealed", "capture-enabled", "snapshots-sealed",
    "seeds-built", "fence-armed", "barrier-held", "old-writers-fenced",
    "deltas-drained", "no-tail-proven", "evidence-sealed",
    "seeds-published", "folded", "equality-proven", "v2-only-enabled",
    "rollback-window", "closed",
)
_STATE_INDEX = {name: index for index, name in enumerate(MIGRATION_STATES)}


class MigrationError(ValueError):
    """Fail-closed migration error with a stable machine reason."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise MigrationError("non-canonical-json", str(exc)) from exc


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def _require_epoch(value: str) -> str:
    if not isinstance(value, str) or not EPOCH_RE.fullmatch(value):
        raise MigrationError("epoch-id-invalid")
    return value


def _require_replica(value: str) -> str:
    if not isinstance(value, str) or not REPLICA_RE.fullmatch(value):
        raise MigrationError("replica-id-invalid")
    return value


def _require_digest(value: Any, reason: str = "digest-invalid") -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise MigrationError(reason)
    return value


def _with_digest(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("manifest_digest", None)
    result["manifest_digest"] = digest_json(result)
    return result


def _verify_digest(value: Mapping[str, Any]) -> None:
    claimed = _require_digest(value.get("manifest_digest"),
                              "manifest-digest-invalid")
    payload = dict(value)
    payload.pop("manifest_digest", None)
    if digest_json(payload) != claimed:
        raise MigrationError("manifest-digest-mismatch")


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MigrationError("artifact-path-invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError("artifact-path-escape")
    return path


def _output_root(value: str | os.PathLike[str], *, create: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise MigrationError("output-must-be-absolute")
    parent = path.parent.resolve(strict=True)
    result = parent / path.name
    if result.exists() and result.is_symlink():
        raise MigrationError("output-symlink-forbidden")
    existed = result.exists()
    if create:
        result.mkdir(mode=0o700, exist_ok=True)
        if not existed:
            _fsync_dir(result)
            _fsync_dir(parent)
    if not result.is_dir() or result.is_symlink():
        raise MigrationError("output-not-directory")
    return result


def _contained(root: Path, relative: str) -> Path:
    result = root.joinpath(*_relative(relative).parts)
    cursor = root
    for part in _relative(relative).parts[:-1]:
        cursor /= part
        if cursor.exists() and (cursor.is_symlink() or not cursor.is_dir()):
            raise MigrationError("artifact-parent-unsafe")
    if result.exists() and result.is_symlink():
        raise MigrationError("artifact-symlink-forbidden")
    return result


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise MigrationError("artifact-not-regular")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _atomic_write(path: Path, raw: bytes) -> bool:
    missing = []
    cursor = path.parent
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_dir(directory)
        _fsync_dir(directory.parent)
    if path.exists():
        if _read_file(path) != raw:
            raise MigrationError("artifact-equivocation")
        return False
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise MigrationError("artifact-write-incomplete")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _fsync_dir(path.parent)
        if _read_file(path) != raw:
            raise MigrationError("artifact-reopen-mismatch")
        return True
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def load_manifest(value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
        # ``changed`` is an invocation receipt field, never part of a durable
        # cross-replica manifest or its digest.
        result.pop("changed", None)
        return result
    raw = _read_file(Path(value))
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("manifest-json-invalid", str(exc)) from exc
    if not isinstance(parsed, dict) or canonical_bytes(parsed) != raw:
        raise MigrationError("manifest-not-canonical")
    return parsed


def state_digest(epoch_id: str, migration_state: str,
                 membership_digest: str | None = None,
                 evidence_digest: str | None = None,
                 *, phase_seq: int | None = None,
                 writer_mode: str = "legacy-capture",
                 fence_capture_seq: int | None = None,
                 equality_digest: str | None = None,
                 rollback_bundle_digest: str | None = None) -> str:
    """Return the authoritative sync-state digest (not a second state model).

    The default arguments intentionally reproduce ``sync_v2``'s synthetic
    pre-row legacy state.  Later-state callers should normally consume
    ``MigrationEngine.current()['state_digest']`` rather than reconstruct it.
    """
    _require_epoch(epoch_id)
    if migration_state not in _STATE_INDEX:
        raise MigrationError("migration-state-invalid")
    if membership_digest is not None:
        _require_digest(membership_digest, "membership-digest-invalid")
    if evidence_digest is not None:
        _require_digest(evidence_digest, "evidence-digest-invalid")
    if phase_seq is None: phase_seq = _STATE_INDEX[migration_state]
    return digest_json({"epoch_id": epoch_id, "equality_digest": equality_digest,
        "evidence_digest": evidence_digest, "fence_capture_seq": fence_capture_seq,
        "membership_digest": membership_digest, "migration_state": migration_state,
        "phase_seq": phase_seq, "protocol_major": 2,
        "rollback_bundle_digest": rollback_bundle_digest,
        "writer_mode": writer_mode})


def _sync_module():
    try:
        import sync_v2  # type: ignore
    except ImportError:  # pragma: no cover - package-style import fallback
        from . import sync_v2  # type: ignore
    return sync_v2


def _protocol_module():
    try:
        import protocol_v2  # type: ignore
    except ImportError:  # pragma: no cover - package-style import fallback
        from . import protocol_v2  # type: ignore
    return protocol_v2


class MigrationEngine:
    """Thin transaction owner over the single authoritative ``sync_v2`` CAS."""

    def __init__(self, connection: sqlite3.Connection, epoch_id: str) -> None:
        self.connection = connection
        self.epoch_id = _require_epoch(epoch_id)

    def current(self) -> dict[str, Any]:
        return dict(_sync_module().migration_status(self.connection, self.epoch_id))

    @staticmethod
    def _check_transition(current: Mapping[str, Any], target: str,
                          membership: str | None, evidence: str | None) -> None:
        state = str(current["migration_state"])
        if target not in _STATE_INDEX or _STATE_INDEX[target] != _STATE_INDEX[state] + 1:
            raise MigrationError("transition-predecessor-invalid")
        if state == "legacy":
            if target != "membership-sealed" or membership is None:
                raise MigrationError("membership-seal-required")
        elif membership is not None and membership != current["membership_digest"]:
            raise MigrationError("membership-drift")
        if target == "evidence-sealed" and evidence is None:
            raise MigrationError("evidence-seal-required")
        if target != "evidence-sealed" and evidence is not None \
                and evidence != current["evidence_digest"]:
            raise MigrationError("evidence-transition-invalid")

    def transition(self, phase: str, target_state: str, inputs: Mapping[str, Any],
                   *, expect: str, apply: bool = False,
                   membership_digest: str | None = None,
                   evidence_digest: str | None = None,
                   writer_mode: str | None = None,
                   fence_capture_seq: int | None = None,
                   equality_digest: str | None = None,
                   rollback_bundle_digest: str | None = None) -> dict[str, Any]:
        if not phase or len(phase.encode()) > 128:
            raise MigrationError("phase-invalid")
        _require_digest(expect, "expected-state-digest-invalid")
        if membership_digest is not None:
            _require_digest(membership_digest, "membership-digest-invalid")
        if evidence_digest is not None:
            _require_digest(evidence_digest, "evidence-digest-invalid")
        input_digests = {str(key): (value if isinstance(value, str)
                         and HEX64_RE.fullmatch(value) else digest_json(value))
                         for key, value in sorted(inputs.items())}
        input_digest = digest_json(input_digests)
        if not apply:
            current = self.current()
            if current["state_digest"] != expect:
                raise MigrationError("stale-state")
            self._check_transition(current, target_state, membership_digest,
                                   evidence_digest)
            if target_state == "old-writers-fenced" \
                    and (writer_mode != "fenced" or fence_capture_seq is None):
                raise MigrationError("fence-proof-required")
            if target_state == "equality-proven" and equality_digest is None:
                raise MigrationError("equality-digest-required")
            if target_state == "v2-only-enabled" and writer_mode != "v2":
                raise MigrationError("v2-writer-mode-required")
            receipt = self._receipt(phase, current, current["migration_state"],
                                    current["state_digest"], input_digests,
                                    membership_digest, evidence_digest, False,
                                    target_state)
            return receipt
        if self.connection.in_transaction:
            raise MigrationError("caller-transaction-active")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            receipt = _sync_module().migration_transition(
                self.connection, epoch_id=self.epoch_id, phase=phase,
                target_state=target_state, expect_digest=expect,
                input_digest=input_digest, membership_digest=membership_digest,
                evidence_digest=evidence_digest, writer_mode=writer_mode,
                fence_capture_seq=fence_capture_seq,
                equality_digest=equality_digest,
                rollback_bundle_digest=rollback_bundle_digest)
            self.connection.commit()
            return dict(receipt)
        except Exception:
            self.connection.rollback()
            raise

    def _receipt(self, phase: str, previous: Mapping[str, Any], state: str,
                 current_digest: str, input_digests: Mapping[str, str],
                 membership: str | None, evidence: str | None, changed: bool,
                 planned: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1, "protocol_major": 2, "epoch_id": self.epoch_id,
            "phase": phase, "migration_state": state,
            "membership_digest": membership or previous["membership_digest"],
            "evidence_digest": evidence or previous["evidence_digest"],
            "status": "local-only" if changed else "planned",
            "reason": "ok" if changed else "apply-required",
            "required_action": "none" if changed else "rerun-with-apply-and-same-expect",
            "changed": changed, "previous_state_digest": previous["state_digest"],
            "current_state_digest": current_digest,
            "previous_receipt_digest": previous["last_receipt_digest"],
            "input_digests": dict(input_digests),
        }
        if planned is not None:
            result["planned_state"] = planned
        result["receipt_digest"] = digest_json(result)
        return result


def seal_membership(*, epoch_id: str,
                    member_manifests: Sequence[Mapping[str, Any]],
                    retirement_manifests: Sequence[Mapping[str, Any]] = (),
                    out: str | os.PathLike[str] | None = None,
                    apply: bool = False) -> dict[str, Any]:
    _require_epoch(epoch_id)
    if not member_manifests:
        raise MigrationError("membership-empty")
    members, ids, refs, caps, keys = [], set(), set(), set(), set()
    for source in member_manifests:
        replica = _require_replica(str(source.get("replica_id", "")))
        if replica in ids:
            raise MigrationError("membership-replica-duplicate")
        ids.add(replica)
        member_keys = sorted(set(map(str, source.get("logical_project_keys", ()))))
        ref = str(source.get("protected_ref", ""))
        cap = _require_digest(source.get("writer_capability_hash"),
                              "writer-capability-invalid")
        if not member_keys or any(not key for key in member_keys) or not ref.startswith("refs/"):
            raise MigrationError("membership-contract-invalid")
        refs.add(ref); caps.add(cap); keys.update(member_keys)
        normalized_member = {"replica_id": replica,
            "logical_project_keys": member_keys, "protected_ref": ref,
            "writer_capability_hash": cap}
        member_digest = digest_json(normalized_member)
        supplied_digest = source.get("manifest_digest")
        if supplied_digest is not None and supplied_digest != member_digest:
            raise MigrationError("member-manifest-digest-mismatch")
        members.append({**normalized_member, "manifest_digest": member_digest})
    if len(refs) != 1 or len(caps) != 1:
        raise MigrationError("membership-contract-mismatch")
    retirements, retired = [], set()
    for source in retirement_manifests:
        replica = _require_replica(str(source.get("replica_id", "")))
        reason = str(source.get("reason", ""))
        if replica in ids | retired or source.get("operator_decision") is not True:
            raise MigrationError("retirement-authority-invalid")
        if reason not in {"decommissioned", "lost", "replaced", "unrecoverable"}:
            raise MigrationError("retirement-reason-invalid")
        if not isinstance(source.get("last_known_frontier"), (dict, list)):
            raise MigrationError("retirement-frontier-missing")
        backup = _require_digest(source.get("unique_data_backup_digest"),
                                 "retirement-backup-missing")
        retired.add(replica)
        normalized_retirement = {"replica_id": replica, "operator_decision": True,
            "reason": reason, "last_known_frontier": source["last_known_frontier"],
            "unique_data_backup_digest": backup}
        retirement_digest = digest_json(normalized_retirement)
        supplied_digest = source.get("retirement_digest")
        if supplied_digest is not None and supplied_digest != retirement_digest:
            raise MigrationError("retirement-manifest-digest-mismatch")
        retirements.append({**normalized_retirement,
                            "manifest_digest": retirement_digest,
                            "retirement_digest": retirement_digest})
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "membership", "epoch_id": epoch_id,
        "protected_ref": next(iter(refs)),
        "writer_capability_hash": next(iter(caps)),
        "logical_project_keys": sorted(keys),
        "members": sorted(members, key=lambda item: item["replica_id"]),
        "retirements": sorted(retirements, key=lambda item: item["replica_id"])})
    result = {**manifest, "changed": False}
    if apply:
        if out is None: raise MigrationError("output-required")
        root = _output_root(out, create=True)
        result["changed"] = _atomic_write(_contained(root, "membership.json"),
                                           canonical_bytes(manifest))
    return result


def verify_membership(value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "membership" or manifest.get("protocol_major") != 2:
        raise MigrationError("membership-manifest-invalid")
    rebuilt = seal_membership(epoch_id=str(manifest.get("epoch_id", "")),
        member_manifests=manifest.get("members", ()),
        retirement_manifests=manifest.get("retirements", ()))
    rebuilt.pop("changed")
    if rebuilt != manifest: raise MigrationError("membership-normalization-mismatch")
    return manifest


_EVIDENCE_FIELDS = ("snapshot_digest", "seed_digest", "delta_digest",
                    "fence_digest", "no_tail_digest", "backup_digest",
                    "equality_input_digest")


def replica_equality_input_digest(*, epoch_id: str, replica_id: str,
                                  membership_digest: str,
                                  snapshot_digest: str, seed_digest: str,
                                  delta_digest: str, fence_digest: str,
                                  no_tail_digest: str,
                                  backup_digest: str) -> str:
    """Bind only evidence available before seed publication and final fetch.

    The final equality manifest separately binds the sealed evidence digest.
    Remote ref OIDs and post-publish report digests are intentionally excluded
    because they do not exist when the evidence seal is created.
    """
    epoch_id = _require_epoch(epoch_id)
    replica_id = _require_replica(replica_id)
    fields = {"membership_digest": membership_digest,
        "snapshot_digest": snapshot_digest, "seed_digest": seed_digest,
        "delta_digest": delta_digest, "fence_digest": fence_digest,
        "no_tail_digest": no_tail_digest, "backup_digest": backup_digest}
    for name, value in fields.items():
        _require_digest(value, f"equality-input-{name}-invalid")
    return digest_json({"schema_version": 1, "protocol_major": 2,
        "kind": "pre-equality-input", "epoch_id": epoch_id,
        "replica_id": replica_id, **fields})


def seal_evidence(*, epoch_id: str,
                  membership: Mapping[str, Any] | str | os.PathLike[str],
                  replica_evidence: Sequence[Mapping[str, Any]],
                  out: str | os.PathLike[str] | None = None,
                  apply: bool = False) -> dict[str, Any]:
    roster = verify_membership(membership)
    if _require_epoch(epoch_id) != roster["epoch_id"]:
        raise MigrationError("evidence-epoch-mismatch")
    expected = {item["replica_id"] for item in roster["members"]}
    seen, rows = set(), []
    for source in replica_evidence:
        replica = _require_replica(str(source.get("replica_id", "")))
        if replica in seen or replica not in expected:
            raise MigrationError("evidence-roster-mismatch")
        if source.get("membership_digest") != roster["manifest_digest"]:
            raise MigrationError("evidence-membership-drift")
        row = {"replica_id": replica,
               "membership_digest": roster["manifest_digest"]}
        for field in _EVIDENCE_FIELDS:
            row[field] = _require_digest(source.get(field),
                                         f"evidence-{field}-invalid")
        seen.add(replica); rows.append(row)
    if seen != expected:
        raise MigrationError("evidence-roster-incomplete")
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "evidence", "epoch_id": epoch_id,
        "membership_digest": roster["manifest_digest"],
        "replicas": sorted(rows, key=lambda item: item["replica_id"])})
    result = {**manifest, "changed": False}
    if apply:
        if out is None: raise MigrationError("output-required")
        result["changed"] = _atomic_write(
            _contained(_output_root(out, create=True), "evidence.json"),
            canonical_bytes(manifest))
    return result


def verify_evidence(value: Mapping[str, Any] | str | os.PathLike[str],
                    membership: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    rebuilt = seal_evidence(epoch_id=str(manifest.get("epoch_id", "")),
        membership=membership, replica_evidence=manifest.get("replicas", ()))
    rebuilt.pop("changed")
    if rebuilt != manifest: raise MigrationError("evidence-normalization-mismatch")
    return manifest


def verify_receipt(value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    """Verify a canonical migration receipt without granting authority."""
    receipt = dict(value) if isinstance(value, Mapping) else load_manifest(value)
    claimed = _require_digest(receipt.get("receipt_digest"),
                              "receipt-digest-invalid")
    payload = dict(receipt); payload.pop("receipt_digest", None)
    if digest_json(payload) != claimed:
        raise MigrationError("receipt-digest-mismatch")
    if receipt.get("protocol_major") != 2 or receipt.get("schema_version") != 1:
        raise MigrationError("receipt-version-invalid")
    _require_epoch(str(receipt.get("epoch_id", "")))
    previous = receipt.get("previous_receipt_digest")
    if previous is not None: _require_digest(previous, "previous-receipt-digest-invalid")
    if not isinstance(receipt.get("changed"), bool):
        raise MigrationError("receipt-changed-invalid")
    return receipt


def create_phase_receipt(*, epoch_id: str, replica_id: str, phase: str,
                         membership_digest: str,
                         state_receipt: Mapping[str, Any] | str | os.PathLike[str],
                         extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind one DB-issued state receipt to an exact sealed replica/phase."""
    epoch_id = _require_epoch(epoch_id); replica_id = _require_replica(replica_id)
    membership_digest = _require_digest(membership_digest,
                                        "membership-digest-invalid")
    if not isinstance(phase, str) or not phase or len(phase.encode()) > 128:
        raise MigrationError("phase-receipt-phase-invalid")
    state = verify_receipt(state_receipt)
    if state.get("epoch_id") != epoch_id:
        raise MigrationError("phase-receipt-epoch-mismatch")
    if state.get("phase") != phase:
        raise MigrationError("phase-receipt-phase-mismatch")
    if state.get("membership_digest") != membership_digest:
        raise MigrationError("phase-receipt-membership-mismatch")
    extra_fields = dict(extra or {})
    if any(not isinstance(key, str) or not key for key in extra_fields):
        raise MigrationError("phase-receipt-extra-invalid")
    payload = {"schema_version": 1, "protocol_major": 2,
        "kind": "replica-phase-receipt", "epoch_id": epoch_id,
        "replica_id": replica_id, "phase": phase,
        "migration_state": state.get("migration_state"),
        "membership_digest": membership_digest,
        "state_receipt_digest": state["receipt_digest"],
        "extra": extra_fields}
    return {**payload, "receipt_digest": digest_json(payload)}


def verify_phase_receipt(
        value: Mapping[str, Any] | str | os.PathLike[str], *,
        epoch_id: str | None = None, replica_id: str | None = None,
        phase: str | None = None, membership_digest: str | None = None,
        state_receipt: Mapping[str, Any] | str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    receipt = dict(value) if isinstance(value, Mapping) else load_manifest(value)
    claimed = _require_digest(receipt.get("receipt_digest"),
                              "phase-receipt-digest-invalid")
    payload = dict(receipt); payload.pop("receipt_digest", None)
    if digest_json(payload) != claimed:
        raise MigrationError("phase-receipt-digest-mismatch")
    if receipt.get("kind") != "replica-phase-receipt" \
            or receipt.get("schema_version") != 1 \
            or receipt.get("protocol_major") != 2:
        raise MigrationError("phase-receipt-version-invalid")
    actual_epoch = _require_epoch(str(receipt.get("epoch_id", "")))
    actual_replica = _require_replica(str(receipt.get("replica_id", "")))
    actual_membership = _require_digest(receipt.get("membership_digest"),
                                        "phase-receipt-membership-invalid")
    actual_phase = receipt.get("phase")
    if not isinstance(actual_phase, str) or not actual_phase:
        raise MigrationError("phase-receipt-phase-invalid")
    if receipt.get("migration_state") not in MIGRATION_STATES:
        raise MigrationError("phase-receipt-state-invalid")
    if not isinstance(receipt.get("extra"), dict):
        raise MigrationError("phase-receipt-extra-invalid")
    _require_digest(receipt.get("state_receipt_digest"),
                    "phase-receipt-state-digest-invalid")
    for expected, actual, reason in (
        (epoch_id, actual_epoch, "phase-receipt-epoch-mismatch"),
        (replica_id, actual_replica, "phase-receipt-replica-mismatch"),
        (phase, actual_phase, "phase-receipt-phase-mismatch"),
        (membership_digest, actual_membership,
         "phase-receipt-membership-mismatch"),
    ):
        if expected is not None and expected != actual:
            raise MigrationError(reason)
    if state_receipt is not None:
        state = verify_receipt(state_receipt)
        if state["receipt_digest"] != receipt["state_receipt_digest"] \
                or state.get("epoch_id") != actual_epoch \
                or state.get("phase") != actual_phase \
                or state.get("membership_digest") != actual_membership:
            raise MigrationError("phase-receipt-state-binding-mismatch")
    return receipt


def verify_phase_receipts(
        values: Sequence[Mapping[str, Any] | str | os.PathLike[str]], *,
        epoch_id: str, phase: str, membership_digest: str,
        expected_replica_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Verify a replica receipt set, rejecting duplicate or missing members."""
    verified, seen = [], set()
    for value in values:
        receipt = verify_phase_receipt(value, epoch_id=epoch_id, phase=phase,
                                       membership_digest=membership_digest)
        replica = receipt["replica_id"]
        if replica in seen:
            raise MigrationError("phase-receipt-replica-duplicate")
        seen.add(replica); verified.append(receipt)
    if expected_replica_ids is not None:
        expected = {_require_replica(str(value)) for value in expected_replica_ids}
        if seen != expected:
            raise MigrationError("phase-receipt-roster-incomplete")
    return sorted(verified, key=lambda receipt: receipt["replica_id"])


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MigrationError("snapshot-source-invalid")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _open_static_ro(path: Path) -> sqlite3.Connection:
    """Open a completed SQLite artifact without creating WAL/SHM sidecars."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MigrationError("snapshot-source-invalid")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _open_snapshot_lock(path: Path) -> sqlite3.Connection:
    """Hold the named store's SQLite mutation lock without changing rows."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MigrationError("snapshot-source-invalid")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
    except Exception:
        connection.close()
        raise
    return connection


def _dump(connection: sqlite3.Connection) -> bytes:
    return ("\n".join(connection.iterdump()) + "\n").encode()


def _inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {}
    for table in tables:
        quoted = str(table).replace('"', '""')
        counts[str(table)] = int(connection.execute(
            f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
    profile: dict[str, dict[str, int]] = {}
    if "records" in tables:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        for field in ("tier", "scope", "type", "status"):
            if field in columns:
                profile[field] = {"null" if key is None else str(key): int(count)
                    for key, count in connection.execute(
                        f'SELECT "{field}",COUNT(*) FROM records '
                        f'GROUP BY "{field}" ORDER BY "{field}"')}
    return {"table_row_counts": counts, "record_profile": profile}


def create_snapshot(*, db_path: str | os.PathLike[str], epoch_id: str,
                    membership: Mapping[str, Any] | str | os.PathLike[str],
                    replica_id: str,
                    out: str | os.PathLike[str] | None = None,
                    apply: bool = False, capture_enabled: bool,
                    snapshot_capture_seq: int, outbox_counter: int = 0,
                    outbox_frontier: Mapping[str, int] | None = None,
                    db_high_watermark: int = 0,
                    local_v1_git_tip: str | None = None) -> dict[str, Any]:
    """Plan or create a consistent SQLite Backup API snapshot."""
    roster = verify_membership(membership); _require_epoch(epoch_id)
    replica_id = _require_replica(replica_id)
    if roster["epoch_id"] != epoch_id:
        raise MigrationError("snapshot-epoch-mismatch")
    member = next((item for item in roster["members"]
                   if item["replica_id"] == replica_id), None)
    if member is None: raise MigrationError("snapshot-replica-not-member")
    if capture_enabled is not True: raise MigrationError("snapshot-capture-not-enabled")
    for watermark in (snapshot_capture_seq, outbox_counter, db_high_watermark):
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
            raise MigrationError("snapshot-watermark-invalid")
    frontier = dict(outbox_frontier or {})
    for key, value in frontier.items():
        _require_replica(str(key))
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MigrationError("snapshot-frontier-invalid")
    if local_v1_git_tip is not None and not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", local_v1_git_tip):
        raise MigrationError("snapshot-v1-tip-invalid")
    source = Path(db_path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise MigrationError("snapshot-source-invalid")
    plan = {"manifest_version": 1, "protocol_major": 2, "kind": "snapshot-plan",
        "epoch_id": epoch_id, "membership_digest": roster["manifest_digest"],
        "replica_id": replica_id,
        "logical_project_keys": member["logical_project_keys"],
        "capture_enabled": True, "snapshot_capture_seq": snapshot_capture_seq,
        "outbox_counter": outbox_counter,
        "outbox_frontier": dict(sorted(frontier.items())),
        "db_high_watermark": db_high_watermark,
        "local_v1_git_tip": local_v1_git_tip}
    if not apply:
        return {**plan, "changed": False, "plan_digest": digest_json(plan)}
    if out is None: raise MigrationError("output-required")
    temporary: Path | None = None
    lock_connection = _open_snapshot_lock(source)
    try:
        actual_capture_seq = _sync_module().capture_frontier(lock_connection)
        if actual_capture_seq != snapshot_capture_seq \
                or db_high_watermark != snapshot_capture_seq:
            raise MigrationError("snapshot-watermark-raced")
        names = {row[0] for row in lock_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "sync_replica" in names:
            active = lock_connection.execute(
                "SELECT replica_id,counter FROM sync_replica WHERE active=1"
            ).fetchone()
            if active is None or str(active[0]) != replica_id \
                    or int(active[1]) != outbox_counter:
                raise MigrationError("snapshot-replica-counter-raced")
        root = _output_root(out, create=True)
        backup = _contained(root, "snapshot.db")
        fd, name = tempfile.mkstemp(prefix=".snapshot.", dir=root); os.close(fd)
        temporary = Path(name)
        source_connection = _open_ro(source)
        try:
            target_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(target_connection)
                target_connection.commit()
            finally:
                target_connection.close()
        finally:
            source_connection.close()
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise
    finally:
        lock_connection.rollback()
        lock_connection.close()
    try:
        if temporary is None:
            raise MigrationError("snapshot-temporary-missing")
        with temporary.open("rb") as handle: os.fsync(handle.fileno())
        raw = _read_file(temporary)
        if backup.exists():
            if _read_file(backup) != raw: raise MigrationError("snapshot-equivocation")
            changed = False
        else:
            os.replace(temporary, backup); _fsync_dir(root); changed = True
        connection = _open_static_ro(backup)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("snapshot-integrity-failed")
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            dump = _dump(connection); inventory = _inventory(connection)
        finally: connection.close()
        raw = _read_file(backup)
        manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
            "kind": "snapshot", "epoch_id": epoch_id,
            "membership_digest": roster["manifest_digest"], "replica_id": replica_id,
            "logical_project_keys": member["logical_project_keys"],
            "schema_user_version": user_version,
            "backup": {"path": "snapshot.db", "sha256": digest_bytes(raw),
                       "bytes": len(raw)},
            "deterministic_dump": {"sha256": digest_bytes(dump), "bytes": len(dump)},
            "snapshot_capture_seq": snapshot_capture_seq,
            "outbox_counter": outbox_counter,
            "outbox_frontier": dict(sorted(frontier.items())),
            "db_high_watermark": db_high_watermark,
            "local_v1_git_tip": local_v1_git_tip, **inventory,
            "capture_enabled": True, "consistent": True})
        changed = _atomic_write(_contained(root, "snapshot.json"),
                                canonical_bytes(manifest)) or changed
        verify_snapshot(root / "snapshot.json")
        return {**manifest, "changed": changed}
    finally:
        if temporary is not None and temporary.exists(): temporary.unlink()


def verify_snapshot(value: Mapping[str, Any] | str | os.PathLike[str], *,
                    root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "snapshot" or manifest.get("consistent") is not True \
            or manifest.get("capture_enabled") is not True:
        raise MigrationError("snapshot-manifest-invalid")
    if root is None:
        if isinstance(value, Mapping): raise MigrationError("snapshot-root-required")
        root_path = Path(value).parent.resolve(strict=True)
    else: root_path = _output_root(root, create=False)
    backup_info = manifest.get("backup", {})
    backup = _contained(root_path, str(backup_info.get("path", "")))
    raw = _read_file(backup)
    if len(raw) != backup_info.get("bytes") or digest_bytes(raw) != backup_info.get("sha256"):
        raise MigrationError("snapshot-backup-digest-mismatch")
    connection = _open_static_ro(backup)
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("snapshot-integrity-failed")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != manifest.get("schema_user_version"):
            raise MigrationError("snapshot-schema-mismatch")
        dump = _dump(connection); inventory = _inventory(connection)
    finally: connection.close()
    if {"sha256": digest_bytes(dump), "bytes": len(dump)} != manifest.get("deterministic_dump"):
        raise MigrationError("snapshot-dump-mismatch")
    if inventory["table_row_counts"] != manifest.get("table_row_counts") \
            or inventory["record_profile"] != manifest.get("record_profile"):
        raise MigrationError("snapshot-count-mismatch")
    return manifest


def _operation(value: bytes | bytearray | Mapping[str, Any], *,
               require_supported: bool = False) -> tuple[str, bytes, dict[str, Any]]:
    try:
        validated = _protocol_module().validate_operation(value)
    except Exception as exc:
        reason = getattr(exc, "code", "operation-invalid")
        raise MigrationError(f"seed-operation-{reason}", str(exc)) from exc
    if require_supported and not validated.supported:
        raise MigrationError("seed-operation-unsupported",
                             str(validated.unsupported_reason or "unsupported"))
    return (str(validated.op_id), bytes(validated.raw),
            dict(validated.payload))


def _seed_dispositions(raw_by_id: Mapping[str, bytes]) -> list[dict[str, Any]]:
    protocol = _protocol_module()
    try:
        classified = protocol.classify_operations(raw_by_id.values())
        folded = protocol.fold_operations(classified)
    except Exception as exc:
        reason = getattr(exc, "code", "classification-failed")
        raise MigrationError(f"seed-{reason}", str(exc)) from exc
    hard = tuple(folded.classification.hard_failures)
    if hard:
        reasons = ",".join(sorted({item.code for item in hard}))
        raise MigrationError("seed-classification-hard-failure", reasons)
    rows = []
    for op_id in sorted(raw_by_id):
        diagnostic = None
        if op_id in folded.quarantined:
            classification, diagnostic = "quarantined", folded.quarantined[op_id]
        elif op_id in folded.deferred:
            classification, diagnostic = "deferred", folded.deferred[op_id]
        elif op_id in folded.blocked:
            classification, diagnostic = "blocked", folded.blocked[op_id]
        else:
            classification = "accepted"
        rows.append({"op_id": op_id, "classification": classification,
            "reason": diagnostic.code if diagnostic is not None else None,
            "diagnostic_id": (diagnostic.diagnostic_id
                              if diagnostic is not None else None)})
    return rows


def _graveyard_entries(raw: bytes, replica_id: str) -> list[dict[str, Any]]:
    _canonical_jsonl(raw)
    entries, seen = [], set()
    protocol = _protocol_module()
    for line in raw.splitlines():
        source = json.loads(line)
        required = {"schema_version", "record_id", "prior_state", "tombstone",
                    "recovery_evidence_digest", "entry_digest"}
        if not isinstance(source, dict) or set(source) != required \
                or source.get("schema_version") != 1:
            raise MigrationError("graveyard-entry-shape-invalid")
        record_id = source.get("record_id")
        prior = source.get("prior_state")
        tombstone = source.get("tombstone")
        if not isinstance(record_id, str) or not record_id \
                or record_id in seen or not isinstance(prior, dict) \
                or prior.get("id") != record_id or not isinstance(tombstone, dict):
            raise MigrationError("graveyard-entry-record-invalid")
        if set(tombstone) != {"action", "pending", "prior_digest", "record_id"} \
                or tombstone.get("record_id") != record_id \
                or not isinstance(tombstone.get("action"), str) \
                or not tombstone["action"] \
                or not isinstance(tombstone.get("pending"), bool):
            raise MigrationError("graveyard-tombstone-invalid")
        prior_digest = digest_bytes(protocol.canonical_bytes(prior))
        pending = prior.get("delivery_state") == "pending" \
            or prior.get("pending") is True
        if tombstone.get("prior_digest") != prior_digest \
                or tombstone.get("pending") != pending:
            raise MigrationError("graveyard-prior-evidence-mismatch")
        _require_digest(source.get("recovery_evidence_digest"),
                        "graveyard-recovery-evidence-invalid")
        payload = dict(source); claimed = payload.pop("entry_digest")
        if _require_digest(claimed, "graveyard-entry-digest-invalid") \
                != digest_json(payload):
            raise MigrationError("graveyard-entry-digest-mismatch")
        project_key = "global" if prior.get("scope") == "global" \
            else prior.get("cwd_origin")
        if not isinstance(project_key, str) or not project_key:
            raise MigrationError("graveyard-project-key-invalid")
        # Reuse the protocol validator to require a complete prior record
        # state.  This operation is validation-only and is never emitted.
        try:
            validation_prior = protocol.build_operation({
                "protocol_major": 2, "schema_minor": 0,
                "replica_id": replica_id, "counter": 1, "parents": [],
                "project_key": project_key, "kind": "put",
                "frontiers": [{"record_id": record_id, "heads": []}],
                "mutations": [{"record_id": record_id, "mutation_ordinal": 0,
                               "post_state": prior}],
                "provenance": {"actor": "migration",
                               "reason": "graveyard-prior-validation"}})
            protocol.build_operation({"protocol_major": 2, "schema_minor": 0,
                "replica_id": replica_id, "counter": 2,
                "parents": [validation_prior["op_id"]],
                "project_key": project_key, "kind": "tombstone",
                "frontiers": [{"record_id": record_id,
                               "heads": [validation_prior["op_id"]]}],
                "mutations": [{"record_id": record_id,
                               "mutation_ordinal": 0,
                               "tombstone": tombstone}],
                "provenance": {"actor": "migration",
                               "reason": "graveyard-tombstone-validation"}})
        except Exception as exc:
            raise MigrationError("graveyard-prior-state-invalid", str(exc)) from exc
        seen.add(record_id); entries.append(source)
    return sorted(entries, key=lambda row: row["record_id"])


def seal_graveyard_source(
        *, epoch_id: str, membership_digest: str, snapshot_digest: str,
        replica_id: str, source: bytes | bytearray | str | os.PathLike[str],
        out: str | os.PathLike[str] | None = None, apply: bool = False,
) -> dict[str, Any]:
    """Seal immutable operator-supplied legacy deletion evidence.

    An absent entry creates no deletion.  Every present entry must carry a
    complete proven prior state, exact tombstone evidence, and recovery digest.
    """
    epoch_id = _require_epoch(epoch_id); replica_id = _require_replica(replica_id)
    _require_digest(membership_digest, "membership-digest-invalid")
    _require_digest(snapshot_digest, "snapshot-digest-invalid")
    raw = _artifact_bytes(source)
    entries = _graveyard_entries(raw, replica_id)
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "graveyard-source", "epoch_id": epoch_id,
        "membership_digest": membership_digest,
        "snapshot_digest": snapshot_digest, "replica_id": replica_id,
        "source": {"path": "graveyard.jsonl", "sha256": digest_bytes(raw),
                   "bytes": len(raw), "entries": len(entries)},
        "entries": [{"record_id": row["record_id"],
                     "entry_digest": row["entry_digest"],
                     "recovery_evidence_digest": row["recovery_evidence_digest"]}
                    for row in entries]})
    result = {**manifest, "changed": False}
    if apply:
        if out is None: raise MigrationError("output-required")
        root = _output_root(out, create=True); changed = False
        changed = _atomic_write(_contained(root, "graveyard.jsonl"), raw) or changed
        changed = _atomic_write(_contained(root, "graveyard.json"),
                                canonical_bytes(manifest)) or changed
        result["changed"] = changed
        verify_graveyard_source(root / "graveyard.json")
    return result


def verify_graveyard_source(
        value: Mapping[str, Any] | str | os.PathLike[str], *,
        root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "graveyard-source":
        raise MigrationError("graveyard-manifest-invalid")
    if root is None:
        if isinstance(value, Mapping):
            raise MigrationError("graveyard-root-required")
        root_path = Path(value).parent.resolve(strict=True)
    else:
        root_path = _output_root(root, create=False)
    info = manifest.get("source")
    if not isinstance(info, dict) or info.get("path") != "graveyard.jsonl":
        raise MigrationError("graveyard-source-inventory-invalid")
    raw = _read_file(_contained(root_path, "graveyard.jsonl"))
    if len(raw) != info.get("bytes") or digest_bytes(raw) != info.get("sha256"):
        raise MigrationError("graveyard-source-digest-mismatch")
    entries = _graveyard_entries(raw, str(manifest.get("replica_id", "")))
    summaries = [{"record_id": row["record_id"],
                  "entry_digest": row["entry_digest"],
                  "recovery_evidence_digest": row["recovery_evidence_digest"]}
                 for row in entries]
    if summaries != manifest.get("entries") or len(entries) != info.get("entries"):
        raise MigrationError("graveyard-entry-set-mismatch")
    return manifest


def graveyard_source_identities(
        value: Mapping[str, Any] | str | os.PathLike[str]) -> list[str]:
    manifest = verify_graveyard_source(value)
    return [f"graveyard:{row['record_id']}:{suffix}"
            for row in manifest["entries"] for suffix in ("prior", "tombstone")]


def build_graveyard_seed_operations(
        *, graveyard: str | os.PathLike[str],
        counter_mappings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Render one prior-state root plus its proven tombstone per graveyard row."""
    manifest = verify_graveyard_source(graveyard)
    root = Path(graveyard).parent.resolve(strict=True)
    entries = _graveyard_entries(_read_file(_contained(root, "graveyard.jsonl")),
                                 manifest["replica_id"])
    expected = graveyard_source_identities(graveyard)
    by_identity = {}
    for source in counter_mappings:
        identity, counter = source.get("source_identity"), source.get("counter")
        if identity in by_identity or identity not in expected \
                or not isinstance(counter, int) or isinstance(counter, bool) \
                or counter <= 0:
            raise MigrationError("graveyard-counter-mapping-invalid")
        by_identity[identity] = counter
    if set(by_identity) != set(expected) \
            or len(set(by_identity.values())) != len(by_identity):
        raise MigrationError("graveyard-counter-mapping-incomplete")
    protocol = _protocol_module(); operations, mappings = [], []
    for entry in entries:
        record_id, prior = entry["record_id"], entry["prior_state"]
        project_key = "global" if prior.get("scope") == "global" \
            else prior["cwd_origin"]
        prior_identity = f"graveyard:{record_id}:prior"
        tombstone_identity = f"graveyard:{record_id}:tombstone"
        prior_op = protocol.build_operation({"protocol_major": 2,
            "schema_minor": 0, "replica_id": manifest["replica_id"],
            "counter": by_identity[prior_identity], "parents": [],
            "project_key": project_key, "kind": "put",
            "frontiers": [{"record_id": record_id, "heads": []}],
            "mutations": [{"record_id": record_id, "mutation_ordinal": 0,
                           "post_state": prior}],
            "provenance": {"actor": "migration", "reason": "graveyard-prior",
                "graveyard_evidence": entry["recovery_evidence_digest"]}})
        # A row deleted while pending was force-deleted: an ordinary tombstone
        # over a pending prior state is blocked, and only ``force-tombstone``
        # carries that authority. The legacy log is the authority evidence.
        pending_prior = bool(entry["tombstone"].get("pending"))
        tombstone_provenance = {"actor": "migration",
            "reason": "graveyard-seed",
            "graveyard_evidence": entry["recovery_evidence_digest"]}
        if pending_prior:
            tombstone_provenance["authority"] = "legacy-graveyard-evidence"
        tombstone_op = protocol.build_operation({"protocol_major": 2,
            "schema_minor": 0, "replica_id": manifest["replica_id"],
            "counter": by_identity[tombstone_identity],
            "parents": [prior_op["op_id"]], "project_key": project_key,
            "kind": "force-tombstone" if pending_prior else "tombstone",
            "frontiers": [{"record_id": record_id,
                           "heads": [prior_op["op_id"]]}],
            "mutations": [{"record_id": record_id, "mutation_ordinal": 0,
                           "tombstone": entry["tombstone"]}],
            "provenance": tombstone_provenance})
        for identity, operation in ((prior_identity, prior_op),
                                    (tombstone_identity, tombstone_op)):
            operations.append(operation)
            mappings.append({"source_identity": identity,
                "counter": by_identity[identity], "op_id": operation["op_id"]})
    return {"source_digest": manifest["manifest_digest"],
            "mappings": mappings, "operations": operations}


def build_seed_manifest(*, epoch_id: str, membership_digest: str,
                        snapshot_digest: str, source_digest: str,
                        replica_id: str, kind: str,
                        mappings: Sequence[Mapping[str, Any]],
                        operations: Sequence[bytes | bytearray | Mapping[str, Any]],
                        out: str | os.PathLike[str] | None = None,
                        apply: bool = False) -> dict[str, Any]:
    _require_epoch(epoch_id); replica_id = _require_replica(replica_id)
    for value, reason in ((membership_digest, "membership-digest-invalid"),
                          (snapshot_digest, "snapshot-digest-invalid"),
                          (source_digest, "source-digest-invalid")):
        _require_digest(value, reason)
    if kind not in {"snapshot", "delta"}: raise MigrationError("seed-kind-invalid")
    raw_by_id, dots, object_rows = {}, set(), []
    for value in operations:
        op_id, raw, payload = _operation(value)
        dot = (str(payload.get("replica_id", "")), payload.get("counter"))
        if dot[0] != replica_id or not isinstance(dot[1], int) \
                or isinstance(dot[1], bool) or dot[1] <= 0:
            raise MigrationError("seed-dot-invalid")
        if dot in dots: raise MigrationError("seed-dot-duplicate")
        if op_id in raw_by_id and raw_by_id[op_id] != raw:
            raise MigrationError("seed-operation-equivocation")
        dots.add(dot); raw_by_id[op_id] = raw
        object_rows.append({"op_id": op_id,
            "path": f"protocol/v2/ops/{op_id[:2]}/{op_id}.json",
            "sha256": digest_bytes(raw), "bytes": len(raw),
            "replica_id": dot[0], "counter": dot[1]})
    source_ids, mapped, mapping_rows = set(), set(), []
    for source in mappings:
        identity, op_id = str(source.get("source_identity", "")), str(source.get("op_id", ""))
        counter = source.get("counter")
        if not identity or identity in source_ids:
            raise MigrationError("seed-source-identity-invalid")
        if op_id not in raw_by_id or op_id in mapped:
            raise MigrationError("seed-mapping-operation-invalid")
        if not isinstance(counter, int) or isinstance(counter, bool) \
                or (replica_id, counter) not in dots:
            raise MigrationError("seed-mapping-dot-invalid")
        source_ids.add(identity); mapped.add(op_id)
        mapping_rows.append({"source_identity": identity, "replica_id": replica_id,
                             "counter": counter, "op_id": op_id})
    if mapped != set(raw_by_id): raise MigrationError("seed-mapping-incomplete")
    counters = [counter for _, counter in dots]
    dispositions = _seed_dispositions(raw_by_id)
    if kind == "snapshot" and any(
            row["classification"] in {"deferred", "quarantined"}
            for row in dispositions):
        raise MigrationError("snapshot-seed-causal-closure-incomplete")
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "canonicalizer_version": 1, "kind": f"{kind}-seed", "epoch_id": epoch_id,
        "membership_digest": membership_digest, "snapshot_digest": snapshot_digest,
        "source_digest": source_digest, "replica_id": replica_id,
        "counter_range": [min(counters), max(counters)] if counters else None,
        "mappings": sorted(mapping_rows, key=lambda row: row["source_identity"]),
        "objects": sorted(object_rows, key=lambda row: row["op_id"]),
        "dispositions": dispositions,
        "dispositions_digest": digest_json(dispositions)})
    result, changed = {**manifest, "changed": False}, False
    if apply:
        if out is None: raise MigrationError("output-required")
        root = _output_root(out, create=True)
        for row in manifest["objects"]:
            changed = _atomic_write(_contained(root, row["path"]),
                                    raw_by_id[row["op_id"]]) or changed
        changed = _atomic_write(_contained(root, "seed.json"),
                                canonical_bytes(manifest)) or changed
        result["changed"] = changed
    return result


def verify_seed_manifest(value: Mapping[str, Any] | str | os.PathLike[str], *,
                         root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") not in {"snapshot-seed", "delta-seed"}:
        raise MigrationError("seed-manifest-invalid")
    if root is None:
        if isinstance(value, Mapping): raise MigrationError("seed-root-required")
        root_path = Path(value).parent.resolve(strict=True)
    else: root_path = _output_root(root, create=False)
    dots, ids, raw_by_id = set(), set(), {}
    for row in manifest.get("objects", ()):
        op_id = str(row.get("op_id", ""))
        relative = f"protocol/v2/ops/{op_id[:2]}/{op_id}.json"
        if row.get("path") != relative: raise MigrationError("seed-object-path-invalid")
        raw = _read_file(_contained(root_path, relative))
        actual, canonical, payload = _operation(raw)
        if actual != op_id or canonical != raw \
                or len(raw) != row.get("bytes") or digest_bytes(raw) != row.get("sha256"):
            raise MigrationError("seed-object-digest-mismatch")
        dot = (payload.get("replica_id"), payload.get("counter"))
        if dot in dots or dot != (row.get("replica_id"), row.get("counter")):
            raise MigrationError("seed-dot-duplicate")
        dots.add(dot); ids.add(op_id)
        raw_by_id[op_id] = raw
    if {row.get("op_id") for row in manifest.get("mappings", ())} != ids:
        raise MigrationError("seed-mapping-incomplete")
    dispositions = _seed_dispositions(raw_by_id)
    if manifest.get("kind") == "snapshot-seed" and any(
            row["classification"] in {"deferred", "quarantined"}
            for row in dispositions):
        raise MigrationError("snapshot-seed-causal-closure-incomplete")
    if manifest.get("dispositions") != dispositions \
            or manifest.get("dispositions_digest") != digest_json(dispositions):
        raise MigrationError("seed-disposition-mismatch")
    return manifest


def create_delta_manifest(*, epoch_id: str, membership_digest: str,
                          snapshot: Mapping[str, Any] | str | os.PathLike[str],
                          fence_receipt: Mapping[str, Any] | str | os.PathLike[str],
                          replica_id: str, fence_capture_seq: int,
                          capture_entries: Sequence[Mapping[str, Any]],
                          operations: Sequence[bytes | bytearray | Mapping[str, Any]],
                          out: str | os.PathLike[str] | None = None,
                          apply: bool = False) -> dict[str, Any]:
    """Bind every captured mutation in ``(snapshot, fence]`` exactly once."""
    _require_epoch(epoch_id); _require_digest(membership_digest,
                                               "membership-digest-invalid")
    replica_id = _require_replica(replica_id)
    snapshot_manifest = (load_manifest(snapshot) if isinstance(snapshot, Mapping)
                         else verify_snapshot(snapshot))
    _verify_digest(snapshot_manifest)
    receipt = verify_receipt(fence_receipt)
    if snapshot_manifest.get("kind") != "snapshot":
        raise MigrationError("delta-snapshot-invalid")
    if snapshot_manifest.get("epoch_id") != epoch_id \
            or receipt.get("epoch_id") != epoch_id:
        raise MigrationError("delta-epoch-mismatch")
    if snapshot_manifest.get("membership_digest") != membership_digest:
        raise MigrationError("delta-membership-mismatch")
    if snapshot_manifest.get("replica_id") != replica_id:
        raise MigrationError("delta-replica-mismatch")
    if receipt.get("migration_state") != "old-writers-fenced":
        raise MigrationError("delta-fence-receipt-invalid")
    start = snapshot_manifest.get("snapshot_capture_seq")
    if not isinstance(start, int) or isinstance(start, bool) \
            or not isinstance(fence_capture_seq, int) or isinstance(fence_capture_seq, bool) \
            or fence_capture_seq < start:
        raise MigrationError("delta-interval-invalid")
    raw_by_id, object_rows = {}, []
    for value in operations:
        op_id, raw, payload = _operation(value)
        if payload.get("replica_id") != replica_id:
            raise MigrationError("delta-operation-replica-mismatch")
        if op_id in raw_by_id: raise MigrationError("delta-operation-duplicate")
        raw_by_id[op_id] = raw
        object_rows.append({"op_id": op_id,
            "path": f"protocol/v2/ops/{op_id[:2]}/{op_id}.json",
            "sha256": digest_bytes(raw), "bytes": len(raw)})
    entries, sequences, mapped, identities = [], set(), set(), set()
    for source in capture_entries:
        sequence, op_id = source.get("capture_seq"), str(source.get("op_id", ""))
        if not isinstance(sequence, int) or isinstance(sequence, bool) \
                or sequence <= start or sequence > fence_capture_seq or sequence in sequences:
            raise MigrationError("delta-capture-sequence-invalid")
        if op_id not in raw_by_id or op_id in mapped:
            raise MigrationError("delta-capture-operation-invalid")
        row = {"capture_seq": sequence, "op_id": op_id}
        identity = source.get("source_identity")
        if identity is not None:
            if not isinstance(identity, str) or not identity or identity in identities:
                raise MigrationError("delta-source-identity-invalid")
            row["source_identity"] = identity
            identities.add(identity)
        entries.append(row); sequences.add(sequence); mapped.add(op_id)
    expected = set(range(start + 1, fence_capture_seq + 1))
    if sequences != expected or mapped != set(raw_by_id):
        raise MigrationError("delta-interval-not-exact")
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "captured-delta", "epoch_id": epoch_id,
        "membership_digest": membership_digest, "replica_id": replica_id,
        "snapshot_digest": snapshot_manifest["manifest_digest"],
        "fence_receipt_digest": receipt["receipt_digest"],
        "exclusive_start_capture_seq": start,
        "inclusive_fence_capture_seq": fence_capture_seq,
        "entries": sorted(entries, key=lambda row: row["capture_seq"]),
        "objects": sorted(object_rows, key=lambda row: row["op_id"])})
    result, changed = {**manifest, "changed": False}, False
    if apply:
        if out is None: raise MigrationError("output-required")
        root = _output_root(out, create=True)
        for row in manifest["objects"]:
            changed = _atomic_write(_contained(root, row["path"]),
                                    raw_by_id[row["op_id"]]) or changed
        changed = _atomic_write(_contained(root, "delta.json"),
                                canonical_bytes(manifest)) or changed
        result["changed"] = changed
    return result


def verify_delta_manifest(value: Mapping[str, Any] | str | os.PathLike[str], *,
                          root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "captured-delta":
        raise MigrationError("delta-manifest-invalid")
    if root is None:
        if isinstance(value, Mapping): raise MigrationError("delta-root-required")
        root_path = Path(value).parent.resolve(strict=True)
    else: root_path = _output_root(root, create=False)
    start, end = (manifest.get("exclusive_start_capture_seq"),
                  manifest.get("inclusive_fence_capture_seq"))
    sequences = [row.get("capture_seq") for row in manifest.get("entries", ())]
    if not isinstance(start, int) or not isinstance(end, int) or end < start \
            or sequences != list(range(start + 1, end + 1)):
        raise MigrationError("delta-interval-not-exact")
    mapped = {row.get("op_id") for row in manifest.get("entries", ())}
    object_ids = set()
    for row in manifest.get("objects", ()):
        op_id = str(row.get("op_id", "")); relative = str(row.get("path", ""))
        expected_path = f"protocol/v2/ops/{op_id[:2]}/{op_id}.json"
        if relative != expected_path: raise MigrationError("delta-object-path-invalid")
        raw = _read_file(_contained(root_path, relative)); actual, canonical, _ = _operation(raw)
        if actual != op_id or canonical != raw or len(raw) != row.get("bytes") \
                or digest_bytes(raw) != row.get("sha256"):
            raise MigrationError("delta-object-digest-mismatch")
        object_ids.add(op_id)
    if mapped != object_ids or len(mapped) != len(sequences):
        raise MigrationError("delta-capture-operation-invalid")
    return manifest


def create_no_tail_report(*, epoch_id: str,
                          snapshot: Mapping[str, Any] | str | os.PathLike[str],
                          fence_receipt: Mapping[str, Any] | str | os.PathLike[str],
                          delta: Mapping[str, Any] | str | os.PathLike[str],
                          current_capture_seq: int, unbound_capture_count: int,
                          unrendered_outbox_count: int, fence_active: bool,
                          out: str | os.PathLike[str] | None = None,
                          apply: bool = False) -> dict[str, Any]:
    for value in (current_capture_seq, unbound_capture_count,
                  unrendered_outbox_count):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MigrationError("no-tail-counter-invalid")
    snapshot_manifest = (load_manifest(snapshot) if isinstance(snapshot, Mapping)
                         else verify_snapshot(snapshot))
    _verify_digest(snapshot_manifest)
    receipt = verify_receipt(fence_receipt)
    delta_manifest = (load_manifest(delta) if isinstance(delta, Mapping)
                      else verify_delta_manifest(delta))
    _verify_digest(delta_manifest)
    if any(item.get("epoch_id") != _require_epoch(epoch_id)
           for item in (snapshot_manifest, receipt, delta_manifest)):
        raise MigrationError("no-tail-epoch-mismatch")
    if delta_manifest.get("snapshot_digest") != snapshot_manifest.get("manifest_digest") \
            or delta_manifest.get("fence_receipt_digest") != receipt.get("receipt_digest"):
        raise MigrationError("no-tail-binding-mismatch")
    fence_seq = delta_manifest.get("inclusive_fence_capture_seq")
    if current_capture_seq != fence_seq:
        raise MigrationError("no-tail-watermark-advanced")
    if unbound_capture_count != 0 or unrendered_outbox_count != 0:
        raise MigrationError("no-tail-work-remains")
    if fence_active is not True:
        raise MigrationError("no-tail-fence-inactive")
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "no-tail", "epoch_id": epoch_id,
        "membership_digest": delta_manifest.get("membership_digest"),
        "replica_id": delta_manifest.get("replica_id"),
        "snapshot_digest": snapshot_manifest["manifest_digest"],
        "fence_receipt_digest": receipt["receipt_digest"],
        "delta_digest": delta_manifest["manifest_digest"],
        "snapshot_capture_seq": snapshot_manifest.get("snapshot_capture_seq"),
        "fence_capture_seq": fence_seq, "current_capture_seq": current_capture_seq,
        "unbound_capture_count": 0, "unrendered_outbox_count": 0,
        "fence_active": True, "proven": True})
    result = {**manifest, "changed": False}
    if apply:
        if out is None: raise MigrationError("output-required")
        result["changed"] = _atomic_write(
            _contained(_output_root(out, create=True), "no-tail.json"),
            canonical_bytes(manifest))
    return result


def verify_no_tail_report(value: Mapping[str, Any] | str | os.PathLike[str], *,
                          snapshot: Mapping[str, Any] | str | os.PathLike[str],
                          fence_receipt: Mapping[str, Any] | str | os.PathLike[str],
                          delta: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    rebuilt = create_no_tail_report(epoch_id=str(manifest.get("epoch_id", "")),
        snapshot=snapshot, fence_receipt=fence_receipt, delta=delta,
        current_capture_seq=manifest.get("current_capture_seq"),
        unbound_capture_count=manifest.get("unbound_capture_count"),
        unrendered_outbox_count=manifest.get("unrendered_outbox_count"),
        fence_active=manifest.get("fence_active"))
    rebuilt.pop("changed")
    if rebuilt != manifest: raise MigrationError("no-tail-normalization-mismatch")
    return manifest


_SHARED_FIELDS = ("accepted_operation_set_digest", "operation_tree_digest",
    "materialized_digest", "record_frontiers_digest", "conflict_variants_digest",
    "deferred_digest", "quarantined_digest", "blocked_digest",
    "pending_consumed_digest", "tombstone_graveyard_digest", "supersession_digest",
    "schema_version", "canonicalizer_version", "reducer_version",
    "derived_index_digest", "diagnostic_digest", "exit_class")
_LOCAL_FIELDS = ("replica_id", "applied_set_digest", "capture_frontier",
    "seed_manifest_digest", "unbound_capture_count",
    "unconfirmed_epoch_outbox_count", "fresh_remote_ref_oid",
    "remote_operation_tree_digest", "local_materialized_digest", "writer_mode",
    "report_digest")


def replica_report_digest(report: Mapping[str, Any]) -> str:
    """Digest exactly the shared and normalized local equality input fields."""
    shared = {field: report.get(field) for field in _SHARED_FIELDS}
    local = {field: report.get(field) for field in _LOCAL_FIELDS
             if field != "report_digest"}
    return digest_json({"shared": shared, "local": local})


def create_equality_report(*, epoch_id: str, evidence_digest: str,
                           replica_reports: Sequence[Mapping[str, Any]],
                           authoritative_ref: str,
                           expected_replica_ids: Iterable[str] | None = None,
                           out: str | os.PathLike[str] | None = None,
                           apply: bool = False) -> dict[str, Any]:
    _require_epoch(epoch_id); _require_digest(evidence_digest, "evidence-digest-invalid")
    if not authoritative_ref.startswith("refs/"): raise MigrationError("equality-ref-invalid")
    if not replica_reports: raise MigrationError("equality-report-empty")
    shared, matrix, seen = None, [], set()
    for report in replica_reports:
        local = {field: report.get(field) for field in _LOCAL_FIELDS}
        replica = _require_replica(str(local["replica_id"] or ""))
        if replica in seen: raise MigrationError("equality-replica-duplicate")
        seen.add(replica)
        if local["unbound_capture_count"] != 0 or local["unconfirmed_epoch_outbox_count"] != 0:
            raise MigrationError("equality-tail-not-empty")
        if local["writer_mode"] != "fenced": raise MigrationError("equality-writer-not-fenced")
        current = {field: report.get(field) for field in _SHARED_FIELDS}
        for field in _SHARED_FIELDS:
            if field.endswith("_digest") or field == "accepted_operation_set_digest":
                _require_digest(current[field], f"equality-{field}-invalid")
        for field in ("applied_set_digest", "seed_manifest_digest",
                      "remote_operation_tree_digest", "local_materialized_digest",
                      "report_digest"):
            _require_digest(local[field], f"equality-{field}-invalid")
        if local["report_digest"] != replica_report_digest(report):
            raise MigrationError("equality-report-digest-mismatch")
        oid = local["fresh_remote_ref_oid"]
        if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise MigrationError("equality-remote-oid-invalid")
        if shared is None: shared = current
        elif canonical_bytes(shared) != canonical_bytes(current):
            raise MigrationError("equality-shared-mismatch")
        matrix.append(local)
    if expected_replica_ids is not None:
        expected = {_require_replica(str(value)) for value in expected_replica_ids}
        if seen != expected:
            raise MigrationError("equality-roster-incomplete")
    trees = {row["remote_operation_tree_digest"] for row in matrix}
    dumps = {row["local_materialized_digest"] for row in matrix}
    oids = {row["fresh_remote_ref_oid"] for row in matrix}
    if len(trees) != 1 or len(dumps) != 1 or len(oids) != 1 \
            or shared["operation_tree_digest"] not in trees \
            or shared["materialized_digest"] not in dumps:
        raise MigrationError("equality-matrix-mismatch")
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "equality", "epoch_id": epoch_id, "evidence_digest": evidence_digest,
        "authoritative_ref": authoritative_ref, "authoritative_ref_oid": next(iter(oids)),
        "equal": True, "shared": shared,
        "replica_matrix": sorted(matrix, key=lambda row: row["replica_id"])})
    result = {**manifest, "changed": False}
    if apply:
        if out is None: raise MigrationError("output-required")
        result["changed"] = _atomic_write(
            _contained(_output_root(out, create=True), "equality.json"),
            canonical_bytes(manifest))
    return result


def verify_equality_report(value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    shared = manifest.get("shared")
    if not isinstance(shared, dict): raise MigrationError("equality-shared-invalid")
    rebuilt = create_equality_report(epoch_id=str(manifest.get("epoch_id", "")),
        evidence_digest=str(manifest.get("evidence_digest", "")),
        replica_reports=[{**shared, **local} for local in manifest.get("replica_matrix", ())],
        authoritative_ref=str(manifest.get("authoritative_ref", "")),
        expected_replica_ids=[row.get("replica_id")
                              for row in manifest.get("replica_matrix", ())])
    rebuilt.pop("changed")
    if rebuilt != manifest: raise MigrationError("equality-normalization-mismatch")
    return manifest


def inspect_store(db_path: str | os.PathLike[str], epoch_id: str) -> dict[str, Any]:
    """Return bounded read-only metadata without bodies, paths, or timestamps."""
    _require_epoch(epoch_id); path = Path(db_path)
    connection = _open_ro(path)
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        inventory = _inventory(connection)
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        migration = _sync_module().migration_status(connection, epoch_id)
    finally: connection.close()
    return {"schema_version": 1, "protocol_major": 2, "epoch_id": epoch_id,
        "integrity": integrity, "schema_user_version": user_version,
        **inventory, "migration_state": migration["migration_state"],
        "state_digest": migration["state_digest"],
        "writer_mode": migration["writer_mode"], "changed": False}


def _source_bytes(value: bytes | bytearray | str | os.PathLike[str]) -> bytes:
    if isinstance(value, (bytes, bytearray)): return bytes(value)
    return _read_file(Path(value))


def _parents(raw: bytes) -> tuple[str, ...]:
    try: value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError): return ()
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict): return ()
    parents = value["payload"].get("parents", ())
    return tuple(parent for parent in parents if isinstance(parent, str)) \
        if isinstance(parents, list) else ()


_ROLLBACK_STATE_SECTIONS = (
    "accepted_set", "frontiers", "conflicts", "pending_consumed",
    "tombstones", "graveyard", "supersession", "applied_matrix",
    "outbox_matrix", "peer_matrix",
)
_ROLLBACK_CAUSAL_CLASSES = frozenset({"accepted", "blocked"})


def rollback_state_section_names() -> tuple[str, ...]:
    """Return the exact normalized state sections required by D-76."""
    return _ROLLBACK_STATE_SECTIONS


def rollback_seed_set_digest(
        values: Sequence[Mapping[str, Any] | str | os.PathLike[str]]) -> str:
    """Digest the exact snapshot+delta seed manifest set for evidence sealing."""
    manifests = []
    for value in values:
        manifest = (load_manifest(value) if isinstance(value, Mapping)
                    else verify_seed_manifest(value))
        _verify_digest(manifest)
        if manifest.get("kind") not in {"snapshot-seed", "delta-seed"}:
            raise MigrationError("rollback-seed-manifest-invalid")
        manifests.append(manifest)
    rows = sorted({manifest["manifest_digest"] for manifest in manifests})
    if len(rows) != len(manifests):
        raise MigrationError("rollback-seed-manifest-duplicate")
    return digest_json({"seed_manifest_digests": rows})


def _artifact_bytes(value: Mapping[str, Any] | bytes | bytearray |
                    str | os.PathLike[str]) -> bytes:
    if isinstance(value, Mapping):
        return canonical_bytes(load_manifest(value))
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return _read_file(Path(value))


def _canonical_jsonl(raw: bytes) -> None:
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError("rollback-jsonl-invalid", str(exc)) from exc
        if canonical_bytes(value) != line:
            raise MigrationError("rollback-jsonl-not-canonical")


def collect_rollback_bundle_inputs(
        *, epoch_id: str,
        membership: Mapping[str, Any] | str | os.PathLike[str],
        evidence: Mapping[str, Any] | str | os.PathLike[str],
        equality: Mapping[str, Any] | str | os.PathLike[str],
        protected_ref_evidence: Mapping[str, Any] | str | os.PathLike[str],
        precutover_replicas: Sequence[Mapping[str, Any]],
        seed_manifests: Sequence[str | os.PathLike[str]],
        delta_manifests: Sequence[str | os.PathLike[str]],
        no_tail_reports: Sequence[Mapping[str, Any] | str | os.PathLike[str]],
        fence_receipts: Sequence[Mapping[str, Any] | str | os.PathLike[str]],
        activation_receipts: Sequence[Mapping[str, Any] | str | os.PathLike[str]],
        operation_objects: Mapping[str, bytes | bytearray | str | os.PathLike[str]],
        classifications: Mapping[str, str],
        unconfirmed_operation_ids: Iterable[str],
        state_sections: Mapping[str, Any],
        materialized_dump: bytes | bytearray | str | os.PathLike[str],
        diagnostics: Mapping[str, Any],
        post_cutover_delta_index: Mapping[str, Any],
) -> dict[str, Any]:
    """Collect the exact complete D-76 input set for bundle sealing.

    This function performs no writes.  All active roster members must provide
    pre-cutover backup evidence, snapshot+delta seed manifests, a drained-delta
    manifest, no-tail proof, and replica-bound fence/activation receipts.
    """
    epoch_id = _require_epoch(epoch_id)
    roster = verify_membership(membership)
    sealed_evidence = verify_evidence(evidence, roster)
    equality_report = verify_equality_report(equality)
    if any(item.get("epoch_id") != epoch_id
           for item in (roster, sealed_evidence, equality_report)):
        raise MigrationError("rollback-authority-epoch-mismatch")
    if sealed_evidence.get("membership_digest") != roster["manifest_digest"] \
            or equality_report.get("evidence_digest") != sealed_evidence["manifest_digest"]:
        raise MigrationError("rollback-authority-chain-mismatch")
    replicas = {item["replica_id"] for item in roster["members"]}
    if {row["replica_id"] for row in equality_report["replica_matrix"]} != replicas:
        raise MigrationError("rollback-equality-roster-mismatch")

    remote = load_manifest(protected_ref_evidence)
    if remote.get("protected_ref") != roster["protected_ref"] \
            or remote.get("fresh_fetch") is not True:
        raise MigrationError("rollback-protected-ref-evidence-invalid")
    oid = remote.get("ref_oid")
    if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise MigrationError("rollback-protected-ref-oid-invalid")
    _require_digest(remote.get("operation_tree_digest"),
                    "rollback-protected-tree-invalid")
    if oid != equality_report["authoritative_ref_oid"] \
            or remote["operation_tree_digest"] != equality_report["shared"]["operation_tree_digest"]:
        raise MigrationError("rollback-protected-ref-equality-mismatch")

    files: dict[str, bytes] = {
        "authority/membership.json": canonical_bytes(roster),
        "authority/evidence.json": canonical_bytes(sealed_evidence),
        "authority/equality.json": canonical_bytes(equality_report),
        "authority/protected-ref.json": canonical_bytes(remote),
    }
    snapshots: dict[str, dict[str, Any]] = {}
    seen_pre: set[str] = set()
    for source in precutover_replicas:
        replica = _require_replica(str(source.get("replica_id", "")))
        if replica not in replicas or replica in seen_pre:
            raise MigrationError("rollback-precutover-roster-mismatch")
        snapshot_path = source.get("snapshot")
        if not isinstance(snapshot_path, (str, os.PathLike)):
            raise MigrationError("rollback-snapshot-path-required")
        snapshot = verify_snapshot(snapshot_path)
        if snapshot.get("epoch_id") != epoch_id or snapshot.get("replica_id") != replica \
                or snapshot.get("membership_digest") != roster["manifest_digest"]:
            raise MigrationError("rollback-snapshot-binding-mismatch")
        snapshot_root = Path(snapshot_path).parent.resolve(strict=True)
        backup_path = _contained(snapshot_root, snapshot["backup"]["path"])
        v1_dump = _artifact_bytes(source.get("v1_dump", b""))
        if not v1_dump:
            raise MigrationError("rollback-v1-dump-missing")
        _canonical_jsonl(v1_dump)
        v1_ref = load_manifest(source.get("v1_ref_evidence", {}))
        tip = v1_ref.get("ref_oid")
        if not isinstance(tip, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tip):
            raise MigrationError("rollback-v1-ref-evidence-invalid")
        prefix = f"precutover/{replica}"
        files[f"{prefix}/snapshot.json"] = canonical_bytes(snapshot)
        files[f"{prefix}/snapshot.db"] = _read_file(backup_path)
        files[f"{prefix}/v1-dump.jsonl"] = v1_dump
        files[f"{prefix}/v1-ref.json"] = canonical_bytes(v1_ref)
        snapshots[replica] = snapshot
        seen_pre.add(replica)
    if seen_pre != replicas:
        raise MigrationError("rollback-precutover-roster-incomplete")

    seeds_by_replica: dict[str, dict[str, dict[str, Any]]] = {
        replica: {} for replica in replicas}
    seed_object_ids: set[str] = set()
    for value in seed_manifests:
        seed = verify_seed_manifest(value)
        replica = seed.get("replica_id")
        if replica not in replicas or seed.get("epoch_id") != epoch_id \
                or seed.get("membership_digest") != roster["manifest_digest"]:
            raise MigrationError("rollback-seed-binding-mismatch")
        kind = str(seed.get("kind"))
        if kind in seeds_by_replica[replica]:
            raise MigrationError("rollback-seed-kind-duplicate")
        if seed.get("snapshot_digest") != snapshots[replica]["manifest_digest"]:
            raise MigrationError("rollback-seed-snapshot-mismatch")
        seeds_by_replica[replica][kind] = seed
        files[f"receipts/seeds/{replica}/{kind}.json"] = canonical_bytes(seed)
        seed_object_ids.update(row["op_id"] for row in seed["objects"])
    if any(set(kinds) != {"snapshot-seed", "delta-seed"}
           for kinds in seeds_by_replica.values()):
        raise MigrationError("rollback-seed-roster-incomplete")

    deltas: dict[str, dict[str, Any]] = {}
    for value in delta_manifests:
        delta = verify_delta_manifest(value)
        replica = delta.get("replica_id")
        if replica not in replicas or replica in deltas \
                or delta.get("epoch_id") != epoch_id \
                or delta.get("membership_digest") != roster["manifest_digest"]:
            raise MigrationError("rollback-delta-roster-mismatch")
        if delta.get("snapshot_digest") != snapshots[replica]["manifest_digest"]:
            raise MigrationError("rollback-delta-snapshot-mismatch")
        deltas[replica] = delta
        files[f"receipts/delta/{replica}.json"] = canonical_bytes(delta)
        seed_object_ids.update(row["op_id"] for row in delta["objects"])
    if set(deltas) != replicas:
        raise MigrationError("rollback-delta-roster-incomplete")

    fences = verify_phase_receipts(fence_receipts, epoch_id=epoch_id,
        phase="fence.activate", membership_digest=roster["manifest_digest"],
        expected_replica_ids=replicas)
    activations = verify_phase_receipts(activation_receipts, epoch_id=epoch_id,
        phase="activate.v2-only", membership_digest=roster["manifest_digest"],
        expected_replica_ids=replicas)
    fence_by_replica = {row["replica_id"]: row for row in fences}
    activation_by_replica = {row["replica_id"]: row for row in activations}
    for row in fences:
        state = row.get("extra", {}).get("state_receipt")
        if not isinstance(state, Mapping):
            raise MigrationError("rollback-fence-state-receipt-missing")
        verify_phase_receipt(row, state_receipt=state)
        replica = row["replica_id"]
        if row.get("migration_state") != "old-writers-fenced" \
                or row.get("extra", {}).get("fence_capture_seq") \
                != deltas[replica]["inclusive_fence_capture_seq"] \
                or deltas[replica].get("fence_receipt_digest") \
                != row["state_receipt_digest"]:
            raise MigrationError("rollback-fence-binding-mismatch")
        files[f"receipts/fence/{row['replica_id']}.json"] = canonical_bytes(row)
    for row in activations:
        state = row.get("extra", {}).get("state_receipt")
        if not isinstance(state, Mapping):
            raise MigrationError("rollback-activation-state-receipt-missing")
        verify_phase_receipt(row, state_receipt=state)
        if row.get("migration_state") != "v2-only-enabled":
            raise MigrationError("rollback-activation-state-invalid")
        files[f"receipts/activation/{row['replica_id']}.json"] = canonical_bytes(row)

    tails: dict[str, dict[str, Any]] = {}
    for value in no_tail_reports:
        report = load_manifest(value); _verify_digest(report)
        replica = report.get("replica_id")
        if report.get("kind") != "no-tail" or report.get("proven") is not True \
                or report.get("epoch_id") != epoch_id or replica not in replicas \
                or replica in tails:
            raise MigrationError("rollback-no-tail-roster-mismatch")
        state = fence_by_replica[replica]["extra"]["state_receipt"]
        verify_no_tail_report(report, snapshot=snapshots[replica],
            fence_receipt=state, delta=deltas[replica])
        if report.get("membership_digest") != roster["manifest_digest"] \
                or report.get("snapshot_digest") != snapshots[replica]["manifest_digest"] \
                or report.get("delta_digest") != deltas[replica]["manifest_digest"] \
                or report.get("fence_receipt_digest") \
                != fence_by_replica[replica]["state_receipt_digest"]:
            raise MigrationError("rollback-no-tail-binding-mismatch")
        tails[replica] = report
        files[f"receipts/no-tail/{replica}.json"] = canonical_bytes(report)
    if set(tails) != replicas:
        raise MigrationError("rollback-no-tail-roster-incomplete")

    evidence_by_replica = {row["replica_id"]: row
                           for row in sealed_evidence["replicas"]}
    for replica in replicas:
        row = evidence_by_replica[replica]
        seed_digest = rollback_seed_set_digest(
            list(seeds_by_replica[replica].values()))
        expected = {
            "snapshot_digest": snapshots[replica]["manifest_digest"],
            "seed_digest": seed_digest,
            "delta_digest": deltas[replica]["manifest_digest"],
            "fence_digest": fence_by_replica[replica]["receipt_digest"],
            "no_tail_digest": tails[replica]["manifest_digest"],
            "backup_digest": snapshots[replica]["backup"]["sha256"],
        }
        expected["equality_input_digest"] = replica_equality_input_digest(
            epoch_id=epoch_id, replica_id=replica,
            membership_digest=roster["manifest_digest"],
            **{field: expected[field] for field in (
                "snapshot_digest", "seed_digest", "delta_digest",
                "fence_digest", "no_tail_digest", "backup_digest")})
        if any(row.get(field) != value for field, value in expected.items()):
            raise MigrationError("rollback-evidence-artifact-mismatch")
        if activation_by_replica[replica].get("membership_digest") \
                != row["membership_digest"]:
            raise MigrationError("rollback-activation-membership-mismatch")

    if set(operation_objects) != set(classifications):
        raise MigrationError("rollback-operation-classification-incomplete")
    unconfirmed = sorted(set(unconfirmed_operation_ids))
    for op_id in unconfirmed:
        _require_digest(op_id, "rollback-unconfirmed-operation-id-invalid")
    if not set(unconfirmed) <= set(operation_objects):
        raise MigrationError("rollback-unconfirmed-operation-missing")
    accepted = sorted(op_id for op_id, value in classifications.items()
                      if value in _ROLLBACK_CAUSAL_CLASSES)
    for op_id, source in sorted(operation_objects.items()):
        _require_digest(op_id, "rollback-operation-id-invalid")
        classification = classifications[op_id]
        if classification not in {"accepted", "blocked", "deferred",
                                  "quarantined"}:
            raise MigrationError("rollback-classification-invalid")
        raw = _artifact_bytes(source)
        if classification in _ROLLBACK_CAUSAL_CLASSES:
            actual, canonical, _ = _operation(raw, require_supported=True)
            if actual != op_id or canonical != raw:
                raise MigrationError("rollback-causal-object-invalid")
            relative = f"objects/accepted/{op_id}.json"
        else:
            relative = f"objects/raw/{classification}/{op_id}.bin"
        files[relative] = raw
    if not seed_object_ids <= set(operation_objects):
        raise MigrationError("rollback-seed-object-set-incomplete")

    if set(state_sections) != set(_ROLLBACK_STATE_SECTIONS):
        raise MigrationError("rollback-state-sections-incomplete")
    accepted_section = state_sections["accepted_set"]
    if not isinstance(accepted_section, Mapping) \
            or accepted_section.get("operation_ids") != accepted:
        raise MigrationError("rollback-accepted-set-mismatch")
    for name in _ROLLBACK_STATE_SECTIONS:
        files[f"state/{name}.json"] = canonical_bytes(state_sections[name])
    dump = _artifact_bytes(materialized_dump)
    _canonical_jsonl(dump)
    files["state/materialized.jsonl"] = dump
    files["state/diagnostics.json"] = canonical_bytes(diagnostics)
    files["state/post-cutover-delta-index.json"] = canonical_bytes(
        post_cutover_delta_index)

    inventory = [{"path": path, "sha256": digest_bytes(raw), "bytes": len(raw)}
                 for path, raw in sorted(files.items())]
    collection = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "rollback-collection", "epoch_id": epoch_id,
        "membership_digest": roster["manifest_digest"],
        "evidence_digest": sealed_evidence["manifest_digest"],
        "equality_digest": equality_report["manifest_digest"],
        "replica_ids": sorted(replicas), "accepted_operation_ids": accepted,
        "unconfirmed_operation_ids": unconfirmed,
        "classifications_digest": digest_json(dict(sorted(classifications.items()))),
        "files": inventory})
    files["collection.json"] = canonical_bytes(collection)
    return {"files": files, "accepted_operation_ids": accepted,
            "classifications": dict(classifications),
            "unconfirmed_operation_ids": unconfirmed,
            "collection_digest": collection["manifest_digest"]}


def _verify_complete_collection(files: Mapping[str, bytes], *,
                                classifications: Mapping[str, str],
                                accepted_operation_ids: Sequence[str],
                                unconfirmed_operation_ids: Sequence[str],
                                collection_digest: str, epoch_id: str,
                                membership_digest: str, evidence_digest: str,
                                equality_digest: str) -> None:
    raw = files.get("collection.json")
    if raw is None:
        raise MigrationError("rollback-collection-missing")
    try:
        collection = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError("rollback-collection-invalid", str(exc)) from exc
    if not isinstance(collection, dict) or canonical_bytes(collection) != raw:
        raise MigrationError("rollback-collection-not-canonical")
    _verify_digest(collection)
    if collection.get("kind") != "rollback-collection" \
            or collection.get("manifest_digest") != collection_digest:
        raise MigrationError("rollback-collection-digest-mismatch")
    if collection.get("epoch_id") != epoch_id \
            or collection.get("membership_digest") != membership_digest \
            or collection.get("evidence_digest") != evidence_digest \
            or collection.get("equality_digest") != equality_digest:
        raise MigrationError("rollback-collection-authority-mismatch")
    replicas = collection.get("replica_ids")
    if not isinstance(replicas, list) or replicas != sorted(set(replicas)) \
            or any(not isinstance(value, str) or not REPLICA_RE.fullmatch(value)
                   for value in replicas):
        raise MigrationError("rollback-collection-roster-invalid")
    expected_paths = set(files) - {"collection.json"}
    rows = collection.get("files")
    if not isinstance(rows, list) or any(
            not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}
            for row in rows) or rows != sorted(rows, key=lambda row: row["path"]) \
            or len({row["path"] for row in rows}) != len(rows) \
            or {row["path"] for row in rows} != expected_paths:
        raise MigrationError("rollback-collection-inventory-mismatch")
    for row in rows:
        member = files.get(str(row.get("path", "")))
        if member is None or len(member) != row.get("bytes") \
                or digest_bytes(member) != row.get("sha256"):
            raise MigrationError("rollback-collection-member-mismatch")
    if collection.get("accepted_operation_ids") != list(accepted_operation_ids) \
            or collection.get("unconfirmed_operation_ids") \
            != list(unconfirmed_operation_ids) \
            or collection.get("classifications_digest") \
            != digest_json(dict(sorted(classifications.items()))):
        raise MigrationError("rollback-collection-state-mismatch")
    required = {"authority/membership.json", "authority/evidence.json",
        "authority/equality.json", "authority/protected-ref.json",
        "state/materialized.jsonl", "state/diagnostics.json",
        "state/post-cutover-delta-index.json"}
    required.update(f"state/{name}.json" for name in _ROLLBACK_STATE_SECTIONS)
    for replica in replicas:
        required.update({
            f"precutover/{replica}/snapshot.json",
            f"precutover/{replica}/snapshot.db",
            f"precutover/{replica}/v1-dump.jsonl",
            f"precutover/{replica}/v1-ref.json",
            f"receipts/seeds/{replica}/snapshot-seed.json",
            f"receipts/seeds/{replica}/delta-seed.json",
            f"receipts/delta/{replica}.json",
            f"receipts/no-tail/{replica}.json",
            f"receipts/fence/{replica}.json",
            f"receipts/activation/{replica}.json",
        })
    for op_id, classification in classifications.items():
        relative = (f"objects/accepted/{op_id}.json"
                    if classification in _ROLLBACK_CAUSAL_CLASSES
                    else f"objects/raw/{classification}/{op_id}.bin")
        required.add(relative)
    if not required <= set(files):
        raise MigrationError("rollback-collection-required-member-missing")


def create_rollback_bundle(*, epoch_id: str, membership_digest: str,
                           evidence_digest: str, equality_digest: str,
                           files: Mapping[str, bytes | bytearray | str | os.PathLike[str]],
                           accepted_operation_ids: Iterable[str],
                           classifications: Mapping[str, str],
                           out: str | os.PathLike[str],
                           unconfirmed_operation_ids: Iterable[str] = (),
                           apply: bool = False,
                           require_complete: bool = False,
                           collection_digest: str | None = None) -> dict[str, Any]:
    """Plan or atomically seal a lossless no-symlink rollback inventory."""
    _require_epoch(epoch_id)
    for value, reason in ((membership_digest, "membership-digest-invalid"),
                          (evidence_digest, "evidence-digest-invalid"),
                          (equality_digest, "equality-digest-invalid")):
        _require_digest(value, reason)
    normalized, inventory = {}, []
    for relative, source in files.items():
        clean = _relative(relative).as_posix()
        if clean == "bundle.json" or clean in normalized:
            raise MigrationError("bundle-member-duplicate")
        raw = _source_bytes(source); normalized[clean] = raw
        inventory.append({"path": clean, "sha256": digest_bytes(raw), "bytes": len(raw)})
    accepted = sorted(set(accepted_operation_ids))
    allowed_classes = {"accepted", "blocked", "deferred", "quarantined"}
    for op_id, classification in classifications.items():
        _require_digest(op_id, "bundle-classification-id-invalid")
        if classification not in allowed_classes:
            raise MigrationError("bundle-classification-invalid")
    for op_id in accepted:
        _require_digest(op_id, "bundle-accepted-id-invalid")
        if classifications.get(op_id) not in _ROLLBACK_CAUSAL_CLASSES:
            raise MigrationError("bundle-accepted-classification-invalid")
    if set(accepted) != {op_id for op_id, value in classifications.items()
                         if value in _ROLLBACK_CAUSAL_CLASSES}:
        raise MigrationError("bundle-accepted-set-classification-mismatch")
    unconfirmed = sorted(set(unconfirmed_operation_ids))
    for op_id in unconfirmed:
        _require_digest(op_id, "bundle-unconfirmed-id-invalid")
    if not set(unconfirmed) <= set(classifications):
        raise MigrationError("bundle-unconfirmed-object-missing")
    if require_complete:
        collection_digest = _require_digest(
            collection_digest, "rollback-collection-digest-required")
        _verify_complete_collection(normalized, classifications=classifications,
            accepted_operation_ids=accepted,
            unconfirmed_operation_ids=unconfirmed,
            collection_digest=collection_digest, epoch_id=epoch_id,
            membership_digest=membership_digest, evidence_digest=evidence_digest,
            equality_digest=equality_digest)
    elif collection_digest is not None:
        raise MigrationError("rollback-collection-without-complete-mode")
    rows = [{"op_id": op_id, "classification": classification}
            for op_id, classification in sorted(classifications.items())]
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "rollback-bundle", "epoch_id": epoch_id,
        "membership_digest": membership_digest, "evidence_digest": evidence_digest,
        "equality_digest": equality_digest, "accepted_operation_ids": accepted,
        "unconfirmed_operation_ids": unconfirmed,
        "classifications": rows, "complete": require_complete,
        "collection_digest": collection_digest,
        "inventory": sorted(inventory, key=lambda row: row["path"])})
    result = {**manifest, "changed": False}
    if not apply: return result
    final = Path(out)
    if not final.is_absolute(): raise MigrationError("output-must-be-absolute")
    final = final.parent.resolve(strict=True) / final.name
    if final.exists():
        if verify_rollback_bundle(final, require_complete=require_complete) != manifest:
            raise MigrationError("bundle-equivocation")
        return result
    stage = Path(tempfile.mkdtemp(prefix=f".{final.name}.", dir=final.parent))
    try:
        os.chmod(stage, 0o700)
        for relative, raw in normalized.items():
            _atomic_write(_contained(stage, relative), raw)
        _atomic_write(_contained(stage, "bundle.json"), canonical_bytes(manifest))
        verify_rollback_bundle(stage, require_complete=require_complete)
        os.replace(stage, final); _fsync_dir(final.parent)
        verify_rollback_bundle(final, require_complete=require_complete)
        result["changed"] = True
        return result
    finally:
        if stage.exists(): shutil.rmtree(stage)


def verify_rollback_bundle(value: str | os.PathLike[str], *,
                           require_complete: bool = False) -> dict[str, Any]:
    root = _output_root(value, create=False)
    manifest = load_manifest(_contained(root, "bundle.json")); _verify_digest(manifest)
    if manifest.get("kind") != "rollback-bundle":
        raise MigrationError("bundle-manifest-invalid")
    _require_epoch(str(manifest.get("epoch_id", "")))
    for field in ("membership_digest", "evidence_digest", "equality_digest"):
        _require_digest(manifest.get(field), f"bundle-{field}-invalid")
    inventory, raw_by_path = set(), {}
    inventory_rows = manifest.get("inventory")
    if not isinstance(inventory_rows, list) or any(
            not isinstance(row, dict) or set(row) != {"path", "sha256", "bytes"}
            for row in inventory_rows) \
            or inventory_rows != sorted(inventory_rows,
                                        key=lambda row: row["path"]):
        raise MigrationError("bundle-inventory-invalid")
    for row in inventory_rows:
        relative = _relative(row["path"]).as_posix()
        _require_digest(row["sha256"], "bundle-member-digest-invalid")
        if not isinstance(row["bytes"], int) or isinstance(row["bytes"], bool) \
                or row["bytes"] < 0:
            raise MigrationError("bundle-member-size-invalid")
        if relative in inventory: raise MigrationError("bundle-member-duplicate")
        raw = _read_file(_contained(root, relative))
        if len(raw) != row.get("bytes") or digest_bytes(raw) != row.get("sha256"):
            raise MigrationError("bundle-member-digest-mismatch")
        inventory.add(relative); raw_by_path[relative] = raw
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink(): raise MigrationError("bundle-symlink-forbidden")
        if path.is_file(): actual.add(path.relative_to(root).as_posix())
    if actual != inventory | {"bundle.json"}:
        raise MigrationError("bundle-inventory-mismatch")
    classification_rows = manifest.get("classifications")
    if not isinstance(classification_rows, list):
        raise MigrationError("bundle-classifications-invalid")
    classifications = {}
    for row in classification_rows:
        if not isinstance(row, dict) or set(row) != {"op_id", "classification"}:
            raise MigrationError("bundle-classification-invalid")
        op_id, classification = row["op_id"], row["classification"]
        _require_digest(op_id, "bundle-classification-id-invalid")
        if classification not in {"accepted", "blocked", "deferred",
                                   "quarantined"} or op_id in classifications:
            raise MigrationError("bundle-classification-invalid")
        classifications[op_id] = classification
    expected_rows = [{"op_id": op_id, "classification": classification}
                     for op_id, classification in sorted(classifications.items())]
    if classification_rows != expected_rows:
        raise MigrationError("bundle-classifications-not-canonical")
    accepted_list = manifest.get("accepted_operation_ids", ())
    if not isinstance(accepted_list, list) or accepted_list != sorted(set(accepted_list)):
        raise MigrationError("bundle-accepted-set-invalid")
    accepted = set(accepted_list)
    if any(classifications.get(op_id) not in _ROLLBACK_CAUSAL_CLASSES
           for op_id in accepted):
        raise MigrationError("bundle-accepted-classification-invalid")
    if accepted != {op_id for op_id, value in classifications.items()
                    if value in _ROLLBACK_CAUSAL_CLASSES}:
        raise MigrationError("bundle-accepted-set-classification-mismatch")
    unconfirmed_list = manifest.get("unconfirmed_operation_ids")
    if not isinstance(unconfirmed_list, list) \
            or unconfirmed_list != sorted(set(unconfirmed_list)):
        raise MigrationError("bundle-unconfirmed-set-invalid")
    for op_id in unconfirmed_list:
        _require_digest(op_id, "bundle-unconfirmed-id-invalid")
    if not set(unconfirmed_list) <= set(classifications):
        raise MigrationError("bundle-unconfirmed-object-missing")
    objects = {}
    preserved_ids = set()
    for relative, raw in raw_by_path.items():
        name = PurePosixPath(relative).name
        stem = name.rsplit(".", 1)[0]
        if HEX64_RE.fullmatch(stem): preserved_ids.add(stem)
        if name.endswith(".json") and HEX64_RE.fullmatch(name[:-5]):
            objects[name[:-5]] = raw
    if not set(classifications) <= preserved_ids:
        raise MigrationError("bundle-classified-object-missing")
    if not accepted <= set(objects): raise MigrationError("bundle-accepted-object-missing")
    for op_id in accepted:
        actual_id, canonical, _ = _operation(objects[op_id],
                                              require_supported=True)
        if actual_id != op_id or canonical != objects[op_id]:
            raise MigrationError("bundle-accepted-object-invalid")
        if any(parent not in accepted for parent in _parents(canonical)):
            raise MigrationError("bundle-accepted-closure-missing")
    complete = manifest.get("complete") is True
    if require_complete and not complete:
        raise MigrationError("rollback-complete-bundle-required")
    if complete:
        digest = _require_digest(manifest.get("collection_digest"),
                                 "rollback-collection-digest-missing")
        _verify_complete_collection(raw_by_path,
            classifications=classifications,
            accepted_operation_ids=accepted_list,
            unconfirmed_operation_ids=unconfirmed_list,
            collection_digest=digest,
            epoch_id=str(manifest.get("epoch_id", "")),
            membership_digest=str(manifest.get("membership_digest", "")),
            evidence_digest=str(manifest.get("evidence_digest", "")),
            equality_digest=str(manifest.get("equality_digest", "")))
    # Raw deferred/quarantined members are inventory-verified but deliberately
    # excluded from accepted-set causal closure.
    return manifest


def export_v1_projection(*, epoch_id: str, bundle: str | os.PathLike[str],
                         records: Sequence[Mapping[str, Any]],
                         loss_items: Mapping[str, Iterable[str]],
                         out: str | os.PathLike[str] | None = None,
                         apply: bool = False) -> dict[str, Any]:
    """Plan/write a v1 compatibility projection, never a lossless authority.

    Any declared non-representable item produces a deterministic loss report
    but no dump.  This is the fail-closed signal used by rollback apply before
    old-writer activation.
    """
    _require_epoch(epoch_id)
    bundle_manifest = verify_rollback_bundle(bundle)
    if bundle_manifest.get("epoch_id") != epoch_id:
        raise MigrationError("v1-export-epoch-mismatch")
    normalized_losses = {}
    for reason, values in sorted(loss_items.items()):
        normalized = sorted(set(map(str, values)))
        if normalized:
            normalized_losses[str(reason)] = normalized
    # Any non-accepted raw classification is inherently non-representable in
    # the v1 materialized dump, even if a caller omits it from loss_items.
    classified_losses = sorted(str(row.get("op_id"))
        for row in bundle_manifest.get("classifications", ())
        if row.get("classification") != "accepted")
    if classified_losses:
        normalized_losses.setdefault("non-accepted-v2-objects", classified_losses)
    rows, record_ids = [], set()
    for source in records:
        row = dict(source); record_id = row.get("id")
        if not isinstance(record_id, str) or not record_id or record_id in record_ids:
            raise MigrationError("v1-export-record-id-invalid")
        record_ids.add(record_id); rows.append(row)
    rows.sort(key=lambda row: row["id"])
    dump = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    loss_report = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "v1-loss-report", "epoch_id": epoch_id,
        "bundle_digest": bundle_manifest["manifest_digest"],
        "representable": not normalized_losses, "losses": normalized_losses})
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "v1-compatibility-projection", "epoch_id": epoch_id,
        "bundle_digest": bundle_manifest["manifest_digest"],
        "representable": not normalized_losses,
        "dump": ({"path": "dump.jsonl", "sha256": digest_bytes(dump),
                  "bytes": len(dump), "records": len(rows)}
                 if not normalized_losses else None),
        "loss_report_digest": loss_report["manifest_digest"],
        "writer_reactivation_allowed": not normalized_losses})
    result = {**manifest, "changed": False,
              "status": "local-only" if not normalized_losses else "hard-failure",
              "reason": "ok" if not normalized_losses else "v1-non-representable"}
    if not apply: return result
    if out is None: raise MigrationError("output-required")
    root = _output_root(out, create=True); changed = False
    changed = _atomic_write(_contained(root, "loss-report.json"),
                            canonical_bytes(loss_report)) or changed
    if not normalized_losses:
        changed = _atomic_write(_contained(root, "dump.jsonl"), dump) or changed
    changed = _atomic_write(_contained(root, "v1-export.json"),
                            canonical_bytes(manifest)) or changed
    result["changed"] = changed
    verify_v1_projection(root / "v1-export.json")
    return result


def verify_v1_projection(value: Mapping[str, Any] | str | os.PathLike[str], *,
                         root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "v1-compatibility-projection":
        raise MigrationError("v1-export-manifest-invalid")
    if root is None:
        if isinstance(value, Mapping): raise MigrationError("v1-export-root-required")
        root_path = Path(value).parent.resolve(strict=True)
    else: root_path = _output_root(root, create=False)
    loss = load_manifest(_contained(root_path, "loss-report.json")); _verify_digest(loss)
    if loss.get("manifest_digest") != manifest.get("loss_report_digest") \
            or loss.get("bundle_digest") != manifest.get("bundle_digest"):
        raise MigrationError("v1-loss-report-binding-mismatch")
    dump_info = manifest.get("dump")
    if manifest.get("representable") is True:
        if not isinstance(dump_info, dict) or loss.get("representable") is not True:
            raise MigrationError("v1-export-representability-mismatch")
        raw = _read_file(_contained(root_path, str(dump_info.get("path", ""))))
        if len(raw) != dump_info.get("bytes") or digest_bytes(raw) != dump_info.get("sha256"):
            raise MigrationError("v1-export-dump-mismatch")
        lines = raw.splitlines()
        try: decoded = [json.loads(line) for line in lines]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationError("v1-export-dump-invalid", str(exc)) from exc
        if any(canonical_bytes(row) != line for row, line in zip(decoded, lines)) \
                or len(decoded) != dump_info.get("records"):
            raise MigrationError("v1-export-dump-not-canonical")
    else:
        if dump_info is not None or loss.get("representable") is not False \
                or _contained(root_path, "dump.jsonl").exists():
            raise MigrationError("v1-export-lossy-dump-forbidden")
    return manifest


def _rollback_install_authority(
        bundle: str | os.PathLike[str],
        projection: str | os.PathLike[str],
) -> tuple[dict[str, Any], dict[str, Any], bytes, list[dict[str, Any]]]:
    bundle_manifest = verify_rollback_bundle(bundle, require_complete=True)
    projection_manifest = verify_v1_projection(projection)
    if projection_manifest.get("representable") is not True \
            or projection_manifest.get("writer_reactivation_allowed") is not True:
        raise MigrationError("rollback-projection-not-representable")
    if projection_manifest.get("bundle_digest") != bundle_manifest["manifest_digest"]:
        raise MigrationError("rollback-projection-bundle-mismatch")
    bundle_root = _output_root(bundle, create=False)
    projection_path = Path(projection)
    if not projection_path.is_absolute() or projection_path.is_symlink() \
            or not projection_path.is_file():
        raise MigrationError("rollback-projection-manifest-required")
    projection_root = projection_path.parent.resolve(strict=True)
    materialized = _read_file(_contained(bundle_root, "state/materialized.jsonl"))
    dump_info = projection_manifest.get("dump")
    if not isinstance(dump_info, dict):
        raise MigrationError("rollback-projection-dump-missing")
    projection_bytes = _read_file(_contained(
        projection_root, str(dump_info.get("path", ""))))
    if projection_bytes != materialized:
        raise MigrationError("rollback-projection-not-bundle-materialized")
    _canonical_jsonl(projection_bytes)
    rows = []
    for line in projection_bytes.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise MigrationError("rollback-projection-record-invalid")
        rows.append(value)
    ids = [row.get("id") for row in rows]
    if any(not isinstance(value, str) or not value for value in ids) \
            or ids != sorted(ids) or len(ids) != len(set(ids)):
        raise MigrationError("rollback-projection-record-order-invalid")
    return bundle_manifest, projection_manifest, projection_bytes, rows


def _store_path_digest(path: Path) -> str:
    return digest_json({"absolute_store_path": str(path.resolve(strict=True))})


def _record_preimage(connection: sqlite3.Connection) -> tuple[list[str], str]:
    columns = [str(row[1]) for row in connection.execute(
        "PRAGMA table_info(records)")]
    if not columns or "id" not in columns:
        raise MigrationError("rollback-target-records-schema-invalid")
    quoted = ",".join(f'"{name.replace(chr(34), chr(34) * 2)}"'
                      for name in columns)
    rows = []
    for source in connection.execute(
            f'SELECT {quoted} FROM records ORDER BY "id"'):
        values = list(source)
        if any(isinstance(value, (bytes, bytearray, memoryview))
               for value in values):
            raise MigrationError("rollback-target-record-value-invalid")
        rows.append(values)
    return columns, digest_json({"columns": columns, "rows": rows})


def _target_runtime(connection: sqlite3.Connection, *, epoch_id: str,
                    replica_id: str, membership_digest: str,
                    bundle_digest: str) -> tuple[dict[str, Any], str]:
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise MigrationError("rollback-target-integrity-failed")
    replicas = [str(row[0]) for row in connection.execute(
        "SELECT replica_id FROM sync_replica WHERE active=1 ORDER BY replica_id")]
    if replicas != [replica_id]:
        raise MigrationError("rollback-target-replica-mismatch")
    state = dict(_sync_module().migration_status(connection, epoch_id))
    if state.get("migration_state") != "rollback-window" \
            or state.get("writer_mode") != "fenced" \
            or state.get("membership_digest") != membership_digest \
            or state.get("rollback_bundle_digest") is not None:
        raise MigrationError("rollback-target-state-invalid")
    prepared = connection.execute(
        "SELECT bundle_digest,state FROM sync_migration_rollback WHERE epoch_id=?",
        (epoch_id,)).fetchone()
    if prepared is None or str(prepared[0]) != bundle_digest \
            or str(prepared[1]) not in {"prepared", "applied"}:
        raise MigrationError("rollback-target-bundle-not-prepared")
    _, preimage = _record_preimage(connection)
    return state, preimage


def create_rollback_target_manifest(
        *, epoch_id: str, replica_id: str, db_path: str | os.PathLike[str],
        bundle: str | os.PathLike[str], projection: str | os.PathLike[str],
        out: str | os.PathLike[str] | None = None, apply: bool = False,
) -> dict[str, Any]:
    """Bind one fenced rollback target to a complete bundle and projection."""
    epoch_id = _require_epoch(epoch_id); replica_id = _require_replica(replica_id)
    bundle_manifest, projection_manifest, raw, rows = \
        _rollback_install_authority(bundle, projection)
    if bundle_manifest.get("epoch_id") != epoch_id:
        raise MigrationError("rollback-target-epoch-mismatch")
    path = Path(db_path)
    connection = _open_ro(path)
    try:
        state, preimage = _target_runtime(connection, epoch_id=epoch_id,
            replica_id=replica_id,
            membership_digest=bundle_manifest["membership_digest"],
            bundle_digest=bundle_manifest["manifest_digest"])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    manifest = _with_digest({"manifest_version": 1, "protocol_major": 2,
        "kind": "rollback-target", "epoch_id": epoch_id,
        "replica_id": replica_id,
        "membership_digest": bundle_manifest["membership_digest"],
        "bundle_digest": bundle_manifest["manifest_digest"],
        "projection_digest": projection_manifest["manifest_digest"],
        "materialized_sha256": digest_bytes(raw),
        "materialized_bytes": len(raw), "record_count": len(rows),
        "store_path_digest": _store_path_digest(path),
        "schema_user_version": user_version,
        "expected_state_digest": state["state_digest"],
        "preimage_record_digest": preimage,
        "writer_mode": "fenced", "install_source": "complete-v2-bundle",
        "precutover_backup_only": False, "protected_ref_action": "none"})
    result = {**manifest, "changed": False}
    if apply:
        if out is None:
            raise MigrationError("output-required")
        result["changed"] = _atomic_write(
            _contained(_output_root(out, create=True), "target.json"),
            canonical_bytes(manifest))
    return result


def verify_rollback_target_manifest(
        value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    manifest = load_manifest(value); _verify_digest(manifest)
    if manifest.get("kind") != "rollback-target" \
            or manifest.get("protocol_major") != 2 \
            or manifest.get("install_source") != "complete-v2-bundle" \
            or manifest.get("precutover_backup_only") is not False \
            or manifest.get("protected_ref_action") != "none" \
            or manifest.get("writer_mode") != "fenced":
        raise MigrationError("rollback-target-manifest-invalid")
    _require_epoch(str(manifest.get("epoch_id", "")))
    _require_replica(str(manifest.get("replica_id", "")))
    for field in ("membership_digest", "bundle_digest", "projection_digest",
                  "materialized_sha256", "store_path_digest",
                  "expected_state_digest", "preimage_record_digest"):
        _require_digest(manifest.get(field), f"rollback-target-{field}-invalid")
    for field in ("materialized_bytes", "record_count", "schema_user_version"):
        value = manifest.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise MigrationError(f"rollback-target-{field}-invalid")
    return manifest


def verify_rollback_target_request(
        value: Mapping[str, Any] | str | os.PathLike[str]) -> dict[str, Any]:
    """Verify the local operator input consumed by ``rollback apply --target``."""
    request = load_manifest(value); _verify_digest(request)
    required = {"schema_version", "protocol_major", "kind", "epoch_id",
        "replica_id", "store", "projection", "install_out", "manifest_digest"}
    if set(request) != required or request.get("schema_version") != 1 \
            or request.get("protocol_major") != 2 \
            or request.get("kind") != "rollback-target-request":
        raise MigrationError("rollback-target-request-invalid")
    _require_epoch(str(request.get("epoch_id", "")))
    _require_replica(str(request.get("replica_id", "")))
    store, projection, install_out = (Path(str(request[name])) for name in
                                      ("store", "projection", "install_out"))
    if any(not path.is_absolute() for path in (store, projection, install_out)):
        raise MigrationError("rollback-target-request-path-not-absolute")
    if store.is_symlink() or not store.is_file() \
            or projection.is_symlink() or not projection.is_file():
        raise MigrationError("rollback-target-request-input-invalid")
    if install_out.exists() and (install_out.is_symlink()
                                 or not install_out.is_dir()):
        raise MigrationError("rollback-target-request-output-invalid")
    install_out.parent.resolve(strict=True)
    return request


def _fresh_sqlite_backup(source: Path, destination: Path) -> dict[str, Any]:
    fd, name = tempfile.mkstemp(prefix=".target-backup.", dir=destination.parent)
    os.close(fd); temporary = Path(name)
    try:
        temporary.unlink()
        source_connection = _open_ro(source)
        target_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close(); source_connection.close()
        fd = os.open(temporary, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        check = _open_static_ro(temporary)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("rollback-target-backup-integrity-failed")
            _, record_digest = _record_preimage(check)
        finally:
            check.close()
        raw = _read_file(temporary)
        os.replace(temporary, destination); _fsync_dir(destination.parent)
        reopened = _read_file(destination)
        if reopened != raw:
            raise MigrationError("rollback-target-backup-reopen-mismatch")
        return {"path": destination.name, "sha256": digest_bytes(reopened),
                "bytes": len(reopened), "integrity": "ok",
                "record_digest": record_digest}
    finally:
        if temporary.exists(): temporary.unlink()


def _verify_record_projection(connection: sqlite3.Connection,
                              rows: Sequence[Mapping[str, Any]]) -> None:
    columns = [str(row[1]) for row in connection.execute(
        "PRAGMA table_info(records)")]
    if any(set(row) != set(columns) for row in rows):
        raise MigrationError("rollback-install-record-schema-mismatch")
    quoted = ",".join(f'"{name.replace(chr(34), chr(34) * 2)}"'
                      for name in columns)
    actual = connection.execute(
        f'SELECT {quoted} FROM records ORDER BY "id"').fetchall()
    if len(actual) != len(rows):
        raise MigrationError("rollback-install-record-count-mismatch")
    for expected, values in zip(rows, actual):
        for column, value in zip(columns, values):
            wanted = expected[column]
            if isinstance(wanted, (dict, list)):
                try: decoded = json.loads(value)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise MigrationError("rollback-install-record-json-mismatch") from exc
                if decoded != wanted:
                    raise MigrationError("rollback-install-record-value-mismatch")
            elif isinstance(wanted, bool):
                if value != int(wanted):
                    raise MigrationError("rollback-install-record-value-mismatch")
            elif value != wanted:
                raise MigrationError("rollback-install-record-value-mismatch")


class _InstallConnectionGuard:
    """Delegate SQLite operations while retaining transaction ownership."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def commit(self) -> None:
        raise MigrationError("rollback-installer-commit-forbidden")

    def rollback(self) -> None:
        raise MigrationError("rollback-installer-rollback-forbidden")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def verify_rollback_install(value: str | os.PathLike[str], *,
                            require_installed: bool = False) -> dict[str, Any]:
    root = _output_root(value, create=False)
    manifest = load_manifest(_contained(root, "install.json")); _verify_digest(manifest)
    if manifest.get("kind") != "rollback-install" \
            or manifest.get("precutover_backup_only") is not False \
            or manifest.get("protected_ref_action") != "none":
        raise MigrationError("rollback-install-manifest-invalid")
    backup = manifest.get("fresh_target_backup")
    projection = manifest.get("verified_projection")
    if not isinstance(backup, dict) or not isinstance(projection, dict):
        raise MigrationError("rollback-install-inventory-invalid")
    backup_path = _contained(root, str(backup.get("path", "")))
    projection_path = _contained(root, str(projection.get("path", "")))
    for info, path in ((backup, backup_path), (projection, projection_path)):
        raw = _read_file(path)
        if len(raw) != info.get("bytes") or digest_bytes(raw) != info.get("sha256"):
            raise MigrationError("rollback-install-member-mismatch")
    check = _open_static_ro(backup_path)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError("rollback-target-backup-integrity-failed")
        _, backup_record_digest = _record_preimage(check)
        if backup_record_digest != backup.get("record_digest") \
                or backup_record_digest != manifest.get("preimage_record_digest"):
            raise MigrationError("rollback-target-backup-state-mismatch")
    finally:
        check.close()
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file()}
    allowed = {"install.json", str(backup["path"]), str(projection["path"])}
    result_path = _contained(root, "install-result.json")
    result = None
    if result_path.exists():
        result = load_manifest(result_path); _verify_digest(result)
        if result.get("kind") != "rollback-install-result" \
                or result.get("install_digest") != manifest["manifest_digest"] \
                or result.get("bundle_digest") != manifest.get("bundle_digest") \
                or result.get("projection_sha256") != projection.get("sha256") \
                or result.get("target_backup_sha256") != backup.get("sha256"):
            raise MigrationError("rollback-install-result-binding-mismatch")
        allowed.add("install-result.json")
    if actual != allowed:
        raise MigrationError("rollback-install-inventory-mismatch")
    if require_installed and result is None:
        raise MigrationError("rollback-install-result-required")
    return {"install_manifest": manifest, "install_result": result,
            "installed": result is not None}


def install_rollback_projection(
        *, epoch_id: str, db_path: str | os.PathLike[str],
        bundle: str | os.PathLike[str], projection: str | os.PathLike[str],
        target: Mapping[str, Any] | str | os.PathLike[str],
        out: str | os.PathLike[str] | None = None, apply: bool = False,
        installer: Callable[[Any, Sequence[Mapping[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Back up and atomically install only a verified complete projection.

    ``installer`` runs under this function's ``BEGIN IMMEDIATE`` transaction
    through a guard that forbids commit/rollback.  The engine verifies the
    resulting base ``records`` projection before it owns the commit.
    """
    epoch_id = _require_epoch(epoch_id)
    target_manifest = verify_rollback_target_manifest(target)
    bundle_manifest, projection_manifest, raw, rows = \
        _rollback_install_authority(bundle, projection)
    path = Path(db_path)
    expected = {
        "epoch_id": epoch_id,
        "membership_digest": bundle_manifest["membership_digest"],
        "bundle_digest": bundle_manifest["manifest_digest"],
        "projection_digest": projection_manifest["manifest_digest"],
        "materialized_sha256": digest_bytes(raw),
        "materialized_bytes": len(raw), "record_count": len(rows),
        "store_path_digest": _store_path_digest(path),
    }
    if any(target_manifest.get(field) != value for field, value in expected.items()):
        raise MigrationError("rollback-install-target-binding-mismatch")
    plan = {"schema_version": 1, "protocol_major": 2,
        "kind": "rollback-install-plan", "epoch_id": epoch_id,
        "replica_id": target_manifest["replica_id"],
        "target_manifest_digest": target_manifest["manifest_digest"],
        "bundle_digest": bundle_manifest["manifest_digest"],
        "projection_digest": projection_manifest["manifest_digest"],
        "projection_sha256": digest_bytes(raw), "record_count": len(rows),
        "precutover_backup_only": False, "protected_ref_action": "none"}
    if not apply:
        return {**plan, "plan_digest": digest_json(plan), "changed": False}
    if out is None or installer is None:
        raise MigrationError("rollback-install-output-and-installer-required")
    final = Path(out)
    if not final.is_absolute():
        raise MigrationError("output-must-be-absolute")
    final = final.parent.resolve(strict=True) / final.name
    lock = _open_snapshot_lock(path)
    try:
        _sync_module().register_writer_functions(
            lock, protocol_major=2, cutover_authority=True)
        state, preimage = _target_runtime(lock, epoch_id=epoch_id,
            replica_id=target_manifest["replica_id"],
            membership_digest=bundle_manifest["membership_digest"],
            bundle_digest=bundle_manifest["manifest_digest"])
        if state["state_digest"] != target_manifest["expected_state_digest"]:
            raise MigrationError("rollback-install-state-raced")
        prepared = None
        if final.exists():
            verified = verify_rollback_install(final)
            prepared = verified["install_manifest"]
            binding = {"epoch_id": epoch_id,
                "replica_id": target_manifest["replica_id"],
                "target_manifest_digest": target_manifest["manifest_digest"],
                "bundle_digest": bundle_manifest["manifest_digest"],
                "projection_digest": projection_manifest["manifest_digest"],
                "preimage_record_digest": target_manifest["preimage_record_digest"]}
            if any(prepared.get(field) != value for field, value in binding.items()):
                raise MigrationError("rollback-install-retry-equivocation")
            if verified["installed"]:
                _verify_record_projection(lock, rows)
                return {**verified["install_result"], "changed": False}
            try:
                _verify_record_projection(lock, rows)
            except MigrationError:
                pass
            else:
                # Crash recovery: the DB commit may have completed before the
                # result receipt became durable.  Seal the already-verified
                # projection without executing the installer again.
                lock.commit()
                recovered = _with_digest({"manifest_version": 1,
                    "protocol_major": 2, "kind": "rollback-install-result",
                    "epoch_id": epoch_id,
                    "replica_id": target_manifest["replica_id"],
                    "install_digest": prepared["manifest_digest"],
                    "bundle_digest": bundle_manifest["manifest_digest"],
                    "projection_sha256": digest_bytes(raw),
                    "target_backup_sha256":
                        prepared["fresh_target_backup"]["sha256"],
                    "installed_record_digest": digest_json(rows),
                    "precutover_backup_only": False,
                    "protected_ref_action": "none"})
                _atomic_write(_contained(final, "install-result.json"),
                              canonical_bytes(recovered))
                verify_rollback_install(final, require_installed=True)
                return {**recovered, "changed": False, "recovered": True}
        else:
            if preimage != target_manifest["preimage_record_digest"]:
                raise MigrationError("rollback-install-target-raced")
            stage = Path(tempfile.mkdtemp(prefix=f".{final.name}.",
                                          dir=final.parent))
            try:
                os.chmod(stage, 0o700)
                backup = _fresh_sqlite_backup(path, stage / "target-backup.db")
                _atomic_write(stage / "verified-projection.jsonl", raw)
                projection_info = {"path": "verified-projection.jsonl",
                    "sha256": digest_bytes(raw), "bytes": len(raw),
                    "records": len(rows)}
                prepared = _with_digest({"manifest_version": 1,
                    "protocol_major": 2, "kind": "rollback-install",
                    "epoch_id": epoch_id,
                    "replica_id": target_manifest["replica_id"],
                    "membership_digest": bundle_manifest["membership_digest"],
                    "target_manifest_digest": target_manifest["manifest_digest"],
                    "bundle_digest": bundle_manifest["manifest_digest"],
                    "projection_digest": projection_manifest["manifest_digest"],
                    "expected_state_digest": state["state_digest"],
                    "preimage_record_digest": preimage,
                    "fresh_target_backup": backup,
                    "verified_projection": projection_info,
                    "precutover_backup_only": False,
                    "protected_ref_action": "none"})
                _atomic_write(stage / "install.json", canonical_bytes(prepared))
                verify_rollback_install(stage)
                os.replace(stage, final); _fsync_dir(final.parent)
            finally:
                if stage.exists(): shutil.rmtree(stage)
        if preimage != prepared["preimage_record_digest"]:
            # A prepared-but-not-installed retry must still name its original
            # target preimage; an already-installed retry was returned above.
            raise MigrationError("rollback-install-target-raced")
        installer(_InstallConnectionGuard(lock), tuple(dict(row) for row in rows))
        if not lock.in_transaction:
            raise MigrationError("rollback-installer-transaction-escaped")
        _verify_record_projection(lock, rows)
        lock.commit()
        result = _with_digest({"manifest_version": 1, "protocol_major": 2,
            "kind": "rollback-install-result", "epoch_id": epoch_id,
            "replica_id": target_manifest["replica_id"],
            "install_digest": prepared["manifest_digest"],
            "bundle_digest": bundle_manifest["manifest_digest"],
            "projection_sha256": digest_bytes(raw),
            "target_backup_sha256": prepared["fresh_target_backup"]["sha256"],
            "installed_record_digest": digest_json(rows),
            "precutover_backup_only": False, "protected_ref_action": "none"})
        _atomic_write(_contained(final, "install-result.json"),
                      canonical_bytes(result))
        verify_rollback_install(final, require_installed=True)
        return {**result, "changed": True}
    except Exception:
        if lock.in_transaction: lock.rollback()
        raise
    finally:
        lock.close()


__all__ = ["CANONICALIZER_VERSION", "MANIFEST_VERSION", "MIGRATION_STATES",
    "MigrationEngine", "MigrationError", "PROTOCOL_MAJOR", "build_seed_manifest",
    "build_graveyard_seed_operations",
    "canonical_bytes", "collect_rollback_bundle_inputs", "create_delta_manifest", "create_equality_report",
    "create_no_tail_report", "create_phase_receipt", "create_rollback_bundle",
    "create_rollback_target_manifest", "create_snapshot",
    "digest_bytes", "digest_json", "export_v1_projection",
    "graveyard_source_identities", "inspect_store",
    "install_rollback_projection",
    "load_manifest", "replica_equality_input_digest", "replica_report_digest",
    "rollback_seed_set_digest",
    "rollback_state_section_names", "seal_evidence", "seal_graveyard_source",
    "seal_membership", "state_digest",
    "verify_delta_manifest", "verify_equality_report", "verify_evidence",
    "verify_membership", "verify_no_tail_report", "verify_phase_receipt",
    "verify_phase_receipts", "verify_receipt",
    "verify_rollback_bundle", "verify_rollback_install",
    "verify_rollback_target_manifest", "verify_rollback_target_request",
    "verify_graveyard_source", "verify_seed_manifest", "verify_snapshot",
    "verify_v1_projection"]
