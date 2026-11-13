"""The project page's LAB table: one row per registered condition, drawn from the shared board.

Everything shown is a field of ``utilities/experiment_board`` (``experiment-board/v1``, an
aggregate of the catalog's ``status()``).  This module chooses columns and words; it does not
decide a name, a state, or whether two values may be subtracted.  Column choice follows the
room the page has (``right - x``), not the screen width, and a whole column leaves before any
number is cut: observation age, then time since progress, expected time, best, baseline, and
last the latest value and its difference.  Colors are the existing palette keys.
"""
from collections.abc import Mapping, Sequence
import sys
import time
from pathlib import Path

from ..config import ui_text
from .grid import cell_width
from .grid import clip_cells as clip

_UTILITIES = str(Path(__file__).resolve().parents[3] / "utilities")
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)

GAP = 2
INDENT = 3
NAME_MIN = 10
NAME_MAX = 28
MAX_ROWS = 8                    # rows one group shows before "and N more"
MAX_NOTES = 5
DROP_ORDER = ("observed", "since", "expected", "best", "base", "pair")
CAPS = {"state": 8, "progress": 11, "latest": 14, "best": 14, "base": 18, "delta": 10,
        "expected": 11, "since": 6, "observed": 6}
STATE_KEY = {"running": "g_spin", "failed": "lvl_r", "stagnant": "lvl_y", "unknown": "lvl_y",
             "waiting": "dim", "ended": "soft"}


def _board():
    import experiment_board
    return experiment_board


def _g(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else default


def _rows(group):
    return tuple(r for r in (_g(group, "rows") or ()) if isinstance(r, Mapping))


def _metric(group):
    metric = _g(group, "metric")
    return metric if isinstance(metric, Mapping) else {}


def state_word(row):
    return ui_text("fleet.lab.state." + str(_g(row, "status") or "waiting"))


def reason_text(row):
    """Why this row has no difference or is not plain, in words; '' when nothing to say."""
    reason, state = _g(row, "reason"), _g(row, "comparison_state")
    out = []
    for code in (_g(row, "anomalies") or ()):
        out.append(ui_text("experiment.anomaly." + code))
    for code in (_g(row, "unknown_reasons") or ()):
        if code not in (_g(row, "anomalies") or ()):
            out.append(ui_text("fleet.lab.reason." + code))
    if state == "not-comparable" and reason:
        out.append(ui_text("experiment.not_comparable", reason=ui_text("experiment.reason." + reason)))
    elif state == "no-data" and reason == "control-no-observation":
        out.append(ui_text("experiment.reason.control-no-observation"))
    return "; ".join(out)


def _delta_cell(row):
    text = _board().delta_text(row)
    if text:
        return text, "lvl_g" if _g(row, "better") else "soft"
    state = _g(row, "comparison_state")
    if state == "not-comparable":
        return ui_text("fleet.lab.unavailable"), "lvl_y"
    if state == "no-data":
        return ui_text("fleet.lab.none"), "dim"
    return "·", "dim"


def _minute(now):
    """`now` rounded down to its minute.

    The explorer redraws only when the displayed `HH:MM` changes (`runtime.draw_frame`), so an age
    counted from a finer `now` would move inside a minute with nothing to repaint it.  Counting
    from the minute makes every draw in the same `HH:MM` the same cells, and the age steps exactly
    when the clock does.
    """
    return None if now is None else int(now // 60 * 60)


def cells(row, group, now):
    """``{column: (text, key)}`` for one row; a missing value is a word, never a zero."""
    board = _board()
    now = _minute(now)
    metric = _metric(group)
    status = _g(row, "status")
    latest = _g(row, "latest")
    if latest and latest.get("nonfinite"):
        latest_cell = (ui_text("fleet.lab.invalid"), "lvl_r")
    else:
        text = board.latest_text(row, metric)
        latest_cell = (text, "soft") if text else (ui_text("fleet.lab.none"), "dim")
    best = board.best_text(row, metric)
    base = board.baseline_text(row)
    delta = _delta_cell(row)
    progress = board.progress_text(row)
    # Both ages count from the draw clock; a board without the anchor (an older shape) keeps the read-time value.
    since_s = board.age_seconds(row, now, "last_progress_at") if now is not None and _g(row, "last_progress_at") \
        else _g(row, "since_last_progress_seconds")
    since = board.duration_text(since_s)
    age = board.age_seconds(row, now) if now is not None else _g(row, "observed_age_seconds")
    return {
        "name": (_g(row, "name") or ui_text("experiment.unnamed"), "soft" if _g(row, "name") else "lvl_y"),
        "state": (state_word(row), STATE_KEY.get(status, "soft")),
        "progress": (progress, "soft") if progress else ("?", "dim"),
        "latest": latest_cell,
        "best": (best, "soft") if best else ("·", "dim"),
        "base": (base, "soft") if base else ("·", "dim"),
        "delta": delta,
        "expected": ((board.clock_text(_g(row, "expected_at"), now), "soft") if _g(row, "expected_at") else ("·", "dim")),
        "since": (since, "soft") if since else ("·", "dim"),
        "observed": ((board.duration_text(age), "lvl_y" if "observation-stale" in (_g(row, "anomalies") or ()) else "soft")
                     if age is not None else ("?", "lvl_y"))}


def _header(name):
    return ui_text("fleet.lab.col." + name)


def layout(table, room, headers):
    """``(columns in order, width per column)`` that fit ``room`` cells; ``table`` is ``cells()`` per row."""
    widths = {}
    for name in CAPS:
        widest = max([cell_width(headers[name])] + [cell_width(t[name][0]) for t in table])
        widths[name] = min(widest, CAPS[name])
    name_width = min(NAME_MAX, max([cell_width(headers["name"])] + [cell_width(t["name"][0]) for t in table]))
    order = ["name", "state", "progress", "latest", "best", "base", "delta", "expected", "since", "observed"]
    for drop in DROP_ORDER + (None,):
        used = [c for c in order if c != "name"]
        need = NAME_MIN + sum(widths[c] for c in used) + GAP * len(used)
        if need <= room or drop is None:
            break
        for gone in (("latest", "delta") if drop == "pair" else (drop,)):
            order.remove(gone)
    used = [c for c in order if c != "name"]
    name_width = max(1, min(name_width, room - sum(widths[c] for c in used) - GAP * len(used)))
    widths["name"] = name_width
    return order, widths


def _put_cell(grid, x, y, text, key, width, right, align="left"):
    text = clip(text, width)
    pad = max(0, width - cell_width(text))
    if align == "right":
        x += pad
    grid.put(x, y, text, key, limit=right)


def _draw_table(grid, group, x, y, right, now):
    rows = _rows(group)
    room = max(1, right - x)
    shown = rows[:MAX_ROWS]
    body = [cells(row, group, now) for row in shown]
    headers = {name: _header(name) for name in ("name",) + tuple(CAPS)}
    order, widths = layout(body, room, headers)
    cursor = x
    for name in order:
        _put_cell(grid, cursor, y, headers[name], "dim", widths[name], right,
                  "left" if name in ("name", "state") else "right")
        cursor += widths[name] + GAP
    y += 1
    for table in body:
        cursor = x
        for name in order:
            text, key = table[name]
            _put_cell(grid, cursor, y, text, key, widths[name], right,
                      "left" if name in ("name", "state") else "right")
            cursor += widths[name] + GAP
        y += 1
    if len(rows) > len(shown):
        grid.put(x, y, ui_text("experiment.and_more", n=len(rows) - len(shown)).strip(), "dim", limit=right)
        y += 1
    return y, order


def _head_lines(grid, group, x, y, right):
    name = _g(group, "name") or ui_text("experiment.no_group")
    label_w = 8
    grid.put(x, y, clip(str(name), max(0, right - x)), "label", limit=right)
    y += 1
    goal = _g(group, "goal")
    grid.put(x, y, ui_text("fleet.lab.goal"), "dim", limit=right)
    grid.put(x + label_w, y, clip(goal or ui_text("fleet.lab.goal_missing"), max(0, right - x - label_w)),
             "soft" if goal else "lvl_y", limit=right)
    y += 1
    metric = _metric(group)
    grid.put(x, y, ui_text("fleet.lab.metric"), "dim", limit=right)
    if metric.get("key"):
        arrow = {"max": " ↑", "min": " ↓"}.get(metric.get("direction"), "")
        text = str(metric["key"]) + (f" ({metric['unit']})" if metric.get("unit") else "") + arrow
        text += "".join(" · " + str(metric[k]) for k in ("split", "scoring") if metric.get(k))
        key = "soft"
    else:
        text, key = ui_text("fleet.lab.metric_missing"), "lvl_y"
    grid.put(x + label_w, y, clip(text, max(0, right - x - label_w)), key, limit=right)
    y += 1
    target = _g(group, "target")
    grid.put(x, y, ui_text("fleet.lab.base"), "dim", limit=right)
    if isinstance(target, Mapping) and target.get("kind") == "fixed" and target.get("value") is not None:
        value = _board().value_text(target["value"], metric.get("unit"))
        text, key = ((str(target["label"]) + " " if target.get("label") else "") + value), "soft"
    elif isinstance(target, Mapping) and target.get("kind") == "condition":
        control = next((_g(r, "name") for r in _rows(group) if _g(r, "condition_id") == target.get("condition")), None)
        text, key = ui_text("fleet.lab.base_condition", name=control or ui_text("experiment.unnamed")), "soft"
    else:
        text, key = ui_text("fleet.lab.base_missing"), "lvl_y"
    grid.put(x + label_w, y, clip(text, max(0, right - x - label_w)), key, limit=right)
    return y + 1


def summary(lab, now=None):
    """The section's detail text: ``5 · running 3 · failed 1 · 20:40`` for the selected group."""
    groups = [g for g in (_g(lab, "groups") or ()) if isinstance(g, Mapping)]
    parts = []
    if groups:
        first = groups[0]
        counts = _g(first, "counts") or {}
        parts.append(ui_text("fleet.lab.count", n=_g(first, "count", 0)))
        for key in ("running", "failed", "stagnant", "unknown"):
            if counts.get(key):
                parts.append(ui_text("fleet.lab.n_" + key, n=counts[key]))
    stamp = _board().clock_text(_g(lab, "generated_at"), now)
    if stamp:
        parts.append(stamp)
    return " · ".join(parts)


def draw_lab(grid, lab, x, right, y, now=None):
    """Draw the LAB body below its section title; returns the next free row."""
    board = _board()
    now = _minute(time.time() if now is None else now)
    x += INDENT
    catalog = _g(lab, "catalog") or {}
    status = catalog.get("status")
    path = catalog.get("path") or "-"
    if status == "invalid":
        grid.put(x, y, clip(ui_text("experiment.header_invalid", path=path, code=catalog.get("code") or "?"),
                            max(0, right - x)), "lvl_y", limit=right)
        y += 1
    elif status == "unreadable":
        grid.put(x, y, clip(ui_text("fleet.lab.unreadable", code=catalog.get("code") or "?"), max(0, right - x)),
                 "lvl_y", limit=right)
        y += 1
    elif status == "missing":
        grid.put(x, y, clip(ui_text("experiment.header_missing", path=path), max(0, right - x)), "lvl_y", limit=right)
        y += 1
    groups = [g for g in (_g(lab, "groups") or ()) if isinstance(g, Mapping)]
    for index, group in enumerate(groups):
        if index:
            y += 1
        y = _head_lines(grid, group, x, y, right)
        if _rows(group):
            y, order = _draw_table(grid, group, x, y, right, now)
            notes = [(_g(r, "name") or ui_text("experiment.unnamed"), reason_text(r)) for r in _rows(group)]
            notes = [(n, t) for n, t in notes if t]
            for name, text in notes[:MAX_NOTES]:
                grid.put(x, y, clip(f"{name}: {text}", max(0, right - x)), "dim", limit=right)
                y += 1
            if len(notes) > MAX_NOTES:
                grid.put(x, y, ui_text("experiment.and_more", n=len(notes) - MAX_NOTES).strip(), "dim", limit=right)
                y += 1
            if right - x < 70:    # a narrow table has no room for changes / problem: one DETAIL line
                head = _g(group, "headline") or {}
                row = next((r for r in _rows(group) if _g(r, "condition_id") == head.get("condition_id")), None)
                detail = (_g(row, "changes") or _g(row, "problem")) if row else None
                if detail:
                    grid.put(x, y, ui_text("fleet.lab.detail"), "dim", limit=right)
                    grid.put(x + 8, y, clip(f"{_g(row, 'name') or ''}: {detail}", max(0, right - x - 8)), "soft", limit=right)
                    y += 1
        else:
            grid.put(x, y, ui_text("fleet.lab.no_conditions"), "dim", limit=right)
            y += 1
    attention = _g(lab, "other_groups_attention") or ()
    for item in attention:
        counts = [ui_text("fleet.lab.n_" + k, n=item[k]) for k in ("failed", "stagnant", "unknown") if _g(item, k)]
        grid.put(x, y, clip(ui_text("fleet.lab.other_group", name=_g(item, "name") or ui_text("experiment.no_group"),
                                    counts=" · ".join(counts)), max(0, right - x)), "lvl_y", limit=right)
        y += 1
    unlinked = _g(lab, "unlinked") or {}
    if _g(unlinked, "count"):
        ids = list(_g(unlinked, "run_ids") or ())
        shown = ids[:3]
        extra = int(unlinked["count"]) - len(shown)
        grid.put(x, y, clip(ui_text("experiment.unlinked", count=unlinked["count"],
                                    ids=", ".join(shown) + (ui_text("experiment.and_more", n=extra) if extra > 0 else "")),
                            max(0, right - x)), "lvl_y", limit=right)
        y += 1
    warnings = [w for w in (_g(lab, "warnings") or ())
                if isinstance(w, Mapping) and w.get("code") != "catalog-missing"
                and not str(w.get("where", "")).startswith("resource-runs/")]
    for w in warnings[:3]:
        grid.put(x, y, clip(ui_text("experiment.warning", code=ui_text("experiment.warning." + str(w.get("code"))),
                                    where=w.get("where"), detail=w.get("detail") or "-"), max(0, right - x)),
                 "dim", limit=right)
        y += 1
    return y
