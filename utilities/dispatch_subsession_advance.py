#!/usr/bin/env python3
"""SD-119: non-model chain-advance checkpoint for a serial sub-session chain.

`utilities/stage-session-chain.py` (R1) no longer waits for the chain in the
foreground -- it starts index 1 and returns. Advancing index 2..N is owned by
this module's `coordinate_subsession_advance()`, called from inside the
per-process session supervisors (`claude-session-supervisor.py`,
`codex-app-server-supervisor.py`) at their existing repark checkpoint, never
by an owner model turn.

Modelled on SD-110's shared stage-advance module's injected-services +
named-checkpoint transaction shape (`_atomic_json`, id-keyed flock,
checkpoint-gated phases) so the five crash windows are a unit cost. This
module never imports that SD-110 module and never calls its route
completion-marker census helper -- a sub-session row carries no stage-gate authority
(SD-96), so route completion-marker state is never consulted here.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
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
from typing import Callable, Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

import dispatch_contract as DC  # noqa: E402
from dispatch_contract import DispatchContractError  # noqa: E402

SUBSESSION_ADVANCE_RECORD_SCHEMA_VERSION = 1

# The five crash-checkpoint names, 1:1 with A-7's CRASH_MATRIX
# (`dispatch_subsession_advance_crash.test.py`). "before-gate-close" here
# means "before predecessor-terminal is verified" -- there is no per-index
# completion gate for a sub-session (stage_authority=0); the name is kept
# identical to that other module's vocabulary only because the plan
# fixes these five names structurally, not because the same semantic gate
# exists at index granularity.
CHECKPOINTS = (
    "before-gate-close",
    "before-claim",
    "before-register",
    "before-start",
    "after-start",
)

# Same terminal-status vocabulary `PARENT_EXTINCTION_TERMINAL_STATUSES` uses
# (dispatch_contract.py:611) -- one registry-row status truth, not a second one.
TERMINAL_STATUSES = frozenset({"done", "killed", "cancelled"})


class SubsessionAdvanceError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class SubsessionAdvanceRequest:
    jobs: Path
    route_id: str
    route_hash: str
    route_node: str
    chain_id: str
    manifest_sha256: str
    predecessor_subsession_id: str
    predecessor_terminal_attempt_id: str
    successor_subsession_index: int
    successor_session: dict
    parent_attempt_id: str
    advance_generation: int = 0


@dataclass(frozen=True)
class SubsessionAdvanceClaim:
    subsession_advance_id: str
    claim_key: tuple
    successor_attempt_id: str
    replayed: bool


@dataclass(frozen=True)
class SubsessionAdvanceResult:
    outcome: str
    reason: str
    subsession_advance_id: str
    successor_subsession_index: int | None
    successor_attempt_id: str | None
    claim_key: tuple | None
    registered: bool
    started: bool
    child_spawned: bool
    record_path: Path | None


class SubsessionAdvanceServices(Protocol):
    """Checked mutation/observation boundary. Tests inject a deterministic impl."""

    def sealed_manifest_sha256(self, request: SubsessionAdvanceRequest) -> str: ...

    def predecessor_terminal(self, request: SubsessionAdvanceRequest) -> bool: ...

    def claim(
        self, request: SubsessionAdvanceRequest, *, subsession_advance_id: str, claim_key: tuple
    ) -> SubsessionAdvanceClaim: ...

    def register_successor(
        self, request: SubsessionAdvanceRequest, *, claim: SubsessionAdvanceClaim
    ) -> dict: ...

    def start_successor(
        self, request: SubsessionAdvanceRequest, *, claim: SubsessionAdvanceClaim
    ) -> dict: ...


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    """Same tempfile+fsync+os.replace shape SD-110's shared stage-advance
    module uses for its own `_atomic_json` -- allowed local duplication (that
    module's own header comment documents the precedent)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
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


def subsession_advance_record_path(jobs: Path, subsession_advance_id: str) -> Path:
    if not subsession_advance_id.startswith("ssadv-"):
        raise SubsessionAdvanceError("subsession-advance-record-identity-invalid")
    return jobs.parent / "subsession_advance" / f"{subsession_advance_id}.json"


@contextlib.contextmanager
def _subsession_advance_transaction_lock(jobs: Path, subsession_advance_id: str):
    lock_dir = jobs.parent / "subsession_advance" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{subsession_advance_id}.lock"
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def canonical_subsession_advance_id(
    *,
    route_id: str,
    route_hash: str,
    route_node: str,
    chain_id: str,
    manifest_sha256: str,
    predecessor_subsession_id: str,
    predecessor_terminal_attempt_id: str,
    successor_subsession_index: int,
) -> str:
    """§13.35.1-(2) hash input order, fixed: `route_id`, `route_hash`,
    `route_node`, `chain_id`, `manifest_sha256`, `predecessor_subsession_id`,
    `predecessor_terminal_attempt_id`, `successor_subsession_index`.

    `manifest_sha256` is IN the identity, so a manifest-drift replay never
    collides with the pre-drift identity -- drift resolves to a refusal
    (`coordinate_subsession_advance`), never a silent re-claim under the old id.
    """

    identity = {
        "route_id": route_id,
        "route_hash": route_hash,
        "route_node": route_node,
        "chain_id": chain_id,
        "manifest_sha256": manifest_sha256,
        "predecessor_subsession_id": predecessor_subsession_id,
        "predecessor_terminal_attempt_id": predecessor_terminal_attempt_id,
        "successor_subsession_index": successor_subsession_index,
    }
    required = {k: v for k, v in identity.items() if k != "successor_subsession_index"}
    if any(not value for value in required.values()):
        raise SubsessionAdvanceError("subsession-advance-identity-incomplete")
    return "ssadv-" + hashlib.sha256(_canonical_bytes(identity)).hexdigest()


def _registry_rows_for_chain(jobs: Path, chain_id: str) -> list[dict]:
    if not jobs.is_file():
        return []
    out = []
    for line in jobs.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        metadata = dict(part.split("=", 1) for part in fields[5].split(",") if "=" in part)
        if metadata.get("session_chain_id") != chain_id:
            continue
        out.append({"status": fields[1], "metadata": metadata})
    return out


def resume_index(jobs: Path, manifest: dict) -> int:
    """§13.35.1-(2): `min{ i : index i has no terminal row }`. An already
    terminal earlier index is never restarted. Never the route completion-marker
    census helper."""

    terminal_indexes: set[int] = set()
    for row in _registry_rows_for_chain(jobs, manifest.get("chain_id", "")):
        if row["status"] not in TERMINAL_STATUSES:
            continue
        raw_index = row["metadata"].get("subsession_index")
        if raw_index is None:
            continue
        try:
            terminal_indexes.add(int(raw_index))
        except ValueError:
            continue
    for session in manifest["sessions"]:
        if session["index"] not in terminal_indexes:
            return session["index"]
    return manifest["sessions"][-1]["index"] + 1


def chain_manifest_pointer_path(jobs: Path, chain_id: str) -> Path:
    """Same canonical location `stage-session-chain.py` persists the sealed
    manifest to at registration time -- the only durable place a later,
    unrelated supervisor process can find it from, since the original
    `--manifest` envelope path is caller-local."""

    return jobs.parent / "session_chains" / f"{chain_id}.json"


def load_chain_manifest(jobs: Path, chain_id: str) -> dict | None:
    path = chain_manifest_pointer_path(jobs, chain_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def coordinate_chain_advance_from_joined_rows(
    jobs: Path, parent_attempt_id: str, joined: dict,
) -> str | None:
    """Shared, harness-agnostic hook both session supervisors call right
    after their repark join and before their own route-level stage advance
    (plan §1 D-C anchor). Returns the next child's `attempt_id` when this
    round's joined child belongs to an unfinished serial sub-session chain;
    `None` when there is no chain at all -- the ordinary, unmodified flow's
    byte-identical no-op case -- or the chain has no further index to
    advance to (already fully advanced, or genuinely complete)."""

    predecessor_row = None
    for row in joined.values():
        metadata = getattr(row, "metadata", None) or {}
        if metadata.get("session_chain_id") and metadata.get("subsession_mode") == "serial":
            predecessor_row = row
            break
    if predecessor_row is None:
        return None
    metadata = predecessor_row.metadata
    if predecessor_row.status not in TERMINAL_STATUSES:
        return None

    chain_id = metadata["session_chain_id"]
    manifest = load_chain_manifest(jobs, chain_id)
    if manifest is None:
        return None
    try:
        predecessor_index = int(metadata.get("subsession_index", "0"))
    except ValueError:
        return None

    successor_index = resume_index(jobs, manifest)
    if successor_index > len(manifest["sessions"]) or successor_index <= predecessor_index:
        return None
    successor_session = next(
        (s for s in manifest["sessions"] if s["index"] == successor_index), None
    )
    if successor_session is None:
        return None

    request = SubsessionAdvanceRequest(
        jobs=jobs,
        route_id=metadata.get("route_id", ""),
        route_hash=metadata.get("route_hash", ""),
        route_node=metadata.get("route_node", ""),
        chain_id=chain_id,
        manifest_sha256=manifest.get("_manifest_sha256", ""),
        predecessor_subsession_id=metadata.get("subsession_id", ""),
        predecessor_terminal_attempt_id=predecessor_row.attempt_id,
        successor_subsession_index=successor_index,
        successor_session=successor_session,
        parent_attempt_id=parent_attempt_id,
    )
    services = RealSubsessionAdvanceServices(manifest)
    result = coordinate_subsession_advance(request, services)
    if result.outcome == "advanced" and result.successor_attempt_id:
        return result.successor_attempt_id
    return None


def subsession_advances(jobs: Path, chain_id: str) -> int:
    """Committed `ssadv-*.json` count for this chain, `outcome == "advanced"`
    only -- the sole source of `subsession_advances` (A-4)."""

    directory = jobs.parent / "subsession_advance"
    if not directory.is_dir():
        return 0
    count = 0
    for path in directory.glob("ssadv-*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("chain_id") == chain_id and record.get("outcome") == "advanced":
            count += 1
    return count


# --------------------------------------------------------------------------
# Transaction core
# --------------------------------------------------------------------------


def _seed_record(subsession_advance_id: str, request: SubsessionAdvanceRequest) -> dict:
    return {
        "schema_version": SUBSESSION_ADVANCE_RECORD_SCHEMA_VERSION,
        "subsession_advance_id": subsession_advance_id,
        "chain_id": request.chain_id,
        "route_id": request.route_id,
        "route_hash": request.route_hash,
        "route_node": request.route_node,
        "predecessor_subsession_id": request.predecessor_subsession_id,
        "predecessor_terminal_attempt_id": request.predecessor_terminal_attempt_id,
        "successor_subsession_index": request.successor_subsession_index,
        "phases": {},
        "outcome": None,
        "reason": "",
    }


def _load_record(path: Path, seed: dict) -> dict:
    if not path.is_file():
        return seed
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SubsessionAdvanceError("subsession-advance-record-invalid", str(exc)) from exc
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != SUBSESSION_ADVANCE_RECORD_SCHEMA_VERSION
        or record.get("subsession_advance_id") != seed["subsession_advance_id"]
    ):
        raise SubsessionAdvanceError("subsession-advance-record-binding-mismatch")
    return record


def _refused(
    reason: str, *, subsession_advance_id: str = "", record_path: Path | None = None
) -> SubsessionAdvanceResult:
    return SubsessionAdvanceResult(
        outcome="refused",
        reason=reason,
        subsession_advance_id=subsession_advance_id,
        successor_subsession_index=None,
        successor_attempt_id=None,
        claim_key=None,
        registered=False,
        started=False,
        child_spawned=False,
        record_path=record_path,
    )


def _result_from_record(record: dict, record_path: Path) -> SubsessionAdvanceResult:
    phases = record.get("phases") or {}
    registered = "registered" in phases
    started_result = (phases.get("started") or {}).get("result") or {}
    started = bool(started_result.get("child_spawned"))
    claim_evidence = (phases.get("claimed") or {}).get("claim") or {}
    return SubsessionAdvanceResult(
        outcome=record.get("outcome") or "refused",
        reason=record.get("reason") or "",
        subsession_advance_id=record.get("subsession_advance_id"),
        successor_subsession_index=record.get("successor_subsession_index"),
        successor_attempt_id=claim_evidence.get("successor_attempt_id"),
        claim_key=tuple(claim_evidence["claim_key"]) if claim_evidence.get("claim_key") else None,
        registered=registered,
        started=started,
        child_spawned=bool(started_result.get("child_spawned")),
        record_path=record_path,
    )


def coordinate_subsession_advance(
    request: SubsessionAdvanceRequest,
    services: SubsessionAdvanceServices,
    *,
    checkpoint: Callable[[str], None] | None = None,
) -> SubsessionAdvanceResult:
    def cp(name: str) -> None:
        if checkpoint is not None:
            checkpoint(name)

    # Manifest drift is decided BEFORE the claim is attempted (plan §3 R2):
    # the sealed chain manifest hash and the request's claimed hash must
    # match, or this advance is refused outright -- claim 0, successor row 0,
    # start 0. `manifest_sha256` is already part of the advance identity, so a
    # drifted request can never accidentally reuse a pre-drift record.
    sealed = services.sealed_manifest_sha256(request)
    if sealed != request.manifest_sha256:
        return _refused("subsession-chain-manifest-drift")

    subsession_advance_id = canonical_subsession_advance_id(
        route_id=request.route_id,
        route_hash=request.route_hash,
        route_node=request.route_node,
        chain_id=request.chain_id,
        manifest_sha256=request.manifest_sha256,
        predecessor_subsession_id=request.predecessor_subsession_id,
        predecessor_terminal_attempt_id=request.predecessor_terminal_attempt_id,
        successor_subsession_index=request.successor_subsession_index,
    )

    with _subsession_advance_transaction_lock(request.jobs, subsession_advance_id):
        record_path = subsession_advance_record_path(request.jobs, subsession_advance_id)
        seed = _seed_record(subsession_advance_id, request)
        record = _load_record(record_path, seed)

        if record.get("outcome") is not None:
            return _result_from_record(record, record_path)

        if "gate_closed" not in record["phases"]:
            cp("before-gate-close")
            if not services.predecessor_terminal(request):
                record["outcome"] = "refused"
                record["reason"] = "subsession-advance-predecessor-not-terminal"
                _atomic_json(record_path, record)
                return _refused(
                    "subsession-advance-predecessor-not-terminal",
                    subsession_advance_id=subsession_advance_id,
                    record_path=record_path,
                )
            record["phases"]["gate_closed"] = {"committed_at_ns": time.time_ns()}
            _atomic_json(record_path, record)

        claim_key = (
            request.route_hash,
            request.route_node,
            request.chain_id,
            request.successor_subsession_index,
            request.advance_generation,
        )
        if "claimed" not in record["phases"]:
            cp("before-claim")
            try:
                claim = services.claim(
                    request, subsession_advance_id=subsession_advance_id, claim_key=claim_key
                )
            except DispatchContractError as exc:
                if exc.reason == "subsession-advance-claim-conflict":
                    record["outcome"] = "refused"
                    record["reason"] = "subsession-advance-claim-conflict"
                    _atomic_json(record_path, record)
                    return _refused(
                        "subsession-advance-claim-conflict",
                        subsession_advance_id=subsession_advance_id,
                        record_path=record_path,
                    )
                raise
            record["phases"]["claimed"] = {
                "committed_at_ns": time.time_ns(),
                "claim": {
                    "subsession_advance_id": claim.subsession_advance_id,
                    "claim_key": list(claim.claim_key),
                    "successor_attempt_id": claim.successor_attempt_id,
                    "replayed": claim.replayed,
                },
            }
            _atomic_json(record_path, record)
        else:
            claim_evidence = record["phases"]["claimed"]["claim"]
            claim = SubsessionAdvanceClaim(
                subsession_advance_id=claim_evidence["subsession_advance_id"],
                claim_key=tuple(claim_evidence["claim_key"]),
                successor_attempt_id=claim_evidence["successor_attempt_id"],
                replayed=True,
            )

        if "registered" not in record["phases"]:
            cp("before-register")
            register_result = services.register_successor(request, claim=claim)
            record["phases"]["registered"] = {
                "committed_at_ns": time.time_ns(), "result": register_result,
            }
            _atomic_json(record_path, record)

        if "started" not in record["phases"]:
            cp("before-start")
            start_result = services.start_successor(request, claim=claim)
            record["phases"]["started"] = {
                "committed_at_ns": time.time_ns(), "result": start_result,
            }
            record["outcome"] = "advanced" if start_result.get("child_spawned") else "refused"
            record["reason"] = (
                "" if start_result.get("child_spawned")
                else "subsession-advance-successor-start-failed"
            )
            _atomic_json(record_path, record)
            cp("after-start")

        return _result_from_record(record, record_path)


# --------------------------------------------------------------------------
# Block: real subprocess-backed services -- the only implementation that
# mutates the registry or spawns a successor. Test suites use only a
# `FakeServices` injected boundary. Reuses `stage-session-chain.py`'s own
# `dispatch_command()` so the register/start argv shape never forks.
# --------------------------------------------------------------------------


def _load_chain_module():
    spec = importlib.util.spec_from_file_location(
        "dispatch_subsession_advance_chain", ROOT / "utilities" / "stage-session-chain.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealSubsessionAdvanceServices:
    def __init__(self, manifest: dict):
        self._manifest = manifest
        self._chain = _load_chain_module()

    def sealed_manifest_sha256(self, request: SubsessionAdvanceRequest) -> str:
        return self._manifest.get("_manifest_sha256", "")

    def predecessor_terminal(self, request: SubsessionAdvanceRequest) -> bool:
        # Newest row for this attempt_id wins -- a row can be superseded by a
        # later status update for the same attempt (open -> done).
        status = None
        for row in _registry_rows_for_chain(request.jobs, request.chain_id):
            if row["metadata"].get("attempt_id") != request.predecessor_terminal_attempt_id:
                continue
            status = row["status"]
        return status in TERMINAL_STATUSES

    def claim(
        self, request: SubsessionAdvanceRequest, *, subsession_advance_id: str, claim_key: tuple
    ) -> SubsessionAdvanceClaim:
        registry_claim = DC.claim_subsession_advance(
            request.jobs,
            subsession_advance_id=subsession_advance_id,
            route_hash=claim_key[0],
            route_node=claim_key[1],
            chain_id=claim_key[2],
            successor_subsession_index=claim_key[3],
            advance_generation=claim_key[4],
            successor_attempt_id=request.successor_session["attempt_id"],
        )
        return SubsessionAdvanceClaim(
            subsession_advance_id=registry_claim.subsession_advance_id,
            claim_key=registry_claim.claim_key,
            successor_attempt_id=registry_claim.successor_attempt_id,
            replayed=registry_claim.replayed,
        )

    def register_successor(self, request: SubsessionAdvanceRequest, *, claim) -> dict:
        command = self._chain.dispatch_command(
            self._manifest, request.successor_session, "register",
            request.parent_attempt_id, request.jobs,
        )
        result = self._chain.run_checked(command)
        return {"returncode": result.returncode}

    def start_successor(self, request: SubsessionAdvanceRequest, *, claim) -> dict:
        command = self._chain.dispatch_command(
            self._manifest, request.successor_session, "start",
            request.parent_attempt_id, request.jobs,
        )
        result = self._chain.run_checked(command)
        return {"child_spawned": result.returncode == 0, "returncode": result.returncode}


if __name__ == "__main__":
    raise SystemExit(
        "dispatch_subsession_advance.py is a library module, not a CLI entry point"
    )
