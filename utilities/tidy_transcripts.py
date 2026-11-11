#!/usr/bin/env python3
"""Read the three harnesses' conversation records for session-tidy.

What this adds over ``tools/fleet/refresh_title.read_delta`` /
``read_opencode_delta`` (whose text parsers it reuses, unmodified):

* it reads a **bounded chunk of whole rows** after a cursor and returns the cursor
  of the last row it really consumed.  ``read_delta`` keeps only the last N
  characters of a long delta, so advancing a cursor from it would silently lose the
  front of a long conversation;
* it extracts the **question tool calls with the user's answer text** (Claude
  ``AskUserQuestion``, Codex ``request_user_input``, OpenCode ``question``), one
  record per question;
* it finds the conversation records of the same seat over the last three days;
* it keeps the per-session watermark: the **ranges still unread** (``pending``) inside
  the snapshot it last saw.  ``read_pending`` hands out the *newest* unread range's
  last whole rows first (so a long record's recent decisions are never starved by its
  old front), and ``mark_applied`` removes only the range a successful apply covered.
  A new append becomes its own range; an old unread front is never overwritten by a
  newer end-of-file.

Position meaning: a byte offset into the JSONL file (Claude, Codex), or for OpenCode
the ``part`` rowid (V1) or the ``session_message`` seq (OpenCode 2), both read through
``opencode_runtime.read_session_parts``; a range is half-open ``[from, to)``.  A row that could not be read
whole (a half-written last line, an unanswered question still open) stays unread.

Public functions (all return plain dicts / dataclasses, never raise for a missing
or unreadable record):

    locate_transcript(harness, sid, hint=None) -> Path | None
    read_chunk(harness, source, cursor=0, sid=None, limit_bytes=DEFAULT_CHUNK_BYTES) -> Chunk
    read_pending(harness, sid, source, limit_bytes=...) -> Chunk        # newest unread range, tail first
    mark_applied(harness, sid, chunk) -> dict                           # drop that range from "unread"
    read_watermark(harness, sid) -> dict            {"cursor": int, "pending": [[from, to], ...], ...}
    write_watermark(harness, sid, cursor, **meta) -> dict               # legacy: "read up to cursor"
    describe_coverage(chunk) -> str                                     # "byte 2,880,000–3,145,728 / 전체 3,145,728"
    select_recent_sessions(seat, cwd=None, now=None, days=3) -> list[dict]
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from typing import Iterator, Optional

_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent / "tools"
for _path in (str(_HERE), str(_TOOLS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import session_tidy  # noqa: E402
from session_tidy import iso_utc, now_epoch, read_json, atomic_write_json, ensure_dir, state_root  # noqa: E402
from fleet import refresh_title as _rt  # noqa: E402  (text parsers reused, file not modified)
import session_summary_trigger as _sst  # noqa: E402

HARNESSES = session_tidy.HARNESSES
DEFAULT_CHUNK_BYTES = 256 * 1024
MAX_ROW_BYTES = 8 * 1024 * 1024
COMPACT_SCAN_BYTES = 1024 * 1024
COMPACT_RECENT_BYTES = 8 * 1024
RECENT_DAYS = 3
MAX_DISCOVER_FILES = 300
HEAD_SCAN_BYTES = 64 * 1024
OPEN_QUESTION_STALE_SEC = 6 * 3600


@dataclasses.dataclass
class Chunk:
    """One bounded read.  ``cursor_to`` is where the next read must start."""

    harness: str
    source: str
    cursor_from: int
    cursor_to: int
    eof: bool = True
    rows: int = 0
    text: str = ""
    choices: list = dataclasses.field(default_factory=list)
    skipped_oversize: int = 0
    blocked: str = ""      # "open-question": stopped before an unanswered question
    error: str = ""
    unit: str = "byte"     # "byte" (Claude, Codex) | "rowid" (OpenCode V1) | "seq" (OpenCode 2)
    total: int = 0         # end of the snapshot this chunk was cut from
    pending_after: list = dataclasses.field(default_factory=list)   # ranges still unread once this one is applied
    plan: dict = dataclasses.field(default_factory=dict)            # what ``mark_applied`` needs

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Where the records are
# ---------------------------------------------------------------------------

def claude_projects_dir(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base).expanduser() / "projects"


def codex_sessions_dir(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def opencode_db_path() -> Path:
    return _sst._opencode_db()


OPENCODE_READ_TRIES = 4        # bounded recovery from transient SQLite access failures


@contextlib.contextmanager
def _opencode_view(db_path: Path) -> Iterator[sqlite3.Connection]:
    """One native read-only view for the chunk, including rows still in the WAL.

    SQLite holds a consistent snapshot across these queries while other windows
    append. Copying the live database and WAL separately instead refused every
    copy whose file signatures changed during the copy, starving a busy session.
    """
    con = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro&cache=private", uri=True, timeout=1)
    try:
        con.execute("BEGIN")
        yield con
    finally:
        con.close()


def _read_error(exc: Exception) -> str:
    detail = " ".join(str(exc).split())[:180]
    return f"unreadable: {exc.__class__.__name__}" + (f": {detail}" if detail else "")


def _opencode_retry(read, pause: float = 0.5) -> "Chunk":
    """Retry transient access failures a bounded number of times; retain the last cause."""
    chunk = read()
    for _ in range(OPENCODE_READ_TRIES - 1):
        if not chunk.error:
            break
        time.sleep(pause)
        chunk = read()
    return chunk


def locate_transcript(harness: str, sid: str, hint: Optional[str] = None) -> Optional[Path]:
    """The record for one session: a JSONL file, or the OpenCode database."""
    if hint:
        path = Path(hint).expanduser()
        if path.is_file():
            return path
    if not sid:
        return None
    if harness == "claude":
        root = claude_projects_dir()
        if root.is_dir():
            for path in root.glob(f"*/{sid}.jsonl"):
                return path
        return None
    if harness == "codex":
        return _sst._codex_transcript(sid)
    if harness == "opencode":
        db = opencode_db_path()
        return db if db.is_file() else None
    return None


def _compact_text(row: dict, harness: str) -> str:
    """Dialogue text only; tool payloads never become card context."""
    if harness == "claude":
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        values = [block.get("text", "") for block in blocks
                  if isinstance(block, dict) and block.get("type") == "text"]
        if isinstance(content, str):
            values = [content]
        return "\n".join(v for v in values if isinstance(v, str)).strip()
    return ""


def _claude_compact_observation(sid: str, source: Path, anchor: dict) -> dict:
    """Read only a bounded tail of one exact Claude transcript; no watermark is consulted."""
    if source.name != f"{sid}.jsonl":
        return {"state": "unknown", "reason": "transcript-session-mismatch", "source": str(source)}
    try:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or source.is_symlink():
            return {"state": "unknown", "reason": "transcript-not-regular", "source": str(source)}
        expected_identity = str(anchor.get("transcript_identity") or "")
        if expected_identity and expected_identity != f"{info.st_dev}:{info.st_ino}":
            return {"state": "unknown", "reason": "transcript-identity-changed", "source": str(source)}
        size = info.st_size
        boundary = anchor.get("boundary_offset")
        if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0 or boundary > size:
            return {"state": "unknown", "reason": "compact-boundary-unknown", "source": str(source)}
        start = max(0, size - COMPACT_SCAN_BYTES)
        boundary_raw = b""
        boundary_start = max(0, boundary - COMPACT_SCAN_BYTES + 1)
        with source.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()       # the first row may start before the bounded window
                start = handle.tell()
            raw = handle.read()
            if start > boundary:
                if boundary_start:
                    handle.seek(boundary_start - 1)
                    boundary_prefix = handle.read(1)
                else:
                    boundary_prefix = b""
                handle.seek(boundary_start)
                boundary_raw = handle.read(boundary - boundary_start)
            after = os.fstat(handle.fileno())
        current = source.lstat()
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if (source.is_symlink()
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != identity):
            return {"state": "unknown", "reason": "transcript-changed-during-read", "source": str(source)}
    except OSError as exc:
        return {"state": "unknown", "reason": f"transcript-read-{type(exc).__name__}", "source": str(source)}
    if raw and not raw.endswith(b"\n"):
        tail = raw.rsplit(b"\n", 1)[-1]
        try:
            json.loads(tail)
        except (UnicodeDecodeError, ValueError, TypeError):
            return {"state": "unknown", "reason": "partial-latest-row", "source": str(source)}
    rows, offset = [], start
    for line in raw.splitlines(keepends=True):
        payload = line.rstrip(b"\r\n")
        if not payload:
            offset += len(line)
            continue
        try:
            row = json.loads(payload)
        except (UnicodeDecodeError, ValueError, TypeError):
            return {"state": "unknown", "reason": "malformed-transcript-row", "source": str(source)}
        if not isinstance(row, dict):
            return {"state": "unknown", "reason": "malformed-transcript-row", "source": str(source)}
        rows.append((offset, row))
        offset += len(line)
    main_rows = [(pos, row) for pos, row in rows
                 if row.get("type") in ("user", "assistant")
                 and row.get("isSidechain") is not True and row.get("isMeta") is not True
                 and row.get("isCompactSummary") is not True]
    boundary_rows = []
    boundary_partial = False
    if boundary_raw:
        boundary_offset = boundary_start
        boundary_lines = boundary_raw.splitlines(keepends=True)
        if boundary_start:
            # The preceding byte tells whether this bounded window starts on a row boundary.
            if boundary_prefix != b"\n" and boundary_lines:
                boundary_offset += len(boundary_lines.pop(0))
        for line in boundary_lines:
            if not line.endswith(b"\n"):
                break
            payload = line.rstrip(b"\r\n")
            if payload:
                try:
                    row = json.loads(payload)
                except (UnicodeDecodeError, ValueError, TypeError):
                    row = None
                if isinstance(row, dict):
                    boundary_rows.append((boundary_offset, row))
                else:
                    boundary_partial = True
            boundary_offset += len(line)
        boundary_rows = [(pos, row) for pos, row in boundary_rows
                         if row.get("type") in ("user", "assistant")
                         and row.get("isSidechain") is not True and row.get("isMeta") is not True
                         and row.get("isCompactSummary") is not True]
    if not main_rows:
        return {"state": "unknown", "reason": "no-main-transcript-row", "source": str(source)}
    reply_rows = sorted(main_rows + boundary_rows, key=lambda item: item[0])
    _last_pos, latest = main_rows[-1]
    message = latest.get("message") if isinstance(latest.get("message"), dict) else {}
    terminal = latest.get("type") == "assistant" and message.get("stop_reason") == "end_turn"
    # If the exact boundary has fallen before this scan window, only the latest
    # terminal row remains provable; the omitted earlier tail is reported as partial.
    partial = start > boundary or boundary_partial
    recent_rows, seen_uuids = [], set()
    def add_recent(pos, row):
        uid = str(row.get("uuid") or f"offset:{pos}")
        if uid in seen_uuids:
            return
        seen_uuids.add(uid)
        text = _compact_text(row, "claude")
        if text:
            recent_rows.append(f"[{row['type']}] {text}")
    preserved = None
    boundary_row = None
    for _pos, row in reversed(rows):
        if _pos == boundary and row.get("type") == "system" and row.get("subtype") == "compact_boundary":
            boundary_row = row
            metadata = row.get("compactMetadata")
            if isinstance(metadata, dict):
                preserved = metadata
            break
    if boundary_row is None:
        for _pos, row in reversed(rows):
            if _pos > boundary:
                continue
            if row.get("isCompactSummary") is True:
                metadata = row.get("compactMetadata")
                segment = metadata.get("preservedSegment") if isinstance(metadata, dict) else None
                if isinstance(segment, dict):
                    preserved = {"preservedSegment": segment}
                break
    if preserved is None:
        partial = True
    if isinstance(preserved, dict):
        messages = preserved.get("preservedMessages") if isinstance(preserved.get("preservedMessages"), dict) else {}
        segment = preserved.get("preservedSegment") if isinstance(preserved.get("preservedSegment"), dict) else preserved
        explicit_uuids = messages.get("uuids") if isinstance(messages.get("uuids"), list) else []
        keep_uuids = {str(uid) for uid in explicit_uuids if uid}
        start_uuid = segment.get("headUuid") or segment.get("startUuid") or segment.get("start_uuid")
        end_uuid = segment.get("tailUuid") or segment.get("endUuid") or segment.get("end_uuid")
        by_uuid = {str(row.get("uuid")): (pos, row) for pos, row in reply_rows if row.get("uuid")}
        if keep_uuids:
            kept_rows = [(pos, row) for uid in explicit_uuids for pos, row in [by_uuid.get(str(uid), (None, None))]
                         if row is not None and row.get("type") in ("user", "assistant")
                         and row.get("isSidechain") is not True and row.get("isMeta") is not True
                         and row.get("isCompactSummary") is not True]
            if len({str(row.get("uuid")) for _pos, row in kept_rows}) != len(keep_uuids):
                partial = True
            for pos, row in kept_rows:
                add_recent(pos, row)
        elif start_uuid and end_uuid and start_uuid in by_uuid and end_uuid in by_uuid:
            lo = by_uuid[start_uuid][0]
            hi = by_uuid[end_uuid][0]
            def on_lineage(uid):
                seen = set()
                current = str(uid)
                for _ in range(256):
                    if current == str(start_uuid):
                        return True
                    if current in seen or current not in by_uuid:
                        return False
                    seen.add(current)
                    current = str(by_uuid[current][1].get("parentUuid") or "")
                    if not current:
                        return False
                return False

            if lo <= hi and on_lineage(end_uuid):
                for pos, row in main_rows:
                    if lo <= pos <= hi and on_lineage(row.get("uuid") or ""):
                        add_recent(pos, row)
            else:
                partial = True
        else:
            partial = True
    preserved_recent = "\n".join(recent_rows)
    for pos, row in main_rows:
        if pos < boundary:
            continue
        add_recent(pos, row)
    recent = "\n".join(recent_rows)
    recent = recent.encode("utf-8")[-COMPACT_RECENT_BYTES:].decode("utf-8", "ignore")
    post_rows = [(pos, row) for pos, row in main_rows if pos >= boundary]
    post_recent_rows, post_seen = [], set()
    for pos, row in post_rows:
        uid = str(row.get("uuid") or f"offset:{pos}")
        if uid in post_seen:
            continue
        post_seen.add(uid)
        text = _compact_text(row, "claude")
        if text:
            post_recent_rows.append(f"[{row['type']}] {text}")
    post_recent = "\n".join(post_recent_rows).encode("utf-8")[-COMPACT_RECENT_BYTES:].decode("utf-8", "ignore")
    reply_candidates = []
    preserved_reply_uuids = keep_uuids if isinstance(preserved, dict) else set()
    for pos, row in reply_rows:
        if pos >= boundary or (preserved_reply_uuids and str(row.get("uuid") or "") not in preserved_reply_uuids):
            continue
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        text = _compact_text(row, "claude")
        if row.get("type") == "assistant" and msg.get("stop_reason") == "end_turn" and text:
            reply_candidates.append((pos, row, text))
    if not reply_candidates:
        for pos, row in reply_rows:
            if pos >= boundary:
                continue
            msg = row.get("message") if isinstance(row.get("message"), dict) else {}
            text = _compact_text(row, "claude")
            if row.get("type") == "assistant" and msg.get("stop_reason") == "end_turn" and text:
                reply_candidates.append((pos, row, text))
    last_reply = None
    if reply_candidates:
        pos, row, text = reply_candidates[-1]
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        first_line = first_line.encode("utf-8")[:1024].decode("utf-8", "ignore")
        last_reply = {"uuid": str(row.get("uuid") or ""), "byte_offset": pos,
                      "timestamp": str(row.get("timestamp") or ""), "first_line": first_line}
    return {"state": "ready" if terminal else "pending", "reason": "" if terminal else "turn-not-ended",
            "source": str(source), "boundary": boundary, "recent": recent,
            "recent_preserved": preserved_recent, "recent_post": post_recent, "last_reply": last_reply,
            "recent_partial": bool(partial or not recent_rows), "latest_type": latest.get("type"),
            "latest_stop_reason": message.get("stop_reason")}


def _decode_json(value):
    try:
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _opencode_compact_observation(sid: str, source: Path, anchor: dict, *, native_idle: bool) -> dict:
    """Validate summary/compaction lineage and the latest completed main turn in one RO view."""
    summary_id = str(anchor.get("summary_id") or "")
    parent_id = str(anchor.get("parent_id") or "")
    tail_start_id = str(anchor.get("tail_start_id") or "")
    if not summary_id or not parent_id:
        return {"state": "unknown", "reason": "compact-anchor-missing", "source": str(source)}
    try:
        with _opencode_view(source) as con:
            message_rows = con.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created DESC LIMIT 64", (sid,)
            ).fetchall()
            messages = []
            for message_id, raw in reversed(message_rows):
                info = _decode_json(raw)
                if not isinstance(info, dict):
                    return {"state": "unknown", "reason": "malformed-opencode-message", "source": str(source)}
                parts_raw = con.execute("SELECT id, data FROM part WHERE session_id = ? AND message_id = ? ORDER BY rowid",
                                        (sid, message_id)).fetchall()
                parts = []
                for part_id, data in parts_raw:
                    part = _decode_json(data)
                    if not isinstance(part, dict):
                        return {"state": "unknown", "reason": "malformed-opencode-part", "source": str(source)}
                    part.setdefault("id", part_id)
                    parts.append(part)
                messages.append({"id": str(message_id), "info": info, "parts": parts})
    except (OSError, sqlite3.Error) as exc:
        return {"state": "unknown", "reason": f"opencode-read-{type(exc).__name__}", "source": str(source)}

    by_id = {row["id"]: row for row in messages}
    summary = by_id.get(summary_id)
    parent = by_id.get(parent_id)
    if not summary or not parent:
        return {"state": "unknown", "reason": "compact-anchor-not-found", "source": str(source)}
    info = summary["info"]
    if info.get("role") != "assistant" or info.get("summary") is not True or str(info.get("parentID") or "") != parent_id:
        return {"state": "unknown", "reason": "summary-parent-mismatch", "source": str(source)}
    compact_parts = [part for part in parent["parts"] if part.get("type") == "compaction"]
    part_anchor = str(anchor.get("compaction_part_id") or "")
    matching = [part for part in compact_parts
                if not part_anchor or str(part.get("id") or "") == part_anchor]
    if not any(part.get("auto") is True or (isinstance(part.get("data"), dict)
                                             and part["data"].get("auto") is True) for part in matching):
        return {"state": "unknown", "reason": "automatic-compaction-part-not-found", "source": str(source)}

    summary_index = messages.index(summary)
    start_index = summary_index
    tail_found = not tail_start_id
    if tail_start_id:
        for index, row in enumerate(messages):
            if row["id"] == tail_start_id:
                start_index, tail_found = index, True
                break
        if not tail_found and any(str(part.get("id") or "") == tail_start_id for part in matching):
            start_index, tail_found = messages.index(parent), True
        if not tail_found:
            # Preserve only the provable post-summary tail if the runtime's retained
            # boundary is absent from this bounded view.
            start_index = summary_index
    after = messages[summary_index + 1:]
    ordinary = [row for row in after if row["info"].get("summary") is not True]
    latest_user = next((row for row in reversed(ordinary) if row["info"].get("role") == "user"), None)
    latest_assistant = next((row for row in reversed(ordinary) if row["info"].get("role") == "assistant"), None)
    if (not native_idle or not latest_user or not latest_assistant
            or latest_user["info"].get("synthetic") is True
            or any(part.get("synthetic") is True for part in latest_user["parts"])
            or latest_assistant["info"].get("synthetic") is True
            or any(part.get("synthetic") is True for part in latest_assistant["parts"])
            or str(latest_assistant["info"].get("parentID") or "") != latest_user["id"]):
        ready = False
    else:
        finish = str(latest_assistant["info"].get("finish") or "")
        pending_tools = any(part.get("type") == "tool" and
                            (part.get("state") or {}).get("status") in ("pending", "running")
                            for part in latest_assistant["parts"])
        ready = bool(finish in ("stop", "end_turn", "complete")
                     and not latest_assistant["info"].get("error")
                     and not latest_assistant["info"].get("toolCalls") and not pending_tools)
    recent_rows = []
    for row in messages[start_index:]:
        if row["id"] != summary_id and row["info"].get("summary") is not True \
                and row["info"].get("synthetic") is not True:
            role = row["info"].get("role")
            if role in ("user", "assistant"):
                for part in row["parts"]:
                    if part.get("type") == "text" and not part.get("synthetic") and isinstance(part.get("text"), str):
                        recent_rows.append(f"[{role}] {part['text']}")
    recent = "\n".join(recent_rows)
    recent = recent.encode("utf-8")[-COMPACT_RECENT_BYTES:].decode("utf-8", "ignore")
    return {"state": "ready" if ready else "pending", "reason": "" if ready else "turn-or-idle-not-confirmed",
            "source": str(source), "recent": recent, "recent_partial": not tail_found,
            "latest_assistant": latest_assistant["id"] if latest_assistant else "",
            "latest_finish": (latest_assistant or {}).get("info", {}).get("finish", "")}


def compact_observation(harness: str, sid: str, source_hint: str, anchor: dict,
                        *, native_idle: bool = False) -> dict:
    """Read-only compact evidence and a bounded conversation tail; never changes tidy watermarks."""
    source = locate_transcript(harness, sid, source_hint or None)
    if source is None:
        return {"state": "unknown", "reason": "transcript-not-found", "source": source_hint or ""}
    if harness == "claude":
        return _claude_compact_observation(sid, source, anchor)
    if harness == "codex":
        return _codex_compact_observation(sid, source, anchor)
    if harness == "opencode":
        return _opencode_compact_observation(sid, source, anchor, native_idle=native_idle)
    return {"state": "unknown", "reason": "unsupported-harness", "source": str(source)}


def _codex_compact_observation(sid: str, source: Path, anchor: dict) -> dict:
    """Confirm this Codex compacted turn from a later exact-session task_complete row."""
    try:
        info = source.lstat()
        if source.is_symlink() or not stat.S_ISREG(info.st_mode):
            return {"state": "unknown", "reason": "unsafe-transcript", "source": str(source)}
        expected_identity = str(anchor.get("transcript_identity") or "")
        identity = f"{info.st_dev}:{info.st_ino}"
        if not expected_identity or expected_identity != identity:
            return {"state": "unknown", "reason": "transcript-identity-changed", "source": str(source)}
        boundary = anchor.get("boundary_offset")
        if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0 or info.st_size < boundary:
            return {"state": "unknown", "reason": "compact-boundary-invalid", "source": str(source)}
        with source.open("rb") as handle:
            head = handle.read(HEAD_SCAN_BYTES)
            handle.seek(max(boundary, info.st_size - COMPACT_SCAN_BYTES))
            start = handle.tell()
            tail = handle.read(COMPACT_SCAN_BYTES)
        meta = None
        for raw in head.splitlines():
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict) and row.get("type") == "session_meta":
                meta = row.get("payload") if isinstance(row.get("payload"), dict) else None
                break
        if (not isinstance(meta, dict) or str(meta.get("id") or meta.get("session_id") or "") != sid
                or (isinstance(meta.get("source"), dict) and meta["source"].get("subagent"))):
            return {"state": "unknown", "reason": "session-identity-changed", "source": str(source)}
        expected_turn = str(anchor.get("turn_id") or "")
        rows = tail.splitlines(keepends=True)
        if start > 0:
            with source.open("rb") as handle:
                handle.seek(start - 1)
                if handle.read(1) != b"\n":
                    rows = rows[1:]
        completed = None
        latest_input = -1
        offset = start
        for index, raw in enumerate(rows):
            line_start = offset
            offset += len(raw)
            try:
                row = json.loads(raw.rstrip(b"\r\n"))
            except (ValueError, UnicodeDecodeError):
                if index == len(rows) - 1:
                    return {"state": "pending", "reason": "transcript-incomplete", "source": str(source)}
                return {"state": "unknown", "reason": "malformed-transcript-row", "source": str(source)}
            if not isinstance(row, dict):
                return {"state": "unknown", "reason": "malformed-transcript-row", "source": str(source)}
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            row_type = str(row.get("type") or "")
            if row_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
                latest_input = line_start
            if row_type == "event_msg" and payload.get("type") in ("task_complete", "turn_completed"):
                turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
                if expected_turn and turn_id != expected_turn:
                    continue
                completed = {"turn_id": turn_id, "offset": line_start}
        if not completed:
            return {"state": "pending", "reason": "turn-not-ended", "source": str(source),
                    "boundary": boundary, "turn_id": expected_turn}
        if latest_input > int(completed["offset"]):
            return {"state": "pending", "reason": "new-input-after-turn", "source": str(source),
                    "boundary": boundary, "turn_id": completed["turn_id"]}
        return {"state": "ready", "reason": "", "source": str(source), "boundary": boundary,
                "completion_offset": completed["offset"], "turn_id": completed["turn_id"]}
    except OSError as exc:
        return {"state": "unknown", "reason": f"transcript-{type(exc).__name__}", "source": str(source)}


# ---------------------------------------------------------------------------
# Normalizing rows (text) and pulling out question tool records
# ---------------------------------------------------------------------------

def _text_of_jsonl_row(data, harness: str) -> tuple[str, str]:
    """``(role, text)`` for one Claude/Codex row, ("", "") when it carries no dialogue."""
    if not isinstance(data, dict):
        return "", ""
    if harness == "claude":
        if data.get("isSidechain") or data.get("isMeta"):
            return "", ""
        if data.get("type") not in ("user", "assistant"):
            return "", ""
        texts = _rt._claude_text(data)
        role = data["type"]
    else:
        texts = _rt._codex_text(data)
        payload = data.get("payload") or {}
        role = str(payload.get("role") or ("assistant" if data.get("type") == "item.completed" else ""))
    text = "\n".join(t.strip() for t in texts if isinstance(t, str) and t.strip())
    if role == "user" and harness == "codex" and _rt._codex_bootstrap_user_text(text):
        return "", ""
    return (role, text) if text else ("", "")


def _dialogue_line(role: str, text: str) -> str:
    return f"[{role}] {text}" if role else text


def _split_answer(answer: str, labels: list, multi: bool) -> list:
    """Chosen labels out of the joined answer text; a free-text answer stays whole."""
    answer = (answer or "").strip()
    if not answer:
        return []
    if answer in labels:
        return [answer]
    if not multi:
        return [answer]
    found, rest = [], answer
    while rest:
        for label in sorted(labels, key=len, reverse=True):
            if rest == label or rest.startswith(label + ", "):
                found.append(label)
                rest = rest[len(label):].lstrip(", ").strip()
                break
        else:
            return [answer]
    return found


def _option_list(question: dict) -> list:
    out = []
    for option in question.get("options") or []:
        if isinstance(option, dict) and option.get("label") is not None:
            out.append({"label": str(option.get("label")), "description": str(option.get("description") or "")})
    return out


def _choice_record(harness, call_id, asked_at, question, answers, answer_raw, note="", index=0) -> Optional[dict]:
    text = str(question.get("question") or "").strip()
    answers = [str(a).strip() for a in answers if str(a).strip()]
    if not text or not answers:
        return None
    return {
        "harness": harness,
        "call_id": call_id,
        "index": index,
        "asked_at": asked_at,
        "header": str(question.get("header") or ""),
        "question": text,
        "options": _option_list(question),
        "multi_select": bool(question.get("multiSelect") or question.get("multiple")),
        "answers": answers,
        "answer_raw": answer_raw,
        "note": note,
    }


_CLAUDE_PAIR_RE = re.compile(r'"([^"]*)"="([^"]*)"')


def _claude_choices(data, uses: dict) -> list:
    out = []
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return out
    stamp = str(data.get("timestamp") or "")
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
            questions = (block.get("input") or {}).get("questions")
            if isinstance(questions, list):
                uses[block.get("id")] = {"questions": questions, "asked_at": stamp}
        elif block.get("type") == "tool_result":
            tid = block.get("tool_use_id")
            result = data.get("toolUseResult")
            if isinstance(result, dict) and isinstance(result.get("answers"), dict) \
                    and isinstance(result.get("questions"), list):
                questions, answers = result["questions"], result["answers"]
                notes = result.get("annotations") if isinstance(result.get("annotations"), dict) else {}
            elif tid in uses and not block.get("is_error") and isinstance(block.get("content"), str):
                questions = uses[tid]["questions"]
                answers = {k: v for k, v in _CLAUDE_PAIR_RE.findall(block["content"])}
                notes = {}
            else:
                continue
            asked = (uses.get(tid) or {}).get("asked_at") or stamp
            for index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                key = str(question.get("question") or "")
                raw = answers.get(key, answers.get(key.strip()))
                if raw is None:
                    continue
                labels = [o["label"] for o in _option_list(question)]
                note = notes.get(key) or {}
                rec = _choice_record("claude", tid or "", asked, question,
                                     _split_answer(str(raw), labels, bool(question.get("multiSelect"))),
                                     str(raw), str(note.get("notes") or "") if isinstance(note, dict) else "", index)
                if rec:
                    out.append(rec)
    return out


def _codex_records(data, calls: dict, path: Optional[Path], before: int) -> list:
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or data.get("type") != "response_item":
        return []
    kind = payload.get("type")
    if kind == "function_call" and str(payload.get("name") or "").startswith("request_user_input"):
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except ValueError:
            return []
        if isinstance(args, dict) and isinstance(args.get("questions"), list):
            calls[payload.get("call_id")] = {"questions": args["questions"], "asked_at": str(data.get("timestamp") or "")}
        return []
    if kind != "function_call_output":
        return []
    call_id = payload.get("call_id")
    call = calls.get(call_id) or (_find_codex_call(path, call_id, before) if path else None)
    if not call:
        return []
    raw = payload.get("output")
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or not answers:
        return []
    out = []
    for index, question in enumerate(call["questions"]):
        if not isinstance(question, dict):
            continue
        value = answers.get(question.get("id"))
        if isinstance(value, dict):
            value = value.get("answers")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        rec = _choice_record("codex", str(call_id), call["asked_at"], question, value,
                             json.dumps(value, ensure_ascii=False), "", index)
        if rec:
            out.append(rec)
    return out


def _find_codex_call(path: Path, call_id, before: int) -> Optional[dict]:
    """The function_call an output row answers, when it sits in an earlier chunk."""
    if not call_id:
        return None
    needle = str(call_id).encode("utf-8")
    try:
        with open(path, "rb") as handle:
            position = 0
            for line in handle:
                position += len(line)
                if position > before:
                    break
                if needle in line and b"request_user_input" in line:
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    found: dict = {}
                    _codex_records(data, found, None, 0)
                    if call_id in found:
                        return found[call_id]
    except OSError:
        return None
    return None


def _opencode_choices(part: dict) -> list:
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    if part.get("tool") != "question" or state.get("status") != "completed":
        return []
    questions = (state.get("input") or {}).get("questions")
    answers = (state.get("metadata") or {}).get("answers")
    if not isinstance(questions, list) or not isinstance(answers, list):
        return []
    started = (state.get("time") or {}).get("start")
    asked = iso_utc(started / 1000) if isinstance(started, (int, float)) else ""
    out = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict) or index >= len(answers):
            continue
        value = answers[index]
        value = [value] if isinstance(value, str) else value
        if not isinstance(value, list):
            continue
        rec = _choice_record("opencode", str(part.get("callID") or ""), asked, question, value,
                             json.dumps(value, ensure_ascii=False), "", index)
        if rec:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Chunked readers
# ---------------------------------------------------------------------------

def _complete_rows(path: Path, cursor: int, limit: int) -> tuple[list, int, bool, int]:
    """Whole JSONL rows after ``cursor``: ``(rows, new_cursor, eof, skipped_oversize)``.

    Reads at most ``limit`` bytes (a single longer row is read whole, up to
    ``MAX_ROW_BYTES``) and cuts at the last newline, so ``new_cursor`` always sits
    on a row boundary.  A last row without a newline counts only when it parses
    (a half-written row stays for the next read); a row over ``MAX_ROW_BYTES`` is
    stepped over and counted, so one giant tool output cannot stall the cursor.
    """
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if cursor >= size:
            return [], cursor, True, 0
        handle.seek(cursor)
        buf = handle.read(limit)
        end = buf.rfind(b"\n")
        rows = [r for r in buf[:end].split(b"\n") if r.strip()] if end >= 0 else []
        new_cursor = cursor + end + 1 if end >= 0 else cursor
        tail = buf[end + 1:]
        skipped = 0
        if tail and end < 0 and cursor + len(buf) < size:
            # One row longer than the limit: keep reading until its newline.
            while b"\n" not in tail and len(tail) < MAX_ROW_BYTES:
                more = handle.read(min(1 << 20, MAX_ROW_BYTES - len(tail)))
                if not more:
                    break
                tail += more
            if b"\n" in tail:
                row = tail[:tail.index(b"\n") + 1]
                rows, new_cursor, tail = [row.rstrip(b"\n")], cursor + len(row), b""
            elif cursor + len(tail) < size:
                skip_to = cursor + len(tail)
                handle.seek(skip_to)
                while True:
                    more = handle.read(1 << 20)
                    if not more:
                        break
                    if b"\n" in more:
                        new_cursor, skipped, tail = skip_to + more.index(b"\n") + 1, 1, b""
                        break
                    skip_to += len(more)
        # ``tail`` (if still set) starts exactly at ``new_cursor``. It is the file's last
        # row without a newline when it reaches the end and parses as JSON.
        if tail.strip() and new_cursor + len(tail) >= size:
            try:
                json.loads(tail)
                rows.append(tail)
                new_cursor += len(tail)
            except ValueError:
                pass                        # half-written: leave it for the next read
        return rows, new_cursor, new_cursor >= size, skipped


def _digest_rows(harness: str, path: Path, lines: list, before: int, chunk: Chunk) -> None:
    """Fill ``chunk`` (rows, text, choices) from whole JSONL rows; ``before`` is where they start."""
    texts: list = []
    uses: dict = {}
    calls: dict = {}
    for raw in lines:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        chunk.rows += 1
        role, text = _text_of_jsonl_row(data, harness)
        if text:
            texts.append(_dialogue_line(role, text))
        if harness == "claude":
            chunk.choices.extend(_claude_choices(data, uses))
        else:
            chunk.choices.extend(_codex_records(data, calls, path, before))
    chunk.text = "\n".join(texts)


def _read_jsonl(harness: str, path: Path, cursor: int, limit: int) -> Chunk:
    chunk = Chunk(harness, str(path), cursor, cursor)
    try:
        lines, new_cursor, eof, skipped = _complete_rows(path, cursor, limit)
    except OSError as exc:
        chunk.error = f"unreadable: {exc.__class__.__name__}"
        return chunk
    _digest_rows(harness, path, lines, cursor, chunk)
    chunk.cursor_to, chunk.eof, chunk.skipped_oversize = new_cursor, eof, skipped
    return chunk


def _opencode_session(db_path: Path, session_id: str, after: int = 0, limit: Optional[int] = None,
                      sized: bool = True):
    """``(unit, rows, error)`` of one exact OpenCode session after ``after``.

    The shared generation-aware reader (``opencode_runtime.read_session_parts``)
    picks the V1 ``part`` rows or the OpenCode 2 ``session_message`` rows from
    one read-only snapshot; a session found in both is refused there. Each row
    becomes ``(cursor, updated_ms, size, role, parts, source)`` with V1-shaped parts.
    ``limit`` uses that reader's existing row budget (the oldest ``limit`` rows).
    With ``sized=False`` the size is left ``None`` for ``_row_size`` to fill on demand.
    """
    import opencode_runtime
    snapshot = opencode_runtime.read_session_parts(session_id, db_path=db_path, after_cursor=after, limit=limit)
    if snapshot.get("state") != "observed":
        reason = str(snapshot.get("reason") or "unknown")
        if reason == "exact-session-not-found":
            return "rowid", [], ""                      # nothing written for this session yet
        detail = " ".join(str(snapshot.get("detail") or "").split())[:180]
        return "rowid", [], f"unreadable: {reason}" + (f": {detail}" if detail else "")
    generation = snapshot.get("generation")
    rows = []
    for row in snapshot["parts"]:
        data, raw = row.get("data"), row.get("raw")
        source = raw if isinstance(raw, (str, bytes)) else data
        size = _row_size((None, None, None, None, None, source)) if sized else None
        role = str(row.get("role") or "")
        if generation == "v2":
            parts = _opencode_v2_parts(role, data)
        else:
            parts = [data] if isinstance(data, dict) else []
        rows.append((int(row["cursor"]), int(row.get("updated") or 0), size, role, parts, source))
    return ("seq" if generation == "v2" else "rowid"), rows, ""


def _row_size(row) -> int:
    """The budget size of one ``_opencode_session`` row: its native JSON length."""
    if row[2] is not None:
        return row[2]
    source = row[5]
    return len(source) if isinstance(source, (str, bytes)) else len(json.dumps(source, ensure_ascii=False))


OPENCODE_CHUNK_PAGE = 16          # first page of rows a bounded chunk asks the shared reader for
OPENCODE_CHUNK_PAGE_MAX = 512


def _opencode_v2_parts(role: str, data) -> list:
    """OpenCode 2 message rows as the V1 part shapes the readers below already parse."""
    if not isinstance(data, dict):
        return []
    if role == "user":
        return [{"type": "text", "text": data["text"]}] if isinstance(data.get("text"), str) else []
    if role != "assistant" or not isinstance(data.get("content"), list):
        return []
    parts = []
    for part in data["content"]:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool":
            state = dict(part.get("state")) if isinstance(part.get("state"), dict) else {}
            started = (part.get("time") or {}).get("created") if isinstance(part.get("time"), dict) else None
            if started is not None and not isinstance(state.get("time"), dict):
                state["time"] = {"start": started}
            part = {**part, "tool": part.get("name"), "callID": part.get("id"), "state": state}
        parts.append(part)
    return parts


def _open_question(part, updated_ms) -> bool:
    """A question still waiting for its answer (an old one stops blocking)."""
    return (isinstance(part, dict) and part.get("tool") == "question"
            and (part.get("state") or {}).get("status") in ("pending", "running", "streaming")
            and now_epoch() - float(updated_ms or 0) / 1000 < OPEN_QUESTION_STALE_SEC)


def _digest_opencode(rows, chunk: Chunk) -> None:
    """Fill ``chunk`` (rows, text, choices) from ``_opencode_session`` rows, oldest first."""
    texts = []
    for row in rows:
        role, parts = row[3], row[4]
        chunk.rows += 1
        for part in parts:
            chunk.choices.extend(_opencode_choices(part))
            if part.get("type") != "text" or part.get("synthetic") or part.get("ignored"):
                continue
            text = _rt._opencode_text(part).strip()
            if text:
                texts.append(_dialogue_line(role, text))
    chunk.text = "\n".join(texts)


def _read_opencode(db_path: Path, session_id: str, cursor: int, limit: int,
                   cursor_unit: Optional[list] = None) -> Chunk:
    chunk = Chunk("opencode", str(db_path), cursor, cursor)
    if not session_id:
        chunk.error = "no session id"
        return chunk
    # Read only the rows this budget can take: small pages through the shared reader's row
    # limit, doubling while the budget lasts, so a long session is not decoded per chunk.
    # A nonzero cursor means something only in the unit (generation) that issued it, so every
    # page of every attempt must stay in the unit the first page saw; ``cursor_unit`` carries it
    # across the retries of one chunk. Cursor 0 belongs to no unit and reads any one from the start.
    held = cursor_unit if cursor_unit is not None and cursor else []
    used, last, taken, exhausted, page = 0, int(cursor), [], True, OPENCODE_CHUNK_PAGE
    unit = held[0] if held else None
    while True:
        page_unit, rows, error = _opencode_session(db_path, session_id, last, page)
        if unit is not None and page_unit != unit:
            # The generation changed (an empty page included): the cursor no longer names this
            # session, so the read fails like an ambiguous one (only a cursor-0 retry reads the new one).
            error = "unreadable: session-generation-ambiguous"
        if error:
            chunk.error = error
            return chunk
        if unit is None:
            unit = page_unit
            held.append(unit)
        stopped = False
        for row in rows:
            if used and used + row[2] > limit:
                stopped = True
                break
            if any(_open_question(part, row[1]) for part in row[4]):
                chunk.blocked, stopped = "open-question", True
                break
            used += row[2]
            last = row[0]
            taken.append(row)
        if stopped:
            exhausted = False
            break
        if len(rows) < page:
            break
        page = min(page * 2, OPENCODE_CHUNK_PAGE_MAX)
    _digest_opencode(taken, chunk)
    chunk.cursor_to, chunk.eof, chunk.unit = last, exhausted, unit
    return chunk


def read_chunk(harness: str, source, cursor: int = 0, sid: Optional[str] = None,
               limit_bytes: int = DEFAULT_CHUNK_BYTES) -> Chunk:
    """A bounded read of whole rows after ``cursor``; ``cursor_to`` covers exactly what was read."""
    limit = max(1, int(limit_bytes))
    cursor = max(0, int(cursor or 0))
    if harness == "opencode":
        cursor_unit: list = []          # the unit the first page saw, kept for every retry
        return _opencode_retry(lambda: _read_opencode(Path(source), sid or "", cursor, limit, cursor_unit))
    if harness in ("claude", "codex"):
        return _read_jsonl(harness, Path(source), cursor, limit)
    return Chunk(harness, str(source), cursor, cursor, error="unknown harness")


# Current-turn observations for automatic change records. This reads no tidy watermark
# and writes nothing: the recorder owns its existing state and evidence lifecycle.
TURN_SCAN_BYTES = 2 * 1024 * 1024


def _turn_prompt(harness, row):
    message = row.get('message') or row.get('payload') or {}
    if not isinstance(message, dict):
        return False
    if harness == 'codex':
        return (row.get('type') == 'event_msg' and message.get('type') == 'user_message' or
                message.get('type') == 'message' and message.get('role') == 'user')
    content = message.get('content')
    return message.get('role') == 'user' and (isinstance(content, str) or
        isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'text' for b in content))


def turn_anchor(harness, sid, hint=None, *, at=None):
    """Locate this exact native session and remember its current prompt boundary.

    Called in the existing detached child, never in Stop. A bounded tail also
    includes tools that started before the start child got CPU. Missing native
    clues preserve the old Git record with unknown observations.
    """
    source = locate_transcript(harness, sid, hint)
    if source is None:
        return {'error': 'native source unavailable'}
    anchor = {'source': str(source), 'session_id': sid, 'harness': harness}
    if harness != 'opencode' and sid not in source.name:
        return {**anchor, 'error': 'native source session mismatch'}
    try:
        if harness == 'opencode':
            with _opencode_view(source) as con:
                if at is not None:
                    row = con.execute("SELECT MAX(rowid) FROM part WHERE session_id=? AND time_created<?",
                                      (sid, int(at * 1000))).fetchone()
                    anchor['cursor'] = int(row[0] or 0)
                    return anchor
                row = con.execute("SELECT MIN(p.rowid) FROM part p WHERE p.session_id=? AND p.message_id=("
                    "SELECT id FROM message WHERE session_id=? AND json_extract(data,'$.role')='user' "
                    "ORDER BY rowid DESC LIMIT 1)", (sid, sid)).fetchone()
                anchor['cursor'] = max(0, int(row[0] or 1) - 1)
        else:
            with source.open('rb') as stream:
                info = os.fstat(stream.fileno())
                anchor['identity'] = f'{info.st_dev}:{info.st_ino}'
                start = max(0, info.st_size - DEFAULT_CHUNK_BYTES)
                stream.seek(start)
                raw = stream.read(DEFAULT_CHUNK_BYTES)
            cursor = info.st_size
            offset = start
            for line in raw.splitlines(keepends=True):
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        if at is None and _turn_prompt(harness, row):
                            cursor = offset
                        elif at is not None:
                            from datetime import datetime
                            stamp = row.get('timestamp')
                            try:
                                stamp = datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
                            except (ValueError, TypeError):
                                stamp = 0
                            if stamp >= at and cursor == info.st_size:
                                cursor = offset
                except (ValueError, UnicodeError):
                    pass
                offset += len(line)
            anchor['cursor'] = cursor
            if at is not None and start and cursor == start:
                anchor['coverage'] = 'bounded start; earlier native rows unknown'
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        anchor['error'] = _read_error(exc)
    return anchor


def turn_observations(harness, sid, anchor, payload=None, *, until=None):
    """Bounded native facts, not a test verdict. Full stdout stays at its source."""
    from work_intent import redact_secrets

    payload = payload or {}
    result = {'commands': [], 'reply': '', 'terminal': 'stop-observed',
              'coverage': 'unknown', 'source': anchor.get('source', ''), 'error': anchor.get('error', '')}
    def text(value, cap=1200):
        return redact_secrets(value if isinstance(value, str) else '')[:cap]
    def command(name, key, args, ref):
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        label = args.get('cmd') or args.get('command') or ''
        item = {'tool': name, 'id': str(key), 'command': text(label, 240),
                'state': 'unknown', 'exit_codes': [], 'reference': ref}
        result['commands'].append(item)
        return item
    def output(item, value, *, failed=False):
        codes, running = [], False
        def visit(value):
            nonlocal running
            if isinstance(value, dict):
                code = value.get('exit_code')
                if isinstance(code, int) and not isinstance(code, bool):
                    codes.append(code)
                if isinstance(value.get('session_id'), int) and code is None:
                    running = True
                for key in ('structuredContent', 'value', 'result', 'results'):
                    if key in value:
                        visit(value[key])
                # content/text/stdout/output are text carriers, never exit fields.
            elif isinstance(value, list):
                for part in value:
                    if isinstance(part, dict) and not part.get('type'):
                        visit(part)
                    elif isinstance(part, dict) and 'structuredContent' in part:
                        visit(part['structuredContent'])
        if isinstance(value, str) and item['tool'].rsplit('.', 1)[-1] in ('exec_command', 'write_stdin'):
            try:
                value = json.loads(value)  # whole native process-result object only
            except ValueError:
                value = None
        visit(value)
        item.update(exit_codes=codes, state='running' if running else 'failed' if failed else 'exited' if codes else 'unknown')
    try:
        if result['error'] or anchor.get('session_id') != sid or anchor.get('harness') != harness:
            raise ValueError(result['error'] or 'native session boundary unavailable')
        source = Path(anchor['source'])
        cursor = int(anchor['cursor'])
        rows = []
        if harness == 'opencode':
            used = 0
            with _opencode_view(source) as con:
                for rowid, raw, message_raw in con.execute(
                    "SELECT p.rowid,p.data,m.data FROM part p LEFT JOIN message m ON m.id=p.message_id "
                    "WHERE p.session_id=? AND p.rowid>? AND p.rowid<=? ORDER BY p.rowid",
                    (sid, cursor, until if until is not None else 2**63-1)):
                    used += len(raw)
                    if used > TURN_SCAN_BYTES:
                        result['coverage'] = 'bounded; remaining native rows unknown'
                        break
                    rows.append((f'{source}#session={sid}&rowid={rowid}', json.loads(raw), json.loads(message_raw or '{}')))
                else:
                    result['coverage'] = 'current-turn'
        else:
            with source.open('rb') as stream:
                info = os.fstat(stream.fileno())
                if f'{info.st_dev}:{info.st_ino}' != anchor.get('identity') or info.st_size < cursor:
                    raise ValueError('native source replaced or truncated')
                stop = min(info.st_size, until if until is not None else info.st_size)
                stream.seek(cursor)
                raw = stream.read(min(TURN_SCAN_BYTES, max(0, stop - cursor)))
            result['coverage'] = anchor.get('coverage') or ('current-turn' if cursor + len(raw) == stop else 'bounded; remaining native rows unknown')
            offset = cursor
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b'\n'):
                    result['coverage'] = 'partial row; native tail unknown'
                    break
                try:
                    rows.append((f'{source}#byte={offset}', json.loads(line), {}))
                except ValueError:
                    result['coverage'] = 'malformed row; partial observations'
                offset += len(line)
        calls = {}
        for ref, row, message in rows:
            if not isinstance(row, dict):
                continue
            if harness == 'opencode':
                if row.get('type') == 'tool' and row.get('tool') in ('bash', 'shell'):
                    state = row.get('state') or {}
                    item = command(row['tool'], row.get('callID', ''), state.get('input'), ref)
                    code = (state.get('metadata') or {}).get('exit')
                    item.update(state='running' if state.get('status') in ('pending', 'running') else
                        'failed' if state.get('status') == 'error' else 'exited' if isinstance(code, int) else 'unknown',
                        exit_codes=[code] if isinstance(code, int) and not isinstance(code, bool) else [])
                if row.get('type') == 'text' and message.get('role') == 'assistant' and not row.get('synthetic'):
                    result.update(reply=text(row.get('text')), reply_reference=ref)
            elif harness == 'claude':
                msg = row.get('message') or {}
                if not isinstance(msg, dict):
                    continue
                for block in msg.get('content') or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use' and block.get('name') in ('Bash', 'bash', 'shell'):
                        calls[block.get('id')] = command(block['name'], block.get('id'), block.get('input'), ref)
                    elif block.get('type') == 'tool_result' and block.get('tool_use_id') in calls:
                        item = calls[block['tool_use_id']]
                        output(item, row.get('toolUseResult') or block.get('content'), failed=block.get('is_error', False))
                        item['result_reference'] = ref
                    elif block.get('type') == 'text' and msg.get('role') == 'assistant':
                        result.update(reply=text(block.get('text')), reply_reference=ref)
            else:
                p = row.get('payload') or {}
                if not isinstance(p, dict):
                    continue
                kind = p.get('type')
                if kind in ('function_call', 'custom_tool_call') and re.search(r'(?:^|\.)(?:exec|exec_command|write_stdin|wait)$', p.get('name', '')):
                    calls[p.get('call_id')] = command(p['name'], p.get('call_id'), p.get('arguments') or p.get('input'), ref)
                elif kind in ('function_call_output', 'custom_tool_call_output') and p.get('call_id') in calls:
                    item = calls[p['call_id']]
                    output(item, p.get('output'))
                    item['result_reference'] = ref
                elif kind == 'message' and p.get('role') == 'assistant' and p.get('channel') in (None, 'final'):
                    result.update(reply=text('\n'.join(_rt._codex_text(row))), reply_reference=ref)
                elif kind in ('turn_aborted', 'task_aborted'):
                    result['terminal'] = 'interrupted'
                elif kind == 'task_complete' and p.get('last_agent_message'):
                    result.update(reply=text(p['last_agent_message']), reply_reference=ref)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        result['error'] = _read_error(exc)
    if isinstance(payload.get('last_assistant_message'), str):
        result.update(reply=text(payload['last_assistant_message']), reply_reference='native Stop.last_assistant_message')
    if payload.get('interrupted') or payload.get('hook_event_name') == 'StopFailure':
        result['terminal'] = 'interrupted'
    result['turn_id'] = payload.get('turn_id', '')
    return result


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------

def watermark_path(harness: str, sid: str) -> Path:
    return state_root() / "watermarks" / f"{harness}-{session_tidy._digest(sid, size=16)}.json"


def read_watermark(harness: str, sid: str) -> dict:
    data = read_json(watermark_path(harness, sid))
    if isinstance(data, dict) and isinstance(data.get("cursor"), int) and data["cursor"] >= 0:
        return data
    return {"cursor": 0}


def write_watermark(harness: str, sid: str, cursor: int, *, source: str = "",
                    now: Optional[float] = None) -> dict:
    """Record the last cursor whose actions were applied (the runner calls this)."""
    info: dict = {}
    if source and harness != "opencode":
        with_stat = _stat(source)
        if with_stat:
            info = {"size": with_stat.st_size, "inode": with_stat.st_ino}
    value = {"schema": 1, "harness": harness, "sid": sid, "cursor": int(cursor),
             "updated": iso_utc(now_epoch() if now is None else now), **info}
    ensure_dir(state_root() / "watermarks")
    atomic_write_json(watermark_path(harness, sid), value)
    return value


def _stat(path) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Unread ranges: newest first
# ---------------------------------------------------------------------------

MAX_BACK_SCAN = 256 * 1024 * 1024


def _merge_ranges(ranges) -> list:
    """Sorted, non-empty, non-touching ``[from, to)`` ranges."""
    out: list = []
    for a, b in sorted((int(a), int(b)) for a, b in ranges if int(b) > int(a)):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _subtract(ranges, lo: int, hi: int) -> list:
    out: list = []
    for a, b in ranges:
        if b <= lo or a >= hi:
            out.append([a, b])
            continue
        if a < lo:
            out.append([a, lo])
        if b > hi:
            out.append([hi, b])
    return out


def _row_end(path, size: int) -> int:
    """End of the last whole row: a file ending in a newline, or whose last row parses, is whole."""
    if size <= 0:
        return 0
    with open(path, "rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return size
        start, buf = size, b""
        while start > 0 and b"\n" not in buf:
            if len(buf) >= MAX_ROW_BYTES:
                raise OSError("last row too long to judge")
            step = min(64 * 1024, start)
            start -= step
            handle.seek(start)
            buf = handle.read(step) + buf
    tail = buf[buf.rfind(b"\n") + 1:]
    try:
        json.loads(tail)
        return size
    except ValueError:
        return size - len(tail)               # half-written: stays unread until it is whole


def _tail_rows(path: Path, lo: int, hi: int, limit: int) -> tuple[list, int, int]:
    """The last whole rows of ``[lo, hi)`` within ``limit`` bytes: ``(rows, start, skipped_oversize)``.

    ``lo`` and ``hi`` sit on row boundaries.  One row longer than ``limit`` is read whole
    (up to ``MAX_ROW_BYTES``); a longer one is stepped over and counted, so a giant tool
    output cannot stall the range.
    """
    with open(path, "rb") as handle:
        want = max(lo, hi - max(1, limit))
        handle.seek(want)
        buf = handle.read(hi - want)
        if want > lo:
            handle.seek(want - 1)
            if handle.read(1) != b"\n":
                cut = buf.find(b"\n")
                if 0 <= cut < len(buf) - 1:
                    buf, want = buf[cut + 1:], want + cut + 1
                else:
                    # One row spans the whole window: find where it starts.
                    start, scanned = want, 0
                    while start > lo:
                        step = min(1 << 20, start - lo)
                        start -= step
                        scanned += step
                        handle.seek(start)
                        block = handle.read(step)
                        found = block.rfind(b"\n")
                        if found >= 0:
                            start += found + 1
                            break
                        if scanned > MAX_BACK_SCAN:
                            raise OSError("row too long to step over")
                    if hi - start > MAX_ROW_BYTES:
                        return [], start, 1
                    handle.seek(start)
                    return [r for r in handle.read(hi - start).split(b"\n") if r.strip()], start, 0
        return [r for r in buf.split(b"\n") if r.strip()], want, 0


def _plan_jsonl(path, mark: dict) -> dict:
    """The unread ranges of a Claude/Codex record (new appends join; a replaced file starts over)."""
    info = os.stat(path)
    end = _row_end(path, info.st_size)
    inode = info.st_ino
    pending = mark.get("pending")
    if isinstance(pending, list) and mark.get("unit") == "byte" and isinstance(mark.get("end"), int):
        if (mark.get("inode") in (None, 0, inode)) and mark["end"] <= end:
            ranges = [list(r) for r in pending if isinstance(r, list) and len(r) == 2]
            if end > mark["end"]:
                ranges.append([mark["end"], end])
            return {"unit": "byte", "end": end, "ranges": _merge_ranges(ranges), "inode": inode, "size": info.st_size}
    elif isinstance(mark.get("cursor"), int) and not isinstance(pending, list):
        cursor = mark["cursor"]                                   # schema 1: "read up to cursor"
        if cursor <= end and (not mark.get("inode") or mark["inode"] == inode):
            return {"unit": "byte", "end": end, "ranges": _merge_ranges([[cursor, end]]), "inode": inode,
                    "size": info.st_size}
    return {"unit": "byte", "end": end, "ranges": _merge_ranges([[0, end]]), "inode": inode, "size": info.st_size}


def _plan_opencode(unit: str, rows: list, mark: dict) -> dict:
    """The unread cursor ranges of one OpenCode session, from one read-only snapshot.

    Cursors are V1 ``part`` rowids or OpenCode 2 ``session_message`` seqs (``unit``);
    a watermark of the other unit starts the session over.
    """
    end = rows[-1][0] + 1 if rows else 0
    snapshot_ms = max((row[1] for row in rows), default=0)
    pending = mark.get("pending")
    if isinstance(pending, list) and mark.get("unit") == unit and isinstance(mark.get("end"), int) \
            and mark["end"] <= end:
        ranges = [list(r) for r in pending if isinstance(r, list) and len(r) == 2]
        if end > mark["end"]:
            ranges.append([mark["end"], end])
        if mark.get("snapshot_ms"):
            # A row that changed after the last snapshot (a tool part that was still running) is read again.
            ranges += [[row[0], row[0] + 1] for row in rows
                       if row[0] < mark["end"] and row[1] > int(mark["snapshot_ms"])]
    elif unit == "rowid" and isinstance(mark.get("cursor"), int) and not isinstance(pending, list):
        ranges = [[mark["cursor"] + 1, end]]                      # schema 1: last rowid consumed
    else:
        ranges = [[0, end]]
    return {"unit": unit, "end": end, "ranges": _merge_ranges(ranges), "snapshot_ms": snapshot_ms}


def _read_opencode_tail(db_path: Path, sid: str, mark: dict, limit: int) -> Chunk:
    chunk = Chunk("opencode", str(db_path), 0, 0, unit="rowid")
    if not sid:
        chunk.error = "no session id"
        return chunk
    # The plan needs every row's cursor and update time (``_plan_opencode``), so this read
    # stays whole; only the rows the budget takes are sized.
    unit, rows, error = _opencode_session(db_path, sid, sized=False)
    if error:
        chunk.error = error
        return chunk
    plan = _plan_opencode(unit, rows, mark)
    chunk.plan, chunk.total, chunk.unit = plan, plan["end"], unit
    ranges = plan["ranges"]
    if not ranges:
        chunk.cursor_from = chunk.cursor_to = plan["end"]
        return chunk
    lo, hi = ranges[-1]
    inside = [row for row in rows if lo <= row[0] < hi]
    blocked_at = next((row[0] for row in inside if any(_open_question(part, row[1]) for part in row[4])), None)
    if blocked_at is not None:
        chunk.blocked, hi = "open-question", blocked_at
    low, taken, used = hi, [], 0
    if hi > lo:
        low = lo
        for row in reversed([row for row in inside if row[0] < hi]):
            size = _row_size(row)
            if used and used + size > limit:
                low = row[0] + 1
                break
            used += size
            taken.append(row)
    _digest_opencode(list(reversed(taken)), chunk)
    chunk.cursor_from, chunk.cursor_to = low, hi
    chunk.pending_after = _subtract(ranges, low, hi)
    chunk.eof = not chunk.pending_after
    return chunk


def _read_jsonl_tail(harness: str, path: Path, mark: dict, limit: int) -> Chunk:
    chunk = Chunk(harness, str(path), 0, 0)
    try:
        plan = _plan_jsonl(path, mark)
        chunk.plan, chunk.total = plan, plan["end"]
        if not plan["ranges"]:
            chunk.cursor_from = chunk.cursor_to = plan["end"]
            return chunk
        lo, hi = plan["ranges"][-1]
        lines, start, skipped = _tail_rows(path, lo, hi, limit)
    except OSError as exc:
        chunk.error = f"unreadable: {exc.__class__.__name__}"
        return chunk
    _digest_rows(harness, path, lines, start, chunk)
    chunk.cursor_from, chunk.cursor_to, chunk.skipped_oversize = start, hi, skipped
    chunk.pending_after = _subtract(plan["ranges"], start, hi)
    chunk.eof = not chunk.pending_after
    return chunk


def read_pending(harness: str, sid: str, source, limit_bytes: int = DEFAULT_CHUNK_BYTES) -> Chunk:
    """The newest unread range's last whole rows (at most ``limit_bytes``), chronological inside.

    ``cursor_from``/``cursor_to`` are the range this chunk covers; ``pending_after`` is what stays
    unread once it is applied.  Nothing is written: ``mark_applied`` records it after a successful apply.
    """
    mark = read_watermark(harness, sid)
    limit = max(1, int(limit_bytes))
    if harness == "opencode":
        return _opencode_retry(lambda: _read_opencode_tail(Path(source), sid or "", mark, limit))
    if harness in ("claude", "codex"):
        return _read_jsonl_tail(harness, Path(source), mark, limit)
    return Chunk(harness, str(source), 0, 0, error="unknown harness")


def mark_applied(harness: str, sid: str, chunk: Chunk, *, now: Optional[float] = None) -> dict:
    """Record that ``chunk``'s range was applied: it leaves the unread ranges, nothing else does.

    The ranges come from the snapshot the chunk was cut from, so a later append is not lost
    and an old unread front is never overwritten.  A record replaced since the read writes nothing.
    """
    plan = chunk.plan or {}
    if not plan or chunk.error:
        return {}
    if harness != "opencode":
        info = _stat(chunk.source)
        if info is None or (plan.get("inode") and plan["inode"] != info.st_ino):
            return {}
    ranges = _subtract(plan["ranges"], chunk.cursor_from, chunk.cursor_to)
    value = {"schema": 2, "harness": harness, "sid": sid, "unit": plan["unit"],
             "cursor": ranges[0][0] if ranges else plan["end"], "end": plan["end"], "pending": ranges,
             "updated": iso_utc(now_epoch() if now is None else now)}
    for key in ("inode", "size", "snapshot_ms"):
        if plan.get(key):
            value[key] = plan[key]
    ensure_dir(state_root() / "watermarks")
    atomic_write_json(watermark_path(harness, sid), value)
    return value


def describe_coverage(chunk: Chunk) -> str:
    """``byte 2,880,000–3,145,728 / 전체 3,145,728`` (rowid for OpenCode): what this chunk covered."""
    return f"{chunk.unit} {chunk.cursor_from:,}–{chunk.cursor_to:,} / 전체 {chunk.total:,}"


def unread_total(chunk: Chunk) -> int:
    return sum(b - a for a, b in chunk.pending_after)


# ---------------------------------------------------------------------------
# Recent sessions of one seat
# ---------------------------------------------------------------------------

def _head_cwd_claude(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEAD_SCAN_BYTES)
    except OSError:
        return ""
    for line in head.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("cwd"):
            return str(data["cwd"])
    return ""


def _head_meta_codex(path: Path) -> tuple[str, str]:
    try:
        with open(path, "rb") as handle:
            first = handle.readline(HEAD_SCAN_BYTES)
        payload = (json.loads(first) or {}).get("payload") or {}
    except (OSError, ValueError, AttributeError):
        return "", ""
    return str(payload.get("cwd") or ""), str(payload.get("id") or payload.get("session_id") or "")


def _codex_sid_from_name(path: Path) -> str:
    match = re.search(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path.name)
    return match.group(1) if match else ""


def _discover(harness: str, horizon: float) -> Iterator[dict]:
    if harness == "claude":
        root = claude_projects_dir()
        files = [(p, _stat(p)) for p in root.glob("*/*.jsonl")] if root.is_dir() else []
        files = [(p, s) for p, s in files if s and s.st_mtime >= horizon]
        for path, info in sorted(files, key=lambda x: -x[1].st_mtime)[:MAX_DISCOVER_FILES]:
            yield {"harness": "claude", "sid": path.stem, "transcript": str(path),
                   "cwd": _head_cwd_claude(path), "last_active": info.st_mtime}
    elif harness == "codex":
        root = codex_sessions_dir()
        files = [(p, _stat(p)) for p in root.rglob("rollout-*.jsonl")] if root.is_dir() else []
        files = [(p, s) for p, s in files if s and s.st_mtime >= horizon]
        for path, info in sorted(files, key=lambda x: -x[1].st_mtime)[:MAX_DISCOVER_FILES]:
            cwd, sid = _head_meta_codex(path)
            yield {"harness": "codex", "sid": sid or _codex_sid_from_name(path), "transcript": str(path),
                   "cwd": cwd, "last_active": info.st_mtime}
    elif harness == "opencode":
        db = opencode_db_path()
        if not db.is_file():
            return
        rows = []
        try:
            with _opencode_view(db) as con:
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('session','session_v2')")}
                # OpenCode 2 keeps its sessions in ``session_v2``; an upgraded home still has ``session``.
                for table in ("session_v2", "session"):
                    if table in tables:
                        rows += con.execute(
                            f"SELECT id, directory, time_updated FROM {table} WHERE time_updated >= ? "
                            "AND parent_id IS NULL ORDER BY time_updated DESC LIMIT ?",
                            (int(horizon * 1000), MAX_DISCOVER_FILES)).fetchall()
        except (OSError, sqlite3.Error):
            return
        rows = sorted(rows, key=lambda row: -float(row[2] or 0))[:MAX_DISCOVER_FILES]
        for sid, directory, updated in rows:
            yield {"harness": "opencode", "sid": sid, "transcript": str(db),
                   "cwd": directory or "", "last_active": float(updated) / 1000}


def select_recent_sessions(seat, cwd=None, now: Optional[float] = None, days: int = RECENT_DAYS) -> list:
    """Sessions of ``seat`` active within ``days`` days, newest first.

    The seat ledger decides.  A past record the ledger never saw is added only when
    it is confirmed to belong to the seat: the seat has no pane (harness + project
    key) and the record's own cwd resolves to the same project key.  Nothing is
    guessed from a path alone, and a pane seat gets no unledgered record at all.
    """
    now = now_epoch() if now is None else now
    horizon = now - days * 86400
    found: dict = {}
    for row in session_tidy.session_summary(seat).values():
        transcript = row["transcript"] or ""
        located = locate_transcript(row["harness"], row["sid"], transcript or None)
        info = _stat(located) if located and row["harness"] != "opencode" else None
        last = max(float(row["last_seen"] or 0), info.st_mtime if info else 0.0)
        if last >= horizon:
            found[(row["harness"], row["sid"])] = {
                "harness": row["harness"], "sid": row["sid"], "transcript": str(located or transcript),
                "cwd": row["cwd"], "last_active": last, "source": "ledger"}
    if seat.kind == "project" and seat.harness in HARNESSES:
        for item in _discover(seat.harness, horizon):
            key = (item["harness"], item["sid"])
            if not item["sid"] or key in found or not item["cwd"]:
                continue
            if session_tidy.project_key_for(item["cwd"]) != seat.project_key:
                continue
            item["source"] = "project-key"
            found[key] = item
    return sorted(found.values(), key=lambda r: -r["last_active"])


if __name__ == "__main__":  # tiny manual probe: tidy_transcripts.py <harness> <source> [cursor] [sid]
    if len(sys.argv) < 3:
        sys.exit("usage: tidy_transcripts.py <harness> <source> [cursor] [sid]")
    result = read_chunk(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 0,
                        sys.argv[4] if len(sys.argv) > 4 else None)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=1))
