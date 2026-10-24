"""First explorer frame waits briefly for usage, then publishes known or unknown data."""
import unittest
from types import SimpleNamespace

from tools.fleet.collectors import FIRST_FRAME_USAGE_WAIT
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.system import usage_text
from tools.fleet.explorer.view_model import ExplorerViewModel


class UsageFirstFrameTests(unittest.TestCase):
    def setUp(self):
        self.runtime = ExplorerRuntime()
        self.vm = ExplorerViewModel()

    def test_pending_usage_keeps_first_frame_loading(self):
        _, readiness = self.runtime.prepare(
            {"observations": {"usage:claude": {"state": "pending"}}}, None,
            vm=self.vm, generation=0, now=1.0)
        self.assertFalse(readiness["ready"])
        frame = self.runtime.loading_frame(40, 10)
        self.assertIn("불러오는 중", frame.text_rows()[5])

    def test_usage_read_completes_frame_with_the_reading(self):
        _, readiness = self.runtime.prepare(
            {"observations": {"usage:claude": {"state": "ok"}}}, None,
            vm=self.vm, generation=0, now=1.0)
        self.assertTrue(readiness["ready"])
        snapshot = SimpleNamespace(usage_snapshots={"claude": {
            "payload": {"rl_5h": 63}, "freshness": "fresh"}}, sessions=())
        self.assertEqual(usage_text(snapshot), "CL 5h63%")

    def test_wait_is_capped_and_timeout_completes_with_unknown_usage(self):
        self.assertEqual(FIRST_FRAME_USAGE_WAIT, 5.0)
        _, readiness = self.runtime.prepare(
            {"observations": {"usage:claude": {
                "state": "unavailable", "reason": "first-frame-usage-timeout"}}},
            None, vm=self.vm, generation=0, now=6.0)
        self.assertTrue(readiness["ready"])
        snapshot = SimpleNamespace(usage_snapshots={}, sessions=())
        self.assertEqual(usage_text(snapshot), "·")


if __name__ == "__main__":
    unittest.main()
