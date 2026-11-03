"""The Codex hook's tui-identity record and the Fleet reader that uses it."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from fleet import codex_tui_identity as identity  # noqa: E402
from fleet.collectors import codex, opencode, procscan  # noqa: E402

PID, START = 51001, "123456"
THREAD = "01a0fa57-7f0e-7eb0-bffe-1d56df94e90c"
OTHER = "01a0fa57-7f0e-7eb0-bffe-1d56df94e91c"


class RecordAndRead(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        self.env = {"HOME": self.state.name, "XDG_STATE_HOME": self.state.name}
        self.directory = os.path.join(self.state.name, "hearting", "tui-identity")

    def write(self, thread=THREAD, tui=(PID, START)):
        with mock.patch.object(identity, "main_thread", return_value=True):
            return identity.record(thread, self.env, tui=tui)

    def test_record_uses_the_shared_format_and_reads_back(self):
        self.assertTrue(self.write())
        path = os.path.join(self.directory, "%d-%s.json" % (PID, START))
        with open(path) as handle:
            row = json.load(handle)
        self.assertEqual((row["schema"], row["pid"], row["start"], row["sessionID"], row["harness"]),
                         ("hearting-tui-selection-v1", PID, START, THREAD, "codex"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(identity.read_thread(PID, START, self.env), THREAD)

    def test_a_switched_thread_replaces_the_record(self):
        self.write()
        self.write(OTHER)
        self.assertEqual(identity.read_thread(PID, START, self.env), OTHER)

    def test_another_start_time_or_pid_reads_nothing(self):
        self.write()
        self.assertIsNone(identity.read_thread(PID, "999", self.env))
        self.assertIsNone(identity.read_thread(PID + 1, START, self.env))

    def test_opencode_reader_never_accepts_a_codex_record(self):
        self.write()
        self.assertIsNone(opencode._tui_selection_record(PID, START, self.env))

    def test_codex_reader_ignores_an_opencode_record(self):
        os.makedirs(self.directory)
        with open(os.path.join(self.directory, "%d-%s.json" % (PID, START)), "w") as handle:
            json.dump({"schema": "hearting-tui-selection-v1", "pid": PID, "start": START,
                       "sessionID": "ses_abc123"}, handle)
        self.assertIsNone(identity.read_thread(PID, START, self.env))

    def test_no_tui_above_the_hook_writes_nothing(self):
        with mock.patch.object(identity, "main_thread", return_value=True), \
                mock.patch.object(identity, "tui_above", return_value=None):
            self.assertFalse(identity.record(THREAD, self.env))
        self.assertFalse(os.path.exists(self.directory))

    def test_subagent_thread_and_bad_ids_write_nothing(self):
        with mock.patch.object(identity, "main_thread", return_value=False):
            self.assertFalse(identity.record(THREAD, self.env, tui=(PID, START)))
        self.assertFalse(self.write("not-a-thread"))

    def test_prune_drops_only_dead_codex_records(self):
        os.makedirs(self.directory)
        def put(name, **row):
            with open(os.path.join(self.directory, name), "w") as handle:
                json.dump(row, handle)
        put("40000-1.json", schema="x", harness="codex", sessionID=OTHER)
        put("40001-1.json", schema="x", sessionID="ses_abc")      # OpenCode's, retained
        with mock.patch.object(procscan, "read_proc_start", return_value=None):
            self.write()
        left = set(os.listdir(self.directory))
        self.assertNotIn("40000-1.json", left)
        self.assertIn("40001-1.json", left)
        self.assertIn("%d-%s.json" % (PID, START), left)


class HookWiring(unittest.TestCase):
    def setUp(self):
        hooks = str(TOOLS.parent / "adapters" / "codex" / "hooks")
        sys.path.insert(0, hooks)
        self.addCleanup(sys.path.remove, hooks)
        sys.modules.pop("herdr_session_projection", None)
        import herdr_session_projection
        self.module = herdr_session_projection

    def test_main_hook_records_and_a_worker_hook_does_not(self):
        with mock.patch.object(identity, "record") as record, \
                mock.patch.object(self.module, "_project", return_value=True), \
                mock.patch.object(self.module, "_state_hook_owns_report", return_value=False):
            self.module.project({}, THREAD, worker=False, request_recovery=False)
            record.assert_called_once_with(THREAD)
            record.reset_mock()
            self.module.project({}, THREAD, worker=True, request_recovery=False)
            record.assert_not_called()

    def test_tui_above_walks_past_a_managed_app_server_to_the_tui(self):
        parents = {900: 800, 800: 700, 700: 1}
        comm = {900: "bash", 800: "codex", 700: "codex"}
        starts = {800: None, 700: START}
        stat = lambda pid: ("%d (x) S %d" % (pid, parents[pid])).encode()
        def opener(path, mode="r", *a, **k):
            pid = int(path.split("/")[2])
            return mock.mock_open(read_data=stat(pid))()
        with mock.patch.object(procscan, "_comm_of", side_effect=lambda pid: comm.get(pid, "")), \
                mock.patch.object(codex, "_live_tui_start", side_effect=lambda pid: starts.get(pid)), \
                mock.patch("builtins.open", opener):
            self.assertEqual(identity.tui_above(900), (700, START))


class CollectorFallback(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(codex, "_live_tui_start", return_value=START),
            mock.patch.object(procscan, "read_proc_start", return_value=START),
            mock.patch.object(identity, "read_thread", return_value=THREAD),
            mock.patch.object(codex, "exact_rollout_for_session_id", return_value="/r/rollout.jsonl"),
            mock.patch.object(codex, "_rollout_meta", return_value={"source": "cli"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_record_identifies_a_tui_the_daemon_cannot(self):
        self.assertEqual(codex._hook_recorded_session(PID), THREAD)

    def test_subagent_or_missing_rollout_or_dead_tui_stays_unidentified(self):
        with mock.patch.object(codex, "_rollout_meta", return_value={"source": {"subagent": {}}}):
            self.assertIsNone(codex._hook_recorded_session(PID))
        with mock.patch.object(codex, "exact_rollout_for_session_id", return_value=None):
            self.assertIsNone(codex._hook_recorded_session(PID))
        with mock.patch.object(codex, "_live_tui_start", return_value=None):
            self.assertIsNone(codex._hook_recorded_session(PID))
        with mock.patch.object(identity, "read_thread", return_value=None):
            self.assertIsNone(codex._hook_recorded_session(PID))

    def test_process_identity_prefers_the_daemon_and_falls_back_to_the_record(self):
        env = {"CODEX_HOME": "/fake"}
        common = [mock.patch.object(codex.os, "readlink", return_value="/w"),
                  mock.patch.object(procscan, "read_environ", return_value=env),
                  mock.patch.object(procscan, "codex_effective_cwd", return_value="/w"),
                  mock.patch.object(procscan, "_comm_of", return_value="codex"),
                  mock.patch.object(procscan, "_read_argv", return_value=["codex"]),
                  mock.patch.object(procscan, "invocation_mode", return_value="session"),
                  mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False),
                  mock.patch.object(codex, "_proc_rollout", return_value=None)]
        for patch in common:
            patch.start()
            self.addCleanup(patch.stop)
        with mock.patch.object(codex, "_daemon_session_for_process", return_value=OTHER):
            self.assertEqual(codex.session_id_of_process(PID), OTHER)
        with mock.patch.object(codex, "_daemon_session_for_process", return_value=None):
            self.assertEqual(codex.session_id_of_process(PID), THREAD)


if __name__ == "__main__":
    unittest.main()
