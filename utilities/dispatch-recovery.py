#!/usr/bin/env python3
"""Crash-idempotent coordinator for one exact receiptless dispatch recovery."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "utilities")]

from dispatch_continuation_budget import resolve_continuation_budget  # noqa: E402
from dispatch_contract import (  # noqa: E402
    ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT,
    DispatchContractError,
    RecoveryRetryClaim,
    claim_recovery_retry,
    parse_registry_metadata,
    process_identity_is_live,
    recovery_id as canonical_recovery_id,
    seal_recovery_blocked,
    validate_attempt_metadata,
)
from replica_batch_contract import (  # noqa: E402
    ReplicaBatchContractError,
    build_manifest,
    verify_manifest,
)


RECOVERY_RECORD_VERSION = 1
PHASES = (
    "intent-observed",
    "cancellation-receipt-committed",
    "continuation-published",
    "retry-claimed",
    "wrapper-started",
    "terminal-or-blocked",
)


class RecoveryError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class RecoveryInterfaceUnavailable(RecoveryError):
    """The checked Phase 4 boundary is not installed in this release."""


class InjectedRecoveryCrash(RuntimeError):
    """Test-only crash raised by an injected phase-boundary callback."""


@dataclass(frozen=True)
class RecoveryRequest:
    jobs: Path
    original_attempt_id: str
    route_file: Path
    resume_from_node: str
    requested_boundary: str
    reason: str = "receipt-unavailable-recovery"
    cancellation_wait: float = 2.0


@dataclass(frozen=True)
class AttemptSnapshot:
    status: str
    repo: str
    worktree: str
    slug: str
    metadata: dict[str, str]
    row_digest: str


@dataclass(frozen=True)
class RecoveryResult:
    recovery_id: str
    phase: str
    state: str
    reason: str
    retry_attempt_id: str = ""
    child_spawned: int = 0
    record_path: str = ""

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SourceBatchContext:
    route: dict[str, object]
    source_route_digest: str
    artifact_root: Path
    group_id: str
    gap_leg_id: str
    parent_slug: str
    parent_attempt_id: str
    cardinality: int
    manifest: dict[str, object]
    manifest_digest: str
    leg_digests: dict[str, str]


class RecoveryServices:
    """Checked mutation/observation boundary around registry and Phase 4.

    Tests inject a deterministic implementation. Production uses only the
    official continuation compiler and exact partial-batch consumer.
    """

    def cancel_receiptless(self, request: RecoveryRequest) -> dict[str, object]:
        raise NotImplementedError

    def remaining_cascade(
        self, request: RecoveryRequest, source: AttemptSnapshot, recovery_identity: str
    ) -> int:
        raise NotImplementedError

    def observe_continuation(
        self, request: RecoveryRequest, recovery_identity: str
    ) -> dict[str, object] | None:
        raise NotImplementedError

    def publish_continuation(
        self,
        request: RecoveryRequest,
        source: AttemptSnapshot,
        recovery_identity: str,
    ) -> dict[str, object]:
        raise NotImplementedError

    def observe_start(
        self,
        request: RecoveryRequest,
        continuation: dict[str, object],
        claim: RecoveryRetryClaim,
    ) -> dict[str, object] | None:
        raise NotImplementedError

    def start_gap(
        self,
        request: RecoveryRequest,
        continuation: dict[str, object],
        claim: RecoveryRetryClaim,
    ) -> dict[str, object]:
        raise NotImplementedError

    def observe_terminal(
        self, request: RecoveryRequest, claim: RecoveryRetryClaim
    ) -> dict[str, object] | None:
        raise NotImplementedError


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _attempt_lock_path(request: RecoveryRequest) -> Path:
    identity = hashlib.sha256(
        f"{request.jobs.resolve()}\0{request.original_attempt_id}".encode("utf-8")
    ).hexdigest()
    return request.jobs.parent / "recovery" / "locks" / f"{identity}.lock"


def recovery_record_path(jobs: Path, recovery_identity: str) -> Path:
    if not recovery_identity.startswith("rec-"):
        raise RecoveryError("recovery-identity-invalid")
    return jobs.parent / "recovery" / f"{recovery_identity}.json"


def recovery_intent_path(request: RecoveryRequest) -> Path:
    identity = hashlib.sha256(_canonical_bytes({
        "jobs": str(request.jobs.resolve()),
        "attempt_id": request.original_attempt_id,
        "route_file": str(request.route_file.resolve()),
        "resume_from_node": request.resume_from_node,
        "requested_boundary": request.requested_boundary,
        "reason": request.reason,
    })).hexdigest()
    return request.jobs.parent / "recovery" / "intents" / f"intent-{identity}.json"


def continuation_record_path(jobs: Path, recovery_identity: str) -> Path:
    return jobs.parent / "recovery" / "continuations" / f"{recovery_identity}.json"


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json_object(raw: bytes, reason: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecoveryError(reason) from exc
    if not isinstance(payload, dict):
        raise RecoveryError(reason)
    return payload


def _command_receipt(
    completed: subprocess.CompletedProcess[str],
    *,
    failed_reason: str,
    invalid_reason: str,
) -> dict[str, object]:
    if completed.returncode != 0:
        raise RecoveryError(
            failed_reason,
            completed.stderr.strip() or completed.stdout.strip() or failed_reason,
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RecoveryError(invalid_reason)
    try:
        receipt = json.loads(lines[-1])
    except ValueError as exc:
        raise RecoveryError(invalid_reason) from exc
    if not isinstance(receipt, dict):
        raise RecoveryError(invalid_reason)
    return receipt


def _source_route(
    request: RecoveryRequest, source: AttemptSnapshot
) -> tuple[dict[str, object], str, Path]:
    try:
        raw = request.route_file.read_bytes()
    except OSError as exc:
        raise RecoveryError("recovery-source-route-unreadable", str(exc)) from exc
    route = _json_object(raw, "recovery-source-route-invalid")
    metadata = source.metadata
    if (
        route.get("route_id") != metadata.get("route_id")
        or route.get("route_hash") != metadata.get("route_hash")
    ):
        raise RecoveryError("recovery-source-route-binding-mismatch")
    try:
        route_cwd = Path(str(route["cwd"])).resolve(strict=False)
        artifact_root = Path(str(route["artifact_root"])).resolve(strict=False)
    except (KeyError, OSError, ValueError) as exc:
        raise RecoveryError("recovery-source-route-binding-mismatch") from exc
    if not artifact_root.is_absolute() or route_cwd != Path(source.worktree).resolve(
        strict=False
    ):
        raise RecoveryError("recovery-source-route-binding-mismatch")
    expected_source_path = (
        artifact_root / ".runtime" / "routes" / f"{route['route_id']}.json"
    ).resolve(strict=False)
    if request.route_file.resolve(strict=False) != expected_source_path:
        raise RecoveryError("recovery-source-route-path-mismatch")
    launch = route.get("launch_compatibility_tuple") or {}
    jobs_binding = (launch.get("jobs_path") or {}).get("path") if isinstance(
        launch, dict
    ) else None
    if not isinstance(jobs_binding, str) or not jobs_binding:
        raise RecoveryError("recovery-source-jobs-binding-mismatch")
    if Path(jobs_binding).resolve(strict=False) != request.jobs.resolve(strict=False):
        raise RecoveryError("recovery-source-jobs-binding-mismatch")
    runtime_jobs = os.environ.get("AGENT_DISPATCH_JOBS", "")
    if (
        not runtime_jobs
        or Path(runtime_jobs).resolve(strict=False)
        != request.jobs.resolve(strict=False)
    ):
        raise RecoveryError("recovery-runtime-jobs-binding-mismatch")
    return route, _sha256_bytes(raw), artifact_root


def _source_batch_context(
    request: RecoveryRequest, source: AttemptSnapshot
) -> SourceBatchContext:
    """Rebuild the one original manifest only from its route and exact rows."""

    route, source_route_digest, artifact_root = _source_route(request, source)
    metadata = source.metadata
    group_id = metadata.get("batch_group", "")
    manifest_digest = metadata.get("batch_manifest_sha256", "")
    gap_leg_id = metadata.get("batch_route_node", "")
    parent_slug = metadata.get("parent", "")
    parent_attempt_id = metadata.get("batch_parent_attempt_id", "")
    try:
        cardinality = int(metadata.get("batch_declared_size", "0"))
    except ValueError as exc:
        raise RecoveryError("recovery-source-batch-binding-mismatch") from exc
    if (
        not group_id
        or not manifest_digest.startswith("sha256:")
        or not gap_leg_id
        or not parent_slug
        or not parent_attempt_id
        or metadata.get("parent_attempt_id") != parent_attempt_id
        or metadata.get("batch_route_id") != route.get("route_id")
        or metadata.get("batch_attempt_id") != request.original_attempt_id
        or cardinality not in range(2, 5)
    ):
        raise RecoveryError("recovery-source-batch-binding-mismatch")

    groups = [
        row for row in route.get("parallel_groups", [])
        if isinstance(row, dict) and row.get("id") == group_id
    ]
    if len(groups) != 1:
        raise RecoveryError("recovery-source-group-mismatch")
    group = groups[0]
    route_nodes = {
        str(node.get("id")): node
        for node in route.get("nodes", [])
        if isinstance(node, dict)
        and (node.get("parallel_group") or node.get("replica_group")) == group_id
    }
    if (
        group.get("width") != cardinality
        or len(route_nodes) != cardinality
        or set(group.get("members") or []) != set(route_nodes)
        or gap_leg_id not in route_nodes
    ):
        raise RecoveryError("recovery-source-group-mismatch")

    rows = [
        row
        for row in _attempt_rows_for_route(request.jobs, str(route["route_id"]))
        if row.metadata.get("batch_manifest_sha256") == manifest_digest
    ]
    if len(rows) != cardinality:
        raise RecoveryError("recovery-source-manifest-row-census-mismatch")
    if len({row.metadata.get("attempt_id") for row in rows}) != cardinality:
        raise RecoveryError("recovery-source-manifest-row-census-mismatch")

    members: list[dict[str, object]] = []
    row_by_attempt: dict[str, AttemptSnapshot] = {}
    independence_values: set[str] = set()
    for row in rows:
        row_metadata = row.metadata
        try:
            validate_attempt_metadata(row_metadata)
            attempt_id = row_metadata["attempt_id"]
            node_id = row_metadata["batch_route_node"]
            node = route_nodes[node_id]
            ordinal = int(row_metadata["batch_fallback_ordinal"])
            leg_index = int(row_metadata["batch_parallel_leg_index"])
            leg_class = row_metadata["batch_leg_class"]
            auxiliary = row_metadata["batch_auxiliary_check"]
            member = {
                "assignment_sha256": row_metadata["batch_assignment_sha256"],
                "attempt_id": attempt_id,
                "route_node": node_id,
                "harness": row_metadata["batch_harness"],
                "fallback_hop": row_metadata["batch_fallback_hop"],
                "fallback_ordinal": ordinal,
                "model_profile": row_metadata["batch_model_profile"],
                "perspective": row_metadata["batch_perspective"],
                "parallel_leg_index": leg_index,
                "leg_class": leg_class,
                **({"auxiliary_check": auxiliary} if leg_class == "auxiliary" else {}),
            }
        except (DispatchContractError, KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("recovery-source-manifest-row-invalid", str(exc)) from exc
        expected_auxiliary = (
            str(node.get("auxiliary_check"))
            if node.get("leg_class") == "auxiliary"
            else "-"
        )
        if (
            row_metadata.get("batch_group") != group_id
            or row_metadata.get("batch_declared_size") != str(cardinality)
            or row_metadata.get("batch_route_id") != route.get("route_id")
            or row_metadata.get("batch_parent_attempt_id") != parent_attempt_id
            or row_metadata.get("parent_attempt_id") != parent_attempt_id
            or row_metadata.get("parent") != parent_slug
            or row_metadata.get("batch_attempt_id") != attempt_id
            or row_metadata.get("route_node") != node_id
            or member["model_profile"] != str(node.get("model_profile"))
            or member["perspective"] != str(node.get("perspective"))
            or member["parallel_leg_index"] != int(node.get("parallel_leg_index", -1))
            or leg_class != str(node.get("leg_class") or "peer")
            or auxiliary != expected_auxiliary
        ):
            raise RecoveryError("recovery-source-manifest-row-binding-mismatch")
        independence_values.add(row_metadata.get("batch_independence", ""))
        members.append(member)
        row_by_attempt[attempt_id] = row

    if len(independence_values) != 1 or "" in independence_values:
        raise RecoveryError("recovery-source-manifest-independence-mismatch")
    independence = next(iter(independence_values))
    required_axes = group.get("independence_axes")
    if not isinstance(required_axes, list):
        raise RecoveryError("recovery-source-group-mismatch")
    realized_axes: list[str] = []
    if len({str(member["harness"]) for member in members}) >= 2:
        realized_axes.append("cross-harness")
    if len({str(member["model_profile"]) for member in members}) >= 2:
        realized_axes.append("model-profile")
    if len({str(member["perspective"]) for member in members}) == cardinality:
        realized_axes.append("perspective")
    degradation_reason = (
        "" if independence == "cross-harness"
        else "cross-harness-unavailable-user-allowed"
    )
    try:
        manifest, rebuilt_digest, leg_digests = build_manifest(
            parallel_group=group_id,
            route_id=str(route["route_id"]),
            parent_attempt_id=parent_attempt_id,
            independence=independence,
            members=members,
            required_independence_axes=[str(axis) for axis in required_axes],
            realized_independence_axes=realized_axes,
            degradation_reason=degradation_reason,
        )
        verified, verified_digest, verified_legs = verify_manifest(manifest)
    except (KeyError, ReplicaBatchContractError, TypeError, ValueError) as exc:
        raise RecoveryError("recovery-source-manifest-invalid", str(exc)) from exc
    if (
        verified != manifest
        or rebuilt_digest != manifest_digest
        or verified_digest != manifest_digest
        or verified_legs != leg_digests
    ):
        raise RecoveryError("recovery-source-manifest-digest-mismatch")
    for attempt_id, leg_digest in leg_digests.items():
        if row_by_attempt[attempt_id].metadata.get("batch_leg_sha256") != leg_digest:
            raise RecoveryError("recovery-source-leg-digest-mismatch")

    return SourceBatchContext(
        route=route,
        source_route_digest=source_route_digest,
        artifact_root=artifact_root,
        group_id=group_id,
        gap_leg_id=gap_leg_id,
        parent_slug=parent_slug,
        parent_attempt_id=parent_attempt_id,
        cardinality=cardinality,
        manifest=manifest,
        manifest_digest=manifest_digest,
        leg_digests=leg_digests,
    )


def _preview_continuation_route(
    request: RecoveryRequest, context: SourceBatchContext
) -> dict[str, object]:
    """Read-only official build used only to derive the exact output basename."""

    source_path = ROOT / "utilities" / "capability-route.py"
    spec = importlib.util.spec_from_file_location(
        "dispatch_recovery_capability_route", source_path
    )
    if spec is None or spec.loader is None:
        raise RecoveryError("continuation-command-unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        preview = module.build_continuation_route(
            context.route,
            resume_from_node=request.resume_from_node,
            requested_boundary=request.requested_boundary,
            reason=request.reason,
            artifact_root=context.artifact_root,
            partial_group={
                "source_group_id": context.group_id,
                "source_batch_manifest": context.manifest,
                "failed_source_attempt_id": request.original_attempt_id,
                "gap_leg_id": context.gap_leg_id,
            },
        )
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise RecoveryError("continuation-preview-failed", str(exc)) from exc
    if not isinstance(preview, dict):
        raise RecoveryError("continuation-preview-invalid")
    return preview


def _attempt_rows(jobs: Path, attempt_id: str) -> list[AttemptSnapshot]:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise RecoveryError("recovery-registry-unreadable", str(exc)) from exc
    rows: list[AttemptSnapshot] = []
    for raw in lines:
        fields = raw.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("attempt_id") != attempt_id:
            continue
        validate_attempt_metadata(metadata)
        rows.append(
            AttemptSnapshot(
                status=fields[1],
                repo=fields[2],
                worktree=fields[3],
                slug=fields[4],
                metadata=metadata,
                row_digest="sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            )
        )
    return rows


def exact_attempt(jobs: Path, attempt_id: str) -> AttemptSnapshot:
    rows = _attempt_rows(jobs, attempt_id)
    if len(rows) != 1:
        raise RecoveryError(
            "attempt-row-not-unique", f"attempt_id={attempt_id} rows={len(rows)}"
        )
    return rows[0]


def _cancellation_evidence(source: AttemptSnapshot) -> dict[str, object] | None:
    metadata = source.metadata
    digest = metadata.get("cancellation_receipt_digest", "")
    if (
        source.status != "done"
        or metadata.get("cancellation_quiescence_receipt")
        != ATTEMPT_CANCELLATION_QUIESCENCE_RECEIPT
        or not digest.startswith("sha256:")
        or metadata.get("classifier_source") != "automatic-receipt-unavailable-v1"
        or metadata.get("note")
        not in {
            "cancelled-receipt-unavailable",
            "receipt-unavailable-retry-exhausted",
        }
        or metadata.get("failure_class") not in {"cancelled", "blocked"}
    ):
        return None
    return {
        "attempt_id": metadata["attempt_id"],
        "receipt_type": metadata["cancellation_quiescence_receipt"],
        "receipt_digest": digest,
    }


def _claim_from_source(
    source: AttemptSnapshot, recovery_identity: str
) -> RecoveryRetryClaim | None:
    metadata = source.metadata
    if metadata.get("recovery_id") != recovery_identity:
        return None
    if metadata.get("note") == "receipt-unavailable-retry-exhausted":
        return RecoveryRetryClaim(
            recovery_identity,
            metadata["attempt_id"],
            0,
            "",
            "exhausted",
            "receipt-unavailable-retry-exhausted",
            False,
        )
    retry_attempt_id = metadata.get("retry_attempt_id", "")
    if metadata.get("retry_ordinal") != "1" or not retry_attempt_id:
        raise RecoveryError("recovery-claim-incomplete")
    return RecoveryRetryClaim(
        recovery_identity,
        metadata["attempt_id"],
        1,
        retry_attempt_id,
        "claimed",
        "recovery-retry-claimed",
        True,
    )


def _new_record(
    request: RecoveryRequest, source: AttemptSnapshot, recovery_identity: str
) -> dict[str, object]:
    metadata = source.metadata
    return {
        "schema_version": RECOVERY_RECORD_VERSION,
        "recovery_id": recovery_identity,
        "source": {
            "jobs": str(request.jobs.resolve()),
            "attempt_id": request.original_attempt_id,
            "route_id": metadata.get("route_id", ""),
            "route_hash": metadata.get("route_hash", ""),
            "node_or_group_leg": metadata.get("route_node")
            or metadata.get("batch_route_node", ""),
        },
        "current_phase": "",
        "phases": {},
    }


def _load_record(
    path: Path,
    request: RecoveryRequest,
    source: AttemptSnapshot,
    recovery_identity: str,
) -> dict[str, object]:
    if not path.is_file():
        return _new_record(request, source, recovery_identity)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RecoveryError("recovery-record-invalid", str(exc)) from exc
    expected = _new_record(request, source, recovery_identity)["source"]
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != RECOVERY_RECORD_VERSION
        or record.get("recovery_id") != recovery_identity
        or record.get("source") != expected
        or not isinstance(record.get("phases"), dict)
    ):
        raise RecoveryError("recovery-record-binding-mismatch")
    return record


def _commit_phase(
    path: Path,
    record: dict[str, object],
    phase: str,
    evidence: dict[str, object],
) -> bool:
    if phase not in PHASES:
        raise RecoveryError("recovery-phase-invalid", phase)
    phases = record["phases"]
    assert isinstance(phases, dict)
    prior = phases.get(phase)
    if prior is not None:
        if not isinstance(prior, dict) or prior.get("evidence") != evidence:
            raise RecoveryError("recovery-phase-evidence-conflict", phase)
        return False
    position = PHASES.index(phase)
    missing = [candidate for candidate in PHASES[:position] if candidate not in phases]
    if missing and phase != "terminal-or-blocked":
        raise RecoveryError("recovery-phase-gap", ",".join(missing))
    phases[phase] = {"committed_at_ns": time.time_ns(), "evidence": evidence}
    record["current_phase"] = phase
    _atomic_json(path, record)
    return True


def _checkpoint(
    checkpoint: Callable[[str], None] | None, phase: str, committed: bool
) -> None:
    if committed and checkpoint is not None:
        checkpoint(phase)


def _phase_evidence(record: dict[str, object], phase: str) -> dict[str, object] | None:
    phases = record.get("phases")
    if not isinstance(phases, dict):
        return None
    item = phases.get(phase)
    return item.get("evidence") if isinstance(item, dict) else None


def _seal_intent(
    request: RecoveryRequest,
    source: AttemptSnapshot,
    evidence: dict[str, object],
) -> tuple[Path, bool]:
    path = recovery_intent_path(request)
    payload = {
        "schema_version": RECOVERY_RECORD_VERSION,
        "intent_id": path.stem,
        "source": {
            "jobs": str(request.jobs.resolve()),
            "attempt_id": request.original_attempt_id,
            "route_id": source.metadata.get("route_id", ""),
            "route_hash": source.metadata.get("route_hash", ""),
            "node_or_group_leg": source.metadata.get("route_node")
            or source.metadata.get("batch_route_node", ""),
        },
        "current_phase": "intent-observed",
        "phases": {
            "intent-observed": {"evidence": evidence},
        },
    }
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryError("recovery-intent-invalid") from exc
        if existing != payload:
            raise RecoveryError("recovery-intent-binding-mismatch")
        return path, False
    _atomic_json(path, payload)
    return path, True


def _blocked_result(
    path: Path,
    record: dict[str, object],
    recovery_identity: str,
    reason: str,
    claim: RecoveryRetryClaim | None,
    checkpoint: Callable[[str], None] | None,
) -> RecoveryResult:
    evidence = {
        "outcome": "blocked",
        "reason": reason or "receipt-unavailable-retry-exhausted",
        "retry_attempt_id": claim.retry_attempt_id if claim else "",
        "start_permitted": False,
    }
    source = record.get("source")
    if not isinstance(source, dict):
        raise RecoveryError("recovery-record-binding-mismatch")
    try:
        seal_recovery_blocked(
            Path(str(source["jobs"])),
            original_attempt_id=str(source["attempt_id"]),
            recovery_id=recovery_identity,
            reason=str(evidence["reason"]),
        )
    except (KeyError, DispatchContractError) as exc:
        if isinstance(exc, DispatchContractError):
            raise RecoveryError(exc.reason, exc.detail) from exc
        raise RecoveryError("recovery-record-binding-mismatch") from exc
    committed = _commit_phase(path, record, "terminal-or-blocked", evidence)
    _checkpoint(checkpoint, "terminal-or-blocked", committed)
    return RecoveryResult(
        recovery_identity,
        "terminal-or-blocked",
        "blocked",
        evidence["reason"],
        evidence["retry_attempt_id"],
        0,
        str(path),
    )


def _validate_continuation(
    continuation: dict[str, object], recovery_identity: str
) -> dict[str, object]:
    if not continuation.get("admitted", False):
        return continuation
    required = ("continuation_id", "continuation_path", "continuation_digest", "gap_leg_id")
    if any(not isinstance(continuation.get(key), str) or not continuation[key] for key in required):
        raise RecoveryError("recovery-continuation-evidence-invalid")
    if continuation.get("recovery_id") != recovery_identity:
        raise RecoveryError("recovery-continuation-binding-mismatch")
    return continuation


def _validate_start(
    start: dict[str, object], claim: RecoveryRetryClaim
) -> dict[str, object]:
    if not start.get("admitted", False):
        return start
    if (
        start.get("retry_attempt_id") != claim.retry_attempt_id
        or start.get("registered") != 1
        or start.get("started") != 1
        or start.get("child_spawned") != 1
    ):
        raise RecoveryError("recovery-start-evidence-invalid")
    return start


def coordinate_recovery(
    request: RecoveryRequest,
    services: RecoveryServices,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> RecoveryResult:
    """Resume one exact recovery from durable evidence and execute only its first gap."""

    request = RecoveryRequest(
        jobs=request.jobs.resolve(),
        original_attempt_id=request.original_attempt_id,
        route_file=request.route_file.resolve(),
        resume_from_node=request.resume_from_node,
        requested_boundary=request.requested_boundary,
        reason=request.reason,
        cancellation_wait=request.cancellation_wait,
    )
    if (
        not request.original_attempt_id
        or not request.resume_from_node
        or not request.requested_boundary
        or request.cancellation_wait < 0
    ):
        raise RecoveryError("recovery-request-invalid")
    lock_path = _attempt_lock_path(request)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        source = exact_attempt(request.jobs, request.original_attempt_id)
        intent = {
            "attempt_id": request.original_attempt_id,
            "requested_boundary": request.requested_boundary,
            "resume_from_node": request.resume_from_node,
            "reason": request.reason,
        }
        _intent_path, intent_committed = _seal_intent(request, source, intent)
        _checkpoint(checkpoint, "intent-observed", intent_committed)
        cancellation = _cancellation_evidence(source)
        if cancellation is None:
            services.cancel_receiptless(request)
            source = exact_attempt(request.jobs, request.original_attempt_id)
            cancellation = _cancellation_evidence(source)
        if cancellation is None:
            raise RecoveryError("cancellation-quiescence-unproven")

        metadata = source.metadata
        node_or_group_leg = metadata.get("route_node") or metadata.get(
            "batch_route_node", ""
        )
        recovery_identity = canonical_recovery_id(
            source_route_id=metadata.get("route_id", ""),
            source_route_hash=metadata.get("route_hash", ""),
            node_or_group_leg=node_or_group_leg,
            original_attempt_id=request.original_attempt_id,
            cancellation_receipt_digest=str(cancellation["receipt_digest"]),
        )
        record_path = recovery_record_path(request.jobs, recovery_identity)
        record = _load_record(record_path, request, source, recovery_identity)

        committed = _commit_phase(record_path, record, "intent-observed", intent)
        committed = _commit_phase(
            record_path, record, "cancellation-receipt-committed", cancellation
        )
        _checkpoint(checkpoint, "cancellation-receipt-committed", committed)

        terminal = _phase_evidence(record, "terminal-or-blocked")
        if terminal is not None:
            return RecoveryResult(
                recovery_identity,
                "terminal-or-blocked",
                str(terminal.get("outcome", "blocked")),
                str(terminal.get("reason", "")),
                str(terminal.get("retry_attempt_id", "")),
                0,
                str(record_path),
            )

        continuation = services.observe_continuation(request, recovery_identity)
        sealed_continuation = _phase_evidence(record, "continuation-published")
        if sealed_continuation is not None and continuation is None:
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                "continuation-evidence-missing",
                None,
                checkpoint,
            )
        if continuation is None:
            continuation = services.publish_continuation(
                request, source, recovery_identity
            )
        continuation = _validate_continuation(continuation, recovery_identity)
        if not continuation.get("admitted", False):
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                str(continuation.get("reason") or "recovery-admission-impossible"),
                None,
                checkpoint,
            )
        committed = _commit_phase(
            record_path, record, "continuation-published", continuation
        )
        _checkpoint(checkpoint, "continuation-published", committed)

        source = exact_attempt(request.jobs, request.original_attempt_id)
        claim = _claim_from_source(source, recovery_identity)
        if claim is None:
            remaining = services.remaining_cascade(
                request, source, recovery_identity
            )
            try:
                claim = claim_recovery_retry(
                    request.jobs,
                    recovery_id=recovery_identity,
                    source_route_id=source.metadata.get("route_id", ""),
                    source_route_hash=source.metadata.get("route_hash", ""),
                    node_or_group_leg=node_or_group_leg,
                    original_attempt_id=request.original_attempt_id,
                    remaining_cascade=remaining,
                )
            except DispatchContractError as exc:
                raise RecoveryError(exc.reason, exc.detail) from exc
        claim_evidence = {
            "state": claim.state,
            "reason": claim.reason,
            "retry_ordinal": claim.retry_ordinal,
            "retry_attempt_id": claim.retry_attempt_id,
            "start_permitted": claim.start_permitted,
        }
        committed = _commit_phase(
            record_path, record, "retry-claimed", claim_evidence
        )
        _checkpoint(checkpoint, "retry-claimed", committed)
        if not claim.start_permitted:
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                "receipt-unavailable-retry-exhausted",
                claim,
                checkpoint,
            )

        start = services.observe_start(request, continuation, claim)
        sealed_start = _phase_evidence(record, "wrapper-started")
        if sealed_start is not None and start is None:
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                "wrapper-start-evidence-missing",
                claim,
                checkpoint,
            )
        if sealed_start is not None and start is not None:
            for key in ("retry_attempt_id", "registered", "started", "child_spawned", "pid", "pid_start"):
                if sealed_start.get(key) != start.get(key):
                    return _blocked_result(
                        record_path,
                        record,
                        recovery_identity,
                        "wrapper-start-evidence-drift",
                        claim,
                        checkpoint,
                    )
            start = sealed_start
        if start is None:
            start = services.start_gap(request, continuation, claim)
        start = _validate_start(start, claim)
        if not start.get("admitted", False):
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                str(start.get("reason") or "recovery-admission-impossible"),
                claim,
                checkpoint,
            )
        committed = _commit_phase(record_path, record, "wrapper-started", start)
        _checkpoint(checkpoint, "wrapper-started", committed)

        terminal = services.observe_terminal(request, claim)
        if terminal is None:
            return RecoveryResult(
                recovery_identity,
                "wrapper-started",
                "in-progress",
                "retry-running",
                claim.retry_attempt_id,
                int(start.get("child_spawned", 0)),
                str(record_path),
            )
        outcome = str(terminal.get("outcome", ""))
        if outcome != "terminal":
            return _blocked_result(
                record_path,
                record,
                recovery_identity,
                str(terminal.get("reason") or "receipt-unavailable-retry-exhausted"),
                claim,
                checkpoint,
            )
        terminal_evidence = {
            **terminal,
            "retry_attempt_id": claim.retry_attempt_id,
            "start_permitted": False,
        }
        committed = _commit_phase(
            record_path, record, "terminal-or-blocked", terminal_evidence
        )
        _checkpoint(checkpoint, "terminal-or-blocked", committed)
        return RecoveryResult(
            recovery_identity,
            "terminal-or-blocked",
            "terminal",
            str(terminal.get("reason", "retry-terminal")),
            claim.retry_attempt_id,
            0,
            str(record_path),
        )


def _partial_identity(
    route: dict[str, object], context: SourceBatchContext, request: RecoveryRequest
) -> dict[str, object]:
    if "recovery_id" in route:
        raise RecoveryError("recovery-id-forbidden-in-continuation-route")
    partial = route.get("partial_group_continuation")
    if not isinstance(partial, dict) or "recovery_id" in partial:
        raise RecoveryError("recovery-continuation-partial-binding-mismatch")
    expected_legs = {
        str(member["route_node"]): context.leg_digests[str(member["attempt_id"])]
        for member in context.manifest["members"]
    }
    group = next(
        row for row in context.route["parallel_groups"]
        if isinstance(row, dict) and row.get("id") == context.group_id
    )
    if (
        route.get("continuation_contract_version") != 1
        or route.get("source_route_id") != context.route.get("route_id")
        or route.get("source_route_hash") != context.route.get("route_hash")
        or route.get("resume_from_node") != request.resume_from_node
        or route.get("requested_boundary") != request.requested_boundary
        or route.get("reason") != request.reason
        or partial.get("contract_version") != 1
        or partial.get("source_group_id") != context.group_id
        or partial.get("source_batch_manifest_digest") != context.manifest_digest
        or partial.get("leg_manifest_digests") != expected_legs
        or partial.get("original_group_cardinality") != context.cardinality
        or partial.get("join_policy") != group.get("join_policy")
        or partial.get("failed_source_attempt_id") != request.original_attempt_id
        or partial.get("gap_leg_id") != context.gap_leg_id
    ):
        raise RecoveryError("recovery-continuation-partial-binding-mismatch")
    string_fields = (
        "reused_peer_set_proof_digest",
        "replacement_leg_identity",
        "replacement_attempt_id",
    )
    if any(not isinstance(partial.get(key), str) or not partial[key] for key in string_fields):
        raise RecoveryError("recovery-continuation-partial-binding-mismatch")
    return {
        "source_group_id": partial["source_group_id"],
        "source_batch_manifest_digest": partial["source_batch_manifest_digest"],
        "failed_source_attempt_id": partial["failed_source_attempt_id"],
        "gap_leg_id": partial["gap_leg_id"],
        "original_group_cardinality": partial["original_group_cardinality"],
        "reused_peer_set_proof_digest": partial["reused_peer_set_proof_digest"],
        "replacement_leg_identity": partial["replacement_leg_identity"],
        "replacement_attempt_id": partial["replacement_attempt_id"],
    }


def _official_continuation_path(
    route: dict[str, object], context: SourceBatchContext
) -> Path:
    route_id = route.get("route_id")
    route_hash = route.get("route_hash")
    if (
        not isinstance(route_id, str)
        or not route_id.startswith("rt-")
        or not isinstance(route_hash, str)
        or not route_hash.startswith("sha256:")
    ):
        raise RecoveryError("recovery-continuation-route-identity-invalid")
    return (
        context.artifact_root / ".runtime" / "routes" / f"{route_id}.json"
    ).resolve(strict=False)


def _continuation_envelope(
    *,
    recovery_identity: str,
    request: RecoveryRequest,
    context: SourceBatchContext,
    route: dict[str, object],
    route_path: Path,
    route_digest: str,
) -> dict[str, object]:
    continuation_id = route.get("continuation_id")
    if not isinstance(continuation_id, str) or not continuation_id:
        raise RecoveryError("recovery-continuation-route-identity-invalid")
    expected_path = _official_continuation_path(route, context)
    if route_path.resolve(strict=False) != expected_path:
        raise RecoveryError("recovery-continuation-route-path-mismatch")
    partial_identity = _partial_identity(route, context, request)
    return {
        "schema_version": 1,
        "recovery_id": recovery_identity,
        "source_route_path": str(request.route_file.resolve(strict=False)),
        "source_route_digest": context.source_route_digest,
        "route_path": str(expected_path),
        "route_digest": route_digest,
        "route_id": route["route_id"],
        "route_hash": route["route_hash"],
        "continuation_id": continuation_id,
        "partial_group_identity": partial_identity,
    }


def _seal_continuation_envelope(
    path: Path, payload: dict[str, object]
) -> None:
    if path.is_file():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryError("recovery-continuation-evidence-invalid") from exc
        if prior != payload:
            raise RecoveryError("recovery-continuation-envelope-conflict")
        return
    _atomic_json(path, payload)


def _receipt_integer(value: object, expected: int) -> bool:
    return not isinstance(value, bool) and value in {expected, str(expected)}


class ProductionRecoveryServices(RecoveryServices):
    """Checked production boundary for one official partial continuation."""

    def cancel_receiptless(self, request: RecoveryRequest) -> dict[str, object]:
        command = [
            sys.executable,
            str(ROOT / "utilities" / "dispatch-registry.py"),
            "reconcile",
            "--jobs",
            str(request.jobs),
            "--attempt",
            request.original_attempt_id,
            "--automatic-cancel-receiptless",
            "--apply",
            "--cancellation-wait",
            str(request.cancellation_wait),
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            raise RecoveryError(
                "registry-cancellation-command-failed",
                completed.stderr.strip() or completed.stdout.strip(),
            )
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise RecoveryError("registry-cancellation-receipt-invalid") from exc
        if result.get("closed") != 1:
            decision = (result.get("decisions") or [{}])[0]
            raise RecoveryError(
                str(decision.get("reason") or "cancellation-quiescence-unproven")
            )
        return result

    def remaining_cascade(
        self, request: RecoveryRequest, source: AttemptSnapshot, recovery_identity: str
    ) -> int:
        budget = resolve_continuation_budget(
            route_file=request.route_file,
            route_id=source.metadata.get("route_id", ""),
            route_hash=source.metadata.get("route_hash", ""),
            expected_cwd=source.worktree,
        )
        return budget.retry_slots

    def observe_continuation(
        self, request: RecoveryRequest, recovery_identity: str
    ) -> dict[str, object] | None:
        path = continuation_record_path(request.jobs, recovery_identity)
        if not path.is_file():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryError("recovery-continuation-evidence-invalid") from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("schema_version") != 1
            or envelope.get("recovery_id") != recovery_identity
            or envelope.get("source_route_path")
            != str(request.route_file.resolve(strict=False))
        ):
            raise RecoveryError("recovery-continuation-binding-mismatch")
        source = exact_attempt(request.jobs, request.original_attempt_id)
        context = _source_batch_context(request, source)
        if envelope.get("source_route_digest") != context.source_route_digest:
            raise RecoveryError("recovery-continuation-source-route-drift")
        route_path_value = envelope.get("route_path")
        if not isinstance(route_path_value, str) or not route_path_value:
            raise RecoveryError("recovery-continuation-binding-mismatch")
        route_path = Path(route_path_value).resolve(strict=False)
        try:
            route_raw = route_path.read_bytes()
        except OSError as exc:
            raise RecoveryError("recovery-continuation-route-missing", str(exc)) from exc
        route_digest = _sha256_bytes(route_raw)
        if route_digest != envelope.get("route_digest"):
            raise RecoveryError("recovery-continuation-route-drift")
        route = _json_object(route_raw, "recovery-continuation-route-invalid")
        expected = _continuation_envelope(
            recovery_identity=recovery_identity,
            request=request,
            context=context,
            route=route,
            route_path=route_path,
            route_digest=route_digest,
        )
        if envelope != expected:
            raise RecoveryError("recovery-continuation-binding-mismatch")
        partial = expected["partial_group_identity"]
        assert isinstance(partial, dict)
        return {
            "admitted": True,
            "recovery_id": recovery_identity,
            "continuation_id": expected["continuation_id"],
            "continuation_path": str(route_path),
            "continuation_digest": route_digest,
            "gap_leg_id": partial["gap_leg_id"],
            "source_group_id": partial["source_group_id"],
            "failed_source_attempt_id": partial["failed_source_attempt_id"],
            "source_route_path": expected["source_route_path"],
            "envelope_path": str(path),
        }

    def publish_continuation(
        self,
        request: RecoveryRequest,
        source: AttemptSnapshot,
        recovery_identity: str,
    ) -> dict[str, object]:
        context = _source_batch_context(request, source)
        preview = _preview_continuation_route(request, context)
        output_path = _official_continuation_path(preview, context)
        temporary_root = request.jobs.parent / "recovery" / "tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=f".{recovery_identity}.", dir=str(temporary_root)
            ) as temporary:
                manifest_path = Path(temporary) / "source-batch-manifest.json"
                _atomic_json(manifest_path, context.manifest)
                command = [
                    sys.executable,
                    str(ROOT / "utilities" / "capability-route.py"),
                    "continuation",
                    "--source-route",
                    str(request.route_file),
                    "--resume-from-node",
                    request.resume_from_node,
                    "--requested-boundary",
                    request.requested_boundary,
                    "--reason",
                    request.reason,
                    "--artifact-root",
                    str(context.artifact_root),
                    "--partial-group-manifest",
                    str(manifest_path),
                    "--source-group-id",
                    context.group_id,
                    "--failed-source-attempt-id",
                    request.original_attempt_id,
                    "--gap-leg-id",
                    context.gap_leg_id,
                    "--output",
                    str(output_path),
                ]
                completed = subprocess.run(
                    command, text=True, capture_output=True, check=False
                )
                route = _command_receipt(
                    completed,
                    failed_reason="continuation-command-failed",
                    invalid_reason="continuation-command-receipt-invalid",
                )
        except OSError as exc:
            raise RecoveryError("continuation-command-unavailable", str(exc)) from exc

        if route != preview:
            raise RecoveryError("recovery-continuation-preview-drift")
        route_path = _official_continuation_path(route, context)
        try:
            route_raw = route_path.read_bytes()
        except OSError as exc:
            raise RecoveryError("recovery-continuation-route-missing", str(exc)) from exc
        published = _json_object(route_raw, "recovery-continuation-route-invalid")
        if published != route:
            raise RecoveryError("recovery-continuation-command-route-mismatch")
        envelope = _continuation_envelope(
            recovery_identity=recovery_identity,
            request=request,
            context=context,
            route=published,
            route_path=route_path,
            route_digest=_sha256_bytes(route_raw),
        )
        _seal_continuation_envelope(
            continuation_record_path(request.jobs, recovery_identity), envelope
        )
        observed = self.observe_continuation(request, recovery_identity)
        if observed is None:
            raise RecoveryError("recovery-continuation-evidence-missing")
        return observed

    def observe_start(
        self,
        request: RecoveryRequest,
        continuation: dict[str, object],
        claim: RecoveryRetryClaim,
    ) -> dict[str, object] | None:
        rows = _attempt_rows(request.jobs, claim.retry_attempt_id)
        if not rows:
            return None
        if len(rows) != 1:
            raise RecoveryError("retry-attempt-row-not-unique")
        row = rows[0]
        metadata = row.metadata
        if metadata.get("launch_started") != "1":
            return None
        if (
            metadata.get("route_node") != continuation.get("gap_leg_id")
            or metadata.get("batch_route_node") != continuation.get("gap_leg_id")
            or metadata.get("batch_group") != continuation.get("source_group_id")
            or metadata.get("attempt_id") != claim.retry_attempt_id
        ):
            raise RecoveryError("recovery-start-row-binding-mismatch")
        live = bool(
            metadata.get("pid", "").isdigit()
            and metadata.get("pid_start")
            and process_identity_is_live(
                int(metadata["pid"]), metadata["pid_start"]
            )
        )
        return {
            "admitted": True,
            "retry_attempt_id": claim.retry_attempt_id,
            "registered": 1,
            "started": 1,
            "child_spawned": 1,
            "row_count": 1,
            "process_count": int(live),
            "pid": metadata.get("pid", ""),
            "pid_start": metadata.get("pid_start", ""),
        }

    def start_gap(
        self,
        request: RecoveryRequest,
        continuation: dict[str, object],
        claim: RecoveryRetryClaim,
    ) -> dict[str, object]:
        source = exact_attempt(request.jobs, request.original_attempt_id)
        context = _source_batch_context(request, source)
        observed_continuation = self.observe_continuation(
            request, claim.recovery_id
        )
        if observed_continuation != continuation:
            raise RecoveryError("recovery-continuation-binding-mismatch")
        if (
            claim.original_attempt_id != request.original_attempt_id
            or not claim.retry_attempt_id
            or continuation.get("source_group_id") != context.group_id
            or continuation.get("gap_leg_id") != context.gap_leg_id
        ):
            raise RecoveryError("recovery-gap-claim-binding-mismatch")
        if (
            os.environ.get("AGENT_DISPATCH_SELF_SLUG") != context.parent_slug
            or os.environ.get("AGENT_DISPATCH_ATTEMPT_ID")
            != context.parent_attempt_id
        ):
            raise RecoveryError("recovery-parent-identity-mismatch")
        command = [
            sys.executable,
            str(ROOT / "utilities" / "dispatch-batch.py"),
            "--route",
            str(request.route_file),
            "--continuation",
            str(continuation["continuation_path"]),
            "--parallel-group",
            context.group_id,
            "--action",
            "start",
            "--jobs",
            str(request.jobs),
            "--slug-prefix",
            source.slug,
            "--parent",
            context.parent_slug,
        ]
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
        except OSError as exc:
            raise RecoveryError("gap-command-unavailable", str(exc)) from exc
        receipt = _command_receipt(
            completed,
            failed_reason="gap-command-failed",
            invalid_reason="gap-command-receipt-invalid",
        )
        legs = receipt.get("legs")
        if (
            receipt.get("schema_version") != 2
            or receipt.get("action") != "start"
            or receipt.get("parallel_group") != context.group_id
            or receipt.get("continuation_id") != continuation.get("continuation_id")
            or receipt.get("replacement_attempt_id") != claim.retry_attempt_id
            or not _receipt_integer(
                receipt.get("original_group_cardinality"), context.cardinality
            )
            or not _receipt_integer(
                receipt.get("reused_peer_count"), context.cardinality - 1
            )
            or not _receipt_integer(receipt.get("newly_started"), 1)
            or not _receipt_integer(receipt.get("existing"), context.cardinality - 1)
            or not isinstance(legs, list)
            or len(legs) != context.cardinality
        ):
            raise RecoveryError("recovery-gap-receipt-binding-mismatch")
        gap_rows = [
            leg for leg in legs
            if isinstance(leg, dict)
            and leg.get("node") == context.gap_leg_id
            and leg.get("attempt_id") == claim.retry_attempt_id
        ]
        peers = [
            leg for leg in legs
            if isinstance(leg, dict) and leg.get("node") != context.gap_leg_id
        ]
        if len(gap_rows) != 1 or len(peers) != context.cardinality - 1:
            raise RecoveryError("recovery-gap-receipt-binding-mismatch")
        expected_peers = {
            (str(member["route_node"]), str(member["attempt_id"]))
            for member in context.manifest["members"]
            if member["route_node"] != context.gap_leg_id
        }
        actual_peers = {
            (str(peer.get("node")), str(peer.get("attempt_id"))) for peer in peers
        }
        if actual_peers != expected_peers:
            raise RecoveryError("recovery-gap-receipt-binding-mismatch")
        gap = gap_rows[0]
        if (
            gap.get("launch_state") != "started"
            or any(
                not _receipt_integer(gap.get(key), 1)
                for key in ("registered", "started", "child_spawned")
            )
            or any(
                peer.get("launch_state") != "existing"
                or peer.get("reason") != "reused-successful-peer"
                or any(
                    not _receipt_integer(peer.get(key), 0)
                    for key in ("registered", "started", "child_spawned")
                )
                for peer in peers
            )
        ):
            raise RecoveryError("recovery-gap-receipt-evidence-invalid")
        observed = self.observe_start(request, continuation, claim)
        if observed is None:
            raise RecoveryError("recovery-gap-start-observation-missing")
        return {
            **observed,
            "batch_receipt_digest": _sha256_bytes(_canonical_bytes(receipt)),
            "continuation_id": continuation["continuation_id"],
            "source_group_id": context.group_id,
        }

    def observe_terminal(
        self, request: RecoveryRequest, claim: RecoveryRetryClaim
    ) -> dict[str, object] | None:
        rows = _attempt_rows(request.jobs, claim.retry_attempt_id)
        if len(rows) != 1 or rows[0].status not in {"done", "killed", "cancelled"}:
            return None
        metadata = rows[0].metadata
        return {
            "outcome": "terminal",
            "reason": metadata.get("note") or "retry-terminal",
            "status": rows[0].status,
            "verdict": metadata.get("failure_class", ""),
            "row_digest": rows[0].row_digest,
        }


def _attempt_rows_for_route(jobs: Path, route_id: str) -> list[AttemptSnapshot]:
    try:
        lines = jobs.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[AttemptSnapshot] = []
    for raw in lines:
        fields = raw.split("\t")
        if len(fields) != 6:
            continue
        metadata = parse_registry_metadata(fields[5])
        if metadata.get("route_id") != route_id or not metadata.get("attempt_id"):
            continue
        rows.append(
            AttemptSnapshot(
                fields[1], fields[2], fields[3], fields[4], metadata,
                "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            )
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--route-file", type=Path, required=True)
    parser.add_argument("--resume-from-node", required=True)
    parser.add_argument("--requested-boundary", required=True)
    parser.add_argument("--reason", default="receipt-unavailable-recovery")
    parser.add_argument("--cancellation-wait", type=float, default=2.0)
    args = parser.parse_args(argv)
    request = RecoveryRequest(
        args.jobs,
        args.attempt_id,
        args.route_file,
        args.resume_from_node,
        args.requested_boundary,
        args.reason,
        args.cancellation_wait,
    )
    try:
        result = coordinate_recovery(request, ProductionRecoveryServices())
    except RecoveryError as exc:
        print(json.dumps({"state": "blocked", "reason": exc.reason}, sort_keys=True))
        return 69 if isinstance(exc, RecoveryInterfaceUnavailable) else 65
    print(json.dumps(result.as_json(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
