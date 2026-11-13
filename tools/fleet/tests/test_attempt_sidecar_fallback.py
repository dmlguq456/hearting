#!/usr/bin/env python3
"""Exact-attempt sidecar fallback for registered dispatch sessions.

A registered worker session (depth-1 owner or depth-2 stage) runs no statusline
producer, so its runtime sid never gains a title/summary sidecar; the SD-95
dispatch summary owner writes under ``dispatch-<attempt_id>`` instead. The
session plane must adopt that exact-attempt sidecar instead of rendering an
empty subtitle (observed 2026-08-07: depth-1 owner card showed no NOW summary
while the attempt sidecar carried one).
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fleet import titles                            # noqa: E402
from fleet.collectors import opencode               # noqa: E402
from fleet.model import Session                     # noqa: E402


class AttemptSidTest(unittest.TestCase):
    def test_valid_attempt_id_maps_to_dispatch_sid(self):
        self.assertEqual(titles.attempt_sid("att-abc123"), "dispatch-att-abc123")

    def test_missing_or_malformed_attempt_id_is_none(self):
        self.assertIsNone(titles.attempt_sid(None))
        self.assertIsNone(titles.attempt_sid(""))
        self.assertIsNone(titles.attempt_sid("att-abc/../evil"))
        self.assertIsNone(titles.attempt_sid(123))


class AttemptSidecarFallbackTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("FLEET_TITLE_STATE_DIR")
        os.environ["FLEET_TITLE_STATE_DIR"] = self._dir.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("FLEET_TITLE_STATE_DIR", None)
        else:
            os.environ["FLEET_TITLE_STATE_DIR"] = self._prev
        self._dir.cleanup()

    def _session(self, **kw):
        return Session(harness="opencode", pid=1, cwd="/x", **kw)

    def test_adopts_attempt_sidecar_when_own_sid_has_none(self):
        titles.write("dispatch-att-f1", "owner title here", harness="opencode",
                     summary="지금 실행 검증 중")
        sess = self._session(attempt_id="att-f1")
        opencode._attempt_sidecar_fallback(sess)
        self.assertEqual(sess.title, "owner title here")
        self.assertEqual(sess.summary, "지금 실행 검증 중")
        self.assertIsNotNone(sess.summary_ts)

    def test_never_overwrites_own_title_or_summary(self):
        titles.write("dispatch-att-f2", "attempt title", harness="opencode",
                     summary="attempt summary")
        sess = self._session(attempt_id="att-f2", title="own title",
                             summary="own summary")
        opencode._attempt_sidecar_fallback(sess)
        self.assertEqual(sess.title, "own title")
        self.assertEqual(sess.summary, "own summary")

    def test_without_attempt_identity_is_a_no_op(self):
        sess = self._session()
        opencode._attempt_sidecar_fallback(sess)
        self.assertIsNone(sess.title)
        self.assertIsNone(sess.summary)


if __name__ == "__main__":
    unittest.main()
