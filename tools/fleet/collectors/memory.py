"""F-19 memory observability collector (agent-fleet-dashboard PRD §4.6).

Read-only tail of two logs the Unified Memory System already writes for its own purposes
(Cluster J D-37 write-events.jsonl + the pre-existing deleted-records.jsonl graveyard) — F-1
zero-injection holds, fleet never writes here and never drives `mem` itself (Non-goal).

Tolerant by construction: missing files, malformed JSON lines, or an unreadable sqlite DB all
degrade to a partial/empty result — never raise. Format is a contract shared with
tools/memory/mem.py (`_append_write_event` / `_graveyard_append`) and
.agent_reports/spec/prd.md §5.12 D-37 — keep both in sync on change.
"""
import datetime
import json
import os
import re
import sqlite3
from pathlib import Path

# mirrors tools/memory/mem.py DOCTOR_DURABLE_SOFT_CEILING (D-39) — duplicated constant is
# intentional (collector must not import mem.py; different module, same number by contract).
DOCTOR_DURABLE_SOFT_CEILING = 80
DISTILL_STALE_MIN = 24 * 60   # silent-death threshold for no distill activity in an active project
RECENT_LIMIT = 8
ADDED_ACTIONS = ("add", "note")
EXPIRED_ACTIONS = ("lifecycle-expire",)
PRUNED_ACTIONS = ("prune", "delete", "merge")
DISTILL_ACTORS = ("distiller", "curator")
_SELECT_RE = re.compile(r"^select\s+origin=\S+\s+active=\d+.*?\bcwd=(.+?)\s*$")
_DONE_RE = re.compile(r"^project=(.+?)\s+elapsed=(\d+)s\s+status=(\S+)")


def _agent_home():
    if os.environ.get("AGENT_HOME"):
        return Path(os.environ["AGENT_HOME"])
    if os.environ.get("CLAUDE_HOME"):
        return Path(os.environ["CLAUDE_HOME"])
    xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    managed = xdg / "hearting" / "current"
    for neutral in (managed, Path.home() / "hearting", Path.home() / "agent_setting"):
        if neutral.exists():
            return neutral
    return managed


def _store():
    if os.environ.get("MEM_STORE"):
        return Path(os.environ["MEM_STORE"])
    legacy = _agent_home() / "memory"
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return (Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
            / "hearting" / "memory")


def _write_events_path():
    # priority mirrors mem.py WRITE_EVENTS exactly (MEM_WRITE_EVENTS > MEM_STORE-adjacent >
    # XDG state default) so fixture-DB tests never leak into (or read from) the real journal.
    if os.environ.get("MEM_WRITE_EVENTS"):
        return Path(os.environ["MEM_WRITE_EVENTS"])
    if os.environ.get("MEM_STORE"):
        return _store() / "write-events.jsonl"
    return (Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
            / "agent-memory" / "write-events.jsonl")


def _graveyard_path():
    return _store() / "deleted-records.jsonl"


def _db_path():
    return _store() / "memory.db"


def _periodic_curate(now=None):
    """Best-effort snapshot of the currently locked periodic curator batch."""
    try:
        locks = list(_store().glob(".distill-lock-periodic-*"))
        if not locks:
            return None
        log = Path(os.environ.get(
            "FLEET_PERIODIC_CURATE_LOG",
            str(Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
                / "hearting" / "periodic-curate.log"),
        ))
        selected, completed = [], []
        with log.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("==="):
                    selected, completed = [], []
                    continue
                match = _SELECT_RE.match(line)
                if match:
                    selected.append(match.group(1))
                    continue
                match = _DONE_RE.match(line)
                if match:
                    completed.append(match.group(1))
        if not selected:
            return None
        done = min(len(completed), len(selected))
        current = selected[min(done, len(selected) - 1)]
        now_ts = (now.timestamp() if isinstance(now, datetime.datetime) else
                  float(now) if isinstance(now, (int, float)) else
                  datetime.datetime.now().timestamp())
        started = min(lock.stat().st_mtime for lock in locks)
        return {"done": done, "total": len(selected), "current_cwd": current,
                "elapsed_s": max(0, int(now_ts - started))}
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def _read_jsonl_tail(path):
    """Return (list-of-dicts, existed). Malformed lines are skipped; a read failure
    degrades to (empty-so-far, existed) rather than raising."""
    if not path.exists():
        return [], False
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out, True


def _parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _local_midnight():
    now = datetime.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_today(ts, midnight):
    dt = _parse_ts(ts)
    return dt is not None and dt >= midnight


def _repo_key(event):
    """Grouping key for F-19 per-repo rows (prd.md §4.7 F-19 extension, 사용자 확정
    2026-07-16). Only trusts fields the journal actually carries — `cwd` (mapped through
    the same `project_of` grouping key the board's own cwd-project cards use) or a literal
    `project` field. Current writers emit `cwd`; `project` remains a tolerant compatibility
    field. Historical rows without either field degrade to no grouping (honest omission, F-3)
    rather than guessing. Grouping runs over the whole journal tail and each repo keeps its
    latest RECENT_LIMIT rows; that replaced the 2026-07-16 today-only rule on 2026-11-02
    (사용자 결정)."""
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        try:
            from ..model import project_of
            return project_of(cwd)
        except Exception:
            return None
    project = event.get("project")
    return project if isinstance(project, str) and project else None


def _durable_over(soft_ceiling=DOCTOR_DURABLE_SOFT_CEILING):
    """D-39 ⑥ durable soft-ceiling check, read-only — [(cwd_origin, count), ...] over ceiling.
    Any DB access failure (absent/locked/corrupt) degrades to []."""
    path = _db_path()
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA query_only=1")
            rows = con.execute(
                "SELECT cwd_origin, COUNT(*) c FROM records WHERE tier='durable' "
                "AND scope='project' GROUP BY cwd_origin HAVING c > ?", (soft_ceiling,),
            ).fetchall()
            return [(r[0] or "?", r[1]) for r in rows]
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []


DECISIONS_PER_PROJECT = 3
OPEN_PER_PROJECT = 200
# Records that may be open work: the obligation and backlog types, a ``backlog`` tag, or a
# ``[marker]`` headline (the explorer reads the marker). Everything else is not read.
_OPEN_TYPES = ("unresolved-obligation", "backlog", "task", "todo", "obligation")
_project_cache = {}


# Kinds that record a finished outcome, an observation or a pointer, never open work by
# themselves; a ``backlog`` tag on them names the topic, not a pending item.
_FINISHED_TYPES = frozenset(("completion", "observation", "artifact-pointer", "feedback",
                             "user-correction", "decision"))
_FINISHED_TAGS = frozenset(("resolved", "done", "closed"))
_OPEN_MARKER_WORDS = ("백로그", "진행", "미확인", "부분 설치", "미해결", "수리")
_HEADLINE_MARKER = re.compile(r"^\s*\[([^\]]{1,40})\]")
OPEN_STAGES = ("doing", "waiting", "open")


def _refers_to_route(route_id, references):
    if not route_id:
        return False
    for reference in references or ():
        text = str(reference)
        if text == route_id or re.search(r"(?<![A-Za-z0-9])" + re.escape(route_id) +
                                         r"(?![A-Za-z0-9])", text):
            return True
    return False


def open_stage(record, routes=()):
    """How far a possibly-open record got: ``doing``, ``waiting``, ``open`` or None (not open work).

    Only what the record and the routes already say is used: its type, tags, headline marker
    (``[진행 중]``, ``[설치 완료·실사용 미확인]``, ``[해결 확인]`` …) and an exact route link.
    An obligation or backlog record with neither stays open on record, never "in progress".
    """
    headline = record.get("headline")
    match = _HEADLINE_MARKER.match(headline if isinstance(headline, str) else "")
    marker = match.group(1) if match else ""
    if "해결" in marker and "미해결" not in marker:
        return None
    rtype = str(record.get("type") or "").casefold()
    tags = {str(tag).strip("[]\"' ").casefold() for tag in record.get("tags") or ()}
    if rtype in _FINISHED_TYPES or tags & _FINISHED_TAGS:
        return None
    if not (rtype in _OPEN_TYPES or "backlog" in tags
            or any(word in marker for word in _OPEN_MARKER_WORDS)):
        return None
    refs = tuple(record.get("links") or ()) + tuple(record.get("artifact_refs") or ())
    linked = [route for route in routes if _refers_to_route(str(route.get("id") or ""), refs)]
    if "진행" in marker or any(route.get("state") in ("working", "blocked") or
                              any(stage.get("st") == "active" for stage in route.get("stages") or ())
                              for route in linked):
        return "doing"
    if "미확인" in marker or "부분 설치" in marker or any(
            route.get("state") == "done" and route.get("outcome") == "success" for route in linked):
        return "waiting"
    return "open"


# Harness differences the docs still carry (core/ADAPTATION.md, "Open harness
# differences").  Each open row is shown with the project's open records on the
# Fleet screen and its counts, so a documented difference stays visible instead of
# being closed by wording such as "separate decision" or "known difference".  The
# session work list (periodic steward check, session shell) does not read them:
# a register row is not a task for every supervising session.
PARITY_REGISTER = Path("core") / "ADAPTATION.md"
PARITY_PROJECT = "hearting"
_REGISTER_START = "<!-- parity-register:start -->"
_REGISTER_END = "<!-- parity-register:end -->"
_REGISTER_STATES = {"open": "미결", "needs-user-decision": "결정 필요"}
_REGISTER_CLOSED = frozenset({"resolved", "limit"})
_REGISTER_LINK = re.compile(r"https?://[^\s)>\]|]+")
_parity_cache = {}


def register_row_closed(state, follow):
    """``resolved`` closes a row; ``limit`` closes it only when ``Next`` links its evidence.

    A ``limit`` without a link is the documented definition unmet, so it stays open
    rather than letting one word hide a fixable difference.
    """
    state = state.strip("`").casefold()
    if state == "limit":
        return bool(_REGISTER_LINK.search(follow))
    return state in _REGISTER_CLOSED


def _register_path():
    return _agent_home() / PARITY_REGISTER


def parity_open_items(path=None):
    """Open rows of the parity register as open-record dicts (headline only).

    Row shape: ``| PG-n | harnesses | user-visible effect | state | next |``.  Only
    ``resolved`` and a ``limit`` whose ``Next`` links its evidence close a row; any
    other state, a typo or an unlinked ``limit`` included, stays open so a row
    cannot disappear unclosed.  A missing or unreadable register yields none.
    """
    path = Path(path) if path is not None else _register_path()
    try:
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
    except OSError:
        return []
    if _parity_cache.get("key") == key:
        return [dict(item) for item in _parity_cache["value"]]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    start, end = text.find(_REGISTER_START), text.find(_REGISTER_END)
    items = []
    if start >= 0 and end > start:
        updated = datetime.datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
        for line in text[start:end].splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 5 or not re.fullmatch(r"PG-\d{1,4}", cells[0]):
                continue
            ident, harnesses, effect, state, follow = cells[:5]
            state = state.strip("`").casefold()
            if register_row_closed(state, follow):
                continue
            label = "limit 근거 없음" if state == "limit" else _REGISTER_STATES.get(state, "미결")
            tags = ("parity", "backlog") + (("user-decision",) if state == "needs-user-decision" else ())
            items.append({
                "id": f"parity:{ident}", "type": "unresolved-obligation",
                "headline": f"[{label}] {ident} {harnesses}: {effect}"[:240],
                "updated": updated, "tags": tags,
                "links": (), "artifact_refs": (f"{PARITY_REGISTER}#{ident}"[:512],),
                "next": follow[:240],
            })
    _parity_cache.update(key=key, value=items)
    return [dict(item) for item in items]


def _json_strings(value, limit=80):
    if isinstance(value, (list, tuple)):
        items = value
    else:
        try:
            items = json.loads(value) if isinstance(value, str) else ()
        except (TypeError, ValueError):
            items = ()
    if not isinstance(items, (list, tuple)):
        return ()
    return tuple(text.strip()[:limit] for text in items
                 if isinstance(text, str) and text.strip())[:32]


def _repo_label(origin):
    """The project name of a ``cwd_origin``; encoded (``id:``/``root:``/``-…``) origins have none."""
    if not isinstance(origin, str) or not origin or origin.startswith(("id:", "root:", "-")):
        return None
    if origin.startswith("git:"):
        return origin.rsplit("/", 1)[-1] or None
    return origin.rstrip("/").rsplit("/", 1)[-1] or None


def _db_stamp(path):
    stamp = []
    for name in (path, path.with_name(path.name + "-wal")):
        try:
            info = name.stat()
        except OSError:
            stamp.append(None)
        else:
            stamp.append((info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def _read_project_memory(today, journal_ids=(), decisions=DECISIONS_PER_PROJECT,
                         open_limit=OPEN_PER_PROJECT, repo=None, parity=True):
    """Per project, the newest decisions and the open records, headline only.

    Open candidates are classified with ``open_stage`` before the ``open_limit`` cut, so
    ``open_total`` is every open record of the project and ``limited`` says the listed
    ``open`` rows are only the newest ``open_limit`` of them.

    One read-only query ranks records within each (origin, kind) so every project keeps its
    own newest rows; record bodies are never selected. ``headlines`` maps the given journal
    ids to their stored headline. The result is reused while the database files are unchanged.
    ``parity=False`` leaves out the parity register rows (the session work list).
    """
    path = _db_path()
    if not path.is_file():
        return {"state": "unavailable", "by_repo": {}, "record_count": None}
    scope_repo = repo   # the row loop below reuses ``repo`` for each record's project
    register = _register_path()
    key = (str(path), _db_stamp(path), _db_stamp(register), today, tuple(journal_ids),
           decisions, open_limit, repo, parity)
    cached = _project_cache.get("value")
    if cached is not None and _project_cache.get("key") == key:
        return dict(cached)
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA query_only=1")
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "records" not in tables:
                return {"state": "unavailable", "by_repo": {}, "record_count": None}
            columns = {row[1] for row in con.execute("PRAGMA table_info(records)").fetchall()}
            if not {"id", "scope", "cwd_origin", "type", "headline", "updated"}.issubset(columns):
                return {"state": "incomplete", "by_repo": {}, "record_count": None}
            fields = ("id", "canonical_id", "cwd_origin", "type", "tags", "links", "artifact_refs",
                      "headline", "updated", "created")
            select = ", ".join(name if name in columns else f"NULL AS {name}" for name in fields)
            where = ["scope='project'"]
            if "status" in columns:
                where.append("status='active'")
            if "injection_flag" in columns:
                where.append("(injection_flag IS NULL OR injection_flag=0)")
            types = ", ".join("?" for _ in _OPEN_TYPES)
            tags = "tags LIKE '%backlog%' OR " if "tags" in columns else ""
            where.append(f"(type='decision' OR type IN ({types}) OR {tags}headline LIKE '[%')")
            scope_params = ()
            if repo is not None:
                # The session block reads one repo through the same row projection.
                # Do not sort/materialize every project's work on its first frame.
                con.create_function("hearting_repo", 1, _repo_label, deterministic=True)
                where.append("hearting_repo(cwd_origin) = ?")
                scope_params = (repo,)
            rows = con.execute(
                f"SELECT {', '.join(fields)}, kind, total, today_count FROM ("
                f" SELECT *, ROW_NUMBER() OVER (PARTITION BY cwd_origin, kind"
                f"   ORDER BY updated DESC, seq DESC) AS rank,"
                f"  COUNT(*) OVER (PARTITION BY cwd_origin, kind) AS total,"
                f"  SUM(CASE WHEN created >= ? THEN 1 ELSE 0 END)"
                f"   OVER (PARTITION BY cwd_origin, kind) AS today_count"
                f" FROM (SELECT {select}, rowid AS seq,"
                f"  CASE WHEN type='decision' THEN 'decision' ELSE 'open' END AS kind"
                f"  FROM records WHERE {' AND '.join(where)}))"
                # Open candidates are not cut here: finished records are dropped by
                # open_stage() below, so a raw cut could hide real open work behind them.
                f" WHERE kind='open' OR rank <= ?"
                f" ORDER BY cwd_origin, kind, rank",
                (today, *_OPEN_TYPES, *scope_params, max(0, int(decisions))),
            ).fetchall()
            ids = [str(item) for item in journal_ids if item][:256]
            headlines = dict(con.execute(
                f"SELECT id, headline FROM records WHERE id IN ({', '.join('?' for _ in ids)})",
                ids).fetchall()) if ids else {}
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {"state": "failed", "by_repo": {}, "record_count": None}

    by_repo, seen, limited = {}, set(), False
    for values in rows:
        (rid, canonical, origin, rtype, tags_raw, links_raw, refs_raw, headline, updated,
         _created, kind, total, today_count) = values
        repo = _repo_label(origin)
        canonical = canonical or rid
        if repo is None or not rid or canonical in seen:
            continue
        seen.add(canonical)
        entry = by_repo.setdefault(repo, {"decisions": [], "decisions_today": 0, "open": [],
                                          "limited": False, "updated": ""})
        record = {"id": str(rid)[:256], "type": str(rtype or "")[:80],
                  "headline": str(headline or "")[:240], "updated": str(updated or "")[:40]}
        if kind == "decision":
            entry["decisions"].append(record)
            if origin not in entry.setdefault("_origins", set()):   # once per merged origin
                entry["_origins"].add(origin)
                entry["decisions_today"] += int(today_count or 0)
        else:
            record.update(tags=_json_strings(tags_raw), links=_json_strings(links_raw, 256),
                          artifact_refs=_json_strings(refs_raw, 512))
            if open_stage(record) is None:   # finished, observed or resolved: not open work
                continue
            entry["open"].append(record)
        entry["updated"] = max(entry["updated"], record["updated"])
    if parity and (scope_repo is None or scope_repo == PARITY_PROJECT):
        parity = parity_open_items(register)
        if parity:
            entry = by_repo.setdefault(PARITY_PROJECT, {"decisions": [], "decisions_today": 0,
                                                        "open": [], "limited": False,
                                                        "updated": ""})
            entry["open"].extend(parity)
            entry["updated"] = max(entry["updated"], *(item["updated"] for item in parity))
    for entry in by_repo.values():
        entry.pop("_origins", None)
        # Origins that share a project name merge; the newest rows win across them.
        entry["decisions"].sort(key=lambda item: item["updated"], reverse=True)
        del entry["decisions"][max(0, int(decisions)):]
        entry["open"].sort(key=lambda item: item["updated"], reverse=True)
        # The total counts every open record; only the listed rows are bounded.
        entry["open_total"] = len(entry["open"])
        if entry["open_total"] > max(1, int(open_limit)):
            entry["limited"] = limited = True
            del entry["open"][max(1, int(open_limit)):]
    result = {"state": "success", "by_repo": by_repo, "limited": limited,
              "record_count": sum(len(e["decisions"]) + len(e["open"]) for e in by_repo.values()),
              "headlines": {str(rid): str(text)[:240] for rid, text in headlines.items() if text}}
    _project_cache.update(key=key, value=result)
    return dict(result)


_latest_cache = {}
SAVED_ACTIONS = ("add", "supersede")


def _read_saved_journal(path):
    """The journal's rows for ``latest_saved``. Undecodable bytes (a half-written line) become
    replacement characters, so one broken byte costs its own line, not the panel; an unreadable
    journal reads as empty (spec E9 rule 37 (b): no journal, no row)."""
    out = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except (OSError, ValueError):
        return []
    return out


def latest_saved(repo):
    """The newest ``add``/``supersede`` journal row of one project (``by_repo``'s grouping key),
    or None, with the journal path. Read-only; the journal is read again only when its size or
    modification time changed, so a panel projection with an unchanged journal reads nothing
    but its stat. Bodies are never carried: the row's id, time, type and its bounded snippet.
    The journal is shared by every repository and each panel reads it again on any write, so a
    re-read asks each cwd's grouping key once (a key walks the directories up to `.git`)."""
    path = _write_events_path()
    try:
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None, str(path)
    if _latest_cache.get("stamp") != stamp:
        newest, keys = {}, {}
        for event in _read_saved_journal(path):
            if event.get("action") not in SAVED_ACTIONS:
                continue
            cwd = event.get("cwd")
            if isinstance(cwd, str) and cwd:
                if cwd not in keys:
                    keys[cwd] = _repo_key(event)
                key = keys[cwd]
            else:
                key = _repo_key(event)
            if key and (key not in newest or str(event.get("ts") or "") >= str(newest[key].get("ts") or "")):
                newest[key] = {name: event.get(name) for name in ("ts", "action", "type", "snippet", "id")}
        _latest_cache.update(stamp=stamp, rows=newest)
    row = (_latest_cache.get("rows") or {}).get(repo)
    return (dict(row) if row else None), str(path)


def collect(now=None, *, include_summary=True, include_backlog=False):
    """Return an additive `memory` dict (fleet.py --json / render.py consume it), or None
    when neither the journal nor the graveyard exists (F-19 not wired up yet — panel omitted).

    ``include_summary=False`` reads the write journal. ``include_backlog=True`` adds the
    Explorer's read-only per-project DB summary (newest decisions, possibly-open records) and
    the stored headline of each journal row; the default collector surface stays journal-only.

    ``by_repo`` is the latest RECENT_LIMIT journal rows per project, not today's rows: the
    2026-11-02 사용자 결정 replaced the 2026-07-16 today-only display rule. ``today`` keeps
    its name and its midnight boundary; only the per-repo display group widened.
    """
    now = now or datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    events, journal_ok = _read_jsonl_tail(_write_events_path())
    today_events = [e for e in events if _is_today(e.get("ts"), midnight)]
    recent = [{key: e.get(key) for key in
               ("ts", "action", "tier", "type", "actor", "sid", "snippet", "id", "cwd", "project", "scope")}
              for e in events[-RECENT_LIMIT:][::-1]]
    by_repo = {}
    for e in events:
        rk = _repo_key(e)
        if rk:
            by_repo.setdefault(rk, []).append({key: e.get(key) for key in
                ("ts", "action", "tier", "type", "actor", "sid", "snippet", "id", "cwd", "project", "scope")})
    for rows in by_repo.values():
        rows.sort(key=lambda e: e.get("ts") or "", reverse=True)
        del rows[RECENT_LIMIT:]
    # Renderers only consume journal/project events. Keep the default aggregate API
    # for structured consumers without touching its DB, graveyard or curator in this mode.
    if not include_summary:
        if not include_backlog:
            return {"journal_available": journal_ok, "recent": recent,
                    "by_repo": by_repo} if journal_ok else None
        journal_ids = sorted({str(row["id"]) for rows in by_repo.values() for row in rows
                              if row.get("id")})
        database = _read_project_memory(now.date().isoformat(), journal_ids)
        headlines = database.pop("headlines", None) or {}
        for rows in by_repo.values():
            for row in rows:
                if row.get("id") in headlines:
                    row["headline"] = headlines[row["id"]]
        return {"journal_available": journal_ok, "recent": recent,
                "by_repo": by_repo, "database": database}
    graveyard, graveyard_ok = _read_jsonl_tail(_graveyard_path())
    periodic_curate = _periodic_curate(now)
    if not journal_ok and not graveyard_ok and periodic_curate is None:
        return None

    if journal_ok:
        added_w = sum(1 for e in today_events
                      if e.get("action") in ADDED_ACTIONS and e.get("tier") == "working")
        added_d = sum(1 for e in today_events
                      if e.get("action") in ADDED_ACTIONS and e.get("tier") == "durable")
        expired = sum(1 for e in today_events if e.get("action") in EXPIRED_ACTIONS)
        pruned = sum(1 for e in today_events if e.get("action") in PRUNED_ACTIONS)
        distill_events = [e for e in events if e.get("actor") in DISTILL_ACTORS]
        last_distill_ts = distill_events[-1]["ts"] if distill_events else None
    else:
        # journal absent — degrade to graveyard-only delete-side visibility (add side has no
        # source without the journal; last-distill is unknowable without the `actor` field).
        added_w = added_d = 0
        today_grave = [g for g in graveyard if _is_today(g.get("_deleted_at"), midnight)]
        expired = sum(1 for g in today_grave if g.get("_action") in EXPIRED_ACTIONS)
        pruned = sum(1 for g in today_grave if g.get("_action") in PRUNED_ACTIONS)
        last_distill_ts = None

    last_distill_min = None
    if last_distill_ts:
        dt = _parse_ts(last_distill_ts)
        if dt is not None:
            last_distill_min = max(0, int((now - dt).total_seconds() // 60))

    durable_over = _durable_over()
    distill_stale = last_distill_min is not None and last_distill_min > DISTILL_STALE_MIN

    return {
        "journal_available": journal_ok,
        "graveyard_available": graveyard_ok,
        "today": {
            "added_working": added_w,
            "added_durable": added_d,
            "added": added_w + added_d,
            "expired": expired,
            "pruned": pruned,
        },
        "last_distill_min": last_distill_min,
        "recent": recent,
        "by_repo": by_repo,
        "alerts": {
            "durable_over": durable_over,
            "distill_stale": distill_stale,
        },
        "periodic_curate": periodic_curate,
    }
