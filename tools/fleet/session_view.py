"""One exact session's existing Fleet observations, without a collection pass.

The attachment already proves the selected native identity. These are display
values only: they never establish input readiness or replace a native census.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import gitinfo, titles, session_registry, context_policy
from .collectors import claude, codex, opencode, usage_cache
from .model import Session


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
        # Claude's independent transcript lacks the native context denominator.
        # Keep that field unknown here; never borrow its statusline input.
    elif harness == "codex":
        transcript = codex.exact_rollout_for_session_id(sid)
        if transcript:
            sess.model, sess.effort = codex._rollout_model_effort(transcript)
            count = codex._tail_token_count(transcript)
            if count:
                codex._apply_token_count(sess, count, transcript)
    else:
        db = opencode._db()
        paths.extend((db, db + "-wal"))
        opencode.enrich(sess)
        if sess.session_id != sid:
            # A delayed callback must never borrow the newly selected session.
            return {}, [str(path) for path in paths if path]
    if transcript:
        paths.append(transcript)
    # Native selection and policy are inputs even before their first record.
    paths.append(Path(session_registry._dir_for(harness)) / f"{pid}.json")
    linked, _main = gitinfo.resolve_gitdir(sess.cwd)
    if linked:
        paths.append(Path(linked) / "HEAD")
    if read_proc_start(pid) != proc_start:
        return {}, []
    values = {name: getattr(sess, name) for name in ("cwd", "model", "effort", "ctx_pct")}
    values.update(harness=harness, title=titles.last_title(sid, harness=harness),
                  branch=gitinfo.branch(sess.cwd),
                  weekly_pct=weekly_quota(usage_cache.read(harness).get("payload")))
    return values, sorted({os.fspath(path) for path in paths if path})
