#!/usr/bin/env python3
"""Render the Fleet view as a wide, coloured SVG for the README.

A fenced code block carries no colour and wraps at the README's column width,
which is the wrong shape for a view whose whole point is one wide row per
session and a visible dispatch tree beneath it. An SVG carries both, and GitHub
renders it inline.

This reproduces the real wide layout rather than inventing one: the bracketed
usage header, the `harness (model·effort) / session (branch) / stages / time`
column header, two-line session cards, and the `╻`/`╹` rail that dispatch rows
hang from. Colours are the terminal palette from `tools/fleet/render.py` —
sage green working, warm-beige idle, coral blocked, per-family model hues, and
level-coloured gauges (green <50, yellow <80, red >=80). Depth reads as
brightness, exactly as it does in the terminal: main rows bright, dispatched
rows progressively dimmer.

Colour is applied with presentation attributes, not a stylesheet: GitHub
sanitises `<style>` out of the SVGs it serves, so classes would render as
undifferentiated grey.

Content is synthetic and ASCII-only. Real `fleet --once` output carries live
project paths and session titles, and double-width CJK would break the
character-grid alignment this layout depends on.

Usage:
  python3 tools/render-fleet-svg.py          # write docs/fleet.svg
  python3 tools/render-fleet-svg.py --check  # verify it is current
"""
from __future__ import annotations

import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "fleet.svg"

CHAR = 7.55
FONT = 12.8
LINE = 19.6
PAD_X = 20.0
PAD_Y = 16.0
COLS = 200
TOP = 40.0

BG = "#0D0F16"
BAR = "#151824"
BORDER = "#2B2E3C"

# tools/fleet/render.py palette
P = {
    "green":   "#addb81",   # working
    "yellow":  "#dbdb81",   # idle, mid-level gauge
    "red":     "#ffa9a9",   # blocked, high-level gauge
    "cyan":    "#81dbdb",   # opus family
    "magenta": "#db81db",   # fable family
    "blue":    "#a9a9ff",   # sonnet family
    "vanilla": "#ffffd7",   # spinner
    "soft":    "#dadada",   # focal text
    "chrome":  "#9a9aa8",   # header bands
    "dim":     "#6a6a7c",
    "dimmer":  "#4e4e60",
    "rail":    "#5a5f7e",
    "track":   "#464a63",   # unfilled gauge
}
# dispatch rows recede: same hue, lower intensity
DIM = {
    "green": "#8fb56b", "yellow": "#b5b56b", "red": "#d49090",
    "cyan": "#5e9c9c", "blue": "#7878b8", "magenta": "#9c5e9c",
    "soft": "#a6a6b4", "track": "#3a3d52",
}


def lvl(pct: int, dim: bool = False) -> str:
    """Gauge hue by level, as `lvl_g`/`lvl_y`/`lvl_r` do in the terminal."""
    key = "green" if pct < 50 else ("yellow" if pct < 80 else "red")
    return DIM[key] if dim else P[key]


def s(text: str, colour: str, bold: bool = False):
    return (text, colour, bold)


def pad(text: str, width: int) -> str:
    return text[:width] if len(text) > width else text + " " * (width - len(text))


def gauge(pct: int, width: int, dim: bool = False):
    filled = max(1, round(pct / 100 * width))
    return [s("━" * filled, lvl(pct, dim)),
            s("─" * (width - filled), DIM["track"] if dim else P["track"])]


def bracket(pct, width, dim=False):
    """The usage header's `[━━━━───  77%]` form."""
    if pct is None:
        return [s("[", P["dimmer"]), s("·" * width, P["dimmer"]),
                s("   —]", P["dimmer"])]
    filled = max(1, round(pct / 100 * width))
    return [s("[", P["dimmer"]), s("━" * filled, lvl(pct, dim)),
            s("─" * (width - filled), P["track"]),
            s(f" {pct:>3}%", P["chrome"]), s("]", P["dimmer"])]


FAMILY = {"Opus": "cyan", "Sonnet": "blue", "Fable": "magenta", "gpt": "yellow"}


def card(depth, spin, spin_colour, harness, model, effort, title, branch,
         stages, age, pct, detail, tag=None, where=None):
    """One two-line session card; `depth` controls rail, indent, and brightness.

    F-100 (2026-09-03): `tag` is the depth-0 session's 2-hex tag, drawn as a `[2f]`
    outline badge (dim brackets, soft-white tag) between the status glyph and the
    harness text; `where` is the context row's lead — "herdr" (soft white), "tty"
    (dim) or None (blank). The old `working`/`idle` word no longer lives on the
    second line: the first line's glyph is the one status indicator.
    """
    d = depth > 0
    fam = FAMILY.get(model.split()[0].split("-")[0], "soft")
    fam_c = DIM[fam] if d else P[fam]
    text_c = DIM["soft"] if d else P["soft"]
    lead = "▍ " + "   " * depth
    rail_a = s("╻ ", P["rail"]) if d else s("", P["rail"])
    rail_b = s("╹ ", P["rail"]) if d else s("", P["rail"])
    who = f"{harness} ({model}·{effort})"
    chip = ([s("[", P["dim"]), s(tag, P["soft"]), s("]", P["dim"])] if tag
            else [s("    ", P["dim"])])
    if where == "herdr":
        lead2 = [s(pad("herdr", 8), P["soft"])]
    elif where == "tty":
        lead2 = [s(pad("tty", 8), P["dim"])]
    else:
        lead2 = [s(" " * 8, P["dim"])]
    line1 = [
        s(lead, P["rail"]), rail_a,
        s(spin + " ", spin_colour),
        *chip,
        s(pad(who, 32 - depth * 3), fam_c),
        s(pad(f"{title} ({branch})", 44), text_c),
        s(pad(stages, 96), DIM["blue"] if d else P["dim"]),
        s(age.rjust(7), P["dimmer"]),
    ]
    line2 = [
        s(lead + "  ", P["rail"]), rail_b,
        *lead2,
        *gauge(pct, 16, d),
        s(f" {pct:>3}%   ", P["dim"]),
        s(detail, P["dimmer"]),
    ]
    return [line1, line2]


def lines():
    out = [
        [s("  usage ", P["chrome"]), s(pad("claude code", 14), P["soft"]),
         s("5h ", P["dim"]), *bracket(77, 10), s(" ↻ 42m    ", P["dimmer"]),
         s("7d ", P["dim"]), *bracket(89, 10), s(" ↻ 3d23h    ", P["dimmer"]),
         s("fable ", P["dim"]), *bracket(93, 10)],
        [s("       ", P["chrome"]), s(pad("codex", 14), P["soft"]),
         s("5h ", P["dim"]), *bracket(None, 10), s("            ", P["dimmer"]),
         s("7d ", P["dim"]), *bracket(100, 10), s(" ↻ 2d16h", P["dimmer"])],
        [s("  fleet ", P["chrome"]), s("⣸ 3 working", P["green"]),
         s("   ● 12 idle", P["yellow"]), s("   ↳ 4 jobs (3 working)", P["dim"]),
         s("      ⚙ governor 3/5", P["dimmer"])],
        [s("─" * 5, P["dimmer"])],
        [],
        [s("    ", P["dim"]), s(pad("harness (model·effort)", 38), P["dim"]),
         s(pad("session (branch)", 44), P["dim"]),
         s(pad("stages", 96), P["dim"]), s("time".rjust(7), P["dim"])],
        [],
        [s("● ", P["magenta"]), s("hearting/", P["magenta"], True),
         s("   3 open   1 note", P["dimmer"])],
    ]

    out += card(0, "⣸", P["vanilla"], "claude code", "Opus 5", "xhigh",
                "Promote Fleet on the landing page", "main", "—", "16h01m",
                53, "⚙ python3   render a wide fleet svg for the readme",
                tag="2f", where="herdr")
    out += card(1, "⣸", P["vanilla"], "claude code", "Opus 5", "xhigh",
                "autopilot-code owner", "opencode-pa…",
                "code(debug·std·owner) / mp:deep / unit:_kernel/owner : execute✓ › impl-review › test › report", "1h43m",
                27, "3 stage workers dispatched at depth 2, joined before continuation")
    out += card(2, "⣸", P["vanilla"], "claude code", "Sonnet 5", "medium",
                "code-execute", "opencode-pa…",
                "code-execute(std) / mp:light / unit:dev/backend : running", "22m",
                11, "⚙ pytest   dispatch_v20 regression")
    out += card(2, "●", DIM["blue"], "codex", "gpt-5.6", "medium",
                "impl-review", "opencode-pa…",
                "impl-review(std) / mp:light / unit:qa/reviewer : done", "2m",
                34, "0 blocking findings")
    out += card(2, "●", DIM["red"], "codex", "gpt-5.6", "xhigh",
                "failure-mode", "opencode-pa…",
                "failure-mode(std) / mp:deep / unit:qa/adversary : blocked", "1m",
                8, "waiting on input")
    out += card(0, "●", P["yellow"], "codex", "gpt-5.6-sol", "xhigh",
                "Fleet dispatch column widths", "main", "—", "1d6h",
                12, "", where="herdr")
    out += [
        [s("▍", P["rail"]), s("─" * (COLS - 2), "#20222E")],
        [s("▍ ", P["rail"]),
         s("mem 16:52 + durable/decision   ", P["dimmer"]),
         s("\"Fleet OpenCode display fixed in two commits…\"", P["dimmer"])],
        [],
        [s("○ ", P["dim"]), s("corrnet_runtime/", P["dim"])],
    ]
    out += card(0, "●", P["yellow"], "claude code", "Opus 5", "xhigh",
                "Loss-function sampling investigation", "main", "—", "51m",
                22, "", tag="9b", where="tty")
    return out


def render() -> str:
    rows = lines()
    width = round(PAD_X * 2 + COLS * CHAR)
    height = round(TOP + PAD_Y * 2 + len(rows) * LINE)
    body = []
    for i, row in enumerate(rows):
        if not row:
            continue
        y = TOP + PAD_Y + LINE * (i + 0.8)
        spans = "".join(
            '<tspan fill="%s"%s>%s</tspan>'
            % (c, ' font-weight="600"' if b else "", escape(t))
            for t, c, b in row
        )
        body.append(f'<text x="{PAD_X:.0f}" y="{y:.1f}" xml:space="preserve">{spans}</text>')

    dots = "".join(f'<circle cx="{21 + n * 15}" cy="20" r="4.2" fill="#31344a"/>' for n in range(3))
    mono = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="fleet: a live cross-harness view. An owner session dispatches execute, impl-review and failure-mode workers at depth two, each with its own sealed model profile and context gauge.">
  <rect width="{width}" height="{height}" rx="11" fill="{BG}"/>
  <path d="M0 11a11 11 0 0 1 11-11h{width - 22}a11 11 0 0 1 11 11v{TOP - 11}H0Z" fill="{BAR}"/>
  <line x1="0" y1="{TOP}" x2="{width}" y2="{TOP}" stroke="{BORDER}"/>
  <rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="11" fill="none" stroke="{BORDER}"/>
  {dots}
  <text x="70" y="25" font-family="{mono}" font-size="12.5" fill="#8A8A9A">fleet</text>
  <circle cx="{width - 76}" cy="20" r="3.3" fill="{P['green']}"/>
  <text x="{width - 66}" y="24" font-family="{mono}" font-size="11.5" fill="{P['green']}">LIVE</text>
  <g font-family="{mono}" font-size="{FONT}">
  {chr(10).join("  " + b for b in body).strip()}
  </g>
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
