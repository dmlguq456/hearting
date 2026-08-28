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

State machine (SD-111 §13.33.1-(4), unchanged):

    None -> pending -> claimed -> {acked | sent-ambiguous | pending (lease reclaim)}
    sent-ambiguous -> {acked | pending (bounded reclaim)}
    pending | claimed | sent-ambiguous -> expired
    acked, expired terminal

ack-before-output is forbidden on token-less surfaces (stdout
``systemMessage``, ``additionalContext``): those surfaces call
:func:`mark_sent_ambiguous`, never :func:`ack`. This module does not enforce
that by itself -- it exposes both transitions because some recipients
(codex-managed-gateway) do have a real consumption token -- the carrier
wiring in P3/P4 is what withholds the ``ack`` call for token-less recipients.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


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
RECLAIM_LIMIT = 8  # SD-106 규율과 같은 유한 상한.

RECIPIENT_KINDS = frozenset({
    "claude-parent-runtime",
    "codex-stop-hook",
    "codex-managed-gateway",
    "opencode-turn",
})

STATES = frozenset({"pending", "claimed", "sent-ambiguous", "acked", "expired"})
OPEN_STATES = frozenset({"pending", "claimed", "sent-ambiguous"})

EXPIRY_REASONS = frozenset({
    "recipient-session-gone",
    "pending-delivery-ttl-exceeded",
    "receipt-row-superseded",
})
EXPIRY_ACTOR = "dispatch-reconcile"

REQUIRED_FIELDS = (
    "schema_version", "delivery_id", "recipient_kind", "recipient_digest",
    "session_generation", "session_generation_supported", "attempt_ids",
    "parent_attempt_id", "route_id", "route_node", "receipt_digest",
    "receipt", "row_revisions", "state", "created_at_ns", "claimed_at_ns",
    "claim_owner", "claim_deadline_ns", "attempts", "last_attempt_at_ns",
    "acked_at_ns", "acked_by", "expiry_reason", "lineage",
)
IMMUTABLE_FIELDS = ("delivery_id", "recipient_digest", "attempt_ids", "receipt_digest")

# Mirrors dispatch_completion_join.{CANONICAL_RECEIPT_KEYS,CANONICAL_CHILD_KEYS,
# canonical_receipt_digest}. Duplicated (not imported): dispatch_completion_join
# imports *this* module for materialize_pending_delivery, so the reverse import
# would be circular. Keep both copies synchronized by hand; §11 forbids
# widening either vocabulary.
CANONICAL_RECEIPT_KEYS = frozenset({
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_classification",
})
CANONICAL_CHILD_KEYS = frozenset({
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
})


def _canonical_receipt_digest(receipt: dict) -> str:
    if not isinstance(receipt, dict):
        raise PendingDeliveryError("pending-delivery-identity-conflict", "receipt-not-dict")
    canonical = {k: v for k, v in receipt.items() if k in CANONICAL_RECEIPT_KEYS}
    children = receipt.get("children")
    if isinstance(children, list):
        canonical["children"] = [
            {k: v for k, v in child.items() if k in CANONICAL_CHILD_KEYS}
            for child in children
            if isinstance(child, dict)
        ]
    encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _validate_record(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != set(REQUIRED_FIELDS):
        raise PendingDeliveryError("delivery-persistence-refused", "record-shape-invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PendingDeliveryError("delivery-persistence-refused", "schema-version-invalid")
    if value.get("recipient_kind") not in RECIPIENT_KINDS:
        raise PendingDeliveryError("delivery-persistence-refused", "recipient-kind-invalid")
    if value.get("state") not in STATES:
        raise PendingDeliveryError("delivery-persistence-refused", "state-invalid")
    if not isinstance(value.get("attempt_ids"), list) or not value["attempt_ids"]:
        raise PendingDeliveryError("delivery-persistence-refused", "attempt-ids-invalid")
    if not isinstance(value.get("attempts"), int):
        raise PendingDeliveryError("delivery-persistence-refused", "attempts-invalid")
    return value


@contextmanager
def _record_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    for directory in (path.parent, path.parent.parent):
        try:
            os.chmod(directory, DIR_MODE)
        except OSError:
            pass
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        try:
            os.chmod(lock_path, FILE_MODE)
        except OSError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    return _validate_record(value)


def _write_unlocked(path: Path, record: dict) -> None:
    _validate_record(record)
    encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
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


def read(root: Path, recipient_key: str, delivery_id: str) -> dict | None:
    """Unlocked observer read -- fleet/P6 visibility, never a transition."""

    return _read_unlocked(record_path(root, recipient_key, delivery_id))


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
        raise PendingDeliveryError("pending-delivery-identity-conflict", "recipient_kind")
    if not attempt_ids or not parent_attempt_id or not route_id or not route_node:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "identity-incomplete")
    computed_digest = _canonical_receipt_digest(receipt)
    if computed_digest != receipt_digest:
        raise PendingDeliveryError(
            "pending-delivery-identity-conflict", "receipt-digest-mismatch"
        )
    body_bytes = len(
        json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    if body_bytes > MAX_RECEIPT_BYTES:
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
        "attempts": 0,
        "last_attempt_at_ns": None,
        "acked_at_ns": None,
        "acked_by": None,
        "expiry_reason": None,
        "lineage": list(lineage or ()),
    }
    with _record_lock(path):
        existing = _read_unlocked(path)
        if existing is not None:
            _verify_identity(existing, candidate)
            return existing
        _write_unlocked(path, candidate)
        return candidate


def _verify_identity(existing: dict, candidate: dict) -> None:
    for key in IMMUTABLE_FIELDS:
        if existing.get(key) != candidate.get(key):
            raise PendingDeliveryError("pending-delivery-identity-conflict", key)


def claim(
    root: Path,
    recipient_key: str,
    delivery_id: str,
    *,
    claim_owner: str,
    lease_seconds: float,
    require_generation_proof: bool = False,
    expected_state: str = "pending",
) -> dict:
    """CAS ``expected_state -> claimed`` under one flock (SD-111 §10.2:
    read-check-write inside one lock; ``os.replace`` atomicity alone is not
    CAS)."""

    if lease_seconds <= 0:
        raise PendingDeliveryError("pending-delivery-identity-conflict", "lease_seconds")
    path = record_path(root, recipient_key, delivery_id)
    with _record_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value["recipient_digest"] != recipient_digest(recipient_key):
            raise PendingDeliveryError("pending-delivery-identity-conflict", "recipient")
        if value["delivery_id"] != delivery_id:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "delivery_id")
        if require_generation_proof and value.get("session_generation_supported") != "1":
            raise PendingDeliveryError("pending-delivery-generation-unproven")
        if value["state"] != expected_state:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        if value["attempts"] >= RECLAIM_LIMIT:
            raise PendingDeliveryError("pending-delivery-reclaim-exhausted")
        now = time.monotonic_ns()
        updated = dict(value)
        updated["state"] = "claimed"
        updated["claimed_at_ns"] = now
        updated["claim_owner"] = claim_owner
        updated["claim_deadline_ns"] = now + int(lease_seconds * 1_000_000_000)
        updated["attempts"] = value["attempts"] + 1
        updated["last_attempt_at_ns"] = now
        _write_unlocked(path, updated)
        return updated


def mark_sent_ambiguous(
    root: Path, recipient_key: str, delivery_id: str, *, claim_owner: str
) -> dict:
    """``claimed -> sent-ambiguous`` after a token-less emit (no ``acked``
    transition follows on that surface -- the carrier simply stops here)."""

    path = record_path(root, recipient_key, delivery_id)
    with _record_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value["state"] != "claimed" or value["claim_owner"] != claim_owner:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        updated = dict(value)
        updated["state"] = "sent-ambiguous"
        _write_unlocked(path, updated)
        return updated


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
    with _record_lock(path):
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
        _write_unlocked(path, updated)
        return updated


def reclaim(root: Path, recipient_key: str, delivery_id: str, *, now_ns: int) -> dict:
    """Bounded lease reclaim: ``{claimed,sent-ambiguous} -> pending`` once the
    claim deadline has passed. Exhausting ``RECLAIM_LIMIT`` attempts is a
    typed terminal refusal, not a further state transition -- the record
    stays exactly where it was so a human/operator sees the stuck claim."""

    path = record_path(root, recipient_key, delivery_id)
    with _record_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if value["state"] not in {"claimed", "sent-ambiguous"}:
            raise PendingDeliveryError(
                "pending-delivery-claim-refused", f"state={value['state']}"
            )
        deadline = value.get("claim_deadline_ns")
        if deadline is not None and now_ns < deadline:
            raise PendingDeliveryError("pending-delivery-claim-refused", "lease-not-expired")
        # The exhaustion check belongs to the next `claim()`, not here: a
        # reclaim only restores eligibility to retry, it is not itself a
        # retry attempt.
        updated = dict(value)
        updated["state"] = "pending"
        updated["claim_owner"] = None
        updated["claim_deadline_ns"] = None
        _write_unlocked(path, updated)
        return updated


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
    with _record_lock(path):
        value = _read_unlocked(path)
        if value is None:
            raise PendingDeliveryError("pending-delivery-identity-conflict", "record-missing")
        if liveness == "unknown" or value["state"] not in OPEN_STATES:
            return value
        updated = dict(value)
        updated["state"] = "expired"
        updated["expiry_reason"] = reason
        _write_unlocked(path, updated)
        return updated
