#!/usr/bin/env python3
"""A peer that has an identity must be shown wearing it.

Every relation row on the board is `[xx] <harness>` when the peer can be identified at
all. Before 2026-09-10 the same peer appeared six ways, and three of the causes were
identity thrown away rather than identity missing:

  * Claude's own SendMessage addresses a peer by its socket, and the socket is named
    after that session's PID — an exact identity the ledger stored as a `name` (222 rows).
  * A Claude derived name (`kws-net-15`) is the very string the badge is read off, so a
    row carrying the name but no id could still be badged.
  * The receive path dropped the sender's harness whenever the sender had no session id,
    leaving the renderer nothing to look anything up with — while the send path had
    always kept it.

The base fix under all three: the Claude session registry was being looked for under
`AGENT_HOME`, which is the harness root and has no `sessions/` at all.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from fleet import render, session_handle                          # noqa: E402
from fleet.collectors import peer_messages                        # noqa: E402
from pathlib import Path  # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[3] / "utilities"))  # split_module_fixture
from split_module_fixture import patch_split  # noqa: E402


def _write_session(cfg, pid, session_id, name=None, name_source="derived"):
    sessions = os.path.join(cfg, "sessions")
    os.makedirs(sessions, exist_ok=True)
    record = {"pid": pid, "sessionId": session_id}
    if name:
        record.update(name=name, nameSource=name_source)
    with open(os.path.join(sessions, "%s.json" % pid), "w", encoding="utf-8") as fh:
        json.dump(record, fh)


class SessionsDirectoryTest(unittest.TestCase):
    """`AGENT_HOME` is the harness root, not a Claude config dir."""

    def test_a_candidate_without_a_sessions_directory_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness_root = os.path.join(tmp, "release")     # no sessions/ — a real release
            real = os.path.join(tmp, "claude")
            os.makedirs(harness_root)
            _write_session(real, 4242, "sid-a")
            with mock.patch.dict(os.environ, {"AGENT_HOME": harness_root,
                                              "HOME": tmp, "CLAUDE_HOME": ""},
                                 clear=False):
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
                with mock.patch.object(os.path, "expanduser", return_value=real):
                    self.assertEqual(session_handle._claude_sessions_dir(),
                                     os.path.join(real, "sessions"))

    def test_no_readable_registry_anywhere_is_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nowhere")
            with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": missing}, clear=False), \
                 mock.patch.object(os.path, "expanduser", return_value=missing):
                self.assertIsNone(session_handle._claude_sessions_dir())


class SocketAddressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = self.tmp.name
        _write_session(self.cfg, 284639, "sid-peer")

    def _resolve(self, address):
        return session_handle.session_id_for_address(address, config_dir=self.cfg)

    def test_a_socket_address_names_the_session_behind_it(self):
        self.assertEqual(self._resolve("uds:/run/user/1002/cc-socks/284639.sock"),
                         "sid-peer")

    def test_an_unknown_pid_resolves_to_nothing_rather_than_a_guess(self):
        self.assertIsNone(self._resolve("uds:/run/user/1002/cc-socks/999999.sock"))

    def test_a_record_that_disagrees_about_its_own_pid_is_refused(self):
        _write_session(self.cfg, 284640, "sid-other")
        path = os.path.join(self.cfg, "sessions", "284640.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 111, "sessionId": "sid-other"}, fh)
        self.assertIsNone(self._resolve("uds:/x/284640.sock"))

    def test_anything_that_is_not_a_socket_address_is_left_alone(self):
        for value in ("hearting-46", "/tmp/x.sock", "http://example/x", "", None, 42,
                      "uds:/run/user/1002/cc-socks/abc.sock"):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))


class DerivedNameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = self.tmp.name

    def _resolve(self, name):
        return session_handle.session_id_for_derived_name(name, home=self.cfg)

    def test_a_derived_name_names_its_session(self):
        _write_session(self.cfg, 1, "sid-15", name="kws-net-15")
        self.assertEqual(self._resolve("kws-net-15"), "sid-15")

    def test_a_user_set_name_of_the_same_shape_is_not_an_identity(self):
        # F-100a: `release-1a` looks exactly like a derived name and is not one.
        _write_session(self.cfg, 2, "sid-x", name="release-1a", name_source="user")
        self.assertIsNone(self._resolve("release-1a"))

    def test_a_name_two_sessions_answer_to_names_neither(self):
        _write_session(self.cfg, 3, "sid-p", name="repo-aa")
        _write_session(self.cfg, 4, "sid-q", name="repo-aa")
        self.assertIsNone(self._resolve("repo-aa"))

    def test_a_name_that_is_not_derived_shaped_is_not_searched_for(self):
        for value in ("peer-test-codex", "케언 인계", "", None, 7):
            with self.subTest(value=value):
                self.assertIsNone(self._resolve(value))


class EndpointUpgradeTest(unittest.TestCase):
    """The renderer's ladder, from the two identities it could not read before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _write_session(self.tmp.name, 777, "sid-peer", name="kws-net-15")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _label(self, name, tag_by_key=None):
        return "".join(t for t, _k in render._peer_endpoint_segs(
            "claude", None, name, tag_by_key or {}))

    def test_an_address_only_row_reaches_the_badge(self):
        self.assertEqual(self._label("uds:/run/user/1002/cc-socks/777.sock",
                                     {("claude", "sid-peer"): "15"}), "[15]")

    def test_a_name_only_row_reaches_the_badge(self):
        self.assertEqual(self._label("kws-net-15", {("claude", "sid-peer"): "15"}),
                         "[15]")

    def test_a_peer_that_resolves_to_no_badge_keeps_its_readable_name(self):
        # Losing a usable name to reach a badge that does not exist would be a downgrade.
        with patch_split(render, "_resolve_session_tag", return_value=None):
            self.assertEqual(self._label("peer-test-codex"), "peer-test-codex")


class ReceivedHarnessTest(unittest.TestCase):
    """The send path always kept an identity-less peer's harness; the receive path did not,
    which is half of "받는 세션도 좀 이상하긴하네 일관성이 없고" (user 2026-09-09)."""

    def _collect(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "peer-messages", "2026-09")
            os.makedirs(path)
            with open(os.path.join(path, "src.jsonl"), "w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(json.dumps(record) + "\n")
            return peer_messages.collect(state_roots=[tmp])["by_session"]

    def _record(self, kind="notice"):
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
        return {"ts": ts, "kind": kind,
                "from": {"harness": "claude", "session_id": "", "name": "kws-net-15"},
                "to": {"harness": "claude", "session_id": "sid-recv"},
                "delivery": {"status": "received"}}

    def test_a_sender_with_no_id_still_reports_its_harness(self):
        row = self._collect([self._record()])[("claude", "sid-recv")]
        self.assertEqual(row["last_recv"]["from_harness"], "claude")
        self.assertIsNone(row["last_recv"]["from_session_id"])
        self.assertEqual(row["last_recv"]["from_name"], "kws-net-15")

    def test_the_two_directions_agree_about_an_identity_less_peer(self):
        rows = self._collect([self._record(kind="steer")])
        sent = rows[("claude", "sid-recv")]["last_recv"]
        # Same record read from the other end: the sender keyed by (harness, id) is absent,
        # so what matters is that neither side invents nor discards the harness.
        self.assertEqual(sent["from_harness"], "claude")


if __name__ == "__main__":
    unittest.main()
