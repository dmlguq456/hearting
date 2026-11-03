#!/usr/bin/env python3
"""Portable contract helpers for splitting one route stage into worker sessions."""

from __future__ import annotations

import hashlib
from glob import has_magic
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from owner_write_advisory import worktree_mutating_scope

SCHEMA_VERSION = 1
MODES = {"serial", "parallel"}
ADAPTERS = {"claude", "codex", "opencode"}
_ID = re.compile(r"^(?:ssc|ss)-[A-Za-z0-9._-]{4,200}$")
_ATTEMPT = re.compile(r"^att-[A-Za-z0-9._-]{8,240}$")
_REPORT_PATH = re.compile(r"^evidence/([a-z0-9][a-z0-9._-]{0,199})/report\.md$")
_REPORT_LINE = re.compile(r"^\s*report\s*:\s*(.*?)\s*$", re.IGNORECASE)


class StageSessionError(ValueError):
    """Raised when a stage-session manifest is unsafe or incomplete."""


def report_path_from_brief(
    brief_text: str, *, session_id: str, route_node: str | None, node,
    explicit_only: bool = False,
) -> str | None:
    """Interpret the session's own in-scope report from its existing phase brief."""
    if not isinstance(node, dict):
        return None
    import artifact_context

    scopes = [
        scope for scope in (node.get("write_scope") or [])
        if isinstance(scope, str) and artifact_context.classify_scope(scope) == "artifact"
    ]
    evidence_probe = "evidence/__stage_session_scope_probe__/report.md"
    has_evidence_scope = artifact_context.scope_matches(evidence_probe, scopes) or any(
        "evidence" in scope.replace("\\", "/").split("/") for scope in scopes
    )
    if not has_evidence_scope:
        return None

    found = []
    for line in brief_text.splitlines():
        match = _REPORT_LINE.fullmatch(line)
        if match:
            raw = match.group(1).strip()
            found.append(raw.split()[0].strip("`*_") if raw else "")
    if len(found) > 1:
        raise StageSessionError(f"report-path-ambiguous:{session_id}")
    explicit = bool(found)
    if explicit:
        value = found[0]
    elif explicit_only:
        return None
    elif route_node == "execute":
        value = f"evidence/{session_id.lower()}/report.md"
    else:
        return None
    if not _REPORT_PATH.fullmatch(value):
        raise StageSessionError(f"report-path-invalid:{session_id}:{value}")
    if not artifact_context.scope_matches(value, scopes):
        if explicit:
            raise StageSessionError(f"report-path-outside-write-scope:{session_id}:{value}")
        return None
    return value


def _git_path(path: Path, flag: str) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", flag],
            text=True, capture_output=True, check=True, timeout=10,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    if not value:
        return None
    found = Path(value)
    return (found if found.is_absolute() else Path(path) / found).resolve()


def same_repository_worktree(candidate: Path | str, sealed_cwd: Path | str) -> bool:
    """True when `candidate` is a worktree root of the repository `sealed_cwd` belongs to."""
    try:
        candidate = Path(candidate).resolve()
        sealed_cwd = Path(sealed_cwd).resolve()
    except (OSError, ValueError):
        return False
    if _git_path(candidate, "--show-toplevel") != candidate:
        return False
    common = _git_path(candidate, "--git-common-dir")
    return common is not None and common == _git_path(sealed_cwd, "--git-common-dir")


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


def sealed_pointer_bytes(manifest: dict[str, Any]) -> bytes:
    """Return the exact bytes used by the canonical chain pointer writer."""

    return (json.dumps(manifest, sort_keys=True, default=str) + "\n").encode("utf-8")


def slice_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def slice_files_sha256(values: list[str]) -> str:
    return slice_text_sha256("\0".join(sorted(values)))


GAP_RETRY_PURPOSE = "gap-retry"


def _worker_adapter(manifest: dict[str, Any]) -> str | None:
    """The worker harness the route's parent moved the worker pin to (`start --pin worker=`),
    or None when it never moved it: a session derived after that change launches there, one
    derived before keeps its source session's adapter."""
    try:
        import route_authority
        route = json.loads(Path(manifest["route_file"]).read_text(encoding="utf-8"))
        harness = route_authority.changed_pin_harness(route, "worker")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return harness if harness in ADAPTERS else None


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
    moved = _worker_adapter(manifest)
    sessions = []
    for offset, session_id in enumerate(wanted, 1):
        source = by_id[session_id]
        retry_id = f"ss-gap-{parent[:16]}-{offset}"
        phase_brief = source["phase_brief"]
        report_path = source.get("report_path")
        if report_path:
            brief_path = Path(phase_brief)
            brief_text = brief_path.read_text(encoding="utf-8")
            if not any(_REPORT_LINE.fullmatch(line) for line in brief_text.splitlines()):
                continued_brief = Path(manifest["_manifest_path"]).parent / f"{retry_id}.brief.md"
                continued_brief.write_text(
                    brief_text.rstrip("\n")
                    + f"\n\nreport: {report_path} (continues the assigned artifact report)\n",
                    encoding="utf-8",
                )
                phase_brief = str(continued_brief)
        derived = {
            "subsession_id": retry_id,
            "attempt_id": f"att-gap-{parent[:16]}-{offset}",
            "adapter": moved or source["adapter"],
            "slug": f"gap-{parent[:8]}-{offset}",
            "phase_brief": phase_brief,
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


def derive_continuation_manifest(
    manifest: dict[str, Any], unfinished_indexes, *, ran=(), notes=None, brief_dir: Path,
) -> dict[str, Any]:
    """The serial chain that finishes a stopped one, derived instead of hand-written.

    `unfinished_indexes` are the sessions that did not pass, in order: one in `ran` (it ran
    and did not pass) is run again as a `gap-retry` of itself, one that never started runs
    as planned. A session that passed is not repeated. `notes` maps an index to the lines
    its retry must know (what the previous attempt finished and what it left); they are
    appended to a copy of its brief under `brief_dir`, the original brief is kept.
    Identities derive from the source manifest's hash, so deriving again is
    byte-identical and the same continuation is reused."""
    if "_manifest_sha256" not in manifest:
        raise StageSessionError("continuation-source-manifest-not-loaded")
    by_index = {session["index"]: session for session in manifest["sessions"]}
    wanted = [index for index in dict.fromkeys(unfinished_indexes) if index in by_index]
    if not wanted:
        raise StageSessionError("continuation-requires-an-unfinished-session")
    parent = manifest["_manifest_sha256"]
    moved = _worker_adapter(manifest)
    sessions = []
    for offset, index in enumerate(wanted, 1):
        source = by_index[index]
        derived = {key: source[key] for key in (
            "adapter", "fixed_files", "narrow_verify", "expected_round_trips",
        )}
        derived["adapter"] = moved or derived["adapter"]
        derived.update({
            "subsession_id": f"ss-cont-{parent[:16]}-{offset}",
            "attempt_id": f"att-cont-{parent[:16]}-{offset}",
            "slug": f"cont-{parent[:8]}-{offset}",
            "phase_brief": source["phase_brief"],
            "continues": source["subsession_id"],
        })
        if index in ran:
            derived["subsession_purpose"] = GAP_RETRY_PURPOSE
            derived["gap_retry_of"] = source["subsession_id"]
        lines = (notes or {}).get(index) or []
        report_path = source.get("report_path")
        original_brief = Path(source["phase_brief"])
        brief_text = original_brief.read_text(encoding="utf-8").rstrip("\n")
        has_report_line = any(_REPORT_LINE.fullmatch(line) for line in brief_text.splitlines())
        if lines or (report_path and not has_report_line):
            brief = Path(brief_dir) / f"{derived['subsession_id']}.brief.md"
            brief.parent.mkdir(parents=True, exist_ok=True)
            content = brief_text
            if lines:
                content += "\n\n## From the previous attempt\n\n" + "".join(
                    f"- {line}\n" for line in lines
                )
            if report_path and not has_report_line:
                content += f"\nreport: {report_path} (continues the assigned artifact report)\n"
            brief.write_text(content, encoding="utf-8")
            derived["phase_brief"] = str(brief)
        if "node" in source:
            derived["node"] = source["node"]
        sessions.append(derived)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "stage-session-chain",
        "chain_id": f"ssc-cont-{parent[:16]}",
        "mode": "serial",
        **{key: manifest[key] for key in ("worktree", "route_file", "route_id", "route_hash",
                                          "route_node", "completion_gate")},
        "continues_manifest_sha256": parent,
        "sessions": sessions,
    }


def _absolute(value: object, *, base: Path, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise StageSessionError(f"{field}-missing")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def is_glob_path(path: Path, *, worktree: Path) -> bool:
    """An existing exact file wins over glob syntax, including Next.js route names.

    Both subdivision admission and the adapters use this judgment. A missing
    ordinary path can still name a file the worker will create; a missing glob
    cannot be an exact file inventory.
    """
    # The checkout's own name is never part of a file's pattern syntax.
    return not path.is_file() and has_magic(path.relative_to(worktree).as_posix())


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
        if is_glob_path(path, worktree=worktree):
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
        # The manifest's `worktree` becomes every session's `--cwd`, so the
        # common loader -- not only `plan-slices` -- must pin it to the route's
        # sealed cwd; every identity field can match while the tree is foreign
        # (routing-flex canary review round 2, M1).
        sealed_cwd = route.get("cwd")
        # A linked worktree of the same repository is accepted.
        if (
            isinstance(sealed_cwd, str) and sealed_cwd
            and worktree != Path(sealed_cwd).resolve()
            and (not same_repository_worktree(worktree, sealed_cwd))
        ):
            raise StageSessionError(f"manifest-worktree-mismatch:{worktree}:{Path(sealed_cwd).resolve()}")
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
        # A worktree-mutating scope (`source/**`) means the whole worktree, the
        # same reading the route and guards use; `_fixed_files` already keeps
        # every file inside the worktree.
        if (
            route is not None and node is not None
            and not any(
                isinstance(scope, str) and scope and worktree_mutating_scope(scope)
                for scope in (node.get("write_scope") or [])
            )
        ):
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
    seen_reports: set[str] = set()
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
        report_identity = item.get("gap_retry_of") or item.get("continues") or session_id
        report_path = report_path_from_brief(
            phase_brief.read_text(encoding="utf-8"), session_id=report_identity,
            route_node=raw.get("route_node"), node=node,
        )
        if report_path and report_path in seen_reports:
            raise StageSessionError(f"report-path-duplicate:{report_path}")
        if report_path:
            seen_reports.add(report_path)
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
            "report_path": report_path,
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
