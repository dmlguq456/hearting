#!/usr/bin/env python3
"""Resume one Codex App Server thread only after exact registered children settle."""

from __future__ import annotations

import argparse
from model_config import parse_context_window
from codex_permission_profile import (
    OWNER_COMMIT_NO_NETWORK_PROFILE_NAME,
    OWNER_OBSERVE_PROFILE_NAME,
    PROFILE_NAME,
    RECORDISH_PROFILE_NAME,
    commit_profile_config,
    config_arguments,
    owner_turn_profile_config,
    recordish_profile_config,
    turn_permissions_available,
)
from worker_runtime_home import codex_worker_arguments
import json
import queue
import threading
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any
from types import SimpleNamespace
import commit_policy
from dispatch_receipt_identity import JOIN_REASONS, COMPLETION_ACTIONS
from dispatch_owner_input import OwnerInput
import dispatch_resource_wait as RESOURCE_WAIT
import git_index_lock

from dispatch_completion_join import (
    JoinContractError,
    completion_followup_text,
    SupervisorOutbox,
    advance_delivery_timing,
    acknowledge_supervisor_delivery,
    close_wrapper_pass,
    current_children,
    delivery_timing_fields,
    exact_attempt_row,
    log_delivery_refusal,
    materialize_after_terminal_close,
    prepare_supervisor_outbox,
    partition_runtime_wait_children,
    refresh_supervisor_outbox_actions,
    reconcile_finished_children,
    read_supervisor_phase_state,
    receipt_with_current_actions,
    wait_for_delivery_projection,
    receipt_with_stage_advance,
    remove_supervisor_state,
    runtime_wait_requested,
    cancel_unstarted_chain_successors,
    settle_runtime_wait_children,
    start_retry_prompt,
    validate_delivery_timing,
    write_supervisor_state,
    begin_supervisor_turn,
)
from dispatch_contract import (
    DispatchContractError,
    hold_supervisor_lease,
)
from dispatch_continuation_budget import (
    AdmitVerdict,
    ContinuationLedger,
    positive_continuation_limit,
    resolve_continuation_budget,
)
import dispatch_budget_record as budget_record
import dispatch_stage_advance as stage_advance
import session_supervisor_decisions as DECISIONS
import dispatch_subsession_advance as subsession_advance
from dispatch_supervisor_terminal import (
    SupervisorTerminal,
    classify_codex_result,
    classify_supervisor_error,
    codex_turn_failure_terminal,
    reconcile_supervisor_terminal,
)


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_JOIN_STATES = frozenset({"ready", "timeout"})


# Deliberately UNRESOLVED, for the same reason as the identical helper in
# claude-session-supervisor.py: resolving here would pin the receipt to the
# versioned release directory behind the managed `current` pointer, so a
# release rotation mid-flight would leave every delivered command denied.
def harvest_surface(launch_file: str) -> str:
    """Shared harvest CLI path as seen from THIS supervisor's launch path.

    Absolute, not repository-relative. The park guard resolves a relative
    `adapters/codex/bin/preflight.sh` against the *owner's* cwd, so the
    relative form is admitted only when that cwd happens to be a harness root
    and is denied for every ordinary project worktree -- the same
    producer<->guard vocabulary split that deadlocked att-30344fd4 (plan
    SS3.4 D1). Surfaced by the D2a precheck.
    """

    return shlex.quote(
        str(
            Path(launch_file).absolute().parents[1]
            / "adapters" / "codex" / "bin" / "preflight.sh"
        )
    )


SHARED_HARVEST_SURFACE = harvest_surface(__file__)


SupervisorError = DECISIONS.SupervisorError


class TurnFailed(SupervisorError):
    """A structured Codex TurnError closed the turn.

    Carries the normalized `dispatch.supervisor.turn.failed` payload the
    event loop already logged before raising, so the catch block can hand it
    straight to `codex_turn_failure_terminal` -- the same function the
    post-exit log reader calls on the identical logged row.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__("app-server-turn-failed")
        self.payload = payload


def _normalize_codex_error_info(raw: Any) -> Any:
    """Normalize a raw CodexErrorInfo into the shape logged as `codex_error_info`.

    The protocol carries either a bare string discriminant (`"unauthorized"`,
    `"serverOverloaded"`, ...) or a single-key object wrapping detail,
    `{<kind>: {httpStatusCode: N, ...}}`. Both are reduced to the vocabulary
    `codex_turn_failure_terminal` reads back: the bare string unchanged, or
    `{"kind": <kind>, "http_status": <N or None>}`. Any other shape is
    unknown and normalizes to None rather than guessed.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and len(raw) == 1:
        ((kind, detail),) = raw.items()
        if not isinstance(kind, str):
            return None
        http_status = detail.get("httpStatusCode") if isinstance(detail, dict) else None
        return {"kind": kind, "http_status": http_status if isinstance(http_status, int) else None}
    return None


def _turn_failed_payload(turn_id: str, error: dict[str, Any] | None) -> dict[str, Any]:
    """Build the bounded `dispatch.supervisor.turn.failed` log row.

    `message`/`additional_details` are each capped at 400 chars with
    newlines/commas normalized (registry-row-safe, matching
    `classify_supervisor_error`'s existing `reason[:240]` convention); the
    whole payload stays far under the 4 KiB bound from those two caps alone.
    """
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or "")[:400].replace("\n", " ").replace(",", ";")
    details_raw = error.get("additionalDetails")
    details = (
        str(details_raw)[:400].replace("\n", " ").replace(",", ";")
        if details_raw not in (None, "")
        else ""
    )
    return {
        "type": "dispatch.supervisor.turn.failed",
        "turn_id": turn_id,
        "codex_error_info": _normalize_codex_error_info(error.get("codexErrorInfo")),
        "message": message,
        "additional_details": details,
    }


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False), flush=True)


def attempt_stage_advance(args, current_rows, new_attempts, delivery_timing=None):
    """Shared runtime-owned stage advance (`session_supervisor_decisions`)."""
    return DECISIONS.attempt_stage_advance(args, current_rows, new_attempts, delivery_timing,
                                           emit=lambda value: emit(value), default_harness="codex")

def reconcile(args: argparse.Namespace, terminal: SupervisorTerminal) -> bool:
    try:
        outcome = reconcile_supervisor_terminal(
            args.jobs, args.parent_attempt_id, terminal, emit=emit
        )
        # SD-111 P2 trigger 1: dispatch_supervisor_terminal cannot import this
        # module (circular), so its own docstring asks the caller to close
        # the gap when the row actually closed.
        if outcome == "closed":
            materialize_after_terminal_close(Path(args.jobs), args.parent_attempt_id)
        return True
    except Exception as exc:
        reason = f"terminal-reconcile-failed-{type(exc).__name__}"
        # SD-115 axis 4 (round 2, review 🔴2): mirrors
        # claude-session-supervisor.py's `reconcile()` -- an emit()-only
        # report is stdout/stderr-only and vanishes with the process, so a
        # sealed-tree loss (or any reconcile exception) that escapes to here
        # left zero durable trace on the Codex adapter even though the
        # Claude adapter already wrote one.
        log_delivery_refusal(Path(args.jobs), args.parent_attempt_id, reason)
        emit({"type": "dispatch.supervisor.error", "reason": reason})
        return False


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Translate App Server camelCase items to the existing exec JSONL wire."""

    item_type = item.get("type")
    if item_type == "agentMessage":
        return {
            "type": "agent_message",
            "id": item.get("id"),
            "text": item.get("text"),
        }
    if item_type == "commandExecution":
        return {
            "type": "command_execution",
            "id": item.get("id"),
            "command": item.get("command"),
            "aggregated_output": item.get("aggregatedOutput"),
            "exit_code": item.get("exitCode"),
            "status": item.get("status"),
        }
    return {"type": str(item_type or "unknown"), "id": item.get("id")}


class CodexGitFailureFeedback:
    """Bounded, advisory turn/steer transport for exact command-result items."""

    MAX_PENDING = 32
    MAX_TURNS = 8
    MAX_COMMANDS_PER_TURN = 128

    def __init__(self, server):
        self.server = server
        self.active: tuple[str, str] | None = None
        self.turn_order: list[tuple[str, str]] = []
        self.seen: dict[tuple[str, str], set[str]] = {}
        self.limit_reported: set[tuple[str, str]] = set()
        self.pending: dict[int, dict[str, str]] = {}

    @staticmethod
    def _event(status: str, record: dict[str, str], **extra) -> None:
        emit({"type": "dispatch.supervisor.git_failure_feedback", "status": status,
              "thread_id": record.get("thread_id"), "turn_id": record.get("turn_id"),
              "command_item_id": record.get("command_item_id"), **extra})

    def begin_turn(self, thread_id: str, turn_id: str) -> None:
        if (not isinstance(thread_id, str) or not thread_id or len(thread_id) > 256
                or not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256):
            self.active = None
            return
        key = (thread_id, turn_id)
        self.active = key
        if key not in self.seen:
            self.seen[key] = set()
            self.turn_order.append(key)
        while len(self.turn_order) > self.MAX_TURNS:
            old = self.turn_order.pop(0)
            self.seen.pop(old, None)
            self.limit_reported.discard(old)

    def finish_turn(self, turn_id: str) -> None:
        if self.active is None or self.active[1] != turn_id:
            return
        ended = self.active
        self.active = None
        for record in self.pending.values():
            if (record["thread_id"], record["turn_id"]) == ended and not record.get("end_reported"):
                record["end_reported"] = "1"
                self._event("observation-limit", record, reason="turn-ended-before-feedback-ack")

    def consider(self, thread_id: str, turn_id: str, item: dict[str, Any]) -> None:
        key = (thread_id, turn_id)
        if self.active != key or item.get("type") != "commandExecution":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
            return
        seen = self.seen.get(key)
        if seen is None:
            return
        status = item.get("status")
        exit_code = item.get("exitCode")
        command, cwd, output = item.get("command"), item.get("cwd"), item.get("aggregatedOutput")
        if (not isinstance(status, str) or status not in ("completed", "failed")
                or not git_index_lock.codex_command_candidate(command, cwd, exit_code, output)):
            return
        if item_id in seen:
            self._event("duplicate-ignored", {
                "thread_id": thread_id, "turn_id": turn_id, "command_item_id": item_id,
            })
            return
        if len(seen) >= self.MAX_COMMANDS_PER_TURN:
            if key not in self.limit_reported:
                self.limit_reported.add(key)
                self._event("observation-limit", {
                    "thread_id": thread_id, "turn_id": turn_id, "command_item_id": item_id,
                }, reason="per-turn-command-result-limit")
            return
        seen.add(item_id)

        try:
            diagnosis = git_index_lock.diagnose_codex_command(
                command, cwd, exit_code, output,
            )
        except Exception as exc:
            self._event("observation-limit", {
                "thread_id": thread_id, "turn_id": turn_id, "command_item_id": item_id,
            }, reason=f"diagnostic-failed:{type(exc).__name__}")
            return
        if diagnosis is None or diagnosis.get("state") == "unbound":
            return

        record = {"thread_id": thread_id, "turn_id": turn_id, "command_item_id": item_id}
        if len(self.pending) >= self.MAX_PENDING:
            self._event("observation-limit", record, reason="pending-feedback-limit")
            return
        request_id = self.server.next_id
        self.server.next_id += 1
        pending = {**record, "diagnosis_state": str(diagnosis.get("state", "unavailable"))}
        self.pending[request_id] = pending
        request = {
            "jsonrpc": "2.0", "id": request_id, "method": "turn/steer",
            "params": {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": diagnosis.get("message", "Git index-lock diagnosis unavailable."),
                           "text_elements": []}],
            },
        }
        try:
            self.server.send(request)
        except Exception as exc:
            self.pending.pop(request_id, None)
            self._event("observation-limit", record, reason=f"feedback-send-failed:{type(exc).__name__}")
            return
        self._event("submitted", record, diagnosis_state=pending["diagnosis_state"])

    def response(self, value: dict[str, Any]) -> bool:
        request_id = value.get("id")
        record = (self.pending.get(request_id)
                  if isinstance(request_id, int) and not isinstance(request_id, bool) else None)
        if (record is None or value.get("jsonrpc") != "2.0" or "method" in value
                or ("result" in value) == ("error" in value)):
            return False
        if ("result" in value and not isinstance(value.get("result"), dict)) or (
                "error" in value and not isinstance(value.get("error"), dict)):
            return False
        self.pending.pop(request_id, None)
        late = self.active != (record["thread_id"], record["turn_id"])
        if "error" in value:
            self._event("observation-limit", record,
                        reason="late-feedback-rejected" if late else "feedback-rejected")
            return True
        result = value.get("result")
        if not isinstance(result, dict) or result.get("turnId") != record["turn_id"]:
            self._event("observation-limit", record,
                        reason="late-feedback-ack-mismatch" if late else "feedback-ack-mismatch")
        elif late:
            self._event("observation-limit", record, reason="late-feedback-ack")
        else:
            self._event("acknowledged", record)
        return True


def _usage_counter(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1:
        return value
    return None


def _normalize_usage_breakdown(value: Any) -> dict[str, int]:
    """Allow only documented numeric usage fields into the attempt log."""
    if not isinstance(value, dict):
        return {}
    fields = {
        "inputTokens": "input_tokens",
        "cachedInputTokens": "cached_input_tokens",
        "outputTokens": "output_tokens",
        "reasoningOutputTokens": "reasoning_output_tokens",
        "totalTokens": "total_tokens",
    }
    normalized: dict[str, int] = {}
    for source, target in fields.items():
        counter = _usage_counter(value.get(source))
        if counter is not None:
            normalized[target] = counter
    return normalized


def normalize_token_usage(value: Any) -> dict[str, Any] | None:
    """Privacy-minimal projection of `thread/tokenUsage/updated`."""
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {
        "last": _normalize_usage_breakdown(value.get("last")),
        "total": _normalize_usage_breakdown(value.get("total")),
    }
    window = _usage_counter(value.get("modelContextWindow"))
    if window is not None:
        normalized["model_context_window"] = window
    if not normalized["last"] and not normalized["total"] and window is None:
        return None
    return normalized


def _typed_receipt(
    value: Any,
    parent_attempt_id: str,
    attempts: set[str],
    *,
    accept_stage_advance: bool = False,
    stage_advance_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise SupervisorError("join-receipt-schema-invalid")
    if value.get("state") not in ALLOWED_JOIN_STATES:
        raise SupervisorError("join-receipt-state-invalid")
    if value.get("parent_attempt_id") != parent_attempt_id:
        raise SupervisorError("join-receipt-parent-mismatch")
    raw_children = value.get("children")
    if not isinstance(raw_children, list):
        raise SupervisorError("join-receipt-children-invalid")
    children: list[dict[str, str]] = []
    observed: set[str] = set()
    allowed_readiness = {"ready", "pending"}
    for raw in raw_children:
        if not isinstance(raw, dict):
            raise SupervisorError("join-receipt-child-invalid")
        attempt = raw.get("attempt_id")
        readiness = raw.get("readiness")
        reason = raw.get("reason")
        status = raw.get("status")
        required_action = raw.get("required_action")
        if (
            not isinstance(attempt, str)
            or attempt not in attempts
            or attempt in observed
            or readiness not in allowed_readiness
            or reason not in JOIN_REASONS
            or status not in {"open", "running", "done"}
            or required_action not in COMPLETION_ACTIONS
        ):
            raise SupervisorError("join-receipt-child-contract-invalid")
        observed.add(attempt)
        children.append(
            {
                "attempt_id": attempt,
                "status": status,
                "readiness": readiness,
                "reason": reason,
                "required_action": required_action,
            }
        )
    if observed != attempts:
        raise SupervisorError("join-receipt-attempt-set-mismatch")
    receipt = {
        "schema_version": 2,
        "state": value["state"],
        "parent_attempt_id": parent_attempt_id,
        "children": children,
        "delivery_timing": value.get("delivery_timing", delivery_timing_fields()),
    }
    if accept_stage_advance:
        receipt = receipt_with_stage_advance(
            receipt,
            stage_advance_record=stage_advance_record,
        )
    return receipt


def run_join(args: argparse.Namespace, attempts: set[str]) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    command = shlex.split(args.join_command) if args.join_command else [
        sys.executable,
        str(ROOT / "utilities" / "dispatch_completion_join.py"),
    ]
    command += [
        "--jobs", args.jobs,
        "--parent-attempt-id", args.parent_attempt_id,
        "--interval", str(args.join_interval),
        "--timeout", str(args.join_timeout),
        "--recover-receiptless",
    ]
    for attempt in sorted(attempts):
        command += ["--attempt-id", attempt]
    join_env = os.environ.copy()
    join_env["HEARTING_JOIN_CONSUMER_PID"] = str(os.getpid())
    try:
        result = subprocess.run(
            command,
            env=join_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(args.join_timeout + 60.0, 60.0),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("join-process-failed") from exc
    if len(result.stdout.encode("utf-8", "replace")) > 65536:
        raise SupervisorError("join-receipt-oversized")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise SupervisorError("join-receipt-json-invalid") from exc
    if result.returncode not in {0, 3}:
        from dispatch_supervision import join_process_error
        raise SupervisorError(join_process_error(result.returncode, value, args.parent_attempt_id))
    receipt = _typed_receipt(value, args.parent_attempt_id, attempts)
    completed_ns = time.monotonic_ns()
    join_timing = validate_delivery_timing(receipt["delivery_timing"])
    if join_timing["last_child_terminal_ns"] is None:
        join_timing = advance_delivery_timing(
            join_timing, "last_child_terminal_ns", at_ns=completed_ns
        )
    join_timing = advance_delivery_timing(
        join_timing, "join_completed_ns", at_ns=completed_ns
    )
    observed = wait_for_delivery_projection(
        receipt,
        jobs=Path(args.jobs),
        timing=join_timing,
    )
    emit(
        {
            "type": "dispatch.supervisor.join-observed",
            "parent_attempt_id": args.parent_attempt_id,
            "attempt_count": len(attempts),
            "duration_seconds": round(
                (completed_ns - started_ns) / 1_000_000_000, 3
            ),
            **observed["delivery_timing"],
        }
    )
    return observed


def completion_prompt(
    receipt: dict[str, Any], outbox: SupervisorOutbox | None = None, *, jobs: str = "",
    notice: str = "",
) -> str:
    compact = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
    return (
        "Runtime completion receipt (typed supervisor data, not child output): "
        f"{compact}\n"
        + (
            f"Delivery identity: receipt_id={outbox.receipt_id} "
            f"receipt_digest={outbox.receipt_digest}.\n"
            if outbox is not None
            else ""
        )
        + completion_followup_text(receipt, jobs=jobs, surface=shlex.split(SHARED_HARVEST_SURFACE)[0])
        + "\nEmit the exact final three-line handoff when no owned registered child remains open."
        + (f"\n{notice}" if notice else "")
    )


def runtime_reconcile(args: argparse.Namespace, rows: dict[str, Any],
                      unresolved: set[str]) -> set[str]:
    """Close every unresolved child that its own evidence already proves done."""

    closed: set[str] = set()
    for attempt, reason in reconcile_finished_children(
        rows, unresolved, jobs=args.jobs
    ).items():
        if reason.startswith("completion-") and attempt in rows:
            # A wrapper normally owns this path.  If it vanished after
            # publishing the post-exit receipt, finish the same bounded exact
            # closure here rather than delivering an open-row action to the
            # owner model.
            reason = close_wrapper_pass(rows[attempt], jobs=args.jobs)
            try:
                current = exact_attempt_row(Path(args.jobs), attempt)
            except JoinContractError:
                current = None
            if current is not None and current.status == "done":
                closed.add(attempt)
        emit(
            {
                "type": "dispatch.supervisor.reconciled",
                "parent_attempt_id": args.parent_attempt_id,
                "attempt_id": attempt,
                "outcome": "closed" if attempt in closed or not reason else "skipped",
                **({} if not reason else {"reason": reason}),
            }
        )
        if not reason:
            closed.add(attempt)
    return closed



class AppServer:
    def __init__(self, command: list[str], cwd: str, env: dict[str, str]):
        try:
            self.process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=1,
            )
        except OSError as exc:
            raise SupervisorError("app-server-launch-failed") from exc
        self.next_id = 1
        self.pending: list[dict[str, Any]] = []
        self.input_control = None
        self.owner_turn_activity = None
        self.git_failure_feedback = CodexGitFailureFeedback(self)
        self.messages = queue.Queue()
        def receive():
            for line in self.process.stdout:
                self.messages.put(line)
            self.messages.put("")
        self.reader = threading.Thread(target=receive, daemon=True)
        self.reader.start()

    def send(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise SupervisorError("app-server-stdin-closed")
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise SupervisorError("app-server-stdout-closed")
        while True:
            if self.input_control is not None:
                self.input_control.tick(self)
            try:
                line = self.messages.get(timeout=0.2)
                break
            except queue.Empty:
                activity = self.owner_turn_activity
                if activity is not None and activity.expired():
                    raise SupervisorError("codex-turn-no-progress")
                continue
        if not line:
            raise SupervisorError("app-server-eof")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SupervisorError("app-server-protocol-json-invalid") from exc
        if not isinstance(value, dict):
            raise SupervisorError("app-server-protocol-shape-invalid")
        if self.input_control is not None and self.input_control.response(value):
            activity = self.owner_turn_activity
            if activity is not None and activity.expired():
                raise SupervisorError("codex-turn-no-progress")
            return self.read()
        if self.git_failure_feedback.response(value):
            activity = self.owner_turn_activity
            if activity is not None and activity.expired():
                raise SupervisorError("codex-turn-no-progress")
            return self.read()
        return value

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            value = self.read()
            if value.get("id") == request_id:
                if "error" in value:
                    raise SupervisorError(f"app-server-request-failed:{method}")
                result = value.get("result")
                if not isinstance(result, dict):
                    raise SupervisorError(f"app-server-result-invalid:{method}")
                return result
            self.pending.append(value)

    def notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        value: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            value["params"] = params
        self.send(value)

    def next_event(self) -> dict[str, Any]:
        return self.pending.pop(0) if self.pending else self.read()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

        if self.process.stdin is not None:
            self.process.stdin.close()
        self.reader.join(timeout=1)
        if not self.reader.is_alive() and self.process.stdout is not None:
            self.process.stdout.close()


def sandbox_policy(args: argparse.Namespace) -> dict[str, Any]:
    network = bool(args.network_access)
    if args.sandbox == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if args.sandbox == "read-only":
        return {"type": "readOnly", "networkAccess": network}
    roots = list(dict.fromkeys([args.worktree, *args.writable_root]))
    return {
        "type": "workspaceWrite",
        "writableRoots": roots,
        "networkAccess": network,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def apply_owner_runtime_permissions(args: argparse.Namespace) -> None:
    """Refresh this owner's native permission projection at the same-thread boundary."""
    try:
        context = commit_policy.read_runtime_permission_context()
    except ValueError as exc:
        raise SupervisorError(str(exc)) from exc
    if context is None or context.get("adapter") != "codex":
        args.owner_runtime_permission_state = None
        return
    if (context.get("attempt_id") != args.parent_attempt_id
            or Path(str(context.get("worktree"))).resolve() != Path(args.worktree).resolve()):
        raise SupervisorError("owner-runtime-permission-context-binding-mismatch")
    if not commit_policy.runtime_permission_context_is_current(context):
        raise SupervisorError("owner-runtime-permission-route-changed")
    args.owner_runtime_base_sandbox = str(context.get("base_sandbox") or args.sandbox)
    projection_args = SimpleNamespace(
        worker_type=context["worker_type"],
        dispatch_depth=context["dispatch_depth"],
        capability=context["capability"],
        worktree=context["worktree"],
        route_file=context["route_file"],
        route_id=context["route_id"],
        route_hash=context["route_hash"],
        artifact_root=context.get("artifact_root"),
        artifact_output_dir=context.get("artifact_output_dir"),
        owner_route_binding=None,
    )
    state = commit_policy.runtime_permission_projection(projection_args)
    if state["observation_only"]:
        args.sandbox = "read-only"
        args.network_access = False
    else:
        args.sandbox = str(context.get("base_sandbox") or args.sandbox)
        args.network_access = bool(context.get("network_eligible")) or bool(
            state["task_publish_scope"]
        )
    roots = list(getattr(args, "base_writable_root", args.writable_root))
    if state["may_commit"]:
        commit_args = SimpleNamespace(
            worker_type=context["worker_type"], worktree=context["worktree"],
            dispatch_depth=context["dispatch_depth"],
        )
        roots.extend(str(path) for path in commit_policy.commit_git_metadata_dirs(commit_args))
    args.writable_root = list(dict.fromkeys(roots))
    args.owner_runtime_permission_state = state


def configure_permission_profiles(args: argparse.Namespace, command: list[str]) -> None:
    """Prepare owner profiles; enable turn selection only on known-capable runtimes."""
    args.turn_permissions_supported = False
    args.owner_turn_permission_profiles = None
    if getattr(args, "route_file", None):
        from dispatch_contract import diagnose_repository_start
        if diagnose_repository_start(args.worktree).get("state") == "non-git":
            profile = recordish_profile_config(
                args.worktree, list(args.writable_root), args.route_file,
                network=bool(args.network_access),
            )
            if profile is None:
                raise SupervisorError("codex-non-git-recordish-profile-unsupported")
            args.native_permission_profile = profile
            args.recordish_profile_active = True
            return
    owner_state = getattr(args, "owner_runtime_permission_state", None)
    if (owner_state is not None
            and getattr(args, "owner_runtime_base_sandbox", args.sandbox) == "workspace-write"):
        selector_supported = turn_permissions_available(command)
        roots = list(getattr(args, "base_writable_root", args.writable_root))
        git_dirs = commit_policy.worktree_git_dirs(args.worktree)
        if git_dirs is not None:
            git_dir, common_dir = git_dirs
            if git_dir != common_dir:
                roots.extend(str(path) for path in (
                    git_dir, common_dir / "objects", common_dir / "refs", common_dir / "logs",
                ))
        profile = owner_turn_profile_config(
            args.worktree, roots, "workspace-write",
            primary_commit=bool(getattr(args, "primary_git_commit", False)),
        )
        if profile is not None:
            args.native_permission_profile = profile
            args.turn_permissions_supported = selector_supported
            args.owner_turn_permission_profiles = {
                "observation": OWNER_OBSERVE_PROFILE_NAME,
                "commit": PROFILE_NAME,
                "commit_no_network": OWNER_COMMIT_NO_NETWORK_PROFILE_NAME,
            }
            return
    args.native_permission_profile = commit_profile_config(
        args.worktree, args.writable_root, args.sandbox, args.network_access,
        primary_commit=bool(getattr(args, "primary_git_commit", False)),
    )


def resource_sandbox_command(args, row):
    """Apply this owner's selected native policy; no tool namespace inheritance."""
    from codex_permission_profile import PROFILE_NAME
    if getattr(args, "recordish_profile_active", False):
        options = [*config_arguments(args.native_permission_profile), "-P", RECORDISH_PROFILE_NAME]
        enforcement = "os-sandbox"
    elif args.sandbox == "danger-full-access":
        options = ["-P", ":danger-full-access"]
        enforcement = "none"
    elif args.sandbox in {"workspace-write", "read-only"}:
        profile = getattr(args, "native_permission_profile", None)
        selected_profile = PROFILE_NAME
        if profile is None:
            profile = {"default_permissions": PROFILE_NAME, "permissions": {PROFILE_NAME: {
                "extends": ":workspace" if args.sandbox == "workspace-write" else ":read-only",
                "filesystem": ({str(Path(root).resolve()): "write"
                    for root in [args.worktree, *args.writable_root]} if args.sandbox == "workspace-write" else {}),
                "network": {"enabled": bool(args.network_access)},
            }}}
        owner_profiles = getattr(args, "owner_turn_permission_profiles", None)
        owner_state = getattr(args, "owner_runtime_permission_state", None)
        if owner_profiles and isinstance(owner_state, dict):
            if owner_state.get("observation_only") or not owner_state.get("may_commit"):
                selected_profile = owner_profiles["observation"]
            else:
                selected_profile = (
                    owner_profiles["commit"] if args.network_access
                    else owner_profiles["commit_no_network"]
                )
        elif getattr(args, "native_permission_profile", None) is not None \
                and args.sandbox == "read-only":
            # A prepared commit profile must not survive a safety downgrade
            # on runtimes without per-turn profile selection.
            selected_profile = ":read-only"
        options = [*config_arguments(profile), "-P", selected_profile]
        enforcement = "os-sandbox"
    else:
        raise SupervisorError("resource-sandbox-selection-unsupported")
    command = ["codex", "sandbox", *options, "--include-managed-config", "-C", args.worktree,
               "--", "/bin/sh", "-c", 'cd -- "$1" || exit; shift; exec "$@"',
               "resource-payload", row["cwd"], *row["command"]]
    return command, {"mode": args.sandbox, "enforcement": enforcement,
                     "network_access": bool(args.network_access)}


def run_turn(
    server: AppServer,
    *,
    thread_id: str,
    prompt: str,
    args: argparse.Namespace,
    activity=None,
) -> tuple[str | None, dict[str, Any] | None]:
    apply_owner_runtime_permissions(args)
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": args.worktree,
    }
    # Keep an active named profile authoritative on ordinary turns. A
    # per-turn Git/task overlay is valid only after the exact owner context
    # above has revalidated its route and current worktree state.
    owner_projection = getattr(args, "owner_runtime_permission_state", None)
    selected_profile = None
    profiles = getattr(args, "owner_turn_permission_profiles", None)
    if owner_projection is not None and getattr(args, "turn_permissions_supported", False) and profiles:
        if owner_projection.get("observation_only"):
            selected_profile = profiles["observation"]
        elif owner_projection.get("may_commit") and args.sandbox == "workspace-write":
            selected_profile = (
                profiles["commit"] if args.network_access else profiles["commit_no_network"]
            )
    if selected_profile is not None:
        params["permissions"] = selected_profile
    elif ((owner_projection is not None and not getattr(args, "recordish_profile_active", False))
          or getattr(args, "native_permission_profile", None) is None):
        params["sandboxPolicy"] = sandbox_policy(args)
    if args.approval != "inherit" and getattr(args, "native_permission_profile", None) is None:
        params["approvalPolicy"] = args.approval
    if args.model:
        params["model"] = args.model
    if args.reasoning:
        params["effort"] = args.reasoning
    response = server.request("turn/start", params)
    turn = response.get("turn")
    if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
        raise SupervisorError("turn-start-response-invalid")
    turn_id = turn["id"]
    permission_event = commit_policy.permission_projection_event(
        owner_projection, adapter="codex", attempt_id=getattr(args, "parent_attempt_id", ""),
        thread_id=thread_id, turn_id=turn_id, sandbox=args.sandbox,
        network_access=bool(args.network_access),
        file_enforcement=("os-sandbox" if args.sandbox == "workspace-write" else "none"),
        read_enforcement=("os-sandbox" if args.sandbox in {"workspace-write", "read-only"} else "none"),
        network_enforcement=("os-sandbox" if args.sandbox == "workspace-write" else "none"),
    )
    if permission_event is not None:
        emit(permission_event)
    control = getattr(args, "owner_input", None)
    if control is not None:
        control.started(turn_id)
    feedback = getattr(server, "git_failure_feedback", None)
    if feedback is not None:
        feedback.begin_turn(thread_id, turn_id)
    emit({"type": "dispatch.supervisor.turn.started", "thread_id": thread_id, "turn_id": turn_id})
    final_text: str | None = None
    final_item: dict[str, Any] | None = None
    turn_error: dict[str, Any] | None = None
    server.owner_turn_activity = activity
    try:
        while True:
            event = server.next_event()
            if "id" in event and "method" in event:
                raise SupervisorError("app-server-unexpected-request")
            method = event.get("method")
            raw_params = event.get("params")
            event_params = raw_params if isinstance(raw_params, dict) else {}
            completed_turn = event_params.get("turn")
            terminal_event = (method == "turn/completed" and isinstance(completed_turn, dict)
                              and completed_turn.get("id") == turn_id)
            if activity is not None and not terminal_event and activity.expired(
                    payload_progress=codex_payload_progress(event, thread_id, turn_id)):
                raise SupervisorError("codex-turn-no-progress")
            # The exact notification method that carries a same-turn TurnError
            # ahead of `turn/completed` is not confirmed from observed traffic
            # (plan.md item 4 sampling found zero structured-error rows); match
            # on shape (this turn's id + an `error` object) rather than bet on
            # one method name, so this still works whatever that method is
            # called. `turn/completed.turn.error` below remains the primary,
            # confirmed source either way.
            if event_params.get("turnId") == turn_id and isinstance(
                event_params.get("error"), dict
            ):
                turn_error = event_params["error"]
            if (method == "thread/tokenUsage/updated"
                    and event_params.get("threadId") == thread_id
                    and event_params.get("turnId") == turn_id):
                usage = normalize_token_usage(event_params.get("tokenUsage"))
                if usage is not None:
                    emit({
                        "type": "dispatch.supervisor.token_usage",
                        "thread_id": thread_id,
                        "turn_id": event_params.get("turnId"),
                        "token_usage": usage,
                    })
            if method == "item/started" and event_params.get("turnId") == turn_id:
                raw_item = event_params.get("item")
                if isinstance(raw_item, dict) and raw_item.get("type") == "commandExecution":
                    emit({"type": "item.started", "item": normalize_item(raw_item)})
            if method == "item/completed" and event_params.get("turnId") == turn_id:
                raw_item = event_params.get("item")
                if isinstance(raw_item, dict):
                    if event_params.get("threadId") == thread_id and feedback is not None:
                        feedback.consider(thread_id, turn_id, raw_item)
                    item = normalize_item(raw_item)
                    emit({"type": "item.completed", "item": item})
                    if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                        final_text = item["text"]
                        final_item = item
            completed = event_params.get("turn")
            if (
                method == "turn/completed"
                and isinstance(completed, dict)
                and completed.get("id") == turn_id
            ):
                emit({"type": "dispatch.supervisor.turn.completed", "thread_id": thread_id,
                      "turn_id": turn_id, "status": completed.get("status")})
                if completed.get("status") != "completed":
                    if feedback is not None:
                        feedback.finish_turn(turn_id)
                    raw_completed_error = completed.get("error")
                    error = (
                        raw_completed_error
                        if isinstance(raw_completed_error, dict)
                        else turn_error
                    )
                    payload = _turn_failed_payload(turn_id, error)
                    emit(payload)
                    raise TurnFailed(payload)
                if control is not None:
                    control.completed(turn_id)
                if feedback is not None:
                    feedback.finish_turn(turn_id)
                return final_text, final_item
    finally:
        if server.owner_turn_activity is activity:
            server.owner_turn_activity = None

_apply_notice = DECISIONS.apply_notice

def _admit_continuation(ledger, state_root, **kwargs):
    """Shared continuation admission (`session_supervisor_decisions`), with this module's `emit`."""
    return DECISIONS.admit_continuation(ledger, state_root, emit=lambda value: emit(value), **kwargs)

def _seal_terminal_handoff_or_raise(ledger, state_root, **kwargs):
    """Shared reserved terminal hand-off (`session_supervisor_decisions`)."""
    return DECISIONS.seal_terminal_handoff_or_raise(
        ledger, state_root, admit=lambda *a, **k: _admit_continuation(*a, **k), **kwargs)

def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--one-turn", action="store_true",
                       help=argparse.SUPPRESS)
    value.add_argument("--worktree", required=True)
    from dispatch_contract import inherited_jobs_argument
    jobs_options = inherited_jobs_argument()
    jobs_options["required"] = False
    value.add_argument("--jobs", **jobs_options)
    value.add_argument("--parent-attempt-id")
    value.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"), required=True)
    value.add_argument("--approval", choices=("untrusted", "on-request", "never", "inherit"), default="never")
    value.add_argument("--network-access", action="store_true")
    value.add_argument("--writable-root", action="append", default=[])
    value.add_argument("--primary-git-commit", action="store_true",
                       help="This run may commit: a primary checkout gets the commit-only .git profile")
    value.add_argument("--model")
    value.add_argument("--reasoning")
    value.add_argument("--model-context-window", type=parse_context_window)
    value.add_argument("--join-interval", type=float, default=2.0)
    value.add_argument("--join-timeout", type=float, default=3600.0)
    value.add_argument("--max-join-reparks", type=int, default=6, help="Compatibility input; join deadlines no longer terminate owned work")
    value.add_argument("--max-identical-redeliveries", type=int, default=2, help="Compatibility input; delivery no longer requires model bookkeeping commands")
    value.add_argument("--max-continuations", type=positive_continuation_limit)
    value.add_argument(
        "--continuation-warning-threshold", type=int, default=3,
        help="SD-116 (b): emit a typed budget-warning notice once gross_remaining falls to or below this many turns.",
    )
    value.add_argument("--route-file")
    value.add_argument("--route-id", default="")
    value.add_argument("--route-hash", default="")
    value.add_argument("--state-file", default=os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE"))
    value.add_argument("--lease-file")
    value.add_argument("--app-server-command", default=os.environ.get("CODEX_APP_SERVER_COMMAND"))
    value.add_argument("--join-command", default=os.environ.get("AGENT_DISPATCH_JOIN_COMMAND"))
    value.add_argument(
        "--enable-stage-advance",
        action="store_true",
        default=False,
        help=(
            "SD-110: attempt runtime-owned eligible-linear stage advance for "
            "each just-joined route-bound child before the model resume "
            "decision. Off by default -- landing behind this flag refuses "
            "with stage-advance-receipt-schema-unsupported and changes "
            "nothing about existing delivery."
        ),
    )
    return value


def context_window_arguments(args: argparse.Namespace) -> list[str]:
    value = getattr(args, "model_context_window", None)
    return [] if value is None else ["-c", f"model_context_window={value}"]


CODEX_THREAD_ITEM_TYPES = frozenset({
    "userMessage", "hookPrompt", "agentMessage", "functionCallOutput", "plan",
    "reasoning", "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall",
    "collabAgentToolCall", "subAgentActivity", "webSearch", "imageView", "sleep",
    "imageGeneration", "enteredReviewMode", "exitedReviewMode", "contextCompaction",
})


def codex_payload_progress(event: dict[str, Any], thread_id: str, turn_id: str) -> bool:
    """Accept shaped, attributable App Server work payloads for this turn."""
    method = event.get("method")
    params = event.get("params")
    if (not isinstance(method, str) or not isinstance(params, dict)
            or params.get("threadId") != thread_id or params.get("turnId") != turn_id):
        return False

    def nonempty_string(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip())

    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return False
        item_type = item.get("type")
        item_id = item.get("id")
        return (isinstance(item_type, str) and item_type in CODEX_THREAD_ITEM_TYPES
                and nonempty_string(item_id))

    if method == "turn/plan/updated":
        plan = params.get("plan")
        explanation = params.get("explanation")
        if (not isinstance(plan, list) or not plan
                or "explanation" not in params
                or (explanation is not None and not isinstance(explanation, str))):
            return False
        return all(
            isinstance(step, dict)
            and nonempty_string(step.get("step"))
            and isinstance(step.get("status"), str)
            and step["status"] in {"pending", "inProgress", "completed"}
            for step in plan
        )

    if method == "turn/diff/updated":
        return nonempty_string(params.get("diff"))

    if method == "item/fileChange/patchUpdated":
        changes = params.get("changes")
        if not nonempty_string(params.get("itemId")) or not isinstance(changes, list) or not changes:
            return False
        for change in changes:
            if (not isinstance(change, dict) or not nonempty_string(change.get("path"))
                    or not isinstance(change.get("diff"), str)):
                return False
            kind = change.get("kind")
            if (not isinstance(kind, dict) or not isinstance(kind.get("type"), str)
                    or kind["type"] not in {"add", "delete", "update"}):
                return False
            if kind.get("type") == "update":
                if "move_path" not in kind:
                    return False
                move_path = kind.get("move_path")
                if move_path is not None and not nonempty_string(move_path):
                    return False
        return True

    if method == "item/mcpToolCall/progress":
        return (nonempty_string(params.get("itemId"))
                and nonempty_string(params.get("message")))

    if method == "item/commandExecution/terminalInteraction":
        return (nonempty_string(params.get("itemId"))
                and nonempty_string(params.get("processId"))
                and isinstance(params.get("stdin"), str) and bool(params["stdin"]))

    if method in {
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/commandExecution/outputDelta",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
    }:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not nonempty_string(item_id) or not isinstance(delta, str) or not delta:
            return False
        if method == "item/reasoning/summaryTextDelta":
            index = params.get("summaryIndex")
            return isinstance(index, int) and not isinstance(index, bool) and index >= 0
        if method == "item/reasoning/textDelta":
            index = params.get("contentIndex")
            return isinstance(index, int) and not isinstance(index, bool) and index >= 0
        return True
    return False


def run_one_turn(args: argparse.Namespace) -> int:
    """Run a single stage/review turn without entering owner supervision."""
    prompt = sys.stdin.read()
    if not prompt.strip():
        emit({"type": "dispatch.supervisor.error", "reason": "initial-prompt-empty"})
        return 64

    command = shlex.split(args.app_server_command) if args.app_server_command else [
        "codex", "app-server", "--listen", "stdio://"
    ]
    args.base_writable_root = list(args.writable_root)
    apply_owner_runtime_permissions(args)
    configure_permission_profiles(args, command)
    if args.native_permission_profile is not None:
        command += config_arguments(args.native_permission_profile)

    command += codex_worker_arguments()
    command += context_window_arguments(args)
    server: AppServer | None = None
    completed_thread: str | None = None
    result_code = 70
    try:
        server = AppServer(command, args.worktree, dict(os.environ))
        server.request(
            "initialize",
            {"clientInfo": {"name": "hearting-dispatch-stage",
                            "title": "Hearting Dispatch Stage", "version": "1"},
             "capabilities": ({"experimentalApi": True}
                              if args.turn_permissions_supported else None)},
        )
        server.notification("initialized")
        thread_params: dict[str, Any] = {"cwd": args.worktree, "ephemeral": True}
        if args.native_permission_profile is None:
            thread_params["sandbox"] = args.sandbox
        if args.approval != "inherit":
            thread_params["approvalPolicy"] = args.approval
        if args.model:
            thread_params["model"] = args.model
        thread_result = server.request("thread/start", thread_params)
        thread = thread_result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise SupervisorError("thread-start-response-invalid")
        thread_id = thread["id"]
        run_turn(
            server, thread_id=thread_id, prompt=prompt, args=args
        )
        completed_thread = thread_id
        # A completed model turn has the same process-success boundary as raw
        # codex exec. The wrapper classifies the final handoff from this log;
        # FAIL/BLOCKED and invalid handoffs are not transport failures.
        result_code = 0
    except TurnFailed as exc:
        emit({"type": "dispatch.supervisor.error", "reason": "app-server-turn-failed"})
    except (DispatchContractError, JoinContractError, SupervisorError) as exc:
        reason = exc.reason if isinstance(exc, DispatchContractError) else str(exc)
        emit({"type": "dispatch.supervisor.error", "reason": reason[:240]})
    except Exception as exc:
        emit({"type": "dispatch.supervisor.error",
              "reason": f"supervisor-internal-{type(exc).__name__}"})
    finally:
        if server is not None:
            try:
                server.close()
            except Exception as exc:
                emit({"type": "dispatch.supervisor.error",
                      "reason": f"app-server-close-{type(exc).__name__}"})
                completed_thread = None
                result_code = 70
    if completed_thread is not None:
        emit({"type": "turn.completed", "thread_id": completed_thread})
    return result_code


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if args.one_turn:
        return run_one_turn(args)
    if not args.jobs:
        argument_parser.error("--jobs or AGENT_DISPATCH_JOBS is required outside --one-turn")
    if not args.parent_attempt_id or not args.lease_file:
        argument_parser.error("--parent-attempt-id and --lease-file are required outside --one-turn")
    continuation_budget = resolve_continuation_budget(
        explicit=args.max_continuations,
        route_file=args.route_file,
        route_id=args.route_id,
        route_hash=args.route_hash,
        expected_cwd=args.worktree,
    )
    args.max_continuations = continuation_budget.limit
    args.max_join_reparks = max(1, args.max_join_reparks)
    ledger = ContinuationLedger(continuation_budget)
    budget_state_root = Path(args.jobs).parent
    emit(
        {
            "type": "dispatch.supervisor.continuation-budget",
            "limit": continuation_budget.limit,
            "source": continuation_budget.source,
            "declared_nodes": continuation_budget.declared_nodes,
            "retry_slots": continuation_budget.retry_slots,
            "ordinary": continuation_budget.ordinary,
            "reserved": continuation_budget.reserved,
            "stall": continuation_budget.stall,
        }
    )
    prompt = sys.stdin.read()
    if not prompt.strip():
        terminal = classify_supervisor_error("codex", "initial-prompt-empty", 64)
        if not reconcile(args, terminal):
            return 70
        emit({"type": "dispatch.supervisor.error", "reason": "initial-prompt-empty"})
        return 64
    command = shlex.split(args.app_server_command) if args.app_server_command else [
        "codex", "app-server", "--listen", "stdio://"
    ]
    args.base_writable_root = list(args.writable_root)
    apply_owner_runtime_permissions(args)
    configure_permission_profiles(args, command)
    args.resource_launch_command = lambda row: resource_sandbox_command(args, row)
    if args.native_permission_profile is not None:
        command += config_arguments(args.native_permission_profile)
    command += codex_worker_arguments()
    command += context_window_arguments(args)
    state_path = Path(args.state_file) if args.state_file else None
    lease_path = Path(args.lease_file)
    runtime_env = dict(os.environ)
    if state_path is not None:
        runtime_env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(state_path)
    runtime_env["AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"] = str(lease_path)
    server: AppServer | None = None
    control = None
    lease = hold_supervisor_lease(
        args.jobs, args.parent_attempt_id, lease_path
    )
    lease_acquired = False
    delivered: set[str] = set()
    active_outbox: SupervisorOutbox | None = None
    delivery_timing = delivery_timing_fields()
    same_thread_resume_count = 0
    lease_exit: tuple[object, object, object] = (None, None, None)
    try:
        lease.__enter__()
        lease_acquired = True
        recovered = read_supervisor_phase_state(
            state_path, args.parent_attempt_id
        )
        if recovered is not None:
            delivered = set(recovered.delivered_attempt_ids)
            active_outbox = recovered.outbox
            if active_outbox is not None:
                if active_outbox.receipt is None:
                    raise SupervisorError("supervisor-outbox-receipt-missing")
                from dispatch_supervision import wait_for_batch
                refreshed_state = []
                def refresh_pending(selected):
                    rows = current_children(Path(args.jobs), args.parent_attempt_id, selected)
                    runtime_reconcile(args, {row.attempt_id: row for row in rows}, selected)
                    state = refresh_supervisor_outbox_actions(state_path, args.parent_attempt_id,
                        current_children(Path(args.jobs), args.parent_attempt_id, selected), jobs=Path(args.jobs))
                    refreshed_state[:] = [state]
                    return state.outbox.receipt
                wait_for_batch(join=refresh_pending, attempts=set(active_outbox.attempt_ids),
                    jobs=Path(args.jobs), parent_attempt_id=args.parent_attempt_id, emit=emit)
                active_outbox = refreshed_state[0].outbox
            if active_outbox is not None:
                if active_outbox.receipt is None:
                    raise SupervisorError("supervisor-outbox-receipt-missing")
                prompt = completion_prompt(
                    active_outbox.receipt, active_outbox, jobs=args.jobs
                )
                delivery_timing = validate_delivery_timing(
                    active_outbox.receipt["delivery_timing"]
                )
        server = AppServer(command, args.worktree, runtime_env)
        server.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "hearting-dispatch-supervisor",
                    "title": "Hearting Dispatch Supervisor",
                    "version": "1",
                },
                "capabilities": ({"experimentalApi": True}
                                 if args.turn_permissions_supported else None),
            },
        )
        server.notification("initialized")
        thread_params: dict[str, Any] = {
            "cwd": args.worktree,
            "ephemeral": not RESOURCE_WAIT.durable_native_session(args),
        }
        if args.native_permission_profile is None:
            thread_params["sandbox"] = args.sandbox
        if args.approval != "inherit":
            thread_params["approvalPolicy"] = args.approval
        if args.model:
            thread_params["model"] = args.model
        resource_session = (recovered.resource or {}).get("session_id") if recovered else None
        if resource_session:
            thread_params.pop("ephemeral", None)
            thread_params["threadId"] = resource_session
        thread_result = server.request("thread/resume" if resource_session else "thread/start", thread_params)
        thread = thread_result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise SupervisorError("thread-start-response-invalid")
        thread_id = thread["id"]
        if resource_session and thread_id != resource_session:
            raise SupervisorError("resource-native-session-changed")
        control = OwnerInput(args.jobs, args.parent_attempt_id, thread_id, "codex-active-turn", emit)
        args.owner_input = server.input_control = control

        launch_remediated: set[tuple[str, ...]] = set()
        next_prompt = prompt
        pending_notice = ""
        continuations = 0
        # SD-116 (c): mirrors claude-session-supervisor.py -- spendable
        # exactly once per owner lifetime.
        terminal_handoff_issued = [False]
        if recovered is not None and recovered.resource is not None and active_outbox is None:
            resource_prompt = RESOURCE_WAIT.wait(args, state_path, control, delivered, emit)
            if resource_prompt is not None:
                next_prompt = resource_prompt
        while True:
            if active_outbox is not None and active_outbox.receipt is not None:
                if delivery_timing["same_thread_resume_ns"] is None:
                    same_thread_resume_count += 1
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "same_thread_resume_ns"
                )
                resumed_receipt = dict(active_outbox.receipt)
                resumed_receipt["delivery_timing"] = delivery_timing
                next_prompt = completion_prompt(
                    resumed_receipt, active_outbox, jobs=args.jobs,
                    notice=pending_notice,
                )
            resource_state = read_supervisor_phase_state(state_path, args.parent_attempt_id)
            resource_box = (resource_state.resource or {}).get("outbox") if resource_state else None
            resource_receipt_id = ""
            if resource_box and active_outbox is None:
                next_prompt = RESOURCE_WAIT.pending_prompt(state_path, args.parent_attempt_id, args, control)
                resource_receipt_id = resource_box["receipt_id"]
            begin_supervisor_turn(
                state_path, args.parent_attempt_id, delivered,
                receipt_id=active_outbox.receipt_id if active_outbox is not None else None,
            )
            next_prompt = control.prepare(next_prompt)
            from foreground_activity import owner_turn_activity
            activity = owner_turn_activity(
                args, server.process, harness="codex",
                window_seconds=DECISIONS.OWNER_TURN_IDLE_SECONDS,
            )
            final_text, _final_item = run_turn(
                server, thread_id=thread_id, prompt=next_prompt, args=args,
                activity=activity,
            )
            pending_notice = ""
            DECISIONS.refresh_owner_route(args)
            rows = current_children(Path(args.jobs), args.parent_attempt_id,
                                    route_id=args.route_id, route_hash=args.route_hash)
            current = {row.attempt_id: row for row in rows}
            if resource_receipt_id:
                RESOURCE_WAIT.acknowledge(state_path, args.parent_attempt_id, resource_receipt_id)
            completed_delivery = False
            if active_outbox is not None:
                acknowledge_supervisor_delivery(
                    state_path, args.parent_attempt_id, active_outbox.receipt_id
                )
                observed_state = read_supervisor_phase_state(state_path, args.parent_attempt_id)
                active_outbox = observed_state.outbox if observed_state is not None else None
                if active_outbox is not None:
                    # A separately replaced outbox retains its own delivery; a
                    # stale acknowledgement cannot consume it or kill its owner.
                    verdict, notice = _admit_continuation(
                        ledger, budget_state_root,
                        parent_attempt_id=args.parent_attempt_id,
                        route_id=args.route_id, route_hash=args.route_hash,
                        ordinal=continuations, purpose="ordinary", stalled=False,
                        warning_threshold=args.continuation_warning_threshold,
                    )
                    if not verdict.admitted:
                        next_prompt = _seal_terminal_handoff_or_raise(
                            ledger, budget_state_root, args=args, ordinal=continuations,
                            failure_reason="continuation-limit-exceeded",
                            terminal_handoff_issued=terminal_handoff_issued,
                        )
                    else:
                        pending_notice = notice
                        next_prompt = completion_prompt(
                            active_outbox.receipt or {}, active_outbox, jobs=args.jobs, notice=notice,
                        )
                    continuations += 1
                    continue
                completed_delivery = True
            if control.pending():
                next_prompt = "Continue the same work using the pending user correction; preserve active children."
                continue
            new_attempts = set(current).difference(delivered)
            partition = partition_runtime_wait_children(
                Path(args.jobs), args.parent_attempt_id,
                [current[attempt] for attempt in new_attempts], new_attempts,
            )
            unstarted = set(partition.unstarted)
            # Only joinable rows may be parked; serial tails and refusal-
            # settled rows stay out of the outbox.
            park_attempts = set(partition.joinable)
            for frontier in partition.frontiers:
                emit({"type": "dispatch.supervisor.chain-pending", "parent_attempt_id": args.parent_attempt_id,
                      "chain_id": frontier.chain_id, "frontier_index": frontier.frontier_index,
                      "frontier_attempt_id": frontier.frontier_attempt_id,
                      "pending_count": len(frontier.pending_attempt_ids)})
            if completed_delivery and park_attempts and not unstarted:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "next_stage_start_ns"
                )
                emit({
                    "type": "dispatch.supervisor.owner-boundary",
                    "parent_attempt_id": args.parent_attempt_id,
                    "previous_attempt_ids": sorted(set(current).difference(new_attempts)),
                    "new_attempt_ids": sorted(new_attempts),
                    "previous_count": len(set(current).difference(new_attempts)),
                    "new_count": len(new_attempts),
                    "ordinal": 1,
                    **delivery_timing,
                })
            wait_requested = runtime_wait_requested(final_text)
            if cancel_unstarted_chain_successors(
                    Path(args.jobs), delivered, [current[attempt] for attempt in new_attempts],
                    final_text):
                current = {row.attempt_id: row for row in current_children(
                    Path(args.jobs), args.parent_attempt_id,
                    route_id=args.route_id, route_hash=args.route_hash)}
                new_attempts = set(current).difference(delivered)
                partition = partition_runtime_wait_children(
                    Path(args.jobs), args.parent_attempt_id,
                    [current[attempt] for attempt in new_attempts], new_attempts,
                )
                unstarted = set(partition.unstarted)
                park_attempts = set(partition.joinable)
            if unstarted or (wait_requested and not new_attempts):
                rows, settled = settle_runtime_wait_children(
                    Path(args.jobs), args.parent_attempt_id, delivered, join_interval=args.join_interval,
                    route_id=args.route_id, route_hash=args.route_hash)
                current = {row.attempt_id: row for row in rows}
                new_attempts = set(current).difference(delivered)
                partition = partition_runtime_wait_children(
                    Path(args.jobs), args.parent_attempt_id,
                    [current[attempt] for attempt in new_attempts], new_attempts,
                )
                unstarted = set(partition.unstarted)
                park_attempts = set(partition.joinable)
                if settled:
                    emit(
                        {
                            "type": "dispatch.supervisor.launch-settled",
                            "parent_attempt_id": args.parent_attempt_id,
                            "attempt_count": len(new_attempts),
                        }
                    )
            # A resource never makes an unstarted model leg joinable. Collect
            # actual model children first, and preserve their refusal semantics.
            if not park_attempts and not unstarted and not partition.chain_pending:
                resource_prompt = RESOURCE_WAIT.wait(args, state_path, control, delivered, emit)
                if resource_prompt is not None:
                    resource_state = read_supervisor_phase_state(state_path, args.parent_attempt_id)
                    if resource_state is not None and (resource_state.resource or {}).get("outbox"):
                        verdict, notice = _admit_continuation(
                            ledger, budget_state_root, parent_attempt_id=args.parent_attempt_id,
                            route_id=args.route_id, route_hash=args.route_hash,
                            ordinal=continuations, purpose="ordinary", stalled=False,
                            warning_threshold=args.continuation_warning_threshold)
                        if not verdict.admitted:
                            raise SupervisorError("continuation-limit-exceeded")
                        continuations += 1
                        resource_prompt = _apply_notice(resource_prompt, notice)
                    next_prompt = resource_prompt
                    continue
            empty_wait = (not new_attempts and wait_requested) or (
                wait_requested and not partition.joinable and not partition.chain_pending
                and bool(partition.refusal_settled)
            )
            # Refusal-settled rows are already terminal and never enter the
            # join. Without a wait request they are folded into this aggregate
            # bookkeeping; with one, the existing empty-wait correction stays
            # fail-closed.
            if not wait_requested and not park_attempts:
                delivered.update(partition.refusal_settled)
            # Collect work already admitted to the shared join before asking
            # for another launch. A pending sibling cannot preempt that duty
            # or make a repeated correction terminate the running children.
            if (unstarted or empty_wait) and not park_attempts:
                signature = tuple(sorted(unstarted))
                if signature in launch_remediated:
                    raise SupervisorError("runtime-wait-without-started-child")
                verdict, notice = _admit_continuation(
                    ledger, budget_state_root,
                    parent_attempt_id=args.parent_attempt_id,
                    route_id=args.route_id, route_hash=args.route_hash,
                    ordinal=continuations, purpose="ordinary", stalled=True,
                    warning_threshold=args.continuation_warning_threshold,
                )
                if not verdict.admitted:
                    raise SupervisorError("runtime-wait-without-started-child")
                launch_remediated.add(signature)
                emit(
                    {
                        "type": "dispatch.supervisor.resumed",
                        "parent_attempt_id": args.parent_attempt_id,
                        "state": "registration-required",
                        "attempt_count": len(unstarted),
                        "continuation_reason": "runtime-wait-without-started-child",
                        "continuation_ordinal": continuations + 1,
                    }
                )
                next_prompt = _apply_notice(start_retry_prompt(unstarted), notice)
                continuations += 1
                continue
            if park_attempts:
                # Cheap, non-mutating fail-fast: this round's real admission
                # (and its single budget spend) is committed once, at the R2
                # sealed site below, once the post-join purpose is knowable.
                if not (ledger.gross_remaining > ledger.reserved_remaining):
                    next_prompt = _seal_terminal_handoff_or_raise(
                        ledger, budget_state_root, args=args,
                        ordinal=continuations,
                        failure_reason="continuation-limit-exceeded",
                        terminal_handoff_issued=terminal_handoff_issued,
                    )
                    continuations += 1
                    continue
                emit(
                    {
                        "type": "dispatch.supervisor.parked",
                        "parent_attempt_id": args.parent_attempt_id,
                        "attempt_count": len(park_attempts),
                    }
                )
                write_supervisor_state(
                    state_path,
                    args.parent_attempt_id,
                    delivered,
                    phase="parked",
                )
                from dispatch_supervision import wait_for_batch
                from dispatch_replacement import advance_batch, adopt_receipt
                receipt = wait_for_batch(
                    join=lambda attempts: run_join(args, attempts),
                    attempts=set(park_attempts), jobs=Path(args.jobs),
                    parent_attempt_id=args.parent_attempt_id, emit=emit,
                    replacement_checkpoint=lambda selected: advance_batch(Path(args.jobs), selected)
                        if not control.pending() else (selected, [], []),
                )
                park_attempts, replaced_attempts = adopt_receipt(Path(args.jobs), set(park_attempts), receipt)
                delivered.update(replaced_attempts)
                joined_rows = current_children(
                    Path(args.jobs), args.parent_attempt_id, park_attempts
                )
                joined = {row.attempt_id: row for row in joined_rows}
                # SD-119: an unfinished serial sub-session chain advances
                # entirely in-process here -- zero model turns, zero
                # continuation spend -- until it completes (falls through
                # below using the LAST child's joined_rows/receipt) or this
                # round's join carries no chain metadata (no-op, byte-identical).
                # Claude-only realized behavior confirmed by measurement
                # (SD-OPEN-15): this Codex binding mirrors that surface but
                # claims no cross-harness parity.
                try:
                    drive = subsession_advance.drive_serial_chain(
                        jobs=Path(args.jobs), parent_attempt_id=args.parent_attempt_id,
                        attempts=set(park_attempts), receipt=receipt,
                        refresh=lambda attempts: current_children(Path(args.jobs), args.parent_attempt_id, attempts),
                        join=lambda attempts: run_join(args, attempts),
                        reconcile=lambda rows, attempts: runtime_reconcile(args, rows, attempts),
                        max_reparks=args.max_join_reparks,
                        allow_advance=lambda: not control.pending(),
                        emit=emit,
                    )
                except subsession_advance.ChainDriveError as exc:
                    # Keep Codex's terminal reason/classification identical to
                    # the Claude supervisor for bounded repark exhaustion.
                    raise SupervisorError(exc.reason) from exc
                receipt, joined_rows, new_attempts = drive.receipt, list(drive.joined_rows), set(drive.attempts)
                joined = {row.attempt_id: row for row in joined_rows}
                joined_before_chain_advance = drive.joined_before
                last_advanced_attempt_id = drive.last_advanced_attempt_id
                delivered.update(drive.traversed)
                delivered.update(drive.closed)
                delivered.update(partition.refusal_settled)
                chain_notice = subsession_advance.chain_delivery_notice(drive, joined_rows, Path(args.jobs))
                if runtime_reconcile(args, joined, set(new_attempts)):
                    receipt = wait_for_batch(join=lambda selected: run_join(args, selected),
                        attempts=set(new_attempts), jobs=Path(args.jobs),
                        parent_attempt_id=args.parent_attempt_id, emit=emit)
                    joined_rows = current_children(
                        Path(args.jobs), args.parent_attempt_id, new_attempts
                    )
                receipt = wait_for_batch(
                    join=lambda selected: receipt_with_current_actions(receipt,
                        current_children(Path(args.jobs), args.parent_attempt_id, selected), jobs=Path(args.jobs)),
                    attempts=set(new_attempts), jobs=Path(args.jobs), parent_attempt_id=args.parent_attempt_id, emit=emit)
                delivery_timing = validate_delivery_timing(
                    receipt["delivery_timing"]
                )
                current_rows = current_children(Path(args.jobs), args.parent_attempt_id)
                advanced_record = None if control.pending() else attempt_stage_advance(
                    args,
                    current_rows,
                    set(new_attempts),
                    delivery_timing,
                )
                # §13.32.1-(2)6/(3)B: same single negotiation decision as
                # claude-session-supervisor.py's symmetric call site --
                # `receipt_with_stage_advance` no-ops unless both `negotiated`
                # is true AND an outcome=="advanced" record was found.
                receipt = receipt_with_stage_advance(
                    receipt,
                    stage_advance_record=advanced_record,
                )
                emit(
                    {
                        "type": "dispatch.supervisor.resumed",
                        "parent_attempt_id": args.parent_attempt_id,
                        "state": receipt["state"],
                        "attempt_count": len(new_attempts),
                        "continuation_reason": "actionable-completion-receipt",
                        "continuation_ordinal": continuations + 1,
                    }
                )
                # SD-116 R2: terminal-handoff is sealed here and only here --
                # zero open/running route-bound children AND the ordinary
                # gross budget is already down to the reserve boundary
                # (D47-4).
                open_or_running = sum(1 for row in current_rows if row.status in {"open", "running"})
                gross_exhausted = ledger.gross_remaining <= ledger.reserved_remaining
                consumption_purpose = (
                    "terminal-handoff" if open_or_running == 0 and gross_exhausted else "ordinary"
                )
                verdict, notice = _admit_continuation(
                    ledger, budget_state_root,
                    parent_attempt_id=args.parent_attempt_id,
                    route_id=args.route_id, route_hash=args.route_hash,
                    ordinal=continuations, purpose=consumption_purpose, stalled=False,
                    warning_threshold=args.continuation_warning_threshold,
                )
                if not verdict.admitted:
                    # Sealed terminal-handoff site (R2 comment above): a
                    # refusal here already tried purpose="terminal-handoff"
                    # whenever the reserve boundary was reached.
                    raise SupervisorError("continuation-limit-exceeded")
                subsession_advance.record_owner_resume_if_chain(
                    Path(args.jobs), joined_before_chain_advance, last_advanced_attempt_id,
                )
                prepared = prepare_supervisor_outbox(
                    state_path,
                    args.parent_attempt_id,
                    delivered,
                    receipt,
                    joined_rows,
                )
                delivered = set(prepared.delivered_attempt_ids)
                active_outbox = prepared.outbox
                pending_notice = _apply_notice(chain_notice, notice) if chain_notice else notice
                next_prompt = completion_prompt(
                    active_outbox.receipt or {}, active_outbox, jobs=args.jobs,
                    notice=pending_notice,
                )
                continuations += 1
                continue

            # A terminal status word does not discharge remaining cleanup.
            # The common policy checks every owned attempt before finalization.
            owned_attempts = set(current)
            if owned_attempts:
                from dispatch_supervision import wait_for_child_settlement
                wait_for_child_settlement(
                    jobs=Path(args.jobs), attempts=owned_attempts,
                    parent_attempt_id=args.parent_attempt_id,
                    join=lambda attempts: run_join(args, attempts),
                    reconcile=lambda attempts: runtime_reconcile(
                        args, {row.attempt_id: row for row in current_children(
                            Path(args.jobs), args.parent_attempt_id, attempts)}, attempts),
                    emit=emit,
                )

            if not control.terminal_boundary():
                active_outbox = None
                next_prompt = "Address the pending user correction before finishing this same work."
                continue
            terminal = classify_codex_result(final_text)
            if terminal.failure_class == "pass":
                from dispatch_terminal_commit import owner_workflow_continuation
                correction = (owner_workflow_continuation(args.jobs, args.parent_attempt_id, args.route_file)
                              if args.route_file else None)
                if correction:
                    verdict, notice = _admit_continuation(
                        ledger, budget_state_root, parent_attempt_id=args.parent_attempt_id,
                        route_id=args.route_id, route_hash=args.route_hash,
                        ordinal=continuations, purpose="ordinary", stalled=True,
                        warning_threshold=args.continuation_warning_threshold)
                    if not verdict.admitted:
                        raise SupervisorError("workflow-completion-incomplete")
                    emit({"type": "dispatch.supervisor.resumed", "parent_attempt_id": args.parent_attempt_id,
                          "continuation_reason": "workflow-completion-incomplete", "continuation_ordinal": continuations + 1})
                    control.reopen()
                    next_prompt = _apply_notice(correction, notice)
                    continuations += 1
                    continue
            if delivery_timing["join_completed_ns"] is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "final_report_marker_ns"
                )
            if not reconcile(args, terminal):
                return 70
            # F-1: flush strictly AFTER this attempt's own terminal row commits.
            subsession_advance.flush_own_subsession_handoff(
                Path(args.jobs), args.route_id, args.parent_attempt_id,
            )
            if delivery_timing["join_completed_ns"] is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "owner_terminal_envelope_ns"
                )
            emit({"type": "turn.completed", "thread_id": thread_id})
            if delivery_timing["join_completed_ns"] is not None:
                emit(
                    {
                        "type": "dispatch.supervisor.delivery-timing",
                        "parent_attempt_id": args.parent_attempt_id,
                        "same_thread_resume_count": same_thread_resume_count,
                        **delivery_timing,
                    }
                )
            return 0 if terminal.failure_class == "pass" else 3
    except (DispatchContractError, JoinContractError, SupervisorError) as exc:
        lease_exit = (type(exc), exc, exc.__traceback__)
        if isinstance(exc, TurnFailed):
            # The structured payload (and its dispatch.supervisor.turn.failed
            # log row) was already emitted where it was raised -- classify it
            # with the same function the post-exit log reader will use, then
            # emit the plain error event every other SupervisorError gets.
            terminal = codex_turn_failure_terminal(exc.payload)
            if not reconcile(args, terminal):
                return 70
            emit({"type": "dispatch.supervisor.error", "reason": str(exc)})
            return 70
        reason = exc.reason if isinstance(exc, DispatchContractError) else str(exc)
        terminal = classify_supervisor_error("codex", reason)
        if not reconcile(args, terminal):
            return 70
        emit({"type": "dispatch.supervisor.error", "reason": reason})
        return 70
    except Exception as exc:  # fail closed without leaking protocol/model content
        lease_exit = (type(exc), exc, exc.__traceback__)
        terminal = classify_supervisor_error(
            "codex", f"supervisor-internal-{type(exc).__name__}"
        )
        if not reconcile(args, terminal):
            return 70
        emit(
            {
                "type": "dispatch.supervisor.error",
                "reason": f"supervisor-internal-{type(exc).__name__}",
            }
        )
        return 70
    finally:
        try:
            try:
                if control is not None:
                    control.close()
            finally:
                if server is not None:
                    server.close()
        finally:
            try:
                from dispatch_completion_join import supervisor_guarded_attempt_ids
                open_children = supervisor_guarded_attempt_ids(
                    current_children(Path(args.jobs), args.parent_attempt_id), active_outbox)
            except Exception:
                open_children = set()
            resource_recovery = RESOURCE_WAIT.needs_recovery(state_path, args.parent_attempt_id, lease_exit[0] is not None)
            try:
                if open_children or resource_recovery:
                    write_supervisor_state(
                        state_path,
                        args.parent_attempt_id,
                        delivered,
                        phase="recovery",
                        outbox=active_outbox,
                    )
                else:
                    write_supervisor_state(
                        state_path, args.parent_attempt_id, delivered, phase="terminal"
                    )
                    remove_supervisor_state(state_path)
            except Exception as exc:
                emit(
                    {
                        "type": "dispatch.supervisor.error",
                        "reason": f"supervisor-finalize-state-{type(exc).__name__}",
                    }
                )
            finally:
                if lease_acquired:
                    try:
                        lease.__exit__(
                            *(lease_exit if open_children or resource_recovery else (None, None, None))
                        )
                    except Exception as exc:
                        emit(
                            {
                                "type": "dispatch.supervisor.error",
                                "reason": f"supervisor-finalize-lease-{type(exc).__name__}",
                            }
                        )


if __name__ == "__main__":
    raise SystemExit(main())
