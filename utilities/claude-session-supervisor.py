#!/usr/bin/env python3
"""Resume one Claude Code print session after runtime-owned child joins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import subprocess
import sys
import time
from typing import Any
import uuid

from dispatch_completion_join import (
    JoinContractError,
    SupervisorOutbox,
    advance_delivery_timing,
    classify_supervised_shell_command,
    classify_supervised_shell_command_reason,
    consume_advance_completed_outbox,
    current_children,
    delivery_timing_fields,
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
from dispatch_contract import DispatchContractError, hold_supervisor_lease
from dispatch_continuation_budget import (
    positive_continuation_limit,
    resolve_continuation_budget,
)
from dispatch_supervisor_terminal import (
    SupervisorTerminal,
    classify_claude_result,
    classify_supervisor_abandonment_terminal,
    classify_supervisor_attention_terminal,
    classify_supervisor_error,
    reconcile_supervisor_terminal,
)


ROOT = Path(__file__).resolve().parents[1]
# Deliberately UNRESOLVED. The parent session's park guard admits a contract
# path only under a harness root it recognizes -- its own root, a valid
# AGENT_HOME, or a cwd ancestor -- and it resolves both sides at check time.
# Resolving here instead pins the receipt to the versioned release directory
# behind a managed `current` pointer, so a release rotation between this
# supervisor's launch and the owner's harvest leaves every delivered command
# denied and the owner deadlocked (2026-08-14 runtime defect candidate 3).
# Keeping the launch path means an unrotated install emits exactly the same
# string as before, while a rotated one re-resolves to the live release.
def harvest_surface(launch_file: str) -> str:
    """Shared harvest CLI path as seen from THIS supervisor's launch path."""

    return shlex.quote(
        str(
            Path(launch_file).absolute().parents[1]
            / "adapters" / "codex" / "bin" / "preflight.sh"
        )
    )


SHARED_HARVEST_SURFACE = harvest_surface(__file__)


class SupervisorError(RuntimeError):
    """The Claude session bridge could not preserve its completion contract."""


def terminal_route_completion(
    args: argparse.Namespace, rows: list[object]
) -> tuple[str, ...]:
    """Return freshly proven terminal nodes, or an empty tuple.

    This only authorizes skipping the owner's final harvest turn when the bound
    route is still sealed and every declared terminal node has a current
    successful row backed by its exact completion marker.
    """

    if not args.route_file or not args.route_id or not args.route_hash:
        return ()
    route_path = Path(args.route_file)
    try:
        if route_path.is_symlink() or route_path.stat().st_size > 1_048_576:
            return ()
        route = json.loads(route_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return ()
    if not isinstance(route, dict) or route.get("schema_version") != 2:
        return ()
    bare = {
        key: value for key, value in route.items()
        if key not in {"route_hash", "route_id"}
    }
    sealed_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            bare, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if (
        route.get("route_id") != args.route_id
        or route.get("route_hash") != args.route_hash
        or sealed_hash != args.route_hash
        or args.route_id != "rt-" + sealed_hash.split(":", 1)[1][:16]
    ):
        return ()
    contract = route.get("workflow_contract")
    raw_terminal = contract.get("terminal_nodes") if isinstance(contract, dict) else None
    if not isinstance(raw_terminal, list) or not raw_terminal:
        return ()
    if any(not isinstance(node, str) or not node for node in raw_terminal):
        return ()
    terminal_nodes = tuple(sorted(raw_terminal))
    route_nodes = route.get("nodes")
    if not isinstance(route_nodes, list):
        return ()
    declared_terminal = tuple(
        sorted(
            node.get("id")
            for node in route_nodes
            if isinstance(node, dict)
            and node.get("terminal") is True
            and isinstance(node.get("id"), str)
        )
    )
    if declared_terminal != terminal_nodes:
        return ()
    if any(getattr(row, "status", "") in {"open", "running"} for row in rows):
        return ()

    proven: set[str] = set()
    for row in rows:
        metadata = getattr(row, "metadata", {})
        node = metadata.get("route_node", "")
        if node not in terminal_nodes:
            continue
        if (
            getattr(row, "status", "") != "done"
            or not (
                metadata.get("failure_class") == "pass"
                or metadata.get("note") in {"completed-marker", "completed-supervisor"}
            )
            or metadata.get("route_id") != args.route_id
            or metadata.get("route_hash") != args.route_hash
        ):
            continue
        marker_path = Path(metadata.get("completion_marker", ""))
        try:
            if (
                not marker_path.is_absolute()
                or marker_path.is_symlink()
                or not marker_path.is_file()
                or marker_path.stat().st_size > 65_536
            ):
                continue
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
        if (
            isinstance(marker, dict)
            and marker.get("schema_version") == 2
            and marker.get("route_id") == args.route_id
            and marker.get("route_hash") == args.route_hash
            and marker.get("node_id") == node
            and marker.get("attempt_id") == getattr(row, "attempt_id", "")
        ):
            proven.add(node)
    return terminal_nodes if proven == set(terminal_nodes) else ()


def terminal_handoff_result(
    result: dict[str, Any], rows: list[object], terminal_nodes: tuple[str, ...]
) -> dict[str, Any]:
    """Build the standard PASS envelope without another model turn."""

    artifacts: list[str] = []
    for row in rows:
        metadata = getattr(row, "metadata", {})
        if metadata.get("route_node") not in terminal_nodes:
            continue
        marker_path = Path(metadata.get("completion_marker", ""))
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            continue
        evidence = marker.get("evidence") if isinstance(marker, dict) else None
        artifact = evidence.get("path") if isinstance(evidence, dict) else None
        if (
            isinstance(artifact, str)
            and Path(artifact).is_absolute()
            and not Path(artifact).is_symlink()
            and Path(artifact).is_file()
        ):
            artifacts.append(artifact)
    artifact = sorted(set(artifacts))[0] if artifacts else "-"
    terminal_result = dict(result)
    terminal_result["result"] = f"artifact: {artifact}\nverdict: PASS\nblocker: none"
    return terminal_result


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
        "delivery_timing": value.get("delivery_timing", delivery_timing_fields()),
    }


def run_join(args: argparse.Namespace, attempts: set[str]) -> dict[str, Any]:
    started_ns = time.monotonic_ns()
    emit(
        {
            "type": "dispatch.supervisor.join-started",
            "parent_attempt_id": args.parent_attempt_id,
            "attempt_count": len(attempts),
            "monotonic_ns": started_ns,
        }
    )
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
    receipt = typed_receipt(value, args.parent_attempt_id, attempts)
    completed_ns = time.monotonic_ns()
    join_timing = validate_delivery_timing(receipt["delivery_timing"])
    if join_timing["last_child_terminal_ns"] is None:
        join_timing = advance_delivery_timing(
            join_timing, "last_child_terminal_ns", at_ns=completed_ns
        )
    join_timing = advance_delivery_timing(
        join_timing, "join_completed_ns", at_ns=completed_ns
    )
    receipt = receipt_with_delivery_observability(
        receipt,
        jobs=Path(args.jobs),
        timing=join_timing,
    )
    emit(
        {
            "type": "dispatch.supervisor.join-completed",
            "parent_attempt_id": args.parent_attempt_id,
            "attempt_count": len(attempts),
            "state": receipt["state"],
            "monotonic_ns": completed_ns,
            "duration_seconds": round((completed_ns - started_ns) / 1_000_000_000, 3),
            **receipt["delivery_timing"],
        }
    )
    return receipt


def completion_prompt(
    receipt: dict[str, Any], outbox: SupervisorOutbox | None = None, *, jobs: str = ""
) -> str:
    # This command's route-bound success depends on the derived-evidence
    # fallback in adapters/codex/bin/dispatch-harvest.py /
    # adapters/opencode/bin/dispatch-harvest.py (`route_completion_evidence`,
    # round_1 finding 5). Do not silently revert that fallback — without it
    # this exact command is unsatisfiable for a route-bound row and the
    # delivered/harvest-only phase deadlocks (SD-70/78).
    compact = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
    jobs_argument = f"--jobs {shlex.quote(jobs)} " if jobs else ""
    commands: list[str] = []
    for child in receipt["children"]:
        attempt = shlex.quote(child["attempt_id"])
        if child["required_action"] == "complete-open":
            commands.append(
                f"{SHARED_HARVEST_SURFACE} harvest {jobs_argument}--attempt-id "
                f"{attempt} --status open --mark-done"
            )
        elif child["required_action"] == "inspect-done-failure":
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


def receipt_satisfiability(
    args: argparse.Namespace, prompt: str, outbox: SupervisorOutbox | None
) -> tuple[bool, str]:
    """D2a: prove every command a prompt prescribes is admitted, before delivery.

    The verdict is ``supervisor_receipt_satisfiable``'s alone -- one traversal
    of the real ``classify_supervised_shell_command``, with the guarded set
    derived by the same helper the park hook uses. The second traversal only
    names the category for the emitted event; it can never flip the verdict.
    A prompt that prescribes no command (the initial brief, a start retry) is
    vacuously satisfiable.
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
    """D2a terminal: no admitted command can satisfy the prompt about to ship.

    A *proof*, not an inference from a non-advancing row: the supervisor and
    the park guard disagree about the command vocabulary, so no model turn and
    no number of re-deliveries can change the outcome. ``protocol``, because
    this is a contract failure between two runtime components -- never an
    owner failure (plan SS3.4 D2a/D2c).
    """

    emit(
        {
            "type": "dispatch.supervisor.receipt-unsatisfiable",
            "parent_attempt_id": args.parent_attempt_id,
            "reason": reason,
        }
    )
    detail = f"receipt-unsatisfiable:{reason}"
    if not reconcile(args, classify_supervisor_attention_terminal("claude", detail)):
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
    """D2b terminal: the receipt was proven satisfiable and the owner did not act.

    A *policy stop*, claiming nothing about why. It closes the owner attempt
    row and publishes no completion marker, so the route node stays incomplete
    and remains available to ordinary SD-106 same-node redispatch.
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
    if not reconcile(args, classify_supervisor_abandonment_terminal("claude", detail)):
        return 70
    emit({"type": "dispatch.supervisor.error", "reason": detail})
    return 70


def remediation_prompt(attempts: set[str], *, jobs: str = "") -> str:
    # Same route-bound-success dependency as completion_prompt() above.
    jobs_argument = f"--jobs {shlex.quote(jobs)} " if jobs else ""
    commands = "\n".join(
        f"{SHARED_HARVEST_SURFACE} harvest {jobs_argument}--attempt-id "
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


def claude_command(
    args: argparse.Namespace, session_id: str, resume: bool, *, stream: bool = False
) -> list[str]:
    if args.claude_command:
        command = shlex.split(args.claude_command)
    else:
        command = ["claude"]
    command += ["-p"]
    command += ["--session-id" if stream or not resume else "--resume", session_id]
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
    if stream:
        command += ["--input-format", "stream-json"]
    command += ["--output-format", "stream-json", "--verbose"]
    if args.model:
        command += ["--model", args.model]
    if args.effort:
        command += ["--effort", args.effort]
    if args.disallowed_tool:
        command += ["--disallowedTools", ",".join(args.disallowed_tool)]
    return command


class ClaudeStreamSession:
    """One long-lived realtime-input Claude process for every owner boundary."""

    def __init__(self, args: argparse.Namespace, session_id: str) -> None:
        try:
            self.process = subprocess.Popen(
                claude_command(args, session_id, False, stream=True),
                cwd=args.worktree,
                env={
                    **os.environ,
                    **(
                        {"AGENT_DISPATCH_COMPLETION_STATE_FILE": args.state_file}
                        if args.state_file
                        else {}
                    ),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=False,
                bufsize=0,
            )
        except OSError as exc:
            raise SupervisorError("claude-stream-process-failed") from exc
        if self.process.stdin is None or self.process.stdout is None:
            raise SupervisorError("claude-stream-pipe-missing")
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.output_buffer = b""
        self.closed = False

    def run_turn(self, prompt: str, timeout: float) -> tuple[dict[str, Any], int]:
        if self.closed or self.process.stdin is None or self.process.stdout is None:
            raise SupervisorError("claude-stream-closed")
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
            "parent_tool_use_id": None,
        }
        try:
            self.process.stdin.write(
                (
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    + "\n"
                ).encode("utf-8")
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SupervisorError("claude-stream-write-failed") from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SupervisorError("claude-turn-process-failed")
            events = self.selector.select(remaining)
            if not events:
                raise SupervisorError("claude-turn-process-failed")
            try:
                chunk = os.read(self.process.stdout.fileno(), 65_536)
            except OSError as exc:
                raise SupervisorError("claude-stream-read-failed") from exc
            if not chunk:
                raise SupervisorError("claude-result-missing")
            self.output_buffer += chunk
            if len(self.output_buffer) > 16_777_216:
                raise SupervisorError("claude-stream-message-oversized")
            while b"\n" in self.output_buffer:
                raw_line, self.output_buffer = self.output_buffer.split(b"\n", 1)
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(value, dict) and value.get("type") == "result":
                    return value, self.process.poll() or 0

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.selector.close()
        except Exception:
            pass
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def resolved_turn_transport(args: argparse.Namespace) -> str:
    if args.turn_transport != "auto":
        return args.turn_transport
    return "resume-process" if args.claude_command else "stream-json"


def run_turn(
    args: argparse.Namespace,
    session_id: str,
    prompt: str,
    *,
    resume: bool,
    stream_session: ClaudeStreamSession | None = None,
) -> tuple[dict[str, Any], int]:
    if stream_session is not None:
        return stream_session.run_turn(prompt, args.turn_timeout)
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
    value.add_argument("--max-identical-redeliveries", type=int, default=2)
    value.add_argument("--turn-timeout", type=float, default=7200.0)
    value.add_argument("--max-continuations", type=positive_continuation_limit)
    value.add_argument("--route-file")
    value.add_argument("--route-id", default="")
    value.add_argument("--route-hash", default="")
    value.add_argument("--state-file", default=os.environ.get("AGENT_DISPATCH_COMPLETION_STATE_FILE"))
    value.add_argument("--lease-file", default=os.environ.get("AGENT_DISPATCH_SUPERVISOR_LEASE_FILE"))
    value.add_argument("--claude-command", default=os.environ.get("CLAUDE_SESSION_COMMAND"))
    value.add_argument(
        "--turn-transport",
        choices=("auto", "stream-json", "resume-process"),
        default="auto",
    )
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
    delivery_timing = delivery_timing_fields()
    same_thread_resume_count = 0
    remediated: set[tuple[str, ...]] = set()
    launch_remediated: set[tuple[str, ...]] = set()
    last_delivery_identity: tuple[str, tuple[tuple[str, str], ...]] = ("", ())
    identical_redeliveries = 0
    next_prompt = initial_prompt
    continuations = 0
    resume = False
    turn_ordinal = 0
    turn_transport = resolved_turn_transport(args)
    stream_session: ClaudeStreamSession | None = None
    lease = hold_supervisor_lease(
        args.jobs, args.parent_attempt_id, args.lease_file or ""
    )
    lease_acquired = False
    lease_exit: tuple[object, object, object] = (None, None, None)
    try:
        lease.__enter__()
        lease_acquired = True
        if turn_transport == "stream-json":
            stream_session = ClaudeStreamSession(args, session_id)
        recovered = read_supervisor_phase_state(
            state_path, args.parent_attempt_id
        )
        if recovered is not None:
            delivered = set(recovered.delivered_attempt_ids)
            active_outbox = recovered.outbox
            if active_outbox is not None:
                if active_outbox.receipt is None:
                    raise SupervisorError("supervisor-outbox-receipt-missing")
                # D3: a supervisor that restarts onto an open-but-finished
                # child must not hand the owner an unsatisfiable receipt. The
                # park path's own pre-receipt runtime_reconcile is already
                # correct; this is the narrowed survivor of the assignment's
                # D2 for the recovery path specifically.
                runtime_reconcile(
                    args,
                    {
                        row.attempt_id: row
                        for row in current_children(
                            Path(args.jobs),
                            args.parent_attempt_id,
                            set(active_outbox.attempt_ids),
                        )
                    },
                    set(active_outbox.attempt_ids),
                )
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
                next_prompt = completion_prompt(
                    active_outbox.receipt or {}, active_outbox, jobs=args.jobs
                )
                delivery_timing = validate_delivery_timing(
                    (active_outbox.receipt or {})["delivery_timing"]
                )
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
            # D2a, one choke point for every producer: no prompt leaves this
            # supervisor until the park guard is proven to admit each command it
            # prescribes. With D1's harvest vocabulary applied this must never
            # fire -- it is a permanent tripwire against exactly the drift that
            # deadlocked att-30344fd4, not an expected path.
            satisfiable, unsatisfiable_reason = receipt_satisfiability(
                args, next_prompt, active_outbox
            )
            if not satisfiable:
                return seal_receipt_unsatisfiable(args, unsatisfiable_reason)
            last_delivery_identity = supervisor_outbox_delivery_identity(active_outbox)
            turn_ordinal += 1
            turn_started_ns = time.monotonic_ns()
            emit(
                {
                    "type": "dispatch.supervisor.turn-started",
                    "parent_attempt_id": args.parent_attempt_id,
                    "turn_ordinal": turn_ordinal,
                    "transport": turn_transport,
                    "resume": resume,
                    "monotonic_ns": turn_started_ns,
                }
            )
            result, process_rc = run_turn(
                args,
                session_id,
                next_prompt,
                resume=resume,
                stream_session=stream_session,
            )
            turn_completed_ns = time.monotonic_ns()
            emit(
                {
                    "type": "dispatch.supervisor.turn-completed",
                    "parent_attempt_id": args.parent_attempt_id,
                    "turn_ordinal": turn_ordinal,
                    "transport": turn_transport,
                    "resume": resume,
                    "monotonic_ns": turn_completed_ns,
                    "duration_seconds": round(
                        (turn_completed_ns - turn_started_ns) / 1_000_000_000, 3
                    ),
                }
            )
            if (
                process_rc != 0
                or result.get("is_error") is True
                or result.get("subtype") not in {None, "success"}
            ):
                if stream_session is not None:
                    teardown_started_ns = time.monotonic_ns()
                    stream_session.close()
                    stream_session = None
                    teardown_completed_ns = time.monotonic_ns()
                    emit(
                        {
                            "type": "dispatch.supervisor.teardown-completed",
                            "parent_attempt_id": args.parent_attempt_id,
                            "reason": "model-error",
                            "monotonic_ns": teardown_completed_ns,
                            "duration_seconds": round(
                                (teardown_completed_ns - teardown_started_ns)
                                / 1_000_000_000,
                                3,
                            ),
                        }
                    )
                terminal = classify_claude_result(result, process_rc)
                if not reconcile(args, terminal):
                    return 70
                emit(result)
                return process_rc or 3
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
                    # D2b. An owner that has simply not run the command yet is a
                    # legitimate state, so a non-advancing row alone never seals
                    # anything. An unchanged outbox first buys in-place work --
                    # no model turn, no continuation spend -- and only a bound
                    # exhausted after that stops the loop.
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
                    resume = True
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
                delivery_timing = validate_delivery_timing(
                    receipt["delivery_timing"]
                )
                current_rows = current_children(Path(args.jobs), args.parent_attempt_id)
                terminal_nodes = terminal_route_completion(args, current_rows)
                if terminal_nodes:
                    emit(
                        {
                            "type": "dispatch.supervisor.terminal-fast-path",
                            "parent_attempt_id": args.parent_attempt_id,
                            "terminal_nodes": list(terminal_nodes),
                            "continuation_saved": True,
                        }
                    )
                    if stream_session is not None:
                        teardown_started_ns = time.monotonic_ns()
                        stream_session.close()
                        stream_session = None
                        teardown_completed_ns = time.monotonic_ns()
                        emit(
                            {
                                "type": "dispatch.supervisor.teardown-completed",
                                "parent_attempt_id": args.parent_attempt_id,
                                "reason": "route-terminal",
                                "monotonic_ns": teardown_completed_ns,
                                "duration_seconds": round(
                                    (teardown_completed_ns - teardown_started_ns)
                                    / 1_000_000_000,
                                    3,
                                ),
                            }
                        )
                    final_result = terminal_handoff_result(
                        result, current_rows, terminal_nodes
                    )
                    delivery_timing = advance_delivery_timing(
                        delivery_timing, "final_report_marker_ns"
                    )
                    terminal = classify_claude_result(final_result, process_rc)
                    if not reconcile(args, terminal):
                        return 70
                    delivery_timing = advance_delivery_timing(
                        delivery_timing, "owner_terminal_envelope_ns"
                    )
                    emit(
                        {
                            "type": "dispatch.supervisor.delivery-timing",
                            "parent_attempt_id": args.parent_attempt_id,
                            "same_thread_resume_count": same_thread_resume_count,
                            **delivery_timing,
                        }
                    )
                    emit(final_result)
                    return 0 if terminal.failure_class == "pass" else 3
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
                next_prompt = remediation_prompt(unresolved, jobs=args.jobs)
                continuations += 1
                resume = True
                continue

            if stream_session is not None:
                teardown_started_ns = time.monotonic_ns()
                stream_session.close()
                stream_session = None
                teardown_completed_ns = time.monotonic_ns()
                emit(
                    {
                        "type": "dispatch.supervisor.teardown-completed",
                        "parent_attempt_id": args.parent_attempt_id,
                        "reason": "route-terminal",
                        "monotonic_ns": teardown_completed_ns,
                        "duration_seconds": round(
                            (teardown_completed_ns - teardown_started_ns)
                            / 1_000_000_000,
                            3,
                        ),
                    }
                )
            if delivery_timing["join_completed_ns"] is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "final_report_marker_ns"
                )
            terminal = classify_claude_result(result, process_rc)
            if not reconcile(args, terminal):
                return 70
            if delivery_timing["join_completed_ns"] is not None:
                delivery_timing = advance_delivery_timing(
                    delivery_timing, "owner_terminal_envelope_ns"
                )
                emit(
                    {
                        "type": "dispatch.supervisor.delivery-timing",
                        "parent_attempt_id": args.parent_attempt_id,
                        "same_thread_resume_count": same_thread_resume_count,
                        **delivery_timing,
                    }
                )
            emit(result)
            return 0 if terminal.failure_class == "pass" else 3
    except (DispatchContractError, JoinContractError, SupervisorError) as exc:
        if stream_session is not None:
            stream_session.close()
            stream_session = None
        lease_exit = (type(exc), exc, exc.__traceback__)
        reason = exc.reason if isinstance(exc, DispatchContractError) else str(exc)
        terminal = classify_supervisor_error("claude", reason)
        if not reconcile(args, terminal):
            return 70
        emit({"type": "dispatch.supervisor.error", "reason": reason})
        return 70
    except Exception as exc:  # fail closed without leaking protocol/model content
        if stream_session is not None:
            stream_session.close()
            stream_session = None
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
        if stream_session is not None:
            stream_session.close()
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
