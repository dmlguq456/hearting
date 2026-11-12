"""Overview and list-view tables for the immutable explorer projection."""
from collections.abc import Mapping
from .live import current_route_counts, route_marker_state
from .grid import CellGrid
from .style import recede_key
from ..config import ui_text

X = 34
GAP = 3
IND = 2
CTX_W = 15                 # CONTEXT: a 10-cell gauge, one space and the ``NNN%`` value
CTX_TRACK = 10             # the gauge of a 1M window, as the Claude statusline and shell draw it
CTX_NARROW_TRACK = 5       # a narrow table keeps a half-length gauge before its percent
CTX_NARROW_W = CTX_NARROW_TRACK + 1 + 4
AGE_W = 6
TITLE_MIN = 8              # a folded row always keeps this much of what it is (its title)
TOOL_W = 14                # ``>_ apply_patch``: the open tool on a route row
# The words the Claude statusline and the session shell use for the same values (규격 2026-11-11).
ROUTE_WORD = "route"
ANSWER_WAIT_WORDS = {"ko": "◑답 대기", "en": "◑ to answer"}


def answer_wait_word():
    """``◑답 대기`` as the Claude statusline and shell write it; an English screen keeps
    Fleet's own English words (screen text follows the declared language)."""
    from ..config import ui_language
    return ANSWER_WAIT_WORDS.get(ui_language(), ANSWER_WAIT_WORDS["en"])


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


def repo_status_segments(status, room, narrow=False):
    """``(text, key)`` pieces of a project's status line on its OVERVIEW group rule.

    With a status document: its one line, what waits on the person (yellow when any), the
    questions waiting (when any), and when the project last changed. Without one: routes
    with live work, the questions, and the newest completed result. The free text (the
    line, or the result) gives way first; a narrow screen keeps only the count waiting on
    the person.
    """
    room = int(room)
    has_doc = status.get("line") is not None
    decisions = int(status.get("decisions") or 0)
    decide = (f"{decisions} to decide", "lvl_y" if decisions else "dim")
    if narrow:
        return [decide] if has_doc and decisions and _seg_width(decide[0]) <= room else []
    tail = []
    if has_doc:
        tail.append(decide)
    elif status.get("routes"):
        tail.append(route_count_segment(status))
    if status.get("questions"):
        tail.append((f"{answer_wait_word()} {int(status['questions'])}", "blocked"))
    if has_doc:
        free = str(status["line"])
        stale = bool(status.get("stale") and status.get("doc_age"))
        if stale:
            free = f"{status['doc_age']} · {free}"
        if status.get("changed") and not (stale and status["changed"] == status["doc_age"]):
            tail.append((str(status["changed"]), "dim"))
    else:
        done = status.get("done")
        free = (f"{status['done_age']} ✓ {done}" if status.get("done_age") else f"✓ {done}") if done else ""
    if not tail and not free:
        return []
    pieces = []
    for text, key in tail:
        pieces += [(BUNDLE_SEP, "dim"), (text, key)] if pieces else [(text, key)]
    fixed = sum(_seg_width(text) for text, _key in pieces)
    if has_doc:
        lead_room = room - fixed - 3
        if lead_room >= 8:
            return [(_clip(free, lead_room), "now_sub"), (BUNDLE_SEP, "dim")] + pieces
        return pieces if fixed <= room else []
    tail_room = room - fixed - (3 if pieces else 0)
    if free and tail_room >= 8:
        return pieces + ([(BUNDLE_SEP, "dim")] if pieces else []) + [(_clip(free, tail_room), "now_sub")]
    return pieces if fixed <= room else []


BUNDLE_SEP = " │ "


def route_count_segment(status):
    """``route 3 ⠏2 ✕1``: the project's current routes and how many run or stopped.

    The by-state counts appear only when the status carries them; a bare count stays
    ``route N`` and no zero is drawn."""
    text = f"{ROUTE_WORD} {int(status['routes'])}"
    for key, glyph in (("routes_running", "⠏"), ("routes_waiting", "●"),
                       ("routes_stopped", "✕")):
        value = status.get(key)
        if type(value) is int and value > 0:
            text += f" {glyph}{value}"
    return (text, "dim")


def gauge_cells(cells, pct):
    """Filled cells of a ``cells``-wide gauge: none at 0, all at 100, otherwise at least one and never all."""
    if pct <= 0:
        return 0
    if pct >= 100:
        return cells
    return max(1, min(cells - 1, int(pct * cells / 100 + 0.5)))


def _cell(value, missing="·"):
    return missing if value is None or value == "" else str(value)


def token_count_text(tokens):
    """``250K`` / ``1.2M``: an exact token count, drawn where no window gives a percent."""
    if type(tokens) is not int or tokens < 0:
        return None
    if tokens < 1000:
        return str(tokens)
    if tokens < 999_500:
        return f"{round(tokens / 1000)}K"
    return f"{tokens / 1_000_000:.1f}M"


def _context_key(pct):
    return "g_blocked" if pct >= 80 else "g_idle" if pct >= 50 else "done"


def _draw_context_gauge(grid, x, y, pct, window, tokens=None, narrow=False, bare=False):
    """The shared CONTEXT cell: a gauge and ``NNN%``; the exact token count when only the count is known.

    The track is ten cells for a 1M window and proportionally shorter for a smaller measured
    one. Nothing is estimated: without a percent and a count the cell keeps its ``·``."""
    track = CTX_NARROW_TRACK if narrow else CTX_TRACK
    value_x = x if bare else x + track + 1
    if pct is None:
        text = token_count_text(tokens)
        grid.put(value_x, y, (text or "·").rjust(4), "dim")
        return
    pct = int(pct)
    key = _context_key(pct)
    if bare:                      # the tightest row keeps the value and drops the gauge
        grid.put(value_x, y, f"{pct:3d}%", key if pct >= 50 else "dim")
        return
    width = min(track, max(1, round(track * float(window or 1))))
    filled = gauge_cells(width, pct)
    grid.put(x, y, "━" * filled, key)
    grid.put(x + filled, y, "─" * (width - filled), "dim")
    grid.put(value_x, y, f"{pct:3d}%", key if pct >= 50 else "dim")


_EFFORT_WORD = {"medium": "med"}


def _effort_word(effort):
    return _EFFORT_WORD.get(str(effort), str(effort)) if effort else ""


def model_cell_parts(harness, model, effort):
    """``claude Fable 5.2 xhigh``: harness, model and effort in one cell, without brackets."""
    harness = str(harness or "other")
    parts = [(harness, "hb_" + harness if harness in ("claude", "codex", "opencode", "other") else "dim")]
    if model:
        name, key = (tuple(model) + ("dim",))[:2]
        parts.append((" " + str(name), key))
    if effort:
        effort = str(effort)
        parts.append((" " + _effort_word(effort),
                      "eff_" + effort if effort in ("medium", "xhigh") else "dim"))
    return parts


_EFFORT_FROM_WORD = {"med": "medium"}


def _model_cell_facts(segments):
    """``(harness, model pair, effort)`` from MODEL cell segments, in the cell's own shape or in
    the older ``harness (model·effort)`` one a recorded view model may still carry."""
    segments = [(str(text), key) for text, key in segments]
    harness = next((text for text, key in segments if isinstance(key, str) and key.startswith("hb_")), None)
    model = effort = None
    for text, key in segments:
        word = text.strip()
        if text == harness or word in ("", "(", ")", "·"):
            continue
        if _EFFORT_FROM_WORD.get(word, word) in ("low", "medium", "high", "xhigh", "max", "minimal"):
            effort = _EFFORT_FROM_WORD.get(word, word)
        elif model is None:
            model = (word, key)
    return harness, model, effort


def _put_model_cell(grid, x, y, parts, limit):
    """Draw the MODEL cell; the model name gives way first, then the effort, never the harness."""
    parts = list(parts)
    room = max(0, limit - x)
    if sum(_seg_width(text) for text, _key in parts) > room and len(parts) > 2:
        fixed = _seg_width(parts[0][0]) + _seg_width(parts[2][0])
        model_room = room - fixed
        if model_room >= 6:
            parts[1] = (_clip(parts[1][0], model_room), parts[1][1])
        else:
            parts = parts[:2]
    if sum(_seg_width(text) for text, _key in parts) > room and len(parts) > 1:
        model_room = room - _seg_width(parts[0][0])
        parts = parts[:1] + ([(_clip(parts[1][0], model_room), parts[1][1])] if model_room >= 6 else [])
    for text, key in parts:
        x = grid.put(x, y, text, key, limit=limit)
    return x


def draw_overview(grid: CellGrid, vm, state, x=34, y=3, *, session_table=False):
    use = max(0, grid.cols - x - 2)
    right = x + use + 1
    if not session_table:
        grid.fill(0, x - 2, y, " ", tint=236)
        grid.put(x - 2, y, "┃", "line")
        grid.put(1, y, "OVERVIEW", "head", tint=236)
    grid.fill(x, grid.cols - 1, y, " ", tint=234)
    route_rows = [route for route in vm.routes if route.get("session_tag")]
    current_routes, open_current_routes = current_route_counts(route_rows)
    # A folded row's `route N` keeps its whole count: the MODEL cell gives way to it as it does
    # to the title (the count is never cut to a shorter number or a part of the word).
    tail_need = max((_seg_width(f"{ROUTE_WORD} {count}") for tag, count in current_routes.items()
                     if state.opened != tag), default=0)
    c = _overview_columns(use, _overview_harness_width(vm.sessions), tail_need)
    harn_end = _overview_harness_end(use, c)
    for offset, label in ((IND + 3, "ID"), (c["h"], "MODEL"), (c["ctx"], "CTX" if c["bare"] else "CONTEXT"),
                          *(((c["title"], "TITLE"),) if c["title_w"] else ()),
                          *(([(c["now"], ui_text("fleet.input.label"))] if c["now"] is not None else [])),
                          (use - 2, "AGE")):
        if label == "MODEL" and harn_end is None or label == "CTX" and not c.get("ctx_w", 1):
            continue                      # a table this narrow has no room for the harness cell
        grid.put(x + offset, y, label, "head", tint=234, limit=right)
    y += 1
    repos = tuple(vm.repos or ())
    sessions = tuple(vm.sessions or ())
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
        if name and not session_table:
            if page_scope:
                grid.add_hit(y, x, right, ("scope", "repo:" + str(repo.get("id", name))))
            grid.put(x + 1, y, "┈ ", "line")
            end = grid.put(x + 3, y, repo_rule_label(name, max(1, use - 6)),
                           "title_dim", limit=x + use - 1)
            # The project's status line rides on its rule (no row of its own); its page shows
            # the same facts as a block, so a page's own rule stays bare.
            status = None if page_scope else repo.get("status")
            segments = (repo_status_segments(status, right - 4 - end - 3, narrow=grid.cols < 100)
                        if isinstance(status, Mapping) else ())
            if not segments:
                end += 2
            if segments:
                end = grid.put(end, y, BUNDLE_SEP, "dim", limit=right - 4)
                for text, key in segments:
                    end = grid.put(end, y, text, key, limit=right - 4)
                grid.put(end, y, " ", "branch_s")
                rule_x = end + 1
            else:
                grid.put(end, y, " ", "branch_s")
                rule_x = end + 2
            for xx in range(rule_x, right):
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
            restart_text = ""
            restart_inline = False
            if session.get("restart") == "stopped":
                restart_text = ui_text("fleet.resume.stopped")
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
            age_text = str(session.get("age") or "")
            # The ID cell ends one cell short of the age text, so a long tag (an unnamed Codex
            # thread's `[?26]`) is clipped inside its own column and never touches AGE.
            id_end = x + use - len(age_text) - 1
            tag_room = id_end - tag_x - 2
            if tag_room < 1:
                grid.put(tag_x, y, "…", "dim", limit=max(tag_x, id_end))
            else:
                shown = _clip_cut(tag, tag_room) if _seg_width(tag) > tag_room else tag
                grid.put(tag_x, y, "[", "dim")
                grid.put(tag_x + 1, y, shown, "steward" if session.get("steward") else "soft")
                grid.put(tag_x + 1 + _seg_width(shown), y, "]", "dim")
            if session.get("steward") and x + IND + 9 <= id_end:
                grid.put(x + IND + 8, y, "⚑", "steward")
            harness = str(session.get("harness") or "other")
            harn_limit = x + c["ctx"] - c["gap"]
            if harn_end is not None:
                harn_limit = min(harn_limit, x + harn_end)
                _put_model_cell(grid, x + c["h"], y,
                                model_cell_parts(harness, session.get("model"),
                                                 str(session.get("effort") or "")),
                                harn_limit)
            context = session.get("context")
            if c.get("ctx_w", 1):
                _draw_context_gauge(grid, x + c["ctx"], y, context, session.get("window"),
                                    narrow=c["narrow"], bare=c["bare"])
            title_whole = False
            if c["title_w"]:
                title = str(session.get("title") or "·")
                # The checkout follows the title in parentheses (12 cells at most); the title gives way
                # first, and a row whose branch is unknown draws no parentheses at all.
                branch = session.get("branch")
                tail = " (%s)" % _clip(str(branch), 12) if branch not in (None, "") else ""
                route_count = current_routes.get(tag, 0)
                route_tail = f" {ROUTE_WORD} {route_count}" if route_count and state.opened != tag else ""
                # Current work remains visible while the session is folded.
                # Reserve its cells before a long CJK title or branch is drawn.
                if route_tail and _seg_width(route_tail) > c["title_w"]:
                    # No title is left beside it: the count starts the cell without its space,
                    # and where even that does not fit it is left out whole, never cut.
                    route_tail = route_tail[1:] if _seg_width(route_tail) - 1 <= c["title_w"] else ""
                if route_tail:
                    tail = tail + route_tail if _seg_width(tail + route_tail) <= c["title_w"] // 2 else route_tail
                if restart_text and _seg_width(" " + restart_text + route_tail) <= c["title_w"]:
                    restart_inline = True
                    tail = " " + restart_text + route_tail
                room = max(0, c["title_w"] - _seg_width(tail))
                branch_tail = " (%s)" % _clip(str(branch), 12) if branch not in (None, "") else ""
                if branch_tail and tail.startswith(branch_tail) and room < min(TITLE_MIN, _seg_width(title)):
                    # The title keeps its minimum before the checkout; the unfolded TITLE line has both.
                    tail = tail[len(branch_tail):]
                    room = max(0, c["title_w"] - _seg_width(tail))
                title_whole = (_seg_width(title) <= room and (
                    not branch_tail or tail.startswith(branch_tail) and branch_tail == " (%s)" % branch))
                title_text = _clip(title, room)
                bold_key = {"claude": ("c", 1, 0), "codex": ("m", 1, 0),
                            "opencode": ("l", 1, 0)}.get(harness, "unknown")
                title_end = grid.put(x + c["title"], y, title_text, bold_key,
                                     limit=x + c["title"] + room)
                if tail:
                    # An open count pulses on the shared working beat between the bright and
                    # dark green of the g_work/g_work_off pair; its text stays, so the title
                    # and columns never move. A finished count stays a steady dim note.
                    tail_key = "g_idle" if session.get("restart") == "stopped" else "dim"
                    route_x = title_end + _seg_width(tail) - _seg_width(route_tail)
                    plain = tail[:len(tail) - len(route_tail)] if route_tail else tail
                    if plain:
                        grid.put(title_end, y, plain, tail_key if not route_tail or restart_inline else "dim",
                                 limit=x + c["title"] + c["title_w"])
                    if route_tail:
                        route_key = "g_idle" if session.get("restart") == "stopped" else "dim"
                        if tag in open_current_routes and session.get("restart") != "stopped":
                            route_key = "g_work" if getattr(grid, "beat", 1) else "g_work_off"
                        grid.put(route_x, y, route_tail, route_key,
                                 limit=x + c["title"] + c["title_w"])
            grid.put(x + use - len(str(session.get("age") or "")), y,
                     str(session.get("age") or ""), "dim")
            if c["now"] is not None:
                now_start = x + c["now"]
                now_end = x + use - AGE_W - GAP
                from ..token_budget import compaction_imminent
                pressure = (session.get("context_detail") or {}).get("pressure")
                if compaction_imminent(pressure):
                    now_start = grid.put(now_start, y, ui_text("fleet.context.compaction_imminent") + " · ",
                                         "lvl_r", limit=now_end)
                now_text = _clip(str(session.get("last_input") or ui_text("fleet.input.unknown")),
                                 max(0, now_end - now_start))
                # Input is muted grey; unfolded NOW keeps its existing white.
                grid.put(now_start, y, now_text, "dim",
                         limit=now_end)
            y += 1
            if restart_text and not restart_inline:
                grid.put(x + IND + 3, y, restart_text, "g_idle", limit=right)
                y += 1
            if session_table and c["now"] is None:
                # The repo page keeps the same table columns; a narrow pane puts
                # the existing last-input projection directly below its session.
                grid.put(x + IND + 3, y,
                         _clip(str(session.get("last_input") or ui_text("fleet.input.unknown")),
                               max(0, right - x - IND - 4)),
                         "head" if session.get("state") in ("working", "blocked") else "title_dim",
                         limit=right)
                y += 1
            gpu_count = sum(1 for host in (vm.compute or ()) for gpu in host[5]
                            if tag in (gpu[7] or ()))
            if gpu_count > 1 and y < grid.rows - 5:
                grid.put(x + c["h"], y, "●", "work")
                grid.put(x + c["h"] + 2, y, f"GPU ×{gpu_count}", "dim")
                y += 1
            if state.opened == tag:
                _tint_row(grid, row_y, x - 1, right, 236)
                y = _session_unfold(grid, vm, x, use, y, session, c, input_shown=session_table,
                                    title_whole=title_whole)
            if y >= grid.rows - 5:
                if sticky:
                    pin(None, tail_height, lambda target, top: _draw_overview_tail(
                        target, vm, state, x, use, right, top, route_rows, repos, recent_rows=recent_rows))
                    grid.mark_page_incomplete()
                    return
                grid.put(right - 18, grid.rows - 5, "↓ more below", "dim", limit=right)
                return
    if session_table:
        return y
    sessions_total = len(sessions)
    working_sessions = sum(1 for item in sessions if item.get("state") == "working")
    waits = (vm.system or {}).get("answer_waits") or {}
    questions = waits.get("total") if waits.get("state") == "complete" else None
    if not waits:
        questions = sum(1 for item in (vm.questions or ()) if not item.get("older"))
    from .chrome import count_word
    summary = (f"{count_word(sessions_total, 'session')} · {working_sessions} working · "
               f"{answer_wait_word()} {questions if questions is not None else '?'} · {count_word(sum(1 for repo in repos if repo_label(repo.get('name', '·'))), 'project')}")
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
    repo_width = route_repo_width(route_rows, use)
    columns = route_row_columns(use, route_rows, _overview_harness_width(vm.sessions))
    title_w = route_title_width(route_rows, columns, repo_width, use)
    for route in route_rows:
        if getattr(state, "scope", "-") == "-":
            grid.add_hit(y, x + IND + 1, right,
                         ("root", "ROUTES", {"ropen:ROUTES": route.get("id")}))
        source = next((session for session in vm.sessions
                       if str(session.get("tag")) == str(route.get("session_tag"))), None)
        repo_name = repo_label(route.get("repo"))
        _draw_overview_route(grid, x, use, y, route,
                             steward=bool((source or {}).get("steward")),
                             repo_w=repo_width, show_repo=bool(repo_name),
                             columns=columns, title_w=title_w)
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


def route_counts(routes):
    """``(open, no_response, stopped)``: a route nothing of which runs (no response) is not open."""
    states = [str(route.get("state") or "") for route in routes]
    return (sum(1 for route, state in zip(routes, states)
                if state not in ("done", "stale") and not route.get("closed")),
            states.count("stale"),
            sum(1 for route in routes if route.get("state") == "dead" and not route.get("closed")))


def _draw_overview_routes_rule(grid, vm, state, x, use, right, y, route_rows):
    open_routes, no_response, stopped = route_counts(route_rows)
    grid.put(x, y, "── ", "line")
    label_x = grid.put(x + 3, y, "ROUTES", "head")
    # Open routes pulse on the shared working beat (the same g_work/g_work_off pair as the
    # working dot); with none open the count stays a steady dim note.
    lit = bool(getattr(grid, "beat", 1))
    count_key = ("g_work" if lit else "g_work_off") if open_routes else "dim"
    count_x = grid.put(label_x, y, "  ", "dim", limit=right)
    count_end = grid.put(count_x, y, f"{open_routes} open", count_key, limit=right)
    meta = ((f" · no response {no_response}" if no_response else "") +
            (f" · {stopped} stopped" if stopped else ""))
    meta_end = grid.put(count_end, y, meta, "dim", limit=right) if meta else count_end
    grid.put(meta_end, y, " ", "branch_s")
    for cell_x in range(meta_end + 1, right):
        grid.put(cell_x, y, "─", "line")
    mix = _harness_mix(route_rows)
    mix_width = sum(len(text) for text, _key in mix)
    mix_x = x + use - 2 - mix_width
    if mix_x - x >= 40 and mix_x > meta_end + 1:     # only where it clears the section's own title
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
        if route.get("state") in ("dead", "done", "stale"):
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


def _put_wrapped(grid, x, y, parts, right, cont_x, newline=None):
    """Draw ``parts`` whole from ``x``, going on at ``cont_x`` on the next row when this one ends.

    An unfolded line gives its values in full, so nothing here is clipped or elided: a segment
    that fits on a fresh row moves there whole, a longer one is cut across rows. ``newline(y)``
    opens the next row (default ``y + 1``). Returns ``(x, y)`` after the last cell drawn."""
    newline = newline or (lambda row: row + 1)
    for text, key in parts:
        text = str(text)
        if (x > cont_x and x + _seg_width(text) > right
                and _seg_width(text.lstrip()) <= right - cont_x):
            y, x = newline(y), cont_x
        if x == cont_x:
            text = text.lstrip()
        chunk = ""
        for char in text:
            if (chunk or x > cont_x) and x + _seg_width(chunk + char) > right:
                x = grid.put(x, y, chunk, key, limit=right)
                y, x, chunk = newline(y), cont_x, ""
                if char == " ":
                    continue
            chunk += char
        x = grid.put(x, y, chunk, key, limit=right)
    return x, y


def _session_unfold(grid, vm, x, use, y, session, c, input_shown=False, title_whole=True):
    """Under an unfolded session row: NOW in full, each route on a line, its question.

    What the folded row left out or cut is given in full here: the title and checkout, a
    clipped MODEL cell's whole ``harness model effort``, and LAST INPUT on a narrow row."""
    tag = str(session.get("tag"))
    right = x + use + 1
    x_lab, x_val = x + c["h"], x + c["h"] + 7
    if not title_whole:
        branch = session.get("branch")
        title = str(session.get("title") or "·") + (
            " (%s)" % branch if branch not in (None, "") else "")
        grid.put(x_lab, y, "TITLE", "label")
        for line in _wrap_cells(title, x + use - x_val):
            grid.put(x_val, y, line, "soft")
            y += 1
    model = _harness_cell_text(session)
    if _seg_width(model) > (c["ctx"] - c["gap"] - c["h"] if c.get("harn_w") else 0):
        # The row clipped its MODEL cell (model or effort given way): the whole cell is here.
        # Whole, wrapped if need be: the folded cell's shortening is not repeated here. A row
        # it goes on to starts under the label, so a long model ID stays in one piece.
        grid.put(x_lab, y, "MODEL", "label")
        _x, y = _put_wrapped(grid, max(x_val, x_lab + 7), y,
                             model_cell_parts(session.get("harness"), session.get("model"),
                                              str(session.get("effort") or "")), x + use, x_lab)
        y += 1
    if c["now"] is None:
        if not input_shown:
            label = ui_text("fleet.input.label")
            input_x = max(x_val, x_lab + _seg_width(label) + 2)
            grid.put(x_lab, y, label, "label", limit=right)
            for line in _wrap_cells(str(session.get("last_input") or ui_text("fleet.input.unknown")),
                                    max(1, right - input_x)):
                grid.put(input_x, y, line, "soft", limit=right)
                y += 1
    grid.put(x_lab, y, "NOW", "label")
    now = session.get("now")
    for line in (_wrap_cells(now, x + use - x_val) if now else ["·"]):
        grid.put(x_val, y, line, "now_sub" if now else "dim")
        y += 1
    received_label = ui_text("fleet.input.received")
    message_x = max(x_val, x_lab + _seg_width(received_label) + 2)
    grid.put(x_lab, y, received_label, "label", limit=right)
    for line in session.get("received_inputs") or ("·",):
        grid.put(message_x, y, _clip(str(line), max(0, right - message_x)), "soft", limit=right)
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
    detail = session.get("context_detail")
    if detail is not None:
        from ..session_line import context_detail_lines
        for label, value in context_detail_lines(detail):
            grid.put(x_lab, y, label, "label", limit=right)
            grid.put(max(x_val, x_lab + _seg_width(label) + 2), y, value, "soft", limit=right)
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
    mine = [item for item in (vm.questions or ()) if item.get("session_tag") == tag]
    question = next((item for item in mine if not item.get("older")), mine[0] if mine else None)
    if question:
        marker = str(question.get("marker") or "●")
        lead = [(marker, "lvl_y" if marker == "◑" else "blocked"),
                (" " + str(question_about(question)), "blocked"),
                (" · waiting " + str(question.get("wait")) + " · ", "dim")]
        from .pages import _fit, _segs
        pane = question.get("pane")
        link = "(open ▸)" if question.get("open_tag") and pane else ""
        if link and x_val + sum(_seg_width(text) for text, _k in lead) + 2 + _seg_width(link) > right:
            link = "▸"                     # narrow: the question text gives way, the link stays whole
        room = right - x_val - (2 + _seg_width(link) if link else 0)
        cursor = _segs(grid, x_val, y, _fit(lead, room) if link else lead, right)
        if link:
            cursor = grid.put(cursor, y, "  ", "branch_s", limit=right)
            open_x = grid.put(cursor, y, link, "hearting", limit=right)
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


def _draw_overview_route(grid, x, use, y, route, steward=False, repo_w=None, show_repo=True,
                         columns=None, title_w=None):
    draw_route_row(grid, x, use, y, route, columns or route_row_columns(use, (route,)),
                   steward=steward, show_repo=show_repo, title_w=title_w, repo_w=repo_w)
    if route_is_history(route):
        # Only the OVERVIEW summary row recedes here. `_draw_route_line` is also the
        # session-unfold route line, which another change owns; leave that path alone.
        _recede_row(grid, y, x + IND + 1, x + use)


_ROUTE_MARK = {"working": ("⠏", "g_spin"), "dead": ("✕", "g_dead"), "blocked": ("●", "g_blocked"),
               "done": ("✓", "grp_live"), "stale": ("◌", "dim"), "idle": ("●", "g_work_off")}


def route_doer_cells(route):
    """``(model parts, (pct, window, tokens), tool, agents)`` for a route row.

    The live doer (the shown stage's worker, else the owner attempt) when the route record has
    one; a recorded/stale record falls back to its owner fields and owner gauge."""
    doer = route.get("doer")
    if isinstance(doer, Mapping):
        harness = doer.get("harness") or route.get("harness")
        # An attempt that names no harness is not identified: no model or effort is
        # attached to it (and none is borrowed from a frame leg).
        parts = (model_cell_parts(harness, doer.get("model"), doer.get("effort")) if harness
                 else [("·", "dim")])
        context = (doer.get("context"), doer.get("window"), doer.get("tokens"))
        return parts, context, doer.get("tool"), int(doer.get("agents") or 0)
    parts = (model_cell_parts(route["harness"], route.get("model"), route.get("effort"))
             if route.get("harness") else [("·", "dim")])
    return parts, route_owner_context(route), None, 0


TOOL_TARGET_W = 40          # ``>_ python3 · tools/run-tests.py --sel…`` on a folded route row


def _route_rail_text(route):
    """The stage rail's plain words, to size it before drawing (``one-shot ●``)."""
    stages = route.get("stages") or ()
    current = next((stage for stage in stages if stage.get("st") in ("active", "failed")), None)
    if len(stages) <= 1:
        return (str((current or (stages[0] if stages else {})).get("name") or "·") + " ●")
    return " › ".join(str(stage.get("name") or "·") for stage in stages)


def route_activity_rows(route):
    """What the route's doer is on now and just finished, one row each, from its own stream:

    ``>_ python3 · tools/run-tests.py  3m · +1 agent`` (or ``>_ no tool`` when its read
    stream shows none open) and ``after Edit · utilities/x.py``. Unreported values are left
    out; both are allow-listed summaries, never the worker's raw command or words."""
    doer = route.get("doer")
    if not isinstance(doer, Mapping):
        return []
    rows = []
    if doer.get("tool"):
        row = [(">_ ", "soft"), (str(doer["tool"]), "soft")]
        if doer.get("target"):
            row.append((" · " + str(doer["target"]), "soft"))
        if doer.get("tool_age"):
            row.append(("  " + str(doer["tool_age"]), "dim"))
        if int(doer.get("agents") or 0):
            row.append((f" · +{int(doer['agents'])} agent", "dim"))
        rows.append(row)
    elif doer.get("idle_tool"):
        rows.append([(">_ ", "dim"), ("no tool", "dim")])
    if doer.get("after_tool"):
        rows.append([("after ", "dim"), (str(doer["after_tool"]), "dim")])
    return rows


def route_row_columns(use, routes, session_need=0):
    """The session row's columns for route rows: the MODEL cell fits the widest doer of either."""
    need = max([session_need] + [_seg_width("".join(text for text, _key in route_doer_cells(route)[0]))
                                 for route in routes])
    return _overview_columns(use, need)


def _route_has_progress(route):
    return bool(route.get("legs") or route.get("stages") or route.get("resume")
                or route.get("state") == "stale")


def route_title_width(routes, c, repo_w=0, use=None):
    """One TITLE width for a list of route rows: the longest title (and repo name), up to 48
    cells, leaving the stage rail and open tool half of what follows the title's start when
    any row has a rail. The title is the cell that gives way."""
    if not c["title_w"]:
        return 0
    wanted = max((_seg_width(str(route.get("title") or "·")) for route in routes), default=1)
    wanted += (repo_w + 1) if repo_w else 0
    width = min(48, wanted)
    if use is not None and c.get("bare"):
        width = min(width, c["title_w"])          # no rail fits beside the tightest title
    elif use is not None:
        after = use - c["title"] - AGE_W - GAP
        reserve = max(16, after // 2) if any(_route_has_progress(route) for route in routes) else 0
        width = min(width, max(8, after - (GAP + reserve if reserve else 0)))
    else:
        width = min(width, c["title_w"])
    return max(6, width)


def route_repo_width(routes, use):
    """One repo-name width for an ungrouped route list: the longest name, within a quarter row."""
    wanted = max((_seg_width(repo_rule_label(route.get("repo"))) for route in routes), default=0)
    return min(wanted, max(8, use // 4)) if wanted else 0


def draw_route_row(grid, x, use, y, route, c, steward=False, show_repo=False, title_w=None,
                   repo_w=None, title_below=True):
    """One route on one line, in the session row's columns:

    ``⠏ [e9]    claude Opus 5.5 med   ━━──────── 25%  PR #1505 보정  one-shot ●   >_ Bash   1h 27m``

    marker and ID, MODEL (harness model effort of who works now), CONTEXT, TITLE (the project
    name ahead of it on a list that is not grouped by project), the stage rail with any
    ``resume`` hint, the open tool and sub-agents while it runs, and the elapsed time. TITLE
    gives way first; the marker, the percent and the time are never cut. A cut title is in the
    returned list unless ``title_below`` says the detail band under the row carries it whole."""
    right = x + use
    age = _clip(str(route.get("age") or ""), AGE_W + 1)
    age_x = right - _seg_width(age)
    marker, marker_key = _ROUTE_MARK[route_marker_state(route)]
    grid.put(x + IND + 1, y, marker, marker_key)
    tag = str(route.get("session_tag") or route.get("past_from") or "")
    tag_x = x + IND + 3
    if tag:
        tag_room = max(1, c["h"] - IND - 3 - 3 - (2 if steward else 0))
        shown = tag if _seg_width(tag) <= tag_room else _clip_cut(tag, tag_room)
        grid.put(tag_x, y, "[", "dim")
        grid.put(tag_x + 1, y, shown, ("steward" if steward else "soft") if route.get("session_tag") else "dim")
        grid.put(tag_x + 1 + _seg_width(shown), y, "]", "dim")
    if steward and x + IND + 9 <= x + c["h"]:
        grid.put(x + IND + 8, y, "⚑", "steward")
    parts, (pct, window, tokens), tool, agents = route_doer_cells(route)
    harn_end = _overview_harness_end(use, c)
    # What this row leaves out or cuts, for the unfolded route to give in full (a cut title is
    # whole on the route detail's top band).
    dropped = []
    model_room = (min(x + c["ctx"] - c["gap"], x + harn_end) - (x + c["h"])) if harn_end is not None else 0
    if sum(_seg_width(text) for text, _key in parts) > model_room:
        dropped.append(list(parts))
    if harn_end is not None:
        _put_model_cell(grid, x + c["h"], y, parts, min(x + c["ctx"] - c["gap"], x + harn_end))
    if c.get("ctx_w", 1):
        _draw_context_gauge(grid, x + c["ctx"], y, pct, window, tokens, narrow=c["narrow"], bare=c["bare"])
    elif pct is not None or token_count_text(tokens):
        dropped.append([(f"{int(pct)}%" if pct is not None else token_count_text(tokens), "dim")])
    cursor = x + c["title"]
    title_w = c["title_w"] if title_w is None else title_w
    room = max(0, min(title_w, age_x - c["gap"] - cursor))
    if room:
        repo_text = repo_rule_label(route.get("repo")) if show_repo else ""
        title_need = min(8, _seg_width(str(route.get("title") or "·")))
        repo_room = min(_seg_width(repo_text), repo_w or 12, max(0, room - 1 - title_need))
        if repo_w and repo_room < _seg_width(repo_text) and repo_room >= 4:
            repo_room = min(repo_room, repo_w)
        if repo_text and repo_room >= 4:
            cursor = grid.put(cursor, y, _clip(repo_text, repo_room), "title_dim", limit=cursor + repo_room)
            cursor += 1
        title_room = x + c["title"] + room - cursor
        grid.put(cursor, y, _clip(str(route.get("title") or "·"), max(0, title_room)), "soft",
                 limit=x + c["title"] + room)
        cursor = x + c["title"] + room + GAP
    title = str(route.get("title") or "")
    if not title_below and title and _seg_width(title) > (title_room if room else 0):
        dropped.insert(0, [(title, "soft")])
    # ``>_ Bash · +1 agent``: the open tool and live sub-agents, only while the work runs.
    tool_text = ">_ " + str(tool) if tool else ""
    agent_text = f" · +{agents} agent" if tool_text and agents else ""
    progress_end = age_x - GAP
    full_tool = [(tool_text, "soft")] + ([(agent_text, "dim")] if agent_text else [])
    # What the tool acts on rides with it while the row has room for it and a stage rail;
    # the unfolded activity row below gives it whole (with its time) either way.
    activity = route_activity_rows(route)
    target = (route.get("doer") or {}).get("target") if tool_text else None
    if target:
        # Only where the cut target fits beside a whole stage rail (a wide row); otherwise the
        # folded row keeps the tool name and the unfolded activity row gives the target.
        wide = _clip(tool_text + " · " + str(target), TOOL_TARGET_W)
        rail = max(12, _seg_width(_route_rail_text(route)))
        if age_x - GAP - _seg_width(wide + agent_text) - cursor >= rail + GAP:
            tool_text = wide
    if activity:
        full_tool = []          # the activity row carries the tool, its target and agents
    if tool_text:
        width = max(TOOL_W, _seg_width(tool_text + agent_text))
        if age_x - GAP - width - cursor < 12:
            agent_text, width = "", TOOL_W          # the agent count gives way before the tool
        tool_x = age_x - GAP - width
        if tool_x - cursor >= 12:
            end = grid.put(tool_x, y, _clip(tool_text, width), "soft", limit=tool_x + width)
            if agent_text:
                grid.put(end, y, agent_text, "dim", limit=tool_x + width)
            progress_end = tool_x - GAP
            if full_tool and (_seg_width(tool_text) > width or len(full_tool) > 1 and not agent_text):
                dropped.append(full_tool)
        elif full_tool:
            dropped.append(full_tool)
    if progress_end > cursor:
        _draw_route_progress(grid, cursor, progress_end, y, route, card=False)
    grid.put(age_x, y, age, "dim")
    return dropped


def _route_line_room(x0, x1, route):
    tag_text = str(route.get("session_tag") or "··")
    tag_width = min(_seg_width(tag_text), 8)
    return max(0, x1 - x0 - 2 - (tag_width + 2 + GAP) - AGE_W - GAP)


def _route_line_repo_width(x0, x1, routes):
    """One repo width for every summary line: the longest shown name, capped by the narrowest row."""
    wanted = max((_seg_width(repo_rule_label(route.get("repo"))) for route in routes), default=0)
    if not wanted:
        return 0
    cap = min((max(0, min(_route_line_room(x0, x1, route) // 2,
                          _route_line_room(x0, x1, route) - 24)) for route in routes), default=0)
    return min(wanted, cap)


def _draw_route_line(grid, x0, x1, y, route, tag=True, repo=True, steward=False,
                     repo_w=None, show_repo=True):
    age = _clip(str(route.get("age") or ""), AGE_W + 1)
    tag_text = str(route.get("session_tag") or "··") if tag else ""
    tag_width = min(_seg_width(tag_text), 8) if tag else 0
    if tag:
        tag_text = _clip(tag_text, tag_width)
    tag_room = tag_width + 2 + GAP if tag else 0
    room = max(0, x1 - x0 - 2 - tag_room - AGE_W - GAP)
    repo_text = repo_rule_label(route.get("repo")) if repo else ""
    if repo_w is None:
        repo_w = min(_seg_width(repo_text), max(0, min(room // 2, room - 24)))
    else:
        repo_w = min(repo_w, max(0, min(room // 2, room - 24)))
    own = room - (repo_w + GAP if repo_w else 0) >= 8 + GAP + 12 + GAP + 12
    # The routed owner's harness, model and effort, as the ROUTES HARNESS column reads
    # them, whenever the title keeps its room; otherwise the harness alone, as before.
    state = "working" if route.get("activity") == "running" else route.get("state")
    if route.get("owner_waits"):
        state = "idle"                    # the owner waits on its worker; the worker line spins
    marker, marker_key = {"working": ("⠏", "g_spin"), "dead": ("✕", "g_dead"),
                          "blocked": ("●", "g_blocked"), "done": ("✓", "grp_live"),
                          "stale": ("◌", "dim")}.get(state, ("●", "g_work_off"))
    grid.put(x0, y, marker, marker_key)
    x = x0 + 2
    if repo_w:
        if show_repo and repo_text:
            grid.put(x, y, _clip(repo_text, repo_w), "title_dim", limit=x + repo_w)
        x += repo_w + GAP
    if tag:
        grid.put(x, y, "[", "dim")
        grid.put(x + 1, y, tag_text, "steward" if steward else "soft", limit=x + 1 + tag_width)
        grid.put(x + 1 + tag_width, y, "]", "dim")
        x += tag_width + 2 + GAP
    owner_x = x
    if own:
        x += 8 + GAP
    title_width = min(48, max(6, int((x1 - x - AGE_W - GAP) * .45)))
    title_width = min(title_width, max(0, x1 - _seg_width(age) - GAP - x))
    if own:
        # The routed owner's harness, model and effort, as the ROUTES HARNESS column reads
        # them. The room comes from the title, never from the stage rail; a title that would
        # fall under 16 cells keeps the harness alone, as before.
        extra = max(0, _owner_width(route) - 8)
        if extra and title_width - extra >= 16:
            _draw_owner(grid, owner_x, y, route, False, True, owner_x + 8 + extra)
            x += extra
            title_width -= extra
        else:
            owner = str(route.get("harness") or "·")
            grid.put(owner_x, y, owner, "hb_" + owner, limit=owner_x + 8)
    grid.put(x, y, _clip(str(route.get("title") or "·"), title_width), "soft")
    x += title_width + GAP
    _draw_route_progress(grid, x, x1 - AGE_W - GAP, y, route)
    grid.put(x1 - _seg_width(age), y, age, "dim")


def _draw_route_progress(grid, x, end, y, route, card=True):
    if route.get("state") == "stale" and x < end:
        # Still marked in flight, but nothing of it runs: said in words before its stages.
        x = grid.put(x, y, "no response ", "lvl_y", limit=end)
    if route.get("capability") == "frame" and route.get("shape") == "framed":
        legs = tuple(route.get("legs") or ())
        # The route's own leg is appended after every earlier leg, so the last frame leg is
        # this route's mark; the first one may belong to a predecessor frame route.
        frame_leg = next((leg for leg in reversed(legs) if str(leg[0]) == "frame"), None)
        status = str(frame_leg[1]) if frame_leg else ""
        mark, mark_key = {"done": ("✓", "done"), "active": ("●", "work"),
                          "failed": ("✕", "lvl_r")}.get(status, ("·", "dim"))
        cursor = grid.put(x, y, "frame", "title_dim", limit=end)
        cursor = grid.put(cursor, y, " " + mark, mark_key, limit=end)
        stages = tuple(route.get("stages") or ())
        if end - cursor > 55 and stages:
            stage_name = str(stages[-1].get("name") or "·")
            stage_mark = {"done": "✓", "active": "●",
                          "failed": "✕"}.get(str(stages[-1].get("st") or ""), "·")
            cursor = grid.put(cursor, y, "  (" + stage_name + " " + stage_mark + ")", "dim",
                              limit=end)
        cursor = grid.put(cursor, y, "  ∥ frame 2-way", "dim", limit=end)
        if card:
            _put_route_card(grid, cursor, end, y, route)
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
    # The brackets (`` (`` … ``)``) are spent only when a leg precedes the stage rail.
    room = end - cursor - (3 if cursor > x else 0) - after_width
    if duplicate and room < _seg_width(current_stage) + 2:
        leg = _leg_items([legs[current]])[0][0]
        cursor = _put_segments(grid, cursor, y, ([((" › "), "dim")] if cursor > x else []) + leg, end)
    elif not same and room >= _seg_width(current_stage) + 2 and stages:
        stage_rail = _fold_items(_stage_items(stages, lit=bool(getattr(grid, "beat", 1))), room)
        if sum(_seg_width(text) for text, _key in stage_rail) > room:
            stage_rail = _minimal_stage_rail(stages, stage_index, room, lit=bool(getattr(grid, "beat", 1)))
        # The stage rail stands alone when no leg precedes it (``plan ✓ › execute ●``).
        opening = " › (" if duplicate and cursor > x else " (" if cursor > x else ""
        cursor = grid.put(cursor, y, opening, "dim", limit=end) if opening else cursor
        cursor = _put_segments(grid, cursor, y, stage_rail, end)
        if opening:
            cursor = grid.put(cursor, y, ")", "dim", limit=end)
    cursor = _put_segments(grid, cursor, y, after, end)
    if card:
        _put_route_card(grid, cursor, end, y, route)


def _put_route_card(grid, cursor, end, y, route):
    """How the route was chosen and what verified it, only where the row has room."""
    visibility = route.get("visibility")
    card = str(visibility.get("card") or "") if isinstance(visibility, Mapping) else ""
    if card and end - cursor >= _seg_width(card) + 2:
        grid.put(cursor, y, "  " + card, "dim", limit=end)


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


def _harness_cell_text(session):
    """The ``harness model effort`` text a session row draws, from its already-clean model."""
    return "".join(text for text, _key in model_cell_parts(
        session.get("harness"), session.get("model"), str(session.get("effort") or "")))


def _overview_harness_width(sessions):
    return max((_seg_width(_harness_cell_text(session)) for session in sessions), default=0)


def _overview_columns(use, harn_need=0, tail_need=0):
    """ID │ MODEL │ CONTEXT │ TITLE │ LAST INPUT … AGE — the session row, and the route row after it.

    MODEL keeps the observed display text (model and effort included) up to the room TITLE,
    CONTEXT and AGE leave; a long model is clipped inside its own cell. TITLE is the cell
    that gives way first."""
    narrow = use < 100
    ctx_w = CTX_NARROW_W if narrow else CTX_W
    h = IND + 3 + 6 + GAP
    title_floor = 12 if narrow else 24
    reserve = title_floor + ctx_w + AGE_W + 4 * GAP
    harn_w = min(max(13, harn_need), max(8, use - h - reserve))
    if use < 44:
        h = min(h, max(11, use - 27))
        harn_w = min(harn_w, max(8, use - h - ctx_w - AGE_W - 2 * GAP))
    ctx = h + harn_w + GAP
    title = ctx + ctx_w + GAP
    flex = use - title - AGE_W - 2 * GAP
    title_w = min(48, max(title_floor, int(flex * 0.45)))
    now = use - (title + title_w + GAP) - AGE_W - GAP >= 16
    if not now:
        title_w = use - title - AGE_W - GAP
    if title_w >= max(TITLE_MIN, tail_need):
        return {"h": h, "title": title, "title_w": title_w, "ctx": ctx, "gap": GAP, "bare": False,
                "harn_w": harn_w, "narrow": narrow, "now": title + title_w + GAP if now else None}
    # Too narrow for the full cells: the margins, the gauge (its value stays) and the model's
    # optional words (model, effort) give way before the title, which keeps what the row is
    # and a folded row's whole `route N`.
    gap = 1
    h = IND + 3 + 6 + gap
    harn_w = max(0, min(8, max(5, harn_need), use - h - 4 - AGE_W - 3 * gap - max(TITLE_MIN, tail_need)))
    if harn_w < 5:
        harn_w = 0                # not even a harness word fits: MODEL is left out
    ctx = h + harn_w + (gap if harn_w else 0)
    title = ctx + 4 + gap
    title_w = use - title - gap - AGE_W
    if title_w < 4:
        title_w = 0               # no honest room for a title: the column is left out
    return {"h": h, "title": title, "title_w": title_w, "ctx": ctx, "gap": gap, "bare": True,
            "harn_w": harn_w, "narrow": True, "now": None,
            "ctx_w": 4 if ctx + 4 + gap <= use - AGE_W else 0}


def _overview_harness_end(use, c):
    """Column where the HARNESS cell must stop (before AGE), or None when under 5 cells remain."""
    if c.get("harn_w") == 0:
        return None
    end = use - AGE_W - c.get("gap", GAP)
    return end if end - c["h"] >= 5 else None


def _clip(text, width):
    from .grid import clip_cells
    return clip_cells(text, int(width))


def _clip_cut(text, width):
    """Cut to ``width`` cells with ``…`` even when the text exactly fills the cell."""
    from .grid import cell_width
    text, width = str(text), max(1, int(width))
    out, used = [], 0
    for char in text:
        cells = cell_width(char)
        if used + cells > width - 1:
            break
        out.append(char)
        used += cells
    return "".join(out) + "…"


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
    narrow = use < 150
    cols = _route_columns(use, _route_harness_width(routes, narrow),
                          _route_age_width(routes) if narrow else AGE_W)
    slim = cols["slim"]
    gap = cols["gap"]
    # One route on one line in the session row's columns (ID │ MODEL │ CONTEXT │ TITLE), then
    # its stages, open tool and elapsed time. The worker and context lines under it, and the
    # route's detail, unfold in place on a press, as the route detail always did.
    row_cols = route_row_columns(use, routes)
    row_title_w = route_title_width(routes, row_cols, use=use)
    grid.fill(x, right, y, " ", tint=234)
    headers = [(IND + 3, "ID"), (row_cols["h"], "MODEL"),
               (row_cols["ctx"], "CTX" if row_cols["bare"] else "CONTEXT")]
    if row_cols["title_w"]:
        headers.append((row_cols["title"], "ROUTE"))
        headers.append((row_cols["title"] + row_title_w + GAP, "PROGRESS"))
    headers.append((use - 2, "AGE"))
    for index, (col, name) in enumerate(headers):
        # a label that would run into the next label is left out, never overlapped
        next_col = headers[index + 1][0] if index + 1 < len(headers) else use + 1
        edge = right if name == "AGE" else min(x + next_col - 1, x + use - 4)
        if x + col + len(name) <= edge:
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
            end = grid.put(x + 3, y, _clip(str(repo) + "/", max(1, use - 6)),
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
            grid.add_hit(y, x, right, action)
            if is_open:
                grid.fill(x - 1, right, y, " ", tint=236)
                grid.put(x, y, "▶", "soft")
            from .pages import route_band_shows_title
            dropped = draw_route_row(
                grid, x, use, y, route, row_cols, title_w=row_title_w,
                # The detail band follows an unfolded row here and on the route page alike.
                title_below=not is_open or route_band_shows_title(grid, vm, route, x, use))
            age = _clip(str(route.get("age") or ""), cols["age_w"])
            age_edge = x + use - _seg_width(age)                              # content stops where AGE starts
            owner_edge = (x + cols["own"] + cols["own_w"] if cols["tiny"] else
                          x + (cols["ctx"] if not slim else cols["prog"]) - gap)
            owner_limit = min(owner_edge, age_edge)
            if history and not is_open:
                _recede_row(grid, y, x, right)     # finished history recedes; an opened row keeps its ink
            if is_open:
                _tint_row(grid, y, x - 1, right, 236)
            y += 1
            if not is_open:
                continue
            kind_parts = [(str(route.get("capability") or "·"), "soft")]
            short = {"standard": "std", "light": "lt", "quick": "q", "thorough": "thr",
                     "adversarial": "adv"}
            suffix = "·".join(short.get(value, value) for value in
                              (route.get("mode"), route.get("intensity")) if value)
            if suffix:
                kind_parts.append(("(" + suffix + ")", "dim"))
            def next_row(row):
                """Close this unfolded row and open the next one under it."""
                _tint_row(grid, row, x - 1, right, 236)
                grid.fill(x - 1, right, row + 1, " ", tint=236)
                grid.add_hit(row + 1, x, right, action)
                return row + 1

            grid.fill(x - 1, right, y, " ", tint=236)
            grid.add_hit(y, x, right, action)
            cursor = grid.put(x + cols["title"], y, "└ kind ", "dim", limit=right)
            # The unfolded owner is whole (harness, model, effort, profile), wrapped if need be.
            cursor, y = _put_wrapped(grid, cursor, y, kind_parts + [("  owner ", "dim")] + _owner_parts(route),
                                     right, x + cols["title"] + 2, next_row)
            # The folded row shows the live doer's context; the owner's own stays here (on a
            # line of its own when the kind line has no room left for it).
            owner_context = right - cursor - 2 >= _owner_context_width(route)
            if owner_context:
                _draw_owner_context(grid, cursor + 2, y, route, right)
            _tint_row(grid, y, x - 1, right, 236)
            y += 1
            extra = [] if owner_context or not _owner_context_width(route) else [None]
            # What the folded row left out or cut (title, MODEL, context, open tool, sub-agents),
            # whole; a cut title the detail band below carries whole is not repeated.
            extra.extend(dropped)
            line_x = None
            for item in extra:
                width = (_owner_context_width(route) if item is None
                         else sum(_seg_width(text) for text, _key in item))
                if line_x is None:
                    grid.fill(x - 1, right, y, " ", tint=236)
                    grid.add_hit(y, x, right, action)
                    line_x = grid.put(x + cols["title"], y, "└ ", "dim", limit=right)
                elif line_x + 3 + width > right:
                    y = next_row(y)
                    line_x = grid.put(x + cols["title"], y, "  ", "dim", limit=right)
                else:
                    line_x = grid.put(line_x, y, " · ", "dim", limit=right)
                if item is None:
                    line_x = grid.put(line_x, y, "owner ", "dim", limit=right)
                    _draw_owner_context(grid, line_x, y, route, right)
                    line_x += _owner_context_width(route)
                    continue
                # A value longer than the row goes on under it, whole.
                line_x, y = _put_wrapped(grid, line_x, y, item, right, x + cols["title"] + 2, next_row)
            if line_x is not None:
                _tint_row(grid, y, x - 1, right, 236)
                y += 1
            # The doer's own now (whole, wrapped like the values above) and the call it
            # just finished (one row, cut with `…`), each on its own row.
            for item in route_activity_rows(route):
                grid.fill(x - 1, right, y, " ", tint=236)
                grid.add_hit(y, x, right, action)
                line_x = grid.put(x + cols["title"], y, "└ ", "dim", limit=right)
                if item[0][0] == "after ":
                    grid.put(line_x, y, _clip("".join(text for text, _key in item),
                                              max(0, right - line_x)), "dim", limit=right)
                else:
                    line_x, y = _put_wrapped(grid, line_x, y, item, right, x + cols["title"] + 2,
                                             next_row)
                _tint_row(grid, y, x - 1, right, 236)
                y += 1
            if link:
                grid.add_hit(y, x + use - 7, right, ("route", ident))
            grid.add_hit(y, x, right, action)
            grid.fill(x - 1, right, y, " ", tint=236)
            worker = route.get("worker") or ()
            worker_contexts = tuple(route.get("worker_contexts") or ())
            context_index = route.get("worker_context_index")
            body_context = (worker_contexts[context_index]
                            if worker and worker[0] != "resource" and type(context_index) is int
                            and 0 <= context_index < len(worker_contexts) else None)
            context_text = ("CONTEXT ·" if body_context.get("context") is None else
                            f"CONTEXT {body_context['context']}%") if body_context is not None else ""
            progress_end = x + use - (9 if link else 0)
            worker_owner_limit = min(owner_limit, x + use - 6 - gap) if link else owner_limit
            context_x = progress_end - _seg_width(context_text)
            inline_context = body_context is not None and (
                not slim or context_x - (x + cols["prog"]) >= 10)
            worker_progress_end = context_x - gap if inline_context and slim else progress_end
            label = str(worker[0]) if worker else "worker"
            unit = route.get("worker_unit")
            route_end = x + cols["kind"] - gap
            route_start = x + cols["title"]
            cursor = grid.put(route_start, y, "└ ", "dim", limit=route_end)
            cursor = grid.put(cursor, y, _clip(label, max(0, route_end - cursor)),
                              "title_dim", limit=route_end)
            if unit and not (label == "owner" and unit == "_kernel/owner"):
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
            if label != "resource" and segments:
                harness, model, effort = _model_cell_facts(segments)
                if harness:
                    # The session row's MODEL cell words; a narrow table keeps harness and effort.
                    segments = model_cell_parts(harness, None if cols["narrow"] else model, effort)
            for text, style in segments:
                if isinstance(style, str) and style.startswith("hb_"):
                    style = {"hb_claude": "h_claude", "hb_codex": "h_codex",
                             "hb_opencode": "version_release"}.get(style, style)
                elif style == "eff_medium":
                    style = "branch_s"
                if link and worker_owner_limit < owner_limit:
                    text = _clip(text, max(0, worker_owner_limit - cursor))
                grid.put(cursor, y, text, style, limit=worker_owner_limit)
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            cursor = x + cols["prog"]
            for text, style in (worker[2] if len(worker) > 2 else ()):
                if "running" in str(text):
                    # A clipped initial is neither a readable state nor a blink target.
                    if worker_progress_end - cursor < len("running"):
                        break
                    style = route.get("worker_stage_key") or (hue, 1, 0)
                    if worker_running and not getattr(grid, "beat", 1):
                        style = (hue, 0, 1)
                grid.put(cursor, y, text, style, limit=worker_progress_end)
                from .grid import cell_width
                cursor += sum(cell_width(char) for char in str(text))
            _put_route_card(grid, cursor, worker_progress_end, y, route)
            if inline_context:
                if slim:
                    grid.put(context_x, y, context_text, "dim", limit=progress_end)
                else:
                    _draw_context_gauge(grid, x + cols["ctx"], y,
                                        body_context.get("context"), body_context.get("window"))
            if link:
                grid.put(x + use - 6, y, "open ▸", "accent")
            _tint_row(grid, y, x - 1, right, 236)
            y += 1
            for index, worker_context in enumerate(worker_contexts):
                if body_context is not None and index == context_index and inline_context:
                    continue
                grid.add_hit(y, x, right, action)
                if body_context is not None and index == context_index:
                    # At the smallest widths keep the metric as a continuation,
                    # without presenting the same worker's identity a second time.
                    grid.put(route_start, y, context_text, "dim", limit=x + use)
                    y += 1
                    continue
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
                    # The header's own short word where ``CONTEXT 100%`` has no room.
                    word = "CONTEXT" if age_edge - (x + cols["prog"]) >= len("CONTEXT 100%") else "CTX"
                    grid.put(x + cols["prog"], y, f"{word} ·" if pct is None else f"{word} {pct}%",
                             "dim", limit=age_edge)
                _tint_row(grid, y, x - 1, right, 236)
                y += 1
            if is_open and y < bottom:
                from .pages import draw_route_detail
                if not getattr(state, "no_repo_group", False):
                    height = max(16, min(30, grid.rows - 6 - y))
                    draw_route_detail(grid, vm, route, x, use, y, state, bottom=y + height)
                    y += height + 1
    return y



# What each wait asks, in plain words, from its source alone: the shared cache is body-free and a
# list row never carries the question text (that is read only for the selected row).
_QUESTION_ABOUT = {"S1-approval": "permission approval", "S1-question": "question window answer",
                   "S3": "blocked work needs direction", "S4": "marked question"}
# One or two words for a selected request that is not waiting for a choice on this screen.
_QUESTION_STATE_WORD = {"loading": "loading…", "submitted": "sent", "received": "received",
                        "applied": "answered", "cancelled": "cancelled", "expired": "expired"}
_NATIVE_ONLY = "answer in its pane"


def question_about(question):
    """``frame plan review`` / ``blocked work needs direction`` …: a body-free one-line summary of a wait."""
    source, node = question.get("source"), question.get("node")
    if source == "S4" and question.get("kind") == "message":
        return "question answer"
    if source == "S2":
        if node == "frame-review":
            return "frame plan review"
        return f"{node} gate check" if node else "route human gate"
    return _QUESTION_ABOUT.get(source) or question.get("kind") or "·"


def _question_line(question):
    """The question cell: what the wait asks and its route, after why no session can take it."""
    parts = []
    owner_state = question.get("owner_state")
    if owner_state == "none":
        parts.append(("no session to take it · ", "lvl_y"))
    elif owner_state == "successor":
        parts.append(("taken over · ", "dim"))
    parts.append((str(question_about(question)), "soft"))
    if question.get("route_title") or question.get("route_id"):
        parts.append((" · " + str(question.get("route_title") or question.get("route_id")), "dim"))
    return parts


def _question_owner_note(question):
    """Who can still take an unfolded wait, when it is not simply its listed session."""
    owner_state, session_tag = question.get("owner_state"), question.get("session_tag")
    owner_sid = question.get("owner_sid")
    if owner_state == "successor":
        registered = question.get("registered")
        return (f"[{session_tag or '··'}] taken over"
                + (f" · originally {registered}" if registered else ""), "dim")
    if owner_state == "none":
        return ("original session ended" + (f" ({owner_sid})" if owner_sid else ""), "lvl_y")
    if "session_tag" in question and not session_tag:
        return ((f"session {owner_sid} · not in the Fleet list" if owner_sid
                 else "no record of who takes it"), "dim")
    return None


def _question_model(question):
    """``claude Fable 5.2 xhigh``: the session row's MODEL cell words (``model_cell_parts``)."""
    return model_cell_parts(question.get("h"), question.get("m"), str(question.get("e") or ""))


def _question_rule(grid, x, right, y, parts):
    """``── ◑답 대기 2 ──────`` (``answer_wait_word``): a titled separator line, no box."""
    if not parts:
        grid.put(x, y, "─" * max(0, right - x), "line", limit=right)
        return
    grid.put(x, y, "── ", "line")
    cursor = x + 3
    for text, key in parts:
        cursor = grid.put(cursor, y, text, key, limit=right)
    if parts:
        cursor = grid.put(cursor, y, " ", "branch_s", limit=right)
    grid.put(cursor, y, "─" * max(0, right - cursor), "line", limit=right)


def _question_text(spec, envelope):
    prompt = (spec.get("question") or spec.get("prompt") or spec.get("header")
              or spec.get("title") or spec.get("text") or envelope.get("question_id") or "")
    if hasattr(prompt, "get"):
        prompt = (prompt.get("text") or prompt.get("question") or prompt.get("gate")
                  or envelope.get("question_id") or "")
    return str(prompt or "")


def _question_choices(spec):
    """``(value, label)`` for each choice, in native order; a value is what is sent."""
    result = []
    choices = spec.get("choices", spec.get("options", ()))
    for choice in choices if isinstance(choices, (list, tuple)) else ():
        if isinstance(choice, Mapping):
            value = choice.get("value")
            if value is None:
                value = choice.get("label", choice.get("text"))
            label = choice.get("label") or choice.get("text") or value
        else:
            value = label = choice
        if isinstance(value, str) and value:
            result.append((value, label if isinstance(label, str) else value))
    return result


def _question_actions(grid, x, right, y, items):
    """``submit │ open pane``: one line of pressable words, each its own hit."""
    cursor = x
    for index, (text, key, action) in enumerate(items):
        if index:
            cursor = grid.put(cursor, y, " │ ", "dim", limit=right)
        start = cursor
        cursor = grid.put(cursor, y, text, key, limit=right)
        if action and cursor > start:
            grid.add_hit(y, start - (1 if index else 0), cursor + 1, action)
    return cursor


def _question_body(grid, question, private, state, x, right, bottom, y, open_item):
    """The unfolded wait in place: full question, choices, typed answer, then the action line."""
    indent = x + 4
    width = max(1, right - indent)

    def line(text, key="soft"):
        nonlocal y
        if y < bottom:
            grid.put(indent, y, text, key, limit=right)
            y += 1

    note = _question_owner_note(question)
    if private is None:
        if note:
            line(*note)
        if "session_tag" not in question and question.get("title") is not None:
            for value in (question.get("title"), question.get("now")):
                for wrapped in _wrap(str(value or ""), width) if value else ():
                    line(wrapped, "soft")
        items = [(_NATIVE_ONLY, "dim", None)]
        if open_item:
            items.append(open_item)
        if y < bottom:
            _question_actions(grid, indent, right, y, items)
            y += 1
        return y
    from .state import (question_action_supported, question_answer_supported,
                        question_choice_click_allowed, question_decisions,
                        question_draft_supported, question_free_text_available, question_items,
                        question_multiple_select, question_values_valid, _question_text_draft)
    detail = private.get("detail")
    envelope = detail if hasattr(detail, "get") else {}
    specs = question_items(envelope)
    kind = str(envelope.get("kind") or "").lower()
    approval = ("approval" in kind or "permission" in kind or
                any(word in kind for word in ("commandexecution", "filechange")))
    request_state = str(private.get("state") or envelope.get("state") or "unknown")
    request_state = request_state if envelope.get("state") == request_state else (
        "loading" if private.get("state") == "loading" else "unknown")
    private_detail = envelope.get("detail") if envelope.get("state") != "unknown" else None
    if approval or not specs:
        # What an approval asks for (or a request without a schema): its own words, line by line.
        lines = []
        if isinstance(private_detail, Mapping):
            for name, value in list(private_detail.items())[:12]:
                if str(name).replace("_", "").casefold() in {
                        "availabledecisions", "threadid", "turnid", "itemid", "requestid",
                        "sessionid", "questionid", "isblocking", "autoresolutionms", "questions",
                        "choices", "responsemode", "decision", "gate", "target"}:
                    continue
                if isinstance(value, (list, tuple, Mapping)):
                    import json
                    value = json.dumps(value if not isinstance(value, Mapping) else dict(value),
                                       ensure_ascii=False, sort_keys=isinstance(value, Mapping))
                if name in ("question", "prompt", "text"):
                    lines.extend(str(value).splitlines() or [""])
                else:
                    lines.append(f"{str(name).replace('_', ' ')}: {value}")
        elif isinstance(private_detail, (str, int, float)):
            lines.extend(str(private_detail).splitlines())
        for text in lines:
            for wrapped in _wrap(text, width):
                line(wrapped)
    can_draft = question_draft_supported(envelope) and request_state == "open"
    can_answer = question_answer_supported(envelope)
    draft = private.get("draft")
    answers = draft.get("answers", {}) if hasattr(draft, "get") else {}
    complete = True
    native_only = False
    for spec in () if approval else specs:
        qid = spec["id"]
        for text in _question_text(spec, envelope).splitlines() or [""]:
            for wrapped in _wrap(text, width):
                line(wrapped)
        values = answers.get(qid, ()) if hasattr(answers, "get") else ()
        values = tuple(values) if isinstance(values, (list, tuple)) else ()
        choices = _question_choices(spec)
        secret = bool(spec.get("is_secret") or spec.get("isSecret"))
        for number, (value, label) in enumerate(choices, 1):
            if y >= bottom:
                break
            chosen = value in values
            selectable = can_draft and question_choice_click_allowed(envelope, qid, value, values)
            mark = ("●" if chosen else "○") if (selectable or chosen) else " "
            cursor = grid.put(indent, y, mark, "hearting" if chosen else "dim", limit=right)
            cursor = grid.put(cursor + 1, y, f"{number}  ", "dim" if not selectable else "soft",
                              limit=right)
            cursor = grid.put(cursor, y, _clip(str(label), max(1, right - cursor)),
                              "soft" if selectable or chosen else "dim", limit=right)
            if spec.get("recommended") == number - 1:
                grid.put(cursor, y, " (recommended)", "dim", limit=right)
            if selectable:
                # The whole line answers: wherever on it the person presses.
                grid.add_hit(y, x, right, ("question.choice", qid, value))
            y += 1
        if not question_values_valid(envelope, qid, values):
            complete = False
        free = question_free_text_available(envelope, qid, values)
        if free and y < bottom:
            editing = private.get("editing") and (private.get("editing_question_id") or qid) == qid
            choice_values = {value for value, _label in choices}
            typed = (_question_text_draft(private, qid) if editing else
                     " · ".join(str(value) for value in values if value not in choice_values))
            if secret:
                typed = "•" * min(len(typed), 32)
            cursor = grid.put(indent, y, "> ", "hearting" if can_draft else "dim", limit=right)
            if typed:
                cursor = grid.put(cursor, y, _clip(typed, max(1, right - cursor - 1)), "soft",
                                  limit=right)
            elif not editing:
                cursor = grid.put(cursor, y, "…", "dim", limit=right)
            if editing:
                grid.put(cursor, y, "▏", "hearting", limit=right)
            if can_draft:
                grid.add_hit(y, x, right, ("question.edit", qid))
            y += 1
        elif not choices:
            native_only = True
    if note:
        line(*note)
    if y >= bottom:
        return y
    items = []
    if request_state == "open" and approval:
        decisions = question_decisions(envelope) if question_action_supported(envelope) else ()
        for decision in decisions:
            items.append((decision, "hearting" if decision != "defer" else "soft",
                          ("question.decision", decision)))
        if not decisions:
            items.append((_NATIVE_ONLY, "dim", None))
    elif request_state == "open" and (specs or can_answer) and can_answer and not native_only:
        ready = complete and not private.get("pending")
        items.append(("submit", "hearting" if ready else "dim",
                      ("question.submit",) if ready else None))
    elif request_state in _QUESTION_STATE_WORD:
        items.append((_QUESTION_STATE_WORD[request_state], "dim", None))
    else:
        items.append((_NATIVE_ONLY, "dim", None))
    if private.get("pending") and items and items[0][0] != "sending…":
        items[0] = ("sending…", "dim", None)
    if open_item:
        items.append(open_item)
    _question_actions(grid, indent, right, y, items)
    return y + 1


def draw_questions(grid, vm, state, x=34, y=3):
    """QUESTIONS: one line per wait; pressing it unfolds the question in place (C5)."""
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 5
    from .grid import put_fit
    from .state import older_waits_shown
    all_questions = tuple(vm.questions or ())
    current_questions = tuple(item for item in all_questions if not item.get("older"))
    older_questions = tuple(item for item in all_questions if item.get("older"))
    older_open = older_waits_shown(state)
    system = vm.system if hasattr(vm.system, "get") else {}
    waits = system.get("answer_waits") if hasattr(system, "get") else None
    if not hasattr(waits, "get"):
        waits = {"state": "complete", "total": len(current_questions), "older": len(older_questions)}
    known = waits.get("state") == "complete"
    total = waits.get("total") if known else None
    word = answer_wait_word()
    heading = [(word[:1], "lvl_y"),
               (word[1:] + " " + (str(total) if total is not None else "?"), "sect")]
    if not known:
        heading.append((" · observation " + str(waits.get("state")), "dim"))
    _question_rule(grid, x, right, y, heading)
    y += 1
    from .state import question_open_tag
    opened = question_open_tag(state)
    selected = state.opened_for("qsel:QUESTIONS", "")
    overlay = getattr(state, "question_overlay", None)
    overlay = overlay if hasattr(overlay, "get") else {}
    shown = current_questions + (older_questions if older_open else ())
    tag_w = max((_seg_width(_clip(str(q.get("session_tag") or ("··" if "session_tag" in q else
                                                                  q.get("tag") or "·")), 5))
                 for q in shown), default=2)
    tag_x, model_x = x + 2, x + 2 + tag_w + 4
    model_w = min(max((_seg_width("".join(t for t, _k in _question_model(q))) for q in shown),
                      default=0), max(8, use // 3))
    projects = {str(q.get("project")) for q in shown if q.get("project")}
    project_w = (min(max(_seg_width(p) for p in projects), 14) if len(projects) > 1 else 0)
    text_x = model_x + model_w + 3 + (project_w + 3 if project_w else 0)

    def row(question):
        nonlocal y
        tag = str(question.get("tag") or "·")
        shown_tag = (str(question.get("session_tag")) if question.get("session_tag")
                     else "··" if "session_tag" in question else tag)
        is_open = opened == tag
        ref = question.get("ref")
        bound = (isinstance(ref, Mapping)
                 and all(ref.get(key) for key in ("id", "harness", "session_id")))
        grid.add_hit(y, x - 1, right, ("question.open", tag, ref if bound else None))
        if is_open:
            grid.fill(x - 1, right, y, " ", tint=236)
        marker = str(question.get("marker") or "●")
        marker = "◌" if marker == "◌" else "◑"
        grid.put(x, y, marker, "dim" if marker == "◌" else "lvl_y")
        tag_text = _clip(shown_tag, 5)
        grid.put(tag_x, y, "[", "dim")
        grid.put(tag_x + 1, y, tag_text, "soft" if question.get("session_tag") or shown_tag == tag
                 else "dim", limit=tag_x + 1 + _seg_width(tag_text))
        grid.put(tag_x + 1 + _seg_width(tag_text), y, "]", "dim")
        _put_model_cell(grid, model_x, y, _question_model(question), model_x + model_w)
        cursor = model_x + model_w + 1
        grid.put(cursor, y, "│", "dim")
        if project_w:
            grid.put(cursor + 2, y, _clip(str(question.get("project") or ""), project_w), "soft")
            grid.put(cursor + 3 + project_w, y, "│", "dim")
        wait = str(question.get("wait") or "")
        wait_x = right - _seg_width(wait) - 1
        # An older wait keeps its age in sight (yellow); it is listed, never closed by age.
        grid.put(wait_x, y, wait, "lvl_y" if question.get("fold") == "older" else "dim")
        private = None
        detail = overlay.get("detail") if tag == selected else None
        current_ref, overlay_ref = question.get("ref"), overlay.get("ref")
        if (tag == selected and hasattr(current_ref, "get") and hasattr(overlay_ref, "get")
                and overlay_ref.get("id") == question.get("id")
                and all(current_ref.get(key) == overlay_ref.get(key)
                        for key in ("harness", "session_id", "request_id", "turn_id",
                                    "route_id", "question_id"))):
            private = overlay
        first = None
        if private is not None and hasattr(detail, "get") and detail.get("state") != "unknown":
            from .state import question_items
            specs = question_items(detail)
            first = (_question_text(specs[0], detail) if specs else
                     detail.get("detail") if isinstance(detail.get("detail"), str) else None)
            if isinstance(detail.get("detail"), Mapping) and not specs:
                first = detail["detail"].get("operation") or detail["detail"].get("question")
            first = str(first).strip().splitlines()[0] if first and str(first).strip() else None
        limit = max(text_x, wait_x - 2)
        if first:
            parts = [(first, "soft")]
        elif "session_tag" not in question and question.get("title") is not None:
            parts = [(str(question["title"]), "hb_" + str(question.get("h") or "·"))]
        else:
            parts = _question_line(question)
        put_fit(grid, text_x, y, parts, limit=limit)
        if is_open:
            _tint_row(grid, y, x - 1, right, 236)
        y += 1
        if not is_open:
            return
        pane = question.get("pane")
        legacy = "session_tag" not in question and question.get("title") is not None
        target = question.get("open_tag") or (tag if legacy else None)
        open_item = (("open pane", "hearting", ("pane", str(target), str(pane)))
                     if target and pane else None)
        y = _question_body(grid, question, private, state, x, right, bottom, y, open_item)

    for question in current_questions:
        if y >= bottom:
            grid.put(right - 13, bottom, "↓ more below", "dim", limit=right)
            return
        row(question)
    if older_questions:
        if y >= bottom:
            grid.put(right - 13, bottom, "↓ more below", "dim", limit=right)
            return
        note = ""
        ownerless = waits.get("ownerless")
        if isinstance(ownerless, int) and not isinstance(ownerless, bool) and ownerless > 0:
            note = f" (no session to take it {ownerless})"
        _question_rule(grid, x, right, y, [("older waits " + str(len(older_questions)), "sect"),
                                            (note, "dim"),
                                            ("  " + ("hide" if older_open else "show"), "hearting")])
        grid.add_hit(y, x - 1, right, ("question.older.toggle",))
        y += 1
        for question in older_questions if older_open else ():
            if y >= bottom:
                grid.put(right - 13, bottom, "↓ more below", "dim", limit=right)
                return
            row(question)
    if y >= bottom:
        return
    answered = tuple(vm.answered or ())
    listed = sum(len(rows) for _repo, rows in answered)
    total = (vm.system or {}).get("answered_total") if hasattr(vm.system, "get") else None
    heading = [("answered today " + str(listed), "sect")]
    if isinstance(total, int) and not isinstance(total, bool) and total != listed:
        heading.append((f" ({total} in all projects)", "dim"))
    _question_rule(grid, x, right, y, heading)
    y += 1
    several = len(answered) > 1
    text_at = x + 9 + (16 if several else 0)
    for repo, records in answered:
        for index, record in enumerate(records):
            if y >= bottom:
                return
            at, text = record[:2]
            ident = f"{repo}:{index}"
            lines = _wrap_cells(str(text), max(1, right - text_at))
            is_open = state.opened_for("aopen:" + state.view) == ident and len(lines) > 1
            if len(lines) > 1:
                grid.add_hit(y, x - 1, right, ("fold", "aopen:" + state.view, ident))
            grid.put(x, y, "✓", "done")
            grid.put(x + 2, y, str(at), "dim")
            if several:
                grid.put(x + 9, y, _clip(repo_label(repo), 14), "soft")
            first_row = y
            visible = lines if is_open else [_clip(str(text), max(0, right - text_at))]
            for text_line in visible:
                if y >= bottom:
                    return
                grid.put(text_at, y, text_line, "title_dim", limit=right)
                y += 1
            if is_open:
                _tint_row(grid, first_row, x - 1, right, 236)
    if y < bottom:
        _question_rule(grid, x, right, y, [])


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


def _route_harness_width(routes, narrow):
    """The widest ``harness effort`` text the ROUTES rows draw in their narrow form."""
    if not narrow:
        return 0
    return max((_seg_width(_harness_cell_text({"harness": route.get("harness") or "·",
                                               "effort": route.get("effort")}))
                for route in routes), default=0)


def _route_age_width(routes):
    """The widest AGE text the ROUTES rows draw, after the column's own clip."""
    return max((_seg_width(_clip(str(route.get("age") or ""), AGE_W + 1))
                for route in routes), default=AGE_W)


def _route_columns(use, own_need=0, age_need=AGE_W):
    if use < 60:
        return {"narrow": True, "slim": True, "tiny": True, "gap": 1, "age_w": AGE_W + 1,
                "st": 3, "title": 10, "title_w": 5, "kind": 16, "own": 25,
                "own_w": 10, "ctx": 35, "prog": 36}
    narrow, slim = use < 150, use < 120
    title, kind = (10, 12) if slim else (12, 15)
    ctx_w = 0 if slim else CTX_W
    gaps = 4 if slim else 5
    base_own = 10 if slim else 13
    if narrow:
        # HARNESS keeps the observed ``harness·effort`` text like OVERVIEW; the title
        # floor, the PROGRESS mark and the other columns bound it, and the end is
        # clipped with ``…`` first.
        title_floor, progress_floor = 8, 7              # 7 = the width of ``frame ✓``
        max_own = (use - title - kind - ctx_w - age_need - gaps * GAP
                   - title_floor - progress_floor)
        own_w = min(max_own, max(base_own, own_need))
    else:
        own_w = 26
    flex = use - title - kind - own_w - ctx_w - AGE_W - gaps * GAP
    title_w = min(48, max(8 if narrow else 24, int(flex * (.35 if narrow else .5))))
    kind_x = title + title_w + GAP
    own_x = kind_x + kind + GAP
    ctx_x = own_x + own_w + GAP
    prog_x = ctx_x + (ctx_w + GAP if ctx_w else 0)
    return {"narrow": narrow, "slim": slim, "st": 3, "title": title,
            "title_w": title_w, "kind": kind_x, "own": own_x, "own_w": own_w,
            "ctx": ctx_x, "prog": prog_x, "tiny": False, "gap": GAP, "age_w": AGE_W + 1}


def _owner_width(route):
    """Cells of ``_draw_owner``'s wide form (slim: no profile)."""
    return sum(_seg_width(text) for text, _key in model_cell_parts(
        str(route.get("harness") or "·"), route.get("model") or None, str(route.get("effort") or "")))


def route_owner_context(route):
    """``(pct, window, tokens)`` of the route's owner attempt: its gauge, else its exact token count."""
    gauge = route.get("owner_gauge")
    pct, window = (tuple(gauge) + (None, None))[:2] if isinstance(gauge, (list, tuple)) else (None, None)
    context = route.get("owner_context")
    tokens = None
    if pct is None and isinstance(context, (list, tuple)) and len(context) == 2:
        tokens, size = context
        if type(tokens) is int and type(size) is int and size > 0:
            pct, window, tokens = round(100 * tokens / size), size / 1_000_000, None
    return pct, window, tokens


def _owner_context_width(route):
    """Cells ``_draw_owner_context`` takes, or 0 when the owner has no context to show."""
    pct, _window, tokens = route_owner_context(route)
    if pct is not None:
        return len("context ") + CTX_W
    text = token_count_text(tokens)
    return len("context ") + len(text) if text else 0


def _draw_owner_context(grid, x, y, route, right):
    """``context ━━━──── 77%`` (or ``context 250K``) after the owner on an unfolded route."""
    pct, window, tokens = route_owner_context(route)
    if pct is None and token_count_text(tokens) is None:
        return
    x = grid.put(x, y, "context ", "dim", limit=right)
    if pct is None:
        grid.put(x, y, token_count_text(tokens), "dim", limit=right)
    elif right - x >= CTX_W:
        _draw_context_gauge(grid, x, y, pct, window)
    else:
        grid.put(x, y, f"{int(pct)}%", _context_key(int(pct)) if pct >= 50 else "dim", limit=right)


def _owner_parts(route):
    """The owner's whole MODEL words with any non-default profile, for an unfolded line."""
    parts = model_cell_parts(str(route.get("harness") or "·"), route.get("model") or None,
                             str(route.get("effort") or ""))
    profile = route.get("profile")
    if profile and profile != "deep":
        parts.append((" " + str(profile), "dim"))
    return parts


def _draw_owner(grid, x, y, route, narrow, slim, limit):
    harness = str(route.get("harness") or "·")
    model = route.get("model")
    effort = str(route.get("effort") or "")
    if narrow:
        parts = model_cell_parts(harness, None, effort)
        room = max(0, limit - x)
        if room and sum(_seg_width(text) for text, _style in parts) > room:
            # Keep the drawn prefix and make the cut visible: when a segment falls
            # outside the cell, the last kept cell is spent on `…` (open…, opencode…).
            kept, used = [], 0
            for text, style in parts:
                width = _seg_width(text)
                remaining = room - used
                if width <= remaining:
                    kept.append((text, style))
                    used += width
                    continue
                if remaining >= 1:
                    clipped = _clip(text, remaining)
                    if clipped:
                        kept.append((clipped, style))
                break
            if kept and not kept[-1][0].endswith("…"):
                text, style = kept[-1]
                kept[-1] = (_clip_cut(text, _seg_width(text)), style)
            parts = kept
        elif not room:
            parts = []
    else:
        # ``claude Opus 5.5 xhigh``: the session row's MODEL cell words.
        parts = model_cell_parts(harness, model or None, effort)
    profile = route.get("profile")
    if profile and profile != "deep" and not slim:
        parts.append((" " + str(profile), "dim"))
    cursor = x
    for text, style in parts:
        cursor = grid.put(cursor, y, text, style, limit=limit)
    return cursor
