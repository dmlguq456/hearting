#!/usr/bin/env python3
"""Dev-only: draw the fleet2 explorer's cell grid as a PNG, with render.py's own colours.

    python3 tools/fleet/tests/explorer_capture.py --cols 140 --rows 45 --out DIR \
        [--view ROUTES] [--scope session:a7] [--usage] [--fold ropen:ROUTES=rt-…] [--demo]
        [--bundle JSON | --view-model JSON]

The default reads this host the way ``fleet --explorer --once`` does (collectors, compute hosts,
memory). ``--demo`` uses the hermetic demo rows the end-to-end test uses instead. Each run writes
``explorer-<label>-<cols>x<rows>.png`` (the painted cells; the palette is render.py's ``_HUE_OF``
decomposition over its xterm-256 colours and tint backgrounds) and the same frame as ``.txt``.
PIL is imported only when the PNG is drawn. Nothing is ever written inside the repository.
"""
import argparse
import importlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

CELL_W, CELL_H, FONT_PX = 9, 18, 15
BACKGROUND = (15, 17, 26)
DEFAULT_FG = (200, 200, 200)
FONTS = {
    "latin": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "latin-bold": "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "wide": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "wide-bold": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "symbols": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",     # braille spinner, arrows
    "symbols-bold": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}


def host_snapshot(render):
    """The snapshot ``render_once`` publishes for this host, read-only.

    The collectors are imported as ``fleet.*``, exactly as ``fleet.py`` does when it runs as a
    script; the same readers imported as ``tools.fleet.*`` find a different module graph and
    return thinner rows.
    """
    from fleet import gitinfo, installinfo
    from fleet.collectors import collect_all, compute_hosts
    from fleet.refresh import as_snapshot
    snapshot = as_snapshot(collect_all(harness_filter=None))
    gitinfo.enrich_entities(list(snapshot.sessions) + list(snapshot.jobs), schedule_ahead=False)
    try:
        render.set_hearting(installinfo.collect(refresh_remote=False, fast_local=True))
    except Exception:
        render.set_hearting(None)
    snapshot.memory = render._collect_memory()
    try:
        render.set_compute_hosts(compute_hosts.collect())
    except Exception:
        render.set_compute_hosts(None)
    return snapshot


def demo_snapshot(render):
    from tools.fleet.tests import explorer_fixture
    snapshot, hosts = explorer_fixture.demo_snapshot()
    render.set_compute_hosts(hosts)
    return snapshot


def palette(render):
    """hue letter -> RGB, from render.py's xterm-256 choices and its richer-RGB refinements."""
    def rgb(index):
        if index >= 232:
            level = 8 + 10 * (index - 232)
            return (level, level, level)
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[index // 36], steps[(index // 6) % 6], steps[index % 6])

    named = {}
    for name, index in render._MUTED_256.items():
        named[name] = rgb(index)
    for name, (r, g, b) in render._RICHER_RGB_1000.items():
        named[name] = (round(r * 255 / 1000), round(g * 255 / 1000), round(b * 255 / 1000))
    return {"d": DEFAULT_FG, "w": named["soft"], "g": named["green"], "y": named["yellow"],
            "v": named["vanilla"], "r": named["red"], "c": named["cyan"], "m": named["magenta"],
            "l": named["blue"], "p": named["steward_pink"]}, {
        tint: tuple(round(v * 255 / 1000) for v in rgb1000)
        for tint, rgb1000 in render._TINT_RGB_1000.items()}


def blend(colour, other, amount):
    return tuple(round(a * (1 - amount) + b * amount) for a, b in zip(colour, other))


def cell_style(render, hues, tints, key, tint):
    """``(fg, bg, bold)`` of one cell the way ``render._key_attr`` composes it."""
    hue, attr = render._HUE_OF.get(key, ("d", 0))
    fg = hues.get(hue, DEFAULT_FG)
    bg = tints.get(tint, BACKGROUND)
    if attr & render._A_DIM:
        fg = blend(fg, bg, 0.42)
    if attr & render._A_REVERSE:
        fg, bg = bg, fg
    return fg, bg, bool(attr & render._A_BOLD)


def draw_png(render, frame, path):
    from PIL import Image, ImageDraw, ImageFont
    hues, tints = palette(render)
    fonts = {name: ImageFont.truetype(file, FONT_PX, index=(0 if file.endswith(".ttc") else 0))
             for name, file in FONTS.items() if Path(file).exists()}
    image = Image.new("RGB", (frame.cols * CELL_W, frame.rows * CELL_H), BACKGROUND)
    canvas = ImageDraw.Draw(image)
    unknown = set()
    for y, row in enumerate(frame.cells):
        x = 0
        while x < len(row):
            glyph, key, tint = row[x]
            width = 2 if x + 1 < len(row) and row[x + 1][0] == "" and glyph else 1
            if key not in render._HUE_OF:
                unknown.add(key)
            fg, bg, bold = cell_style(render, hues, tints, key, tint)
            canvas.rectangle([x * CELL_W, y * CELL_H, (x + width) * CELL_W - 1, (y + 1) * CELL_H - 1], fill=bg)
            if glyph.strip():
                family = ("wide" if width == 2 else "symbols" if "\u2800" <= glyph <= "\u28ff"
                          else "latin") + ("-bold" if bold else "")
                font = fonts.get(family) or fonts.get("latin")
                if font is not None:
                    canvas.text((x * CELL_W, y * CELL_H - 1), glyph, font=font, fill=fg)
            x += width
    image.save(path)
    return sorted(str(key) for key in unknown)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--rows", type=int, default=45)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--view", default="OVERVIEW")
    parser.add_argument("--scope", default="-")
    parser.add_argument("--usage", action="store_true", help="draw the usage block open")
    parser.add_argument("--fold", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--demo", action="store_true", help="the hermetic demo rows, not this host")
    parser.add_argument("--bundle", type=Path, help="replay a recorded snapshot/detail JSON bundle")
    parser.add_argument("--view-model", type=Path, help="draw a scrubbed typed view-model fixture")
    args = parser.parse_args(argv)

    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error("--out must be outside the repository (captures are evidence, not source)")
    out.mkdir(parents=True, exist_ok=True)

    if args.bundle and args.view_model:
        parser.error("--bundle and --view-model are mutually exclusive")
    package = "tools.fleet" if args.demo or args.bundle or args.view_model else "fleet"
    render = importlib.import_module(package + ".render")
    NavState = importlib.import_module(package + ".explorer.state").NavState
    render.set_explorer_mode(True)
    folds = tuple(sorted(tuple(item.split("=", 1)) for item in args.fold))
    title = args.scope.split(":", 1)[-1] if args.scope != "-" else args.view
    state = NavState(view=args.view, scope=args.scope, title=title, usage_open=args.usage, folds=folds)
    if args.view_model:
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.view_model import load_fixture
        frame = draw(load_fixture(args.view_model), state, args.cols, args.rows, beat=1)
    elif args.bundle:
        from tools.fleet.explorer.frame import draw
        from tools.fleet.explorer.live import build_view_model
        from tools.fleet.tests.snapshot_fixture import replay_snapshot
        snapshot, detail = replay_snapshot(args.bundle)
        frame = draw(build_view_model(snapshot, accepted_detail=detail, now=time.time()),
                     state, args.cols, args.rows, beat=1)
    else:
        snapshot = demo_snapshot(render) if args.demo else host_snapshot(render)
        frame = render._explorer_compose(snapshot, args.cols, args.rows, state, now=time.time())
        if not args.demo:                       # the reads the live view would do for this screen
            detail = render.explorer_read_detail(snapshot, state)
            frame = render._explorer_compose(snapshot, args.cols, args.rows, state, detail, now=time.time())

    label = (args.scope.replace(":", "-") if args.scope != "-" else args.view.lower()) + (
        "-usage" if args.usage else "") + ("-demo" if args.demo else "") + ("-recorded" if args.bundle else "")
    stem = out / f"explorer-{label}-{args.cols}x{args.rows}"
    stem.with_suffix(".txt").write_text("\n".join(row.rstrip() for row in frame.text_rows()) + "\n",
                                        encoding="utf-8")
    try:
        unknown = draw_png(render, frame, stem.with_suffix(".png"))
    except ImportError as exc:
        print(f"PIL is not installed ({exc}); wrote the text frame only", file=sys.stderr)
        print(stem.with_suffix(".txt"))
        return 0
    print(stem.with_suffix(".png"))
    print(stem.with_suffix(".txt"))
    if unknown:
        print("colour keys without a render.py hue (drawn default):", ", ".join(unknown), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
