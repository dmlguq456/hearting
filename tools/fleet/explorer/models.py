"""Read-only model configuration view and viewer-local selection controls."""
from collections.abc import Mapping

from ..config import ui_text
from . import follow as follow_view
from .grid import CellGrid, cell_width, clip_cells

X = 34
GAP = 3


def _cut(text, width):
    """``text`` cut to ``width`` cells (no ellipsis): the part that would be drawn off the screen is not asked for."""
    used, out = 0, []
    for char in str(text):
        w = cell_width(char)
        if used + w > width:
            break
        out.append(char)
        used += w
    return "".join(out)


def _command_lines(command, width):
    """Wrap at spaces, splitting a token only when it exceeds a full row."""
    if width < 2:
        return [command]
    lines = []
    while cell_width(command) > width:
        line = _cut(command, width)
        boundary = command.rfind(" ", 0, len(line) + 1)
        if boundary > 0:
            line = line[:boundary]
        lines.append(line)
        command = command[len(line):].lstrip(" ")
    return lines + [command]


def _dict(value):
    return value if isinstance(value, Mapping) else {}


def _profiles(data):
    return tuple(data.get("profiles", ()))


def _write(grid, x, y, text, key="dim", limit=None):
    return grid.put(x, y, str(text), key, limit=limit)


def _section(grid, x, width, y, title, meta=""):
    """A section head in the shell panel's form: the name in dim upper case, the rule to the edge."""
    cursor = grid.put(x, y, title, "label")
    if meta:
        cursor = grid.put(cursor, y, _cut("  " + meta, grid.cols - cursor), "dim")
    grid.put(cursor, y, " ", "branch_s")
    for col in range(cursor + 1, min(grid.cols, x + width + 1)):
        grid.put(col, y, "─", "dim")


def _chips(grid, x, y, items, current, chosen, action_key, right, labels=None):
    labels = labels or {}
    for item in items:
        active = item == (chosen if chosen is not None else current)
        shown_value = str(labels.get(item, item))
        shown = ("[ " + shown_value + " ]") if active else ("  " + shown_value + "  ")
        width = len(shown)
        if x + width > right:
            break
        grid.add_hit(y, x, x + width, ("ui", action_key, item))
        x = _write(grid, x, y, shown, "chip_on" if active else "title_dim") + 1
    return x


def _wrapped_chips(grid, x, y, items, current, chosen, key, right):
    """Keep every short choice visible and clickable, wrapping at the pane edge."""
    start = x
    for item in items:
        size = cell_width(str(item)) + 4
        if x > start and x + size > right:
            x, y = start, y + 1
        if x + size <= right:
            x = _chips(grid, x, y, (item,), current, chosen, key, right)
    return x, y


# ---- model follow: marks beside the table's cells, the lines under the table, the chosen cell's box ----

_MARK_KEY = {"^": "success", "=": "lvl_y", "!": "g_blocked"}
_GROUP_KEY = {"followed": "success", "next": "soft", "held": "g_blocked", "proposal": "lvl_y"}
_UNDO = "[ undo ]"
_OPEN = "ui:fopen:"


def _plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _follow(vm, data):
    """The follow facts for this frame, or None (nothing about following is drawn)."""
    try:
        return follow_view.Follow.read(data, getattr(vm, "clock", None))
    except Exception:
        return None


def _summary_plan(view, profiles, shown, values):
    """The rows under the table: ``[{"kind": group|item|note|legend, ...}]``, [] when there is nothing to say."""
    try:
        name_of = lambda model: shown.get(model, model)               # noqa: E731
        out = []
        for name, rows in view.groups(profiles):
            opened = values.get(_OPEN + name) == "open"
            one = rows[0] if len(rows) == 1 else None
            fit = None
            if name == "followed":
                date = max(row["cell"]["since"]["date"] for row in rows)[5:]
                moved = len(view.moved_lines(rows))
                text = f"followed {date} · " + (f"{one['harness']} {one['target']}" if one
                                                else _plural(len(rows), "cell") + (
                                                    f" + {_plural(moved, 'linked line')}" if moved else ""))
            elif name == "next":
                linked = len(view.linked(rows))
                text = "next " + (f"{one['harness']} {one['target']}" if one else
                                  _plural(len(rows), "cell") + (f" + {_plural(linked, 'linked line')}"
                                                                if linked else ""))
                note, short = view.table_note(rows), view.table_note(rows, short=True)
                if not note:                       # no read-only table: the line promises the next update
                    text += " · on the next update or refresh"
                else:                              # the notice first: a narrow row cuts the promise, never the notice
                    fit = (text + note, [(text, note, 12), (text, short, 12), short.removeprefix(" · ")])
                    text += note if view.every_read_only(rows) else note + " · on the next update or refresh"
            elif name == "held":
                text = "held " + (f"{one['harness']} {one['target']} · back on {name_of(one['cell']['model'])}"
                                  if one else _plural(len(rows), "cell"))
            else:
                following = one["cell"]["next"] if one else None
                reason = f" ({following['reason']})" if one and following["reason"] else ""
                text = "proposal " + (f"{one['harness']} {one['target']} · {name_of(following['model'])}{reason}"
                                      if one else _plural(len(rows), "cell"))
            out.append({"kind": "group", "name": name, "text": text, "opened": opened, "fit": fit})
            if opened:
                out += [{"kind": "item", "text": row["text"], "profile": row["profile"],
                         "harness": row["harness"]} for row in rows]
                if name == "next":
                    out += [{"kind": "item", "text": line, "profile": None, "harness": None}
                            for line in view.linked(rows)]
                elif name == "followed":
                    out += [{"kind": "item", "text": "linked " + follow_view.line_label(key), "profile": None,
                             "harness": None} for key in view.moved_lines(rows)]
        out += [{"kind": "note", "text": line} for line in view.cli_lines()]
        return out
    except Exception:
        return []


def _draw_summary(grid, plan, legend, x, y, right):
    """Draw the summary rows from ``y``; returns how many rows were used."""
    room = right - x + 1
    start = y
    for item in plan:
        kind = item["kind"]
        if kind == "group":
            glyph = follow_view.GLYPH[item["name"]]
            grid.put(x, y, glyph, _GROUP_KEY[item["name"]], limit=right + 1)
            text = item["text"]
            if item["fit"] and cell_width(text) > room - 4:       # a read-only notice: cut the promise, then the head
                must, fallback = item["fit"]
                text = clip_cells(text, room - 4) if cell_width(must) <= room - 4 else _fit(fallback, room - 4)
            end = grid.put(x + 2, y, clip_cells(text, room - 4), "soft", limit=right + 1)
            grid.put(end + 1, y, "▾" if item["opened"] else "▸", "soft", limit=right + 1)
            grid.add_hit(y, x, right + 1, ("ui", "fopen:" + item["name"], "closed" if item["opened"] else "open"))
        elif kind == "item":
            grid.put(x + 2, y, clip_cells(item["text"], room - 2), "dim", limit=right + 1)
            if item["profile"]:
                grid.add_hit(y, x + 2, right + 1, ("ui", "sel", f"{item['profile']}/{item['harness']}"))
        else:
            grid.put(x, y, clip_cells(item["text"], room), "dim", limit=right + 1)
        y += 1
    if legend:
        cursor = x
        for glyph, word in legend:
            if cursor > x:
                cursor += 3
            cursor = grid.put(cursor, y, glyph, _MARK_KEY[glyph], limit=right + 1)
            cursor = grid.put(cursor, y, " " + word, "dim", limit=right + 1)
        y += 1
    return y - start


def _fit(candidates, room):
    """The first candidate that fits in ``room`` cells, else the last one cut with an ellipsis.

    A candidate may be ``(name, tail[, keep])``: when it does not fit whole, the ``name`` is cut and
    the ``tail`` stays, as long as the name keeps ``keep`` (8) cells; else the next candidate is tried."""
    for text in candidates:
        if isinstance(text, tuple):
            name, tail, keep = (*text, 8)[:3] if len(text) == 2 else text
            left = room - cell_width(tail)
            if left >= min(keep, cell_width(name)):
                return clip_cells(name, left) + tail
        elif cell_width(text) <= room:
            return text
    last = candidates[-1]
    # A tuple candidate is `(name, tail[, keep])`: only the name and the tail are text.
    return clip_cells(last if isinstance(last, str) else "".join(last[:2]), max(1, room))


def _box_plan(view, harness, profile, shown, values, table_effort=None):
    """What the chosen cell's box adds: status lines, the ``follow`` row and the draft, or None."""
    try:
        cell = view.cell(harness, profile)
        if cell is None:
            return None
        name_of = lambda model: shown.get(model, model)               # noqa: E731
        since, following = cell["since"], cell["next"]
        lines = []
        if cell["state"] == "follow" and cell["why"] == "followed" and since:
            date, before = since["date"], name_of(since["from"] or "?")
            back = follow_view.effort_after_undo(cell, table_effort)       # what the cell shows once put back
            effort = f" · {back}" if back else ""
            lines.append(("followed", [f"{date} · from {before}{effort}", f"{date} · from {before}",
                                       f"{date[5:]} · from {before}"], _UNDO))
        elif cell["state"] == "keep" and since:
            date = since["date"]
            if cell["why"] == "reverted":
                undone = name_of(since["to"] or "?")
                lines.append(("kept", [f"{date} · undone from {undone}", f"{date[5:]} · undone from {undone}"], None))
            else:
                lines.append(("kept", [date], None))
        elif cell["state"] == "held":
            date = (since or {}).get("date") or "?"
            refused = name_of(cell["held"] or (since or {}).get("to") or "?")
            lines.append(("held", [f"{date} · {refused} rejected · back on {name_of(cell['model'])}",
                                   f"{date[5:]} · {refused} rejected · back on {name_of(cell['model'])}"], None))
        if following and cell["state"] != "keep":
            idle = view.table_note([{"harness": harness}])
            ro = ((name_of(following["model"]), idle), (name_of(following["model"]), " · read-only"), "read-only")
            if following["branch"] == "will-follow":
                lines.append(("next", list(ro) if idle else
                                      [f"{name_of(following['model'])} · on the next update or refresh"], None))
            elif following["branch"] == "proposal":
                reason = f" · {following['reason']}" if following["reason"] else ""
                lines.append(("proposal", [f"{name_of(following['model'])}{reason} · choose it yourself",
                                           f"{name_of(following['model'])}{reason}"], None))
            elif following["branch"] == "revert":
                lines.append(("next", list(ro) if idle else
                                      [f"{name_of(following['model'])} · back on the next update or refresh"], None))
        word = values.get("ui:fol")
        undo = follow_view.undo_request(cell)
        requested = None
        if word == "keep" and cell["state"] != "keep":
            requested = "keep"
        elif word == "follow" and cell["state"] in ("keep", "held"):
            requested = "follow"
        elif word == "undo" and undo:
            requested = undo
        moved = len((since or {}).get("lines") or ())
        then = ["then Keep" + (f" · linked line{'s' if moved > 1 else ''} stay{'' if moved > 1 else 's'}"
                               if moved else ""), "then Keep"]
        return {"cell": cell, "lines": lines, "rows": len(lines) + 1, "word": word, "requested": requested,
                "undo": undo, "kept": cell["state"] == "keep", "then": then,
                "back_effort": follow_view.effort_after_undo(cell, table_effort),
                "skips": name_of(cell["held"]) if cell["state"] == "held" and cell["held"] else None}
    except Exception:
        return None


def _draw_box_follow(grid, box, harness, bx, y, right):
    """The status lines and the ``follow`` row of the chosen cell, from row ``y``; one row each."""
    for index, (label, candidates, chip) in enumerate(box["lines"]):
        grid.put(bx - 9, y, label, "label", limit=right)
        chip_x = right - 13
        room = (chip_x - 2 if chip else right - 1) - bx
        text = _fit(candidates, room)
        grid.put(bx, y, text, "soft", limit=right)
        if chip:
            drafted = box["word"] == "undo" and box["requested"]
            grid.put(chip_x, y, _UNDO, "chip_on" if drafted else "title_dim", limit=right)
            grid.add_hit(y, chip_x, chip_x + len(_UNDO), ("ui", "fol", "undo"))
        y += 1
    grid.put(bx - 9, y, "follow", "label", limit=right)
    chosen = box["word"] if box["word"] in ("keep", "follow") and box["requested"] else None
    end = _chips(grid, bx, y, ("follow", "keep"), "keep" if box["kept"] else "follow", chosen, "fol",
                 right - 2, {"follow": "Follow", "keep": "Keep"})
    if box["skips"]:
        grid.put(end + 1, y, "skips " + box["skips"], "dim", limit=right - 1)
    elif box["requested"] and box["word"] == "undo":      # the end of this row: the list below keeps its own note row
        room = right - 1 - (end + 2)
        text = next((item for item in box["then"] if cell_width(item) <= room), None)
        if text:
            grid.put(end + 2, y, text, "dim", limit=right - 1)


PICK_ROWS = 8          # visible rows of the model list; the box grows by this many rows, the list scrolls


def _same_model(item, model):
    """Native catalog identity includes the provider; never invent a missing prefix."""
    if item is None or model is None:
        return False
    return str(item) == str(model)


def _chips_fit(items, labels, room):
    return sum(len(str(labels.get(item, item))) + 5 for item in items) <= room


def model_matches(items, query, labels=None):
    """``items`` whose id or shown name holds every space-separated word of ``query`` (case-insensitive)."""
    labels = labels or {}
    words = str(query or "").lower().split()
    return tuple(item for item in items
                 if all(word in (str(item) + " " + str(labels.get(item, ""))).lower() for word in words))


def _provider_counts(items):
    counts = {}
    for item in items:
        provider = str(item).partition("/")[0] if "/" in str(item) else ""
        if provider:
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def _model_picker(grid, x, y, right, items, current, chosen, labels, values, offsets, source=None):
    """A filter row and a scrolling list holding every model: each row is a click target, the
    wheel scrolls it, and the row hit ``("models.find", count, cursor item)`` lets keys move and pick."""
    query = values.get("ui:mfind", "")
    editing = values.get("ui:mfocus") == "1"
    found = model_matches(items, query, labels)
    try:
        cursor = max(0, min(len(found) - 1, int(values.get("ui:mcursor", -1))))
    except (TypeError, ValueError):
        cursor = 0
    if "ui:mcursor" not in values:
        active = chosen if chosen is not None else current
        cursor = next((index for index, item in enumerate(found) if _same_model(item, active)), 0)
    top_max = max(0, len(found) - PICK_ROWS)
    top = max(0, min(top_max, int(offsets.get("mpick", max(0, cursor - PICK_ROWS + 1)))))
    field = 26
    grid.put(x, y, "⌕ ", "line" if editing else "dim", limit=right)
    shown = clip_cells(query, field - 1) + ("▏" if editing else "")
    end = grid.put(x + 2, y, shown or "type to filter", "soft" if editing else "dim", limit=right)
    for col in range(end, x + 2 + field):
        grid.put(col, y, "·" if not editing else " ", "dim", limit=right)
    grid.add_hit(y, x, x + 2 + field, ("models.find", len(found), found[cursor] if found else "", cursor, top,
                                             PICK_ROWS))
    cursor_x = grid.put(x + 2 + field, y, f"  {len(found)}/{len(items)}", "soft", limit=right)
    hint = ("  ↑↓ PgUp/PgDn · Enter pick · Esc done" if editing else "  click or e to search")
    grid.put(cursor_x, y, hint, "dim", limit=right)
    counts = _provider_counts(items)
    about = " · ".join(f"{name} {count}" for name, count in counts.items())
    if source:
        about = (about + "  · " if about else "") + source
    if about:
        grid.put(x, y - 1, about, "dim", limit=right)
    active = chosen if chosen is not None else current
    for row in range(PICK_ROWS):
        yy = y + 1 + row
        index = top + row
        if index >= len(found):
            if row == 0:
                grid.put(x + 2, yy, "no model matches the filter", "dim", limit=right)
            continue
        item = found[index]
        on = _same_model(item, active)
        here = _same_model(item, current)
        mark = "▶ " if editing and index == cursor else "  "    # 11-27: the place, as MODELS' own selected cell reads it
        cursor_x = grid.put(x - 2, yy, mark, "soft", limit=right)
        text = str(item)
        label = str(labels.get(item, ""))
        cursor_x = grid.put(cursor_x, yy, ("[ " + text + " ]") if on else ("  " + text + "  "),
                            "chip_on" if on else "title_dim", limit=right - 12)
        if label and label != text and label != text.partition("/")[2]:
            cursor_x = grid.put(cursor_x + 1, yy, label, "dim", limit=right - 12)
        if here:
            grid.put(cursor_x + 1, yy, "now", "label", limit=right - 2)
        grid.add_hit(yy, x - 2, right - 8, ("ui", "model", item))
        grid.add_hit(yy, x - 2, right - 1, ("scroll", "mpick", top_max, -1))
    if top_max:
        grid.put(right - 8, y + 1, "▲" if top else " ", "dim", limit=right)
        grid.put(right - 8, y + PICK_ROWS, "▼" if top < top_max else " ", "dim", limit=right)
        span = f"{top + 1}-{min(len(found), top + PICK_ROWS)}"
        grid.put(right - 2 - len(span), y + PICK_ROWS // 2, span, "dim", limit=right)
    return PICK_ROWS


def _catalog_note(data, harness):
    source = _dict(_dict(data.get("catalog_sources")).get(harness))
    if source.get("state") != "available":
        return ""
    if str(source.get("source") or "").endswith("#/api/model"):
        return "from runtime catalog, callability not checked"
    return "from cache, callability not checked"


def _model_cell(grid, x, y, harness, value, shown, effort_keys, room, limit=None):
    edge = x + room if limit is None else min(x + room, limit)
    if value is None:
        return _write(grid, x, y, "— not set", "dim", limit=edge)
    model, effort = (tuple(value) + (None, None))[:2]
    name = shown.get(str(model), model)
    try:
        from .. import render
        model_key = render._model_key(model, harness=harness)
    except Exception:
        model_key = "fam_other"
    cursor = _write(grid, x, y, clip_cells(str(name), room - 9), model_key, limit=limit)
    cursor = _write(grid, cursor, y, " · ", "dim", limit=limit)
    return _write(grid, cursor, y, effort or "·", effort_keys.get(effort, "dim"), limit=edge)


def _draw_context(grid, data, state, x, y, width):
    right = min(grid.cols - 1, x + width)
    cursor = grid.put(x, y, "CONTEXT & TIDY", "label")
    cursor = grid.put(cursor, y, "  input tokens · new sessions only", "dim", limit=right)
    grid.put(cursor, y, " ", "branch_s")
    for col in range(cursor + 1, right + 1):
        grid.put(col, y, "─", "line")
    y += 2
    units = _dict(data.get("token_units"))
    context = _dict(data.get("context"))
    source = _dict(data.get("context_source"))
    tidy = tuple(data.get("tidy") or (500000, 700000))        # the configured points when the view carries none
    chosen1 = state.fold_map().get("ui:tidy1")
    chosen2 = state.fold_map().get("ui:tidy2")
    t1 = units.get(chosen1, tidy[0] if tidy else None)
    t2 = units.get(chosen2, tidy[1] if len(tidy) > 1 else None)
    bar_width = max(8, width - 22)
    bx = x + 11
    for amount, label in ((0, "0"), (500000, "500K"), (1000000, "1M")):
        pos = bx + round(amount / 1e6 * (bar_width - 1)) - (len(label) // 2 if amount else 0)
        grid.put(pos, y, label, "dim", limit=right)
    y += 1
    cmds, warnings = [], []
    for harness in ("claude", "codex", "opencode"):
        selected = state.fold_map().get("ui:ctx:" + harness)
        if selected not in units:
            selected = None
        window = units.get(selected, context.get(harness))
        grid.put(x + 1, y, harness, "hb_" + harness)
        cutoff1, cutoff2 = ((t1 or 0) / 1e6, (t2 or 0) / 1e6)
        for index in range(bar_width):
            point = (index + .5) / bar_width
            key = "dim" if window is not None and point > window / 1e6 else (
                "g_blocked" if point >= cutoff2 else "g_idle" if point >= cutoff1 else "g_work_off")
            glyph = "┄" if window is not None and point > window / 1e6 else "━"
            grid.put(bx + index, y, glyph, key, limit=right)
        if window is not None:
            grid.put(bx + min(bar_width - 1, round(window / 1e6 * (bar_width - 1))), y, "┃", "soft", limit=right)
            window_label = "1M" if window >= 1000000 else f"{window // 1000}k"
        else:
            window_label = "·"
        grid.put(bx + bar_width + 2, y, window_label, "soft", limit=right)
        y += 1
        grid.put(x + 3, y, "window", "label")
        choices = ("400k", "600k", "800k", "1M")
        current = next((label for label in choices if units.get(label) == context.get(harness)), None)
        chip_end, y = _wrapped_chips(grid, bx, y, choices, current, selected,
                                     "ctx:" + harness, right - 1)
        source_text = source.get(harness, "·")
        if source_text:
            grid.put(chip_end + 1, y, clip_cells(source_text, x + width - chip_end - 2), "dim", limit=right)
        if selected:
            amount = units[selected]
            cmds.append((harness, amount,
                         f"hearting model set {harness} context-window {amount}"))
        if window is not None and t2 is not None and t2 >= window:
            warnings.append(harness)
        y += 2
    grid.put(x + 1, y, "tidy+clear notice points", "soft", limit=right)
    y += 1
    grid.put(x + 3, y, "notice", "label")
    current1 = next((label for label, amount in units.items() if amount == tidy[0]
                     and label.endswith("K")), None)
    _, y = _wrapped_chips(grid, bx, y, ("300K", "400K", "500K", "600K"),
                          current1, chosen1, "tidy1", right - 1)
    y += 1
    grid.put(x + 3, y, "urgent", "label")
    current2 = next((label for label, amount in units.items() if amount == tidy[1]
                     and label.endswith("K")), None)
    _, y = _wrapped_chips(grid, bx, y, ("600K", "650K", "700K", "750K"),
                          current2, chosen2, "tidy2", right - 1)
    y += 1
    grid.put(x + 3, y, ui_text("fleet.models.tidy_shared"), "dim", limit=right)
    y += 1
    if warnings:
        grid.put(x + 3, y, "tidy point meets or exceeds window: " + ", ".join(warnings), "g_blocked", limit=right)
        y += 1
    for harness, amount, command in cmds:
        cursor = grid.put(x + 3, y, "$ " + command, "soft", limit=right - 14)
        grid.put(cursor + 2, y, "[ apply ]", "chip_on", limit=right)
        grid.add_hit(y, cursor + 2, cursor + 11,
                     ("models.apply", harness, "context-window", amount, False))
        y += 1
    return y


_EDITED_FIELDS = ("strategy:", "weights:", "field tilt:", "affinity:")   # read-only lines the editor replaces
_STRATEGY_NOTES = {
    "balanced": "remaining usage sets each harness's share; recent attempts fill it",
    "capacity-aware": "most remaining usage first; unknown usage is skipped",
    "least-recent-attempts": "plain rotation by recent attempts",
}
_ALLOC_CHOICES = {
    "window": (10, 20, 30, 60, 100),
    "usage_gate_used_percent": (70, 80, 85, 90, 95, 100),
    "usage_headroom_exponent": (1, 2, 3, 4),
    "depth_affinity_weight": (0.5, 0.65, 0.85, 1.0),
    "harness_weights": (0.2, 0.3, 0.5, 0.7, 1.0),
}


def _alloc_label(name):
    parent, _dot, child = name.partition(".")
    return {"strategy": "strategy", "usage_gate_used_percent": "usage gate %",
            "usage_headroom_exponent": "usage effect", "window": "window",
            "depth_affinity_weight": "prefer strength", "harness_weights": "share " + child,
            "depth_affinity": child + " prefers"}.get(parent, name)


def _num(value):
    if isinstance(value, float):
        text = f"{value:g}"
        return text if "." in text or "e" in text else text + ".0"
    return str(value)


def _alloc_options(name, current):
    options = list(_ALLOC_CHOICES[name.partition(".")[0]])
    if isinstance(current, (int, float)) and not isinstance(current, bool) and current not in options:
        options.append(current)
    return tuple(_num(item) for item in sorted(options))


def allocation_changes(allocation, folds):
    """``((key, old text, new text), ...)`` the pending chips differ from the current file on."""
    values = _dict(allocation.get("values"))
    current = {name: _num(values.get(name)) for name in
               ("strategy", "window", "usage_gate_used_percent", "usage_headroom_exponent",
                "depth_affinity_weight")}
    for role in ("owner", "worker"):
        current["depth_affinity." + role] = str(_dict(values.get("depth_affinity")).get(role) or "none")
    for harness in allocation.get("harnesses") or ():
        current["harness_weights." + harness] = _num(float(_dict(values.get("harness_weights")).get(harness, 1.0)))
    changes = []
    for name, old in current.items():
        new = folds.get("ui:alloc:" + name)
        if new is not None and new != old:
            changes.append((name, old, new))
    return tuple(changes)


def _alloc_row(grid, x, y, right, label, name, options, current, folds, note="", inert=""):
    grid.put(x + 3, y, label, "label", limit=right)
    cursor = _chips(grid, x + 20, y, options, current, folds.get("ui:alloc:" + name), "alloc:" + name, right)
    if inert:
        grid.put(cursor + 1, y, "· " + inert, "lvl_y", limit=right)
    elif note:
        grid.put(cursor + 1, y, note, "dim", limit=right)
    return y + 1


_FIELD_TABLE_FOLD = "fieldtable"       # the existing ("ui", key, value) choice holds it, like a chip


def _table_date(view):
    return str(view.get("checked_on") or "")[5:] or "·"


def field_line_fragments(view, room=None, opened=False):
    """The ``field tilt`` line as ``[[text, colour key], ...]``: each field whose applied row leans
    (a row with all cells equal is off and left out) as ``field→harness·harness``, the toggle last.
    With ``room`` (cells), trailing fields give way to ``…`` and the toggle stays."""
    head = [[f"table {_table_date(view)} · ", "dim"]]
    groups = []
    for row in view.get("rows", ()):
        if row.get("off") or not row.get("top"):
            continue
        group = [[str(row["field"]), "lvl_y" if row.get("overridden") else "soft"], ["→", "line"]]
        for index, harness in enumerate(row["top"]):
            group += ([["·", "dim"]] if index else []) + [[harness, "hb_" + harness]]
        groups.append(group)
    toggle = [["▾" if opened else "▸", "soft"]]
    if not groups:
        groups = [[["no field row leans", "dim"]]]
    separator, ellipsis = [["  ", "dim"]], [["…", "dim"]]

    def width(fragments):
        return sum(cell_width(text) for text, _key in fragments)

    def join(items, tail):
        out = list(head)
        for index, group in enumerate(items):
            out += (separator if index else []) + group
        return out + tail + separator + toggle

    full = join(groups, [])
    if room is None or width(full) <= room:
        return full
    for count in range(len(groups) - 1, 0, -1):
        shortened = join(groups[:count], [["  ", "dim"], ["…", "dim"]])
        if width(shortened) <= room:
            return shortened
    bare = join([], [["…", "dim"]])
    return bare if width(bare) <= room else toggle


def _draw_field_rows(grid, view, state, x, y, right, overrides=(), editor=False):
    """The ``field tilt`` line and, opened, the table of applied rows; returns the next row.

    The toggle's cell is settled first, inside the pane, and the date and fields take the room that
    is left; the click target is exactly that cell."""
    opened = state.fold_map().get("ui:" + _FIELD_TABLE_FOLD) == "open"
    last = right if grid.pane_clip is None else min(right, grid.pane_clip[1] - 1)
    start = x + 20
    fragments = field_line_fragments(view, max(1, last - start + 1), opened)
    toggle_x = min(start + sum(cell_width(text) for text, _key in fragments) - 1, last)
    grid.put(x + 3, y, "field tilt" if editor else "field tilt:", "label" if editor else "dim", limit=toggle_x)
    if start <= last:
        cursor = start
        for text, key in fragments[:-1]:
            cursor = grid.put(cursor, y, text, "branch_s" if text.isspace() else key, limit=toggle_x)
    grid.put(toggle_x, y, fragments[-1][0], fragments[-1][1], limit=toggle_x + 1)
    grid.add_hit(y, toggle_x, toggle_x + 1, ("ui", _FIELD_TABLE_FOLD, "closed" if opened else "open"))
    y += 1
    if not opened:
        return y
    columns = {"claude": start, "codex": start + 8, "opencode": start + 15}
    grid.put(x + 3, y, "field", "dim", limit=right + 1)
    for harness, column in columns.items():
        grid.put(column, y, harness, "hb_" + harness, limit=right + 1)
    y += 1
    rows = view.get("rows", ())
    for index, row in enumerate(rows):
        off = bool(row.get("off"))
        grid.put(x + 3, y, "  " + str(row["field"]), "lvl_y" if row.get("overridden") else "dim", limit=right + 1)
        for harness, column in columns.items():
            mine = harness in (row.get("overridden") or ())
            key = "dim" if off else ("lvl_y" if mine else "soft")
            grid.put(column, y, f"{float(row['cells'].get(harness, 1.0)):.2f}", key, limit=right + 1)
        if index == len(rows) - 1:
            note = f"table {_table_date(view)}" + (f" · basis: {view['basis']}" if view.get("basis") else "")
            grid.put(start + 25, y, note, "dim", limit=right + 1)
        y += 1
    tail = []
    if view.get("differs"):
        tail.append("this install differs: " + str(view["differs"]))
    tail.append("→ is a row's highest cell · a row only multiplies the share above, never a band or a limit")
    names = [str(name) for name in overrides or ()]
    tail.append(("your rows: " + ", ".join(names) + " · " if names else "") + "read only, edit the file to change")
    for text in tail:
        grid.put(start, y, text, "dim", limit=right + 1)
        y += 1
    return y


def _draw_allocation_editor(grid, allocation, state, x, y, right):
    """Every existing allocation key as choices, with what the chosen strategy ignores, a preview and Apply."""
    folds = state.fold_map()
    values = _dict(allocation.get("values"))
    strategy = folds.get("ui:alloc:strategy") or str(values.get("strategy"))
    inert = _dict(_dict(allocation.get("inert")).get(strategy))
    y = _alloc_row(grid, x, y, right, "strategy", "strategy", tuple(allocation.get("strategies") or ()),
                   str(values.get("strategy")), folds, _STRATEGY_NOTES.get(strategy, ""))
    rows = (("usage gate %", "usage_gate_used_percent",
             "skip a harness past this used share while another is under it"),
            ("usage effect", "usage_headroom_exponent", "how strongly remaining usage shapes the share"),
            ("window", "window", "recent attempts counted"))
    for label, name, note in rows:
        y = _alloc_row(grid, x, y, right, label, name, _alloc_options(name, values.get(name)),
                       _num(values.get(name)), folds, note, inert.get(name, ""))
    weights = _dict(values.get("harness_weights"))
    for harness in allocation.get("harnesses") or ():
        current = float(weights.get(harness, 1.0))
        y = _alloc_row(grid, x, y, right, "share " + harness, "harness_weights." + harness,
                       _alloc_options("harness_weights", current), _num(current), folds,
                       "1.0 = full share; lower takes fewer new placements", inert.get("harness_weights", ""))
    view = allocation.get("field_view")
    if isinstance(view, Mapping):
        y = _draw_field_rows(grid, view, state, x, y, right, allocation.get("field_overrides"), editor=True)
    affinity = _dict(values.get("depth_affinity"))
    choices = tuple(allocation.get("harnesses") or ()) + ("none",)
    for role in ("worker", "owner"):
        y = _alloc_row(grid, x, y, right, role + " prefers", "depth_affinity." + role, choices,
                       str(affinity.get(role) or "none"), folds, "", inert.get("depth_affinity", ""))
    y = _alloc_row(grid, x, y, right, "prefer strength", "depth_affinity_weight",
                   _alloc_options("depth_affinity_weight", values.get("depth_affinity_weight")),
                   _num(values.get("depth_affinity_weight")), folds, "0.5 = no lean",
                   inert.get("depth_affinity_weight", ""))
    changes = allocation_changes(allocation, folds)
    if changes:
        preview = "  ·  ".join(f"{_alloc_label(name)} {old} → {new}" for name, old, new in changes)
        grid.put(x + 3, y, "change", "label", limit=right)
        dry_x = min(x + 20 + cell_width(preview) + 3, right - 25)
        grid.put(x + 20, y, preview, "soft", limit=dry_x - 2)
        payload = tuple((name, new) for name, _old, new in changes)
        grid.put(dry_x, y, "[ dry-run ]", "title_dim", limit=right)
        grid.add_hit(y, dry_x, dry_x + 11, ("alloc.apply", payload, True))
        grid.put(dry_x + 13, y, "[ apply ]", "chip_on", limit=right)
        grid.add_hit(y, dry_x + 13, dry_x + 22, ("alloc.apply", payload, False))
        y += 1
    status = str(state.status or "")
    if status.startswith("allocation"):
        failed = "failed" in status or "invalid" in status
        grid.put(x + 3, y, status, "g_blocked" if failed else "soft", limit=right)
        y += 1
    if not allocation.get("user_file"):
        grid.put(x + 3, y, "no user policy file yet: Apply creates it from the shipped defaults", "dim",
                 limit=right)
        y += 1
    return y


_STEWARD_CHOICES = {"check_interval_seconds": (600, 1800, 3600, 7200, 21600, 86400),
                    "max_supervisors_per_repo": (1, 2, 3, 4, 8)}
_STEWARD_LABELS = {"check_interval_seconds": "check every", "max_supervisors_per_repo": "per repo"}


def _interval(seconds):
    """Seconds shown as minutes or hours; the file keeps seconds."""
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


def _steward_shown(name, value):
    return _interval(value) if name == "check_interval_seconds" else str(value)


def steward_changes(steward, folds):
    """``((key, old seconds/count, new), ...)`` the pending steward chips differ from the file on."""
    values = _dict(steward.get("values"))
    changes = []
    for name in _STEWARD_CHOICES:
        if values.get(name) is None:
            continue
        old, new = str(values[name]), folds.get("ui:steward:" + name)
        if new is not None and new != old:
            changes.append((name, old, new))
    return tuple(changes)


def _draw_supervisors(grid, census, limit, x, y, right):
    """Current supervisors per repository as counted by the steward collector. The count is
    that census, not the ⚑ marks: a fresh grant can count before its ⚑ appears."""
    grid.put(x + 3, y, "supervising", "label", limit=right)
    if not isinstance(census, (list, tuple)):
        grid.put(x + 20, y, f"· / {limit} per repo (not counted yet)", "dim", limit=right)
        return y + 1
    if not census:
        grid.put(x + 20, y, f"none · {limit} per repo allowed", "soft", limit=right)
        return y + 1
    for repo in census[:4]:
        repo = _dict(repo)
        shown = repo.get("limit") if repo.get("limit") is not None else limit
        holders = [str(holder.get("name") or str(holder.get("session_id") or "?")[:8])
                   + ("" if holder.get("counted", True) else " (" + str(holder.get("count_reason") or holder.get("liveness") or "unknown") + ")")
                   for holder in repo.get("holders") or () if isinstance(holder, Mapping)]
        text = (f"{repo.get('repository') or '?'} · {repo.get('counted', 0)} / {shown} supervising"
                + (" · " + ", ".join(holders) if holders else ""))
        over = bool(repo.get("over_limit"))
        grid.put(x + 20, y, text + (" · over limit" if over else ""), "g_blocked" if over else "soft",
                 limit=right)
        y += 1
        for holder in repo.get("holders") or ():
            if not isinstance(holder, Mapping):
                continue
            name = holder.get("name") or str(holder.get("session_id") or "?")[:8]
            detail = (f"{name}: {holder.get('liveness', 'unknown')} · {holder.get('count_reason', 'unknown')} "
                      f"· activity {holder.get('last_activity') or '?'} (role marker)")
            grid.put(x + 22, y, detail, "dim", limit=right)
            y += 1
    if len(census) > 4:
        grid.put(x + 20, y, f"+{len(census) - 4} more repositories", "dim", limit=right)
        y += 1
    return y


def _draw_steward_editor(grid, steward, state, x, y, right):
    """Supervision check interval and per-repo supervisor limit, edited like the allocation keys."""
    folds = state.fold_map()
    values = _dict(steward.get("values"))
    notes = {"check_interval_seconds": "a supervising session checks its targets this often without a notice",
             "max_supervisors_per_repo": "new supervisors allowed per repo; ended holders free their seats"}
    for name, choices in _STEWARD_CHOICES.items():
        current = values.get(name)
        if current is None:
            continue
        options = sorted(set(choices) | {int(current)})
        labels = {str(item): _steward_shown(name, item) for item in options}
        grid.put(x + 3, y, _STEWARD_LABELS[name], "label", limit=right)
        cursor = _chips(grid, x + 20, y, tuple(str(item) for item in options), str(current),
                        folds.get("ui:steward:" + name), "steward:" + name, right, labels)
        grid.put(cursor + 1, y, notes[name], "dim", limit=right)
        y += 1
    y = _draw_supervisors(grid, steward.get("supervisors"), values.get("max_supervisors_per_repo"),
                          x, y, right)
    changes = steward_changes(steward, folds)
    if changes:
        preview = "  ·  ".join(f"{_STEWARD_LABELS[name]} {_steward_shown(name, old)} → {_steward_shown(name, new)}"
                               for name, old, new in changes)
        grid.put(x + 3, y, "change", "label", limit=right)
        dry_x = min(x + 20 + cell_width(preview) + 3, right - 25)
        grid.put(x + 20, y, preview, "soft", limit=dry_x - 2)
        payload = tuple(("steward." + name, new) for name, _old, new in changes)
        grid.put(dry_x, y, "[ dry-run ]", "title_dim", limit=right)
        grid.add_hit(y, dry_x, dry_x + 11, ("alloc.apply", payload, True))
        grid.put(dry_x + 13, y, "[ apply ]", "chip_on", limit=right)
        grid.add_hit(y, dry_x + 13, dry_x + 22, ("alloc.apply", payload, False))
        y += 1
    status = str(state.status or "")
    if status.startswith("steward"):
        failed = "failed" in status or "invalid" in status
        grid.put(x + 3, y, status, "g_blocked" if failed else "soft", limit=right)
        y += 1
    grid.put(x + 3, y, "affects new checks and new supervisor grants only",
             "dim", limit=right + 1)
    return y + 1


def draw_models(grid: CellGrid, vm, state, x=X, y=3):
    data = _dict(vm.models)
    if not data:
        grid.put(x + 1, y, "·", "dim")
        return
    profiles = _profiles(data)
    profile_map = _dict(data.get("profile_map"))
    choices = _dict(data.get("model_choices"))
    pools = _dict(data.get("pools"))
    shown = _dict(data.get("shown"))
    effort_keys = _dict(data.get("effort_keys"))
    values = state.fold_map()
    selected = values.get("ui:sel", "balanced/claude")
    profile, _, harness = selected.partition("/")
    harness = harness or "claude"
    if harness not in ("claude", "codex", "opencode") or profile not in profiles:
        profile, harness = "balanced", "claude"
    slim = grid.cols - x - 2 < 150
    pool_width = 0 if slim else 44
    c_claude = 19
    col_width = max(16, min(30, (grid.cols - x - 2 - c_claude - pool_width - 15) // 3))
    columns = {"claude": c_claude, "codex": c_claude + col_width + GAP,
               "opencode": c_claude + 2 * (col_width + GAP)}
    pool_col = columns["opencode"] + col_width + 6 + GAP
    left = min(grid.cols - x - 2, pool_col + pool_width)
    right = x + left
    cursor = grid.put(x, y, "MODEL PROFILE", "label")
    cursor = grid.put(cursor, y, "  profile → model · effort, per harness", "dim", limit=right + 1)
    grid.put(cursor, y, " ", "branch_s")
    for col in range(cursor + 1, right + 1):
        grid.put(col, y, "─", "line")
    y += 1
    grid.fill(x, right + 1, y, " ", tint=234)
    for col, name in ((3, "PROFILE"), (columns["claude"], "CLAUDE"),
                      (columns["codex"], "CODEX"), (columns["opencode"], "OPENCODE")):
        grid.put(x + col, y, name, "head", tint=234, limit=right + 1)
    if not slim:
        grid.put(x + pool_col, y, "DISPATCH POOL", "head", tint=234, limit=right + 1)
    y += 1
    view = _follow(vm, data)
    shown_marks = set()
    for prof in profiles:
        grid.put(x + 3, y, prof, "soft" if prof != "top" else "dim")
        for h, col in columns.items():
            current = _dict(profile_map.get(h)).get(prof)
            hit_left, hit_right = x + col - 1, min(right, x + col + col_width)
            grid.add_hit(y, hit_left, hit_right, ("ui", "sel", f"{prof}/{h}"))
            if (prof, h) == (profile, harness):
                grid.fill(hit_left, min(right, hit_right + 1), y, " ", tint=236)
                grid.put(x + col - 2, y, "▶", "soft")
            elif view is not None and current is not None:
                mark = view.mark(h, prof)
                if mark:
                    grid.put(x + col - 2, y, mark, _MARK_KEY[mark])
                    shown_marks.add(mark)
            _model_cell(grid, x + col, y, h, current, shown, effort_keys,
                        col_width + (6 if h == "opencode" else 0))
            if (prof, h) == (profile, harness):
                for tint_x in range(max(0, hit_left), min(grid.cols, hit_right + 1)):
                    glyph, key, _tint = grid.cells[y][tint_x]
                    grid.cells[y][tint_x] = (glyph, key, 236)
        pool = pools.get(prof)
        if not slim:
            if pool:
                order, note = pool
                pool_order = order[0] if order and isinstance(order[0], (list, tuple)) else order
                cursor = x + pool_col
                for index, item in enumerate(pool_order):
                    if index:
                        cursor = grid.put(cursor, y, " › ", "dim")
                    cursor = grid.put(cursor, y, item, "hb_" + item)
                if note:
                    grid.put(cursor + 2, y, note, "dim", limit=right + 1)
            else:
                grid.put(x + pool_col, y, "route-sealed explicit choice only", "dim", limit=right + 1)
        y += 1
    main_only = data.get("main_only")
    if isinstance(main_only, Mapping):
        pairs = tuple(sorted(main_only.items(), key=lambda item: str(item[0])))
    elif isinstance(main_only, (list, tuple)):
        pairs = tuple((item.get("harness", "·"), item.get("model", "·"))
                      if isinstance(item, Mapping) else ("·", item) for item in main_only)
    else:
        pairs = ()
    main_items = tuple((harness_name, model_id) for harness_name, models in pairs         # one entry per model
                       for model_id in ((models,) if isinstance(models, str) else tuple(models or ())))
    if main_items:
        label = "main session only, never dispatched:"
        grid.put(x + 3, y, label, "dim", limit=right + 1)
        cursor = grid.put(x + 3 + len(label), y, "  ", "dim", limit=right + 1)
        for index, (harness_name, model_id) in enumerate(main_items):
            display = shown.get(model_id, model_id)
            if index:
                cursor = grid.put(cursor, y, "  ·  ", "dim", limit=right + 1)
            cursor = grid.put(cursor, y, f"{harness_name} {display}", "dim", limit=right + 1)
    summary = []
    if view is not None:
        summary = _summary_plan(view, profiles, shown, values)
    legend = tuple(item for item in follow_view.LEGEND if item[0] in shown_marks)
    used = _draw_summary(grid, summary, legend, x, y + (1 if main_items else 0), right) if (summary or legend) else 0
    y += 2 + used
    current = _dict(profile_map.get(harness)).get(profile)
    model_choice = values.get("ui:model")
    effort_choice = values.get("ui:effort")
    current_model, current_effort = (tuple(current) + (None, None))[:2] if current else (None, None)
    model_choice = model_choice or current_model or next(iter(choices.get(harness, ())), None)
    effort_choice = effort_choice or current_effort or "high"
    changed = current is None or not (_same_model(model_choice, current_model)
                                      and effort_choice == current_effort)
    harness_choices = tuple(choices.get(harness, ()))
    picking = not _chips_fit(harness_choices, shown, right - 2 - (x + 13))
    extra = PICK_ROWS if picking else 0
    box = (_box_plan(view, harness, profile, shown, values, current_effort) if view is not None else None)
    extra_rows = box["rows"] if box else 0           # the status lines and the follow row
    box_top, box_left = y, x + 1
    command_x = box_left + 5
    draft = box["requested"] if box else None
    if draft:
        changed = False
        command = f"hearting model set {harness} {box['cell']['target']} {draft}"
        command_lines = _command_lines(command, right - command_x)
    else:
        command = f"hearting model set {harness} profile/{profile} {model_choice}@{effort_choice}"
        command_lines = [command]
    command_y = box_top + extra_rows + 6 + extra
    pool_y = command_y + len(command_lines)
    box_bottom = pool_y + 1
    for col in range(box_left + 1, right):
        grid.put(col, box_top, "─", "line")
        grid.put(col, box_bottom, "─", "line")
    for yy in range(box_top + 1, box_bottom):
        grid.put(box_left, yy, "│", "line")
        grid.put(right, yy, "│", "line")
    for xx, yy, glyph in ((box_left, box_top, "┌"), (right, box_top, "┐"),
                          (box_left, box_bottom, "└"), (right, box_bottom, "┘")):
        grid.put(xx, yy, glyph, "line")
    title = [(harness, "hb_" + harness), (" · ", "dim"), (profile, "head")]
    cursor = box_left + 2
    grid.put(cursor, box_top, " ", "branch_s")
    cursor += 1
    for text, key in title:
        cursor = grid.put(cursor, box_top, text, key)
    grid.put(cursor, box_top, " ", "branch_s")
    grid.put(box_left + 3, y + 1, "now", "label")
    now_end = _model_cell(grid, box_left + 12, y + 1, harness, current, shown, effort_keys, 40, limit=right)
    catalog = _dict(_dict(data.get("catalog_sources")).get(harness))
    missing_current = (current_model and catalog.get("state") in ("available", "empty")
                       and not any(_same_model(item, current_model) for item in harness_choices))
    if draft and box["word"] == "undo":
        grid.put(now_end + 2, y + 1, "→", "line", limit=right)
        _model_cell(grid, now_end + 5, y + 1, harness,
                    (box["cell"]["since"]["from"], box["back_effort"]), shown, effort_keys, 40, limit=right)
    if changed:
        grid.put(box_left + 56, y + 1, "→", "line", limit=right)
        _model_cell(grid, box_left + 59, y + 1, harness, (model_choice, effort_choice), shown, effort_keys, 40,
                    limit=right)
    if box:
        _draw_box_follow(grid, box, harness, box_left + 12, y + 2, right)
    y += extra_rows
    grid.put(box_left + 3, y + 3, "model", "label")
    if picking:
        _model_picker(grid, box_left + 12, y + 3, right - 1, harness_choices, current_model,
                      values.get("ui:model"), shown, values, dict(state.scroll),
                      _catalog_note(data, harness))
    else:
        _chips(grid, box_left + 12, y + 3, harness_choices, current_model, values.get("ui:model"), "model",
               right - 2, shown)
    if missing_current:
        grid.put(box_left + 12, y + 2, "not in catalog · choose from the current list", "dim", limit=right)
    grid.put(box_left + 3, y + 4 + extra, "effort", "label")
    effort_options = tuple(data.get("efforts", ()))
    observed = catalog.get("variants")
    if harness == "opencode" and isinstance(observed, Mapping):
        model_variants = observed.get(model_choice)
        if isinstance(model_variants, (list, tuple)) and model_variants:
            effort_options = tuple(model_variants)
    effort_options += (("runtime-default",) if harness == "opencode" else ())
    _chips(grid, box_left + 12, y + 4 + extra, effort_options, current_effort, values.get("ui:effort"), "effort",
           right - 2)
    grid.put(box_left + 3, command_y, "$ ", "line")
    command_end = command_x + cell_width(command)
    dry_x = min(command_end + 3, right - 25)
    button_y = command_y
    if draft and dry_x < command_end + 3:
        # A follow draft has the full row when its buttons move above it.
        dry_x, button_y = command_x, command_y - 1
    command_limit = dry_x - 2 if (changed or draft) and button_y == command_y else right
    for row, line in enumerate(command_lines, command_y):
        grid.put(command_x, row, line, "soft" if changed or draft else "dim", limit=command_limit)
    if draft:
        actions = (("models.apply", harness, "follow", box["cell"]["target"], draft, True),
                   ("models.apply", harness, "follow", box["cell"]["target"], draft, False))
    else:
        actions = (("models.apply", harness, "profile", profile, model_choice, effort_choice, True),
                   ("models.apply", harness, "profile", profile, model_choice, effort_choice, False))
    if changed or draft:
        grid.put(dry_x, button_y, "[ dry-run ]", "title_dim", limit=right)
        grid.add_hit(button_y, dry_x, dry_x + 11, actions[0])
        apply_x = dry_x + 11 + 2
        grid.put(apply_x, button_y, "[ apply ]", "chip_on", limit=right)
        grid.add_hit(button_y, apply_x, apply_x + 9, actions[1])
    if slim:
        grid.put(box_left + 3, pool_y, "pool", "label")
        pool = pools.get(profile)
        if pool:
            order, note = pool
            pool_order = order[0] if order and isinstance(order[0], (list, tuple)) else order
            cursor = grid.put(box_left + 12, pool_y, " › ".join(pool_order), "soft",
                              limit=right - 2)
            if note:
                grid.put(cursor, pool_y, "  " + note, "dim", limit=right - 2)
        else:
            grid.put(box_left + 12, pool_y, "route-sealed explicit choice only", "dim", limit=right - 2)
    foot = "models.conf → .bak backup · new sessions and launches only · routes keep their depth, launches read this table"
    grid.put(box_left + 2, box_bottom, " ", "branch_s")
    grid.put(box_left + 3, box_bottom, foot, "dim", limit=right)
    grid.put(min(right - 1, x + 96), box_bottom, " ", "branch_s")
    y = box_bottom + 2
    beside = grid.cols - x - 2 - left > 70
    context_end = 3
    if beside:
        context_end = _draw_context(grid, data, state, x + left + GAP + 2, 3,
                                    grid.cols - (x + left + GAP + 4))
    allocation = _dict(data.get("allocation"))
    _section(grid, x, left, y, "DISPATCH ALLOCATION", "~/.config/hearting/dispatch-defaults.yaml")
    y += 1
    fields = allocation.get("fields", ())
    if isinstance(allocation.get("values"), Mapping):
        y = _draw_allocation_editor(grid, allocation, state, x, y, right)
        fields = [field for field in fields if field[0] not in _EDITED_FIELDS]
    field_view = allocation.get("field_view")
    for label, fragments in fields:
        if label == "field tilt:" and isinstance(field_view, Mapping):
            y = _draw_field_rows(grid, field_view, state, x, y, right, allocation.get("field_overrides"))
            continue
        grid.put(x + 3, y, label, "dim")
        cursor = x + 20
        for fragment, key in fragments:
            cursor = grid.put(cursor, y, _cut(fragment, grid.cols - cursor),
                              "branch_s" if fragment.isspace() else key)
        y += 1
    if allocation:
        grid.put(x + 3, y, allocation.get("note", "·"), "dim", limit=right + 1)
    steward = _dict(data.get("steward"))
    if isinstance(steward.get("values"), Mapping):
        y += 2
        _section(grid, x, left, y, "SUPERVISION", "steward · same file")
        y = _draw_steward_editor(grid, steward, state, x, y + 1, right) - 1
    if not beside:
        context_end = _draw_context(grid, data, state, x, y + 2, min(grid.cols - x - 2, 100))
    # A blank row at the viewport's overflow probe is not the end of MODELS.
    # Its geometry is already known, including wrapped choices and Apply rows.
    report_end = getattr(grid, "report_natural_page_end", None)
    if callable(report_end):
        report_end(max(y, context_end))
