"""Read-only model configuration view and viewer-local selection controls."""
from collections.abc import Mapping

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


def _dict(value):
    return value if isinstance(value, Mapping) else {}


def _profiles(data):
    return tuple(data.get("profiles", ()))


def _write(grid, x, y, text, key="dim", limit=None):
    return grid.put(x, y, str(text), key, limit=limit)


def _section(grid, x, width, y, title, meta=""):
    cursor = grid.put(x, y, "── ", "line")
    cursor = grid.put(cursor, y, title, "head")
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


def _model_cell(grid, x, y, harness, value, shown, effort_keys, room, limit=None):
    edge = x + room if limit is None else min(x + room, limit)
    if value is None:
        return _write(grid, x, y, "— not set", "dim", limit=edge)
    model, effort = (tuple(value) + (None, None))[:2]
    name = shown.get(str(model), model)
    cursor = _write(grid, x, y, clip_cells(str(name), room - 9), "hb_" + harness, limit=limit)
    cursor = _write(grid, cursor, y, " · ", "dim", limit=limit)
    return _write(grid, cursor, y, effort or "·", effort_keys.get(effort, "dim"), limit=edge)


def _draw_context(grid, data, state, x, y, width):
    right = min(grid.cols - 1, x + width)
    cursor = grid.put(x, y, "── ", "line")
    cursor = grid.put(cursor, y, "CONTEXT & TIDY", "head")
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
        chip_end = _chips(grid, bx, y, choices, "800k", selected, "ctx:" + harness, right - 1)
        source_text = source.get(harness, "·")
        if source_text:
            grid.put(chip_end + 1, y, clip_cells(source_text, x + width - chip_end - 2), "dim", limit=right)
        if selected:
            cmds.append(f"hearting model set {harness} context-window {units.get(selected, 0)}")
        if window is not None and t2 is not None and t2 >= window:
            warnings.append(harness)
        y += 2
    grid.put(x + 1, y, "tidy+clear notice points", "soft", limit=right)
    y += 1
    grid.put(x + 3, y, "notice", "label")
    _chips(grid, bx, y, ("300K", "400K", "500K", "600K"), "500K", chosen1, "tidy1", right - 1)
    y += 1
    grid.put(x + 3, y, "urgent", "label")
    _chips(grid, bx, y, ("600K", "650K", "700K", "750K"), "700K", chosen2, "tidy2", right - 1)
    y += 1
    grid.put(x + 3, y, "세 하네스 공통 · 설정 변경은 실행되지 않습니다", "dim", limit=right)
    y += 1
    if warnings:
        grid.put(x + 3, y, "tidy point meets or exceeds window: " + ", ".join(warnings), "g_blocked", limit=right)
        y += 1
    for command in cmds:
        cursor = grid.put(x + 3, y, "$ " + command, "soft", limit=right - 14)
        grid.put(cursor + 2, y, "[ apply ]", "chip_on", limit=right)
        grid.add_hit(y, cursor + 2, cursor + 11, ("ui", "apply", "적용: " + command + " (시안이라 실행 안 함)"))
        y += 1


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
    cursor = grid.put(x, y, "── ", "line")
    cursor = grid.put(cursor, y, "MODEL PROFILE", "head")
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
    for prof in profiles:
        grid.put(x + 3, y, prof, "soft" if prof != "top" else "dim")
        for h, col in columns.items():
            current = _dict(profile_map.get(h)).get(prof)
            hit_left, hit_right = x + col - 1, min(right, x + col + col_width)
            grid.add_hit(y, hit_left, hit_right, ("ui", "sel", f"{prof}/{h}"))
            if (prof, h) == (profile, harness):
                grid.fill(hit_left, min(right, hit_right + 1), y, " ", tint=236)
                grid.put(x + col - 2, y, "▶", "soft")
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
    y += 2
    current = _dict(profile_map.get(harness)).get(profile)
    model_choice = values.get("ui:model")
    effort_choice = values.get("ui:effort")
    current_model, current_effort = (tuple(current) + (None, None))[:2] if current else (None, None)
    model_choice = model_choice or current_model or next(iter(choices.get(harness, ())), None)
    effort_choice = effort_choice or current_effort or "high"
    changed = current is None or (model_choice, effort_choice) != (current_model, current_effort)
    box_top, box_bottom = y, y + 8
    box_left = x + 1
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
    _model_cell(grid, box_left + 12, y + 1, harness, current, shown, effort_keys, 40, limit=right)
    if changed:
        grid.put(box_left + 56, y + 1, "→", "line", limit=right)
        _model_cell(grid, box_left + 59, y + 1, harness, (model_choice, effort_choice), shown, effort_keys, 40,
                    limit=right)
    grid.put(box_left + 3, y + 3, "model", "label")
    _chips(grid, box_left + 12, y + 3, choices.get(harness, ()), current_model, values.get("ui:model"), "model", right - 2, shown)
    grid.put(box_left + 3, y + 4, "effort", "label")
    effort_options = tuple(data.get("efforts", ())) + (("runtime-default",) if harness == "opencode" else ())
    _chips(grid, box_left + 12, y + 4, effort_options, current_effort, values.get("ui:effort"), "effort", right - 2)
    command = f"hearting model set {harness} profile/{profile} {model_choice}@{effort_choice}"
    grid.put(box_left + 3, y + 6, "$ ", "line")
    edge = right                   # the box's right border is column ``right``: nothing crosses it
    grid.put(box_left + 5, y + 6, command, "soft" if changed else "dim", limit=edge)
    if changed:
        # the two buttons follow the command on its row; the box's border cuts what runs past it
        dry_x = box_left + 5 + cell_width(command) + 3
        grid.put(dry_x, y + 6, "[ dry-run ]", "title_dim", limit=edge)
        grid.add_hit(y + 6, dry_x, dry_x + 11, ("ui", "apply", "dry-run: " + command + " --dry-run (시안이라 실행 안 함)"))
        apply_x = dry_x + 11 + 2
        grid.put(apply_x, y + 6, "[ apply ]", "chip_on", limit=edge)
        grid.add_hit(y + 6, apply_x, apply_x + 9, ("ui", "apply", "적용: " + command + " (시안이라 실행 안 함)"))
    y += 10
    if slim:
        grid.put(box_left + 3, box_top + 7, "pool", "label")
        pool = pools.get(profile)
        if pool:
            order, note = pool
            pool_order = order[0] if order and isinstance(order[0], (list, tuple)) else order
            cursor = grid.put(box_left + 12, box_top + 7, " › ".join(pool_order), "soft", limit=right - 2)
            if note:
                grid.put(cursor, box_top + 7, "  " + note, "dim", limit=right - 2)
        else:
            grid.put(box_left + 12, box_top + 7, "route-sealed explicit choice only", "dim", limit=right - 2)
    foot = "models.conf → .bak backup · new sessions and launches only · sealed routes keep their choice"
    grid.put(box_left + 2, box_bottom, " ", "branch_s")
    grid.put(box_left + 3, box_bottom, foot, "dim", limit=right)
    grid.put(min(right - 1, x + 96), box_bottom, " ", "branch_s")
    beside = grid.cols - x - 2 - left > 70
    if beside:
        _draw_context(grid, data, state, x + left + GAP + 2, 3, grid.cols - (x + left + GAP + 4))
    allocation = _dict(data.get("allocation"))
    _section(grid, x, left, y, "DISPATCH ALLOCATION", "~/.config/hearting/dispatch-defaults.yaml")
    y += 1
    fields = allocation.get("fields", ())
    for label, fragments in fields:
        grid.put(x + 3, y, label, "dim")
        cursor = x + 20
        for fragment, key in fragments:
            cursor = grid.put(cursor, y, _cut(fragment, grid.cols - cursor),
                              "branch_s" if fragment.isspace() else key)
        y += 1
    if allocation:
        grid.put(x + 3, y, allocation.get("note", "·"), "dim", limit=right + 1)
    if not beside:
        _draw_context(grid, data, state, x, y + 2, min(grid.cols - x - 2, 100))
