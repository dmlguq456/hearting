"""Current Codex footer ids enter the existing pane projection only with live ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "utilities"))
from fleet import herdr_projection as projection  # noqa: E402
from fleet.collectors import procscan  # noqa: E402
import pane_ownership  # noqa: E402

_RUN = subprocess.run  # setUp replaces subprocess.run with the herdr double


THREAD_A = "01a133d1-5d32-7581-ac0c-c2dccaa26b4f"
THREAD_B = "01a1336e-9aa6-75e2-80fc-cca13b79b1e3"
THREAD_C = "01a133d1-5d32-7581-ac0c-c2dccaa26b4c"
TREE_ROOT = "01a12d62-4ef3-7363-8d6a-9e12a26d250f"


def screen(thread_ids, *, prompt=True, clipped=False):
    if isinstance(thread_ids, str):
        thread_ids = [thread_ids]
    shown_ids = [thread_id[:-4] + "…" if clipped else thread_id for thread_id in thread_ids]
    lines = ["Codex", "›" if prompt else "composer unavailable", "",
             "fixture-project · " + " · ".join("\x1b[2m" + sid + "\x1b[0m" for sid in shown_ids),
             "  ? for shortcuts"]
    return "\x1b[?25l" + "\r\n".join(lines) + "\r\n\x1b[?25h"


class CodexFooterBindingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-footer-binding-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = {}
        self.process_info = {}
        self.frontend_env = {}
        self.screens = {}
        self.screen_reads = []
        self.info_reads = []
        self.report_calls = []
        self.native = {}
        self.services = set()
        self.workers = set()

        self.patches = [
            mock.patch.object(pane_ownership, "_process", side_effect=self.read_process),
            mock.patch.object(pane_ownership, "_runtime", side_effect=self.runtime),
            mock.patch.object(pane_ownership, "_native_session", side_effect=self.native_session),
            mock.patch.object(pane_ownership, "_registered_worker", side_effect=self.is_worker),
            mock.patch.object(procscan, "read_proc_start", side_effect=self.proc_start),
            mock.patch.object(pane_ownership.subprocess, "run", side_effect=self.herdr),
            mock.patch.object(pane_ownership.shutil, "which", return_value="/fake/herdr"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        env_patch = mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "HERDR_SOCKET_PATH": ""})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def add_frontend(self, pid, pane, thread_id, *, group=None, tty=None, start=None,
                     socket_path=None):
        group = group or pid
        tty = tty or pid + 10000
        start = start or f"start-{pid}"
        self.records[pid] = (pid, 1, group, tty, group, start)
        self.process_info[pane] = {
            "pane_id": pane,
            "foreground_process_group_id": group,
            "foreground_processes": [{"pid": pid}],
        }
        env = {"HERDR_PANE_ID": pane}
        if socket_path:
            env["HERDR_SOCKET_PATH"] = socket_path
            self.process_info[(socket_path, pane)] = self.process_info[pane]
        self.frontend_env[pid] = env
        self.native[pid] = thread_id
        self.screens[pane] = [screen(thread_id), screen(thread_id)]
        return self.records[pid]

    def proc_start(self, pid):
        record = self.records.get(int(pid))
        return record[-1] if record else None

    def add_service(self, pid, pane):
        self.records[pid] = (pid, 1, pid, 0, -1, f"server-{pid}")
        self.services.add(pid)
        self.frontend_env[pid] = {"HERDR_PANE_ID": pane}

    def read_process(self, pid):
        try:
            return self.records.get(int(pid))
        except (TypeError, ValueError):
            return None

    def runtime(self, pid):
        return ("codex", int(pid) in self.services) if int(pid) in self.records else ("", False)

    def native_session(self, pid, harness, *, codex_observation=None):
        return self.native.get(int(pid), "") if harness == "codex" else ""

    def is_worker(self, pid):
        return int(pid) in self.workers

    def herdr(self, command, **kwargs):
        env = kwargs.get("env")
        env = os.environ if env is None else env
        if "process-info" in command:
            pane = command[command.index("--pane") + 1]
            self.info_reads.append((pane, dict(env)))
            socket_path = env.get("HERDR_SOCKET_PATH")
            info = self.process_info.get((socket_path, pane), self.process_info.get(pane, {}))
            result = {"result": {"process_info": info}}
            return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
        if len(command) > 3 and command[1:3] == ["agent", "read"]:
            pane = command[3]
            self.screen_reads.append((pane, None if kwargs.get("env") is None else dict(env)))
            queue = self.screens.get(pane, [""])
            output = queue.pop(0) if len(queue) > 1 else queue[0]
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[1:3] in (["pane", "report-agent-session"], ["pane", "report-metadata"]):
            self.report_calls.append((command, dict(kwargs.get("env") or os.environ)))
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected Herdr call: {command!r}")

    def scan_panes(self, pids):
        return (mock.patch.object(projection.os, "listdir", return_value=[str(pid) for pid in pids]),
                mock.patch.object(projection, "_comm", return_value="codex"),
                mock.patch.object(procscan, "read_environ",
                                  side_effect=lambda pid: self.frontend_env.get(int(pid), {})))

    def test_parser_requires_prompt_and_complete_bounded_uuid(self):
        hint = "  ? for shortcuts"
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["activity contains " + THREAD_A, "› Ask Codex", "", "project · telemetry · " + THREAD_A, hint],
            current_id_only=True), {THREAD_A})
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", THREAD_A[:-3] + "…", hint], current_id_only=True), set())
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", THREAD_A + " suffix", hint], current_id_only=True), set())
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", THREAD_A + " | " + THREAD_B, hint], current_id_only=True), set())
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", THREAD_A + " · telemetry", hint], current_id_only=True), set())
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", THREAD_A + " · " + THREAD_B, hint], current_id_only=True), {THREAD_B})
        self.assertEqual(pane_ownership.codex_footer_threads(
            ["› Ask Codex", "", "Where should the new conversation run?", "❯ Use current Git worktree", hint],
            current_id_only=True), set())
        self.assertEqual(pane_ownership.codex_footer_threads(["activity " + THREAD_A], current_id_only=True), set())

    def test_current_thread_from_normal_callback_drives_the_deferred_report(self):
        for index, (source, thread_id) in enumerate((("startup", THREAD_A),
                                                      ("resume", THREAD_B),
                                                      ("fork", THREAD_C))):
            with self.subTest(source=source):
                pid = 41000 + index
                pane = f"wB:p{index}"
                self.add_frontend(pid, pane, thread_id, socket_path=f"/isolated/{index}.sock")
                self.report_calls.clear()
                scan = self.scan_panes([pid])
                with scan[0], scan[1], scan[2], \
                        mock.patch.object(projection.shutil, "which", return_value="/fake/herdr"), \
                        mock.patch.object(projection, "_in_shared_codex_service", return_value=True), \
                        mock.patch.object(projection, "_defer_until_proven",
                                          side_effect=lambda sid, include, **lifecycle:
                                          projection._await_tui_and_project(sid, include, **lifecycle)), \
                        mock.patch.object(projection, "session_title", return_value="fixture title"), \
                        mock.patch.object(projection, "_formatter_overrides", return_value=(None, None)), \
                        mock.patch.object(projection, "compose", return_value=("[46] codex", "fixture title")), \
                        mock.patch.object(projection, "header_title", return_value="fixture header"), \
                        mock.patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                                     "XDG_STATE_HOME": str(self.root / "state"),
                                                     "HERDR_PANE_ID": "stale:pane",
                                                     "CODEX_THREAD_ID": thread_id,
                                                     "HERDR_SOCKET_PATH": "/inherited-service.sock"}, clear=True), \
                        mock.patch("tempfile.gettempdir", return_value=str(self.root)):
                    projection.project("codex", thread_id)
                    self.assertEqual(os.environ["HERDR_SOCKET_PATH"], "/inherited-service.sock")
                reports = [command for command, _env in self.report_calls]
                self.assertEqual(len(reports), 2)
                self.assertTrue(all(env.get("HERDR_SOCKET_PATH") == f"/isolated/{index}.sock"
                                    for _command, env in self.report_calls))
                self.assertEqual(reports[0][1:4], ["pane", "report-agent-session", pane])
                self.assertEqual(reports[0][reports[0].index("--agent-session-id") + 1], thread_id)
                self.assertEqual(reports[1][1:4], ["pane", "report-metadata", pane])
                self.assertTrue(any(read_env and read_env.get("HERDR_SOCKET_PATH") == f"/isolated/{index}.sock"
                                    and "HERDR_PANE_ID" not in read_env
                                    for _target, read_env in self.screen_reads if _target == pane))

    def run_deferred_report_with_endpoints(self, endpoints):
        sent = []

        def record_report(command, _observation):
            sent.append((command, os.environ.get("HERDR_SOCKET_PATH")))

        with mock.patch.object(pane_ownership, "codex_tui_pane_endpoint",
                               side_effect=endpoints), \
                mock.patch.object(projection.shutil, "which", return_value="/fake/herdr"), \
                mock.patch.object(projection, "_send_projection", side_effect=record_report), \
                mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            projection._await_tui_and_project(THREAD_A, True)
        return sent

    def test_deferred_reporter_script_reaches_its_proof_and_report_imports(self):
        # The detached reporter runs this module as a script: its path holds
        # tools/fleet, never the hook's utilities directory, and it has no package.
        probe = ("import sys\n"
                 f"sys.path.insert(0, {str(ROOT / 'tools' / 'fleet')!r})\n"
                 "import herdr_projection\n"
                 "herdr_projection._codex_tui_endpoint('not-a-thread')\n"
                 "print('pane_ownership' in sys.modules)\n"
                 "print(herdr_projection._session_lifecycle('codex', None, None))\n")
        done = _RUN([sys.executable, "-I", "-c", probe], capture_output=True,
                    text=True, timeout=60, check=False)
        self.assertEqual(done.stdout.split(), ["True", "{}"], done.stderr)

    def test_deferred_report_proof_rejects_socket_switch_before_session_report(self):
        pane = "wC:p7"
        sent = self.run_deferred_report_with_endpoints([
            (pane, "/endpoint/A.sock"),
            (pane, "/endpoint/B.sock"),
        ])
        self.assertEqual(sent, [])

    def test_deferred_report_proof_rejects_socket_switch_before_metadata_report(self):
        pane = "wC:p7"
        sent = self.run_deferred_report_with_endpoints([
            (pane, "/endpoint/A.sock"),
            (pane, "/endpoint/A.sock"),
            (pane, "/endpoint/B.sock"),
        ])
        self.assertEqual(len(sent), 1)
        command, socket_path = sent[0]
        self.assertEqual(command[1:4], ["pane", "report-agent-session", pane])
        self.assertEqual(socket_path, "/endpoint/A.sock")

    def test_deferred_report_proof_allows_a_stable_pane_socket_pair(self):
        pane = "wC:p7"
        sent = self.run_deferred_report_with_endpoints([
            (pane, "/endpoint/A.sock"),
            (pane, "/endpoint/A.sock"),
            (pane, "/endpoint/A.sock"),
        ])
        self.assertEqual(len(sent), 2)
        self.assertEqual([item[0][2] for item in sent],
                         ["report-agent-session", "report-metadata"])
        self.assertTrue(all(socket_path == "/endpoint/A.sock" for _, socket_path in sent))

    def test_deferred_proof_helper_skips_only_known_incapable_frontend_config(self):
        for feasibility, should_spawn in (("incapable", False), ("capable", True), ("unknown", True)):
            with self.subTest(feasibility=feasibility), \
                 mock.patch.object(projection, "_nearest_codex_pid", return_value=44001), \
                 mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
                 mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
                 mock.patch.object(procscan, "read_environ", return_value={"CODEX_HOME": str(self.root)}), \
                 mock.patch.object(projection, "_codex_status_line_feasibility", return_value=feasibility), \
                 mock.patch.object(projection.subprocess, "Popen") as spawn:
                projection._defer_until_proven(
                    THREAD_A, True, session_seq=123, session_start_source="resume")
                self.assertEqual(spawn.called, should_spawn)
                if should_spawn:
                    args, kwargs = spawn.call_args
                    command = args[0]
                    self.assertEqual(command[command.index("--session-id") + 1], THREAD_A)
                    self.assertNotIn("--no-report-session", command)
                    self.assertEqual(command[command.index("--seq") + 1], "123")
                    self.assertEqual(command[command.index("--session-start-source") + 1], "resume")
                    self.assertTrue(kwargs["start_new_session"])

    def test_shared_daemon_projection_defers_real_session_report_with_lifecycle(self):
        with mock.patch.object(projection.shutil, "which", return_value="/fake/herdr"), \
             mock.patch.object(projection, "_in_shared_codex_service", return_value=True), \
             mock.patch.object(projection, "_defer_until_proven") as defer, \
             mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "HERDR_PANE_ID": "",
                                           "HERDR_SOCKET_PATH": ""}, clear=True):
            projection.project("codex", THREAD_A, report_session=True,
                               session_seq=123456, session_start_source="resume")
        defer.assert_called_once_with(THREAD_A, True, session_seq=123456,
                                     session_start_source="resume")

    def test_deferred_proof_feasibility_reads_the_frontend_home_and_retained_overrides(self):
        home = self.root / "frontend-codex-home"
        home.mkdir()
        config = home / "config.toml"
        config.write_text('[tui]\nstatus_line = ["custom"]\n', encoding="utf-8")
        with mock.patch.object(projection, "_nearest_codex_pid", return_value=44002), \
             mock.patch.object(procscan, "is_shared_codex_daemon", return_value=False), \
             mock.patch.object(procscan, "_read_argv", return_value=["codex"]), \
             mock.patch.object(procscan, "read_environ", return_value={"CODEX_HOME": str(home)}), \
             mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "masked-home")}):
            self.assertEqual(projection._codex_status_line_feasibility(), "incapable")
            config.write_text('[tui]\nstatus_line = ["custom", "thread-id"]\n', encoding="utf-8")
            self.assertEqual(projection._codex_status_line_feasibility(), "capable")
            with mock.patch.object(procscan, "_read_argv",
                                   return_value=["codex", "--profile", "custom"]):
                self.assertEqual(projection._codex_status_line_feasibility(), "unknown")

    def test_two_same_cwd_frontends_are_selected_by_their_current_footer(self):
        self.add_frontend(42001, "wA:p1", THREAD_A)
        self.add_frontend(42002, "wA:p2", THREAD_B)
        self.frontend_env[42001]["PWD"] = "/fixture/shared-cwd"
        self.frontend_env[42002]["PWD"] = "/fixture/shared-cwd"
        scan = self.scan_panes([42001, 42002])
        with scan[0], scan[1], scan[2]:
            self.assertEqual(projection._codex_tui_pane(THREAD_B), "wA:p2")
            self.screens["wA:p1"] = [screen(THREAD_A), screen(THREAD_A)]
            self.screens["wA:p2"] = [screen(THREAD_B), screen(THREAD_B)]
            self.assertIsNone(projection._codex_tui_pane(TREE_ROOT))

    def test_duplicate_selection_in_two_panes_remains_ambiguous(self):
        self.add_frontend(42101, "wA:p1", THREAD_A)
        self.add_frontend(42102, "wA:p2", THREAD_A)
        scan = self.scan_panes([42101, 42102])
        with scan[0], scan[1], scan[2]:
            self.assertIsNone(projection._codex_tui_pane(THREAD_A))

    def test_same_pane_id_on_different_sockets_is_not_one_unique_endpoint(self):
        self.add_frontend(42111, "wA:p1", THREAD_A, socket_path="/isolated/first.sock")
        self.add_frontend(42112, "wA:p1", THREAD_A, socket_path="/isolated/second.sock")
        scan = self.scan_panes([42111, 42112])
        with scan[0], scan[1], scan[2]:
            self.assertIsNone(projection._codex_tui_pane(THREAD_A))

    def test_hidden_footer_uses_process_identity_and_a_visible_difference_refuses(self):
        pid, pane = 42121, "wA:ptitle"
        self.add_frontend(pid, pane, THREAD_A)
        self.screens[pane] = ["Codex\n› Ask Codex\n\nfixture-project · " + THREAD_A + " · Fix it"
                              + "\n  ? for shortcuts"] * 2
        scan = self.scan_panes([pid])
        with scan[0], scan[1], scan[2]:
            self.assertEqual(pane_ownership.verified_pane(
                pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), pane)
            self.screens[pane] = ["Codex\n› Ask Codex\n\nfixture-project · " + THREAD_A + " · " + THREAD_B
                                  + "\n  ? for shortcuts"] * 2
            self.assertEqual(pane_ownership.verified_pane(
                pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")
            self.assertEqual(pane_ownership.verified_pane(
                pane, "codex", THREAD_B, pid=pid, executable="/fake/herdr"), "")

    def test_frontend_default_socket_does_not_inherit_service_socket(self):
        pid, pane = 42131, "wA:pdefault"
        self.add_frontend(pid, pane, THREAD_A)
        scan = self.scan_panes([pid])
        with scan[0], scan[1], scan[2], \
                mock.patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/wrong-service.sock"}, clear=True):
            self.assertEqual(projection._codex_tui_pane(THREAD_A), pane)
            self.assertIsNone((self.info_reads[-1][1] or {}).get("HERDR_SOCKET_PATH"))
            self.assertEqual(os.environ["HERDR_SOCKET_PATH"], "/wrong-service.sock")

    def test_same_frontend_current_selection_is_read_again_on_the_next_callback(self):
        pid, pane = 42151, "wA:pcurrent"
        self.add_frontend(pid, pane, THREAD_A)
        scan = self.scan_panes([pid])
        with scan[0], scan[1], scan[2]:
            self.assertEqual(projection._codex_tui_pane(THREAD_A), pane)
            self.native[pid] = THREAD_B
            self.screens[pane] = [screen(THREAD_B), screen(THREAD_B)]
            self.assertEqual(projection._codex_tui_pane(THREAD_B), pane)

    def test_stale_inherited_pane_hint_cannot_claim_another_foreground_group(self):
        pid, pane = 42161, "wB:p-old"
        self.add_frontend(pid, pane, THREAD_A)
        other_pid, other_group = 42162, 42162
        self.records[other_pid] = (other_pid, 1, other_group, 52162, other_group, "other-start")
        self.process_info["wB:p-old"] = {
            "pane_id": "wB:p-old",
            "foreground_process_group_id": other_group,
            "foreground_processes": [{"pid": other_pid}],
        }
        self.frontend_env[pid]["HERDR_PANE_ID"] = "wB:p-old"
        scan = self.scan_panes([pid])
        with scan[0], scan[1], scan[2]:
            self.assertIsNone(projection._codex_tui_pane(THREAD_A))

    def test_shared_server_is_never_the_foreground_owner(self):
        self.add_frontend(42202, "wB:p9", THREAD_A)
        self.add_service(42201, "wB:p9")
        scan = self.scan_panes([42201, 42202])
        with scan[0], scan[1], scan[2]:
            self.assertEqual(projection._codex_tui_pane(THREAD_A), "wB:p9")

    def test_shared_caller_keeps_sender_thread_but_dual_root_has_no_pane(self):
        owner, pane, service = 42211, "wB:p-loader", 42212
        self.add_frontend(owner, pane, THREAD_B)
        self.add_service(service, "stale:pane")
        self.native[owner] = ""  # process_identity is ambiguous across loaded root threads
        self.screens[pane] = ["Codex\n› Ask Codex\n\nproject · Context 80% used\n  ? for shortcuts"] * 2
        env = {"CODEX_THREAD_ID": THREAD_B}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pane_ownership.os, "listdir",
                                  return_value=[str(owner), str(service)]), \
                mock.patch.object(procscan, "read_environ",
                                  side_effect=lambda pid: self.frontend_env.get(int(pid), {})), \
                mock.patch.object(pane_ownership, "_caller_runtime",
                                  return_value=(self.records[service], True)):
            self.assertEqual(pane_ownership.current_codex_thread_for_caller("stale:pane"), THREAD_B)
            self.assertEqual(pane_ownership.verified_pane(
                "stale:pane", "codex", THREAD_B, pid=service, executable="/fake/herdr"), "")
            self.assertEqual(pane_ownership.verified_pane(
                "", "codex", THREAD_B, pid=service, executable="/fake/herdr"), "")
            self.screens[pane] = [screen(THREAD_A), screen(THREAD_A)]
            self.assertEqual(pane_ownership.verified_pane(
                "stale:pane", "codex", THREAD_B, pid=service, executable="/fake/herdr"), "")

    def test_absent_footer_uses_identity_but_visible_conflicting_footer_refuses(self):
        cases = (
            screen(THREAD_A, prompt=False),
            screen(THREAD_A, clipped=True),
            "Codex\n›\n" + THREAD_A + " " + THREAD_B,
            screen(TREE_ROOT),
        )
        for index, content in enumerate(cases):
            with self.subTest(index=index):
                pid, pane = 42300 + index, f"wB:p{index}"
                self.add_frontend(pid, pane, THREAD_A)
                self.screens[pane] = [content, content]
                scan = self.scan_panes([pid])
                with scan[0], scan[1], scan[2], \
                        mock.patch.object(pane_ownership, "_registered_worker", return_value=False):
                    expected = "" if index == 3 else pane
                    self.assertEqual(pane_ownership.verified_pane(
                        pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), expected)

    def test_visible_footer_disagreement_rejects_process_identity(self):
        pid, pane = 42401, "wB:pconflict"
        self.add_frontend(pid, pane, THREAD_A)
        self.native[pid] = TREE_ROOT
        self.assertEqual(pane_ownership.verified_pane(
            pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")

    def test_stale_process_start_and_changed_current_selection_refuse(self):
        pid, pane = 42501, "wB:pstale"
        original = self.add_frontend(pid, pane, THREAD_A)
        changed = (*original[:-1], "reused-pid-start")

        def change_process(*_args):
            self.records[pid] = changed

        with mock.patch.object(pane_ownership.time, "sleep", side_effect=change_process):
            self.assertEqual(pane_ownership.verified_pane(
                pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")

        pid, pane = 42502, "wB:pselection"
        self.add_frontend(pid, pane, THREAD_A)
        self.screens[pane] = [screen(THREAD_A), screen(THREAD_B)]
        self.assertEqual(pane_ownership.verified_pane(
            pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")

    def test_registered_worker_and_inherited_environment_are_not_selection_proof(self):
        pid, pane = 42601, "wB:pworker"
        self.add_frontend(pid, pane, THREAD_A)
        self.workers.add(pid)
        self.assertEqual(pane_ownership.verified_pane(
            pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")
        self.assertEqual(self.screen_reads, [])
        self.native[pid] = THREAD_A
        self.assertEqual(pane_ownership.verified_pane(
            pane, "codex", THREAD_A, pid=pid, executable="/fake/herdr"), "")
        self.native.pop(pid)

        self.workers.clear()
        self.screens[pane] = [screen(TREE_ROOT), screen(TREE_ROOT)]
        report = mock.Mock()
        scan = self.scan_panes([pid])

        def observe_once(sid, include_session, **lifecycle):
            selected_pane = projection._codex_tui_pane(sid)
            if selected_pane:
                projection._report("codex", sid, selected_pane, include_session, **lifecycle)

        with scan[0], scan[1], scan[2], \
                mock.patch.object(projection.shutil, "which", return_value="/fake/herdr"), \
                mock.patch.object(projection, "_in_shared_codex_service", return_value=True), \
                mock.patch.object(projection, "_defer_until_proven", side_effect=observe_once), \
                mock.patch.object(projection, "_report", report), \
                mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane, "CODEX_THREAD_ID": THREAD_A,
                                             "HERDR_SOCKET_PATH": "/stale.sock"}, clear=True), \
                mock.patch("tempfile.gettempdir", return_value=str(self.root)):
            projection.project("codex", THREAD_A)
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
