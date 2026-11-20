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

Cost: routes are not read one by one. Listing the routes folder with `stat`, one cycle record
(about 1 KB) per route and the small close result are what is read. What was read is kept in
`<root>/.runtime/campaign-document/<campaign_id>.json`; deleting it only starts the campaign
again from its newest 20 pieces of work (nothing is backfilled).
"""
from __future__ import annotations

import os
import re
import stat
import sys
import time
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
    `activity`). `titles`: cycle id -> title from the campaign's meta. A route's PR number joins
    it to the other routes of that number; without one it stands alone. Frames do no work and a
    close that is not proven is not finished work: neither is a line or a count. Publish routes
    have no line of their own; they add their version and a record to the PRs they name.
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
        return {"key": item["key"], "pr": item["pr"], "v": version, "title": title, "impl": impl,
                "count": len(members) + len(item["pubs"]),
                "closed_at": max((m.get("c") or 0 for m in item["closed"]), default=0),
                "activity": max((m.get("activity") or 0 for m in item["open"]), default=0),
                "routes": len(item["closed"])}

    active, active_folded, done = [], 0, []
    for item in units.values():
        if item["open"]:
            line = finish(item)
            if now - line["activity"] < ACTIVE_SECONDS:
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


def _escape(value):
    import artifact_entrance
    return artifact_entrance._md_summary(value)


def _clip(value, limit):
    value = " ".join(str(value or "").split())
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
    bullets under `## 기다리는 것` counts open questions and nothing else."""
    stamp = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %H:%M")
    heading = _escape(" ".join(str(title or "").split()))
    one_line = _escape(" ".join(str(summary or "").split())) or heading
    active = list(units.get("active") or [])
    done = list(units.get("done") or [])
    done_folded = int(units.get("done_folded") or 0)

    def build():
        out = [MARKER, f"# {heading}", "", f"갱신 {stamp}", "", f"**한 줄**: {one_line}", "", "## 하는 중"]
        out += [_unit_line(unit) for unit in active]
        if not active:
            out.append("지금 없음.")
        if units.get("active_folded"):
            out.append(f"6시간 넘게 움직임 없는 열린 일 {units['active_folded']:,}개는 접어 둠.")
        out += ["", "## 기다리는 것"]
        if waits and waits.get("status") == "ok":
            items = waits.get("items") or []
            out += [f"- {_escape(_clip(item, WAIT_CHARS))}" for item in items]
            if not items:
                out.append("지금 없음.")
            out.append("질문판 기준 " + datetime.fromtimestamp(waits["at"], tz).strftime("%H:%M"))
        else:
            out.append("질문판을 읽지 못함.")
        out += ["", "## 최근에 끝난 것"]
        out += [_unit_line(unit, prefix=datetime.fromtimestamp(unit["closed_at"], tz).strftime("%m-%d") + " ")
                for unit in done]
        if done_folded:
            out.append(f"앞서 끝난 것 {done_folded:,}개는 접어 둠.")
        elif not done:
            out.append("아직 없음.")
        return "\n".join(out) + "\n"

    text = build()
    while len(text.encode("utf-8")) > SIZE_LIMIT and done:
        done_folded += done.pop()["routes"] or 1
        text = build()
    return text


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _first_line(value):
    for line in str(value or "").split("\n"):
        if line.strip():
            return line.strip()
    return ""


def read_waits(root, now):
    """This repo's open questions from the question board's saved copy (never refreshed here).

    `{"status": "ok", "at": observed_at, "items": [first lines]}`, or `{"status": "unreadable"}`
    when there is no copy, it is too old, or reading it fails. An item is open when a person
    must answer it and it is not folded as old; it belongs here when its route is one of this
    root's, or its folder resolves to this root. Anything else is left out."""
    try:
        import human_wait_state
        view = human_wait_state.cached_view(now=now, request=False)
        if view is None:
            return {"status": "unreadable"}
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.fleet.collectors import project_status
        root_real = os.path.realpath(str(root))
        items = []
        for item in view["items"]:
            if not human_wait_state.person_wait(item) or item.get("stale") in (True, "older"):
                continue
            route_id = item.get("route_id")
            here = isinstance(route_id, str) and _ROUTE_FILE.match(route_id + ".json") is not None \
                and (Path(root) / ".runtime" / "routes" / (route_id + ".json")).is_file()
            if not here and item.get("cwd"):
                found = project_status.artifact_root(item["cwd"])
                here = bool(found) and os.path.realpath(str(found)) == root_real
            if here:
                items.append(_first_line(item.get("question")) or "질문 (본문 없음)")
        return {"status": "ok", "at": float(view["observed_at"]), "items": items}
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


def _load_cache(root, campaign_id):
    value = store._read_json(Path(root) / CACHE_REL / f"{campaign_id}.json")
    if (not isinstance(value, dict) or value.get("v") != 1 or not isinstance(value.get("members"), dict)
            or not isinstance(value.get("facts"), dict) or not isinstance(value.get("titles"), dict)):
        return None
    return value


def _cache_member(entry):
    return isinstance(entry, dict) and isinstance(entry.get("slug"), str)


def collect(root, campaign_dir, *, now, waits, cache=None, persist=False, listing=None, new_records=None, tz=None):
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

    doing = {f"pr:{pr}" for pr in (pr_from_slug(members[rid]["slug"]) for rid in opened
                                   if kinds[rid] not in ("frame", "publish")) if pr}

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
                          "st": member["st"], "pr": pr_from_slug(member["slug"]), "t": cached_titles.get(rid),
                          "activity": _activity(root, rid, member)})
    for row in fact_rows:
        row["t"] = cached_titles.get(row["rid"])

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


def _write_document(campaign_dir, text):
    """Create `campaign.md`, or replace it when it is a regular file that starts with MARKER.
    True when this campaign's folder holds the generator's document afterwards."""
    path = Path(campaign_dir) / DOCUMENT
    try:
        meta = os.lstat(path)
    except FileNotFoundError:
        meta = None
    if meta is not None:
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


def catch_up(root, cycle_id=None, *, now=None, waits=None):
    """Bring the documents of the campaigns that moved since the last pass up to date.

    A campaign moved when one of its routes was written or closed after the saved mark (the
    first pass looks back ten minutes), or when `cycle_id` is its cycle. Only campaigns that are
    still active are written. One campaign failing never stops another. Returns the names of
    the campaign folders whose document was written or already current."""
    root = Path(root)
    if not (root / ".runtime" / "routes").is_dir():
        return []
    started = time.time_ns() if now is None else int(now * 10 ** 9)  # `now` is the pass's clock
    now = time.time() if now is None else now
    listing = _list_routes(root)
    mark = (store._read_json(root / CACHE_REL / "watermark.json") or {}).get("ns")
    mark = mark if isinstance(mark, int) and not isinstance(mark, bool) else started - FIRST_WINDOW_NS
    changed = {rid for rid, (route, closed) in listing.items() if max(route, closed or 0) > mark}
    records = store.list_cycle_records(root, route_ids=changed) if changed else []
    if isinstance(cycle_id, str) and cycle_id:
        hinted = store.read_cycle_record(root, cycle_id)
        if hinted is not None and all(r.get("cycle_id") != cycle_id for r in records):
            records.append(hinted)
    by_campaign = {}
    for record in records:
        if isinstance(record.get("campaign_id"), str):
            by_campaign.setdefault(record["campaign_id"], []).append(record)
    written = []
    if by_campaign:
        board = waits if waits is not None else read_waits(root, now)
        for campaign_id, rows in sorted(by_campaign.items()):
            try:
                folder = _campaign_folder(root, campaign_id)
                record = store._read_json(folder / "campaign.json")
                if not record or record.get("campaign_id") != campaign_id or record.get("state") != "active":
                    continue
                text, cache = collect(root, folder, now=now, waits=board, cache=_load_cache(root, campaign_id),
                                      persist=True, listing=listing, new_records=rows)
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
