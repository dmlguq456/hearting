#!/usr/bin/env python3
"""Read an existing session transcript without changing transcript or tidy state."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import attempt_view
import tidy_transcripts

_ATTEMPT = re.compile(r"^\.att-[A-Za-z0-9_-]+\.(claude|codex|opencode)\.jsonl$")


@dataclass(frozen=True)
class ConversationRead:
    status: str
    text: str
    source: str | None = None
    reason: str = ""


def _problem(status: str, source: str | None, reason: str) -> ConversationRead:
    return ConversationRead(status, "", source, reason)


def _say(message: str) -> str:
    """A reason the viewer shows, from tools/i18n/messages.json (English unless Korean is declared)."""
    import session_tidy
    return session_tidy._ui_translator()(message)


def _safe_file(path: Path) -> ConversationRead | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _problem("missing", str(path), _say("conversation.reason.no_file"))
    except OSError as exc:
        return _problem("unreadable", str(path), type(exc).__name__)
    if not stat.S_ISREG(info.st_mode):
        return _problem("unreadable", str(path), _say("conversation.reason.not_regular"))
    try:
        with path.open("rb") as stream:
            stream.read(0)
    except OSError as exc:
        return _problem("unreadable", str(path), type(exc).__name__)
    return None


def _read_attempt(path: Path, harness: str) -> ConversationRead:
    checked = _safe_file(path)
    if checked:
        return checked
    cursor = attempt_view.Cursor(path)
    source = attempt_view.Source(path, harness, "log")
    try:
        records, cursor = attempt_view.read_new(source, cursor, )
        lines = []
        for record in records:
            lines.extend(attempt_view.format_record(harness, record))
        while cursor.pending or cursor.offset < path.stat().st_size:
            previous = cursor.offset
            records, cursor = attempt_view.read_new(source, cursor)
            for record in records:
                lines.extend(attempt_view.format_record(harness, record))
            if cursor.error or cursor.offset <= previous:
                return ConversationRead("partial", "\n".join(line.text for line in lines), str(path),
                                        _say("conversation.reason.not_to_end"))
        text = "\n".join(line.text for line in lines)
        return ConversationRead("available", text, str(path), "" if text else _say("conversation.reason.empty"))
    except (OSError, ValueError, TypeError) as exc:
        return _problem("unreadable", str(path), type(exc).__name__)


def read_conversation(entry: dict) -> ConversationRead:
    """Read an entry from ``session_tidy.seat_lineage`` from its first record to EOF."""
    if not isinstance(entry, dict):
        return _problem("unreadable", None, _say("conversation.reason.bad_entry"))
    harness, sid = entry.get("harness"), entry.get("sid")
    transcript = entry.get("transcript") if isinstance(entry.get("transcript"), dict) else {}
    raw_path = transcript.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        status = transcript.get("status")
        return _problem("missing" if status in (None, "missing", "expired") else "unreadable", None,
                        _say("conversation.reason.no_place" if status in (None, "missing", "expired")
                             else "conversation.reason.place_unreadable"))
    path = Path(raw_path).expanduser()
    if not isinstance(harness, str) or harness not in tidy_transcripts.HARNESSES or not isinstance(sid, str):
        return _problem("unreadable", str(path), _say("conversation.reason.bad_identity"))
    if _ATTEMPT.fullmatch(path.name):
        return _read_attempt(path, harness)
    checked = _safe_file(path)
    if checked:
        return checked
    cursor, chunks, seen = 0, [], set()
    try:
        while True:
            chunk = tidy_transcripts.read_chunk(harness, path, cursor=cursor, sid=sid)
            if chunk.error:
                status = "unreadable" if cursor == 0 else "partial"
                return ConversationRead(status, "\n".join(chunks), str(path), chunk.error)
            parts = (chunk.text, *(c.get("text", "") for c in chunk.choices if isinstance(c, dict)))
            chunks.extend(attempt_view.sanitize(part) for part in parts if part)
            if chunk.skipped_oversize or chunk.blocked:
                reason = "oversize row skipped" if chunk.skipped_oversize else chunk.blocked
                return ConversationRead("partial", "\n".join(chunks), str(path), reason)
            if chunk.eof:
                break
            if chunk.cursor_to <= cursor or chunk.cursor_to in seen:
                return ConversationRead("partial", "\n".join(chunks), str(path), "transcript cursor did not advance")
            seen.add(chunk.cursor_to)
            cursor = chunk.cursor_to
        text = "\n".join(chunks)
        return ConversationRead("available", text, str(path), "" if text else _say("conversation.reason.empty"))
    except FileNotFoundError:
        return ConversationRead("missing" if cursor == 0 else "partial", "\n".join(chunks), str(path),
                                _say("conversation.reason.no_file"))
    except (OSError, ValueError, TypeError) as exc:
        return ConversationRead("unreadable" if cursor == 0 else "partial", "\n".join(chunks), str(path),
                                type(exc).__name__)
