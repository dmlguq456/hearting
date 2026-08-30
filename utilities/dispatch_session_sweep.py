#!/usr/bin/env python3
"""SD-111 P4: carrier 2 -- durable-session-activation sweep, fail-closed.

One portable function, three harness carriers. Every harness this cycle
measured `session_generation_supported = "0"` (§3.5 -- Claude
`measured-unsupported`, Codex `unproven`, OpenCode `documented-only`), so
:func:`sweep` always calls :func:`dispatch_pending_delivery.claim` with
``require_generation_proof=True`` and is refused with
``pending-delivery-generation-unproven`` on every real record it finds. That
refusal -- not a successful claim -- is this package's observable output
(plan §7 A-21: "fixture-measured / live-unproven").

No blocking vocabulary anywhere in this module. A caller never receives an
exception from :func:`sweep`; every failure mode (unreadable directory,
corrupt record, lock contention) is swallowed and folded into ``"refused"``.
Enumeration is bounded to the caller's own ``recipient_digest`` directory
only (O(own records), §13.33.1-(3)) -- this module never walks any other
session's records and never reads ``jobs.log``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_pending_delivery as pending_delivery  # noqa: E402

SWEEP_LEASE_SECONDS = 30.0
LOG_FILENAME = "dispatch-session-sweep.log"


def _append_self_instrumentation(
    root: Path, elapsed_ns: int, entry_count: int, claimed_count: int
) -> None:
    """SD-OPEN-12 observation only -- never a gate (§12-3). No threshold, no
    warning, no block; a write failure here is swallowed like every other
    failure mode in this module."""

    try:
        root_path = Path(root)
        if not root_path.is_dir():
            # Never materialize a root just to log into it: the read-order
            # includes the legacy agent-home/.dispatch tree, and creating
            # `<release>/.dispatch/logs/` on every prompt made every
            # superseded release un-prunable (delta-digest-mismatch).
            return
        log_dir = root_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts_ns": time.time_ns(),
                "elapsed_ns": elapsed_ns,
                "entries": entry_count,
                "claimed": 1 if claimed_count else 0,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with open(log_dir / LOG_FILENAME, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


DELIVER_LEASE_SECONDS = 120.0


def _bounded_receipt_text(record: dict) -> str:
    """One bounded line per pending record for `additionalContext`; never the
    receipt body, transcript, or artifact contents (SD-111 receipt policy)."""

    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    children = receipt.get("children") if isinstance(receipt.get("children"), list) else []
    parts = []
    for child in children:
        if not isinstance(child, dict):
            continue
        parts.append(
            f"attempt_id={child.get('attempt_id', '-')} status={child.get('status', '-')} "
            f"required_action={child.get('required_action', '-')} "
            f"classification={child.get('delivery_classification', '-')}"
        )
    return (
        f"delivery_id={record.get('delivery_id', '-')} route_id={record.get('route_id', '-')} "
        f"route_node={record.get('route_node', '-')} attempts={record.get('attempts', 0)} "
        + " ".join(parts)
    )


def sweep_deliver(
    root: Path, recipient_kind: str, session_id: str, *, now_ns: int | None = None
) -> tuple[list[dict], int]:
    """Claude carrier 2 -- deliver, not merely refuse (2026-08-29 decision,
    supersedes the A-21 "surface nothing" slice for Claude).

    Claims every record addressed to ``session_id`` that is ``pending`` or
    whose lease (``claimed``/``sent-ambiguous``) has expired, WITHOUT a
    generation proof: Claude is measured-unsupported for that proof, and the
    accepted trade is at-least-once re-delivery (bounded by
    ``RECLAIM_LIMIT``) over never-delivered. Each returned record is then
    acked by the caller once its bounded receipt has been injected into the
    session's own turn -- an injection into ``additionalContext`` is
    synchronous with the recipient's next inference, unlike the async rewake
    carrier, so acking there is the recipient consuming the token.

    Returns ``(claimed_records, entry_count)``; never raises.
    """

    now = time.monotonic_ns() if now_ns is None else now_ns
    start_ns = time.monotonic_ns()
    claimed: list[dict] = []
    entries: list[Path] = []
    try:
        directory = pending_delivery.record_directory(root, session_id)
        entries = sorted(
            p for p in directory.glob("delivery-*.json") if p.is_file()
        )
        for entry in entries:
            delivery_id = entry.stem
            try:
                current = pending_delivery.read(root, session_id, delivery_id)
            except pending_delivery.PendingDeliveryError:
                continue
            if current is None or current.get("recipient_kind") != recipient_kind:
                continue
            state = current.get("state")
            if state in {"claimed", "sent-ambiguous"}:
                try:
                    pending_delivery.reclaim(root, session_id, delivery_id, now_ns=now)
                except pending_delivery.PendingDeliveryError:
                    continue
            elif state != "pending":
                continue
            claim_owner = (
                f"session-sweep:{recipient_kind}:{os.getpid()}:{time.monotonic_ns()}"
            )
            try:
                record = pending_delivery.claim(
                    root,
                    session_id,
                    delivery_id,
                    claim_owner=claim_owner,
                    lease_seconds=DELIVER_LEASE_SECONDS,
                    require_generation_proof=False,
                )
            except pending_delivery.PendingDeliveryError:
                continue
            claimed.append(record)
    except OSError:
        pass
    elapsed_ns = time.monotonic_ns() - start_ns
    _append_self_instrumentation(root, elapsed_ns, len(entries), len(claimed))
    return claimed, len(entries)


def ack_delivered(root: Path, session_id: str, records: list[dict], *, acked_by: str) -> int:
    """Ack the records whose bounded receipt was injected; returns the count."""

    acked = 0
    for record in records:
        try:
            pending_delivery.ack(
                root, session_id, record["delivery_id"], acked_by=acked_by
            )
            acked += 1
        except (pending_delivery.PendingDeliveryError, KeyError, OSError):
            continue
    return acked


def sweep(
    root: Path,
    recipient_kind: str,
    session_id: str,
    session_generation: str,
) -> tuple[str, int]:
    """Enumerate this session's own ``recipient_digest`` directory and try to
    claim every open record found in it, always demanding generation proof.

    Returns ``(outcome, entry_count)`` where ``outcome`` is ``"claimed"`` when
    at least one record was actually claimed and ``"refused"`` otherwise
    (including the "nothing here" / "directory unreadable" cases -- those are
    not distinguished because a foreign session and an empty own session must
    look identical from the outside, §13.33.1-(6)). Never raises.

    ``session_generation`` is accepted, not consulted: the record's identity
    (its ``recipient_digest``, computed from ``session_id`` alone -- the same
    formula the P2 writer and carrier 1 already use) does not depend on it,
    and ``require_generation_proof=True`` is unconditional below regardless
    of what this caller believes its own generation is. It is kept in the
    signature so a future generation-proof harness (§9 R-8, not adopted this
    cycle) has a call site to extend without a signature break.
    """

    start_ns = time.monotonic_ns()
    entries: list[Path] = []
    claimed = 0
    try:
        directory = pending_delivery.record_directory(root, session_id)
        entries = sorted(
            p for p in directory.glob("delivery-*.json") if p.is_file()
        )
        for entry in entries:
            delivery_id = entry.stem
            claim_owner = (
                f"session-sweep:{recipient_kind}:{os.getpid()}:{time.monotonic_ns()}"
            )
            try:
                pending_delivery.claim(
                    root,
                    session_id,
                    delivery_id,
                    claim_owner=claim_owner,
                    lease_seconds=SWEEP_LEASE_SECONDS,
                    require_generation_proof=True,
                )
            except pending_delivery.PendingDeliveryError:
                continue
            claimed += 1
    except OSError:
        pass
    elapsed_ns = time.monotonic_ns() - start_ns
    _append_self_instrumentation(root, elapsed_ns, len(entries), claimed)
    return ("claimed" if claimed else "refused", len(entries))
