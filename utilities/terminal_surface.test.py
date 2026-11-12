#!/usr/bin/env python3
"""Tests for utilities/terminal_surface.py.

No test reaches the user's terminal: every Herdr call goes to an in-memory fake,
every tmux call to a private server on a socket inside the test's own directory,
and setUp removes the inherited pane and socket variables even for a single-file run.
"""
from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import copy
import json
import os
from pathlib import Path
import shutil
import signal
import shlex
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
                "TMUX", "TMUX_PANE", "AGENT_HERDR_SESSION", "CMUX_WORKSPACE_ID", "CMUX_SURFACE_ID")

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


class FakeTidyHerdr(FakeHerdr):
    """Existing PTYs in split trees, with Herdr 0.9.5's mutation semantics."""

    def __init__(self, n=6, area=(10, 2, 200, 100)):
        super().__init__([FULL], area=area)
        self.panes = {f"w1:p{i}": {"tab_id": "w1:t1", "terminal_id": f"terminal-{i}",
                                  "title": "", "display_agent": "", "shell_pid": os.getpid()}
                      for i in range(n)}
        self.trees = {"w1:t1": self.chain(list(self.panes), "down", .25)}
        self.focused_pane = f"w1:p{n - 1}"
        self.zoomed, self.fail_at, self.failure, self.mutation_count = False, None, None, 0

    @classmethod
    def chain(cls, ids, direction, ratio=None):
        return ids[0] if len(ids) == 1 else {
            "direction": direction, "ratio": ratio or 1 / len(ids), "first": ids[0],
            "second": cls.chain(ids[1:], direction, ratio)}

    @staticmethod
    def ids(tree):
        return [tree] if isinstance(tree, str) else FakeTidyHerdr.ids(tree["first"]) + FakeTidyHerdr.ids(tree["second"])

    @classmethod
    def remove(cls, tree, pane):
        if tree == pane:
            return None
        if isinstance(tree, str):
            return tree
        a, b = cls.remove(tree["first"], pane), cls.remove(tree["second"], pane)
        return b if a is None else a if b is None else {**tree, "first": a, "second": b}

    @classmethod
    def insert(cls, tree, target, pane, direction, ratio):
        if tree == target:
            return {"first": target, "second": pane, "direction": direction, "ratio": max(.1, min(.9, ratio))}
        if isinstance(tree, str):
            return tree
        return {**tree, "first": cls.insert(tree["first"], target, pane, direction, ratio),
                "second": cls.insert(tree["second"], target, pane, direction, ratio)}

    def canonical(self):
        ids, counts = list(self.panes), ts._grid_rows(len(self.panes))
        rows, index = [], 0
        for count in counts:
            rows.append(self.chain(ids[index:index + count], "right"))
            index += count
        def join(parts):
            return parts[0] if len(parts) == 1 else {"first": parts[0], "second": join(parts[1:]),
                                                    "direction": "down", "ratio": 1 / len(parts)}
        self.trees["w1:t1"] = join(rows)

    def layout_snapshot(self, tab):
        panes, splits = [], []
        def walk(tree, rect, path="s"):
            if isinstance(tree, str):
                panes.append({"pane_id": tree, "rect": rect, "focused": tree == self.focused_pane})
                return
            splits.append({"id": path, "direction": tree["direction"], "ratio": tree["ratio"], "rect": rect})
            axis, length = ("x", "width") if tree["direction"] == "right" else ("y", "height")
            first = int(rect[length] * tree["ratio"] + .5)
            walk(tree["first"], {**rect, length: first}, path + "0")
            walk(tree["second"], {**rect, axis: rect[axis] + first, length: rect[length] - first}, path + "1")
        walk(self.trees[tab], dict(self.area))
        return {"workspace_id": "w1", "tab_id": tab, "zoomed": self.zoomed, "area": dict(self.area),
                "focused_pane_id": self.focused_pane if self.focused_pane in self.ids(self.trees[tab]) else panes[0]["pane_id"],
                "panes": panes, "splits": splits}

    def metadata(self, pane):
        return {**self.panes[pane], "pane_id": pane, "workspace_id": "w1", "focused": pane == self.focused_pane}

    def __call__(self, argv):
        args = list(argv[1:])
        if args[:1] == ["--session"]:
            args = args[2:]
        self.calls.append(args)
        verb = args[:2]
        if verb in (["pane", "move"], ["pane", "resize"], ["pane", "swap"]):
            self.mutation_count += 1
            if self.mutation_count == self.fail_at:
                if callable(self.failure):
                    return self.failure(args)
                if isinstance(self.failure, BaseException):
                    raise self.failure
                return _done({"result": {"move_result": {"changed": False, "reason": "same_tab"}}})
        def arg(key):
            return args[args.index(key) + 1]
        if verb == ["workspace", "get"]:
            return _done({"result": {"workspace": {"workspace_id": "w1", "focused": True,
                "active_tab_id": self.focused_tab, "label": "my workspace"}}})
        if verb == ["tab", "list"]:
            return _done({"result": {"tabs": copy.deepcopy(self.tabs)}})
        if verb == ["pane", "list"]:
            return _done({"result": {"panes": [self.metadata(p) for p in self.panes]}})
        if verb == ["pane", "get"]:
            return _done({"result": {"pane": self.metadata(args[2])}})
        if verb == ["pane", "process-info"]:
            pane = arg("--pane")
            return _done({"result": {"process_info": {"pane_id": pane, "shell_pid": self.panes[pane]["shell_pid"]}}})
        if verb == ["pane", "layout"]:
            pane = arg("--pane")
            return _done({"result": {"layout": self.layout_snapshot(self.panes[pane]["tab_id"])}})
        if verb == ["pane", "move"]:
            pane, previous = args[2], self.panes[args[2]]["tab_id"]
            self.assert_no_focus(args)
            created, closed = None, None
            if "--new-tab" in args:
                assert arg("--workspace") == "w1"
                tab = "w1:t-stage"
                created = {"tab_id": tab, "workspace_id": "w1", "number": 2, "label": "", "focused": False}
            else:
                tab = arg("--tab")
                if previous == tab:
                    return _done({"result": {"move_result": {"changed": False, "reason": "same_tab"}}})
                assert self.panes[arg("--target-pane")]["tab_id"] == tab
            tree = self.remove(self.trees[previous], pane)
            if tree is None:
                closed = previous
                del self.trees[previous]
                self.tabs = [t for t in self.tabs if t["tab_id"] != previous]
            else:
                self.trees[previous] = tree
            if created:
                self.tabs.append(created)
                self.trees[tab] = pane
            else:
                self.trees[tab] = self.insert(self.trees[tab], arg("--target-pane"), pane,
                                               arg("--split"), float(arg("--ratio")))
            self.panes[pane]["tab_id"] = tab
            return _done({"result": {"move_result": {"changed": True, "previous_pane_id": pane,
                "previous_tab_id": previous, "previous_workspace_id": "w1", "pane": self.metadata(pane),
                "source_layout": self.layout_snapshot(previous) if not closed else None,
                "target_layout": self.layout_snapshot(tab), "created_tab": created,
                "closed_tab_id": closed, "focused_pane_id": self.layout_snapshot(tab)["focused_pane_id"]}}})
        if verb == ["pane", "swap"]:
            source, target = arg("--source-pane"), arg("--target-pane")
            tab = self.panes[source]["tab_id"]
            assert self.panes[target]["tab_id"] == tab
            def swap(tree):
                if isinstance(tree, str):
                    return target if tree == source else source if tree == target else tree
                return {**tree, "first": swap(tree["first"]), "second": swap(tree["second"])}
            self.trees[tab] = swap(self.trees[tab])
            self.focused_pane, self.focused_tab = source, tab
            return _done({"result": {"swap": {"changed": True, "source_pane_id": source,
                "target_pane_id": target, "focused_pane_id": source, "layout": self.layout_snapshot(tab)}}})
        if verb == ["pane", "resize"]:
            pane, direction = arg("--pane"), arg("--direction")
            tab = self.panes[pane]["tab_id"]
            snapshot = self.layout_snapshot(tab)
            rect = next(p["rect"] for p in snapshot["panes"] if p["pane_id"] == pane)
            axis, size, cross, cross_size = ("x", "width", "y", "height") if direction in ("left", "right") else ("y", "height", "x", "width")
            def candidates(grow):
                edge = rect[axis] + (rect[size] if grow else 0)
                return [s for s in snapshot["splits"] if s["direction"] == ("right" if axis == "x" else "down")
                        and abs(s["rect"][axis] + int(s["rect"][size] * s["ratio"] + .5) - edge) <= 1
                        and max(s["rect"][cross], rect[cross]) < min(s["rect"][cross] + s["rect"][cross_size], rect[cross] + rect[cross_size])]
            grow = direction in ("right", "down")
            found = candidates(grow) or candidates(not grow)
            if not found:
                return _done({"result": {"resize": {"changed": False}}})
            boundary = found[0]
            node = self.trees[tab]
            for bit in boundary["id"][1:]:
                node = node["first" if bit == "0" else "second"]
            old = node["ratio"]
            node["ratio"] = max(.1, min(.9, old + min(.5, abs(float(arg("--amount")))) * (1 if grow else -1)))
            return _done({"result": {"resize": {"changed": old != node["ratio"], "pane_id": pane,
                "focused_pane_id": self.focused_pane, "layout": self.layout_snapshot(tab)}}})
        raise AssertionError(f"unexpected tidy command: {args}")

    @staticmethod
    def assert_no_focus(args):
        assert "--no-focus" in args and "--focus" not in args

    def mutations(self):
        return [a for a in self.calls if a[:2] in (["pane", "move"], ["pane", "resize"], ["pane", "swap"])]


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

    def test_herdr_zoom_flag_keeps_the_grid_decision(self):
        # Herdr always reports `zoomed`; peer-steward never read it, so placement does not either.
        fake = FakeHerdr([TWO])

        def zoomed(argv):
            result = fake(argv)
            if argv[1:3] == ["pane", "layout"]:
                payload = json.loads(result.stdout)
                payload["result"]["layout"]["zoomed"] = True
                return _done(payload)
            return result
        self.assertEqual(ts.place(ts.HerdrSurface(zoomed), "w1:p0"), ("split", "w1:p1", "down"))

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

    def test_a_raising_post_open_read_is_a_note_not_a_failure(self):
        fake = FakeHerdr([FULL])
        state = {"opened": False}

        def run(argv):
            if argv[1:3] == ["pane", "list"] and state["opened"]:
                raise RuntimeError("checked socket timed out")
            if argv[1:3] == ["pane", "run"]:
                state["opened"] = True
            return fake(argv)
        result = ts.open_pane(["true"], near=ts.Near(ts.HerdrSurface(run), pane="w1:p0"))
        self.assertEqual((result.placement, result.notes), ("split", ["pane-list-unconfirmed"]))
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

    def test_a_second_opener_waits_for_the_first_launch(self):
        import multiprocessing
        ctx = multiprocessing.get_context("fork")
        first_in, release, second_in = ctx.Event(), ctx.Event(), ctx.Event()

        def opener(name):
            def launch(pane, created):
                if name == "first":
                    first_in.set()
                    release.wait(5)
                else:
                    second_in.set()
                return True
            ts.open_pane(launch=launch, near=ts.Near(ts.HerdrSurface(FakeHerdr([FULL])), pane="w1:p0"))
        first = ctx.Process(target=opener, args=("first",))
        second = ctx.Process(target=opener, args=("second",))
        first.start()
        try:
            self.assertTrue(first_in.wait(5))
            second.start()
            self.assertFalse(second_in.wait(.3))
            release.set()
            self.assertTrue(second_in.wait(5))
        finally:
            release.set()
            for child in (first, second):
                if child.pid:
                    child.join(5)
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_lock_file_lives_under_the_peer_start_root_and_reenters(self):
        surface = ts.HerdrSurface()
        with ts.workspace_lock(surface, "w1"):
            with ts.workspace_lock(surface, "w1"):
                path = Path(os.environ["AGENT_PEER_LEDGER_ROOT"]) / "peer-starts" / f"{surface.lock_key('w1')}.lock"
                self.assertIn(path, ts._HELD)
                self.assertTrue(path.exists())
            self.assertIn(path, ts._HELD)
        self.assertEqual(ts._HELD, set())


class TidyGeometryTest(unittest.TestCase):
    def test_required_shapes_and_full_width_odd_cells(self):
        area = dict(zip(ts._RECT, (3, 7, 201, 101)))
        for n, counts in ((1, [1]), (2, [2]), (4, [2, 2]), (5, [3, 2]), (6, [3, 3])):
            for border in (0, 1):
                cells = ts._grid_cells(area, n, border)
                ys = sorted({r["y"] for r in cells})
                rows = [[r for r in cells if r["y"] == y] for y in ys]
                self.assertEqual([len(row) for row in rows], counts)
                self.assertEqual(sum(row[0]["height"] for row in rows) + border * (len(rows) - 1), 101)
                for row in rows:
                    self.assertEqual(sum(r["width"] for r in row) + border * (len(row) - 1), 201)
                    self.assertLessEqual(max(r["width"] for r in row) - min(r["width"] for r in row), 1)

    def test_reading_order_supervisors_and_bad_rects(self):
        fake = FakeTidyHerdr()
        fake.canonical()
        layout = fake.layout_snapshot("w1:t1")
        for pane in layout["panes"]:
            pane["title"] = "[50] ⚑ codex" if pane["pane_id"] in {"w1:p2", "w1:p4"} else ""
        layout["panes"].reverse()
        self.assertEqual(ts._tidy_order(layout, "w1:p1"), ["w1:p2", "w1:p0", "w1:p1", "w1:p3", "w1:p4", "w1:p5"])
        cells = ts._grid_cells(layout["area"], 6)
        layout["panes"][0]["rect"]["width"] += 20
        with self.assertRaises(ts._TidyError):
            ts._grid_matches(layout, cells)
        for mutate in (lambda l: l["panes"].append(copy.deepcopy(l["panes"][0])),
                       lambda l: l["panes"][0]["rect"].update(width=0),
                       lambda l: l["area"].update(height=True)):
            invalid = fake.layout_snapshot("w1:t1")
            mutate(invalid)
            with self.assertRaises(ts._TidyError):
                ts._reading(invalid)

    def test_large_ratio_difference_is_not_a_grid_and_small_area_is_refused(self):
        fake = FakeTidyHerdr(2)
        fake.canonical()
        fake.trees["w1:t1"]["ratio"] = .8
        self.assertFalse(ts._grid_matches(fake.layout_snapshot("w1:t1"), ts._grid_cells(fake.area, 2)))
        with self.assertRaisesRegex(ts._TidyError, "too-small"):
            ts._grid_cells(dict(zip(ts._RECT, (0, 0, 8, 5))), 6)


class TidyHerdrTest(_IsolatedMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch("session_tidy.is_worker", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def tidy(self, fake, caller="w1:p1", runner=None):
        return ts.tidy_panes(near=ts.Near(ts.HerdrSurface(runner or fake), pane=caller))

    def test_different_supervisor_caller_focus_then_idempotent_for_each_shape(self):
        for n, counts in ((2, [2]), (4, [2, 2]), (5, [3, 2]), (6, [3, 3])):
            with self.subTest(n=n):
                fake = FakeTidyHerdr(n)
                marked = f"w1:p{n - 2}"
                fake.panes[marked]["display_agent"] = "[50] ⚑ codex"
                identity = {p: (v["terminal_id"], v["shell_pid"]) for p, v in fake.panes.items()}
                focus = (fake.focused_pane, fake.focused_tab)
                result = self.tidy(fake)
                self.assertEqual(result.notes, [], result)
                self.assertTrue(result.changed)
                self.assertEqual(result.panes, [marked] + [f"w1:p{i}" for i in range(n) if f"w1:p{i}" != marked])
                layout = fake.layout_snapshot("w1:t1")
                self.assertEqual([sum(p["rect"]["y"] == y for p in layout["panes"])
                                  for y in sorted({p["rect"]["y"] for p in layout["panes"]})], counts)
                self.assertEqual((fake.focused_pane, fake.focused_tab), focus)
                self.assertEqual(identity, {p: (v["terminal_id"], v["shell_pid"]) for p, v in fake.panes.items()})
                self.assertEqual([t["tab_id"] for t in fake.tabs], ["w1:t1"])
                mutations = fake.mutations()
                moves = [a for a in mutations if a[1] == "move"]
                self.assertEqual(len(moves), 2 * (n - 1))
                for args in moves:
                    self.assertIn("--no-focus", args)
                    self.assertTrue("--new-tab" in args or "--tab" in args and "--target-pane" in args)
                self.assertTrue(all(a[a.index("--source-pane") + 1] == focus[0]
                                    for a in mutations if a[1] == "swap"))
                fake.calls.clear()
                again = self.tidy(fake)
                self.assertEqual(again.notes, [], again)
                self.assertFalse(again.changed)
                self.assertEqual(fake.mutations(), [])

    def test_return_command_order_and_thirds_are_fixed(self):
        for n, expected in ((2, [("w1:p0", "w1:p1", "right", .5)]),
                             (4, [("w1:p1", "w1:p3", "down", .5), ("w1:p0", "w1:p3", "right", .5), ("w1:p2", "w1:p1", "right", .5)]),
                             (5, [("w1:p2", "w1:p4", "down", .5), ("w1:p0", "w1:p4", "right", 1/3), ("w1:p1", "w1:p0", "right", .5), ("w1:p3", "w1:p2", "right", .5)]),
                             (6, [("w1:p2", "w1:p5", "down", .5), ("w1:p0", "w1:p5", "right", 1/3), ("w1:p1", "w1:p0", "right", .5), ("w1:p3", "w1:p2", "right", 1/3), ("w1:p4", "w1:p3", "right", .5)])):
            fake = FakeTidyHerdr(n)
            result = self.tidy(fake)
            self.assertEqual(result.notes, [], result)
            moves = [a for a in fake.mutations() if a[1] == "move" and "--tab" in a and a[a.index("--tab") + 1] == "w1:t1"]
            actual = [(a[2], a[a.index("--target-pane") + 1], a[a.index("--split") + 1], float(a[a.index("--ratio") + 1])) for a in moves]
            for got, want in zip(actual, expected):
                self.assertEqual(got[:3], want[:3])
                self.assertAlmostEqual(got[3], want[3])
            self.assertEqual(len(actual), len(expected))

    def test_matching_grid_reads_only_and_order_only_uses_swaps(self):
        fake = FakeTidyHerdr()
        fake.canonical()
        self.assertFalse(self.tidy(fake, "w1:p0").changed)
        self.assertEqual(fake.mutations(), [])
        result = self.tidy(fake, "w1:p3")
        self.assertEqual(result.notes, [], result)
        self.assertEqual(result.panes, ["w1:p3", "w1:p0", "w1:p1", "w1:p2", "w1:p4", "w1:p5"])
        self.assertTrue(all(a[1] == "swap" for a in fake.mutations()))

    def test_canonical_tree_uses_resize_before_swaps_without_staging(self):
        fake = FakeTidyHerdr()
        fake.canonical()
        fake.trees["w1:t1"]["ratio"] = .8
        fake.trees["w1:t1"]["first"]["ratio"] = .15
        fake.trees["w1:t1"]["second"]["ratio"] = .7
        result = self.tidy(fake, "w1:p3")
        self.assertEqual(result.notes, [], result)
        actions = [a[1] for a in fake.mutations()]
        self.assertNotIn("move", actions)
        self.assertEqual(actions[:3], ["resize"] * 3)
        resizes = [a for a in fake.mutations() if a[1] == "resize"]
        self.assertEqual([a[a.index("--direction") + 1] for a in resizes], ["up", "right", "left"])
        self.assertTrue(all(0 < float(a[a.index("--amount") + 1]) <= .5 for a in resizes))

    def test_move_refusal_timeout_and_interrupt_gather_only_owned_staging(self):
        for fail_at in (1, 3, 10):
            for error in (None, subprocess.TimeoutExpired("fake-herdr", 1), KeyboardInterrupt()):
                with self.subTest(fail_at=fail_at, error=type(error).__name__):
                    fake = FakeTidyHerdr()
                    fake.fail_at, fake.failure = fail_at, error
                    result = self.tidy(fake)
                    self.assertTrue(result.notes, result)
                    self.assertEqual(set(fake.panes), {f"w1:p{i}" for i in range(6)})
                    self.assertEqual({p["tab_id"] for p in fake.panes.values()}, {"w1:t1"})
                    self.assertEqual(fake.focused_pane, "w1:p5")
                    self.assertFalse(any(a[:2] in (["pane", "close"], ["tab", "create"], ["pane", "run"], ["pane", "send-text"], ["layout", "apply"]) for a in fake.calls))

    def test_ambiguous_first_move_after_server_applied_it_is_observed_and_gathered(self):
        for mode in ("timeout", "json", "wrong-id"):
            fake = FakeTidyHerdr()
            fake.fail_at = 1
            def applied(args):
                fake.fail_at = None
                response = fake(["herdr", *args])
                if mode == "timeout":
                    raise subprocess.TimeoutExpired("herdr", 1)
                if mode == "json":
                    return subprocess.CompletedProcess([], 0, stdout="{bad json", stderr="")
                block = json.loads(response.stdout)
                block["result"]["move_result"]["pane"]["pane_id"] = "foreign:p0"
                return _done(block)
            fake.failure = applied
            result = self.tidy(fake)
            self.assertTrue(result.notes)
            self.assertEqual({p["tab_id"] for p in fake.panes.values()}, {"w1:t1"})
            self.assertEqual(len(fake.tabs), 1)

    def test_cleanup_failure_is_partial_and_preserves_live_panes(self):
        fake = FakeTidyHerdr()
        calls = {"moves": 0}
        def run(argv):
            if argv[1:3] == ["pane", "move"]:
                calls["moves"] += 1
                if calls["moves"] >= 3:
                    return _done(None, 1)
            return fake(argv)
        result = self.tidy(fake, runner=run)
        self.assertTrue(result.changed)
        self.assertTrue(any("staging-retained" in n for n in result.notes), result)
        self.assertEqual(len(fake.panes), 6)

    def test_sigint_sigterm_restore_handlers_and_gather_staging(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            fake = FakeTidyHerdr()
            fake.fail_at = 3
            def terminate(_args):
                os.kill(os.getpid(), sig)
            fake.failure = terminate
            before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
            result = self.tidy(fake)
            self.assertTrue(any(sig.name in note for note in result.notes), result)
            self.assertEqual({p["tab_id"] for p in fake.panes.values()}, {"w1:t1"})
            self.assertEqual(before, {s: signal.getsignal(s) for s in before})

    def test_resize_and_swap_noop_are_failure(self):
        for resize in (True, False):
            fake = FakeTidyHerdr()
            fake.canonical()
            if resize:
                fake.trees["w1:t1"]["ratio"] = .7
            fake.fail_at = 1
            result = self.tidy(fake, "w1:p3")
            self.assertTrue(result.notes)
            self.assertEqual(len(fake.panes), 6)

    def test_concurrent_focus_change_stops_before_next_mutation(self):
        fake = FakeTidyHerdr()
        def run(argv):
            response = fake(argv)
            if fake.mutation_count == 1:
                fake.focused_pane = "w1:p4"
            return response
        result = self.tidy(fake, runner=run)
        self.assertTrue(result.notes)
        self.assertEqual(fake.focused_pane, "w1:p4")
        self.assertFalse(any(a[1] == "swap" for a in fake.mutations()))
        self.assertFalse(any("staging-retained" in n for n in result.notes), result)
        self.assertEqual({p["tab_id"] for p in fake.panes.values()}, {"w1:t1"})

    def test_one_pane_and_foreign_or_duplicate_layout_refuse_without_mutations(self):
        fake = FakeTidyHerdr(1)
        self.assertEqual(self.tidy(fake, "w1:p0").notes, [])
        self.assertEqual(fake.mutations(), [])
        for foreign in (True, False):
            fake = FakeTidyHerdr()
            def corrupt(argv):
                response = fake(argv)
                if argv[1:3] == ["pane", "layout"]:
                    block = json.loads(response.stdout)
                    panes = block["result"]["layout"]["panes"]
                    panes[0]["pane_id"] = "other:p0" if foreign else panes[-1]["pane_id"]
                    return _done(block)
                return response
            self.assertTrue(self.tidy(fake, runner=corrupt).notes)
            self.assertEqual(fake.mutations(), [])

    def test_resize_swap_result_failure_matrix_and_confirmed_no_change(self):
        for verb in ("resize", "swap"):
            for mode in ("false", "json", "timeout", "wrong-id", "interrupt"):
                with self.subTest(verb=verb, mode=mode):
                    fake = FakeTidyHerdr()
                    fake.canonical()
                    if verb == "resize":
                        fake.trees["w1:t1"]["ratio"] = .7
                    def fail(argv):
                        if argv[1:3] != ["pane", verb]:
                            return fake(argv)
                        if mode == "timeout":
                            raise subprocess.TimeoutExpired("herdr", 1)
                        if mode == "interrupt":
                            raise KeyboardInterrupt()
                        if mode == "json":
                            return subprocess.CompletedProcess(argv, 0, stdout="invalid", stderr="")
                        if mode == "false":
                            return _done({"result": {verb: {"changed": False}}})
                        block = json.loads(fake(argv).stdout)
                        block["result"][verb]["pane_id" if verb == "resize" else "source_pane_id"] = "other:p0"
                        return _done(block)
                    result = self.tidy(fake, "w1:p3", runner=fail)
                    self.assertTrue(result.notes, result)
                    if mode == "false":
                        self.assertFalse(result.changed)
                    self.assertEqual(fake.focused_pane, "w1:p5")
                    self.assertEqual(len(fake.panes), 6)

    def test_pane_natural_exit_during_staging_preserves_all_survivors(self):
        fake = FakeTidyHerdr()
        fake.fail_at = 4
        def exited(_args):
            tab = fake.panes["w1:p0"]["tab_id"]
            fake.trees[tab] = fake.remove(fake.trees[tab], "w1:p0")
            del fake.panes["w1:p0"]
            return _done(None, 1)
        fake.failure = exited
        result = self.tidy(fake)
        self.assertTrue(result.notes, result)
        self.assertEqual(set(fake.panes), {f"w1:p{i}" for i in range(1, 6)})
        self.assertEqual({p["tab_id"] for p in fake.panes.values()}, {"w1:t1"})
        self.assertEqual(fake.focused_pane, "w1:p5")

    def test_an_existing_other_tab_and_its_processes_are_untouched(self):
        fake = FakeTidyHerdr()
        fake.tabs.append({"tab_id": "w1:tOther", "workspace_id": "w1", "number": 9, "label": "keep me"})
        fake.trees["w1:tOther"] = "w1:pOther"
        fake.panes["w1:pOther"] = {"tab_id": "w1:tOther", "terminal_id": "other-terminal", "shell_pid": os.getpid()}
        before = copy.deepcopy(fake.panes["w1:pOther"])
        result = self.tidy(fake)
        self.assertEqual(result.notes, [], result)
        self.assertEqual(fake.panes["w1:pOther"], before)
        self.assertEqual({t["tab_id"] for t in fake.tabs}, {"w1:t1", "w1:tOther"})

    def test_final_identity_or_inventory_uncertainty_is_never_success(self):
        for mode in ("identity", "inventory"):
            fake = FakeTidyHerdr()
            def run(argv):
                response = fake(argv)
                if fake.mutation_count and argv[1:3] == ["pane", "get"] and mode == "identity":
                    block = json.loads(response.stdout)
                    block["result"]["pane"]["terminal_id"] = "replaced-terminal"
                    return _done(block)
                if fake.mutation_count and argv[1:3] == ["pane", "list"] and mode == "inventory":
                    return _done(None, 1)
                return response
            result = self.tidy(fake, runner=run)
            self.assertTrue(result.notes, result)
            self.assertEqual(len(fake.panes), 6)


class TidyCallerTest(_IsolatedMixin, unittest.TestCase):
    def test_worker_rejected_before_any_runner_even_with_near(self):
        with mock.patch("session_tidy.is_worker", return_value=True), \
             mock.patch.object(ts, "_run", side_effect=AssertionError("terminal touched")):
            self.assertEqual(ts.tidy_panes().notes, ["worker-refused"])
            fake = FakeTidyHerdr()
            self.assertEqual(ts.tidy_panes(near=ts.Near(ts.HerdrSurface(fake), pane="w1:p0")).notes, ["worker-refused"])
            self.assertEqual(fake.calls, [])

    def test_plain_shell_proof_rechecked_under_lock_and_endpoint_bound(self):
        os.environ.update(HERDR_PANE_ID="w1:p1", HERDR_TAB_ID="foreign:t9", AGENT_HERDR_SESSION="private")
        fake = FakeTidyHerdr()
        with mock.patch("session_tidy.is_worker", return_value=False), \
             mock.patch.object(ts, "_caller_herdr_pane", return_value=""), \
             mock.patch("pane_ownership.verified_terminal_caller", return_value=True) as proof, \
             mock.patch.object(ts, "_run", side_effect=fake):
            result = ts.tidy_panes()
        self.assertEqual(result.notes, [], result)
        self.assertGreaterEqual(proof.call_count, 2)
        self.assertEqual(result.tab, "w1:t1")

    def test_stale_pane_wrong_endpoint_and_missing_proof_do_not_mutate(self):
        os.environ.update(HERDR_PANE_ID="w1:p0", TMUX="/private/tmux.sock,1,0", TMUX_PANE="%0")
        fake = FakeTidyHerdr()
        with mock.patch("session_tidy.is_worker", return_value=False), \
             mock.patch.object(ts, "_caller_herdr_pane", return_value=""), \
             mock.patch("pane_ownership.verified_terminal_caller", return_value=False), \
             mock.patch.object(ts, "_run", return_value=_done(None, 1)):
            self.assertTrue(ts.tidy_panes().notes)
        self.assertEqual(fake.mutations(), [])

    def test_zoom_small_and_inactive_are_no_mutation_diagnostics(self):
        for mode in ("zoom", "small", "inactive"):
            fake = FakeTidyHerdr(4, area=(0, 0, 200, 100) if mode != "small" else (0, 0, 4, 4))
            fake.zoomed = mode == "zoom"
            if mode == "inactive":
                fake.focused_tab = "w1:tOther"
            with mock.patch("session_tidy.is_worker", return_value=False):
                result = ts.tidy_panes(near=ts.Near(ts.HerdrSurface(fake), pane="w1:p0"))
            self.assertTrue(result.notes, result)
            self.assertEqual(fake.mutations(), [])


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class OpenPaneTmuxTest(_IsolatedMixin, unittest.TestCase):
    """A private tmux server per test: its socket is in the test directory, never $TMUX."""

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], capture_output=True, text=True,
                              env=self.env, timeout=10, check=False)

    def start_server(self, border=None):
        self.socket = self.root / f"tmux-{border or 'default'}-{time.monotonic_ns()}.sock"
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


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class TidyTmuxTest(_IsolatedMixin, unittest.TestCase):
    tmux = OpenPaneTmuxTest.tmux
    start_server = OpenPaneTmuxTest.start_server
    focus = OpenPaneTmuxTest.focus

    def setUp(self):
        super().setUp()
        patch = mock.patch("session_tidy.is_worker", return_value=False)
        patch.start()
        self.addCleanup(patch.stop)

    def make_panes(self, n, border=None):
        near = self.start_server(border)
        for i in range(n - 1):
            split = self.tmux("split-window", "-d", "-t", self.reference, "-h" if i % 2 == 0 else "-v",
                              "-P", "-F", "#{pane_id}", "sleep 600")
            self.assertEqual(split.returncode, 0, split.stderr)
        ids = self.tmux("list-panes", "-t", self.reference, "-F", "#{pane_id}").stdout.split()
        self.tmux("select-pane", "-t", ids[-1])
        return near, ids

    def test_required_shapes_ids_process_focus_borders_and_idempotence(self):
        for n, counts in ((2, [2]), (4, [2, 2]), (5, [3, 2]), (6, [3, 3])):
            for border in (None, "top", "bottom"):
                with self.subTest(n=n, border=border):
                    near, ids = self.make_panes(n, border)
                    caller = ids[1]
                    marked = ids[-2]
                    self.tmux("select-pane", "-t", marked, "-T", "[50] ⚑ supervisor")
                    base = near.surface.tidy_snapshot(caller)
                    desired = [marked] + [p["pane_id"] for p in ts._reading(base) if p["pane_id"] != marked]
                    focus = self.focus()
                    calls = []
                    run = near.surface.runner
                    def record(argv):
                        calls.append(argv)
                        return run(argv)
                    near = ts.Near(ts.TmuxSurface(record, str(self.socket)), pane=caller)
                    result = ts.tidy_panes(near=near)
                    self.assertEqual(result.notes, [], result)
                    self.assertEqual(result.panes, desired)
                    final = near.surface.tidy_snapshot(caller)
                    self.assertEqual(final["identity"], base["identity"])
                    self.assertEqual(self.focus(), focus)
                    rows = sorted({p["rect"]["y"] for p in final["panes"]})
                    self.assertEqual([sum(p["rect"]["y"] == y for p in final["panes"]) for y in rows], counts)
                    self.assertTrue(all(a[1:3] == ["-S", str(self.socket)] for a in calls))
                    for a in calls:
                        if "swap-pane" in a:
                            source, target = a[a.index("-s") + 1], a[a.index("-t") + 1]
                            if final["focused_pane_id"] in (source, target):
                                self.assertEqual(target, final["focused_pane_id"])
                                self.assertNotIn("-d", a)
                            else:
                                self.assertIn("-d", a)
                    calls.clear()
                    again = ts.tidy_panes(near=near)
                    self.assertEqual(again.notes, [], again)
                    self.assertFalse(again.changed)
                    self.assertFalse(any("select-layout" in a or "swap-pane" in a for a in calls))

    def test_zoom_is_unchanged_and_operation_failure_restores_only_known_cells(self):
        near, ids = self.make_panes(6)
        self.tmux("resize-pane", "-Z", "-t", self.reference)
        before = self.tmux("display-message", "-p", "-t", self.reference, "#{window_layout}").stdout
        result = ts.tidy_panes(near=near)
        self.assertIn("pane-tab-zoomed", result.notes)
        self.assertFalse(result.changed)
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.reference, "#{window_layout}").stdout, before)
        self.tmux("resize-pane", "-Z", "-t", self.reference)
        base = near.surface.tidy_snapshot(self.reference)
        run = near.surface.runner
        def refuse(argv):
            if "swap-pane" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="refused")
            return run(argv)
        result = ts.tidy_panes(near=ts.Near(ts.TmuxSurface(refuse, str(self.socket)), pane=ids[1]))
        self.assertTrue(result.notes)
        final = near.surface.tidy_snapshot(self.reference)
        self.assertEqual(final["identity"], base["identity"])
        self.assertEqual(final["original_layout"], base["original_layout"])
        self.assertEqual(final["focused_pane_id"], base["focused_pane_id"])

    def test_plain_shell_cli_finds_its_tab_with_another_pane_focused(self):
        # Only this private server's environment loses worker markers. Production
        # still refuses this test runner's inherited registered-worker context.
        near = self.start_server()
        for key in ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                    "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL"):
            self.env.pop(key, None)
            self.tmux("set-environment", "-gu", key)
        created = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "/bin/sh")
        self.assertEqual(created.returncode, 0, created.stderr)
        caller = created.stdout.strip()
        for _ in range(5):
            self.assertEqual(self.tmux("split-window", "-d", "-h", "-t", caller, "sleep 600").returncode, 0)
        window = self.tmux("display-message", "-p", "-t", caller, "#{window_id}").stdout.strip()
        self.tmux("rename-window", "-t", window, "shell tidy fixture")
        self.tmux("select-window", "-t", window)
        ids = self.tmux("list-panes", "-t", window, "-F", "#{pane_id}").stdout.split()
        self.tmux("select-pane", "-t", ids[-1])
        surface = ts.TmuxSurface(socket=str(self.socket))
        before = surface.tidy_snapshot(caller)
        target = self.root / "cli-result.json"
        line = (shlex.join([sys.executable, str(_HERE / "pane.py"), "tidy"]) + " > " + shlex.quote(str(target))
                + "; " + shlex.join(["tmux", "-S", str(self.socket), "wait-for", "-S", "tidy-finished"]))
        self.tmux("send-keys", "-t", caller, "-l", line)
        self.tmux("send-keys", "-t", caller, "Enter")
        waited = self.tmux("wait-for", "tidy-finished")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        result = json.loads(target.read_text())
        self.assertEqual(result["notes"], [], result)
        self.assertEqual(result["tab"], window)
        self.assertEqual(result["panes"][0], caller)
        final = surface.tidy_snapshot(caller)
        self.assertEqual(final["identity"], before["identity"])
        self.assertEqual(final["focused_pane_id"], ids[-1])



_FAKE_TUI = r"""
import json, os, sys, termios, time, tty
from pathlib import Path
log = open(sys.argv[1], "ab", buffering=0)
if len(sys.argv) > 2:
    # The runtime's own session registry, as Claude writes it for this exact process.
    stat = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
    record = {"pid": os.getpid(), "sessionId": sys.argv[2], "procStart": stat[19],
              "kind": "interactive", "status": sys.argv[3]}
    registry = Path(os.environ["HOME"]) / ".claude" / "sessions"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{os.getpid()}.json").write_text(json.dumps(record))
if len(sys.argv) > 4:
    sys.path.insert(0, sys.argv[5])
    import pane_ownership
    Path(sys.argv[4]).write_text(json.dumps(pane_ownership.verified_pane(
        os.environ.get("TMUX_PANE", ""), "claude", sys.argv[2])))
fd = sys.stdin.fileno()
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h\x1b[2mTry a suggestion\x1b[0m\r\n> ")
sys.stdout.flush()
while True:
    data = os.read(fd, 4096)
    if not data:
        break
    log.write(data)
"""


@unittest.skipUnless(shutil.which("tmux") and shutil.which("bash"), "tmux or bash is not installed")
class TmuxAgentTerminalTest(_IsolatedMixin, unittest.TestCase):
    """The agent terminal operations on a private tmux server, against a fake runtime."""

    def setUp(self):
        super().setUp()
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            os.environ.pop(key, None)
        self.socket = self.root / f"t{time.monotonic_ns() % 10**8}.sock"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in _SURFACE_ENV and not key.startswith(("AGENT_DISPATCH", "OPENCODE_DISPATCH"))
                    and key not in ("AGENT_SESSION_ROLE", "FLEET_TITLE_REFRESH", "MEM_DISTILL")}
        self.env.update(SHELL="/bin/sh", PS1="$ ")
        started = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "a1", "-x", "160", "-y", "40",
                            "sh")
        if started.returncode:
            self.fail(f"isolated tmux server did not start: {started.stderr.strip()}")
        self.addCleanup(lambda: subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                                               capture_output=True, env=self.env, timeout=10, check=False))
        self.pane = self.tmux("display-message", "-p", "-t", "a1", "#{pane_id}").stdout.strip()
        self.surface = ts.TmuxSurface(socket=str(self.socket))
        (self.root / "tui.py").write_text(_FAKE_TUI)
        self.log = self.root / "input.log"

    def tmux(self, *args):
        return subprocess.run(["tmux", "-S", str(self.socket), *args], capture_output=True, text=True,
                              env=self.env, timeout=10, check=False)

    def start_runtime(self, *extra, name="claude"):
        line = shlex.join(["exec", "-a", name, sys.executable, str(self.root / "tui.py"),
                           str(self.log), *extra])
        # bash's `exec -a` names the runtime the way its launcher does (argv[0]).
        self.tmux("send-keys", "-t", self.pane, "-l", "bash -c " + shlex.quote(line))
        self.tmux("send-keys", "-t", self.pane, "Enter")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = self.surface.process_info(self.pane)
            found = [item for item in (info or {}).get("foreground_processes", ()) if item["argv"][:1] == [name]]
            screen = self.surface.read_visible(self.pane) or ""
            if found and "Try a suggestion" in screen and (len(extra) < 3 or Path(extra[2]).exists()):
                return found[0]["pid"]
            time.sleep(.05)
        self.fail("the fake runtime never took the pane's foreground")

    def test_process_info_lists_the_foreground_group_with_argv_and_the_shell(self):
        pid = self.start_runtime()
        info = self.surface.process_info(self.pane)
        shell = int(self.tmux("display-message", "-p", "-t", self.pane, "#{pane_pid}").stdout)
        self.assertEqual((info["pane_id"], info["shell_pid"]), (self.pane, shell))
        self.assertEqual(info["foreground_process_group_id"], pid)
        self.assertEqual([item["pid"] for item in info["foreground_processes"]], [pid])
        self.assertEqual(info["foreground_processes"][0]["argv"][0], "claude")
        self.assertEqual(os.stat(info["tty"]).st_rdev, os.stat(f"/proc/{pid}/fd/0").st_rdev)
        self.assertIsNone(self.surface.process_info("%999"))
        self.assertIsNone(self.surface.process_info("w1:p0"))

    def test_agent_reads_harness_session_and_the_runtime_turn_record(self):
        sid = "10000068-0000-4000-8000-000000000076"
        pid = self.start_runtime(sid, "idle")
        agent = self.surface.agent(self.pane)["result"]["agent"]
        self.assertEqual((agent["pane_id"], agent["agent"], agent["pid"], agent["agent_status"]),
                         (self.pane, "claude", pid, "idle"))
        self.assertEqual(agent["agent_session"], {"kind": "id", "value": sid})
        registry = Path(os.environ["HOME"]) / ".claude" / "sessions" / f"{pid}.json"
        record = json.loads(registry.read_text())
        for status, state in (("busy", "working"), ("waiting", "blocked"), ("shell", "unknown")):
            registry.write_text(json.dumps(dict(record, status=status)))
            self.assertEqual(self.surface.agent(self.pane)["result"]["agent"]["agent_status"], state)
        # A record of another process birth names nothing about this one.
        registry.write_text(json.dumps(dict(record, status="idle", procStart="1")))
        agent = self.surface.agent(self.pane)["result"]["agent"]
        self.assertNotIn("agent_session", agent)
        self.assertEqual(agent["agent_status"], "unknown")

    def test_a_shell_pane_has_no_agent_and_an_absent_server_is_unanswered(self):
        self.assertEqual(self.surface.agent(self.pane), {"error": {"code": "agent_not_found"}})
        self.assertEqual(self.surface.agents(), {"result": {"agents": []}})
        gone = ts.TmuxSurface(socket=str(self.root / "absent.sock"))
        self.assertIsNone(gone.agent("%0"))
        self.assertIsNone(gone.agents())

    def test_prompt_is_one_bracketed_paste_then_enter_and_keeps_leading_dashes(self):
        self.start_runtime()
        self.assertEqual(self.surface.prompt(self.pane, "-n /clear\nsecond line"),
                         (0, {"result": {"type": "prompt", "pane_id": self.pane}}))
        self.assertTrue(self.surface.send_text(self.pane, "draft"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.log.read_bytes().endswith(b"draft\x1b[201~"):
            time.sleep(.05)
        self.assertEqual(self.log.read_bytes(),
                         b"\x1b[200~-n /clear\rsecond line\x1b[201~\r\x1b[200~draft\x1b[201~")
        self.assertEqual(self.tmux("list-buffers").stdout, "")
        self.assertEqual(self.surface.prompt("%999", "x"),
                         (None, {"error": {"code": "herdr-not-started", "reason": "pane-unavailable"}}))
        self.assertEqual(self.surface.prompt(self.pane, ""), (None, {"error": {"code": "herdr-not-started"}}))

    def test_every_byte_of_a_long_or_semicolon_ended_body_arrives(self):
        self.start_runtime()
        body = "x" * 20000 + " end;"
        self.assertEqual(self.surface.prompt(self.pane, body)[0], 0)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.log.read_bytes().endswith(b"\x1b[201~\r"):
            time.sleep(.05)
        self.assertEqual(self.log.read_bytes(), b"\x1b[200~" + body.encode() + b"\x1b[201~\r")

    def test_a_pane_in_a_tmux_mode_receives_nothing(self):
        self.start_runtime()
        for mode in (("copy-mode",), ("choose-tree",)):
            with self.subTest(mode=mode[0]):
                self.assertEqual(self.tmux(*mode, "-t", self.pane).returncode, 0)
                self.assertEqual(self.surface.input_refusal(self.pane), "pane-in-mode")
                self.assertEqual(self.surface.prompt(self.pane, "first\nsecond"),
                                 (None, {"error": {"code": "herdr-not-started", "reason": "pane-in-mode"}}))
                self.assertFalse(self.surface.send_text(self.pane, "draft"))
                self.assertFalse(self.surface.send_keys(self.pane, "Enter"))
                self.assertFalse(self.surface.run(self.pane, "echo no"))
                self.assertFalse(self.surface.empty_shell(self.pane))
                self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane, "#{pane_in_mode}").stdout.strip(),
                                 "1")
                self.assertEqual(self.log.read_bytes(), b"")
                self.tmux("send-keys", "-t", self.pane, *(("-X", "cancel") if mode[0] == "copy-mode" else ("q",)))

    def test_a_synchronized_pane_is_refused_and_a_paste_never_reaches_its_neighbour(self):
        self.start_runtime()
        neighbour_log = self.root / "neighbour.log"
        line = shlex.join([sys.executable, str(self.root / "tui.py"), str(neighbour_log)])
        neighbour = self.tmux("split-window", "-d", "-t", self.pane, "-P", "-F", "#{pane_id}", line).stdout.strip()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "Try a suggestion" not in (self.surface.read_visible(neighbour) or ""):
            time.sleep(.05)
        self.tmux("set-option", "-w", "-t", self.pane, "synchronize-panes", "on")
        self.assertEqual(self.surface.input_refusal(self.pane), "pane-synchronized")
        self.assertEqual(self.surface.prompt(self.pane, "hello")[1]["error"]["reason"], "pane-synchronized")
        self.assertFalse(self.surface.run(self.pane, "echo no"))
        # A paste goes to its target only, even while the window is synchronized.
        self.assertEqual(self.surface._paste(self.pane, "only here\r", bracketed=False), "pasted")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.log.read_bytes():
            time.sleep(.05)
        self.assertEqual(self.log.read_bytes(), b"only here\r")
        self.assertEqual(neighbour_log.read_bytes(), b"")

    def test_a_title_is_shown_literally(self):
        self.assertTrue(self.surface.set_title(self.pane, "id=#{pane_id} #(echo x)"))
        self.assertEqual(self.tmux("display-message", "-p", "-t", self.pane, "#{pane_title}").stdout.strip(),
                         "id=#{pane_id} #(echo x)")

    def test_a_pane_address_names_its_server(self):
        real = os.path.realpath(self.socket)
        self.assertEqual(self.surface.address(self.pane), f"{self.pane}@{real}")
        listed = [agent["pane_id"] for agent in self.surface.agents()["result"]["agents"]]
        self.assertEqual(listed, [])
        other = self.root / "other.sock"
        self.assertEqual(self.surface.agent(f"{self.pane}@{other}"), {"error": {"code": "agent_not_found"}})
        self.assertIsNone(self.surface.process_info(f"{self.pane}@{other}"))
        self.assertIsNone(self.surface.read_visible(f"{self.pane}@{other}"))
        with mock.patch.object(ts.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertEqual(ts.pane_surface(f"%3@{other}", tmux="/s").socket, str(other))
        self.assertTrue(ts.same_tmux_pane(f"%3@{real}", "%3"))
        self.assertFalse(ts.same_tmux_pane(f"%3@{real}", f"%3@{other}"))

    def test_read_visible_keeps_faint_text_only_with_ansi(self):
        self.start_runtime()
        self.assertIn("\x1b[2mTry a suggestion", self.surface.read_visible(self.pane, ansi=True))
        self.assertNotIn("\x1b[", self.surface.read_visible(self.pane))
        self.assertIsNone(self.surface.read_visible("%999"))

    def test_wait_agent_returns_on_the_wanted_state_or_times_out(self):
        sid = "10000085-0000-4000-8000-000000000094"
        pid = self.start_runtime(sid, "busy")
        registry = Path(os.environ["HOME"]) / ".claude" / "sessions" / f"{pid}.json"
        self.assertEqual(self.surface.wait_agent(self.pane, ("idle",), 300), {"error": {"code": "timeout"}})
        record = json.loads(registry.read_text())
        registry.write_text(json.dumps(dict(record, status="idle")))
        waited = self.surface.wait_agent(self.pane, ("idle", "done"), 5000)
        self.assertEqual(waited["result"]["agent"]["agent_status"], "idle")
        self.assertEqual(self.surface.wait_agent("%999", ("idle",), 100), {"error": {"code": "agent_not_found"}})

    def test_pane_surface_chooses_by_pane_id(self):
        with mock.patch.object(ts.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertIsInstance(ts.pane_surface("%3", tmux="/s"), ts.TmuxSurface)
            self.assertEqual(ts.pane_surface("%3", tmux="/s").socket, "/s")
            self.assertIsInstance(ts.pane_surface("w1:p3"), ts.HerdrSurface)
        with mock.patch.object(ts.shutil, "which", return_value=None):
            self.assertIsNone(ts.pane_surface("%3"))
            self.assertIsNone(ts.pane_surface("w1:p3"))
        self.assertEqual(ts.caller_tmux_pane({"TMUX": "/s,1,0", "TMUX_PANE": "%4"}), ("%4@/s", "/s"))
        self.assertEqual(ts.caller_tmux_pane({"TMUX_PANE": "%4"}), ("", None))
        self.assertEqual(ts.caller_tmux_pane({"TMUX": "/s,1,0", "TMUX_PANE": "w1:p0"}), ("", None))

    def test_notify_is_one_literal_status_message_on_attached_clients_only(self):
        self.assertFalse(self.surface.notify("hearting-question nobody attached"))
        if not shutil.which("script"):
            self.skipTest("script is not installed")
        # The client attaches to a session of its own, so no key it might pass reaches the shell pane.
        self.tmux("new-session", "-d", "-s", "viewer", "-x", "120", "-y", "30", "sleep 600")
        out = self.root / "client.out"
        client = subprocess.Popen(
            ["script", "-qfc", shlex.join(["tmux", "-S", str(self.socket), "attach", "-t", "viewer"]), str(out)],
            env=dict(self.env, TERM="xterm-256color"), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (client.kill(), client.wait(timeout=10), client.stdin.close()))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self.tmux("list-clients", "-F", "#{client_name}").stdout.strip():
            time.sleep(.1)
        before = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_title} #{pane_active}").stdout
        self.assertTrue(self.surface.notify("hearting-question #{pane_id} 100%"))
        while time.monotonic() < deadline and b"hearting-question #{pane_id} 100%" not in out.read_bytes():
            time.sleep(.1)
        self.assertIn(b"hearting-question #{pane_id} 100%", out.read_bytes())
        # Display only: no pane, title or focus changed.
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_title} #{pane_active}").stdout,
                         before)

    def test_verified_pane_proves_the_runtime_in_its_own_tmux_pane_only(self):
        sid = "9b2f3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"
        out = self.root / "verified.json"
        self.start_runtime(sid, "idle", str(out), str(_HERE), name="claude")
        self.assertEqual(json.loads(out.read_text()), self.pane)
        import pane_ownership
        # This test process is not that runtime: the same pane proves nothing for it.
        self.assertEqual(pane_ownership.verified_pane(self.pane, "claude", sid, tmux=str(self.socket)), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
