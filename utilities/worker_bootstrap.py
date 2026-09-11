#!/usr/bin/env python3
"""Render the portable minimal worker bootstrap and deterministic type overlay."""

from __future__ import annotations

import re
from pathlib import Path

WORKER_TYPES = ("owner", "stage", "review", "support", "frame")
UNIT_REF_RE = re.compile(r"^[a-z-]+/[a-z-]+$")
RESERVED_UNITS = ("_kernel/owner", "_kernel/resource")
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
WORKER_KIND_TYPES = {
    "capability-owner": "owner",
    "pipeline-stage": "stage",
    "review-worker": "review",
    "map-worker": "support",
}
REVIEW_MARKERS = (
    "review",
    "reviewer",
    "verify",
    "verifier",
    "audit",
    "adversary",
    "perspective",
    "plan-check",
)
STAGE_NODE_CONTRACT = {
    "plan": "code-plan",
    "planning": "code-plan",
    "execute": "code-execute",
    "implementation": "code-execute",
    "test": "code-test",
    "verification": "code-test",
    "report": "code-report",
    "reporting": "code-report",
}


def profile_worker_type(root: Path, profile: str | None) -> str | None:
    """Read the single scalar needed from a profile without loading its full schema."""
    if not profile:
        return None
    path = root / "profiles" / f"{profile}.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^worker_type:\s*([a-z-]+)\s*$", text, re.MULTILINE)
    return match.group(1) if match and match.group(1) in WORKER_TYPES else None


def resolve_worker_type(
    *,
    explicit: str | None,
    dispatch_depth: int,
    worker_role: str | None = None,
    route_node: str | None = None,
    profile_type: str | None = None,
) -> str:
    """Resolve one bootstrap type.

    Canonical route writers pass ``explicit`` from the topology node kind.
    ``worker_role`` remains a final legacy-reader fallback only; it is not a
    portable session-bootstrap field.
    """
    for candidate in (explicit, profile_type):
        if candidate:
            if candidate not in WORKER_TYPES:
                raise ValueError(f"invalid worker type: {candidate}")
            return candidate
    if dispatch_depth == 1:
        return "owner"
    signal = (route_node or "").lower()
    if any(marker in signal for marker in REVIEW_MARKERS):
        return "review"
    if signal:
        return "stage"
    # Compatibility for pre-worker_type commands and registry fixtures. New
    # writers must not use this branch.
    legacy_signal = (worker_role or "").lower()
    if any(marker in legacy_signal for marker in REVIEW_MARKERS):
        return "review"
    if legacy_signal:
        return "stage"
    return "support"


def worker_type_for_kind(kind: str) -> str:
    """Map portable topology kind to the one worker bootstrap overlay."""
    try:
        return WORKER_KIND_TYPES[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported headless worker kind: {kind}") from exc


def unit_persona_path(root: Path, unit: str | None) -> Path | None:
    """Resolve a route node's unit ref to its catalog persona file.

    Reserved kernel refs (`_kernel/owner`, `_kernel/resource`) carry no catalog
    persona — the owner overlay / detached lifecycle is the contract — and an
    absent unit means a pre-unit route node. Both return None. A malformed or
    dangling catalog ref fails loud: silently dropping an assigned persona
    would dispatch a bare kernel worker.
    """
    if not unit or unit in RESERVED_UNITS:
        return None
    if not UNIT_REF_RE.match(unit):
        raise ValueError(f"invalid unit ref: {unit!r}")
    path = root / "roles" / "units" / f"{unit}.md"
    if not path.is_file():
        raise ValueError(f"unknown unit: {unit} (no roles/units/{unit}.md)")
    return path


def unit_persona_body(root: Path, unit: str | None) -> str | None:
    """Return the unit BODY as plain markdown (frontmatter stripped), or None."""
    path = unit_persona_path(root, unit)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def render_worker_bootstrap(root: Path, worker_type: str, unit: str | None = None) -> str:
    """Return exactly one canonical kernel plus one type fragment.

    When the assigned route node carries a catalog ``unit``, the unit BODY from
    ``roles/units/<unit>.md`` is appended as the worker persona (kernel +
    worker-type overlay + unit body); kernel and overlay mechanics are unchanged.
    """
    if worker_type not in WORKER_TYPES:
        raise ValueError(f"invalid worker type: {worker_type}")
    paths = (
        root / "roles" / "worker-bootstrap.md",
        root / "roles" / "worker-types" / f"{worker_type}.md",
    )
    fragments = [path.read_text(encoding="utf-8").strip() for path in paths]
    persona = unit_persona_body(root, unit)
    if persona:
        fragments.append(persona)
    return "\n\n".join(fragments) + "\n"


def assigned_contract(
    *,
    capability: str,
    worker_type: str,
    route_node: str | None,
    completion_gate: str | None = None,
    explicit: str | None = None,
    root: Path | None = None,
) -> str:
    """Resolve the assigned portable contract without consulting worker role.

    A completion gate names the stage contract when that contract exists in
    the portable catalog. Otherwise the entry capability remains the readable
    contract and the immutable route node supplies the narrower assignment.
    """
    if explicit:
        return explicit
    if worker_type in {"stage", "review", "support"}:
        if completion_gate and root and (root / "capabilities" / f"{completion_gate}.md").is_file():
            return completion_gate
        if route_node and route_node.lower() in STAGE_NODE_CONTRACT:
            return STAGE_NODE_CONTRACT[route_node.lower()]
    return capability


def handoff_template() -> str:
    return (
        "artifact: <canonical path | ->\n"
        "verdict: PASS | FAIL | BLOCKED\n"
        "blocker: none | <one line>"
    )
