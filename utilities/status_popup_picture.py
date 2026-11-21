#!/usr/bin/env python3
"""The status popup's screens as text, from one document and a size (11-21, design C).

    python3 -I -B utilities/status_popup_picture.py <campaign.md | -> [--width 100] [--height 26] [--steps first,group,item,short,full]
                                                    [--fleet-line TEXT] [--now EPOCH] [--out FILE] [--kind status|backlog] [--own FOLDER]

``<document>`` is a ``campaign.md`` (the generated page) or any other status page; ``-`` is no document. ``--width`` and
``--height`` are the popup frame's size in cells (100 x 26 by default). The steps draw, in order: ``first`` the screen the
popup opens on, ``group`` the same with the first folded group opened, ``item`` with the first item cut at the width opened in place,
``short`` with the first item that is not cut pressed (it has nothing to open: the screen stays), ``all`` with
``all N ›`` opened, ``full`` the document read whole (``full document ›``). A page that is not the generated one has no list
screen: ``first`` and ``full`` draw the one screen it always had.

``--kind backlog`` draws the BACKLOG popup the same way: ``<document>`` is a ``backlog.md`` (or ``-`` for none), ``--own`` the name of the
campaign folder this window's status document is in (its group stands open on top), and the steps are the same (``first``, ``group`` the
first folded group opened, ``item`` the first item that has something to open, opened, ``short``, ``full``); it has no Fleet lines.

What it runs is the product's: the backend's own answer (``session_shell._status_popup``, which reads the document the way the
popup does) laid out by the host's own list code (``utilities/session-shell/status-list.cjs``) and drawn in the popup's own frame
(``panel-view.cjs``): the list screen from the backend's ``list`` answer and ``full document`` from its ``doc`` answer, the two the popup asks
for. It runs in an environment of its own: the document is copied into a private temporary folder, the
environment is emptied (no real session, setting or artifact root is read), nothing is written outside that folder, and the
folder is removed at the end. Fleet's status lines (the ``full`` screen) are one fixture line (``--fleet-line``), never a real
collection. The keys row is put together from the product's own words.

One limit: the cell widths the pictures are laid out by are a plain rule (every character from U+2E80 up is two cells, the rest one), not
the terminal's Unicode tables the real popup measures with, which the picture's isolated environment does not have. A combining mark, a
zero-width character or a jamo can be one cell wider or narrower here than in the real popup (the backend's own row cut, which uses
Fleet's measure, is the same in both).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ["STATUS", "BACKLOG", "NOW", "QUESTIONS", "ROUTE", "MESSAGES", "MEM"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("document", help="a campaign.md (or another status page), or - for no document")
    parser.add_argument("--width", type=int, default=100)
    parser.add_argument("--height", type=int, default=26)
    parser.add_argument("--steps", default="first,group,item,short,full")
    parser.add_argument("--fleet-line", default="fixture: Fleet's status line stands here")
    parser.add_argument("--now", type=float, default=None, help="the time the age is told against (default: now)")
    parser.add_argument("--out", default="")
    parser.add_argument("--kind", choices=("status", "backlog"), default="status")
    parser.add_argument("--own", default="")
    args = parser.parse_args(argv)
    if args.width < 20 or args.height < 6:
        parser.error("the popup frame is at least 20 x 6 cells")
    scratch = Path(tempfile.mkdtemp(prefix="spp.", dir="/tmp"))
    try:
        text = picture(args, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def picture(args, scratch):
    home = scratch / "home"
    home.mkdir()
    node = shutil.which("node")             # found before the environment is emptied
    if not node:
        raise SystemExit("node is needed to draw the popup")
    for name in list(os.environ):          # nothing of the real session, setting or project is read (or written) through the environment
        del os.environ[name]
    os.environ.update(PATH="/usr/local/bin:/usr/bin:/bin", HOME=str(home), TMPDIR=str(scratch), LANG="C.UTF-8")
    sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
    from unittest import mock
    import session_shell
    from fleet import session_status
    from fleet import session_tags
    from fleet.explorer.view_model import ExplorerViewModel

    cols, rows = args.width + 4, args.height + 2
    helper = ROOT / "utilities/session-shell/status-popup-picture.cjs"
    size = json.loads(run_node(node, helper, scratch, {"mode": "size", "cols": cols, "rows": rows}))
    columns = max(20, min(200, size["width"] - 4))
    now = time.time() if args.now is None else args.now

    if args.kind == "backlog":
        return backlog_picture(args, scratch, node, helper, columns, cols, rows, now, session_shell, session_status, mock)
    campaign = "campaign"
    if args.document == "-":
        view = {"word": "STATUS", "has_doc": False, "campaign": "", "age": "", "title": "STATUS", "path": "",
                "slot": "campaigns/<campaign>/현황.md", "watch": []}
    else:
        source = Path(args.document)
        name = source.resolve().parent.name        # the campaign folder's own name titles the popup, as it does there
        folder = scratch / "root" / "campaigns" / (name if session_status._DATE.match(name) else "2026-01-01_" + name)
        folder.mkdir(parents=True)
        copy = folder / source.name
        shutil.copy2(source, copy)          # the modification time comes with it: the age is the document's own
        campaign = session_status._campaign_name(folder)
        age = session_status._age(session_status._mtime(copy), now)
        view = {"word": "STATUS", "has_doc": True, "campaign": campaign, "age": age,
                "title": session_status._title("STATUS", campaign, age), "path": str(copy), "slot": "", "watch": []}
    binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
    request = {"cwd": str(scratch), "routes": [], "status": {}}
    status = {"routes": 0, "questions": 0, "campaigns": [{"campaign": campaign, "line": args.fleet_line}]}
    model = ExplorerViewModel.from_mapping({
        "repos": [{"id": "proj", "name": "proj", "status": status}], "system": {}, "routes": [],
        "sessions": [{"tag": "e1", "repo_id": "proj", "harness": "claude", "state": "idle"}]})
    real_doc = getattr(session_status, "popup_doc", None)       # (a tree before the list screen has none: its screen is the one it had)
    with mock.patch.object(session_status, "held", lambda cwd, claim, now=None: view), \
            mock.patch.object(session_status, "popup_doc", lambda v, w, now_=None, want_list=False: real_doc(v, w, now, want_list), create=True), \
            mock.patch.object(session_shell, "_popup_view_model", lambda harness, sid, kind: model), \
            mock.patch.object(session_tags, "assigned_tag", lambda harness, sid: "e1"):
        # (a tree before the list screen answers `doc` for both: its one screen)
        first = session_shell._status_popup(binding, dict(request, part="list" if real_doc else "doc"), columns, 256, True)
        doc = session_shell._status_popup(binding, dict(request, part="doc"), columns, 256, True)
        fleet = session_shell._status_popup(binding, dict(request, part="fleet"), columns, 256, True)
    if not first.get("ok") or not doc.get("ok"):
        raise SystemExit("the document could not be read: " + json.dumps(doc))
    from fleet.config import ui_text
    keys = {"list": ui_text("fleet.session_panel.popup_status"),
            "popup_keys": ui_text("fleet.session_panel.popup_keys"), "fold": ui_text("fleet.session_panel.popup_unfold")}
    title = str(first.get("title") or "STATUS")
    rest = title[len("STATUS"):] if title.startswith("STATUS") else ""
    steps = [step for step in args.steps.split(",") if step]
    drawn = run_node(node, helper, scratch, {"mode": "draw", "cols": cols, "rows": rows, "answer": first, "document": doc, "fleet": fleet if fleet.get("ok") else None,
                                       "sections": SECTIONS, "keys": keys, "title_rest": rest, "steps": steps})
    return "{} x {} popup frame\n\n{}\n".format(args.width, args.height, drawn.rstrip("\n"))


def backlog_picture(args, scratch, node, helper, columns, cols, rows, now, session_shell, session_status, mock):
    """The BACKLOG popup's screens: ``<scratch>/root/backlog.md`` is the copy of the document (its modification time kept), read the way the popup reads it."""
    root = scratch / "root"
    root.mkdir(exist_ok=True)
    if args.document != "-":
        shutil.copy2(Path(args.document), root / "backlog.md")
    binding = {"harness": "claude", "sid": "00000000-0000-4000-8000-000000000001"}
    request = {"cwd": str(scratch), "routes": [], "status": {}, "own": args.own}
    with mock.patch.object(session_status, "_root", lambda cwd, at: str(root)), mock.patch("time.time", lambda: now):
        first = session_shell._status_popup(binding, dict(request, part="list"), columns, 256, True, "backlog")
        doc = session_shell._status_popup(binding, dict(request, part="doc"), columns, 256, True, "backlog")
    if not first.get("ok") or not doc.get("ok"):
        raise SystemExit("the document could not be read: " + json.dumps(doc))
    from fleet.config import ui_text
    keys = {"list": ui_text("fleet.session_panel.popup_status"),
            "popup_keys": ui_text("fleet.session_panel.popup_keys"), "fold": ui_text("fleet.session_panel.popup_unfold")}
    title = str(first.get("title") or "BACKLOG")
    steps = [step for step in args.steps.split(",") if step]
    drawn = run_node(node, helper, scratch, {"mode": "draw", "cols": cols, "rows": rows, "answer": first, "document": doc, "fleet": None, "sections": SECTIONS,
                                       "keys": keys, "title_rest": title[len("BACKLOG"):], "word": "BACKLOG", "current": 1, "steps": steps})
    return "{} x {} popup frame\n\n{}\n".format(args.width, args.height, drawn.rstrip("\n"))


def run_node(node, helper, scratch, payload):
    path = scratch / "input-{}.json".format(payload["mode"])
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run([node, str(helper), str(path)], capture_output=True, text=True, check=False, timeout=60,
                          env={"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "TMPDIR": os.environ["TMPDIR"], "LANG": "C.UTF-8"})
    if done.returncode:
        raise SystemExit("the drawing failed:\n" + done.stderr)
    return done.stdout


if __name__ == "__main__":
    raise SystemExit(main())
