#!/usr/bin/env python3
"""Writes and keeps each campaign's own document, `campaigns/<campaign>/campaign.md`.

The entrance refresh worker calls `catch_up` before it writes the entrance: code, not a
session, makes and edits the document (no model call, no resident process, no new command).
A campaign's document has three sections, in this order: `## 하는 중`, `## 기다리는 것`,
`## 최근에 끝난 것`. One piece of work is one line: routes that carry the same PR number are
joined (`link_units`), a route with no number is its own line.

Three layers:

- pure functions that read no file, clock or environment (`pr_from_slug`, `pr_from_report`,
  `publish_facts`, `route_kind`, `link_units`, `render_document`);
- reads without writes (`read_waits`, `collect`; `collect(persist=False)` draws a document from
  real records and touches nothing);
- one writer, `catch_up`, which only creates `campaign.md` or replaces a file whose first line
  is `MARKER`. A same-named file without the marker, a link, a directory, and the hand-written
  `현황.md` / `status.md` are never changed.

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

import hashlib
import json
import os
import re
import stat
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import artifact_producer_store as store  # noqa: E402

MARKER = "<!-- hearting:campaign-document v1 -->"
DOCUMENT = "campaign.md"
ACTIVE_SECONDS = 6 * 3600
DONE_LINES = 20
READ_CAP = 400
FIRST_WINDOW_NS = 10 * 60 * 10 ** 9
WATERMARK_SLACK_NS = 2 * 10 ** 9
REPORT_LINES = 30
PUBLISH_LINES = 80
HEAD_BYTES = 16 * 1024
TITLE_CHARS = 90
WAIT_CHARS = 120
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
_QUESTION_FILE = re.compile(r"[0-9a-f]{64}")


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


def link_units(facts, open_routes, titles, *, now, closed_total=None):
    """Join routes into pieces of work and sort them into the document's lines.

    `facts`: closed routes (`rid`, `kind`, `slug`, `cy`, `st`, `c`, `ok`, `pr`, `prs`, `v`, `t`).
    `open_routes`: routes with no close result (`rid`, `kind`, `slug`, `cy`, `st`, `pr`,
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
                "routes": len(item["closed"])}

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
    return {"active": active, "active_folded": active_folded, "done": shown,
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


def _unit_line(unit, *, prefix=""):
    parts = [_escape(_clip(unit.get("title") or unit["impl"].get("slug") or unit["key"], TITLE_CHARS))]
    if unit.get("pr"):
        parts.append(f"#{unit['pr']}")
    if unit.get("v"):
        parts.append(_escape(unit["v"]))
    parts.append(f"기록 {unit['count']}")
    return "- " + prefix + " · ".join(parts)


def render_document(title, summary, units, waits, *, now, tz=None):
    """The campaign document's text. Description lines are never bullets, so a reader counting
    bullets under `## 기다리는 것` counts open questions and nothing else.

    The text always fits `SIZE_LIMIT`, which is under the readers' limit: the title and the one-line
    summary are clipped, and when the lines still do not fit, finished lines are folded first, then
    the oldest open work, and questions last (they are what a person must answer). Each fold is counted on a line that is not a bullet."""
    stamp = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M")
    heading = _escape(_clip(title, HEADING_CHARS))
    one_line = _escape(_clip(summary, SUMMARY_CHARS)) or heading
    active = list(units.get("active") or [])
    done = list(units.get("done") or [])
    done_folded = int(units.get("done_folded") or 0)
    items = list(waits.get("items") or []) if waits and waits.get("status") == "ok" else []

    def build(keep_done, keep_waits, keep_active):
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
        out += ["", "## 기다리는 것"]
        if waits and waits.get("status") == "ok":
            out += [f"- {_escape(_clip(item, WAIT_CHARS))}" for item in items[:keep_waits]]
            if not items:
                out.append("지금 없음.")
            if len(items) > keep_waits:
                out.append(f"크기 한도로 접은 질문 {len(items) - keep_waits:,}개.")
            seen = datetime.fromtimestamp(waits["at"], tz)
            today = seen.date() == datetime.fromtimestamp(now, tz).date()
            out.append("질문판 기준 " + seen.strftime("%H:%M" if today else "%m-%d %H:%M"))
        else:
            out.append("질문판을 읽지 못했습니다.")
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

    counts = [len(done), len(items), len(active)]   # build's order: done, waits, active
    for index in (0, 2, 1):   # finished lines first, then open work, and the questions last
        if fits(*counts):
            break
        counts[index] = most(counts[index], lambda k, i=index: fits(*[k if j == i else c for j, c in enumerate(counts)]))   # 0 when even none of them would make it fit
    return build(*counts)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _first_line(value):
    for line in _CONTROLS.sub("", str(value or "")).split("\n"):   # an unpaired surrogate cannot be written
        if line.strip():
            return line.strip()
    return ""


def _open_question_files():
    """The open question files of the board's own state folder, read once: a list of their values.

    Only names the board writes (64 hexadecimal digits) are read, so the board's copies in the same
    folder are not; a file that cannot be read, or whose `id` is not text, is left out. Read only."""
    import human_wait_state
    found = []
    for path in sorted(human_wait_state.state_root().glob("*.json")):
        if not _QUESTION_FILE.fullmatch(path.stem):
            continue
        value = human_wait_state._read(path)
        if value and isinstance(value.get("id"), str) and value.get("lifecycle") == "open":
            found.append(value)
    return found


def _folder_root(cwd, root_real):
    """Whether an absolute folder resolves to the artifact root `root_real` (a relative one would
    resolve against the worker's own folder, so it never does)."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return False
    from tools.fleet.collectors import project_status
    found = project_status.artifact_root(cwd)
    return bool(found) and os.path.realpath(str(found)) == root_real


def read_waits(root, now):
    """This repo's open questions from the question board's saved copy (never refreshed here).

    `{"status": "ok", "at": observed_at, "items": [first lines]}`, or `{"status": "unreadable"}`
    only when there is no copy or reading it fails; a copy of any age is used and its time is
    shown. An item is open when a person must answer it and it is not folded as old. A route's
    item belongs here when its route is one of this root's. An `S4` item reads its question and
    folder from the question file with the same id (open only; none found means left out) and
    belongs here when that folder resolves to this root. An open `S4` question file made after the
    copy (its `since` is later than the copy's `observed_at`) and not on it is added when its folder
    resolves to this root, once however many files carry its id. Anything else is left out."""
    try:
        import human_wait_state
        view = human_wait_state.cached_view(now=now, request=False, max_age=float("inf"))
        if view is None:
            return {"status": "unreadable"}
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        root_real = os.path.realpath(str(root))
        open_items = [item for item in view["items"]
                      if human_wait_state.person_wait(item) and item.get("stale") not in (True, "older")]
        wanted = {item.get("id") for item in open_items if str(item.get("source")) == "S4"}
        opened = _open_question_files()   # the one look at the state folder
        files = {value["id"]: value for value in opened if value["id"] in wanted}
        items = []
        for item in open_items:
            question, cwd = item.get("question"), item.get("cwd")
            if str(item.get("source")) == "S4":
                record = files.get(item.get("id"))
                if record is None:
                    continue
                question, cwd = record.get("question"), record.get("cwd")
            route_id = item.get("route_id")
            here = isinstance(route_id, str) and _ROUTE_FILE.match(route_id + ".json") is not None \
                and (Path(root) / ".runtime" / "routes" / (route_id + ".json")).is_file()
            if not here and cwd:
                here = _folder_root(cwd, root_real)
            if here:
                items.append(_first_line(question) or "질문 (본문 없음)")
        # A question asked after the copy was made is not on it yet: its own file says it is open.
        observed, listed = float(view["observed_at"]), {item.get("id") for item in view["items"]}
        for value in opened:
            if (value.get("mode") != "message" or value["id"] in listed or not value.get("cwd")
                    or not isinstance(value.get("since"), (int, float)) or value["since"] <= observed
                    or human_wait_state.wait_fold(value["since"], now) is not None):
                continue
            if _folder_root(value["cwd"], root_real):
                items.append(_first_line(value.get("question")) or "질문 (본문 없음)")
                listed.add(value["id"])
        return {"status": "ok", "at": observed, "items": items}
    except Exception:  # noqa: BLE001 -- an unreadable copy is a stated state, never a failure
        return {"status": "unreadable"}


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
        rank = (record.get("state") == "sealed", str(record.get("started_on") or ""))
        if rid not in chosen or rank > chosen[rid][0]:
            chosen[rid] = (rank, record)
    return {rid: pair[1] for rid, pair in chosen.items()}


def _member(record):
    return {"cy": record.get("cycle_id"), "slug": record.get("slug") or "", "cap": record.get("route_capability") or "",
            "st": _epoch(record.get("started_on")), "loc": record.get("locator") or "", "cid": record.get("campaign_id")}


def _read_head(root, path):
    """A text file's first bytes, only when it is a regular file inside the artifact root."""
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
        with open(real, "rb") as handle:
            return handle.read(HEAD_BYTES).decode("utf-8", "replace")
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


def collect(root, campaign_dir, *, now, waits, cache=None, persist=False, listing=None, new_records=None, tz=None,
            live=None):
    """`(document text, cache)` for one campaign, from its records. Writes nothing itself;
    `persist=False` is a faithful drawing from real records, `catch_up` persists."""
    root, campaign_dir = Path(root), Path(campaign_dir)
    campaign = store._read_json(campaign_dir / "campaign.json") or {}
    campaign_id = campaign.get("campaign_id")
    meta = store._read_json(campaign_dir / "meta.json") or {}
    meta_campaign = meta.get("campaign") if isinstance(meta.get("campaign"), dict) else {}
    listing = listing if listing is not None else _list_routes(root)
    if cache is not None and (cache.get("v") != 1 or not isinstance(cache.get("members"), dict)):
        cache = None
    fresh = cache is None
    cache = {"v": 1, "horizon_ns": None, "members": {}, "facts": {}, "titles": {}} if fresh else cache
    members, facts, cached_titles = cache["members"], cache["facts"], cache.setdefault("titles", {})
    cached_prs = cache.setdefault("prs", {})   # open route -> the PR its intent's first sentence names (0: none)

    records = store.list_cycle_records(root) if fresh else (new_records or [])
    for rid, record in _choose(records).items():
        if record.get("campaign_id") == campaign_id:
            members[rid] = _member(record)
        else:
            members.pop(rid, None)
    members = cache["members"] = {rid: m for rid, m in members.items() if _cache_member(m)}

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
                  "kind": f.get("k")} for rid, f in facts.items() if rid in closed]
    open_rows = []
    for rid in opened:
        if kinds[rid] in ("frame", "publish"):
            continue
        member = members[rid]
        open_rows.append({"rid": rid, "kind": kinds[rid], "slug": member["slug"], "cy": member["cy"],
                          "st": member["st"], "pr": open_pr(rid), "t": cached_titles.get(rid),
                          "activity": activity[rid], "alive": rid in moves})
    for rid in [rid for rid in cached_prs if rid not in members]:
        del cached_prs[rid]
    for row in fact_rows:
        row["t"] = cached_titles.get(row["rid"])
        row["pr"] = row["pr"] or cached_prs.get(row["rid"]) or None   # a close keeps what its open route named

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
    return render_document(title, summary, units, waits, now=now, tz=tz), cache


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


def _write_document(campaign_dir, text):
    """Create `campaign.md`, or replace it when it is a regular file that starts with MARKER.
    True when this campaign's folder holds the generator's document afterwards."""
    path = Path(campaign_dir) / DOCUMENT
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
        if before.split("\n", 1)[0].strip() != MARKER:
            return False
        if _body(before) == _body(text):
            return True
        store._write_atomic(path, text.encode("utf-8"), 0o644)
        return True
    return False


def remove_generated(campaign_dir):
    """Delete the campaign's `campaign.md` when it is the generator's own (first line MARKER).

    For the one place that removes a campaign folder created by a begin and never populated:
    the document is derived and comes back with the next pass, but must not keep that folder."""
    path = Path(campaign_dir) / DOCUMENT
    try:
        if stat.S_ISREG(os.lstat(path).st_mode):
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                first = handle.readline(len(MARKER) + 16).decode("utf-8", "replace").strip()
            if first == MARKER:
                path.unlink()
    except OSError:
        pass


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


def _waits_signature(board):
    """What the question list says, apart from the time it was read."""
    if not isinstance(board, dict) or board.get("status") != "ok":
        return "unreadable"
    return hashlib.sha256(json.dumps(board.get("items") or []).encode()).hexdigest()[:16]   # ASCII escapes: any text, even an unpaired surrogate


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


def catch_up(root, cycle_id=None, *, now=None, waits=None):
    """Bring the documents of the campaigns that moved since the last pass up to date.

    A campaign moved when one of its cycle records was written or one of its routes closed after the
    saved mark (the first pass looks back ten minutes), or when `cycle_id` is its cycle. Only campaigns that are
    still active are written, and so is each one that already has a document when the repo's
    question list changed since the last pass. One campaign failing never stops another. Returns the names of
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
    board = waits if waits is not None else read_waits(root, now)
    signature = _waits_signature(board)
    if signature != saved.get("waits"):
        # The questions are the repo's, the same on every document: when they changed (one answered,
        # one asked) each campaign that has a document is written again, with no route event needed.
        for campaign_id in _cached_campaigns(root):
            by_campaign.setdefault(campaign_id, [])
    written = []
    if by_campaign:
        for campaign_id, rows in sorted(by_campaign.items()):
            try:
                folder = _campaign_folder(root, campaign_id)
                record = store._read_json(folder / "campaign.json")
                if not record or record.get("campaign_id") != campaign_id or record.get("state") != "active":
                    continue
                text, cache = collect(root, folder, now=now, waits=board, cache=_load_cache(root, campaign_id),
                                      persist=True, listing=listing, new_records=records, live=live)
                if _write_document(folder, text):
                    written.append(folder.name)
                else:
                    print(f"campaign_document skipped foreign={folder.name}", file=sys.stderr)
                _write_state(root, f"{campaign_id}.json", cache)
            except Exception as exc:  # noqa: BLE001 -- one campaign never stops the others
                print(f"campaign_document skipped error={type(exc).__name__}", file=sys.stderr)
    try:
        _write_state(root, "watermark.json", {"v": 1, "ns": started - WATERMARK_SLACK_NS, "waits": signature})
    except OSError:
        pass
    return written
