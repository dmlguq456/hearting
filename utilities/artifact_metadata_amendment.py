#!/usr/bin/env python3
"""Prepare, apply, and verify sealed-evidence-bound producer metadata amendments."""
from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

import artifact_admission
import artifact_campaign
import artifact_identity
import artifact_locator
from campaign_title_repair import canonical, digest_bytes, digest_json, write_atomic, write_atomic_bytes

PACKAGE_SCHEMA = "hearting-artifact-metadata-amendment-package/v1"
JOURNAL_SCHEMA = "hearting-artifact-metadata-amendment-journal/v1"
CAMPAIGN_SCHEMA = "hearting-campaign-metadata-amendments/v1"
CYCLE_SCHEMA = "hearting-cycle-display-titles/v1"
PRODUCER_REL = Path(".runtime/artifact-producer/v1")
CAMPAIGN_METADATA_REL = PRODUCER_REL / "campaign-metadata-amendments.json"
CYCLE_TITLES_REL = PRODUCER_REL / "cycle-display-titles.json"
ROOT_IDENTITY_REL = Path(".runtime/artifact-admission/v1/root-identity.json")
DISPLAY_TITLES_REL = PRODUCER_REL / "campaign-display-titles.json"
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class AmendmentError(Exception):
    pass


def _read_bytes(path: Path, *, missing_ok: bool = False) -> bytes | None:
    try:
        path.lstat()
        if path.is_symlink() or not path.is_file():
            raise AmendmentError(f"regular-file-required:{path}")
        return path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise AmendmentError(f"file-missing:{path}")
    except OSError as exc:
        raise AmendmentError(f"file-read-failed:{path}:{exc}") from exc


def _read_json(path: Path, *, missing_ok: bool = False) -> Dict[str, Any] | None:
    raw = _read_bytes(path, missing_ok=missing_ok)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise AmendmentError(f"json-read-failed:{path}:{exc}") from exc
    if not isinstance(value, dict):
        raise AmendmentError(f"json-object-required:{path}")
    return value


def _b64(raw: bytes | None) -> str | None:
    return None if raw is None else base64.b64encode(raw).decode("ascii")


def _unb64(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise AmendmentError("package-bytes-invalid") from exc


def _file_state(path: Path) -> Dict[str, Any]:
    raw = _read_bytes(path, missing_ok=True)
    return {
        "exists": raw is not None,
        "bytes_b64": _b64(raw),
        "digest": digest_bytes(raw) if raw is not None else None,
    }


def _assert_closed(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    actual = set(value)
    if actual != expected:
        raise AmendmentError(f"{code}:missing={sorted(expected-actual)}:extra={sorted(actual-expected)}")


def _manifest_binding(manifest_path: Path, *, root_id: str, repository_id: str,
                      campaign_id: str, cycle_id: str) -> Dict[str, str]:
    manifest = _read_json(manifest_path)
    assert manifest is not None
    campaign = manifest.get("campaign")
    cycle = manifest.get("cycle")
    revision = manifest.get("manifest_revision_id")
    if manifest.get("artifact_root_id") != root_id or manifest.get("repository_id") != repository_id:
        raise AmendmentError(f"manifest-root-repository-mismatch:{manifest_path}")
    if not isinstance(campaign, Mapping) or campaign.get("campaign_id") != campaign_id:
        raise AmendmentError(f"manifest-campaign-mismatch:{manifest_path}")
    if not isinstance(cycle, Mapping) or cycle.get("campaign_id") != campaign_id or cycle.get("cycle_id") != cycle_id:
        raise AmendmentError(f"manifest-cycle-mismatch:{manifest_path}")
    if not artifact_identity.is_well_formed(str(revision), "manifest_revision"):
        raise AmendmentError(f"manifest-revision-malformed:{manifest_path}")
    return {"manifest_revision_id": str(revision), "manifest_digest": digest_json(manifest)}


def _validate_bindings(bindings: Any, code: str) -> list[Dict[str, str]]:
    if not isinstance(bindings, list) or not bindings:
        raise AmendmentError(f"{code}-required")
    rows: list[Dict[str, str]] = []
    for value in bindings:
        if not isinstance(value, Mapping):
            raise AmendmentError(f"{code}-object-required")
        _assert_closed(value, {"manifest_revision_id", "manifest_digest"}, f"{code}-fields")
        revision = value.get("manifest_revision_id")
        digest = value.get("manifest_digest")
        if not artifact_identity.is_well_formed(str(revision), "manifest_revision"):
            raise AmendmentError(f"{code}-revision-malformed")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise AmendmentError(f"{code}-digest-malformed")
        rows.append({"manifest_revision_id": str(revision), "manifest_digest": digest})
    if rows != sorted(rows, key=lambda row: row["manifest_revision_id"]):
        raise AmendmentError(f"{code}-not-sorted")
    if len({row["manifest_revision_id"] for row in rows}) != len(rows):
        raise AmendmentError(f"{code}-duplicate-revision")
    if len({row["manifest_digest"] for row in rows}) != len(rows):
        raise AmendmentError(f"{code}-duplicate-digest")
    return rows


def _load_sidecar(path: Path, schema: str, root_id: str, repository_id: str,
                  entry_fields: set[str]) -> Dict[str, Any]:
    doc = _read_json(path, missing_ok=True)
    if doc is None:
        return {"schema": schema, "artifact_root_id": root_id, "repository_id": repository_id, "entries": []}
    _assert_closed(doc, {"schema", "artifact_root_id", "repository_id", "entries"}, "sidecar-fields")
    if doc.get("schema") != schema or doc.get("artifact_root_id") != root_id or doc.get("repository_id") != repository_id:
        raise AmendmentError(f"sidecar-identity-mismatch:{path}")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        raise AmendmentError(f"sidecar-entries-required:{path}")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise AmendmentError(f"sidecar-entry-object-required:{path}")
        _assert_closed(entry, entry_fields, "sidecar-entry-fields")
        _validate_bindings(entry.get("manifest_bindings"), "manifest-bindings")
    return dict(doc)


def _campaign_records(root: Path) -> Iterable[tuple[Path, Dict[str, Any]]]:
    campaigns = root / "campaigns"
    if campaigns.is_symlink() or not campaigns.is_dir():
        raise AmendmentError("campaigns-directory-required")
    for path in sorted(campaigns.glob("*/campaign.json")):
        record = _read_json(path)
        assert record is not None
        yield path, record


def _find_campaign(root: Path, campaign_id: str) -> tuple[Path, Dict[str, Any]]:
    matches = [(path, row) for path, row in _campaign_records(root) if row.get("campaign_id") == campaign_id]
    if len(matches) != 1:
        raise AmendmentError(f"campaign-match-count:{campaign_id}:{len(matches)}")
    return matches[0]


def _cycle_record_path(root: Path, cycle_id: str) -> Path:
    return root / PRODUCER_REL / "cycles" / f"{cycle_id}.json"


def _protected_files(root: Path, campaign_dir: Path, campaign_json: Path,
                     cycle_records: Sequence[Mapping[str, Any]]) -> list[Dict[str, str]]:
    paths: set[Path] = {root / ROOT_IDENTITY_REL}
    display_titles = root / DISPLAY_TITLES_REL
    if display_titles.is_file() and not display_titles.is_symlink():
        paths.add(display_titles)
    for path in campaign_dir.rglob("*"):
        if path.is_file() and not path.is_symlink() and path != campaign_json:
            paths.add(path)
    for record in cycle_records:
        cycle_path = _cycle_record_path(root, str(record["cycle_id"]))
        paths.add(cycle_path)
        for field in ("route_file",):
            raw = record.get(field)
            if isinstance(raw, str):
                candidate = Path(raw).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if candidate.is_file() and not candidate.is_symlink():
                    paths.add(candidate)
                outcome = Path(str(candidate)[:-5] + ".outcome.json") if str(candidate).endswith(".json") else None
                if outcome is not None and outcome.is_file() and not outcome.is_symlink():
                    paths.add(outcome)
    shared = root / "shared"
    if shared.is_dir() and not shared.is_symlink():
        paths.update(path for path in shared.rglob("*") if path.is_file() and not path.is_symlink())
    rows = []
    # Sort by the recorded posix string: `Path` ordering compares components,
    # so `shards/retrieval/…` sorted before `shards/retrieval-alternative/…`
    # while the validator's string order puts them the other way round, and a
    # real root (TF-Rehancer 2026-09-15) was refused as not canonical.
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        raw = _read_bytes(path)
        assert raw is not None
        rows.append({"path": path.relative_to(root).as_posix(), "digest": digest_bytes(raw)})
    return rows


def _prepare_locked(root: Path, *, campaign_id: str, key: str, goal: str,
                    cycle_titles: Mapping[str, str]) -> Dict[str, Any]:
    root = Path(root).resolve()
    if not artifact_identity.is_well_formed(campaign_id, "campaign"):
        raise AmendmentError("campaign-id-malformed")
    if _KEY_RE.fullmatch(key) is None or key == "_unassigned":
        raise AmendmentError("campaign-key-invalid")
    if not isinstance(goal, str) or not goal.strip():
        raise AmendmentError("campaign-goal-empty")
    identity_doc = _read_json(root / ROOT_IDENTITY_REL)
    assert identity_doc is not None
    try:
        identity = artifact_identity.RootIdentity.parse(identity_doc)
    except artifact_identity.IdentityError as exc:
        raise AmendmentError(f"root-identity-invalid:{exc}") from exc
    campaign_json, campaign = _find_campaign(root, campaign_id)
    try:
        folded_state = artifact_campaign.campaign_state(root, campaign_json, campaign).state
    except artifact_campaign.CampaignError as exc:
        raise AmendmentError(f"campaign-state-invalid:{exc.code}") from exc
    if folded_state != "active" or campaign.get("key") != "_unassigned":
        raise AmendmentError("campaign-not-active-unassigned")
    if campaign.get("degraded") is not True or campaign.get("degraded_reason") != "campaign-unassigned":
        raise AmendmentError("campaign-degraded-condition-not-exact")
    if any(row.get("key") == key and row.get("campaign_id") != campaign_id for _, row in _campaign_records(root)):
        raise AmendmentError("campaign-key-collision")
    cycle_ids = campaign.get("cycles")
    if not isinstance(cycle_ids, list) or not cycle_ids or any(not artifact_identity.is_well_formed(str(value), "cycle") for value in cycle_ids):
        raise AmendmentError("campaign-cycles-invalid")
    if len(set(cycle_ids)) != len(cycle_ids):
        raise AmendmentError("campaign-cycle-duplicate")
    if set(cycle_titles) != set(cycle_ids) or any(not str(value).strip() for value in cycle_titles.values()):
        raise AmendmentError("cycle-title-set-mismatch")

    cycle_records: list[Dict[str, Any]] = []
    binding_by_cycle: Dict[str, Dict[str, str]] = {}
    manifest_sources: list[Dict[str, str]] = []
    for cycle_id in cycle_ids:
        record_path = _cycle_record_path(root, cycle_id)
        record = _read_json(record_path)
        if record is None or record.get("cycle_id") != cycle_id or record.get("campaign_id") != campaign_id:
            raise AmendmentError(f"cycle-record-binding-mismatch:{cycle_id}")
        if record.get("state") != "sealed" or record.get("cycle_state") not in {"completed", "abandoned"}:
            raise AmendmentError(f"cycle-not-sealed:{cycle_id}")
        locator = record.get("locator")
        if not isinstance(locator, str) or "/" in locator or locator in {"", ".", ".."}:
            raise AmendmentError(f"cycle-locator-invalid:{cycle_id}")
        cycle_dir = campaign_json.parent / locator
        try:
            binding_doc = artifact_locator.read_cycle_binding(cycle_dir)
        except artifact_locator.LocatorError:
            binding_doc = None
        if (binding_doc is None or binding_doc["campaign_id"] != campaign_id
                or binding_doc["cycle_id"] != cycle_id):
            raise AmendmentError(f"cycle-binding-mismatch:{cycle_id}")
        manifest_path = cycle_dir / "manifest.json"
        binding = _manifest_binding(manifest_path, root_id=identity.artifact_root_id,
                                    repository_id=identity.repository_id,
                                    campaign_id=campaign_id, cycle_id=cycle_id)
        binding_by_cycle[cycle_id] = binding
        manifest_raw = _read_bytes(manifest_path)
        assert manifest_raw is not None
        manifest_sources.append({"cycle_id": cycle_id, "path": manifest_path.relative_to(root).as_posix(),
                                 "raw_digest": digest_bytes(manifest_raw), **binding})
        cycle_records.append(record)
    all_bindings = sorted(binding_by_cycle.values(), key=lambda row: row["manifest_revision_id"])
    _validate_bindings(all_bindings, "manifest-bindings")

    campaign_sidecar = _load_sidecar(root / CAMPAIGN_METADATA_REL, CAMPAIGN_SCHEMA,
                                     identity.artifact_root_id, identity.repository_id,
                                     {"campaign_id", "key", "goal", "manifest_bindings"})
    cycle_sidecar = _load_sidecar(root / CYCLE_TITLES_REL, CYCLE_SCHEMA,
                                  identity.artifact_root_id, identity.repository_id,
                                  {"campaign_id", "cycle_id", "display_title", "manifest_bindings"})
    campaign_entries = [row for row in campaign_sidecar["entries"] if row.get("campaign_id") != campaign_id]
    campaign_entries.append({"campaign_id": campaign_id, "key": key, "goal": goal, "manifest_bindings": all_bindings})
    campaign_entries.sort(key=lambda row: str(row["campaign_id"]))
    cycle_entries = [row for row in cycle_sidecar["entries"] if row.get("campaign_id") != campaign_id]
    for cycle_id in cycle_ids:
        cycle_entries.append({"campaign_id": campaign_id, "cycle_id": cycle_id,
                              "display_title": str(cycle_titles[cycle_id]).strip(),
                              "manifest_bindings": [binding_by_cycle[cycle_id]]})
    cycle_entries.sort(key=lambda row: (str(row["campaign_id"]), str(row["cycle_id"])))
    intended_campaign_sidecar = {**campaign_sidecar, "entries": campaign_entries}
    intended_cycle_sidecar = {**cycle_sidecar, "entries": cycle_entries}

    updated = dict(campaign)
    updated["key"] = key
    updated["goal"] = goal.strip()
    del updated["degraded"]
    del updated["degraded_reason"]
    campaign_pre = _read_bytes(campaign_json)
    assert campaign_pre is not None
    campaign_post = canonical(updated) + b"\n"
    targets = []
    for path, post in (
        (campaign_json, campaign_post),
        (root / CAMPAIGN_METADATA_REL, canonical(intended_campaign_sidecar) + b"\n"),
        (root / CYCLE_TITLES_REL, canonical(intended_cycle_sidecar) + b"\n"),
    ):
        pre = _read_bytes(path, missing_ok=True)
        targets.append({"path": path.relative_to(root).as_posix(), "pre_exists": pre is not None,
                        "pre_bytes_b64": _b64(pre), "pre_digest": digest_bytes(pre) if pre is not None else None,
                        "post_bytes_b64": _b64(post), "post_digest": digest_bytes(post)})
    package = {
        "schema": PACKAGE_SCHEMA,
        "artifact_root": str(root),
        "artifact_root_id": identity.artifact_root_id,
        "repository_id": identity.repository_id,
        "campaign_id": campaign_id,
        "campaign_path": campaign_json.parent.relative_to(root).as_posix(),
        "desired": {"key": key, "goal": goal.strip(), "cycle_titles": dict(sorted(cycle_titles.items()))},
        "campaign_preimage_digest": digest_bytes(campaign_pre),
        "manifest_sources": sorted(manifest_sources, key=lambda row: row["manifest_revision_id"]),
        "protected_files": _protected_files(root, campaign_json.parent, campaign_json, cycle_records),
        "targets": targets,
    }
    _validate_package(package)
    return package


def prepare(root: Path, *, campaign_id: str, key: str, goal: str,
            cycle_titles: Mapping[str, str]) -> Dict[str, Any]:
    """Capture one coherent preimage while holding the producer admission lock."""
    root = Path(root).resolve()
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        return _prepare_locked(root, campaign_id=campaign_id, key=key, goal=goal,
                               cycle_titles=cycle_titles)
    finally:
        artifact_admission._release_lock(root, lock_fd)


def package_digest(package: Mapping[str, Any]) -> str:
    return digest_json(package)


def _validate_package(package: Mapping[str, Any]) -> None:
    _assert_closed(package, {"schema", "artifact_root", "artifact_root_id", "repository_id", "campaign_id",
                             "campaign_path", "desired", "campaign_preimage_digest", "manifest_sources",
                             "protected_files", "targets"}, "package-fields")
    if package.get("schema") != PACKAGE_SCHEMA:
        raise AmendmentError("package-schema-mismatch")
    for field, kind in (("artifact_root_id", "artifact_root"), ("repository_id", "repository"),
                        ("campaign_id", "campaign")):
        if not artifact_identity.is_well_formed(str(package.get(field)), kind):
            raise AmendmentError(f"package-{field}-malformed")
    desired = package.get("desired")
    if not isinstance(desired, Mapping):
        raise AmendmentError("package-desired-required")
    _assert_closed(desired, {"key", "goal", "cycle_titles"}, "package-desired-fields")
    if _KEY_RE.fullmatch(str(desired.get("key", ""))) is None or desired.get("key") == "_unassigned":
        raise AmendmentError("package-desired-key-invalid")
    if not isinstance(desired.get("goal"), str) or not str(desired.get("goal")).strip():
        raise AmendmentError("package-desired-goal-invalid")
    titles = desired.get("cycle_titles")
    if not isinstance(titles, Mapping) or not titles:
        raise AmendmentError("package-cycle-titles-invalid")
    for cycle_id, title in titles.items():
        if not artifact_identity.is_well_formed(str(cycle_id), "cycle") or not isinstance(title, str) or not title.strip():
            raise AmendmentError("package-cycle-title-invalid")
    sources = package.get("manifest_sources")
    if not isinstance(sources, list) or len(sources) != len(titles):
        raise AmendmentError("package-manifest-sources-invalid")
    source_rows: list[Dict[str, str]] = []
    source_cycles: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            raise AmendmentError("package-manifest-source-object-required")
        _assert_closed(source, {"cycle_id", "path", "raw_digest", "manifest_revision_id", "manifest_digest"},
                       "package-manifest-source-fields")
        cycle_id = str(source.get("cycle_id", ""))
        if cycle_id not in titles or cycle_id in source_cycles:
            raise AmendmentError("package-manifest-source-cycle-mismatch")
        source_cycles.add(cycle_id)
        source_rows.append({"manifest_revision_id": str(source.get("manifest_revision_id", "")),
                            "manifest_digest": str(source.get("manifest_digest", ""))})
    if source_cycles != set(titles):
        raise AmendmentError("package-manifest-source-cycle-set-mismatch")
    _validate_bindings(source_rows, "package-manifest-bindings")
    targets = package.get("targets")
    if not isinstance(targets, list) or len(targets) != 3:
        raise AmendmentError("package-targets-invalid")
    expected_paths = {str(Path(str(package["campaign_path"])) / "campaign.json"),
                      CAMPAIGN_METADATA_REL.as_posix(), CYCLE_TITLES_REL.as_posix()}
    observed_paths = set()
    for target in targets:
        if not isinstance(target, Mapping):
            raise AmendmentError("package-target-object-required")
        _assert_closed(target, {"path", "pre_exists", "pre_bytes_b64", "pre_digest",
                                "post_bytes_b64", "post_digest"}, "package-target-fields")
        path = str(target.get("path"))
        if path.startswith("/") or ".." in Path(path).parts:
            raise AmendmentError("package-target-path-unsafe")
        pre = _unb64(target.get("pre_bytes_b64"))
        post = _unb64(target.get("post_bytes_b64"))
        if post is None or digest_bytes(post) != target.get("post_digest"):
            raise AmendmentError("package-target-post-digest-mismatch")
        if bool(target.get("pre_exists")) != (pre is not None):
            raise AmendmentError("package-target-pre-existence-mismatch")
        if pre is not None and digest_bytes(pre) != target.get("pre_digest"):
            raise AmendmentError("package-target-pre-digest-mismatch")
        observed_paths.add(path)
    if observed_paths != expected_paths:
        raise AmendmentError("package-target-set-mismatch")
    target_by_path = {str(row["path"]): row for row in targets}
    try:
        campaign_post = json.loads((_unb64(target_by_path[str(Path(str(package["campaign_path"])) / "campaign.json")]["post_bytes_b64"]) or b"").decode("utf-8"))
        campaign_doc = json.loads((_unb64(target_by_path[CAMPAIGN_METADATA_REL.as_posix()]["post_bytes_b64"]) or b"").decode("utf-8"))
        cycle_doc = json.loads((_unb64(target_by_path[CYCLE_TITLES_REL.as_posix()]["post_bytes_b64"]) or b"").decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise AmendmentError("package-target-json-invalid") from exc
    if not all(isinstance(value, dict) for value in (campaign_post, campaign_doc, cycle_doc)):
        raise AmendmentError("package-target-json-object-required")
    if campaign_post.get("campaign_id") != package["campaign_id"] or campaign_post.get("key") != desired["key"] or campaign_post.get("goal") != desired["goal"]:
        raise AmendmentError("package-campaign-post-mismatch")
    if "degraded" in campaign_post or "degraded_reason" in campaign_post:
        raise AmendmentError("package-campaign-post-degraded")
    for doc, schema, fields in (
        (campaign_doc, CAMPAIGN_SCHEMA, {"campaign_id", "key", "goal", "manifest_bindings"}),
        (cycle_doc, CYCLE_SCHEMA, {"campaign_id", "cycle_id", "display_title", "manifest_bindings"}),
    ):
        _assert_closed(doc, {"schema", "artifact_root_id", "repository_id", "entries"}, "package-sidecar-fields")
        if doc.get("schema") != schema or doc.get("artifact_root_id") != package["artifact_root_id"] or doc.get("repository_id") != package["repository_id"]:
            raise AmendmentError("package-sidecar-identity-mismatch")
        if not isinstance(doc.get("entries"), list):
            raise AmendmentError("package-sidecar-entries-invalid")
        for entry in doc["entries"]:
            if not isinstance(entry, Mapping):
                raise AmendmentError("package-sidecar-entry-invalid")
            _assert_closed(entry, fields, "package-sidecar-entry-fields")
            _validate_bindings(entry.get("manifest_bindings"), "package-sidecar-bindings")
    expected_bindings = sorted(source_rows, key=lambda row: row["manifest_revision_id"])
    matching_campaign = [row for row in campaign_doc["entries"] if row.get("campaign_id") == package["campaign_id"]]
    if matching_campaign != [{"campaign_id": package["campaign_id"], "key": desired["key"], "goal": desired["goal"],
                              "manifest_bindings": expected_bindings}]:
        raise AmendmentError("package-campaign-sidecar-binding-mismatch")
    by_cycle = {str(source["cycle_id"]): {"manifest_revision_id": str(source["manifest_revision_id"]),
                                          "manifest_digest": str(source["manifest_digest"])} for source in sources}
    matching_cycles = [row for row in cycle_doc["entries"] if row.get("campaign_id") == package["campaign_id"]]
    expected_cycles = sorted(
        ({"campaign_id": package["campaign_id"], "cycle_id": cycle_id, "display_title": title,
          "manifest_bindings": [by_cycle[cycle_id]]} for cycle_id, title in titles.items()),
        key=lambda row: (row["campaign_id"], row["cycle_id"]),
    )
    if matching_cycles != expected_cycles:
        raise AmendmentError("package-cycle-sidecar-binding-mismatch")
    protected = package.get("protected_files")
    if not isinstance(protected, list) or not protected:
        raise AmendmentError("package-protected-files-required")
    paths = [row.get("path") for row in protected if isinstance(row, Mapping)]
    if len(paths) != len(protected) or paths != sorted(paths) or len(set(paths)) != len(paths):
        raise AmendmentError("package-protected-files-not-canonical")


def _target_state(root: Path, target: Mapping[str, Any]) -> str:
    raw = _read_bytes(root / str(target["path"]), missing_ok=True)
    if raw == _unb64(target.get("pre_bytes_b64")):
        return "pre"
    if raw == _unb64(target.get("post_bytes_b64")):
        return "post"
    return "drift"


def _verify_protected(root: Path, package: Mapping[str, Any]) -> None:
    for row in package["protected_files"]:
        raw = _read_bytes(root / str(row["path"]))
        assert raw is not None
        if digest_bytes(raw) != row.get("digest"):
            raise AmendmentError(f"protected-file-drift:{row['path']}")


def _verify_live_sources(root: Path, package: Mapping[str, Any], *, pre_apply: bool) -> None:
    identity = _read_json(root / ROOT_IDENTITY_REL)
    if identity is None or identity.get("artifact_root_id") != package["artifact_root_id"] or identity.get("repository_id") != package["repository_id"]:
        raise AmendmentError("root-identity-drift")
    campaign_path = root / str(package["campaign_path"]) / "campaign.json"
    campaign = _read_json(campaign_path)
    assert campaign is not None
    desired = package["desired"]
    if pre_apply:
        try:
            folded_state = artifact_campaign.campaign_state(root, campaign_path, campaign).state
        except artifact_campaign.CampaignError as exc:
            raise AmendmentError(f"campaign-state-invalid:{exc.code}") from exc
        if folded_state != "active" or campaign.get("key") != "_unassigned":
            raise AmendmentError("campaign-precondition-drift")
        if campaign.get("degraded") is not True or campaign.get("degraded_reason") != "campaign-unassigned":
            raise AmendmentError("campaign-degraded-precondition-drift")
    for _, row in _campaign_records(root):
        if row.get("campaign_id") != package["campaign_id"] and row.get("key") == desired["key"]:
            raise AmendmentError("campaign-key-collision")
    source_cycles = []
    for source in package["manifest_sources"]:
        manifest_path = root / str(source["path"])
        raw = _read_bytes(manifest_path)
        assert raw is not None
        if digest_bytes(raw) != source["raw_digest"]:
            raise AmendmentError(f"manifest-raw-drift:{source['cycle_id']}")
        binding = _manifest_binding(manifest_path, root_id=str(package["artifact_root_id"]),
                                    repository_id=str(package["repository_id"]),
                                    campaign_id=str(package["campaign_id"]), cycle_id=str(source["cycle_id"]))
        if binding != {"manifest_revision_id": source["manifest_revision_id"], "manifest_digest": source["manifest_digest"]}:
            raise AmendmentError(f"manifest-binding-drift:{source['cycle_id']}")
        source_cycles.append(str(source["cycle_id"]))
    if sorted(source_cycles) != sorted(campaign.get("cycles", [])):
        raise AmendmentError("campaign-membership-drift")


def _verify_semantics(root: Path, package: Mapping[str, Any]) -> None:
    desired = package["desired"]
    campaign_path = root / str(package["campaign_path"]) / "campaign.json"
    campaign = _read_json(campaign_path)
    assert campaign is not None
    if campaign.get("campaign_id") != package["campaign_id"] or campaign.get("key") != desired["key"] or campaign.get("goal") != desired["goal"]:
        raise AmendmentError("campaign-amendment-not-applied")
    if "degraded" in campaign or "degraded_reason" in campaign:
        raise AmendmentError("campaign-degraded-marker-remains")
    for _, row in _campaign_records(root):
        if row.get("campaign_id") != package["campaign_id"] and row.get("key") == desired["key"]:
            raise AmendmentError("campaign-key-collision")
    campaign_sidecar = _load_sidecar(root / CAMPAIGN_METADATA_REL, CAMPAIGN_SCHEMA,
                                     str(package["artifact_root_id"]), str(package["repository_id"]),
                                     {"campaign_id", "key", "goal", "manifest_bindings"})
    cycle_sidecar = _load_sidecar(root / CYCLE_TITLES_REL, CYCLE_SCHEMA,
                                  str(package["artifact_root_id"]), str(package["repository_id"]),
                                  {"campaign_id", "cycle_id", "display_title", "manifest_bindings"})
    manifest_rows = package["manifest_sources"]
    expected_bindings = [{"manifest_revision_id": row["manifest_revision_id"], "manifest_digest": row["manifest_digest"]}
                         for row in manifest_rows]
    expected_bindings.sort(key=lambda row: row["manifest_revision_id"])
    matching_campaign = [row for row in campaign_sidecar["entries"] if row["campaign_id"] == package["campaign_id"]]
    if matching_campaign != [{"campaign_id": package["campaign_id"], "key": desired["key"],
                              "goal": desired["goal"], "manifest_bindings": expected_bindings}]:
        raise AmendmentError("campaign-sidecar-entry-mismatch")
    source_by_cycle = {row["cycle_id"]: {"manifest_revision_id": row["manifest_revision_id"],
                                         "manifest_digest": row["manifest_digest"]} for row in manifest_rows}
    matching_cycles = [row for row in cycle_sidecar["entries"] if row["campaign_id"] == package["campaign_id"]]
    expected_cycles = [{"campaign_id": package["campaign_id"], "cycle_id": cycle_id,
                        "display_title": title, "manifest_bindings": [source_by_cycle[cycle_id]]}
                       for cycle_id, title in desired["cycle_titles"].items()]
    expected_cycles.sort(key=lambda row: (row["campaign_id"], row["cycle_id"]))
    if matching_cycles != expected_cycles:
        raise AmendmentError("cycle-sidecar-entry-mismatch")


def verify(package: Mapping[str, Any]) -> Dict[str, Any]:
    _validate_package(package)
    root = Path(str(package["artifact_root"])).resolve()
    for target in package["targets"]:
        if _target_state(root, target) != "post":
            raise AmendmentError(f"target-not-applied:{target['path']}")
    _verify_protected(root, package)
    _verify_live_sources(root, package, pre_apply=False)
    _verify_semantics(root, package)
    return {"status": "verified", "package_digest": package_digest(package),
            "campaign_id": package["campaign_id"], "cycles": len(package["desired"]["cycle_titles"])}


def _journal_path(root: Path, digest: str) -> Path:
    return root / PRODUCER_REL / "metadata-amendments" / (digest.removeprefix("sha256:") + ".journal.json")


def _restore_targets(root: Path, targets: Sequence[Mapping[str, Any]]) -> None:
    for target in targets:
        path = root / str(target["path"])
        state = _target_state(root, target)
        if state not in {"pre", "post"}:
            raise AmendmentError(f"rollback-target-drift:{target['path']}")
    for target in targets:
        path = root / str(target["path"])
        pre = _unb64(target.get("pre_bytes_b64"))
        if pre is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            write_atomic_bytes(path, pre)


def apply(package: Mapping[str, Any], *, expected_package_digest: str,
          fault_after_writes: int | None = None) -> Dict[str, Any]:
    _validate_package(package)
    digest = package_digest(package)
    if expected_package_digest != digest:
        raise AmendmentError("package-digest-mismatch")
    root = Path(str(package["artifact_root"])).resolve()
    journal_path = _journal_path(root, digest)
    lock_fd = artifact_admission._acquire_lock(root, artifact_admission.LOCK_TIMEOUT_DEFAULT)
    try:
        journal = _read_json(journal_path, missing_ok=True)
        if journal is not None:
            _assert_closed(journal, {"schema", "state", "package_digest", "targets"}, "journal-fields")
            if journal.get("schema") != JOURNAL_SCHEMA or journal.get("package_digest") != digest or journal.get("targets") != package["targets"]:
                raise AmendmentError("journal-binding-mismatch")
            if journal.get("state") == "committed":
                result = verify(package)
                return {**result, "status": "already-applied", "journal": str(journal_path)}
            if journal.get("state") == "prepared":
                _restore_targets(root, package["targets"])
                journal["state"] = "rolled-back"
                write_atomic(journal_path, journal)
            elif journal.get("state") not in {"rolled-back"}:
                raise AmendmentError("journal-state-invalid")
        if any(_target_state(root, target) != "pre" for target in package["targets"]):
            raise AmendmentError("prepared-preimage-drift")
        _verify_protected(root, package)
        _verify_live_sources(root, package, pre_apply=True)
        journal = {"schema": JOURNAL_SCHEMA, "state": "prepared", "package_digest": digest,
                   "targets": package["targets"]}
        write_atomic(journal_path, journal)
        writes = 0
        try:
            for target in package["targets"]:
                write_atomic_bytes(root / str(target["path"]), _unb64(target["post_bytes_b64"]) or b"")
                writes += 1
                if fault_after_writes is not None and writes >= fault_after_writes:
                    raise AmendmentError("injected-apply-failure")
            result = verify(package)
        except Exception:
            try:
                _restore_targets(root, package["targets"])
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


def _parse_cycle_titles(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        cycle_id, sep, title = value.partition("=")
        if not sep or not artifact_identity.is_well_formed(cycle_id, "cycle") or not title.strip():
            raise AmendmentError(f"cycle-title-invalid:{value}")
        if cycle_id in result:
            raise AmendmentError(f"cycle-title-duplicate:{cycle_id}")
        result[cycle_id] = title.strip()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--artifact-root", type=Path, required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--cycle-title", action="append", default=[], required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("apply")
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--expect-package-digest", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--package", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            package = prepare(args.artifact_root, campaign_id=args.campaign, key=args.key,
                              goal=args.goal, cycle_titles=_parse_cycle_titles(args.cycle_title))
            write_atomic(args.output, package)
            result = {"status": "prepared", "package": str(args.output),
                      "package_digest": package_digest(package), "campaign_id": args.campaign}
        else:
            package = _read_json(args.package)
            assert package is not None
            result = (apply(package, expected_package_digest=args.expect_package_digest)
                      if args.command == "apply" else verify(package))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (AmendmentError, artifact_admission.AdmissionBusy) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
