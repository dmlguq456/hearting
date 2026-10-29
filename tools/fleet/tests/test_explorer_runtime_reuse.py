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
                         return_value=([], {"tag": {"session": self.source, "sid": "sid",
                                                     "ids": ("sid",)}})),
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

    def test_compose_reuses_immutable_vm_until_snapshot_detail_or_generation_changes(self):
        from tools.fleet import render
        from tools.fleet.explorer import live, order
        from tools.fleet.explorer.state import NavState

        runtime = ExplorerRuntime()
        snapshot = {"sessions": [], "resources": [], "jobs": []}
        detail = object()
        compose_globals = ("_EXPLORER_COMPOSE_MODEL", "_EXPLORER_COMPOSE_SNAPSHOT",
                           "_EXPLORER_COMPOSE_DETAIL", "_EXPLORER_COMPOSE_GENERATION")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(order, "order_path", return_value=Path(temp) / "order.json"), \
                patch.object(render, "_explorer_runtime", return_value=runtime), \
                patch.object(render, "_EXPLORER_GENERATION", 10), \
                patch.object(render, "_EXPLORER_STATE", NavState()), \
                patch.object(render, "_EXPLORER_ORDER", []), \
                patch.object(render, "_BLINK_ON", 1), \
                patch.object(render, "_EXPLORER_COMPOSE_MODEL", None), \
                patch.object(render, "_EXPLORER_COMPOSE_SNAPSHOT", None), \
                patch.object(render, "_EXPLORER_COMPOSE_DETAIL", None), \
                patch.object(render, "_EXPLORER_COMPOSE_GENERATION", None), \
                patch.object(live, "build_view_model", wraps=live.build_view_model) as build:
            render._explorer_compose(snapshot, 140, 45, NavState(), detail, now=1000)
            first = render._EXPLORER_MODEL
            render._explorer_compose(snapshot, 140, 45, NavState(), detail, now=1000)
            self.assertIs(render._EXPLORER_MODEL, first)
            self.assertEqual(build.call_count, 1)

            render._explorer_compose(snapshot, 140, 45, NavState(), object(), now=1000)
            self.assertEqual(build.call_count, 2)
            render._EXPLORER_GENERATION = 11
            render._explorer_compose(snapshot, 140, 45, NavState(), render._EXPLORER_RESULT, now=1000)
            self.assertEqual(build.call_count, 3)

    def test_persistence_deduplicates_before_serializing_and_retries_failed_write(self):
        from tools.fleet.explorer.view_model import ExplorerViewModel

        readiness = {"ready": True, "fully_fresh": True, "all_new": True,
                     "observed_at": 1000.0, "generation": 1, "generation_identity": "g1"}
        vm = ExplorerViewModel.from_mapping({"repos": [], "sessions": []})
        with patch.object(self.runtime, "_write_cache", return_value=True) as write, \
                patch.object(self.runtime, "_cache_envelope", wraps=self.runtime._cache_envelope) as envelope:
            first = self.runtime.persist_complete_async(vm, readiness)
            first.join(2)
            self.assertIsNone(self.runtime.persist_complete_async(vm, readiness))
            self.assertEqual(envelope.call_count, 1)
            self.assertEqual(write.call_count, 1)

        retry_runtime = ExplorerRuntime(self.path.parent)
        retry_runtime.configure_persistence({"codex"})
        with patch.object(retry_runtime, "_write_cache", side_effect=(False, True)) as write:
            failed = retry_runtime.persist_complete_async(vm, readiness)
            failed.join(2)
            recovered = retry_runtime.persist_complete_async(vm, readiness)
            self.assertIsNotNone(recovered)
            recovered.join(2)
            self.assertEqual(write.call_count, 2)

    def test_candidate_composition_does_not_adopt_input_state(self):
        from tools.fleet import render
        from tools.fleet.explorer.state import NavState
        state = NavState(drag=("tree", "session", "painted", "painted", 2, 15, None, False, False),
                         scroll=(("pane", 100),))
        with patch.object(render, "_explorer_runtime", return_value=ExplorerRuntime()), \
                patch.object(render, "_EXPLORER_COMPOSE_MODEL", None), \
                patch.object(render, "_EXPLORER_STATE", state), \
                patch.object(render, "_EXPLORER_ORDER", ["painted-order"]):
            render._explorer_compose({"sessions": [], "jobs": []}, 140, 45, state)
            self.assertEqual(render._EXPLORER_STATE, state)
            self.assertEqual(render._EXPLORER_ORDER, ["painted-order"])

    def test_orphan_motion_and_release_on_unpreviewed_target_are_inert(self):
        from tools.fleet.explorer.frame import CellFrame, Hit
        from tools.fleet.explorer.state import NavState, reduce
        frame = CellFrame((), (Hit(4, 0, 32, ("scope", "repo:a")),
                               Hit(5, 0, 32, ("scope", "repo:b")),
                               Hit(6, 0, 32, ("scope", "repo:c"))), 40, 20)
        orphan, effects = reduce(NavState(), {"type": "mouse", "phase": "press", "motion": True,
                                              "x": 2, "y": 4}, frame)
        self.assertEqual((orphan.drag, effects), ((), ()))
        held, _ = reduce(NavState(), {"type": "mouse", "phase": "press", "x": 2, "y": 4}, frame)
        moved, _ = reduce(held, {"type": "mouse", "phase": "press", "motion": True, "x": 2, "y": 5}, frame)
        ended, effects = reduce(moved, {"type": "mouse", "phase": "release", "x": 2, "y": 6}, frame)
        self.assertEqual((ended.drag, effects), ((), ()))

    def test_painted_process_replacement_cancels_a_pending_gesture_on_the_same_tag(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from tools.fleet import render
        from tools.fleet.explorer.state import NavState
        old = ExplorerViewModel(source_records={"_detail_tags": {"claude:full-sid": "tag"},
            "_detail_bindings": {"claude:full-sid": ("claude", "full-sid", "17", "old-start")}},
            ordering={"repo_by_tag": {"tag": "repo"}, "tag_keys": {"tag": "sid:claude:full-sid"}})
        state = NavState(drag=("tree", "session", "tag", "tag", 2, 15, None, False, False,
                               render._explorer_gesture_binding(old, "tag")))
        current = replace(old, source_records={"_detail_tags": {"claude:full-sid": "tag"},
            "_detail_bindings": {"claude:full-sid": ("claude", "full-sid", "17", "new-start")}})
        frame = ExplorerRuntime().loading_frame(140, 45)
        with patch.object(render, "_EXPLORER_MODEL", current), \
                patch.object(render, "_EXPLORER_STATE", state), \
                patch.object(render, "_EXPLORER_SIZE", None), \
                patch.object(render, "_EXPLORER_ORDER", []), \
                patch.object(render, "_explorer_frame_from_model", return_value=frame), \
                patch.object(render, "_draw_explorer_frame"):
            render._draw_explorer(SimpleNamespace(getmaxyx=lambda: (45, 140)), {}, frame=frame)
            self.assertEqual(render._EXPLORER_STATE.drag, ())


if __name__ == "__main__":
    unittest.main()
