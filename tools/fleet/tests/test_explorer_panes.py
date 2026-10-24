"""Exact pane-focus effects against synthetic and live-owned pane graphs."""
import unittest

from tools.fleet.explorer.pane import PaneFocusRunner, pane_for_session


class PaneFocusTests(unittest.TestCase):
    def setUp(self):
        self.graph = [
            {"harness": "claude", "session_id": "sid-a", "pane_id": "workspace:pane-a"},
            {"harness": "codex", "session_id": "sid-a", "pane_id": "workspace:pane-b"},
        ]

    def test_exact_session_edge_selects_only_its_pane(self):
        called = []
        result = PaneFocusRunner(called.append).run(self.graph, "claude", "sid-a")
        self.assertEqual((result.status, result.pane_id), ("focused", "workspace:pane-a"))
        self.assertEqual(called, ["workspace:pane-a"])

    def test_missing_or_ambiguous_edge_never_focuses_a_pane(self):
        called = []
        runner = PaneFocusRunner(called.append)
        self.assertEqual(runner.run(self.graph, "opencode", "sid-a").status, "unavailable")
        ambiguous = self.graph + [dict(self.graph[0], pane_id="workspace:pane-c")]
        self.assertIsNone(pane_for_session(ambiguous, "claude", "sid-a"))
        self.assertEqual(runner.run(ambiguous, "claude", "sid-a").status, "unavailable")
        self.assertEqual(called, [])

    def test_failed_focus_is_reported_and_not_retried(self):
        calls = []

        def reject(pane_id):
            calls.append(pane_id)
            return False

        result = PaneFocusRunner(reject).run(self.graph, "codex", "sid-a")
        self.assertEqual((result.status, result.reason), ("failed", "focus rejected"))
        self.assertEqual(calls, ["workspace:pane-b"])

    def test_callback_error_is_a_bounded_result(self):
        result = PaneFocusRunner(lambda _pane: (_ for _ in ()).throw(OSError())).run(
            self.graph, "claude", "sid-a")
        self.assertEqual((result.status, result.reason), ("failed", "OSError"))


if __name__ == "__main__":
    unittest.main()
