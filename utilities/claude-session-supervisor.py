#!/usr/bin/env python3
"""Resume one Claude Code print session after runtime-owned child joins."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any
import uuid

from dispatch_completion_join import (
    JoinContractError,
    SupervisorOutbox,
    consume_advance_completed_outbox,
    current_children,
    prepare_supervisor_outbox,
    refresh_supervisor_outbox_actions,
    reconcile_finished_children,
    read_supervisor_phase_state,
    receipt_with_current_actions,
    remove_supervisor_state,
    runtime_wait_requested,
    start_retry_prompt,
    unstarted_child_attempts,
    write_supervisor_state,
)
from dispatch_contract import DispatchContractError, hold_supervisor_lease
from dispatch_continuation_budget import (
    positive_continuation_limit,
    resolve_continuation_budget,
)
from dispatch_supervisor_terminal import (
    SupervisorTerminal,
    classify_claude_result,
    classify_supervisor_error,
    reconcile_supervisor_terminal,
)


ROOT = Path(__file__).resolve().parents[1]
SHARED_HARVEST_SURFACE = shlex.quote(
    str(ROOT / "adapters" / "codex" / "bin" / "preflight.sh")
)


class SupervisorError(RuntimeError):
    """The Claude session bridge could not preserve its completion contract."""


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


def typed_receipt(value: object, parent_attempt_id: str, attempts: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise SupervisorError("join-receipt-schema-invalid")
    if value.get("state") not in {"ready", "timeout"}:
        raise SupervisorError("join-receipt-state-invalid")
    if value.get("parent_attempt_id") != parent_attempt_id:
        raise SupervisorError("join-receipt-parent-mismatch")
    raw_children = value.get("children")
    if not isinstance(raw_children, list):
        raise SupervisorError("join-receipt-children-invalid")
    children: list[dict[str, str]] = []
    observed: set[str] = set()
    for raw in raw_children:
        if not isinstance(raw, dict):
            raise SupervisorError("join-receipt-child-invalid")
        attempt = raw.get("attempt_id")
        status = raw.get("status")
        readiness = raw.get("readiness")
        reason = raw.get("reason")
        required_action = raw.get("required_action")
        if (
            not isinstance(attempt, str)
            or attempt not in attempts
            or attempt in observed
            or status not in {"open", "running", "done"}
            or readiness not in {"ready", "pending"}
            or reason not in {
                "registry-closed",
                "terminal-observed",
                "process-alive",
                "process-unverifiable",
            }
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
    }


def run_join(args: argparse.Namespace, attempts: set[str]) -> dict[str, Any]:
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
    return typed_receipt(value, args.parent_attempt_id, attempts)


def completion_prompt(
    receipt: dict[str, Any], outbox: SupervisorOutbox | None = None
) -> str:
    # This command's route-bound success depends on the derived-evidence
    # fallback in adapters/codex/bin/dispatch-harvest.py /
    # adapters/opencode/bin/dispatch-harvest.py (`route_completion_evidence`,
    # round_1 finding 5). Do not silently revert that fallback — without it
    # this exact command is unsatisfiable for a route-bound row and the
    # delivered/harvest-only phase deadlocks (SD-70/78).
    compact = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
    commands: list[str] = []
    for child in receipt["children"]:
        attempt = shlex.quote(child["attempt_id"])
        if child["required_action"] == "complete-open":
            commands.append(
                f"{SHARED_HARVEST_SURFACE} harvest --attempt-id "
                f"{attempt} --status open --mark-done"
            )
        elif child["required_action"] == "inspect-done-failure":
            commands.append(
                f"{SHARED_HARVEST_SURFACE} harvest --attempt-id "
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
        "The absolute preflight path below is the shared, runtime-neutral registry "
        "harvest compatibility surface. It does not select or change the owner or "
        "child harness; a Claude owner must execute it literally. "
        "Harvest every listed exact attempt through the checked contract. Run only "
        "these exact commands, one at a time:\n"
        f"{command_text}\n"
        "Then advance "
        "the route, and register the next separable batch if required. Do not call "
        "dispatch-wait or inspect raw child logs. Emit the exact final three-line "
        "handoff only when no owned registered child remains open."
    )


def runtime_reconcile(args: argparse.Namespace, rows: dict[str, Any],
                      unresolved: set[str]) -> set[str]:
    """Close every unresolved child that its own evidence already proves done."""

    closed: set[str] = set()
    for attempt, reason in reconcile_finished_children(
        rows, unresolved, jobs=args.jobs
    ).items():
        emit(
            {
                "type": "dispatch.supervisor.reconciled",
                "parent_attempt_id": args.parent_attempt_id,
                "attempt_id": attempt,
                "outcome": "closed" if not reason else "skipped",
                **({} if not reason else {"reason": reason}),
            }
        )
        if not reason:
            closed.add(attempt)
    return closed


def remediation_prompt(attempts: set[str]) -> str:
    # Same route-bound-success dependency as completion_prompt() above.
    commands = "\n".join(
        f"{SHARED_HARVEST_SURFACE} harvest --attempt-id "
        f"{shlex.quote(attempt)} --mark-done"
        for attempt in sorted(attempts)
    )
    return (
        "Runtime completion contract violation: previously delivered exact attempt(s) "
        f"remain open: {','.join(sorted(attempts))}. The absolute preflight path is "
        "the shared, runtime-neutral registry harvest compatibility surface and does "
        "not change either harness. Run only these exact commands, "
        f"one at a time:\n{commands}\n"
        "Do not wait, poll, inspect raw logs, or do unrelated work."
    )


def claude_command(args: argparse.Namespace, session_id: str, resume: bool) -> list[str]:
    if args.claude_command:
        command = shlex.split(args.claude_command)
    else:
        command = ["claude"]
    command += ["-p"]
    command += ["--resume" if resume else "--session-id", session_id]
    hook_command = " ".join(
        shlex.quote(value)
        for value in (
            str(ROOT / "adapters" / "claude" / "hooks" / "registered-parent-park.py"),
        )
    )
    hook_settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": hook_command,
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }
    command += ["--settings", json.dumps(hook_settings, separators=(",", ":"))]
    for path in args.add_dir:
        command += ["--add-dir", path]
    command += ["--output-format", "stream-json", "--verbose"]
    if args.model:
        command += ["--model", args.model]
    if args.effort:
        command += ["--effort", args.effort]
    if args.disallowed_tool:
        command += ["--disallowedTools", ",".join(args.disallowed_tool)]
    return command


def run_turn(
    args: argparse.Namespace,
    session_id: str,
    prompt: str,
    *,
    resume: bool,
) -> tuple[dict[str, Any], int]:
    try:
        result = subprocess.run(
            claude_command(args, session_id, resume),
            cwd=args.worktree,
            env={
                **os.environ,
                **(
                    {"AGENT_DISPATCH_COMPLETION_STATE_FILE": args.state_file}
                    if args.state_file
                    else {}
                ),
            },
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=None,
            timeout=args.turn_timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SupervisorError("claude-turn-process-failed") from exc
    final: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") == "result":
            final = value
    if final is None:
        raise SupervisorError("claude-result-missing")
    return final, result.returncode


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--worktree", required=True)
    value.add_argument("--jobs", required=True)
    value.add_argument("--parent-attempt-id", required=True)
    value.add_argument("--add-dir", action="append", default=[])
    value.add_argument("--model")
    value.add_argument("--effort")
    value.add_argument("--disallowed-tool", action="append", default=[])
    value.add_argument("--join-interval", type=float, default=2.0)
    value.add_argument("--join-timeout", type=float, default=3600.0)
    value.add_argument("--max-join-reparks", type=int, default=6)
    value.add_argument("--turn-timeout", type=float, default=7200.0)
    value.add_argument("--max-continuations", type=positive_continuation_limit)
    value.add_argument("--route-file")
    value.add_argument("--route-id", default="")
    value.add_argument("--route-hash", default="")
    value.add_argument("--state-file", default=os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE"))
    value.add_argument("--lease-file", default=os.environ.get("AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"))
    value.add_argument("--claude-command", default=os.environ.get("CLAUDE_SESSION_COMMAND"))
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
    initial_prompt = sys.stdin.read()
    if not initial_prompt.strip():
        terminal = classify_supervisor_error("claude", "initial-prompt-empty", 64)
        if not reconcile(args, terminal):
            return 70
        emit({"type": "dispatch.supervisor.error", "reason": "initial-prompt-empty"})
        return 64
    session_id = str(uuid.uuid4())
    # This attempt log is a receipt log, never a transcript: it carries control rows
    # plus exactly one final `result`, and deliberately never echoes model text. A
    # summary producer reading only this file therefore has no conversational input
    # and can never name the run (observed 2026-08-04: every supervised owner rendered
    # with no title and no NOW line for its whole lifetime). Announce the child's own
    # session id — control metadata, not model content — so the summary owner can
    # follow the real transcript instead. Emitted before the first turn so the
    # follower has a source from the start rather than only at completion.
    emit(
        {
            "type": "dispatch.supervisor.session",
            "parent_attempt_id": args.parent_attempt_id,
            "session_id": session_id,
            "cwd": args.worktree,
        }
    )
    emit(
        {
            "type": "dispatch.supervisor.continuation-budget",
            "limit": continuation_budget.limit,
            "source": continuation_budget.source,
            "declared_nodes": continuation_budget.declared_nodes,
            "retry_slots": continuation_budget.retry_slots,
        }
    )
    state_path = Path(args.state_file) if args.state_file else None
    delivered: set[str] = set()
    active_outbox: SupervisorOutbox | None = None
    remediated: set[tuple[str, ...]] = set()
    launch_remediated: set[tuple[str, ...]] = set()
    next_prompt = initial_prompt
    continuations = 0
    resume = False
    lease = hold_supervisor_lease(
        args.jobs, args.parent_attempt_id, args.lease_file or ""
    )
    lease_acquired = False
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
                )
                active_outbox = refreshed.outbox
                next_prompt = completion_prompt(
                    active_outbox.receipt or {}, active_outbox
                )
        while True:
            if active_outbox is None:
                write_supervisor_state(
                    state_path, args.parent_attempt_id, delivered, phase="running-turn"
                )
            result, process_rc = run_turn(
                args, session_id, next_prompt, resume=resume
            )
            if (
                process_rc != 0
                or result.get("is_error") is True
                or result.get("subtype") not in {None, "success"}
            ):
                terminal = classify_claude_result(result, process_rc)
                if not reconcile(args, terminal):
                    return 70
                emit(result)
                return process_rc or 3
            rows = current_children(Path(args.jobs), args.parent_attempt_id)
            current = {row.attempt_id: row for row in rows}
            if active_outbox is not None:
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
                    if continuations >= args.max_continuations:
                        raise SupervisorError("continuation-limit-exceeded")
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
                    )
                    active_outbox = refreshed.outbox
                    next_prompt = completion_prompt(
                        active_outbox.receipt or {}, active_outbox
                    )
                    continuations += 1
                    resume = True
                    continue
            new_attempts = set(current).difference(delivered)
            unstarted = unstarted_child_attempts(
                [current[attempt] for attempt in new_attempts]
            )
            empty_wait = (
                not current
                and runtime_wait_requested(result.get("result"))
            )
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
                resume = True
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
                # receipt. Consuming it as delivered/actionable sends the model
                # harvest instructions for children that are still open, which
                # cascades into owned-children-remain-open-after-resume. Re-run
                # the bounded join in place — no model turn, no delivered update,
                # no continuation spend — until it resolves or the repark bound
                # trips.
                reparks = 0
                while True:
                    receipt = run_join(args, new_attempts)
                    if receipt["state"] != "timeout":
                        break
                    reparks += 1
                    if reparks > args.max_join_reparks:
                        raise SupervisorError("join-timeout-repark-exceeded")
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
                    receipt_with_current_actions(receipt, joined_rows),
                    joined_rows,
                )
                delivered = set(prepared.delivered_attempt_ids)
                active_outbox = prepared.outbox
                next_prompt = completion_prompt(
                    active_outbox.receipt or {}, active_outbox
                )
                continuations += 1
                resume = True
                continue

            unresolved = {
                attempt
                for attempt, row in current.items()
                if row.status in {"open", "running"}
            }
            if unresolved:
                # Evidence-backed closure first: a route-bound child that finished
                # without writing its own marker leaves the model with no legal
                # remediation (see runtime_close_child), so asking it again only
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
                resume = True
                continue

            terminal = classify_claude_result(result, process_rc)
            if not reconcile(args, terminal):
                return 70
            emit(result)
            return 0 if terminal.failure_class == "pass" else 3
    except (DispatchContractError, JoinContractError, SupervisorError) as exc:
        lease_exit = (type(exc), exc, exc.__traceback__)
        reason = exc.reason if isinstance(exc, DispatchContractError) else str(exc)
        terminal = classify_supervisor_error("claude", reason)
        if not reconcile(args, terminal):
            return 70
        emit({"type": "dispatch.supervisor.error", "reason": reason})
        return 70
    except Exception as exc:  # fail closed without leaking protocol/model content
        lease_exit = (type(exc), exc, exc.__traceback__)
        terminal = classify_supervisor_error(
            "claude", f"supervisor-internal-{type(exc).__name__}"
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
