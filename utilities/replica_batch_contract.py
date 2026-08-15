#!/usr/bin/env python3
"""Canonical immutable identity for one bounded parallel dispatch batch.

Schema v3 seals 2..4 asymmetric legs with the leg_class/auxiliary_check split.
Schema v2 remains verify-only for the migration window; schema v1 is verify-only
for the one-window fixed two-way migration. New manifests are always v3.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SUPPORTED_HARNESSES = frozenset({"claude", "codex", "opencode"})
SUPPORTED_INDEPENDENCE = frozenset({"cross-harness", "degraded-same-harness"})
SUPPORTED_AXES = frozenset({"cross-harness", "model-profile", "perspective"})
SUPPORTED_PROFILES = frozenset({"deep", "balanced-deep", "light"})
MIN_WIDTH = 2
MAX_WIDTH = 4


class ReplicaBatchContractError(ValueError):
    """Compatibility name for the portable parallel-batch contract error."""


def _digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def build_manifest(
    *,
    parallel_group: str | None = None,
    replica_group: str | None = None,
    route_id: str,
    parent_attempt_id: str,
    independence: str,
    members: list[dict[str, object]],
    required_independence_axes: list[str] | None = None,
    realized_independence_axes: list[str] | None = None,
    degradation_reason: str = "",
) -> tuple[dict[str, object], str, dict[str, str]]:
    """Validate and seal the full declared N-way group plus exact leg bindings."""

    group = parallel_group or replica_group
    if parallel_group and replica_group and parallel_group != replica_group:
        raise ReplicaBatchContractError("parallel/replica group aliases differ")
    if any(not isinstance(value, str) or not value for value in (
        group, route_id, parent_attempt_id, independence
    )):
        raise ReplicaBatchContractError("parallel batch identity must be nonempty")
    if independence not in SUPPORTED_INDEPENDENCE:
        raise ReplicaBatchContractError("unsupported parallel batch independence")
    if not isinstance(members, list) or not MIN_WIDTH <= len(members) <= MAX_WIDTH:
        raise ReplicaBatchContractError("parallel batch must declare 2..4 members")

    required_axes = required_independence_axes or ["cross-harness"]
    realized_axes = realized_independence_axes or (
        ["cross-harness"] if independence == "cross-harness" else []
    )
    for label, axes in (("required", required_axes), ("realized", realized_axes)):
        if (not isinstance(axes, list) or len(axes) != len(set(axes))
                or not set(axes) <= SUPPORTED_AXES):
            raise ReplicaBatchContractError(f"invalid {label} independence axes")
    if "cross-harness" not in required_axes:
        raise ReplicaBatchContractError("parallel batch requires cross-harness intent")

    required_member = {
        "assignment_sha256", "attempt_id", "route_node", "harness",
        "fallback_hop", "fallback_ordinal", "model_profile", "perspective",
        "parallel_leg_index", "leg_class",
    }
    normalized: list[dict[str, object]] = []
    for raw in members:
        if not isinstance(raw, dict):
            raise ReplicaBatchContractError("invalid parallel batch member shape")
        if "leg_class" not in raw or raw.get("leg_class") not in ("peer", "auxiliary"):
            raise ReplicaBatchContractError("invalid parallel batch leg_class")
        leg_class = raw["leg_class"]
        expected_shape = (
            required_member | {"auxiliary_check"}
            if leg_class == "auxiliary" else required_member
        )
        if set(raw) != expected_shape:
            raise ReplicaBatchContractError("invalid parallel batch member shape")
        for key in (
            "assignment_sha256", "attempt_id", "route_node", "harness",
            "fallback_hop", "model_profile", "perspective",
        ):
            if not isinstance(raw[key], str) or not raw[key]:
                raise ReplicaBatchContractError("invalid parallel batch member identity")
        if not DIGEST.fullmatch(str(raw["assignment_sha256"])):
            raise ReplicaBatchContractError("invalid parallel assignment digest")
        ordinal = raw["fallback_ordinal"]
        leg_index = raw["parallel_leg_index"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise ReplicaBatchContractError("invalid parallel fallback ordinal")
        if isinstance(leg_index, bool) or not isinstance(leg_index, int) or leg_index < 0:
            raise ReplicaBatchContractError("invalid parallel leg index")
        if raw["harness"] not in SUPPORTED_HARNESSES:
            raise ReplicaBatchContractError("unsupported parallel batch harness")
        if raw["model_profile"] not in SUPPORTED_PROFILES:
            raise ReplicaBatchContractError("unsupported substantive model profile")
        if leg_class == "auxiliary":
            if not isinstance(raw["auxiliary_check"], str) or not raw["auxiliary_check"]:
                raise ReplicaBatchContractError("auxiliary member requires auxiliary_check")
        normalized.append({key: raw[key] for key in sorted(expected_shape)})

    size = len(normalized)
    if len({str(member["attempt_id"]) for member in normalized}) != size:
        raise ReplicaBatchContractError("parallel batch attempts must be distinct")
    if len({str(member["route_node"]) for member in normalized}) != size:
        raise ReplicaBatchContractError("parallel batch nodes must be distinct")
    if sorted(int(member["parallel_leg_index"]) for member in normalized) != list(range(size)):
        raise ReplicaBatchContractError("parallel batch leg indexes must be exact 0..N-1")

    harness_count = len({str(member["harness"]) for member in normalized})
    profile_count = len({str(member["model_profile"]) for member in normalized})
    perspective_count = len({str(member["perspective"]) for member in normalized})
    derived_axes = set()
    if harness_count >= 2:
        derived_axes.add("cross-harness")
    if profile_count >= 2:
        derived_axes.add("model-profile")
    if perspective_count == size:
        derived_axes.add("perspective")
    if set(realized_axes) != derived_axes:
        raise ReplicaBatchContractError("realized independence axes differ from member evidence")
    if independence == "cross-harness":
        if harness_count < 2 or degradation_reason:
            raise ReplicaBatchContractError("cross-harness batch requires 2+ harnesses and no degradation")
    elif harness_count != 1 or not degradation_reason:
        raise ReplicaBatchContractError("degraded batch requires one harness and a reason")

    normalized.sort(key=lambda member: int(member["parallel_leg_index"]))
    common = {
        "schema_version": 3,
        "kind": "parallel-batch",
        "declared_size": size,
        "parallel_group": group,
        "replica_group": group,
        "route_id": route_id,
        "parent_attempt_id": parent_attempt_id,
        "independence": independence,
        "required_independence_axes": list(required_axes),
        "realized_independence_axes": list(realized_axes),
        "degradation_reason": degradation_reason,
    }
    manifest = {**common, "members": normalized}
    manifest_digest = _digest(manifest)
    leg_digests = {
        str(member["attempt_id"]): _digest({**common, "member": member})
        for member in normalized
    }
    return manifest, manifest_digest, leg_digests


def _verify_v1(manifest: dict[str, object]):
    expected_keys = {
        "schema_version", "kind", "declared_size", "replica_group", "route_id",
        "parent_attempt_id", "independence", "members",
    }
    if set(manifest) != expected_keys or manifest.get("kind") != "replica-batch" or manifest.get("declared_size") != 2:
        raise ReplicaBatchContractError("unsupported legacy replica batch manifest")
    required = {
        "assignment_sha256", "attempt_id", "route_node", "harness",
        "fallback_hop", "fallback_ordinal",
    }
    members = manifest.get("members")
    if not isinstance(members, list) or len(members) != 2:
        raise ReplicaBatchContractError("legacy replica batch must have two members")
    for member in members:
        if not isinstance(member, dict) or set(member) != required:
            raise ReplicaBatchContractError("invalid legacy replica member")
    canonical = json.loads(json.dumps(manifest))
    canonical["members"] = sorted(
        canonical["members"], key=lambda member: (member["route_node"], member["attempt_id"])
    )
    if canonical != manifest:
        raise ReplicaBatchContractError("legacy replica batch manifest is not canonical")
    common = {key: manifest[key] for key in expected_keys if key != "members"}
    return manifest, _digest(manifest), {
        str(member["attempt_id"]): _digest({**common, "member": member})
        for member in members
    }


def _verify_v2(manifest: dict[str, object]):
    """Verify-only read path for schema-v2 manifests in the migration window."""
    expected_keys = {
        "schema_version", "kind", "declared_size", "parallel_group",
        "replica_group", "route_id", "parent_attempt_id", "independence",
        "required_independence_axes", "realized_independence_axes",
        "degradation_reason", "members",
    }
    if (set(manifest) != expected_keys or manifest.get("schema_version") != 2
            or manifest.get("kind") != "parallel-batch"):
        raise ReplicaBatchContractError("invalid v2 parallel batch manifest shape")
    required = {
        "assignment_sha256", "attempt_id", "route_node", "harness",
        "fallback_hop", "fallback_ordinal", "model_profile", "perspective",
        "parallel_leg_index",
    }
    members = manifest.get("members")
    if not isinstance(members, list) or not MIN_WIDTH <= len(members) <= MAX_WIDTH:
        raise ReplicaBatchContractError("v2 parallel batch must declare 2..4 members")
    for member in members:
        if not isinstance(member, dict) or set(member) != required:
            raise ReplicaBatchContractError("invalid v2 parallel batch member")
    return manifest, _digest(manifest), {
        str(member["attempt_id"]): _digest({**{k: v for k, v in manifest.items() if k != "members"}, "member": member})
        for member in members
    }


def verify_manifest(manifest: object) -> tuple[dict[str, object], str, dict[str, str]]:
    if not isinstance(manifest, dict):
        raise ReplicaBatchContractError("parallel batch manifest must be an object")
    if manifest.get("schema_version") == 1:
        return _verify_v1(manifest)
    if manifest.get("schema_version") == 2:
        return _verify_v2(manifest)
    expected_keys = {
        "schema_version", "kind", "declared_size", "parallel_group",
        "replica_group", "route_id", "parent_attempt_id", "independence",
        "required_independence_axes", "realized_independence_axes",
        "degradation_reason", "members",
    }
    if set(manifest) != expected_keys or manifest.get("schema_version") != 3 or manifest.get("kind") != "parallel-batch":
        raise ReplicaBatchContractError("invalid parallel batch manifest shape")
    rebuilt, digest, legs = build_manifest(
        parallel_group=manifest.get("parallel_group"),
        replica_group=manifest.get("replica_group"),
        route_id=manifest.get("route_id"),
        parent_attempt_id=manifest.get("parent_attempt_id"),
        independence=manifest.get("independence"),
        members=manifest.get("members"),
        required_independence_axes=manifest.get("required_independence_axes"),
        realized_independence_axes=manifest.get("realized_independence_axes"),
        degradation_reason=manifest.get("degradation_reason"),
    )
    if rebuilt != manifest:
        raise ReplicaBatchContractError("parallel batch manifest is not canonical")
    return rebuilt, digest, legs
