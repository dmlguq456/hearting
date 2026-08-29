#!/usr/bin/env python3
"""W7C artifact write-cutover: producer begin/finalize lifecycle.

Correction to the W7 relocation: new cycle output is written in place under
`<artifact-root>/campaigns/<campaign_id>/cycles/<cycle_id>/artifacts/` and the
typed IDs are issued by `begin` *before* the first write (D-2, D-4).  The
step-1 modules stay the only lineage authorities: `artifact_identity` issues
IDs, `artifact_manifest` validates the closed D-6 schema, `artifact_index`
guards uniqueness, and `artifact_admission` owns the root identity, the global
mutex, and the derived index.

Layout (D-2, closed):

    campaigns/<camp>/campaign.json                    mutable campaign record
    campaigns/<camp>/cycles/<cyc>/artifacts/...       producer output (open)
    campaigns/<camp>/cycles/<cyc>/manifest.json       finalize commit point
    shared/<spec|analysis|research>/<ref>/reference.json
    shared/<kind>/<ref>/revisions/<rrev>/...          immutable revision
    .runtime/artifact-producer/v1/cutover.json        cutover state (approval-gated)
    .runtime/artifact-producer/v1/cycles/<cyc>.json   cycle record open|sealed|abandoned
    .runtime/artifact-producer/v1/journal/<cyc>.json  finalize crash journal
    .runtime/artifact-producer/v1/shared-journal/<rrev>.json

Two cutover states.  While `cutover.json` is absent (`inactive`) the legacy
top-level buckets remain writable (compatibility window) and `begin` reports
`layout=legacy`; once the approval package activates the root, `begin` issues
a cycle and every new write outside an open cycle's `artifacts/` is denied.
Shared revisions are immutable in both states and are only created by
`admit-shared` from a sealed cycle.  Research is admitted to `shared/` only
with an explicit promotion (D-3).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_index  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_manifest  # noqa: E402
from dispatch_contract import process_start_ticks  # noqa: E402

PRODUCER_REL = ".runtime/artifact-producer/v1"
CONTRACT = "artifact-producer/v1"
ALGORITHM_VERSION = "w7c-producer/v1"
OK, BLOCKED, USAGE = 0, 65, 64

# SD-117 §13.34.5-(2): a cycle's abandonment sealing decision must always
# name why -- a closed enum, disjoint from review verdict vocabulary
# (PASS/FAIL/BLOCKED, allow/deny) so the two can never be confused (E47-7).
ABANDON_REASONS = frozenset({
    "operator-decision",
    "route-unrecoverable",
    "lease-expired-no-publisher",
    "operator-override-live-review",
})
REVIEW_LEASE_REL = "review-leases"

INTENSITIES = ("direct", "quick", "standard", "strong", "thorough", "adversarial")
ENTRY_CAPABILITIES = (
    "analyze-project", "analyze-user", "audit", "autopilot-apply", "autopilot-code",
    "autopilot-design", "autopilot-draft", "autopilot-lab", "autopilot-refine",
    "autopilot-research", "autopilot-ship", "autopilot-spec",
)
STAGE_CAPABILITIES = (
    "code-plan", "code-execute", "code-refine", "code-report", "code-test",
    "design-init", "design-refs", "design-tokens", "design-components",
    "design-review", "design-handoff", "draft-strategy", "draft-refine",
)
CANONICAL_ROOTS = ("campaigns", "shared")
# Legacy capability buckets (CORE.md §3 C-DUR) plus the undeclared containers.
LEGACY_TOP_LEVEL = (
    "analysis_project", "research", "spec", "plans", "documents", "experiments",
    "designs", "_internal", "reviews", "shards", "routes", "_routes", "notes",
    "proposals", "spec-research-alternative", "research-alternative", "release-config",
    "evidence", "dev_logs", "test_logs", "user_profile",
)
SHARED_KINDS = {
    "spec": "shared-spec",
    "analysis": "cumulative-analysis",
    "research": "shared-research",
}
BUCKET_TYPES = {
    "plans": "plan", "documents": "document", "designs": "design", "spec": "spec",
    "research": "research", "experiments": "experiment", "analysis_project": "analysis",
    "analysis": "analysis", "reviews": "review", "release-config": "release-config",
    "apply-log": "apply-log", "user_profile": "profile",
}
MEDIA_TYPES = {
    ".md": "text/markdown", ".json": "application/json", ".yaml": "application/yaml",
    ".yml": "application/yaml", ".txt": "text/plain", ".csv": "text/csv",
    ".html": "text/html", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf",
    ".py": "text/x-python", ".sh": "text/x-shellscript", ".log": "text/plain",
    ".jsonl": "application/x-ndjson", ".toml": "application/toml",
}
PRIMARY_CANDIDATES = (
    "final_report.md", "report.md", "prd.md", "plan.md", "handoff.md", "verdict.json",
)
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REL_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/ -]{0,1023}$")


class ProducerError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# small filesystem helpers
# ---------------------------------------------------------------------------


def _rfc3339(now: Optional[float] = None) -> str:
    t = time.time() if now is None else now
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + "Z"


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, data: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _ensure_dir(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ProducerError("path-not-directory", str(path))
        return
    path.mkdir(parents=True, exist_ok=True)


def _walk_files(top: Path) -> List[Path]:
    out: List[Path] = []
    for current, dirs, files in os.walk(str(top), followlinks=False):
        dirs.sort()
        for name in sorted(files):
            out.append(Path(current) / name)
        for name in list(dirs):
            if os.path.islink(os.path.join(current, name)):
                out.append(Path(current) / name)
                dirs.remove(name)
    return out


def _copy_tree_files(source: Path, target: Path) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """Copy regular files only. Returns (rows, violations)."""
    rows: List[Tuple[str, str, int]] = []
    violations: List[str] = []
    if source.is_file():
        entries = [source]
        base = source.parent
    else:
        entries = _walk_files(source)
        base = source
    for entry in entries:
        rel = entry.relative_to(base).as_posix()
        if os.path.islink(str(entry)):
            violations.append(f"symlink-forbidden:{rel}")
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        data = entry.read_bytes()
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(dst, data)
        rows.append((rel, _digest(data), len(data)))
    for current, _dirs, _files in os.walk(str(target)):
        _fsync_dir(Path(current))
    return rows, violations


# ---------------------------------------------------------------------------
# state paths
# ---------------------------------------------------------------------------


def producer_dir(root: Path) -> Path:
    return Path(root) / PRODUCER_REL


def cutover_path(root: Path) -> Path:
    return producer_dir(root) / "cutover.json"


def cycle_record_path(root: Path, cycle_id: str) -> Path:
    return producer_dir(root) / "cycles" / f"{cycle_id}.json"


def journal_path(root: Path, cycle_id: str) -> Path:
    return producer_dir(root) / "journal" / f"{cycle_id}.json"


def shared_journal_path(root: Path, revision_id: str) -> Path:
    return producer_dir(root) / "shared-journal" / f"{revision_id}.json"


def campaign_dir(root: Path, campaign_id: str) -> Path:
    return Path(root) / "campaigns" / campaign_id


def cycle_dir(root: Path, campaign_id: str, cycle_id: str) -> Path:
    return campaign_dir(root, campaign_id) / "cycles" / cycle_id


def read_cutover(root: Path) -> Dict[str, Any]:
    value = _read_json(cutover_path(root))
    if value is None:
        return {"state": "inactive"}
    return value


def is_active(root: Path) -> bool:
    return read_cutover(root).get("state") == "active"


def read_cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(cycle_record_path(root, cycle_id))


def _write_cycle_record(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = cycle_record_path(root, record["cycle_id"])
    _ensure_dir(path.parent)
    data = _json_bytes(record)
    if exclusive:
        _write_exclusive(path, data, 0o600)
    else:
        _write_atomic(path, data, 0o600)


def list_cycle_records(root: Path) -> List[Dict[str, Any]]:
    directory = producer_dir(root) / "cycles"
    rows: List[Dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.suffix == ".json":
            value = _read_json(entry)
            if value is not None:
                rows.append(value)
    return rows


def _write_journal(root: Path, cycle_id: str, **fields: Any) -> None:
    path = journal_path(root, cycle_id)
    _ensure_dir(path.parent)
    payload = {"schema_version": 1, "cycle_id": cycle_id, "updated_at": _rfc3339()}
    payload.update(fields)
    _write_atomic(path, _json_bytes(payload), 0o600)


def _remove_journal(root: Path, cycle_id: str) -> None:
    try:
        journal_path(root, cycle_id).unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# route helpers
# ---------------------------------------------------------------------------


def load_route(root: Path, route_file: Path) -> Dict[str, Any]:
    route = _read_json(Path(route_file))
    if route is None:
        raise ProducerError("route-unreadable", str(route_file))
    for key in ("route_id", "route_hash", "capability", "effective_intensity", "artifact_root", "nodes"):
        if key not in route:
            raise ProducerError("route-invalid", f"missing {key}")
    if Path(str(route["artifact_root"])).resolve() != Path(root).resolve():
        raise ProducerError("route-artifact-root-mismatch")
    if route["effective_intensity"] not in INTENSITIES:
        raise ProducerError("route-invalid", "effective_intensity")
    return route


def route_is_closed(root: Path, route: Mapping[str, Any]) -> bool:
    try:
        outcome = artifact_lifecycle.canonical_outcome_path(root, route["route_id"])
    except artifact_lifecycle.LifecycleError:
        return False
    return outcome.is_file()


def _route_node(route: Mapping[str, Any], node_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not node_id or node_id == "-":
        return None
    for row in route.get("nodes", []):
        if isinstance(row, dict) and row.get("id") == node_id:
            return row
    raise ProducerError("route-node-unknown", node_id)


# ---------------------------------------------------------------------------
# campaign records
# ---------------------------------------------------------------------------


def _campaign_path(root: Path, campaign_id: str) -> Path:
    return campaign_dir(root, campaign_id) / "campaign.json"


def read_campaign(root: Path, campaign_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_campaign_path(root, campaign_id))


def find_campaign_by_key(root: Path, key: str) -> Optional[Dict[str, Any]]:
    campaigns = Path(root) / "campaigns"
    if not campaigns.is_dir():
        return None
    for entry in sorted(campaigns.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "campaign.json")
        if record and record.get("key") == key and record.get("state") == "active":
            return record
    return None


def _write_campaign(root: Path, record: Dict[str, Any], *, exclusive: bool) -> None:
    path = _campaign_path(root, record["campaign_id"])
    _ensure_dir(path.parent)
    if exclusive:
        _write_exclusive(path, _json_bytes(record))
    else:
        _write_atomic(path, _json_bytes(record))


# ---------------------------------------------------------------------------
# activate / status
# ---------------------------------------------------------------------------


def activate(
    root: Path,
    *,
    repository_id: str,
    artifact_root_id: str,
    w7: Optional[Mapping[str, Any]] = None,
    approval_receipt_sha256: Optional[str] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if not artifact_identity.is_well_formed(repository_id, "repository"):
        raise ProducerError("identity-malformed", "repository_id")
    if not artifact_identity.is_well_formed(artifact_root_id, "artifact_root"):
        raise ProducerError("identity-malformed", "artifact_root_id")
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        existing = artifact_lifecycle.read_root_identity(root)
        if existing is None:
            payload = artifact_identity.RootIdentity(
                schema_version=1,
                artifact_root_id=artifact_root_id,
                repository_id=repository_id,
                issued_at=_rfc3339(now),
                producer_contract_version=artifact_manifest.CONTRACT_VERSION,
            ).to_payload()
            identity_path = artifact_admission._root_identity_path(root)
            _ensure_dir(identity_path.parent)
            _write_exclusive(identity_path, _json_bytes(payload), 0o600)
            identity = artifact_identity.RootIdentity.parse(payload)
            identity_state = "created"
        else:
            if (existing.repository_id, existing.artifact_root_id) != (repository_id, artifact_root_id):
                raise ProducerError("identity-conflict", "root identity already frozen with other ids")
            identity = existing
            identity_state = "matched"
        current = read_cutover(root)
        if current.get("state") == "active":
            if current.get("identity", {}).get("artifact_root_id") != artifact_root_id:
                raise ProducerError("cutover-identity-conflict")
            return {"status": "already-active", "cutover": current, "identity": identity_state}
        body = {
            "schema_version": 1,
            "contract": CONTRACT,
            "state": "active",
            "activated_at": _rfc3339(now),
            "identity": {"repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id},
            "w7": dict(w7 or {}),
            "approval_receipt_sha256": approval_receipt_sha256,
        }
        _ensure_dir(producer_dir(root))
        _write_exclusive(cutover_path(root), _json_bytes(body), 0o600)
        return {"status": "activated", "cutover": body, "identity": identity_state}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def status(root: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = artifact_lifecycle.read_root_identity(root)
    records = list_cycle_records(root)
    counts: Dict[str, int] = {}
    for row in records:
        counts[row.get("state", "?")] = counts.get(row.get("state", "?"), 0) + 1
    journal_dir = producer_dir(root) / "journal"
    pending = sorted(p.stem for p in journal_dir.glob("*.json")) if journal_dir.is_dir() else []
    return {
        "artifact_root": str(root),
        "cutover": read_cutover(root),
        "identity": identity.to_payload() if identity else None,
        "cycle_counts": counts,
        "open_cycles": [r["cycle_id"] for r in records if r.get("state") == "open"],
        "pending_journals": pending,
    }


# ---------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------


def _env_for(root: Path, record: Mapping[str, Any]) -> Dict[str, str]:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"])
    return {
        "AGENT_ARTIFACT_ROOT": str(root),
        "AGENT_ARTIFACT_CAMPAIGN_ID": record["campaign_id"],
        "AGENT_ARTIFACT_CYCLE_ID": record["cycle_id"],
        "AGENT_ARTIFACT_PRODUCER_ID": record["producer_id"],
        "AGENT_ARTIFACT_CYCLE_DIR": str(directory),
        "AGENT_ARTIFACT_OUTPUT_DIR": str(directory / "artifacts"),
    }


def begin(
    root: Path,
    *,
    route_file: Path,
    capability: str,
    intensity: str,
    node_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    campaign_key: Optional[str] = None,
    title: Optional[str] = None,
    goal: Optional[str] = None,
    parent_cycle_id: Optional[str] = None,
    require_cycle: bool = False,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if capability not in ENTRY_CAPABILITIES + STAGE_CAPABILITIES:
        raise ProducerError("capability-unknown", capability)
    if intensity not in INTENSITIES:
        raise ProducerError("intensity-unknown", intensity)
    route = load_route(root, Path(route_file))
    route_capability = route["capability"]
    if capability in ENTRY_CAPABILITIES and route_capability != capability:
        raise ProducerError("route-capability-mismatch", f"{route_capability}!={capability}")
    if route["effective_intensity"] != intensity:
        raise ProducerError("route-intensity-mismatch", f"{route['effective_intensity']}!={intensity}")
    node = _route_node(route, node_id)
    if route_is_closed(root, route):
        raise ProducerError("route-already-closed", route["route_id"])
    if not is_active(root):
        if require_cycle:
            raise ProducerError("cutover-inactive", "activation required before cycle issuance")
        return {
            "status": "legacy-compat",
            "layout": "legacy",
            "route_id": route["route_id"],
            "reason": "cutover-inactive",
            "env": {"AGENT_ARTIFACT_ROOT": str(root)},
        }
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    alloc = allocator or artifact_identity.IdAllocator()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        # Idempotent per route: one open cycle per route.
        for record in list_cycle_records(root):
            if record.get("route_id") == route["route_id"] and record.get("state") == "open":
                if record.get("route_hash") != route["route_hash"]:
                    raise ProducerError("route-hash-drift", record["cycle_id"])
                return {
                    "status": "resumed", "layout": "cycle", "campaign_id": record["campaign_id"],
                    "cycle_id": record["cycle_id"], "producer_id": record["producer_id"],
                    "cycle_dir": str(cycle_dir(root, record["campaign_id"], record["cycle_id"])),
                    "env": _env_for(root, record),
                }
        index = artifact_admission.load_index(root)
        campaign: Optional[Dict[str, Any]] = None
        if campaign_id:
            campaign = read_campaign(root, campaign_id)
            if campaign is None:
                raise ProducerError("campaign-unknown", campaign_id)
            if campaign.get("state") != "active":
                raise ProducerError("campaign-not-active", campaign_id)
        elif campaign_key:
            if not _KEY_RE.match(campaign_key):
                raise ProducerError("campaign-key-invalid", campaign_key)
            campaign = find_campaign_by_key(root, campaign_key)
        if parent_cycle_id:
            parent = read_cycle_record(root, parent_cycle_id)
            if parent is None or parent.get("state") != "sealed":
                raise ProducerError("parent-cycle-not-sealed", parent_cycle_id)
            if campaign is not None and parent.get("campaign_id") != campaign["campaign_id"]:
                raise ProducerError("parent-cycle-campaign-mismatch", parent_cycle_id)
            if campaign is None:
                campaign = read_campaign(root, parent["campaign_id"])
        campaign_created = False
        if campaign is None:
            new_campaign_id = alloc.allocate("campaign")
            while new_campaign_id in index.stable_ids or campaign_dir(root, new_campaign_id).exists():
                new_campaign_id = alloc.allocate("campaign")
            campaign = {
                "schema_version": 1,
                "contract": CONTRACT,
                "campaign_id": new_campaign_id,
                "key": campaign_key or f"{route_capability}:{route['route_id']}",
                "title": title or f"{route_capability} campaign",
                "goal": goal or f"{route_capability} cycle output",
                "completion_criterion": {"statement": "every cycle sealed with a manifest"},
                "state": "active",
                "created_on": _rfc3339(now),
                "cycles": [],
            }
            campaign_created = True
        new_cycle_id = alloc.allocate("cycle")
        while new_cycle_id in index.stable_ids or cycle_record_path(root, new_cycle_id).exists():
            new_cycle_id = alloc.allocate("cycle")
        producer_id = alloc.allocate("producer")
        record = {
            "schema_version": 1,
            "contract": CONTRACT,
            "cycle_id": new_cycle_id,
            "campaign_id": campaign["campaign_id"],
            "producer_id": producer_id,
            "parent_cycle_id": parent_cycle_id,
            "capability": capability,
            "route_capability": route_capability,
            "intensity": intensity,
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_file": str(Path(route_file).resolve()),
            "node_id": node["id"] if node else None,
            "state": "open",
            "started_on": _rfc3339(now),
            "sealed_on": None,
            "manifest_digest": None,
            "title": title or f"{capability} {intensity} cycle",
        }
        target = cycle_dir(root, campaign["campaign_id"], new_cycle_id)
        if target.exists():
            raise ProducerError("cycle-dir-exists", str(target))
        # Order: durable record first (crash before dir => recover drops the
        # record), then the folder.  Nothing here is visible to the index until
        # finalize's manifest commit point.
        _write_cycle_record(root, record, exclusive=True)
        _ensure_dir(target / "artifacts")
        campaign["cycles"] = list(campaign.get("cycles", [])) + [new_cycle_id]
        _write_campaign(root, campaign, exclusive=campaign_created)
        return {
            "status": "begun", "layout": "cycle", "campaign_id": campaign["campaign_id"],
            "cycle_id": new_cycle_id, "producer_id": producer_id, "cycle_dir": str(target),
            "campaign_created": campaign_created, "env": _env_for(root, record),
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def _media_type(rel: str) -> str:
    return MEDIA_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def _bucket_type(rel: str) -> str:
    first = rel.split("/", 1)[0]
    return BUCKET_TYPES.get(first, "file")


def _unmanifestable_reason(rel: str) -> Optional[str]:
    """Why a relocated legacy file cannot carry a D-6 locator (None when it can)."""
    for part in rel.split("/"):
        if part.startswith("."):
            return "hidden-component"
        if not artifact_manifest._LOCATOR_COMPONENT_RE.match(part):
            return "invalid-component"
    return None


def _enumerate_output(directory: Path, *, exclude_hidden: bool = False,
                      excluded: Optional[List[str]] = None) -> Tuple[List[Tuple[str, bytes]], List[str]]:
    """Regular files under `artifacts/`.  With `exclude_hidden`, files whose path
    cannot be a D-6 locator (a dot-component such as `.git/`/`.claude/` runtime
    residue, or a component longer than the locator limit) are left out of the
    manifest and reported through `excluded` instead of failing validation
    (W7E retrospective seal of relocated legacy trees)."""
    rows: List[Tuple[str, bytes]] = []
    violations: List[str] = []
    artifacts = directory / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink():
        raise ProducerError("artifacts-dir-missing", str(artifacts))
    for entry in _walk_files(directory):
        rel = entry.relative_to(directory).as_posix()
        if os.path.islink(str(entry)):
            violations.append(f"symlink-forbidden:{rel}")
            continue
        if not entry.is_file():
            violations.append(f"non-regular-file:{rel}")
            continue
        if not rel.startswith("artifacts/"):
            violations.append(f"file-outside-artifacts:{rel}")
            continue
        if exclude_hidden and _unmanifestable_reason(rel) is not None:
            if excluded is not None:
                excluded.append(rel)
            continue
        if not _REL_RE.match(rel) or ".." in rel.split("/"):
            violations.append(f"unsafe-locator:{rel}")
            continue
        rows.append((rel, entry.read_bytes()))
    return rows, violations


def _choose_primary(rows: Sequence[Tuple[str, bytes]], primary: Optional[str]) -> Optional[str]:
    names = [rel for rel, _ in rows]
    if primary:
        candidate = primary if primary.startswith("artifacts/") else "artifacts/" + primary
        if candidate not in names:
            raise ProducerError("primary-artifact-missing", primary)
        return candidate
    for wanted in PRIMARY_CANDIDATES:
        for rel in names:
            if rel.endswith("/" + wanted) or rel == "artifacts/" + wanted:
                return rel
    return names[0] if names else None


def build_manifest(
    root: Path,
    record: Mapping[str, Any],
    route: Mapping[str, Any],
    rows: Sequence[Tuple[str, bytes]],
    *,
    state: str,
    primary: Optional[str],
    allow_open_route: bool,
    allocator: artifact_identity.IdAllocator,
    now: Optional[float],
    abandon_reason: Optional[str] = None,
) -> Dict[str, Any]:
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise ProducerError("root-identity-missing")
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is None:
        raise ProducerError("campaign-unknown", record["campaign_id"])
    man_id = allocator.allocate("manifest")
    mrev_id = allocator.allocate("manifest_revision")
    when = _rfc3339(now)
    primary_rel = _choose_primary(rows, primary)

    def provenance(digest: str) -> Dict[str, Any]:
        return {
            "source_manifest_id": man_id, "source_revision_id": mrev_id,
            "producer_route_id": route["route_id"], "algorithm_version": ALGORITHM_VERSION,
            "schema_version": 1, "source_digest": digest,
        }

    artifacts: List[Dict[str, Any]] = []
    revisions: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for rel, data in rows:
        art_id = allocator.allocate("artifact")
        arev_id = allocator.allocate("artifact_revision")
        digest = _digest(data)
        inner = rel[len("artifacts/"):]
        artifacts.append({
            "artifact_id": art_id, "cycle_id": record["cycle_id"],
            "role": "primary" if rel == primary_rel else "output",
            "type": _bucket_type(inner), "capability": record["capability"], "title": inner,
        })
        revisions.append({
            "artifact_revision_id": arev_id, "artifact_id": art_id, "revision_sequence": 1,
            "content_digest": digest, "byte_size": len(data), "media_type": _media_type(rel),
            "locator": {"kind": "cycle-relative", "path": rel}, "provenance": provenance(digest),
        })
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "artifact.revision.recorded", "target_id": art_id,
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(digest), "evidence_ids": [], "payload": {"locator": rel},
        })
    cycle_digest = _digest(_canonical([[rel, _digest(data)] for rel, data in rows]))
    routes_row = {
        "artifact_root_id": identity.artifact_root_id, "route_id": route["route_id"],
        "route_hash": route["route_hash"], "terminal_marker": "pending",
        "terminal_evidence_id": "",
    }
    closed = route_is_closed(root, route)
    if not closed and not allow_open_route:
        raise ProducerError("route-not-closed", route["route_id"])
    # D-6: a `completed` cycle must bind a route.terminal.recorded event, which
    # only exists once the route is closed.  Sealing an open route therefore
    # records a provisional `active` cycle (lineage committed, completion not
    # claimed); `abandoned` needs no terminal evidence.
    if state == "abandoned":
        cycle_state = "abandoned"
    elif closed:
        cycle_state = "completed"
    else:
        cycle_state = "active"
    if cycle_state != "active":
        payload = {"abandon_reason": abandon_reason} if cycle_state == "abandoned" else {}
        events.append({
            "event_id": allocator.allocate("event"), "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": f"cycle.{cycle_state}", "target_id": record["cycle_id"],
            "actor": {"kind": "producer", "id": record["producer_id"]}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": payload,
        })
    if closed and cycle_state == "completed":
        terminal_event_id = allocator.allocate("event")
        events.append({
            "event_id": terminal_event_id, "stream_id": allocator.allocate("stream"),
            "stream_sequence": 1, "event_type": "route.terminal.recorded", "target_id": record["cycle_id"],
            "actor": {"kind": "system", "id": "capability-route"}, "recorded_at": when,
            "provenance": provenance(cycle_digest), "evidence_ids": [], "payload": {},
        })
        routes_row["terminal_evidence_id"] = terminal_event_id
    document = {
        "schema_version": 2, "manifest_kind": "artifact.cycle",
        "manifest_id": man_id, "manifest_revision_id": mrev_id,
        "repository_id": identity.repository_id, "artifact_root_id": identity.artifact_root_id,
        "campaign": {
            "campaign_id": campaign["campaign_id"], "goal": str(campaign.get("goal", "")),
            "completion_criterion": {"statement": str((campaign.get("completion_criterion") or {}).get("statement", ""))},
            "title": str(campaign.get("title", "")), "state": str(campaign.get("state", "active")),
        },
        "cycle": {
            "cycle_id": record["cycle_id"], "campaign_id": campaign["campaign_id"],
            "parent_cycle_id": record.get("parent_cycle_id"),
            "started_on": record["started_on"], "input_digest": _digest(_canonical({
                "route_id": route["route_id"], "route_hash": route["route_hash"],
                "capability": record["capability"], "intensity": record["intensity"],
            })),
            "outcome_criterion": {"required_artifact_roles": ["primary"] if rows else [], "decision_required": False},
            "state": cycle_state,
        },
        "artifacts": artifacts, "artifact_revisions": revisions,
        "shared_references": [], "shared_reference_revisions": [],
        "routes": [routes_row], "events": events,
        "producer": {
            "producer_id": record["producer_id"], "contract_version": artifact_manifest.CONTRACT_VERSION,
            "source_revision": f"{record['capability']}/{record['intensity']}/{ALGORITHM_VERSION}",
        },
    }
    if closed and cycle_state == "completed":
        binding, sealed_route = artifact_lifecycle.bind_existing_runtime_route(
            root, Path(record["route_file"]), expected_root_id=identity.artifact_root_id
        )
        if sealed_route.get("route_hash") != route["route_hash"]:
            raise ProducerError("route-hash-drift", route["route_id"])
        try:
            document = artifact_lifecycle._derive_terminal_evidence(document, binding, sealed_route)
        except artifact_lifecycle.LifecycleError as exc:
            raise ProducerError(exc.code, exc.detail)
    return document


def _remove_empty_cycle(root: Path, record: Mapping[str, Any]) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"])
    artifacts = directory / "artifacts"
    for path in (artifacts, directory):
        try:
            path.rmdir()
        except OSError as exc:
            raise ProducerError("cycle-dir-not-empty", str(path)) from exc
    campaign = read_campaign(root, record["campaign_id"])
    if campaign is not None:
        campaign["cycles"] = [c for c in campaign.get("cycles", []) if c != record["cycle_id"]]
        if not campaign["cycles"] and campaign.get("key", "").endswith(record["route_id"]):
            # Campaign created by this begin and never populated: drop it.
            try:
                _campaign_path(root, campaign["campaign_id"]).unlink()
                (campaign_dir(root, campaign["campaign_id"]) / "cycles").rmdir()
                campaign_dir(root, campaign["campaign_id"]).rmdir()
            except OSError:
                _write_campaign(root, campaign, exclusive=False)
        else:
            _write_campaign(root, campaign, exclusive=False)


def _review_lease_dir(root: Path, cycle_id: str) -> Path:
    return Path(root) / PRODUCER_REL / REVIEW_LEASE_REL / cycle_id


def _review_lease_path(root: Path, cycle_id: str, attempt_id: str) -> Path:
    return _review_lease_dir(root, cycle_id) / f"{attempt_id}.json"


def _rfc3339_to_epoch(value: str) -> float:
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone


def _lease_record_is_live(record: Optional[Dict[str, Any]], *, now: Optional[float] = None) -> bool:
    """SD-105/SD-90 evidence hierarchy, cited not redefined (plan.md §6.2):
    exact PID/start/PGID identity, a finite deadline, and judgment-impossible
    inputs (corrupt record, unreadable /proc, clock anomaly, missing field)
    read as *live* -- conservative, so an undecidable lease never lets an
    abandon through (E47-4)."""

    if record is None:
        return False
    if record.get("released_at") is not None:
        return False
    deadline = record.get("deadline")
    if not isinstance(deadline, str):
        return True
    try:
        deadline_ts = _rfc3339_to_epoch(deadline)
    except (ValueError, OverflowError):
        return True
    when = time.time() if now is None else now
    if when > deadline_ts:
        return False
    pid = record.get("pid")
    pid_start = record.get("pid_start")
    pgid = record.get("pgid")
    if not isinstance(pid, int) or not isinstance(pid_start, str) or not pid_start or not isinstance(pgid, int):
        return True
    actual_start = process_start_ticks(pid)
    if actual_start is None:
        return False
    if actual_start != pid_start:
        return False
    try:
        actual_pgid = os.getpgid(pid)
    except OSError:
        return False
    return actual_pgid == pgid


def _live_review_lease(root: Path, cycle_id: str) -> Optional[Path]:
    lease_dir = _review_lease_dir(root, cycle_id)
    if not lease_dir.is_dir():
        return None
    for path in sorted(lease_dir.glob("*.json")):
        record = _read_json(path)
        if record is None:
            # The glob already proved this file exists, so an unparseable
            # read here is corruption, not absence -- conservative live
            # (E47-4), unlike `_lease_record_is_live(None)` below which
            # means "no lease file at this specific path".
            return path
        if _lease_record_is_live(record):
            return path
    return None


def review_lease_acquire(
    root: Path, *, cycle_id: str, attempt_id: str, deadline_seconds: float = 900.0,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        existing = _read_json(path)
        when = time.time() if now is None else now
        # E47-9: the same (cycle, attempt) re-acquiring its own still-live
        # lease is an idempotent no-op -- zero state change.
        if existing is not None and _lease_record_is_live(existing, now=when):
            return {"status": "already-held", "cycle_id": cycle_id, "attempt_id": attempt_id}
        pid = os.getpid()
        record = {
            "schema_version": 1, "cycle_id": cycle_id, "attempt_id": attempt_id,
            "pid": pid, "pid_start": process_start_ticks(pid) or "",
            "pgid": os.getpgrp(), "acquired_at": _rfc3339(when),
            "deadline": _rfc3339(when + max(1.0, deadline_seconds)),
            "released_at": None,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(path, _json_bytes(record), 0o600)
        return {"status": "acquired", "cycle_id": cycle_id, "attempt_id": attempt_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_release(
    root: Path, *, cycle_id: str, attempt_id: str, now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        path = _review_lease_path(root, cycle_id, attempt_id)
        existing = _read_json(path)
        if existing is None or existing.get("released_at") is not None:
            # E47-9: releasing an already-released (or never-acquired) lease
            # is an idempotent no-op.
            return {"status": "already-released", "cycle_id": cycle_id, "attempt_id": attempt_id}
        existing["released_at"] = _rfc3339(time.time() if now is None else now)
        _write_atomic(path, _json_bytes(existing), 0o600)
        return {"status": "released", "cycle_id": cycle_id, "attempt_id": attempt_id}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def review_lease_status(root: Path, *, cycle_id: str, attempt_id: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    if attempt_id is not None:
        record = _read_json(_review_lease_path(root, cycle_id, attempt_id))
        return {"cycle_id": cycle_id, "attempt_id": attempt_id, "live": _lease_record_is_live(record)}
    live_path = _live_review_lease(root, cycle_id)
    return {"cycle_id": cycle_id, "live": live_path is not None}


def finalize(
    root: Path,
    *,
    cycle_id: str,
    state: str = "completed",
    primary: Optional[str] = None,
    publication: str = "not-offered",
    allow_open_route: bool = False,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
    crash_after_manifest: bool = False,
    exclude_hidden: bool = False,
    adopt_root_outputs: Sequence[str] = (),
    abandon_reason: Optional[str] = None,
    force_abandon_ignoring_lease: bool = False,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if state not in {"completed", "abandoned"}:
        raise ProducerError("finalize-state-invalid", state)
    alloc = allocator or artifact_identity.IdAllocator()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        _recover_locked(root)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") == "sealed":
            return {"status": "already-sealed", "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest")}
        if record.get("state") != "open":
            raise ProducerError("cycle-not-open", record.get("state", "?"))
        if state == "abandoned":
            # SD-117 L1 before L3 (plan-check C-2): live-lease enforcement
            # comes first -- a live registered review lease refuses the
            # abandon outright, zero events, zero record-state change
            # (E47-2), before the abandon_reason vocabulary is even
            # consulted.
            if not force_abandon_ignoring_lease:
                if _live_review_lease(root, cycle_id) is not None:
                    raise ProducerError("cycle-abandon-blocked-live-review", cycle_id)
            elif abandon_reason not in (None, "operator-override-live-review"):
                raise ProducerError("abandon-reason-required", str(abandon_reason))
            if force_abandon_ignoring_lease:
                abandon_reason = "operator-override-live-review"
            if abandon_reason not in ABANDON_REASONS:
                raise ProducerError("abandon-reason-required", str(abandon_reason))
        directory = cycle_dir(root, record["campaign_id"], cycle_id)
        route = load_route(root, Path(record["route_file"]))
        if route["route_hash"] != record["route_hash"]:
            raise ProducerError("route-hash-drift", cycle_id)
        adopted_root_outputs: List[str] = []
        if adopt_root_outputs and state != "abandoned":
            raise ProducerError("root-output-adoption-requires-abandoned")
        adoption_moves: List[Tuple[str, Path, Path]] = []
        seen_adoptions: set[str] = set()
        for name in adopt_root_outputs:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ProducerError("root-output-adoption-name-invalid", name)
            if name in seen_adoptions:
                raise ProducerError("root-output-adoption-duplicate", name)
            seen_adoptions.add(name)
            source = directory / name
            target = directory / "artifacts" / name
            if source.is_symlink() or not source.is_file():
                raise ProducerError("root-output-adoption-source-invalid", name)
            if target.exists() or target.is_symlink():
                raise ProducerError("root-output-adoption-target-exists", name)
            adoption_moves.append((name, source, target))
        for name, source, target in adoption_moves:
            os.replace(source, target)
            adopted_root_outputs.append(name)
        excluded_hidden: List[str] = []
        rows, violations = _enumerate_output(directory, exclude_hidden=exclude_hidden, excluded=excluded_hidden)
        if violations:
            raise ProducerError("output-invalid", ";".join(violations))
        if not rows:
            # D-9: no durable output, no lineage. Unchanged except that an
            # abandoned empty cycle also carries its sealed abandon_reason
            # (E47-5: `_remove_empty_cycle` call and returned `status` stay
            # byte-identical either way).
            _remove_empty_cycle(root, record)
            record = dict(record)
            record["state"] = "abandoned" if state == "abandoned" else "no-lineage"
            record["sealed_on"] = _rfc3339(now)
            if state == "abandoned":
                record["abandon_reason"] = abandon_reason
            _write_cycle_record(root, record, exclusive=False)
            return {"status": "no-lineage", "cycle_id": cycle_id, "lineage_committed": False}
        document = build_manifest(
            root, record, route, rows, state=state, primary=primary,
            allow_open_route=allow_open_route, allocator=alloc, now=now,
            abandon_reason=abandon_reason,
        )
        report = artifact_manifest.validate(document)
        if not report.ok:
            raise ProducerError("manifest-invalid", ";".join(v.code for v in report.violations))
        digest = artifact_manifest.manifest_digest(document)
        identity = artifact_lifecycle.read_root_identity(root)
        index = artifact_admission.load_index(root)
        index_report = artifact_index.check(
            index, document, idempotency_key=cycle_id, manifest_digest=digest,
            repository_id=identity.repository_id if identity else None,
        )
        if not index_report.ok:
            raise ProducerError("index-rejected", ";".join(v.code for v in index_report.violations))
        if state == "completed" and route_is_closed(root, route):
            completion = artifact_lifecycle.evaluate_cycle_completion(
                document, content_root=directory, route_file=Path(record["route_file"]),
                publication=publication, expected_root_id=identity.artifact_root_id if identity else None,
            )
            if not completion.ok:
                raise ProducerError(
                    "completion-rejected",
                    ";".join(f"{v.code}:{v.detail}" for v in completion.reasons),
                )
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise ProducerError("manifest-already-present", str(manifest_path))
        cycle_path = os.path.relpath(str(directory), str(root))
        _write_journal(root, cycle_id, state="sealing", manifest_digest=digest, cycle_path=cycle_path)
        # COMMIT POINT: exclusive manifest creation.
        _write_exclusive(manifest_path, artifact_manifest.canonical_bytes(document))
        if crash_after_manifest:  # test hook: simulate a crash after the commit point
            raise artifact_admission.AdmissionRecoveryRequired("simulated crash after manifest publish")
        try:
            _write_journal(root, cycle_id, state="published", manifest_digest=digest, cycle_path=cycle_path)
            _commit_sealed(root, record, document, digest, now=now)
        except BaseException as exc:
            raise artifact_admission.AdmissionRecoveryRequired(
                f"cycle {cycle_id} manifest published but post-publish update failed; run recover"
            ) from exc
        return {"excluded_hidden": excluded_hidden, "adopted_root_outputs": adopted_root_outputs,
            "status": "sealed", "cycle_id": cycle_id, "campaign_id": record["campaign_id"],
            "manifest_digest": digest, "manifest_path": str(manifest_path),
            "artifact_count": len(rows), "lineage_committed": True, "cycle_state": document["cycle"]["state"],
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _commit_sealed(
    root: Path, record: Mapping[str, Any], document: Mapping[str, Any], digest: str, *, now: Optional[float]
) -> None:
    directory = cycle_dir(root, record["campaign_id"], record["cycle_id"])
    index = artifact_admission.load_index(root)
    if record["cycle_id"] not in index.manifests:
        index = artifact_index.apply(
            index, document, cycle_path=os.path.relpath(str(directory), str(root)),
            manifest_digest=digest, idempotency_key=record["cycle_id"],
        )
        artifact_admission._write_index(root, index)
    sealed = dict(record)
    sealed["state"] = "sealed"
    sealed["sealed_on"] = _rfc3339(now)
    sealed["manifest_digest"] = digest
    sealed["cycle_state"] = document["cycle"]["state"]
    _write_cycle_record(root, sealed, exclusive=False)
    _write_journal(root, record["cycle_id"], state="committed", manifest_digest=digest,
                   cycle_path=os.path.relpath(str(directory), str(root)))
    _remove_journal(root, record["cycle_id"])


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def _recover_locked(root: Path, *, now: Optional[float] = None) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {"rolled_forward": [], "rolled_back": [], "dropped": [], "open": []}
    journal_dir = producer_dir(root) / "journal"
    if journal_dir.is_dir():
        for entry in sorted(journal_dir.glob("*.json")):
            journal = _read_json(entry)
            if journal is None:
                entry.unlink()
                continue
            cycle_id = journal.get("cycle_id", entry.stem)
            record = read_cycle_record(root, cycle_id)
            if record is None:
                entry.unlink()
                result["dropped"].append(cycle_id)
                continue
            manifest_path = root / str(journal.get("cycle_path", "")) / "manifest.json"
            document = _read_json(manifest_path)
            if document is not None and artifact_manifest.manifest_digest(document) == journal.get("manifest_digest"):
                _commit_sealed(root, record, document, journal["manifest_digest"], now=now)
                result["rolled_forward"].append(cycle_id)
            elif document is None:
                # Crash before the commit point: cycle stays open.
                entry.unlink()
                result["rolled_back"].append(cycle_id)
            else:
                raise artifact_admission.AdmissionRecoveryRequired(
                    f"manifest digest mismatch for {cycle_id}; manual inspection required"
                )
    for record in list_cycle_records(root):
        if record.get("state") != "open":
            continue
        directory = cycle_dir(root, record["campaign_id"], record["cycle_id"])
        if not directory.is_dir():
            dropped = dict(record)
            dropped["state"] = "dropped"
            dropped["sealed_on"] = _rfc3339(now)
            _write_cycle_record(root, dropped, exclusive=False)
            result["dropped"].append(record["cycle_id"])
            continue
        if (directory / "manifest.json").is_file():
            document = _read_json(directory / "manifest.json")
            if document is not None:
                _commit_sealed(root, record, document, artifact_manifest.manifest_digest(document), now=now)
                result["rolled_forward"].append(record["cycle_id"])
                continue
        result["open"].append(record["cycle_id"])
    shared_dir = producer_dir(root) / "shared-journal"
    if shared_dir.is_dir():
        for entry in sorted(shared_dir.glob("*.json")):
            journal = _read_json(entry)
            if journal is None:
                entry.unlink()
                continue
            staging = root / str(journal.get("staging", ""))
            target = root / str(journal.get("target", ""))
            if journal.get("state") == "staging" and staging.is_dir():
                shutil.rmtree(str(staging))
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
            elif target.is_dir():
                _commit_shared(root, journal)
                result["rolled_forward"].append(journal.get("revision_id", entry.stem))
            else:
                entry.unlink()
                result["rolled_back"].append(journal.get("revision_id", entry.stem))
    return result


def recover(root: Path, *, now: Optional[float] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        step1 = artifact_admission._recover_locked(root, now=now)
        producer = _recover_locked(root, now=now)
        return {"status": "recovered", "admission": step1, "producer": producer}
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# shared admission
# ---------------------------------------------------------------------------


def _reference_path(root: Path, kind: str, ref_id: str) -> Path:
    return Path(root) / "shared" / kind / ref_id / "reference.json"


def find_reference_by_key(root: Path, kind: str, key: str) -> Optional[Dict[str, Any]]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        record = _read_json(entry / "reference.json")
        if record and record.get("key") == key:
            return record
    return None


def _commit_shared(root: Path, journal: Mapping[str, Any]) -> None:
    kind = journal["kind"]
    ref_id = journal["reference_id"]
    reference = _read_json(_reference_path(root, kind, ref_id))
    if reference is None:
        reference = {
            "schema_version": 1, "contract": CONTRACT, "shared_reference_id": ref_id,
            "kind": SHARED_KINDS[kind], "key": journal.get("key"), "title": journal.get("title"),
            "created_on": journal.get("created_on"), "latest_revision_id": None, "revisions": [],
        }
    if journal["revision_id"] not in reference["revisions"]:
        reference["revisions"] = list(reference["revisions"]) + [journal["revision_id"]]
    reference["latest_revision_id"] = journal["revision_id"]
    reference["updated_on"] = journal.get("created_on")
    path = _reference_path(root, kind, ref_id)
    _ensure_dir(path.parent)
    _write_atomic(path, _json_bytes(reference))
    try:
        shared_journal_path(root, journal["revision_id"]).unlink()
    except FileNotFoundError:
        pass


def admit_shared(
    root: Path,
    *,
    cycle_id: str,
    kind: str,
    source: str,
    reference_id: Optional[str] = None,
    key: Optional[str] = None,
    title: Optional[str] = None,
    promote_research: bool = False,
    promotion_evidence: Optional[str] = None,
    allocator: Optional[artifact_identity.IdAllocator] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    if kind not in SHARED_KINDS:
        raise ProducerError("shared-kind-not-admissible", kind)
    if kind == "research":
        if not promote_research:
            raise ProducerError("research-promotion-required",
                                "research is admitted to shared/ only with an explicit promotion")
        if not promotion_evidence:
            raise ProducerError("research-promotion-evidence-required")
    if reference_id and not artifact_identity.is_well_formed(reference_id, "shared_reference"):
        raise ProducerError("reference-id-malformed", reference_id)
    if key and not _KEY_RE.match(key):
        raise ProducerError("reference-key-invalid", key)
    alloc = allocator or artifact_identity.IdAllocator()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT, now=now)
    try:
        _recover_locked(root)
        record = read_cycle_record(root, cycle_id)
        if record is None:
            raise ProducerError("cycle-unknown", cycle_id)
        if record.get("state") != "sealed":
            raise ProducerError("cycle-not-sealed", record.get("state", "?"))
        directory = cycle_dir(root, record["campaign_id"], cycle_id)
        source_rel = source if source.startswith("artifacts/") else "artifacts/" + source
        if ".." in source_rel.split("/"):
            raise ProducerError("source-unsafe", source)
        source_path = directory / source_rel
        if os.path.islink(str(source_path)) or not source_path.exists():
            raise ProducerError("source-missing", source_rel)
        evidence_rel: Optional[str] = None
        evidence_digest: Optional[str] = None
        if kind == "research":
            assert promotion_evidence is not None
            evidence_rel = promotion_evidence if promotion_evidence.startswith("artifacts/") else "artifacts/" + promotion_evidence
            evidence_path = directory / evidence_rel
            if os.path.islink(str(evidence_path)) or not evidence_path.is_file():
                raise ProducerError("research-promotion-evidence-missing", evidence_rel)
            evidence_digest = _digest(evidence_path.read_bytes())
        reference: Optional[Dict[str, Any]] = None
        if reference_id:
            reference = _read_json(_reference_path(root, kind, reference_id))
            if reference is None:
                raise ProducerError("reference-unknown", reference_id)
        elif key:
            reference = find_reference_by_key(root, kind, key)
        if reference is None:
            reference_id = alloc.allocate("shared_reference")
            created = True
        else:
            reference_id = reference["shared_reference_id"]
            created = False
        revision_id = alloc.allocate("shared_reference_revision")
        revisions_dir = Path(root) / "shared" / kind / reference_id / "revisions"
        _ensure_dir(revisions_dir)
        target = revisions_dir / revision_id
        if target.exists():
            raise ProducerError("revision-exists", str(target))
        staging = revisions_dir / f".admitting-{os.urandom(8).hex()}"
        journal = {
            "schema_version": 1, "state": "staging", "kind": kind, "reference_id": reference_id,
            "revision_id": revision_id, "key": key or (reference or {}).get("key"),
            "title": title or (reference or {}).get("title") or source_rel,
            "created_on": _rfc3339(now), "staging": os.path.relpath(str(staging), str(root)),
            "target": os.path.relpath(str(target), str(root)), "cycle_id": cycle_id,
        }
        _ensure_dir(shared_journal_path(root, revision_id).parent)
        _write_exclusive(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        os.makedirs(str(staging))
        try:
            rows, violations = _copy_tree_files(source_path, staging)
            if violations:
                raise ProducerError("source-invalid", ";".join(violations))
            if not rows:
                raise ProducerError("source-empty", source_rel)
            content_digest = _digest(_canonical([[rel, digest, size] for rel, digest, size in rows]))
            sequence = len((reference or {}).get("revisions", [])) + 1
            revision = {
                "schema_version": 1, "contract": CONTRACT,
                "shared_reference_revision_id": revision_id, "shared_reference_id": reference_id,
                "kind": SHARED_KINDS[kind], "sequence": sequence, "content_digest": content_digest,
                "file_count": len(rows), "byte_size": sum(size for _, _, size in rows),
                "created_on": journal["created_on"],
                "source": {
                    "campaign_id": record["campaign_id"], "cycle_id": cycle_id,
                    "manifest_digest": record.get("manifest_digest"), "path": source_rel,
                    "capability": record.get("capability"), "route_id": record.get("route_id"),
                },
                "promotion": (
                    {"kind": "explicit", "evidence": evidence_rel, "evidence_digest": evidence_digest}
                    if kind == "research" else {"kind": "canonical-shared-kind"}
                ),
                "files": [{"path": rel, "sha256": digest, "byte_size": size} for rel, digest, size in rows],
            }
            _write_exclusive(staging / "revision.json", _json_bytes(revision))
            _fsync_dir(staging)
        except BaseException:
            shutil.rmtree(str(staging), ignore_errors=True)
            try:
                shared_journal_path(root, revision_id).unlink()
            except FileNotFoundError:
                pass
            raise
        if target.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
            raise ProducerError("revision-exists", str(target))
        # COMMIT POINT: no-replace rename of the staged immutable revision.
        os.rename(str(staging), str(target))
        _fsync_dir(revisions_dir)
        journal["state"] = "published"
        _write_atomic(shared_journal_path(root, revision_id), _json_bytes(journal), 0o600)
        _commit_shared(root, journal)
        return {
            "status": "admitted", "kind": SHARED_KINDS[kind], "shared_reference_id": reference_id,
            "shared_reference_revision_id": revision_id, "reference_created": created,
            "revision_dir": str(target), "content_digest": content_digest, "file_count": len(rows),
            "promotion": revision["promotion"],
        }
    finally:
        artifact_admission._release_lock(root, lock_fd)


# ---------------------------------------------------------------------------
# write policy (used by hooks and writers)
# ---------------------------------------------------------------------------


def _relative(root: Path, target: Path) -> Optional[str]:
    root = Path(root).resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Resolve the deepest existing ancestor so a not-yet-created target still
    # normalizes; the leaf is appended unchanged.
    probe = candidate
    tail: List[str] = []
    while not probe.exists() and probe.parent != probe:
        tail.insert(0, probe.name)
        probe = probe.parent
    resolved = probe.resolve()
    for part in tail:
        resolved = resolved / part
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def check_write(root: Path, target: Path) -> Dict[str, Any]:
    """Classify one prospective write under the artifact root.

    Returns {verdict: allow|deny, reason, layout, cutover, bucket, cycle_id}.
    """
    root = Path(root).resolve()
    rel = _relative(root, Path(target))
    active = is_active(root)
    base = {"cutover": "active" if active else "inactive", "target": str(target)}
    if rel is None:
        return {**base, "verdict": "allow", "reason": "outside-artifact-root", "layout": None}
    parts = rel.split("/")
    top = parts[0]
    if top.startswith(".") or top == "_scratch":
        return {**base, "verdict": "allow", "reason": "runtime-owned", "layout": "runtime"}
    if top == "shared":
        return {**base, "verdict": "deny", "reason": "shared-revision-immutable", "layout": "shared"}
    if top == "campaigns":
        if len(parts) < 5 or parts[2] != "cycles":
            return {**base, "verdict": "deny", "reason": "campaign-record-machine-managed", "layout": "cycle"}
        campaign_id, cycle_id = parts[1], parts[3]
        if len(parts) < 6 or parts[4] != "artifacts":
            return {**base, "verdict": "deny", "reason": "outside-cycle-artifacts", "layout": "cycle",
                    "cycle_id": cycle_id}
        record = read_cycle_record(root, cycle_id)
        sealed_on_disk = (cycle_dir(root, campaign_id, cycle_id) / "manifest.json").exists()
        if record is None:
            reason = "cycle-sealed" if sealed_on_disk else "cycle-unknown"
            return {**base, "verdict": "deny", "reason": reason, "layout": "cycle", "cycle_id": cycle_id}
        if record.get("state") != "open" or record.get("campaign_id") != campaign_id or sealed_on_disk:
            return {**base, "verdict": "deny", "reason": "cycle-not-open", "layout": "cycle", "cycle_id": cycle_id}
        bucket = parts[5] if len(parts) > 6 else None
        return {**base, "verdict": "allow", "reason": "open-cycle-artifacts", "layout": "cycle",
                "cycle_id": cycle_id, "campaign_id": campaign_id, "bucket": bucket}
    if active:
        return {**base, "verdict": "deny", "reason": "legacy-top-level-write-denied", "layout": "legacy",
                "bucket": top}
    return {**base, "verdict": "allow", "reason": "legacy-compat-window", "layout": "legacy", "bucket": top}


def cycle_bucket(root: Path, target: Path) -> Optional[Tuple[str, str]]:
    """Return (bucket, cycle_id) for a path inside a cycle's artifacts."""
    rel = _relative(Path(root), Path(target))
    if rel is None:
        return None
    parts = rel.split("/")
    if len(parts) >= 7 and parts[0] == "campaigns" and parts[2] == "cycles" and parts[4] == "artifacts":
        return parts[5], parts[3]
    return None


def resolve_output_dir(root: Path, bucket: str, *, cycle_dir_hint: Optional[str] = None) -> Tuple[Path, str]:
    """Where a writer must place `<bucket>/...` output: cycle layout or legacy."""
    root = Path(root).resolve()
    hint = cycle_dir_hint or os.environ.get("AGENT_ARTIFACT_CYCLE_DIR")
    if hint:
        directory = Path(hint)
        verdict = check_write(root, directory / "artifacts" / bucket / "probe")
        if verdict["verdict"] != "allow":
            raise ProducerError(verdict["reason"], str(directory))
        return directory / "artifacts" / bucket, "cycle"
    if is_active(root):
        raise ProducerError("legacy-top-level-write-denied",
                            f"{bucket}: begin a cycle first (AGENT_ARTIFACT_CYCLE_DIR unset)")
    return root / bucket, "legacy"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print(payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("activate")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--repository-id", required=True)
    p.add_argument("--artifact-root-id", required=True)
    p.add_argument("--w7-campaign-id")
    p.add_argument("--w7-cycle-id")
    p.add_argument("--w7-handoff-sha256")
    p.add_argument("--w7-map-sha256")
    p.add_argument("--w7-shared", action="append", default=[], help="kind=ref_id:rrev_id")
    p.add_argument("--approval-receipt-sha256")

    p = sub.add_parser("status")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("begin")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--route", required=True)
    p.add_argument("--node", default=None)
    p.add_argument("--capability", required=True)
    p.add_argument("--intensity", required=True)
    p.add_argument("--campaign")
    p.add_argument("--campaign-key")
    p.add_argument("--title")
    p.add_argument("--goal")
    p.add_argument("--parent-cycle")
    p.add_argument("--require-cycle", action="store_true")
    p.add_argument("--env-file", help="write KEY=VALUE lines for the producer environment")

    p = sub.add_parser("finalize")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--state", default="completed", choices=["completed", "abandoned"])
    p.add_argument("--primary")
    p.add_argument("--publication", default="not-offered")
    p.add_argument("--allow-open-route", action="store_true")
    p.add_argument("--adopt-root-output", action="append", default=[])
    p.add_argument("--abandon-reason", choices=sorted(ABANDON_REASONS))
    p.add_argument("--force-abandon-ignoring-lease", action="store_true")

    p = sub.add_parser("review-lease")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--attempt")
    p.add_argument("--deadline-seconds", type=float, default=900.0)
    p.add_argument("operation", choices=["acquire", "release", "status"])

    p = sub.add_parser("admit-shared")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--reference")
    p.add_argument("--key")
    p.add_argument("--title")
    p.add_argument("--promote-research", action="store_true")
    p.add_argument("--promotion-evidence")

    p = sub.add_parser("recover")
    p.add_argument("--artifact-root", required=True)

    p = sub.add_parser("check-write")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--file", required=True)

    p = sub.add_parser("resolve-output")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--cycle-dir")

    args = parser.parse_args(argv)
    try:
        root = Path(args.artifact_root)
        if args.command == "activate":
            w7: Dict[str, Any] = {}
            if args.w7_campaign_id:
                w7["campaign_id"] = args.w7_campaign_id
            if args.w7_cycle_id:
                w7["cycle_id"] = args.w7_cycle_id
            if args.w7_handoff_sha256:
                w7["handoff_sha256"] = args.w7_handoff_sha256
            if args.w7_map_sha256:
                w7["compatibility_map_sha256"] = args.w7_map_sha256
            shared: Dict[str, Any] = {}
            for row in args.w7_shared:
                kind, _, ids = row.partition("=")
                ref, _, rrev = ids.partition(":")
                shared[kind] = {"shared_reference_id": ref, "shared_reference_revision_id": rrev}
            if shared:
                w7["shared"] = shared
            result = activate(root, repository_id=args.repository_id, artifact_root_id=args.artifact_root_id,
                              w7=w7, approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "status":
            result = status(root)
        elif args.command == "begin":
            result = begin(root, route_file=Path(args.route), capability=args.capability,
                           intensity=args.intensity, node_id=args.node, campaign_id=args.campaign,
                           campaign_key=args.campaign_key, title=args.title, goal=args.goal,
                           parent_cycle_id=args.parent_cycle, require_cycle=args.require_cycle)
            if args.env_file:
                lines = "".join(f"{k}={v}\n" for k, v in result.get("env", {}).items())
                Path(args.env_file).write_text(lines, encoding="utf-8")
        elif args.command == "finalize":
            result = finalize(root, cycle_id=args.cycle, state=args.state, primary=args.primary,
                              publication=args.publication, allow_open_route=args.allow_open_route,
                              adopt_root_outputs=args.adopt_root_output,
                              abandon_reason=args.abandon_reason,
                              force_abandon_ignoring_lease=args.force_abandon_ignoring_lease)
        elif args.command == "review-lease":
            if args.operation == "acquire":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_acquire(root, cycle_id=args.cycle, attempt_id=args.attempt,
                                              deadline_seconds=args.deadline_seconds)
            elif args.operation == "release":
                if not args.attempt:
                    raise ProducerError("review-lease-attempt-required")
                result = review_lease_release(root, cycle_id=args.cycle, attempt_id=args.attempt)
            else:
                result = review_lease_status(root, cycle_id=args.cycle, attempt_id=args.attempt)
        elif args.command == "admit-shared":
            result = admit_shared(root, cycle_id=args.cycle, kind=args.kind, source=args.source,
                                  reference_id=args.reference, key=args.key, title=args.title,
                                  promote_research=args.promote_research,
                                  promotion_evidence=args.promotion_evidence)
        elif args.command == "recover":
            result = recover(root)
        elif args.command == "check-write":
            result = check_write(root, Path(args.file))
            _print(result)
            return OK if result["verdict"] == "allow" else BLOCKED
        elif args.command == "resolve-output":
            directory, layout = resolve_output_dir(root, args.bucket, cycle_dir_hint=args.cycle_dir)
            result = {"status": "ok", "output_dir": str(directory), "layout": layout}
        else:  # pragma: no cover
            parser.error("unknown command")
            return USAGE
    except ProducerError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except artifact_admission.AdmissionBusy as exc:
        _print({"status": "blocked", "reason": "admission-busy", "detail": str(exc)})
        return BLOCKED
    except artifact_admission.AdmissionRecoveryRequired as exc:
        _print({"status": "blocked", "reason": "recovery-required", "detail": str(exc)})
        return BLOCKED
    except artifact_lifecycle.LifecycleError as exc:
        _print({"status": "blocked", "reason": exc.code, "detail": exc.detail})
        return BLOCKED
    except (OSError, ValueError) as exc:
        _print({"status": "blocked", "reason": "request-invalid", "detail": str(exc)})
        return BLOCKED
    _print(result)
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
