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
RECLAIM_LIMIT = 8  # SD-106 규율과 같은 유한 상한.

RECIPIENT_KINDS = frozenset({
    "claude-parent-runtime",
    "codex-stop-hook",
    "codex-managed-gateway",
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
# zero-byte tombstone (`<record>.json.pruned`, never deleted) so the
# materialize backstop cannot resurrect it from the append-only jobs.log row
# (review round 1, B1).
TERMINAL_STATES = frozenset({"acked", "expired", "rejected"})
TERMINAL_RETENTION_SECONDS = 7 * 86400
ORPHAN_LOCK_RETENTION_SECONDS = 24 * 3600
TOMBSTONE_SUFFIX = ".pruned"
_LOCK_REOPEN_LIMIT = 16

REQUIRED_FIELDS = (
    "schema_version", "delivery_id", "recipient_kind", "recipient_digest",
    "session_generation", "session_generation_supported", "attempt_ids",
    "parent_attempt_id", "route_id", "route_node", "receipt_digest",
    "receipt", "row_revisions", "state", "created_at_ns", "claimed_at_ns",
    "claim_owner", "claim_deadline_ns", "claim_authority", "attempts",
    "last_attempt_at_ns", "acked_at_ns", "acked_by", "expiry_reason", "lineage",
)
RECOVERY_AUDIT_FIELDS = frozenset({"expiry_actor", "expiry_detail", "expired_at_ns"})
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
    if not set(REQUIRED_FIELDS) <= keys or keys - set(REQUIRED_FIELDS) - RECOVERY_AUDIT_FIELDS:
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
        raise PendingDeliveryError("delivery-persistence-refused", "recipient-kind-unknown")
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
        "claim_authority": "",
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
        # SD-113 §13.34.1-(2): the grade is recorded in the same CAS write as
        # the claim itself -- if `_write_unlocked` below cannot persist it,
        # the whole claim raises and no claim is recorded.
        updated["claim_authority"] = (
            "generation-proven" if require_generation_proof else "deliverer-unproven"
        )
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


def reject_claimed(root: Path, recipient_key: str, delivery_id: str, *, claim_owner: str,
                   reason: str) -> dict:
    """Close a claimed delivery after a typed, permanent carrier refusal."""
    path = record_path(root, recipient_key, delivery_id)
    with _record_lock(path):
        value = _read_unlocked(path)
        if value is None or value.get("state") != "claimed" or value.get("claim_owner") != claim_owner:
            raise PendingDeliveryError("pending-delivery-claim-refused", "claim-owner-or-state")
        updated = dict(value)
        updated["state"] = "rejected"
        updated["expiry_reason"] = str(reason)[:1024] or "gateway-rejected"
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
    with _record_lock(path):
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
                return {**value, "idempotent": True}
            raise PendingDeliveryError("pending-delivery-recovery-refused", "already-expired")
        if value.get("state") not in {"pending", "sent-ambiguous"}:
            raise PendingDeliveryError("pending-delivery-recovery-refused", f"state={value.get('state')}")
        preview = {**value, "eligible": True, "idempotent": False}
        if not apply:
            return preview
        updated = dict(value)
        updated.update({"state": "expired", "expiry_reason": "operator-recovery",
                        "expiry_actor": actor, "expiry_detail": reason,
                        "expired_at_ns": time.time_ns()})
        _write_unlocked(path, updated)
        return updated


def tombstone_path(record_file: Path) -> Path:
    """`<record>.json.pruned` -- proof that a terminal record was pruned, so the
    append-only jobs.log row behind it is never materialized again."""

    return record_file.with_name(record_file.name + TOMBSTONE_SUFFIX)


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
    pending_root = root / "pending-delivery"
    plan = {
        "retention_seconds": retention_seconds,
        "lock_retention_seconds": lock_retention_seconds,
        "records": [],
        "orphan_locks": [],
        "kept_open": 0,
        "kept_terminal_recent": 0,
        "kept_locks_recent": 0,
        "unreadable": 0,
    }
    if not pending_root.is_dir():
        return plan
    for record_file in sorted(pending_root.glob("*/*.json")):
        try:
            stat = record_file.stat()
            value = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan["unreadable"] += 1
            continue
        state = value.get("state") if isinstance(value, dict) else None
        if state not in TERMINAL_STATES:
            plan["kept_open"] += 1
            continue
        age = now - stat.st_mtime
        if age < retention_seconds:
            plan["kept_terminal_recent"] += 1
            continue
        plan["records"].append({
            "path": str(record_file),
            "state": state,
            "expiry_reason": value.get("expiry_reason"),
            "delivery_id": value.get("delivery_id"),
            "age_seconds": int(age),
            "mtime": stat.st_mtime,
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
    record is re-read under its own lock and unlinked only if it is still
    terminal and unchanged since the plan: tombstone first, then ``.json``,
    then its ``.lock``. A planned orphan lock is unlinked only while this
    process holds it and only if its record is still absent (holders waiting
    on the old inode reopen, see ``_record_lock``). Recipient directories are
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
        "pruned_records": 0,
        "pruned_locks": 0,
        "skipped": 0,
        "plan": plan,
    }
    if not apply:
        return result
    for item in plan["records"]:
        path = Path(item["path"])
        lock_path = path.with_name(path.name + ".lock")
        try:
            with _record_lock(path):
                value = _read_unlocked(path)
                if value is None:
                    # Gone since the plan (a concurrent prune): the lock this
                    # process just created must not become a new orphan
                    # (review round 1, minor 2).
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
                tombstone_path(path).touch(mode=FILE_MODE, exist_ok=True)
                path.unlink()
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
        f"locks_recent={plan['kept_locks_recent']} unreadable={plan['unreadable']}",
        f"planned: records={result['planned_records']} orphan_locks={result['planned_locks']}",
    ]
    if result["apply"]:
        lines.append(
            f"pruned: records={result['pruned_records']} locks={result['pruned_locks']} "
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

    parser = argparse.ArgumentParser(description="SD-111 pending-delivery retention prune")
    sub = parser.add_subparsers(dest="command", required=True)
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
