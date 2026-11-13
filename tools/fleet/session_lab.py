"""The shell panel's LAB section (the chosen draft A): the shared experiment board in words.

Every value comes from ``utilities/experiment_board`` (``experiment-board/v1``, an aggregate of the
catalog's ``status()``), and every cell is worded by the Fleet LAB table's own helpers
(``explorer.lab_table.cells``), so the panel and Fleet never show one condition two ways: the time
since progress counts from the observation, a value of another metric names its metric, and a
number is shown whole or its column leaves.  This module only chooses which of those words the
panel's few rows carry; it decides no name, no state and no difference.

``read`` finds the session folder's artifact root the way Fleet does (file reads only) and asks the
board's own cache, so a redraw with nothing changed costs a few ``stat`` calls; the paths it returns
are for the panel's existing file watch (no poller).  ``view`` returns ``None`` when the board says
there is nothing to show (no experiments and no problem): then the panel has no LAB row at all.
Read-only; nothing here writes, gates, asks, or starts a process.
"""
from collections.abc import Mapping
import re
import sys
import time
from pathlib import Path

from .config import ui_text
from .collectors import project_status
from .explorer import lab_table

_UTILITIES = str(Path(__file__).resolve().parents[2] / "utilities")
if _UTILITIES not in sys.path:
    sys.path.insert(0, _UTILITIES)

INTERVAL = 60.0     # seconds a session folder keeps its resolved root (Fleet's project interval)
WATCH_CAP = 16      # LAB's share of the panel's watched paths (the host watches 64 at most)
MAX_ROWS = 8        # condition rows the opened table shows before "and N more" (Fleet's table)
# The opened table's extra pieces, in order; the panel leaves out those its row already shows.
PIECES = ("latest", "best", "base", "delta", "state", "expected", "since", "observed")
_ROOTS = {}         # session folder -> (resolved at, root or None)


def clear_cache():
    _ROOTS.clear()


def _g(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else default


def read(cwd, now=None):
    """``(board or None, watch paths)`` for a session folder; never raises."""
    if not cwd:
        return None, []
    now = time.time() if now is None else now
    try:
        resolved = _ROOTS.get(str(cwd))
        if resolved is None or not 0 <= now - resolved[0] < INTERVAL:
            resolved = (now, project_status.artifact_root(str(cwd)))
            _ROOTS[str(cwd)] = resolved
        root = resolved[1]
        if not root:
            return None, []
        import experiment_board
        board, watch = experiment_board.read(Path(root), now=now)
        return board, list(watch)[:WATCH_CAP]
    except Exception:  # noqa: BLE001 -- the panel draws on without its LAB section
        return None, []


def _parts(text, key):
    return [(str(text), key)] if text else []


def _selected(board):
    groups = [g for g in (_g(board, "groups") or ()) if isinstance(g, Mapping)]
    wanted = _g(board, "selected")
    return next((g for g in groups if _g(g, "id") == wanted), groups[0] if groups else None)


def _counts(group, unlinked):
    """The head's counts, most pressing first; the running count only when nothing else is to note."""
    counts = _g(group, "counts") or {}
    out = []
    for name, key in (("failed", "lvl_r"), ("stagnant", "lvl_y"), ("unknown", "lvl_y")):
        if counts.get(name):
            out.append(_parts(ui_text("fleet.lab.n_" + name, n=counts[name]), key))
    if unlinked:
        out.append(_parts(ui_text("fleet.lab.n_unnamed", n=unlinked), "lvl_y"))
    if counts.get("waiting"):
        out.append(_parts(ui_text("fleet.lab.n_waiting", n=counts["waiting"]), "dim"))
    if not out and counts.get("running"):
        out.append(_parts(ui_text("fleet.lab.n_running", n=counts["running"]), "g_spin"))
    return out


def _catalog_note(board):
    catalog = _g(board, "catalog") or {}
    status = catalog.get("status")
    if status in ("invalid", "unreadable"):
        return _parts(ui_text("fleet.lab.unreadable", code=catalog.get("code") or "?"), "lvl_y")
    if status == "missing":
        return _parts(ui_text("fleet.lab.catalog_missing"), "lvl_y")
    return []


def _headline(board, group, table, unlinked):
    """"Look at this": the board's own choice for the selected group (failed, stuck, unconfirmed,
    not comparable, waiting, then the best latest value), with the unnamed runs standing beside the
    unconfirmed ones as the board's strip counts them.  ``{"left", "right"}``; ``None`` for nothing."""
    head = _g(group, "headline") or {}
    kind = head.get("kind")
    rows = {_g(r, "condition_id"): (r, t) for r, t in table}
    row, cells = rows.get(head.get("condition_id"), (None, None))
    if unlinked and kind not in ("failed", "stagnant", "unknown"):
        return {"left": _parts(ui_text("fleet.lab.unnamed_runs"), "lvl_y"),
                "right": _parts(ui_text("fleet.lab.runs_n", n=unlinked), "lvl_y")}
    if row is None:
        return None
    name = [cells["name"]]
    if kind in ("failed", "stagnant", "unknown"):
        left = name + [(" " + cells["state"][0], cells["state"][1])]
        right = {"failed": cells["latest"] if _g(_g(row, "latest"), "nonfinite") else cells["progress"],
                 "stagnant": cells["since"], "unknown": cells["observed"]}[kind]
        return {"left": left, "right": [right] if right[0] not in ("·", "?") else []}
    if kind == "best":
        group_metric = _g(group, "metric") or {}
        import experiment_board
        latest = experiment_board.latest_text(row, group_metric, unit=True)
        return {"left": name + ([(" " + latest, "soft")] if latest else []),
                "right": [cells["base"]] if cells["base"][0] != "·" else []}
    if kind == "not-comparable":
        return {"left": name, "right": [cells["delta"]]}
    if kind == "waiting":
        return {"left": name, "right": [cells["latest"]]}
    if kind == "no-target":
        return {"left": name, "right": _parts(ui_text("fleet.lab.base_missing"), "lvl_y")}
    return None


def _metric_pieces(group):
    """The opened table's head: the metric and the baseline, each a piece the panel keeps whole."""
    import experiment_board
    metric = _g(group, "metric") or {}
    if metric.get("key"):
        arrow = {"max": " ↑", "min": " ↓"}.get(metric.get("direction"), "")
        out = [[(str(metric["key"]) + (f" ({metric['unit']})" if metric.get("unit") else "") + arrow, "soft")]]
    else:
        out = [[(ui_text("fleet.lab.metric_missing"), "lvl_y")]]
    target = _g(group, "target")
    if isinstance(target, Mapping) and target.get("kind") == "fixed" and target.get("value") is not None:
        value = experiment_board.value_text(target["value"], metric.get("unit"))
        base = ((str(target["label"]) + " ") if target.get("label") else "") + value
        out.append([(lab_table._header("base") + " ", "dim"), (base, "soft")])
    elif isinstance(target, Mapping) and target.get("kind") == "condition":
        control = next((_g(r, "name") for r in (_g(group, "rows") or ()) if _g(r, "condition_id") == target.get("condition")), None)
        out.append([(ui_text("fleet.lab.base_condition", name=control or ui_text("experiment.unnamed")), "soft")])
    else:
        out.append([(ui_text("fleet.lab.base_missing"), "lvl_y")])
    return out


def view(board, now=None, paint=None):
    """The panel's ``lab`` value, or ``None`` when the board has nothing to show.

    Parts are ``(text, palette key)`` pairs; ``paint`` (the projection's ``styled``) turns each list
    of them into painted parts.  Nothing here is cut to a width: the panel lays the rows out and a
    value either fits whole or leaves."""
    if not isinstance(board, Mapping) or not board.get("visible"):
        return None
    paint = paint or (lambda parts: [{"text": text, "key": key} for text, key in parts])
    now = time.time() if now is None else now
    import experiment_board
    group = _selected(board)
    rows = [r for r in (_g(group, "rows") or ()) if isinstance(r, Mapping)]
    table = [(row, lab_table.cells(row, group, now)) for row in rows]
    unlinked_doc = _g(board, "unlinked") or {}
    unlinked = int(_g(unlinked_doc, "count") or 0)
    count = _g(group, "count")
    goal = _g(group, "goal")
    note = _catalog_note(board)
    if group is not None:
        first = _parts(goal, "soft") if goal else _parts(ui_text("fleet.lab.goal_missing"), "lvl_y")
    else:
        first, note = note, []
    notes = [note] if note else []
    for item in _g(board, "other_groups_attention") or ():
        counts = [ui_text("fleet.lab.n_" + k, n=item[k]) for k in ("failed", "stagnant", "unknown") if _g(item, k)]
        notes.append(_parts(ui_text("fleet.lab.other_group", name=_g(item, "name") or ui_text("experiment.no_group"),
                                    counts=" · ".join(counts)), "lvl_y"))
    target = _g(group, "target") or {}
    out_rows = []
    for row, cells in table[:MAX_ROWS]:
        def cell(name):
            text, key = cells[name]
            return ("", key) if text in ("?", "·") else (text, key)
        pieces = []
        for name in PIECES:
            text, key = cells[name]
            if text in ("?", "·") or name == "base" and _g(target, "kind") == "fixed":
                continue  # unknown or not applicable: no piece; a fixed baseline stands on the metric row
            # The state word reads alone (`failed`); every other value after its column's word.
            label = [] if name == "state" else [(lab_table._header(name) + " ", "dim")]
            pieces.append({"col": name, "parts": paint(label + [(text, key)])})
        reason = lab_table.reason_text(row)
        out_rows.append({"name": paint([cells["name"]])[0], "full": cells["name"][0],
                         "cells": {name: paint([cell(name)])[0] for name in ("progress", "latest", "delta", "state")},
                         "more": pieces, "reason": paint(_parts(reason, "dim"))})
    more_rows = (paint(_parts(ui_text("experiment.and_more", n=len(table) - MAX_ROWS).strip(), "dim"))
                 if len(table) > MAX_ROWS else [])
    ids = list(_g(unlinked_doc, "run_ids") or ())
    extra = unlinked - len(ids[:3])
    unlinked_line = paint(_parts(ui_text("experiment.unlinked", count=unlinked, ids=", ".join(ids[:3])
                                         + (ui_text("experiment.and_more", n=extra) if extra > 0 else "")), "lvl_y")) if unlinked else []
    # The catalog's own warnings (a name used twice, a goal missing), as Fleet's LAB lists them: not
    # a catalog missing or not read nor a run without a name (each said above), three at most.
    warnings = [w for w in (_g(board, "warnings") or ()) if isinstance(w, Mapping)
                and w.get("code") not in ("catalog-missing", "catalog-invalid")
                and not str(w.get("where", "")).startswith("resource-runs/")][:3]
    warning_lines = [paint(_parts(ui_text("experiment.warning", code=ui_text("experiment.warning." + str(w.get("code"))),
                                          where=w.get("where"), detail=w.get("detail") or "-"), "dim")) for w in warnings]
    headline = _headline(board, group, table, unlinked)
    strip = str(board.get("strip") or "")
    match = re.fullmatch(r"([!?]?)(\d+\+?)( ?LB)", strip)
    if match:
        mark, number, word = match.groups()
        strip_parts = paint([(mark + number, {"!": "lvl_r", "?": "lvl_y"}.get(mark, "loc_repo")), (word, "dim")])
    else:
        strip_parts = []
    return {
        "key": "lab:" + str(_g(group, "id") or ""),
        "count": str(count) if group is not None else "",
        "clock": experiment_board.clock_text(board.get("generated_at"), now),
        "counts": [paint(parts) for parts in _counts(group, unlinked)],
        "goal": paint(first),
        "headline": ({"left": paint(headline["left"]), "right": paint(headline["right"])} if headline else None),
        "notes": [paint(parts) for parts in notes],
        "metric": [paint(parts) for parts in _metric_pieces(group)] if group is not None else [],
        "header": {name: lab_table._header(name) for name in ("name", "progress", "latest", "delta", "state")},
        "rows": out_rows, "more_rows": more_rows, "unlinked": unlinked_line, "warnings": warning_lines,
        "strip": strip_parts, "strip_attention": bool(match and match.group(1))}
