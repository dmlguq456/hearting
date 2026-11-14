#!/usr/bin/env python3
"""Dev-only: render the Fleet screens the README shows, off-screen, from the README demo rows.

    python3 tools/fleet/tests/readme_captures.py [--out docs/fleet] [--no-gif]

Draws the Fleet screens with the cell renderer and ``readme_demo.snapshot()`` (English, neutral,
synthetic rows; no home, registry, herdr or network read; no live Fleet window is opened):

    overview.png  routes.png  questions.png  session.png  resources.png  output.png
    narrow.png (OVERVIEW at 100 columns)

and the session panel of the same demo world with ``readme_panel`` (the shell backend's own
projection, painted by ``panel-view.cjs`` under node; the conversation beside it is illustrative):

    seat.png (a session with its panel above Fleet's OVERVIEW)  panel.png (one panel with LAB)
    panel-question.png (the bottom layout, question first)  requests.png (requester and receiver)
    panel-focus.gif (Ctrl+] > Tab > 1 > Enter > Ctrl+])
    walkthrough.gif (OVERVIEW > ROUTES > session > that session's panel > output)

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
WALK = ("overview", "routes", "session", "panel", "output")   # "panel": the session's own window
COLOURS = 96
FRAME_MS = 2600
PANEL_COLS = 40
# The first picture: the [4f] window (conversation columns + two + the panel) over Fleet's top rows.
SEAT_TALK, SEAT_ROWS, SEAT_FLEET_ROWS = 80, 38, 12


def isolate():
    """No home, config, locale or clock of the machine reaches the frame."""
    home = tempfile.mkdtemp(prefix="fleet-readme-home-")
    atexit.register(shutil.rmtree, home, True)
    os.environ["HOME"] = home
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
                 "FLEET_DEMO", "HEARTING_HOME", "AGENT_HOME"):
        os.environ.pop(name, None)
    # Nor the calling session's own pointers (an agent shell names the real dispatch jobs log,
    # state roots, herdr socket and tmux server in these).
    for name in [key for key in os.environ if key != "FLEET_NOW_LANG" and key.startswith(
            ("AGENT_", "HARNESS_", "HERDR_", "TMUX", "CLAUDE", "CODEX_", "OPENCODE_", "FLEET_"))]:
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


def panel_rows(world, sid, height, *, width=PANEL_COLS, side=True, keys=(), expanded=(),
               state=None, configuration=None):
    """One demo session's panel, painted by the shell's own renderer (``readme_panel``)."""
    from tools.fleet.tests import readme_panel
    session = world.sessions[sid]
    state = state or {"working": "working", "blocked": "blocked"}.get(session.liveness, "idle")
    payload = readme_panel.projection(world, session.harness, sid, columns=width, rows=height, state=state)
    if "lab" in expanded:
        expanded = [key for key in expanded if key != "lab"] + [str((payload["panel"].get("lab") or {}).get("key") or "lab:")]
    return readme_panel.paint([{"payload": payload, "width": width, "height": height, "side": side,
                                "state": state, "sid": sid, "harness": session.harness, "frame": 3,
                                "keys": list(keys), "expanded": list(expanded),
                                "configuration": configuration or {}}])[0]


def session_window(world, sid, cols, rows):
    """A demo session's terminal: the illustrative conversation, two cells, then its panel."""
    from tools.fleet.tests import readme_panel as panel
    talk = cols - PANEL_COLS - 2
    grid = panel.beside(panel.cells(panel.conversation(sid, talk, rows)),
                        panel.cells(panel_rows(world, sid, rows)), talk)
    return panel.image(grid, cols, rows)


def seat(render, capture, world):
    """P1: the ``[4f]`` window with its panel, and Fleet's OVERVIEW top rows under it."""
    from tools.fleet.tests import readme_panel as panel
    cols = SEAT_TALK + 2 + PANEL_COLS
    top = panel.window(session_window(world, panel.API, cols, SEAT_ROWS),
                       "web-api · claude [4f] — conversation (illustrative) and the Hearting panel")
    SCREENS["_seat"] = ("OVERVIEW", "-", cols, 45, False)
    try:
        fleet = image_of(render, capture, frames(render, ["_seat"])["_seat"])[0]
    finally:
        SCREENS.pop("_seat", None)
    fleet = fleet.crop((0, 0, fleet.size[0], SEAT_FLEET_ROWS * capture.CELL_H))
    return panel.screen([[top], [panel.window(fleet, "fleet — every session on this machine")]])


def panel_pictures(render, capture, with_gif):
    """``{file name: PIL image or [GIF frames]}`` for the session panel's README pictures."""
    from tools.fleet.tests import readme_demo
    from tools.fleet.tests import readme_panel as panel
    snapshot, _hosts = readme_demo.snapshot()
    world = panel.World(snapshot, readme_demo.NOW)
    pictures = {"seat.png": seat(render, capture, world)}
    pictures["panel.png"] = panel.screen([[panel.window(
        panel.image(panel.cells(panel_rows(world, panel.ML, 46, expanded=("lab",))), PANEL_COLS, 46),
        "ml-train · claude [d1]")]])
    question = panel.beside(panel.cells(panel.conversation(panel.API_NOTES, 100, 9)), [], 100, gap=0)
    question += panel.cells(panel_rows(world, panel.API_NOTES, 14, width=100, side=False,
                                       configuration={"layout": "bottom"}))
    pictures["panel-question.png"] = panel.screen([[panel.window(
        panel.image(question, 100, 23), "web-api · claude [9c] — a narrow window: the panel below, question first")]])
    rq = "request:rq-7d21c4"
    pictures["requests.png"] = panel.screen([[
        panel.window(panel.image(panel.cells(panel_rows(world, panel.API, 46, expanded=(rq,))), PANEL_COLS, 46),
                     "claude [4f] — asked"),
        panel.window(panel.image(panel.cells(panel_rows(world, panel.API_REVIEW, 46, expanded=(rq,))), PANEL_COLS, 46),
                     "codex [b2] — asked to")]])
    if with_gif:
        steps = ((), ("enter",), ("enter", "\t"), ("enter", "\t", "2"), ("enter", "\t", "2", "\r"),
                 ("enter", "\t", "2", "\r", "\x1d"))
        words = (("", "keys still go to the agent"),
                 ("Ctrl+]", "focus the panel"), ("Tab", "the next question"),
                 ("2", "choose the second option"), ("Enter", "send the answer"),
                 ("Ctrl+]", "back to the agent"))
        frames_ = []
        for keys, (key, text) in zip(steps, words):
            rows = panel_rows(world, panel.API_NOTES, 30, keys=keys)
            body = panel.image(panel.cells(rows), PANEL_COLS, 30)
            from PIL import Image
            framed = Image.new("RGB", (body.size[0], body.size[1] + 2 * capture.CELL_H), capture.BACKGROUND)
            framed.paste(body, (0, 0))
            framed.paste(panel.caption(text, PANEL_COLS, key=key), (0, body.size[1]))
            frames_.append(panel.screen([[panel.window(framed, "web-api · claude [9c]")]]))
        pictures["panel-focus.gif"] = frames_
    return pictures, world


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
    extra, world = panel_pictures(render, capture, with_gif)
    for name, picture in extra.items():
        path = out / name
        if isinstance(picture, list):
            steps = [quantised(step) for step in picture]
            steps[0].save(path, save_all=True, append_images=steps[1:], duration=FRAME_MS, loop=0,
                          optimize=True, disposal=1)
        else:
            quantised(picture).save(path, optimize=True)
        written.append(path)
    if with_gif:
        # One size for every step: the GIF is drawn at the tallest screen's height.
        fleet_steps = [name for name in WALK if name in SCREENS]
        rows = max(SCREENS[name][3] for name in fleet_steps)
        walk = frames(render, fleet_steps, rows_override=rows)
        # The session page's own window (Fleet opens that session's panel), at the same size.
        session = session_window(world, "readme-claude-ml", SCREENS["session"][2], rows)
        steps = [quantised(session if name == "panel" else image_of(render, capture, walk[name])[0])
                 for name in WALK]
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
    os.environ["FLEET_NOW_LANG"] = "en"       # README screens are English on every machine
    isolate()
    write(args.out.resolve(), not args.no_gif)
    return 0


if __name__ == "__main__":
    sys.exit(main())
