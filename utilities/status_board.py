#!/usr/bin/env python3
"""A route's close refreshes its campaign's status page, when the campaign keeps one (C-W4).

`core/ARTIFACT_META.md` (`현황.md` / `STATUS.md`): one optional page per campaign, directly
under a cycle's `artifacts/`, that a person reads first. Writing one stays optional and
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
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

NAMES = ("현황.md", "status.md")             # NFC-normalized, case-folded; first wins
KEEP = 5
LOCK_WAIT_SECONDS = 2.0
_STAMP = re.compile(r"^(?P<word>갱신|Updated)(?P<gap>\s+)\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?",
                    re.IGNORECASE)
_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_SECTIONS = {"ko": "## 최근 마감", "en": "## Recently closed"}
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_HANGUL = re.compile(r"[가-힣]")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def campaign_dir(root: Path, key: str) -> Path | None:
    """The active campaign folder sealed under `key`, read from its own `campaign.json`."""
    if not key or "/" in key or key.startswith("."):
        return None
    campaigns = Path(root) / "campaigns"
    found = []
    for candidate in campaigns.glob("*_" + key):
        try:
            record = json.loads((candidate / "campaign.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("key") == key and record.get("state", "active") == "active":
            found.append(candidate)
    return found[0] if len(found) == 1 else None


def status_page(campaign: Path) -> Path | None:
    """The campaign's newest status page directly under one of its cycles' `artifacts/`."""
    pages = []
    try:
        cycles = [entry for entry in os.scandir(campaign) if entry.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for cycle in cycles:
        artifacts = Path(cycle.path) / "artifacts"
        try:
            names = {_fold(entry.name): entry for entry in os.scandir(artifacts)}
        except OSError:
            continue
        for name in NAMES:
            entry = names.get(name)
            if entry is not None and entry.is_file(follow_symlinks=False):
                pages.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
                break
    return max(pages)[1] if pages else None


def closure_line(route, outcome, now: datetime) -> str:
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
    return "- " + " · ".join(part for part in parts + prs if part)


def refreshed(text: str, line: str, now: datetime) -> str:
    """`text` with its stamp set to `now` and `line` first in the recent-closures section."""
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
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", _SECTIONS[language], line, ""]
        return "\n".join(lines)
    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])
                or lines[i].startswith("# ")), len(lines))
    kept = [row for row in lines[start + 1:end] if row.startswith("- ") and row != line]
    body = [line] + kept[:KEEP - 1]
    return "\n".join(lines[:start + 1] + body + [""] + lines[end:]).rstrip("\n") + "\n"


def note_route_closed(route, outcome, *, now: datetime | None = None) -> Path | None:
    """Refresh the closed route's campaign status page; the page path, or None when untouched."""
    if not isinstance(outcome, dict) or outcome.get("terminal_gate_proven") is not True:
        return None                       # a cancelled or unproven close is not finished work
    root = Path(str(route.get("artifact_root") or ""))
    key = route.get("campaign_key")
    if not root.is_dir() or not isinstance(key, str):
        return None
    campaign = campaign_dir(root, key)
    page = status_page(campaign) if campaign is not None else None
    if page is None:
        return None
    now = now or datetime.now()
    lock_path = root / ".runtime" / "status-board.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return None           # another close is writing it; this one stays off the page
                time.sleep(0.05)
        try:
            before = page.stat(follow_symlinks=False)
            text = page.read_text(encoding="utf-8")
            after = refreshed(text, closure_line(route, outcome, now), now)
            if after == text:
                return page
            fd, temporary = tempfile.mkstemp(prefix=".status-", dir=page.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(after)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, before.st_mode & 0o7777)
                current = page.stat(follow_symlinks=False)
                if (current.st_mtime_ns, current.st_size) != (before.st_mtime_ns, before.st_size):
                    return None           # the person saved it meanwhile: their edit stands
                os.replace(temporary, page)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return page
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
