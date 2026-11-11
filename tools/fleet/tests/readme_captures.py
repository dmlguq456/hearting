#!/usr/bin/env python3
"""Dev-only: render the Fleet screens the README shows, off-screen, from the README demo rows.

    python3 tools/fleet/tests/readme_captures.py [--out docs/fleet] [--no-gif]

Draws eight screens with the cell renderer and ``readme_demo.snapshot()`` (English, neutral,
synthetic rows; no home, registry, herdr or network read; no live Fleet window is opened):

    overview.png  routes.png  questions.png  session.png  resources.png  output.png
    walkthrough.gif (OVERVIEW > ROUTES > session > output)  narrow.png (OVERVIEW at 100 columns)

PNGs are palette-quantised so the README loads quickly. Unlike ``explorer_capture.py`` (evidence,
kept outside the repo) these are published assets, so the default output is ``docs/fleet``.
``tools/render-fleet-svg.py`` draws the same OVERVIEW as ``docs/fleet.svg``.
"""
import argparse
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

# name -> (view, scope, cols, rows, with raw output)
SCREENS = {
    "overview": ("OVERVIEW", "-", 160, 45, False),
    "routes": ("ROUTES", "-", 160, 40, False),
    "questions": ("QUESTIONS", "-", 160, 40, False),
    "session": ("OVERVIEW", "session:d1", 160, 40, False),
    "resources": ("COMPUTE", "-", 160, 40, False),
    "output": ("ROUTES", "route:LAB", 160, 45, True),
    "narrow": ("OVERVIEW", "-", 100, 40, False),
}
WALK = ("overview", "routes", "session", "output")
COLOURS = 96
FRAME_MS = 2600


def isolate():
    """No home, config, locale or clock of the machine reaches the frame."""
    home = tempfile.mkdtemp(prefix="fleet-readme-home-")
    atexit.register(shutil.rmtree, home, True)
    os.environ["HOME"] = home
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                 "FLEET_DEMO", "HEARTING_HOME", "AGENT_HOME"):
        os.environ.pop(name, None)
    os.environ["TZ"] = "UTC"
    time.tzset()


def frames(render, wanted, rows_override=None):
    """``{name: Frame}`` for the wanted screens, all from one snapshot."""
    from tools.fleet.explorer.state import NavState
    from tools.fleet.tests import readme_demo
    snapshot, hosts = readme_demo.snapshot()
    render.set_explorer_mode(True)
    render.set_compute_hosts(hosts)
    lab = readme_demo.route_id(snapshot, "lab")
    result = {}
    for name in wanted:
        view, scope, cols, rows, raw = SCREENS[name]
        rows = rows_override or rows
        scope = scope.replace("LAB", lab or "")
        title = scope.split(":", 1)[-1] if scope != "-" else view
        state = NavState(view=view, scope=scope, title=title)
        result[name] = readme_demo.frame(render, snapshot, state, cols, rows,
                                         (lab, "a3f9c21d") if raw else None)
    return result


def image_of(render, capture, frame):
    """The frame as a PIL image, drawn with ``explorer_capture``'s cell painter."""
    from PIL import Image
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "frame.png"
        unknown = capture.draw_png(render, frame, path)
        image = Image.open(path)
        image.load()
    return image, unknown


def quantised(image):
    from PIL import Image
    return image.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def write(out, with_gif):
    from PIL import Image
    from tools.fleet import render
    from tools.fleet.tests import explorer_capture as capture
    names = list(SCREENS)
    drawn = frames(render, names)
    out.mkdir(parents=True, exist_ok=True)
    pictures, written = {}, []
    for name in names:
        image, unknown = image_of(render, capture, drawn[name])
        if unknown:
            print(f"{name}: colour keys without a render.py hue: {', '.join(unknown)}", file=sys.stderr)
        pictures[name] = image
        path = out / f"{name}.png"
        quantised(image).save(path, optimize=True)
        written.append(path)
    if with_gif:
        # One size for every step: the GIF is drawn at the tallest screen's height.
        walk = frames(render, WALK, rows_override=max(SCREENS[name][3] for name in WALK))
        steps = [quantised(image_of(render, capture, walk[name])[0]) for name in WALK]
        path = out / "walkthrough.gif"
        steps[0].save(path, save_all=True, append_images=steps[1:], duration=FRAME_MS, loop=0,
                      optimize=True, disposal=1)
        written.append(path)
    for path in written:
        print(f"{path} {path.stat().st_size // 1024} KiB")
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "fleet")
    parser.add_argument("--no-gif", action="store_true")
    args = parser.parse_args(argv)
    isolate()
    write(args.out.resolve(), not args.no_gif)
    return 0


if __name__ == "__main__":
    sys.exit(main())
