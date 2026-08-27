#!/usr/bin/env python3
"""Resume one Codex App Server thread only after exact registered children settle."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

from dispatch_completion_join import (
    JoinContractError,
    SupervisorOutbox,
    advance_delivery_timing,
    classify_supervised_shell_command,
    classify_supervised_shell_command_reason,
    consume_advance_completed_outbox,
    close_wrapper_pass,
    current_children,
    delivery_timing_fields,
    exact_attempt_row,
    harvest_command_lines,
    prepare_supervisor_outbox,
    refresh_supervisor_outbox_actions,
    reconcile_finished_children,
    read_supervisor_phase_state,
    receipt_with_current_actions,
    receipt_with_delivery_observability,
    remove_supervisor_state,
    runtime_wait_requested,
    start_retry_prompt,
    supervisor_guarded_attempt_ids,
    supervisor_outbox_delivery_identity,
    supervisor_receipt_satisfiable,
    unstarted_child_attempts,
    validate_delivery_timing,
    write_supervisor_state,
)
from dispatch_contract import (
    AUTOMATIC_RECEIPTLESS_CLASSIFIER,
    DispatchContractError,
    hold_supervisor_lease,
)
from dispatch_continuation_budget import (
    positive_continuation_limit,
    resolve_continuation_budget,
)
from dispatch_supervisor_terminal import (
    SupervisorTerminal,
    classify_codex_result,
    classify_supervisor_abandonment_terminal,
    classify_supervisor_attention_terminal,
    classify_supervisor_error,
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


class SupervisorError(RuntimeError):
    """The runtime completion bridge could not preserve its contract."""


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False), flush=True)


def reconcile(args: argparse.Namespace, terminal: SupervisorTerminal) -> bool:
    try:
        reconcile_supervisor_terminal(
            args.jobs, args.parent_attempt_id, terminal
        )
        return True
    except Exception as exc:
        emit(
            {
                "type": "dispatch.supervisor.error",
                "reason": f"terminal-reconcile-failed-{type(exc).__name__}",
            }
        )
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


def _usage_counter(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
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


def _typed_receipt(value: Any, parent_attempt_id: str, attempts: set[str]) -> dict[str, Any]:
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
    allowed_reasons = {
        "registry-closed",
        "terminal-observed",
        "process-alive",
        "process-unverifiable",
    }
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
            or reason not in allowed_reasons
            or status not in {"open", "running", "done"}
            or required_action not in {
                "complete-open", "inspect-done-failure", "advance-completed"
            }
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
    return {
        "schema_version": 2,
        "state": value["state"],
        "parent_attempt_id": parent_attempt_id,
        "children": children,
        "delivery_timing": value.get("delivery_timing", delivery_timing_fields()),
    }


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
    ]
    for attempt in sorted(attempts):
        command += ["--attempt-id", attempt]
    try:
        result = subprocess.run(
            command,
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
        raise SupervisorError("join-process-contract-failed")
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
    observed = receipt_with_delivery_observability(
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


def settle_runtime_wait_children(
    args: argparse.Namespace, delivered: set[str]
) -> tuple[list[Any], bool]:
    """Reread the exact registry through the register-to-start publication race.

    Atomic reservation can become visible just before the fenced wrapper appends
    ``launch_started=1``. Treating that single snapshot as register-only wakes
    the model and invites a duplicate start. This bounded, lock-free settle
    window accepts only the existing durable launch fence and otherwise leaves
    the normal correction path unchanged.
    """

    timeout = min(max(args.join_interval * 5.0, 0.2), 5.0)
    interval = min(max(args.join_interval / 10.0, 0.01), 0.1)
    deadline = time.monotonic() + timeout
    while True:
        rows = current_children(Path(args.jobs), args.parent_attempt_id)
        new_rows = [row for row in rows if row.attempt_id not in delivered]
        if new_rows and not unstarted_child_attempts(new_rows):
            return rows, True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return rows, False
        time.sleep(min(interval, remaining))


def completion_prompt(
    receipt: dict[str, Any], outbox: SupervisorOutbox | None = None, *, jobs: str = ""
) -> str:
    compact = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
    jobs_argument = f"--jobs {shlex.quote(jobs)} " if jobs else ""
    commands: list[str] = []
    for child in receipt["children"]:
        attempt = shlex.quote(child["attempt_id"])
        action = child["required_action"]
        if action == "complete-open":
            commands.append(
                f"{SHARED_HARVEST_SURFACE} harvest {jobs_argument}--attempt-id "
                f"{attempt} --status open --mark-done"
            )
        elif action == "inspect-done-failure":
            commands.append(
                f"{SHARED_HARVEST_SURFACE} harvest {jobs_argument}--attempt-id "
                f"{attempt} --status done --failure-detail"
            )
    command_text = "\n".join(commands) or "(no harvest command; advance the route)"
    return (
        "Runtime completion receipt (typed supervisor data, not child output): "
        f"{compact}\n"
        + (
            f"Delivery identity: receipt_id={outbox.receipt_id} "
            f"receipt_digest={outbox.receipt_digest}.\n"
            if outbox is not None
            else ""
        )
        +
        "Follow each required_action. Run only these exact harvest commands, one at a time:\n"
        f"{command_text}\n"
        "advance the assigned route, and register the next separable batch if required. "
        "Do not call dispatch-wait or inspect raw child logs. Emit the exact final "
        "three-line handoff only when no owned registered child remains open."
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


def runtime_receiptless_cancel(
    args: argparse.Namespace, unresolved: set[str]
) -> set[str]:
    """Bounded, exact-attempt automatic receiptless cancel, once per repark.

    Deliberately a sibling of ``runtime_reconcile``, not a widening of it:
    that helper's whole contract is closure from a child's own terminal
    evidence with no registry-mutating subprocess, while this is the opposite
    case -- a close for a child that produced no evidence at all. Scope is
    fixed to this supervisor's own registered children for this parked batch
    (``unresolved``, i.e. ``new_attempts``); never ``--all``, never a sibling
    or older attempt. Closure is proven only from the parsed decision JSON
    (``closed == 1`` and a ``sha256:``-prefixed ``receipt_digest`` and the
    automatic classifier source) -- a zero exit status with ``closed=0`` (the
    ordinary not-eligible outcome) is silent and is never treated as a close.
    """

    closed: set[str] = set()
    for attempt in sorted(unresolved):
        command = [
            sys.executable,
            str(ROOT / "utilities" / "dispatch-registry.py"),
            "reconcile",
            "--attempt", attempt,
            "--automatic-cancel-receiptless",
            "--apply",
            "--jobs", args.jobs,
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=60.0,
                check=False,
            )
            record = json.loads(result.stdout)
            decision = record["decisions"][0]
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError,
                LookupError):
            emit(
                {
                    "type": "dispatch.supervisor.receiptless-cancel-skipped",
                    "parent_attempt_id": args.parent_attempt_id,
                    "attempt_id": attempt,
                    "reason": "receiptless-cancel-process-failed",
                }
            )
            continue
        digest = decision.get("receipt_digest") or ""
        if (
            result.returncode == 0
            and decision.get("closed") == 1
            and digest.startswith("sha256:")
            and record.get("classifier_source") == AUTOMATIC_RECEIPTLESS_CLASSIFIER
        ):
            closed.add(attempt)
            emit(
                {
                    "type": "dispatch.supervisor.receiptless-cancelled",
                    "parent_attempt_id": args.parent_attempt_id,
                    "attempt_id": attempt,
                    "receipt_digest": digest,
                    "classifier_source": AUTOMATIC_RECEIPTLESS_CLASSIFIER,
                }
            )
        elif result.returncode != 0 or decision.get("closed") != 0:
            emit(
                {
                    "type": "dispatch.supervisor.receiptless-cancel-skipped",
                    "parent_attempt_id": args.parent_attempt_id,
                    "attempt_id": attempt,
                    "reason": decision.get("reason") or "receiptless-cancel-process-failed",
                }
            )
        # An ordinary closed=0 not-eligible outcome is silent: no event.
    return closed


def receipt_satisfiability(
    args: argparse.Namespace, prompt: str, outbox: SupervisorOutbox | None
) -> tuple[bool, str]:
    """D2a, mirrored from claude-session-supervisor.py: prove every command a
    prompt prescribes is admitted by the park guard before it is delivered.

    The verdict is ``supervisor_receipt_satisfiable``'s alone; the second
    traversal only names the category for the emitted event. A prompt that
    prescribes no command is vacuously satisfiable.
    """

    lines = harvest_command_lines(prompt)
    if not lines:
        return True, ""
    classifier_args: dict[str, Any] = {
        "base": Path(args.worktree),
        "open_attempt_ids": supervisor_guarded_attempt_ids(
            current_children(Path(args.jobs), args.parent_attempt_id), outbox
        ),
        "parent_slug": os.environ.get("AGENT_DISPATCH_SELF_SLUG", ""),
        "jobs": Path(args.jobs),
        "parent_attempt_id": args.parent_attempt_id,
        "route_file": Path(args.route_file) if args.route_file else None,
        "route_id": args.route_id,
    }
    satisfiable, reason = supervisor_receipt_satisfiable(lines, **classifier_args)
    if satisfiable:
        return True, ""
    failing = next(
        (
            line
            for line in lines
            if classify_supervised_shell_command(command=line, **classifier_args)
            is None
        ),
        "",
    )
    if not failing:
        return False, reason
    return False, classify_supervised_shell_command_reason(
        command=failing, **classifier_args
    )


def seal_receipt_unsatisfiable(args: argparse.Namespace, reason: str) -> int:
    """D2a terminal: supervisor and park guard disagree on the vocabulary."""

    emit(
        {
            "type": "dispatch.supervisor.receipt-unsatisfiable",
            "parent_attempt_id": args.parent_attempt_id,
            "reason": reason,
        }
    )
    detail = f"receipt-unsatisfiable:{reason}"
    if not reconcile(args, classify_supervisor_attention_terminal("codex", detail)):
        return 70
    emit(
        {
            "type": "dispatch.supervisor.redelivery-suppressed",
            "parent_attempt_id": args.parent_attempt_id,
            "resolution": "receipt-unsatisfiable",
            "reason": reason,
        }
    )
    return 70


def seal_redelivery_abandoned(args: argparse.Namespace, identical: int) -> int:
    """D2b terminal: proven satisfiable, and the owner did not act.

    Closes the owner attempt row only; it publishes no completion marker, so
    the route node stays incomplete and SD-106 same-node redispatch remains
    available.
    """

    detail = f"identical-redelivery-bound:{identical}"
    emit(
        {
            "type": "dispatch.supervisor.redelivery-suppressed",
            "parent_attempt_id": args.parent_attempt_id,
            "resolution": "identical-redelivery-bound",
            "identical_redeliveries": identical,
        }
    )
    if not reconcile(args, classify_supervisor_abandonment_terminal("codex", detail)):
        return 70
    emit({"type": "dispatch.supervisor.error", "reason": detail})
    return 70


def remediation_prompt(attempts: set[str]) -> str:
    joined = ",".join(sorted(attempts))
    return (
        "Runtime completion contract violation: previously delivered exact attempt(s) "
        f"remain open: {joined}. Perform typed exact-attempt harvest/closure now. "
        "Do not wait, poll, inspect raw logs, or perform unrelated work."
    )


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

    def send(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise SupervisorError("app-server-stdin-closed")
        self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise SupervisorError("app-server-stdout-closed")
        line = self.process.stdout.readline()
        if not line:
            raise SupervisorError("app-server-eof")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise SupervisorError("app-server-protocol-json-invalid") from exc
        if not isinstance(value, dict):
            raise SupervisorError("app-server-protocol-shape-invalid")
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


def run_turn(
    server: AppServer,
    *,
    thread_id: str,
    prompt: str,
    args: argparse.Namespace,
) -> tuple[str | None, dict[str, Any] | None]:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt, "text_elements": []}],
        "cwd": args.worktree,
        "sandboxPolicy": sandbox_policy(args),
    }
    if args.approval != "inherit":
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
    emit({"type": "dispatch.supervisor.turn.started", "turn_id": turn_id})
    final_text: str | None = None
    final_item: dict[str, Any] | None = None
    while True:
        event = server.next_event()
        if "id" in event and "method" in event:
            raise SupervisorError("app-server-unexpected-request")
        method = event.get("method")
        raw_params = event.get("params")
        event_params = raw_params if isinstance(raw_params, dict) else {}
        if (method == "thread/tokenUsage/updated"
                and event_params.get("threadId") == thread_id):
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
            if not isinstance(completed, dict) or completed.get("status") != "completed":
                raise SupervisorError("app-server-turn-failed")
            return final_text, final_item


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--worktree", required=True)
    value.add_argument("--jobs", required=True)
    value.add_argument("--parent-attempt-id", required=True)
    value.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"), required=True)
    value.add_argument("--approval", choices=("untrusted", "on-request", "never", "inherit"), default="never")
    value.add_argument("--network-access", action="store_true")
    value.add_argument("--writable-root", action="append", default=[])
    value.add_argument("--model")
    value.add_argument("--reasoning")
    value.add_argument("--join-interval", type=float, default=2.0)
    value.add_argument("--join-timeout", type=float, default=3600.0)
    value.add_argument("--max-join-reparks", type=int, default=6)
    value.add_argument("--max-identical-redeliveries", type=int, default=2)
    value.add_argument("--max-continuations", type=positive_continuation_limit)
    value.add_argument("--route-file")
    value.add_argument("--route-id", default="")
    value.add_argument("--route-hash", default="")
    value.add_argument("--state-file", default=os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE"))
    value.add_argument("--lease-file", required=True)
    value.add_argument("--app-server-command", default=os.environ.get("CODEX_APP_SERVER_COMMAND"))
    value.add_argument("--join-command", default=os.environ.get("AGENT_DISPATCH_JOIN_COMMAND"))
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    continuation_budget = resolve_continuation_budget(
        explicit=args.max_continuations,
        route_file=args.route_file,
        route_id=args.route_id,
        route_hash=args.route_hash,
        expected_cwd=args.worktree,
    )
    args.max_continuations = continuation_budget.limit
    args.max_join_reparks = max(1, args.max_join_reparks)
    emit(
        {
            "type": "dispatch.supervisor.continuation-budget",
            "limit": continuation_budget.limit,
            "source": continuation_budget.source,
            "declared_nodes": continuation_budget.declared_nodes,
            "retry_slots": continuation_budget.retry_slots,
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
    state_path = Path(args.state_file) if args.state_file else None
    lease_path = Path(args.lease_file)
    runtime_env = dict(os.environ)
    if state_path is not None:
        runtime_env["AGENT_DISPATCH_COMPLETION_STATE_FILE"] = str(state_path)
    runtime_env["AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"] = str(lease_path)
    server: AppServer | None = None
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
                refreshed = refresh_supervisor_outbox_actions(
                    state_path,
                    args.parent_attempt_id,
                    current_children(
                        Path(args.jobs),
                        args.parent_attempt_id,
                        set(active_outbox.attempt_ids),
                    ),
                    jobs=Path(args.jobs),
                )
                active_outbox = refreshed.outbox
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
                "capabilities": None,
            },
        )
        server.notification("initialized")
        thread_params: dict[str, Any] = {
            "cwd": args.worktree,
            "sandbox": args.sandbox,
            "ephemeral": True,
        }
        if args.approval != "inherit":
            thread_params["approvalPolicy"] = args.approval
        if args.model:
            thread_params["model"] = args.model
        thread_result = server.request("thread/start", thread_params)
        thread = thread_result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise SupervisorError("thread-start-response-invalid")
        thread_id = thread["id"]

        remediated: set[tuple[str, ...]] = set()
        launch_remediated: set[tuple[str, ...]] = set()
        next_prompt = prompt
        continuations = 0
        last_delivery_identity: tuple[str, tuple[tuple[str, str], ...]] = ("", ())
        identical_redeliveries = 0
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
                    resumed_receipt, active_outbox, jobs=args.jobs
                )
            if active_outbox is None:
                write_supervisor_state(
                    state_path, args.parent_attempt_id, delivered, phase="running-turn"
                )
            # D2a, one choke point for every producer -- see the matching
            # comment in claude-session-supervisor.py.
            satisfiable, unsatisfiable_reason = receipt_satisfiability(
                args, next_prompt, active_outbox
            )
            if not satisfiable:
                return seal_receipt_unsatisfiable(args, unsatisfiable_reason)
            last_delivery_identity = supervisor_outbox_delivery_identity(active_outbox)
            final_text, _final_item = run_turn(
                server, thread_id=thread_id, prompt=next_prompt, args=args
            )
            rows = current_children(Path(args.jobs), args.parent_attempt_id)
            current = {row.attempt_id: row for row in rows}
            completed_delivery = False
            if active_outbox is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "exact_harvest_ns"
                )
                consume_advance_completed_outbox(
                    state_path, args.parent_attempt_id, rows
                )
                observed_state = read_supervisor_phase_state(
                    state_path, args.parent_attempt_id
                )
                active_outbox = (
                    observed_state.outbox
                    if observed_state is not None
                    else None
                )
                if active_outbox is not None:
                    if active_outbox.receipt is None:
                        raise SupervisorError("supervisor-outbox-receipt-missing")
                    outbox_attempts = set(active_outbox.attempt_ids)
                    active_outbox = refresh_supervisor_outbox_actions(
                        state_path,
                        args.parent_attempt_id,
                        current_children(
                            Path(args.jobs),
                            args.parent_attempt_id,
                            outbox_attempts,
                        ),
                        jobs=Path(args.jobs),
                    ).outbox
                    # D2b, mirrored from claude-session-supervisor.py.
                    if (
                        supervisor_outbox_delivery_identity(active_outbox)
                        == last_delivery_identity
                    ):
                        runtime_reconcile(
                            args,
                            {
                                row.attempt_id: row
                                for row in current_children(
                                    Path(args.jobs),
                                    args.parent_attempt_id,
                                    outbox_attempts,
                                )
                            },
                            outbox_attempts,
                        )
                        run_join(args, outbox_attempts)
                        active_outbox = refresh_supervisor_outbox_actions(
                            state_path,
                            args.parent_attempt_id,
                            current_children(
                                Path(args.jobs),
                                args.parent_attempt_id,
                                outbox_attempts,
                            ),
                            jobs=Path(args.jobs),
                        ).outbox
                        if (
                            supervisor_outbox_delivery_identity(active_outbox)
                            != last_delivery_identity
                        ):
                            emit(
                                {
                                    "type": "dispatch.supervisor.redelivery-suppressed",
                                    "parent_attempt_id": args.parent_attempt_id,
                                    "resolution": "row-advanced",
                                    "identical_redeliveries": identical_redeliveries,
                                }
                            )
                            identical_redeliveries = 0
                        else:
                            identical_redeliveries += 1
                            if identical_redeliveries > args.max_identical_redeliveries:
                                return seal_redelivery_abandoned(
                                    args, identical_redeliveries
                                )
                    else:
                        identical_redeliveries = 0
                    if continuations >= args.max_continuations:
                        raise SupervisorError("continuation-limit-exceeded")
                    next_prompt = completion_prompt(
                        active_outbox.receipt or {}, active_outbox, jobs=args.jobs
                    )
                    continuations += 1
                    continue
                completed_delivery = True
            new_attempts = set(current).difference(delivered)
            unstarted = unstarted_child_attempts(
                [current[attempt] for attempt in new_attempts]
            )
            if completed_delivery and new_attempts and not unstarted:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "next_stage_start_ns"
                )
            wait_requested = runtime_wait_requested(final_text)
            if wait_requested and (not new_attempts or unstarted):
                rows, settled = settle_runtime_wait_children(args, delivered)
                current = {row.attempt_id: row for row in rows}
                new_attempts = set(current).difference(delivered)
                unstarted = unstarted_child_attempts(
                    [current[attempt] for attempt in new_attempts]
                )
                if settled:
                    emit(
                        {
                            "type": "dispatch.supervisor.launch-settled",
                            "parent_attempt_id": args.parent_attempt_id,
                            "attempt_count": len(new_attempts),
                        }
                    )
            empty_wait = not new_attempts and wait_requested
            if unstarted or empty_wait:
                signature = tuple(sorted(unstarted))
                if signature in launch_remediated or continuations >= args.max_continuations:
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
                next_prompt = start_retry_prompt(unstarted)
                continuations += 1
                continue
            if new_attempts:
                if continuations >= args.max_continuations:
                    raise SupervisorError("continuation-limit-exceeded")
                emit(
                    {
                        "type": "dispatch.supervisor.parked",
                        "parent_attempt_id": args.parent_attempt_id,
                        "attempt_count": len(new_attempts),
                    }
                )
                write_supervisor_state(
                    state_path,
                    args.parent_attempt_id,
                    delivered,
                    phase="parked",
                )
                # D-1 (owner-supervisor-liveness S-1): a `timeout` join receipt is
                # an internal repark checkpoint, not an actionable attention
                # receipt. See the matching comment in claude-session-supervisor.py.
                reparks = 0
                while True:
                    receipt = run_join(args, new_attempts)
                    if receipt["state"] != "timeout":
                        break
                    reparks += 1
                    if reparks > args.max_join_reparks:
                        raise SupervisorError("join-timeout-repark-exceeded")
                    runtime_receiptless_cancel(args, set(new_attempts))
                    emit(
                        {
                            "type": "dispatch.supervisor.reparked",
                            "parent_attempt_id": args.parent_attempt_id,
                            "attempt_count": len(new_attempts),
                            "repark_ordinal": reparks,
                        }
                    )
                joined_rows = current_children(
                    Path(args.jobs), args.parent_attempt_id, new_attempts
                )
                joined = {row.attempt_id: row for row in joined_rows}
                if runtime_reconcile(args, joined, set(new_attempts)):
                    receipt = run_join(args, new_attempts)
                    joined_rows = current_children(
                        Path(args.jobs), args.parent_attempt_id, new_attempts
                    )
                delivery_timing = validate_delivery_timing(
                    receipt["delivery_timing"]
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
                prepared = prepare_supervisor_outbox(
                    state_path,
                    args.parent_attempt_id,
                    delivered,
                    receipt_with_current_actions(
                        receipt, joined_rows, jobs=Path(args.jobs)
                    ),
                    joined_rows,
                )
                delivered = set(prepared.delivered_attempt_ids)
                active_outbox = prepared.outbox
                next_prompt = completion_prompt(
                    active_outbox.receipt or {}, active_outbox, jobs=args.jobs
                )
                continuations += 1
                continue

            unresolved = {
                attempt
                for attempt, row in current.items()
                if row.status in {"open", "running"}
            }
            if unresolved:
                # Evidence-backed closure first: a route-bound child that finished
                # without writing its own marker leaves the model with no legal
                # remediation (see close_finished_child), so asking it again only
                # burns a continuation before the same deadlock.
                closed = runtime_reconcile(args, current, unresolved)
                if closed:
                    unresolved -= closed
                    if not unresolved:
                        rows = current_children(Path(args.jobs), args.parent_attempt_id)
                        current = {row.attempt_id: row for row in rows}
                        continue
                signature = tuple(sorted(unresolved))
                if signature in remediated or continuations >= args.max_continuations:
                    raise SupervisorError("owned-children-remain-open-after-resume")
                remediated.add(signature)
                next_prompt = remediation_prompt(unresolved)
                continuations += 1
                continue

            if delivery_timing["join_completed_ns"] is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "final_report_marker_ns"
                )
            terminal = classify_codex_result(final_text)
            if not reconcile(args, terminal):
                return 70
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
            if server is not None:
                server.close()
        finally:
            try:
                open_children = {
                    row.attempt_id
                    for row in current_children(Path(args.jobs), args.parent_attempt_id)
                    if row.status in {"open", "running"}
                }
            except Exception:
                open_children = set()
            try:
                if open_children:
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
                            *(lease_exit if open_children else (None, None, None))
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
