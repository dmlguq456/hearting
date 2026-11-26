#!/usr/bin/env python3
"""Prepare, apply, and verify producer-owned campaign display-title repairs; auto-title untitled campaigns.

The mutable campaign record is updated for future producer runs, while the
sealed cycle manifests are treated as immutable evidence.  Cairn consumes the
sidecar declaration written by this module to render the repaired title for
historical manifests without rewriting their content-addressed identity.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import artifact_admission  # noqa: E402
import artifact_locator  # noqa: E402
import artifact_manifest  # noqa: E402

DISPLAY_TITLE_REL = Path(".runtime/artifact-producer/v1/campaign-display-titles.json")
PACKAGE_SCHEMA = "hearting-campaign-title-repair/v1"
DECLARATION_SCHEMA = "hearting-campaign-display-titles/v2"
REVIEW_SCHEMA = "hearting-campaign-title-review/v1"
DEFAULT_REVIEW_STATE = Path.home() / ".local/state/hearting/campaign-title-repair/v1/campaign-title-review-package.json"
MAX_TITLE_LENGTH = 34
FORBIDDEN_GENERIC_TITLES = {
    "w7c delta migration",
    "legacy project material",
    "legacy support residue",
    "unassigned — cross-campaign residue",
    "_unassigned",
}
DEFAULT_RULESET = "convention-2026-09-11-cadf02"
# Repair callers wait for the shared declaration lock this long; tests shorten it.
REPAIR_LOCK_TIMEOUT = artifact_admission.LOCK_TIMEOUT_DEFAULT

# Automatic display titles (see "automatic display titles" below).
AUTO_DISABLE_ENV = "HEARTING_CAMPAIGN_TITLE_AUTO"


class RepairError(Exception):
    pass


class DeclarationLockBusy(RepairError):
    pass


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_json(value: Any) -> str:
    return digest_bytes(canonical(value))


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise RepairError(f"json-read-failed:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise RepairError(f"json-object-required:{path}")
    return value


def write_atomic(path: Path, value: Mapping[str, Any]) -> None:
    write_atomic_bytes(path, canonical(value) + b"\n")


def write_atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    preserved_mode: int | None = None
    try:
        current = os.lstat(path)
        if stat.S_ISREG(current.st_mode):
            preserved_mode = stat.S_IMODE(current.st_mode)
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        if preserved_mode is not None:
            os.chmod(tmp_name, preserved_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _declaration_lock(root: Path, timeout: float | None = None):
    """The per-root producer admission lock every declaration writer takes.

    Held only across a read-decide-write of the declaration, never across a model call.
    Acquisition failure is `DeclarationLockBusy`; the callers decide how to report it.
    """
    root = Path(root)
    wait = REPAIR_LOCK_TIMEOUT if timeout is None else timeout
    try:
        fd = artifact_admission._acquire_lock(root, wait)
    except (artifact_admission.AdmissionBusy, artifact_admission.dispatch_lock_order.LockOrderError) as exc:
        raise DeclarationLockBusy("declaration-lock-busy") from exc
    try:
        yield
    finally:
        artifact_admission._release_lock(root, fd)


def render_review(review: Mapping[str, Any]) -> str:
    entries = review.get("entries")
    if not isinstance(entries, list):
        raise RepairError("review-entries-required")
    lines = [
        "# Campaign title review",
        "",
        f"- schema: `{review.get('schema')}`",
        f"- campaigns: `{len(entries)}`",
        "- state: `review-only`; no campaign, manifest, or production DB write",
        "",
        "| 루트 ID | campaign locator | 기존 제목 | 현재 제목 | 승인 후보 | 상태 |",
        "|---|---|---|---|---|---|",
    ]
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RepairError("review-entry-object-required")
        values = [
            str(entry.get("artifact_root_id", "")),
            str(entry.get("campaign_locator", "")),
            str(entry.get("original_title", "")).replace("|", "\\|"),
            str(entry.get("current_title", "")).replace("|", "\\|"),
            str(entry.get("display_title", "")).replace("|", "\\|"),
            str(entry.get("state", "")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"


def manifest_rows(campaign_dir: Path) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    campaign_dir = Path(campaign_dir)
    artifact_root = campaign_dir.parent.parent
    campaign_binding = campaign_dir.relative_to(artifact_root).as_posix()
    for cycle_dir, _layout in artifact_locator.iter_cycle_dirs(campaign_dir):
        manifest_path = cycle_dir / "manifest.json"
        try:
            observed = os.lstat(manifest_path)
        except OSError:
            continue
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
            continue
        classification = artifact_manifest.classify_artifact_path(
            str(artifact_root), campaign_binding,
            cycle_dir.relative_to(artifact_root).as_posix(), "control",
            manifest_path.relative_to(artifact_root).as_posix(), "regular",
        )
        if not classification.allowed:
            continue
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("campaign"), dict):
            raise RepairError(f"manifest-campaign-missing:{manifest_path}")
        campaign = manifest["campaign"]
        rows.append({
            "manifest_path": str(manifest_path),
            "manifest_id": manifest.get("manifest_id"),
            "manifest_revision_id": manifest.get("manifest_revision_id"),
            # Cairn hashes the parsed manifest's canonical JSON, not the source
            # file's formatting bytes.  Keep this contract identical on both sides.
            "manifest_digest": digest_json(manifest),
            "campaign_id": campaign.get("campaign_id"),
            "campaign_title": campaign.get("title", ""),
            "cycle_id": manifest["cycle"].get("cycle_id") if isinstance(manifest.get("cycle"), dict) else None,
        })
    return rows


def manifest_binding_fields(manifests: Iterable[Mapping[str, Any]]) -> tuple[list[Dict[str, str]], list[str], list[str]]:
    """Return one deterministic revision/digest pair ordering and its compatibility arrays."""
    bindings = [
        {
            "manifest_revision_id": str(row.get("manifest_revision_id", "")),
            "manifest_digest": str(row.get("manifest_digest", "")),
        }
        for row in manifests
    ]
    if any(not row["manifest_revision_id"] or not row["manifest_digest"] for row in bindings):
        raise RepairError("manifest-binding-value-missing")
    bindings.sort(key=lambda row: (row["manifest_revision_id"], row["manifest_digest"]))
    revisions = [row["manifest_revision_id"] for row in bindings]
    digests = [row["manifest_digest"] for row in bindings]
    if len(set(revisions)) != len(revisions) or len(set(digests)) != len(digests):
        raise RepairError("manifest-binding-duplicate")
    return bindings, revisions, digests


def entry_manifest_bindings(entry: Mapping[str, Any]) -> list[tuple[str, str]]:
    raw = entry.get("manifest_bindings")
    if not isinstance(raw, list):
        raise RepairError("manifest-binding-pairs-required")
    pairs: list[tuple[str, str]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise RepairError("manifest-binding-pair-object-required")
        revision = str(row.get("manifest_revision_id", ""))
        digest = str(row.get("manifest_digest", ""))
        if not revision or not digest:
            raise RepairError("manifest-binding-pair-value-required")
        pairs.append((revision, digest))
    if pairs != sorted(pairs) or len({revision for revision, _ in pairs}) != len(pairs) or len({digest for _, digest in pairs}) != len(pairs):
        raise RepairError("manifest-binding-pairs-not-canonical")
    return pairs


def manifest_bindings_contain(actual: Sequence[Mapping[str, Any]], anchors: Sequence[tuple[str, str]]) -> bool:
    observed = {(str(row.get("manifest_revision_id", "")), str(row.get("manifest_digest", ""))) for row in actual}
    return all(anchor in observed for anchor in anchors)


def iter_campaign_records(root: Path) -> Iterable[tuple[Path, Dict[str, Any]]]:
    campaigns = root / "campaigns"
    for campaign_json in sorted(campaigns.glob("*/campaign.json")):
        record = read_json(campaign_json)
        if not record.get("campaign_id"):
            raise RepairError(f"campaign-id-missing:{campaign_json}")
        yield campaign_json, record


def proposal_map(proposals_path: Path) -> Dict[tuple[str, str], str]:
    proposals_doc = read_json(proposals_path)
    proposals = proposals_doc.get("entries")
    if not isinstance(proposals, list):
        raise RepairError("proposals-entries-required")
    result: Dict[tuple[str, str], str] = {}
    for row in proposals:
        if not isinstance(row, dict):
            raise RepairError("proposal-row-object-required")
        key = (str(row.get("artifact_root_id", "")), str(row.get("campaign_locator", "")))
        title = str(row.get("display_title", "")).strip()
        if not all(key) or not title:
            raise RepairError("proposal-root-locator-title-required")
        if key in result:
            raise RepairError(f"proposal-duplicate:{key[0]}:{key[1]}")
        result[key] = title
    return result


def validate_titles(entries: Sequence[Mapping[str, Any]], expected_count: int | None = None) -> None:
    if expected_count is not None and len(entries) != expected_count:
        raise RepairError(f"campaign-count-mismatch:expected={expected_count}:observed={len(entries)}")
    titles = [str(entry.get("display_title", "")).strip() for entry in entries]
    if any(not title for title in titles):
        raise RepairError("display-title-empty")
    too_long = [(str(entry.get("campaign_locator", "")), len(title)) for entry, title in zip(entries, titles) if len(title) > MAX_TITLE_LENGTH]
    if too_long:
        raise RepairError("display-title-too-long:" + ",".join(f"{locator}={length}" for locator, length in too_long))
    forbidden = [(str(entry.get("campaign_locator", "")), title) for entry, title in zip(entries, titles) if title.casefold() in FORBIDDEN_GENERIC_TITLES]
    if forbidden:
        raise RepairError("display-title-forbidden:" + ",".join(f"{locator}={title}" for locator, title in forbidden))
    seen: Dict[str, str] = {}
    for entry, title in zip(entries, titles):
        key = title.casefold()
        if key in seen:
            raise RepairError(f"display-title-duplicate:{seen[key]}:{entry.get('campaign_locator')}:{title}")
        seen[key] = str(entry.get("campaign_locator", ""))


def review_snapshot(root_declaration: Path, proposals_path: Path, applied_package_path: Path) -> Dict[str, Any]:
    roots_doc = read_json(root_declaration)
    proposals = proposal_map(proposals_path)
    applied = read_json(applied_package_path)
    if applied.get("schema") != PACKAGE_SCHEMA:
        raise RepairError("applied-package-schema-mismatch")
    applied_entries = applied.get("entries")
    if not isinstance(applied_entries, list):
        raise RepairError("applied-package-entries-required")
    applied_by_key = {(str(row.get("artifact_root_id")), str(row.get("campaign_locator"))): row for row in applied_entries if isinstance(row, Mapping)}

    roots: list[Dict[str, Any]] = []
    entries: list[Dict[str, Any]] = []
    reviewed_keys: set[tuple[str, str]] = set()
    unreviewed_campaigns: list[Dict[str, str]] = []
    for root in roots_doc.get("roots", []):
        root_id = str(root.get("artifact_root_id", ""))
        root_path = Path(str(root.get("artifact_root_path", ""))).resolve()
        if not root_id or not root_path.is_dir():
            raise RepairError(f"root-invalid:{root_id}:{root_path}")
        roots.append({"artifact_root_id": root_id, "artifact_root_path": str(root_path)})
        for campaign_json, record in iter_campaign_records(root_path):
            locator = campaign_json.parent.name
            key = (root_id, locator)
            if key not in applied_by_key:
                unreviewed_campaigns.append({"artifact_root_id": root_id, "campaign_locator": locator, "campaign_id": str(record.get("campaign_id", ""))})
                continue
            reviewed_keys.add(key)
            if key not in proposals:
                raise RepairError(f"proposal-missing:{root_id}:{locator}")
            manifests = manifest_rows(campaign_json.parent)
            campaign_id = str(record["campaign_id"])
            if any(str(row.get("campaign_id")) != campaign_id for row in manifests):
                raise RepairError(f"manifest-campaign-id-mismatch:{campaign_id}")
            prior = applied_by_key.get(key)
            prior_original = prior.get("original_title") if prior else None
            original_title = str(prior_original) if prior_original not in (None, "") else (str(prior.get("old_campaign_title")) if prior else str(record.get("title", "")))
            bindings, revisions, digests = manifest_binding_fields(manifests)
            entries.append({
                "artifact_root_id": root_id,
                "artifact_root_path": str(root_path),
                "campaign_id": campaign_id,
                "campaign_locator": locator,
                "campaign_json_path": str(campaign_json),
                "baseline_campaign_json_digest": str(prior.get("campaign_json_digest")) if prior else digest_bytes(campaign_json.read_bytes()),
                "original_title": original_title,
                "current_title": str(record.get("title", "")),
                "display_title": proposals[key],
                "manifest_titles": sorted({str(row.get("campaign_title", "")) for row in manifests}),
                "manifest_bindings": bindings,
                "manifest_revision_ids": revisions,
                "manifest_digests": digests,
                "state": "already-applied" if prior and str(record.get("title", "")) == proposals[key] else ("quality-update-pending" if prior else "new-pending"),
            })
    missing = sorted(set(applied_by_key) - reviewed_keys)
    if missing:
        raise RepairError("applied-campaign-missing:" + ",".join(f"{root}:{locator}" for root, locator in missing))
    unknown = sorted(set(proposals) - reviewed_keys)
    if unknown:
        raise RepairError("proposal-unknown:" + ",".join(f"{root}:{locator}" for root, locator in unknown))
    entries.sort(key=lambda row: (row["artifact_root_id"], row["campaign_locator"]))
    validate_titles(entries, expected_count=len(reviewed_keys))
    return {
        "schema": REVIEW_SCHEMA,
        "ruleset": "convention-2026-09-11-cadf02",
        "approval_required": True,
        "apply_state": "review-only",
        "artifact_roots": roots,
        "source_applied_package": str(applied_package_path.resolve()),
        "unreviewed_campaigns": sorted(unreviewed_campaigns, key=lambda row: (row["artifact_root_id"], row["campaign_locator"])),
        "entries": entries,
    }


def prepare(root_declaration: Path, proposals_path: Path, allow_unlisted: bool = False) -> Dict[str, Any]:
    roots_doc = read_json(root_declaration)
    proposals_doc = read_json(proposals_path)
    proposals = proposals_doc.get("entries")
    if not isinstance(proposals, list):
        raise RepairError("proposals-entries-required")
    proposal_map: Dict[tuple[str, str], str] = {}
    for row in proposals:
        if not isinstance(row, dict):
            raise RepairError("proposal-row-object-required")
        key = (str(row.get("artifact_root_id", "")), str(row.get("campaign_locator", "")))
        title = str(row.get("display_title", "")).strip()
        if not all(key) or not title:
            raise RepairError("proposal-root-locator-title-required")
        if key in proposal_map:
            raise RepairError(f"proposal-duplicate:{key[0]}:{key[1]}")
        proposal_map[key] = title

    package_roots: list[Dict[str, Any]] = []
    package_entries: list[Dict[str, Any]] = []
    excluded_campaigns: list[Dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    for root in roots_doc.get("roots", []):
        root_id = str(root.get("artifact_root_id", ""))
        root_path = Path(str(root.get("artifact_root_path", ""))).resolve()
        if not root_id or not root_path.is_dir():
            raise RepairError(f"root-invalid:{root_id}:{root_path}")
        package_roots.append({"artifact_root_id": root_id, "artifact_root_path": str(root_path)})
        for campaign_json, record in iter_campaign_records(root_path):
            locator = campaign_json.parent.name
            key = (root_id, locator)
            if key not in proposal_map:
                if allow_unlisted:
                    excluded_campaigns.append({"artifact_root_id": root_id, "campaign_locator": locator})
                    continue
                raise RepairError(f"proposal-missing:{root_id}:{locator}")
            seen_keys.add(key)
            manifests = manifest_rows(campaign_json.parent)
            campaign_id = str(record["campaign_id"])
            if any(str(row.get("campaign_id")) != campaign_id for row in manifests):
                raise RepairError(f"manifest-campaign-id-mismatch:{campaign_id}")
            bindings, revisions, digests = manifest_binding_fields(manifests)
            package_entries.append({
                "artifact_root_id": root_id,
                "artifact_root_path": str(root_path),
                "campaign_id": campaign_id,
                "campaign_locator": locator,
                "campaign_json_path": str(campaign_json),
                "campaign_json_digest": digest_bytes(campaign_json.read_bytes()),
                "old_campaign_title": str(record.get("title", "")),
                "manifest_titles": sorted({str(row.get("campaign_title", "")) for row in manifests}),
                "manifest_bindings": bindings,
                "manifest_revision_ids": revisions,
                "manifest_digests": digests,
                "display_title": proposal_map[key],
            })
    unknown = sorted(set(proposal_map) - seen_keys)
    if unknown:
        raise RepairError("proposal-unknown:" + ",".join(f"{root}:{locator}" for root, locator in unknown))
    return {
        "schema": PACKAGE_SCHEMA,
        "ruleset": "convention-2026-09-11-cadf02",
        "artifact_roots": package_roots,
        "entries": sorted(package_entries, key=lambda row: (row["artifact_root_id"], row["campaign_locator"])),
        "excluded_campaigns": sorted(excluded_campaigns, key=lambda row: (row["artifact_root_id"], row["campaign_locator"])),
    }


def _apply_drift_check(entry: Mapping[str, Any]) -> tuple[Path, Dict[str, Any]]:
    campaign_json = Path(str(entry["campaign_json_path"])).resolve()
    if digest_bytes(campaign_json.read_bytes()) != entry["campaign_json_digest"]:
        raise RepairError(f"campaign-json-drift:{campaign_json}")
    record = read_json(campaign_json)
    if str(record.get("campaign_id")) != str(entry["campaign_id"]):
        raise RepairError(f"campaign-id-drift:{campaign_json}")
    if str(record.get("title", "")) != str(entry["old_campaign_title"]):
        raise RepairError(f"campaign-title-drift:{campaign_json}")
    manifests = manifest_rows(campaign_json.parent)
    bindings, _revisions, _digests = manifest_binding_fields(manifests)
    if not manifest_bindings_contain(bindings, entry_manifest_bindings(entry)):
        raise RepairError(f"manifest-binding-drift:{campaign_json.parent}")
    return campaign_json, record


def apply(package: Mapping[str, Any]) -> Dict[str, Any]:
    if package.get("schema") != PACKAGE_SCHEMA:
        raise RepairError("package-schema-mismatch")
    entries = package.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RepairError("package-entries-required")
    if package.get("expected_campaign_count") is not None:
        if package.get("review_approved") is not True:
            raise RepairError("review-approval-required")
        validate_titles(entries, expected_count=int(package["expected_campaign_count"]))
    grouped: Dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RepairError("package-entry-object-required")
        grouped.setdefault(str(entry["artifact_root_id"]), []).append(entry)

    # Complete the read/compare phase before changing any campaign record.  A
    # later drift must never leave an earlier root partially repaired.
    checked: list[tuple[Mapping[str, Any], Path, Dict[str, Any]]] = []
    for entry in entries:
        campaign_json, record = _apply_drift_check(entry)
        checked.append((entry, campaign_json, record))

    journal_path = Path(str(package.get("transaction_journal_path", ""))).resolve() if package.get("transaction_journal_path") else None
    journal: Dict[str, Any] | None = None
    if journal_path:
        sidecars: list[Dict[str, Any]] = []
        for root_id, root_entries in grouped.items():
            sidecar_path = Path(str(root_entries[0]["artifact_root_path"])).resolve() / DISPLAY_TITLE_REL
            sidecars.append({
                "artifact_root_id": root_id,
                "path": str(sidecar_path),
                "exists": sidecar_path.exists(),
                "bytes_b64": base64.b64encode(sidecar_path.read_bytes()).decode("ascii") if sidecar_path.exists() else None,
            })
        journal = {
            "schema": "hearting-campaign-title-apply-journal/v1",
            "state": "prepared",
            "package_digest": digest_json(package),
            "campaigns": [{"path": str(path), "bytes_b64": base64.b64encode(path.read_bytes()).decode("ascii")} for _, path, _ in checked],
            "sidecars": sidecars,
        }
        write_atomic(journal_path, journal)

    changed = 0
    declarations = 0
    try:
        for root_id, root_entries in grouped.items():
            root_path = Path(str(root_entries[0]["artifact_root_path"])).resolve()
            declaration_path = root_path / DISPLAY_TITLE_REL
            # Same per-root lock the automatic writer takes: the declaration is read,
            # merged, and written without an automatic entry slipping in between, and a
            # person's title always replaces an automatic one for the same campaign.
            with _declaration_lock(root_path, REPAIR_LOCK_TIMEOUT):
                root_checked = [
                    (entry, *_apply_drift_check(entry))
                    for entry in sorted((row[0] for row in checked if str(row[0]["artifact_root_id"]) == root_id),
                                        key=lambda row: str(row["campaign_id"]))
                ]
                declaration = read_json(declaration_path) if declaration_path.exists() else {
                    "schema": DECLARATION_SCHEMA, "artifact_root_id": root_id,
                }
                declaration_entries = {str(row["campaign_id"]): row for row in declaration.get("entries", [])}
                for entry, campaign_json, record in root_checked:
                    updated = dict(record)
                    updated["title"] = str(entry["display_title"])
                    write_atomic(campaign_json, updated)
                    changed += int(str(entry["old_campaign_title"]) != str(entry["display_title"]))
                    declaration_entries[str(entry["campaign_id"])] = {
                        "campaign_id": str(entry["campaign_id"]),
                        "campaign_locator": str(entry["campaign_locator"]),
                        "display_title": str(entry["display_title"]),
                        "manifest_bindings": list(entry["manifest_bindings"]),
                        "manifest_revision_ids": list(entry["manifest_revision_ids"]),
                        "manifest_digests": list(entry["manifest_digests"]),
                    }
                declaration = {
                    **declaration,
                    "schema": DECLARATION_SCHEMA,
                    "artifact_root_id": root_id,
                    "ruleset": str(package.get("ruleset", "")),
                    "entries": [declaration_entries[key] for key in sorted(declaration_entries)],
                }
                write_atomic(root_path / DISPLAY_TITLE_REL, declaration)
            declarations += 1
        verify(package)
        if journal and journal_path:
            journal["state"] = "committed"
            write_atomic(journal_path, journal)
        return {"status": "applied", "campaigns": len(entries), "campaigns_changed": changed, "declarations": declarations}
    except Exception:
        if journal:
            for item in journal["campaigns"]:
                write_atomic_bytes(Path(str(item["path"])), base64.b64decode(str(item["bytes_b64"])))
            for item in journal["sidecars"]:
                sidecar_path = Path(str(item["path"]))
                if item["exists"]:
                    write_atomic_bytes(sidecar_path, base64.b64decode(str(item["bytes_b64"])))
                elif sidecar_path.exists():
                    os.unlink(sidecar_path)
            if journal_path:
                journal["state"] = "rolled-back"
                write_atomic(journal_path, journal)
        raise


def verify(package: Mapping[str, Any]) -> Dict[str, Any]:
    if package.get("schema") != PACKAGE_SCHEMA:
        raise RepairError("package-schema-mismatch")
    entries = package.get("entries")
    if not isinstance(entries, list):
        raise RepairError("package-entries-required")
    for entry in entries:
        campaign_json = Path(str(entry["campaign_json_path"])).resolve()
        record = read_json(campaign_json)
        if str(record.get("title")) != str(entry["display_title"]):
            raise RepairError(f"campaign-title-not-applied:{campaign_json}")
        bindings, _, _ = manifest_binding_fields(manifest_rows(campaign_json.parent))
        if not manifest_bindings_contain(bindings, entry_manifest_bindings(entry)):
            raise RepairError(f"manifest-binding-drift:{campaign_json.parent}")
    grouped: Dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(str(entry["artifact_root_id"]), []).append(entry)
    for root_id, root_entries in grouped.items():
        declaration_path = Path(str(root_entries[0]["artifact_root_path"])).resolve() / DISPLAY_TITLE_REL
        declaration = read_json(declaration_path)
        if declaration.get("schema") != DECLARATION_SCHEMA or declaration.get("artifact_root_id") != root_id:
            raise RepairError(f"sidecar-mismatch:{declaration_path}")
        actual_rows = declaration.get("entries")
        if not isinstance(actual_rows, list):
            raise RepairError(f"sidecar-entry-count-drift:{declaration_path}")
        expected = {
            str(entry["campaign_id"]): (str(entry["display_title"]), tuple(entry_manifest_bindings(entry)))
            for entry in root_entries
        }
        actual = {}
        for row in actual_rows:
            if not isinstance(row, Mapping):
                raise RepairError(f"sidecar-entry-invalid:{declaration_path}")
            campaign_id = str(row.get("campaign_id", ""))
            if campaign_id not in expected:
                continue
            if campaign_id in actual:
                raise RepairError(f"sidecar-entry-duplicate:{declaration_path}:{campaign_id}")
            actual[campaign_id] = (str(row.get("display_title", "")), tuple(entry_manifest_bindings(row)))
        if any(actual.get(key) != value for key, value in expected.items()):
            raise RepairError(f"sidecar-binding-drift:{declaration_path}")
    return {"status": "verified", "campaigns": len(entries)}


def rollback(package: Mapping[str, Any]) -> Dict[str, Any]:
    """Full repair rollback: restore original titles and remove this repair's sidecars.

    Transaction-failure rollback is separate: `apply` restores the pre-apply bytes from its
    journal. This command intentionally returns to the package's recorded original-title state.
    """
    if package.get("schema") != PACKAGE_SCHEMA:
        raise RepairError("package-schema-mismatch")
    entries = package.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RepairError("package-entries-required")
    grouped: Dict[str, list[Mapping[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise RepairError("package-entry-object-required")
        grouped.setdefault(str(entry["artifact_root_id"]), []).append(entry)

    checked: list[tuple[Mapping[str, Any], Path, Dict[str, Any]]] = []
    for entry in entries:
        campaign_json = Path(str(entry["campaign_json_path"])).resolve()
        record = read_json(campaign_json)
        if str(record.get("campaign_id")) != str(entry["campaign_id"]):
            raise RepairError(f"campaign-id-drift:{campaign_json}")
        if str(record.get("title", "")) != str(entry["display_title"]):
            raise RepairError(f"campaign-title-not-at-ap-state:{campaign_json}")
        manifests = manifest_rows(campaign_json.parent)
        bindings, _, _ = manifest_binding_fields(manifests)
        if [(row["manifest_revision_id"], row["manifest_digest"]) for row in bindings] != entry_manifest_bindings(entry):
            raise RepairError(f"manifest-binding-drift:{campaign_json.parent}")
        checked.append((entry, campaign_json, record))

    for root_id, root_entries in grouped.items():
        root_path = Path(str(root_entries[0]["artifact_root_path"])).resolve()
        declaration_path = root_path / DISPLAY_TITLE_REL
        declaration = read_json(declaration_path)
        if declaration.get("schema") != DECLARATION_SCHEMA or declaration.get("artifact_root_id") != root_id:
            raise RepairError(f"sidecar-mismatch:{declaration_path}")
        expected = {
            (str(entry["campaign_id"]), str(entry["display_title"]), tuple(entry_manifest_bindings(entry)))
            for entry in root_entries
        }
        actual = {
            (str(row.get("campaign_id")), str(row.get("display_title")), tuple(entry_manifest_bindings(row)))
            for row in declaration.get("entries", []) if isinstance(row, Mapping)
        }
        if not expected.issubset(actual):
            raise RepairError(f"sidecar-ownership-mismatch:{declaration_path}")

    for root_id, root_entries in grouped.items():
        root_path = Path(str(root_entries[0]["artifact_root_path"])).resolve()
        declaration_path = root_path / DISPLAY_TITLE_REL
        # The lock comes first so a busy root fails before any of its records change.
        with _declaration_lock(root_path, REPAIR_LOCK_TIMEOUT):
            for entry, campaign_json, record in checked:
                if str(entry["artifact_root_id"]) == root_id:
                    restored = dict(record)
                    restored["title"] = str(entry.get("original_title", entry["old_campaign_title"]))
                    write_atomic(campaign_json, restored)
            declaration = read_json(declaration_path)
            affected = {str(entry["campaign_id"]) for entry in root_entries}
            declaration["entries"] = [row for row in declaration.get("entries", [])
                                      if str(row.get("campaign_id")) not in affected]
            if declaration["entries"]:
                write_atomic(declaration_path, declaration)
            else:
                os.unlink(declaration_path)
    return {"status": "rolled-back", "rollback_kind": "full-original", "campaigns": len(entries), "declarations": len(grouped)}


def promote(review_path: Path, output: Path, confirmation: str) -> Dict[str, Any]:
    if confirmation != "APPROVE hearting-campaign-title-review/v1":
        raise RepairError("approval-confirmation-mismatch")
    review = read_json(review_path)
    if review.get("schema") != REVIEW_SCHEMA or review.get("approval_required") is not True:
        raise RepairError("review-package-invalid")
    review_entries = review.get("entries")
    if not isinstance(review_entries, list):
        raise RepairError("review-entries-required")
    entries: list[Dict[str, Any]] = []
    for row in review_entries:
        if not isinstance(row, Mapping):
            raise RepairError("review-entry-object-required")
        campaign_json = Path(str(row["campaign_json_path"])).resolve()
        record = read_json(campaign_json)
        if str(record.get("campaign_id")) != str(row["campaign_id"]):
            raise RepairError(f"campaign-id-drift:{campaign_json}")
        if str(record.get("title", "")) != str(row["current_title"]):
            raise RepairError(f"campaign-title-drift:{campaign_json}")
        manifests = manifest_rows(campaign_json.parent)
        bindings, revisions, digests = manifest_binding_fields(manifests)
        if [(item["manifest_revision_id"], item["manifest_digest"]) for item in bindings] != entry_manifest_bindings(row):
            raise RepairError(f"manifest-binding-drift:{campaign_json.parent}")
        entries.append({
            "artifact_root_id": str(row["artifact_root_id"]),
            "artifact_root_path": str(row["artifact_root_path"]),
            "campaign_id": str(row["campaign_id"]),
            "campaign_locator": str(row["campaign_locator"]),
            "campaign_json_path": str(campaign_json),
            "campaign_json_digest": digest_bytes(campaign_json.read_bytes()),
            "old_campaign_title": str(record.get("title", "")),
            "pre_apply_title": str(record.get("title", "")),
            "original_title": str(row.get("original_title", record.get("title", ""))),
            "baseline_campaign_json_digest": str(row.get("baseline_campaign_json_digest", "")),
            "manifest_titles": list(row["manifest_titles"]),
            "manifest_bindings": bindings,
            "manifest_revision_ids": revisions,
            "manifest_digests": digests,
            "display_title": str(row["display_title"]),
        })
    entries.sort(key=lambda row: (row["artifact_root_id"], row["campaign_locator"]))
    validate_titles(entries, expected_count=len(review_entries))
    package = {
        "schema": PACKAGE_SCHEMA,
        "ruleset": str(review.get("ruleset", "")),
        "review_approved": True,
        "approval_confirmation": confirmation,
        "expected_campaign_count": len(entries),
        "transaction_journal_path": str(DEFAULT_REVIEW_STATE.parent / "campaign-title-apply-journal.json"),
        "artifact_roots": review.get("artifact_roots", []),
        "entries": entries,
    }
    write_atomic(output, package)
    return {"status": "approved-package-prepared", "campaigns": len(entries), "package": str(output)}


# ---------------------------------------------------------------------------
# automatic display titles -- now one part of the unified background review
#
# Campaign titles, summaries, and tags are judged by ONE model call per
# campaign in `artifact_workflow_group_review`, and written to `campaigns/<locator>/meta.json`
# through `artifact_meta`.  This module keeps the explicit declaration repair above and thin
# compatibility entry points (`auto_title`, `backfill_roots`, `launch_after_seal`) that run
# that same job; it no longer owns a model call, a lock, a pending queue, or a detached
# follow-up.  The old declaration file is only read, never written: a title a person set
# there is respected by the unified writer.
# ---------------------------------------------------------------------------


class AutoTitleError(Exception):
    def __init__(self, failure_class: str, code: str = "") -> None:
        super().__init__(f"{failure_class}:{code}" if code else failure_class)
        self.failure_class = failure_class
        self.code = code


def auto_disabled(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(AUTO_DISABLE_ENV, "")).strip().lower() in {"off", "0", "false", "no", "disabled"}


def _default_invoke(prompt: str) -> tuple[str, Optional[str]]:
    import artifact_workflow_group_review as R
    return R._invoke_model(prompt)


def _campaign_locator(root: Path, campaign_id: str) -> str:
    for directory in artifact_locator.iter_campaign_dirs(Path(root)):
        try:
            if read_json(directory / "campaign.json").get("campaign_id") == campaign_id:
                return directory.name
        except RepairError:
            continue
    return ""


def _target_row(root: Path, report: Mapping[str, Any]) -> Dict[str, Any]:
    """One old-shape `targets` row from the review's per-campaign report."""
    campaign_id = str(report.get("campaign_id"))
    row: Dict[str, Any] = {"campaign_id": campaign_id, "campaign_locator": _campaign_locator(root, campaign_id),
                           "reason": "unified-review"}
    if report.get("harness"):
        row["harness"] = report["harness"]
    if report.get("status") == "failed":
        row.update(status="failed", failure_class=report.get("failure_class"), code=report.get("detail") or "")
        return row
    by_model = any(change["field"] == "campaign.title" and change["actor"] == "model"
                   for change in report.get("metadata_diff", []))
    title = ((report.get("metadata") or {}).get("campaign") or {}).get("title")
    if by_model:
        row.update(status="proposed" if report.get("status") == "dry-run" else "written", display_title=title)
    else:
        row.update(status="skipped", code="title-protected-or-unchanged")
    return row


def _latest_sealed(root: Path, campaign_ids: Sequence[str]) -> list[str]:
    import artifact_producer
    wanted = set(campaign_ids)
    latest: Dict[str, tuple] = {}
    for record in artifact_producer.list_cycle_records(Path(root)):
        campaign_id = record.get("campaign_id")
        if (campaign_id in wanted and artifact_producer.cycle_record_closed(record)
                and isinstance(record.get("sealed_on"), str)):
            stamp = (record["sealed_on"], str(record.get("cycle_id")))
            if campaign_id not in latest or stamp > latest[campaign_id]:
                latest[campaign_id] = stamp
    return [cycle_id for _stamp, cycle_id in latest.values()]


def auto_title(root: Path, *, campaign_ids: Sequence[str] = (), mode: str = "explicit", dry_run: bool = False,
               invoke: Optional[Callable[[str], tuple]] = None, limit: int | None = None,
               lock_timeout: float | None = None) -> Dict[str, Any]:
    """Compatibility entry point: run the unified review sweep and report its campaigns in the old shape.

    `mode="seal"` is the automatic sweep (enrollment, single-flight, pending, both kill switches) with
    each named campaign's newest sealed cycle as its trigger; any other mode is an explicit fill-in sweep
    over `campaign_ids`, or over everything when none are named, which skips cycles that already have
    metadata.  `limit` bounds the cycles judged.  `lock_timeout` is unused: the sweep's own locks apply."""
    import artifact_workflow_group_review as R
    root = Path(root)
    result: Dict[str, Any] = {"status": "dry-run" if dry_run else "ok", "artifact_root": str(root),
                              "targets": [], "protected": 0, "waiting": 0}
    auto = mode == "seal"
    swept = R.sweep(root, auto=auto, dry_run=dry_run, limit=limit, invoke=invoke or _default_invoke,
                    campaign_ids=() if auto else tuple(campaign_ids), missing_only=not auto,
                    cycles=_latest_sealed(root, campaign_ids) if auto else (),
                    since=None if (auto or campaign_ids) else R.MIN_STAMP)
    if swept.get("status") in ("busy", "disabled"):
        result["status"] = swept["status"]
        if swept["status"] == "busy" and campaign_ids:
            result["queued"] = list(campaign_ids)
        return result
    result["targets"] = [_target_row(root, report) for report in swept.get("campaigns", [])]
    result["waiting"] = len(swept.get("skipped", []))
    result["protected"] = sum(1 for row in result["targets"] if row["status"] == "skipped")
    return result


def backfill_roots(root_declaration: Path, *, dry_run: bool = False, limit: int | None = None,
                   invoke: Optional[Callable[[str], tuple]] = None,
                   lock_timeout: float | None = None) -> Dict[str, Any]:
    """`auto_title` over every root a declaration lists; a root whose identity differs is skipped."""
    import artifact_lifecycle
    roots_doc = read_json(root_declaration)
    listed = roots_doc.get("roots")
    if not isinstance(listed, list):
        raise RepairError("roots-required")
    rows: list[Dict[str, Any]] = []
    totals = {"targets": 0, "written": 0, "failed": 0, "proposed": 0, "skipped": 0, "protected": 0, "waiting": 0}
    for item in listed:
        item = item if isinstance(item, Mapping) else {}
        root_id = str(item.get("artifact_root_id", ""))
        path = Path(str(item.get("artifact_root_path", "")))
        row: Dict[str, Any] = {"artifact_root_id": root_id}
        if item.get("display_name"):
            row["display_name"] = str(item["display_name"])
        try:
            identity = artifact_lifecycle.read_root_identity(path) if root_id and path.is_dir() else None
        except artifact_lifecycle.LifecycleError:
            identity = None
        if identity is None or identity.artifact_root_id != root_id:
            row.update(status="skipped", code="root-identity-mismatch")
            rows.append(row)
            continue
        outcome = auto_title(path, mode="backfill", dry_run=dry_run, invoke=invoke, limit=limit,
                             lock_timeout=lock_timeout)
        counts = {name: sum(1 for target in outcome["targets"] if target["status"] == name)
                  for name in ("written", "failed", "proposed", "skipped")}
        row.update(status=outcome["status"], targets=len(outcome["targets"]), protected=outcome["protected"],
                   waiting=outcome["waiting"], rows=outcome["targets"], **counts)
        for name in totals:
            totals[name] += int(row.get(name, 0))
        rows.append(row)
    return {"status": "dry-run" if dry_run else "ok", "roots": rows, "totals": totals}


def launch_after_seal(root: Path, record: Mapping[str, Any]) -> bool:
    """Compatibility: start the one background review (never a second job); never raises, waits, or reads."""
    try:
        import artifact_workflow_group_review as R
        return R.launch_after_seal(root, record)
    except Exception:  # noqa: BLE001 -- a trigger never fails its caller
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n", 1)[0],
        epilog="exit codes: 0 done; 65 blocked or refused (JSON on stdout); "
               "2 usage error (a missing or unknown option, reported by argparse on stderr)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root-declaration", type=Path, required=True)
    p.add_argument("--proposals", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--allow-unlisted", action="store_true", help="leave campaigns absent from the approved proposal unchanged")
    p = sub.add_parser("apply")
    p.add_argument("--package", type=Path, required=True)
    p = sub.add_parser("verify")
    p.add_argument("--package", type=Path, required=True)
    p = sub.add_parser("rollback")
    p.add_argument("--package", type=Path, required=True)
    p = sub.add_parser("review")
    p.add_argument("--root-declaration", type=Path, required=True)
    p.add_argument("--proposals", type=Path, required=True)
    p.add_argument("--applied-package", type=Path, required=True)
    p.add_argument("--output", type=Path, default=DEFAULT_REVIEW_STATE)
    p = sub.add_parser("promote")
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--confirmation", required=True)
    p = sub.add_parser("report")
    p.add_argument("--review", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("auto", allow_abbrev=False, help="write Korean display titles for one root's untitled campaigns")
    p.add_argument("--artifact-root", type=Path, required=True)
    p.add_argument("--campaign", action="append", default=[])
    p.add_argument("--mode", choices=("seal", "explicit"), default="explicit")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("backfill", allow_abbrev=False, help="run `auto` over every root a declaration lists")
    p.add_argument("--root-declaration", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command in ("auto", "backfill"):
            import artifact_identity
            import artifact_lifecycle
            problem = None
            if args.limit is not None and args.limit < 1:
                problem = "limit-invalid"
            elif args.command == "auto":
                if any(not artifact_identity.is_well_formed(cid, "campaign") for cid in args.campaign):
                    problem = "campaign-id-invalid"
                else:
                    try:
                        valid = args.artifact_root.is_dir() and artifact_lifecycle.read_root_identity(args.artifact_root.resolve()) is not None
                    except artifact_lifecycle.LifecycleError:
                        valid = False
                    problem = None if valid else "root-invalid"
            if problem:
                print(json.dumps({"status": "blocked", "code": problem}, ensure_ascii=False, sort_keys=True))
                return 65
            if args.command == "auto":
                result = auto_title(args.artifact_root.resolve(), campaign_ids=args.campaign, mode=args.mode,
                                    dry_run=args.dry_run, limit=args.limit)
            else:
                result = backfill_roots(args.root_declaration, dry_run=args.dry_run, limit=args.limit)
        elif args.command == "prepare":
            package = prepare(args.root_declaration, args.proposals, allow_unlisted=args.allow_unlisted)
            write_atomic(args.output, package)
            result = {"status": "prepared", "campaigns": len(package["entries"]), "excluded": len(package["excluded_campaigns"]), "package": str(args.output)}
        elif args.command == "review":
            review = review_snapshot(args.root_declaration, args.proposals, args.applied_package)
            write_atomic(args.output, review)
            result = {"status": "review-package-written", "campaigns": len(review["entries"]), "package": str(args.output)}
        elif args.command == "promote":
            result = promote(args.review, args.output, args.confirmation)
        elif args.command == "report":
            review = read_json(args.review)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_atomic_bytes(args.output, render_review(review).encode("utf-8"))
            result = {"status": "review-report-written", "campaigns": len(review.get("entries", [])), "report": str(args.output)}
        else:
            package = read_json(args.package)
            if args.command == "apply":
                result = apply(package)
            elif args.command == "verify":
                result = verify(package)
            else:
                result = rollback(package)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except RepairError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
