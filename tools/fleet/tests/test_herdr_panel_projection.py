"""The Herdr mirror uses the exact shell board and grants no lifecycle authority."""
import copy
import os
from pathlib import Path
import sys
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
from fleet import herdr_projection as projection


class HerdrPanelProjectionTest(unittest.TestCase):
    def setUp(self):
        if hasattr(projection, "_PANEL_LAST"):
            projection._PANEL_LAST = None
        self.binding = dict(harness="codex", sid="current", host_pid=101,
                            native_pid=102, incarnation="current-incarnation")
        self.values = dict(tag="[ab]", answer_wait=4, state="working", model="test-model",
                           ctx_pct=42.4, quota_windows=[("5h", 16.4, None), ("7d", 70, None)],
                           route_label="route 1", title="PRIVATE SUBJECT")
        self.board = [self.question("current", count=2),
                      self.question("current", answerable=False),
                      self.question("current", request_state="submitted"),
                      self.question("foreign"), self.question("current", state="older")]

    def question(self, sid, *, count=1, answerable=True, state="current", **extra):
        return dict(owner=dict(harness="codex", session_id=sid), state=state,
                    answerable=answerable, questions=[dict(question="PRIVATE QUESTION",
                        choices=["PRIVATE CHOICE"])] * count, **extra)

    def command(self, values=None, board=None, harness="codex"):
        build = getattr(projection, "panel_metadata_command", None)
        self.assertTrue(callable(build), "shell metadata mirror is available")
        with mock.patch.object(projection, "compose", return_value=("[ab] ⚑ " + harness, "")):
            return build("herdr", "private:p1", harness, "current",
                         self.values if values is None else values,
                         self.board if board is None else board)

    def tokens(self, command):
        return dict(command[i+1].split("=", 1) for i, item in enumerate(command) if item == "--token")

    def test_current_board_and_answerable_count_are_separate_and_body_free(self):
        before = copy.deepcopy((self.values, self.board))
        command = self.command()
        tokens = self.tokens(command)
        self.assertEqual((tokens["hearting_questions"], tokens["hearting_answer_needed"]), ("4", "2"))
        self.assertIn("Q4/◑A2", command[command.index("--display-agent")+1])
        self.assertNotIn("PRIVATE", repr(command))
        self.assertEqual((self.values, self.board), before)

    def test_unknown_is_not_zero_and_resolved_board_clears_the_wait(self):
        tokens = self.tokens(self.command(values={**self.values, "answer_wait": None}, board=[]))
        self.assertEqual((tokens["hearting_questions"], tokens["hearting_answer_needed"]), ("?", "?"))
        tokens = self.tokens(self.command(values={**self.values, "answer_wait": 0}, board=[]))
        self.assertEqual((tokens["hearting_questions"], tokens["hearting_answer_needed"]), ("0", "0"))

    def test_all_harnesses_keep_native_source_identity_and_user_title(self):
        for harness in projection.HARNESSES:
            with self.subTest(harness=harness):
                command = self.command(harness=harness)
                self.assertEqual(command[command.index("--source")+1], "hearting:pane")
                # Pinned native integrations report identity, not hook authority;
                # applies-to-source would hide their otherwise valid metadata.
                self.assertNotIn("--applies-to-source", command)
                self.assertEqual(command[command.index("--agent")+1], harness)
                self.assertNotIn("--title", command)
                self.assertNotIn("report-agent-session", command)
                self.assertNotIn("report-agent", command)
                self.assertIn("--ttl-ms", command)
                self.assertTrue(command[command.index("--display-agent")+1].startswith("[ab] ⚑ "+harness))

    def test_model_context_and_limits_use_the_shared_formatter(self):
        from fleet.session_line import _percent
        tokens = self.tokens(self.command())
        self.assertEqual(tokens["hearting_context"], _percent(self.values["ctx_pct"]))
        self.assertEqual(tokens["hearting_limits"], "5h 16% · 7d 70%")
        self.assertEqual(tokens["hearting_state"], "working")
        unknown = self.tokens(self.command(values={"tag":"[ab]", "answer_wait":None}, board=[]))
        self.assertEqual(unknown["hearting_context"], "?")
        self.assertEqual(unknown["hearting_state"], "unknown")
        self.assertIn("?", unknown["hearting_limits"])

    def project(self, joined, *, worker=False):
        report = getattr(projection, "project_panel", None)
        self.assertTrue(callable(report), "existing shell callback can project metadata")
        check = mock.Mock(return_value=joined)
        peer = SimpleNamespace(_pane_shell_binding_result=check)
        with mock.patch.dict(sys.modules, {"_hearting_panel_peer_steward":peer}), \
                mock.patch.object(projection, "is_worker", return_value=worker), \
                mock.patch.object(projection.shutil, "which", return_value="herdr"), \
                mock.patch.object(projection, "panel_metadata_command", return_value=["prepared-metadata", "--seq", "1"]) as build, \
                mock.patch.object(projection, "_send_projection") as send, \
                mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH":"/private/server.sock"}):
            self.assertTrue(report(self.binding, self.values, self.board, pane_id="private:p1"))
            return check, build, send

    def test_fresh_birth_bound_join_is_the_only_effect_path(self):
        check, build, send = self.project((self.binding, None))
        check.assert_called_once_with("private:p1", "codex", "current", expected=self.binding,
                                      server="/private/server.sock")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args, (["prepared-metadata", "--seq", "1"], {}))
        build.assert_called_once()

    def test_changed_or_unknown_native_binding_never_reports(self):
        for reason in ("target-identity-changed", "attachment-ambiguous", None):
            with self.subTest(reason=reason):
                _check, _build, send = self.project((None, reason))
                send.assert_not_called()

    def test_registered_worker_never_repaints_its_parent(self):
        check, build, send = self.project((self.binding, None), worker=True)
        check.assert_not_called();build.assert_not_called();send.assert_not_called()

    def test_unchanged_projection_uses_one_publication_until_an_existing_event_refreshes_it(self):
        report = getattr(projection, "project_panel", None)
        self.assertTrue(callable(report))
        peer = SimpleNamespace(_pane_shell_binding_result=mock.Mock(return_value=(self.binding, None)))
        def sent(command, observed):
            observed["metadata_report"] = "exit0"
        with mock.patch.dict(sys.modules, {"_hearting_panel_peer_steward":peer}), \
                mock.patch.object(projection, "is_worker", return_value=False), \
                mock.patch.object(projection.shutil, "which", return_value="herdr"), \
                mock.patch.object(projection, "compose", return_value=("[ab] codex", "")), \
                mock.patch.object(projection, "_send_projection", side_effect=sent) as send, \
                mock.patch.object(projection.time, "monotonic", side_effect=[10, 11, 251]):
            for _ in range(3):
                report(self.binding, self.values, self.board, pane_id="private:p1")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(peer._pane_shell_binding_result.call_count, 2)

    def test_restarted_endpoint_at_the_same_path_gets_a_fresh_publication(self):
        report = getattr(projection, "project_panel", None)
        self.assertTrue(callable(report))
        peer = SimpleNamespace(_pane_shell_binding_result=mock.Mock(return_value=(self.binding, None)))
        def sent(command, observed):
            observed["metadata_report"] = "exit0"
        with tempfile.TemporaryDirectory(prefix="herdr-panel-") as directory, \
                mock.patch.dict(sys.modules, {"_hearting_panel_peer_steward":peer}), \
                mock.patch.object(projection, "is_worker", return_value=False), \
                mock.patch.object(projection.shutil, "which", return_value="herdr"), \
                mock.patch.object(projection, "compose", return_value=("[ab] codex", "")), \
                mock.patch.object(projection, "_send_projection", side_effect=sent) as send:
            endpoint = Path(directory)/"herdr.sock"
            with mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH":str(endpoint)}):
                with socket.socket(socket.AF_UNIX) as first:
                    first.bind(str(endpoint))
                    report(self.binding, self.values, self.board, pane_id="private:p1")
                    report(self.binding, self.values, self.board, pane_id="private:p1")
                endpoint.unlink()
                with socket.socket(socket.AF_UNIX) as replacement:
                    replacement.bind(str(endpoint))
                    report(self.binding, self.values, self.board, pane_id="private:p1")
            self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
