"""Confirmed basic activity paints while unrelated required detail is pending."""
from contextlib import ExitStack
import os
import unittest
from unittest import mock

from tools.fleet import render
from tools.fleet.explorer import live
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.explorer.state import NavState
from tools.fleet.model import Session
from tools.fleet.native_refresh import NativeDisplayLayer
from tools.fleet.refresh import LiveSnapshot
from tools.fleet.tests.test_explorer_initial import _Screen
from split_module_fixture import patch_split


def observation(generation, state, *, complete=False, birth="fixture-birth"):
    return LiveSnapshot(sessions=[Session(
        harness="codex", pid=420001, proc_start=birth, cwd="/fixture-project",
        session_id="01000000-0000-7000-8000-000000000001", session_tag="a1",
        model="fixture-model", liveness=state)], observations={
            "basic": {"state": "complete", "generation_identity": generation},
            "required-details": {"state": "complete" if complete else "pending",
                                 "generation_identity": generation, "all_new": True},
            "usage:codex": {"state": "complete"},
            "remote-host": {"state": "absent", "attempted": True},
            "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
        })


class PendingActivityTest(unittest.TestCase):
    def check_transition(self, previous, current, *, scope="-"):
        runtime = ExplorerRuntime()
        runtime.configure_persistence({"codex"}, strict_readiness=True)
        screen = _Screen(103, 334)
        with ExitStack() as stack:
            for name, value in {
                "_EXPLORER_ENABLED": True, "_EXPLORER_STATE": NavState(scope=scope),
                "_EXPLORER_MODEL": None, "_EXPLORER_PUBLISHED_MODEL": None,
                "_EXPLORER_REPLAY_ACTIVE": False, "_EXPLORER_SHOWN": False,
                "_EXPLORER_COMPOSE_MODEL": None, "_EXPLORER_COMPOSE_SNAPSHOT": None,
                "_EXPLORER_COMPOSE_DETAIL": None, "_EXPLORER_COMPOSE_GENERATION": None,
                "_EXPLORER_RESULT": None, "_EXPLORER_RUNTIME": runtime,
                "_EXPLORER_NATIVE_DISPLAY": NativeDisplayLayer(),
                "_EXPLORER_ORDER": [], "_EXPLORER_SIZE": None,
                "_EXPLORER_PAINT_CACHE": None, "_EXPLORER_DRAW_CACHE": None,
            }.items():
                stack.enter_context(patch_split(render, name, value))
            persist = stack.enter_context(mock.patch.object(runtime, "persist_complete_async"))
            stack.enter_context(mock.patch.object(render, "_key_attr", return_value=0))
            stack.enter_context(mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}))

            def draw(snapshot, generation, clock):
                with patch_split(render, "_EXPLORER_GENERATION", generation), \
                     mock.patch.object(render.time, "time", return_value=clock):
                    render._draw(screen, snapshot.sessions, [], "all", 0, snapshot=snapshot)
                return render._EXPLORER_MODEL

            base = draw(observation("g1", previous, complete=True), 1, 1001)
            admitted = render._EXPLORER_PUBLISHED_MODEL
            for clock in (1002, 1063):
                shown = draw(observation("g2", current), 2, clock)
                self.assertEqual(shown.sessions[0]["state"], current)
                self.assertIs(render._EXPLORER_PUBLISHED_MODEL, admitted)
                self.assertEqual(shown.routes, base.routes)
                self.assertEqual(shown.source_records, base.source_records)
                self.assertFalse(render._EXPLORER_READINESS["ready"])
                count = "1 working" if current == "working" else "0 working"
                self.assertIn(count, screen.text())
            self.assertEqual(persist.call_count, 1)
            self.assertEqual(draw(observation("g2", current, complete=True), 2, 1064)
                             .sessions[0]["state"], current)
            self.assertTrue(render._EXPLORER_READINESS["ready"])

    def test_working_to_idle_does_not_wait_for_required_details(self):
        self.check_transition("working", "idle")

    def test_selected_idle_to_working_does_not_wait_for_required_details(self):
        self.check_transition("idle", "working", scope="session:a1")

    def test_reused_process_does_not_receive_the_other_incarnations_activity(self):
        old = observation("g1", "working", complete=True)
        new = observation("g2", "idle", birth="new-birth")
        model = live.build_view_model(old, now=1)
        candidate = live.build_view_model(new, now=2)
        self.assertIs(live.current_session_activity(model, candidate, new), model)


if __name__ == "__main__":
    unittest.main()
