"""Bounded repo, session, route, and selected-history detail projections."""

from collections.abc import Mapping
from .grid import cell_width
from .grid import clip_cells as clip
from .tables import repo_rule_label
from .tables import _clip, _tint_row, draw_overview, draw_routes


def draw_scope(grid, vm, state):
    scope = str(state.scope or "-")
    kind, _, ident = scope.partition(":")
    x, right, bottom = 34, max(34, grid.cols - 1), max(0, grid.rows - 5)
    if kind == "repo":
        _repo_page(grid, vm, ident, x, right, bottom, state)
    elif kind == "session":
        _session_page(grid, vm, ident, x, right, bottom, state)
    elif kind == "route":
        _route_page(grid, vm, ident, x, right, bottom, state)


def _put_line(grid, x, y, label, value, key="soft", right=None):
    right = grid.cols if right is None else right
    grid.put(x, y, label, "label", limit=right)
    grid.put(x + 13, y, _clip("·" if value is None or value == "" else value,
                              max(0, right - x - 18)), key, limit=right)
    return y + 1


def _section(grid, x, right, y, title, detail=""):
    grid.put(x, y, "── ", "line", limit=right)
    cursor = grid.put(x + 3, y, title, "sect", limit=right)
    if detail:
        cursor = grid.put(cursor, y, "  " + str(detail), "dim", limit=right)
    grid.put(cursor, y, " ", "branch_s", limit=right)
    for xx in range(min(right, cursor + 1), right):
        grid.put(xx, y, "─", "line")
    return y + 1


def _repo_page(grid, vm, ident, x, right, bottom, nav=None):
    repo = next((item for item in vm.repos
                 if str(item.get("id", item.get("name", ""))) == ident), None)
    if repo is None:
        return
    from dataclasses import replace
    sessions = tuple(item for item in vm.sessions
                     if item.get("repo_id", item.get("repo")) == ident)
    routes = tuple(item for item in vm.routes if str(item.get("repo") or "") == ident)
    questions = tuple(item for item in vm.questions if item.get("project") == ident)
    scoped_vm = replace(vm, repos=(repo,), sessions=sessions, routes=routes,
                        questions=questions)
    folds = dict(getattr(nav, "folds", ()) or ())
    draw_overview(grid, scoped_vm, state=type("ScopedOverview", (), {
        "opened": "", "scope": "repo:" + ident,
        "opened_for": staticmethod(lambda key, default="": folds.get(str(key), default)),
    })(), x=x, y=3)
    for column in range(1, 9):
        glyph, _key, _tint = grid.cells[3][column]
        grid.cells[3][column] = (glyph, "loc_repo", None)
    for column in range(0, x - 2):
        glyph, key, _tint = grid.cells[3][column]
        grid.cells[3][column] = (glyph, key, None)


def _session_page(grid, vm, tag, x, right, bottom, state=None):
    session = next((item for item in vm.sessions if str(item.get("tag")) == tag), None)
    if session is None:
        return
    harness = str(session.get("harness") or "·")
    branch = session.get("branch") or "·"
    y = _section(grid, x, right, 3, "[" + tag + "] " + str(session.get("title") or "·"),
                 " · ".join(part for part in (repo_rule_label(session.get("repo")), str(branch)) if part))
    model = tuple(session.get("model") or ())
    grid.put(x + 3, y, "harness:", "label", limit=right)
    cursor = grid.put(x + 16, y, harness, "hb_" + harness, limit=right)
    if model:
        effort = session.get("effort")
        cursor = _segs(grid, cursor, y, [
            (" (", "dim"), (model[0], model[1] if len(model) > 1 else "dim"), ("·", "dim"),
            (str(effort), "eff_" + str(effort) if effort in ("medium", "xhigh") else "dim"),
            (")", "dim")], right)
    pane = session.get("pane")
    cursor = _segs(grid, cursor, y, [("      pane ", "label"),
                                     (str(pane or "·"), "soft" if pane else "dim")], right)
    if pane:
        open_x = grid.put(cursor, y, "  (open ▸)", "hearting", limit=right)
        if open_x <= right - 1:          # the link is a target only where all of it is drawn in the pane
            grid.add_hit(y, cursor + 2, open_x, ("pane", tag, str(pane)))
    y += 1
    session_state = str(session.get("state") or "·")
    ask = bool(session.get("ask"))
    marker, marker_key = {"working": ("⠏", "spin"), "idle": ("●", "g_work_off"),
                          "blocked": ("●", "g_blocked"), "dead": ("✕", "g_dead"),
                          "done": ("✓", "done")}.get(session_state, ("·", "dim"))
    if ask:
        marker, marker_key = "●", "g_blocked"
    word = "answer needed" if ask else {"dead": "ended", "done": "ended"}.get(session_state, session_state)
    grid.put(x + 3, y, "state:", "label", limit=right)
    _segs(grid, x + 16, y, [(marker, marker_key), (" " + word, "blocked" if ask else "soft"),
                            (" · " + str(session.get("age") or "·"), "dim")], right)
    y += 1
    grid.put(x + 3, y, "context:", "label", limit=right)
    context = session.get("context")
    generation = session.get("generation")
    parts = []
    if context is not None:
        gauge_width = 16
        pct = int(context)
        filled = 0 if pct <= 0 else gauge_width if pct >= 100 else max(
            1, min(gauge_width - 1, int(pct * gauge_width / 100 + 0.5)))
        gauge_key = "g_blocked" if pct >= 80 else "g_idle" if pct >= 50 else "done"
        parts += [("━" * filled, gauge_key), ("─" * (gauge_width - filled), "dim"),
                  ("  %d%%" % pct, gauge_key if pct >= 50 else "dim")]
    parts += [("      generation ", "label"),
              (str(generation) if generation else "·", "soft" if generation else "dim")]
    if any(str(item[0]) == tag for item in vm.seats or ()) or (generation and session.get("last_clear")):
        parts.append((" · last clear " + str(session.get("last_clear") or "·"), "dim"))
    _segs(grid, x + 16, y, parts, grid.cols)                # a field line runs to the screen edge and is cut there
    y += 1
    now = session.get("now")
    grid.put(x + 3, y, "now:", "label", limit=right)
    grid.put(x + 16, y, clip(str(now or "·"), right - x - 21), "now_sub" if now else "dim", limit=right)
    y += 1
    grid.put(x + 3, y, "running:", "label", limit=right)
    _segs(grid, x + 16, y, tuple(session.get("running") or ()) or (("·", "dim"),), right)
    y += 1
    y = _section(grid, x, right, y + 1, "ROUTES",
                 sum(1 for route in vm.routes if route.get("session_tag") == tag) or "none")
    routes = tuple(route for route in vm.routes if route.get("session_tag") == tag)
    if routes:
        end = _draw_route_subset(grid, vm, routes, x, y, bottom, view_key=tag, state=state)
        # An opened route unfolds its detail in place, so the next section starts below what was drawn.
        fixed = y + len(routes) * 2 + 2
        y = end if end and end > fixed else fixed
    # The reference draws every section below unguarded; whatever passes the pane's foot is cut
    # and flagged by the frame, never left out here.
    links = tuple(edge for edge in tuple((vm.peer_edges or {}).get("links", ()))
                  if any(isinstance(end, (list, tuple)) and str(end[0]) == tag
                         for end in edge[:2]))
    y = _section(grid, x, right, y + 1, "PEERS",
                 str(len(links)) + (" link" if len(links) == 1 else " links") if links else "none")
    for edge in links[:4]:
        for edge_x, endpoint in ((x + 3, edge[0]), (x + 19, edge[1])):
            if isinstance(endpoint, (list, tuple)):
                session_tag, endpoint_harness = str(endpoint[0]), str(endpoint[1])
                edge_x = grid.put(edge_x, y, "[", "dim")
                steward = any(str(item.get("tag")) == session_tag and item.get("steward")
                              for item in vm.sessions)
                edge_x = grid.put(edge_x, y, session_tag, "steward" if steward else "soft")
                edge_x = grid.put(edge_x, y, "]", "dim")
                grid.put(edge_x, y, " " + endpoint_harness,
                         "hb_" + endpoint_harness, limit=right)
            else:
                grid.put(edge_x, y, _clip(str(endpoint), 14), "dim", limit=right)
        grid.put(x + 16, y, "→", "soft")
        kind = str(edge[2])
        kind_key = {"steer": "soft", "notice": "dim", "handover": "hb_claude",
                    "watch ⚑": "steward"}.get(kind, "dim")
        grid.put(x + 40, y, kind, kind_key, limit=right)
        grid.put(x + 51, y, str(edge[3]), "dim", limit=right)
        grid.put(x + 64, y, _clip(str(edge[4]), max(0, right - x - 65)), "now_sub", limit=right)
        y += 1
    gpu_rows = tuple(session.get("gpu") or ())
    if gpu_rows:
        y = _section(grid, x, right, y + 1, "GPU", str(len(gpu_rows)) + " in use")
        for gpu in gpu_rows:
            cursor = x + 3
            for text, key in gpu:
                key = {"host": "loc_repo", "gpu_ada": "h_claude"}.get(str(key), key)
                cursor = grid.put(cursor, y, str(text), key, limit=right - 1)
            y += 1
    pane_lines = tuple(session.get("pane_lines") or ())
    if pane_lines:
        y = _section(grid, x, right, y + 1, "PANE " + str(pane or "·"),
                     str(session.get("pane_meta") or ""))
        for line, key in pane_lines:
            grid.put(x + 3, y, _clip(str(line), max(0, right - x - 4)), key, limit=right)
            y += 1
    y += 1
    seat = next((item for item in vm.seats if str(item[0]) == tag), None)
    history_bottom = grid.rows - 6
    if seat and history_bottom - y > 4:
        generations = tuple((vm.generations or {}).get(tag, ()))
        detail = any(isinstance(item, Mapping) and item.get("detail") for item in generations)
        _section(grid, x, right, y, "HISTORY",
                 "seat · %d generations" % len(generations) if detail else "")
        from .history import draw_seat_expand
        draw_seat_expand(grid, vm, state, tag, x, right - x - 1, y + 1, history_bottom, seat,
                         harness)


def _route_page(grid, vm, ident, x, right, bottom, state):
    route = next((item for item in vm.routes if str(item.get("id")) == ident), None)
    if route is None:
        return
    y = _draw_route_subset(grid, vm, (route,), x, 3, bottom, opened=ident, no_repo_group=True,
                           view_key="route:" + ident, state=state)
    draw_route_detail(grid, vm, route, x, grid.cols - x - 2, y + 1, state,
                      bottom=min(bottom, grid.rows - 6))


class _SubsetState:
    """The view-local state a route table reads: a fixed fold key, everything else from the real state."""

    def __init__(self, base, view, opened="", no_repo_group=False):
        self.base, self.view, self.no_repo_group = base, view, no_repo_group
        self.opened = opened
        self.scroll = tuple(getattr(base, "scroll", ()) or ())

    def opened_for(self, key, default=""):
        if key == "ropen:" + self.view and self.opened:
            return self.opened
        if self.base is not None and hasattr(self.base, "opened_for"):
            return self.base.opened_for(key, default)
        return default


def _draw_route_subset(grid, vm, routes, x, y, bottom, opened="", no_repo_group=False,
                       view_key="ROUTES", state=None):
    # Reuse the exact corrected table renderer with a view-model carrying only these routes.
    from dataclasses import replace
    subset = replace(vm, routes=tuple(routes))
    return draw_routes(grid, subset, _SubsetState(state, view_key, opened, no_repo_group), x=x, y=y)


_BAND_W, _BAND_H, _BAND_GAP = 44, 4, 4
_LW = 7
_STAGE_HUE = ("l", "c", "g", "y", "m")
_STATE_MARK = {"done": ("✓", "done"), "active": ("●", "work"), "failed": ("✕", "lvl_r"),
               "todo": ("·", "dim")}
_ROUND_KEY = {"✕": "lvl_r", "↺": "lvl_y", "✓": "done", "●": "work"}
_RAW_ROWS = 10
_LOOP_MIN_W = 24
_ARROWS = ("▶", "◀", "▼")


def _segs(grid, x, y, parts, limit=None):
    for text, key in parts:
        x = grid.put(x, y, str(text), "branch_s" if key is None else key, limit=limit)
    return x


def _width(parts):
    return sum(cell_width(str(text)) for text, _key in parts)


def _lab(name):
    return (str(name).ljust(_LW), "dim")


def _outline(grid, x, y, w, h, style, top_holes=()):
    for xx in range(x + 1, x + w - 1):
        if not any(start <= xx < end for start, end in top_holes):
            grid.put(xx, y, "─", style)
        grid.put(xx, y + h - 1, "─", style)
    for yy in range(y + 1, y + h - 1):
        grid.put(x, yy, "│", style)
        grid.put(x + w - 1, yy, "│", style)
    for (xx, yy), glyph in (((x, y), "┌"), ((x + w - 1, y), "┐"),
                            ((x, y + h - 1), "└"), ((x + w - 1, y + h - 1), "┘")):
        grid.put(xx, yy, glyph, style)


def _rounds_lines(rounds, room):
    """Rounds read as runs (`✕ R1–2   ✓ R3–5`); a few timed rounds get a line each."""
    rounds = [tuple(item) for item in (rounds or ())]
    if not rounds:
        return []
    if len(rounds) <= 3 and any(took or why for _mark, took, why in rounds):
        tw = max(cell_width(took) for _mark, took, _why in rounds)
        lines = []
        for index, (mark, took, why) in enumerate(rounds):
            line = [("R%d" % (index + 1), "dim"), ("  ", None), (mark, _ROUND_KEY.get(mark, "dim")),
                    ("  " + took.ljust(tw), "soft" if mark in "✓●" else "dim")]
            if why:
                line.append(("  " + clip(why, room - 9 - tw),
                             "lvl_r_flat" if mark == "✕" else "lvl_y"))
            lines.append(line)
        return lines
    runs = []
    for index, (mark, _took, _why) in enumerate(rounds):
        if runs and runs[-1][0] == mark:
            runs[-1][2] = index + 1
        else:
            runs.append([mark, index + 1, index + 1])
    lines, current, used = [], [], 0
    for mark, first, last in runs:
        label = "R%d" % first if first == last else "R%d–%d" % (first, last)
        cell = [(mark, _ROUND_KEY.get(mark, "dim")), (" " + label, "soft")]
        span = 2 + cell_width(label)
        if current and used + 3 + span > room:
            lines.append(current)
            current, used = [], 0
        if current:
            current.append(("   ", None))
            used += 3
        current += cell
        used += span
    lines.append(current)
    return lines


def _stage_box(grid, x, y, w, h, index, stage):
    hue = _STAGE_HUE[index % 5]
    status = stage.get("st") if stage.get("st") in _STATE_MARK else "todo"
    verdict_text = str(stage.get("verdict") or status)
    gate = stage.get("kind") == "gate"
    if gate:
        line = ("p", 0, 1)
    elif status == "active":
        line = (hue, 0, 1)
    elif status == "failed":
        line = ("r", 0, 0)
    else:
        line = ("d", 0, 1)
    narrow = w < 34
    if status == "active":
        right = [(" ", None), ("●", "work"), (" " if narrow else " " + verdict_text + " ", "soft")]
    elif gate:
        right = [(" ✓ ", "done")] + ([] if narrow else [(verdict_text + " ", "soft")])
    else:
        mark, key = _STATE_MARK[status]
        right = [(" %s " % mark, key if status != "todo" else "dim")] + (
            [] if narrow else [(verdict_text + " ", "soft" if status != "todo" else "dim")])
    rw = _width(right)
    name = str(stage.get("n"))
    if gate:
        title = [(" ◑ ", "steward"), (clip(name, w - 8 - rw), ("p", 1, 0)), (" ", None)]
    else:
        title = [(" ", None), (clip(name, w - 6 - rw), (hue, 1, 0) if status != "todo" else (hue, 0, 1)),
                 (" ", None)]
    _outline(grid, x, y, w, h, line)
    grid.mark_moved()
    _segs(grid, x + 2, y, title)
    _segs(grid, x + w - 1 - rw, y, right)
    room = w - 4 - _LW
    lines = []
    who = stage.get("who")
    if who:
        harness_name, model = who
        lines.append([_lab("by"), (harness_name, "hb_" + str(harness_name))] +
                     ([(" " + str(model), "soft")] if model else []))
    lines.append([_lab("time"), (clip(str(stage.get("when") or "·"), room), "soft" if status != "todo" else "dim")])
    if gate and stage.get("qa"):
        for question, answer in stage["qa"]:
            lines.append([_lab("Q"), (clip(question, room - cell_width(answer) - 5), "soft"),
                          (" → ", "line"), (answer, "done"), (" ★", "lvl_y")])
        lines.append([_lab(""), ("★ = recommended · both followed", "dim")])
    for k, parts in enumerate(_rounds_lines(stage.get("rounds"), room)):
        if parts and parts[0][0].startswith("R") and len(parts) > 1 and parts[1][0] == "  ":
            lines.append([_lab(parts[0][0])] + parts[2:])
        else:
            lines.append([_lab("rounds" if k == 0 else "")] + parts)
    if stage.get("rounds_note"):
        lines.append([_lab("rounds"), (clip(stage["rounds_note"], room), "dim")])
    if stage.get("gate"):
        gate_key = "done" if status == "done" else ("work" if status == "active" else "dim")
        lines.append([_lab("gate"), (clip(stage["gate"], room - 3), "title_dim"),
                      (" ✓" if status == "done" else "", gate_key)])
    lines.append([_lab("out"), (clip(str(stage.get("out") or ""), room), "title_dim")])
    lines.append([_lab("unit"), (clip(str(stage.get("unit") or ""), room), "dim")])
    if stage.get("says"):
        lines.append([_lab("says"), (clip(stage["says"], room), "now_sub")])
    for k, parts in enumerate(lines[:h - 2]):
        _segs(grid, x + 2, y + 1 + k, parts, x + w - 1)


def _edge_style(stage):
    if stage.get("st") == "todo":
        return ("d", 0, 1)
    if stage.get("st") == "active":
        return ("g", 0, 0)
    return ("g", 0, 1)


def _stage_grid(grid, x0, y, stages, room, bottom, loops=()):
    gap = 4
    per = max(3, min(6, (room + gap) // (36 + gap)))
    if loops and (room + gap) // (_LOOP_MIN_W + gap) >= len(stages):
        per = len(stages)
    per = min(per, len(stages))
    nrows = (len(stages) - 1) // per + 1
    w = (room - gap * (per - 1)) // per
    pitch_gap = 1
    lanes = len(loops) + 1 if loops and nrows == 1 else 0
    spare = per - (len(stages) - (nrows - 1) * per)
    listed = len(loops) + 2 if loops and nrows > 1 and not spare else 0
    avail = bottom - y - lanes - listed
    if avail - (nrows - 1) < 6 * nrows:
        pitch_gap = 0
    h = max(4, min(10, (avail - pitch_gap * (nrows - 1)) // nrows))
    pos = []
    hh = (h - 1) // 2
    for index, stage in enumerate(stages):
        row, k = divmod(index, per)
        col = k if row % 2 == 0 else per - 1 - k
        pos.append((x0 + col * (w + gap), y + row * (h + pitch_gap), row))
        bx, by = pos[-1][0], pos[-1][1]
        if "group" in stage:
            group = stage["group"]
            _stage_box(grid, bx, by, w, hh, index, group[0])
            _stage_box(grid, bx, by + h - hh, w, hh, index, group[1])
            grid.put(bx + 2, by + hh, "∥ 2-way · %s" % (
                "both pass" if all(item.get("st") == "done" for item in group) else "…"), "dim",
                limit=bx + w - 1)
        else:
            _stage_box(grid, bx, by, w, h, index, stage)

    def mids(stage, by):
        return [by + hh // 2, by + h - hh + hh // 2] if "group" in stage else [by + h // 2]

    for index in range(len(stages) - 1):
        (bx, by, row), (nx, ny, nrow) = pos[index], pos[index + 1]
        style = _edge_style(stages[index + 1])
        if nrow == row:
            gx = (bx + w if nx > bx else nx + w) + gap // 2 - 1
            for my in sorted(set(mids(stages[index], by)) | set(mids(stages[index + 1], by))):
                grid.put(gx, my, _ARROWS[0] if nx > bx else _ARROWS[1], style)
        else:
            arrow_y, arrow_x = by + h, bx + w // 2
            if 0 <= arrow_y < grid.rows and 0 <= arrow_x < grid.cols and \
                    grid.cells[arrow_y][arrow_x][0] in (" ", "─"):
                grid.put(arrow_x, arrow_y, _ARROWS[2], style)
    if lanes:
        _loop_lanes(grid, pos, w, y, h, stages, loops)
        return y + h + lanes
    end = max(by for _bx, by, _row in pos) + h
    back = {}
    for frm, to, label in loops:
        back.setdefault(to, []).append((frm, label))
    for to, items in back.items():
        bx, by, _row = pos[to]
        total = sum(int(label.split()[1]) if label.startswith("↺") else 1 for _frm, label in items)
        _segs(grid, bx + 2, by + h - 1, [(" ↺ ", "lvl_y"), ("%d back" % total, "soft"), (" ", None)])
    if not loops:
        return end
    lr = nrows - 1
    if spare:
        cols = range(per - spare, per) if lr % 2 == 0 else range(spare)
        lx, ly, lw = x0 + min(cols) * (w + gap), y + lr * (h + pitch_gap), spare * w + (spare - 1) * gap
        _outline(grid, lx, ly, lw, h, ("y", 0, 1))
        top = ly + 1
    else:
        lx, lw, top = x0, room, end + 1
    one = len({to for _frm, to, _label in loops}) == 1
    head = "back to " + str(stages[loops[0][1]]["n"]) if one else "loops back"
    if spare:
        _segs(grid, lx + 2, ly, [(" ↺ ", "lvl_y"), (clip(head, lw - 8), ("y", 1, 0)), (" ", None)])
    else:
        _segs(grid, lx, top - 1, [("↺ ", "lvl_y"), (head, ("y", 1, 0))])
    listed_rows = []
    for frm, to, label in loops:
        n, why = label.split(" · ", 1) if " · " in label else ("↺ 1", label)
        parts = [(stages[frm]["n"], "soft")] + ([] if one else [(" → ", "line"), (stages[to]["n"], "soft")])
        parts += [("  ×" + n.split()[1], "lvl_y")]
        listed_rows.append((parts, why.replace(" → " + str(stages[to]["n"]), "").replace(
            str(stages[frm]["n"]) + " ", "")))
    with_why = all(_width(parts) + 2 + cell_width(why) <= lw - 4 for parts, why in listed_rows)
    for k, (parts, why) in enumerate(listed_rows):
        _segs(grid, lx + 2, top + k, parts + ([("  " + why, "dim")] if with_why else []), lx + lw - 2)
    return end if spare else end + len(loops) + 2


def _loop_lanes(grid, pos, w, y, h, stages, loops):
    line, head = ("y", 0, 1), ("y", 1, 0)
    yb = y + h - 1
    by_to = {}
    for frm, to, label in loops:
        by_to.setdefault(to, []).append((frm, label))
    row = yb + 2
    for to, items in by_to.items():
        tx, ey = pos[to][0] - 2, yb - 1
        grid.put(tx, ey, "┌", line)
        grid.put(tx + 1, ey, _ARROWS[0], head)
        for yy in range(ey + 1, row):
            grid.put(tx, yy, "│", line)
        items.sort(key=lambda item: item[0])
        for k, (frm, label) in enumerate(items):
            sx = pos[frm][0] + w // 2
            last = k == len(items) - 1
            grid.put(tx, row, "└" if last else "├", line)
            for xx in range(tx + 1, sx):
                grid.put(xx, row, "─", line)
            grid.put(sx, row, "┘", line)
            for yy in range(yb + 1, row):
                grid.put(sx, yy, "│", line)
            grid.put(sx, yb, "┬", ("d", 0, 1))
            if not last:
                grid.put(tx, row + 1, "│", line)
            n, why = label.split(" · ", 1) if " · " in label else ("↺ 1", label)
            text = [(" ↺ ", "lvl_y"), (why.replace(" → " + str(stages[to]["n"]), ""), "soft"),
                    ("  ×" + n.split()[1] + " ", "lvl_y")]
            tw = _width(text)
            if tw > sx - tx - 3:
                text = [(" ↺ ×" + n.split()[1] + " ", "lvl_y")]
                tw = _width(text)
            _segs(grid, tx + max(2, (sx - tx - tw) // 2), row, text)
            row += 1


def _current_leg(grid, x, y, stages, room):
    """The stage that matters now, in its stage hue, then n/m and a thin progress rule."""
    x0 = x
    cur = next((i for i, item in enumerate(stages) if item.get("st") in ("active", "failed")), None)
    if cur is None:
        cur = next((i for i, item in enumerate(stages) if item.get("st") != "done"), len(stages) - 1)
    stage = stages[cur]
    hue = _STAGE_HUE[cur % 5]
    name_style = ("r", 1, 0) if stage.get("st") == "failed" else (hue, 1, 0)
    limit = x0 + max(0, room)
    x = grid.put(x, y, clip(str(stage.get("n") or stage.get("name")), max(0, room)),
                 name_style, limit=limit)
    if stage.get("st") != "active":
        mark, key = _STATE_MARK.get(stage.get("st"), ("·", "dim"))
        x = grid.put(x + 1, y, mark, key, limit=limit)
    x += 2
    done = sum(1 for item in stages if item.get("st") == "done")
    x = grid.put(x, y, "%d/%d" % (done, len(stages)), "soft", limit=limit) + 1
    cells = max(0, min(12, limit - x))
    fill = round(cells * done / len(stages))
    for i in range(cells):
        x = grid.put(x, y, "━" if i < fill else "─", (hue, 0, 0) if i < fill else ("d", 0, 1))
    return x


def _scroll_strip(grid, state, x0, x1, y, head, layout, rows, key, tail=True):
    """A framed text window named on its top edge; the bottom edge says where the window sits."""
    n = len(layout)
    top = max(0, n - rows)
    offset = max(0, min(int(dict(getattr(state, "scroll", ()) or ()).get(key, 0)), top))
    start = max(0, n - rows - offset) if tail else offset
    shown = layout[start:start + rows]
    where = ("last %d rows" % len(shown) if tail and not offset
             else "%d–%d / %d rows" % (start + 1, start + len(shown), n))
    y1 = y + rows + 1
    for xx in range(x0 + 1, x1):
        grid.put(xx, y, "─", "line")
        grid.put(xx, y1, "─", "line")
    for yy in range(y + 1, y1):
        grid.put(x0, yy, "│", "line")
        grid.put(x1, yy, "│", "line")
    for (xx, yy), glyph in (((x0, y), "┌"), ((x1, y), "┐"), ((x0, y1), "└"), ((x1, y1), "┘")):
        grid.put(xx, yy, glyph, "line")
    _segs(grid, x0 + 2, y, [(" ", None)] + list(head) + [(" ", None)], x1 - 1)
    _segs(grid, x0 + 2, y1, [(" ", None), (where, "dim")] +
          ([("  ·  drag·wheel ↕", "dim")] if n > rows else []) + [(" ", None)], x1 - 1)
    for yy in range(y, y + rows + 2):
        grid.add_hit(yy, x0, x1 + 1, ("scroll", key, top) + (() if tail else (-1,)))
    for k, (segments, tint) in enumerate(shown):
        if tint:
            _tint_row(grid, y + 1 + k, x0 + 1, x1, tint)
        _segs(grid, x0 + 2, y + 1 + k, segments, x1 - 1)


def _route_after(route, nodes):
    legs = tuple(route.get("legs") or ())
    if route.get("shape") == "framed" and nodes and nodes[0].get("kind") == "frame":
        return "frame-review and the legs after it are outside this snapshot"
    if nodes and nodes[0].get("kind") == "leg":
        if len(legs) > 1:
            parts = []
            for name, status, progress in legs[:-1]:
                mark = _STATE_MARK.get(status, ("·", "dim"))[0]
                parts.append(str(name) + " " + mark + (" " + str(progress) if progress else ""))
            return "before this leg: %s — outside this snapshot" % " · ".join(parts)
        if not route.get("session_tag"):
            return "the route's frame is outside this snapshot"
    return ""


def draw_route_detail(grid, vm, route, x, use, y, state=None, bottom=None):
    """Route frame, node band and the picked node's panel from row y to bottom."""
    right = min(grid.cols - 1, x + use)
    bottom = max(y, grid.rows - 5) if bottom is None else bottom
    ident = str(route.get("id") or "")
    nodes = tuple((vm.nodes or {}).get(ident, ()))
    frame_key = ("c", 0, 1)
    for xx in range(x + 1, right):
        grid.put(xx, bottom, "═", frame_key)
    for yy in range(y + 1, bottom):
        grid.put(x, yy, "║", frame_key)
        grid.put(right, yy, "║", frame_key)
    for (xx, yy), glyph in (((x, y), "╔"), ((right, y), "╗"), ((x, bottom), "╚"), ((right, bottom), "╝")):
        grid.put(xx, yy, glyph, frame_key)
    if not nodes:
        for xx in range(x + 1, right):
            grid.put(xx, y, "═", frame_key)
        grid.put(x + 3, y + 2, clip("이 route의 stage 기록을 읽지 못했습니다 (route 기록 없음)", max(0, use - 6)),
                 "dim", limit=right)
        grid.mark_moved()
        return bottom + 1
    tag = route.get("session_tag")
    repo_text = repo_rule_label(route.get("repo"))
    head = [(" ", None), *(((repo_text, "title_dim"), (" › ", "line")) if repo_text else ())]
    if tag:
        head += [("[", "tag_b"), (str(tag), "soft"), ("]", "tag_b"), (" › ", "line")]
    leg_count = sum(1 for item in route.get("legs") or () if str(item[0]) != "frame")
    if route.get("detail_trailer") is not None:
        # The source states how long the route ran and what its record holds; nothing is derived.
        trailer = "  " + str(route["detail_trailer"]).strip()
    elif any(node.get("kind") == "gate" for node in nodes):
        trailer = f"  {route.get('age') or '·'} · {leg_count} leg · human gate answered"
    else:
        count = len(route.get("legs") or ())
        trailer = f"  {route.get('age') or '·'} · {count} leg{'s' if count != 1 else ''}"
    head += [(str(route.get("detail_title") or route.get("title") or "·"), "head"), (trailer, "dim")]
    if route.get("shape"):
        head.append((" · " + str(route["shape"]), "dim"))
    title_start = x + 2
    title_parts = head + [(" ", None)]
    title_end = min(right - 1, title_start + _width(title_parts))
    for xx in range(x + 1, right):
        if xx < title_start or xx >= title_end:
            grid.put(xx, y, "═", frame_key)
    _segs(grid, title_start, y, title_parts, right - 1)

    got = ""
    if state is not None and hasattr(state, "opened_for"):
        got = str(state.opened_for("ui:node", "") or "")
    if got.startswith(ident + ":") and got.split(":")[-1].isdigit():
        selected = min(int(got.split(":")[-1]), len(nodes) - 1)
    else:
        selected = next((i for i, node in enumerate(nodes) if node.get("st") == "active"), len(nodes) - 1)
    band_y = y + 1
    room = use - 6
    width = min(_BAND_W, (room - _BAND_GAP * (len(nodes) - 1)) // len(nodes))
    centre = x + 3 + width // 2
    for index, node in enumerate(nodes):
        bx = x + 3 + index * (width + _BAND_GAP)
        status = str(node.get("st") or "todo")
        gate = node.get("kind") == "gate"
        picked = index == selected
        if picked:
            line = ("v", 0, 0)
        elif gate:
            line = ("p", 0, 1)
        elif status == "active":
            line = ("y", 0, 1)
        else:
            line = ("d", 0, 1)
        name_key = ("p", 1, 0) if gate else ("w", 1, 0) if status != "todo" else ("d", 0, 1)
        if status == "active":
            tail = [(" ", None), ("●", "work"), (" running ", "soft")]
        else:
            mark, key = _STATE_MARK.get(status, ("·", "dim"))
            tail = [(" %s " % mark, key), (str(node.get("verdict") or status) + " ", "soft")]
        name_room = width - 6 - (3 if gate else 1) - _width(tail)
        tail_x = bx + width - 1 - _width(tail)
        _outline(grid, bx, band_y, width, _BAND_H, line)
        grid.mark_moved()
        for hit_y in range(band_y, band_y + _BAND_H):
            grid.add_hit(hit_y, bx, bx + width, ("ui", "node", f"{ident}:{index}"))
        _segs(grid, bx + 2, band_y, ([ (" ◑ ", "steward")] if gate else [(" ", None)]) +
              [(clip(str(node.get("n")), max(4, name_room)), name_key), (" ", None)], tail_x)
        _segs(grid, tail_x, band_y, tail)
        node_lines = [list(item) for item in (node.get("lines") or ())]
        detail = [dict(item) if hasattr(item, "keys") else item for item in (node.get("detail") or ())]
        band_lines = ([None] if node.get("kind") == "leg" and detail else []) + node_lines
        for k, parts in enumerate(band_lines[:_BAND_H - 2]):
            if parts is None:
                end = bx + width - 2
                label_x = bx + 2
                label_room = max(0, end - label_x)
                label_text = "stage".ljust(_LW) if label_room >= _LW else clip("stage", label_room)
                label_end = grid.put(label_x, band_y + 1 + k,
                                     label_text, "dim", limit=end)
                _current_leg(grid, label_end, band_y + 1 + k, detail, max(0, end - label_end))
            else:
                _segs(grid, bx + 2, band_y + 1 + k, parts, bx + width - 1)
        if index:
            grid.put(bx - _BAND_GAP // 2 - 1, band_y + _BAND_H // 2, _ARROWS[0],
                     _edge_style(node))
        if picked:
            centre = bx + width // 2
            grid.put(centre, band_y + _BAND_H - 1, "┬", line)
            grid.put(centre, band_y + _BAND_H, "│", line)
    band_end = x + 3 + len(nodes) * (width + _BAND_GAP) - _BAND_GAP
    after = route["detail_after"] if "detail_after" in route else _route_after(route, nodes)
    if after:
        # A note that outruns the frame is cut at the screen edge, over the frame's right rail.
        grid.put(band_end + 3, y + 2, str(after), "dim")

    node = nodes[selected]
    panel_left, panel_right = x + 2, right - 2
    panel_y = y + _BAND_H + 2
    style = ("d", 0, 0)
    for yy in range(panel_y + 1, bottom):
        grid.put(panel_left, yy, "│", style)
        grid.put(panel_right, yy, "│", style)
    grid.put(panel_left, panel_y, "┌", style)
    grid.put(panel_right, panel_y, "┐", style)
    grid.put(panel_left, bottom, "╧", ("c", 0, 1))
    grid.put(panel_right, bottom, "╧", ("c", 0, 1))
    title = [(" ", None)] + [tuple(item) for item in (node.get("title") or ())] + [(" ", None)]
    tw = _width(title)
    tx = panel_left + 2 if panel_left + 2 + tw < centre - 1 else centre + 2
    title_end = min(panel_right - 1, tx + tw)
    for xx in range(panel_left + 1, panel_right):
        if xx != centre and (xx < tx or xx >= title_end):
            grid.put(xx, panel_y, "─", style)
    _segs(grid, tx, panel_y, title, panel_right - 1)
    grid.put(centre, panel_y, "┴", ("v", 0, 0))
    stages = [dict(item) if hasattr(item, "keys") else item for item in (node.get("detail") or ())]
    stage_room = panel_right - panel_left - 4
    if len(stages) < 3:
        stage_room = min(stage_room, 64 * len(stages))
    stage_bottom = bottom
    attempts = vm.attempts or {}
    raw_lines = (tuple(attempts.get("lines", ()))
                 if str(attempts.get("route") or "") == ident and node.get("kind") == "leg" else ())
    if raw_lines:
        per = min(len(stages), max(3, min(6, (stage_room + 4) // 40)))
        need = ((len(stages) - 1) // per + 1) * 4
        raw_rows = min(_RAW_ROWS, stage_bottom - panel_y - 3 - need)
        if raw_rows >= 2:
            from .transcript import dialogue_rows
            layout = dialogue_rows(raw_lines, max(1, (panel_right - 2) - (panel_left + 2) - 3), "claude")
            head_parts = [("owner raw", "soft"),
                          (" · attempt-view " + str((vm.attempts or {}).get("att", "·")), "dim")]
            _scroll_strip(grid, state, panel_left + 2, panel_right - 2, stage_bottom - raw_rows - 2,
                          head_parts, layout, raw_rows, "rawoff")
            stage_bottom -= raw_rows + 2
    if stages:
        _stage_grid(grid, panel_left + 2, panel_y + 1, stages, stage_room, stage_bottom,
                    tuple(tuple(item) for item in (node.get("loops") or ())))
    grid.mark_moved()
    return bottom + 1



def draw_history_detail(grid, vm, state, tag, x, right, y, bottom):
    generations = tuple((vm.generations or {}).get(tag, ()))
    selected = state.opened_for("ui:gen:" + tag)
    index = int(selected) if selected and selected.isdigit() else next(
        (i for i in range(len(generations) - 1, -1, -1)
         if isinstance(generations[i], Mapping) and generations[i].get("card")),
        len(generations) - 1)
    if not 0 <= index < len(generations):
        return y
    generation = generations[index]
    card_block = (vm.cards or {}).get(tag, {})
    ordered_cards = tuple(card_block.get("prior_cards", ())) + tuple(card_block.get("cards", ()))
    card = None
    if isinstance(generation, Mapping) and generation.get("card"):
        position = sum(1 for item in generations[:index]
                       if isinstance(item, Mapping) and item.get("card"))
        card = ordered_cards[position] if position < len(ordered_cards) else None
    dialogue = (vm.dialogue or {}).get("logs", {}).get(f"{tag}:{index}", {})
    lines = tuple(dialogue.get("lines", ()))
    from .transcript import card_rows, dialogue_rows

    card_lines = tuple(card.get("lines", ())) if card else ()
    card_layout = card_rows(card_lines, max(1, right - x - 5)) if card else []
    dialogue_layout = dialogue_rows(lines, max(1, right - x - 4),
                                    str(generation.get("harness") or "claude")) if lines else []
    room = max(0, bottom - y)
    if card_layout and dialogue_layout:
        card_height = max(3, min(len(card_layout), (room - 4) // 2))
        dialogue_height = max(2, room - card_height - 4)
    elif card_layout:
        card_height, dialogue_height = max(3, min(len(card_layout), room - 2)), 0
    else:
        card_height, dialogue_height = 0, max(2, room - 2)

    def box(left, top, width, height, title, foot, body_rows, scroll_key, tint_rows=False, tail=True):
        last_x = min(right, left + width)
        frame = "dim"
        grid.put(left, top, "┌", frame)
        grid.put(left + 1, top, "─" * max(0, last_x - left - 1), frame, limit=last_x)
        grid.put(last_x, top, "┐", frame)
        title_parts = [(" ", "branch_s")] + title + [(" ", "branch_s")]
        cursor = left + 2
        for text, key in title_parts:
            cursor = grid.put(cursor, top, text, key, limit=last_x - 1)
        for row in range(top + 1, top + height + 1):
            grid.put(left, row, "│", frame)
            grid.put(last_x, row, "│", frame)
        grid.put(left, top + height + 1, "└", frame)
        grid.put(left + 1, top + height + 1, "─" * max(0, last_x - left - 1), frame, limit=last_x)
        grid.put(last_x, top + height + 1, "┘", frame)
        cursor = grid.put(left + 2, top + height + 1, " ", "branch_s", limit=last_x)
        footer_parts = foot if isinstance(foot, (list, tuple)) else ((foot, "dim"),)
        for text, key in footer_parts:
            cursor = grid.put(cursor, top + height + 1, text, key, limit=last_x)
        grid.put(cursor, top + height + 1, " ", "branch_s", limit=last_x)
        total = len(body_rows)
        max_offset = max(0, total - height)
        offset = int(dict(state.scroll or ()).get(scroll_key, 0))
        offset = max(0, min(offset, max_offset))
        start = max(0, total - height - offset) if tail else offset
        visible = body_rows[start:start + height]
        for row_index, (parts, tint) in enumerate(visible):
            yy = top + 1 + row_index
            if tint_rows and tint is not None:
                grid.fill(left + 1, last_x - 1, yy, " ", tint=tint)
            if tint_rows:
                grid.fill(left + 2, left + 4, yy, " ", "branch_s")
            cursor = left + 2
            for text, key in parts:
                cursor = grid.put(cursor, yy, text, key, limit=last_x - 1)
            if tint_rows:
                grid.fill(left + 2, left + 4, yy, " ", "branch_s")
        hit_right = last_x + 1 if tail else last_x
        action = ("scroll", scroll_key, max_offset) if tail else ("scroll", scroll_key, max_offset, -1)
        for hit_y in range(top, top + height + 2):
            grid.add_hit(hit_y, left, hit_right, action)

    pane_width = max(0, right - x - 1)
    if card_layout and card_height:
        title = [(f"card · gen {index + 1}", "head"),
                 ("  " + str(generation.get("at") or "") + " · ", "dim"),
                 (str(generation.get("harness") or "claude"), "hb_" + str(generation.get("harness") or "claude")),
                 (" " + str(generation.get("sid") or "") + " · " +
                  f"{int(card.get('chars') or 0) / 1000:.1f}k chars", "dim")]
        offset = int(dict(state.scroll or ()).get(f"card:{tag}:{index}", 0))
        offset = max(0, min(offset, max(0, len(card_layout) - card_height)))
        shown = card_layout[max(0, len(card_layout) - card_height - offset):][:card_height]
        foot = f"{offset + 1}–{min(len(card_layout), offset + card_height)} / {len(card_layout)} rows"
        if len(card_layout) > card_height:
            foot += "  ·  drag·wheel ↕"
        box(x, y, pane_width, card_height, title, foot, card_layout,
            f"card:{tag}:{index}", tail=False)
        y += card_height + 2
    if dialogue_layout and dialogue_height and y < bottom:
        harness = str(generation.get("harness") or "claude")
        title = [("conversation", "soft"), (" · ", "dim"), (harness, "hb_" + harness),
                 (" " + str(generation.get("sid") or ""), "dim")]
        total = len(dialogue_layout)
        offset = max(0, min(int(dict(state.scroll or ()).get(f"log:{tag}:{index}", 0)),
                            max(0, total - dialogue_height)))
        start = max(0, total - dialogue_height - offset)
        shown = len(dialogue_layout[start:start + dialogue_height])
        where = (f"last {shown} rows" if not offset
                 else f"{start + 1}–{start + shown} / {total} rows")
        box(x, y, pane_width, dialogue_height, title,
            [(where, "dim")] + ([("  ·  drag·wheel ↕", "dim")] if total > dialogue_height else []),
            dialogue_layout, f"log:{tag}:{index}", tint_rows=True)
        y += dialogue_height + 2
    return y
