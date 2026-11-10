#!/usr/bin/env python3
"""A wrapped Claude caller keeps its exact pane seat through the common join."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]
import pane_ownership as po
import session_tidy as st
from fleet.collectors import claude

spec = importlib.util.spec_from_file_location("wrapped_seat_fixture",
    ROOT / "utilities/pane_ownership_wrapped.test.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class WrappedClaudeSeatTest(unittest.TestCase):
    close_children = fixture.WrappedTidyTest.close_children
    publish = fixture.WrappedTidyTest.publish

    def setUp(self):
        fixture.WrappedTidyTest.setUp(self)
        self.binding = self.publish("claude")
        mock.patch.object(po, "_runtime", side_effect=lambda pid:
            ("claude", False) if pid == self.native["pid"] else (None, False)).start()
        self.native_row = {"sessionId": self.sid, "procStart": self.native["start"]}
        self.registry = mock.patch.object(claude, "read_registry",
                                           return_value=self.native_row).start()
        mock.patch("session_tidy_native.caller_target", return_value=None).start()

    def verify(self, sid=None):
        return po.verified_pane(self.pane, "claude", sid or self.sid,
            pid=self.native["pid"], executable="/fixture/herdr", socket_path=self.socket)

    def test_public_caller_resolution_retains_the_inner_native_exact_pane(self):
        self.assertEqual(self.verify(), self.pane)
        seat, harness, sid = st.resolve_caller("claude", self.sid, self.home.name)
        self.assertEqual((seat.kind, seat.pane, harness, sid),
                         ("pane", self.pane, "claude", self.sid))

    def test_unknown_different_or_parked_native_session_is_not_authority(self):
        for row in (None, {**self.native_row, "sessionId": "another-session"},
                    {**self.native_row, "procStart": "reused"},
                    {**self.native_row, "parkedJobId": "job"}):
            with self.subTest(row=row), mock.patch.object(claude, "read_registry", return_value=row):
                self.assertEqual(self.verify(), "")

    def test_one_seat_observation_decides_with_the_native_selection(self):
        # The seat join is one observation; Claude's own record must name the
        # attachment's session at that look (a reselected TUI is not the seat).
        for row in (None, {**self.native_row, "sessionId": "another-session"}):
            with self.subTest(row=row), mock.patch.object(claude, "read_registry",
                    return_value=row) as read:
                self.assertEqual(self.verify(), "")
                self.assertGreaterEqual(read.call_count, 1)

    def test_inherited_pane_hint_never_claims_another_native_or_service(self):
        for own, service in ((tuple([self.native["pid"] + 100, *self.record[1:]]), False),
                             (self.record, True)):
            with self.subTest(service=service), mock.patch.object(po, "_caller_runtime",
                                                                 return_value=(own, service)):
                self.assertEqual(self.verify(), "")
        with mock.patch.object(po, "_registered_worker", return_value=True):
            self.assertEqual(self.verify(), "")

    def test_the_requested_endpoint_join_decides_and_changed_births_are_rejected(self):
        # The pane is read from the requested endpoint; its foreground owning the
        # attachment is the proof. An inherited native env hint changes nothing.
        for changes in ({"HERDR_PANE_ID": "another:p1"},
                        {"HERDR_SOCKET_PATH": self.socket + ".other"}):
            with self.subTest(changes=changes), mock.patch("fleet.collectors.procscan.read_environ",
                    return_value={"HERDR_PANE_ID": self.pane, "HERDR_SOCKET_PATH": self.socket, **changes}):
                self.assertEqual(self.verify(), self.pane)
        for key in ("native_start", "host_start", "broker_start"):
            with self.subTest(key=key):
                self.publish("claude", **{key: "reused"})
                self.assertEqual(self.verify(), "")
                self.publish("claude")
        with mock.patch.object(po, "_pane_process_info", return_value={
                **self.info, "foreground_process_group_id": self.native["pid"]}):
            self.assertEqual(self.verify(), "")

    def test_swapped_attachment_roles_are_not_a_pane(self):
        self.publish("claude", host_pid=self.native["pid"], host_start=self.native["start"],
                     native_pid=self.host["pid"], native_start=self.host["start"])
        self.assertEqual(self.verify(), "")

    def test_service_or_registered_worker_caller_is_not_the_seat(self):
        for mode in ("service", "registered-worker"):
            with self.subTest(mode=mode):
                def runtime(pid):
                    return ("claude", mode == "service") if pid == self.native["pid"] else ("", False)
                with mock.patch.object(po, "_runtime", side_effect=runtime), \
                     mock.patch.object(po, "_registered_worker", side_effect=lambda pid:
                                       mode == "registered-worker"):
                    self.assertEqual(self.verify(), "")

if __name__ == "__main__":
    unittest.main()
