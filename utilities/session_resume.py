"""Native resume observations delivered by the existing ordinary notice courier.

No new queue or session authority: the source is a recorded resume start, the
recipient is its exact native identity, and the existing carrier owns I/O.
"""
from __future__ import annotations
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import sys
import re
import time

PREFIX = "native-resume-v1:"
TAIL_BYTES = 512 * 1024
NEEDS_WAKE = frozenset({"interrupted-turn", "queued-task-result", "open-work"})
ATTENTION = NEEDS_WAKE | {"open-work-unobserved"}
FINISHED = frozenset({"completed", "human-wait", "activity-after-resume"})


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _tail(path, sid):
    if not path or sid not in Path(path).name:
        return None
    try:
        with Path(path).open("rb") as handle:
            size = handle.seek(0, 2)
            offset = max(0, size - TAIL_BYTES)
            handle.seek(offset)
            raw = handle.read(TAIL_BYTES + 1)
        lines = raw.splitlines()
        if offset and lines:
            lines.pop(0)
        rows = []
        for line in lines:
            row = json.loads(line)
            if not isinstance(row, dict):
                return None
            if (row.get("sessionId", sid) != sid or row.get("isSidechain") is True
                    or row.get("isMeta") is True or row.get("isCompactSummary") is True):
                continue
            rows.append(row)
        return rows
    except (OSError, ValueError, UnicodeError):
        return None



def native_state(harness, sid, transcript, *, resumed_at):
    """Cache only a small enum, bounded to 32 exact native source generations."""
    source = str(transcript or "")
    if harness == "opencode":
        import opencode_runtime
        source = str(opencode_runtime.opencode_db_path())
    try:
        info = Path(source).stat()
        generation = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid)
        if harness == "opencode":
            wal = Path(source + "-wal")
            if wal.exists():
                info = wal.stat()
                generation += (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid)
    except OSError:
        return _native_state(harness, sid, transcript, resumed_at=resumed_at)
    return _cached_native_state(harness, sid, source, resumed_at, generation)


@lru_cache(maxsize=32)
def _cached_native_state(harness, sid, source, resumed_at, generation):
    return _native_state(harness, sid, source, resumed_at=resumed_at)

def _native_state(harness, sid, transcript, *, resumed_at):
    try:
        return _read_native_state(harness, sid, transcript, resumed_at=resumed_at)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return "unknown"


def _read_native_state(harness, sid, transcript, *, resumed_at):
    """Structural native facts only; ordinary prose never means 'unfinished'."""
    if harness == "opencode":
        import opencode_runtime
        import human_wait_state
        observed = opencode_runtime.read_session_observation(sid, shell_after_seq=0)
        if observed.get("state") != "observed":
            return "unknown"
        messages = [m for m in observed.get("messages", ()) if m.get("type") in {"user", "assistant"}]
        if not messages:
            return "unknown"
        latest = messages[-1]
        if latest.get("type") == "assistant":
            if (human_wait_state.marked_question(latest.get("text", ""))
                    or latest.get("tools") and any(t.get("name") in {"question", "request_user_input"}
                                                 for t in latest["tools"])):
                return "human-wait"
            stamp = latest.get("started") or latest.get("created")
            if isinstance(stamp, (int, float)) and stamp / 1000 >= resumed_at:
                return "activity-after-resume"
        completed = latest.get("completed")
        for task in observed.get("shell_tasks", ()):
            if task.get("native_record_kind") != "session-message-shell":
                continue
            finished = task.get("completed_at_ms")
            if (task.get("native_status") in {"stopped", "failed", "error", "completed", "exited"}
                    and isinstance(finished, (int, float)) and isinstance(completed, (int, float))
                    and finished > completed):
                return "queued-task-result"
        if latest.get("type") == "assistant":
            if latest.get("has_error"):
                return "interrupted-turn"
            return "completed" if completed else "interrupted-turn"
        return "interrupted-turn" if latest.get("type") == "user" else "unknown"
    rows = _tail(transcript, sid)
    if rows is None:
        return "unknown"
    if harness == "codex":
        import human_wait_state
        tools = str(Path(__file__).resolve().parents[1] / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        from fleet.collectors.shell_jobs import _codex_payload
        state, last_end = "unknown", -1
        commands, questions = {}, set()
        for index, row in enumerate(rows):
            payload = _codex_payload(row) or {}
            kind = payload.get("type")
            if kind in {"item.started", "item.completed"}:
                item = payload.get("item") or {}
                owner = row.get("thread_id") or row.get("session_id") or payload.get("thread_id") or payload.get("session_id")
                if owner and owner != sid:
                    continue
                if item.get("type") == "command_execution" and isinstance(item.get("id"), str):
                    entry = commands.setdefault(item["id"], {})
                    entry["start" if kind == "item.started" else "end"] = index
            if row.get("type") == "event_msg":
                if kind in {"task_started", "turn_started", "turn_aborted", "task_aborted"}:
                    state = "interrupted-turn"
                elif kind == "user_message":
                    state = "interrupted-turn"
                elif kind in {"task_complete", "turn_completed"}:
                    final = payload.get("last_agent_message")
                    if isinstance(final, str):
                        final = final.strip()
                    if final:
                        state = "human-wait" if human_wait_state.marked_question(final) else "completed"
                    elif state != "human-wait":
                        state = "completed"
                    last_end = index
            elif row.get("type") == "response_item":
                if kind in {"function_call", "custom_tool_call"} and str(payload.get("name", "")).split(".")[-1] == "request_user_input":
                    questions.add(payload.get("call_id"))
                elif kind in {"function_call_output", "custom_tool_call_output"}:
                    questions.discard(payload.get("call_id"))
                elif kind == "message" and payload.get("role") == "user":
                    state = "interrupted-turn"
                elif kind == "message" and payload.get("role") == "assistant" and payload.get("channel") == "final":
                    text = "\n".join(p.get("text", "") for p in payload.get("content", ()) if isinstance(p, dict))
                    if text.strip():
                        state = "human-wait" if human_wait_state.marked_question(text) else "completed"
                    elif state != "human-wait":
                        state = "completed"
                    last_end = index
            if (row.get("type") == "response_item" and payload.get("role") == "assistant"
                    and (_epoch(row.get("timestamp")) or 0) >= resumed_at):
                return "activity-after-resume"
        if questions:
            return "human-wait"
        if state == "completed":
            background = [item for item in commands.values() if item.get("start", last_end + 1) < last_end]
            if any(item.get("end", -1) > last_end for item in background):
                return "queued-task-result"
            if any("end" not in item for item in background):
                return "waiting-background"
        return state
    if harness != "claude":
        return "unknown"
    last_assistant = -1
    background_calls, task_ids, finished_tasks, questions = set(), {}, set(), set()
    for index, row in enumerate(rows):
        message = row.get("message") or {}
        if not isinstance(message, dict):
            return "unknown"
        blocks = message.get("content")
        if row.get("type") == "user" and isinstance(blocks, list):
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            for result in results:
                questions.discard(result.get("tool_use_id"))
                native = row.get("toolUseResult") or {}
                if (len(results) == 1 and isinstance(native, dict)
                        and isinstance(native.get("backgroundTaskId"), str)):
                    task_ids[result.get("tool_use_id")] = native["backgroundTaskId"]
        if row.get("type") == "user" and isinstance(blocks, str) and (row.get("origin") or {}).get("kind") == "task-notification":
            match = re.search(r"<task-id>([A-Za-z0-9_-]+)</task-id>", blocks)
            if match and re.search(r"<status>(?:stopped|killed|failed|completed)</status>", blocks):
                finished_tasks.add(match[1])
        if row.get("type") != "assistant":
            continue
        last_assistant = index
        if (_epoch(row.get("timestamp")) or 0) >= resumed_at:
            return "activity-after-resume"
        blocks = message.get("content")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") == "AskUserQuestion":
                    questions.add(block.get("id"))
                if (block.get("name") == "Bash" and isinstance(block.get("input"), dict)
                        and block["input"].get("run_in_background") is True):
                    background_calls.add(block.get("id"))
    if questions:
        return "human-wait"
    # Native-only queued task envelopes are not model turns. The minimal origin
    # below is what the current Claude runtime actually writes after resume;
    # a user's pasted <task-notification> has neither native provenance field.
    for row in rows[last_assistant + 1:]:
        origin = row.get("origin") or {}
        message = row.get("message") or {}
        if (row.get("type") != "user" or not isinstance(origin, dict)
                or origin.get("kind") != "task-notification" or row.get("promptSource") != "system"
                or row.get("queueTranscriptOnly") is not True or not isinstance(message, dict)):
            continue
        body = message.get("content")
        if (isinstance(body, str) and re.search(r"<task-id>[A-Za-z0-9_-]+</task-id>", body)
                and re.search(r"<status>(?:stopped|killed|failed|completed)</status>", body)):
            return "queued-task-result"
    for row in rows[last_assistant + 1:]:
        if (row.get("type") == "user" and isinstance((row.get("message") or {}).get("content"), str)
                and row.get("promptSource") != "system"):
            return "interrupted-turn"
    if last_assistant < 0:
        return "interrupted-turn" if any(r.get("type") == "user" for r in rows) else "unknown"
    latest = rows[last_assistant].get("message") or {}
    import human_wait_state
    text = "\n".join(b.get("text", "") for b in latest.get("content", ())
                     if isinstance(b, dict) and b.get("type") == "text")
    if human_wait_state.marked_question(text):
        return "human-wait"
    if latest.get("stop_reason") != "end_turn":
        return "interrupted-turn"
    background = any(task_ids.get(call) not in finished_tasks for call in background_calls)
    return "waiting-background" if background else "completed"


def _seat(harness, sid):
    import session_tidy as tidy
    return tidy.resolve_seat(harness, env={}, sid=sid)



def _ledger(harness, sid):
    """One fresh observation for an effect, independent of a loop's read cache."""
    import session_tidy as tidy
    seat = _seat(harness, sid)
    raw = tidy.read_bytes(tidy._native_ledger_path(seat))
    return seat, raw or b"", tidy._read_ledger_lines(seat, raw=raw or b"")

def _resume(harness, sid):
    import session_tidy as tidy
    _target, _raw, rows = _ledger(harness, sid)
    candidate, launch = None, None
    for row in reversed(rows):
        if (row.get("harness"), row.get("sid")) != (harness, sid):
            continue
        if row.get("event") == "prompt" and candidate is not None and launch is None:
            break
        if row.get("event") == "start":
            source = row.get("source")
            event = row
        elif row.get("event") == "summary":
            source = row.get("latest_start_source", row.get("start_source"))
            event = {**row, "source": source,
                     "ts": row.get("latest_start_at", row.get("start_at", 0)),
                     "session_seq": row.get("start_seq", row.get("latest_start_seq"))}
        else:
            continue
        if source in {"compact", "select"}:
            continue
        if source != "resume":
            if candidate is None:
                return None
            break
        if type(event.get("session_seq")) is not int:
            continue
        if candidate is not None:
            previous_launch = event.get("launch_process")
            if launch and previous_launch:
                if previous_launch != launch:
                    break
            elif event.get("prompt_seq") != candidate.get("prompt_seq"):
                break
        candidate = event
        launch = launch or event.get("launch_process")
    if candidate is None:
        note = next((r for r in reversed(rows) if r.get("event") == "resume-notice"
                     and (r.get("harness"), r.get("sid")) == (harness, sid)), None)
        if note:
            candidate = {**note, "event": "start", "source": "resume"}
    return candidate


def _human_wait(harness, sid):
    import human_wait_state
    path = human_wait_state._cache_path()
    try:
        info = path.stat()
        generation = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                      info.st_ctime_ns, info.st_mode, info.st_uid)
    except OSError:
        return False
    keys, observed = _wait_index(str(path), generation)
    return bool(observed and 0 <= time.time() - observed <= human_wait_state._DISPLAY_TTL
                and (harness, sid) in keys)


@lru_cache(maxsize=1)
def _wait_index(path, generation):
    # Retain identities only, never the question text or the full cache object.
    import human_wait_state
    view = human_wait_state.cached_view(request=False)
    return (frozenset(key for key, ids in view.get("by_session", {}).items() if ids),
            view.get("observed_at")) if view else (frozenset(), None)


def open_work(harness, sid):
    """Fleet's own route/outcome projection, without a second process census."""
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet import route_chain, route
    chain = route_chain.assemble(route_chain.read_tail(harness, sid),
        load_record=route.load, load_outcome=route.load_outcome)
    if any(node.get("state") in {"open", "finish-pending"} for node in chain.get("nodes", ())):
        return True
    import dispatch_recipient_inbox as inbox
    root = inbox.dispatch_inbox_root()
    pending = inbox.read_inbox(root, harness, sid)
    return bool(pending and inbox.is_current(pending, root)
                and any(item.get("attempts") for item in pending.get("candidates", ())))

def _prompt_after_resume(harness, sid, resume):
    """The same quick frontier read before and after slow native observation."""
    import session_tidy as tidy
    seat = _seat(harness, sid)
    frontier = tidy.prompt_state(seat)
    return bool(isinstance(frontier, dict) and (frontier.get("harness"), frontier.get("sid")) == (harness, sid)
            and ((isinstance(resume.get("prompt_seq"), int) and isinstance(frontier.get("seq"), int)
                  and frontier["seq"] > resume["prompt_seq"])
                 or isinstance(frontier.get("at"), (int, float)) and frontier["at"] > resume.get("ts", 0)))


def observation(harness, sid, resume, *, registered_work=None):
    import session_tidy as tidy
    seat = _seat(harness, sid)
    if _prompt_after_resume(harness, sid, resume):
        return "activity-after-resume"
    _observed, raw, _rows = _ledger(harness, sid)
    real = tidy.real_prompt_frontier(seat, harness, sid, raw=raw)
    if real and real.get("ts", 0) > resume.get("ts", 0):
        return "activity-after-resume"
    if _human_wait(harness, sid):
        return "human-wait"
    source = resume.get("transcript") or ""
    if not source and harness != "opencode":
        import tidy_transcripts
        source = tidy_transcripts.locate_transcript(harness, sid) or ""
    state = native_state(harness, sid, source, resumed_at=resume.get("ts", 0))
    if state in {"human-wait", "activity-after-resume", "waiting-background"} or state in NEEDS_WAKE:
        return state
    if registered_work is None:
        registered_work = open_work(harness, sid)
    return ("open-work-unobserved" if state == "unknown" else "open-work") if registered_work else state


def register(harness, sid, *, source, env=None, jobs=None):
    if source != "resume":
        return None
    import session_tidy as tidy
    if tidy.is_worker(env) or harness not in tidy.HARNESSES or not isinstance(sid, str) or not sid:
        return None
    resume = _resume(harness, sid)
    if resume is None:
        return None
    # The reopened session takes over its own open routes whatever the native state:
    # before the notice filter, once per resume generation (its own ledger dedupe).
    try:
        import session_route_resume
        session_route_resume.request(harness, sid, resume=resume, env=env)
    except Exception:
        pass
    state = observation(harness, sid, resume)
    if state not in ATTENTION and state != "waiting-background":
        return None
    import session_notice
    import dispatch_pending_delivery as pending
    seat = _seat(harness, sid)
    # Existing logical-session locking also serializes recovery from another
    # registry root. A second controller cannot mint a second native wake.
    with tidy.session_state_lock(seat):
        resume = _resume(harness, sid)
        if resume is None:
            return None
        state = observation(harness, sid, resume)
        if state not in ATTENTION and state != "waiting-background":
            return None
        _observed, _raw, rows = _ledger(harness, sid)
        prior = next((r for r in reversed(rows) if r.get("event") == "resume-notice"
                      and r.get("session_seq") == resume.get("session_seq")), None)
        if prior:
            canonical_jobs = Path(prior["jobs"])
            record = pending.read(canonical_jobs.parent, sid, prior["delivery_id"])
            if record and record.get("state") in pending.OPEN_STATES:
                session_notice.retain_pending(canonical_jobs, {**record, "_storage_key": sid})
            return record
        key = PREFIX + json.dumps([harness, sid, resume["session_seq"]], separators=(",", ":"))
        translate = tidy._ui_translator()
        record = session_notice.notify(harness, sid, key=key, subject=translate("resume.notice.subject"),
            text=translate("resume.notice.text"), required_action="continue-interrupted-work",
            jobs=session_notice._registry(jobs))
        if record:
            canonical_jobs, _lineage = session_notice.pending_lineage(record, harness, sid)
            tidy._append_ledger_entry(seat, {"event": "resume-notice", "harness": harness, "sid": sid,
                "ts": resume["ts"], "session_seq": resume["session_seq"],
                "prompt_seq": resume.get("prompt_seq"), "launch_process": resume.get("launch_process"),
                "transcript": resume.get("transcript", ""),
                "jobs": str(canonical_jobs), "delivery_id": record["delivery_id"]})
        return record


def is_resume(record):
    key = (record.get("receipt") or {}).get("key")
    return isinstance(key, str) and key.startswith(PREFIX)


def current(record, *, for_submission=False):
    """False is obsolete; unknown evidence retains the existing lease.

    The existing first-write predicate requests a fresh source even for a
    prepared ambiguous notice; accepted-input receipts remain settleable.
    """
    key = (record.get("receipt") or {}).get("key") or ""
    try:
        harness, sid, sequence = json.loads(key[len(PREFIX):])
        from harness_capabilities import parent_completion
        import dispatch_pending_delivery as pending
        if (type(sequence) is not int or not isinstance(sid, str)
                or record.get("recipient_kind") != parent_completion(harness).get("carrier")
                or record.get("recipient_digest") != pending.recipient_digest(sid)):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    resume = _resume(harness, sid)
    # Keep the exact receipt for a submission that already crossed I/O.
    if not for_submission and record.get("state") in {"sent-ambiguous", "acked"}:
        return True
    if not resume:
        _target, _raw, rows = _ledger(harness, sid)
        for row in reversed(rows):
            if (row.get("harness"), row.get("sid")) != (harness, sid):
                continue
            source = row.get("source") if row.get("event") == "start" else row.get("latest_start_source")
            if source in {"startup", "clear", "fork"}:
                return False
            if source:
                break
        raise ValueError("resume-start-unavailable")
    if resume.get("session_seq") != sequence:
        if resume.get("session_seq", 0) > sequence:
            return False
        raise ValueError("resume-start-generation-unavailable")
    state = observation(harness, sid, resume)
    if state in FINISHED:
        return False
    if state not in NEEDS_WAKE:
        raise ValueError("resume-observation-" + state)
    # Match idle tidy's final atomic-file recheck: native observation above
    # can block, so it must not be the last word on a person's input frontier.
    latest = _resume(harness, sid)
    if latest is None or latest.get("session_seq", 0) < sequence:
        raise ValueError("resume-start-generation-unavailable")
    return latest.get("session_seq") == sequence and not _prompt_after_resume(harness, sid, latest)


def status_for_session(harness, sid):
    """Private Fleet display; reads the existing ledger and exact pending record."""
    import session_tidy as tidy
    if harness not in tidy.HARNESSES or not isinstance(sid, str) or not sid:
        return None
    resume = _resume(harness, sid)
    if not resume:
        return None
    notice = next((r for r in reversed(tidy._read_ledger_lines(_seat(harness, sid)))
                   if r.get("event") == "resume-notice" and r.get("session_seq") == resume.get("session_seq")), None)
    if not notice:
        return "stopped" if observation(harness, sid, resume) in ATTENTION else None
    import dispatch_pending_delivery as pending
    record = pending.read(Path(notice["jobs"]).parent, sid, notice["delivery_id"])
    if not record:
        return "stopped" if observation(harness, sid, resume) in ATTENTION else None
    if record.get("state") not in pending.OPEN_STATES:
        return None
    state = observation(harness, sid, resume)
    return "stopped" if state not in FINISHED and state != "waiting-background" else None


def record_opencode_resume(payload):
    """One actual selected TUI child report; a server-global event is insufficient."""
    import os
    import session_tidy as tidy
    if not isinstance(payload, dict) or payload.get("harness") != "opencode":
        return None
    pid, start, sid = payload.get("pid"), payload.get("start"), payload.get("sid")
    if type(pid) is not int or pid <= 1 or pid != os.getppid() or not isinstance(sid, str):
        return None
    directory = Path("/proc") / str(pid)
    if directory.stat().st_uid != os.getuid():
        return None
    actual = (directory / "stat").read_text().rsplit(") ", 1)[1].split()[19]
    if actual != start:
        return None
    argv = (directory / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
    if not argv or Path(argv[0]).name != "opencode":
        return None
    if any(arg == "--fork" or arg == "--fork=true" for arg in argv):
        return None
    requested = any((arg in {"--session", "-s"} and i + 1 < len(argv) and argv[i + 1] == sid)
        or arg in {"--session=" + sid, "-s=" + sid, "--continue", "--last", "-c"}
        for i, arg in enumerate(argv))
    if not requested:
        return None
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from fleet.collectors import opencode
    selected_sid, selected_source = opencode.session_of_process(pid)
    if selected_sid != sid or selected_source != "opencode-tui-selection":
        return None
    cwd = os.readlink(directory / "cwd")
    if (directory / "stat").read_text().rsplit(") ", 1)[1].split()[19] != start:
        return None
    seat = tidy.resolve_seat("opencode", cwd, sid=sid)
    launch = {"pid": pid, "start": start}
    with tidy.session_state_lock(seat):
        prior = any(row.get("event") == "start" and row.get("source") == "resume"
                    and row.get("launch_process") == launch
                    for row in _ledger("opencode", sid)[2])
        if not prior:
            tidy.record_event(seat, "opencode", sid, "start", source="resume", cwd=cwd,
                              resume_argv=argv, launch_process=launch)
    return register("opencode", sid, source="resume")


if __name__ == "__main__":
    try:
        record_opencode_resume(json.loads(sys.stdin.read(8193)))
    except (OSError, ValueError, TypeError, KeyError):
        pass
