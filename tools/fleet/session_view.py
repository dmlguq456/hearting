"""One exact session's existing Fleet observations, without a collection pass.

The attachment already proves the selected native identity. These are display
values only: they never establish input readiness or replace a native census.
"""
from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

from . import gitinfo, titles, session_registry, context_policy
from .collectors import claude, codex, opencode, usage_cache
from .model import Session
from .token_budget import parse_codex_token_count, telemetry_from_explicit


def _claude_callback(path, sid, pid, start):
    """Read the existing bounded, exact-session display tap, never latest-global."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > 65536:
                return None
            raw = os.read(fd, 65537)
            stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if len(raw) != before.st_size or stamp(os.fstat(fd)) != stamp(before) or stamp(path.lstat()) != stamp(before):
                return None
            value = json.loads(raw)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("session_id") != sid:
        return None
    if ("pid" in value or "proc_start" in value) and (
            value.get("pid") != pid or str(value.get("proc_start")) != start):
        return None
    return value


def _percentage(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100 else None


def _claude_percentage(value):
    window = value.get("context_window")
    if not isinstance(window, dict):
        return None
    percentage = _percentage(window.get("used_percentage"))
    if percentage is not None:
        return percentage
    current = window.get("current_usage")
    capacity = window.get("context_window_size")
    if not isinstance(current, dict) or type(capacity) not in (int, float) or not math.isfinite(capacity) or capacity <= 0:
        return None
    parts = [current.get(key) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    if not all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in parts):
        return None
    return min(100, 100 * sum(parts) / capacity)


def weekly_quota(payload):
    if not isinstance(payload, dict):
        return None
    windows = payload.get("rl_windows") or payload.get("windows")
    if isinstance(windows, list):
        # A dynamic nonweekly window must not inherit the legacy 7d label.
        return next((row[1] for row in windows if isinstance(row, (list, tuple))
                     and len(row) >= 2 and row[0] == "7d"), None)
    return payload.get("rl_7d")


def observe(harness, pid, proc_start, sid):
    """Read the selected session through Fleet's parsers and report watch inputs."""
    from .collectors.procscan import read_proc_start
    if harness not in {"claude", "codex", "opencode"} or read_proc_start(pid) != proc_start:
        return {}, []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = ""
    sess = Session(harness, pid, cwd=cwd, session_id=sid)
    sess.proc_start = proc_start
    sess._fast_first = True
    paths = [titles.sidecar_path(sid, harness), usage_cache._path(harness),
             context_policy.policy_path(harness, pid=pid, proc_start=proc_start)]
    transcript = None
    if harness == "claude":
        home = claude._home()
        paths.append(Path(home) / "sessions" / f"{pid}.json")
        record = claude.read_registry(pid, home)
        if record and record.get("sessionId") == sid:
            claude._apply_registry(sess, record)
        transcript = claude._newest_transcript_path(home, cwd, sid)
        if transcript:
            sess.model, sess.effort, _at = claude._subagent_budget(transcript)
        tap_path = Path(home) / ".statusline" / (sid + ".json")
        paths.append(tap_path)
        callback = _claude_callback(tap_path, sid, pid, proc_start)
        if callback:
            claude._apply_statusline(sess, callback)
            # Native context use is distinct from the compaction-trigger %.
            sess.ctx_pct = _claude_percentage(callback)
    elif harness == "codex":
        transcript = codex.exact_rollout_for_session_id(sid)
        if transcript:
            sess.model, sess.effort = codex._rollout_model_effort(transcript)
            count = codex._tail_token_count(transcript)
            if count:
                sess.ctx_pct = parse_codex_token_count(count, session_id=sid).context_used_pct
    else:
        db = opencode._db()
        paths.extend((db, db + "-wal"))
        opencode.enrich(sess)
        if sess.session_id != sid:
            # A delayed callback must never borrow the newly selected session.
            return {}, [str(path) for path in paths if path]
        raw = _percentage((sess.context_policy or {}).get("raw_percentage"))
        sess.ctx_pct = raw if raw is not None else telemetry_from_explicit(
            adapter="opencode", active_context_tokens=sess.active_context_tokens,
            context_window_tokens=sess.context_window_tokens).context_used_pct
    if transcript:
        paths.append(transcript)
    # Native selection and policy are inputs even before their first record.
    paths.append(Path(session_registry._dir_for(harness)) / f"{pid}.json")
    linked, _main = gitinfo.resolve_gitdir(sess.cwd)
    if linked:
        paths.append(Path(linked) / "HEAD")
    git_request = gitinfo._display_request(sess.cwd)
    paths.append(gitinfo._display_paths(git_request)[1])
    observed_git = (gitinfo.cached_display(sess.cwd) or {}).get("git") or {}
    branch = gitinfo.branch(sess.cwd)
    if branch is None and observed_git.get("repo") == 1:
        branch = observed_git.get("branch")
    if branch is None and not linked and observed_git.get("repo") == 0:
        branch = "no-git"
    if read_proc_start(pid) != proc_start:
        return {}, []
    values = {name: getattr(sess, name) for name in ("cwd", "model", "effort", "ctx_pct")}
    values.update(harness=harness, title=titles.last_title(sid, harness=harness),
                  branch=branch,
                  weekly_pct=weekly_quota(usage_cache.read(harness).get("payload")))
    return values, sorted({os.fspath(path) for path in paths if path})
