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
Enumeration is bounded to the caller's own key and the confirmed predecessor
keys returned by ``storage_recipients`` (O(addressed records), §13.33.1-(3));
this module never scans unrelated session directories and never reads ``jobs.log``.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_pending_delivery as pending_delivery  # noqa: E402
from dispatch_recipient_receipt import IDENTITY_KEY  # noqa: E402

SWEEP_LEASE_SECONDS = 30.0
LOG_FILENAME = "dispatch-session-sweep.log"


def request_obligation_reconciliation(*, lifecycle_root: str | Path | None = None) -> None:
    """Ask the common recovery entry to reconnect observers without holding a hook."""
    try:
        import peer_obligations
        root = lifecycle_root or Path(__file__).resolve().parents[1]
        peer_obligations.request_obligation_recovery(lifecycle_root=root)
    except Exception:
        pass


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
        from dispatch_observation_log import append_record
        append_record(root_path, LOG_FILENAME, {
            "ts_ns": time.time_ns(),
            "elapsed_ns": elapsed_ns,
            "entries": entry_count,
            "claimed": 1 if claimed_count else 0,
        })
    except OSError:
        pass


DELIVER_LEASE_SECONDS = 120.0
# Claude cancels a prompt hook at 10 s and then nothing reaches the turn or is acked,
# so the same records come back on every prompt. Records not reached within this
# budget stay pending for the next prompt instead.
DELIVER_BUDGET_SECONDS = 6.0
# One pass-wide deadline covers discovery and claiming inside the configured
# 10 s prompt hook; no new work starts after it. Rendering and ACK of what the
# pass already holds run after it, so the remaining 4 s is their reserve under
# load (HOOK-LATENCY 7-4; 8 s left 2 s and was decided down on 2026-11-04).
ACTION_BUDGET_SECONDS = 6.0
STORAGE_KEY = "_storage_key"     # added to a returned copy only: the directory a record is stored under


def _retry_context(recipient_kind: str, session_id: str,
                   storage_key: str, record: dict) -> str:
    """Stable evidence for this recipient and durable delivery target."""

    return ":".join(str(value or "") for value in (
        recipient_kind, session_id, storage_key, record.get("recipient_digest"),
        record.get("session_generation"), record.get("session_generation_supported"),
        record.get("delivery_id"), record.get("receipt_digest"),
    ))


HUMAN_GATE_PREFIX = "human-gate:"
NOTICE_CONTEXT_KEY = "_notice_read_context"


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
        from dispatch_notice_state import NoticeReadContext
        context = record.get(NOTICE_CONTEXT_KEY)
        return render_text(receipt, continued=record.get(CONTINUED_KEY),
            notice_context=context if isinstance(context, NoticeReadContext) else None) + _timestamp_note(record)
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


def _timestamp_note(record: dict) -> str:
    timeline = record.get("timestamp_provenance")
    if not isinstance(timeline, dict):
        return " Timing provenance is unknown for this legacy notice; do not infer its original send time."
    def value(key):
        result = timeline.get(key)
        return result if isinstance(result, str) and result else "unknown"
    return (" Timing (UTC wall clock; monotonic lease values remain separate): notice record materialized "
            + value("record_materialized_at_utc") + "; first observed send "
            + value("first_send_at_utc") + "; latest observed send " + value("last_send_at_utc")
            + "; exact receipt " + value("receive_at_utc") + "; observed sends="
            + str(timeline.get("observed_send_count", "unknown")) + ", redeliveries="
            + str(timeline.get("observed_redelivery_count", "unknown")) + "; materializer="
            + value("record_materialized_source") + "; first-send source="
            + value("first_send_source") + "; latest-send source=" + value("last_send_source")
            + "; receipt source=" + value("receive_source") + ". Missing legacy values stay unknown.")


def _recipient_harness(recipient_kind: str) -> str | None:
    for harness in ("claude", "codex", "opencode"):
        if recipient_kind.startswith(harness):
            return harness
    return None


class AddressedRecords(list):
    """The records discovery reached, plus whether the bounded scan finished.

    ``complete=False`` means the pass time budget ran out. Delivery keeps what
    it reached and leaves the rest pending; nothing publishes a partial scan as
    a current inbox.
    """

    __slots__ = ("complete", "stale_inbox")

    def __init__(self, items=(), *, complete: bool = True, stale_inbox: bool = False):
        super().__init__(items)
        self.complete = complete
        self.stale_inbox = stale_inbox


def _inbox_root(root: Path) -> Path:
    """The dispatch root inboxes live under for one read root."""
    try:
        import dispatch_recipient_inbox as inbox
        return Path(inbox.dispatch_inbox_root(jobs=Path(root) / "jobs.log"))
    except Exception:  # noqa: BLE001 - derived discovery never owns the pass
        return Path(root)


def _directory_identity(path: Path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return None
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def _record_mtime_ns(root: Path, storage_key: str, delivery_id: str):
    try:
        info = os.lstat(pending_delivery.record_path(root, storage_key, delivery_id))
    except (OSError, pending_delivery.PendingDeliveryError):
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return info.st_mtime_ns


def _scope_entries(root: Path, storage_key: str) -> list[tuple[str, str]]:
    try:
        directory = pending_delivery.record_directory(root, storage_key)
        return [(storage_key, path.stem) for path in sorted(directory.glob("delivery-*.json"))
                if path.is_file() and not path.is_symlink()]
    except (OSError, pending_delivery.PendingDeliveryError):
        return []


def _scope_entries_newest_first(root: Path, storage_key: str) -> list[tuple[str, str]]:
    """Scope entries ordered by record mtime, newest first.

    A stale pass may be cut by the shared deadline. The previous inbox's named
    records are read first; the remaining directory entries follow newest
    first so a record created after publication is reached before an old
    sorted prefix of terminal records.
    """
    try:
        directory = pending_delivery.record_directory(root, storage_key)
        entries = []
        for path in directory.iterdir():
            if not path.name.startswith("delivery-") or path.suffix != ".json":
                continue
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            entries.append((info.st_mtime_ns, path.name, path.stem))
    except (OSError, pending_delivery.PendingDeliveryError):
        return []
    entries.sort(reverse=True)
    return [(storage_key, stem) for _mtime_ns, _name, stem in entries]


def _read_addressed(
    root: Path,
    recipient_kind: str,
    session_id: str,
    harness: str,
    candidates,
    *,
    read_context,
    deadline_ns: int | None,
):
    """Read the candidate records this session currently answers for.

    ``candidates`` are ``(storage_key, delivery_id)`` discovery hints from the
    inbox or the session's own scopes. Authority is always re-read per record
    through ``record_for_session``; a hint never substitutes for it.
    """
    from dispatch_seat_handover import record_for_session

    addressed: list[dict] = []
    open_entries: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    count = 0
    complete = True
    # One discovery pass observes one registry generation; the effect group
    # below opens its own scope because a claim is an effect boundary.
    with _registry_read_scope():
        for storage_key, delivery_id in candidates:
            if (storage_key, delivery_id) in seen:
                continue
            seen.add((storage_key, delivery_id))
            if deadline_ns is not None and time.monotonic_ns() >= deadline_ns:
                complete = False
                break
            try:
                record = pending_delivery.read(root, storage_key, delivery_id)
            except (OSError, pending_delivery.PendingDeliveryError):
                continue
            count += 1
            if not record or record.get("state") not in pending_delivery.OPEN_STATES:
                continue
            if not (record.get("recipient_kind") == recipient_kind or storage_key != session_id):
                continue
            # The inbox names every open record in the recipient's scopes; each
            # pass still re-reads authority below before it hands one over.
            mtime_ns = _record_mtime_ns(root, storage_key, delivery_id)
            if mtime_ns is not None:
                open_entries.append((storage_key, delivery_id, mtime_ns))
            try:
                if not record_for_session(record, None, session=session_id, harness=harness,
                                          storage_key=storage_key, jobs=record_jobs(root, record),
                                          read_context=read_context):
                    continue
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                continue
            addressed.append({**record, STORAGE_KEY: storage_key})
    return addressed, open_entries, count, complete


def _inbox_payload(root: Path, session_id: str, harness: str, scopes, open_entries,
                   *, edge_generation: int, folded_edge_digest, inbox_root: Path | None = None) -> dict:
    """Describe the exact open-record set and the scopes that can change it.

    ``edge_generation`` is read by the caller before it enumerated ``scopes`` and
    ``folded_edge_digest`` digests the edges of that same observation (the
    scopes' own, or the previous inbox's for a stale republish). Nothing here
    reads the lineage graph. Publication is refused when the generation moved
    since the caller's capture, so old scopes never carry a newer edge key.
    """
    import dispatch_recipient_inbox as inbox

    generation_root = inbox_root if inbox_root is not None else root
    identities = {}
    storage_keys = [session_id]
    candidates = []
    for storage_key, allowed in scopes:
        if storage_key not in storage_keys:
            storage_keys.append(storage_key)
        try:
            attempts = sorted(str(item) for item in (allowed or ()))
        except TypeError:
            attempts = []
        candidates.append({"storage_key": storage_key, "attempts": attempts})
    for storage_key in storage_keys:
        try:
            directory = pending_delivery.record_directory(root, storage_key)
        except pending_delivery.PendingDeliveryError:
            continue
        # Record every candidate scope, including one that does not exist yet.
        # `is_current` then requires an absent directory to still be absent, so
        # a record created there after publication invalidates the inbox.
        identities[str(directory)] = _directory_identity(directory)
    if inbox.read_edge_generation(generation_root) != edge_generation:
        raise OSError("lineage-generation-changed")
    return {
        "edge_generation": edge_generation,
        "folded_edge_digest": folded_edge_digest,
        "dir_identities": identities,
        "delivery_ids": [{"storage_key": storage_key, "delivery_id": delivery_id,
                          "mtime_ns": mtime_ns}
                         for storage_key, delivery_id, mtime_ns in open_entries],
        "candidates": candidates,
        "observed_at_ns": time.time_ns(),
    }


def scopes_edge_digest(scopes) -> str:
    """Digest of the lineage observation ``scopes`` were enumerated from.

    Raises when the scopes carry no observation, so such a set is never
    published (a digest from a later graph would hide a newer edge).
    """
    import dispatch_recipient_inbox as inbox

    edges = getattr(scopes, "edges", None)
    if edges is None:
        raise OSError("scopes-without-lineage-observation")
    return inbox.edge_digest(edges)


def addressed_records(root: Path, recipient_kind: str, session_id: str, *, read_context=None,
                      deadline_ns: int | None = None) -> tuple[list[dict], int]:
    """Read this session's open notices from the derived inbox, never globally.

    A current inbox names exactly the open records to open (``delivery_ids``).
    A stale inbox keeps its previous names plus the session's own scope and
    leaves the global recomputation to the existing obligation runner. Only the
    one-time prefill (no inbox yet) derives the scopes once, within the pass
    deadline, and publishes them; a partial prefill is never published.
    """
    from dispatch_seat_handover import storage_recipients
    from peer_read_context import PeerReadContext
    import dispatch_recipient_inbox as inbox

    read_context = read_context or PeerReadContext(compact_lineage=True)
    harness = _recipient_harness(recipient_kind) or ""
    if not Path(root).is_dir():
        # A read-only fallback root that does not exist holds nothing to sweep;
        # discovery must not materialize it (or its derived inbox directory).
        return AddressedRecords(complete=True), 0
    inbox_root = _inbox_root(root)
    canonical = inbox_root == Path(root).resolve(strict=False)
    recorded = inbox.read_inbox(inbox_root, harness, session_id) if harness else None
    current = False
    if recorded is not None:
        try:
            current = inbox.is_current(recorded, inbox_root)
        except (OSError, ValueError, TypeError):
            current = False

    if current:
        candidates = [(item["storage_key"], item["delivery_id"])
                      for item in recorded["delivery_ids"]]
        # A scope directory that did not exist when the inbox was written cannot
        # be compared by identity; read the session's own directory until it is
        # recorded. Once recorded, its identity detects create/delete and the
        # named ids cover modification.
        try:
            own_directory = str(pending_delivery.record_directory(root, session_id))
        except pending_delivery.PendingDeliveryError:
            own_directory = ""
        if own_directory and own_directory not in recorded["dir_identities"]:
            candidates.extend(_scope_entries(root, session_id))
        addressed, _open, count, complete = _read_addressed(
            root, recipient_kind, session_id, harness, candidates,
            read_context=read_context, deadline_ns=deadline_ns)
        return AddressedRecords(addressed, complete=complete), count

    # No current inbox. Reuse what a previous inbox named, every scope
    # directory it recorded, and always add the session's own scope. A new
    # record under a cleared predecessor's key changes only that directory, so
    # naming its scope is what keeps the next completion visible. This is still
    # only the session's share; a missing or stale inbox never recomputes
    # globally and never calls `storage_recipients`. Read the previous inbox's
    # named records first, then the newest scope entries: a pass cut by the
    # deadline must reach a record created after publication instead of an old
    # sorted prefix of terminal records.
    stale_scopes = []
    if recorded is not None:
        seen_scopes = set()
        for item in recorded.get("candidates") or ():
            storage_key = item.get("storage_key") if isinstance(item, dict) else None
            if (not isinstance(storage_key, str) or not storage_key
                    or storage_key in seen_scopes):
                continue
            seen_scopes.add(storage_key)
            attempts = item.get("attempts") if isinstance(item.get("attempts"), list) else []
            stale_scopes.append((storage_key, tuple(attempts)))
        if session_id not in seen_scopes:
            stale_scopes.append((session_id, ()))
    prefill = recorded is None and harness and canonical
    candidates = []
    if recorded is not None:
        candidates.extend(
            (item["storage_key"], item["delivery_id"])
            for item in recorded.get("delivery_ids") or ()
            if isinstance(item, dict) and isinstance(item.get("storage_key"), str)
            and item["storage_key"] and isinstance(item.get("delivery_id"), str)
            and item["delivery_id"])
    if not prefill:
        for storage_key, _allowed in stale_scopes:
            candidates.extend(_scope_entries_newest_first(root, storage_key))
        candidates.extend(_scope_entries_newest_first(root, session_id))

    if prefill:
        # Capture the edge generation before enumerating scopes: an edge writer
        # that completes after this point makes the final recheck refuse the
        # publication instead of stamping old scopes with its newer generation.
        edge_generation = inbox.read_edge_generation(inbox_root)
        with _registry_read_scope():
            scopes = storage_recipients(
                session_id, harness=harness, read_context=read_context,
                deadline_ns=deadline_ns if deadline_ns is not None
                else time.monotonic_ns() + int(DELIVER_BUDGET_SECONDS * 1_000_000_000))
            complete_scopes = bool(getattr(scopes, "complete", True))
            scoped_candidates = []
            for storage_key, _allowed in scopes:
                scoped_candidates.extend(_scope_entries(root, storage_key))
        if session_id not in {key for key, _allowed in scopes}:
            # An unreadable full enumeration still leaves the session's own
            # scope deliverable (its records need no lineage authority).
            candidates.extend(_scope_entries(root, session_id))
        candidates.extend(scoped_candidates)
        addressed, open_entries, count, complete = _read_addressed(
            root, recipient_kind, session_id, harness, candidates,
            read_context=read_context, deadline_ns=deadline_ns)
        complete = complete and complete_scopes
        if complete and open_entries is not None:
            try:
                payload = _inbox_payload(
                    root, session_id, harness, scopes, open_entries,
                    edge_generation=edge_generation,
                    folded_edge_digest=scopes_edge_digest(scopes), inbox_root=inbox_root)
                inbox.write_inbox(inbox_root, harness, session_id, payload)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                pass
        return AddressedRecords(addressed, complete=complete,
                                stale_inbox=not complete), count

    addressed, open_entries, count, complete = _read_addressed(
        root, recipient_kind, session_id, harness, candidates,
        read_context=read_context, deadline_ns=deadline_ns)
    # Reading order is a deadline concern only; the returned order stays the
    # previous scope-then-record-name order so batch grouping and rendering do
    # not change just because a stale pass read the newest entries first.
    scope_rank = {key: index for index, (key, _allowed) in enumerate(stale_scopes)}
    addressed.sort(key=lambda record: (
        scope_rank.get(record.get(STORAGE_KEY), len(scope_rank)), record["delivery_id"]))
    open_entries.sort(key=lambda item: (
        scope_rank.get(item[0], len(scope_rank)), item[1]))
    if complete and recorded is not None:
        # The pass observed the whole share. Republish only under the inbox's own
        # edge generation and its own folded edge digest: this pass never read
        # the lineage graph, so it cannot vouch for edges newer than the scopes
        # it re-read. A missed generation bump therefore stays visible to the
        # runner's digest comparison, which repairs the session.
        try:
            recorded_generation = recorded.get("edge_generation")
            if (isinstance(recorded_generation, int) and not isinstance(recorded_generation, bool)
                    and inbox.read_edge_generation(inbox_root) == recorded_generation):
                payload = _inbox_payload(
                    root, session_id, harness, stale_scopes, open_entries,
                    edge_generation=recorded_generation,
                    folded_edge_digest=recorded.get("folded_edge_digest"),
                    inbox_root=inbox_root)
                inbox.write_inbox(inbox_root, harness, session_id, payload)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            pass
    return AddressedRecords(addressed, complete=complete, stale_inbox=recorded is not None), count


def _registry_read_scope():
    from dispatch_registry_cache import registry_read_scope
    return registry_read_scope()



def activate(root: Path, recipient_kind: str, session_id: str, *,
             request_recovery: bool = True, notice_context=None,
             deadline_ns: int | None = None) -> list[dict]:
    """Retain this session's duties and request maintenance from its existing runner."""
    records, _ = addressed_records(root, recipient_kind, session_id, deadline_ns=deadline_ns)
    from peer_obligations import retain_ordinary_notice, retain_registered_completion
    for record in records:
        jobs = record_jobs(root, record)
        if jobs is None:
            continue
        if (record.get("receipt") or {}).get("kind") == "notice":
            try:
                retain_ordinary_notice(jobs, record)
            except (OSError, ValueError, RuntimeError):
                continue
            continue
        for attempt in record.get("attempt_ids") or []:
            try:
                retain_registered_completion(jobs, attempt, session_id, recipient_kind)
            except (OSError, ValueError):
                continue
    if request_recovery:
        _reconcile_canonical_dispatch_state(root)
    from dispatch_notice_state import NoticeReadContext, notice_is_current
    notice_context = notice_context or NoticeReadContext()
    visible = []
    for record in records:
        try:
            if not notice_is_current(record, jobs=record_jobs(root, record), notice_context=notice_context):
                continue
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass  # Unknown is still pending; activation never claims/rejects/ACKs.
        visible.append({**record, NOTICE_CONTEXT_KEY: notice_context})
    return visible


def carrier_view(root: Path, record: dict, storage_key: str, identity: dict, *, notice_context=None) -> dict:
    """The bound in-memory copy a carrier renders for one stored record.

    The same parent-close projection as the original claim applies, so a
    re-rendered or reconstructed body matches what that claim sent. An
    unreadable registry keeps the stored view; a pending close still raises.
    """
    from dispatch_notice_state import project_parent_close
    from dispatch_notice_state import NoticeReadContext
    view = {**record, STORAGE_KEY: storage_key, IDENTITY_KEY: identity,
            NOTICE_CONTEXT_KEY: notice_context or NoticeReadContext()}
    try:
        project_parent_close(root, view, jobs=record_jobs(root, record))
    except (OSError, ValueError):
        pass
    return view


def _reconcile_canonical_dispatch_state(root: Path) -> None:
    """Request recovery without putting global settlement ahead of delivery."""
    try:
        from peer_obligations import request_dispatch_reconciliation
        request_dispatch_reconciliation(root)
    except Exception:
        pass


def record_jobs(root: Path, record: dict, *, observed_jobs=None) -> Path | None:
    """Use the receipt, observed registry, then canonical selection, preserving its filename."""
    from dispatch_contract import resolve_agent_home, resolve_global_registry
    receipt = record.get("receipt") or {}
    raw = receipt.get("job_registry")
    try:
        if (record.get("receipt") or {}).get("kind") == "notice":
            state_root = Path(root).resolve(strict=True)
            import session_notice
            for value in record.get("lineage", []):
                if not isinstance(value, str) or not value.startswith(session_notice.LINEAGE_PREFIX):
                    continue
                try:
                    stored = json.loads(value[len(session_notice.LINEAGE_PREFIX):])
                    raw_jobs = stored.get("jobs") if isinstance(stored, dict) else None
                    candidate = Path(raw_jobs).expanduser() if isinstance(raw_jobs, str) else None
                    if (candidate and candidate.is_absolute() and not candidate.is_symlink()
                            and candidate.resolve(strict=False).parent == state_root):
                        return candidate.resolve(strict=False)
                except (OSError, ValueError, TypeError):
                    continue
            return state_root / "jobs.log"
        if isinstance(raw, str) and raw:
            jobs = Path(raw).expanduser()
        else:
            jobs = (Path(observed_jobs) if observed_jobs is not None else
                    resolve_global_registry(resolve_agent_home(), None, 0, "read").path)
        if jobs.is_symlink():
            return None
        jobs = jobs.resolve(strict=True)
        if not jobs.is_file() or jobs.parent != Path(root).resolve():
            return None
        return jobs
    except (OSError, RuntimeError, ValueError):
        return None


def retain_receiving_turn(batches: list[tuple[Path, list[dict]]], session_id: str,
                          receiving_turn: dict | None, *,
                          request_context: str | None = None) -> set[tuple[str, str]]:
    """Persist exact completion/turn ownership before a Codex prompt is ACKed.

    Returned keys identify records that the hook may ACK after successful
    rendering. Non-completion notices need no follow-through duty.
    """
    ackable: set[tuple[str, str]] = set()
    for root, records in batches:
        for record in records:
            delivery_id = record.get("delivery_id")
            receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
            children = receipt.get("children")
            if not isinstance(children, list):
                if isinstance(delivery_id, str):
                    ackable.add((str(root), delivery_id))
                continue
            attempt_ids = record.get("attempt_ids")
            if (not isinstance(attempt_ids, list) or not attempt_ids
                    or not isinstance(delivery_id, str) or receiving_turn is None):
                continue
            receipt_attempts = {child.get("attempt_id") for child in children
                                if isinstance(child, dict)}
            if any(attempt not in receipt_attempts for attempt in attempt_ids):
                continue
            jobs = record_jobs(root, record)
            if jobs is None:
                continue
            from peer_obligations import retain_registered_completion
            retained = True
            for attempt in attempt_ids:
                try:
                    if not retain_registered_completion(
                            jobs, attempt, session_id, record.get("recipient_kind", ""),
                            delivery_record=record, receiving_turn=receiving_turn,
                            request_context=request_context):
                        retained = False
                        break
                except (OSError, ValueError, RuntimeError):
                    retained = False
                    break
            if retained:
                ackable.add((str(root), delivery_id))
    return ackable


def sweep_deliver(
    root: Path, recipient_kind: str, session_id: str, *, now_ns: int | None = None,
    native_evidence: dict | None = None, receipt_observer=None,
    notice_context=None, deadline_ns: int | None = None, read_context=None,
    select=None,
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

    ``select``, when given, is a predicate on an addressed record: a record it
    rejects is not claimed here and stays with its existing carriers.

    Returns ``(claimed_records, entry_count)``; never raises.
    """

    now = time.monotonic_ns() if now_ns is None else now_ns
    start_ns = time.monotonic_ns()
    limit_ns = start_ns + int(DELIVER_BUDGET_SECONDS * 1_000_000_000)
    if deadline_ns is not None:
        limit_ns = min(limit_ns, int(deadline_ns))

    def over_budget() -> bool:
        return time.monotonic_ns() > limit_ns

    from dispatch_notice_state import NoticeReadContext
    from dispatch_seat_handover import record_for_session
    from peer_read_context import PeerReadContext
    notice_context = notice_context or NoticeReadContext()
    # One discovery context for the whole pass; the post-claim effect section
    # opens its own per group (a claim is an effect boundary).
    discovery_context = read_context or PeerReadContext(compact_lineage=True)
    claimed: list[dict] = []
    addressed, entry_count = addressed_records(
        root, recipient_kind, session_id, read_context=discovery_context,
        deadline_ns=deadline_ns)
    addressed = [{**record, NOTICE_CONTEXT_KEY: notice_context} for record in addressed]
    from dispatch_recipient_receipt import settle_native_receipt
    harness = ("claude" if recipient_kind.startswith("claude") else
               "codex" if recipient_kind.startswith("codex") else
               "opencode" if recipient_kind.startswith("opencode") else "")
    legacy_bindings_by_evidence: dict[int, tuple[dict, dict]] = {}
    visited_batch_ids: set[str] = set()
    observed_evidence: dict[int, dict] = {}

    def currently_addressed(record, storage_key, context=None):
        from dispatch_seat_handover import record_for_session
        # Authority is re-read per record; the inbox and the enumeration scopes
        # are discovery hints, never permission. Outside the effect group this
        # call is its own one-record discovery scope; inside it, the group's
        # scope is already active and this is a no-op.
        with _registry_read_scope():
            return record_for_session(
                record, None, session=session_id, harness=harness,
                storage_key=storage_key, jobs=record_jobs(root, record),
                read_context=context or discovery_context)

    def settle_observed(record):
        evidence = native_evidence
        if receipt_observer is not None:
            try:
                evidence = receipt_observer(root, record.get(STORAGE_KEY) or session_id, record)
            except Exception:
                evidence = None
        if isinstance(evidence, dict):
            observed_evidence[id(evidence)] = evidence
        return settle_evidence(record, evidence)

    def settle_evidence(record, evidence):
        if harness == "claude" and isinstance(evidence, dict):
            key = id(evidence)
            cached = legacy_bindings_by_evidence.get(key)
            if cached is None or cached[0] is not evidence:
                try:
                    from dispatch_recipient_receipt import legacy_claude_receipt_bindings
                    bindings = legacy_claude_receipt_bindings(
                        root, recipient_kind, session_id, addressed, evidence)
                except Exception:
                    bindings = {}
                legacy_bindings_by_evidence[key] = (evidence, bindings)
            storage = record.get(STORAGE_KEY) or session_id
            legacy_binding = legacy_bindings_by_evidence[key][1].get(
                (storage, record.get("delivery_id")))
            if legacy_binding is not None:
                evidence = {**evidence,
                            "bindings": [*(evidence.get("bindings") or []), legacy_binding]}
        try:
            return settle_native_receipt(
                root, record.get(STORAGE_KEY) or session_id, harness, session_id,
                record, evidence,
                acked_by=f"native-receipt:{harness}:{session_id}")
        except Exception:
            return {"received": False, "settled": False,
                    "reason": "native-receipt-unknown"}

    def unsubmitted(record, storage_key):
        fence = pending_delivery.read_submission_fence(
            root, storage_key, record["delivery_id"], expected_identity=
            pending_delivery.submission_fence_identity(record, session_id))
        return fence is None or fence.get("state") == "not-accepted"

    def release_group(records, storage_key, claim_owner, identities):
        for member in records:
            member_id = member["delivery_id"]
            try:
                fresh = pending_delivery.read(root, storage_key, member_id)
                if (fresh is not None and fresh.get("state") == "claimed"
                        and fresh.get("claim_owner") == claim_owner
                        and pending_delivery.received_ack_identity(
                            fresh, storage_key, member_id) == identities[member_id]
                        and unsubmitted(fresh, storage_key)):
                    pending_delivery.release_claim(root, storage_key, member_id,
                        claim_owner=claim_owner, expected_identity=identities[member_id])
            except (OSError, pending_delivery.PendingDeliveryError):
                pass

    # The session's own records, then the records of a cleared predecessor at the same pane
    # that the seat handover bound to it. Those stay stored (and acked) under the registered
    # parent; only the receiving session differs.
    for current in addressed:
        if "answer_binding" in (current.get("receipt") or {}):
            # Message answers use the retained peer courier on every harness:
            # its exact consumed envelope owns completion. A context-only sweep
            # must not turn an answer into an ordinary runtime prompt or ACK.
            continue
        if current.get("delivery_id") in visited_batch_ids:
            continue
        if select is not None and not select(current):
            continue
        if claimed and over_budget():
            break
        storage_key = current[STORAGE_KEY]
        delivery_id = current["delivery_id"]
        observed_evidence.clear()
        if settle_observed(current)["received"]:
            continue
        if not currently_addressed(current, storage_key):
            continue
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
        receipt = current.get("receipt") or {}
        standalone_notice = receipt.get("kind") == "notice"
        jobs = record_jobs(root, current)
        if jobs is None and not receipt.get("job_registry") and not standalone_notice:
            continue
        try:
            from dispatch_notice_state import resolve_delivery_batch
            batch = resolve_delivery_batch(jobs, current, recipient_key=storage_key,
                                           delivery_root=root) if jobs is not None else None
            batch_ids = tuple(batch.get("delivery_ids", ())) if batch else ()
            if batch and batch.get("held"):
                visited_batch_ids.update(batch.get("member_delivery_ids", ()))
                continue
            if (batch and len(batch_ids) > 1
                    and over_budget()):
                continue
            group_claims = pending_delivery._claim_ready_batch_snapshot(
                root, storage_key, delivery_id, claim_owner=claim_owner,
                lease_seconds=DELIVER_LEASE_SECONDS, require_generation_proof=False,
                jobs=jobs, retry_context=_retry_context(recipient_kind, session_id, storage_key, current),
                seed=current, batch=batch,
            )
        except pending_delivery.PendingDeliveryError:
            continue
        group_ids = {member.get("delivery_id") for member in group_claims}
        visited_batch_ids.update(group_ids)
        group_identities = {member["delivery_id"]: pending_delivery.received_ack_identity(
            member, storage_key, member["delivery_id"]) for member in group_claims}
        # Check elapsed at the group boundary. A group that crossed the
        # six-second carrier budget while claiming is released as a whole.
        if len(group_claims) > 1 and over_budget():
            release_group(group_claims, storage_key, claim_owner, group_identities)
            continue
        group_output = []
        group_ok = True
        authority_changed = False
        from dispatch_notice_state import keep_claim
        with _registry_read_scope():
            # One effect context and one pinned registry generation per group.
            effect_context = PeerReadContext(compact_lineage=True)
            for record in group_claims:
                member_id = record["delivery_id"]
                expected_identity = group_identities[member_id]
                try:
                    if not record_for_session(
                            record, None, session=session_id, harness=harness,
                            storage_key=storage_key, jobs=jobs, read_context=effect_context):
                        group_ok = False
                        break
                    if not keep_claim(root, storage_key, member_id, record, claim_owner, jobs=jobs,
                                      notice_context=notice_context):
                        group_ok = False
                        break
                except OSError:
                    group_ok = False
                    break
                fresh = pending_delivery.read(root, storage_key, member_id)
                if (fresh is None or pending_delivery.received_ack_identity(
                        fresh, storage_key, member_id) != expected_identity
                        or fresh.get("claim_owner") != claim_owner
                        or settle_observed({**fresh, STORAGE_KEY: storage_key})["received"]):
                    group_ok = False
                    break
                final = pending_delivery.read(root, storage_key, member_id)
                if (final is None or pending_delivery.received_ack_identity(
                        final, storage_key, member_id) != expected_identity
                        or final.get("claim_owner") != claim_owner):
                    group_ok = False
                    break
                if not currently_addressed(final, storage_key, effect_context):
                    group_ok = False
                    authority_changed = True
                    break
                from dispatch_supervision import CONTINUED_KEY, continue_for_parent
                continued = continue_for_parent(record, session_id=session_id,
                                                recipient_kind=recipient_kind)
                group_output.append({**record, STORAGE_KEY: storage_key, IDENTITY_KEY: expected_identity,
                                     NOTICE_CONTEXT_KEY: notice_context,
                                     **({CONTINUED_KEY: continued} if continued else {})})
            # A later native snapshot can contain an earlier member's exact receipt,
            # even when its claim/recipient changed or the ACK write must be retried.
            # Reuse this group's observations without starting another native read
            # mid-check. A later unavailable/empty snapshot cannot undo exact proof.
            for member in group_claims:
                member_id = member["delivery_id"]
                try:
                    fresh = pending_delivery.read(root, storage_key, member_id)
                    if (fresh is None or pending_delivery.received_ack_identity(
                            fresh, storage_key, member_id) != group_identities[member_id]):
                        group_ok = False
                        continue
                    if any(settle_evidence({**fresh, STORAGE_KEY: storage_key}, evidence)["received"]
                           for evidence in observed_evidence.values()):
                        group_ok = False
                except (OSError, pending_delivery.PendingDeliveryError):
                    group_ok = False
            # Every native observation and continuation above can change any member.
            # Validate the whole group here; no native callback or continuation may
            # intervene between these current checks and handing out the group.
            if group_ok:
                for member in group_claims:
                    member_id = member["delivery_id"]
                    try:
                        final = pending_delivery.read(root, storage_key, member_id)
                        deadline = final.get("claim_deadline_ns") if final is not None else None
                        if (final is None or pending_delivery.received_ack_identity(
                                final, storage_key, member_id) != group_identities[member_id]
                                or final.get("state") != "claimed"
                                or final.get("claim_owner") != claim_owner
                                or type(deadline) is not int or deadline <= time.monotonic_ns()
                                or not unsubmitted(final, storage_key)):
                            group_ok = False
                            break
                        if not currently_addressed(final, storage_key, effect_context):
                            group_ok = False
                            authority_changed = True
                            break
                    except (OSError, pending_delivery.PendingDeliveryError):
                        group_ok = False
                        break
        if len(group_claims) > 1 and over_budget():
            group_ok = False
        if not group_ok:
            if len(group_claims) == 1 and not authority_changed:
                continue  # Preserve the established singleton recovery lease.
            release_group(group_claims, storage_key, claim_owner, group_identities)
            continue
        claimed.extend(group_output)
    elapsed_ns = time.monotonic_ns() - start_ns
    _append_self_instrumentation(root, elapsed_ns, entry_count, len(claimed))
    return claimed, entry_count


NOTICE_DELIVERY_HEADER = (
    "Hearting notice about work you started (a paused route resumed, a remote run ended). "
    "Report observed metrics, trend, and elapsed or ETA against the criteria and plan; state when unavailable. "
    "Act only where a line's required_action asks for it. "
    "Do not start a separate harness-scheduled re-invocation, Monitor, dispatch-wait, or a polling loop."
)


GATE_DELIVERY_HEADER = (
    "Hearting human gate awaiting your decision (SD-123/129). Read the named artifact "
    "(an interview file or a frame summary), put the [방향 확인] card and every interview "
    "question to the user through AskUserQuestion -- one topic at a time, in plain words, "
    "recommended answer first -- then record the answer with `workflow-supervisor.py "
    "release --route <route file> --gate <name> --decision proceed|revise|stop "
    "[--answers <file from frame_interview.py answers-template>]`. The owner is waiting on "
    "`await-release` or has paused at the gate; the release continues the work either way. "
    "Do not start a separate harness-scheduled re-invocation, Monitor, dispatch-wait, or a polling loop."
)
COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery. Follow required_action/next-action receipt; "
    "inspect-recovery: diagnostic. runtime-v1 finalizes. Tracked-Workflow Continuation. "
    "Keep new failures/conflicts, unfinished work, questions; no extra "
    "harness-scheduled calls/Monitor/dispatch-wait/polling."
)
# Frozen headers from the previous short rendering; receipt recovery only.
_PREVIOUS_NOTICE_DELIVERY_HEADER = (
    "Hearting notice about work you started (a paused route resumed, a remote run ended). "
    "Report observed metrics, trend, and elapsed or ETA against the criteria and plan; state when unavailable. "
    "Act only where a line's required_action asks for it. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_PREVIOUS_GATE_DELIVERY_HEADER = (
    "Hearting human gate awaiting your decision (SD-123/129). Read the named artifact "
    "(an interview file or a frame summary), put the [방향 확인] card and every interview "
    "question to the user through AskUserQuestion -- one topic at a time, in plain words, "
    "recommended answer first -- then record the answer with `workflow-supervisor.py "
    "release --route <route file> --gate <name> --decision proceed|revise|stop "
    "[--answers <file from frame_interview.py answers-template>]`. The owner is waiting on "
    "`await-release` or has paused at the gate; the release continues the work either way. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_PREVIOUS_COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery. Follow required_action; inspect-recovery is diagnostic. "
    "Follow route's next-action receipt; runtime-v1 owners close/finalize. "
    "Tracked-Workflow Continuation. Keep new failures/conflicts, unfinished work, questions; "
    "no Monitor/dispatch-wait/polling."
)
_LEGACY_COMPLETION_DELIVERY_HEADER = (
    "Hearting runtime delivery (SD-111 durable pending records for this session). "
    "Follow each receipt's required_action. advance-completed confirms that attempt's "
    "completion: read its exact result and perform already approved parent review/follow-through "
    "this turn or its existing registered continuation. Receipt, queue consumption and ACK are not execution. "
    "On the first user-facing close of material work, explain what changed and why, observed before/after, "
    "checks actually run (including reused, skipped, and unrun checks), applicable commit/PR/deployment/serving, "
    "and limits or remaining work; do not invent deployment or operating results. Follow the owning "
    "route's next-action receipt for any remaining stages. Runtime-v1 owners have "
    "runtime-owned close/finalize; a closed route needs no restart. "
    "inspect-recovery is a runtime diagnostic, not a request for a user decision. "
    "For other unresolved actions use the exact checked recovery surface. "
    "An automatic repeat of the same exact result or cleanup-only notice after follow-through was handled "
    "does not repeat the action or request a new user ACK; preserve new failures, conflicts, unfinished work, "
    "and direct user questions. "
    "Do not start Monitor, dispatch-wait, or a polling loop."
)
_CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX = (
    "Original request/format apply. Review result; do approved follow-through or existing "
    "continuation. First close: changes/why, before/after, checks run/reused/skipped/unrun, "
    "commit/PR/deployment/serving, limits. No harvest/restart/manual finalize. "
    "Receipt/ACK isn't execution; handled exact repeats need no ACK."
)
_LEGACY_RUNTIME_V1_FOLLOWUP = (
    "The requested work is complete, including its declared stages and runtime workflow/route/cycle settlement. "
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
)


def _previous_opencode_delivery_body(text: str, *, legacy_completion: bool = False) -> str:
    """Restore only renderer-owned block headers, keeping receipt content exact."""
    from dispatch_recipient_receipt import OPENCODE_MARKER_LINE_RE
    headers = {
        GATE_DELIVERY_HEADER: _PREVIOUS_GATE_DELIVERY_HEADER,
        NOTICE_DELIVERY_HEADER: _PREVIOUS_NOTICE_DELIVERY_HEADER,
        COMPLETION_DELIVERY_HEADER: (_LEGACY_COMPLETION_DELIVERY_HEADER if legacy_completion
                                     else _PREVIOUS_COMPLETION_DELIVERY_HEADER),
    }
    blocks = []
    previous = None
    for block in text.split("\n\n"):
        header, separator, body = block.partition("\n")
        # Gate/notice blocks end at their last receipt marker. Blank lines inside
        # a receipt or follow-up do not turn payload text into another header.
        if previous is None or OPENCODE_MARKER_LINE_RE.fullmatch(previous.rsplit("\n", 1)[-1]):
            header = headers.get(header, header)
        blocks.append(header + separator + body)
        previous = block
    return "\n\n".join(blocks)


def _legacy_opencode_completion_body(text: str, roots: list[Path], records: list[dict]) -> str:
    """Render the exact previous full body for receipt recovery only."""
    legacy = _previous_opencode_delivery_body(text, legacy_completion=True)
    for root, record in zip(roots, records):
        follow = _followup(root, record)
        if follow.startswith(_CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX):
            replacement = _LEGACY_RUNTIME_V1_FOLLOWUP + follow[
                len(_CURRENT_RUNTIME_V1_FOLLOWUP_PREFIX):]
            legacy = legacy.replace(follow, replacement, 1)
    return legacy


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


_COMPUTE_RUN_NOTICE_RE = re.compile(
    r"^run (?P<run_id>[A-Za-z0-9_-]+) on (?P<host>[A-Za-z0-9_.-]+) "
    r"ended with exit (?P<exit>[0-9]+)(?: \([^()\n]*\))?"
    r"(?:; started by attempt [^;\n]+ in route [^;\n]+)?"
    r"(?:; 지표 없음; 계획 시간 미상; run 기록 경로 없음)?"
    r"; read it with: compute-hosts tail (?P<tail>[A-Za-z0-9_-]+)$"
)


def _compute_run_notice(record: dict) -> dict | None:
    """Classify only the canonical, fully self-identifying compute-run notice."""
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    receipt_key = receipt.get("key")
    if (receipt.get("kind") != "notice" or not isinstance(receipt_key, str)
            or receipt_key.split(":", 1)[0] != "compute-run"
            or receipt.get("subject") != "compute run"):
        return None
    match = _COMPUTE_RUN_NOTICE_RE.fullmatch(receipt.get("text", ""))
    if not match or match.group("tail") != match.group("run_id"):
        return None
    original_key = f"compute-run:{match.group('run_id')}"
    if receipt_key != original_key:
        from harness_capabilities import HARNESSES, parent_completion
        harness = next((name for name in HARNESSES
            if parent_completion(name).get("carrier") == record.get("recipient_kind")), None)
        storage = record.get(STORAGE_KEY)
        if not harness or not storage or receipt_key != f"{original_key}:{harness}:{storage}":
            return None
        try:
            import session_notice
            session_notice.pending_lineage(record, harness, storage)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            return None
    run_id, host = match.group("run_id"), match.group("host")
    identity = re.fullmatch(r"(?P<host>.+)-(?P<stamp>[0-9]{8}-[0-9]{6})(?:-(?P<label>.*))?", run_id)
    if not identity or identity.group("host") != host:
        return None
    return {"host": host, "stamp": identity.group("stamp"),
            "label": identity.group("label") or "", "exit": int(match.group("exit")),
            "run_id": run_id}


_EXPERIMENT_FINAL_KEY_RE = re.compile(r"^experiment:(?P<run_id>[^:]+):final(?::|$)")
_STATUS_EXIT_RE = re.compile(
    r"status=(?P<status>[^;,\s]+)(?P<sep>;\s*)exit=(?P<exit>[^;,\s]+)")


def _utc_epoch(value) -> float | None:
    """Parse the existing UTC wall-clock provenance; ``None`` stays unknown."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _experiment_run_state(root: Path, record: dict, cache: dict):
    """The runner's public state for this experiment notice, only when newer.

    The hook path reads this one small published file; it never opens a run
    registry or a notice record to refresh a body. Anything it cannot resolve
    leaves the notice's own text unchanged (unknown is never folded).
    """
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    key = receipt.get("key")
    if not isinstance(key, str):
        return None
    match = _EXPERIMENT_FINAL_KEY_RE.match(key)
    if not match:
        return None
    import dispatch_recipient_inbox as inbox
    inbox_root = _inbox_root(root)
    if inbox_root not in cache:
        cache[inbox_root] = inbox.read_run_states(inbox_root)
    payload = cache[inbox_root]
    runs = payload.get("runs") if isinstance(payload, dict) else None
    run = runs.get(match.group("run_id")) if isinstance(runs, dict) else None
    if not isinstance(run, dict):
        return None
    generated = payload.get("generated_at")
    if not isinstance(generated, (int, float)) or isinstance(generated, bool):
        return None
    provenance = record.get("timestamp_provenance")
    materialized = _utc_epoch(provenance.get("record_materialized_at_utc")
                              if isinstance(provenance, dict) else None)
    if materialized is not None and float(generated) < materialized:
        return None
    if not isinstance(run.get("status"), str) or not run["status"]:
        return None
    return run


def _refreshed_notice_text(root: Path, record: dict, cache: dict) -> str | None:
    """Rewrite only ``status=…; exit=…`` for this render; never the record."""
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    text = receipt.get("text")
    if not isinstance(text, str):
        return None
    run = _experiment_run_state(root, record, cache)
    if run is None or not _STATUS_EXIT_RE.search(text):
        return None
    exit_code = run.get("exit_code")
    exit_text = "None" if exit_code is None else str(exit_code)
    return _STATUS_EXIT_RE.sub(
        f"status={run['status']}; exit={exit_text}", text, count=1)


def _notice_text(root: Path, record: dict, cache: dict | None = None) -> str:
    """The bounded notice line, with the runner's newer public state if any."""
    from session_notice import render_text
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    if "answer_binding" in receipt:
        from session_notice import consumer_text
        return consumer_text(receipt)
    original = render_text(receipt)
    refreshed = _refreshed_notice_text(root, record, {} if cache is None else cache)
    if refreshed is None or refreshed == receipt.get("text"):
        return original
    return f"{receipt.get('subject', '')}: {refreshed} (required_action={receipt.get('required_action', '')})"


def _render_context(batches: list[tuple[Path, list[dict]]]) -> str:
    """Render delivery text after the claims under one registry observation.

    Rendering is not an authority decision; the followups are projections of
    the same post-claim state, so they share one pass scope instead of opening
    one full registry read each.
    """
    from dispatch_registry_cache import registry_read_scope

    with registry_read_scope():
        return delivery_context(batches)


def delivery_context(batches: list[tuple[Path, list[dict]]]) -> str:
    """The one text a parent receives for its delivered records, whichever runtime carries it.

    ``batches`` pairs each state root with the records claimed there. Gate notices are
    answered, completions are consumed, so they get separate instructions.
    """

    gate_lines: list[tuple[str, str]] = []
    notice_records: list[tuple[Path, dict]] = []
    lines: list[tuple[str, str]] = []
    followups: list[str] = []
    run_state_cache: dict = {}
    from dispatch_recipient_receipt import receipt_marker

    def bound_text(root, record):
        text = _bounded_receipt_text(record)
        storage_key = record.get(STORAGE_KEY)
        if not isinstance(storage_key, str) or not storage_key:
            return text
        try:
            return text + "\n" + receipt_marker(record, storage_key)
        except (KeyError, TypeError, ValueError, pending_delivery.PendingDeliveryError):
            return text

    def bound_notice_text(root, record):
        text = _notice_text(root, record, run_state_cache)
        storage_key = record.get(STORAGE_KEY)
        if not isinstance(storage_key, str) or not storage_key:
            return text
        try:
            return text + "\n" + receipt_marker(record, storage_key)
        except (KeyError, TypeError, ValueError, pending_delivery.PendingDeliveryError):
            return text

    for root, records in batches:
        for record in records:
            if is_human_gate_record(record):
                gate_lines.append((str(record.get("delivery_id", "")), bound_text(root, record)))
                continue
            if (record.get("receipt") or {}).get("kind") == "notice":
                notice_records.append((root, record))
                continue
            text = bound_text(root, record)
            lines.append((str(record.get("delivery_id", "")), text))
            follow = _followup(root, record)
            if follow and follow not in followups:
                followups.append(follow)
    blocks: list[str] = []
    if gate_lines:
        blocks.append(GATE_DELIVERY_HEADER + "\n" + "\n".join(
            f"- {line}" for _delivery_id, line in sorted(gate_lines)))
    if notice_records:
        individual: list[tuple[tuple, str]] = []
        groups: dict[tuple, list[tuple[dict, dict]]] = {}
        for root, record in notice_records:
            run = _compute_run_notice(record)
            if run and run["exit"] == 0:
                receipt = record.get("receipt") or {}
                key = (receipt.get("subject"), run["host"], run["label"],
                       receipt.get("required_action"), 0)
                groups.setdefault(key, []).append((run, {"root": root, "record": record}))
            else:
                receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
                text = bound_notice_text(root, record)
                individual.append(((0, str(receipt.get("text", "")),
                                    str(record.get("delivery_id", ""))), text))
        for key, members in groups.items():
            members.sort(key=lambda item: (item[0]["stamp"], item[0]["run_id"],
                                           str(item[1]["record"].get("delivery_id", ""))))
            if len(members) < 3:
                for run, pair in members:
                    record = pair["record"]
                    receipt = record.get("receipt") or {}
                    individual.append(((0, str(receipt.get("text", "")),
                                        str(record.get("delivery_id", ""))),
                                       bound_notice_text(pair["root"], record)))
                continue
            runs = [run for run, _pair in members]
            latest = runs[-1]
            label = key[2] or "(unlabeled)"
            summary = (f"compute run summary: {len(runs)} runs on {key[1]} label={label}; "
                       f"exit 0:{len(runs)}; time {runs[0]['stamp']}..{latest['stamp']}; "
                       f"latest {latest['run_id']}; compute-hosts tail {latest['run_id']}")
            markers = []
            for _run, pair in members:
                text = bound_notice_text(pair["root"], pair["record"])
                markers.extend(line for line in text.splitlines()
                               if line.startswith("HEARTING_RECEIPT_V1 "))
            individual.append(((1, key, latest["run_id"]), summary + "\n" + "\n".join(markers)))
        ordered_notices = sorted(individual, key=lambda item: item[0])
        if ordered_notices:
            blocks.append(NOTICE_DELIVERY_HEADER + "\n" + "\n".join(
                f"- {line}" for _sort_key, line in ordered_notices))
    if lines:
        blocks.append(COMPLETION_DELIVERY_HEADER + "\n" + "\n".join(
            f"- {line}" for _delivery_id, line in sorted(lines))
                      + "".join("\n" + follow for follow in followups))
    return "\n\n".join(blocks)


def _opencode_expected_message(text: str, identities: list[tuple[str, dict]]) -> dict:
    """Bind an exact rendered OpenCode body to its handed-off identities."""
    from dispatch_recipient_receipt import identity_binding, receipt_marker_for_identity
    lines = set(text.splitlines()) if isinstance(text, str) else set()
    bindings = []
    for delivery_id, identity in identities:
        try:
            marker = receipt_marker_for_identity(delivery_id, identity)
            if marker in lines:
                bindings.append((delivery_id, identity_binding(identity)))
        except (KeyError, TypeError, ValueError):
            continue
    return {"text": text, "bindings": bindings}


def _opencode_native_user_texts(messages, session_id: str) -> list[str]:
    """Read only the native same-session user bodies used for candidate selection."""
    if not isinstance(messages, list):
        return []
    texts = []
    for row in messages:
        if not isinstance(row, dict):
            continue
        info = row.get("info")
        if (not isinstance(info, dict) or info.get("sessionID") != session_id
                or info.get("role") != "user"):
            continue
        message = row.get("message")
        content = (message.get("content") if isinstance(message, dict)
                   else row.get("parts"))
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(item["text"] for item in content
                              if isinstance(item, dict)
                              and item.get("type") in {"text", "input_text"}
                              and isinstance(item.get("text"), str))
        else:
            text = ""
        if text:
            texts.append(text)
    return texts


def _opencode_historical_attempts(native_text: str, record: dict) -> int | None:
    """Read a displayed historical claim count only when it could belong to this record."""
    if (record.get("receipt") or {}).get("kind") in {"supervision", "human-gate", "notice"}:
        return record.get("attempts")
    delivery_id = record.get("delivery_id")
    display = _bounded_receipt_text(record).split()
    if (not isinstance(delivery_id, str) or len(display) < 4
            or display[0] != f"delivery_id={delivery_id}"
            or not display[1].startswith("route_id=")
            or not display[2].startswith("route_node=")
            or not display[3].startswith("attempts=")):
        return record.get("attempts")

    lines = []
    for line in native_text.splitlines():
        fields = line.split()
        if (len(fields) >= 4 and fields[0] == "-"
                and fields[1] == f"delivery_id={delivery_id}"
                and fields[2].startswith("route_id=")
                and fields[3].startswith("route_node=")):
            lines.append(fields)
    if len(lines) != 1:
        return None
    counts = [field[len("attempts="):] for field in lines[0][4:]
              if field.startswith("attempts=")]
    current = record.get("attempts")
    if (len(counts) != 1 or not counts[0].isascii() or not counts[0].isdigit()
            or type(current) is not int or current < 1
            or record.get("claim_authority") not in pending_delivery.CLAIM_AUTHORITIES):
        return None
    historical = int(counts[0])
    if historical < 1 or historical > current:
        return None
    return historical


def _opencode_delivery_expectations(roots: list[Path], recipient_kind: str,
                                    session_id: str, messages=None, *, notice_context=None) -> list[dict]:
    """Rebuild only batches named by native markers and verify their exact body.

    Markers select candidate records; the rendered body and each current immutable
    identity must still match. This recognizes an older batch after unrelated
    pending records arrive, a member is ACKed, or its displayed claim count advances.
    Historical counts are accepted only within the record's already-claimed range.
    """
    from dispatch_recipient_receipt import OPENCODE_MARKER_LINE_RE, identity_binding
    from dispatch_seat_handover import record_for_session, storage_recipients
    from peer_read_context import PeerReadContext
    read_context = PeerReadContext()
    from dispatch_notice_state import NoticeReadContext
    notice_context = notice_context or NoticeReadContext()

    by_id: dict[str, list[tuple[Path, dict]]] = {}
    candidate_ids = set()
    native_rows = []
    for text in _opencode_native_user_texts(messages, session_id):
        markers = OPENCODE_MARKER_LINE_RE.findall(text)
        if not markers or len({delivery_id for delivery_id, _digest in markers}) != len(markers):
            continue
        native_rows.append((text, markers))
        candidate_ids.update(delivery_id for delivery_id, _digest in markers)

    for root in roots:
        records, _count = addressed_records(root, recipient_kind, session_id, read_context=read_context)
        for record in records:
            delivery_id = record.get("delivery_id")
            if delivery_id in candidate_ids:
                by_id.setdefault(delivery_id, []).append((root, record))

    # Open records are already enumerated above. Look up only marker-selected
    # terminal records so an ACKed member can validate the old complete batch.
    harness = _recipient_harness(recipient_kind)
    recipients = storage_recipients(session_id, harness=harness, read_context=read_context)
    for delivery_id in candidate_ids - by_id.keys():
        for root in roots:
            for storage, allowed in recipients:
                try:
                    record = pending_delivery.read(root, storage, delivery_id)
                    if (record is None or record.get("state") != "acked"
                            or (record.get("recipient_kind") != recipient_kind and storage == session_id)
                            or not record_for_session(record, allowed, session=session_id,
                                                      harness=harness,
                                                      storage_key=storage, jobs=record_jobs(root, record), read_context=read_context)):
                        continue
                    by_id.setdefault(delivery_id, []).append(
                        (root, {**record, STORAGE_KEY: storage}))
                except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                    continue

    expected, seen = [], set()
    for native_text, markers in native_rows:
        selected, identities, valid = [], [], True
        for delivery_id, digest in markers:
            matches = []
            for root, record in by_id.get(delivery_id, ()):
                storage = record.get(STORAGE_KEY) or session_id
                try:
                    identity = pending_delivery.received_ack_identity(
                        record, storage, delivery_id)
                    if identity_binding(identity) == digest:
                        matches.append((root, record, identity))
                except (KeyError, TypeError, ValueError,
                        pending_delivery.PendingDeliveryError):
                    continue
            if len(matches) != 1:
                valid = False
                break
            root, record, identity = matches[0]
            historical_attempts = _opencode_historical_attempts(native_text, record)
            if historical_attempts is None:
                valid = False
                break
            try:
                rendered_record = carrier_view(
                    root, {**record, "attempts": historical_attempts},
                    record.get(STORAGE_KEY) or session_id, identity, notice_context=notice_context)
            except (OSError, ValueError, RuntimeError):
                valid = False
                break
            selected.append((root, rendered_record))
            identities.append((delivery_id, identity))
        if not valid:
            continue
        grouped_selected: dict[Path, list[dict]] = {}
        for root, record in selected:
            grouped_selected.setdefault(root, []).append(record)
        rendered = delivery_context(list(grouped_selected.items()))
        previous_rendered = _previous_opencode_delivery_body(rendered)
        legacy_rendered = _legacy_opencode_completion_body(
            rendered, [root for root, _record in selected],
            [record for _root, record in selected]) if rendered else ""
        variants = {rendered, previous_rendered, legacy_rendered}
        if any((record.get("receipt") or {}).get("reason") == "workflow-completion-pending"
               for _root, record in selected):
            # Recover the previous wording from the same stored diagnosis.
            # Receipt markers alone never authorize an arbitrary native body.
            variants.update(text.replace("The last recorded closure refusal is ", "The latest closure refusal is ")
                .replace("No recorded refusal is available.", "No current refusal record is available.")
                .replace("The last recorded checkpoint is ", "The current checkpoint is ")
                for text in tuple(variants))
        if not rendered or native_text not in variants:
            continue
        candidate = _opencode_expected_message(native_text, identities)
        key = (candidate["text"], tuple(candidate["bindings"]))
        if candidate["bindings"] and key not in seen:
            seen.add(key)
            expected.append(candidate)
    return expected


def _units(batches) -> list[tuple[Path, list[dict]]]:
    """Split claimed batches into the units that are rendered and ACKed together.

    Members of one sealed batch share their group claim owner, so a unit never
    splits a batch; every other record is its own unit.
    """
    units: list[tuple[Path, list[dict]]] = []
    index: dict[tuple, int] = {}
    for root, records in batches:
        for record in records:
            owner = record.get("claim_owner")
            key = ((str(root), str(owner)) if isinstance(owner, str) and owner
                   else (str(root), "delivery", str(record.get("delivery_id"))))
            if key in index:
                units[index[key]][1].append(record)
            else:
                index[key] = len(units)
                units.append((root, [record]))
    return units


def _batches(units) -> list[tuple[Path, list[dict]]]:
    merged: list[tuple[Path, list[dict]]] = []
    for root, records in units:
        if merged and merged[-1][0] == root:
            merged[-1][1].extend(records)
        else:
            merged.append((root, list(records)))
    return merged


def fit_whole(batches, render, max_chars: int):
    """Choose the leading units whose full rendering fits ``max_chars``.

    Returns ``(text, chosen_batches, rest_batches)``. The text is never cut:
    only what it contains whole is ACKed, and the rest stays pending for the
    next boundary. The first unit is always chosen so a single large receipt
    still moves.
    """
    units = _units(batches)
    if not units:
        return "", [], []
    whole = render(_batches(units))
    if len(whole) <= max_chars or len(units) == 1:
        return whole, _batches(units), []
    best_n, best_text = 1, render(_batches(units[:1]))
    lo, hi = 2, len(units) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        text = render(_batches(units[:mid]))
        if len(text) <= max_chars:
            best_n, best_text, lo = mid, text, mid + 1
        else:
            hi = mid - 1
    return best_text, _batches(units[:best_n]), _batches(units[best_n:])


def release_own_claims(recipient_kind: str, session_id: str, batches, *, reason: str) -> None:
    """Return this process's own sweep claims on records it did not ACK.

    A tool boundary comes far more often than a prompt, so a boundary carrier
    hands back at once what it claimed but will not ACK (or did not fit whole)
    instead of holding other carriers off for the claim lease.
    """
    owner_prefix = f"session-sweep:{recipient_kind}:{os.getpid()}:"
    for root, records in batches:
        for record in records:
            delivery_id = record.get("delivery_id")
            storage = record.get(STORAGE_KEY) or session_id
            if not isinstance(delivery_id, str) or not delivery_id:
                continue
            try:
                fresh = pending_delivery.read(root, storage, delivery_id)
                owner = (fresh or {}).get("claim_owner")
                if (fresh is None or fresh.get("state") != "claimed"
                        or not isinstance(owner, str) or not owner.startswith(owner_prefix)):
                    continue
                # A distinct reason per release keeps the courier's retry delay
                # at its base instead of growing it on every tool boundary.
                pending_delivery.release_claim(
                    root, storage, delivery_id, claim_owner=owner,
                    failure_reason=f"{reason}:{time.time_ns()}",
                    expected_identity=pending_delivery.received_ack_identity(
                        fresh, storage, delivery_id))
            except Exception:  # noqa: BLE001 - an unreleased claim only waits for its lease
                continue


def ack_delivered(root: Path, session_id: str, records: list[dict], *, acked_by: str) -> int:
    """Ack the records whose bounded receipt was injected; returns the count.

    A record a handover bound to this session is acked under the registered parent it is stored under."""

    acked = 0
    acknowledged: list[dict] = []
    for record in records:
        try:
            storage = record.get(STORAGE_KEY) or session_id
            delivery_id = record["delivery_id"]
            try:
                event_id = hashlib.sha256((delivery_id + "\0" + acked_by).encode("utf-8")).hexdigest()
                pending_delivery.record_send(root, storage, delivery_id,
                    source="prompt-context-rendered", event_id=event_id)
            except (pending_delivery.PendingDeliveryError, OSError):
                pass  # Timestamp observation is advisory; it never withholds an exact ACK.
            updated = pending_delivery.ack(root, storage, delivery_id, acked_by=acked_by)
            acked += 1
            if (isinstance(updated, dict)
                    and updated.get("recipient_kind") == "codex-native-queue"):
                acknowledged.append({**updated, STORAGE_KEY: storage})
        except (pending_delivery.PendingDeliveryError, KeyError, OSError):
            continue
    # ACK locks are all released before any queue RPC. The caller's exact
    # session_id is the thread whose prompt rendered this batch.
    if acknowledged and len(acknowledged) == len(records):
        try:
            import codex_queue_delivery
            from codex_queue_dispatch import resolve_queue_socket
            endpoint = resolve_queue_socket()
            deadline = time.monotonic() + 1.5
            with codex_queue_delivery.DeliverySession(
                    endpoint, timeout=1.0, deadline=deadline) as session:
                by_storage: dict[str, list[dict]] = {}
                for seed in acknowledged:
                    storage = seed.get(STORAGE_KEY) or session_id
                    by_storage.setdefault(storage, []).append(seed)
                for storage, seeds in by_storage.items():
                    codex_queue_delivery.retire_acked_completion(
                        root, endpoint, session_id, seeds, storage,
                        timeout=1.0, deadline=deadline, session=session,
                    )
        except Exception:  # noqa: BLE001 - queue retirement never changes ACK outcome.
            pass
    return acked


def sweep(
    root: Path,
    recipient_kind: str,
    session_id: str,
    session_generation: str,
    *,
    delivery_ids=None,
) -> tuple[str, int]:
    """Enumerate this session's own ``recipient_digest`` directory and try to
    claim every open record found in it, always demanding generation proof.

    ``delivery_ids`` narrows that enumeration to the ids a current derived
    inbox named; without it the session's own directory is the enumeration.
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
    settled_batch_ids: set[str] = set()
    try:
        directory = pending_delivery.record_directory(root, session_id)
        if delivery_ids is None:
            entries = sorted(
                p for p in directory.glob("delivery-*.json") if p.is_file()
            )
        else:
            entries = [directory / f"{delivery_id}.json" for delivery_id in
                       dict.fromkeys(str(item) for item in delivery_ids)
                       if isinstance(delivery_id, str) and delivery_id.startswith("delivery-")]
            entries = [path for path in entries if path.is_file() and not path.is_symlink()]
        for entry in entries:
            delivery_id = entry.stem
            if delivery_id in settled_batch_ids:
                continue
            claim_owner = (
                f"session-sweep:{recipient_kind}:{os.getpid()}:{time.monotonic_ns()}"
            )
            record = pending_delivery.read(root, session_id, delivery_id)
            jobs = record_jobs(root, record) if record is not None else None
            if jobs is None and record is not None and not (record.get("receipt") or {}).get("job_registry"):
                continue
            try:
                group = pending_delivery.claim_ready_batch(
                    root,
                    session_id,
                    delivery_id,
                    claim_owner=claim_owner,
                    lease_seconds=SWEEP_LEASE_SECONDS,
                    require_generation_proof=True,
                    jobs=jobs,
                    retry_context=_retry_context(recipient_kind, session_id, session_id, record),
                )
            except pending_delivery.PendingDeliveryError:
                continue
            settled_batch_ids.update(member["delivery_id"] for member in group)
            claimed += len(group)
    except OSError:
        pass
    elapsed_ns = time.monotonic_ns() - start_ns
    _append_self_instrumentation(root, elapsed_ns, len(entries), claimed)
    return ("claimed" if claimed else "refused", len(entries))


def _state_roots() -> list[Path]:
    from dispatch_contract import dispatch_state_roots, resolve_agent_home
    return [Path(root) for root in dict.fromkeys(dispatch_state_roots(resolve_agent_home()))
            if Path(root).is_dir()]


def _lineage_generation_key(graph):
    """One comparable observation identity for full and compact graphs.

    The full read records each ledger's readable-bytes digest; the compact read
    intentionally does not. Both describe the same file generation in the first
    seven fields, so the comparison normalizes on those.
    """
    generation = getattr(graph, "generation", None)
    if not isinstance(generation, tuple) or len(generation) != 3:
        return None
    location, files, directory = generation
    try:
        files = tuple(tuple(item)[:7] for item in files)
    except TypeError:
        return None
    return (location, files, directory)


def _recipients_view(roots: list[Path], session_id: str, recipient_kind: str) -> dict:
    """The OpenCode tick's bounded look: a current inbox, else one computation.

    The legacy-cache key is the inbox's edge generation and folded edge digest,
    never a sessions-ledger file signature. On a cache miss one bounded scope
    enumeration runs; an incomplete enumeration is never published, and the
    previous inbox (or the session's own scope) answers this tick instead.
    """
    import dispatch_recipient_inbox as inbox
    from dispatch_seat_handover import lineage_graph, storage_recipients
    from peer_read_context import PeerReadContext

    if not roots:
        return {"generation": "", "digests": [], "delivery_ids": []}
    root = roots[0]
    inbox_root = _inbox_root(root)
    recorded = inbox.read_inbox(inbox_root, "opencode", session_id)

    def inbox_view(payload):
        digests = sorted({hashlib.sha256(item["storage_key"].encode("utf-8")).hexdigest()
                          for item in payload["candidates"]
                          if isinstance(item.get("storage_key"), str) and item["storage_key"]})
        return {"generation": f"{payload['edge_generation']}:{payload.get('folded_edge_digest') or ''}",
                "digests": digests,
                "delivery_ids": [item["delivery_id"] for item in payload["delivery_ids"]]}

    if recorded is not None and inbox.is_current(recorded, inbox_root):
        view = inbox_view(recorded)
        # The session's own scope may have appeared after the inbox was written.
        try:
            own_directory = str(pending_delivery.record_directory(root, session_id))
        except pending_delivery.PendingDeliveryError:
            own_directory = ""
        if own_directory and own_directory not in recorded["dir_identities"]:
            known = set(view["delivery_ids"])
            view["delivery_ids"] = [*view["delivery_ids"], *(
                delivery_id for _storage, delivery_id in _scope_entries(root, session_id)
                if delivery_id not in known)]
        return view

    read_context = PeerReadContext(compact_lineage=True)
    deadline_ns = time.monotonic_ns() + int(DELIVER_BUDGET_SECONDS * 1_000_000_000)
    # Capture the edge generation before the graph and scope reads, so a writer
    # that completes later makes the publication recheck refuse; old scopes
    # never publish under a new generation.
    edge_generation = inbox.read_edge_generation(inbox_root)
    graph = lineage_graph(read_context=read_context)
    generation = hashlib.sha256(json.dumps(graph.generation, separators=(",", ":")).encode()).hexdigest()
    scopes = storage_recipients(session_id, harness="opencode",
                                read_context=read_context, deadline_ns=deadline_ns)
    digests = sorted({hashlib.sha256(storage.encode("utf-8")).hexdigest()
                      for storage, _allowed in scopes})
    # The compact builder returns a graph without seeding the full-read cache,
    # so object identity can never prove that discovery saw one observation.
    # Compare the observation identity itself against a fresh readable graph
    # (the compact path stays inside that graph's own normalized generation).
    current = _lineage_generation_key(lineage_graph())
    expected = _lineage_generation_key(graph)
    if current is None or expected is None or current != expected:
        raise OSError("lineage-observation-changed")
    if not bool(getattr(scopes, "complete", True)):
        _reconcile_canonical_dispatch_state(root)
        if recorded is not None:
            return inbox_view(recorded)
        return {"generation": generation, "digests": digests, "delivery_ids": []}

    candidates = []
    for storage_key, _allowed in scopes:
        candidates.extend(_scope_entries(root, storage_key))
    _addressed, open_entries, _count, complete = _read_addressed(
        root, recipient_kind, session_id, "opencode", candidates,
        read_context=read_context, deadline_ns=deadline_ns)
    if complete:
        try:
            payload = _inbox_payload(
                root, session_id, "opencode", scopes, open_entries,
                edge_generation=edge_generation,
                folded_edge_digest=scopes_edge_digest(scopes), inbox_root=inbox_root)
            inbox.write_inbox(inbox_root, "opencode", session_id, payload)
            return inbox_view(payload)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            pass
    return {"generation": generation, "digests": digests,
            "delivery_ids": [delivery_id for _storage, delivery_id, _mtime in open_entries]}


def main(argv: list[str] | None = None) -> int:
    """A runtime carrier's steps for one session's records, as JSON on stdout.

    ``roots`` names the state roots; ``deliver`` claims what is owed to the session and renders it; the carrier then
    hands the text to its runtime and answers ``ack`` (taken) or ``release`` (not
    taken, so the next pass claims it again), passing ``deliver``'s records on stdin.
    """
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("action", choices=("roots", "recipients", "activate", "deliver", "ack", "release", "settle"))
    parser.add_argument("--recipient-kind", default="")
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    deadline_ns = time.monotonic_ns() + int(ACTION_BUDGET_SECONDS * 1_000_000_000)
    if args.action == "roots":
        # Where this runtime's records live, so a carrier can look before it asks.
        print(json.dumps([str(root) for root in _state_roots()]))
        return 0
    if not args.session:
        parser.error("--session is required")
    if args.recipient_kind not in pending_delivery.RECIPIENT_KINDS:
        parser.error(f"unknown recipient kind {args.recipient_kind}")
    from dispatch_notice_state import NoticeReadContext
    notice_context = NoticeReadContext()
    if args.action == "recipients":
        if args.recipient_kind != "opencode-turn":
            parser.error("recipients is an internal OpenCode carrier action")
        print(json.dumps(_recipients_view(_state_roots(), args.session, args.recipient_kind)))
        return 0
    if args.action == "settle":
        try:
            handed = json.load(sys.stdin)
        except (ValueError, TypeError):
            handed = {}
        from dispatch_recipient_receipt import opencode_messages_evidence, settle_native_receipt
        requested = handed.get("records")
        roots = _state_roots()
        allowed_roots = {Path(root).resolve() for root in roots}
        targets = []
        requested_complete, unavailable_ids = True, set()
        if isinstance(requested, list):
            for item in requested:
                if not isinstance(item, dict):
                    requested_complete = False
                    continue
                try:
                    root = Path(item["root"]).resolve()
                    storage, delivery_id = item["storage_key"], item["delivery_id"]
                    if (root not in allowed_roots or not isinstance(storage, str) or not storage
                            or not isinstance(delivery_id, str) or not delivery_id):
                        requested_complete = False
                        if isinstance(delivery_id, str):
                            unavailable_ids.add(delivery_id)
                        continue
                    targets.append((root, storage, delivery_id, item))
                except (KeyError, TypeError, OSError, RuntimeError):
                    requested_complete = False
                    continue
        else:
            for root in roots:
                records, _count = addressed_records(root, args.recipient_kind, args.session,
                                                    deadline_ns=deadline_ns)
                targets.extend((root, r.get(STORAGE_KEY) or args.session,
                                r["delivery_id"], None) for r in records)
        expected_messages = []
        if isinstance(requested, list):
            text = handed.get("text")
            if isinstance(text, str):
                identities = []
                # The exact native body includes every original member, even
                # when one root is no longer admitted. Only admitted targets
                # may be read/ACKed; their historical proof is still usable.
                for item in requested:
                    if not isinstance(item, dict):
                        continue
                    delivery_id = item.get("delivery_id")
                    identity = item.get("immutable_identity")
                    if (isinstance(delivery_id, str) and isinstance(identity, dict)
                            and identity.get("delivery_id") == delivery_id):
                        identities.append((delivery_id, identity))
                expected = _opencode_expected_message(text, identities)
                if expected["bindings"]:
                    expected_messages.append(expected)
            expected_messages.extend(_opencode_delivery_expectations(
                roots, args.recipient_kind, args.session, handed.get("messages"), notice_context=notice_context))
        else:
            supplied = handed.get("expected_native_messages")
            if isinstance(supplied, list):
                for item in supplied:
                    if (not isinstance(item, dict) or not isinstance(item.get("text"), str)
                            or item.get("carrier") != args.recipient_kind
                            or item.get("recipient_sid") != args.session
                            or not all(isinstance(item.get(key), str) and item[key]
                                       for key in ("delivery_id", "storage_recipient", "claim_owner",
                                                   "duty_id", "jobs", "attempt_id"))
                            or not item["duty_id"].startswith("registered-batch-")
                            or item["claim_owner"] != "retained-rewake:" + item["duty_id"]
                            or not Path(item["jobs"]).is_absolute()):
                        continue
                    for root, storage, delivery_id, _handed_record in targets:
                        if (storage != item["storage_recipient"]
                                or delivery_id != item["delivery_id"]):
                            continue
                        try:
                            current = pending_delivery.read(root, storage, delivery_id)
                            if (current is None
                                    or current.get("state") not in pending_delivery.OPEN_STATES):
                                continue
                            identity = pending_delivery.received_ack_identity(
                                current, storage, delivery_id)
                            expected = _opencode_expected_message(
                                item["text"], [(delivery_id, identity)])
                            if expected["bindings"]:
                                expected_messages.append(expected)
                        except (OSError, KeyError, TypeError, ValueError,
                                pending_delivery.PendingDeliveryError):
                            continue
                        break
        evidence = opencode_messages_evidence(
            handed.get("messages"), args.session, expected_messages)
        received_ids, unknown_ids, settled_count = set(), set(unavailable_ids), 0
        remaining_context, remaining_handoffs = [], []
        for root, storage, delivery_id, handed_record in targets:
            try:
                current = pending_delivery.read(root, storage, delivery_id)
                if current is None:
                    if isinstance(requested, list):
                        unknown_ids.add(delivery_id)
                    continue
                original_identity = None
                original_owner = None
                if isinstance(requested, list):
                    original_identity = (handed_record or {}).get("immutable_identity")
                    original_owner = (handed_record or {}).get("claim_owner")
                    live_identity = pending_delivery.received_ack_identity(
                        current, storage, delivery_id)
                    if (not isinstance(original_identity, dict)
                            or live_identity != original_identity):
                        unknown_ids.add(delivery_id)
                        continue
                outcome = settle_native_receipt(
                    root, storage, "opencode", args.session, current, evidence,
                    acked_by=f"native-receipt:opencode:{args.session}")
                if outcome["received"]:
                    received_ids.add(delivery_id)
                    settled_count += int(outcome["settled"])
                elif isinstance(requested, list):
                    fresh = pending_delivery.read(root, storage, delivery_id)
                    claim_deadline_ns = (fresh.get("claim_deadline_ns")
                                         if fresh is not None else None)
                    if (fresh is None
                            or pending_delivery.received_ack_identity(
                                fresh, storage, delivery_id) != original_identity
                            or fresh.get("state") != "claimed"
                            or fresh.get("claim_owner") != original_owner
                            or type(claim_deadline_ns) is not int
                            or claim_deadline_ns <= time.monotonic_ns()):
                        unknown_ids.add(delivery_id)
                        continue
                    try:
                        view = carrier_view(root, fresh, storage, original_identity, notice_context=notice_context)
                    except (OSError, ValueError, RuntimeError):
                        unknown_ids.add(delivery_id)
                        continue
                    item = dict(handed_record or {})
                    item.update(root=str(root), storage_key=storage, delivery_id=delivery_id)
                    remaining_context.append((root, [view]))
                    remaining_handoffs.append(item)
            except (OSError, KeyError, TypeError, pending_delivery.PendingDeliveryError):
                if isinstance(requested, list) and isinstance(handed_record, dict):
                    unknown_ids.add(delivery_id)
                continue
        # Receipt settlement above precedes authority admission. Every original
        # unreceived member must still be current after all views/observations.
        if isinstance(requested, list):
            from dispatch_recipient_receipt import claimed_send_current, release_unsubmitted_claim
            for root, storage, delivery_id, item in targets:
                if delivery_id in received_ids:
                    continue
                original = {"delivery_id": delivery_id,
                            IDENTITY_KEY: (item or {}).get("immutable_identity")}
                if not claimed_send_current(root, storage, original,
                        (item or {}).get("claim_owner"), "opencode", args.session):
                    unknown_ids.add(delivery_id)
            if unknown_ids or not requested_complete:
                for root, storage, delivery_id, item in targets:
                    if delivery_id not in received_ids:
                        release_unsubmitted_claim(root, storage,
                            {"delivery_id": delivery_id,
                             IDENTITY_KEY: (item or {}).get("immutable_identity")},
                            (item or {}).get("claim_owner"), args.session)
                remaining_context, remaining_handoffs = [], []
        print(json.dumps({"received_ids": sorted(received_ids), "settled_count": settled_count,
                          "unknown_ids": sorted(unknown_ids),
                          "text": _render_context(remaining_context),
                          "records": remaining_handoffs},
                         ensure_ascii=False), flush=True)
        return 0
    if args.action in {"activate", "deliver"}:
        batches = []
        try:
            handed = json.load(sys.stdin) if args.action == "deliver" else {}
        except (ValueError, TypeError):
            handed = {}
        from dispatch_recipient_receipt import opencode_messages_evidence
        try:
            roots = _state_roots()
            native_evidence = (opencode_messages_evidence(
                handed.get("messages"), args.session,
                _opencode_delivery_expectations(roots, args.recipient_kind, args.session,
                                                handed.get("messages"), notice_context=notice_context))
                               if args.action == "deliver" else None)
            for root in roots:
                if args.action == "activate":
                    records = activate(root, args.recipient_kind, args.session,
                                       request_recovery=False, notice_context=notice_context,
                                       deadline_ns=deadline_ns)
                else:
                    records, _entries = sweep_deliver(
                        root, args.recipient_kind, args.session,
                        native_evidence=native_evidence, notice_context=notice_context,
                        deadline_ns=deadline_ns)
                if records:
                    batches.append((root, records))
            print(json.dumps({
                "text": _render_context(batches),
                "records": [{"root": str(root), "storage_key": record.get(STORAGE_KEY) or args.session,
                             "delivery_id": record["delivery_id"], "claim_owner": record.get("claim_owner"),
                             "immutable_identity": record.get(IDENTITY_KEY)
                             or pending_delivery.received_ack_identity(
                                 record, record.get(STORAGE_KEY) or args.session, record["delivery_id"])}
                            for root, records in batches for record in records],
            }, ensure_ascii=False), flush=True)
        finally:
            if args.action == "activate":
                request_obligation_reconciliation(lifecycle_root=Path(__file__).resolve().parents[1])
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
                from dispatch_recipient_receipt import release_unsubmitted_claim
                release_unsubmitted_claim(root, key,
                    {"delivery_id": delivery_id, IDENTITY_KEY: item.get("immutable_identity")},
                    item["claim_owner"], args.session)
                done += 1
        except (KeyError, TypeError, OSError, pending_delivery.PendingDeliveryError):
            continue
    print(json.dumps({"action": args.action, "count": done}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
