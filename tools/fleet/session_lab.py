"""The shell panel's LAB section (11-15: its head and three rows): the shared experiment board in words.

Every value comes from ``utilities/experiment_board`` (``experiment-board/v1``, an aggregate of the
catalog's ``status()``), and every cell is worded by the Fleet LAB table's own helpers
(``explorer.lab_table.cells``), so the panel and Fleet never show one condition two ways: the time
since progress counts from the observation, a value of another metric names its metric, and a
number is shown whole or its column leaves.  This module only chooses which of those words the
panel's few rows carry; it decides no name, no state and no difference.

``read`` finds the session folder's artifact root the way Fleet does (file reads only) and asks the
board's own cache, so a redraw with nothing changed costs a few ``stat`` calls; the paths it returns
are for the panel's existing file watch (no poller): the catalog, and each folder holding run records
as one folder target (a trailing separator), so a record redraws however many there are.  ``view``
returns ``None`` when the board says there is nothing to show (no experiments and no problem): then
the panel has no LAB row at all.  A root or board that cannot be read, or a board that cannot be
worded, is LAB's own unconfirmed state (``unconfirmed``), never "no experiments" and never the panel's
failure.  The whole table is Fleet's own, in the LAB popup (``lab_table.draw_lab``); the panel's rows are
its CONDITION, STATE, PROGRESS and DIFF in the board's order of what to look at first.  Read-only;
nothing here writes, gates, asks, or starts a process.
"""
from collections.abc import Mapping
import os
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
FOLDER_CAP = 4      # run-record folders LAB watches whole (records share one); more are said, not cut
MAX_ROWS = 3        # condition rows the panel's section shows (its popup shows Fleet's whole table)
_ROOTS = {}         # session folder -> (resolved at, root or None)
_WATCH = {}         # session folder -> its last read's watch; a read that fails keeps it


def clear_cache():
    _ROOTS.clear()
    _WATCH.clear()


def _g(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else default


def _targets(paths):
    """The panel's watch for the board's paths (the catalog, the run folder, then each linked record):
    the catalog file, then every folder holding records as one folder target (a trailing separator:
    the host redraws on any change inside it), never one path per record.  ``(targets, folders over
    FOLDER_CAP)``; those are said as unconfirmed by ``view``, never left out silently."""
    paths = [str(path) for path in paths]
    folders = []
    for index, path in enumerate(paths[1:]):
        folder = path if index == 0 else os.path.dirname(path)
        if folder not in folders:
            folders.append(folder)
    kept = folders[:FOLDER_CAP]
    return paths[:1] + [folder.rstrip(os.sep) + os.sep for folder in kept], len(folders) - len(kept)


def _base(root):
    """The root's catalog and run folder: what a first read that failed still watches."""
    try:
        import experiment_catalog
        return _targets([experiment_catalog.catalog_path(Path(root)), Path(root) / "resource-runs"])[0] if root else []
    except Exception:  # noqa: BLE001
        return []


def _unreadable(now, why):
    try:
        import experiment_board
        return experiment_board.unreadable(Path("."), now, why)
    except Exception:  # noqa: BLE001 -- ``view`` cannot word it either; the panel's guard says it
        return {"visible": True, "catalog": {"status": "unreadable", "code": why}, "strip": "?1LB"}


def read(cwd, now=None):
    """``(board or None, watch targets)`` for a session folder; never raises.

    ``None`` only when there is nothing to show: no folder, or no artifact root.  A root or board
    that cannot be read is the board's unconfirmed ("could not read") board, never ``None``, and keeps
    the last read's watch (else the root's catalog and run folder), so the next change reads again."""
    if not cwd:
        return None, []
    now = time.time() if now is None else now
    key, root = str(cwd), None
    try:
        resolved = _ROOTS.get(key)
        if resolved is None or not 0 <= now - resolved[0] < INTERVAL:
            resolved = (now, project_status.artifact_root(key))
            _ROOTS[key] = resolved
        root = resolved[1]
        if not root:
            _WATCH.pop(key, None)
            return None, []
        import experiment_board
        board, watch = experiment_board.read(Path(root), now=now)
        targets, unwatched = _targets(watch)
        if unwatched:
            board = {**board, "unwatched": unwatched}
        _WATCH[key] = targets
        return board, list(targets)
    except Exception as exc:  # noqa: BLE001 -- LAB says it could not read; the panel draws on
        return _unreadable(now, type(exc).__name__), list(_WATCH.get(key) or _base(root))


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


def _plain_paint(parts):
    return [{"text": text, "key": key} for text, key in parts]


def view(board, now=None, paint=None):
    """The panel's ``lab`` value, or ``None`` when the board has nothing to show.

    Parts are ``(text, palette key)`` pairs; ``paint`` (the projection's ``styled``) turns each list
    of them into painted parts.  Nothing here is cut to a width: the panel lays the rows out and a
    value either fits whole or leaves."""
    if not isinstance(board, Mapping) or not board.get("visible"):
        return None
    return _draw(board, time.time() if now is None else now, paint or _plain_paint)


def unconfirmed(now=None, why="", paint=None):
    """The ``lab`` value when the board could not be worded: the unconfirmed board's, so LAB's own
    failure stays in LAB and the rest of the panel draws on.  Never raises."""
    now = time.time() if now is None else now
    paint = paint or _plain_paint
    try:
        return _draw(_unreadable(now, why), now, paint)
    except Exception:  # noqa: BLE001 -- the words themselves failed: the unconfirmed mark alone
        return {"key": "lab:", "count": "", "goal": paint([("? " + str(why or "LAB"), "lvl_y")]),
                "strip": paint([("?1", "lvl_y"), ("LB", "dim")]), "strip_attention": True}


def _draw(board, now, paint):
    import experiment_board
    group = _selected(board)
    rows = [r for r in (_g(group, "rows") or ()) if isinstance(r, Mapping)]
    # What to look at first leads (the board's own order: failed, stuck, unconfirmed, …, the best).
    rows = experiment_board.look_order(rows, _g(group, "metric"))
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
    # Record folders over the watch's cap: their changes may not redraw, so their values are unconfirmed.
    unwatched = int(_g(board, "unwatched") or 0)
    if unwatched:
        notes.append(_parts(ui_text("fleet.lab.unwatched", n=unwatched), "lvl_y"))
    if unlinked:
        notes.append(_parts(ui_text("fleet.lab.unnamed_runs") + " · " + ui_text("fleet.lab.runs_n", n=unlinked), "lvl_y"))
    # Each row's cells as Fleet's table words them: an unknown PROGRESS is Fleet's `?`, a value not
    # applicable its `·`; nothing is left blank.
    out_rows = [{"name": paint([cells["name"]])[0],
                 "cells": {name: paint([cells[name]])[0] for name in ("state", "progress", "delta")}}
                for _row, cells in table[:MAX_ROWS]]
    strip = str(board.get("strip") or "")
    if unwatched and not strip.startswith(("!", "?")):
        cap = experiment_board.STRIP_CAP
        strip = "?" + (str(unwatched) if unwatched <= cap else f"{cap}+") + "LB"
    match = re.fullmatch(r"([!?]?)(\d+\+?)( ?LB)", strip)
    return {
        "key": "lab:" + str(_g(group, "id") or ""),
        "count": str(count) if group is not None else "",
        # The board's own moment: a reading of the same values redraws nothing; any change does.
        "clock": experiment_board.clock_text(board.get("generated_at"), now),
        "revision": str(board.get("generated_at") or ""),
        "counts": [paint(parts) for parts in _counts(group, unlinked)],
        "goal": paint(first),
        "notes": [paint(parts) for parts in notes],
        "rows": out_rows,
        "strip_attention": bool(match and match.group(1))}
