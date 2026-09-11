#!/usr/bin/env python3
"""Publish and verify fail-closed Hearting quiescence evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
SCHEMA_VERSION = 4
ATTRIBUTION_VERSION = 1
COUNT_KEYS = ("open_routes", "open_jobs", "open_dispatch_attempts")
RESOURCE_OPEN = {"open", "running", "pending", "working"}


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"reader-unavailable:{filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ROUTES = _load("artifact_quiescence_routes", "capability-route.py")
RESOURCES = _load("artifact_quiescence_resources", "resource_run_registry.py")
DISPATCH = _load("artifact_quiescence_dispatch", "dispatch-registry.py")
CONTRACT = _load("artifact_quiescence_contract", "dispatch_contract.py")


class SourceError(ValueError):
    def __init__(self, reason: str, diagnostics: list[dict], sources: dict | None = None):
        super().__init__(reason)
        self.diagnostics = diagnostics
        self.sources = sources or {}


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_row(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("source-not-file")
    data = resolved.read_bytes()
    return {"kind": "file", "path": str(resolved), "sha256": _digest_bytes(data), "size": len(data)}


def _directory_row(path: Path) -> dict:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("source-not-directory")
    names = sorted(item.name for item in resolved.iterdir())
    return {"kind": "directory", "path": str(resolved),
            "sha256": _digest_bytes("\n".join(names).encode())}


def _snapshot(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: (row["path"], row["kind"]))
    encoded = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    return {"files": ordered, "snapshot_sha256": _digest_bytes(encoded)}


def _absolute_path(value: object, reason: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{reason}-missing")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{reason}-not-absolute")
    return path


def _root_identity(value: object, reason: str = "artifact-root") -> dict:
    logical = _absolute_path(value, reason)
    try:
        resolved = logical.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"{reason}-not-directory")
        stat = resolved.stat()
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{reason}-unresolvable") from exc
    return {
        "kind": "artifact-root",
        "path": str(logical),
        "resolved_path": str(resolved),
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _same_root(first: dict, second: dict) -> bool:
    return (
        first.get("resolved_path") == second.get("resolved_path")
        and first.get("device") == second.get("device")
        and first.get("inode") == second.get("inode")
    )


def _target_root_snapshot(config: dict) -> dict:
    identity = _root_identity(
        config.get("artifact_root_source", config.get("artifact_root")),
        "target-artifact-root",
    )
    configured = _absolute_path(config.get("artifact_root"), "configured-artifact-root")
    if str(configured.resolve(strict=True)) != identity["resolved_path"]:
        raise ValueError("target-artifact-root-config-mismatch")
    return identity


def _sealed_route(
    route_file: object,
    *,
    expected_id: object = None,
    expected_hash: object = None,
    expected_node: object = None,
    binding: object = None,
) -> dict:
    logical = _absolute_path(route_file, "route-file")
    if logical.is_symlink():
        raise ValueError("route-file-symlink")
    try:
        file_evidence, raw = RESOURCES.read_registry_source(logical)
        if raw is None or file_evidence["kind"] != "file":
            raise ValueError("route-file-unreadable")
        resolved = Path(file_evidence["resolved_path"])
    except (OSError, RuntimeError) as exc:
        raise ValueError("route-file-unreadable") from exc
    try:
        route = RESOURCES.strict_json_loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("route-record-unreadable") from exc
    if not isinstance(route, dict) or not isinstance(route.get("nodes"), list):
        raise ValueError("route-record-invalid")
    # Attribution grants no launch authority; old registry digests may remain
    # readable, but schema, self integrity and the sealed graph must be valid.
    ROUTES.verify_route(route, allow_stale_registry=True)
    basis = route.get("validation_basis", {})
    if basis.get("basis_version", 1) > ROUTES.VALIDATION_BASIS_VERSION:
        raise ValueError("route-attribution-basis-unsupported")
    for key in ("registry_digest", "unit_catalog_digest"):
        value = route.get(key)
        if (not isinstance(value, str) or not value.startswith("sha256:")
                or len(value) != 71 or any(c not in "0123456789abcdef" for c in value[7:])):
            raise ValueError("route-digest-invalid")
    _absolute_path(route.get("cwd"), "route-cwd")
    nodes = route["nodes"]
    ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    if (not nodes or len(ids) != len(nodes) or any(not isinstance(i, str) or not i for i in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("route-graph-invalid")
    for node in nodes:
        if not node.get("kind") or not node.get("unit") or not node.get("completion_gate"):
            raise ValueError("route-node-contract-incomplete")
        if node["kind"] == "resource-runner":
            if (any(key in node for key in ("dispatch_depth", "depth", "owner_depth", "max_depth", "transport", "fallback_hops"))
                    or node.get("resource_transport") != "detached-process"
                    or (node.get("continuation") or {}).get("kind") != "supervised"):
                raise ValueError("route-resource-contract-invalid")
        elif (isinstance(node.get("dispatch_depth"), bool)
                or node.get("dispatch_depth") not in (0, 1, 2)):
            raise ValueError("route-node-contract-incomplete")
    dependencies = {}
    for node in nodes:
        deps = node.get("depends_on", [])
        if (not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps)
                or not set(deps) <= set(ids) or len(deps) != len(set(deps))):
            raise ValueError("route-dependencies-invalid")
        dependencies[node["id"]] = set(deps)
    while dependencies:
        ready = {key for key, deps in dependencies.items() if not deps}
        if not ready:
            raise ValueError("route-graph-cycle")
        dependencies = {key: deps - ready for key, deps in dependencies.items() if key not in ready}
    # Reuse the portable intrinsic workflow rules even when the historical
    # recipe cannot be re-derived from today's registry. Do not copy a subset:
    # terminal gates and nonterminal continuations are part of the same seal.
    workflow = ROUTES._workflow_contract(
        ROUTES.TOPO.load_registry(), nodes, route.get("human_gate_bindings")
    )
    if route.get("workflow_contract") != workflow:
        raise ValueError("route-workflow-contract-invalid")
    # N2: the two depth-1 bindings assert the identity of the node they name,
    # never the route's node COUNT. Quick carries three nodes now (two frame
    # legs plus `one-shot`), so a `len(nodes) != 1` assertion here would reject
    # every quick route -- and it never checked the right thing anyway.
    referenced = next(
        (node for node in nodes if node.get("id") == expected_node), None
    ) if expected_node is not None else None
    if binding == "quick-owner-route" and (
        route.get("effective_intensity") != "quick"
        or referenced is None
        or referenced.get("id") != "one-shot"
        or referenced.get("kind") != "capability-owner"
        or referenced.get("dispatch_depth") != 1
        or referenced.get("unit") != "_kernel/owner"
    ):
        raise ValueError("quick-owner-route-axis-invalid")
    if binding == "frame-route" and (
        referenced is None
        or referenced.get("dispatch_depth") != 1
        or referenced.get("worker_type") != "frame"
        or referenced.get("unit") != "plan/frame"
    ):
        raise ValueError("frame-route-axis-invalid")
    digest = route.get("route_hash")
    if not isinstance(digest, str) or digest != ROUTES.route_hash(route):
        raise ValueError("route-self-hash-mismatch")
    route_id = route.get("route_id")
    if route_id != "rt-" + digest.split(":", 1)[1][:16]:
        raise ValueError("route-id-mismatch")
    if expected_id is not None and expected_id != route_id:
        raise ValueError("route-row-id-mismatch")
    if expected_hash is not None and expected_hash != digest:
        raise ValueError("route-row-hash-mismatch")
    root = _root_identity(route.get("artifact_root"), "route-artifact-root")
    canonical = Path(root["resolved_path"]) / ".runtime" / "routes" / f"{route_id}.json"
    try:
        canonical_resolved = canonical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("route-canonical-path-missing") from exc
    if canonical.is_symlink() or resolved != canonical_resolved:
        raise ValueError("route-canonical-path-mismatch")
    if expected_node is not None:
        if not isinstance(expected_node, str) or not expected_node:
            raise ValueError("route-node-missing")
        matches = [
            node for node in route["nodes"]
            if isinstance(node, dict) and node.get("id") == expected_node
        ]
        if len(matches) != 1:
            raise ValueError("route-node-mismatch")
    file_evidence["logical_path"] = str(logical)
    return {
        "kind": "sealed-route",
        "logical_path": str(logical),
        "route_id": route_id,
        "route_hash": digest,
        "route_node": expected_node,
        "binding": binding,
        "artifact_root": root,
        "file": file_evidence,
    }


def _dispatch_route_references(metadata: dict) -> list[dict]:
    references = []
    owner_keys = ("owner_route_file", "owner_route_id", "owner_route_hash")
    owner_present = [bool(metadata.get(key)) for key in owner_keys]
    if any(owner_present):
        if not all(owner_present):
            raise ValueError("owner-route-binding-incomplete")
        if (
            str(metadata.get("dispatch_depth")) != "1"
            or metadata.get("worker_type") != "owner"
            or metadata.get("unit") != "_kernel/owner"
        ):
            raise ValueError("owner-route-binding-axis-invalid")
        references.append({
            "route_file": metadata["owner_route_file"],
            "expected_id": metadata["owner_route_id"],
            "expected_hash": metadata["owner_route_hash"],
            "expected_node": None,
            "binding": "owner-route",
        })
    route_keys = ("route_file", "route_id", "route_hash", "route_node")
    route_present = [bool(metadata.get(key)) for key in route_keys]
    if any(route_present):
        if not all(route_present):
            raise ValueError("stage-route-binding-incomplete")
        # N2: three legal axes carry a route key, not two. A depth-1 frame leg
        # (`worker_type=frame`, `unit=plan/frame`) is neither a depth-2 stage
        # nor the quick owner, so before this branch existed every frame leg --
        # at quick AND at standard+ -- died here as an axis violation.
        quick_owner = (
            str(metadata.get("dispatch_depth")) == "1"
            and metadata.get("worker_type") == "owner"
            and metadata.get("unit") == "_kernel/owner"
        )
        frame_leg = (
            str(metadata.get("dispatch_depth")) == "1"
            and metadata.get("worker_type") == "frame"
            and metadata.get("unit") == "plan/frame"
        )
        if str(metadata.get("dispatch_depth")) != "2" and not (quick_owner or frame_leg):
            raise ValueError("stage-route-binding-axis-invalid")
        if quick_owner:
            binding = "quick-owner-route"
        elif frame_leg:
            binding = "frame-route"
        else:
            binding = "stage-route"
        references.append({
            "route_file": metadata["route_file"],
            "expected_id": metadata["route_id"],
            "expected_hash": metadata["route_hash"],
            "expected_node": metadata["route_node"],
            "binding": binding,
        })
    return references


def _resource_route_references(row: dict) -> list[dict]:
    route = row.get("route")
    route_file = row.get("route_file")
    if row.get("node") and row.get("route_node") and row["node"] != row["route_node"]:
        raise ValueError("resource-route-node-conflict")
    node = row.get("node") or row.get("route_node")
    if route and route_file and route != route_file:
        raise ValueError("resource-route-path-conflict")
    selected = route_file or route
    route_present = any((selected, node, row.get("route_id"), row.get("route_hash")))
    if not route_present:
        return []
    if not selected or not node:
        raise ValueError("resource-route-binding-incomplete")
    return [{
        "route_file": selected,
        "expected_id": row.get("route_id"),
        "expected_hash": row.get("route_hash"),
        "expected_node": node,
        "binding": "resource-route",
    }]


def _item_identity(kind: str, row: dict) -> dict:
    if kind == "dispatch":
        return {
            "attempt_id": row.get("meta", {}).get("attempt_id"),
            "slug": row.get("slug"),
        }
    return {"run_id": row.get("run_id"), "registry_path": row.get("registry_path")}


def _attribute_open_item(kind: str, row: dict, target: dict) -> dict:
    if row.get("_attribution_error"):
        raise ValueError(row["_attribution_error"])
    values = row.get("meta", {}) if kind == "dispatch" else row
    if kind == "dispatch" and not values.get("artifact_root"):
        raise ValueError("dispatch-artifact-root-required")
    explicit = None
    if values.get("artifact_root"):
        explicit = _root_identity(values["artifact_root"], "row-artifact-root")
    references = (
        _dispatch_route_references(values)
        if kind == "dispatch" else _resource_route_references(values)
    )
    route_proofs = [_sealed_route(**reference) for reference in references]
    route_root = route_proofs[0]["artifact_root"] if route_proofs else None
    for proof in route_proofs[1:]:
        if (
            proof["route_id"] != route_proofs[0]["route_id"]
            or proof["route_hash"] != route_proofs[0]["route_hash"]
            or proof["file"]["path"] != route_proofs[0]["file"]["path"]
            or not _same_root(proof["artifact_root"], route_root)
        ):
            raise ValueError("route-binding-conflict")
    if explicit is not None and route_root is not None and not _same_root(explicit, route_root):
        raise ValueError("artifact-root-route-conflict")
    attributed_root = explicit or route_root
    if attributed_root is None:
        raise ValueError("artifact-root-attribution-missing")
    decision = "target" if _same_root(attributed_root, target) else "external"
    return {
        "kind": kind,
        "open": True,
        "decision": decision,
        "basis": (
            "explicit-root+sealed-route" if explicit is not None and route_root is not None
            else "explicit-root" if explicit is not None else "sealed-route"
        ),
        "identity": _item_identity(kind, row),
        "artifact_root": attributed_root,
        "explicit_root": explicit,
        "routes": route_proofs,
    }


def _resource_is_open(row: dict) -> bool:
    return (
        row.get("liveness") == "working"
        or str(row.get("registry_status", "")).lower() in RESOURCE_OPEN
    )


def _attribution_snapshot(config: dict, resource_rows: list[dict], dispatch_rows: list[dict]) -> dict:
    target = _target_root_snapshot(config)
    rows = []
    summary = {
        decision: {"dispatch": 0, "resource": 0}
        for decision in ("target", "external", "unattributable", "historical")
    }
    for kind, values in (("dispatch", dispatch_rows), ("resource", resource_rows)):
        for row in values:
            opened = row["status"] in DISPATCH.OPEN if kind == "dispatch" else _resource_is_open(row)
            if not opened:
                result = {
                    "kind": kind,
                    "open": False,
                    "decision": "historical",
                    "basis": "terminal-row-not-attributed",
                    "identity": _item_identity(kind, row),
                }
            else:
                try:
                    result = _attribute_open_item(kind, row, target)
                except Exception as exc:
                    result = {
                        "kind": kind,
                        "open": True,
                        "decision": "unattributable",
                        "basis": "fail-closed",
                        "identity": _item_identity(kind, row),
                        "reason": str(exc).replace("\n", " ")[:160],
                    }
            summary[result["decision"]][kind] += 1
            rows.append(result)
    rows.sort(key=lambda row: (
        row["kind"],
        json.dumps(row.get("identity", {}), sort_keys=True, separators=(",", ":")),
    ))
    body = {
        "attribution_version": ATTRIBUTION_VERSION,
        "target_root": target,
        "summary": summary,
        "rows": rows,
    }
    body["snapshot_sha256"] = _digest_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    )
    return body


def _route_dirs(artifact_root: Path) -> list[Path]:
    return [artifact_root / ".runtime" / "routes", artifact_root,
            artifact_root / "routes", artifact_root / "_routes", artifact_root / ".routes"]


def _route_snapshot(artifact_root: Path) -> dict:
    rows = []
    for directory in _route_dirs(artifact_root):
        if directory.exists():
            rows.append(_directory_row(directory))
            rows.extend(_file_row(path) for path in sorted(directory.glob("*.json")))
    if not rows or not (artifact_root / ".runtime" / "routes").is_dir():
        raise ValueError("canonical-route-source-missing")
    return _snapshot(rows)


def _resource_snapshot(index_path: Path) -> dict:
    if not index_path.is_file():
        raise ValueError("resource-index-missing")
    paths, diagnostics = RESOURCES.indexed_paths(index_path)
    if diagnostics:
        raise ValueError("resource-index-unverifiable")
    rows = [_file_row(index_path)]
    for path in paths:
        try:
            source, _ = RESOURCES.read_registry_source(path)
            rows.append(source)
        except Exception as exc:
            raise SourceError("resource-source-unverifiable", [
                {"path": str(path), "kind": "unverifiable-registry", "error": str(exc)}]) from exc
    return _snapshot(rows)


def _dispatch_snapshot(jobs: Path) -> dict:
    if not jobs.is_file():
        raise ValueError("dispatch-registry-missing")
    return _snapshot([_file_row(jobs)])


def _lock_probe(lock_path: Path) -> dict:
    """Observe ownership without treating the persistent lock file as state."""
    resolved = lock_path.expanduser().resolve(strict=False)
    if not resolved.exists():
        return {"kind": "lock", "path": str(resolved), "present": False,
                "held": False, "owner_state": "absent"}
    if not resolved.is_file():
        raise ValueError("lock-source-not-file")
    try:
        fd = os.open(resolved, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("lock-source-unreadable") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = False
        except BlockingIOError:
            held = True
        content = os.pread(fd, 4096, 0).decode("utf-8", errors="strict")
        if "\x00" in content or "\n\n" in content:
            raise ValueError("lock-owner-malformed")
        return {"kind": "lock", "path": str(resolved), "present": held,
                "held": held, "owner_state": "held" if held else
                ("stale-owner" if content else "empty-unlocked")}
    except UnicodeError as exc:
        raise ValueError("lock-owner-malformed") from exc
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _authority_roots(paths) -> list[str]:
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError("observation-authority-roots-missing")
    result = set()
    for value in paths:
        if not isinstance(value, (str, Path)):
            raise ValueError("observation-authority-root-invalid")
        path = Path(value).expanduser()
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("observation-authority-root-invalid")
        result.add(str(path))
    return sorted(result)


def _authority_snapshot(config: dict) -> dict:
    """Observation only: never select a new launch registry or merge ledgers.

    The additive v1 authority seal is mandatory when revalidating schema-3
    evidence. Older evidence without it fails source comparison/config checks.
    Missing exploratory candidates are negative facts, not lost authorities.
    """
    roots = _authority_roots(config.get("authority_roots"))
    selected = {"dispatch": config["dispatch_jobs"], "resource": config["resource_index"]}
    rows = []
    divergence = []
    for role, basename in (("dispatch", "jobs.log"), ("resource", "resource-runs.index.json")):
        paths = sorted({str(Path(root) / basename) for root in roots} | {selected[role]})
        identities = {}
        for path in paths:
            try:
                source, _ = RESOURCES.read_registry_source(Path(path))
            except Exception as exc:
                raise SourceError("observation-authority-unverifiable", [
                    {"kind": "authority-unverifiable", "ledger": role,
                     "path": path, "error": str(exc)}],
                    {"authority": {"roots_read": roots, "files": rows}}) from exc
            source = {**source, "ledger": role}
            rows.append(source)
            if source["kind"] == "missing":
                source["reason"] = "authority-candidate-absent"
                if path == selected[role]:
                    raise SourceError("observation-authority-source-missing", [
                        {"kind": "selected-authority-missing", "ledger": role, "path": path}],
                        {"authority": {"roots_read": roots, "files": rows}})
            else:
                identity = (source["device"], source["inode"])
                identities.setdefault(identity, set()).add(source["resolved_path"])
        if len(identities) > 1:
            divergence.append({"kind": "authority-mismatch", "ledger": role,
                               "paths": sorted({p for paths in identities.values() for p in paths})})
    snapshot = {"authority_version": 1, "scope": config["scope"], "roots_read": roots,
                "selected_sources": selected,
                "authority_reason": "multiple-physical-authorities" if divergence else "single-physical-authority",
                **_snapshot(rows)}
    if divergence:
        raise SourceError("observation-authority-mismatch", divergence, {"authority": snapshot})
    return snapshot


def _source_snapshots(config: dict) -> dict:
    return {
        "authority": _authority_snapshot(config),
        "target_root": _target_root_snapshot(config),
        "routes": _route_snapshot(Path(config["artifact_root"])),
        "jobs": _resource_snapshot(Path(config["resource_index"])),
        "dispatch": _dispatch_snapshot(Path(config["dispatch_jobs"])),
        "lock": _lock_probe(Path(config["lock_path"])),
    }


def _dispatch_rows(jobs: Path) -> list[dict]:
    raw_lines = [line for line in jobs.read_text(encoding="utf-8", errors="strict").splitlines() if line]
    if any(len(line.split("\t")) != 6 for line in raw_lines):
        raise ValueError("dispatch-registry-malformed")
    rows = DISPATCH.read_rows(jobs)
    if len(rows) != len(raw_lines):
        raise ValueError("dispatch-registry-unverifiable")
    identity_keys = {"artifact_root", "attempt_id", "dispatch_depth", "worker_type", "unit",
                     "subsession_id", "route_file", "route_id", "route_hash", "route_node",
                     "owner_route_file", "owner_route_id", "owner_route_hash"}
    ambiguous = []
    ordinary = []
    for row in rows:
        keys = [part.split("=", 1)[0] for part in row["pipe"].split(",") if "=" in part]
        duplicates = sorted(key for key in identity_keys if keys.count(key) > 1)
        if duplicates:
            row["_attribution_error"] = "dispatch-duplicate-identity:" + ",".join(duplicates)
            # Never let an ambiguous fold key erase an open attempt.
            ambiguous.append(row)
        else:
            ordinary.append(row)
    current = DISPATCH.current(ordinary) + ambiguous
    for row in current:
        if row["status"] in DISPATCH.OPEN and row.get("attempt_contract_status") != "current":
            raise ValueError("open-dispatch-contract-unverifiable")
    return current


def _observed_at(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("observation-time-offset-missing")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def collect(config: dict, now: datetime | None = None) -> dict:
    before = _source_snapshots(config)
    route_diagnostics: list[dict] = []
    route_rows = ROUTES.route_status(config["artifact_root"], diagnostics=route_diagnostics)
    canonical_routes = Path(config["artifact_root"]) / ".runtime" / "routes"
    blocking_route_diagnostics = [
        row for row in route_diagnostics
        if row.get("blocking", True)
        and (Path(row.get("path", "")).parent == canonical_routes
             or "route" in Path(row.get("path", "")).name.lower())
    ]
    scanned_sources: list[dict] = []
    resource_rows, resource_diagnostics = RESOURCES.scan(
        index_path=config["resource_index"], observed_sources=scanned_sources)
    if any(row.get("kind") != "missing-registry" for row in resource_diagnostics):
        raise SourceError("resource-source-unverifiable", resource_diagnostics)
    scanned = _snapshot([_file_row(Path(config["resource_index"])), *scanned_sources])
    if scanned != before["jobs"]:
        raise ValueError("source-changed-during-observation")
    dispatch_rows = _dispatch_rows(Path(config["dispatch_jobs"]))
    attribution_before = _attribution_snapshot(config, resource_rows, dispatch_rows)
    after = _source_snapshots(config)
    attribution_after = _attribution_snapshot(config, resource_rows, dispatch_rows)
    if before != after or attribution_before != attribution_after:
        raise ValueError("source-changed-during-observation")

    open_route_ids = {row.get("route_id") for row in route_rows if not row.get("closed")}
    open_route_ids.discard(None)
    attribution_summary = attribution_after["summary"]
    open_jobs = attribution_summary["target"]["resource"]
    counts = {
        "open_routes": len(open_route_ids),
        "open_jobs": open_jobs,
        "open_dispatch_attempts": attribution_summary["target"]["dispatch"],
    }
    unattributable = sum(attribution_summary["unattributable"].values())
    lock_present = bool(after["lock"].get("held"))
    stamp = _observed_at(now)
    sources = {
        "authority": after["authority"],
        "routes": {"reader": "capability-route.py:route_status", "count": counts["open_routes"], **after["routes"]},
        "jobs": {"reader": "resource_run_registry.py:scan", "count": counts["open_jobs"],
                 "global_open_count": sum(_resource_is_open(row) for row in resource_rows),
                 "diagnostics": resource_diagnostics, **after["jobs"]},
        "dispatch": {"reader": "dispatch-registry.py:current(read_rows)",
                     "count": counts["open_dispatch_attempts"],
                     "global_open_count": sum(row["status"] in DISPATCH.OPEN for row in dispatch_rows),
                     **after["dispatch"]},
        "lock": {"reader": "flock(LOCK_EX|LOCK_NB)", "count": int(lock_present), **after["lock"]},
        "attribution": attribution_after,
    }
    pending = sum(counts.values()) + int(lock_present) + unattributable
    observation_valid = not blocking_route_diagnostics and unattributable == 0
    identity_seed = json.dumps({"observed_at": stamp, "sources": sources,
                                "nonce": uuid.uuid4().hex}, sort_keys=True).encode()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "observation_valid": observation_valid,
        "observation_id": _digest_bytes(identity_seed),
        "scope": config["scope"],
        "observed_at": stamp,
        **counts,
        "unattributable_open_items": unattributable,
        "lock_present": lock_present,
        "pending": pending,
        "proven": observation_valid and pending == 0,
        "config": config,
        "sources": sources,
    }
    if blocking_route_diagnostics or unattributable:
        payload["reason"] = (
            "route-source-unverifiable" if blocking_route_diagnostics
            else "open-item-unattributable"
        )
        payload["source_diagnostics"] = [
            {"path": row.get("path"), "reason": row.get("reason")}
            for row in blocking_route_diagnostics
        ]
        payload["source_diagnostics"].extend(
            {"kind": row["kind"], "identity": row["identity"], "reason": row.get("reason")}
            for row in attribution_after["rows"]
            if row["decision"] == "unattributable"
        )
    return payload


def _atomic(path: Path, payload: dict) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def fixture_config(artifact_root: str, resource_index: str, dispatch_jobs: str) -> dict:
    artifact_source = str(Path(artifact_root).expanduser().absolute())
    return {
        "scope": "fixture",
        "authority_roots": _authority_roots([Path(resource_index).expanduser().absolute().parent,
                                              Path(dispatch_jobs).expanduser().absolute().parent]),
        "artifact_root": str(Path(artifact_source).resolve(strict=False)),
        "artifact_root_source": artifact_source,
        "resource_index": str(Path(resource_index).expanduser().resolve(strict=False)),
        "dispatch_jobs": str(Path(dispatch_jobs).expanduser().resolve(strict=False)),
        "lock_path": str((Path(artifact_root) / ".pipeline-lock").expanduser().resolve(strict=False)),
    }


def live_config(cwd: str | None = None) -> dict:
    target_cwd = Path(cwd or os.getcwd()).expanduser().resolve(strict=True)
    if not target_cwd.is_dir():
        raise ValueError("observation-cwd-not-directory")
    resolver_env = dict(os.environ)
    resolver_env.pop("AGENT_ARTIFACT_ROOT", None)
    artifact_root = subprocess.check_output(
        [str(ROOT / "utilities" / "artifact-root.sh"), str(target_cwd)], text=True,
        stderr=subprocess.DEVNULL, env=resolver_env,
    ).strip()
    root_identity = _root_identity(artifact_root, "target-artifact-root")
    jobs = os.environ.get("AGENT_DISPATCH_JOBS")
    if not jobs:
        raise ValueError("canonical-dispatch-registry-unavailable")
    index = RESOURCES.default_index_path()
    user_home = Path(os.environ.get("HOME", str(Path.home())))
    roots = [*CONTRACT.dispatch_state_roots(RESOURCES.agent_home(), jobs, environ=os.environ),
             user_home / ".codex" / ".harness" / "dispatch",
             Path(os.environ.get("CODEX_HOME", str(user_home / ".codex"))) / ".harness" / "dispatch",
             Path(jobs).expanduser().absolute().parent, index.parent]
    return {
        "scope": "live",
        "authority_roots": _authority_roots(roots),
        "artifact_root": root_identity["resolved_path"],
        "artifact_root_source": str(_absolute_path(artifact_root, "target-artifact-root")),
        "cwd": str(target_cwd),
        "resource_index": str(index),
        "dispatch_jobs": str(Path(jobs).expanduser().resolve(strict=False)),
        "lock_path": str((Path(artifact_root) / ".pipeline-lock").resolve(strict=False)),
    }


def publish(output: str, config: dict, now: datetime | None = None) -> dict:
    try:
        payload = collect(config, now)
    except Exception as exc:
        stamp = _observed_at(now)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "observation_valid": False,
            "observation_id": _digest_bytes(f"{stamp}:{uuid.uuid4().hex}".encode()),
            "scope": config.get("scope", "unknown"),
            "observed_at": stamp,
            "open_routes": 0,
            "open_jobs": 0,
            "open_dispatch_attempts": 0,
            "unattributable_open_items": 0,
            "lock_present": False,
            "pending": 0,
            "proven": False,
            "config": config,
            "sources": {},
            "reason": str(exc).replace("\n", " ")[:160],
        }
        if isinstance(exc, SourceError):
            payload["source_diagnostics"] = exc.diagnostics
            payload["sources"] = exc.sources
    _atomic(Path(output), payload)
    return payload


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp-missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp-offset-missing")
    return parsed.astimezone(timezone.utc)


def validate_integrity(path: str, max_age: int = 300, now: datetime | None = None,
                       allow_fixture: bool = False) -> dict:
    result = {"valid": False, "evidence": str(Path(path).expanduser().resolve(strict=False))}
    reasons: list[str] = []
    try:
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
            raise ValueError("max-age-invalid")
        evidence_source, evidence_bytes = RESOURCES.read_registry_source(Path(path))
        if evidence_bytes is None:
            raise ValueError("evidence-missing")
        payload = RESOURCES.strict_json_loads(evidence_bytes)
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("evidence-schema-invalid")
        if payload.get("observation_valid") is not True:
            raise ValueError("observation-invalid")
        scope = payload.get("scope")
        if scope not in {"live", "fixture"} or (scope == "fixture" and not allow_fixture):
            raise ValueError("evidence-scope-not-authorized")
        observed = _time(payload.get("observed_at"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if observed > current + timedelta(seconds=30):
            raise ValueError("evidence-from-future")
        if current - observed > timedelta(seconds=max_age):
            raise ValueError("evidence-stale")
        for key in COUNT_KEYS:
            value = payload.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("count-invalid")
        unattributable = payload.get("unattributable_open_items")
        if not isinstance(unattributable, int) or isinstance(unattributable, bool) or unattributable < 0:
            raise ValueError("unattributable-count-invalid")
        expected_pending = (
            sum(payload[key] for key in COUNT_KEYS)
            + int(payload.get("lock_present") is True)
            + unattributable
        )
        if (not isinstance(payload.get("pending"), int) or isinstance(payload.get("pending"), bool)
                or payload.get("pending") != expected_pending):
            raise ValueError("pending-sum-invalid")
        if not isinstance(payload.get("lock_present"), bool):
            raise ValueError("lock-state-invalid")
        if payload.get("proven") is not (
            payload.get("observation_valid") is True and payload["pending"] == 0
        ):
            raise ValueError("published-proof-invalid")
        config = payload.get("config")
        if not isinstance(config, dict) or config.get("scope") != scope:
            raise ValueError("source-config-missing")
        if scope == "live" and config != live_config(config.get("cwd")):
            raise ValueError("live-source-config-changed")
        current_payload = collect(config, current)
        if any(current_payload[key] != payload[key] for key in (
            *COUNT_KEYS, "unattributable_open_items", "lock_present", "pending"
        )):
            raise ValueError("source-count-changed")
        if current_payload["sources"] != payload.get("sources"):
            raise ValueError("source-evidence-changed")
        if current_payload.get("observation_valid") is not True:
            raise ValueError("current-observation-invalid")
        result.update({"valid": True, "payload": payload,
                       "sha256": evidence_source["sha256"]})
    except Exception as exc:
        reasons.append(str(exc).replace("\n", " ")[:160])
    if reasons:
        result["reasons"] = reasons
    return result


def validate(path: str, max_age: int = 300, now: datetime | None = None,
             allow_fixture: bool = False) -> dict:
    integrity = validate_integrity(path, max_age, now, allow_fixture)
    result = {"proven": False, "evidence": integrity["evidence"]}
    if not integrity["valid"]:
        result["reasons"] = integrity["reasons"]
        return result
    payload = integrity["payload"]
    if payload["pending"] != 0:
        result["reasons"] = ["pending-work-nonzero"]
        return result
    result.update({"proven": True, "pending": 0, "scope": payload["scope"],
                   "observation_id": payload.get("observation_id")})
    return result


def pair(first: str, second: str, fold_start: str, fold_end: str, max_age: int = 300,
         now: datetime | None = None, allow_fixture: bool = False) -> dict:
    current = now or datetime.now(timezone.utc)
    first_result = validate(first, max_age, current, allow_fixture)
    second_result = validate(second, max_age, current, allow_fixture)
    result = {"proven": False, "first": first_result, "second": second_result,
              "fold_start": fold_start, "fold_end": fold_end}
    reasons = []
    try:
        first_path = Path(first).expanduser().resolve(strict=True)
        second_path = Path(second).expanduser().resolve(strict=True)
        if first_path == second_path:
            raise ValueError("samples-not-distinct")
        first_payload = json.loads(first_path.read_text(encoding="utf-8"))
        second_payload = json.loads(second_path.read_text(encoding="utf-8"))
        before = _time(first_payload.get("observed_at"))
        after = _time(second_payload.get("observed_at"))
        start = _time(fold_start)
        end = _time(fold_end)
        if first_payload.get("observation_id") == second_payload.get("observation_id") or not before < after:
            raise ValueError("samples-not-independent")
        if first_payload.get("config") != second_payload.get("config"):
            raise ValueError("sample-source-config-mismatch")
        if not before <= start <= end <= after:
            raise ValueError("fold-not-bracketed")
        if not first_result.get("proven") or not second_result.get("proven"):
            raise ValueError("sample-not-proven")
        result["proven"] = True
    except Exception as exc:
        reasons.append(str(exc).replace("\n", " ")[:160])
    if reasons:
        result["reasons"] = reasons
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--output", required=True)
    observe.add_argument("--cwd")
    observe.add_argument("--fixture", action="store_true")
    observe.add_argument("--artifact-root")
    observe.add_argument("--resource-index")
    observe.add_argument("--dispatch-jobs")
    check = sub.add_parser("validate")
    check.add_argument("evidence")
    check.add_argument("--max-age", type=int, default=300)
    check.add_argument("--allow-fixture", action="store_true")
    fold = sub.add_parser("pair")
    fold.add_argument("first")
    fold.add_argument("second")
    fold.add_argument("--fold-start", required=True)
    fold.add_argument("--fold-end", required=True)
    fold.add_argument("--max-age", type=int, default=300)
    fold.add_argument("--allow-fixture", action="store_true")
    args = parser.parse_args()

    if args.operation == "observe":
        fixture_values = (args.artifact_root, args.resource_index, args.dispatch_jobs)
        if args.fixture:
            if not all(fixture_values):
                parser.error("fixture observation requires all three source paths")
            config = fixture_config(*fixture_values)
        else:
            if any(fixture_values):
                parser.error("live observation does not accept source overrides")
            config = live_config(args.cwd)
        result = publish(args.output, config)
    elif args.operation == "validate":
        result = validate(args.evidence, args.max_age, allow_fixture=args.allow_fixture)
    else:
        result = pair(args.first, args.second, args.fold_start, args.fold_end,
                      args.max_age, allow_fixture=args.allow_fixture)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("proven") else 1


if __name__ == "__main__":
    raise SystemExit(main())
