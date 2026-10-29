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
        mark = "▸ " if editing and index == cursor else "  "
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
        model_key = render._model_key(model)
    except Exception:
        model_key = "fam_other"
    cursor = _write(grid, x, y, clip_cells(str(name), room - 9), model_key, limit=limit)
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
    grid.put(x + 3, y, "세 하네스 공통 · 설정은 Apply로 변경", "dim", limit=right)
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


_EDITED_FIELDS = ("strategy:", "weights:", "affinity:")   # read-only lines the editor replaces
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
                   + ("" if holder.get("counted", True) else " (ended)")
                   for holder in repo.get("holders") or () if isinstance(holder, Mapping)]
        text = (f"{repo.get('repository') or '?'} · {repo.get('counted', 0)} / {shown} supervising"
                + (" · " + ", ".join(holders) if holders else ""))
        over = bool(repo.get("over_limit"))
        grid.put(x + 20, y, text + (" · over limit" if over else ""), "g_blocked" if over else "soft",
                 limit=right)
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
             "max_supervisors_per_repo": "sessions that may hold the supervisor flag in one repo"}
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
    changed = current is None or not (_same_model(model_choice, current_model)
                                      and effort_choice == current_effort)
    harness_choices = tuple(choices.get(harness, ()))
    picking = not _chips_fit(harness_choices, shown, right - 2 - (x + 13))
    extra = PICK_ROWS if picking else 0
    box_top, box_bottom = y, y + 8 + extra
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
    catalog = _dict(_dict(data.get("catalog_sources")).get(harness))
    missing_current = (current_model and catalog.get("state") in ("available", "empty")
                       and not any(_same_model(item, current_model) for item in harness_choices))
    if changed:
        grid.put(box_left + 56, y + 1, "→", "line", limit=right)
        _model_cell(grid, box_left + 59, y + 1, harness, (model_choice, effort_choice), shown, effort_keys, 40,
                    limit=right)
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
    effort_options = tuple(data.get("efforts", ())) + (("runtime-default",) if harness == "opencode" else ())
    _chips(grid, box_left + 12, y + 4 + extra, effort_options, current_effort, values.get("ui:effort"), "effort",
           right - 2)
    command = f"hearting model set {harness} profile/{profile} {model_choice}@{effort_choice}"
    command_y = y + 6 + extra
    grid.put(box_left + 3, command_y, "$ ", "line")
    edge = right                   # the box's right border is column ``right``: nothing crosses it
    # the two buttons follow the command on its row and stay inside the box; a long model id
    # gives up the command's tail, never the buttons
    dry_x = min(box_left + 5 + cell_width(command) + 3, right - 25)
    grid.put(box_left + 5, command_y, command, "soft" if changed else "dim",
             limit=dry_x - 2 if changed else edge)
    if changed:
        grid.put(dry_x, command_y, "[ dry-run ]", "title_dim", limit=edge)
        grid.add_hit(command_y, dry_x, dry_x + 11,
                     ("models.apply", harness, "profile", profile, model_choice,
                      effort_choice, True))
        apply_x = dry_x + 11 + 2
        grid.put(apply_x, command_y, "[ apply ]", "chip_on", limit=edge)
        grid.add_hit(command_y, apply_x, apply_x + 9,
                     ("models.apply", harness, "profile", profile, model_choice,
                      effort_choice, False))
    y += 10 + extra
    if slim:
        grid.put(box_left + 3, box_top + 7 + extra, "pool", "label")
        pool = pools.get(profile)
        if pool:
            order, note = pool
            pool_order = order[0] if order and isinstance(order[0], (list, tuple)) else order
            cursor = grid.put(box_left + 12, box_top + 7 + extra, " › ".join(pool_order), "soft",
                              limit=right - 2)
            if note:
                grid.put(cursor, box_top + 7 + extra, "  " + note, "dim", limit=right - 2)
        else:
            grid.put(box_left + 12, box_top + 7 + extra, "route-sealed explicit choice only", "dim", limit=right - 2)
    foot = "models.conf → .bak backup · new sessions and launches only · sealed routes keep their choice"
    grid.put(box_left + 2, box_bottom, " ", "branch_s")
    grid.put(box_left + 3, box_bottom, foot, "dim", limit=right)
    grid.put(min(right - 1, x + 96), box_bottom, " ", "branch_s")
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
    for label, fragments in fields:
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
