#!/usr/bin/env python3
"""Dev-only: render the Fleet screens the README shows, off-screen, from the README demo rows.

    python3 tools/fleet/tests/readme_captures.py [--out docs/fleet] [--no-gif]

Draws the Fleet screens with the cell renderer and ``readme_demo.snapshot()`` (English, neutral,
synthetic rows; no home, registry, herdr or network read; no live Fleet window is opened):

    overview.png  peers.png  routes.png (one route opened in place)  questions.png  session.png
    resources.png  output.png  narrow.png (OVERVIEW at 100 columns)

and the session panel of the same demo world with ``readme_panel`` (the shell backend's own
projection, painted by ``panel-view.cjs`` under node; the conversation beside it is illustrative):

    seat.png (session [4f]'s window: the conversation and its panel)
    panel.png ([4f]'s window: its REPO row's STATUS button pressed, the project status document in
    a popup, the rest gray)
    panel-question.png (the bottom layout)  requests.png (one request in the route box of the
    requester and of the receiver)  panel-focus.gif (the question in rose > Ctrl+] > Tab > 2 > Enter:
    the question popup over the window, then `answer sent`)
    walkthrough.gif (OVERVIEW > ROUTES > session > that session's panel > output)

The README opens with seat.png; overview.png, peers.png and routes.png show the same moment of the
same demo world, so the route, the request and the messages [4f]'s panel shows read the same in the
three Fleet screens (the panel's ROUTE popup is Fleet's own table of the same world).
One run redraws every picture; two runs write the same bytes.

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

# name -> (view, scope, cols, rows, with raw output, the route opened in place: "code", "lab" or None)
# Each Fleet screen is tall enough for its whole list: nothing below it is cut off.
SCREENS = {
    "overview": ("OVERVIEW", "-", 160, 45, False),
    "peers": ("PEERS", "-", 160, 45, False),
    "routes": ("ROUTES", "-", 160, 50, False, "code"),
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
# The first picture: the [4f] window (conversation columns + two + the panel).
SEAT_TALK, SEAT_ROWS = 80, 61
# The answering GIF: the [9c] window, its panel and the question popup over both.
FOCUS_TALK, FOCUS_ROWS = 80, 30
# The status button pressed: the [4f] window with the status popup (as wide as the first picture).
POPUP_ROWS = 43
# The request on both sides: the two panels (the same height as the first picture: the route box keeps the named rules of
# its zones, `dispatch` and `request`, while any row of the zone shows).
REQUEST_ROWS = 43


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
    from tools.fleet.tests import readme_demo, readme_panel
    snapshot, hosts = readme_demo.snapshot()
    snapshot, _folders = readme_panel.with_status_document(snapshot, readme_demo.NOW)
    render.set_explorer_mode(True)
    render.set_compute_hosts(hosts)
    lab = readme_demo.route_id(snapshot, "lab")
    result = {}
    for name in wanted:
        view, scope, cols, rows, raw, opened = (*SCREENS[name], None)[:6]
        rows = rows_override or rows
        scope = scope.replace("LAB", lab or "")
        title = scope.split(":", 1)[-1] if scope != "-" else view
        folds = (("ropen:" + view, readme_demo.route_id(snapshot, opened)),) if opened else ()
        state = NavState(view=view, scope=scope, title=title, folds=folds)
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


def named_colours():
    """The RGB of the colours that carry a meaning, in the cell painter's own terms: the five fields' hues of a route's kind
    word (and their dim rung, the ``quick`` bracket), and the three zones' (``tools/fleet/palette.py``). A picture keeps each
    exactly: median-cut alone merges them into a neighbour with another meaning (code blue into claude's teal, lab amber into
    the status yellow)."""
    from tools.fleet import render
    from tools.fleet.palette import _KIND_256, _ZONE_256
    from tools.fleet.tests import explorer_capture as capture
    hues, _tints = capture.palette(render)

    def rgb(index):
        if index >= 232:
            level = 8 + 10 * (index - 232)
            return (level, level, level)
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[index // 36], steps[(index // 6) % 6], steps[index % 6])

    colours = [rgb(index) for index in (*_KIND_256.values(), *_ZONE_256.values())]
    colours += [capture.blend(rgb(index), capture.BACKGROUND, 0.42) for index in _KIND_256.values()]
    assert all(hues["k" + name] in colours for name in ("code", "lab", "design", "docs", "research"))
    return list(dict.fromkeys(colours))


def quantised(image):
    """The image in at most ``COLOURS`` palette colours, the named ones among them exactly: median-cut the rest, then map every
    pixel to its nearest palette colour (a named colour's own pixels are at distance 0, so they stay)."""
    from PIL import Image
    pinned = named_colours()
    cut = image.quantize(colors=COLOURS - len(pinned), method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    flat = cut.getpalette()[:3 * (max(cut.getdata()) + 1)]
    cut_colours = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
    palette = list(dict.fromkeys([*pinned, *cut_colours]))[:COLOURS]
    table = Image.new("P", (1, 1))
    table.putpalette([channel for colour in palette for channel in colour])
    return image.quantize(palette=table, dither=Image.Dither.NONE)


def panel_paint(world, sid, height, *, width=PANEL_COLS, side=True, keys=(), expanded=(),
                state=None, configuration=None, window=None, table=None):
    """One demo session's panel, painted by the shell's own renderer (``readme_panel``):
    ``{"rows", "popup"}``. With ``window`` (``cols``, ``rows``) the popup the host would lay over it:
    the question popup while ``keys`` leave the editor focused, or the ``table`` popup (``ROUTE``,
    ``STATUS`` or ``MESSAGES``) pressed in the panel."""
    from tools.fleet.tests import readme_panel
    session = world.sessions[sid]
    state = state or {"working": "working", "blocked": "blocked"}.get(session.liveness, "idle")
    columns = readme_panel.popup_width(window["cols"]) if window else None
    payload = readme_panel.projection(world, session.harness, sid, columns=width, rows=height, state=state,
                                      popup_columns=columns)
    table_cols = max(20, min(200, readme_panel.popup_width(window["cols"] if window else 160, "table") - 4))
    job_table = None
    if table:
        job_table = {"kind": table}
        if table == "MESSAGES":
            job_table["lines"] = readme_panel.unreceived_lines(world, sid)
        else:
            kind = {"ROUTE": "routes", "STATUS": "status"}[table]
            job_table["result"] = readme_panel.popup_read(world, sid, kind, columns=table_cols)
    return readme_panel.paint([{"payload": payload, "width": width, "height": height, "side": side,
                                "state": state, "sid": sid, "harness": session.harness, "frame": 3,
                                "keys": list(keys), "expanded": list(expanded),
                                "configuration": configuration or {}, "window": window, "table": job_table}])[0]


def panel_rows(world, sid, height, **options):
    return panel_paint(world, sid, height, **options)["rows"]


def session_window(world, sid, cols, rows, *, keys=(), table=None):
    """A demo session's terminal: the illustrative conversation, two cells, then its panel, and the
    popup over both while the keys leave the question open or a ``table`` popup stands, with
    everything behind it in the veil's gray (``host.cjs`` lays it over the window)."""
    from tools.fleet.tests import readme_panel as panel
    talk = cols - PANEL_COLS - 2
    painted = panel_paint(world, sid, rows, keys=keys, window={"cols": cols, "rows": rows}, table=table)
    grid = panel.beside(panel.cells(panel.conversation(sid, talk, rows)), panel.cells(painted["rows"]), talk)
    if painted["popup"]:
        shown = painted["popup"]
        if shown.get("veil") is not None:
            grid = panel.veiled(grid, shown["veil"])
        grid = panel.overlay(grid, panel.cells(shown["rows"]), shown["top"], shown["left"])
    return panel.image(grid, cols, rows)


def answered(questions, sid, question_id, value):
    """The demo answer-wait board a moment after ``value`` was sent to one question."""
    import copy
    board = copy.deepcopy(questions)
    for item in board.get(sid, ()):
        if item["id"] == question_id:
            item.update(request_state="submitted", answerable=False, sent=[value])
    return board


def seat(world):
    """The README's first picture: the ``[4f]`` window, its conversation and its whole panel."""
    from tools.fleet.tests import readme_panel as panel
    cols = SEAT_TALK + 2 + PANEL_COLS
    return panel.screen([[panel.window(session_window(world, panel.API, cols, SEAT_ROWS),
                                       "web-api · claude [4f] — conversation (illustrative) and the Hearting panel")]])


def panel_pictures(render, capture, with_gif):
    """``{file name: PIL image or [GIF frames]}`` for the session panel's README pictures."""
    from tools.fleet.tests import readme_demo
    from tools.fleet.tests import readme_panel as panel
    snapshot, hosts = readme_demo.snapshot()
    world = panel.World(snapshot, readme_demo.NOW, hosts)
    pictures = {"seat.png": seat(world)}
    # The REPO row's status button pressed: [4f]'s project status document opens in a popup in the
    # middle of the window, everything behind it in one gray.
    cols = SEAT_TALK + 2 + PANEL_COLS
    pictures["panel.png"] = panel.screen([[panel.window(
        session_window(world, panel.API, cols, POPUP_ROWS, table="STATUS"),
        "web-api · claude [4f] — the STATUS button pressed")]])
    question = panel.beside(panel.cells(panel.conversation(panel.API_NOTES, 100, 9)), [], 100, gap=0)
    question += panel.cells(panel_rows(world, panel.API_NOTES, 18, width=100, side=False,
                                       configuration={"layout": "bottom"}))
    pictures["panel-question.png"] = panel.screen([[panel.window(
        panel.image(question, 100, 27), "web-api · claude [9c] — the bottom layout")]])
    # One request on both sides: its row in the route box of the session that asked (→) and of the
    # one asked (◂), the same title and state.
    pictures["requests.png"] = panel.screen([[
        panel.window(panel.image(panel.cells(panel_rows(world, panel.API, REQUEST_ROWS)), PANEL_COLS, REQUEST_ROWS),
                     "claude [4f] — asked"),
        panel.window(panel.image(panel.cells(panel_rows(world, panel.API_REVIEW, REQUEST_ROWS)), PANEL_COLS, REQUEST_ROWS),
                     "codex [b2] — asked to")]])
    if with_gif:
        # Answering: Ctrl+] opens the question in a popup over the window (host.cjs ``popupNow``),
        # Tab reads the next one, 2 chooses, Enter sends it: the popup closes, the panel keeps a dim
        # receipt line, and the keys are the agent's again.
        from PIL import Image
        cols = FOCUS_TALK + 2 + PANEL_COLS
        steps = (((), "", "a question waits; keys still go to the agent"),
                 (("enter",), "Ctrl+]", "the question opens in a popup"),
                 (("enter", "\t"), "Tab", "the next question"),
                 (("enter", "\t", "2"), "2", "choose the second option"),
                 (None, "Enter", "send it; the popup closes and keys go back to the agent"))
        frames_ = []
        for keys, key, text in steps:
            if keys is None:
                # The moment after Enter: the backend reports the answer on its way (``submitted``,
                # what was sent), so the panel's count and its dim receipt line agree.
                kept = world.questions
                world.questions = answered(kept, panel.API_NOTES, "q-demo-scope", "Public and internal")
                try:
                    body = session_window(world, panel.API_NOTES, cols, FOCUS_ROWS)
                finally:
                    world.questions = kept
            else:
                body = session_window(world, panel.API_NOTES, cols, FOCUS_ROWS, keys=keys)
            framed = Image.new("RGB", (body.size[0], body.size[1] + 2 * capture.CELL_H), capture.BACKGROUND)
            framed.paste(body, (0, 0))
            framed.paste(panel.caption(text, cols, key=key), (0, body.size[1]))
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
