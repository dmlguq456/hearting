"""The NOW popup's lines: ``timeline.rows`` drawn as a list, ``timeline.passage`` drawn as a page.

Only drawing is here. What the rows are, how a long list folds (``timeline.fold``) and what a
passage reads (``timeline.passage``) belong to ``timeline`` and are called, not made again. Every
line is a list of ``(text, style key)`` segments in the keys the panel and the popups already use
(``dim``, ``loc_repo``, ``hb_<harness>``, ``g_blocked``, ``lvl_g`` and the route glyph keys), so no
new color is made here. Nothing is read from disk, written or sent to a harness.
"""
from __future__ import annotations

import time

from . import timeline
from .grid import cell_width

TIME_CELLS = 5                 # `HH:MM`
GAP = "  "                     # two cells between the time and the mark
NOW_INDENT = "    "            # a NOW has no mark: the mark's two cells and two more
SELECTABLE = frozenset({"prompt", "now", "route", "question", "message"})
DIVIDER_MARKS = {"clear": "●", "compact": "○", "handover": "◆"}   # Fleet HISTORY's marks
PAGE_HEAD_ROWS = 40            # a page too long for one answer keeps this many rows from its top
REASONS = frozenset({"invalid-target", "no-transcript", "span-reversed", "transcript-shorter",
                     "no-cursor", "prompt-not-found", "cursor-unit-changed", "unreadable"})


def _word(name, **fields):
    from ..config import ui_text
    return ui_text("fleet.session_panel." + name, **fields)


def cells(text):
    return sum(cell_width(char) for char in text)


def _clip(text, width):
    if width <= 0:
        return ""
    if cells(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        step = cell_width(char)
        if used + step > width - 1:
            break
        out.append(char)
        used += step
    return "".join(out) + "…"


def _line(lead, body, width, right=None):
    """``lead`` whole, ``body`` cut with ``…`` to what is left, ``right`` against the right edge.

    ``lead``, ``body`` and ``right`` are lists of ``(text, key)``. The time and mark in ``lead`` and
    the word in ``right`` stay when the body is cut."""
    right = [part for part in (right or ()) if part[0]]
    used = sum(cells(text) for text, _key in lead)
    tail = sum(cells(text) for text, _key in right)
    room = max(0, width - used - (tail + 1 if tail else 0))
    out, left = list(lead), room
    for text, key in body:
        if left <= 0:
            break
        shown = _clip(text, left) if cells(text) > left else text
        out.append((shown, key))
        left -= cells(shown)
        if shown != text:
            break
    if tail:
        spare = width - sum(cells(text) for text, _key in out) - tail
        if spare >= 1:
            out.append((" " * spare, None))
            out.extend(right)
        elif spare >= 0:
            out.extend(right)
    return out


def _clock(at):
    return time.strftime("%H:%M", time.localtime(at))


def _glyph_key(marker):
    try:
        from ..session_line import route_glyph
        return route_glyph(marker)[1]
    except Exception:  # noqa: BLE001 -- an unknown key draws in the terminal's own look
        return "dim"


def _elapsed(seconds):
    try:
        from ..session_line import elapsed_text
        return elapsed_text(seconds)
    except Exception:  # noqa: BLE001
        return ""


def marker(row):
    """The word that says what an event row is (``route``, the question word, a message's kind), else ``""``."""
    kind = row.get("kind")
    if kind == "route":
        return "route"
    if kind == "question":
        return _word("now_question")
    if kind == "message":
        return str(row.get("message_kind") or "")
    return ""


def marker_width(story):
    """The widest event marker of ``story``, in cells: the text of every event row starts in the same column."""
    return max((cells(marker(row)) for row in story or ()), default=0)


def _marked(row, mark):
    word = marker(row)
    return word + " " * max(0, mark - cells(word)) + "  "


def draw_row(row, width, *, folded=0, mark=0):
    """One story row as ``(segments, meta)``; ``meta`` is what the host keeps for the line.

    ``mark`` is the width the event markers are filled to (see ``marker_width``)."""
    kind = row.get("kind")
    meta = {"key": row.get("key") or "", "kind": kind, "select": kind in SELECTABLE}
    if kind == "date":
        label = (_word("now_today") + " " if row.get("today") else "") + str(row.get("text") or "")
        rest = max(0, width - cells(label) - 1)
        return [(label, "dim"), (" " + "─" * rest, "dim")], meta
    stamp = (_clock(row["at"]), "dim")
    harness = row.get("harness") if row.get("harness") in timeline.HARNESSES else "other"
    if kind == "divider":
        mark = DIVIDER_MARKS.get(row.get("sub"), "●")
        word = _word("now_divider_" + str(row.get("sub")))
        return _line([stamp, (GAP, None), (mark + " ", "dim")], [(word, "dim")], width), meta
    suffix = []
    if folded:
        suffix.append((_word("now_folded", count=folded), "dim"))
    if kind == "prompt":
        lead = [stamp, (GAP, None), (">", "hb_" + harness), (" ", None)]
        if row.get("answer"):
            lead.append((_word("now_answer") + " · ", "dim"))
        meta["read"] = True
        return _line(lead, [(row.get("text") or "", "loc_repo")], width, suffix), meta
    if kind == "now":
        current = row.get("current") is True
        right = suffix + ([(_word("now_current"), "dim")] if current else [])
        if len(right) == 2:
            right = [(right[0][0] + " · " + right[1][0], "dim")]
        meta["read"] = True
        return _line([stamp, (GAP, None), (NOW_INDENT, None)],
                     [(row.get("text") or "", "loc_repo" if current else "dim")], width, right), meta
    if kind == "route":
        if row.get("sub") == "end":
            passed = row.get("result") == "passed"
            glyph, key = ("✓", _glyph_key("done")) if passed else ("✕", _glyph_key("dead"))
            tail = [(" · " + (_word("now_route_passed") if passed else _word("now_route_unproven")), "dim")]
            took = row.get("elapsed")
            right = [(_elapsed(took / 60), "dim")] if isinstance(took, (int, float)) else []
        else:
            glyph, key = "⠏", _glyph_key("working")
            # 11-20: a unit route has no intensity to read (`unit`, not `unit · quick`); the story row keeps the record's value.
            more = [part for part in (row.get("capability"), None if row.get("capability") == "unit" else row.get("intensity")) if part]
            tail = [(" · " + " · ".join(more), "dim")] if more else []
            right = []
        meta["go"] = "routes:" + str(row.get("route_id") or "")
        return _line([stamp, (GAP, None), (glyph, key), (" " + _marked(row, mark), "dim")],
                     [(row.get("text") or "", "loc_repo"), *tail], width, right), meta
    if kind == "question":
        sub = row.get("sub")
        tail = [(" · " + _word("now_question_" + str(sub)), "dim")] if sub in ("answered", "closed") else []
        meta["go"] = "questions"
        return _line([stamp, (GAP, None), ("◑", "g_blocked"), (" " + _marked(row, mark), "dim")],
                     [(row.get("text") or str(row.get("id") or ""), "loc_repo"), *tail], width), meta
    if kind == "message":
        arrow = "←" if row.get("sub") == "received" else "→"
        body = [((str(row.get("who") or "") + " ") if row.get("who") else "", "loc_repo"),
                (row.get("text") or "", "loc_repo")]
        if row.get("delivery"):
            body.append((" · " + str(row["delivery"]), "dim"))
        meta["go"] = "messages"
        return _line([stamp, (GAP, None), (arrow, "lvl_g" if arrow == "←" else "loc_repo"),
                      (" " + _marked(row, mark), "dim")], body, width), meta
    return [(str(row.get("text") or ""), "dim")], {**meta, "select": False}


def counts(story):
    return (sum(1 for row in story if row.get("kind") == "prompt"),
            sum(1 for row in story if row.get("kind") == "now"))


def list_view(story, width, room, selected=""):
    """The list a popup of ``room`` lines and ``width`` cells shows, oldest first.

    ``selected`` is the key of the row the person stands on; without one (or a key that is gone)
    the popup starts on the current NOW, else the last row that can be stood on. Returns
    ``{"rows", "meta", "start", "folded", "title"}``: ``rows`` are segment lists, ``meta`` the
    host's note per row (``key``, ``kind``, ``select``, ``read`` | ``go``), ``start`` the key to stand
    on, ``folded`` whether the list was folded to fit the room."""
    story = list(story or ())
    if not story:
        return {"rows": [[(_word("now_empty"), "dim")]], "meta": [{"key": "", "kind": "empty", "select": False}],
                "start": "", "folded": False, "title": ""}
    index = next((i for i, row in enumerate(story) if row.get("key") == selected), None)
    if index is None:
        index = next((i for i, row in enumerate(story) if row.get("current") is True), None)
    if index is None:
        index = next((i for i in range(len(story) - 1, -1, -1) if story[i].get("kind") in SELECTABLE), None)
    shown = timeline.fold(story, room, index)
    mark = marker_width(story)
    rows, meta = [], []
    for item in shown:
        drawn, note = draw_row(story[item["index"]], width, folded=item.get("folded") or 0, mark=mark)
        rows.append(drawn)
        meta.append(note)
    prompts, nows = counts(story)
    return {"rows": rows, "meta": meta, "start": story[index]["key"] if index is not None else "",
            "folded": len(shown) < len(story),
            "title": _word("now_title_counts", prompts=prompts, nows=nows)}


def read_reason(reason):
    name = reason if reason in REASONS else "unreadable"
    return _word("now_reason_" + name.replace("-", "_"))


def page_view(row, width):
    """The conversation ``row`` (a prompt or NOW) stands for, as the popup reads it in place.

    Returns ``{"rows", "title", "ok", "reason"}``: ``rows`` are segment lists. A passage that cannot
    be read gives one dim line in the person's words, from the reason word the layer gave."""
    kind = "NOW" if row.get("kind") == "now" else ">"
    title = _clock(row["at"]) + " " + kind
    result = timeline.passage(row.get("open"), width)
    if not result.get("ok"):
        return {"rows": [[(read_reason(result.get("reason")), "dim")]], "title": title, "ok": False,
                "reason": result.get("reason") or "unreadable"}
    harness = row.get("harness") if row.get("harness") in timeline.HARNESSES else "other"
    out = []
    drawn = [[(text, key) for text, key in spans] for spans, _tint in result.get("rows") or ()]
    head = str(result.get("head") or "")
    # A passage that opens with the prompt itself needs no head line above it.
    if head and not any(head[:20] in "".join(text for text, _key in line) for line in drawn[:3]):
        out.append([(">", "hb_" + harness), (" ", None), (_clip(head, max(1, width - 2)), "loc_repo")])
    out.extend(drawn)
    return {"rows": out or [[(read_reason("no-transcript"), "dim")]], "title": title, "ok": True, "reason": ""}
