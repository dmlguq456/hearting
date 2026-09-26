#!/usr/bin/env python3
"""Cycle display-title declaration: seal-time emission and retroactive backfill.

Writes `.runtime/artifact-producer/v1/cycle-display-titles.json` (PRD §37.1 B)
so Cairn can show a human title instead of a folder name. `emit_after_seal_locked`
is called once, at the tail of the producer's `_commit_sealed`, and never blocks
sealing: any failure becomes a deferred marker, drained on a later seal or by
`backfill`. `backfill` replays the same rule set across every sealed cycle
already on disk, gated by the deployed Cairn reader so no rejected byte is ever
written.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_identity  # noqa: E402
import artifact_lifecycle  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402
import artifact_metadata_amendment as AMA  # noqa: E402
import campaign_title_repair as CTR  # noqa: E402

CYCLE_SCHEMA = AMA.CYCLE_SCHEMA
CYCLE_TITLES_REL = AMA.CYCLE_TITLES_REL
ROOT_IDENTITY_REL = AMA.ROOT_IDENTITY_REL
DISPLAY_TITLES_REL = AMA.DISPLAY_TITLES_REL
PRODUCER_REL = AMA.PRODUCER_REL

MAX_TITLE_CHARS = 80
# `FORBIDDEN_GENERIC_TITLES` (campaign_title_repair) is rejected by containment: a
# candidate carrying "legacy support residue" as a label, not just as the whole
# string, is still a residue label (owner addendum item 14). The extra list below
# stays exact-match so an ordinary title that merely contains "report" or "요약"
# is not rejected.
GENERIC_TITLES_EXACT = {
    "goal", "runlog", "experiments runlog", "report", "final report", "summary",
    "분석 요약", "최종 구현 보고서", "문서화 실행 요약", "보고서", "요약",
}
EMISSIONS_REL = PRODUCER_REL / "cycle-title-emissions"
BACKFILL_JOURNAL_REL = EMISSIONS_REL / "backfill"
SEAL_DRAIN_LIMIT = 8
PRIMARY_MAX_BYTES = 1024 * 1024
HEADING_SCAN_LINES = 60
MARKER_SCHEMA = "hearting-cycle-title-emission-marker/v1"
BACKFILL_JOURNAL_SCHEMA = "hearting-cycle-title-backfill-journal/v1"
_CYCLE_TITLE_ENTRY_KEYS = frozenset({"campaign_id", "cycle_id", "display_title", "manifest_bindings"})
_REJECT_ORDER = (
    "empty", "control-char", "too-long", "slug-like", "campaign-title",
    "token-only", "filename-like", "generic", "markdown-link", "title-not-distinct",
)
_TOKEN_RE = re.compile(r"^[\w.\-]+$")
_FILENAME_EXT_RE = re.compile(r"\.(md|json|ya?ml|txt|py|sh)$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ROUTE_PREFIX_HASH_RE = re.compile(r"^#+\s*")
_ROUTE_PREFIX_TASK_RE = re.compile(r"^Task:\s*", re.IGNORECASE)
_ROUTE_PREFIX_TASK_KO_RE = re.compile(r"^작업\s*:\s*")
_ROUTE_PREFIX_CAPABILITY_RE = re.compile(r"^autopilot-[\w.-]+\s*\([^)]*\)\s*[—-]\s*")
_HEADING_TRAILING_HASH_RE = re.compile(r"\s*#+\s*$")


class CycleTitlesError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CycleContext:
    record: Mapping[str, Any]
    manifest: Mapping[str, Any]
    cycle_dir: Path
    campaign: Mapping[str, Any]
    v2_title: Optional[str]
    route_text: Optional[str]


@dataclass(frozen=True)
class Decision:
    title: Optional[str]
    source: Optional[str]
    reason: Optional[str]
    rejected: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReaderResult:
    status: str
    accepted_cycle_ids: FrozenSet[str]
    detail: str
    app_dir: Optional[str]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[Any]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None


def _now_rfc3339(now: Optional[float] = None) -> str:
    when = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm(value: str) -> str:
    return normalize_candidate(value).casefold()


def record_digest(raw: bytes) -> str:
    """Digest of the manifest's on-disk bytes; compared to `record.manifest_digest`."""
    return artifact_manifest.digest_bytes(raw)


def binding_digest(parsed: Mapping[str, Any]) -> str:
    """Digest of the parsed manifest, canonicalized without a trailing newline.

    Deliberately distinct from `record_digest`: the on-disk manifest bytes carry
    a trailing newline the canonical-JSON binding digest does not, so the two
    values differ even for the same manifest (T2).
    """
    return CTR.digest_json(parsed)


# ---------------------------------------------------------------------------
# title rules
# ---------------------------------------------------------------------------


def normalize_candidate(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def _route_task_first_line(route_text: Optional[str]) -> Optional[str]:
    if not isinstance(route_text, str):
        return None
    line = None
    for raw_line in route_text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            line = stripped
            break
    if line is None:
        return None
    line = _ROUTE_PREFIX_HASH_RE.sub("", line)
    line = _ROUTE_PREFIX_TASK_RE.sub("", line)
    line = _ROUTE_PREFIX_TASK_KO_RE.sub("", line)
    line = _ROUTE_PREFIX_CAPABILITY_RE.sub("", line)
    return line


def _first_heading(raw: bytes) -> Optional[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()[:HEADING_SCAN_LINES]
    index = 0
    if index < len(lines) and lines[index].strip() == "---":
        closing = index + 1
        while closing < len(lines) and lines[closing].strip() != "---":
            closing += 1
        if closing < len(lines):
            index = closing + 1
    in_fence = False
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("# "):
            heading = _HEADING_TRAILING_HASH_RE.sub("", line[2:]).strip()
            if heading:
                return heading
    return None


def _primary_heading_candidate(ctx: CycleContext) -> Tuple[Optional[str], Optional[str]]:
    artifacts = ctx.manifest.get("artifacts")
    primaries = [row for row in artifacts if isinstance(row, Mapping) and row.get("role") == "primary"] \
        if isinstance(artifacts, list) else []
    if len(primaries) == 0:
        return None, "primary-missing"
    if len(primaries) > 1:
        return None, "primary-ambiguous"
    artifact_id = primaries[0].get("artifact_id")
    revisions_field = ctx.manifest.get("artifact_revisions")
    revisions = [row for row in revisions_field if isinstance(row, Mapping) and row.get("artifact_id") == artifact_id] \
        if isinstance(revisions_field, list) else []
    if len(revisions) != 1:
        return None, "primary-ambiguous"
    revision = revisions[0]
    locator = revision.get("locator") if isinstance(revision.get("locator"), Mapping) else {}
    rel = locator.get("path")
    if locator.get("kind") != "cycle-relative" or not isinstance(rel, str):
        return None, "primary-not-markdown"
    media_type = revision.get("media_type")
    if not (media_type == "text/markdown" or rel.endswith(".md")):
        return None, "primary-not-markdown"
    try:
        resolved = (ctx.cycle_dir / rel).resolve()
        resolved.relative_to(ctx.cycle_dir.resolve())
    except (OSError, ValueError):
        return None, "primary-missing"
    try:
        info = resolved.lstat()
    except OSError:
        return None, "primary-missing"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "primary-missing"
    if info.st_size > PRIMARY_MAX_BYTES:
        return None, "primary-too-large"
    try:
        raw = resolved.read_bytes()
    except OSError:
        return None, "primary-missing"
    if len(raw) > PRIMARY_MAX_BYTES:
        return None, "primary-too-large"
    if artifact_manifest.digest_bytes(raw) != revision.get("content_digest"):
        return None, "primary-digest-mismatch"
    heading = _first_heading(raw)
    if heading is None:
        return None, "no-heading"
    return heading, None


def _reject_code(candidate: str, ctx: CycleContext, reserved: Set[str]) -> Optional[str]:
    if not candidate:
        return "empty"
    if _CONTROL_RE.search(candidate):
        return "control-char"
    if len(candidate) > MAX_TITLE_CHARS:
        return "too-long"
    try:
        slug, _truncated = artifact_locator.slugify(candidate)
    except artifact_locator.LocatorError:
        slug = ""
    locator_slug = artifact_locator.strip_leading_date(str(ctx.record.get("locator") or ""))
    if slug and (slug == locator_slug or locator_slug.startswith(slug)):
        return "slug-like"
    campaign_values = {
        str(ctx.campaign.get("title") or ""),
        str(ctx.campaign.get("key") or ""),
        str(ctx.campaign.get("slug") or ""),
    }
    if ctx.v2_title:
        campaign_values.add(str(ctx.v2_title))
    if candidate.casefold() in {value.casefold() for value in campaign_values if value}:
        return "campaign-title"
    if " " not in candidate and _TOKEN_RE.fullmatch(candidate):
        return "token-only"
    if "/" in candidate or _FILENAME_EXT_RE.search(candidate):
        return "filename-like"
    folded = candidate.casefold()
    if folded in GENERIC_TITLES_EXACT or any(forbidden in folded for forbidden in CTR.FORBIDDEN_GENERIC_TITLES):
        return "generic"
    if "](" in candidate:
        return "markdown-link"
    if candidate.casefold() in reserved:
        return "title-not-distinct"
    return None


def derive_display_title(ctx: CycleContext, *, existing_title: Optional[str], reserved: Set[str]) -> Decision:
    if existing_title is not None:
        return Decision(existing_title, "existing-declaration", None, ())
    rejected: List[Tuple[str, str]] = []
    record_raw = ctx.record.get("title") if isinstance(ctx.record.get("title"), str) else None
    route_raw = _route_task_first_line(ctx.route_text)
    primary_raw, primary_reason = _primary_heading_candidate(ctx)
    for source, raw in (("record-title", record_raw), ("route-task", route_raw), ("primary-heading", primary_raw)):
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = normalize_candidate(raw)
        code = _reject_code(candidate, ctx, reserved)
        if code is None:
            return Decision(candidate, source, None, tuple(rejected))
        rejected.append((source, code))
    # Terminal reason is the primary-heading outcome: its rejection code if a
    # heading candidate existed and was rejected, else the extraction-failure
    # reason if the primary step produced no candidate at all. `no-source` is
    # reserved for the case where neither happened (owner addendum item 14 /
    # gap G3) -- record-title or route-task rejections alone must not mask a
    # primary-heading outcome, since primary-heading is always attempted last.
    primary_rejection = next((code for source, code in rejected if source == "primary-heading"), None)
    if primary_rejection is not None:
        reason = primary_rejection
    elif primary_reason is not None:
        reason = primary_reason
    else:
        reason = "no-source"
    return Decision(None, None, reason, tuple(rejected))


# ---------------------------------------------------------------------------
# declaration validation / rendering
# ---------------------------------------------------------------------------


def validate_declaration(raw: bytes, *, root_id: str, repository_id: str) -> Dict[str, Any]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise CycleTitlesError("sidecar-invalid", f"json:{exc}") from exc
    if not isinstance(doc, dict):
        raise CycleTitlesError("sidecar-invalid", "object-required")
    try:
        AMA._assert_closed(doc, {"schema", "artifact_root_id", "repository_id", "entries"}, "sidecar-fields")
    except AMA.AmendmentError as exc:
        raise CycleTitlesError("sidecar-invalid", str(exc)) from exc
    if doc.get("schema") != CYCLE_SCHEMA or doc.get("artifact_root_id") != root_id or doc.get("repository_id") != repository_id:
        raise CycleTitlesError("sidecar-invalid", "identity-mismatch")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise CycleTitlesError("sidecar-invalid", "entries-required")
    seen: Set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CycleTitlesError("sidecar-invalid", "entry-object-required")
        try:
            AMA._assert_closed(entry, _CYCLE_TITLE_ENTRY_KEYS, "entry-fields")
        except AMA.AmendmentError as exc:
            raise CycleTitlesError("sidecar-invalid", str(exc)) from exc
        campaign_id = entry.get("campaign_id")
        cycle_id = entry.get("cycle_id")
        if not artifact_identity.is_well_formed(str(campaign_id), "campaign"):
            raise CycleTitlesError("sidecar-invalid", "campaign-id-malformed")
        if not artifact_identity.is_well_formed(str(cycle_id), "cycle"):
            raise CycleTitlesError("sidecar-invalid", "cycle-id-malformed")
        if cycle_id in seen:
            raise CycleTitlesError("sidecar-invalid", "cycle-id-duplicate")
        seen.add(cycle_id)
        title = entry.get("display_title")
        if not isinstance(title, str) or not title.strip():
            raise CycleTitlesError("sidecar-invalid", "display-title-empty")
        try:
            bindings = AMA._validate_bindings(entry.get("manifest_bindings"), "manifest-bindings")
        except AMA.AmendmentError as exc:
            raise CycleTitlesError("sidecar-invalid", str(exc)) from exc
        if len(bindings) != 1:
            raise CycleTitlesError("sidecar-invalid", "manifest-bindings-count")
        normalized.append({"campaign_id": campaign_id, "cycle_id": cycle_id,
                           "display_title": title, "manifest_bindings": bindings})
    if normalized != sorted(normalized, key=lambda row: (row["campaign_id"], row["cycle_id"])):
        raise CycleTitlesError("sidecar-invalid", "entries-not-sorted")
    return {"schema": CYCLE_SCHEMA, "artifact_root_id": root_id, "repository_id": repository_id, "entries": normalized}


def check_declaration_file(path: Path, *, root_id: str, repository_id: str) -> Dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"schema": CYCLE_SCHEMA, "artifact_root_id": root_id, "repository_id": repository_id, "entries": []}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CycleTitlesError("sidecar-invalid", "regular-file-required")
    return validate_declaration(path.read_bytes(), root_id=root_id, repository_id=repository_id)


def render_declaration(root_id: str, repository_id: str, entries: Sequence[Mapping[str, Any]]) -> bytes:
    doc = {
        "schema": CYCLE_SCHEMA, "artifact_root_id": root_id, "repository_id": repository_id,
        "entries": sorted(
            ({"campaign_id": row["campaign_id"], "cycle_id": row["cycle_id"],
              "display_title": row["display_title"], "manifest_bindings": list(row["manifest_bindings"])}
             for row in entries),
            key=lambda row: (row["campaign_id"], row["cycle_id"]),
        ),
    }
    return CTR.canonical(doc) + b"\n"


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


def _load_root_identity(root: Path) -> artifact_identity.RootIdentity:
    doc = _read_json(root / ROOT_IDENTITY_REL)
    if not isinstance(doc, dict):
        raise CycleTitlesError("root-identity-missing")
    try:
        return artifact_identity.RootIdentity.parse(doc)
    except artifact_identity.IdentityError as exc:
        raise CycleTitlesError("root-identity-invalid", str(exc)) from exc


def _eligibility_check(record: Optional[Mapping[str, Any]], raw: bytes, parsed: Mapping[str, Any],
                       identity: artifact_identity.RootIdentity) -> Optional[str]:
    if record is None:
        return "record-missing"
    state = record.get("state")
    if state == "superseded":
        return "record-superseded"
    if state != "sealed":
        return "record-not-sealed"
    if record_digest(raw) != record.get("manifest_digest"):
        return "record-digest-mismatch"
    campaign = parsed.get("campaign") if isinstance(parsed.get("campaign"), Mapping) else {}
    cycle = parsed.get("cycle") if isinstance(parsed.get("cycle"), Mapping) else {}
    if (parsed.get("artifact_root_id") != identity.artifact_root_id
            or parsed.get("repository_id") != identity.repository_id
            or campaign.get("campaign_id") != record.get("campaign_id")
            or cycle.get("campaign_id") != record.get("campaign_id")
            or cycle.get("cycle_id") != record.get("cycle_id")):
        return "identity-mismatch"
    if not artifact_identity.is_well_formed(str(parsed.get("manifest_revision_id")), "manifest_revision"):
        return "revision-malformed"
    return None


def _no_symlink_components(root: Path, manifest_path: Path) -> bool:
    campaigns = root / "campaigns"
    try:
        rel = manifest_path.resolve().relative_to(campaigns.resolve())
    except (OSError, ValueError):
        return False
    current = campaigns
    for part in rel.parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        except OSError:
            return False
    return True


def _campaign_json_for(manifest_path: Path) -> Dict[str, Any]:
    value = _read_json(manifest_path.parent.parent / "campaign.json")
    return value if isinstance(value, dict) else {}


def _v2_title(root: Path, campaign_id: str) -> Optional[str]:
    doc = _read_json(root / DISPLAY_TITLES_REL)
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("campaign_id") == campaign_id:
            title = entry.get("display_title")
            return title if isinstance(title, str) else None
    return None


def _route_text(root: Path, record: Mapping[str, Any]) -> Optional[str]:
    route_id = record.get("route_id")
    if not isinstance(route_id, str):
        return None
    try:
        path = artifact_lifecycle.canonical_route_path(root, route_id)
    except artifact_lifecycle.LifecycleError:
        return None
    doc = _read_json(path)
    request = doc.get("work_request") if isinstance(doc, dict) else None
    text = request.get("text") if isinstance(request, Mapping) else None
    return text if isinstance(text, str) else None


def _cycle_record_path(root: Path, cycle_id: str) -> Path:
    return root / PRODUCER_REL / "cycles" / f"{cycle_id}.json"


def _cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    doc = _read_json(_cycle_record_path(root, cycle_id))
    return doc if isinstance(doc, dict) else None


def _walk_manifests(root: Path) -> Dict[str, List[Path]]:
    campaigns = root / "campaigns"
    try:
        info = campaigns.lstat()
    except FileNotFoundError:
        raise CycleTitlesError("campaigns-directory-required")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CycleTitlesError("campaigns-directory-required")
    found: Dict[str, List[Path]] = {}
    stack = [campaigns]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                is_real_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_real_dir:
                stack.append(Path(entry.path))
                continue
            if entry.name != "manifest.json":
                continue
            path = Path(entry.path)
            try:
                lst = path.lstat()
            except OSError:
                found.setdefault("__unparseable__", []).append(path)
                continue
            if stat.S_ISLNK(lst.st_mode) or not stat.S_ISREG(lst.st_mode):
                found.setdefault("__unparseable__", []).append(path)
                continue
            parsed = _read_json(path)
            cycle = parsed.get("cycle") if isinstance(parsed, dict) else None
            cycle_id = cycle.get("cycle_id") if isinstance(cycle, Mapping) else None
            if not isinstance(cycle_id, str):
                found.setdefault("__unparseable__", []).append(path)
                continue
            found.setdefault(cycle_id, []).append(path)
    return found


# ---------------------------------------------------------------------------
# seal-time emission
# ---------------------------------------------------------------------------


def _write_marker_quiet(root: Path, cycle_id: Optional[str], reason: str, manifest_path: Path) -> None:
    if not isinstance(cycle_id, str):
        return
    try:
        try:
            manifest_rel = manifest_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            manifest_rel = str(manifest_path)
        marker = {"schema": MARKER_SCHEMA, "cycle_id": cycle_id, "reason": reason,
                  "manifest_path": manifest_rel, "recorded_on": _now_rfc3339()}
        CTR.write_atomic(root / EMISSIONS_REL / f"{cycle_id}.json", marker)
    except Exception:
        pass


def _remove_marker(root: Path, cycle_id: Optional[str]) -> None:
    if not isinstance(cycle_id, str):
        return
    try:
        (root / EMISSIONS_REL / f"{cycle_id}.json").unlink()
    except FileNotFoundError:
        pass


def _emit_single_locked(root: Path, record: Mapping[str, Any], manifest_path: Path) -> None:
    cycle_id = record.get("cycle_id")
    if not _no_symlink_components(root, manifest_path):
        return
    identity = artifact_lifecycle.read_root_identity(root)
    if identity is None:
        return
    raw = manifest_path.read_bytes()
    parsed = json.loads(raw.decode("utf-8"))
    if _eligibility_check(record, raw, parsed, identity) is not None:
        return
    declaration_path = root / CYCLE_TITLES_REL
    try:
        pre_doc = check_declaration_file(declaration_path, root_id=identity.artifact_root_id,
                                        repository_id=identity.repository_id)
    except CycleTitlesError:
        _write_marker_quiet(root, cycle_id, "deferred:sidecar-invalid", manifest_path)
        return
    pre_bytes = render_declaration(identity.artifact_root_id, identity.repository_id, pre_doc["entries"])
    ctx = CycleContext(
        record=record, manifest=parsed, cycle_dir=manifest_path.parent,
        campaign=_campaign_json_for(manifest_path), v2_title=_v2_title(root, str(record.get("campaign_id"))),
        route_text=_route_text(root, record),
    )
    existing = next((row for row in pre_doc["entries"] if row["cycle_id"] == cycle_id), None)
    reserved = {_norm(row["display_title"]) for row in pre_doc["entries"]
               if row["campaign_id"] == record.get("campaign_id") and row["cycle_id"] != cycle_id}
    decision = derive_display_title(ctx, existing_title=(existing["display_title"] if existing else None),
                                    reserved=reserved)
    if decision.title is None:
        return
    binding = {"manifest_revision_id": str(parsed.get("manifest_revision_id")), "manifest_digest": binding_digest(parsed)}
    new_entries = [row for row in pre_doc["entries"] if row["cycle_id"] != cycle_id]
    new_entries.append({"campaign_id": record.get("campaign_id"), "cycle_id": cycle_id,
                        "display_title": decision.title, "manifest_bindings": [binding]})
    post_bytes = render_declaration(identity.artifact_root_id, identity.repository_id, new_entries)
    validate_declaration(post_bytes, root_id=identity.artifact_root_id, repository_id=identity.repository_id)
    if post_bytes != pre_bytes:
        CTR.write_atomic_bytes(declaration_path, post_bytes)
    _remove_marker(root, cycle_id)


def _drain_pending(root: Path, *, exclude_cycle_id: Optional[str], limit: int) -> None:
    directory = root / EMISSIONS_REL
    if not directory.is_dir() or limit <= 0:
        return
    rows: List[Tuple[str, str, Optional[str]]] = []
    for entry in sorted(directory.glob("*.json")):
        if entry.stem == exclude_cycle_id:
            continue
        doc = _read_json(entry)
        cycle_id = doc.get("cycle_id") if isinstance(doc, dict) else entry.stem
        recorded_on = doc.get("recorded_on") if isinstance(doc, dict) else ""
        manifest_rel = doc.get("manifest_path") if isinstance(doc, dict) else None
        rows.append((str(recorded_on or ""), str(cycle_id or entry.stem), manifest_rel))
    rows.sort(key=lambda row: (row[0], row[1]))
    for _recorded_on, cycle_id, manifest_rel in rows[:limit]:
        if not isinstance(manifest_rel, str):
            continue
        record = _cycle_record(root, cycle_id)
        manifest_path = root / manifest_rel
        if record is None or not manifest_path.is_file():
            continue
        try:
            _emit_single_locked(root, record, manifest_path)
        except Exception:
            continue


def _emit_cycle_locked(root: Path, record: Mapping[str, Any], manifest_path: Path, *, drain_limit: int) -> None:
    _emit_single_locked(root, record, manifest_path)
    _drain_pending(root, exclude_cycle_id=record.get("cycle_id"), limit=drain_limit)


def emit_after_seal_locked(root: Path, record: Mapping[str, Any], document: Mapping[str, Any],
                           manifest_path: Path) -> None:
    """Called at the tail of `_commit_sealed`, admission lock already held.

    Never raises: a failure becomes a `deferred:<...>` marker under
    `cycle-title-emissions/`, retried on the next seal's drain pass or by
    `backfill`. Never re-acquires the admission lock (the caller holds it;
    `dispatch_lock_order` refuses re-entry).
    """
    root = Path(root)
    manifest_path = Path(manifest_path)
    try:
        _emit_cycle_locked(root, record, manifest_path, drain_limit=SEAL_DRAIN_LIMIT)
    except Exception as exc:
        _write_marker_quiet(root, record.get("cycle_id"), f"deferred:{type(exc).__name__}", manifest_path)


# ---------------------------------------------------------------------------
# Cairn reader gate
# ---------------------------------------------------------------------------


def _resolve_reader_dir(reader_dir: Optional[Path]) -> Optional[Path]:
    if reader_dir is not None:
        candidate = Path(reader_dir)
    else:
        doc = _read_json(Path.home() / ".config" / "cairn-sync" / "config.json")
        app_dir = doc.get("app_dir") if isinstance(doc, dict) else None
        if not isinstance(app_dir, str):
            return None
        candidate = Path(app_dir)
    cli = candidate / "node_modules" / "tsx" / "dist" / "cli.mjs"
    driver_source = candidate / "lib" / "artifact-projection" / "campaign-metadata-amendments.ts"
    if not candidate.is_dir() or not cli.is_file() or not driver_source.is_file():
        return None
    return candidate


def run_cairn_reader(candidate_bytes: bytes, root_id: str, repository_id: str, *,
                     reader_dir: Optional[Path] = None) -> ReaderResult:
    app_dir = _resolve_reader_dir(reader_dir)
    if app_dir is None:
        return ReaderResult("unavailable", frozenset(), "reader-app-dir-unavailable", None)
    tmp = tempfile.mkdtemp(prefix="cycle-title-gate-")
    try:
        tmp_root = Path(tmp) / "root"
        declaration_path = tmp_root / CYCLE_TITLES_REL
        declaration_path.parent.mkdir(parents=True, exist_ok=True)
        declaration_path.write_bytes(candidate_bytes)
        driver_source = (app_dir / "lib" / "artifact-projection" / "campaign-metadata-amendments.ts").resolve()
        driver = Path(tmp) / "gate.mts"
        driver.write_text(
            "import { readCycleDisplayTitles } from " + json.dumps(driver_source.as_uri()) + ";\n"
            "const [root, rootId, repoId] = process.argv.slice(2);\n"
            "const map = await readCycleDisplayTitles(root, rootId, repoId);\n"
            "console.log(JSON.stringify([...map.keys()]));\n",
            encoding="utf-8",
        )
        cli = app_dir / "node_modules" / "tsx" / "dist" / "cli.mjs"
        try:
            proc = subprocess.run(
                ["node", str(cli), str(driver), str(tmp_root), root_id, repository_id],
                cwd=str(app_dir), timeout=120, capture_output=True, text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ReaderResult("unavailable", frozenset(), f"reader-invoke-failed:{exc}", str(app_dir))
        if proc.returncode != 0:
            output = proc.stderr or proc.stdout or ""
            detail = output.splitlines()[0] if output else "reader-nonzero-exit"
            return ReaderResult("rejected", frozenset(), detail, str(app_dir))
        try:
            accepted = json.loads(proc.stdout)
        except ValueError:
            return ReaderResult("rejected", frozenset(), "reader-output-invalid", str(app_dir))
        if not isinstance(accepted, list):
            return ReaderResult("rejected", frozenset(), "reader-output-invalid", str(app_dir))
        return ReaderResult("accepted", frozenset(str(item) for item in accepted), "", str(app_dir))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ReaderFn = Callable[..., ReaderResult]


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def _backfill_journal_path(root: Path, post_digest: str, *, now: Optional[float] = None) -> Path:
    when = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    short = post_digest.removeprefix("sha256:")[:8]
    return root / BACKFILL_JOURNAL_REL / f"{stamp}-{short}.json"


def backfill(root: Path, *, apply: bool = False, report_path: Optional[Path] = None,
             out_path: Optional[Path] = None, reader: Optional[ReaderFn] = None,
             reader_dir: Optional[Path] = None, expect_post_digest: Optional[str] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    root = Path(root).resolve()
    identity = _load_root_identity(root)
    declaration_path = root / CYCLE_TITLES_REL
    try:
        pre_doc = check_declaration_file(declaration_path, root_id=identity.artifact_root_id,
                                        repository_id=identity.repository_id)
    except CycleTitlesError as exc:
        return {"status": "refused:sidecar-invalid", "detail": exc.detail, "artifact_root": str(root)}
    # Digest the raw on-disk bytes (b"" when the file is absent), never a
    # re-rendered "empty" skeleton -- the later in-lock drift check re-reads
    # the same way, and a missing file must compare equal to itself, not to
    # a synthesized empty-entries document it never actually held.
    pre_raw = declaration_path.read_bytes() if declaration_path.is_file() else b""
    pre_digest = record_digest(pre_raw)
    existing_by_cycle = {row["cycle_id"]: row for row in pre_doc["entries"]}

    manifests = _walk_manifests(root)
    counts: Dict[str, Any] = {
        "sealed_records": 0, "eligible": 0, "ineligible": {}, "existing_kept": 0, "existing_dropped": 0,
        "emitted_by_source": {"record-title": 0, "route-task": 0, "primary-heading": 0}, "unassigned": {},
    }
    rows: List[Dict[str, Any]] = []
    eligible_items: List[Dict[str, Any]] = []
    handled: Set[str] = set()

    def _drop_existing(cycle_id: str, campaign_id: Any) -> None:
        if cycle_id in existing_by_cycle:
            counts["existing_dropped"] += 1
            rows.append({"cycle_id": cycle_id, "campaign_id": campaign_id, "verdict": "ineligible",
                        "reason": "existing-entry-ineligible"})

    for cycle_id, paths in manifests.items():
        if cycle_id == "__unparseable__":
            counts["ineligible"]["manifest-unparseable"] = counts["ineligible"].get("manifest-unparseable", 0) + len(paths)
            continue
        handled.add(cycle_id)
        if len(paths) > 1:
            counts["ineligible"]["manifest-ambiguous"] = counts["ineligible"].get("manifest-ambiguous", 0) + 1
            _drop_existing(cycle_id, existing_by_cycle.get(cycle_id, {}).get("campaign_id"))
            continue
        manifest_path = paths[0]
        record = _cycle_record(root, cycle_id)
        if record is not None and record.get("state") in {"sealed", "superseded"}:
            counts["sealed_records"] += 1
        try:
            raw = manifest_path.read_bytes()
            parsed = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            counts["ineligible"]["manifest-unparseable"] = counts["ineligible"].get("manifest-unparseable", 0) + 1
            _drop_existing(cycle_id, existing_by_cycle.get(cycle_id, {}).get("campaign_id"))
            continue
        code = _eligibility_check(record, raw, parsed, identity)
        if code is not None:
            counts["ineligible"][code] = counts["ineligible"].get(code, 0) + 1
            _drop_existing(cycle_id, existing_by_cycle.get(cycle_id, {}).get("campaign_id")
                          or (record or {}).get("campaign_id"))
            continue
        counts["eligible"] += 1
        sealed_on = record.get("sealed_on") if isinstance(record.get("sealed_on"), str) else None
        eligible_items.append({
            "cycle_id": cycle_id, "campaign_id": record.get("campaign_id"), "record": record,
            "manifest_path": manifest_path, "parsed": parsed, "sealed_on": sealed_on or "~",
        })

    for cycle_id, existing in existing_by_cycle.items():
        if cycle_id not in handled:
            _drop_existing(cycle_id, existing.get("campaign_id"))

    eligible_items.sort(key=lambda item: (item["sealed_on"], item["cycle_id"]))
    eligible_ids = {item["cycle_id"] for item in eligible_items}
    reserved: Dict[str, Set[str]] = {}
    for cycle_id in eligible_ids:
        existing = existing_by_cycle.get(cycle_id)
        if existing is not None:
            reserved.setdefault(existing["campaign_id"], set()).add(_norm(existing["display_title"]))

    new_entries: List[Dict[str, Any]] = []
    for item in eligible_items:
        cycle_id = item["cycle_id"]
        campaign_id = item["campaign_id"]
        existing = existing_by_cycle.get(cycle_id)
        if existing is not None:
            counts["existing_kept"] += 1
            new_entries.append({"campaign_id": campaign_id, "cycle_id": cycle_id,
                                "display_title": existing["display_title"],
                                "manifest_bindings": existing["manifest_bindings"]})
            rows.append({"cycle_id": cycle_id, "campaign_id": campaign_id, "source": "existing",
                        "title": existing["display_title"], "verdict": "kept"})
            continue
        campaign_json = _campaign_json_for(item["manifest_path"])
        ctx = CycleContext(
            record=item["record"], manifest=item["parsed"], cycle_dir=item["manifest_path"].parent,
            campaign=campaign_json, v2_title=_v2_title(root, campaign_id),
            route_text=_route_text(root, item["record"]),
        )
        reserved_set = reserved.setdefault(campaign_id, set())
        decision = derive_display_title(ctx, existing_title=None, reserved=reserved_set)
        if decision.title is None:
            reason = decision.reason or "no-source"
            counts["unassigned"][reason] = counts["unassigned"].get(reason, 0) + 1
            rows.append({"cycle_id": cycle_id, "campaign_id": campaign_id, "verdict": "unassigned",
                        "reason": reason, "rejected": list(decision.rejected)})
            continue
        reserved_set.add(_norm(decision.title))
        counts["emitted_by_source"][decision.source] = counts["emitted_by_source"].get(decision.source, 0) + 1
        binding = {"manifest_revision_id": str(item["parsed"].get("manifest_revision_id")),
                  "manifest_digest": binding_digest(item["parsed"])}
        new_entries.append({"campaign_id": campaign_id, "cycle_id": cycle_id, "display_title": decision.title,
                            "manifest_bindings": [binding]})
        rows.append({"cycle_id": cycle_id, "campaign_id": campaign_id, "source": decision.source,
                    "title": decision.title, "verdict": "emitted", "rejected": list(decision.rejected)})

    candidate_bytes = render_declaration(identity.artifact_root_id, identity.repository_id, new_entries)
    validate_declaration(candidate_bytes, root_id=identity.artifact_root_id, repository_id=identity.repository_id)
    post_digest = record_digest(candidate_bytes)
    reader_fn = reader or run_cairn_reader
    reader_result = reader_fn(candidate_bytes, identity.artifact_root_id, identity.repository_id, reader_dir=reader_dir)
    candidate_ids = {row["cycle_id"] for row in new_entries}

    result: Dict[str, Any] = {
        "artifact_root": str(root), "pre_digest": pre_digest, "post_digest": post_digest, "counts": counts,
        "reader": {"status": reader_result.status, "app_dir": reader_result.app_dir,
                  "accepted_count": len(reader_result.accepted_cycle_ids)},
        "rows": sorted(rows, key=lambda row: (str(row.get("campaign_id")), str(row.get("cycle_id")))),
    }

    if report_path is not None:
        report_path = Path(report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(_render_report(result), encoding="utf-8")
    if out_path is not None:
        out_resolved = Path(out_path).resolve()
        try:
            out_resolved.relative_to(root)
        except ValueError:
            pass
        else:
            raise CycleTitlesError("out-inside-root")
        out_resolved.parent.mkdir(parents=True, exist_ok=True)
        out_resolved.write_bytes(candidate_bytes)

    if not apply:
        result["status"] = "dry-run"
        return result
    if reader_result.status != "accepted" or candidate_ids != reader_result.accepted_cycle_ids:
        result["status"] = ("refused:reader-unavailable" if reader_result.status == "unavailable"
                            else "refused:reader-rejected")
        return result
    if post_digest == pre_digest:
        result["status"] = "unchanged"
        return result
    if expect_post_digest is not None and expect_post_digest != post_digest:
        result["status"] = "refused:post-digest-mismatch"
        return result

    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        current = declaration_path.read_bytes() if declaration_path.is_file() else b""
        if record_digest(current) != pre_digest:
            result["status"] = "refused:declaration-drift"
            return result
        for item in eligible_items:
            if item["cycle_id"] not in candidate_ids:
                continue
            raw_now = item["manifest_path"].read_bytes()
            if record_digest(raw_now) != item["record"].get("manifest_digest"):
                result["status"] = "refused:declaration-drift"
                return result
        journal_path = _backfill_journal_path(root, post_digest, now=now)
        journal = {
            "schema": BACKFILL_JOURNAL_SCHEMA, "pre_exists": declaration_path.is_file(),
            "pre_bytes_b64": base64.b64encode(current).decode("ascii"), "pre_digest": pre_digest,
            "post_digest": post_digest, "entry_count": len(new_entries), "recorded_on": _now_rfc3339(now),
        }
        CTR.write_atomic(journal_path, journal)
        CTR.write_atomic_bytes(declaration_path, candidate_bytes)
        verify_raw = declaration_path.read_bytes()
        try:
            if record_digest(verify_raw) != post_digest:
                raise CycleTitlesError("post-verify-digest-mismatch")
            validate_declaration(verify_raw, root_id=identity.artifact_root_id, repository_id=identity.repository_id)
        except CycleTitlesError:
            CTR.write_atomic_bytes(declaration_path, current)
            result["status"] = "refused:post-verify-failed"
            return result
        for cycle_id in existing_by_cycle:
            if cycle_id not in candidate_ids:
                _remove_marker(root, cycle_id)
        for cycle_id in candidate_ids:
            _remove_marker(root, cycle_id)
        result["status"] = "applied"
        result["journal"] = str(journal_path)
        return result
    finally:
        artifact_admission._release_lock(root, lock_fd)


def restore_backfill(root: Path, journal_path: Path) -> Dict[str, Any]:
    root = Path(root).resolve()
    journal = _read_json(Path(journal_path))
    if not isinstance(journal, dict) or journal.get("schema") != BACKFILL_JOURNAL_SCHEMA:
        raise CycleTitlesError("journal-invalid")
    declaration_path = root / CYCLE_TITLES_REL
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        current = declaration_path.read_bytes() if declaration_path.is_file() else b""
        if record_digest(current) != journal.get("post_digest"):
            return {"status": "refused:restore-drift"}
        if journal.get("pre_exists"):
            CTR.write_atomic_bytes(declaration_path, base64.b64decode(str(journal["pre_bytes_b64"])))
        else:
            try:
                declaration_path.unlink()
            except FileNotFoundError:
                pass
        return {"status": "restored"}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _reason_cell(row: Mapping[str, Any]) -> str:
    """Unassigned rows show the full rejection chain, not just the terminal
    reason, so the reader can see why every earlier source lost out too."""
    if row.get("verdict") == "unassigned":
        rejected = row.get("rejected") or []
        if rejected:
            return ", ".join(f"{source}:{code}" for source, code in rejected)
    return str(row.get("reason", ""))


def _render_report(result: Mapping[str, Any]) -> str:
    lines = [
        "# Cycle display-title backfill", "",
        f"- artifact_root: `{result['artifact_root']}`",
        f"- pre_digest: `{result['pre_digest']}`",
        f"- post_digest: `{result['post_digest']}`",
        f"- reader: `{json.dumps(result['reader'], sort_keys=True)}`",
        f"- counts: `{json.dumps(result['counts'], sort_keys=True)}`",
        "",
        "| cycle_id | campaign_id | verdict | source | title | reason |",
        "|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        cells = [str(row.get(key, "")) for key in ("cycle_id", "campaign_id", "verdict", "source", "title")]
        cells.append(_reason_cell(row))
        lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0], allow_abbrev=False)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--reader-dir", type=Path)
    parser.add_argument("--expect-post-digest")
    parser.add_argument("--restore-journal", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.restore_journal is not None:
            result = restore_backfill(args.artifact_root, args.restore_journal)
        else:
            result = backfill(args.artifact_root, apply=args.apply, report_path=args.report, out_path=args.out,
                              reader_dir=args.reader_dir, expect_post_digest=args.expect_post_digest)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if str(result.get("status", "")).startswith("refused") else 0
    except CycleTitlesError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
