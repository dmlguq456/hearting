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

Position meaning: a byte offset into the JSONL file (Claude, Codex) or the ``part``
rowid (OpenCode); a range is half-open ``[from, to)``.  A row that could not be read
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
    unit: str = "byte"     # "byte" (Claude, Codex) | "rowid" (OpenCode)
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


def _read_opencode(db_path: Path, session_id: str, cursor: int, limit: int) -> Chunk:
    chunk = Chunk("opencode", str(db_path), cursor, cursor)
    if not session_id:
        chunk.error = "no session id"
        return chunk
    try:
        with _opencode_view(db_path) as con:
            rows = con.execute(
                "SELECT p.rowid, p.data, p.time_updated, m.data FROM part p "
                "LEFT JOIN message m ON m.id = p.message_id "
                "WHERE p.session_id = ? AND p.rowid > ? ORDER BY p.rowid ASC", (session_id, int(cursor)))
            used, last, texts, exhausted = 0, int(cursor), [], True
            for rowid, raw, updated_ms, message_raw in rows:
                raw = raw if isinstance(raw, str) else (raw or b"").decode("utf-8", "replace")
                if used and used + len(raw) > limit:
                    exhausted = False
                    break
                try:
                    part = json.loads(raw)
                except ValueError:
                    part = None
                if isinstance(part, dict) and part.get("tool") == "question" and \
                        (part.get("state") or {}).get("status") in ("pending", "running") and \
                        now_epoch() - float(updated_ms or 0) / 1000 < OPEN_QUESTION_STALE_SEC:
                    chunk.blocked, exhausted = "open-question", False
                    break
                used += len(raw)
                last = int(rowid)
                chunk.rows += 1
                if not isinstance(part, dict):
                    continue
                chunk.choices.extend(_opencode_choices(part))
                if part.get("type") != "text" or part.get("synthetic") or part.get("ignored"):
                    continue
                text = _rt._opencode_text(part).strip()
                if text:
                    try:
                        role = str((json.loads(message_raw) or {}).get("role") or "")
                    except (ValueError, TypeError, AttributeError):
                        role = ""
                    texts.append(_dialogue_line(role, text))
            chunk.cursor_to, chunk.eof, chunk.text = last, exhausted, "\n".join(texts)
    except (OSError, sqlite3.Error) as exc:
        chunk.error = _read_error(exc)
    return chunk


def read_chunk(harness: str, source, cursor: int = 0, sid: Optional[str] = None,
               limit_bytes: int = DEFAULT_CHUNK_BYTES) -> Chunk:
    """A bounded read of whole rows after ``cursor``; ``cursor_to`` covers exactly what was read."""
    limit = max(1, int(limit_bytes))
    cursor = max(0, int(cursor or 0))
    if harness == "opencode":
        return _opencode_retry(lambda: _read_opencode(Path(source), sid or "", cursor, limit))
    if harness in ("claude", "codex"):
        return _read_jsonl(harness, Path(source), cursor, limit)
    return Chunk(harness, str(source), cursor, cursor, error="unknown harness")


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


def _plan_opencode(con, sid: str, mark: dict) -> dict:
    """The unread rowid ranges of one OpenCode session, inside one read-only snapshot."""
    top, stamp = con.execute("SELECT MAX(rowid), MAX(time_updated) FROM part WHERE session_id = ?", (sid,)).fetchone()
    end = int(top) + 1 if top is not None else 0
    snapshot_ms = int(stamp or 0)
    pending = mark.get("pending")
    if isinstance(pending, list) and mark.get("unit") == "rowid" and isinstance(mark.get("end"), int) \
            and mark["end"] <= end:
        ranges = [list(r) for r in pending if isinstance(r, list) and len(r) == 2]
        if end > mark["end"]:
            ranges.append([mark["end"], end])
        if mark.get("snapshot_ms"):
            # A row that changed after the last snapshot (a tool part that was still running) is read again.
            for (rowid,) in con.execute("SELECT rowid FROM part WHERE session_id = ? AND rowid < ? "
                                        "AND time_updated > ?", (sid, mark["end"], int(mark["snapshot_ms"]))):
                ranges.append([int(rowid), int(rowid) + 1])
    elif isinstance(mark.get("cursor"), int) and not isinstance(pending, list):
        ranges = [[mark["cursor"] + 1, end]]                      # schema 1: last rowid consumed
    else:
        ranges = [[0, end]]
    return {"unit": "rowid", "end": end, "ranges": _merge_ranges(ranges), "snapshot_ms": snapshot_ms}


def _open_question_rowid(con, sid: str, lo: int, hi: int) -> Optional[int]:
    """The first rowid in ``[lo, hi)`` of a question still waiting for its answer, if any."""
    rows = con.execute("SELECT rowid, data, time_updated FROM part WHERE session_id = ? AND rowid >= ? "
                       "AND rowid < ? AND data LIKE '%question%' ORDER BY rowid ASC", (sid, lo, hi))
    for rowid, raw, updated_ms in rows:
        raw = raw if isinstance(raw, str) else (raw or b"").decode("utf-8", "replace")
        try:
            part = json.loads(raw)
        except ValueError:
            continue
        if isinstance(part, dict) and part.get("tool") == "question" and \
                (part.get("state") or {}).get("status") in ("pending", "running") and \
                now_epoch() - float(updated_ms or 0) / 1000 < OPEN_QUESTION_STALE_SEC:
            return int(rowid)
    return None


def _read_opencode_tail(db_path: Path, sid: str, mark: dict, limit: int) -> Chunk:
    chunk = Chunk("opencode", str(db_path), 0, 0, unit="rowid")
    if not sid:
        chunk.error = "no session id"
        return chunk
    try:
        with _opencode_view(db_path) as con:
            plan = _plan_opencode(con, sid, mark)
            chunk.plan, chunk.total = plan, plan["end"]
            ranges = plan["ranges"]
            if not ranges:
                chunk.cursor_from = chunk.cursor_to = plan["end"]
                return chunk
            lo, hi = ranges[-1]
            blocked_at = _open_question_rowid(con, sid, lo, hi)
            if blocked_at is not None:
                chunk.blocked, hi = "open-question", blocked_at
            low, rows, used = hi, [], 0
            if hi > lo:
                low = lo
                for rowid, raw, message_raw in con.execute(
                        "SELECT p.rowid, p.data, m.data FROM part p LEFT JOIN message m ON m.id = p.message_id "
                        "WHERE p.session_id = ? AND p.rowid >= ? AND p.rowid < ? ORDER BY p.rowid DESC",
                        (sid, lo, hi)):
                    raw = raw if isinstance(raw, str) else (raw or b"").decode("utf-8", "replace")
                    if used and used + len(raw) > limit:
                        low = int(rowid) + 1
                        break
                    used += len(raw)
                    rows.append((int(rowid), raw, message_raw))
            texts = []
            for _rowid, raw, message_raw in reversed(rows):
                chunk.rows += 1
                try:
                    part = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(part, dict):
                    continue
                chunk.choices.extend(_opencode_choices(part))
                if part.get("type") != "text" or part.get("synthetic") or part.get("ignored"):
                    continue
                text = _rt._opencode_text(part).strip()
                if text:
                    try:
                        role = str((json.loads(message_raw) or {}).get("role") or "")
                    except (ValueError, TypeError, AttributeError):
                        role = ""
                    texts.append(_dialogue_line(role, text))
            chunk.cursor_from, chunk.cursor_to, chunk.text = low, hi, "\n".join(texts)
            chunk.pending_after = _subtract(ranges, low, hi)
            chunk.eof = not chunk.pending_after
    except (OSError, sqlite3.Error) as exc:
        chunk.error = _read_error(exc)
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
        try:
            with _opencode_view(db) as con:
                rows = con.execute(
                    "SELECT id, directory, time_updated FROM session WHERE time_updated >= ? "
                    "AND parent_id IS NULL ORDER BY time_updated DESC LIMIT ?",
                    (int(horizon * 1000), MAX_DISCOVER_FILES)).fetchall()
        except (OSError, sqlite3.Error):
            return
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
