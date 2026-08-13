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
    for neutral in (Path.home() / "hearting", Path.home() / "agent_setting"):
        if neutral.exists():
            return neutral
    return Path.home() / ".claude"


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
    rather than guessing."""
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


def collect(now=None):
    """Return an additive `memory` dict (fleet.py --json / render.py consume it), or None
    when neither the journal nor the graveyard exists (F-19 not wired up yet — panel omitted).
    """
    now = now or datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    events, journal_ok = _read_jsonl_tail(_write_events_path())
    graveyard, graveyard_ok = _read_jsonl_tail(_graveyard_path())
    periodic_curate = _periodic_curate(now)
    if not journal_ok and not graveyard_ok and periodic_curate is None:
        return None

    if journal_ok:
        today_events = [e for e in events if _is_today(e.get("ts"), midnight)]
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

    recent = []
    by_repo = {}
    if journal_ok:
        for e in events[-RECENT_LIMIT:][::-1]:
            recent.append({
                "ts": e.get("ts"), "action": e.get("action"), "tier": e.get("tier"),
                "type": e.get("type"), "actor": e.get("actor"), "sid": e.get("sid"),
                "snippet": e.get("snippet"),
            })
        # F-19 repo extension: today's events, grouped by repo when the journal row names
        # one (see `_repo_key`) — additive, most-recent-first per repo.
        for e in today_events:
            rk = _repo_key(e)
            if not rk:
                continue
            by_repo.setdefault(rk, []).append({
                "ts": e.get("ts"), "action": e.get("action"), "tier": e.get("tier"),
                "type": e.get("type"), "actor": e.get("actor"), "sid": e.get("sid"),
                "snippet": e.get("snippet"),
            })
        for rk in by_repo:
            by_repo[rk].sort(key=lambda x: x.get("ts") or "", reverse=True)

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
