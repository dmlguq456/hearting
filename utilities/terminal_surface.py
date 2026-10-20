"""Open one Hearting pane beside the caller without moving anyone's focus.

``open_pane`` is the one place Hearting creates a terminal pane. It uses the
terminal the caller already sits in: a Herdr pane proven to be the caller's,
then the tmux server ``$TMUX`` names when it answers, else none
(``surface="none"`` with the command line a person can run). Placement reads
the tab's shape and fills it as a 2×2 grid; a full tab reuses an agentless empty
shell, then the newest other tab of the same workspace, then gets one new tab.
It never re-lays out a tab, retries, or closes a pane, and it reports what the
pane list showed right after opening.

The provider boundary (a surface places, labels and lists panes; the command
is composed apart from it), the honest partial result, and the single pane-list
read right after opening are adapted from OpenRig
``packages/daemon/src/domain/terminal/`` (``terminal-provider.ts``,
``view-composer.ts``, ``herdr-adapter.ts``) at commit
9eeb2e42ca6dcfb8da60a8517dc39c7d83b84082, Apache-2.0, rewritten in Python and
modified for single-pane placement; see ``docs/third-party/openrig/``.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Optional


_HERE = Path(__file__).resolve().parent
_PROMPT = r"(?m)(?:^|[ ])(?:[$#%❯])\s*(?-m:$)"
_PROMPT_LINE = re.compile(r"(?:^|[ ])(?:[$#%❯])\s*$")
_SHELLS = frozenset({"bash", "dash", "fish", "ksh", "nu", "sh", "zsh"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LABEL_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")   # Herdr metadata token names
_TITLE_MAX = 48
_HELD = set()          # workspace lock files this process holds
_PEER_MESSAGE = None


@dataclass
class PaneResult:
    """What opening did, as observed; ``notes`` carries every reason and doubt."""

    surface: str                  # herdr | tmux | none
    pane: Optional[str]
    placement: str                # split | reuse | tab | none
    notes: list = field(default_factory=list)
    view_command: str = ""

    def json(self):
        return asdict(self)


@dataclass(frozen=True)
class Near:
    """An already-checked surface and reference: beside ``pane``, or inside ``workspace``."""

    surface: object
    pane: Optional[str] = None
    workspace: Optional[str] = None


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15,
                          stdin=subprocess.DEVNULL, check=False)


def next_cell(layout):
    """Match partial 2×2 grids by their dividers, at any split ratio.

    The user resizes columns and rows; the shape, not a 0.5 ratio, decides the next split.
    """
    area, panes = layout.get("area"), layout.get("panes")
    if not isinstance(area, dict) or not isinstance(panes, list) or not 1 <= len(panes) <= 3:
        return None
    keys = ("x", "y", "width", "height")
    if any(type(area.get(k)) is not int for k in keys) or min(area["width"], area["height"]) < 4:
        return None
    for pane in panes:
        rect = pane.get("rect") if isinstance(pane, dict) else None
        if not isinstance(rect, dict) or any(type(rect.get(k)) is not int for k in keys):
            return None
    panes = sorted(panes, key=lambda p: (p["rect"]["x"], p["rect"]["y"]))
    x0, y0, x1, y1 = area["x"], area["y"], area["x"] + area["width"], area["y"] + area["height"]
    xs = sorted({p["rect"]["x"] for p in panes} - {x0})
    ys = sorted({p["rect"]["y"] for p in panes} - {y0})
    if len(xs) > 1 or len(ys) > 1:
        return None
    # One column divider and one row divider at most; an absent one is never consulted.
    cx = xs[0] if xs else (x0 + x1) // 2
    cy = ys[0] if ys else (y0 + y1) // 2
    full = (x0, y0, x1, y1)
    left, right = (x0, y0, cx, y1), (cx, y0, x1, y1)
    top, bottom = (x0, y0, x1, cy), (x0, cy, x1, y1)
    tl, bl, tr, br = (x0, y0, cx, cy), (x0, cy, cx, y1), (cx, y0, x1, cy), (cx, cy, x1, y1)
    patterns = (([full], 0, "right"), ([left, right], 1, "down"),
                ([left, tr, br], 0, "down"), ([tl, bl, right], 2, "down"),
                ([top, bottom], 0, "right"), ([tl, bottom, tr], 1, "right"),
                ([top, bl, br], 0, "right"))
    for expected, index, direction in patterns:
        if len(expected) != len(panes):
            continue
        if all(abs(pane["rect"]["x"] - ax) <= 1 and abs(pane["rect"]["y"] - ay) <= 1
               and abs(pane["rect"]["x"] + pane["rect"]["width"] - bx) <= 1
               and abs(pane["rect"]["y"] + pane["rect"]["height"] - by) <= 1
               for pane, (ax, ay, bx, by) in zip(panes, expected)):
            return panes[index]["pane_id"], direction
    return None


def compose_command(argv, cwd=None, env=None):
    """One shell-inert line: what the pane runs, and what a person can type instead.

    Like OpenRig's view composer, this is pure and every surface uses it unchanged.
    """
    words = [str(arg) for arg in argv or ()]
    if not words:
        raise ValueError("a command is required")
    assignments = []
    for name, value in sorted((env or {}).items()):
        if not _ENV_NAME.fullmatch(str(name)):
            raise ValueError(f"not an environment variable name: {name}")
        assignments.append(f"{name}={shlex.quote(str(value))}")
    line = " ".join(assignments + [shlex.join(words)])
    return f"cd {shlex.quote(str(cwd))} && {line}" if cwd else line


class HerdrSurface:
    """Herdr's pane API through ``runner(argv) -> CompletedProcess``."""

    name = "herdr"

    def __init__(self, runner=None, session=None):
        self.runner = runner or _run
        self.session = session or None

    def _argv(self, *args):
        return ["herdr"] + (["--session", self.session] if self.session else []) + [str(a) for a in args]

    def _result(self, *args):
        try:
            proc = self.runner(self._argv(*args))
            payload = json.loads(proc.stdout or "")
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            return {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if proc.returncode or not isinstance(result, dict) or payload.get("error"):
            return {}
        return result

    def _ok(self, *args):
        try:
            proc = self.runner(self._argv(*args))
        except (OSError, subprocess.SubprocessError):
            return False
        try:
            payload = json.loads(proc.stdout or "{}")
        except ValueError:
            payload = {}
        return proc.returncode == 0 and not (isinstance(payload, dict) and payload.get("error"))

    def lock_key(self, workspace):
        # Byte-identical to the peer-start claim, which a pinned older release still takes.
        return hashlib.sha256(f"{self.session or 'default'}:{workspace}".encode()).hexdigest()

    def workspace_of(self, pane):
        return pane.split(":")[0] or None

    def layout(self, pane):
        layout = self._result("pane", "layout", "--pane", pane).get("layout")
        if not isinstance(layout, dict):
            return None
        panes, workspace = layout.get("panes"), layout.get("workspace_id")
        if (not isinstance(workspace, str) or not workspace.strip()
                or not isinstance(layout.get("tab_id"), str) or not isinstance(panes, list)
                or not panes or any(not isinstance(p, dict) or not isinstance(p.get("pane_id"), str)
                                    or not p["pane_id"].strip() for p in panes)):
            return None
        ids = [p["pane_id"] for p in panes]
        if (pane not in ids or len(ids) != len(set(ids))
                or not layout["tab_id"].startswith(workspace + ":")
                or any(not pane_id.startswith(workspace + ":") for pane_id in ids)):
            return None
        return layout

    def tabs(self, workspace):
        tabs = self._result("tab", "list", "--workspace", workspace).get("tabs")
        if not isinstance(tabs, list) or any(
                not isinstance(tab, dict) or tab.get("workspace_id") != workspace
                or not isinstance(tab.get("tab_id"), str)
                or not tab["tab_id"].startswith(workspace + ":")
                or type(tab.get("number")) is not int for tab in tabs):
            return None
        return tabs

    def panes(self, workspace):
        panes = self._result("pane", "list", "--workspace", workspace).get("panes")
        if (not isinstance(panes, list)
                or any(not isinstance(p, dict) or p.get("workspace_id") != workspace
                       or not isinstance(p.get("pane_id"), str)
                       or not p["pane_id"].startswith(workspace + ":") for p in panes)
                or len({p["pane_id"] for p in panes}) != len(panes)):
            return None
        return panes

    def pane_ids(self, workspace):
        panes = self.panes(workspace)
        return None if panes is None else {p["pane_id"] for p in panes}

    def _agentless(self, pane):
        block = self._result("pane", "get", pane).get("pane")
        return isinstance(block, dict) and not block.get("agent")

    def _shell_foreground(self, pane):
        info = self._result("pane", "process-info", "--pane", pane).get("process_info")
        if not isinstance(info, dict) or info.get("pane_id") != pane:
            return False
        shell, group = info.get("shell_pid"), info.get("foreground_process_group_id")
        processes = info.get("foreground_processes")
        return (type(shell) is int and shell > 0 and group == shell
                and isinstance(processes, list) and bool(processes)
                and all(isinstance(item, dict) and item.get("pid") == shell for item in processes))

    def empty_shell(self, pane):
        """No agent, the shell itself in the foreground, and a prompt on screen — twice."""
        return (self._agentless(pane) and self._shell_foreground(pane)
                and self._ok("pane", "wait-output", pane, "--regex", _PROMPT,
                             "--source", "visible", "--timeout", "1")
                and self._agentless(pane) and self._shell_foreground(pane))

    def _created(self, key, avoid, *args):
        block = self._result(*args).get(key)
        pane = block.get("pane_id") if isinstance(block, dict) else None
        if not isinstance(pane, str) or not pane.strip() or pane == avoid or block.get("focused") is not False:
            return None
        return pane

    def split(self, pane, direction, cwd=None):
        return self._created("pane", pane, "pane", "split", "--pane", pane, "--direction", direction,
                             "--ratio", "0.5", "--no-focus", *(["--cwd", cwd] if cwd else []))

    def new_tab(self, workspace, cwd=None):
        return self._created("root_pane", None, "tab", "create", "--workspace", workspace,
                             "--no-focus", *(["--cwd", cwd] if cwd else []))

    def run(self, pane, command):
        return self._ok("pane", "run", pane, command)

    def annotate(self, pane, title, labels):
        # Herdr replaces a source's whole record, so a reused shell's old title is overwritten
        # and the agent projection's own `herdr:<harness>` record is left alone.
        renamed = not title or self._ok("pane", "rename", pane, title)
        report = ["pane", "report-metadata", pane, "--source", "hearting:pane"]
        report += ["--title", title] if title else []
        for name, value in labels.items():
            report += ["--token", f"{name}={value}"]
        return self._ok(*report) and renamed


class TmuxSurface:
    """The tmux server ``$TMUX`` names, through ``runner(argv) -> CompletedProcess``."""

    name = "tmux"

    def __init__(self, runner=None, socket=None):
        self.runner = runner or _run
        self.socket = socket or None

    def _tmux(self, *args):
        argv = ["tmux"] + (["-S", self.socket] if self.socket else []) + [str(a) for a in args]
        try:
            proc = self.runner(argv)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def _rows(self, width, *args):
        out = self._tmux(*args)
        if out is None:
            return None
        rows = [line.split("\t") for line in out.splitlines() if line]
        return rows if all(len(row) == width for row in rows) else None

    def _describe(self, pane, *fields):
        rows = self._rows(1 + len(fields), "display-message", "-p", "-t", pane,
                          "\t".join(["#{pane_id}", *fields]))
        return rows[0][1:] if rows and len(rows) == 1 and rows[0][0] == pane else None

    def lock_key(self, session):
        return hashlib.sha256(f"tmux:{self.socket}:{session}".encode()).hexdigest()

    def workspace_of(self, pane):
        found = self._describe(pane, "#{session_id}")
        return found[0] if found else None

    def layout(self, pane):
        found = self._describe(pane, "#{session_id}", "#{window_id}", "#{window_zoomed_flag}")
        if not found:
            return None
        session, window, zoomed = found
        rows = self._rows(5, "list-panes", "-t", window, "-F",
                          "#{pane_id}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}")
        try:
            panes = [{"pane_id": row[0], "rect": dict(zip(("x", "y", "width", "height"), map(int, row[1:])))}
                     for row in rows or ()]
        except ValueError:
            return None
        ids = [p["pane_id"] for p in panes]
        if pane not in ids or len(ids) != len(set(ids)):
            return None
        # Pane rects sit inside borders and status lines, so their union is the grid area.
        x0 = min(p["rect"]["x"] for p in panes)
        y0 = min(p["rect"]["y"] for p in panes)
        x1 = max(p["rect"]["x"] + p["rect"]["width"] for p in panes)
        y1 = max(p["rect"]["y"] + p["rect"]["height"] for p in panes)
        return {"workspace_id": session, "tab_id": window, "zoomed": zoomed == "1", "panes": panes,
                "area": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}}

    def tabs(self, session):
        rows = self._rows(2, "list-windows", "-t", session, "-F", "#{window_id}\t#{window_index}")
        try:
            return None if rows is None else [
                {"tab_id": window, "workspace_id": session, "number": int(index)} for window, index in rows]
        except ValueError:
            return None

    def panes(self, session):
        rows = self._rows(2, "list-panes", "-s", "-t", session, "-F", "#{pane_id}\t#{window_id}")
        if rows is None or len({row[0] for row in rows}) != len(rows):
            return None
        return [{"pane_id": pane, "tab_id": window, "workspace_id": session} for pane, window in rows]

    def pane_ids(self, session):
        panes = self.panes(session)
        return None if panes is None else {p["pane_id"] for p in panes}

    def empty_shell(self, pane):
        """The shell owns the terminal's foreground group and its last line is a prompt."""
        found = self._describe(pane, "#{pane_pid}", "#{pane_current_command}")
        if not found or found[1] not in _SHELLS:
            return False
        try:
            pid = int(found[0])
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(stat[5]) != pid:
                return False
        except (OSError, ValueError, IndexError):
            return False
        lines = [line.rstrip() for line in (self._tmux("capture-pane", "-p", "-t", pane) or "").splitlines()
                 if line.strip()]
        return bool(lines) and _PROMPT_LINE.search(lines[-1]) is not None

    def _created(self, out, avoid):
        pane = (out or "").strip()
        return pane if pane and pane != avoid else None

    def split(self, pane, direction, cwd=None):
        return self._created(self._tmux(
            "split-window", "-d", "-t", pane, "-h" if direction == "right" else "-v", "-l", "50%",
            *(["-c", cwd] if cwd else []), "-P", "-F", "#{pane_id}"), pane)

    def new_tab(self, session, cwd=None):
        return self._created(self._tmux(
            "new-window", "-d", "-t", f"{session}:", *(["-c", cwd] if cwd else []),
            "-P", "-F", "#{pane_id}"), None)

    def run(self, pane, command):
        # Typed into the pane's shell like Herdr `pane run`, so both surfaces keep the shell after.
        return (self._tmux("send-keys", "-t", pane, "-l", command) is not None
                and self._tmux("send-keys", "-t", pane, "Enter") is not None)

    def annotate(self, pane, title, labels):
        # A program can retitle its pane; the `@hearting_*` pane options are what stays.
        ok = not title or self._tmux("select-pane", "-t", pane, "-T", title) is not None
        for name, value in labels.items():
            ok = self._tmux("set-option", "-p", "-t", pane, f"@hearting_{name}", value) is not None and ok
        return ok


def _peer_message():
    global _PEER_MESSAGE
    if _PEER_MESSAGE is None:
        spec = importlib.util.spec_from_file_location("terminal_surface_peer_message",
                                                      _HERE / "peer-message.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PEER_MESSAGE = module
    return _PEER_MESSAGE


@contextmanager
def workspace_lock(surface, workspace):
    """Serialize openings in one workspace; a process already holding it re-enters.

    The file is the peer-start claim (`peer-starts/<key>.lock`), so peer starts, plain
    command panes and older pinned releases all wait for the same layout read.
    """
    path = _peer_message().peer_state_root() / "peer-starts" / f"{surface.lock_key(workspace)}.lock"
    if path in _HELD:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _HELD.add(path)
        try:
            yield
        finally:
            _HELD.discard(path)


def _empty_shell(surface, panes):
    for pane in panes:
        if surface.empty_shell(pane["pane_id"]):
            return "reuse", pane["pane_id"], None
    return None


def _tab_cell(surface, layout):
    """One grid/shell decision for both the reference tab and its workspace peers."""
    split = None if layout.get("zoomed") else next_cell(layout)
    if split:
        return "split", split[0], split[1]
    return _empty_shell(surface, layout["panes"])


def place(surface, pane=None, workspace=None):
    """``(action, target, direction)`` or None when the inventory cannot be trusted.

    Fill the reference tab, then the newest other tab of its workspace, then add a tab.
    With only a workspace, reuse an empty shell or add a tab; nobody's pane is split.
    """
    if pane is None:
        panes = surface.panes(workspace)
        if not panes:
            return None
        return _empty_shell(surface, reversed(panes)) or ("tab", workspace, None)
    layout = surface.layout(pane)
    if layout is None:
        return None
    found = _tab_cell(surface, layout)
    if found:
        return found
    workspace = layout["workspace_id"]
    tabs = surface.tabs(workspace)
    if tabs is None:
        return None
    others = [tab for tab in tabs if tab["tab_id"] != layout["tab_id"]]
    if others:
        panes = surface.panes(workspace)
        if panes is None:
            return None
        for tab in sorted(others, key=lambda t: t["number"], reverse=True):
            first = next((p["pane_id"] for p in panes if p.get("tab_id") == tab["tab_id"]), None)
            if first is None:
                return None
            other = surface.layout(first)
            if other is None or other["workspace_id"] != workspace or other["tab_id"] != tab["tab_id"]:
                return None
            found = _tab_cell(surface, other)
            if found:
                return found
    return "tab", workspace, None


def _caller_herdr_pane(session):
    pane = os.environ.get("HERDR_PANE_ID", "")
    if not pane:
        return ""
    try:
        from pane_ownership import verified_pane
        from session_tidy import session_from_env
        harness, sid = session_from_env()
        return verified_pane(pane, harness, sid, server=session)
    except Exception:
        return ""


def caller_surface():
    """The caller's own terminal; a registered worker or a plain process has none.

    Herdr needs the shared live ownership proof, so an inherited `HERDR_PANE_ID`
    alone never opens anything. tmux needs `$TMUX` and `$TMUX_PANE` to answer.
    """
    from session_tidy import is_worker
    if is_worker():
        return None
    session = os.environ.get("AGENT_HERDR_SESSION") or None
    pane = _caller_herdr_pane(session)
    if pane:
        return Near(HerdrSurface(session=session), pane=pane)
    socket, pane = os.environ.get("TMUX", "").split(",", 1)[0], os.environ.get("TMUX_PANE", "")
    if socket and pane:
        surface = TmuxSurface(socket=socket)
        if surface.workspace_of(pane):
            return Near(surface, pane=pane)
    return None


def _labels(labels, group):
    found = {"group": group}
    for name, value in (labels or {}).items():
        if not _LABEL_NAME.fullmatch(str(name)):
            raise ValueError(f"not a pane label name: {name}")
        found[str(name)] = str(value)
    if len(found) > 16:
        raise ValueError("a pane carries at most 16 labels")
    return found


def open_pane(argv=None, *, launch=None, title=None, labels=None, group="session",
              near=None, cwd=None, env=None):
    """Open one pane and run ``argv`` in it, or call ``launch(pane, created) -> bool``.

    ``near`` passes an already-checked ``Near`` and skips the caller check. ``launch``
    runs while the workspace is still locked. ``group`` names the placement family;
    only ``"session"`` exists today. Nothing is retried or closed: a failure is a note.
    """
    if group != "session":
        raise ValueError(f"unknown pane group: {group}")
    cwd = os.path.abspath(cwd) if cwd else None
    command = compose_command(argv, cwd, env) if argv else ""
    annotate = bool(title or labels)
    labels = _labels(labels, group)
    near = near or caller_surface()
    if near is None:
        return PaneResult("none", None, "none", ["no-surface"], command)
    surface = near.surface
    workspace = near.workspace or surface.workspace_of(near.pane)
    if not workspace:
        return PaneResult(surface.name, None, "none", ["pane-layout-unavailable"], command)
    with workspace_lock(surface, workspace):
        found = place(surface, near.pane, near.workspace)
        if found is None:
            return PaneResult(surface.name, None, "none", ["pane-layout-unavailable"], command)
        action, target, direction = found
        if action == "reuse":
            pane = target
        elif action == "split":
            pane = surface.split(target, direction, cwd)
        else:
            pane = surface.new_tab(target, cwd)
        if not pane:
            failure = "pane-split-failed" if action == "split" else "pane-tab-create-failed"
            return PaneResult(surface.name, None, action, [failure], command)
        notes = []
        if annotate and not surface.annotate(pane, (title or "")[:_TITLE_MAX], labels):
            notes.append("pane-label-failed")
        launched = launch(pane, action != "reuse") if launch else (not command or surface.run(pane, command))
        if not launched:
            notes.append("pane-launch-failed")
        # One read right after opening, as OpenRig's herdr adapter does: it can show the
        # pane already gone, never that its command is running or visible.
        live = surface.pane_ids(workspace)
        if live is None:
            notes.append("pane-list-unconfirmed")
        elif pane not in live:
            notes.append("pane-exited")
        return PaneResult(surface.name, pane, action, notes, command)
