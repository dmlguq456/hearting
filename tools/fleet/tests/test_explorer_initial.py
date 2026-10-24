"""Initial explorer collection publishes a complete frame or explicit loading state."""
import unittest

from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class InitialReadinessTests(unittest.TestCase):
    def test_first_generation_disables_fast_first_and_later_generations_keep_it(self):
        runtime = ExplorerRuntime()
        self.assertEqual(runtime.collection_options(first_complete=True), {"fast_first": False})
        self.assertEqual(runtime.collection_options(first_complete=False), {"fast_first": True})

    def test_only_explicit_pending_sources_delay_readiness_and_model_is_not_rebuilt(self):
        runtime = ExplorerRuntime()
        vm = ExplorerViewModel.from_mapping({"identity": "same"})
        pending = {"observations": {"usage": {"state": "pending"},
                                     "models": {"state": "unavailable"}}}
        prepared, readiness = runtime.prepare(pending, None, vm=vm, generation=0, now=1.0)
        # The model is the one already built (never rebuilt); prepare may only add its ordering record.
        self.assertEqual({key: value for key, value in prepared.to_mapping().items() if key != "ordering"},
                         {key: value for key, value in vm.to_mapping().items() if key != "ordering"})
        self.assertEqual(readiness["pending"], ("usage",))
        self.assertEqual(readiness["sources"]["models"]["state"], "unavailable")
        self.assertFalse(readiness["ready"])
        frame = runtime.loading_frame(40, 10)
        self.assertEqual((frame.cols, frame.rows), (40, 10))
        self.assertIn("불러오는 중", frame.text_rows()[5])
        _, completed = runtime.prepare({"observations": {"usage": {"state": "unavailable"}}}, None,
                                       vm=vm, generation=1, now=2.0)
        self.assertTrue(completed["ready"])
