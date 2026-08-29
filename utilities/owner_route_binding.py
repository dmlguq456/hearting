#!/usr/bin/env python3
"""Validate the node-less route binding carried by a depth-1 owner."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "capability_route_owner_binding", ROOT / "utilities" / "capability-route.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("capability-route loader unavailable")
ROUTE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ROUTE)


class OwnerRouteBindingError(ValueError):
    pass


@dataclass(frozen=True)
class OwnerRouteBinding:
    route_file: str
    route_id: str
    route_hash: str


@dataclass(frozen=True)
class QuickOwnerRouteBinding:
    route_file: str
    route_id: str
    route_hash: str
    route_node: str
    registry_digest: str
    write_scope: str
    completion_gate: str


def derive_quick_owner_binding(route_file: str | Path, *, worktree: str | Path,
                               capability: str, capability_mode: str,
                               intensity: str, harness: str) -> QuickOwnerRouteBinding:
    """Derive the complete node tuple for the quick one-shot owner."""
    path = Path(route_file).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        route = ROUTE.verify_route(raw, expected_cwd=str(Path(worktree).resolve()))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerRouteBindingError("owner-route-verification-failed") from exc
    if route.get("effective_intensity") != "quick":
        raise OwnerRouteBindingError("quick-owner-route-required")
    validate_owner_route_binding(path, worktree=worktree, capability=capability,
                                 capability_mode=capability_mode, intensity=intensity,
                                 harness=harness)
    node = next((n for n in route.get("nodes", []) if n.get("id") == "one-shot"), None)
    if not isinstance(node, dict):
        raise OwnerRouteBindingError("quick-owner-node-missing")
    scope = node.get("write_scope")
    if isinstance(scope, list):
        scope = ";".join(str(x) for x in scope)
    fields = (route.get("route_id"), route.get("route_hash"), route.get("registry_digest"),
              scope, node.get("completion_gate"))
    if not all(isinstance(x, str) and x for x in fields):
        raise OwnerRouteBindingError("quick-owner-binding-incomplete")
    return QuickOwnerRouteBinding(str(path), fields[0], fields[1], "one-shot", fields[2], fields[3], fields[4])


def _supported_owner_harnesses(route: dict) -> set[str]:
    if route.get("effective_intensity") == "quick":
        rows, field = route.get("registered_headless_candidates") or [], "harness"
    else:
        rows = (route.get("dispatch_evidence") or {}).get("tuples") or []
        field = "parent_harness"
    return {
        str(row.get(field))
        for row in rows
        if isinstance(row, dict) and row.get("status") == "supported"
    }


def validate_owner_route_binding(
    route_file: str | Path,
    *,
    worktree: str | Path,
    capability: str,
    capability_mode: str,
    intensity: str,
    harness: str,
    expected_route_id: str = "",
    expected_route_hash: str = "",
) -> OwnerRouteBinding:
    path = Path(route_file).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        route = ROUTE.verify_route(raw, expected_cwd=str(Path(worktree).resolve()))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerRouteBindingError("owner-route-verification-failed") from exc
    checks = (
        (route.get("schema_version") == 2, "owner-route-schema-invalid"),
        (route.get("capability") == capability, "owner-route-capability-mismatch"),
        (route.get("capability_mode") == capability_mode, "owner-route-mode-mismatch"),
        (route.get("effective_intensity") == intensity, "owner-route-intensity-mismatch"),
        (route.get("owner_dispatch_depth") == 1, "owner-route-depth-mismatch"),
        (harness in _supported_owner_harnesses(route), "owner-route-harness-mismatch"),
        (
            not expected_route_id or route.get("route_id") == expected_route_id,
            "owner-route-id-mismatch",
        ),
        (
            not expected_route_hash or route.get("route_hash") == expected_route_hash,
            "owner-route-hash-mismatch",
        ),
    )
    for valid, reason in checks:
        if not valid:
            raise OwnerRouteBindingError(reason)
    return OwnerRouteBinding(
        route_file=str(path),
        route_id=str(route["route_id"]),
        route_hash=str(route["route_hash"]),
    )


def binding_from_environment(
    environ: dict[str, str],
    **expected: str,
) -> OwnerRouteBinding | None:
    route_file = environ.get("AGENT_OWNER_ROUTE_FILE", "")
    route_id = environ.get("AGENT_OWNER_ROUTE_ID", "")
    route_hash = environ.get("AGENT_OWNER_ROUTE_HASH", "")
    if not route_file and not route_id and not route_hash:
        return None
    if not route_file or not route_id or not route_hash:
        raise OwnerRouteBindingError("owner-route-binding-incomplete")
    return validate_owner_route_binding(
        route_file,
        expected_route_id=route_id,
        expected_route_hash=route_hash,
        **expected,
    )


def validate_runtime_requirements(
    route: dict,
    node_id: str,
    *,
    supported: set[str] | None = None,
) -> None:
    nodes = route.get("nodes")
    node = next(
        (
            item for item in nodes or []
            if isinstance(item, dict) and item.get("id") == node_id
        ),
        None,
    )
    if node is None:
        raise OwnerRouteBindingError("route-node-unknown")
    requirements = node.get("runtime_requirements") or []
    unsupported = set(requirements).difference(supported or set())
    if "loopback-listen" in unsupported:
        raise OwnerRouteBindingError("loopback-only-unsupported")
    if unsupported:
        raise OwnerRouteBindingError("runtime-requirement-unsupported")
