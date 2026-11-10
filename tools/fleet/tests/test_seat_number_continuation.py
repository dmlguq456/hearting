"""A seat keeps its number when its session provably continues (clear/handover)."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import session_tags
from fleet.collectors import procscan
from fleet.model import Session
from fleet.session_handle import resolve_tag

HARNESSES = ("claude", "codex", "opencode")


def agent(harness, sid, pane):
    return {"agent": harness, "pane_id": pane, "agent_session": {"kind": "id", "value": sid}}


class SeatNumberContinuationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root),
                                           "FLEET_SESSION_REGISTRY_DIR": str(self.root / "registry"),
                                           "CLAUDE_CONFIG_DIR": str(self.root / "claude")})
        env.start()
        self.addCleanup(env.stop)
        patch = mock.patch("fleet.collectors.herdr.list_agents", return_value=[])
        self.agents = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch("fleet.herdr_projection._report")
        patch.start()
        self.addCleanup(patch.stop)
        self.start = procscan.read_proc_start(os.getpid())

    def session(self, harness, sid, started=10, pid=None, start=None):
        return Session(harness=harness, session_id=sid, started_at=started,
                       pid=os.getpid() if pid is None else pid,
                       proc_start=self.start if start is None else start)

    def census(self, sessions, panes):
        self.agents.return_value = [agent(s.harness, s.session_id, pane)
                                    for s, pane in zip(sessions, panes)]
        session_tags.refresh(sessions)

    def rows(self):
        return session_tags._read(session_tags._path())

    def test_same_process_and_pane_keeps_the_number_for_every_harness(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                old, new = harness + "-before-clear", harness + "-after-clear"
                pane = "w:" + harness
                self.census([self.session(harness, old)], [pane])
                before = resolve_tag(harness, old)
                self.census([self.session(harness, new, started=20)], [pane])
                self.assertEqual(resolve_tag(harness, new), before)
                self.assertNotIn((harness, old), self.rows())

    def test_number_returns_when_the_successor_was_already_numbered(self):
        # The live case: the successor already holds a fresh number while the
        # ended predecessor kept the seat's number on the same process.
        self.census([self.session("claude", "e9-session")], ["w:pBA"])
        seat = resolve_tag("claude", "e9-session")
        with mock.patch.object(session_tags, "_continuations", return_value={}):
            self.census([self.session("claude", "e7-session", started=20)], ["w:pBA"])
        self.assertNotEqual(resolve_tag("claude", "e7-session"), seat)
        self.census([self.session("claude", "e7-session", started=20)], ["w:pBA"])
        self.assertEqual(resolve_tag("claude", "e7-session"), seat)
        self.assertNotIn(("claude", "e9-session"), self.rows())

    def test_without_a_record_the_new_session_gets_a_new_number(self):
        cases = {
            "other pane": dict(pane="w:other"),
            "other process start": dict(start="999999999"),
            "no pane": dict(pane=None),
        }
        for label, change in cases.items():
            with self.subTest(label):
                old, new = "old-" + label, "new-" + label
                self.census([self.session("codex", old)], ["w:seat"])
                kept = resolve_tag("codex", old)
                successor = self.session("codex", new, started=20, start=change.get("start"))
                pane = change.get("pane", "w:seat")
                with mock.patch.object(session_tags, "_keep", return_value=True):
                    if pane is None:
                        self.agents.return_value = []
                        session_tags.refresh([successor])
                    else:
                        self.census([successor], [pane])
                self.assertNotEqual(resolve_tag("codex", new), kept)
                self.assertEqual(resolve_tag("codex", old), kept)

    def test_shared_process_serving_two_live_sessions_inherits_nothing(self):
        self.census([self.session("codex", "server-a")], ["w:seat"])
        kept = resolve_tag("codex", "server-a")
        self.census([self.session("codex", "server-b", started=20),
                     self.session("codex", "server-c", started=30)], ["w:seat", "w:seat"])
        self.assertNotIn(kept, {resolve_tag("codex", "server-b"), resolve_tag("codex", "server-c")})

    def test_recorded_lineage_hands_the_number_on_for_every_harness(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                old, new = harness + "-tidy-old", harness + "-tidy-new"
                session_tags.assign_observed(harness, old, pane_id="w:t")
                before = resolve_tag(harness, old)
                self.assertEqual(session_tags.inherit(harness, old, new, pane_id="w:t"), before)
                self.assertEqual(resolve_tag(harness, new), before)
                self.assertNotIn((harness, old), self.rows())
                # A repeated hook event finds nothing left to continue and writes nothing.
                mtime = session_tags._path().stat().st_mtime_ns
                self.assertEqual(session_tags.inherit(harness, old, new, pane_id="w:t"), before)
                self.assertEqual(session_tags._path().stat().st_mtime_ns, mtime)

    def test_lineage_never_takes_a_live_or_unknown_number(self):
        session_tags.assign_observed("codex", "unrelated", pane_id="w:u")
        self.assertIsNone(session_tags.inherit("codex", "never-seen", "fresh"))
        self.assertNotIn(("codex", "fresh"), self.rows())
        self.assertIsNone(session_tags.inherit("codex", "unrelated", "unrelated"))

    def test_continued_number_reaches_peer_trailer(self):
        self.census([self.session("claude", "cl-old")], ["w:pBA"])
        seat = resolve_tag("claude", "cl-old")
        self.census([self.session("claude", "cl-new", started=20)], ["w:pBA"])
        spec = importlib.util.spec_from_file_location("peer_seat", ROOT / "utilities/peer-message.py")
        peer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(peer)
        self.assertIn("claude [%s]" % seat, peer.peer_trailer("claude", "cl-new", pane_id="w:pBA"))


class TidyHookContinuationTest(unittest.TestCase):
    def test_confirmed_clear_predecessor_hands_on_the_number(self):
        sys.path.insert(0, str(ROOT / "utilities"))
        import session_tidy
        seat = session_tidy.Seat("pane", "k", pane="w:p")
        with mock.patch.object(session_tidy, "_confirmed_clear_predecessor",
                               return_value={"harness": "codex", "sid": "a"}), \
             mock.patch("fleet.session_tags.inherit") as inherit:
            session_tidy._continue_seat_number(seat, "codex", "b")
        inherit.assert_called_once_with("codex", "a", "b", pane_id="w:p")
        with mock.patch.object(session_tidy, "_confirmed_clear_predecessor", return_value=None), \
             mock.patch("fleet.session_tags.inherit") as inherit:
            session_tidy._continue_seat_number(seat, "codex", "b")
        inherit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
