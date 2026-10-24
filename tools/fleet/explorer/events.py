"""Pure recent-event projection: newest first, one line, only rows a source actually carries.

An event is a fact a source already recorded: a registry row opening or ending an attempt, or
a peer-ledger message record. Nothing here declares a new event, and an attempt whose row does
not say what it was (stage / owner / frame) is shown by the plain word it does carry.
"""

from collections.abc import Mapping

from .output import cell_width, clip


def harness_abbr(harness):
    value = str(harness or "").lower()
    return {"claude": "CL", "codex": "CX", "opencode": "OC"}.get(
        value, "?" if not harness else value[:2].upper())

SEPARATOR = " · "
_PEER_WORD = {"notice": "메시지 알림", "steer": "메시지 지시"}


def _get(value, *names, default=None):
    for name in names:
        item = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if item is not None and item != "":
            return item
    return default


def clock(stamp):
    """HH:MM of an ISO timestamp as written; '--:--' when the source carries none."""
    text = str(stamp or "")
    return text[11:16] if len(text) >= 16 and text[13] == ":" else "--:--"


def _what(record):
    kind = _get(record, "kind", default="")
    if kind == "peer-ledger":
        return _PEER_WORD.get(_get(record, "record_type", default=""), "메시지")
    node = _get(record, "route_node") or _get(record, "worker_type") or "시도"
    status = str(_get(record, "status", default=""))
    verb = "시작" if status in ("open", "running") else "종료" if status else "기록"
    harness = _get(record, "harness")
    return f"{node} {verb}" + (f" {harness_abbr(harness)}" if harness else "")


def event_text(record):
    """'21:18 [3e] execute 시작 CX'."""
    tag = _get(record, "tag")
    stamp = _get(record, "time_kst", "timestamp", "time", "observed_at", default="")
    return " ".join(part for part in (clock(stamp), f"[{tag}]" if tag else "", _what(record)) if part)


def _identity(record):
    """What makes two rows the same record: every field that identifies it, so a repeat collapses."""
    stamp = str(_get(record, "time_kst", "timestamp", "time", default=""))
    return (str(_get(record, "kind", default="")), stamp,
            str(_get(record, "attempt_id", "identity", "event_id", "ref", default="")),
            str(_get(record, "status", "record_type", default="")),
            str(_get(record, "route_node", default="")),
            str(_get(record, "source", default="")), str(_get(record, "fixture_line", "line", default="")))


def build_events(records, width=None):
    """Event lines newest first, one per distinct source record (duplicates collapse)."""
    unique = {}
    for index, record in enumerate(records or ()):
        unique.setdefault(_identity(record), (index, record))
    ordered = sorted(unique.values(),
                     key=lambda item: (str(_get(item[1], "time_kst", "timestamp", "time", default="")), item[0]),
                     reverse=True)
    lines = [event_text(record) for _index, record in ordered]
    return tuple(clip(line, max(0, int(width))) for line in lines) if width is not None else tuple(lines)


def event_count(cols):
    """Wide screens show five events, narrow ones three."""
    return 5 if cols >= 125 else 3


def event_bar(records, width, limit=None):
    """One row: the newest events joined by ' · ', never cutting an event in half to make it fit."""
    limit = event_count(width) if limit is None else limit
    shown = []
    for line in build_events(records)[:limit]:
        candidate = SEPARATOR.join(shown + [line])
        if shown and cell_width(candidate) > width:
            break
        shown.append(line)
    return clip(SEPARATOR.join(shown), width)
