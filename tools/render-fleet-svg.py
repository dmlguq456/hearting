#!/usr/bin/env python3
"""Render the Fleet Explorer OVERVIEW as a coloured SVG for the README (docs/fleet.svg).

The picture is the real cell renderer's frame, not a hand-drawn mock-up: ``readme_demo`` supplies
English, neutral, synthetic rows (no home, registry, herdr or network read; no Fleet window is
opened), ``tools/fleet/explorer`` draws the 160x45 cell grid, and every cell is written out with
the colour ``render.py`` gives it. The same rows, drawn to PNG/GIF, are the other README screens
(``python3 tools/fleet/tests/readme_captures.py``).

Colour is applied with presentation attributes, not a stylesheet: GitHub sanitises ``<style>``
out of the SVGs it serves. Each run of text carries ``textLength`` so the character grid holds
whatever monospace font the viewer falls back to.

Usage:
  python3 tools/render-fleet-svg.py          # write docs/fleet.svg
  python3 tools/render-fleet-svg.py --check  # verify it is current
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]
OUT = ROOT / "docs" / "fleet.svg"

CELL_W, CELL_H, FONT = 9, 18, 15
TOP = 40                         # the window bar above the grid
BAR, BORDER, DIM = "#151824", "#2B2E3C", "#8A8A9A"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"
LABEL = ("Fleet's Explorer OVERVIEW, drawn from synthetic demo data: eight sessions in four projects "
         "across Claude Code, Codex and OpenCode, grouped by project with model, context gauge and "
         "your last input, one session waiting for an answer, two open routes with their "
         "stage lines, and per-host CPU and GPU use at the bottom left.")


def hexcolour(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def grid_rows(frame, render, hues, tints, background):
    """Per row: ``(backgrounds, texts)``; a background is ``(x, width, colour)``, a text
    ``(x, width, string, colour, bold)``, both in cell units. Only non-default fills are kept."""
    from tools.fleet.tests import explorer_capture as capture
    rows = []
    for row in frame.cells:
        fills, runs, x = [], [], 0
        while x < len(row):
            glyph, key, tint = row[x]
            width = 2 if x + 1 < len(row) and row[x + 1][0] == "" and glyph else 1
            fg, bg, bold = capture.cell_style(render, hues, tints, key, tint)
            if bg != background:
                if fills and fills[-1][2] == bg and fills[-1][0] + fills[-1][1] == x:
                    fills[-1] = (fills[-1][0], fills[-1][1] + width, bg)
                else:
                    fills.append((x, width, bg))
            glyph = capture.GLYPH_FALLBACK.get(glyph, glyph)
            if glyph.strip():
                style = (fg, bold)
                if runs and runs[-1][4] == style and runs[-1][0] + runs[-1][1] == x:
                    last = runs[-1]
                    runs[-1] = (last[0], last[1] + width, last[2] + glyph, last[3], style)
                else:
                    runs.append((x, width, glyph, None, style))
            elif runs and runs[-1][0] + runs[-1][1] == x and width == 1 and x + 1 < len(row):
                # A space inside a run of one style: keep it in the run when the next glyph
                # continues that style, so one <text> covers a phrase.
                nxt = row[x + 1]
                nfg, _nbg, nbold = capture.cell_style(render, hues, tints, nxt[1], nxt[2])
                if nxt[0].strip() and (nfg, nbold) == runs[-1][4]:
                    last = runs[-1]
                    runs[-1] = (last[0], last[1] + 1, last[2] + " ", last[3], last[4])
            x += width
        rows.append((fills, [(a, w, t, style[0], style[1]) for a, w, t, _n, style in runs]))
    return rows


def render() -> str:
    from tools.fleet import render as fleet_render
    from tools.fleet.tests import explorer_capture as capture
    from tools.fleet.tests import readme_captures
    readme_captures.isolate()
    frame = readme_captures.frames(fleet_render, ["overview"])["overview"]
    hues, tints = capture.palette(fleet_render)
    background = capture.BACKGROUND
    rows = grid_rows(frame, fleet_render, hues, tints, background)
    width, height = frame.cols * CELL_W, TOP + frame.rows * CELL_H
    body = []
    for y, (fills, texts) in enumerate(rows):
        top = TOP + y * CELL_H
        for x, span, colour in fills:
            body.append(f'<rect x="{x * CELL_W}" y="{top}" width="{span * CELL_W}" height="{CELL_H}" '
                        f'fill="{hexcolour(colour)}"/>')
        for x, span, text, colour, bold in texts:
            weight = ' font-weight="700"' if bold else ""
            body.append(f'<text x="{x * CELL_W}" y="{top + 14}" textLength="{span * CELL_W}" '
                        f'lengthAdjust="spacing" fill="{hexcolour(colour)}"{weight} '
                        f'xml:space="preserve">{escape(text)}</text>')
    dots = "".join(f'<circle cx="{21 + n * 15}" cy="20" r="4.2" fill="#31344a"/>' for n in range(3))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(LABEL)}">
<title>Fleet Explorer, OVERVIEW (demo data)</title>
<rect width="{width}" height="{height}" rx="11" fill="{hexcolour(background)}"/>
<path d="M0 11a11 11 0 0 1 11-11h{width - 22}a11 11 0 0 1 11 11v{TOP - 11}H0Z" fill="{BAR}"/>
<line x1="0" y1="{TOP}" x2="{width}" y2="{TOP}" stroke="{BORDER}"/>
{dots}
<text x="70" y="25" font-family="{MONO}" font-size="12.5" fill="{DIM}">fleet</text>
<text x="{width - 20}" y="25" text-anchor="end" font-family="{MONO}" font-size="11.5" fill="{DIM}">demo data</text>
<g font-family="{MONO}" font-size="{FONT}">
{chr(10).join(body)}
</g>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="11" fill="none" stroke="{BORDER}"/>
</svg>
"""


def main() -> int:
    want = render()
    if "--check" in sys.argv[1:]:
        if not OUT.is_file() or OUT.read_text(encoding="utf-8") != want:
            print(f"stale: {OUT.relative_to(ROOT)}")
            return 1
        print("docs/fleet.svg up-to-date")
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(want, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
