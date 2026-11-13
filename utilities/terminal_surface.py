"""Open a Hearting pane, or explicitly tidy the caller's existing panes.

``open_pane`` is the one place Hearting creates a terminal pane. It uses the
terminal the caller already sits in: a Herdr pane proven to be the caller's,
then the tmux server ``$TMUX`` names when it answers, else none
(``surface="none"`` with the command line a person can run). Placement reads
the tab's shape and fills it as a 2×2 grid; a full tab reuses an agentless empty
shell, then the newest other tab of the same workspace, then gets one new tab.
Opening never re-lays out a tab, retries, or closes a pane, and reports what the
pane list showed right after opening.

A surface only places, labels and lists panes; the command it runs is composed
separately by ``compose_command``, so every surface runs the same line.

The same surfaces carry the agent terminal operations of the core loop: who runs
in a pane (``process_info``), what it shows (``read_visible``), and one typed
input (``send_text`` / ``send_keys`` / ``prompt``). ``pane_surface`` chooses by the
pane id itself: a tmux pane id (``%N``) is tmux, anything else stays Herdr. Pane
ids are only unique per tmux server, so the core loop carries ``%N@<socket>``. tmux
has no agent API, so ``TmuxSurface.agent`` answers Herdr's ``agent get`` shape from
the pane's foreground runtime, its process-proven session and that runtime's own
turn record (Claude's session registry status, Codex's rollout lifecycle,
OpenCode's own record of that session). What no record proves stays
``unknown``, which every typing path refuses.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
from typing import Optional


_HERE = Path(__file__).resolve().parent
# A tmux pane address: the pane id, and the server socket it belongs to once known.
_TMUX_PANE_ID = re.compile(r"(%[0-9]+)(?:@(/[^\s@]+))?")
_HARNESSES = ("claude", "codex", "opencode")
_PASTE_SETTLE_SEC = 0.2   # a TUI ends its bracketed-paste read before the separate Enter
_WAIT_POLL_SEC = 0.5      # tmux has no agent event; its wait re-reads the native turn record
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
    same_tab: bool = False


@dataclass
class TidyResult:
    """The last observed tab/order; notes distinguish partial and unknown state."""

    surface: str
    tab: Optional[str] = None
    panes: list = field(default_factory=list)
    changed: bool = False
    notes: list = field(default_factory=list)

    def json(self):
        return asdict(self)


class _TidyError(Exception):
    pass


_RECT = ("x", "y", "width", "height")
_SUPERVISOR = re.compile(r"^(?:\[[^\]\r\n]+\]\s*)?⚑")


def _rect(value):
    return (isinstance(value, dict) and all(type(value.get(k)) is int for k in _RECT)
            and min(value["x"], value["y"]) >= 0 and min(value["width"], value["height"]) > 0)


def _reading(layout):
    """Validate geometry before using it to address or move any existing pane."""
    if not isinstance(layout, dict) or not _rect(layout.get("area")):
        raise _TidyError("pane-layout-invalid")
    area, panes = layout["area"], layout.get("panes")
    if (not isinstance(panes, list) or not panes or any(
            not isinstance(p, dict) or not isinstance(p.get("pane_id"), str)
            or not p["pane_id"] or not _rect(p.get("rect")) for p in panes)):
        raise _TidyError("pane-layout-invalid")
    if len({p["pane_id"] for p in panes}) != len(panes):
        raise _TidyError("pane-layout-invalid")
    for i, pane in enumerate(panes):
        r = pane["rect"]
        if any(r[k] < area[k] or r[k] + r[size] > area[k] + area[size]
               for k, size in (("x", "width"), ("y", "height"))):
            raise _TidyError("pane-layout-invalid")
        for other in panes[:i]:
            b = other["rect"]
            if all(max(r[k], b[k]) < min(r[k] + r[size], b[k] + b[size])
                   for k, size in (("x", "width"), ("y", "height"))):
                raise _TidyError("pane-layout-invalid")
    return sorted(panes, key=lambda p: (p["rect"]["y"], p["rect"]["x"]))


def _grid_rows(n):
    cols = math.isqrt(n - 1) + 1
    return [min(cols, n - i) for i in range(0, n, cols)]


def _grid_cells(area, n, border=0):
    """Integer cells for full-width rows; tmux dividers consume one cell."""
    rows, cells = _grid_rows(n), []
    height = area["height"] - border * (len(rows) - 1)
    y = area["y"]
    for i, count in enumerate(rows):
        h = height // (len(rows) - i)
        width, x = area["width"] - border * (count - 1), area["x"]
        for j in range(count):
            w = width // (count - j)
            if min(w, h) < 4 or (border == 0 and (count - j > 10 or len(rows) - i > 10)):
                raise _TidyError("pane-grid-too-small")
            cells.append(dict(zip(_RECT, (x, y, w, h))))
            width -= w
            x += w + border
        height -= h
        y += h + border
    return cells


def _grid_matches(layout, cells):
    panes = _reading(layout)
    return len(panes) == len(cells) and all(
        all(abs(p["rect"][k] - r[k]) <= 1 for k in _RECT) for p, r in zip(panes, cells))


def _tidy_order(layout, caller):
    panes = _reading(layout)
    first = next((p["pane_id"] for p in panes if any(
        _SUPERVISOR.match(p.get(key) or "") for key in ("display_agent", "title"))), caller)
    ids = [p["pane_id"] for p in panes]
    if first not in ids:
        raise _TidyError("pane-caller-unconfirmed")
    return [first] + [p for p in ids if p != first]


def supervisor_corner(layout, marks):
    """``(pane, top_left)`` for a tab's supervisor, as ``pane tidy`` would order it.

    ``marks`` maps a pane id to its headers (display agent, title). The supervisor is
    the first pane in reading order whose header carries ``⚑``; ``top_left`` says
    whether it already is the top-left pane. None when no pane is marked or the
    geometry does not read as a tiling. Read only.
    """
    try:
        panes = _reading(layout)
    except _TidyError:
        return None
    for pane in panes:
        headers = marks.get(pane["pane_id"]) or ()
        if isinstance(headers, str):
            headers = (headers,)
        if any(_SUPERVISOR.match(str(text or "")) for text in headers):
            return pane["pane_id"], pane["pane_id"] == panes[0]["pane_id"]
    return None


def _shell_record(info):
    from pane_ownership import _process
    record = _process(info.get("shell_pid")) if isinstance(info, dict) else None
    if record is None:
        raise _TidyError("pane-process-unconfirmed")
    return record[0], record[5]


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=15,
                          stdin=subprocess.DEVNULL, check=False)


def is_tmux_pane(pane):
    """tmux pane ids are ``%N`` (``%N@<socket>`` with their server); Herdr's carry a
    workspace (``w1:p2``), so they never meet."""
    return isinstance(pane, str) and _TMUX_PANE_ID.fullmatch(pane) is not None


def tmux_address(pane):
    """``(pane id, server socket or None)`` of a tmux pane address, else ``("", None)``."""
    found = _TMUX_PANE_ID.fullmatch(pane) if isinstance(pane, str) else None
    return (found.group(1), found.group(2)) if found else ("", None)


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, TypeError, ValueError):
        return path


def same_tmux_pane(left, right):
    """Two tmux addresses name one pane: the same id, and the same server where both say."""
    (a, a_socket), (b, b_socket) = tmux_address(left), tmux_address(right)
    return bool(a) and a == b and (not a_socket or not b_socket or _real(a_socket) == _real(b_socket))


def tmux_socket(environ=None):
    """The server ``$TMUX`` names, else None (tmux's own default server)."""
    env = os.environ if environ is None else environ
    return str(env.get("TMUX") or "").split(",", 1)[0].strip() or None


def caller_tmux_pane(environ=None):
    """``("%N@<socket>", socket)`` for a process started inside tmux; a hint, never proof."""
    env = os.environ if environ is None else environ
    pane = str(env.get("TMUX_PANE") or "").strip()
    socket = tmux_socket(env)
    if not socket or not is_tmux_pane(pane) or tmux_address(pane)[1]:
        return "", None
    return f"{pane}@{_real(socket)}", socket


def pane_surface(pane, *, tmux=None, herdr_session=None):
    """The surface that owns ``pane``: tmux for a tmux pane id (its own server first,
    then ``tmux``, then ``$TMUX``), else Herdr."""
    if is_tmux_pane(pane):
        if not shutil.which("tmux"):
            return None
        return TmuxSurface(socket=tmux_address(pane)[1] or tmux or tmux_socket())
    return HerdrSurface(session=herdr_session) if shutil.which("herdr") else None


def _proc_table():
    """``(pid, group, tty)`` of every live process this user owns, read once."""
    rows = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return rows
    uid = os.getuid()
    for name in names:
        if not name.isdigit():
            continue
        try:
            path = Path("/proc") / name
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X"} or path.stat().st_uid != uid:
                continue
            rows.append((int(name), int(fields[2]), int(fields[4])))
        except (OSError, ValueError, IndexError):
            continue
    return rows


def _foreground_members(group, tty, table=None):
    """Every process of one foreground group on one terminal, with its exact argv."""
    from pane_ownership import _process
    members = []
    for pid, pgrp, terminal in (_proc_table() if table is None else table):
        if pgrp != group or terminal != tty:
            continue
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            record = _process(pid)
            if (not raw.endswith(b"\0") or len(raw) > 65536 or not record
                    or record[2] != group or record[3] != tty):
                continue
            members.append({"pid": pid, "argv": [part.decode("utf-8") for part in raw[:-1].split(b"\0")]})
        except (OSError, UnicodeError):
            continue
    return members


def pane_runtime(info):
    """``(harness, pid)`` of the one outermost interactive runtime in a pane's foreground.

    Services, registered workers and anything that is not Claude/Codex/OpenCode are
    ignored; a runtime started by another runtime in the same group (a tool's child)
    belongs to the outer one. Two unrelated runtimes prove nothing.
    """
    from pane_ownership import _process, _registered_worker, _runtime
    found = {}
    for item in (info or {}).get("foreground_processes") or ():
        pid = item.get("pid") if isinstance(item, dict) else None
        if type(pid) is not int or pid <= 1:
            continue
        kind, service = _runtime(pid)
        if kind in _HARNESSES and not service and not _registered_worker(pid):
            found[pid] = kind
    outer = []
    for pid, kind in found.items():
        ancestor, depth, nested = _process(pid), 0, False
        while ancestor and depth < 64 and ancestor[1] > 1:
            if ancestor[1] in found:
                nested = True
                break
            ancestor, depth = _process(ancestor[1]), depth + 1
        if not nested:
            outer.append((kind, pid))
    return outer[0] if len(outer) == 1 else None


def shell_seat(info):
    """The session-shell attachment that owns this pane's foreground, else None."""
    try:
        import session_shell
        return session_shell.pane_seat(info)
    except Exception:  # noqa: BLE001 - an optional reader never blocks the bare path
        return None


def shell_turn(seat):
    """The turn state the shell host follows from the native lifecycle hooks."""
    try:
        import session_shell
        result = session_shell.control(seat, "status")
    except Exception:  # noqa: BLE001 - an optional reader never blocks the bare path
        return "unknown"
    state = result.get("state") if isinstance(result, dict) and result.get("ok") else None
    return {"idle": "idle", "working": "working", "submitting": "working",
            "blocked": "blocked"}.get(state, "unknown")


def _process_session(harness, pid, pane, surface):
    """The session this exact runtime process proves, else ""."""
    try:
        if harness == "codex":
            from pane_ownership import current_codex_thread
            return current_codex_thread(pane, pid, tmux=surface.socket) or ""
        import pane_ownership  # noqa: F401 - puts tools/ on the path
        from fleet.process_identity import PROVEN, process_identity
        found = process_identity(pid, harness)
        return found.session_id if found.confidence == PROVEN and found.session_id else ""
    except Exception:
        return ""


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def native_turn(harness, pid, session_id):
    """``idle`` | ``working`` | ``blocked`` | ``unknown`` from the runtime's own record.

    Claude: its session registry entry for this exact process (PID start) and
    session. Codex: the latest task lifecycle in that thread's rollout (an ended one is
    still work while its App Server runs a turn or holds a queued item); a thread with
    no rollout has no recorded turn and stays unknown. OpenCode: that session's own
    record in its database (V1 messages; OpenCode 2's run record, whose ``idle`` row
    ends a turn), read only while this exact process's TUI selection record still
    names that session.
    """
    try:
        import pane_ownership
        if harness == "opencode":
            from fleet.collectors import opencode
            if opencode.session_of_process(pid) != (session_id, "opencode-tui-selection"):
                return "unknown"
            import opencode_runtime
            return _opencode_turn(opencode_runtime.read_session_observation(
                session_id, shell_after_seq=None))
        if harness == "claude":
            from fleet.collectors import claude
            record = claude.read_registry(pid) or {}
            live = pane_ownership._process(pid)
            if (not live or str(record.get("procStart") or "") != live[5]
                    or record.get("sessionId") != session_id):
                return "unknown"
            return {"busy": "working", "idle": "idle", "waiting": "blocked"}.get(
                record.get("status"), "unknown")
        if harness == "codex" and pane_ownership._THREAD_ID_RE.fullmatch(session_id or ""):
            import glob
            from fleet.collectors import codex
            paths = glob.glob(str(_codex_home() / "sessions" / "*" / "*" / "*"
                                  / f"rollout-*-{glob.escape(session_id)}.jsonl"))
            if len(paths) != 1:
                return "unknown"
            lifecycle = codex._latest_task_lifecycle(paths[0])
            if lifecycle and lifecycle[0] == "task_started":
                return "working"
            if lifecycle and lifecycle[0] in {"task_complete", "turn_aborted"}:
                # The rollout can trail the App Server by a moment, and a queued item starts
                # right at a turn's end: where the App Server answers, that is still work.
                return "working" if _codex_server_busy(session_id) else "idle"
    except Exception:
        pass
    return "unknown"


def _codex_server_busy(thread_id):
    """The Codex App Server reports a running turn or a queued item for this thread.

    Unknown (no socket, an RPC error, a thread it does not hold) is not busy, so the
    rollout's own judgment stands.
    """
    try:
        import codex_queue_delivery
        from codex_queue_dispatch import resolve_queue_socket
        endpoint = resolve_queue_socket()
        if not endpoint.is_socket():
            return False
        if codex_queue_delivery._latest_turn_status(endpoint, thread_id, timeout=1.0) in {
                "inProgress", "in_progress", "running"}:
            return True
        return bool(codex_queue_delivery.list_queue(endpoint, thread_id, timeout=1.0))
    except Exception:  # noqa: BLE001 - an optional reader never blocks the rollout path
        return False


def _opencode_turn(observed):
    """One OpenCode session's turn state from ``read_session_observation``.

    V1: the newest user or assistant message decides. OpenCode 2 records each run
    itself, so its newest user, synthetic, assistant, compaction, shell or ``idle``
    row decides: the ``idle`` row it appends when a run ends is idle; a user or
    synthetic row, or a compaction (it runs inside a run, which only its ``idle`` row
    ends), is working; a ``!`` shell command runs outside any run, so its row is
    working while ``running`` and, once ended, leaves the row before it to decide.
    An assistant step has ended the turn when it finished for good -- a ``finish``
    other than ``tool-calls``/``unknown``, or an error -- unless OpenCode 2 has a
    retry pending on it; a step still running or about to call tools is working,
    and a running ``question`` tool waits on the person. A child (subagent) session,
    whose state is not the pane's, an unread session, or one with no message, is
    unknown.
    """
    if not isinstance(observed, dict) or observed.get("state") != "observed":
        return "unknown"
    if (observed.get("session") or {}).get("parent_id"):
        return "unknown"
    kinds = (("user", "synthetic", "assistant", "compaction", "shell", "idle")
             if observed.get("generation") == "v2" else ("user", "assistant"))
    messages = [row for row in observed.get("messages") or ()
                if isinstance(row, dict) and row.get("type") in kinds
                and (row["type"] != "shell" or row.get("status") == "running")]
    if not messages:
        return "unknown"
    latest = messages[-1]
    if latest["type"] == "idle":
        return "idle"
    if latest["type"] != "assistant" or latest.get("has_retry"):
        return "working"
    if any(isinstance(tool, dict) and tool.get("name") == "question"
           for tool in latest.get("tools") or ()):
        return "blocked"
    finish = latest.get("finish")
    if latest.get("has_error") or (isinstance(finish, str) and finish
                                   and finish not in ("tool-calls", "unknown")):
        return "idle"
    return "working"


def _cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


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
    """One shell-inert line: what the pane runs, and what a person can type instead."""
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

    def process_info(self, pane):
        info = self._result("pane", "process-info", "--pane", pane).get("process_info")
        return info if isinstance(info, dict) and info.get("pane_id") == pane else None

    def tidy_snapshot(self, pane):
        layout = self.layout(pane)
        ordered = _reading(layout)
        workspace, tab = layout["workspace_id"], layout["tab_id"]
        tabs, inventory = self.tabs(workspace), self.panes(workspace)
        context = self._result("workspace", "get", workspace).get("workspace")
        named = next((t for t in tabs or [] if t["tab_id"] == tab), None)
        if (not named or not isinstance(context, dict) or context.get("workspace_id") != workspace
                or type(context.get("focused")) is not bool or not isinstance(context.get("label"), str)
                or not isinstance(named.get("label"), str) or inventory is None
                or {p["pane_id"] for p in inventory if p.get("tab_id") == tab}
                != {p["pane_id"] for p in ordered}
                or layout.get("focused_pane_id") not in {p["pane_id"] for p in ordered}):
            raise _TidyError("pane-snapshot-unconfirmed")
        identity = {}
        for p in ordered:
            pid = p["pane_id"]
            metadata = self._result("pane", "get", pid).get("pane")
            if (not isinstance(metadata, dict) or metadata.get("pane_id") != pid
                    or metadata.get("workspace_id") != workspace or metadata.get("tab_id") != tab
                    or not isinstance(metadata.get("terminal_id"), str) or not metadata["terminal_id"]):
                raise _TidyError("pane-identity-unconfirmed")
            identity[pid] = (metadata["terminal_id"], *_shell_record(self.process_info(pid)))
            for key in ("display_agent", "title"):
                p[key] = metadata.get(key)
        return {**layout, "identity": identity, "names": (context["label"], named["label"]),
                "active": (context["focused"], context.get("active_tab_id")),
                "tabs": {t["tab_id"] for t in tabs}}

    def move(self, pane, *, tab=None, target=None, direction="right", ratio=.5, workspace=None):
        destination = (["--tab", tab, "--target-pane", target, "--split", direction,
                        "--ratio", format(ratio, ".9g")] if tab else ["--new-tab", "--workspace", workspace])
        return self._result("pane", "move", pane, *destination, "--no-focus").get("move_result", {})

    def resize(self, pane, direction, amount):
        return self._result("pane", "resize", "--pane", pane, "--direction", direction,
                            "--amount", format(amount, ".9g")).get("resize", {})

    def swap(self, source, target):
        return self._result("pane", "swap", "--source-pane", source,
                            "--target-pane", target).get("swap", {})


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

    def _target(self, pane):
        """The bare id of a pane on this server; an address naming another server is None."""
        bare, socket = tmux_address(pane)
        if not bare or (socket and self.socket and _real(socket) != _real(self.socket)):
            return None
        return bare

    def _tmux_stdin(self, data, *args):
        argv = ["tmux"] + (["-S", self.socket] if self.socket else []) + [str(a) for a in args]
        try:
            proc = subprocess.run(argv, input=data, capture_output=True, text=True, timeout=15,
                                  check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def _describe(self, pane, *fields):
        pane = self._target(pane)
        if pane is None:
            return None
        rows = self._rows(1 + len(fields), "display-message", "-p", "-t", pane,
                          "\t".join(["#{pane_id}", *fields]))
        return rows[0][1:] if rows and len(rows) == 1 and rows[0][0] == pane else None

    def address(self, pane):
        """``%N@<socket>``: the pane id with the server that answers for it, else None."""
        found = self._describe(pane, "#{socket_path}")
        return f"{self._target(pane)}@{_real(found[0])}" if found and found[0] else None

    def input_refusal(self, pane):
        """Why typed input must not reach this pane now, else None.

        A pane in a tmux mode (copy, choose-client, tree...) gives keys to the mode,
        not to the program, and drops the paste brackets; a synchronized pane copies
        keys to its neighbours; input-off ignores them. Unknown state refuses too.
        """
        found = self._describe(pane, "#{pane_in_mode}", "#{pane_input_off}",
                               "#{pane_synchronized}", "#{pane_dead}")
        if not found:
            return "pane-unavailable"
        in_mode, off, synchronized, dead = found
        if dead != "0":
            return "pane-dead" if dead == "1" else "pane-state-unknown"
        if in_mode != "0":
            return "pane-in-mode" if in_mode.isdigit() else "pane-state-unknown"
        if off != "0":
            return "pane-input-off" if off == "1" else "pane-state-unknown"
        if synchronized != "0":
            return "pane-synchronized" if synchronized == "1" else "pane-state-unknown"
        return None

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
        # A zoomed window reports overlapping rects; it is irregular, never a grid.
        x0 = min(p["rect"]["x"] for p in panes)
        y0 = min(p["rect"]["y"] for p in panes)
        x1 = max(p["rect"]["x"] + p["rect"]["width"] for p in panes)
        y1 = max(p["rect"]["y"] + p["rect"]["height"] for p in panes)
        return {"workspace_id": session, "tab_id": window, "irregular": zoomed == "1", "panes": panes,
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
        if self.input_refusal(pane):
            return False
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
        lines = [line.rstrip() for line in (self._tmux("capture-pane", "-p", "-t", self._target(pane))
                                            or "").splitlines()
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
        # Written into the pane's shell like Herdr `pane run`, so both surfaces keep the shell
        # after. A paste reaches this pane only; `send-keys` would also reach synchronized
        # neighbours, so the line and its Enter both go through private buffers.
        return (not self.input_refusal(pane) and self._paste(pane, command, bracketed=False) == "pasted"
                and self._paste(pane, "\r", bracketed=False) == "pasted")

    def annotate(self, pane, title, labels):
        # A program can retitle its pane; the `@hearting_*` pane options are what stays.
        # `select-pane -T` expands formats, so a literal `#` is doubled.
        pane = self._target(pane)
        if pane is None:
            return False
        ok = not title or self._tmux("select-pane", "-t", pane, "-T", title.replace("#", "##")) is not None
        for name, value in labels.items():
            ok = self._tmux("set-option", "-p", "-t", pane, f"@hearting_{name}", value) is not None and ok
        return ok

    def process_info(self, pane, *, table=None):
        """Herdr's ``pane process-info`` shape, read from tmux and the kernel.

        tmux names the pane's root process and terminal; the kernel names that
        terminal's foreground group. Every live process of that group on that
        terminal is listed with its argv. The root, its terminal and the group must
        read the same before and after the listing, else nothing is claimed.
        """
        found = self._describe(pane, "#{pane_pid}", "#{pane_tty}")
        if not found:
            return None
        try:
            from pane_ownership import _process
            root, tty = int(found[0]), found[1]
            before = _process(root)
            if (not before or not before[3] or before[4] <= 0 or not tty.startswith("/dev/")
                    or os.stat(tty).st_rdev != before[3]):
                return None
            members = _foreground_members(before[4], before[3], table)
            if not members or _process(root) != before:
                return None
        except (OSError, ValueError, TypeError):
            return None
        return {"pane_id": pane, "shell_pid": root, "tty": tty,
                "foreground_process_group_id": before[4], "foreground_processes": members}

    def read_visible(self, pane, *, ansi=False):
        """The pane's visible screen; ``ansi`` keeps SGR, so faint text stays faint."""
        pane = self._target(pane)
        if pane is None:
            return None
        return self._tmux("capture-pane", "-p", *(["-e"] if ansi else []), "-t", pane)

    def send_text(self, pane, text):
        """Paste ``text`` once, bracketed when the program asked for it; no Enter."""
        return not self.input_refusal(pane) and self._paste(pane, text) == "pasted"

    def _paste(self, pane, text, *, bracketed=True):
        """Write ``text`` to this pane only, through a private buffer read from stdin.

        stdin keeps every byte (an argument loses a trailing ``;`` to tmux's command
        parser and stops near 16 KB). ``-p`` brackets the paste when the program asked.
        """
        pane = self._target(pane)
        if pane is None or not isinstance(text, str) or not text:
            return "not-started"
        name = f"hearting-{os.getpid()}-{secrets.token_hex(6)}"
        if self._tmux_stdin(text, "load-buffer", "-b", name, "-") is None:
            return "not-started"
        if self._tmux("paste-buffer", *(["-p"] if bracketed else []), "-d", "-b", name, "-t", pane) is None:
            self._tmux("delete-buffer", "-b", name)
            return "failed"
        return "pasted"

    def send_keys(self, pane, *keys):
        target = self._target(pane)
        return (target is not None and not self.input_refusal(pane)
                and self._tmux("send-keys", "-t", target, *keys) is not None)

    def prompt(self, pane, text):
        """One typed input in ``herdr agent prompt``'s ``(rc, payload)`` shape.

        Nothing is typed into a pane that is in a tmux mode, synchronized or has input
        off; that, or a buffer tmux never accepted, is ``herdr-not-started`` (nothing
        reached the pane, so a carrier may retry it). The Enter is a one-byte paste to
        this pane only, after the same check; a failed paste or Enter may already have
        typed and stays an error that is never repeated.
        """
        refusal = self.input_refusal(pane)
        if refusal:
            return None, {"error": {"code": "herdr-not-started", "reason": refusal}}
        pasted = self._paste(pane, text)
        if pasted == "not-started":
            return None, {"error": {"code": "herdr-not-started"}}
        if pasted != "pasted":
            return 1, {"error": {"code": "tmux_paste_failed"}}
        time.sleep(_PASTE_SETTLE_SEC)
        refusal = self.input_refusal(pane)
        if refusal:
            return 1, {"error": {"code": "tmux_" + refusal.replace("-", "_")}}
        if self._paste(pane, "\r", bracketed=False) != "pasted":
            return 1, {"error": {"code": "tmux_enter_failed"}}
        return 0, {"result": {"type": "prompt", "pane_id": pane}}

    def set_title(self, pane, title):
        return self.annotate(pane, str(title), {})

    def answers(self):
        return self._tmux("display-message", "-p", "#{pid}") is not None

    def notify(self, text, *, seconds=10):
        """Show ``text`` once on every client attached to this server; display only.

        Herdr's ``notification show`` counterpart: a status-line message, so nothing is
        typed and no pane, focus, title or option changes. ``#`` is doubled so a
        question is never read as a tmux format. True when a client was there to show it.
        """
        rows = self._rows(1, "list-clients", "-F", "#{client_name}")
        message = str(text or "").replace("#", "##")
        if not rows or not message:
            return False
        shown = False
        for (client,) in rows[:16]:
            ok = self._tmux("display-message", "-c", client, "-d", int(seconds * 1000),
                            "--", message) is not None
            if not ok:  # tmux before 3.2 has no -d; its own display-time applies
                ok = self._tmux("display-message", "-c", client, "--", message) is not None
            shown = ok or shown
        return shown

    def agent(self, pane, *, table=None):
        """``herdr agent get``'s payload for one tmux pane; None when tmux cannot answer.

        The agent is the one outermost Claude/Codex/OpenCode runtime among the pane's
        foreground processes; its session is the one that process proves; its state
        is that runtime's own turn record for that session (``unknown`` without one).
        """
        if self._target(pane) is None:
            return {"error": {"code": "agent_not_found"}}
        info = self.process_info(pane, table=table)
        if info is None:
            return {"error": {"code": "agent_not_found"}} if self.answers() else None
        found = pane_runtime(info)
        seat = shell_seat(info) if found is None else None
        if found is None and seat is None:
            return {"error": {"code": "agent_not_found"}}
        if seat is not None:
            # The foreground is the shell's PTY owner; its attachment names the
            # native runtime and session behind it (the pane's seat).
            harness, pid = seat["harness"], seat["native_pid"]
            session_id = "" if seat["sid"] == "-" else seat["sid"]  # "-": no session selected
        else:
            harness, pid = found
            session_id = _process_session(harness, pid, pane, self)
        status = native_turn(harness, pid, session_id) if session_id else "unknown"
        if seat is not None and status == "unknown":
            # The shell host follows the native lifecycle hooks for every harness.
            status = shell_turn(seat)
        agent = {"pane_id": pane, "agent": harness, "pid": pid,
                 "agent_status": status,
                 "name": self._started_name(pane, pid), "cwd": _cwd(pid)}
        if session_id:
            agent["agent_session"] = {"kind": "id", "value": session_id}
        return {"result": {"type": "agent_info", "agent": agent}}

    def _started_name(self, pane, pid):
        """The name ``peer-steward start`` gave this exact runtime process, else None.

        The name is bound to the runtime's PID and start time, so a later program in
        the same pane never inherits it.
        """
        from pane_ownership import _process
        found = self._describe(pane, "#{@hearting_name}", "#{@hearting_name_pid}")
        live = _process(pid)
        if not found or not found[0] or not live or found[1] != f"{pid}:{live[5]}":
            return None
        return found[0]

    def name_started(self, pane, pid, name):
        """Bind ``name`` to the runtime ``pid`` now in ``pane`` (pane options only)."""
        from pane_ownership import _process
        live = _process(pid)
        return bool(live) and self.annotate(pane, None, {"name": str(name),
                                                         "name_pid": f"{pid}:{live[5]}"})

    def close(self, pane):
        target = self._target(pane)
        return target is not None and self._tmux("kill-pane", "-t", target) is not None

    def agents(self):
        """``herdr agent list``'s payload over every pane of this tmux server."""
        rows = self._rows(1, "list-panes", "-a", "-F", "#{pane_id}")
        if rows is None:
            return None
        table, found = _proc_table(), []
        socket = self._tmux("display-message", "-p", "#{socket_path}")
        if not socket or not socket.strip():
            return None
        for (pane,) in rows:
            payload = self.agent(f"{pane}@{_real(socket.strip())}", table=table)
            if payload is None:
                return None
            agent = (payload.get("result") or {}).get("agent")
            if isinstance(agent, dict):
                found.append(agent)
        return {"result": {"agents": found}}

    def wait_agent(self, pane, until=(), timeout_ms=None):
        """``herdr agent wait``: the agent once its state is in ``until`` (a change of
        state when ``until`` is empty), an error payload, or None.

        tmux sends no agent event, so the runtime's own turn record is re-read at a
        fixed interval inside the bound; a changed foreground process re-reads all.
        """
        deadline = None if timeout_ms is None else time.monotonic() + max(0, timeout_ms) / 1000
        first = None
        while True:
            payload = self.agent(pane)
            agent = (payload or {}).get("result", {}).get("agent") if isinstance(payload, dict) else None
            if not isinstance(agent, dict):
                return payload
            state = agent["agent_status"]
            if (state in until) if until else (first is not None and state != first):
                return payload
            first = state if first is None else first
            from pane_ownership import _process
            record = _process(agent["pid"])
            session = (agent.get("agent_session") or {}).get("value")
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return {"error": {"code": "timeout"}}
                time.sleep(_WAIT_POLL_SEC if deadline is None
                           else max(0.0, min(_WAIT_POLL_SEC, deadline - time.monotonic())))
                if _process(agent["pid"]) != record:
                    break
                current = native_turn(agent["agent"], agent["pid"], session) if session else "unknown"
                if current != state:
                    break

    def tidy_snapshot(self, pane):
        found = self._describe(pane, "#{session_id}", "#{window_id}", "#{window_zoomed_flag}",
                               "#{window_width}", "#{window_height}", "#{window_layout}",
                               "#{window_name}", "#{session_name}", "#{pane-border-status}")
        if not found:
            raise _TidyError("pane-snapshot-unconfirmed")
        session, window, zoomed, width, height, original, name, session_name, border = found
        rows = self._rows(8, "list-panes", "-t", window, "-F", "\t".join([
            "#{pane_id}", "#{pane_left}", "#{pane_top}", "#{pane_width}", "#{pane_height}",
            "#{pane_active}", "#{pane_pid}", "#{pane_title}"]))
        try:
            width, height = int(width), int(height)
            panes, identity, focus = [], {}, []
            for pid, x, y, w, h, active, process, title in rows or []:
                x, y, w, h = map(int, (x, y, w, h))
                if border == "top" and y == 1:
                    y, h = 0, h + 1
                elif border == "bottom" and y + h == height - 1:
                    h += 1
                panes.append({"pane_id": pid, "rect": dict(zip(_RECT, (x, y, w, h))), "title": title})
                identity[pid] = _shell_record({"shell_pid": int(process)})
                if active == "1":
                    focus.append(pid)
        except (ValueError, TypeError):
            raise _TidyError("pane-snapshot-unconfirmed") from None
        active = self._tmux("display-message", "-p", "-t", session, "#{window_id}")
        tabs = self.tabs(session)
        layout = {"workspace_id": session, "tab_id": window, "zoomed": zoomed == "1",
                  "area": dict(zip(_RECT, (0, 0, width, height))), "panes": panes,
                  "focused_pane_id": focus[0] if len(focus) == 1 else None,
                  "identity": identity, "names": (session_name, name),
                  "active": active.strip() if active is not None else None,
                  "tabs": {t["tab_id"] for t in tabs or []}, "original_layout": original}
        if (pane not in identity or len(focus) != 1 or active is None or tabs is None
                or any(not re.fullmatch(r"%[0-9]+", p["pane_id"]) for p in panes)):
            raise _TidyError("pane-snapshot-unconfirmed")
        if not layout["zoomed"]:
            _reading(layout)
        return layout


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
    split = None if layout.get("irregular") else next_cell(layout)
    if split:
        return "split", split[0], split[1]
    return _empty_shell(surface, layout["panes"])


def place(surface, pane=None, workspace=None, *, same_tab=False):
    """``(action, target, direction)`` or None when the inventory cannot be trusted.

    Fill the reference tab, then the newest other tab of its workspace, then add a tab.
    With only a workspace, reuse an empty shell or add a tab; nobody's pane is split.
    ``same_tab`` keeps placement inside the reference tab: when it has no grid cell or
    empty shell left, split the reference pane itself instead of reading other tabs.
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
    if same_tab:
        rect = next((p.get("rect") for p in layout["panes"] if p.get("pane_id") == pane), None)
        keys = ("x", "y", "width", "height")
        if not isinstance(rect, dict) or any(type(rect.get(k)) is not int for k in keys):
            return None
        return "split", pane, "right" if rect["width"] >= 2 * rect["height"] else "down"
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


def _tidy_caller():
    """Tidy alone accepts a proven foreground shell job as well as an agent."""
    from pane_ownership import verified_terminal_caller
    session = os.environ.get("AGENT_HERDR_SESSION") or None
    surface = HerdrSurface(session=session)
    pane = os.environ.get("HERDR_PANE_ID", "")
    proven = _caller_herdr_pane(session) if pane else ""
    if proven:
        return Near(surface, pane=proven)
    if pane and verified_terminal_caller(surface.process_info(pane)):
        return Near(surface, pane=pane)
    socket, pane = os.environ.get("TMUX", "").split(",", 1)[0], os.environ.get("TMUX_PANE", "")
    if socket and pane:
        surface = TmuxSurface(socket=socket)
        if verified_terminal_caller(surface.process_info(pane)):
            return Near(surface, pane=pane)
    return None


@contextmanager
def _tidy_signals(cleanup=False):
    """One command lifetime, restoring the caller's handlers on every exit."""
    previous = {}

    def interrupted(signum, _frame):
        raise _TidyError(f"pane-tidy-interrupted:{signal.Signals(signum).name}")
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old = signal.signal(sig, signal.SIG_IGN if cleanup else interrupted)
                previous[sig] = old
            except ValueError:  # an embedded caller outside the main thread
                break
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _same_snapshot(base, current):
    for key in ("workspace_id", "tab_id", "area", "zoomed", "identity", "focused_pane_id", "names", "active", "tabs"):
        if base[key] != current[key]:
            raise _TidyError(f"pane-state-changed:{key}")


def _split_rect(rect, direction, ratio):
    axis, size = ("x", "width") if direction == "right" else ("y", "height")
    length = int(rect[size] * ratio + .5)
    return ({**rect, size: length}, {**rect, axis: rect[axis] + length, size: rect[size] - length})


def _canonical_splits(layout):
    """Recognize the row/remaining-columns tree, independent of split ratios.

    An unfamiliar tree is rebuilt instead of guessing which divider resize will
    affect. Each recognized boundary has a first-child pane and second-child
    pane touching it, matching Herdr's nearest-edge resize semantics.
    """
    panes, splits, out = _reading(layout), layout.get("splits"), []
    if not isinstance(splits, list):
        return None

    def walk(area, counts):
        if counts == [1]:
            matches = [p for p in panes if p["rect"] == area]
            return len(matches) == 1
        direction = "down" if len(counts) > 1 else "right"
        found = [s for s in splits if s.get("rect") == area and s.get("direction") == direction]
        if len(found) != 1:
            return False
        split = found[0]
        ratio = split.get("ratio")
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or not .1 <= ratio <= .9:
            return False
        first, second = _split_rect(area, direction, ratio)
        desired = 1 / (len(counts) if len(counts) > 1 else counts[0])
        out.append((split, desired, first, second))
        return (walk(first, counts[:1] if len(counts) > 1 else [1])
                and walk(second, counts[1:] if len(counts) > 1 else [counts[0] - 1]))
    return out if walk(layout["area"], _grid_rows(len(panes))) and len(out) == len(splits) else None


def _layout_state(layout):
    return (layout.get("workspace_id"), layout.get("tab_id"), layout.get("area"),
            layout.get("focused_pane_id"), layout.get("zoomed"), layout.get("splits"),
            [(p["pane_id"], p["rect"]) for p in _reading(layout)])


class _HerdrTidy:
    """One finite rearrangement; its staging and obligations die with the call."""

    def __init__(self, surface, base, result):
        self.surface, self.base, self.result = surface, base, result
        self.workspace, self.tab, self.focus = (base[k] for k in ("workspace_id", "tab_id", "focused_pane_id"))
        self.expected, self.staging, self.pending = base, None, None
        self.locations = {p: self.tab for p in base["identity"]}

    def check(self):
        live = self.surface.layout(self.focus)
        context = self.surface._result("workspace", "get", self.workspace).get("workspace", {})
        if (not live or _layout_state(live) != _layout_state(self.expected)
                or context.get("focused") is not True or context.get("active_tab_id") != self.tab
                or context.get("label") != self.base["names"][0]):
            raise _TidyError("pane-layout-changed")

    def bound(self, layout, tab, ids):
        if (not isinstance(layout, dict) or layout.get("workspace_id") != self.workspace
                or layout.get("tab_id") != tab or layout.get("area") != self.base["area"]
                or layout.get("zoomed") is not False
                or {p["pane_id"] for p in _reading(layout)} != set(ids)
                or (tab == self.tab and layout.get("focused_pane_id") != self.focus)):
            raise _TidyError("pane-operation-unconfirmed")
        return layout

    def move(self, pane, *, tab=None, target=None, direction="right", ratio=.5, cleanup=False):
        if not cleanup:
            self.check()
        previous = self.locations[pane]
        self.pending = pane
        was_changed = self.result.changed
        self.result.changed = True
        response = self.surface.move(pane, tab=tab, target=target, direction=direction,
                                     ratio=ratio, workspace=self.workspace)
        if not isinstance(response, dict):
            raise _TidyError("pane-move-unconfirmed")
        if response.get("changed") is False:
            self.result.changed, self.pending = was_changed, None
            raise _TidyError("pane-move-unchanged")
        created = response.get("created_tab")
        if tab is None and isinstance(created, dict):
            candidate = created.get("tab_id")
            if (isinstance(candidate, str) and candidate.startswith(self.workspace + ":")
                    and candidate not in self.base["tabs"] and created.get("workspace_id") == self.workspace):
                self.staging = candidate
        tab = tab or self.staging
        metadata = response.get("pane", {})
        if (response.get("changed") is not True or not tab
                or response.get("previous_pane_id") != pane
                or response.get("previous_workspace_id") != self.workspace
                or response.get("previous_tab_id") != previous
                or not isinstance(metadata, dict) or metadata.get("pane_id") != pane
                or metadata.get("workspace_id") != self.workspace or metadata.get("tab_id") != tab
                or metadata.get("terminal_id") != self.base["identity"][pane][0]):
            raise _TidyError("pane-move-unconfirmed")
        expected_target = {p for p, t in self.locations.items() if t == tab} | {pane}
        target_layout = self.bound(response.get("target_layout"), tab, expected_target)
        if response.get("focused_pane_id") != target_layout.get("focused_pane_id"):
            raise _TidyError("pane-move-unconfirmed")
        expected_source = {p for p, t in self.locations.items() if t == previous} - {pane}
        source_layout = response.get("source_layout")
        if expected_source:
            self.bound(source_layout, previous, expected_source)
        elif response.get("closed_tab_id") != previous:
            raise _TidyError("pane-staging-unconfirmed")
        self.locations[pane], self.pending = tab, None
        self.expected = target_layout if tab == self.tab else source_layout
        return target_layout

    def reshape(self, cells):
        if _grid_matches(self.expected, cells):
            return
        boundaries = _canonical_splits(self.expected)
        if boundaries is not None:
            for split, desired, _a, _b in boundaries:
                # Earlier outer resizes change rects, never the split identities.
                current = next(b for b in _canonical_splits(self.expected) if b[0]["id"] == split["id"])
                item, _, first, second = current
                delta = desired - item["ratio"]
                if abs(delta) < 1e-6:
                    continue
                axis, size = ("x", "width") if item["direction"] == "right" else ("y", "height")
                direction = ("right" if axis == "x" else "down") if delta > 0 else ("left" if axis == "x" else "up")
                edge = first[axis] + first[size]
                panes = _reading(self.expected)
                anchor = next(p["pane_id"] for p in panes if
                              (p["rect"][axis] + p["rect"][size] if delta > 0 else p["rect"][axis]) == edge
                              and all(p["rect"][k] >= item["rect"][k]
                                      and p["rect"][k] + p["rect"][s] <= item["rect"][k] + item["rect"][s]
                                      for k, s in (("x", "width"), ("y", "height"))))
                for amount in (min(abs(delta), .5), max(0, abs(delta) - .5)):
                    if not amount:
                        continue
                    self.check()
                    was_changed = self.result.changed
                    self.result.changed = True
                    response = self.surface.resize(anchor, direction, amount)
                    if isinstance(response, dict) and response.get("changed") is False:
                        self.result.changed = was_changed
                    if (not isinstance(response, dict) or response.get("changed") is not True
                            or response.get("pane_id") != anchor or response.get("focused_pane_id") != self.focus):
                        raise _TidyError("pane-resize-unconfirmed")
                    before = self.expected
                    self.expected = self.bound(response.get("layout"), self.tab, self.locations)
                    if _layout_state(before) == _layout_state(self.expected):
                        raise _TidyError("pane-resize-unconfirmed")
            return
        # Leave F in the original tab, preserving its identity and local focus.
        queue = [self.focus] + [p["pane_id"] for p in _reading(self.expected) if p["pane_id"] != self.focus]
        staged = self.move(queue[1])
        for pane in queue[2:]:
            largest = max(_reading(staged), key=lambda p: p["rect"]["width"] * p["rect"]["height"])
            direction = "right" if largest["rect"]["width"] >= largest["rect"]["height"] else "down"
            staged = self.move(pane, tab=self.staging, target=largest["pane_id"], direction=direction)
        rows, index, seeds = _grid_rows(len(queue)), 0, []
        for i, count in enumerate(rows):
            seeds.append(queue[index])
            if i:
                self.move(queue[index], tab=self.tab, target=seeds[i - 1], direction="down",
                          ratio=1 / (len(rows) - i + 1))
            index += count
        index = 0
        for count in rows:
            for j in range(1, count):
                self.move(queue[index + j], tab=self.tab, target=queue[index + j - 1],
                          ratio=1 / (count - j + 1))
            index += count

    def reorder(self, desired):
        def swap(other):
            self.check()
            was_changed = self.result.changed
            self.result.changed = True
            response = self.surface.swap(self.focus, other)
            if isinstance(response, dict) and response.get("changed") is False:
                self.result.changed = was_changed
            if (not isinstance(response, dict) or response.get("changed") is not True
                    or response.get("source_pane_id") != self.focus or response.get("target_pane_id") != other
                    or response.get("focused_pane_id") != self.focus):
                raise _TidyError("pane-swap-unconfirmed")
            self.expected = self.bound(response.get("layout"), self.tab, self.locations)
        for i, wanted in enumerate(desired):
            actual = [p["pane_id"] for p in _reading(self.expected)]
            other = actual[i]
            if wanted == other:
                continue
            if self.focus in (wanted, other):
                swap(other if wanted == self.focus else wanted)
            else:
                swap(wanted)
                swap(other)
                swap(wanted)

    def cleanup(self):
        if not self.staging and not self.pending:
            return
        inventory = self.surface.panes(self.workspace)
        if inventory is None:
            self.result.notes.append("pane-staging-unconfirmed:" + (self.staging or self.pending))
            return
        by_id = {p["pane_id"]: p.get("tab_id") for p in inventory}
        if not self.staging and self.pending:
            candidate = by_id.get(self.pending)
            if (candidate and candidate not in self.base["tabs"]
                    and all(p["pane_id"] in self.locations for p in inventory if p.get("tab_id") == candidate)):
                self.staging = candidate
        self.locations.update({p: t for p, t in by_id.items() if p in self.locations})
        remaining = [p for p, t in by_id.items() if t == self.staging and p in self.locations]
        if not remaining:
            return
        anchor = next((p for p, t in by_id.items() if p in self.locations and t == self.tab), None)
        if not anchor:
            self.result.notes.append("pane-staging-retained:" + self.staging)
            return
        for pane in remaining:
            try:
                metadata = self.surface._result("pane", "get", pane).get("pane", {})
                if metadata.get("terminal_id") != self.base["identity"][pane][0]:
                    raise _TidyError("pane-identity-changed")
                self.move(pane, tab=self.tab, target=anchor, cleanup=True)
            except (Exception, KeyboardInterrupt):
                # A lost/mismatching reply can follow an applied move. The next
                # inventory, rather than that reply, decides what is retained.
                pass
        after = self.surface.panes(self.workspace)
        tabs = self.surface.tabs(self.workspace)
        if after is None or tabs is None:
            self.result.notes.append("pane-staging-unconfirmed:" + self.staging)
        else:
            retained = [p["pane_id"] for p in after
                        if p.get("tab_id") == self.staging and p["pane_id"] in self.locations]
            for pane in retained:
                self.result.notes.append(f"pane-staging-retained:{self.staging}:{pane}")
            if not retained and any(t["tab_id"] == self.staging for t in tabs):
                self.result.notes.append("pane-staging-retained:" + self.staging)


def _tidy_herdr(surface, base, desired, cells, result):
    engine = _HerdrTidy(surface, base, result)
    try:
        engine.reshape(cells)
        engine.reorder(desired)
    finally:
        with _tidy_signals(cleanup=True):
            try:
                engine.cleanup()
            except (Exception, KeyboardInterrupt):
                result.notes.append("pane-staging-unconfirmed:" + (engine.staging or "unknown"))


def _tmux_grid(area, cells, ids):
    """tmux's custom layout parser assigns its internal pane list to these cells."""
    def block(rect, children=None, bracket=None, pane=None):
        s = f'{rect["width"]}x{rect["height"]},{rect["x"]},{rect["y"]}'
        return s + (bracket[0] + ",".join(children) + bracket[1] if children else f",{pane[1:]}")
    rows = []
    for count in _grid_rows(len(ids)):
        part, part_ids = cells[:count], ids[:count]
        cells, ids = cells[count:], ids[count:]
        row = {**part[0], "width": area["width"]}
        leaves = [block(r, pane=p) for r, p in zip(part, part_ids)]
        rows.append(leaves[0] if count == 1 else block(row, leaves, "{}"))
    value = rows[0] if len(rows) == 1 else block(area, rows, "[]")
    checksum = 0
    for byte in value.encode("ascii"):
        checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xffff
    return f"{checksum:04x},{value}"


def _tidy_tmux(surface, base, desired, cells, result):
    current = base
    geometry_changed = not _grid_matches(base, cells)
    try:
        if geometry_changed:
            # A transport failure can be ambiguous: changed means a mutation was
            # attempted, until a fresh snapshot can confirm its result.
            result.changed = True
            custom = _tmux_grid(base["area"], cells, [p["pane_id"] for p in _reading(base)])
            if surface._tmux("select-layout", "-t", base["tab_id"], custom) is None:
                raise _TidyError("pane-layout-apply-failed")
            current = surface.tidy_snapshot(desired[0])
            _same_snapshot(base, current)
            if not _grid_matches(current, cells):
                raise _TidyError("pane-grid-unconfirmed")
        for index, wanted in enumerate(desired):
            actual = [p["pane_id"] for p in _reading(current)]
            other = actual[index]
            if wanted == other:
                continue
            focus = base["focused_pane_id"]
            if focus in (wanted, other):
                source, target, detached = (other if wanted == focus else wanted), focus, []
            else:
                source, target, detached = wanted, other, ["-d"]
            # Re-read the active identity immediately before a swap with focus semantics.
            fresh = surface.tidy_snapshot(wanted)
            _same_snapshot(base, fresh)
            if fresh["panes"] != current["panes"]:
                raise _TidyError("pane-layout-changed")
            result.changed = True
            if surface._tmux("swap-pane", *detached, "-s", source, "-t", target) is None:
                raise _TidyError("pane-swap-failed")
            current = surface.tidy_snapshot(wanted)
            _same_snapshot(base, current)
    except (_TidyError, KeyboardInterrupt):
        if geometry_changed:
            # Restore only known cells, without replacing panes or overwriting
            # a concurrent geometry/focus change. One attempt, never a retry loop.
            with _tidy_signals(cleanup=True):
                try:
                    fresh = surface.tidy_snapshot(desired[0])
                    _same_snapshot(base, fresh)
                    if _grid_matches(fresh, cells):
                        if surface._tmux("select-layout", "-t", base["tab_id"], base["original_layout"]) is None:
                            result.notes.append("pane-layout-restore-unconfirmed")
                except (Exception, KeyboardInterrupt):
                    result.notes.append("pane-layout-restore-unconfirmed")
        raise


def tidy_panes(*, near=None):
    """Rearrange only the caller's existing panes, once, with no typed input."""
    from session_tidy import is_worker
    if is_worker():
        return TidyResult("none", notes=["worker-refused"])
    checked = near is not None
    near = near or _tidy_caller()
    if near is None:
        if any(os.environ.get(key) for key in ("CMUX_WORKSPACE_ID", "CMUX_SURFACE_ID")):
            return TidyResult("cmux", notes=["Hearting pane tidy is not supported on cmux."])
        return TidyResult("none", notes=["pane-caller-unconfirmed"])
    surface = near.surface
    result = TidyResult(surface.name)
    try:
        with _tidy_signals():
            workspace = surface.workspace_of(near.pane)
            if not workspace:
                raise _TidyError("pane-layout-unavailable")
            with workspace_lock(surface, workspace):
                if not checked:
                    fresh = _tidy_caller()
                    if (fresh is None or fresh.pane != near.pane or fresh.surface.name != surface.name
                            or getattr(fresh.surface, "socket", None) != getattr(surface, "socket", None)
                            or getattr(fresh.surface, "session", None) != getattr(surface, "session", None)):
                        raise _TidyError("pane-caller-unconfirmed")
                base = surface.tidy_snapshot(near.pane)
                result.tab = base["tab_id"]
                if base.get("zoomed"):
                    raise _TidyError("pane-tab-zoomed")
                result.panes = [p["pane_id"] for p in _reading(base)]
                desired = _tidy_order(base, near.pane)
                cells = _grid_cells(base["area"], len(desired), int(surface.name == "tmux"))
                if _grid_matches(base, cells) and desired == result.panes:
                    return result
                if surface.name == "herdr":
                    if base["active"] != (True, base["tab_id"]):
                        raise _TidyError("pane-tab-inactive")
                    _tidy_herdr(surface, base, desired, cells, result)
                else:
                    _tidy_tmux(surface, base, desired, cells, result)
                final = surface.tidy_snapshot(near.pane)
                _same_snapshot(base, final)
                result.panes = [p["pane_id"] for p in _reading(final)]
                if not _grid_matches(final, cells) or result.panes != desired:
                    raise _TidyError("pane-grid-unconfirmed")
    except (Exception, KeyboardInterrupt) as exc:
        result.notes.append(str(exc) if isinstance(exc, _TidyError) else
                            "pane-tidy-interrupted" if isinstance(exc, KeyboardInterrupt) else
                            f"pane-tidy-unconfirmed:{type(exc).__name__}")
        if result.tab:
            try:
                live = surface.layout(near.pane)
                result.panes = [p["pane_id"] for p in _reading(live)] if live else []
            except Exception:
                result.panes = []
            if not result.panes:
                result.notes.append("pane-layout-unconfirmed")
    return result


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
        found = place(surface, near.pane, near.workspace, same_tab=near.same_tab)
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
        # One read right after opening: it can show the pane already gone, never
        # that its command is running or visible.
        try:
            live = surface.pane_ids(workspace)
        except Exception:
            # A caller's runner may raise (recovery's checked socket does); the pane
            # is already open, so an unreadable list is a doubt, not a failure.
            live = None
        if live is None:
            notes.append("pane-list-unconfirmed")
        elif pane not in live:
            notes.append("pane-exited")
        return PaneResult(surface.name, pane, action, notes, command)
