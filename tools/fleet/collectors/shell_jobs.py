"""Shared, passive projections for native background shell jobs.

Native handles identify runtime tasks, never operating-system PIDs. This module
keeps those lifecycle facts separate from targeted process and output evidence.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import shlex
import stat
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..model import CURRENT_SHELL_EVIDENCE_MAX_AGE_S, display_shell_jobs


MAX_COMMAND = 120
MAX_READ_PER_TICK = 64 * 1024
MAX_PARTIAL_JSON_LINE = 1024 * 1024
MAX_RETAINED_TASKS = 1024
MAX_PENDING_CALLS = 512
_JSONL_LIMIT = 4096
_STREAMS = OrderedDict()
_OUTPUTS = OrderedDict()
_VERIFIED_PROCESSES = OrderedDict()
_TASKS = OrderedDict()
_CREDENTIAL_NAME = (r"(?:[A-Za-z_][A-Za-z0-9_-]*?)?"
                    r"(?:password|passwd|token|secret|api[_-]?key|authorization)"
                    r"[A-Za-z0-9_-]*")
_CREDENTIAL_VALUE = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;|&]+)"""
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])" + _CREDENTIAL_NAME + r"\s*[:=]\s*" + _CREDENTIAL_VALUE)
_SECRET_OPTION = re.compile(
    r"(?i)(?<![\w-])--?(?:password|passwd|token|secret|api[-_]?key|authorization)"
    r"(?:\s*=\s*" + _CREDENTIAL_VALUE + r"|\s+(?!-{1,2})" + _CREDENTIAL_VALUE
    + r"|(?=$|[\s,;|&]))")
_AUTH_HEADER = re.compile(r"(?i)(?<![\w-])authorization\s*:\s*[^'\x22\x60\r\n;|&]+")


@dataclass(frozen=True)
class ProcObservation:
    """One typed /proc read; missing and inaccessible are different facts."""

    state: str
    value: object = None
    reason: str | None = None


def valid_epoch(value, *, milliseconds=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        result = float(value) / (1000.0 if milliseconds else 1.0)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def parse_time(value):
    """Parse native ISO timestamps without treating malformed values as now."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        return valid_epoch(stamp.astimezone(timezone.utc).timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def command_summary(value):
    if isinstance(value, dict):
        value = value.get("cmd", value.get("command"))
    if not isinstance(value, str):
        return None
    value = "".join(ch if ch >= " " and ch not in "\x7f\x1b" else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip()
    value = _AUTH_HEADER.sub("[redacted]", value)
    value = _SECRET_OPTION.sub("[redacted]", value)
    value = _SECRET_ASSIGNMENT.sub("[redacted]", value)
    return value[:MAX_COMMAND] + ("…" if len(value) > MAX_COMMAND else "") if value else None


def _codex_shell_command_display(value):
    """Remove only a literal native shell ``-c`` wrapper from a Codex command.

    The inner text must parse as argv without unquoted shell operators,
    expansions, or globs.
    This is a presentation hint from a trusted native command field, never a
    process or background inference. Arbitrary tool-script source is not read.
    """
    if not isinstance(value, str):
        return None
    try:
        wrapper = shlex.split(value)
    except ValueError:
        return None
    if (len(wrapper) != 3 or os.path.basename(wrapper[0]) not in {"sh", "bash", "zsh"}
            or wrapper[1] not in {"-c", "-lc"}):
        return None
    try:
        lexer = shlex.shlex(wrapper[2], posix=True,
                            punctuation_chars=";&|<>(){}*?~")
        lexer.whitespace_split = True
        command_argv = list(lexer)
    except (ValueError, TypeError):
        return None
    if (not command_argv
            or any(token and all(char in ";&|<>(){}*?~" for char in token)
                   for token in command_argv)
            or any("$" in token or "`" in token for token in command_argv)):
        return None
    # This helper's options precede its required --; preserve the outer command
    # and summarize only a payload whose option sequence is fully recognized.
    payload = _verification_lease_payload(command_argv)
    if payload is not None:
        command_argv = payload
    return command_summary(shlex.join(command_argv))


def _verification_lease_payload(argv):
    if (len(argv) < 2 or argv[0] not in {"python", "python3"}
            or argv[1] != "utilities/verification-background-lease.py"):
        return None
    i, seen = 2, set()
    while i < len(argv) and argv[i] != "--":
        name, eq, value = argv[i].partition("=")
        if name not in ("--timeout", "--memory-budget") or name in seen:
            return None
        if not eq:
            i += 1
            if i >= len(argv):
                return None
            value = argv[i]
        if name == "--timeout" and not value.replace(".", "", 1).isdigit():
            return None
        if name == "--memory-budget" and (not value or value.startswith("-")):
            return None
        seen.add(name)
        i += 1
    if "--timeout" not in seen or i + 1 >= len(argv):
        return None
    return argv[i + 1:]


def _bounded(cache, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def jsonl_delta(path, cache_key, *, max_bytes=MAX_READ_PER_TICK):
    """Read a bounded append-only JSONL range and retain source-generation cursor.

    Cold readers start at byte zero and advance over later ticks. Rotation or
    truncation starts a new generation; no tail-only read silently loses an open
    task. The caller owns its reduced records and must reset them on ``reset``.
    """
    identity = (str(cache_key), os.path.abspath(path))
    try:
        st = os.stat(path)
        stamp = (st.st_dev, st.st_ino)
        size = st.st_size
    except OSError as exc:
        return {"lines": [], "state": "unavailable", "reason": type(exc).__name__,
                "reset": False, "covered": False, "source": identity,
                "generation": None}
    state = _STREAMS.get(identity)
    reset = not isinstance(state, dict) or state.get("stamp") != stamp or size < state.get("offset", 0)
    if isinstance(state, dict) and not reset and state.get("offset", 0) > 0:
        try:
            with open(path, "rb") as stream:
                boundary_start = max(0, state["offset"] - 256)
                stream.seek(boundary_start)
                boundary = stream.read(state["offset"] - boundary_start)
            reset = boundary != state.get("boundary", b"")
        except OSError as exc:
            return {"lines": [], "state": "unavailable", "reason": type(exc).__name__,
                    "reset": False, "covered": False, "source": identity,
                    "generation": state.get("generation")}
    if reset:
        revision = state.get("revision", 0) + 1 if isinstance(state, dict) else 0
        state = {"stamp": stamp, "offset": 0, "carry": b"", "read_error": None,
                 "revision": revision, "boundary": b""}
    start = state["offset"]
    end = min(size, start + max(1, int(max_bytes)))
    try:
        with open(path, "rb") as stream:
            stream.seek(start)
            data = stream.read(end - start)
        if len(data) != end - start:
            raise OSError("short-read")
    except OSError as exc:
        state["read_error"] = type(exc).__name__
        _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
        return {"lines": [], "state": "unavailable", "reason": state["read_error"],
                "reset": reset, "covered": False, "source": identity,
                "generation": stamp}
    state["read_error"] = None
    state["offset"] = end
    raw = state["carry"] + data
    pieces = raw.split(b"\n")
    state["carry"] = pieces.pop() if pieces else raw
    lines = []
    for item in pieces:
        if not item:
            continue
        try:
            row = json.loads(item.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            state["gap"] = state.get("gap") or "malformed-json-line"
            continue
        if isinstance(row, dict):
            lines.append(row)
    if len(state["carry"]) > MAX_PARTIAL_JSON_LINE:
        state["carry"] = b""
        state["gap"] = state.get("gap") or "partial-json-line-exceeds-limit"
    try:
        with open(path, "rb") as stream:
            boundary_start = max(0, end - 256)
            stream.seek(boundary_start)
            state["boundary"] = stream.read(end - boundary_start)
    except OSError as exc:
        state["read_error"] = type(exc).__name__
        _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
        return {"lines": lines, "state": "unavailable", "reason": state["read_error"],
                "reset": reset, "covered": False, "source": identity,
                "generation": (stamp[0], stamp[1], state["revision"])}
    _bounded(_STREAMS, identity, state, _JSONL_LIMIT)
    return {"lines": lines, "state": "partial" if state.get("gap") else "observed",
            "reason": state.get("gap"),
            "reset": reset, "covered": end >= size and not state["carry"],
            "offset": end, "size": size, "source": identity,
            "generation": (stamp[0], stamp[1], state["revision"])}


def source_status(path, cache_key):
    """Describe exact source coverage without interpreting a miss as no jobs."""
    identity = (str(cache_key), os.path.abspath(path))
    try:
        st = os.stat(path)
    except OSError as exc:
        return {"state": "unavailable", "reason": type(exc).__name__,
                "coverage": "unavailable", "source": os.path.abspath(path)}
    cursor = _STREAMS.get(identity)
    current = (st.st_dev, st.st_ino)
    if not isinstance(cursor, dict) or cursor.get("stamp") != current:
        return {"state": "catching-up", "reason": "source-not-scanned",
                "coverage": "partial", "offset": 0, "size": st.st_size,
                "source": os.path.abspath(path)}
    if cursor.get("read_error"):
        return {"state": "unavailable", "reason": cursor["read_error"],
                "coverage": "unavailable", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    if cursor.get("gap"):
        return {"state": "partial", "reason": cursor["gap"],
                "coverage": "partial", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    task_state = _TASKS.get(cache_key)
    if isinstance(task_state, dict) and task_state.get("coverage_reasons"):
        reasons = list(task_state["coverage_reasons"])
        return {"state": "partial", "reason": reasons[0] if len(reasons) == 1
                else "multiple-observation-gaps", "reasons": reasons,
                "coverage": "partial", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    if isinstance(task_state, dict) and task_state.get("overflow"):
        return {"state": "partial", "reason": "task-retention-limit",
                "coverage": "partial", "offset": cursor.get("offset", 0),
                "size": st.st_size, "source": os.path.abspath(path)}
    offset = cursor.get("offset", 0)
    complete = offset >= st.st_size and not cursor.get("carry")
    if complete and isinstance(task_state, dict) and task_state.get("calls"):
        return {"state": "partial", "reason": "native-call-result-pending",
                "coverage": "partial", "offset": offset, "size": st.st_size,
                "source": os.path.abspath(path)}
    return {"state": "observed" if complete else "catching-up",
            "reason": None if complete else "bounded-source-scan-in-progress",
            "coverage": "complete" if complete else "partial",
            "offset": offset, "size": st.st_size, "source": os.path.abspath(path)}


def _job_key(job):
    return tuple(str(job.get(key) or "") for key in (
        "harness", "session_id", "attempt_id", "source_generation",
        "source_locator", "native_id_kind", "native_id"))


def merge_jobs(*groups):
    """Merge projections of the same exact native task without cwd/time guesses."""
    merged = OrderedDict()
    for group in groups:
        for item in group or ():
            if not isinstance(item, dict) or not item.get("native_id"):
                continue
            key = tuple(str(item.get(field) or "") for field in (
                "harness", "session_id", "attempt_id", "source_generation",
                "source_locator", "native_id_kind", "native_id"))
            previous = merged.get(key, {})
            combined = {**previous, **item}
            if (previous.get("current_generation_valid") is False
                    or item.get("current_generation_valid") is False):
                combined.update(current_generation_valid=False,
                                current_execution_state="unknown",
                                current_execution_reason="source-generation-retired")
            merged[key] = combined
    return list(merged.values())


def _row_time(row):
    if not isinstance(row, dict):
        return None
    value = row.get("timestamp") or row.get("created_at") or row.get("time_created")
    parsed = parse_time(value)
    return parsed if parsed is not None else valid_epoch(
        value, milliseconds=isinstance(value, (int, float)) and value > 100_000_000_000)


def _row_time_problem(row):
    for key in ("timestamp", "created_at", "time_created"):
        if key in row:
            return "missing" if row[key] in (None, "") else "invalid"
    return "missing"


def _set_current_event(item, *, row, source, terminal=False):
    """Record event-based execution evidence without substituting collection time."""
    stamp = _row_time(row)
    item["native_event_at"] = stamp
    if item.get("current_generation_valid") is not False:
        item["current_generation_valid"] = True
    if terminal:
        item.update(current_execution_state="terminal",
                    current_execution_source=source,
                    current_execution_reason="native-terminal-event",
                    current_execution_at=None)
        return
    if stamp is None:
        item.update(current_execution_state="unknown",
                    current_execution_source=source,
                    current_execution_reason="native-event-time-" + _row_time_problem(row),
                    current_execution_at=None)
        return
    previous = valid_epoch(item.get("current_execution_at"))
    if previous is not None and stamp < previous:
        item.update(current_execution_state="unknown",
                    current_execution_source=source,
                    current_execution_reason="native-evidence-time-went-backwards",
                    current_execution_at=None)
        return
    item.update(current_execution_state="running", current_execution_source=source,
                current_execution_reason=None, current_execution_at=stamp)


def _task_cache(cache_key):
    state = _TASKS.get(cache_key)
    if not isinstance(state, dict):
        state = {"calls": OrderedDict(), "jobs": {}, "handles": {},
                 "coverage_reasons": OrderedDict()}
    if not isinstance(state.get("calls"), OrderedDict):
        state["calls"] = OrderedDict(state.get("calls") or {})
    if not isinstance(state.get("coverage_reasons"), OrderedDict):
        state["coverage_reasons"] = OrderedDict(
            (reason, None) for reason in (state.get("coverage_reasons") or ()))
    _TASKS[cache_key] = state
    _TASKS.move_to_end(cache_key)
    while len(_TASKS) > _JSONL_LIMIT:
        _TASKS.popitem(last=False)
    return state


def _mark_partial(state, reason):
    reasons = state.setdefault("coverage_reasons", OrderedDict())
    reasons.setdefault(reason, None)
    while len(reasons) > 8:
        reasons.popitem(last=False)


def _remember_call(state, call_id, call):
    calls = state["calls"]
    calls.pop(call_id, None)
    calls[call_id] = call
    while len(calls) > MAX_PENDING_CALLS:
        calls.popitem(last=False)
        _mark_partial(state, "pending-call-correlation-limit")


def _store_task(state, key, item):
    previous = state["jobs"].get(key)
    if isinstance(previous, dict) and previous.get("current_generation_valid") is False:
        item = dict(item)
        item.update(current_generation_valid=False,
                    current_execution_state="unknown",
                    current_execution_reason="source-generation-retired")
    state["jobs"].pop(key, None)
    state["jobs"][key] = item
    while len(state["jobs"]) > MAX_RETAINED_TASKS:
        state["jobs"].pop(next(iter(state["jobs"])))
        state["overflow"] = True


def _claude_result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content
                          if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


_CLAUDE_TASK_PATH = re.compile(r"(/[^\s\"'`<>]*/tasks/([A-Za-z0-9_-]{4,})\.output)")
_CLAUDE_TASK_ID = re.compile(r"(?i)\btask(?:\s+id)?\s*[:=]\s*[`\"']?([A-Za-z0-9_-]{4,})")


def _claude_task_ack(text, *, background_requested=False):
    lower = text.lower()
    background_ack = ("background" in lower
                      and any(word in lower for word in ("running", "started", "launched")))
    match = _CLAUDE_TASK_PATH.search(text)
    if match and (background_requested or background_ack):
        return match.group(2), match.group(1)
    if not background_requested or not background_ack:
        return None, None
    match = _CLAUDE_TASK_ID.search(text)
    return (match.group(1), None) if match else (None, None)


_CLAUDE_TASK_NOTIFICATION = re.compile(
    r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_CLAUDE_NATIVE_ID = re.compile(r"^[A-Za-z0-9_-]{4,}$")


def _claude_task_notification(row):
    """Parse only Claude's native, provenance-marked terminal task envelope."""
    if not isinstance(row, dict) or row.get("type") != "user":
        return None
    origin = row.get("origin")
    if (not isinstance(origin, dict) or origin.get("kind") != "task-notification"
            or origin.get("producer") != "session-task"
            or row.get("promptSource") != "system"
            or row.get("turnOrigin") != "task_notification"):
        return None
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return None
    blocks = _CLAUDE_TASK_NOTIFICATION.findall(content)
    if len(blocks) != 1:
        return None
    fields = {}
    for name in ("task-id", "tool-use-id", "output-file", "status", "summary"):
        matches = re.findall(r"<%s>(.*?)</%s>" % (name, name), blocks[0], re.DOTALL)
        if len(matches) > 1 or (name in {"task-id", "tool-use-id", "status"}
                                and len(matches) != 1):
            return None
        fields[name] = matches[0].strip() if matches else None
    task_id, call_id, status = fields["task-id"], fields["tool-use-id"], fields["status"]
    if (not isinstance(task_id, str) or not _CLAUDE_NATIVE_ID.fullmatch(task_id)
            or not isinstance(call_id, str) or not _CLAUDE_NATIVE_ID.fullmatch(call_id)):
        return None
    statuses = {"completed": "completed", "complete": "completed",
                "failed": "failed", "error": "failed",
                "stopped": "stopped", "cancelled": "stopped", "canceled": "stopped"}
    native_status = statuses.get((status or "").lower())
    if native_status is None:
        return None
    output_path = fields["output-file"]
    if output_path and (not os.path.isabs(output_path) or "\n" in output_path):
        return None
    summary = fields["summary"] or ""
    exit_codes = re.findall(r"\bexit code\s+(-?\d+)\b", summary, re.IGNORECASE)
    try:
        exit_code = int(exit_codes[0]) if len(exit_codes) == 1 else None
    except (ValueError, OverflowError):
        exit_code = None
    return {"task_id": task_id, "call_id": call_id, "status": native_status,
            "output_path": output_path, "exit_code": exit_code,
            "completed_at": _row_time(row)}


def _claude_row_matches_session(row, session_id):
    """Prefer Claude's native transcript SID over a conflicting runtime SID."""
    native_sid = row.get("sessionId")
    declared_sid = native_sid if isinstance(native_sid, str) and native_sid else row.get("session_id")
    return not isinstance(declared_sid, str) or declared_sid == session_id


def _apply_claude_notification(state, notification, *, path, session_id,
                               attempt_id, generation):
    task_id = notification["task_id"]
    prior = state["jobs"].get(task_id)
    if (not isinstance(prior, dict) or prior.get("harness") != "claude"
            or prior.get("native_task_id") != task_id
            or prior.get("native_call_id") != notification["call_id"]
            or prior.get("session_id") != session_id
            or prior.get("attempt_id") != attempt_id
            or prior.get("source_locator") != os.path.abspath(path)
            or prior.get("source_generation") != str(generation)
            or prior.get("native_run_id")):
        return
    output_path = notification.get("output_path")
    prior_path = prior.get("output_path")
    if output_path and prior_path and output_path != prior_path:
        return
    stamp = notification.get("completed_at")
    prior_stamp = valid_epoch(prior.get("completed_at"))
    if stamp is not None and prior_stamp is not None and stamp < prior_stamp:
        return
    updated = dict(prior)
    updated.update({"native_status": notification["status"],
                    "native_status_source": "claude-task-notification",
                    "completed_at": stamp if stamp is not None else prior.get("completed_at"),
                    "completion_time_source": "claude-task-notification.timestamp"
                    if stamp is not None else prior.get("completion_time_source"),
                    "native_event_at": stamp,
                    "current_execution_state": "terminal",
                    "current_execution_source": "claude.task-notification",
                    "current_execution_reason": "native-terminal-event",
                    "current_execution_at": None})
    if notification.get("exit_code") is not None:
        updated["exit_code"] = notification["exit_code"]
        updated["exit_code_source"] = "claude-task-notification.summary"
    if output_path and not prior_path:
        updated["output_path"] = output_path
        updated["output_reason"] = None
        updated["output_coverage"] = "sampled"
    _store_task(state, task_id, updated)


def _claude_record_kinds(prior, *record_kinds):
    kinds = prior.get("native_record_kinds")
    if not isinstance(kinds, list):
        kinds = [prior["native_record_kind"]] if prior.get("native_record_kind") else []
    for kind in record_kinds:
        if isinstance(kind, str) and kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _claude_tasks_for_call(state, call_id):
    return [item for item in state["jobs"].values()
            if isinstance(item, dict) and item.get("native_call_id") == call_id]


def _claude_native_status(value):
    statuses = {
        "running": "running", "in_progress": "running", "ongoing": "running",
        "background": "running", "completed": "completed", "complete": "completed",
        "exited": "completed", "failed": "failed", "error": "failed",
        "stopped": "stopped", "cancelled": "stopped", "canceled": "stopped",
    }
    return statuses.get(value.lower()) if isinstance(value, str) else None


def _claude_native_start(state, row, *, path, session_id, attempt_id, generation):
    if (row.get("type") != "system" or row.get("subtype") != "task_started"
            or row.get("session_id") != session_id
            or row.get("task_type") != "local_bash"
            or row.get("is_backgrounded") is not True):
        return
    task_id, run_id, call_id = (row.get("task_id"), row.get("run_id"),
                                row.get("tool_use_id"))
    if not all(isinstance(value, str) and _CLAUDE_NATIVE_ID.fullmatch(value)
               for value in (task_id, run_id, call_id)):
        return
    prior = state["jobs"].get(task_id)
    call = state["calls"].get(call_id)
    call_jobs = _claude_tasks_for_call(state, call_id)
    call_job = call_jobs[0] if len(call_jobs) == 1 else None
    if (len(call_jobs) > 1
            or (call_job is not None and call_job.get("native_task_id") != task_id)
            or (call is None and call_job is None)):
        return
    if prior is not None and (
            prior.get("native_call_id") != call_id
            or prior.get("session_id") != session_id
            or prior.get("attempt_id") != attempt_id
            or prior.get("source_locator") != os.path.abspath(path)
            or prior.get("source_generation") != str(generation)
            or (prior.get("native_run_id") and prior.get("native_run_id") != run_id)):
        return
    source = "claude.system.task_started"
    item = dict(prior or {})
    status = str(item.get("native_status") or "").lower()
    terminal = status in {"completed", "complete", "exited", "failed", "error",
                          "cancelled", "canceled", "stopped", "timed_out", "timeout"}
    # task_started has no start clock; the assistant envelope timestamp is not
    # a native task start time.
    item.update({
        "harness": "claude", "session_id": session_id, "attempt_id": attempt_id,
        "source_generation": str(generation), "source_locator": os.path.abspath(path),
        "native_id": task_id, "native_task_id": task_id, "native_call_id": call_id,
        "native_run_id": run_id, "native_task_type": "local_bash",
        "native_record_kind": item.get("native_record_kind") or source,
        "native_record_kinds": _claude_record_kinds(item, source),
        "background_state": "confirmed",
        "background_source": item.get("background_source") or source,
        "background_reason": None,
        "command": item.get("command") or (call or {}).get("command"),
        "command_source": item.get("command_source") or (
            "Bash.input.command" if (call or {}).get("command") else None),
        "started_at": item.get("started_at"),
        "start_source": item.get("start_source"),
        "observed_at": _row_time(row) or item.get("observed_at"),
        "native_status": item.get("native_status") if terminal else "running",
        "native_status_source": (item.get("native_status_source") if terminal else source),
        "process_state": "unknown",
        "process_reason": "native-task-event-has-no-process-identity",
        "output_path": item.get("output_path"),
        "output_reason": item.get("output_reason") or "native-output-locator-unavailable",
        "output_coverage": item.get("output_coverage") or "unavailable",
        "observation_state": "observed", "observation_reason": None,
    })
    item["native_event_at"] = _row_time(row)
    if item.get("current_generation_valid") is not False:
        item["current_generation_valid"] = True
    if item.get("current_execution_state") not in {"running", "terminal"}:
        item.update(current_execution_state="unknown",
                    current_execution_source=source,
                    current_execution_reason="native-task-start-is-not-execution-proof")
    _store_task(state, task_id, item)


def _claude_native_prior(state, row, *, path, session_id, attempt_id, generation):
    if (row.get("type") != "system" or row.get("session_id") != session_id):
        return None
    task_id, run_id = row.get("task_id"), row.get("run_id")
    if (not isinstance(task_id, str) or not _CLAUDE_NATIVE_ID.fullmatch(task_id)
            or not isinstance(run_id, str) or not _CLAUDE_NATIVE_ID.fullmatch(run_id)):
        return None
    prior = state["jobs"].get(task_id)
    if (not isinstance(prior, dict) or prior.get("harness") != "claude"
            or prior.get("native_task_id") != task_id
            or prior.get("native_run_id") != run_id
            or prior.get("native_task_type") != "local_bash"
            or not prior.get("native_call_id")
            or (row.get("tool_use_id") is not None
                and row.get("tool_use_id") != prior.get("native_call_id"))
            or prior.get("session_id") != session_id
            or prior.get("attempt_id") != attempt_id
            or prior.get("source_locator") != os.path.abspath(path)
            or prior.get("source_generation") != str(generation)):
        return None
    return prior


def _apply_claude_native_update(state, row, *, path, session_id, attempt_id, generation):
    if row.get("subtype") != "task_updated":
        return
    prior = _claude_native_prior(
        state, row, path=path, session_id=session_id,
        attempt_id=attempt_id, generation=generation)
    patch = row.get("patch")
    status = _claude_native_status(patch.get("status")) if isinstance(patch, dict) else None
    if prior is None or status is None:
        return
    source = "claude.system.task_updated"
    item = dict(prior)
    old_status = str(item.get("native_status") or "").lower()
    old_terminal = old_status in {"completed", "complete", "exited", "failed", "error",
                                  "cancelled", "canceled", "stopped", "timed_out", "timeout"}
    if old_terminal and status == "running":
        status = old_status
    item.update({
        "native_status": status,
        "native_status_source": source,
        "native_record_kinds": _claude_record_kinds(item, source),
        "observed_at": _row_time(row) or item.get("observed_at"),
    })
    terminal = status in {"completed", "failed", "stopped"}
    if terminal:
        if "end_time" not in patch:
            if valid_epoch(item.get("completed_at")) is None:
                item.update(completed_at=None, completion_time_state="missing",
                            completion_time_reason="native-task-update-has-no-end-time")
        else:
            stamp = valid_epoch(patch.get("end_time"), milliseconds=True)
            if stamp is None:
                if valid_epoch(item.get("completed_at")) is None:
                    item.update(completed_at=None, completion_time_state="invalid",
                                completion_time_reason="invalid-native-task-end-time",
                                completion_time_source=(
                                    "claude.system.task_updated.patch.end_time"))
            else:
                previous = valid_epoch(item.get("completed_at"))
                if previous is not None and stamp < previous:
                    return
                item.update(
                    completed_at=stamp,
                    completion_time_source=(
                        "claude.system.task_updated.patch.end_time"))
                item.pop("completion_time_state", None)
                item.pop("completion_time_reason", None)
    _set_current_event(item, row=row, source=source, terminal=terminal)
    _store_task(state, row["task_id"], item)


def _apply_claude_native_notification(state, row, *, path, session_id,
                                      attempt_id, generation):
    if row.get("subtype") != "task_notification":
        return
    prior = _claude_native_prior(
        state, row, path=path, session_id=session_id,
        attempt_id=attempt_id, generation=generation)
    call_id = row.get("tool_use_id")
    status = _claude_native_status(row.get("status"))
    if (prior is None or call_id != prior.get("native_call_id")
            or status not in {"completed", "failed", "stopped"}):
        return
    source = "claude.system.task_notification"
    item = dict(prior)
    item.update({
        "native_status": status,
        "native_status_source": source,
        "native_record_kinds": _claude_record_kinds(item, source),
    })
    _set_current_event(item, row=row, source=source, terminal=True)
    if valid_epoch(item.get("completed_at")) is None and not item.get("completion_time_state"):
        item.update(completion_time_state="missing",
                    completion_time_reason="task-notification-has-no-timestamp")
    output_path = row.get("output_file")
    if isinstance(output_path, str) and output_path:
        if os.path.isabs(output_path) and "\n" not in output_path:
            prior_path = item.get("output_path")
            if not prior_path or prior_path == output_path:
                item.update(output_path=output_path, output_reason=None,
                            output_coverage="sampled")
        elif not item.get("output_path"):
            item["output_reason"] = "invalid-native-output-locator"
    summary = row.get("summary")
    exit_codes = (re.findall(r"\bexit code\s+(-?\d+)\b", summary, re.IGNORECASE)
                  if isinstance(summary, str) else [])
    if len(exit_codes) == 1:
        try:
            item["exit_code"] = int(exit_codes[0])
            item["exit_code_source"] = "claude.system.task_notification.summary"
        except (ValueError, OverflowError):
            pass
    _store_task(state, row["task_id"], item)


def read_claude_jobs(path, *, session_id, attempt_id=None, now=None):
    """Read exact Claude Bash acknowledgements from an incrementally scanned transcript."""
    source_key = ("claude", os.path.abspath(path), session_id, attempt_id)
    state = _task_cache(source_key)
    delta = jsonl_delta(path, source_key)
    if delta.get("reset"):
        if state["jobs"]:
            retired = list(state.get("retired_jobs", ()))
            for old in state["jobs"].values():
                item = dict(old)
                item.update(observation_state="unknown",
                            observation_reason="source-generation-changed",
                            process_state="unknown",
                            process_reason="source-generation-changed",
                            current_generation_valid=False,
                            current_execution_state="unknown",
                            current_execution_reason="source-generation-retired")
                retired.append(item)
            state["overflow"] = state.get("overflow", False) or len(retired) > 256
            state["retired_jobs"] = retired[-256:]
        state["calls"].clear()
        state["coverage_reasons"].clear()
        state["jobs"].clear()
    for row in delta.get("lines", ()):
        if not isinstance(row, dict):
            continue
        if row.get("type") == "system":
            _claude_native_start(
                state, row, path=path, session_id=session_id,
                attempt_id=attempt_id, generation=delta.get("generation"))
            _apply_claude_native_update(
                state, row, path=path, session_id=session_id,
                attempt_id=attempt_id, generation=delta.get("generation"))
            _apply_claude_native_notification(
                state, row, path=path, session_id=session_id,
                attempt_id=attempt_id, generation=delta.get("generation"))
            continue
        if not _claude_row_matches_session(row, session_id):
            continue
        stamp = _row_time(row)
        row_type = row.get("type")
        message = row.get("message") if isinstance(row.get("message"), dict) else row
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list) and row_type in ("assistant", "user"):
            result_parts = [part for part in content if isinstance(part, dict)
                            and part.get("type") == "tool_result"]
            metadata_candidates = []
            metadata_conflict = False
            if row_type == "user" and len(result_parts) == 1:
                for key, source in (
                        ("toolUseResult", "claude.toolUseResult.backgroundTaskId"),
                        ("tool_use_result", "claude.tool_use_result.backgroundTaskId")):
                    metadata = row.get(key)
                    if (key == "tool_use_result" and row.get("session_id") != session_id):
                        continue
                    candidate = metadata.get("backgroundTaskId") if isinstance(metadata, dict) else None
                    if isinstance(candidate, str) and _CLAUDE_NATIVE_ID.fullmatch(candidate):
                        metadata_candidates.append((candidate, source))
                if len({candidate for candidate, _source in metadata_candidates}) > 1:
                    metadata_conflict = True
            structured_task_id = (metadata_candidates[0][0] if metadata_candidates
                                  and not metadata_conflict else None)
            structured_sources = [source for candidate, source in metadata_candidates
                                  if candidate == structured_task_id]
            for part in content:
                if not isinstance(part, dict):
                    continue
                if row_type == "assistant" and part.get("type") == "tool_use" and part.get("name") == "Bash":
                    call_id = part.get("id")
                    inputs = part.get("input") if isinstance(part.get("input"), dict) else {}
                    if isinstance(call_id, str) and call_id:
                        _remember_call(state, call_id, {
                            "command": command_summary(inputs.get("command")),
                            "started_at": stamp,
                            "run_in_background": inputs.get("run_in_background") is True,
                        })
                elif row_type == "user" and part.get("type") == "tool_result":
                    if metadata_conflict:
                        continue
                    call_id = part.get("tool_use_id")
                    call = state["calls"].pop(call_id, None)
                    if not isinstance(call, dict):
                        continue
                    text_task_id, text_output_path = _claude_task_ack(
                        _claude_result_text(part.get("content")),
                        background_requested=call.get("run_in_background") is True)
                    if structured_task_id:
                        task_id = structured_task_id
                        output_path = (text_output_path if text_task_id in (None, task_id)
                                       else None)
                    else:
                        task_id, output_path = text_task_id, text_output_path
                    if not task_id:
                        continue
                    call_jobs = _claude_tasks_for_call(state, call_id)
                    prior = state["jobs"].get(task_id)
                    if (len(call_jobs) > 1
                            or (call_jobs and call_jobs[0].get("native_task_id") != task_id)
                            or (prior is not None and prior.get("native_call_id") != call_id)):
                        continue
                    ack_kind = "claude.background-bash-task-ack"
                    record_kinds = _claude_record_kinds(
                        prior or {}, *(structured_sources or [ack_kind]))
                    if ack_kind not in record_kinds:
                        record_kinds = _claude_record_kinds(
                            dict(prior or {}, native_record_kinds=record_kinds), ack_kind)
                    native_record_kind = ((prior or {}).get("native_record_kind")
                                          or (structured_sources[0] if structured_sources
                                              else ack_kind))
                    background_confirmed = bool(structured_task_id) or (
                        (prior or {}).get("background_state") == "confirmed")
                    background_source = ((prior or {}).get("background_source")
                                         or (structured_sources[0] if structured_sources else None))
                    snake_metadata = any(source.startswith(
                        "claude.tool_use_result.") for source in structured_sources)
                    started_at = (prior or {}).get("started_at")
                    start_source = (prior or {}).get("start_source")
                    if started_at is None and not snake_metadata:
                        started_at = call.get("started_at")
                        start_source = ("Bash.tool_use.timestamp"
                                        if started_at is not None else None)
                    prior_status = str((prior or {}).get("native_status") or "").lower()
                    terminal = prior_status in {
                        "completed", "complete", "exited", "failed", "error",
                        "cancelled", "canceled", "stopped", "timed_out", "timeout"}
                    item = dict(prior or {})
                    item.update({
                        "harness": "claude", "session_id": session_id,
                        "attempt_id": attempt_id, "source_generation": str(delta.get("generation")),
                        "source_locator": os.path.abspath(path), "native_id": task_id,
                        "native_record_kind": native_record_kind,
                        "native_record_kinds": record_kinds,
                        "background_state": "confirmed" if background_confirmed else "unknown",
                        "background_source": background_source if background_confirmed else None,
                        "background_reason": (None if background_confirmed else
                                              "claude-native-background-task-metadata-unavailable"),
                        "native_call_id": call_id, "native_task_id": task_id,
                        "native_run_id": (prior or {}).get("native_run_id"),
                        "native_task_type": (prior or {}).get("native_task_type"),
                        "command": (prior or {}).get("command") or call.get("command"),
                        "command_source": ((prior or {}).get("command_source")
                                           or ("Bash.input.command" if call.get("command") else None)),
                        "started_at": started_at, "start_source": start_source,
                        "native_event_at": stamp, "observed_at": stamp,
                        "native_status": ((prior or {}).get("native_status")
                                          if terminal or prior_status == "running"
                                          else "acknowledged"),
                        "native_status_source": ((prior or {}).get("native_status_source")
                                                 if terminal or prior_status == "running"
                                                 else "claude.tool_result.acknowledgement"),
                        "process_state": "unknown",
                        "process_reason": "task-ack-is-not-process-proof",
                        "output_path": ((prior or {}).get("output_path") or output_path),
                        "output_reason": (None if (prior or {}).get("output_path") or output_path
                                          else "ack-has-no-exact-output-path"),
                        "output_coverage": ("sampled" if (prior or {}).get("output_path")
                                            or output_path else "unavailable"),
                        "observation_state": delta.get("state"),
                        "observation_reason": delta.get("reason"),
                    })
                    if item.get("current_generation_valid") is not False:
                        item["current_generation_valid"] = True
                    if item.get("current_execution_state") not in {"running", "terminal"}:
                        item.update(current_execution_state="unknown",
                                    current_execution_source="claude.tool_result.acknowledgement",
                                    current_execution_reason="background-acknowledgement-is-not-execution-proof")
                    _store_task(state, task_id, item)
        notification = _claude_task_notification(row)
        if (notification is not None and row.get("sessionId") == session_id):
            _apply_claude_notification(
                state, notification, path=path, session_id=session_id,
                attempt_id=attempt_id, generation=delta.get("generation"))
    result = []
    collection_at = valid_epoch(now) if now is not None else None
    collection_at = time.time() if collection_at is None else collection_at
    for item in list(state.get("retired_jobs", ())) + list(state["jobs"].values()):
        current = dict(item)
        current["observed_at"] = collection_at
        if delta.get("state") != "observed":
            current["observation_state"] = delta.get("state")
            current["observation_reason"] = delta.get("reason")
        normalized = normalize_job(current, now=now)
        if normalized is not None:
            result.append(normalized)
    return result


def _codex_payload(row):
    if not isinstance(row, dict):
        return None
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else row


def _terminal_job(item):
    if not isinstance(item, dict):
        return False
    status = str(item.get("native_status") or "").lower()
    return (status in {"completed", "complete", "exited", "failed", "error",
                       "cancelled", "canceled", "stopped", "timed_out", "timeout"}
            or item.get("completed_at") is not None
            or (isinstance(item.get("exit_code"), int)
                and not isinstance(item.get("exit_code"), bool)))


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _codex_output_objects(value):
    # Only a structured native result object carries tool metadata provenance.
    # Text and input_text blocks may be arbitrary command/script stdout.
    return [value] if isinstance(value, dict) else []


def read_codex_jobs(path, *, session_id, attempt_id=None, now=None):
    """Project native command_execution events and structured direct Codex results."""
    source_key = ("codex", os.path.abspath(path), session_id, attempt_id)
    state = _task_cache(source_key)
    delta = jsonl_delta(path, source_key)
    if delta.get("reset"):
        if state["jobs"]:
            retired = list(state.get("retired_jobs", ()))
            for old in state["jobs"].values():
                item = dict(old)
                item.update(observation_state="unknown",
                            observation_reason="source-generation-changed",
                            process_state="unknown",
                            process_reason="source-generation-changed",
                            current_generation_valid=False,
                            current_execution_state="unknown",
                            current_execution_reason="source-generation-retired")
                retired.append(item)
            state["overflow"] = state.get("overflow", False) or len(retired) > 256
            state["retired_jobs"] = retired[-256:]
        state["calls"].clear()
        state["coverage_reasons"].clear()
        state["jobs"].clear()
        state["handles"].clear()
    for row in delta.get("lines", ()):
        event = _codex_payload(row)
        event_type = event.get("type") if isinstance(event, dict) else None
        item = event.get("item") if isinstance(event, dict) else None
        if (event_type in {"item.started", "item.completed"}
                and isinstance(item, dict) and item.get("type") == "command_execution"):
            declared_sid = (row.get("thread_id") or row.get("session_id")
                            or event.get("thread_id") or event.get("session_id"))
            if isinstance(declared_sid, str) and declared_sid != session_id:
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id:
                _mark_partial(state, "native-command-event-missing-item-id")
                continue
            stamp = _row_time(row) or _row_time(event)
            job_key = "item:" + item_id
            prior = state["jobs"].get(job_key, {})
            completed = event_type == "item.completed"
            command_display = _codex_shell_command_display(item.get("command"))
            output_seen = isinstance(item.get("aggregated_output"), str) and bool(
                item.get("aggregated_output"))
            exit_code = item.get("exit_code")
            job = dict(prior)
            job.update({
                "harness": "codex", "session_id": session_id, "attempt_id": attempt_id,
                "source_generation": str(delta.get("generation")),
                "source_locator": os.path.abspath(path), "native_id": job_key,
                "native_item_id": item_id,
                "native_record_kind": "codex.command_execution",
                "command": command_summary(item.get("command")) or prior.get("command"),
                "command_display": command_display or prior.get("command_display"),
                "command_display_source": (
                    "codex.native-shell-command.argv"
                    if command_display
                    else prior.get("command_display_source")),
                "command_source": "codex.command_execution.command" if item.get("command")
                else prior.get("command_source"),
                "command_reason": None if item.get("command") or prior.get("command")
                else "native-command-unavailable",
                "started_at": (prior.get("started_at") or stamp) if not completed
                else prior.get("started_at"),
                "start_source": prior.get("start_source") or (
                    "codex.item.started.timestamp" if not completed and stamp is not None else None),
                "native_event_at": stamp, "observed_at": stamp,
                "native_status": "completed" if completed else "running",
                "native_status_source": "codex.item.completed" if completed
                else "codex.item.started",
                "completed_at": stamp if completed else prior.get("completed_at"),
                "exit_code": exit_code if isinstance(exit_code, int)
                and not isinstance(exit_code, bool) else prior.get("exit_code"),
                "process_state": "unknown",
                "process_reason": "native-command-event-has-no-process-identity",
                "background_state": "unknown",
                "background_source": None,
                "background_reason": "native-command-event-does-not-expose-background-mode",
                "output_coverage": "sampled",
                "output_reason": "native-command-output-is-sampled" if output_seen
                else "native-output-not-present",
                "last_output_at": stamp if output_seen else prior.get("last_output_at"),
                "last_output_observed_at": stamp,
                "observation_state": delta.get("state"),
                "observation_reason": delta.get("reason"),
            })
            if _terminal_job(prior):
                for field in ("native_status", "native_status_source", "completed_at",
                              "exit_code", "process_state", "process_reason"):
                    if field in prior:
                        job[field] = prior[field]
                job.update(current_execution_state="terminal",
                           current_execution_reason="native-terminal-cache-prevents-resurrection")
            if job.get("current_generation_valid") is not False:
                job["current_generation_valid"] = True
            job.setdefault("current_execution_state", "unknown")
            job.setdefault("current_execution_source", "codex.command_execution")
            job.setdefault("current_execution_reason",
                           "app-server-command-record-does-not-confirm-background-execution")
            _store_task(state, job_key, job)
            continue
        payload = _codex_payload(row)
        if not isinstance(payload, dict):
            continue
        declared_sid = (row.get("thread_id") or row.get("session_id")
                        or payload.get("thread_id")) if isinstance(row, dict) else None
        if isinstance(declared_sid, str) and declared_sid != session_id:
            continue
        kind = payload.get("type")
        call_id = payload.get("call_id") or payload.get("id")
        name = payload.get("name")
        stamp = _row_time(row) or _row_time(payload)
        if kind in {"custom_tool_call", "function_call"} and isinstance(call_id, str):
            args = _json_object(payload.get("input", payload.get("arguments")))
            if name in {"exec_command", "command_execution", "shell", "write_stdin",
                        "functions.exec", "functions.write_stdin"}:
                handle = args.get("session_id") if isinstance(args, dict) else None
                target = state["handles"].get(str(handle)) if handle is not None else None
                _remember_call(state, call_id, {
                    "call_id": call_id, "name": name, "args": args,
                    "started_at": stamp,
                    "handle": str(handle) if handle is not None else None,
                    "job_key": target,
                })
                if name in {"functions.exec", "functions.write_stdin"}:
                    _mark_partial(state, "outer-functions-exec-output-untrusted" if
                                  name == "functions.exec" else
                                  "outer-functions-write-stdin-output-untrusted")
        elif kind in {"custom_tool_call_output", "function_call_output"}:
            if not isinstance(call_id, str) or not call_id:
                _mark_partial(state, "native-call-result-missing-call-id")
                continue
            call = state["calls"].pop(call_id, None)
            if not isinstance(call, dict):
                if not state.get("coverage_reasons"):
                    _mark_partial(state, "native-call-result-without-retained-call")
                continue
            if call.get("name") in {"functions.exec", "functions.write_stdin"}:
                _mark_partial(state, "outer-functions-exec-output-untrusted" if
                              call.get("name") == "functions.exec" else
                              "outer-functions-write-stdin-output-untrusted")
                continue
            if call.get("name") not in {"exec_command", "write_stdin"}:
                _mark_partial(state, "codex-command-result-provenance-unavailable")
                continue
            raw_output = payload.get("output")
            objects = _codex_output_objects(raw_output)
            if not objects:
                _mark_partial(state, "codex-native-result-not-structured")
                continue
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            for detail in objects:
                exit_code = detail.get("exit_code")
                status_value = str(detail.get("status") or "").lower()
                terminal = ((isinstance(exit_code, int) and not isinstance(exit_code, bool))
                            or (call.get("name") not in {"functions.exec", "functions.write_stdin"}
                                and status_value in {"exited", "completed", "failed",
                                                     "cancelled", "canceled"}))
                handle_value = detail.get("session_id")
                # The installed tool envelope uses an integer exec session handle.
                # Never reinterpret a model thread id, cell id, or arbitrary string as it.
                handle = (str(handle_value) if isinstance(handle_value, int)
                          and not isinstance(handle_value, bool) else call.get("handle"))
                if handle is None and not terminal:
                    _mark_partial(state, "codex-native-result-lifecycle-unavailable")
                    continue
                job_key = ("handle:" + handle if handle is not None
                           else call.get("job_key") or "call:" + str(call_id))
                prior = state["jobs"].get(job_key, {})
                command = (args.get("cmd", args.get("command"))
                           if isinstance(args, dict) else None)
                if not command:
                    command = prior.get("command")
                returned_handle = (str(handle_value) if isinstance(handle_value, int)
                                   and not isinstance(handle_value, bool) else None)
                ongoing_status = status_value in {"", "running", "in_progress", "ongoing"}
                request_handle = args.get("session_id") if isinstance(args, dict) else None
                continuation_matches = bool(
                    returned_handle and not terminal and ongoing_status
                    and (call.get("name") == "exec_command"
                         or (call.get("name") == "write_stdin"
                             and isinstance(request_handle, int)
                             and not isinstance(request_handle, bool)
                             and str(request_handle) == returned_handle
                             and call.get("handle") == returned_handle)))
                background_state = "confirmed" if continuation_matches else (
                    "confirmed" if prior.get("background_state") == "confirmed" else "unknown")
                background_source = (
                    "codex.exec_command.native-continuation-handle"
                    if continuation_matches and call.get("name") == "exec_command"
                    else "codex.write_stdin.native-continuation-handle"
                    if continuation_matches and call.get("name") == "write_stdin"
                    else prior.get("background_source"))
                background_reason = (None if background_state == "confirmed" else
                                    "native-result-has-no-exact-ongoing-continuation-handle")
                command_display = _codex_shell_command_display(command) or prior.get(
                    "command_display")
                nested_output = detail.get("output")
                output_seen = isinstance(nested_output, str) and bool(nested_output)
                if not objects or detail == {}:
                    if call.get("name") != "write_stdin":
                        output_seen = False
                started = prior.get("started_at") or call.get("started_at")
                job = dict(prior)
                job.update({
                    "harness": "codex", "session_id": session_id, "attempt_id": attempt_id,
                    "source_generation": str(delta.get("generation")),
                    "source_locator": os.path.abspath(path), "native_id": job_key,
                    "native_record_kind": prior.get("native_record_kind") or
                    "codex." + str(call.get("name") or "command-result"),
                    "native_call_id": call_id, "continuation_handle": handle,
                    "command": command_summary(command),
                    "command_display": command_display,
                    "command_display_source": (
                        "codex.native-shell-command.argv" if command_display
                        and _codex_shell_command_display(command) else
                        prior.get("command_display_source")),
                    "command_source": prior.get("command_source") or (
                        ("exec_command.arguments.cmd" if args.get("cmd") is not None
                         else "exec_command.arguments.command") if command else None),
                    "command_reason": None if command else "nested-command-unavailable",
                    "started_at": started, "start_source": "native-call.timestamp",
                    "native_event_at": stamp, "observed_at": stamp,
                    "native_status": "completed" if terminal else "running" if handle is not None else "unknown",
                    "native_status_source": "codex." + str(call.get("name")) + ".structured-result",
                    "background_state": background_state,
                    "background_source": background_source,
                    "background_reason": background_reason,
                    "completed_at": stamp if terminal else prior.get("completed_at"),
                    "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    else prior.get("exit_code"),
                    "process_state": "unknown", "process_reason": "codex-handle-is-not-a-pid",
                    "output_coverage": "sampled", "output_reason": "poll-response-is-sampled",
                    "last_output_at": stamp if output_seen else prior.get("last_output_at"),
                    "last_output_observed_at": stamp,
                    "observation_state": delta.get("state"),
                    "observation_reason": delta.get("reason"),
                })
                if job.get("current_generation_valid") is not False:
                    job["current_generation_valid"] = True
                event_time_row = row if _row_time(row) is not None else payload
                if _terminal_job(prior):
                    for field in ("native_status", "native_status_source", "completed_at",
                                  "exit_code", "process_state", "process_reason"):
                        if field in prior:
                            job[field] = prior[field]
                    job.update(current_execution_state="terminal",
                               current_execution_source=prior.get("current_execution_source"),
                               current_execution_reason="native-terminal-cache-prevents-resurrection",
                               current_execution_at=None)
                elif terminal:
                    _set_current_event(job, row=event_time_row,
                                       source=job["native_status_source"],
                                       terminal=True)
                elif continuation_matches:
                    _set_current_event(job, row=event_time_row, source=background_source)
                elif job.get("current_execution_state") not in {"running", "terminal"}:
                    job.update(current_execution_state="unknown",
                               current_execution_source=background_source,
                               current_execution_reason=(
                                   "native-result-has-no-exact-ongoing-continuation"))
                _store_task(state, job_key, job)
                if handle is not None:
                    state["handles"][handle] = job_key
    result = []
    collection_at = valid_epoch(now) if now is not None else None
    collection_at = time.time() if collection_at is None else collection_at
    for item in list(state.get("retired_jobs", ())) + list(state["jobs"].values()):
        current = dict(item)
        current["observed_at"] = collection_at
        if state.get("overflow"):
            current["observation_state"] = "partial"
            current["observation_reason"] = "task-retention-limit"
        if state.get("coverage_reasons"):
            current["observation_state"] = "partial"
            current["observation_reason"] = next(iter(state["coverage_reasons"]))
        if delta.get("state") != "observed":
            current["observation_state"] = delta.get("state")
            current["observation_reason"] = delta.get("reason")
        normalized = normalize_job(current, now=now)
        if normalized is not None:
            result.append(normalized)
    return result


def normalize_job(job, *, now=None):
    """Sanitize a native projection and compute time/output advisory fields."""
    if not isinstance(job, dict):
        return None
    now = time.time() if now is None else valid_epoch(now)
    if now is None:
        return None
    result = dict(job)
    result["command"] = command_summary(result.get("command"))
    result["command_display"] = command_summary(result.get("command_display"))
    record_kind = result.get("native_record_kind")
    result["native_record_kind"] = (record_kind if isinstance(record_kind, str)
                                    and record_kind else None)
    background_source = result.get("background_source")
    if (result.get("background_state") == "confirmed"
            and isinstance(background_source, str) and background_source):
        result["background_state"] = "confirmed"
        result["background_reason"] = None
    else:
        result["background_state"] = "unknown"
        result["background_source"] = None
        reason = result.get("background_reason")
        result["background_reason"] = (reason if isinstance(reason, str) and reason
                                       else "native-background-evidence-unavailable")
    result["started_at"] = valid_epoch(result.get("started_at"))
    observed_at = valid_epoch(result.get("observed_at"))
    result["observed_at"] = observed_at if observed_at is not None else now
    result["observation_time_state"] = (
        "future" if observed_at is not None and observed_at > now
        else "valid" if observed_at is not None else "collection-time")
    result["observation_time_reason"] = (
        "future-observation-time" if observed_at is not None and observed_at > now
        else "missing-or-invalid-observation-time" if observed_at is None else None)
    raw_event_at = result.get("native_event_at")
    native_event_at = valid_epoch(raw_event_at)
    result["native_event_at"] = native_event_at
    result["native_event_time_state"] = (
        "missing" if raw_event_at is None else
        "invalid" if native_event_at is None else
        "future" if native_event_at > now else "valid")
    result["native_event_time_reason"] = (
        "native-event-time-missing" if raw_event_at is None else
        "native-event-time-invalid" if native_event_at is None else
        "native-event-time-future" if native_event_at > now else None)
    generation_valid = result.get("current_generation_valid")
    if not isinstance(generation_valid, bool):
        generation_valid = None
    result["current_generation_valid"] = generation_valid
    execution_state = result.get("current_execution_state")
    execution_source = result.get("current_execution_source")
    raw_execution_at = result.get("current_execution_at")
    execution_at = valid_epoch(raw_execution_at)
    result["current_execution_at"] = execution_at
    if generation_valid is False:
        result.update(current_execution_state="unknown",
                      current_execution_reason="source-generation-retired")
    elif execution_state == "running":
        if not isinstance(execution_source, str) or not execution_source:
            result.update(current_execution_state="unknown",
                          current_execution_reason="current-evidence-source-missing")
        elif execution_at is None:
            result.update(current_execution_state="unknown",
                          current_execution_reason=("current-evidence-time-missing"
                              if raw_execution_at is None else "current-evidence-time-invalid"))
        elif execution_at > now:
            result.update(current_execution_state="unknown",
                          current_execution_reason="current-evidence-time-future")
        elif now - execution_at > CURRENT_SHELL_EVIDENCE_MAX_AGE_S:
            result.update(current_execution_state="unknown",
                          current_execution_reason="current-execution-evidence-stale")
        else:
            result.update(current_execution_state="running", current_execution_reason=None)
    elif execution_state == "terminal":
        result.update(current_execution_state="terminal",
                      current_execution_reason=(result.get("current_execution_reason")
                                                or "native-terminal-event"))
    else:
        result["current_execution_state"] = "unknown"
        reason = result.get("current_execution_reason")
        result["current_execution_reason"] = (
            reason if isinstance(reason, str) and reason else "current-execution-evidence-unavailable")
    raw_completed_at = result.get("completed_at")
    completed_at = valid_epoch(raw_completed_at)
    started = result["started_at"]
    result["completed_at"] = completed_at
    if raw_completed_at is None:
        previous_state = result.get("completion_time_state")
        previous_reason = result.get("completion_time_reason")
        if (previous_state in {"invalid", "future", "missing"}
                and isinstance(previous_reason, str) and previous_reason):
            completion_state, completion_reason = previous_state, previous_reason
        else:
            completion_state, completion_reason = "missing", "missing-completion-time"
    elif completed_at is None:
        completion_state, completion_reason = "invalid", "invalid-completion-time"
    elif completed_at > now:
        completion_state, completion_reason = "future", "future-completion-time"
    elif started is not None and completed_at < started:
        completion_state, completion_reason = "invalid", "completion-before-start"
    else:
        completion_state, completion_reason = "valid", None
    result["completion_time_state"] = completion_state
    result["completion_time_reason"] = completion_reason
    if result.get("exit_code") is not None and (
            not isinstance(result["exit_code"], int) or isinstance(result["exit_code"], bool)):
        result["exit_code"] = None
        result["exit_code_reason"] = "invalid-exit-code"
    reason = result.get("observation_reason")
    if started is None:
        result["time_state"] = "start-unavailable"
        result["time_reason"] = reason or "missing-or-invalid-start"
    elif started > now:
        result["time_state"] = "invalid"
        result["time_reason"] = "future-start"
    else:
        result["time_state"] = "valid"
        result["time_reason"] = None

    status = str(result.get("native_status") or "unknown").lower()
    terminal = status in {"completed", "complete", "exited", "failed", "error",
                          "cancelled", "canceled", "stopped", "timed_out", "timeout"}
    if completion_state == "valid" or terminal or result.get("exit_code") is not None:
        result["lifecycle_state"] = "completed"
    elif status in {"running", "in_progress", "ongoing", "background"}:
        result["lifecycle_state"] = "running"
    else:
        result["lifecycle_state"] = "unknown"
    if result["lifecycle_state"] == "completed":
        result["elapsed_s"] = (max(0, int(completed_at - started))
                               if completion_state == "valid" and started is not None
                               and started <= completed_at else None)
    else:
        result["elapsed_s"] = (max(0, int(now - started))
                               if started is not None and started <= now else None)

    output_path = result.get("output_path")
    output_reason = result.get("output_reason")
    if isinstance(output_path, str) and output_path:
        try:
            st = os.stat(output_path)
            if not stat.S_ISREG(st.st_mode):
                raise OSError("not-regular-file")
            output_id = _job_key(result) + (output_path,)
            sequence = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            previous = _OUTPUTS.get(output_id)
            continuous = bool(previous and previous[0][:2] == sequence[:2]
                              and st.st_size >= previous[0][2])
            changed = not previous or previous[0] != sequence
            observed = now
            mtime = valid_epoch(st.st_mtime)
            previous_time_valid = bool(previous and len(previous) > 4 and previous[4])
            previous_mtime = previous[5] if previous and len(previous) > 5 else None
            clock_went_back = bool(previous and len(previous) > 3 and observed < previous[3])
            mtime_went_back = bool(previous_time_valid and previous_mtime is not None
                                   and mtime is not None and mtime < previous_mtime)
            if clock_went_back:
                output_time_state, output_time_reason = "invalid", "observer-clock-went-backwards"
            elif mtime_went_back:
                output_time_state, output_time_reason = "invalid", "output-mtime-went-backwards"
            elif mtime is None:
                output_time_state, output_time_reason = "invalid", "invalid-output-mtime"
            elif mtime > now:
                output_time_state, output_time_reason = "future", "future-output-mtime"
            else:
                output_time_state, output_time_reason = "valid", None
            valid_output_time = output_time_state == "valid"
            last_output = mtime if valid_output_time and st.st_size > 0 else None
            reset_quiet_window = (not valid_output_time or not previous_time_valid
                                  or not previous or previous[0][:2] != sequence[:2]
                                  or st.st_size < previous[0][2])
            if reset_quiet_window:
                quiet_since = max(started or observed, observed)
            else:
                quiet_since = previous[2] if previous and continuous else observed
            _bounded(_OUTPUTS, output_id,
                     (sequence, last_output, quiet_since, observed, valid_output_time, mtime),
                     _JSONL_LIMIT * 8)
            result.update(output_coverage="continuous-file" if continuous else "sampled",
                          output_state="observed", output_size=st.st_size,
                          output_identity={"device": st.st_dev, "inode": st.st_ino},
                          last_output_at=last_output,
                          last_output_observed_at=observed,
                          quiet_since=quiet_since,
                          output_time_state=output_time_state,
                          output_time_reason=output_time_reason,
                          output_reason=None)
        except OSError as exc:
            result.update(output_coverage="unavailable", output_state="unavailable",
                          output_reason=type(exc).__name__, last_output_observed_at=now)
    else:
        result.update(output_coverage=result.get("output_coverage") or "unavailable",
                      output_state="unavailable",
                      output_reason=output_reason or "native-output-source-unavailable",
                      last_output_observed_at=now)
    process_state = result.get("process_state")
    if process_state not in {"alive", "dead", "unknown"}:
        result["process_state"] = "unknown"
        result["process_reason"] = "process-state-invalid"
    last_output = valid_epoch(result.get("last_output_at"))
    quiet_since = valid_epoch(result.get("quiet_since"))
    output_age = now - last_output if last_output is not None else None
    observation_age = now - quiet_since if quiet_since is not None else None
    quiet_age = (min(output_age, observation_age) if output_age is not None
                 and observation_age is not None else observation_age)
    result["quiet_suspected"] = bool(
        result["lifecycle_state"] != "completed"
        and result.get("output_coverage") == "continuous-file"
        and result.get("output_time_state") == "valid"
        and result.get("output_size", 0) > 0 and last_output is not None
        and (result.get("process_state") == "alive"
             or result.get("lifecycle_state") == "running")
        and quiet_age is not None and quiet_age >= 600
        and quiet_since is not None and quiet_since <= now)
    result.setdefault("process_state", "unknown")
    result.setdefault("process_reason", "native-handle-is-not-a-pid")
    result["elapsed_s"] = result.get("elapsed_s")
    return result


def _observe_proc_value(pid, kind):
    """Read one process identity fact without collapsing absence and access failure."""
    try:
        root = "/proc/self" if pid == "self" else f"/proc/{int(pid)}"
        if kind == "start":
            with open(root + "/stat", encoding="ascii") as stream:
                raw = stream.read()
            tail = raw[raw.rfind(")") + 2:].split()
            if len(tail) <= 19:
                return ProcObservation("unavailable", reason="malformed-proc-stat")
            value = tail[19]
        elif kind == "pid_namespace":
            value = os.readlink(root + "/ns/pid")
        elif kind == "mount_namespace":
            value = os.readlink(root + "/ns/mnt")
        else:
            return ProcObservation("unavailable", reason="unsupported-proc-identity-kind")
        return ProcObservation("observed", str(value))
    except OSError as exc:
        state = "missing" if exc.errno in {errno.ENOENT, errno.ESRCH} else "unavailable"
        return ProcObservation(state, reason=type(exc).__name__)
    except (TypeError, ValueError, IndexError):
        return ProcObservation("unavailable", reason="invalid-proc-identity")


def _observer_context():
    self_start = _observe_proc_value("self", "start")
    pid_ns = _observe_proc_value("self", "pid_namespace")
    mount_ns = _observe_proc_value("self", "mount_namespace")
    if any(value.state != "observed" for value in (self_start, pid_ns, mount_ns)):
        failed = next(value for value in (self_start, pid_ns, mount_ns)
                      if value.state != "observed")
        return ProcObservation("unavailable", reason=failed.reason
                               or "observer-identity-unavailable")
    return ProcObservation("observed", (os.getpid(), self_start.value,
                                        pid_ns.value, mount_ns.value))


def _pid_namespace(pid):
    observation = _observe_proc_value(pid, "pid_namespace")
    return observation.value if observation.state == "observed" else None


def _proc_start(pid):
    observation = _observe_proc_value(pid, "start")
    return observation.value if observation.state == "observed" else None


def _fd_is_writer(pid, output_path):
    try:
        target = os.stat(output_path)
        for fd in os.listdir(f"/proc/{int(pid)}/fd"):
            try:
                opened = os.stat(f"/proc/{int(pid)}/fd/{fd}")
                if (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino):
                    continue
                with open(f"/proc/{int(pid)}/fdinfo/{fd}", encoding="ascii") as stream:
                    info = stream.read()
                flags_line = next((line for line in info.splitlines() if line.startswith("flags:")), None)
                flags = int(flags_line.split()[1], 8) if flags_line else 0
                if flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                    return True
            except (OSError, ValueError, IndexError):
                continue
    except (OSError, TypeError, ValueError):
        return False
    return False


def correlate_output_writers(job, candidates, *, namespace=None, root_pid=None):
    """Bind a job only to a writable FD on an exact, identity-checked descendant."""
    path = job.get("output_path") if isinstance(job, dict) else None
    if not isinstance(path, str) or not path:
        return {"process_state": "unknown", "process_reason": "no-exact-output-writer-locator"}
    if isinstance(namespace, ProcObservation):
        if namespace.state != "observed":
            return {"process_state": "unknown",
                    "process_reason": "parent-pid-namespace-unavailable:" +
                    str(namespace.reason)}
        namespace = namespace.value
    if not isinstance(namespace, str) or not namespace:
        return {"process_state": "unknown", "process_reason": "parent-pid-namespace-unavailable"}
    matched = []
    observation_gaps = []
    for candidate in candidates or ():
        if not isinstance(candidate, dict) or candidate.get("ownership_verified") is not True:
            continue
        ancestry = candidate.get("ancestry") if isinstance(candidate.get("ancestry"), list) else []
        if (not ancestry or not all(isinstance(item, dict) for item in ancestry)
                or ancestry[0].get("pid") != root_pid
                or ancestry[-1].get("pid") != candidate.get("pid")
                or candidate.get("root_pid") != root_pid):
            continue
        pid, start = candidate.get("pid"), candidate.get("proc_start")
        current_start = _observe_proc_value(pid, "start") if pid else ProcObservation(
            "unavailable", reason="candidate-pid-unavailable")
        current_namespace = _observe_proc_value(pid, "pid_namespace") if pid else ProcObservation(
            "unavailable", reason="candidate-pid-unavailable")
        if not pid or not start:
            continue
        if current_start.state != "observed":
            observation_gaps.append("candidate-start-" + current_start.state + ":" +
                                    str(current_start.reason))
            continue
        if current_start.value != str(start):
            observation_gaps.append("candidate-generation-mismatch")
            continue
        if current_namespace.state != "observed":
            observation_gaps.append("candidate-namespace-" + current_namespace.state + ":" +
                                    str(current_namespace.reason))
            continue
        ancestry_namespaces = [_observe_proc_value(item.get("pid"), "pid_namespace")
                               for item in ancestry
                               if isinstance(item, dict) and item.get("pid")]
        if any(value.state != "observed" for value in ancestry_namespaces):
            failed = next(value for value in ancestry_namespaces if value.state != "observed")
            observation_gaps.append("ancestry-namespace-" + failed.state + ":" +
                                    str(failed.reason))
            continue
        if current_namespace.value != namespace or any(
                value.value != namespace for value in ancestry_namespaces):
            observation_gaps.append("candidate-namespace-mismatch")
            continue
        if _fd_is_writer(pid, path):
            matched.append({"pid": pid, "start": str(start),
                            "namespace": current_namespace.value,
                            "observer_context": _observer_context().value,
                            "ancestry": candidate.get("ancestry")})
    if len(matched) == 1:
        identity = matched[0]
        _bounded(_VERIFIED_PROCESSES, _job_key(job), identity, _JSONL_LIMIT * 8)
        return {"process_state": "alive", "process_reason": "exact-descendant-writable-output-fd",
                "process_identity": identity}
    if len(matched) > 1:
        return {"process_state": "unknown", "process_reason": "multiple-output-writers"}
    previous = _VERIFIED_PROCESSES.get(_job_key(job))
    if previous:
        observer = _observer_context()
        saved_observer = previous.get("observer_context")
        if observer.state != "observed":
            return {"process_state": "unknown",
                    "process_reason": "observer-identity-unavailable:" + str(observer.reason),
                    "process_identity": previous}
        if not saved_observer or observer.value != tuple(saved_observer):
            return {"process_state": "unknown", "process_reason": "observer-context-changed",
                    "process_identity": previous}
        current_start = _observe_proc_value(previous.get("pid"), "start")
        if current_start.state == "missing":
            return {"process_state": "dead", "process_reason": "verified-process-disappeared",
                    "process_identity": previous}
        if current_start.state != "observed":
            return {"process_state": "unknown",
                    "process_reason": "verified-process-start-unavailable:" +
                    str(current_start.reason), "process_identity": previous}
        if current_start.value != str(previous.get("start")):
            return {"process_state": "dead",
                    "process_reason": "verified-process-generation-replaced",
                    "process_identity": previous}
        current_namespace = _observe_proc_value(previous.get("pid"), "pid_namespace")
        if current_namespace.state != "observed":
            return {"process_state": "unknown",
                    "process_reason": "verified-process-namespace-unavailable:" +
                    str(current_namespace.reason), "process_identity": previous}
        if current_namespace.value != previous.get("namespace"):
            return {"process_state": "unknown", "process_reason": "verified-process-namespace-changed",
                    "process_identity": previous}
        if observation_gaps:
            return {"process_state": "unknown",
                    "process_reason": "proc-identity-unavailable:" + observation_gaps[0],
                    "process_identity": previous}
    if observation_gaps:
        return {"process_state": "unknown",
                "process_reason": "proc-identity-unavailable:" + observation_gaps[0]}
    return {"process_state": "unknown", "process_reason": "exact-writer-not-observed"}


def observe_jobs(jobs, *, now=None, parent_pid=None, parent_start=None, attempt_id=None,
                 tree=None, kids=None):
    """Normalize jobs and add only targeted exact output-writer process evidence."""
    jobs = list(jobs or ())
    now = valid_epoch(now) if now is not None else None
    now = time.time() if now is None else now
    normalized = []
    for raw in jobs:
        item = normalize_job(raw, now=now)
        if item is not None:
            if item.get("lifecycle_state") != "completed":
                # A prior alive sample is not current evidence. Each collection
                # must repeat the exact writer correlation before keeping it.
                item["process_state"] = "unknown"
                item["process_reason"] = "process-not-observed-this-collection"
                if item.get("current_execution_source") == (
                        "process.exact-descendant-writable-output-fd"):
                    item.update(current_execution_state="unknown",
                                current_execution_reason="exact-writer-not-observed-this-collection")
            normalized.append(item)
    visible = display_shell_jobs(normalized, now=now)
    candidates = []
    needs_candidates = any(
        job.get("output_path") and job.get("lifecycle_state") != "completed"
        and job.get("process_state") != "dead" for job in visible)
    parent_namespace = ProcObservation("unavailable", reason="parent-identity-not-observed")
    if needs_candidates and parent_pid and parent_start and tree is not None and kids is not None:
        try:
            parent_namespace = _observe_proc_value(parent_pid, "pid_namespace")
            from . import procscan
            candidates = procscan.exec_children(
                parent_pid, tree=tree, kids=kids, min_age=0, expected_start=parent_start,
                attempt_id=attempt_id)
        except Exception as exc:
            candidates = []
            parent_namespace = ProcObservation("unavailable", reason=type(exc).__name__)
    for item in visible:
        if item.get("current_generation_valid") is False:
            item.update(process_state="unknown",
                        process_reason="source-generation-retired")
        elif (item.get("output_path") and item.get("lifecycle_state") != "completed"
                and item.get("process_state") != "dead"):
            process_evidence = correlate_output_writers(
                item, candidates, namespace=parent_namespace, root_pid=parent_pid)
            item.update(process_evidence)
            if (process_evidence.get("process_state") == "alive"
                    and process_evidence.get("process_reason") ==
                    "exact-descendant-writable-output-fd"):
                item.update(current_execution_state="running",
                            current_execution_source="process.exact-descendant-writable-output-fd",
                            current_execution_reason=None,
                            current_execution_at=now)
    return visible


def shell_job_strip_label(job):
    """Compact, safe display text shared by interactive and dispatch rows."""
    command = job.get("command_display") or job.get("command") or "command unavailable"
    status = ("exit %s" % job["exit_code"] if job.get("exit_code") is not None
              else "complete" if job.get("lifecycle_state") == "completed"
              else "alive" if job.get("process_state") == "alive"
              else "process dead" if job.get("process_state") == "dead"
              else "live ?")
    background = ("confirmed" if job.get("background_state") == "confirmed" else "unknown")
    parts = [command, "background " + background]
    started = valid_epoch(job.get("started_at"))
    if started is not None:
        try:
            parts.append("start " + time.strftime("%H:%M", time.localtime(started)))
        except (OverflowError, OSError, ValueError):
            parts.append("start ?")
    else:
        parts.append("start ?")
    elapsed = job.get("elapsed_s")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool):
        parts.append("%ds" % elapsed if elapsed < 60 else "%dm" % (elapsed // 60))
    parts.append(status)
    if job.get("quiet_suspected") and job.get("lifecycle_state") != "completed":
        parts.append("quiet ≥10m?")
    elif job.get("output_coverage") == "sampled":
        parts.append("output sampled")
    elif job.get("output_coverage") == "unavailable":
        parts.append("output ?")
    elif job.get("last_output_at") is not None:
        parts.append("output sampled" if job.get("output_coverage") == "sampled"
                     else "output observed")
    return " · ".join(parts)
