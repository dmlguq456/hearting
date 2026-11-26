#!/usr/bin/env python3
"""Writes and keeps each campaign's own document, `campaigns/<campaign>/campaign.md`.

The entrance refresh worker calls `catch_up` before it writes the entrance: code, not a
session, makes and edits the document (no model call, no resident process, no new command).
A campaign's document has two sections, in this order: `## 하는 중`, `## 최근에 끝난 것`. What waits for
a person's answer is a registered question and stands on the question screens only; the document says
nothing about it. One piece of work is one line: routes that carry the same PR number are
joined (`link_units`), a route with no number is its own line. A route that closed with code but no number is
joined to its PR by git ancestry (`_GitPass`: read only, no fetch). Each line's tail `기록 N` links to the
record page of that work, `records/<key>.md`, which the same pass writes for the lines on the page.

Three layers:

- pure functions that read no file, clock or environment (`pr_from_slug`, `pr_from_report`,
  `publish_facts`, `route_kind`, `link_units`, `render_document`);
- reads without writes (`collect`; `collect(persist=False)` draws a document from
  real records and touches nothing);
- one writer, `catch_up`, which only creates `campaign.md` (and `records/<key>.md`) or replaces a file whose
  first line is its marker (`MARKER`, `RECORD_MARKER`). A same-named file without the marker, a link, a
  directory, and the hand-written `현황.md` / `status.md` are never changed, and nothing is ever deleted.

Cost: routes are not read one by one. Listing the routes folder and the cycle folder with `stat`
picks what changed since the last pass; only the cycle records written after it (about 1 KB each)
and the small close results are read, and no index is rebuilt (only a close that no saved list holds
is looked up by route id). What was read is kept in
`<root>/.runtime/campaign-document/<campaign_id>.json`; deleting it only starts the campaign
again from its newest 20 pieces of work (nothing is backfilled).

A piece of work with a running registered attempt on any of its open routes stays under `## 하는 중`
whatever its age: "running" is the runtime's own liveness of an attempt row (`route_autoclose.live_routes`,
the verdict the route sweep uses), read once per pass and only for the open routes, and the attempt's
last write to its log is that work's movement too. An attempt that ended, or whose process is gone,
counts for nothing, and a replacement attempt of the same route counts as the route's own.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_producer_store as store  # noqa: E402
from artifact_campaign_documents import carries_marker  # noqa: E402

MARKER = "<!-- hearting:campaign-document v1 -->"
DOCUMENT = "campaign.md"
RECORD_MARKER = "<!-- hearting:campaign-record v1 -->"
RECORDS_DIR = "records"   # beside `campaign.md`; the same name as `artifact_locator.CAMPAIGN_RECORDS_DIR`
GIT_TIMEOUT = 5
GIT_BUDGET = 40
VERDICT_CHARS = 60
RECORD_KEY_CHARS = 200
UNPROVEN_WORD = "미증명"   # the head of a page of work whose close was not proven: it ended, and passing is not shown
ACTIVE_SECONDS = 6 * 3600
DONE_LINES = 20
READ_CAP = 400
FIRST_WINDOW_NS = 10 * 60 * 10 ** 9
WATERMARK_SLACK_NS = 2 * 10 ** 9
REPORT_LINES = 30
PUBLISH_LINES = 80
HEAD_BYTES = 16 * 1024
TITLE_CHARS = 90
SIZE_LIMIT = 60 * 1024
HEADING_CHARS = 200
SUMMARY_CHARS = 400
CACHE_REL = Path(".runtime") / "campaign-document"

_ROUTE_FILE = re.compile(r"^(rt-[0-9a-f]+)\.json$")
_OUTCOME_FILE = re.compile(r"^(rt-[0-9a-f]+)\.outcome\.json$")
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_PR_TOKEN = re.compile(r"^(pr)?(\d{3,5})$", re.IGNORECASE)
_MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$")
_REPORT_PR = re.compile(r"PR\s*[:：]?\s*#(\d+)")
_PUBLISH_HEAD = re.compile(r"^#\s+(v\d+\.\d+\.\d+\S*)\s*[—–-]\s*(.*)$")
_PR_NUMBER = re.compile(r"#(\d+)")
_REVIEW_SLUG = re.compile(r"^(rv|review|audit)[-_]|[-_]close[-_]unit$")
_UPDATED_LINE = re.compile(r"^갱신 \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")   # C0, C1 (not tab or line breaks) and unpaired surrogates, which UTF-8 cannot write
_INTENT_PR = re.compile(r"(?<![A-Za-z])PR\s*#(\d{1,5})(?!\d)")
_RECORD_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")   # a viewer shows a name that starts with `.` as missing
_VERDICT = re.compile(r"최종\s*판정\s*[:：]\s*(.+)")
_HEAD_TAIL = re.compile(r"\s*[—–]\s*head\b.*$", re.IGNORECASE)
_MERGED_PR = re.compile(r"^Merge pull request #(\d+)(?=\s|$)")
_COMMIT = re.compile(r"[0-9a-f]{40}")   # the only shape of commit that is handed to git
_LINK_UNSAFE = re.compile(r"[<>\\\r\n\x00]")


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------


def pr_from_slug(slug):
    """The one PR number a route's slug names, else None.

    Only whole alphanumeric tokens of 3-5 digits count, with an optional `pr` prefix. A
    four-digit token without the prefix that reads as a month and day is a date, not a PR."""
    found = set()
    for token in _TOKEN.findall(str(slug or "")):
        match = _PR_TOKEN.match(token)
        if not match:
            continue
        number = match.group(2)
        if match.group(1) is None and len(number) == 4 and _MONTH_DAY.match(number):
            continue
        found.add(int(number))
    return found.pop() if len(found) == 1 else None


def pr_from_report(text):
    """The one PR number named (`PR #N`, `PR: #N`) in a report's first lines, else None."""
    head = "\n".join(str(text or "").split("\n")[:REPORT_LINES])
    found = {int(number) for number in _REPORT_PR.findall(head)}
    return found.pop() if len(found) == 1 else None


def publish_facts(text):
    """(version, [PR numbers]) from a publish report: its `# vX.Y.Z — #N` first line and the
    `병합된 PR:` lines in its first lines."""
    lines = str(text or "").split("\n")[:PUBLISH_LINES]
    version, numbers = None, []
    first = next((line for line in lines if line.strip()), "")
    match = _PUBLISH_HEAD.match(first.strip())
    if match:
        version = match.group(1)
        numbers += [int(n) for n in _PR_NUMBER.findall(match.group(2))]
    for line in lines:
        if "병합된 PR:" in line:
            numbers += [int(n) for n in _PR_NUMBER.findall(line)]
    return version, list(dict.fromkeys(numbers))


def route_kind(capability, slug):
    """`frame` (does no work), `publish`, `review` or `work`."""
    capability, slug = str(capability or ""), str(slug or "")
    if capability == "route-frame":
        return "frame"
    if capability == "autopilot-ship" or slug.startswith("publish-"):
        return "publish"
    if capability == "audit" or _REVIEW_SLUG.search(slug):
        return "review"
    return "work"


def _start(member):
    return member.get("st") if isinstance(member.get("st"), (int, float)) else float("inf")


def record_key(pr, locator):
    """The name of a piece of work's record page (`records/<key>.md`): `pr-<N>` when it has a PR, else the
    folder name of the cycle its first route stands on (`_choose`: a sealed one, else the latest started);
    None when the name has a character a file name or link should not."""
    if pr:
        return f"pr-{int(pr)}"
    locator = locator if isinstance(locator, str) else ""
    if len(locator) <= RECORD_KEY_CHARS and _RECORD_KEY.fullmatch(locator):
        return locator
    return None


def link_units(facts, open_routes, titles, *, now, closed_total=None):
    """Join routes into pieces of work and sort them into the document's lines.

    `facts`: closed routes (`rid`, `kind`, `slug`, `cy`, `loc`, `st`, `c`, `ok`, `pr`, `prs`, `v`, `t`).
    `open_routes`: routes with no close result (`rid`, `kind`, `slug`, `cy`, `loc`, `st`, `pr`,
    `activity`, `alive`: a registered attempt of it is running). `titles`: cycle id -> title from the campaign's meta. A route's PR number joins
    it to the other routes of that number; without one it stands alone. A piece with a running
    attempt on any of its open routes is doing whatever its age. Frames do no work and a
    close that is not proven is not finished work: neither is a line or a count. Publish routes
    have no line of their own; they add their version and a record to the PRs they name.

    A piece of work that carries a published version is finished even when routes of its PR are still
    open, unless one of them is work (not a review) that started after the publish closed: that is the
    next piece. A review route left open by a blocking verdict or an attention is not "doing". Such a
    piece is listed by when its latest close happened, else by when its publish closed.
    """
    units = {}

    def unit(member):
        key = f"pr:{member['pr']}" if member.get("pr") else f"rt:{member['rid']}"
        return units.setdefault(key, {"key": key, "pr": member.get("pr"), "closed": [], "open": [], "pubs": []})

    publishes = []
    for fact in facts:
        if fact.get("kind") == "frame" or not fact.get("ok"):
            continue
        if fact.get("kind") == "publish":
            publishes.append(fact)
        else:
            unit(fact)["closed"].append(fact)
    for route in open_routes:
        if route.get("kind") in ("frame", "publish"):
            continue
        unit(route)["open"].append(route)
    for publish in publishes:
        for number in publish.get("prs") or []:
            if f"pr:{number}" in units:
                units[f"pr:{number}"]["pubs"].append(publish)

    def finish(item):
        members = item["closed"] + item["open"]
        worked = [m for m in members if m.get("kind") != "review"] or members
        impl = min(worked, key=lambda m: (_start(m), m["rid"]))
        versions = [p for p in item["pubs"] if p.get("v")]
        version = max(versions, key=lambda p: (p.get("c") or 0, p["rid"]))["v"] if versions else None
        title = (titles or {}).get(impl.get("cy")) or impl.get("t") or None
        closed_at = max((m.get("c") or 0 for m in item["closed"]), default=0)
        if not closed_at and versions:
            closed_at = max((p.get("c") or 0 for p in versions), default=0)
        return {"key": item["key"], "pr": item["pr"], "v": version, "title": title, "impl": impl,
                "count": len(members) + len(item["pubs"]),
                "closed_at": closed_at,
                "activity": max((m.get("activity") or 0 for m in item["open"]), default=0),
                "routes": len(item["closed"]),
                "closed": item["closed"], "open": item["open"], "pubs": item["pubs"],
                "record": record_key(item["pr"], impl.get("loc"))}

    def published(item):
        """The version-carrying publish closed and no work route opened after it."""
        closes = [p.get("c") or 0 for p in item["pubs"] if p.get("v")]
        return bool(closes) and not any(
            route.get("kind") != "review" and _start(route) > max(closes) for route in item["open"])

    active, active_folded, done = [], 0, []
    for item in units.values():
        if item["open"] and published(item):
            done.append(finish(item))
        elif item["open"]:
            line = finish(item)
            if now - line["activity"] < ACTIVE_SECONDS or any(m.get("alive") for m in item["open"]):
                active.append(line)
            else:
                active_folded += 1
        elif item["closed"]:
            done.append(finish(item))
    active.sort(key=lambda u: (-u["activity"], u["key"]))
    done.sort(key=lambda u: (-u["closed_at"], u["key"]))
    shown = done[:DONE_LINES]
    total = closed_total if closed_total is not None else sum(len(i["closed"]) for i in units.values())
    return {"active": active, "active_folded": active_folded, "done": shown, "done_hidden": done[DONE_LINES:],
            "done_folded": max(0, total - sum(u["routes"] for u in shown))}


# What a viewer reads as markup stays escaped: a backslash, `*`, brackets, `<` `>` `&`, and a `_` that
# is not between two letters or digits (`campaign_document` stays as written). A pair of single
# backticks is a code span (the panel, Fleet and the viewer all draw it as code) and is left alone,
# its words too; a backtick with no partner is escaped so the line does not break.
_MARKUP = re.compile(r"[\\*\[\]<>&]|(?<![^\W_])_|_(?![^\W_])")
_TICKS = re.compile(r"(`+)")


def _escape(value):
    parts = _TICKS.split(unicodedata.normalize("NFC", _CONTROLS.sub("", str(value))))
    singles = [i for i in range(1, len(parts), 2) if len(parts[i]) == 1]
    spans = [(singles[i], singles[i + 1]) for i in range(0, len(singles) - 1, 2)]
    out = []
    for index, part in enumerate(parts):
        if any(start <= index <= end for start, end in spans):
            out.append(part)
        elif index % 2:
            out.append("\\`" * len(part))
        else:
            out.append(_MARKUP.sub(lambda found: "\\" + found.group(0), part))
    return "".join(out)


def _clip(value, limit):
    value = " ".join(_CONTROLS.sub("", str(value or "")).split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _unit_title(unit):
    return _escape(_clip(unit.get("title") or unit["impl"].get("slug") or unit["key"], TITLE_CHARS))


def _unit_line(unit, *, prefix=""):
    parts = [_unit_title(unit)]
    if unit.get("pr"):
        parts.append(f"#{unit['pr']}")
    if unit.get("v"):
        parts.append(_escape(unit["v"]))
    tail = f"기록 {unit['count']}"
    parts.append(f"[{tail} ›](<{RECORDS_DIR}/{unit['record']}.md>)" if unit.get("record") else tail)
    return "- " + prefix + " · ".join(parts)


def render_document(title, summary, units, *, now, tz=None, shown=None):
    """The campaign document's text: the head, `## 하는 중`, `## 최근에 끝난 것`. What waits for a person is a
    question and stands on the question screens, never here.

    The text always fits `SIZE_LIMIT`, which is under the readers' limit: the title and the one-line
    summary are clipped, and when the lines still do not fit, finished lines are folded first, then
    the oldest open work. Each fold is counted on a line that is not a bullet. A list given as `shown` is
    filled with the units whose lines the text really has (the record pages are written for those only)."""
    stamp = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M")
    heading = _escape(_clip(title, HEADING_CHARS))
    one_line = _escape(_clip(summary, SUMMARY_CHARS)) or heading
    active = list(units.get("active") or [])
    done = list(units.get("done") or [])
    done_folded = int(units.get("done_folded") or 0)

    def build(keep_done, keep_active):
        shown_done = done[:keep_done]
        folded = done_folded + sum(unit["routes"] or 1 for unit in done[keep_done:])
        out = [MARKER, f"# {heading}", "", f"갱신 {stamp}", "", f"**한 줄**: {one_line}", "", "## 하는 중"]
        out += [_unit_line(unit) for unit in active[:keep_active]]
        if not active:
            out.append("지금 없음.")
        if units.get("active_folded"):
            out.append(f"6시간 넘게 움직임 없는 열린 일 {units['active_folded']:,}개는 접어 둠.")
        if len(active) > keep_active:
            out.append(f"크기 한도로 접은 열린 일 {len(active) - keep_active:,}개.")
        out += ["", "## 최근에 끝난 것"]
        out += [_unit_line(unit, prefix=datetime.fromtimestamp(unit["closed_at"], tz).strftime("%m-%d") + " ")
                for unit in shown_done]
        if folded:
            out.append(f"앞서 끝난 기록 {folded:,}개는 접어 둠.")   # routes, as each line's `기록 N`
        elif not done:
            out.append("아직 없음.")
        return "\n".join(out) + "\n"

    def fits(*counts):
        return len(build(*counts).encode("utf-8")) <= SIZE_LIMIT

    def most(limit, fit):   # the most lines kept (0..limit) that still fit; `fit` only gets easier with fewer
        low, high = 0, limit
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if fit(middle) else (low, middle - 1)
        return low

    counts = [len(done), len(active)]   # build's order: done, active
    for index in (0, 1):   # finished lines first, then open work
        if fits(*counts):
            break
        counts[index] = most(counts[index], lambda k, i=index: fits(*[k if j == i else c for j, c in enumerate(counts)]))   # 0 when even none of them would make it fit
    text = build(*counts)
    if shown is not None:
        shown[:] = active[:counts[1]] + done[:counts[0]]
    return text


# ---------------------------------------------------------------------------
# record pages (pure): the lines of one piece of work, in the order it happened
# ---------------------------------------------------------------------------


def record_labels(unit, firsts=None):
    """`[(label, kind, member)]` for every line of a piece of work's record page, in order: its work and
    review routes and its publishes by when they started (then route id; `firsts` is route id -> the start
    of its first cycle, for a route that ran in more than one round), as many lines as the unit's
    `count`. The first work route is `구현` and the later ones `고침` (`고침 1`, `고침 2` from two), the first
    review is `독립 검토` and the later ones `닫힘 확인` (numbered from two), a publish is `출판`."""
    members = [("route", m) for m in unit.get("closed", []) + unit.get("open", [])]
    members += [("publish", p) for p in unit.get("pubs", [])]
    firsts = firsts or {}
    members.sort(key=lambda pair: (firsts.get(pair[1]["rid"], _start(pair[1])), pair[1]["rid"]))
    later_work = sum(1 for kind, m in members if kind == "route" and m.get("kind") != "review") - 1
    later_review = sum(1 for kind, m in members if kind == "route" and m.get("kind") == "review") - 1
    work_seen = review_seen = 0
    out = []
    for kind, member in members:
        if kind == "publish":
            out.append(("출판", "publish", member))
        elif member.get("kind") == "review":
            label = "독립 검토" if review_seen == 0 else ("닫힘 확인" if later_review == 1 else f"닫힘 확인 {review_seen}")
            review_seen += 1
            out.append((label, "review", member))
        else:
            label = "구현" if work_seen == 0 else ("고침" if later_work == 1 else f"고침 {work_seen}")
            work_seen += 1
            out.append((label, "work", member))
    return out


def verdict_of(text):
    """The verdict a review report states in its first 8 lines (`최종 판정: <words>`), without its trailing
    `— head <commit>`, clipped; None when there is none."""
    for line in str(text or "").split("\n")[:8]:
        found = _VERDICT.search(line)
        if found:
            words = _HEAD_TAIL.sub("", found.group(1)).strip().strip("*`").strip()
            if words:
                return words if len(words) <= VERDICT_CHARS else words[:VERDICT_CHARS - 1] + "…"
    return None


def supervisor_names(listing, pr):
    """The supervisor's own review records of a PR: names `RV-….md` that carry `PR<pr>` with no digit on
    either side (`PR1798` is found; `PR17980` and `XPR1798` are not), in name order."""
    if not pr:
        return []
    mark = re.compile(r"(?<![A-Za-z0-9])PR" + str(int(pr)) + r"(?!\d)")
    return sorted(name for name in (listing or ()) if name.startswith("RV-") and name.endswith(".md") and mark.search(name))


def pr_from_merges(log_text, head, base=None):
    """The number of the pull request merge that brought `head` in, from `<hash> <parents>\\t<subject>` lines of
    `git log --ancestry-path --topo-order --reverse head..base` (parents first), else None.

    A head on the base branch's own line (from `base`, first parent after first parent, down to `head`) came
    in through no pull request: a commit pushed straight to the base, or a head that is itself a merge on it.
    It has none, even when a longer branch took the base in by a `Merge pull request` and came back. Any
    other head is taken by the first `Merge pull request #N` with two or more parents whose first parent is
    not yet a descendant of `head` (one that is was merged on top of a commit already holding `head`, so it
    did not bring `head` in). Through a longer branch that is the head's own pull request into that branch,
    not the later one that brought the branch to the base. `Merge branch …` is another merge."""
    rows = []
    for line in str(log_text or "").split("\n"):
        meta, _, subject = line.partition("\t")
        sha, *parents = meta.split() or [""]
        if sha:
            rows.append((sha, parents, subject))
    if base:
        first = {sha: parents[0] for sha, parents, _subject in rows if parents}
        current = base
        for _step in range(len(first)):   # the listed commits are the head's descendants: the walk ends within them
            if current not in first:
                break
            if first[current] == head:
                return None
            current = first[current]
    seen = {head}
    for sha, parents, subject in rows:
        found = _MERGED_PR.match(subject)
        if found and len(parents) > 1 and parents[0] not in seen:
            return int(found.group(1))
        seen.add(sha)
    return None


def _link(text, target):
    """`[text](<target>)`, or the bare text when the target has a character a link cannot carry."""
    if not target or _LINK_UNSAFE.search(target):
        return text
    return f"[{text}](<{target}>)"


def render_record(title, head, lines):
    """A record page: the marker, the title, the head line and one bullet per line. No date or time is
    stamped, so the same input is the same bytes."""
    return "\n".join([RECORD_MARKER, f"# {title}", "", head, ""] + [f"- {line}" for line in lines]) + "\n"


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _list_routes(root):
    """route id -> [route file mtime_ns, close result mtime_ns or None], by exact file names."""
    found = {}
    try:
        with os.scandir(Path(root) / ".runtime" / "routes") as entries:
            for entry in entries:
                match = _OUTCOME_FILE.match(entry.name)
                if match:
                    found.setdefault(match.group(1), [0, None])[1] = entry.stat().st_mtime_ns
                    continue
                match = _ROUTE_FILE.match(entry.name)
                if match:
                    found.setdefault(match.group(1), [0, None])[0] = entry.stat().st_mtime_ns
    except OSError:
        return {}
    return found


def _epoch(text):
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


def _choose(records):
    """route id -> its one cycle record: a sealed one, then the most recently started."""
    chosen = {}
    for record in records:
        rid = record.get("route_id")
        if not isinstance(rid, str):
            continue
        rank = _rank(record)
        if rid not in chosen or rank > chosen[rid][0]:
            chosen[rid] = (rank, record)
    return {rid: pair[1] for rid, pair in chosen.items()}


def _rank(record):
    return (record.get("state") == "sealed", str(record.get("started_on") or ""))


def _member(record):
    return {"cy": record.get("cycle_id"), "slug": record.get("slug") or "", "cap": record.get("route_capability") or "",
            "st": _epoch(record.get("started_on")), "loc": record.get("locator") or "", "cid": record.get("campaign_id"),
            "rk": list(_rank(record))}


def _cycle_entry(record):
    return {"cy": record.get("cycle_id"), "st": _epoch(record.get("started_on")), "loc": record.get("locator") or ""}


def _add_cycle(member, entry):
    """Keep one more cycle of a route in `member["cycles"]` (`_choose` still names the one a line stands on).
    A route with one cycle carries no list, so the saved list does not grow for it."""
    if not isinstance(entry.get("cy"), str):
        return
    held = member.get("cycles")
    if not isinstance(held, list):
        held = [{"cy": member.get("cy"), "st": member.get("st"), "loc": member.get("loc") or ""}]
    held = [c for c in held if isinstance(c, dict) and isinstance(c.get("cy"), str) and c["cy"] != entry["cy"]] + [entry]
    held.sort(key=lambda c: (_start(c), c["cy"]))
    if len(held) > 1:
        member["cycles"] = held
    else:
        member.pop("cycles", None)


def _regular_in_root(root, path):
    """The real path of `path` when it is a regular file inside the artifact root (not a link), else None."""
    try:
        path = str(path)
        if not os.path.isabs(path):
            return None
        real = os.path.realpath(path)
        base = os.path.realpath(str(root))
        if real != base and not real.startswith(base + os.sep):
            return None
        if stat.S_ISLNK(os.lstat(path).st_mode) or not stat.S_ISREG(os.stat(real).st_mode):
            return None
        return real
    except (OSError, ValueError):
        return None


def _read_head(root, path, limit=None):
    """A text file's first bytes, only when it is a regular file inside the artifact root."""
    real = _regular_in_root(root, path)
    if real is None:
        return None
    try:
        with open(real, "rb") as handle:
            return handle.read(limit or HEAD_BYTES).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _read_fact(root, rid, member, kind, om):
    """What a closed route's close result and report say, or None when its result is unreadable."""
    path = Path(root) / ".runtime" / "routes" / f"{rid}.outcome.json"
    outcome = store._read_json(path)
    if outcome is None:
        return None
    ok = outcome.get("terminal_gate_proven") is True
    closed = _epoch(outcome.get("closed_at"))
    fact = {"k": kind, "slug": member["slug"], "cy": member["cy"], "st": member["st"], "c": closed or 0,
            "ok": ok, "om": om, "pr": pr_from_slug(member["slug"]), "prs": [], "v": None, "t": None}
    if kind == "publish":
        fact["pr"] = None
    if ok and (kind == "publish" or fact["pr"] is None):
        gates = outcome.get("terminal_gates")
        text = None
        for gate in (gates.values() if isinstance(gates, dict) else []):
            evidence = gate.get("evidence") if isinstance(gate, dict) else None
            text = _read_head(root, evidence) if evidence else None
            if text is not None:
                break
        if text is not None:
            if kind == "publish":
                fact["v"], fact["prs"] = publish_facts(text)
            else:
                fact["pr"] = pr_from_report(text)
    return fact


def _activity(root, rid, member):
    """Newest write to an open route: the existing cheap measure of a route's last movement,
    plus its cycle record and the top of its cycle folder."""
    import route_autoclose
    runtime = Path(root) / ".runtime"
    newest = route_autoclose._last_activity(Path(root), runtime / "routes" / f"{rid}.json",
                                            {"route_id": rid}, None, None, None, time.time())
    if member.get("cy"):
        newest = max(newest, route_autoclose._mtime(runtime / "artifact-producer" / "v1" / "cycles" / (member["cy"] + ".json")))
    return newest


def _live_moves(root, listing):
    """A reader of the route ids that hold a running attempt, with when each last moved: the runtime's
    own liveness (`route_autoclose.live_routes`) over the registry, read once however many campaigns
    ask in one pass and only for routes that have no close result. Unreadable means none are known."""
    memo = []

    def read():
        if not memo:
            try:
                import route_autoclose
                memo.append(route_autoclose.live_routes(
                    route_autoclose.registries_at(_agent_home()), [rid for rid, (_, closed) in listing.items() if closed is None]))
            except Exception:  # noqa: BLE001 -- the six-hour rule alone then decides, as before
                memo.append({})
        return memo[0]
    return read


def _agent_home():
    import dispatch_contract
    return dispatch_contract.resolve_agent_home()


def _intent_title(root, rid):
    try:
        import work_intent
        route = store._read_json(Path(root) / ".runtime" / "routes" / f"{rid}.json")
        intent = ((work_intent.route_intent(route) or {}).get("intent") or "") if route else ""
    except Exception:  # noqa: BLE001 -- the slug stands in
        return None
    intent = re.sub(r"^[#>*\-\s]+", "", " ".join(intent.split()))
    match = re.match(r"^(.+?[.!?。])(\s|$)", intent)
    return (match.group(1) if match else intent) or None


def pr_from_title(text):
    """The one PR number a first sentence names as `PR #N`, else None. A bare `#N` or a date is not one."""
    found = {int(number) for number in _INTENT_PR.findall(str(text or ""))}
    return found.pop() if len(found) == 1 else None


def _load_cache(root, campaign_id):
    value = store._read_json(Path(root) / CACHE_REL / f"{campaign_id}.json")
    if (not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("members"), dict)
            or not isinstance(value.get("facts"), dict) or not isinstance(value.get("titles"), dict)):
        return None
    return value


def _cache_member(entry):
    return isinstance(entry, dict) and isinstance(entry.get("slug"), str)


# ---------------------------------------------------------------------------
# record pages (reads): what each line of a page says, read from the folders
# ---------------------------------------------------------------------------


def _count_files(folder):
    """Regular files under `folder` (a link is not counted and never followed)."""
    total, stack = 0, [str(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _unit_reports(folder):
    base = Path(folder) / "artifacts" / "units"
    try:
        with os.scandir(base) as entries:
            names = sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))
    except OSError:
        return []
    return [base / name / "report.md" for name in names]


def _supervisor_listing(root, campaign_name):
    """The names of the regular files in `research/<campaign folder name>/inputs/`, read once; none when the
    folder is not there."""
    names = []
    try:
        with os.scandir(Path(root) / "research" / campaign_name / "inputs") as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(names)


def _evidence_paths(root, rid):
    """The evidence files a closed route's gates name (in the order the result lists them)."""
    outcome = store._read_json(Path(root) / ".runtime" / "routes" / f"{rid}.outcome.json") or {}
    gates = outcome.get("terminal_gates")
    found = []
    for gate in (gates.values() if isinstance(gates, dict) else []):
        evidence = gate.get("evidence") if isinstance(gate, dict) else None
        if isinstance(evidence, str) and evidence:
            found.append(evidence)
    return found


class _Pages:
    """Draws the record page of each shown line. `facts[rid]["pg"]` keeps what a closed route's cycle
    folder held (file count, report, verdict) so a pass does not walk it again; an open route is read
    every pass."""

    def __init__(self, root, campaign_dir, *, tz, facts, members, title_of, active_ids):
        self.root, self.campaign_dir, self.tz = Path(root), Path(campaign_dir), tz
        self.facts, self.members, self.title_of, self.active_ids = facts, members, title_of, active_ids
        self.records_real = os.path.realpath(str(self.campaign_dir / RECORDS_DIR))
        self._listing = None

    def day(self, moment):
        return datetime.fromtimestamp(moment, self.tz).strftime("%m-%d") if moment else None

    def listing(self):
        if self._listing is None:
            self._listing = _supervisor_listing(self.root, self.campaign_dir.name)
        return self._listing

    def target(self, real):
        """The link target of a real file, from the page folder, or None."""
        if not real:
            return None
        rel = os.path.relpath(real, self.records_real)
        return None if _LINK_UNSAFE.search(rel) else rel

    def cycle_info(self, rid, entry, *, primary, closed, review):
        """`{"nf": files, "rep": report path from the page folder or "", "vd": verdict or ""}` of one cycle."""
        cycle = entry.get("cy")
        slot = self.facts.get(rid) if closed else None
        held = (slot.get("pg") or {}).get(cycle) if isinstance(slot, dict) else None
        if isinstance(held, dict) and isinstance(held.get("nf"), int) and isinstance(held.get("rep"), str):
            info = held
        else:
            loc = entry.get("loc") or ""
            folder = self.campaign_dir / loc if loc and loc == os.path.basename(loc) and not loc.startswith(".") else None
            nf, rep, verdict = 0, "", ""
            if folder is not None and os.path.isdir(folder):
                nf = _count_files(folder / "artifacts")
                candidates = _evidence_paths(self.root, rid) if primary and closed else []
                finals = [folder / "artifacts" / "final_report.md"]
                units = _unit_reports(folder)
                candidates += finals + units if primary else units + finals
                real = next((r for r in map(lambda path: _regular_in_root(self.root, path), candidates) if r), None)
                if real:
                    rep = self.target(real) or ""
                    if review and rep:
                        verdict = verdict_of(_read_head(self.root, real, 4096)) or ""
            elif primary and closed:   # a folder that is gone can still have named its evidence
                real = next((r for r in map(lambda path: _regular_in_root(self.root, path), _evidence_paths(self.root, rid)) if r), None)
                rep = (self.target(real) or "") if real else ""
                if review and rep:
                    verdict = verdict_of(_read_head(self.root, real, 4096)) or ""
            info = {"nf": nf, "rep": rep, "vd": verdict}
            if closed and isinstance(slot, dict):
                slot.setdefault("pg", {})[cycle] = info
        return info

    def report_link(self, info):
        """The page-folder-relative path of the report, only while it is still a regular file inside the root."""
        rep = info.get("rep")
        if not rep or os.path.isabs(rep):
            return None
        real = _regular_in_root(self.root, os.path.normpath(os.path.join(self.records_real, rep)))
        return self.target(real)

    def when(self, member, closed):
        return self.day(member.get("c") if closed and member.get("c") else member.get("st"))

    def lines(self, unit):
        """`(lines, files)` of a unit's page: one line per record, the supervisor's after them."""
        open_ids = {m["rid"] for m in unit.get("open", [])}
        out, files = [], 0
        firsts = {}
        for member in unit.get("closed", []) + unit.get("open", []):
            held = (self.members.get(member["rid"]) or {}).get("cycles")
            starts = [c["st"] for c in held if isinstance(c, dict) and isinstance(c.get("st"), (int, float))] if isinstance(held, list) else []
            firsts[member["rid"]] = min(starts + [_start(member)])
        for label, kind, member in record_labels(unit, firsts):
            rid = member["rid"]
            closed = rid not in open_ids
            if kind == "publish":
                held = (self.facts.get(rid) or {}).get("pg") or {}
                slot = held.get("pub") if isinstance(held.get("pub"), dict) else None
                if slot is None:
                    real = next((r for r in map(lambda path: _regular_in_root(self.root, path), _evidence_paths(self.root, rid)) if r), None)
                    slot = {"nf": 0, "rep": (self.target(real) or "") if real else "", "vd": ""}
                    if isinstance(self.facts.get(rid), dict):
                        self.facts[rid].setdefault("pg", {})["pub"] = slot
                link = self.report_link(slot)
                parts = [label, self.day(member.get("c")), _escape(member["v"]) if member.get("v") else None,
                         _link("보고", link) if link else None]
                files += 1 if link else 0
                out.append(" · ".join(part for part in parts if part))
                continue
            member_row = self.members.get(rid) or {}
            held = member_row.get("cycles")
            cycles = [c for c in held if isinstance(c, dict)] if isinstance(held, list) and kind == "review" else None
            cycles = cycles or [{"cy": member.get("cy"), "loc": member.get("loc") or member_row.get("loc") or ""}]
            infos = [self.cycle_info(rid, c, primary=c.get("cy") == member.get("cy"), closed=closed, review=kind == "review")
                     for c in cycles]
            nf = sum(info["nf"] for info in infos) if kind == "review" else infos[0]["nf"]
            files += nf
            if kind == "work":
                link = self.report_link(infos[0])
                tail = _link("보고", link) if link else (None if closed else "보고 아직 없음")
                title = _escape(_clip(self.title_of(member), TITLE_CHARS))
                parts = [label, self.when(member, closed), title, tail, f"파일 {nf}개"]
            else:
                rounds = []
                for index, info in enumerate(infos, 1):
                    link = self.report_link(info)
                    words = f'"{_escape(info["vd"])}"' if info.get("vd") else None
                    if len(infos) == 1:
                        rounds.append(_link(words or "보고", link) if link else (None if closed else "보고 아직 없음"))
                    elif link:
                        rounds.append(_link(f"{index}라운드" + (f" {words}" if words else ""), link))
                    else:
                        rounds.append(f"{index}라운드" + (" 보고 아직 없음" if index == len(infos) and not closed else ""))
                parts = [label, self.when(member, closed)] + rounds + [f"파일 {nf}개"]
            out.append(" · ".join(part for part in parts if part))
        for name in supervisor_names(self.listing(), unit.get("pr")):
            path = self.root / "research" / self.campaign_dir.name / "inputs" / name
            real = _regular_in_root(self.root, path)
            link = self.target(real)
            if not link:
                continue
            verdict = verdict_of(_read_head(self.root, real, 4096))
            parts = ["감독의 검토 기록", f'"{_escape(verdict)}"' if verdict else None, _link(_escape(name), link)]
            out.append(" · ".join(part for part in parts if part))
            files += 1
        return out, files

    def page(self, unit):
        lines, files = self.lines(unit)
        head = [f"#{unit['pr']}" if unit.get("pr") else None, _escape(unit["v"]) if unit.get("v") else None,
                "하는 중" if id(unit) in self.active_ids else UNPROVEN_WORD if unit.get("unproven") else self.day(unit.get("closed_at")),
                f"기록 {unit['count']}", f"파일 {files}개"]
        return render_record(_unit_title(unit), " · ".join(part for part in head if part), lines)


# ---------------------------------------------------------------------------
# git: a closed route that carries no PR number is joined to its PR by ancestry (read only)
# ---------------------------------------------------------------------------


class _GitPass:
    """One pass's git reads. Only `rev-parse` and `log` are ever run, each with a time limit and a clean
    environment, and nothing is fetched: what is not in the repository now is left for a later pass.
    `budget` is how many `git log` calls are left this pass (the newest closes first, the rest next time).
    A base lookup (`rev-parse`, once per repository in a pass) does not use it, so the lookups of routes not
    merged yet cannot starve an older route. 0 starts nothing, and a call that timed out sets it to 0."""

    def __init__(self, budget=None):
        self.budget, self.calls, self.bases = GIT_BUDGET if budget is None else budget, 0, {}

    def _run(self, repo, *args, counted=True):
        """`(exit code, stdout)`; the code is None when git did not answer (not installed, timed out, or the
        pass has no budget left: nothing is started then). A `counted` call uses one of the budget, and any
        call that timed out ends git for the rest of the pass."""
        if self.budget <= 0:
            return None, ""
        env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
        env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})
        if counted:
            self.budget -= 1
        self.calls += 1
        try:
            done = subprocess.run(["git", "--no-pager", "-C", str(repo), *args], capture_output=True, text=True,
                                  timeout=GIT_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.budget = 0
            return None, ""
        except (OSError, subprocess.SubprocessError, ValueError):
            return None, ""
        return done.returncode, done.stdout

    def base(self, repo):
        """The commit of the first of `origin/HEAD`, `origin/main`, `main` that resolves; "" when none does
        (not a repository, or git is missing). Looked up once per repository in a pass; a folder with no
        `.git` in it or above it is not asked at all (starting git costs tens of milliseconds)."""
        if repo not in self.bases:
            found = ""
            for ref in () if not _inside_repository(repo) else ("origin/HEAD", "origin/main", "main"):
                code, out = self._run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", counted=False)
                if code == 0 and out.strip():
                    found = out.strip().split()[0]
                    break
                if code != 1:   # 1 is "no such ref"; anything else (not a repository, git missing, timed out) holds for every ref
                    break
            self.bases[repo] = found
        return self.bases[repo]

    def first_pr(self, repo, head, base):
        """`("ok", N or None)`, `("error", None)` (git refused: an unknown commit) or `("timeout", None)`."""
        code, out = self._run(repo, "log", "--ancestry-path", "--topo-order", "--reverse", "--no-show-signature",
                              "--format=%H %P%x09%s", "--end-of-options", f"{head}..{base}")
        if code is None:
            return "timeout", None
        if code != 0:
            return "error", None
        return "ok", pr_from_merges(out, head, base)


def _inside_repository(path):
    """True when `path` or a folder above it holds a `.git` (a folder, or the file a working tree has)."""
    current = os.path.abspath(str(path))
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _git_repo(root, entry):
    """The repository to ask: the route's own working tree while it is there, else the one holding the root."""
    repo = entry.get("r")
    if isinstance(repo, str) and repo and os.path.exists(os.path.join(repo, ".git")):
        return repo
    return str(root)


def _link_by_git(root, cache, rows, git):
    """Give a closed work route with no PR number the number of the first pull request that merged it,
    in `row["pr"]`. Only a route that made a commit (`head_commit` differs from `source_commit`) is asked
    and a number already known is never touched. A found number is kept for good; a route not merged yet
    is asked again only when the base commit has moved; a timeout is not remembered."""
    kept = cache.setdefault("git", {})
    targets = [row for row in rows if row.get("kind") == "work" and row.get("ok") and not row.get("pr")]
    for row in sorted(targets, key=lambda r: (-(r.get("c") or 0), r["rid"])):
        rid = row["rid"]
        entry = kept.get(rid)
        if not isinstance(entry, dict):
            outcome = store._read_json(Path(root) / ".runtime" / "routes" / f"{rid}.outcome.json") or {}
            route = store._read_json(Path(root) / ".runtime" / "routes" / f"{rid}.json") or {}
            tree = ((route.get("source_repository") or {}).get("worktree") or {}) if isinstance(route.get("source_repository"), dict) else {}
            entry = kept[rid] = {"h": outcome.get("head_commit") or None, "s": route.get("source_commit") or None,
                                 "r": tree.get("path") if isinstance(tree, dict) and isinstance(tree.get("path"), str) else None,
                                 "pr": None, "base": None}
        if isinstance(entry.get("pr"), int):
            row["pr"] = entry["pr"]
            continue
        head, source = entry.get("h"), entry.get("s")
        if not (isinstance(head, str) and isinstance(source, str) and _COMMIT.fullmatch(head) and _COMMIT.fullmatch(source)) or head == source:
            continue
        if git.budget <= 0:
            continue
        repo = _git_repo(root, entry)
        base = git.base(repo)
        if not base or entry.get("base") == base:
            continue
        status, number = git.first_pr(repo, head, base)
        if status == "timeout":
            continue
        entry["base"] = base
        if number:
            entry["pr"] = number
            row["pr"] = number


def _has_marker(path, marker):
    """True when `path` is a regular file (not a link) whose first line is `marker`."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return False
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            return carries_marker(handle.readline(len(marker) + 16).decode("utf-8", "replace"), marker)
    except OSError:
        return False


def _decide_record_links(campaign_dir, lines):
    """Say which lines may link to a record page: the key is a safe name, no earlier line has it, `records`
    is a real folder (or not there yet) and a file already at that name is one of the generator's. A line
    that may not stays as it was, `기록 N`, and what is there is not touched. Cheap `lstat`s only."""
    folder = Path(campaign_dir) / RECORDS_DIR
    try:
        usable = stat.S_ISDIR(os.lstat(folder).st_mode)
    except FileNotFoundError:
        usable = True
    except OSError:
        usable = False
    seen = set()
    for unit in lines:
        key = unit.get("record")
        if not key:
            unit["record"] = None
            continue
        page = folder / f"{key}.md"
        if not usable or key in seen or (os.path.lexists(page) and not _has_marker(page, RECORD_MARKER)):
            unit["record"] = None
            continue
        seen.add(key)


def collect(root, campaign_dir, *, now, cache=None, persist=False, listing=None, new_records=None, tz=None,
            live=None, pages=None, git=None):
    """`(document text, cache)` for one campaign, from its records. Writes nothing itself;
    `persist=False` is a faithful drawing from real records, `catch_up` persists. A dict given as `pages`
    is filled with the record pages (`key -> text`) of the lines the document really shows and of the pages
    `records/` already holds for work that is not on a line now; `git` is the pass's `_GitPass` (one is made
    when none is given)."""
    root, campaign_dir = Path(root), Path(campaign_dir)
    campaign = store._read_json(campaign_dir / "campaign.json") or {}
    campaign_id = campaign.get("campaign_id")
    meta = store._read_json(campaign_dir / "meta.json") or {}
    meta_campaign = meta.get("campaign") if isinstance(meta.get("campaign"), dict) else {}
    listing = listing if listing is not None else _list_routes(root)
    if cache is not None and (cache.get("v") != 1 or not isinstance(cache.get("members"), dict)):
        cache = None
    fresh = cache is None
    cache = {"v": 1, "horizon_ns": None, "members": {}, "facts": {}, "titles": {}, "git": {}} if fresh else cache
    members, facts, cached_titles = cache["members"], cache["facts"], cache.setdefault("titles", {})
    cached_prs = cache.setdefault("prs", {})   # open route -> the PR its intent's first sentence names (0: none)

    # A review an older version left without its rounds (it replaced the member, so `cycles` and `rk` are gone)
    # is picked out before this pass's records replace that member: a new record of the same review must not
    # make it look read.
    recover = {rid for rid, m in members.items()
               if _cache_member(m) and "rk" not in m and route_kind(m.get("cap"), m.get("slug")) == "review"}
    records = store.list_cycle_records(root) if fresh else (new_records or [])
    for rid, record in _choose(records).items():
        if record.get("campaign_id") == campaign_id:
            old = members.get(rid)
            member = _member(record)
            if _cache_member(old):
                try:   # a cycle that ranks below the one a line already stands on does not take its place
                    if tuple(old["rk"]) > tuple(member["rk"]):
                        old, member = None, dict(old)
                except (KeyError, TypeError):
                    pass
                if old is not None:
                    if isinstance(old.get("cycles"), list):
                        member["cycles"] = old["cycles"]
                    if old.get("cy") != member["cy"]:
                        _add_cycle(member, {"cy": old.get("cy"), "st": old.get("st"), "loc": old.get("loc") or ""})
            members[rid] = member
        else:
            members.pop(rid, None)
    for record in records:   # every cycle of a route, not only the one a line stands on (a review's rounds)
        member = members.get(record.get("route_id")) if record.get("campaign_id") == campaign_id else None
        if _cache_member(member):
            _add_cycle(member, _cycle_entry(record))
    members = cache["members"] = {rid: m for rid, m in members.items() if _cache_member(m)}
    reviews = [rid for rid, m in members.items()
               if route_kind(m["cap"], m["slug"]) == "review" and ("rk" not in m or rid in recover)]
    if reviews:   # a list an older version wrote (or replaced a member of): its reviews' cycles are read once, and `rk` marks them read
        wanted = set(reviews)
        # The indexed lookup saves the index it builds; a drawing (`persist=False`) reads every record instead
        # and writes nothing.
        read = (store.list_cycle_records(root, route_ids=reviews) if persist
                else [r for r in store.list_cycle_records(root) if r.get("route_id") in wanted])
        for record in read:
            member = members.get(record.get("route_id")) if record.get("campaign_id") == campaign_id else None
            if _cache_member(member):
                _add_cycle(member, _cycle_entry(record))
        for rid in reviews:
            member = members[rid]
            if "rk" in member:   # this pass's own record of the review already ranked the cycle it stands on
                continue
            mine = next((r for r in read if r.get("route_id") == rid and r.get("cycle_id") == member.get("cy")), None)
            member["rk"] = list(_rank(mine)) if mine else [False, ""]

    kinds = {rid: route_kind(m["cap"], m["slug"]) for rid, m in members.items()}
    closed = {rid: listing[rid][1] for rid in members if rid in listing and listing[rid][1] is not None}
    opened = [rid for rid in members if rid in listing and listing[rid][1] is None]

    # Reads: a close result that changed since it was read (promoted or replaced), then the new
    # ones newest first. A first drawing stops once DONE_LINES pieces of work are in hand.
    work_closed = [rid for rid in closed if kinds[rid] != "frame"]
    horizon = cache.get("horizon_ns")
    redo = sorted((rid for rid in work_closed if rid in facts and facts[rid].get("om") != closed[rid]),
                  key=lambda rid: -closed[rid])
    new = sorted((rid for rid in work_closed
                  if rid not in facts and (horizon is None or closed[rid] > horizon)),
                 key=lambda rid: (-closed[rid], rid))

    moves = (live or _live_moves(root, listing))()   # route id -> last move of its running attempt
    activity = {rid: max(_activity(root, rid, members[rid]), moves.get(rid, 0.0))
                for rid in opened if kinds[rid] not in ("frame", "publish")}

    def open_pr(rid):
        """The PR of an open route: its slug's, else the one its intent's first sentence names. Only a
        route still moving reads its intent, once; a long-idle one is folded and keeps its own line."""
        found = pr_from_slug(members[rid]["slug"])
        if found is not None or (rid not in moves and now - activity[rid] >= ACTIVE_SECONDS):
            return found
        if rid not in cached_prs:
            title = cached_titles.get(rid) or _intent_title(root, rid)
            if title:
                cached_titles[rid] = title
            cached_prs[rid] = pr_from_title(title) or 0
        return cached_prs[rid] or None

    doing = {f"pr:{pr}" for pr in (open_pr(rid) for rid in activity) if pr}

    def distinct():
        """Pieces of work already in hand for the done section (one still being worked on is not)."""
        return len({key for key in (f"pr:{f['pr']}" if f.get("pr") else f"rt:{rid}" for rid, f in facts.items()
                                    if f.get("ok") and f.get("k") in ("work", "review")) if key not in doing})

    reads = 0
    for rid in redo[:READ_CAP]:
        fact = _read_fact(root, rid, members[rid], kinds[rid], closed[rid])
        if fact is not None:
            facts[rid] = fact
        reads += 1
    last_read = None
    for rid in new:
        if reads >= READ_CAP:
            break
        if horizon is None and distinct() >= DONE_LINES:
            cache["horizon_ns"] = last_read if last_read is not None else min(
                (f["om"] for f in facts.values()), default=0)
            break
        fact = _read_fact(root, rid, members[rid], kinds[rid], closed[rid])
        reads += 1
        if fact is not None:
            facts[rid] = fact
            last_read = closed[rid]
    else:
        if horizon is None and reads < READ_CAP:
            cache["horizon_ns"] = 0  # the whole history fit in the window: nothing older is left

    for rid in [rid for rid in facts if rid not in members]:
        del facts[rid]

    fact_rows = [{"rid": rid, **{k: f.get(k) for k in ("slug", "cy", "st", "c", "ok", "pr", "prs", "v", "t")},
                  "loc": members[rid].get("loc") or "", "kind": f.get("k")} for rid, f in facts.items() if rid in closed]
    open_rows = []
    for rid in opened:
        if kinds[rid] in ("frame", "publish"):
            continue
        member = members[rid]
        open_rows.append({"rid": rid, "kind": kinds[rid], "slug": member["slug"], "cy": member["cy"],
                          "loc": member.get("loc") or "", "st": member["st"], "pr": open_pr(rid), "t": cached_titles.get(rid),
                          "activity": activity[rid], "alive": rid in moves})
    for rid in [rid for rid in cached_prs if rid not in members]:
        del cached_prs[rid]
    for row in fact_rows:
        row["t"] = cached_titles.get(row["rid"])
        row["pr"] = row["pr"] or cached_prs.get(row["rid"]) or None   # a close keeps what its open route named
    git = git if git is not None else _GitPass()
    kept_git = cache.setdefault("git", {})
    for rid in [rid for rid in kept_git if rid not in members]:
        del kept_git[rid]
    _link_by_git(root, cache, fact_rows, git)   # the routes that closed before their PR had a number

    titles = {}
    cycles = meta.get("cycles") if isinstance(meta.get("cycles"), dict) else {}
    for cycle_id, entry in cycles.items():
        value = entry.get("title") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip():
            titles[cycle_id] = value.strip()
    for row in fact_rows + open_rows:
        if titles.get(row["cy"]) == row["slug"]:
            del titles[row["cy"]]
    closed_total = sum(1 for rid in closed if kinds[rid] in ("work", "review")
                       and not (rid in facts and not facts[rid].get("ok")))  # a close read as unproven is no work
    units = link_units(fact_rows, open_rows, titles, now=now, closed_total=closed_total)
    for unit in units["active"] + units["done"]:
        if not unit["title"]:
            rid = unit["impl"]["rid"]
            found = cached_titles.get(rid) or _intent_title(root, rid)
            if found:
                cached_titles[rid] = found
                unit["title"] = found
    title = meta_campaign.get("title") or campaign.get("title") or campaign_dir.name
    summary = meta_campaign.get("summary") or ""
    lines = units["active"] + units["done"]
    _decide_record_links(campaign_dir, lines)
    shown = []
    text = render_document(title, summary, units, now=now, tz=tz, shown=shown)
    if pages is not None:
        def title_of(member):
            rid = member["rid"]
            return (titles.get(member.get("cy")) or member.get("t") or cached_titles.get(rid)
                    or _intent_title(root, rid) or member.get("slug") or rid)
        drawing = _Pages(root, campaign_dir, tz=tz, facts=facts, members=members, title_of=title_of,
                         active_ids={id(unit) for unit in units["active"]})
        for unit in shown:
            if unit.get("record"):
                pages[unit["record"]] = drawing.page(unit)
        _redraw_held_pages(campaign_dir, pages, drawing, units, shown, members, fact_rows, titles, now)
    return text, cache


def _redraw_held_pages(campaign_dir, pages, drawing, units, shown, members, fact_rows, titles, now):
    """Add to `pages` the pages `records/` already holds for work, whether or not its line is on the document and
    under every name the work has had (its PR number became known, a later round began in another folder):
    the name of each cycle of its routes, and its own. A name `records/` does not hold is not made, and a name a
    line already has its page for is left to that line. A close that was not proven is no line and no count,
    and its page shows `UNPROVEN_WORD`."""
    held = _page_names(campaign_dir)   # one listing of `records/` for the pass
    if not held:
        return
    unproven = [dict(row, ok=True) for row in fact_rows
                if not row.get("ok") and row.get("kind") in ("work", "review")]
    quiet = link_units(unproven, [], titles, now=now)["done"] if unproven else []
    for unit in quiet:
        unit["unproven"] = True
    shown_ids = {id(unit) for unit in shown}
    for unit in shown + units.get("done_hidden", []) + quiet:
        names = set()
        for member in unit["closed"] + unit["open"]:
            row = members.get(member["rid"]) or {}
            names.add(row.get("loc") or "")
            names |= {c.get("loc") or "" for c in row.get("cycles") or [] if isinstance(c, dict)}
        if id(unit) not in shown_ids:
            names.add(unit.get("record") or "")
        names = {name for name in names if name in held and name not in pages and _RECORD_KEY.fullmatch(name)}
        if names:
            text = drawing.page(unit)
            for name in sorted(names):
                pages[name] = text


def _page_names(campaign_dir):
    """The names (without `.md`) of the files in `records/`; a bare listing, nothing is read from them."""
    try:
        with os.scandir(Path(campaign_dir) / RECORDS_DIR) as entries:
            return {entry.name[:-3] for entry in entries if entry.name.endswith(".md")}
    except OSError:
        return set()


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def _body(text):
    return "\n".join(line for line in text.split("\n") if not _UPDATED_LINE.match(line))


def _create_document(path, text):
    """Make `path` with `text` only when nothing is there: written whole beside it, then linked in, so a
    `campaign.md` that appeared meanwhile is never overwritten. False when one did."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(str(tmp), str(path))
    except FileExistsError:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    store._fsync_dir(path.parent)
    return True


def _write_document(campaign_dir, text, *, name=DOCUMENT, marker=MARKER):
    """Create `campaign.md` (or another generated file, `name`), or replace it when it is a regular file that
    starts with `marker`. True when the folder holds the generator's own file afterwards."""
    path = Path(campaign_dir) / name
    for _attempt in range(2):
        try:
            meta = os.lstat(path)
        except FileNotFoundError:
            meta = None
        if meta is None:
            if _create_document(path, text):
                return True
            continue   # one appeared between the look and the link: judge it like any other file
        if not stat.S_ISREG(meta.st_mode):
            return False
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                before = handle.read(65537).decode("utf-8", "replace")
        except OSError:
            return False
        if not carries_marker(before, marker):
            return False
        if _body(before) == _body(text):
            return True
        store._write_atomic(path, text.encode("utf-8"), 0o644)
        return True
    return False


def remove_generated(campaign_dir, *, name=DOCUMENT, marker=MARKER):
    """Delete the campaign's `campaign.md` when it is the generator's own (first line MARKER).

    For the one place that removes a campaign folder created by a begin and never populated:
    the document is derived and comes back with the next pass, but must not keep that folder.
    `name` and `marker` name another generated file (a record page) for the same use."""
    path = Path(campaign_dir) / name
    if _has_marker(path, marker):
        try:
            path.unlink()
        except OSError:
            pass


def remove_generated_records(campaign_dir):
    """Delete the record pages the generator made in a campaign folder (first line `RECORD_MARKER`) and the
    `records` folder when that leaves it empty. For the same place as `remove_generated`: a campaign folder
    a begin made and nothing ever filled must not be kept by what the generator wrote into it. Any other
    file in `records` stays, and so does the folder."""
    folder = Path(campaign_dir) / RECORDS_DIR
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        remove_generated(folder, name=name, marker=RECORD_MARKER)
    try:
        folder.rmdir()
    except OSError:
        pass


def _write_records(campaign_dir, pages):
    """Write the record pages (`key -> text`) into `records/`: made (inside a campaign folder that is there;
    never its parents) when there is a page to write, never a folder that is a link or a file, a page changed only when its text changed, a file of that name that
    is not the generator's left as it is. Nothing is removed: a page whose line has folded stays (and is drawn
    again while its work is known)."""
    if not pages:
        return
    folder = Path(campaign_dir) / RECORDS_DIR
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return
    except FileNotFoundError:
        try:
            os.mkdir(folder)   # one level: a campaign folder that is gone is not made again
        except FileExistsError:
            pass
        except OSError:
            return
        try:
            if not stat.S_ISDIR(os.lstat(folder).st_mode):
                return
        except OSError:
            return
    for key, text in sorted(pages.items()):
        if not _RECORD_KEY.fullmatch(key):
            continue
        try:
            _write_document(folder, text, name=f"{key}.md", marker=RECORD_MARKER)
        except Exception as exc:  # noqa: BLE001 -- one page failing never stops the others
            print(f"campaign_document skipped error={type(exc).__name__}", file=sys.stderr)


def _document_is_ours_or_absent(campaign_dir):
    path = Path(campaign_dir) / DOCUMENT
    return not os.path.lexists(path) or _has_marker(path, MARKER)


def _records_since(root, mark):
    """Cycle records written after `mark` (ns), chosen by one `stat` each on the `cycles/` listing, as
    the routes listing is. Only those are read; no index is built, so a pass reads what changed."""
    directory = store.producer_dir(root) / "cycles"
    rows = []
    try:
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.name.endswith(".json") and not entry.name.startswith(".")
                           and entry.stat().st_mtime_ns > mark)
    except OSError:
        return rows
    for name in names:
        value = store._read_json(directory / name)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _cached_campaigns(root):
    """The ids of the campaigns this generator has kept a saved list for (so, a document)."""
    try:
        names = sorted(os.listdir(root / CACHE_REL))
    except OSError:
        return []
    return [name[:-len(".json")] for name in names if name.endswith(".json") and name != "watermark.json"]


def _records_of_closes(root, listing, mark, have):
    """Records for routes whose close result changed after `mark` but whose cycle record did not (a close
    promoted later): found through the saved campaign lists, which hold each route's cycle id, and for
    the ones no list holds (a campaign without a document yet) by route id."""
    need = {rid for rid, (_route, closed) in listing.items() if closed and closed > mark} - have
    rows = []
    if not need:
        return rows
    for other in _cached_campaigns(root):
        held = _load_cache(root, other)
        for rid in sorted(need & set(held["members"] if held else ())):
            need.discard(rid)
            record = store.read_cycle_record(root, held["members"][rid]["cy"]) if _cache_member(held["members"][rid]) else None
            if record is not None:
                rows.append(record)
    if need:
        # Not in any saved list: a campaign with no document yet. Looked up by route id, which is
        # rare (only a close whose route no list holds), so a pass does not read every record.
        rows += store.list_cycle_records(root, route_ids=need)
    return rows


def _write_state(root, name, value):
    folder = Path(root) / CACHE_REL
    store._ensure_dir(folder)
    store._write_atomic(folder / name, store._json_bytes(value))


def _campaign_folder(root, campaign_id):
    """The campaign's folder by its id, found without healing any index (this pass only reads)."""
    import artifact_locator
    found = artifact_locator.locate(root, campaign_id, heal=False)
    if found is None:
        raise LookupError("campaign-folder-not-found")
    return found


def _former_campaigns(root, rid, record, mark):
    """The other campaigns whose saved list still holds `rid` when its record, older than this pass's
    window, now names a campaign that does not (a route moved). A route that only began or closed is
    already in its campaign's list, so this reads nothing more for it."""
    target, started = record.get("campaign_id"), _epoch(record.get("started_on"))
    if not isinstance(target, str) or started is None or started * 10 ** 9 >= mark:
        return []
    cache = _load_cache(root, target)
    if cache is not None and rid in cache["members"]:
        return []
    found = []
    for other in _cached_campaigns(root):
        if other == target:
            continue
        held = _load_cache(root, other)
        if held is not None and rid in held["members"]:
            found.append(other)
    return found


def catch_up(root, cycle_id=None, *, now=None, git=None):
    """Bring the documents of the campaigns that moved since the last pass up to date.

    A campaign moved when one of its cycle records was written or one of its routes closed after the
    saved mark (the first pass looks back ten minutes), or when `cycle_id` is its cycle. Only campaigns that are
    still active are written. One campaign failing never stops another. Returns the names of
    the campaign folders whose document was written or already current."""
    root = Path(root)
    if not (root / ".runtime" / "routes").is_dir():
        return []
    started = time.time_ns() if now is None else int(now * 10 ** 9)  # `now` is the pass's clock
    now = time.time() if now is None else now
    listing = _list_routes(root)
    live = _live_moves(root, listing)
    saved = store._read_json(root / CACHE_REL / "watermark.json") or {}
    mark = saved.get("ns")
    mark = mark if isinstance(mark, int) and not isinstance(mark, bool) else started - FIRST_WINDOW_NS
    records = _records_since(root, mark)
    records += _records_of_closes(root, listing, mark, {r.get("route_id") for r in records})
    if isinstance(cycle_id, str) and cycle_id:
        hinted = store.read_cycle_record(root, cycle_id)
        if hinted is not None and all(r.get("cycle_id") != cycle_id for r in records):
            records.append(hinted)
    by_campaign = {}
    for record in records:
        if isinstance(record.get("campaign_id"), str):
            by_campaign.setdefault(record["campaign_id"], []).append(record)
    for rid, record in _choose(records).items():
        for other in _former_campaigns(root, rid, record, mark):
            by_campaign.setdefault(other, [])   # its document is written again, and drops the route
    written = []
    git = git if git is not None else _GitPass()   # a caller that must start no process gives `_GitPass(budget=0)`
    if by_campaign:
        for campaign_id, rows in sorted(by_campaign.items()):
            try:
                folder = _campaign_folder(root, campaign_id)
                record = store._read_json(folder / "campaign.json")
                if not record or record.get("campaign_id") != campaign_id or record.get("state") != "active":
                    continue
                pages = {}
                text, cache = collect(root, folder, now=now, cache=_load_cache(root, campaign_id),
                                      persist=True, listing=listing, new_records=records, live=live,
                                      pages=pages, git=git)
                if _document_is_ours_or_absent(folder):   # a page is for a line that links to it; no document, no pages
                    _write_records(folder, pages)
                if _write_document(folder, text):
                    written.append(folder.name)
                else:
                    print(f"campaign_document skipped foreign={folder.name}", file=sys.stderr)
                _write_state(root, f"{campaign_id}.json", cache)
            except Exception as exc:  # noqa: BLE001 -- one campaign never stops the others
                print(f"campaign_document skipped error={type(exc).__name__}", file=sys.stderr)
    try:
        _write_state(root, "watermark.json", {"v": 1, "ns": started - WATERMARK_SLACK_NS})
    except OSError:
        pass
    return written
