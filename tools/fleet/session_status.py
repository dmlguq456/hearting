"""The shell panel's status button and popup: which status document this session shows, and its lines.

``read(cwd, route_ids)`` is the panel's one value (``None`` when the folder has no artifact root: no
button then).  The document is the one of this session's own campaign (route -> its record's campaign
key -> ``route_autoclose.campaign_of_route`` -> the campaign folder -> ``artifact_campaign_documents.
campaign_status``, the selection Fleet, ``status_board`` and the entrance share); a session whose
routes span several campaigns shows the most recently changed document among them (Fleet's own rule).
A session with no route, no campaign key, a campaign Fleet does not list (closed, or not yet named) or
a campaign without a document shows the document Fleet's project line picks (``project_status.collect``).
No document at all is still a value (``has_doc`` False: the button stands, dim) with the place a
document would stand.  ``watch`` is for the panel's existing file watch (no poller): the document, and
each session campaign's folder as one folder target (a trailing separator) so a new document redraws.

``groups`` (11-20, the panel's STATUS section) are the document's lists read from its first 64 KiB: what is in
progress, what waits for the person, what ended lately, what comes later, each its name, its count and its first
titles (``read_groups``; a document with none of those headings gives ``[]`` and the panel draws no section).  They
are read with the button's value (each read of the value reads the document's first 64 KiB once; nothing is kept between reads).

``held(cwd, claim)`` is the value the host already holds (its button's) taken for the popup, so the popup
does not read what the button read: it stands only where the claim names a status document inside this
folder's own artifact root (else ``None``, and the popup reads).

``popup_rows(view, status, width)`` is the popup's body: Fleet's status lines for this project exactly as
its project page draws them (``pages.repo_status_lines``), an empty row, then the whole document
(``transcript.doc_rows``: Fleet cards, fences as written, markdown tables aligned).  Rows are lists of
``(text, key)`` segments; ``key`` is a Fleet palette key (``None`` for padding).  Nothing here writes,
asks, gates or starts a process; ``project_status``, ``artifact_campaign_documents`` and
``status_board`` are called, not changed.
"""
from collections.abc import Mapping
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


def _group_kind(title):
    lowered = title.casefold().lstrip()
    if any(word in lowered for word in project_status._RECORD_WORDS):
        return None
    for kind, words in _GROUP_WORDS:
        if any(_begins(lowered, word) for word in words):
            return kind
    return None


def parse_groups(text):
    """The lists of a status document as ``[{kind, name, short, count, items}]`` in the panel's order.

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
        groups.append({"kind": kind, "name": ui_text("fleet.status.group." + kind),
                       "short": ui_text("fleet.status.group." + kind + ".short"), "count": len(titles), "items": items})
    return groups


def read_groups(path):
    """``parse_groups`` of the document's first 64 KiB; ``[]`` if unreadable."""
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
    (paths for the panel's file watch), ``groups`` (the document's lists, above; ``[]`` without one)."""
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
        watch = ([path] if path else []) + [folder.rstrip(os.sep) + os.sep for folder in folders]
        return {"word": word, "has_doc": path is not None, "campaign": campaign, "age": age,
                "title": _title(word, campaign if path else "", age), "path": path or "",
                "slot": slot, "watch": list(dict.fromkeys(watch)), "groups": read_groups(path) if path else []}
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
    return text.splitlines(), cut


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
    rows = _doc_rows(view, max(1, int(width)))
    if rows is None:
        return None
    return [[(transcript.clean(text), key) for text, key in row] for row in rows]


def _clean_all(value):
    """Every string inside ``value`` (Fleet's status, which carries words from the document) made terminal-safe."""
    if isinstance(value, str):
        return transcript.clean(value)
    if isinstance(value, Mapping):
        return {key: _clean_all(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_all(item) for item in value]
    return value


def _doc_rows(view, width):
    """The document's rows at ``width``; ``None`` when it cannot be read (no row to mistake for one)."""
    if not view.get("has_doc"):
        none = ui_text("fleet.repo.no_status")
        place = [(" · " + transcript.clean(view["slot"]), "dim")] if view.get("slot") else []
        return [transcript._cut([(none, "dim")] + place, width)]
    lines, cut = read_text(view["path"])
    if lines is None:
        return None
    rows = transcript.doc_rows(lines, width)
    if cut:
        rows.append([("… 64 KiB", "dim")])
    return rows
