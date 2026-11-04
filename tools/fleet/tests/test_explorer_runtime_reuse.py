"""Per-generation reuse for the live explorer's order preparation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.fleet.explorer import live, order
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class ExplorerRuntimeReuseTests(unittest.TestCase):
    def test_model_updates_freeze_changed_data_and_share_unchanged_fields(self):
        from dataclasses import replace
        from dataclasses import FrozenInstanceError

        model = ExplorerViewModel(sessions=({"tag": "a", "nested": [1]},),
                                  source_records={"private": {"rows": [1, 2, 3]}})
        rows = [{"tag": "b", "nested": [2]}]
        updated = model.with_updates(sessions=rows, clock=1000)
        self.assertEqual(updated, replace(model, sessions=rows, clock=1000))
        self.assertIs(updated.source_records, model.source_records)
        self.assertIs(updated.routes, model.routes)
        rows[0]["nested"].append(3)
        self.assertEqual(updated.sessions[0]["nested"], (2,))
        with self.assertRaises(TypeError):
            updated.sessions[0]["tag"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            updated.clock = 2000
        with self.assertRaises(TypeError):
            model.with_updates(unknown=1)

    def test_thousands_of_completed_saves_do_not_retain_thread_or_generation_history(self):
        import weakref

        retained = []

        class CompletedThread:
            def __init__(self, *, target, args, **_kwargs):
                self.target, self.args, self.ident = target, args, None
                retained.append(weakref.ref(self))

            def start(self):
                self.ident = 1
                self.target(*self.args)
                self.target = self.args = None

            def is_alive(self):
                return False

            def join(self, _timeout):
                pass

        for written in (True, False):
            runtime = ExplorerRuntime()
            model = ExplorerViewModel()
            readiness = {"ready": True, "fully_fresh": True, "all_new": True}
            with patch("tools.fleet.explorer.runtime.threading.Thread", CompletedThread), \
                    patch.object(runtime, "_cache_envelope", return_value={}), \
                    patch.object(runtime, "_write_cache", return_value=written):
                for generation in range(5000):
                    current = {**readiness, "generation": generation, "observed_at": 1000 + generation}
                    runtime.persist_complete_async(model, current)
                    if generation == 100:
                        warm_history = len(runtime._persisted_generations) + len(runtime._failed_generations)
                self.assertIsNone(runtime.persist_complete_async(model, current))
            self.assertEqual(len(runtime._persisted_generations) + len(runtime._failed_generations), warm_history)
            self.assertLessEqual(sum(ref() is not None for ref in retained), 1)
            self.assertEqual(len(runtime._pending_generations), 0)

    def test_unchanged_live_usage_does_not_copy_the_whole_immutable_model(self):
        row = {"harness": "claude", "text": "claude", "windows": (("week", 40, None),)}
        model = ExplorerViewModel(usage=(row,))
        with patch.object(live, "_usage_records", return_value=[dict(row)]):
            self.assertIs(live.current_claude_usage(model, {}, 1000), model)
        changed = {**row, "windows": (("week", 50, None),)}
        with patch.object(live, "_usage_records", return_value=[changed]):
            updated = live.current_claude_usage(model, {}, 1000)
        self.assertIsNot(updated, model)
        self.assertEqual(updated.usage[0]["windows"][0][1], 50)

    def test_idle_frames_reuse_cells_without_freezing_the_clock_or_input(self):
        from dataclasses import replace
        from tools.fleet.explorer import frame
        from tools.fleet.explorer.state import NavState

        runtime = ExplorerRuntime()
        model = ExplorerViewModel(clock=1000, repos=({"id": "repo", "name": "repo"},))
        state = NavState()
        first = runtime.draw_frame(model, state, 140, 45)
        with patch.object(frame, "draw", wraps=frame.draw) as draw:
            # A new VM can carry the same visible data while a collector runs.
            same = replace(model, clock=1000.5)
            self.assertIs(runtime.draw_frame(same, state, 140, 45), first)
            self.assertEqual(draw.call_count, 0)
            cases = (
                (replace(model, clock=1060), state, 140, 45, 1),
                (model, replace(state, view="ROUTES"), 140, 45, 1),
                (model, replace(state, scroll=(("pane", 3),)), 140, 45, 1),
                (model, state, 80, 24, 1),
                (model, state, 140, 45, 0),
                (replace(model, hearting={"version": "new"}), state, 140, 45, 1),
                (replace(model, repos=({"id": "changed"},)), state, 140, 45, 1),
            )
            for vm, nav, cols, rows, beat in cases:
                with self.subTest(view=nav.view, cols=cols, beat=beat):
                    before = draw.call_count
                    actual = runtime.draw_frame(vm, nav, cols, rows, beat=beat)
                    self.assertEqual(draw.call_count, before + 1)
                    self.assertEqual(actual, frame.draw(vm, nav, cols, rows, beat=beat))

    def test_question_edits_invalidate_a_frame_even_when_nav_states_compare_equal(self):
        from dataclasses import replace
        from tools.fleet.explorer import frame
        from tools.fleet.explorer.state import NavState

        runtime = ExplorerRuntime()
        model = ExplorerViewModel()
        state = NavState(view="QUESTIONS", question_overlay={"state": "opening"})
        runtime.draw_frame(model, state, 140, 45)
        edited = replace(state, question_overlay={"state": "failed", "error": "offline"})
        self.assertEqual(state, edited)  # the editor is excluded from NavState equality
        with patch.object(frame, "draw", wraps=frame.draw) as draw:
            actual = runtime.draw_frame(model, edited, 140, 45)
            self.assertEqual(draw.call_count, 1)
            self.assertEqual(actual, frame.draw(model, edited, 140, 45))
            edited.question_overlay["error"] = "retry"
            before = draw.call_count
            runtime.draw_frame(model, edited, 140, 45)
            self.assertEqual(draw.call_count, before + 1)

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

    def test_persistence_deduplicates_failures_and_retries_the_next_generation(self):
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
            for _ in range(100):
                self.assertIsNone(retry_runtime.persist_complete_async(vm, readiness))
            recovered = retry_runtime.persist_complete_async(vm, {
                **readiness, "generation": 2, "generation_identity": "g2", "observed_at": 1001.0})
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
