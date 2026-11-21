#!/usr/bin/env python3
"""The end-of-line record of a model-table cell, and the state it gives.

A cell's state is decided by the line's value and the record at the end of that line,
nothing else (no state file). One reader and one writer, kept together so a round trip
is testable:

    # auto-follow 2026-11-21: model-a -> model-b                followed
    # auto-follow 2026-11-21: model-a -> model-b (effort was xhigh)
    # auto-follow keep 2026-11-21                                       held by the user
    # auto-follow held 2026-11-21: model-b rejected; back to model-a

A form this module does not know, or a broken one, reads as "no record": it is never
guessed into one. A comment the user put on the line is theirs; a line carries one
record, and writing a new one replaces the old one only. Pure text, standard library
only: the table's editor (`tools/install/model_settings.py`) and the survey
(`utilities/model_follow.py`) both read and write records through here.
"""
from __future__ import annotations

import datetime
import re
from typing import Mapping

PREFIX = "# auto-follow"
_MODEL = r"[A-Za-z0-9][A-Za-z0-9._/-]*"
_DATE = r"\d{4}-\d{2}-\d{2}"
_EFFORT = r"[a-z][a-z-]*"
# One record, bounded by a space (or the start) before and by the end or another comment after.
_RECORD = re.compile(
    r"(?:(?<=\s)|^)#\s*auto-follow\s+(?:"
    rf"keep\s+(?P<keep>{_DATE})"
    rf"|held\s+(?P<held>{_DATE}):\s*(?P<rejected>{_MODEL})\s+rejected;\s*back\s+to\s+(?P<back>{_MODEL})"
    rf"|(?P<date>{_DATE}):\s*(?P<a>{_MODEL})\s+->\s+(?P<b>{_MODEL})(?:\s+\(effort\s+was\s+(?P<was>{_EFFORT})\))?"
    r")(?=\s*(?:#|$))")
ASSIGNMENT = re.compile(r"^(CFG_[A-Z0-9_]+)=(.*)$")


def today() -> str:
    return datetime.date.today().isoformat()


def _real(date: str) -> bool:
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return False
    return True


def split_comment(rest: str) -> tuple[str, str]:
    """`(value, comment)` of the text after `KEY=`; the comment starts at the first `#`
    outside quotes and is "" when there is none. Same quoting rule as the table's parser."""
    quote, escaped = None, False
    for index, char in enumerate(rest):
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#":
            return rest[:index].rstrip(), rest[index:]
    return rest.rstrip(), ""


def _from_match(match: re.Match) -> dict | None:
    if match.group("keep"):
        date = match.group("keep")
        return {"kind": "keep", "date": date, "from": None, "to": None, "effort_was": None} if _real(date) else None
    if match.group("held"):
        date = match.group("held")
        if not _real(date):
            return None
        return {"kind": "held", "date": date, "from": match.group("back"),
                "to": match.group("rejected"), "effort_was": None}
    date = match.group("date")
    if not _real(date):
        return None
    return {"kind": "follow", "date": date, "from": match.group("a"), "to": match.group("b"),
            "effort_was": match.group("was")}


def parse_record(comment: str) -> dict | None:
    """The record in a line's comment (the last well-formed one), or None."""
    found = None
    for match in _RECORD.finditer(comment or ""):
        record = _from_match(match)
        if record is not None:
            found = record
    return found


def render(record: Mapping) -> str:
    """The comment text of a record (the exact inverse of `parse_record`)."""
    kind = record["kind"]
    if kind == "keep":
        return f"{PREFIX} keep {record['date']}"
    if kind == "held":
        return f"{PREFIX} held {record['date']}: {record['to']} rejected; back to {record['from']}"
    text = f"{PREFIX} {record['date']}: {record['from']} -> {record['to']}"
    if record.get("effort_was"):
        text += f" (effort was {record['effort_was']})"
    return text


def follow_record(date, before, after, effort_was=None) -> dict:
    return {"kind": "follow", "date": date, "from": before, "to": after, "effort_was": effort_was}


def keep_record(date) -> dict:
    return {"kind": "keep", "date": date, "from": None, "to": None, "effort_was": None}


def held_record(date, rejected, back) -> dict:
    return {"kind": "held", "date": date, "from": back, "to": rejected, "effort_was": None}


def strip_records(comment: str) -> str:
    """The comment without any well-formed record (the user's own words stay as they are)."""
    kept, last = [], 0
    for match in _RECORD.finditer(comment or ""):
        if _from_match(match) is None:
            continue
        kept.append(comment[last:match.start()])
        last = match.end()
    kept.append((comment or "")[last:])
    return "".join(kept).strip()


def with_record(comment: str, record: Mapping | None) -> str:
    """The comment carrying `record` as its only record; None removes it."""
    base = strip_records(comment).rstrip()
    if record is None:
        return base
    return f"{base} {render(record)}" if base else render(record)


def read_marks(text: str) -> dict[str, dict]:
    """`{key: record}` for every assignment line of the table text that carries one."""
    marks = {}
    for line in text.splitlines():
        match = ASSIGNMENT.fullmatch(line.strip())
        if not match:
            continue
        record = parse_record(split_comment(match.group(2))[1])
        if record is not None:
            marks[match.group(1)] = record
    return marks


def cell_state(model: str, record: Mapping | None) -> dict:
    """The state of a cell with value `model` and end-of-line `record`.

    `state` is `follow` (gets the newer model of the list), `keep` (stays as it is) or
    `held` (a newer model was refused; it is skipped). `why` says how it got there:
    `default` (no record), `followed`, `reverted` (a person went back to the earlier value),
    `kept` (a person asked to keep it), `held`, `changed` (a person chose another model;
    the record is old and the next write to this line replaces it).
    """
    out = {"state": "follow", "why": "default", "date": None, "from": None, "to": None, "skip": None}
    if not record:
        return out
    kind = record["kind"]
    out.update(date=record["date"], to=record.get("to"))
    out["from"] = record.get("from")
    if kind == "keep":
        return {**out, "state": "keep", "why": "kept"}
    if kind == "held":
        if model == record["from"]:
            return {**out, "state": "held", "why": "held", "skip": record["to"]}
        return {**out, "why": "changed"}
    if model == record["to"]:
        return {**out, "why": "followed"}
    if model == record["from"]:
        return {**out, "state": "keep", "why": "reverted"}
    return {**out, "why": "changed"}
