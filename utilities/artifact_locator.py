#!/usr/bin/env python3
"""Human-readable campaign/cycle locators and their rebuildable path cache.

Stable IDs remain record data. Directory names are display values only; this
module discovers IDs by reading ``campaign.json``/``manifest.json`` (or an open
producer side record), never by parsing a locator name.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple


INDEX_JSON = "INDEX.json"
INDEX_MD = "INDEX.md"
CYCLE_BINDING = ".cycle.json"
MAX_SLUG_LENGTH = 48
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_CAMPAIGN_ID = re.compile(r"^camp_[0-9a-f]{32}$")
_CYCLE_ID = re.compile(r"^cyc_[0-9a-f]{32}$")


class LocatorError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def slugify(value: str, *, fallback: Optional[str] = None) -> Tuple[str, bool]:
    """Return the D-88 ASCII slug and whether its 48-character cap truncated it."""

    raw = _NON_SLUG.sub("-", str(value).lower()).strip("-")
    if not raw:
        if fallback is None:
            raise LocatorError("slug-empty")
        raw = _NON_SLUG.sub("-", str(fallback).lower()).strip("-") or "unnamed"
    truncated = len(raw) > MAX_SLUG_LENGTH
    return raw[:MAX_SLUG_LENGTH].rstrip("-"), truncated


def date_part(timestamp: str) -> str:
    match = _DATE_PREFIX.match(str(timestamp))
    if match is None:
        raise LocatorError("locator-date-invalid", str(timestamp))
    return match.group(0)


def locator_base(timestamp: str, slug: str) -> str:
    normalized, _truncated = slugify(slug)
    return f"{date_part(timestamp)}_{normalized}"


def validate_component(value: Any) -> str:
    """Validate one persisted display locator before using it in a join."""

    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or _SAFE_COMPONENT.fullmatch(value) is None
    ):
        raise LocatorError("locator-invalid-component", str(value))
    return value


def safe_child(root: Path, parent: Path, component: Any) -> Path:
    """Join one record locator and reject path or existing-symlink escape."""

    root_resolved = Path(root).resolve(strict=False)
    candidate = Path(parent) / validate_component(component)
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LocatorError("locator-outside-artifact-root", str(candidate)) from exc
    return candidate


def allocate_locator(parent: Path, timestamp: str, slug: str) -> Tuple[str, str]:
    """Choose the smallest unused suffix once: ``""``, ``-2``, ``-3`` ..."""

    base = locator_base(timestamp, slug)
    suffix = ""
    ordinal = 1
    while True:
        candidate = Path(parent) / f"{base}{suffix}"
        try:
            candidate.lstat()
            occupied = True
        except FileNotFoundError:
            occupied = False
        if not occupied:
            break
        ordinal += 1
        suffix = f"-{ordinal}"
    return f"{base}{suffix}", suffix


def campaigns_dir(root: Path) -> Path:
    return Path(root) / "campaigns"


def iter_campaign_dirs(root: Path) -> Iterator[Path]:
    base = campaigns_dir(root)
    if not base.is_dir() or base.is_symlink():
        return
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / "campaign.json").is_file() or _campaign_from_manifests(entry) is not None:
            yield entry


def iter_cycle_dirs(campaign: Path) -> Iterator[Tuple[Path, str]]:
    """Yield new direct children and legacy ``cycles/*`` children."""

    rows = []
    campaign = Path(campaign)
    if not campaign.is_dir() or campaign.is_symlink():
        return
    for entry in campaign.iterdir():
        if entry.name.startswith(".") or entry.name == "cycles":
            continue
        if entry.is_dir() and not entry.is_symlink():
            rows.append((entry, "readable"))
    legacy = campaign / "cycles"
    if legacy.is_dir() and not legacy.is_symlink():
        for entry in legacy.iterdir():
            if entry.name.startswith("."):
                continue
            if entry.is_dir() and not entry.is_symlink():
                rows.append((entry, "legacy-id"))
    for row in sorted(rows, key=lambda item: item[0].as_posix()):
        yield row


def _cycle_record_path(root: Path, cycle_id: str) -> Path:
    if not isinstance(cycle_id, str) or _CYCLE_ID.fullmatch(cycle_id) is None:
        raise LocatorError("locator-cycle-id-invalid", str(cycle_id))
    parent = Path(root) / ".runtime" / "artifact-producer" / "v1" / "cycles"
    return safe_child(root, parent, f"{cycle_id}.json")


def read_cycle_record(root: Path, cycle_id: str) -> Optional[Dict[str, Any]]:
    try:
        return _read_json(_cycle_record_path(root, cycle_id))
    except LocatorError:
        return None


def _manifest_cycle_id(path: Path) -> Optional[str]:
    manifest = _read_json(path / "manifest.json")
    cycle = manifest.get("cycle") if manifest else None
    value = cycle.get("cycle_id") if isinstance(cycle, dict) else None
    if value is None:
        return None
    if not isinstance(value, str) or _CYCLE_ID.fullmatch(value) is None:
        raise LocatorError("locator-cycle-id-invalid", str(value))
    return value


def cycle_binding_bytes(campaign_id: str, cycle_id: str) -> bytes:
    if _CAMPAIGN_ID.fullmatch(str(campaign_id)) is None:
        raise LocatorError("locator-campaign-id-invalid", str(campaign_id))
    if _CYCLE_ID.fullmatch(str(cycle_id)) is None:
        raise LocatorError("locator-cycle-id-invalid", str(cycle_id))
    payload = {
        "schema_version": 1,
        "kind": "artifact-cycle-binding",
        "campaign_id": campaign_id,
        "cycle_id": cycle_id,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_cycle_binding(path: Path) -> Optional[Dict[str, Any]]:
    marker = Path(path) / CYCLE_BINDING
    if not marker.exists() and not marker.is_symlink():
        return None
    binding = _read_json(marker)
    if binding is None or set(binding) != {"schema_version", "kind", "campaign_id", "cycle_id"}:
        raise LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if binding.get("schema_version") != 1 or binding.get("kind") != "artifact-cycle-binding":
        raise LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if _CAMPAIGN_ID.fullmatch(str(binding.get("campaign_id"))) is None:
        raise LocatorError("locator-campaign-id-invalid", str(binding.get("campaign_id")))
    if _CYCLE_ID.fullmatch(str(binding.get("cycle_id"))) is None:
        raise LocatorError("locator-cycle-id-invalid", str(binding.get("cycle_id")))
    return binding


def _campaign_from_manifests(path: Path) -> Optional[Dict[str, Any]]:
    """Recover a campaign record view for legacy admission-only folders.

    Historical ``artifact_admission`` transactions published sealed manifests
    without a mutable campaign.json. Those folders remain readable; their IDs
    come from manifest records, never directory names.
    """

    campaign_rows = []
    cycle_ids = []
    for cycle_path, _layout in iter_cycle_dirs(path):
        manifest = _read_json(cycle_path / "manifest.json")
        campaign = manifest.get("campaign") if manifest else None
        cycle = manifest.get("cycle") if manifest else None
        campaign_id = campaign.get("campaign_id") if isinstance(campaign, dict) else None
        cycle_id = cycle.get("cycle_id") if isinstance(cycle, dict) else None
        if campaign_id is not None and (
            not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None
        ):
            raise LocatorError("locator-campaign-id-invalid", str(campaign_id))
        if not isinstance(campaign_id, str):
            continue
        campaign_rows.append(dict(campaign))
        if cycle_id is not None and (
            not isinstance(cycle_id, str) or _CYCLE_ID.fullmatch(cycle_id) is None
        ):
            raise LocatorError("locator-cycle-id-invalid", str(cycle_id))
        if isinstance(cycle_id, str):
            cycle_ids.append(cycle_id)
    if not campaign_rows:
        return None
    identifiers = {row["campaign_id"] for row in campaign_rows}
    if len(identifiers) != 1:
        raise LocatorError("locator-campaign-id-conflict", path.as_posix())
    result = campaign_rows[0]
    result["cycles"] = sorted(set(cycle_ids))
    return result


def _cycle_id_for_open_dir(root: Path, campaign: Mapping[str, Any], path: Path, layout: str) -> Optional[str]:
    campaign_id = campaign.get("campaign_id")
    cycle_ids = campaign.get("cycles", []) if isinstance(campaign.get("cycles"), list) else []
    for cycle_id in cycle_ids:
        if not isinstance(cycle_id, str):
            continue
        record = read_cycle_record(root, cycle_id)
        if not record or record.get("campaign_id") != campaign_id:
            continue
        if layout == "readable" and record.get("locator") == path.name:
            return cycle_id
        # Compatibility only: old directories were created with the recorded
        # ID as their locator. The ID comes from records, never from the path.
        if layout == "legacy-id" and path.name == cycle_id:
            return cycle_id
    return None


def scan_index(root: Path) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """Scan records into ``id -> root-relative path`` plus display metadata."""

    root = Path(root).resolve()
    mapping: Dict[str, str] = {}
    rows: Dict[str, Dict[str, str]] = {}

    def add(identifier: str, path: Path, *, title: str, started: str, status: str) -> None:
        rel = path.resolve().relative_to(root).as_posix()
        previous = mapping.get(identifier)
        if previous is not None and previous != rel:
            raise LocatorError("locator-index-duplicate-id", identifier)
        mapping[identifier] = rel
        rows[identifier] = {
            "title": title,
            "started": started,
            "status": status,
            "path": rel,
        }

    for campaign_path in iter_campaign_dirs(root):
        campaign = _read_json(campaign_path / "campaign.json")
        manifest_campaign = _campaign_from_manifests(campaign_path)
        if campaign is None:
            campaign = manifest_campaign
        if campaign is None:
            continue
        campaign_id = campaign.get("campaign_id")
        if not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None:
            continue
        if manifest_campaign is not None and manifest_campaign.get("campaign_id") != campaign_id:
            raise LocatorError("locator-campaign-id-conflict", campaign_path.as_posix())
        campaign_title = str(campaign.get("title") or campaign.get("slug") or "unnamed")
        add(
            campaign_id,
            campaign_path,
            title=campaign_title,
            started=str(campaign.get("created_on") or ""),
            status=str(campaign.get("state") or "unknown"),
        )
        unresolved = []
        assigned_ids = set()
        for cycle_path, layout in iter_cycle_dirs(campaign_path):
            binding = read_cycle_binding(cycle_path) if layout == "readable" else None
            if binding is not None and binding.get("campaign_id") != campaign_id:
                raise LocatorError("locator-cycle-binding-campaign-mismatch", cycle_path.as_posix())
            cycle_id = _manifest_cycle_id(cycle_path)
            if cycle_id is not None and binding is not None and binding.get("cycle_id") != cycle_id:
                raise LocatorError("locator-cycle-binding-id-mismatch", cycle_path.as_posix())
            if cycle_id is None and binding is not None:
                bound_id = binding["cycle_id"]
                bound_record = read_cycle_record(root, bound_id)
                cycle_ids = campaign.get("cycles", []) if isinstance(campaign.get("cycles"), list) else []
                if (
                    bound_record is None
                    or bound_record.get("campaign_id") != campaign_id
                    or bound_id not in cycle_ids
                ):
                    raise LocatorError("locator-cycle-binding-unverified", cycle_path.as_posix())
                cycle_id = bound_id
            if cycle_id is None:
                cycle_id = _cycle_id_for_open_dir(root, campaign, cycle_path, layout)
            if cycle_id is None:
                if layout == "readable" and (cycle_path / "artifacts").is_dir():
                    unresolved.append(cycle_path)
                continue
            assigned_ids.add(cycle_id)
            record = read_cycle_record(root, cycle_id) or {}
            add(
                cycle_id,
                cycle_path,
                title=str(record.get("title") or record.get("slug") or campaign_title),
                started=str(record.get("started_on") or ""),
                status=str(record.get("state") or ("sealed" if (cycle_path / "manifest.json").is_file() else "open")),
            )
        # A manual rename changes only the display locator. For an open cycle
        # there is no manifest yet, so bind the sole remaining record to the
        # sole remaining direct cycle directory. Ambiguity stays unresolved;
        # a path name is never used as an ID seed or guessed among peers.
        remaining_records = []
        cycle_ids = campaign.get("cycles", []) if isinstance(campaign.get("cycles"), list) else []
        for cycle_id in cycle_ids:
            if not isinstance(cycle_id, str) or cycle_id in assigned_ids:
                continue
            record = read_cycle_record(root, cycle_id)
            if (
                record
                and record.get("campaign_id") == campaign_id
                and record.get("state") == "open"
            ):
                remaining_records.append((cycle_id, record))
        if len(unresolved) == 1 and len(remaining_records) == 1:
            cycle_path = unresolved[0]
            cycle_id, record = remaining_records[0]
            add(
                cycle_id,
                cycle_path,
                title=str(record.get("title") or record.get("slug") or campaign_title),
                started=str(record.get("started_on") or ""),
                status="open",
            )
    return dict(sorted(mapping.items())), rows


def _atomic_write(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp), str(path))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _markdown(rows: Mapping[str, Mapping[str, str]]) -> bytes:
    def cell(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Artifact campaigns index",
        "",
        "Derived cache; campaign and cycle records are authoritative.",
        "",
        "| ID | Title | Started | Status | Path |",
        "|---|---|---|---|---|",
    ]
    for identifier in sorted(rows):
        row = rows[identifier]
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} |".format(
                cell(identifier), cell(row["title"]), cell(row["started"]),
                cell(row["status"]), cell(row["path"]),
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _index_json_bytes(mapping: Mapping[str, str]) -> bytes:
    return (json.dumps(mapping, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _regular_bytes(path: Path) -> Optional[bytes]:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _restore_file(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, previous)


def rebuild_indexes(root: Path) -> Dict[str, str]:
    root = Path(root).resolve()
    base = campaigns_dir(root)
    if not base.is_dir() or base.is_symlink():
        return {}
    mapping, rows = scan_index(root)
    json_path = base / INDEX_JSON
    markdown_path = base / INDEX_MD
    previous_json = _regular_bytes(json_path)
    previous_markdown = _regular_bytes(markdown_path)
    try:
        _atomic_write(json_path, _index_json_bytes(mapping))
        _atomic_write(markdown_path, _markdown(rows))
    except OSError:
        # Two filenames cannot share one POSIX rename. Roll the first replace
        # back if the second fails; crash residue is detected and healed by
        # resolve_path's authoritative record scan on the next access.
        try:
            _restore_file(json_path, previous_json)
            _restore_file(markdown_path, previous_markdown)
        except OSError:
            pass
        raise
    return mapping


def _load_index(root: Path) -> Optional[Dict[str, str]]:
    payload = _read_json(campaigns_dir(root) / INDEX_JSON)
    if payload is None or any(not isinstance(key, str) or not isinstance(value, str)
                              for key, value in payload.items()):
        return None
    return dict(payload)


def _path_has_id(root: Path, path: Path, identifier: str) -> bool:
    root = Path(root).resolve()
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    campaign = _read_json(path / "campaign.json") or _campaign_from_manifests(path)
    if campaign and campaign.get("campaign_id") == identifier:
        return True
    if _manifest_cycle_id(path) == identifier:
        return True
    binding = read_cycle_binding(path)
    if binding is not None and binding.get("cycle_id") == identifier:
        record = read_cycle_record(root, identifier)
        return bool(record and record.get("campaign_id") == binding.get("campaign_id"))
    record = read_cycle_record(root, identifier)
    if not record:
        return False
    campaign_id = record.get("campaign_id")
    parent = path.parent.parent if path.parent.name == "cycles" else path.parent
    parent_record = _read_json(parent / "campaign.json")
    if not parent_record or parent_record.get("campaign_id") != campaign_id:
        return False
    if path.parent.name == "cycles":
        return path.name == identifier
    # Direct open cycles can be manually renamed before they have a manifest.
    # Reuse the same record-based, ambiguity-failing scan that produced the
    # candidate instead of treating the stale display locator as identity.
    try:
        mapping, _rows = scan_index(root)
        relative = path.resolve().relative_to(root).as_posix()
    except (LocatorError, OSError, RuntimeError, ValueError):
        return False
    return mapping.get(identifier) == relative


def find_path_by_id(root: Path, identifier: str) -> Optional[Path]:
    mapping, _rows = scan_index(root)
    rel = mapping.get(identifier)
    return Path(root).resolve() / rel if rel else None


def resolve_path(root: Path, identifier: str) -> Optional[Path]:
    """Resolve through the cache, rebuilding by record scan on miss or drift."""

    root = Path(root).resolve()
    cached = _load_index(root)
    scanned, rows = scan_index(root)
    json_current = _regular_bytes(campaigns_dir(root) / INDEX_JSON)
    markdown_current = _regular_bytes(campaigns_dir(root) / INDEX_MD)
    if (
        cached != scanned
        or json_current != _index_json_bytes(scanned)
        or markdown_current != _markdown(rows)
    ):
        scanned = rebuild_indexes(root)
    rel = scanned.get(identifier)
    candidate = root / rel if rel else None
    return candidate if candidate is not None and _path_has_id(root, candidate, identifier) else None
