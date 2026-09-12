#!/usr/bin/env python3
"""Durable, forward-only terminal settlement primitives (SD-120/121)."""
from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from contextlib import contextmanager

import artifact_lifecycle
import route_identity
import dispatch_contract
import dispatch_lock_order

_TOPOLOGY = None

TERMINAL_RESULTS = frozenset({"completed", "recoverable", "needs-owner", "ineligible"})
TERMINAL_REASONS = frozenset({
    "route-identity-unverified", "terminal-marker-not-current", "child-not-quiescent",
    "producer-binding-required", "producer-binding-mismatch", "route-close-failed",
    "producer-finalize-failed", "transaction-conflict", "recovery-unavailable",
})
CLEANUP_VERDICTS = frozenset({"allowed", "denied-operation", "denied-target",
                              "denied-route", "denied-cycle", "denied-expired", "denied-consumed"})


@dataclass(frozen=True)
class CleanupVerdict:
    verdict: str
    detail: str = ""

_DETAIL_REASON_MAP = {
    "owner-route-mismatch": "route-identity-unverified",
    "terminal-attempt-not-pass": "terminal-marker-not-current",
    "child-not-terminal": "child-not-quiescent",
    "active-retry": "child-not-quiescent",
    "active-review-lease": "producer-finalize-failed",
    "binding-cycle-not-open": "producer-binding-mismatch",
}


def producer_lifecycle_applies(route: Mapping[str, Any]) -> bool:
    """Return whether the canonical topology declares producer lifecycle here."""
    global _TOPOLOGY
    if not isinstance(route, Mapping):
        return False
    if _TOPOLOGY is None:
        topology_spec = importlib.util.spec_from_file_location(
            "terminal_capability_topology",
            Path(__file__).resolve().parents[1] / "tools" / "capability_topology.py",
        )
        if topology_spec is None or topology_spec.loader is None:
            return False
        _TOPOLOGY = importlib.util.module_from_spec(topology_spec)
        topology_spec.loader.exec_module(_TOPOLOGY)
    capability, mode = route.get("capability"), route.get("capability_mode", "default")
    registry = _TOPOLOGY.load_registry(Path(__file__).resolve().parents[1] / "capabilities" / "topologies.json")
    manifest = json.loads((Path(__file__).resolve().parents[1] / "harness-manifest.json").read_text(encoding="utf-8"))
    known = ((capability, mode) in _TOPOLOGY.recipe_keys(registry)
             and (capability, mode) in _TOPOLOGY.expected_recipe_keys(manifest))
    lifecycle = registry.get("producer_lifecycle", {})
    intensity = route.get("effective_intensity", "standard")
    intensity_key = intensity if intensity in {"direct", "quick"} else "standard+"
    return bool(known and lifecycle.get("contract") == "artifact-producer/v1"
                and lifecycle.get("by_intensity", {}).get(intensity_key, {}).get("finalize"))

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
CONTRACT = "producer_binding_v1"


class TerminalCommitError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class ProducerBindingResult:
    status: str
    path: Path
    binding: Optional[Mapping[str, Any]] = None
    digest: Optional[str] = None
    replay: bool = False

    def __getitem__(self, key: str) -> Any:
        return {"status": self.status, "path": self.path, "binding": self.binding,
                "digest": self.digest, "replay": self.replay}[key]


@dataclass(frozen=True)
class TerminalCommitRequest:
    route_file: Path
    owner_attempt_id: str
    jobs: Path
    artifact_root: Path


@dataclass(frozen=True)
class TerminalProof:
    status: str
    reason: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class TerminalCommitResult:
    result: str
    reason: Optional[str] = None
    detail: Optional[str] = None
    terminal_nodes: tuple[str, ...] = ()
    envelope_text: Optional[str] = None


def _default_close_route(route, route_file, **kwargs):
    module = _route_module()
    kwargs.setdefault("allow_unproven", False)
    return module.close_route(route, route_file, **kwargs)


def _producer_operation(operation, *args, **kwargs):
    import artifact_admission
    import artifact_producer
    try:
        return operation(*args, **kwargs)
    except (artifact_producer.ProducerError, artifact_admission.AdmissionBusy,
            artifact_admission.AdmissionRecoveryRequired) as exc:
        # Publication can commit before producer bookkeeping does. Preserve
        # the transaction checkpoint so exact recovery can finish that cycle.
        raise TerminalCommitError("producer-finalize-failed", str(exc)) from exc


def _default_finalize_exact_cycle(root, *, cycle_id, expected_binding, **kwargs):
    import artifact_producer
    return _producer_operation(artifact_producer.finalize_exact_cycle,
        root, cycle_id=cycle_id, expected_binding=expected_binding, **kwargs)


def _default_seal_envelope(**kwargs):
    return _seal_owner_envelope(**kwargs)


@dataclass(frozen=True)
class TerminalCommitServices:
    close_route: Any = _default_close_route
    finalize_exact_cycle: Any = _default_finalize_exact_cycle
    seal_envelope: Any = _default_seal_envelope
    crash_after: Optional[str] = None


@dataclass(frozen=True)
class CleanupScope:
    artifact_root: Path
    route_id: str
    allowed_targets: tuple[Path, ...] = ()
    owner_attempt_id: str = ""
    route_hash: str = ""
    terminal_commit_id: str = ""
    claim_id: str = ""
    intent_id: str = ""
    cycle_id: Optional[str] = None
    allowed_write_roots: tuple[Path, ...] = ()
    allowed_read_roots: tuple[Path, ...] = ()
    allowed_recovery_targets: tuple[Path, ...] = ()
    allowed_operations: tuple[str, ...] = ("partial-report", "final-report", "close-forward-recovery",
                                            "finalize-forward-recovery", "read", "verify")
    expires_at: Optional[float] = None
    one_use: bool = True
    consumed: bool = False
    scope_digest: str = ""


def terminal_slot(artifact_root: Path, route_id: str, owner_attempt_id: str) -> Path:
    return Path(artifact_root).resolve() / ".runtime" / "terminal-commits" / "v1" / _component(route_id, "route") / _component(owner_attempt_id, "owner")


def _atomic_json(path: Path, value: Mapping[str, Any], *, exclusive: bool = False) -> None:
    data = _canonical(dict(value)) + b"\n"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if exclusive:
        _write_exclusive(path, data)
        return
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        try: temp.unlink()
        except OSError: pass


def terminal_marker_digest(markers: list[Mapping[str, Any]]) -> str:
    fields = ("node_id", "attempt_id", "completion_gate", "marker_digest", "evidence_digest")
    rows = []
    for row in markers:
        if not all(isinstance(row.get(key), str) and row[key] for key in fields):
            raise TerminalCommitError("terminal-marker-not-current", "terminal-identity-incomplete")
        rows.append([row[key] for key in fields])
    rows.sort(key=lambda row: row[0])
    if len({row[0] for row in rows}) != len(rows):
        raise TerminalCommitError("terminal-marker-not-current", "duplicate-terminal-node")
    return _digest(_canonical(rows))


def terminal_commit_id(*, route_id: str, route_hash: str, owner_attempt_id: str,
                       marker_digest: str, producer_digest: str) -> str:
    return _digest(_canonical([route_id, route_hash, owner_attempt_id, marker_digest, producer_digest]))


def _commit_state_path(request: TerminalCommitRequest) -> Path:
    try:
        route_id = json.loads(Path(request.route_file).read_text(encoding="utf-8")).get("route_id", request.route_file.stem)
    except (OSError, ValueError, TypeError):
        route_id = request.route_file.stem
    return terminal_slot(request.artifact_root, route_id, request.owner_attempt_id) / "terminal-commit.json"


def _record_attempt(request: TerminalCommitRequest, reason: str, detail: str = "") -> None:
    slot = _commit_state_path(request).parent / "attempts"
    event = hashlib.sha256(f"{time.time_ns()}:{reason}:{detail}".encode()).hexdigest()
    _atomic_json(slot / f"{event}.json", {"reason": reason, "detail": detail, "event": event})


@contextmanager
def _jobs_lock(jobs: Path):
    """Hold the canonical registry lock for the claim/state publication fence."""
    lock_path = Path(f"{jobs}.lock")
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        # PRD §13.53.4(3): rank 2 of the canonical table. Declaring it here is
        # what makes "the producer lock is never held when the jobs lock is
        # taken" a checked property rather than a comment.
        try:
            with dispatch_lock_order.acquired("jobs"):
                yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_STATES = ("claimed", "route-closed", "producer-finalized", "not-applicable", "owner-envelope-sealed")
_STATE_INDEX = {name: index for index, name in enumerate(_STATES)}


def _advance_state(path: Path, commit_id: str, expected_from: str, to_state: str,
                   receipts: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    # State publication is a small CAS, separate from producer work. The
    # producer lock has been released before any caller reaches this point.
    with (path.parent / "state.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Rank 4 of the canonical table: declaring it is what makes "this
            # CAS is taken only after the producer lock was released" checkable
            # rather than a comment two modules away.
            with dispatch_lock_order.acquired("terminal-commit-state"):
                return _advance_state_locked(path, commit_id, expected_from, to_state, receipts)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _advance_state_locked(path: Path, commit_id: str, expected_from: str, to_state: str,
                          receipts: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    if to_state not in _STATE_INDEX or expected_from not in _STATE_INDEX:
        raise TerminalCommitError("recovery-unavailable", "invalid-state")
    current = json.loads(path.read_text(encoding="utf-8"))
    if current.get("terminal_commit_id") != commit_id:
        raise TerminalCommitError("transaction-conflict", str(path))
    actual = current.get("state")
    if actual == to_state:
        return current
    if actual != expected_from:
        if _STATE_INDEX.get(actual, -1) > _STATE_INDEX[expected_from]:
            return current
        raise TerminalCommitError("recovery-unavailable", f"state:{actual}:{expected_from}")
    updated = dict(current)
    updated["state"] = to_state
    if receipts:
        updated.setdefault("receipts", {}).update(dict(receipts))
    _atomic_json(path, updated)
    return updated


def _in_root_regular(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return resolved.is_file() and not path.is_symlink() and (resolved == root or root in resolved.parents)
    except OSError:
        return False


def select_primary_artifact(route: Mapping[str, Any], gates: Mapping[str, Any],
                            binding: Optional[Mapping[str, Any]], *, artifact_root: Path) -> Optional[Path]:
    root = Path(artifact_root).resolve()
    explicit = (binding or {}).get("primary") or (binding or {}).get("primary_artifact")
    if isinstance(explicit, str):
        candidate = Path(explicit)
        if candidate.is_absolute() and _in_root_regular(candidate, root) and candidate.stat().st_size:
            return candidate.resolve()
    contract = route.get("workflow_contract") or {}
    declared = contract.get("terminal_nodes")
    if not isinstance(declared, list) or not declared:
        return None
    candidates = []
    for node_id in declared:
        row = gates.get(node_id)
        evidence = row.get("evidence") if isinstance(row, Mapping) else None
        if isinstance(evidence, str):
            candidate = Path(evidence)
            if candidate.is_absolute() and _in_root_regular(candidate, root) and candidate.stat().st_size:
                candidates.append(candidate.resolve())
    return candidates[0] if len(candidates) == 1 else (candidates[0] if candidates else None)


def _seal_owner_envelope(*, request: TerminalCommitRequest, route: Mapping[str, Any],
                         commit_id: str, gates: Mapping[str, Any], binding: Optional[Mapping[str, Any]],
                         slot: Path, terminal_nodes: tuple[str, ...]) -> Optional[str]:
    primary = select_primary_artifact(route, gates, binding, artifact_root=request.artifact_root)
    if primary is None:
        return None
    first_digest = _digest(primary.read_bytes())
    second_digest = _digest(primary.read_bytes())
    if first_digest != second_digest:
        raise TerminalCommitError("recovery-unavailable", "primary-changed-before-envelope")
    text = f"artifact: {primary}\nverdict: PASS\nblocker: none\n"
    txt_path = slot / "owner-envelope.txt"
    try:
        _write_exclusive(txt_path, text.encode())
    except FileExistsError:
        if txt_path.read_bytes() != text.encode():
            raise TerminalCommitError("transaction-conflict", str(txt_path))
    meta = {"content_digest": _digest(text.encode()), "primary_path": str(primary),
            "primary_digest": second_digest, "terminal_commit_id": commit_id,
            "owner_attempt_id": request.owner_attempt_id, "terminal_nodes": list(terminal_nodes)}
    _atomic_json(slot / "owner-envelope.json", meta, exclusive=not (slot / "owner-envelope.json").exists())
    return text


def _component(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or not _SAFE.fullmatch(value):
        raise TerminalCommitError("producer-binding-mismatch", f"unsafe-{label}")
    return value


def producer_binding_path(artifact_root: Path, route_id: str, owner_attempt_id: str) -> Path:
    root = Path(artifact_root).resolve()
    return root / ".runtime" / "terminal-commits" / "v1" / _component(route_id, "route") / _component(owner_attempt_id, "owner") / "producer-binding.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".terminal-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.unlink(temporary)


def load_producer_binding(*, artifact_root: Path, route_id: str, owner_attempt_id: str) -> ProducerBindingResult:
    path = producer_binding_path(artifact_root, route_id, owner_attempt_id)
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode())
    except (OSError, UnicodeError, ValueError) as exc:
        if not path.exists():
            raise TerminalCommitError("producer-binding-required", str(path)) from exc
        raise TerminalCommitError("producer-binding-mismatch", str(path)) from exc
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise TerminalCommitError("producer-binding-mismatch", str(path))
    if (value.get("owner_attempt_id") != owner_attempt_id or value.get("route_id") != route_id
            or value.get("artifact_root") != str(Path(artifact_root).resolve())):
        raise TerminalCommitError("producer-binding-mismatch", "binding-identity")
    identity = artifact_lifecycle.read_root_identity(Path(artifact_root))
    if identity is None or value.get("root_identity") != {
            "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id}:
        raise TerminalCommitError("producer-binding-mismatch", "root-identity")
    return ProducerBindingResult("loaded", path, value, _digest(raw), True)


def validate_owner_route(*, jobs: Path, route_file: Path, owner_attempt_id: str) -> owner_route_binding.OwnerRouteBinding:
    """Verify the live registry owner before producer admission is locked."""
    # Route loading belongs to terminal proof, not import-time artifact guards.
    import owner_route_binding
    if owner_attempt_id == "-" or not owner_attempt_id:
        raise TerminalCommitError("producer-binding-required", "owner-attempt")
    try:
        owner, _status = owner_route_binding.resolve_owner_route_lifecycle(
            jobs, owner_attempt_id=owner_attempt_id)
        if owner is None:
            # A registered quick one-shot already seals the complete node
            # tuple in route_* fields, without the standard owner's separate
            # launch/attachment fields. Verify that existing contract exactly.
            fields, meta = owner_route_binding._owner_snapshot(Path(jobs), owner_attempt_id)
            route = json.loads(Path(route_file).read_text())
            # Quick is a three-node route: `one-shot` plus two depth-1 frame
            # legs. Both worker types terminate through here, and the derived
            # tuple must come from the node the row actually names -- assuming
            # `one-shot` made every frame leg's termination fail as
            # `route-identity-unverified: quick-owner-tuple`.
            if (route.get("effective_intensity") != "quick"
                    or meta.get("worker_type") not in {"owner", "frame"}
                    or meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"
                    or fields[1] not in {"open", "running", "done"}
                    or (fields[1] == "done" and meta.get("failure_class") != "pass")):
                raise TerminalCommitError("route-identity-unverified", "quick-owner-axes")
            quick = owner_route_binding.derive_quick_owner_binding(route_file,
                worktree=fields[3], capability=meta.get("capability", ""),
                capability_mode=meta.get("capability_mode", ""),
                intensity=meta.get("intensity", ""), harness=meta.get("harness", ""),
                route_node=meta.get("route_node") or "one-shot")
            for key in ("route_id", "route_hash", "route_node", "registry_digest", "completion_gate"):
                if meta.get(key) != getattr(quick, key):
                    raise TerminalCommitError("route-identity-unverified", "quick-owner-tuple")
            if Path(meta.get("route_file", "")).resolve() != Path(quick.route_file):
                raise TerminalCommitError("route-identity-unverified", "quick-owner-route")
            if meta.get("write_scope") != quick.write_scope:
                raise TerminalCommitError("route-identity-unverified", "quick-owner-scope")
            owner = owner_route_binding.OwnerRouteBinding(quick.route_file, quick.route_id, quick.route_hash)
    except owner_route_binding.OwnerRouteBindingError as exc:
        raise TerminalCommitError("route-identity-unverified", str(exc)) from exc
    if owner is None or owner.route_file != str(Path(route_file).resolve()):
        raise TerminalCommitError("route-identity-unverified", "owner-route")
    return owner


def verify_request_identity(request, route):
    digest = route_identity.route_hash(route)
    if (route.get("route_hash") != digest or route.get("route_id") != route_identity.route_id_from_hash(digest)
            or Path(route.get("artifact_root", "")).resolve() != Path(request.artifact_root).resolve()):
        raise TerminalCommitError("route-identity-unverified", "canonical-route-identity")
    rows = []
    for line in Path(request.jobs).read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 6:
            rows.append((fields, dispatch_contract.parse_registry_metadata(fields[5])))
    binding = validate_owner_route(jobs=request.jobs, route_file=request.route_file,
                                   owner_attempt_id=request.owner_attempt_id)
    owners = [(fields, meta) for fields, meta in rows if meta.get("worker_type") == "owner"
              and (meta.get("owner_route_id") == route["route_id"]
                   or meta.get("route_id") == route["route_id"]
                   or meta.get("attempt_id") == request.owner_attempt_id)]
    if not owners or owners[-1][1].get("attempt_id") != request.owner_attempt_id:
        raise TerminalCommitError("route-identity-unverified", "owner-not-current")
    fields, meta = owners[-1]
    if (meta.get("dispatch_depth") != "1" or meta.get("registered_worker") != "1"
            or meta.get("harness") not in dispatch_contract.WRAPPER_PARENT_HARNESSES or binding.route_hash != digest
            or fields[1] not in {"open", "running", "done"}
            or (fields[1] == "done" and meta.get("failure_class") != "pass")):
        raise TerminalCommitError("route-identity-unverified", "owner-axes")


def publish_producer_binding(*, artifact_root: Path, jobs: Path, route_file: Path,
                             owner_attempt_id: str, cycle_id: str,
                             owner_begin: bool) -> ProducerBindingResult:
    if owner_attempt_id == "-" or not owner_attempt_id:
        raise TerminalCommitError("producer-binding-required", "owner-attempt")
    root = Path(artifact_root).resolve()
    route_path = Path(route_file).resolve()
    owner = validate_owner_route(jobs=jobs, route_file=route_path, owner_attempt_id=owner_attempt_id)
    try:
        route = json.loads(route_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise TerminalCommitError("route-identity-unverified", str(route_path)) from exc
    if route.get("route_id") != owner.route_id or route.get("route_hash") != owner.route_hash:
        raise TerminalCommitError("route-identity-unverified", "route-binding")
    identity = artifact_lifecycle.read_root_identity(root)
    record_path = root / ".runtime" / "artifact-producer" / "v1" / "cycles" / f"{cycle_id}.json"
    try:
        record_raw = record_path.read_bytes()
        record = json.loads(record_raw.decode())
    except (OSError, UnicodeError, ValueError) as exc:
        raise TerminalCommitError("producer-binding-mismatch", f"cycle:{cycle_id}") from exc
    if not isinstance(record, dict) or record.get("cycle_id") != cycle_id or record.get("state") != "open":
        raise TerminalCommitError("producer-binding-mismatch", f"cycle:{cycle_id}")
    if record.get("route_id") != owner.route_id or record.get("route_hash") != owner.route_hash:
        raise TerminalCommitError("producer-binding-mismatch", "cycle-route")
    if identity is None:
        raise TerminalCommitError("producer-binding-mismatch", "root-identity")
    binding = {
        "schema_version": 1, "contract": CONTRACT,
        "owner_attempt_id": owner_attempt_id, "route_file": str(route_path),
        "route_id": owner.route_id, "route_hash": owner.route_hash,
        "artifact_root": str(root),
        "root_identity": {"repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id},
        "campaign_id": record.get("campaign_id"), "cycle_id": cycle_id,
        "producer_id": record.get("producer_id"),
        "cycle_record_digest": cycle_identity_digest(record), "observed_state": "open",
    }
    path = producer_binding_path(root, owner.route_id, owner_attempt_id)
    data = _canonical(binding) + b"\n"
    if not owner_begin:
        existing = load_producer_binding(artifact_root=root, route_id=owner.route_id,
                                         owner_attempt_id=owner_attempt_id)
        if existing.path.read_bytes() != data:
            raise TerminalCommitError("transaction-conflict", str(path))
        return ProducerBindingResult("replayed", path, existing.binding, existing.digest, True)
    try:
        _write_exclusive(path, data)
        return ProducerBindingResult("published", path, binding, _digest(data), False)
    except FileExistsError:
        existing = load_producer_binding(artifact_root=root, route_id=owner.route_id, owner_attempt_id=owner_attempt_id)
        if existing.binding is not None and existing.path.read_bytes() == data:
            return ProducerBindingResult("replayed", path, existing.binding, existing.digest, True)
        raise TerminalCommitError("transaction-conflict", str(path))


def producer_binding_digest(binding_path: Path) -> str:
    try:
        return _digest(Path(binding_path).read_bytes())
    except OSError as exc:
        raise TerminalCommitError("producer-binding-required", str(binding_path)) from exc


def cycle_identity_digest(record):
    """Only immutable cycle identity; state/mtime/projection updates are not identity."""
    return _digest(_canonical({key: record.get(key) for key in (
        "campaign_id", "cycle_id", "producer_id", "route_id", "route_hash", "route_file")}))


def _route_module():
    path = Path(__file__).with_name("capability-route.py")
    spec = importlib.util.spec_from_file_location("terminal_capability_route", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _proof_failure(reason: str, detail: Optional[str] = None) -> TerminalProof:
    return TerminalProof("rejected", _DETAIL_REASON_MAP.get(reason, reason), reason if detail is None else detail)


def _prove_route_children(request, route, gates):
    """Use the shared attempt policy for initial closure and every replay."""
    from dispatch_attempt_policy import decide_attempt
    related = []
    for line in Path(request.jobs).read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        meta = dispatch_contract.parse_registry_metadata(fields[5])
        if meta.get("route_id") == route["route_id"] or meta.get("parent_attempt_id") == request.owner_attempt_id:
            related.append((fields[1], meta))
    for node in route.get("nodes", []):
        if node.get("terminal") is not True:
            continue
        matching = [(status, meta) for status, meta in related
                    if meta.get("route_id") == route["route_id"] and meta.get("route_node") == node["id"]]
        if (not matching or matching[-1][0] != "done" or matching[-1][1].get("failure_class") != "pass"
                or gates.get(node["id"], {}).get("attempt_id") != matching[-1][1].get("attempt_id")):
            return _proof_failure("terminal-attempt-not-pass")
    for status, meta in related:
        process = dispatch_contract.attempt_process_quiescence(meta, terminal_receipt=True)
        decision = decide_attempt(status, meta, process_state=process.state, process_reason=process.reason)
        if decision.action == "inspect-conflict":
            return _proof_failure("transaction-conflict", "child-terminal-conflict")
        if decision.action == "reconcile":
            return _proof_failure("child-not-terminal")
        if decision.action in {"wait", "recover"}:
            return _proof_failure("child-not-quiescent")
    # Retry timestamps record history. Real rows and the shared jobs-lock
    # terminal claim decide what remains active and fence every later start.
    return TerminalProof("proved")


def prove_terminal_authority(request: TerminalCommitRequest) -> TerminalProof:
    """Read-only, exact terminal authority proof; it performs zero mutation."""
    try:
        route_file = Path(request.route_file).resolve()
        route = json.loads(route_file.read_text(encoding="utf-8"))
        if not isinstance(route, dict) or not route.get("route_id") or not route.get("route_hash"):
            return _proof_failure("route-identity-unverified")
        verify_request_identity(request, route)
        owner = validate_owner_route(jobs=request.jobs, route_file=route_file,
                                     owner_attempt_id=request.owner_attempt_id)
        if owner.route_id != route.get("route_id") or owner.route_hash != route.get("route_hash"):
            return _proof_failure("owner-route-mismatch")
        route_mod = _route_module()
        gates = route_mod.terminal_gate_observation(route, jobs=request.jobs, exact_terminal=True)
        if not gates or any(not row.get("passed") for row in gates.values()):
            return _proof_failure("terminal-marker-not-current")
        children = _prove_route_children(request, route, gates)
        if children.status != "proved":
            return children
        if producer_lifecycle_applies(route):
            # Binding is immutable and must still point at an open, matching cycle.
            binding = load_producer_binding(artifact_root=request.artifact_root,
                                            route_id=route["route_id"],
                                            owner_attempt_id=request.owner_attempt_id)
            if binding.binding.get("route_hash") != route["route_hash"]:
                return _proof_failure("producer-binding-mismatch")
            cycle_path = Path(request.artifact_root).resolve() / ".runtime/artifact-producer/v1/cycles" / f"{binding.binding['cycle_id']}.json"
            cycle = json.loads(cycle_path.read_text(encoding="utf-8"))
            if cycle.get("state") != "open" or cycle.get("route_hash") not in (None, route["route_hash"]):
                return _proof_failure("binding-cycle-not-open")
            if cycle_identity_digest(cycle) != binding.binding.get("cycle_record_digest"):
                return _proof_failure("producer-binding-mismatch", "cycle-identity-drift")
            producer = __import__("artifact_producer")
            if _producer_operation(producer._live_review_lease,
                    Path(request.artifact_root), binding.binding["cycle_id"]):
                return _proof_failure("active-review-lease")
        # Non-producer routes explicitly carry the absence as a proof detail.
        else:
            return TerminalProof("proved", None, "producer-binding:not-applicable")
    except TerminalCommitError as exc:
        return _proof_failure(exc.code, exc.detail)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return _proof_failure("route-identity-unverified")
    return TerminalProof("proved", None)


def _reverify_forward_recovery(
    request: TerminalCommitRequest, existing_state_value: Mapping[str, Any]
) -> TerminalProof:
    """A82-7/§13.53.5 exact forward recovery proof.

    Re-entry after route-close/producer-finalize/envelope-seal must not fall
    back to `prove_terminal_authority` -- that function assumes an
    open-cycle, from-scratch quiescence/review-lease proof, which forward
    progress has already legitimately invalidated (finalize closes the
    cycle; close_route can flip node state). It also must not accept the
    stored `state` string alone as proof (that was the bypass this replaces).
    Instead it recomputes the exact terminal identity from the *current*
    route/marker/binding and requires it to still match the durable
    record's `terminal_commit_id`, and re-validates that the current
    registry owner/route binding is still the one on file. Either check
    failing is `transaction-conflict` with zero mutation -- never a silent
    pass-through.
    """
    try:
        route_file = Path(request.route_file).resolve()
        route = json.loads(route_file.read_text(encoding="utf-8"))
        if not isinstance(route, dict) or not route.get("route_id") or not route.get("route_hash"):
            return _proof_failure("route-identity-unverified")
        verify_request_identity(request, route)
        validate_owner_route(
            jobs=request.jobs, route_file=route_file, owner_attempt_id=request.owner_attempt_id
        )
        route_module = _route_module()
        gates = route_module.terminal_gate_observation(route, jobs=request.jobs, exact_terminal=True)
        marker_digest = terminal_marker_digest(list(gates.values()))
        if producer_lifecycle_applies(route):
            binding = load_producer_binding(
                artifact_root=request.artifact_root,
                route_id=route["route_id"],
                owner_attempt_id=request.owner_attempt_id,
            )
            producer_digest = binding.digest or producer_binding_digest(binding.path)
        else:
            producer_digest = _digest(_canonical({
                "contract": "producer-binding-not-applicable/v1",
                "reason": "sealed-topology-nonproducer",
            }))
        recomputed = terminal_commit_id(
            route_id=route["route_id"], route_hash=route["route_hash"],
            owner_attempt_id=request.owner_attempt_id,
            marker_digest=marker_digest, producer_digest=producer_digest,
        )
    except TerminalCommitError as exc:
        return _proof_failure(exc.code, exc.detail)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        return _proof_failure("route-identity-unverified")
    if recomputed != existing_state_value.get("terminal_commit_id"):
        return _proof_failure("transaction-conflict", "forward-recovery-identity-mismatch")
    try:
        children = _prove_route_children(request, route, gates)
    except (OSError, ValueError, TypeError) as exc:
        return _proof_failure("transaction-conflict", type(exc).__name__)
    if children.status != "proved":
        return children
    claim = dispatch_contract.terminal_claim_observation(
        request.jobs, route["route_id"], request.owner_attempt_id
    )
    if not isinstance(claim, Mapping):
        return _proof_failure("transaction-conflict", "terminal-claim-missing")
    proof_claim = claim.get("proof") if isinstance(claim.get("proof"), Mapping) else {}
    if proof_claim.get("terminal_commit_id") != recomputed:
        return _proof_failure("transaction-conflict", "terminal-claim-identity-mismatch")
    return TerminalProof("proved", None, "forward-recovery-replay")


def settle_terminal_commit(request: TerminalCommitRequest, services: Any = None) -> TerminalCommitResult:
    production_services = services is None
    services = services if services is not None else TerminalCommitServices()
    # A forward retry must be allowed to inspect the durable transaction after
    # route-close/producer-finalize changed the live cycle state.  The original
    # claim already contains the exact identity; re-running the open-cycle
    # eligibility proof here would turn a recoverable retry into a false
    # ineligible result. But (A82-7/§13.53.5) that forward-recovery allowance
    # is not a blanket bypass keyed off "state != claimed" -- it is
    # conditioned on recomputing the exact `terminal_commit_id` from the
    # *current* route/marker/binding and requiring it to still match the
    # durable record. A stored state string alone proves nothing about
    # whether the owner/route/marker set is still the one that was claimed.
    existing_state_value: Optional[dict] = None
    try:
        existing_hint = _commit_state_path(request)
        if existing_hint.exists():
            existing_state_value = json.loads(existing_hint.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing_state_value = None
    if existing_state_value is not None and existing_state_value.get("state") != "claimed":
        proof = _reverify_forward_recovery(request, existing_state_value)
    else:
        proof = prove_terminal_authority(request)
    if proof.status != "proved":
        _record_attempt(request, proof.reason or "recovery-unavailable", proof.detail or "")
        result_kind = "recoverable" if proof.reason == "transaction-conflict" else "ineligible"
        return TerminalCommitResult(result_kind, proof.reason, proof.detail)
    try:
        route = json.loads(Path(request.route_file).read_text(encoding="utf-8"))
        route_module = _route_module()
        gates = route_module.terminal_gate_observation(route, jobs=request.jobs, exact_terminal=True)
        markers = list(gates.values())
        terminal_nodes = tuple((route.get("workflow_contract") or {}).get("terminal_nodes") or ())
        marker_digest = terminal_marker_digest(markers)
        binding_value = None
        if producer_lifecycle_applies(route):
            binding = load_producer_binding(artifact_root=request.artifact_root,
                                            route_id=route["route_id"], owner_attempt_id=request.owner_attempt_id)
            producer_digest = binding.digest or producer_binding_digest(binding.path)
            binding_value = binding.binding
        else:
            producer_digest = _digest(_canonical({"contract": "producer-binding-not-applicable/v1",
                                                  "reason": "sealed-topology-nonproducer"}))
        commit_id = terminal_commit_id(route_id=route["route_id"], route_hash=route["route_hash"],
                                       owner_attempt_id=request.owner_attempt_id,
                                       marker_digest=marker_digest, producer_digest=producer_digest)
        path = _commit_state_path(request)
        initial = {"schema_version": 1, "terminal_commit_id": commit_id,
                   "route_id": route["route_id"], "route_hash": route["route_hash"],
                   "owner_attempt_id": request.owner_attempt_id,
                   "terminal_marker_digest": marker_digest,
                   "producer_binding_digest": producer_digest, "state": "claimed"}
        # The terminal claim and the first durable commit record share the
        # existing jobs.log.lock.  This closes the snapshot->claim race with
        # start/retry/marker writers, which perform the matching claim-absent
        # check in their jobs-lock critical section.  No producer lock is held
        # here, preserving node -> jobs -> producer lock order.
        with _jobs_lock(request.jobs) as jobs_lock:
            fresh = (_reverify_forward_recovery(request, existing_state_value)
                     if existing_state_value is not None and existing_state_value.get("state") != "claimed"
                     else prove_terminal_authority(request))
            if fresh.status != "proved":
                raise TerminalCommitError(fresh.reason or "transaction-conflict", fresh.detail or "claim-proof-changed")
            fresh_gates = route_module.terminal_gate_observation(route, jobs=request.jobs, exact_terminal=True)
            if terminal_marker_digest(list(fresh_gates.values())) != marker_digest:
                raise TerminalCommitError("transaction-conflict", "claim-markers-changed")
            dispatch_contract.claim_terminal_route_locked(
                request.jobs, route["route_id"], request.owner_attempt_id,
                lock_fd=jobs_lock.fileno(),
                proof={
                    "terminal_commit_id": commit_id,
                    "terminal_marker_digest": marker_digest,
                    "producer_binding_digest": producer_digest,
                },
            )
            try:
                _atomic_json(path, initial, exclusive=True)
            except FileExistsError:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("terminal_commit_id") != commit_id:
                    raise TerminalCommitError("transaction-conflict", str(path))
        state = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = getattr(services, "crash_after", None)
        if state.get("state") == "claimed":
            if checkpoint == "claim-after":
                raise RuntimeError("crash-after-claim")
            services.close_route(route, request.route_file, allow_unproven=False, jobs=request.jobs,
                                 expected_terminal_marker_digest=marker_digest,
                                 terminal_commit_id=commit_id,
                                 expected_owner_attempt_id=request.owner_attempt_id,
                                 expected_producer_binding_digest=producer_digest)
            state = _advance_state(path, commit_id, "claimed", "route-closed",
                                   {"route": "closed"})
            if checkpoint == "close-after":
                raise RuntimeError("crash-after-close")
        if state.get("state") == "route-closed":
            if binding_value is not None:
                services.finalize_exact_cycle(request.artifact_root, cycle_id=binding_value["cycle_id"],
                                              expected_binding=binding_value)
                state = _advance_state(path, commit_id, "route-closed", "producer-finalized",
                                       {"producer": "finalized"})
            else:
                state = _advance_state(path, commit_id, "route-closed", "not-applicable",
                                       {"producer": "not-applicable"})
        if state.get("state") in {"producer-finalized", "not-applicable"}:
            if production_services:
                _verify_settled_outputs(request, route, commit_id, binding_value)
            envelope = services.seal_envelope(request=request, route=route, commit_id=commit_id,
                                               gates=gates, binding=binding_value, slot=path.parent,
                                               terminal_nodes=terminal_nodes)
            if envelope is None:
                _record_attempt(request, "recovery-unavailable", "material-primary-required")
                return TerminalCommitResult("needs-owner", "recovery-unavailable", "material-primary-required",
                                            terminal_nodes)
            _advance_state(path, commit_id, state["state"], "owner-envelope-sealed",
                           {"envelope": "sealed"})
            return TerminalCommitResult("completed", None, None, terminal_nodes, envelope)
        if state.get("state") == "owner-envelope-sealed":
            if production_services:
                _verify_settled_outputs(request, route, commit_id, binding_value)
            try:
                envelope_text = _read_sealed_owner_envelope(request, commit_id)
            except TerminalCommitError as exc:
                _record_attempt(request, exc.code, exc.detail)
                return TerminalCommitResult("recoverable", exc.code, exc.detail, terminal_nodes)
            return TerminalCommitResult("completed", None, None, terminal_nodes, envelope_text)
        return TerminalCommitResult("recoverable", "recovery-unavailable", "partial-state", terminal_nodes)
    except TerminalCommitError as exc:
        _record_attempt(request, exc.code, exc.detail)
        return TerminalCommitResult("recoverable" if exc.code in {"route-close-failed", "producer-finalize-failed", "recovery-unavailable"} else "ineligible", exc.code, exc.detail)
    except RuntimeError as exc:
        _record_attempt(request, "recovery-unavailable", str(exc))
        return TerminalCommitResult("recoverable", "recovery-unavailable", str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _record_attempt(request, "recovery-unavailable", str(exc))
        return TerminalCommitResult("recoverable", "recovery-unavailable", str(exc))


def _read_sealed_owner_envelope(request, commit_id):
    """One read-only envelope proof for settlement replay and delivery."""
    slot = _commit_state_path(request).parent
    try:
        meta = json.loads((slot / "owner-envelope.json").read_text())
        text = (slot / "owner-envelope.txt").read_text()
    except (OSError, ValueError, TypeError) as exc:
        raise TerminalCommitError("recovery-unavailable", "envelope-unreadable") from exc
    if meta.get("terminal_commit_id") != commit_id:
        raise TerminalCommitError("transaction-conflict", "envelope-identity-mismatch")
    if _digest(text.encode()) != meta.get("content_digest"):
        raise TerminalCommitError("transaction-conflict", "envelope-content-mismatch")
    primary = Path(meta.get("primary_path") or "")
    if not primary.is_absolute() or not _in_root_regular(primary, request.artifact_root.resolve()):
        raise TerminalCommitError("recovery-unavailable", "primary-no-longer-in-root")
    if _digest(primary.read_bytes()) != meta.get("primary_digest"):
        raise TerminalCommitError("transaction-conflict", "primary-content-drifted-after-seal")
    return text


def _verify_settled_outputs(request, route, commit_id, binding):
    outcome = json.loads(_route_module().outcome_path(request.route_file).read_text())
    if (outcome.get("terminal_commit_id") != commit_id
            or outcome.get("terminal_gate_proven") is not True
            or outcome.get("route_id") != route["route_id"]
            or outcome.get("route_hash") != route["route_hash"]
            or outcome.get("terminal_owner_attempt_id") != request.owner_attempt_id):
        raise TerminalCommitError("transaction-conflict", "settled-outcome-mismatch")
    if binding is not None:
        import artifact_producer
        _producer_operation(artifact_producer.verify_finalized_cycle, request.artifact_root,
            cycle_id=binding["cycle_id"], expected_binding=binding)


def load_active_cleanup_scope(state_root, owner_attempt_id: str) -> CleanupScope | None:
    from dispatch_budget_record import terminal_handoff_root
    path = terminal_handoff_root(state_root, owner_attempt_id, 0).parent
    # An intent published just before a crash is already a restrictive state;
    # a missing scope sidecar must not restore unrestricted owner authority.
    matches = list(path.glob("*/prompt-intent.json"))
    if not matches:
        return None
    # D7/§13.53.9: the ordinal segment (`.../<ordinal>/cleanup-scope.json`) is
    # an integer, but `Path.glob` returns matches in directory order (not
    # even reliably lexical) -- picking `matches[-1]` as "the latest" silently
    # selects the wrong scope once ordinal 10 exists alongside ordinal 9, or
    # on any filesystem/glob ordering that doesn't happen to sort ascending.
    # Parse and sort numerically; a non-numeric ordinal segment is dropped
    # rather than guessed at.
    def _ordinal(candidate: Path) -> Optional[int]:
        try:
            return int(candidate.parent.name)
        except (ValueError, TypeError):
            return None

    numbered = sorted(
        (m for m in matches if _ordinal(m) is not None), key=_ordinal
    )
    if not numbered:
        return None
    try:
        intent = json.loads(numbered[-1].read_text(encoding="utf-8"))
        raw = intent["cleanup_scope"]
        digest = hashlib.sha256(_canonical({k: v for k, v in raw.items() if k != "scope_digest"})).hexdigest()
        if (digest != raw.get("scope_digest") or raw.get("owner_attempt_id") != owner_attempt_id
                or raw.get("claim_id") != intent.get("claim_id")
                or raw.get("intent_id") != intent.get("intent_id")):
            raise TerminalCommitError("transaction-conflict", "cleanup-scope-identity")
        return CleanupScope(
            artifact_root=Path(raw.get("artifact_root", ".")), route_id=raw.get("route_id", ""),
            cycle_id=raw.get("cycle_id"), owner_attempt_id=raw.get("owner_attempt_id", owner_attempt_id),
            route_hash=raw.get("route_hash", ""), terminal_commit_id=raw.get("terminal_commit_id", ""),
            claim_id=raw.get("claim_id", ""), intent_id=raw.get("intent_id", ""),
            allowed_targets=tuple(Path(x) for x in raw.get("allowed_targets", [])),
            allowed_write_roots=tuple(Path(x) for x in raw.get("allowed_write_roots", [])),
            allowed_read_roots=tuple(Path(x) for x in raw.get("allowed_read_roots", [])),
            allowed_recovery_targets=tuple(Path(x) for x in raw.get("allowed_recovery_targets", [])),
            allowed_operations=tuple(raw.get("allowed_operations", ())),
            expires_at=raw.get("expires_at"), one_use=bool(raw.get("one_use", True)),
            consumed=(numbered[-1].parent / "cleanup-consumed.json").exists(), scope_digest=raw.get("scope_digest", ""),
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise TerminalCommitError("recovery-unavailable", "cleanup-scope-unreadable") from exc


def authorize_cleanup_operation(scope: CleanupScope, *, operation: str, target: Optional[Path],
                                route_id: str, cycle_id: Optional[str],
                                owner_attempt_id: Optional[str] = None,
                                route_hash: Optional[str] = None,
                                terminal_commit_id: Optional[str] = None,
                                claim_id: Optional[str] = None,
                                intent_id: Optional[str] = None) -> CleanupVerdict:
    if operation not in set(scope.allowed_operations):
        return CleanupVerdict("denied-operation", operation)
    if route_id != scope.route_id:
        return CleanupVerdict("denied-route", route_id)
    identity = (
        ("owner_attempt_id", owner_attempt_id, scope.owner_attempt_id),
        ("route_hash", route_hash, scope.route_hash),
        ("terminal_commit_id", terminal_commit_id, scope.terminal_commit_id),
        ("claim_id", claim_id, scope.claim_id),
        ("intent_id", intent_id, scope.intent_id),
    )
    for name, supplied, recorded in identity:
        if recorded and supplied != recorded:
            return CleanupVerdict("denied-identity", name)
    if scope.cycle_id is not None and cycle_id != scope.cycle_id:
        return CleanupVerdict("denied-cycle", str(cycle_id))
    if scope.expires_at is not None and time.time() >= float(scope.expires_at):
        return CleanupVerdict("denied-expired")
    if scope.one_use and scope.consumed:
        return CleanupVerdict("denied-consumed")
    if target is not None:
        target = Path(target).resolve()
        if operation in {"close-forward-recovery", "finalize-forward-recovery"}:
            return (CleanupVerdict("allowed") if target in {p.resolve() for p in scope.allowed_recovery_targets}
                    else CleanupVerdict("denied-target", str(target)))
        roots = scope.allowed_read_roots if operation in {"read", "verify"} else scope.allowed_write_roots
        roots = roots or scope.allowed_targets
        if not any(target == p.resolve() or p.resolve() in target.parents for p in roots):
            return CleanupVerdict("denied-target", str(target))
    elif operation in {"read", "verify", "partial-report", "final-report"}:
        return CleanupVerdict("denied-target", "explicit-target-required")
    return CleanupVerdict("allowed")


def authorize_bound_cleanup(scope, *, operation, target, owner_attempt_id, route_id, cycle_id=None):
    """The loaded, digest-verified scope supplies transaction identities.

    Caller identities still come from the immutable launch environment;
    knowing a route name does not make a foreign owner its cleanup holder.
    """
    return authorize_cleanup_operation(
        scope, operation=operation, target=target, owner_attempt_id=owner_attempt_id,
        route_id=route_id, cycle_id=cycle_id if cycle_id is not None else scope.cycle_id,
        route_hash=scope.route_hash, terminal_commit_id=scope.terminal_commit_id,
        claim_id=scope.claim_id, intent_id=scope.intent_id)


def require_current_cleanup(operation, *, target=None, cycle_id=None, jobs=None):
    """Common direct-writer guard, including post-join owners with no child."""
    owner = os.environ.get("AGENT_DISPATCH_ATTEMPT_ID", "")
    if not owner:
        return
    registry = jobs or os.environ.get("AGENT_DISPATCH_JOBS")
    state_root = Path(registry).parent if registry else dispatch_contract.resolve_dispatch_state_root(
        dispatch_contract.resolve_agent_home())
    scope = load_active_cleanup_scope(state_root, owner)
    if scope is None:
        return
    verdict = authorize_bound_cleanup(scope, operation=operation, target=target,
                                      owner_attempt_id=owner,
                                      route_id=os.environ.get("AGENT_ROUTE_ID", ""), cycle_id=cycle_id)
    if verdict.verdict != "allowed":
        raise TerminalCommitError("recovery-unavailable", "cleanup-scope-" + verdict.verdict)
    return scope


def cleanup_tool_permission(scope, *, tool, arguments, cwd, owner_attempt_id, route_id):
    """Closed native tool surface; shell prefixes never grant permission."""
    import shlex
    operation, target = "denied", None
    if tool in {"Read", "Write", "ArtifactWrite"}:
        value = arguments.get("file_path")
        if isinstance(value, str) and value:
            target = Path(value)
            if not target.is_absolute():
                target = Path(cwd) / target
            operation = "read" if tool == "Read" else "partial-report"
    elif tool in {"Bash", "Shell", "shell", "bash"}:
        command = arguments.get("command", "")
        if isinstance(command, str) and not any(c in command for c in "$`\n\r;&|<>()"):
            try:
                parts = shlex.split(command)
            except ValueError:
                parts = []
            helper = Path(__file__).resolve()
            if (len(parts) == 3 and parts[0] in {"python3", sys.executable}
                    and Path(parts[1]).is_absolute() and Path(parts[1]).resolve() == helper
                    and parts[2] == "cleanup-recover" and scope.terminal_commit_id):
                operation = "close-forward-recovery"
    return authorize_bound_cleanup(scope, operation=operation, target=target,
                                   owner_attempt_id=owner_attempt_id, route_id=route_id)


def _completion_request(jobs, status, metadata):
    """The launch contract, not terminal words alone, assigns workflow closure."""
    if (metadata.get("workflow_completion") != "runtime-v1"
            or metadata.get("worker_type") != "owner" or metadata.get("dispatch_depth") != "1"
            or status != "done" or metadata.get("failure_class") != "pass"):
        return None
    import owner_route_binding
    binding, _ = owner_route_binding.resolve_owner_route_lifecycle(
        Path(jobs), owner_attempt_id=metadata["attempt_id"])
    path = Path(binding.route_file if binding else metadata.get("route_file", ""))
    route = json.loads(path.read_text())
    request = TerminalCommitRequest(path, metadata["attempt_id"], Path(jobs), Path(route["artifact_root"]))
    verify_request_identity(request, route)
    validate_owner_route(jobs=request.jobs, route_file=path, owner_attempt_id=request.owner_attempt_id)
    return request


def owner_completion_pending(jobs, status, metadata) -> bool:
    """Read-only consumption check; success bytes remain immutable while finishing."""
    if metadata.get("workflow_completion") != "runtime-v1" or status != "done" or metadata.get("failure_class") != "pass":
        return False
    try:
        request = _completion_request(jobs, status, metadata)
        if request is None:
            return False
        state = json.loads(_commit_state_path(request).read_text())
        if state.get("state") != "owner-envelope-sealed":
            return True
        if _reverify_forward_recovery(request, state).status != "proved":
            return True
        route = json.loads(request.route_file.read_text())
        binding = (load_producer_binding(artifact_root=request.artifact_root, route_id=route["route_id"],
                   owner_attempt_id=request.owner_attempt_id).binding if producer_lifecycle_applies(route) else None)
        _verify_settled_outputs(request, route, state["terminal_commit_id"], binding)
        _read_sealed_owner_envelope(request, state["terminal_commit_id"])
        import workflow_state as workflow
        ledger = workflow.WorkflowLedger(state["route_id"], state["route_hash"], jobs=Path(jobs))
        return ledger.state()["workflow_state"] != "COMPLETE"
    except (OSError, ValueError, KeyError, TypeError, TerminalCommitError):
        return True


def settle_owner_completion(jobs, status, metadata) -> TerminalCommitResult | None:
    """Runtime-owned, retryable workflow/route/cycle closure after exact PASS.

    Reapers, recovering joins and explicit recovery all use this transaction.
    A failure preserves the committed PASS and leaves an attention obligation;
    it cannot grant another model execution or manufacture a failure result.
    """
    try:
        request = _completion_request(jobs, status, metadata)
        if request is None:
            return None
        if dispatch_contract.terminal_conflict_pending(metadata):
            return TerminalCommitResult("recoverable", "transaction-conflict", "terminal-evidence-conflict")
        process = dispatch_contract.attempt_process_quiescence(metadata, terminal_receipt=True)
        if process.state != "quiescent":
            return TerminalCommitResult("recoverable", "child-not-quiescent", process.reason)
        route = json.loads(request.route_file.read_text())
        import artifact_producer as producer
        import workflow_state as workflow
        # Reuse an existing immutable binding during forward recovery, including
        # a cycle already sealed by the prior invocation.
        binding_path = producer_binding_path(request.artifact_root, route["route_id"], request.owner_attempt_id)
        if producer_lifecycle_applies(route) and not binding_path.exists():
            producer.begin(request.artifact_root, route_file=request.route_file,
                           capability=route["capability"], intensity=route["effective_intensity"],
                           require_cycle=True, jobs=request.jobs, owner_attempt_id=request.owner_attempt_id)
        ledger = workflow.WorkflowLedger(route["route_id"], route["route_hash"], jobs=request.jobs)
        gates = _route_module().terminal_gate_observation(route, jobs=request.jobs, exact_terminal=True)
        with ledger.lock():
            ledger.completion_paths(workflow.route_terminal_nodes(route), gates)
        result = settle_terminal_commit(request)
        if result.result == "completed":
            # The terminal transaction fences late starts and proves every
            # child before the workflow can advertise COMPLETE.
            with ledger.lock():
                ledger.complete(workflow.route_terminal_nodes(route), gates, actor="completion-controller")
    except (OSError, ValueError, KeyError, TypeError, TerminalCommitError) as exc:
        result = TerminalCommitResult("recoverable", "recovery-unavailable", str(exc))
    except Exception as exc:
        # Producer/ledger errors are owned recovery outcomes, never a new
        # terminal failure. Preserve their typed reason without model content.
        result = TerminalCommitResult("recoverable", "recovery-unavailable",
                                      str(getattr(exc, "code", type(exc).__name__)))
    if result.result != "completed":
        from dispatch_supervision import materialize
        try:
            materialize(Path(jobs), {metadata["attempt_id"]}, reason="workflow-completion-pending")
        except (OSError, ValueError, RuntimeError) as exc:
            # The immutable launch contract keeps the obligation discoverable
            # even if the notice store fails. The next join retries both.
            sys.stderr.write(f"workflow-completion-notice-pending attempt={metadata['attempt_id']} reason={exc}\n")
    return result


def cleanup_recover():
    """Structured exact forward settlement; no user-supplied mutation argv."""
    jobs = Path(os.environ["AGENT_DISPATCH_JOBS"])
    owner = os.environ["AGENT_DISPATCH_ATTEMPT_ID"]
    scope = load_active_cleanup_scope(jobs.parent, owner)
    if scope is None or not scope.terminal_commit_id:
        raise TerminalCommitError("recovery-unavailable", "cleanup-scope-required")
    verdict = authorize_bound_cleanup(scope, operation="close-forward-recovery", target=None,
                                      owner_attempt_id=owner, route_id=os.environ.get("AGENT_ROUTE_ID", ""))
    if verdict.verdict != "allowed":
        raise TerminalCommitError("recovery-unavailable", verdict.verdict)
    route_file = Path(os.environ["AGENT_ROUTE_FILE"])
    request = TerminalCommitRequest(route_file, owner, jobs, scope.artifact_root)
    state = json.loads(_commit_state_path(request).read_text())
    if state.get("terminal_commit_id") != scope.terminal_commit_id:
        raise TerminalCommitError("transaction-conflict", "cleanup-commit-mismatch")
    result = settle_terminal_commit(request)
    print(json.dumps({"result": result.result, "reason": result.reason, "detail": result.detail}))
    return 0 if result.result == "completed" else 70


if __name__ == "__main__":
    if sys.argv[1:] == ["cleanup-recover"]:
        raise SystemExit(cleanup_recover())
    import argparse
    parser = argparse.ArgumentParser(description="Recover one runtime-owned workflow completion; never launch a model.")
    parser.add_argument("operation", choices=["finish"])
    parser.add_argument("--jobs", required=True, type=Path)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    dispatch_contract.ensure_global_registry_writable(args.jobs)
    from dispatch_completion_join import exact_attempt_row, materialize_after_terminal_close
    row = exact_attempt_row(args.jobs, args.attempt)
    result = settle_owner_completion(args.jobs, row.status, row.metadata)
    if result is None:
        print(json.dumps({"result": "not-applicable", "reason": "no-runtime-owned-successful-workflow"}))
        raise SystemExit(3)
    if result.result == "completed":
        materialize_after_terminal_close(args.jobs, args.attempt)
    print(json.dumps({"result": result.result, "reason": result.reason, "detail": result.detail}))
    raise SystemExit(0 if result.result == "completed" else 70)
