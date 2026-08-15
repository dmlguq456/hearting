#!/usr/bin/env python3
"""Portable contract helpers for splitting one route stage into worker sessions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

SCHEMA_VERSION = 1
MODES = {"serial", "parallel"}
ADAPTERS = {"claude", "codex", "opencode"}
_ID = re.compile(r"^(?:ssc|ss)-[A-Za-z0-9._-]{4,200}$")
_ATTEMPT = re.compile(r"^att-[A-Za-z0-9._-]{8,240}$")


class StageSessionError(ValueError):
    """Raised when a stage-session manifest is unsafe or incomplete."""


def validate_subdivision_or_fallback(
    path: Path | str,
    *,
    route: dict[str, Any],
    node: dict[str, Any],
    record=None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a parallel subdivision at batch admission; on violation fall back
    to a single session with a `subdivision-disjointness-unproven` ledger row.

    Returns (manifest, None) on success and (None, reason) when the subdivision
    cannot be proven disjoint/safe — the caller then runs the node as one
    ordinary session instead of raising (the typed fallback of SD-103). `record`
    is a callable(route_id, route_node, detail) that writes the SD-93 ledger.
    """
    try:
        manifest = load_manifest(path, route=route, node=node)
    except StageSessionError as exc:
        if record is not None:
            record(route.get("route_id"), node.get("id"), str(exc))
        return None, "subdivision-disjointness-unproven"
    if manifest.get("mode") != "parallel":
        return manifest, None
    permission = node.get("subdivision")
    if not isinstance(permission, dict):
        if record is not None:
            record(route.get("route_id"), node.get("id"), "no subdivision permission")
        return None, "subdivision-disjointness-unproven"
    declared = {
        Path(value).resolve(strict=False)
        for session in manifest["sessions"]
        for value in session["fixed_files"]
    }
    if len(declared) != sum(len(s["fixed_files"]) for s in manifest["sessions"]):
        if record is not None:
            record(route.get("route_id"), node.get("id"), "parallel-fixed-file-overlap")
        return None, "subdivision-disjointness-unproven"
    return manifest, None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


GAP_RETRY_PURPOSE = "gap-retry"


def derive_gap_retry_manifest(
    manifest: dict[str, Any], failed_subsession_ids
) -> dict[str, Any]:
    """AC 29: derive the gap-retry chain from ONLY the failed slices.

    SD-103: a failed slice does not roll back its successful siblings; the owner
    opens a `gap-retry` sub-session carrying the failed slices' `fixed_files` and
    nothing else. Deriving that manifest here — instead of hand-copying it at the
    call site — is what makes "exactly the failed slice's files" a property of
    the production path rather than of one fixture's assignment statement.

    Identities are derived deterministically from the parent manifest's hash and
    the failed slice id, so re-deriving the same retry is byte-identical and
    resumable. A retry of one slice is a `serial` chain (a parallel subdivision
    is a 2..N contract); two or more stay `parallel`.

    The parent's leg binding (`node`, or `leg_index` as the positional spelling)
    is carried through unchanged. A gap-retry is a SUBSET of the parent's slices,
    never a re-partition, so the failed slice's leg name stays valid -- and
    `dispatch-batch._bind_subdivision_sessions` (N1) refuses any manifest session
    that names neither. Dropping the key here would make the one recovery path
    SD-103 13.30.5 declares refusable at admission the moment two slices fail
    (a single failure falls to `serial` and never reaches the binding gate,
    which is why the seam stayed invisible).
    """
    if "_manifest_sha256" not in manifest:
        raise StageSessionError("gap-retry-source-manifest-not-loaded")
    wanted = list(dict.fromkeys(failed_subsession_ids))
    if not wanted:
        raise StageSessionError("gap-retry-requires-a-failed-slice")
    by_id = {session["subsession_id"]: session for session in manifest["sessions"]}
    unknown = [item for item in wanted if item not in by_id]
    if unknown:
        raise StageSessionError("gap-retry-unknown-slice:" + ",".join(sorted(unknown)))
    parent = manifest["_manifest_sha256"]
    sessions = []
    for offset, session_id in enumerate(wanted, 1):
        source = by_id[session_id]
        derived = {
            "subsession_id": f"ss-gap-{parent[:16]}-{offset}",
            "attempt_id": f"att-gap-{parent[:16]}-{offset}",
            "adapter": source["adapter"],
            "slug": f"gap-{parent[:8]}-{offset}",
            "phase_brief": source["phase_brief"],
            # The whole point of the derivation: the retry's fence is exactly
            # the failed slice's, never widened to the parent union.
            "fixed_files": list(source["fixed_files"]),
            "narrow_verify": source["narrow_verify"],
            "expected_round_trips": source["expected_round_trips"],
            "subsession_purpose": GAP_RETRY_PURPOSE,
            "gap_retry_of": session_id,
        }
        if "node" in source:
            # The stable spelling: a leg name survives subsetting unchanged.
            derived["node"] = source["node"]
        elif "leg_index" in source:
            # The positional spelling cannot survive subsetting verbatim --
            # `_bind_subdivision_sessions` indexes into the RETRY's leg list,
            # which is as long as the retry (the admission count check pairs
            # sessions with legs 1:1). Re-index onto the retry's own ordering,
            # which is `wanted` order, so the key still names one leg.
            derived["leg_index"] = offset - 1
        sessions.append(derived)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage-session-chain",
        "chain_id": f"ssc-gap-{parent[:16]}",
        "mode": "parallel" if len(sessions) > 1 else "serial",
        "worktree": manifest["worktree"],
        "route_file": manifest["route_file"],
        "route_id": manifest["route_id"],
        "route_hash": manifest["route_hash"],
        "route_node": manifest["route_node"],
        "completion_gate": manifest["completion_gate"],
        "gap_retry_of_manifest_sha256": parent,
        "sessions": sessions,
    }


def _absolute(value: object, *, base: Path, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise StageSessionError(f"{field}-missing")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def _fixed_files(values: object, *, worktree: Path, session_id: str) -> list[str]:
    if not isinstance(values, list) or not values:
        raise StageSessionError(f"fixed-files-missing:{session_id}")
    result: list[str] = []
    for raw in values:
        path = _absolute(raw, base=worktree, field=f"fixed-file:{session_id}")
        try:
            path.relative_to(worktree)
        except ValueError as exc:
            raise StageSessionError(f"fixed-file-outside-worktree:{session_id}:{path}") from exc
        if any(char in str(raw) for char in "*?[]"):
            raise StageSessionError(f"fixed-file-must-be-exact:{session_id}:{raw}")
        value = str(path)
        if value not in result:
            result.append(value)
    return sorted(result)


def load_manifest(
    path: Path | str,
    *,
    route: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load and normalize a bounded stage-session chain manifest."""

    manifest_path = Path(path).resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise StageSessionError(f"manifest-unreadable:{manifest_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise StageSessionError("manifest-schema-invalid")
    if raw.get("kind") != "stage-session-chain":
        raise StageSessionError("manifest-kind-invalid")
    chain_id = raw.get("chain_id")
    if not isinstance(chain_id, str) or not _ID.fullmatch(chain_id) or not chain_id.startswith("ssc-"):
        raise StageSessionError("chain-id-invalid")
    mode = raw.get("mode")
    if mode not in MODES:
        raise StageSessionError("session-mode-invalid")
    worktree = _absolute(raw.get("worktree"), base=manifest_path.parent, field="worktree")
    route_file = _absolute(raw.get("route_file"), base=manifest_path.parent, field="route-file")
    if route is not None:
        expected = {"route_id": route.get("route_id"), "route_hash": route.get("route_hash")}
        if any(raw.get(key) != value for key, value in expected.items()):
            raise StageSessionError("manifest-route-identity-mismatch")
        expected_file = route.get("_route_file")
        if expected_file and route_file != Path(str(expected_file)).resolve():
            raise StageSessionError("manifest-route-file-mismatch")
    if node is not None:
        if raw.get("route_node") != node.get("id"):
            raise StageSessionError("manifest-route-node-mismatch")
        if raw.get("completion_gate") != node.get("completion_gate"):
            raise StageSessionError("manifest-completion-gate-mismatch")
    sessions = raw.get("sessions")
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 16:
        raise StageSessionError("session-count-invalid")
    if mode == "parallel":
        # SD-103: a parallel subdivision's session count is capped by the
        # registry permission block (2..max_slices), never the generic 1..16.
        permission = None
        if node is not None:
            permission = node.get("subdivision")
        elif route is not None:
            for candidate in route.get("nodes", []):
                if isinstance(candidate, dict) and candidate.get("id") == raw.get("route_node"):
                    permission = candidate.get("subdivision")
                    break
        cap = permission.get("max_slices", 4) if isinstance(permission, dict) else 4
        if not 2 <= len(sessions) <= cap:
            raise StageSessionError(
                f"parallel-session-count-invalid:{2}:{cap}"
            )
        if not isinstance(permission, dict) or permission.get("disjointness") != "exact-fixed-files":
            raise StageSessionError("parallel-subdivision-not-permitted")
        union = set()
        for item in sessions:
            fixed = _fixed_files(
                item.get("fixed_files"), worktree=worktree,
                session_id=item.get("subsession_id") or f"index-{sessions.index(item) + 1}",
            )
            union |= set(fixed)
        if route is not None and node is not None:
            sealed_scopes = []
            for scope in (node.get("write_scope") or []):
                if not isinstance(scope, str) or not scope:
                    continue
                root = scope[:-3] if scope.endswith("/**") else scope
                sealed_scopes.append((worktree / root).resolve(strict=False))
            for file in sorted(union):
                if not any(
                    file == str(root) or file.startswith(str(root) + "/")
                    for root in sealed_scopes
                ):
                    raise StageSessionError(
                        f"parallel-fixed-file-outside-write-scope:{file}"
                    )
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_attempts: set[str] = set()
    for offset, item in enumerate(sessions, 1):
        if not isinstance(item, dict):
            raise StageSessionError(f"session-invalid:{offset}")
        session_id = item.get("subsession_id")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id) or not session_id.startswith("ss-"):
            raise StageSessionError(f"subsession-id-invalid:{offset}")
        if session_id in seen_ids:
            raise StageSessionError(f"subsession-id-duplicate:{session_id}")
        seen_ids.add(session_id)
        attempt_id = item.get("attempt_id")
        if not isinstance(attempt_id, str) or not _ATTEMPT.fullmatch(attempt_id):
            raise StageSessionError(f"attempt-id-invalid:{session_id}")
        if attempt_id in seen_attempts:
            raise StageSessionError(f"attempt-id-duplicate:{attempt_id}")
        seen_attempts.add(attempt_id)
        adapter = item.get("adapter")
        if adapter not in ADAPTERS:
            raise StageSessionError(f"adapter-invalid:{session_id}")
        slug = item.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", slug):
            raise StageSessionError(f"slug-invalid:{session_id}")
        phase_brief = _absolute(
            item.get("phase_brief"), base=manifest_path.parent, field=f"phase-brief:{session_id}"
        )
        if not phase_brief.is_file():
            raise StageSessionError(f"phase-brief-missing:{session_id}")
        narrow_verify = item.get("narrow_verify")
        if not isinstance(narrow_verify, str) or not narrow_verify.strip() or "\n" in narrow_verify:
            raise StageSessionError(f"narrow-verify-invalid:{session_id}")
        rounds = item.get("expected_round_trips")
        if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= 20:
            raise StageSessionError(f"expected-round-trips-invalid:{session_id}")
        fixed = _fixed_files(item.get("fixed_files"), worktree=worktree, session_id=session_id)
        normalized.append({
            **item,
            "index": offset,
            "count": len(sessions),
            "subsession_id": session_id,
            "attempt_id": attempt_id,
            "adapter": adapter,
            "slug": slug,
            "phase_brief": str(phase_brief),
            "fixed_files": fixed,
            "narrow_verify": narrow_verify.strip(),
            "expected_round_trips": rounds,
        })
    if mode == "parallel":
        owners: dict[str, str] = {}
        for item in normalized:
            for file in item["fixed_files"]:
                prior = owners.setdefault(file, item["subsession_id"])
                if prior != item["subsession_id"]:
                    raise StageSessionError(
                        f"parallel-fixed-file-overlap:{file}:{prior}:{item['subsession_id']}"
                    )
    return {
        **raw,
        "_manifest_path": str(manifest_path),
        "_manifest_sha256": sha256_file(manifest_path),
        "worktree": str(worktree),
        "route_file": str(route_file),
        "sessions": normalized,
    }
