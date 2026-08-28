#!/usr/bin/env python3
"""W7C approval-gate executors: delta migration, compatibility switch, source retirement.

These commands run only against an artifact root whose producer cutover is active
(`artifact_producer.py activate`, gate G1) and every mutation is journaled:

  migrate-delta   G2  copy the census-classified cycle candidates into one open
                      producer cycle (`campaigns/<camp>/cycles/<cyc>/artifacts/`),
                      snapshot the current `spec/` and `analysis_project/` trees
                      as staged shared-kind input, and write a W7-shaped journal,
                      inverse journal and compatibility map.  Sources are never
                      touched.
  migrate-seal    G2  after the route is closed: finalize the cycle, admit the
                      staged `spec`/`analysis` trees as new immutable shared
                      revisions (adopting the W7 references), rewrite the map.
  compat-close    G3  record the closed compatibility window and the map set that
                      legacy readers must consult (`resolve-legacy`).
  resolve-legacy  G3  map a legacy root-relative path to its live target
                      (latest map wins) or list the canonical prd candidates.
  retire          G4  verify every mapped file source against its target digest,
                      back the sources up outside the artifact root, then delete
                      them and prune the emptied legacy directories.  Excluded
                      prefixes and unverified files are always kept.
  route-sweep     W7F classify legacy runtime-route rows, close provably abandoned
                      routes, canonicalize recoverable names, and journal every
                      mutation behind a digest-bound approval.
  closeout-residue
                  W7F migrate the declared legacy-runtime residue into one sealed
                      producer cycle, write a verified external backup, then remove
                      only the approved source inventory.
  approval-package
                  W7F bind byte-identical route and residue dry-runs into the
                      human-gate package.  It is always emitted unauthorized.
  recover-closeout-prebackup
                  W7F restore route aliases after an apply failure that stopped
                      in the prepared phase, before any backup or source retire.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_identity  # noqa: E402
import artifact_producer as P  # noqa: E402
import codex_dispatch_terminal as TERMINAL  # noqa: E402
import dispatch_contract as DISPATCH  # noqa: E402

JOURNAL_SCHEMA = "artifact-relocation-live-journal-row/v1"
MAP_SCHEMA = "artifact-relocation-compatibility-map-row/v1"
CYCLE_BUCKETS = ("plans", "documents", "designs", "research", "experiments")
SHARED_SNAPSHOT = {"spec": "spec", "analysis_project": "analysis"}
CANDIDATE_DISPOSITIONS = (
    "after-cutoff-after_cutoff_arrival", "after-cutoff-after_cutoff_drift", "after-cutoff-after_cutoff_unstable",
    "w6-baseline-legacy", "w7-source-preserved-descendant", "post-w7-arrival",
)
OK, BLOCKED = 0, 65

SEALED_EVIDENCE_PATHS = (
    "plans/2026-08-24_artifact-knowledge-index-w7",
    "plans/2026-08-25_artifact-knowledge-index-w7-e1",
    "plans/2026-08-25_artifact-knowledge-index-w7-e2-e3",
    "plans/2026-08-25_artifact-write-cutover-w7c",
    "spec/artifact-path-contract/_internal/research",
    "research/hermes-agent/.gitignore",
)
RESIDUE_CONTAINERS = (
    "notes", "proposals", "dev_logs", "test_logs", "evidence", "release-config",
    "research-alternative", "spec-research-alternative", "routes", "_routes",
)
PRESERVED_SUPPORT_CONTAINERS = ("reviews", "shards", "_internal")
DECLARED_TOP_LEVEL = frozenset({
    "analysis_project", "research", "spec", "plans", "documents", "experiments", "designs",
    "campaigns", "shared", "_internal", "reviews", "shards", ".runtime", ".core-grounding",
    ".spec-grounding", "routes", "_routes", "notes", "proposals",
    "spec-research-alternative", "research-alternative", ".git", ".agents", ".codex", "_scratch",
})
CORE_SYNC_ROUTE_ID = "rt-f06e4a05a1bb924d"
ROUTE_ID_PATTERN = re.compile(r"^rt-[0-9a-f]{16}$")


class CutoverError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _plan_package(kind: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    digest = "sha256:" + hashlib.sha256(_canonical_bytes(plan)).hexdigest()
    return {
        "schema_version": 1,
        "kind": kind,
        "authorized": False,
        "plan_sha256": digest,
        "plan": plan,
    }


def _emit_package(package: Dict[str, Any], output: Optional[Path]) -> bytes:
    data = _canonical_bytes(package)
    if output is not None:
        P._write_atomic(Path(output), data)
    return data


def _route_module():
    path = Path(__file__).with_name("capability-route.py")
    spec = importlib.util.spec_from_file_location("artifact_cutover_capability_route", path)
    if spec is None or spec.loader is None:
        raise CutoverError("route-module-unavailable", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jobs_digest(paths: Sequence[Path]) -> List[Dict[str, Any]]:
    rows = []
    for raw in sorted({str(Path(p).expanduser().resolve()) for p in paths}):
        path = Path(raw)
        rows.append({
            "path": raw,
            "exists": path.is_file(),
            "sha256": "sha256:" + _sha(path) if path.is_file() else None,
            "size": path.stat().st_size if path.is_file() else 0,
        })
    return rows


def _registry_attempts(paths: Sequence[Path]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for jobs in sorted({Path(p).expanduser().resolve() for p in paths}, key=str):
        if not jobs.is_file():
            continue
        with jobs.open(encoding="utf-8", errors="replace") as handle:
            for ordinal, line in enumerate(handle):
                parts = line.rstrip("\n").split("\t", 5)
                if len(parts) != 6:
                    continue
                timestamp, status, _worktree, _cwd, _label, pipe = parts
                metadata = DISPATCH.parse_registry_metadata(pipe)
                attempt_id = metadata.get("attempt_id") or f"{jobs}:{ordinal}"
                candidate = {
                    "timestamp": timestamp,
                    "status": status,
                    "metadata": metadata,
                    "jobs": str(jobs),
                    "ordinal": ordinal,
                }
                previous = latest.get(attempt_id)
                if previous is None or (timestamp, str(jobs), ordinal) >= (
                    previous["timestamp"], previous["jobs"], previous["ordinal"]
                ):
                    latest[attempt_id] = candidate
    return latest


def _route_liveness(route_id: str, attempts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    observations = []
    for attempt_id, row in attempts.items():
        metadata = row["metadata"]
        identities = {
            metadata.get("route_id", ""),
            metadata.get("owner_route_id", ""),
            metadata.get("batch_route_id", ""),
        }
        if route_id not in identities:
            continue
        try:
            terminal = TERMINAL.terminal_envelope_observed(metadata.get("log_file"))
            observed = DISPATCH.observed_attempt_liveness(
                row["status"], metadata, terminal_envelope=terminal
            )
            state, reason = observed.state, observed.reason
        except Exception as exc:
            state, reason = "unverifiable", type(exc).__name__
        observations.append({
            "attempt_id": attempt_id,
            "status": row["status"],
            "state": state,
            "reason": reason,
        })
    observations.sort(key=lambda row: row["attempt_id"])
    states = {row["state"] for row in observations}
    if "alive" in states:
        state = "alive"
    elif "unverifiable" in states:
        state = "unverifiable"
    else:
        state = "dead"
    return {"state": state, "attempts": observations}


def _valid_route_identity(route: Any, routes) -> bool:
    if not isinstance(route, dict):
        return False
    route_id = str(route.get("route_id", ""))
    route_hash = str(route.get("route_hash", ""))
    return bool(
        ROUTE_ID_PATTERN.fullmatch(route_id)
        and route_hash == routes.route_hash(route)
        and route_id == "rt-" + route_hash.split(":", 1)[-1][:16]
    )


def build_route_sweep_plan(
    root: Path,
    *,
    jobs: Sequence[Path],
    preserve_routes: Sequence[str] = (),
) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    routes = _route_module()
    attempts = _registry_attempts(jobs)
    preserve = set(preserve_routes)
    directory = root / ".runtime" / "routes"
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and not path.is_symlink()),
        key=lambda path: path.name,
    ) if directory.is_dir() else []
    consumed: set[str] = set()
    reserved_targets: set[str] = set()
    actions: List[Dict[str, Any]] = []
    route_population = 0
    for path in files:
        if path.name.endswith(".outcome.json") or not path.name.endswith(".json"):
            continue
        route_population += 1
        rel = os.path.relpath(path, root)
        try:
            route = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            route = None
        valid = _valid_route_identity(route, routes)
        route_id = str(route.get("route_id", "")) if isinstance(route, dict) else ""
        route_artifact_root = str(route.get("artifact_root", "")) if isinstance(route, dict) else ""
        root_match = bool(
            valid
            and route_artifact_root
            and Path(route_artifact_root).resolve(strict=False) == root
        )
        exact = root_match and path.name == f"{route_id}.json"
        outcome = path.with_name(path.stem + ".outcome.json")
        closed = False
        if outcome.is_file() and isinstance(route, dict):
            try:
                closure = json.loads(outcome.read_text(encoding="utf-8"))
                closed = (
                    closure.get("route_id") == route_id
                    and closure.get("route_hash") == route.get("route_hash")
                )
            except (OSError, ValueError, TypeError):
                closed = False
        consumed.add(path.name)
        if outcome.is_file():
            consumed.add(outcome.name)
        liveness = _route_liveness(route_id, attempts) if root_match else {"state": "dead", "attempts": []}
        sources = [rel] + ([os.path.relpath(outcome, root)] if outcome.is_file() else [])
        base = {
            "route_id": route_id or None,
            "route_hash": route.get("route_hash") if isinstance(route, dict) else None,
            "route_artifact_root": route_artifact_root or None,
            "root_match": root_match,
            "source_paths": sources,
            "closed": closed,
            "liveness": liveness,
        }
        if valid and not root_match:
            action = "migrate-residue"
        elif route_id in preserve:
            action = "preserve-explicit"
        elif liveness["state"] == "alive":
            action = "preserve-live"
        elif liveness["state"] == "unverifiable":
            action = "preserve-unverifiable"
        elif exact and closed:
            action = "preserve-closed"
        elif exact:
            action = "close-abandoned"
        elif valid:
            target = directory / f"{route_id}.json"
            target_outcome = directory / f"{route_id}.outcome.json"
            collision = (
                target.exists()
                or target.name in reserved_targets
                or (outcome.exists() and target_outcome.exists())
                or (outcome.exists() and target_outcome.name in reserved_targets)
            )
            if collision:
                action = "migrate-residue"
            else:
                action = "canonicalize"
                reserved_targets.add(target.name)
                reserved_targets.add(target_outcome.name)
                base["target_paths"] = [
                    os.path.relpath(target, root),
                    *([os.path.relpath(target_outcome, root)] if outcome.exists() or not closed else []),
                ]
        else:
            action = "migrate-residue"
        base["action"] = action
        actions.append(base)
    for path in files:
        if path.name in consumed:
            continue
        actions.append({
            "action": "migrate-residue",
            "route_id": None,
            "route_hash": None,
            "route_artifact_root": None,
            "root_match": False,
            "source_paths": [os.path.relpath(path, root)],
            "closed": False,
            "liveness": {"state": "dead", "attempts": []},
        })
    actions.sort(key=lambda row: (row["source_paths"][0], row["action"]))
    counts: Dict[str, int] = {}
    for row in actions:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    residue_paths = sorted({
        rel for row in actions if row["action"] == "migrate-residue"
        for rel in row["source_paths"]
    })
    return {
        "schema_version": 1,
        "artifact_root_id": identity.artifact_root_id,
        "repository_id": identity.repository_id,
        "route_population": route_population,
        "jobs": _jobs_digest(jobs),
        "preserve_routes": sorted(preserve),
        "counts": counts,
        "actions": actions,
        "residue_paths": residue_paths,
    }


def route_sweep_package(root: Path, *, jobs: Sequence[Path], preserve_routes: Sequence[str] = ()) -> Dict[str, Any]:
    return _plan_package(
        "w7f-route-sweep-plan",
        build_route_sweep_plan(root, jobs=jobs, preserve_routes=preserve_routes),
    )


def _is_sealed_evidence(rel: str) -> bool:
    return any(rel == prefix or rel.startswith(prefix + "/") for prefix in SEALED_EVIDENCE_PATHS)


def _collect_tree(root: Path, rel_root: str, files: set[str], directories: set[str],
                  blockers: List[Dict[str, str]]) -> None:
    base = root / rel_root
    if not base.exists() and not base.is_symlink():
        return
    if base.is_symlink():
        blockers.append({"path": rel_root, "reason": "symlink"})
        return
    if base.is_file():
        files.add(rel_root)
        return
    directories.add(rel_root)
    for current, names, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        current_rel = os.path.relpath(current_path, root)
        directories.add(current_rel)
        kept = []
        for name in sorted(names):
            child = current_path / name
            rel = os.path.relpath(child, root)
            if child.is_symlink():
                blockers.append({"path": rel, "reason": "symlink"})
            else:
                kept.append(name)
                directories.add(rel)
        names[:] = kept
        for name in sorted(filenames):
            child = current_path / name
            rel = os.path.relpath(child, root)
            if _is_sealed_evidence(rel):
                continue
            if child.is_symlink():
                blockers.append({"path": rel, "reason": "symlink"})
            elif child.is_file():
                files.add(rel)
            else:
                blockers.append({"path": rel, "reason": "not-regular"})


def _classify_residue(rel: str) -> str:
    top = rel.split("/", 1)[0]
    name = Path(rel).name
    if "/" not in rel and ("route" in name or name.endswith(".outcome.json")):
        return "root-legacy-route"
    if top in {"routes", "_routes"}:
        return "legacy-route-container"
    if top in RESIDUE_CONTAINERS:
        return "wrong-base-container"
    if rel.startswith(".runtime/routes/"):
        return "runtime-route-residue"
    return "root-loose-file"


def _core_sync_observation(root: Path, core_file: Path, sync_route_id: str,
                           planned_top_level_removals: set[str]) -> Dict[str, Any]:
    core = Path(core_file).resolve()
    text = core.read_text(encoding="utf-8") if core.is_file() else ""
    exact_rows = {
        path: (f"`{path}/`" in text if path != "research/hermes-agent/.gitignore"
               else f"`{path}`" in text)
        for path in SEALED_EVIDENCE_PATHS
    }
    class_declared = "C-LEG(sealed-evidence)" in text and all(exact_rows.values())
    outcome_path = root / ".runtime" / "routes" / f"{sync_route_id}.outcome.json"
    outcome_valid = False
    outcome_digest = None
    if outcome_path.is_file() and not outcome_path.is_symlink():
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            outcome_valid = (
                outcome.get("route_id") == sync_route_id
                and outcome.get("terminal_gate_proven") is True
            )
            outcome_digest = "sha256:" + _sha(outcome_path)
        except (OSError, ValueError, TypeError):
            pass
    undeclared = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        name = path.name
        if path.is_file() or name in planned_top_level_removals:
            continue
        if name in DECLARED_TOP_LEVEL or name in PRESERVED_SUPPORT_CONTAINERS or name.startswith(".probe-"):
            continue
        undeclared.append(name)
    return {
        "core_file": str(core),
        "core_sha256": "sha256:" + _sha(core) if core.is_file() else None,
        "sealed_evidence_rows": exact_rows,
        "sealed_evidence_class_declared": class_declared,
        "sync_route_id": sync_route_id,
        "sync_outcome": os.path.relpath(outcome_path, root),
        "sync_outcome_sha256": outcome_digest,
        "terminal_proven": outcome_valid,
        "undeclared_top_level": undeclared,
        "proven": class_declared and outcome_valid and not undeclared,
    }


def build_closeout_residue_plan(
    root: Path,
    *,
    jobs: Sequence[Path],
    residue_route: Path,
    backup_root: Path,
    core_file: Path,
    preserve_routes: Sequence[str] = (),
    self_cycles: Sequence[str] = (),
    sync_route_id: str = CORE_SYNC_ROUTE_ID,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    route_package = route_sweep_package(root, jobs=jobs, preserve_routes=preserve_routes)
    route_plan = route_package["plan"]
    files: set[str] = set()
    directories: set[str] = set()
    blockers: List[Dict[str, str]] = []
    for name in RESIDUE_CONTAINERS:
        _collect_tree(root, name, files, directories, blockers)
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name == ".pipeline-lock":
            continue
        if path.is_symlink():
            if path.name not in DECLARED_TOP_LEVEL:
                blockers.append({"path": path.name, "reason": "top-level-symlink"})
        elif path.is_file():
            files.add(path.name)
    for rel in route_plan["residue_paths"]:
        path = root / rel
        if path.is_symlink():
            blockers.append({"path": rel, "reason": "symlink"})
        elif path.is_file():
            files.add(rel)
        elif path.exists():
            blockers.append({"path": rel, "reason": "not-regular"})
    inventory = []
    for rel in sorted(files):
        if _is_sealed_evidence(rel):
            blockers.append({"path": rel, "reason": "sealed-evidence-selected"})
            continue
        path = root / rel
        if not path.is_file() or path.is_symlink():
            blockers.append({"path": rel, "reason": "source-not-regular"})
            continue
        st = path.stat()
        inventory.append({
            "path": rel,
            "classification": _classify_residue(rel),
            "sha256": "sha256:" + _sha(path),
            "size": st.st_size,
            "mode": stat.S_IMODE(st.st_mode),
        })
    planned_tops = {rel.split("/", 1)[0] for rel in files} | {
        name for name in RESIDUE_CONTAINERS if (root / name).exists()
    }
    core_sync = _core_sync_observation(root, core_file, sync_route_id, planned_tops)
    status = P.status(root)
    self_set = set(self_cycles)
    open_cycles = sorted(status.get("open_cycles", []))
    explicit_routes = set(preserve_routes)
    liveness_by_route: Dict[str, set[str]] = {}
    for row in route_plan["actions"]:
        route_id = row.get("route_id")
        if route_id:
            liveness_by_route.setdefault(route_id, set()).add(row.get("liveness", {}).get("state", "dead"))
    preserved_live_cycles = []
    external_cycles = []
    for cycle in open_cycles:
        if cycle in self_set:
            continue
        record = P.read_cycle_record(root, cycle)
        route_id = record.get("route_id") if isinstance(record, dict) else None
        if route_id in explicit_routes and "alive" in liveness_by_route.get(route_id, set()):
            preserved_live_cycles.append({"cycle_id": cycle, "route_id": route_id})
        else:
            external_cycles.append(cycle)
    external_route_holds = sorted({
        row["route_id"] for row in route_plan["actions"]
        if row["action"] in {"preserve-live", "preserve-unverifiable"}
        and row.get("route_id") not in explicit_routes
    })
    quiesce = {
        "open_cycles": open_cycles,
        "self_cycles": sorted(self_set & set(open_cycles)),
        "preserved_live_cycles": preserved_live_cycles,
        "external_open_cycles": external_cycles,
        "explicit_route_holds": sorted(explicit_routes),
        "external_route_holds": external_route_holds,
        "proven": not external_cycles and not external_route_holds,
    }
    route_file = Path(residue_route).resolve()
    try:
        residue_route_body = json.loads(route_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CutoverError("residue-route-invalid", str(exc))
    routes = _route_module()
    if not _valid_route_identity(residue_route_body, routes):
        raise CutoverError("residue-route-invalid", str(route_file))
    if residue_route_body.get("artifact_root") != str(root):
        raise CutoverError("residue-route-root-mismatch", str(route_file))
    backup_base = Path(backup_root).expanduser().resolve()
    provisional = {
        "schema_version": 1,
        "artifact_root_id": identity.artifact_root_id,
        "repository_id": identity.repository_id,
        "route_sweep_plan_sha256": route_package["plan_sha256"],
        "residue_route": {
            "route_id": residue_route_body["route_id"],
            "route_hash": residue_route_body["route_hash"],
            "path": str(route_file),
        },
        "jobs": _jobs_digest(jobs),
        "preserve_routes": sorted(explicit_routes),
        "self_cycles": sorted(self_set),
        "sealed_evidence_exclusions": list(SEALED_EVIDENCE_PATHS),
        "preserved_support_containers": list(PRESERVED_SUPPORT_CONTAINERS),
        "source_inventory": inventory,
        "source_directories": sorted(directories, key=lambda value: (value.count("/"), value)),
        "target_blockers": sorted(blockers, key=lambda row: (row["path"], row["reason"])),
        "totals": {
            "files": len(inventory),
            "bytes": sum(row["size"] for row in inventory),
            "by_classification": {
                key: sum(1 for row in inventory if row["classification"] == key)
                for key in sorted({row["classification"] for row in inventory})
            },
        },
        "core_section3_sync": core_sync,
        "quiesce": quiesce,
    }
    provisional_digest = hashlib.sha256(_canonical_bytes(provisional)).hexdigest()
    provisional["planned_destination"] = {
        "campaign_key": "legacy-runtime-residue",
        "layout": "plans/legacy-runtime-residue/objects/<sha256>",
        "backup_dir": str(backup_base / identity.artifact_root_id / f"w7f-{provisional_digest[:16]}"),
        "backup_tar": "legacy-runtime-residue.tar",
    }
    provisional["apply_ready"] = bool(
        inventory and not blockers and core_sync["proven"] and quiesce["proven"]
    )
    return provisional


def closeout_residue_package(root: Path, **kwargs: Any) -> Dict[str, Any]:
    return _plan_package(
        "w7f-closeout-residue-plan",
        build_closeout_residue_plan(root, **kwargs),
    )


def closeout_approval_package(
    root: Path,
    *,
    route_package: Dict[str, Any],
    closeout_package: Dict[str, Any],
) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    _validate_closeout_pair(closeout_package, route_package, identity.artifact_root_id)
    plan = closeout_package["plan"]
    return {
        "schema_version": 1,
        "kind": "w7f-closeout-approval",
        "authorized": False,
        "artifact_root_id": identity.artifact_root_id,
        "repository_id": identity.repository_id,
        "route_sweep_plan_sha256": route_package["plan_sha256"],
        "closeout_residue_plan_sha256": closeout_package["plan_sha256"],
        "source_inventory_sha256": "sha256:" + hashlib.sha256(
            _canonical_bytes(plan["source_inventory"])
        ).hexdigest(),
        "source_file_count": plan["totals"]["files"],
        "source_byte_size": plan["totals"]["bytes"],
        "source_by_classification": plan["totals"]["by_classification"],
        "planned_destination": plan["planned_destination"],
        "sealed_evidence_exclusions": plan["sealed_evidence_exclusions"],
        "quiesce": plan["quiesce"],
        "core_section3_sync": plan["core_section3_sync"],
        "route_sweep_counts": route_package["plan"]["counts"],
        "route_population": route_package["plan"]["route_population"],
        "route_sweep_package": route_package,
        "closeout_package": closeout_package,
    }


def _validate_plan_package(package: Dict[str, Any], kind: str) -> None:
    if package.get("schema_version") != 1 or package.get("kind") != kind:
        raise CutoverError("plan-package-invalid", kind)
    plan = package.get("plan")
    if not isinstance(plan, dict):
        raise CutoverError("plan-package-invalid", kind)
    expected = "sha256:" + hashlib.sha256(_canonical_bytes(plan)).hexdigest()
    if package.get("plan_sha256") != expected:
        raise CutoverError("plan-package-digest-mismatch", kind)


def _validate_closeout_pair(
    closeout_package: Dict[str, Any],
    route_package: Dict[str, Any],
    root_id: str,
) -> None:
    _validate_plan_package(closeout_package, "w7f-closeout-residue-plan")
    _validate_plan_package(route_package, "w7f-route-sweep-plan")
    if closeout_package["plan"].get("artifact_root_id") != root_id:
        raise CutoverError("closeout-plan-root-mismatch")
    if route_package["plan"].get("artifact_root_id") != root_id:
        raise CutoverError("route-plan-root-mismatch")
    if closeout_package["plan"].get("route_sweep_plan_sha256") != route_package["plan_sha256"]:
        raise CutoverError("closeout-route-plan-mismatch")


def _approval_body(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CutoverError("approval-invalid", str(exc))
    if value.get("authorized") is not True:
        raise CutoverError("approval-not-authorized")
    return value


def _approval(path: Path, *, root_id: str, closeout_digest: str,
              route_digest: str) -> Dict[str, Any]:
    value = _approval_body(path)
    if value.get("artifact_root_id") != root_id:
        raise CutoverError("approval-root-mismatch")
    if value.get("closeout_residue_plan_sha256") != closeout_digest:
        raise CutoverError("approval-closeout-digest-mismatch")
    if value.get("route_sweep_plan_sha256") != route_digest:
        raise CutoverError("approval-route-digest-mismatch")
    return value


def _closeout_journal(root: Path, name: str) -> Path:
    path = P.producer_dir(root) / "closeout" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def recover_closeout_prebackup(root: Path, *, journal_path: Path, reason: str) -> Dict[str, Any]:
    """Abort a prepared closeout and restore partial canonical route moves.

    This recovery is intentionally unavailable after backup starts.  Route
    closure is append-only, so a partially applied close-abandoned action is
    also outside the recoverable shape and must roll forward instead.
    """

    root = Path(root).resolve()
    _require_active(root)
    journal_path = Path(journal_path).resolve()
    expected_parent = (P.producer_dir(root) / "closeout").resolve()
    if journal_path.parent != expected_parent or not journal_path.is_file():
        raise CutoverError("closeout-recovery-journal-invalid", str(journal_path))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("kind") != "w7f-closeout-residue-journal":
        raise CutoverError("closeout-recovery-journal-invalid", str(journal_path))
    if journal.get("phase") == "aborted-prebackup":
        return {"status": "already-aborted", **journal}
    if journal.get("phase") != "prepared":
        raise CutoverError("closeout-recovery-phase-invalid", str(journal.get("phase")))
    route_package = journal.get("route_sweep_package")
    closeout_package = journal.get("closeout_package")
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None or not isinstance(route_package, dict) or not isinstance(closeout_package, dict):
        raise CutoverError("closeout-recovery-package-missing", str(journal_path))
    _validate_closeout_pair(closeout_package, route_package, identity.artifact_root_id)
    route_digest = route_package["plan_sha256"]
    route_journal_path = _closeout_journal(
        root, f"route-sweep-{route_digest.split(':', 1)[-1]}"
    )
    route_journal = (
        json.loads(route_journal_path.read_text(encoding="utf-8"))
        if route_journal_path.is_file()
        else {
            "schema_version": 1,
            "kind": "w7f-route-sweep-journal",
            "plan_sha256": route_digest,
            "phase": "applying",
            "applied": [],
        }
    )
    if (
        route_journal.get("kind") != "w7f-route-sweep-journal"
        or route_journal.get("plan_sha256") != route_digest
        or route_journal.get("phase") not in {"applying", "aborted-prebackup"}
    ):
        raise CutoverError("route-recovery-journal-invalid", str(route_journal_path))
    if any(row.get("action") == "close-abandoned" for row in route_journal.get("applied", [])):
        raise CutoverError("route-recovery-nonreversible-close", str(route_journal_path))

    restored = []
    for row in reversed(route_package["plan"]["actions"]):
        if row.get("action") != "canonicalize":
            continue
        source = root / row["source_paths"][0]
        target = root / row["target_paths"][0]
        source_outcome = source.with_name(source.stem + ".outcome.json")
        target_outcome = target.with_name(target.stem + ".outcome.json")
        if source.exists() and target.exists():
            raise CutoverError("route-recovery-collision", str(target))
        if not source.exists() and target.is_file() and not target.is_symlink():
            route = json.loads(target.read_text(encoding="utf-8"))
            if route.get("route_hash") != row.get("route_hash"):
                raise CutoverError("route-recovery-target-mismatch", str(target))
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
            if target_outcome.exists():
                if source_outcome.exists():
                    raise CutoverError("route-recovery-collision", str(source_outcome))
                os.replace(target_outcome, source_outcome)
            restored.append({"route_id": row.get("route_id"), "source": row["source_paths"][0]})
        elif not source.is_file() or source.is_symlink() or target.exists() or target_outcome.exists():
            raise CutoverError("route-recovery-shape-invalid", row["source_paths"][0])

    route_journal.update({
        "phase": "aborted-prebackup",
        "abort_reason": reason,
        "restored": restored,
    })
    P._write_atomic(route_journal_path, P._json_bytes(route_journal))
    journal.update({
        "phase": "aborted-prebackup",
        "abort_reason": reason,
        "route_recovery_journal": str(route_journal_path),
        "restored_routes": restored,
    })
    P._write_atomic(journal_path, P._json_bytes(journal))
    return {"status": "aborted-prebackup", **journal}


def _apply_route_sweep_plan(root: Path, package: Dict[str, Any]) -> Dict[str, Any]:
    digest = package["plan_sha256"]
    short = digest.split(":", 1)[-1]
    journal_path = _closeout_journal(root, f"route-sweep-{short}")
    if journal_path.is_file():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("phase") == "complete" and journal.get("plan_sha256") == digest:
            return {"status": "already-applied", "journal": str(journal_path), **journal}
    routes = _route_module()
    applied = []
    for row in package["plan"]["actions"]:
        action = row["action"]
        if action not in {"close-abandoned", "canonicalize"}:
            continue
        source = root / row["source_paths"][0]
        if action == "close-abandoned":
            if source.is_file():
                outcome = source.with_name(source.stem + ".outcome.json")
                if not outcome.is_file():
                    route = json.loads(source.read_text(encoding="utf-8"))
                    routes.close_route(route, source, summary="abandoned-closeout")
            applied.append({"action": action, "route_id": row.get("route_id")})
        else:
            target = root / row["target_paths"][0]
            source_outcome = source.with_name(source.stem + ".outcome.json")
            target_outcome = target.with_name(target.stem + ".outcome.json")
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise CutoverError("route-canonicalization-collision", str(target))
                os.replace(source, target)
            elif not target.is_file():
                raise CutoverError("route-canonicalization-source-missing", str(source))
            if target.is_symlink() or not target.is_file():
                raise CutoverError("route-canonicalization-target-invalid", str(target))
            route = json.loads(target.read_text(encoding="utf-8"))
            if row.get("route_hash") and route.get("route_hash") != row["route_hash"]:
                raise CutoverError("route-canonicalization-target-mismatch", str(target))
            if source_outcome.is_file():
                if target_outcome.exists():
                    raise CutoverError("route-canonicalization-collision", str(target_outcome))
                os.replace(source_outcome, target_outcome)
            if not target_outcome.is_file():
                # Runtime close refuses alias basenames under the canonical
                # routes directory.  Move first, then close at the canonical
                # path.  An existing target without an outcome is the durable
                # crash-recovery shape after the route move.
                routes.close_route(route, target, summary="abandoned-closeout")
            applied.append({"action": action, "route_id": row.get("route_id")})
        journal = {
            "schema_version": 1,
            "kind": "w7f-route-sweep-journal",
            "plan_sha256": digest,
            "phase": "applying",
            "applied": applied,
        }
        P._write_atomic(journal_path, P._json_bytes(journal))
    journal = {
        "schema_version": 1,
        "kind": "w7f-route-sweep-journal",
        "plan_sha256": digest,
        "phase": "complete",
        "applied": applied,
    }
    P._write_atomic(journal_path, P._json_bytes(journal))
    return {"status": "applied", "journal": str(journal_path), **journal}


def apply_route_sweep(root: Path, *, package: Dict[str, Any], approval_path: Path,
                      closeout_digest: str) -> Dict[str, Any]:
    _validate_plan_package(package, "w7f-route-sweep-plan")
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    _approval(
        approval_path,
        root_id=identity.artifact_root_id,
        closeout_digest=closeout_digest,
        route_digest=package["plan_sha256"],
    )
    return _apply_route_sweep_plan(root, package)


def _backup_closeout_sources(root: Path, plan: Dict[str, Any], backup_root: Path,
                             plan_sha256: str) -> Dict[str, Any]:
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    backup_root = Path(backup_root).expanduser().resolve()
    if backup_root == root or str(backup_root).startswith(str(root) + os.sep):
        raise CutoverError("backup-root-inside-artifact-root", str(backup_root))
    backup_dir = Path(plan["planned_destination"]["backup_dir"])
    expected_parent = backup_root / identity.artifact_root_id
    if backup_dir.parent != expected_parent:
        raise CutoverError("backup-plan-root-mismatch", str(backup_dir))
    archive = backup_dir / plan["planned_destination"]["backup_tar"]
    manifest = backup_dir / "source-inventory.json"
    seal_path = backup_dir / "backup-seal.json"
    if seal_path.is_file():
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        if seal.get("plan_sha256") != plan_sha256:
            raise CutoverError("backup-seal-plan-mismatch", str(seal_path))
        if (
            seal.get("archive") != str(archive)
            or seal.get("manifest") != str(manifest)
            or not archive.is_file()
            or not manifest.is_file()
            or seal.get("archive_sha256") != "sha256:" + _sha(archive)
            or seal.get("manifest_sha256") != "sha256:" + _sha(manifest)
            or seal.get("file_count") != len(plan["source_inventory"])
            or seal.get("byte_size") != sum(row["size"] for row in plan["source_inventory"])
        ):
            raise CutoverError("backup-seal-verification-failed", str(seal_path))
        return seal
    backup_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(archive), "w") as tar:
        for rel in plan["source_directories"]:
            path = root / rel
            if path.is_dir() and not path.is_symlink():
                tar.add(str(path), arcname=rel, recursive=False)
        for row in plan["source_inventory"]:
            path = root / row["path"]
            if not path.is_file() or path.is_symlink() or "sha256:" + _sha(path) != row["sha256"]:
                raise CutoverError("approval-stale", row["path"])
            tar.add(str(path), arcname=row["path"], recursive=False)
    with archive.open("rb") as handle:
        os.fsync(handle.fileno())
    P._write_atomic(manifest, _canonical_bytes(plan["source_inventory"]))
    verified = {}
    with tarfile.open(str(archive), "r") as tar:
        for row in plan["source_inventory"]:
            member = tar.getmember(row["path"])
            handle = tar.extractfile(member)
            if handle is None:
                raise CutoverError("backup-incomplete", row["path"])
            verified[row["path"]] = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    mismatch = [
        row["path"] for row in plan["source_inventory"]
        if verified.get(row["path"]) != row["sha256"]
    ]
    if mismatch:
        raise CutoverError("backup-digest-mismatch", mismatch[0])
    seal = {
        "schema_version": 1,
        "kind": "w7f-closeout-backup-seal",
        "artifact_root_id": identity.artifact_root_id,
        "plan_sha256": plan_sha256,
        "archive": str(archive),
        "archive_sha256": "sha256:" + _sha(archive),
        "manifest": str(manifest),
        "manifest_sha256": "sha256:" + _sha(manifest),
        "file_count": len(plan["source_inventory"]),
        "byte_size": sum(row["size"] for row in plan["source_inventory"]),
    }
    P._write_atomic(seal_path, P._json_bytes(seal))
    return seal


def _begin_residue_cycle(root: Path, plan: Dict[str, Any]) -> Dict[str, Any]:
    begun = P.begin(
        root,
        route_file=Path(plan["residue_route"]["path"]),
        capability="autopilot-code",
        intensity="direct",
        campaign_key="legacy-runtime-residue",
        title="W7F legacy runtime residue",
        goal="preserve approved legacy runtime residue before source retirement",
    )
    if begun["status"] not in {"begun", "resumed"}:
        raise CutoverError("residue-cycle-begin-failed", begun.get("status", ""))
    return {
        "campaign_id": begun["campaign_id"],
        "cycle_id": begun["cycle_id"],
        "cycle_dir": begun["cycle_dir"],
    }


def _seal_residue_cycle(
    root: Path,
    plan: Dict[str, Any],
    plan_sha256: str,
    begun: Dict[str, Any],
) -> Dict[str, Any]:
    route_file = Path(plan["residue_route"]["path"])
    record = P.read_cycle_record(root, begun["cycle_id"])
    if record is None:
        raise CutoverError("residue-cycle-missing", begun["cycle_id"])
    expected_dir = P.cycle_dir(root, begun["campaign_id"], begun["cycle_id"])
    if (
        record.get("campaign_id") != begun["campaign_id"]
        or record.get("route_id") != plan["residue_route"]["route_id"]
        or record.get("route_hash") != plan["residue_route"]["route_hash"]
        or Path(begun["cycle_dir"]).resolve() != expected_dir.resolve()
    ):
        raise CutoverError("residue-cycle-binding-mismatch", begun["cycle_id"])
    if record.get("state") == "sealed":
        manifest_path = expected_dir / "manifest.json"
        if not manifest_path.is_file() or record.get("manifest_digest") != "sha256:" + _sha(manifest_path):
            raise CutoverError("residue-cycle-seal-drift", begun["cycle_id"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return {
            "campaign_id": begun["campaign_id"],
            "cycle_id": begun["cycle_id"],
            "cycle_dir": str(expected_dir),
            "manifest_digest": record["manifest_digest"],
            "artifact_count": len(manifest.get("artifact_revisions", [])),
        }
    if record.get("state") != "open":
        raise CutoverError("residue-cycle-state-invalid", str(record.get("state")))
    cycle_dir = expected_dir
    objects = cycle_dir / "artifacts" / "plans" / "legacy-runtime-residue" / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in plan["source_inventory"]:
        source = root / row["path"]
        if not source.is_file() or source.is_symlink() or "sha256:" + _sha(source) != row["sha256"]:
            raise CutoverError("approval-stale", row["path"])
        digest = row["sha256"].split(":", 1)[-1]
        destination = objects / digest
        data = source.read_bytes()
        if destination.exists():
            if destination.is_symlink() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise CutoverError("residue-object-collision", digest)
        else:
            P._write_exclusive(destination, data, 0o644)
        rows.append({
            **row,
            "object": os.path.relpath(destination, cycle_dir / "artifacts"),
        })
    inventory_path = cycle_dir / "artifacts" / "plans" / "legacy-runtime-residue" / "inventory.json"
    inventory_document = {
        "schema_version": 1,
        "kind": "legacy-runtime-residue-inventory",
        "artifact_root_id": plan["artifact_root_id"],
        "closeout_plan_sha256": plan_sha256,
        "source_files": rows,
    }
    if inventory_path.exists():
        if inventory_path.read_bytes() != _canonical_bytes(inventory_document):
            raise CutoverError("residue-inventory-drift", str(inventory_path))
    else:
        P._write_exclusive(inventory_path, _canonical_bytes(inventory_document), 0o644)
    evidence = cycle_dir / "artifacts" / "evidence" / "closeout.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence_body = _canonical_bytes({
        "schema_version": 1,
        "closeout_plan_sha256": plan_sha256,
        "source_file_count": len(rows),
    })
    if not evidence.exists():
        P._write_exclusive(evidence, evidence_body, 0o644)
    routes = _route_module()
    route = json.loads(route_file.read_text(encoding="utf-8"))
    for node in route.get("nodes", []):
        if node.get("terminal") is True:
            routes.write_completion_marker(route, node, node["id"], evidence)
    outcome, _ = routes.close_route(route, route_file, summary="legacy-runtime-residue sealed")
    if outcome.get("terminal_gate_proven") is not True:
        raise CutoverError("residue-route-terminal-unproven", route["route_id"])
    sealed = P.finalize(
        root,
        cycle_id=begun["cycle_id"],
        state="completed",
        primary="plans/legacy-runtime-residue/inventory.json",
    )
    if sealed.get("status") not in {"sealed", "already-sealed"}:
        raise CutoverError("residue-cycle-seal-failed", sealed.get("status", ""))
    return {
        "campaign_id": begun["campaign_id"],
        "cycle_id": begun["cycle_id"],
        "cycle_dir": str(cycle_dir),
        "manifest_digest": sealed.get("manifest_digest"),
        "artifact_count": sealed.get("artifact_count"),
    }


def _retire_closeout_sources(root: Path, plan: Dict[str, Any]) -> Dict[str, Any]:
    retired = []
    already_absent = []
    for row in plan["source_inventory"]:
        path = root / row["path"]
        if not path.exists() and not path.is_symlink():
            already_absent.append(row["path"])
            continue
        if not path.is_file() or path.is_symlink() or "sha256:" + _sha(path) != row["sha256"]:
            raise CutoverError("approval-stale", row["path"])
        path.unlink()
        retired.append(row["path"])
    pruned = []
    protected = {root / ".runtime" / "routes"}
    for rel in sorted(plan["source_directories"], key=lambda value: (-value.count("/"), value)):
        path = root / rel
        if path in protected or not path.is_dir() or path.is_symlink():
            continue
        try:
            path.rmdir()
            pruned.append(rel)
        except OSError:
            pass
    return {
        "retired_files": retired,
        "already_absent": already_absent,
        "pruned_directories": pruned,
    }


def _approved_route_sweep_complete(root: Path, route_digest: str) -> bool:
    short = route_digest.split(":", 1)[-1]
    path = _closeout_journal(root, f"route-sweep-{short}")
    if not path.is_file():
        return False
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return journal.get("phase") == "complete" and journal.get("plan_sha256") == route_digest


def _validate_post_sweep_drift(current: Dict[str, Any], approved: Dict[str, Any]) -> None:
    stable_fields = (
        "artifact_root_id", "repository_id", "residue_route", "jobs", "preserve_routes",
        "self_cycles", "sealed_evidence_exclusions", "preserved_support_containers",
        "source_inventory", "source_directories", "target_blockers", "totals",
        "core_section3_sync", "quiesce",
    )
    for field in stable_fields:
        if current.get(field) != approved.get(field):
            raise CutoverError("approval-stale", field)
    if current.get("apply_ready") is not True:
        raise CutoverError("closeout-preconditions-unproven")


def apply_closeout_residue(
    root: Path,
    *,
    closeout_package: Dict[str, Any],
    route_package: Dict[str, Any],
    approval_path: Path,
    backup_root: Path,
    crash_after_phase: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = P.artifact_lifecycle.read_root_identity(root)
    if identity is None:
        raise CutoverError("root-identity-missing")
    _validate_closeout_pair(closeout_package, route_package, identity.artifact_root_id)
    approval = _approval_body(approval_path)
    if approval.get("artifact_root_id") != identity.artifact_root_id:
        raise CutoverError("approval-root-mismatch")
    closeout_digest = str(approval.get("closeout_residue_plan_sha256", ""))
    route_digest = str(approval.get("route_sweep_plan_sha256", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", closeout_digest):
        raise CutoverError("approval-closeout-digest-invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", route_digest):
        raise CutoverError("approval-route-digest-invalid")
    short = closeout_digest.split(":", 1)[-1]
    journal_path = _closeout_journal(root, f"residue-{short}")
    if journal_path.is_file():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("phase") == "complete" and journal.get("plan_sha256") == closeout_digest:
            return {
                "status": "already-applied",
                "journal": str(journal_path),
                **{key: value for key, value in journal.items()
                   if key not in {"closeout_package", "route_sweep_package"}},
            }
        if (
            journal.get("kind") != "w7f-closeout-residue-journal"
            or journal.get("plan_sha256") != closeout_digest
            or journal.get("route_sweep_plan_sha256") != route_digest
        ):
            raise CutoverError("closeout-journal-binding-mismatch", str(journal_path))
        closeout_package = journal.get("closeout_package")
        route_package = journal.get("route_sweep_package")
        if not isinstance(closeout_package, dict) or not isinstance(route_package, dict):
            raise CutoverError("closeout-journal-package-missing", str(journal_path))
        _validate_closeout_pair(closeout_package, route_package, identity.artifact_root_id)
        _approval(
            approval_path,
            root_id=identity.artifact_root_id,
            closeout_digest=closeout_package["plan_sha256"],
            route_digest=route_package["plan_sha256"],
        )
    else:
        if (
            closeout_package["plan_sha256"] != closeout_digest
            or route_package["plan_sha256"] != route_digest
        ):
            approved_closeout = approval.get("closeout_package")
            approved_route = approval.get("route_sweep_package")
            if not isinstance(approved_closeout, dict) or not isinstance(approved_route, dict):
                raise CutoverError("approval-plan-package-missing")
            _validate_closeout_pair(approved_closeout, approved_route, identity.artifact_root_id)
            if not _approved_route_sweep_complete(root, route_digest):
                raise CutoverError("approval-stale", "plan-digest")
            _validate_post_sweep_drift(closeout_package["plan"], approved_closeout["plan"])
            closeout_package = approved_closeout
            route_package = approved_route
        _approval(
            approval_path,
            root_id=identity.artifact_root_id,
            closeout_digest=closeout_package["plan_sha256"],
            route_digest=route_package["plan_sha256"],
        )
        journal = {
            "schema_version": 1,
            "kind": "w7f-closeout-residue-journal",
            "plan_sha256": closeout_digest,
            "route_sweep_plan_sha256": route_digest,
            "phase": "prepared",
            "closeout_package": closeout_package,
            "route_sweep_package": route_package,
        }
        P._write_atomic(journal_path, P._json_bytes(journal))
        if crash_after_phase == "prepared":
            raise CutoverError("crash-fixture", "prepared")
    plan = closeout_package["plan"]
    if not plan.get("apply_ready"):
        raise CutoverError("closeout-preconditions-unproven")
    phase = journal.get("phase")
    if phase == "prepared":
        for row in plan["source_inventory"]:
            path = root / row["path"]
            if not path.is_file() or path.is_symlink() or "sha256:" + _sha(path) != row["sha256"]:
                raise CutoverError("approval-stale", row["path"])
        sweep = _apply_route_sweep_plan(root, route_package)
        backup = _backup_closeout_sources(root, plan, backup_root, closeout_digest)
        journal.update({"phase": "backup-sealed", "route_sweep": sweep, "backup": backup})
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = "backup-sealed"
        if crash_after_phase == phase:
            raise CutoverError("crash-fixture", phase)
    if phase == "backup-sealed":
        begun = _begin_residue_cycle(root, plan)
        journal.update({"phase": "destination-started", "destination_cycle": begun})
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = "destination-started"
        if crash_after_phase == phase:
            raise CutoverError("crash-fixture", phase)
    if phase == "destination-started":
        cycle = _seal_residue_cycle(root, plan, closeout_digest, journal["destination_cycle"])
        journal.update({"phase": "destination-sealed", "destination_cycle": cycle})
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = "destination-sealed"
        if crash_after_phase == phase:
            raise CutoverError("crash-fixture", phase)
    if phase == "destination-sealed":
        retirement = _retire_closeout_sources(root, plan)
        journal.update({"phase": "complete", "retirement": retirement})
        P._write_atomic(journal_path, P._json_bytes(journal))
        phase = "complete"
    if phase != "complete":
        raise CutoverError("closeout-journal-phase-invalid", str(phase))
    return {
        "status": "applied",
        "journal": str(journal_path),
        **{key: value for key, value in journal.items()
           if key not in {"closeout_package", "route_sweep_package"}},
    }


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
    P._write_atomic(path, data)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _require_active(root: Path) -> Dict[str, Any]:
    cut = P.read_cutover(root)
    if cut.get("state") != "active":
        raise CutoverError("cutover-inactive", "run artifact_producer.py activate (gate G1) first")
    return cut


def _excluded(rel: str, excludes: Sequence[str]) -> bool:
    return any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in excludes)


def _has_hidden_component(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/"))


def _prune_hidden_copies(root: Path, run_dir: Path, report: Dict[str, Any]) -> List[str]:
    """Remove copied targets whose locator has a hidden component (an earlier
    migrate-delta copied them before the D-6 rule was applied) and rewrite the
    journal, inverse and map without them."""
    pruned: List[str] = []
    for name in ("journal.jsonl", "inverse.jsonl", "compatibility-map.jsonl"):
        path = run_dir / name
        if not path.is_file():
            continue
        kept = []
        for row in _read_jsonl(path):
            target = row.get("target_locator", "")
            if row.get("kind", "file") == "file" and _has_hidden_component(target):
                if name == "journal.jsonl":
                    victim = root / target
                    if victim.is_file() and not os.path.islink(str(victim)):
                        victim.unlink()
                    pruned.append(row.get("source_locator", target))
                continue
            kept.append(row)
        report["digests"][name.split(".")[0].replace("compatibility-map", "compatibility_map")] = _write_jsonl(path, kept)
    if pruned:
        report["skipped_hidden_components"] = sorted(set(report.get("skipped_hidden_components", [])) | set(pruned))
        report["journal_rows"] = len(_read_jsonl(run_dir / "journal.jsonl"))
    return pruned


def migrations_dir(root: Path) -> Path:
    return P.producer_dir(root) / "migrations"


# ---------------------------------------------------------------------------
# G2 migrate-delta
# ---------------------------------------------------------------------------


def _identity_refs(identity, campaign_id: Optional[str], cycle_id: Optional[str],
                   shared: Optional[Tuple[str, str, str]] = None) -> List[Dict[str, str]]:
    rows = [
        {"binding_key": "repository", "id_kind": "repository", "required_id": "repository_id", "stable_id": identity.repository_id},
        {"binding_key": "artifact_root", "id_kind": "artifact_root", "required_id": "artifact_root_id", "stable_id": identity.artifact_root_id},
    ]
    if campaign_id:
        rows.append({"binding_key": "campaign", "id_kind": "campaign", "required_id": "campaign_id", "stable_id": campaign_id})
    if cycle_id:
        rows.append({"binding_key": "cycle", "id_kind": "cycle", "required_id": "cycle_id", "stable_id": cycle_id})
    if shared:
        kind, ref, rrev = shared
        rows.append({"binding_key": kind, "id_kind": "shared_reference", "required_id": "shared_reference_id", "stable_id": ref})
        rows.append({"binding_key": kind, "id_kind": "shared_reference_revision", "required_id": "shared_reference_revision_id", "stable_id": rrev})
    return rows


def migrate_delta(root: Path, *, census_rows: Path, route_file: Path, capability: str, intensity: str,
                  excludes: Sequence[str], approval_receipt_sha256: Optional[str], campaign_id: Optional[str],
                  campaign_key: Optional[str] = "w7c-delta-migration") -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    rows = _read_jsonl(census_rows)
    candidates = [r for r in rows if r["disposition"] in CANDIDATE_DISPOSITIONS and r["kind"] == "file"
                  and r["detail"].startswith("cycle-candidate:") and not _excluded(r["path"], excludes)]
    skipped_excluded = [r["path"] for r in rows if _excluded(r["path"], excludes) and r["kind"] == "file"]
    begun = P.begin(root, route_file=route_file, capability=capability, intensity=intensity,
                    campaign_id=campaign_id, campaign_key=campaign_key,
                    title="W7C delta migration", goal="relocate the post-W7 legacy delta into cycle output")
    if begun["status"] not in ("begun", "resumed"):
        raise CutoverError("begin-failed", begun.get("status", "?"))
    cycle_dir = Path(begun["cycle_dir"])
    run_dir = migrations_dir(root) / f"{_stamp()}-{begun['cycle_id']}"
    run_dir.mkdir(parents=True, exist_ok=False)
    journal: List[Dict[str, Any]] = []
    inverse: List[Dict[str, Any]] = []
    mapping: List[Dict[str, Any]] = []
    made_dirs: set = set()
    ordinal = 0

    def copy_one(rel: str, target_rel: str, refs) -> None:
        nonlocal ordinal
        src = root / rel
        dst = root / target_rel
        if os.path.islink(str(src)) or not src.is_file():
            raise CutoverError("source-not-regular", rel)
        parent_rel = os.path.dirname(target_rel)
        chain = []
        p = parent_rel
        while p and p not in made_dirs and not (root / p).exists():
            chain.append(p)
            p = os.path.dirname(p)
        for d in reversed(chain):
            (root / d).mkdir()
            made_dirs.add(d)
            journal.append({"schema_version": JOURNAL_SCHEMA, "row_ordinal": ordinal, "action": "create_destination",
                            "kind": "directory", "source_locator": os.path.dirname(rel), "target_locator": d,
                            "sha256": None, "size": None, "mode": 0o755, "commit_state": "committed",
                            "source_preserved": True, "link_inverse": {"action": "none"},
                            "mapping_inverse": {"action": "none"}})
            inverse.append({"ordinal": ordinal, "action": "remove_directory_if_empty", "target_locator": d})
            ordinal += 1
        verdict = P.check_write(root, dst)
        if verdict["verdict"] != "allow":
            raise CutoverError(verdict["reason"], target_rel)
        data = src.read_bytes()
        P._write_exclusive(dst, data, stat.S_IMODE(src.stat().st_mode) & 0o644 | 0o644)
        digest = hashlib.sha256(data).hexdigest()
        journal.append({"schema_version": JOURNAL_SCHEMA, "row_ordinal": ordinal, "action": "create_destination",
                        "kind": "file", "source_locator": rel, "target_locator": target_rel, "sha256": digest,
                        "size": len(data), "mode": src.stat().st_mode & 0o777, "commit_state": "committed",
                        "source_preserved": True, "link_inverse": {"action": "none"},
                        "mapping_inverse": {"action": "remove_mapping_row", "source_locator": rel}})
        inverse.append({"ordinal": ordinal, "action": "remove_file", "target_locator": target_rel, "sha256": digest})
        mapping.append({"schema_version": MAP_SCHEMA, "kind": "file", "source_locator": rel,
                        "target_locator": target_rel, "sha256": digest, "identity_refs": refs})
        ordinal += 1

    cycle_refs = _identity_refs(identity, begun["campaign_id"], begun["cycle_id"])
    per_bucket: Dict[str, int] = {}
    skipped_hidden: List[str] = []
    for row in candidates:
        rel = row["path"]
        bucket = rel.split("/", 1)[0]
        if _has_hidden_component(rel):
            skipped_hidden.append(rel)  # D-6 locators cannot name dot-files; stays legacy
            continue
        if bucket in SHARED_SNAPSHOT:
            continue  # shared kinds are snapshotted whole below
        if bucket not in CYCLE_BUCKETS:
            continue
        target_rel = os.path.relpath(str(cycle_dir / "artifacts" / rel), str(root))
        copy_one(rel, target_rel, cycle_refs)
        per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
    # Full snapshots of the current shared-kind trees (a revision is a whole copy).
    snapshot_counts: Dict[str, int] = {}
    for bucket, kind in SHARED_SNAPSHOT.items():
        base = root / bucket
        if not base.is_dir() or os.path.islink(str(base)):
            continue
        staged = "shared-input/" + kind
        n = 0
        for entry in P._walk_files(base):
            if os.path.islink(str(entry)) or not entry.is_file():
                continue
            rel = entry.relative_to(root).as_posix()
            if _excluded(rel, excludes):
                continue
            if _has_hidden_component(rel):
                skipped_hidden.append(rel)
                continue
            target_rel = os.path.relpath(str(cycle_dir / "artifacts" / staged / entry.relative_to(base).as_posix()), str(root))
            copy_one(rel, target_rel, cycle_refs)
            n += 1
        snapshot_counts[kind] = n
    digests = {
        "journal": _write_jsonl(run_dir / "journal.jsonl", journal),
        "inverse": _write_jsonl(run_dir / "inverse.jsonl", inverse),
        "compatibility_map": _write_jsonl(run_dir / "compatibility-map.jsonl", mapping),
    }
    report = {
        "schema_version": 1, "kind": "w7c-delta-migration", "state": "copied-awaiting-seal", "created_at": _now(),
        "artifact_root": str(root), "run_dir": str(run_dir), "campaign_id": begun["campaign_id"],
        "cycle_id": begun["cycle_id"], "producer_id": begun["producer_id"], "cycle_dir": str(cycle_dir),
        "route_file": str(Path(route_file).resolve()), "approval_receipt_sha256": approval_receipt_sha256,
        "census_rows": str(Path(census_rows).resolve()), "census_rows_sha256": _sha(Path(census_rows)),
        "candidates_total": len(candidates), "copied_by_bucket": per_bucket, "shared_snapshots": snapshot_counts,
        "journal_rows": len(journal), "excluded_prefixes": list(excludes), "excluded_files": len(skipped_excluded),
        "skipped_hidden_components": skipped_hidden,
        "digests": digests, "sources_touched": False,
    }
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


def migrate_seal(root: Path, *, run_dir: Path, primary: Optional[str] = None,
                 spec_reference: Optional[str] = None, analysis_reference: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    run_dir = Path(run_dir)
    report = P._read_json(run_dir / "report.json")
    if report is None or report.get("kind") != "w7c-delta-migration":
        raise CutoverError("run-report-invalid", str(run_dir))
    if report.get("state") == "sealed":
        return report
    identity = P.artifact_lifecycle.read_root_identity(root)
    cycle_id = report["cycle_id"]
    report["pruned_hidden_copies"] = _prune_hidden_copies(root, run_dir, report)
    sealed = P.finalize(root, cycle_id=cycle_id, primary=primary)
    if sealed["status"] not in ("sealed", "already-sealed"):
        raise CutoverError("finalize-failed", sealed.get("status", "?"))
    report["finalize"] = sealed
    admitted: Dict[str, Any] = {}
    references = {"spec": spec_reference, "analysis": analysis_reference}
    cycle_dir = Path(report["cycle_dir"])
    for kind in ("spec", "analysis"):
        staged = cycle_dir / "artifacts" / "shared-input" / kind
        if not staged.is_dir():
            continue
        ref = references.get(kind)
        if ref:
            _adopt_reference(root, kind, ref, title=f"{kind} (W7 reference)")
        admitted[kind] = P.admit_shared(root, cycle_id=cycle_id, kind=kind, source=f"shared-input/{kind}",
                                        reference_id=ref, key=None if ref else kind, title=f"{kind} snapshot (W7C delta)")
    report["shared_admissions"] = admitted
    # Rewrite the map: shared snapshot rows now point at the immutable revision.
    mapping = _read_jsonl(run_dir / "compatibility-map.jsonl")
    rewritten = []
    for row in mapping:
        target = row["target_locator"]
        marker = "/artifacts/shared-input/"
        if marker in target:
            kind, _, rest = target.split(marker, 1)[1].partition("/")
            adm = admitted.get(kind)
            if adm:
                row = dict(row)
                row["target_locator"] = os.path.relpath(adm["revision_dir"], str(root)) + "/" + rest
                row["identity_refs"] = _identity_refs(identity, report["campaign_id"], cycle_id,
                                                      (kind, adm["shared_reference_id"], adm["shared_reference_revision_id"]))
        rewritten.append(row)
    report["digests"]["compatibility_map"] = _write_jsonl(run_dir / "compatibility-map.jsonl", rewritten)
    report["state"] = "sealed"
    report["sealed_at"] = _now()
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


def _tree_digest(directory: Path) -> Dict[str, Any]:
    """Byte-conservation witness: sorted (rel, size, sha256) over every regular file."""
    rows = []
    for entry in P._walk_files(directory):
        if os.path.islink(str(entry)) or not entry.is_file():
            continue
        rows.append((entry.relative_to(directory).as_posix(), entry.stat().st_size, _sha(entry)))
    rows.sort()
    payload = "\n".join(f"{r}\t{n}\t{d}" for r, n, d in rows).encode("utf-8")
    return {"file_count": len(rows), "byte_count": sum(n for _, n, _ in rows),
            "tree_sha256": hashlib.sha256(payload).hexdigest()}


def seal_legacy_cycle(root: Path, *, cycle_dir: Path, route_file: Path, capability: str = "autopilot-code",
                      title: Optional[str] = None, started_on: Optional[str] = None,
                      primary: Optional[str] = None, exclude_hidden: bool = False,
                      allocator=None) -> Dict[str, Any]:
    """W7E: adopt a producer record for an existing `campaigns/<camp>/cycles/<cyc>` directory
    that was created outside the producer (the W7 relocation), then run the ordinary
    finalize (manifest build, validation, index apply).  Bytes under `artifacts/` are
    never touched; the route must already be closed (this is a retrospective seal)."""
    root = Path(root).resolve()
    _require_active(root)
    directory = Path(cycle_dir).resolve()
    try:
        rel = directory.relative_to(root)
    except ValueError as exc:
        raise CutoverError("cycle-dir-outside-root", str(directory)) from exc
    parts = rel.parts
    if len(parts) != 4 or parts[0] != "campaigns" or parts[2] != "cycles":
        raise CutoverError("cycle-dir-shape-invalid", str(rel))
    campaign_id, cycle_id = parts[1], parts[3]
    if not (directory / "artifacts").is_dir():
        raise CutoverError("artifacts-dir-missing", str(rel))
    if (directory / "manifest.json").exists():
        raise CutoverError("manifest-already-present", str(rel))
    existing = P.read_cycle_record(root, cycle_id)
    if existing is not None and existing.get("state") != "no-lineage":
        raise CutoverError("cycle-record-exists", existing.get("state", "?"))
    campaign = P.read_campaign(root, campaign_id)
    if campaign is None:
        raise CutoverError("campaign-unknown", campaign_id)
    if capability not in P.ENTRY_CAPABILITIES:
        raise CutoverError("capability-unknown", capability)
    route = P.load_route(root, Path(route_file))
    if not P.route_is_closed(root, route):
        raise CutoverError("route-not-closed", route["route_id"])
    if route["capability"] != capability:
        raise CutoverError("route-capability-mismatch", f"{route['capability']}!={capability}")
    before = _tree_digest(directory / "artifacts")
    alloc = allocator or P.artifact_identity.IdAllocator()
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "cycle_id": cycle_id, "campaign_id": campaign_id,
        "producer_id": alloc.allocate("producer"), "parent_cycle_id": None,
        "capability": capability, "route_capability": route["capability"], "intensity": route["effective_intensity"],
        "route_id": route["route_id"], "route_hash": route["route_hash"],
        "route_file": str(Path(route_file).resolve()), "node_id": None, "state": "open",
        "started_on": started_on or _now(), "sealed_on": None, "manifest_digest": None,
        "title": title or f"{capability} legacy cycle (retrospective seal)",
        "adopted": {"kind": "seal-legacy-cycle", "adopted_on": _now(), "tree_before": before},
    }
    P._write_cycle_record(root, record, exclusive=existing is None)
    if cycle_id not in campaign.get("cycles", []):
        campaign["cycles"] = list(campaign.get("cycles", [])) + [cycle_id]
        P._write_campaign(root, campaign, exclusive=False)
    try:
        sealed = P.finalize(root, cycle_id=cycle_id, primary=primary, allocator=alloc, exclude_hidden=exclude_hidden)
    except P.ProducerError as exc:
        # Leave no half-adopted record behind; the directory itself is untouched.
        P.cycle_record_path(root, cycle_id).unlink(missing_ok=True)
        raise CutoverError("finalize-failed", f"{exc.code}: {exc.detail}") from exc
    if sealed.get("status") != "sealed":
        P.cycle_record_path(root, cycle_id).unlink(missing_ok=True)
        raise CutoverError("finalize-failed", sealed.get("status", "?"))
    after = _tree_digest(directory / "artifacts")
    if after != before:
        raise CutoverError("bytes-changed", json.dumps({"before": before, "after": after}))
    excluded = list(sealed.get("excluded_hidden") or [])
    if excluded:
        # Durable trace of what the manifest deliberately does not list.
        sealed_record = P.read_cycle_record(root, cycle_id) or {}
        sealed_record.setdefault("adopted", {})["hidden_excluded"] = [
            {"path": rel, "reason": P._unmanifestable_reason(rel), "sha256": _sha(directory / rel),
             "byte_size": (directory / rel).stat().st_size} for rel in excluded]
        P._write_cycle_record(root, sealed_record, exclusive=False)
    return {"status": "sealed", "cycle_id": cycle_id, "campaign_id": campaign_id, "route_id": route["route_id"],
            "producer_id": record["producer_id"], "manifest_digest": sealed["manifest_digest"],
            "artifact_count": sealed["artifact_count"], "hidden_excluded": len(excluded),
            "tree": after, "bytes_unchanged": True}


def adopt_campaign(root: Path, campaign_id: str, *, title: str, goal: str) -> Dict[str, Any]:
    """Create `campaign.json` for a W7-relocated campaign directory that has none.

    The W7 E2/E3 relocation created `campaigns/<camp>/cycles/<cyc>/artifacts/`
    additively without producer records; adopting the campaign lists its
    existing cycle directories so `begin --campaign` can join it.
    """
    root = Path(root).resolve()
    _require_active(root)
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        raise CutoverError("campaign-id-malformed", campaign_id)
    existing = P.read_campaign(root, campaign_id)
    if existing is not None:
        return existing
    directory = P.campaign_dir(root, campaign_id)
    if not directory.is_dir() or os.path.islink(str(directory)):
        raise CutoverError("campaign-dir-missing", str(directory))
    cycles_dir = directory / "cycles"
    cycles = sorted(p.name for p in cycles_dir.iterdir()
                    if p.is_dir() and artifact_identity.is_well_formed(p.name, "cycle")) if cycles_dir.is_dir() else []
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "campaign_id": campaign_id, "key": f"adopted:{campaign_id}",
        "title": title, "goal": goal, "completion_criterion": {"statement": "every cycle sealed with a manifest"},
        "state": "active", "created_on": _now(), "adopted_from": "w7-e2-e3-relocation", "cycles": cycles,
    }
    P._write_campaign(root, record, exclusive=True)
    return record


def _adopt_reference(root: Path, kind: str, ref_id: str, *, title: str) -> Dict[str, Any]:
    """Create `reference.json` for a W7-relocated shared reference that has none."""
    if not artifact_identity.is_well_formed(ref_id, "shared_reference"):
        raise CutoverError("reference-id-malformed", ref_id)
    path = P._reference_path(root, kind, ref_id)
    existing = P._read_json(path)
    if existing is not None:
        return existing
    revisions_dir = path.parent / "revisions"
    if not revisions_dir.is_dir():
        raise CutoverError("reference-unknown", ref_id)
    revisions = sorted(p.name for p in revisions_dir.iterdir()
                       if p.is_dir() and artifact_identity.is_well_formed(p.name, "shared_reference_revision"))
    record = {
        "schema_version": 1, "contract": P.CONTRACT, "shared_reference_id": ref_id, "kind": P.SHARED_KINDS[kind],
        "key": kind, "title": title, "created_on": _now(), "adopted_from": "w7-e2-e3-relocation",
        "latest_revision_id": revisions[-1] if revisions else None, "revisions": revisions,
    }
    P._write_exclusive(path, P._json_bytes(record))
    return record


# ---------------------------------------------------------------------------
# G3 compat-close / resolve-legacy
# ---------------------------------------------------------------------------


def compat_path(root: Path) -> Path:
    return P.producer_dir(root) / "compat.json"


def compat_close(root: Path, *, maps: Sequence[Path], approval_receipt_sha256: Optional[str]) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    rows = []
    for m in maps:
        m = Path(m).resolve()
        if not m.is_file():
            raise CutoverError("map-missing", str(m))
        with m.open(encoding="utf-8") as handle:
            row_count = sum(1 for line in handle if line.strip())
        rows.append({"path": str(m), "sha256": _sha(m), "rows": row_count})
    body = {"schema_version": 1, "contract": P.CONTRACT, "compatibility_window": "closed", "closed_at": _now(),
            "maps": rows, "approval_receipt_sha256": approval_receipt_sha256,
            "legacy_readers": "resolve through artifact_cutover.py resolve-legacy; latest map wins"}
    P._ensure_dir(compat_path(root).parent)
    P._write_atomic(compat_path(root), P._json_bytes(body), 0o600)
    return body


def _load_maps(root: Path) -> List[Tuple[str, Dict[str, str]]]:
    compat = P._read_json(compat_path(root)) or {}
    out = []
    for entry in compat.get("maps", []):
        path = Path(entry["path"])
        if not path.is_file():
            continue
        table = {}
        for row in _read_jsonl(path):
            table[row["source_locator"]] = row["target_locator"]
        out.append((str(path), table))
    return out


def resolve_legacy(root: Path, rel: str) -> Dict[str, Any]:
    root = Path(root).resolve()
    rel = rel.strip("/")
    direct = root / rel
    if direct.exists() and not os.path.islink(str(direct)):
        return {"path": rel, "resolution": "present", "target": rel, "absolute": str(direct)}
    maps = _load_maps(root)
    for name, table in reversed(maps):  # latest map wins
        if rel in table and (root / table[rel]).exists():
            return {"path": rel, "resolution": "mapped", "target": table[rel], "absolute": str(root / table[rel]), "map": name}
    # longest mapped ancestor directory
    parts = rel.split("/")
    for depth in range(len(parts) - 1, 0, -1):
        ancestor = "/".join(parts[:depth])
        tail = "/".join(parts[depth:])
        for name, table in reversed(maps):
            if ancestor in table and (root / table[ancestor] / tail).exists():
                return {"path": rel, "resolution": "mapped-ancestor", "target": table[ancestor] + "/" + tail,
                        "absolute": str(root / table[ancestor] / tail), "map": name}
    return {"path": rel, "resolution": "unresolved", "target": None, "absolute": None}


def latest_shared_revision(root: Path, kind: str) -> Optional[Path]:
    base = Path(root) / "shared" / kind
    if not base.is_dir():
        return None
    best: Optional[Tuple[str, Path]] = None
    for ref in sorted(base.iterdir()):
        record = P._read_json(ref / "reference.json")
        if record and record.get("latest_revision_id"):
            candidate = ref / "revisions" / record["latest_revision_id"]
            stamp = str(record.get("updated_on") or record.get("created_on") or "")
        else:
            revs = sorted(p for p in (ref / "revisions").iterdir() if p.is_dir()) if (ref / "revisions").is_dir() else []
            if not revs:
                continue
            candidate = revs[-1]
            stamp = ""
        if candidate.is_dir() and (best is None or stamp >= best[0]):
            best = (stamp, candidate)
    return best[1] if best else None


def prd_candidates(root: Path) -> List[str]:
    """Canonical prd.md candidates: legacy `spec/` first, else the latest shared/spec revision."""
    root = Path(root).resolve()
    out: List[str] = []
    legacy = root / "spec"
    if legacy.is_dir():
        if (legacy / "prd.md").is_file():
            out.append(str(legacy / "prd.md"))
        for d in sorted(legacy.iterdir()):
            if d.is_dir() and d.name != "_internal" and (d / "prd.md").is_file():
                out.append(str(d / "prd.md"))
    if out:
        return out
    revision = latest_shared_revision(root, "spec")
    if revision is None:
        return out
    if (revision / "prd.md").is_file():
        out.append(str(revision / "prd.md"))
    for d in sorted(revision.iterdir()):
        if d.is_dir() and d.name != "_internal" and (d / "prd.md").is_file():
            out.append(str(d / "prd.md"))
    return out


# ---------------------------------------------------------------------------
# G4 retire
# ---------------------------------------------------------------------------


def retire(root: Path, *, maps: Sequence[Path], backup_root: Path, excludes: Sequence[str],
           approval_receipt_sha256: Optional[str], dry_run: bool = False) -> Dict[str, Any]:
    root = Path(root).resolve()
    _require_active(root)
    identity = P.artifact_lifecycle.read_root_identity(root)
    rows: List[Dict[str, Any]] = []
    for m in maps:
        rows.extend(_read_jsonl(Path(m)))
    # latest map wins for a source seen more than once: later rows overwrite
    files: Dict[str, List[str]] = {}
    dirs: set = set()
    for row in rows:
        src = row["source_locator"]
        if _excluded(src, excludes):
            continue
        if row.get("kind") == "directory":
            dirs.add(src)
        else:
            files.setdefault(src, []).append(row["target_locator"])
    verified: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    absent = 0
    for src, targets in sorted(files.items()):
        path = root / src
        if os.path.islink(str(path)):
            kept.append({"source": src, "reason": "symlink"})
            continue
        if not path.exists():
            absent += 1
            continue
        if not path.is_file():
            kept.append({"source": src, "reason": "not-regular"})
            continue
        digest = _sha(path)
        match = None
        for target in targets:
            tpath = root / target
            if tpath.is_file() and not os.path.islink(str(tpath)) and _sha(tpath) == digest:
                match = target
                break
        if match is None:
            kept.append({"source": src, "reason": "no-target-with-identical-digest", "targets": targets})
            continue
        verified.append({"source": src, "target": match, "sha256": digest, "size": path.stat().st_size})
    stamp = _stamp()
    run_dir = migrations_dir(root) / f"{stamp}-retirement"
    backup_dir = Path(backup_root).resolve() / identity.artifact_root_id / stamp
    report: Dict[str, Any] = {
        "schema_version": 1, "kind": "w7c-source-retirement", "created_at": _now(), "dry_run": dry_run,
        "artifact_root": str(root), "run_dir": str(run_dir), "backup_dir": str(backup_dir),
        "approval_receipt_sha256": approval_receipt_sha256, "map_files": [str(Path(m).resolve()) for m in maps],
        "excluded_prefixes": list(excludes), "verified_files": len(verified), "kept_files": len(kept),
        "already_absent": absent, "kept": kept[:500],
    }
    if dry_run:
        report["verified_sample"] = verified[:20]
        return report
    if Path(backup_root).resolve() == root or str(Path(backup_root).resolve()).startswith(str(root) + "/"):
        raise CutoverError("backup-root-inside-artifact-root", str(backup_root))
    run_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.mkdir(parents=True, exist_ok=False)
    archive = backup_dir / "retired-sources.tar.gz"
    with tarfile.open(str(archive), "w:gz") as tar:
        for row in verified:
            tar.add(str(root / row["source"]), arcname=row["source"], recursive=False)
    with open(archive, "rb") as fh:
        os.fsync(fh.fileno())
    archive_sha = _sha(archive)
    manifest_sha = _write_jsonl(backup_dir / "retired-manifest.jsonl", verified)
    seal = {"schema_version": 1, "archive": str(archive), "archive_sha256": archive_sha, "manifest_sha256": manifest_sha,
            "file_count": len(verified), "byte_size": sum(r["size"] for r in verified), "artifact_root_id": identity.artifact_root_id,
            "created_at": _now()}
    P._write_atomic(backup_dir / "backup-seal.json", P._json_bytes(seal))
    # verify the archive before deleting anything
    with tarfile.open(str(archive), "r:gz") as tar:
        names = set(tar.getnames())
    missing = [r["source"] for r in verified if r["source"] not in names]
    if missing:
        raise CutoverError("backup-incomplete", f"{len(missing)} sources missing from archive")
    journal: List[Dict[str, Any]] = []
    for ordinal, row in enumerate(verified):
        (root / row["source"]).unlink()
        journal.append({"schema_version": "artifact-retirement-journal-row/v1", "row_ordinal": ordinal, "action": "retire_source",
                        "source_locator": row["source"], "target_locator": row["target"], "sha256": row["sha256"],
                        "backup_archive": str(archive), "commit_state": "committed"})
    # prune emptied directories: mapped source dirs plus their ancestors inside the legacy buckets
    pruned = []
    candidates = set(dirs)
    for row in verified:
        p = os.path.dirname(row["source"])
        while p:
            candidates.add(p)
            p = os.path.dirname(p)
    for d in sorted(candidates, key=lambda s: -s.count("/")):
        if _excluded(d, excludes) or d.split("/", 1)[0] in ("campaigns", "shared") or d.startswith("."):
            continue
        path = root / d
        if path.is_dir() and not os.path.islink(str(path)):
            try:
                path.rmdir()
                pruned.append(d)
            except OSError:
                pass
    report.update({"backup_seal": seal, "retired_files": len(verified), "pruned_directories": len(pruned),
                   "pruned_top_level": sorted(d for d in pruned if "/" not in d),
                   "journal_sha256": _write_jsonl(run_dir / "journal.jsonl", journal)})
    P._write_atomic(run_dir / "report.json", P._json_bytes(report))
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("migrate-delta")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--census-rows", required=True, help="jsonl rows from artifact-delta-census.py --rows-output")
    p.add_argument("--route", required=True)
    p.add_argument("--capability", default="autopilot-code")
    p.add_argument("--intensity", default="direct")
    p.add_argument("--campaign")
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--approval-receipt-sha256")
    p = sub.add_parser("adopt-campaign")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--title", default="W7 relocation campaign")
    p.add_argument("--goal", default="artifact knowledge index relocation (W7) and its W7C delta")
    p = sub.add_parser("migrate-seal")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--primary")
    p.add_argument("--spec-reference")
    p.add_argument("--analysis-reference")
    p = sub.add_parser("seal-legacy-cycle")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--cycle-dir", required=True)
    p.add_argument("--route", required=True, help="a closed route owned by the sealing session")
    p.add_argument("--capability", default="autopilot-code")
    p.add_argument("--title")
    p.add_argument("--started-on", help="RFC3339 start instant of the original transaction")
    p.add_argument("--primary")
    p.add_argument("--exclude-hidden", action="store_true",
                   help="leave files that cannot carry a D-6 locator (dot components, over-long components) out of the manifest; recorded in the cycle record")
    p = sub.add_parser("compat-close")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--map", action="append", required=True)
    p.add_argument("--approval-receipt-sha256")
    p = sub.add_parser("resolve-legacy")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--path")
    p.add_argument("--prd-candidates", action="store_true")
    p = sub.add_parser("retire")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--map", action="append", required=True)
    p.add_argument("--backup-root", required=True)
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--approval-receipt-sha256")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("route-sweep")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--jobs", action="append", default=[])
    p.add_argument("--preserve-route", action="append", default=[])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    p.add_argument("--approval-package")
    p.add_argument("--closeout-plan-sha256")
    p.add_argument("--output")
    p = sub.add_parser("closeout-residue")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--jobs", action="append", default=[])
    p.add_argument("--residue-route", required=True)
    p.add_argument("--backup-root", required=True)
    p.add_argument("--core-file", required=True)
    p.add_argument("--preserve-route", action="append", default=[])
    p.add_argument("--self-cycle", action="append", default=[])
    p.add_argument("--sync-route", default=CORE_SYNC_ROUTE_ID)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    p.add_argument("--approval-package")
    p.add_argument("--output")
    p = sub.add_parser("approval-package")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--route-plan", required=True)
    p.add_argument("--closeout-plan", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("recover-closeout-prebackup")
    p.add_argument("--artifact-root", required=True)
    p.add_argument("--journal", required=True)
    p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    root = Path(args.artifact_root)
    try:
        if args.command == "migrate-delta":
            result = migrate_delta(root, census_rows=Path(args.census_rows), route_file=Path(args.route),
                                   capability=args.capability, intensity=args.intensity, excludes=args.exclude,
                                   approval_receipt_sha256=args.approval_receipt_sha256, campaign_id=args.campaign)
        elif args.command == "adopt-campaign":
            result = adopt_campaign(root, args.campaign, title=args.title, goal=args.goal)
        elif args.command == "seal-legacy-cycle":
            result = seal_legacy_cycle(root, cycle_dir=Path(args.cycle_dir), route_file=Path(args.route),
                                       capability=args.capability, title=args.title, started_on=args.started_on,
                                       primary=args.primary, exclude_hidden=args.exclude_hidden)
        elif args.command == "migrate-seal":
            result = migrate_seal(root, run_dir=Path(args.run_dir), primary=args.primary,
                                  spec_reference=args.spec_reference, analysis_reference=args.analysis_reference)
        elif args.command == "compat-close":
            result = compat_close(root, maps=[Path(m) for m in args.map], approval_receipt_sha256=args.approval_receipt_sha256)
        elif args.command == "resolve-legacy":
            if args.prd_candidates:
                for line in prd_candidates(root):
                    print(line)
                return OK
            if not args.path:
                parser.error("--path or --prd-candidates required")
            result = resolve_legacy(root, args.path)
            print(json.dumps(result, sort_keys=True))
            return OK if result["resolution"] != "unresolved" else BLOCKED
        elif args.command == "retire":
            result = retire(root, maps=[Path(m) for m in args.map], backup_root=Path(args.backup_root),
                            excludes=args.exclude, approval_receipt_sha256=args.approval_receipt_sha256, dry_run=args.dry_run)
        elif args.command == "route-sweep":
            package = route_sweep_package(
                root,
                jobs=[Path(path) for path in args.jobs],
                preserve_routes=args.preserve_route,
            )
            if args.dry_run:
                sys.stdout.buffer.write(_emit_package(package, Path(args.output) if args.output else None))
                return OK
            if not args.approval_package or not args.closeout_plan_sha256:
                parser.error("--apply requires --approval-package and --closeout-plan-sha256")
            result = apply_route_sweep(
                root,
                package=package,
                approval_path=Path(args.approval_package),
                closeout_digest=args.closeout_plan_sha256,
            )
        elif args.command == "closeout-residue":
            kwargs = {
                "jobs": [Path(path) for path in args.jobs],
                "residue_route": Path(args.residue_route),
                "backup_root": Path(args.backup_root),
                "core_file": Path(args.core_file),
                "preserve_routes": args.preserve_route,
                "self_cycles": args.self_cycle,
                "sync_route_id": args.sync_route,
            }
            package = closeout_residue_package(root, **kwargs)
            if args.dry_run:
                sys.stdout.buffer.write(_emit_package(package, Path(args.output) if args.output else None))
                return OK
            if not args.approval_package:
                parser.error("--apply requires --approval-package")
            route_package = route_sweep_package(
                root,
                jobs=kwargs["jobs"],
                preserve_routes=kwargs["preserve_routes"],
            )
            result = apply_closeout_residue(
                root,
                closeout_package=package,
                route_package=route_package,
                approval_path=Path(args.approval_package),
                backup_root=Path(args.backup_root),
            )
        elif args.command == "approval-package":
            try:
                route_package = json.loads(Path(args.route_plan).read_text(encoding="utf-8"))
                closeout_package = json.loads(Path(args.closeout_plan).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise CutoverError("plan-package-invalid", str(exc))
            package = closeout_approval_package(
                root,
                route_package=route_package,
                closeout_package=closeout_package,
            )
            sys.stdout.buffer.write(_emit_package(package, Path(args.output)))
            return OK
        elif args.command == "recover-closeout-prebackup":
            result = recover_closeout_prebackup(
                root,
                journal_path=Path(args.journal),
                reason=args.reason,
            )
        else:  # pragma: no cover
            parser.error("unknown command")
            return 64
    except (CutoverError, P.ProducerError) as exc:
        print(json.dumps({"status": "blocked", "reason": exc.code, "detail": exc.detail}, sort_keys=True))
        return BLOCKED
    except P.artifact_admission.AdmissionRecoveryRequired as exc:
        print(json.dumps({"status": "blocked", "reason": "recovery-required", "detail": str(exc)}, sort_keys=True))
        return BLOCKED
    print(json.dumps({k: v for k, v in result.items() if k not in ("kept",)}, sort_keys=True, default=str))
    return OK


if __name__ == "__main__":
    raise SystemExit(main())
