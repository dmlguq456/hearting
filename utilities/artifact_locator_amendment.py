#!/usr/bin/env python3
"""Transactionally repair canonical campaign/cycle locators without rewriting seals."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

import artifact_admission
import artifact_campaign
import artifact_identity
import artifact_index
import artifact_locator
import artifact_manifest
from artifact_metadata_amendment import (
    CAMPAIGN_METADATA_REL,
    CAMPAIGN_SCHEMA,
    CYCLE_SCHEMA,
    CYCLE_TITLES_REL,
    DISPLAY_TITLES_REL,
    PRODUCER_REL,
    ROOT_IDENTITY_REL,
    canonical,
    digest_bytes,
    digest_json,
    write_atomic,
    write_atomic_bytes,
)

PACKAGE_SCHEMA = "hearting-artifact-locator-amendment-package/v1"
JOURNAL_SCHEMA = "hearting-artifact-locator-amendment-journal/v1"
_LOCATOR_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([a-z0-9][a-z0-9-]{0,47})$")


class LocatorAmendmentError(Exception):
    pass


def _read_bytes(path: Path, *, missing_ok: bool = False) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LocatorAmendmentError(f"file-missing:{path}")
    if not stat.S_ISREG(mode):
        raise LocatorAmendmentError(f"regular-file-required:{path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LocatorAmendmentError(f"file-read-failed:{path}:{exc}") from exc


def _read_json(path: Path, *, missing_ok: bool = False) -> Dict[str, Any] | None:
    raw = _read_bytes(path, missing_ok=missing_ok)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise LocatorAmendmentError(f"json-read-failed:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise LocatorAmendmentError(f"json-object-required:{path}")
    return value


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(value: Any) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (ValueError, TypeError) as exc:
        raise LocatorAmendmentError("package-bytes-invalid") from exc


def _assert_closed(value: Mapping[str, Any], fields: set[str], code: str) -> None:
    actual = set(value)
    if actual != fields:
        raise LocatorAmendmentError(f"{code}:missing={sorted(fields-actual)}:extra={sorted(actual-fields)}")


def _locator(value: str) -> tuple[str, str]:
    match = _LOCATOR_RE.fullmatch(value)
    if match is None:
        raise LocatorAmendmentError(f"locator-invalid:{value}")
    return match.group(1), match.group(2)


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _readlink_exact(path: Path, expected: str) -> bool:
    try:
        return stat.S_ISLNK(path.lstat().st_mode) and os.readlink(path) == expected
    except (FileNotFoundError, OSError):
        return False


def _find_campaign(root: Path, campaign_id: str) -> tuple[Path, Dict[str, Any]]:
    found = []
    campaigns = root / "campaigns"
    for path in sorted(campaigns.iterdir()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        record = _read_json(path / "campaign.json", missing_ok=True)
        if record is not None and record.get("campaign_id") == campaign_id:
            found.append((path, record))
    if len(found) != 1:
        raise LocatorAmendmentError(f"campaign-match-count:{campaign_id}:{len(found)}")
    return found[0]


def _binding(manifest_path: Path, *, root_id: str, repository_id: str,
             campaign_id: str, cycle_id: str) -> Dict[str, str]:
    manifest = _read_json(manifest_path)
    assert manifest is not None
    campaign = manifest.get("campaign")
    cycle = manifest.get("cycle")
    revision = str(manifest.get("manifest_revision_id", ""))
    if manifest.get("artifact_root_id") != root_id or manifest.get("repository_id") != repository_id:
        raise LocatorAmendmentError(f"manifest-root-repository-mismatch:{manifest_path}")
    if not isinstance(campaign, Mapping) or campaign.get("campaign_id") != campaign_id:
        raise LocatorAmendmentError(f"manifest-campaign-mismatch:{manifest_path}")
    if not isinstance(cycle, Mapping) or cycle.get("campaign_id") != campaign_id or cycle.get("cycle_id") != cycle_id:
        raise LocatorAmendmentError(f"manifest-cycle-mismatch:{manifest_path}")
    if not artifact_identity.is_well_formed(revision, "manifest_revision"):
        raise LocatorAmendmentError(f"manifest-revision-malformed:{manifest_path}")
    return {"manifest_revision_id": revision, "manifest_digest": digest_json(manifest)}


def _target(pre_path: Path, post_path: Path, root: Path, post: bytes) -> Dict[str, str]:
    pre = _read_bytes(pre_path)
    assert pre is not None
    return {
        "pre_path": pre_path.relative_to(root).as_posix(),
        "post_path": post_path.relative_to(root).as_posix(),
        "pre_bytes_b64": _b64(pre),
        "pre_digest": digest_bytes(pre),
        "post_bytes_b64": _b64(post),
        "post_digest": digest_bytes(post),
    }


def _protected(root: Path, campaign_dir: Path, campaign_json: Path,
               mutable_paths: set[Path]) -> list[Dict[str, str]]:
    paths: set[Path] = {root / ROOT_IDENTITY_REL, root / CAMPAIGN_METADATA_REL}
    for path in campaign_dir.rglob("*"):
        if path.is_file() and not path.is_symlink() and path != campaign_json:
            paths.add(path)
    shared = root / "shared"
    if shared.is_dir() and not shared.is_symlink():
        paths.update(path for path in shared.rglob("*") if path.is_file() and not path.is_symlink())
    routes = root / ".runtime" / "routes"
    if routes.is_dir() and not routes.is_symlink():
        paths.update(path for path in routes.glob("*.json") if path.is_file() and not path.is_symlink())
    rows = []
    # String order, not `Path` component order: the validator compares the
    # recorded posix strings (see artifact_metadata_amendment._protected_files).
    for path in sorted(paths - mutable_paths, key=lambda item: item.relative_to(root).as_posix()):
        raw = _read_bytes(path)
        assert raw is not None
        rows.append({"path": path.relative_to(root).as_posix(), "digest": digest_bytes(raw)})
    return rows


def _sidecar_entry(doc: Mapping[str, Any], campaign_id: str, code: str,
                   *, optional: bool = False) -> Dict[str, Any] | None:
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise LocatorAmendmentError(f"{code}-entries-required")
    found = [row for row in entries if isinstance(row, dict) and row.get("campaign_id") == campaign_id]
    if optional and not found:
        return None
    if len(found) != 1:
        raise LocatorAmendmentError(f"{code}-campaign-entry-count:{len(found)}")
    return found[0]


def prepare(root: Path, *, campaign_id: str, campaign_locator: str,
            cycles: Mapping[str, Mapping[str, str]]) -> Dict[str, Any]:
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _prepare_locked(root, campaign_id=campaign_id, campaign_locator=campaign_locator, cycles=cycles)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def _prepare_locked(root: Path, *, campaign_id: str, campaign_locator: str,
                    cycles: Mapping[str, Mapping[str, str]]) -> Dict[str, Any]:
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        raise LocatorAmendmentError("campaign-id-malformed")
    _, campaign_slug = _locator(campaign_locator)
    identity = _read_json(root / ROOT_IDENTITY_REL)
    if identity is None:
        raise LocatorAmendmentError("root-identity-missing")
    try:
        parsed_identity = artifact_identity.RootIdentity.parse(identity)
    except artifact_identity.IdentityError as exc:
        raise LocatorAmendmentError(f"root-identity-invalid:{exc}") from exc
    old_campaign_dir, campaign = _find_campaign(root, campaign_id)
    old_campaign_locator = old_campaign_dir.name
    try:
        folded_state = artifact_campaign.campaign_state(
            root, old_campaign_dir / "campaign.json", campaign).state
    except artifact_campaign.CampaignError as exc:
        raise LocatorAmendmentError(f"campaign-state-invalid:{exc.code}") from exc
    if folded_state != "active" or campaign.get("locator") != old_campaign_locator:
        raise LocatorAmendmentError("campaign-state-locator-mismatch")
    if campaign.get("key") in {None, "_unassigned"} or not isinstance(campaign.get("goal"), str):
        raise LocatorAmendmentError("campaign-semantic-amendment-required")
    new_campaign_dir = root / "campaigns" / campaign_locator
    if campaign_locator == old_campaign_locator or _lexists(new_campaign_dir):
        raise LocatorAmendmentError("campaign-target-collision")
    member_ids = campaign.get("cycles")
    if not isinstance(member_ids, list) or set(cycles) != set(member_ids) or len(set(member_ids)) != len(member_ids):
        raise LocatorAmendmentError("cycle-set-mismatch")

    campaign_meta = _read_json(root / CAMPAIGN_METADATA_REL)
    cycle_titles = _read_json(root / CYCLE_TITLES_REL)
    display_titles = _read_json(root / DISPLAY_TITLES_REL, missing_ok=True)
    if campaign_meta is None or campaign_meta.get("schema") != CAMPAIGN_SCHEMA:
        raise LocatorAmendmentError("campaign-metadata-amendment-required")
    if cycle_titles is None or cycle_titles.get("schema") != CYCLE_SCHEMA:
        raise LocatorAmendmentError("cycle-title-sidecar-required")
    # D-103 aligns an *existing* campaign display-title v2 entry; a root that
    # never ran the fleet title repair has none, and that is not a refusal.
    display_entry = None
    if display_titles is not None:
        if display_titles.get("schema") != "hearting-campaign-display-titles/v2":
            raise LocatorAmendmentError("campaign-display-title-sidecar-invalid")
        display_entry = _sidecar_entry(display_titles, campaign_id, "campaign-display-title", optional=True)
    meta_entry = _sidecar_entry(campaign_meta, campaign_id, "campaign-metadata")
    if meta_entry.get("key") != campaign.get("key") or meta_entry.get("goal") != campaign.get("goal"):
        raise LocatorAmendmentError("campaign-metadata-binding-mismatch")
    if display_entry is not None and display_entry.get("campaign_locator") != old_campaign_locator:
        raise LocatorAmendmentError("campaign-display-locator-mismatch")

    file_targets: list[Dict[str, str]] = []
    updated_campaign = dict(campaign)
    updated_campaign.update({"locator": campaign_locator, "locator_suffix": "", "slug": campaign_slug,
                             "slug_source": "locator-amendment", "slug_truncated": False})
    campaign_json = old_campaign_dir / "campaign.json"
    file_targets.append(_target(campaign_json, new_campaign_dir / "campaign.json", root,
                                canonical(updated_campaign) + b"\n"))

    manifest_sources = []
    cycle_rows = []
    mutable_paths = {campaign_json, root / CYCLE_TITLES_REL, root / DISPLAY_TITLES_REL,
                     root / "campaigns" / artifact_locator.INDEX_JSON,
                     root / "campaigns" / artifact_locator.INDEX_MD,
                     root / artifact_admission.ADMISSION_REL / "index.json"}
    title_entries = [dict(row) for row in cycle_titles.get("entries", [])]
    title_by_cycle = {str(row.get("cycle_id")): row for row in title_entries if row.get("campaign_id") == campaign_id}
    for cycle_id in member_ids:
        request = cycles[cycle_id]
        if set(request) != {"locator", "title"}:
            raise LocatorAmendmentError(f"cycle-request-fields:{cycle_id}")
        new_locator = str(request["locator"])
        _, new_slug = _locator(new_locator)
        title = str(request["title"]).strip()
        if not title:
            raise LocatorAmendmentError(f"cycle-title-empty:{cycle_id}")
        record_path = root / PRODUCER_REL / "cycles" / f"{cycle_id}.json"
        record = _read_json(record_path)
        if record is None or record.get("state") != "sealed" or record.get("campaign_id") != campaign_id:
            raise LocatorAmendmentError(f"cycle-not-sealed:{cycle_id}")
        old_locator = str(record.get("locator", ""))
        old_dir = old_campaign_dir / old_locator
        move = new_locator != old_locator
        if not _real_dir(old_dir) or (move and _lexists(old_campaign_dir / new_locator)):
            raise LocatorAmendmentError(f"cycle-target-collision:{cycle_id}")
        try:
            binding_doc = artifact_locator.read_cycle_binding(old_dir)
        except artifact_locator.LocatorError:
            binding_doc = None
        if (binding_doc is None or binding_doc["campaign_id"] != campaign_id
                or binding_doc["cycle_id"] != cycle_id):
            raise LocatorAmendmentError(f"cycle-binding-mismatch:{cycle_id}")
        manifest_path = old_dir / "manifest.json"
        binding = _binding(manifest_path, root_id=parsed_identity.artifact_root_id,
                           repository_id=parsed_identity.repository_id,
                           campaign_id=campaign_id, cycle_id=cycle_id)
        manifest_sources.append({"cycle_id": cycle_id,
                                 "path": manifest_path.relative_to(root).as_posix(),
                                 "raw_digest": digest_bytes(_read_bytes(manifest_path) or b""), **binding})
        title_entry = title_by_cycle.get(cycle_id)
        if title_entry is None or title_entry.get("manifest_bindings") != [binding]:
            raise LocatorAmendmentError(f"cycle-title-binding-mismatch:{cycle_id}")
        title_entry["display_title"] = title
        updated_record = dict(record)
        if move:
            updated_record.update({"locator": new_locator, "locator_suffix": "", "slug": new_slug,
                                   "slug_source": "locator-amendment", "slug_truncated": False, "title": title})
        else:
            # Unchanged locator: only the title moves; the D-90 suffix and the
            # recorded slug provenance stay exactly as issued.
            new_slug = str(record.get("slug", new_slug))
            updated_record["title"] = title
        file_targets.append(_target(record_path, record_path, root, canonical(updated_record) + b"\n"))
        mutable_paths.add(record_path)
        cycle_rows.append({"cycle_id": cycle_id, "old_locator": old_locator,
                           "new_locator": new_locator, "new_slug": new_slug, "title": title, "move": move})
    if len({row["new_locator"] for row in cycle_rows}) != len(cycle_rows):
        raise LocatorAmendmentError("cycle-target-duplicate")
    if len(title_by_cycle) != len(member_ids):
        raise LocatorAmendmentError("cycle-title-entry-set-mismatch")
    meta_bindings = sorted(({"manifest_revision_id": row["manifest_revision_id"],
                             "manifest_digest": row["manifest_digest"]} for row in manifest_sources),
                           key=lambda row: row["manifest_revision_id"])
    if meta_entry.get("manifest_bindings") != meta_bindings:
        raise LocatorAmendmentError("campaign-manifest-binding-mismatch")

    cycle_titles_post = {**cycle_titles, "entries": sorted(title_entries,
                         key=lambda row: (str(row.get("campaign_id")), str(row.get("cycle_id"))))}
    file_targets.append(_target(root / CYCLE_TITLES_REL, root / CYCLE_TITLES_REL, root,
                                canonical(cycle_titles_post) + b"\n"))
    if display_entry is not None:
        display_entries = [dict(row) for row in display_titles["entries"]]
        display_match = [row for row in display_entries if row.get("campaign_id") == campaign_id]
        display_match[0]["campaign_locator"] = campaign_locator
        display_post = {**display_titles, "entries": sorted(display_entries,
                        key=lambda row: (str(row.get("campaign_id")), str(row.get("campaign_locator"))))}
        file_targets.append(_target(root / DISPLAY_TITLES_REL, root / DISPLAY_TITLES_REL, root,
                                    canonical(display_post) + b"\n"))

    locator_mapping, locator_rows = artifact_locator.scan_index(root)
    new_mapping = dict(locator_mapping)
    new_mapping[campaign_id] = f"campaigns/{campaign_locator}"
    locator_rows = {key: dict(row) for key, row in locator_rows.items()}
    locator_rows[campaign_id]["path"] = new_mapping[campaign_id]
    locator_rows[campaign_id]["title"] = str(campaign.get("title"))
    request_by_cycle = {row["cycle_id"]: row for row in cycle_rows}
    for cycle_id, request in request_by_cycle.items():
        new_mapping[cycle_id] = f"campaigns/{campaign_locator}/{request['new_locator']}"
        locator_rows[cycle_id]["path"] = new_mapping[cycle_id]
        locator_rows[cycle_id]["title"] = request["title"]
    file_targets.append(_target(root / "campaigns" / artifact_locator.INDEX_JSON,
                                root / "campaigns" / artifact_locator.INDEX_JSON, root,
                                artifact_locator._index_json_bytes(new_mapping)))
    file_targets.append(_target(root / "campaigns" / artifact_locator.INDEX_MD,
                                root / "campaigns" / artifact_locator.INDEX_MD, root,
                                artifact_locator._markdown(locator_rows)))

    admission_path = root / artifact_admission.ADMISSION_REL / "index.json"
    admission_doc = _read_json(admission_path)
    if admission_doc is None:
        raise LocatorAmendmentError("admission-index-required")
    try:
        parsed_index = artifact_index.parse(admission_doc)
    except ValueError as exc:
        raise LocatorAmendmentError(f"admission-index-invalid:{exc}") from exc
    admission_post = artifact_index.to_payload(parsed_index)
    for cycle_id, request in request_by_cycle.items():
        row = dict(admission_post["cycles"].get(cycle_id, {}))
        if row.get("campaign_id") != campaign_id:
            raise LocatorAmendmentError(f"admission-index-cycle-mismatch:{cycle_id}")
        row["cycle_path"] = f"campaigns/{campaign_locator}/{request['new_locator']}"
        admission_post["cycles"][cycle_id] = row
    file_targets.append(_target(admission_path, admission_path, root,
                                artifact_manifest.canonical_bytes(admission_post)))

    package = {
        "schema": PACKAGE_SCHEMA,
        "artifact_root": str(root),
        "artifact_root_id": parsed_identity.artifact_root_id,
        "repository_id": parsed_identity.repository_id,
        "campaign_id": campaign_id,
        "old_campaign_locator": old_campaign_locator,
        "new_campaign_locator": campaign_locator,
        "new_campaign_slug": campaign_slug,
        "cycles": cycle_rows,
        "manifest_sources": sorted(manifest_sources, key=lambda row: row["manifest_revision_id"]),
        "protected_files": _protected(root, old_campaign_dir, campaign_json, mutable_paths),
        "file_targets": file_targets,
    }
    _validate_package(package)
    return package


def package_digest(package: Mapping[str, Any]) -> str:
    return digest_json(package)


def _validate_package(package: Mapping[str, Any]) -> None:
    _assert_closed(package, {"schema", "artifact_root", "artifact_root_id", "repository_id", "campaign_id",
                             "old_campaign_locator", "new_campaign_locator", "new_campaign_slug", "cycles",
                             "manifest_sources", "protected_files", "file_targets"}, "package-fields")
    if package.get("schema") != PACKAGE_SCHEMA:
        raise LocatorAmendmentError("package-schema-mismatch")
    for field, kind in (("artifact_root_id", "artifact_root"), ("repository_id", "repository"),
                        ("campaign_id", "campaign")):
        if not artifact_identity.is_well_formed(str(package.get(field)), kind):
            raise LocatorAmendmentError(f"package-{field}-malformed")
    old = str(package.get("old_campaign_locator")); new = str(package.get("new_campaign_locator"))
    _locator(old); _, slug = _locator(new)
    if old == new or package.get("new_campaign_slug") != slug:
        raise LocatorAmendmentError("package-campaign-locator-invalid")
    cycles = package.get("cycles")
    if not isinstance(cycles, list) or not cycles:
        raise LocatorAmendmentError("package-cycles-required")
    seen_ids = set(); seen_targets = set()
    for row in cycles:
        if not isinstance(row, Mapping):
            raise LocatorAmendmentError("package-cycle-object-required")
        _assert_closed(row, {"cycle_id", "old_locator", "new_locator", "new_slug", "title", "move"}, "package-cycle-fields")
        cycle_id = str(row.get("cycle_id")); old_locator = str(row.get("old_locator")); new_locator = str(row.get("new_locator"))
        if not artifact_identity.is_well_formed(cycle_id, "cycle") or cycle_id in seen_ids:
            raise LocatorAmendmentError("package-cycle-id-invalid")
        _locator(old_locator); _, cycle_slug = _locator(new_locator)
        move = row.get("move")
        if not isinstance(move, bool) or move != (old_locator != new_locator):
            raise LocatorAmendmentError("package-cycle-move-invalid")
        if new_locator in seen_targets or not str(row.get("title", "")).strip():
            raise LocatorAmendmentError("package-cycle-locator-invalid")
        if move and row.get("new_slug") != cycle_slug:
            raise LocatorAmendmentError("package-cycle-locator-invalid")
        seen_ids.add(cycle_id); seen_targets.add(new_locator)
    sources = package.get("manifest_sources")
    if not isinstance(sources, list) or {row.get("cycle_id") for row in sources if isinstance(row, Mapping)} != seen_ids:
        raise LocatorAmendmentError("package-manifest-source-set-mismatch")
    revisions = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise LocatorAmendmentError("package-manifest-source-object-required")
        _assert_closed(source, {"cycle_id", "path", "raw_digest", "manifest_revision_id", "manifest_digest"},
                       "package-manifest-source-fields")
        revision = str(source["manifest_revision_id"])
        if not artifact_identity.is_well_formed(revision, "manifest_revision"):
            raise LocatorAmendmentError("package-manifest-revision-malformed")
        revisions.append(revision)
        for field in ("raw_digest", "manifest_digest"):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", str(source[field])) is None:
                raise LocatorAmendmentError("package-manifest-digest-malformed")
        path = str(source["path"])
        if path.startswith("/") or ".." in Path(path).parts:
            raise LocatorAmendmentError("package-manifest-path-unsafe")
    if revisions != sorted(revisions) or len(set(revisions)) != len(revisions):
        raise LocatorAmendmentError("package-manifest-sources-not-canonical")
    protected = package.get("protected_files")
    if not isinstance(protected, list) or not protected:
        raise LocatorAmendmentError("package-protected-required")
    protected_paths = [row.get("path") for row in protected if isinstance(row, Mapping)]
    if len(protected_paths) != len(protected) or protected_paths != sorted(protected_paths) or len(set(protected_paths)) != len(protected_paths):
        raise LocatorAmendmentError("package-protected-not-canonical")
    for row in protected:
        _assert_closed(row, {"path", "digest"}, "package-protected-fields")
        if str(row["path"]).startswith("/") or ".." in Path(str(row["path"])).parts:
            raise LocatorAmendmentError("package-protected-path-unsafe")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", str(row["digest"])) is None:
            raise LocatorAmendmentError("package-protected-digest-malformed")
    targets = package.get("file_targets")
    # campaign record + one producer record per cycle + cycle title sidecar +
    # two locator indexes + one admission index, plus the campaign display-title
    # sidecar exactly when the package carries it.
    if not isinstance(targets, list):
        raise LocatorAmendmentError("package-file-target-count")
    display_targets = sum(1 for t in targets if isinstance(t, Mapping)
                          and str(t.get("post_path")) == DISPLAY_TITLES_REL.as_posix())
    if display_targets > 1 or len(targets) != 5 + len(cycles) + display_targets:
        raise LocatorAmendmentError("package-file-target-count")
    target_pre_paths = []; target_post_paths = []
    for target in targets:
        if not isinstance(target, Mapping):
            raise LocatorAmendmentError("package-file-target-object-required")
        _assert_closed(target, {"pre_path", "post_path", "pre_bytes_b64", "pre_digest",
                                "post_bytes_b64", "post_digest"}, "package-file-target-fields")
        for field in ("pre_path", "post_path"):
            value = str(target[field])
            if value.startswith("/") or ".." in Path(value).parts:
                raise LocatorAmendmentError("package-file-target-path-unsafe")
        pre = _unb64(target["pre_bytes_b64"]); post = _unb64(target["post_bytes_b64"])
        if digest_bytes(pre) != target["pre_digest"] or digest_bytes(post) != target["post_digest"]:
            raise LocatorAmendmentError("package-file-target-digest-mismatch")
        target_pre_paths.append(str(target["pre_path"])); target_post_paths.append(str(target["post_path"]))
    if len(set(target_pre_paths)) != len(target_pre_paths) or len(set(target_post_paths)) != len(target_post_paths):
        raise LocatorAmendmentError("package-file-target-duplicate")


def _verify_protected(root: Path, package: Mapping[str, Any]) -> None:
    for row in package["protected_files"]:
        raw = _read_bytes(root / str(row["path"]))
        assert raw is not None
        if digest_bytes(raw) != row.get("digest"):
            raise LocatorAmendmentError(f"protected-file-drift:{row['path']}")


def _verify_identity_and_manifests(root: Path, package: Mapping[str, Any]) -> None:
    identity = _read_json(root / ROOT_IDENTITY_REL)
    if identity is None:
        raise LocatorAmendmentError("root-identity-missing")
    if (identity.get("artifact_root_id") != package["artifact_root_id"] or
            identity.get("repository_id") != package["repository_id"]):
        raise LocatorAmendmentError("package-root-identity-mismatch")
    for source in package["manifest_sources"]:
        path = root / str(source["path"])
        raw = _read_bytes(path)
        assert raw is not None
        if digest_bytes(raw) != source["raw_digest"]:
            raise LocatorAmendmentError(f"manifest-raw-drift:{source['cycle_id']}")
        binding = _binding(
            path,
            root_id=str(package["artifact_root_id"]),
            repository_id=str(package["repository_id"]),
            campaign_id=str(package["campaign_id"]),
            cycle_id=str(source["cycle_id"]),
        )
        if binding != {"manifest_revision_id": source["manifest_revision_id"],
                       "manifest_digest": source["manifest_digest"]}:
            raise LocatorAmendmentError(f"manifest-binding-drift:{source['cycle_id']}")


def _target_state(root: Path, target: Mapping[str, Any], state: str) -> bool:
    path = root / str(target[f"{state}_path"])
    raw = _read_bytes(path, missing_ok=True)
    return raw == _unb64(target[f"{state}_bytes_b64"])


def _precheck(root: Path, package: Mapping[str, Any]) -> None:
    old = root / "campaigns" / str(package["old_campaign_locator"])
    new = root / "campaigns" / str(package["new_campaign_locator"])
    if not _real_dir(old) or _lexists(new):
        raise LocatorAmendmentError("locator-preimage-drift")
    for row in package["cycles"]:
        if not _real_dir(old / row["old_locator"]) or (row["move"] and _lexists(old / row["new_locator"])):
            raise LocatorAmendmentError(f"cycle-locator-preimage-drift:{row['cycle_id']}")
    for target in package["file_targets"]:
        if not _target_state(root, target, "pre"):
            raise LocatorAmendmentError(f"file-preimage-drift:{target['pre_path']}")
    _verify_protected(root, package)
    _verify_identity_and_manifests(root, package)


def _remove_exact_symlink(path: Path, target: str) -> None:
    if not _lexists(path):
        return
    if not _readlink_exact(path, target):
        raise LocatorAmendmentError(f"locator-redirect-drift:{path}")
    path.unlink()


def _recover_pre(root: Path, package: Mapping[str, Any]) -> None:
    campaigns = root / "campaigns"
    old = campaigns / str(package["old_campaign_locator"])
    new = campaigns / str(package["new_campaign_locator"])
    if old.is_symlink():
        _remove_exact_symlink(old, str(package["new_campaign_locator"]))
    if _real_dir(new):
        work = new
    elif _real_dir(old):
        work = old
    else:
        raise LocatorAmendmentError("locator-rollback-campaign-missing")
    for row in package["cycles"]:
        old_cycle = work / str(row["old_locator"])
        new_cycle = work / str(row["new_locator"])
        if not row["move"]:
            if not _real_dir(old_cycle):
                raise LocatorAmendmentError(f"locator-rollback-cycle-missing:{row['cycle_id']}")
            continue
        if old_cycle.is_symlink():
            _remove_exact_symlink(old_cycle, str(row["new_locator"]))
        if _real_dir(new_cycle):
            if _lexists(old_cycle):
                raise LocatorAmendmentError(f"locator-rollback-cycle-collision:{row['cycle_id']}")
            os.rename(new_cycle, old_cycle)
        elif not _real_dir(old_cycle):
            raise LocatorAmendmentError(f"locator-rollback-cycle-missing:{row['cycle_id']}")
    if work == new:
        if _lexists(old):
            raise LocatorAmendmentError("locator-rollback-campaign-collision")
        os.rename(new, old)
    for target in package["file_targets"]:
        write_atomic_bytes(root / str(target["pre_path"]), _unb64(target["pre_bytes_b64"]))


def _journal_path(root: Path, digest: str) -> Path:
    return root / PRODUCER_REL / "locator-amendments" / (digest.removeprefix("sha256:") + ".journal.json")


def apply(package: Mapping[str, Any], *, expected_package_digest: str,
          fault_after_steps: int | None = None) -> Dict[str, Any]:
    _validate_package(package)
    digest = package_digest(package)
    if digest != expected_package_digest:
        raise LocatorAmendmentError("package-digest-mismatch")
    root = Path(str(package["artifact_root"])).resolve()
    journal_path = _journal_path(root, digest)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        journal = _read_json(journal_path, missing_ok=True)
        if journal is not None:
            _assert_closed(journal, {"schema", "state", "package_digest"}, "journal-fields")
            if journal.get("schema") != JOURNAL_SCHEMA or journal.get("package_digest") != digest:
                raise LocatorAmendmentError("journal-binding-mismatch")
            if journal.get("state") == "committed":
                result = verify(package)
                return {**result, "status": "already-applied", "journal": str(journal_path)}
            if journal.get("state") == "prepared":
                _recover_pre(root, package)
                journal["state"] = "rolled-back"
                write_atomic(journal_path, journal)
            elif journal.get("state") not in {"rolled-back"}:
                raise LocatorAmendmentError("journal-state-invalid")
        _precheck(root, package)
        journal = {"schema": JOURNAL_SCHEMA, "state": "prepared", "package_digest": digest}
        write_atomic(journal_path, journal)
        step = 0
        def tick() -> None:
            nonlocal step
            step += 1
            if fault_after_steps is not None and step >= fault_after_steps:
                raise LocatorAmendmentError("injected-locator-failure")
        old = root / "campaigns" / str(package["old_campaign_locator"])
        new = root / "campaigns" / str(package["new_campaign_locator"])
        try:
            os.rename(old, new); tick()
            for row in package["cycles"]:
                if row["move"]:
                    os.rename(new / row["old_locator"], new / row["new_locator"]); tick()
            for target in package["file_targets"]:
                write_atomic_bytes(root / str(target["post_path"]), _unb64(target["post_bytes_b64"])); tick()
            for row in package["cycles"]:
                if row["move"]:
                    os.symlink(row["new_locator"], new / row["old_locator"]); tick()
            os.symlink(package["new_campaign_locator"], old); tick()
            result = verify(package)
        except Exception:
            try:
                _recover_pre(root, package)
                journal["state"] = "rolled-back"
            except Exception:
                journal["state"] = "rollback-conflict"
            write_atomic(journal_path, journal)
            raise
        journal["state"] = "committed"
        write_atomic(journal_path, journal)
        return {**result, "status": "applied", "journal": str(journal_path)}
    finally:
        artifact_admission._release_lock(root, lock_fd)


def verify(package: Mapping[str, Any]) -> Dict[str, Any]:
    _validate_package(package)
    root = Path(str(package["artifact_root"])).resolve()
    old = root / "campaigns" / str(package["old_campaign_locator"])
    new = root / "campaigns" / str(package["new_campaign_locator"])
    if not _real_dir(new) or not _readlink_exact(old, str(package["new_campaign_locator"])):
        raise LocatorAmendmentError("campaign-locator-not-applied")
    if new.resolve().parent != (root / "campaigns").resolve():
        raise LocatorAmendmentError("campaign-target-outside-root")
    for row in package["cycles"]:
        new_cycle = new / str(row["new_locator"])
        old_cycle = new / str(row["old_locator"])
        if not _real_dir(new_cycle) or (row["move"] and not _readlink_exact(old_cycle, str(row["new_locator"]))):
            raise LocatorAmendmentError(f"cycle-locator-not-applied:{row['cycle_id']}")
        if new_cycle.resolve().parent != new.resolve():
            raise LocatorAmendmentError(f"cycle-target-outside-campaign:{row['cycle_id']}")
        binding = _read_json(new_cycle / ".cycle.json")
        if binding is None or binding.get("campaign_id") != package["campaign_id"] or binding.get("cycle_id") != row["cycle_id"]:
            raise LocatorAmendmentError(f"cycle-target-identity-mismatch:{row['cycle_id']}")
    for target in package["file_targets"]:
        if not _target_state(root, target, "post"):
            raise LocatorAmendmentError(f"file-postimage-drift:{target['post_path']}")
    _verify_protected(root, package)
    mapping, _ = artifact_locator.scan_index(root)
    if mapping.get(package["campaign_id"]) != f"campaigns/{package['new_campaign_locator']}":
        raise LocatorAmendmentError("campaign-index-drift")
    for row in package["cycles"]:
        expected = f"campaigns/{package['new_campaign_locator']}/{row['new_locator']}"
        if mapping.get(row["cycle_id"]) != expected:
            raise LocatorAmendmentError(f"cycle-index-drift:{row['cycle_id']}")
    _verify_identity_and_manifests(root, package)
    return {"status": "verified", "package_digest": package_digest(package),
            "campaign_id": package["campaign_id"], "campaign_locator": package["new_campaign_locator"],
            "cycles": len(package["cycles"])}


def _parse_cycles(values: Sequence[str]) -> Dict[str, Dict[str, str]]:
    result = {}
    for value in values:
        parts = value.split("=", 2)
        if len(parts) != 3 or not artifact_identity.is_well_formed(parts[0], "cycle") or not parts[2].strip():
            raise LocatorAmendmentError(f"cycle-argument-invalid:{value}")
        if parts[0] in result:
            raise LocatorAmendmentError(f"cycle-argument-duplicate:{parts[0]}")
        result[parts[0]] = {"locator": parts[1], "title": parts[2].strip()}
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--artifact-root", type=Path, required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--campaign-locator", required=True)
    p.add_argument("--cycle", action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("apply")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--expect-package-digest", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--package", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            package = prepare(args.artifact_root, campaign_id=args.campaign,
                              campaign_locator=args.campaign_locator, cycles=_parse_cycles(args.cycle))
            write_atomic(args.output, package)
            result = {"status": "prepared", "package": str(args.output),
                      "package_digest": package_digest(package), "campaign_id": args.campaign}
        else:
            package = _read_json(args.package)
            assert package is not None
            result = apply(package, expected_package_digest=args.expect_package_digest) if args.command == "apply" else verify(package)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (LocatorAmendmentError, artifact_admission.AdmissionBusy) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
