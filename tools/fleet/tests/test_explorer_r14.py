"""Focused renderer regressions for the r14 frame and dynamic data layouts."""
import unittest
from pathlib import Path

from tools.fleet.explorer.frame import draw
from tools.fleet.explorer.state import NavState
from tools.fleet.explorer.view_model import load_fixture


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


if __name__ == "__main__":
    unittest.main()
