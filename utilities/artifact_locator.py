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
from dataclasses import dataclass
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional,
                     Sequence, Set, Tuple)


INDEX_JSON = "INDEX.json"
INDEX_MD = "INDEX.md"
CYCLE_BINDING = ".cycle.json"
CAMPAIGN_EVENTS_DIR = "campaign.events"
MAX_SLUG_LENGTH = 48

# id -> {title, started, status, campaign, path}, the row shape both the full
# scan and every incremental recompute produce -- sharing this shape (and the
# `scan_campaign` function that fills it) is what makes the two byte-identical
# by construction rather than by convention.
Rows = Dict[str, Dict[str, str]]
_NON_SLUG = re.compile(r"[^a-z0-9]+")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
_CAMPAIGN_ID = re.compile(r"^camp_[0-9a-f]{32}$")
_CYCLE_ID = re.compile(r"^cyc_[0-9a-f]{32}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BINDING_REQUIRED = frozenset({"schema_version", "kind", "campaign_id", "cycle_id"})
# `started_on` is display data (D-88: the date prefix shows `cycle.started_on`);
# the binding carries the full timestamp so same-day cycles stay orderable
# from the folder alone. Bindings written before it existed omit it. A cycle
# whose work predates the producer (W7G resplit, W7H residue) knows only the
# work's date, so a date-only value is allowed and means exactly that.
_BINDING_OPTIONAL = frozenset({"started_on"})


def started_on_is_valid(value: Any) -> bool:
    return isinstance(value, str) and (_RFC3339.fullmatch(value) is not None
                                       or _DATE_ONLY.fullmatch(value) is not None)


def display_started_on(record: Optional[Mapping[str, Any]],
                       manifest: Optional[Mapping[str, Any]] = None) -> Optional[str]:
    """The start a reader wants: the work's date for a resplit cycle (D-79
    ``resplit_started_on``, which is also what the folder name shows), else the
    record's own ``started_on``, else the sealed manifest's. Same order as
    ``artifact_relayout._cycle_date``; never a path date or an mtime."""
    record = record or {}
    # A time recovered from the pre-migration backup (original file mtimes,
    # `cycle-time-recovery`) is the best evidence there is; it comes first.
    if started_on_is_valid(record.get("recovered_started_on")):
        return record["recovered_started_on"]
    cycle = (manifest or {}).get("cycle") if isinstance(manifest, Mapping) else None
    # A W7G/W7H cycle whose date came from a folder name, an mtime or its
    # origin cycle stores that date as midnight; the clock is a placeholder,
    # so the reader gets the date alone rather than a time nobody recorded.
    date_derived = bool(record.get("derived_from_cycle_id") or record.get("started_on_source"))
    for candidate in (record.get("resplit_started_on"), record.get("started_on"),
                      cycle.get("started_on") if isinstance(cycle, Mapping) else None):
        if started_on_is_valid(candidate):
            if date_derived and candidate.endswith("T00:00:00Z"):
                return candidate[:10]
            return candidate
    return None


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


def strip_leading_date(slug: str) -> str:
    """Drop a date a freshly named slug carries, since the locator adds one.

    Measured on BC_ResNet 2026-09-10: six campaign locators read
    ``2026-09-10_2026-09-10-r5-streaming-window-sim``, and one read
    ``2026-09-09_2026-09-10-r4-...`` where the two dates disagreed, so the
    name said one day while the sort order said another.

    Only route compilation calls this -- the one place a new slug is named.
    A migration locator legitimately
    carries two dates -- the locator date is when the content moved and the
    slug date is when the content was made (``core/CORE.md`` W7H relocation
    table, e.g. ``2026-09-05_2026-08-24-artifact-knowledge-index-w7/``) --
    so ``locator_base`` stays neutral and relayout/residue/resplit keep both.
    A slug that is nothing but a date keeps its own text.
    """

    remainder = _DATE_PREFIX.sub("", str(slug), count=1).lstrip("-_ ")
    return remainder or str(slug)


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
        if entry.name.startswith(".") or entry.name in {"cycles", CAMPAIGN_EVENTS_DIR}:
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


def cycle_binding_bytes(campaign_id: str, cycle_id: str, *, started_on: Optional[str] = None) -> bytes:
    if _CAMPAIGN_ID.fullmatch(str(campaign_id)) is None:
        raise LocatorError("locator-campaign-id-invalid", str(campaign_id))
    if _CYCLE_ID.fullmatch(str(cycle_id)) is None:
        raise LocatorError("locator-cycle-id-invalid", str(cycle_id))
    payload: Dict[str, Any] = {
        "schema_version": 1,
        "kind": "artifact-cycle-binding",
        "campaign_id": campaign_id,
        "cycle_id": cycle_id,
    }
    if started_on is not None:
        if not started_on_is_valid(started_on):
            raise LocatorError("locator-cycle-started-on-invalid", str(started_on))
        payload["started_on"] = started_on
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_cycle_binding(path: Path) -> Optional[Dict[str, Any]]:
    marker = Path(path) / CYCLE_BINDING
    if not marker.exists() and not marker.is_symlink():
        return None
    binding = _read_json(marker)
    if (binding is None or not _BINDING_REQUIRED <= set(binding)
            or set(binding) - _BINDING_REQUIRED - _BINDING_OPTIONAL):
        raise LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if binding.get("schema_version") != 1 or binding.get("kind") != "artifact-cycle-binding":
        raise LocatorError("locator-cycle-binding-invalid", marker.as_posix())
    if "started_on" in binding and not started_on_is_valid(binding["started_on"]):
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


def _campaign_view(root: Path, campaign_path: Path) -> Optional[Dict[str, Any]]:
    """Fold ``campaign.json`` (or recover a manifest-only view) into one dict.

    Returns ``None`` when the directory carries no usable campaign record --
    not an error, just nothing to scan. Raises when the two possible sources
    (a real ``campaign.json`` and the manifests underneath) name different
    campaign ids for the same directory: that is always a defect, never a
    display choice.
    """

    campaign = _read_json(campaign_path / "campaign.json")
    if campaign is not None:
        from artifact_campaign import CampaignError, fold_campaign
        try:
            campaign = fold_campaign(root, campaign_path / "campaign.json", campaign)
        except CampaignError as exc:
            raise LocatorError(exc.code, exc.detail) from exc
    manifest_campaign = _campaign_from_manifests(campaign_path)
    if campaign is None:
        campaign = manifest_campaign
    if campaign is None:
        return None
    campaign_id = campaign.get("campaign_id")
    if not isinstance(campaign_id, str) or _CAMPAIGN_ID.fullmatch(campaign_id) is None:
        return None
    if manifest_campaign is not None and manifest_campaign.get("campaign_id") != campaign_id:
        raise LocatorError("locator-campaign-id-conflict", campaign_path.as_posix())
    return campaign


def _cycle_entry_id(root: Path, campaign: Mapping[str, Any], campaign_id: str,
                     cycle_path: Path, layout: str) -> Optional[str]:
    """The verified id for one cycle directory, or ``None`` if unresolved.

    Binding, manifest and record must all agree; any disagreement is a typed
    defect (never a silent pick), matching what a full scan has always done.
    """

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
    return cycle_id


def scan_campaign(root: Path, campaign_path: Path) -> Optional[Rows]:
    """Row computation for exactly one campaign directory.

    This is the loop body a full `scan_index` merges over every campaign, and
    the same function an incremental update calls for just the touched or
    drifted ones. Sharing it is the whole equivalence proof: full and
    incremental output cannot diverge in row *content* because both compute a
    campaign's rows the same way; they can only diverge in which campaigns get
    recomputed, and `_merge_rows` treats every recomputed campaign identically
    regardless of who called it.
    """

    root = Path(root).resolve()
    campaign = _campaign_view(root, campaign_path)
    if campaign is None:
        return None
    campaign_id = campaign["campaign_id"]
    rows: Rows = {}

    def add(identifier: str, path: Path, *, title: str, started: str, status: str) -> None:
        rel = path.resolve().relative_to(root).as_posix()
        previous = rows.get(identifier)
        if previous is not None and previous["path"] != rel:
            raise LocatorError("locator-index-duplicate-id", identifier)
        rows[identifier] = {
            "title": title,
            "started": started,
            "status": status,
            "campaign": campaign_id,
            "path": rel,
        }

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
        cycle_id = _cycle_entry_id(root, campaign, campaign_id, cycle_path, layout)
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
            started=display_started_on(record) or "",
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
            started=display_started_on(record) or "",
            status="open",
        )
    # A campaign begins with its first cycle. A producer-born campaign's
    # `created_on` already is that; a W7G resplit campaign's is the resplit
    # run, so the earliest cycle start (possibly recovered) is shown instead.
    cycle_starts = [row["started"] for key, row in rows.items() if key != campaign_id and row.get("started")]
    if cycle_starts and rows[campaign_id]["started"] and min(cycle_starts) < rows[campaign_id]["started"]:
        rows[campaign_id]["started"] = min(cycle_starts)
    return rows


def _merge_rows(into: Rows, part: Rows) -> None:
    """Add one campaign's rows into the accumulated set.

    A collision here is always cross-campaign: `scan_campaign` already raised
    on any collision *inside* a single campaign's own rows before returning.
    """

    for identifier, row in part.items():
        previous = into.get(identifier)
        if previous is not None and previous["path"] != row["path"]:
            raise LocatorError("locator-index-duplicate-id", identifier)
        into[identifier] = row


def scan_index(root: Path) -> Tuple[Dict[str, str], Rows]:
    """Scan records into ``id -> root-relative path`` plus display metadata."""

    root = Path(root).resolve()
    rows: Rows = {}
    for campaign_path in iter_campaign_dirs(root):
        part = scan_campaign(root, campaign_path)
        if part is None:
            continue
        _merge_rows(rows, part)
    mapping = {identifier: row["path"] for identifier, row in rows.items()}
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


def _display_order(rows: Mapping[str, Mapping[str, str]]) -> Iterator[str]:
    """Campaigns oldest first, each followed by its cycles in start order.

    A human table sorted by ID interleaves the cycles of every campaign at
    random; several cycles share a date prefix, so the full ``started``
    timestamp is the order. Rows without a timestamp come last, by ID.
    """

    def key(identifier: str) -> Tuple[bool, str, str]:
        started = str(rows[identifier].get("started") or "")
        return (started == "", started, identifier)

    by_campaign: Dict[str, list] = {}
    for identifier, row in rows.items():
        by_campaign.setdefault(str(row.get("campaign") or ""), []).append(identifier)
    campaigns = sorted((c for c in by_campaign if c in rows), key=key)
    for campaign_id in campaigns:
        yield campaign_id
        for identifier in sorted((i for i in by_campaign[campaign_id] if i != campaign_id), key=key):
            yield identifier
    for campaign_id in sorted(c for c in by_campaign if c not in rows):
        for identifier in sorted(by_campaign[campaign_id], key=key):
            yield identifier


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
    for identifier in _display_order(rows):
        row = rows[identifier]
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} |".format(
                cell(identifier), cell(row["title"]), cell(row["started"]),
                cell(row["status"]), cell(row["path"]),
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


_MARKDOWN_HEADER: Tuple[str, ...] = (
    "# Artifact campaigns index",
    "",
    "Derived cache; campaign and cycle records are authoritative.",
    "",
    "| ID | Title | Started | Status | Path |",
    "|---|---|---|---|---|",
)


def _split_unescaped_pipes(line: str) -> Optional[List[str]]:
    """Escape-aware cell split: ``\\\\`` -> ``\\``, ``\\|`` -> ``|``, any other
    ``\\x`` is a parse failure. An unescaped ``|`` is the delimiter."""

    tokens: List[str] = []
    current: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            nxt = line[i + 1]
            if nxt == "\\":
                current.append("\\")
            elif nxt == "|":
                current.append("|")
            else:
                return None
            i += 2
            continue
        if ch == "|":
            tokens.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    tokens.append("".join(current))
    return tokens


def _parse_row_line(line: str) -> Optional[List[str]]:
    if not (line.startswith("| ") and line.endswith(" |")):
        return None
    tokens = _split_unescaped_pipes(line)
    if tokens is None or len(tokens) != 7 or tokens[0] != "" or tokens[-1] != "":
        return None
    cells = []
    for raw in tokens[1:-1]:
        if len(raw) < 2 or not (raw.startswith(" ") and raw.endswith(" ")):
            return None
        cells.append(raw[1:-1])
    return cells


def parse_index_markdown(data: bytes) -> Optional[Rows]:
    """Strict inverse of `_markdown`. Any deviation -- header, cell count,
    escape, unknown id shape, an unresolvable `cyc_*` campaign reference,
    a duplicate id -- returns ``None`` rather than a best-effort guess; the
    caller falls back to a full rebuild on ``None`` (LE §4)."""

    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None
    if not text.endswith("\n"):
        return None
    lines = text[:-1].split("\n")
    if len(lines) < len(_MARKDOWN_HEADER) or tuple(lines[:len(_MARKDOWN_HEADER)]) != _MARKDOWN_HEADER:
        return None
    campaign_paths: Dict[str, str] = {}
    parsed: List[Tuple[str, Dict[str, str]]] = []
    for line in lines[len(_MARKDOWN_HEADER):]:
        cells = _parse_row_line(line)
        if cells is None:
            return None
        identifier, title, started, status, path = cells
        if _CAMPAIGN_ID.fullmatch(identifier) is not None:
            campaign_paths[path] = identifier
        parsed.append((identifier, {"title": title, "started": started, "status": status, "path": path}))
    rows: Rows = {}
    for identifier, partial in parsed:
        if identifier in rows:
            return None
        if _CAMPAIGN_ID.fullmatch(identifier) is not None:
            campaign_ref = identifier
        elif _CYCLE_ID.fullmatch(identifier) is not None:
            parts = partial["path"].split("/")
            if len(parts) < 2 or parts[0] != "campaigns":
                return None
            campaign_ref = campaign_paths.get("/".join(parts[:2]))
            if campaign_ref is None:
                return None
        else:
            return None
        rows[identifier] = dict(partial, campaign=campaign_ref)
    return rows


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


def _load_published_rows(root: Path) -> Optional[Rows]:
    """The published rows, but only when both files round-trip byte-for-byte.

    Three checks, all required: both files are plain files; parsing then
    re-rendering `INDEX.md` reproduces it exactly; and the JSON side agrees
    with the parsed paths. Any miss returns ``None`` -- the incremental path's
    only legitimate answer is "I can't trust this cache", never a guess built
    from a partially-parsed file.
    """

    root = Path(root).resolve()
    json_bytes = _regular_bytes(campaigns_dir(root) / INDEX_JSON)
    md_bytes = _regular_bytes(campaigns_dir(root) / INDEX_MD)
    if json_bytes is None or md_bytes is None:
        return None
    rows = parse_index_markdown(md_bytes)
    if rows is None or _markdown(rows) != md_bytes:
        return None
    mapping = {identifier: row["path"] for identifier, row in rows.items()}
    if _index_json_bytes(mapping) != json_bytes:
        return None
    return rows


def render_indexes(mapping: Mapping[str, str], rows: Rows) -> Tuple[bytes, bytes]:
    return _index_json_bytes(mapping), _markdown(rows)


def expected_index_bytes(root: Path) -> Tuple[bytes, bytes]:
    """The full-rebuild byte answer. Pure (no write); the equivalence oracle."""
    mapping, rows = scan_index(root)
    return render_indexes(mapping, rows)


def _write_index_pair(root: Path, json_bytes: bytes, md_bytes: bytes) -> bool:
    """Atomic two-file replace with rollback. Skips the write (returns False)
    when both files already hold these exact bytes."""

    root = Path(root).resolve()
    base = campaigns_dir(root)
    json_path = base / INDEX_JSON
    markdown_path = base / INDEX_MD
    previous_json = _regular_bytes(json_path)
    previous_markdown = _regular_bytes(markdown_path)
    if previous_json == json_bytes and previous_markdown == md_bytes:
        return False
    base.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(json_path, json_bytes)
        _atomic_write(markdown_path, md_bytes)
    except OSError:
        # Two filenames cannot share one POSIX rename. Roll the first replace
        # back if the second fails; crash residue is detected and healed by
        # `locate`'s authoritative record scan on the next access.
        try:
            _restore_file(json_path, previous_json)
            _restore_file(markdown_path, previous_markdown)
        except OSError:
            pass
        raise
    return True


def rebuild_indexes(root: Path) -> Dict[str, str]:
    root = Path(root).resolve()
    base = campaigns_dir(root)
    if not base.is_dir() or base.is_symlink():
        return {}
    mapping, rows = scan_index(root)
    json_bytes, md_bytes = render_indexes(mapping, rows)
    _write_index_pair(root, json_bytes, md_bytes)
    return mapping


@dataclass(frozen=True)
class IndexUpdate:
    status: str                          # "unchanged" | "updated" | "rebuilt"
    rescanned: Tuple[str, ...]           # campaign rel paths recomputed
    skipped: Tuple[Dict[str, str], ...]  # unrelated problem campaigns: {"path", "code", "id"}


def _rel_prefix_rows(rows: Rows, prefix: str) -> List[str]:
    return [identifier for identifier, row in rows.items()
            if row["path"] == prefix or row["path"].startswith(prefix + "/")]


def _is_campaign_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (
        (path / "campaign.json").is_file() or _campaign_from_manifests(path) is not None)


def _lenient_rebuild(root: Path, touched: Sequence[str]) -> Tuple[Rows, Dict[str, str], str,
                                                                    Tuple[str, ...], Tuple[Dict[str, str], ...]]:
    """Full scan, one campaign at a time, that never lets an unrelated
    campaign's defect or duplicate stop the touched campaign's write.

    Used only when the published `INDEX.md`/`INDEX.json` cannot be trusted
    (missing or failing round-trip); the common incremental path never reaches
    here.
    """

    root = Path(root).resolve()
    touched_set = set(touched)
    rows: Rows = {}
    skipped: List[Dict[str, str]] = []
    for campaign_path in iter_campaign_dirs(root):
        rel = campaign_path.name
        prefix = f"campaigns/{rel}"
        cid: Optional[str] = None
        try:
            view = _campaign_view(root, campaign_path)
            cid = view.get("campaign_id") if view else None
        except LocatorError:
            cid = None
        is_target = cid is not None and cid in touched_set
        try:
            part = scan_campaign(root, campaign_path)
        except LocatorError as exc:
            if is_target:
                raise
            skipped.append({"path": prefix, "code": exc.code, "id": exc.detail or ""})
            continue
        if part is None:
            continue
        trial = dict(rows)
        try:
            _merge_rows(trial, part)
        except LocatorError as exc:
            conflict_id = exc.detail
            if is_target or conflict_id in touched_set:
                raise
            existing = rows.get(conflict_id)
            if existing is not None:
                existing_parts = existing["path"].split("/")
                if len(existing_parts) >= 2:
                    existing_prefix = f"campaigns/{existing_parts[1]}"
                    for identifier in _rel_prefix_rows(rows, existing_prefix):
                        del rows[identifier]
                    skipped.append({"path": existing_prefix, "code": exc.code, "id": conflict_id})
            skipped.append({"path": prefix, "code": exc.code, "id": conflict_id})
            continue
        rows = trial
    mapping = {identifier: row["path"] for identifier, row in rows.items()}
    return rows, mapping, "rebuilt", tuple(), tuple(skipped)


def _plan_update(root: Path, campaign_ids: Iterable[str]) -> Tuple[Rows, Dict[str, str], str,
                                                                     Tuple[str, ...], Tuple[Dict[str, str], ...]]:
    root = Path(root).resolve()
    touched = list(dict.fromkeys(campaign_ids))
    published = _load_published_rows(root)
    if published is None:
        return _lenient_rebuild(root, touched)

    touched_set = set(touched)
    on_disk = set(_campaign_entries(root))
    indexed: Set[str] = set()
    for identifier, row in published.items():
        if _CAMPAIGN_ID.fullmatch(identifier) is not None:
            parts = row["path"].split("/")
            if len(parts) >= 2:
                indexed.add(parts[1])

    touched_rels: Set[str] = set()
    for campaign_id in touched:
        row = published.get(campaign_id)
        if row is None:
            continue
        parts = row["path"].split("/")
        if len(parts) >= 2 and parts[0] == "campaigns":
            touched_rels.add(parts[1])

    rescan_rels = touched_rels | (on_disk - indexed) | (indexed - on_disk)
    rows: Rows = {identifier: dict(row) for identifier, row in published.items()}
    skipped: List[Dict[str, str]] = []
    rescanned: List[str] = []

    for rel in sorted(rescan_rels):
        prefix = f"campaigns/{rel}"
        for identifier in _rel_prefix_rows(rows, prefix):
            del rows[identifier]
        touches_target = rel in touched_rels
        campaign_path = campaigns_dir(root) / rel
        if not _is_campaign_dir(campaign_path):
            rescanned.append(rel)
            continue
        try:
            part = scan_campaign(root, campaign_path)
        except LocatorError as exc:
            if touches_target:
                raise
            skipped.append({"path": prefix, "code": exc.code, "id": exc.detail or ""})
            continue
        if part is None:
            rescanned.append(rel)
            continue
        trial = dict(rows)
        try:
            _merge_rows(trial, part)
        except LocatorError as exc:
            # "Touches the target" is about the *colliding id*, not which rel
            # happened to be rescanned first: a copy of the touched campaign
            # still names the touched campaign_id, so the touched operation
            # must refuse even when the untouched copy is the one just
            # rescanned (the campaign row -- always the first one
            # `scan_campaign` emits -- is what collides in that case).
            if touches_target or exc.detail in touched_set:
                raise
            # The copy that was never indexed is the interloper; the side
            # already published (the other rel) keeps its row untouched.
            skipped.append({"path": prefix, "code": exc.code, "id": exc.detail or ""})
            continue
        rows = trial
        rescanned.append(rel)

    mapping = {identifier: row["path"] for identifier, row in rows.items()}
    status = "updated" if rescanned or skipped else "unchanged"
    return rows, mapping, status, tuple(rescanned), tuple(skipped)


def prepare_index_update(root: Path, campaign_ids: Iterable[str]) -> IndexUpdate:
    """Read-only precheck: identical computation to `update_indexes`, no
    write. Callers run this before their own first write so a duplicate-copy
    conflict on the touched campaign is refused before any byte changes."""

    _rows, _mapping, status, rescanned, skipped = _plan_update(root, campaign_ids)
    return IndexUpdate(status=status, rescanned=rescanned, skipped=skipped)


def update_indexes(root: Path, campaign_ids: Iterable[str]) -> IndexUpdate:
    """Recompute and persist just the touched campaigns' rows.

    Requires the caller to already hold the producer-admission flock for
    `root` -- this writes the shared index and must be serialized with every
    other writer, exactly like `rebuild_indexes`.
    """

    import artifact_admission
    import dispatch_lock_order

    root = Path(root).resolve()
    if not artifact_admission.holds_lock(root):
        raise LocatorError("locator-index-lock-not-held")
    dispatch_lock_order.assert_held("producer-admission", "locator-update-indexes")
    rows, mapping, status, rescanned, skipped = _plan_update(root, campaign_ids)
    json_bytes, md_bytes = render_indexes(mapping, rows)
    wrote = _write_index_pair(root, json_bytes, md_bytes)
    if not wrote:
        status = "unchanged"
    return IndexUpdate(status=status, rescanned=rescanned, skipped=skipped)


def verify_indexes(root: Path, *, repair: bool) -> Dict[str, Any]:
    """A full, typed health check: ``current`` / ``rebuilt`` / ``stale`` / ``problems``.

    Never partially repairs: a defect anywhere makes the whole answer
    ``problems`` rather than a silently incomplete rewrite (LE §4 -- an
    unknown outcome is reported as unknown, not dressed up as success).
    """

    root = Path(root).resolve()
    try:
        mapping, rows = scan_index(root)
    except LocatorError as exc:
        return {"status": "problems", "code": exc.code, "detail": exc.detail}
    json_bytes, md_bytes = render_indexes(mapping, rows)
    current_json = _regular_bytes(campaigns_dir(root) / INDEX_JSON)
    current_md = _regular_bytes(campaigns_dir(root) / INDEX_MD)
    if current_json == json_bytes and current_md == md_bytes:
        return {"status": "current"}
    import artifact_admission
    if repair and artifact_admission.holds_lock(root):
        _write_index_pair(root, json_bytes, md_bytes)
        return {"status": "rebuilt"}
    return {"status": "stale"}


def _load_index(root: Path) -> Optional[Dict[str, str]]:
    payload = _read_json(campaigns_dir(root) / INDEX_JSON)
    if payload is None or any(not isinstance(key, str) or not isinstance(value, str)
                              for key, value in payload.items()):
        return None
    return dict(payload)


# Module-memoized `INDEX.json` reads, keyed by the file identity that would
# change if anyone replaced it: realpath plus (dev, ino, size, mtime_ns). The
# memo is never trusted on its own -- every hit still goes through
# `_verify_candidate`'s record check -- so a stale entry can only ever cost a
# fallback scan, never a wrong answer.
_HINT_CACHE: Dict[Tuple[Any, ...], Dict[str, str]] = {}


def _hint_map(root: Path) -> Dict[str, str]:
    root = Path(root).resolve()
    json_path = campaigns_dir(root) / INDEX_JSON
    try:
        st = json_path.stat()
        key: Tuple[Any, ...] = (str(root), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    except OSError:
        key = (str(root), None)
    cached = _HINT_CACHE.get(key)
    if cached is not None:
        return cached
    loaded = _load_index(root) or {}
    _HINT_CACHE[key] = loaded
    return loaded


def _campaign_entries(root: Path) -> List[str]:
    """Directory names under `campaigns/` from one `scandir`, using `d_type`
    only (no per-name `stat`). Hidden names and non-directories are excluded.
    Does not check for `campaign.json` -- that is what makes this cheap enough
    to run on every incremental update and every 3a probe."""

    try:
        with os.scandir(campaigns_dir(Path(root).resolve())) as it:
            return [entry.name for entry in it
                    if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _verify_candidate(root: Path, path: Path, identifier: str) -> bool:
    """Prove one path names `identifier`, by the same record checks a full
    scan applies to that path shape -- except global uniqueness, which only a
    full scan or a campaign-scoped pre-check can establish. Any shape mismatch
    or `LocatorError`/`OSError` means "not proven", never a crash: the caller
    always has a fallback to try next."""

    root = Path(root).resolve()
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_dir():
            return False
        base = campaigns_dir(root)
        if base.is_symlink():
            return False
        try:
            rel = path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            return False
        parts = rel.parts
        if len(parts) < 2 or parts[0] != "campaigns" or any(p.startswith(".") for p in parts):
            return False
        if len(parts) == 2:
            view = _campaign_view(root, path)
            return view is not None and view.get("campaign_id") == identifier
        if len(parts) == 3 and parts[2] != "cycles":
            campaign_path = path.parent
            campaign = _campaign_view(root, campaign_path)
            if campaign is None:
                return False
            campaign_id = campaign.get("campaign_id")
            if _cycle_entry_id(root, campaign, campaign_id, path, "readable") != identifier:
                return False
            manifest = _read_json(path / "manifest.json")
            if manifest is not None:
                manifest_campaign = manifest.get("campaign")
                if not isinstance(manifest_campaign, dict) or manifest_campaign.get("campaign_id") != campaign_id:
                    return False
            return True
        if len(parts) == 4 and parts[2] == "cycles":
            campaign_path = path.parent.parent
            campaign = _campaign_view(root, campaign_path)
            if campaign is None:
                return False
            campaign_id = campaign.get("campaign_id")
            manifest_cycle_id = _manifest_cycle_id(path)
            if manifest_cycle_id is not None:
                return manifest_cycle_id == identifier
            record = read_cycle_record(root, identifier)
            cycle_ids = campaign.get("cycles", []) if isinstance(campaign.get("cycles"), list) else []
            return bool(
                record
                and record.get("campaign_id") == campaign_id
                and identifier in cycle_ids
                and path.name == identifier
            )
        return False
    except (LocatorError, OSError):
        return False


def _campaign_dir_via_hint_or_candidates(
    root: Path, campaign_id: str, candidates: Optional[Callable[[], Iterable[Path]]]
) -> Optional[Path]:
    """The cheap half of finding a campaign directory: verify the INDEX hint,
    then verify any candidate paths the caller already knows. No directory
    walk here -- that is `_unindexed_campaign_probe`'s job, and it costs
    nothing on this path's many hits."""

    hints = _hint_map(root)
    hinted = hints.get(campaign_id)
    if hinted:
        candidate_path = root / hinted
        if _verify_candidate(root, candidate_path, campaign_id):
            return candidate_path
    if candidates is not None:
        for candidate_path in candidates():
            candidate_path = Path(candidate_path)
            if _verify_candidate(root, candidate_path, campaign_id):
                return candidate_path
    return None


def _unindexed_campaign_probe(root: Path, campaign_id: str) -> Optional[Path]:
    """Read every un-indexed directory's own campaign record once, looking
    for `campaign_id`. Zero cost on a healthy root (`on_disk - indexed` is
    normally empty); the only path that finds a hand-renamed or hand-copied
    campaign directory the index has never seen."""

    root = Path(root).resolve()
    hints = _hint_map(root)
    indexed: Set[str] = set()
    for rel in hints.values():
        parts = rel.split("/")
        if len(parts) >= 2 and parts[0] == "campaigns":
            indexed.add(parts[1])
    found: List[Path] = []
    for name in _campaign_entries(root):
        if name in indexed:
            continue
        candidate_path = campaigns_dir(root) / name
        try:
            view = _campaign_view(root, candidate_path)
        except LocatorError:
            continue
        if view is not None and view.get("campaign_id") == campaign_id:
            found.append(candidate_path)
    if len(found) > 1:
        raise LocatorError("locator-index-duplicate-id", campaign_id)
    return found[0] if found else None


def _locate_owner_campaign(
    root: Path, campaign_id: str, candidates: Optional[Callable[[], Iterable[Path]]]
) -> Optional[Path]:
    """Find campaign `campaign_id`'s directory on its own: hint, then
    candidates, then (only on miss) the un-indexed probe."""

    found = _campaign_dir_via_hint_or_candidates(root, campaign_id, candidates)
    if found is not None:
        return found
    return _unindexed_campaign_probe(root, campaign_id)


_UNRESOLVED_OWNER = object()  # sentinel: 3a made no determination -- fall through to the lenient scan


def _heal_via_lock(root: Path, write: Callable[[], None]) -> None:
    import artifact_admission

    if artifact_admission.holds_lock(root):
        write()
        return
    fd = artifact_admission.try_acquire_lock(root)
    if fd is None:
        return
    try:
        write()
    finally:
        artifact_admission._release_lock(root, fd)


def _locate_within_owner_campaign(
    root: Path, cycle_id: str, owner_campaign_id: str,
    owner_campaign_candidates: Optional[Callable[[], Iterable[Path]]],
) -> Any:
    """§3.5 3a: once a cycle's owning campaign is known, look there instead of
    the whole root. A miss here is a proof, not a guess (D-88: a cycle
    directory's only valid location is under its own campaign; a cycle folder
    moved elsewhere already fails a full scan with
    ``locator-cycle-binding-campaign-mismatch``, so finding it "elsewhere"
    would never be a valid answer anyway)."""

    root = Path(root).resolve()
    probed = False

    def probe() -> Optional[Path]:
        nonlocal probed
        probed = True
        return _unindexed_campaign_probe(root, owner_campaign_id)

    campaign_path = _campaign_dir_via_hint_or_candidates(root, owner_campaign_id, owner_campaign_candidates)
    if campaign_path is None:
        campaign_path = probe()
    if campaign_path is None:
        return _UNRESOLVED_OWNER

    part = scan_campaign(root, campaign_path)
    if part is not None and cycle_id in part:
        rel = part[cycle_id]["path"]
        hints = _hint_map(root)
        if hints.get(cycle_id) != rel:
            _heal_via_lock(root, lambda: update_indexes(root, [owner_campaign_id]))
        return root / rel

    if not probed:
        duplicate_path = probe()
        if duplicate_path is not None and duplicate_path != campaign_path:
            raise LocatorError("locator-index-duplicate-id", owner_campaign_id)
    return None


def _scan_lenient(root: Path) -> Tuple[Dict[str, str], Rows, Set[str], Optional[LocatorError]]:
    """Gather rows from every campaign, never stopping at the first defect or
    duplicate. Returns the clean rows/mapping, the set of ids found in more
    than one place, and the first campaign-level defect encountered (if any) --
    the caller decides what to raise, since only it knows whether the id it
    actually wants was affected."""

    root = Path(root).resolve()
    rows: Rows = {}
    duplicates: Set[str] = set()
    defect: Optional[LocatorError] = None
    for campaign_path in iter_campaign_dirs(root):
        try:
            part = scan_campaign(root, campaign_path)
        except LocatorError as exc:
            if defect is None:
                defect = exc
            continue
        if part is None:
            continue
        for identifier, row in part.items():
            if identifier in duplicates:
                continue
            previous = rows.get(identifier)
            if previous is not None and previous["path"] != row["path"]:
                duplicates.add(identifier)
                del rows[identifier]
                continue
            rows[identifier] = row
    mapping = {identifier: row["path"] for identifier, row in rows.items()}
    return mapping, rows, duplicates, defect


def _heal_root(root: Path, mapping: Dict[str, str], rows: Rows) -> None:
    # `mapping`/`rows` come from a pre-lock scan; they are only a cheap filter
    # to skip taking the lock when nothing looks stale. The write itself must
    # recompute from a fresh scan taken *after* the lock is held, or a seal
    # that completes during this function's pre-lock scan gets its fresh
    # write clobbered by these stale bytes (lost update).
    json_bytes, md_bytes = render_indexes(mapping, rows)
    current_json = _regular_bytes(campaigns_dir(root) / INDEX_JSON)
    current_md = _regular_bytes(campaigns_dir(root) / INDEX_MD)
    if current_json == json_bytes and current_md == md_bytes:
        return

    def write() -> None:
        fresh_mapping, fresh_rows, duplicates, defect = _scan_lenient(root)
        if duplicates or defect is not None:
            return
        fresh_json, fresh_md = render_indexes(fresh_mapping, fresh_rows)
        _write_index_pair(root, fresh_json, fresh_md)

    _heal_via_lock(root, write)


def locate(
    root: Path, identifier: str, *,
    candidates: Optional[Callable[[], Iterable[Path]]] = None,
    owner_campaign_id: Optional[str] = None,
    owner_campaign_candidates: Optional[Callable[[], Iterable[Path]]] = None,
) -> Optional[Path]:
    """The one verified ID -> path lookup behind `find_path_by_id`,
    `resolve_path`, `campaign_dir`, `cycle_dir`, `read_campaign` and their
    peers. An INDEX hit is never trusted on its own -- `_verify_candidate`
    proves it against the record every time -- so a stale or missing index
    can only ever cost a fallback scan, never a wrong answer (D-89).

    `owner_campaign_id` only matters when `identifier` is a cycle id: it lets
    a cache miss scan just that one campaign instead of the whole root.
    """

    root = Path(root).resolve()
    hints = _hint_map(root)
    hinted = hints.get(identifier)
    if hinted:
        candidate_path = root / hinted
        if _verify_candidate(root, candidate_path, identifier):
            return candidate_path
    if candidates is not None:
        for candidate_path in candidates():
            candidate_path = Path(candidate_path)
            if _verify_candidate(root, candidate_path, identifier):
                return candidate_path

    is_cycle = _CYCLE_ID.fullmatch(str(identifier)) is not None
    if is_cycle and owner_campaign_id is not None:
        result = _locate_within_owner_campaign(root, identifier, owner_campaign_id, owner_campaign_candidates)
        if result is not _UNRESOLVED_OWNER:
            return result

    mapping, rows, duplicates, defect = _scan_lenient(root)
    if not duplicates and defect is None:
        _heal_root(root, mapping, rows)
    if identifier in duplicates:
        raise LocatorError("locator-index-duplicate-id", identifier)
    rel = mapping.get(identifier)
    if rel is not None:
        return root / rel
    if defect is not None:
        raise defect
    return None


def find_path_by_id(root: Path, identifier: str) -> Optional[Path]:
    return locate(root, identifier)


def resolve_path(root: Path, identifier: str) -> Optional[Path]:
    return locate(root, identifier)
