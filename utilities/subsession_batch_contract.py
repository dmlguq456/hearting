#!/usr/bin/env python3
"""Canonical immutable identity for one bounded sub-session batch (SD-119 M-5).

`replica_batch_contract.py` seals a *route-leg* batch: its members carry
`parallel_leg_index`, `perspective`, `fallback_hop` and an independence axis,
because every member is a leg of an SD-89 parallel group. The sole
subdivision-permitted node (`autopilot-code` `execute`) structurally has no such
group (SD-119 (1)), so a sub-session batch has none of those fields.

M-5 requires reusing the same full-N atomic reservation *primitive* without
presupposing route-leg membership. That is what this module is: a second, narrow
manifest kind the governor can verify, so a sub-session batch proves its own
identity instead of forging leg fields it does not have.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SCHEMA_VERSION = 1
KIND = "subsession-batch"
MIN_WIDTH = 2
MAX_WIDTH = 4
SUPPORTED_HARNESSES = frozenset({"claude", "codex", "opencode"})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_KEYS = {
    "schema_version", "kind", "declared_size", "chain_id", "route_id",
    "route_node", "parent_attempt_id", "chain_manifest_sha256", "members",
}
_MEMBER_KEYS = {
    "attempt_id", "subsession_id", "subsession_index", "route_node", "harness",
    "fixed_files_sha256", "stage_authority",
}
_IDENTITY_KEYS = ("chain_id", "route_id", "route_node", "parent_attempt_id")


class SubsessionBatchContractError(ValueError):
    """Typed refusal for a manifest that is not a canonical sub-session batch."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _exact_int(value: Any) -> bool:
    """`True`/`False` are ints in Python; an index or authority flag is not."""
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise SubsessionBatchContractError("subsession batch manifest keys are not canonical")
    if manifest["schema_version"] != SCHEMA_VERSION or manifest["kind"] != KIND:
        raise SubsessionBatchContractError("invalid subsession batch manifest identity")
    for key in _IDENTITY_KEYS:
        if not isinstance(manifest[key], str) or not manifest[key]:
            raise SubsessionBatchContractError(f"subsession batch {key} must be a nonempty string")
    if not manifest["chain_id"].startswith("ssc-"):
        raise SubsessionBatchContractError("invalid subsession batch chain id")
    if not _HEX64.fullmatch(str(manifest["chain_manifest_sha256"])):
        raise SubsessionBatchContractError("invalid sealed chain manifest digest")
    members = manifest["members"]
    size = manifest["declared_size"]
    if not isinstance(members, list) or not _exact_int(size):
        raise SubsessionBatchContractError("subsession batch members must be a declared list")
    if not MIN_WIDTH <= size <= MAX_WIDTH or size != len(members):
        raise SubsessionBatchContractError("subsession batch must declare 2..4 members")
    indices: list[int] = []
    attempts: set[str] = set()
    subsessions: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict) or set(member) != _MEMBER_KEYS:
            raise SubsessionBatchContractError("subsession batch member keys are not canonical")
        attempt_id = member["attempt_id"]
        subsession_id = member["subsession_id"]
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SubsessionBatchContractError("subsession batch member attempt id is invalid")
        if not isinstance(subsession_id, str) or not subsession_id.startswith("ss-"):
            raise SubsessionBatchContractError("invalid subsession id")
        if attempt_id in attempts or subsession_id in subsessions:
            raise SubsessionBatchContractError("duplicate subsession member identity")
        attempts.add(attempt_id)
        subsessions.add(subsession_id)
        if member["harness"] not in SUPPORTED_HARNESSES:
            raise SubsessionBatchContractError("unsupported subsession member harness")
        # A slice never owns the stage gate (SD-119 (6)); a manifest that claims
        # otherwise would let a reservation authorize a stage-authoritative start.
        if not _exact_int(member["stage_authority"]) or member["stage_authority"] != 0:
            raise SubsessionBatchContractError("subsession member must declare stage_authority 0")
        if not isinstance(member["fixed_files_sha256"], str) or not _HEX64.fullmatch(
            member["fixed_files_sha256"]
        ):
            raise SubsessionBatchContractError("invalid subsession member fixed-files digest")
        if member["route_node"] != manifest["route_node"]:
            raise SubsessionBatchContractError("subsession member route node mismatch")
        if not _exact_int(member["subsession_index"]):
            raise SubsessionBatchContractError("subsession index must be an integer")
        indices.append(member["subsession_index"])
        normalized.append(dict(member))
    if sorted(indices) != list(range(1, size + 1)):
        raise SubsessionBatchContractError("subsession indices are not an exact 1..N permutation")
    normalized.sort(key=lambda member: member["subsession_index"])
    canonical = {**manifest, "members": normalized}
    if canonical != manifest:
        raise SubsessionBatchContractError("subsession batch manifest is not canonical")
    return canonical


def build_manifest(
    *,
    chain_id: str,
    route_id: str,
    route_node: str,
    parent_attempt_id: str,
    chain_manifest_sha256: str,
    members: list[dict[str, Any]],
) -> tuple[dict[str, Any], str, dict[str, str]]:
    """Validate and seal the full declared N-way sub-session batch."""

    manifest = _validate({
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "declared_size": len(members) if isinstance(members, list) else 0,
        "chain_id": chain_id,
        "route_id": route_id,
        "route_node": route_node,
        "parent_attempt_id": parent_attempt_id,
        "chain_manifest_sha256": chain_manifest_sha256,
        "members": members,
    })
    return manifest, _digest(manifest), _member_digests(manifest)


def verify_manifest(manifest: Any) -> tuple[dict[str, Any], str, dict[str, str]]:
    verified = _validate(manifest)
    return verified, _digest(verified), _member_digests(verified)


def _member_digests(manifest: dict[str, Any]) -> dict[str, str]:
    """Bind each member digest to the WHOLE batch identity, not just its own row.

    Digesting the member alone would make one slice's proof replayable in any
    other chain that happens to share a route node -- the leg digest has to name
    the batch it belongs to, exactly as the replica contract's does.
    """

    common = {key: value for key, value in manifest.items() if key != "members"}
    return {
        str(member["attempt_id"]): _digest({**common, "member": member})
        for member in manifest["members"]
    }
