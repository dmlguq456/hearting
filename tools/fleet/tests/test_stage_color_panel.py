"""A stage's name wears the same color in the shell panel's expanded rows as on Fleet's screen (11-16, spec E9 rule 43)."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest

from tools.fleet import render, render_stages, session_panel
from tools.fleet.palette import style_components

SEQUENCE = [("plan", "done"), ("plan-check", "done"), ("execute", "active"), ("impl-review", "failed"),
            ("test", "pending"), ("report", "attention"), ("close", "recovering")]


def panel_route(workers=()):
    stages = [{"name": name, "st": state} for name, state in SEQUENCE]
    row = {"id": "rt-s", "title": "t", "state": "working", "shape": "staged", "stages": stages,
           "workers_live": [{"stage": name, "marker": "working"} for name in workers]}
    return session_panel.presentation({"route_label": "route 1"}, {"route_panels": {"claude\0s": [row]}}, "claude", "s", 60)["routes"][0]


class StageColorPanelTests(unittest.TestCase):
    def test_the_panels_stage_keys_are_the_keys_fleet_draws_its_route_rail_with(self):
        fleet = [key for text, key in render._route_stage_segs(SEQUENCE, False, None) if text != " › "]
        route = panel_route()
        self.assertEqual([stage["key"] for stage in route["stages"]], fleet)
        self.assertEqual(fleet, ["stg0_off", "stg1_off", "stg2_on", "lvl_r", "stg4_off", "lvl_y", "lvl_y"])

    def test_each_live_workers_stage_takes_the_key_of_that_stage_of_its_route(self):
        route = panel_route(workers=["execute", "plan"])
        self.assertEqual([w["stage_key"] for w in route["workers_live"]], ["stg2_on", "stg0_off"])
        self.assertNotIn("stage_key", panel_route(workers=["unknown-stage"])["workers_live"][0])

    def test_one_table_serves_both_screens(self):
        # The running worker's color index on Fleet's dispatch rows and the route rail's are the same `index % 5`.
        for i in range(12):
            self.assertEqual(render_stages.route_stage_key(i, "active"), "stg%d_on" % (i % 5))
            self.assertEqual(render_stages.route_stage_key(i, "done"), "stg%d_off" % (i % 5))
        self.assertEqual(render_stages.route_stage_key(2, "active", bright=False), "stg2_off")

    def test_the_keys_resolve_to_the_same_style_on_both_screens_and_to_none_without_color(self):
        from utilities import session_shell
        self.assertTrue(all("stg%d_%s" % (i, on) in session_shell.PANEL_STYLE_KEYS for i in range(5) for on in ("on", "off")))
        lit, dimmed = style_components("stg2_on"), style_components("stg2_off")
        self.assertEqual(lit["foreground"], dimmed["foreground"])
        self.assertTrue(lit["bold"] and not lit["dim"] and dimmed["dim"] and not dimmed["bold"])
        self.assertIsNone(style_components("stg2_on", no_color=True)["foreground"])


if __name__ == "__main__":
    unittest.main()
