#!/usr/bin/env python3
"""Derived read entrance for one artifact root.

Writes `README.md`, `index.html`, `all-campaigns.md`, `all-campaigns.html`,
`all-spec.md`, `all-spec.html`, `recent-route-reports.md` and
`recent-route-reports.html` directly under the
artifact root so a person can see what exists and open it, without any external
tool.  The entrance (`README.md`/`index.html`) has up to four sections: spec topics,
recent campaigns (with their one-line summary), the backlog (one line that opens the
root's `backlog.md`, drawn only when that regular file exists) and the records (links to the
recent route reports page and the ID index).  The spec and campaign sections
show `ENTRANCE_SPEC_LINES` / `ENTRANCE_CAMPAIGN_LINES` lines and fold the rest
into one line that opens `all-spec.*` / `all-campaigns.*`.  The route reports themselves are
listed on `recent-route-reports.*`.  One function (`entrance_sections`) feeds the
entrance files, `hearting reports` (the numbered list in the terminal) and
`hearting open <number>`, so the three point at the same lines.

What this module never does: it never creates, moves, renames, or deletes a
campaign, a cycle, a manifest, or any document, and it never creates a symbolic
link. Refresh writes the eight entrance files above plus, once ever,
`<root>/.runtime/artifact-entrance.json` (the first user-facing cycle record).
One worker turn may also write a campaign's `campaign.md` (see `campaign_document`; it
replaces only a file whose first line is its own marker) before it refreshes the entrance.
Seal requests also use two locks and one coalesced pending record under
`<root>/.runtime/`; the finite refresh worker consumes that record and exits.

Determinism: identical root content and the same declared language produce
byte-identical files.  No wall clock, host name, user name, or absolute path is
rendered; every link is a relative POSIX path from the root.

Reading cost: directory enumeration reuses `artifact_locator.iter_campaign_dirs`
and `iter_cycle_dirs`; manifests are read newest first, only until the recent
list is full or `MAX_MANIFESTS` is reached (a full scan is never done).

Row sources: every recent row carries an internal `kind` (not rendered); rows
built from cycle folders are `"cycle"`.  Rows of kind `canonical-topic` are the
spec section's, not the route reports page's: `scan` returns them as `topics`.  `ROW_SOURCES` is the one place where
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
from datetime import datetime
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
CAMPAIGN_LIMIT = 10  # `campaign_groups`' own default; the entrance uses ENTRANCE_CAMPAIGN_LINES
ENTRANCE_SPEC_LINES = 3
ENTRANCE_CAMPAIGN_LINES = 3
SPEC_MD = "all-spec.md"
SPEC_HTML = "all-spec.html"
RECORDS_MD = "recent-route-reports.md"
RECORDS_HTML = "recent-route-reports.html"
MAX_MANIFESTS = 300
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HEAD_BYTES = 16 * 1024
SUMMARY_CHARS = 120
MARKER = "<!-- hearting:artifact-entrance v1 -->"
BACKLOG_NAME = "backlog.md"
MAX_BACKLOG_BYTES = 64 * 1024

# Hook for rows that are not cycle folders (see the module header).
def _campaign_target_path(folder: str, campaign_path: Path) -> str:
    """`<folder>/campaign.md` when the campaign has a readable one, else the folder itself."""
    try:
        from artifact_campaign_documents import CAMPAIGN_DOCUMENT, campaign_page
        page = campaign_page(campaign_path)
    except Exception:  # noqa: BLE001 -- a document that cannot be taken leaves the folder
        return folder
    return folder + "/" + CAMPAIGN_DOCUMENT if page is not None and page.get("status") == "known" else folder


def _daily_journals(root):
    from artifact_journal import entrance_snapshot
    return entrance_snapshot(root)


def _campaign_documents(root):
    from artifact_campaign_documents import entrance_snapshot
    return entrance_snapshot(root)


def _canonical_topics(root):
    from artifact_spec_storage import entrance_snapshot
    return entrance_snapshot(root)


ROW_SOURCES: Tuple[Callable[[Path], Iterable[Mapping]], ...] = (_daily_journals, _campaign_documents, _canonical_topics)
_DATE_FULL = re.compile(r"^\d{4}-\d{2}-\d{2}$")

ENTRANCE_NAMES = store.ENTRANCE_NAMES
DERIVED_KINDS = ("spec-rest", "all-campaigns", "recent-routes")  # lines that open a derived chapter
ENTRANCE_FIRST = ("README.md", "index.html")  # the entrance proper, written after its chapters
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
        "entrance.totals": "Campaigns: {campaigns} · routes: {cycles} · running: {open}",
        "entrance.section_spec": "spec",
        "entrance.section_campaigns": "Campaigns",
        "entrance.section_records": "Records",
        "entrance.section_backlog": "Backlog",
        "entrance.backlog_note": ("Work handed off, and what you said then. "
                                  "The session that postpones work writes one line each."),
        "entrance.backlog_open": "Open backlog.md",
        "entrance.backlog_count": "{count} items",
        "entrance.backlog_updated": "updated {date}",
        "entrance.recent_routes": "Recent route reports",
        "entrance.spec_note": "How this repo works now. {count} topics; each change edits the same document.",
        "entrance.campaigns_note": "One document per stream of work. No new document appears; this one is rewritten.",
        "entrance.records_note": "Reports left by routes. Not usually opened.",
        "entrance.all_spec": "All spec topics",
        "entrance.spec_rest": "{count} more",
        "entrance.spec_contents": "Contents",
        "entrance.more": "+{count} more",
        "entrance.cycles": "routes {count}",
        "entrance.latest": "latest {date}",
        "entrance.all_campaigns": "All campaigns",
        "entrance.all_campaigns_line": "All {count} campaigns",
        "entrance.id_index": "ID index",
        "entrance.back": "Back to the entrance",
        "entrance.rewrite": ("Hearting rewrites this file. Editing it is overwritten; "
                             "deleting it comes back at the next publish."),
        "entrance.empty": "No documents yet.",
        "entrance.status_ambiguous": "conflicting status pages",
        "entrance.status_recorded": "recorded",
        "entrance.status_unknown": "unconfirmed",
        "open.viewer": "Viewer: {url}",
        "reports.empty": "No documents yet.",
        "reports.foreign": "Kept an existing {name}: it carries no Hearting entrance marker.",
        "reports.entrance": "Entrance: {path}",
        "open.not_found": "No artifact matches {query}.",
        "open.ambiguous": "Several artifacts match; add more words to narrow it.",
        "open.no_line": "There is no line {number}. The entrance has lines 1 to {last}.",
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


def _campaign_meta(root: Path, campaign_id: Optional[str], locator: str, campaign_path: Path,
                   membership: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[str, str]:
    """`(title, summary)`: `artifact_meta.effective_title` without its per-call
    campaign-location lookup, plus the one-line summary from the same meta.json.

    The campaign directory is already known from the enumeration, so the only
    saving is that lookup; the source order (meta.json title, old display
    declaration, folder name) is the same function's order.  `membership` is
    shared across one scan because `read_campaign_meta` would otherwise rescan
    every cycle record.  A campaign without a readable meta.json has no summary."""
    summary = ""
    if campaign_id:
        try:
            if (campaign_path / artifact_meta.META_NAME).is_file():
                read = artifact_meta.read_campaign_meta(root, campaign_id,
                                                       members=membership if membership is not None else {})
                campaign = (read.doc or {}).get("campaign") if read.status == "ok" else None
                campaign = campaign if isinstance(campaign, dict) else {}
                if isinstance(campaign.get("summary"), str):
                    summary = _clip(campaign["summary"])
                title = campaign.get("title")
                if isinstance(title, str) and title:
                    return _nfc(title), summary
            legacy, _protected = artifact_meta.legacy_title(root, campaign_id)
            if legacy:
                return _nfc(legacy), summary
        except Exception:  # noqa: BLE001 - a title never blocks the entrance
            pass
    return _nfc(locator), summary


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
         campaign_limit: int = ENTRANCE_CAMPAIGN_LINES) -> Dict[str, Any]:
    """Read-only snapshot of the root feeding every rendered file and the CLI."""
    root = Path(root)
    snapshot: Dict[str, Any] = {
        "root": str(root),
        "recent": [],
        "topics": [],
        "campaigns": [],
        "backlog": None,
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
        folder = _nfc(campaign_path.relative_to(root).as_posix())
        campaign_rows.append({
            "title": "",
            "path": folder,
            # The campaign's own document when it has one, else the folder.
            "target": _campaign_target_path(folder, campaign_path),
            "cycles": len(cycles),
            "date": max(dates) if dates else "",
            "locator": campaign_locator,
            "summary": "",
        })
    membership = artifact_meta._membership(root) if has_meta else None
    for row, (campaign_locator, campaign_id, campaign_path) in zip(campaign_rows, identities):
        row["title"], row["summary"] = _campaign_meta(root, campaign_id, campaign_locator,
                                                      campaign_path, membership)
    snapshot["totals"] = totals
    snapshot["all_campaigns"] = sorted(campaign_rows, key=lambda row: (_desc(row["date"]), row["locator"]))

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

    source_rows = _source_rows(root, statuses=snapshot.setdefault("source_status", []))
    topics = [row for row in source_rows if row["kind"] == "canonical-topic"]
    recent.extend(row for row in source_rows if row["kind"] != "canonical-topic")
    recent.sort(key=lambda row: (_desc(row.get("at") or row["date"]), row["cycle_id"] or row["doc_path"], row["doc_path"]))
    snapshot["recent"] = recent[:recent_limit]
    topics.sort(key=lambda row: (_desc(row["date"]), row.get("component") or "", row["doc_path"]))
    with_components = {row.get("reference_id") for row in topics if row.get("component")}
    for row in topics:  # titles come from the PRD bytes the spec reader already verified
        row["role"] = "contents" if not row.get("component") and row.get("reference_id") in with_components \
            else "topic"
        _topic_title(row)
    snapshot["topics"] = topics
    snapshot["campaigns"] = [dict(row) for row in snapshot["all_campaigns"][:campaign_limit]]
    snapshot["backlog"] = _backlog(root)
    return snapshot


_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_BACKLOG_UPDATED = re.compile(r"^갱신 ([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})$")
_BACKLOG_HEADING = re.compile(r"^##(?:[ \t]|$)")


def _backlog_updated(line: str) -> str:
    """`갱신 YYYY-MM-DD HH:MM` as `YYYY-MM-DD HH:MM`, or "" when the line is another shape or no real time."""
    found = _BACKLOG_UPDATED.match(line.strip())
    if not found:
        return ""
    try:
        datetime.strptime(found.group(1), "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return found.group(1)


def _backlog(root: Path) -> Optional[Dict[str, Any]]:
    """The root's `backlog.md` as `{"count", "updated"}`, or None when there is no such regular file.

    Read by name from a no-follow fd, never written, never followed (a link is no file).  A file that
    cannot be read or is over MAX_BACKLOG_BYTES keeps the line but has no count (`count` None).
    A job is a top-level `- ` bullet under a `##` heading (core/ARTIFACT_META.md `backlog.md`)."""
    try:
        if not stat.S_ISREG(os.lstat(str(Path(root) / BACKLOG_NAME)).st_mode):
            return None
    except OSError:
        return None
    entry: Dict[str, Any] = {"count": None, "updated": ""}
    try:
        root_fd = open_root(os.path.realpath(str(root)))
        try:
            fd = open_relative(root_fd, [BACKLOG_NAME])
        finally:
            os.close(root_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = b""
            while len(raw) <= MAX_BACKLOG_BYTES:
                chunk = os.read(fd, MAX_BACKLOG_BYTES + 1 - len(raw))
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    except (OSError, RuntimeError):
        return entry
    if len(raw) > MAX_BACKLOG_BYTES:
        return entry
    count, fence, updated, headed = 0, None, "", False
    for line in raw.decode("utf-8", "replace").splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= fence[1] \
                    and not line.strip().strip(fence[0]):
                fence = None
            continue
        if opened:
            fence = (opened.group(1)[0], len(opened.group(1)))
            continue
        if _BACKLOG_HEADING.match(line):
            headed = True
        elif not headed and not updated:
            updated = _backlog_updated(line)
        elif headed and line.startswith("- "):
            count += 1
    entry.update({"count": count, "updated": updated})
    return entry


_SPEC_TAIL = re.compile(r" — [^—]*\bPRD\b[^—]*$")


def _topic_title(row: Dict[str, Any]) -> None:
    """Set the line's title from the PRD's first H1 (already read with the PRD).

    A topic drops the H1's trailing ` — …PRD…` part and falls back to the component name,
    or to the reference's title for a reference without components.  A reference's top
    contents `prd.md` keeps its H1 as written; without one the renderer says "Contents"."""
    h1 = row.get("h1") or ""
    if row["role"] == "contents":
        row["title"] = h1
        return
    title = _SPEC_TAIL.sub("", h1).strip()
    row["title"] = " ".join(title.split()) or row["title"]


def _document_path(root: Path, doc: str, *, placed: bool = False,
                   directory: bool = False) -> Optional[Path]:
    """A regular document (or a directory) under root, without traversal or any symlink component."""
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
        if not (path.is_dir() if directory else path.is_file()) \
                or not path.resolve().is_relative_to(root.resolve()):
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
    kind, date, title = _nfc(kind).strip(), _nfc(date), _nfc(title).strip()
    # Existing documents and immutable provider paths retain their exact
    # filesystem spelling, including decomposed Unicode names.
    doc = doc if kind in ('campaign-document', 'canonical-topic') else _nfc(doc)
    if not kind or kind == "cycle" or not title or not (_DATE_FULL.match(date) or (kind == 'canonical-topic' and date == '')):
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
    if kind == 'canonical-topic':
        from artifact_identity import is_well_formed
        if not (is_well_formed(raw.get('reference_id'), 'shared_reference')
                and is_well_formed(raw.get('revision_id'), 'shared_reference_revision')):
            return None
        row.update({key: raw.get(key) for key in ('artifact_root_id', 'repository_id', 'campaign_id',
            'cycle_id', 'cycle_path', 'artifact_id', 'artifact_revision_id', 'reference_id', 'component',
            'revision_id', 'source_revision_id', 'source_path', 'logical_locator', 'status', 'coverage', 'digest')})
        h1 = raw.get('h1')
        row['h1'] = " ".join(_nfc(h1).split()) if isinstance(h1, str) else ""
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
    elif kind == 'campaign-document':
        row.update({key: raw.get(key) for key in ('campaign_id', 'artifact_root_id', 'repository_id',
            'artifact_id', 'revision_id', 'status', 'coverage', 'mtime', 'timestamp_source')})
        row['record_target'] = doc
        row['result_targets'] = []
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
    """Recent records (cycle reports, journal entries, campaign documents), newest first, at most `limit`.

    Not the entrance's numbered lines (`entrance_sections` owns those)."""
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


def _md_summary(value: str) -> str:
    """A model-written summary line: Markdown specials and `<`, `>`, `&` show as typed.
    Backslash escapes, not entities: the viewer does not decode entities, and the raw file stays readable."""
    return re.sub(r"([\\`*_\[\]<>&])", r"\\\1", _nfc(value))


def _campaign_target(row: Mapping[str, Any]) -> str:
    """Where a campaign line opens: its document when it has one, else its folder."""
    return str(row.get("target") or row["path"])


def _md_href(path: str) -> str:
    return _nfc(path).replace("<", "%3C").replace(">", "%3E")


def _href(path: str) -> str:
    return quote(path, safe="/.")


def _record_target(row):
    return row.get("record_target") or row["doc_path"]


def _record_md_href(row):
    return _href(row['doc_path']) if row['kind'] in ('campaign-document', 'canonical-topic') else _md_href(_record_target(row))


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


def _row_status(row, t):
    status = row.get('status') or 'unknown'
    if row['kind'] == 'campaign-document':
        return t('entrance.status_' + (status if status in ('recorded', 'ambiguous') else 'unknown'))
    return str(status)


def _markdown_recent(snapshot: Dict[str, Any], t) -> List[str]:
    lines: List[str] = []
    for row in snapshot["recent"]:
        suffix = f"  ({t('entrance.more', count=_count(row['extra']))})" if row["extra"] else ""
        where = f"  — `{_md_href(row['cycle_path'])}`" if row["cycle_path"] else ""
        lines.append(f"- {row['date']}  [{_md_text(row['title'])}](<{_record_md_href(row)}>)"
                     f"{where}{suffix}")
        if row["kind"] in ("journal-entry", "campaign-document"):
            links = [f"[{t('entrance.open_record')}](<{_record_md_href(row)}>)"]
            links += [f"[{t('entrance.open_result')}](<{item['href']}>)" for item in _result_links(row)]
            lines.append("   " + " · ".join(links) + " · " + _md_text(_row_status(row, t)))
        if row["summary"]:
            lines.append(f"   {_md_summary(row['summary'])}")
    if not lines:
        lines.append(t("entrance.empty"))
    if _partial_sources(snapshot):
        lines.append(t("entrance.partial"))
    return lines


def _spec_topics(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every spec topic: each component's PRD, and the top `prd.md` of a reference without components."""
    return [row for row in (snapshot.get("topics") or []) if row.get("role") == "topic"]


def _spec_contents(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The top `prd.md` of a reference that has components: a table of contents, not a topic."""
    return [row for row in (snapshot.get("topics") or []) if row.get("role") == "contents"]


def entrance_sections(snapshot: Dict[str, Any], t) -> List[Dict[str, Any]]:
    """The entrance's sections and lines, numbered once for every consumer.

    `README.md`, `index.html`, `hearting reports` and `hearting open <number>` all
    read this list, so they point at the same lines.  Only lines that link
    somewhere carry a `number` (1, 2, … from the top).  A section's `note` is the
    one line under its title; a line's `note` is the one line about it.  Neither
    is numbered."""
    def line(kind, title, md, html, target, *, date="", summary="", row=None, directory=False,
             note=""):
        return {"kind": kind, "date": date, "title": title, "summary": summary, "note": note,
                "md": md, "html": html, "target": target, "row": row,
                "directory": directory, "number": None}

    sections: List[Dict[str, Any]] = []
    components = _spec_topics(snapshot)
    if components:
        lines = [line("spec", row["title"], _record_md_href(row), _record_href(row), row["doc_path"],
                      date=row["date"], row=row) for row in components[:ENTRANCE_SPEC_LINES]]
        if len(components) > ENTRANCE_SPEC_LINES:
            lines.append(line("spec-rest",
                              t("entrance.spec_rest", count=_count(len(components) - ENTRANCE_SPEC_LINES)),
                              SPEC_MD, SPEC_HTML, SPEC_HTML))
        sections.append({"key": "spec", "title": t("entrance.section_spec"),
                         "note": t("entrance.spec_note", count=_count(len(components))), "lines": lines})
    lines = [line("campaign", row["title"], _md_href(_campaign_target(row)), _href(_campaign_target(row)),
                  _campaign_target(row), date=row["date"], summary=row.get("summary") or "",
                  directory=_campaign_target(row) == row["path"])
             for row in snapshot["campaigns"][:ENTRANCE_CAMPAIGN_LINES]]
    if not lines:
        lines.append(line("text", t("entrance.empty"), None, None, None))
    else:
        lines.append(line("all-campaigns", t("entrance.all_campaigns_line",
                                             count=_count(len(snapshot["all_campaigns"]))),
                          "all-campaigns.md", "all-campaigns.html", "all-campaigns.html"))
    sections.append({"key": "campaigns", "title": t("entrance.section_campaigns"),
                     "note": t("entrance.campaigns_note"), "lines": lines})
    backlog = snapshot.get("backlog")
    if backlog:
        meta = []
        if backlog["count"] is not None:
            meta.append(t("entrance.backlog_count", count=_count(backlog["count"])))
        if backlog["updated"]:
            meta.append(t("entrance.backlog_updated", date=backlog["updated"]))
        sections.append({"key": "backlog", "title": t("entrance.section_backlog"),
                         "note": t("entrance.backlog_note"), "lines": [
            line("backlog", t("entrance.backlog_open"), _md_href(BACKLOG_NAME), _href(BACKLOG_NAME),
                 BACKLOG_NAME, note=" · ".join(meta))]})
    sections.append({"key": "records", "title": t("entrance.section_records"), "note": "", "lines": [
        line("recent-routes", t("entrance.recent_routes"), RECORDS_MD, RECORDS_HTML, RECORDS_HTML,
             note=t("entrance.records_note")),
        line("id-index", t("entrance.id_index"), "campaigns/INDEX.md", "campaigns/INDEX.md",
             "campaigns/INDEX.md")]})
    number = 0
    for section in sections:
        for item in section["lines"]:
            if item["md"] is not None:
                number += 1
                item["number"] = number
    return sections


def render_markdown(snapshot: Dict[str, Any], t) -> str:
    totals = snapshot["totals"]
    lines = [
        MARKER,
        f"# {t('entrance.title')}",
        "",
        t("entrance.totals", campaigns=_count(totals["campaigns"]),
          cycles=_count(totals["cycles"]), open=_count(totals["open"])),
    ]
    for section in entrance_sections(snapshot, t):
        lines.extend(["", f"## {section['title']}", ""])
        if section["note"]:
            lines.extend([_md_text(section["note"]), ""])
        for item in section["lines"]:
            if item["md"] is None:
                lines.append(_md_text(item["title"]))
                continue
            when = f"{item['date']}  " if item["date"] else ""
            lines.append(f"{item['number']}. {when}[{_md_text(item['title'])}](<{item['md']}>)")
            if item["summary"]:
                lines.append(f"   {_md_summary(item['summary'])}")
            if item["note"]:
                lines.append(f"   {_md_text(item['note'])}")
        if section["key"] == "records" and _partial_sources(snapshot):
            lines.append(t("entrance.partial"))
    lines.extend(["", t("entrance.rewrite"), ""])
    return "\n".join(lines)


def render_recent_markdown(snapshot: Dict[str, Any], t) -> str:
    lines = [MARKER, f"# {t('entrance.recent_routes')}", "",
             f"[{t('entrance.back')}](<README.md>)", ""]
    lines.extend(_markdown_recent(snapshot, t))
    lines.extend(["", t("entrance.rewrite"), ""])
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
        lines.append(f"- [{_md_text(row['title'])}](<{_md_href(_campaign_target(row))}>) — " + " · ".join(parts))
    if not snapshot["all_campaigns"]:
        lines.append(t("entrance.empty"))
    lines.extend(["", t("entrance.rewrite"), ""])
    return "\n".join(lines)


def render_spec_markdown(snapshot: Dict[str, Any], t) -> str:
    components = _spec_topics(snapshot)
    lines = [MARKER, f"# {t('entrance.all_spec')}", "", f"[{t('entrance.back')}](<README.md>)", ""]
    for row in _spec_contents(snapshot):
        lines.extend([f"{row['date']}  [{_md_text(row['title'] or t('entrance.spec_contents'))}]"
                      f"(<{_record_md_href(row)}>)", ""])
    for row in components:
        lines.append(f"- {row['date']}  [{_md_text(row['title'])}](<{_record_md_href(row)}>)")
    if not components:
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


def _html_recent_items(snapshot: Dict[str, Any], t) -> List[str]:
    body: List[str] = []
    for row in snapshot["recent"]:
        suffix = f" &#183; {_escape(t('entrance.more', count=_count(row['extra'])))}" if row["extra"] else ""
        summary = f"<br><span class=\"meta\">{_escape(row['summary'])}</span>" if row["summary"] else ""
        where = f" &#183; {_escape(row['cycle_path'])}" if row["cycle_path"] else ""
        body.append(f'<li><span class="meta">{_escape(row["date"])}</span> '
                    f'<a href="{_record_href(row)}">{_escape(row["title"])}</a>'
                    f'<span class="meta">{where}{suffix}</span>{summary}')
        if row["kind"] in ("journal-entry", "campaign-document"):
            links = [f'<a href="{_record_href(row)}">{_escape(t("entrance.open_record"))}</a>']
            # Result hrefs are already encoded file URIs from the common reader.
            links += [f'<a href="{_escape(item["href"])}">{_escape(t("entrance.open_result"))}</a>' for item in _result_links(row)]
            body.append('<br>' + ' &#183; '.join(links) + ' &#183; ' + _escape(_row_status(row, t)))
        body[-1] += '</li>'
    if not snapshot["recent"]:
        body.append(f"<li>{_escape(t('entrance.empty'))}</li>")
    if _partial_sources(snapshot):
        body.append(f"<li>{_escape(t('entrance.partial'))}</li>")
    return body


def render_html(snapshot: Dict[str, Any], t) -> str:
    totals = snapshot["totals"]
    body = [f"<h1>{_escape(t('entrance.title'))}</h1>",
            f'<p class="meta">{_escape(t("entrance.totals", campaigns=_count(totals["campaigns"]), cycles=_count(totals["cycles"]), open=_count(totals["open"])))}</p>']
    for section in entrance_sections(snapshot, t):
        body.append(f"<h2>{_escape(section['title'])}</h2>")
        if section["note"]:
            body.append(f'<p class="meta">{_escape(section["note"])}</p>')
        body.append("<ul>")
        for item in section["lines"]:
            if item["html"] is None:
                body.append(f"<li>{_escape(item['title'])}</li>")
                continue
            when = f'<span class="meta">{_escape(item["date"])}</span> ' if item["date"] else ""
            summary = f'<br><span class="meta">{_escape(item["summary"])}</span>' if item["summary"] else ""
            note = f' <span class="meta">{_escape(item["note"])}</span>' if item["note"] else ""
            body.append(f'<li>{when}<a href="{item["html"]}">{_escape(item["title"])}</a>{note}{summary}</li>')
        body.append("</ul>")
        if section["key"] == "records" and _partial_sources(snapshot):
            body.append(f'<p class="meta">{_escape(t("entrance.partial"))}</p>')
    body.append(f'<p class="note">{_escape(t("entrance.rewrite"))}</p>')
    return _html_page(t("entrance.title"), getattr(t, "lang", "en"), body)


def render_recent_html(snapshot: Dict[str, Any], t) -> str:
    body = [f"<h1>{_escape(t('entrance.recent_routes'))}</h1>",
            f'<p><a href="index.html">{_escape(t("entrance.back"))}</a></p>', "<ul>"]
    body.extend(_html_recent_items(snapshot, t))
    body.extend(["</ul>", f'<p class="note">{_escape(t("entrance.rewrite"))}</p>'])
    return _html_page(t("entrance.recent_routes"), getattr(t, "lang", "en"), body)


def render_spec_html(snapshot: Dict[str, Any], t) -> str:
    components = _spec_topics(snapshot)
    body = [f"<h1>{_escape(t('entrance.all_spec'))}</h1>",
            f'<p><a href="index.html">{_escape(t("entrance.back"))}</a></p>']
    for row in _spec_contents(snapshot):
        body.append(f'<p><span class="meta">{_escape(row["date"])}</span> '
                    f'<a href="{_record_href(row)}">{_escape(row["title"] or t("entrance.spec_contents"))}</a></p>')
    body.append("<ul>")
    for row in components:
        body.append(f'<li><span class="meta">{_escape(row["date"])}</span> '
                    f'<a href="{_record_href(row)}">{_escape(row["title"])}</a></li>')
    if not components:
        body.append(f"<li>{_escape(t('entrance.empty'))}</li>")
    body.extend(["</ul>", f'<p class="note">{_escape(t("entrance.rewrite"))}</p>'])
    return _html_page(t("entrance.all_spec"), getattr(t, "lang", "en"), body)


def render_all_html(snapshot: Dict[str, Any], t) -> str:
    body = [f"<h1>{_escape(t('entrance.all_campaigns'))}</h1>",
            f'<p><a href="index.html">{_escape(t("entrance.back"))}</a></p>', "<ul>"]
    for row in snapshot["all_campaigns"]:
        parts = [t("entrance.cycles", count=_count(row["cycles"]))]
        if row["date"]:
            parts.append(t("entrance.latest", date=row["date"]))
        body.append(f'<li><a href="{_href(_campaign_target(row))}">{_escape(row["title"])}</a> '
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
        _worker_pass(root, cycle_id)
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


def _worker_pass(root: Path, cycle_id: Optional[str] = None) -> None:
    """One worker turn: bring the campaign documents up to date, then refresh the entrance.

    The documents come first so that the entrance written next already points at a document
    this turn created. A failing generator never stops the entrance."""
    try:
        import campaign_document
        campaign_document.catch_up(root, cycle_id)
    except Exception as exc:  # noqa: BLE001 -- the entrance never waits on the documents
        print(f"campaign_document skipped error={type(exc).__name__}", file=sys.stderr)
    refresh(root, cycle_id=cycle_id)


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
                _worker_pass(root, demand.get("cycle_id"))
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
        RECORDS_MD: render_recent_markdown(snapshot, t),
        RECORDS_HTML: render_recent_html(snapshot, t),
        SPEC_MD: render_spec_markdown(snapshot, t),
        SPEC_HTML: render_spec_html(snapshot, t),
    }
    # All page inputs, translations and renderer implementation determine reuse.
    # The cached file bytes are compared as well, so foreign edits and missing
    # files always go through the regular marker and symlink checks below.
    cache_key = str(root.absolute())
    try:
        renderer_revision = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        renderer_revision = None  # no reliable identity: render normally
    pages_input = [{"title": row["title"], "path": row["path"], "target": _campaign_target(row),
                    "cycles": row["cycles"], "date": row["date"]}
                   for row in snapshot["all_campaigns"]]  # the cells the page renders
    fingerprint = json.dumps([pages_input, getattr(t, "lang", "en"),
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
    # The chapters the entrance points at are written first and the two entrance files
    # last, so a write that fails part-way never leaves an entrance pointing at a chapter
    # that does not exist.
    for name in (*(n for n in ENTRANCE_NAMES if n not in ENTRANCE_FIRST), *ENTRANCE_FIRST):
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
    """Rows without numbers: the entrance's numbers do not count these."""
    for row in rows:
        suffix = f"  ({_translator()('entrance.more', count=_count(row['extra']))})" if row["extra"] else ""
        when = f"{row['date']}  " if row["date"] else ""
        print(f"{when}{row['title']}{suffix}")
        status = ' · ' + _row_status(row, _translator()) if row['kind'] == 'campaign-document' else ''
        print(f"   {_record_target(row)}{status}")
        for item in _result_links(row):
            print(f"   {_translator()('entrance.open_result')}: {item['path']}")


def _can_open_externally() -> bool:
    """Whether this shell has a screen to open a document on."""
    return sys.stdout.isatty() and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or sys.platform == "darwin")


def _open_externally(path: Path | str) -> None:
    if not _can_open_externally():
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
    sections = entrance_sections(snapshot, t) if snapshot else []
    if snapshot and _partial_sources(snapshot):
        print(t("entrance.partial"))
    if not sections:
        print(t("reports.empty"))
    for section in sections:
        print(section["title"])
        if section["note"]:
            print(f"   {section['note']}")
        for item in section["lines"]:
            if item["number"] is None:
                print(f"   {item['title']}")
                continue
            when = f"{item['date']}  " if item["date"] else ""
            print(f"{item['number']}. {when}{item['title']}")
            # a terminal reads the `.md` chapter; `index.html` links the `.html` one
            print(f"   {item['md'] if item['kind'] in DERIVED_KINDS else item['target']}")
            if item["summary"]:
                print(f"   {item['summary']}")
            if item["note"]:
                print(f"   {item['note']}")
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
    if not args:
        target = root / "index.html"
        print(str(target))
        _show_viewer(root, target, t)
        return 0
    selector = " ".join(args)
    numbered = [item for section in (entrance_sections(snapshot, t) if snapshot else [])
                for item in section["lines"] if item["number"] is not None]
    line: Optional[Dict[str, Any]] = None
    chosen: Optional[Dict[str, Any]] = None
    if len(args) == 1 and args[0].isascii() and args[0].isdigit():
        if not numbered:  # no entrance lines at all: there is no range to name
            print(t("open.not_found", query=selector), file=sys.stderr)
            return 1
        if not 1 <= int(args[0]) <= len(numbered):  # a number is a line, never a search word
            print(t("open.no_line", number=args[0], last=len(numbered)), file=sys.stderr)
            return 1
        line = numbered[int(args[0]) - 1]
        doc_path = line["target"]
        if line["kind"] in DERIVED_KINDS and not _can_open_externally():
            doc_path = line["md"]  # a shell without a screen reads the `.md` chapter
        chosen = line["row"] or {"doc_path": doc_path, "kind": line["kind"]}
    if chosen is None:
        rows = (snapshot["recent"] + snapshot["topics"]) if snapshot else []
        words = [word.casefold() for word in args if word.strip()]
        matches = [row for row in rows if all(
            word in (" ".join([row["title"], row["summary"], row["doc_path"], row["cycle_path"] or '']).casefold())
            for word in words)]
        if not matches:
            print(t("open.not_found", query=selector), file=sys.stderr)
            return 1
        if len(matches) > 1:
            _print_rows(matches, root)
            print(t("open.ambiguous"), file=sys.stderr)
            return 1
        chosen = matches[0]
    target = _document_path(root, chosen["doc_path"], directory=bool(line and line["directory"]))
    if target is None:
        print(t("open.not_found", query=selector), file=sys.stderr)
        return 1
    anchor = chosen.get("anchor")
    print(str(target) + ("#" + anchor if anchor else ""))
    viewer_target = root / chosen['logical_locator'] if chosen['kind'] == 'canonical-topic' else target
    url = _show_viewer(root, viewer_target, t)
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
