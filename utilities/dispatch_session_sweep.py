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
STORAGE_KEY = "_storage_key"     # added to a returned copy only: the directory a record is stored under


HUMAN_GATE_PREFIX = "human-gate:"


def is_human_gate_record(record: object) -> bool:
    """True when this record is a SD-123 (8) gate notice rather than an attempt
    completion. The two need different instructions -- one is harvested, the
    other is released -- and they are told apart by the `required_action`
    vocabulary alone, so no field was added to the record schema."""

    if not isinstance(record, dict):
        return False
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    if receipt.get("kind") == "human-gate":
        return True  # the strict gate receipt a Codex owner raises (human_gate_receipt)
    children = receipt.get("children") if isinstance(receipt.get("children"), list) else []
    return any(
        isinstance(child, dict)
        and str(child.get("required_action", "")).startswith(HUMAN_GATE_PREFIX)
        for child in children
    )


def _bounded_receipt_text(record: dict) -> str:
    """One bounded line per pending record for `additionalContext`; never the
    receipt body, transcript, or artifact contents (SD-111 receipt policy).

    A gate record renders its gate name and the artifact **path** (carried in
    `reason`). Without that the recipient would learn a gate is open and not
    what to look at -- contract (a) names the path as part of the record, so
    rendering it is the contract, not decoration.
    """

    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    if receipt.get("kind") == "supervision":
        from dispatch_supervision import CONTINUED_KEY, render_text
        return render_text(receipt, continued=record.get(CONTINUED_KEY))
    if receipt.get("kind") == "human-gate":
        return (f"delivery_id={record.get('delivery_id', '-')} route_id={receipt.get('route_id', '-')} "
                f"route_file={receipt.get('route_file', '-')} attempt_id={receipt.get('owner_attempt_id', '-')} "
                f"required_action={HUMAN_GATE_PREFIX}{receipt.get('gate', '-')} "
                f"gate={receipt.get('gate', '-')} artifact={receipt.get('artifact_path', '-')}")
    if receipt.get("kind") == "notice":
        from session_notice import render_text
        return render_text(receipt)
    children = receipt.get("children") if isinstance(receipt.get("children"), list) else []
    parts = []
    for child in children:
        if not isinstance(child, dict):
            continue
        action = str(child.get("required_action", "-"))
        if action.startswith(HUMAN_GATE_PREFIX):
            parts.append(
                f"attempt_id={child.get('attempt_id', '-')} required_action={action} "
                f"gate={action[len(HUMAN_GATE_PREFIX):] or '-'} "
                f"artifact={child.get('reason', '-')}"
            )
            continue
        parts.append(
            f"attempt_id={child.get('attempt_id', '-')} status={child.get('status', '-')} "
            f"required_action={action} "
            f"classification={child.get('delivery_classification', '-')}"
        )
    return (
        f"delivery_id={record.get('delivery_id', '-')} route_id={record.get('route_id', '-')} "
        f"route_node={record.get('route_node', '-')} attempts={record.get('attempts', 0)} "
        + " ".join(parts)
    )


def addressed_records(root: Path, recipient_kind: str, session_id: str) -> tuple[list[dict], int]:
    """Read the current session's open notices without claiming or consuming them."""
    from dispatch_seat_handover import record_for_session, storage_recipients
    records, count = [], 0
    for storage_key, allowed in storage_recipients(
            session_id, harness="codex" if recipient_kind.startswith("codex") else None):
        try:
            directory = pending_delivery.record_directory(root, storage_key)
            entries = sorted(directory.glob("delivery-*.json"))
        except (OSError, pending_delivery.PendingDeliveryError):
            continue
        count += len(entries)
        for entry in entries:
            try:
                record = pending_delivery.read(root, storage_key, entry.stem)
            except (OSError, pending_delivery.PendingDeliveryError):
                continue
            if (record and record.get("recipient_kind") == recipient_kind
                    and record.get("state") in pending_delivery.OPEN_STATES
                    and record_for_session(record, allowed)):
                records.append({**record, STORAGE_KEY: storage_key})
    return records, count


def activate(root: Path, recipient_kind: str, session_id: str) -> list[dict]:
    """Reconnect existing courier duties on startup/resume; context alone is not receipt."""
    records, _ = addressed_records(root, recipient_kind, session_id)
    from peer_obligations import retain_registered_completion
    for record in records:
        jobs = record_jobs(root, record)
        if jobs is None:
            continue
        for attempt in record.get("attempt_ids") or []:
            try:
                retain_registered_completion(jobs, attempt, session_id, recipient_kind)
            except (OSError, ValueError):
                continue
    return records


def record_jobs(root: Path, record: dict) -> Path | None:
    """Use the receipt's registry or the canonical selection, preserving its filename."""
    from dispatch_contract import resolve_agent_home, resolve_global_registry
    raw = (record.get("receipt") or {}).get("job_registry")
    jobs = Path(raw) if isinstance(raw, str) and raw else resolve_global_registry(
        resolve_agent_home(), None, 0, "read").path
    return jobs if jobs.resolve().parent == Path(root).resolve() else None


def sweep_deliver(
    root: Path, recipient_kind: str, session_id: str, *, now_ns: int | None = None
) -> tuple[list[dict], int]:
    """Claude carrier 2 -- deliver, not merely refuse (2026-08-29 decision,
    supersedes the A-21 "surface nothing" slice for Claude).

    Claims every record addressed to ``session_id`` that is ``pending`` or
    whose lease (``claimed``/``sent-ambiguous``) has expired, WITHOUT a
    generation proof: Claude is measured-unsupported for that proof, and the
    accepted trade is at-least-once delivery on a real prompt. Expired claims
    remain recoverable; the carrier emits at most once per prompt. Each record is then
    acked by the caller once its bounded receipt has been injected into the
    session's own turn -- an injection into ``additionalContext`` is
    synchronous with the recipient's next inference, unlike the async rewake
    carrier, so acking there is the recipient consuming the token.

    Returns ``(claimed_records, entry_count)``; never raises.
    """

    now = time.monotonic_ns() if now_ns is None else now_ns
    start_ns = time.monotonic_ns()
    claimed: list[dict] = []
    addressed, entry_count = addressed_records(root, recipient_kind, session_id)
    # The session's own records, then the records of a cleared predecessor at the same pane
    # that the seat handover bound to it. Those stay stored (and acked) under the registered
    # parent; only the receiving session differs.
    for current in addressed:
        storage_key = current[STORAGE_KEY]
        delivery_id = current["delivery_id"]
        state = current.get("state")
        if state in {"claimed", "sent-ambiguous"}:
            try:
                pending_delivery.reclaim(root, storage_key, delivery_id, now_ns=now)
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
                storage_key,
                delivery_id,
                claim_owner=claim_owner,
                lease_seconds=DELIVER_LEASE_SECONDS,
                require_generation_proof=False,
            )
        except pending_delivery.PendingDeliveryError:
            continue
        from dispatch_notice_state import keep_claim
        try:
            if not keep_claim(root, storage_key, delivery_id, record, claim_owner):
                continue
        except OSError:
            continue
        # An answer another session sent: this session is the parent, so its carrier continues the route.
        from dispatch_supervision import CONTINUED_KEY, continue_for_parent
        continued = continue_for_parent(record, session_id=session_id, recipient_kind=recipient_kind)
        claimed.append({**record, STORAGE_KEY: storage_key, **({CONTINUED_KEY: continued} if continued else {})})
    elapsed_ns = time.monotonic_ns() - start_ns
    _append_self_instrumentation(root, elapsed_ns, entry_count, len(claimed))
    return claimed, entry_count


NOTICE_DELIVERY_HEADER = (
    "Hearting notice about work you started (a paused route resumed, a remote run ended). "
    "Tell the user in one line; act only where a line's required_action asks for it. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)


GATE_DELIVERY_HEADER = (
    "Hearting human gate awaiting your decision (SD-123/129). Read the named artifact "
    "(an interview file or a frame summary), put the [방향 확인] card and every interview "
    "question to the user through AskUserQuestion -- one topic at a time, in plain words, "
    "recommended answer first -- then record the answer with `workflow-supervisor.py "
    "release --route <route file> --gate <name> --decision proceed|revise|stop "
    "[--answers <file from frame_interview.py answers-template>]`. The owner is waiting on "
    "`await-release` or has paused at the gate; the release continues the work either way. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery (SD-111 durable pending records for this session). "
    "Follow each receipt's required_action. advance-completed confirms that attempt's "
    "completion: consume its result without another harvest, then follow the owning "
    "route's next-action receipt for any remaining stages. Runtime-v1 owners have "
    "runtime-owned close/finalize; a closed route needs no restart. "
    "inspect-recovery is a runtime diagnostic, not a request for a user decision. "
    "For other unresolved actions use the exact checked recovery surface. "
    "A receipt may repeat an earlier async notice; do not repeat an already handled action. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)


def _followup(root: Path, record: dict) -> str:
    """The exact next handle for one completion record (the queue carrier's text); never raises."""
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    if receipt.get("kind") in {"supervision", "notice"} or not isinstance(receipt.get("children"), list):
        return ""
    try:
        from dispatch_completion_join import completion_followup_text
        jobs = record_jobs(root, record)
        if jobs is None:
            return ""
        return completion_followup_text(
            receipt, jobs=str(jobs),
            surface=str(Path(__file__).resolve().parents[1] / "adapters" / "codex" / "bin" / "preflight.sh"))
    except Exception:  # noqa: BLE001 -- the bounded receipt line still reaches the parent
        return ""


def delivery_context(batches: list[tuple[Path, list[dict]]]) -> str:
    """The one text a parent receives for its delivered records, whichever runtime carries it.

    ``batches`` pairs each state root with the records claimed there. Gate notices are
    answered, completions are consumed, so they get separate instructions.
    """

    gate_lines: list[str] = []
    notice_lines: list[str] = []
    lines: list[str] = []
    followups: list[str] = []
    for root, records in batches:
        for record in records:
            text = _bounded_receipt_text(record)
            if is_human_gate_record(record):
                gate_lines.append(text)
                continue
            if (record.get("receipt") or {}).get("kind") == "notice":
                notice_lines.append(text)
                continue
            lines.append(text)
            follow = _followup(root, record)
            if follow and follow not in followups:
                followups.append(follow)
    blocks: list[str] = []
    if gate_lines:
        blocks.append(GATE_DELIVERY_HEADER + "\n" + "\n".join(f"- {line}" for line in gate_lines))
    if notice_lines:
        blocks.append(NOTICE_DELIVERY_HEADER + "\n" + "\n".join(f"- {line}" for line in notice_lines))
    if lines:
        blocks.append(COMPLETION_DELIVERY_HEADER + "\n" + "\n".join(f"- {line}" for line in lines)
                      + "".join("\n" + follow for follow in followups))
    return "\n\n".join(blocks)


def ack_delivered(root: Path, session_id: str, records: list[dict], *, acked_by: str) -> int:
    """Ack the records whose bounded receipt was injected; returns the count.

    A record a handover bound to this session is acked under the registered parent it is stored under."""

    acked = 0
    for record in records:
        try:
            pending_delivery.ack(
                root, record.get(STORAGE_KEY) or session_id, record["delivery_id"], acked_by=acked_by
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


def _state_roots() -> list[Path]:
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    return [Path(root) for root in dict.fromkeys(dispatch_state_roots(resolve_agent_home()))
            if Path(root).is_dir()]


def main(argv: list[str] | None = None) -> int:
    """A runtime carrier's steps for one session's records, as JSON on stdout.

    ``roots`` names the state roots; ``deliver`` claims what is owed to the session and renders it; the carrier then
    hands the text to its runtime and answers ``ack`` (taken) or ``release`` (not
    taken, so the next pass claims it again), passing ``deliver``'s records on stdin.
    """
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("action", choices=("roots", "activate", "deliver", "ack", "release"))
    parser.add_argument("--recipient-kind", default="")
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    if args.action == "roots":
        # Where this runtime's records live, so a carrier can look before it asks.
        print(json.dumps([str(root) for root in _state_roots()]))
        return 0
    if not args.session:
        parser.error("--session is required")
    if args.recipient_kind not in pending_delivery.RECIPIENT_KINDS:
        parser.error(f"unknown recipient kind {args.recipient_kind}")
    if args.action in {"activate", "deliver"}:
        batches = []
        for root in _state_roots():
            if args.action == "activate":
                records = activate(root, args.recipient_kind, args.session)
            else:
                records, _entries = sweep_deliver(root, args.recipient_kind, args.session)
            if records:
                batches.append((root, records))
        print(json.dumps({
            "text": delivery_context(batches),
            "records": [{"root": str(root), "storage_key": record.get(STORAGE_KEY) or args.session,
                         "delivery_id": record["delivery_id"], "claim_owner": record.get("claim_owner")}
                        for root, records in batches for record in records],
        }, ensure_ascii=False))
        return 0
    try:
        handed = json.load(sys.stdin).get("records") or []
    except (ValueError, AttributeError):
        handed = []
    done = 0
    for item in handed:
        try:
            root, key, delivery_id = Path(item["root"]), item["storage_key"], item["delivery_id"]
            if args.action == "ack":
                done += ack_delivered(root, args.session, [{"delivery_id": delivery_id, STORAGE_KEY: key}],
                                      acked_by=f"{args.recipient_kind}:{args.session}")
            else:
                pending_delivery.release_claim(root, key, delivery_id, claim_owner=item["claim_owner"])
                done += 1
        except (KeyError, TypeError, OSError, pending_delivery.PendingDeliveryError):
            continue
    print(json.dumps({"action": args.action, "count": done}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
