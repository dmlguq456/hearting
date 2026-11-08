"""Daemon-loaded Codex TUI identity fails closed on incomplete observations."""
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2]
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
UTILITIES = TOOLS.parent / "utilities"
if str(UTILITIES) not in sys.path:
    sys.path.insert(0, str(UTILITIES))

from fleet.collectors import codex, procscan  # noqa: E402

_REAL_LISTENER_SNAPSHOT = codex._tcp_listener_snapshot
PID = 43101
ROOT = "01000013-0000-7000-8000-00000000001b"
SECOND = "01000013-0000-7000-8000-00000000001c"
CHILD = "01000013-0000-7000-8000-00000000001d"


class FakeDaemonWebSocket:
    """Collector RPC double; transport framing is covered by the queue client tests."""

    def __init__(self, threads, *, loaded_pages=None, status_pages=None):
        self.threads = threads
        self.loaded_pages = loaded_pages
        self.status_pages = status_pages or {}
        self.read_fail = set()
        self.status_fail = set()
        self.loaded_fail = False
        self.calls = []
        self.closed = False
        self.initialized = False

    def initialize(self):
        # The live daemon refuses a second initialize on one connection.
        if self.initialized:
            raise OSError("Already initialized")
        self.initialized = True
        self.calls.append(("initialize", {}))

    def connect(self, _path, _timeout):
        """Mirror codex_queue_delivery._connect: a fresh, already initialized connection."""
        self.initialized = False
        self.closed = False
        self.initialize()
        return self

    @staticmethod
    def _page(pages, params):
        cursor = params.get("cursor")
        if cursor is None:
            index = 0
        else:
            index = next((i + 1 for i, page in enumerate(pages[:-1])
                          if page.get("nextCursor") == cursor), -1)
        if index < 0 or index >= len(pages):
            raise AssertionError(f"unexpected cursor: {cursor!r}")
        return pages[index]

    def request(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "thread/loaded/list":
            if self.loaded_fail:
                raise OSError("loaded list failed")
            if self.loaded_pages is None:
                return {"data": list(self.threads)}
            return self._page(self.loaded_pages, params)
        thread_id = params["threadId"]
        if method == "mcpServerStatus/list":
            if thread_id in self.status_fail:
                raise OSError("status read failed")
            pages = self.status_pages.get(thread_id)
            if pages is None:
                port, _source = self.threads[thread_id]
                data = ([{"name": "codex_tui", "httpOrigin": f"http://127.0.0.1:{port}",
                          "runtimeStatus": "connected"}] if port else [])
                pages = [{"data": data}]
            return self._page(pages, params)
        if method == "thread/read":
            if thread_id in self.read_fail:
                raise OSError("thread read failed")
            _port, source = self.threads[thread_id]
            thread = {"id": thread_id, "threadSource": source}
            if source == "subagent":
                thread["parentThreadId"] = ROOT
            return {"thread": thread}
        raise AssertionError(f"unexpected daemon method: {method}")

    def close(self):
        self.closed = True


class CodexDaemonIdentityTest(unittest.TestCase):
    def setUp(self):
        codex._DAEMON_ORIGINS.clear()
        self.threads = {ROOT: (41301, "user"), CHILD: (41301, "subagent")}
        self.client = FakeDaemonWebSocket(self.threads)
        self.socket_identity = ("/tmp/fake-codex.sock", 1, 22, os.geteuid(), 0o600)
        self.listener = (frozenset({"881122"}), frozenset({PID}))
        self._patches = [
            mock.patch.object(codex, "_control_socket", return_value=Path("/tmp/fake-codex.sock")),
            mock.patch.object(codex, "_control_socket_identity", return_value=self.socket_identity),
            mock.patch("codex_queue_delivery._connect", side_effect=self.client.connect),
            mock.patch.object(codex, "_tcp_listener_snapshot", return_value=self.listener),
            mock.patch.object(codex, "_live_tui_start", return_value="778899"),
            mock.patch.object(procscan, "read_proc_start", return_value="778899"),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_answer_root_rechecks_an_already_loaded_threads_changed_origin(self):
        import session_shell
        self.threads[ROOT] = (41302, 'user')
        self.threads[SECOND] = (41301, 'user')
        empty = (frozenset(), frozenset())
        with mock.patch.object(codex, '_tcp_listener_snapshot', side_effect=lambda port, *args: self.listener if port == 41301 else empty), \
             mock.patch.object(session_shell, 'process', return_value={'pid':PID,'start':'778899'}), \
             mock.patch.object(codex, 'session_id_of_process', side_effect=lambda pid, **kw: codex._daemon_session_for_process(pid, {}, **kw)):
            self.assertTrue(session_shell._codex_current_root(PID, '778899', SECOND))
            # /resume reattaches an already loaded root to the same live TUI.
            # Loaded membership, PID/birth and listening port do not change.
            self.threads[ROOT] = (41301, 'user')
            self.assertFalse(session_shell._codex_current_root(PID, '778899', SECOND))

    def test_loaded_root_thread_uses_origin_listener_and_excludes_subagents(self):
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        methods = [method for method, _params in self.client.calls]
        self.assertEqual(methods[0], "initialize")
        self.assertCountEqual(methods[1:], [
            "thread/loaded/list", "thread/read", "mcpServerStatus/list",
            "thread/read", "mcpServerStatus/list",
        ])
        self.assertTrue(self.client.closed)

    def test_public_process_identity_uses_the_daemon_loaded_root(self):
        env = {"CODEX_HOME": "/fake"}
        with mock.patch.object(codex.os, "readlink", return_value="/fixture/work"), \
                mock.patch.object(procscan, "read_environ", return_value=env), \
                mock.patch.object(procscan, "codex_effective_cwd", return_value="/fixture/work"), \
                mock.patch.object(procscan, "_comm_of", return_value="codex"), \
                mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
                mock.patch.object(procscan, "invocation_mode", return_value="session"), \
                mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
                mock.patch.object(codex, "_proc_rollout", return_value=None):
            self.assertEqual(codex.session_id_of_process(PID), ROOT)

    def test_zero_or_multiple_root_threads_fail_closed(self):
        self.threads[ROOT] = (None, "user")
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))

        codex._DAEMON_ORIGINS.clear()
        self.threads[ROOT] = (41301, "user")
        self.threads[SECOND] = (41301, "user")
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))

    def test_thread_to_process_preserves_the_exact_loaded_thread_during_transition(self):
        self.threads[SECOND] = (41301, "user")
        self.assertEqual(codex.process_for_thread(SECOND, {"CODEX_HOME": "/fake"}),
                         (PID, "778899"))

    def test_loaded_list_pages_are_followed_with_schema_cursor(self):
        self.threads[SECOND] = (41301, "user")
        self.client.loaded_pages = [
            {"data": [ROOT], "nextCursor": "loaded-page-2"},
            {"data": [SECOND]},
        ]
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))
        requests = [params for method, params in self.client.calls
                    if method == "thread/loaded/list"]
        self.assertEqual(requests[0], {"limit": 100})
        self.assertEqual(requests[1], {"limit": 100, "cursor": "loaded-page-2"})

    def test_status_list_pages_are_followed_before_publishing_origin(self):
        self.client.status_pages[ROOT] = [
            {"data": [], "nextCursor": "status-page-2"},
            {"data": [{"name": "codex_tui", "httpOrigin": "http://127.0.0.1:41301",
                       "runtimeStatus": "connected"}]},
        ]
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        requests = [params for method, params in self.client.calls
                    if method == "mcpServerStatus/list" and params["threadId"] == ROOT]
        self.assertEqual(requests[0]["serverName"], "codex_tui")
        self.assertEqual(requests[1]["cursor"], "status-page-2")

    def test_repeated_cursor_fails_closed_and_clears_prior_snapshot(self):
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        self.client.loaded_pages = [
            {"data": [ROOT], "nextCursor": "loop"},
            {"data": [CHILD], "nextCursor": "loop"},
        ]
        self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_page_budget_exhaustion_fails_closed(self):
        self.client.loaded_pages = [{"data": [ROOT], "nextCursor": "more"}]
        with mock.patch.object(codex, "_DAEMON_MAX_PAGES", 1):
            self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_per_thread_read_failure_cannot_publish_partial_unique_cache(self):
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        self.threads[SECOND] = (41301, "user")
        self.client.read_fail.add(SECOND)
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_status_failure_and_unknown_source_invalidate_the_whole_snapshot(self):
        self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        self.threads[SECOND] = (41301, "user")
        self.client.status_fail.add(SECOND)
        self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

        self.client.status_fail.clear()
        self.threads[SECOND] = (41301, None)
        self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_invalid_or_nonlocal_connected_origin_makes_snapshot_unknown(self):
        self.client.status_pages[ROOT] = [{"data": [{
            "name": "codex_tui", "httpOrigin": "http://127.0.0.1:41301/unexpected",
            "runtimeStatus": "connected"}]}]
        self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

        self.client.status_pages[ROOT] = [{"data": [{
            "name": "codex_tui", "httpOrigin": "http://192.168.1.4:41301",
            "runtimeStatus": "connected"}]}]
        self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_cache_drops_unloaded_threads_and_is_keyed_by_socket_inode(self):
        first = codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        calls_before = len(self.client.calls)
        again = codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        self.assertEqual(first, again)
        self.assertEqual(len(self.client.calls) - calls_before, 2)  # initialize + loaded/list

        self.threads.pop(CHILD)
        pruned = codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        self.assertEqual(set(pruned), {ROOT})
        self.assertNotIn(CHILD, codex._DAEMON_ORIGINS[first.cache_key])

        replacement = ("/tmp/fake-codex.sock", 1, 23, os.geteuid(), 0o600)
        with mock.patch.object(codex, "_control_socket_identity", return_value=replacement):
            codex._daemon_thread_origins({"CODEX_HOME": "/fake"})
        self.assertEqual(set(codex._DAEMON_ORIGINS), {replacement})

    def test_control_socket_replacement_during_observation_is_unknown(self):
        replacement = ("/tmp/fake-codex.sock", 1, 23, os.geteuid(), 0o600)
        with mock.patch.object(codex, "_control_socket_identity",
                               side_effect=[self.socket_identity, replacement]):
            self.assertIsNone(codex._daemon_thread_origins({"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_listener_change_or_inaccessible_owner_set_is_unknown(self):
        replacement = (frozenset({"998877"}), frozenset({PID}))
        with mock.patch.object(codex, "_tcp_listener_snapshot",
                               side_effect=[self.listener, replacement]):
            self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

        with mock.patch.object(codex, "_tcp_listener_snapshot", return_value=None):
            self.assertIsNone(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_observation_and_recheck_each_share_one_fd_walk(self):
        self.threads[SECOND] = (41302, "user")
        seen = []

        def snapshot(port, scan=None):
            seen.append((port, scan))
            return self.listener if port == 41301 else (frozenset({"881133"}), frozenset({999}))

        with mock.patch.object(codex, "_tcp_listener_snapshot", side_effect=snapshot):
            self.assertEqual(codex._daemon_session_for_process(PID, {"CODEX_HOME": "/fake"}), ROOT)
        self.assertCountEqual([port for port, _scan in seen], [41301, 41302, 41301, 41302])
        observe, recheck = [scan for _port, scan in seen[:2]], [scan for _port, scan in seen[2:]]
        self.assertIs(observe[0], observe[1])
        self.assertIs(recheck[0], recheck[1])
        self.assertIsNot(observe[0], recheck[0])

    def test_multi_process_observation_shares_two_fresh_fd_walks(self):
        observation = codex.DaemonIdentityObservation()
        seen = []
        walks = []
        current_pid = [PID]

        def snapshot(port, scan=None):
            seen.append((current_pid[0], port, scan))
            if scan is not None and "owners" not in scan:
                scan["owners"] = {}
                walks.append(scan)
            inode = f"inode-{port}"
            return frozenset({inode}), frozenset({current_pid[0]})

        origins_by_pid = {
            pid: {f"thread-{pid}": (41300 + index, "user")}
            for index, pid in enumerate(range(PID, PID + 5))
        }
        with mock.patch.object(codex, "_daemon_thread_origins",
                               side_effect=lambda _env: origins_by_pid[current_pid[0]]), \
                mock.patch.object(codex, "_tcp_listener_snapshot", side_effect=snapshot), \
                mock.patch.object(codex, "_listener_unchanged",
                                 side_effect=lambda port, observed, scan=None:
                                 snapshot(port, scan) == observed), \
                mock.patch.object(codex, "_live_tui_start", return_value="778899"), \
                mock.patch.object(codex, "_daemon_endpoint_current", return_value=True), \
                mock.patch.object(procscan, "read_proc_start", return_value="778899"):
            for pid in range(PID, PID + 5):
                current_pid[0] = pid
                self.assertEqual(codex._daemon_session_for_process(
                    pid, {"CODEX_HOME": "/fake"}, observation=observation), f"thread-{pid}")

        self.assertEqual(len(seen), 10)  # 5 PIDs × initial and fresh port reads
        initial = [scan for _pid, _port, scan in seen if scan is observation.initial_scan]
        recheck = [scan for _pid, _port, scan in seen if scan is observation.recheck_scan]
        self.assertEqual(len(initial), 5)
        self.assertEqual(len(recheck), 5)
        self.assertIsNot(observation.initial_scan, observation.recheck_scan)
        self.assertEqual(walks, [observation.initial_scan, observation.recheck_scan])

    def test_tcp_table_and_port_results_are_memoized_per_fresh_phase(self):
        second_listener = (
            "   1: 0100007F:A156 00000000:0000 0A 00000000:00000000 00:00000000 "
            "00000000  1002        0 881133 1 0 100 0 0 10 0\n")
        replacement = (
            "   0: 0100007F:A155 00000000:0000 0A 00000000:00000000 00:00000000 "
            "00000000  1002        0 992211 1 0 100 0 0 10 0\n")
        tcp_header = TcpListenerSnapshotTest.TCP.splitlines()[0] + "\n"
        initial_table = TcpListenerSnapshotTest.TCP + second_listener
        recheck_table = tcp_header + replacement + second_listener
        tables = iter((initial_table, recheck_table))
        owners = {"881122": {PID}, "881133": {PID + 1}, "992211": {PID + 2}}
        observation = codex.DaemonIdentityObservation()

        def read_table(path, **_kwargs):
            self.assertEqual(path, "/proc/net/tcp")
            return io.StringIO(next(tables))

        with mock.patch.object(codex, "open", side_effect=read_table, create=True) as table_read, \
                mock.patch.object(codex, "_socket_owners_by_inode", return_value=owners) as owner_scan:
            first = _REAL_LISTENER_SNAPSHOT(41301, observation.initial_scan)
            self.assertIs(_REAL_LISTENER_SNAPSHOT(41301, observation.initial_scan), first)
            other_port = _REAL_LISTENER_SNAPSHOT(41302, observation.initial_scan)
            rechecked = _REAL_LISTENER_SNAPSHOT(41301, observation.recheck_scan)
            recheck_port = _REAL_LISTENER_SNAPSHOT(41302, observation.recheck_scan)

        self.assertEqual(first, (frozenset({"881122"}), frozenset({PID})))
        self.assertNotEqual(rechecked, first)
        self.assertEqual(other_port, (frozenset({"881133"}), frozenset({PID + 1})))
        self.assertEqual(recheck_port, other_port)
        self.assertEqual(table_read.call_count, 2)
        self.assertEqual(owner_scan.call_count, 2)
        self.assertIsNot(observation.initial_scan, observation.recheck_scan)

    def test_daemon_identity_scope_is_nested_and_does_not_survive_the_pass(self):
        with codex.daemon_identity_read_scope() as observation:
            self.assertIs(codex._DAEMON_IDENTITY_SCOPE.get(), observation)
            with codex.daemon_identity_read_scope() as nested:
                self.assertIs(nested, observation)
        self.assertIsNone(codex._DAEMON_IDENTITY_SCOPE.get())
        with codex.daemon_identity_read_scope() as next_observation:
            self.assertIsNot(next_observation, observation)

    def test_changed_listener_evidence_invalidates_shared_snapshots(self):
        observation = codex.DaemonIdentityObservation()
        old = {"owners": {"old": {PID}}}
        new = {"owners": {"new": {PID}}}
        observation.initial_scan.update(old)
        observation.recheck_scan.update(new)
        replacement = (frozenset({"replacement-inode"}), frozenset({PID}))
        with mock.patch.object(codex, "_tcp_listener_snapshot",
                               side_effect=[self.listener, replacement]), \
                mock.patch.object(procscan, "read_proc_start", return_value="778899"):
            self.assertIsNone(codex._daemon_session_for_process(
                PID, {"CODEX_HOME": "/fake"}, observation=observation))
        self.assertEqual(observation.initial_scan, {})
        self.assertEqual(observation.recheck_scan, {})

    def test_thread_to_process_revalidates_listener_and_pid_start(self):
        replacement = (frozenset({"551144"}), frozenset({PID}))
        with mock.patch.object(codex, "_tcp_listener_snapshot",
                               side_effect=[self.listener, replacement]):
            self.assertIsNone(codex.process_for_thread(ROOT, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

        with mock.patch.object(codex, "_tcp_listener_snapshot", return_value=self.listener), \
                mock.patch.object(procscan, "read_proc_start", return_value="reused-pid-start"):
            self.assertIsNone(codex.process_for_thread(ROOT, {"CODEX_HOME": "/fake"}))
        self.assertEqual(codex._DAEMON_ORIGINS, {})

    def test_direct_tui_does_not_recover_identity_from_an_old_registry_record(self):
        from fleet import session_registry
        session = SimpleNamespace(pid=PID, harness="codex", cwd="/fixture/work",
                                  session_id=None, _invocation_mode="session",
                                  app_server=False, managed_dir=None)
        tick = SimpleNamespace(default_home="/fake", proc_paths={})
        record = {"sessionId": ROOT, "procStart": "old-start", "kind": "legacy-launcher"}
        with mock.patch.object(session_registry, "read", return_value=record), \
                mock.patch.object(session_registry, "apply_to_session") as apply:
            codex.enrich(session, tick)
        self.assertIsNone(session.session_id)
        apply.assert_not_called()


class TcpListenerSnapshotTest(unittest.TestCase):
    """The /proc scan behind the daemon join, on a host with closed fd tables."""

    TCP = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
           "retrnsmt   uid  timeout inode\n"
           "   0: 0100007F:A155 00000000:0000 0A 00000000:00000000 00:00000000 "
           "00000000  1002        0 881122 1 0 100 0 0 10 0\n")

    def _scan(self, locked_error):
        def listdir(path):
            if path == "/proc":
                return ["100", "200", "300", "self"]
            if path == "/proc/100/fd":
                return ["3"]
            if path == "/proc/200/fd":
                raise locked_error
            raise AssertionError(f"unexpected listdir: {path}")

        def stat(path):
            uid = os.geteuid() + 1 if path == "/proc/300" else os.geteuid()
            return SimpleNamespace(st_uid=uid)

        with mock.patch.object(codex, "open", mock.mock_open(read_data=self.TCP), create=True), \
                mock.patch.object(codex.os, "listdir", side_effect=listdir), \
                mock.patch.object(codex.os, "stat", side_effect=stat), \
                mock.patch.object(codex.os, "readlink", return_value="socket:[881122]"):
            return codex._tcp_listener_snapshot(41301)

    def test_unreadable_same_uid_fd_table_is_skipped(self):
        # User systemd and privsep sshd always refuse /proc/<pid>/fd here.
        self.assertEqual(self._scan(PermissionError(13, "denied")),
                         (frozenset({"881122"}), frozenset({100})))

    def test_other_proc_failures_still_make_the_owner_set_unknown(self):
        self.assertIsNone(self._scan(OSError(5, "I/O error")))

    def test_ports_of_one_observation_share_one_fd_walk(self):
        tcp = self.TCP + ("   1: 0100007F:A156 00000000:0000 0A 00000000:00000000 00:00000000 "
                          "00000000  1002        0 881133 1 0 100 0 0 10 0\n")
        owners = {"881122": {100}, "881133": {200}, "990011": {300}}
        with mock.patch.object(codex, "open", mock.mock_open(read_data=tcp), create=True), \
                mock.patch.object(codex, "_socket_owners_by_inode", return_value=owners) as walk:
            scan = {}
            self.assertEqual(_REAL_LISTENER_SNAPSHOT(41301, scan),
                             (frozenset({"881122"}), frozenset({100})))
            self.assertEqual(_REAL_LISTENER_SNAPSHOT(41302, scan),
                             (frozenset({"881133"}), frozenset({200})))
            self.assertEqual(walk.call_count, 1)
            _REAL_LISTENER_SNAPSHOT(41301, {})
            _REAL_LISTENER_SNAPSHOT(41301)
            self.assertEqual(walk.call_count, 3)
        with mock.patch.object(codex, "open", mock.mock_open(read_data=tcp), create=True), \
                mock.patch.object(codex, "_socket_owners_by_inode", return_value=None):
            scan = {}
            self.assertIsNone(_REAL_LISTENER_SNAPSHOT(41301, scan))
            self.assertIsNone(_REAL_LISTENER_SNAPSHOT(41302, scan))


if __name__ == "__main__":
    unittest.main()
