"""Peer graph and filtered LINKS list."""

import re

from . import tables
from .output import cell_width
from .tables import _clip, repo_rule_label
from .style import steward_flag_key

STAGE_HUES = ("l", "c", "g", "y", "m")          # blue · cyan · green · yellow · magenta
LANE_HEAD_LEFT, LANE_HEAD_RIGHT = "<", ">"
STATE_MARKS = {"working": ("⠏", "spin"), "active": ("⠏", "spin"), "idle": ("●", "idle"),
               "blocked": ("●", "blocked"), "failed": ("✕", "dead"), "done": ("✓", "done"),
               "dead": ("✕", "dead")}


def _extra_lines(session, questions):
    """Lines under a card's gauge, from the session record: held GPUs, an open question."""
    tag = str(session.get("tag"))
    lines = []
    gpus = session.get("gpu") or ()
    if len(gpus) > 1:
        lines.append((f"GPU ×{len(gpus)}", "dim"))
    question = next((item for item in questions if str(item.get("tag")) == tag), None)
    if question is not None or session.get("ask"):
        wait = str((question or {}).get("wait") or "")
        lines.append(("answer needed" + (f" · {wait}" if wait else ""), "blocked"))
    return lines


def _card_height(owners, extra):
    return 3 + len(extra) + (1 + len(owners) if owners else 0)


def _stage_mark(index, state, lit=True):
    hue = STAGE_HUES[index % len(STAGE_HUES)]
    if state == "done":
        return "✓", (hue, 0, 1)
    if state == "active":
        return "●", (hue, 1, 0) if lit else (hue, 0, 1)
    if state == "failed":
        return "✕", "lvl_r"
    return "·", (hue, 0, 1)


def _percent_key(percent):
    return "lvl_r" if percent >= 80 else "lvl_y" if percent >= 50 else "lvl_g"


def _draw_card(grid, session, tag, bx, by, box_w, right, border, extra, owners):
    """One session card: header in the border, title, context gauge, extra lines, owners."""
    def put(px, py, text, key=None):
        return grid.put(px, py, text, key, limit=right)

    br = bx + box_w - 1
    card_h = _card_height(owners, extra)
    for xx in range(bx + 1, br):
        put(xx, by, "─", border)
        put(xx, by + card_h, "─", border)
    for yy in range(by + 1, by + card_h):
        put(bx, yy, "│", border)
        put(br, yy, "│", border)
    for xx, yy, glyph in ((bx, by, "┌"), (br, by, "┐"), (bx, by + card_h, "└"), (br, by + card_h, "┘")):
        put(xx, yy, glyph, border)
    harness = str(session.get("harness") or "other")
    glyph, glyph_key = STATE_MARKS.get(str(session.get("state") or ""), ("·", "dim"))
    steward = bool(session.get("steward"))
    xx = put(bx + 2, by, " ", "branch_s")
    xx = put(xx, by, glyph, glyph_key)
    xx = put(xx, by, " ", "branch_s")
    xx = put(xx, by, "[", "tag_b")
    xx = put(xx, by, tag, "steward" if steward else "text")
    xx = put(xx, by, "]", "tag_b")
    if steward:
        xx = put(xx + 1, by, "⚑", steward_flag_key(session, lit=bool(getattr(grid, "beat", 1))))
    xx = put(xx, by, " ", "branch_s")
    xx = put(xx, by, harness, "hb_" + harness)
    put(xx, by, " ", "branch_s")
    put(bx + 2, by + 1, _clip(str(session.get("title") or "·"), box_w - 4), "hb_" + harness)
    context = session.get("context")
    if context is not None:                      # an unknown context draws nothing, as OVERVIEW does
        gauge_width = max(1, int(round(8 * float(session.get("window") or 1.0))))
        percent = float(context)
        filled = (0 if percent <= 0 else gauge_width if percent >= 100
                  else max(1, min(gauge_width - 1, int(percent * gauge_width / 100 + .5))))
        put(bx + 2, by + 2, "━" * filled, _percent_key(percent))
        put(bx + 2 + filled, by + 2, "─" * (gauge_width - filled), "dim")
        put(bx + 11, by + 2, f"{int(context):3d}%", _percent_key(percent) if percent >= 50 else "dim")
    yy = by + 3
    for text, key in extra:
        put(bx + 2, yy, _clip(text, box_w - 4), key)
        yy += 1
    if owners:
        put(bx, yy, "├", border)
        for xx in range(bx + 1, br):
            put(xx, yy, "┈", border)
        put(br, yy, "┤", border)
        yy += 1
        for owner in owners:
            mark, mark_key = STATE_MARKS.get(str(owner.get("state") or ""), ("·", "dim"))
            stages = tuple(owner.get("stages", ()))
            strip_x = bx + box_w - 2 - len(stages) - 5
            put(bx + 2, yy, mark, mark_key)
            put(bx + 4, yy, _clip(str(owner.get("label") or ""), strip_x - bx - 5), "title_dim")
            xx = strip_x
            for index, stage in enumerate(stages):
                glyph, key = _stage_mark(index, str(stage.get("state") or ""),
                                        lit=bool(getattr(grid, "beat", 1)))
                xx = put(xx, yy, glyph, key)
            done = sum(str(stage.get("state")) == "done" for stage in stages)
            put(xx + 1, yy, f"{done}/{len(stages)}", "dim")
            if tables.route_is_history(owner):
                # A finished owner line recedes; the card border and its live neighbours do not.
                tables._recede_row(grid, yy, bx + 2, br)
            yy += 1


def _draw_frame(grid, x0, y0, x1, y1, title, right):
    def put(px, py, text, key="dim"):
        grid.put(px, py, text, key, limit=right)

    for xx in range(x0 + 1, x1):
        put(xx, y0, "═")
        put(xx, y1, "═")
    for yy in range(y0 + 1, y1):
        put(x0, yy, "║")
        put(x1, yy, "║")
    for xx, yy, glyph in ((x0, y0, "╔"), (x1, y0, "╗"), (x0, y1, "╚"), (x1, y1, "╝")):
        put(xx, yy, glyph)
    put(x0 + 2, y0, " " + title + " ", "soft")


def _segments(grid, x, y, segments, right):
    for text, key in segments:
        x = grid.put(x, y, text, key, limit=right)
    return x


def _link_when(links, upper, lower):
    """``(kind, when)`` of the newest link sent from ``upper`` to ``lower``; None without one."""
    for link in links:
        frm, to = link[0], link[1]
        if (isinstance(frm, (list, tuple)) and isinstance(to, (list, tuple)) and frm and to
                and str(frm[0]) == upper and str(to[0]) == lower):
            return str(link[2]), str(link[3] if len(link) > 3 else "").removesuffix(" ago")
    return None


def _clear_text(seat):
    """The recent-clear label a seat puts on its card's bottom border, if it has one."""
    recent = re.search(r"· (\S+) ago$", str(seat[3])) if len(seat) > 3 else None
    if recent and str(seat[2]).isdigit():
        return f"◂ gen {int(seat[2]) - 1} · {recent.group(1)}"
    return None


def _lane_cells(edges, boxes, right):
    """Screen cells a side lane and its two labels occupy, from the same geometry it draws with."""
    cells = set()
    links = tuple(edges.get("links", ()))
    for lane in edges.get("side_lanes", ()):
        if len(lane) < 2:
            continue
        upper, lower = str(lane[0]), str(lane[1])
        top, bottom = boxes.get(upper), boxes.get(lower)
        if (not top or not bottom or top[4] != bottom[4] or top[2] != bottom[2]
                or top[3] >= bottom[1]):
            continue
        ux, uy, edge, ub = top[0], top[1], top[2], top[3]
        fy = bottom[1]
        for yy in range(uy + 1, fy + 3):
            for xx in range(edge, edge + 6):
                cells.add((xx, yy))
        label_right = min(right, top[2])
        for row, (src, dst) in enumerate(((upper, lower), (lower, upper))):
            sent = _link_when(links, src, dst)
            if sent:
                text = f"[{src}]→[{dst}] {sent[0]} {sent[1]}"
                for xx in range(ux + 2, min(label_right, ux + 2 + cell_width(text))):
                    cells.add((xx, ub + 1 + row))
    return cells


def _cross_occupancy(boxes, frames, lane_cells):
    """Cells a cross line may not paint over: every card, every frame border, every side lane."""
    cells = set(lane_cells)
    for box in boxes.values():
        for yy in range(box[1], box[3] + 1):
            for xx in range(box[0], box[2] + 1):
                cells.add((xx, yy))
    for fx0, fy0, fx1, fy1 in frames.values():
        for xx in range(fx0, fx1 + 1):
            cells.add((xx, fy0))
            cells.add((xx, fy1))
        for yy in range(fy0, fy1 + 1):
            cells.add((fx0, yy))
            cells.add((fx1, yy))
    return cells


def _footer_start(box, clear):
    """Where text may start on a card's bottom border without covering the clear marker."""
    start = box[0] + 2
    if clear:
        start = max(start, box[0] + 1 + cell_width(clear))
    return start


def _draw_side_lanes(grid, edges, boxes, right):
    """Stacked pairs join round their right side, one lane each way, labelled under the upper card.

    Both directions share one bidirectional trunk on the title row: each card's arriving head
    and its trunk junction are on the same row, so the head and tail never sit on different
    rows. The trunk runs in the gutter right of the cards and never lands on a corner, a
    divider or another card whatever height the cards have.
    """
    links = tuple(edges.get("links", ()))
    for lane in edges.get("side_lanes", ()):
        if len(lane) < 2:
            continue
        upper, lower = str(lane[0]), str(lane[1])
        top, tail = boxes.get(upper), boxes.get(lower)
        if (not top or not tail or top[4] != tail[4] or top[2] != tail[2]
                or top[3] >= tail[1]):
            continue
        ux, uy, edge, ub = top[0], top[1], top[2], top[3]
        fy = tail[1]

        def put(px, py, text, key="branch_s"):
            grid.put(px, py, text, key, limit=right)

        # One bidirectional trunk: each head and its line share the title row, so
        # the visible head and tail never sit on different rows at a card.
        put(edge, uy + 1, LANE_HEAD_LEFT, "soft")      # lower → upper arrives
        put(edge + 1, uy + 1, "──")
        put(edge + 3, uy + 1, "┐")
        put(edge, fy + 1, LANE_HEAD_LEFT, "soft")      # upper → lower arrives
        put(edge + 1, fy + 1, "──")
        put(edge + 3, fy + 1, "┘")
        for yy in range(uy + 2, fy + 1):
            put(edge + 3, yy, "│")
        label_right = min(right, top[2])
        for row, (src, dst) in enumerate(((upper, lower), (lower, upper))):
            sent = _link_when(links, src, dst)
            if sent:
                _segments(grid, ux + 2, ub + 1 + row,
                          [(f"[{src}]→[{dst}] ", "dim"), (sent[0], "soft"), (f" {sent[1]}", "dim")],
                          label_right)


def _draw_cross_links(grid, edges, boxes, frames, right, clear_of):
    """A line between two frames that sit side by side, else a ``→[tag]`` badge on the sender.

    A line may only use empty cells: the side-lane gutter, another card or a third frame's border
    turns it into the existing badge. Both ends stay on the card borders they belong to, and the
    badge starts after the clear marker so the two never paint over each other.
    """
    lane_cells = _lane_cells(edges, boxes, right)
    occupied = _cross_occupancy(boxes, frames, lane_cells)
    for edge in edges.get("cross", ()):
        if len(edge) < 2:
            continue
        src, dst = str(edge[0]), str(edge[1])
        when = str(edge[2]) if len(edge) > 2 else ""
        a, b = boxes.get(src), boxes.get(dst)
        if not a or not b:
            continue
        sx0, sy0, sx1, sy1 = a[:4]
        dx0, dy0, _dx1, dy1 = b[:4]
        ly = max(sy0, dy0) + 2
        drawable = (sx1 + 1 < dx0 and sy0 < ly < sy1 and dy0 < ly < dy1
                    and (len(a) < 8 or a[7] != ly) and (len(b) < 8 or b[7] != ly)
                    and (sx1, ly) not in lane_cells)
        if drawable:
            allowed = {(sx1, ly), (frames[a[4]][2], ly), (frames[b[4]][0], ly)}
            drawable = (all((xx, ly) not in lane_cells for xx in range(sx1, dx0))
                        and all((xx, ly) not in occupied or (xx, ly) in allowed
                                for xx in range(sx1, dx0)))
        if drawable:
            grid.put(sx1, ly, "├", a[5], limit=right)
            for xx in range(sx1 + 1, dx0 - 1):
                grid.put(xx, ly, "─", "branch_s", limit=right)
            for edge_x in (frames[a[4]][2], frames[b[4]][0]):
                grid.put(edge_x, ly, "╫", "dim", limit=right)
            grid.put(dx0 - 1, ly, LANE_HEAD_RIGHT, "soft", limit=right)
            label = [(f"[{src}]→[{dst}] ", "dim"), ("steer", "soft"), (" " + when, "dim")]
            label_width = sum(cell_width(text) for text, _key in label)
            if frames[b[4]][0] - frames[a[4]][2] - 3 >= label_width:
                _segments(grid, frames[a[4]][2] + 2, ly - 1, label, right)
            else:
                _segments(grid, _footer_start(a, clear_of.get(src)), sy1,
                          [(" steer", "soft"), (" " + when + " ", "dim")], min(right, sx1))
        else:
            _segments(grid, _footer_start(a, clear_of.get(src)), sy1,
                      [(" →", "soft"), (f"[{dst}]", "text"), (" steer", "soft"),
                       (" " + when + " ", "dim")], min(right, sx1))


def _draw_markers(grid, vm, edges, boxes, frames, right, clear_of):
    """A recent clear on a seat marks its card's bottom border; a steward's watch marks its frame."""
    session_by_tag = {str(item.get("tag")): item for item in vm.sessions}
    for seat in vm.seats or ():
        tag = str(seat[0])
        box = boxes.get(tag)
        text = clear_of.get(tag)
        if box and text:
            harness = str(session_by_tag.get(tag, {}).get("harness") or "claude")
            grid.put(box[0] + 1, box[3], text, "hb_" + harness,
                     limit=min(right, box[2] + 1))
    for marker in edges.get("watches", ()):
        frame = frames.get(str(marker.get("repo")))
        if frame:
            grid.put(frame[0] + 3, frame[3], " ⚑ " + str(marker.get("text") or "") + " ", "steward",
                     limit=min(right, frame[2] + 1))


def _draw_links_compact(grid, vm, state, links, opened, x, y, use, right, bottom):
    grid.put(x, y, "── LINKS", "head", limit=right)
    grid.put(x + 10, y, f"{len(links)}" + (f" · [{opened}]" if opened else ""), "dim", limit=right)
    y += 2
    for link in links:
        if y + 1 >= bottom:
            grid.put(x + 2, bottom - 1, f"… {len(links)} links", "dim", limit=right)
            _mark_page_incomplete(grid)
            return
        frm, to, kind, when, note = link[:5]
        start0, start1 = x + 2, x + 20
        endpoint_text = lambda value: (f"[{value[0]}] {value[1]}" if isinstance(value, (tuple, list))
                                      else str(value))
        for start, value, limit in ((start0, frm, start1 - 2), (start1, to, right)):
            text = endpoint_text(value)
            grid.put(start, y, _clip(text, max(0, limit - start)), "soft", limit=limit)
        grid.put(start1 - 2, y, "→", "soft")
        for offset, endpoint in ((start0, frm), (start1, to)):
            if isinstance(endpoint, (tuple, list)) and endpoint and any(
                    session.get("tag") == str(endpoint[0]) for session in vm.sessions):
                grid.add_hit(y, offset, min(right, offset + cell_width(endpoint_text(endpoint))),
                             ("fold", "pfocus:" + state.view, str(endpoint[0])))
        y += 1
        grid.put(x + 4, y, f"{kind} · {when} · {note}", "dim", limit=right)
        y += 1




def _row_width(col_gaps, members, box_width):
    """Frame width for one row of columns: a trailing side lane keeps its gutter inside."""
    inner = 2 + sum(box_width + col_gaps[ci] for ci in members)
    return inner + 1 if col_gaps[members[-1]] > 2 else inner - col_gaps[members[-1]] + 2


def _wrap_columns(col_gaps, box_width, use):
    """Split column indexes into the fewest rows that fit ``use``, balanced across rows."""
    count = len(col_gaps)
    for rows in range(1, count + 1):
        per_row = -(-count // rows)
        chunks = [list(range(start, min(count, start + per_row))) for start in range(0, count, per_row)]
        if all(_row_width(col_gaps, chunk, box_width) <= use for chunk in chunks):
            return chunks
    return [[index] for index in range(count)]


def draw_peers(grid, vm, state, x=34, y=3):
    """Draw data-sized repository frames, session cards and the complete link list.

    ``peer_edges`` carries links, cross and side_lanes; ``cards[tag].owners`` the owner-stage
    rows under a card ([{state, label, stages:[{name, state}]}]) and ``watches`` the frame
    footers ([{repo, text}]). The GPU and question lines come from the session record and the
    questions, lane labels from the links, and the clear marker from the seats.
    """
    use = max(0, grid.cols - x - 2)
    right, bottom = x + use + 1, grid.rows - 4
    nodes = tuple(vm.peer_nodes or ())
    edges = vm.peer_edges or {}
    cards = edges.get("cards", {})
    questions = tuple(vm.questions or ())
    opened = state.opened_for("pfocus:" + state.view)
    session_by_tag = {str(item.get("tag")): item for item in vm.sessions}
    repo_by_tag = {str(tag): str(repo) for repo, columns in nodes
                   for column in columns for tag in column}
    title_width = max((cell_width(str(s.get("title") or "·")) for s in session_by_tag.values()), default=0)
    box_width = max(28, min(44, title_width + 4))
    frame_gap, column_gap, lane_gap, stack_gap = 3, 2, 6, 2
    owners_of = {tag: tuple(card.get("owners", ())) for tag, card in cards.items()}
    extra_of = {tag: _extra_lines(item, questions) for tag, item in session_by_tag.items()}
    clear_of = {}
    for seat in vm.seats or ():
        text = _clear_text(seat)
        if text:
            clear_of[str(seat[0])] = text

    def height_of(tag):
        return _card_height(owners_of.get(tag, ()), extra_of[tag])

    layouts = {}
    for repo, columns in nodes:
        cols = [tuple(str(tag) for tag in col if str(tag) in session_by_tag) for col in columns]
        cols = [col for col in cols if col]
        if not cols:
            continue
        col_gaps = [lane_gap if any(tuple(pair) == tuple(col[i:i + 2])
                                    for pair in edges.get("side_lanes", ())
                                    for i in range(max(0, len(col) - 1))) else column_gap
                    for col in cols]
        # Wrap the columns into as many rows as the pane width needs; each row keeps the
        # columns' own order and a stacked pair stays one column with its side lanes.
        grid_rows = _wrap_columns(col_gaps, box_width, use)
        positions, frame_width, yy = {}, 0, 1
        for members in grid_rows:
            frame_width = max(frame_width, _row_width(col_gaps, members, box_width))
            depth = max(len(cols[ci]) for ci in members)
            row_height = [max(height_of(cols[ci][row]) + 1 for ci in members if row < len(cols[ci]))
                          for row in range(depth)]
            dx = 2
            for ci in members:
                cy = yy
                for ri, tag in enumerate(cols[ci]):
                    positions[tag] = (dx, cy, height_of(tag))
                    cy += row_height[ri] + stack_gap
                dx += box_width + col_gaps[ci]
            yy += sum(row_height) + stack_gap * depth
        frame_height = yy - stack_gap + 1
        layouts[str(repo)] = {"repo": str(repo), "columns": cols, "positions": positions,
                              "width": frame_width, "height": frame_height, "col_gaps": col_gaps}

    def band_width(band):
        return sum(column["width"] for column in band) + frame_gap * max(0, len(band) - 1)

    def band_height(band):
        return max((offset + layouts[repo]["height"]
                    for column in band for repo, offset in column["items"]), default=0)

    # Reserve a shared row for explicit cross-repo edges when their frames fit.
    bands, band, placed = [], [], set()
    for edge in edges.get("cross", ()):
        if len(edge) < 2:
            continue
        src, dst = str(edge[0]), str(edge[1])
        src_repo, dst_repo = repo_by_tag.get(src), repo_by_tag.get(dst)
        if src_repo not in layouts or dst_repo not in layouts:
            continue
        source, target = layouts[src_repo], layouts[dst_repo]
        src_y = source["positions"][src][1]
        dst_y = target["positions"][dst][1]
        lo = max(0, dst_y - src_y - (height_of(src) - 3))
        hi = dst_y - src_y + (height_of(dst) - 3)
        if band and band_width(band) + frame_gap + source["width"] + frame_gap + target["width"] > use:
            bands.append(band); band = []
        if source["width"] + frame_gap + target["width"] > use:
            bands.append([{"width": source["width"], "items": [[src_repo, 0]]}])
            band = [{"width": target["width"], "items": [[dst_repo, 0]]}]
        else:
            band.extend([{"width": source["width"], "items": [[src_repo, lo]], "hi": hi},
                         {"width": target["width"], "items": [[dst_repo, 0]]}])
        placed.update((src_repo, dst_repo))
    if band:
        bands.append(band)

    def fits(candidate, repo):
        frame = layouts[repo]
        for column in candidate:
            grow = max(0, frame["width"] - column["width"])
            if band_width(candidate) + grow > use:
                continue
            if "hi" in column and len(column["items"]) == 1 and frame["height"] + 1 <= column["hi"]:
                column["items"][0][1] = max(column["items"][0][1], frame["height"] + 1)
                column["items"].insert(0, [repo, 0]); column["width"] += grow
                return True
            lowest = max(offset + layouts[name]["height"] for name, offset in column["items"])
            if frame["width"] <= column["width"] and lowest + 1 + frame["height"] <= band_height(candidate):
                column["items"].append([repo, lowest + 1])
                return True
        if band_width(candidate) + frame_gap + frame["width"] <= use or not candidate:
            candidate.append({"width": frame["width"], "items": [[repo, 0]]})
            return True
        return False

    for repo, _columns in nodes:
        repo = str(repo)
        if repo not in layouts or repo in placed:
            continue
        if not any(fits(candidate, repo) for candidate in bands if candidate):
            bands.append([{"width": layouts[repo]["width"], "items": [[repo, 0]]}])
    placements = []
    for candidate in bands:
        offset_x = 0
        row = []
        for column in candidate:
            for repo, offset_y in column["items"]:
                row.append((layouts[repo], offset_x, offset_y))
            offset_x += column["width"] + frame_gap
        placements.append((row, band_height(candidate)))
    frame_boxes, session_boxes = {}, {}
    cursor_y = y
    for band, band_h in placements:
        for frame, offset_x, offset_y in band:
            repo, fw, fh = frame["repo"], frame["width"], frame["height"]
            cursor_x, frame_y = x + offset_x, cursor_y + offset_y
            last_x = min(right - 1, cursor_x + fw - 1)
            _draw_frame(grid, cursor_x, frame_y, last_x, frame_y + fh - 1,
                        _clip(repo_rule_label(repo), max(0, fw - 6)), right)
            frame_boxes[repo] = (cursor_x, frame_y, last_x, frame_y + fh - 1)
            for tag, (dx, dy, card_h) in frame["positions"].items():
                session = session_by_tag[tag]
                bx, by = cursor_x + dx, frame_y + dy
                harness = str(session.get("harness") or "other")
                selected = opened == tag
                border = ("hb_" if selected else "h_") + harness
                _draw_card(grid, session, tag, bx, by, box_width, right, border,
                           extra_of[tag], owners_of.get(tag, ()))
                # The last field is the owner divider row; cross lines must not land on it.
                divider = by + 3 + len(extra_of[tag]) if owners_of.get(tag) else None
                session_boxes[tag] = (bx, by, bx + box_width - 1, by + card_h, repo, "h_" + harness,
                                      "hb_" + harness, divider)
                for hit_y in range(by, by + card_h + 1):
                    grid.add_hit(hit_y, bx, min(right, bx + box_width), ("fold", "pfocus:" + state.view, tag))
        cursor_y += band_h + 1

    _draw_side_lanes(grid, edges, session_boxes, right)
    _draw_cross_links(grid, edges, session_boxes, frame_boxes, right, clear_of)
    _draw_markers(grid, vm, edges, session_boxes, frame_boxes, right, clear_of)

    y = cursor_y
    if not placements:
        # Keep the same single row between the empty peer graph and LINKS as the
        # populated graph path leaves after its last repository frame.
        y += 1
    links = tuple(edges.get("links", ()))
    if opened:
        links = tuple(link for link in links
                      if any(isinstance(end, (list, tuple)) and str(end[0]) == opened for end in link[:2]))
    if links:
        report_end = getattr(grid, "report_natural_page_end", None)
        if report_end is not None:
            last_row = y + (2 + 2 * len(links) - 1 if use < 75 else 2 + len(links) - 1)
            report_end(last_row)
    if use < 75 and links:
        if y + 2 < bottom:
            _draw_links_compact(grid, vm, state, links, opened, x, y, use, right, bottom)
        return
    _draw_links(grid, state, session_by_tag, links, opened, x, y, use, right)


LINK_KIND_KEYS = {"steer": "soft", "notice": "dim", "watch ⚑": "steward"}


def _draw_links(grid, state, session_by_tag, links, opened, x, y, use, right):
    """Section rule, tinted column heads, then every link (or how many did not fit)."""
    cursor = grid.put(x, y, "── ", "line", limit=right)
    cursor = grid.put(cursor, y, "LINKS", "head", limit=right)
    meta = (f"[{opened}] · {len(links)} link{'' if len(links) == 1 else 's'} · click it again: all"
            if opened else "communication · supervision · handover")
    cursor = grid.put(cursor, y, "  " + meta, "dim", limit=right)
    grid.put(cursor, y, " ", "branch_s", limit=right)
    for xx in range(cursor + 1, right):
        grid.put(xx, y, "─", "line", limit=right)
    y += 1
    grid.fill(x, right, y, " ", tint=234)
    for off, label in ((3, "FROM"), (19, "TO"), (40, "KIND"), (51, "WHEN"), (64, "NOTE")):
        grid.put(x + off, y, label, "head", tint=234, limit=right)
    y += 1
    room = grid.rows - 6 - y
    if getattr(grid, "complete_page", False):
        # The natural-end repaint uses the row otherwise reserved between the
        # renderer's internal limit and the shared bottom probe.
        room += 1
    shown = links if len(links) <= room else links[:max(0, room - 1)]
    if len(shown) < len(links):
        more = len(links) - len(shown)
        grid.put(x + 3, y + len(shown), f"… {more} more link{'' if more == 1 else 's'}", "dim", limit=right)
        _mark_page_incomplete(grid)

    def endpoint(px, py, value, limit):
        if isinstance(value, (tuple, list)) and value:
            tag = str(value[0])
            harness = str(value[1]) if len(value) > 1 else "·"
            cursor = grid.put(px, py, "[", "tag_b", limit=right)
            steward = bool(session_by_tag.get(tag, {}).get("steward"))
            cursor = grid.put(cursor, py, tag, "steward" if steward else "text", limit=right)
            cursor = grid.put(cursor, py, "]", "tag_b", limit=right)
            grid.put(cursor, py, " " + harness, "hb_" + harness, limit=right)
        else:
            grid.put(px, py, _clip(str(value), limit), "dim", limit=right)

    for frm, to, kind, when, note in (tuple(link[:5]) for link in shown):
        for start, end, value in ((3, 16, frm), (19, 40, to)):
            if isinstance(value, (tuple, list)) and value and str(value[0]) in session_by_tag:
                grid.add_hit(y, x + start, min(right, x + end), ("fold", "pfocus:" + state.view, str(value[0])))
        endpoint(x + 3, y, frm, 13)
        grid.put(x + 16, y, "→", "soft", limit=right)
        endpoint(x + 19, y, to, 20)
        kind_key = LINK_KIND_KEYS.get(str(kind)) or (
            "hb_" + str(to[1]) if isinstance(to, (tuple, list)) and len(to) > 1 else "dim")
        grid.put(x + 40, y, str(kind), kind_key, limit=right)
        grid.put(x + 51, y, str(when), "dim", limit=right)
        grid.put(x + 64, y, _clip(str(note), max(0, use - 64)), "now_sub", limit=right)
        y += 1


def _mark_page_incomplete(grid):
    mark = getattr(grid, "mark_page_incomplete", None)
    if mark is not None:
        mark()
