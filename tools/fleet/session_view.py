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

from . import gitinfo, titles, session_registry, context_policy, session_handle
from .collectors import claude, codex, opencode, usage_cache
from .model import Session
from .token_budget import parse_codex_token_count, telemetry_from_explicit


from .native_display import claude_callback as _claude_callback, percentage as _percentage, claude_percentage as _claude_percentage
from .native_display import claude_quota, quota_windows


def weekly_quota(payload):
    if not isinstance(payload, dict):
        return None
    windows = payload.get("rl_windows") or payload.get("windows")
    if isinstance(windows, list):
        # A dynamic nonweekly window must not inherit the legacy 7d label.
        return next((row[1] for row in windows if isinstance(row, (list, tuple))
                     and len(row) >= 2 and row[0] == "7d"), None)
    return payload.get("rl_7d")


# The cells each native source supplies. A source that exists but was not read
# (``session_registry.unread_error``, or a replacement during the read) did not
# observe these cells empty.
_TAP_CELLS = ("model", "effort", "ctx_pct", "active_context_tokens", "context_window_tokens")
_TAP_QUOTA = ("weekly_pct", "quota_windows", "model_quotas")
_MODEL_CELLS = ("model", "effort")
_TOKEN_CELLS = ("ctx_pct", "active_context_tokens", "context_window_tokens", "compaction_pressure")


def observe(harness, pid, proc_start, sid, *, previous=None):
    """Read the selected session through Fleet's parsers and report watch inputs.

    ``previous`` is the caller's last observation of this exact live session
    (harness, SID, PID and start). When given, a native source that was not
    read keeps the cells it supplies from it, as Fleet's native reader keeps
    its last display, and the next call reads the source again. Absence,
    refusal, another session and an ended process still clear; a cell that
    was never observed stays empty. Without it the result is unchanged.
    """
    from .collectors.procscan import read_proc_start
    keep = previous if isinstance(previous, dict) else None
    unread = []
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
    callback = None
    # The runtime's own turn record (spec E9 rule 26, rule 6 of 11-12): Claude's REPL writes
    # its UI state (`busy` while it shows progress, `waiting` with a dialog or question open,
    # `idle`) into its session record; Codex appends each turn's start and end to its rollout.
    # Display only: it never stands for input readiness.
    turn = None
    if harness == "claude":
        home = claude._home()
        paths.append(Path(home) / "sessions" / f"{pid}.json")
        record = claude.read_registry(pid, home)
        if record and record.get("sessionId") == sid:
            claude._apply_registry(sess, record)
            if str(record.get("procStart") or "") == str(proc_start):
                turn = {"busy": "working", "idle": "idle", "waiting": "blocked"}.get(record.get("status"))
        transcript = claude._newest_transcript_path(home, cwd, sid)
        if transcript:
            sess.model, sess.effort, _at = claude._subagent_budget(transcript)
        tap_path = Path(home) / ".statusline" / (sid + ".json")
        paths.append(tap_path)
        try:
            callback = _claude_callback(tap_path, sid, pid, proc_start, raise_unread=keep is not None)
        except OSError:
            callback = None
            unread.extend(_TAP_CELLS + _TAP_QUOTA)
        if callback:
            claude._apply_statusline(sess, callback)
            # Native context use is distinct from the compaction-trigger %.
            sess.ctx_pct = _claude_percentage(callback)
    elif harness == "codex":
        transcript = codex.exact_rollout_for_session_id(sid)
        if transcript:
            try:
                sess.model, sess.effort = codex._rollout_model_effort(transcript, raise_unread=keep is not None)
            except OSError:
                unread.extend(_MODEL_CELLS)
            try:
                count = codex._tail_token_count(transcript, raise_unread=keep is not None)
            except OSError:
                count = None
                unread.extend(_TOKEN_CELLS)
            try:
                lifecycle = codex._latest_task_lifecycle(transcript)
            except OSError:
                lifecycle = None
            if lifecycle:
                turn = {"task_started": "working", "task_complete": "idle",
                        "turn_aborted": "idle"}.get(lifecycle[0])
            if count:
                telemetry = parse_codex_token_count(count, session_id=sid)
                sess.ctx_pct = telemetry.context_used_pct
                sess.active_context_tokens = telemetry.active_context_tokens
                sess.context_window_tokens = telemetry.context_window_tokens
                from .token_budget import codex_compaction_pressure
                sess.compaction_pressure = codex_compaction_pressure(
                    sid, transcript, count, home=codex._rollout_home(transcript))
        if not sess.model and not ("model" in unread and keep.get("model")):
            from .native_display import codex_selected_model
            selected = codex_selected_model(pid, proc_start, sid)
            sess.model, sess.effort = selected.get("model"), selected.get("effort")
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
    observed_git = {}
    if sess.cwd:
        git_request = gitinfo._display_request(sess.cwd)
        paths.append(gitinfo._display_paths(git_request)[1])
        observed_git = gitinfo.display_fields(gitinfo.cached_display(sess.cwd))
    branch = gitinfo.branch(sess.cwd)
    if branch is None and observed_git.get("repo") == 1:
        branch = observed_git.get("branch")
    if branch is None and not linked and observed_git.get("repo") == 0:
        branch = "no-git"
    if read_proc_start(pid) != proc_start:
        return {}, []
    values = {name: getattr(sess, name) for name in ("cwd", "model", "effort", "ctx_pct",
                                                     "active_context_tokens", "context_window_tokens",
                                                     "compaction_pressure")}
    name_path = session_handle._session_name_registry_path(harness, sid)
    if name_path:
        paths.append(name_path)
    names = session_handle.resolve_display_inputs(harness, sid, pid=pid)
    title = session_handle.current_session_subject(names.get("runtime_name"), titles.last_title(sid, harness=harness))
    # Fleet's NOW (`summary.now`): the same fresh one-line summary from the same sidecar.
    now_line = titles.fresh_summary_with_ts(sid, harness=harness)[0]
    quota = dict(usage_cache.read(harness).get("payload") or {})
    if callback:
        native_quota = claude_quota(callback)
        if native_quota:
            # This terminal mirrors its exact native callback; Fleet account
            # quota admission remains credential-bound and unchanged.
            quota = native_quota
    values.update(harness=harness, title=title or None, now=now_line or None, native_turn=turn, git=observed_git,
                  branch=branch + "*" if branch and observed_git.get("dirty") == 1 else branch,
                  weekly_pct=weekly_quota(quota), quota_windows=quota_windows(quota),
                  model_quotas=quota.get("rl_ms") or [], jobs=observed_git.get("jobs"))
    for name in unread if keep is not None else ():
        if name in keep:
            values[name] = keep[name]
    return values, sorted({os.fspath(path) for path in paths if path})
