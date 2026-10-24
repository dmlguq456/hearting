#!/usr/bin/env python3
"""Runtime-owned exact-batch join for registered headless children.

The joiner never returns child output.  It snapshots attempts bound to one
``parent_attempt_id``, waits until every attempt is either closed or requires
typed harvest, and emits one bounded JSON receipt for the session supervisor.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def _cli_guard_failure(message: str) -> None:
    sys.stderr.write(f"dispatch-completion-join: {message}\n")
    raise SystemExit(70)


def _cli_parent_fence() -> None:
    """Bind this CLI waiter to its direct consumer before project imports."""
    raw_expected = os.environ.get("HEARTING_JOIN_CONSUMER_PID")
    if raw_expected is None:
        expected_parent = os.getppid()
    elif not (raw_expected.isascii() and raw_expected.isdecimal()):
        _cli_guard_failure("invalid consumer pid")
    else:
        expected_parent = int(raw_expected)
        if expected_parent <= 0:
            _cli_guard_failure("invalid consumer pid")

    if os.getppid() != expected_parent:
        _cli_guard_failure("consumer pid mismatch")
    if sys.platform != "linux":
        sys.stderr.write(
            "dispatch-completion-join: parent-lifetime guarantee is unproven on "
            f"{sys.platform}; continuing with existing join behavior\n"
        )
        return

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # Linux PR_SET_PDEATHSIG. Install before project imports or registry
        # access, then reread PPID to close the setup race.
        result = libc.prctl(1, signal.SIGTERM, 0, 0, 0)
    except (AttributeError, OSError) as exc:
        _cli_guard_failure(f"parent-death guard failed: {exc}")
    if result != 0:
        err = ctypes.get_errno()
        _cli_guard_failure(f"parent-death guard failed: {os.strerror(err)}")
    if os.getppid() != expected_parent:
        _cli_guard_failure("consumer exited during guard setup")


if __name__ == "__main__":
    _cli_parent_fence()

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_attempt_policy import (
    decide_attempt, required_action, deferred_completion, success_note,
    DEFERRED_COMPLETION_NOTE, DEFERRED_COMPLETION_SOURCE, terminal_conflict_pending,
)
from dispatch_receipt_identity import (
    CANONICAL_RECEIPT_KEYS, CANONICAL_CHILD_KEYS, canonical_receipt, receipt_digest, unseal_receipt,
)
from dispatch_contract import (  # noqa: E402
    AUTOMATIC_RECEIPTLESS_CLASSIFIER,
    SUBSESSION_NOTE,
    SUBSESSION_TERMINAL_CLASSIFIER,
    SUBSESSION_CHAIN_REFUSAL_CLASSIFIER,
    _marker_bound_prepare_marker_proof,
    SUCCESS_NOTES,
    DispatchContractError,
    ProcessQuiescence,
    FOREGROUND_OUTCOME_KEYS,
    ROUTE_IDENTITY_METADATA_KEYS,
    _foreground_outcome_values_from_pipe,
    detached_review_outcome_from_pipe,
    attempt_raw_row_sha256,
    attempt_process_quiescence,
    foreground_review_eligible,
    validate_review_output_binding,
    close_attempt_row,
    close_attempt_row_if,
    close_refused_chain_rows,
    marker_bound_delivery_transaction,
    marker_bound_process_identity,
    observed_attempt_liveness,
    parse_registry_metadata,
    process_identity_disposition,
    launched_attempt_identity,
    reconcile_attempt_terminal,
    residue_live_pids,
    resolve_attempt_cleanup,
    resolve_residue_drain,
    row_is_subsession,
    tagged_residue_receipt,
    validate_attempt_metadata,
)
import dispatch_pending_delivery as pending_delivery  # noqa: E402
import dispatch_subsession_advance as subsession_advance  # noqa: E402
from codex_dispatch_terminal import (  # noqa: E402
    ARTIFACT_HINT_KEYS,
    REVIEW_BLOCKING_NOTE,
    review_blocking_handoff,
    inspect_terminal_attempt,
    terminal_envelope_observed,
)
from dispatch_degradation import record_degradation  # noqa: E402
from dispatch_supervisor_terminal import (  # noqa: E402
    classify_supervisor_log, missing_result_terminal,
)
from dispatch_registry_cache import registry_lines, registry_view  # noqa: E402

INVALID_ENVELOPE_CLASSIFIER_SOURCE = "completion-join-invalid-envelope-v1"
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
CODEX_QUEUE_PARENT_DELIVERY = "codex-native-queue"
SESSION_PARENT_DELIVERIES = frozenset(
    {SESSION_PARENT_DELIVERY, MANAGED_SESSION_PARENT_DELIVERY,
     CODEX_QUEUE_PARENT_DELIVERY}
)
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
MAX_DELIVERY_RECEIPT_BYTES = 2048
# SUCCESS_NOTES / SUBSESSION_NOTE / row_is_subsession are re-exported from
# `dispatch_contract`, which owns the definition (this module imports that one,
# so the vocabulary cannot live here without a cycle). `worker-route-guard.py`
# imports SUCCESS_NOTES from this module by name.


class JoinContractError(RuntimeError):
    """A registry or liveness boundary could not be proved."""


class CompletionDeferred(JoinContractError):
    """Cancellation cleanup is retained by the existing execution observer."""


def canonical_delivery_receipt(receipt: dict[str, object]) -> dict[str, object]:
    """Select only the digest-material keys of one v2 receipt (SD-111 D-2).

    ``delivery_timing`` is process-local (``time.monotonic_ns()``) and is
    excluded on purpose -- it is observability, not completion identity. This
    projection never becomes the record's stored ``receipt`` field (that is
    the sealed original, round 2 C-2); it exists only to compute a stable
    digest/``delivery_id`` across carriers.
    """

    try:
        return canonical_receipt(receipt)
    except ValueError as exc:
        raise JoinContractError(str(exc)) from exc


def canonical_receipt_digest(receipt: dict[str, object]) -> str:
    """Return the sha256 hex digest of the timing-excluded canonical receipt."""

    try:
        return receipt_digest(receipt)
    except ValueError as exc:
        raise JoinContractError(str(exc)) from exc


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
    try:
        return unseal_receipt(encoded)
    except ValueError as exc:
        raise JoinContractError("delivery-receipt-invalid") from exc


OWNER_ROUTE_NODE = "_owner"
NO_PARENT_ATTEMPT = "-"
PENDING_DELIVERY_LOG = "dispatch-pending-delivery.log"


def pending_record_identity(
    metadata: dict[str, str], jobs: Path | None = None
) -> tuple[str, str, str]:
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

    A registered depth-1 owner may since have advanced past its launch-sealed
    route generation. When a jobs path is available, the verified current
    binding (owner_route_binding's advance lifecycle) outranks the sealed
    ``owner_route_id`` so the delivery receipt reflects the owner's current
    generation rather than the one it launched under. Invalid or conflicting
    advance evidence is a refusal here (``JoinContractError``), never a
    silent fall back to the stale sealed value.
    """

    route_id = metadata.get("route_id") or ""
    route_node = metadata.get("route_node") or ""
    parent_attempt_id = metadata.get("parent_attempt_id") or ""
    # A route-free depth-1 review or standalone verification stage has no
    # owner route. Its exact attempt is the delivery scope, not a route grant.
    # 2026-10-08: the first-NOW stage completed but no pending record could
    # be materialized, leaving the native courier retrying an absent ledger.
    worker_type = metadata.get("worker_type")
    if (worker_type in {"review", "stage"}
            and metadata.get("dispatch_depth") == "1"
            and not any(metadata.get(key) for key in ROUTE_IDENTITY_METADATA_KEYS)):
        attempt_id = metadata.get("attempt_id", "")
        return (f"{worker_type}:{attempt_id}" if attempt_id else "", f"_{worker_type}",
                parent_attempt_id or NO_PARENT_ATTEMPT)
    # A dispatch-depth-1 frame leg is launched by the depth-0 session itself:
    # it carries route_id/route_node like a stage row but has no parent
    # attempt (its parent is the session). Before 2026-09-17 this shape fell
    # through to the stage triple with an empty parent, so every frame
    # terminal was refused as identity-incomplete, no record existed, and
    # the asyncRewake carrier exited 0 without waking the session -- the
    # 2026-08-29 owner defect reproduced on the frame layer added 2026-09-10.
    if (metadata.get("worker_type") == "frame"
            and metadata.get("dispatch_depth") == "1"
            and route_id and route_node):
        return route_id, route_node, parent_attempt_id or NO_PARENT_ATTEMPT
    is_owner_row = (metadata.get("worker_type") == "owner"
                    or (metadata.get("dispatch_depth") == "1"
                        and metadata.get("worker_type") not in {"review", "frame"}
                        and metadata.get("owner_route_id")))
    if is_owner_row:
        sealed = (
            metadata.get("owner_route_file"),
            metadata.get("owner_route_id"),
            metadata.get("owner_route_hash"),
        )
        current_route_id = metadata.get("owner_route_id") or ""
        # Legacy terminal rows may carry only owner_route_id, which is already
        # sufficient for the delivery identity sentinel. Consult the durable
        # lifecycle for either a complete launch tuple or a completely
        # route-less owner that can have a post-launch attachment; do not turn
        # the historical id-only form into a new refusal.
        sealed_present = any(sealed)
        lifecycle_eligible = all(sealed) or not sealed_present
        if (not route_id and jobs is not None and metadata.get("attempt_id")
                and lifecycle_eligible):
            from owner_route_binding import (
                OwnerRouteBinding,
                OwnerRouteBindingError,
                resolve_owner_route_lifecycle,
            )
            try:
                anchor = OwnerRouteBinding(*sealed) if all(sealed) else None
                current, status = resolve_owner_route_lifecycle(
                    jobs, owner_attempt_id=str(metadata.get("attempt_id", "")),
                    sealed_binding=anchor,
                )
                if status == "owner-route-advance-loop":
                    raise JoinContractError("owner-route-advance-conflict")
                if current is not None and status != "owner-route-advance-anchor-unresolvable":
                    current_route_id = current.route_id
            except OwnerRouteBindingError as exc:
                raise JoinContractError("owner-route-advance-conflict") from exc
        route_id = route_id or current_route_id
        route_node = route_node or OWNER_ROUTE_NODE
        parent_attempt_id = parent_attempt_id or NO_PARENT_ATTEMPT
    return route_id, route_node, parent_attempt_id


def _log_pending_delivery_refusal(jobs: Path, attempt_id: str, reason: str) -> None:
    """Durable trace of a swallowed materialize failure. stderr alone is not
    kept by any supervisor, so a refusal used to leave no evidence anywhere
    (2026-08-29: three owner terminals refused, zero trace). Never raises."""

    try:
        from dispatch_contract import dispatch_state_root
        from dispatch_observation_log import append_record
        append_record(dispatch_state_root(jobs), PENDING_DELIVERY_LOG, {
            "ts": time.time(),
            "event": "delivery-persistence-refused",
            "attempt_id": attempt_id,
            "reason": reason,
        })
    except OSError:
        pass


def log_delivery_refusal(jobs: Path, attempt_id: str, reason: str) -> None:
    """Public surface for a non-join caller (`claude-session-supervisor.py`'s
    `reconcile()`, SD-115 axis 4 (짝)) to leave the same durable refusal trace
    this module writes for its own swallowed failures, without reaching past
    this module's public/private boundary to call the private leaf directly.
    """

    _log_pending_delivery_refusal(jobs, attempt_id, reason)


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
    if metadata.get("workflow_completion") == "runtime-v1":
        from dispatch_terminal_commit import owner_completion_pending
        if owner_completion_pending(jobs, row_fields[1], metadata):
            return None
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
    route_id, route_node, parent_attempt_id = pending_record_identity(metadata, jobs)
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
    converge N calls on one record). `ImportError` is caught alongside the
    rest: `pending_record_identity()` lazily imports `owner_route_binding`,
    and that import raises `ModuleNotFoundError` (a subclass) if the sealed
    release tree it lives under has already been pruned out from under a
    still-running process (SD-115 axis 4 (짝)) -- without this, that one
    exception type escaped every trace this function otherwise guarantees.
    """

    try:
        row = current_attempt_row(jobs, attempt_id)
        if row is None:
            return None
        if row.metadata.get("worker_type") == "owner":
            from dispatch_owner_input import materialize_unresolved
            materialize_unresolved(jobs, attempt_id)
        if row.metadata.get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import settle_owner_completion
            settlement = settle_owner_completion(jobs, row.status, row.metadata)
            if settlement is not None and settlement.result != "completed":
                return None
        # A resultless-death recovery claim is durable controller work. Publish
        # it before the immutable terminal notice becomes visible to carriers;
        # the carrier still waits for a launched successor or a settled failure.
        try:
            from dispatch_replacement import accept_terminal_delivery_duty
            accept_terminal_delivery_duty(jobs, attempt_id)
        except (DispatchContractError, pending_delivery.PendingDeliveryError,
                OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write(
                f"replacement-duty-not-accepted attempt_id={attempt_id} reason={exc}\n"
            )
        return materialize_pending_delivery(jobs, row.raw.split("\t"))
    except (JoinContractError, pending_delivery.PendingDeliveryError, OSError, ImportError) as exc:
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


def materialize_backstop_due(root, parent_sid, delivery_id) -> bool:
    """Whether the idempotent materialize backstop may run for one row.

    Shared by the global sweep below and the exact-attempt scope in
    ``dispatch-registry.py`` so both honor the same condition: only when the
    record file is absent AND no prune tombstone exists. A pruned record's
    tombstone is proof its terminal state was already delivered and retained
    past retention -- the backstop must never resurrect it as a fresh pending
    obligation (B1). Read-only: no files are created, moved, or unlinked here.
    """
    if not parent_sid or not delivery_id:
        return False
    try:
        record_file = pending_delivery.record_path(root, parent_sid, delivery_id)
    except pending_delivery.PendingDeliveryError:
        return False
    return not record_file.is_file() \
        and not pending_delivery.tombstone_path(record_file).is_file()


_DEAD_RECIPIENT_ROW_FIELDS = (
    "attempt_id", "parent_sid", "parent_harness", "parent_runtime_pid",
    "parent_runtime_pid_start", "parent_runtime_ns", "delivery_intent",
    "delivery_id", "delivery_receipt_digest", "delivery_row_revision",
    "route_id", "route_hash", "route_node", "owner_route_id",
    "owner_route_hash", "owner_route_node", "artifact_root",
)


def expire_pending_for_dead_recipient(
    jobs: Path, owner_metadata: dict[str, str], record: dict, *, attempt_id: str,
) -> str:
    """Expire one exact pending delivery only for its positively dead original parent.

    This is the shared single-record rule used by the periodic reconcile sweep
    and the retained-obligation runner. A handover changes the effective
    recipient and protects the original record; missing, unknown, or changed
    identity remains pending. Fresh registry lineage and record identity are
    checked immediately before the existing declared expiry actor writes.

    Returns expired, pending (not positively dead), protected (successor or
    identity changed), or skipped (incomplete/unreadable).
    """
    if not isinstance(owner_metadata, dict) or not isinstance(record, dict):
        return "skipped"
    recipient_key = owner_metadata.get("parent_sid", "")
    delivery_id = record.get("delivery_id")
    row_revisions = record.get("row_revisions")
    if (not recipient_key or not isinstance(delivery_id, str) or not delivery_id
            or owner_metadata.get("attempt_id") != attempt_id
            or attempt_id not in (record.get("attempt_ids") or [])
            or owner_metadata.get("delivery_intent") != "1"
            or owner_metadata.get("delivery_id") != delivery_id
            or owner_metadata.get("delivery_receipt_digest") != record.get("receipt_digest")
            or not isinstance(row_revisions, dict)
            or row_revisions.get(attempt_id) != owner_metadata.get("delivery_row_revision")
            or pending_delivery.recipient_digest(recipient_key) != record.get("recipient_digest")
            or record.get("state") not in {"pending", "claimed", "sent-ambiguous"}):
        return "skipped"
    raw_pid = owner_metadata.get("parent_runtime_pid", "")
    pid_start = owner_metadata.get("parent_runtime_pid_start", "")
    try:
        if not raw_pid or not pid_start:
            return "skipped"
        pid = int(raw_pid)
        if pid <= 0:
            return "skipped"
    except (TypeError, ValueError):
        return "skipped"

    from dispatch_seat_handover import effective_parent, lineage_effect_lock

    def exact_current_row():
        current = current_attempt_row(jobs, attempt_id)
        if current is None:
            return None
        metadata = current.metadata
        if any(metadata.get(key, "") != owner_metadata.get(key, "")
               for key in _DEAD_RECIPIENT_ROW_FIELDS):
            return None
        if effective_parent(metadata, jobs) != recipient_key:
            return None
        return current

    try:
        current = exact_current_row()
    except (JoinContractError, OSError, ValueError, TypeError):
        return "protected"
    if current is None:
        return "protected"
    try:
        disposition = process_identity_disposition(pid, pid_start)
    except (ValueError, OSError):
        return "pending"
    if disposition != "dead":
        return "pending"

    root = jobs.resolve(strict=False).parent
    try:
        fresh_record = pending_delivery.read(root, recipient_key, delivery_id)
    except (pending_delivery.PendingDeliveryError, OSError, ValueError):
        return "skipped"
    if (fresh_record is None or fresh_record.get("state") not in {
            "pending", "claimed", "sent-ambiguous",
    } or any(fresh_record.get(key) != record.get(key)
             for key in pending_delivery.IMMUTABLE_FIELDS)
            or fresh_record.get("row_revisions") != record.get("row_revisions")
            or attempt_id not in (fresh_record.get("attempt_ids") or [])):
        return "protected"
    # Close both observation races: the attempt and its effective parent must
    # still be the same exact row after liveness and record rereads.
    try:
        with lineage_effect_lock(owner_metadata, jobs, recipient_key) as current_owner:
            if not current_owner or exact_current_row() is None:
                return "protected"
            # Recheck the exact record after acquiring the seat writer lock;
            # expiry then commits before a successor can adopt this binding.
            fresh_record = pending_delivery.read(root, recipient_key, delivery_id)
            if (fresh_record is None or fresh_record.get("state") not in {
                    "pending", "claimed", "sent-ambiguous"}
                    or any(fresh_record.get(key) != record.get(key)
                           for key in pending_delivery.IMMUTABLE_FIELDS)
                    or fresh_record.get("row_revisions") != record.get("row_revisions")
                    or attempt_id not in (fresh_record.get("attempt_ids") or [])):
                return "protected"
            expired = pending_delivery.expire_if_due(
                root, recipient_key, delivery_id,
                actor="dispatch-reconcile",
                reason="recipient-session-gone",
                liveness="known",
            )
    except (JoinContractError, pending_delivery.PendingDeliveryError, OSError, ValueError,
            TypeError, RuntimeError, BlockingIOError):
        return "skipped"
    return ("expired" if expired.get("state") == "expired"
            and expired.get("expiry_reason") == "recipient-session-gone" else "protected")


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
    3. **Retention prune** (v67): terminal records past retention and orphan
       lock files are unlinked by this same actor -- see ``pending_delivery.prune``.
       A pruned record leaves a tombstone that sweep 1 honours, so the
       append-only row never re-materializes a delivered receipt (B1).
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
    result = {"materialized": 0, "expired": 0, "skipped": 0,
              "pruned_records": 0, "pruned_locks": 0, "prune_skipped": 0}
    try:
        lines = registry_lines(jobs)
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
        if attempt_id and metadata.get("worker_type") == "owner":
            from dispatch_owner_input import materialize_unresolved
            materialize_unresolved(jobs, attempt_id)
        if attempt_id and metadata.get("delivery_intent") == "1":
            if materialize_backstop_due(root, metadata.get("parent_sid"),
                                        metadata.get("delivery_id")) \
                    and materialize_after_terminal_close(jobs, attempt_id) is not None:
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
            owner_attempt_id = None
            attempt_ids = record.get("attempt_ids")
            if isinstance(attempt_ids, list):
                for candidate in attempt_ids:
                    if candidate in rows_by_attempt:
                        owner_attempt_id = candidate
                        owner_metadata = rows_by_attempt[candidate]
                        break
            if owner_metadata is None:
                result["skipped"] += 1
                continue
            expiry = expire_pending_for_dead_recipient(
                jobs, owner_metadata, record, attempt_id=owner_attempt_id)
            if expiry == "expired":
                result["expired"] += 1
            elif expiry == "skipped":
                result["skipped"] += 1
    # 3. **Retention prune** (SD-111 §(7) v67). Terminal (`acked`/`expired`)
    #    records past `TERMINAL_RETENTION_SECONDS` and orphan `.lock` files
    #    past `ORPHAN_LOCK_RETENTION_SECONDS` are unlinked under the same
    #    single actor; open-state records are never touched (see
    #    `pending_delivery.prune`). Observers still delete nothing.
    try:
        pruned = pending_delivery.prune(root, apply=True)
        result["pruned_records"] = pruned["pruned_records"]
        result["pruned_locks"] = pruned["pruned_locks"]
        result["prune_skipped"] = pruned["skipped"]
    except Exception:  # noqa: BLE001 -- the sweep never raises
        pass
    return result


def required_action_for_attempt(status: str, metadata: dict[str, str]) -> str:
    """Return the one typed follow-up that the exact registry row permits."""

    if status not in OPEN_STATES | {"done"}:
        raise JoinContractError("owned-row-status-invalid")
    return required_action(status, metadata)


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
    completion_proven: bool = False
    terminal_conflict: bool = False
    workflow_complete: bool = True
    cancelled: bool = False
    cancellation_requested: bool = False
    workflow_state: str = "complete"
    workflow_reason: str = ""


def _delivery_choice(state, *, blocked_reason=""):
    from route_authority import decide_continuation
    blocked_reason = blocked_reason or ("closure-blocked:" + state.workflow_reason if state.workflow_state == "blocked" else "")
    choice = decide_continuation(action="delivery", facts={"state": state, "blocked_reason": blocked_reason})
    if choice.state == "wait":
        error = CompletionDeferred(choice.reason)
        error.retry_when, error.next_action = choice.retry_when, choice.next_action
        raise error
    return choice


def delivery_classification(state: CurrentDeliveryState) -> str:
    return _delivery_choice(state).state


def delivery_required_action(state: CurrentDeliveryState) -> str:
    return _delivery_choice(state).next_action


def completion_harvest_command(attempt_id: str, action: str, *, jobs: str, surface: str) -> str:
    """Project an already-decided record action; grant no workflow authority."""
    if action == "finish-workflow":
        from parent_next_directive import entrypoint
        utility = entrypoint(Path(__file__).resolve().parents[1], "utilities/dispatch_terminal_commit.py")
        return f"python3 {shlex.quote(str(utility))} finish --jobs {shlex.quote(jobs)} --attempt {shlex.quote(attempt_id)}"
    if action not in {"complete-open", "inspect-done-failure"}:
        return ""
    registry = f"--jobs {shlex.quote(jobs)} " if jobs else ""
    status = "--status open --mark-done" if action == "complete-open" else "--status done"
    return f"{shlex.quote(surface)} harvest {registry}--attempt-id {shlex.quote(attempt_id)} {status}"


def completion_followup_text(receipt: dict, *, jobs: str, surface: str) -> str:
    if receipt.get("children") and all(child.get("reason") == "cancelled-by-parent"
            and child.get("required_action") == "advance-completed" for child in receipt["children"]):
        return ("Parent-requested close has settled. No harvest, next-stage launch, "
                "route restart, or manual finalization is required.")
    # A submitted work request already owns preparation and continuation. Give
    # its parent that exact handle again, rather than ask it to rediscover a
    # route or synthesize a new workflow from generic harvest instructions.
    work_commands = {}
    completed_work = set()
    parked_notes = []
    if jobs:
        from route_identity import route_hash
        for child in receipt["children"]:
            try:
                row = exact_attempt_row(Path(jobs), child["attempt_id"])
                meta = row.metadata
                import route_parent_close
                if route_parent_close.row_requested(meta, Path(jobs)):
                    continue
                if meta.get("dispatch_depth") != "1" or meta.get("worker_type") not in {"frame", "owner"}:
                    continue
                if meta["worker_type"] == "owner" and child["required_action"] == "advance-completed":
                    if meta.get("workflow_completion") == "runtime-v1":
                        completed_work.add(child["attempt_id"])
                    continue  # Already sealed; successful work has no extra command obligation.
                path = Path(meta.get("owner_route_file") or meta.get("route_file") or "")
                route = json.loads(path.read_text())
                if (not route.get("work_request") or route.get("route_hash") != route_hash(route)
                        or route["route_hash"] != (meta.get("owner_route_hash") or meta.get("route_hash"))
                        or route.get("route_id") != (meta.get("owner_route_id") or meta.get("route_id"))):
                    continue
                from parent_next_directive import resume_command
                work_commands[child["attempt_id"]] = resume_command(
                    path, jobs, agent_home=Path(__file__).resolve().parents[1])
                if meta["worker_type"] == "owner" and child["required_action"] == "inspect-done-failure":
                    import dispatch_replacement
                    parked = dispatch_replacement.owner_parked_gate(Path(jobs), child["attempt_id"])
                    if parked:
                        parked_notes.append(
                            f"Owner {child['attempt_id']} paused at human gate {parked['gate']} "
                            f"({parked['status']}); this is not a failure. The start handle below returns "
                            "the exact release command, or starts the continuation once the gate is released.")
            except (OSError, ValueError, KeyError, JoinContractError):
                continue  # Legacy/corrupt context retains the exact inspection surface below.
    commands = [work_commands.get(child["attempt_id"]) or
                completion_harvest_command(child["attempt_id"], child["required_action"],
                    jobs=jobs, surface=surface) for child in receipt["children"]]
    if completed_work and all(child["attempt_id"] in completed_work for child in receipt["children"]):
        notes = [f"The approved route plan names a next leg (index {child['next_leg']['index']}): "
                 f"{child['next_leg']['compose_command']}" for child in receipt["children"] if child.get("next_leg")]
        return ("The requested work is complete, including its declared stages and runtime workflow/route/cycle settlement. "
                "Continue and complete the original user task itself, preserving every explicit content and output-format "
                "requirement from that request; this receipt supplements the task and does not replace it with a receipt-only response. "
                "Read the exact result and perform the already approved parent review/follow-through in this turn, "
                "or run its existing registered continuation. On the first user-facing close, explain what changed and why, "
                "the observed before/after, checks actually run (including reused, skipped, and unrun checks), applicable "
                "commit/PR/deployment/serving state, and limits or remaining work. Do not invent deployment or operating "
                "results. Receipt, queue consumption and ACK are not execution. No harvest, route restart, or manual "
                "finalization is required. An automatic repeat of this exact result or a cleanup-only notice after the "
                "follow-through is handled does not call for the same action or a new user ACK; preserve a new failure, "
                "conflict, unfinished work, or direct user question."
                + ("".join("\n" + note for note in notes)
                   + ("\nIf this next leg is already approved, execute it in this turn or its existing registered continuation. "
                      "Nothing starts it from receipt alone; obtain approval only for work outside the existing scope." if notes else "")))
    if work_commands:
        text = "\n".join(dict.fromkeys(command for command in commands if command))
        return ("\n".join(parked_notes) + ("\n" if parked_notes else "")
                + "Continue the existing work with its exact handle:\n" + text
                + "\nThis reuses registered attempts and the shared outcome/cleanup controller. "
                  "It returns the current frame results and whether a native user question is ready. "
                  "Use those exact results; another cycle's artifact cannot replace a failed frame. "
                  "No replacement launch or manual workflow/cycle finalization is implied.")
    command_text = "\n".join(command for command in commands if command) or "(no harvest command; advance the route)"
    return ("Completion bookkeeping for the listed attempts:\n" + command_text
            + "\nThe commands use the shared, runtime-neutral registry harvest compatibility surface; "
              "it does not select or change the owner or child harness. "
              "Continue the authorized work within its existing human gates. "
              "This receipt creates no new approval step.")


def _project_legacy_receipt(receipt, jobs: Path, parent_attempt_id: str):
    if (isinstance(receipt, dict) and receipt.get("schema_version") == 2
            and receipt.get("state") == "delivered"
            and receipt.get("job_registry") == "" and receipt.get("parent_attempt_id") == ""):
        # Legacy per-child delivery kept scope in the validated pending
        # identity instead of its body. Project it; never rewrite the
        # stored receipt/digest or treat a prefix alone as a peer ACK.
        return {**receipt, "state": "ready", "job_registry": str(Path(jobs).resolve(strict=True)),
                "parent_attempt_id": parent_attempt_id}
    return receipt


def _batch_receipt_projection(
    jobs: Path, attempt_ids: set[str], *, recipient_sid: str,
    parent_attempt_id: str, parent_completion_delivery: str,
    require_ack: bool,
) -> dict[str, object] | None:
    """Read one batch receipt from every current, exact child delivery row.

    This projection never grants execution or report authority.
    ACK mode recovers a received result before joining; current mode
    revalidates a cached result's receipt identity and generation.
    """
    if (not attempt_ids or len(attempt_ids) > MAX_BATCH_ATTEMPTS or not recipient_sid
            or not parent_attempt_id):
        return None
    import dispatch_pending_delivery as pending
    from dispatch_seat_handover import effective_parent

    jobs = Path(jobs)
    root = jobs.resolve(strict=True).parent
    receipts: list[dict[str, object]] = []
    originals = set(attempt_ids)
    for attempt_id in sorted(attempt_ids):
        try:
            row = current_attempt_row(jobs, attempt_id)
            if row is None or row.status != "done":
                return None
            meta = row.metadata
            if (meta.get("delivery_intent") != "1"
                    or meta.get("parent_completion_delivery") != parent_completion_delivery
                    or not meta.get("parent_sid")
                    or effective_parent(meta, jobs) != recipient_sid
                    or not meta.get("delivery_id")
                    or not meta.get("delivery_receipt_digest")
                    or not meta.get("delivery_row_revision")):
                return None
            from dispatch_contract import terminal_conflict_pending
            if terminal_conflict_pending(meta):
                return None
            storage_recipient = meta["parent_sid"]
            record = pending.read(root, storage_recipient, meta["delivery_id"])
            if (record is None
                    or (require_ack and record.get("state") != "acked")
                    or record.get("state") not in pending.OPEN_STATES | {"acked"}
                    or record.get("recipient_kind") != parent_completion_delivery
                    or record.get("attempt_ids") != [attempt_id]
                    or record.get("receipt_digest") != meta["delivery_receipt_digest"]
                    or record.get("row_revisions", {}).get(attempt_id)
                        != meta["delivery_row_revision"]):
                return None
            route_id, route_node, row_parent = pending_record_identity(meta, jobs)
            identity = pending.received_ack_identity(record, storage_recipient, meta["delivery_id"])
            if (identity.get("delivery_id") != meta["delivery_id"]
                    or identity.get("route_id") != route_id
                    or identity.get("route_node") != route_node
                    or identity.get("parent_attempt_id") != row_parent
                    or identity.get("row_revisions", {}).get(attempt_id)
                        != meta["delivery_row_revision"]):
                return None
            if (record.get("state") == "acked"
                    and not _received_ack_actor_valid(record.get("acked_by"),
                        parent_completion_delivery, recipient_sid, jobs=jobs, record=record)):
                return None
            receipt = _project_legacy_receipt(record.get("receipt"), jobs, parent_attempt_id)
            if (not isinstance(receipt, dict) or receipt.get("schema_version") != 2
                    or receipt.get("state") != "ready"
                    or receipt.get("parent_attempt_id") != parent_attempt_id
                    or receipt.get("job_registry") != str(jobs.resolve(strict=True))):
                return None
            receipts.append(receipt)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, JoinContractError):
            return None
    from dispatch_receipt_identity import canonical_receipt
    first = canonical_receipt(receipts[0])
    if any(canonical_receipt(receipt) != first for receipt in receipts[1:]):
        return None
    children = first.get("children")
    if not isinstance(children, list) or {
            child.get("attempt_id") for child in children if isinstance(child, dict)
    } != attempt_ids:
        return None
    for child in children:
        if (not isinstance(child, dict) or child.get("status") != "done"
                or child.get("readiness") != "ready"):
            return None
    if first.get("replacement_lineage"):
        try:
            from dispatch_replacement import adopt_receipt
            for edge in first["replacement_lineage"]:
                originals.discard(edge["replacement_attempt_id"])
                originals.add(edge["original_attempt_id"])
            adopt_receipt(jobs, originals, first)
        except (OSError, ValueError, KeyError, TypeError, DispatchContractError):
            return None
    return first


def _received_ack_actor_valid(acked_by: object, carrier: str, recipient_sid: str, *, jobs=None, record=None) -> bool:
    if isinstance(acked_by, str) and acked_by.startswith("peer-received:"):
        if jobs is None or record is None:
            return False
        from peer_obligations import received_registered_ack
        return received_registered_ack(jobs, record, recipient_sid)
    if carrier == "codex-native-queue":
        return (acked_by == "codex-native-prompt:" + recipient_sid
                or isinstance(acked_by, str)
                and acked_by.startswith("codex-native-queue:delivery-"))
    if carrier == "codex-managed-gateway":
        return isinstance(acked_by, str) and acked_by.startswith("codex-managed-gateway:")
    return isinstance(acked_by, str) and bool(acked_by)


def recover_acknowledged_batch_outcome(
    jobs: Path, attempt_ids: set[str], *, recipient_sid: str,
    parent_attempt_id: str, parent_completion_delivery: str,
) -> dict[str, object] | None:
    """Recover a batch result from every exact recipient ACK, without joining."""
    return _batch_receipt_projection(jobs, attempt_ids, recipient_sid=recipient_sid,
        parent_attempt_id=parent_attempt_id, parent_completion_delivery=parent_completion_delivery,
        require_ack=True)


def current_batch_receipt_projection(
    jobs: Path, attempt_ids: set[str], *, recipient_sid: str,
    parent_attempt_id: str, parent_completion_delivery: str,
) -> dict[str, object] | None:
    """Revalidate a cached outcome using current exact receipt identities."""
    return _batch_receipt_projection(jobs, attempt_ids, recipient_sid=recipient_sid,
        parent_attempt_id=parent_attempt_id, parent_completion_delivery=parent_completion_delivery,
        require_ack=False)


def _receipts_agree(current: dict[str, object], cached: dict[str, object]) -> bool:
    """One delivery: every receipt field both bodies carry has the same value.

    Legacy per-child bodies omit fields (a child ``reason``) that the
    observer's normalized outcome carries. That is shape, not identity; a
    differing value for a shared field is a different delivery.
    """
    try:
        left, right = canonical_receipt(current), canonical_receipt(cached)
    except ValueError:
        return False
    if any(left[key] != right[key] for key in set(left) & set(right) if key != "children"):
        return False
    children = []
    for body in (left, right):
        items = body.get("children")
        if not isinstance(items, list) or not all(isinstance(child, dict) for child in items):
            return False
        by_attempt = {child.get("attempt_id"): child for child in items}
        if len(by_attempt) != len(items):
            return False
        children.append(by_attempt)
    if set(children[0]) != set(children[1]):
        return False
    return all(children[0][aid][key] == children[1][aid][key]
               for aid in children[0] for key in set(children[0][aid]) & set(children[1][aid]))


def cached_batch_delivery_state(
    jobs: Path, attempt_ids: set[str], *, recipient_sid: str,
    parent_attempt_id: str, parent_completion_delivery: str,
    cached: dict[str, object],
) -> str:
    """Classify a cached batch outcome against its current per-child ledger.

    ``resolved``: the ledger already closed this delivery. Every child record
    is terminal (acked/expired/rejected) or was pruned after a terminal state.
    ``unmaterialized``: a child row carries intent but has neither a record
    nor a prune tombstone. Its terminal close or the reconcile backstop owns
    that record, never a carrier; absence is not resolution.
    ``partial``: some child deliveries were retired while another child's
    record is still open. The batch can no longer be sent as one, and the
    open record still owes its delivery, so nothing is resent or completed.
    ``current``: every child record is open or acked under the row's exact
    identity and agrees with ``cached`` on every receipt field both carry.
    ``changed``: anything else. A cached outcome is never resent then.
    """
    if (not attempt_ids or len(attempt_ids) > MAX_BATCH_ATTEMPTS or not recipient_sid
            or not parent_attempt_id or not isinstance(cached, dict)):
        return "changed"
    from dispatch_contract import terminal_conflict_pending
    from dispatch_seat_handover import effective_parent

    jobs = Path(jobs)
    ledger = []
    try:
        root = jobs.resolve(strict=True).parent
        for attempt_id in sorted(attempt_ids):
            row = current_attempt_row(jobs, attempt_id)
            if row is None or row.status != "done":
                return "changed"
            meta = row.metadata
            if (meta.get("delivery_intent") != "1"
                    or meta.get("parent_completion_delivery") != parent_completion_delivery
                    or not meta.get("parent_sid")
                    or effective_parent(meta, jobs) != recipient_sid
                    or not meta.get("delivery_id")
                    or not meta.get("delivery_receipt_digest")
                    or not meta.get("delivery_row_revision")
                    or terminal_conflict_pending(meta)):
                return "changed"
            record = pending_delivery.read(root, meta["parent_sid"], meta["delivery_id"])
            if record is None:
                record_file = pending_delivery.record_path(
                    root, meta["parent_sid"], meta["delivery_id"])
                ledger.append("pruned" if pending_delivery.tombstone_path(record_file).is_file()
                              else "absent")
                continue
            if (record.get("recipient_kind") != parent_completion_delivery
                    or record.get("attempt_ids") != [attempt_id]
                    or record.get("receipt_digest") != meta["delivery_receipt_digest"]
                    or record.get("row_revisions", {}).get(attempt_id)
                        != meta["delivery_row_revision"]):
                return "changed"
            ledger.append(record.get("state"))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, JoinContractError):
        return "changed"
    if any(state not in pending_delivery.STATES | {"pruned", "absent"} for state in ledger):
        return "changed"
    if all(state in pending_delivery.TERMINAL_STATES | {"pruned"} for state in ledger):
        return "resolved"
    if "absent" in ledger:
        return "unmaterialized"
    if any(state in {"expired", "rejected", "pruned"} for state in ledger):
        return "partial"
    current = current_batch_receipt_projection(
        jobs, attempt_ids, recipient_sid=recipient_sid, parent_attempt_id=parent_attempt_id,
        parent_completion_delivery=parent_completion_delivery)
    return "current" if current is not None and _receipts_agree(current, cached) else "changed"


def acknowledged_attempt_receipt(
    jobs: Path, attempt_id: str, *, recipient_sid: str,
    parent_completion_delivery: str,
) -> dict[str, object] | None:
    """Read one exact ACKed terminal receipt, including BLOCKED/FAIL outcomes."""
    if not attempt_id or not recipient_sid or not parent_completion_delivery:
        return None
    try:
        import dispatch_pending_delivery as pending
        from dispatch_seat_handover import effective_parent
        from dispatch_contract import terminal_conflict_pending
        jobs = Path(jobs)
        row = current_attempt_row(jobs, attempt_id)
        if row is None or row.status != "done":
            return None
        meta = row.metadata
        if (meta.get("delivery_intent") != "1"
                or meta.get("parent_completion_delivery") != parent_completion_delivery
                or not meta.get("parent_sid")
                or effective_parent(meta, jobs) != recipient_sid
                or terminal_conflict_pending(meta)
                or not meta.get("delivery_id") or not meta.get("delivery_receipt_digest")
                or not meta.get("delivery_row_revision")):
            return None
        storage = meta["parent_sid"]
        record = pending.read(jobs.resolve(strict=True).parent, storage, meta["delivery_id"])
        if (record is None or record.get("state") != "acked"
                or record.get("recipient_kind") != parent_completion_delivery
                or record.get("attempt_ids") != [attempt_id]
                or record.get("receipt_digest") != meta["delivery_receipt_digest"]
                or record.get("row_revisions", {}).get(attempt_id) != meta["delivery_row_revision"]
                or not _received_ack_actor_valid(record.get("acked_by"), carrier=parent_completion_delivery,
                                                 recipient_sid=recipient_sid, jobs=jobs, record=record)):
            return None
        route_id, route_node, parent_attempt = pending_record_identity(meta, jobs)
        identity = pending.received_ack_identity(record, storage, meta["delivery_id"])
        if (identity.get("route_id") != route_id or identity.get("route_node") != route_node
                or identity.get("parent_attempt_id") != parent_attempt
                or identity.get("row_revisions", {}).get(attempt_id) != meta["delivery_row_revision"]):
            return None
        receipt = record.get("receipt")
        return receipt if isinstance(receipt, dict) else None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, JoinContractError):
        return None


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


def _completed_next_leg(jobs: Path, attempt_id: str):
    """`next_leg` of the route a completed registered owner ran, or None.

    Only an owner whose workflow/route/cycle settlement is complete and whose bound route carries a
    `route_plan` has one; any unreadable part means no key, never an error.
    """
    try:
        row = exact_attempt_row(Path(jobs), attempt_id)
        meta = row.metadata
        if meta.get("worker_type") != "owner" or meta.get("dispatch_depth") != "1":
            return None
        from dispatch_terminal_commit import owner_completion_state
        if owner_completion_state(Path(jobs), row.status, meta).state != "complete":
            return None
        from route_identity import route_hash
        route = json.loads(Path(meta.get("owner_route_file") or meta.get("route_file") or "").read_text())
        if (route.get("route_plan") is None or route.get("route_hash") != route_hash(route)
                or route["route_hash"] != (meta.get("owner_route_hash") or meta.get("route_hash"))):
            return None
        import route_plan
        return route_plan.next_leg_for_route(route)
    except (OSError, ValueError, KeyError, TypeError, JoinContractError, DispatchContractError):
        return None


def settle_open_pass(jobs: Path, attempt_id: str) -> bool:
    """Close an open stage child whose process has exited and whose own envelope is a
    readable PASS, through the runtime's own settlement (`settle_finished_attempt`), so a
    receipt reports its result instead of a harvest command. Anything else is left as it
    is: an owner or frame row, a live process, or an envelope that is not a readable PASS."""
    try:
        row = exact_attempt_row(Path(jobs), attempt_id)
    except (JoinContractError, OSError):
        return False
    metadata = row.metadata
    from route_authority import decide_continuation
    process = attempt_process_quiescence(metadata)
    terminal = inspect_terminal_attempt(metadata.get("log_file"), worktree=_row_worktree(row),
        artifact_root_metadata=metadata.get("artifact_root"), worker_type=metadata.get("worker_type"),
        artifact_binding=metadata)
    choice = decide_continuation(action="stage-settlement", status=row.status, metadata=metadata,
        facts={"process": process, "terminal": terminal})
    if not choice.allowed:
        return False
    return bool(settle_finished_attempt(Path(jobs), row).get("closed"))


def harvest_open_attempt(jobs, rows, *, completion, complete_routed):
    """Apply the common harvest selection through existing exact terminal writers."""
    from route_authority import decide_continuation
    from dispatch_contract import parse_registry_metadata, validate_attempt_metadata
    choice = decide_continuation(action="harvest", rows=rows)
    if not choice.allowed:
        error = JoinContractError(choice.reason)
        error.matched_live = len(choice.census)
        raise error
    if not choice.census:
        return 0, None, {}
    target = choice.census[0]
    metadata = parse_registry_metadata(target[5])
    validate_attempt_metadata(metadata)
    attempt = metadata.get("attempt_id")
    if not attempt:
        raise JoinContractError("attempt-id-required")
    if metadata.get("route_id"):
        complete_routed(jobs, metadata, target[3], completion)
    elif close_attempt_row(jobs, attempt, "harvest-complete"):
        materialize_after_terminal_close(jobs, attempt)
    else:
        raise JoinContractError("attempt-row-not-open")
    return 1, target, metadata


def wait_for_delivery_projection(receipt, *, jobs, timing=None):
    """One observation; the existing batch collector owns deferred retry and deadlines."""
    return receipt_with_delivery_observability(receipt, jobs=jobs, timing=timing)


def receipt_with_delivery_observability(
    receipt: dict[str, object],
    *,
    jobs: Path,
    timing: dict[str, int | None] | None = None,
) -> dict[str, object]:
    """Attach shared per-attempt classification and one timing vocabulary.

    A child still open with a finished readable PASS is settled first (`settle_open_pass`)."""

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
            if state.status in OPEN_STATES and settle_open_pass(jobs, attempt_id):
                state = current_delivery_state(jobs, attempt_id, parent_attempt_id=attempt_id)
        except (DispatchContractError, OSError) as exc:
            reason = exc.reason if isinstance(exc, DispatchContractError) else type(exc).__name__
            raise JoinContractError(f"delivery-transaction-failed:{reason}") from exc
        blocked_reason = str(raw_child.get("reason", ""))
        blocked_reason = blocked_reason if blocked_reason.startswith("closure-blocked:") else ""
        choice = _delivery_choice(state, blocked_reason=blocked_reason)
        classification = choice.state
        child = dict(raw_child)
        child["status"] = state.status
        child["delivery_classification"] = classification
        child["required_action"] = choice.next_action
        if classification == "attention":
            child["reason"] = choice.reason or "terminal-failure-or-unclosed"
        elif state.advanced:
            child["reason"] = "row-advanced"
        # Information only: the approved route plan's next leg, never a launch or an action.
        next_leg = _completed_next_leg(jobs, attempt_id) if classification == "success" else None
        if next_leg is not None:
            child["next_leg"] = next_leg
        else:
            child.pop("next_leg", None)
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
    resource: dict[str, object] | None = None


def valid_resource_state(value: object) -> bool:
    """Separate resource delivery; never manufacture model attempts."""
    if not isinstance(value, dict) or set(value) != {"session_id", "delivered", "outbox"}:
        return False
    delivered = value["delivered"]
    if (not isinstance(value["session_id"], str) or not _safe_identity(value["session_id"])
            or not isinstance(delivered, list)
            or any(not re_fullmatch_digest(key) for key in delivered)
            or len(set(delivered)) != len(delivered)):
        return False
    box = value["outbox"]
    if box is None:
        return True
    if (not isinstance(box, dict) or set(box) != {"receipt_id", "digest", "key", "receipt"}
            or not isinstance(box["receipt_id"], str) or not _safe_identity(box["receipt_id"])
            or not re_fullmatch_digest(box["digest"]) or not re_fullmatch_digest(box["key"])
            or box["receipt_id"] != "resource-" + box["digest"][:32]
            or box["key"] in delivered or not isinstance(box["receipt"], dict)
            or box["receipt"].get("type") != "resource-completion"):
        return False
    receipt = box["receipt"]
    if (set(receipt).difference({"launch_error"}) != {"type", "parent_attempt_id", "session_id", "route_id", "route_hash",
            "jobs", "node", "run_id", "resource_key", "resource_sha256", "state", "exit_code",
            "reason", "verification_pass", "workflow_complete", "successors"}
            or any(not isinstance(receipt[k], str) or not _safe_identity(receipt[k])
                   for k in ("parent_attempt_id", "session_id", "route_id", "route_hash", "node", "run_id", "jobs"))
            or not Path(receipt["jobs"]).is_absolute() or receipt["session_id"] != value["session_id"]
            or receipt["resource_key"] != box["key"] or not re_fullmatch_digest(receipt["resource_sha256"])
            or not isinstance(receipt["state"], str) or receipt["state"] not in {"succeeded", "cancelled", "needs-attention"}
            or (receipt["exit_code"] is not None and type(receipt["exit_code"]) is not int)
            or receipt["verification_pass"] is not False or receipt["workflow_complete"] is not False
            or (receipt["reason"] is not None and
                (not isinstance(receipt["reason"], str) or not _safe_identity(receipt["reason"])))
            or not isinstance(receipt["successors"], list) or len(receipt["successors"]) > MAX_BATCH_ATTEMPTS
            or any(not isinstance(n, str) or not _safe_identity(n) for n in receipt["successors"])
            or (receipt["state"] != "succeeded" and receipt["successors"])
            or (receipt["state"] == "succeeded" and receipt["exit_code"] != 0)):
        return False
    if "launch_error" in receipt:
        error = receipt["launch_error"]
        if (not isinstance(error, dict) or set(error) != {"type", "message"}
                or not isinstance(error["type"], str) or not _safe_identity(error["type"])
                or not isinstance(error["message"], str) or len(error["message"]) > 2048
                or receipt["state"] != "needs-attention"):
            return False
    return hashlib.sha256(json.dumps(box["receipt"], sort_keys=True,
        separators=(",", ":")).encode()).hexdigest() == box["digest"]


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


@dataclass(frozen=True)
class RuntimeWaitPartition:
    joinable: frozenset[str]
    chain_pending: frozenset[str]
    unstarted: frozenset[str]
    refusal_settled: frozenset[str]
    frontiers: tuple


def partition_runtime_wait_children(
    jobs: Path, parent_attempt_id: str, rows: list[ChildRow], candidates: set[str]
) -> RuntimeWaitPartition:
    frontiers = subsession_advance.serial_chain_frontiers(jobs, parent_attempt_id, rows, candidates)
    chain_pending = frozenset(
        attempt_id for frontier in frontiers for attempt_id in frontier.pending_attempt_ids
    )
    refusal_settled = frozenset(
        row.attempt_id for row in rows
        if row.attempt_id in candidates - chain_pending
        and row.status in {"done", "killed", "cancelled"}
        and row.metadata.get("classifier_source") == SUBSESSION_CHAIN_REFUSAL_CLASSIFIER
        and row.metadata.get("launch_outcome") == "never-launched"
        and row.metadata.get("launch_claimed") == "0"
        and not row.metadata.get("pid")
        and row.metadata.get("launch_started") != "1"
    )
    rest = [row for row in rows if row.attempt_id in candidates - chain_pending - refusal_settled]
    # Join/partition and terminal proof use the same durable launch fence.
    # A done row is ignorable only when the lock-time claim says no spawn was
    # claimed and there is no PID or start identity; outcome labels alone are
    # insufficient, and ambiguous/live rows stay protected.
    import dispatch_contract
    never_launched_terminal = {
        row.attempt_id for row in rest
        if row.status == "done"
        and dispatch_contract.attempt_row_never_started(row.raw.split("\t"))
    }
    # Terminal claims still need reconciliation even when their start receipt
    # was never published. They must not enter the retry-to-start path.
    unstarted = frozenset(unstarted_child_attempts(
        [row for row in rest if row.status not in {"done", "killed", "cancelled"}]
    ))
    joinable = frozenset(
        candidates - chain_pending - refusal_settled - unstarted - never_launched_terminal
    )
    if chain_pending and not joinable:
        unstarted = frozenset(set(unstarted) | set(chain_pending))
        chain_pending = frozenset()
    return RuntimeWaitPartition(joinable, chain_pending, unstarted, refusal_settled, tuple(frontiers))


def cancel_unstarted_chain_successors(
    jobs: Path, delivered: set[str], rows: list[ChildRow], final_text: object,
) -> frozenset[str]:
    """Close the serial sub-sessions a finished owner never started.

    An owner whose turn ends with its final handoff (PASS, FAIL or BLOCKED) -- after a
    correction stopped its chain, say -- is done with the chain. The successors registered up
    front and never started have no process to wait for or clean up, so they close as
    cancelled and never launched (the refused-chain close), and the owner's result ends the
    work instead of a demand to start them (BC rt-96bab699, 2026-10-07). Every owner
    supervisor asks here; a turn without a handoff changes nothing.
    """

    from route_authority import HANDOFF_RE

    if not isinstance(final_text, str) or HANDOFF_RE.search(final_text.strip()) is None:
        return frozenset()
    attempts = [
        row.attempt_id for row in rows
        if row.attempt_id not in delivered
        and row.metadata.get("session_chain_id")
        and row.metadata.get("subsession_mode") == "serial"
        and subsession_advance.row_is_registered_only(row.status, row.metadata)
    ]
    if not attempts:
        return frozenset()
    closed = close_refused_chain_rows(
        Path(jobs), attempts, note="subsession-chain-owner-finished",
        reconcile_reason="owner-final-handoff",
    )
    return frozenset(closed.cancelled)


def settle_runtime_wait_children(
    jobs: Path, parent_attempt_id: str, delivered: set[str], *, join_interval: float,
    route_id: str | None = None, route_hash: str | None = None,
) -> tuple[list[ChildRow], bool]:
    """Reread the exact registry through the register-to-start publication race.

    Atomic reservation can become visible just before the fenced wrapper appends
    ``launch_started=1``. Treating that single snapshot as register-only wakes
    the model and invites a duplicate start, whatever the owner's turn ended
    with. This bounded, lock-free settle window accepts only the existing
    durable launch fence and otherwise leaves the normal correction path
    unchanged. Every harness's owner supervisor uses it the same way.
    """

    timeout = min(max(join_interval * 5.0, 0.2), 5.0)
    interval = min(max(join_interval / 10.0, 0.01), 0.1)
    deadline = time.monotonic() + timeout
    while True:
        rows = current_children(Path(jobs), parent_attempt_id, route_id=route_id, route_hash=route_hash)
        new_attempts = {row.attempt_id for row in rows} - delivered
        partition = partition_runtime_wait_children(
            Path(jobs), parent_attempt_id, [row for row in rows if row.attempt_id in new_attempts], new_attempts)
        if new_attempts and not partition.unstarted:
            return rows, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return rows, False
        time.sleep(min(interval, remaining))


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
    resource: dict[str, object] | None = None,
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
        or (outbox is not None and phase not in {"deliverable", "recovery", "running-turn"})
    ):
        raise JoinContractError("supervisor-state-contract-invalid")
    value = {
        "schema_version": STATE_SCHEMA_VERSION,
        "parent_attempt_id": parent_attempt_id,
        "delivered_attempt_ids": sorted(delivered_attempt_ids),
        "phase": phase,
    }
    # A model-child phase update cannot erase an unacknowledged resource result.
    if resource is None:
        previous = read_supervisor_phase_state(path, parent_attempt_id)
        resource = previous.resource if previous is not None else None
    if resource is not None:
        if (not valid_resource_state(resource) or
                (resource["outbox"] is not None and
                 resource["outbox"]["receipt"]["parent_attempt_id"] != parent_attempt_id)):
            raise JoinContractError("supervisor-resource-state-invalid")
        value["resource"] = resource
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


def begin_supervisor_turn(
    path: Path | None,
    parent_attempt_id: str,
    delivered_attempt_ids: set[str],
    *,
    receipt_id: str | None = None,
) -> None:
    """Publish execution without acknowledging or overwriting pending delivery.

    A receiving turn may run for minutes. Its outbox must survive a crash until
    the runtime acknowledges that exact receipt after the turn returns. Read
    under the state lock so concurrent partial consumption is never undone.
    """
    if path is None:
        return
    with _supervisor_state_lock(path):
        state = read_supervisor_phase_state(path, parent_attempt_id)
        if state is None and path.exists():
            raise JoinContractError("supervisor-state-contract-invalid")
        outbox = state.outbox if state is not None else None
        if outbox is not None and outbox.receipt_id != receipt_id:
            raise JoinContractError("supervisor-outbox-receipt-mismatch")
        if state is None and receipt_id is not None:
            raise JoinContractError("supervisor-outbox-missing")
        delivered = set(delivered_attempt_ids)
        if state is not None:
            delivered.update(state.delivered_attempt_ids)
        _write_supervisor_state_unlocked(
            path, parent_attempt_id, delivered, phase="running-turn", outbox=outbox,
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
            "running-turn",
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
    resource = value.get("resource")
    if resource is not None and (not valid_resource_state(resource) or
            (resource["outbox"] is not None and
             resource["outbox"]["receipt"]["parent_attempt_id"] != parent_attempt_id)):
        return None
    return SupervisorState(str(value["phase"]), frozenset(delivered), outbox, resource)


def read_supervisor_state(
    path: Path | None,
    parent_attempt_id: str,
) -> set[str] | None:
    """Compatibility view returning only the delivered attempt set."""

    state = read_supervisor_phase_state(path, parent_attempt_id)
    return set(state.delivered_attempt_ids) if state is not None else None


def child_row_revision(row: ChildRow) -> str:
    """Return the bounded exact-row revision sealed into an outbox receipt."""

    return attempt_raw_row_sha256(row.raw)


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
    return wait_for_delivery_projection(receipt, jobs=jobs)


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


def acknowledge_supervisor_delivery(
    path: Path | None, parent_attempt_id: str, receipt_id: str,
) -> bool:
    """Acknowledge the exact notification after the runtime's receiving turn.

    This commits delivery only. It never closes a worker, publishes a marker,
    authorizes retry, or requires the model to execute a bookkeeping command.
    """
    if path is None or not receipt_id:
        return False
    with _supervisor_state_lock(path):
        state = read_supervisor_phase_state(path, parent_attempt_id)
        if state is None:
            return False
        if state.outbox is None:
            return True
        if state.outbox.receipt_id != receipt_id:
            return False
        _write_supervisor_state_unlocked(
            path, parent_attempt_id, set(state.delivered_attempt_ids),
            phase="running-turn", outbox=None,
        )
        return True


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


def _local_contract_path(
    base: Path, raw: str, relative: str, extra_roots: list[Path] | None = None,
) -> bool:
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
    if extra_roots:
        roots.extend(extra_roots)
    return any(resolved == (root / relative).resolve() for root in roots)


def _sealed_launch_home_roots(jobs: Path | None, open_attempt_ids: set[str]) -> list[Path]:
    """Every guarded attempt's sealed `launch_home` that is itself a valid
    harness root (SD-115 axis 4 R1).

    Once a row's `launch_home` seals a resolved release path
    (`dispatch_contract.sealed_launch_home()`), a supervisor whose own
    `ROOT`/`AGENT_HOME` has since rotated to a newer release must still
    admit a harvest command that names the row's OWN sealed release --
    otherwise sealing a release from prune (axis 4 (a)/(b)) is undone by the
    very next guard that reads the row it protects
    (`claude-session-supervisor.py:68`'s "Deliberately UNRESOLVED" comment
    names exactly this trap). Only rows already in the caller's own
    `open_attempt_ids` (the guarded set the caller computed independently)
    are consulted -- this never widens which attempts are trusted, only
    which *path* a trusted attempt's own command may name.
    """

    if jobs is None or not open_attempt_ids:
        return []
    roots: list[Path] = []
    for attempt_id in open_attempt_ids:
        try:
            row = current_attempt_row(jobs, attempt_id)
        except (JoinContractError, OSError):
            continue
        if row is None:
            continue
        sealed = row.metadata.get("launch_home")
        if not sealed:
            continue
        candidate = Path(sealed)
        if (candidate / "core" / "CORE.md").is_file():
            roots.append(candidate)
    return roots


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
    extra_roots = _sealed_launch_home_roots(jobs, open_attempt_ids)

    if (
        len(tokens) >= 2
        and _local_contract_path(base, tokens[0], "adapters/codex/bin/preflight.sh", extra_roots)
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
    if _local_contract_path(base, dispatch_tokens[0], "adapters/codex/bin/preflight.sh", extra_roots):
        if len(dispatch_tokens) < 2 or dispatch_tokens[1] != "dispatch-batch":
            return None
        dispatch_tokens = [str(ROOT / "utilities" / "dispatch-batch.py"), *dispatch_tokens[2:]]
        is_batch = True
    elif _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-batch.py", extra_roots):
        is_batch = True
    elif not _local_contract_path(base, dispatch_tokens[0], "utilities/dispatch-node.py", extra_roots):
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
    *,
    route_id: str | None = None,
    route_hash: str | None = None,
    registry_append_only: bool = False,
) -> list[ChildRow]:
    """Return latest exact-attempt rows owned by ``parent_attempt_id``.

    Foreign parents, legacy slug-only rows, and same-slug retries are ignored.
    A matching v2 row that lacks its attempt identity fails closed.
    """

    if not parent_attempt_id:
        raise JoinContractError("parent-attempt-id-missing")
    if bool(route_id) != bool(route_hash):
        raise JoinContractError("route-selection-identity-incomplete")
    try:
        view = registry_view(jobs, append_only=registry_append_only)
    except FileNotFoundError:
        view = None
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    records = view.completion_children(parent_attempt_id) if view is not None else ()
    for record in records:
        fields = record.fields
        meta = record.completion_metadata()
        if meta.get("parent_attempt_id") != parent_attempt_id:
            continue
        if meta.get("attempt_schema_version") != "2":
            raise JoinContractError("owned-row-schema-invalid")
        attempt_id = meta.get("attempt_id", "")
        if not attempt_id:
            raise JoinContractError("owned-row-attempt-id-missing")
        latest[attempt_id] = ChildRow(
            order=record.order,
            status=fields[1],
            slug=fields[4],
            attempt_id=attempt_id,
            raw=record.raw,
            metadata=meta,
        )

    has_bound_rows = any(
        child.metadata.get("route_id") or child.metadata.get("owner_route_id")
        for child in latest.values()
    )
    if route_id and route_hash and has_bound_rows:
        # Apply selection to each attempt's latest row, not to historical
        # rows. A replacement or route advance must not resurrect an older
        # route generation merely because its row once matched. Entirely
        # route-free legacy registries retain their existing parent-only view.
        latest = {
            attempt_id: child for attempt_id, child in latest.items()
            if ((child.metadata.get("route_id"), child.metadata.get("route_hash"))
                == (route_id, route_hash)
                or (child.metadata.get("owner_route_id"), child.metadata.get("owner_route_hash"))
                == (route_id, route_hash))
        }
    if expected_attempts is not None:
        latest = {attempt_id: child for attempt_id, child in latest.items()
                  if attempt_id in expected_attempts}
        missing = expected_attempts.difference(latest)
        if missing:
            raise JoinContractError("expected-attempt-missing")
    return sorted(latest.values(), key=lambda row: row.order)


def exact_attempt_row(jobs: Path, attempt_id: str, *,
                      registry_append_only: bool = False) -> ChildRow:
    """Read one unique current registry row by immutable attempt identity."""

    if not attempt_id:
        raise JoinContractError("attempt-id-missing")
    try:
        import dispatch_registry_cache
        with dispatch_registry_cache.registry_read_scope():
            view = registry_view(jobs, append_only=registry_append_only)
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc
    matches: list[ChildRow] = []
    for record in view.completion_attempt_records(attempt_id):
        fields = record.fields
        metadata = record.completion_metadata()
        if metadata.get("attempt_schema_version") != "2":
            raise JoinContractError("attempt-row-schema-invalid")
        matches.append(
            ChildRow(
                order=record.order,
                status=fields[1],
                slug=fields[4],
                attempt_id=attempt_id,
                raw=record.raw,
                metadata=metadata,
            )
        )
    if len(matches) != 1:
        raise JoinContractError("attempt-row-not-unique")
    return matches[0]


def current_attempt_row(jobs: Path, attempt_id: str, *, read_context=None,
                        registry_append_only: bool = False) -> ChildRow | None:
    """Return the latest exact registry row for one attempt identity."""

    if not _safe_identity(attempt_id):
        raise JoinContractError("attempt-id-invalid")
    if read_context is not None:
        return read_context.current_attempt_row(
            jobs, attempt_id, append_only=registry_append_only)
    try:
        view = registry_view(jobs, append_only=registry_append_only)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc
    record, error = view.completion_attempt(attempt_id)
    if error:
        raise JoinContractError(error)
    if record is None:
        return None
    return ChildRow(record.order, record.status, record.slug, attempt_id,
                    record.raw, record.completion_metadata())


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
    import route_parent_close
    if snapshot is not None and route_parent_close.row_requested(snapshot.metadata, jobs):
        route_parent_close.recover_attempt(jobs, snapshot.metadata)
        current = current_attempt_row(jobs, attempt_id)
        settled = bool(current and route_parent_close.cancellation_settled(current.metadata, jobs))
        return CurrentDeliveryState(None, "", child_row_revision(current),
            hashlib.sha256(current.raw.encode()).hexdigest(), current.status, "CANCELLED",
            settled, 0 if settled else 1, False, settled, False, settled,
            cancelled=settled, cancellation_requested=True)
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
    from dispatch_terminal_commit import CompletionState, owner_completion_state
    current = current_attempt_row(jobs, attempt_id) if result.advanced else snapshot
    completion = owner_completion_state(jobs, current.status, current.metadata) if current is not None else CompletionState("not-applicable")
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
        completion_proven=result.completion_proven,
        terminal_conflict=result.terminal_conflict,
        workflow_complete=completion.settled,
        workflow_state=completion.state,
        workflow_reason=completion.reason,
    )


def current_session_children(
    jobs: Path,
    parent_session_id: str,
    expected_attempts: set[str] | None = None,
    parent_completion_delivery: str = SESSION_PARENT_DELIVERY,
    *,
    registry_append_only: bool = False,
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
        view = registry_view(jobs, append_only=registry_append_only)
    except FileNotFoundError:
        view = None
    except OSError as exc:
        raise JoinContractError("registry-unreadable") from exc

    latest: dict[str, ChildRow] = {}
    records = (view.completion_parent_records(parent_session_id) if view else ())
    for record in records:
        fields = record.fields
        line = record.raw
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
            order=record.order,
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


def join_observation_path(jobs: Path, identity: dict[str, str]) -> Path:
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return jobs.resolve().parent / "join-observations" / f"{key}.json"


def read_join_observation(jobs: Path, identity: dict[str, str], *, now: float | None = None) -> dict:
    """Read a fresh exact-parent diagnostic; it never grants completion."""
    path = join_observation_path(jobs, identity)
    try:
        if path.is_symlink() or path.stat().st_size > MAX_STATE_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - value["observed_at"]
        if (value.get("schema_version") != 1 or value.get("identity") != identity
                or not 0 <= age <= 120 or value.get("state") not in {"waiting", "attention", "ready"}
                or not isinstance(value.get("children"), list)):
            return {}
        return value
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def write_join_observation(jobs: Path, identity: dict[str, str], children: list[dict],
                           *, elapsed: float, recovery_results: dict,
                           eligible_attempt_ids: set[str],
                           residue_pids: dict[str, list[int]] | None = None) -> None:
    """Publish bounded diagnostics separately from the completion receipt."""
    pending = [child for child in children if child["readiness"] == "pending"]
    attention = any(child["attempt_id"] in eligible_attempt_ids for child in pending)
    value = {"schema_version": 1, "identity": identity, "observed_at": time.time(),
             "state": "attention" if attention else ("waiting" if pending else "ready"),
             "elapsed_seconds": round(elapsed, 1),
             "pending_count": len(pending),
             "children": [{"attempt_id": child["attempt_id"], "reason": child["reason"],
                           "recovery_reason": recovery_results.get(child["attempt_id"], {}).get("reason", ""),
                           **({"residue_pids": residue_pids[child["attempt_id"]]}
                              if (residue_pids or {}).get(child["attempt_id"]) else {})}
                          for child in pending[:16]]}
    path = join_observation_path(jobs, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def reconcile_exact_dead_attempt(jobs: Path, row: ChildRow) -> dict[str, object]:
    """Delegate exact death to the registry's lock-revalidated classifier."""
    command = [sys.executable, str(ROOT / "utilities" / "dispatch-registry.py"),
               "reconcile", "--attempt", row.attempt_id, "--only-exact-dead",
               "--apply", "--jobs", str(jobs)]
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        record = json.loads(result.stdout)
        decisions = record.get("decisions")
        if (result.returncode != 0
                or record.get("classifier_source") != "tools.fleet.model.classify_attempt_evidence"
                or not isinstance(decisions, list) or len(decisions) != 1
                or decisions[0].get("attempt_id") != row.attempt_id):
            raise ValueError("exact-death-contract-invalid")
        decision = decisions[0]
        closed = decision.get("closed") == 1
        if closed and (decision.get("category") not in {"exact-dead", "owner-terminal-settled"}
                       or decision.get("revalidated") is not True):
            raise ValueError("exact-death-proof-missing")
        return {"attempt_id": row.attempt_id, "closed": closed,
                "reason": str(decision.get("reason") or "exact-death-unproved")[:160]}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return {"attempt_id": row.attempt_id, "closed": False,
                "reason": "exact-death-recovery-failed"}


def recover_receiptless_attempt(jobs: Path, row: ChildRow) -> dict[str, object]:
    """Ask the existing proof authority to settle one exact receiptless row.

    A failed observation never grants cancellation. The registry helper owns
    proof, revalidation, and closure; the join only schedules the bounded check.
    """
    if row.status not in OPEN_STATES:
        return resolve_attempt_cleanup(jobs, row.attempt_id, apply=True)
    if tagged_residue_receipt(row.metadata):
        drain = resolve_residue_drain(jobs, row.attempt_id, apply=True)
        if drain.get("changed") is True:
            # The watcher's own drain observation, repeated now that the residue
            # it recorded is gone. The existing close path runs on the next tick.
            return {"attempt_id": row.attempt_id, "closed": False, "changed": True,
                    "reason": "residue-drain-refreshed"}
    exact = reconcile_exact_dead_attempt(jobs, row)
    if exact["closed"]:
        # A closed row can still lack its namespace cleanup proof. Settle the
        # exact post-exit proof now, rather than sleeping another recovery tick.
        resolve_attempt_cleanup(jobs, row.attempt_id, apply=True)
        return exact
    if row.metadata.get("pid_scope") != "namespace-local":
        return exact
    command = [
        sys.executable, str(ROOT / "utilities" / "dispatch-registry.py"),
        "reconcile", "--attempt", row.attempt_id,
        "--automatic-cancel-receiptless", "--apply",
        "--cancellation-wait", "0", "--jobs", str(jobs),
    ]
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        record = json.loads(result.stdout)
        decisions = record.get("decisions")
        if (result.returncode != 0
                or record.get("classifier_source") != AUTOMATIC_RECEIPTLESS_CLASSIFIER
                or not isinstance(decisions, list) or len(decisions) != 1
                or decisions[0].get("attempt_id") != row.attempt_id):
            raise ValueError("recovery-contract-invalid")
        decision = decisions[0]
        closed = decision.get("closed") == 1
        if closed and not str(decision.get("receipt_digest") or "").startswith("sha256:"):
            raise ValueError("recovery-proof-missing")
        if closed:
            resolve_attempt_cleanup(jobs, row.attempt_id, apply=True)
        return {"attempt_id": row.attempt_id, "closed": closed,
                "reason": str(decision.get("reason") or "recovery-unavailable")[:160]}
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return {"attempt_id": row.attempt_id, "closed": False,
                "reason": "recovery-process-failed"}


def settle_finished_attempt(jobs: Path, row: ChildRow) -> dict[str, object]:
    """Commit exact terminal evidence before a runtime join can deliver it.

    Reapers and joiners use the same terminal writers. The canonical row, not
    a writer's exit code, proves that the transition actually committed.
    """
    reason = ""
    try:
        support = _route_free_support_row(row)
        if support:
            expected = (row.attempt_id, int(row.metadata.get("pid", "0")),
                        row.metadata.get("pid_start", ""), int(row.metadata.get("pgid", "0")))
            fresh = exact_attempt_row(jobs, row.attempt_id)
            selected_identity = launched_attempt_identity(row.raw.split("\t"))
            if launched_attempt_identity(fresh.raw.split("\t")) != selected_identity:
                return {"attempt_id": row.attempt_id, "closed": False,
                        "reason": "support-binding-mismatch"}
            if fresh.status not in OPEN_STATES:
                return {"attempt_id": row.attempt_id, "closed": True, "reason": "terminal-committed"}
            classification = classify_exact_route_free_support_outcome(
                fresh, jobs=jobs, expected_attempt_id=expected[0], expected_pid=expected[1],
                expected_pid_start=expected[2], expected_pgid=expected[3],
                quiescence=attempt_process_quiescence(fresh.metadata),
                expected_log_file=row.metadata.get("log_file", ""),
                selected_identity=launched_attempt_identity(row.raw.split("\t")),
            )
            reason = apply_exact_route_free_support_classification(
                fresh, jobs=jobs, classification=classification,
                selected_identity=launched_attempt_identity(row.raw.split("\t")),
                expected_log_file=row.metadata.get("log_file", ""),
                expected_pid=expected[1], expected_pid_start=expected[2], expected_pgid=expected[3],
            )
        elif _route_free_review_row(row):
            expected = (row.attempt_id, int(row.metadata.get("pid", "0")),
                        row.metadata.get("pid_start", ""), int(row.metadata.get("pgid", "0")))
            fresh = exact_attempt_row(jobs, row.attempt_id)
            if fresh.status not in OPEN_STATES:
                return {"attempt_id": row.attempt_id, "closed": True, "reason": "terminal-committed"}
            classification = classify_exact_route_free_review_outcome(
                fresh, jobs=jobs, expected_attempt_id=expected[0], expected_pid=expected[1],
                expected_pid_start=expected[2], expected_pgid=expected[3],
                quiescence=attempt_process_quiescence(fresh.metadata))
            reason = apply_exact_route_free_review_classification(
                fresh, jobs=jobs, classification=classification)
        else:
            reason = close_finished_child(row, jobs=jobs)
        current = exact_attempt_row(jobs, row.attempt_id)
        if support and launched_attempt_identity(current.raw.split("\t")) != selected_identity:
            return {"attempt_id": row.attempt_id, "closed": False,
                    "reason": "support-binding-mismatch"}
        if current.status == "done" and current.metadata.get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import owner_completion_pending
            materialize_after_terminal_close(jobs, current.attempt_id)
            if owner_completion_pending(jobs, current.status, current.metadata):
                return {"attempt_id": row.attempt_id, "closed": False, "reason": "workflow-completion-pending"}
        return {"attempt_id": row.attempt_id, "closed": current.status not in OPEN_STATES,
                "reason": reason or ("terminal-committed" if current.status not in OPEN_STATES
                                     else "terminal-commit-unconfirmed")}
    except (DispatchContractError, JoinContractError, OSError, ValueError) as exc:
        return {"attempt_id": row.attempt_id, "closed": False,
                "reason": str(getattr(exc, "reason", type(exc).__name__))[:160]}


def _join_snapshot(
    *,
    jobs: Path,
    initial: list[ChildRow],
    refresh: Callable[[set[str]], list[ChildRow]],
    identity: dict[str, str],
    interval: float,
    timeout: float,
    liveness_command: list[str] | None,
    liveness_probe_timeout: float,
    env: dict[str, str] | None,
    recovery: Callable[[ChildRow], dict[str, object]] | None = None,
    observation_jobs: Path | None = None,
    settlement: Callable[[ChildRow], dict[str, object]] | None = None,
    observation_context=None,
    registry_append_only: bool = False,
) -> dict[str, object]:
    """Join one immutable exact-attempt snapshot."""

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
    if observation_context is None:
        from runtime_observation import ObservationContext
        observation_context = ObservationContext(max_entries=8)
    state_key = ("join", str(Path(jobs).resolve(strict=False)),
                 tuple(sorted(snapshot)), tuple(sorted(identity.items())))
    state = observation_context.state(state_key, lambda: {
        "started": time.monotonic(), "last_recovery": {}, "last_settlement": {},
        "settlement_backoff": {}, "recovery_results": {},
        "last_observation": float("-inf"), "last_signature": "",
        "observation_error": "", "unverifiable_since": {},
        "notified_attempt_ids": set(),
        "last_notice_attempt": float("-inf"), "blocked_notice_published": False,
        "last_blocked_notice_attempt": float("-inf"), "registry_generation": None,
    })
    started = state["started"]
    last_recovery = state["last_recovery"]
    last_settlement = state["last_settlement"]
    settlement_backoff = state["settlement_backoff"]
    recovery_results = state["recovery_results"]
    last_observation = state["last_observation"]
    last_signature = state["last_signature"]
    observation_error = state["observation_error"]
    unverifiable_since = state["unverifiable_since"]
    notified_attempt_ids = state["notified_attempt_ids"]
    last_notice_attempt = state["last_notice_attempt"]
    blocked_notice_published = state["blocked_notice_published"]
    last_blocked_notice_attempt = state["last_blocked_notice_attempt"]
    while True:
        try:
            jobs_stat = Path(jobs).stat()
            current_registry_generation = (jobs_stat.st_dev, jobs_stat.st_ino,
                                           jobs_stat.st_size, jobs_stat.st_mtime_ns,
                                           jobs_stat.st_ctime_ns)
        except OSError:
            current_registry_generation = None
        if state["registry_generation"] != current_registry_generation:
            # Changed registry evidence gets a prompt fresh settlement or
            # recovery observation. The process, claim and recipient checks
            # below remain fresh on every pass regardless of this cache.
            last_recovery.clear()
            state["registry_generation"] = current_registry_generation
        rows = refresh(snapshot)
        children: list[dict[str, str]] = []
        blocked_attempt_ids: set[str] = set()
        blocked_reasons: dict[str, str] = {}
        closure_eligible: dict[str, bool] = {}
        pending = False
        recovered = False
        for row in rows:
            import route_parent_close
            if route_parent_close.row_requested(row.metadata, jobs):
                unverifiable_since.pop(row.attempt_id, None)
                outcome = route_parent_close.recover_attempt(jobs, row.metadata)
                current = current_attempt_row(
                    jobs, row.attempt_id, registry_append_only=registry_append_only)
                settled = bool(outcome and outcome.get("state") == "cancelled" and current
                               and current.metadata.get("parent_close_settled") == "1")
                pending = pending or not settled
                closure_eligible[row.attempt_id] = settled
                children.append({"attempt_id": row.attempt_id, "slug": row.slug,
                    "status": current.status if current else row.status,
                    "readiness": "ready" if settled else "pending",
                    "reason": "cancelled-by-parent" if settled else "termination-pending",
                    "required_action": "advance-completed" if settled else "complete-open"})
                continue
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
            if row.status not in OPEN_STATES | {"done"}:
                raise JoinContractError("owned-row-status-invalid")
            from dispatch_terminal_commit import owner_completion_state
            from route_authority import decide_continuation
            completion_state = owner_completion_state(jobs, row.status, row.metadata)
            choice = decide_continuation(action="join", status=row.status, metadata=row.metadata,
                facts={"observed": observed, "completion": completion_state, "settlement_available": settlement is not None})
            decision = choice.census["attempt"]
            workflow_pending, workflow_blocked = choice.census["workflow_pending"], choice.census["blocked"]
            if workflow_blocked:
                blocked_attempt_ids.add(row.attempt_id)
                blocked_reasons[row.attempt_id] = completion_state.reason
            readiness, reason = choice.state, choice.reason
            if reason == "process-unverifiable":
                unverifiable_since.setdefault(row.attempt_id, time.monotonic())
            else:
                unverifiable_since.pop(row.attempt_id, None)
            pending = pending or choice.state == "pending"
            closure_eligible[row.attempt_id] = choice.census["closure_eligible"]
            if choice.census["settle"]:
                # A repeated identical (closed, reason) outcome, or the
                # registry not having changed at all since the last call,
                # backs the retry interval off 2 -> 4 -> ... -> 60s instead
                # of re-driving the whole settlement transaction every tick
                # forever (item 8): the same permanent story does not need
                # re-proving every 2s. A changed outcome or a changed
                # registry resets to 2s -- new evidence deserves a prompt
                # recheck, not the backed-off cadence from before it arrived.
                backoff = settlement_backoff.setdefault(
                    row.attempt_id, {"interval": 2.0, "result": None, "registry_key": None})
                try:
                    registry_stat = Path(jobs).stat()
                    registry_key = (registry_stat.st_ino, registry_stat.st_mtime_ns, registry_stat.st_size)
                except OSError:
                    registry_key = None
                if registry_key != backoff["registry_key"]:
                    backoff["interval"] = 2.0
                if time.monotonic() - last_settlement.get(row.attempt_id, float("-inf")) >= backoff["interval"]:
                    last_settlement[row.attempt_id] = time.monotonic()
                    outcome = settlement(row)
                    recovery_results[row.attempt_id] = outcome
                    recovered = recovered or outcome.get("closed") is True
                    result_key = (outcome.get("closed"), outcome.get("reason"))
                    backoff["interval"] = (
                        2.0 if result_key != backoff["result"] else min(60.0, backoff["interval"] * 2)
                    )
                    backoff["result"] = result_key
                    backoff["registry_key"] = registry_key
            if (recovery is not None and choice.census["recover"]
                    and row.metadata.get("registered_worker") == "1"
                    and time.monotonic() - last_recovery.get(row.attempt_id, float("-inf")) >= 30):
                last_recovery[row.attempt_id] = time.monotonic()
                outcome = recovery(row)
                recovery_results[row.attempt_id] = outcome
                recovered = recovered or outcome.get("closed") is True or outcome.get("changed") is True
            children.append(
                {
                    "attempt_id": row.attempt_id,
                    "slug": row.slug,
                    "status": row.status,
                    "readiness": readiness,
                    "reason": reason,
                    "required_action": choice.next_action,
                }
            )
        current_attempt_ids = {child["attempt_id"] for child in children}
        for attempt_id in set(unverifiable_since) - current_attempt_ids:
            unverifiable_since.pop(attempt_id, None)
        if recovered:
            # Re-read the canonical rows and all process evidence. The helper's
            # exit code or JSON alone never makes a child ready.
            continue
        if blocked_attempt_ids and rows and all(
            closure_eligible.get(row.attempt_id) for row in rows
        ):
            # unfinishable-watch (item 8): every child is either proven
            # permanently blocked or was already otherwise terminal-delivered
            # -- no amount of further waiting changes that. Once the one
            # closure-blocked notice is durably recorded (idempotent: a
            # relaunched join over an already-notified batch records no
            # second notice), stop waiting out the deadline and hand back a
            # typed ready receipt instead, so the sidecar delivers it once
            # and exits.
            try:
                from dispatch_supervision import materialize
                materialize(jobs, blocked_attempt_ids, reason="closure-blocked")
            except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                pass
            else:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "state": "ready",
                    **identity,
                    "children": [
                        {
                            **child,
                            "reason": f"closure-blocked:{blocked_reasons.get(child['attempt_id'], '')}",
                            "required_action": "inspect-recovery",
                            "readiness": "ready",
                        }
                        if child["attempt_id"] in blocked_attempt_ids else child
                        for child in children
                    ],
                    "delivery_timing": delivery_timing_fields(
                        last_child_terminal_ns=time.monotonic_ns()
                    ),
                }
        elapsed = time.monotonic() - started
        now = started + elapsed
        eligible = {
            child["attempt_id"] for child in children
            if child["reason"] == "process-unverifiable"
            and now - unverifiable_since[child["attempt_id"]] >= 30
        }
        signature = json.dumps({"children": children, "eligible": sorted(eligible)}, sort_keys=True)
        if observation_jobs is not None and (signature != last_signature
                or time.monotonic() - last_observation >= 30):
            try:
                # Display only: name the survivors a finished worker left running.
                residue_pids = {
                    row.attempt_id: list(residue_live_pids(row.metadata))[:16]
                    for row in rows
                    if tagged_residue_receipt(row.metadata)
                    and any(child["attempt_id"] == row.attempt_id
                            and child["reason"] == "process-alive" for child in children)
                }
                write_join_observation(observation_jobs, identity, children,
                                       elapsed=elapsed, recovery_results=recovery_results,
                                       eligible_attempt_ids=eligible,
                                       residue_pids=residue_pids)
                observation_error = ""
            except OSError:
                # A display record never owns the child's lifetime or verdict.
                observation_error = "join-observation-write-failed"
            last_signature, last_observation = signature, time.monotonic()
            state["last_signature"], state["last_observation"] = last_signature, last_observation
            state["observation_error"] = observation_error
        notice_attempt_ids = eligible - notified_attempt_ids
        if (notice_attempt_ids and observation_jobs is not None
                and now - last_notice_attempt >= 30):
            last_notice_attempt = now
            state["last_notice_attempt"] = last_notice_attempt
            try:
                from dispatch_supervision import materialize
                materialize(observation_jobs, notice_attempt_ids, reason="process-unverifiable")
                notified_attempt_ids.update(notice_attempt_ids)
            except (OSError, ValueError, pending_delivery.PendingDeliveryError) as exc:
                observation_error = "supervision-notice-unpersisted:" + str(exc)
        if (not blocked_notice_published and observation_jobs is not None and blocked_attempt_ids
                and time.monotonic() - last_blocked_notice_attempt >= 30):
            last_blocked_notice_attempt = time.monotonic()
            state["last_blocked_notice_attempt"] = last_blocked_notice_attempt
            try:
                from dispatch_supervision import materialize
                materialize(observation_jobs, blocked_attempt_ids, reason="closure-blocked")
                blocked_notice_published = True
                state["blocked_notice_published"] = True
            except (OSError, ValueError, pending_delivery.PendingDeliveryError) as exc:
                observation_error = "supervision-notice-unpersisted:" + str(exc)
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
                "recovery_diagnostics": list(recovery_results.values()),
                "observation_error": observation_error,
            }
        time.sleep(interval)


def join_selected_attempts(*, jobs: Path, expected_attempts: set[str],
                           timeout: float = 0.0, interval: float = 2.0,
                           recover: bool = True, observation_context=None,
                           registry_append_only: bool = False) -> dict[str, object]:
    """Use the runtime join for an operator's exact selected attempts too.

    Selection is done by the caller; subsequent observations keep that fixed
    identity set. The same terminal writers and cleanup recovery own progress.
    No parent runtime or completion-note vocabulary is inferred here.
    """
    def refresh(attempts):
        return [exact_attempt_row(jobs, attempt, registry_append_only=registry_append_only)
                for attempt in sorted(attempts)]

    return _join_snapshot(
        jobs=jobs,
        initial=refresh(expected_attempts), refresh=refresh,
        identity={"selected_attempts": ",".join(sorted(expected_attempts))},
        interval=interval, timeout=timeout, liveness_command=None,
        liveness_probe_timeout=30.0, env=None,
        recovery=(lambda row: recover_receiptless_attempt(jobs, row)) if recover else None,
        observation_jobs=jobs if recover else None,
        settlement=(lambda row: settle_finished_attempt(jobs, row)) if recover else None,
        observation_context=observation_context,
        registry_append_only=registry_append_only,
    )


def join_batch(
    *,
    jobs: Path,
    parent_attempt_id: str,
    expected_attempts: set[str] | None = None,
    interval: float = 2.0,
    timeout: float = 3600.0,
    liveness_command: list[str] | None = None,
    env: dict[str, str] | None = None,
    recover_receiptless: bool = False,
    observation_context=None,
    registry_append_only: bool = False,
) -> dict[str, object]:
    """Join one immutable child batch sealed to an exact parent attempt."""

    initial = current_children(jobs, parent_attempt_id, expected_attempts,
                               registry_append_only=registry_append_only)
    return _join_snapshot(
        jobs=jobs,
        initial=initial,
        refresh=lambda snapshot: current_children(
            jobs, parent_attempt_id, snapshot, registry_append_only=registry_append_only),
        identity={"parent_attempt_id": parent_attempt_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=30.0,
        env=env,
        recovery=(lambda row: recover_receiptless_attempt(jobs, row)) if recover_receiptless else None,
        observation_jobs=jobs if recover_receiptless else None,
        settlement=(lambda row: settle_finished_attempt(jobs, row)) if recover_receiptless else None,
        observation_context=observation_context,
        registry_append_only=registry_append_only,
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
    recover_receiptless: bool = False,
    observation_context=None,
    registry_append_only: bool = False,
) -> dict[str, object]:
    """Join one exact batch sealed to an interactive parent session."""

    initial = current_session_children(
        jobs,
        parent_session_id,
        expected_attempts,
        parent_completion_delivery,
        registry_append_only=registry_append_only,
    )
    return _join_snapshot(
        jobs=jobs,
        initial=initial,
        refresh=lambda snapshot: current_session_children(
            jobs,
            parent_session_id,
            snapshot,
            parent_completion_delivery,
            registry_append_only=registry_append_only,
        ),
        identity={"parent_session_id": parent_session_id},
        interval=interval,
        timeout=timeout,
        liveness_command=liveness_command,
        liveness_probe_timeout=5.0,
        env=env,
        recovery=(lambda row: recover_receiptless_attempt(jobs, row)) if recover_receiptless else None,
        observation_jobs=jobs if recover_receiptless else None,
        settlement=(lambda row: settle_finished_attempt(jobs, row)) if recover_receiptless else None,
        observation_context=observation_context,
        registry_append_only=registry_append_only,
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
    value.add_argument("--liveness-command", help="Compatibility input; only shared exact process evidence decides readiness")
    value.add_argument("--recover-receiptless", action="store_true",
                       help="run bounded exact-proof recovery while joining owned children")
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
                recover_receiptless=args.recover_receiptless,
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
                registry_append_only=True,
            )
        else:
            receipt = join_batch(
                jobs=Path(args.jobs),
                parent_attempt_id=args.parent_attempt_id or "",
                recover_receiptless=args.recover_receiptless,
                expected_attempts=(
                    set(args.attempt_id) if args.attempt_id else None
                ),
                interval=args.interval,
                timeout=args.timeout,
                liveness_command=liveness,
                registry_append_only=True,
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
    metadata: dict[str, str], *, worktree: str, terminal=None, decision=None
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

    if terminal_conflict_pending(metadata):
        return None, "terminal-evidence-conflict"

    if terminal is None:
        terminal = inspect_terminal_attempt(metadata.get("log_file"), worktree=worktree,
            artifact_root_metadata=metadata.get("artifact_root"), worker_type=metadata.get("worker_type"),
            artifact_binding=metadata)
    from route_authority import decide_continuation
    choice = decision or decide_continuation(action="terminal-evidence", metadata=metadata, facts={"terminal": terminal})
    if not choice.allowed:
        reason = ("evidence-not-valid" if choice.state in {"absent", "invalid"} else
                  "evidence-not-pass" if choice.state == "typed" else
                  choice.reason if choice.reason.startswith("evidence-review-verdict-conflict:") else "evidence-not-readable")
        return None, reason
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


@dataclass(frozen=True)
class ExactReviewClassification:
    """A neutral decision shared by reaper and registry reconciliation."""

    state: str
    reason: str
    note: str | None
    close_action: str
    # Set only when a typed classifier (e.g. `missing_result_terminal`) already
    # computed the full close evidence -- `close_finished_child` uses it as-is
    # instead of deriving a generic one from `note`/`reason` alone, so an
    # OpenCode capacity/auth upgrade keeps its `failure_class`/`capacity_log`.
    evidence: dict[str, str] | None = None

    def as_registry_tuple(self) -> tuple[str, str, str | None]:
        return self.state, self.reason, self.note


def _route_free_support_row(row: ChildRow) -> bool:
    metadata = row.metadata
    try:
        validate_attempt_metadata(metadata)
    except DispatchContractError:
        return False
    return (
        metadata.get("attempt_schema_version") == "2"
        and metadata.get("transport") == "headless"
        and metadata.get("execution_surface") == "registered-headless"
        and metadata.get("registered_worker") == "1"
        and metadata.get("dispatch_depth") == "1"
        and metadata.get("worker_type") == "support"
        and metadata.get("unit") == "ops/session-tidy-memory"
        and metadata.get("assigned_contract") == "session-tidy-memory"
        and not any(metadata.get(key) for key in ROUTE_IDENTITY_METADATA_KEYS)
    )


def classify_exact_route_free_support_outcome(
    row: ChildRow, *, jobs: str | Path, expected_attempt_id: str,
    expected_pid: int, expected_pid_start: str, expected_pgid: int,
    quiescence: ProcessQuiescence, expected_log_file: str,
    selected_identity: tuple[object, ...],
) -> ExactReviewClassification:
    """Classify the exact semantic handoff for one known route-free support row."""
    if not _route_free_support_row(row):
        return ExactReviewClassification("contract-invalid", "support-ineligible",
                                         None, "pending")
    if (not expected_attempt_id or expected_pid <= 0 or expected_pgid != expected_pid
            or not expected_pid_start or not expected_log_file):
        return ExactReviewClassification("active", "support-binding-incomplete", None, "pending")
    if (row.attempt_id != expected_attempt_id
            or launched_attempt_identity(row.raw.split("\t")) != selected_identity
            or row.metadata.get("pid") != str(expected_pid)
            or row.metadata.get("pid_start") != expected_pid_start
            or row.metadata.get("pgid") != str(expected_pgid)
            or row.metadata.get("log_file", "") != expected_log_file):
        return ExactReviewClassification("contract-invalid", "support-binding-mismatch",
                                         None, "pending")
    try:
        unique = exact_attempt_row(Path(jobs), expected_attempt_id)
    except (JoinContractError, OSError):
        return ExactReviewClassification("contract-invalid", "support-row-not-unique",
                                         None, "pending")
    if unique.raw != row.raw:
        return ExactReviewClassification("contract-invalid", "support-row-refreshed",
                                         None, "pending")
    if quiescence.state != "quiescent":
        return ExactReviewClassification("active", f"support-process-{quiescence.reason}", None, "pending")
    terminal = classify_supervisor_log(row.metadata.get("log_file"), row.metadata.get("harness", ""))
    if terminal.reconcile_reason in {
        "terminal-log-missing", "terminal-log-unreadable", "terminal-event-missing",
    }:
        terminal = missing_result_terminal(row.metadata)
    if terminal.note == "completed-supervisor" and terminal.failure_class == "pass":
        return ExactReviewClassification("done", "support-supervisor-pass", terminal.note,
                                         "typed-close", terminal.evidence())
    if terminal.failure_class == "fail":
        state = "dead-worker-fail"
    elif terminal.failure_class == "blocked":
        state = "dead-worker-blocked"
    else:
        state = "terminal-handoff"
    return ExactReviewClassification(state, terminal.reconcile_reason or terminal.terminal_event,
                                     terminal.note, "typed-close", terminal.evidence())


def apply_exact_route_free_support_classification(
    row: ChildRow, *, jobs: str | Path, classification: ExactReviewClassification,
    selected_identity: tuple[object, ...], expected_log_file: str,
    expected_pid: int, expected_pid_start: str, expected_pgid: int,
) -> str:
    """Commit only a fresh, still-unique semantic decision through exact-row CAS."""
    if classification.close_action == "pending":
        return "pending"
    if row.status not in OPEN_STATES:
        return "terminal-history-preserved"
    # The locked predicate validates this exact decision. A second unlocked
    # classification could supply a different payload during an ABA log change.
    evidence = classification.evidence
    note = classification.note
    committed = close_attempt_row_if(
        Path(jobs), row.attempt_id, note,
        lambda fields: _support_cas_matches(
            Path(jobs), fields, row.attempt_id, selected_identity, expected_log_file,
            expected_pid, expected_pid_start, expected_pgid, classification,
        ),
        evidence=evidence,
    )
    if not committed:
        return "support-settlement-revalidation-veto"
    try:
        current = exact_attempt_row(Path(jobs), row.attempt_id)
    except (JoinContractError, OSError):
        return "support-settlement-commit-unverified"
    if (current.status != "done" or current.metadata.get("note") != note
            or launched_attempt_identity(current.raw.split("\t")) != selected_identity):
        return "support-settlement-commit-unverified"
    materialize_after_terminal_close(Path(jobs), row.attempt_id)
    return "support-settlement-committed"


def _support_cas_matches(
    jobs: Path, fields: list[str], attempt_id: str, selected_identity: tuple[object, ...],
    expected_log_file: str, expected_pid: int, expected_pid_start: str, expected_pgid: int,
    selected: ExactReviewClassification,
) -> bool:
    try:
        current = exact_attempt_row(jobs, attempt_id)
        if (current.status not in OPEN_STATES or current.raw.split("\t") != fields
                or not _route_free_support_row(current)
                or launched_attempt_identity(fields) != selected_identity
                or current.metadata.get("log_file", "") != expected_log_file
                or current.metadata.get("pid") != str(expected_pid)
                or current.metadata.get("pid_start") != expected_pid_start
                or current.metadata.get("pgid") != str(expected_pgid)):
            return False
        decision = classify_exact_route_free_support_outcome(
            current, jobs=jobs, expected_attempt_id=attempt_id,
            expected_pid=expected_pid, expected_pid_start=expected_pid_start,
            expected_pgid=expected_pgid, quiescence=attempt_process_quiescence(current.metadata),
            expected_log_file=expected_log_file, selected_identity=selected_identity,
        )
        return (decision.state == selected.state and decision.reason == selected.reason
                and decision.note == selected.note and decision.evidence == selected.evidence
                and decision.close_action != "pending")
    except (DispatchContractError, JoinContractError, OSError, ValueError, TypeError):
        return False


def _route_free_review_row(row: ChildRow) -> bool:
    metadata = row.metadata
    return (
        metadata.get("transport") == "headless"
        and metadata.get("execution_surface") == "registered-headless"
        and metadata.get("registered_worker") == "1"
        and metadata.get("dispatch_depth") == "1"
        and metadata.get("worker_type") == "review"
        and metadata.get("launch_lifecycle") in {"foreground-scoped", "detached"}
        and not any(metadata.get(key) for key in ROUTE_IDENTITY_METADATA_KEYS)
    )


def classify_exact_route_free_review_outcome(
    row: ChildRow,
    *,
    jobs: str | Path,
    expected_attempt_id: str,
    expected_pid: int,
    expected_pid_start: str,
    expected_pgid: int,
    quiescence: ProcessQuiescence,
) -> ExactReviewClassification:
    """Classify one sealed, route-free review without mutating its row."""

    if not _route_free_review_row(row):
        return ExactReviewClassification(
            "contract-invalid", "foreground-outcome-ineligible",
            "dead-foreground-outcome-ineligible", "typed-close"
        )
    detached = row.metadata.get("launch_lifecycle") == "detached"
    bound_review = any(row.metadata.get(key) for key in (
        "review_admission", "review_cycle_id", "review_producer_id",
        "review_output_locator_b64", "review_output_digest",
        "review_watchdog_budget_digest", "review_readiness_digest",
        "review_governed_lease_nonce", "review_governed_lease", "review_fence_pid",
    ))
    if not foreground_review_eligible(
        {**row.metadata, "launch_lifecycle": "foreground-scoped"},
        expected_attempt_id=expected_attempt_id,
        expected_pid=expected_pid,
        expected_pid_start=expected_pid_start,
        expected_pgid=expected_pgid,
    ):
        return ExactReviewClassification(
            "contract-invalid", "foreground-outcome-binding-mismatch",
            "dead-foreground-outcome-binding-mismatch", "typed-close"
        )
    seal_error = None
    try:
        raw_fields = row.raw.split("\t")
        if len(raw_fields) != 6:
            raise DispatchContractError("foreground-outcome-malformed")
        sealed = (detached_review_outcome_from_pipe(raw_fields[5]) if detached
                  else _foreground_outcome_values_from_pipe(raw_fields[5]))
    except (DispatchContractError, IndexError) as exc:
        sealed = None
        seal_error = exc
    if sealed is None:
        if seal_error is not None:
            if quiescence.state != "quiescent":
                return ExactReviewClassification(
                    "active", f"foreground-process-{quiescence.reason}", None, "pending"
                )
            return ExactReviewClassification(
                "terminal-handoff", seal_error.reason,
                "dead-foreground-outcome-malformed", "typed-close"
            )
        if detached and bound_review and quiescence.state == "quiescent":
            return ExactReviewClassification(
                "terminal-handoff", "review-process-outcome-missing",
                "dead-review-process-outcome-missing", "typed-close"
            )
        if not detached or bound_review:
            return ExactReviewClassification(
                "active", "foreground-outcome-pending", None, "pending"
            )
        # Ordinary detached reviews use the same governor and exact drain
        # proof as stage workers. Only --review-output launches a watchdog;
        # requiring its outcome on an ordinary review invents a worker death
        # before the readable terminal handoff can be inspected.
    if quiescence.state != "quiescent":
        return ExactReviewClassification(
            "active", f"foreground-process-{quiescence.reason}", None, "pending"
        )
    if sealed is not None and sealed["foreground_process_failure"] != "none":
        failure = sealed["foreground_process_failure"]
        return ExactReviewClassification(
            "terminal-handoff", f"foreground-process-{failure}", f"dead-{failure}", "typed-close"
        )
    metadata = row.metadata
    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=_row_worktree(row),
        artifact_root_metadata=metadata.get("artifact_root"),
        worker_type=metadata.get("worker_type"),
        artifact_binding=metadata,
    )
    from route_authority import decide_continuation
    choice = decide_continuation(action="terminal-evidence", metadata=metadata, facts={"terminal": terminal})
    if not choice.allowed:
        note, evidence = choice.detail, choice.census
        reason = choice.reason
        if choice.state == "absent":
            result = missing_result_terminal({**metadata, "worktree": _row_worktree(row), "started_at": raw_fields[0]})
            note, evidence = result.note, result.evidence()
        if choice.state in {"absent", "invalid"} and terminal.get("state") != "valid":
            reason = str(terminal.get("reason") or "terminal-" + str(terminal.get("state") or "absent"))
        return ExactReviewClassification("terminal-handoff", reason, note, "typed-close", evidence)
    artifact, evidence_reason = route_completion_evidence(
        metadata, worktree=_row_worktree(row), terminal=terminal, decision=choice
    )
    if artifact is None:
        return ExactReviewClassification(
            "terminal-handoff", evidence_reason, "dead-invalid-envelope", "typed-close"
        )
    try:
        if bound_review:
            validate_review_output_binding(
                jobs,
                attempt_id=row.attempt_id,
                output_path=artifact,
                cycle_id=metadata.get("review_cycle_id", ""),
                producer_id=metadata.get("review_producer_id", ""),
                capability=metadata.get("capability", ""),
                unit=metadata.get("unit", ""),
                worktree=_row_worktree(row),
                artifact_root=metadata.get("artifact_root", ""),
            )
    except (DispatchContractError, OSError) as exc:
        return ExactReviewClassification(
            "terminal-handoff", getattr(exc, "reason", "review-output-binding-invalid"),
            "dead-invalid-envelope", "typed-close"
        )
    return ExactReviewClassification("done", "detached-review-pass" if detached else "foreground-review-pass", "completed-review", "wrapper-pass")


def review_terminal_evidence(note: str, reason: str) -> dict[str, str]:
    """Preserve the classifier's result at every review terminal writer."""
    return {
        "classifier_source": "foreground-review-classifier-v1",
        "reconcile_reason": reason,
        "failure_class": "pass" if note == "completed-review" else "contract",
    }


def apply_exact_route_free_review_classification(
    row: ChildRow, *, jobs: str | Path, classification: ExactReviewClassification
) -> str:
    """Apply one already-computed review decision through one terminal CAS."""

    if classification.close_action == "pending":
        return "pending"
    if classification.close_action == "typed-close":
        return close_finished_child(row, jobs=jobs, classification=classification)
    if classification.close_action == "wrapper-pass":
        return close_wrapper_pass(row, jobs=jobs, classification=classification)
    return "foreground-outcome-close-action-invalid"


def settle_exited_owner_terminal(row: ChildRow, *, jobs: str | Path) -> str | None:
    """Retry the supervisor's terminal writer from its retained open row.

    Owners carry owner_route_file rather than a worker's route_file/node.
    Their exact log and process observation are the same evidence the
    post-exit watcher consumes; no new recovery record is needed.
    """
    from dispatch_contract import SUPERVISOR_LEASE_KIND, supervisor_lease_is_held
    from dispatch_supervisor_terminal import classify_supervisor_log

    meta = row.metadata
    from route_authority import decide_continuation
    choice = decide_continuation(action="owner-terminal", status=row.status, metadata=meta,
        facts={"lease_held": supervisor_lease_is_held(jobs, meta),
               "process": attempt_process_quiescence(meta, terminal_receipt=True)})
    if not choice.allowed:
        return None if choice.state == "not-applicable" else choice.reason
    terminal = classify_supervisor_log(meta["log_file"], meta.get("harness", "unknown"))
    if terminal.reconcile_reason == "terminal-log-unreadable":
        return terminal.reconcile_reason

    def still_exited(fields):
        return (fields == row.raw.split("\t")
                and not supervisor_lease_is_held(jobs, meta)
                and attempt_process_quiescence(meta, terminal_receipt=True).state == "quiescent"
                and classify_supervisor_log(meta["log_file"], meta.get("harness", "unknown")) == terminal)

    closed = close_attempt_row_if(Path(jobs), row.attempt_id, terminal.note,
                                  still_exited, evidence=terminal.evidence())
    if closed:
        materialize_after_terminal_close(Path(jobs), row.attempt_id)
    return "" if closed else "owner-terminal-revalidation-pending"


def _reconcile_late_terminal_conflict(row: ChildRow, *, jobs: str | Path) -> str | None:
    """Record an exact late negative envelope without rewriting a committed PASS."""
    try:
        current = exact_attempt_row(Path(jobs), row.attempt_id)
        from dispatch_attempt_policy import committed_outcome
        from dispatch_contract import terminal_conflict_pending
        if current.status not in {"done", "killed", "cancelled"}:
            return None
        if launched_attempt_identity(current.raw.split("\t")) != launched_attempt_identity(row.raw.split("\t")):
            return None
        if terminal_conflict_pending(current.metadata):
            return "terminal-evidence-conflict"
        if current.metadata.get("terminal_conflict") == "1":
            return None
        if committed_outcome(current.status, current.metadata) != "succeeded":
            return None
        if not current.metadata.get("log_file"):
            return None
        terminal = inspect_terminal_attempt(
            current.metadata.get("log_file"),
            worktree=_row_worktree(current),
            artifact_root_metadata=current.metadata.get("artifact_root"),
            worker_type=current.metadata.get("worker_type"),
            artifact_binding=current.metadata,
        )
        if terminal.get("state") != "valid" or terminal.get("verdict") not in {"FAIL", "BLOCKED"}:
            return None
        note = str(terminal.get("failure_note") or "")
        failure_class = str(terminal.get("failure_class") or "")
        if not note or failure_class not in {"fail", "blocked", "sandbox-init"}:
            return None
        evidence = {
            "failure_class": failure_class,
            "classifier_source": "completion-join-late-terminal-v1",
            "reconcile_reason": f"late-terminal-envelope:{terminal.get('verdict')}",
        }
        if note == REVIEW_BLOCKING_NOTE and terminal.get("artifact_path_b64"):
            evidence["review_artifact_b64"] = str(terminal["artifact_path_b64"])
        reconcile_attempt_terminal(Path(jobs), current.attempt_id, note, evidence=evidence)
        updated = exact_attempt_row(Path(jobs), current.attempt_id)
        return ("terminal-evidence-conflict"
                if terminal_conflict_pending(updated.metadata) else None)
    except (DispatchContractError, JoinContractError, OSError, ValueError, RuntimeError):
        return None


def close_finished_child(
    row: ChildRow, *, jobs: str | Path, classification: ExactReviewClassification | None = None,
    correct_recorded_move: bool = False,
) -> str:
    """Reconcile cleanup or commit a still-open child's exact terminal evidence.

    The runtime owns this transition; a model need not harvest to acknowledge
    delivery or close rows. Committed results are never reclassified here.
    """

    metadata = getattr(row, "metadata", {}) or {}
    late_conflict = _reconcile_late_terminal_conflict(row, jobs=jobs)
    if late_conflict:
        cleanup = resolve_attempt_cleanup(Path(jobs), row.attempt_id, apply=True)
        return late_conflict if cleanup["settled"] else str(cleanup["reason"])
    from route_authority import decide_continuation
    obligation = decide_continuation(action="attempt-action", status=row.status, metadata=metadata)
    if (obligation.next_action not in {"complete-open", "complete-deferred"}
            and not (correct_recorded_move and row.status == "done"
                     and metadata.get("note") == "dead-invalid-envelope"
                     and metadata.get("reconcile_reason") == "terminal-invalid:artifact-missing")):
        # A done row closed typed-deferred (budget exhausted, no marker yet)
        # is not finished -- it still owes the completion command below, so
        # only every OTHER terminal row (including a marker-bound deferred
        # one) takes the ordinary resolved-cleanup shortcut.
        result = resolve_attempt_cleanup(Path(jobs), row.attempt_id, apply=True)
        return "" if result["settled"] else str(result["reason"])
    if classification is not None:
        if classification.close_action == "pending":
            return "pending"
        if classification.close_action == "wrapper-pass":
            return "classification-action-mismatch"
        try:
            closed = close_attempt_row(
                Path(jobs), row.attempt_id, classification.note or "dead-foreground-review",
                evidence=classification.evidence
                or review_terminal_evidence(classification.note, classification.reason),
            )
        except (DispatchContractError, OSError) as exc:
            return getattr(exc, "reason", type(exc).__name__)
        if closed:
            materialize_after_terminal_close(Path(jobs), row.attempt_id)
            return ""
        return classification.reason
    owner_terminal = settle_exited_owner_terminal(row, jobs=jobs)
    if owner_terminal is not None:
        return owner_terminal
    route_file = metadata.get("route_file")
    route_node = metadata.get("route_node")
    if not route_file or not route_node:
        return "not-route-bound"
    terminal = inspect_terminal_attempt(
        metadata.get("log_file"),
        worktree=_row_worktree(row),
        artifact_root_metadata=metadata.get("artifact_root"),
        worker_type=metadata.get("worker_type"),
        artifact_binding=metadata,
    )
    if correct_recorded_move and (
        terminal.get("state") != "valid"
        or terminal.get("verdict") != "PASS"
        or not terminal.get("artifact_origin_path_b64")
    ):
        return "recorded-move-correction-evidence-unavailable"
    choice = decide_continuation(action="terminal-evidence", metadata=metadata, facts={"terminal": terminal})
    if not choice.allowed:
        if choice.next_action != "typed-close":
            return choice.reason
        note, evidence, reason = choice.detail, choice.census, choice.reason
        if choice.state == "typed":
            from foreground_terminal import terminal_evidence
            evidence = {**terminal_evidence(terminal, note, metadata.get("log_file")),
                        **(evidence or {})}
            # SD-124 preserves the foreground wrapper's fail axis, but the
            # typed blocking-review join producer carries its verdict in the
            # note and exact artifact, without a failure_class annotation.
            if note == REVIEW_BLOCKING_NOTE:
                evidence.pop("failure_class", None)
        if choice.state == "absent":
            missing = missing_result_terminal({**metadata, "worktree": _row_worktree(row),
                                              "started_at": row.raw.split("\t")[0]})
            note, evidence, reason = missing.note, missing.evidence(), "terminal-envelope-absent"
        elif choice.state in {"invalid", "evidence"}:
            evidence = {key: str(terminal.get(key) or "") for key in ARTIFACT_HINT_KEYS}
        if _close_invalid_envelope_child(row, jobs=jobs, reason=reason, note=note,
            classifier_source="completion-join-terminal-verdict-v1" if choice.state == "typed"
                              else "completion-join-missing-result-v1" if choice.state == "absent"
                              else "completion-join-invalid-envelope-v1",
            terminal_envelope=choice.state != "absent", extra_evidence=evidence):
            return ""
        return choice.reason
    artifact, evidence_reason = route_completion_evidence(
        metadata, worktree=_row_worktree(row), terminal=terminal, decision=choice
    )
    if artifact is None:
        return evidence_reason
    if row_is_subsession(metadata):
        # Defect F. `capability-route.py complete` is one transaction that
        # publishes the node's stage marker AND closes the row, and a
        # sub-session must have the second without the first: it holds no stage
        # gate authority (`subsession-has-no-stage-gate-authority`), so running
        # the command here guaranteed a typed contract failure and left a proven
        # success recorded as `dead-route-completion-rejected` -- which then
        # stalled the whole declared chain, because `complete_subsession_stage`
        # requires every slice row to be a semantic PASS.
        #
        # Fixed here rather than at the reap-watch caller so that every caller
        # is fixed at once: both supervisors' `reconcile_finished_children` and
        # the Codex headless path reach this same function.
        # Say which of the two things went wrong. A draining slice is ordinary
        # and must NOT escalate -- `dispatch-reap-watch.py` escalates only on a
        # `completion-` prefix -- but a refused close is a contract problem that
        # main used to surface as `completion-rejected`, and collapsing both into
        # one string would hide it (review round 1, item 2).
        observed = observed_attempt_liveness(
            row.status, metadata, terminal_envelope=True
        )
        if observed.state != "reconcile-needed" or observed.process_state != "quiescent":
            return "subsession-not-quiescent"
        if _close_invalid_envelope_child(
            row,
            jobs=jobs,
            reason="subsession-terminal",
            note=SUBSESSION_NOTE,
            classifier_source="completion-join-subsession-terminal-v1",
            extra_evidence={"failure_class": "pass"},
        ):
            return ""
        return "completion-subsession-close-refused"
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
    # `complete` is exact-attempt idempotent. `complete_route_with_budget`
    # owns the one bounded retry / transient backoff for both call sites.
    return complete_route_with_budget(command)


def close_wrapper_pass(
    row: ChildRow, *, jobs: str | Path, classification: ExactReviewClassification | None = None
) -> str:
    """Complete one wrapper-reaped PASS or close a typed contract failure."""

    if classification is not None:
        if classification.close_action != "wrapper-pass":
            return close_finished_child(row, jobs=jobs, classification=classification)
        try:
            closed = close_attempt_row(
                Path(jobs), row.attempt_id, classification.note or "completed-review",
                evidence=review_terminal_evidence(classification.note, classification.reason),
            )
        except (DispatchContractError, OSError) as exc:
            return getattr(exc, "reason", type(exc).__name__)
        if closed:
            materialize_after_terminal_close(Path(jobs), row.attempt_id)
            return ""
        return classification.reason

    reason = close_finished_child(row, jobs=jobs)
    if not reason:
        return ""
    try:
        current = exact_attempt_row(Path(jobs), row.attempt_id)
    except JoinContractError:
        return reason
    if current.status == "done" and success_note(current.metadata):
        return ""
    if current.status == "done" and deferred_completion(current.metadata):
        # C13: already closed typed-deferred by an earlier budget exhaustion
        # (this attempt's own retry above just failed transiently again).
        # Re-closing a terminal row here would only manufacture a terminal
        # conflict (`completion-deferred` -> `dead-route-completion-rejected`)
        # against a row that already has a committed outcome; the caller
        # keeps retrying via the ordinary settlement path instead.
        return reason
    if reason.startswith("completion-transient:"):
        try:
            outcome = reconcile_attempt_terminal(
                Path(jobs),
                row.attempt_id,
                DEFERRED_COMPLETION_NOTE,
                evidence={
                    "failure_class": "infrastructure",
                    "classifier_source": DEFERRED_COMPLETION_SOURCE,
                    "reconcile_reason": reason,
                },
            )
        except DispatchContractError:
            return reason
        if outcome == "closed":
            materialize_after_terminal_close(Path(jobs), row.attempt_id)
        return reason
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
    classifier_source: str = INVALID_ENVELOPE_CLASSIFIER_SOURCE,
    terminal_envelope: bool = True,
    extra_evidence: dict[str, str] | None = None,
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
    # B47-3: `failure_class` is bound to `classifier_source`, not to this
    # function as a whole -- `dead-missing-result`/`dead-worker-blocked`/
    # `dead-worker-fail` callers pass their own classifier_source and must
    # stay unlabeled (Appendix A-34's regression risk).
    evidence = {
        "classifier_source": classifier_source,
        "reconcile_reason": reason,
    }
    if classifier_source == INVALID_ENVELOPE_CLASSIFIER_SOURCE:
        evidence["failure_class"] = "invalid-envelope"
    for key, value in (extra_evidence or {}).items():
        if value:
            evidence[key] = value
    try:
        closed = close_attempt_row(
            Path(jobs),
            row.attempt_id,
            note,
            evidence=evidence,
        )
    except DispatchContractError:
        return False
    if closed:
        materialize_after_terminal_close(Path(jobs), row.attempt_id)
        if classifier_source == INVALID_ENVELOPE_CLASSIFIER_SOURCE:
            record_degradation(
                writer="dispatch_completion_join.py",
                kind="degradation",
                route_id=row.metadata.get("route_id"),
                route_node=row.metadata.get("route_node"),
                route_hash=row.metadata.get("route_hash"),
                dispatch_depth=row.metadata.get("dispatch_depth"),
                reason="invalid-envelope",
                attempt_id=row.attempt_id,
                classifier_source=classifier_source,
                jobs=jobs,
            )
    return closed


def run_route_completion(command: list[str], *, timeout: float = 60.0) -> str:
    """Named seam for the completion invocation — tests replace only this.

    An ``OSError``/``TimeoutExpired`` here proves nothing about whether the
    work committed -- the transport failed, not the verdict. That is
    ``completion-transient:<Exc>``, never the typed contract refusal
    ``completion-rejected`` a real non-zero exit reports.
    """

    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "completion-transient:%s" % type(exc).__name__
    return "" if result.returncode == 0 else "completion-rejected"


COMPLETION_BUDGET_S = 600.0
_COMPLETION_BACKOFF_S = (15.0, 30.0, 60.0, 120.0, 240.0)


def complete_route_with_budget(
    command: list[str], *, budget_s: float = COMPLETION_BUDGET_S,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    run: Callable[..., str] | None = None,
) -> str:
    """Retry a completion command until it settles or the budget runs out.

    ``""`` is success. ``"completion-rejected"`` is a genuine typed contract
    refusal and gets exactly one immediate retry (recovers the ordinary
    marker-already-published race) before being returned as-is -- this is
    the pre-existing "one bounded retry" both former call sites did.
    ``"completion-transient:<Exc>"`` means the transport itself failed
    (timeout/OSError) -- the work's outcome is unknown, so this backs off
    (15/30/60/120/240s) and keeps retrying until ``budget_s`` elapses (each
    call capped at ``min(120, remaining budget)``), then returns the last
    transient reason for the caller to close typed-deferred.
    """

    # A bare default parameter would bind these at def-time, so a test (or
    # caller) that monkeypatches the module/`time` attribute would be
    # silently ignored. Resolve every seam by name at call time instead --
    # the same pattern every existing `run_route_completion` test patches.
    run = run or run_route_completion
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    started = clock()
    reason = run(command)
    if not reason:
        return ""
    if reason == "completion-rejected":
        return run(command)
    if not reason.startswith("completion-transient:"):
        return reason
    for backoff in _COMPLETION_BACKOFF_S:
        remaining = budget_s - (clock() - started)
        if remaining <= 0:
            break
        sleep(min(backoff, remaining))
        remaining = budget_s - (clock() - started)
        if remaining <= 0:
            break
        reason = run(command, timeout=min(120.0, remaining))
        if not reason:
            return ""
        if reason == "completion-rejected":
            return run(command)
        if not reason.startswith("completion-transient:"):
            return reason
    return reason


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
