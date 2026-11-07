#!/usr/bin/env python3
"""Derived read entrance for one artifact root.

Writes `README.md`, `index.html`, `all-campaigns.md` and `all-campaigns.html`
directly under the artifact root so a person can see what came out most
recently and open it, without any external tool.  The same computation feeds
`hearting reports` (the numbered list in the terminal) and `hearting open`
(opening one document or the browser entrance).

What this module never does: it never creates, moves, renames, or deletes a
campaign, a cycle, a manifest, or any document, and it never creates a symbolic
link. Refresh writes the four entrance files above plus, once ever,
`<root>/.runtime/artifact-entrance.json` (the first user-facing cycle record).
Seal requests also use two locks and one coalesced pending record under
`<root>/.runtime/`; the finite refresh worker consumes that record and exits.

Determinism: identical root content and the same declared language produce
byte-identical files.  No wall clock, host name, user name, or absolute path is
rendered; every link is a relative POSIX path from the root.

Reading cost: directory enumeration reuses `artifact_locator.iter_campaign_dirs`
and `iter_cycle_dirs`; manifests are read newest first, only until the recent
list is full or `MAX_MANIFESTS` is reached (a full scan is never done).

Row sources: every recent row carries an internal `kind` (not rendered); rows
built from cycle folders are `"cycle"`.  `ROW_SOURCES` is the one place where
rows that are not cycle folders can join the recent list.  It is an ordered
tuple of callables including the common daily-journal reader; each takes the artifact root and returns
rows (`kind`, `date`, `title`, `doc_path`, optional `summary`/`extra`/
`cycle_id`/`cycle_path`).  `scan` merges the valid rows before the sort and the
limit; an invalid row or a source that raises is skipped.  Daily-journal identity, revision, coverage, anchors and result targets are retained; their format is owned by artifact_journal, not this renderer.
"""
from __future__ import annotations

import fcntl
import json
import hashlib
import os
import re
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_locator  # noqa: E402
import artifact_meta  # noqa: E402
import artifact_producer_store as store  # noqa: E402
from artifact_producer_manifest import artifact_audience, resolve_placed_output  # noqa: E402
from detached_lifetime import bind as end_with_test  # noqa: E402
from artifact_checkpoint_trigger import in_test_process  # noqa: E402
from artifact_nofollow import open_relative, open_root  # noqa: E402

try:  # tools/i18n is a sibling of utilities/ in every checkout and release
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "i18n"))
    import ui_text  # noqa: E402
except Exception:  # pragma: no cover - a missing catalog never blocks the entrance
    ui_text = None


RECENT_LIMIT = 20
CAMPAIGN_LIMIT = 10
MAX_MANIFESTS = 300
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEAD_BYTES = 16 * 1024
SUMMARY_CHARS = 120
MARKER = "<!-- hearting:artifact-entrance v1 -->"

# Hook for rows that are not cycle folders (see the module header).
def _daily_journals(root):
    from artifact_journal import entrance_snapshot
    return entrance_snapshot(root)


ROW_SOURCES: Tuple[Callable[[Path], Iterable[Mapping]], ...] = (_daily_journals,)
_DATE_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ENTRANCE_NAMES = store.ENTRANCE_NAMES
FIRST_RECORD_REL = ".runtime/artifact-entrance.json"

_STATUS_NAMES = tuple(unicodedata.normalize("NFC", name).casefold()
                      for name in store.STATUS_DOCUMENT_NAMES)
_GENERIC_TITLES = frozenset({"final report", "report", "summary", "최종 보고서", "보고서"})
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
_URL_ONLY = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_PARA = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _translator():
    return ui_text.translator() if ui_text is not None else _fallback_translator


def _fallback_translator(message: str, /, **fields: Any) -> str:
    """English-only stand-in when the shared catalog is unavailable."""
    templates = {
        "entrance.title": "Artifacts",
        "entrance.totals": "Campaigns: {campaigns} · cycles: {cycles} · running: {open}",
        "entrance.recent_documents": "Recent documents",
        "entrance.recent_campaigns": "Recent campaigns",
        "entrance.more": "+{count} more",
        "entrance.records": "work records {count}",
        "entrance.cycles": "cycles {count}",
        "entrance.latest": "latest {date}",
        "entrance.all_campaigns": "All campaigns",
        "entrance.id_index": "ID index",
        "entrance.back": "Back to the entrance",
        "entrance.rewrite": ("Hearting rewrites this file. Editing it is overwritten; "
                             "deleting it comes back at the next publish."),
        "entrance.empty": "No user-facing document yet.",
        "open.viewer": "Viewer: {url}",
        "reports.empty": "No user-facing document yet.",
        "reports.foreign": "Kept an existing {name}: it carries no Hearting entrance marker.",
        "reports.entrance": "Entrance: {path}",
        "open.not_found": "No artifact matches {query}.",
        "open.ambiguous": "Several artifacts match; open one by number.",
        "install.entrance": ("Results land in each project's `.agent_reports/`; "
                             "open its `README.md` or run `hearting reports`."),
        "completion.entrance": "Artifact list: {path}",
    }
    template = templates.get(message, message)
    return template.format(**fields) if fields else template


_fallback_translator.lang = "en"


# ---------------------------------------------------------------------------
# reading one document's head
# ---------------------------------------------------------------------------


def _read_head(root: Path, path: Path) -> str:
    """The first HEAD_BYTES of a regular document, read from a no-follow fd.

    Without the platform primitives, or when the document cannot be opened
    that way, the head is empty (callers fall back to a generic title).
    """
    try:
        parts = list(path.relative_to(root).parts)
        root_fd = open_root(os.path.realpath(str(root)))
    except (OSError, RuntimeError, ValueError):
        return ""
    try:
        fd = open_relative(root_fd, parts)
    except OSError:
        return ""
    finally:
        os.close(root_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        raw = os.read(fd, HEAD_BYTES)
    except OSError:
        return ""
    finally:
        os.close(fd)
    return raw.decode("utf-8", "replace")


def _markdown_title(text: str) -> Optional[str]:
    lines = text.splitlines()
    index = 0
    if lines and lines[0].strip() == "---":
        for at in range(1, len(lines)):
            if lines[at].strip() == "---":
                index = at + 1
                break
    for line in lines[index:]:
        stripped = line.strip()
        if stripped.startswith("# "):
            return _nfc(stripped[2:].strip()) or None
    return None


def _html_title(text: str) -> Optional[str]:
    for pattern in (_TITLE_TAG, _H1):
        match = pattern.search(text)
        if match:
            plain = _TAG.sub("", match.group(1))
            value = _nfc(_unescape(plain)).strip()
            if value:
                return value
    return None


def _unescape(value: str) -> str:
    import html

    return html.unescape(value)


def _document_title(path: Path, text: str, *, fallback: str) -> str:
    lowered = path.name.casefold()
    if lowered.endswith((".html", ".htm")):
        found = _html_title(text)
    else:
        found = _markdown_title(text)
    if found and found.strip().casefold() not in _GENERIC_TITLES:
        return found
    return fallback


def _summary_source_markdown(text: str) -> Optional[str]:
    lines = text.splitlines()
    index = 0
    if lines and lines[0].strip() == "---":
        for at in range(1, len(lines)):
            if lines[at].strip() == "---":
                index = at + 1
                break
    body = lines[index:]
    for ordinal, line in enumerate(body):
        stripped = line.strip()
        for label in ("**한 줄**:", "**One line**:", "**한 줄** :", "**One line** :"):
            if stripped.startswith(label):
                rest = stripped[len(label):].strip()
                if rest:
                    return rest
        if _skip_summary_line(stripped):
            continue
        collected = [stripped]
        for follower in body[ordinal + 1:]:
            nxt = follower.strip()
            if not nxt or _skip_summary_line(nxt):
                break
            collected.append(nxt)
        return " ".join(collected)
    return None


def _skip_summary_line(stripped: str) -> bool:
    if not stripped:
        return True
    if stripped.startswith(("#", "|", ">", "```", "~~~", "<!--", "---", "***", "===")):
        return True
    if re.match(r"^[-*+] \*\*[^*]+\*\*\s*:", stripped):
        return True
    if re.match(r"^PR[:#]", stripped, re.IGNORECASE):
        return True
    if _URL_ONLY.match(stripped):
        return True
    return False


def _summary_source_html(text: str) -> Optional[str]:
    match = _PARA.search(text)
    if not match:
        return None
    plain = _nfc(_unescape(_TAG.sub(" ", match.group(1))))
    return " ".join(plain.split()) or None


def _summarize(text: str, *, html_document: bool) -> str:
    source = _summary_source_html(text) if html_document else _summary_source_markdown(text)
    if not source:
        return ""
    return _clip(source)


def _clip(text: str) -> str:
    """Whitespace folded and cut at SUMMARY_CHARS code points, as every summary is."""
    collapsed = " ".join(_nfc(text).split())
    if len(collapsed) <= SUMMARY_CHARS:
        return collapsed
    return collapsed[:SUMMARY_CHARS].rstrip() + "…"


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------


def _date_key(locator: str, fallback_locator: str = "") -> str:
    for candidate in (locator, fallback_locator):
        match = _DATE_PREFIX.match(candidate or "")
        if match:
            return match.group(1)
    return ""


def _display_date(value: Optional[str], fallback: str) -> str:
    text = _nfc(str(value or "")).strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else fallback


def _campaign_identity(entry: Path) -> Tuple[Optional[str], Dict[str, Any]]:
    record = store._read_json(entry / "campaign.json")
    record = record if isinstance(record, dict) else {}
    campaign_id = record.get("campaign_id")
    if not isinstance(campaign_id, str):
        try:
            recovered = artifact_locator._campaign_from_manifests(entry)
        except artifact_locator.LocatorError:
            recovered = None
        campaign_id = (recovered or {}).get("campaign_id")
    return campaign_id if isinstance(campaign_id, str) else None, record


def _campaign_title(root: Path, campaign_id: Optional[str], locator: str, campaign_path: Path,
                    membership: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """`artifact_meta.effective_title` without its per-call campaign-location lookup.

    The campaign directory is already known from the enumeration, so the only
    saving is that lookup; the source order (meta.json title, old display
    declaration, folder name) is the same function's order.  `membership` is
    shared across one scan because `read_campaign_meta` would otherwise rescan
    every cycle record."""
    if campaign_id:
        try:
            if (campaign_path / artifact_meta.META_NAME).is_file():
                read = artifact_meta.read_campaign_meta(root, campaign_id,
                                                       members=membership if membership is not None else {})
                title = ((read.doc or {}).get("campaign") or {}).get("title") if read.status == "ok" else None
                if isinstance(title, str) and title:
                    return _nfc(title)
            legacy, _protected = artifact_meta.legacy_title(root, campaign_id)
            if legacy:
                return _nfc(legacy)
        except Exception:  # noqa: BLE001 - a title never blocks the entrance
            pass
    return _nfc(locator)


def _document_rows(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Join the manifest's artifact rows with their revisions by artifact_id."""
    revisions: Dict[str, str] = {}
    for row in manifest.get("artifact_revisions") or []:
        if not isinstance(row, dict):
            continue
        locator = row.get("locator") if isinstance(row.get("locator"), dict) else {}
        path = locator.get("path")
        artifact_id = row.get("artifact_id")
        if isinstance(path, str) and isinstance(artifact_id, str):
            revisions.setdefault(artifact_id, path)
    rows: List[Dict[str, Any]] = []
    for row in manifest.get("artifacts") or []:
        if not isinstance(row, dict):
            continue
        artifact_id = row.get("artifact_id")
        path = revisions.get(artifact_id) if isinstance(artifact_id, str) else None
        if not path:
            continue
        has_audience = "audience" in row
        audience = row.get("audience") if has_audience else artifact_audience(path)
        if row.get("role") == "support":
            audience = None
        if audience != "user":
            audience = None
        rows.append({
            "path": _nfc(path),
            "audience": audience,
            "has_audience": has_audience,
            "role": row.get("role"),
        })
    return rows


def _rank(path: str) -> Tuple[int, str]:
    name = unicodedata.normalize("NFC", path.rsplit("/", 1)[-1]).casefold()
    order = {status: at for at, status in enumerate(_STATUS_NAMES)}
    if name in order:
        return (order[name], path)
    for at, pattern in enumerate(("final_report.", "report.", "summary.", "audit-report.", "prd.")):
        if name.startswith(pattern) and name.count(".") == 1:
            return (len(order) + at, path)
    return (len(order) + 8, path)


def _representative(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [row for row in rows if row.get("audience") == "user"]
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: _rank(row["path"]))[0]


def _cycle_locator(cycle_path: Path, root: Path) -> str:
    return cycle_path.relative_to(root).as_posix()


def scan(root: Path, *, recent_limit: int = RECENT_LIMIT,
         campaign_limit: int = CAMPAIGN_LIMIT) -> Dict[str, Any]:
    """Read-only snapshot of the root feeding every rendered file and the CLI."""
    root = Path(root)
    snapshot: Dict[str, Any] = {
        "root": str(root),
        "recent": [],
        "campaigns": [],
        "all_campaigns": [],
        "totals": {"campaigns": 0, "cycles": 0, "open": 0},
        "selected_by": {"audience": 0, "name-rule": 0},
    }
    if not root.is_dir():
        return snapshot

    campaign_rows: List[Dict[str, Any]] = []
    entries: List[Tuple[str, str, str, Path, Path]] = []
    totals = {"campaigns": 0, "cycles": 0, "open": 0}
    has_meta = False
    identities: List[Tuple[str, Optional[str], Path]] = []
    for campaign_path in artifact_locator.iter_campaign_dirs(root):
        campaign_locator = _nfc(campaign_path.name)
        campaign_id, _record = _campaign_identity(campaign_path)
        identities.append((campaign_locator, campaign_id, campaign_path))
        has_meta = has_meta or (campaign_path / artifact_meta.META_NAME).is_file()
        cycles = list(artifact_locator.iter_cycle_dirs(campaign_path))
        dates = []
        for cycle_path, _layout in cycles:
            cycle_locator = _nfc(cycle_path.parents[2].name if _layout == "request-execution" else cycle_path.name)
            key = _date_key(cycle_locator, campaign_locator)
            dates.append(key)
            if not (cycle_path / "manifest.json").is_file():
                totals["open"] += 1
            entries.append((key, campaign_locator, cycle_locator, cycle_path, campaign_path))
        totals["campaigns"] += 1
        totals["cycles"] += len(cycles)
        campaign_rows.append({
            "title": "",
            "path": _nfc(campaign_path.relative_to(root).as_posix()),
            "cycles": len(cycles),
            "date": max(dates) if dates else "",
            "locator": campaign_locator,
            "doc_cycles": 0,
            "docs": [],
        })
    membership = artifact_meta._membership(root) if has_meta else None
    for row, (campaign_locator, campaign_id, campaign_path) in zip(campaign_rows, identities):
        row["title"] = _campaign_title(root, campaign_id, campaign_locator, campaign_path, membership)
    snapshot["totals"] = totals
    snapshot["all_campaigns"] = sorted(campaign_rows, key=lambda row: (_desc(row["date"]), row["locator"]))
    by_locator = {row["locator"]: row for row in campaign_rows}

    entries.sort(key=lambda item: (_desc(item[0]), item[1], item[2]))
    recent: List[Dict[str, Any]] = []
    manifests_read = 0
    active_date = ""
    for date_key, campaign_locator, cycle_locator, cycle_path, campaign_path in entries:
        passed_date = bool(active_date) and date_key != active_date
        if recent and len(recent) >= recent_limit and passed_date:
            break
        if manifests_read >= MAX_MANIFESTS:
            break
        if date_key:
            active_date = date_key
        group = by_locator.get(campaign_locator)
        manifest_path = cycle_path / "manifest.json"
        try:
            size = manifest_path.stat().st_size
        except OSError:
            continue
        if size > MAX_MANIFEST_BYTES:
            continue
        manifest = store._read_json(manifest_path)
        if not isinstance(manifest, dict):
            continue
        manifests_read += 1
        cycle = manifest.get("cycle") if isinstance(manifest.get("cycle"), dict) else {}
        cycle_id = cycle.get("cycle_id")
        if not isinstance(cycle_id, str):
            try:
                binding = artifact_locator.read_cycle_binding(cycle_path)
            except artifact_locator.LocatorError:
                binding = None
            cycle_id = (binding or {}).get("cycle_id")
        cycle_id = cycle_id if isinstance(cycle_id, str) else ""
        record = artifact_locator.read_cycle_record(root, cycle_id) if cycle_id else None
        rows = _document_rows(manifest)
        chosen = _representative(rows)
        if chosen is None:
            continue
        document = _document_path(root, _cycle_locator(cycle_path, root) + "/" + chosen["path"],
                                  placed=True)
        if document is None:
            continue
        document_rel = _nfc(document.relative_to(root).as_posix())
        date = _display_date((record or {}).get("sealed_on") or cycle.get("started_on"), date_key)
        locator = _cycle_locator(cycle_path, root)
        title_fallback = ""
        if record:
            title_fallback = str(record.get("title") or record.get("slug") or "").strip()
        head = _read_head(root, document)
        title = _document_title(document, head, fallback=_nfc(title_fallback or cycle_locator))
        extras = max(0, sum(1 for row in rows if row.get("audience") == "user") - 1)
        selected_by = "audience" if any(
            row["has_audience"] for row in rows if row.get("audience") == "user") else "name-rule"
        snapshot["selected_by"][selected_by] += 1
        recent.append({
            "kind": "cycle",
            "cycle_id": cycle_id,
            "cycle_path": locator,
            "date": date,
            "title": title,
            "summary": _summarize(head,
                                  html_document=document.name.casefold().endswith((".html", ".htm"))),
            "doc_path": document_rel,
            "extra": extras,
            "selected_by": selected_by,
        })
        if group is not None:
            group["doc_cycles"] += 1
            group["docs"].append({"title": title, "path": document_rel})

    recent.extend(_source_rows(root, statuses=snapshot.setdefault("source_status", [])))
    recent.sort(key=lambda row: (_desc(row.get("at") or row["date"]), row["cycle_id"] or row["doc_path"], row["doc_path"]))
    snapshot["recent"] = recent[:recent_limit]
    groups = []
    for row in snapshot["all_campaigns"]:
        live = dict(row)
        live["records"] = max(0, live["cycles"] - live["doc_cycles"])
        live["docs"] = list(live["docs"])[:3]
        groups.append(live)
    snapshot["campaigns"] = groups[:campaign_limit]
    return snapshot


def _document_path(root: Path, doc: str, *, placed: bool = False) -> Optional[Path]:
    """A regular document under root, without traversal or any symlink component."""
    if not doc or doc.startswith("/") or "\\" in doc or "\x00" in doc:
        return None
    parts = doc.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    path = root
    try:
        for part in parts:
            path = path / part
            if path.is_symlink():
                return None
        if placed:
            path = resolve_placed_output(path)
            return _document_path(root, path.relative_to(root).as_posix())
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def _source_row(root: Path, raw: Any) -> Optional[Dict[str, Any]]:
    """One validated non-cycle row, or None when any required part is missing or unsafe."""
    if not isinstance(raw, Mapping):
        return None
    kind, date, title, doc = (raw.get("kind"), raw.get("date"), raw.get("title"), raw.get("doc_path"))
    if not all(isinstance(value, str) for value in (kind, date, title, doc)):
        return None
    kind, date, title, doc = _nfc(kind).strip(), _nfc(date), _nfc(title).strip(), _nfc(doc)
    if not kind or kind == "cycle" or not title or not _DATE_FULL.match(date):
        return None
    if _document_path(root, doc) is None:
        return None
    summary = raw.get("summary", "")
    extra = raw.get("extra", 0)
    ids = []
    for key in ("cycle_id", "cycle_path"):
        value = raw.get(key, "")
        ids.append(_nfc(value) if isinstance(value, str) else "")
    row = {
        "kind": kind,
        "cycle_id": ids[0],
        "cycle_path": ids[1],
        "date": date,
        "title": " ".join(title.split()),
        "summary": _clip(summary) if isinstance(summary, str) else "",
        "doc_path": doc,
        "extra": extra if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0 else 0,
        "selected_by": "source",
    }
    if kind == "journal-entry":
        from artifact_journal import is_well_formed
        if not (is_well_formed(raw.get("artifact_id", ""), "artifact")
                and is_well_formed(raw.get("revision_id", ""), "artifact_revision")):
            return None
        anchor = raw["artifact_id"] + "-" + raw["revision_id"]
        if raw.get("anchor") != anchor or raw.get("record_target") != doc + "#" + anchor:
            return None
        row.update({key: raw.get(key) for key in ("artifact_id", "revision_id", "anchor", "record_target",
            "campaign_id", "route_id", "session", "turn", "at", "status", "coverage", "checks", "digest",
            "results_total", "results_coverage")})
        row["result_targets"] = list(raw.get("result_targets") or [])
    return row


def _source_rows(root: Path, *, statuses=None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for source in ROW_SOURCES:
        try:
            result = source(root)
            if isinstance(result, Mapping) and "rows" in result:
                if statuses is not None:
                    statuses.append({"kind": result.get("kind"), "coverage": result.get("coverage", "unknown"),
                                     "errors": result.get("errors") or []})
                candidates = result["rows"]
            else:
                candidates = list(result)
        except Exception:
            if statuses is not None:
                statuses.append({"kind": "source", "coverage": "unknown", "errors": ["source-unreadable"]})
            continue  # the entrance never fails because of a source
        for raw in candidates:
            try:
                row = _source_row(root, raw)
            except Exception:
                row = None
            if row is not None:
                rows.append(row)
    return rows


def _desc(value: str) -> "_Desc":
    """Descending order for one string inside an otherwise ascending sort key."""
    return _Desc(value)


class _Desc:
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value

    def __lt__(self, other: "_Desc") -> bool:
        return self.value > other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Desc) and self.value == other.value


def recent_rows(root: Path, limit: int = RECENT_LIMIT) -> List[Dict[str, Any]]:
    """The numbered list `hearting reports` and `hearting open` share with the entrance."""
    return scan(root, recent_limit=limit)["recent"]


def campaign_groups(root: Path, limit: int = CAMPAIGN_LIMIT) -> List[Dict[str, Any]]:
    """Recent campaign groups (title, cycle count, latest date, already-found documents)."""
    return scan(root, campaign_limit=limit)["campaigns"]


def all_campaigns(root: Path) -> List[Dict[str, Any]]:
    """Every campaign by (latest date desc, locator asc): directory listing and meta titles only."""
    return scan(root)["all_campaigns"]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _md_text(value: str) -> str:
    return re.sub(r"([\\`*_\[\]])", r"\\\1", _nfc(value))


def _md_href(path: str) -> str:
    return _nfc(path).replace("<", "%3C").replace(">", "%3E")


def _href(path: str) -> str:
    return quote(_nfc(path), safe="/.")


def _record_target(row):
    return row.get("record_target") or row["doc_path"]


def _record_href(row):
    path = _href(row["doc_path"])
    return path + "#" + quote(row["anchor"], safe="") if row.get("anchor") else path


def _result_links(row):
    # Availability was judged by the same journal reader, against this
    # entry's execution folder. No renderer performs its own path inference.
    return [item for item in (row.get("result_targets") or [])[:3]
            if isinstance(item, Mapping) and item.get("availability") == "available"
            and isinstance(item.get("href"), str) and item["href"].startswith("file://")]


def _partial_sources(snapshot):
    return any(row.get("coverage") != "complete" for row in snapshot.get("source_status") or [])


def _markdown_recent(snapshot: Dict[str, Any], t) -> List[str]:
    lines: List[str] = []
    for number, row in enumerate(snapshot["recent"], start=1):
        suffix = f"  ({t('entrance.more', count=_count(row['extra']))})" if row["extra"] else ""
        where = f"  — `{_md_href(row['cycle_path'])}`" if row["cycle_path"] else ""
        lines.append(f"{number}. {row['date']}  [{_md_text(row['title'])}](<{_md_href(_record_target(row))}>)"
                     f"{where}{suffix}")
        if row["kind"] == "journal-entry":
            links = [f"[{t('entrance.open_record')}](<{_md_href(_record_target(row))}>)"]
            links += [f"[{t('entrance.open_result')}](<{item['href']}>)" for item in _result_links(row)]
            lines.append("   " + " · ".join(links) + " · " + _md_text(str(row.get("status") or "unknown")))
        if row["summary"]:
            lines.append(f"   {_md_text(row['summary'])}")
    if not lines:
        lines.append(t("entrance.empty"))
    if _partial_sources(snapshot):
        lines.append(t("entrance.partial"))
    return lines


def _markdown_campaigns(snapshot: Dict[str, Any], t) -> List[str]:
    lines: List[str] = []
    for row in snapshot["campaigns"]:
        parts = [t("entrance.cycles", count=_count(row["cycles"]))]
        if row["date"]:
            parts.append(t("entrance.latest", date=row["date"]))
        parts.append(t("entrance.records", count=_count(row["records"])))
        lines.append(f"- [{_md_text(row['title'])}](<{_md_href(row['path'])}>) — "
                     + " · ".join(parts))
        for document in row["docs"]:
            lines.append(f"  - [{_md_text(document['title'])}](<{_md_href(document['path'])}>)")
    if not lines:
        lines.append(t("entrance.empty"))
    return lines


def render_markdown(snapshot: Dict[str, Any], t) -> str:
    totals = snapshot["totals"]
    lines = [
        MARKER,
        f"# {t('entrance.title')}",
        "",
        t("entrance.totals", campaigns=_count(totals["campaigns"]),
          cycles=_count(totals["cycles"]), open=_count(totals["open"])),
        "",
        f"## {t('entrance.recent_documents')}",
        "",
    ]
    lines.extend(_markdown_recent(snapshot, t))
    lines.extend([
        "",
        f"## {t('entrance.recent_campaigns')}",
        "",
    ])
    lines.extend(_markdown_campaigns(snapshot, t))
    lines.extend([
        "",
        f"[{t('entrance.all_campaigns')}](<all-campaigns.md>) · "
        f"[{t('entrance.id_index')}](<campaigns/INDEX.md>)",
        "",
        t("entrance.rewrite"),
        "",
    ])
    return "\n".join(lines)


def render_all_markdown(snapshot: Dict[str, Any], t) -> str:
    lines = [
        MARKER,
        f"# {t('entrance.all_campaigns')}",
        "",
        f"[{t('entrance.back')}](<README.md>)",
        "",
    ]
    for row in snapshot["all_campaigns"]:
        parts = [t("entrance.cycles", count=_count(row["cycles"]))]
        if row["date"]:
            parts.append(t("entrance.latest", date=row["date"]))
        lines.append(f"- [{_md_text(row['title'])}](<{_md_href(row['path'])}>) — " + " · ".join(parts))
    if not snapshot["all_campaigns"]:
        lines.append(t("entrance.empty"))
    lines.extend(["", t("entrance.rewrite"), ""])
    return "\n".join(lines)


_CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:52rem;margin:2rem auto;
padding:0 1rem;line-height:1.55;color:#1b1b1b;background:#fff}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem}
ol,ul{padding-left:1.4rem}li{margin:.35rem 0}
.meta{color:#666;font-size:.85rem}a{color:#0b5cad;text-decoration:none}a:hover{text-decoration:underline}
.note{margin-top:2.5rem;color:#666;font-size:.85rem}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}.meta,.note{color:#aaa}
a{color:#7fb2e5}}
""".strip()


def _html_page(title: str, lang: str, body: List[str]) -> str:
    return "\n".join([
        "<!DOCTYPE html>",
        MARKER,
        f'<html lang="{lang}">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_escape(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        *body,
        "</body>",
        "</html>",
        "",
    ])


def _escape(value: str) -> str:
    import html

    return html.escape(_nfc(value), quote=True)


def _count(value: int) -> str:
    return f"{value:,}"


def render_html(snapshot: Dict[str, Any], t) -> str:
    totals = snapshot["totals"]
    body = [f"<h1>{_escape(t('entrance.title'))}</h1>",
            f'<p class="meta">{_escape(t("entrance.totals", campaigns=_count(totals["campaigns"]), cycles=_count(totals["cycles"]), open=_count(totals["open"])))}</p>',
            f"<h2>{_escape(t('entrance.recent_documents'))}</h2>",
            "<ol>"]
    for row in snapshot["recent"]:
        suffix = f" &#183; {_escape(t('entrance.more', count=_count(row['extra'])))}" if row["extra"] else ""
        summary = f"<br><span class=\"meta\">{_escape(row['summary'])}</span>" if row["summary"] else ""
        where = f" &#183; {_escape(row['cycle_path'])}" if row["cycle_path"] else ""
        body.append(f'<li><span class="meta">{_escape(row["date"])}</span> '
                    f'<a href="{_record_href(row)}">{_escape(row["title"])}</a>'
                    f'<span class="meta">{where}{suffix}</span>{summary}')
        if row["kind"] == "journal-entry":
            links = [f'<a href="{_record_href(row)}">{_escape(t("entrance.open_record"))}</a>']
            # Result hrefs are already encoded file URIs from the common reader.
            links += [f'<a href="{_escape(item["href"])}">{_escape(t("entrance.open_result"))}</a>' for item in _result_links(row)]
            body.append('<br>' + ' &#183; '.join(links) + ' &#183; ' + _escape(row.get("status") or "unknown"))
        body[-1] += '</li>'
    if not snapshot["recent"]:
        body.append(f"<li>{_escape(t('entrance.empty'))}</li>")
    if _partial_sources(snapshot):
        body.append(f"<li>{_escape(t('entrance.partial'))}</li>")
    body.extend(["</ol>", f"<h2>{_escape(t('entrance.recent_campaigns'))}</h2>", "<ul>"])
    for row in snapshot["campaigns"]:
        parts = [t("entrance.cycles", count=_count(row["cycles"]))]
        if row["date"]:
            parts.append(t("entrance.latest", date=row["date"]))
        parts.append(t("entrance.records", count=_count(row["records"])))
        body.append(f'<li><a href="{_href(row["path"])}">{_escape(row["title"])}</a> '
                    f'<span class="meta">&#183; {_escape(" · ".join(parts))}</span>')
        if row["docs"]:
            body.append("<ul>")
            for document in row["docs"]:
                body.append(f'<li><a href="{_href(document["path"])}">{_escape(document["title"])}</a></li>')
            body.append("</ul>")
        body.append("</li>")
    if not snapshot["campaigns"]:
        body.append(f"<li>{_escape(t('entrance.empty'))}</li>")
    body.extend([
        "</ul>",
        f'<p><a href="all-campaigns.html">{_escape(t("entrance.all_campaigns"))}</a> '
        f'&#183; <a href="campaigns/INDEX.md">{_escape(t("entrance.id_index"))}</a></p>',
        f'<p class="note">{_escape(t("entrance.rewrite"))}</p>',
    ])
    return _html_page(t("entrance.title"), getattr(t, "lang", "en"), body)


def render_all_html(snapshot: Dict[str, Any], t) -> str:
    body = [f"<h1>{_escape(t('entrance.all_campaigns'))}</h1>",
            f'<p><a href="index.html">{_escape(t("entrance.back"))}</a></p>', "<ul>"]
    for row in snapshot["all_campaigns"]:
        parts = [t("entrance.cycles", count=_count(row["cycles"]))]
        if row["date"]:
            parts.append(t("entrance.latest", date=row["date"]))
        body.append(f'<li><a href="{_href(row["path"])}">{_escape(row["title"])}</a> '
                    f'<span class="meta">&#183; {_escape(" · ".join(parts))}</span></li>')
    if not snapshot["all_campaigns"]:
        body.append(f"<li>{_escape(t('entrance.empty'))}</li>")
    body.extend(["</ul>", f'<p class="note">{_escape(t("entrance.rewrite"))}</p>'])
    return _html_page(t("entrance.all_campaigns"), getattr(t, "lang", "en"), body)


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def request_refresh(root: Path, cycle_id: Optional[str] = None) -> None:
    """Coalesce seal requests into one finite worker, following human_wait's fd handoff.

    The short request lock covers only pending state and worker ownership, never
    scanning or rendering. The worker relinquishes ownership under that same
    lock, so a request arriving as it exits cannot be stranded.
    """
    root = Path(root).absolute()
    if not root.is_dir():
        return
    if in_test_process():
        # A test process never spawns a detached child (the checkpoint trigger's
        # rule); refreshing in place keeps its fixtures free of stray writers.
        refresh(root, cycle_id=cycle_id)
        return
    runtime = root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    with open(runtime / "artifact-entrance-request.lock", "a+b") as request:
        fcntl.flock(request, fcntl.LOCK_EX)
        store._write_atomic(runtime / "artifact-entrance-pending.json",
                            store._json_bytes({"cycle_id": cycle_id or ""}))
        fd = os.open(runtime / "artifact-entrance-refresh.lock",
                     os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            code = ("import sys; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv[1]); import artifact_entrance as e; "
                    "e._refresh_requested(Path(sys.argv[2]), int(sys.argv[3]))")
            end_with_test(subprocess.Popen(
                [sys.executable, "-c", code, str(Path(__file__).resolve().parent), str(root), str(fd)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, pass_fds=(fd,), start_new_session=True))
        finally:
            # Do not LOCK_UN: the child inherits this same locked description.
            os.close(fd)


def _refresh_requested(root: Path, fd: int) -> None:
    """Consume pending requests without sleeping or holding the request lock over a refresh."""
    runtime = root / ".runtime"
    pending = runtime / "artifact-entrance-pending.json"
    try:
        while root.is_dir():
            with open(runtime / "artifact-entrance-request.lock", "a+b") as request:
                fcntl.flock(request, fcntl.LOCK_EX)
                if not pending.exists():
                    # Serialize exit with request_refresh's ownership check.
                    os.close(fd)
                    fd = -1
                    return
                demand = store._read_json(pending) or {}
                pending.unlink()
            try:
                refresh(root, cycle_id=demand.get("cycle_id"))
            except Exception as exc:  # best effort, including a follow-up request
                print(f"artifact_entrance skipped error={type(exc).__name__}", file=sys.stderr)
    finally:
        if fd >= 0:
            os.close(fd)


def _has_marker(data: bytes) -> bool:
    """The generator's first line; an HTML file keeps `<!DOCTYPE html>` above it."""
    lines = [line.decode("utf-8", "replace").strip() for line in data.split(b"\n", 3)[:2]]
    return bool(lines) and (lines[0] == MARKER or (len(lines) > 1 and lines[1] == MARKER))


def _first_cycle_record(root: Path, snapshot: Dict[str, Any], cycle_id: str) -> None:
    if not cycle_id:
        return
    row = next((item for item in snapshot["recent"] if item["cycle_id"] == cycle_id), None)
    if row is None:
        return
    path = root / FIRST_RECORD_REL
    if os.path.lexists(str(path)):
        return
    try:
        store._ensure_dir(path.parent)
        payload = {
            "schema": 1,
            "first_cycle_id": cycle_id,
            "first_cycle_path": row["cycle_path"],
        }
        store._write_atomic(path, store._json_bytes(payload), 0o644)
    except (OSError, store.ProducerError):
        pass  # the record is an optional notification, never part of the entrance


# Process-local hint only. Files are rechecked before reuse; no sidecar or persistent
# state is created, and process startup always computes the pages normally.
_ALL_PAGE_CACHE: Dict[str, Any] = {}


def _refresh_and_snapshot(root: Path, cycle_id: Optional[str] = None):
    """Refresh once and return the same snapshot to the calling command."""
    root = Path(root)
    result: Dict[str, Any] = {
        "status": "skipped-no-root", "paths": [], "foreign": [], "created": [],
        "selected_by": {"audience": 0, "name-rule": 0},
    }
    if not root.is_dir():
        return result, None
    snapshot = scan(root)
    t = _translator()
    payloads = {
        "README.md": render_markdown(snapshot, t),
        "index.html": render_html(snapshot, t),
    }
    # All page inputs, translations and renderer implementation determine reuse.
    # The cached file bytes are compared as well, so foreign edits and missing
    # files always go through the regular marker and symlink checks below.
    cache_key = str(root.absolute())
    try:
        renderer_revision = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        renderer_revision = None  # no reliable identity: render normally
    fingerprint = json.dumps([snapshot["all_campaigns"], getattr(t, "lang", "en"),
        [t(k, **fields) for k, fields in (
            ("entrance.all_campaigns", {}), ("entrance.back", {}),
            ("entrance.cycles", {"count": "123"}),
            ("entrance.latest", {"date": "sample"}),
            ("entrance.empty", {}), ("entrance.rewrite", {}))],
        renderer_revision],
        ensure_ascii=False, sort_keys=True)
    cached = _ALL_PAGE_CACHE.get(cache_key)
    reuse = bool(renderer_revision and cached and cached[0] == fingerprint)
    if reuse:
        try:
            for name, data in cached[1].items():
                path = root / name
                if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                    reuse = False
                    break
        except OSError:
            reuse = False
    if not reuse:
        payloads["all-campaigns.md"] = render_all_markdown(snapshot, t)
        payloads["all-campaigns.html"] = render_all_html(snapshot, t)
    written: List[str] = []
    foreign: List[str] = []
    created: List[str] = []
    for name in ENTRANCE_NAMES:
        if name not in payloads:
            continue
        path = root / name
        data = payloads[name].encode("utf-8")
        if os.path.islink(str(path)):
            foreign.append(name)  # never follow a link and never replace one
            continue
        if path.exists():
            if not path.is_file():
                foreign.append(name)
                continue
            try:
                current = path.read_bytes()
            except OSError:
                foreign.append(name)
                continue
            if not _has_marker(current):
                foreign.append(name)
                continue
            if current == data:
                continue
            store._write_atomic(path, data)
            written.append(name)
        else:
            store._write_atomic(path, data)
            written.append(name)
            created.append(name)
    if not reuse:
        cached_pages = {}
        for name in ("all-campaigns.md", "all-campaigns.html"):
            path = root / name
            data = payloads[name].encode("utf-8")
            try:
                if not path.is_symlink() and path.is_file() and path.read_bytes() == data and _has_marker(data):
                    cached_pages[name] = data
            except OSError:
                pass
        if renderer_revision and len(cached_pages) == 2:
            if len(_ALL_PAGE_CACHE) >= 16 and cache_key not in _ALL_PAGE_CACHE:
                _ALL_PAGE_CACHE.pop(next(iter(_ALL_PAGE_CACHE)))
            _ALL_PAGE_CACHE[cache_key] = (fingerprint, cached_pages)
        else:
            _ALL_PAGE_CACHE.pop(cache_key, None)
    _first_cycle_record(root, snapshot, cycle_id or "")
    if written:
        status = "written"
    elif foreign and len(foreign) == len(ENTRANCE_NAMES):
        status = "skipped-foreign"  # every target is someone else's file: nothing was ours to write
    else:
        status = "unchanged"
    result.update({
        "status": status,
        "paths": [str(root / name) for name in ENTRANCE_NAMES],
        "foreign": foreign,
        "created": created,
        "selected_by": snapshot["selected_by"],
    })
    return result, snapshot


def refresh(root: Path, cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Rewrite the entrance files when their bytes differ."""
    return _refresh_and_snapshot(root, cycle_id)[0]


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def resolve_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    script = Path(__file__).resolve().parent / "artifact-root.sh"
    if script.is_file():
        try:
            done = subprocess.run(["sh", str(script), os.getcwd()],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0 and done.stdout.strip():
            return Path(done.stdout.strip()).resolve()
    env = os.environ.get("AGENT_ARTIFACT_ROOT")
    if env:
        return Path(env).resolve()
    return Path(os.getcwd()) / ".agent_reports"


def _print_rows(rows: Sequence[Dict[str, Any]], root: Path) -> None:
    for number, row in enumerate(rows, start=1):
        suffix = f"  ({_translator()('entrance.more', count=_count(row['extra']))})" if row["extra"] else ""
        print(f"{number}. {row['date']}  {row['title']}{suffix}")
        print(f"   {_record_target(row)}")
        for item in _result_links(row):
            print(f"   {_translator()('entrance.open_result')}: {item['path']}")


def _open_externally(path: Path | str) -> None:
    if not sys.stdout.isatty():
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
            or sys.platform == "darwin"):
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def _command_reports(root: Path) -> int:
    result, snapshot = _refresh_and_snapshot(root)
    t = _translator()
    rows = snapshot["recent"] if snapshot else []
    if _partial_sources(snapshot):
        print(t("entrance.partial"))
    if not rows:
        print(t("reports.empty"))
    else:
        _print_rows(rows, root)
    print(t("reports.entrance", path=str(root / "README.md")))
    for name in result["foreign"]:
        print(t("reports.foreign", name=name))
    return 0


def _open_target(root: Path) -> Path:
    entrance = root / "index.html"
    if not entrance.is_file():
        refresh(root)
    return entrance


def _command_open(root: Path, args: Sequence[str]) -> int:
    t = _translator()
    _, snapshot = _refresh_and_snapshot(root)
    rows = snapshot["recent"] if snapshot else []
    if not args:
        target = root / "index.html"
        print(str(target))
        _show_viewer(root, target, t)
        return 0
    selector = " ".join(args)
    chosen: Optional[Dict[str, Any]] = None
    if len(args) == 1 and args[0].isdigit() and 1 <= int(args[0]) <= len(rows):
        chosen = rows[int(args[0]) - 1]
    if chosen is None:
        words = [word.casefold() for word in args if word.strip()]
        matches = [row for row in rows if all(
            word in (" ".join([row["title"], row["summary"], row["doc_path"], row["cycle_path"]]).casefold())
            for word in words)]
        if not matches:
            print(t("open.not_found", query=selector), file=sys.stderr)
            return 1
        if len(matches) > 1:
            _print_rows(matches, root)
            print(t("open.ambiguous"), file=sys.stderr)
            return 1
        chosen = matches[0]
    target = _document_path(root, chosen["doc_path"])
    if target is None:
        print(t("open.not_found", query=selector), file=sys.stderr)
        return 1
    anchor = chosen.get("anchor")
    print(str(target) + ("#" + anchor if anchor else ""))
    url = _show_viewer(root, target, t)
    if url:
        _open_externally(url + ("#" + anchor if anchor else ""))
    else:
        _open_externally(target.as_uri() + "#" + anchor if anchor else target)
    return 0


def _show_viewer(root: Path, target: Path, t) -> Optional[str]:
    try:
        from artifact_viewer import viewer_url
        url = viewer_url(root, target)
        if url:
            print(t("open.viewer", url=url))
        return url
    except Exception:
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print("usage: artifact_entrance.py refresh | reports | open [number|words ...]",
              file=sys.stderr)
        return 0 if args else 2
    command, rest = args[0], args[1:]
    root = resolve_root()
    if command == "refresh":
        print(json.dumps(refresh(root), ensure_ascii=False, sort_keys=True))
        return 0
    if command == "reports":
        return _command_reports(root)
    if command == "open":
        return _command_open(root, rest)
    print(f"artifact_entrance.py: unknown command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
