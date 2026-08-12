#!/usr/bin/env python3
"""OpenCode dispatch liveness check using OpenCode SQLite session state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import anchored_capacity_failure  # noqa: E402
from dispatch_contract import resolve_agent_home as _resolve_agent_home  # noqa: E402
from tools.fleet.model import (  # noqa: E402
    ATTEMPT_CLASSIFIER_SOURCE,
    classify_attempt_evidence,
)

# SD-15 (OPERATIONS §5.10 ⑨): if an open row's dispatch log shows a limit/auth death
# pattern, judge it DEAD regardless of the SQLite session mtime — this is the axis that
# catches OpenCode's hang-on-limit (#8203), which the wrapper's launch watch cannot.
# Kept in sync with dispatch-headless.py DEATH_PATTERNS (intentional duplication).
LIMIT_RE = re.compile(
    r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b|"
    r"hit your (session|usage) limit|session limit reached|usage[_ ]limit[_ ]reached|"
    r"usage limit reached|weekly limit|rate limit(ed)?|provider rate limit|"
    r"exceeded retry limit|[^0-9]429[^0-9]|invalid api key|authentication_error|"
    r"not logged in|please run /login|unauthorized|[^0-9]401[^0-9]|"
    r"credit balance is too low|insufficient (credit|quota|funds)",
    re.I,
)


def log_shows_limit(agent_home: Path, slug: str) -> Path | None:
    """Return the dispatch log path if a terse trailing line shows a limit/auth death.

    SD-15b (OPERATIONS §8.6.1): anchor the match. A real CLI limit death prints a terse
    standalone error line (e.g. "You've hit your session limit · resets 3pm") at the log
    tail and exits; a completion report that merely *discusses* limits would false-positive
    a whole-tail scan. So we only look at the last few non-empty lines and only accept a
    match on a short/standalone line. The fresh-signal completion check is applied by main()
    (a fresh SQLite session or heartbeat never reaches this function).
    """
    if not slug:
        return None
    log_dir = agent_home / ".dispatch" / "logs"
    for lf in sorted(log_dir.glob(f"{slug}.*")):
        if not lf.is_file():
            continue
        try:
            with lf.open("rb") as fh:
                try:
                    fh.seek(-8000, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        nonempty = [ln for ln in tail.splitlines() if ln.strip()]
        for ln in nonempty[-3:]:
            match = LIMIT_RE.search(ln) if len(ln) <= 200 else None
            if match and ("capacity" not in match.group(0).lower() or anchored_capacity_failure(ln)):
                return lf
    return None


def usage() -> int:
    print("usage: dispatch-liveness.py [jobs.log]", file=sys.stderr)
    return 64


def current_job_lines(jobs: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
         "liveness", "--jobs", str(jobs)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "current-view-failed")
    return result.stdout.splitlines()


def same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return os.path.abspath(a) == os.path.abspath(b)


def db_path() -> Path:
    explicit = os.environ.get("OPENCODE_DB")
    if explicit:
        return Path(explicit)
    data_home = os.environ.get("OPENCODE_DATA_HOME")
    if data_home:
        return Path(data_home) / "opencode.db"
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def to_seconds(ts: int | float | None) -> float:
    if ts is None:
        return 0.0
    return float(ts) / 1000.0 if ts > 10_000_000_000 else float(ts)


def locate_latest_for_worktree(con: sqlite3.Connection, worktree: str) -> tuple[str, str, float] | None:
    rows = con.execute(
        """
        SELECT
          s.id,
          s.slug,
          s.directory,
          MAX(
            s.time_updated,
            COALESCE((SELECT MAX(time_updated) FROM message WHERE session_id = s.id), 0),
            COALESCE((SELECT MAX(time_updated) FROM part WHERE session_id = s.id), 0),
            COALESCE((SELECT MAX(time_updated) FROM session_message WHERE session_id = s.id), 0),
            COALESCE((SELECT MAX(time_created) FROM session_input WHERE session_id = s.id), 0)
          ) AS last_updated
        FROM session s
        ORDER BY last_updated DESC
        """,
    )
    for row in rows:
        if same_path(row["directory"], worktree):
            return row["id"], row["slug"] or "", to_seconds(row["last_updated"])
    return None


def heartbeat_age_min(agent_home: Path, slug: str, now: float) -> float | None:
    """Return the heartbeat file age in minutes, or None when no heartbeat exists.

    The plugin hearting-guards.js touches
    <agent-home>/.dispatch/logs/<slug>.heartbeat on every session.idle event
    when OPENCODE_DISPATCH_SLUG is set (i.e., the session is a dispatched
    headless worker). A missing or aging heartbeat is a secondary liveness
    signal: it never overrides a fresher OpenCode SQLite mtime, but when the
    SQLite mtime is inconclusive or the working session cannot be located, a
    recent heartbeat is evidence the plugin loaded and reached idle.
    """
    if not slug:
        return None
    hb = agent_home / ".dispatch" / "logs" / f"{slug}.heartbeat"
    try:
        mtime = hb.stat().st_mtime
    except OSError:
        return None
    return (now - mtime) / 60.0


def plugin_loaded_for_slug(agent_home: Path, slug: str) -> bool:
    """True when the plugin-load marker for this dispatch slug exists.

    dispatch-headless.py exports OPENCODE_DISPATCH_SLUG to the runtime child;
    hearting-guards.js writes <agent-home>/.dispatch/plugin-load.<slug>.mark
    once at plugin init. A missing marker means the plugin did not load in the
    headless runtime — a strong DEAD signal even when the OpenCode SQLite
    session is alive (the session is running but no harness guards are active).
    """
    if not slug:
        return False
    return (agent_home / ".dispatch" / f"plugin-load.{slug}.mark").is_file()


def attempt_heartbeat(agent_home: Path, metadata: dict[str, str]) -> dict | None:
    attempt = metadata.get("attempt_id", "").replace("/", "_")
    if not attempt:
        return None
    path = agent_home / ".dispatch" / "heartbeats" / f"{attempt}.json"
    try:
        if path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def exact_attempt_state(pipe: str, now: float, agent_home: Path) -> dict | None:
    metadata = dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)
    raw, expected = metadata.get("pid", ""), metadata.get("pid_start", "")
    if not raw.isdigit() or not expected:
        return None
    actual = ""; alive = False
    try:
        actual = (Path("/proc") / raw / "stat").read_text(encoding="utf-8").split()[21]
        alive = True
    except (OSError, IndexError):
        pass
    return classify_attempt_evidence({
        "pid": int(raw), "proc_start": expected, "actual_proc_start": actual,
        "pid_alive": alive, "proc_start_match": bool(alive and actual == expected),
        "pid_scope": metadata.get("pid_scope"),
        "attempt_id": metadata.get("attempt_id"), "route_id": metadata.get("route_id"),
        "route_node": metadata.get("route_node"),
        "heartbeat": attempt_heartbeat(agent_home, metadata),
    }, now)


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in {"-h", "--help"}):
        return usage()

    agent_home = resolve_agent_home()
    jobs_override = argv[1] if len(argv) == 2 else os.environ.get("AGENT_DISPATCH_JOBS")
    jobs = Path(jobs_override) if jobs_override else agent_home / ".dispatch" / "jobs.log"
    database = db_path()
    stale_min = int(os.environ.get("DISPATCH_STALE_MIN", "15"))

    if not jobs.is_file():
        print(f"(jobs.log missing: {jobs})")
        return 0
    if not database.is_file():
        print(f"OpenCode DB missing: {database}")
        return 69

    con = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    now = time.time()
    open_n = alive = suspect = 0
    try:
        rows = current_job_lines(jobs)
    except RuntimeError as exc:
        print(f"liveness current-view failed: {exc}", file=sys.stderr)
        return 69
    for raw in rows:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            parts = raw.split("\t")
            while len(parts) < 6:
                parts.append("")
            ts, status, _repo, worktree, slug, pipe = parts[:6]
            if status != "open":
                continue
            open_n += 1
            label = slug or "?"
            exact = exact_attempt_state(pipe, now, agent_home)
            if exact and exact["state"] == "working":
                detail = ("namespace-local exact heartbeat" if exact.get("pid_scope") == "namespace-local"
                          else f"recorded pid {exact['pid']} running")
                print(f"ALIVE    {label} ({detail}; classifier={ATTEMPT_CLASSIFIER_SOURCE})")
                alive += 1
                continue
            if exact and exact["state"] == "dead":
                log_hit = log_shows_limit(agent_home, slug)
                if log_hit is not None:
                    print(f"DEAD     {label} - log limit/auth pattern ({log_hit}) [open: {ts}]")
                else:
                    print(f"EXITED   {label} - recorded pid {exact['pid']} ended or identity changed; classifier={ATTEMPT_CLASSIFIER_SOURCE} [open: {ts}]")
                suspect += 1
                continue
            if exact and exact["state"] == "done":
                print(f"COMPLETED {label} - namespace-local terminal heartbeat awaits registry reconciliation [open: {ts}]")
                suspect += 1
                continue
            match = locate_latest_for_worktree(con, worktree)
            if match is None:
                # No SQLite session for this worktree. Fall back to the heartbeat
                # side-channel: a recent heartbeat means the plugin is still
                # touching it on session.idle even though the OpenCode session row
                # is gone or unreadable.
                hb_age = heartbeat_age_min(agent_home, slug, now)
                plugin_marker = plugin_loaded_for_slug(agent_home, slug)
                if hb_age is not None and hb_age <= stale_min:
                    # SD-15b: fresh heartbeat is a liveness signal — do NOT consult the log
                    # (a report discussing limits must not force DEAD).
                    print(f"ALIVE    {label} (heartbeat {hb_age:.1f}m ago; plugin_loaded={plugin_marker})")
                    alive += 1
                    continue
                # No fresh signal — SD-15b: consult the anchored log scan to name a limit/auth
                # death (also catches hang-on-limit #8203); else fall through to the DEAD reasons.
                log_hit = log_shows_limit(agent_home, slug)
                if log_hit is not None:
                    print(f"DEAD     {label} - log limit/auth pattern ({log_hit}) [open: {ts}]")
                elif not plugin_marker:
                    print(f"DEAD     {label} - OpenCode session not found for {worktree} and no plugin-load marker [open: {ts}]")
                else:
                    print(f"DEAD     {label} - OpenCode session not found for {worktree} [open: {ts}]")
                suspect += 1
                continue
            session_id, session_slug, updated_at = match
            age = int((now - updated_at) // 60)
            detail = f"{session_id}"
            if session_slug:
                detail = f"{session_id}/{session_slug}"
            # Cross-check plugin-load marker: a running SQLite session *without*
            # the marker means the headless runtime loaded OpenCode but the
            # harness plugin did not init — guards are not active, so the run is
            # effectively unguarded even if the process is alive.
            plugin_marker = plugin_loaded_for_slug(agent_home, slug)
            if age <= stale_min:
                marker_note = "" if plugin_marker else " plugin_loaded=false"
                print(f"ALIVE    {label} (OpenCode session {age}m ago: {detail}{marker_note})")
                if not plugin_marker and slug:
                    # Alive but unguarded — flag as SUSPECT so main harvests attention.
                    print(f"  note: no plugin-load marker for {slug}; harness guards likely inactive")
                alive += 1
            else:
                hb_age = heartbeat_age_min(agent_home, slug, now)
                if hb_age is not None and hb_age <= stale_min:
                    print(f"ALIVE    {label} (heartbeat {hb_age:.1f}m ago overrides stale SQLite {age}m: {detail})")
                    alive += 1
                else:
                    # Stale with no fresh heartbeat — SD-15b: consult the anchored log scan for a
                    # definitive limit/auth reason (hang-on-limit #8203); else SUSPECT (mtime-stale).
                    log_hit = log_shows_limit(agent_home, slug)
                    if log_hit is not None:
                        print(f"DEAD     {label} - log limit/auth pattern ({log_hit}) [open: {ts}]")
                    else:
                        print(f"SUSPECT  {label} - OpenCode session {age}m stale: {detail} [open: {ts}]")
                    suspect += 1

    print(f"open {open_n} ; alive {alive} ; suspect/dead {suspect}")
    print(f"classifier_source={ATTEMPT_CLASSIFIER_SOURCE}")
    if suspect:
        print("SUSPECT/DEAD: inspect OpenCode session export/DB and dispatch log, then harvest or redispatch.")
        return 3
    return 0


def resolve_agent_home() -> Path:
    return _resolve_agent_home(
        runtime_pointer=Path.home() / ".config" / "opencode" / "hearting"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
