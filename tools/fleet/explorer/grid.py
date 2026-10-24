"""Cell-accurate drawing surface and ordered hit-map geometry."""
from __future__ import annotations

from dataclasses import dataclass
from .style import normalize_style


def cell_width(char):
    if not char:
        return 0
    from .output import cell_width as existing_width
    return existing_width(char)


def clip_cells(text, room):
    """Cut ``text`` to ``room`` cells, always ending in ``…`` when something was cut.

    A room of zero or less still leaves the ``…``: the approved drawing never returns an empty
    string for text that did not fit, however little room is left.
    """
    text = str(text)
    if cell_width(text) <= room:
        return text
    out, used = [], 0
    for char in text:
        cells = cell_width(char)
        if used + cells > room - 1:
            break
        out.append(char)
        used += cells
    return "".join(out) + "…"


@dataclass(frozen=True)
class Hit:
    y: int
    x0: int
    x1: int
    action: tuple


class CellGrid:
    default_trace = None       # a list a test installs so the next grids record their puts

    def __init__(self, cols, rows, background=None):
        self.cols, self.rows = max(0, int(cols)), max(0, int(rows))
        self.cells = [[(" ", None, background) for _ in range(self.cols)] for _ in range(self.rows)]
        self.hits = []
        self.trace = CellGrid.default_trace   # ownership checks in tests; None in production
        self.pane_clip = None                 # (first column, edge): pane text never crosses the edge

    def put(self, x, y, text, key=None, tint=None, limit=None):
        x, y = int(x), int(y)
        if y < 0 or y >= self.rows:
            return x
        right = self.cols if limit is None else min(self.cols, int(limit))
        if self.pane_clip is not None and x >= self.pane_clip[0]:
            right = min(right, self.pane_clip[1])
        if x >= right:
            return x                          # a column the pane or screen does not have: nothing to draw
        style = normalize_style(key, tint)
        text = str(text)
        trace = self.trace
        if trace is not None:
            serial = len(trace)
        for index, char in enumerate(text):
            width = cell_width(char)
            if width <= 0:
                continue
            if x + width > right:
                if trace is not None:
                    trace.append(("clip", serial, y, text[index:], x + width > self.cols))
                break
            if x >= 0:
                if trace is not None:
                    trace.append(("cell", serial, y, char, x, width))
                self.cells[y][x] = (char, style[0], style[1])
                for offset in range(1, width):
                    self.cells[y][x + offset] = ("", style[0], style[1])
            x += width
        return x

    def fill(self, x0, x1, y, glyph=" ", key=None, tint=None):
        y = int(y)
        if y < 0 or y >= self.rows:
            return
        lo, hi = max(0, int(x0)), min(self.cols, int(x1))
        style = normalize_style(key, tint)
        if self.trace is not None:
            self.trace.append(("fill", y, lo, hi, glyph))
        for x in range(lo, hi):
            self.cells[y][x] = (glyph, style[0], style[1])

    def mark_moved(self):
        """Tell a recording test that cells were copied wholesale (their owners no longer apply)."""
        if self.trace is not None:
            self.trace.append(("reset",))

    def add_hit(self, y, x0, x1, action):
        y, x0, x1 = int(y), int(x0), int(x1)
        if 0 <= y < self.rows and x0 < x1:
            left, right = max(0, x0), min(self.cols, x1)
            if left < right:
                self.hits.append(Hit(y, left, right, tuple(action)))

    def replace_hits(self, hits):
        self.hits = []
        for y, x0, x1, action in hits:
            self.add_hit(y, x0, x1, action)

    def rows_text(self):
        return tuple("".join(cell[0] for cell in row) for row in self.cells)

    def snapshot(self):
        return tuple(tuple(row) for row in self.cells)


def put_fit(grid, x, y, segments, limit=None, tint=None):
    """Draw ``(text, key)`` segments left to right, ending in ``…`` where the room runs out.

    Nothing is dropped silently: the segment that does not fit is clipped with an ellipsis and the
    rest are left out. Returns the x after the last drawn cell.
    """
    from .output import clip
    right = grid.cols if limit is None else min(grid.cols, int(limit))
    for text, key in segments:
        room = right - x
        if room <= 0:
            break
        text = str(text)
        if cell_width(text) <= room:
            x = grid.put(x, y, text, key, tint=tint, limit=right)
        else:
            grid.put(x, y, clip(text, room), key, tint=tint, limit=right)
            return right
    return x


Grid = CellGrid
