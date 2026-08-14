#!/usr/bin/env python3
"""Typed dispatch mode axes shared by every adapter wrapper.

``capability_mode`` belongs to the entry capability. ``worker_mode`` is only a
compatibility projection of a non-owner portable unit.  The retired ``mode``
input is classified by shape and never wins over canonical fields.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SCALAR = re.compile(r"^[a-z][a-z0-9-]*$")
_WORKER_MODE = re.compile(r"^[a-z][a-z0-9-]*/[a-z][a-z0-9-]*$")
RESERVED_UNITS = frozenset({"_kernel/owner", "_kernel/resource"})


@dataclass(frozen=True)
class DispatchModeContractError(ValueError):
    reason: str
    fields: Mapping[str, str]

    def __str__(self) -> str:
        detail = " ".join(f"{key}={value}" for key, value in self.fields.items())
        return f"{self.reason}{(' ' + detail) if detail else ''}"


def _error(reason: str, **fields: object) -> DispatchModeContractError:
    return DispatchModeContractError(
        reason,
        {key: str(value) for key, value in fields.items() if value is not None},
    )


def capability_modes_from_info(output: str) -> tuple[str, ...]:
    for line in output.splitlines():
        if line.startswith("capability_modes="):
            values = tuple(
                value.strip()
                for value in line.split("=", 1)[1].split(",")
                if value.strip()
            )
            if values and all(_SCALAR.fullmatch(value) for value in values):
                return values
            break
    raise _error("capability-mode-contract-missing")


def validate_capability_mode(capability: str, capability_mode: str, info: str) -> None:
    allowed = capability_modes_from_info(info)
    if capability_mode not in allowed:
        raise _error(
            "invalid-dispatch-capability-mode",
            capability=capability,
            capability_mode=capability_mode,
            allowed_capability_modes=",".join(allowed),
        )


def validate_manifest_mode_axes(
    root: Path, capability: str, capability_mode: str, worker_mode: str | None
) -> None:
    """Validate portable axes for adapters without a native preflight catalog."""

    try:
        manifest = json.loads((root / "harness-manifest.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise _error("dispatch-manifest-unavailable", detail=exc)
    row = manifest.get("capabilities", {}).get(capability)
    if not isinstance(row, dict):
        raise _error("invalid-dispatch-capability", capability=capability)
    modes = row.get("modes")
    if not isinstance(modes, list):
        raise _error(
            "capability-mode-contract-missing",
            capability=capability,
        )
    if not modes:
        # A modeless capability ("Supported modes: none") is a single-mode
        # contract: the topology registry declares modes=["default"] for it
        # and compiled routes carry capability_mode="default". Interpret the
        # manifest's empty list the same way instead of rejecting dispatch.
        modes = ["default"]
    validate_capability_mode(
        capability,
        capability_mode,
        "capability_modes=" + ",".join(str(value) for value in modes),
    )
    if worker_mode and worker_mode not in manifest.get("units", {}):
        raise _error("invalid-dispatch-worker-mode", worker_mode=worker_mode)


def capability_mode_from_route_file(route_file: str | None) -> str | None:
    """Return only the sealed top-level mode needed for legacy normalization.

    Full route parsing and verification stays with the wrapper's route guard.
    A malformed or unreadable record deliberately supplies no default so the
    existing validation path can report the authoritative route failure later.
    """

    if not route_file:
        return None
    try:
        route = json.loads(Path(route_file).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    value = route.get("capability_mode")
    return value if isinstance(value, str) else None


def normalize_dispatch_modes(
    args: Any, *, default_capability_mode: str | None = None
) -> None:
    """Normalize argparse fields in place and validate the typed bootstrap tuple."""

    capability_mode = (getattr(args, "capability_mode", None) or "").strip()
    worker_mode = (getattr(args, "worker_mode", None) or "").strip()
    legacy_mode = (getattr(args, "mode", None) or "").strip()

    if legacy_mode and (capability_mode or worker_mode):
        raise _error(
            "ambiguous-dispatch-mode-input",
            mode=legacy_mode,
            capability_mode=capability_mode or "-",
            worker_mode=worker_mode or "-",
        )
    if legacy_mode:
        if "/" in legacy_mode:
            worker_mode = legacy_mode
        else:
            capability_mode = legacy_mode
    if not capability_mode and default_capability_mode:
        capability_mode = default_capability_mode

    if worker_mode and not _WORKER_MODE.fullmatch(worker_mode):
        raise _error("invalid-dispatch-worker-mode-format", worker_mode=worker_mode)

    worker_type = getattr(args, "worker_type", None)
    unit = (getattr(args, "unit", None) or "").strip()
    capability = getattr(args, "capability", None) or ""
    assigned_contract = getattr(args, "assigned_contract", None)

    if worker_type == "owner":
        if getattr(args, "dispatch_depth", 1) != 1:
            raise _error(
                "invalid-owner-dispatch-depth",
                dispatch_depth=getattr(args, "dispatch_depth", None),
            )
        if worker_mode:
            raise _error(
                "owner-worker-mode-forbidden",
                worker_mode=worker_mode,
                unit=unit or "-",
            )
        if unit and unit != "_kernel/owner":
            raise _error("invalid-owner-unit", unit=unit)
        if assigned_contract and assigned_contract != capability:
            raise _error(
                "invalid-owner-assigned-contract",
                capability=capability,
                assigned_contract=assigned_contract,
            )
        unit = "_kernel/owner"
    else:
        if unit == "_kernel/owner":
            raise _error(
                "owner-unit-worker-type-mismatch",
                worker_type=worker_type or "-",
                unit=unit,
            )
        if unit in RESERVED_UNITS:
            if worker_mode:
                raise _error(
                    "reserved-unit-worker-mode-forbidden",
                    unit=unit,
                    worker_mode=worker_mode,
                )
        elif unit:
            if not _WORKER_MODE.fullmatch(unit):
                raise _error("invalid-dispatch-unit-format", unit=unit)
            if worker_mode and worker_mode != unit:
                raise _error(
                    "worker-mode-unit-mismatch",
                    worker_mode=worker_mode,
                    unit=unit,
                )
            worker_mode = unit
        elif worker_mode:
            # Compatibility for a route-less historical invocation. Current route
            # writers always provide the unit explicitly.
            unit = worker_mode
        elif worker_type in {"stage", "review"}:
            raise _error("missing-dispatch-worker-mode", worker_type=worker_type)

    # Structural contradictions take precedence over a missing capability mode.
    # This makes the historically dangerous owner + stage-mode tuple fail with
    # the actionable reason even when supplied through the legacy slash form.
    if not capability_mode:
        raise _error("missing-dispatch-capability-mode")
    if not _SCALAR.fullmatch(capability_mode):
        raise _error(
            "invalid-dispatch-capability-mode-format",
            capability_mode=capability_mode,
        )

    args.capability_mode = capability_mode
    args.worker_mode = worker_mode or None
    args.unit = unit


def validate_route_mode_axes(args: Any, route: Mapping[str, Any]) -> None:
    route_mode = route.get("capability_mode")
    if route_mode != args.capability_mode:
        raise _error(
            "route-capability-mode-mismatch",
            capability_mode=args.capability_mode,
            route_capability_mode=route_mode,
        )
    if not getattr(args, "route_node", None):
        return
    node = next(
        (row for row in route.get("nodes", ()) if row.get("id") == args.route_node),
        None,
    )
    if node is None:
        return  # worker-route-guard owns the unknown-node failure.
    route_unit = node.get("unit") or ""
    if route_unit != (args.unit or ""):
        raise _error(
            "route-worker-unit-mismatch",
            unit=args.unit or "-",
            route_unit=route_unit or "-",
        )
