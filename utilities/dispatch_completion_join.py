#!/usr/bin/env python3
"""Runtime-owned exact-batch join for registered headless children.

The joiner never returns child output.  It snapshots attempts bound to one
``parent_attempt_id``, waits until every attempt is either closed or requires
typed harvest, and emits one bounded JSON receipt for the session supervisor.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    ProcessQuiescence,
    attempt_process_quiescence,
    close_attempt_row,
    marker_bound_delivery_transaction,
    marker_bound_process_identity,
    observed_attempt_liveness,
    parse_registry_metadata,
    process_identity_disposition,
    reconcile_attempt_terminal,
)
import dispatch_pending_delivery as pending_delivery  # noqa: E402
from codex_dispatch_terminal import (  # noqa: E402
    inspect_terminal_attempt,
    terminal_envelope_observed,
)
OPEN_STATES = frozenset({"open", "running"})
SCHEMA_VERSION = 2
# SD-110 13.32.1-(3)B: a second, additive receipt schema for consumers that
# have explicitly negotiated it. SCHEMA_VERSION above -- what this module's
# own join subprocess (`main`/`_join_snapshot`) emits -- never changes.
STAGE_ADVANCE_SCHEMA_VERSION = 3
STAGE_ADVANCE_RECEIPT_KEY = "stage_advance"
STAGE_ADVANCE_RECEIPT_FIELDS = (
    "schema_version",
    "stage_advance_id",
    "route_id",
    "route_hash",
    "predecessor_node",
    "predecessor_terminal_attempt_id",
    "successor_node",
    "successor_attempt_id",
    "claim_key",
    "brief_template_digest",
    "outcome",
    "reason",
    "registered",
    "started",
    "child_spawned",
)
STATE_SCHEMA_VERSION = 2
PARENT_SESSION_STATE_SCHEMA_VERSION = 1
STATE_PHASES = frozenset({"parked", "deliverable", "running-turn", "recovery", "terminal"})
MAX_STATE_BYTES = 16384
MAX_BATCH_ATTEMPTS = 4
SESSION_PARENT_DELIVERY = "codex-stop-hook"
MANAGED_SESSION_PARENT_DELIVERY = "codex-managed-gateway"
SESSION_PARENT_DELIVERIES = frozenset(
    {SESSION_PARENT_DELIVERY, MANAGED_SESSION_PARENT_DELIVERY}
)
SUCCESS_NOTES = frozenset({"completed-marker", "completed-supervisor"})
RUNTIME_WAIT_SENTINEL = "runtime_wait: registered-children"
DELIVERY_TIMING_SCHEMA_VERSION = 1
DELIVERY_TIMING_POINTS = (
    "last_child_terminal_ns",
    "join_completed_ns",
    "same_thread_resume_ns",
    "exact_harvest_ns",
    "next_stage_start_ns",
    "final_report_marker_ns",
    "owner_terminal_envelope_ns",
)
# SD-111 D-2/C-2: mirrors codex-managed-gateway.py's ALLOWED_RECEIPT_KEYS /
# ALLOWED_CHILD_KEYS minus "delivery_timing". Duplicated (not imported) because
# codex-managed-gateway.py imports this module already -- importing back would
# be circular. Keep both lists synchronized by hand; §11 forbids widening
# either vocabulary.
CANONICAL_RECEIPT_KEYS = frozenset({
    "schema_version", "state", "parent_attempt_id", "job_registry", "children",
    "delivery_classification",
})
CANONICAL_CHILD_KEYS = frozenset({
    "attempt_id", "status", "readiness", "reason", "required_action", "harness",
    "delivery_classification",
})
MAX_DELIVERY_RECEIPT_BYTES = 2048


class JoinContractError(RuntimeError):
    """A registry or liveness boundary could not be proved."""


def canonical_delivery_receipt(receipt: dict[str, object]) -> dict[str, object]:
    """Select only the digest-material keys of one v2 receipt (SD-111 D-2).

    ``delivery_timing`` is process-local (``time.monotonic_ns()``) and is
    excluded on purpose -- it is observability, not completion identity. This
    projection never becomes the record's stored ``receipt`` field (that is
    the sealed original, round 2 C-2); it exists only to compute a stable
    digest/``delivery_id`` across carriers.
    """

    if not isinstance(receipt, dict):
        raise JoinContractError("delivery-receipt-invalid")
    canonical: dict[str, object] = {
        key: value for key, value in receipt.items() if key in CANONICAL_RECEIPT_KEYS
    }
    raw_children = receipt.get("children")
    if isinstance(raw_children, list):
        canonical["children"] = [
            {key: value for key, value in child.items() if key in CANONICAL_CHILD_KEYS}
            for child in raw_children
            if isinstance(child, dict)
        ]
    return canonical


def canonical_receipt_digest(receipt: dict[str, object]) -> str:
    """Return the sha256 hex digest of the timing-excluded canonical receipt."""

    canonical = canonical_delivery_receipt(receipt)
    encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_delivery_receipt(receipt: dict[str, object]) -> str:
    """Encode the exact v2 receipt body as unpadded standard base64 (C-2).

    The materializer restores these bytes verbatim -- it never regenerates
    the receipt -- so record.receipt is byte-identical to what the terminal
    writer saw, including ``delivery_timing``.
    """

    if not isinstance(receipt, dict):
        raise JoinContractError("delivery-receipt-invalid")
    encoded = json.dumps(receipt, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_DELIVERY_RECEIPT_BYTES:
        raise JoinContractError("pending-delivery-oversized")
    return base64.standard_b64encode(encoded).decode("ascii").rstrip("=")


def unseal_delivery_receipt(encoded: str) -> dict[str, object]:
    """Decode a sealed receipt body back into the exact original dict."""

    if not isinstance(encoded, str) or not encoded:
        raise JoinContractError("delivery-receipt-invalid")
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.standard_b64decode(padded.encode("ascii"))
    except ValueError as exc:
        raise JoinContractError("delivery-receipt-invalid") from exc
    try:
        value = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise JoinContractError("delivery-receipt-invalid") from exc
    if not isinstance(value, dict):
        raise JoinContractError("delivery-receipt-invalid")
    return value


OWNER_ROUTE_NODE = "_owner"
NO_PARENT_ATTEMPT = "-"
PENDING_DELIVERY_LOG = "dispatch-pending-delivery.log"


def pending_record_identity(metadata: dict[str, str]) -> tuple[str, str, str]:
    """Resolve the (route_id, route_node, parent_attempt_id) identity triple a
    pending-delivery record requires from one terminal row's metadata.

    A dispatch-depth-2 stage row carries ``route_id``/``route_node`` directly.
    A dispatch-depth-1 owner row never does: the owner is bound to its route
    through ``owner_route_id`` and owns no node, and its parent is the
    depth-0 session (no parent attempt). Before 2026-08-29 the materializer
    passed the empty strings straight through, so every owner terminal --
    the one recipient the depth-0 wake actually depends on -- was refused as
    ``identity-incomplete`` and no production record ever existed. Owner rows
    now resolve to ``owner_route_id`` + the ``_owner`` node sentinel + the
    ``-`` no-parent sentinel; stage rows are unchanged.
    """

    route_id = metadata.get("route_id") or ""
    route_node = metadata.get("route_node") or ""
    parent_attempt_id = metadata.get("parent_attempt_id") or ""
    is_owner_row = (
        metadata.get("worker_type") == "owner"
        or metadata.get("dispatch_depth") == "1"
    )
    if is_owner_row:
        route_id = route_id or metadata.get("owner_route_id") or ""
        route_node = route_node or OWNER_ROUTE_NODE
        parent_attempt_id = parent_attempt_id or NO_PARENT_ATTEMPT
    return route_id, route_node, parent_attempt_id


def _log_pending_delivery_refusal(jobs: Path, attempt_id: str, reason: str) -> None:
    """Durable trace of a swallowed materialize failure. stderr alone is not
    kept by any supervisor, so a refusal used to leave no evidence anywhere
    (2026-08-29: three owner terminals refused, zero trace). Never raises."""

    try:
        root = jobs.resolve(strict=False).parent
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": time.time(),
                "event": "delivery-persistence-refused",
                "attempt_id": attempt_id,
                "reason": reason,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with open(log_dir / PENDING_DELIVERY_LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def materialize_pending_delivery(jobs: Path, row_fields: list[str]) -> Path | None:
    """Carrier-independent idempotent materializer (SD-111 P2 round 2 C-1).

    Turns one already-intent-stamped row into its durable pending-delivery
    record. Never acquires the registry lock -- the row is read from
    ``row_fields`` already in the caller's hand, exactly the property that
    keeps a ``registry.lock -> record.lock`` nesting from ever existing
    (§4.4, the reason direction B was chosen over A). Two independent
    triggers call this: (1) in-process, right after the terminal transaction
    commits and the registry lock is released, and (2) the dispatch
    reconcile path's idempotent backstop. Both converge on
    ``dispatch_pending_delivery.create``'s ``O_EXCL`` + identity-verify
    semantics, so N calls for the same row produce exactly one record.

    Returns the record path, or ``None`` if the row carries no delivery
    intent (nothing to materialize).
    """

    if len(row_fields) != 6:
        raise JoinContractError("delivery-receipt-invalid")
    metadata = parse_registry_metadata(row_fields[5])
    if metadata.get("delivery_intent") != "1":
        return None
    required = (
        "delivery_id", "delivery_recipient_kind", "delivery_receipt_digest",
        "delivery_receipt_b64", "delivery_row_revision", "attempt_id",
        "parent_sid",
    )
    if any(not metadata.get(key) for key in required):
        raise pending_delivery.PendingDeliveryError(
            "delivery-persistence-refused", "intent-fields-incomplete"
        )
    receipt = unseal_delivery_receipt(metadata["delivery_receipt_b64"])
    root = jobs.resolve(strict=False).parent
    route_id, route_node, parent_attempt_id = pending_record_identity(metadata)
    record = pending_delivery.create(
        root,
        recipient_kind=metadata["delivery_recipient_kind"],
        recipient_key=metadata["parent_sid"],
        delivery_id=metadata["delivery_id"],
        session_generation=metadata.get("session_generation", ""),
        session_generation_supported=metadata.get("session_generation_supported", "0"),
        attempt_ids=[metadata["attempt_id"]],
        parent_attempt_id=parent_attempt_id,
        route_id=route_id,
        route_node=route_node,
        receipt=receipt,
        receipt_digest=metadata["delivery_receipt_digest"],
        row_revisions={metadata["attempt_id"]: metadata["delivery_row_revision"]},
    )
    return pending_delivery.record_path(root, metadata["parent_sid"], record["delivery_id"])


def materialize_after_terminal_close(jobs: Path, attempt_id: str) -> Path | None:
    """SD-111 P2 trigger 1 -- shared helper. Call once, right after a
    lock-releasing terminal close (`close_attempt_row`, `close_attempt_row_if`,
    `reconcile_attempt_terminal`'s `closed` outcome, or
    `marker_bound_delivery_transaction`'s `advanced` outcome) returns.  Never
    from inside the registry lock, never from a carrier.

    Reads the just-closed row unlocked -- by the time this runs the delivery
    identity fields are already write-once immutable (§4.4), so an unlocked
    read is exactly as safe as `current_attempt_row` already is for every
    other terminal-evidence consumer -- and materializes its pending-delivery
    record if the row carries intent.

    Never raises. The row is already durably closed by the time this call
    happens, so a materialize failure here must never look like the close
    itself failed; it is recorded as ``delivery-persistence-refused`` on
    stderr and swallowed. Idempotent with every other trigger 1/2 call site
    (`dispatch_pending_delivery.create`'s O_EXCL + identity-verify semantics
    converge N calls on one record).
    """

    try:
        row = current_attempt_row(jobs, attempt_id)
        if row is None:
            return None
        return materialize_pending_delivery(jobs, row.raw.split("\t"))
    except (JoinContractError, pending_delivery.PendingDeliveryError, OSError) as exc:
        sys.stderr.write(
            f"delivery-persistence-refused attempt_id={attempt_id} reason={exc}\n"
        )
        _log_pending_delivery_refusal(jobs, attempt_id, str(exc))
        return None


def supersede_pending_delivery_for_advance(jobs: Path, predecessor_attempt_id: str) -> str:
    """SD-111 F-2 / A-17 -- an SD-110 eligible-linear runtime advance carries
    no model delivery at all (PRD §13.33.1-(8)). No discriminator exists at
    intent-stamp time (the predecessor row closes through the ordinary W2
    edge and gets its ordinary intent stamp before eligible-linear success is
    even decided -- see plan §4.3.1), so the successful advance transaction
    itself -- the one place "this completion will never be delivered to a
    model" becomes true -- supersedes the predecessor's record here.

    Materializes first if the trigger-1/trigger-2 crash window has not
    closed yet, then expires as ``receipt-row-superseded`` (§13.33.1-(7)),
    the same reason SD-106 uses when a retry's new receipt obsoletes an old
    one. Expired, never deleted. A-18's refusal counter-case never reaches
    this function -- only an actually-advanced successor does.

    Never raises, full stop -- the caller commits the advance before this
    runs (F-6: a committed advance's outcome must never be unwound by
    delivery-layer cleanup), so every step here, including the initial row
    read and the expiry call's own lock/IO, is armored. A read or lock
    failure is reported the same way `materialize_after_terminal_close`
    already reports its own persistence failures: the typed
    ``delivery-persistence-refused`` outcome, never a raised exception. Every
    other ordinary "nothing to supersede" case (no recipient declared, no
    intent stamped, record already terminal) returns its own short typed
    outcome string, all for the caller's own logging only.
    """

    try:
        row = current_attempt_row(jobs, predecessor_attempt_id)
    except (JoinContractError, OSError) as exc:
        return f"delivery-persistence-refused:{exc}"
    if row is None:
        return "row-missing"
    metadata = row.metadata
    if metadata.get("delivery_intent") != "1":
        return "no-intent"
    recipient_key = metadata.get("parent_sid", "")
    delivery_id = metadata.get("delivery_id", "")
    if not recipient_key or not delivery_id:
        return "delivery-identity-incomplete"
    root = jobs.resolve(strict=False).parent
    try:
        materialize_pending_delivery(jobs, row.raw.split("\t"))
    except (JoinContractError, pending_delivery.PendingDeliveryError, OSError):
        pass
    try:
        pending_delivery.expire_if_due(
            root, recipient_key, delivery_id,
            actor="dispatch-reconcile",
            reason="receipt-row-superseded",
            liveness="known",
        )
    except pending_delivery.PendingDeliveryError as exc:
        return f"expire-refused:{exc.reason}"
    except OSError as exc:
        return f"delivery-persistence-refused:{exc}"
    return "superseded"


def reconcile_pending_delivery(jobs: Path) -> dict[str, int]:
    """SD-111 P2 trigger 2 + the single declared expiry actor (§2-b-2/§2-c).

    Meant to run on the same bounded cadence as ``dispatch-registry.py
    reconcile --apply`` (the existing "dispatch reconcile path" -- no new
    driver process is introduced; this function is called from there).  Two
    independent sweeps, neither depending on the other:

    1. **Materialize backstop.** Any row with ``delivery_intent`` stamped
       that trigger 1 has not yet turned into a record (the crash window
       between the terminal commit and trigger 1's in-process call) is
       materialized here, idempotently converging with any other trigger.
    2. **Expiry.** Any open-state (``pending``/``claimed``/``sent-ambiguous``)
       record whose owning row's recorded launch-time incarnation
       (``parent_runtime_pid``/``parent_runtime_pid_start``, §2-a-5) is
       provably dead expires under the one declared actor
       ``dispatch-reconcile`` (§10.2). A record whose liveness cannot be
       determined -- fields absent, owning row gone, digest mismatch -- is
       left untouched: unknown liveness never expires.

    Never raises; each row/record is handled independently so one bad row or
    record cannot stop the sweep. Observers (fleet, sweep hooks) have no call
    path to this function -- only this reconcile driver does.
    """

    root = jobs.resolve(strict=False).parent
    result = {"materialized": 0, "expired": 0, "skipped": 0}
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    rows_by_attempt: dict[str, dict[str, str]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        attempt_id = metadata.get("attempt_id", "")
        if attempt_id:
            rows_by_attempt[attempt_id] = metadata
        if attempt_id and metadata.get("delivery_intent") == "1":
            record_file = None
            if metadata.get("parent_sid") and metadata.get("delivery_id"):
                try:
                    record_file = pending_delivery.record_path(
                        root, metadata["parent_sid"], metadata["delivery_id"]
                    )
                except pending_delivery.PendingDeliveryError:
                    record_file = None
            if record_file is not None and not record_file.is_file():
                if materialize_after_terminal_close(jobs, attempt_id) is not None:
                    result["materialized"] += 1

    pending_root = root / "pending-delivery"
    if pending_root.is_dir():
        for record_file in pending_root.glob("*/*.json"):
            try:
                record = json.loads(record_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("state") not in {
                "pending", "claimed", "sent-ambiguous",
            }:
                continue
            owner_metadata = None
            attempt_ids = record.get("attempt_ids")
            if isinstance(attempt_ids, list):
                for candidate in attempt_ids:
                    if candidate in rows_by_attempt:
                        owner_metadata = rows_by_attempt[candidate]
                        break
            if owner_metadata is None:
                result["skipped"] += 1
                continue
            recipient_key = owner_metadata.get("parent_sid", "")
            if (
                not recipient_key
                or pending_delivery.recipient_digest(recipient_key)
                != record.get("recipient_digest")
            ):
                result["skipped"] += 1
                continue
            raw_pid = owner_metadata.get("parent_runtime_pid", "")
            pid_start = owner_metadata.get("parent_runtime_pid_start", "")
            if not raw_pid or not pid_start:
                result["skipped"] += 1
                continue
            try:
                disposition = process_identity_disposition(int(raw_pid), pid_start)
            except ValueError:
                result["skipped"] += 1
                continue
            # F-1: only a positive "dead" determination expires. "live" and
            # "unresolved" (e.g. a transient procfs/permission/namespace
            # denial) both leave the record pending -- an inaccessible
            # observation is not a session-gone observation.
            if disposition != "dead":
                continue
            try:
                pending_delivery.expire_if_due(
                    root, recipient_key, record["delivery_id"],
                    actor="dispatch-reconcile",
                    reason="recipient-session-gone",
                    liveness="known",
                )
                result["expired"] += 1
            except pending_delivery.PendingDeliveryError:
                pass
    return result


def required_action_for_attempt(status: str, metadata: dict[str, str]) -> str:
    """Return the one typed follow-up that the exact registry row permits."""

    if status in OPEN_STATES:
        return "complete-open"
    if status != "done":
        raise JoinContractError("owned-row-status-invalid")
    if metadata.get("failure_class") == "pass" or metadata.get("note") in SUCCESS_NOTES:
        return "advance-completed"
    return "inspect-done-failure"


def harvest_command_lines(prompt: str) -> list[str]:
    """Extract the exact harvest command lines a supervisor prompt prescribes.

    Both ``completion_prompt`` and ``remediation_prompt`` (in either
    supervisor) emit each harvest command on its own line, second shell token
    ``harvest``; every other line is prose. Used by the producer<->guard
    parity fixture (plan SS3.4 D1b) so that fixture never needs a
    hand-written command literal.
    """

    lines = []
    for line in prompt.splitlines():
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            continue
        if (
            len(tokens) >= 2
            and tokens[1] == "harvest"
            and tokens[0].endswith("preflight.sh")
        ):
            lines.append(line)
    return lines


@dataclass(frozen=True)
class ChildRow:
    order: int
    status: str
    slug: str
    attempt_id: str
    raw: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class CurrentDeliveryState:
    """One marker/current-row classification snapshot from the jobs lock."""

    marker: dict[str, object] | None
    marker_digest: str
    row_revision: str
    row_digest: str
    status: str
    verdict: str
    quiescent: bool
    owned_children: int
    advanced: bool
    supervisor_terminal: bool = False


def delivery_classification(state: CurrentDeliveryState) -> str:
    """Return the sole shared success/attention decision for delivery writers."""

    return (
        "success"
        if (
            (
                (state.marker is not None and bool(state.marker_digest))
                or state.supervisor_terminal
            )
            and state.status == "done"
            and state.verdict == "PASS"
            and state.quiescent
            and state.owned_children == 0
        )
        else "attention"
    )


def delivery_required_action(state: CurrentDeliveryState) -> str:
    """Map the shared classification to its only legal parent action."""

    classification = delivery_classification(state)
    if classification == "success":
        return "advance-completed"
    if state.status in OPEN_STATES:
        return "complete-open"
    return "inspect-done-failure"


def delivery_timing_fields(**values: int | None) -> dict[str, int | None]:
    """Project the same versioned timing vocabulary on every runtime surface."""

    unknown = set(values).difference(DELIVERY_TIMING_POINTS)
    if unknown:
        raise JoinContractError("delivery-timing-field-invalid")
    for value in values.values():
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise JoinContractError("delivery-timing-value-invalid")
    result = {
        "delivery_timing_schema_version": DELIVERY_TIMING_SCHEMA_VERSION,
        **{point: values.get(point) for point in DELIVERY_TIMING_POINTS},
    }
    observed = [result[point] for point in DELIVERY_TIMING_POINTS if result[point] is not None]
    if observed != sorted(observed):
        raise JoinContractError("delivery-timing-order-invalid")
    return result


def validate_delivery_timing(value: object) -> dict[str, int | None]:
    """Validate and normalize one complete SD-109 timing projection."""

    if not isinstance(value, dict) or set(value) != {
        "delivery_timing_schema_version",
        *DELIVERY_TIMING_POINTS,
    }:
        raise JoinContractError("delivery-timing-shape-invalid")
    if value.get("delivery_timing_schema_version") != DELIVERY_TIMING_SCHEMA_VERSION:
        raise JoinContractError("delivery-timing-version-invalid")
    return delivery_timing_fields(
        **{point: value.get(point) for point in DELIVERY_TIMING_POINTS}
    )


def advance_delivery_timing(
    timing: dict[str, int | None] | None,
    point: str,
    *,
    at_ns: int | None = None,
) -> dict[str, int | None]:
    """Idempotently stamp one real lifecycle boundary in monotonic order."""

    if point not in DELIVERY_TIMING_POINTS:
        raise JoinContractError("delivery-timing-field-invalid")
    current = validate_delivery_timing(timing or delivery_timing_fields())
    if current[point] is not None:
        return current
    value = time.monotonic_ns() if at_ns is None else at_ns
    candidate = dict(current)
    candidate[point] = value
    return validate_delivery_timing(candidate)


def stamp_delivery_receipt(
    receipt: dict[str, object], point: str, *, at_ns: int | None = None
) -> dict[str, object]:
    stamped = dict(receipt)
    inherited = receipt.get("delivery_timing")
    stamped["delivery_timing"] = advance_delivery_timing(
        inherited if isinstance(inherited, dict) else None,
        point,
        at_ns=at_ns,
    )
    return stamped


def receipt_with_delivery_observability(
    receipt: dict[str, object],
    *,
    jobs: Path,
    timing: dict[str, int | None] | None = None,
) -> dict[str, object]:
    """Attach shared per-attempt classification and one timing vocabulary."""

    raw_children = receipt.get("children")
    if not isinstance(raw_children, list):
        raise JoinContractError("delivery-receipt-children-invalid")
    children: list[dict[str, object]] = []
    classifications: list[str] = []
    for raw_child in raw_children:
        if not isinstance(raw_child, dict):
            raise JoinContractError("delivery-receipt-child-invalid")
        attempt_id = raw_child.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise JoinContractError("delivery-receipt-attempt-invalid")
        try:
            state = current_delivery_state(
                jobs, attempt_id, parent_attempt_id=attempt_id
            )
        except (DispatchContractError, OSError) as exc:
            reason = exc.reason if isinstance(exc, DispatchContractError) else type(exc).__name__
            raise JoinContractError(f"delivery-transaction-failed:{reason}") from exc
        classification = delivery_classification(state)
        child = dict(raw_child)
        child["status"] = state.status
        child["delivery_classification"] = classification
        child["required_action"] = delivery_required_action(state)
        if classification == "attention":
            child["reason"] = "terminal-failure-or-unclosed"
        elif state.advanced:
            child["reason"] = "row-advanced"
        children.append(child)
        classifications.append(classification)
    projected = dict(receipt)
    projected["children"] = children
    projected["delivery_classification"] = (
        "success" if classifications and set(classifications) == {"success"} else "attention"
    )
    inherited_timing = receipt.get("delivery_timing")
    base_timing = (
        validate_delivery_timing(inherited_timing)
        if isinstance(inherited_timing, dict)
        else delivery_timing_fields()
    )
    supplied_timing = validate_delivery_timing(timing) if timing is not None else None
    if supplied_timing is not None:
        for point in DELIVERY_TIMING_POINTS:
            if supplied_timing[point] is not None:
                base_timing = advance_delivery_timing(
                    base_timing, point, at_ns=supplied_timing[point]
                )
    projected["delivery_timing"] = validate_delivery_timing(
        base_timing
    )
    return projected


@dataclass(frozen=True)
class SupervisorOutbox:
    """One idempotent completion receipt committed before model delivery."""

    receipt_id: str
    receipt_digest: str
    attempt_ids: frozenset[str]
    row_revisions: tuple[tuple[str, str], ...]
    receipt: dict[str, object] | None = None
    consumed_attempt_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class SupervisorState:
    """Validated schema-v2 supervisor phase and optional durable outbox."""

    phase: str
    delivered_attempt_ids: frozenset[str]
    outbox: SupervisorOutbox | None = None


@dataclass(frozen=True)
class ParentSessionState:
    attempt_ids: frozenset[str]
    delivered_attempt_ids: frozenset[str]


@dataclass(frozen=True)
class SupervisorShellAction:
    kind: str
    attempt_id: str = ""
    status: str = ""
    mark_done: bool = False
    failure_detail: bool = False


@dataclass(frozen=True)
class SupervisedDispatchContext:
    """Immutable owner boundary used while a supervised batch is parked."""

    jobs: Path
    route_file: Path
    route_id: str
    parent_attempt_id: str
    route: dict[str, object]
    rows: tuple[ChildRow, ...]


def runtime_wait_requested(value: object) -> bool:
    """Accept only the exact owner park sentinel, never prose that mentions it."""

    return isinstance(value, str) and value.strip() == RUNTIME_WAIT_SENTINEL


def unstarted_child_attempts(rows: list[ChildRow]) -> set[str]:
    """Return registered children lacking the durable child-spawn receipt.

    ``launch_started=1`` is written only after the exact PID/start/PGID fence has
    proved a live spawned process.  It is therefore the registry equivalent of a
    wrapper receipt with registered=1, started=1, and child_spawned=1.
    """

    return {
        row.attempt_id
        for row in rows
        if row.metadata.get("launch_started") != "1"
    }


def start_retry_prompt(attempts: set[str] | None = None) -> str:
    """Bounded same-session correction for a preview/register-only park."""

    exact = ",".join(sorted(attempts or ())) or "none"
    return (
        "Runtime wait was rejected because no fully started child batch exists "
        f"(registered-only attempts: {exact}). In this turn, rerun the checked child "
        "dispatch with --start, not --dry-run or --register. Treat check=ok or an "
        "attempt_id as preview metadata only. Yield `runtime_wait: registered-children` "
        "only after the start receipt itself reports registered=1, started=1, and "
        "child_spawned=1. Do not perform unrelated work."
    )


def _safe_identity(value: str) -> bool:
    return (
        0 < len(value.encode("utf-8")) <= 256
        and "," not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


@contextmanager
def _supervisor_state_lock(path: Path):
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise JoinContractError("supervisor-state-lock-unavailable") from exc


def _write_supervisor_state_unlocked(
    path: Path | None,
    parent_attempt_id: str,
    delivered_attempt_ids: set[str],
    *,
    phase: str = "parked",
    outbox: SupervisorOutbox | None = None,
) -> None:
    if path is None:
        return
    previous_phase = "absent"
    try:
        previous_value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(previous_value, dict) and isinstance(
            previous_value.get("phase"), str
        ):
            previous_phase = previous_value["phase"]
    except (FileNotFoundError, OSError, ValueError):
        pass
    if (
        not path.is_absolute()
        or not _safe_identity(parent_attempt_id)
        or phase not in STATE_PHASES
        or any(not _safe_identity(attempt) for attempt in delivered_attempt_ids)
        or (outbox is not None and phase not in {"deliverable", "recovery"})
    ):
        raise JoinContractError("supervisor-state-contract-invalid")
    value = {
        "schema_version": STATE_SCHEMA_VERSION,
        "parent_attempt_id": parent_attempt_id,
        "delivered_attempt_ids": sorted(delivered_attempt_ids),
        "phase": phase,
    }
    if outbox is not None:
        if (
            not _safe_identity(outbox.receipt_id)
            or not re_fullmatch_digest(outbox.receipt_digest)
            or not outbox.attempt_ids
            or len(outbox.attempt_ids) > MAX_BATCH_ATTEMPTS
            or not outbox.attempt_ids.issubset(delivered_attempt_ids)
            or any(not _safe_identity(attempt) for attempt in outbox.attempt_ids)
            or {attempt for attempt, _revision in outbox.row_revisions}
            != set(outbox.attempt_ids)
            or any(
                not _safe_identity(attempt) or not re_fullmatch_digest(revision)
                for attempt, revision in outbox.row_revisions
            )
            or outbox.receipt is None
            or not outbox.consumed_attempt_ids.issubset(outbox.attempt_ids)
        ):
            raise JoinContractError("supervisor-outbox-contract-invalid")
        try:
            receipt_bytes = json.dumps(
                outbox.receipt, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise JoinContractError("supervisor-outbox-receipt-invalid") from exc
        if hashlib.sha256(receipt_bytes).hexdigest() != outbox.receipt_digest:
            raise JoinContractError("supervisor-outbox-receipt-digest-mismatch")
        value["outbox"] = {
            "receipt_id": outbox.receipt_id,
            "receipt_digest": outbox.receipt_digest,
            "attempt_ids": sorted(outbox.attempt_ids),
            "row_revisions": dict(outbox.row_revisions),
            "receipt": outbox.receipt,
            "consumed_attempt_ids": sorted(outbox.consumed_attempt_ids),
        }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise JoinContractError("supervisor-state-oversized")
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".supervisor-state.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        _append_supervisor_transition(
            path,
            parent_attempt_id,
            previous_phase,
            phase,
            outbox,
        )
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise JoinContractError("supervisor-state-unwritable") from exc


def _append_supervisor_transition(
    path: Path,
    parent_attempt_id: str,
    previous_phase: str,
    phase: str,
    outbox: SupervisorOutbox | None,
) -> None:
    """Append the exact outer process and phase edge for later diagnosis."""

    try:
        stat_fields = (Path("/proc") / str(os.getpid()) / "stat").read_text(
            encoding="utf-8"
        )
        process_start = stat_fields[stat_fields.rfind(")") + 2 :].split()[19]
        event = {
            "schema_version": 1,
            "parent_attempt_id": parent_attempt_id,
            "previous_phase": previous_phase,
            "phase": phase,
            "outer_pid": os.getpid(),
            "outer_pid_start": process_start,
            "receipt_id": outbox.receipt_id if outbox is not None else "",
            "consumed_attempt_ids": (
                sorted(outbox.consumed_attempt_ids) if outbox is not None else []
            ),
        }
        encoded = json.dumps(
            event, separators=(",", ":"), sort_keys=True
        ).encode("utf-8") + b"\n"
        audit = path.with_name(f"{path.name}.transitions.jsonl")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(audit, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            if os.write(fd, encoded) != len(encoded):
                raise OSError("short supervisor transition write")
            os.fsync(fd)
        finally:
            os.close(fd)
    except (IndexError, OSError) as exc:
        raise JoinContractError("supervisor-transition-audit-unwritable") from exc


def write_supervisor_state(
    path: Path | None,
    parent_attempt_id: str,
    delivered_attempt_ids: set[str],
    *,
    phase: str = "parked",
    outbox: SupervisorOutbox | None = None,
) -> None:
    """Atomically publish the bounded phase state consumed by native hooks."""

    if path is None:
        return
    with _supervisor_state_lock(path):
        _write_supervisor_state_unlocked(
            path,
            parent_attempt_id,
            delivered_attempt_ids,
            phase=phase,
            outbox=outbox,
        )


def re_fullmatch_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def read_supervisor_phase_state(
    path: Path | None,
    parent_attempt_id: str,
) -> SupervisorState | None:
    """Return a fully validated phase/outbox snapshot, or ``None``."""

    if path is None or not path.is_absolute() or not _safe_identity(parent_attempt_id):
        return None
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_SCHEMA_VERSION
        or value.get("parent_attempt_id") != parent_attempt_id
        or value.get("phase") not in STATE_PHASES
    ):
        return None
    raw = value.get("delivered_attempt_ids")
    if not isinstance(raw, list) or len(raw) > 64:
        return None
    delivered: set[str] = set()
    for attempt in raw:
        if not isinstance(attempt, str) or not _safe_identity(attempt) or attempt in delivered:
            return None
        delivered.add(attempt)
    outbox_value = value.get("outbox")
    outbox: SupervisorOutbox | None = None
    if outbox_value is not None:
        if not isinstance(outbox_value, dict) or value.get("phase") not in {
            "deliverable",
            "recovery",
        }:
            return None
        receipt_id = outbox_value.get("receipt_id")
        receipt_digest = outbox_value.get("receipt_digest")
        raw_attempts = outbox_value.get("attempt_ids")
        raw_revisions = outbox_value.get("row_revisions")
        receipt = outbox_value.get("receipt")
        raw_consumed = outbox_value.get("consumed_attempt_ids", [])
        if (
            not isinstance(receipt_id, str)
            or not _safe_identity(receipt_id)
            or not re_fullmatch_digest(receipt_digest)
            or not isinstance(raw_attempts, list)
            or not raw_attempts
            or len(raw_attempts) > MAX_BATCH_ATTEMPTS
            or not isinstance(raw_revisions, dict)
            or not isinstance(receipt, dict)
            or not isinstance(raw_consumed, list)
        ):
            return None
        attempts: set[str] = set()
        revisions: list[tuple[str, str]] = []
        for attempt in raw_attempts:
            revision = raw_revisions.get(attempt)
            if (
                not isinstance(attempt, str)
                or not _safe_identity(attempt)
                or attempt in attempts
                or not re_fullmatch_digest(revision)
            ):
                return None
            attempts.add(attempt)
            revisions.append((attempt, str(revision)))
        if set(raw_revisions) != attempts or not attempts.issubset(delivered):
            return None
        consumed: set[str] = set()
        for attempt in raw_consumed:
            if (
                not isinstance(attempt, str)
                or attempt not in attempts
                or attempt in consumed
            ):
                return None
            consumed.add(attempt)
        try:
            receipt_bytes = json.dumps(
                receipt, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
        except (TypeError, ValueError):
            return None
        if hashlib.sha256(receipt_bytes).hexdigest() != receipt_digest:
            return None
        outbox = SupervisorOutbox(
            receipt_id,
            str(receipt_digest),
            frozenset(attempts),
            tuple(sorted(revisions)),
            receipt,
            frozenset(consumed),
        )
    return SupervisorState(
        str(value["phase"]), frozenset(delivered), outbox
    )


def read_supervisor_state(
    path: Path | None,
    parent_attempt_id: str,
) -> set[str] | None:
    """Compatibility view returning only the delivered attempt set."""

    state = read_supervisor_phase_state(path, parent_attempt_id)
    return set(state.delivered_attempt_ids) if state is not None else None


def child_row_revision(row: ChildRow) -> str:
    """Return the bounded exact-row revision sealed into an outbox receipt."""

    return hashlib.sha256(row.raw.encode("utf-8")).hexdigest()


def receipt_with_current_actions(
    receipt: dict[str, object], rows: list[ChildRow], *, jobs: Path
) -> dict[str, object]:
    """Refresh through the shared delivery classifier, never a second mapper."""

    raw_children = receipt.get("children")
    if not isinstance(raw_children, list) or not raw_children:
        raise JoinContractError("supervisor-outbox-children-invalid")
    indexed = {row.attempt_id: row for row in rows}
    seen: set[str] = set()
    for raw_child in raw_children:
        if not isinstance(raw_child, dict):
            raise JoinContractError("supervisor-outbox-children-invalid")
        attempt = raw_child.get("attempt_id")
        if (
            not isinstance(attempt, str)
            or attempt in seen
            or attempt not in indexed
        ):
            raise JoinContractError("supervisor-outbox-attempt-set-mismatch")
        seen.add(attempt)
    if seen != set(indexed):
        raise JoinContractError("supervisor-outbox-attempt-set-mismatch")
    return receipt_with_delivery_observability(receipt, jobs=jobs)


def prepare_supervisor_outbox(
    path: Path | None,
    parent_attempt_id: str,
    delivered_attempt_ids: set[str],
    receipt: dict[str, object],
    rows: list[ChildRow],
) -> SupervisorState:
    """Commit one deterministic actionable receipt before model delivery."""

    if receipt.get("state") in {"timeout", "no-children", "contract-error"}:
        raise JoinContractError("supervisor-outbox-receipt-not-actionable")
    raw_children = receipt.get("children")
    if not isinstance(raw_children, list) or not raw_children:
        raise JoinContractError("supervisor-outbox-children-invalid")
    attempts = {
        str(child.get("attempt_id"))
        for child in raw_children
        if isinstance(child, dict) and isinstance(child.get("attempt_id"), str)
    }
    indexed = {row.attempt_id: row for row in rows}
    if (
        len(attempts) != len(raw_children)
        or not attempts
        or len(attempts) > MAX_BATCH_ATTEMPTS
        or set(indexed) != attempts
    ):
        raise JoinContractError("supervisor-outbox-attempt-set-mismatch")
    canonical_receipt = json.dumps(
        receipt, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    receipt_digest = hashlib.sha256(canonical_receipt).hexdigest()
    revisions = tuple(
        sorted((attempt, child_row_revision(indexed[attempt])) for attempt in attempts)
    )
    identity_material = json.dumps(
        {
            "parent_attempt_id": parent_attempt_id,
            "receipt_digest": receipt_digest,
            "row_revisions": dict(revisions),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    outbox = SupervisorOutbox(
        "receipt-" + hashlib.sha256(identity_material).hexdigest()[:32],
        receipt_digest,
        frozenset(attempts),
        revisions,
        dict(receipt),
    )
    delivered = set(delivered_attempt_ids).union(attempts)
    write_supervisor_state(
        path,
        parent_attempt_id,
        delivered,
        phase="deliverable",
        outbox=outbox,
    )
    return SupervisorState("deliverable", frozenset(delivered), outbox)


def refresh_supervisor_outbox_actions(
    path: Path | None,
    parent_attempt_id: str,
    rows: list[ChildRow],
    *,
    jobs: Path,
) -> SupervisorState:
    """Recommit one pending outbox against the exact current row generations.

    The receipt identity names one delivery transaction and therefore remains
    stable across a row advance.  Its digest, copied status/action fields, and
    row revisions must advance together before the next model delivery.
    """

    if path is None:
        raise JoinContractError("supervisor-state-path-missing")
    with _supervisor_state_lock(path):
        state = read_supervisor_phase_state(path, parent_attempt_id)
        if state is None or state.outbox is None:
            raise JoinContractError("supervisor-outbox-missing")
        outbox = state.outbox
        indexed = {row.attempt_id: row for row in rows}
        if set(indexed) != set(outbox.attempt_ids):
            raise JoinContractError("supervisor-outbox-attempt-set-mismatch")
        refreshed_receipt = receipt_with_current_actions(
            outbox.receipt or {}, rows, jobs=jobs
        )
        encoded = json.dumps(
            refreshed_receipt, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        refreshed_outbox = SupervisorOutbox(
            outbox.receipt_id,
            hashlib.sha256(encoded).hexdigest(),
            outbox.attempt_ids,
            tuple(
                sorted(
                    (attempt, child_row_revision(indexed[attempt]))
                    for attempt in outbox.attempt_ids
                )
            ),
            refreshed_receipt,
            outbox.consumed_attempt_ids,
        )
        _write_supervisor_state_unlocked(
            path,
            parent_attempt_id,
            set(state.delivered_attempt_ids),
            phase="deliverable",
            outbox=refreshed_outbox,
        )
        return SupervisorState(
            "deliverable", state.delivered_attempt_ids, refreshed_outbox
        )


def supervisor_outbox_row_state(
    state: SupervisorState,
    rows: list[ChildRow],
) -> str:
    """Compare an outbox to current exact rows without trusting stale status."""

    if state.outbox is None:
        return "absent"
    indexed = {row.attempt_id: row for row in rows}
    pending = state.outbox.attempt_ids.difference(
        state.outbox.consumed_attempt_ids
    )
    if not pending:
        return "consumed"
    if not pending.issubset(indexed):
        return "row-missing"
    current = {
        attempt: child_row_revision(indexed[attempt])
        for attempt in pending
    }
    expected = {
        attempt: revision
        for attempt, revision in state.outbox.row_revisions
        if attempt in pending
    }
    return "current" if current == expected else "row-advanced"


def consume_supervisor_outbox_attempts(
    path: Path | None,
    parent_attempt_id: str,
    attempt_ids: set[str],
) -> bool:
    """Exactly once consume successful current-row actions from one outbox."""

    if path is None or not attempt_ids:
        return False
    with _supervisor_state_lock(path):
        state = read_supervisor_phase_state(path, parent_attempt_id)
        if state is None or state.outbox is None:
            return False
        outbox = state.outbox
        pending = outbox.attempt_ids.difference(outbox.consumed_attempt_ids)
        if not attempt_ids.issubset(pending):
            return False
        consumed = outbox.consumed_attempt_ids.union(attempt_ids)
        remaining = outbox.attempt_ids.difference(consumed)
        next_outbox = None
        phase = "running-turn"
        if remaining:
            next_outbox = SupervisorOutbox(
                outbox.receipt_id,
                outbox.receipt_digest,
                outbox.attempt_ids,
                outbox.row_revisions,
                outbox.receipt,
                frozenset(consumed),
            )
            phase = state.phase
        _write_supervisor_state_unlocked(
            path,
            parent_attempt_id,
            set(state.delivered_attempt_ids),
            phase=phase,
            outbox=next_outbox,
        )
        return True


def consume_advance_completed_outbox(
    path: Path | None,
    parent_attempt_id: str,
    rows: list[ChildRow],
) -> set[str]:
    """Consume no-command advance actions after current-row revalidation."""

    state = read_supervisor_phase_state(path, parent_attempt_id)
    if state is None or state.outbox is None:
        return set()
    pending = state.outbox.attempt_ids.difference(
        state.outbox.consumed_attempt_ids
    )
    indexed = {row.attempt_id: row for row in rows}
    completed = {
        attempt
        for attempt in pending
        if attempt in indexed
        and any(
            isinstance(child, dict)
            and child.get("attempt_id") == attempt
            and child.get("delivery_classification") == "success"
            and child.get("required_action") == "advance-completed"
            for child in ((state.outbox.receipt or {}).get("children") or [])
        )
    }
    if completed and consume_supervisor_outbox_attempts(
        path, parent_attempt_id, completed
    ):
        return completed
    return set()


def remove_supervisor_state(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A unique attempt path cannot wake a later owner. Reconciliation may
        # remove an unreadable leftover after the process exits.
        pass


def parent_session_state_path(jobs: Path, parent_session_id: str) -> Path:
    """Return a non-identifying, registry-scoped phase-state path."""

    if not jobs.is_absolute() or not _safe_identity(parent_session_id):
        raise JoinContractError("parent-session-state-contract-invalid")
    digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()
    return jobs.resolve(strict=False).parent / "parent-session-state" / f"{digest}.json"


@contextmanager
def _parent_session_state_lock(path: Path):
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        with lock_path.open("a+", encoding="utf-8") as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise JoinContractError("parent-session-state-lock-unavailable") from exc


def _write_parent_session_state_unlocked(
    path: Path,
    parent_session_id: str,
    delivered_attempt_ids: set[str],
    attempt_ids: set[str],
) -> None:
    if (
        not path.is_absolute()
        or not _safe_identity(parent_session_id)
        or not attempt_ids
        or len(attempt_ids) > MAX_BATCH_ATTEMPTS
        or len(delivered_attempt_ids) > MAX_BATCH_ATTEMPTS
        or not delivered_attempt_ids.issubset(attempt_ids)
        or any(not _safe_identity(attempt) for attempt in attempt_ids)
        or any(not _safe_identity(attempt) for attempt in delivered_attempt_ids)
    ):
        raise JoinContractError("parent-session-state-contract-invalid")
    value = {
        "schema_version": PARENT_SESSION_STATE_SCHEMA_VERSION,
        "parent_session_id_sha256": hashlib.sha256(
            parent_session_id.encode("utf-8")
        ).hexdigest(),
        "attempt_ids": sorted(attempt_ids),
        "delivered_attempt_ids": sorted(delivered_attempt_ids),
        "phase": (
            "delivered"
            if delivered_attempt_ids == attempt_ids
            else "pending"
        ),
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) > MAX_STATE_BYTES:
        raise JoinContractError("parent-session-state-oversized")
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=".parent-session-state.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise JoinContractError("parent-session-state-unwritable") from exc


def write_parent_session_state(
    path: Path,
    parent_session_id: str,
    delivered_attempt_ids: set[str],
    *,
    attempt_ids: set[str] | None = None,
) -> None:
    """Atomically publish one pending or delivered interactive Stop batch."""

    registered = set(delivered_attempt_ids if attempt_ids is None else attempt_ids)
    if not registered:
        raise JoinContractError("parent-session-state-contract-invalid")
    with _parent_session_state_lock(path):
        _write_parent_session_state_unlocked(
            path,
            parent_session_id,
            set(delivered_attempt_ids),
            registered,
        )


def _read_parent_session_state_unlocked(
    path: Path,
    parent_session_id: str,
) -> ParentSessionState | None:
    if not path.is_absolute() or not _safe_identity(parent_session_id):
        return None
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    expected_digest = hashlib.sha256(parent_session_id.encode("utf-8")).hexdigest()
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PARENT_SESSION_STATE_SCHEMA_VERSION
        or value.get("parent_session_id_sha256") != expected_digest
    ):
        return None
    raw_delivered = value.get("delivered_attempt_ids")
    raw_attempts = value.get("attempt_ids", raw_delivered)
    if (
        not isinstance(raw_attempts, list)
        or not isinstance(raw_delivered, list)
        or not raw_attempts
        or len(raw_attempts) > MAX_BATCH_ATTEMPTS
        or len(raw_delivered) > MAX_BATCH_ATTEMPTS
    ):
        return None
    parsed: list[set[str]] = []
    for raw in (raw_attempts, raw_delivered):
        values: set[str] = set()
        for attempt in raw:
            if (
                not isinstance(attempt, str)
                or not _safe_identity(attempt)
                or attempt in values
            ):
                return None
            values.add(attempt)
        parsed.append(values)
    attempts, delivered = parsed
    expected_phase = (
        "delivered"
        if delivered == attempts
        else "pending"
        if not delivered
        else "invalid"
    )
    if (
        not delivered.issubset(attempts)
        or value.get("phase", expected_phase) != expected_phase
    ):
        return None
    return ParentSessionState(frozenset(attempts), frozenset(delivered))


def read_parent_session_batch_state(
    path: Path,
    parent_session_id: str,
) -> ParentSessionState | None:
    """Return the exact registered and delivered sets for one native Stop batch."""

    return _read_parent_session_state_unlocked(path, parent_session_id)


def read_parent_session_state(
    path: Path,
    parent_session_id: str,
) -> set[str] | None:
    """Return delivered attempts, or None for missing/foreign/invalid state."""

    state = read_parent_session_batch_state(path, parent_session_id)
    return set(state.delivered_attempt_ids) if state is not None else None


def register_parent_session_attempt(
    path: Path,
    parent_session_id: str,
    attempt_id: str,
) -> None:
    """Bind a newly spawned direct child before the parent reaches Stop."""

    if not _safe_identity(attempt_id):
        raise JoinContractError("parent-session-state-contract-invalid")
    with _parent_session_state_lock(path):
        state = _read_parent_session_state_unlocked(path, parent_session_id)
        if state is not None and not state.delivered_attempt_ids:
            attempts = set(state.attempt_ids)
            attempts.add(attempt_id)
        else:
            attempts = {attempt_id}
        _write_parent_session_state_unlocked(
            path,
            parent_session_id,
            set(),
            attempts,
        )


def consume_parent_session_attempt(
    path: Path,
    parent_session_id: str,
    attempt_id: str,
    *,
    before_consume: Callable[[], bool] | None = None,
    allow_pending: bool = False,
) -> bool:
    """Consume one exact receipt after a successful typed harvest.

    ``allow_pending`` exists only for migration from the retired native Stop
    bridge. Its caller must already have proved the exact attempt terminal.
    """

    if not _safe_identity(attempt_id):
        return False
    with _parent_session_state_lock(path):
        state = _read_parent_session_state_unlocked(path, parent_session_id)
        if state is None or attempt_id not in state.attempt_ids:
            return False
        if not allow_pending and attempt_id not in state.delivered_attempt_ids:
            return False
        if before_consume is not None and not before_consume():
            raise JoinContractError("parent-session-consume-commit-failed")
        attempts = set(state.attempt_ids)
        delivered = set(state.delivered_attempt_ids)
        attempts.remove(attempt_id)
        delivered.discard(attempt_id)
        if attempts:
            _write_parent_session_state_unlocked(
                path,
                parent_session_id,
                delivered,
                attempts,
            )
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise JoinContractError("parent-session-state-unwritable") from exc
        return True


def remove_parent_session_state(path: Path | None) -> None:
    remove_supervisor_state(path)


def _local_contract_path(base: Path, raw: str, relative: str) -> bool:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    roots = [ROOT]
    agent_home = os.environ.get("AGENT_HOME")
    if agent_home and (Path(agent_home) / "core" / "CORE.md").is_file():
        roots.append(Path(agent_home))
    resolved_base = base.resolve()
    for parent in (resolved_base, *resolved_base.parents):
        if (parent / "core" / "CORE.md").is_file():
            roots.append(parent)
            break
    return any(resolved == (root / relative).resolve() for root in roots)


def _parse_long_options(
    tokens: list[str],
    valued: set[str],
    switches: set[str],
) -> dict[str, list[str]] | None:
    parsed: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in switches:
            parsed.setdefault(token, []).append("1")
            index += 1
            continue
        if token.startswith("--") and "=" in token:
            option, option_value = token.split("=", 1)
            if option not in valued or not option_value:
                return None
            parsed.setdefault(option, []).append(option_value)
            index += 1
            continue
        if token not in valued or index + 1 >= len(tokens):
            return None
        parsed.setdefault(token, []).append(tokens[index + 1])
        index += 2
    return parsed


def _resolved_from(base: Path, raw: str) -> Path | None:
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _selected_long_options(
    tokens: list[str], selected: set[str]
) -> dict[str, list[str]] | None:
    """Read selected opaque adapter options without accepting missing values."""

    values: dict[str, list[str]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        matched = next(
            (option for option in selected if token.startswith(option + "=")),
            None,
        )
        if matched is not None:
            value = token[len(matched) + 1 :]
            if not value:
                return None
            values.setdefault(matched, []).append(value)
            index += 1
            continue
        if token in selected:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                return None
            values.setdefault(token, []).append(tokens[index + 1])
            index += 2
            continue
        index += 1
    return values


def _strict_supervisor_binding_requested(
    *,
    jobs: Path | None,
    parent_attempt_id: str,
    route_file: Path | None,
    route_id: str,
) -> bool:
    return bool(
        jobs
        or parent_attempt_id
        or route_file
        or route_id
        or os.environ.get("AGENT_DISPATCH_COMPLETION_MODE") == "supervised"
    )


def _supervised_dispatch_context(
    *,
    jobs: Path | None,
    parent_attempt_id: str,
    route_file: Path | None,
    route_id: str,
    open_attempt_ids: set[str],
) -> SupervisedDispatchContext:
    """Resolve the exact owner route/registry tuple or fail closed."""

    raw_jobs = jobs or (
        Path(os.environ["AGENT_DISPATCH_JOBS"])
        if os.environ.get("AGENT_DISPATCH_JOBS")
        else None
    )
    raw_route = route_file or (
        Path(os.environ["AGENT_ROUTE_FILE"])
        if os.environ.get("AGENT_ROUTE_FILE")
        else None
    )
    expected_parent = parent_attempt_id or os.environ.get(
        "AGENT_DISPATCH_ATTEMPT_ID", ""
    )
    expected_route_id = route_id or os.environ.get("AGENT_ROUTE_ID", "")
    if (
        raw_jobs is None
        or raw_route is None
        or not raw_jobs.is_absolute()
        or not raw_route.is_absolute()
        or not expected_parent
        or not expected_route_id
    ):
        raise JoinContractError("supervisor-dispatch-binding-missing")
    try:
        canonical_jobs = raw_jobs.resolve()
        canonical_route = raw_route.resolve()
        route = json.loads(canonical_route.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JoinContractError("supervisor-dispatch-binding-unreadable") from exc
    if not isinstance(route, dict) or route.get("route_id") != expected_route_id:
        raise JoinContractError("supervisor-route-id-mismatch")
    rows = current_children(canonical_jobs, expected_parent)
    indexed = {row.attempt_id: row for row in rows}
    if not open_attempt_ids or not open_attempt_ids.issubset(indexed):
        raise JoinContractError("supervisor-open-attempt-binding-mismatch")
    return SupervisedDispatchContext(
        jobs=canonical_jobs,
        route_file=canonical_route,
        route_id=expected_route_id,
        parent_attempt_id=expected_parent,
        route=route,
        rows=tuple(rows),
    )


def _command_paths_match(
    *,
    base: Path,
    route_values: list[str],
    jobs_values: list[str],
    context: SupervisedDispatchContext,
) -> bool:
    return (
        len(route_values) == 1
        and len(jobs_values) == 1
        and _resolved_from(base, route_values[0]) == context.route_file
        and _resolved_from(base, jobs_values[0]) == context.jobs
    )


def _row_matches_current_route(
    row: ChildRow, context: SupervisedDispatchContext
) -> bool:
    route_path = _resolved_from(
        Path(row.metadata.get("worktree", "/")),
        row.metadata.get("route_file", ""),
    )
    return (
        row.metadata.get("parent_attempt_id") == context.parent_attempt_id
        and row.metadata.get("route_id") == context.route_id
        and route_path == context.route_file
    )


def _declared_parallel_nodes(
    route: dict[str, object], group: str
) -> tuple[dict[str, dict[str, object]], bool] | None:
    """Return a bounded group and whether it uses the canonical v2 axis."""

    raw_nodes = route.get("nodes")
    if not isinstance(raw_nodes, list):
        return None
    nodes = {
        str(node.get("id")): node
        for node in raw_nodes
        if isinstance(node, dict)
        and (node.get("parallel_group") or node.get("replica_group")) == group
    }
    if (
        not 2 <= len(nodes) <= 4
        or "" in nodes
        or any(node.get("dispatch_depth") != 2 for node in nodes.values())
    ):
        return None
    canonical = any(node.get("parallel_group") == group for node in nodes.values())
    if canonical and any(node.get("parallel_group") != group for node in nodes.values()):
        return None
    return nodes, canonical


def _parallel_row_matches(
    row: ChildRow,
    *,
    context: SupervisedDispatchContext,
    group: str,
    node_ids: set[str],
    declared_size: int,
    canonical: bool,
) -> bool:
    metadata = row.metadata
    node = metadata.get("route_node", "")
    row_group = metadata.get("parallel_group") or metadata.get("replica_group")
    expected_kind = "parallel-batch" if canonical else "replica-batch"
    return (
        _row_matches_current_route(row, context)
        and node in node_ids
        and row_group == group
        and (not canonical or metadata.get("parallel_group") == group)
        and metadata.get("reservation_kind") == expected_kind
        and metadata.get("batch_declared_size") == str(declared_size)
        and metadata.get("batch_group") == group
        and metadata.get("batch_route_id") == context.route_id
        and metadata.get("batch_parent_attempt_id") == context.parent_attempt_id
        and metadata.get("batch_attempt_id") == row.attempt_id
        and metadata.get("batch_route_node") == node
    )


def _bound_batch_start(
    *,
    base: Path,
    options: dict[str, list[str]],
    group: str,
    open_attempt_ids: set[str],
    context: SupervisedDispatchContext,
) -> bool:
    if not _command_paths_match(
        base=base,
        route_values=options.get("--route", []),
        jobs_values=options.get("--jobs", []),
        context=context,
    ):
        return False
    declaration = _declared_parallel_nodes(context.route, group)
    if declaration is None:
        return False
    declared, canonical = declaration
    node_ids = set(declared)
    declared_size = len(node_ids)
    pending_rows = [
        row for row in context.rows if row.attempt_id in open_attempt_ids
    ]
    if not pending_rows or any(
        not _parallel_row_matches(
            row,
            context=context,
            group=group,
            node_ids=node_ids,
            declared_size=declared_size,
            canonical=canonical,
        )
        for row in pending_rows
    ):
        return False

    route_group_rows = [
        row
        for row in context.rows
        if row.metadata.get("route_node") in node_ids
        or row.metadata.get("parallel_group") == group
        or row.metadata.get("replica_group") == group
        or row.metadata.get("batch_group") == group
    ]
    exact_rows = [
        row
        for row in route_group_rows
        if _parallel_row_matches(
            row,
            context=context,
            group=group,
            node_ids=node_ids,
            declared_size=declared_size,
            canonical=canonical,
        )
    ]
    # A parked recovery may fill exactly one missing leg. Fewer than N-1 rows
    # would create a partial new batch; N rows means the group was already
    # claimed. Duplicate/malformed rows cannot authorize recovery either.
    return (
        len(route_group_rows) == declared_size - 1
        and len(exact_rows) == declared_size - 1
        and len({row.metadata.get("route_node") for row in exact_rows})
        == declared_size - 1
    )


def _bound_dispatch_node_start(
    *,
    base: Path,
    options: dict[str, list[str]],
    trailing: list[str],
    open_attempt_ids: set[str],
    context: SupervisedDispatchContext,
) -> bool:
    selected = _selected_long_options(
        trailing,
        {
            "--jobs",
            "--parent-attempt-id",
            "--route-file",
            "--route-id",
            "--route-node",
            "--dispatch-depth",
            "--parent",
        },
    )
    if selected is None or any(
        option in selected
        for option in {
            "--route-file",
            "--route-id",
            "--route-node",
            "--dispatch-depth",
            "--parent",
        }
    ):
        return False
    if not _command_paths_match(
        base=base,
        route_values=options.get("--route", []),
        jobs_values=selected.get("--jobs", []),
        context=context,
    ):
        return False
    explicit_parent = selected.get("--parent-attempt-id", [])
    if len(explicit_parent) > 1 or (
        explicit_parent and explicit_parent != [context.parent_attempt_id]
    ):
        return False
    raw_nodes = context.route.get("nodes")
    if not isinstance(raw_nodes, list):
        return False
    node_id = options["--node"][0]
    matches = [
        node
        for node in raw_nodes
        if isinstance(node, dict) and node.get("id") == node_id
    ]
    if (
        len(matches) != 1
        or matches[0].get("dispatch_depth") != 2
        or matches[0].get("parallel_group")
        or matches[0].get("replica_group")
    ):
        return False
    pending_rows = [
        row for row in context.rows if row.attempt_id in open_attempt_ids
    ]
    if not pending_rows or any(
        not _row_matches_current_route(row, context) for row in pending_rows
    ):
        return False
    return not any(row.metadata.get("route_node") == node_id for row in context.rows)


def supervisor_outbox_delivery_identity(
    outbox: SupervisorOutbox | None,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Pure identity of a delivered outbox, for the D2b unchanged-delivery test.

    No I/O, no mutation -- both supervisors compare this tuple across passes
    to decide whether a redelivery is identical to the last one.
    """

    if outbox is None:
        return ("", ())
    return (outbox.receipt_digest, outbox.row_revisions)


def supervisor_receipt_satisfiable(
    command_lines: list[str],
    *,
    base: Path,
    open_attempt_ids: set[str],
    parent_slug: str,
    jobs: Path | None = None,
    parent_attempt_id: str = "",
    route_file: Path | None = None,
    route_id: str = "",
) -> tuple[bool, str]:
    """D2a: prove a receipt satisfiable BEFORE delivering it, or name why not.

    Runs every command line the supervisor is about to prescribe through the
    real ``classify_supervised_shell_command`` -- the exact guard the park
    hook enforces -- with the same guarded-attempt set. Pure: no I/O beyond
    what the classifier itself performs (none, on this call shape), no
    mutation. The caller must derive ``open_attempt_ids`` exactly as
    ``hooks/registered-parent-park.py``'s ``guarded_attempts`` does (D-6c) --
    a precheck computing that set differently from the hook would report
    satisfiable for a command the guard then denies, which is worse than no
    precheck at all.
    """

    for line in command_lines:
        action = classify_supervised_shell_command(
            base=base,
            command=line,
            open_attempt_ids=open_attempt_ids,
            parent_slug=parent_slug,
            jobs=jobs,
            parent_attempt_id=parent_attempt_id,
            route_file=route_file,
            route_id=route_id,
        )
        if action is None:
            return False, f"unrecognized-or-unadmitted-command:{line}"
    return True, ""


def classify_supervised_shell_command(
    *,
    base: Path,
    command: str,
    open_attempt_ids: set[str],
    parent_slug: str,
    jobs: Path | None = None,
    parent_attempt_id: str = "",
    route_file: Path | None = None,
    route_id: str = "",
) -> SupervisorShellAction | None:
    """Recognize only exact harvest or one additional parent-bound dispatch."""

    if not command or not open_attempt_ids or re_search_shell_composition(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    if (
        len(tokens) >= 2
        and _local_contract_path(base, tokens[0], "adapters/codex/bin/preflight.sh")
        and tokens[1] == "harvest"
    ):
        options = _parse_long_options(
            tokens[2:],
            {"--attempt-id", "--status", "--completion", "--jobs"},
            {"--mark-done", "--keep-home", "--failure-detail"},
        )
        if options is None or len(options.get("--attempt-id", [])) != 1:
            return None
        if any(len(values) != 1 for values in options.values()):
            return None
        supplied_jobs = options.get("--jobs", [])
        if supplied_jobs and jobs is not None:
            if Path(supplied_jobs[0]).resolve(strict=False) != Path(jobs).resolve(strict=False):
                # A harvest pointed at another registry is not the prescribed
                # command, even though it otherwise parses.
                return None
        attempt = options["--attempt-id"][0]
        status = options.get("--status", ["open"])[0]
        if attempt not in open_attempt_ids or status not in {"open", "done", "all"}:
            return None
        return SupervisorShellAction(
            "harvest",
            attempt,
            status,
            "--mark-done" in options,
            "--failure-detail" in options,
        )

    strict_binding = _strict_supervisor_binding_requested(
        jobs=jobs,
        parent_attempt_id=parent_attempt_id,
        route_file=route_file,
        route_id=route_id,
    )
    context: SupervisedDispatchContext | None = None
    if strict_binding:
        try:
            context = _supervised_dispatch_context(
                jobs=jobs,
                parent_attempt_id=parent_attempt_id,
                route_file=route_file,
                route_id=route_id,
                open_attempt_ids=open_attempt_ids,
            )
        except JoinContractError:
            return None

    dispatch_tokens = tokens
    if tokens[0] in {"python", "python3"}:
        if len(tokens) < 2:
            return None
        dispatch_tokens = tokens[1:]
    is_batch = False
    if _local_contract_path(base, dispatch_tokens[0], "adapters/codex/bin/preflight.sh"):
        if len(dispatch_tokens) < 2 or dispatch_tokens[1] != "dispatch-batch":
            return None
        dispatch_tokens = [str(ROOT / "utilities" / "dispatch-batch.py"), *dispatch_tokens[2:]]
        is_batch = True
    elif _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-batch.py"):
        is_batch = True
    elif not _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-node.py"):
        return None

    if is_batch:
        options = _parse_long_options(
            dispatch_tokens[1:],
            {
                "--route",
                "--parallel-group",
                "--replica-group",
                "--action",
                "--slug-prefix",
                "--parent",
                "--qa",
                "--jobs",
                "--prompt-text",
            },
            {"--allow-degraded-independence"},
        )
        required = {
            "--route",
            "--action",
            "--slug-prefix",
            "--parent",
        }
        canonical_groups = options.get("--parallel-group", []) if options else []
        legacy_groups = options.get("--replica-group", []) if options else []
        group_values = canonical_groups or legacy_groups
        aliases_match = not (
            canonical_groups
            and legacy_groups
            and canonical_groups != legacy_groups
        )
        if (
            options is None
            or not parent_slug
            or any(len(values) != 1 for values in options.values())
            or not required.issubset(options)
            or len(group_values) != 1
            or not aliases_match
            or options["--action"] != ["start"]
            or options["--parent"] != [parent_slug]
        ):
            return None
        group = group_values[0]
        if context is not None and not _bound_batch_start(
            base=base,
            options=options,
            group=group,
            open_attempt_ids=open_attempt_ids,
            context=context,
        ):
            return None
        return SupervisorShellAction("dispatch-batch")

    try:
        separator = dispatch_tokens.index("--")
    except ValueError:
        separator = len(dispatch_tokens)
    options = _parse_long_options(
        dispatch_tokens[1:separator],
        {
            "--route",
            "--node",
            "--adapter",
            "--action",
            "--slug",
            "--qa",
            "--parent",
            "--prompt-text",
        },
        set(),
    )
    required = {"--route", "--node", "--adapter", "--action", "--slug", "--parent"}
    if (
        options is None
        or not parent_slug
        or any(len(values) != 1 for values in options.values())
        or not required.issubset(options)
        or options["--action"] != ["start"]
        or options["--parent"] != [parent_slug]
        or options["--adapter"][0] not in {"claude", "codex", "opencode"}
    ):
        return None
    if context is not None and not _bound_dispatch_node_start(
        base=base,
        options=options,
        trailing=dispatch_tokens[separator + 1 :] if separator < len(dispatch_tokens) else [],
        open_attempt_ids=open_attempt_ids,
        context=context,
    ):
        return None
    return SupervisorShellAction("dispatch")


def classify_supervised_shell_command_reason(
    *,
    base: Path,
    command: str,
    open_attempt_ids: set[str],
    parent_slug: str,
    jobs: Path | None = None,
    parent_attempt_id: str = "",
    route_file: Path | None = None,
    route_id: str = "",
) -> str:
    """Typed reason a Bash command failed ``classify_supervised_shell_command``.

    A parallel accessor, not a change to the existing function's return type
    (the frozen ``SupervisorShellAction`` is consumed by the hook and every
    existing fixture). Re-parses the command on the denial path only -- never
    called for an admitted command. One of ``unrecognized-surface``,
    ``unknown-option``, ``shell-composition``, ``attempt-not-guarded``.
    """

    if not command or re_search_shell_composition(command):
        return "shell-composition"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "shell-composition"
    if not tokens:
        return "unrecognized-surface"

    if (
        len(tokens) >= 2
        and _local_contract_path(base, tokens[0], "adapters/codex/bin/preflight.sh")
        and tokens[1] == "harvest"
    ):
        options = _parse_long_options(
            tokens[2:],
            {"--attempt-id", "--status", "--completion", "--jobs"},
            {"--mark-done", "--keep-home", "--failure-detail"},
        )
        if (
            options is None
            or len(options.get("--attempt-id", [])) != 1
            or any(len(values) != 1 for values in options.values())
        ):
            return "unknown-option"
        attempt = options["--attempt-id"][0]
        if attempt not in open_attempt_ids:
            return "attempt-not-guarded"
        status = options.get("--status", ["open"])[0]
        supplied_jobs = options.get("--jobs", [])
        if status not in {"open", "done", "all"} or (
            supplied_jobs
            and jobs is not None
            and Path(supplied_jobs[0]).resolve(strict=False)
            != Path(jobs).resolve(strict=False)
        ):
            return "unknown-option"
        return ""

    dispatch_tokens = tokens
    if tokens[0] in {"python", "python3"}:
        if len(tokens) < 2:
            return "unrecognized-surface"
        dispatch_tokens = tokens[1:]
    recognized_surface = (
        _local_contract_path(base, dispatch_tokens[0], "adapters/codex/bin/preflight.sh")
        or _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-batch.py")
        or _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-node.py")
    )
    if not recognized_surface:
        return "unrecognized-surface"
    # A recognized launcher surface with anything else wrong (bad option,
    # wrong action, unbound route/group, attempt outside the guarded set for
    # the exact-batch/exact-node checks) -- classify_supervised_shell_command
    # itself is the source of truth for admission; this accessor only names
    # the coarse category once that function has already said no.
    return "unknown-option"


def re_search_shell_composition(command: str) -> bool:
    return (
        any(char in command for char in "\n\r;&|<>")
        or chr(96) in command
        or "$(" in command
    )


def _metadata(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        values[key] = value
    return values


def current_children(
    jobs: Path,
    parent_attempt_id: str,
    expected_attempts: set[str] | None = None,
) -> list[ChildRow]:
    """Return latest exact-attempt rows owned by ``parent_attempt_id``.

    Foreign parents, legacy slug-only rows, and same-slug retries are ignored.
    A matching v2 row that lacks its attempt identity fails closed.
    """

    if not parent_attempt_id:
        raise JoinContractError("parent-attempt-id-missing")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = _metadata(fields[5])
        if meta.get("parent_attempt_id") != parent_attempt_id:
            continue
        if meta.get("attempt_schema_version") != "2":
            raise JoinContractError("owned-row-schema-invalid")
        attempt_id = meta.get("attempt_id", "")
        if not attempt_id:
            raise JoinContractError("owned-row-attempt-id-missing")
        if expected_attempts is not None and attempt_id not in expected_attempts:
            continue
        latest[attempt_id] = ChildRow(
            order=order,
            status=fields[1],
            slug=fields[4],
            attempt_id=attempt_id,
            raw=line,
            metadata=meta,
        )

    if expected_attempts is not None:
        missing = expected_attempts.difference(latest)
        if missing:
            raise JoinContractError("expected-attempt-missing")
    return sorted(latest.values(), key=lambda row: row.order)


def exact_attempt_row(jobs: Path, attempt_id: str) -> ChildRow:
    """Read one unique current registry row by immutable attempt identity."""

    if not attempt_id:
        raise JoinContractError("attempt-id-missing")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc
    matches: list[ChildRow] = []
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = _metadata(fields[5])
        if metadata.get("attempt_id") != attempt_id:
            continue
        if metadata.get("attempt_schema_version") != "2":
            raise JoinContractError("attempt-row-schema-invalid")
        matches.append(
            ChildRow(
                order=order,
                status=fields[1],
                slug=fields[4],
                attempt_id=attempt_id,
                raw=line,
                metadata=metadata,
            )
        )
    if len(matches) != 1:
        raise JoinContractError("attempt-row-not-unique")
    return matches[0]


def current_attempt_row(jobs: Path, attempt_id: str) -> ChildRow | None:
    """Return the latest exact registry row for one attempt identity."""

    if not _safe_identity(attempt_id):
        raise JoinContractError("attempt-id-invalid")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc
    found: ChildRow | None = None
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = _metadata(fields[5])
        if metadata.get("attempt_id") != attempt_id:
            continue
        if metadata.get("attempt_schema_version") != "2":
            raise JoinContractError("attempt-row-schema-invalid")
        found = ChildRow(order, fields[1], fields[4], attempt_id, line, metadata)
    return found


def current_delivery_state(
    jobs: Path,
    attempt_id: str,
    *,
    parent_attempt_id: str,
    advance: bool = True,
) -> CurrentDeliveryState:
    """Construct one current marker/row/owned-child delivery decision.

    The potentially expensive process and descendant observation happens
    before the registry lock.  The contract transaction then proves that both
    the exact row revision and its process identity still match before it may
    advance a marker-bound open row.
    """

    snapshot = current_attempt_row(jobs, attempt_id)
    if snapshot is None:
        expected_revision = ""
        expected_process_identity: tuple[tuple[str, str], ...] = ()
        process = ProcessQuiescence("unverifiable", "attempt-row-missing")
    else:
        expected_revision = child_row_revision(snapshot)
        expected_process_identity = marker_bound_process_identity(snapshot.metadata)
        process = attempt_process_quiescence(
            snapshot.metadata,
            terminal_receipt=(
                snapshot.status == "done"
                or bool(snapshot.metadata.get("completion_marker"))
            ),
        )
    result = marker_bound_delivery_transaction(
        jobs,
        attempt_id,
        parent_attempt_id=parent_attempt_id,
        expected_row_revision=expected_revision,
        expected_process_identity=expected_process_identity,
        process_observation=process,
        advance=advance,
    )
    if result.advanced:
        # SD-111 P2 trigger 1: the registry lock already released above.
        materialize_after_terminal_close(jobs, attempt_id)
    return CurrentDeliveryState(
        marker=result.marker,
        marker_digest=result.marker_digest,
        row_revision=result.row_revision,
        row_digest=result.row_digest,
        status=result.status,
        verdict=result.verdict,
        quiescent=result.quiescent,
        owned_children=result.owned_children,
        advanced=result.advanced,
        supervisor_terminal=result.supervisor_terminal,
    )


def current_session_children(
    jobs: Path,
    parent_session_id: str,
    expected_attempts: set[str] | None = None,
    parent_completion_delivery: str = SESSION_PARENT_DELIVERY,
) -> list[ChildRow]:
    """Return exact direct children owned by one interactive Codex session.

    Only rows explicitly stamped for the selected parent-runtime delivery
    surface qualify. Unmarked rows remain on the disclosed polling fallback.
    The initial lookup selects current open/running rows; an expected snapshot
    continues to follow those same attempts after their status changes.
    """

    if not parent_session_id:
        raise JoinContractError("parent-session-id-missing")
    if parent_completion_delivery not in SESSION_PARENT_DELIVERIES:
        raise JoinContractError("parent-completion-delivery-invalid")
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    for order, line in enumerate(lines):
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = _metadata(fields[5])
        if (
            meta.get("parent_sid") != parent_session_id
            or meta.get("parent_completion_delivery")
            != parent_completion_delivery
        ):
            continue
        if (
            meta.get("launch_claimed") == "0"
            or meta.get("parent_completion_harvested") == "1"
        ):
            continue
        if (
            meta.get("attempt_schema_version") != "2"
            or meta.get("dispatch_depth") != "1"
            or meta.get("execution_surface") != "registered-headless"
            or meta.get("registered_worker") != "1"
            or meta.get("launch_claimed") != "1"
        ):
            raise JoinContractError("owned-session-row-contract-invalid")
        attempt_id = meta.get("attempt_id", "")
        if not attempt_id:
            raise JoinContractError("owned-session-row-attempt-id-missing")
        if expected_attempts is not None and attempt_id not in expected_attempts:
            continue
        latest[attempt_id] = ChildRow(
            order=order,
            status=fields[1],
            slug=fields[4],
            attempt_id=attempt_id,
            raw=line,
            metadata=meta,
        )

    if len(latest) > MAX_BATCH_ATTEMPTS:
        raise JoinContractError("owned-session-batch-oversized")
    if expected_attempts is not None:
        missing = expected_attempts.difference(latest)
        if missing:
            raise JoinContractError("expected-attempt-missing")
    rows = sorted(latest.values(), key=lambda row: row.order)
    if expected_attempts is None:
        rows = [row for row in rows if row.status in OPEN_STATES]
    return rows


def pending_attempt_ids(rows: list[ChildRow]) -> set[str]:
    """Return children that are open or terminal-but-not-yet-quiescent."""

    pending: set[str] = set()
    for row in rows:
        observed = observed_attempt_liveness(
            row.status,
            row.metadata,
            terminal_envelope=terminal_envelope_observed(
                row.metadata.get("log_file")
            ),
        )
        if observed.state in {"alive", "unverifiable"}:
            pending.add(row.attempt_id)
    return pending


def supervisor_guarded_attempt_ids(
    rows: list[ChildRow],
    outbox: "SupervisorOutbox | None",
) -> set[str]:
    """The exact attempt set ``hooks/registered-parent-park.py`` guards on.

    One derivation, two callers: the park hook enforces it, and a supervisor
    D2a precheck (``supervisor_receipt_satisfiable``) must answer with the
    same set or it can report a command satisfiable that the guard then
    denies -- worse than no precheck at all (plan SS3.4 D2a, fixture D-6c).
    Reads log files through ``pending_attempt_ids``; mutates nothing.
    """

    guarded = {row.attempt_id for row in rows if row.status in {"open", "running"}}
    guarded.update(pending_attempt_ids(rows))
    if outbox is not None:
        guarded.update(
            set(outbox.attempt_ids).difference(outbox.consumed_attempt_ids)
        )
    return guarded


def _liveness_state(
    row: ChildRow,
    command: list[str],
    env: dict[str, str],
    timeout: float = 30.0,
) -> str:
    """Return ``alive`` or ``terminal`` without exposing liveness output."""

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        fields = row.raw.split("\t")
        if len(fields) == 6 and fields[1] == "running":
            fields[1] = "open"
        handle.write("\t".join(fields) + "\n")
        registry = Path(handle.name)
    try:
        result = subprocess.run(
            [*command, str(registry)],
            env={**env, "AGENT_DISPATCH_JOBS": str(registry)},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.1, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JoinContractError("liveness-contract-unavailable") from exc
    finally:
        try:
            registry.unlink()
        except OSError:
            pass
    if result.returncode == 0:
        return "alive"
    if result.returncode == 3:
        return "terminal"
    raise JoinContractError("liveness-contract-failed")


def _join_snapshot(
    *,
    initial: list[ChildRow],
    refresh: Callable[[set[str]], list[ChildRow]],
    identity: dict[str, str],
    interval: float,
    timeout: float,
    liveness_command: list[str] | None,
    liveness_probe_timeout: float,
    env: dict[str, str] | None,
) -> dict[str, object]:
    """Join one immutable exact-attempt snapshot."""

    command = liveness_command or [str(ROOT / "utilities" / "dispatch-liveness.sh")]
    runtime_env = dict(os.environ if env is None else env)
    interval = max(0.05, interval)
    timeout = max(0.0, timeout)
    if not initial:
        return {
            "schema_version": SCHEMA_VERSION,
            "state": "no-children",
            **identity,
            "children": [],
        }
    snapshot = {row.attempt_id for row in initial}
    started = time.monotonic()

    while True:
        rows = refresh(snapshot)
        children: list[dict[str, str]] = []
        pending = False
        for row in rows:
            observed = observed_attempt_liveness(
                row.status,
                row.metadata,
                terminal_envelope=terminal_envelope_observed(
                    row.metadata.get("log_file")
                ),
                # A join is itself a terminal gate.  Namespace-local workers
                # must therefore carry the wrapper-issued portable post-exit
                # receipt before any liveness fallback may make them ready,
                # even when their final runtime envelope is not visible yet.
                terminal_receipt_gate=True,
            )
            if row.status == "done":
                if observed.state == "terminal":
                    readiness, reason = "ready", "registry-closed"
                else:
                    readiness = "pending"
                    reason = (
                        "process-alive"
                        if observed.state == "alive"
                        else "process-unverifiable"
                    )
                    pending = True
            elif row.status in OPEN_STATES:
                if observed.state == "alive":
                    readiness, reason = "pending", "process-alive"
                    pending = True
                elif observed.state == "reconcile-needed":
                    readiness, reason = "ready", "terminal-observed"
                elif observed.process_reason == "post-exit-receipt-incomplete":
                    # A namespace-local fallback probe cannot replace the
                    # wrapper-issued portable receipt.
                    readiness, reason = "pending", "process-unverifiable"
                    pending = True
                else:
                    probe = _liveness_state(
                        row, command, runtime_env, liveness_probe_timeout
                    )
                    if probe == "terminal":
                        readiness, reason = "ready", "terminal-observed"
                    else:
                        readiness = "pending"
                        reason = "process-unverifiable"
                        pending = True
            else:
                raise JoinContractError("owned-row-status-invalid")
            children.append(
                {
                    "attempt_id": row.attempt_id,
                    "slug": row.slug,
                    "status": row.status,
                    "readiness": readiness,
                    "reason": reason,
                    "required_action": required_action_for_attempt(
                        row.status, row.metadata
                    ),
                }
            )
        if not pending:
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "ready",
                **identity,
                "children": children,
                "delivery_timing": delivery_timing_fields(
                    last_child_terminal_ns=time.monotonic_ns()
                ),
            }
        if time.monotonic() - started >= timeout:
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "timeout",
                **identity,
                "children": children,
            }
        time.sleep(interval)


def join_batch(
    *,
    jobs: Path,
    parent_attempt_id: str,
    expected_attempts: set[str] | None = None,
    interval: float = 2.0,
    timeout: float = 3600.0,
    liveness_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Join one immutable child batch sealed to an exact parent attempt."""

    initial = current_children(jobs, parent_attempt_id, expected_attempts)
    return _join_snapshot(
        initial=initial,
        refresh=lambda snapshot: current_children(jobs, parent_attempt_id, snapshot),
        identity={"parent_attempt_id": parent_attempt_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=30.0,
        env=env,
    )


def join_session_batch(
    *,
    jobs: Path,
    parent_session_id: str,
    expected_attempts: set[str] | None = None,
    parent_completion_delivery: str = SESSION_PARENT_DELIVERY,
    interval: float = 2.0,
    timeout: float = 540.0,
    liveness_command: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    """Join one exact batch sealed to an interactive parent session."""

    initial = current_session_children(
        jobs,
        parent_session_id,
        expected_attempts,
        parent_completion_delivery,
    )
    return _join_snapshot(
        initial=initial,
        refresh=lambda snapshot: current_session_children(
            jobs,
            parent_session_id,
            snapshot,
            parent_completion_delivery,
        ),
        identity={"parent_session_id": parent_session_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=5.0,
        env=env,
    )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--jobs", default=os.environ.get("AGENT_DISPATCH_JOBS"))
    value.add_argument(
        "--parent-attempt-id",
        default=None,
    )
    value.add_argument("--parent-session-id")
    value.add_argument(
        "--parent-completion-delivery",
        choices=sorted(SESSION_PARENT_DELIVERIES),
        default=SESSION_PARENT_DELIVERY,
    )
    value.add_argument("--attempt-id", action="append", default=[])
    value.add_argument("--interval", type=float, default=2.0)
    value.add_argument("--timeout", type=float, default=3600.0)
    value.add_argument("--liveness-command")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.parent_attempt_id and not args.parent_session_id:
        args.parent_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    identity_name = (
        "parent_session_id" if args.parent_session_id else "parent_attempt_id"
    )
    identity_value = args.parent_session_id or args.parent_attempt_id or "-"
    if not args.jobs:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "contract-error",
            identity_name: identity_value,
            "reason": "jobs-path-missing",
            "children": [],
        }
        print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
        return 69
    liveness = [args.liveness_command] if args.liveness_command else None
    try:
        if bool(args.parent_attempt_id) == bool(args.parent_session_id):
            raise JoinContractError("parent-identity-ambiguous")
        if args.parent_session_id:
            receipt = join_session_batch(
                jobs=Path(args.jobs),
                parent_session_id=args.parent_session_id,
                expected_attempts=(
                    set(args.attempt_id) if args.attempt_id else None
                ),
                parent_completion_delivery=args.parent_completion_delivery,
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
            )
        else:
            receipt = join_batch(
                jobs=Path(args.jobs),
                parent_attempt_id=args.parent_attempt_id or "",
                expected_attempts=(
                    set(args.attempt_id) if args.attempt_id else None
                ),
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
            )
    except JoinContractError as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "contract-error",
            identity_name: identity_value,
            "reason": str(exc),
            "children": [],
        }
    except Exception as exc:  # noqa: BLE001 - protocol boundary, see below
        # The join receipt on stdout is this process's only protocol. An
        # unexpected exception used to escape here, so the caller saw a
        # traceback on a discarded stderr and an EMPTY stdout, which
        # `claude-session-supervisor.py` / `codex-app-server-supervisor.py`
        # can only classify as `join-receipt-json-invalid`. Fail closed with a
        # typed receipt instead: the exception class name is diagnostic
        # enough and, unlike `str(exc)`, can never carry raw child bytes.
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": "contract-error",
            identity_name: identity_value,
            "reason": "join-internal-error-" + type(exc).__name__,
            "children": [],
        }
    print(json.dumps(receipt, separators=(",", ":"), sort_keys=True))
    return {"ready": 0, "no-children": 2, "timeout": 3}.get(str(receipt["state"]), 69)


def _row_worktree(row: ChildRow) -> str:
    fields = str(getattr(row, "raw", "")).split("\t")
    return fields[3] if len(fields) == 6 else ""


def stage_advance_receipt_block(record: dict[str, object]) -> dict[str, object]:
    """Render the closed `stage_advance` v3 receipt block from a durable
    `stage_advance_record_v1` (`dispatch_stage_advance.stage_advance_record`,
    13.32.1-(3)B). Never includes `phases` -- that key is transaction-internal
    bookkeeping, not part of the receipt contract."""

    return {field: record.get(field) for field in STAGE_ADVANCE_RECEIPT_FIELDS}


def receipt_with_stage_advance(
    receipt: dict[str, object],
    *,
    stage_advance_record: dict[str, object] | None = None,
) -> dict[str, object]:
    """The only place a delivery receipt's `schema_version` becomes 3.

    A delivery with no stage-advance attempt, or a refused stage-advance
    outcome, returns the identical `receipt` object unmodified. Refusal must
    stay byte-identical to the pre-SD-110 receipt (A-19, 13.32.1-(4): "모든
    거부는... 기존 delivery 경로를 그대로 수행한다") -- that invariant is enforced
    here rather than left to each caller to remember, so only an
    `outcome == "advanced"` record can ever change the receipt a consumer
    sees.

    There is deliberately no separate `negotiated` flag. `outcome ==
    "advanced"` is reachable ONLY if `dispatch_stage_advance.
    coordinate_stage_advance` already required `receipt_schema_negotiated ==
    3` from its caller -- it refuses `stage-advance-receipt-schema-
    unsupported` before ever producing a record otherwise. A second,
    independently-settable "negotiated" bool here could disagree with that
    fact (13.32.1-(3)B's forbidden state: advanced outcome, un-negotiated
    delivery); collapsing to one token makes that combination unrepresentable
    instead of merely "not currently produced".
    """

    if stage_advance_record is None or stage_advance_record.get("outcome") != "advanced":
        return receipt
    return {
        **receipt,
        "schema_version": STAGE_ADVANCE_SCHEMA_VERSION,
        STAGE_ADVANCE_RECEIPT_KEY: stage_advance_receipt_block(stage_advance_record),
    }


def typed_stage_advance_block(value: object) -> dict[str, object]:
    """Strict decode of a v3 receipt's `stage_advance` block. Raises
    `JoinContractError` on any shape deviation -- a negotiated consumer never
    passes an unvalidated block through to the model."""

    if not isinstance(value, dict) or set(value) != set(STAGE_ADVANCE_RECEIPT_FIELDS):
        raise JoinContractError("stage-advance-block-shape-invalid")
    if value.get("schema_version") != 1:
        raise JoinContractError("stage-advance-block-schema-invalid")
    if value.get("outcome") not in ("advanced", "refused"):
        raise JoinContractError("stage-advance-block-outcome-invalid")
    for flag in ("registered", "started", "child_spawned"):
        if not isinstance(value.get(flag), bool):
            raise JoinContractError("stage-advance-block-flags-invalid")
    return dict(value)


def route_completion_evidence(
    metadata: dict[str, str], *, worktree: str
) -> tuple[str | None, str]:
    """Derive a readable in-root artifact path from a route-bound child's own
    terminal envelope, or ``(None, reason)`` when it cannot legally complete.

    Shared by `close_finished_child` (supervisor path) and
    `dispatch-harvest.py --mark-done` (SD-70/78, round_1 finding 5) so both
    derive completion evidence through the identical fail-closed predicate:
    a valid envelope, a PASS verdict, and a readable in-root artifact. Never
    a raw untrusted path — the inspector hands back only a bounded url-safe
    base64 form.

    The reason distinguishes a bad base64 payload (`evidence-undecodable`)
    from an empty or non-absolute decoded path (`evidence-absent`) — both
    used to collapse into one bare `None`, which lost the distinction a
    reader debugging a stuck row needs (round_1 review advisory 4).
    """

    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=worktree,
        artifact_root_metadata=metadata.get("artifact_root"),
    )
    if terminal.get("state") != "valid":
        return None, "evidence-not-valid"
    if str(terminal.get("verdict")) != "PASS":
        return None, "evidence-not-pass"
    if terminal.get("artifact_state") != "readable":
        return None, "evidence-not-readable"
    encoded = str(terminal.get("artifact_path_b64") or "")
    try:
        artifact = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None, "evidence-undecodable"
    if not artifact or not Path(artifact).is_absolute():
        return None, "evidence-absent"
    return artifact, ""


def close_finished_child(row: ChildRow, *, jobs: str | Path) -> str:
    """Close one finished-but-open child from its own terminal evidence.

    A route-bound node may only be closed through the completion-marker path
    (OPERATIONS §5.10, SD-70); `dispatch-harvest --mark-done` refuses it with
    `route-completion-required`. The supervised-parent park hook in turn admits
    only that harvest command (`classify_supervised_shell_command`), so a
    supervised owner cannot execute the one command that would work: the model
    has no legal exit and the batch deadlocks until the owner is killed. A
    supervisor is not park-guarded and already owns the join outside the model
    loop, so it performs this closure itself.

    Completion is never invented — without a valid terminal envelope naming an
    in-root artifact the child stays open and the caller keeps its existing
    failure. Returns ``""`` on success, else a short skip reason.
    """

    metadata = getattr(row, "metadata", {}) or {}
    route_file = metadata.get("route_file")
    route_node = metadata.get("route_node")
    if not route_file or not route_node:
        return "not-route-bound"
    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=_row_worktree(row),
        artifact_root_metadata=metadata.get("artifact_root"),
    )
    if terminal.get("state") != "valid":
        # Carry the inspector's own enum: a bare `terminal-invalid` sends the
        # next reader back through the parser to learn whether the envelope was
        # missing, malformed, or a runtime error.  The enum is a fixed vocabulary,
        # never raw agent text.
        skip = "terminal-%s" % (terminal.get("state") or "absent")
        detail = terminal.get("reason")
        reason = f"{skip}:{detail}" if detail else skip
        if (
            str(terminal.get("state")) == "absent"
            and _close_invalid_envelope_child(
                row,
                jobs=jobs,
                reason="terminal-envelope-absent",
                note="dead-missing-result",
                classifier_source="completion-join-missing-result-v1",
                terminal_envelope=False,
            )
        ):
            return ""
        if str(terminal.get("state")) == "invalid" and _close_invalid_envelope_child(
            row, jobs=jobs, reason=reason
        ):
            return ""
        return reason
    verdict = str(terminal.get("verdict"))
    if verdict in ("BLOCKED", "FAIL"):
        # SD-72/SD-78 (round_1 finding 1): a valid terminal handoff carrying
        # BLOCKED or FAIL closes typed IMMEDIATELY on the verdict alone — this
        # must run before the artifact_state branch below, because the
        # inspector accepts a readable artifact independently of verdict
        # (codex_dispatch_terminal.py). Without this ordering a readable
        # BLOCKED/FAIL envelope would fall through to route completion and
        # manufacture a PASS for a worker that never passed. Route completion
        # is reachable only for a PASS verdict, never for BLOCKED/FAIL.
        note = "dead-worker-blocked" if verdict == "BLOCKED" else "dead-worker-fail"
        reason = "typed-%s" % verdict.lower()
        if _close_invalid_envelope_child(
            row,
            jobs=jobs,
            reason=reason,
            note=note,
            classifier_source="completion-join-terminal-verdict-v1",
        ):
            return ""
        return reason
    if terminal.get("artifact_state") != "readable":
        reason = "evidence-%s" % (terminal.get("artifact_state") or "absent")
        if verdict == "PASS" and _close_invalid_envelope_child(
            row, jobs=jobs, reason=reason
        ):
            return ""
        return reason
    artifact, evidence_reason = route_completion_evidence(
        metadata, worktree=_row_worktree(row)
    )
    if artifact is None:
        return evidence_reason
    command = [
        sys.executable,
        str(ROOT / "utilities" / "capability-route.py"),
        "complete",
        "--route", str(route_file),
        "--node", str(route_node),
        "--evidence", artifact,
        "--jobs", str(jobs),
        "--attempt-id", row.attempt_id,
    ]
    for flag, key in (
        ("--dispatch-depth", "dispatch_depth"),
        ("--transport", "transport"),
        ("--execution-surface", "execution_surface"),
        ("--registered-worker", "registered_worker"),
        ("--fallback-hop", "fallback_hop"),
    ):
        value = metadata.get(key)
        if value:
            command += [flag, str(value)]
    completion = run_route_completion(command)
    if completion:
        # `complete` is exact-attempt idempotent.  One bounded retry recovers
        # the marker-written/row-not-yet-closed publication window.
        completion = run_route_completion(command)
    return completion


def close_wrapper_pass(row: ChildRow, *, jobs: str | Path) -> str:
    """Complete one wrapper-reaped PASS or close a typed contract failure."""

    reason = close_finished_child(row, jobs=jobs)
    if not reason:
        return ""
    try:
        current = exact_attempt_row(Path(jobs), row.attempt_id)
    except JoinContractError:
        return reason
    if (
        current.status == "done"
        and current.metadata.get("note") in {"completed-marker", "completed-supervisor"}
    ):
        return ""
    try:
        outcome = reconcile_attempt_terminal(
            Path(jobs),
            row.attempt_id,
            "dead-route-completion-rejected",
            evidence={
                "failure_class": "contract",
                "classifier_source": "registered-wrapper-completion-v1",
                "reconcile_reason": reason,
            },
        )
    except DispatchContractError:
        return reason
    if outcome == "closed":
        materialize_after_terminal_close(Path(jobs), row.attempt_id)
    return reason


def _close_invalid_envelope_child(
    row: ChildRow,
    *,
    jobs: str | Path,
    reason: str,
    note: str = "dead-invalid-envelope",
    classifier_source: str = "completion-join-invalid-envelope-v1",
    terminal_envelope: bool = True,
) -> bool:
    """Close a quiescent child whose terminal envelope can never legally complete.

    The worker reached its semantic terminal — an envelope exists — but named
    no readable in-root artifact file, so the completion-marker path (SD-70)
    can never run for this attempt and a remediation continuation cannot
    change the outcome. Recording a typed death keeps the route node
    incomplete for re-dispatch instead of deadlocking the supervised owner
    into `owned-children-remain-open-after-resume`. A live or unverifiable
    process keeps the row open — this never races a still-draining worker.
    """

    observed = observed_attempt_liveness(
        row.status, row.metadata, terminal_envelope=terminal_envelope
    )
    if observed.state != "reconcile-needed" or observed.process_state != "quiescent":
        return False
    try:
        closed = close_attempt_row(
            Path(jobs),
            row.attempt_id,
            note,
            evidence={
                "classifier_source": classifier_source,
                "reconcile_reason": reason,
            },
        )
    except DispatchContractError:
        return False
    if closed:
        materialize_after_terminal_close(Path(jobs), row.attempt_id)
    return closed


def run_route_completion(command: list[str]) -> str:
    """Named seam for the completion invocation — tests replace only this."""

    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=60.0, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "completion-%s" % type(exc).__name__
    return "" if result.returncode == 0 else "completion-rejected"


def reconcile_finished_children(
    rows: dict[str, ChildRow], unresolved: set[str], *, jobs: str | Path
) -> dict[str, str]:
    """Attempt evidence-backed closure for each unresolved child.

    Returns ``{attempt_id: reason}`` where an empty reason means closed.
    """

    outcomes: dict[str, str] = {}
    for attempt in sorted(unresolved):
        row = rows.get(attempt)
        if row is None:
            continue
        outcomes[attempt] = close_finished_child(row, jobs=jobs)
    return outcomes


if __name__ == "__main__":
    raise SystemExit(main())
