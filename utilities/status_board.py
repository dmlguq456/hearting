#!/usr/bin/env python3
"""A route's close refreshes its campaign's status page, when the campaign keeps one (C-W4).

`core/ARTIFACT_META.md` (`현황.md` / `STATUS.md`): one optional page per campaign, directly
under the campaign, with legacy cycle `artifacts/` pages as fallback. Writing one stays optional and
nothing waits on it. When the closed route's campaign keeps one, the close refreshes two
parts the runtime can state from what it saw, and nothing else:

- the `갱신` / `Updated` stamp's date and time, when the page has that line;
- a `## 최근 마감` / `## Recently closed` section, newest first, at most five lines,
  each naming when, what (the route's sealed intent, else its slug) and the route,
  with any PR link from the close summary. Only a close whose terminal gates are
  proven counts; a cancelled or unproven close leaves the page alone.

The one line, `## 결정 필요` / `## Decisions needed` and `## 다음` / `## Next` are the
person's judgment and stay as written. A page is never created, a symlinked or unreadable
page is left alone, and any failure leaves the page as it was: a close never waits on it.
A PR merge happens on GitHub, with no Hearting event to attach to.
"""
from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from artifact_campaign_documents import READ_LIMIT, campaign_status
from artifact_nofollow import open_relative, open_root

KEEP = 5
LOCK_WAIT_SECONDS = 2.0
_STAMP = re.compile(r"^(?P<word>갱신|Updated)(?P<gap>\s+)\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
                    re.IGNORECASE)
_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_SECTIONS = {"ko": "## 최근 마감", "en": "## Recently closed"}
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_OURS = re.compile(r"^- \d{4}-\d{2}-\d{2} \d{2}:\d{2} · .* · route rt-[0-9a-f]{16}(?: · .*)?$")
_HANGUL = re.compile(r"[가-힣]")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def campaign_dir(root: Path, route) -> Path | None:
    """The route's active campaign folder, found by the existing resolver route autoclose uses."""
    try:
        import route_autoclose
        found = route_autoclose.campaign_of_route(Path(root), route)
    except Exception:  # noqa: BLE001 -- no campaign found means no page to refresh
        return None
    return Path(found[1]) if found else None


def status_page(campaign: Path) -> Path | None:
    """The same canonical/ambiguous/legacy choice as the common read entrance."""
    from artifact_campaign_documents import campaign_status
    return campaign_status(campaign)['path']


def closure_line(route, outcome, now: datetime, *, journal_entry=None, page=None, language="en") -> str:
    what = ""
    try:
        import work_intent
        what = (work_intent.route_intent(route) or {}).get("intent") or ""
    except Exception:  # noqa: BLE001 -- the slug stands in
        what = ""
    what = " ".join((what or route.get("slug") or route.get("capability") or "").split())
    if len(what) > 90:
        what = what[:89] + "…"
    parts = [now.strftime("%Y-%m-%d %H:%M"), what, f"route {route.get('route_id')}"]
    prs = list(dict.fromkeys(_PR_URL.findall(str((outcome or {}).get("summary") or ""))))[:2]
    links = []
    try:
        import artifact_journal as journal
        import inline_finish
        root = Path(route["artifact_root"])
        if journal_entry is None:
            slot = inline_finish.pending_state(root, route["route_id"]) or {}
            refs = (slot.get("intent") or {}).get("entries") or []
            journal_entry = ({**journal.read_reference(refs[0]["path"]), "reference": refs[0]["path"]}
                             if slot.get("schema") == "inline_finish_v2" and refs else None)
        if journal_entry:
            value = journal.read_reference(journal_entry["reference"])
            path, aid, rid = journal.split_reference(journal_entry["reference"])
            relative = os.path.relpath(path, Path(page).parent) if page else str(path)
            record_label, result_label = (("기록 보기", "결과물 열기") if language == "ko" else ("View record", "Open result"))
            links.append(f"[{record_label}](<{relative}#{aid}-{rid}>)")
            for result in journal.result_targets(root, value)[:1]:
                if result.get("availability") == "available":
                    links.append(f"[{result_label}](<{result['href']}>)")
    except (OSError, ValueError, KeyError, TypeError):
        pass  # historical cycle rows keep their original text
    return "- " + " · ".join(part for part in parts + prs + links if part)


def _ours(row: str) -> bool:
    return _OURS.match(row) is not None


def refreshed(text: str, line: str, now: datetime) -> str:
    """`text` with its stamp set to `now` and `line` first among the runtime's closure lines.

    Only the stamp's date and time and the runtime's own lines (`_OURS`) change; every other
    byte -- the person's notes, sub-bullets or paragraphs inside the section included -- stays
    where it was. Past `KEEP`, the oldest runtime lines go."""
    lines = text.split("\n")
    first_section = next((i for i, row in enumerate(lines) if _HEADING.match(row)), len(lines))
    for index in range(first_section):
        match = _STAMP.match(lines[index])
        if match:
            lines[index] = (match["word"] + match["gap"] + now.strftime("%Y-%m-%d %H:%M")
                            + lines[index][match.end():])
            break
    language = "ko" if _HANGUL.search(text) else "en"
    titles = {_fold(value[3:]) for value in _SECTIONS.values()}
    start = next((i for i, row in enumerate(lines)
                  if _HEADING.match(row) and _fold(_HEADING.match(row)["title"]) in titles), None)
    if start is None:
        head = "\n".join(lines)
        gap = "" if not head else ("\n" if head.endswith("\n") else "\n\n")
        return head + gap + _SECTIONS[language] + "\n" + line + "\n"
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])
                or lines[i].startswith("# ")), len(lines))
    section = lines[start + 1:end]
    if line in section:
        return "\n".join(lines)
    ours = [i for i, row in enumerate(section) if _ours(row)]
    at = ours[0] if ours else 0
    section.insert(at, line)
    drop = set([i + 1 if i >= at else i for i in ours][KEEP - 1:])
    section = [row for i, row in enumerate(section) if i not in drop]
    return "\n".join(lines[:start + 1] + section + lines[end:])


def note_route_closed(route, outcome, *, now: datetime | None = None, journal_entry=None) -> Path | None:
    """Refresh the closed route's campaign status page; the page path, or None when untouched."""
    if not isinstance(outcome, dict) or outcome.get("terminal_gate_proven") is not True:
        return None                       # a cancelled or unproven close is not finished work
    root = Path(str(route.get("artifact_root") or "")).resolve()
    if not root.is_dir():
        return None
    campaign = campaign_dir(root, route)
    if campaign is None:
        return None
    now = now or datetime.now()
    root_fd = runtime_fd = None
    try:
        # Select again under the existing lock. A canonical conflict never
        # becomes a legacy write, including while this close was waiting.
        selected = campaign_status(campaign)
        if selected['path'] is None:
            return None
        root_fd = open_root(root)
        try:
            os.mkdir('.runtime', mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        runtime_fd = open_relative(root_fd, ['.runtime'])
        lock_fd = os.open('status-board.lock', os.O_WRONLY | os.O_APPEND | os.O_CREAT |
                          os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=runtime_fd)
        with os.fdopen(lock_fd, 'a') as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                return None
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        return None
                    time.sleep(0.05)
            selected = campaign_status(campaign)
            page = selected['path']
            if page is None or (page.parent == campaign and len(selected['candidates']) > 1):
                # Updating just one identical copy would create a conflict.
                return None
            return _refresh_page(root_fd, root, campaign, page, route, outcome, now, journal_entry)
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return None  # optional status never blocks the already proven close
    finally:
        if runtime_fd is not None:
            os.close(runtime_fd)
        if root_fd is not None:
            os.close(root_fd)


def _refresh_page(root_fd, root, campaign, page, route, outcome, now, journal_entry):
    parts = page.relative_to(root).parts
    parent = open_relative(root_fd, parts[:-1])
    temporary = None
    try:
        fd = open_relative(parent, [parts[-1]])
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            raw = handle.read(READ_LIMIT + 1)
        if len(raw) > READ_LIMIT:
            return None
        text = raw.decode('utf-8')
        after = refreshed(text, closure_line(route, outcome, now, journal_entry=journal_entry,
            page=page, language='ko' if _HANGUL.search(text) else 'en'), now)
        if after == text:
            return page
        temporary = '.status-' + secrets.token_hex(8)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     before.st_mode & 0o7777, dir_fd=parent)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(after)
            handle.flush()
            os.fchmod(handle.fileno(), before.st_mode & 0o7777)
            os.fsync(handle.fileno())
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_mtime_ns, item.st_ctime_ns, item.st_size)
        if signature(current) != signature(before) or status_page(campaign) != page:
            return None
        current_parent = open_relative(root_fd, parts[:-1])
        try:
            a, b = os.fstat(parent), os.fstat(current_parent)
            if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                return None
        finally:
            os.close(current_parent)
        os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
        return page
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)


def note_journal_entry(root: Path, entry):
    """A later automatic observation supplements an already closed record.

    Its actual existing outcome supplies route authority. Saved observations
    never invent a test/task PASS or create a status page.
    """
    import json
    import artifact_journal
    path = artifact_journal.safe_path(root, root / ".runtime/routes" / (entry["route_id"] + ".json"), missing=False)
    route = json.loads(path.read_text())
    outcome = json.loads(path.with_suffix(".outcome.json").read_text())
    return note_route_closed(route, outcome, journal_entry=entry,
                             now=datetime.fromisoformat(entry["at"].replace("Z", "+00:00")))
