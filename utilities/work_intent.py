#!/usr/bin/env python3
"""The person's intent, sealed once when a route is composed (WORKFLOW §0.4).

A route used to carry only the agent-written request (`work_request.text`). The
person's own words and the one-line goal it was cut from reached no owner or
worker unless a frame-review answer happened to sit on that same route. This
leaf seals four optional values into `work_request.intent` at compose:

- `original`: a short excerpt of the person's current prompt, read by the
  runtime from the session record -- only when the prompt boundary is a person's
  (not a peer or runtime transfer). Nothing is read when that is unclear.
- `intent`, `not_doing`, `done`: the agent's leading `의도:` / `안 할 것:` /
  `완료:` lines (or `Intent:` / `Not doing:` / `Done:`) of the prompt file. With
  no such line the request's first sentence is the drafted intent.

Every value is optional and every failure leaves only that value out: this is a
record and a carrier, never a gate or a required input.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

MAX_ORIGINAL_CHARS = 600
MAX_LINE_CHARS = 300
MAX_NOT_DOING = 3
TAIL_BYTES = 1024 * 1024
FIELDS = ("original", "intent", "not_doing", "done", "basis")
BASES = ("header", "first-sentence")
_LABELS = {
    "intent": ("의도", "intent", "goal"),
    "not_doing": ("안 할 것", "하지 않을 것", "not doing", "non-goals", "out of scope"),
    "done": ("완료", "done", "done when"),
}
_HEADER = re.compile(r"^\s*(?:[-*]\s+)?(?P<label>[^:：]{1,24})\s*[:：]\s*(?P<value>.*\S)?\s*$")
_NOT_HUMAN_PREFIXES = ("<environment_context", "<user_instructions", "<turn_aborted", "<user_shell_command")


def _line(value, cap=MAX_LINE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _label(raw: str) -> str | None:
    key = " ".join(raw.strip().lower().split())
    return next((name for name, labels in _LABELS.items() if key in labels), None)


def _split_header(text: str) -> tuple[dict, str]:
    """The leading label lines of a prompt file and the text after them."""
    found: dict = {}
    lines = (text or "").splitlines()
    index = 0
    for index, raw in enumerate(lines):
        if not raw.strip():
            if found:
                break
            continue
        match = _HEADER.match(raw)
        name = _label(match["label"]) if match else None
        if name is None:
            break
        value = (match["value"] or "").strip()
        if not value:
            continue
        if name == "not_doing":
            items = [_line(part) for part in re.split(r"\s*[;；]\s*", value) if part.strip()]
            found["not_doing"] = (found.get("not_doing", []) + items)[:MAX_NOT_DOING]
        elif name not in found:
            found[name] = _line(value)
    else:
        index = len(lines)
    return found, "\n".join(lines[index:]) if found else (text or "")


def parse_header(text: str) -> dict:
    return _split_header(text)[0]


def draft(text: str) -> dict:
    """Intent fields from the prompt file alone: its header, else its first sentence."""
    found, body = _split_header(text)
    if found.get("intent"):
        return {**found, "basis": "header"}
    import frame_interview
    first = frame_interview.understanding_draft(body)
    if first:
        found = {**found, "intent": _line(first), "basis": "first-sentence"}
    return found


def validate(value) -> dict:
    """The sealed shape; read by `work_start.validate_request`."""
    if not isinstance(value, dict) or not value or set(value) - set(FIELDS):
        raise ValueError("work-request-intent-invalid")
    for key in ("original", "intent", "done"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()
                             or len(value[key]) > (MAX_ORIGINAL_CHARS if key == "original" else MAX_LINE_CHARS)):
            raise ValueError("work-request-intent-invalid")
    if "not_doing" in value:
        items = value["not_doing"]
        if (not isinstance(items, list) or not items or len(items) > MAX_NOT_DOING
                or any(not isinstance(item, str) or not item.strip() or len(item) > MAX_LINE_CHARS
                       for item in items)):
            raise ValueError("work-request-intent-invalid")
    if "basis" in value and (value["basis"] not in BASES or "intent" not in value):
        raise ValueError("work-request-intent-invalid")
    if not set(value) - {"basis"}:
        raise ValueError("work-request-intent-invalid")
    return value


# ---------------------------------------------------------------------------
# The person's words, read from the session record at the current prompt
# ---------------------------------------------------------------------------

def _human_prompt_boundary(harness: str, sid: str, cwd, environ) -> bool:
    """The current prompt boundary exists and carries no peer/runtime transfer origin."""
    import session_tidy
    seat = session_tidy.resolve_seat(harness, cwd or None, environ, sid, "")
    turn = session_tidy.read_json(session_tidy._prompt_seq_path(seat))
    return (isinstance(turn, dict) and turn.get("sid") == sid and turn.get("harness") == harness
            and type(turn.get("seq")) is int and not turn.get("request_origin"))


def _human_text(text: str) -> str:
    from fleet import refresh_title
    value = refresh_title._human_language_text(text or "")
    if not value or value.lstrip().startswith(_NOT_HUMAN_PREFIXES):
        return ""
    return value.strip()


def _last_jsonl_user_text(harness: str, path: Path) -> str:
    import tidy_transcripts
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - TAIL_BYTES)
        handle.seek(start)
        if start:
            handle.readline()
        rows = handle.read().splitlines()
    for raw in reversed(rows):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        role, text = tidy_transcripts._text_of_jsonl_row(data, harness)
        if role == "user":
            # The latest user row is the prompt boundary; an envelope there is not the person's.
            return _human_text(text)
    return ""


def _last_opencode_user_text(db_path: Path, sid: str) -> str:
    import tidy_transcripts
    from fleet import refresh_title
    with tidy_transcripts._opencode_view(db_path) as con:
        rows = con.execute(
            "SELECT p.data, m.data FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id = ? ORDER BY p.rowid DESC LIMIT 400", (sid,))
        for raw, message_raw in rows:
            try:
                part, message = json.loads(raw), json.loads(message_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if (not isinstance(part, dict) or part.get("type") != "text"
                    or part.get("synthetic") or part.get("ignored")):
                continue
            return _human_text(refresh_title._opencode_text(part))
    return ""


def original_excerpt(cwd=None, environ=None) -> str:
    """The person's current prompt, bounded; empty whenever that cannot be shown exactly."""
    environ = os.environ if environ is None else environ
    try:
        from session_identity import identity
        who = identity(environ)
        if not who.known or not who.session_id or who.harness not in ("claude", "codex", "opencode"):
            return ""
        if not _human_prompt_boundary(who.harness, who.session_id, cwd, environ):
            return ""
        tools = HERE.parent / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        import tidy_transcripts
        source = tidy_transcripts.locate_transcript(who.harness, who.session_id)
        if source is None:
            return ""
        text = (_last_opencode_user_text(Path(source), who.session_id) if who.harness == "opencode"
                else _last_jsonl_user_text(who.harness, Path(source)))
    except Exception:  # noqa: BLE001 -- the excerpt is optional; any failure leaves it out
        return ""
    text = (text or "").strip()
    if len(text) > MAX_ORIGINAL_CHARS:
        text = text[:MAX_ORIGINAL_CHARS - 1].rstrip() + "…"
    return text


def capture(text: str, cwd=None, environ=None) -> dict | None:
    """What compose seals: the drafted fields plus the person's words when readable."""
    found = draft(text)
    original = original_excerpt(cwd, environ)
    if original:
        found = {"original": original, **found}
    return validate(found) if set(found) - {"basis"} else None


def inherited(source_request, text: str) -> dict | None:
    """A later leg keeps the intent sealed at the person's start; never re-reads the session."""
    value = source_request.get("intent") if isinstance(source_request, dict) else None
    if value is not None:
        try:
            return validate(dict(value))
        except ValueError:
            pass
    found = draft(text)
    return validate(found) if set(found) - {"basis"} else None


# ---------------------------------------------------------------------------
# Readers: the compose card and the owner/worker prompt
# ---------------------------------------------------------------------------

def route_intent(route) -> dict | None:
    request = route.get("work_request") if isinstance(route, dict) else None
    value = request.get("intent") if isinstance(request, dict) else None
    try:
        return validate(dict(value)) if value is not None else None
    except ValueError:
        return None


def card_lines(route) -> list[str]:
    """Two or three lines under compose's `[경로]` notice."""
    value = route_intent(route)
    if not value:
        return []
    lines = []
    if value.get("intent"):
        drafted = " (요청 첫 문장)" if value.get("basis") == "first-sentence" else ""
        lines.append(f"  [의도] {_line(value['intent'], 160)}{drafted}")
    extra = []
    if value.get("not_doing"):
        extra.append("안 할 것 " + "; ".join(_line(item, 80) for item in value["not_doing"]))
    if value.get("done"):
        extra.append("완료 " + _line(value["done"], 100))
    if extra:
        lines.append("    " + " · ".join(extra))
    if value.get("original"):
        lines.append(f"    원문 \"{_line(value['original'], 120)}\"")
    return lines


def prompt_block(route) -> str:
    """The sealed intent as an owner/worker prompt section; empty when none was sealed."""
    value = route_intent(route)
    if not value:
        return ""
    lines = ["Sealed intent (recorded when this work started; scope reference, not a new task):"]
    if value.get("original"):
        lines.append("- Person's words (excerpt): " + json.dumps(value["original"], ensure_ascii=False))
    if value.get("intent"):
        drafted = " (drafted from the request's first sentence)" if value.get("basis") == "first-sentence" else ""
        lines.append(f"- Intent: {value['intent']}{drafted}")
    for item in value.get("not_doing") or []:
        lines.append(f"- Not doing: {item}")
    if value.get("done"):
        lines.append(f"- Done when: {value['done']}")
    lines.append("Stay within this intent; the assignment below says which part is yours.")
    return "\n".join(lines) + "\n\n"
