"""Overview and list-view tables for the immutable explorer projection."""
from .grid import CellGrid

X = 34
GAP = 3
IND = 2
HARN_W = 25
CTX_W = 13
AGE_W = 6


NO_PROJECT_LABELS = frozenset({"(unknown)", "(root)"})


def repo_label(value):
    """The repo name to draw: empty when the project is unknown (the default board draws no label for it)."""
    text = "" if value is None else str(value)
    return "" if text in NO_PROJECT_LABELS else text


def repo_rule_label(value, limit=None):
    """``name/`` for a named repo, empty for the nameless group; clipped to ``limit`` cells."""
    text = repo_label(value)
    if not text:
        return ""
    text += "/"
    return _clip(text, limit) if limit else text


def gauge_cells(cells, pct):
    """Filled cells of a ``cells``-wide gauge: none at 0, all at 100, otherwise at least one and never all."""
    if pct <= 0:
        return 0
    if pct >= 100:
        return cells
    return max(1, min(cells - 1, int(pct * cells / 100 + 0.5)))


def _cell(value, missing="·"):
    return missing if value is None or value == "" else str(value)


def draw_overview(grid: CellGrid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right = x + use + 1
    grid.fill(0, x - 2, y, " ", tint=236)
    grid.fill(x, grid.cols - 1, y, " ", tint=234)
    grid.put(x - 2, y, "┃", "line")
    grid.put(1, y, "OVERVIEW", "head", tint=236)
    c = _overview_columns(use)
    for offset, label in ((IND + 3, "ID"), (c["h"], "HARNESS"),
                          *(((c["title"], "TITLE"),) if c["title_w"] else ()), (c["ctx"], "CTX"),
                          *(([(c["now"], "NOW")] if c["now"] is not None else [])),
                          (use - 2, "AGE")):
        grid.put(x + offset, y, label, "head", tint=234, limit=right)
    y += 1
    repos = tuple(vm.repos or ())
    sessions = tuple(vm.sessions or ())
    for repo in repos:
        name = repo_label(repo.get("name", "·"))
        mine = [session for session in sessions
                if session.get("repo_id", session.get("repo")) == repo.get("id", repo.get("name", "·"))]
        if not mine:
            continue                      # a rule is drawn only above rows that are drawn under it
        page_scope = getattr(state, "scope", "-") != "-"
        if name:
            if page_scope:
                grid.add_hit(y, x, right, ("scope", "repo:" + str(repo.get("id", name))))
            grid.put(x + 1, y, "┈ ", "line")
            end = grid.put(x + 3, y, name + "/", "title_dim") + 2
            grid.put(end, y, " ", "branch_s")
            for xx in range(end + 2, right):
                grid.put(xx, y, "┈", "line")
            y += 1
        for session in mine:
            tag = str(session.get("tag") or "·")
            if page_scope:
                grid.add_hit(y, x, right, ("scope", tag))
            else:
                grid.add_hit(y, x, right, ("fold", "sopen:OVERVIEW", tag))
            row_y = y
            if state.opened == tag:
                grid.put(x, y, "▶", "soft")
            status = session.get("state")
            marker, marker_key = {"working": ("⠏", "spin"), "blocked": ("●", "blocked"),
                                  "dead": ("✕", "dead"), "done": ("✓", "done")}.get(
                                      status, ("●", "idle"))
            grid.put(x + IND + 1, y, marker, marker_key)
            tag_x = x + IND + 3
            grid.put(tag_x, y, "[", "dim")
            grid.put(tag_x + 1, y, tag, "steward" if session.get("steward") else "soft")
            grid.put(tag_x + 1 + len(tag), y, "]", "dim")
            if session.get("steward"):
                grid.put(x + IND + 8, y, "⚑", "steward")
            harness = str(session.get("harness") or "other")
            if c["narrow"]:
                grid.put(x + c["h"], y, harness, "hb_" + harness)
                if session.get("effort"):
                    grid.put(x + c["h"] + len(harness), y, "·" + str(session["effort"]), "eff_xhigh")
            elif not session.get("model"):
                # No model value: the harness alone, as the default board draws a row without telemetry.
                grid.put(x + c["h"], y, harness, "hb_" + harness)
            else:
                model = session.get("model") or ("·", "dim")
                model_name, model_key = (tuple(model) + ("dim",))[:2]
                effort_cells = len(str(session.get("effort") or ""))
                room = (c["title"] - c["h"] - GAP) - len(harness) - 2 - 1 - (1 + effort_cells if effort_cells else 0)
                model_name = _clip(model_name, max(1, room))
                grid.put(x + c["h"], y, harness, "hb_" + harness)
                grid.put(x + c["h"] + len(harness), y, " (", "dim")
                model_style = ("g_idle" if harness == "codex" and session.get("state") != "working"
                               else "hb_" + harness)
                grid.put(x + c["h"] + len(harness) + 2, y, model_name, model_style)
                pos = x + c["h"] + len(harness) + 2 + len(model_name)
                effort = str(session.get("effort") or "")
                if effort:
                    grid.put(pos, y, "·", "dim")
                    grid.put(pos + 1, y, effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim")
                    pos += 1 + len(effort)
                grid.put(pos, y, ")", "dim")
            if c["title_w"]:
                title = str(session.get("title") or "·")
                # The checkout follows the title in parentheses (12 cells at most); the title gives way
                # first, and a row whose branch is unknown draws no parentheses at all.
                branch = session.get("branch")
                tail = " (%s)" % _clip(str(branch), 12) if branch not in (None, "") else ""
                room = max(6, c["title_w"] - _seg_width(tail))
                title_text = _clip(title, room)
                bold_key = {"claude": ("c", 1, 0), "codex": ("m", 1, 0),
                            "opencode": ("l", 1, 0)}.get(harness, "unknown")
                title_end = grid.put(x + c["title"], y, title_text, bold_key)
                if tail:
                    grid.put(title_end, y, tail, "dim", limit=x + c["title"] + c["title_w"])
            context = session.get("context")
            if not c["narrow"] and context is not None:
                gauge_width = max(1, round(8 * float(session.get("window") or 1)))
                filled = gauge_cells(gauge_width, int(context))
                gauge_key = ("g_blocked" if int(context) >= 80 else
                             "g_idle" if int(context) >= 50 else "done")
                grid.put(x + c["ctx"], y, "━" * filled, gauge_key)
                grid.put(x + c["ctx"] + filled, y, "─" * (gauge_width - filled), "dim")
                percent_key = gauge_key if int(context) >= 50 else "dim"
                grid.put(x + c["ctx"] + 9, y, f"{int(context):3d}%", percent_key)
            elif context is not None:
                narrow_key = ("g_blocked" if int(context) >= 80 else
                              "g_idle" if int(context) >= 50 else "dim")
                grid.put(x + c["ctx"], y, f"{int(context):3d}%", narrow_key)
            grid.put(x + use - len(str(session.get("age") or "")), y,
                     str(session.get("age") or ""), "dim")
            if c["now"] is not None:
                now_start = x + c["now"]
                now_end = x + use - AGE_W - GAP
                ask = str(session.get("ask") or "")
                if ask:
                    ask_text = _clip(ask, max(0, now_end - now_start))
                    grid.put(now_start, y, ask_text, "blocked", limit=now_end)
                    now_start += len(ask_text) + 2
                now_text = _clip(str(session.get("now") or ""), max(0, now_end - now_start))
                now_key = "head" if session.get("state") in ("working", "blocked") else "title_dim"
                grid.put(now_start, y, now_text, now_key,
                         limit=now_end)
            y += 1
            gpu_count = sum(1 for host in (vm.compute or ()) for gpu in host[5]
                            if tag in (gpu[7] or ()))
            if gpu_count > 1 and y < grid.rows - 5:
                grid.put(x + c["h"], y, "●", "work")
                grid.put(x + c["h"] + 2, y, f"GPU ×{gpu_count}", "dim")
                y += 1
            if state.opened == tag:
                _tint_row(grid, row_y, x - 1, right, 236)
                y = _session_unfold(grid, vm, x, use, y, session, c)
            if y >= grid.rows - 5:
                grid.put(right - 18, grid.rows - 5, "↓ more below", "dim", limit=right)
                return
    sessions_total = len(sessions)
    working_sessions = sum(1 for item in sessions if item.get("state") == "working")
    questions = len(vm.questions or ())
    from .chrome import count_word
    summary = (f"{count_word(sessions_total, 'session')} · {working_sessions} working · "
               f"{questions} needs answer · {count_word(sum(1 for repo in repos if repo_label(repo.get('name', '·'))), 'project')}")
    grid.put(x + 1, y, summary, "dim")                # the summary runs to the screen edge and is cut there
    y += 2
    route_rows = [route for route in vm.routes if route.get("session_tag")]
    open_routes = sum(1 for route in route_rows if route.get("state") != "done")
    stopped = sum(1 for route in route_rows if route.get("state") == "dead")
    grid.put(x, y, "── ", "line")
    label_x = grid.put(x + 3, y, "ROUTES", "head")
    scoped_page = getattr(state, "scope", "-") != "-"
    meta = f"  {open_routes} open" + (f" · {stopped} stopped leg{'s' if stopped != 1 else ''}"
                                      if stopped else "")
    meta_end = grid.put(label_x, y, meta, "dim", limit=right)
    grid.put(meta_end, y, " ", "branch_s")
    for cell_x in range(meta_end + 1, right):
        grid.put(cell_x, y, "─", "line")
    mix = _harness_mix(route_rows)
    mix_width = sum(len(text) for text, _key in mix)
    mix_x = x + use - 2 - mix_width
    if mix_x - x >= 40:                      # only where it clears the section's own title
        cursor = mix_x
        for text, key in mix:
            cursor = grid.put(cursor, y, text, key, limit=right)
    y += 1
    if scoped_page:
        from types import SimpleNamespace
        live_routes = tuple(route for route in vm.routes if route.get("state") != "done")
        from dataclasses import replace
        y = draw_routes(grid, replace(vm, routes=live_routes),
                        SimpleNamespace(view="repo:" + str(vm.repos[0].get("id", "")),
                                        opened_for=state.opened_for),
                        x=x, y=y) + 1
        recent_y = y
        grid.put(x, y, "── ", "line")
        recent_end = grid.put(x + 3, y, "RECENT", "head", limit=right)
        grid.put(recent_end, y, "  ", "dim", limit=right)
        recent_end = grid.put(recent_end + 2, y, "completed results", "dim", limit=right)
        grid.put(recent_end, y, " ", "branch_s", limit=right)
        for xx in range(recent_end + 1, right):
            grid.put(xx, y, "─", "line")
        y += 1
        for repo in repos:
            if not repo.get("done") or not repo_label(repo.get("name")) or y >= grid.rows - 5:
                continue
            grid.add_hit(y, x, right, ("scope", "repo:" + str(repo.get("id", repo.get("name", "")))))
            _recent_row(grid, repo, x, use, y)
            y += 1
        y = recent_y + 7           # the brief starts a fixed seven rows under RECENT's header
        if recent_y + 9 >= grid.rows - 5:
            return                 # ... and only where its own header and a row still fit
        scoped_seats = tuple(seat for seat in vm.seats
                             if len(seat) > 1 and str(seat[1]) == str(vm.repos[0].get("id", "")))
        generations = sum(int(seat[2]) for seat in scoped_seats if len(seat) > 2)
        grid.put(x, y, "── ", "line")
        history_end = grid.put(x + 3, y, "HISTORY", "sect")
        history_end = grid.put(history_end, y,
                               f"  {len(scoped_seats)} seat{'s' if len(scoped_seats) != 1 else ''} · {generations} generations",
                               "dim", limit=right)
        grid.put(history_end, y, " ", "branch_s", limit=right)
        for xx in range(history_end + 1, right):
            grid.put(xx, y, "─", "line")
        y += 1
        previous_repo = None
        c_id, c_h, c_gen = 17, 24, 33
        gen_width = max(8, min(24, use - c_gen - 3 - 3 - 18))
        c_last = c_gen + 3 + gen_width + 3
        for seat in scoped_seats:
            if y >= grid.rows - 5:
                break
            tag = str(seat[0])
            session = next((item for item in sessions if str(item.get("tag")) == tag), {})
            grid.add_hit(y, x, right - 1, ("root", "HISTORY"))
            repo_name = str(seat[1]) if len(seat) > 1 else "·"
            if repo_name != previous_repo:
                grid.put(x + 1, y, repo_rule_label(repo_name, 13), "title_dim")
                previous_repo = repo_name
            grid.put(x + c_id, y, "[", "dim")
            grid.put(x + c_id + 1, y, tag, "steward" if session.get("steward") else "soft")
            grid.put(x + c_id + 1 + len(tag), y, "]", "dim")
            harness_x = x + c_h
            grid.put(harness_x, y, str(session.get("harness") or "·"),
                     "hb_" + str(session.get("harness") or "other"))
            count = int(seat[2]) if len(seat) > 2 else 0
            grid.put(x + c_gen, y, f"{count:2d} ", "soft")
            marks = tuple(seat[6]) if len(seat) > 6 else tuple((vm.generations or {}).get(tag, ()))
            from .history import _gen_strip
            _gen_strip(grid, x + c_gen + 3, y, marks, gen_width, session.get("harness") or "·")
            last_clear = str(seat[3] if len(seat) > 3 else "·")
            if x + c_last + _seg_width("clear " + last_clear) < right:       # nothing cut: it fits or is left out
                grid.put(x + c_last, y, "clear " + last_clear, "dim", limit=right)
            y += 1
        return
    repo_order = {repo.get("id", repo.get("name", "")): index for index, repo in enumerate(repos)}
    route_rows.sort(key=lambda route: repo_order.get(route.get("repo"), len(repo_order)))
    for route in route_rows:
        if getattr(state, "scope", "-") == "-":
            grid.add_hit(y, x + IND + 1, right,
                         ("root", "ROUTES", {"ropen:ROUTES": route.get("id")}))
        source = next((session for session in vm.sessions
                       if str(session.get("tag")) == str(route.get("session_tag"))), None)
        _draw_overview_route(grid, x, use, y, route,
                             steward=bool(scoped_page and (source or {}).get("steward")))
        y += 1
    y += 1
    grid.put(x, y, "── ", "line")
    recent_title_end = grid.put(x + 3, y, "RECENT", "head", limit=right)
    grid.put(recent_title_end, y, "  ", "dim", limit=right)
    recent_title_end = grid.put(recent_title_end + 2, y, "completed results", "dim", limit=right)
    grid.put(recent_title_end, y, " ", "branch_s")
    for cell_x in range(recent_title_end + 1, right):
        grid.put(cell_x, y, "─", "line")
    y += 1
    for repo in repos:
        if not repo.get("done") or not repo_label(repo.get("name")) or y >= grid.rows - 5:
            continue
        _recent_row(grid, repo, x, use, y)
        y += 1


def _recent_row(grid, repo, x, use, y):
    """One completed repo: mark, name (12 cells), what finished, and how long ago."""
    grid.put(x + 1, y, "✓", "done")
    grid.put(x + 3, y, _clip(str(repo.get("name", "")) + "/", 12), "soft")
    grid.put(x + 3 + 12 + GAP, y, _clip(str(repo.get("done") or ""), use - 3 - 12 - 2 * GAP - 8), "now_sub")
    age = str(repo.get("age") or "")
    grid.put(x + use - len(age), y, age, "dim")


def _harness_mix(routes):
    counts = {"claude": 0, "codex": 0, "opencode": 0}
    for route in routes:
        if route.get("state") in ("dead", "done"):
            continue
        owner = str(route.get("harness") or "")
        if owner in counts:
            counts[owner] += 1
        subworker = route.get("subworker")        # only the depth-2 workers the tree draws
        if subworker and len(subworker) > 1 and str(subworker[1]) in counts:
            counts[str(subworker[1])] += 1
    return [(" owner·worker ", "dim"),
            ("claude", "hb_claude" if counts["claude"] else "dim"),
            (f" {counts['claude']}", "soft" if counts["claude"] else "dim"), (" · ", "dim"),
            ("codex", "hb_codex" if counts["codex"] else "dim"),
            (f" {counts['codex']}", "soft" if counts["codex"] else "dim"),
            (" · ", "dim"),
            ("opencode", "hb_opencode" if counts["opencode"] else "dim"),
            (f" {counts['opencode']}", "soft" if counts["opencode"] else "dim"),
            (" ", "branch_s")]


def _wrap_cells(text, width):
    rows, current, used = [], "", 0
    for char in str(text):
        w = _seg_width(char)
        if used + w > width:
            rows.append(current)
            current, used = "", 0
        current += char
        used += w
    return rows + [current]


def _session_unfold(grid, vm, x, use, y, session, c):
    """Under an unfolded session row: NOW in full, each route on a line, its question."""
    tag = str(session.get("tag"))
    right = x + use + 1
    x_lab, x_val = x + c["h"], x + c["h"] + 7
    grid.put(x_lab, y, "NOW", "label")
    now = session.get("now")
    for line in (_wrap_cells(now, x + use - x_val) if now else ["·"]):
        grid.put(x_val, y, line, "now_sub" if now else "dim")
        y += 1
    grid.put(x_lab, y, "ROUTE", "label")
    rows = [route for route in vm.routes
            if route.get("session_tag") == tag and route.get("state") != "done"]
    for route in rows:
        grid.add_hit(y, x_val, right, ("root", "ROUTES", {"ropen:ROUTES": route.get("id")}))
        _draw_route_line(grid, x_val, x + use, y, route, tag=False, repo=False)
        y += 1
    if not rows:
        grid.put(x_val, y, "·", "dim")
        y += 1
    grid.put(x_lab, y, "ASK", "label")
    question = next((item for item in (vm.questions or ()) if item.get("tag") == tag), None)
    if question:
        lead = [(str(question.get("kind")), "blocked"),
                (" · waiting " + str(question.get("wait")) + " · ", "dim")]
        room = x + use - x_val - sum(_seg_width(text) for text, _key in lead) - 10
        title_room = max(0, room)
        from .grid import put_fit
        cursor = put_fit(grid, x_val, y, lead + [(_clip(question.get("title") or "", title_room)
                                                  if title_room else "", "soft"),
                                                 ("  ", "branch_s")], limit=right - 1 - 8)
        grid.put(cursor, y, "(open ▸)", "hearting", limit=right)
        grid.add_hit(y, cursor, cursor + 8, ("pane", tag, str(question.get("pane") or "")))
    else:
        grid.put(x_val, y, "·", "dim")
    return y + 2


def _draw_overview_route(grid, x, use, y, route, steward=False):
    _draw_route_line(grid, x + IND + 1, x + use, y, route, steward=steward)


def _draw_route_line(grid, x0, x1, y, route, tag=True, repo=True, steward=False):
    room = x1 - x0 - 2 - (4 + GAP if tag else 0) - 16 - GAP - 24 - GAP - AGE_W
    own = room >= 8 + GAP + (12 + GAP if repo else 0)
    repo = repo and room >= 12 + GAP
    state = "working" if route.get("activity") == "running" else route.get("state")
    marker, marker_key = {"working": ("⠏", "g_spin"), "dead": ("✕", "g_dead"),
                          "blocked": ("●", "g_blocked"), "done": ("✓", "grp_live")}.get(
                              state, ("●", "g_work_off"))
    grid.put(x0, y, marker, marker_key)
    x = x0 + 2
    if tag:
        tag_text = str(route.get("session_tag") or "··")
        grid.put(x, y, "[", "dim")
        grid.put(x + 1, y, tag_text, "steward" if steward else "soft")
        grid.put(x + 1 + len(tag_text), y, "]", "dim")
        x += 4 + GAP
    if own:
        owner = str(route.get("harness") or "·")
        grid.put(x, y, owner, "hb_" + owner)
        x += 8 + GAP
    if repo:
        grid.put(x, y, repo_rule_label(route.get("repo"), 12), "title_dim")
        x += 12 + GAP
    age = str(route.get("age") or "")
    title_width = max(16, min(48, (x1 - x) // 3))
    title_width = min(title_width, max(1, x1 - _seg_width(age) - 1 - x))     # the title ends before AGE starts
    grid.put(x, y, _clip(str(route.get("title") or "·"), title_width), "soft")
    x += title_width + GAP
    _draw_route_progress(grid, x, x1 - AGE_W - GAP, y, route)
    grid.put(x1 - _seg_width(age), y, age, "dim")


def _draw_route_progress(grid, x, end, y, route):
    if route.get("capability") == "frame" and route.get("shape") == "framed":
        cursor = grid.put(x, y, "frame", "title_dim", limit=end)
        cursor = grid.put(cursor, y, " ✓", "done", limit=end)
        stages = tuple(route.get("stages") or ())
        if end - cursor > 55 and stages:
            stage_name = str(stages[-1].get("name") or "·")
            cursor = grid.put(cursor, y, "  (" + stage_name + " ✓)", "dim", limit=end)
        grid.put(cursor, y, "  ∥ frame 2-way", "dim", limit=end)
        return
    legs = tuple(route.get("legs") or ())
    stages = tuple(route.get("stages") or ())
    if not legs or x >= end:
        return
    current = next((i for i, leg in enumerate(legs) if leg[1] != "done"), len(legs) - 1)
    name, status, _progress = legs[current]
    same = len(stages) == 1 and stages[0].get("name") == name
    kind = route.get("tree_label")
    duplicate = bool(not same and kind and name == kind)
    items = _leg_items(legs[:current + 1])
    if not same:
        items[-1] = ([(str(name), "soft")], status)
    lead = items[:-1] if duplicate else items
    after = []
    if current < len(legs) - 1:
        after.append((f" › +{len(legs) - current - 1}", "dim"))
    if route.get("resume"):
        after.extend([("  resume ", "dim"), (str(route["resume"]), "g_idle")])
    elif route.get("loops"):
        after.append(("  " + str(route["loops"]), "dim"))
    stage_index = next((i for i, stage in enumerate(stages) if stage.get("st") != "done"), len(stages) - 1)
    current_stage = str(stages[stage_index].get("name", "·")) if stages else str(name)
    after_width = sum(_seg_width(text) for text, _key in after)
    need = (0 if same else _seg_width(current_stage) + 18) + after_width
    rail = _fold_items(lead, max(8, end - x - need))
    cursor = _put_segments(grid, x, y, rail, end)
    room = end - cursor - 3 - after_width
    if duplicate and room < _seg_width(current_stage) + 2:
        leg = _leg_items([legs[current]])[0][0]
        cursor = _put_segments(grid, cursor, y, ([((" › "), "dim")] if cursor > x else []) + leg, end)
    elif not same and room >= _seg_width(current_stage) + 2 and stages:
        stage_rail = _fold_items(_stage_items(stages), room)
        if sum(_seg_width(text) for text, _key in stage_rail) > room:
            stage_rail = _minimal_stage_rail(stages, stage_index, room)
        opening = " › (" if duplicate and cursor > x else " (" if cursor > x else "("
        cursor = grid.put(cursor, y, opening, "dim", limit=end)
        cursor = _put_segments(grid, cursor, y, stage_rail, end)
        cursor = grid.put(cursor, y, ")", "dim", limit=end)
    _put_segments(grid, cursor, y, after, end)


def _seg_width(text):
    from .grid import cell_width
    return sum(cell_width(char) for char in str(text))


def _put_segments(grid, x, y, segments, end):
    for text, key in segments:
        x = grid.put(x, y, text, key, limit=end)
    return x


def _leg_items(legs):
    result = []
    for name, status, _progress in legs:
        parts = [(str(name), "title_dim")]
        mark = {"done": (" ✓", "done"), "failed": (" ✕", "g_dead")}.get(status)
        if mark:
            parts.append(mark)
        result.append((parts, status))
    return result


def _stage_hue(route, step):
    """The stage hue of a worker step: its index in the route's stages (five hues), green when it is not one of them."""
    names = [stage.get("n") or stage.get("name") for stage in route.get("stages") or ()]
    return "lcgym"[names.index(step) % 5] if step in names else "g"


def _stage_items(stages):
    bright_hues = ("hb_opencode", "hb_claude", "grp_live", "g_idle", "hb_codex")
    dim_hues = ("version_release", "h_claude", "g_work_off", "lvl_y_dim", "h_codex")
    result = []
    for index, stage in enumerate(stages):
        status = stage.get("st")
        if status == "active":
            name_key = {0: "hb_opencode", 1: "hb_claude", 2: "g_work", 3: "stg3_on", 4: "eff_xhigh"}[index % len(bright_hues)]
        elif status == "failed":
            name_key = bright_hues[index % len(bright_hues)]
        else:
            name_key = dim_hues[index % len(dim_hues)]
        parts = [(str(stage.get("name", "·")), name_key)]
        if int(stage.get("r") or 1) > 1:
            live = status in ("active", "failed")
            parts.append((f" R{stage['r']}", "g_idle" if live and stage.get("r", 1) >= 3 else "dim"))
        mark = {"done": (" ✓", "done"), "failed": (" ✕", "g_dead"),
                "active": (" ●", "work")}.get(status)
        if mark:
            parts.append(mark)
        if stage.get("host"):
            parts.append((" " + str(stage["host"]), "g_work_off"))
        result.append((parts, "done" if status == "done" else "todo" if status == "todo" else status))
    return result


def _fold_items(items, room):
    def build(low, high):
        out = [(f"+{low}✓ ", "dim")] if low else []
        for index, (parts, _status) in enumerate(items[low:len(items) - high]):
            if index:
                out.append((" › ", "dim"))
            out.extend(parts)
        if high:
            out.append((f" › +{high}", "dim"))
        return out
    low = high = 0
    while sum(_seg_width(text) for text, _key in build(low, high)) > room:
        if low < len(items) - high - 1 and items[low][1] == "done":
            low += 1
        elif high < len(items) - low - 1 and items[len(items) - 1 - high][1] == "todo":
            high += 1
        else:
            break
    return build(low, high)


def _minimal_stage_rail(stages, index, room):
    items = _stage_items(stages)
    current = items[index][0]
    rail = ([ ("… ", "dim") ] if index else []) + current + ([ (" …", "dim") ] if index < len(items) - 1 else [])
    if sum(_seg_width(text) for text, _key in rail) <= room:
        return rail
    if sum(_seg_width(text) for text, _key in current) <= room:
        return current
    marks = [segment for segment in current if segment[0] in (" ●", " ✕", " ✓")]
    return current[:1] + marks


def _overview_columns(use):
    narrow = use < 100
    harn_w, ctx_w = (13, 4) if narrow else (HARN_W, CTX_W)
    h = IND + 3 + 6 + GAP
    title = h + harn_w + GAP
    flex = use - title - ctx_w - AGE_W - 3 * GAP
    title_w = min(48, max(12 if narrow else 24, int(flex * 0.45)))
    now = use - (title + title_w + GAP + ctx_w + GAP) - AGE_W - GAP >= 16
    if not now:
        title_w = use - title - ctx_w - AGE_W - 2 * GAP
    if title_w < 6:
        # No honest room for a title: the column is left out and CTX follows the harness.
        title_w = 0
        ctx = title
    else:
        ctx = title + title_w + GAP
    return {"h": h, "title": title, "title_w": title_w, "ctx": ctx,
            "narrow": narrow, "now": ctx + ctx_w + GAP if now else None}


def _clip(text, width):
    from .grid import clip_cells
    return clip_cells(text, int(width))


def _tint_row(grid, y, x0, x1, tint):
    if 0 <= y < grid.rows:
        for xx in range(max(0, x0), min(grid.cols, x1)):
            glyph, key, _old = grid.cells[y][xx]
            grid.cells[y][xx] = (glyph, key, tint)


_VIEW_NAMES = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS",
               "SYSTEM")


def draw_routes(grid, vm, state, x=34, y=3):
    routes = tuple(vm.routes or ())
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 5
    cols = _route_columns(use)
    slim = cols["slim"]
    grid.fill(x, right, y, " ", tint=234)
    headers = [(cols["st"], "ID"), (cols["title"], "ROUTE"), (cols["kind"], "KIND"),
               (cols["own"], "HARNESS")]
    if not slim:
        headers.append((cols["ctx"], "CTX"))
    headers += [(cols["prog"], "PROGRESS"), (use - 2, "AGE")]
    for col, name in headers:
        # a label that would run into the AGE label is left out, never overlapped
        edge = right if name == "AGE" else x + use - 2 - 1
        grid.put(x + col, y, name, "head", tint=234, limit=edge)
    y += 1
    repos = [r.get("id", r.get("name", "")) for r in vm.repos]
    order = repos + sorted({str(route.get("repo") or "") for route in routes} - set(repos))
    opened = state.opened_for("ropen:" + state.view)
    for repo in order:
        mine = [route for route in routes if str(route.get("repo") or "") == str(repo)]
        if not mine:
            continue
        if not getattr(state, "no_repo_group", False) and repo_label(repo):
            if str(getattr(state, "view", "")) not in _VIEW_NAMES:
                grid.add_hit(y, x, right, ("scope", "repo:" + str(repo)))
            grid.put(x + 1, y, "┈ ", "line")
            end = grid.put(x + 3, y, str(repo) + "/", "title_dim")
            for xx in range(end + 1, right):
                grid.put(xx, y, "┈", "line")
            y += 1
        for route in mine:
            ident = str(route.get("id") or "·")
            is_open = opened == ident
            action = (("route", ident) if getattr(state, "no_repo_group", False) else
                      ("fold", "ropen:" + state.view, ident))
            # A page's route list also links an unfolded route to its own page; that hit comes first so it wins.
            link = (is_open and not getattr(state, "no_repo_group", False) and
                    str(getattr(state, "view", "")) not in _VIEW_NAMES)
            if link:
                grid.add_hit(y + 1, x + use - 7, right, ("route", ident))
            grid.add_hit(y, x, right, action)
            grid.add_hit(y + 1, x, right, action)
            if is_open:
                grid.fill(x - 1, right, y, " ", tint=236)
                grid.fill(x - 1, right, y + 1, " ", tint=236)
                grid.put(x, y, "▶", "soft")
            status = route.get("state") if route.get("activity") != "running" else "running"
            marker, key = {"running": ("⠏", "g_spin"), "working": ("⠏", "g_spin"),
                           "blocked": ("●", "g_blocked"), "dead": ("✕", "g_dead"),
                           "done": ("✓", "done"), "idle": ("●", "g_work_off")}.get(
                               status, ("●", "g_work_off"))
            grid.put(x + cols["st"], y, marker, key)
            tag = str(route.get("session_tag") or route.get("past_from") or "")
            if tag:
                grid.put(x + cols["st"] + 2, y, "[", "dim")
                grid.put(x + cols["st"] + 3, y, tag, "soft" if route.get("session_tag") else "dim")
                grid.put(x + cols["st"] + 3 + len(tag), y, "]", "dim")
            title = _clip(str(route.get("title") or "·"), cols["title_w"])
            grid.put(x + cols["title"], y, title, "soft", limit=x + cols["kind"] - GAP)
            kind_parts = [(str(route.get("capability") or "·"), "soft")]
            short = {"standard": "std", "light": "lt", "quick": "q", "thorough": "thr",
                     "adversarial": "adv"}
            suffix = "·".join(short.get(value, value) for value in
                              (route.get("mode"), route.get("intensity")) if value)
            if suffix:
                kind_parts.append(("(" + suffix + ")", "dim"))
            cursor = x + cols["kind"]
            age_edge = x + use - len(str(route.get("age") or "")) - 1       # nothing runs into AGE
            for text, style in kind_parts:
                cursor = grid.put(cursor, y, text, style, limit=min(x + cols["own"] - GAP, age_edge))
            _draw_owner(grid, x + cols["own"], y, route, cols["narrow"], slim,
                        min(x + (cols["ctx"] if not slim else cols["prog"]) - GAP, age_edge))
            if not slim:
                context = route.get("owner_context")
                if context:
                    tokens, window = context
                    pct = round(100 * tokens / max(1, window))
                    gauge_width = max(1, round(8 * window / 1_000_000))
                    filled = gauge_cells(gauge_width, pct)
                    grid.put(x + cols["ctx"], y, "━" * filled, "g_blocked" if pct >= 80 else "g_idle" if pct >= 50 else "done")
                    grid.put(x + cols["ctx"] + filled, y, "─" * (gauge_width - filled), "dim")
                    percent_key = "g_blocked" if pct >= 80 else "g_idle" if pct >= 50 else "dim"
                    grid.put(x + cols["ctx"] + 9, y, f"{pct:3d}%", percent_key)
                else:
                    grid.put(x + cols["ctx"] + 9, y, "   ·", "dim")
            _draw_route_progress(grid, x + cols["prog"], x + use - AGE_W - GAP, y, route)
            age = str(route.get("age") or "")
            grid.put(x + use - len(age), y, age, "dim")
            if is_open:
                _tint_row(grid, y, x - 1, right, 236)
            y += 1
            worker = route.get("worker") or ()
            label = str(worker[0]) if worker else "worker"
            unit = route.get("worker_unit")
            route_end = x + cols["kind"] - GAP
            route_start = x + cols["title"]
            cursor = grid.put(route_start, y, "└ ", "dim", limit=route_end)
            cursor = grid.put(cursor, y, _clip(label, max(0, route_end - cursor)),
                              "title_dim", limit=route_end)
            if unit:
                grid.put(cursor, y, _clip(" · " + str(unit), route_end - cursor), "dim")
            worker_running = route.get("activity") == "running" and any(
                "running" in str(text) for text, _style in (worker[2] if len(worker) > 2 else ()))
            worker_stage = str(route.get("worker_stage") or "·")
            hue = _stage_hue(route, worker_stage)
            stage_key = (route.get("worker_step_key") if worker_running else
                         route.get("worker_stage_key")) or ((hue, 0, 0) if worker_running else (hue, 0, 1))
            if not worker_running and isinstance(stage_key, (tuple, list)) and len(stage_key) == 3:
                stage_key = (stage_key[0], 0, 1)
            stage_room = max(0, cols["own"] - GAP - cols["kind"])
            grid.put(x + cols["kind"], y, _clip(worker_stage, stage_room), stage_key,
                     limit=x + cols["own"] - GAP)
            cursor = x + cols["own"]
            segments = tuple(worker[1] if len(worker) > 1 else ())
            if cols["narrow"] and label != "resource" and len(segments) > 1 and segments[1][0] == " (":
                harness = next((str(text) for text, style in segments
                                if isinstance(style, str) and style.startswith("hb_")), "·")
                effort = next((str(text) for text, _style in segments
                               if str(text) in ("low", "medium", "high", "xhigh", "max")), "")
                effort_key = next((style for text, style in segments
                                   if str(text) == effort), "dim")
                segments = ((harness, "hb_" + harness),) + (("·" + effort, effort_key),) if effort else ((harness, "hb_" + harness),)
            for text, style in segments:
                if isinstance(style, str) and style.startswith("hb_"):
                    style = {"hb_claude": "h_claude", "hb_codex": "h_codex",
                             "hb_opencode": "version_release"}.get(style, style)
                elif style == "fam_gpt":
                    style = "h_codex"
                elif style == "eff_medium":
                    style = "branch_s"
                grid.put(cursor, y, text, style, limit=x + cols["prog"] - GAP)
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            cursor = x + cols["prog"]
            for text, style in (worker[2] if len(worker) > 2 else ()):
                if "running" in str(text):
                    style = route.get("worker_stage_key") or (hue, 1, 0)
                grid.put(cursor, y, text, style, limit=x + use - (9 if link else 0))
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            if link:
                grid.put(x + use - 6, y, "open ▸", "accent")
            if is_open:
                _tint_row(grid, y - 1, x - 1, right, 236)
                _tint_row(grid, y, x - 1, right, 236)
            y += 1
            if is_open and y < bottom:
                from .pages import draw_route_detail
                if not getattr(state, "no_repo_group", False):
                    height = max(16, min(30, grid.rows - 6 - y))
                    draw_route_detail(grid, vm, route, x, use, y, state, bottom=y + height)
                    y += height + 1
    return y



def draw_questions(grid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 5
    questions = tuple(vm.questions or ())
    grid.put(x, y, "── ", "line")
    cursor = grid.put(x + 3, y, "WAITING", "sect")
    cursor = grid.put(cursor, y, f"  {len(questions)} question" + ("s" if len(questions) != 1 else ""), "dim")
    grid.put(cursor, y, " ", "branch_s")
    grid.put(cursor + 1, y, "─" * max(0, right - cursor - 1), "line", limit=right)
    y += 1
    narrow = use < 100
    kind_x = 10 + (13 if narrow else 25) + 2
    wait_x = kind_x + 11
    project_x = None if narrow else wait_x + 10
    session_x = wait_x + 10 + (0 if narrow else 14)
    columns = [(3, "ID"), (10, "HARNESS"), (kind_x, "KIND"), (wait_x, "WAITING")]
    if project_x is not None:
        columns.append((project_x, "PROJECT"))
    columns.append((session_x, "SESSION"))
    grid.fill(x, right, y, " ", tint=234)
    for off, name in columns:
        grid.put(x + off, y, name, "head", tint=234, limit=right)
    y += 1
    closed = state.opened_for("qshut:" + state.view, "")
    for question in questions:
        if y >= bottom:
            grid.put(right - 18, bottom, "↓ more below", "dim", limit=right)
            return
        tag = str(question.get("tag") or "·")
        is_open = closed != tag
        grid.add_hit(y, x, right, ("fold", "qshut:" + state.view, tag))
        if is_open:
            grid.fill(x - 1, right, y, " ", tint=236)
            grid.put(x, y, "▶", "soft")
        grid.put(x + 1, y, "●", "blocked")
        grid.put(x + 3, y, "[", "dim")
        grid.put(x + 4, y, tag, "soft")
        grid.put(x + 4 + len(tag), y, "]", "dim")
        harness = str(question.get("h") or "·")
        grid.put(x + 10, y, harness, "hb_" + harness)
        if narrow:
            effort = str(question.get("e") or "")
            grid.put(x + 10 + len(harness), y, "·" + effort, "eff_" + effort)
        else:
            model = question.get("m") or ("·", "dim")
            cursor = grid.put(x + 10 + len(harness), y, " (", "dim")
            model_key = "g_idle" if harness == "codex" and len(model) > 1 and model[1] == "fam_gpt" else (model[1] if len(model) > 1 else "dim")
            cursor = grid.put(cursor, y, str(model[0]), model_key)
            cursor = grid.put(cursor, y, "·", "dim")
            effort = str(question.get("e") or "")
            cursor = grid.put(cursor, y, effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim")
            grid.put(cursor, y, ")", "dim")
        grid.put(x + kind_x, y, str(question.get("kind") or "·"), "blocked")
        grid.put(x + wait_x, y, str(question.get("wait") or "·"), "soft")
        if project_x is not None:
            grid.put(x + project_x, y, str(question.get("project") or "·"), "soft")
        grid.put(x + session_x, y, _clip(str(question.get("title") or "·"), use - session_x), "hb_" + harness)
        if is_open:
            _tint_row(grid, y, x - 1, right, 236)
        y += 1
        if not is_open:
            continue
        for label, value, style in (("title:", question.get("title"), "hb_" + harness),
                                    *(([("project:", question.get("project"), "soft")] if narrow else [])),
                                    ("now:", question.get("now"), "soft")):
            grid.put(x + 10, y, label, "label")
            for line in _wrap(str(value or "·"), max(1, use - 20)):
                grid.put(x + 20, y, line, style, limit=right)
                y += 1
        pane = str(question.get("pane") or "·")
        grid.put(x + 10, y, "pane:", "label")
        pane_start = x + 20
        grid.put(pane_start, y, pane, "soft", limit=right)
        pane_hit_start = x + 20 + len(pane) + 2
        grid.add_hit(y, pane_hit_start, pane_hit_start + len("(open ▸)"), ("pane", tag, pane))
        cursor = grid.put(pane_start + len(pane), y, "  ", "branch_s", limit=x + use)
        grid.put(cursor, y, "(open ▸)", "hearting", limit=x + use)
        y += 1
        grid.put(x + 10, y, "answer:", "label")
        grid.put(x + 20, y, "⏎", "hearting")
        grid.put(x + 21, y, " go to its pane and answer there", "dim", limit=x + use)
        y += 1
    y += 1
    answered = tuple(vm.answered or ())
    grid.put(x, y, "── ", "line")
    cursor = grid.put(x + 3, y, "ANSWERED TODAY", "sect")
    answered_count = len(answered[0][1]) if answered else 0     # the first repo's own rows, as its name before it
    first_repo = repo_label(answered[0][0]) if answered else "·"
    total = (vm.system or {}).get("answered_total") if hasattr(vm.system, "get") else None
    total_text = str(total) if total is not None else str(sum(len(rows) for _, rows in answered))
    cursor = grid.put(cursor, y, f"  {first_repo + ' ' if first_repo else ''}{answered_count} · {total_text} decision records in all projects", "dim", limit=grid.cols)
    grid.put(cursor, y, " ", "branch_s")
    for xx in range(cursor + 1, right):
        grid.put(xx, y, "─", "line")
    y += 1
    grid.fill(x, right, y, " ", tint=234)
    for off, label in ((3, "TIME"), (10, "PROJECT"), (24, "QUESTION")):
        grid.put(x + off, y, label, "head", tint=234, limit=right)
    y += 1
    for repo, records in answered:
        for index, record in enumerate(records):
            if y >= bottom:
                return
            at, text = record[:2]
            ident = f"{repo}:{index}"
            lines = _wrap_cells(str(text), max(1, use - 24))
            is_open = state.opened_for("aopen:" + state.view) == ident and len(lines) > 1
            if len(lines) > 1:
                grid.add_hit(y, x, right, ("fold", "aopen:" + state.view, ident))
            if is_open:
                grid.put(x, y, "▶", "soft")
            grid.put(x + 1, y, "✓", "done")
            grid.put(x + 3, y, str(at), "dim")
            if index == 0:
                grid.put(x + 10, y, repo_label(repo), "soft")
            first_row = y
            visible = lines if is_open else [(_clip(str(text), max(0, use - 24)))]
            for line in visible:
                grid.put(x + 24, y, line, "title_dim", limit=right)
                y += 1
            if is_open:
                _tint_row(grid, first_row, x - 1, right, 236)


def _wrap(text, width):
    """Wrap by screen cells, so a wide glyph never straddles the right edge."""
    from .grid import cell_width
    text, width = str(text), max(1, int(width))
    rows, current, used = [], "", 0
    for char in text:
        w = cell_width(char)
        if used + w > width:
            rows.append(current)
            current, used = "", 0
        current += char
        used += w
    return rows + [current] if current or not rows else rows


def _route_columns(use):
    narrow, slim = use < 150, use < 120
    title, kind = 12, 15
    own_w, ctx_w = (12 if slim else 13 if narrow else 26), (0 if slim else 13)
    flex = use - title - kind - own_w - ctx_w - AGE_W - (4 if slim else 5) * GAP
    title_w = min(48, max(12, int(flex * .5)))
    kind_x = title + title_w + GAP
    own_x = kind_x + kind + GAP
    ctx_x = own_x + own_w + GAP
    prog_x = ctx_x + (ctx_w + GAP if ctx_w else 0)
    return {"narrow": narrow, "slim": slim, "st": 3, "title": title,
            "title_w": title_w, "kind": kind_x, "own": own_x, "own_w": own_w,
            "ctx": ctx_x, "prog": prog_x}


def _draw_owner(grid, x, y, route, narrow, slim, limit):
    harness = str(route.get("harness") or "·")
    model = route.get("model")
    effort = str(route.get("effort") or "")
    if narrow:
        parts = [(harness, "hb_" + harness)]
        if effort:
            parts.append(("·" + effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim"))
    else:
        model_name, model_key = (tuple(model) + ("dim",))[:2] if model else ("·", "dim")
        parts = [(harness, "hb_" + harness), (" (", "dim"), (str(model_name), "hb_" + harness),
                 ("·", "dim"), (effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim"),
                 (")", "dim")]
    profile = route.get("profile")
    if profile and profile != "deep" and not slim:
        parts.append((" " + str(profile), "dim"))
    cursor = x
    for text, style in parts:
        cursor = grid.put(cursor, y, text, style, limit=limit)
