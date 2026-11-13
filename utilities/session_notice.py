#!/usr/bin/env python3
"""Retained ordinary notices delivered by the existing native peer courier."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

import dispatch_pending_delivery as pending_delivery

KIND = "notice"
MAX_TEXT = 600
KEYS = frozenset({"schema_version", "kind", "subject", "text", "required_action", "key"})
LINEAGE_PREFIX = "session-notice-lineage-v1:"
# The one existing key shape a finished experiment run writes
# (``resource_progress.observe_experiment``). Classification and the send-time
# supersede check read only these receipt fields; nothing here is new state.
EXPERIMENT_FINAL_KEY = re.compile(r"^experiment:(?P<run_id>[^:]+):final:")
TERMINAL_ACTIONS = frozenset({"report-to-user"})
TERMINAL = "terminal"
PROGRESS = "progress"


def _registry(jobs=None) -> Path:
    if jobs:
        return Path(jobs)
    from dispatch_contract import stable_state_root
    return Path(os.environ.get("AGENT_DISPATCH_JOBS") or stable_state_root(os.environ) / "jobs.log")


def receipt_digest(receipt: dict) -> str:
    return pending_delivery._canonical_receipt_digest(receipt)


def validate(receipt: dict) -> dict:
    if (not isinstance(receipt, dict) or set(receipt) not in (KEYS, KEYS | {"answer_binding"})
            or receipt.get("kind") != KIND
            or receipt.get("schema_version") != 1
            or any(not isinstance(receipt[key], str) or not receipt[key]
                   for key in ("subject", "text", "required_action", "key"))
            or len(receipt["text"]) > MAX_TEXT):
        raise ValueError("notice-shape-invalid")
    if "answer_binding" in receipt:
        import human_wait_state as waits
        binding = receipt["answer_binding"]
        if (not waits.message_binding_valid(binding)
                or receipt["key"] != "question-answer:" + binding["request_id"]
                or receipt["subject"] != "Question answer" or receipt["text"] != "private-answer"
                or receipt["required_action"] != "read-question-answer"):
            raise ValueError("notice-answer-binding-invalid")
    if len(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 2048:
        raise ValueError("notice-receipt-too-large")
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
            record: dict, *, ensure_runner=True):
    import peer_obligations
    storage = record.get("_storage_key") or session_id
    jobs, lineage = pending_lineage(record, harness, storage, jobs)
    identity = {"jobs": str(jobs), "storage_recipient": storage,
                "recipient_harness": harness, "recipient_session_id": lineage["session_id"],
                "lineage": lineage}
    store = peer_obligations.ObligationStore(jobs.parent)
    legacy_identity = {"state_root": str(jobs.parent), "storage_sid": storage,
                       "harness": harness, "delivery_id": record["delivery_id"],
                       "receipt_digest": record["receipt_digest"]}
    duty = store.get(peer_obligations.stable_duty_id("session-notice", legacy_identity)) or store.create(
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


def pending_lineage(record, harness, storage, jobs=None):
    """Read the original notice authority; old notices keep their storage provenance."""
    validate_pending_record(record)
    from harness_capabilities import parent_completion
    if (not storage or parent_completion(harness).get("carrier") != record.get("recipient_kind")
            or record.get("recipient_digest") != pending_delivery.recipient_digest(storage)):
        raise ValueError("notice-storage-lineage-conflict")
    values = [value[len(LINEAGE_PREFIX):] for value in record.get("lineage", [])
              if isinstance(value, str) and value.startswith(LINEAGE_PREFIX)]
    if not values:
        tag = "notice-" + hashlib.sha256(f"{KIND}\0{record['receipt']['key']}".encode()).hexdigest()[:32]
        if (record.get("attempt_ids") != [tag] or record.get("parent_attempt_id") != tag
                or record.get("row_revisions") != {tag: KIND}):
            raise ValueError("notice-lineage-missing")
    if len(values) > 1:
        raise ValueError("notice-lineage-ambiguous")
    stored = json.loads(values[0]) if values else None
    if values and not isinstance(stored, dict):
        raise ValueError("notice-lineage-invalid")
    if isinstance(stored, dict) and set(stored) == {"jobs", "binding"}:
        raw_jobs = stored["jobs"]
        if not isinstance(raw_jobs, str) or not Path(raw_jobs).is_absolute() or Path(raw_jobs).is_symlink():
            raise ValueError("notice-registry-invalid")
        recorded_jobs = Path(raw_jobs).resolve(strict=False)
        if jobs is not None and recorded_jobs.parent != Path(jobs).resolve(strict=False).parent:
            raise ValueError("notice-registry-conflict")
        jobs, stored = recorded_jobs, stored["binding"]
        if not isinstance(stored, dict):
            raise ValueError("notice-lineage-invalid")
    lineage = _lineage(stored, harness, storage)
    return (Path(jobs) if jobs is not None else None), lineage


def notify(harness: str, session_id: str, *, key: str, subject: str, text: str,
           required_action: str = "read-notice", route_id: str = "route-free", jobs=None,
           lineage=None, exact_text=False, answer_binding=None) -> dict | None:
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
    if exact_text:
        import human_wait_state as waits
        if waits._message_answer_error(text):
            raise ValueError(waits._message_answer_error(text))
        if (not answer_binding or answer_binding.get("harness") != harness
                or answer_binding.get("session_id") != session_id
                or waits.message_answer_body(answer_binding, current=True) != text):
            raise ValueError("notice-answer-body-conflict")
    elif answer_binding is not None:
        raise ValueError("notice-answer-exact-text-required")
    receipt = {"schema_version": 1, "kind": KIND, "subject": subject,
               "text": "private-answer" if exact_text else " ".join(text.split())[:MAX_TEXT],
               "required_action": required_action, "key": key}
    if exact_text:
        receipt["answer_binding"] = dict(answer_binding)
    receipt = validate(receipt)
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
    _retain(jobs_path, carrier, harness, session_id, record)
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
        _retain(jobs_path, carrier, harness, storage, record,
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


def consumer_text(receipt: dict) -> str:
    """Only the private native payload resolves the answer body; summaries use render_text."""
    receipt = validate(receipt)
    if "answer_binding" not in receipt:
        return render_text(receipt)
    import human_wait_state as waits
    body = waits.message_answer_body(receipt["answer_binding"])
    return f"{receipt['subject']} (request={receipt['answer_binding']['request_id']}):\n{body}"


def classify(receipt: dict) -> str:
    """Terminal (failure/completion) or progress, from existing receipt fields only.

    One helper for the whole notice path: the runner's group selection and the
    batch ordering both ask this, so a notice cannot be "terminal" for one and
    "progress" for the other.
    """
    try:
        receipt = validate(receipt)
    except ValueError:
        return PROGRESS
    key = receipt["key"]
    if (receipt["required_action"] in TERMINAL_ACTIONS
            or EXPERIMENT_FINAL_KEY.match(key) is not None
            or receipt["subject"].startswith("experiment final")):
        return TERMINAL
    return PROGRESS


# A new or changed attention only. The producer (`resource_progress`) posts the same
# attention again at a periodic checkpoint as `attention-checkpoint:`, which is not this.
EXPERIMENT_ATTENTION_KEY = re.compile(r"^experiment:[^:]+:attention:")


def wakes_resting_recipient(record: dict) -> bool:
    """Whether a resting recipient is woken for this notice instead of waiting for its next input.

    Only what needs a decision: an experiment's new or changed attention, and a final that
    did not succeed (or whose outcome is not known). A normal finish, a periodic checkpoint
    and the same attention re-posted at a checkpoint wait for the next input as before, so
    an unchanged attention costs no further model turn. This reads the same receipt
    `classify` reads and changes none of its answers; leg 1 (alarm classification) may
    move this rule into it.
    """
    receipt = record.get("receipt") if isinstance(record, dict) else None
    try:
        key = validate(receipt)["key"]
    except ValueError:
        return False
    if EXPERIMENT_ATTENTION_KEY.match(key):
        return True
    if EXPERIMENT_FINAL_KEY.match(key):
        run = _run_state(record)
        return not (isinstance(run, dict) and run.get("status") == "succeeded")
    return False


def _run_state(record: dict):
    """The runner-published state for this run notice, or ``None``.

    ``run-states.json`` is the one cheap derived observation the obligation
    runner publishes; this path never opens a run registry or a jobs log on its
    own account (the completion projection below reads its own exact row).
    """
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    key = receipt.get("key")
    if not isinstance(key, str):
        return None
    match = EXPERIMENT_FINAL_KEY.match(key)
    if match is None:
        return None
    try:
        import dispatch_recipient_inbox as inbox
        payload = inbox.read_run_states(inbox.dispatch_inbox_root())
    except (OSError, ValueError, TypeError):
        return None
    runs = payload.get("runs") if isinstance(payload, dict) else None
    run = runs.get(match.group("run_id")) if isinstance(runs, dict) else None
    return run if isinstance(run, dict) else None


def _run_notice_superseded(record: dict, *, notice_context=None):
    """``True`` only when the exact parent completion already covers this run notice.

    Chain (owner note N1): notice key -> run id -> the runner's published run
    state -> the recorded parent attempt and its jobs -> the existing exact
    completion projection. Any unreadable, missing or ambiguous step returns
    ``None`` (unknown), and an unknown notice is never folded.
    """
    run = _run_state(record)
    if run is None:
        return None
    status = run.get("status")
    if not isinstance(status, str) or not status or status == "running":
        return None
    attempt = run.get("parent_attempt_id")
    jobs = run.get("jobs")
    if not isinstance(attempt, str) or not attempt or not isinstance(jobs, str) or not jobs:
        return None
    try:
        from dispatch_completion_join import current_attempt_row
        from dispatch_seat_handover import effective_parent
        jobs_path = Path(jobs).resolve(strict=True)
        row = current_attempt_row(jobs_path, attempt)
        if row is None:
            return None
        target = effective_parent(row.metadata, jobs_path)
        if not target:
            return None
        from dispatch_supervision import _recorded_completion_result
        completion = {"job_registry": str(jobs_path), "recipient_thread_id": target,
                      "monitored_attempt_ids": [attempt]}
        result = _recorded_completion_result(completion, require_ack=False,
                                             notice_context=notice_context)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None
    return True if result is not None else None


def fold_summary(record: dict) -> str | None:
    """The one-line replacement for a folded run notice, or ``None``.

    Only a notice whose run already ended folds; the parent completion receipt
    remains the primary replacement and this line is what a body that also
    carries other notices can show instead of the stale individual notice.
    """
    run = _run_state(record)
    if run is None:
        return None
    status = run.get("status")
    if not isinstance(status, str) or not status or status == "running":
        return None
    receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
    match = EXPERIMENT_FINAL_KEY.match(str(receipt.get("key") or ""))
    run_id = match.group("run_id") if match else "unknown"
    exit_code = run.get("exit_code")
    exit_text = "unknown" if exit_code is None else str(exit_code)
    return f"compute run summary: run {run_id} status={status} exit={exit_text}"


def notice_is_current(record: dict, *, jobs=None, notice_context=None) -> bool:
    """A notice is retired by its delivery, never by registry state, except a
    role-bound steward-check notice, which its own duty keeps current, and a
    run notice whose exact parent completion is already recorded."""
    try:
        receipt = validate(record.get("receipt"))
    except ValueError:
        return False
    if receipt["key"].startswith("native-resume-v1:"):
        import session_resume
        return session_resume.current(record)
    if "answer_binding" in receipt:
        try:
            import human_wait_state as waits
            waits.message_answer_body(receipt["answer_binding"], current=True)
            return True
        except (OSError, ValueError, TypeError):
            return False
    if receipt["key"].startswith("steward-check:"):
        try:
            import steward_check
            return bool(steward_check.notice_is_current(record))
        except Exception:  # noqa: BLE001 - unknown evidence stays recoverable
            return True
    return _run_notice_superseded(record, notice_context=notice_context) is not True


def context(receipt: dict, delivery_id: str) -> dict:
    return {"kind": KIND, "delivery_id": delivery_id, "text": render_text(receipt)}


def gateway_delivery_id(receipt: dict) -> str:
    return "delivery-" + hashlib.sha256(f"{KIND}\0{validate(receipt)['key']}".encode()).hexdigest()


def validate_pending_record(record: dict, **_kwargs) -> dict:
    receipt = validate(record.get("receipt"))
    if record.get("receipt_digest") != receipt_digest(receipt):
        raise ValueError("notice-digest-mismatch")
    return receipt
