#!/usr/bin/env python3
"""SD-111 P1: durable completion-delivery pending-record state machine.

One JSON record per ``(recipient_digest, delivery_id)`` under
``<root>/pending-delivery/<sha256(recipient_key)>/<delivery_id>.json``. This
module is a pure state machine: it never reads ``jobs.log``, never inspects a
live process, and never decides *whether* a delivery is owed. P2's terminal
writer computes the intent (``dispatch_contract._delivery_intent_values``);
the carrier-independent materializer
(``dispatch_completion_join.materialize_pending_delivery``) is the sole
caller of :func:`create`. Carriers (P3/P4) only call :func:`claim` and the
emit/ack helpers on a record that already exists.

Base claim/emission state machine (SD-111 §13.33.1-(4)):

    None -> pending -> claimed -> {acked | sent-ambiguous | pending (lease reclaim)}
    sent-ambiguous -> {acked | pending (lease reclaim)}
    pending | claimed | sent-ambiguous -> expired
    acked, expired terminal

A validated recipient-side receipt may settle its exact open delivery through
:func:`ack_received` after reclaim; the generic :func:`ack` keeps its
token-bearing claim-state contract.

ack-before-output is forbidden on token-less surfaces (stdout
``systemMessage``, ``additionalContext``): those surfaces call
:func:`mark_sent_ambiguous`, never the generic :func:`ack`. The queue exposes
that token-bearing transition for recipients such as codex-managed-gateway;
carrier wiring withholds it for token-less recipients. The separate
:func:`ack_received` transition requires the caller's verified receipt binding.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import re
import dispatch_attempt_reference as attempt_reference
import dispatch_lock_order
from dispatch_receipt_identity import CANONICAL_RECEIPT_KEYS, CANONICAL_CHILD_KEYS, receipt_digest


class PendingDeliveryError(RuntimeError):
    """A pending-delivery record boundary could not be proved."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 2048
DIR_MODE = 0o700
FILE_MODE = 0o600

RECIPIENT_KINDS = frozenset({
    "claude-parent-runtime",
    "codex-stop-hook",
    "codex-managed-gateway",
    "codex-native-queue",
    "opencode-turn",
})

CLAIM_AUTHORITIES = frozenset({"generation-proven", "deliverer-unproven"})

# SD-113 §8 lazy upgrade: the grade §13.34.1-(7) assigns each recipient
# kind's harness. `claude-parent-runtime` is the one measured-unsupported
# harness this cycle (dispatch_session_sweep.sweep() always refuses its
# generation-proof demand today) -- every other kind is generation-proven.
# This is the contract's own mapping, not a guess, so it introduces no third
# grade and CLAIM_AUTHORITIES stays a 2-value enum.
_LEGACY_CLAIM_AUTHORITY_BY_RECIPIENT_KIND = {
    "claude-parent-runtime": "deliverer-unproven",
}
_LEGACY_CLAIM_AUTHORITY_DEFAULT = "generation-proven"

STATES = frozenset({"pending", "claimed", "sent-ambiguous", "acked", "expired", "rejected"})
OPEN_STATES = frozenset({"pending", "claimed", "sent-ambiguous"})

EXPIRY_REASONS = frozenset({
    "recipient-session-gone",
    "pending-delivery-ttl-exceeded",
    "receipt-row-superseded",
})
EXPIRY_ACTOR = "dispatch-reconcile"
# SD-111 §13.33.1-(7) v67: terminal records are kept for a retention window and
# then pruned by the same single declared actor (the dispatch reconcile path);
# open-state records are never pruned whatever their age. Ages are file mtimes:
# the terminal transition is the record's last write. A pruned record leaves a
# binding-bearing tombstone (`<record>.json.pruned`, never deleted) so the
# materialize backstop cannot resurrect it from the append-only jobs.log row
# (review round 1, B1).
TERMINAL_STATES = frozenset({"acked", "expired", "rejected"})
TERMINAL_RETENTION_SECONDS = 7 * 86400
ORPHAN_LOCK_RETENTION_SECONDS = 24 * 3600
TOMBSTONE_SUFFIX = ".pruned"
PRUNE_TOMBSTONE_SCHEMA = "delivery-prune-v1"
_LOCK_REOPEN_LIMIT = 16
_RECORD_LOCK_SUFFIX = ".json.lock"

REQUIRED_FIELDS = (
    "schema_version", "delivery_id", "recipient_kind", "recipient_digest",
    "session_generation", "session_generation_supported", "attempt_ids",
    "parent_attempt_id", "route_id", "route_node", "receipt_digest",
    "receipt", "row_revisions", "state", "created_at_ns", "claimed_at_ns",
    "claim_owner", "claim_deadline_ns", "claim_authority", "attempts",
    "last_attempt_at_ns", "acked_at_ns", "acked_by", "expiry_reason", "lineage",
)
RECOVERY_AUDIT_FIELDS = frozenset({"expiry_actor", "expiry_detail", "expired_at_ns"})
TIMESTAMP_PROVENANCE_FIELD = "timestamp_provenance"
TIMESTAMP_PROVENANCE_KEYS = frozenset({
    "record_materialized_at_utc", "record_materialized_source",
    "first_send_at_utc", "first_send_source", "last_send_at_utc", "last_send_source",
    "last_send_event_id",
    "receive_at_utc", "receive_source", "observed_send_count", "observed_redelivery_count",
})
COURIER_RETRY_FIELD = "courier_retry"
COURIER_RETRY_FIELDS = frozenset({
    "schema_version", "context_digest", "failure_digest", "failure_count",
    "last_failure_at_ns", "next_attempt_at_ns", "delay_ns", "base_delay_ns",
})
COURIER_RETRY_SCHEMA = 1
SUBMISSION_FENCE_FIELD = "submission_fence"
SUBMISSION_FENCE_FIELDS = frozenset({
    "schema_version", "delivery_id", "recipient_digest", "session_generation",
    "target_thread_id", "receipt_digest", "attempt", "state", "updated_at_ns",
    "queue_submission_id", "rejection_reason",
})
SUBMISSION_FENCE_SCHEMA = 1
SUBMISSION_FENCE_STATES = frozenset({"prepared", "accepted", "not-accepted"})
COURIER_RETRY_MAX_SECONDS = 15.0
COURIER_RETRY_SUFFIX = ".courier-retry"
COURIER_TIMELINE_SCHEMA = 1
COURIER_TIMELINE_SUFFIX = ".courier-timeline"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
IMMUTABLE_FIELDS = ("delivery_id", "recipient_digest", "attempt_ids", "receipt_digest")



def _canonical_receipt_digest(receipt: dict) -> str:
    if not isinstance(receipt, dict):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "receipt-not-dict")
    return receipt_digest(receipt)


def recipient_digest(recipient_key: str) -> str:
    if not recipient_key:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "recipient_key-empty")
    return hashlib.sha256(recipient_key.encode("utf-8")).hexdigest()


def record_directory(root: Path, recipient_key: str) -> Path:
    return Path(root) / "pending-delivery" / recipient_digest(recipient_key)


def record_path(root: Path, recipient_key: str, delivery_id: str) -> Path:
    if not delivery_id or not delivery_id.startswith("delivery-"):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "delivery_id")
    return record_directory(root, recipient_key) / f"{delivery_id}.json"


def courier_retry_path(path: Path) -> Path:
    """Optional retry state stays outside legacy public records."""

    return Path(path).with_name(Path(path).name + COURIER_RETRY_SUFFIX)


def courier_timeline_path(path: Path) -> Path:
    """Optional timestamp provenance stays outside legacy public records."""

    return Path(path).with_name(Path(path).name + COURIER_TIMELINE_SUFFIX)


def _validate_courier_retry(retry: object) -> dict:
    if (not isinstance(retry, dict) or set(retry) != COURIER_RETRY_FIELDS
            or retry.get("schema_version") != COURIER_RETRY_SCHEMA
            or not isinstance(retry.get("context_digest"), str)
            or (retry["context_digest"] and not _SHA256_RE.fullmatch(retry["context_digest"]))
            or not isinstance(retry.get("failure_digest"), str)
            or (retry["failure_digest"] and not _SHA256_RE.fullmatch(retry["failure_digest"]))
            or any(type(retry.get(name)) is not int for name in (
                "failure_count", "last_failure_at_ns", "next_attempt_at_ns",
                "delay_ns", "base_delay_ns"))
            or retry["failure_count"] < 0
            or retry["last_failure_at_ns"] < 0
            or retry["next_attempt_at_ns"] < 0
            or retry["delay_ns"] < 0
            or not (1 <= retry["base_delay_ns"] <= int(COURIER_RETRY_MAX_SECONDS * 1e9))
            or (retry["failure_count"] == 0 and any((
                retry["failure_digest"], retry["last_failure_at_ns"],
                retry["next_attempt_at_ns"], retry["delay_ns"])))
            or (retry["failure_count"] > 0 and (
                not retry["failure_digest"] or retry["last_failure_at_ns"] < 1
                or retry["next_attempt_at_ns"] < retry["last_failure_at_ns"]
                or retry["delay_ns"] < 1
                or retry["next_attempt_at_ns"] - retry["last_failure_at_ns"]
                != retry["delay_ns"]))):
        raise PendingDeliveryError("delivery-persistence-refused", "courier-retry-invalid")
    return retry


def _validate_submission_fence(fence: object, record: dict) -> dict:
    if (not isinstance(fence, dict)
            or set(fence) - {"record_identity"} != SUBMISSION_FENCE_FIELDS
            or fence.get("schema_version") != SUBMISSION_FENCE_SCHEMA
            or fence.get("delivery_id") != record.get("delivery_id")
            or fence.get("recipient_digest") != record.get("recipient_digest")
            or fence.get("session_generation") != record.get("session_generation")
            or fence.get("receipt_digest") != record.get("receipt_digest")
            or not isinstance(fence.get("target_thread_id"), str)
            or not fence["target_thread_id"] or len(fence["target_thread_id"]) > 512
            or any(ord(char) < 32 for char in fence["target_thread_id"])
            or type(fence.get("attempt")) is not int or fence["attempt"] < 0
            or fence.get("state") not in SUBMISSION_FENCE_STATES
            or type(fence.get("updated_at_ns")) is not int or fence["updated_at_ns"] < 1
            or (fence.get("queue_submission_id") is not None
                and (not isinstance(fence["queue_submission_id"], str)
                     or not fence["queue_submission_id"]
                     or len(fence["queue_submission_id"]) > 256))
            or (fence.get("rejection_reason") is not None
                and (not isinstance(fence["rejection_reason"], str)
                     or not fence["rejection_reason"]
                     or len(fence["rejection_reason"]) > 256))
            or (fence["state"] == "prepared" and (
                fence["attempt"] < 1 or fence["queue_submission_id"] is not None
                or fence["rejection_reason"] is not None))
            or (fence["state"] == "accepted" and fence["rejection_reason"] is not None)
            or (fence["state"] == "not-accepted" and (
                fence["attempt"] < 1 or fence["queue_submission_id"] is not None
                or fence["rejection_reason"] is None))):
        raise PendingDeliveryError("delivery-persistence-refused", "submission-fence-invalid")
    original = fence.get("record_identity")
    if "record_identity" in fence and (
            not isinstance(original, dict)
            or set(original) != set(_received_ack_identity(record))
            or any(original.get(key) != fence.get(key) for key in (
                "delivery_id", "recipient_digest", "session_generation", "receipt_digest"))
            or original.get("receipt_canonical_digest") != fence.get("receipt_digest")):
        raise PendingDeliveryError("delivery-persistence-refused", "submission-fence-identity-invalid")
    return fence


def _validate_record(value: object) -> dict:
    if not isinstance(value, dict):
        raise PendingDeliveryError("delivery-persistence-refused", "record-shape-invalid")
    keys = set(value)
    if keys == set(REQUIRED_FIELDS) - {"claim_authority"}:
        # SD-113 §8 lazy upgrade: a legacy v1 on-disk record (pre-`claim_
        # authority`) is decided in memory at read time, never rewritten
        # here -- the next normal CAS write (claim()) persists the upgrade.
        value = dict(value)
        value["claim_authority"] = _LEGACY_CLAIM_AUTHORITY_BY_RECIPIENT_KIND.get(
            value.get("recipient_kind"), _LEGACY_CLAIM_AUTHORITY_DEFAULT
        )
        keys = set(value)
    optional_fields = RECOVERY_AUDIT_FIELDS | {
        TIMESTAMP_PROVENANCE_FIELD, COURIER_RETRY_FIELD, SUBMISSION_FENCE_FIELD,
    }
    if not set(REQUIRED_FIELDS) <= keys or keys - set(REQUIRED_FIELDS) - optional_fields:
        raise PendingDeliveryError("delivery-persistence-refused", "record-shape-invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PendingDeliveryError("delivery-persistence-refused", "schema-version-invalid")
    if value.get("recipient_kind") not in RECIPIENT_KINDS:
        raise PendingDeliveryError("delivery-persistence-refused", "recipient-kind-invalid")
    if value.get("state") not in STATES:
        raise PendingDeliveryError("delivery-persistence-refused", "state-invalid")
    ordinary_notice = (isinstance(value.get("receipt"), dict)
                       and value["receipt"].get("kind") == "notice"
                       and value.get("route_node") == "notice"
                       and value.get("parent_attempt_id") == ""
                       and value.get("row_revisions") == {})
    if (not isinstance(value.get("attempt_ids"), list)
            or (not value["attempt_ids"] and not ordinary_notice)):
        raise PendingDeliveryError("delivery-persistence-refused", "attempt-ids-invalid")
    if not isinstance(value.get("attempts"), int):
        raise PendingDeliveryError("delivery-persistence-refused", "attempts-invalid")
    if COURIER_RETRY_FIELD in value:
        _validate_courier_retry(value[COURIER_RETRY_FIELD])
    if SUBMISSION_FENCE_FIELD in value:
        _validate_submission_fence(value[SUBMISSION_FENCE_FIELD], value)
    if (
        value.get("state") in {"claimed", "sent-ambiguous", "acked"}
        and value.get("claim_authority") not in CLAIM_AUTHORITIES
    ):
        raise PendingDeliveryError("delivery-persistence-refused", "claim-authority-invalid")
    audit_present = RECOVERY_AUDIT_FIELDS & keys
    if audit_present and (
        audit_present != RECOVERY_AUDIT_FIELDS
        or value.get("state") != "expired"
        or not isinstance(value.get("expiry_actor"), str)
        or not value["expiry_actor"]
        or not isinstance(value.get("expiry_detail"), str)
        or not value["expiry_detail"]
        or not isinstance(value.get("expired_at_ns"), int)
        or value["expired_at_ns"] < 1
    ):
        raise PendingDeliveryError("delivery-persistence-refused", "recovery-audit-invalid")
    if TIMESTAMP_PROVENANCE_FIELD in value:
        _validate_timestamp_provenance(value[TIMESTAMP_PROVENANCE_FIELD])
    return value


@contextmanager
def _record_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in (path.parent, path.parent.parent):
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
    lock_path = path.with_name(
        path.name[:-5] + _RECORD_LOCK_SUFFIX if path.name.endswith(".json")
        else path.name + ".lock"
    )
    # The lock is identified by inode, not by name: a lock file unlinked while a
    # waiter still holds the old inode (the orphan-lock prune, review round 1
    # M1) must not let two holders into the critical section. After acquiring
    # the flock, the fd's inode has to be the inode the path currently names;
    # otherwise the file was replaced and this holder reopens.
    for _attempt in range(_LOCK_REOPEN_LIMIT):
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, FILE_MODE)
        try:
            try:
                os.chmod(lock_path, FILE_MODE)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                try:
                    current = os.stat(lock_path).st_ino == os.fstat(fd).st_ino
                except FileNotFoundError:
                    current = False
                if current:
                    with dispatch_lock_order.acquired("delivery-record"):
                        yield
                    return
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    raise PendingDeliveryError("delivery-persistence-refused", "lock-file-replaced")


def _read_unlocked(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", "corrupt-record") from exc
    value = _validate_record(value)
    timeline = _read_timestamp_provenance_unlocked(path, value)
    if timeline is not None:
        value = dict(value)
        value[TIMESTAMP_PROVENANCE_FIELD] = timeline
    return value


def _public_record(value: dict) -> dict:
    """Return the persisted record shape without internal courier state."""

    internal = {COURIER_RETRY_FIELD, SUBMISSION_FENCE_FIELD}
    if not internal.intersection(value):
        return value
    return {key: item for key, item in value.items() if key not in internal}


@contextmanager
def _record_reference_lock(path: Path, *, candidate: dict | None = None):
    """Acquire attempt references before the record CAS lock.

    Read the current immutable references optimistically, then verify them
    again after the ordered reference set and record lock are held. If a
    supported writer changed the reference set before admission, reopen with
    the expanded old/new set. The candidate is supplied for creation, so its
    group directory and first record become visible under the same admission.
    """
    root = Path(path).parents[2]
    required = set(attempt_reference.record_attempts(candidate))
    for _attempt in range(_LOCK_REOPEN_LIMIT):
        before = _read_unlocked(path)
        required.update(attempt_reference.record_attempts(before))
        retry = False
        from worker_home_retention import home_reference_relevant, home_view_admission
        home_relevant = home_reference_relevant(root, required)
        home_scope = (home_view_admission(root / "jobs.log", blocking=True)
                      if home_relevant is not False else nullcontext(True))
        with attempt_reference.admission(root, required) as admitted:
            if not admitted:
                raise PendingDeliveryError("delivery-persistence-refused",
                                           "attempt-reference-admission-busy")
            with home_scope as home_admitted:
                if not home_admitted:
                    raise PendingDeliveryError("delivery-persistence-refused",
                                               "home-reference-admission-busy")
                with _record_lock(path):
                    current = _read_unlocked(path)
                    current_refs = set(attempt_reference.record_attempts(current))
                    if not current_refs.issubset(required):
                        required.update(current_refs)
                        retry = True
                    else:
                        yield current
                        return
        if not retry:
            break
    raise PendingDeliveryError("delivery-persistence-refused",
                               "attempt-reference-set-changed")


def _courier_retry_identity_digest(record: dict) -> str:
    identity = {key: record.get(key) for key in IMMUTABLE_FIELDS}
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_delivery_metadata_unlocked(path: Path, record: dict) -> dict | None:
    metadata_path = courier_retry_path(path)
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc
    try:
        metadata = json.loads(raw)
    except ValueError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", "courier-retry-corrupt") from exc
    required = {"schema_version", "delivery_id", "recipient_digest", "identity_digest"}
    optional = {"retry", TIMESTAMP_PROVENANCE_FIELD}
    if (not isinstance(metadata, dict)
            or not required <= set(metadata)
            or set(metadata) - required - optional
            or not (set(metadata) & optional)
            or metadata.get("schema_version") != COURIER_RETRY_SCHEMA
            or metadata.get("delivery_id") != record.get("delivery_id")
            or metadata.get("recipient_digest") != record.get("recipient_digest")
            or metadata.get("identity_digest") != _courier_retry_identity_digest(record)):
        raise PendingDeliveryError("delivery-persistence-refused", "courier-retry-identity-invalid")
    if "retry" in metadata:
        _validate_courier_retry(metadata["retry"])
    if TIMESTAMP_PROVENANCE_FIELD in metadata:
        _validate_timestamp_provenance(metadata[TIMESTAMP_PROVENANCE_FIELD])
    return metadata


def _read_courier_timeline_unlocked(path: Path, record: dict) -> dict | None:
    metadata_path = courier_timeline_path(path)
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc
    try:
        metadata = json.loads(raw)
    except ValueError as exc:
        raise PendingDeliveryError("delivery-persistence-refused",
                                   "courier-timeline-corrupt") from exc
    if (not isinstance(metadata, dict)
            or set(metadata) != {
                "schema_version", "delivery_id", "recipient_digest", "identity_digest",
                TIMESTAMP_PROVENANCE_FIELD,
            }
            or metadata.get("schema_version") != COURIER_TIMELINE_SCHEMA
            or metadata.get("delivery_id") != record.get("delivery_id")
            or metadata.get("recipient_digest") != record.get("recipient_digest")
            or metadata.get("identity_digest") != _courier_retry_identity_digest(record)):
        raise PendingDeliveryError("delivery-persistence-refused",
                                   "courier-timeline-identity-invalid")
    _validate_timestamp_provenance(metadata[TIMESTAMP_PROVENANCE_FIELD])
    return metadata[TIMESTAMP_PROVENANCE_FIELD]


def _read_courier_retry_unlocked(path: Path, record: dict) -> dict | None:
    """Read retry state bound to this record; legacy inline state is migrated on write."""

    metadata = _read_delivery_metadata_unlocked(path, record)
    if metadata is not None and "retry" in metadata:
        return _validate_courier_retry(metadata["retry"])
    legacy = record.get(COURIER_RETRY_FIELD)
    return _validate_courier_retry(legacy) if legacy is not None else None


def _read_timestamp_provenance_unlocked(path: Path, record: dict) -> dict | None:
    metadata = _read_delivery_metadata_unlocked(path, record)
    timeline = _read_courier_timeline_unlocked(path, record)
    if timeline is not None:
        return timeline
    if metadata is not None and TIMESTAMP_PROVENANCE_FIELD in metadata:
        return metadata[TIMESTAMP_PROVENANCE_FIELD]
    legacy = record.get(TIMESTAMP_PROVENANCE_FIELD)
    if legacy is not None:
        _validate_timestamp_provenance(legacy)
    return legacy


def _atomic_write_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, path)
    except OSError as exc:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_provenance(*, materialized: str | None = None,
                          materialized_source: str | None = None) -> dict:
    return {
        "record_materialized_at_utc": materialized,
        "record_materialized_source": materialized_source,
        "first_send_at_utc": None, "first_send_source": None,
        "last_send_at_utc": None, "last_send_source": None, "last_send_event_id": None,
        "receive_at_utc": None, "receive_source": None,
        "observed_send_count": 0, "observed_redelivery_count": 0,
    }


def _validate_timestamp_provenance(value: object) -> None:
    if not isinstance(value, dict) or set(value) != TIMESTAMP_PROVENANCE_KEYS:
        raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid")
    for key in ("record_materialized_at_utc", "first_send_at_utc", "last_send_at_utc", "receive_at_utc"):
        item = value[key]
        if item is not None:
            if not isinstance(item, str):
                raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid")
            try:
                normalized = item.removesuffix("Z") + ("+00:00" if item.endswith("Z") else "")
                datetime.datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid") from exc
    for key in ("record_materialized_source", "first_send_source", "last_send_source",
                "receive_source", "last_send_event_id"):
        item = value[key]
        if item is not None and (not isinstance(item, str) or not item or len(item) > 128
                                 or any(ord(char) < 32 for char in item)):
            raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid")
    for key in ("observed_send_count", "observed_redelivery_count"):
        item = value[key]
        if type(item) is not int or item < 0:
            raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid")
    if value["observed_redelivery_count"] > value["observed_send_count"]:
        raise PendingDeliveryError("delivery-persistence-refused", "timestamp-provenance-invalid")


def _record_received(value: dict, source: str) -> dict:
    timeline = value.get(TIMESTAMP_PROVENANCE_FIELD)
    if timeline is None:
        # A newly observed event never fills in a missing legacy timestamp.
        timeline = _timestamp_provenance(materialized_source="legacy-unknown")
    updated = dict(timeline)
    updated["receive_at_utc"] = _utc_now()
    updated["receive_source"] = source[:128]
    result = dict(value)
    result[TIMESTAMP_PROVENANCE_FIELD] = updated
    return result


def _remove_courier_retry_unlocked(path: Path) -> None:
    try:
        courier_retry_path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc


def _write_delivery_metadata_unlocked(path: Path, record: dict, retry: dict | None,
                                      timeline: dict | None, *,
                                      defer_retry_cleanup: bool = False) -> None:
    identity = {
        "delivery_id": record["delivery_id"],
        "recipient_digest": record["recipient_digest"],
        "identity_digest": _courier_retry_identity_digest(record),
    }
    retry_path = courier_retry_path(path)
    if retry is None:
        if not defer_retry_cleanup:
            _remove_courier_retry_unlocked(path)
    else:
        _atomic_write_json(retry_path, {
            "schema_version": COURIER_RETRY_SCHEMA,
            **identity,
            "retry": _validate_courier_retry(retry),
        })

    timeline_path = courier_timeline_path(path)
    if timeline is None:
        try:
            timeline_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PendingDeliveryError("delivery-persistence-refused", str(exc)) from exc
    else:
        _validate_timestamp_provenance(timeline)
        _atomic_write_json(timeline_path, {
            "schema_version": COURIER_TIMELINE_SCHEMA,
            **identity,
            TIMESTAMP_PROVENANCE_FIELD: timeline,
        })


def _write_unlocked(path: Path, record: dict) -> None:
    """Persist sidecar state first, then write the legacy-compatible record CAS."""

    _validate_record(record)
    previous = _read_unlocked(path)
    references = (attempt_reference.record_attempts(previous)
                  | attempt_reference.record_attempts(record))
    if not references.issubset(attempt_reference.held_attempts()):
        raise PendingDeliveryError("delivery-persistence-refused",
                                   "attempt-reference-admission-missing")
    persisted = dict(record)
    retry = persisted.pop(COURIER_RETRY_FIELD, None)
    timeline = persisted.pop(TIMESTAMP_PROVENANCE_FIELD, None)
    terminal = persisted.get("state") in TERMINAL_STATES
    if terminal:
        retry = None
    elif retry is None:
        # State-only transitions (for example claimed -> sent-ambiguous) keep
        # an existing retry deadline while refreshing the timing sidecar.
        retry = _read_courier_retry_unlocked(path, record)
    _write_delivery_metadata_unlocked(
        path, record, retry, timeline, defer_retry_cleanup=terminal)
    _validate_record(persisted)
    _atomic_write_json(path, persisted)
    if terminal:
        _remove_courier_retry_unlocked(path)


def _retry_context_digest(context: str | None) -> str | None:
    if context is None:
        return None
    if not isinstance(context, str) or len(context) > 2048:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "retry-context-invalid")
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def _new_courier_retry(context_digest: str = "", base_delay_ns: int = 50_000_000) -> dict:
    return {
        "schema_version": COURIER_RETRY_SCHEMA,
        "context_digest": context_digest,
        "failure_digest": "",
        "failure_count": 0,
        "last_failure_at_ns": 0,
        "next_attempt_at_ns": 0,
        "delay_ns": 0,
        "base_delay_ns": base_delay_ns,
    }


def _delay_ns(seconds: float) -> int:
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "retry-delay-invalid") from exc
    if not 0.05 <= value <= COURIER_RETRY_MAX_SECONDS:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "retry-delay-invalid")
    return int(value * 1_000_000_000)


def _advance_courier_retry(retry: dict, failure_reason: str, now_ns: int,
                           base_delay_ns: int) -> dict:
    failure_digest = hashlib.sha256(str(failure_reason)[:2048].encode("utf-8")).hexdigest()
    same = (retry.get("failure_digest") == failure_digest
            and retry.get("failure_count", 0) > 0
            and now_ns >= retry.get("last_failure_at_ns", 0))
    if same:
        failure_count = retry["failure_count"] + 1
        previous = max(base_delay_ns, retry.get("delay_ns", base_delay_ns))
        delay_ns = min(int(COURIER_RETRY_MAX_SECONDS * 1e9), max(
            base_delay_ns, int(previous * 1.7)))
    else:
        failure_count = 1
        delay_ns = base_delay_ns
    return {
        "schema_version": COURIER_RETRY_SCHEMA,
        "context_digest": retry.get("context_digest", ""),
        "failure_digest": failure_digest,
        "failure_count": failure_count,
        "last_failure_at_ns": now_ns,
        "next_attempt_at_ns": now_ns + delay_ns,
        "delay_ns": delay_ns,
        "base_delay_ns": base_delay_ns,
    }


def read(root: Path, recipient_key: str, delivery_id: str) -> dict | None:
    """Unlocked observer read -- fleet/P6 visibility, never a transition."""

    path = record_path(root, recipient_key, delivery_id)
    value = _read_unlocked(path)
    if value is None:
        return None
    return _public_record(value)


def submission_fence_identity(record: dict, target_thread_id: str) -> dict:
    """Bind one native queue destination to the immutable delivery identity."""
    if (not isinstance(record, dict) or not isinstance(target_thread_id, str)
            or not target_thread_id or len(target_thread_id) > 512
            or any(ord(char) < 32 for char in target_thread_id)):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-target-invalid")
    return {
        "delivery_id": record.get("delivery_id"),
        "recipient_digest": record.get("recipient_digest"),
        "session_generation": record.get("session_generation"),
        "target_thread_id": target_thread_id,
        "receipt_digest": record.get("receipt_digest"),
        "record_identity": _received_ack_identity(record),
    }


_SUBMISSION_IDENTITY_FIELDS = frozenset({
    "delivery_id", "recipient_digest", "session_generation",
    "target_thread_id", "receipt_digest",
})


def _check_submission_identity(record: dict, expected_identity: dict) -> None:
    if (not isinstance(expected_identity, dict)
            or set(expected_identity) - {"record_identity"} != _SUBMISSION_IDENTITY_FIELDS
            or expected_identity.get("delivery_id") != record.get("delivery_id")
            or expected_identity.get("recipient_digest") != record.get("recipient_digest")
            or expected_identity.get("session_generation") != record.get("session_generation")
            or expected_identity.get("receipt_digest") != record.get("receipt_digest")
            or not isinstance(expected_identity.get("target_thread_id"), str)
            or not expected_identity["target_thread_id"]
            or len(expected_identity["target_thread_id"]) > 512
            or any(ord(char) < 32 for char in expected_identity["target_thread_id"])):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-fence-identity")
    original = expected_identity.get("record_identity")
    if original is not None and _received_ack_identity(record) != original:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-record-changed")
    fence = record.get(SUBMISSION_FENCE_FIELD)
    if fence is not None and any(fence.get(key) != expected_identity.get(key)
                                 for key in _SUBMISSION_IDENTITY_FIELDS):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-fence-changed")
    if (fence is not None and fence.get("record_identity") is not None
            and fence["record_identity"] != original):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-record-changed")


def read_submission_fence(root: Path, recipient_key: str, delivery_id: str, *,
                          expected_identity: dict | None = None) -> dict | None:
    """Read internal queue-fence evidence without exposing it in public records."""
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if expected_identity is not None:
            _check_submission_identity(value, expected_identity)
        fence = value.get(SUBMISSION_FENCE_FIELD)
        return dict(fence) if fence is not None else None


def prepare_submission_fence(root: Path, recipient_key: str, delivery_id: str, *,
                             claim_owner: str, expected_identity: dict) -> dict:
    """Durably fence an exact native queue add before its RPC is issued."""
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value.get("state") != "claimed" or value.get("claim_owner") != claim_owner:
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-claim")
        _check_submission_identity(value, expected_identity)
        deadline = value.get("claim_deadline_ns")
        if type(deadline) is not int or deadline <= time.monotonic_ns():
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-lease-expired")
        old = value.get(SUBMISSION_FENCE_FIELD)
        if old is None:
            attempt = 1
        elif old["state"] == "not-accepted":
            attempt = old["attempt"] + 1
        else:
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-already-fenced")
        fence = {
            "schema_version": SUBMISSION_FENCE_SCHEMA,
            **expected_identity,
            "attempt": attempt,
            "state": "prepared",
            "updated_at_ns": time.monotonic_ns(),
            "queue_submission_id": None,
            "rejection_reason": None,
        }
        updated = dict(value)
        updated[SUBMISSION_FENCE_FIELD] = fence
        _write_unlocked(path, updated)
        return dict(fence)


def record_submission_accepted(root: Path, recipient_key: str, delivery_id: str, *,
                               claim_owner: str, expected_identity: dict,
                               queue_submission_id: str | None = None,
                               source: str = "queue-response",
                               expected_attempt: int | None = None) -> dict:
    """Persist queue acceptance or an exact pending/consumed observation."""
    if source not in {"queue-response", "exact-observation"}:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-source-invalid")
    if (queue_submission_id is not None
            and (not isinstance(queue_submission_id, str) or not queue_submission_id
                 or len(queue_submission_id) > 256)):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-id-invalid")
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value.get("state") != "claimed" or value.get("claim_owner") != claim_owner:
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-claim")
        _check_submission_identity(value, expected_identity)
        old = value.get(SUBMISSION_FENCE_FIELD)
        if old is None:
            if source != "exact-observation":
                raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-missing")
            attempt = 0
        else:
            attempt = old["attempt"]
            if expected_attempt is not None and attempt != expected_attempt:
                raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-attempt-changed")
            if old["state"] == "accepted":
                return dict(old)
            if old["state"] == "not-accepted" and source != "exact-observation":
                raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-not-prepared")
        fence = {
            "schema_version": SUBMISSION_FENCE_SCHEMA,
            **expected_identity,
            "attempt": attempt,
            "state": "accepted",
            "updated_at_ns": time.monotonic_ns(),
            "queue_submission_id": queue_submission_id,
            "rejection_reason": None,
        }
        updated = dict(value)
        updated[SUBMISSION_FENCE_FIELD] = fence
        _write_unlocked(path, updated)
        return dict(fence)


def record_submission_not_accepted(root: Path, recipient_key: str, delivery_id: str, *,
                                   claim_owner: str, expected_identity: dict,
                                   expected_attempt: int, reason: str) -> dict:
    """Reopen only an add attempt with an explicit nonacceptance response."""
    if (not isinstance(reason, str) or not reason.startswith(
            "queue-rpc-refused:thread/queue/add:") or len(reason) > 256):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "submission-refusal-unproven")
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value.get("state") != "claimed" or value.get("claim_owner") != claim_owner:
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-claim")
        _check_submission_identity(value, expected_identity)
        old = value.get(SUBMISSION_FENCE_FIELD)
        if (old is None or old["state"] != "prepared"
                or old["attempt"] != expected_attempt):
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-fence-not-prepared")
        fence = dict(old)
        fence.update(
            state="not-accepted", updated_at_ns=time.monotonic_ns(),
            queue_submission_id=None, rejection_reason=reason,
        )
        updated = dict(value)
        updated[SUBMISSION_FENCE_FIELD] = fence
        _write_unlocked(path, updated)
        return dict(fence)


def create(
    root: Path,
    *,
    recipient_kind: str,
    recipient_key: str,
    delivery_id: str,
    session_generation: str,
    session_generation_supported: str,
    attempt_ids: list[str],
    parent_attempt_id: str,
    route_id: str,
    route_node: str,
    receipt: dict,
    receipt_digest: str,
    row_revisions: dict[str, str],
    lineage: list[str] | None = None,
) -> dict:
    """Create one record, or -- idempotently -- verify identity of the
    existing one (O_EXCL semantics: N materializer triggers converge on one
    file, round 2 C-1)."""

    if recipient_kind not in RECIPIENT_KINDS:
        raise PendingDeliveryError("delivery-persistence-refused", "recipient-kind-unknown")
    ordinary_notice = (isinstance(receipt, dict) and receipt.get("kind") == "notice"
                       and route_node == "notice" and parent_attempt_id == ""
                       and not attempt_ids and not row_revisions)
    if ((not attempt_ids or not parent_attempt_id) and not ordinary_notice) \
            or not route_id or not route_node:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "identity-incomplete")
    computed_digest = _canonical_receipt_digest(receipt)
    if computed_digest != receipt_digest:
        raise PendingDeliveryError(
            "pending-delivery-identity-conflict", "receipt-digest-mismatch"
        )
    body_bytes = len(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    limit = MAX_RECEIPT_BYTES
    if receipt.get("replacement_lineage") or receipt.get("replacement_attention"):
        try:
            from dispatch_replacement import adopt_receipt, validate_attention
            jobs = Path(receipt["job_registry"])
            if jobs.resolve().parent != Path(root).resolve():
                raise ValueError("replacement-registry-scope-invalid")
            original = {child["attempt_id"] for child in receipt["children"]}
            if receipt.get("replacement_lineage"):
                for edge in receipt["replacement_lineage"]:
                    original.discard(edge["replacement_attempt_id"])
                    original.add(edge["original_attempt_id"])
                adopt_receipt(jobs, original, receipt)
            if receipt.get("replacement_attention"):
                validate_attention(jobs, receipt["replacement_attention"], allowed_attempts=original | {child["attempt_id"] for child in receipt["children"]})
            limit = 8192  # Four checked edges plus the unchanged four-child receipt.
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "replacement-proof-invalid") from exc
    if body_bytes > limit:
        raise PendingDeliveryError("pending-delivery-oversized")

    digest = recipient_digest(recipient_key)
    path = record_path(root, recipient_key, delivery_id)
    now = time.monotonic_ns()
    candidate = {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "recipient_kind": recipient_kind,
        "recipient_digest": digest,
        "session_generation": session_generation,
        "session_generation_supported": session_generation_supported,
        "attempt_ids": sorted(attempt_ids),
        "parent_attempt_id": parent_attempt_id,
        "route_id": route_id,
        "route_node": route_node,
        "receipt_digest": receipt_digest,
        "receipt": receipt,
        "row_revisions": dict(row_revisions),
        "state": "pending",
        "created_at_ns": now,
        "claimed_at_ns": None,
        "claim_owner": None,
        "claim_deadline_ns": None,
        "claim_authority": "",
        "attempts": 0,
        "last_attempt_at_ns": None,
        "acked_at_ns": None,
        "acked_by": None,
        "expiry_reason": None,
        "lineage": list(lineage or ()),
        TIMESTAMP_PROVENANCE_FIELD: _timestamp_provenance(
            materialized=_utc_now(), materialized_source="dispatch_pending_delivery.create"),
    }
    with _record_reference_lock(path, candidate=candidate):
        existing = _read_unlocked(path)
        if existing is not None:
            _verify_identity(existing, candidate)
            return _public_record(existing)
        if tombstone_path(path).exists():
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-pruned")
        _write_unlocked(path, candidate)
        return _public_record(candidate)


def _verify_identity(existing: dict, candidate: dict) -> None:
    for key in IMMUTABLE_FIELDS:
        if existing.get(key) != candidate.get(key):
            raise PendingDeliveryError("pending-delivery-identity-conflict", key)


def _verified_gateway_consumption(proof: dict, recipient_key: str) -> set[str]:
    """A durable accepted gateway transaction, never a send or timeout, is proof."""
    from dispatch_replacement import adopt_receipt
    receipt = proof["receipt"]
    jobs = Path(receipt["job_registry"])
    ledger_path = Path(proof["ledger_path"])
    if ledger_path.is_symlink():
        raise ValueError("replacement-consumption-ledger-unsafe")
    ledger = json.loads(ledger_path.read_text())
    entry = ledger["deliveries"][proof["delivery_id"]]
    digest = "sha256:" + hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if (entry.get("state") != "accepted" or entry.get("thread_id") != recipient_key
            or entry.get("receipt_digest") != digest or proof.get("recipient_key") != recipient_key):
        raise ValueError("replacement-consumption-unproven")
    originals = {child["attempt_id"] for child in receipt["children"]}
    for edge in receipt["replacement_lineage"]:
        originals.discard(edge["replacement_attempt_id"])
        originals.add(edge["original_attempt_id"])
    _, consumed = adopt_receipt(jobs, originals, receipt)
    return consumed


def record_replacement_consumption(root: Path, recipient_key: str, receipt: dict, *, ledger_path: Path, delivery_id: str) -> None:
    """Persist a replayable acknowledgement proof without acking a failure row."""
    if not receipt.get("replacement_lineage"):
        return
    from artifact_receipt import _write_once
    proof = {"receipt": receipt, "recipient_key": recipient_key,
             "ledger_path": str(ledger_path.resolve()), "delivery_id": delivery_id}
    consumed = _verified_gateway_consumption(proof, recipient_key)
    directory = Path(root) / "replacement-delivery-consumed"
    directory.mkdir(parents=True, exist_ok=True)
    for aid in consumed:
        path = directory / (hashlib.sha256((recipient_key + "\0" + aid).encode()).hexdigest() + ".json")
        if path.is_symlink():
            raise ValueError("replacement-consumption-unsafe")
        _write_once(directory, path, (json.dumps(proof, sort_keys=True) + "\n").encode())


def _gateway_consumed(root: Path, recipient_key: str, aid: str) -> bool:
    path = Path(root) / "replacement-delivery-consumed" / (hashlib.sha256((recipient_key + "\0" + aid).encode()).hexdigest() + ".json")
    if not path.is_file() or path.is_symlink():
        return False
    return aid in _verified_gateway_consumption(json.loads(path.read_text()), recipient_key)


def _replacement_disposition(root: Path, recipient_key: str, value: dict,
                             jobs: Path | str | None = None) -> str:
    """Original completion is deferred until its proven successor is consumed.

    This never treats a replacement claim as a launch or an async emit as ack.
    Human gates retain their original delivery contract and are never retired.
    """
    receipt = value.get("receipt") or {}
    if (receipt.get("kind") in {"human-gate", "supervision", "notice"}
            or any(isinstance(child, dict) and str(child.get("required_action", "")).startswith("human-gate:")
                   for child in receipt.get("children", []))):
        return ""
    raw_jobs = receipt.get("job_registry")
    receipt_jobs = None
    carrier_jobs = None
    if isinstance(raw_jobs, str) and raw_jobs:
        try:
            receipt_path = Path(raw_jobs).expanduser()
            if receipt_path.is_symlink():
                return "defer" if jobs is not None else ""
            receipt_jobs = receipt_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return "defer" if jobs is not None else ""
    if jobs is not None:
        try:
            carrier_path = Path(jobs).expanduser()
            if carrier_path.is_symlink():
                return "defer"
            carrier_jobs = carrier_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return "defer"
        if (carrier_jobs.is_symlink() or not carrier_jobs.is_file()
                or carrier_jobs.parent != Path(root).resolve()):
            return "defer"
    if receipt_jobs is not None and carrier_jobs is not None and receipt_jobs != carrier_jobs:
        return "defer"
    selected_jobs = receipt_jobs or carrier_jobs
    if selected_jobs is None:
        return ""
    if (selected_jobs.is_symlink() or not selected_jobs.is_file()
            or selected_jobs.parent != Path(root).resolve()):
        return "defer" if carrier_jobs is not None else ""
    try:
        from dispatch_replacement import effective_attempts, recovery_delivery_state, _rows
        selected = set(value.get("attempt_ids") or [])
        rows = _rows(selected_jobs.read_text().splitlines())
        if selected and all(
            aid in rows
            and rows[aid][1].get("replacement_original_attempt_id")
            and rows[aid][1].get("launch_started") != "1"
            for aid in selected
        ):
            # A registered successor that never crossed the launch fence is not
            # a logical-stage result. Keep the source's original failure live.
            return "superseded"
        effective, edges = effective_attempts(selected_jobs, selected)
        if not edges:
            states = [recovery_delivery_state(selected_jobs, aid) for aid in selected]
            if any(state in {"accepted", "unknown"} for state in states):
                return "defer"
            return ""
        if all(_gateway_consumed(root, recipient_key, edge["original_attempt_id"]) for edge in edges):
            return "consumed"
        rows = _rows(selected_jobs.read_text().splitlines())
        # Mixed aggregate records require every effective completion to be
        # consumed; a successful sibling never makes a dead leg disappear.
        consumed = True
        for aid in effective:
            metadata = rows[aid][1]
            if metadata.get("parent_sid") != recipient_key:
                return "defer"
            target_id = metadata.get("delivery_id")
            target = read(root, recipient_key, target_id) if target_id else None
            if target is None or target.get("state") != "acked" or aid not in target.get("attempt_ids", []):
                consumed = False
        return "consumed" if consumed else "defer"
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # An unreadable lineage cannot grant delivery of a stale failure.
        return "defer"


def claim(
    root: Path,
    recipient_key: str,
    delivery_id: str,
    *,
    claim_owner: str,
    lease_seconds: float,
    require_generation_proof: bool = False,
    live_recipient_generation: tuple[str, str] | None = None,
    expected_state: str = "pending",
    expected_identity: dict | None = None,
    jobs: Path | str | None = None,
    retry_context: str | None = None,
    retry_base_delay_seconds: float = 0.05,
    batch_owner: str | None = None,
    _batch_snapshot: dict | None = None,
    _batch_snapshot_resolved: bool = False,
) -> dict:
    """CAS ``expected_state -> claimed`` under one flock (SD-111 §10.2:
    read-check-write inside one lock; ``os.replace`` atomicity alone is not
    CAS). A courier may supply the recipient/generation it just proved through
    the live runtime. That proof belongs to this claim, not to the immutable
    receipt. The courier must first validate the receipt's own binding rules.
    The durable retry deadline suppresses repeated failed transport attempts;
    changed context or a backward wall-clock step makes the obligation eligible.
    """

    if lease_seconds <= 0:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "lease_seconds")
    batch_snapshot = _batch_snapshot
    snapshot_record = None
    if jobs is not None:
        try:
            snapshot_record = read(root, recipient_key, delivery_id)
            if snapshot_record is not None and not _batch_snapshot_resolved:
                from dispatch_notice_state import resolve_delivery_batch
                batch_snapshot = resolve_delivery_batch(
                    Path(jobs), snapshot_record, recipient_key=recipient_key,
                    delivery_root=root)
        except Exception:
            batch_snapshot = None
    retry_context_digest = _retry_context_digest(retry_context)
    retry_base_ns = _delay_ns(retry_base_delay_seconds)
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value["recipient_digest"] != recipient_digest(recipient_key):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "recipient")
        if value["delivery_id"] != delivery_id:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "delivery_id")
        if (expected_identity is not None
                and received_ack_identity(value, recipient_key, delivery_id) != expected_identity):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "claim-identity-changed")
        if live_recipient_generation is not None and (
            not require_generation_proof
            or len(live_recipient_generation) != 2
            or live_recipient_generation[0] != recipient_key
            or not isinstance(live_recipient_generation[1], str)
            or not live_recipient_generation[1]
        ):
            raise PendingDeliveryError("pending-delivery-generation-unproven")
        if (require_generation_proof and live_recipient_generation is None
                and value.get("session_generation_supported") != "1"):
            raise PendingDeliveryError("pending-delivery-generation-unproven")
        if value["state"] != expected_state:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        batch = batch_snapshot
        if batch is not None:
            signature_matches = False
            try:
                info = Path(batch["registry_path"]).stat()
                signature_matches = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == batch["registry_signature"]
            except (OSError, KeyError, TypeError):
                pass
            identity = (value.get("delivery_id"), value.get("receipt_digest"),
                        tuple(value.get("attempt_ids") or ()), value.get("recipient_digest"))
            if (not signature_matches or identity != batch.get("seed_identity")
                    or snapshot_record is None
                    or snapshot_record.get("delivery_id") != delivery_id):
                batch = None
        if batch and len(batch["delivery_ids"]) > 1:
            if batch["held"]:
                raise PendingDeliveryError("pending-delivery-claim-refused", "batch-hold")
            leader_id = batch["delivery_ids"][0]
            if not batch_owner:
                raise PendingDeliveryError("pending-delivery-claim-refused", "batch-group-claim-required")
            if delivery_id != leader_id:
                leader = read(root, recipient_key, leader_id)
                if (leader is None or leader.get("state") != "claimed"
                        or leader.get("claim_owner") != batch_owner
                        or leader.get("claim_deadline_ns", 0) <= time.monotonic_ns()):
                    raise PendingDeliveryError("pending-delivery-claim-refused", "batch-hold")
        elif batch and batch["held"]:
            raise PendingDeliveryError("pending-delivery-claim-refused", "batch-hold")
        retry = _read_courier_retry_unlocked(path, value)
        now_wall_ns = time.time_ns()
        if retry is None:
            retry = _new_courier_retry(retry_context_digest or "", retry_base_ns)
        elif (retry["failure_count"]
              and now_wall_ns < retry["last_failure_at_ns"]):
            # A wall clock moved backward across a process or host restart.
            # Release the stale deadline once; do not make an obligation wait
            # indefinitely for a clock value that may never recur.
            retry = _new_courier_retry(
                retry_context_digest or retry["context_digest"], retry_base_ns)
        elif (retry_context_digest is not None
              and retry_context_digest != retry["context_digest"]):
            # A changed recipient/queue target is fresh delivery evidence.
            retry = _new_courier_retry(retry_context_digest, retry_base_ns)
        elif now_wall_ns < retry["next_attempt_at_ns"]:
            remaining = retry["next_attempt_at_ns"] - now_wall_ns
            raise PendingDeliveryError(
                "pending-delivery-retry-deferred", f"remaining_ns={remaining}"
            )
        retry = dict(retry, base_delay_ns=retry_base_ns)
        disposition = _replacement_disposition(root, recipient_key, value, jobs=jobs)
        if disposition:
            if disposition in {"consumed", "superseded"}:
                reason = ("replacement-consumed" if disposition == "consumed"
                          else "replacement-successor-not-launched")
                retired = dict(value, state="rejected", expiry_reason=reason)
                _write_unlocked(path, retired)
            raise PendingDeliveryError("pending-delivery-claim-refused", "replacement-" + disposition)
        now = time.monotonic_ns()
        updated = dict(value)
        if live_recipient_generation is not None:
            updated["session_generation"] = live_recipient_generation[1]
            updated["session_generation_supported"] = "1"
        updated["state"] = "claimed"
        updated["claimed_at_ns"] = now
        updated["claim_owner"] = claim_owner
        updated["claim_deadline_ns"] = now + int(lease_seconds * 1_000_000_000)
        # SD-113 §13.34.1-(2): the grade is recorded in the same CAS write as
        # the claim itself -- if `_write_unlocked` below cannot persist it,
        # the whole claim raises and no claim is recorded.
        updated["claim_authority"] = (
            "generation-proven" if require_generation_proof else "deliverer-unproven"
        )
        updated[COURIER_RETRY_FIELD] = retry
        updated["attempts"] = value["attempts"] + 1
        updated["last_attempt_at_ns"] = now
        _write_unlocked(path, updated)
        return _public_record(updated)


def claim_ready_batch(root: Path, recipient_key: str, delivery_id: str, *,
                      claim_owner: str, lease_seconds: float, jobs: Path | str,
                      require_generation_proof: bool = False,
                      retry_context: str | None = None) -> list[dict]:
    """Claim a whole ready success batch, releasing every acquired lease on failure."""
    seed = read(root, recipient_key, delivery_id)
    if seed is None:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
    if jobs is None:
        return [claim(root, recipient_key, delivery_id, claim_owner=claim_owner,
                      lease_seconds=lease_seconds,
                      require_generation_proof=require_generation_proof,
                      jobs=None, retry_context=retry_context)]
    from dispatch_notice_state import resolve_delivery_batch
    batch = resolve_delivery_batch(Path(jobs), seed, recipient_key=recipient_key,
                                   delivery_root=root)
    return _claim_ready_batch_snapshot(
        root, recipient_key, delivery_id, claim_owner=claim_owner,
        lease_seconds=lease_seconds, jobs=jobs,
        require_generation_proof=require_generation_proof,
        retry_context=retry_context, seed=seed, batch=batch)


def _claim_ready_batch_snapshot(root: Path, recipient_key: str, delivery_id: str, *,
                                claim_owner: str, lease_seconds: float,
                                jobs: Path | str, require_generation_proof: bool = False,
                                retry_context: str | None = None,
                                seed: dict | None = None,
                                batch: dict | None = None) -> list[dict]:
    """Claim with a resolver snapshot already taken by the carrier or public wrapper."""
    seed = seed if seed is not None else read(root, recipient_key, delivery_id)
    if seed is None:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
    if batch is None or len(batch["delivery_ids"]) <= 1:
        return [claim(root, recipient_key, delivery_id, claim_owner=claim_owner,
                      lease_seconds=lease_seconds,
                      require_generation_proof=require_generation_proof,
                      jobs=jobs, retry_context=retry_context,
                      _batch_snapshot=_member_batch_snapshot(batch, seed),
                      _batch_snapshot_resolved=True)]
    if batch["held"]:
        raise PendingDeliveryError("pending-delivery-claim-refused", "batch-hold")
    ids = batch["delivery_ids"]
    if delivery_id not in ids:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "batch-seed-missing")
    acquired = []
    expected_claims = 0
    try:
        for member_id in ids:
            member = read(root, recipient_key, member_id)
            if member is None:
                raise PendingDeliveryError("pending-delivery-claim-refused", "batch-member-unavailable")
            if member.get("state") in {"acked", "expired", "rejected"}:
                continue
            if member.get("state") not in {"pending", "claimed", "sent-ambiguous"}:
                raise PendingDeliveryError("pending-delivery-claim-refused", "batch-member-unavailable")
            expected_claims += 1
            if member["state"] in {"claimed", "sent-ambiguous"}:
                if member.get("claim_deadline_ns", 0) > time.monotonic_ns():
                    raise PendingDeliveryError("pending-delivery-claim-refused", "batch-owned-elsewhere")
                reclaim(root, recipient_key, member_id, now_ns=time.monotonic_ns())
            claimed = claim(root, recipient_key, member_id, claim_owner=claim_owner,
                            lease_seconds=lease_seconds,
                            require_generation_proof=require_generation_proof,
                            jobs=jobs, retry_context=retry_context,
                            batch_owner=claim_owner,
                            _batch_snapshot=_member_batch_snapshot(batch, member),
                            _batch_snapshot_resolved=True)
            acquired.append(claimed)
        if len(acquired) != expected_claims:
            raise PendingDeliveryError("pending-delivery-claim-refused", "batch-partial")
        return acquired
    except Exception:
        for member in acquired:
            with suppress(PendingDeliveryError, OSError):
                release_claim(root, recipient_key, member["delivery_id"], claim_owner=claim_owner)
        raise


def _member_batch_snapshot(batch: dict | None, record: dict) -> dict | None:
    if batch is None:
        return None
    return dict(batch, seed_identity=(record.get("delivery_id"), record.get("receipt_digest"),
                                      tuple(record.get("attempt_ids") or ()),
                                      record.get("recipient_digest")))


def mark_sent_ambiguous(
    root: Path, recipient_key: str, delivery_id: str, *, claim_owner: str,
    expected_identity: dict | None = None,
) -> dict:
    """``claimed -> sent-ambiguous`` after a token-less emit (no ``acked``
    transition follows on that surface -- the carrier simply stops here)."""

    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if (expected_identity is not None
                and received_ack_identity(value, recipient_key, delivery_id) != expected_identity):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "send-identity-changed")
        if value["state"] != "claimed" or value["claim_owner"] != claim_owner:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        updated = dict(value)
        updated["state"] = "sent-ambiguous"
        _write_unlocked(path, updated)
        return _public_record(updated)


def ack(
    root: Path,
    recipient_key: str,
    delivery_id: str,
    *,
    acked_by: str,
    expected_states: tuple[str, ...] = ("claimed", "sent-ambiguous"),
) -> dict:
    """``{claimed,sent-ambiguous} -> acked``. Only for a recipient surface
    with a real consumption token; token-less surfaces never call this."""

    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value["state"] not in expected_states:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        updated = dict(value)
        updated["state"] = "acked"
        updated["acked_at_ns"] = time.monotonic_ns()
        updated["acked_by"] = acked_by
        updated = _record_received(updated, acked_by)
        updated.pop(COURIER_RETRY_FIELD, None)
        _write_unlocked(path, updated)
        return _public_record(updated)


def record_send(root: Path, recipient_key: str, delivery_id: str, *, source: str,
                event_id: str, expected_identity: dict | None = None) -> dict:
    """Record one confirmed send event, outside receipt identity and CAS state."""
    if (not isinstance(source, str) or not source or len(source) > 128
            or not isinstance(event_id, str) or not event_id or len(event_id) > 128):
        raise PendingDeliveryError("delivery-persistence-refused", "timestamp-source-invalid")
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value.get("state") not in OPEN_STATES | {"acked"}:
            raise PendingDeliveryError("pending-delivery-claim-refused", f"state={value['state']}")
        if expected_identity is not None and received_ack_identity(
                value, recipient_key, delivery_id) != expected_identity:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "send-identity-changed")
        old = value.get(TIMESTAMP_PROVENANCE_FIELD)
        timeline = dict(old) if old is not None else _timestamp_provenance(
            materialized_source="legacy-unknown")
        if timeline.get("last_send_event_id") == event_id:
            return _public_record(value)
        now = _utc_now()
        count = timeline["observed_send_count"]
        if count == 0 and timeline["record_materialized_at_utc"] is not None:
            timeline["first_send_at_utc"] = now
            timeline["first_send_source"] = source
        timeline["last_send_at_utc"] = now
        timeline["last_send_source"] = source
        timeline["last_send_event_id"] = event_id
        timeline["observed_send_count"] = count + 1
        timeline["observed_redelivery_count"] += 1 if count else 0
        updated = dict(value)
        updated[TIMESTAMP_PROVENANCE_FIELD] = timeline
        _write_unlocked(path, updated)
        return _public_record(updated)


def received_ack_identity(record: dict, recipient_key: str, delivery_id: str) -> dict:
    """Freeze the immutable queue identity used by a verified peer receipt.

    Claim ownership, lease state, and attempt counters describe transport
    progress and deliberately do not participate in this identity.
    """
    if (not isinstance(record, dict) or record.get("delivery_id") != delivery_id
            or record.get("recipient_digest") != recipient_digest(recipient_key)):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "received-identity-binding")
    return _received_ack_identity(record)


def _received_ack_identity(record: dict) -> dict:
    """The immutable portion shared by queue fences and exact receipt CAS."""
    canonical_digest = _canonical_receipt_digest(record.get("receipt"))
    if canonical_digest != record.get("receipt_digest"):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "receipt-digest-mismatch")
    fields = (
        "delivery_id", "recipient_digest", "recipient_kind", "session_generation",
        "session_generation_supported", "attempt_ids", "parent_attempt_id", "route_id",
        "route_node", "receipt_digest", "row_revisions", "lineage",
    )
    identity = {key: record.get(key) for key in fields}
    identity["receipt_canonical_digest"] = canonical_digest
    return json.loads(json.dumps(identity, sort_keys=True, separators=(",", ":")))


def ack_received(
    root: Path,
    recipient_key: str,
    delivery_id: str,
    *,
    expected_identity: dict,
    acked_by: str,
) -> dict:
    """Settle an exact recipient receipt, independent of its transport lease.

    The caller must establish the receipt-to-delivery binding. This transition
    rechecks that immutable identity and the latest state inside the queue's
    record lock. Existing terminal records are returned without rewriting any
    bytes; only an open delivery can transition to ``acked``.
    """
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        actual_identity = received_ack_identity(value, recipient_key, delivery_id)
        if actual_identity != expected_identity:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "received-identity-changed")
        if value["state"] in TERMINAL_STATES:
            return _public_record(value)
        if value["state"] not in OPEN_STATES:
            raise PendingDeliveryError("pending-delivery-claim-refused", f"state={value['state']}")
        updated = dict(value)
        updated["state"] = "acked"
        updated["acked_at_ns"] = time.monotonic_ns()
        updated["acked_by"] = acked_by
        updated = _record_received(updated, acked_by)
        updated.pop(COURIER_RETRY_FIELD, None)
        _write_unlocked(path, updated)
        return _public_record(updated)


def reclaim(root: Path, recipient_key: str, delivery_id: str, *, now_ns: int,
            expected_identity: dict | None = None) -> dict:
    """Reclaim an expired lease without deciding whether to send again.

    Claims count ownership transfers, not emissions. A courier owns backoff,
    acceptance checks and no-resend rules; this queue cannot abandon an owed
    delivery because earlier couriers crashed or observed transient failures.
    """

    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if (expected_identity is not None
                and received_ack_identity(value, recipient_key, delivery_id) != expected_identity):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "reclaim-identity-changed")
        if value["state"] not in {"claimed", "sent-ambiguous"}:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        deadline = value.get("claim_deadline_ns")
        if deadline is not None and now_ns < deadline:
            raise PendingDeliveryError("pending-delivery-claim-refused", "lease-not-expired")
        updated = dict(value)
        updated["state"] = "pending"
        updated["claim_owner"] = None
        updated["claim_deadline_ns"] = None
        _write_unlocked(path, updated)
        return _public_record(updated)


def renew_unsubmitted_claim(root: Path, recipient_key: str, delivery_id: str, *,
                           claim_owner: str, expected_identity: dict,
                           target_session: str, lease_seconds: float) -> dict:
    """Renew the same owned lease for an exact peer transfer still pending I/O.

    No ownership transfer, attempt increment, or submission-state change. The
    caller owns the pending transfer lock; this store CAS protects concurrent
    identity/owner/terminal/fence changes under the existing record lock.
    """
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if (value is None or value.get("state") not in {"claimed", "sent-ambiguous"}
                or value.get("claim_owner") != claim_owner
                or received_ack_identity(value, recipient_key, delivery_id) != expected_identity
                or type(value.get("claim_deadline_ns")) is not int):
            raise PendingDeliveryError("pending-delivery-claim-refused", "renew-claim-changed")
        _check_submission_identity(value, submission_fence_identity(value, target_session))
        fence = value.get(SUBMISSION_FENCE_FIELD)
        if fence is not None and fence.get("state") != "not-accepted":
            raise PendingDeliveryError("pending-delivery-claim-refused", "submission-already-fenced")
        if value["claim_deadline_ns"] <= time.monotonic_ns():
            value = dict(value, claim_deadline_ns=time.monotonic_ns() + int(lease_seconds * 1e9))
            _write_unlocked(path, value)
        return _public_record(value)


def release_claim(root: Path, recipient_key: str, delivery_id: str, *, claim_owner: str,
                  failure_reason: str | None = None,
                  retry_base_delay_seconds: float | None = None,
                  expected_identity: dict | None = None,
                  unsubmitted_to: str | None = None) -> dict:
    """Return only this carrier's claim and persist its next retry interval.

    Claim counts remain ownership transfers. The mutable courier retry fields
    delay the same failure without changing receipt identity, delivery state,
    or the carrier's authority to settle it.
    """
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value.get("state") not in {"claimed", "sent-ambiguous"} or value.get("claim_owner") != claim_owner:
            return _public_record(value)
        if (expected_identity is not None
                and received_ack_identity(value, recipient_key, delivery_id) != expected_identity):
            return _public_record(value)
        if expected_identity is not None:
            fence = value.get(SUBMISSION_FENCE_FIELD)
            if fence is not None and fence.get("state") != "not-accepted":
                return _public_record(value)
        if unsubmitted_to is not None:
            _check_submission_identity(value, submission_fence_identity(value, unsubmitted_to))
            fence = value.get(SUBMISSION_FENCE_FIELD)
            deadline = value.get("claim_deadline_ns")
            if (value.get("state") != "claimed"
                    or type(deadline) is not int or deadline <= time.monotonic_ns()
                    or fence is not None and fence.get("state") != "not-accepted"):
                return _public_record(value)
        updated = dict(value)
        retry = (_read_courier_retry_unlocked(path, value)
                 or _new_courier_retry())
        base_delay_ns = (retry["base_delay_ns"] if retry_base_delay_seconds is None
                         else _delay_ns(retry_base_delay_seconds))
        failure = failure_reason or ("sent-ambiguous" if value.get("state") == "sent-ambiguous"
                                     else "claim-released")
        updated[COURIER_RETRY_FIELD] = _advance_courier_retry(
            retry, failure, time.time_ns(), base_delay_ns)
        updated.update(state="pending", claim_owner=None, claim_deadline_ns=None)
        _write_unlocked(path, updated)
        return _public_record(updated)


def reject_claimed(root: Path, recipient_key: str, delivery_id: str, *, claim_owner: str,
                   reason: str, expected_identity: dict | None = None) -> dict:
    """Close a claimed delivery after a typed, permanent carrier refusal."""
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None or value.get("state") != "claimed" or value.get("claim_owner") != claim_owner:
            raise PendingDeliveryError("pending-delivery-claim-refused", "claim-owner-or-state")
        if (expected_identity is not None
                and received_ack_identity(value, recipient_key, delivery_id) != expected_identity):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "reject-identity-changed")
        updated = dict(value)
        updated["state"] = "rejected"
        updated["expiry_reason"] = str(reason)[:1024] or "gateway-rejected"
        _write_unlocked(path, updated)
        return _public_record(updated)


def expire_if_due(
    root: Path,
    recipient_key: str,
    delivery_id: str,
    *,
    actor: str,
    reason: str,
    liveness: str = "known",
) -> dict:
    """Single declared actor (``dispatch-reconcile``), under lock. ``unknown``
    liveness never expires (record stays ``pending``); expired records are
    never deleted -- SD-111 §10.2."""

    if actor != EXPIRY_ACTOR:
        raise PendingDeliveryError("pending-delivery-expiry-actor-invalid", actor)
    if reason not in EXPIRY_REASONS:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "expiry_reason")
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if liveness == "unknown" or value["state"] not in OPEN_STATES:
            return _public_record(value)
        updated = dict(value)
        updated["state"] = "expired"
        updated["expiry_reason"] = reason
        _write_unlocked(path, updated)
        return _public_record(updated)


def expire_recovery(
    root: Path, recipient_key: str, delivery_id: str, *, expected: dict,
    actor: str, reason: str, apply: bool = True,
) -> dict:
    """Atomically expire one exact, unreleasable delivery identity.

    This operator surface intentionally accepts only ``pending`` and
    ``sent-ambiguous``.  A claimed carrier lease is still live, and therefore
    cannot be converted by recovery.  Replaying the same audit against an
    already-expired record is idempotent; any different identity is refused.
    """
    if (
        not isinstance(actor, str) or not isinstance(reason, str)
        or not actor.strip() or not reason.strip()
        or len(actor.encode("utf-8")) > 256
        or len(reason.encode("utf-8")) > 1024
        or any(ord(char) < 32 for char in actor + reason)
    ):
        raise PendingDeliveryError("pending-delivery-recovery-audit-invalid")
    path = record_path(root, recipient_key, delivery_id)
    with _record_reference_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        checks = {
            "delivery_id": delivery_id,
            "recipient_digest": recipient_digest(recipient_key),
            **expected,
        }
        for key, expected_value in checks.items():
            if value.get(key) != expected_value:
                raise PendingDeliveryError(
                    "pending-delivery-identity-conflict", f"recovery-{key}"
                )
        if value.get("state") == "expired":
            if (value.get("expiry_actor"), value.get("expiry_detail")) == (actor, reason):
                return _public_record({**value, "idempotent": True})
            raise PendingDeliveryError("pending-delivery-recovery-refused", "already-expired")
        if value.get("state") not in {"pending", "sent-ambiguous"}:
            raise PendingDeliveryError("pending-delivery-recovery-refused", f"state={value.get('state')}")
        preview = {**value, "eligible": True, "idempotent": False}
        if not apply:
            return _public_record(preview)
        updated = dict(value)
        updated.update({"state": "expired", "expiry_reason": "operator-recovery",
                        "expiry_actor": actor, "expiry_detail": reason,
                        "expired_at_ns": time.time_ns()})
        _write_unlocked(path, updated)
        return _public_record(updated)


def tombstone_path(record_file: Path) -> Path:
    """`<record>.json.pruned` -- proof that a terminal record was pruned, so the
    append-only jobs.log row behind it is never materialized again."""

    return record_file.with_name(record_file.name + TOMBSTONE_SUFFIX)


def _write_prune_tombstone(record_file: Path, value: dict) -> None:
    """Bind a pruned terminal obligation before unlinking its record."""
    tomb = tombstone_path(record_file)
    proof = {"schema": PRUNE_TOMBSTONE_SCHEMA,
             "delivery_id": value["delivery_id"],
             "recipient_digest": value["recipient_digest"],
             "attempt_ids": value["attempt_ids"],
             "parent_attempt_id": value["parent_attempt_id"],
             "terminal_state": value["state"]}
    fd, temp_name = tempfile.mkstemp(prefix=f".{tomb.name}.", dir=str(tomb.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(proof, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, tomb)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def prune_plan(
    root: Path,
    *,
    now: float | None = None,
    retention_seconds: float = TERMINAL_RETENTION_SECONDS,
    lock_retention_seconds: float = ORPHAN_LOCK_RETENTION_SECONDS,
) -> dict:
    """SD-111 §(7) v67 retention plan -- an observer read: no transition, no
    unlink. Lists terminal (``acked``/``expired``) records whose last write is
    older than ``retention_seconds`` and ``.lock`` files with no sibling record
    older than ``lock_retention_seconds``. Every open-state record is kept
    regardless of age; an unreadable record is counted and kept."""

    now = time.time() if now is None else now
    root = Path(root)
    pending_root = root / "pending-delivery"
    reference_root = root / attempt_reference.LOCK_DIRECTORY
    plan = {
        "retention_seconds": retention_seconds,
        "lock_retention_seconds": lock_retention_seconds,
        "records": [],
        "orphan_locks": [],
        "reference_locks": [],
        "kept_open": 0,
        "kept_terminal_recent": 0,
        "kept_locks_recent": 0,
        "kept_reference_locks_recent": 0,
        "unreadable": 0,
        "unreadable_reference_locks": 0,
    }
    if pending_root.is_dir():
        for record_file in sorted(pending_root.glob("*/*.json")):
            try:
                stat_value = record_file.stat()
                value = json.loads(record_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                plan["unreadable"] += 1
                continue
            state = value.get("state") if isinstance(value, dict) else None
            if state not in TERMINAL_STATES:
                plan["kept_open"] += 1
                continue
            age = now - stat_value.st_mtime
            if age < retention_seconds:
                plan["kept_terminal_recent"] += 1
                continue
            plan["records"].append({
                "path": str(record_file),
                "state": state,
                "expiry_reason": value.get("expiry_reason"),
                "delivery_id": value.get("delivery_id"),
                "age_seconds": int(age),
                "mtime": stat_value.st_mtime,
            })
        for lock_file in sorted(pending_root.glob("*/*.json.lock")):
            if lock_file.with_name(lock_file.name[: -len(".lock")]).exists():
                continue
            try:
                age = now - lock_file.stat().st_mtime
            except OSError:
                continue
            if age < lock_retention_seconds:
                plan["kept_locks_recent"] += 1
                continue
            plan["orphan_locks"].append({"path": str(lock_file), "age_seconds": int(age)})
    try:
        reference_dir_stat = reference_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        reference_locks = []
    except OSError:
        reference_locks = []
        plan["unreadable_reference_locks"] += 1
    else:
        if (not stat.S_ISDIR(reference_dir_stat.st_mode)
                or reference_dir_stat.st_uid != os.geteuid()):
            reference_locks = []
            plan["unreadable_reference_locks"] += 1
        else:
            try:
                reference_locks = sorted(reference_root.iterdir())
            except OSError:
                reference_locks = []
                plan["unreadable_reference_locks"] += 1
    for lock_file in reference_locks:
        digest = lock_file.name[:-len(attempt_reference.LOCK_SUFFIX)] \
            if lock_file.name.endswith(attempt_reference.LOCK_SUFFIX) else ""
        try:
            stat_value = lock_file.stat(follow_symlinks=False)
        except OSError:
            plan["unreadable_reference_locks"] += 1
            continue
        if (not _SHA256_RE.fullmatch(digest) or not stat.S_ISREG(stat_value.st_mode)
                or stat_value.st_uid != os.geteuid()):
            plan["unreadable_reference_locks"] += 1
            continue
        age = now - stat_value.st_mtime
        if age < lock_retention_seconds:
            plan["kept_reference_locks_recent"] += 1
            continue
        plan["reference_locks"].append({
            "path": str(lock_file), "age_seconds": int(age), "mtime": stat_value.st_mtime,
        })
    return plan


def prune(
    root: Path,
    *,
    apply: bool,
    now: float | None = None,
    retention_seconds: float = TERMINAL_RETENTION_SECONDS,
    lock_retention_seconds: float = ORPHAN_LOCK_RETENTION_SECONDS,
) -> dict:
    """Retention prune under the single declared actor (SD-111 §(7) v67).

    ``apply=False`` returns the plan only. With ``apply=True`` each planned
    record is re-read under its reference and record locks and unlinked only if it is still
    terminal and unchanged since the plan: tombstone first, then ``.json``,
    then its ``.lock``. A planned orphan lock is unlinked only while this
    process holds it and only if its record is still absent (holders waiting
    on the old inode reopen, see ``_record_lock``). Old attempt-reference
    locks are pruned under their own flock; their waiters also reopen after an
    unlink. Recipient directories are
    never removed (the tombstones live there). Open-state records are never
    touched. Never raises: one bad file is counted in ``skipped`` and the sweep
    continues."""

    plan = prune_plan(
        root, now=now, retention_seconds=retention_seconds,
        lock_retention_seconds=lock_retention_seconds,
    )
    result = {
        "apply": apply,
        "planned_records": len(plan["records"]),
        "planned_locks": len(plan["orphan_locks"]),
        "planned_reference_locks": len(plan["reference_locks"]),
        "pruned_records": 0,
        "pruned_locks": 0,
        "pruned_reference_locks": 0,
        "skipped": 0,
        "plan": plan,
    }
    if not apply:
        return result
    for item in plan["records"]:
        path = Path(item["path"])
        lock_path = path.with_name(path.name[:-5] + _RECORD_LOCK_SUFFIX)
        try:
            with _record_reference_lock(path):
                value = _read_unlocked(path)
                if value is None:
                    # Gone since the plan (a concurrent prune): the lock this
                    # process just created must not become a new orphan
                    # (review round 1, minor 2).
                    try:
                        courier_retry_path(path).unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        courier_timeline_path(path).unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    result["skipped"] += 1
                    continue
                if (
                    value["state"] not in TERMINAL_STATES
                    or path.stat().st_mtime != item["mtime"]
                ):
                    result["skipped"] += 1
                    continue
                _write_prune_tombstone(path, value)
                path.unlink()
                try:
                    courier_retry_path(path).unlink()
                except FileNotFoundError:
                    pass
                try:
                    courier_timeline_path(path).unlink()
                except FileNotFoundError:
                    pass
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
            result["pruned_records"] += 1
        except (PendingDeliveryError, OSError):
            result["skipped"] += 1
    for item in plan["orphan_locks"]:
        lock_path = Path(item["path"])
        record_file = lock_path.with_name(lock_path.name[: -len(".lock")])
        try:
            fd = os.open(str(lock_path), os.O_RDWR)  # no O_CREAT: a vanished lock is not recreated
        except FileNotFoundError:
            continue
        except OSError:
            result["skipped"] += 1
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with dispatch_lock_order.acquired("delivery-record"):
                try:
                    if record_file.exists():
                        result["skipped"] += 1
                        continue
                    try:
                        lock_path.unlink()
                        result["pruned_locks"] += 1
                    except FileNotFoundError:
                        pass
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            result["skipped"] += 1
        finally:
            os.close(fd)
    cutoff_ns = int(((time.time() if now is None else now) - lock_retention_seconds) * 1e9)
    for item in plan["reference_locks"]:
        outcome = attempt_reference.prune_stale_lock(Path(item["path"]), cutoff_ns=cutoff_ns)
        if outcome == "removed":
            result["pruned_reference_locks"] += 1
        elif outcome not in {"missing"}:
            result["skipped"] += 1
    return result


def _default_state_root() -> Path:
    from dispatch_contract import resolve_agent_home, resolve_dispatch_state_root

    return resolve_dispatch_state_root(
        resolve_agent_home(), os.environ.get("AGENT_DISPATCH_JOBS") or None
    )


def _render_prune_table(result: dict) -> str:
    plan = result["plan"]
    lines = [
        f"pending-delivery prune {'APPLY' if result['apply'] else 'DRY-RUN'} "
        f"retention={int(plan['retention_seconds'] // 86400)}d "
        f"lock_retention={int(plan['lock_retention_seconds'] // 3600)}h",
        f"kept: open={plan['kept_open']} terminal_recent={plan['kept_terminal_recent']} "
        f"locks_recent={plan['kept_locks_recent']} "
        f"reference_locks_recent={plan['kept_reference_locks_recent']} "
        f"unreadable={plan['unreadable'] + plan['unreadable_reference_locks']}",
        f"planned: records={result['planned_records']} orphan_locks={result['planned_locks']} "
        f"reference_locks={result['planned_reference_locks']}",
    ]
    if result["apply"]:
        lines.append(
            f"pruned: records={result['pruned_records']} locks={result['pruned_locks']} "
            f"reference_locks={result['pruned_reference_locks']} "
            f"skipped={result['skipped']}"
        )
    lines.append("state          age_d  expiry_reason              delivery_id")
    for item in plan["records"]:
        lines.append(
            f"{item['state']:<14} {item['age_seconds'] / 86400:5.1f}  "
            f"{(item.get('expiry_reason') or '-'):<26} {item.get('delivery_id') or '-'}"
        )
    for item in plan["orphan_locks"]:
        lines.append(f"orphan-lock    {item['age_seconds'] / 86400:5.1f}  -                          {item['path']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import functools

    parser = argparse.ArgumentParser(description="SD-111 pending-delivery retention prune", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True,
                                parser_class=functools.partial(argparse.ArgumentParser, allow_abbrev=False))
    pr = sub.add_parser("prune", help="list (default) or delete terminal records past retention")
    pr.add_argument("--root", type=Path, default=None, help="dispatch state root (default: resolved)")
    pr.add_argument("--apply", action="store_true", help="delete; without it only the plan is printed")
    pr.add_argument("--retention-days", type=float, default=TERMINAL_RETENTION_SECONDS / 86400)
    pr.add_argument("--lock-retention-hours", type=float, default=ORPHAN_LOCK_RETENTION_SECONDS / 3600)
    pr.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root or _default_state_root()
    result = prune(
        root, apply=args.apply,
        retention_seconds=args.retention_days * 86400,
        lock_retention_seconds=args.lock_retention_hours * 3600,
    )
    result["root"] = str(root)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"root={root}")
        print(_render_prune_table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
