#!/usr/bin/env python3
"""Join one exact child batch and deliver one bounded receipt to a managed gateway."""

from __future__ import annotations

import argparse
import copy
from collections import Counter, deque
import errno
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import WRAPPER_PARENT_HARNESSES, process_namespace_identity
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    CompletionDeferred,
    CODEX_QUEUE_PARENT_DELIVERY,
    MANAGED_SESSION_PARENT_DELIVERY,
    OPEN_STATES,
    advance_delivery_timing,
    current_children,
    current_session_children,
    delivery_timing_fields,
    wait_for_delivery_projection,
    receipt_with_stage_advance,
    validate_delivery_timing,
    completion_followup_text,
)
import dispatch_pending_delivery as pending_delivery  # noqa: E402
import dispatch_batch_obligations as batch_obligations  # noqa: E402
from dispatch_supervision import StopWaiting
import codex_queue_delivery
from codex_queue_dispatch import resolve_queue_socket
import human_gate_receipt
import dispatch_notice_receipt as notice_receipt  # noqa: E402


MAX_RESPONSE_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 2048
from dispatch_receipt_identity import COMPLETION_REASONS as ALLOWED_REASONS, COMPLETION_ACTIONS as REQUIRED_ACTIONS


class CompletionError(RuntimeError):
    """The exact batch or gateway control contract is invalid."""


class ObserverOwnershipLost(StopWaiting):
    """This observer no longer owns the exact duty lease and durable row."""


PARENT_SESSION_ENDED = "parent-session-ended"
JOIN_CHECKPOINT_SECONDS = 5.0


def _observer_ownership_lost(args) -> bool:
    duty_id = getattr(args, "_obligation_id", None)
    fd = getattr(args, "obligation_lock_fd", None)
    return bool(duty_id and fd is not None
                and not batch_obligations.observer_owns_current(args.jobs, duty_id, fd))


def _update_duty(args, duty_id, **changes):
    fd = getattr(args, "obligation_lock_fd", None)
    if fd is None:
        return batch_obligations.update(args.jobs, duty_id, **changes)
    updated = batch_obligations.update_for_observer(
        args.jobs, duty_id, fd, **changes)
    if updated is None:
        raise ObserverOwnershipLost("completion-observer-ownership-lost")
    return updated


def _parent_proc_state(pid):
    try:
        raw = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return "gone", None
    except (OSError, TypeError, ValueError):
        return "unknown", None
    try:
        fields = raw[raw.rfind(")") + 2:].split()
        if fields[0] in {"Z", "X"}:
            return "gone", None
        return "live", fields[19]
    except (IndexError, ValueError):
        return "unknown", None


def capture_parent_lifetime(args):
    """Bind an interactive Codex recipient to its exact native TUI process."""
    session_id = getattr(args, "parent_session_id", None)
    if not session_id or getattr(args, "thread_id", None) != session_id:
        return None
    try:
        from pane_ownership import codex_tui_process_bindings
        bindings = codex_tui_process_bindings(
            session_id,
            pane_hint=os.environ.get("HERDR_PANE_ID") or None,
        )
        namespace = process_namespace_identity()
        matches = [item for item in bindings
                   if item.get("session_id") == session_id
                   and namespace and process_namespace_identity(item.get("pid")) == namespace]
        return {**matches[0], "pid_namespace": namespace} if len(matches) == 1 else None
    except Exception:
        return None


def _confirmed_parent_successor(args, attempts, *, current_session=None):
    """Use only the exact seat-handover bindings already sealed to these attempts."""
    try:
        import dispatch_seat_handover as handover
        rows = handover.latest_rows(args.jobs)
        for attempt in attempts:
            row = rows.get(attempt)
            if row is None:
                continue
            successor = handover.effective_parent(row[1], args.jobs)
            if (successor and successor != args.parent_session_id
                    and (current_session is None or successor == current_session)):
                return successor
    except Exception:
        return ""
    return ""


def parent_session_stop_reason(args, attempts):
    """Return a stop reason only after the exact parent lifetime is disproved.

    Missing process/session evidence remains unknown. A current same-seat
    successor with exact attempt bindings keeps the delivery obligation alive.
    """
    binding = getattr(args, "parent_lifetime", None)
    if not isinstance(binding, dict) or not args.parent_session_id:
        return ""
    namespace = binding.get("pid_namespace")
    if not namespace or process_namespace_identity() != namespace:
        return ""
    try:
        pid = int(binding.get("pid"))
        start = str(binding.get("pid_start") or "")
    except (TypeError, ValueError):
        return ""
    state, observed_start = _parent_proc_state(pid)
    if state == "unknown":
        return ""
    if state == "live" and observed_start == start and start:
        try:
            from pane_ownership import _native_session
            current_session = _native_session(pid, "codex")
        except Exception:
            return ""
        if not current_session or current_session == args.parent_session_id:
            return ""
        if _confirmed_parent_successor(args, attempts, current_session=current_session):
            return ""
        return PARENT_SESSION_ENDED

    # The recorded PID/start no longer names the original TUI. A new live TUI
    # for the same native session is a normal resume and retains the duty.
    try:
        from pane_ownership import codex_tui_processes_for_session
        bindings = codex_tui_processes_for_session(args.parent_session_id)
        matches = [item for item in bindings
                   if item.get("session_id") == args.parent_session_id
                   and process_namespace_identity(item.get("pid")) == namespace]
    except Exception:
        return ""
    if len(matches) == 1:
        args.parent_lifetime = {**matches[0], "pid_namespace": namespace}
        return ""
    if len(matches) > 1:
        return ""
    if _confirmed_parent_successor(args, attempts):
        return ""
    return PARENT_SESSION_ENDED


def _join_checkpoint(args, attempts):
    """Run the existing exact join with a short bounded checkpoint for supervision."""
    has_parent_lifetime = bool(getattr(args, "parent_lifetime", None))
    is_resumed_observer = bool(getattr(args, "_resume_execution", False))
    if not has_parent_lifetime and not is_resumed_observer:
        return run_join(args, attempts)
    bounded = copy.copy(args)
    interval = float(getattr(args, "interval", 2.0))
    checkpoint = (min(max(float(args.interval), 0.05), JOIN_CHECKPOINT_SECONDS)
                  if has_parent_lifetime else JOIN_CHECKPOINT_SECONDS)
    bounded.timeout = min(checkpoint, max(float(args.timeout), 0.05))
    bounded.interval = min(max(interval, 0.05), 2.0)
    return run_join(bounded, attempts)


class NoticeWatcher:
    """One courier for typed gate and recovery notices in this session."""

    def __init__(
        self, args: argparse.Namespace, attempts: set[str], capability: dict[str, Any] | None
    ):
        self.args = args
        self.root = args.jobs.resolve(strict=False).parent
        self.recipient = args.parent_session_id or ""
        self.owner = f"codex-notice:{os.getpid()}"
        self.attempts = set(attempts)
        self.epoch = int(capability["epoch"]) if capability else 0
        self._scan_cursor = 0
        self.stop = __import__("threading").Event()
        self.thread = __import__("threading").Thread(target=self._run, daemon=True)
        self.errors = deque(maxlen=32)
        self.error_counts: Counter[str] = Counter()
        self.error_count_total = 0

    def _record_error(self, reason: str) -> None:
        self.errors.append(reason)
        self.error_count_total += 1
        if reason in self.error_counts or len(self.error_counts) < 63:
            self.error_counts[reason] += 1
        else:
            self.error_counts["other"] += 1

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=min(max(self.args.interval * 2, 0.2), 5.0))

    def _run(self) -> None:
        if not self.recipient:
            return
        directory = pending_delivery.record_directory(self.root, self.recipient)
        while not self.stop.is_set():
            try:
                if directory.is_dir() and not directory.is_symlink():
                    paths = self._candidate_paths(directory)
                    for path in paths:
                        self._one(path)
            except (OSError, ValueError) as exc:
                self._record_error(type(exc).__name__)
            self.stop.wait(min(max(self.args.interval, 0.05), 2.0))

    def _candidate_paths(self, directory: Path) -> list[Path]:
        """Bound work while rotating eligible records to avoid lexical starvation."""
        all_paths = sorted(directory.glob("delivery-*.json"))
        eligible = []
        for path in all_paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            receipt = value.get("receipt") if isinstance(value, dict) else None
            if (isinstance(receipt, dict) and notice_receipt.is_notice(receipt)
                    and value.get("state") in {"pending", "claimed", "sent-ambiguous"}):
                eligible.append(path)
        if not eligible:
            return []
        start = self._scan_cursor % len(eligible)
        selected = [eligible[(start + i) % len(eligible)] for i in range(min(16, len(eligible)))]
        self._scan_cursor = (start + len(selected)) % len(eligible)
        return selected

    def _one(self, path: Path) -> None:
        sent = False
        retry_failure = "notice-not-consumed"
        try:
            queue_socket = getattr(self.args, "queue_socket", None)
            capability = (
                {"epoch": 1} if queue_socket
                else negotiate_human_gate(self.args)
            )
            if capability is None:
                raise CompletionError("notice-recipient-unavailable")
            self.epoch = int(capability["epoch"])
            record = json.loads(path.read_text(encoding="utf-8"))
            receipt = record.get("receipt") if isinstance(record, dict) else None
            if not notice_receipt.is_notice(receipt):
                return
            notice_receipt.validate_pending_record(
                record, jobs=self.args.jobs,
                expected_thread_id=self.recipient,
                expected_epoch=self.epoch,
                expected_attempts=self.attempts,
                expected_sealed_batch_id=self.args.sealed_batch_id,
            )
            target = queue_target(self.args.jobs, self.attempts, self.recipient) if queue_socket else ""
            retry_context = f"{self.recipient}:{self.epoch}:{target}"
            # Reclaim only after checking the live monotonic deadline.  The
            # old carrier's lease may still be valid, so a fresh watcher must
            # preserve that claim; using deadline+1 here incorrectly steals a
            # live lease. Sent-ambiguous is made eligible for the existing
            # gateway lookup/no-resend path, not for a blind second send.
            if record.get("state") in {"claimed", "sent-ambiguous"}:
                deadline = record.get("claim_deadline_ns")
                if not isinstance(deadline, int) or time.monotonic_ns() < deadline:
                    raise pending_delivery.PendingDeliveryError(
                        "pending-delivery-claim-refused", "lease-not-expired"
                    )
                record = pending_delivery.reclaim(
                    self.root, self.recipient, record["delivery_id"],
                    now_ns=time.monotonic_ns(),
                )
            claimed = pending_delivery.claim(
                self.root, self.recipient, record["delivery_id"],
                claim_owner=self.owner,
                lease_seconds=300.0 if queue_socket else 30.0,
                require_generation_proof=True,
                live_recipient_generation=(self.recipient, str(self.epoch)),
                jobs=self.args.jobs,
                retry_context=retry_context,
            )
            notice_receipt.validate_pending_record(
                claimed, jobs=self.args.jobs,
                expected_thread_id=self.recipient,
                expected_epoch=self.epoch,
                expected_attempts=self.attempts,
                expected_sealed_batch_id=self.args.sealed_batch_id,
            )
            if queue_socket:
                context = notice_receipt.context(
                    receipt, notice_receipt.gateway_delivery_id(receipt)
                )
                additional = context.get("additionalContext")
                values = [value.get("value") for value in additional.values()
                          if isinstance(value, dict) and isinstance(value.get("value"), str)] \
                    if isinstance(additional, dict) else []
                if len(values) != 1:
                    raise CompletionError("native-queue-notice-context-invalid")
                fence_identity = pending_delivery.submission_fence_identity(claimed, target)
                fence = pending_delivery.read_submission_fence(
                    self.root, self.recipient, claimed["delivery_id"],
                    expected_identity=fence_identity)
                allow_add = fence is None or fence["state"] == "not-accepted"
                prepared_attempt = None

                def before_add():
                    nonlocal sent, prepared_attempt
                    prepared = pending_delivery.prepare_submission_fence(
                        self.root, self.recipient, claimed["delivery_id"],
                        claim_owner=self.owner, expected_identity=fence_identity)
                    prepared_attempt = prepared["attempt"]
                    sent = True

                # Claim and ack stay with the registered recipient; the queue is the successor's after a /clear.
                try:
                    queue_result = codex_queue_delivery.send_at_least_once(
                        queue_socket, thread_id=target,
                        client_message_id=claimed["delivery_id"], message=values[0],
                        timeout=5.0, allow_add=allow_add, retry_ambiguous=False,
                        require_message_match=True, before_add=before_add,
                    )
                except codex_queue_delivery.QueueDeliveryError as exc:
                    if (prepared_attempt is not None and not exc.ambiguous
                            and exc.reason.startswith("queue-rpc-refused:thread/queue/add:")):
                        pending_delivery.record_submission_not_accepted(
                            self.root, self.recipient, claimed["delivery_id"],
                            claim_owner=self.owner, expected_identity=fence_identity,
                            expected_attempt=prepared_attempt, reason=exc.reason)
                        sent = False
                    raise
                if queue_result.get("status") in {"queued", "consumed"}:
                    pending_delivery.record_submission_accepted(
                        self.root, self.recipient, claimed["delivery_id"],
                        claim_owner=self.owner, expected_identity=fence_identity,
                        queue_submission_id=queue_result.get("queued_submission_id"),
                        source=("queue-response" if prepared_attempt is not None
                                else "exact-observation"),
                        expected_attempt=prepared_attempt,
                    )
                if (queue_result.get("status") == "queued"
                        and not queue_result.get("already_pending")):
                    try:
                        pending_delivery.record_send(
                            self.root, self.recipient, claimed["delivery_id"],
                            source="codex-native-queue-notice-accepted",
                            event_id=str(queue_result.get("queued_submission_id") or claimed["delivery_id"]))
                    except (pending_delivery.PendingDeliveryError, OSError):
                        pass  # Timing is evidence, never delivery or execution authority.
                if queue_result.get("status") == "consumed":
                    pending_delivery.ack(self.root, self.recipient, claimed["delivery_id"],
                                         acked_by=self.owner)
                    return
                queued = codex_queue_delivery.list_queue(
                    queue_socket, target, timeout=5.0
                )
                pending_item = next((item for item in queued
                                     if item.get("clientUserMessageId") == claimed["delivery_id"]), None)
                if pending_item is None:
                    turn = codex_queue_delivery._known_consumed(
                        queue_socket, target, claimed["delivery_id"], timeout=5.0,
                        strict_history=True, expected_message=values[0],
                    )
                    if turn is not None:
                        pending_delivery.record_submission_accepted(
                            self.root, self.recipient, claimed["delivery_id"],
                            claim_owner=self.owner, expected_identity=fence_identity,
                            source="exact-observation",
                            expected_attempt=prepared_attempt,
                        )
                        pending_delivery.ack(
                            self.root, self.recipient, claimed["delivery_id"],
                            acked_by=self.owner,
                        )
                        return
                pending_delivery.mark_sent_ambiguous(
                    self.root, self.recipient, claimed["delivery_id"],
                    claim_owner=self.owner,
                )
                retry_failure = "native-queue-notice-consumption-pending"
                return
            request = {"schema_version": 1, "op": "deliver-notice",
                       "thread_id": self.recipient,
                       "recipient_epoch": self.epoch,
                       "parent_attempt_id": receipt["owner_attempt_id"],
                       "sealed_batch_id": receipt["sealed_batch_id"],
                       "delivery_id": notice_receipt.gateway_delivery_id(receipt),
                       "receipt_digest": claimed["receipt_digest"],
                       "receipt": receipt}
            result = gateway_request(self.args.control_socket, request)
            sent = True
            if result.get("status") == "accepted":
                pending_delivery.ack(
                    self.root, self.recipient, claimed["delivery_id"],
                    acked_by=self.owner,
                )
            elif result.get("status") == "sent-ambiguous":
                pending_delivery.mark_sent_ambiguous(
                    self.root, self.recipient, claimed["delivery_id"],
                    claim_owner=self.owner,
                )
            elif result.get("status") == "retryable":
                retry_failure = str(result.get("reason") or "gateway-retryable")
                self._record_error(retry_failure)
            elif result.get("status") == "rejected":
                reason = result.get("reason")
                if (result.get("ledger_commit") == "unconfirmed"
                        or (isinstance(reason, str) and reason.endswith("-ledger-commit-failed"))):
                    self._record_error("gateway-rejection-commit-unconfirmed")
                    return
                if (not isinstance(reason, str)
                        or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+(?:[:].*)?", reason)):
                    self._record_error("gateway-rejection-untyped")
                    return
                if receipt.get("kind") == "supervision" and reason != "supervision-resolved":
                    self._record_error(reason)
                    return
                pending_delivery.reject_claimed(
                    self.root, self.recipient, claimed["delivery_id"],
                    claim_owner=self.owner, reason=reason)
        except Exception as exc:
            retry_failure = str(exc) or type(exc).__name__
            if str(exc) == "supervision-resolved" and "record" in locals():
                try:
                    if record.get("state") == "pending":
                        resolved = pending_delivery.claim(
                            self.root, self.recipient, record["delivery_id"],
                            claim_owner=self.owner, lease_seconds=30.0,
                            require_generation_proof=True,
                            live_recipient_generation=(self.recipient, str(self.epoch)),
                            jobs=self.args.jobs)
                    elif "claimed" in locals():
                        resolved = claimed
                    else:
                        raise ValueError("supervision-resolution-owned-by-other-carrier")
                    pending_delivery.reject_claimed(
                        self.root, self.recipient, resolved["delivery_id"],
                        claim_owner=self.owner, reason="supervision-resolved")
                except (ValueError, pending_delivery.PendingDeliveryError):
                    pass
                return
            if sent:
                try:
                    pending_delivery.mark_sent_ambiguous(
                        self.root, self.recipient, claimed["delivery_id"], claim_owner=self.owner)
                except Exception:
                    pass
            elif "claimed" in locals():
                try:
                    pending_delivery.reclaim(self.root, self.recipient, claimed["delivery_id"],
                                         now_ns=time.monotonic_ns())
                except Exception:
                    pass
            self._record_error(str(exc))
        finally:
            if getattr(self.args, "queue_socket", None) and "claimed" in locals():
                try:
                    pending_delivery.release_claim(self.root, self.recipient,
                        claimed["delivery_id"], claim_owner=self.owner,
                        failure_reason=retry_failure)
                except pending_delivery.PendingDeliveryError as exc:
                    self._record_error(str(exc))


# Compatibility name for callers; there is only one notice courier.
HumanGateWatcher = NoticeWatcher


def negotiate_human_gate(args: argparse.Namespace) -> dict[str, Any] | None:
    try:
        result = gateway_request(args.control_socket, {
            "schema_version": 1, "op": "status",
        })
    except CompletionError:
        return None
    capability = (result.get("capabilities") or {}).get("human_gate_delivery")
    if not (
        result.get("status") == "ready" and isinstance(capability, dict)
        and capability.get("version") == 1
        and capability.get("thread_id") == args.parent_session_id
        and isinstance(capability.get("epoch"), int)
        and capability["epoch"] >= 1
    ):
        return None
    return dict(capability)


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def wait_for_session_launch_claims(
    args: argparse.Namespace,
    attempts: set[str],
) -> None:
    """Bridge registration→spawn without leaving a post-spawn sidecar gap."""

    if not args.parent_session_id:
        return
    deadline = time.monotonic() + max(0.0, args.launch_ready_timeout)
    while True:
        try:
            lines = args.jobs.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError as exc:
            raise CompletionError("registry-launch-read-failed") from exc
        latest: dict[str, tuple[str, dict[str, str]]] = {}
        for line in lines:
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            metadata = dict(
                part.split("=", 1)
                for part in fields[5].split(",")
                if "=" in part
            )
            attempt_id = metadata.get("attempt_id", "")
            if attempt_id in attempts:
                latest[attempt_id] = (fields[1], metadata)
        if set(latest) != attempts:
            raise CompletionError("expected-attempt-missing")
        pending = False
        for status, metadata in latest.values():
            if (
                metadata.get("parent_sid") != args.parent_session_id
                or metadata.get("parent_completion_delivery")
                != (CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket else MANAGED_SESSION_PARENT_DELIVERY)
                or metadata.get("attempt_schema_version") != "2"
                or metadata.get("dispatch_depth") != "1"
                or metadata.get("execution_surface") != "registered-headless"
                or metadata.get("registered_worker") != "1"
                or metadata.get("harness") not in WRAPPER_PARENT_HARNESSES
            ):
                raise CompletionError("registry-launch-contract-invalid")
            claimed = metadata.get("launch_claimed")
            if claimed == "1":
                continue
            if claimed != "0":
                raise CompletionError("registry-launch-claim-invalid")
            if status == "done" or metadata.get("launch_outcome"):
                raise CompletionError("registered-child-never-launched")
            pending = True
        if not pending:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompletionError("launch-claim-timeout")
        time.sleep(min(max(args.interval, 0.05), remaining))


def run_join(args: argparse.Namespace, attempts: set[str]) -> dict[str, Any]:
    if not args.join_command:
        # The normal path stays in this observer process so its immutable file
        # views and bounded settlement/recovery backoff survive each short
        # supervision checkpoint. Explicit external join commands retain their
        # existing argv, timeout, output-size, JSON, and error boundary below.
        import dispatch_completion_join as completion_join
        from runtime_observation import ObservationContext

        observation_context = getattr(args, "_join_observation_context", None)
        if observation_context is None:
            observation_context = ObservationContext(max_entries=8)
            args._join_observation_context = observation_context
        try:
            if args.parent_session_id:
                receipt = completion_join.join_session_batch(
                    jobs=args.jobs,
                    parent_session_id=args.parent_session_id,
                    expected_attempts=set(attempts),
                    parent_completion_delivery=(
                        CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket
                        else MANAGED_SESSION_PARENT_DELIVERY
                    ),
                    interval=args.interval,
                    timeout=args.timeout,
                    recover_receiptless=True,
                    observation_context=observation_context,
                    registry_append_only=True,
                )
            else:
                receipt = completion_join.join_batch(
                    jobs=args.jobs,
                    parent_attempt_id=args.parent_attempt_id,
                    expected_attempts=set(attempts),
                    interval=args.interval,
                    timeout=args.timeout,
                    recover_receiptless=True,
                    observation_context=observation_context,
                    registry_append_only=True,
                )
        except completion_join.JoinContractError as exc:
            raise CompletionError("join-contract-failed") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise CompletionError("join-process-failed") from exc
        if not isinstance(receipt, dict):
            raise CompletionError("join-response-shape-invalid")
        try:
            encoded_size = len(json.dumps(receipt, separators=(",", ":"),
                                          sort_keys=True).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise CompletionError("join-response-json-invalid") from exc
        if encoded_size > 65536:
            raise CompletionError("join-response-oversized")
        return receipt

    command = (
        shlex.split(args.join_command)
        if args.join_command else []
    )
    command += ["--jobs", str(args.jobs)]
    if args.parent_session_id:
        command += [
            "--parent-session-id",
            args.parent_session_id,
            "--parent-completion-delivery",
            (CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket else MANAGED_SESSION_PARENT_DELIVERY),
        ]
    else:
        command += ["--parent-attempt-id", args.parent_attempt_id]
    command += [
        "--interval", str(args.interval),
        "--timeout", str(args.timeout),
        "--recover-receiptless",
    ]
    for attempt_id in sorted(attempts):
        command += ["--attempt-id", attempt_id]
    join_env = os.environ.copy()
    join_env["HEARTING_JOIN_CONSUMER_PID"] = str(os.getpid())
    try:
        result = subprocess.run(
            command,
            env=join_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(args.timeout + 30.0, 30.0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompletionError("join-process-failed") from exc
    if len(result.stdout.encode("utf-8", "replace")) > 65536:
        raise CompletionError("join-response-oversized")
    try:
        receipt = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise CompletionError("join-response-json-invalid") from exc
    if not isinstance(receipt, dict):
        raise CompletionError("join-response-shape-invalid")
    if result.returncode == 3 and receipt.get("state") == "timeout":
        return receipt
    if result.returncode != 0:
        raise CompletionError("join-contract-failed")
    return receipt


def normalize_receipt(
    receipt: dict[str, Any],
    *,
    jobs: Path,
    parent_attempt_id: str | None,
    parent_session_id: str | None,
    delivery_parent_id: str,
    attempts: set[str],
    parent_completion_delivery: str = MANAGED_SESSION_PARENT_DELIVERY,
    accept_stage_advance: bool = False,
    stage_advance_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity_name = (
        "parent_session_id" if parent_session_id else "parent_attempt_id"
    )
    identity_value = parent_session_id or parent_attempt_id
    if (
        receipt.get("schema_version") != 2
        or receipt.get("state") != "ready"
        or receipt.get(identity_name) != identity_value
    ):
        raise CompletionError("join-receipt-identity-invalid")
    raw_children = receipt.get("children")
    if not isinstance(raw_children, list) or len(raw_children) != len(attempts):
        raise CompletionError("join-receipt-cardinality-invalid")
    by_attempt: dict[str, dict[str, Any]] = {}
    for child in raw_children:
        if not isinstance(child, dict):
            raise CompletionError("join-receipt-child-invalid")
        attempt_id = child.get("attempt_id")
        status = child.get("status")
        reason = child.get("reason")
        required_action = child.get("required_action")
        if (
            not isinstance(attempt_id, str)
            or attempt_id not in attempts
            or attempt_id in by_attempt
            or child.get("readiness") != "ready"
            or reason not in ALLOWED_REASONS
            or required_action not in REQUIRED_ACTIONS
            or (reason in {"registry-closed", "registry-closed-marker"} and status != "done")
            or (reason == "terminal-observed" and status not in OPEN_STATES)
            or (
                required_action == "complete-open"
                and status not in OPEN_STATES
            )
            or (
                required_action in {"inspect-done-failure", "advance-completed", "finish-workflow"}
                and status != "done"
            )
        ):
            raise CompletionError("join-receipt-child-contract-invalid")
        by_attempt[attempt_id] = child
    if set(by_attempt) != attempts:
        raise CompletionError("join-receipt-attempt-set-mismatch")
    try:
        if parent_session_id:
            rows = current_session_children(
                jobs,
                parent_session_id,
                attempts,
                parent_completion_delivery,
            )
        else:
            rows = current_children(jobs, parent_attempt_id or "", attempts)
    except JoinContractError as exc:
        raise CompletionError("registry-recheck-failed") from exc
    harnesses: dict[str, str] = {}
    for row in rows:
        harness = row.metadata.get("harness")
        if harness not in WRAPPER_PARENT_HARNESSES:
            raise CompletionError("registry-child-harness-invalid")
        harnesses[row.attempt_id] = harness
    try:
        timing = validate_delivery_timing(
            receipt.get("delivery_timing", delivery_timing_fields())
        )
        observed_ns = time.monotonic_ns()
        if timing["last_child_terminal_ns"] is None:
            timing = advance_delivery_timing(
                timing, "last_child_terminal_ns", at_ns=observed_ns
            )
        timing = advance_delivery_timing(
            timing, "join_completed_ns", at_ns=observed_ns
        )
    except JoinContractError as exc:
        raise CompletionError("delivery-timing-invalid") from exc
    normalized = {
        "schema_version": 2,
        "state": "ready",
        "parent_attempt_id": delivery_parent_id,
        "job_registry": str(jobs),
        "delivery_timing": timing,
        "children": [
            {
                "attempt_id": attempt_id,
                "status": str(by_attempt[attempt_id]["status"]),
                "readiness": "ready",
                "reason": str(by_attempt[attempt_id]["reason"]),
                "required_action": str(
                    by_attempt[attempt_id]["required_action"]
                ),
                "harness": harnesses[attempt_id],
            }
            for attempt_id in sorted(attempts)
        ],
    }
    try:
        normalized = wait_for_delivery_projection(
            normalized,
            jobs=jobs,
            timing=timing,
        )
    except CompletionDeferred:
        raise
    except JoinContractError as exc:
        raise CompletionError("delivery-classification-failed") from exc
    originals = set(attempts)
    if receipt.get("replacement_lineage"):
        from dispatch_replacement import adopt_receipt
        for edge in receipt["replacement_lineage"]:
            originals.discard(edge["replacement_attempt_id"])
            originals.add(edge["original_attempt_id"])
        adopt_receipt(jobs, originals, receipt)
        normalized["replacement_lineage"] = receipt["replacement_lineage"]
    if receipt.get("replacement_attention"):
        from dispatch_replacement import validate_attention
        normalized["replacement_attention"] = validate_attention(jobs, receipt["replacement_attention"], allowed_attempts=originals | set(attempts))
    if accept_stage_advance:
        normalized = receipt_with_stage_advance(
            normalized,
            stage_advance_record=stage_advance_record,
        )
    if len(canonical(normalized).encode("utf-8")) > (8192 if normalized.get("replacement_lineage") or normalized.get("replacement_attention") else MAX_RECEIPT_BYTES):
        raise CompletionError("typed-receipt-oversized")
    return normalized


def delivery_parent_id(args: argparse.Namespace) -> str:
    if args.parent_session_id:
        digest = hashlib.sha256(args.parent_session_id.encode("utf-8")).hexdigest()
        return f"parent-session-{digest}"
    return args.parent_attempt_id or ""


def gateway_request(path: Path, value: dict[str, Any], *, timeout: float = 130.0) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise CompletionError("control-socket-path-unsafe")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(max(0.05, float(timeout)))
    try:
        connection.connect(str(path))
        connection.sendall((canonical(value) + "\n").encode("utf-8"))
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_RESPONSE_BYTES:
                raise CompletionError("control-response-oversized")
    except OSError as exc:
        raise CompletionError("control-socket-unavailable") from exc
    finally:
        connection.close()
    line, separator, remainder = bytes(response).partition(b"\n")
    if not separator or remainder.strip():
        raise CompletionError("control-response-framing-invalid")
    try:
        result = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CompletionError("control-response-json-invalid") from exc
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise CompletionError("control-response-schema-invalid")
    return result


def deliver_with_retry(
    args: argparse.Namespace,
    request: dict[str, Any],
    *, stop_check=None,
) -> dict[str, Any]:
    """Retry only a provably unsent/retryable delivery.

    A `sent-ambiguous` result is never retried: doing so could duplicate a turn
    after the server accepted the first request but the acknowledgement was
    lost. The gateway's durable ledger handles accepted replays locally.
    """

    deadline = time.monotonic() + max(0.0, args.delivery_retry_window)
    last_reason = "control-socket-unavailable"
    while True:
        if stop_check is not None:
            stopped = stop_check() or ""
            if stopped:
                return {"schema_version": 1, "status": "retryable", "reason": stopped}
        try:
            result = gateway_request(args.control_socket, request,
                                     timeout=5.0 if stop_check is not None else 130.0)
        except CompletionError as exc:
            if str(exc) != "control-socket-unavailable":
                raise
            result = {
                "schema_version": 1,
                "status": "retryable",
                "reason": str(exc),
            }
        status = result.get("status")
        if status != "retryable":
            return result
        last_reason = str(result.get("reason") or "gateway-retryable")
        if stop_check is not None:
            stopped = stop_check() or ""
            if stopped:
                return {"schema_version": 1, "status": "retryable", "reason": stopped}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            args._time_budget_spent = True
            return {
                "schema_version": 1,
                "status": "retryable",
                "reason": f"delivery-retry-exhausted:{last_reason}",
            }
        time.sleep(min(max(args.delivery_retry_interval, 0.05), remaining))


def queue_target(jobs: Path, attempts: set[str], registered: str) -> str:
    """The Codex thread that receives a completion: the registered parent, or its same-seat
    successor after a /clear.  Storage, claim and ack stay under the registered parent (the
    record's own signature and digest); only the queue the item is put into changes.  Looked up
    again on every pass, so a handover that happens while waiting is seen before the send."""
    try:
        import dispatch_seat_handover as handover
        rows = handover.latest_rows(jobs)
        owners = {(handover.effective_parent(rows[a][1], jobs),
                   handover.effective_parent_harness(rows[a][1], jobs))
                  for a in attempts if a in rows}
        if len(owners) == 1:
            owner, harness = owners.pop()
            return (owner or registered) if harness == "codex" else registered
    except Exception:  # noqa: BLE001 - the registered parent remains a valid target
        pass
    return registered


def _queue_target_from_rows(jobs: Path, rows, registered: str, *, read_context=None) -> str:
    """Resolve the recipient from the already-read exact batch rows."""
    try:
        import dispatch_seat_handover as handover
        owners = {(handover.effective_parent(row.metadata, jobs, read_context=read_context),
                   handover.effective_parent_harness(row.metadata, jobs, read_context=read_context))
                  for row in rows}
        if len(owners) == 1:
            owner, harness = owners.pop()
            return (owner or registered) if harness == "codex" else registered
    except Exception:  # noqa: BLE001 - the registered parent remains a safe target
        pass
    return registered


# Read-only copy of the success follow-up text sent before the trimmed wording
# (commit aec3a362f). A body already queued or consumed with it is recognized by an
# exact full-body match only; every new send uses the current text.
_PRIOR_SUCCESS_FOLLOWUP = (
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
_CURRENT_SUCCESS_FOLLOWUP_START = "Original request/format apply."
_LEGACY_HISTORY_MISMATCH = {"queue-peer-history-mismatch", "queue-peer-history-conflict",
                            "queue-peer-pending-mismatch"}
_IMMEDIATE_RECEIPT_KINDS = {"human-gate", "supervision", "notice"}
QUEUE_BODY_LIMIT = 15 * 1024  # codex_queue_delivery refuses a message over 16 KiB


def _prior_followup_text(current: str) -> str | None:
    """The previous wording of a current success follow-up, or None when it never differed."""
    head, newline, rest = current.partition("\n")
    if not head.startswith(_CURRENT_SUCCESS_FOLLOWUP_START):
        return None
    return _PRIOR_SUCCESS_FOLLOWUP + newline + rest


def _is_completion_record(record: dict) -> bool:
    receipt = record.get("receipt")
    return (isinstance(receipt, dict) and receipt.get("kind") not in _IMMEDIATE_RECEIPT_KINDS
            and isinstance(receipt.get("children"), list))


def _batch_client_id(thread_id: str, delivery_ids) -> str:
    material = "\0".join(("codex-native-queue-batch-v1", thread_id, *sorted(delivery_ids)))
    return "delivery-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _batch_units(root: Path, thread_id: str, members: list[dict]) -> list[tuple[list[dict], str]]:
    """Render a claimed sibling batch as one shared-header body, halving it only when it
    cannot fit one queue message. The attempts counter is pinned so every sibling sidecar
    and every pass renders the same bytes for the same members."""
    import dispatch_session_sweep as sweep
    members = sorted(members, key=lambda record: record["delivery_id"])
    body = sweep.delivery_context([(root, [
        {**record, "attempts": 0, sweep.STORAGE_KEY: thread_id} for record in members])])
    if len(body.encode("utf-8")) <= QUEUE_BODY_LIMIT or len(members) == 1:
        return [(members, body)]
    middle = len(members) // 2
    return (_batch_units(root, thread_id, members[:middle])
            + _batch_units(root, thread_id, members[middle:]))


def _native_queue_delivery_once(
    args: argparse.Namespace,
    receipt: dict[str, Any],
    attempts: set[str],
    *, allow_enqueue: bool = True,
    session: codex_queue_delivery.DeliverySession | None = None,
    delivery_target: str | None = None,
    initial_rows=None,
) -> dict[str, Any]:
    """Queue one stable completion item through its durable per-child ledger."""
    path = args.queue_socket or resolve_queue_socket()
    if not path.is_absolute():
        raise CompletionError("native-queue-endpoint-path-invalid")
    rows = (initial_rows if initial_rows is not None else
            current_session_children(args.jobs, args.thread_id, attempts,
                                     CODEX_QUEUE_PARENT_DELIVERY))
    if {row.attempt_id for row in rows} != attempts:
        raise CompletionError("native-queue-ledger-attempt-set-mismatch")
    from peer_read_context import PeerReadContext
    target = (delivery_target if initial_rows is not None and delivery_target is not None else
              _queue_target_from_rows(args.jobs, rows, args.thread_id,
                                      read_context=PeerReadContext()))
    if delivery_target is not None and target != delivery_target:
        raise CompletionError("native-queue-target-changed")
    transport = {"session": session} if session is not None else {}
    thread = codex_queue_delivery._rpc(
        path, "thread/read", {"threadId": target}, timeout=5.0, **transport
    ).get("thread")
    if not isinstance(thread, dict) or thread.get("id") != target:
        raise CompletionError("native-queue-thread-identity-invalid")
    if thread.get("cwd") and not isinstance(thread.get("cwd"), str):
        raise CompletionError("native-queue-thread-cwd-invalid")
    envelope = "AGENT_HARNESS_COMPLETION_V1\n" + canonical(receipt) + "\n"
    followup = completion_followup_text(
        receipt,
        jobs=str(args.jobs),
        surface=str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh"),
    )
    context = envelope + followup
    prior_followup = _prior_followup_text(followup)
    prior_context = envelope + prior_followup if prior_followup else None
    delivery_material = "\0".join((
        "codex-native-queue-v1", args.thread_id,
        *sorted(row.metadata.get("delivery_id", "") for row in rows),
    ))
    client_message_id = "delivery-" + hashlib.sha256(
        delivery_material.encode("utf-8")
    ).hexdigest()[:32]
    # The first batch read selected the queue. Re-read immediately before the
    # claim/send boundary and reject a changed recipient or delivery identity.
    fresh_rows = current_session_children(
        args.jobs, args.thread_id, attempts, CODEX_QUEUE_PARENT_DELIVERY
    )
    fresh_context = PeerReadContext()
    fresh_target = _queue_target_from_rows(args.jobs, fresh_rows, args.thread_id,
                                           read_context=fresh_context)
    if fresh_target != target:
        raise CompletionError("native-queue-target-changed")
    if ({row.attempt_id for row in fresh_rows} != attempts
            or {row.metadata.get("delivery_id", "") for row in fresh_rows}
            != {row.metadata.get("delivery_id", "") for row in rows}):
        raise CompletionError("native-queue-ledger-attempt-set-mismatch")
    rows = fresh_rows
    root = args.jobs.resolve(strict=False).parent
    ledger_ids: list[str] = []
    for row in rows:
        delivery_id = row.metadata.get("delivery_id", "")
        if (not delivery_id.startswith("delivery-")
                or row.metadata.get("delivery_recipient_kind") != "codex-native-queue"):
            raise CompletionError("native-queue-ledger-intent-invalid")
        record = pending_delivery.read(root, args.thread_id, delivery_id)
        if (record is None or record.get("recipient_kind") != "codex-native-queue"
                or record.get("attempt_ids") != [row.attempt_id]
                or record.get("receipt_digest") != row.metadata.get("delivery_receipt_digest")
                or record.get("row_revisions", {}).get(row.attempt_id)
                != row.metadata.get("delivery_row_revision")):
            raise CompletionError("native-queue-ledger-record-mismatch")
        ledger_ids.append(delivery_id)
    records = [pending_delivery.read(root, args.thread_id, delivery_id)
               for delivery_id in ledger_ids]
    if any(record is None for record in records):
        raise CompletionError("native-queue-ledger-state-invalid")
    acked_by = f"codex-native-queue:{client_message_id}"
    if all(record.get("state") in {"acked", "rejected", "expired"} for record in records) and any(
            record.get("state") != "acked" for record in records):
        # A sibling batch already settled or retired these records (for instance an
        # exact result the recipient had already received): nothing is owed here.
        return {
            "schema_version": 1, "status": "accepted", "thread_id": args.thread_id,
            "client_user_message_id": client_message_id,
            "deduplicated_by": "pending-delivery-ledger",
        }
    acknowledged = [record for record in records if record.get("state") == "acked"]
    if acknowledged and (len(acknowledged) == len(records) or all(
            record.get("acked_by") == acked_by for record in acknowledged)):
        # Ack writes are per-record, so a crash can leave one exact batch
        # partially acknowledged after its consumed turn was already proven.
        # The same stable client id is that proof's batch identity: finish
        # closing its remaining records instead of stranding or resending it.
        for record in records:
            if record.get("state") == "acked":
                continue
            if record.get("state") not in {"pending", "claimed", "sent-ambiguous"}:
                raise CompletionError("native-queue-ledger-state-invalid")
            pending_delivery.ack(
                root, args.thread_id, record["delivery_id"], acked_by=acked_by,
                expected_states=("pending", "claimed", "sent-ambiguous"),
            )
        return {
            "schema_version": 1, "status": "accepted", "thread_id": args.thread_id,
            "client_user_message_id": client_message_id,
            "deduplicated_by": "pending-delivery-ledger",
        }
    from dispatch_recipient_receipt import (
        codex_consumed_evidence, codex_queue_evidence, receipt_marker,
        settle_native_receipt,
    )
    markers = "\n" + "\n".join(receipt_marker(record, args.thread_id) for record in records)
    context += markers
    if prior_context is not None:
        prior_context += markers
    # Recover an exact native receipt before taking another queue claim. A
    # successful queue RPC is insufficient; the strict history translator
    # requires the stable client id and this exact marker-bearing body.
    # The registered recipient owns the record; after an approved handover,
    # only the exact current successor may settle it. A predecessor's native
    # history is never promoted into the successor's receipt.
    native_threads = (target,) if target != args.thread_id else (args.thread_id,)
    previous_send_observed = any(
        record.get("state") in {"claimed", "sent-ambiguous"}
        or int(record.get("attempts") or 0) > 0
        for record in records)
    for native_thread in (native_threads if previous_send_observed else ()):
        evidence = codex_queue_evidence(
            endpoint=path, session_id=native_thread, delivery_id=client_message_id,
            expected_message=context, timeout=5.0, session=session)
        if (evidence is None or not evidence.get("bindings")) and prior_context is not None:
            # A body queued before the follow-up was trimmed (exact full-body match).
            evidence = codex_queue_evidence(
                endpoint=path, session_id=native_thread, delivery_id=client_message_id,
                expected_message=prior_context, timeout=5.0, session=session) or evidence
        if evidence is None:
            continue
        bindings = set(evidence.get("bindings") or ())
        expected_ids = {record["delivery_id"] for record in records}
        found_ids = {delivery_id for delivery_id, _digest in bindings}
        if not expected_ids & found_ids:
            continue
        receiving_turn = evidence.get("receiving_turn")
        if (not isinstance(receiving_turn, dict)
                or not isinstance(receiving_turn.get("turn_id"), str)
                or not isinstance(receiving_turn.get("status"), str)):
            return {"schema_version": 1, "status": "sent-ambiguous",
                    "reason": "native-receiving-turn-unknown", "submitted": True,
                    "thread_id": args.thread_id, "client_user_message_id": client_message_id}
        from peer_obligations import retain_registered_completion
        retained = all(retain_registered_completion(
            args.jobs, row.attempt_id, native_thread, "codex-native-queue",
            delivery_record={**record, "_storage_key": args.thread_id},
            receiving_turn=receiving_turn)
            for row, record in zip(rows, records))
        if not retained:
            return {"schema_version": 1, "status": "sent-ambiguous",
                    "reason": "native-receipt-retention-pending", "submitted": True,
                    "thread_id": args.thread_id, "client_user_message_id": client_message_id}
        outcomes = [settle_native_receipt(
            root, args.thread_id, "codex", native_thread, record, evidence,
            acked_by=acked_by) for record in records]
        if all(outcome["received"] for outcome in outcomes):
            return {"schema_version": 1, "status": "accepted",
                    "reason": ("native-receipt-ack-pending"
                               if not all(outcome["settled"] for outcome in outcomes)
                               else "native-receipt-settled"),
                    "thread_id": args.thread_id, "client_user_message_id": client_message_id}
        # The batch body and native client id are exact. A partial binding is
        # malformed evidence, so preserve recovery without enqueueing a second
        # copy of a message that already reached this native turn.
        return {"schema_version": 1, "status": "sent-ambiguous",
                "reason": "native-batch-receipt-partial", "submitted": True,
                "thread_id": args.thread_id, "client_user_message_id": client_message_id}
    records = [record for record in records
               if record.get("state") not in {"acked", "rejected", "expired"}]
    if any(record.get("state") not in {"pending", "claimed", "sent-ambiguous"}
           for record in records):
        raise CompletionError("native-queue-ledger-state-invalid")
    claim_owner = f"codex-native-queue:{os.getpid()}:{client_message_id}"
    claimed: list[str] = []
    retry_base = min(max(args.delivery_retry_interval, 0.05), 15.0)
    retry_failure = "native-queue-pass-incomplete"

    def consumed_by(thread_id: str, bodies: list[str]):
        """The consumed turn for the first body that exactly matches native history."""
        first_error = None
        for body in bodies:
            try:
                turn = codex_queue_delivery._known_consumed(
                    path, thread_id, unit_client_id, timeout=5.0, strict_history=True,
                    expected_message=body, **transport)
            except codex_queue_delivery.QueueDeliveryError as exc:
                first_error = first_error or exc
                continue
            if turn is not None:
                return turn, body
        if first_error is not None:
            raise first_error
        return None, bodies[0]

    def send_unit(bodies: list[str]):
        try:
            result = codex_queue_delivery.send_at_least_once(
                path, thread_id=target, client_message_id=unit_client_id,
                message=bodies[0], timeout=5.0, require_message_match=True, **transport)
            return result, bodies[0]
        except codex_queue_delivery.QueueDeliveryError as exc:
            if len(bodies) == 1 or str(exc) not in _LEGACY_HISTORY_MISMATCH:
                raise
            # The same id may already hold the previous exact wording; never resend it.
            for earlier in bodies[1:]:
                try:
                    turn = codex_queue_delivery._known_consumed(
                        path, target, unit_client_id, timeout=5.0, strict_history=True,
                        expected_message=earlier, **transport)
                except codex_queue_delivery.QueueDeliveryError:
                    turn = None
                if turn is not None:
                    return {"status": "consumed", "turn_id": turn.get("id"),
                            "turn_status": turn.get("status")}, earlier
                queued = [item for item in codex_queue_delivery.list_queue(
                    path, target, timeout=5.0, **transport)
                    if isinstance(item, dict)
                    and item.get("clientUserMessageId") == unit_client_id]
                if len(queued) == 1 and codex_queue_delivery._peer_text_input(
                        queued[0].get("input"), earlier):
                    item_id = queued[0].get("id")
                    return {"status": "queued", "already_pending": True,
                            "queued_submission_id": item_id if isinstance(item_id, str) else None
                            }, earlier
            raise

    def deliver_unit(unit_records: list[dict], bodies: list[str]) -> dict[str, Any]:
        nonlocal retry_failure
        unit_ids = [record["delivery_id"] for record in unit_records]
        unit_acked_by = f"codex-native-queue:{unit_client_id}"
        body = bodies[0]
        turn_thread = target
        send_observed = False
        consumed = None
        if target != args.thread_id:
            consumed, body = consumed_by(args.thread_id, bodies)
        if consumed is not None:
            # The item already reached the cleared window's thread before the handover.
            result = {"status": "consumed", "turn_id": consumed.get("id"),
                      "turn_status": consumed.get("status")}
            turn_thread = args.thread_id
        elif allow_enqueue:
            result, body = send_unit(bodies)
            send_observed = (result.get("status") == "queued"
                             and not result.get("already_pending"))
        else:
            consumed, body = consumed_by(target, bodies)
            if consumed is not None:
                result = {"status": "consumed", "turn_id": consumed.get("id"),
                          "turn_status": consumed.get("status")}
            else:
                # An item still waiting in the old thread's queue cannot be moved (the local queue API
                # has no move/remove): it is never restarted there; the successor gets the same record.
                result = codex_queue_delivery.poll_owned_item(
                    path, thread_id=target, client_message_id=unit_client_id, timeout=5.0,
                    **transport)
                result["status"] = "queued"

        if result["status"] == "consumed":
            turn_id = result.get("turn_id")
            turn_status = result.get("turn_status")
            retained = bool(isinstance(turn_id, str) and turn_id and isinstance(turn_status, str))
            if retained:
                from peer_obligations import retain_registered_completion
                receiving_turn = {"thread_id": turn_thread, "turn_id": turn_id,
                                  "status": turn_status}
                for record in unit_records:
                    if not retain_registered_completion(
                            args.jobs, record["attempt_ids"][0], turn_thread, "codex-native-queue",
                            delivery_record={**record, "_storage_key": args.thread_id},
                            receiving_turn=receiving_turn):
                        retained = False
                        break
            if not retained:
                for delivery_id in unit_ids:
                    pending_delivery.mark_sent_ambiguous(
                        root, args.thread_id, delivery_id, claim_owner=claim_owner)
                return {"schema_version": 1, "status": "sent-ambiguous",
                        "reason": "native-receiving-turn-retain-unavailable",
                        "submitted": True, "thread_id": args.thread_id,
                        "client_user_message_id": unit_client_id}
            evidence = codex_consumed_evidence(
                session_id=turn_thread, client_message_id=unit_client_id,
                confirmed_client_message_id=unit_client_id, expected_message=body)
            outcomes = []
            for delivery_id in unit_ids:
                exact = pending_delivery.read(root, args.thread_id, delivery_id)
                if exact is None:
                    outcomes.append({"received": False, "settled": False})
                    continue
                outcomes.append(settle_native_receipt(
                    root, args.thread_id, "codex", turn_thread, exact, evidence,
                    acked_by=unit_acked_by))
            if all(outcome["received"] for outcome in outcomes):
                return {"schema_version": 1, "status": "accepted",
                        "reason": ("native-receipt-ack-pending"
                                   if not all(outcome["settled"] for outcome in outcomes)
                                   else "native-receipt-settled"),
                        "thread_id": args.thread_id, "client_user_message_id": unit_client_id}
            for delivery_id in unit_ids:
                pending_delivery.mark_sent_ambiguous(
                    root, args.thread_id, delivery_id, claim_owner=claim_owner)
            return {"schema_version": 1, "status": "sent-ambiguous",
                    "reason": "native-receipt-binding-unavailable", "submitted": True,
                    "thread_id": args.thread_id, "client_user_message_id": unit_client_id}
        if send_observed:
            for delivery_id in unit_ids:
                try:
                    pending_delivery.record_send(
                        root, args.thread_id, delivery_id,
                        source="codex-native-queue-accepted", event_id=unit_client_id)
                except (pending_delivery.PendingDeliveryError, OSError):
                    pass  # Timestamp evidence is advisory and never changes delivery acceptance.
        for delivery_id in unit_ids:
            pending_delivery.mark_sent_ambiguous(
                root, args.thread_id, delivery_id, claim_owner=claim_owner)
        retry_failure = str(result.get("reason") or "native-queue-consumption-pending")
        return {"schema_version": 1, "status": "sent-ambiguous",
                "reason": "native-queue-consumption-pending", "submitted": True, "thread_id": args.thread_id,
                "client_user_message_id": unit_client_id,
                "queued_submission_id": result.get("queued_submission_id")}

    try:
        # Claim each owed record. A completion record goes through the ready-batch
        # claim: a held sibling batch waits on the retry loop, a lost claim means
        # another sidecar owns the same batch and this pass stays quiet.
        group: dict[str, dict] = {}
        lost = False
        for record in records:
            delivery_id = record["delivery_id"]
            if delivery_id in group:
                continue
            try:
                if record["state"] in {"claimed", "sent-ambiguous"}:
                    pending_delivery.reclaim(root, args.thread_id, delivery_id, now_ns=time.monotonic_ns())
                if _is_completion_record(record):
                    members = pending_delivery.claim_ready_batch(
                        root, args.thread_id, delivery_id, claim_owner=claim_owner,
                        lease_seconds=60.0, jobs=args.jobs, retry_context=target)
                else:
                    members = [pending_delivery.claim(
                        root, args.thread_id, delivery_id,
                        claim_owner=claim_owner, lease_seconds=60.0,
                        jobs=args.jobs, retry_context=target,
                        retry_base_delay_seconds=retry_base)]
            except pending_delivery.PendingDeliveryError as exc:
                if (exc.reason != "pending-delivery-claim-refused"
                        or exc.detail == "batch-hold"):
                    raise
                lost = True
                continue
            if len(members) == 1:
                members = [record]  # A singleton claim is the seed's own validated record.
            for member in members:
                group[member["delivery_id"]] = member
                claimed.append(member["delivery_id"])
        kept: dict[str, dict] = {}
        from dispatch_notice_state import keep_claim
        for delivery_id, member in group.items():
            if member.get("recipient_kind") != "codex-native-queue":
                raise CompletionError("native-queue-batch-recipient-mismatch")
            if (not _is_completion_record(member)
                    or keep_claim(root, args.thread_id, delivery_id, member, claim_owner,
                                  jobs=args.jobs)):
                kept[delivery_id] = member
                continue
            fresh = pending_delivery.read(root, args.thread_id, delivery_id)
            if (fresh and fresh.get("state") == "claimed"
                    and fresh.get("claim_owner") == claim_owner):
                kept[delivery_id] = member  # Unreadable evidence never withholds delivery.
            else:
                claimed.remove(delivery_id)  # Retired as already received/obsolete, or handed back.
        if not kept:
            settled = all((pending_delivery.read(root, args.thread_id, record["delivery_id"]) or {}
                           ).get("state") in {"acked", "rejected", "expired"}
                          for record in records)
            retry_failure = "native-queue-claim-not-held"
            return {"schema_version": 1, "status": "accepted" if settled else "retryable",
                    "reason": ("native-queue-nothing-current" if settled else
                               "native-queue-batch-owned-elsewhere" if lost else
                               "native-queue-completion-deferred"),
                    "thread_id": args.thread_id}
        own_ids = [record["delivery_id"] for record in records]
        if set(kept) == set(own_ids):
            units = [([kept[i] for i in own_ids],
                      [context] + ([prior_context] if prior_context else []),
                      client_message_id)]
        else:
            units = [(unit_records, [body], _batch_client_id(
                args.thread_id, [record["delivery_id"] for record in unit_records]))
                for unit_records, body in _batch_units(root, args.thread_id, list(kept.values()))]
        result = {"schema_version": 1, "status": "accepted", "thread_id": args.thread_id}
        for unit_records, bodies, unit_client_id in units:
            result = deliver_unit(unit_records, bodies)
            if result.get("status") != "accepted":
                return result
        return result
    except Exception as exc:
        retry_failure = str(exc) or type(exc).__name__
        raise
    finally:
        # Each network pass owns a bounded claim, never the whole watch window.
        # A rejected send and partial claim cannot hide receipts on the next
        # prompt; an ack or another owner's claim is preserved by the CAS.
        for delivery_id in claimed:
            pending_delivery.release_claim(
                root, args.thread_id, delivery_id, claim_owner=claim_owner,
                failure_reason=retry_failure, retry_base_delay_seconds=retry_base)


def _courier_retry_wait(exc) -> float | None:
    """Seconds until the ledger's courier backoff admits the next claim."""
    if (not isinstance(exc, pending_delivery.PendingDeliveryError)
            or exc.reason != "pending-delivery-retry-deferred"):
        return None
    try:
        return max(0.0, int(exc.detail.partition("remaining_ns=")[2]) / 1e9)
    except ValueError:
        return None


def deliver_to_native_queue(args, receipt, attempts, *, stop_check=None):
    from peer_read_context import PeerReadContext
    deadline = time.monotonic() + max(0.0, args.timeout)
    result = {"schema_version": 1, "status": "retryable", "reason": "native-queue-timeout"}
    submitted = False
    base_delay = min(max(args.delivery_retry_interval, 0.05), 15.0)
    delay = base_delay
    last_observation = None
    while time.monotonic() < deadline:
        if stop_check is not None:
            stopped = stop_check() or ""
            if stopped:
                return {"schema_version": 1, "status": "retryable", "reason": stopped}
        session = None
        courier_wait = None
        try:
            path = args.queue_socket or resolve_queue_socket()
            rows = current_session_children(args.jobs, args.thread_id, attempts,
                                            CODEX_QUEUE_PARENT_DELIVERY)
            target = _queue_target_from_rows(args.jobs, rows, args.thread_id,
                                             read_context=PeerReadContext())
            session = codex_queue_delivery.DeliverySession(path, timeout=5.0)
            result = _native_queue_delivery_once(
                args, receipt, attempts, allow_enqueue=not submitted,
                session=session, delivery_target=target, initial_rows=rows,
            )
            # Positive queue acceptance is not ambiguous. Keep observing until
            # consumed, but do not repeatedly enqueue during a history outage.
            # A crashed courier may retry with the same id (at-least-once).
            submitted = submitted or result.get("submitted", False)
            if result.get("status") == "accepted":
                return result
        except (codex_queue_delivery.QueueDeliveryError, pending_delivery.PendingDeliveryError,
                CompletionError) as exc:
            result = {"schema_version": 1, "status": "retryable", "reason": str(exc),
                      "thread_id": args.thread_id}
            courier_wait = _courier_retry_wait(exc)
        finally:
            if session is not None:
                session.close()
        if stop_check is not None:
            stopped = stop_check() or ""
            if stopped:
                return {"schema_version": 1, "status": "retryable", "reason": stopped}
        remaining = deadline - time.monotonic()
        if remaining > 0 and courier_wait is not None:
            # The ledger's own courier backoff names the next admissible
            # claim. Waking before it only repeats this pass's registry reads,
            # and its changing remaining time is not a new observation.
            time.sleep(min(max(courier_wait, delay), remaining))
            continue
        if remaining > 0:
            observation = (result.get("status"), result.get("reason"),
                           result.get("client_user_message_id"), bool(submitted))
            if (observation != last_observation
                    or result.get("reason") == "native-queue-target-changed"):
                delay = base_delay
            else:
                delay = min(15.0, delay * 1.7)
            last_observation = observation
            time.sleep(min(delay, remaining))
    args._time_budget_spent = True
    return result


def _record_parent_session_ended(args, attempts, duty_id, outcome=None):
    from dispatch_supervision import materialize
    if _observer_ownership_lost(args):
        raise ObserverOwnershipLost("completion-observer-ownership-lost")
    try:
        materialize(args.jobs, attempts, reason=PARENT_SESSION_ENDED)
    except (OSError, ValueError, pending_delivery.PendingDeliveryError):
        pass
    if not duty_id:
        return
    current = batch_obligations.read(args.jobs, duty_id) or {}
    changes = {
        "state": "unknown", "reason": PARENT_SESSION_ENDED,
        "delivery": "pending", "effective_attempts": sorted(attempts),
        "parent_lifetime": getattr(args, "parent_lifetime", None),
    }
    if current.get("outcome") is None and outcome is not None:
        changes["outcome"] = outcome
    _update_duty(args, duty_id, **changes)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--control-socket",
        type=Path,
        default=os.environ.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET"),
    )
    value.add_argument("--queue-socket", type=Path)
    value.add_argument(
        "--jobs",
        type=Path,
        default=os.environ.get("AGENT_DISPATCH_JOBS"),
    )
    value.add_argument(
        "--parent-attempt-id",
        default=None,
    )
    value.add_argument("--parent-session-id")
    value.add_argument("--sealed-batch-id")
    value.add_argument("--thread-id")
    value.add_argument("--gateway-epoch", type=int)
    value.add_argument("--binding-generation", type=int)
    value.add_argument("--attempt-id", action="append", default=[])
    value.add_argument("--interval", type=float, default=2.0)
    value.add_argument("--timeout", type=float, default=3600.0)
    value.add_argument("--launch-ready-timeout", type=float, default=60.0)
    value.add_argument("--delivery-retry-window", type=float, default=300.0)
    value.add_argument("--delivery-retry-interval", type=float, default=2.0)
    value.add_argument("--join-command")
    value.add_argument("--resume-obligation", help=argparse.SUPPRESS)
    value.add_argument("--obligation-lock-fd", type=int, help=argparse.SUPPRESS)
    value.add_argument("--ensure-obligations", action="store_true", help=argparse.SUPPRESS)
    value.add_argument("--service-current", help=argparse.SUPPRESS)
    return value


def _control_socket_connection_failed(path: Path | None) -> bool:
    """True only for a hard connection failure -- the socket file is simply
    gone (ENOENT) or nothing is listening (ECONNREFUSED). Any other outcome
    (connected but the gateway answered "not ready", a slow accept, ...) is
    an ordinary transient state a plain retry may still resolve, not proof
    the receiver itself is gone."""
    if path is None:
        return True
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(str(path))
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.ECONNREFUSED)
    finally:
        connection.close()
    return False


def _any_attempt_open(jobs: Path, attempts: set[str]) -> bool:
    """Read-only registry check: is any monitored attempt still open/running?

    An unreadable registry is treated as "still open" (conservative -- never
    a reason to stop watching on missing evidence, LOOP §4).
    """
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    latest: dict[str, str] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        attempt_id = metadata.get("attempt_id", "")
        if attempt_id in attempts:
            latest[attempt_id] = fields[1]
    return any(latest.get(attempt_id, "open") in OPEN_STATES for attempt_id in attempts)


def _receiver_unavailable_reason(args: argparse.Namespace, attempts: set[str]) -> str:
    """The stop_check probe for a `wait_for_batch` watch (item 8, defect (4)).

    Only a proven connection failure AND every monitored attempt already
    terminal justifies giving up -- a live attempt still deserves the
    delivery once the gateway comes back, and a connectable-but-not-ready
    gateway is not "unavailable" (the ordinary join-deadline retry already
    covers that).
    """
    if not _control_socket_connection_failed(args.queue_socket or args.control_socket):
        return ""
    if _any_attempt_open(args.jobs, attempts):
        return ""
    return "receiver-unavailable"


def replacement_authority(args: argparse.Namespace, jobs: Path, aid: str, metadata: dict) -> bool:
    """Only the still-current managed TUI binding can replace its child."""
    if (Path(jobs).resolve() != args.jobs.resolve()
            or metadata.get("dispatch_depth") != "1"
            or metadata.get("parent_sid") != args.parent_session_id
            or metadata.get("parent_completion_delivery") != MANAGED_SESSION_PARENT_DELIVERY):
        return False
    try:
        status = gateway_request(args.control_socket, {"schema_version": 1, "op": "status"})
    except CompletionError:
        return False
    return bool(status.get("status") == "ready" and status.get("tui_connected") is True
        and status.get("thread_id") == args.parent_session_id
        and type(args.gateway_epoch) is int and status.get("epoch") == args.gateway_epoch
        and type(args.binding_generation) is int and status.get("binding_generation") == args.binding_generation)


def _await_notice_ack(
    root: Path, recipient_key: str, delivery_records: list[dict[str, Any]], *, timeout: float
) -> None:
    """Give the still-running notice courier (`watcher`) a bounded chance to
    claim and ack the watch-deadline notice `wait_for_batch` just recorded,
    before this process exits without ever calling `deliver`."""
    delivery_ids = [
        record.get("delivery_id") for record in delivery_records
        if isinstance(record, dict) and record.get("delivery_id")
    ]
    if not recipient_key or not delivery_ids:
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            records = [pending_delivery.read(root, recipient_key, did) for did in delivery_ids]
        except (OSError, ValueError):
            records = []
        if records and all(
            record is not None and record.get("state") in pending_delivery.TERMINAL_STATES
            for record in records
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _execute_once(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.control_socket is None and args.queue_socket is None:
        raise CompletionError("control-socket-missing")
    if (
        args.jobs is None
        or not args.jobs.is_absolute()
        or args.jobs.is_symlink()
        or not args.jobs.is_file()
    ):
        raise CompletionError("jobs-path-invalid")
    if not args.parent_attempt_id and not args.parent_session_id:
        args.parent_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if bool(args.parent_attempt_id) == bool(args.parent_session_id):
        raise CompletionError("parent-identity-invalid")
    if args.parent_session_id and args.thread_id != args.parent_session_id:
        raise CompletionError("parent-thread-identity-mismatch")
    attempts = set(args.attempt_id)
    if (
        not attempts
        or len(attempts) != len(args.attempt_id)
        or len(attempts) > 4
    ):
        raise CompletionError("attempt-set-invalid")
    duty_id = getattr(args, "_obligation_id", None)
    obligation = batch_obligations.read(args.jobs, duty_id) if duty_id else None
    normalized = obligation.get("outcome") if obligation else None
    if duty_id and normalized is None and args.parent_session_id:
        # A native prompt may have rendered and exactly ACKed the immutable
        # receipt before this observer stored its outcome. Recover that receipt
        # before entering cleanup/join; the receiver's registered-turn duty
        # remains responsible for review and follow-through.
        from dispatch_completion_join import recover_acknowledged_batch_outcome
        effective = set((obligation or {}).get("effective_attempts") or attempts)
        recovered = recover_acknowledged_batch_outcome(
            args.jobs, effective, recipient_sid=args.parent_session_id,
            parent_attempt_id=delivery_parent_id(args),
            parent_completion_delivery=(CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket
                                        else MANAGED_SESSION_PARENT_DELIVERY),
        )
        if recovered is not None:
            if _observer_ownership_lost(args):
                raise ObserverOwnershipLost("completion-observer-ownership-lost")
            current = batch_obligations.read(args.jobs, duty_id) or {}
            if current.get("outcome") is None:
                _update_duty(args, duty_id, state="delivery-pending",
                    reason="exact-receipt-ack-recovered", delivery="accepted",
                    outcome=recovered, effective_attempts=sorted(effective))
            obligation = batch_obligations.read(args.jobs, duty_id) or obligation
            normalized = obligation.get("outcome") if obligation else recovered
    if duty_id and normalized is not None and args.parent_session_id:
        from dispatch_completion_join import cached_batch_delivery_state
        effective = set((obligation or {}).get("effective_attempts") or attempts)
        cached_state = cached_batch_delivery_state(
            args.jobs, effective, recipient_sid=args.parent_session_id,
            parent_attempt_id=delivery_parent_id(args),
            parent_completion_delivery=(CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket
                                        else MANAGED_SESSION_PARENT_DELIVERY),
            cached=normalized,
        )
        if cached_state == "resolved":
            # The per-child ledger already closed this delivery; nothing is
            # left for this observer to send or to keep waiting on.
            _update_duty(args, duty_id, state="complete", reason="delivery-resolved",
                         delivery="resolved", effective_attempts=sorted(effective))
            return {"schema_version": 1, "status": "accepted",
                    "reason": "delivery-resolved"}, 0
        if cached_state != "current":
            reason = {"unmaterialized": "cached-outcome-delivery-unmaterialized",
                      "partial": "cached-outcome-delivery-partial"}.get(
                          cached_state, "cached-outcome-identity-unavailable")
            _update_duty(args, duty_id, state="unknown", reason=reason,
                         delivery="pending", effective_attempts=sorted(effective))
            return {"schema_version": 1, "status": "retryable", "reason": reason}, 75
    if normalized is None:
        wait_for_session_launch_claims(args, attempts)
        watcher: HumanGateWatcher | None = None
        if args.parent_session_id:
            capability = (
                {"epoch": 1} if args.queue_socket else negotiate_human_gate(args)
            )
            watcher = HumanGateWatcher(args, attempts, capability)
            watcher.start()
        try:
            from dispatch_supervision import wait_for_batch
            monitored = set(attempts)

            def replacement_checkpoint(selected):
                from dispatch_replacement import advance_batch
                nonlocal monitored
                if _observer_ownership_lost(args):
                    raise ObserverOwnershipLost("completion-observer-ownership-lost")
                advanced = advance_batch(args.jobs, selected,
                    authority_check=(lambda jobs, aid, metadata: replacement_authority(args, jobs, aid, metadata))
                        if args.parent_session_id else None)
                monitored = set(advanced[0])
                if watcher is not None:
                    watcher.attempts = set(monitored)
                if duty_id:
                    _update_duty(
                        args, duty_id, effective_attempts=sorted(monitored),
                        replacement_lineage=advanced[1], reason="replacement-checkpoint",
                    )
                return advanced

            resume = bool(getattr(args, "_resume_execution", False))
            deadline = time.monotonic() + (max(30.0, args.timeout) if resume else max(0.0, args.timeout))

            previous_parent_lifetime = getattr(args, "parent_lifetime", None)

            def supervision_stop_check():
                nonlocal previous_parent_lifetime
                if _observer_ownership_lost(args):
                    raise ObserverOwnershipLost("completion-observer-ownership-lost")
                reason = parent_session_stop_reason(args, monitored)
                if reason:
                    return reason
                current_lifetime = getattr(args, "parent_lifetime", None)
                if duty_id and current_lifetime != previous_parent_lifetime:
                    _update_duty(args, duty_id, parent_lifetime=current_lifetime)
                    previous_parent_lifetime = current_lifetime
                if resume:
                    return ""
                return _receiver_unavailable_reason(args, monitored)

            receipt = wait_for_batch(
                join=lambda selected: _join_checkpoint(args, selected),
                attempts=attempts, jobs=args.jobs,
                parent_attempt_id=delivery_parent_id(args),
                deadline=deadline,
                stop_check=supervision_stop_check,
                replacement_checkpoint=replacement_checkpoint,
            )
            from dispatch_replacement import adopt_receipt
            attempts, _replaced_attempts = adopt_receipt(args.jobs, attempts, receipt)
            if receipt.get("state") == "watch-expired":
                reason = receipt.get("reason") or "watch-deadline"
                args._time_budget_spent = reason == "watch-deadline"
                if reason == "watch-deadline" and args.parent_session_id:
                    _await_notice_ack(
                        args.jobs.resolve(strict=False).parent, args.parent_session_id,
                        receipt.get("delivery_records") or [], timeout=130.0,
                    )
                if duty_id:
                    _update_duty(
                        args, duty_id,
                        state="unknown", reason=reason,
                        effective_attempts=sorted(monitored),
                        replacement_lineage=receipt.get("replacement_lineage") or [],
                    )
                return {"schema_version": 1, "status": "retryable", "reason": reason}, 75
        finally:
            if watcher is not None:
                watcher.close()
        delivery_parent = delivery_parent_id(args)
        normalization_error = []
        def project(selected):
            try:
                projected = normalize_receipt(
                    receipt,
                    jobs=args.jobs,
                    parent_attempt_id=args.parent_attempt_id,
                    parent_session_id=args.parent_session_id,
                    delivery_parent_id=delivery_parent,
                    attempts=attempts,
                    parent_completion_delivery=(
                        CODEX_QUEUE_PARENT_DELIVERY if args.queue_socket
                        else MANAGED_SESSION_PARENT_DELIVERY
                    ),
                )
            except CompletionDeferred:
                raise
            except CompletionError as exc:
                normalization_error[:] = [exc]
                return receipt
            return projected
        def projection_stop_check():
            if _observer_ownership_lost(args):
                raise ObserverOwnershipLost("completion-observer-ownership-lost")
            reason = parent_session_stop_reason(args, attempts)
            if reason:
                return reason
            return "" if resume else _receiver_unavailable_reason(args, attempts)

        normalized = wait_for_batch(join=project, attempts=attempts, jobs=args.jobs,
            parent_attempt_id=delivery_parent, deadline=deadline,
            stop_check=projection_stop_check)
        if normalization_error:
            raise normalization_error[0]
        if normalized.get("state") == "watch-expired":
            reason = normalized.get("reason") or "watch-deadline"
            if reason == "watch-deadline":
                args._time_budget_spent = True
                reason = "termination-pending"
            if duty_id:
                _update_duty(args, duty_id, state="unknown", reason=reason,
                    outcome=None, delivery="pending", effective_attempts=sorted(attempts))
            return {"schema_version": 1, "status": "retryable", "reason": reason}, 75
        if duty_id:
            current = batch_obligations.read(args.jobs, duty_id) or {}
            lineage = normalized.get("replacement_lineage") or current.get("replacement_lineage") or []
            _update_duty(
                args, duty_id, state="delivery-pending", reason="result-settled",
                outcome=normalized, effective_attempts=sorted(attempts),
                replacement_lineage=lineage,
            )
    else:
        attempts = set(obligation.get("effective_attempts") or attempts)
        delivery_parent = delivery_parent_id(args)
    parent_reason = parent_session_stop_reason(args, attempts)
    if _observer_ownership_lost(args):
        raise ObserverOwnershipLost("completion-observer-ownership-lost")
    if parent_reason:
        _record_parent_session_ended(args, attempts, duty_id, normalized)
        return {"schema_version": 1, "status": "retryable", "reason": parent_reason}, 75

    def delivery_stop_check():
        if _observer_ownership_lost(args):
            raise ObserverOwnershipLost("completion-observer-ownership-lost")
        reason = parent_session_stop_reason(args, attempts)
        if reason:
            _record_parent_session_ended(args, attempts, duty_id, normalized)
        return reason

    if args.queue_socket:
        if delivery_stop_check():
            return {"schema_version": 1, "status": "retryable",
                    "reason": PARENT_SESSION_ENDED}, 75
        result = deliver_to_native_queue(args, normalized, attempts,
                                         stop_check=delivery_stop_check)
        status = result.get("status")
        return result, {
            "accepted": 0,
            "retryable": 75,
            "sent-ambiguous": 74,
            "rejected": 65,
        }.get(status, 65)
    request = {
        "schema_version": 1,
        "op": "deliver",
        "thread_id": args.thread_id or "",
        "parent_attempt_id": delivery_parent,
        "sealed_batch_id": args.sealed_batch_id,
        "receipt": normalized,
    }
    if args.gateway_epoch is not None:
        request["gateway_epoch"] = args.gateway_epoch
    if args.binding_generation is not None:
        request["binding_generation"] = args.binding_generation
    result = deliver_with_retry(args, request, stop_check=delivery_stop_check)
    status = result.get("status")
    return result, {
        "accepted": 0,
        "retryable": 75,
        "sent-ambiguous": 74,
        "rejected": 65,
    }.get(status, 65)


def _restore_obligation_arguments(record: dict) -> argparse.Namespace:
    values = dict(record.get("arguments") or {})
    for name in ("jobs", "control_socket", "queue_socket"):
        if values.get(name) is not None:
            values[name] = Path(values[name])
    return argparse.Namespace(**values)


def _validate_obligation_identity(args: argparse.Namespace, identity: dict) -> None:
    if args.jobs is None:
        raise CompletionError("jobs-path-invalid")
    args.jobs = Path(args.jobs)
    if args.control_socket is not None:
        args.control_socket = Path(args.control_socket)
    if args.queue_socket is not None:
        args.queue_socket = Path(args.queue_socket)
    if not args.parent_attempt_id and not args.parent_session_id:
        args.parent_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
    if bool(args.parent_attempt_id) == bool(args.parent_session_id):
        raise CompletionError("parent-identity-invalid")
    if args.parent_session_id and args.thread_id != args.parent_session_id:
        raise CompletionError("parent-thread-identity-mismatch")
    if not args.sealed_batch_id:
        raise CompletionError("sealed-batch-id-missing")
    attempts = list(args.attempt_id or [])
    if not attempts or len(set(attempts)) != len(attempts) or len(attempts) > 4:
        raise CompletionError("attempt-set-invalid")
    expected = {
        "jobs": str(args.jobs.resolve(strict=True)),
        "sealed_batch_id": args.sealed_batch_id,
        "parent_attempt_id": args.parent_attempt_id or "",
        "parent_session_id": args.parent_session_id or "",
        "attempt_ids": sorted(attempts),
        "delivery_parent_id": (args.parent_session_id or args.parent_attempt_id or ""),
    }
    if expected != identity:
        raise CompletionError("batch-obligation-identity-mismatch")


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    service_current = getattr(args, "service_current", None)
    if getattr(args, "ensure_obligations", False):
        launched = batch_obligations.ensure_observers(args.jobs, service_current=service_current)
        return {"schema_version": 1, "status": "accepted", "resumed": launched}, 0

    inherited_fd = getattr(args, "obligation_lock_fd", None)
    expected_observer = None
    if inherited_fd is not None and getattr(args, "resume_obligation", None):
        expected_observer = {"pid": os.getpid(), "pid_start": batch_obligations._pid_start(os.getpid())}
    if getattr(args, "resume_obligation", None):
        duty_id = args.resume_obligation
        record = batch_obligations.read(args.jobs, duty_id)
        if record is None:
            raise CompletionError("batch-obligation-missing")
        restored = _restore_obligation_arguments(record)
        restored._resume_execution = True
        restored._obligation_id = duty_id
        restored.obligation_lock_fd = inherited_fd
        args = restored
    else:
        if args.jobs is None or not Path(args.jobs).is_absolute() or Path(args.jobs).is_symlink() \
                or not Path(args.jobs).is_file():
            raise CompletionError("jobs-path-invalid")
        args.jobs = Path(args.jobs)
        if args.control_socket is not None:
            args.control_socket = Path(args.control_socket)
        if args.queue_socket is not None:
            args.queue_socket = Path(args.queue_socket)
        if not args.parent_attempt_id and not args.parent_session_id:
            args.parent_attempt_id = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
        if bool(args.parent_attempt_id) == bool(args.parent_session_id):
            raise CompletionError("parent-identity-invalid")
        if args.parent_session_id and args.thread_id != args.parent_session_id:
            raise CompletionError("parent-thread-identity-mismatch")
        if not args.sealed_batch_id:
            raise CompletionError("sealed-batch-id-missing")
        attempts = list(args.attempt_id or [])
        if not attempts or len(set(attempts)) != len(attempts) or len(attempts) > 4:
            raise CompletionError("attempt-set-invalid")
        args.parent_lifetime = capture_parent_lifetime(args)
        duty_id, _record = batch_obligations.create(args)
        args._obligation_id = duty_id
        args._resume_execution = False

    record = batch_obligations.read(args.jobs, args._obligation_id)
    if record is None:
        raise CompletionError("batch-obligation-missing")
    _validate_obligation_identity(args, record.get("identity") or {})
    lease = batch_obligations.acquire(args.jobs, args._obligation_id, inherited_fd)
    if lease is None:
        return {"schema_version": 1, "status": "retryable",
                "reason": "completion-observer-already-running"}, 75
    args.obligation_lock_fd = lease
    ownership_lost = False
    result = None
    code = 75
    try:
        claimed = batch_obligations.update_for_observer(
            args.jobs, args._obligation_id, lease, claim=True,
            expected_observer=expected_observer, reason="observing")
        if claimed is None:
            return {"schema_version": 1, "status": "retryable",
                    "reason": "completion-observer-ownership-lost"}, 75
        result, code = _execute_once(args)
        if _observer_ownership_lost(args):
            raise ObserverOwnershipLost("completion-observer-ownership-lost")
        status = result.get("status") if isinstance(result, dict) else None
        current = batch_obligations.read(args.jobs, args._obligation_id) or {}
        if current.get("state") not in batch_obligations._PENDING:
            return result, code
        if current.get("outcome") is not None:
            if status == "accepted":
                _update_duty(args, args._obligation_id,
                    state="complete", delivery="accepted", reason="delivered")
            elif status == "retryable" and isinstance(result, dict) \
                    and result.get("reason") == PARENT_SESSION_ENDED:
                _update_duty(args, args._obligation_id,
                    state="unknown", delivery="pending", reason=PARENT_SESSION_ENDED,
                    parent_lifetime=getattr(args, "parent_lifetime", None))
            else:
                _update_duty(args, args._obligation_id,
                    state="delivery-pending", delivery=status or "unknown",
                    reason=(result.get("reason") if isinstance(result, dict) else None)
                           or "delivery-unconfirmed")
        elif status == "retryable":
            reason = str(result.get("reason") or "observation-unavailable")
            _update_duty(args, args._obligation_id,
                state="unknown", reason=reason, delivery="pending")
        return result, code
    except ObserverOwnershipLost:
        ownership_lost = True
        return {"schema_version": 1, "status": "retryable",
                "reason": "completion-observer-ownership-lost"}, 75
    finally:
        still_owner = (not ownership_lost and batch_obligations.observer_owns_current(
            args.jobs, args._obligation_id, lease))
        if still_owner:
            current = batch_obligations.read(args.jobs, args._obligation_id) or {}
            if current.get("state") in batch_obligations._PENDING:
                if getattr(args, "_time_budget_spent", False):
                    # Only a generation that used its whole observation budget
                    # passes its retained lease straight to a successor.
                    batch_obligations.handoff_observer(
                        args.jobs, args._obligation_id, lease,
                        service_current=service_current)
                else:
                    # Any other pending end (an immediate retryable, an error,
                    # a stopped recipient) returns to the existing runner and
                    # its bounded backoff, never an immediate successor loop.
                    batch_obligations.request_runner(args.jobs)
        # Keep this descriptor open until the command exits. The successor,
        # when launched, shares the same open file description and therefore
        # retains the flock as this process returns its receipt.


def main() -> int:
    args = parser().parse_args()
    try:
        result, code = execute(args)
    except CompletionError as exc:
        result = {
            "schema_version": 1,
            "status": "rejected",
            "reason": str(exc),
        }
        code = 65
    print(canonical(result), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
