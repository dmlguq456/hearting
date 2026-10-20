#!/usr/bin/env python3
"""Tests for utilities/terminal_surface.py.

No test reaches the user's terminal: every Herdr call goes to an in-memory fake,
every tmux call to a private server on a socket inside the test's own directory,
and setUp removes the inherited pane and socket variables even for a single-file run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import terminal_surface as ts  # noqa: E402

_SURFACE_ENV = ("HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID",
                "TMUX", "TMUX_PANE", "AGENT_HERDR_SESSION")

FULL = [(10, 2, 200, 100)]
TWO = [(10, 2, 100, 100), (110, 2, 100, 100)]
THREE = [(10, 2, 100, 100), (110, 2, 100, 50), (110, 52, 100, 50)]
FOUR = [(10, 2, 100, 50), (10, 52, 100, 50), (110, 2, 100, 50), (110, 52, 100, 50)]


def _done(payload=None, code=0):
    return subprocess.CompletedProcess(["herdr"], code, stdout="" if payload is None else json.dumps(payload),
                                       stderr="")


class FakeHerdr:
    """One Herdr workspace `w1` in memory; the runner a HerdrSurface calls.

    Tab n's panes are `w1:p<i>` for the first tab and `w1:p<n>-<i>` after it. A pane is
    occupied (an agent) unless named in `free`; a new pane is an idle shell until a
    command runs in it. Focus is tracked so a test can prove it never moved.
    """

    def __init__(self, tabs, area=FULL[0], free=(), foreground="shell", prompt=True):
        self.area = dict(zip(("x", "y", "width", "height"), area))
        self.calls, self.tabs, self.panes, self.created = [], [], {}, 0
        self.foreground, self.prompt = foreground, prompt
        for number, rects in enumerate(tabs, 1):
            tab = {"tab_id": f"w1:t{number}", "workspace_id": "w1", "number": number,
                   "label": f"직접 정한 이름 {number}"}
            self.tabs.append(tab)
            for i, rect in enumerate(rects):
                pane_id = f"w1:p{i}" if number == 1 else f"w1:p{number}-{i}"
                self.panes[pane_id] = {"tab_id": tab["tab_id"], "rect": dict(zip(("x", "y", "width", "height"), rect)),
                                       "agent": None if pane_id in free else "codex", "busy": False}
        self.focused_pane, self.focused_tab = next(iter(self.panes), None), "w1:t1"

    def tab_panes(self, tab_id):
        return [pane_id for pane_id, pane in self.panes.items() if pane["tab_id"] == tab_id]

    def __call__(self, argv):
        args = list(argv[1:])
        if args[:1] == ["--session"]:
            args = args[2:]
        self.calls.append(args)
        verb = args[:2]
        if verb == ["pane", "layout"]:
            pane = self.panes.get(args[-1])
            if pane is None:
                return _done({"error": {"code": "pane_not_found"}}, 1)
            return _done({"result": {"layout": {
                "workspace_id": "w1", "tab_id": pane["tab_id"], "area": dict(self.area),
                "panes": [{"pane_id": p, "rect": dict(self.panes[p]["rect"])} for p in self.tab_panes(pane["tab_id"])]}}})
        if verb == ["tab", "list"]:
            return _done({"result": {"tabs": [dict(tab) for tab in self.tabs]}})
        if verb == ["pane", "list"]:
            return _done({"result": {"panes": [{"pane_id": p, "workspace_id": "w1", "tab_id": v["tab_id"]}
                                               for p, v in self.panes.items()]}})
        if verb == ["pane", "get"]:
            pane = self.panes[args[2]]
            return _done({"result": {"pane": {"pane_id": args[2], "workspace_id": "w1", "tab_id": pane["tab_id"],
                                              "agent": pane["agent"]}}})
        if verb == ["pane", "process-info"]:
            pane, state = self.panes[args[-1]], self.foreground
            if pane["busy"]:
                state = "busy"
            if state == "unknown":
                return _done({"error": {"code": "unavailable"}}, 1)
            group = 101 if state == "shell" else 202
            return _done({"result": {"process_info": {
                "pane_id": args[-1], "shell_pid": 101, "foreground_process_group_id": group,
                "foreground_processes": [{"pid": group}]}}})
        if verb == ["pane", "wait-output"]:
            return _done(None, 0 if self.prompt and not self.panes[args[2]]["busy"] else 1)
        if verb == ["pane", "split"]:
            return self.split(args)
        if verb == ["tab", "create"]:
            return self.new_tab(args)
        if verb == ["pane", "run"]:
            self.panes[args[2]]["busy"] = True
            return _done({"result": {"type": "ok"}})
        if verb in (["pane", "rename"], ["pane", "report-metadata"]):
            return _done({"result": {"type": "ok"}})
        raise AssertionError(argv)

    def split(self, args):
        target = args[args.index("--pane") + 1]
        rect = self.panes[target]["rect"]
        self.created += 1
        new_id = f"w1:pNew{self.created}"
        if args[args.index("--direction") + 1] == "right":
            half = rect["width"] // 2
            new = dict(rect, x=rect["x"] + half, width=rect["width"] - half)
            rect["width"] = half
        else:
            half = rect["height"] // 2
            new = dict(rect, y=rect["y"] + half, height=rect["height"] - half)
            rect["height"] = half
        self.panes[new_id] = {"tab_id": self.panes[target]["tab_id"], "rect": new, "agent": None, "busy": False}
        focused = "--focus" in args
        if focused:
            self.focused_pane = new_id
        return _done({"result": {"pane": {"pane_id": new_id, "focused": focused}}})

    def new_tab(self, args):
        number = max(tab["number"] for tab in self.tabs) + 1
        tab = {"tab_id": f"w1:t{number}", "workspace_id": "w1", "number": number, "label": ""}
        self.tabs.append(tab)
        pane_id = f"w1:p{number}-0"
        self.panes[pane_id] = {"tab_id": tab["tab_id"], "rect": dict(self.area), "agent": None, "busy": False}
        focused = "--focus" in args
        if focused:
            self.focused_pane, self.focused_tab = pane_id, tab["tab_id"]
        return _done({"result": {"tab": tab, "root_pane": {"pane_id": pane_id, "focused": focused}}})


class _IsolatedMixin:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in _SURFACE_ENV:
            os.environ.pop(key, None)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.root / "ledger")
        os.environ["HOME"] = str(self.root / "home")


class NextCellTest(unittest.TestCase):
    def layout(self, rects, area=None):
        return {"area": dict(zip(("x", "y", "width", "height"), area or FULL[0])),
                "panes": [{"pane_id": f"w1:p{i}", "rect": dict(zip(("x", "y", "width", "height"), rect))}
                          for i, rect in enumerate(rects)]}

    def test_one_two_three_fill_the_grid_even_when_the_reference_is_in_the_other_column(self):
        for rects, expected in ((FULL, ("w1:p0", "right")), (TWO, ("w1:p1", "down")),
                                (THREE, ("w1:p0", "down")),
                                ([(10, 2, 100, 50), (10, 52, 100, 50), (110, 2, 100, 100)], ("w1:p2", "down"))):
            with self.subTest(rects=rects):
                self.assertEqual(ts.next_cell(self.layout(rects)), expected)

    def test_horizontal_rows_fill_the_equivalent_grid(self):
        for rects, target in (([(10, 2, 200, 50), (10, 52, 200, 50)], "w1:p0"),
                              ([(10, 2, 100, 50), (10, 52, 200, 50), (110, 2, 100, 50)], "w1:p1"),
                              ([(10, 2, 200, 50), (10, 52, 100, 50), (110, 52, 100, 50)], "w1:p0")):
            self.assertEqual(ts.next_cell(self.layout(rects)), (target, "right"))

    def test_odd_terminal_dimensions_use_the_actual_half_cells(self):
        rects = [(23, 1, 138, 76), (161, 1, 137, 38), (161, 39, 137, 38)]
        self.assertEqual(ts.next_cell(self.layout(rects, (23, 1, 275, 76))), ("w1:p0", "down"))

    def test_user_resized_dividers_still_fill_the_same_tab(self):
        for rects, area, expected in (
                ([(0, 0, 125, 76), (125, 0, 143, 76)], (0, 0, 268, 76), ("w1:p1", "down")),
                ([(10, 2, 93, 100), (103, 2, 107, 40), (103, 42, 107, 60)], None, ("w1:p0", "down")),
                ([(10, 2, 140, 30), (10, 32, 140, 70), (150, 2, 60, 100)], None, ("w1:p2", "down")),
                ([(10, 2, 200, 30), (10, 32, 200, 70)], None, ("w1:p0", "right"))):
            with self.subTest(rects=rects):
                self.assertEqual(ts.next_cell(self.layout(rects, area)), expected)
        uneven = [(10, 2, 100, 30), (10, 32, 100, 70), (110, 2, 100, 60)]
        self.assertIsNone(ts.next_cell(self.layout(uneven)))

    def test_tmux_border_rects_fit_within_one_cell(self):
        # tmux leaves a one-cell divider and reports rects inside it; the union is the area.
        rects = [(0, 1, 100, 59), (101, 1, 99, 29), (101, 31, 99, 29)]
        self.assertEqual(ts.next_cell(self.layout(rects, (0, 1, 200, 59))), ("w1:p0", "down"))

    def test_irregular_or_overfull_tab_never_splits(self):
        for rects in ([(10, 2, 50, 100), (60, 2, 50, 100), (110, 2, 100, 100)], FOUR, FOUR + [(60, 52, 50, 50)]):
            self.assertIsNone(ts.next_cell(self.layout(rects)))


class PlacementTest(_IsolatedMixin, unittest.TestCase):
    def place(self, fake, pane="w1:p0", workspace=None):
        return ts.place(ts.HerdrSurface(fake), pane, workspace)

    def creations(self, fake):
        return [args for args in fake.calls if args[:2] in (["pane", "split"], ["tab", "create"], ["pane", "run"])]

    def test_full_caller_fills_the_newest_other_partial_grid(self):
        fake = FakeHerdr([FOUR, TWO, FULL])
        self.assertEqual(self.place(fake), ("split", "w1:p3-0", "right"))
        self.assertNotIn(["pane", "layout", "--pane", "w1:p2-0"], fake.calls)
        self.assertEqual(self.creations(fake), [])

    def test_all_workspace_tabs_full_create_a_new_tab(self):
        self.assertEqual(self.place(FakeHerdr([FOUR, FOUR])), ("tab", "w1", None))

    def test_other_full_tab_with_an_empty_shell_is_reused(self):
        self.assertEqual(self.place(FakeHerdr([FOUR, FOUR], free={"w1:p2-3"})), ("reuse", "w1:p2-3", None))

    def test_full_newest_tab_does_not_hide_an_older_partial_tab(self):
        self.assertEqual(self.place(FakeHerdr([FOUR, THREE, FOUR])), ("split", "w1:p2-0", "down"))

    def test_full_tab_reuses_only_an_empty_foreground_shell_otherwise_creates_a_tab(self):
        self.assertEqual(self.place(FakeHerdr([FOUR])), ("tab", "w1", None))
        self.assertEqual(self.place(FakeHerdr([FOUR], free={"w1:p3"})), ("reuse", "w1:p3", None))
        self.assertEqual(self.place(FakeHerdr([FOUR], free={"w1:p0"})), ("reuse", "w1:p0", None))
        for foreground, prompt in (("busy", True), ("unknown", True), ("shell", False)):
            with self.subTest(foreground=foreground, prompt=prompt):
                fake = FakeHerdr([FOUR], free={"w1:p3"}, foreground=foreground, prompt=prompt)
                self.assertEqual(self.place(fake), ("tab", "w1", None))

    def test_irregular_tab_never_splits(self):
        fake = FakeHerdr([[(10, 2, 50, 100), (60, 2, 50, 100), (110, 2, 100, 100)]])
        self.assertEqual(self.place(fake), ("tab", "w1", None))

    def test_unreadable_or_foreign_tab_inventory_never_allocates(self):
        for inventory in ({"error": {"code": "unavailable"}}, {"result": {"tabs": None}},
                          {"result": {"tabs": [{"workspace_id": "w2", "tab_id": "w2:t1", "number": 9}]}}):
            with self.subTest(inventory=inventory):
                fake = FakeHerdr([FOUR])
                original = fake.__call__

                def run(argv, original=original, inventory=inventory):
                    if argv[1:3] == ["tab", "list"]:
                        fake.calls.append(argv[1:])
                        return _done(inventory, 1 if "error" in inventory else 0)
                    return original(argv)
                self.assertIsNone(ts.place(ts.HerdrSurface(run), "w1:p0"))
                self.assertEqual(self.creations(fake), [])

    def test_missing_duplicate_or_foreign_layout_never_allocates(self):
        def layout_of(payload):
            return lambda argv: _done({"result": {"layout": payload}})
        base = {"workspace_id": "w1", "tab_id": "w1:t1", "area": {"x": 0, "y": 0, "width": 200, "height": 100}}
        missing = dict(base, panes=[{"pane_id": "w1:pElse", "rect": {"x": 0, "y": 0, "width": 200, "height": 100}}])
        duplicate = dict(base, panes=[{"pane_id": "w1:p0", "rect": {"x": 0, "y": 0, "width": 100, "height": 100}},
                                      {"pane_id": "w1:p0", "rect": {"x": 100, "y": 0, "width": 100, "height": 100}}])
        foreign = dict(base, workspace_id="w2",
                       panes=[{"pane_id": "w1:p0", "rect": {"x": 0, "y": 0, "width": 200, "height": 100}}])
        for payload in (None, {}, missing, duplicate, foreign):
            with self.subTest(payload=payload):
                self.assertIsNone(ts.place(ts.HerdrSurface(layout_of(payload)), "w1:p0"))

    def test_workspace_only_reuses_an_empty_shell_or_adds_a_tab_without_splitting(self):
        fake = FakeHerdr([TWO], free={"w1:p1"})
        self.assertEqual(self.place(fake, pane=None, workspace="w1"), ("reuse", "w1:p1", None))
        fake = FakeHerdr([TWO])
        self.assertEqual(self.place(fake, pane=None, workspace="w1"), ("tab", "w1", None))
        self.assertFalse(any(args[:2] == ["pane", "layout"] for args in fake.calls))

    def test_unreadable_duplicate_or_foreign_workspace_inventory_never_allocates(self):
        own = {"pane_id": "w1:pEmpty", "workspace_id": "w1"}
        for panes in (None, [], [own, own], [{**own, "workspace_id": "w2"}], [{}]):
            with self.subTest(panes=panes):
                calls = []

                def run(argv, panes=panes):
                    calls.append(argv[1:])
                    return _done({"result": {"panes": panes}})
                self.assertIsNone(ts.place(ts.HerdrSurface(run), None, "w1"))
                self.assertEqual([args[:2] for args in calls], [["pane", "list"]])


class OpenPaneHerdrTest(_IsolatedMixin, unittest.TestCase):
    def open(self, fake, *argv, **options):
        options.setdefault("near", ts.Near(ts.HerdrSurface(fake), pane="w1:p0"))
        return ts.open_pane(list(argv) or None, cwd=str(self.root), **options)

    def test_d1_five_opens_fill_the_grid_then_one_tab_without_moving_focus(self):
        fake = FakeHerdr([FULL])
        before = (fake.focused_pane, fake.focused_tab)
        rows = []
        for _ in range(5):
            result = self.open(fake, "sleep", "300", title="d1")
            caller_tab = len(fake.tab_panes("w1:t1"))
            new_tab = len(fake.tab_panes("w1:t2")) if len(fake.tabs) > 1 else 0
            rows.append((result.placement, caller_tab, len(fake.tabs), new_tab, result.notes))
            self.assertEqual((fake.focused_pane, fake.focused_tab), before)
        self.assertEqual(rows, [("split", 2, 1, 0, []), ("split", 3, 1, 0, []), ("split", 4, 1, 0, []),
                                ("tab", 4, 2, 1, []), ("split", 4, 2, 2, [])])
        splits = [args for args in fake.calls if args[:2] == ["pane", "split"]]
        self.assertEqual([args[args.index("--direction") + 1] for args in splits[:3]], ["right", "down", "down"])
        self.assertTrue(all("--no-focus" in args for args in self.creations(fake) if args[1] != "run"))
        runs = [args for args in fake.calls if args[:2] == ["pane", "run"]]
        self.assertEqual(len(runs), 5)
        self.assertEqual(runs[0][3], f"cd {self.root} && sleep 300")

    def creations(self, fake):
        return [args for args in fake.calls if args[:2] in (["pane", "split"], ["tab", "create"], ["pane", "run"])]

    def test_title_and_labels_use_the_hearting_source_only(self):
        fake = FakeHerdr([FULL])
        result = self.open(fake, "true", title="fleet", labels={"owner": "steward"})
        self.assertEqual(result.notes, [])
        rename = next(args for args in fake.calls if args[:2] == ["pane", "rename"])
        self.assertEqual(rename, ["pane", "rename", result.pane, "fleet"])
        report = next(args for args in fake.calls if args[:2] == ["pane", "report-metadata"])
        self.assertEqual(report[report.index("--source") + 1], "hearting:pane")
        self.assertIn("group=session", report)
        self.assertIn("owner=steward", report)
        with self.assertRaises(ValueError):
            self.open(FakeHerdr([FULL]), "true", labels={"bad name": "x"})

    def test_no_title_reports_no_metadata(self):
        fake = FakeHerdr([FULL])
        self.open(fake, "true")
        self.assertFalse(any(args[:2] in (["pane", "rename"], ["pane", "report-metadata"]) for args in fake.calls))

    def test_launch_runs_once_inside_the_workspace_lock(self):
        fake = FakeHerdr([FULL])
        surface = ts.HerdrSurface(fake)
        seen = []

        def launch(pane, created):
            path = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-starts" / f"{surface.lock_key('w1')}.lock"
            seen.append((pane, created, path in ts._HELD))
            return True
        result = ts.open_pane(launch=launch, near=ts.Near(surface, pane="w1:p0"))
        self.assertEqual(seen, [(result.pane, True, True)])
        self.assertFalse(any(args[:2] == ["pane", "run"] for args in fake.calls))
        self.assertEqual(ts._HELD, set())

    def test_failures_are_notes_and_nothing_is_retried_or_closed(self):
        fake = FakeHerdr([FULL])

        def refuse(argv):
            if argv[1:3] == ["pane", "split"]:
                fake.calls.append(argv[1:])
                return _done({"result": {"pane": {"pane_id": "w1:pX", "focused": True}}})
            return fake(argv)
        result = ts.open_pane(["true"], near=ts.Near(ts.HerdrSurface(refuse), pane="w1:p0"))
        self.assertEqual((result.pane, result.placement, result.notes), (None, "split", ["pane-split-failed"]))
        self.assertEqual(sum(args[:2] == ["pane", "split"] for args in fake.calls), 1)
        self.assertFalse(any(args[:2] in (["pane", "close"], ["pane", "run"]) for args in fake.calls))

        unreadable = ts.open_pane(["true"], near=ts.Near(ts.HerdrSurface(lambda argv: _done(None, 1)), pane="w1:p0"))
        self.assertEqual((unreadable.surface, unreadable.notes), ("herdr", ["pane-layout-unavailable"]))

    def test_post_open_read_reports_exited_unconfirmed_and_launch_failure(self):
        for listing, launched, expected in ((None, True, ["pane-list-unconfirmed"]),
                                            ("gone", True, ["pane-exited"]),
                                            ("live", False, ["pane-launch-failed"])):
            with self.subTest(listing=listing, launched=launched):
                fake = FakeHerdr([FULL])
                state = {"opened": False}

                def run(argv, listing=listing):
                    if argv[1:3] == ["pane", "list"] and state["opened"]:
                        if listing is None:
                            return _done({"error": {"code": "unavailable"}}, 1)
                        if listing == "gone":
                            return _done({"result": {"panes": [{"pane_id": "w1:p0", "workspace_id": "w1"}]}})
                    if argv[1:3] == ["pane", "split"]:
                        state["opened"] = True
                    return fake(argv)
                result = ts.open_pane(launch=lambda pane, created, launched=launched: launched,
                                      near=ts.Near(ts.HerdrSurface(run), pane="w1:p0"))
                self.assertEqual(result.notes, expected)
                self.assertIsNotNone(result.pane)

    def test_only_the_session_group_exists(self):
        with self.assertRaises(ValueError):
            ts.open_pane(["true"], group="output", near=ts.Near(ts.HerdrSurface(FakeHerdr([FULL])), pane="w1:p0"))


class SurfaceChoiceTest(_IsolatedMixin, unittest.TestCase):
    def test_no_surface_returns_the_command_line_and_runs_nothing(self):
        result = ts.open_pane(["printf", "%s\n", "it's here"], cwd=str(self.root))
        self.assertEqual((result.surface, result.pane, result.placement, result.notes),
                         ("none", None, "none", ["no-surface"]))
        self.assertEqual(result.view_command, f"cd {self.root} && printf '%s\n' 'it'\"'\"'s here'")

    def test_registered_worker_never_reaches_herdr_or_tmux(self):
        os.environ.update({"HERDR_PANE_ID": "w1:p0", "HERDR_SOCKET_PATH": str(self.root / "herdr.sock"),
                           "TMUX": f"{self.root}/tmux.sock,1,0", "TMUX_PANE": "%0", "AGENT_DISPATCH_CHILD": "1"})
        with mock.patch.object(ts, "_run", side_effect=AssertionError("terminal touched")), \
             mock.patch("pane_ownership.verified_pane", side_effect=AssertionError("ownership read")):
            self.assertIsNone(ts.caller_surface())
            self.assertEqual(ts.open_pane(["true"]).surface, "none")

    # These two run as a non-worker caller whatever session runs the suite.
    def test_inherited_herdr_pane_without_live_proof_opens_nothing(self):
        os.environ["HERDR_PANE_ID"] = "w1:p0"
        with mock.patch("session_tidy.is_worker", return_value=False), \
             mock.patch("pane_ownership.verified_pane", return_value="") as verified, \
             mock.patch.object(ts, "_run", side_effect=AssertionError("terminal touched")):
            self.assertIsNone(ts.caller_surface())
        verified.assert_called_once()

    def test_proven_herdr_pane_comes_before_tmux(self):
        os.environ.update({"HERDR_PANE_ID": "w1:p0", "TMUX": "/tmp/x,1,0", "TMUX_PANE": "%0"})
        with mock.patch("session_tidy.is_worker", return_value=False), \
             mock.patch("pane_ownership.verified_pane", return_value="w1:p0"):
            near = ts.caller_surface()
        self.assertEqual((near.surface.name, near.pane), ("herdr", "w1:p0"))

    def test_command_composition_quotes_every_word(self):
        line = ts.compose_command(["echo", "a b", "$(x)", "q'uote", "new\nline"], "/tmp/with space", {"K": "v w"})
        self.assertEqual(line, "cd '/tmp/with space' && K='v w' echo 'a b' '$(x)' 'q'\"'\"'uote' 'new\nline'")
        with self.assertRaises(ValueError):
            ts.compose_command(["true"], env={"1BAD": "x"})
        with self.assertRaises(ValueError):
            ts.compose_command([])


class LockTest(_IsolatedMixin, unittest.TestCase):
    def test_herdr_key_is_the_peer_start_claim_byte_for_byte(self):
        for session in (None, "default", "work"):
            with self.subTest(session=session):
                expected = hashlib.sha256(f"{session or 'default'}:w1".encode()).hexdigest()
                self.assertEqual(ts.HerdrSurface(session=session).lock_key("w1"), expected)
        tmux = ts.TmuxSurface(socket="/tmp/s").lock_key("$1")
        self.assertEqual(tmux, hashlib.sha256(b"tmux:/tmp/s:$1").hexdigest())
        self.assertNotEqual(tmux, ts.HerdrSurface().lock_key("$1"))

    def test_lock_file_lives_under_the_peer_start_root_and_reenters(self):
        surface = ts.HerdrSurface()
        with ts.workspace_lock(surface, "w1"):
            with ts.workspace_lock(surface, "w1"):
                path = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-starts" / f"{surface.lock_key('w1')}.lock"
                self.assertIn(path, ts._HELD)
                self.assertTrue(path.exists())
            self.assertIn(path, ts._HELD)
        self.assertEqual(ts._HELD, set())


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class OpenPaneTmuxTest(_IsolatedMixin, unittest.TestCase):
    """A private tmux server per test: its socket is in the test directory, never $TMUX."""

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], capture_output=True, text=True,
                              env=self.env, timeout=10, check=False)

    def start_server(self, border=None):
        self.socket = self.root / f"tmux-{border or 'default'}.sock"
        self.env = {key: value for key, value in os.environ.items() if key not in _SURFACE_ENV}
        self.env["SHELL"] = "/bin/sh"
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "d1", "-x", "200", "-y", "60",
                            "sleep 600")
        if started.returncode:
            self.fail(f"isolated tmux server did not start: {started.stderr.strip()}")
        socket = self.socket
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", str(socket), "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.tmux("set-option", "-g", "status", "off")
        if border:
            self.tmux("set-option", "-wg", "pane-border-status", border)
        self.reference = self.tmux("display-message", "-p", "-t", "d1", "#{pane_id}").stdout.strip()
        surface = ts.TmuxSurface(lambda argv: subprocess.run(argv, capture_output=True, text=True, env=self.env,
                                                             timeout=10, check=False), str(self.socket))
        return ts.Near(surface, pane=self.reference)

    def focus(self):
        window = self.tmux("display-message", "-p", "-t", "d1", "#{window_id}").stdout.strip()
        pane = self.tmux("display-message", "-p", "-t", "d1", "#{pane_id}").stdout.strip()
        return window, pane

    def windows(self):
        out = self.tmux("list-windows", "-t", "d1", "-F", "#{window_id}").stdout.split()
        return [len(self.tmux("list-panes", "-t", window, "-F", "#{pane_id}").stdout.split()) for window in out]

    def settle(self, pane):
        # The typed command replaces the shell before the next open reads the tab.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.tmux("display-message", "-p", "-t", pane, "#{pane_current_command}").stdout.strip() == "sleep":
                return
            time.sleep(.05)
        self.fail(f"pane {pane} never ran its command")

    def test_d1_five_opens_fill_the_grid_then_one_window_with_default_and_top_borders(self):
        for border in (None, "top"):
            with self.subTest(border=border):
                near = self.start_server(border)
                before = self.focus()
                rows = []
                for _ in range(5):
                    result = ts.open_pane(["sleep", "300"], title="d1", near=near, cwd=str(self.root))
                    self.assertEqual(result.notes, [], result)
                    self.settle(result.pane)
                    rows.append((result.placement, self.windows()))
                    self.assertEqual(self.focus(), before)
                self.assertEqual(rows, [("split", [2]), ("split", [3]), ("split", [4]),
                                        ("tab", [4, 1]), ("split", [4, 2])])
                title = self.tmux("display-message", "-p", "-t", result.pane, "#{pane_title}").stdout.strip()
                group = self.tmux("show-options", "-p", "-t", result.pane, "-v", "@hearting_group").stdout.strip()
                self.assertEqual((title, group), ("d1", "session"))

    def test_zoomed_window_is_irregular_and_never_split(self):
        near = self.start_server()
        first = ts.open_pane(["sleep", "300"], near=near, cwd=str(self.root))
        self.settle(first.pane)
        self.tmux("resize-pane", "-Z", "-t", self.reference)
        before = self.focus()
        second = ts.open_pane(["sleep", "300"], near=near, cwd=str(self.root))
        self.assertEqual((second.placement, self.windows()), ("tab", [2, 1]))
        self.assertEqual(self.focus(), before)

    def test_idle_shell_in_a_full_window_is_reused(self):
        near = self.start_server()
        panes = []
        for _ in range(3):
            panes.append(ts.open_pane(launch=lambda pane, created: True, near=near).pane)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not near.surface.empty_shell(panes[-1]):
            time.sleep(.05)
        reused = ts.open_pane(["sleep", "300"], near=near, cwd=str(self.root))
        self.assertEqual(reused.placement, "reuse")
        self.assertIn(reused.pane, panes)
        self.assertEqual(self.windows(), [4])


if __name__ == "__main__":
    unittest.main(verbosity=2)
