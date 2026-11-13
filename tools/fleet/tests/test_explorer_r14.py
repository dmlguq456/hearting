"""Focused renderer regressions for the r14 frame and dynamic data layouts."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw, measure_pane_max
from tools.fleet.explorer.state import NavState, reduce
from tools.fleet.explorer.view_model import ExplorerViewModel, load_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "explorer" / "view_model.json"


class R14Renderer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vm = load_fixture(FIXTURE)

    def test_navigation_has_six_views_and_one_compute_destination(self):
        frame = draw(self.vm, NavState(), cols=100, rows=45)
        tabs = [hit.action[1] for hit in frame.hits
                if hit.action and hit.action[0] == "tab"]
        self.assertEqual(tabs.count("COMPUTE"), 1)  # the persistent band opens the same pane via root
        self.assertTrue(any(hit.action == ("root", "COMPUTE") for hit in frame.hits))
        rows = frame.text_rows()
        views_y = next(i for i, row in enumerate(rows) if row.lstrip().startswith("OVERVIEW"))
        sessions_y = next(i for i, row in enumerate(rows) if "SESSIONS" in row)
        self.assertGreater(sessions_y - views_y, 6)
        self.assertIn("┌", rows[3])
        self.assertEqual(rows[2].strip(), "")
        self.assertIn("┴", rows[-4])

    def test_resources_label_keeps_the_internal_compute_navigation_key(self):
        frame = draw(self.vm, NavState(view="COMPUTE"), cols=140, rows=45)
        text = "\n".join(frame.text_rows())
        self.assertIn("RESOURCES", text)
        self.assertNotIn("COMPUTE", text)
        self.assertTrue(any(hit.action == ("tab", "COMPUTE") for hit in frame.hits))
        self.assertTrue(any(hit.action == ("root", "COMPUTE") for hit in frame.hits))

    def test_sol_astra_and_luna_get_distinct_family_colors_in_every_viewer(self):
        from tools.fleet import render
        from tools.fleet.explorer import live, models
        from tools.fleet.explorer.grid import CellGrid

        cases = (("sol", ("gpt-6.1-sol", "Sol", "gpt-6-sol")),
                 ("astra", ("gpt-6.1-astra", "Astra")),
                 ("luna", ("gpt-6-luna", "Luna")))
        for family, names in cases:
            for model in names:
                with self.subTest(model=model):
                    key = "fam_" + family
                    self.assertEqual(render._model_key(model), key)
                    self.assertEqual(live._model_pair(model)[1], key)
                    self.assertEqual(live._stage_model(model)[1], key)
                    grid = CellGrid(48, 3)
                    models._model_cell(grid, 1, 1, "codex", (model, "high"), {}, {}, 40)
                    self.assertEqual(grid.cells[1][1][1], key)
        hues = {render._HUE_OF["fam_" + family][0] for family, _names in cases}
        self.assertEqual(len(hues), 3)
        self.assertEqual(render._model_key("gpt-5.5"), "fam_gpt")

    def test_route_columns_leave_progress_room_at_100_140_and_168(self):
        from tools.fleet.explorer.tables import _route_columns

        for width in (100, 140, 168):
            use = width - 36
            cols = _route_columns(use)
            age_start = use - 6 - 3
            with self.subTest(width=width):
                self.assertLessEqual(cols["title_w"], 48)
                self.assertLess(cols["kind"], cols["own"])
                self.assertLess(cols["own"], cols["prog"])
                self.assertGreater(age_start - cols["prog"], 0)

    def test_peers_and_all_renderer_views_stay_inside_four_viewports(self):
        views = ("OVERVIEW", "PEERS", "ROUTES", "QUESTIONS", "HISTORY", "COMPUTE",
                 "MEMORY", "MODELS", "SYSTEM")
        for cols in (80, 100, 140, 168):
            for view in views:
                for usage_open in (False, True):
                    with self.subTest(cols=cols, view=view, usage_open=usage_open):
                        frame = draw(self.vm, NavState(view=view, usage_open=usage_open),
                                     cols=cols, rows=45)
                        self.assertEqual(len(frame.cells), 45)
                        self.assertTrue(all(len(row) == cols for row in frame.cells))
                        self.assertTrue(all((hit.y == -1 and hit.action[0] == "parent"
                                             and hit.x0 == hit.x1 == 0) or
                                            (0 <= hit.y < 45 and 0 <= hit.x0 < hit.x1 <= cols)
                                            for hit in frame.hits))

    def test_peer_selection_has_session_hits_and_filters_links(self):
        state = NavState(view="PEERS", opened="pfocus:PEERS=a3")
        frame = draw(self.vm, state, cols=140, rows=45)
        selected = [hit for hit in frame.hits if hit.action == ("fold", "pfocus:PEERS", "a3")]
        self.assertTrue(selected)

    def test_main_only_models_are_shown_from_vm_values(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        data = self.vm.to_mapping()
        data["models"] = {"profiles": ("balanced",), "profile_map": {},
                           "main_only": {"z-harness": "private-model-a",
                                         "a-harness": "private-model-b"},
                           "shown": {"private-model-a": "Visible A",
                                     "private-model-b": "Visible B"}}
        frame = draw(ExplorerViewModel.from_mapping(data), NavState(view="MODELS"),
                     cols=140, rows=45)
        rows = frame.text_rows()
        self.assertTrue(any("main session only" in row for row in rows))
        self.assertTrue(any("Visible A" in row for row in rows))
        self.assertTrue(any("Visible B" in row for row in rows))

    def test_compute_process_rows_use_only_vm_process_facts(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel
        data = self.vm.to_mapping()
        data["system"] = {**data["system"],
                           "gpu_processes": ({"host": self.vm.hosts[0]["name"], "gpu": 0,
                                              "session_id": "full-session-id",
                                              "cwd": "/work/project-x",
                                              "command": "python3 -u /opt/run_eval.py --secret arg",
                                              "vram": "12 GB", "age": "4m"},),
                           "gpu_process_summary": {"count": 1, "unregistered": 0},
                           "unregistered_gpu_processes": (("host", 0,
                                                          "python3 -u /opt/run_eval.py --secret arg",
                                                          1, "1m"),)}
        frame = draw(ExplorerViewModel.from_mapping(data), NavState(view="COMPUTE"),
                     cols=140, rows=45)
        rows = frame.text_rows()
        process_row = next(row for row in rows if "full-session-id" in row)
        self.assertIn("project-x/", process_row)
        self.assertIn("run_eval.py", process_row)
        self.assertEqual("\n".join(rows).count("run_eval.py"), 1)

    def test_compute_block_anchors_above_footer_and_tree_scroll_keeps_full_identity_map(self):
        for cols in (80, 100, 140, 168):
            for rows in (24, 40, 60):
                for usage_open in (False, True):
                    with self.subTest(cols=cols, rows=rows, usage_open=usage_open):
                        frame = draw(self.vm, NavState(usage_open=usage_open), cols=cols, rows=rows)
                        self.assertIn("PgUp/Dn", frame.text_rows()[-2])
                        self.assertIn("Home/End", frame.text_rows()[-2])
                        self.assertIn("wheel", frame.text_rows()[-2])
                        footer = rows - 4
                        compute_hit = next(hit for hit in frame.hits
                                           if hit.action == ("root", "COMPUTE"))
                        compute_rows = 2 + min(5, len(self.vm.compute))
                        self.assertEqual(compute_hit.y, footer - compute_rows)
                        compute_hits = [hit for hit in frame.hits
                                        if hit.action == ("root", "COMPUTE")]
                        self.assertEqual(max(hit.y for hit in compute_hits), footer - 1)
                        self.assertTrue(all(hit.y < footer for hit in compute_hits))
                        self.assertTrue(any(hit.action == ("tab", "COMPUTE") for hit in frame.hits))
                        tree = frame.tree_scroll
                        rule = tree[1] < compute_hit.y - 1       # the faint rule row above RESOURCES
                        self.assertEqual(tree[3], compute_hit.y - 1 if rule else compute_hit.y)
                        tree_hits = [hit for hit in frame.hits
                                     if hit.action[:1] in (("scope",), ("route",))]
                        self.assertTrue(all(tree[1] <= hit.y < tree[3] for hit in tree_hits))
                        self.assertTrue(all(0 <= hit.y < rows and 0 <= hit.x0 < hit.x1 <= cols
                                            for hit in frame.hits if hit.y >= 0))

                        if rows >= 40 and tree[5] > 0:
                            state, _ = reduce(
                                NavState(), {"type": "wheel", "direction": -1,
                                             "x": 2, "y": tree[1]}, frame)
                            self.assertEqual(dict(state.scroll)["tree"], 3)
                            bottom = draw(self.vm, NavState(scroll=(("tree", tree[5]),)),
                                          cols=cols, rows=rows)
                            bottom_tree = bottom.tree_scroll
                            bottom_hits = [hit for hit in bottom.hits
                                           if hit.action[:1] in (("scope",), ("route",))]
                            self.assertTrue(bottom_hits)
                            self.assertEqual(max(hit.y for hit in bottom_hits), bottom_tree[3] - 1)

    def test_page_keys_scroll_unclipped_body_rows_and_clamp_resize(self):
        frame = draw(self.vm, NavState(), cols=120, rows=24)
        self.assertTrue(frame.pane_scroll[5] > 0)
        moved, _ = reduce(NavState(), {"type": "key", "key": "pagedown"}, frame)
        self.assertGreater(dict(moved.scroll)["pane"], 0)
        scrolled = draw(self.vm, moved, cols=120, rows=24)
        self.assertGreater(scrolled.pane_scroll[4], 0)
        self.assertTrue(all(hit.y < 20 for hit in scrolled.hits if hit.y >= 0))
        top, _ = reduce(moved, {"type": "key", "key": "home"}, scrolled)
        self.assertEqual(dict(top.scroll)["pane"], 0)
        end_offset = frame.pane_scroll[5]
        bottom, _ = reduce(NavState(), {"type": "key", "key": "end",
                                        "max_scroll": end_offset}, frame)
        self.assertEqual(dict(bottom.scroll)["pane"], end_offset)
        resized = draw(self.vm, bottom, cols=168, rows=60)
        self.assertLessEqual(resized.pane_scroll[4], resized.pane_scroll[5])

    def test_huge_stale_offset_clamps_without_offset_sized_canvas(self):
        from unittest.mock import patch
        import tools.fleet.explorer.frame as frame

        real_grid = frame.CellGrid
        allocated = []

        class ScreenGridGuard(real_grid):
            def __init__(self, cols, rows, *args, **kwargs):
                allocated.append((cols, rows))
                if rows > 24:
                    raise AssertionError(f"dense canvas allocation scales with stale offset: {rows}")
                super().__init__(cols, rows, *args, **kwargs)

        stale = NavState(view="QUESTIONS", scroll=(("pane", 1 << 20),))
        with patch.object(frame, "CellGrid", ScreenGridGuard), patch.object(frame, "_draw_page"):
            rendered = frame.draw(self.vm, stale, cols=120, rows=24)

        self.assertEqual(allocated, [(120, 24)])
        self.assertEqual(rendered.pane_scroll[4], 0)
        self.assertEqual(rendered.pane_scroll[5], 0)
        self.assertFalse(rendered.pane_scroll[7])

    def test_end_measures_finite_questions_past_8192_rows(self):
        questions = tuple({"tag": f"q{index:04d}", "h": "claude", "kind": "wait",
                           "wait": "1m", "project": "p", "title": "q", "now": "n",
                           "pane": "p"} for index in range(8300))
        vm = ExplorerViewModel.from_mapping({"questions": questions})
        state = NavState(view="QUESTIONS")

        maximum = measure_pane_max(vm, state, cols=60, rows=24)
        self.assertGreater(maximum, 8192)

        end = draw(vm, NavState(view="QUESTIONS", scroll=(("pane", maximum),)),
                   cols=60, rows=24)
        self.assertEqual(end.pane_scroll[4], maximum)
        self.assertEqual(end.pane_scroll[5], maximum)
        self.assertFalse(end.pane_scroll[7])
        self.assertIn("q8299", "\n".join(end.text_rows()))
        self.assertTrue(all(0 <= hit.y < 20 for hit in end.hits if hit.y >= 0))

    def test_peers_end_and_stale_restore_reach_fixture_tail_in_wide_and_compact_layouts(self):
        from unittest.mock import patch
        import tools.fleet.explorer.frame as frame

        links = tuple((self.vm.peer_edges or {}).get("links", ()))
        self.assertGreater(len(links), 1)
        tail = links[-1]
        tail_to = tail[1]
        tail_identity = str(tail_to[0]) if isinstance(tail_to, (list, tuple)) else str(tail_to)
        tail_kind, tail_when = str(tail[2]), str(tail[3])

        def assert_tail_text(frame):
            rows = frame.text_rows()
            identity_rows = [index for index, row in enumerate(rows) if tail_identity in row]
            detail_rows = [index for index, row in enumerate(rows)
                           if tail_kind in row and tail_when in row]
            self.assertTrue(identity_rows, "final PEERS link endpoint is absent")
            self.assertTrue(detail_rows, "final PEERS link kind/time text is absent")
            self.assertTrue(any(detail in (identity, identity + 1)
                                for identity in identity_rows for detail in detail_rows),
                            "final endpoint and its link text are not painted together")

        for cols in (120, 100):
            with self.subTest(cols=cols):
                with patch.object(frame, "measure_pane_max", wraps=measure_pane_max) as measured:
                    first = draw(self.vm, NavState(view="PEERS"), cols=cols, rows=24)
                    measured.assert_not_called()

                maximum = measure_pane_max(self.vm, NavState(view="PEERS"), cols, 24)
                end = draw(self.vm, NavState(view="PEERS", scroll=(("pane", maximum),)),
                           cols=cols, rows=24)
                end_text = "\n".join(end.text_rows())
                self.assertEqual(end.pane_scroll[4], maximum)
                self.assertEqual(end.pane_scroll[5], maximum)
                self.assertFalse(end.pane_scroll[7])
                self.assertIn(tail_identity, end_text)
                self.assertIn(tail_kind, end_text)
                self.assertIn(tail_when, end_text)
                assert_tail_text(end)

                restored = draw(self.vm, NavState(view="PEERS", scroll=(("pane", 1 << 20),)),
                                cols=cols, rows=24)
                restored_text = "\n".join(restored.text_rows())
                self.assertEqual(restored.pane_scroll[4], maximum)
                self.assertEqual(restored.pane_scroll[5], maximum)
                self.assertFalse(restored.pane_scroll[7])
                self.assertIn(tail_identity, restored_text)
                self.assertIn(tail_kind, restored_text)
                self.assertIn(tail_when, restored_text)
                assert_tail_text(restored)

        intermediate = draw(self.vm, NavState(view="PEERS", scroll=(("pane", 36),)),
                             cols=120, rows=24)
        intermediate_text = "\n".join(intermediate.text_rows())
        self.assertIn("more link", intermediate_text)
        self.assertNotIn(tail_identity, intermediate_text)
        self.assertEqual(intermediate.pane_scroll[4:6], (36, 37))
        self.assertTrue(intermediate.pane_scroll[7])

    def test_keyboard_session_selection_reveals_its_identity_in_scrolled_tree(self):
        frame = draw(self.vm, NavState(scroll=(("tree", 6),)), cols=120, rows=40)
        order = tuple("session:" + str(item.get("tag")) for item in self.vm.sessions)
        state, _ = reduce(NavState(scroll=(("tree", 6),)),
                          {"type": "key", "key": "down", "order": order}, frame)
        tree = frame.tree_scroll
        indices = tree[8]
        selected_index = indices[state.scope]
        offset = dict(state.scroll).get("tree", tree[4])
        self.assertLessEqual(offset, selected_index)
        self.assertLess(selected_index, offset + tree[7])
        repainted = draw(self.vm, state, cols=120, rows=40)
        self.assertTrue(any(hit.action == ("scope", state.scope.partition(":")[2])
                            for hit in repainted.hits))


if __name__ == "__main__":
    unittest.main()
