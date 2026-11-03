#!/usr/bin/env python3
"""aedd50 — Fleet names where each session's pane sits and hints a supervisor off the corner.

Herdr: the tab and pane from the existing pane observation (`wB:tZ·pA6`). tmux: one
`list-panes -a` on the server `$TMUX` names, joined by the runtime's controlling
terminal (`main:1.2`). A `⚑` pane that is not its tab's top-left pane — the order
`hearting run pane tidy` arranges — leads with `↖`. Nothing moves any pane.

The tmux cases run a private server on a socket inside the test directory.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
_UTILITIES_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "utilities")
if _UTILITIES_DIR not in sys.path:
    sys.path.insert(0, _UTILITIES_DIR)

from fleet import render                                           # noqa: E402
from fleet.model import Session                                    # noqa: E402
from fleet.collectors import herdr, tmux                           # noqa: E402
import terminal_surface                                            # noqa: E402


def _text(segs):
    return "".join(text for text, _key in segs)


def _rect(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


class SupervisorCornerTest(unittest.TestCase):
    layout = {"area": _rect(0, 0, 100, 40),
              "panes": [{"pane_id": "a", "rect": _rect(0, 0, 50, 40)},
                        {"pane_id": "b", "rect": _rect(50, 0, 50, 20)},
                        {"pane_id": "c", "rect": _rect(50, 20, 50, 20)}]}

    def test_the_first_marked_pane_in_reading_order_is_judged(self):
        corner = terminal_surface.supervisor_corner
        self.assertEqual(corner(self.layout, {"a": ("[50] ⚑ claude", None)}), ("a", True))
        self.assertEqual(corner(self.layout, {"c": "[6c] ⚑ codex x", "b": ("", "⚑ claude")}), ("b", False))
        self.assertIsNone(corner(self.layout, {"a": "[50] claude", "b": "codex ⚑ later"}))
        broken = {"area": _rect(0, 0, 10, 10), "panes": [{"pane_id": "a", "rect": _rect(0, 0, 20, 10)}]}
        self.assertIsNone(corner(broken, {"a": "⚑"}))


class HerdrLocationTest(unittest.TestCase):
    def test_location_names_tab_and_pane(self):
        self.assertEqual(herdr.location({"pane_id": "wB:pA6", "tab_id": "wB:tZ", "workspace_id": "wB"}),
                         "wB:tZ·pA6")
        self.assertIsNone(herdr.location({"pane_id": "wB:pA6", "tab_id": "wC:tZ", "workspace_id": "wB"}))
        self.assertIsNone(herdr.location({"pane_id": "wB:pA6"}))

    def test_enrich_places_attached_sessions_and_marks_the_supervisor(self):
        agents = [{"pane_id": "w1:p1", "agent": "claude",
                   "agent_session": {"agent": "claude", "value": "sid-steward"}},
                  {"pane_id": "w1:p2", "agent": "claude",
                   "agent_session": {"agent": "claude", "value": "sid-worker"}}]
        panes = [{"pane_id": "w1:p1", "tab_id": "w1:t3", "workspace_id": "w1", "display_agent": "[50] ⚑ claude"},
                 {"pane_id": "w1:p2", "tab_id": "w1:t3", "workspace_id": "w1", "display_agent": "[0a] claude"}]
        sessions = [Session(harness="claude", pid=11, session_id="sid-steward"),
                    Session(harness="claude", pid=12, session_id="sid-worker")]
        with mock.patch.object(herdr, "tab_corner", return_value=("w1:p1", False)) as corner, \
             mock.patch.object(herdr, "pane_session_aliases", return_value=[]), \
             mock.patch.object(herdr, "_clear_gpu_session_aliases", return_value=[]):
            herdr.enrich(sessions, agents=agents, panes=panes, pids=(set(), set()))
        self.assertEqual([(s.pane_location, s.multiplexer) for s in sessions],
                         [("w1:t3·p1", "herdr"), ("w1:t3·p2", "herdr")])
        self.assertEqual([s.pane_corner for s in sessions], [False, None])
        self.assertEqual(corner.call_args.args[1], "w1:t3")

    def test_the_corner_read_never_waits_and_is_reused(self):
        herdr._CORNERS.clear()
        self.addCleanup(herdr._CORNERS.clear)
        calls = []

        def reader(endpoint, pane, marks):
            calls.append((endpoint, pane))
            return ("w1:p1", True)
        first = herdr.tab_corner("default", "w1:t3", "w1:p1", {"w1:p1": ("⚑",)}, reader=reader)
        self.assertIsNone(first)  # the read runs in the background; this tick shows nothing yet
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and herdr._CORNERS[("default", "w1:t3")]["future"].running():
            time.sleep(.01)
        herdr._CORNERS[("default", "w1:t3")]["future"].result(timeout=5)
        self.assertEqual(herdr.tab_corner("default", "w1:t3", "w1:p1", {}, reader=reader), ("w1:p1", True))
        self.assertEqual(herdr.tab_corner("default", "w1:t3", "w1:p1", {}, reader=reader), ("w1:p1", True))
        self.assertEqual(len(calls), 1)


class RenderWhereCellTest(unittest.TestCase):
    def _session(self, **fields):
        return Session(harness="claude", pid=1, session_id="s", liveness="idle", **fields)

    def test_main_rows_name_the_location_and_other_rows_keep_the_word(self):
        located = self._session(herdr_attached=True, pane_location="wB:tZ·pA6", multiplexer="herdr")
        self.assertEqual(_text(render._context_lead_chip(located, room=12)), "wB:tZ·pA6 ")
        self.assertEqual(_text(render._context_lead_chip(located)), "herdr   ")
        tmux_row = self._session(pane_location="work:1.2", multiplexer="tmux")
        self.assertEqual(_text(render._context_lead_chip(tmux_row)), "tmux    ")
        self.assertEqual(_text(render._context_lead_chip(tmux_row, room=12)), "work:1.2 ")
        off = self._session(pane_location="main:0.0", multiplexer="tmux", pane_corner=False, steward=True)
        segs = render._context_lead_chip(off, room=12)
        self.assertEqual(segs[0], ("↖", "lvl_y"))
        self.assertEqual(_text(segs), "↖main:0.0 ")
        long = self._session(pane_location="a-very-long-session:12.3", multiplexer="tmux")
        text = _text(render._context_lead_chip(long, room=12))
        self.assertTrue(text.startswith("…") and text.endswith("ion:12.3 "))
        self.assertLessEqual(render._dw(text), 12)
        # Without evidence nothing changes.
        self.assertEqual(_text(render._context_lead_chip(self._session(herdr_attached=False), room=12)), "tty     ")
        self.assertEqual(_text(render._context_lead_chip(self._session(), room=12)), " " * 8)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TmuxLocationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.socket = os.path.join(temp.name, "t.sock")
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("TMUX", "HERDR_"))}
        self.env["SHELL"] = "/bin/sh"
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "work", "-x", "160", "-y", "40",
                            "sleep 600")
        if started.returncode:
            self.fail("isolated tmux server did not start: " + started.stderr.strip())
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", self.socket, "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.left = self.tmux("display-message", "-p", "-t", "work", "#{pane_id}").stdout.strip()
        self.right = self.tmux("split-window", "-d", "-h", "-t", self.left, "-P", "-F", "#{pane_id}",
                               "sleep 600").stdout.strip()
        patch = mock.patch.dict(os.environ, {"TMUX": self.socket + ",1,0"})
        patch.start()
        self.addCleanup(patch.stop)

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.socket, *args], capture_output=True, text=True,
                              env=self.env, timeout=10, check=False)

    def pid(self, pane):
        return int(self.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip())

    def test_sessions_are_placed_by_their_terminal_and_the_supervisor_corner_is_read(self):
        steward = SimpleNamespace(pid=self.pid(self.right), herdr_attached=None, multiplexer=None,
                                  pane_location=None, pane_corner=None)
        worker = SimpleNamespace(pid=self.pid(self.left), herdr_attached=None, multiplexer=None,
                                 pane_location=None, pane_corner=None)
        elsewhere = SimpleNamespace(pid=os.getpid(), herdr_attached=None, multiplexer=None,
                                    pane_location=None, pane_corner=None)
        in_herdr = SimpleNamespace(pid=self.pid(self.left), herdr_attached=True, multiplexer=None,
                                   pane_location=None, pane_corner=None)
        self.tmux("set-option", "-p", "-t", self.right, "@hearting_title", "[50] ⚑ claude 감독")
        before = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_left} #{pane_top} #{pane_active}").stdout
        tmux.enrich([steward, worker, elsewhere, in_herdr])
        self.assertEqual((steward.pane_location, steward.multiplexer, steward.pane_corner),
                         ("work:0.1", "tmux", False))
        self.assertEqual((worker.pane_location, worker.pane_corner), ("work:0.0", None))
        self.assertIsNone(elsewhere.pane_location)
        self.assertIsNone(in_herdr.pane_location)
        # Read only: no pane moved, resized or took focus.
        self.assertEqual(self.tmux("list-panes", "-a", "-F",
                                   "#{pane_id} #{pane_left} #{pane_top} #{pane_active}").stdout, before)
        # After `swap-pane` (what a person or `pane tidy` would do) the corner reads true.
        self.tmux("swap-pane", "-d", "-s", self.right, "-t", self.left)
        tmux.enrich([steward, worker])
        self.assertEqual((steward.pane_location, steward.pane_corner), ("work:0.0", True))

    def test_no_server_or_no_tmux_leaves_every_cell(self):
        session = SimpleNamespace(pid=os.getpid(), herdr_attached=None, multiplexer=None,
                                  pane_location=None, pane_corner=None)
        with mock.patch.dict(os.environ, {"TMUX": self.socket + ".gone,1,0"}):
            tmux.enrich([session])
        with mock.patch.object(tmux.shutil, "which", return_value=None):
            tmux.enrich([session])
        self.assertIsNone(session.pane_location)


if __name__ == "__main__":
    unittest.main(verbosity=2)
