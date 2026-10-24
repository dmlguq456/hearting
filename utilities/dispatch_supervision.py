"""Shared wait ownership and durable, non-terminal parent handback.

The canonical registry owns outcomes. This controller schedules bounded recovery
and keeps waiting; its timeout never authorizes killing a worker or retrying it.
The existing pending-delivery queue owns a notice until a carrier accepts it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time
from typing import Callable, Iterable

from dispatch_attempt_policy import decide_attempt, terminal_conflict_digest
from dispatch_receipt_identity import receipt_digest
from dispatch_registry_cache import registry_view
import dispatch_pending_delivery as pending_delivery

KIND = "supervision"
REASONS = frozenset({"owner-input-undelivered", "no-progress", "process-unverifiable", "join-deadline", "supervisor-exited", "join-observer-failed", "terminal-evidence-conflict", "workflow-completion-pending", "closure-blocked", "watch-deadline", "receiver-unavailable", "answer-awaiting-parent", "parent-session-ended"})
# Another session answered a BLOCKED owner; only the route's parent launches the continuation.
ANSWER_AWAITING_PARENT = "answer-awaiting-parent"


class SupervisionError(ValueError):
    pass


class StopWaiting(RuntimeError):
    """Abort a managed wait without recording a delivery notice or row change."""


def join_process_error(returncode: int, receipt: object, parent_attempt_id: str) -> str:
    """Preserve the join protocol's typed failure, never arbitrary child output."""
    reason = "unverified-error-receipt"
    if (isinstance(receipt, dict) and receipt.get("schema_version") == 2
            and receipt.get("state") == "contract-error"
            and receipt.get("parent_attempt_id") == parent_attempt_id
            and receipt.get("children") == []):
        candidate = receipt.get("reason")
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,127}", candidate):
            reason = candidate
    return f"join-process-contract-failed:exit={returncode}:reason={reason}"


def _rows(jobs: Path, attempts: Iterable[str] | None = None) -> dict:
    view = registry_view(Path(jobs))
    rows = {}
    if attempts is None:
        selected = ((record, record.completion_metadata()) for record in view.records)
    else:
        pending = list(dict.fromkeys(attempts))
        seen = set()
        selected_rows = []
        while pending:
            aid = pending.pop()
            if aid in seen:
                continue
            seen.add(aid)
            candidates = view.completion_attempt_records(aid)
            if len(candidates) > 1:
                raise SupervisionError("supervision-attempt-ambiguous")
            if not candidates:
                continue
            record = candidates[0]
            meta = record.completion_metadata()
            selected_rows.append((record, meta))
            parent = meta.get("parent_attempt_id")
            if parent not in (None, "", "-"):
                pending.append(parent)
        selected = iter(selected_rows)
    for record, meta in selected:
        aid = meta.get("attempt_id")
        if aid:
            if aid in rows:
                raise SupervisionError("supervision-attempt-ambiguous")
            rows[aid] = (record.status, meta)
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


def _awaiting_answers(jobs: Path, aid: str) -> list[str]:
    """The kept answers of a BLOCKED owner whose continuation has not launched yet."""
    from dispatch_owner_input import blocked_owner_answers
    from dispatch_replacement import _replacement_in_flight, _rows as replacement_rows
    answers = blocked_owner_answers(jobs, aid)
    if not answers:
        return []
    rows = replacement_rows(Path(jobs).read_text(encoding="utf-8").splitlines())
    if aid in rows and _replacement_in_flight(jobs, rows, rows[aid][1]):
        return []
    return [item["id"] for item in answers]


def _pending(rows: dict, attempts: list[str], jobs: Path, reason=None) -> bool:
    import route_parent_close
    attempts = [aid for aid in attempts if aid not in rows or not route_parent_close.row_requested(rows[aid][1], jobs)]
    if not attempts:
        return False
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved
        return any(unresolved(jobs, aid) for aid in attempts)
    if reason == ANSWER_AWAITING_PARENT:
        return any(_awaiting_answers(jobs, aid) for aid in attempts)
    if reason == "no-progress":
        from dispatch_replacement import parent_resume_obligation
        for aid in attempts:
            meta = (rows.get(aid) or (None, {}))[1]
            route_id = meta.get("owner_route_id") or meta.get("route_id")
            if parent_resume_obligation(jobs, aid, route_id=route_id) is not None:
                return True
        return False
    from dispatch_contract import observed_attempt_liveness
    from codex_dispatch_terminal import terminal_envelope_observed
    for aid in attempts:
        if aid not in rows:
            raise SupervisionError("supervision-attempt-missing")
        status, meta = rows[aid]
        # Route settlement can retire its completion wait while process cleanup
        # remains an independent obligation. Never let route close hide a live,
        # unknown, or conflicting execution.
        from dispatch_notice_state import route_obligation_closed
        route_closed = reason == "supervisor-exited" and route_obligation_closed(meta, jobs)
        from dispatch_contract import observed_attempt_liveness
        from codex_dispatch_terminal import terminal_envelope_observed
        proof = observed_attempt_liveness(status, meta,
            terminal_envelope=terminal_envelope_observed(meta.get("log_file")),
            terminal_receipt_gate=True)
        decision = decide_attempt(status, meta, process_state=proof.process_state,
                                  process_reason=proof.process_reason)
        if decision.action in {"wait", "recover", "reconcile", "inspect-conflict"}:
            return True
        # A supervisor-exited notice may be only a lagging route-settlement
        # signal. Once that exact route is closed, do not keep it alive solely
        # because the workflow ledger is still flushing; closure-blocked remains
        # a distinct reason and does not take this branch.
        if meta.get("workflow_completion") == "runtime-v1":
            from dispatch_terminal_commit import owner_completion_pending
            if not route_closed and owner_completion_pending(jobs, status, meta):
                return True
    return False


def _obligation_revision(rows: dict, attempts: list[str], reason: str, jobs=None) -> str:
    if reason == "owner-input-undelivered":
        from dispatch_owner_input import unresolved_revision
        return hashlib.sha256(json.dumps([(aid, unresolved_revision(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason == ANSWER_AWAITING_PARENT:
        return hashlib.sha256(json.dumps([(aid, _awaiting_answers(jobs, aid))
                                          for aid in attempts]).encode()).hexdigest()
    if reason != "terminal-evidence-conflict":
        return ""
    value = [(aid, terminal_conflict_digest(rows[aid][1])) for aid in attempts]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def materialize(jobs: Path, attempts: set[str], *, reason: str) -> list[dict]:
    """Idempotent handback per exact root/batch/reason, with no row mutation."""
    if reason not in REASONS:
        raise SupervisionError("supervision-reason-invalid")
    jobs = jobs.resolve()
    rows = _rows(jobs, attempts)
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
            "obligation_revision": _obligation_revision(rows, monitored, reason, jobs),
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


def handover_identity_refusal(jobs: Path, owner_attempt_id: str, start_receipt: dict) -> dict:
    """Durably hand an identity-only replacement refusal to its route parent."""
    if start_receipt.get("reason") != "replacement-parent-identity-unproven":
        return {}
    from dispatch_owner_input import retained
    reason = ANSWER_AWAITING_PARENT if retained(jobs, owner_attempt_id) else "no-progress"
    try:
        records = materialize(Path(jobs), {owner_attempt_id}, reason=reason)
        if records:
            try:
                from peer_obligations import retain_registered_notice
                for record in records:
                    retain_registered_notice(jobs, record)
            except (OSError, ValueError, RuntimeError):
                # A runner outage does not erase the durable parent notice.
                # Its normal carrier/lifecycle recovery can still deliver it.
                pass
            return {"parent_notified": True,
                    "next_step": ("The route parent has a durable notice to continue this exact replacement. "
                                  "Its runtime will recheck the existing route start and parent authority when "
                                  "the notice is consumed; this receipt does not claim that a worker launched.")}
    except (OSError, ValueError, SupervisionError):
        pass
    return {"parent_notified": False,
            "next_step": ("The route parent remains the only session allowed to start this replacement, but "
                          "no durable parent notice was recorded. Continue with the existing route start from "
                          "that parent; no launch-settlement wait is required.")}


def validate(receipt: dict, *, expected_thread: str | None = None,
             expected_epoch: int | None = None) -> dict:
    keys = {"schema_version", "kind", "state", "owner_attempt_id", "monitored_attempt_ids",
            "recipient_thread_id", "sealed_batch_id", "job_registry",
            "reason", "responsible", "required_action", "pending_delivery_id", "obligation_revision"}
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
    rows = _rows(jobs, set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    owner = receipt["owner_attempt_id"]
    if owner not in rows:
        raise SupervisionError("supervision-owner-missing")
    meta = rows[owner][1]
    if (meta.get("parent_sid") != expected_thread_id
            or meta.get("managed_sealed_batch_id", "") != expected_sealed_batch_id
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    # A recovered batch must not receive an obsolete intervention request.
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        raise SupervisionError("supervision-resolved")
    if validate_live and (not _pending(rows, receipt["monitored_attempt_ids"], jobs, receipt["reason"])
                          or _cleanup_only_result(receipt) is not None):
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
    rows = _rows(Path(receipt["job_registry"]),
                 set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    owner = receipt["owner_attempt_id"]
    if (owner not in rows or rows[owner][1].get("parent_sid") != receipt["recipient_thread_id"]
            or any(_root(rows, aid) != owner for aid in receipt["monitored_attempt_ids"])):
        raise SupervisionError("supervision-lineage-mismatch")
    if _obligation_revision(rows, receipt["monitored_attempt_ids"], receipt["reason"], Path(receipt["job_registry"])) != receipt["obligation_revision"]:
        return False
    if _cleanup_only_result(receipt) is not None:
        return False  # Retire the model wake; the cleanup controller still owns its observation.
    return _pending(rows, receipt["monitored_attempt_ids"], Path(receipt["job_registry"]), receipt["reason"])


def _residue_text(receipt: dict) -> str:
    """Name survivors a finished worker left running; display only, empty when none."""
    try:
        from dispatch_contract import residue_live_pids
        rows = _rows(Path(receipt["job_registry"]),
                     set(receipt["monitored_attempt_ids"]) | {receipt["owner_attempt_id"]})
    except (OSError, ValueError, SupervisionError):
        return ""
    parts = []
    for aid in receipt["monitored_attempt_ids"]:
        pids = residue_live_pids(rows[aid][1]) if aid in rows else ()
        if pids:
            parts.append(f"{aid} finished, but the worker left process pid "
                         f"{', '.join(map(str, pids[:4]))} running")
    if not parts:
        return ""
    return (" " + "; ".join(parts) + ". The row closes by itself once that process exits; "
            "stop it if it is no longer needed. Nothing is signalled automatically.")


def _printed(name: str) -> str:
    from parent_next_directive import entrypoint
    return entrypoint(Path(__file__).resolve().parents[1], f"utilities/{name}")


def _resume_text(receipt: dict) -> str:
    """The route's start command for the notice's owner, or "" when its route cannot be read."""
    try:
        from dispatch_replacement import _route
        from parent_next_directive import resume_command
        rows = _rows(Path(receipt["job_registry"]), {receipt["owner_attempt_id"]})
        path, _route_doc = _route(Path(receipt["job_registry"]), receipt["owner_attempt_id"],
                                  rows[receipt["owner_attempt_id"]][1])
        return resume_command(path, receipt["job_registry"], agent_home=Path(__file__).resolve().parents[1])
    except Exception:  # noqa: BLE001 -- the notice still names the owner
        return ""


def _acknowledged_completion_result(receipt: dict) -> dict | None:
    """Return the exact all-child receipt when its current recipient ACKed it."""
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import current_attempt_row, recover_acknowledged_batch_outcome
        from dispatch_seat_handover import effective_parent
        jobs = Path(receipt["job_registry"])
        monitored = set(receipt["monitored_attempt_ids"])
        covered = {}
        for attempt in sorted(monitored):
            row = current_attempt_row(jobs, attempt)
            if row is None:
                return None
            meta = row.metadata
            target = effective_parent(meta, jobs)
            if (meta.get("parent_sid") != receipt["recipient_thread_id"]
                    or not meta.get("delivery_id") or not target):
                return None
            record = pending.read(jobs.resolve(strict=True).parent,
                                  meta["parent_sid"], meta["delivery_id"])
            body = (record or {}).get("receipt")
            if not isinstance(body, dict):
                continue
            attempt_ids = {child.get("attempt_id") for child in body.get("children", [])
                           if isinstance(child, dict)}
            if not attempt_ids:
                continue
            recovered = recover_acknowledged_batch_outcome(
                jobs, attempt_ids, recipient_sid=target,
                parent_attempt_id=body.get("parent_attempt_id", ""),
                parent_completion_delivery=meta.get("parent_completion_delivery", ""))
            if recovered is None:
                continue
            children = recovered.get("children")
            if (isinstance(children, list) and children
                    and all(isinstance(child, dict) and child.get("status") == "done"
                            and child.get("required_action") == "advance-completed"
                            for child in children)):
                covered.update({child["attempt_id"]: child for child in children})
        return {"children": list(covered.values())} if monitored and monitored <= covered.keys() else None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _completion_refusal_notice(receipt: dict) -> str:
    """Render late-bound refusal details from this receipt's exact owner row."""
    try:
        utility = _printed("dispatch_terminal_commit.py")
    except Exception:  # noqa: BLE001 -- a read-only path fallback keeps the pending notice useful
        utility = str(Path(__file__).resolve().parents[1] / "utilities" / "dispatch_terminal_commit.py")
    clean = lambda value: re.sub(r"[\x00-\x1f\x7f]", "", str(value))[:2048]
    command = shlex.join(["python3", str(utility), "inspect",
                          "--jobs", clean(receipt["job_registry"]),
                          "--attempt", clean(receipt["owner_attempt_id"])])
    try:
        from dispatch_completion_join import exact_attempt_row
        import dispatch_terminal_commit
        row = exact_attempt_row(Path(receipt["job_registry"]), receipt["owner_attempt_id"])
        diagnosis = dispatch_terminal_commit.inspect_owner_completion(
            Path(receipt["job_registry"]), row.status, row.metadata)
    except Exception:  # noqa: BLE001 -- closure remains controller-owned if the exact row is unavailable
        diagnosis = {}
    refusal = diagnosis.get("last_refusal") if isinstance(diagnosis, dict) else None
    if not isinstance(refusal, dict):
        checkpoint = diagnosis.get("checkpoint") if isinstance(diagnosis, dict) else None
        detail = f" The current checkpoint is {checkpoint}." if isinstance(checkpoint, str) else ""
        return ("No current refusal record is available." + detail
                + " The existing controller retries closure automatically; read-only diagnosis: " + command)
    reason = refusal.get("reason")
    raw_detail = refusal.get("detail", "")
    nested_output_invalid = reason == "producer-finalize-failed" and isinstance(raw_detail, str) \
        and raw_detail.startswith("output-invalid:")
    if reason != "output-invalid" and not nested_output_invalid:
        safe_reason = reason if isinstance(reason, str) and re.fullmatch(r"[a-z0-9-]{1,80}", reason) else "closure-refused"
        return (f"The latest closure refusal is {safe_reason}. The existing controller retries when possible; "
                "read-only diagnosis: " + command)
    # Producer validation records a semicolon-separated list of code:locator
    # values. Keep only that known shape and strip control characters.
    details = []
    if isinstance(raw_detail, str):
        for item in raw_detail.split(";"):
            match = re.match(r"[a-z0-9-]{1,80}:[^;\x00-\x1f\x7f]{1,500}", item)
            if match:
                details.append(match.group(0))
    safe_detail = "; ".join(details) if details else "invalid payload locator"
    path = refusal.get("record_path")
    safe_path = clean(path) if isinstance(path, str) else "record path unavailable"
    remediation = refusal.get("remediation")
    safe_remediation = (remediation if isinstance(remediation, str) and "\n" not in remediation and "\r" not in remediation
                        else "Correct the locator, mark its exact payload directory for self-exclusion, or move it outside the cycle.")
    cause = "producer-finalize-failed (output-invalid)" if nested_output_invalid else "output-invalid"
    return (f"The latest closure refusal is {cause}: {safe_detail}. Record: {safe_path}. "
            f"{safe_remediation} The existing controller retries closure automatically; read-only diagnosis: {command}")


def _cleanup_only_result(receipt: dict) -> dict | None:
    if receipt["reason"] in {"process-unverifiable", "join-deadline", "join-observer-failed",
                              "supervisor-exited", "watch-deadline", "receiver-unavailable"}:
        return _acknowledged_completion_result(receipt)
    return None


def _cleanup_only_text(receipt: dict, completed: dict) -> str:
    attempts = ", ".join(receipt["monitored_attempt_ids"])
    text = (f"The exact settled completion for {attempts} was already received by the current parent. "
            "This notice retains cleanup observation only; the process cleanup is still live or unknown "
            "under the existing controller. Do not restart the goal, repeat completed follow-through, "
            "or ask the user whether to wait or cancel. This notice grants no process signal, registry "
            "rewrite, retry, or cancellation. Preserve any new failure, conflict, unfinished work, or "
            "direct user question. The parent result remains the exact received result.")
    try:
        import dispatch_pending_delivery as pending
        from dispatch_completion_join import current_attempt_row
        completion_times = []
        for child in completed.get("children", []):
            if not isinstance(child, dict) or not isinstance(child.get("attempt_id"), str):
                continue
            row = current_attempt_row(Path(receipt["job_registry"]), child["attempt_id"])
            if row is None or not row.metadata.get("parent_sid") or not row.metadata.get("delivery_id"):
                continue
            record = pending.read(Path(receipt["job_registry"]).resolve(strict=True).parent,
                                  row.metadata["parent_sid"], row.metadata["delivery_id"])
            timing = (record or {}).get("timestamp_provenance") or {}
            first_send = timing.get("first_send_at_utc") or "unknown"
            received = timing.get("receive_at_utc") or "unknown"
            first_source = timing.get("first_send_source") or "unknown"
            receive_source = timing.get("receive_source") or "unknown"
            completion_times.append(f"{child['attempt_id']} first observed send={first_send} ({first_source}), "
                                    f"exact receipt={received} ({receive_source})")
        if completion_times:
            text += " Original completion delivery timing (UTC wall clock): " + "; ".join(completion_times) + "."

        rows = _rows(Path(receipt["job_registry"]), set(receipt["monitored_attempt_ids"]))
        from dispatch_contract import residue_live_pids
        residues = []
        for attempt in receipt["monitored_attempt_ids"]:
            row = rows.get(attempt)
            pids = residue_live_pids(row[1]) if row else ()
            if pids:
                residues.append(f"{attempt} residue pid(s) {', '.join(map(str, pids[:4]))}")
        if residues:
            text += " Observed cleanup residue: " + "; ".join(residues) + "."
    except (OSError, ValueError, SupervisionError):
        pass
    return text


# Set on a delivered record (not its receipt) when its carrier started the continuation.
CONTINUED_KEY = "parent_continuation"


def continue_for_parent(record: dict, *, session_id: str, recipient_kind: str,
                        environ=None, arm=None) -> dict | None:
    """Let the route parent resume an answer or exact resultless replacement notice.

    Only the route's parent launches it (`route_authority.require_replacement_parent`). The carrier
    that kept its claim for `session_id` runs under that session's own identity and arms the route's
    existing `start` path. Exact replacement records use their source edge; ordinary answers keep
    their existing answer-resume behavior."""
    receipt = record.get("receipt") if isinstance(record, dict) else None
    if (not session_id or not isinstance(receipt, dict) or receipt.get("kind") != KIND
            or receipt.get("reason") not in {ANSWER_AWAITING_PARENT, "no-progress"}):
        return None
    try:
        from dispatch_replacement import _route, source_reservation
        from session_identity import session_env
        harness = recipient_kind.split("-", 1)[0]
        jobs = Path(receipt["job_registry"])
        owner = receipt["owner_attempt_id"]
        route_file, route = _route(jobs, owner, _rows(jobs, {owner})[owner][1])
        env = {**(os.environ if environ is None else environ),
               session_env()[harness][0]: session_id, "AGENT_DISPATCH_CALLER_HARNESS": harness}
        if arm is None:
            from capacity_auto_resume import arm
        result = {"reason": receipt["reason"], "route_id": route.get("route_id")}
        if receipt["reason"] == "no-progress" or source_reservation(jobs, owner):
            result["source_attempt_id"] = owner
        return arm(result, route_file, jobs, environ=env)
    except Exception:  # noqa: BLE001 -- the notice still carries the start command
        return None


def render_text(receipt: dict, *, continued: dict | None = None) -> str:
    receipt = validate(receipt)
    completed = _cleanup_only_result(receipt)
    if completed is not None:
        return _cleanup_only_text(receipt, completed)
    if receipt["reason"] == ANSWER_AWAITING_PARENT:
        if continued:
            return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                    "the answer is kept. This session's runtime scheduled the route's start under the parent's "
                    "identity; the existing launch checks still decide whether a new owner starts. The result "
                    "will arrive through the existing notice path. This is a handoff, not completion; do not "
                    "ask for the answer again.")
        command = _resume_text(receipt)
        return ("Another session answered owner " + receipt["owner_attempt_id"] + ", which ended BLOCKED; "
                "the answer is kept. Only this route's parent session launches its continuation, so "
                "continue the work with the route's start command: the new owner receives the answer "
                "first. Do not ask for the answer again." + (" " + command if command else ""))
    if receipt["reason"] == "no-progress":
        if continued:
            return ("The route parent has an armed start for the remaining replacement work. Its runtime "
                    "will recheck the exact process, claim, and parent authority before launching; this is "
                    "handoff evidence, not worker completion. The result will arrive through the existing "
                    "notice path.")
        command = _resume_text(receipt)
        return ("A resultless replacement still needs its route parent to continue it. No parent start "
                "was observed; the exact process, claim, and launch checks remain in force. "
                + ("Use the route start command: " + command if command else "Inspect the exact route and owner."))
    if receipt["reason"] == "owner-input-undelivered":
        command = shlex.join(["python3", _printed("capability-route.py"),
                              "correct", "--jobs", receipt["job_registry"],
                              "--attempt-id", receipt["owner_attempt_id"]])
        return ("A user correction was not delivered, or its delivery is unknown. "
                "The execution result is unchanged. Read the exact input receipts, tell the user "
                "what remains unconfirmed, and do not resend an unknown input automatically. " + command)
    if receipt["reason"] == "workflow-completion-pending":
        return ("The owner result remains PASS, but workflow/route/report closure is pending. "
                "The completion controller owns closure; this is not a running model. "
                "The notice authorizes no new execution. " + _completion_refusal_notice(receipt))
    if receipt["reason"] == "closure-blocked":
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} inspect --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        return ("The owner result remains PASS, but its own workflow/route closure can never complete from "
                "this exact attempt -- a proven permanent reason (a later attempt already claimed this route "
                "node, or the route's completion marker no longer matches its recorded evidence), not an "
                "ordinary in-flight wait. Retrying this attempt's closure will not help. Explain this to the "
                "user and inspect the exact gate reason before deciding a recovery path (a fresh dispatch of "
                "the same route node, or manual repair of the marker/evidence). Read-only diagnosis: " + command)
    if receipt["reason"] == "parent-session-ended":
        return ("The exact Codex parent session ended. The recorded child attempts continue unchanged, "
                "and their completion and delivery obligations remain in the durable batch record. "
                "The existing controller resumes them automatically when that parent or a confirmed "
                "same-seat successor is active; nothing was cancelled or retried.")
    if receipt["reason"] in {"watch-deadline", "receiver-unavailable"}:
        utility = _printed("dispatch_terminal_commit.py")
        command = (f"python3 {shlex.quote(str(utility))} finish --jobs {shlex.quote(receipt['job_registry'])} "
                   f"--attempt {shlex.quote(receipt['owner_attempt_id'])}")
        detail = ("The completion watch reached its own deadline (about a day) without the batch settling"
                  if receipt["reason"] == "watch-deadline" else
                  "The completion watch could not reach its delivery gateway, and every monitored attempt "
                  "is already terminal")
        return (f"{detail}, so it recorded this notice and stopped instead of running forever. The owner "
                "result and workflow/route closure state are unchanged -- nothing was retried or discarded. "
                "The same observation duty stays retained: the existing controller, orphan recovery, or session "
                "sweep resumes it automatically without a manual retry command; this notice alone does not. "
                "Existing transaction recovery: " + command)
    utility = _printed("dispatch-registry.py")
    operation = "resolve-terminal-conflict" if receipt["reason"] == "terminal-evidence-conflict" else "reconcile"
    commands = [f"python3 {shlex.quote(str(utility))} {operation} --jobs "
                f"{shlex.quote(receipt['job_registry'])} --attempt {shlex.quote(aid)}"
                for aid in receipt["monitored_attempt_ids"]]
    disposition = (
        "Compare the committed result with the conflicting evidence and explain the discrepancy. "
        "The read-only command previews the exact conflict. After reviewing and recording the evidence, "
        "use the same command with --review-evidence <report> --expected-row-sha256 <preview-hash> --apply "
        "to retain the committed result and release consumption. A new conflict requires a fresh review. "
        if receipt["reason"] == "terminal-evidence-conflict" else
        "Keep unresolved observation with the existing recovery controller. An inspection notice alone "
        "does not require a user decision. Ask about cancellation only when actual unfinished work "
        "requires that decision under its existing authority; "
    )
    return ("Hearting supervision needs attention. This is not workflow completion. "
            f"reason={receipt['reason']} owner={receipt['owner_attempt_id']}. "
            "The controller retains waiting/recovery responsibility. Explain the blockage to the user "
            "and inspect these exact attempts. Do not infer death, erase rows, or retry from this notice. "
            + disposition +
            "an accepted notification does not close the work. Read-only diagnosis: " + " ; ".join(commands)
            + _residue_text(receipt))


def context(receipt: dict, delivery_id: str) -> dict:
    return {"threadId": receipt["recipient_thread_id"], "input": [],
            "clientUserMessageId": delivery_id,
            "additionalContext": {"hearting-supervision": {"kind": "application", "value": render_text(receipt)}}}


def _project_replacement_lineage(edges: list[dict], roots: set[str]) -> list[dict]:
    """Collapse observed exact edges to one bounded original-to-final row."""
    outgoing = {}
    for edge in edges:
        original = edge.get("original_attempt_id")
        target = edge.get("replacement_attempt_id")
        if not isinstance(original, str) or not isinstance(target, str):
            raise SupervisionError("replacement-lineage-invalid")
        previous = outgoing.get(original)
        if previous is not None and previous.get("replacement_attempt_id") != target:
            raise SupervisionError("replacement-lineage-conflict")
        outgoing[original] = edge
    projected = []
    for root in sorted(roots):
        first = outgoing.get(root)
        if first is None:
            continue
        target = first["replacement_attempt_id"]
        seen = {root}
        while target in outgoing:
            if target in seen:
                raise SupervisionError("replacement-lineage-cycle")
            seen.add(target)
            target = outgoing[target]["replacement_attempt_id"]
        projected.append({**first, "replacement_attempt_id": target})
    return projected


def wait_for_batch(*, join: Callable[[set[str]], dict], attempts: set[str],
                   jobs: Path, parent_attempt_id: str = "", emit: Callable[[dict], None] | None = None,
                   on_timeout: Callable[[set[str]], None] | None = None,
                   deadline: float | None = None,
                   stop_check: Callable[[], str] | None = None,
                   replacement_checkpoint: Callable | None = None) -> dict:
    """One shared wait loop. Join deadlines are checkpoints, never death votes.

    Execution boundaries retain their finite budgets. The parent receives one
    durable notice for this exact batch while this controller retains the wait.
    A failed queue write is retried at the next bounded checkpoint, not discarded.

    `deadline` and `stop_check` are both optional and, left `None`, leave this
    loop's behavior exactly as before (every existing caller). A caller that
    itself owns an unfinishable-watch budget -- one process that must not run
    forever -- supplies one or both: `deadline` (a `time.monotonic()` value;
    once reached after a join timeout, this records one `watch-deadline`
    notice and returns `{"state": "watch-expired", ...}` instead of looping
    again) and/or `stop_check` (called once per join timeout; a non-empty
    REASONS token it returns halts the same way, under that reason). Neither
    ever fires mid-join -- only at the same checkpoint an ordinary
    `join-deadline` notice already used.
    """
    ordinal = 0
    lineage_edges = []
    lineage = []
    roots = set(attempts)
    attention = []
    while True:
        observer_error, deferred = "", None
        try:
            receipt = join(set(attempts))
            if replacement_checkpoint is not None:
                effective, edges, attention = replacement_checkpoint(set(attempts))
                lineage_edges.extend(edge for edge in edges if edge not in lineage_edges)
                lineage = _project_replacement_lineage(lineage_edges, roots)
                if set(effective) != set(attempts):
                    attempts = set(effective)
                    continue
            if receipt.get("state") != "timeout":
                if lineage:
                    receipt = {**receipt, "replacement_lineage": sorted(lineage, key=lambda edge: edge["original_attempt_id"])}
                if attention:
                    receipt = {**receipt, "replacement_attention": attention}
                return receipt
        except StopWaiting:
            raise
        except Exception as exc:
            from dispatch_completion_join import CompletionDeferred
            if isinstance(exc, CompletionDeferred):
                deferred = exc
            else:
                # Observation failure retains the exact batch and diagnostic.
                observer_error = str(exc)
        ordinal += 1
        halt_reason = ""
        if not observer_error:
            if stop_check is not None:
                try:
                    halt_reason = stop_check() or ""
                except StopWaiting:
                    raise
                except Exception as exc:
                    observer_error = str(exc)
            if not halt_reason and deadline is not None and time.monotonic() >= deadline:
                halt_reason = "watch-deadline"
        if halt_reason:
            try:
                records = materialize(jobs, attempts, reason=halt_reason)
            except (OSError, ValueError, pending_delivery.PendingDeliveryError):
                records = []
            result = {"state": "watch-expired", "reason": halt_reason, "delivery_records": records}
            if lineage:
                result["replacement_lineage"] = sorted(lineage, key=lambda edge: edge["original_attempt_id"])
                result["children"] = [{"attempt_id": aid} for aid in sorted(attempts)]
            if attention:
                result["replacement_attention"] = attention
            return result
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
                  "observer_error": observer_error,
                  **({"reason": str(deferred), "retry_when": getattr(deferred, "retry_when", "")} if deferred else {})})
        # A failed observer receives bounded backoff; execution budgets remain
        # the execution boundary's responsibility, never this observer's vote.
        pause = 30.0 if observer_error or deferred else 0.05
        if deadline is not None:
            pause = min(pause, max(0.0, deadline - time.monotonic()))
        time.sleep(pause)


def wait_for_child_settlement(*, jobs: Path, attempts: set[str],
                              parent_attempt_id: str,
                              join: Callable[[set[str]], dict],
                              reconcile: Callable[[set[str]], object],
                              emit: Callable[[dict], None]) -> None:
    """Retain an unresolved child after notification without another model turn.

    Exact recovery and the shared attempt policy decide when work is settled.
    A ready join receipt alone does not prove cleanup or registry closure.
    The ordinary wait controller retains the obligation and parent handback.
    """
    def observe(monitored: set[str]) -> dict:
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        reconcile(monitored)
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        receipt = join(monitored)
        reconcile(monitored)
        if not _pending(_rows(jobs, monitored), sorted(monitored), jobs):
            return {"state": "settled"}
        if receipt.get("state") != "timeout":
            # A terminal-but-unclosed observation may return immediately.
            # Retry evidence collection at a bounded cadence, without tokens.
            time.sleep(1.0)
        return {"state": "timeout"}

    wait_for_batch(join=observe, attempts=attempts, jobs=jobs,
                   parent_attempt_id=parent_attempt_id, emit=emit)
