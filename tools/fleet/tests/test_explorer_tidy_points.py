"""The explorer's model view reads its tidy thresholds from the shared source."""

import unittest
from unittest import mock

from tools.fleet.explorer import focus


class TidyPoints(unittest.TestCase):
    def test_reads_points_from_context_alert_source(self):
        source = focus.utility("context_alert")
        self.assertEqual(focus._read_tidy_points(), source.THRESHOLDS)

    def test_missing_source_keeps_points_unknown(self):
        with mock.patch.object(focus, "utility", side_effect=ImportError("source unavailable")):
            self.assertIsNone(focus._read_tidy_points())

    def test_malformed_source_keeps_points_unknown(self):
        with mock.patch.object(focus, "utility", return_value=type("Source", (), {"THRESHOLDS": (1, True)})):
            self.assertIsNone(focus._read_tidy_points())

    def test_route_carries_named_worker_step_and_ordered_stages(self):
        from tools.fleet.explorer import live

        node = {"id": "execute", "state": "active", "harness": "codex", "unit": "dev/backend"}
        collected = {"route-x": {"view": {"nodes": [
            {"id": "plan", "state": "done"}, node], "slug": "route-slug"},
            "record": {"slug": "route-slug"}, "jobs": [], "sessions": []}}
        with mock.patch.object(live, "_collect_routes", return_value=collected):
            routes, _nodes = live._route_records({}, [], {}, lambda _cwd: None, 10)

        self.assertEqual([stage["name"] for stage in routes[0]["stages"]], ["plan", "execute"])
        self.assertEqual(routes[0]["worker_stage"], "execute")
        self.assertIsNone(routes[0]["worker_step_key"])


if __name__ == "__main__":
    unittest.main()
