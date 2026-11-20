"""11-20 a unit route's screen has no intensity (`code(unit·quick)` → `code(unit)`); every other form's line is as it was.

The person: "solo라도 strong은 가능하잖아. unit이어도 마찬가지고. 맞지?" — a unit does one thing and has no procedure, so its
intensity means nothing (`quick` is always the same blank value; only the model depth differs): "unit에는 강도를 표시안하는게 맞겠네".
Only what is drawn changes. The route record's `intensity` / `effective_intensity`, `route_form.FORM_INTENSITIES["unit"]`, the
governor, the budget and the panel payload (`session_panel.route_kind`) keep the value; the tests below pin that too.

  * Fleet: the kind cell (`_kind_cell`, the one cell the table row, the progress cell, the narrow form and the route page's `└ kind`
    read), the timeline popup's route-start row, the older board's chain node knobs and options dial.
  * Every other form (solo, team, direct, frame, an older record) reads exactly as before, at every width.
  * The panel's own drawing is `utilities/session_shell_panel.test.py`'s; the compose card is `utilities/route_unit.test.py`'s.
Everything is synthetic.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fleet import render, session_panel  # noqa: E402
from fleet.explorer import timeline_popup, tables  # noqa: E402
from fleet.explorer.grid import CellGrid  # noqa: E402
from fleet.explorer.view_model import ExplorerViewModel  # noqa: E402


def route(capability="unit", intensity="quick", shape="one-shot-owner", field="code", **extra):
    return {"capability": capability, "intensity": intensity, "shape": shape, "field": field, "worker_unit": "qa/code-review",
            "state": "working", **extra}


def progress(row, width):
    grid = CellGrid(width, 1)
    grid.beat = 1
    tables._draw_route_progress(grid, 0, width, 0, {**row, "stages": [{"name": "unit", "st": "active"}],
                                                    "legs": (("unit", "active", None),)}, card=False)
    return "".join(c[0] for c in grid.cells[0]).rstrip()


class FleetKindCell(unittest.TestCase):
    def test_a_unit_routes_bracket_is_its_form_alone_whatever_intensity_the_record_holds(self):
        for intensity in ("quick", "strong", "standard", "", "thorough"):
            with self.subTest(intensity=intensity):
                row = route(intensity=intensity)
                self.assertEqual(tables._kind_cell(row), [("code", "kind_code"), ("(unit)", "kinds_code")])
                self.assertEqual(tables._kind_cell(row, tail=False), tables._kind_cell(row))      # (no tail to fold)
                for width in (40, 20, 14):
                    self.assertEqual(progress(row, width), "code(unit)")
        self.assertEqual(tables._kind_cell(route(field="plan")), [("plan", "grp"), ("(unit)", "dim")])     # (no field hue: dim)
        # an older unit route (no field) keeps its bare `unit`
        self.assertEqual(tables._kind_cell(route(field="")), [("unit", "grp")])

    def test_every_other_form_reads_exactly_as_before(self):
        cases = [
            (route("code", "quick", "one-shot-owner"), [("code", "kind_code"), ("(solo·quick)", "kindq_code")], "code(solo·quick)"),
            (route("code", "quick", "solo"), [("code", "kind_code"), ("(solo·quick)", "kindq_code")], None),
            (route("code", "standard", "staged"), [("code", "kind_code"), ("(team·standard)", "kinds_code")], None),
            (route("lab", "strong", "staged", field="lab"), [("lab", "kind_lab"), ("(team·strong)", "kindb_lab")], None),
            (route("code", "", "staged"), [("code", "kind_code"), ("(team)", "kinds_code")], None),
            (route("frame", "standard", "framed", field=""), [("frame", "grp"), ("(standard)", "dim")], None),
            (route("code", "direct", "direct", field="code"), [("code", "kind_code")], None),
        ]
        for row, cells, drawn in cases:
            with self.subTest(capability=row["capability"], shape=row["shape"], intensity=row["intensity"]):
                self.assertEqual(tables._kind_cell(row), cells)
                if drawn:
                    self.assertEqual(progress(row, 40), drawn)
        self.assertEqual(progress(route("code", "quick", "one-shot-owner"), 14), "code(solo)")      # (the intensity goes first, as before)

    def test_the_panel_payload_and_the_record_keep_the_intensity(self):
        shown = session_panel.route_kind({"capability": "unit", "worker_unit": "qa/code-review", "field": "code", "intensity": "quick"})
        self.assertEqual((shown["intensity"], shown["kind_form"]), ("quick", "unit"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "route_form_1120", Path(__file__).resolve().parents[3] / "utilities" / "route_form.py")
        form = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(form)
        self.assertEqual(form.FORM_INTENSITIES["unit"], ("quick",))


class OtherFleetSurfaces(unittest.TestCase):
    def story_row(self, capability, intensity):
        row = {"kind": "route", "sub": "start", "at": 1_000_000.0, "text": "review subject", "capability": capability,
               "intensity": intensity, "route_id": "rt-x", "harness": "claude"}
        segments, _meta = timeline_popup.draw_row(row, 100)
        return "".join(text for text, _key in segments).rstrip()

    def test_the_timeline_popups_route_start_row_names_a_unit_without_an_intensity(self):
        self.assertTrue(self.story_row("unit", "quick").endswith("review subject · unit"), self.story_row("unit", "quick"))
        self.assertNotIn("quick", self.story_row("unit", "quick"))
        for capability, intensity in (("code", "quick"), ("code", "standard"), ("lab", "strong")):
            self.assertTrue(self.story_row(capability, intensity).endswith("review subject · %s · %s" % (capability, intensity)),
                            (capability, intensity))
        self.assertTrue(self.story_row("frame", "").endswith("review subject · frame"))

    def test_the_board_chain_node_names_a_unit_without_an_intensity(self):
        from fleet import render_select
        knobs = render_select._route_chain_node_knobs
        self.assertEqual(knobs({"capability": "unit", "intensity": "quick"}, True), "")
        self.assertEqual(knobs({"capability": "unit", "intensity": "quick", "round": 2}, True), "R2")
        for capability in ("autopilot-code", "code"):
            self.assertEqual(knobs({"capability": capability, "intensity": "quick"}, True), render._short_level("quick"))
        self.assertEqual(knobs({"capability": "autopilot-lab", "capability_mode": "dev", "intensity": "strong"}, True),
                         "dev·" + render._short_level("strong"))

    def test_the_boards_options_dial_names_a_unit_legs_without_an_intensity(self):
        def job(owner, intensity):
            return SimpleNamespace(depth=1, capability_owner=owner, intensity=intensity, key="k", slug="s", worker_mode=None, mode=None,
                                   unit=None, route_node=None, assigned_contract=None, work_projection=None, state_evidence=None,
                                   attempt_contract_status=None, profile=None, role=None, route_shape=None, route_id=None)
        with patch.object(render, "_SHOW_ALL", True):
            unit = "".join(text for text, _key in render._opts_segs(job("unit", "quick"))[0])
            code = "".join(text for text, _key in render._opts_segs(job("autopilot-code", "quick"))[0])
        self.assertEqual(unit, "unit")
        self.assertEqual(code, "code(" + render._short_level("quick") + ")")


if __name__ == "__main__":
    unittest.main()
