"""Open memory records and exact native ownership shared by tidy, Fleet and steward."""

from __future__ import annotations

import hashlib
import re

from harness_capabilities import HARNESSES


# Records that may be open work: the obligation and backlog types, a ``backlog`` tag, or a
# ``[marker]`` headline (the explorer reads the marker). Everything else is not read.
OPEN_TYPES = ("unresolved-obligation", "backlog", "task", "todo", "obligation")
# Kinds that record a finished outcome, an observation or a pointer, never open work by
# themselves; a ``backlog`` tag on them names the topic, not a pending item.
_FINISHED_TYPES = frozenset(("completion", "observation", "artifact-pointer", "feedback",
                             "user-correction", "decision"))
_FINISHED_TAGS = frozenset(("resolved", "done", "closed"))
_OPEN_MARKER_WORDS = ("백로그", "진행", "미확인", "부분 설치", "미해결", "수리")
_HEADLINE_MARKER = re.compile(r"^\s*\[([^\]]{1,40})\]")
OPEN_STAGES = ("doing", "waiting", "open")


def _refers_to_route(route_id, references):
    if not route_id:
        return False
    for reference in references or ():
        text = str(reference)
        if text == route_id or re.search(r"(?<![A-Za-z0-9])" + re.escape(route_id) +
                                         r"(?![A-Za-z0-9])", text):
            return True
    return False


def open_stage(record, routes=()):
    """How far a possibly-open record got: ``doing``, ``waiting``, ``open`` or None (not open work).

    Only what the record and the routes already say is used: its type, tags, headline marker
    (``[진행 중]``, ``[설치 완료·실사용 미확인]``, ``[해결 확인]`` …) and an exact route link.
    An obligation or backlog record with neither stays open on record, never "in progress".
    """
    headline = record.get("headline")
    match = _HEADLINE_MARKER.match(headline if isinstance(headline, str) else "")
    marker = match.group(1) if match else ""
    if "해결" in marker and "미해결" not in marker:
        return None
    rtype = str(record.get("type") or "").casefold()
    tags = {str(tag).strip("[]\"' ").casefold() for tag in record.get("tags") or ()}
    if rtype in _FINISHED_TYPES or tags & _FINISHED_TAGS:
        return None
    if not (rtype in OPEN_TYPES or "backlog" in tags
            or any(word in marker for word in _OPEN_MARKER_WORDS)):
        return None
    refs = tuple(record.get("links") or ()) + tuple(record.get("artifact_refs") or ())
    linked = [route for route in routes if _refers_to_route(str(route.get("id") or ""), refs)]
    if "진행" in marker or any(route.get("state") in ("working", "blocked") or
                              any(stage.get("st") == "active" for stage in route.get("stages") or ())
                              for route in linked):
        return "doing"
    if "미확인" in marker or "부분 설치" in marker or any(
            route.get("state") == "done" and route.get("outcome") == "success" for route in linked):
        return "waiting"
    return "open"


_OWNER_PREFIX = "session-owner:"
_OWNER_LINK = re.compile(_OWNER_PREFIX + r"[0-9a-f]{64}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")


def owner_identity(row):
    """The exact (harness, session ID) from a resolved target or an owner declaration."""
    if not isinstance(row, dict) or row.get("unresolved"):
        return None
    return owner_key(row.get("harness"), row.get("session_id"))


def owner_key(harness, sid):
    if harness in HARNESSES and isinstance(sid, str) and _SESSION_ID.fullmatch(sid):
        return harness, sid
    return None


def owner_link(owner):
    """Use the existing links field without persisting or displaying a raw session ID."""
    if owner is None:
        return None
    return _OWNER_PREFIX + hashlib.sha256(":".join(owner).encode()).hexdigest()


def record_owner(record):
    """Only a single explicit owner link binds a record. Mentions and route links do not."""
    owners = {link for link in record.get("links") or ()
              if isinstance(link, str) and _OWNER_LINK.fullmatch(link)}
    return owners.pop() if len(owners) == 1 else None
