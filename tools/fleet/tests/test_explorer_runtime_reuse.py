"""Per-generation reuse for the live explorer's order preparation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.fleet.explorer import live, order
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class ExplorerRuntimeReuseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "fleet2-order.json"
        self.runtime = ExplorerRuntime()
        self.snapshot = {"sessions": [{"session_tag": "tag", "session_id": "sid"}]}
        self.model = ExplorerViewModel(
            repos=({"id": "repo"},),
            sessions=({"tag": "tag", "repo_id": "repo", "harness": "claude"},))
        self.source = self.snapshot["sessions"][0]
        self.identity_patches = (
            patch.object(live, "_resolver", return_value={}),
            patch.object(live, "_session_records",
                         return_value=([], {"tag": {"session": self.source, "sid": "sid"}})),
            patch.object(order, "order_path", return_value=self.path),
        )
        for item in self.identity_patches:
            item.start()
            self.addCleanup(item.stop)
        self.load_patch = patch.object(order, "load_order", wraps=order.load_order)
        self.load_order = self.load_patch.start()
        self.addCleanup(self.load_patch.stop)

    def prepare(self, generation=1, detail=None):
        return self.runtime.prepare(self.snapshot, detail, vm=self.model,
                                    generation=generation, now=1000)[0]

    def test_reuses_identity_and_order_reads_for_same_generation(self):
        with patch.object(live, "_resolver", wraps=live._resolver) as resolver, \
                patch.object(live, "_session_records", wraps=live._session_records) as records:
            first = self.prepare()
            second = self.prepare()
        self.assertEqual(first.ordering, second.ordering)
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(records.call_count, 1)
        self.assertEqual(self.load_order.call_count, 1)

    def test_new_generation_rebuilds_identity_without_rereading_unchanged_order(self):
        with patch.object(live, "_resolver", wraps=live._resolver) as resolver:
            self.prepare(generation=1)
            self.prepare(generation=2)
        self.assertEqual(resolver.call_count, 2)
        self.assertEqual(self.load_order.call_count, 1)

    def test_changed_order_file_is_reloaded(self):
        self.prepare()
        self.path.write_text('{"schema":1,"repos":["repo"],"sessions":{}}',
                             encoding="utf-8")
        model = self.prepare()
        self.assertEqual(model.ordering["repo_keys"], ("repo",))
        self.assertEqual(self.load_order.call_count, 2)

    def test_drag_release_invalidates_cached_order_for_next_prepare(self):
        model = self.prepare()
        self.runtime.apply_effects((("reorder", "repo", "repo", "repo", "repo"),), model)
        self.prepare()
        self.assertEqual(self.load_order.call_count, 2)


if __name__ == "__main__":
    unittest.main()
