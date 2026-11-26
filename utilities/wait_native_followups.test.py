#!/usr/bin/env python3
"""`wait_native` follow-ups from the #1576 review: retry an unreadable pass within the
deadline, end as session_ended once the seen process is gone, and cut App Server reads to
the time left."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import time
import unittest
from unittest import mock

import peer_steward_native
import terminal_surface

_REAL_SLEEP = time.sleep


def info(state, pid=4242):
    return {"result": {"type": "agent_info", "agent": {
        "agent_status": state, "pid": pid, "pid_start": "100", "pid_namespace": "pid:[1]"}}}


class WaitNativeTest(unittest.TestCase):
    def wait(self, reads, *, lifetime="live", until=("idle",), timeout_ms=2000):
        sequence = iter(reads)

        def native_agent(*args, **kwargs):
            return next(sequence)

        with mock.patch.object(peer_steward_native, "native_agent", side_effect=native_agent), \
                mock.patch.object(peer_steward_native, "native_lifetime", return_value=lifetime), \
                mock.patch.object(terminal_surface.time, "sleep"):
            return terminal_surface.wait_native("codex", "thread", until, timeout_ms)

    def test_a_moments_unreadable_record_is_retried_not_unknown(self):
        result = self.wait([None, None, info("idle")])
        self.assertEqual(result["result"]["agent"]["agent_status"], "idle")

    def test_the_seen_process_gone_ends_the_wait_as_session_ended(self):
        result = self.wait([info("working"), None], lifetime="ended")
        self.assertEqual(result["error"]["code"], "session_ended")
        self.assertEqual(result["agent"]["pid"], 4242)

    def test_unreadable_to_the_deadline_is_unknown_and_never_earlier(self):
        started = time.monotonic()
        with mock.patch.object(peer_steward_native, "native_agent", return_value=None), \
                mock.patch.object(terminal_surface.time, "sleep", side_effect=lambda s: _REAL_SLEEP(0.01)):
            result = terminal_surface.wait_native("codex", "thread", ("idle",), 200)
        self.assertEqual(result["error"]["code"], "unknown")
        self.assertGreaterEqual(time.monotonic() - started, 0.19)

    def test_an_unbounded_wait_gives_up_after_consecutive_unreadable_passes(self):
        reads = [None] * 20
        result = self.wait(reads, timeout_ms=None)
        self.assertEqual(result["error"]["code"], "unknown")

    def test_app_server_reads_are_cut_to_the_time_left(self):
        seen = []
        import codex_queue_delivery
        import codex_queue_dispatch

        class Endpoint:
            def is_socket(self):
                return True

        def status(endpoint, thread, timeout):
            seen.append(timeout)
            return "idle"

        def queue(endpoint, thread, timeout):
            seen.append(timeout)
            return []

        with mock.patch.object(codex_queue_dispatch, "resolve_queue_socket", return_value=Endpoint()), \
                mock.patch.object(codex_queue_delivery, "_latest_turn_status", side_effect=status), \
                mock.patch.object(codex_queue_delivery, "list_queue", side_effect=queue):
            terminal_surface._READ_BUDGET = 0.2
            try:
                terminal_surface._codex_server_busy("thread")
            finally:
                terminal_surface._READ_BUDGET = None
            terminal_surface._codex_server_busy("thread")
        self.assertEqual(seen[:2], [0.2, 0.2])
        self.assertEqual(seen[2:], [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
