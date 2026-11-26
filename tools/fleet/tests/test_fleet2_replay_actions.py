"""A persisted screen may navigate, but never supplies a live control target."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.fleet import render


class ReplayActionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.prior = render._EXPLORER_REPLAY_ACTIVE
        self.addCleanup(setattr, render, "_EXPLORER_REPLAY_ACTIVE", self.prior)

    def row(self, target):
        return SimpleNamespace(kind="session", target_ref=target, identity="session:c7")

    def test_stale_frame_cannot_supply_a_live_session_target(self):
        target = SimpleNamespace(pid=431, harness="codex", session_id="old-session")
        row = self.row(target)
        render._EXPLORER_REPLAY_ACTIVE = True
        with patch.object(render, "_selectable_session") as selectable:
            self.assertEqual(render._explorer_targets([row]), [])
            selectable.assert_not_called()

    def test_fresh_frame_targets_only_the_current_source_object(self):
        current = SimpleNamespace(pid=432, harness="codex", session_id="new-session")
        row = self.row(current)
        render._EXPLORER_REPLAY_ACTIVE = False
        selected = {"pid": current.pid, "sid": current.session_id, "kind": "session"}
        with patch.object(render, "_selectable_session", return_value=True), \
                patch.object(render, "session_parent_visible", return_value=True), \
                patch.object(render, "_select_entry", return_value=selected) as select_entry:
            targets = render._explorer_targets([row])
        self.assertEqual(targets, [dict(selected, nav_id="session:c7")])
        select_entry.assert_called_once_with(current, 0)


if __name__ == "__main__":
    unittest.main()
