"""Strict, transport-neutral receipts for live human-gate delivery.

This module deliberately does not read or mutate workflow state.  Producers
construct a receipt from already-authoritative evidence; carriers validate the
same bytes before claiming or sending it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
from typing import Any

from dispatch_completion_join import (
    JoinContractError,
    MANAGED_SESSION_PARENT_DELIVERY,
    OPEN_STATES,
    current_session_children,
)
import dispatch_pending_delivery as pending_delivery
from route_identity import route_hash as computed_route_hash
import workflow_state

MAX_RECEIPT_BYTES = 2048
MAX_CONTROL_BYTES = 16 * 1024
MAX_CONTEXT_BYTES = 8 * 1024
MAX_PATH_BYTES = 1024
MAX_ROUTE_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
MAX_JOBS_BYTES = 128 * 1024 * 1024
SCHEMA_VERSION = 1
KIND = "human-gate"
STATE = "blocked"
REQUIRED_ACTION = "inspect-release"
RECIPIENT_KIND = MANAGED_SESSION_PARENT_DELIVERY
CAPABILITY = {
    "schema_version": SCHEMA_VERSION,
    "recipient_kind": RECIPIENT_KIND,
}
IDENTIFIER = re.compile(r"^[A-Za-z0-9._:@/+\-=]{1,256}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PENDING_DELIVERY_ID = re.compile(r"^delivery-[A-Za-z0-9._-]{1,240}$")
ALLOWED_KEYS = {
    "kind", "schema_version", "state", "required_action", "route_id",
    "route_hash", "route_file", "route_node", "gate", "gate_epoch",
    "owner_attempt_id", "sealed_batch_id", "job_registry",
    "recipient_thread_id", "recipient_epoch", "artifact_path",
    "release_authority", "journal_path", "interview", "questions",
    "pending_delivery_id",
}
RELEASE_AUTHORITIES = {"depth-0", "any"}


class HumanGateReceiptError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def digest(receipt: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(receipt)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise HumanGateReceiptError(f"{name}-invalid")
    return value


def _absolute_regular(
    value: Any,
    name: str,
    *,
    max_file_bytes: int | None = None,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_PATH_BYTES
        or not Path(value).is_absolute()
    ):
        raise HumanGateReceiptError(f"{name}-path-invalid")
    path = Path(value)
    try:
        info = path.lstat()
    except OSError as exc:
        raise HumanGateReceiptError(f"{name}-unreadable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HumanGateReceiptError(f"{name}-path-unsafe")
    if max_file_bytes is not None and info.st_size > max_file_bytes:
        raise HumanGateReceiptError(f"{name}-oversized")
    if str(path) != value:
        raise HumanGateReceiptError(f"{name}-path-noncanonical")
    return value


def _validate_shape(
    receipt: Any,
    *,
    expected_thread: str | None = None,
    expected_epoch: int | None = None,
    journal_committed: bool = True,
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != ALLOWED_KEYS:
        raise HumanGateReceiptError("receipt-shape-invalid")
    if (
        receipt["kind"] != KIND
        or receipt["schema_version"] != SCHEMA_VERSION
        or receipt["state"] != STATE
        or receipt["required_action"] != REQUIRED_ACTION
    ):
        raise HumanGateReceiptError("receipt-type-invalid")
    for key in (
        "route_id",
        "route_node",
        "gate",
        "owner_attempt_id",
        "sealed_batch_id",
        "recipient_thread_id",
    ):
        _identifier(receipt[key], key.replace("_", "-"))
    if not DIGEST.fullmatch(str(receipt["route_hash"])):
        raise HumanGateReceiptError("route-hash-invalid")
    if not PENDING_DELIVERY_ID.fullmatch(str(receipt["pending_delivery_id"])):
        raise HumanGateReceiptError("pending-delivery-id-invalid")
    if not isinstance(receipt["gate_epoch"], int) or receipt["gate_epoch"] < 1:
        raise HumanGateReceiptError("gate-epoch-invalid")
    if not isinstance(receipt["recipient_epoch"], int) or receipt["recipient_epoch"] < 1:
        raise HumanGateReceiptError("recipient-epoch-invalid")
    if expected_thread is not None and receipt["recipient_thread_id"] != expected_thread:
        raise HumanGateReceiptError("recipient-thread-mismatch")
    if expected_epoch is not None and receipt["recipient_epoch"] != expected_epoch:
        raise HumanGateReceiptError("recipient-epoch-mismatch")
    if receipt["release_authority"] not in RELEASE_AUTHORITIES:
        raise HumanGateReceiptError("release-authority-invalid")
    if not isinstance(receipt["interview"], bool):
        raise HumanGateReceiptError("interview-invalid")
    if (
        not isinstance(receipt["questions"], int)
        or isinstance(receipt["questions"], bool)
        or receipt["questions"] < 0
        or receipt["questions"] > 64
        or (receipt["interview"] and receipt["questions"] < 1)
        or (not receipt["interview"] and receipt["questions"] != 0)
    ):
        raise HumanGateReceiptError("questions-invalid")
    _absolute_regular(receipt["route_file"], "route", max_file_bytes=MAX_ROUTE_BYTES)
    _absolute_regular(
        receipt["job_registry"], "job-registry", max_file_bytes=MAX_JOBS_BYTES
    )
    _absolute_regular(receipt["artifact_path"], "artifact")
    if journal_committed:
        _absolute_regular(
            receipt["journal_path"], "journal", max_file_bytes=MAX_JOURNAL_BYTES
        )
    elif (
        not isinstance(receipt["journal_path"], str)
        or not Path(receipt["journal_path"]).is_absolute()
        or len(receipt["journal_path"].encode("utf-8")) > MAX_PATH_BYTES
    ):
        raise HumanGateReceiptError("journal-path-invalid")
    if len(canonical(receipt)) > MAX_RECEIPT_BYTES:
        raise HumanGateReceiptError("receipt-oversized")
    return dict(receipt)


def validate(
    receipt: Any,
    *,
    expected_thread: str | None = None,
    expected_epoch: int | None = None,
) -> dict[str, Any]:
    """Backward-compatible structural validator used by producer call sites."""

    return _validate_shape(
        receipt,
        expected_thread=expected_thread,
        expected_epoch=expected_epoch,
    )


def receipt_digest(receipt: dict[str, Any]) -> str:
    _validate_shape(receipt)
    return digest(receipt)


def validate_digest(receipt: dict[str, Any], supplied: Any) -> str:
    _validate_shape(receipt)
    expected = digest(receipt)
    if not isinstance(supplied, str) or supplied != expected:
        raise HumanGateReceiptError("receipt-digest-mismatch")
    return supplied


def expected_journal_path(jobs: Path, route_id: str) -> Path:
    return jobs.parent / "workflow" / route_id / "journal.jsonl"


def make_receipt(
    *,
    route_file: Path,
    route: dict[str, Any],
    route_node: str,
    gate: str,
    gate_epoch: int,
    owner_attempt_id: str,
    sealed_batch_id: str,
    jobs: Path,
    recipient_thread_id: str,
    recipient_epoch: int,
    artifact_path: Path,
    release_authority: str,
    interview: bool,
    questions: int,
    pending_delivery_id: str,
) -> dict[str, Any]:
    """Build bytes for one raise; live journal validation happens after commit."""

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "state": STATE,
        "required_action": REQUIRED_ACTION,
        "route_id": route.get("route_id"),
        "route_hash": route.get("route_hash"),
        "route_file": str(route_file),
        "route_node": route_node,
        "gate": gate,
        "gate_epoch": gate_epoch,
        "owner_attempt_id": owner_attempt_id,
        "sealed_batch_id": sealed_batch_id,
        "job_registry": str(jobs),
        "recipient_thread_id": recipient_thread_id,
        "recipient_epoch": recipient_epoch,
        "artifact_path": str(artifact_path),
        "release_authority": release_authority,
        "journal_path": str(expected_journal_path(jobs, str(route.get("route_id") or ""))),
        "interview": interview,
        "questions": questions,
        "pending_delivery_id": pending_delivery_id,
    }
    return _validate_shape(receipt, journal_committed=False)


def _load_route(receipt: dict[str, Any]) -> dict[str, Any]:
    path = Path(receipt["route_file"])
    try:
        route = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HumanGateReceiptError("route-unreadable") from exc
    if not isinstance(route, dict):
        raise HumanGateReceiptError("route-shape-invalid")
    if (
        route.get("route_id") != receipt["route_id"]
        or route.get("route_hash") != receipt["route_hash"]
        or computed_route_hash(route) != receipt["route_hash"]
    ):
        raise HumanGateReceiptError("route-identity-mismatch")
    node = next(
        (
            row
            for row in route.get("nodes", [])
            if isinstance(row, dict) and row.get("id") == receipt["route_node"]
        ),
        None,
    )
    continuation = (node or {}).get("continuation") or {}
    if not workflow_state.node_raises_human_gate(node or {}, receipt["gate"]):
        raise HumanGateReceiptError("route-gate-node-mismatch")
    binding = next(
        (
            row
            for row in route.get("human_gate_bindings", [])
            if isinstance(row, dict) and row.get("gate") == receipt["gate"]
        ),
        None,
    )
    if binding is None:
        raise HumanGateReceiptError("route-gate-binding-missing")
    declared_authority = str(binding.get("release_authority") or "")
    if declared_authority and declared_authority != receipt["release_authority"]:
        raise HumanGateReceiptError("route-release-authority-mismatch")
    artifact_root = route.get("artifact_root")
    if not isinstance(artifact_root, str) or not Path(artifact_root).is_absolute():
        raise HumanGateReceiptError("route-artifact-root-invalid")
    try:
        Path(receipt["artifact_path"]).resolve().relative_to(Path(artifact_root).resolve())
    except ValueError as exc:
        raise HumanGateReceiptError("artifact-outside-root") from exc
    return route


def _load_journal(receipt: dict[str, Any], jobs: Path) -> list[dict[str, Any]]:
    expected = expected_journal_path(jobs, receipt["route_id"])
    if str(expected) != receipt["journal_path"]:
        raise HumanGateReceiptError("journal-path-mismatch")
    try:
        raw_lines = expected.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise HumanGateReceiptError("gate-journal-invalid") from exc
    entries: list[dict[str, Any]] = []
    for line in raw_lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise HumanGateReceiptError("gate-journal-invalid") from exc
        if not isinstance(value, dict):
            raise HumanGateReceiptError("gate-journal-invalid")
        entries.append(value)
    if not entries:
        raise HumanGateReceiptError("gate-journal-invalid")
    return entries


def _validate_live_row(
    receipt: dict[str, Any], jobs: Path, expected_attempts: set[str]
) -> None:
    owner_attempt = receipt["owner_attempt_id"]
    if owner_attempt not in expected_attempts:
        raise HumanGateReceiptError("batch-attempt-mismatch")
    try:
        rows = current_session_children(
            jobs,
            receipt["recipient_thread_id"],
            {owner_attempt},
            RECIPIENT_KIND,
        )
    except JoinContractError as exc:
        raise HumanGateReceiptError("registry-owner-invalid") from exc
    if len(rows) != 1 or rows[0].status not in OPEN_STATES:
        raise HumanGateReceiptError("registry-owner-not-live")
    metadata = rows[0].metadata
    route_id = metadata.get("owner_route_id") or metadata.get("route_id")
    route_hash = metadata.get("owner_route_hash") or metadata.get("route_hash")
    route_file = metadata.get("owner_route_file") or metadata.get("route_file")
    if (
        metadata.get("worker_type") != "owner"
        or metadata.get("unit") != "_kernel/owner"
        or metadata.get("launch_started") != "1"
        or route_id != receipt["route_id"]
        or route_hash != receipt["route_hash"]
        or route_file != receipt["route_file"]
        or metadata.get("managed_sealed_batch_id") != receipt["sealed_batch_id"]
    ):
        raise HumanGateReceiptError("registry-owner-binding-mismatch")


def validate_receipt(
    receipt: Any,
    *,
    jobs: Path,
    expected_thread_id: str,
    expected_epoch: int,
    expected_attempts: set[str],
    expected_sealed_batch_id: str,
    validate_live: bool = True,
) -> dict[str, Any]:
    normalized = _validate_shape(
        receipt,
        expected_thread=expected_thread_id,
        expected_epoch=expected_epoch,
    )
    if (
        not jobs.is_absolute()
        or jobs.is_symlink()
        or not jobs.is_file()
        or str(jobs) != normalized["job_registry"]
    ):
        raise HumanGateReceiptError("jobs-authority-mismatch")
    if normalized["sealed_batch_id"] != expected_sealed_batch_id:
        raise HumanGateReceiptError("sealed-batch-mismatch")
    _load_route(normalized)
    if validate_live:
        _validate_live_row(normalized, jobs, expected_attempts)
    entries = _load_journal(normalized, jobs)
    resolution = workflow_state.human_gate_resolution(entries, normalized["gate"])
    expected_delivery = pending_delivery.record_path(
        jobs.parent,
        expected_thread_id,
        normalized["pending_delivery_id"],
    )
    if (
        resolution.get("status") != "blocked"
        or resolution.get("epoch") != normalized["gate_epoch"]
        or resolution.get("artifact") != normalized["artifact_path"]
        or resolution.get("delivery") != str(expected_delivery)
        or resolution.get("release_authority") != normalized["release_authority"]
        or bool(resolution.get("interview")) != normalized["interview"]
        or int(resolution.get("questions") or 0) != normalized["questions"]
    ):
        raise HumanGateReceiptError("gate-journal-authority-mismatch")
    return normalized


def validate_pending_record(
    record: Any,
    *,
    jobs: Path,
    expected_thread_id: str,
    expected_epoch: int,
    expected_attempts: set[str],
    expected_sealed_batch_id: str,
) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("state") not in {
        "pending",
        "claimed",
        "sent-ambiguous",
    }:
        raise HumanGateReceiptError("pending-record-invalid")
    receipt = validate_receipt(
        record.get("receipt"),
        jobs=jobs,
        expected_thread_id=expected_thread_id,
        expected_epoch=expected_epoch,
        expected_attempts=expected_attempts,
        expected_sealed_batch_id=expected_sealed_batch_id,
    )
    owner_attempt = receipt["owner_attempt_id"]
    if (
        record.get("delivery_id") != receipt["pending_delivery_id"]
        or record.get("recipient_kind") != RECIPIENT_KIND
        or record.get("recipient_digest")
        != pending_delivery.recipient_digest(expected_thread_id)
        or record.get("session_generation") != str(expected_epoch)
        or record.get("session_generation_supported") != "1"
        or record.get("attempt_ids") != [owner_attempt]
        or record.get("parent_attempt_id") != owner_attempt
        or record.get("route_id") != receipt["route_id"]
        or record.get("route_node") != receipt["route_node"]
        or record.get("receipt_digest") != digest(receipt)
        or (record.get("row_revisions") or {}).get(owner_attempt)
        != f"human-gate:{receipt['gate']}:{receipt['gate_epoch']}"
    ):
        raise HumanGateReceiptError("pending-authority-mismatch")
    return dict(record)


def validate_pending(
    record: Any,
    *,
    expected_thread: str | None = None,
    expected_delivery_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility structural check retained for producer integration."""

    if not isinstance(record, dict) or record.get("state") not in {
        "pending",
        "claimed",
        "sent-ambiguous",
    }:
        raise HumanGateReceiptError("pending-record-invalid")
    if expected_delivery_id is not None and record.get("delivery_id") != expected_delivery_id:
        raise HumanGateReceiptError("delivery-id-mismatch")
    receipt = _validate_shape(record.get("receipt"), expected_thread=expected_thread)
    validate_digest(receipt, record.get("receipt_digest"))
    if (
        record.get("route_id") != receipt["route_id"]
        or record.get("route_node") != receipt["route_node"]
        or record.get("parent_attempt_id") != receipt["owner_attempt_id"]
    ):
        raise HumanGateReceiptError("pending-authority-mismatch")
    return receipt


def gateway_delivery_id(receipt: dict[str, Any]) -> str:
    normalized = _validate_shape(receipt)
    identity = {
        "kind": KIND,
        "pending_delivery_id": normalized["pending_delivery_id"],
        "receipt_digest": digest(normalized),
        "recipient_thread_id": normalized["recipient_thread_id"],
        "recipient_epoch": normalized["recipient_epoch"],
        "route_id": normalized["route_id"],
        "gate": normalized["gate"],
        "gate_epoch": normalized["gate_epoch"],
        "sealed_batch_id": normalized["sealed_batch_id"],
    }
    return "hg-dlv-" + hashlib.sha256(canonical(identity)).hexdigest()


def render_context(
    receipt: dict[str, Any],
    delivery_id: str,
    *,
    agent_home: Path,
) -> dict[str, Any]:
    normalized = _validate_shape(receipt)
    if delivery_id != gateway_delivery_id(normalized):
        raise HumanGateReceiptError("gateway-delivery-id-mismatch")
    supervisor = shlex.quote(str(agent_home / "utilities" / "workflow-supervisor.py"))
    route = shlex.quote(normalized["route_file"])
    gate = shlex.quote(normalized["gate"])
    jobs = shlex.quote(normalized["job_registry"])
    inspect = f"python3 {supervisor} status --route {route} --jobs {jobs} --json"
    release_base = f"python3 {supervisor} release --route {route} --gate {gate}"
    release_commands = []
    for decision in ("proceed", "revise", "stop"):
        command = f"{release_base} --decision {decision} --jobs {jobs}"
        if normalized["interview"] and decision == "proceed":
            command += " --answers <validated-answers-file>"
        release_commands.append(command)
    text = (
        "AGENT_HARNESS_HUMAN_GATE_V1\n"
        + canonical(normalized).decode("utf-8")
        + "\nA human decision is required; the owner is alive and waiting.\n"
        + f"Artifact: {normalized['artifact_path']}\n"
        + "Run only these checked forms after presenting the gate to the person:\n"
        + inspect
        + "\n"
        + "\n".join(release_commands)
        + "\nFor interview gates, answers are required only on proceed; revise/stop omit answers."
        + "\nDo not auto-approve, mark-done, harvest, or start any polling/wait loop."
    )
    if len(text.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise HumanGateReceiptError("context-oversized")
    return {
        "threadId": normalized["recipient_thread_id"],
        "input": [],
        "clientUserMessageId": delivery_id,
        "additionalContext": {
            "hearting-human-gate": {"kind": "application", "value": text}
        },
    }


def context(receipt: dict[str, Any], delivery_id: str) -> dict[str, Any]:
    return render_context(
        receipt,
        delivery_id,
        agent_home=Path(__file__).resolve().parents[1],
    )


def probe_consumer(control_path: Path, *, expected_thread_id: str) -> dict[str, Any]:
    if (
        not control_path.is_absolute()
        or control_path.is_symlink()
        or not expected_thread_id
    ):
        raise HumanGateReceiptError("consumer-control-path-invalid")
    try:
        info = control_path.stat()
    except OSError as exc:
        raise HumanGateReceiptError("consumer-unavailable") from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise HumanGateReceiptError("consumer-control-path-invalid")
    request = canonical({"schema_version": 1, "op": "status"}) + b"\n"
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5.0)
    try:
        connection.connect(str(control_path))
        connection.sendall(request)
        response = bytearray()
        while b"\n" not in response:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_CONTROL_BYTES:
                raise HumanGateReceiptError("consumer-response-oversized")
    except OSError as exc:
        raise HumanGateReceiptError("consumer-unavailable") from exc
    finally:
        connection.close()
    line, separator, remainder = bytes(response).partition(b"\n")
    if not separator or remainder.strip():
        raise HumanGateReceiptError("consumer-response-framing-invalid")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HumanGateReceiptError("consumer-response-invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("status") != "ready"
        or value.get("thread_id") != expected_thread_id
    ):
        raise HumanGateReceiptError("consumer-capability-unproven")
    capability = (value.get("capabilities") or {}).get("human_gate_delivery")
    if (
        not isinstance(capability, dict)
        or capability.get("version") != 1
        or capability.get("thread_id") != expected_thread_id
        or not isinstance(capability.get("epoch"), int)
        or capability["epoch"] < 1
    ):
        raise HumanGateReceiptError("consumer-capability-unproven")
    return {
        "schema_version": 1,
        "thread_id": expected_thread_id,
        "epoch": capability["epoch"],
        "recipient_kind": RECIPIENT_KIND,
    }
