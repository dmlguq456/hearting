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
import dispatch_subsession_handoff as HANDOFF  # noqa: E402
import dispatch_subsession_resume_record as RESUME_RECORD  # noqa: E402

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
    artifact_root: str = ""
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

    def classify_handoff(self, request: SubsessionAdvanceRequest) -> str: ...

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

    # F-5 (impl-review round 1): a single completeness check over the 7
    # required hash inputs -- `successor_subsession_index` is excluded (index
    # 0 is never valid but is a legitimate falsy int elsewhere, so it is
    # proven by the claim-key/index range checks, not here) -- replacing a
    # prior two-dict-construction (`identity` then a filtered `required`
    # copy) that built the same 7 values twice for one `any(...)` check.
    if not all((
        route_id, route_hash, route_node, chain_id, manifest_sha256,
        predecessor_subsession_id, predecessor_terminal_attempt_id,
    )):
        raise SubsessionAdvanceError("subsession-advance-identity-incomplete")
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
        out.append({"status": fields[1], "metadata": metadata, "timestamp": fields[0]})
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


def _resolve_artifact_root() -> str:
    """The one canonical writable artifact root (CLAUDE.md `Runtime Router`),
    resolved the same way every other harness surface resolves it -- never a
    supervisor-local guess."""

    try:
        result = subprocess.run(
            [str(ROOT / "utilities" / "artifact-root.sh")],
            cwd=ROOT, text=True, capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


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
    artifact_root = _resolve_artifact_root()
    # F-1 (impl-review round 2): THE production connection between a
    # predecessor's committed terminal registry row and its chain-scoped
    # handoff. `flush_own_subsession_handoff()` below cannot serve this --
    # it reads the CALLING process's `AGENT_DISPATCH_SUBSESSION_ID`, and a
    # sub-session child is an ordinary dispatch-depth-2 worker with no
    # supervisor process of its own (supervised completion is scoped to
    # dispatch-depth-1 owners, `adapters/*/bin/dispatch-headless.py`
    # `resolve_completion_delivery`), so nothing in the child's own runtime
    # ever reaches that call site. The supervisor that observes the terminal
    # row flushes it here instead, from the predecessor's own durable state
    # ledger, strictly AFTER that row committed (the terminal check above)
    # and strictly BEFORE the successor's handoff gate reads it.
    #
    # This does NOT make A-8's hard stop unreachable: the flush is a
    # projection of the predecessor's ledger, so a predecessor that never
    # maintained a readable ledger bound to its own attempt id produces no
    # handoff at all and the advance still refuses `subsession-handoff-
    # missing` (or `-stale`, when an earlier index's handoff is still there).
    flush_chain_handoff_for_row(jobs, predecessor_row, manifest, artifact_root)
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
        artifact_root=artifact_root,
    )
    services = RealSubsessionAdvanceServices(manifest)
    result = coordinate_subsession_advance(request, services)
    if result.outcome == "advanced" and result.successor_attempt_id:
        return result.successor_attempt_id
    return None


def _load_ledger_module():
    spec = importlib.util.spec_from_file_location(
        "dispatch_subsession_advance_ledger", ROOT / "utilities" / "worker-state-ledger.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def flush_own_subsession_handoff(jobs: Path, route_id: str, attempt_id: str) -> None:
    """SD-119 R3 impl-review fix (F-1): the flush path for a sub-session that
    happens to run under a session supervisor of its OWN (only a supervised
    attempt reaches this call site). The production path for an ordinary
    unsupervised sub-session child is `flush_chain_handoff_for_row()`, driven
    by the chain-advancing supervisor. Called immediately AFTER its own terminal
    registry row commits (i.e. right after `reconcile()` returns), never
    before -- so the handoff's mtime is always later in wall-clock time than
    the registry's recorded terminal timestamp, and index i+1's
    `classify_handoff` mtime-inversion stale check (SD §13.35.1-(5)
    condition 3) never fires for a legitimate flush. Content is synthesized
    from this attempt's own state ledger (already required for every
    sub-session), not from a second worker-authored artifact.

    No-op (byte-identical) for a non-subsession attempt or a subsession
    outside a serial chain -- the only two cases where
    `AGENT_DISPATCH_SESSION_CHAIN_ID`/`AGENT_DISPATCH_SUBSESSION_MODE` are
    unset or not `"serial"` at this call site."""

    subsession_id = os.environ.get("AGENT_DISPATCH_SUBSESSION_ID", "")
    chain_id = os.environ.get("AGENT_DISPATCH_SESSION_CHAIN_ID", "")
    mode = os.environ.get("AGENT_DISPATCH_SUBSESSION_MODE", "")
    if not subsession_id or not chain_id or mode != "serial":
        return
    manifest = load_chain_manifest(Path(jobs), chain_id)
    if manifest is None:
        return
    _flush_handoff_from_ledger(
        artifact_root=_resolve_artifact_root(),
        route_id=route_id,
        chain_id=chain_id,
        attempt_id=attempt_id,
        subsession_id=subsession_id,
        manifest_sha256=manifest.get("_manifest_sha256", ""),
        ledger_path=os.environ.get("AGENT_WORKER_STATE_LEDGER", ""),
        require_ledger=False,
    )


def _flush_handoff_from_ledger(
    *,
    artifact_root: str,
    route_id: str,
    chain_id: str,
    attempt_id: str,
    subsession_id: str,
    manifest_sha256: str,
    ledger_path: str,
    require_ledger: bool,
) -> str:
    """The one handoff writer both call sites share. Returns a typed outcome
    so a caller (and a fixture) can tell "flushed" from each reason a flush
    was declined, without either call site raising into a supervisor loop."""

    if not artifact_root:
        return "artifact-root-unavailable"
    fields: dict = {}
    if ledger_path:
        fields = _load_ledger_module().read_fields(Path(ledger_path), attempt_id)
    if require_ledger and not fields:
        # No ledger bound to this exact attempt id -- there is nothing durable
        # to carry forward, so writing a handoff here would fabricate evidence
        # of a completion the predecessor never recorded. Declining keeps
        # A-8's `subsession-handoff-missing` hard stop reachable.
        return "ledger-unavailable"
    HANDOFF.flush_handoff(
        HANDOFF.handoff_path(artifact_root, route_id, chain_id),
        predecessor_attempt_id=attempt_id,
        predecessor_subsession_id=subsession_id,
        manifest_sha256=manifest_sha256,
        completed_items=list(fields.get("completed_items") or []),
        next_command=str(fields.get("next_action") or ""),
        invariants=list(fields.get("invariants") or []),
        forbidden_files=list(fields.get("forbidden_files") or []),
    )
    return "flushed"


def flush_chain_handoff_for_row(
    jobs: Path, row, manifest: dict, artifact_root: str,
) -> str:
    """F-1: flush the chain-scoped handoff on behalf of a serial sub-session
    child whose terminal row the caller has already observed, deriving every
    field from that row's sealed registry metadata and the predecessor's own
    state ledger -- never from the calling process's environment, which
    belongs to the supervisor, not to the child.

    Outcomes: `flushed` | `no-chain` | `artifact-root-unavailable` |
    `ledger-unavailable`. Only `flushed` produces a file; every other outcome
    leaves the handoff gate to decide `missing`/`stale` on its own evidence."""

    metadata = getattr(row, "metadata", None) or {}
    if not metadata.get("session_chain_id") or metadata.get("subsession_mode") != "serial":
        return "no-chain"
    return _flush_handoff_from_ledger(
        artifact_root=artifact_root,
        route_id=metadata.get("route_id", ""),
        chain_id=metadata["session_chain_id"],
        attempt_id=row.attempt_id,
        subsession_id=metadata.get("subsession_id", ""),
        manifest_sha256=manifest.get("_manifest_sha256", ""),
        ledger_path=metadata.get("state_ledger", ""),
        require_ledger=True,
    )


def record_owner_resume_if_chain(
    jobs: Path, joined: dict, last_advanced_attempt_id: str | None,
) -> None:
    """SD-119 impl-review fix (F-2): the real call site for A-4's owner-resume
    census. Called once per join round, immediately after the chain-advance
    loop in `claude-session-supervisor.py`/`codex-app-server-supervisor.py`
    finishes advancing as far as the sealed manifest allows -- i.e. exactly
    at the point this round's join is about to be delivered to the owner
    (either the no-further-model-turn terminal fast path or the
    `resume = True` continuation path). Never called from inside
    `coordinate_subsession_advance()` itself, so an internal advance commits
    zero resume events (SD §13.35.1-(3)).

    No-op (byte-identical) when `joined` (as captured BEFORE the
    chain-advance loop ran) carries no serial chain metadata at all."""

    chain_id = route_id = route_hash = route_node = ""
    predecessor_attempt_id = None
    for row in joined.values():
        metadata = getattr(row, "metadata", None) or {}
        if metadata.get("session_chain_id") and metadata.get("subsession_mode") == "serial":
            chain_id = metadata["session_chain_id"]
            route_id = metadata.get("route_id", "")
            route_hash = metadata.get("route_hash", "")
            route_node = metadata.get("route_node", "")
            predecessor_attempt_id = row.attempt_id
            break
    if not chain_id:
        return
    manifest = load_chain_manifest(Path(jobs), chain_id)
    manifest_sha256 = manifest.get("_manifest_sha256", "") if manifest else ""
    final_attempt_id = last_advanced_attempt_id or predecessor_attempt_id or ""
    delivery_id = hashlib.sha256(
        f"{chain_id}:{final_attempt_id}".encode("utf-8")
    ).hexdigest()
    RESUME_RECORD.record_resume(
        Path(jobs).parent,
        route_id=route_id, route_hash=route_hash, route_node=route_node,
        chain_id=chain_id, manifest_sha256=manifest_sha256, delivery_id=delivery_id,
    )


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
            # SD-119 R3 (A-8): a missing or stale chain-scoped handoff hard
            # stops here -- claim 0, register 0, start 0. Never proceeds on
            # the assumption that index i+1 can reconstruct context another way.
            handoff_classification = services.classify_handoff(request)
            if handoff_classification != "ok":
                record["outcome"] = "refused"
                record["reason"] = handoff_classification
                _atomic_json(record_path, record)
                return _refused(
                    handoff_classification,
                    subsession_advance_id=subsession_advance_id,
                    record_path=record_path,
                )
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

    def _predecessor_terminal_at_ns(self, request: SubsessionAdvanceRequest) -> int:
        from datetime import datetime, timezone

        for row in _registry_rows_for_chain(request.jobs, request.chain_id):
            if row["metadata"].get("attempt_id") != request.predecessor_terminal_attempt_id:
                continue
            try:
                parsed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return int(parsed.timestamp() * 1_000_000_000)
            except (AttributeError, ValueError):
                return 0
        return 0

    def classify_handoff(self, request: SubsessionAdvanceRequest) -> str:
        if not request.artifact_root:
            return "subsession-handoff-missing"
        path = HANDOFF.handoff_path(request.artifact_root, request.route_id, request.chain_id)
        return HANDOFF.classify_handoff(
            path,
            predecessor_attempt_id_expected=request.predecessor_terminal_attempt_id,
            manifest_sha256_expected=request.manifest_sha256,
            predecessor_terminal_at_ns=self._predecessor_terminal_at_ns(request),
        )

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
