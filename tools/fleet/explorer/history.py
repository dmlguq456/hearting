"""Seat and generation list for the HISTORY view."""

from collections.abc import Mapping
from .grid import cell_width
from .tables import _clip, _tint_row, repo_rule_label


def _hard_cut(text, room):
    """Cut at a display-cell boundary without adding a marker."""
    out, used = [], 0
    for char in str(text):
        width = cell_width(char)
        if used + width > max(0, int(room)):
            break
        out.append(char)
        used += width
    return "".join(out)


def draw_history(grid, vm, state, x=34, y=3):
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 6
    grid.fill(x, right, y, " ", tint=234)
    seats = tuple(vm.seats or ())
    opened = state.opened_for("hopen:" + state.view)
    c_id = 18
    c_h, c_gen = c_id + 4 + 3, c_id + 4 + 3 + 8 + 3
    gen_width = max(8, min(24, max((len((vm.generations or {}).get(str(seat[0]), ())) for seat in seats),
                                   default=0), use - c_gen - 3 - 3 - 14 - 3 - 16))
    c_last = c_gen + 3 + gen_width + 3
    c_card = c_last + 14 + 3
    for off, label in ((2, "REPO"), (c_id, "ID"), (c_h, "HARNESS"), (c_gen, "GENERATIONS"),
                       (c_last, "LAST CLEAR"), (c_card, "LAST CARD")):
        grid.put(x + off, y, label, "head", tint=234, limit=right)
    y += 1
    previous_repo = None
    for seat in seats:
        if y > bottom:
            break
        tag = str(seat[0])
        repo = str(seat[1])
        generation_count = int(seat[2]) if len(seat) > 2 else 0
        last_clear = str(seat[3]) if len(seat) > 3 else "·"
        cards = tuple((vm.cards or {}).get(tag, {}).get("cards", ()))
        title = str(seat[5] or "·") if len(seat) > 5 else "·"
        is_open = opened == tag
        grid.add_hit(y, x, right, ("fold", "hopen:" + state.view, tag))
        if is_open:
            grid.fill(x - 1, right, y, " ", tint=236)
            grid.put(x, y, "▶", "soft")
        if repo != previous_repo:
            grid.put(x + 2, y, _hard_cut(repo_rule_label(repo, 13), right - x - 2),
                     "title_dim", limit=right)
            previous_repo = repo
        session = next((item for item in vm.sessions if item.get("tag") == tag), {})
        harness = str(session.get("harness") or "·")
        tag_style = "steward" if session.get("steward") else "soft"
        grid.put(x + c_id, y, "[", "dim")
        shown_tag = _hard_cut(tag, right - x - c_id - 1)
        grid.put(x + c_id + 1, y, shown_tag, tag_style, limit=right)
        grid.put(x + c_id + 1 + cell_width(shown_tag), y, "]", "dim", limit=right)
        grid.put(x + c_h, y, _hard_cut(harness, right - x - c_h), "hb_" + harness, limit=right)
        grid.put(x + c_gen, y, f"{generation_count:2d} ", "soft")
        root_marks = tuple(seat[6]) if len(seat) > 6 else tuple((vm.generations or {}).get(tag, ()))
        _gen_strip(grid, x + c_gen + 3, y, root_marks, gen_width, harness)
        grid.put(x + c_last, y, _hard_cut(last_clear, right - x - c_last), "dim", limit=right)
        grid.put(x + c_card, y, _clip(title, use - c_card) if title != "·" else "·",
                 "soft" if title != "·" else "dim", limit=right)
        if is_open:
            _tint_row(grid, y, x - 1, right, 236)
        y += 1
        if is_open:
            y = draw_seat_expand(grid, vm, state, tag, x, use, y, bottom, seat, harness) + 1
    if y + 3 <= bottom:
        footer_end = x + use
        cursor = grid.put(x + 2, y + 1, "●", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " new session after clear   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "○", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " same session after compact   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "◆", "soft", limit=footer_end)
        cursor = grid.put(cursor, y + 1, " a running session took over (retire)   ", "dim", limit=footer_end)
        cursor = grid.put(cursor, y + 1, "colour", "soft", limit=footer_end)
        grid.put(cursor, y + 1, " = harness   ·   click a row: its generations here", "dim", limit=footer_end)


def _gen_strip(grid, x, y, marks, room, harness):
    """One mark per generation, newest bold; a seat with more generations than room leads with ``…``."""
    shown = marks
    if len(marks) > room:
        x = grid.put(x, y, "…", "dim")
        shown = marks[-(room - 1):]
    for index, generation in enumerate(shown):
        if isinstance(generation, Mapping):
            gen_harness = str(generation.get("harness") or harness)
            kind = str(generation.get("kind") or "new")
        else:
            details = generation if isinstance(generation, (list, tuple)) else (generation,)
            gen_harness = str(details[0]) if details else harness
            kind = str(details[1]) if len(details) > 1 else "new"
        glyph = {"new": "●", "cont": "○", "existing": "◆"}.get(kind, "●")
        hue = {"claude": "c", "codex": "m", "opencode": "l"}.get(gen_harness, "d")
        x = grid.put(x, y, glyph, (hue, int(index == len(shown) - 1), 0))
    return x


def draw_seat_expand(grid, vm, state, tag, x, use, y, bottom, seat, harness):
    """A seat's generations newest first, then the picked card and conversation; returns the next row."""
    x0 = x + 6
    generations = tuple((vm.generations or {}).get(tag, ()))
    has_history_detail = any(isinstance(item, Mapping) and item.get("detail") for item in generations)
    if not generations or not has_history_detail:
        count = int(seat[2]) if len(seat) > 2 else 0
        last_clear = str(seat[3] if len(seat) > 3 else "·").split(" · ")[0]
        cursor = grid.put(x0, y, f"{count} generations", "soft", limit=x + use + 1)
        usage = (vm.system or {}).get("usage_api") if isinstance(vm.system, Mapping) else None
        stamp = usage.get("freshness") if isinstance(usage, Mapping) else None
        note = f"{stamp} 전에 남은 카드 본문이 없습니다." if stamp else "읽은 카드 본문이 없습니다."
        limit = x + use + 1
        line = f"  · last clear {last_clear}   {note}"
        grid.put(cursor, y, _hard_cut(line, limit - cursor), "dim", limit=limit)
        return y + 1
    right = x + use + 1
    selected_generation = state.opened_for("ui:gen:" + tag)
    card_indices = [i for i, item in enumerate(generations)
                    if isinstance(item, Mapping) and item.get("card")]
    default_generation = card_indices[-1] if card_indices else len(generations) - 1
    selected = default_generation
    if selected_generation.isdigit() and 0 <= int(selected_generation) < len(generations):
        selected = int(selected_generation)
    for index in range(len(generations) - 1, -1, -1):
        if y > bottom:
            return y
        gen = generations[index]
        grid.add_hit(y, x0, right, ("ui", "gen:" + tag, index))
        if index == selected:
            grid.fill(x0, right, y, " ", tint=234)
            grid.put(x0 - 2, y, "▶", "soft")
        h = str(gen.get("harness") or harness) if isinstance(gen, Mapping) else harness
        at = str(gen.get("at") or "") if isinstance(gen, Mapping) else ""
        kind = str(gen.get("kind") or "new") if isinstance(gen, Mapping) else "new"
        sid = str(gen.get("sid") or "") if isinstance(gen, Mapping) else ""
        connection = str(gen.get("connection") or "seat start") if isinstance(gen, Mapping) else "seat start"
        title = str(gen.get("title") or "") if isinstance(gen, Mapping) else ""
        if not title:
            title = ("from another seat" if isinstance(gen, Mapping) and gen.get("note") else
                     "no card" if at != "now" else "current generation · no card yet")
        grid.put(x0, y, "now" if at == "now" else f"{index + 1:3d}", "soft")
        grid.put(x0 + 6, y, _hard_cut(at if at != "now" else "", right - x0 - 6), "dim", limit=right)
        hue = {"claude": "c", "codex": "m", "opencode": "l"}.get(h, "d")
        grid.put(x0 + 20, y, {"new": "●", "cont": "○", "existing": "◆"}.get(kind, "●"), (hue, 1, 0))
        cursor = grid.put(x0 + 22, y, h, "hb_" + h)
        grid.put(cursor + 1, y, _hard_cut(sid, right - cursor - 1), "dim", limit=right)
        grid.put(x0 + 42, y, _clip(connection, 30), "blocked" if kind == "existing" else "dim", limit=right)
        grid.put(x0 + 75, y, _clip(title, use - 75 - 6),
                 "soft" if isinstance(gen, Mapping) and gen.get("card") else "dim", limit=right)
        if index == selected:
            _tint_row(grid, y, x0 - 1, right, 234)
        y += 1
    if bottom - y < 4:
        grid.put(x0, min(y, bottom), "↓ card · conversation need a taller pane", "dim")
        return y + 1
    from .pages import draw_history_detail
    return draw_history_detail(grid, vm, state, tag, x0, right, y, bottom)
