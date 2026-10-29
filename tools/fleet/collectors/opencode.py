"""opencode enrichment — passive, read-only SQLite (01_tap_mechanics.md §3).

State lives in ~/.local/share/opencode/opencode.db (WAL; opened mode=ro). The `session` table
carries per-session model/cwd/cost/tokens live. Session data is attached only when the pane's
current TUI selection record proves its session id for the exact PID/start pair. The `--session`
argument is only where the process started; cwd, creation windows, and update recency do not
prove which conversation the pane currently displays. The process scan remains the source of
live row existence when no current selection can be observed.

Structurally missing (render '—', PRD §2/§4): rate-limit (no column). effort = model.variant.
Active context is the latest request's input + cache.read + cache.write. Its display
denominator requires the same process/session's observed native compaction policy; the
session-table `tokens_input` is cumulative cost-side usage and a models.dev catalogue window
does not prove the running policy.
"""
import datetime
import math
import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections import OrderedDict
from pathlib import Path

from ..model import ContextEvidence, SubAgent
from . import shell_jobs as _shell_jobs
from .. import session_registry
from .. import titles

_COLS = ("id, slug, agent, model, cost, tokens_input, tokens_output, tokens_reasoning, "
         "time_updated, parent_id")

# Variant tokens that name the runtime's own choice rather than a user-set dial. These are
# the same words render drops in `_EMPTY_EFFORT` (render.py) — mirrored here, not imported:
# collectors must not import render at module scope (see session_registry's import
# contract), so the two lists are kept in step by name and test rather than by dependency.
_DEFAULT_VARIANTS = frozenset({"default", "runtime-default", "inherit"})
_V2_SHELL_TASKS = OrderedDict()
_V2_SHELL_TASK_LIMIT = 256
_NATIVE_SHELL_OUTPUT_LOCATORS = OrderedDict()
_NATIVE_SHELL_LOCATION_LIMIT = 256
_NATIVE_SHELL_DIRECTORY_ENTRY_LIMIT = 512
_NATIVE_SHELL_LOCATOR_TTL = 3.0


def _v2_shell_key(db, session_id, attempt_id=None):
    return (os.path.realpath(db), session_id, attempt_id, "v2")


def _v2_shell_cursor(db, session_id, attempt_id=None):
    state = _V2_SHELL_TASKS.get(_v2_shell_key(db, session_id, attempt_id))
    if not isinstance(state, dict):
        return 0
    try:
        st = os.stat(db)
        identity = {"device": st.st_dev, "inode": st.st_ino}
    except OSError:
        identity = None
    return state.get("cursor", 0) if state.get("database_identity") == identity else 0


def _v2_shell_refresh_seqs(db, session_id, attempt_id=None):
    state = _V2_SHELL_TASKS.get(_v2_shell_key(db, session_id, attempt_id))
    if not isinstance(state, dict):
        return []
    try:
        st = os.stat(db)
        identity = {"device": st.st_dev, "inode": st.st_ino}
    except OSError:
        identity = None
    if state.get("database_identity") != identity or not identity:
        return []
    pending = sorted(seq for seq in state.get("mutable_seqs", ())
                     if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0)
    if not pending:
        return []
    runtime = _opencode_v2_runtime()
    limit = runtime.MAX_SHELL_TASK_REFRESH
    offset = state.get("refresh_offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool):
        offset = 0
    offset %= len(pending)
    selected = (pending[offset:] + pending[:offset])[:limit]
    state["refresh_offset"] = (offset + len(selected)) % len(pending)
    return selected


def _verified_native_shell_output(path, data_root, shell_root):
    try:
        output = Path(path)
        parent = output.parent
        parent_stat = os.lstat(parent)
        output_stat = os.lstat(output)
        return (not stat.S_ISLNK(parent_stat.st_mode)
                and stat.S_ISDIR(parent_stat.st_mode)
                and not stat.S_ISLNK(output_stat.st_mode)
                and stat.S_ISREG(output_stat.st_mode)
                and parent.parent.resolve() == shell_root
                and output.resolve(strict=True).is_relative_to(data_root))
    except (OSError, ValueError, RuntimeError):
        return False


def _native_shell_output_locator(native_id, *, data_root=None):
    """Find only an exact native shellID output file under OpenCode's data root."""
    if not isinstance(native_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", native_id):
        return {"state": "unknown", "reason": "native-shell-id-invalid"}
    try:
        if data_root is None:
            runtime = _opencode_v2_runtime()
            data_root = runtime.opencode_data_dir()
        data_root = Path(data_root).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"state": "unknown", "reason": "native-data-root-unavailable"}
    shell_root = data_root / "shell"
    cache_key = (str(data_root), native_id)
    now = time.monotonic()
    cached = _NATIVE_SHELL_OUTPUT_LOCATORS.get(cache_key)
    if isinstance(cached, dict) and now - cached.get("checked_at", 0) <= _NATIVE_SHELL_LOCATOR_TTL:
        path = cached.get("path")
        if path is None:
            return {"state": cached.get("state", "unknown"),
                    "reason": cached.get("reason")}
        if _verified_native_shell_output(path, data_root, shell_root):
            return {"state": "observed", "path": path, "reason": None}
        _NATIVE_SHELL_OUTPUT_LOCATORS.pop(cache_key, None)
    candidates = []
    failure = None
    scanned = 0
    locations = 0
    try:
        entries = os.scandir(shell_root)
    except FileNotFoundError:
        result = {"state": "unavailable", "reason": "native-shell-storage-not-found"}
    except OSError as exc:
        result = {"state": "unavailable",
                  "reason": "native-shell-storage-" + type(exc).__name__}
    else:
        try:
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > _NATIVE_SHELL_DIRECTORY_ENTRY_LIMIT:
                        failure = "native-shell-location-scan-limit"
                        break
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        failure = "native-shell-location-unreadable"
                        break
                    locations += 1
                    if locations > _NATIVE_SHELL_LOCATION_LIMIT:
                        failure = "native-shell-location-scan-limit"
                        break
                    if len(candidates) >= _NATIVE_SHELL_LOCATION_LIMIT:
                        failure = "native-shell-location-scan-limit"
                        break
                    candidate = os.path.join(entry.path, native_id + ".out")
                    try:
                        candidate_stat = os.lstat(candidate)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        failure = "native-shell-output-unreadable"
                        break
                    if (not stat.S_ISREG(candidate_stat.st_mode)
                            or stat.S_ISLNK(candidate_stat.st_mode)
                            or not _verified_native_shell_output(candidate, data_root, shell_root)):
                        failure = "native-shell-output-not-regular"
                        break
                    candidates.append(candidate)
        except OSError:
            failure = failure or "native-shell-location-read-failed"
        if failure:
            result = {"state": "partial", "reason": failure}
        elif len(candidates) > 1:
            result = {"state": "ambiguous", "reason": "native-shell-output-id-ambiguous"}
        elif len(candidates) == 1:
            result = {"state": "observed", "path": candidates[0], "reason": None}
        else:
            result = {"state": "unavailable", "reason": "native-shell-output-not-found"}
    _NATIVE_SHELL_OUTPUT_LOCATORS[cache_key] = {**result, "checked_at": now}
    _NATIVE_SHELL_OUTPUT_LOCATORS.move_to_end(cache_key)
    while len(_NATIVE_SHELL_OUTPUT_LOCATORS) > 512:
        _NATIVE_SHELL_OUTPUT_LOCATORS.popitem(last=False)
    return result


def _native_terminal(item):
    status = str(item.get("native_status") or "").lower()
    return (status in {"completed", "complete", "exited", "failed", "error",
                       "cancelled", "canceled", "stopped"}
            or item.get("completed_at") is not None
            or (isinstance(item.get("exit_code"), int)
                and not isinstance(item.get("exit_code"), bool)))


def _attach_v2_shell_tasks(sess, observed, db, session_id, *, tree=None, kids=None,
                           native_data_root=None):
    attempt_id = getattr(sess, "attempt_id", None)
    key = _v2_shell_key(db, session_id, attempt_id)
    source = observed.get("database_identity")
    source_valid = (isinstance(source, dict)
                    and isinstance(source.get("device"), int)
                    and not isinstance(source.get("device"), bool)
                    and isinstance(source.get("inode"), int)
                    and not isinstance(source.get("inode"), bool))
    source_label = ("opencode-v2:%s:%s" % (source.get("device"), source.get("inode"))
                    if source_valid else "opencode-v2:identity-unavailable")
    state = _V2_SHELL_TASKS.get(key) if source_valid else _V2_SHELL_TASKS.pop(key, None)
    if not isinstance(state, dict) or state.get("database_identity") != source:
        retired = list(state.get("retired_jobs", ())) if isinstance(state, dict) else []
        overflow = bool(state.get("overflow")) if isinstance(state, dict) else False
        if isinstance(state, dict):
            for old in state.get("jobs", {}).values():
                item = dict(old)
                item.update(current_generation_valid=False,
                            current_execution_state="unknown",
                            current_execution_reason="source-generation-retired")
                if not _native_terminal(item):
                    item.update(observation_state="unknown",
                                observation_reason="database-generation-changed",
                                process_state="unknown",
                                process_reason="database-generation-changed")
                retired.append(item)
        state = {"database_identity": source, "cursor": 0,
                 "jobs": OrderedDict(), "retired_jobs": retired[-256:],
                 "mutable_seqs": set(), "refresh_offset": 0,
                 "overflow": overflow or len(retired) > 256}
    cursor = observed.get("shell_cursor") or {}
    if isinstance(cursor.get("next_seq"), int) and not isinstance(cursor.get("next_seq"), bool):
        state["cursor"] = max(state.get("cursor", 0), cursor["next_seq"])
    pending = set(state.get("mutable_seqs", ()))
    pending.difference_update(cursor.get("refreshed_seqs") or ())
    pending.update(cursor.get("mutable_seqs") or ())
    state["mutable_seqs"] = pending
    collection_at = time.time()
    current_ids = {}
    for native in observed.get("shell_tasks") or ():
        if isinstance(native, dict) and native.get("native_id"):
            native_id = str(native["native_id"])
            kind = native.get("native_id_kind") or native.get("native_record_kind") or "unknown"
            current_ids.setdefault(native_id, set()).add(kind)
    for old in state.get("jobs", {}).values():
        if old.get("native_id"):
            current_ids.setdefault(str(old["native_id"]), set()).add(
                old.get("native_id_kind") or old.get("native_record_kind") or "unknown")
    ambiguous_ids = {native_id for native_id, kinds in current_ids.items() if len(kinds) > 1}
    observed_job_keys = set()
    for native in observed.get("shell_tasks") or ():
        if not isinstance(native, dict) or not native.get("native_id"):
            continue
        native_id = str(native["native_id"])
        native_id_kind = native.get("native_id_kind") or "assistant-tool-part"
        native_record_kind = native.get("native_record_kind") or "assistant-shell-tool-part"
        job_key = (native_id_kind, native_id)
        observed_job_keys.add(job_key)
        previous = state["jobs"].get(job_key, {})
        started_ms = native.get("started_at_ms")
        started = _shell_jobs.valid_epoch(
            started_ms, milliseconds=isinstance(started_ms, (int, float)) and not isinstance(started_ms, bool))
        updated_ms = native.get("time_updated")
        observed_at = _shell_jobs.valid_epoch(
            updated_ms, milliseconds=isinstance(updated_ms, (int, float)) and not isinstance(updated_ms, bool))
        is_native_shell = native_record_kind == "session-message-shell"
        native_status = str(native.get("native_status") or "unknown") if is_native_shell else "acknowledged"
        completed_at = (_shell_jobs.valid_epoch(native.get("completed_at_ms"), milliseconds=True)
                        if is_native_shell else None)
        exit_code = native.get("exit_code") if isinstance(native.get("exit_code"), int) \
            and not isinstance(native.get("exit_code"), bool) else None
        locator = (_native_shell_output_locator(native_id, data_root=native_data_root)
                   if is_native_shell else None)
        output_path = locator.get("path") if isinstance(locator, dict) else None
        native_terminal = is_native_shell and (
            native_status.lower() in {"completed", "complete", "exited", "failed", "error",
                                      "cancelled", "canceled", "stopped"}
            or completed_at is not None or exit_code is not None)
        invocation_completed = (_shell_jobs.valid_epoch(
            native.get("invocation_completed_at_ms"), milliseconds=True)
            if not is_native_shell else None)
        item = dict(previous)
        previous_terminal = _native_terminal(previous)
        current_native_running = (is_native_shell and native_status.lower() == "running"
                                  and not previous_terminal)
        current_native_terminal = native_terminal or previous_terminal
        item.update({
            "harness": "opencode", "session_id": session_id,
            "attempt_id": attempt_id,
            "source_generation": source_label,
            "source_locator": os.path.realpath(db), "native_id": native_id,
            "native_id_kind": native_id_kind,
            "native_record_kind": native_record_kind,
            "background_state": "confirmed",
            "background_source": "opencode.v2.session-message.background-true"
            if is_native_shell else "opencode.v2.assistant-shell.input-background-true",
            "background_reason": None,
            "identity_link_state": "ambiguous-cross-namespace-id"
            if native_id in ambiguous_ids else "native-id-unlinked",
            "native_call_id": native.get("call_id"), "command": (native.get("input") or {}).get("command"),
            "command_source": ("session_message.shell.command" if is_native_shell
                               else "assistant.shell.input.command"),
            "started_at": previous.get("started_at", started),
            "start_source": native.get("start_source") or "assistant-shell-part.time.ran|created",
            "native_event_at": observed_at,
            "observed_at": collection_at,
            "current_generation_valid": previous.get("current_generation_valid") is not False,
            "current_execution_state": (
                "terminal" if current_native_terminal else
                "running" if current_native_running else
                "unknown" if is_native_shell else
                previous.get("current_execution_state", "unknown")),
            "current_execution_source": (
                "opencode.v2.session-message.exact-query" if is_native_shell else
                previous.get("current_execution_source")),
            "current_execution_reason": (
                "native-terminal-cache-prevents-resurrection" if previous_terminal and not native_terminal else
                "native-terminal-row" if native_terminal else
                None if current_native_running else
                "exact-native-row-is-not-running" if is_native_shell else
                previous.get("current_execution_reason")),
            "current_execution_at": (collection_at if current_native_running else
                                     None if is_native_shell or current_native_terminal else
                                     previous.get("current_execution_at")),
            "native_status": native_status,
            "native_status_source": native.get("native_status_source"),
            "invocation_status": native.get("invocation_status") if not is_native_shell else None,
            "invocation_completed_at": invocation_completed,
            "completed_at": completed_at,
            "exit_code": exit_code,
            "process_state": "dead" if native_terminal else "unknown",
            "process_reason": ("native-shell-terminal-record" if native_terminal
                               else "opencode-background-process-id-unobserved"),
            "output_path": output_path,
            "output_coverage": "sampled" if output_path or native.get("native_output_observed")
            else "unavailable",
            "output_reason": (None if output_path else
                              locator.get("reason") if isinstance(locator, dict) else
                              "assistant-shell-output-locator-unobserved"),
            "native_output_observed": native.get("native_output_observed"),
            "native_output_cursor": native.get("native_output_cursor"),
            "native_output_size": native.get("native_output_size"),
            "native_output_truncated": native.get("native_output_truncated"),
            "observation_state": cursor.get("state"), "observation_reason": cursor.get("reason"),
            "source_revision": (native.get("seq"), native.get("time_updated")),
        })
        if _native_terminal(previous) and not _native_terminal(item):
            for field in ("native_status", "native_status_source", "completed_at", "exit_code",
                          "process_state", "process_reason", "native_output_observed",
                          "native_output_cursor", "native_output_size", "native_output_truncated"):
                if field in previous:
                    item[field] = previous[field]
        state["jobs"][job_key] = item
        state["jobs"].move_to_end(job_key)
    for job_key, item in state.get("jobs", {}).items():
        if (job_key not in observed_job_keys
                and item.get("native_record_kind") == "session-message-shell"
                and not _native_terminal(item)):
            item.update(current_execution_state="unknown",
                        current_execution_reason="native-row-not-refreshed-this-collection",
                        current_execution_at=None)
    for item in state.get("jobs", {}).values():
        if str(item.get("native_id")) in ambiguous_ids:
            item["identity_link_state"] = "ambiguous-cross-namespace-id"
    while len(state["jobs"]) > _shell_jobs.MAX_RETAINED_TASKS:
        state["jobs"].popitem(last=False)
        state["overflow"] = True
    if source_valid:
        _V2_SHELL_TASKS[key] = state
        _V2_SHELL_TASKS.move_to_end(key)
        while len(_V2_SHELL_TASKS) > _V2_SHELL_TASK_LIMIT:
            _V2_SHELL_TASKS.popitem(last=False)
    ambiguous = bool(ambiguous_ids)
    pending_overflow = len(state.get("mutable_seqs", ())) > \
        _opencode_v2_runtime().MAX_SHELL_TASK_REFRESH
    sess.shell_jobs = _shell_jobs.observe_jobs(
        list(state.get("retired_jobs", ())) + list(state["jobs"].values()), now=collection_at,
        parent_pid=getattr(sess, "pid", None), parent_start=getattr(sess, "proc_start", None),
        attempt_id=attempt_id, tree=tree, kids=kids)
    sess.shell_job_observation = {
        "state": "partial" if (state.get("overflow") or ambiguous or pending_overflow
                                 or not source_valid
                                 or cursor.get("state") == "partial")
        else cursor.get("state", "unknown"),
        "reason": ("task-retention-limit" if state.get("overflow") else
                   "native-id-cross-namespace-ambiguous" if ambiguous else
                   "pending-row-refresh-limit" if pending_overflow else
                   "database-identity-unavailable" if not source_valid else cursor.get("reason")),
        "coverage": "partial" if (state.get("overflow") or ambiguous or pending_overflow
                                     or not source_valid
                                     or not cursor.get("caught_up")
                                     or cursor.get("state") != "observed") else "complete",
        "cursor": state.get("cursor"), "source_generation": source,
    }

_REG = {"ts": 0.0, "map": None, "by_provider": None}   # → context window (from models.json)
_REG_TTL = 300.0
# `part` first — the `message` table carries only per-message metadata (role/tokens/cost/
# modelID), never conversational text, so a refresh cursor anchored there advances over
# rows that can never produce a title or summary. Fixed order, no schema guessing.
_MESSAGE_TABLES = ("part", "message", "session_message")


def _attempt_sidecar_fallback(sess):
    """Exact-attempt sidecar fallback (SD-95): a registered dispatch session has
    no statusline producer for its runtime sid; the dispatch summary owner
    writes under the attempt sid. attempt_id is exact env/registry identity."""
    attempt_sid = titles.attempt_sid(getattr(sess, "attempt_id", None))
    if not attempt_sid:
        return
    if not getattr(sess, "title", None):
        sess.title = titles.last_title(attempt_sid, harness="opencode")
    if not getattr(sess, "summary", None):
        sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
            attempt_sid, harness="opencode")


def _resolve_current_identity(sess):
    """Use only the exact live TUI selection as the current conversation claim."""
    from ..process_identity import resolve_session_claims

    pid = getattr(sess, "pid", None)
    expected_start = getattr(sess, "proc_start", None)
    observed_sid, source, rejected_selection = None, "", None
    if pid and expected_start is not None:
        before = _proc_start_ticks(pid)
        if before is not None and str(before) == str(expected_start):
            candidate_sid, candidate_source = session_of_process(pid)
            after = _proc_start_ticks(pid)
            if (after is not None and str(after) == str(expected_start)
                    and str(after) == str(before)):
                observed_sid, source = candidate_sid, candidate_source
            elif candidate_sid and candidate_source == "opencode-tui-selection":
                rejected_selection = candidate_sid
    previous_sid = getattr(sess, "session_id", None)
    claims = {}
    current_source = None
    if observed_sid and source == "opencode-tui-selection":
        claims[source] = observed_sid
        current_source = source
    sid, evidence = resolve_session_claims(claims, current_source=current_source)
    if source == "opencode-tui-selection" and previous_sid and previous_sid != sid:
        # A prior projection is diagnostic history, not an exact identity claim.
        evidence["prior_projection"] = previous_sid
    if observed_sid and source == "opencode-argv":
        evidence["started_on"] = observed_sid
    if rejected_selection:
        evidence["rejected_selection"] = rejected_selection
    evidence.update(pid=getattr(sess, "pid", None),
                    proc_start=getattr(sess, "proc_start", None))
    sess.session_id = sid
    sess.session_identity_evidence = evidence
    return sid


def _clear_session_enrichment(sess):
    """Clear conversation-derived fields while leaving process and attempt data intact."""
    for name, value in (
        ("slug", None), ("title", None), ("title_ts", None),
        ("summary", None), ("summary_ts", None), ("model", None),
        ("effort", None), ("effort_default", False), ("cost", None),
        ("tokens", None), ("session_input_tokens", None),
        ("session_output_tokens", None), ("session_reasoning_output_tokens", None),
        ("session_total_tokens", None), ("active_context_tokens", None),
        ("context_window_tokens", None), ("context_display_window_tokens", None),
        ("context_policy", None), ("ctx_pct", None), ("_context_evidence", None),
        ("_refresh_source", None), ("subagents", None), ("is_child", False),
        ("mtime", None), ("shell_jobs", []), ("shell_job_observation", None),
        ("_opencode_provider_id", None),
        # These fields are populated only from the session registry for OpenCode.
        # Clear them on every pass so an old or rejected record cannot survive a
        # selection change on the same Session object.
        ("status", None), ("kind", None), ("started_at", None), ("updated_at", None),
        ("registry_proc_start", None), ("registry_name", None), ("runtime_name", None),
    ):
        setattr(sess, name, value)
    if not getattr(sess, "session_id", None):
        sess.session_tag = None
        sess._opencode_bind_kind = None
    else:
        sess._opencode_bind_kind = "process"
        from fleet.session_handle import resolve_tag
        sess.session_tag = resolve_tag("opencode", sess.session_id)


def _current_registry_record(sess, sid):
    """Return registry data only when it names this exact live PID/start/SID."""
    pid = getattr(sess, "pid", None)
    expected_start = getattr(sess, "proc_start", None)
    if not sid or pid is None or isinstance(pid, bool) or expected_start is None:
        return None
    try:
        expected_pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return None
    before = _proc_start_ticks(pid)
    if before is None or str(before) != str(expected_start):
        return None
    try:
        record = session_registry.read("opencode", pid)
    except Exception:
        return None
    after = _proc_start_ticks(pid)
    if after is None or str(after) != str(expected_start) or str(after) != str(before):
        return None
    if not isinstance(record, dict):
        return None
    record_pid = record.get("pid")
    if (isinstance(record_pid, bool)
            or not isinstance(record_pid, (int, str))
            or (isinstance(record_pid, str)
                and (not record_pid.isascii() or not record_pid.isdigit()))):
        return None
    try:
        if int(record_pid) != expected_pid:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    record_start = record.get("procStart")
    if record_start is None or isinstance(record_start, bool) or str(record_start) != str(expected_start):
        return None
    # Registry display fields are session-bound. A PID/start match alone cannot
    # attach a prior conversation's name or status to the current TUI selection.
    if record.get("sessionId") != sid:
        return None
    return record


def _opencode_v2_runtime():
    utilities = Path(__file__).resolve().parents[3] / "utilities"
    if str(utilities) not in sys.path:
        sys.path.insert(0, str(utilities))
    import opencode_runtime
    return opencode_runtime


def _enrich_v2(sess, tick, db):
    """Read V2 data for the already-resolved current TUI selection."""
    runtime = _opencode_v2_runtime()
    if not runtime.credential_store_present(db_path=db):
        return False
    sid = getattr(sess, "session_id", None)
    if not sid:
        _attempt_sidecar_fallback(sess)
        return True
    shell_cursor = (None if getattr(sess, "_fast_first", False)
                    else _v2_shell_cursor(db, sid, getattr(sess, "attempt_id", None)))
    shell_refresh_seqs = (_v2_shell_refresh_seqs(db, sid, getattr(sess, "attempt_id", None))
                          if not getattr(sess, "_fast_first", False) else [])
    observed = runtime.read_session_observation(
        sid, db_path=db, shell_after_seq=shell_cursor,
        shell_refresh_seqs=shell_refresh_seqs)
    generation = observed.get("generation") if observed.get("state") == "observed" else None
    if generation != "v2":
        sess.shell_jobs = []
        if generation == "v1":
            sess.shell_job_observation = {
                "state": "unobserved", "reason": "v1-background-native-record-not-observed"}
        else:
            sess.shell_job_observation = {
                "state": observed.get("state", "unknown"),
                "reason": observed.get("reason", "runtime-generation-unresolved")}
        if observed.get("state") == "observed" and generation == "v1":
            return False
        _attempt_sidecar_fallback(sess)
        return True
    if observed.get("state") != "observed" or observed.get("generation") != "v2":
        sess.shell_jobs = []
        sess.shell_job_observation = {"state": observed.get("state", "unknown"),
                                     "reason": observed.get("reason", "v2-source-unavailable")}
        _attempt_sidecar_fallback(sess)
        return True
    _attach_v2_shell_tasks(sess, observed, db, sid,
                           tree=getattr(sess, "_proc_tree", None),
                           kids=getattr(sess, "_proc_kids", None))
    native = observed.get("session") or {}
    sess._opencode_bind_kind = "process"
    sess._refresh_source = {"kind": "opencode-db-v2", "harness": "opencode",
                            "db_path": db, "session_id": sid,
                            "table": "session_message",
                            "cursor_kind": observed.get("cursor_kind"),
                            "observed_cursor": observed.get("cursor")}
    if native.get("directory"):
        from .. import session_cwd
        session_cwd.observe(sess, native["directory"], sid)
    sess.session_id = sid
    sess.title = titles.last_title(sid, harness="opencode")
    sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(sid, harness="opencode")
    _attempt_sidecar_fallback(sess)
    if native.get("slug"):
        sess.slug = native["slug"]
    sess.is_child = bool(native.get("parent_id"))
    model = native.get("model") if isinstance(native.get("model"), dict) else {}
    if model:
        sess.model = model.get("id")
        if model.get("providerID"):
            sess._opencode_provider_id = model["providerID"]
        variant = str(model.get("variant") or "").strip()
        if variant.lower() in _DEFAULT_VARIANTS:
            sess.effort_default = True
        elif variant:
            sess.effort = variant
    for attr, key in (("cost", "cost"), ("session_input_tokens", "tokens_input"),
                      ("session_output_tokens", "tokens_output"),
                      ("session_reasoning_output_tokens", "tokens_reasoning")):
        value = native.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(sess, attr, value)
    values = [native.get(key) for key in ("tokens_input", "tokens_output", "tokens_reasoning")]
    sess.tokens = sum(value for value in values if isinstance(value, (int, float))) or None
    sess.session_total_tokens = sess.tokens
    messages = observed.get("messages") or []
    sess.active_context_tokens = None
    latest = None
    for row in reversed(messages):
        if row.get("type") != "assistant":
            continue
        last_ctx = _context_tokens_from_payload({"tokens": row.get("tokens")})
        if last_ctx == 0 and not (row.get("finish") or row.get("completed")):
            continue
        latest = row
        if last_ctx is not None:
            sess.active_context_tokens = int(last_ctx)
        break
    from ..token_budget import context_policy_snapshot, context_policy_used_pct
    from ..context_policy import read_policy
    request_model = latest.get("model") if latest and isinstance(latest.get("model"), dict) else model
    model_ref = f"{request_model.get('providerID') or ''}/{request_model.get('id') or ''}"
    policy_record = read_policy(harness="opencode", session_id=sid, pid=sess.pid,
                                proc_start=sess.proc_start, model=model_ref)
    policy_data = policy_record.get("policy") if isinstance(policy_record, dict) else None
    updated = latest.get("updated") if latest else None
    if type(updated) not in (int, float):
        updated = native.get("time_updated")
    policy_current = bool(
        policy_record and type(updated) in (int, float)
        and type(policy_record.get("observed_at")) in (int, float)
        and math.isfinite(policy_record["observed_at"])
        and policy_record["observed_at"] <= updated / 1000.0 + 1.0)
    if isinstance(policy_data, dict) and policy_current:
        sess.context_policy = context_policy_snapshot(
            source=policy_data.get("source") or "opencode-native-chat-params",
            runtime_version=policy_data.get("runtime_version"),
            configured_request_tokens=policy_data.get("configured_request_tokens"),
            configured_request=policy_data.get("configured_request"),
            effective_window_tokens=policy_data.get("effective_window_tokens"),
            applied_trigger_tokens=policy_data.get("applied_trigger_tokens"),
            output_allowance_tokens=policy_data.get("output_allowance_tokens"),
            compaction_reserve_tokens=policy_data.get("compaction_reserve_tokens"),
            disabled=policy_data.get("disabled"), enabled=policy_data.get("enabled"),
            enforced=policy_data.get("enforced"), session_id=sid,
            raw_percentage=policy_data.get("raw_percentage"), reason=policy_data.get("reason"))
    else:
        sess.context_policy = context_policy_snapshot(
            source="opencode-db-v2", session_id=sid,
            reason="selected-request-policy-not-observed")
    sess.context_window_tokens = sess.context_policy["effective_window_tokens"]
    sess.context_display_window_tokens = (sess.context_policy["applied_trigger_tokens"]
                                          if sess.context_policy["state"] == "applied" else None)
    sess.ctx_pct = context_policy_used_pct(sess.active_context_tokens, sess.context_policy)
    stamp = native.get("time_updated")
    if isinstance(stamp, (int, float)):
        sess.mtime = stamp / 1000.0
    return True


def _message_table(con):
    """Choose one compatible table in fixed order; no schema guessing by recency."""
    for table in _MESSAGE_TABLES:
        try:
            row = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                con.execute("SELECT rowid FROM %s LIMIT 1" % table).fetchone()
                return table
        except Exception:
            continue
    return None


def _observed_cursor(con, table, sid):
    if not table:
        return None
    try:
        row = con.execute("SELECT MAX(rowid) FROM %s WHERE session_id=?" % table, (sid,)).fetchone()
        return int(row[0]) if row and isinstance(row[0], int) else 0
    except Exception:
        return None


def _load_model_registry():
    """Build (provider-scoped, provider-agnostic) context-window maps. Cached 5 min."""
    now = time.time()
    if _REG["map"] is not None and now - _REG["ts"] <= _REG_TTL:
        return _REG["by_provider"] or {}, _REG["map"] or {}
    scoped, flat = {}, {}
    path = os.environ.get("OPENCODE_MODELS") or os.path.expanduser("~/.cache/opencode/models.json")
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
        for pkey, prov in (reg.items() if isinstance(reg, dict) else []):
            models = prov.get("models") if isinstance(prov, dict) else None
            if not isinstance(models, dict):
                continue
            pid = (prov.get("id") if isinstance(prov, dict) else None) or pkey
            for mkey, mdef in models.items():
                lim = mdef.get("limit") if isinstance(mdef, dict) else None
                ctx = lim.get("context") if isinstance(lim, dict) else None
                if not isinstance(ctx, (int, float)) or ctx <= 0:
                    continue
                for k in (mkey, mkey.split("/")[-1]):   # bare id or provider/org-prefixed
                    scoped.setdefault((pid, k), int(ctx))
                    if flat.get(k, 0) < ctx:
                        flat[k] = int(ctx)
    except Exception:
        scoped, flat = {}, {}
    _REG.update(ts=now, map=flat, by_provider=scoped)
    return scoped, flat


def _model_ctx_limit(model_id, provider=None):
    """Context window for a model id, from opencode's models.dev registry cache
    (~/.cache/opencode/models.json — the same source opencode's own TUI uses for context%).
    None when unavailable → ctx% stays '—'. Cached 5 min.

    The same model id is published by many providers at different window sizes (one id
    in this registry spans a 48k spread across two providers), so the session's own
    providerID decides. The provider-agnostic max is only a last resort for an unknown
    provider — an over-large window understates ctx%, the safer direction to be wrong.
    """
    if not model_id:
        return None
    scoped, flat = _load_model_registry()
    leaf = model_id.split("/")[-1]
    if provider:
        for key in ((provider, model_id), (provider, leaf)):
            if key in scoped:
                return scoped[key]
    return flat.get(model_id) or flat.get(leaf)


def _db():
    return str(_opencode_v2_runtime().opencode_db_path())


SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{1,252}$")
TUI_SELECTION_SCHEMA = "hearting-tui-selection-v1"
TUI_SELECTION_MAX_BYTES = 1024
# Words that make an `opencode` command line something other than an interactive pane.
_NON_PANE_COMMANDS = frozenset(("run", "serve", "attach", "web", "auth", "agent", "models", "stats",
                                "export", "import", "session", "upgrade", "uninstall", "mcp", "acp",
                                "debug", "completion"))


def _proc_start_ticks(pid):
    try:
        with open("/proc/%d/stat" % int(pid), encoding="ascii", errors="replace") as handle:
            stat = handle.read()
        fields = stat[stat.rindex(") ") + 2:].split()
        return fields[19] if fields[19].isdigit() else None
    except (OSError, ValueError, IndexError):
        return None


def _cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % int(pid), "rb") as handle:
            raw = handle.read(32769)
    except (OSError, ValueError):
        return None
    if len(raw) > 32768:
        return None
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _argv_session(argv):
    """The `--session`/`-s` id an `opencode` pane was started on, or None."""
    if not argv or os.path.basename(argv[0]) != "opencode":
        return None
    found = None
    for index, arg in enumerate(argv[1:], 1):
        if arg in _NON_PANE_COMMANDS:
            return None
        key, equal, value = arg.partition("=")
        if key not in ("--session", "-s"):
            continue
        if not equal:
            value = argv[index + 1] if index + 1 < len(argv) else ""
        if found is not None or not SESSION_ID_RE.match(value or ""):
            return None
        found = value
    return found


def _tui_selection_record(pid, start, environ=None):
    """Read the producer's exact lifetime record, including a retained dead one.

    No process, argv, cwd or expected-session lookup supplies this identity.
    Source timestamps are observations; the retire consumer checks acceptance.
    Live readers do not require a retirement timestamp or writtenAt."""
    env = os.environ if environ is None else environ
    home = env.get("HOME") or ""
    base = env.get("XDG_STATE_HOME") or (os.path.join(home, ".local", "state") if home else "")
    if (not base or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or isinstance(start, bool) or not str(start).isdigit() or int(start) <= 0):
        return None
    path = os.path.join(base, "hearting", "tui-identity", "%d-%s.json" % (pid, start))
    def generation(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_uid,
                info.st_mtime_ns, info.st_ctime_ns)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_size > TUI_SELECTION_MAX_BYTES):
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(TUI_SELECTION_MAX_BYTES + 1)
            if (len(raw) > TUI_SELECTION_MAX_BYTES or len(raw) != before.st_size
                    or generation(before) != generation(os.fstat(fd))
                    or generation(before) != generation(os.lstat(path))):
                return None
        finally:
            os.close(fd)
        row = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(row, dict) or row.get("schema") != TUI_SELECTION_SCHEMA
            or type(row.get("pid")) is not int or row["pid"] != pid
            or isinstance(row.get("start"), bool) or str(row.get("start")) != str(start)
            or not isinstance(row.get("sessionID"), str)
            or not SESSION_ID_RE.fullmatch(row["sessionID"])):
        return None
    written = None
    try:
        timestamp = datetime.datetime.fromisoformat(row["writtenAt"].replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            written = timestamp.timestamp()
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        pass
    return {"value": row, "source": {"generation": generation(before),
            "mtime": before.st_mtime, "ctime": before.st_ctime, "written_at": written}}


def _tui_selection(pid, start, environ=None):
    """The exact TUI selection SID or None; live callers check /proc separately."""
    record = _tui_selection_record(pid, start, environ)
    return record["value"]["sessionID"] if record is not None else None


def session_of_process(pid, environ=None):
    """`(session_id, source)` an OpenCode process proves about itself, or `(None, "")`.

    Its TUI's own selection record first (it follows `/new` and session switches),
    then the `--session` it was started with. Never the database's newest session,
    which is a guess shared by every pane in the directory."""
    start = _proc_start_ticks(pid)
    if start is None:
        return None, ""
    selected = _tui_selection(pid, start, environ)
    if selected:
        return selected, "opencode-tui-selection"
    started_on = _argv_session(_cmdline(pid))
    if started_on and _proc_start_ticks(pid) == start:
        return started_on, "opencode-argv"
    return None, ""


def session_id_of_process(pid, environ=None):
    return session_of_process(pid, environ)[0]


# Commands that start no session of their own and open none: a process running one proves
# nothing about any session, either way.
_UTILITY_COMMANDS = frozenset(("agent", "auth", "bench", "completion", "debug", "export", "github",
                               "import", "mcp", "models", "session", "stats", "upgrade", "uninstall"))


def _command_of(argv):
    """``(command, index)`` an `opencode` argv selects, or None when it cannot be read.

    ``command`` is "" when the process is the default TUI (no command word, or a positional
    project/prompt). Same word walk as ``procscan.invocation_mode``."""
    from . import procscan
    if not argv or os.path.basename(argv[0]) != "opencode":
        return None
    i = 1
    while i < len(argv):
        word = argv[i]
        if word == "--":
            return "", i
        if word in procscan._VALUE_OPTIONS:
            if i + 1 >= len(argv):
                return None
            i += 2
            continue
        if word in procscan._FLAG_OPTIONS or (word.startswith("--") and "=" in word):
            i += 1
            continue
        if word.startswith("-"):
            return None
        return (word, i) if word in procscan._COMMANDS["opencode"] else ("", i)
    return "", len(argv)


def _run_session(tail):
    """For the words after `opencode run`: ``(session_id, unresolved)``.

    `--session/-s` names the session it works on; `--continue/-c` and `--fork` pick one the
    command line does not name (unresolved); anything else starts a session of its own."""
    found, unresolved = None, False
    for index, arg in enumerate(tail):
        key, equal, value = arg.partition("=")
        if key in ("--continue", "-c", "--fork"):
            unresolved = True
        elif key in ("--session", "-s"):
            if not equal:
                value = tail[index + 1] if index + 1 < len(tail) else ""
            if SESSION_ID_RE.match(value or ""):
                found = value
            else:
                unresolved = True
    return found, unresolved


def _unseen_clients(servers, clients):
    """True when every connection into a `serve`/`web` listener comes from a known opencode
    client. ``servers``/``clients`` are pid sets; anything unreadable or unmatched is False."""
    from . import codex
    connections = codex._tcp_connections()
    owners = codex._socket_owners_by_inode()
    if connections is None or owners is None:
        return False
    port_of = lambda address: address.rsplit(":", 1)[1]  # noqa: E731 - "hexaddr:hexport"
    for pid in servers:
        mine = {inode for inode, pids in owners.items() if pid in pids}
        ports = {port_of(local) for state, local, _remote, inode in connections
                 if state == "0A" and inode in mine}
        if not ports:
            return False  # a listener this walk cannot see may have clients it cannot see
        for state, local, remote, _inode in connections:
            if state != "01" or port_of(local) not in ports:
                continue
            peers = [row for row in connections
                     if row[0] == "01" and row[1] == remote and row[2] == local]
            who = owners.get(peers[0][3]) if len(peers) == 1 else None
            if not who or not who <= clients:
                return False
    return True


def session_visibility(pids=None):
    """``{"complete", "visible", "earliest_start_ms"}`` for the live OpenCode processes, or
    None when the process table cannot be listed.

    ``visible`` holds every session a running OpenCode client shows (its TUI selection
    record, `--session`, or `run --session`). ``complete`` is False when some process
    could not be classified, a `serve`/`web` runtime has a connection that is not a known
    client, a table was unreadable, or the caller is outside the initial PID namespace:
    then a session missing from ``visible`` is not proven unshown. ``earliest_start_ms``
    is the oldest running OpenCode process's start (epoch ms), None when none runs."""
    from . import procscan
    if pids is None:
        try:
            pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
        except OSError:
            return None
    boot = procscan.boot_time()
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    complete = procscan.initial_pid_namespace() and boot is not None
    uid = os.geteuid()
    visible, servers, clients, earliest = set(), set(), set(), None
    for pid in pids:
        try:
            if os.stat("/proc/%s" % pid).st_uid != uid:
                continue
        except OSError:
            continue
        if procscan._comm_of(pid) != "opencode" or procscan.is_terminal_state(pid):
            continue
        start = _proc_start_ticks(pid)
        if start is None:
            continue
        if boot is not None:
            began = int((boot + int(start) / ticks_per_second) * 1000)
            earliest = began if earliest is None else min(earliest, began)
        selected, _source = session_of_process(pid)
        if selected:
            visible.add(selected)
            clients.add(pid)
            continue
        argv = _cmdline(pid)
        command = _command_of(argv)
        if command is None:
            complete = False
        elif command[0] in ("serve", "web"):
            servers.add(pid)
        elif command[0] in _UTILITY_COMMANDS:
            continue
        elif command[0] == "run":
            found, unresolved = _run_session(argv[command[1] + 1:])
            clients.add(pid)
            if found:
                visible.add(found)
            complete = complete and not unresolved
        else:
            clients.add(pid)
            complete = False  # a TUI/attach that has not chosen a session yet
    if servers and complete:
        complete = _unseen_clients(servers, clients)
    return {"complete": complete, "visible": frozenset(visible), "earliest_start_ms": earliest}


def session_activity(session_id):
    """``(time_updated_ms, time_idle_ms | None)`` of one session, or None when OpenCode's
    database does not hold it (or cannot be read). ``session_v2`` wins; the older
    ``session`` table has no idle mark. Message content is never read."""
    import contextlib
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return None
    try:
        with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % _db(), uri=True,
                                                timeout=0.2)) as con:
            for query in ("SELECT time_updated, time_idle FROM session_v2 WHERE id = ?",
                          "SELECT time_updated, NULL FROM session WHERE id = ?"):
                try:
                    row = con.execute(query, (session_id,)).fetchone()
                except sqlite3.OperationalError:
                    continue
                if row and isinstance(row[0], (int, float)) and not isinstance(row[0], bool):
                    idle = row[1] if isinstance(row[1], (int, float)) else None
                    return row[0], idle
    except (sqlite3.Error, OSError, ValueError):
        return None
    return None


def _keeper_key(sess):
    """Prefer an exact TUI selection, then earliest process start and lowest PID."""
    kind = getattr(sess, "_opencode_bind_kind", None)
    try:
        started = int(getattr(sess, "proc_start", None))
    except (TypeError, ValueError):
        started = None
    try:
        pid = int(getattr(sess, "pid", None))
    except (TypeError, ValueError):
        pid = None
    return (
        {"process": 0}.get(kind, 1),
        started if started is not None and started >= 0 else float("inf"),
        pid if pid is not None else float("inf"),
    )


def _unbind_shadow(sess):
    """Return a duplicate-bound row to anonymous-process state.

    The process row stays (existence is the backbone's decision, PRD §1) but
    carries no session identity, so the exact herdr/steward/peer joins and the
    tag mint — all keyed on session_id — answer on the keeper only. mtime is
    kept: the transcript really is updating, and liveness must stay truthful
    about the live process. slug is restored to the scan-time cwd basename.
    """
    sess.session_id = None
    sess.session_tag = None
    cwd = getattr(sess, "cwd", None)
    sess.slug = os.path.basename(cwd.rstrip("/")) if cwd else None
    sess.title = None
    sess.summary = None
    sess.summary_ts = None
    sess.subagents = None
    sess.model = None
    sess.effort = None
    sess.effort_default = False
    sess.cost = None
    sess.tokens = None
    sess.session_input_tokens = None
    sess.session_output_tokens = None
    sess.session_reasoning_output_tokens = None
    sess.session_total_tokens = None
    sess.active_context_tokens = None
    sess.context_window_tokens = None
    sess.context_display_window_tokens = None
    sess.context_policy = None
    sess.ctx_pct = None
    sess._context_evidence = None
    sess._refresh_source = None
    sess._opencode_bind_kind = None
    sess.title_ts = None
    sess.shell_jobs = []
    sess.shell_job_observation = None
    sess.is_child = False
    sess._opencode_provider_id = None
    # A dispatch attempt is a separate exact identity. Restore only its own
    # sidecar after clearing the conversation-bound fields above.
    _attempt_sidecar_fallback(sess)


def collapse_duplicate_sids(sessions):
    """One session id → one row.

    If multiple rows arrive with the same SID, keep the row with the strongest
    current-selection evidence. This runs after enrichment and before exact
    herdr/steward/peer joins, all of which key on session_id.
    """
    groups = {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "opencode":
            continue
        sid = getattr(sess, "session_id", None)
        if not sid:
            continue
        groups.setdefault(sid, []).append(sess)
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_keeper_key)
        for loser in members[1:]:
            _unbind_shadow(loser)


def _context_tokens_from_payload(payload):
    tokens = payload.get("tokens") if isinstance(payload, dict) else None
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    total_tokens = tokens.get("total")
    if (isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool)
            and math.isfinite(total_tokens) and total_tokens >= 0):
        return int(total_tokens)
    total = 0
    observed = False
    for value in (tokens.get("input"), tokens.get("output"), cache.get("read"), cache.get("write")):
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            total += value
            observed = True
    return total if observed else None


def _last_request_context(con, sid):
    """Latest assistant step prompt size, excluding output/cumulative session totals."""
    for table in ("message", "part", "session_message"):
        try:
            rows = con.execute(
                "SELECT data FROM %s WHERE session_id=? ORDER BY time_updated DESC LIMIT 50" % table,
                (sid,),
            )
        except Exception:
            continue
        for (data,) in rows:
            try:
                payload = json.loads(data) or {}
            except Exception:
                continue
            ctx = _context_tokens_from_payload(payload)
            if ctx == 0 and not _request_completed(payload):
                continue         # a just-created assistant row starts at zero; not a reading
            if ctx is not None:
                return ctx
    return None


def _request_completed(payload):
    """True when an assistant row records that its request finished (finish or time.completed)."""
    if not isinstance(payload, dict):
        return False
    if payload.get("finish"):
        return True
    moment = payload.get("time")
    return isinstance(moment, dict) and bool(moment.get("completed"))


def _child_sessions(con, sid):
    """F-29 (v9, prd.md:292 source #1 — already SELECTing agent/parent_id, previously
    discarded at the `_query` filter). None on any read failure (honest gap, not a guess);
    [] when the query succeeds and finds no children.

    No completion signal exists in this schema (unlike claude's tool_use/tool_result
    pairing) — every row found here is reported active=True; that is not a guess, it is
    the absence of evidence to the contrary, and the absence renders as '—'-adjacent
    (nothing hidden) rather than a fabricated 'done'.
    """
    try:
        rows = con.execute(
            "SELECT id, agent, time_updated FROM session WHERE parent_id=? "
            "ORDER BY time_updated DESC", (sid,),
        ).fetchall()
    except Exception:
        return None
    out = []
    for _cid, agent, tupd in rows:
        out.append(SubAgent(agent_type=agent or None, active=True,
                            started_at=(tupd / 1000.0) if isinstance(tupd, (int, float))
                                      else None,
                            source="opencode-db"))
    return out


def enrich(sess, tick=None):
    # B-5: no hearting-managed writer exists yet for OpenCode (`writer_support ==
    # "not-implemented"`), so this always reads None today — the call is here so the
    # harness-parity contract (D) and a future writer both have one call site, matching
    # claude/codex's tier-1 read at the top of their own `enrich`.
    sess._session_cwd = (None, None)
    sid = _resolve_current_identity(sess)
    _clear_session_enrichment(sess)
    rec = _current_registry_record(sess, sid)
    if rec:
        session_registry.apply_to_session(sess, rec, "opencode")
    if not sid:
        _attempt_sidecar_fallback(sess)
        return
    db = _db()
    if not os.path.exists(db):
        _attempt_sidecar_fallback(sess)
        return
    if _enrich_v2(sess, tick, db):
        return
    con = None
    last_ctx = None
    subagents = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1.0)
        cur = con.cursor()
        row = cur.execute("SELECT %s FROM session WHERE id=? LIMIT 1" % _COLS,
                          (sid,)).fetchone()
        if row:
            sess._opencode_bind_kind = "process"
            directory = cur.execute("SELECT directory FROM session WHERE id=? LIMIT 1",
                                    (sid,)).fetchone()
            if directory:
                from .. import session_cwd
                session_cwd.observe(sess, directory[0], sid)
        if row and row[0]:
            table = _message_table(con)
            cursor = _observed_cursor(con, table, row[0])
            sess._refresh_source = {
                "kind": "opencode-db", "harness": "opencode", "db_path": db,
                "session_id": row[0], "table": table,
                "cursor_kind": "opencode-rowid-v1:%s" % table if table else None,
                "observed_cursor": cursor,
            }
            last_ctx = _last_request_context(con, row[0])
            # R3-2: only query children when `row` is genuinely top-level (parent_id IS NULL,
            # row[-1] here), or querying ITS children would surface grandchildren under the
            # wrong parent.
            if row[-1] is None:
                subagents = _child_sessions(con, row[0])
    except Exception:
        _attempt_sidecar_fallback(sess)
        return
    finally:
        if con is not None:
            con.close()
    if not row:
        _attempt_sidecar_fallback(sess)
        return
    sid, slug, agent, model_j, cost, ti, to, tr, tupd, _parent = row
    sidecar_title = titles.last_title(sid, harness="opencode")
    sidecar_summary, sidecar_summary_ts = titles.fresh_summary_with_ts(
        sid, harness="opencode")
    sess.title = sidecar_title
    if sidecar_summary:
        sess.summary = sidecar_summary
        sess.summary_ts = sidecar_summary_ts
    _attempt_sidecar_fallback(sess)
    sess.subagents = subagents
    if sid:
        sess.session_id = sid
        # Read only a confirmed shared assignment; normal collection seeds new tags.
        from fleet.session_handle import resolve_tag
        sess.session_tag = resolve_tag("opencode", sid)
    if slug:
        sess.slug = slug
    provider = None
    if model_j:
        try:
            mj = json.loads(model_j) or {}
            sess.model = mj.get("id") or model_j
            provider = mj.get("providerID") or None
            # opencode reasoning effort = model JSON 'variant' (e.g. high/low) — user 2026-07-01
            if mj.get("variant"):
                # A real `default` is the runtime naming its own choice, not an unset
                # dial, so it is kept OUT of `effort` (render's `_EMPTY_EFFORT` treats
                # those words as carrying no information) and recorded separately —
                # otherwise "the runtime defaulted" and "we observed nothing" render
                # as the same blank.
                if str(mj["variant"]).strip().lower() in _DEFAULT_VARIANTS:
                    sess.effort_default = True
                else:
                    sess.effort = mj.get("variant")
        except Exception:
            sess.model = model_j
    if isinstance(cost, (int, float)):
        sess.cost = cost
    toks = sum(x for x in (ti, to, tr) if isinstance(x, (int, float)))
    sess.tokens = toks or None
    sess.session_input_tokens = int(ti) if isinstance(ti, (int, float)) else None
    sess.session_output_tokens = int(to) if isinstance(to, (int, float)) else None
    sess.session_reasoning_output_tokens = int(tr) if isinstance(tr, (int, float)) else None
    sess.session_total_tokens = toks or None
    # The latest assistant request alone describes current usage. Cumulative
    # session input is cost telemetry; a catalogue window is not the applied
    # compaction boundary. Preserve an observed zero without manufacturing a %.
    if isinstance(last_ctx, (int, float)) and not isinstance(last_ctx, bool) and last_ctx >= 0:
        sess.active_context_tokens = int(last_ctx)
    from ..token_budget import context_policy_snapshot, context_policy_used_pct
    from ..context_policy import read_policy
    native_model = f"{provider or ''}/{sess.model or ''}"
    policy_record = read_policy(
        harness="opencode", session_id=sid, pid=sess.pid,
        proc_start=sess.proc_start, model=native_model)
    policy_data = policy_record.get("policy") if isinstance(policy_record, dict) else None
    policy_fresh_for_request = bool(
        policy_record and isinstance(tupd, (int, float))
        and isinstance(policy_record.get("observed_at"), (int, float))
        and policy_record["observed_at"] <= tupd / 1000.0 + 1.0)
    if isinstance(policy_data, dict) and policy_fresh_for_request:
        sess.context_policy = context_policy_snapshot(
            source=policy_data.get("source") or "opencode-native-chat-params",
            runtime_version=policy_data.get("runtime_version"),
            configured_request_tokens=policy_data.get("configured_request_tokens"),
            configured_request=policy_data.get("configured_request"),
            effective_window_tokens=policy_data.get("effective_window_tokens"),
            applied_trigger_tokens=policy_data.get("applied_trigger_tokens"),
            output_allowance_tokens=policy_data.get("output_allowance_tokens"),
            compaction_reserve_tokens=policy_data.get("compaction_reserve_tokens"),
            disabled=policy_data.get("disabled"), enabled=policy_data.get("enabled"),
            enforced=policy_data.get("enforced"), session_id=sid,
            raw_percentage=policy_data.get("raw_percentage"),
            reason=policy_data.get("reason"))
        sess.context_window_tokens = sess.context_policy["effective_window_tokens"]
    else:
        sess.context_policy = context_policy_snapshot(
            source="opencode-db", session_id=sid,
            reason="selected-request-policy-not-observed-or-mismatched")
    sess.context_display_window_tokens = (
        sess.context_policy["applied_trigger_tokens"]
        if sess.context_policy["state"] == "applied" else None)
    sess.ctx_pct = context_policy_used_pct(sess.active_context_tokens, sess.context_policy)
    if sess.ctx_pct is not None:
        cursor = (sess._refresh_source or {}).get("observed_cursor", 0)
        sess._context_evidence = ContextEvidence(
            used_pct=sess.ctx_pct, source="opencode-db-native-policy",
            sequence=(tupd or 0, cursor), source_head_sequence=(tupd or 0, cursor),
            observed_at=(tupd / 1000.0 if isinstance(tupd, (int, float)) else None),
            fresh_until=(tupd / 1000.0 + 86400 if isinstance(tupd, (int, float)) else None))
    if isinstance(tupd, (int, float)):
        sess.mtime = tupd / 1000.0                  # ms → s
    # rl_5h / rl_7d: the Go plan account quota rides the usage cache (collectors
    # __init__ account-usage loop) — nothing per-session here stays None.
