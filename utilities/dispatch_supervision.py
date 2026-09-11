"""Shared wait ownership and durable, non-terminal parent handback.

The canonical registry owns outcomes. This controller schedules bounded recovery
and keeps waiting; its timeout never authorizes killing a worker or retrying it.
The existing pending-delivery queue owns a notice until a carrier accepts it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import time
from typing import Callable

from dispatch_attempt_policy import decide_attempt
from dispatch_receipt_identity import receipt_digest
import dispatch_pending_delivery as pending_delivery

KIND = "supervision"
REASONS = frozenset({"process-unverifiable", "join-deadline", "supervisor-exited", "join-observer-failed"})


class SupervisionError(ValueError):
    pass


def _rows(jobs: Path) -> dict:
    from dispatch_contract import parse_registry_metadata
    rows = {}
    for line in jobs.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = parse_registry_metadata(fields[5])
        aid = meta.get("attempt_id")
        if aid:
            if aid in rows:
                raise SupervisionError("supervision-attempt-ambiguous")
            rows[aid] = (fields[1], meta)
    return rows


def _root(rows: dict, attempt: str) -> str:
    seen = set()
    while attempt in rows and attempt not in seen:
        seen.add(attempt)
        meta = rows[attempt][1]
        parent = meta.get("parent_attempt_id")
        if parent in (None, "", "-"):
            return attempt
        attempt = parent
    raise SupervisionError("supervision-lineage-unresolved")


def _pending(rows: dict, attempts: list[str]) -> bool:
    from dispatch_contract import observed_attempt_liveness
    from codex_dispatch_terminal import terminal_envelope_observed
    for aid in attempts:
        if aid not in rows:
            raise SupervisionError("supervision-attempt-missing")
        status, meta = rows[aid]
        proof = observed_attempt_liveness(status, meta,
            terminal_envelope=terminal_envelope_observed(meta.get("log_file")),
            terminal_receipt_gate=True)
        decision = decide_attempt(status, meta, process_state=proof.process_state,
                                  process_reason=proof.process_reason)
        if decision.action in {"wait", "recover", "reconcile"}:
            return True
    return False


def materialize(jobs: Path, attempts: set[str], *, reason: str) -> list[dict]:
    """Idempotent handback per exact root/batch/reason, with no row mutation."""
    if reason not in REASONS:
        raise SupervisionError("supervision-reason-invalid")
    jobs = jobs.resolve()
    rows = _rows(jobs)
    groups: dict[str, list[str]] = {}
    for aid in sorted(attempts):
        groups.setdefault(_root(rows, aid), []).append(aid)
    results = []
    for owner, monitored in groups.items():
        meta = rows[owner][1]
        recipient = meta.get("parent_sid", "")
        kind = meta.get("parent_completion_delivery", "")
        if not recipient or kind not in pending_delivery.RECIPIENT_KINDS:
            raise SupervisionError("supervision-parent-carrier-unbound")
        receipt = {
            "schema_version": 1, "kind": KIND, "state": "attention",
            "owner_attempt_id": owner, "monitored_attempt_ids": monitored,
            "recipient_thread_id": recipient,
            "sealed_batch_id": meta.get("managed_sealed_batch_id", ""),
            "job_registry": str(jobs), "reason": reason,
            "responsible": "supervision-controller", "required_action": "inspect-recovery",
        }
        key = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        delivery = "delivery-" + key
        receipt["pending_delivery_id"] = delivery
        record = pending_delivery.create(jobs.parent, delivery_id=delivery,
            recipient_kind=kind, recipient_key=recipient,
            # The work obligation outlives gateway connections. The courier
            # proves the current recipient generation when claiming it.
            session_generation="", session_generation_supported="0",
            attempt_ids=[owner], parent_attempt_id=owner,
            route_id=meta.get("owner_route_id") or meta.get("route_id") or "route-free",
            route_node=meta.get("route_node") or "supervision",
            receipt=receipt, receipt_digest=receipt_digest(receipt),
            row_revisions={owner: "supervision:" + key})
        results.append(record)
    return results


def validate(receipt: dict, *, expected_thread: str | None = None,
             expected_epoch: int | None = None) -> dict:
    keys = {"schema_version", "kind", "state", "owner_attempt_id", "monitored_attempt_ids",
            "recipient_thread_id", "sealed_batch_id", "job_registry",
            "reason", "responsible", "required_action", "pending_delivery_id"}
    if (not isinstance(receipt, dict) or set(receipt) != keys
            or receipt.get("kind") != KIND or receipt.get("schema_version") != 1
            or receipt.get("state") != "attention" or receipt.get("reason") not in REASONS
            or receipt.get("required_action") != "inspect-recovery"
            or receipt.get("responsible") != "supervision-controller"):
        raise SupervisionError("supervision-shape-invalid")
    text_keys = keys - {"schema_version", "monitored_attempt_ids"}
    if (any(not isinstance(receipt[key], str) for key in text_keys)
            or not Path(receipt["job_registry"]).is_absolute()
            or any(ord(char) < 32 for key in text_keys for char in receipt[key])):
        raise SupervisionError("supervision-field-invalid")
    attempts = receipt["monitored_attempt_ids"]
    if (not isinstance(attempts, list) or not attempts
            or any(not isinstance(a, str) or not re.fullmatch(r"att-[A-Za-z0-9._-]+", a)
                   for a in [*attempts, receipt["owner_attempt_id"]])
            or attempts != sorted(set(attempts))):
        raise SupervisionError("supervision-attempts-invalid")
    if expected_thread is not None and receipt["recipient_thread_id"] != expected_thread:
        raise SupervisionError("supervision-thread-mismatch")
    # Unlike an approval gate, recovery is attempt-scoped, not tied to a
    # particular connection. The common transport fences its live epoch.
    return dict(receipt)


def validate_digest(receipt: dict, supplied: str) -> None:
    if receipt_digest(receipt) != supplied:
        raise SupervisionError("supervision-digest-mismatch")


def validate_receipt(receipt: dict, *, jobs: Path, expected_thread_id: str,
                     expected_epoch: int, expected_attempts: set[str],
                     expected_sealed_batch_id: str, validate_live: bool = True) -> dict:
    receipt = validate(receipt, expected_thread=expected_thread_id, expected_epoch=expected_epoch)
    if (not jobs.is_absolute() or jobs.is_symlink() or str(jobs) != receipt["job_registry"]
            or receipt["owner_attempt_id"] not in expected_attempts
            or receipt["sealed_batch_id"] != expected_sealed_batch_id):
        raise SupervisionError("supervision-binding-mismatch")
    rows = _rows(jobs)
    owner = receipt["owner_attempt_id"]
    if owner not in rows:
        raise SupervisionError("supervision-owner-missing")
    meta = rows[owner][1]
    if (meta.get("parent_sid") != expected_thread_id
            or meta.get("managed_sealed_batch_id", "") != expected_sealed_batch_id
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    # A recovered batch must not receive an obsolete intervention request.
    if validate_live and not _pending(rows, receipt["monitored_attempt_ids"]):
        raise SupervisionError("supervision-resolved")
    record = pending_delivery.read(jobs.parent, expected_thread_id, receipt["pending_delivery_id"])
    if record is None or record.get("receipt") != receipt:
        raise SupervisionError("supervision-authority-missing")
    return receipt


def validate_pending_record(record: dict, **kwargs) -> dict:
    receipt = validate_receipt(record.get("receipt"), **kwargs)
    if (record.get("delivery_id") != receipt["pending_delivery_id"]
            or record.get("attempt_ids") != [receipt["owner_attempt_id"]]
            or record.get("parent_attempt_id") != receipt["owner_attempt_id"]
            or record.get("recipient_digest") != pending_delivery.recipient_digest(receipt["recipient_thread_id"])
            or record.get("receipt_digest") != receipt_digest(receipt)):
        raise SupervisionError("supervision-pending-mismatch")
    return dict(record)


def gateway_delivery_id(receipt: dict) -> str:
    return "sn-dlv-" + receipt_digest(validate(receipt)).removeprefix("sha256:")


def notice_is_current(record: dict) -> bool:
    """Shared pre-delivery check for native prompt and async carriers."""
    receipt = validate(record.get("receipt"))
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise SupervisionError("supervision-digest-mismatch")
    rows = _rows(Path(receipt["job_registry"]))
    owner = receipt["owner_attempt_id"]
    if (owner not in rows or rows[owner][1].get("parent_sid") != receipt["recipient_thread_id"]
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    return _pending(rows, receipt["monitored_attempt_ids"])


def render_text(receipt: dict) -> str:
    receipt = validate(receipt)
    utility = Path(__file__).resolve().with_name("dispatch-registry.py")
    commands = [f"python3 {shlex.quote(str(utility))} reconcile --jobs "
                f"{shlex.quote(receipt['job_registry'])} --attempt {shlex.quote(aid)}"
                for aid in receipt["monitored_attempt_ids"]]
    return ("Hearting supervision needs attention. This is not workflow completion. "
            f"reason={receipt['reason']} owner={receipt['owner_attempt_id']}. "
            "The controller retains waiting/recovery responsibility. Explain the blockage to the user "
            "and inspect these exact attempts. Do not infer death, erase rows, or retry from this notice. "
            "If evidence cannot settle them, ask the user whether to keep waiting or cancel the exact work; "
            "an accepted notification does not close the work. Read-only diagnosis: " + " ; ".join(commands))


def context(receipt: dict, delivery_id: str) -> dict:
    return {"threadId": receipt["recipient_thread_id"], "input": [],
            "clientUserMessageId": delivery_id,
            "additionalContext": {"hearting-supervision": {"kind": "application", "value": render_text(receipt)}}}


def wait_for_batch(*, join: Callable[[set[str]], dict], attempts: set[str],
                   jobs: Path, parent_attempt_id: str = "", emit: Callable[[dict], None] | None = None,
                   on_timeout: Callable[[set[str]], None] | None = None) -> dict:
    """One shared wait loop. Join deadlines are checkpoints, never death votes.

    Execution boundaries retain their finite budgets. The parent receives one
    durable notice for this exact batch while this controller retains the wait.
    A failed queue write is retried at the next bounded checkpoint, not discarded.
    """
    ordinal = 0
    while True:
        observer_error = ""
        try:
            receipt = join(set(attempts))
            if receipt.get("state") != "timeout":
                return receipt
        except Exception as exc:
            # Observation failure is not worker failure. Keep the exact wait
            # and transfer the diagnostic, without fabricating completion.
            observer_error = str(exc)
        ordinal += 1
        notice_error = ""
        try:
            materialize(jobs, attempts,
                        reason="join-observer-failed" if observer_error else "join-deadline")
        except (OSError, ValueError, pending_delivery.PendingDeliveryError) as exc:
            notice_error = str(exc)
        if on_timeout:
            try:
                on_timeout(set(attempts))
            except Exception as exc:
                observer_error = str(exc)
        if emit:
            emit({"type": "dispatch.supervisor.reparked", "parent_attempt_id": parent_attempt_id,
                  "attempt_count": len(attempts), "repark_ordinal": ordinal,
                  "responsible": "supervision-controller", "notice_error": notice_error,
                  "observer_error": observer_error})
        # A failed observer receives bounded backoff; execution budgets remain
        # the execution boundary's responsibility, never this observer's vote.
        time.sleep(30.0 if observer_error else 0.05)
