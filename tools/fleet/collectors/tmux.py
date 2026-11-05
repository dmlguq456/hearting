"""aedd50 — where each session sits in tmux, for Fleet's WHERE cell (display only).

One ``tmux list-panes -a`` per snapshot on the server ``$TMUX`` names (tmux's default
server otherwise). A session is placed in the pane whose terminal is its runtime's
controlling terminal — the terminal the pane identity proof also reads — and only
when exactly one pane has it. A session Herdr already placed is left to Herdr.
Nothing is typed, moved or retitled; any failure leaves the cell as it was.
"""
import os
import shutil
import subprocess

from . import herdr

_TIMEOUT_S = 1.0
_FIELDS = ("#{pane_id}", "#{session_name}", "#{window_id}", "#{window_index}", "#{pane_index}",
           "#{pane_tty}", "#{pane_left}", "#{pane_top}", "#{pane_width}", "#{pane_height}",
           "#{@hearting_title}", "#{pane_title}")


def _tty_of(pid):
    """The controlling terminal device of ``pid`` (``/proc/<pid>/stat`` tty_nr), or None."""
    try:
        with open("/proc/%d/stat" % int(pid), "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        return int(fields[4]) or None
    except (OSError, ValueError, IndexError, TypeError):
        return None


def _device(path):
    try:
        return os.stat(path).st_rdev or None
    except (OSError, TypeError, ValueError):
        return None


def list_panes(runner=None, which=None, environ=None):
    """Every pane of the tmux server, or None when tmux is absent or does not answer."""
    runner, which = runner or subprocess.run, which or shutil.which
    if which("tmux") is None:
        return None
    env = os.environ if environ is None else environ
    socket = str(env.get("TMUX") or "").split(",", 1)[0].strip()
    argv = ["tmux"] + (["-S", socket] if socket else []) + ["list-panes", "-a", "-F", "\t".join(_FIELDS)]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=_TIMEOUT_S,
                      stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    rows = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != len(_FIELDS) or not parts[0].startswith("%"):
            continue
        try:
            rect = dict(zip(("x", "y", "width", "height"), map(int, parts[6:10])))
        except ValueError:
            continue
        rows.append({"pane_id": parts[0], "session": parts[1], "window_id": parts[2],
                     "window": parts[3], "pane": parts[4], "tty": parts[5], "rect": rect,
                     "headers": (parts[10], parts[11])})
    return rows


def location(row):
    """``<session>:<window>.<pane>``, tmux's own target notation."""
    return "%s:%s.%s" % (row["session"], row["window"], row["pane"])


def _mark_corners(rows, placed):
    """Set ``pane_corner`` on the ``⚑`` pane of every window that holds a placed session."""
    surface = herdr._terminal_surface()
    windows = {}
    for row in rows:
        windows.setdefault(row["window_id"], []).append(row)
    for members in windows.values():
        if not any(row["pane_id"] in placed for row in members):
            continue
        rects = [row["rect"] for row in members]
        x0, y0 = min(r["x"] for r in rects), min(r["y"] for r in rects)
        area = {"x": x0, "y": y0, "width": max(r["x"] + r["width"] for r in rects) - x0,
                "height": max(r["y"] + r["height"] for r in rects) - y0}
        layout = {"area": area, "panes": [{"pane_id": row["pane_id"], "rect": dict(row["rect"])}
                                          for row in members]}
        found = surface.supervisor_corner(layout, {row["pane_id"]: row["headers"] for row in members})
        if found and found[0] in placed:
            placed[found[0]].pane_corner = bool(found[1])


def enrich(sessions, rows=None):
    """Set ``pane_location``/``multiplexer``/``pane_corner`` for sessions in tmux panes."""
    for session in sessions:
        if getattr(session, "multiplexer", None) == "tmux":
            session.pane_location = session.multiplexer = session.pane_corner = None
    candidates = [s for s in sessions if herdr._eligible(s) and getattr(s, "pid", None)
                  and getattr(s, "herdr_attached", None) is not True
                  and getattr(s, "multiplexer", None) is None]
    if not candidates:
        return
    rows = list_panes() if rows is None else rows
    if not rows:
        return
    by_device = {}
    for row in rows:
        device = _device(row["tty"])
        if device:
            by_device.setdefault(device, []).append(row)
    placed = {}
    for session in candidates:
        found = by_device.get(_tty_of(session.pid)) or ()
        if len(found) != 1:
            continue
        session.pane_location, session.multiplexer = location(found[0]), "tmux"
        placed[found[0]["pane_id"]] = session
    if placed:
        _mark_corners(rows, placed)
