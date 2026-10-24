"""Run the committed reference-state matrix against Fleet's cell renderer."""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
WIDTHS = (80, 100, 140, 168)


_CONTEXT = {}


def _frame(row):
    """A reference capture as a comparable frame; hit actions are JSON lists like Fleet's."""
    from types import SimpleNamespace
    return SimpleNamespace(cells=row["cells"], hits=json.loads(json.dumps(row["hits"])),
                           cols=row["cols"], rows=row["rows"])


def _reference_values(vm):
    values = vm.to_mapping()
    ignored = {key: values.pop(key) for key in ("ordering", "readiness") if key in values}
    return values, ignored


def detect_reference_defects(frame, width, *, pane_x=34):
    """Return row/area coordinates for clipped text and hits outside their pane."""
    defects = []
    for y, row in enumerate(frame.cells):
        if len(row) > width:
            defects.append({"kind": "text-beyond-width", "row": y,
                           "area": [width, len(row)], "evidence": "cell row exceeds requested width"})
    hits = _hit_rows(frame)
    outside = [hit for hit in hits if len(hit) >= 3 and
               not (hit[0] == -1 and hit[1] == hit[2] == 0) and
               (hit[0] < 0 or hit[0] >= frame.rows or hit[1] < 0 or hit[2] > width or hit[1] >= hit[2])]
    if outside:
        defects.append({"kind": "hit-outside-screen", "row": None,
                        "area": [0, width], "evidence": outside})
    # Navigation hits are valid in the left rail. Pane actions must remain
    # inside the content pane and inside the frame.
    pane_hits = [hit for hit in hits if len(hit) >= 4 and
                 isinstance(hit[3], (tuple, list)) and hit[3] and hit[3][0] == "pane" and
                 (hit[1] < pane_x or hit[2] > width)]
    if pane_hits:
        defects.append({"kind": "hit-outside-pane", "row": None,
                        "area": [pane_x, width], "evidence": pane_hits})
    # A wide lead cell at the final column has no display space for its trail.
    for y, row in enumerate(frame.cells):
        if len(row) == width and row[-1][0] and _display_width(row[-1][0]) == 2:
            defects.append({"kind": "wide-glyph-overlap", "row": y, "area": [width - 1, width],
                            "evidence": row[-1][0]})
    return defects


# Horizontal rules and fills are drawn under text on purpose (a title inside a rule, a count laid
# over a section line). A vertical border, corner, junction or arrow is not: whatever reaches one
# has left its box. Two different text glyphs on one cell are never intended either.
_RULES = frozenset("─━═┈┄┅┉╌╍█▌▐░▒▓")
_BORDERS = frozenset("│┃║┌┐└┘├┤┬┴┼╔╗╚╝╧╤╟╢╪╫╬┏┓┗┛┣┫┳┻╋╭╮╯╰▶◀▼▲")


def _glyph_class(char):
    if not char or char.isspace():
        return "S"
    return "R" if char in _RULES else "B" if char in _BORDERS else "T"


def _overwrites(before, after):
    """A different glyph landing on a border or on text, other than a blank over text."""
    old, new = _glyph_class(before), _glyph_class(after)
    if before == after or old in ("S", "R") or new == "R" or (old == "B" and new == "B"):
        return False                       # two borders on one cell are lines crossing
    return not (old == "T" and new == "S")


def find_text_defects(events, width, rows=(3, 40), x_min=33, row_map=None):
    """Overlaps and beyond-width text from a put trace: ('cell', call, row, glyph, x, cells),
    ('clip'/'beyond', call, row, text, beyond_screen, start), ('fill', row, lo, hi, glyph), ('reset',).

    Overlap is judged on the pane body only (rows ``rows[0]``..``rows[1]`` and columns from
    ``x_min``): the chrome layers its rows on purpose. Text cut by the screen edge counts on any row.
    ``row_map`` turns the drawing's row into the row of the final frame."""
    shown = row_map or (lambda row: row)
    span, owner, overlaps, clips = {}, {}, [], []
    for event in events:
        if event[0] == "cell":
            _kind, call, row, _glyph, x, cells = event[:6]
            low, high = span.get((call, row), (x, x))
            span[(call, row)] = (min(low, x), max(high, x + max(1, cells)))
    for event in events:
        kind = event[0]
        if kind == "reset":
            owner.clear()
        elif kind == "fill":
            _kind, row, low, high, _glyph = event
            for x in range(low, high):
                owner.pop((row, x), None)
        elif kind == "cell":
            _kind, call, row, glyph, x, cells = event[:6]
            before = owner.get((row, x))
            if before and before[0] != call and rows[0] <= row <= rows[1] and x >= x_min and \
                    _overwrites(before[1], glyph):
                # The row's pane part is the area: an overflowing line also blanks or overwrites
                # neighbours through its other segments, so the damage is not one put's span.
                overlaps.append((shown(row), x_min, width, before[1], glyph))
            owner[(row, x)] = (call, glyph)
            for offset in range(1, cells):
                owner[(row, x + offset)] = (call, "")
        elif kind == "clip" and event[4]:
            start = event[5] if len(event) > 5 else 0
            if start < width:              # text that starts off the screen shows nothing to clip
                inside = rows[0] <= event[2] <= rows[1]
                clips.append((shown(event[2]), x_min if inside else start, width, str(event[3])[:24]))
        elif kind == "beyond" and rows[0] <= event[2] <= rows[1]:
            clips.append((shown(event[2]), x_min, width, str(event[3])[:24]))
    found, seen = [], set()
    for row, low, high, before, glyph in overlaps:
        if (row, low, high) not in seen:
            seen.add((row, low, high))
            found.append({"kind": "text-overlap", "row": row, "area": [low, high],
                          "evidence": f"{before!r} under {glyph!r}"})
    for row, low, high, text in clips:
        if (row, low, high) not in seen:
            seen.add((row, low, high))
            found.append({"kind": "text-beyond-width", "row": row, "area": [low, high],
                          "evidence": f"text {text!r} runs past column {high}"})
    return found


def trace_reference(reference, width, args):
    """Capture the reference once with its grid writes recorded; returns (capture, defects)."""
    from tools.fleet.tests.freeze_explorer import capture, load_reference
    module = load_reference(reference)
    grid_class = module.mock2.Grid
    original = grid_class.put
    events, calls, offscreen = [], [0], []

    def traced(self, x, y, text, key=None, limit=None, style=None):
        calls[0] += 1
        call = calls[0]
        edge = self.W if limit is None else min(limit, self.W)
        if 0 <= y < len(self.c):
            at, beyond_pane = x, False
            if text and x >= self.W and 2 <= y <= 39:
                offscreen.append(y)          # nothing is drawn, but a caller may keep the position
            for char in text:
                cells = module.mock2.cw(char)
                if at + cells > edge:
                    if edge >= self.W and at + cells > self.W:
                        events.append(("clip", call, y, text[text.index(char):], True, x))
                    break
                events.append(("cell", call, y, char, at, cells))
                beyond_pane = beyond_pane or at + cells > self.W - 1     # the last column is outside the pane
                at += cells
            if beyond_pane:
                events.append(("beyond", call, y, text, True, x))
        return original(self, x, y, text, key, limit, style)

    grid_class.put = traced
    try:
        row = capture(reference, width, renderer=module, **args)
    finally:
        grid_class.put = original
    top = 4 if args.get("uopen") else 2        # the frame inserts a spacer row at the pane top
    found = find_text_defects(events, width, rows=(top, 39),
                              row_map=lambda y: y + 1 if y >= top else y)
    # A control placed past the screen edge leaves a click target that covers other text.
    unseen = [hit for hit in row["hits"] if len(hit) > 3 and hit[2] >= width and hit[1] >= 33 and
              hit[0] in {y + 1 if y >= top else y for y in offscreen}]
    if unseen:
        found.append({"kind": "hit-unseen", "row": None, "area": [0, width],
                      "evidence": [list(hit) for hit in unseen]})
    return row, found


def own_trace_defects(vm, nav, width):
    """Draw Fleet's frame with its grids recording puts; returns (frame, defects).

    The view is drawn before the shared bands; their rows are already final, the view's are
    not (the frame shifts them), so the two phases are mapped separately.
    """
    from tools.fleet.explorer import chrome
    from tools.fleet.explorer.frame import draw
    from tools.fleet.explorer.grid import CellGrid
    events, boundary = [], []
    original = chrome.draw_chrome

    def marked(*args, **kwargs):
        boundary.append(len(events))
        return original(*args, **kwargs)

    CellGrid.default_trace = events
    chrome.draw_chrome = marked
    try:
        frame = draw(vm, nav, width, 45, beat=1)
    finally:
        CellGrid.default_trace = None
        chrome.draw_chrome = original
    push = 2 if nav.usage_open else 0
    cut = boundary[0] if boundary else len(events)
    found = find_text_defects(events[:cut], width, rows=(3, 39 - push),
                              row_map=lambda y: y + 1 + push if y >= 3 else y)
    found += find_text_defects(events[cut:], width, rows=(1, 0))   # bands: text cut by the edge only
    return frame, found


def _hit_rows(frame):
    return [[hit.y, hit.x0, hit.x1, list(hit.action)] if hasattr(hit, "y") else list(hit)
            for hit in frame.hits]


def _display_width(value):
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in str(value))


def _exclusions(defects):
    return tuple((row["row"], *row["area"]) for row in defects if row.get("row") is not None)


def _diff_with_reference_exclusions(expected, actual, defects):
    from types import SimpleNamespace
    from tools.fleet.tests.parity_compare import diff_frames
    action_key = lambda action: json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
    excluded = {(hit[0], action_key(hit[3])) for defect in defects
                for hit in defect.get("evidence", ()) if len(hit) >= 4}
    # A click target the reference draws on a defective row may sit elsewhere when Fleet draws the
    # row clean (a button that did not fit): it is left out of both maps, wherever it is.
    bad_rows = {defect["row"] for defect in defects if defect.get("row") is not None}
    moved = {action_key(hit[3]) for hit in _hit_rows(expected)
             if hit[0] in bad_rows and hit[1] >= 33 and len(hit) >= 4}
    def filtered(frame):
        hits = [hit for hit in _hit_rows(frame)
                if (hit[0], action_key(hit[3])) not in excluded and
                not (hit[1] >= 33 and action_key(hit[3]) in moved)]
        return SimpleNamespace(cells=frame.cells, hits=hits, cols=frame.cols, rows=frame.rows)
    return diff_frames(filtered(expected), filtered(actual), _exclusions(defects))


def _owner(view):
    return {"OVERVIEW": "tables.py", "ROUTES": "tables.py", "QUESTIONS": "tables.py",
            "HISTORY": "history.py", "PEERS": "peers.py", "COMPUTE": "resources.py",
            "MEMORY": "resources.py", "SYSTEM": "resources.py"}.get(view, "pages.py")


def _zones(diffs, rows):
    """Cell differences by the area that draws them: the left rail, the top and bottom bands, the pane."""
    zones = defaultdict(int)
    for row in diffs:
        if row.get("kind") != "cell":
            zones["hits"] += 1
        elif row["column"] < 33:
            zones["rail"] += 1
        elif row["row"] < 3:
            zones["top"] += 1
        elif row["row"] >= rows - 4:
            zones["bottom"] += 1
        else:
            zones["pane"] += 1
    return dict(zones)


def _compare_state(task):
    """Draw one state at every width; compare scrubbed reference against Fleet's fixture draw.

    The reference side goes through the freezer's scrubbed DESIGN=4 capture (the same path that
    froze the committed goldens) and Fleet draws from the committed fixture view model, so an
    unchanged renderer is equal exactly where the oracle is.
    """
    from tools.fleet.explorer.frame import draw
    from tools.fleet.tests.freeze_explorer import capture
    from tools.fleet.tests.parity_states import nav_state, reference_arguments
    state = task
    reference, vm = _CONTEXT["reference"], _CONTEXT["vm"]
    records, actions = [], []
    for width in WIDTHS:
        narrow = width in (80, 100)
        try:
            ref_row, ref_text = trace_reference(reference, width, reference_arguments(state))
        except Exception as error:  # the reference itself cannot draw this state
            records.append({"width": width, "error": f"reference: {type(error).__name__}: {error}"})
            continue
        left = _frame(ref_row)
        for hit in left.hits:
            if len(hit) > 3 and hit[3] and hit[3] not in actions:
                actions.append(hit[3])
        right, own_text = own_trace_defects(vm, nav_state(state), width)
        # Equal is judged on every cell and click target first; only a state that differs is judged
        # again without the rows where the reference overlaps or overflows itself.
        strict = _diff_with_reference_exclusions(left, right, [])
        defects = own = []
        diffs = strict
        if narrow or strict:
            defects = detect_reference_defects(left, width) + ref_text
            diffs = _diff_with_reference_exclusions(left, right, defects) if strict else []
        if narrow:
            own = detect_reference_defects(right, width) + own_text
        records.append({"width": width, "defects": defects if narrow or strict else [],
                        "own_defects": own, "strict_difference_count": len(strict),
                        "difference_count": len(diffs), "differences": diffs[:40],
                        "zones": _zones(diffs, right.rows),
                        "rows": sorted({row["row"] for row in diffs if row.get("kind") == "cell"})})
    return {"state": state, "records": records, "actions": actions}


def _scrubbed_cases(reference):
    """The README cases, naming the repo as the scrubbed capture sees it."""
    from tools.fleet.tests.freeze_explorer import (_reference_cases, load_reference,
                                                   sanitize_reference)
    cases = _reference_cases(reference)
    raw = str(load_reference(reference).PROJECTS[0][0])
    scrubbed = load_reference(reference)
    scrubbed.path = reference
    sanitize_reference(scrubbed)
    name = str(scrubbed.PROJECTS[0][0])
    return {label: (scope.replace("repo:" + raw, "repo:" + name), view, ui, uopen)
            for label, (scope, view, ui, uopen) in cases.items()}


def _zone_totals(rows):
    """Where the differing captures differ: the pane and click map (the views), or the shared bands."""
    totals = defaultdict(lambda: defaultdict(int))
    for row in rows:
        for zone in row.get("zones", {}):
            totals[str(row["width"])][zone] += 1
    return {width: dict(zones) for width, zones in totals.items()}


def _defect_report(schema, defects):
    """Distinct defects (kind, width, row, area) with how many states show each, and examples."""
    grouped = {}
    for defect in defects:
        key = (defect["width"], defect["kind"], defect.get("row"), tuple(defect.get("area") or ()))
        entry = grouped.setdefault(key, {"width": key[0], "kind": key[1], "row": key[2],
                                         "area": list(key[3]), "states": 0, "examples": [],
                                         "evidence": defect.get("evidence")})
        entry["states"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(defect["state"])
    by_kind = defaultdict(int)
    for defect in defects:
        by_kind[f"{defect['width']}:{defect['kind']}"] += 1
    return {"schema": schema, "widths": [80, 100], "defect_count": len(defects),
            "distinct_count": len(grouped), "by_width_kind": dict(sorted(by_kind.items())),
            "defects": sorted(grouped.values(), key=lambda row: (row["width"], row["kind"],
                                                               row["row"] if row["row"] is not None else -1))}


def _map(function, items, processes):
    if processes <= 1 or len(items) < 2:
        return [function(item) for item in items]
    import multiprocessing
    with multiprocessing.get_context("fork").Pool(processes) as pool:
        return pool.map(function, items, chunksize=max(1, len(items) // (processes * 8)))


def run_matrix(reference, fixture, out, processes=16, depth=4):
    from tools.fleet.explorer.view_model import load_fixture
    from tools.fleet.tests.freeze_explorer import _reference_cases
    from tools.fleet.tests.parity_states import (base_state, readme_states, state_id, state_key,
                                                 walk_next)
    reference, fixture, out = Path(reference), Path(fixture), Path(out)
    vm = load_fixture(fixture)
    _values, ignored_metadata = _reference_values(vm)
    _CONTEXT.update(reference=reference, vm=vm)
    seen, order, results = set(), [], {}
    manifest = readme_states(_scrubbed_cases(reference))
    frontier = [base_state()] + manifest
    for state in frontier:
        seen.add(state_key(state))
    levels = 0
    while frontier and levels < depth:
        levels += 1
        outcomes = _map(_compare_state, frontier, processes)
        nxt = []
        for outcome in outcomes:
            state = outcome["state"]
            results[state_id(state)] = outcome
            order.append(state)
            for child in walk_next(state, outcome["actions"]):
                if state_key(child) not in seen:
                    seen.add(state_key(child))
                    nxt.append(child)
        frontier = nxt
    rows, reference_defects, fleet2_defects = [], [], []
    residue = defaultdict(list)
    manifest_ids = {state_id(state) for state in manifest}
    for state in order:
        sid = state_id(state)
        for record in results[sid]["records"]:
            width = record["width"]
            if "error" in record:
                rows.append({"state": sid, "width": width, "equal": False, "difference_count": 1,
                             "differences": [{"kind": "reference-error", "error": record["error"]}]})
                continue
            for defect in record["defects"]:
                reference_defects.append({"state": sid, "width": width, **defect})
            for defect in record["own_defects"]:
                fleet2_defects.append({"state": sid, "width": width, **defect})
            row = {"state": sid, "width": width, "equal": not record["difference_count"],
                   "strict_equal": not record["strict_difference_count"],
                   "difference_count": record["difference_count"], "zones": record["zones"],
                   "differences": record["differences"]}
            rows.append(row)
            if record["difference_count"]:
                residue[_owner(state["view"])].append({"state": sid, "width": width,
                    "difference_count": record["difference_count"], "rows": record["rows"]})
    out.mkdir(parents=True, exist_ok=True)
    (out / "reference-defects.json").write_text(json.dumps(
        _defect_report("fleet-reference-defects-v2", reference_defects), ensure_ascii=False, indent=2) + "\n")
    (out / "fleet2-defects.json").write_text(json.dumps(
        _defect_report("fleet-renderer-defects-v2", fleet2_defects), ensure_ascii=False, indent=2) + "\n")
    equal = sum(row["equal"] for row in rows)
    per_width = {str(width): {"captures": sum(row["width"] == width for row in rows),
                              "equal": sum(row["width"] == width and row["equal"] for row in rows),
                              "strict_equal": sum(row["width"] == width and row.get("strict_equal", False)
                                                  for row in rows)}
                 for width in WIDTHS}
    summary = {"schema": "fleet-parity-matrix-v2", "state_count": len(order),
        "reference_readme_state_count": len(manifest), "walk_levels": levels,
        "widths": list(WIDTHS), "capture_count": len(rows), "equal_count": equal,
        "different_count": len(rows) - equal, "per_width": per_width,
        "strict_equal_count": sum(row.get("strict_equal", False) for row in rows),
        "equal_after_defect_exclusion_count": sum(row["equal"] and not row.get("strict_equal", False)
                                                  for row in rows),
        "pane_clean_count": sum(not (row["zones"].get("pane") or row["zones"].get("hits"))
                                for row in rows if "zones" in row),
        "different_by_zone": _zone_totals(rows),
        "manifest_results": {sid: [row["equal"] for row in rows_for(rows, sid)] for sid in sorted(manifest_ids)},
        "reference_defect_count": len(reference_defects),
        "fleet2_defect_count": len(fleet2_defects), "results": rows,
        "reference_ignored_metadata_fields": sorted(ignored_metadata),
        "residue_by_renderer": dict(residue)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (out / "residue-by-renderer.json").write_text(json.dumps(dict(residue), ensure_ascii=False, indent=2) + "\n")
    return summary


def rows_for(rows, sid):
    return [row for row in rows if row["state"] == sid]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tools/fleet/tests/fixtures/explorer/view_model.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--processes", type=int, default=16)
    args = parser.parse_args(argv)
    result = run_matrix(args.reference, args.fixture, args.out, args.processes)
    print(json.dumps({key: result[key] for key in ("state_count", "capture_count", "equal_count",
                                                    "different_count", "reference_defect_count",
                                                    "per_width")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
