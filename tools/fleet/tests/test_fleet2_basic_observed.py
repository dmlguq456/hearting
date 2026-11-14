"""A launch with nothing to replay shows the basic collection with its late cells marked.

The frame before the required details are in is shown, never published and never kept as the
last complete model; cells those details fill read "details wait", not a blank or a value.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.fleet import render
from tools.fleet.explorer import live, order
from tools.fleet.explorer.runtime import ExplorerRuntime
from tools.fleet.tests import explorer_fixture as ef
from tools.fleet.tests.test_explorer_initial import _Screen

BASIC = {"state": "complete", "observed_at": 10, "generation_identity": "g-1"}


def observations(*, details):
    return {
        "basic": dict(BASIC),
        "required-details": ({"state": "complete", "generation_identity": "g-1", "all_new": True}
                             if details else {"state": "pending"}),
        "resource-runs": {"state": "absent", "attempted": True, "all_new": True},
        "usage:claude": {"state": "complete", "observed_at": 10},
        "usage:codex": {"state": "complete", "observed_at": 10},
        "usage:opencode": {"state": "complete", "observed_at": 10},
        "remote-host": {"state": "ok", "attempted": True},
    }


class BasicObservedFrame(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)   # the saver thread may still be writing
        self.addCleanup(self.temp.cleanup)
        self.runtime = ExplorerRuntime(Path(self.temp.name), strict_readiness=True)
        self.runtime.configure_persistence({"claude", "codex", "opencode"}, strict_readiness=True)
        for patch in (mock.patch.object(order, "order_path", return_value=Path(self.temp.name) / "order.json"),
                      mock.patch.object(render, "_EXPLORER_RUNTIME", self.runtime),
                      mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", None),
                      mock.patch.object(render, "_EXPLORER_MODEL", None),
                      mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", False),
                      mock.patch.object(render, "_EXPLORER_SHOWN", False),
                      mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"})):
            patch.start()
            self.addCleanup(patch.stop)
        render.set_explorer_mode(True)
        self.addCleanup(render.set_explorer_mode, False)
        self.snapshot, hosts = ef.demo_snapshot()
        self.snapshot.compute_hosts = hosts

    def draw(self, *, details, titled=True):
        self.snapshot.observations = observations(details=details)
        if not titled:
            for session in self.snapshot.sessions:
                session.title = None
        screen = _Screen()
        render._draw(screen, self.snapshot.sessions, self.snapshot.jobs, "all", 0, loading=False,
                     snapshot=self.snapshot)
        return screen.text()

    def test_basic_only_is_observed_not_ready(self):
        self.draw(details=False)
        readiness = render._EXPLORER_READINESS
        self.assertTrue(readiness["observed"])
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["all_new"])
        self.assertEqual(readiness["details_pending"], ("required-details",))
        self.assertTrue(ExplorerRuntime.observed_only(readiness))
        self.assertFalse(ExplorerRuntime.observed_only({"observed": True, "ready": True}))
        self.draw(details=True)
        self.assertFalse(ExplorerRuntime.observed_only(render._EXPLORER_READINESS))

    def test_i_the_late_cells_say_details_wait_and_the_frame_is_not_loading(self):
        text = self.draw(details=False, titled=False)
        self.assertNotIn("loading", text)
        self.assertIn("details wait", text)
        self.assertIn("TITLE · details wait", text)       # the header marks the column too
        self.assertGreater(text.count("details wait"), 1)   # and a row's own title cell says it

    def test_ii_the_frame_is_not_published_or_kept_as_the_last_complete_model(self):
        self.draw(details=False, titled=False)
        self.assertIsNone(render._EXPLORER_PUBLISHED_MODEL)
        self.assertFalse(self.runtime.has_complete)
        self.assertFalse(self.runtime.publication_ready(render._EXPLORER_READINESS))
        self.assertFalse(self.runtime.all_new(render._EXPLORER_READINESS))
        with mock.patch.object(self.runtime, "persist_complete_async") as persist:
            self.draw(details=False, titled=False)
        persist.assert_not_called()
        self.assertIsNone(self.runtime.load_last_complete())

    def test_iii_the_complete_frame_is_the_one_drawn_without_this_path(self):
        self.draw(details=False, titled=False)
        complete = self.draw(details=True, titled=False)
        self.assertNotIn("details wait", complete)
        self.assertNotIn("loading", complete)
        self.assertTrue(self.runtime.has_complete)
        self.assertIsNotNone(render._EXPLORER_PUBLISHED_MODEL)
        # The same snapshot drawn in a fresh process state (no basic frame first) gives the same screen.
        with mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", None):
            fresh_runtime = ExplorerRuntime(Path(self.temp.name) / "fresh", strict_readiness=True)
            fresh_runtime.configure_persistence({"claude", "codex", "opencode"}, strict_readiness=True)
            with mock.patch.object(render, "_EXPLORER_RUNTIME", fresh_runtime):
                self.assertEqual(self.still(self.draw(details=True, titled=False)), self.still(complete))

    @staticmethod
    def still(text):
        return text.translate({ord(glyph): "*" for glyph in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"})   # the working spinner turns

    def test_iv_a_late_cell_never_reads_as_a_value(self):
        marked = live.mark_details_wait(self._model(), {"details_pending": ("required-details",)})
        for row in marked.sessions:
            if row.get("title_wait"):
                self.assertIsNone(row.get("title"))          # the mark replaces nothing that was a value
        titled = [row for row in marked.sessions if row.get("title")]
        for row in titled:
            self.assertNotIn("title_wait", row)
        # Nothing pending: the model is returned as it is.
        model = self._model()
        self.assertIs(live.mark_details_wait(model, {"details_pending": ()}), model)
        self.assertIs(live.mark_details_wait(model, None), model)

    def _model(self):
        self.snapshot.observations = observations(details=False)
        render._draw(_Screen(), self.snapshot.sessions, self.snapshot.jobs, "all", 0, loading=False,
                     snapshot=self.snapshot)
        return render._EXPLORER_COMPOSE_MODEL

    def test_a_saved_replay_keeps_showing_until_ready(self):
        replay = self._model()
        with mock.patch.object(render, "_EXPLORER_PUBLISHED_MODEL", replay), \
                mock.patch.object(render, "_EXPLORER_REPLAY_ACTIVE", True):
            text = self.draw(details=False, titled=False)
        self.assertNotIn("details wait", text)


if __name__ == "__main__":
    unittest.main()
