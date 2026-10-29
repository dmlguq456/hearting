"""Initial explorer collection publishes a complete frame or explicit loading state."""
import unittest

from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.view_model import ExplorerViewModel


class InitialReadinessTests(unittest.TestCase):
    def test_basic_observation_stays_fast_after_and_before_first_complete(self):
        runtime = ExplorerRuntime()
        self.assertEqual(runtime.collection_options(first_complete=True),
                         {"fast_first": True, "include_report_verification": False})
        self.assertEqual(runtime.collection_options(first_complete=False),
                         {"fast_first": True, "include_report_verification": False})

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

    def test_production_requires_basic_detail_usage_and_attempted_remote_observations(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from tools.fleet.explorer import order

        with tempfile.TemporaryDirectory() as temp:
            runtime = ExplorerRuntime()
            runtime.configure_persistence({"codex"}, strict_readiness=True)
            observations = {
                "basic": {"state": "complete", "observed_at": 10,
                          "generation_identity": "g-1"},
                "required-details": {"state": "pending"},
                "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
                "usage:codex": {"state": "complete", "observed_at": 10},
                "remote-host": {"state": "pending", "attempted": False},
            }
            snapshot = {"observations": observations}
            vm = ExplorerViewModel.from_mapping({})
            with patch.object(order, "order_path", return_value=Path(temp) / "order.json"):
                _model, pending = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
                self.assertFalse(pending["ready"])
                self.assertEqual(pending["pending"], ("remote-host", "required-details"))
                observations["required-details"] = {
                    "state": "complete", "generation_identity": "g-1", "all_new": False}
                observations["remote-host"] = {"state": "failed", "attempted": True}
                _model, continued = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
                observations["required-details"] = {
                    "state": "complete", "generation_identity": "other", "all_new": True}
                _model, mismatched = runtime.prepare(snapshot, None, vm=vm, generation=1, now=10)
            self.assertTrue(continued["ready"])
            self.assertFalse(continued["all_new"])
            self.assertFalse(mismatched["ready"])
            self.assertIn("required-details", mismatched["pending"])

    def test_strict_readiness_does_not_treat_missing_required_sources_as_complete(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from tools.fleet.explorer import order

        runtime = ExplorerRuntime(strict_readiness=True)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(order, "order_path", return_value=Path(temp) / "order.json"):
            _model, readiness = runtime.prepare({"observations": {}}, None,
                                                vm=ExplorerViewModel.from_mapping({}),
                                                generation=0, now=1)
        self.assertFalse(readiness["ready"])
        self.assertEqual(set(readiness["pending"]),
                         {"basic", "required-details", "usage:claude", "usage:codex",
                          "usage:opencode", "resource-runs", "remote-host"})


class DetailCallbackSelection(unittest.TestCase):
    def test_required_callback_precedes_legacy_callback_and_optional_uses_legacy(self):
        from types import SimpleNamespace
        from tools.fleet import render

        calls = []
        collector = SimpleNamespace(
            required_detail_refresh=lambda source: calls.append(("required", source)),
            detail_refresh=lambda source: calls.append(("full", source)),
        )
        required, is_required = render._explorer_detail_callback(collector)
        optional, optional_is_required = render._explorer_detail_callback(
            collector, optional=True)

        self.assertTrue(is_required)
        self.assertFalse(optional_is_required)
        required("basic")
        optional("complete")
        self.assertEqual(calls, [("required", "basic"), ("full", "complete")])

    def test_selected_peers_and_memory_scopes_request_optional_refresh_without_reselecting_base(self):
        from tools.fleet import render
        from tools.fleet.explorer.state import NavState

        overview = render._explorer_optional_detail_key("generation-1", NavState())
        peers = render._explorer_optional_detail_key(
            "generation-1", NavState(view="PEERS", scope="repo:repo-a"))
        memory = render._explorer_optional_detail_key(
            "generation-1", NavState(view="MEMORY", scope="repo:repo-a"))
        other_repo = render._explorer_optional_detail_key(
            "generation-1", NavState(view="MEMORY", scope="repo:repo-b"))
        self.assertEqual(overview, peers)  # PEERS uses the existing list selection worker.
        self.assertNotEqual(peers, memory)
        self.assertNotEqual(memory, other_repo)
        self.assertEqual(peers, render._explorer_optional_detail_key(
            "generation-1", NavState(view="PEERS", scope="repo:repo-a")))


class _Screen:
    def __init__(self, rows=45, cols=140):
        self.size, self.cells = (rows, cols), {}

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.cells = {}

    def addstr(self, y, x, text, *_attr):
        self.cells[(y, x)] = text

    def refresh(self):
        pass

    def text(self):
        return "".join(self.cells[key] for key in sorted(self.cells))


class LoadingGate(unittest.TestCase):
    """The full-screen loading frame stands only before the first complete frame."""

    def setUp(self):
        from tools.fleet import render
        from tools.fleet.tests import explorer_fixture as ef
        self.render = render
        self.snapshot, hosts = ef.demo_snapshot()
        self.snapshot.compute_hosts = hosts
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", render._EXPLORER_MODEL)
        self.addCleanup(setattr, render, "_EXPLORER_READINESS", render._EXPLORER_READINESS)
        render.set_explorer_mode(True)
        self.addCleanup(render.set_explorer_mode, False)
        self.addCleanup(setattr, render, "_EXPLORER_SHOWN", False)
        self.addCleanup(setattr, render, "_EXPLORER_PUBLISHED_MODEL", None)
        self.addCleanup(setattr, render, "_EXPLORER_REPLAY_ACTIVE", False)
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", None)
        render._EXPLORER_SHOWN = False
        render._EXPLORER_PUBLISHED_MODEL = None
        render._EXPLORER_MODEL = None
        render._EXPLORER_REPLAY_ACTIVE = False

    def draw(self, *, loading, pending=False):
        self.snapshot.observations = {"usage": {"state": "pending"}} if pending else {}
        screen = _Screen()
        self.render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0,
                          loading=loading, snapshot=self.snapshot)
        return screen.text()

    def test_no_collection_yet_is_loading_not_an_empty_fleet(self):
        self.assertIn("불러오는 중", self.draw(loading=True))

    def test_a_pending_source_after_the_first_frame_keeps_the_frame(self):
        self.assertIn("불러오는 중", self.draw(loading=False, pending=True))
        self.assertNotIn("불러오는 중", self.draw(loading=False))
        self.assertNotIn("불러오는 중", self.draw(loading=False, pending=True))

    def test_cached_frame_skips_live_composition_before_first_collection(self):
        from unittest.mock import patch
        from tools.fleet.explorer.view_model import ExplorerViewModel

        cached = ExplorerViewModel.from_mapping({"repos": [{"id": "cached", "name": "cached"}]})
        self.render._EXPLORER_PUBLISHED_MODEL = cached
        self.render._EXPLORER_MODEL = cached
        self.render._EXPLORER_REPLAY_ACTIVE = True
        self.render._EXPLORER_READINESS = {"ready": False, "pending": ("basic",)}
        self.render._EXPLORER_STATE = None
        screen = _Screen()
        with patch.object(self.render, "_explorer_compose",
                          side_effect=AssertionError("replay must not compose live data")):
            self.render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0,
                              loading=True, snapshot=self.snapshot)
        self.assertNotIn("불러오는 중", screen.text())
        self.assertIsNotNone(self.render._EXPLORER_STATE)
        self.assertEqual(self.render._EXPLORER_STATE.view, "OVERVIEW")

    def test_working_marks_turn_at_draw_time_and_the_frame_keeps_the_still_glyph(self):
        from unittest import mock
        from tools.fleet.explorer.runtime import ExplorerRuntime
        frame = ExplorerRuntime().loading_frame(40, 10)
        self.assertIn("⠏", frame.text_rows()[5])
        glyphs = set()
        for tick in range(10):
            screen = _Screen(10, 40)
            with mock.patch.object(self.render.time, "time", return_value=tick / 10):
                self.render._draw_explorer_frame(screen, frame, self.snapshot)
            glyphs.update(ch for ch in screen.text() if "⠀" <= ch <= "⣿")
        self.assertEqual(len(glyphs), 10)

    def test_selected_models_publish_over_complete_base_while_next_base_is_pending(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.fleet import render
        from tools.fleet.explorer.state import NavState
        from tools.fleet.explorer.view_model import ExplorerViewModel

        base = ExplorerViewModel.from_mapping({"models": {"profiles": ("old",)}})
        selected = SimpleNamespace(config={"profiles": ("new",)})
        published = []
        screen = _Screen()
        patches = (
            mock.patch.object(render, "_EXPLORER_ENABLED", True),
            mock.patch.object(render, "_EXPLORER_STATE", NavState(view="MODELS")),
            mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", base),
            mock.patch.object(render, "_EXPLORER_MODEL", base),
            mock.patch.object(render, "_EXPLORER_RESULT", selected),
            mock.patch.object(render, "_EXPLORER_READINESS", {"ready": False, "pending": ("basic",)}),
            mock.patch.object(render, "_explorer_runtime", return_value=SimpleNamespace(
                publication_ready=lambda _readiness: False)),
            mock.patch.object(render, "_explorer_compose",
                              return_value=None),
            mock.patch.object(render, "_explorer_frame_from_model",
                              side_effect=lambda model, *_args, **_kwargs: model),
            mock.patch.object(render, "_draw_explorer",
                              side_effect=lambda _screen, _snapshot, frame=None: published.append(frame)),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8], patches[9]:
            render._draw(screen, [], [], "all", 0, loading=False,
                         snapshot={"observations": {"basic": {"state": "pending"}}})

        self.assertEqual(published[0].models["profiles"], ("new",))
        self.assertEqual(base.models["profiles"], ("old",))


class SelectedDetailPublication(unittest.TestCase):
    def setUp(self):
        from tools.fleet import render
        from tools.fleet.explorer import live
        from tools.fleet.explorer.runtime import ExplorerRuntime
        from tools.fleet.explorer.state import NavState
        from tools.fleet.tests.explorer_fixture import demo_snapshot
        from unittest import mock
        self.render, self.live = render, live
        self.snapshot, self.snapshot_hosts = demo_snapshot()
        self.snapshot.compute_hosts = self.snapshot_hosts
        for session in self.snapshot.sessions:
            session.proc_start = "test-process-start"
        self.session = next(s for s in self.snapshot.sessions if s.session_id == "demo-claude-1")
        self.runtime = ExplorerRuntime()
        self.screen = _Screen()
        for name, value in {"_EXPLORER_ENABLED": True, "_EXPLORER_STATE": NavState(view="HISTORY"),
                            "_EXPLORER_MODEL": None, "_EXPLORER_PUBLISHED_MODEL": None,
                            "_EXPLORER_RESULT": None, "_EXPLORER_REPLAY_ACTIVE": False,
                            "_EXPLORER_ORDER": [], "_EXPLORER_SIZE": None,
                            "_EXPLORER_COMPOSE_MODEL": None, "_EXPLORER_GENERATION": 1}.items():
            patch = mock.patch.object(render, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(render, "_explorer_runtime", return_value=self.runtime)
        patch.start()
        self.addCleanup(patch.stop)
        self.draw()
        self.base = render._EXPLORER_PUBLISHED_MODEL
        self.assertIsNotNone(self.base)

    def draw(self):
        self.render._draw(self.screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0,
                          snapshot=self.snapshot)
        return self.render._EXPLORER_MODEL

    def detail(self, entries=(), **records):
        from tools.fleet.explorer.types import ExplorerDetail, FocusKey, FocusRequest, FocusResult, Health
        key = FocusKey("claude", "demo-claude-1")
        request = FocusRequest(key, 1, 1, ("claude:demo-claude-1",), ("seat", "cards", "dialogue:last"),
                               tuple(self.live.detail_bindings(self.snapshot).items()))
        return ExplorerDetail(focus=FocusResult(key, 1, 1, health=Health("success"), request=request,
                                               source_records={"seat": entries, **records}))

    def test_selected_history_paints_while_remote_is_pending_without_publishing_partial_base(self):
        from unittest import mock
        entry = {"harness": "claude", "sid": "demo-claude-1", "current": True,
                 "cards": [{"generation": 1, "status": "available", "title": "selected-history-ready",
                            "lines": ["private card body"]}]}
        self.snapshot.observations = {"remote-host": {"state": "pending"}}
        self.render._EXPLORER_RESULT = self.detail((entry,))
        with mock.patch.object(self.runtime, "persist_complete_async") as persist, \
                mock.patch.object(self.runtime, "mark_published") as published:
            shown = self.draw()
        self.assertEqual(shown.cards["a7"]["cards"][0]["lines"], ("private card body",))
        self.assertEqual(shown.seats[0][5], "selected-history-ready")
        self.assertIs(self.render._EXPLORER_PUBLISHED_MODEL, self.base)
        self.assertEqual(shown.ordering, self.base.ordering)
        self.assertEqual([row["tag"] for row in shown.sessions], [row["tag"] for row in self.base.sessions])
        persist.assert_not_called()
        published.assert_not_called()
        from tools.fleet.explorer.view_model import persistable_view_model
        self.assertNotIn("private card body", repr(persistable_view_model(shown)))

    def test_models_overlay_does_not_starve_a_new_complete_base(self):
        from tools.fleet.explorer.types import ExplorerDetail
        self.render._EXPLORER_RESULT = ExplorerDetail(config={"profiles": ["new"]})
        self.snapshot.observations = {"remote-host": {"state": "pending"}}
        self.assertEqual(self.draw().models["profiles"], ("new",))
        self.snapshot.observations = {}
        self.session.title = "a newly completed base"
        self.render._EXPLORER_GENERATION += 1
        shown = self.draw()
        self.assertIsNot(self.render._EXPLORER_PUBLISHED_MODEL, self.base)
        self.assertIn("a newly completed base", repr(shown.sessions))
        self.assertEqual(shown.models["profiles"], ("new",))

    def test_snapshot_peers_paint_over_pending_base_and_empty_is_observed(self):
        from unittest.mock import patch
        from tools.fleet import details
        from tools.fleet.explorer.state import NavState
        from tools.fleet.collectors import peer_messages
        from tools.fleet.explorer.types import ExplorerDetail, FocusKey, FocusRequest, FocusResult
        self.render._EXPLORER_STATE = NavState(view="PEERS")
        self.snapshot.observations = {"remote-host": {"state": "pending"}}
        self.render._EXPLORER_COMPOSE_MODEL = None
        self.session.peer_last_sent = {"to_session_id": "demo-codex-1", "to_harness": "codex",
                                      "kind": "selected-peer-ready", "age_min": 1}
        self.snapshot.observations["optional:peers"] = {"state": "success", "observed_at": 10}
        shown = self.draw()
        self.assertIn("selected-peer-ready", repr(shown.peer_edges))
        self.assertIs(self.render._EXPLORER_PUBLISHED_MODEL, self.base)
        self.assertEqual(shown.sessions, self.base.sessions)
        key = FocusKey("*", "peers")
        request = FocusRequest(key, 2, 1, (), (), tuple(self.live.detail_bindings(self.snapshot).items()))
        with patch.object(peer_messages, "collect", return_value=(
                {"records": [], "by_session": {}}, {"state": "success"})):
            empty = details.observe_peers(self.snapshot)
        self.render._EXPLORER_RESULT = ExplorerDetail(results={"seats": FocusResult(
            key, 2, 1, source_records={"peers": empty}, request=request)})
        self.render._EXPLORER_COMPOSE_MODEL = None
        shown = self.draw()
        self.assertNotIn("selected-peer-ready", repr(shown.peer_edges))
        self.assertEqual(shown.system["peer_observation"]["state"], "success")
        # The independent read did not mutate its input or create live control targets.
        self.assertEqual(self.session.peer_last_sent["kind"], "selected-peer-ready")
        self.assertEqual(shown.ordering, self.base.ordering)

    def test_peer_graph_rejects_changed_binding_replay_and_older_observation(self):
        from dataclasses import replace
        self.session.peer_last_sent = {"to_session_id": "demo-codex-1", "to_harness": "codex",
                                      "kind": "must-not-cross", "age_min": 1}
        self.snapshot.observations = {"optional:peers": {"state": "success", "observed_at": 10}}
        candidate = self.live.build_view_model(self.snapshot)
        self.assertIn("must-not-cross", repr(candidate.peer_edges))
        for base in (replace(self.base, source_records=None),
                     replace(self.base, source_records={**self.base.source_records,
                             "_detail_bindings": {"other": ("new-start",)}}),
                     replace(self.base, source_records={**self.base.source_records,
                             "_peer_observed_at": 11})):
            shown = self.live.selected_detail(base, None, peer_model=candidate)
            self.assertEqual(shown.peer_edges, base.peer_edges)
        relocated = replace(candidate, sessions=tuple(dict(row, repo="other", repo_id="other")
                                                       for row in candidate.sessions))
        self.assertEqual(self.live.selected_detail(self.base, None, peer_model=relocated).peer_edges,
                         self.base.peer_edges)
        for state in ("failed", "unknown"):
            failed = replace(candidate, system={**candidate.system, "peer_observation": {"state": state}})
            shown = self.live.selected_detail(self.base, None, peer_model=failed)
            self.assertEqual(shown.peer_edges, self.base.peer_edges)
            self.assertEqual(shown.system["peer_observation"]["state"], state)

    def test_new_process_same_sid_and_replay_do_not_borrow_selected_detail(self):
        from dataclasses import replace
        detail = self.detail(dialogue={"index": 0, "harness": "claude", "sid": "demo-claude-1",
                                       "lines": ["must not cross process incarnations"]})
        self.session.proc_start = "replacement-start"
        replacement = self.live.build_view_model(self.snapshot)
        self.assertNotIn("must not cross", repr(self.live.selected_detail(replacement, detail).dialogue))
        replay = replace(self.base, source_records=None)
        self.assertNotIn("must not cross", repr(self.live.selected_detail(replay, detail).dialogue))

    def test_successful_empty_scope_clears_previous_cards_and_dialogue(self):
        entry = {"harness": "claude", "sid": "demo-claude-1", "current": True,
                 "cards": [{"generation": 1, "status": "available", "title": "old",
                            "lines": ["old-body"]}]}
        populated = self.live.selected_detail(self.base, self.detail((entry,), dialogue={
            "index": 0, "harness": "claude", "sid": "demo-claude-1", "lines": ["old dialogue"]}))
        cleared = self.live.selected_detail(populated, self.detail())
        self.assertNotIn("old-body", repr(cleared.cards))
        self.assertNotIn("old dialogue", repr(cleared.dialogue))

    def test_unobserved_dialogue_preserves_same_binding_last_good(self):
        entry = {"harness": "claude", "sid": "demo-claude-1", "current": True, "cards": []}
        populated = self.live.selected_detail(self.base, self.detail((entry,), dialogue={
            "index": 0, "harness": "claude", "sid": "demo-claude-1", "lines": ["last good dialogue"]}))
        refreshed = self.live.selected_detail(populated, self.detail((entry,)))
        self.assertIn("last good dialogue", repr(refreshed.dialogue))
        empty = self.live.selected_detail(refreshed, self.detail((entry,), dialogue={
            "index": 0, "harness": "claude", "sid": "demo-claude-1", "lines": []}))
        self.assertNotIn("last good dialogue", repr(empty.dialogue))

    def test_replay_allows_local_config_without_guessing_selected_session_identity(self):
        from dataclasses import replace
        self.render._EXPLORER_PUBLISHED_MODEL = replace(self.base, source_records=None)
        self.render._EXPLORER_REPLAY_ACTIVE = True
        self.snapshot.observations = {"remote-host": {"state": "pending"}}
        detail = self.detail(dialogue={"index": 0, "harness": "claude", "sid": "demo-claude-1",
                                       "lines": ["private unbound dialogue"]})
        self.render._EXPLORER_RESULT = replace(detail, config={"profiles": ["replay-local-config"]})
        shown = self.draw()
        self.assertEqual(shown.models["profiles"], ("replay-local-config",))
        self.assertNotIn("private unbound dialogue", repr(shown.dialogue))
        self.assertTrue(self.render._EXPLORER_REPLAY_ACTIVE)


class RemoteHostObservation(unittest.TestCase):
    def test_compute_host_pump_result_records_success_failure_or_confirmed_absence(self):
        from tools.fleet.render import _explorer_remote_host_observation

        success = _explorer_remote_host_observation({
            "configured": True, "observed_at": 10,
            "hosts": [{"self": True, "reachable": True},
                      {"self": False, "reachable": True}],
        })
        failed = _explorer_remote_host_observation({
            "configured": True, "observed_at": 11,
            "hosts": [{"self": False, "reachable": False}],
        })
        absent = _explorer_remote_host_observation({
            "configured": False, "status": "missing", "observed_at": 12, "hosts": [],
        })
        self.assertEqual((success["state"], success["attempted"]), ("success", True))
        self.assertEqual((failed["state"], failed["attempted"]), ("failed", True))
        self.assertEqual((absent["state"], absent["attempted"]), ("absent", True))


class StartupOrdering(unittest.TestCase):
    def test_cached_draw_happens_before_collection_and_metadata_pumps_start(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tools.fleet import render

        events = []

        class Runtime:
            has_complete = False
            cached_continuation = None
            def configure_persistence(self, *_args, **_kwargs):
                pass
            def load_last_complete(self, *_args, **_kwargs):
                return object()

        class Pump:
            def __init__(self, _producer, _interval, **kwargs):
                self.name = kwargs.get("name", "fleet-refresh")
                self.generation = 0
                self.running = False
            def start(self):
                events.append("start:" + self.name)
                self.running = True
            def health(self, *_args, **_kwargs):
                return {}
            def request_due(self, **_kwargs):
                return False
            def request(self, **_kwargs):
                return False
            def poll(self, *_args, **_kwargs):
                return None
            def stop(self, **_kwargs):
                pass

        class Screen:
            def getmaxyx(self):
                return 45, 168
            def timeout(self, _value):
                pass
            def keypad(self, _value):
                pass

        class Reader:
            def __init__(self, curses_mod, *_args, **_kwargs):
                self.error = curses_mod.ERR
                self.calls = 0
            def read(self, *_args, **_kwargs):
                self.calls += 1
                return (self.error if self.calls == 1 else ord("q")), None
            def has_buffered_input(self):
                return False

        collector = lambda **_kwargs: None
        collector.install_follower = SimpleNamespace(target=lambda: None)
        collector.compute_hosts_refresh = lambda: None
        prior_mode = render._EXPLORER_ENABLED
        prior_published = render._EXPLORER_PUBLISHED_MODEL
        prior_model = render._EXPLORER_MODEL
        prior_readiness = render._EXPLORER_READINESS
        prior_replay = render._EXPLORER_REPLAY_ACTIVE
        prior_shown = render._EXPLORER_SHOWN
        self.addCleanup(render.set_explorer_mode, prior_mode)
        self.addCleanup(setattr, render, "_EXPLORER_PUBLISHED_MODEL", prior_published)
        self.addCleanup(setattr, render, "_EXPLORER_MODEL", prior_model)
        self.addCleanup(setattr, render, "_EXPLORER_READINESS", prior_readiness)
        self.addCleanup(setattr, render, "_EXPLORER_REPLAY_ACTIVE", prior_replay)
        self.addCleanup(setattr, render, "_EXPLORER_SHOWN", prior_shown)
        render.set_explorer_mode(True)

        def record_draw(*_args, **_kwargs):
            self.assertTrue(render._EXPLORER_REPLAY_ACTIVE)
            events.append("draw")

        with patch.object(render, "_explorer_runtime", return_value=Runtime()), \
                patch.object(render, "_init_colors"), \
                patch.object(render, "_configure_input"), \
                patch.object(render, "_CursesInput", Reader), \
                patch.object(render, "RefreshPump", Pump), \
                patch.object(render, "_ExplorerFeed", return_value=None), \
                patch("tools.fleet.explorer.focus.FocusReader", return_value=object()), \
                patch.object(render, "set_refresh_health"), \
                patch.object(render.curses, "curs_set"), \
                patch.object(render, "_draw", side_effect=record_draw), \
                patch.object(render.os, "write", return_value=0):
            render._loop(Screen(), collector, None, "both", 2.0)

        draw_index = events.index("draw")
        starts = [index for index, event in enumerate(events) if event.startswith("start:")]
        self.assertTrue(starts)
        self.assertTrue(all(draw_index < index for index in starts), events)
