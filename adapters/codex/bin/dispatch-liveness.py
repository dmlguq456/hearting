#!/usr/bin/env python3
"""Codex dispatch liveness check using Codex session JSONL mtimes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    anchored_capacity_failure,
    authoritative_process_identities,
    observed_attempt_liveness,
    process_start_ticks,
    process_state,
    resolve_agent_home as _resolve_agent_home,
)
from codex_dispatch_terminal import (  # noqa: E402
    inspect_terminal_attempt,
    terminal_envelope_observed,
)
from tools.fleet.model import (  # noqa: E402
    ATTEMPT_CLASSIFIER_SOURCE,
    classify_attempt_evidence,
)

# SD-15 (OPERATIONS §5.10 ⑨): if an open row's dispatch log shows a limit/auth death
# pattern, judge it DEAD regardless of transcript mtime (the wrapper's launch watch may
# have missed it, or the child died — or hung, OpenCode #8203 — after launch). Kept in
# sync with dispatch-headless.py DEATH_PATTERNS (intentional cross-runtime duplication).
LIMIT_RE = re.compile(
    r"(?:selected\s+)?model\b.{0,80}\b(?:is\s+)?at capacity\b|"
    r"operation not permitted|network is unreachable|network access denied|"
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
    match on a short/standalone line. The fresh-transcript completion signal is applied by
    main() (a fresh transcript never reaches this function).
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


def same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return os.path.abspath(a) == os.path.abspath(b)


def transcript_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    payload = (json.loads(line).get("payload") or {})
                except Exception:
                    continue
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def parse_profile(pipe: str) -> str | None:
    """Extract profile=<name> from the jobs.log 6th (pipe) field.

    Replicates utilities/dispatch-liveness.sh:23 semantics: last ``profile=``
    occurrence, comma-terminated. Returns None when absent (non-profile job).
    """
    if not pipe or "profile=" not in pipe:
        return None
    name = pipe.split("profile=")[-1].split(",")[0].strip()
    return name or None


def parse_metadata(pipe: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in pipe.split(",") if "=" in part)


def current_job_lines(jobs: Path) -> list[str]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "utilities/dispatch-registry.py"),
         "liveness", "--jobs", str(jobs)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "current-view-failed")
    return result.stdout.splitlines()


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


def attempt_terminal_observation(
    agent_home: Path, metadata: dict[str, str]
) -> dict | None:
    attempt = metadata.get("attempt_id", "").replace("/", "_")
    route = metadata.get("route_id")
    node = metadata.get("route_node")
    if not attempt or not route or not node:
        return None
    path = agent_home / ".dispatch" / "watchdog" / f"{attempt}.json"
    try:
        if path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not value.get("terminal_action"):
        return None
    return {
        **value,
        "attempt_id": metadata["attempt_id"],
        "route_id": route,
        "route_node": node,
    }


def recorded_attempt_state(metadata: dict[str, str], now: float, agent_home: Path) -> dict | None:
    raw_local = metadata.get("pid", "")
    local_pid = int(raw_local) if raw_local.isdigit() else None
    local_start = metadata.get("pid_start", "")
    identities = authoritative_process_identities(metadata)
    selected = None
    fallback = None
    for identity in identities:
        actual = process_start_ticks(identity.pid) or ""
        alive = bool(actual) and process_state(identity.pid) != "Z"
        evidence = (identity, actual, alive, bool(alive and actual == identity.expected_start))
        if fallback is None:
            fallback = evidence
        if evidence[3]:
            selected = evidence
            break
    selected = selected or fallback
    if selected is None:
        numeric_pid = None
        expected = ""
        actual = ""
        alive = None
        start_match = None
        identity_source = None
    else:
        identity, actual, alive, start_match = selected
        numeric_pid = identity.pid
        expected = identity.expected_start
        identity_source = identity.source
    raw_host = metadata.get("pid_host", "")
    return classify_attempt_evidence({
        "pid": numeric_pid, "proc_start": expected, "actual_proc_start": actual,
        "pid_alive": alive, "proc_start_match": start_match,
        "pid_scope": metadata.get("pid_scope"),
        "pid_authoritative": selected is not None,
        "pid_identity_source": identity_source,
        "pid_local": local_pid, "pid_local_start": local_start,
        "pid_host": int(raw_host) if raw_host.isdigit() else None,
        "pid_host_start": metadata.get("pid_host_start"),
        "pid_host_ns": metadata.get("pid_host_ns"),
        "pid_ns": metadata.get("pid_ns"),
        "pid_observer_ns": metadata.get("pid_observer_ns"),
        "pid_host_proof": metadata.get("pid_host_proof"),
        "pgid": metadata.get("pgid"),
        "attempt_id": metadata.get("attempt_id"), "route_id": metadata.get("route_id"),
        "route_node": metadata.get("route_node"),
        "heartbeat": attempt_heartbeat(agent_home, metadata),
        "terminal_observation": attempt_terminal_observation(agent_home, metadata),
    }, now)


def orphan_status(agent_home: Path, jobs: Path, attempt_id: str) -> dict | None:
    """SD-64/71: reuse dispatch-registry.py's own classification (never re-derived)."""
    if not attempt_id:
        return None
    registry = Path(__file__).resolve().parents[3] / "utilities" / "dispatch-registry.py"
    try:
        result = subprocess.run(
            [sys.executable, str(registry), "orphan-status", "--attempt", attempt_id,
             "--jobs", str(jobs), "--agent-home", str(agent_home)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("orphan") != "1":
        return None
    return {"route_id": fields.get("route_id", "-"), "resume_boundary": fields.get("resume_boundary", "-")}


def sessions_dir_for(pipe: str, slug: str, agent_home: Path, default_sessions: Path) -> Path:
    """Resolve the Codex session store for one job.

    A ``--profile`` dispatch runs under CODEX_HOME=.dispatch/homes/<slug>.<profile>
    (dispatch-headless.py), so its transcripts land in that home's ``sessions/``,
    not ~/.codex/sessions. Isomorphic to portable dispatch-liveness.sh:22-28 and
    fleet collectors/dispatch.py, adapted to the Codex ``sessions/`` layout (the
    sh helper's ``projects/<enc>`` is Claude-shaped). Non-profile jobs keep the
    default store (byte-behavior for existing callers).
    """
    prof = parse_profile(pipe)
    if prof:
        return agent_home / ".dispatch" / "homes" / f"{slug}.{prof}" / "sessions"
    return default_sessions


def sessions_dirs_for(
    pipe: str,
    slug: str,
    agent_home: Path,
    default_sessions: Path,
    worktree: str,
) -> list[Path]:
    """Resolve all possible session stores without weakening profile isolation."""
    prof = parse_profile(pipe)
    if prof:
        return [sessions_dir_for(pipe, slug, agent_home, default_sessions)]

    candidates = [
        Path(worktree) / ".dispatch" / "codex-home" / "sessions",
        default_sessions,
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.abspath(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def locate_latest_for_worktree(sessions: Path, worktree: str) -> Path | None:
    if not sessions.is_dir():
        return None
    try:
        candidates = sorted(
            sessions.glob("**/*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for path in candidates:
        if same_path(transcript_cwd(path) or "", worktree):
            return path
    return None


def locate_latest_for_worktree_dirs(sessions_dirs: list[Path], worktree: str) -> Path | None:
    matches = [
        path
        for sessions in sessions_dirs
        if (path := locate_latest_for_worktree(sessions, worktree)) is not None
    ]
    if not matches:
        return None
    try:
        return max(matches, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None


def main(argv: list[str]) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in {"-h", "--help"}):
        return usage()

    agent_home = resolve_agent_home()
    jobs_override = argv[1] if len(argv) == 2 else os.environ.get("AGENT_DISPATCH_JOBS")
    jobs = Path(jobs_override) if jobs_override else agent_home / ".dispatch" / "jobs.log"
    default_sessions = Path(os.environ.get("CODEX_SESSIONS", Path.home() / ".codex" / "sessions"))
    stale_min = int(os.environ.get("DISPATCH_STALE_MIN", "15"))

    if not jobs.is_file():
        print(f"(jobs.log missing: {jobs})")
        return 0

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
            metadata = parse_metadata(pipe)
            harness = metadata.get("harness", "")
            terminal = None
            if harness in {"", "codex", "claude", "opencode"} and metadata.get("log_file"):
                terminal = inspect_terminal_attempt(
                    metadata.get("log_file"),
                    worktree=worktree,
                    artifact_root_metadata=metadata.get("artifact_root"),
                )
            common = observed_attempt_liveness(
                status,
                metadata,
                terminal_envelope=terminal_envelope_observed(
                    metadata.get("log_file")
                ),
            )
            exact = recorded_attempt_state(metadata, now, agent_home)
            # SD-64/71 post-exit owner-orphan reconciliation retains precedence
            # over a PASS observation.  Failure handoffs keep their existing
            # registry precedence because dispatch-registry classifies them as
            # terminal failures before orphan repair.
            if common.state == "reconcile-needed" or (
                exact and exact["state"] == "dead"
            ):
                orphan = orphan_status(agent_home, jobs, metadata.get("attempt_id", ""))
                if orphan is not None:
                    print(f"ORPHANED {label} - pipeline orphaned; route={orphan['route_id']}; "
                          f"resume boundary={orphan['resume_boundary']}; dispatch-depth-0 decision [open: {ts}]")
                    suspect += 1
                    continue
            if common.state == "alive":
                print(
                    f"ALIVE    {label} ({common.process_reason}; "
                    f"classifier={ATTEMPT_CLASSIFIER_SOURCE})"
                )
                alive += 1
                continue
            if terminal and terminal.get("state") == "valid":
                verdict = terminal["verdict"]
                artifact_state = terminal["artifact_state"]
                terminal_label = (
                    "Claude result"
                    if terminal.get("source") == "exact-claude-result"
                    else "turn.completed"
                )
                if verdict == "PASS":
                    print(
                        f"COMPLETED {label} - exact {terminal_label} PASS; harvest required "
                        f"(artifact_state={artifact_state}; blocker_reason=none) [open: {ts}]"
                    )
                else:
                    print(
                        f"EXITED   {label} - exact {terminal_label} {verdict} "
                        f"({terminal['failure_note']}; blocker_reason={terminal['blocker_reason']}; "
                        f"artifact_state={artifact_state}) [open: {ts}]"
                    )
                suspect += 1
                continue
            if terminal and terminal.get("state") == "invalid":
                print(
                    f"EXITED   {label} - invalid-handoff "
                    f"(artifact_state={terminal['artifact_state']}; "
                    "blocker_reason=contract-violation) "
                    f"[open: {ts}]"
                )
                suspect += 1
                continue
            if terminal and terminal.get("state") == "error":
                print(
                    f"EXITED   {label} - terminal-inspector-error "
                    f"(artifact_state={terminal['artifact_state']}; "
                    "blocker_reason=contract-violation) "
                    f"[open: {ts}]"
                )
                suspect += 1
                continue
            if common.state == "reconcile-needed":
                print(
                    f"RECONCILE {label} - terminal-observed/reconcile-needed "
                    f"({common.process_reason}) [open: {ts}]"
                )
                suspect += 1
                continue
            if exact and exact["state"] == "working":
                detail = (
                    "namespace-local exact heartbeat"
                    if exact.get("source") == "heartbeat"
                    else f"recorded pid {exact['pid']} running"
                )
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
            if exact and exact["state"] == "unknown":
                print(
                    f"SUSPECT  {label} - exact attempt has no fresh or terminal evidence; "
                    f"cwd-wide transcripts ignored [open: {ts}]"
                )
                suspect += 1
                continue
            # Profile jobs live under their isolated home. Non-profile nested workers
            # may inherit a conductor's worktree-local CODEX_HOME, so inspect both that
            # deterministic projection and the caller's default session store.
            sessions_dirs = sessions_dirs_for(
                pipe, slug, agent_home, default_sessions, worktree
            )
            transcript = locate_latest_for_worktree_dirs(sessions_dirs, worktree)
            if transcript is None:
                wrapper_log = agent_home / ".dispatch" / "logs" / f"{slug}.codex.jsonl"
                if wrapper_log.is_file():
                    transcript = wrapper_log
            if transcript is None:
                # No transcript — a launch that died before writing, or never came up. SD-15b:
                # consult the anchored log scan to name a limit/auth death; else generic DEAD.
                log_hit = log_shows_limit(agent_home, slug)
                if log_hit is not None:
                    print(f"DEAD     {label} - log limit/auth pattern ({log_hit}) [open: {ts}]")
                else:
                    print(f"DEAD     {label} - Codex session transcript not found for {worktree} [open: {ts}]")
                suspect += 1
                continue
            age = int((now - transcript.stat().st_mtime) // 60)
            if age <= stale_min:
                # SD-15b: a fresh transcript is a completion/liveness signal — do NOT let a log
                # that merely discusses limits force DEAD (the anchored scan isn't even consulted).
                print(f"ALIVE    {label} (Codex transcript {age}m ago: {transcript})")
                alive += 1
            else:
                # Stale — hang, or a post-limit death. SD-15b: consult the anchored log scan for a
                # definitive limit/auth reason; else SUSPECT (mtime-stale).
                log_hit = log_shows_limit(agent_home, slug)
                if log_hit is not None:
                    print(f"DEAD     {label} - log limit/auth pattern ({log_hit}) [open: {ts}]")
                else:
                    print(f"SUSPECT  {label} - Codex transcript {age}m stale [open: {ts}]")
                suspect += 1

    print(f"open {open_n} ; alive {alive} ; suspect/dead {suspect}")
    print(f"classifier_source={ATTEMPT_CLASSIFIER_SOURCE}")
    if suspect:
        print("terminal/SUSPECT/DEAD: inspect typed status, then harvest or redispatch.")
        return 3
    return 0


def resolve_agent_home() -> Path:
    return _resolve_agent_home(runtime_pointer=Path.home() / ".codex" / "hearting")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
