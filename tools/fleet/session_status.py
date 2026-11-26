"""The shell panel's status button and popup: which status document this session shows, and its lines.

``read(cwd, route_ids)`` is the panel's one value (``None`` when the folder has no artifact root: no
button then).  The document is the one of this session's own campaign (route -> its record's campaign
key -> ``route_autoclose.campaign_of_route`` -> the campaign folder -> ``artifact_campaign_documents.
campaign_status``, the selection Fleet and the entrance share); a session whose
routes span several campaigns shows the most recently changed document among them (Fleet's own rule).
A session with no route, no campaign key, a campaign Fleet does not list (closed, or not yet named) or
a campaign without a document shows the document Fleet's project line picks (``project_status.collect``).
No document at all is still a value (``has_doc`` False: the button stands, dim) with the place a
document would stand.  ``watch`` is for the panel's existing file watch (no poller): the document, and
each session campaign's folder as one folder target (a trailing separator) so a new document redraws.

The value does not read the document (11-20: the panel has the button alone, no preview): it is whether a document stands, its
title and its age.  ``parse_groups`` / ``read_groups`` (the document's lists: what is in progress, what waits for the person,
what ended lately, what comes later) are kept for the popup's coming list and are not called on the panel's refresh path.

``popup_doc(view, width)`` is the popup's document part: the document's rows (``popup_doc_rows``) and, for the
campaign document Hearting generates (its first line is the marker and it was read whole), ``listing``: the
list screen the popup opens on (11-21, design C; 11-22: what is in progress first, no question among it), as entries the host lays out
by its state (which row is chosen, which groups and items are open).  Any other document (a hand-written page,
a cut one, none) has no listing and the popup draws exactly the rows it always drew.

``held(cwd, claim)`` is the value the host already holds (its button's) taken for the popup, so the popup
does not read what the button read: it stands only where the claim names a status document inside this
folder's own artifact root (else ``None``, and the popup reads).

``popup_rows(view, status, width)`` is the popup's body: Fleet's status lines for this project exactly as
its project page draws them (``pages.repo_status_lines``), an empty row, then the whole document
(``transcript.doc_rows``: Fleet cards, fences as written, markdown tables aligned).  Rows are lists of
``(text, key)`` segments; ``key`` is a Fleet palette key (``None`` for padding).  Nothing here writes,
asks, gates or starts a process; ``project_status`` and ``artifact_campaign_documents``
are called, not changed.
"""
from collections.abc import Mapping
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from .config import ui_text
from .collectors import project_status
from .explorer import pages, transcript
from .explorer.grid import CellGrid

_UTILITIES = str(Path(__file__).resolve().parents[2] / "utilities")
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)

INTERVAL = 60.0        # seconds a folder keeps its resolved root, and a campaign key its folder
READ_LIMIT = project_status.READ_LIMIT     # bytes of the document the popup reads (64 KiB)
MAX_ROUTES = 8         # routes of one session whose campaigns are looked at
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
_ROOTS = {}            # session folder -> (resolved at, artifact root or None)
_KEYS = {}             # (root, route id) -> its campaign key (a route's key never changes)
_FOLDERS = {}          # (root, campaign key) -> (resolved at, campaign folder or None)
GROUP_ITEMS = 6        # titles kept per group (the panel shows eight at most in all)
GROUP_TITLE = 200      # cells of a title kept


def clear_cache():
    for cache in (_ROOTS, _KEYS, _FOLDERS):
        cache.clear()


def _root(cwd, now):
    key = str(cwd)
    found = _ROOTS.get(key)
    if found is None or not 0 <= now - found[0] < INTERVAL:
        found = (now, project_status.artifact_root(key))
        _ROOTS[key] = found
    return found[1]


def _route_ids(routes):
    ids = []
    for item in routes or ():
        value = item.get("id") if isinstance(item, Mapping) else item
        value = str(value or "")
        if _NAME.match(value) and value not in ids:
            ids.append(value)
    return ids[:MAX_ROUTES]


def _campaign_folders(root, route_ids, now):
    """The folders of the (open) campaigns this session's routes belong to, in route order.

    A route whose record cannot be read, names no campaign, or whose campaign is not open (closed, or
    not yet in the ``campaigns/`` list) adds none: the session does not know that campaign."""
    try:
        import artifact_campaign_documents as documents
        import route_autoclose
    except Exception:  # noqa: BLE001 -- no resolver: the session knows no campaign
        return []
    folders = []
    for route_id in route_ids:
        try:
            record = None
            key = _KEYS.get((root, route_id))
            if key is None:
                raw, _info, coverage = documents.read_document(      # no link is followed, 64 KiB at most
                    Path(root) / ".runtime" / "routes", route_id + ".json")
                if coverage != "complete":
                    continue
                record = json.loads(raw.decode("utf-8"))
                if not isinstance(record, dict) or record.get("route_id") != route_id:
                    continue
                key = route_autoclose.campaign_key_of(record) or ""
                _KEYS[(root, route_id)] = key
            if not key:
                continue
            found = _FOLDERS.get((root, key))
            if found is None or not 0 <= now - found[0] < INTERVAL:
                resolved = route_autoclose.campaign_of_route(
                    root, record if record is not None else {"campaign_key": key})
                found = (now, str(resolved[1]) if resolved else None)
                _FOLDERS[(root, key)] = found
            if found[1] and found[1] not in folders:
                folders.append(found[1])
        except Exception:  # noqa: BLE001 -- an unreadable route is a campaign not known
            continue
    return folders


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _campaign_name(folder):
    return _DATE.sub("", os.path.basename(str(folder).rstrip(os.sep)))


def _oversize(documents, candidates):
    """The status page the shared selection left 'unknown' only because it is over 64 KiB, else None.

    ``select_status`` takes a page only when it reads whole; a longer one is still this campaign's page,
    and the popup reads its first 64 KiB (Fleet's project line does not list such a page)."""
    for path in candidates or ():
        try:
            if documents.read_document(Path(path).parent, Path(path).name)[2] == "partial":
                return path
        except Exception:  # noqa: BLE001 -- a page that cannot be read is not shown
            continue
    return None


def _own_document(folders):
    """``(path, campaign folder)`` of the most recently changed status document among ``folders``."""
    import artifact_campaign_documents as documents
    best = None
    for folder in folders:
        try:
            chosen = documents.campaign_status(Path(folder))
        except Exception:  # noqa: BLE001 -- a campaign whose pages cannot be read has none to show
            continue
        path = chosen.get("path") if isinstance(chosen, Mapping) else None
        if path is None and isinstance(chosen, Mapping) and chosen.get("status") == "unknown":
            path = _oversize(documents, chosen.get("candidates"))
        stamp = _mtime(path) if path is not None else None
        if stamp is not None and (best is None or stamp > best[0]):
            best = (stamp, str(path), folder)
    return best[1:] if best else (None, None)


def _age(stamp, now):
    if stamp is None:
        return ""
    try:
        from .explorer.live import _ago
        return _ago(stamp, now) or ""
    except Exception:  # noqa: BLE001 -- the age is left out, never the document
        return ""


# The headings of the campaign document (``## 하는 중`` ...) and the near spellings an older hand-written page uses.  A heading
# that names a record of what is done (``기록``, ``log`` ...) is no list of work.  The order is the panel's.  A heading is read by how it
# BEGINS (as ``project_status._section_kind`` reads ``다음``/``예정``), an English word whole (``Definition of Done``, ``Not done yet``,
# ``How to do the release`` name no list).
_GROUP_WORDS = (
    ("running", ("하는 중", "진행 중", "in progress", "in flight", "doing")),
    ("waiting", ("결정 기다리", "기다리는", "결정 필요", "결정할", "결정 대기", "사용자에게 필요", "decisions needed",
                 "decision needed", "needs decision", "to decide", "open decision", "pending decision", "needs you", "waiting")),
    ("done", ("끝난 일", "끝난 것", "최근에 끝", "최근 마감", "recently closed", "recently done", "done")),
    ("next", ("예정", "나중에", "다음", "next", "later", "할 일", "to do", "todo")),
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
_ENGLISH_WORD = re.compile(r"[a-z][a-z ]*")


def _begins(lowered, word):
    if not lowered.startswith(word):
        return False
    return not (_ENGLISH_WORD.fullmatch(word) and lowered[len(word):len(word) + 1].isalnum())


# What a hand-written heading may carry before its words: a number (``1.``, ``2)``) and pictures or marks (``⏳``, ``🔴``, ``[``).
_LEAD = re.compile(r"^[\W_]*(?:\d+[.)]\s*)?[\W_]*")


def _group_kind(title):
    lowered = title.casefold().lstrip()
    lowered = lowered[_LEAD.match(lowered).end():]
    if any(word in lowered for word in project_status._RECORD_WORDS):
        return None
    for kind, words in _GROUP_WORDS:
        if any(_begins(lowered, word) for word in words):
            return kind
    return None


def parse_groups(text):
    """The lists of a status document as ``[{kind, name, count, items}]`` in the panel's order.

    Read: the top-level bullets under a heading of a list (``하는 중``, ``결정 기다리는 것``, ``끝난 일``, ``예정`` and the near
    spellings above; one of each kind, the first).  A bullet that is only a note in parentheses (``(… 접어 둠)``) is no title.
    A list with no title is left out, except the one that waits for the person: its heading with nothing under it says
    nothing waits (count 0).  Nothing is invented: a document with no such heading gives ``[]``."""
    found, current, level, fence = {}, None, 0, False
    for raw in str(text).splitlines():
        if raw.strip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:                       # what a code fence holds is no heading and no job
            continue
        heading = _HEADING.match(raw.strip())
        if heading:
            depth, words = len(heading.group(1)), project_status._plain(heading.group(2))
            if depth == 1:
                current = None
                continue
            kind = _group_kind(words)
            if kind is not None and kind not in found:
                current, level = kind, depth
                found[kind] = []
            elif current is not None and depth <= level:
                current = None
            continue
        if current is None or raw[:1] in (" ", "\t"):
            continue
        bullet = _BULLET.match(raw)
        if not bullet:
            continue
        title = project_status._plain(bullet.group(1))
        if title and not (title.startswith(("(", "（")) and title.endswith((")", "）"))):
            found[current].append(title)
    groups = []
    for kind in ("running", "waiting", "done", "next"):
        titles = found.get(kind)
        if titles is None or not (titles or kind == "waiting"):
            continue
        items = [transcript.clean(title)[:GROUP_TITLE] for title in titles[:GROUP_ITEMS]]
        groups.append({"kind": kind, "name": ui_text("fleet.status.group." + kind), "count": len(titles), "items": items})
    return groups


def read_groups(path):
    """``parse_groups`` of the document's first 64 KiB; ``[]`` if unreadable.  (Kept for the popup's list; the panel's refresh does not call it.)"""
    try:
        lines, _cut = read_text(path)
        return parse_groups("\n".join(lines)) if lines is not None else []
    except Exception:  # noqa: BLE001 -- a document that cannot be read gives no section, never a failed button
        return []


# 11-20 (the person: "현황은 영어로"): the panel's other section names (REPO, GIT, PEERS, SESSION, ROUTE, MESSAGES, MEM) are English whatever
# language is declared, so this one is too.  Fleet's explorer keeps ``fleet.repo.status`` (there every section name follows the language).
STATUS_WORD = "STATUS"


def _title(word, campaign, age):
    if not (campaign or age):
        return word
    return word + (" " + campaign if campaign else "") + (" · " + age if age else "")


def read(cwd, routes=(), now=None):
    """The panel's status value for a session folder, or ``None`` when its project has no artifact root.

    ``routes``: this session's route ids (or its route rows, each with ``id``).  Never raises.
    Keys: ``word`` (the section's name, ``STATUS``), ``has_doc``, ``campaign`` (name, '' when not known),
    ``age`` (Fleet's age words, '' when none), ``title`` (the popup's title), ``path`` (the document or
    ''), ``slot`` (where a missing document would stand, '' when no campaign is known), ``watch``
    (paths for the panel's file watch).  The document itself is not read."""
    if not cwd:
        return None
    now = time.time() if now is None else now
    try:
        root = _root(cwd, now)
        if not root:
            return None
        word = STATUS_WORD
        folders = _campaign_folders(root, _route_ids(routes), now)
        path, folder = _own_document(folders)
        campaign = _campaign_name(folder) if folder else ""
        if path is None:
            record = project_status.collect([str(cwd)], now).get(str(cwd))
            if isinstance(record, Mapping) and record.get("path"):
                path = str(record["path"])
                campaign = str(record.get("campaign") or "")
        age = _age(_mtime(path), now) if path else ""
        slot = ""
        if path is None and folders:
            campaign = _campaign_name(folders[0])
            slot = os.path.relpath(os.path.join(folders[0], project_status.STATUS_NAMES[0]), root)
        # (11-21: the backlog document is on the same watch, so the panel's BACKLOG part is read again when it changes)
        watch = ([path] if path else []) + [folder.rstrip(os.sep) + os.sep for folder in folders] + [os.path.join(root, BACKLOG_NAME)]
        return {"word": word, "has_doc": path is not None, "campaign": campaign, "age": age,
                "title": _title(word, campaign if path else "", age), "path": path or "",
                "slot": slot, "watch": list(dict.fromkeys(watch))}
    except Exception:  # noqa: BLE001 -- the panel draws on without a status button
        return None


def held(cwd, claim, now=None):
    """``claim`` (the host's button value: ``word``, ``title``, ``has_doc``, ``path``, ``slot``) as a value, or ``None``.

    Nothing the host sends is trusted as a place to read: a document path stands only when it is a status
    document's name inside the artifact root of ``cwd`` (the one ``read`` would name)."""
    try:
        if not cwd or not isinstance(claim, Mapping):
            return None
        root = _root(cwd, time.time() if now is None else now)
        word, title, slot = claim.get("word"), claim.get("title"), claim.get("slot") or ""
        if (not root or not isinstance(word, str) or not isinstance(title, str) or not isinstance(slot, str)
                or len(word) > 80 or len(title) > 400 or len(slot) > 400):
            return None
        path = ""
        if claim.get("has_doc") is True:
            import artifact_campaign_documents as documents
            path = claim.get("path")
            if (not isinstance(path, str) or not os.path.isabs(path) or len(path) > 4096 or "\0" in path
                    or os.path.commonpath([root, os.path.realpath(path)]) != root
                    or _fold(os.path.basename(path)) not in (documents.CAMPAIGN_DOCUMENT, *documents.STATUS_NAMES)):
                return None
            slot = ""
        return {"word": word, "has_doc": bool(path), "campaign": "", "age": "", "title": title, "path": path,
                "slot": slot, "watch": []}
    except Exception:  # noqa: BLE001 -- a claim that cannot be judged is read again
        return None


def _fold(name):
    import artifact_campaign_documents as documents
    return documents.fold(name)


def _grid_rows(grid, until):
    """A Fleet grid's first ``until`` rows as ``(text, key)`` segments (tints dropped, trailing blanks cut)."""
    rows = []
    for cells in grid.cells[:until]:
        parts, current, text = [], None, ""
        for char, key, _tint in cells:
            if char == "":
                continue
            if key != current and text:
                parts.append((text, current))
                text = ""
            current = key
            text += char
        if text:
            parts.append((text, current))
        while parts and not parts[-1][0].strip():
            parts.pop()
        rows.append(parts)
    return rows


def _fleet_lines(status, width):
    grid = CellGrid(width, 12)
    return _grid_rows(grid, pages.repo_status_lines(grid, 0, width, 0, status))


class _Lines(list):
    """A document's lines, with its first line as ``carries_marker`` reads it (``splitlines`` also cuts at CR, FF, U+2028)."""
    head = None


def read_text(path):
    """``(lines, cut)`` of the document's first 64 KiB: a cut leaves off its torn last line; None if unreadable."""
    import artifact_campaign_documents as documents
    try:
        raw, _info, coverage = documents.read_document(Path(path).parent, Path(path).name)
    except Exception:  # noqa: BLE001
        return None, False
    text = raw.decode("utf-8", "replace")
    cut = coverage != "complete"
    if cut:
        text = text[:text.rfind("\n") + 1] if "\n" in text else ""
    lines = _Lines(text.splitlines())
    lines.head = text.split("\n", 1)[0]    # `is_generated` judges the first line as the one rule does, not by splitlines
    return lines, cut


def popup_rows(view, status, width):
    """The popup's rows at ``width`` cells: Fleet's status lines, an empty row, then the document.

    ``status`` is the project's Fleet status (``live._repo_status``'s ``status`` for its repo; ``None``
    or empty when Fleet holds none).  Without a document: Fleet's ``fleet.repo.no_status`` word, and the
    place the document would stand when a campaign is known.  The two parts are also read apart
    (``popup_head_rows``, ``popup_doc_rows``): the document needs nothing of Fleet's collection.  A
    document that cannot be read is one dim ``?`` here; the shell's popup reads it apart and answers
    that failure instead (``None`` from ``popup_doc_rows``), so it is neither drawn as an empty status
    nor kept for the next opening."""
    rows = popup_doc_rows(view, width)
    return popup_head_rows(status, width) + ([[("?", "dim")]] if rows is None else rows)


def popup_head_rows(status, width):
    """Fleet's status lines and the empty row under them; no row when Fleet holds no status."""
    status = _clean_all(status) if isinstance(status, Mapping) else {}
    rows = (_fleet_lines(status, max(1, int(width))) + [[]]) if status else []
    return [[(transcript.clean(text), key) for text, key in row] for row in rows]


def popup_doc_rows(view, width):
    """The document's rows at ``width`` cells (``popup_rows`` without Fleet's lines above them).

    ``None`` when the document cannot be read (``read_text`` gave nothing): the caller answers the
    failure rather than drawing a row that looks like a document, and remembers nothing."""
    return popup_doc(view, width)[0]


def popup_doc(view, width, now=None, want_list=False):
    """``(rows, listing)``: the document's rows at ``width`` cells and, when asked, its list screen, from one read.

    ``rows`` is ``None`` when the document cannot be read (see ``popup_doc_rows``).  ``listing`` is ``None`` unless
    ``want_list`` and the document is the generated campaign page (marker on its first line) read whole; then it is
    ``[entry]`` (see ``listing_entries``).  Without ``want_list`` the rows are the ones the popup always drew and no list
    is made at all: the document read whole (``full document ›``) asks this way.  With a list, ``rows`` is a function that makes
    them (the list screen needs none of them: the caller that has to drop the list calls it, nothing else pays for them)."""
    width = max(1, int(width))

    def make(lines, cut):
        rows = _doc_rows_of(view, width, lines, cut)
        return [[(transcript.clean(text), key) for text, key in row] for row in rows]
    if not view.get("has_doc"):
        return make(None, False), None
    lines, cut = read_text(view["path"])
    if lines is None:
        return None, None
    if not want_list or cut or not is_generated(lines):
        return make(lines, cut), None
    now = time.time() if now is None else now
    return (lambda: make(lines, cut)), listing_entries(parse_listing(lines), width, _doc_old(view, now), now)


def _clean_all(value):
    """Every string inside ``value`` (Fleet's status, which carries words from the document) made terminal-safe."""
    if isinstance(value, str):
        return transcript.clean(value)
    if isinstance(value, Mapping):
        return {key: _clean_all(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_all(item) for item in value]
    return value


def _doc_rows_of(view, width, lines, cut):
    """The document's rows at ``width`` (``lines``: what ``read_text`` gave; None without a document)."""
    if lines is None:
        none = ui_text("fleet.repo.no_status")
        place = [(" · " + transcript.clean(view["slot"]), "dim")] if view.get("slot") else []
        return [transcript._cut([(none, "dim")] + place, width)]
    rows = transcript.doc_rows(lines, width)
    if cut:
        rows.append([("… 64 KiB", "dim")])
    return rows


# -- the list screen of the generated campaign document (11-21, design C) ----------------------------------------------
#
# The screen is the document's own words: the head (title, when it was updated, the one line),
# what is in progress (three, then ``all N ›``), every other
# heading as a folded line with its count, and ``full document ›``.  Which heading is which is ``_group_kind``'s reading (no
# new rule); a heading it does not know is a folded group like the rest.  Nothing is invented: a group the document has
# not is not drawn, a bullet is counted as written, a line that is no bullet is the group's note.

CAMPAIGN_MARKER = project_status.CAMPAIGN_MARKER       # utilities/campaign_document.MARKER (a test holds them equal)
RUNNING_FIRST = 3      # in-progress items shown before ``all N ›``
_UPDATED = re.compile(r"^(?:갱신|updated)\s*[:：]?\s*(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", re.IGNORECASE)
_ONE_LINE = re.compile(r"^\*\*(?:한 줄|one line)\*\*\s*[:：]\s*(.*)$", re.IGNORECASE)
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_SLOT = ("  ", None)          # the two cells in front of a chosen-able row: the host writes ``> `` there when it is chosen


def is_generated(lines):
    """Whether the document is the generated campaign page: its first line is the marker."""
    import artifact_campaign_documents as documents
    head = getattr(lines, "head", None)
    return documents.carries_marker(head if head is not None else (lines[0] if lines else ""), CAMPAIGN_MARKER)


def parse_listing(lines):
    """``{title, updated, summary, groups}`` of a generated campaign document's lines.

    ``updated`` is ``(year, month, day, hour, minute)`` or None, ``summary`` the one line's text or ''.  ``groups`` is
    ``[{kind, name, items, notes}]`` in the document's order: ``kind`` is ``running`` (the first heading of that kind, by
    ``_group_kind``) or ``other``; ``name`` the heading as written (markdown taken off); ``items`` the
    top-level bullets' words; ``notes`` the group's other lines (a table row, a rule, a comment and what is indented
    or in a fence are left to the full document).  A heading that waits for the person (``기다리는 것`` ...) is no group
    (11-22): what waits for an answer is a question and stands on the question screens only, so an older generated
    document that still has the section is read without it (its full text is the document's, read whole)."""
    title, updated, summary, groups = "", None, "", []
    current, level, fence, seen = None, None, False, set()
    for raw in lines:
        stripped = str(raw).strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        heading = _HEADING.match(stripped)
        if heading:
            depth, words = len(heading.group(1)), project_status._plain(heading.group(2))
            if depth == 1:
                title = title or words
                current = None
            elif level is None or depth <= level:
                kind = _group_kind(words)
                level = level or depth
                if kind == "waiting":
                    current = None
                    continue
                kind = kind if kind == "running" and kind not in seen else "other"
                seen.add(kind)
                current = {"kind": kind, "name": words, "items": [], "notes": []}
                groups.append(current)
            continue
        if current is None:
            found = _UPDATED.match(stripped)
            if found and updated is None:
                updated = tuple(int(part) for part in found.groups())
            found = _ONE_LINE.match(stripped)
            if found and not summary:
                summary = project_status._plain(found.group(1))
            continue
        if not stripped or raw[:1] in (" ", "\t") or stripped.startswith(("<!--", "|")) or _RULE.match(stripped):
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            text = project_status._plain(bullet.group(1))
            if text:
                current["items"].append(text)
        else:
            current["notes"].append(project_status._plain(stripped))
    return {"title": title, "updated": updated, "summary": summary, "groups": groups}


def _doc_old(view, now):
    """Whether the document is older than Fleet's own limit (``live.STATUS_STALE_DAYS``): the panel's rule."""
    stamp = _mtime(view["path"]) if view.get("path") else None
    try:
        from .explorer.live import STATUS_STALE_DAYS
    except Exception:  # noqa: BLE001 -- the limit is three days wherever Fleet's own cannot be read
        STATUS_STALE_DAYS = 3
    return stamp is not None and now - stamp >= STATUS_STALE_DAYS * 86400


def _wrapped(text, width, rows, key):
    """``text`` wrapped to ``rows`` rows at ``width`` cells; what does not fit ends the last row in ``…``."""
    wrapped = transcript.wrap_spans([(transcript.clean(text), key)], width)
    if len(wrapped) > rows:
        wrapped = wrapped[:rows - 1] + [transcript._cut(wrapped[rows - 1] + [("…", key)], width)]
    return wrapped


def _cut_row(segments, width):
    return transcript._cut([(transcript.clean(text), key) for text, key in segments], width)


def _entry(rows, key=None, group=None, when=None, alt=None, text=None, more=None):
    """One part of the screen.  ``key`` makes it a row the person can choose; ``group`` the heading it belongs to
    (the id of the group the host's open state is kept by); ``when="open"`` shows it only while that group is open;
    ``alt`` the rows shown instead while the group is open; ``text`` (the whole words of an item whose row was cut)
    is what the host sets in place of the cut row, wrapped, while the person has the item open; ``more`` (the backlog's
    indented lines under an item) is the rows that stand under the item's row while the person has it open."""
    entry = {"rows": rows}
    for name, value in (("key", key), ("group", group), ("when", when), ("alt", alt), ("text", text), ("more", more)):
        if value:
            entry[name] = value
    return entry


def _digest(*parts):
    """A short stable name for words: the keys and group ids are made of these, never of the words themselves (a heading
    or an item can be of any length and a document can say the same words twice)."""
    return hashlib.sha256("\0".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def _group_ids(groups):
    """One id per group, in order: the digest of its heading, and ``-N`` for the Nth group that says the same heading (headings of
    the same name are joined in order: the Nth of them is the one the choice and the opened state follow)."""
    seen, ids = {}, []
    for group in groups:
        base = _digest(group["name"])
        seen[base] = seen.get(base, 0) + 1
        ids.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return ids


def _item_key(gid, text, seen):
    """The key of an item: its group's id, the words' own digest, and the order of the same words twice in a group."""
    return f"i:{gid}:{_digest(text)}" + (f":{seen}" if seen > 1 else "")


def listing_entries(parsed, width, old=False, now=None):
    """The list screen at ``width`` cells as entries (see ``_entry``), the host laying them out by its state.

    Every item of a group is there: the screen has no count of its own to stop at, and an answer too big for the popup's
    limit is the caller's to leave out (the popup then draws the document as it always did).  A row that can be chosen
    has a key made of ids (``g:<group>``, ``a:<group>``, ``i:<group>:<words digest>[:N]``, ``full``) and an item
    whose row was cut carries its whole words (``text``)."""
    width = max(8, int(width))
    ui = ui_text
    out = []
    if parsed["title"]:
        out.append(_entry(_wrapped(parsed["title"], width, 2, "sect")))
    stamp = parsed["updated"]
    if stamp:
        year, month, day, hour, minute = stamp
        when = f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
        if year != time.localtime(time.time() if now is None else now).tm_year:
            when = f"{year}-{when}"
        said = ui("fleet.status.popup.updated", when=when)
    else:
        said = ui("fleet.status.popup.updated_unknown")
    # (how long ago is in the title band: the second line says the document's own time, and that it is old)
    said = " · ".join(part for part in (said, ui("fleet.status.popup.old") if old else "") if part)
    out.append(_entry([_cut_row(([("! ", "lvl_y")] if old else []) + [(said, "dim")], width)]))
    if parsed["summary"]:
        out.append(_entry(_wrapped(parsed["summary"], width, 2, "soft")))
    out.append(_entry([[]]))
    # What is in progress first, then every other heading in the document's order.
    ids = _group_ids(parsed["groups"])
    order = sorted(zip(parsed["groups"], ids), key=lambda pair: 0 if pair[0]["kind"] == "running" else 1)
    for group, gid in order:
        name, kind, items = group["name"], group["kind"], group["items"]
        count = len(items)
        head = [(name, "sect"), (f" {count}", "dim")]
        folded = kind == "other"
        lead_rows, lead_open = ([[_SLOT, ("› ", "soft")] + head], [[_SLOT, ("▾ ", "soft")] + head]) if folded else ([[*head]], None)
        out.append(_entry([_cut_row(lead_rows[0], width)], key=("g:" + gid) if folded else None, group=gid if folded else None,
                          alt=[_cut_row(lead_open[0], width)] if lead_open else None))
        taken = {}
        for index, text in enumerate(items):
            seen = taken[text] = taken.get(text, 0) + 1
            # (the words' own digest in the key: the choice follows the same words when the page is read again; the same words
            # twice in a group are told by their order)
            key = _item_key(gid, text, seen)
            lead = [_SLOT] + ([("  ", None)] if folded else [])
            whole = lead + [(transcript.clean(text), "soft")]
            row = _cut_row(whole, width)
            cut = "".join(part for part, _ in row) != "".join(part for part, _ in whole)
            when = "open" if folded or (kind == "running" and index >= RUNNING_FIRST) else None
            # (the group id goes only where it is used: an item that shows whenever the screen does needs none, which is most of the bytes)
            out.append(_entry([row], key=key, group=gid if when else None, when=when, text=transcript.clean(text) if cut else None))
        if kind == "running" and count > RUNNING_FIRST:
            label = ui("fleet.status.popup.all", count=count)
            out.append(_entry([_cut_row([_SLOT, (label + " ›", "soft")], width)], key="a:" + gid, group=gid,
                              alt=[_cut_row([_SLOT, (label + " ▾", "soft")], width)]))
        for note in group["notes"]:
            out.append(_entry([[("    ", None)] + row for row in
                               transcript.wrap_spans([(transcript.clean(note), "dim")], max(8, width - 4))],
                              group=gid, when="open" if folded else None))
        if kind != "other":
            out.append(_entry([[]]))
    if not (out and out[-1]["rows"] == [[]]):
        out.append(_entry([[]]))
    out.append(_entry([_cut_row([_SLOT, (ui("fleet.status.popup.full") + " ›", "soft")], width)], key="full"))
    return out


# -- the panel's two short lists, and the backlog (11-21) ---------------------------------------------------------------
#
# Under the REPO group the panel has a STATUS part and a BACKLOG part: each a head row and the first three items, the rest as
# ``+N`` on the head.  Both are read when the panel reads its status button (one read, in the same worker), and only when the
# document changed (``sign``: its path, modification time and size, which the host sends back with its next ask).
#
# STATUS items are the list screen's own reading: ``parse_listing``'s ``running`` group (what waits for an answer is a question and
# stands on the question screens, never here)
# (``listing_entries`` draws the same groups in the same order); there is no second reading of the page.  BACKLOG is
# ``<artifact root>/backlog.md`` (written by another task; only read here): ``##`` headings are the groups (campaign folder names
# or ``Unassigned``, as written), a top-level bullet is an item, and the indented lines under it are its detail.

BACKLOG_NAME = "backlog.md"
BACKLOG_WORD = "BACKLOG"
BACKLOG_READ_LIMIT = 1 << 20     # bytes of backlog.md read, at most: a longer file is read to here and says there is more (`+`)
BACKLOG_DOC_LIMIT = 65536        # bytes of it the document read whole draws (as the status document's)
PANEL_ITEMS = 3        # items each short list shows (the panel gives them up in its own order)
PANEL_TEXT = 200       # characters of an item's title the panel keeps
TITLE_TEXT = 600       # characters of a title the board's answer carries (to open a cut one in place)
DETAIL_LINES = 12      # indented lines shown under one opened item
DETAIL_ROWS = 3        # rows one indented line takes in an opened item
NOTE_ROWS = 8          # rows its `note:` line takes (the description is what an opened item is read for)
BACKLOG_WORDS = {"updated": "updated", "old": "old document", "none": "No backlog document yet.", "campaign": "campaign"}
# (the words Hearting puts there are English, as the panel's section names are)

# The backlog is read by the one rule the artifact entrance reads it by (core/ARTIFACT_META.md `backlog.md`; `utilities/backlog_items.py`,
# which both call): a job is a top-level `- ` bullet under a `##` heading, outside a code fence; the `갱신` line is read before the first heading;
# the optional indented `kind:`, `state:`, `area:` and `date:` lines (11-26, scheme A) are its kind (`fix`, `task`, `note`; the earlier `improve`
# is `task`; none, another value or two different values is no kind, drawn `TBD`), its state (`discuss`, `ready`, `waiting`; else none, `—`),
# its one area (case and runs of blanks folded; the first name written is the group's name; none or two different is `No area`) and its date
# (`YYYY-MM-DD`, a real one; else none, `—`).  A wrong value leaves that cell undecided and the document opens.
KIND_MARKS = {"fix": "FIX", "task": "TASK", "note": "NOTE", None: "TBD"}
KIND_STYLES = {"fix": "lvl_y", "task": "lvl_g", "note": "dim", None: "dim"}
STATE_MARKS = {"discuss": "Discuss", "ready": "Ready", "waiting": "Waiting", None: "—"}
BOARD_STYLES = ("sect", "dim", "soft", "loc_repo", "lvl_y", "lvl_g")   # the styles the board's rows are painted in (the host paints them)


def _backlog_items():
    import backlog_items
    return backlog_items


def parse_backlog(lines, detail=True):
    """``{title, updated, groups}`` of a backlog document's lines; ``groups`` is ``[{name, items}]`` and an item
    ``{text, kind, state, area, area_key, date, source, note, lines, detail, has_detail, at}`` (``utilities/backlog_items.py``'s job, its title
    and heading with the markdown taken off: ``text`` is ``(empty)`` for a bullet with no words).  ``lines`` (with ``detail``) are the
    indented lines under it, ``(name, value)``; ``detail`` the same as plain text (``date: 2026-11-20``), ``DETAIL_LINES`` of them."""
    parsed = _backlog_items().parse(lines, detail=detail)
    groups = []
    for group in parsed["groups"]:
        heading = _HEADING.match(group["line"])
        items = []
        for job in group["jobs"]:
            # (``detail``: the indented lines as plain text, a line that sets the item's kind left out where it has one)
            shown = [f"{name}: {value}" if name else value for name, value in job["lines"] if not (name == "kind" and job["kind"])]
            items.append(dict(job, text=project_status._plain(job["title"]) or "(empty)",
                              detail=[transcript.clean(project_status._plain(text)) for text in shown][:DETAIL_LINES] if detail else []))
        groups.append({"name": project_status._plain(heading.group(2)) if heading else "", "items": items})
    title = project_status._plain(parsed["title"]) if parsed["title"] else ""
    return {"title": title, "updated": parsed["updated"], "groups": groups}


def _backlog_rows(parsed):
    """``[(campaign, item, key)]`` in the document's order.  A key is ``i:<digest of the campaign and the title>`` (``:N`` for the Nth of the same
    title in the same campaign), so it names one item wherever it is drawn (the panel's rows, the board in any grouping) and whichever the window."""
    out, taken = [], {}
    for group in parsed["groups"]:
        for item in group["items"]:
            digest = _digest(group["name"] + "\n" + item["text"])[:12]
            taken[digest] = taken.get(digest, 0) + 1
            out.append((group["name"], item, f"i:{digest}" + (f":{taken[digest]}" if taken[digest] > 1 else "")))
    return out


def _newest(rows):
    """The rows newest first: the latest date first, the same date in the document's order, no date last."""
    import datetime
    return sorted(rows, key=lambda row: (row[1]["date"] is None, -datetime.date.fromisoformat(row[1]["date"]).toordinal() if row[1]["date"] else 0,
                                         row[1]["at"]))


def backlog_counts(parsed):
    """``{fix, task, note, tbd, discuss, ready, waiting}``: the number of items of each kind (``tbd``: none) and of each state."""
    counts = {"fix": 0, "task": 0, "note": 0, "tbd": 0, "discuss": 0, "ready": 0, "waiting": 0}
    for group in parsed["groups"]:
        for item in group["items"]:
            counts[item.get("kind") or "tbd"] += 1
            if item.get("state"):
                counts[item["state"]] += 1
    return counts


def backlog_total(parsed):
    return sum(len(group["items"]) for group in parsed["groups"])


def _updated_words(parsed, old, now):
    stamp = parsed["updated"]
    if stamp:
        year, month, day, hour, minute = stamp
        when = f"{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
        if year != time.localtime(time.time() if now is None else now).tm_year:
            when = f"{year}-{when}"
        said = f"{BACKLOG_WORDS['updated']} {when}"
    else:
        said = BACKLOG_WORDS["updated"] + ": could not read the time"
    return " · ".join(part for part in (said, BACKLOG_WORDS["old"] if old else "") if part)


def backlog_board(parsed, old=False, now=None, cut=False, limit=None):
    """The board's answer (the BACKLOG popup, 11-26 scheme A): the document's head and one short row per item, newest first; the host groups,
    filters and finds on them (``utilities/session-shell/backlog-board.cjs``) and keeps that state while the board is open.

    ``{title, updated, old, counts, total, sent, cut, campaigns, items}``: an item is ``[key, kind, date, area, state, title, campaign]`` (``kind``
    ``fix``/``task``/``note`` or ``""``, ``date`` ``YYYY-MM-DD`` or ``""``, ``area`` the area's name or ``""``, ``state`` ``discuss``/``ready``/
    ``waiting`` or ``""``, ``campaign`` the index of its heading in ``campaigns``).  ``limit`` is how many items are sent at most (the newest:
    the answer's size decides it, and ``sent`` against ``total`` says how many more are in the document); ``cut``: the file is longer than is
    read (the counts are of what was read)."""
    rows = _newest(_backlog_rows(parsed))
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    campaigns, at = [], {}
    items = []
    for campaign, item, key in rows:
        if campaign not in at:
            at[campaign] = len(campaigns)
            campaigns.append(campaign)
        items.append([key, item["kind"] or "", item["date"] or "", item["area"] or "", item["state"] or "",
                      transcript.clean(item["text"])[:TITLE_TEXT], at[campaign]])
    return {"title": parsed["title"], "updated": _updated_words(parsed, old, now), "old": bool(old), "counts": backlog_counts(parsed),
            "total": backlog_total(parsed), "sent": len(items), "cut": bool(cut), "campaigns": campaigns, "items": items}


def backlog_path(root):
    return os.path.join(root, BACKLOG_NAME)


def read_backlog(root):
    """``(lines, cut)`` of ``<root>/backlog.md`` (the first ``BACKLOG_READ_LIMIT`` bytes, a cut one without its torn last line), or
    ``(None, False)`` when there is none or it cannot be read (a link, a folder, no permission): the same as no document.

    Opened without following a link, as the other documents are; a file replaced while it is read is read once more."""
    from artifact_nofollow import open_relative, open_root
    import stat as stat_
    for _attempt in (0, 1):
        try:
            parent = open_root(root)
            try:
                fd = open_relative(parent, [BACKLOG_NAME])
            finally:
                os.close(parent)
            try:
                before = os.fstat(fd)
                if not stat_.S_ISREG(before.st_mode):
                    return None, False
                chunks, remaining = [], BACKLOG_READ_LIMIT + 1
                while remaining:
                    chunk = os.read(fd, min(remaining, 1 << 16))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except Exception:  # noqa: BLE001 -- an unreadable backlog is no backlog
            return None, False
        if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size):
            break
    raw = b"".join(chunks)
    cut = len(raw) > BACKLOG_READ_LIMIT
    text = raw[:BACKLOG_READ_LIMIT].decode("utf-8", "replace")
    if cut:
        text = text[:text.rfind("\n") + 1] if "\n" in text else ""
    return text.splitlines(), cut


def own_folder(view):
    """The name of the folder this window's status document is in (a backlog group of that name is this window's campaign)."""
    path = view.get("path") if isinstance(view, Mapping) else ""
    return os.path.basename(os.path.dirname(str(path))) if path else ""


def backlog_view(root, own, width, now=None, want_list=False):
    """``(rows, listing, view)`` of the backlog popup at ``width`` cells.  ``rows`` is a function when ``listing`` is made (as
    ``popup_doc``'s); ``listing`` is a function of the number of items to draw (``listing.total`` items in the document) so the
    caller can fit the answer's size; ``view`` is ``{has_doc, title, age}``.  No document (or one that cannot be read): one dim row, no list."""
    now = time.time() if now is None else now
    lines, cut = read_backlog(root) if root else (None, False)
    if lines is None:
        row = [[(BACKLOG_WORDS["none"], "dim")]]
        return row, None, {"has_doc": False, "title": BACKLOG_WORD, "age": ""}
    stamp = _mtime(backlog_path(root))
    age = _age(stamp, now)
    view = {"has_doc": True, "title": BACKLOG_WORD + (" · " + age if age else ""), "age": age}

    def make():
        text = "\n".join(lines)
        part = len(text.encode("utf-8")) > BACKLOG_DOC_LIMIT
        if part:
            text = text.encode("utf-8")[:BACKLOG_DOC_LIMIT].decode("utf-8", "ignore")
            text = text[:text.rfind("\n") + 1] if "\n" in text else ""
        rows = transcript.doc_rows(text.splitlines(), max(1, int(width)))
        if part or cut:
            rows.append([("… 64 KiB", "dim")])
        return [[(transcript.clean(t), key) for t, key in row] for row in rows]
    if not want_list:
        return make(), None, view
    old = stamp is not None and now - stamp >= 3 * 86400
    try:
        from .explorer.live import STATUS_STALE_DAYS
        old = stamp is not None and now - stamp >= STATUS_STALE_DAYS * 86400
    except Exception:  # noqa: BLE001 -- three days wherever Fleet's own cannot be read
        pass
    parsed = parse_backlog(lines, detail=False)

    def listing(limit=None):
        return backlog_board(parsed, old, now, cut, limit)
    listing.total = backlog_total(parsed)
    listing.board = True
    return make, listing, view


def backlog_detail(root, own, key, width, indent=0):
    """The rows of one item opened on the board (``None`` where the document or the item is not there): what the host asks for when the
    person opens it (the board's answer carries titles only), ``indent`` cells in (under the board's date column):

        2026-11-16 · FIX · Panel · Waiting        the date, kind, area and state (``—``, ``No area`` where there is none)
        campaign 2026-11-02_v4-open               the ``##`` heading the item stands under
        source …  /  note …                       its other indented lines, as written (a ``note:`` line wraps to ``NOTE_ROWS`` rows)

    Only that one item is read; ``own`` is not used (the key names the item whichever the window)."""
    lines, _cut = read_backlog(root) if root else (None, False)
    if lines is None:
        return None
    parsed = parse_backlog(lines, detail=True)
    width = max(8, int(width))
    indent = max(0, min(40, int(indent or 0)))
    room = max(8, width - indent)
    for campaign, item, item_key in _backlog_rows(parsed):
        if item_key != key:
            continue
        lead = [(" " * indent, None)] if indent else []
        out = [lead + [(item["date"] or "—", "soft"), (" · ", "dim"), (KIND_MARKS[item["kind"]], KIND_STYLES[item["kind"]]), (" · ", "dim"),
                       (item["area"] or "No area", "soft" if item["area"] else "dim"), (" · ", "dim"),
                       (STATE_MARKS[item["state"]], "soft" if item["state"] else "dim")]]
        out = [_cut_row(out[0], width)]
        shown = [(BACKLOG_WORDS["campaign"], campaign or "—")] + [(name, value) for name, value in item["lines"] if name not in ("kind", "state", "area", "date")]
        for name, value in shown[:1 + DETAIL_LINES]:
            text = transcript.clean(project_status._plain(value) if value else "")
            rows = _wrapped((name + " " + text) if name else text, room, NOTE_ROWS if name == "note" else DETAIL_ROWS, "soft")
            if name and rows and rows[0] and rows[0][0][0].startswith(name + " "):
                # (the line's name dim, its value in the body's colour)
                first, style = rows[0][0]
                rows[0] = [(name + " ", "dim")] + ([(first[len(name) + 1:], style)] if first[len(name) + 1:] else []) + rows[0][1:]
            out.extend(lead + row for row in rows)
        return out
    return None


def panel_items(parsed, first=PANEL_ITEMS):
    """``(items, total)`` of the status list: what is in progress, the list screen's own order.

    ``parsed`` is ``parse_listing``'s answer.  Each item is ``{mark, text, key}``: ``mark`` is '' (the host draws the dim ``·``);
    ``key`` is the line's key on the list screen (a press on the item stands on it there).
    ``total`` counts every item of the ``running`` group; the recent and folded groups are not counted."""
    ids = _group_ids(parsed["groups"])
    items, total = [], 0
    for group, gid in zip(parsed["groups"], ids):
        if group["kind"] != "running":
            continue
        taken = {}
        for text in group["items"]:
            seen = taken[text] = taken.get(text, 0) + 1
            total += 1
            if len(items) < first:
                items.append({"mark": "", "text": transcript.clean(text)[:PANEL_TEXT], "key": _item_key(gid, text, seen)})
    return items, total


def panel_backlog_items(parsed, own, first=PANEL_ITEMS):
    """``(items, total)`` of the backlog for the panel's three rows (11-26, scheme A): the newest item in Discuss first, then the other newest
    ones (the latest date first, the same date in the document's order, no date last); with nothing in Discuss the newest three.

    Each item is ``{mark, kind, state, area, date, text, key}``: ``kind`` is ``fix``, ``task``, ``note`` or ``tbd``; ``state`` ``discuss``,
    ``ready``, ``waiting`` or ``""``; ``area`` the area's name or ``""``; ``date`` ``YYYY-MM-DD`` or ``""`` (the panel's drawing takes the
    four values as they are); ``key`` is the item's key on the board.  ``own`` is not used: the order is the dates'."""
    rows = _newest(_backlog_rows(parsed))
    first_talk = next((row for row in rows if row[1]["state"] == "discuss"), None)
    chosen = ([first_talk] + [row for row in rows if row is not first_talk])[:first] if first_talk else rows[:first]
    items = [{"mark": "", "kind": item["kind"] or "tbd", "state": item["state"] or "", "area": item["area"] or "", "date": item["date"] or "",
              "text": transcript.clean(item["text"])[:PANEL_TEXT], "key": key} for _campaign, item, key in chosen]
    return items, backlog_total(parsed)


def _sign(path):
    """A document's identity for the read-again test: its path, modification time (ns), size, inode and change time (ns), as text; None
    where there is none.  (The inode and the change time catch a same-size rewrite within one tick of a coarse clock, on a network
    file system, and a rename over the file.)"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return f"{path}|{info.st_mtime_ns}|{info.st_size}|{info.st_ino}|{info.st_ctime_ns}"


def panel_lists(cwd, view, have=None, now=None):
    """The panel's two short lists, ``{"status": …, "backlog": …}`` (``None`` where the folder has no artifact root).

    Each is ``{"same": True}`` where the document is the one ``have`` (the signs the host holds, ``{"status": s, "backlog": s}``)
    names, else ``{"sign", "items", "total"}`` read from it (``has_doc`` for the backlog: it is read and readable).  A
    document that did not change is not read or parsed again.  Never raises: a list that cannot be read has no items."""
    try:
        now = time.time() if now is None else now
        root = _root(cwd, now)
        if not root:
            return None
        have = have if isinstance(have, Mapping) else {}
        out = {}
        path = view.get("path") if isinstance(view, Mapping) else ""
        sign = _sign(path) if path else None
        if sign is not None and have.get("status") == sign:
            out["status"] = {"same": True}
        else:
            items, total = [], 0
            lines, cut = read_text(path) if sign is not None else (None, False)
            if lines is not None and not cut and is_generated(lines):
                items, total = panel_items(parse_listing(lines))
            out["status"] = {"sign": sign, "items": items, "total": total}
        own = own_folder(view)
        sign = _sign(backlog_path(root))
        # (the sign names this window's folder too, as the read is asked for it; since 11-26 the order is the dates', the same for every folder)
        sign = sign and f"{sign}|{own}"
        if sign is not None and have.get("backlog") == sign:
            out["backlog"] = {"same": True}
        else:
            items, total, has, more, counts = [], 0, False, False, None
            lines, more = read_backlog(root) if sign is not None else (None, False)
            if lines is not None:
                has = True
                parsed = parse_backlog(lines, detail=False)
                items, total = panel_backlog_items(parsed, own)
                counts = backlog_counts(parsed)
            out["backlog"] = {"sign": sign, "has_doc": has, "items": items, "total": total, "more": more is True, "counts": counts}
        return out
    except Exception:  # noqa: BLE001 -- the panel draws on without its lists
        return None
