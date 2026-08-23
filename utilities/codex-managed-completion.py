#!/usr/bin/env python3
"""Join one exact child batch and deliver one bounded receipt to a managed gateway."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_completion_join import (  # noqa: E402
    JoinContractError,
    MANAGED_SESSION_PARENT_DELIVERY,
    OPEN_STATES,
    current_children,
    current_session_children,
)


MAX_RESPONSE_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 2048
ALLOWED_REASONS = {"registry-closed", "terminal-observed"}
REQUIRED_ACTIONS = {
    "complete-open", "inspect-done-failure", "advance-completed",
}


class CompletionError(RuntimeError):
    """The exact batch or gateway control contract is invalid."""


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
                != MANAGED_SESSION_PARENT_DELIVERY
                or metadata.get("attempt_schema_version") != "2"
                or metadata.get("dispatch_depth") != "1"
                or metadata.get("execution_surface") != "registered-headless"
                or metadata.get("registered_worker") != "1"
                or metadata.get("harness") not in {"codex", "claude"}
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
    command = (
        shlex.split(args.join_command)
        if args.join_command
        else [
            sys.executable,
            str(ROOT / "utilities" / "dispatch_completion_join.py"),
        ]
    )
    command += ["--jobs", str(args.jobs)]
    if args.parent_session_id:
        command += [
            "--parent-session-id",
            args.parent_session_id,
            "--parent-completion-delivery",
            MANAGED_SESSION_PARENT_DELIVERY,
        ]
    else:
        command += ["--parent-attempt-id", args.parent_attempt_id]
    command += [
        "--interval", str(args.interval),
        "--timeout", str(args.timeout),
    ]
    for attempt_id in sorted(attempts):
        command += ["--attempt-id", attempt_id]
    try:
        result = subprocess.run(
            command,
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
            or (reason == "registry-closed" and status != "done")
            or (reason == "terminal-observed" and status not in OPEN_STATES)
            or (
                required_action == "complete-open"
                and status not in OPEN_STATES
            )
            or (
                required_action in {"inspect-done-failure", "advance-completed"}
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
                MANAGED_SESSION_PARENT_DELIVERY,
            )
        else:
            rows = current_children(jobs, parent_attempt_id or "", attempts)
    except JoinContractError as exc:
        raise CompletionError("registry-recheck-failed") from exc
    harnesses: dict[str, str] = {}
    for row in rows:
        harness = row.metadata.get("harness")
        if harness not in {"codex", "claude"}:
            raise CompletionError("registry-child-harness-invalid")
        harnesses[row.attempt_id] = harness
    normalized = {
        "schema_version": 2,
        "state": "ready",
        "parent_attempt_id": delivery_parent_id,
        "job_registry": str(jobs),
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
    if len(canonical(normalized).encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise CompletionError("typed-receipt-oversized")
    return normalized


def delivery_parent_id(args: argparse.Namespace) -> str:
    if args.parent_session_id:
        digest = hashlib.sha256(args.parent_session_id.encode("utf-8")).hexdigest()
        return f"parent-session-{digest}"
    return args.parent_attempt_id or ""


def gateway_request(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise CompletionError("control-socket-path-unsafe")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(130.0)
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
) -> dict[str, Any]:
    """Retry only a provably unsent/retryable delivery.

    A `sent-ambiguous` result is never retried: doing so could duplicate a turn
    after the server accepted the first request but the acknowledgement was
    lost. The gateway's durable ledger handles accepted replays locally.
    """

    deadline = time.monotonic() + max(0.0, args.delivery_retry_window)
    last_reason = "control-socket-unavailable"
    while True:
        try:
            result = gateway_request(args.control_socket, request)
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "schema_version": 1,
                "status": "retryable",
                "reason": f"delivery-retry-exhausted:{last_reason}",
            }
        time.sleep(min(max(args.delivery_retry_interval, 0.05), remaining))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--control-socket",
        type=Path,
        default=os.environ.get("AGENT_CODEX_MANAGED_CONTROL_SOCKET"),
    )
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
    value.add_argument("--sealed-batch-id", required=True)
    value.add_argument("--thread-id")
    value.add_argument("--attempt-id", action="append", default=[])
    value.add_argument("--interval", type=float, default=2.0)
    value.add_argument("--timeout", type=float, default=3600.0)
    value.add_argument("--launch-ready-timeout", type=float, default=60.0)
    value.add_argument("--delivery-retry-window", type=float, default=300.0)
    value.add_argument("--delivery-retry-interval", type=float, default=2.0)
    value.add_argument("--join-command")
    return value


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.control_socket is None:
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
    wait_for_session_launch_claims(args, attempts)
    receipt = run_join(args, attempts)
    delivery_parent = delivery_parent_id(args)
    if receipt.get("state") == "timeout":
        return (
            {
                "schema_version": 1,
                "status": "timeout",
                "parent_attempt_id": delivery_parent,
                "sealed_batch_id": args.sealed_batch_id,
                "attempt_ids": sorted(attempts),
            },
            3,
        )
    normalized = normalize_receipt(
        receipt,
        jobs=args.jobs,
        parent_attempt_id=args.parent_attempt_id,
        parent_session_id=args.parent_session_id,
        delivery_parent_id=delivery_parent,
        attempts=attempts,
    )
    request = {
        "schema_version": 1,
        "op": "deliver",
        "thread_id": args.thread_id or "",
        "parent_attempt_id": delivery_parent,
        "sealed_batch_id": args.sealed_batch_id,
        "receipt": normalized,
    }
    result = deliver_with_retry(args, request)
    status = result.get("status")
    return result, {
        "accepted": 0,
        "retryable": 75,
        "sent-ambiguous": 74,
        "rejected": 65,
    }.get(status, 65)


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
