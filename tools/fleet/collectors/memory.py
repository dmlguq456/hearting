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


_BACKLOG_RECORD_LIMIT = 512
_BACKLOG_TYPES = frozenset(("backlog", "task", "todo", "obligation"))


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
    if not isinstance(origin, str) or not origin:
        return "·"
    if origin.startswith("git:"):
        return origin.rsplit("/", 1)[-1] or "·"
    if origin.startswith("-"):
        return "·"                         # encoded cwd values are not display labels
    return origin.rstrip("/").rsplit("/", 1)[-1] or "·"


def _read_backlog_records(limit=_BACKLOG_RECORD_LIMIT):
    """Read explicit backlog metadata from memory.db without opening record bodies."""
    path = _db_path()
    if not path.is_file():
        return {"state": "unavailable", "by_repo": {}, "record_count": None}
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA query_only=1")
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "records" not in tables:
                return {"state": "unavailable", "by_repo": {}, "record_count": None}
            columns = {row[1] for row in con.execute("PRAGMA table_info(records)").fetchall()}
            metadata_contract = {"id", "tier", "scope", "cwd_origin", "type", "source", "tags",
                                 "links", "artifact_refs", "status", "canonical_id"}
            if not metadata_contract.issubset(columns):
                return {"state": "incomplete", "by_repo": {}, "record_count": None}
            fields = ("id", "tier", "scope", "type", "cwd_origin", "source", "tags", "links",
                      "artifact_refs", "status", "canonical_id", "superseded_by", "updated",
                      "headline", "delivery_state", "injection_flag")
            select = ", ".join(name if name in columns else f"NULL AS {name}" for name in fields)
            where = ["scope='project'"]
            if "status" in columns:
                where.append("status='active'")
            if "injection_flag" in columns:
                where.append("(injection_flag IS NULL OR injection_flag=0)")
            order = " ORDER BY updated DESC, id" if "updated" in columns else " ORDER BY id"
            rows = con.execute(
                f"SELECT {select} FROM records WHERE {' AND '.join(where)}{order} LIMIT ?",
                (max(1, min(int(limit), _BACKLOG_RECORD_LIMIT)),),
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return {"state": "failed", "by_repo": {}, "record_count": None}

    by_repo, seen = {}, set()
    for values in rows:
        (rid, tier, scope, rtype, origin, source, tags_raw, links_raw, refs_raw, status,
         canonical, superseded_by, updated, headline, delivery, _injection) = values
        tags = _json_strings(tags_raw)
        if not any(tag.casefold() == "backlog" for tag in tags) and str(rtype or "").casefold() not in _BACKLOG_TYPES:
            continue
        canonical = canonical or rid
        if not rid or canonical in seen:
            continue
        seen.add(canonical)
        repo = _repo_label(origin)
        record = {
            "id": str(rid)[:256], "canonical_id": str(canonical)[:256],
            "repo": repo, "cwd_origin": str(origin or "")[:512],
            "tier": str(tier or "")[:32], "type": str(rtype or "")[:80],
            "source": str(source or "")[:240], "tags": tags,
            "links": _json_strings(links_raw, 256),
            "artifact_refs": _json_strings(refs_raw, 512),
            "headline": str(headline or "")[:240], "updated": str(updated or "")[:40],
            "status": str(status or "unknown")[:32],
            "superseded_by": str(superseded_by or "")[:256],
            "delivery_state": str(delivery or "unknown")[:32],
        }
        by_repo.setdefault(repo, []).append(record)
    count = sum(len(records) for records in by_repo.values())
    return {"state": "success", "by_repo": by_repo, "record_count": count,
            "limited": len(rows) >= max(1, min(int(limit), _BACKLOG_RECORD_LIMIT))}


def collect(now=None, *, include_summary=True, include_backlog=False):
    """Return an additive `memory` dict (fleet.py --json / render.py consume it), or None
    when neither the journal nor the graveyard exists (F-19 not wired up yet — panel omitted).

    ``include_summary=False`` reads the write journal. ``include_backlog=True`` adds bounded,
    read-only DB metadata for the Explorer; the default collector surface stays journal-only.

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
        database = _read_backlog_records()
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
