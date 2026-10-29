"""Codex CLI enrichment — passive, read-only (01_tap_mechanics.md §2).

Codex telemetry is recovered from the rollout jsonl, while the native task title is
read from the read-only ``threads.title`` state DB with ``session_index.jsonl``
(``id`` -> ``thread_name``) as a compatibility fallback:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<sid>.jsonl
  · line 1  = session_meta  → payload.cwd (pid↔session match key)
  · last "token_count" event → payload.info.{last_token_usage, model_context_window}
      + payload.rate_limits.{primary,secondary}.used_percent and optional limit_window_seconds
Model/effort come from the matched rollout's latest ``turn_context``. The active
runtime home's top-level ``config.toml`` values are only a compatibility fallback
for rollouts that predate that record.

context% mirrors Codex's own formula: tokens_in_context_window() is the last
request's total_tokens, and
  used% = 100 - percent_of_context_window_remaining
        = round(100 * max(0, last.total_tokens - BASELINE) / (window - BASELINE)),
BASELINE_TOKENS = 12000 ("prompts, tools and space to call compact") subtracted from BOTH sides
so a fresh session reads 0% used exactly like the codex TUI's "100% context left".
(History: v1 used input_tokens/window — plausible but ~2x codex's number; the 2026-07-01
empirical note that rejected (input+cached)/window still holds, superseded by the source
formula. Cumulative total_token_usage is NOT context occupancy — it double-counts inputs.)

A per-tick cache (cwd → newest rollout path) is built from cheap line-1 reads of all rollouts
(≈200 files); the expensive last-token-count parse touches only a 64 KB tail of the matched file.
"""
import contextlib
import contextvars
import glob
import json
import os
import re
import sqlite3
import stat
import sys
import time
import socket
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from fleet import session_registry
from fleet.model import ContextEvidence, SESSION_WORK_SEC, SubAgent
from fleet.token_budget import parse_codex_token_count
from . import shell_jobs

# rollout filename tail: rollout-<ISO-ts>-<uuid>.jsonl
_SID_RE = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")

_INDEX = {"ts": 0.0, "map": None, "home": None}   # cwd → rollout paths (newest first)
_INDEX_TTL = 1.5
_DAEMON_ORIGINS = {}  # live control-socket identity -> complete loaded thread observations
_DAEMON_RPC_SECONDS = 6.0
_DAEMON_RPC_TIMEOUT = 1.5
_DAEMON_MAX_PAGES = 16
_DAEMON_PAGE_SIZE = 100
_DAEMON_MAX_THREADS = 512
_PROC_PATHS = {}                                  # pid -> rollout reserved at collect tick start
_CFG = {"ts": 0.0, "model": None, "effort": None}
_TITLE_INDEX = {"stamp": None, "map": {}, "runtime_map": {}}   # native state stamps -> sid: title
                                                                # (runtime_map = JSONL thread_name only, F-99a ①)
_SUBAGENT_INDEX = {}                  # runtime home -> (read time, stamp, map, available, scope)
_LIFECYCLE_CACHE_MAX = 512
_LIFECYCLE_CACHE = OrderedDict()
_LIFECYCLE_CACHE_EVICTIONS = 0
_EXACT_LIFECYCLE_CACHE = OrderedDict()   # path -> (full stat stamp, exact-scan lifecycle)
_ROLLOUT_CWD_CACHE_MAX = 8192
_ROLLOUT_CWD_CACHE = {}                  # path -> (stat stamp, session_meta cwd)
_LIFECYCLE_BOUNDARY_BYTES = 256
_TURN_CONTEXT_CACHE_MAX = 512
_TURN_CONTEXT_CACHE = OrderedDict()
_TURN_CONTEXT_CACHE_EVICTIONS = 0
_SUMMARY_BOUNDARY_SCAN = 1 << 20
_TCP_UNKNOWN = object()
_SYNTHETIC_NATIVE_TITLE_RE = re.compile(
    r"^(?:agents\.md\s*[-—:]\s*)?codex adapter bootstrap$", re.IGNORECASE
)


class DaemonIdentityObservation:
    """Short-lived socket-owner snapshots shared by one multi-PID observation."""

    __slots__ = ("initial_scan", "recheck_scan")

    def __init__(self):
        self.initial_scan = {}
        self.recheck_scan = {}

    def invalidate(self):
        """Discard both snapshots after any identity evidence changes."""
        self.initial_scan.clear()
        self.recheck_scan.clear()


_DAEMON_IDENTITY_SCOPE = contextvars.ContextVar("codex_daemon_identity_scope", default=None)


@contextlib.contextmanager
def daemon_identity_read_scope():
    """Share live listener evidence only within one collection pass."""
    active = _DAEMON_IDENTITY_SCOPE.get()
    token = _DAEMON_IDENTITY_SCOPE.set(
        active if active is not None else DaemonIdentityObservation())
    try:
        yield _DAEMON_IDENTITY_SCOPE.get()
    finally:
        _DAEMON_IDENTITY_SCOPE.reset(token)


@dataclass
class _CodexTick:
    """Collection-local Codex evidence frozen independently for each runtime home."""

    default_home: str
    proc_paths: dict
    subagents_by_home: dict
    # Exact parent ids the tick's subagent maps were scoped to (prepare_tick);
    # None means unscoped (legacy full build). enrich widens to full when a
    # session id outside the scope needs a lookup.
    subagents_parent_ids: Optional[frozenset] = None


def _home():
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _state_db(home):
    """Return the newest versioned Codex state DB without assuming one schema version."""
    candidates = []
    try:
        names = os.listdir(home)
    except OSError:
        return None
    for name in names:
        match = re.fullmatch(r"state_(\d+)\.sqlite", name)
        if match:
            candidates.append((int(match.group(1)), os.path.join(home, name)))
    return max(candidates, default=(None, None))[1]


def _file_stamp(path):
    if not path:
        return None
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _thread_titles(home):
    """Tolerant Codex native-title index; state DB wins over the JSONL fallback."""
    index_path = os.path.join(home, "session_index.jsonl")
    db_path = _state_db(home)
    stamp = (
        _file_stamp(index_path),
        db_path,
        _file_stamp(db_path),
        _file_stamp(db_path + "-wal") if db_path else None,
    )
    if _TITLE_INDEX["stamp"] == stamp:
        return _TITLE_INDEX["map"]
    mapped = {}
    runtime_mapped = {}
    try:
        with open(index_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                sid = row.get("id") if isinstance(row, dict) else None
                name = row.get("thread_name") if isinstance(row, dict) else None
                if isinstance(sid, str) and isinstance(name, str) and name.strip():
                    mapped[sid] = name.strip()
                    # F-99a ① — the JSONL thread_name, kept separate from the DB `title`
                    # overwrite below so a runtime-set name is never collapsed into it.
                    runtime_mapped[sid] = name.strip()
    except OSError:
        pass
    if db_path:
        connection = None
        try:
            connection = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
            for sid, title in connection.execute(
                "SELECT id, title FROM threads WHERE title IS NOT NULL AND title != ''"
            ):
                if isinstance(sid, str) and isinstance(title, str) and title.strip():
                    mapped[sid] = title.strip()
        except (OSError, sqlite3.Error):
            pass
        finally:
            if connection is not None:
                connection.close()
    _TITLE_INDEX.update(stamp=stamp, map=mapped, runtime_map=runtime_mapped)
    return mapped


def _thread_runtime_names(home):
    """F-99a ① — the JSONL ``thread_name`` map, kept distinct from ``_thread_titles``'s
    DB-title-wins merge. Piggybacks on ``_thread_titles``'s own stamp/cache."""
    _thread_titles(home)
    return _TITLE_INDEX["runtime_map"]


def _displayable_title(value):
    """Drop the known title Codex derives from the synthetic AGENTS envelope."""
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    return None if _SYNTHETIC_NATIVE_TITLE_RE.fullmatch(title) else title or None


def _agent_type(agent_role, agent_path=None):
    """Runtime role, falling back to the final native agent-path component."""
    if isinstance(agent_role, str) and agent_role.strip():
        return agent_role.strip()
    if isinstance(agent_path, str) and agent_path.strip():
        return os.path.basename(agent_path.strip().rstrip(os.sep)) or None
    return None


def _rollout_home(path):
    """Runtime home owning a rollout; dispatched Codex sessions may use another home."""
    marker = os.sep + "sessions" + os.sep
    absolute = os.path.abspath(path)
    prefix, found, _tail = absolute.partition(marker)
    return prefix if found and prefix else None


@dataclass(frozen=True)
class _LifecycleCursor:
    """Validated JSONL state at one stable byte boundary of one file identity."""

    dev: int
    ino: int
    offset: int
    mtime_ns: int
    ctime_ns: int
    carry: bytes
    lifecycle: object
    boundary: bytes
    pending_calls: object = None


def _advance_task_lifecycle(lifecycle, raw_line):
    """Advance one complete JSONL record; unrelated or malformed JSON is silent."""
    try:
        row = json.loads(raw_line)
    except Exception:
        return lifecycle
    return _advance_task_lifecycle_row(lifecycle, row)


def _advance_task_lifecycle_row(lifecycle, row):
    """Advance one already-decoded JSONL record."""
    if not isinstance(row, dict) or row.get("type") != "event_msg":
        return lifecycle
    payload = row.get("payload")
    event_type = payload.get("type") if isinstance(payload, dict) else None
    if event_type not in {"task_started", "task_complete", "turn_aborted"}:
        return lifecycle
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    if event_type == "task_started":
        return (event_type, turn_id)
    if lifecycle == ("task_started", turn_id):
        return (event_type, turn_id)
    return None


@dataclass(frozen=True)
class _PendingInput:
    since: object
    turn_id: object
    # None is a blocking call; async calls retain only unanswered question indices.
    questions: object = None
    ended: bool = False


def _question_replies(payload):
    """Read exact native reply envelopes, never search conversational prose."""
    if payload.get("type") != "message" or payload.get("role") != "user":
        return
    content = payload.get("content")
    if not isinstance(content, list):
        return
    opening = "<send_user_message_question_reply>"
    closing = "</send_user_message_question_reply>"
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "input_text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not (text.startswith(opening) and text.endswith(closing)):
            continue
        try:
            replies = json.loads(text[len(opening):-len(closing)])
        except (ValueError, TypeError):
            continue
        if not isinstance(replies, list):
            continue
        for reply in replies:
            if not isinstance(reply, dict) or not isinstance(reply.get("answer"), str):
                continue
            try:
                identity = json.loads(reply.get("questionItemId", ""))
            except (ValueError, TypeError):
                continue
            if (isinstance(identity, list) and len(identity) == 3
                    and identity[0] == "request_user_input_async"
                    and isinstance(identity[1], str) and identity[1]
                    and type(identity[2]) is int and identity[2] >= 0):
                yield identity[1], identity[2]


def _composer_prompt(item):
    """True for a prompt the person submitted in the composer, judged by structure only.

    A composer submit carries a UUID `client_id`; Hearting completion deliveries are
    `delivery-<digest>`, and an exact native question reply keeps its per-index path.
    The text is only tested for that envelope prefix and never retained.
    """
    if not isinstance(item, dict) or item.get("type") != "UserMessage":
        return False
    client_id = item.get("client_id")
    if not isinstance(client_id, str) or not client_id or client_id.startswith("delivery-"):
        return False
    content = item.get("content")
    for part in content if isinstance(content, list) else ():
        text = part.get("text") if isinstance(part, dict) else None
        if isinstance(text, str) and text.lstrip().startswith("<send_user_message_question_reply>"):
            return False
    return True


def _advance_pending_input(open_calls, lifecycle, row):
    """Track only call/turn identity/time/indices; never retain question content."""
    if not isinstance(row, dict):
        return
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return
    if row.get("type") == "event_msg":
        kind = payload.get("type")
        if kind == "task_started":
            for call_id, request in list(open_calls.items()):
                if request.questions is None or request.ended:
                    open_calls.pop(call_id)
        elif kind in {"task_complete", "turn_aborted"}:
            for call_id, request in list(open_calls.items()):
                if (kind == "task_complete" and request.questions is not None
                        and request.turn_id is not None
                        and request.turn_id == payload.get("turn_id")):
                    open_calls[call_id] = _PendingInput(
                        request.since, request.turn_id, request.questions, ended=True)
                    continue
                if request.turn_id is None or request.turn_id == payload.get("turn_id"):
                    open_calls.pop(call_id)
        elif kind == "item_completed" and _composer_prompt(payload.get("item")):
            # The TUI drops open async questions on any composer submit; the person's
            # typed reply is the answer (roles/response-policy.md). Deliveries never clear.
            for call_id, request in list(open_calls.items()):
                if request.questions is not None:
                    open_calls.pop(call_id)
        return
    if row.get("type") != "response_item":
        return
    for answered_call, index in _question_replies(payload):
        request = open_calls.get(answered_call)
        if request is None or request.questions is None or index not in request.questions:
            continue
        remaining = request.questions - {index}
        if remaining:
            open_calls[answered_call] = _PendingInput(
                request.since, request.turn_id, remaining, request.ended)
        else:
            open_calls.pop(answered_call)
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    kind = payload.get("type")
    name = payload.get("name")
    if kind == "function_call" and isinstance(name, str) and name in {
        "request_user_input", "request_user_input_async",
        "functions.request_user_input", "functions.request_user_input_async",
    }:
        questions = None
        if name in {"request_user_input_async", "functions.request_user_input_async"}:
            try:
                arguments = payload.get("arguments")
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                values = arguments.get("questions") if isinstance(arguments, dict) else None
                if not isinstance(values, list) or not values or not all(
                        isinstance(value, dict) for value in values):
                    return
                questions = frozenset(range(len(values)))
            except (ValueError, TypeError):
                return
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            turn_id = lifecycle[1] if lifecycle and lifecycle[0] == "task_started" else None
        # Reinsertion preserves most-recent-call order when an id is reused.
        open_calls.pop(call_id, None)
        open_calls[call_id] = _PendingInput(
            _event_timestamp(row.get("timestamp"), None),
            turn_id, questions,
        )
    elif kind == "function_call_output":
        request = open_calls.get(call_id)
        if request is not None and request.questions is not None:
            try:
                result = payload.get("output")
                if isinstance(result, str):
                    result = json.loads(result)
                if isinstance(result, dict) and result.get("accepted") is True:
                    return
            except (ValueError, TypeError):
                pass
        open_calls.pop(call_id, None)


def _consume_lifecycle_bytes(lifecycle, carry, data, pending_calls=None):
    """Consume complete newline-delimited rows, retaining an incomplete suffix."""
    parts = (carry + data).split(b"\n")
    carry = parts.pop()
    for raw_line in parts:
        # One decode serves both observers; a trailing CR is JSON whitespace.
        try:
            row = json.loads(raw_line)
        except Exception:
            row = None
        if pending_calls is not None:
            _advance_pending_input(pending_calls, lifecycle, row)
        lifecycle = _advance_task_lifecycle_row(lifecycle, row)
    return lifecycle, carry


def _read_lifecycle_range(handle, start, end, chunk, lifecycle=None, carry=b"",
                          pending_calls=None):
    if not isinstance(chunk, int) or chunk <= 0:
        raise ValueError("chunk must be a positive integer")
    handle.seek(start)
    remaining = end - start
    while remaining:
        data = handle.read(min(chunk, remaining))
        if not data:
            raise OSError("short lifecycle read")
        lifecycle, carry = _consume_lifecycle_bytes(lifecycle, carry, data, pending_calls)
        remaining -= len(data)
    return lifecycle, carry


def _cursor_boundary(handle, offset):
    start = max(0, offset - _LIFECYCLE_BOUNDARY_BYTES)
    handle.seek(start)
    data = handle.read(offset - start)
    if len(data) != offset - start:
        raise OSError("short lifecycle boundary read")
    return data


def _lifecycle_timestamps_regressed(cursor, *stats):
    return any(
        stat.st_mtime_ns < cursor.mtime_ns or stat.st_ctime_ns < cursor.ctime_ns
        for stat in stats
    )


def _stable_path_stat(canonical_path, handle, endpoint):
    """Return the current path stat only while it still names this open snapshot."""
    opened = os.fstat(handle.fileno())
    current = os.stat(canonical_path)
    identity = (opened.st_dev, opened.st_ino)
    if identity != (current.st_dev, current.st_ino):
        raise OSError("lifecycle path identity changed")
    if opened.st_size < endpoint or current.st_size < endpoint:
        raise OSError("lifecycle file truncated during read")
    return current


def _initialize_lifecycle_cursor(canonical_path, chunk=65536):
    """Read one newly observed identity exactly once from byte zero."""
    with open(canonical_path, "rb") as handle:
        opened = os.fstat(handle.fileno())
        endpoint = opened.st_size
        pending_calls = {}
        lifecycle, carry = _read_lifecycle_range(
            handle, 0, endpoint, chunk, pending_calls=pending_calls)
        current = _stable_path_stat(canonical_path, handle, endpoint)
        boundary = _cursor_boundary(handle, endpoint)
    return _LifecycleCursor(
        dev=opened.st_dev,
        ino=opened.st_ino,
        offset=endpoint,
        mtime_ns=current.st_mtime_ns,
        ctime_ns=current.st_ctime_ns,
        carry=carry,
        lifecycle=lifecycle,
        boundary=boundary,
        pending_calls=pending_calls,
    )


def _parse_latest_task_lifecycle(rollout_path, chunk=65536, max_scan=1048576):
    """Return the latest exact lifecycle after one stable full-history scan.

    ``max_scan`` remains in the private compatibility surface because native-subagent
    callers explicitly pass ``None``. Filesystem errors deliberately reach the caller.
    """
    del max_scan
    canonical_path = os.path.realpath(os.path.abspath(rollout_path))
    return _initialize_lifecycle_cursor(canonical_path, chunk).lifecycle


def _latest_task_lifecycle(rollout_path, chunk=65536, max_scan=1048576):
    """Identity-bound incremental lifecycle observation.

    A path's first stable identity is scanned in full. Later calls verify a small
    boundary fingerprint and consume only bytes after the saved offset. Rotation,
    truncation, retargeting, or any I/O failure removes the cursor and returns no
    lifecycle for that observation; a later clean call may initialize afresh.
    Intermediate-byte rewrites are outside the contract and are not detected;
    detecting them would require rereading the whole file on every tick, which
    conflicts with this feature's purpose. ``max_scan=None`` is the exact full scan
    used for native-subagent state; see `_exact_task_lifecycle`.
    """
    if max_scan is None:
        return _exact_task_lifecycle(rollout_path, chunk)

    global _LIFECYCLE_CACHE_EVICTIONS
    canonical_path = os.path.realpath(os.path.abspath(rollout_path))
    cached = _LIFECYCLE_CACHE.get(canonical_path)
    try:
        before = os.stat(canonical_path)
    except OSError:
        _LIFECYCLE_CACHE.pop(canonical_path, None)
        return None

    if cached is not None:
        if (before.st_dev, before.st_ino) != (cached.dev, cached.ino):
            _LIFECYCLE_CACHE.pop(canonical_path, None)
            return None
        if before.st_size < cached.offset:
            _LIFECYCLE_CACHE.pop(canonical_path, None)
            return None
        if _lifecycle_timestamps_regressed(cached, before):
            _LIFECYCLE_CACHE.pop(canonical_path, None)
            return None
        if before.st_size == cached.offset:
            if (before.st_mtime_ns, before.st_ctime_ns) != (
                    cached.mtime_ns, cached.ctime_ns):
                _LIFECYCLE_CACHE.pop(canonical_path, None)
                return None
            _LIFECYCLE_CACHE.move_to_end(canonical_path)
            return cached.lifecycle

    try:
        if cached is None:
            updated = _initialize_lifecycle_cursor(canonical_path, chunk)
            confirmed = os.stat(canonical_path)
            if ((confirmed.st_dev, confirmed.st_ino) != (updated.dev, updated.ino)
                    or confirmed.st_size < updated.offset):
                raise OSError("lifecycle identity changed after initialization")
        else:
            with open(canonical_path, "rb") as handle:
                opened = os.fstat(handle.fileno())
                endpoint = opened.st_size
                if (opened.st_dev, opened.st_ino) != (cached.dev, cached.ino):
                    raise OSError("lifecycle identity changed before append read")
                if endpoint < cached.offset:
                    raise OSError("lifecycle file truncated before append read")
                if _lifecycle_timestamps_regressed(cached, opened):
                    raise OSError("lifecycle timestamps regressed before append read")
                if _cursor_boundary(handle, cached.offset) != cached.boundary:
                    raise OSError("saved lifecycle boundary changed before append read")
                pending_calls = dict(cached.pending_calls or {})
                lifecycle, carry = _read_lifecycle_range(
                    handle, cached.offset, endpoint, chunk,
                    cached.lifecycle, cached.carry, pending_calls=pending_calls,
                )
                current = _stable_path_stat(canonical_path, handle, endpoint)
                if _lifecycle_timestamps_regressed(cached, current):
                    raise OSError("lifecycle timestamps regressed after append read")
                boundary = _cursor_boundary(handle, endpoint)
            updated = _LifecycleCursor(
                dev=opened.st_dev,
                ino=opened.st_ino,
                offset=endpoint,
                mtime_ns=current.st_mtime_ns,
                ctime_ns=current.st_ctime_ns,
                carry=carry,
                lifecycle=lifecycle,
                boundary=boundary,
                pending_calls=pending_calls,
            )
    except (OSError, ValueError):
        _LIFECYCLE_CACHE.pop(canonical_path, None)
        return None

    _LIFECYCLE_CACHE[canonical_path] = updated
    _LIFECYCLE_CACHE.move_to_end(canonical_path)
    while len(_LIFECYCLE_CACHE) > _LIFECYCLE_CACHE_MAX:
        _LIFECYCLE_CACHE.popitem(last=False)
        _LIFECYCLE_CACHE_EVICTIONS += 1
    return updated.lifecycle


def _exact_task_lifecycle(rollout_path, chunk=65536):
    """Full-history lifecycle, rescanned whenever the file's stat stamp changes.

    Completed native children keep ``open`` spawn edges, so every child ever spawned
    is observed on every tick. Only a byte-identical stat (identity, size, mtime,
    ctime) reuses the previous full scan; any change, including an in-place rewrite,
    replacement, or I/O failure, scans or fails exactly as an uncached read would.
    The incremental session cursor is neither consulted nor mutated.
    """
    canonical_path = os.path.realpath(os.path.abspath(rollout_path))
    try:
        st = os.stat(canonical_path)
    except OSError:
        _EXACT_LIFECYCLE_CACHE.pop(canonical_path, None)
        return None
    stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    cached = _EXACT_LIFECYCLE_CACHE.get(canonical_path)
    if cached is not None and cached[0] == stamp:
        _EXACT_LIFECYCLE_CACHE.move_to_end(canonical_path)
        return cached[1]
    try:
        lifecycle = _parse_latest_task_lifecycle(rollout_path, chunk=chunk, max_scan=None)
    except (OSError, ValueError):
        _EXACT_LIFECYCLE_CACHE.pop(canonical_path, None)
        return None
    # The stamp predates the scan, so a write racing the scan forces the next rescan.
    _EXACT_LIFECYCLE_CACHE[canonical_path] = (stamp, lifecycle)
    _EXACT_LIFECYCLE_CACHE.move_to_end(canonical_path)
    while len(_EXACT_LIFECYCLE_CACHE) > _LIFECYCLE_CACHE_MAX:
        _EXACT_LIFECYCLE_CACHE.popitem(last=False)
    return lifecycle


def _subagent_active(edge_status, rollout_path, updated_at=None, updated_at_ms=None,
                     now=None):
    """Active/done from the edge plus the child's latest persisted task lifecycle.

    An ``open`` edge means the thread remains available for follow-ups, not that its
    current turn is still running.  A completed native agent therefore commonly retains
    an open edge while its rollout ends in ``task_complete``.
    """
    normalized = edge_status.strip().lower() if isinstance(edge_status, str) else ""
    if normalized == "closed":
        return False
    if normalized != "open" or not isinstance(rollout_path, str) or not rollout_path:
        return None
    # The state DB already proves this is a native child.  Its current turn can be
    # larger than the ordinary session tail budget, so do not discard an otherwise
    # exact spawn edge merely because the matching lifecycle lies beyond 1 MiB.
    lifecycle = _latest_task_lifecycle(rollout_path, max_scan=None)
    if lifecycle is None:
        return None
    if lifecycle[0] in ("task_complete", "turn_aborted"):
        return False
    try:
        freshness = os.path.getmtime(rollout_path)
    except OSError:
        return None
    if isinstance(updated_at_ms, (int, float)) and updated_at_ms > 0:
        freshness = max(freshness, updated_at_ms / 1000.0)
    elif isinstance(updated_at, (int, float)) and updated_at > 0:
        freshness = max(freshness, float(updated_at))
    age = (time.time() if now is None else now) - freshness
    return True if age <= SESSION_WORK_SEC else None


def _build_thread_subagents(db_path, parent_ids=None):
    """Build the exact spawn-edge mapping from one readable Codex state DB.

    ``parent_ids`` narrows the expensive child-lifecycle reads to edges of
    exact parents collected this tick; ``None`` keeps the legacy full build.
    Duplicate/foreign-parent verification always sees every edge (a separate
    cheap pair query), and out-of-scope children are omitted without any
    dead/missing claim — the tick-scoped index cache never serves a narrowed
    map to a wider scope.
    """
    scope = None if parent_ids is None else frozenset(
        parent_id for parent_id in parent_ids
        if isinstance(parent_id, str) and parent_id)
    full_rows_query = (
        "SELECT e.parent_thread_id, e.child_thread_id, e.status, "
        "t.agent_role, t.agent_path, t.created_at, t.created_at_ms, "
        "t.thread_source, t.source, t.rollout_path, t.updated_at, t.updated_at_ms "
        "FROM thread_spawn_edges AS e "
        "JOIN threads AS t ON t.id = e.child_thread_id"
    )
    connection = None
    try:
        connection = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        if scope is None:
            rows = list(connection.execute(full_rows_query))
            pair_rows = rows
        else:
            pair_rows = list(connection.execute(
                "SELECT e.parent_thread_id, e.child_thread_id "
                "FROM thread_spawn_edges AS e"
            ))
            if scope:
                ordered = sorted(scope)
                placeholders = ",".join("?" for _ in ordered)
                rows = list(connection.execute(
                    full_rows_query +
                    " WHERE e.parent_thread_id IN (%s)" % placeholders,
                    tuple(ordered),
                ))
            else:
                rows = []
    finally:
        if connection is not None:
            connection.close()

    raw_parents = {}
    for parent_id, child_id, *_rest in pair_rows:
        if isinstance(parent_id, str) and parent_id and isinstance(child_id, str) and child_id:
            raw_parents.setdefault(child_id, set()).add(parent_id)

    by_child = {}
    for (parent_id, child_id, status, role, agent_path, created, created_ms,
         thread_source, source, rollout_path, updated, updated_ms) in rows:
        if not (isinstance(parent_id, str) and parent_id and
                isinstance(child_id, str) and child_id and
                isinstance(thread_source, str) and thread_source.lower() == "subagent"):
            continue
        if len(raw_parents.get(child_id, set())) != 1:
            continue
        try:
            source_data = json.loads(source) if isinstance(source, str) else source
            source_parent = source_data["subagent"]["thread_spawn"]["parent_thread_id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if source_parent != parent_id:
            continue
        active = _subagent_active(status, rollout_path, updated, updated_ms)
        if active is None:
            continue
        started_at = None
        if isinstance(created_ms, (int, float)) and created_ms > 0:
            started_at = created_ms / 1000.0
        elif isinstance(created, (int, float)) and created > 0:
            started_at = float(created)
        ended_at = None
        if not active:
            # 완료 시각 = the thread's last update (사용자 2026-07-29 '언제 끝났는지')
            if isinstance(updated_ms, (int, float)) and updated_ms > 0:
                ended_at = updated_ms / 1000.0
            elif isinstance(updated, (int, float)) and updated > 0:
                ended_at = float(updated)
        entry = (parent_id, SubAgent(
            agent_type=_agent_type(role, agent_path), active=active,
            started_at=started_at, ended_at=ended_at, source="codex-state-db"))
        by_child.setdefault(child_id, []).append(entry)

    mapped = {}
    for entries in by_child.values():
        parents = {parent_id for parent_id, _subagent in entries}
        if len(parents) != 1:
            continue                         # ambiguous linkage: omission beats misattribution
        parent_id, subagent = entries[0]
        mapped.setdefault(parent_id, []).append(subagent)
    return mapped


def _thread_subagents(home, parent_ids=None):
    """Exact parent-thread subagents from the Codex state DB, read-only and fail-closed.

    ``None`` means the source/schema is unavailable; a mapping means the source was
    checked (a missing parent key therefore means zero observed children).  The edge is
    the attribution authority: rollout cwd/mtime/title heuristics never attach children.

    ``parent_ids`` scopes the build to exact parents collected this tick; the
    index entry is keyed by that scope, so a narrowed map is never served to a
    wider tick. ``None`` keeps the legacy full build.
    """
    scope = None if parent_ids is None else frozenset(
        parent_id for parent_id in parent_ids
        if isinstance(parent_id, str) and parent_id)
    db_path = _state_db(home)
    stamp = (
        db_path,
        _file_stamp(db_path),
        _file_stamp(db_path + "-wal") if db_path else None,
        _file_stamp(db_path + "-shm") if db_path else None,
    )
    now = time.time()
    cached = _SUBAGENT_INDEX.get(home)
    if (cached and len(cached) == 5 and cached[1] == stamp
            and now - cached[0] < _INDEX_TTL and cached[4] == scope):
        return cached[2] if cached[3] else None
    if not db_path:
        _SUBAGENT_INDEX[home] = (now, stamp, {}, False, scope)
        return None
    try:
        mapped = _build_thread_subagents(db_path, parent_ids=scope)
    except (OSError, sqlite3.Error):
        _SUBAGENT_INDEX[home] = (now, stamp, {}, False, scope)
        return None
    _SUBAGENT_INDEX[home] = (now, stamp, mapped, True, scope)
    return mapped


def _tick_subagents(tick, home, parent_ids=None):
    """Return one home snapshot, loading it at most once in this collection."""
    normalized = os.path.abspath(home)
    if normalized not in tick.subagents_by_home:
        tick.subagents_by_home[normalized] = _thread_subagents(normalized, parent_ids)
    return tick.subagents_by_home[normalized]


def _subagents_for_session(tick, home, session_id):
    """Tick-local subagent map for one session, widened when out of scope.

    A narrowed tick map cannot tell "zero children" from "never scanned" for
    a session id outside its scope (e.g. heuristic fallback attributions).
    On such a lookup the tick widens every home map to the full build once --
    worst case equals the legacy cost, output always equals the legacy map.
    """
    scope = tick.subagents_parent_ids
    if scope is not None and session_id and session_id not in scope:
        for home_key in list(tick.subagents_by_home):
            tick.subagents_by_home[home_key] = _thread_subagents(home_key, None)
        tick.subagents_parent_ids = None
    return _tick_subagents(tick, home)


def _config_model_effort(home):
    """Global ~/.codex/config.toml model/effort.

    B-6/3: `enrich()` no longer calls this to pre-seed a session's model/effort — a
    row whose rollout attribution fails must show None ("—"), not the global default
    dressed up as its own value (F-26b "no guessing"). Definition kept for callers
    that explicitly want the config fallback (currently none in this collector)."""
    now = time.time()
    if now - _CFG["ts"] < 10.0 and (_CFG["model"] or _CFG["effort"]):
        return _CFG["model"], _CFG["effort"]
    model = effort = None
    try:
        with open(os.path.join(home, "config.toml"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                s = ln.strip()
                if s.startswith("["):              # stop at first [section] — top-level keys only
                    break
                if not s or s.startswith("#") or "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key == "model":
                    model = val
                elif key == "model_reasoning_effort":
                    effort = val
    except Exception:
        pass
    _CFG.update(ts=now, model=model, effort=effort)
    return model, effort


def _turn_context_values(raw):
    """Return non-empty model/effort values from one valid ``turn_context`` row."""
    if b"turn_context" not in raw:
        return None
    try:
        row = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(row, dict) or row.get("type") != "turn_context":
        return None
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    effort = payload.get("effort")
    model = model.strip() if isinstance(model, str) and model.strip() else None
    effort = effort.strip() if isinstance(effort, str) and effort.strip() else None
    return (model, effort) if model or effort else None


def _complete_jsonl_end(handle, size, chunk=65536):
    """Byte boundary after the last complete JSONL row in ``handle``."""
    if size <= 0:
        return 0
    handle.seek(size - 1)
    if handle.read(1) == b"\n":
        return size
    end = size
    while end > 0:
        start = max(0, end - chunk)
        handle.seek(start)
        data = handle.read(end - start)
        newline = data.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def _parse_latest_turn_context(path, chunk=65536):
    """Return the latest valid rollout model/effort and its complete-row offset.

    The cold path walks backward and stops at the first valid ``turn_context``;
    it does not assume that the record fits in a fixed tail window. Callers can
    then scan only newly appended complete rows on later ticks.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        complete_end = _complete_jsonl_end(handle, size, chunk)
        end = complete_end
        carry = b""
        while end > 0:
            start = max(0, end - chunk)
            handle.seek(start)
            data = handle.read(end - start) + carry
            lines = data.splitlines()
            if start > 0:
                carry = lines.pop(0) if lines else data
            for raw in reversed(lines):
                values = _turn_context_values(raw)
                if values is not None:
                    return values, complete_end
            end = start
    return (None, None), complete_end


def _parse_appended_turn_context(path, offset, size, previous):
    """Fold complete rows appended after ``offset`` into a cached value."""
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read(max(0, size - offset))
    newline = data.rfind(b"\n")
    if newline < 0:
        return previous, offset
    values = previous
    for raw in data[:newline + 1].splitlines():
        candidate = _turn_context_values(raw)
        if candidate is not None:
            values = candidate
    return values, offset + newline + 1


def _rollout_model_effort(path, chunk=65536):
    """Latest per-rollout model/effort with an append-aware, strong-stamp LRU."""
    global _TURN_CONTEXT_CACHE_EVICTIONS
    canonical = os.path.realpath(os.path.abspath(path))
    try:
        before = os.stat(canonical)
    except OSError:
        _TURN_CONTEXT_CACHE.pop(canonical, None)
        return None, None
    stamp = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    cached = _TURN_CONTEXT_CACHE.get(canonical)
    if cached is not None and cached["stamp"] == stamp:
        _TURN_CONTEXT_CACHE.move_to_end(canonical)
        return cached["values"]
    try:
        if (cached is not None
                and cached["identity"] == stamp[:2]
                and before.st_size > cached["observed_size"]):
            values, offset = _parse_appended_turn_context(
                canonical, cached["offset"], before.st_size, cached["values"]
            )
        else:
            values, offset = _parse_latest_turn_context(canonical, chunk)
        after = os.stat(canonical)
    except OSError:
        _TURN_CONTEXT_CACHE.pop(canonical, None)
        return None, None
    after_stamp = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    if after_stamp != stamp:
        return values
    _TURN_CONTEXT_CACHE[canonical] = {
        "identity": stamp[:2],
        "observed_size": before.st_size,
        "offset": offset,
        "stamp": stamp,
        "values": values,
    }
    _TURN_CONTEXT_CACHE.move_to_end(canonical)
    while len(_TURN_CONTEXT_CACHE) > _TURN_CONTEXT_CACHE_MAX:
        _TURN_CONTEXT_CACHE.popitem(last=False)
        _TURN_CONTEXT_CACHE_EVICTIONS += 1
    return values


def _rollout_cwd(path):
    """cwd from session_meta (line 1), reread only when the file's stat changes."""
    try:
        st = os.stat(path)
    except OSError:
        _ROLLOUT_CWD_CACHE.pop(path, None)
        return None
    stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _ROLLOUT_CWD_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    cwd = _read_rollout_cwd(path)
    _ROLLOUT_CWD_CACHE[path] = (stamp, cwd)
    if len(_ROLLOUT_CWD_CACHE) > _ROLLOUT_CWD_CACHE_MAX:
        _ROLLOUT_CWD_CACHE.pop(next(iter(_ROLLOUT_CWD_CACHE)))
    return cwd


def _read_rollout_cwd(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = json.loads(f.readline())
        if m.get("type") == "session_meta":
            return (m.get("payload") or {}).get("cwd")
    except Exception:
        pass
    return None


def _rollout_meta(path):
    """Minimal session_meta payload for a rollout, or {} when it is unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = json.loads(f.readline())
        if m.get("type") == "session_meta":
            return m.get("payload") or {}
    except Exception:
        pass
    return {}


def _index(home):
    now = time.time()
    if (_INDEX["map"] is not None and now - _INDEX["ts"] < _INDEX_TTL
            and _INDEX.get("home") == home):
        return _INDEX["map"]
    base = os.path.join(home, "sessions")
    files = []
    for root, _dirs, names in os.walk(base):
        for n in names:
            if n.startswith("rollout-") and n.endswith(".jsonl"):
                p = os.path.join(root, n)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    files.sort(reverse=True)
    m = {}
    for _mt, p in files:
        cwd = _rollout_cwd(p)
        if cwd:
            m.setdefault(cwd, []).append(p)
    _INDEX.update(ts=now, map=m, home=home)
    return m


def _sid(path):
    match = _SID_RE.search(os.path.basename(path))
    return match.group(1) if match else None


def exact_rollout_for_session_id(session_id, homes=None):
    """Return the one rollout with this exact session id across fixed runtime homes.

    Dispatch worktrees project their active Codex home under ``.dispatch``.  Accepting an
    explicit home set lets the dispatch collector use that projection without walking every
    runtime directory. Realpath dedup keeps a projection symlink and its target from looking
    ambiguous; genuinely distinct duplicate rollouts fail closed.
    """
    if not isinstance(session_id, str) or _sid("rollout-x-%s.jsonl" % session_id) != session_id:
        return None
    raw_homes = list(homes) if homes is not None else [_home()]
    paths = set()
    seen_homes = set()
    pattern = "rollout-*-%s.jsonl" % session_id
    for raw_home in raw_homes:
        if not isinstance(raw_home, (str, bytes, os.PathLike)):
            continue
        home = os.path.realpath(os.path.expanduser(os.fspath(raw_home)))
        if home in seen_homes:
            continue
        seen_homes.add(home)
        root = os.path.join(home, "sessions")
        for path in glob.glob(os.path.join(root, "*", "*", "*", pattern)):
            paths.add(os.path.realpath(path))
        for path in glob.glob(os.path.join(root, pattern)):
            paths.add(os.path.realpath(path))
    return paths.pop() if len(paths) == 1 else None


def _is_subagent(meta):
    source = meta.get("source")
    return (
        source == "subagent"
        or (isinstance(source, dict) and "subagent" in source)
        or meta.get("originator") == "subagent"
        or meta.get("thread_source") == "subagent"
    )


def _rollout_fd_paths(pid):
    """``(paths, partial)``: the rollout files this process holds open, as realpaths.

    A rollout is a ``…/sessions/…/rollout-*-<sid>.jsonl`` shape (``_sid``). ``partial``
    is True when some fd link could not be read for a reason other than the fd closing
    under us, so ``paths`` may miss a file. An unreadable fd directory raises OSError.
    """
    names = os.listdir("/proc/%s/fd" % pid)
    sessions_marker = os.sep + "sessions" + os.sep
    paths = []
    partial = False
    for name in names:
        try:
            path = os.readlink("/proc/%s/fd/%s" % (pid, name))
            if path.endswith(" (deleted)"):
                path = path[:-10]
        except FileNotFoundError:
            continue
        except OSError:
            partial = True
            continue
        real = os.path.realpath(path)
        if sessions_marker in real and _sid(real):
            paths.append(real)
    return paths, partial


def _proc_rollout(pid, cwd, home):
    """Return the rollout open by this process, preferring root/user over subagent.

    Deliberately NOT pinned to ``home`` (2026-07-19): a dispatched worker runs with a
    worktree-local ``CODEX_HOME`` (``.dispatch/nested-codex-home``), so its rollout never
    lives under the fleet process's own home — the old home-prefix filter silently dropped
    every nested child, which is why they never earned a title/subtitle. The rollout SHAPE
    (``…/sessions/…/rollout-*.jsonl`` via ``_sid``) plus the session_meta cwd match below
    is already the ownership proof; the home root added nothing but the false negative.
    """
    candidates = []
    unmatched = []
    try:
        paths, _partial = _rollout_fd_paths(pid)
    except OSError:
        return None
    wanted_cwd = os.path.realpath(cwd)
    for real in paths:
        meta = _rollout_meta(real)
        if os.path.realpath(meta.get("cwd") or "") != wanted_cwd:
            unmatched.append(real)
            continue
        candidates.append((1 if _is_subagent(meta) else 0, real))
    if candidates:
        # A shared App Server can hold many root threads from the same cwd.
        # Prefer a root over a subagent only when that preference identifies
        # exactly one rollout; lexical order is never session ownership proof.
        roots = {path for rank, path in candidates if rank == 0}
        preferred = roots or {path for _rank, path in candidates}
        return next(iter(preferred)) if len(preferred) == 1 else None
    # No cwd match, but the process holds exactly ONE rollout open: the fd itself is the
    # ownership proof. This is the `codex exec --cd <worktree>` shape (2026-07-19): the
    # PROCESS cwd stays where the wrapper launched it while session_meta records the
    # --cd target, so the equality check above can never match a dispatched worker.
    # Multiple unmatched rollouts stay refused — an app-server holds many sessions'
    # files, and guessing among them is the misattribution F-26 forbids.
    if len(set(unmatched)) == 1:
        return unmatched[0]
    return None




def open_rollout_sessions(pids=None):
    """``{"complete": bool, "open": frozenset(session ids)}`` for the live Codex processes,
    or None when the process table cannot be listed.

    ``open`` holds the sid of every rollout a same-UID, non-zombie ``codex`` process has
    open: the one proof that a Codex thread is still held by a running app-server or TUI.
    ``complete`` is True only when every such process's fd table was fully read and the
    caller sits in the initial PID namespace; otherwise an absent sid proves nothing.
    ``pids`` restricts the walk (tests pass fixture pids)."""
    from . import procscan
    if pids is None:
        try:
            pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
        except OSError:
            return None
    complete = procscan.initial_pid_namespace()
    held = set()
    uid = os.geteuid()
    for pid in pids:
        try:
            if os.stat("/proc/%s" % pid).st_uid != uid:
                continue
        except OSError:
            continue
        if procscan._comm_of(pid) != "codex" or procscan.is_terminal_state(pid):
            continue
        try:
            paths, partial = _rollout_fd_paths(pid)
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
            continue
        complete = complete and not partial
        held.update(sid for sid in map(_sid, paths) if sid)
    return {"complete": complete, "open": frozenset(held)}


def _control_socket(environ):
    """The App Server control socket selected by this Codex process's own environment."""
    utilities = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "utilities"))
    if utilities not in sys.path:
        sys.path.insert(0, utilities)
    from codex_queue_dispatch import resolve_queue_socket
    raw = dict(environ or {})
    raw.setdefault("CODEX_HOME", _home())
    return resolve_queue_socket(raw)


def _origin_port(value):
    if not isinstance(value, str):
        return None
    try:
        origin = urlsplit(value)
        if (origin.scheme != "http" or origin.hostname != "127.0.0.1"
                or origin.username is not None or origin.password is not None
                or origin.path or origin.query or origin.fragment):
            return None
        port = origin.port
    except (TypeError, ValueError):
        return None
    return port if isinstance(port, int) and 1 <= port <= 65535 else None


def _thread_source(thread):
    if not isinstance(thread, dict):
        return None
    value = thread.get("threadSource") or thread.get("thread_source")
    source = value.lower() if isinstance(value, str) and value else None
    legacy_source = thread.get("source")
    parent = thread.get("parentThreadId")
    if (source == "subagent" or isinstance(parent, str) and parent
            or isinstance(legacy_source, dict) and "subagent" in legacy_source):
        return "subagent"
    if source in {"user", "root"} or "parentThreadId" in thread and parent is None:
        return "user"
    return None


class _DaemonOrigins(dict):
    """One complete loaded-thread snapshot tied to a live control socket inode."""

    def __init__(self, values, *, endpoint, cache_key):
        super().__init__(values)
        self.endpoint = endpoint
        self.cache_key = cache_key


def _invalidate_daemon_cache(endpoint=None):
    if endpoint is None:
        _DAEMON_ORIGINS.clear()
        return
    endpoint = str(endpoint)
    for key in tuple(_DAEMON_ORIGINS):
        if isinstance(key, tuple) and key and key[0] == endpoint:
            _DAEMON_ORIGINS.pop(key, None)


def _control_socket_identity(endpoint):
    """Return the safe, live Unix-socket identity used to bind daemon observations."""
    from pathlib import Path
    from codex_queue_delivery import resolve_endpoint

    resolved = resolve_endpoint(Path(endpoint))
    info = resolved.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        return None
    return (str(resolved), info.st_dev, info.st_ino, info.st_uid,
            stat.S_IMODE(info.st_mode))


def _daemon_request(client, method, params, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Codex daemon observation deadline expired")
    connection = getattr(client, "socket", None)
    if connection is not None:
        connection.settimeout(min(_DAEMON_RPC_TIMEOUT, remaining))
    return client.request(method, params)


def _daemon_read_pages(client, method, params, deadline, *, max_items):
    """Read one schema-paged list completely, rejecting loops and exhausted budgets."""
    values = []
    cursor = None
    seen = set()
    for _page in range(_DAEMON_MAX_PAGES):
        request = dict(params)
        request["limit"] = _DAEMON_PAGE_SIZE
        if cursor is not None:
            request["cursor"] = cursor
        response = _daemon_request(client, method, request, deadline)
        data = response.get("data")
        if not isinstance(data, list):
            raise ValueError("Codex daemon list response omitted data")
        values.extend(data)
        if len(values) > max_items:
            raise ValueError("Codex daemon list exceeded its observation limit")
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            return values
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            raise ValueError("Codex daemon returned an invalid or looping cursor")
        seen.add(next_cursor)
        cursor = next_cursor
    raise ValueError("Codex daemon page budget exhausted")


def _daemon_thread_origins(environ):
    """Read a complete loaded/source/status snapshot, caching only loaded threads.

    The only daemon calls are initialize, thread/loaded/list,
    mcpServerStatus/list, and a turns-free thread/read for its root/subagent marker.
    All are observations; no thread is resumed, subscribed, or modified. Any failed
    page or per-thread read invalidates the whole cache so an old unique result cannot
    survive an incomplete observation.
    """
    endpoint_path = None
    cache_key = None
    client = None
    try:
        endpoint = _control_socket(environ)
        identity = _control_socket_identity(endpoint)
        if identity is None:
            raise OSError("Codex control socket identity is unavailable")
        endpoint_path = identity[0]
        cache_key = identity
        # A replacement at the same pathname starts a fresh cache lifetime.
        for old_key in tuple(_DAEMON_ORIGINS):
            if (isinstance(old_key, tuple) and old_key and old_key[0] == endpoint_path
                    and old_key != cache_key):
                _DAEMON_ORIGINS.pop(old_key, None)
        cache = _DAEMON_ORIGINS.get(cache_key)
        if cache is None:
            cache = {}
        utilities = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "utilities"))
        if utilities not in sys.path:
            sys.path.insert(0, utilities)
        from codex_queue_delivery import _connect
        deadline = time.monotonic() + _DAEMON_RPC_SECONDS
        client = _connect(endpoint, min(_DAEMON_RPC_TIMEOUT, _DAEMON_RPC_SECONDS))
    except Exception:
        _invalidate_daemon_cache(endpoint_path)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        return None
    try:
        connection = getattr(client, "socket", None)
        if connection is not None:
            connection.settimeout(min(_DAEMON_RPC_TIMEOUT, deadline - time.monotonic()))
        # _connect already initialized; the daemon refuses a second initialize.
        rows = _daemon_read_pages(client, "thread/loaded/list", {}, deadline,
                                  max_items=_DAEMON_MAX_THREADS)
        thread_ids = []
        for thread_id in rows:
            if (not isinstance(thread_id, str)
                    or _sid("rollout-x-%s.jsonl" % thread_id) != thread_id
                    or thread_id in thread_ids):
                raise ValueError("Codex daemon returned an invalid or duplicate loaded thread")
            thread_ids.append(thread_id)
        loaded_ids = set(thread_ids)
        for thread_id in tuple(cache):
            if thread_id not in loaded_ids:
                del cache[thread_id]
        missing = [thread_id for thread_id in thread_ids if thread_id not in cache]
        for thread_id in missing:
            thread_result = _daemon_request(client, "thread/read", {
                "threadId": thread_id, "includeTurns": False,
            }, deadline)
            thread = thread_result.get("thread")
            if not isinstance(thread, dict) or thread.get("id") != thread_id:
                raise ValueError("Codex daemon omitted loaded-thread source metadata")
            source = _thread_source(thread)
            if source not in {"user", "subagent"}:
                raise ValueError("Codex daemon thread root/source is unknown")

            statuses = _daemon_read_pages(client, "mcpServerStatus/list", {
                "threadId": thread_id, "serverName": "codex_tui",
                "detail": "toolsAndAuthOnly",
            }, deadline, max_items=128)
            if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   for item in statuses):
                raise ValueError("Codex daemon returned malformed MCP status data")
            entries = [item for item in statuses
                       if item.get("name") == "codex_tui"]
            if len(entries) > 1:
                raise ValueError("Codex daemon returned duplicate codex_tui status entries")
            origin_port = None
            if entries:
                entry = entries[0]
                runtime_status = entry.get("runtimeStatus")
                if runtime_status is None:
                    raise ValueError("Codex daemon MCP status is incomplete")
                if runtime_status == "connected":
                    origin_port = _origin_port(entry.get("httpOrigin"))
                    if origin_port is None:
                        raise ValueError("Codex daemon returned an unusable codex_tui origin")
                elif runtime_status not in {
                        "notStarted", "starting", "authenticationRequired", "failed",
                        "cancelled", "disabled"}:
                    raise ValueError("Codex daemon returned an unknown MCP runtime status")
            cache[thread_id] = (origin_port, source)

        if _control_socket_identity(endpoint) != cache_key:
            raise OSError("Codex control socket changed during observation")
        _DAEMON_ORIGINS[cache_key] = cache
        return _DaemonOrigins(cache, endpoint=endpoint, cache_key=cache_key)
    except Exception:
        _invalidate_daemon_cache(endpoint_path)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def _socket_owners_by_inode():
    """Map each socket inode to the same-UID PIDs holding it, or unknown."""
    owners = {}
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    for raw_pid in pids:
        try:
            process_stat = os.stat("/proc/%s" % raw_pid)
            if process_stat.st_uid != os.geteuid():
                continue
            fd_dir = "/proc/%s/fd" % raw_pid
            try:
                fds = os.listdir(fd_dir)
            except FileNotFoundError:
                continue
            for fd in fds:
                try:
                    link = os.readlink("%s/%s" % (fd_dir, fd))
                except FileNotFoundError:
                    continue
                if link.startswith("socket:["):
                    owners.setdefault(link[8:-1], set()).add(int(raw_pid))
        except (FileNotFoundError, PermissionError):
            # Gone, or a same-UID process whose fd table is closed to us (user
            # systemd, privsep sshd, sandboxed children). It cannot be proven an
            # owner, so it is skipped instead of voiding every listener snapshot.
            continue
        except OSError:
            return None
    return owners


def _proc_net_tcp_rows(path):
    """The data rows of one ``/proc/net/tcp*`` table as field lists, or None if unreadable."""
    try:
        with open(path, encoding="ascii", errors="strict") as handle:
            return [row.split() for row in handle.read().splitlines()[1:]]
    except (OSError, UnicodeError):
        return None


def _tcp_connections():
    """``[(state, local, remote, inode)]`` across IPv4 and IPv6 TCP, or None if either table
    is unreadable or malformed. Addresses stay the kernel's ``hexaddr:hexport`` strings."""
    out = []
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        rows = _proc_net_tcp_rows(path)
        if rows is None:
            return None
        for fields in rows:
            if len(fields) < 10:
                return None
            out.append((fields[3], fields[1], fields[2], fields[9]))
    return out


def _tcp_listener_snapshot(port, scan=None):
    """Return the exact IPv4 LISTEN inode and same-UID owner sets, or unknown.

    ``scan`` is one observation's shared fd-table read: the ports a caller
    snapshots together reuse a single walk of every process instead of one walk
    per port. A caller re-observing passes a fresh one.
    """
    ports = scan.setdefault("ports", {}) if scan is not None else None
    if ports is not None and port in ports:
        return ports[port]
    table = scan.get("tcp_table") if scan is not None else None
    if table is None:
        rows = _proc_net_tcp_rows("/proc/net/tcp")
        if rows is None:
            table = _TCP_UNKNOWN
        else:
            parsed = {}
            for fields in rows:
                try:
                    if fields[3] == "0A":
                        row_port = int(fields[1].rsplit(":", 1)[1], 16)
                        parsed.setdefault(row_port, set()).add(fields[9])
                except (IndexError, ValueError):
                    parsed = _TCP_UNKNOWN
                    break
            table = (_TCP_UNKNOWN if parsed is _TCP_UNKNOWN else
                     {row_port: frozenset(inodes) for row_port, inodes in parsed.items()})
        if scan is not None:
            scan["tcp_table"] = table
    if table is _TCP_UNKNOWN:
        if ports is not None:
            ports[port] = None
        return None
    inodes = table.get(port, frozenset())
    if not inodes:
        result = (frozenset(), frozenset())
        if ports is not None:
            ports[port] = result
        return result
    if scan is not None and "owners" in scan:
        by_inode = scan["owners"]
    else:
        by_inode = _socket_owners_by_inode()
        if scan is not None:
            scan["owners"] = by_inode
    if by_inode is None:
        result = None
    else:
        owners = set()
        for inode in inodes:
            owners |= by_inode.get(inode, set())
        result = frozenset(inodes), frozenset(owners)
    if ports is not None:
        ports[port] = result
    return result


def _daemon_endpoint_current(origins):
    try:
        return _control_socket_identity(origins.endpoint) == origins.cache_key
    except Exception:
        return False


def _listener_unchanged(port, observed, scan=None):
    current = _tcp_listener_snapshot(port, scan)
    return current is not None and current == observed


def _invalidate_origins(origins):
    key = getattr(origins, "cache_key", None)
    _invalidate_daemon_cache(key[0] if isinstance(key, tuple) and key else None)


def _live_tui_start(pid):
    """A port owner counts only while it is a live Codex conversation frontend."""
    from . import procscan
    start = procscan.read_proc_start(pid)
    if not start or procscan._comm_of(pid) != "codex" or procscan.is_shared_codex_daemon(pid):
        return None
    argv = procscan._read_argv(pid)
    if procscan.invocation_mode("codex", argv) != "session":
        return None
    return start if procscan.read_proc_start(pid) == start else None


def _daemon_session_for_process(pid, environ=None, observation=None):
    """The sole root thread whose codex_tui listener belongs to this live TUI PID."""
    from . import procscan
    start = _live_tui_start(pid)
    if not start:
        return None
    env = environ if isinstance(environ, dict) else procscan.read_environ(pid)
    origins = _daemon_thread_origins(env)
    if origins is None:
        return None
    listeners = {}
    candidates = set()
    scan = observation.initial_scan if isinstance(observation, DaemonIdentityObservation) else {}
    for thread_id, (port, source) in origins.items():
        if not port or not source or source == "subagent":
            continue
        observed = listeners.get(port)
        if observed is None:
            observed = _tcp_listener_snapshot(port, scan)
            if observed is None:
                _invalidate_origins(origins)
                if isinstance(observation, DaemonIdentityObservation):
                    observation.invalidate()
                return None
            listeners[port] = observed
        inodes, owners = observed
        if inodes and owners == {int(pid)}:
            candidates.add(thread_id)
    if len(candidates) != 1:
        return None
    recheck = observation.recheck_scan if isinstance(observation, DaemonIdentityObservation) else {}
    if (not _daemon_endpoint_current(origins)
            or any(not _listener_unchanged(port, observed, recheck)
                   for port, observed in listeners.items())
            or procscan.read_proc_start(pid) != start):
        _invalidate_origins(origins)
        if isinstance(observation, DaemonIdentityObservation):
            observation.invalidate()
        return None
    return next(iter(candidates))


def process_for_thread(thread_id, environ=None):
    """Return the unique live root TUI PID/start owning ``thread_id``'s MCP origin."""
    if not isinstance(thread_id, str) or _sid("rollout-x-%s.jsonl" % thread_id) != thread_id:
        return None
    from . import procscan
    env = dict(environ or os.environ)
    origins = _daemon_thread_origins(env)
    row = origins.get(thread_id) if origins else None
    if not row:
        return None
    port, source = row
    if not port or not source or source == "subagent":
        return None
    observed = _tcp_listener_snapshot(port)
    if observed is None:
        _invalidate_origins(origins)
        return None
    inodes, owners = observed
    if not inodes or len(owners) != 1:
        return None
    pid = next(iter(owners))
    start = _live_tui_start(pid)
    if not start:
        return None
    if (not _daemon_endpoint_current(origins)
            or not _listener_unchanged(port, observed)
            or procscan.read_proc_start(pid) != start):
        _invalidate_origins(origins)
        return None
    return pid, start


def session_id_of_process(pid, live_codex=None, *, observation=None):
    """Return an fd-proven thread or the daemon's unique TUI-loader thread.

    A Codex TUI without its own rollout fd is identified only by its
    ``codex_tui`` MCP origin and the PID holding that origin's LISTEN socket.
    Time, cwd, and launcher records never attach a TUI thread.
    """
    try:
        pid = int(pid)
        cwd = os.readlink("/proc/%d/cwd" % pid)
    except (OSError, ValueError):
        return None
    from . import procscan
    # /proc/<pid>/environ retains the environment supplied at execve. Pass its
    # PWD explicitly as the launch-base candidate; it is independent of current
    # cwd and may differ after Codex applies --cd or otherwise changes directory.
    initial_env = procscan.read_environ(pid)
    initial_launch_pwd = initial_env.get("PWD")
    cwd = procscan.codex_effective_cwd(
        procscan._read_argv(pid), cwd, initial_launch_pwd)
    if procscan.is_shared_codex_daemon(pid):
        return None
    path = _proc_rollout(pid, cwd, _home())
    if path:
        return _sid(path)
    if procscan._comm_of(pid) != "codex":
        return None                     # procscan lists only `codex` itself as a runtime
    if procscan.invocation_mode("codex", procscan._read_argv(pid)) != "session":
        return None
    if observation is None:
        observation = _DAEMON_IDENTITY_SCOPE.get()
    return _daemon_session_for_process(pid, initial_env, observation=observation)






def _session_created(meta):
    """Parse a rollout's persisted creation time for context-policy telemetry."""
    value = meta.get("timestamp") if isinstance(meta, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return __import__("datetime").datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _process_started_at(sess):
    """Convert one exact Linux ``/proc`` start identity to epoch seconds.

    ``Session.proc_start`` is field 22 from ``/proc/<pid>/stat``: clock ticks
    since boot.  ``btime`` uses the same boot epoch, so the pair is materially
    stronger than the whole-minute ``ps etime`` used by the legacy fallback.
    Missing/non-Linux evidence stays unknown and never enables attribution.
    """
    try:
        ticks = int(sess.proc_start)
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        if ticks < 0 or clock_ticks <= 0:
            return None
        with open("/proc/stat", encoding="ascii", errors="replace") as handle:
            boot = next(
                int(line.split()[1]) for line in handle
                if line.startswith("btime ")
            )
    except (AttributeError, OSError, StopIteration, TypeError, ValueError):
        return None
    return boot + ticks / clock_ticks






def process_rollouts(sessions, home):
    """Return only rollouts directly held by each Codex process.

    This is telemetry ownership, not a TUI thread resolver. Daemon-attached
    TUIs without a rollout fd are identified by ``session_id_of_process`` using
    the loader origin; no time, cwd, or launcher matching runs here.
    """
    paths = {}
    claimed = set()
    for sess in sessions:
        if getattr(sess, "harness", None) != "codex" or not getattr(sess, "cwd", None):
            continue
        path = _proc_rollout(sess.pid, sess.cwd, home)
        # Role consumers need the native FD observation beside direct paths.
        sess._fd_owner = bool(path)
        if not path:
            continue
        paths[sess.pid] = path
        sid = _sid(path)
        if sid:
            claimed.add(sid)
    return paths, claimed








def _transfer_managed_rollouts(sessions, paths):
    """Hand each managed app-server's rollout to the TUI client of the SAME state dir.

    Managed (client-server) Codex splits one session across two `codex` processes: the
    `app-server` holds the rollout fd but is marked a companion and hidden, while the
    visible row is the `--remote` TUI client, which holds no transcript fd at all. Without
    this transfer the visible row loses session_id/title/context entirely.

    The join key is the managed-sessions state dir carried in both argv lines — exact path
    equality, never a heuristic. Fail closed (F-26): a dir without exactly one owning
    app-server and exactly one TUI client is left alone and degrades to the honest gap.
    Returns the set of pids that gave their rollout away (F-24: they must not re-claim it).
    """
    servers, clients = {}, {}
    for sess in sessions:
        managed_dir = getattr(sess, "managed_dir", None)
        if getattr(sess, "harness", None) != "codex" or not managed_dir:
            continue
        bucket = servers if getattr(sess, "app_server", False) else clients
        bucket.setdefault(managed_dir, []).append(sess)
    donated = set()
    for managed_dir, owners in servers.items():
        owning = [s for s in owners if paths.get(s.pid)]
        peers = clients.get(managed_dir, [])
        if len(owning) != 1 or len(peers) != 1:
            continue
        server, client = owning[0], peers[0]
        if paths.get(client.pid):
            continue                       # client already owns a rollout — nothing to fix
        paths[client.pid] = paths.pop(server.pid)
        # F-47: a managed session's exec children hang off the app-server, which is a hidden
        # companion row — move the evidence onto the visible TUI client through the SAME
        # exact managed-dir join, so the work shows up where the user can see it.
        if getattr(server, "exec_child", None) and not getattr(client, "exec_child", None):
            client.exec_child = server.exec_child
        donated.add(server.pid)
    return donated


def share_managed_tags(sessions):
    """Give each managed app-server row the `[xx]` tag of its own TUI client.

    A managed Codex session is two `codex` processes on one `--state-dir`: the
    `--remote .../managed-tui.sock` client owns the rollout after
    `_transfer_managed_rollouts` (so the thread id and its assigned tag), while the
    `app-server --listen` sibling holds no rollout of its own and leaves `enrich`
    before the shared tag reader runs (2026-09-08 audit: 8 Codex rows with a blank badge).
    The pairing key is the exact managed state dir carried in both argv lines --
    never the thread id, never a session-id reverse lookup. Exactly one server
    and exactly one tagged client per dir; anything else stays as it is. A row
    that already has a tag is never changed. A Codex app-server row that ends
    without a tag is marked unpaired so the badge can say so instead of staying
    blank (render `_session_tag_chip`); the mark is ephemeral, never in --json.
    """
    servers, clients = {}, {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "codex":
            continue
        managed_dir = getattr(sess, "managed_dir", None)
        if not managed_dir:
            continue
        key = os.path.normpath(managed_dir)
        bucket = servers if getattr(sess, "app_server", False) else clients
        bucket.setdefault(key, []).append(sess)
    for key, owners in servers.items():
        tagged = [
            c for c in clients.get(key, [])
            if isinstance(getattr(c, "session_tag", None), str) and c.session_tag
        ]
        if len(owners) != 1 or len(tagged) != 1:
            continue
        server = owners[0]
        if getattr(server, "session_tag", None):
            continue
        server.session_tag = tagged[0].session_tag
    for sess in sessions:
        if not (getattr(sess, "harness", None) == "codex" and getattr(sess, "app_server", False)):
            continue
        if not getattr(sess, "session_tag", None):
            sess._session_tag_unpaired = True
        # B-6/4: mark whether this app-server row has a live TUI client in the same
        # managed dir. `model.session_parent_visible` reads this to un-hide an
        # app-server row that IS the real session (its client died first) instead of
        # hiding every app-server row unconditionally. Ephemeral — never in --json.
        managed_dir = getattr(sess, "managed_dir", None)
        sess._managed_client_present = bool(
            clients.get(os.path.normpath(managed_dir))
        ) if managed_dir else False


def share_managed_registry_state(sessions):
    """Copy tier-1 session_registry state from a managed app-server to its TUI client.

    B-2c: the gateway (B-2b) is the only observer of thread lifecycle, and it can only
    write the app-server's registry record (it never learns the client's pid). The
    client is the visible row, so its `session_id`/`status` must come from here. This
    mirrors `share_managed_tags`' exact managed_dir pairing and guards — same bucket
    build, same "exactly one server, exactly one client" gate, same "never overwrite
    a row that already has a value" rule — but the copy direction is the OPPOSITE of
    tags (server → client, not client → server), which is why it is a separate
    function rather than folded into `share_managed_tags`.
    """
    servers, clients = {}, {}
    for sess in sessions:
        if getattr(sess, "harness", None) != "codex":
            continue
        managed_dir = getattr(sess, "managed_dir", None)
        if not managed_dir:
            continue
        key = os.path.normpath(managed_dir)
        bucket = servers if getattr(sess, "app_server", False) else clients
        bucket.setdefault(key, []).append(sess)
    for key, owners in servers.items():
        peers = clients.get(key, [])
        if len(owners) != 1 or len(peers) != 1:
            continue
        server, client = owners[0], peers[0]
        session_id = getattr(server, "session_id", None)
        if session_id and not getattr(client, "session_id", None):
            client.session_id = session_id
        status = getattr(server, "status", None)
        if status and not getattr(client, "status", None):
            client.status = status
            server_updated = getattr(server, "updated_at", None)
            if server_updated is not None:
                client.updated_at = server_updated
        if (getattr(server, "session_id", None) == getattr(client, "session_id", None)
                and getattr(server, "attempt_id", None) == getattr(client, "attempt_id", None)
                and not getattr(client, "shell_jobs", None) and getattr(server, "shell_jobs", None)):
            client.shell_jobs = list(server.shell_jobs)
            client.shell_job_observation = getattr(server, "shell_job_observation", None)


def prepare_tick(sessions, *, wait_timeout=0.0):
    """Reserve only process-owned rollouts before enriching Codex rows.

    A TUI without a rollout fd gets a session id only from the pane's
    process_identity check, which uses the daemon loader origin. Rollout timing
    and same-cwd proximity do not assign a thread.
    """
    home = os.path.abspath(_home())
    eligible = any(
        getattr(sess, "harness", None) == "codex" and getattr(sess, "cwd", None)
        for sess in sessions
    )
    paths, claimed = process_rollouts(sessions, home)
    # Managed Codex: move each app-server's rollout onto its exact TUI client row.
    _transfer_managed_rollouts(sessions, paths)
    # Herdr's current pane session can move while the process keeps an older
    # rollout open (for example, after /new or resume). Re-read direct TUI pane
    # identities even when process_rollouts already selected a path, then replace
    # that path only when the exact current pane identity is unambiguous.
    direct_tuis = [s for s in sessions
                   if getattr(s, "harness", None) == "codex"
                   and not getattr(s, "app_server", False)
                   and not getattr(s, "managed_dir", None)
                   and not getattr(s, "is_child", False)
                   and getattr(s, "cwd", None)]
    pane_ids = {}
    if direct_tuis:
        from . import herdr, procscan
        pane_ids = herdr.codex_pane_sessions(
            direct_tuis, force_current=True, wait_timeout=wait_timeout)
        for sess in direct_tuis:
            sid = pane_ids.get(sess.pid)
            if not sid or _sid("rollout-x-%s.jsonl" % sid) != sid:
                continue
            old_path = paths.get(sess.pid)
            old_sid = _sid(old_path) if old_path else None
            if old_sid != sid and old_path:
                paths.pop(sess.pid, None)
                if old_sid and not any(_sid(path) == old_sid for path in paths.values()):
                    claimed.discard(old_sid)
            if sid in claimed and old_sid != sid:
                # A different live row already owns this thread. Drop the old
                # path and leave this PID unresolved instead of showing either.
                continue
            sess._herdr_session_id = sid
            claimed.add(sid)
            runtime_home = procscan.read_environ(sess.pid).get("CODEX_HOME")
            homes = (home, runtime_home) if runtime_home else (home,)
            path = exact_rollout_for_session_id(sid, homes=homes)
            if path:
                paths[sess.pid] = path
    _PROC_PATHS.clear()
    _PROC_PATHS.update(paths)
    # Exact parents evidenced this tick (pre-existing, registry, proc-owned
    # rollout, herdr pane): the subagent index build reads lifecycle details
    # only for their edges. Heuristic fallback attributions are not exact, so
    # they stay out of the upfront scope; enrich widens to the full build on
    # demand when such a session id needs a lookup, keeping output identical.
    exact_parent_ids = set()
    for sess in sessions:
        if getattr(sess, "harness", None) != "codex":
            continue
        sid = getattr(sess, "session_id", None)
        if isinstance(sid, str) and sid:
            exact_parent_ids.add(sid)
        try:
            record = session_registry.read("codex", sess.pid)
        except Exception:
            record = None
        if record and record.get("kind") != "codex-tui":
            sid = record.get("sessionId")
            if isinstance(sid, str) and sid:
                exact_parent_ids.add(sid)
    for path in paths.values():
        sid = _sid(path)
        if sid:
            exact_parent_ids.add(sid)
    for sid in pane_ids.values():
        if isinstance(sid, str) and sid:
            exact_parent_ids.add(sid)
    tick = _CodexTick(default_home=home, proc_paths=dict(paths), subagents_by_home={},
                      subagents_parent_ids=frozenset(exact_parent_ids))
    homes = {home} if eligible else set()
    homes.update(
        rollout_home for rollout_home in (_rollout_home(path) for path in paths.values())
        if rollout_home
    )
    for runtime_home in sorted(homes):
        _tick_subagents(tick, runtime_home, exact_parent_ids)
    return tick


def _tail_token_count(path, chunk=65536):
    """Last '"token_count"' line within the file's trailing `chunk` bytes."""
    try:
        sz = os.path.getsize(path)
        start = max(0, sz - chunk)
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]                           # drop the partial first line
    last = None
    for ln in lines:
        if '"token_count"' in ln:
            last = ln
    return last


def _latest_user_message_offset(path, chunk=_SUMMARY_BOUNDARY_SCAN):
    """Return the byte offset of the newest durable Codex user message.

    UserPromptSubmit precedes rollout persistence.  A sidecar whose cursor has
    not advanced beyond this boundary describes the preceding turn and must not
    be rendered as the current NOW line.
    """
    try:
        size = os.path.getsize(path)
        start = max(0, size - max(1, int(chunk)))
        with open(path, "rb") as handle:
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    start += len(handle.readline())
                else:
                    handle.seek(start)
            else:
                handle.seek(0)
            offset = start
            latest = None
            for raw in handle:
                row_offset = offset
                offset += len(raw)
                try:
                    row = json.loads(raw.decode("utf-8", "replace"))
                except (UnicodeDecodeError, ValueError):
                    continue
                payload = row.get("payload") if isinstance(row, dict) else None
                if (row.get("type") == "response_item"
                        and isinstance(payload, dict)
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"):
                    latest = row_offset
            return latest
    except (OSError, TypeError, ValueError):
        return None


_EXEC_CMD_RE = re.compile(r"""cmd:\s*['"]([^'"\n]+)""")


def _tail_open_tool_call(path, chunk=65536):
    """F-47: the newest `custom_tool_call` in the tail with no matching output → detail dict.

    Same tolerant tail-chunk idiom as `_tail_token_count`: one bounded read, partial first
    line dropped, any parse failure silently skipped. Pairing is by `call_id`, so a call
    whose output row already landed is closed and reports nothing. This adds DISPLAY detail
    only — Codex state stays on the exact `task_started`/`task_complete` lifecycle
    (prd.md:263), so a rollout that is mid-tool but lifecycle-terminal remains idle.

    Returns {'name', 'command'} (command None when the shell command is not extractable)
    or None.
    """
    try:
        size = os.path.getsize(path)
        start = max(0, size - chunk)
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = data.splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    calls, closed = [], set()
    for line in lines:
        if "custom_tool_call" not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        kind = payload.get("type")
        if kind == "custom_tool_call":
            calls.append((call_id, payload))
        elif kind == "custom_tool_call_output":
            closed.add(call_id)
    for call_id, payload in reversed(calls):
        if call_id in closed:
            continue
        name = payload.get("name")
        match = _EXEC_CMD_RE.search(payload.get("input") or "")
        command = match.group(1).strip().split()[0] if match and match.group(1).strip() else None
        if not name and not command:
            return None
        return {"name": name if isinstance(name, str) else None, "command": command}
    return None


def _event_timestamp(value, fallback):
    if not isinstance(value, str):
        return fallback
    try:
        return __import__("datetime").datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return fallback


def _tail_pending_request_user_input(path, chunk=65536):
    """Full-history, append-cached pending input independent of the gateway.

    The lifecycle cursor pairs blocking calls/outputs and exact async question
    replies. Async acceptance and normal turn completion keep the wait open; a later
    composer prompt clears it.
    A question older than the last read chunk remains visible without rescanning
    history on each Fleet tick. Partial lines and changed file identities fail
    closed under the same cursor contract as task lifecycle observation.
    """
    _latest_task_lifecycle(path, chunk=chunk)
    canonical_path = os.path.realpath(os.path.abspath(path))
    cursor = _LIFECYCLE_CACHE.get(canonical_path)
    if cursor is None or not cursor.pending_calls:
        return None
    call_id = next(reversed(cursor.pending_calls))
    since = cursor.pending_calls[call_id].since
    return {"call_id": call_id, "waiting_since": since if since is not None else cursor.mtime_ns / 1e9}


_ROLLOUT_VERSION_CACHE = {}              # path -> session_meta cli_version (immutable per rollout)


def _rollout_cli_version(path):
    """Codex runtime version recorded by the rollout's own session_meta, or None."""
    key = os.path.realpath(path)
    if key in _ROLLOUT_VERSION_CACHE:
        return _ROLLOUT_VERSION_CACHE[key]
    version = _rollout_meta(path).get("cli_version")
    version = version[:32] if isinstance(version, str) else None
    if version is not None:
        if len(_ROLLOUT_VERSION_CACHE) > 512:
            _ROLLOUT_VERSION_CACHE.pop(next(iter(_ROLLOUT_VERSION_CACHE)))
        _ROLLOUT_VERSION_CACHE[key] = version
    return version


def _cmdline_overrides_compaction(pid):
    """True when this process was launched with a compaction-affecting `-c` override."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as stream:
            raw = stream.read(65536)
    except OSError:
        return None
    return b"auto_compact" in raw or b"token_budget" in raw


def _apply_token_count(sess, line, path=None):
    # Shared parser keeps active context separate from cumulative session counters.
    telemetry = parse_codex_token_count(line, session_id=sess.session_id)
    sess.active_context_tokens = telemetry.active_context_tokens
    sess.context_window_tokens = telemetry.context_window_tokens
    sess.session_input_tokens = telemetry.session_input_tokens
    sess.session_cached_input_tokens = telemetry.session_cached_input_tokens
    sess.session_output_tokens = telemetry.session_output_tokens
    sess.session_reasoning_output_tokens = telemetry.session_reasoning_output_tokens
    sess.session_total_tokens = telemetry.session_total_tokens
    if telemetry.active_context_tokens is not None:
        # Legacy render compatibility: Codex ``tokens`` remains active context.
        sess.tokens = telemetry.active_context_tokens
    # token_count reports the thread's usable window (window * effective percent). The
    # applied auto-compaction trigger comes from the verified native consumer for this
    # rollout's own runtime version and model; anything unproven stays unknown.
    from ..context_policy import codex_applied_policy
    from ..token_budget import context_policy_used_pct
    policy = codex_applied_policy(
        runtime_version=_rollout_cli_version(path) if path else None,
        model=getattr(sess, "model", None),
        usable_window_tokens=telemetry.context_window_tokens,
        session_id=sess.session_id, home=(_rollout_home(path) if path else None),
        execution_started_at=_process_started_at(sess),
        execution_key=(sess.pid, sess.proc_start, sess.session_id),
        session_created_at=_session_created(_rollout_meta(path)) if path else None,
        cli_override_present=_cmdline_overrides_compaction(getattr(sess, "pid", None)))
    sess.context_policy = policy
    sess.context_display_window_tokens = (
        policy["applied_trigger_tokens"] if policy["state"] == "applied" else None)
    sess.ctx_pct = context_policy_used_pct(telemetry.active_context_tokens, policy)
    try:
        p = json.loads(line).get("payload") or {}
    except Exception:
        return
    p5, p7, windows = _rates_from_payload(p)
    if p5 is not None:
        sess.rl_5h = p5
    if p7 is not None:
        sess.rl_7d = p7
    if windows:
        sess.rl_windows = windows


def _duration_label(seconds):
    """Human label for a Codex limit_window_seconds value.

    Incident 2026-07-13: Codex primary_window changed to 604800 seconds while fleet still
    hard-labeled primary as 5h. Known durations get stable labels; unknown durations are
    shown as their actual duration instead of being forced into the old 5h/7d schema.
    """
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    seconds = int(seconds)
    known = {
        300 * 60: "5h",
        24 * 60 * 60: "24h",
        7 * 24 * 60 * 60: "7d",
        30 * 24 * 60 * 60: "30d",
    }
    if seconds in known:
        return known[seconds]
    if seconds % (7 * 24 * 60 * 60) == 0:
        return "%dw" % (seconds // (7 * 24 * 60 * 60))
    if seconds % (24 * 60 * 60) == 0:
        return "%dd" % (seconds // (24 * 60 * 60))
    if seconds % 3600 == 0:
        return "%dh" % (seconds // 3600)
    if seconds % 60 == 0:
        return "%dm" % (seconds // 60)
    return "%ds" % seconds


def _duration_label_from_minutes(minutes):
    """Same label table as `_duration_label`, from a `window_minutes` payload field.

    B-6/1: a live Codex payload can carry `window_minutes` (e.g. 10080 = 7 days)
    instead of `limit_window_seconds`; without this, `_window_from_limit` fell
    through to the legacy 5h/7d guess and mislabeled a 7-day window as "5h".
    """
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        return None
    return _duration_label(minutes * 60)


def _legacy_window_key(label):
    if label == "5h":
        return "rl_5h"
    if label == "7d":
        return "rl_7d"
    return None


def _window_from_limit(name, data, legacy_label):
    d = data or {}
    v = d.get("used_percent")
    if not isinstance(v, (int, float)):
        return None
    label = (_duration_label(d.get("limit_window_seconds"))
             or _duration_label_from_minutes(d.get("window_minutes"))
             or legacy_label)
    rs = d.get("reset_at")
    if not isinstance(rs, (int, float)):
        rs = d.get("resets_at")
    reset = float(rs) if isinstance(rs, (int, float)) else None
    return {"source": name, "label": label, "pct": round(v), "reset": reset}


def _rates_from_payload(p):
    """(rl_5h, rl_7d, windows) from a token_count payload, EXPIRY-AWARE.

    Legacy rollout payloads had primary/secondary windows that meant 5h/7d. Newer Codex
    payloads may include `limit_window_seconds`; that duration is now the label source.
    """
    rl = p.get("rate_limits") or {}

    windows = []
    legacy = {"rl_5h": None, "rl_7d": None}
    for source, fallback in (("primary", "5h"), ("secondary", "7d")):
        win = _window_from_limit(source, rl.get(source), fallback)
        if not win:
            continue
        if isinstance(win.get("reset"), (int, float)) and win["reset"] < time.time():
            win["pct"] = 0
        windows.append([win["label"], win["pct"], win.get("reset")])
        key = _legacy_window_key(win["label"])
        if key:
            legacy[key] = win["pct"]
    return legacy["rl_5h"], legacy["rl_7d"], windows


_ACCT = {"ts": 0.0, "data": None}


def _api_usage():
    """LIVE account usage via the same endpoint the codex TUI itself uses (`/wham/usage`,
    bundle: account/usage/read) — read-only GET with the user's own OAuth token from auth.json.
    Rollout samples update only when a session is used, so an active probe is
    the reliable primary source. Return None on any failure
    (expired token, offline, schema change) — the rollout scan below remains the fallback."""
    return _api_usage_detail()[0]


def _api_usage_detail():
    """(payload, reason): the one HTTP client for the codex gauge, shared with
    utilities/harness-capacity.py. `reason` names why there is no payload
    (`no-auth`, `no-token`, `http-<code>`, `timeout`, `network:<Exc>`, `schema`)
    so a dispatch receipt can say *why* headroom is unknown (2026-09-16)."""
    try:
        with open(os.path.join(_home(), "auth.json")) as f:
            a = json.load(f)
        toks = a.get("tokens") or {}
        tok, acc = toks.get("access_token"), toks.get("account_id")
    except Exception:
        return None, "no-auth"
    if not tok:
        return None, "no-token"
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/wham/usage",
        headers={"Authorization": "Bearer " + tok,
                 "chatgpt-account-id": acc or "",
                 "User-Agent": "codex-cli"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            d = json.load(r)
    except urllib.error.HTTPError as exc:
        return None, "http-%s" % exc.code
    except (socket.timeout, TimeoutError):
        return None, "timeout"
    except Exception as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return None, "timeout"
        return None, "network:%s" % type(exc).__name__
    rl = (d if isinstance(d, dict) else {}).get("rate_limit") or {}

    def rp(k, fallback):
        w = rl.get(k) or {}
        return _window_from_limit(k, w, fallback)

    raw_windows = [rp("primary_window", "5h"), rp("secondary_window", "7d")]
    windows = [[w["label"], w["pct"], w.get("reset")] for w in raw_windows if w]
    if not windows:
        return None, "schema"
    p5 = p7 = rs5 = rs7 = None
    for label, pct, reset in windows:
        if label == "5h":
            p5, rs5 = pct, reset
        elif label == "7d":
            p7, rs7 = pct, reset
    return {"rl_5h": p5, "rl_7d": p7, "rs_5h": rs5, "rs_7d": rs7, "windows": windows}, "live"


def account_usage():
    """Account-level usage: live API first (see _api_usage), then the NEWEST on-disk
    rollout carrying rate_limits (expiry rule applied) as offline fallback. TTL-cached 60s."""
    now = time.time()
    if now - _ACCT["ts"] <= 60.0:
        return _ACCT["data"]
    _ACCT["ts"] = now
    _ACCT["data"] = _api_usage()
    if _ACCT["data"] is not None:
        return _ACCT["data"]
    files = []
    root = os.path.join(_home(), "sessions")
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.endswith(".jsonl"):
                p = os.path.join(dirpath, n)
                try:
                    files.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    for _mt, path in sorted(files, reverse=True)[:12]:   # newest first, first rate hit wins
        line = _tail_token_count(path)
        if not line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except Exception:
            continue
        if payload.get("rate_limits"):
            p5, p7, windows = _rates_from_payload(payload)
            if windows:
                windows = [[lbl, pct, rs] for lbl, pct, rs in (windows or [])]
                _ACCT["data"] = {"rl_5h": p5, "rl_7d": p7, "rs_5h": None, "rs_7d": None,
                                  "windows": windows}   # rollout fallback: no reliable reset time
                break
    return _ACCT["data"]


def enrich(sess, tick=None):
    home = tick.default_home if tick is not None else _home()
    # Keep exact managed/headless registry evidence. A direct shared-daemon TUI
    # can only get its thread from the current loader resolver, never a stale
    # launcher record left by an earlier selection.
    try:
        rec = session_registry.read("codex", sess.pid)
        direct_tui = (not getattr(sess, "app_server", False)
                      and not getattr(sess, "managed_dir", None)
                      and getattr(sess, "_invocation_mode", "unknown") in {"session", "unknown"})
        if rec and rec.get("kind") != "codex-tui" and not direct_tui:
            session_registry.apply_to_session(sess, rec, "codex")
    except Exception:
        pass
    if getattr(sess, "_herdr_session_id", None):
        sess.session_id = sess._herdr_session_id
    if not sess.cwd:
        return
    if tick is None:
        path = _PROC_PATHS.get(sess.pid) or _proc_rollout(sess.pid, sess.cwd, home)
    else:
        path = tick.proc_paths.get(sess.pid)
    herdr_sid = getattr(sess, "_herdr_session_id", None)
    if path and herdr_sid and _sid(path) != herdr_sid:
        path = None                      # the pane selected another thread; never borrow this one's
    if not path:
        sess.shell_jobs = []
        sess.shell_job_observation = {
            "state": "unavailable",
            "reason": "exact-rollout-unavailable" if sess.session_id else "exact-session-id-unresolved",
        }
        if sess.session_id:
            from fleet.session_handle import resolve_tag
            sess.session_tag = resolve_tag("codex", sess.session_id)
        return                           # no rollout-specific telemetry for this session
    rollout_model, rollout_effort = _rollout_model_effort(path)
    if rollout_model:
        sess.model = rollout_model
    if rollout_effort:
        sess.effort = rollout_effort
    # app-server is the session process in this client-server Codex version. Treating it as a
    # companion once cleared mtime and broke working-state detection. Its /proc/cwd matches the
    # project cwd, so cwd-matched
    # A UUID comes only from an owned fd or a one-candidate fallback; one newest
    # cwd rollout must never be stamped on every live TUI sharing the repository.
    # B-3: a tier-1 session_registry sessionId (set above) outranks the rollout's —
    # preserve it instead of unconditionally overwriting with the rollout-derived id.
    sess.session_id = sess.session_id or _sid(path)
    if sess.session_id == _sid(path):
        source_key = ("codex", os.path.abspath(path), sess.session_id, sess.attempt_id)
        if getattr(sess, "_fast_first", False):
            sess.shell_jobs = []
            sess.shell_job_observation = {"state": "unobserved",
                                         "reason": "detail-refresh-deferred"}
        else:
            jobs = shell_jobs.read_codex_jobs(
                path, session_id=sess.session_id, attempt_id=sess.attempt_id)
            sess.shell_jobs = shell_jobs.observe_jobs(
                jobs, parent_pid=sess.pid, parent_start=sess.proc_start,
                attempt_id=sess.attempt_id, tree=getattr(sess, "_proc_tree", None),
                kids=getattr(sess, "_proc_kids", None))
            sess.shell_job_observation = shell_jobs.source_status(path, source_key)
    else:
        sess.shell_jobs = []
        sess.shell_job_observation = {"state": "unavailable", "reason": "rollout-session-mismatch"}
    if sess.session_id == _sid(path):
        from .. import session_cwd
        session_cwd.observe(sess, session_cwd.jsonl_cwd(path, "codex", sess.session_id), sess.session_id)
    sess._transcript_path = path                 # ephemeral: live title scheduler, not --json
    sess._refresh_source = {"kind": "transcript", "harness": "codex",
                            "session_id": sess.session_id, "path": path,
                            "cursor_kind": "byte-offset-v1"}
    if sess.session_id:
        from fleet import titles
        # Read only a confirmed shared assignment; normal collection seeds new tags.
        from fleet.session_handle import resolve_tag
        sess.session_tag = resolve_tag("codex", sess.session_id)
        sidecar_title = _displayable_title(
            titles.last_title(sess.session_id, harness="codex")
        )
        sess.title = sidecar_title
        # Codex thread_name also carries automatic first-message titles and has
        # no user/derived marker. Only the explicit Hearting name registry can
        # override a Fleet subject; automatic native titles must not mask it.
        try:
            from fleet.session_handle import resolve_display_inputs
            sess.runtime_name = resolve_display_inputs(
                "codex", sess.session_id).get("runtime_name")
        except Exception:
            sess.runtime_name = None
        sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
            sess.session_id, harness="codex",
            after_offset=_latest_user_message_offset(path),
        )
        runtime_home = _rollout_home(path) or home
        subagent_index = (
            _subagents_for_session(tick, runtime_home, sess.session_id)
            if tick is not None else _thread_subagents(runtime_home)
        )
        if subagent_index is not None:
            sess.subagents = subagent_index.get(sess.session_id, [])
    # Exact-attempt fallback (SD-95): a registered dispatch session has no
    # statusline producer for its runtime sid; the dispatch summary owner
    # writes under the attempt sid. attempt_id is exact env/registry identity.
    if not getattr(sess, "summary", None) or not getattr(sess, "title", None):
        from fleet import titles
        attempt_sid = titles.attempt_sid(getattr(sess, "attempt_id", None))
        if attempt_sid:
            if not sess.title:
                sess.title = titles.last_title(attempt_sid, harness="codex")
            if not sess.summary:
                sess.summary, sess.summary_ts = titles.fresh_summary_with_ts(
                    attempt_sid, harness="codex")
    try:
        sess.mtime = os.path.getmtime(path)
    except OSError:
        sess.mtime = None
    if sess.ctx_pct is not None:
        try:
            st = os.stat(path)
            sequence = (st.st_mtime_ns, st.st_size)
            sess._context_evidence = ContextEvidence(
                used_pct=sess.ctx_pct, source="codex-rollout",
                sequence=sequence, source_head_sequence=sequence,
                observed_at=st.st_mtime, fresh_until=st.st_mtime + 900)
        except OSError:
            pass
    lifecycle = _latest_task_lifecycle(path)
    sess.task_lifecycle = lifecycle[0] if lifecycle else None
    # Rollout mtime is exact post-marker activity for sidecar invalidation. The
    # structured pending call itself remains authoritative until its paired output.
    sess._interaction_activity = sess.mtime
    pending_input = _tail_pending_request_user_input(path)
    if pending_input:
        sess.interaction_state = {
            "kind": "decision",
            "source": "codex-rollout",
            "waiting_since": pending_input["waiting_since"],
        }
    # F-47 display detail only — never a state input (see _tail_open_tool_call).
    if sess.task_lifecycle == "task_started":
        sess.exec_tool = _tail_open_tool_call(path)
    tc = _tail_token_count(path)
    if tc:
        _apply_token_count(sess, tc, path)
