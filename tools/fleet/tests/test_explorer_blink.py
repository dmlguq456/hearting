"""The existing live phase changes only active text and supervisor flags."""
import copy
import re
import unicodedata
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tools.fleet import model, render
from tools.fleet.explorer import live, pages, peers, tables
from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.grid import CellGrid
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.style import normalize_style, steward_flag_key
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture
from tools.fleet.tests.explorer_capture import cell_style, palette

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
NAMES = ("plan", "execute", "review", "test", "report")
STATES = ("done", "active", "active", "todo", "failed")
DIM_KEYS = ("version_release", "h_claude", "g_work_off", "lvl_y_dim", "h_codex")


def blink_view_model():
    """One fixed-clock VM shared by the cell checks and the visual evidence."""
    values = load_fixture(FIXTURES / "view_model.json").to_mapping()
    template = values["sessions"][0]
    values["sessions"] = [
        dict(template, tag=tag, harness=harness, state="idle", steward=True,
             title=title, repo="hearting", repo_id="hearting", gpu=None, running=None,
             pane=None, pane_lines=[], now="Supervising the current route")
        for tag, harness, title in (("c1", "claude", "Route supervisor"),
                                    ("c2", "codex", "Implementation supervisor"),
                                    ("c3", "opencode", "Review supervisor"))]
    stages = [dict(name=name, st=status, r=1) for name, status in zip(NAMES, STATES)]
    route = dict(values["routes"][0], id="blink", repo="hearting", session_tag="c1",
                 title="Fleet stage and supervisor indicators", state="working",
                 legs=[["code", "active", "1/5"]], stages=stages,
                 worker=["worker", [["codex", "hb_codex"]], [["running 3m", "dim"]]],
                 worker_stage="execute", worker_stage_key=["c", 1, 0],
                 worker_step_key=["c", 0, 0], worker_unit="dev/frontend",
                 capability="code", mode="dev", shape="staged", tree_label="code",
                 detail_title="Fleet stage and supervisor indicators", detail_trailer="1 leg",
                 worktree=None, loops=None, resume=None, activity="running")
    values["routes"] = [route]
    values["repos"] = [dict(id="hearting", name="hearting", done=0, age="3m")]
    values["nodes"] = {"blink": [
        dict(kind="leg", n="code", st="active", verdict="running",
             title=[["Implementation", "head"]], lines=[], loops=[],
             detail=[dict(n=name, st=status, verdict=status, unit="dev/frontend",
                          when="3m", out="source", says="")
                     for name, status in zip(NAMES, STATES)])]}
    values["peer_nodes"] = [["hearting", [["c1"], ["c2"], ["c3"]]]]
    values["peer_edges"] = {
        "links": [[["c1", "claude"], ["c2", "codex"], "steer", "3m ago", "Current route"]],
        "cards": {"c1": {"owners": [
            {"state": "working", "label": "code",
             "stages": [dict(name=name, state=status) for name, status in zip(NAMES, STATES)]}]}},
        "cross": [], "side_lanes": []}
    values.update(questions=[], seats=[], attempts={}, ordering={})
    return ExplorerViewModel.from_mapping(values)


def blink_states():
    return {"route-detail": NavState(scope="route:blink"),
            "routes": NavState(view="ROUTES", title="ROUTES"),
            "peers": NavState(view="PEERS", title="PEERS")}


def _grid(beat, cols=200, rows=65):
    grid = CellGrid(cols, rows)
    grid.beat = beat
    return grid


def _changes(on, off):
    return {(y, x): (a[1], b[1]) for y, row in enumerate(on.cells)
            for x, (a, b) in enumerate(zip(row, off.cells[y])) if a[1] != b[1]}


class BlinkAssertions(unittest.TestCase):
    def assert_structure(self, on, off):
        self.assertEqual([[c[0] for c in row] for row in on.cells],
                         [[c[0] for c in row] for row in off.cells])
        self.assertEqual([[c[2] for c in row] for row in on.cells],
                         [[c[2] for c in row] for row in off.cells])
        self.assertEqual(on.hits, off.hits)
        for y, row in enumerate(on.cells):
            for x, cell in enumerate(row):
                if cell[0] and "BOX DRAWINGS" in unicodedata.name(cell[0], ""):
                    self.assertEqual(cell, off.cells[y][x], (y, x, cell))

    def assert_hues(self, on, off):
        for (y, x), (bright, dim) in _changes(on, off).items():
            self.assertEqual(render._HUE_OF[bright][0], render._HUE_OF[dim][0], (y, x))
            self.assertTrue(render._HUE_OF[dim][1] & render._A_DIM, (y, x, dim))
            self.assertFalse(render._HUE_OF[dim][1] & render._A_BOLD, (y, x, dim))


class SessionRouteCountTests(BlinkAssertions):
    def test_only_open_current_counts_disappear_without_moving_or_recolouring_text(self):
        base = blink_view_model()
        title = "잘린 제목 Long title " * 12
        for cols in (100, 140, 168, 274, 410):
            for status in ("working", "idle", "dead", "done", "stale"):
                for current in (False, True):
                    with self.subTest(cols=cols, status=status, current=current):
                        vm = replace(base,
                                     sessions=tuple(dict(row, title=title) for row in base.sessions),
                                     routes=tuple(dict(row, current=current, state=status)
                                                  for row in base.routes))
                        on = draw(vm, NavState(), cols, 45, beat=1)
                        off = draw(vm, NavState(), cols, 45, beat=0)
                        # Include wide-character continuation cells when locating
                        # the suffix after a clipped CJK title.
                        lines = ["".join(cell[0] or "\0" for cell in row) for row in on.cells]
                        badge = [(y, line.index("route 1")) for y, line in enumerate(lines)
                                 if "route 1" in line]
                        if not current:
                            self.assertFalse(badge)
                            self.assert_structure(on, off)
                            continue
                        self.assertTrue(badge)
                        for y, x in badge:
                            for offset in range(len("route 1")):
                                a, b = on.cells[y][x + offset], off.cells[y][x + offset]
                                self.assertEqual(b[1], "dim")
                                self.assertEqual(a[1], "dim")
                                self.assertEqual(b[0], " " if status not in ("done", "stale") else a[0])
                                self.assertEqual(render._HUE_OF[a[1]][0], render._HUE_OF[b[1]][0])
                                self.assertFalse(render._HUE_OF[a[1]][1] & render._A_REVERSE)
                        masked = [list(row) for row in on.cells]
                        for y, x in badge:
                            for offset in range(len("route 1")):
                                masked[y][x + offset] = off.cells[y][x + offset]
                        adjusted = replace(on, cells=tuple(tuple(row) for row in masked))
                        self.assert_structure(adjusted, off)
                        expanded = draw(vm, NavState(opened="c1"), cols, 45, beat=1)
                        self.assertNotIn("route 1", "\n".join(expanded.text_rows()))


class StagePhaseTests(BlinkAssertions):
    def test_stage_boxes_pulse_only_active_names_and_dot_all_hues(self):
        for index in range(5):
            for status in ("active", "done", "todo", "failed"):
                for gate in (False, True):
                    with self.subTest(index=index, status=status, gate=gate):
                        on, off = _grid(1), _grid(0)
                        stage = dict(n="sample", st=status, kind="gate" if gate else "stage")
                        for grid in (on, off):
                            pages._stage_box(grid, 3, 2, 50, 8, index, stage)
                        self.assert_structure(on, off)
                        expected = {}
                        if status == "active":
                            line = on.rows_text()[2]
                            x = line.index("sample")
                            expected.update({(2, col): ("tag_steward_dim" if gate else DIM_KEYS[index])
                                             for col in range(x, x + 6)})
                            x = line.index("●")
                            expected[(2, x)] = "g_work_off"
                        self.assertEqual({pos: pair[1] for pos, pair in _changes(on, off).items()}, expected)
                        self.assert_hues(on, off)

    def test_parallel_group_uses_each_members_own_state(self):
        stages = [dict(n="pair", st="active", group=[
            dict(n="live-peer", st="active"), dict(n="done-peer", st="done")]),
                  dict(n="later", st="todo"), dict(n="failed", st="failed")]
        on, off = _grid(1), _grid(0)
        for grid in (on, off):
            pages._stage_grid(grid, 2, 2, stages, 180, 60)
        self.assert_structure(on, off)
        allowed = set()
        for y, line in enumerate(on.rows_text()):
            if "live-peer" in line:
                x = line.index("live-peer")
                allowed |= {(y, col) for col in range(x, x + len("live-peer"))}
                allowed.add((y, line.index("●")))
        self.assertEqual(set(_changes(on, off)), allowed)
        self.assertTrue(allowed)

    def test_stage_rails_and_current_leg_keep_metadata_and_other_states(self):
        for index in range(5):
            stages = [dict(name=name, st="active" if i == index else STATES[i],
                           r=3, host="gpu0") for i, name in enumerate(NAMES)]
            on, off = tables._stage_items(stages), tables._stage_items(stages, lit=False)
            for i, ((bright, _), (dim, _)) in enumerate(zip(on, off)):
                if stages[i]["st"] == "active":
                    self.assertEqual(dim[0][1], DIM_KEYS[i])
                    self.assertEqual(dim[2], (" ●", "g_work_off"))
                    self.assertEqual(bright[1], dim[1])  # round is steady
                    self.assertEqual(bright[3:], dim[3:])  # host is steady
                else:
                    self.assertEqual(bright, dim)
            self.assertIn((NAMES[index], DIM_KEYS[index]),
                          tables._minimal_stage_rail(stages, index, 45, lit=False))
        for status in ("active", "done", "todo", "failed"):
            on, off = _grid(1), _grid(0)
            for grid in (on, off):
                pages._current_leg(grid, 2, 3, [dict(n="sample", st=status)], 40)
            self.assert_structure(on, off)
            expected = {(3, x) for x in range(2, 8)} if status == "active" else set()
            self.assertEqual(set(_changes(on, off)), expected)

    def test_same_name_single_stage_rail_uses_stage_phase_and_keeps_metadata(self):
        for status in ("active", "done", "todo", "failed"):
            for leg_status in ("active", "done", "todo", "failed"):
                for width, prefix in ((9, False), (40, True), (64, True)):
                    with self.subTest(status=status, leg_status=leg_status, width=width):
                        legs = [["frame", "done", "1/1"]] if prefix else []
                        route = dict(legs=legs + [["execute", leg_status, "0/1"]],
                                     stages=[dict(name="execute", st=status, r=3, host="gpu0")],
                                     tree_label="execute", resume="R3")
                        on, off = _grid(1, width, 1), _grid(0, width, 1)
                        for grid in (on, off):
                            tables._draw_route_progress(grid, 0, width, 0, route)
                        mark = {"done": " ✓", "failed": " ✕"}.get(leg_status, "")
                        text = ("frame ✓ › " if prefix else "") + "execute" + mark + "  resume R3"
                        self.assertEqual(on.rows_text()[0], text[:width].ljust(width))
                        self.assertEqual(on.rows_text()[0].count("execute"), 1)
                        self.assert_structure(on, off)
                        x = on.rows_text()[0].index("execute")
                        bright = normalize_style("hb_opencode" if status == "active" else "title_dim")[0]
                        self.assertEqual([on.cells[0][col][1] for col in range(x, x + 7)],
                                         [bright] * 7)
                        expected = {(0, col): DIM_KEYS[0] for col in range(x, x + 7)} if status == "active" else {}
                        self.assertEqual({pos: pair[1] for pos, pair in _changes(on, off).items()}, expected)
                        self.assert_hues(on, off)
                        default = CellGrid(width, 1)
                        tables._draw_route_progress(default, 0, width, 0, route)
                        self.assertEqual(default.snapshot(), on.snapshot())

    def test_leg_only_and_narrow_leg_fallback_stay_steady(self):
        for stages in ([], [dict(name="execute", st="active")]):
            with self.subTest(stages=stages):
                route = dict(legs=[["code", "active", "0/1"]],
                             stages=stages, tree_label="code" if stages else None)
                on, off = _grid(1, 9, 1), _grid(0, 9, 1)
                for grid in (on, off):
                    tables._draw_route_progress(grid, 0, 9, 0, route)
                self.assertEqual(on.rows_text()[0].strip(), "code")
                self.assertEqual(on.snapshot(), off.snapshot())


class SupervisorPhaseTests(BlinkAssertions):
    def test_flag_role_and_liveness_boundaries_for_each_harness(self):
        for harness in ("claude", "codex", "opencode"):
            for state in ("working", "idle", "unused", "blocked", "unknown", "done", "dead", "stale"):
                for steward in (True, False):
                    with self.subTest(harness=harness, state=state, steward=steward):
                        session = dict(tag="a1", title="Supervisor", harness=harness,
                                       state=state, steward=steward)
                        on, off = _grid(1), _grid(0)
                        for grid in (on, off):
                            peers._draw_card(grid, session, "a1", 2, 2, 40, 60, "dim", [], [])
                        self.assert_structure(on, off)
                        expected = {}
                        if steward and state not in ("dead", "stale"):
                            x = on.rows_text()[2].index("⚑")
                            expected[(2, x)] = ("tag_steward", "tag_steward_dim")
                        self.assertEqual(_changes(on, off), expected)
                        self.assertEqual("⚑" in on.rows_text()[2], steward)
                        self.assert_hues(on, off)
                        self.assertEqual(steward_flag_key(session, lit=False),
                                         "tag_steward_dim" if expected else "tag_steward")

    def test_codex_turn_complete_stays_idle_and_retains_role_through_live_projection(self):
        evidence = dict(harness="codex", pid=100, pid_alive=True, status=None,
                        task_lifecycle="task_complete", mtime=1000.0, transcript=True)
        state, proof = model.classify_session(evidence, 1200.0)
        self.assertEqual((state, proof["source"]), ("idle", "codex-lifecycle"))
        session = dict(evidence, session_tag="a1", session_id="test-sid",
                       liveness=state, steward=True, cwd="/example/hearting",
                       title="Supervisor", model="gpt-6", effort="high")
        rows, _ = live._session_records({"sessions": [session]}, 1200.0, lambda _cwd: "hearting")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["steward"])
        self.assertEqual(rows[0]["state"], "idle")
        self.assertEqual(steward_flag_key(rows[0], lit=False), "tag_steward_dim")

    def test_peer_owner_active_dots_and_finished_owner_recede(self):
        owners = [dict(state="working", label="code",
                       stages=[dict(state=status) for status in STATES])]
        session = dict(tag="a1", title="Worker", harness="codex", state="idle")
        for ended in (False, True):
            on, off = _grid(1), _grid(0)
            owners[0]["state"] = "done" if ended else "working"
            for grid in (on, off):
                peers._draw_card(grid, session, "a1", 2, 2, 40, 60, "dim", [], owners)
            self.assert_structure(on, off)
            self.assertEqual({pos: pair[1] for pos, pair in _changes(on, off).items()},
                             {} if ended else {(6, 31): DIM_KEYS[1], (6, 32): DIM_KEYS[2]})


def route_count_spans(frame):
    """The OVERVIEW ROUTES rule's ``N open`` pulses on the working pair while N > 0."""
    spans = {}
    for y, line in enumerate(frame.text_rows()):
        match = re.search(r"── ROUTES  ([0-9]+ open)", line)
        if match and int(match.group(1).split()[0]) > 0:
            spans.update({(y, col): "g_work_off" for col in range(*match.span(1))})
    return spans


class FramePhaseTests(BlinkAssertions):
    def expected_spans(self, frame, state):
        """Declare spans from fixed semantic labels, never from the observed off diff."""
        expected = route_count_spans(frame)
        active = {"execute": "h_claude", "review": "g_work_off"}
        for y, line in enumerate(frame.text_rows()):
            for x, glyph in enumerate(line):
                if glyph == "⚑" and (x < 32 or state.view == "PEERS"):
                    expected[(y, x)] = "tag_steward_dim"
            pane = line[34:]
            if state.view == "PEERS" and state.scope == "-":
                strip = pane.find("✓●●·✕")
                if strip >= 0:
                    expected[(y, 34 + strip + 1)] = "h_claude"
                    expected[(y, 34 + strip + 2)] = "g_work_off"
                continue
            if "└ " in pane:
                x = line.find("running", 34)
                if x >= 0:
                    run = "running 3m"
                    length = next((size for size in range(len(run), len("running") - 1, -1)
                                   if line[x:x + size] == run[:size]), len("running"))
                    expected.update({(y, col): "h_claude" for col in range(x, x + length)})
                continue  # KIND is a steady stage label
            for name, dim in active.items():
                for match in re.finditer(name, pane):
                    expected.update({(y, 34 + col): dim
                                     for col in range(match.start(), match.end())})
            # At 80 columns the name in each active box is a single ellipsis.
            # The active box header (not its observed phase difference) owns it.
            for box in re.finditer(r"┌─ (\S+)[^┐]*┐", pane):
                if "●" in box.group() and "● running" not in box.group():
                    begin, end = box.span(1)
                    for col in range(34 + begin, 34 + end):
                        hue = render._HUE_OF[frame.cells[y][col][1]][0]
                        expected[(y, col)] = DIM_KEYS["lcgym".index(hue)]
            for x in range(34, frame.cols):
                if frame.cells[y][x][:2] == ("●", "g_work") and "● running" not in pane:
                    expected[(y, x)] = "g_work_off"
                    if frame.cells[y][x - 1][:2] == (" ", "g_work"):
                        expected[(y, x - 1)] = "g_work_off"
        return expected

    def test_complete_frames_keep_geometry_hits_tints_and_all_steady_ink(self):
        vm = blink_view_model()
        original = vm.to_mapping()
        states = list(blink_states().values()) + [
            NavState(), NavState(scope="session:c1"), NavState(scope="repo:hearting"),
            NavState(view="ROUTES", title="ROUTES", folds=(("ropen:ROUTES", "blink"),)),
            NavState(view="PEERS", title="PEERS", folds=(("pfocus:PEERS", "c1"),)),
            replace(blink_states()["route-detail"], usage_open=True, scroll=(("pane", 5),)),
            NavState(view="ROUTES", title="ROUTES", scroll=(("tree", 3),))]
        for cols in (80, 100, 140, 168):
            for state in states:
                with self.subTest(cols=cols, state=state):
                    before = copy.deepcopy(state)
                    on, off = (draw(vm, state, cols, 45, beat=beat) for beat in (1, 0))
                    self.assert_structure(on, off)
                    self.assertEqual((on.cols, on.rows, on.title, on.status, on.pane_scroll, on.tree_scroll),
                                     (off.cols, off.rows, off.title, off.status, off.pane_scroll, off.tree_scroll))
                    self.assertEqual({pos: pair[1] for pos, pair in _changes(on, off).items()},
                                     self.expected_spans(on, state))
                    self.assert_hues(on, off)
                    self.assertEqual(draw(vm, state, cols, 45, beat=0), off)
                    self.assertEqual(state, before)
        self.assertEqual(vm.to_mapping(), original)

    def test_tree_flags_follow_each_harness_and_selection_and_drag_preserve_tint(self):
        vm = blink_view_model()
        for state in (NavState(scope="session:c2"),
                      NavState(drag=("tree", "session", "c2", "hearting", 1, 15, "c3", True, True))):
            on, off = (draw(vm, state, 140, 45, beat=beat) for beat in (1, 0))
            for session in vm.sessions:
                phrase = f"[{session['tag']}] {session['harness']} ⚑ ●"
                y = next(y for y, line in enumerate(on.text_rows()) if phrase in line[:32])
                x = on.text_rows()[y].index("⚑")
                self.assertEqual((on.cells[y][x][1], off.cells[y][x][1]),
                                 ("tag_steward", "tag_steward_dim"))
                if session["tag"] == "c2":
                    self.assertEqual(on.cells[y][x][2], 236)
                    self.assertEqual(off.cells[y][x][2], 236)

    def test_pinned_overview_tail_and_tree_viewport_keep_the_phase(self):
        values = blink_view_model().to_mapping()
        seed = values["sessions"][-1]
        values["sessions"] += [dict(seed, tag=f"{i:02x}", steward=False, title="Session")
                               for i in range(20, 60)]
        vm = ExplorerViewModel.from_mapping(values)
        on, off = (draw(vm, NavState(), 168, 45, beat=b) for b in (1, 0))
        self.assert_structure(on, off)
        # The long session list forces the closing ROUTES/RECENT block to be pinned.
        y = next(y for y, line in enumerate(on.text_rows())
                 if "execute ●" in line[34:])
        self.assertGreater(y, 25)
        x = on.text_rows()[y].index("execute", 34)
        self.assertEqual((on.cells[y][x][1], off.cells[y][x][1]), ("hb_claude", "h_claude"))
        flag_y = next(y for y, line in enumerate(on.text_rows()) if "[c1] claude ⚑" in line[:32])
        flag_x = on.text_rows()[flag_y].index("⚑")
        self.assertEqual(off.cells[flag_y][flag_x][1], "tag_steward_dim")

    def test_same_name_single_stage_in_routes_pinned_and_scoped_frames(self):
        states = [NavState(view="ROUTES", title="ROUTES"), NavState(),
                  NavState(scope="session:c1"), NavState(scope="repo:hearting")]
        for status in ("active", "done", "todo", "failed"):
            values = blink_view_model().to_mapping()
            values["routes"][0].update(
                legs=[["execute", status, "0/1"]],
                stages=[dict(name="execute", st=status, r=3, host="gpu0")],
                activity="ended", worker_stage="execute",
                worker=["worker", [["codex", "hb_codex"]], [["worker ended", "dim"]]])
            vm = ExplorerViewModel.from_mapping(values)
            original = vm.to_mapping()
            seed = values["sessions"][-1]
            values["sessions"] += [dict(seed, tag=f"{i:02x}", steward=False, title="Session")
                                   for i in range(20, 60)]
            pinned_vm = ExplorerViewModel.from_mapping(values)
            for cols in (140, 168):
                for state in states:
                    with self.subTest(status=status, cols=cols, state=state):
                        current_vm = pinned_vm if state == NavState() else vm
                        on, off = (draw(current_vm, state, cols, 45, beat=b) for b in (1, 0))
                        self.assert_structure(on, off)
                        self.assertEqual((on.cols, on.rows, on.title, on.status, on.pane_scroll, on.tree_scroll),
                                         (off.cols, off.rows, off.title, off.status, off.pane_scroll, off.tree_scroll))
                        # The route row owns one label; the worker's KIND stays steady.
                        labels = [(y, match.start()) for y, line in enumerate(on.text_rows())
                                  if "└ worker" not in line[34:]
                                  for match in re.finditer(r"\bexecute\b", line)
                                  if match.start() >= 34]
                        self.assertEqual(len(labels), 1)
                        y, x = labels[0]
                        if state.scope == "-" and state.view == "OVERVIEW":
                            self.assertGreater(y, 25)  # actual pinned screen-grid consumer
                        expected = {(yy, xx): "tag_steward_dim"
                                    for yy, line in enumerate(on.text_rows())
                                    for xx, glyph in enumerate(line[:32]) if glyph == "⚑"}
                        expected.update(route_count_spans(on))
                        if status == "active":
                            expected.update({(y, col): DIM_KEYS[0] for col in range(x, x + 7)})
                            self.assertEqual(on.cells[y][x][1], "hb_opencode")
                        self.assertEqual({pos: pair[1] for pos, pair in _changes(on, off).items()}, expected)
                        self.assert_hues(on, off)
                        self.assertEqual(draw(current_vm, state, cols, 45, beat=0), off)
            self.assertEqual(vm.to_mapping(), original)

    def test_tree_role_release_and_invalid_liveness_do_not_pulse_a_flag(self):
        for state, role in (("idle", False), ("dead", True), ("stale", True),
                            ("unknown", True), ("done", True)):
            values = blink_view_model().to_mapping()
            values["sessions"] = [dict(values["sessions"][0], state=state, steward=role)]
            values["routes"] = []
            vm = ExplorerViewModel.from_mapping(values)
            on, off = (draw(vm, NavState(), 140, 45, beat=b) for b in (1, 0))
            self.assert_structure(on, off)
            tree_diff = {pos: pair for pos, pair in _changes(on, off).items() if pos[1] < 32}
            if role and state in ("unknown", "done"):
                self.assertEqual(list(tree_diff.values()), [("tag_steward", "tag_steward_dim")])
            else:
                self.assertFalse(tree_diff)

    def test_ended_worker_and_resource_worker_do_not_change_kind_or_harness(self):
        for label, activity, progress in (("worker", "ended", "worker ended"),
                                          ("worker", "running", "waiting"),
                                          ("resource", "running", "running 3m")):
            values = blink_view_model().to_mapping()
            values["routes"][0].update(activity=activity, worker=[
                label, [["codex", "hb_codex"]], [[progress, "dim"]]])
            vm = ExplorerViewModel.from_mapping(values)
            on, off = (draw(vm, blink_states()["routes"], 168, 45, beat=b) for b in (1, 0))
            y = next(y for y, line in enumerate(on.text_rows()) if "└ " + label in line[34:])
            x = on.text_rows()[y].index(progress, 34)
            for col in range(34, x):
                self.assertEqual(on.cells[y][col], off.cells[y][col])
            changed = any(on.cells[y][col] != off.cells[y][col] for col in range(x, x + len(progress)))
            self.assertEqual(changed, activity == "running" and progress.startswith("running"))


class StewardPaletteTests(unittest.TestCase):
    def test_dim_pink_palette_normal_fallback_and_tint(self):
        saved = {name: dict(getattr(render, name)) for name in ("_COLOR", "_TINT_PAIR")}
        tint_ok, blink = render._TINT_OK, render._BLINK_ON
        self.addCleanup(setattr, render, "_TINT_OK", tint_ok)
        self.addCleanup(setattr, render, "_BLINK_ON", blink)
        for name, value in saved.items():
            def restore(name=name, value=value):
                getattr(render, name).clear()
                getattr(render, name).update(value)
            self.addCleanup(restore)
        for fallback in (False, True):
            def init_pair(index, *_args):
                if fallback and index == render._STEWARD_PAIR:
                    raise RuntimeError("no dedicated pink pair")
            with mock.patch.multiple(render.curses, COLORS=256, COLOR_PAIRS=256,
                                     create=True,
                                     start_color=mock.Mock(), use_default_colors=mock.Mock(),
                                     can_change_color=mock.Mock(return_value=False),
                                     init_pair=init_pair, color_pair=lambda n: n << 8):
                render._init_colors()
            on, off = (render._COLOR[key] for key in ("tag_steward", "tag_steward_dim"))
            self.assertEqual(off, (on & ~render._A_BOLD) | render._A_DIM)
            self.assertFalse(off & render._A_BOLD)
            self.assertEqual(render._key_attr("tag_steward_dim"), off)
            render._TINT_OK = True
            render._TINT_PAIR[(236, "p")] = 199 << 8
            self.assertEqual(render._key_attr("tag_steward_dim", 236), (199 << 8) | render._A_DIM)
        self.assertEqual(render._HUE_OF["tag_steward_dim"], ("p", render._A_DIM))
        self.assertEqual(normalize_style(("p", 0, 1))[0], "unknown")  # gate outlines unchanged
        hues, tints = palette(render)
        for tint in (None, 236):
            bright = cell_style(render, hues, tints, "tag_steward", tint)
            dim = cell_style(render, hues, tints, "tag_steward_dim", tint)
            self.assertEqual(bright[1], dim[1])
            self.assertTrue(bright[2])
            self.assertFalse(dim[2])
            self.assertNotEqual(bright[0], dim[0])


if __name__ == "__main__":
    unittest.main()
