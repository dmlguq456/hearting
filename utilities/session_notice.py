#!/usr/bin/env python3
"""Retained ordinary notices delivered by the existing native peer courier."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import dispatch_pending_delivery as pending_delivery

KIND = "notice"
MAX_TEXT = 600
KEYS = frozenset({"schema_version", "kind", "subject", "text", "required_action", "key"})
LINEAGE_PREFIX = "session-notice-lineage-v1:"


def _registry(jobs=None) -> Path:
    if jobs:
        return Path(jobs)
    from dispatch_contract import stable_state_root
    return Path(os.environ.get("AGENT_DISPATCH_JOBS") or stable_state_root(os.environ) / "jobs.log")


def receipt_digest(receipt: dict) -> str:
    return pending_delivery._canonical_receipt_digest(receipt)


def validate(receipt: dict) -> dict:
    if (not isinstance(receipt, dict) or set(receipt) != KEYS or receipt.get("kind") != KIND
            or receipt.get("schema_version") != 1
            or any(not isinstance(receipt[key], str) or not receipt[key]
                   for key in ("subject", "text", "required_action", "key"))
            or len(receipt["text"]) > MAX_TEXT):
        raise ValueError("notice-shape-invalid")
    return dict(receipt)


def _lineage(value, harness, session_id):
    if not isinstance(value, dict):
        value = {"kind": "provenance", "harness": harness, "session_id": session_id}
    kind = value.get("kind")
    required = ({"kind", "harness", "session_id", "attempt_id"}
                if kind in {"attempt-parent", "owner-session"}
                else {"kind", "harness", "session_id"})
    if (set(value) != required or value.get("harness") != harness
            or value.get("session_id") != session_id
            or kind not in {"provenance", "attempt-parent", "owner-session"}
            or (kind in {"attempt-parent", "owner-session"}
                and (not isinstance(value.get("attempt_id"), str) or not value["attempt_id"]))):
        raise ValueError("notice-lineage-invalid")
    return dict(value)


def _retain(jobs: Path, carrier: str, harness: str, session_id: str,
            record: dict, lineage: dict, *, ensure_runner=True):
    import peer_obligations
    storage = record.get("_storage_key") or session_id
    identity = {"jobs": str(jobs), "storage_recipient": storage,
                "recipient_harness": harness, "recipient_session_id": session_id,
                "lineage": lineage}
    duty = peer_obligations.ObligationStore(jobs.parent).create(
        peer_obligations.stable_duty_id("run-notice", identity, record["delivery_id"]),
        "run-notice", identity,
        {"carrier": carrier, "delivery_id": record["delivery_id"],
         "receipt_digest": record["receipt_digest"],
         "route_id": record.get("route_id") or "route-free", "route_node": "notice"})
    if ensure_runner and duty.get("state") not in {"complete", "cancelled"}:
        if not peer_obligations.ensure_normal_runner(jobs.parent):
            peer_obligations.request_dispatch_reconciliation(jobs.parent)
    return duty


def _stored_lineage(jobs: Path, lineage: dict) -> str:
    return LINEAGE_PREFIX + json.dumps({"jobs": str(jobs), "binding": lineage},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def notify(harness: str, session_id: str, *, key: str, subject: str, text: str,
           required_action: str = "read-notice", route_id: str = "route-free", jobs=None,
           lineage=None) -> dict | None:
    """Store one immutable notice and wake the existing normal runner."""
    from harness_capabilities import parent_completion
    carrier = parent_completion(harness).get("carrier")
    if not carrier or not session_id or carrier not in pending_delivery.RECIPIENT_KINDS:
        return None
    raw_jobs = _registry(jobs).expanduser()
    if raw_jobs.is_symlink():
        return None
    jobs_path = raw_jobs.resolve(strict=False)
    binding = _lineage(lineage, harness, session_id)
    receipt = validate({"schema_version": 1, "kind": KIND, "subject": subject,
                        "text": " ".join(text.split())[:MAX_TEXT],
                        "required_action": required_action, "key": key})
    digest = hashlib.sha256(f"{KIND}\0{key}".encode()).hexdigest()
    delivery_id = "delivery-" + digest
    root = jobs_path.parent
    record = pending_delivery.read(root, session_id, delivery_id)
    if record is None:
        record = pending_delivery.create(
            root, recipient_kind=carrier, recipient_key=session_id,
            delivery_id=delivery_id, session_generation="", session_generation_supported="0",
            attempt_ids=[], parent_attempt_id="", route_id=route_id or "route-free",
            route_node=KIND, receipt=receipt, receipt_digest=receipt_digest(receipt),
            row_revisions={},
            lineage=[_stored_lineage(jobs_path, binding)])
    elif (record.get("receipt_digest") != receipt_digest(receipt)
          or record.get("recipient_kind") != carrier):
        raise ValueError("notice-key-identity-conflict")
    _retain(jobs_path, carrier, harness, session_id, record, binding)
    return record


def retain_pending(jobs, record) -> bool:
    """Rebuild a retained duty from the exact pending record during recovery."""
    jobs_path = Path(jobs).expanduser().resolve(strict=False)
    try:
        receipt = validate(record.get("receipt"))
        if record.get("receipt_digest") != receipt_digest(receipt):
            return False
        carrier = record.get("recipient_kind")
        from harness_capabilities import HARNESSES, parent_completion
        harness = next((name for name in HARNESSES
                        if parent_completion(name).get("carrier") == carrier), None)
        storage = record.get("_storage_key")
        if not harness or not storage:
            return False
        lineage_value = next((value[len(LINEAGE_PREFIX):] for value in record.get("lineage", [])
                              if isinstance(value, str) and value.startswith(LINEAGE_PREFIX)), None)
        stored = json.loads(lineage_value) if lineage_value else None
        if isinstance(stored, dict) and set(stored) == {"jobs", "binding"}:
            recorded_jobs = Path(stored["jobs"]).expanduser().resolve(strict=False)
            if recorded_jobs.parent != jobs_path.parent:
                return False
            jobs_path = recorded_jobs
            lineage = stored["binding"]
        else:
            lineage = (stored if stored else
                       {"kind": "provenance", "harness": harness, "session_id": storage})
        if not isinstance(lineage, dict):
            return False
        lineage = _lineage(lineage, harness, lineage.get("session_id", storage))
        _retain(jobs_path, carrier, harness, lineage["session_id"], record, lineage,
                ensure_runner=False)
        return True
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def render_text(receipt: dict) -> str:
    try:
        receipt = validate(receipt)
    except ValueError:
        return "notice=unreadable"
    return f"{receipt['subject']}: {receipt['text']} (required_action={receipt['required_action']})"


def notice_is_current(record: dict) -> bool:
    """A notice is retired only by its delivery, never by registry state."""
    try:
        validate(record.get("receipt"))
    except ValueError:
        return False
    return True


def context(receipt: dict, delivery_id: str) -> dict:
    return {"kind": KIND, "delivery_id": delivery_id, "text": render_text(receipt)}


def gateway_delivery_id(receipt: dict) -> str:
    return "delivery-" + hashlib.sha256(f"{KIND}\0{validate(receipt)['key']}".encode()).hexdigest()


def validate_pending_record(record: dict, **_kwargs) -> dict:
    receipt = validate(record.get("receipt"))
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise ValueError("notice-digest-mismatch")
    return receipt
