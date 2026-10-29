"""Overview and list-view tables for the immutable explorer projection."""
from collections.abc import Mapping
from .grid import CellGrid
from .style import recede_key

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


def _draw_context_gauge(grid, x, y, pct, window):
    """Draw the resolved session/attempt gauge in the shared 13-cell column."""
    if pct is None:
        grid.put(x + 9, y, "   ·", "dim")
        return
    pct = int(pct)
    width = min(8, max(1, round(8 * float(window or 1))))
    filled = gauge_cells(width, pct)
    key = "g_blocked" if pct >= 80 else "g_idle" if pct >= 50 else "done"
    grid.put(x, y, "━" * filled, key)
    grid.put(x + filled, y, "─" * (width - filled), "dim")
    grid.put(x + 9, y, f"{pct:3d}%", key if pct >= 50 else "dim")


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
    route_rows = [route for route in vm.routes if route.get("session_tag")]
    current_routes = {}
    for route in route_rows:
        if route.get("current") is True:
            tag = route["session_tag"]
            current_routes[tag] = current_routes.get(tag, 0) + 1
    # The ROUTES summary and RECENT stay in view under a long session list: the page
    # viewport pins them to its bottom edge and only the sessions above them scroll.
    # A short screen keeps fewer RECENT rows (at least one) before it gives the pin up.
    room = getattr(grid, "viewport_rows", 0) - OVERVIEW_SCROLL_ROWS
    recent = sum(1 for repo in repos if repo.get("done") and repo_label(repo.get("name")))
    recent_rows = min(recent, max(min(1, recent), room - 4 - len(route_rows)))
    tail_height = 4 + len(route_rows) + recent_rows
    pin = getattr(grid, "pin_bottom", None)
    sticky = pin is not None and getattr(state, "scope", "-") == "-" and tail_height <= room
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
            if session.get("answer_wait_marker") == "●":
                marker, marker_key = "●", "blocked"
            elif session.get("answer_wait_marker") == "◑":
                marker, marker_key = "◑", "lvl_y"
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
                grid.put(x + c["h"] + len(harness) + 2, y, model_name, model_key)
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
                route_count = current_routes.get(tag, 0)
                route_tail = f" route {route_count}" if route_count and state.opened != tag else ""
                # Current work remains visible while the session is folded.
                # Reserve its cells before a long CJK title or branch is drawn.
                if route_tail:
                    tail = route_tail
                room = max(0, c["title_w"] - _seg_width(tail))
                title_text = _clip(title, room)
                bold_key = {"claude": ("c", 1, 0), "codex": ("m", 1, 0),
                            "opencode": ("l", 1, 0)}.get(harness, "unknown")
                title_end = grid.put(x + c["title"], y, title_text, bold_key,
                                     limit=x + c["title"] + room)
                if tail:
                    grid.put(title_end, y, tail, "dim", limit=x + c["title"] + c["title_w"])
            context = session.get("context")
            if not c["narrow"]:
                _draw_context_gauge(grid, x + c["ctx"], y, context, session.get("window"))
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
                if sticky:
                    pin(None, tail_height, lambda target, top: _draw_overview_tail(
                        target, vm, state, x, use, right, top, route_rows, repos, recent_rows=recent_rows))
                    grid.mark_page_incomplete()
                    return
                grid.put(right - 18, grid.rows - 5, "↓ more below", "dim", limit=right)
                return
    sessions_total = len(sessions)
    working_sessions = sum(1 for item in sessions if item.get("state") == "working")
    waits = (vm.system or {}).get("answer_waits") or {}
    questions = waits.get("total") if waits.get("state") == "complete" else None
    if not waits:
        questions = sum(1 for item in (vm.questions or ()) if not item.get("older"))
    from .chrome import count_word
    summary = (f"{count_word(sessions_total, 'session')} · {working_sessions} working · "
               f"{questions if questions is not None else '?'} needs answer · {count_word(sum(1 for repo in repos if repo_label(repo.get('name', '·'))), 'project')}")
    grid.put(x + 1, y, summary, "dim")                # the summary runs to the screen edge and is cut there
    if sticky:
        pin(y + 1, tail_height, lambda target, top: _draw_overview_tail(
            target, vm, state, x, use, right, top, route_rows, repos, recent_rows=recent_rows))
        grid.report_natural_page_end(y + tail_height)
        return
    y += 2
    _draw_overview_routes_rule(grid, vm, state, x, use, right, y, route_rows)
    scoped_page = getattr(state, "scope", "-") != "-"
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
    _draw_overview_tail(grid, vm, state, x, use, right, y - 2, route_rows, repos, heading=False,
                        limit=grid.rows - 5)


OVERVIEW_SCROLL_ROWS = 4        # session rows the pinned tail always leaves above itself


def _draw_overview_tail(grid, vm, state, x, use, right, y, route_rows, repos, heading=True, limit=None,
                        recent_rows=None):
    """The OVERVIEW ROUTES summary and RECENT block, from the blank gap row ``y`` down.

    It is four rows (gap, two rules, the blank between them) plus one per route and
    per shown RECENT repo; the pinned copy shows ``recent_rows`` of them.
    """
    y += 1
    if heading:
        _draw_overview_routes_rule(grid, vm, state, x, use, right, y, route_rows)
    y += 1
    scoped_page = getattr(state, "scope", "-") != "-"
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
    shown = 0
    for repo in repos:
        if not repo.get("done") or not repo_label(repo.get("name")) or (limit is not None and y >= limit):
            continue
        if recent_rows is not None and shown >= recent_rows:
            break
        _recent_row(grid, repo, x, use, y)
        shown += 1
        y += 1


def _draw_overview_routes_rule(grid, vm, state, x, use, right, y, route_rows):
    open_routes = sum(1 for route in route_rows if route.get("state") != "done")
    stopped = sum(1 for route in route_rows if route.get("state") == "dead")
    grid.put(x, y, "── ", "line")
    label_x = grid.put(x + 3, y, "ROUTES", "head")
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
    system = vm.system if hasattr(vm.system, "get") else {}
    task = system.get("task_summary") if hasattr(system, "get") else None
    if (hasattr(task, "get") and task.get("session_tag") == tag
            and task.get("state") not in (None, "unknown", "failed")):
        for name, key in (("TODO", "lvl_y"), ("DOING", "g_spin_dim"), ("DONE", "done")):
            values = tuple(task.get(name.casefold()) or ())
            grid.put(x_lab, y, name, key)
            grid.put(x_val, y, ", ".join(values[:3]) or "·", "soft" if values else "dim",
                     limit=right)
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
    question = next((item for item in (vm.questions or ())
                     if item.get("session_tag") == tag), None)
    if question:
        marker = str(question.get("marker") or "●")
        lead = [(marker, "lvl_y" if marker == "◑" else "blocked"),
                (" " + str(question.get("source") or question.get("kind") or "wait"), "blocked"),
                (" · waiting " + str(question.get("wait")) + " · ", "dim")]
        from .pages import _segs
        cursor = _segs(grid, x_val, y, lead, right)
        pane = question.get("pane")
        if question.get("open_tag") and pane:
            cursor = grid.put(cursor, y, "  ", "branch_s", limit=right)
            open_x = grid.put(cursor, y, "(open ▸)", "hearting", limit=right)
            if open_x <= right:
                grid.add_hit(y, cursor, open_x, ("pane", tag, str(pane)))
    else:
        grid.put(x_val, y, "·", "dim")
    _session_open_link(grid, x_lab, x_val, right, y + 1, tag)
    return y + 2


SESSION_OPEN_LABEL = "PAGE"
SESSION_OPEN_LINK = "(open ▸)"


def _session_open_link(grid, x_lab, x_val, right, y, tag):
    """The unfolded row's way to its session page, whose HISTORY lists the seat's generations.

    It takes the spacer row under ASK. Only a wholly drawn ``(open ▸)`` is a target: a
    ``("page", "session", tag)`` hit, which the reducer opens on press (no tree drag).
    A spacer at or past the page's overflow probe row (``rows - 5``) stays blank, as before.
    """
    if y >= grid.rows - 5:
        return
    grid.put(x_lab, y, SESSION_OPEN_LABEL, "label", limit=right)
    end = grid.put(x_val, y, SESSION_OPEN_LINK, "hearting", limit=right)
    if end == x_val + _seg_width(SESSION_OPEN_LINK):
        grid.add_hit(y, x_val, end, ("page", "session", tag))


def _draw_overview_route(grid, x, use, y, route, steward=False):
    _draw_route_line(grid, x + IND + 1, x + use, y, route, steward=steward)
    if route_is_history(route):
        # Only the OVERVIEW summary row recedes here. `_draw_route_line` is also the
        # session-unfold route line, which another change owns; leave that path alone.
        _recede_row(grid, y, x + IND + 1, x + use)


def _draw_route_line(grid, x0, x1, y, route, tag=True, repo=True, steward=False):
    age = _clip(str(route.get("age") or ""), AGE_W + 1)
    tag_text = str(route.get("session_tag") or "··") if tag else ""
    tag_width = min(_seg_width(tag_text), 8) if tag else 0
    if tag:
        tag_text = _clip(tag_text, tag_width)
    tag_room = tag_width + 2 + GAP if tag else 0
    room = x1 - x0 - 2 - tag_room - 16 - GAP - 24 - GAP - AGE_W
    own = room >= 8 + GAP + (12 + GAP if repo else 0)
    repo = repo and room >= 12 + GAP
    state = "working" if route.get("activity") == "running" else route.get("state")
    marker, marker_key = {"working": ("⠏", "g_spin"), "dead": ("✕", "g_dead"),
                          "blocked": ("●", "g_blocked"), "done": ("✓", "grp_live")}.get(
                              state, ("●", "g_work_off"))
    grid.put(x0, y, marker, marker_key)
    x = x0 + 2
    if tag:
        grid.put(x, y, "[", "dim")
        grid.put(x + 1, y, tag_text, "steward" if steward else "soft", limit=x + 1 + tag_width)
        grid.put(x + 1 + tag_width, y, "]", "dim")
        x += tag_width + 2 + GAP
    if own:
        owner = str(route.get("harness") or "·")
        grid.put(x, y, owner, "hb_" + owner)
        x += 8 + GAP
    if repo:
        grid.put(x, y, repo_rule_label(route.get("repo"), 12), "title_dim")
        x += 12 + GAP
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
    if same and stages[0].get("st") == "active":
        parts, _ = _stage_items(stages, lit=bool(getattr(grid, "beat", 1)))[0]
        items[-1] = (parts[:1] + items[-1][0][1:], status)
    elif not same:
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
        stage_rail = _fold_items(_stage_items(stages, lit=bool(getattr(grid, "beat", 1))), room)
        if sum(_seg_width(text) for text, _key in stage_rail) > room:
            stage_rail = _minimal_stage_rail(stages, stage_index, room, lit=bool(getattr(grid, "beat", 1)))
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


def _stage_items(stages, lit=True):
    bright_hues = ("hb_opencode", "hb_claude", "grp_live", "g_idle", "hb_codex")
    dim_hues = ("version_release", "h_claude", "g_work_off", "lvl_y_dim", "h_codex")
    result = []
    for index, stage in enumerate(stages):
        status = stage.get("st")
        if status == "active" and lit:
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
                "active": (" ●", "work" if lit else "g_work_off")}.get(status)
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


def _minimal_stage_rail(stages, index, room, lit=True):
    items = _stage_items(stages, lit=lit)
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


def route_is_history(route):
    """A route that already ended: the sidebar tree's done/dead boundary on a list row.

    A dead owner whose worker still runs (`activity == "running"`) stays in the live group;
    a record without `activity` (stored payloads, PEERS owners) decides by state alone.
    """
    if str(route.get("state") or "") not in ("dead", "done"):
        return False
    return route.get("activity") != "running"


def _recede_row(grid, y, x0, x1):
    """Lower a finished row's ink: same glyphs, tints and hits — only the key moves."""
    if 0 <= y < grid.rows:
        for xx in range(max(0, x0), min(grid.cols, x1)):
            glyph, key, tint = grid.cells[y][xx]
            grid.cells[y][xx] = (glyph, recede_key(key), tint)


_VIEW_NAMES = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE", "MEMORY", "MODELS",
               "SYSTEM")


def draw_routes(grid, vm, state, x=34, y=3):
    routes = tuple(vm.routes or ())
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 5
    cols = _route_columns(use)
    slim = cols["slim"]
    gap = cols["gap"]
    grid.fill(x, right, y, " ", tint=234)
    headers = [(cols["st"], "ID"), (cols["title"], "ROUTE"), (cols["kind"], "KIND"),
               (cols["own"], "HARNESS")]
    if not slim:
        headers.append((cols["ctx"], "CTX"))
    headers += [(cols["prog"], "PROGRESS"), (use - 2, "AGE")]
    for col, name in headers:
        # a label that would run into the AGE label is left out, never overlapped
        edge = right if name == "AGE" else x + use - 2
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
            end = grid.put(x + 3, y, _clip(str(repo) + "/", min(24, max(1, use - 6))),
                           "title_dim", limit=x + use - 1)
            for xx in range(end + 1, right):
                grid.put(xx, y, "┈", "line")
            y += 1
        for route in mine:
            ident = str(route.get("id") or "·")
            is_open = opened == ident
            history = route_is_history(route)
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
                tag_width = max(0, cols["title"] - cols["st"] - 4)
                tag_text = _clip(tag, tag_width)
                tag_x = x + cols["st"] + 3
                grid.put(x + cols["st"] + 2, y, "[", "dim")
                grid.put(tag_x, y, tag_text, "soft" if route.get("session_tag") else "dim",
                         limit=tag_x + _seg_width(tag_text))
                grid.put(tag_x + _seg_width(tag_text), y, "]", "dim", limit=x + cols["title"])
            title = _clip(str(route.get("title") or "·"), cols["title_w"])
            grid.put(x + cols["title"], y, title, "soft", limit=x + cols["kind"] - gap)
            kind_parts = [(str(route.get("capability") or "·"), "soft")]
            short = {"standard": "std", "light": "lt", "quick": "q", "thorough": "thr",
                     "adversarial": "adv"}
            suffix = "·".join(short.get(value, value) for value in
                              (route.get("mode"), route.get("intensity")) if value)
            if suffix:
                kind_parts.append(("(" + suffix + ")", "dim"))
            cursor = x + cols["kind"]
            age = _clip(str(route.get("age") or ""), cols["age_w"])
            age_edge = x + use - _seg_width(age)                              # content stops where AGE starts
            for text, style in kind_parts:
                cursor = grid.put(cursor, y, text, style, limit=min(x + cols["own"] - gap, age_edge))
            owner_edge = (x + cols["own"] + cols["own_w"] if cols["tiny"] else
                          x + (cols["ctx"] if not slim else cols["prog"]) - gap)
            owner_limit = min(owner_edge, age_edge)
            _draw_owner(grid, x + cols["own"], y, route, cols["narrow"], slim, owner_limit)
            if not slim:
                if "owner_gauge" in route:
                    pct, window = route["owner_gauge"]
                elif route.get("owner_context"):
                    context = route["owner_context"]
                    tokens, window = context
                    pct = round(100 * tokens / max(1, window))
                    window /= 1_000_000
                else:
                    pct, window = None, None
                _draw_context_gauge(grid, x + cols["ctx"], y, pct, window)
            _draw_route_progress(grid, x + cols["prog"], age_edge - gap, y, route)
            grid.put(age_edge, y, age, "dim")
            if is_open:
                _tint_row(grid, y, x - 1, right, 236)
            y += 1
            worker = route.get("worker") or ()
            label = str(worker[0]) if worker else "worker"
            unit = route.get("worker_unit")
            route_end = x + cols["kind"] - gap
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
            stage_room = max(0, cols["own"] - gap - cols["kind"])
            grid.put(x + cols["kind"], y, _clip(worker_stage, stage_room), stage_key,
                     limit=x + cols["own"] - gap)
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
                elif style == "eff_medium":
                    style = "branch_s"
                grid.put(cursor, y, text, style, limit=owner_limit)
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            cursor = x + cols["prog"]
            for text, style in (worker[2] if len(worker) > 2 else ()):
                if "running" in str(text):
                    style = route.get("worker_stage_key") or (hue, 1, 0)
                    if worker_running and not getattr(grid, "beat", 1):
                        style = (hue, 0, 1)
                grid.put(cursor, y, text, style, limit=x + use - (9 if link else 0))
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            if link:
                grid.put(x + use - 6, y, "open ▸", "accent")
            if history and not is_open:
                # Finished history recedes; an opened row is being read and keeps its ink.
                _recede_row(grid, y - 1, x, right)
                _recede_row(grid, y, x, right)
            if is_open:
                _tint_row(grid, y - 1, x - 1, right, 236)
                _tint_row(grid, y, x - 1, right, 236)
            y += 1
            for index, worker_context in enumerate(route.get("worker_contexts") or ()):
                grid.add_hit(y, x, right, action)
                label = str(worker_context.get("stage") or "worker")
                grid.put(route_start, y, _clip(f"└ {label} · {index + 1}", route_end - route_start),
                         "title_dim", limit=route_end)
                harness = str(worker_context.get("harness") or "·")
                grid.put(x + cols["own"], y, harness, "hb_" + harness, limit=owner_limit)
                if not slim:
                    _draw_context_gauge(grid, x + cols["ctx"], y,
                                        worker_context.get("context"), worker_context.get("window"))
                else:
                    pct = worker_context.get("context")
                    grid.put(x + cols["prog"], y, "CTX ·" if pct is None else f"CTX {pct}%",
                             "dim", limit=age_edge)
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
    all_questions = tuple(vm.questions or ())
    current_questions = tuple(item for item in all_questions if not item.get("older"))
    older_questions = tuple(item for item in all_questions if item.get("older"))
    older_open = state.opened_for("qolder:QUESTIONS", "") == "open"
    questions = current_questions + (older_questions if older_open else ())
    system = vm.system if hasattr(vm.system, "get") else {}
    waits = system.get("answer_waits") if hasattr(system, "get") else None
    if not hasattr(waits, "get"):
        waits = {"state": "complete", "total": len(current_questions), "older": len(older_questions)}
    known = waits.get("state") == "complete"
    total = waits.get("total") if known else None
    older = waits.get("older") if known else None
    older_count = len(older_questions)
    grid.put(x, y, "── ", "line")
    cursor = grid.put(x + 3, y, "WAITING", "sect")
    cursor = grid.put(cursor, y, "  " + (str(total) if total is not None else "?") + " question" +
                      ("s" if total != 1 else ""), "dim")
    if isinstance(older, int) and older > 0:
        cursor = grid.put(cursor, y, f" · older waits {older}", "dim")
    elif not known:
        cursor = grid.put(cursor, y, " · observation " + str(waits.get("state")), "dim")
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
    selected = state.opened_for("qsel:QUESTIONS", "")
    overlay = getattr(state, "question_overlay", None)
    overlay = overlay if hasattr(overlay, "get") else {}
    older_heading_drawn = False
    for question in questions:
        if y >= bottom:
            grid.put(right - 18, bottom, "↓ more below", "dim", limit=right)
            return
        if question.get("older") and not older_heading_drawn:
            grid.put(x + 3, y, f"OLDER WAITS · {older_count}", "sect", limit=right)
            toggle = "[ hide ]"
            end = grid.put(right - len(toggle), y, toggle, "hearting", limit=right)
            grid.add_hit(y, right - len(toggle), end, ("question.older.toggle",))
            grid.add_hit(y, x, right, ("question.older.toggle",))
            y += 1
            older_heading_drawn = True
            if y >= bottom:
                return
        tag = str(question.get("tag") or "·")
        tag_text = _clip(tag, 5)
        tag_x = x + 4
        is_open = closed != tag
        current_ref = question.get("ref")
        can_select = (isinstance(current_ref, Mapping)
                      and all(current_ref.get(key) for key in ("id", "harness", "session_id")))
        grid.add_hit(y, x, x + 2 if can_select else right,
                     ("fold", "qshut:" + state.view, tag))
        if can_select:
            grid.add_hit(y, x + 2, right, ("question.select", tag, current_ref))
        if is_open:
            grid.fill(x - 1, right, y, " ", tint=236)
            grid.put(x, y, "▶", "soft")
        marker = str(question.get("marker") or "●")
        grid.put(x + 1, y, marker, "lvl_y" if marker == "◑" else "blocked")
        grid.put(x + 3, y, "[", "dim")
        grid.put(tag_x, y, tag_text, "soft", limit=tag_x + _seg_width(tag_text))
        grid.put(tag_x + _seg_width(tag_text), y, "]", "dim", limit=x + 10)
        harness = str(question.get("h") or "·")
        grid.put(x + 10, y, harness, "hb_" + harness)
        if narrow:
            effort = str(question.get("e") or "")
            grid.put(x + 10 + len(harness), y, "·" + effort, "eff_" + effort)
        else:
            model = question.get("m") or ("·", "dim")
            cursor = grid.put(x + 10 + len(harness), y, " (", "dim")
            model_key = model[1] if len(model) > 1 else "dim"
            cursor = grid.put(cursor, y, str(model[0]), model_key)
            cursor = grid.put(cursor, y, "·", "dim")
            effort = str(question.get("e") or "")
            cursor = grid.put(cursor, y, effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim")
            grid.put(cursor, y, ")", "dim")
        grid.put(x + kind_x, y, str(question.get("kind") or "·"), "blocked")
        grid.put(x + wait_x, y, str(question.get("wait") or "·"), "soft")
        if project_x is not None:
            grid.put(x + project_x, y, str(question.get("project") or "·"), "soft")
        row_label = question.get("session_tag") or question.get("title") or "·"
        grid.put(x + session_x, y, _clip(str(row_label),
                                         use - session_x), "hb_" + harness)
        if tag == selected:
            grid.put(x, y, "◆", "hearting")
        if is_open:
            _tint_row(grid, y, x - 1, right, 236)
        y += 1
        if not is_open:
            continue
        if "session_tag" not in question and question.get("title") is not None:
            detail_fields = (("title:", question.get("title"), "hb_" + harness),
                             *(([("project:", question.get("project"), "soft")] if narrow else [])),
                             ("now:", question.get("now"), "soft"))
        else:
            detail_fields = (("source:", question.get("source"), "soft"),
                             ("kind:", question.get("kind"), "blocked"),
                             *(([("project:", question.get("project"), "soft")] if narrow else [])))
        for label, value, style in detail_fields:
            grid.put(x + 10, y, label, "label")
            for line in _wrap(str(value or "·"), max(1, use - 20)):
                grid.put(x + 20, y, line, style, limit=right)
                y += 1
        pane = str(question.get("pane") or "·")
        grid.put(x + 10, y, "pane:", "label")
        pane_start = x + 20
        grid.put(pane_start, y, pane, "soft", limit=right)
        open_tag = str(question.get("open_tag") or "")
        legacy_fixture = "session_tag" not in question and question.get("title") is not None
        if legacy_fixture:
            pane_hit_start = pane_start + len(pane) + 2
            grid.add_hit(y, pane_hit_start, pane_hit_start + len("(open ▸)"),
                         ("pane", tag, pane))
            cursor = grid.put(pane_start + len(pane), y, "  ", "branch_s", limit=x + use)
            grid.put(cursor, y, "(open ▸)", "hearting", limit=x + use)
        elif open_tag and pane != "·":
            pane_hit_start = x + 20 + len(pane) + 2
            grid.add_hit(y, pane_hit_start, pane_hit_start + len("(open ▸)"),
                         ("pane", open_tag, pane))
            cursor = grid.put(pane_start + len(pane), y, "  ", "branch_s", limit=x + use)
            grid.put(cursor, y, "(open ▸)", "hearting", limit=x + use)
        else:
            grid.put(pane_start + len(pane), y, "  no current pane", "dim", limit=x + use)
        y += 1
        grid.put(x + 10, y, "answer:", "label")
        grid.put(x + 20, y, "⏎", "hearting")
        guide = (" select to view and answer · native fallback if unsupported" if can_select
                 else " go to its pane and answer there")
        grid.put(x + 21, y, guide, "dim", limit=x + use)
        y += 1
        current_ref = question.get("ref")
        overlay_ref = overlay.get("ref")
        same_request = (hasattr(current_ref, "get") and hasattr(overlay_ref, "get")
                        and overlay_ref.get("id") == question.get("id")
                        and all(current_ref.get(key) == overlay_ref.get(key)
                                for key in ("harness", "session_id", "request_id", "turn_id",
                                            "route_id", "question_id")))
        private = overlay if tag == selected and same_request else None
        if private:
            detail = private.get("detail")
            envelope = detail if hasattr(detail, "get") else {}
            from .state import question_items
            question_specs = question_items(envelope)
            kind = str(envelope.get("kind") or "").lower()
            approval = ("approval" in kind or "permission" in kind or
                        any(word in kind for word in ("commandexecution", "filechange")))
            native_ref = envelope.get("native_ref")
            opencode_v2_form = (envelope.get("harness") == "opencode"
                                and isinstance(native_ref, Mapping)
                                and native_ref.get("transport") == "opencode-http-v2")
            private_detail = envelope.get("detail") if envelope.get("state") != "unknown" else None
            content_x = x + (10 if narrow else 20)
            if (question_specs or opencode_v2_form) and not approval:
                detail_lines = []
            elif isinstance(private_detail, dict) or hasattr(private_detail, "items"):
                detail_lines = []
                for name, value in list(private_detail.items())[:12]:
                    if str(name).replace("_", "").casefold() in {
                            "availabledecisions", "threadid", "turnid", "itemid", "requestid",
                            "sessionid", "questionid", "isblocking", "autoresolutionms"}:
                        continue
                    label = str(name).replace("_", " ") + ": "
                    if isinstance(value, (str, int, float)):
                        detail_lines.append(label + str(value))
                    elif isinstance(value, (list, tuple)):
                        import json
                        detail_lines.append(label + json.dumps(value, ensure_ascii=False))
                    elif isinstance(value, Mapping):
                        import json
                        detail_lines.append(label + json.dumps(dict(value), ensure_ascii=False,
                                                               sort_keys=True))
            elif isinstance(private_detail, (str, int, float)):
                detail_lines = [str(private_detail)]
            else:
                detail_lines = []
            for line in detail_lines:
                for wrapped in _wrap(line, max(1, right - content_x)):
                    if y >= bottom:
                        break
                    if not narrow:
                        grid.put(x + 10, y, "detail:", "label")
                    grid.put(content_x, y, wrapped, "soft", limit=right)
                    y += 1
            if envelope.get("source") == "S2" and isinstance(private_detail, Mapping):
                target = private_detail.get("target")
                if isinstance(target, Mapping):
                    parts = ["gate: " + str(target.get("gate") or envelope.get("question_id") or "")]
                    if target.get("node"):
                        parts.append("target: " + str(target["node"]))
                    if private_detail.get("response_mode") == "frame-interview":
                        parts.append("submit answers to proceed")
                    else:
                        parts.append("choose proceed, revise, or stop")
                    for wrapped in _wrap(" · ".join(parts), max(1, right - content_x)):
                        if y >= bottom:
                            break
                        grid.put(content_x, y, wrapped, "soft", limit=right)
                        y += 1
            status = str(private.get("status") or private.get("state") or "")
            support = envelope.get("support")
            if (support is False or
                    isinstance(support, Mapping) and support.get("supported") is False):
                status = "No verified direct response; check the native prompt."
            if status and y < bottom:
                if narrow:
                    grid.put(x + 10, y, _clip("status: " + status, max(1, right - x - 10)),
                             "lvl_y" if private.get("state") in ("unknown", "expired", "cancelled")
                             else "dim", limit=right)
                else:
                    grid.put(x + 10, y, "status:", "label")
                    grid.put(x + 20, y, _clip(status, max(1, use - 20)),
                             "lvl_y" if private.get("state") in ("unknown", "expired", "cancelled")
                             else "dim", limit=right)
                y += 1
            request_state = str(private.get("state") or envelope.get("state") or "unknown")
            request_state = (request_state if envelope.get("state") == request_state else "unknown")
            from .state import (question_action_supported, question_answer_supported,
                                question_draft_supported,
                                question_choice_click_allowed, question_decisions,
                                question_free_text_available, question_multiple_select,
                                question_values_valid)
            supported = question_action_supported(envelope)
            can_answer = question_answer_supported(envelope)
            if request_state == "open" and supported and approval and y < bottom:
                for decision in question_decisions(envelope):
                    cursor = x + 10
                    label = "[ " + decision + " ]"
                    end = grid.put(cursor, y, label, "hearting" if decision != "defer" else "dim",
                                   limit=right)
                    if end > cursor:
                        grid.add_hit(y, cursor, end, ("question.decision", decision))
                    y += 1
                    if y >= bottom:
                        break
            elif not approval and (question_specs or can_answer):
                draft = private.get("draft")
                answers = draft.get("answers", {}) if hasattr(draft, "get") else {}
                can_draft = question_draft_supported(envelope)
                complete = True
                for index, question_spec in enumerate(question_specs, 1):
                    qid = question_spec["id"]
                    prompt = (question_spec.get("question") or question_spec.get("prompt")
                              or question_spec.get("header") or question_spec.get("title")
                              or question_spec.get("text") or envelope.get("question_id")
                              or "Question")
                    if hasattr(prompt, "get"):
                        prompt = (prompt.get("text") or prompt.get("question")
                                  or prompt.get("gate") or envelope.get("question_id")
                                  or "Question")
                    prompt_lines = _wrap(str(prompt), max(1, right - (x + 20)))
                    if y < bottom:
                        grid.put(x + 10, y, f"Q{index}:", "label")
                        grid.put(x + 20, y, prompt_lines[0] if prompt_lines else "Question",
                                 "soft", limit=right)
                        y += 1
                    for extra_line in prompt_lines[1:]:
                        if y >= bottom:
                            break
                        grid.put(x + 20, y, extra_line, "soft", limit=right)
                        y += 1
                    choices = question_spec.get("choices", question_spec.get("options", ()))
                    values = answers.get(qid, ()) if hasattr(answers, "get") else ()
                    values = tuple(values) if isinstance(values, (list, tuple)) else ()
                    multi_select = question_multiple_select(envelope, qid)
                    choice_labels = {}
                    for choice in choices if isinstance(choices, (list, tuple)) else ():
                        if isinstance(choice, Mapping):
                            value = choice.get("value")
                            if value is None:
                                value = choice.get("label", choice.get("text"))
                            label = choice.get("label") or choice.get("text") or value
                        else:
                            value = label = choice
                        if not isinstance(value, str) or not value:
                            continue
                        choice_labels[value] = label if isinstance(label, str) else value
                        if y >= bottom:
                            break
                        cursor = x + 20
                        selected_choice = value in values
                        shown = ("[" + ("x" if selected_choice else " ") + "] "
                                 + str(label if isinstance(label, str) else value)
                                 if multi_select else
                                 "[ " + str(label if isinstance(label, str) else value) + " ]")
                        selectable = (can_draft and request_state == "open"
                                      and question_choice_click_allowed(
                                          envelope, qid, value, values))
                        end = grid.put(cursor, y, shown,
                                       "hearting" if selectable else
                                       "soft" if selected_choice else "dim",
                                       limit=right)
                        if selectable and end > cursor:
                            grid.add_hit(y, cursor, end, ("question.choice", qid, value))
                        y += 1
                    answer_text = " · ".join(str(value) for value in values) if values else ""
                    display_answer = " · ".join(choice_labels.get(value, value)
                                                for value in values)
                    if question_spec.get("is_secret") or question_spec.get("isSecret"):
                        answer_text = "•" * min(len(answer_text), 32)
                        display_answer = "•" * min(len(display_answer), 32)
                    if not question_values_valid(envelope, qid, values):
                        complete = False
                    if (y < bottom and question_free_text_available(envelope, qid, values)):
                        label = "answer: " + (answer_text or "press e or click to type")
                        end = grid.put(x + 10, y, _clip(label, max(0, right - x - 10)),
                                       "soft", limit=right)
                        if can_draft and request_state == "open":
                            grid.add_hit(y, x + 10, max(x + 11, end),
                                         ("question.edit", qid))
                        y += 1
                    elif display_answer and y < bottom:
                        grid.put(x + 10, y, _clip("selected: " + display_answer,
                                                  max(0, right - x - 10)), "soft", limit=right)
                        y += 1
                no_native_input = any(
                    not question_free_text_available(envelope, item["id"],
                        answers.get(item["id"], ()) if hasattr(answers, "get") else ())
                    and not (item.get("choices", item.get("options")))
                    for item in question_specs)
                if no_native_input and y < bottom:
                    fallback = ("Multiple question response is unavailable; use the native prompt."
                                if len(question_specs) > 1 else
                                "No native answer input is available; use the native prompt.")
                    grid.put(x + 10, y, fallback, "dim", limit=right)
                    y += 1
                elif (complete and can_answer and request_state == "open"
                      and not private.get("pending") and y < bottom):
                    response_mode = (envelope.get("detail", {}).get("response_mode")
                                     if isinstance(envelope.get("detail"), Mapping) else None)
                    if envelope.get("source") == "S2":
                        label = ("[ submit answers and proceed ]" if response_mode == "frame-interview"
                                 else "[ submit gate decision ]")
                    else:
                        label = "[ submit answer ]"
                    end = grid.put(x + 10, y, label, "hearting", limit=right)
                    grid.add_hit(y, x + 10, end, ("question.submit",))
                    y += 1
            elif request_state == "open" and approval and y < bottom:
                grid.put(x + 10, y, "Approval response unavailable; use the native prompt.",
                         "dim", limit=right)
                y += 1
            if y < bottom and private.get("editing"):
                grid.put(x + 10, y, "typing · Enter submits · Esc pauses", "dim", limit=right)
                y += 1
    if older_count > 0 and not older_heading_drawn:
        label = f"OLDER WAITS · {older_count}"
        grid.put(x + 3, y, label, "sect", limit=right)
        toggle = "[ show ]" if not older_open else "[ hide ]"
        end = grid.put(right - len(toggle), y, toggle, "hearting", limit=right)
        grid.add_hit(y, right - len(toggle), end, ("question.older.toggle",))
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
    if use < 60:
        return {"narrow": True, "slim": True, "tiny": True, "gap": 1, "age_w": AGE_W + 1,
                "st": 3, "title": 10, "title_w": 5, "kind": 16, "own": 25,
                "own_w": 8, "ctx": 32, "prog": 33}
    narrow, slim = use < 150, use < 120
    title, kind = (10, 12) if slim else (12, 15)
    own_w, ctx_w = (10 if slim else 13 if narrow else 26), (0 if slim else 13)
    flex = use - title - kind - own_w - ctx_w - AGE_W - (4 if slim else 5) * GAP
    title_w = min(48, max(8 if narrow else 24, int(flex * (.35 if narrow else .5))))
    kind_x = title + title_w + GAP
    own_x = kind_x + kind + GAP
    ctx_x = own_x + own_w + GAP
    prog_x = ctx_x + (ctx_w + GAP if ctx_w else 0)
    return {"narrow": narrow, "slim": slim, "st": 3, "title": title,
            "title_w": title_w, "kind": kind_x, "own": own_x, "own_w": own_w,
            "ctx": ctx_x, "prog": prog_x, "tiny": False, "gap": GAP, "age_w": AGE_W + 1}


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
        parts = [(harness, "hb_" + harness), (" (", "dim"), (str(model_name), model_key),
                 ("·", "dim"), (effort, "eff_" + effort if effort in ("medium", "xhigh") else "dim"),
                 (")", "dim")]
    profile = route.get("profile")
    if profile and profile != "deep" and not slim:
        parts.append((" " + str(profile), "dim"))
    cursor = x
    for text, style in parts:
        cursor = grid.put(cursor, y, text, style, limit=limit)
