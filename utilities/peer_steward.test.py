#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_steward", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)
sys.path.insert(0, str(_HERE.parent / "tools"))
import fixture_processes  # noqa: E402


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
        # `steward_marker_roots()`'s fleet-reader branch also adds
        # `stable_state_root(os.environ)` and every installed runtime's own root
        # (~/.codex, ~/.claude, ~/.config/opencode) as read candidates; without
        # isolating HOME too those fall through to this machine's real roots.
        os.environ["HOME"] = str(self.tmp_root / "home")
        os.environ.pop("XDG_STATE_HOME", None)
        os.environ.pop("HARNESS_STATE_ROOT", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("AGENT_HOME", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGENT_SESSION_ID", None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _all_records(self):
        root = peer_steward.peer_message.peer_state_root() / "peer-messages"
        if not root.is_dir():
            return []
        recs = []
        for month in root.glob("*"):
            for f in month.glob("*.jsonl"):
                for line in f.read_text().splitlines():
                    if line.strip():
                        recs.append(json.loads(line))
        return recs


def _herdr_json(payload):
    """Measured herdr 0.8.0 shape: success -> stdout + exit 0; an `error` payload
    -> STDERR + exit 1.  The fixture must reproduce the split, or a stdout-only
    reader passes every test and still misclassifies every real timeout."""
    if isinstance(payload, dict) and "error" in payload:
        return subprocess.CompletedProcess(["herdr"], 1, stdout="", stderr=json.dumps(payload))
    return subprocess.CompletedProcess(["herdr"], 0, stdout=json.dumps(payload), stderr="")


def _idle_shell_run(argv, **kwargs):
    if argv[:3] == ["herdr", "pane", "process-info"]:
        return _herdr_json({"result": {"process_info": {
            "pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101,
            "foreground_processes": [{"pid": 101, "argv": ["zsh"]}],
        }}})
    return _herdr_json({"result": {"pane": {}}})


class WaitTest(_TmpRootMixin, unittest.TestCase):
    def _wait(self, target="fleet-cycle2", until=None, timeout=None, ref=None):
        argv = ["wait", target]
        for u in until or []:
            argv += ["--until", u]
        if timeout is not None:
            argv += ["--timeout", str(timeout)]
        for r in ref or []:
            argv += ["--ref", r]
        return peer_steward.main(argv)

    def test_idle_target_returns_zero_with_five_typed_fields(self):
        payload = {
            "result": {
                "agent": {
                    "agent": "claude", "agent_session": {"value": "sid-123"},
                    "agent_status": "idle", "name": "fleet-cycle2", "pane_id": "w1:pM",
                },
                "type": "agent_info",
            }
        }
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run_mock:
            rc = self._wait()
        self.assertEqual(rc, 0)
        # F-100c: one `agent get` resolves the target's session id for the ledger record;
        # the WAIT itself is still exactly one herdr call — no poll loop.
        waits = [c[0][0] for c in run_mock.call_args_list if c[0][0][:3] == ["herdr", "agent", "wait"]]
        self.assertEqual(len(waits), 1)
        self.assertEqual(run_mock.call_args[0][0][:3], ["herdr", "agent", "wait"])

    def test_unrecognized_agent_status_is_normalized_to_unknown(self):
        payload = {
            "result": {
                "agent": {
                    "agent": "claude", "agent_session": {"value": "sid-123"},
                    "agent_status": "busy", "name": "fleet-cycle2", "pane_id": "w1:pM",
                },
                "type": "agent_info",
            }
        }
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 0)
        line = print_mock.call_args[0][0]
        self.assertIn("state=unknown", line)

    def test_timeout_exit_3(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 3)

    def test_agent_not_found_exit_2(self):
        payload = {"error": {"code": "agent_not_found"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 2)

    def test_wait_on_a_mistyped_target_records_but_never_marks(self):
        """Review round 1 #2: the old code marked before herdr answered, so one typo was a
        permanent bold-yellow badge over an empty steward strip."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-innocent"
        payload = {"error": {"code": "agent_not_found"}}
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            self.assertEqual(self._wait(target="no-such-target"), 2)
        self.assertEqual(len(self._all_records()), 1)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})
        self.assertFalse(peer_steward.peer_message.steward_marker_path("claude", "sid-innocent").exists())

    def test_wait_marks_the_caller_only_after_a_real_target_answered(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(_agent_json("codex", "thread-1", "fleet-cycle2"))):
            self.assertEqual(self._wait(), 0)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertEqual(set(markers), {("claude", "sid-steward")})
        entry = markers[("claude", "sid-steward")]["targets"]["thread-1"]
        self.assertEqual((entry["harness"], entry["session_id"], entry["kind"], entry["source"]),
                         ("codex", "thread-1", "watch", "watch"))
        # a timeout still names a real target (herdr resolved it), so it counts
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward-2"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json({"error": {"code": "timeout"}})):
            self.assertEqual(self._wait(), 3)
        self.assertIn(("claude", "sid-steward-2"), peer_steward.peer_message.read_steward_markers())
        # herdr missing: record, no flag
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward-3"
        with mock.patch("builtins.print"), mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self.assertEqual(self._wait(), 4)
        self.assertNotIn(("claude", "sid-steward-3"), peer_steward.peer_message.read_steward_markers())

    def test_herdr_binary_missing_is_herdr_unavailable_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_json_parse_failure_is_herdr_unavailable_exit_4(self):
        bad = subprocess.CompletedProcess(["herdr"], 0, stdout="not json", stderr="")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=bad):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_non_timeout_non_agent_not_found_error_code_is_herdr_unavailable(self):
        payload = {"error": {"code": "some-other-protocol-error"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            rc = self._wait()
        self.assertEqual(rc, 4)

    def test_subprocess_run_called_exactly_once_no_poll_loop(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run_mock:
            self._wait()
        waits = [c[0][0] for c in run_mock.call_args_list if c[0][0][:3] == ["herdr", "agent", "wait"]]
        self.assertEqual(len(waits), 1)                      # F-100c: + one `agent get`, never a loop

    def test_one_watch_record_written_at_wait_start_body_empty(self):
        payload = {"error": {"code": "timeout"}}
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)):
            self._wait(target="peer-a", ref=["rt-abc"])
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["kind"], "watch")
        self.assertEqual(rec["delivery"]["surface"], "herdr")
        self.assertEqual(rec["to"]["name"], "peer-a")
        self.assertEqual(rec["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(rec["refs"], ["rt-abc"])

    def test_watch_record_written_even_when_herdr_missing(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait(target="peer-b")
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "watch")

    def test_herdr_unavailable_names_a_fallback(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("herdr-unavailable", line)
        self.assertIn("fallback=", line)

    def test_claude_session_env_selects_claude_native_notify_idle_fallback(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-claude"
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("fallback=claude-native-notify-idle", line)

    def test_no_claude_session_env_selects_poll_fallback(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self._wait()
        line = print_mock.call_args[0][0]
        self.assertIn("fallback=poll-fallback", line)


def _agent_start_cmd(run_mock):
    """The `herdr agent start …` argv among every herdr call the wrapper makes.

    `start` now also asks herdr about the pane and re-establishes the launcher ingress in
    it, so "the last call" is no longer the launch. Selecting by what the call IS keeps
    these assertions about the launch instead of about call ordering.
    """
    for call in run_mock.call_args_list:
        argv = call[0][0]
        if isinstance(argv, list) and argv[:3] == ["herdr", "agent", "start"]:
            return argv
    raise AssertionError("no `herdr agent start` call: %r"
                         % [c[0][0] for c in run_mock.call_args_list])


class StartTest(_TmpRootMixin, unittest.TestCase):
    def _start(self, name="peer-c", kind="claude", pane="w1:pM", permission_mode=None, agent_args=None):
        argv = ["start", name, "--kind", kind, "--pane", pane]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if agent_args:
            argv += ["--"] + list(agent_args)
        return peer_steward.main(argv)

    def _ingress(self, kind="codex"):
        """A real wrapper on disk, so the existence check has something to find."""
        bindir = self.tmp_root / "codex-home" / ".harness" / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        wrapper = bindir / kind
        wrapper.write_text("#!/bin/sh\nexec true\n")
        wrapper.chmod(0o755)
        os.environ["CODEX_HOME"] = str(self.tmp_root / "codex-home")
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        return str(bindir)

    def test_the_launcher_ingress_is_re_established_in_the_pane_first(self):
        """Only hearting's wrapper reaches the managed entry, and only the managed entry
        writes the session record — so a pane whose PATH misses the wrapper produces a
        session with no identity anywhere.

        A shell reads its startup files once. Measured 2026-09-10: `command -v codex` in a
        pane whose shell started 2026-08-24 — a week before the ingress was installed —
        answered with the vendor binary, while a pane opened 2026-09-09 answered with the
        wrapper. Nothing can reach the old pane afterwards; the profile cannot touch a
        running process. The launch can.
        """
        bindir = self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        sends = [a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]]
        self.assertEqual(len(sends), 1, argvs)
        self.assertIn(bindir, sends[0][-1])
        self.assertTrue(sends[0][-1].startswith("export PATH="))
        # ... and it has to be typed BEFORE the launch, or the launch resolves the old PATH.
        self.assertLess(argvs.index(sends[0]),
                        argvs.index(_agent_start_cmd(run_mock)))
        self.assertIn(["herdr", "pane", "send-keys", "w1:pM", "Enter"], argvs)

    def test_a_pane_already_running_an_agent_is_never_typed_into(self):
        # Text sent to an occupied pane lands in that agent's prompt, not a shell.
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json({"result": {"pane": {"agent": "codex"}}})) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])

    def test_a_harness_with_no_wrapper_gets_nothing_typed_on_its_behalf(self):
        # Claude and OpenCode have no launcher wrapper; there is no PATH to fix.
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "claude", "--pane", "w1:pM"])
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "get"]], [])

    def test_an_uninstalled_wrapper_is_not_put_on_anyones_path(self):
        os.environ["CODEX_HOME"] = str(self.tmp_root / "no-such-home")
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        self.assertIsNone(peer_steward._managed_ingress_dir("codex"))

    def test_the_receipt_says_whether_the_session_came_up_managed(self):
        import io
        import contextlib
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("managed=false", out.getvalue())

    def test_a_managed_launch_says_so(self):
        import io
        import contextlib
        self._ingress()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("managed=true", out.getvalue())

    def test_the_managed_check_reads_the_entry_process_not_only_its_children(self):
        """`codex-managed-entry.py` sets `AGENT_CODEX_MANAGED_GATEWAY` for the app-server
        and TUI client it spawns, NOT for itself. Looking only for that variable on the
        pane's foreground process reported `managed=false` for a launch that really was
        managed — caught end-to-end 2026-09-10, after the unit tests were already green."""
        info = {"result": {"process_info": {"foreground_processes": [
            {"pid": 1, "argv": ["/usr/bin/python3",
                                "/x/utilities/codex-managed-entry.py", "--codex", "/y/codex"]}]}}}
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(info)):
            self.assertIs(peer_steward._pane_is_managed("w1:pM"), True)

    def test_a_plain_vendor_process_is_reported_unmanaged(self):
        info = {"result": {"process_info": {"foreground_processes": [
            {"pid": 999999999, "argv": ["codex", "--dangerously-bypass-approvals-and-sandbox"]}]}}}
        with mock.patch.object(peer_steward.subprocess, "run",
                               return_value=_herdr_json(info)):
            self.assertIs(peer_steward._pane_is_managed("w1:pM"), False)

    def test_an_unreadable_pane_is_unknown_not_a_verdict(self):
        # Claiming "unmanaged" because herdr did not answer would be a guess wearing a
        # receipt's clothes.
        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=OSError("herdr gone")):
            self.assertIsNone(peer_steward._pane_is_managed("w1:pM"))

    def test_cwd_reaches_the_agent_that_can_take_one(self):
        """`herdr agent start` has no cwd option, so the launched agent inherits the
        PANE's directory. `--cwd` used to be passed only to this CLI process: a session
        started with `--cwd <hearting>` came up in `SR_CorrNet` (measured 2026-09-10)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--cd", cmd)
        self.assertIn(os.path.realpath(str(self.tmp_root)), cmd)

    def test_claude_cwd_is_quoted_and_applied_in_the_idle_pane_before_start(self):
        import shlex
        def fake_run(argv, **kwargs):
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {}}})
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                self.assertEqual(argv[argv.index("--timeout") + 1], "15000")
                self.assertEqual(kwargs["timeout"], 16)
                time.sleep(0.025)  # later than the old erroneous 15 ms budget
                return _herdr_json({"result": {"pane": {}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _idle_shell_run(argv, **kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=fake_run) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "claude",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        send = next(argv for argv in argvs if argv[:3] == ["herdr", "pane", "send-text"])
        self.assertEqual(send[-1], "cd -- " + shlex.quote(os.path.realpath(str(self.tmp_root))))
        ready = next(argv for argv in argvs if argv[:3] == ["herdr", "pane", "wait-output"])
        self.assertLess(argvs.index(ready), argvs.index(send))
        self.assertLess(argvs.index(send), argvs.index(_agent_start_cmd(run_mock)))

    def test_shell_prompt_glyphs_and_past_prompt_are_distinct(self):
        screens = [
            (" Uihyeop@moving4  /home/nas/user/Uihyeop/NN_Zoo/BC_ResNet  ↱ main ± ", True),
            ("host ~/project $ ", True), ("#\n\n", True), ("% ", True), ("❯ ", True),
            ("host $\nrunning command\n", False),
            ("host \nDownloading 42%\n", False),
            ("host $ echo hi", False),
            ("host $\nTrust this folder?\n1. Yes\n2. No", False),
        ]
        for screen, expected in screens:
            with self.subTest(screen=screen):
                def wait(argv, **kwargs):
                    self.assertEqual(argv[:5], ["herdr", "pane", "wait-output", "w1:pM", "--regex"])
                    matched = re.search(argv[5], screen) is not None
                    return subprocess.CompletedProcess(argv, 0 if matched else 1, "", "")
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=wait):
                    self.assertEqual(peer_steward._wait_for_shell_prompt("w1:pM"), expected)

    def test_shell_wait_keeps_explicit_milliseconds_and_default_seconds(self):
        for milliseconds, expected_ms, expected_seconds in [(None, "15000", 16), (2750, "2750", 3.75), (0, "0", 1)]:
            with self.subTest(milliseconds=milliseconds), \
                 mock.patch.object(peer_steward, "_herdr_get_timeout", return_value=15), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                self.assertTrue(peer_steward._wait_for_shell_prompt("w1:pM", milliseconds))
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index("--timeout") + 1], expected_ms)
                self.assertEqual(run.call_args.kwargs["timeout"], expected_seconds)

    def test_old_prompt_busy_form_and_trust_receive_no_bootstrap_or_start(self):
        for screen, reason in [
            ("host $\ncommand still running", "pane-busy"),
            ("host \nSelect an option\n1. Continue\n2. Cancel", "pane-busy"),
            ("host $\nDo you trust the contents of this folder?", "native-trust-wait"),
        ]:
            with self.subTest(screen=screen):
                calls = []
                def busy(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        return _herdr_json({"result": {"process_info": {
                            "pane_id": "w1:pM", "shell_pid": 101,
                            "foreground_process_group_id": 202,
                            "foreground_processes": [{"pid": 202, "argv": ["busy"]}],
                        }}})
                    if argv[:3] == ["herdr", "agent", "read"]:
                        return subprocess.CompletedProcess(argv, 0, screen, "")
                    # Even a stale successful wait receipt must not authorize input.
                    return _herdr_json({"result": {"pane": {}}})
                with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run", side_effect=busy), \
                     mock.patch("builtins.print") as output:
                    rc = peer_steward.main(["start", "peer-c", "--kind", "claude", "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
                self.assertEqual(rc, 1)
                self.assertIn("reason=" + reason, output.call_args.args[0])
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"], ["herdr", "agent", "start"]) for a in calls))

    def test_occupancy_changed_during_wait_does_not_receive_bootstrap(self):
        gets = 0
        calls = []
        def changed(argv, **kwargs):
            nonlocal gets
            calls.append(argv)
            if argv[:3] == ["herdr", "pane", "get"]:
                gets += 1
                return _herdr_json({"result": {"pane": {} if gets == 1 else {"agent": "claude"}}})
            return _idle_shell_run(argv, **kwargs)
        with mock.patch.object(peer_steward.subprocess, "run", side_effect=changed):
            self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-occupied")
        self.assertEqual(gets, 2)
        self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"]) for a in calls))

    def test_unreadable_or_error_pane_never_reaches_wait_or_input(self):
        payloads = [[], {"result": "invalid"}, {"result": {"pane": None}},
                    {"error": {"code": "unavailable"}, "result": {"pane": {}}}]
        for payload in payloads:
            with self.subTest(payload=payload), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=_herdr_json(payload)) as run:
                self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-unknown")
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][:3], ["herdr", "pane", "get"])

    def test_unknown_or_foreign_foreground_receives_no_bootstrap(self):
        infos = [None, {},
            {"pane_id": "w1:pOther", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": None, "foreground_processes": []},
            {"pane_id": "w1:pM", "shell_pid": True, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": []},
            {"pane_id": "w1:pM", "shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": False}]},
        ]
        for info in infos:
            with self.subTest(info=info):
                calls = []
                def unknown(argv, **kwargs):
                    calls.append(argv)
                    if argv[:3] == ["herdr", "pane", "process-info"]:
                        return _herdr_json({"result": {"process_info": info}})
                    return _herdr_json({"result": {"pane": {}}})
                with mock.patch.object(peer_steward.subprocess, "run", side_effect=unknown):
                    self.assertEqual(peer_steward._ensure_pane_ingress("w1:pM", "claude", str(self.tmp_root)), "pane-unknown")
                self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"]) for a in calls))
        for failure in (OSError("unavailable"), subprocess.TimeoutExpired(["herdr"], 5)):
            with self.subTest(failure=type(failure).__name__), \
                 mock.patch.object(peer_steward.subprocess, "run", side_effect=failure):
                self.assertEqual(peer_steward._pane_foreground_shell("w1:pM"), "pane-unknown")

    def test_opencode_uses_the_interactive_positional_project_argument(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn(os.path.realpath(str(self.tmp_root)), cmd[cmd.index("--") + 1:])
        self.assertNotIn("--cwd", cmd)

    def _scoped_tui_config(self):
        scoped = Path(os.environ["HOME"]) / ".config" / "opencode" / "tui"
        scoped.mkdir(parents=True, exist_ok=True)
        target = scoped / "hearting-owned-tui.json"
        target.write_text('{"$schema": "https://opencode.ai/tui.json", "plugin": ["./hearting-tui-identity.ts"]}\n',
                          encoding="utf-8")
        return str(target)

    def test_opencode_start_exports_scoped_tui_config_before_launch(self):
        import contextlib
        import io
        scoped = self._scoped_tui_config()
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock, \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        sends = [a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]]
        self.assertEqual(len(sends), 1, argvs)
        self.assertIn("OPENCODE_TUI_CONFIG=", sends[0][-1])
        self.assertIn(scoped, sends[0][-1])
        self.assertLess(argvs.index(sends[0]),
                        argvs.index(_agent_start_cmd(run_mock)))
        self.assertIn(["herdr", "pane", "send-keys", "w1:pM", "Enter"], argvs)
        self.assertIn("tui_scoped=exported", out.getvalue())
        # The launch argv itself gains no new flag: the config travels in the pane env.
        cmd = _agent_start_cmd(run_mock)
        self.assertNotIn("OPENCODE_TUI_CONFIG", " ".join(cmd))

    def test_scoped_export_keeps_an_explicit_override_already_in_the_pane(self):
        # The captured prelaunch line runs in an isolated child shell: an
        # explicit user value survives, an unset value takes the scoped file.
        scoped = self._scoped_tui_config()
        line = "export OPENCODE_TUI_CONFIG=${OPENCODE_TUI_CONFIG:-%s}" % scoped
        keep = subprocess.run(["sh", "-c", line + '; printf %s "$OPENCODE_TUI_CONFIG"'],
                              capture_output=True, text=True, timeout=10,
                              env={"OPENCODE_TUI_CONFIG": "/user/tui.json", "PATH": os.environ["PATH"]})
        self.assertEqual(keep.stdout, "/user/tui.json")
        fill = subprocess.run(["sh", "-c", line + '; printf %s "$OPENCODE_TUI_CONFIG"'],
                              capture_output=True, text=True, timeout=10,
                              env={"PATH": os.environ["PATH"]})
        self.assertEqual(fill.stdout, scoped)

    def test_opencode_start_without_scoped_config_proceeds_and_notes_missing(self):
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                side_effect=_idle_shell_run) as run_mock, \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "opencode", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        argvs = [c[0][0] for c in run_mock.call_args_list]
        self.assertEqual([a for a in argvs if a[:3] == ["herdr", "pane", "send-text"]], [])
        self.assertIn("tui_scoped=missing", out.getvalue())
        _agent_start_cmd(run_mock)

    def test_pane_input_waits_for_shell_readiness_and_times_out_without_start(self):
        self._ingress()
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="timeout")
            return _herdr_json({"result": {"pane": {}}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run), \
             mock.patch("builtins.print") as output:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 1)
        self.assertIn("reason=shell-readiness-timeout", output.call_args[0][0])
        self.assertFalse(any(argv[:3] == ["herdr", "pane", "send-text"] for argv in calls))
        self.assertFalse(any(argv[:3] == ["herdr", "agent", "start"] for argv in calls))

    def test_native_folder_trust_wait_is_explicit_and_unknown_screens_stay_unknown(self):
        trust = peer_steward._screen_lines("Trust this folder? Continue only if you trust this project.")
        unknown = peer_steward._screen_lines("Press Enter to continue")
        self.assertEqual(peer_steward._native_trust_reason("codex", trust), "native-trust-wait")
        self.assertIsNone(peer_steward._native_trust_reason("codex", unknown))

    def test_started_process_waits_for_native_trust_without_claiming_ready(self):
        import contextlib
        import io
        calls = []
        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json(_agent_json("codex", "thread-9", "peer-c"))
            if argv[:3] == ["herdr", "agent", "read"]:
                return subprocess.CompletedProcess(argv, 0,
                    stdout="Trust this folder? Do you trust this project?", stderr="")
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"foreground_processes": []}}})
            return _herdr_json({"result": {"pane": {}}})
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=fake_run), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertEqual(rc, 0)
        self.assertIn("started=true", out.getvalue())
        self.assertIn("ready=false reason=native-trust-wait", out.getvalue())
        self.assertFalse(any(argv[:3] == ["herdr", "pane", "send-text"] for argv in calls))

    def test_a_cwd_that_is_not_a_directory_never_reaches_herdr(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "codex",
                                    "--pane", "w1:pM",
                                    "--cwd", str(self.tmp_root / "nope")])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_no_cwd_leaves_the_launch_exactly_as_it_was(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertNotIn("--cd", _agent_start_cmd(run_mock))

    def test_the_receipt_says_when_the_launch_produced_no_identity(self):
        """A session herdr cannot name has no ledger endpoint and no board badge. That
        used to surface hours later as a nameless row; it belongs in the launch receipt."""
        import io
        import contextlib
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", None, "peer-c"))), \
             mock.patch.object(peer_steward, "_BIND_PROCESS_SECONDS", 0.1), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.02):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("session_id=-", out.getvalue())
        self.assertIn("session_bind=no-process", out.getvalue())

    def test_the_receipt_names_the_identity_when_there_is_one(self):
        import io
        import contextlib
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=_herdr_json(_agent_json("codex", "thread-9", "peer-c"))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("session_id=thread-9", out.getvalue())

    def test_name_is_the_positional_right_after_start(self):
        """herdr `agent start <NAME> --kind --pane`: the name is a required positional.
        Without it herdr answers `unknown option: claude` and starts nothing, while the
        wrapper still printed started=false and recorded a `[start]` steer (measured
        2026-09-03 during the F-100 comms test)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(name="peer-c", kind="claude", pane="w1:pM")
        cmd = _agent_start_cmd(run_mock)
        self.assertEqual(cmd[:6], ["herdr", "agent", "start", "peer-c", "--kind", "claude"])
        self.assertEqual(cmd[6:8], ["--pane", "w1:pM"])

    def test_default_bypass_prepends_claude_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = self._start(kind="claude")
        self.assertEqual(rc, 0)
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)

    def test_default_bypass_prepends_codex_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="codex")
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_default_bypass_prepends_opencode_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="opencode")
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--auto", cmd)

    def test_inherit_prepends_zero_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", permission_mode="inherit")
        cmd = _agent_start_cmd(run_mock)
        self.assertNotIn("bypassPermissions", cmd)
        self.assertNotIn("--permission-mode", cmd)

    def test_record_and_typed_line_emitted(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")):
            self._start(name="peer-d", kind="claude")
        line = print_mock.call_args[0][0]
        self.assertIn("started=true", line)
        self.assertIn("agent=claude", line)
        self.assertIn("name=peer-d", line)
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "steer")
        self.assertEqual(recs[0]["summary"], "[start] peer-d kind=claude mode=bypass")
        self.assertEqual(recs[0]["delivery"]["surface"], "herdr")

    def test_start_that_launched_marks_the_launcher_with_source_start(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        started = json.dumps({"result": {"agent": {"agent_session": {"value": "child-sid"}}}})
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout=started, stderr="")):
            self.assertEqual(self._start(name="peer-e", kind="codex"), 0)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertEqual(set(markers), {("claude", "sid-steward")})
        entry = markers[("claude", "sid-steward")]["targets"]["child-sid"]
        self.assertEqual((entry["harness"], entry["name"], entry["kind"], entry["source"]),
                         ("codex", "peer-e", "start", "start"))
        self.assertEqual(entry["session_id"], "child-sid")

    def test_start_that_herdr_refused_marks_nothing(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print"), \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            self.assertEqual(self._start(name="peer-f", kind="claude"), 0)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_start_with_an_error_body_or_no_agent_block_marks_nothing(self):
        """Review round 1 #9: exit 0 alone is not a launch."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        for stdout, expect in ((json.dumps({"error": {"code": "pane_busy"}}), "started=false"),
                               ("", "started=true"), ("not json", "started=true")):
            with self.subTest(stdout=stdout):
                with mock.patch("builtins.print") as print_mock, \
                     mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run",
                                       return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")):
                    self.assertEqual(self._start(name="peer-g", kind="codex"), 0)
                self.assertIn(expect, print_mock.call_args[0][0])
                self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_herdr_missing_exit_4(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            rc = self._start()
        self.assertEqual(rc, 4)

    def test_started_false_when_herdr_start_fails(self):
        with mock.patch("builtins.print") as print_mock, \
             mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom")):
            rc = self._start()
        self.assertEqual(rc, 0)
        line = print_mock.call_args[0][0]
        self.assertIn("started=false", line)

    def test_start_failure_explains_name_collision_and_bounded_fallbacks(self):
        for error, rc, stderr, reason in (
            ({"code": "agent_name_taken"}, 0, "private detail", "agent_name_taken"),
            ({"code": "bad code\nwith details"}, 0, "private detail", "herdr-start-error"),
            ("unstructured error", 0, "private detail", "herdr-start-error"),
            (None, 2, "private detail", "herdr-stderr-private-detail"),
            (None, 2, "", "herdr-start-failed"),
            (None, 2, "\x1b[31mName too long\x1b[0m\nprivate second line",
             "herdr-stderr-name-too-long"),
            (None, 2, "A" * 120 + "\nprivate second line", "herdr-stderr-" + "a" * 80),
        ):
            with self.subTest(error=error), mock.patch("builtins.print") as output, \
                 mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], rc, stdout=json.dumps({"error": error}), stderr=stderr)):
                self._start()
                line = output.call_args[0][0]
                self.assertIn("started=false", line)
                self.assertIn(f"reason={reason} herdr_rc={rc}", line)
                self.assertNotIn("private detail", line)
                self.assertNotIn("private second line", line)
                self.assertNotIn("\n", line)
                self.assertFalse(any(ord(char) < 32 or ord(char) == 127 for char in line))
                diagnostic = re.search(r"\breason=([^ ]+)", line).group(1)
                self.assertRegex(diagnostic, r"^[a-z0-9_-]+$")
                if diagnostic.startswith("herdr-stderr-"):
                    self.assertLessEqual(len(diagnostic.removeprefix("herdr-stderr-")), 80)

    def test_agent_args_pass_through_after_prefix_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", agent_args=["--extra-flag"])
        cmd = _agent_start_cmd(run_mock)
        self.assertIn("--extra-flag", cmd)
        self.assertLess(cmd.index("bypassPermissions"), cmd.index("--extra-flag"))



# ---------------------------------------------------------------------------
# SD-122 (10) detached steward watch — A56-1..A56-4, A56-7
# ---------------------------------------------------------------------------

_FAKE_HERDR = r'''#!/usr/bin/env python3
"""Fake herdr 0.8.0. Reproduces the measured stdout/stderr + exit-code split."""
import json, os, sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_HERDR_CALLLOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(" ".join(argv) + "\n")

mode = os.environ.get("FAKE_HERDR_MODE", "idle")
target = argv[2] if len(argv) > 2 else "-"
info = {"id": "cli:agent:wait", "result": {"agent": {
    "agent": "claude", "agent_session": {"value": "sid-fake"},
    "agent_status": "idle", "name": target, "pane_id": "w1:p9"}, "type": "agent_info"}}

def fail(code):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": code}}))
    sys.exit(1)

verb = argv[1] if len(argv) > 1 else ""
if verb == "get" and mode == "hang-get":
    with open(os.environ["FAKE_HERDR_FIFO"], "r") as fh:   # wedged socket
        fh.read()
if mode == "not-found":
    fail("agent_not_found")
if verb == "get":
    print(json.dumps(info)); sys.exit(0)
if verb == "wait":
    if mode == "timeout":
        fail("timeout")
    if mode == "held":
        fifo = os.environ["FAKE_HERDR_FIFO"]
        with open(fifo, "r") as fh:   # blocks until the test writes: an event, not a poll
            fh.read()
    print(json.dumps(info)); sys.exit(0)
print(json.dumps(info)); sys.exit(0)
'''


class StartSessionBindTest(_TmpRootMixin, unittest.TestCase):
    """`start --kind codex` binds the launched thread when the launcher can prove it.

    A TUI attached to the shared Codex daemon holds no rollout file and the daemon creates
    the thread's rollout about a second after the TUI starts, so Fleet sees an anonymous
    row (several same-cwd starts overlap its start-time window). The launcher is the one
    party that saw the launch: it notes the rollouts that exist before `herdr agent start`
    and, afterwards, takes exactly one new root rollout for the target cwd as the session.
    Zero or several new rollouts is no proof: no guess, `session_id=-`, and the start is
    never failed or blocked beyond the bound.
    """

    def setUp(self):
        super().setUp()
        self.home = self.tmp_root / "codex-home"
        (self.home / "sessions" / "2026" / "10" / "02").mkdir(parents=True)
        self.project = self.tmp_root / "project"
        self.project.mkdir()
        self.registry = self.tmp_root / "registry"
        os.environ["CODEX_HOME"] = str(self.home)
        os.environ["FLEET_SESSION_REGISTRY_DIR"] = str(self.registry)
        # the "TUI": a real process, so /proc has a start time and a cwd for it
        self.tui = subprocess.Popen(["sleep", "60"], cwd=str(self.project))
        self.addCleanup(self.tui.wait)
        self.addCleanup(self.tui.kill)
        self.late = []        # rollouts the "daemon" writes after the TUI is up
        self.calls = []

    def _rollout(self, sid, cwd=None, created=None, originator="codex-tui", source="vscode"):
        created = created if created is not None else time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + ".%03dZ" % (int(created * 1000) % 1000)
        meta = {"id": sid, "timestamp": stamp, "cwd": str(cwd or self.project),
                "originator": originator, "source": source}
        path = self.home / "sessions" / "2026" / "10" / "02" / (
            "rollout-%s-%s.jsonl" % (stamp[:19].replace(":", "-"), sid))
        path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n")
        return path

    def _fake_herdr(self, argv, **_kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["herdr", "agent", "start"]:
            return _herdr_json({"result": {"agent": {"name": argv[3], "agent": "codex",
                                                     "pane_id": "w1:pM"}}})
        if argv[:3] == ["herdr", "pane", "process-info"]:
            for write in self.late:
                write()
            self.late = []
            return _herdr_json({"result": {"process_info": {"foreground_processes": [
                {"pid": self.tui.pid, "argv": ["codex", "--cd", str(self.project)]}]}}})
        return _herdr_json({"result": {"pane": {}}})

    def _start(self, *extra, kind="codex", name="bl-c1"):
        import io
        import contextlib
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_herdr), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_BIND_SECONDS", 1.0), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.05), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", name, "--kind", kind, "--pane", "w1:pM",
                                    *(["--cwd", str(self.project)] if kind == "codex" else []), *extra])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _registry_record(self):
        path = self.registry / "codex" / ("%d.json" % self.tui.pid)
        return json.loads(path.read_text()) if path.is_file() else None

    def _proc_start(self):
        raw = Path("/proc/%d/stat" % self.tui.pid).read_text()
        return raw[raw.rindex(")") + 1:].split()[19]

    def test_one_new_root_rollout_is_the_launched_session(self):
        sid = "01a0fa57-7f0e-7eb0-bffe-1d56df94e90c"
        self.late.append(lambda: self._rollout(sid))
        out = self._start()
        self.assertIn("session_id=" + sid, out)
        self.assertIn("session_bind=bound", out)
        record = self._registry_record()
        self.assertEqual(record["sessionId"], sid)
        self.assertEqual(record["procStart"], self._proc_start())
        self.assertEqual(record["cwd"], os.path.realpath(str(self.project)))
        self.assertEqual(record["name"], "bl-c1")
        self.assertNotEqual(record.get("nameSource"), "derived")
        self.assertEqual(record["harness"], "codex")
        self.assertEqual(record["pid"], self.tui.pid)
        sent = [r for r in self._all_records() if r.get("kind") == "steer"]
        self.assertEqual(sent[0]["to"]["session_id"], sid)

    def test_two_new_root_rollouts_are_never_guessed(self):
        self.late.append(lambda: self._rollout("01a0fa57-0000-7000-8000-000000000001"))
        self.late.append(lambda: self._rollout("01a0fa57-0000-7000-8000-000000000002"))
        out = self._start()
        self.assertIn("session_id=-", out)
        self.assertIn("session_bind=ambiguous", out)
        self.assertIsNone(self._registry_record())

    def test_an_embedded_start_never_binds_a_time_candidate(self):
        """F2: an Embedded TUI owns its rollout fd, but the fd appears only after
        its first thread exists. While it does not, another same-cwd execution's
        lone new root rollout would satisfy the before/after binder — and the
        resulting tier-1 record would outrank the later own fd forever. So an
        Embedded start skips the binder entirely: unknown until the fd proves it,
        zero registry writes from time candidates."""
        import io
        import contextlib
        foreign = "01a0fa57-0000-7000-8000-0000000000f1"
        self.late.append(lambda: self._rollout(foreign))
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_herdr), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_managed_ingress_dir", return_value=None), \
             mock.patch.object(peer_steward, "_codex_supports_no_daemon", return_value=True), \
             mock.patch.object(peer_steward, "_bind_codex_session") as bind_mock, \
             mock.patch.object(peer_steward, "_BIND_SECONDS", 1.0), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.05), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "bl-c1", "--kind", "codex", "--pane", "w1:pM",
                                    "--cwd", str(self.project)])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("started=true", text)
        self.assertIn("session_id=-", text)
        self.assertNotIn("session_bind=", text)
        self.assertEqual(bind_mock.call_count, 0)
        self.assertIsNone(self._registry_record())
        starts = [c for c in self.calls if c[:3] == ["herdr", "agent", "start"]]
        self.assertEqual(len(starts), 1)
        self.assertIn("--no-daemon", starts[0])

    def test_a_caller_stated_no_daemon_start_never_binds_a_time_candidate(self):
        """F2 residual: a caller-stated `--no-daemon` really is Embedded execution
        (the helper returns ``[]`` only to avoid injecting a duplicate flag), so it
        must skip the binder like an injected one. Reproduced against 4543a6ec with
        `_start(--, --no-daemon)`: the foreign root bound and would have been
        written tier-1. No duplicate flag is injected on top of the caller's own."""
        import io
        import contextlib
        foreign = "01a0fa57-0000-7000-8000-0000000000f2"
        self.late.append(lambda: self._rollout(foreign))
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_herdr), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_bind_codex_session") as bind_mock, \
             mock.patch.object(peer_steward, "_BIND_SECONDS", 1.0), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.05), \
             contextlib.redirect_stdout(out):
            rc = peer_steward.main(["start", "bl-c1", "--kind", "codex", "--pane", "w1:pM",
                                    "--cwd", str(self.project), "--", "--no-daemon"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("started=true", text)
        self.assertIn("session_id=-", text)
        self.assertNotIn("session_bind=", text)
        self.assertEqual(bind_mock.call_count, 0)
        self.assertIsNone(self._registry_record())
        starts = [c for c in self.calls if c[:3] == ["herdr", "agent", "start"]]
        self.assertEqual(len(starts), 1)
        typed = starts[0][starts[0].index("--") + 1:]
        self.assertEqual(typed.count("--no-daemon"), 1)

    def test_stated_no_daemon_ignores_option_values_and_native_literal_boundary(self):
        self.assertTrue(peer_steward._codex_stated_no_daemon(["--no-daemon"]))
        self.assertTrue(peer_steward._codex_stated_no_daemon(["resume", "--no-daemon"]))
        self.assertFalse(peer_steward._codex_stated_no_daemon([]))
        self.assertFalse(peer_steward._codex_stated_no_daemon(["--remote", "unix:///x.sock"]))
        self.assertFalse(peer_steward._codex_stated_no_daemon(["-m", "--no-daemon"]))
        self.assertFalse(peer_steward._codex_stated_no_daemon(["--", "--no-daemon"]))

    def test_no_new_rollout_within_the_bound_times_out_without_failing(self):
        started = time.time()
        out = self._start()
        self.assertLess(time.time() - started, 5)
        self.assertIn("started=true", out)
        self.assertIn("session_id=-", out)
        self.assertIn("session_bind=timeout", out)
        self.assertIsNone(self._registry_record())

    def test_a_rollout_that_existed_before_the_start_is_never_taken(self):
        self._rollout("01a0fa57-0000-7000-8000-0000000000aa")
        out = self._start()
        self.assertIn("session_id=-", out)
        self.assertIn("session_bind=timeout", out)
        sid = "01a0fa57-0000-7000-8000-0000000000bb"
        self.late.append(lambda: self._rollout(sid))
        self.assertIn("session_id=" + sid, self._start(name="bl-c2"))

    def test_other_cwd_subagent_and_stale_rollouts_are_ignored(self):
        other = self.tmp_root / "elsewhere"
        other.mkdir()
        self.late.append(lambda: self._rollout("01a0fa57-0000-7000-8000-0000000000c1", cwd=other))
        self.late.append(lambda: self._rollout("01a0fa57-0000-7000-8000-0000000000c2",
                                               source={"subagent": {"parent": "x"}}))
        self.late.append(lambda: self._rollout("01a0fa57-0000-7000-8000-0000000000c3",
                                               created=time.time() - 3600))
        out = self._start()
        self.assertIn("session_id=-", out)
        self.assertIn("session_bind=timeout", out)
        self.assertIsNone(self._registry_record())

    def test_a_session_id_herdr_already_gave_is_kept_and_not_searched_for(self):
        def with_session(argv, **kwargs):
            if argv[:3] == ["herdr", "agent", "start"]:
                self.calls.append(list(argv))
                return _herdr_json({"result": {"agent": {
                    "name": argv[3], "agent": "codex",
                    "agent_session": {"value": "herdr-given-sid"}}}})
            return self._fake_herdr(argv, **kwargs)
        import io
        import contextlib
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=with_session), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             contextlib.redirect_stdout(out):
            peer_steward.main(["start", "bl-c1", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("session_id=herdr-given-sid", out.getvalue())
        self.assertNotIn("session_bind=", out.getvalue())
        self.assertEqual([c for c in self.calls if c[:3] == ["herdr", "pane", "process-info"]], [])

    def test_other_harnesses_are_not_searched_for_a_rollout(self):
        out = self._start(kind="claude")
        self.assertNotIn("session_bind=", out)
        self.assertEqual([c for c in self.calls if c[:3] == ["herdr", "pane", "process-info"]], [])

    def test_the_cwd_defaults_to_the_launched_process_cwd(self):
        sid = "01a0fa57-0000-7000-8000-0000000000d1"
        self.late.append(lambda: self._rollout(sid))
        import io
        import contextlib
        out = io.StringIO()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._fake_herdr), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch.object(peer_steward, "_BIND_SECONDS", 1.0), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.05), \
             contextlib.redirect_stdout(out):
            peer_steward.main(["start", "bl-c1", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("session_id=" + sid, out.getvalue())

    def test_a_missing_proc_start_keeps_the_thread_id_but_writes_no_registry_record(self):
        # Fleet rejects a record without a matching procStart, so none is written and the
        # receipt says the binding is unregistered; the start itself is neither failed nor blocked.
        sid = "01a0fa57-0000-7000-8000-0000000000e1"
        self.late.append(lambda: self._rollout(sid))
        with mock.patch.object(peer_steward, "_proc_start_ticks", return_value=None):
            out = self._start()
        self.assertIn("started=true", out)
        self.assertIn("session_id=" + sid, out)
        self.assertIn("session_bind=bound-unregistered", out)
        self.assertNotIn("session_bind=bound ", out + " ")
        self.assertIsNone(self._registry_record())
        sent = [r for r in self._all_records() if r.get("kind") == "steer"]
        self.assertEqual(sent[0]["to"]["session_id"], sid)

    def test_a_registry_write_that_fails_is_reported_unregistered_not_bound(self):
        sid = "01a0fa57-0000-7000-8000-0000000000e2"
        self.late.append(lambda: self._rollout(sid))
        registry = peer_steward._session_registry()
        with mock.patch.object(registry, "write", side_effect=OSError("read-only registry")):
            out = self._start()
        self.assertIn("session_id=" + sid, out)
        self.assertIn("session_bind=bound-unregistered", out)


class BindDeadlineConfirmationTest(unittest.TestCase):
    """A single candidate counts only after a second observation, also at the deadline.

    A rollout first seen on the final poll cannot be told apart from a concurrent same-cwd
    start, so the bind ends as `timeout` instead of taking it.
    """

    def _bind(self, sightings):
        """Run `_bind_codex_session` on a fake clock; `sightings[i]` is the poll-i rollout list."""
        clock = {"now": 0.0}
        polls = iter(sightings)
        fake_time = mock.Mock()
        fake_time.monotonic = lambda: clock["now"]
        fake_time.sleep = lambda seconds: clock.__setitem__("now", clock["now"] + seconds)
        fake_time.time = time.time
        with mock.patch.object(peer_steward, "time", fake_time), \
             mock.patch.object(peer_steward, "_fleet_codex_collector", return_value=object()), \
             mock.patch.object(peer_steward, "_pane_codex_pid", return_value=4242), \
             mock.patch.object(peer_steward, "_new_root_rollouts",
                               side_effect=lambda *a, **k: next(polls, sightings[-1])), \
             mock.patch.object(peer_steward, "_BIND_SECONDS", 1.0), \
             mock.patch.object(peer_steward, "_BIND_POLL_SECONDS", 0.25):
            return peer_steward._bind_codex_session("w1:pM", "/work", "/home", set(), 0.0)

    def test_a_candidate_first_seen_on_the_final_poll_is_not_bound(self):
        # polls at t=0, .25, .5, .75 -- the candidate appears only on the last one
        state, sid, pid, cwd = self._bind([[], [], [], ["sid-late"]])
        self.assertEqual((state, sid), ("timeout", None))
        self.assertEqual((pid, cwd), (4242, "/work"))

    def test_a_candidate_seen_on_the_last_two_polls_is_bound(self):
        state, sid, _pid, _cwd = self._bind([[], [], ["sid-ok"], ["sid-ok"]])
        self.assertEqual((state, sid), ("bound", "sid-ok"))

    def test_a_candidate_that_changes_on_the_final_poll_is_not_bound(self):
        state, sid, _pid, _cwd = self._bind([[], [], ["sid-a"], ["sid-b"]])
        self.assertEqual((state, sid), ("timeout", None))

    def test_a_candidate_that_vanishes_before_confirmation_is_not_bound(self):
        state, sid, _pid, _cwd = self._bind([["sid-a"], [], [], []])
        self.assertEqual((state, sid), ("timeout", None))


class EmbeddedCodexStartTest(_TmpRootMixin, unittest.TestCase):
    """Fresh steward-started Codex TUIs run Embedded (`--no-daemon`).

    A shared-daemon TUI holds no rollout fd, so two same-cwd TUIs started moments
    apart (2026-10-04: PID 3023216/3023356, 0.03 s apart) leave Fleet with no
    per-process proof and both rows stay anonymous. An Embedded TUI owns its
    transcript fd, and the existing fd resolver attributes each one exactly."""

    def _start_cmd(self, *argv, kind="codex", support=True, ingress=None):
        with mock.patch.object(peer_steward.shutil, "which",
                               return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=_idle_shell_run) as run_mock, \
             mock.patch.object(peer_steward, "_managed_ingress_dir",
                               return_value=ingress), \
             mock.patch.object(peer_steward, "_codex_supports_no_daemon",
                               return_value=support):
            rc = peer_steward.main(["start", "peer-c", "--kind", kind,
                                    "--pane", "w1:pM", *argv])
        self.assertEqual(rc, 0)
        return _agent_start_cmd(run_mock)

    def _typed(self, cmd):
        return cmd[cmd.index("--") + 1:]

    def test_a_fresh_codex_start_runs_embedded_first(self):
        cmd = self._start_cmd("--cwd", str(self.tmp_root))
        typed = self._typed(cmd)
        self.assertEqual(typed[0], "--no-daemon")
        self.assertEqual(typed[1], "--dangerously-bypass-approvals-and-sandbox")
        self.assertEqual(typed.count("--no-daemon"), 1)

    def test_resume_and_fork_keep_their_existing_thread(self):
        # An Embedded resume of a thread open in another app is refused natively;
        # continuing a thread must keep reaching it, never reshape the launch.
        for args in (["resume", "01a1055e-bd7b-7481-a5f9-7b2cc1bee230"],
                     ["fork", "01a1055e-bd7b-7481-a5f9-7b2cc1bee230"]):
            with self.subTest(args=args):
                cmd = self._start_cmd("--", *args)
                self.assertNotIn("--no-daemon", self._typed(cmd))

    def test_a_managed_ingress_is_never_given_a_conflicting_flag(self):
        # The wrapper forwards trailing args to its `--remote` client; the managed
        # path already attributes via registry + rollout transfer.
        cmd = self._start_cmd(ingress=str(self.tmp_root / "bin"))
        self.assertNotIn("--no-daemon", self._typed(cmd))

    def test_a_caller_stated_daemon_stance_is_never_second_guessed(self):
        for args in (["--no-daemon"], ["--remote", "unix:///tmp/x.sock"],
                     ["--remote=unix:///tmp/x.sock"]):
            with self.subTest(args=args):
                cmd = self._start_cmd("--", *args)
                self.assertLessEqual(self._typed(cmd).count("--no-daemon"),
                                     1 if args == ["--no-daemon"] else 0)

    def test_an_unrecognized_invocation_keeps_its_previous_shape(self):
        cmd = self._start_cmd("--", "--future-flag")
        self.assertNotIn("--no-daemon", self._typed(cmd))

    def test_an_initial_prompt_is_still_a_fresh_start(self):
        # A quoted prompt phrase is one argv token with a space: no native
        # subcommand name contains a space, so this cannot be a subcommand.
        cmd = self._start_cmd("--", "do stuff")
        self.assertEqual(self._typed(cmd)[0], "--no-daemon")

    def test_a_subcommand_after_a_prompt_word_is_never_reshaped(self):
        # F1: official 0.160 parses `codex hello resume` with `resume` as the
        # subcommand (`hello` sits in the PROMPT slot), so the scan must not stop
        # at the first positional. `hello` alone really is a prompt per
        # `codex --help` (`codex [OPTIONS] [PROMPT]`), pinned here so no future
        # "fix" re-breaks it; native aliases (`e`, `a`) fail closed too.
        for args, fresh in ((["hello", "resume", "--help"], False),
                            (["hello", "fork", "--help"], False),
                            (["hello"], True),
                            (["e", "dated"], False),
                            (["a"], False),
                            (["--help"], False),
                            (["-h"], False),
                            (["--version"], False),
                            (["--", "resume"], True),
                            (["-m", "resume"], True),
                            (["do", "stuff"], True)):
            with self.subTest(args=args):
                self.assertEqual(peer_steward._codex_fresh_tui_args(args), fresh)
                cmd = self._start_cmd("--", *args)
                self.assertEqual("--no-daemon" in self._typed(cmd), fresh)

    def test_other_harnesses_and_unprobed_support_add_nothing(self):
        cmd = self._start_cmd("--cwd", str(self.tmp_root), kind="claude")
        self.assertNotIn("--no-daemon", self._typed(cmd))
        cmd = self._start_cmd("--cwd", str(self.tmp_root), support=False)
        self.assertNotIn("--no-daemon", self._typed(cmd))

    def test_the_support_probe_is_fail_closed(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value=None):
            self.assertFalse(peer_steward._codex_supports_no_daemon())
        with mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=OSError("no codex")):
            self.assertFalse(peer_steward._codex_supports_no_daemon())
        bad = subprocess.CompletedProcess([], 1, stdout="--no-daemon", stderr="")
        with mock.patch.object(peer_steward.subprocess, "run", return_value=bad):
            self.assertFalse(peer_steward._codex_supports_no_daemon())
        plain = subprocess.CompletedProcess([], 0, stdout="usage: codex", stderr="")
        with mock.patch.object(peer_steward.subprocess, "run", return_value=plain):
            self.assertFalse(peer_steward._codex_supports_no_daemon())

    def test_freshness_scan_skips_known_root_options(self):
        self.assertTrue(peer_steward._codex_fresh_tui_args(
            ["--cd", "/work", "-m", "gpt-x", "--config", "k=v"]))
        self.assertTrue(peer_steward._codex_fresh_tui_args([]))
        self.assertFalse(peer_steward._codex_fresh_tui_args(["exec", "ls"]))
        self.assertFalse(peer_steward._codex_fresh_tui_args(["--cd"]))


class _WatchMixin(_TmpRootMixin):
    """Real subprocesses, real files, only `herdr` faked."""

    def setUp(self):
        super().setUp()
        # Registered after the tmp-dir cleanup so it runs first (LIFO): every watcher and
        # fake-herdr child is dead before the directory it writes into is removed.
        self.addCleanup(fixture_processes.reap, str(self.tmp_root),
                        fifo=str(self.tmp_root / "release.fifo"))
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.calllog = self.tmp_root / "calls.log"
        self.fifo = self.tmp_root / "release.fifo"
        os.mkfifo(self.fifo)
        self.watch_root = peer_steward.peer_message.peer_state_root() / "peer-watches"

    def _env(self, mode="idle", session_id="steward-1", with_herdr=True):
        env = dict(os.environ)
        env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        env["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
        env["FAKE_HERDR_MODE"] = mode
        env["FAKE_HERDR_CALLLOG"] = str(self.calllog)
        env["FAKE_HERDR_FIFO"] = str(self.fifo)
        env["CLAUDE_CODE_SESSION_ID"] = session_id
        base = env.get("PATH", "")
        env["PATH"] = (str(self.bin) + os.pathsep + base) if with_herdr else "/nonexistent"
        return env

    def _run(self, *argv, env=None, timeout=30, **kw):
        return subprocess.run(
            [sys.executable, str(_HERE / "peer-steward.py"), *argv],
            capture_output=True, text=True, env=env or self._env(), timeout=timeout, **kw
        )

    def _fields(self, line):
        out = {}
        for token in line.strip().split():
            key, sep, value = token.partition("=")
            if sep:
                out[key] = value
        return out

    def _directive(self, stdout):
        """The `parent_next*` fields, read from the directive line only.

        `parent_next_command` holds spaces, so it is the rest of its own line.
        """
        for line in stdout.splitlines():
            if line.startswith("parent_next="):
                head, _, command = line.partition("parent_next_command=")
                fields = self._fields(head)
                fields["parent_next_command"] = command.strip()
                return fields
        return {}

    def _release(self):
        # A held reader can close between our open() and write() (a watcher that just
        # got EOF from the previous release): EPIPE means "no reader took it", retry.
        for _ in range(100):
            try:
                with open(self.fifo, "w") as fh:
                    fh.write("go")
                return
            except BrokenPipeError:
                time.sleep(0.01)
        self.fail("no reader took the FIFO release")

    def _wait_for_lock(self, watch_id, seconds=30):
        """Bound-wait until the watcher has actually taken its flock.

        `watch` returns as soon as the watcher is spawned, so for a few hundred
        milliseconds the watch is legitimately `armed` and not yet `alive` --
        that ordering is the whole reason `join` must not decide `watcher-dead`
        from lock acquisition alone.
        """
        lock = self.watch_root / f"{watch_id}.lock"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if peer_steward._lock_held(lock):
                return
            time.sleep(0.02)
        self.fail(f"watcher never took its lock for {watch_id}")

    def _wait_for_receipt(self, watch_id, seconds=30):
        """Bound-wait in the TEST (allowed); the product waits on events only."""
        path = self.watch_root / f"{watch_id}.receipt.json"
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists():
                return json.loads(path.read_text())
            time.sleep(0.02)
        self.fail(f"receipt never appeared for {watch_id}")


class WatchArmTest(_WatchMixin, unittest.TestCase):
    """A56-1."""

    def test_a_hook_armed_watch_tells_the_caller_to_end_the_turn(self):
        """Review round 2/3, M-B: the contract function was locked, its wiring
        was not -- reverting either call site passed every suite."""
        proc = self._run("watch", "peer-a", "--wake", "hook", env=self._env("idle"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "end-turn", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "carrier-steward-watch")
        self.assertEqual(directive.get("parent_next_command"), "-")

    def test_a_watch_with_no_carrier_tells_the_caller_to_wait_with_a_bound(self):
        proc = self._run("watch", "peer-a", "--wake", "none", env=self._env("idle"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "bounded-wait", proc.stdout)
        self.assertEqual(directive.get("parent_next_reason"), "steward-wake-none")
        command = directive.get("parent_next_command", "")
        self.assertIn("peer-steward.py join ", command)
        self.assertIn("--timeout ", command)
        self.assertNotIn("dispatch-wait", command)

    def test_no_line_the_hook_cannot_arm_from_ever_says_end_turn(self):
        """The dedupe hit and `rearm` both print from the session, not `watch`.

        Answering `end-turn` there is worst exactly when it is reached: a caller
        re-runs `watch`/`rearm` *because* it doubts the first arm carried.
        """
        first = self._run("watch", "peer-a", "--wake", "hook", env=self._env("held"))
        self.assertEqual(first.returncode, 0, first.stderr)
        watch_id = self._fields(first.stdout.splitlines()[0])["watch_id"]
        self._wait_for_lock(watch_id)
        for argv in (
            ("watch", "peer-a", "--wake", "hook"),  # dedupe -> already-armed
            ("rearm", watch_id),                    # live watcher -> alive
        ):
            with self.subTest(argv=argv):
                proc = self._run(*argv, env=self._env("held"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                state = self._fields(proc.stdout.splitlines()[0]).get("state")
                self.assertIn(state, {"already-armed", "alive"}, proc.stdout)
                directive = self._directive(proc.stdout)
                self.assertEqual(
                    directive.get("parent_next"), "bounded-wait", proc.stdout
                )
                self.assertIn("--timeout ", directive.get("parent_next_command", ""))
        self._release()
        self._wait_for_receipt(watch_id)

    def test_arm_emits_typed_line_immutable_record_and_one_ledger_row(self):
        proc = self._run("watch", "peer-a", "--wake", "hook", env=self._env("held"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = self._fields(proc.stdout)
        self.assertEqual(fields["state"], "armed")
        self.assertEqual(fields["target"], "peer-a")
        self.assertEqual(fields["wake"], "hook")
        for key in ("watch_id", "until", "pid", "pid_start", "receipt"):
            self.assertIn(key, fields)
        self.assertEqual(len(fields["watch_id"]), 16)

        arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(arm["watch_id"], fields["watch_id"])
        self.assertEqual(arm["steward"]["session_id"], "steward-1")
        self.assertEqual(arm["watcher"]["pid"], int(fields["pid"]))
        self.assertTrue(arm["watcher"]["pid_start"])

        rows = [r for r in self._all_records() if r["kind"] == "watch"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery"]["surface"], "herdr")
        self.assertEqual(rows[0]["delivery"]["status"], "sent")
        self.assertEqual(rows[0]["delivery"]["receipt"], fields["watch_id"])

        # F-100c-2: arming a watcher on a resolved target is steward evidence (source=watch)
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertIn(("claude", "steward-1"), markers)
        entry = next(iter(markers[("claude", "steward-1")]["targets"].values()))
        self.assertEqual((entry["name"], entry["kind"], entry["source"]), ("peer-a", "watch", "watch"))

        self._release()
        self._wait_for_receipt(fields["watch_id"])

    def test_watcher_runs_in_its_own_session_not_the_callers(self):
        # `setsid`, proven exactly -- `os.getsid`, never a /proc/<pid>/stat field
        # index (comm can contain spaces, and the session id is not field 6 of
        # the whitespace split).
        proc = self._run("watch", "peer-a", env=self._env("held"))
        pid = int(self._fields(proc.stdout)["pid"])
        self.assertNotEqual(os.getsid(pid), os.getsid(os.getpid()))
        self.assertEqual(os.getsid(pid), pid)
        self._release()
        self._wait_for_receipt(self._fields(proc.stdout)["watch_id"])

    def test_duplicate_arm_spawns_nothing(self):
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        self._wait_for_lock(watch_id)
        before = sorted(self.watch_root.glob("*.json"))
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(second.returncode, 0)
        fields = self._fields(second.stdout)
        self.assertEqual(fields["state"], "already-armed")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertEqual(sorted(self.watch_root.glob("*.json")), before)
        self._release()
        self._wait_for_receipt(watch_id)

    def test_duplicate_arm_holds_during_the_spawn_latency_window(self):
        """The regression the E2 fix exists for.

        The second `watch` runs before the first watcher has taken its flock, so
        a lock-based liveness test reports the healthy watch as dead and spawns a
        duplicate. Dedupe must decide on PID identity alone.
        """
        first = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(first.stdout)["watch_id"]
        # The lock is held from before `watch` returned -- the caller takes it
        # and hands the open description to the watcher, so no window exists in
        # which a healthy watch looks unlocked.
        self.assertTrue(peer_steward._lock_held(self.watch_root / f"{watch_id}.lock"))
        second = self._run("watch", "peer-a", env=self._env("held"))
        self.assertEqual(self._fields(second.stdout)["state"], "already-armed")
        self.assertEqual(len(list(self.watch_root.glob("*.json"))), 1)
        self._release()
        self._wait_for_receipt(watch_id)

    def test_herdr_missing_is_exit_4_with_zero_watch_files(self):
        proc = self._run("watch", "peer-a", env=self._env(with_herdr=False))
        self.assertEqual(proc.returncode, 4)
        self.assertIn("herdr-unavailable", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")) if self.watch_root.is_dir() else [], [])

    def test_absent_target_is_exit_2_with_zero_watch_files(self):
        proc = self._run("watch", "ghost", env=self._env("not-found"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("state=agent-not-found", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")), [])

    def test_product_modules_contain_no_sleep_or_poll_loop(self):
        """The user-facing constraint, asserted rather than left to review.

        Checked against the AST, not the raw text: a comment explaining why a
        loop is bounded must not be able to fail the guard, and a `sleep`
        hidden in a string must not be able to pass it.
        """
        import ast

        def violations(source, *, bounded_ui):
            tree = ast.parse(source)
            parents = {child: parent for parent in ast.walk(tree)
                       for child in ast.iter_child_nodes(parent)}
            found = []
            for node in ast.walk(tree):
                if (isinstance(node, ast.While) and isinstance(node.test, ast.Constant)
                        and node.test.value is True):
                    found.append((node.lineno, "unbounded while"))
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                sleeping = (isinstance(func, ast.Attribute) and func.attr == "sleep"
                            or isinstance(func, ast.Name) and func.id == "sleep")
                if not sleeping:
                    continue
                branch, parent = node, parents.get(node)
                bounded = False
                while parent is not None:
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef,
                                           ast.Lambda, ast.ClassDef, ast.For, ast.AsyncFor)):
                        break
                    if isinstance(parent, ast.While):
                        test = parent.test
                        if (bounded_ui and branch in parent.body
                                and isinstance(test, ast.Compare)
                                and len(test.ops) == len(test.comparators) == 1
                                and isinstance(test.ops[0], ast.Lt)
                                and isinstance(test.left, ast.Call)
                                and not test.left.args and not test.left.keywords
                                and isinstance(test.left.func, ast.Attribute)
                                and test.left.func.attr == "monotonic"
                                and isinstance(test.left.func.value, ast.Name)
                                and test.left.func.value.id == "time"
                                and isinstance(test.comparators[0], ast.Name)):
                            deadline = test.comparators[0].id
                            bounded = not any(
                                isinstance(inner, ast.Name) and inner.id == deadline
                                and isinstance(inner.ctx, (ast.Store, ast.Del))
                                for statement in parent.body for inner in ast.walk(statement)
                            )
                        break
                    branch, parent = parent, parents.get(parent)
                if not bounded:
                    found.append((node.lineno, "sleep outside fixed monotonic deadline body"))
            return found

        # Structural examples exercise the guard itself; function names grant
        # nothing, and a deadline cannot be refreshed inside its observed loop.
        bounded = "while time.monotonic() < deadline:\n    time.sleep(.05)\n"
        self.assertEqual(violations(bounded, bounded_ui=True), [])
        self.assertTrue(violations(bounded, bounded_ui=False))  # wake hook never sleeps
        self.assertEqual(violations("text = 'time.sleep(1)'  # while True\n", bounded_ui=True), [])
        for forbidden in (
            "time.sleep(1)\n",
            "while True:\n    pass\n",
            "while ready():\n    time.sleep(1)\n",
            "while time.time() < deadline:\n    time.sleep(1)\n",
            "while time.monotonic() <= deadline:\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    pass\nelse:\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    deadline = time.monotonic() + 1\n    time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    def later():\n        time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    for event in events():\n        time.sleep(1)\n",
            "while time.monotonic() < deadline:\n    while ready():\n        time.sleep(1)\n",
            "def _bind_codex_session():\n    time.sleep(1)\n",
        ):
            with self.subTest(source=forbidden):
                self.assertTrue(violations(forbidden, bounded_ui=True))

        for name in ("peer-steward.py", "../hooks/peer-steward-rewake.py"):
            path = (_HERE / name).resolve()
            self.assertTrue(path.exists(), f"{name} missing")
            self.assertEqual(violations(path.read_text(), bounded_ui=name == "peer-steward.py"),
                             [], f"{path.name}: only fixed-deadline foreground observation may sleep")


class WatcherReceiptTest(_WatchMixin, unittest.TestCase):
    """A56-2."""

    def test_receipt_is_atomic_complete_and_text_free(self):
        proc = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        self._release()
        receipt = self._wait_for_receipt(watch_id)

        # Receipt publication precedes the notice write and process exit.
        # Wait for the real lock release before asserting either one; seeing
        # the receipt is not proof that the watcher has already exited.
        lock = self.watch_root / f"{watch_id}.lock"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail(f"watcher did not release its lock for {watch_id}")
                    time.sleep(0.02)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

        self.assertEqual(set(receipt), {
            "schema_version", "watch_id", "target", "steward", "armed_ts", "done_ts",
            "state", "agent", "herdr_exit", "watcher", "rearmed_from", "refs",
        })
        self.assertEqual(receipt["state"], "idle")
        self.assertEqual(receipt["steward"]["session_id"], "steward-1")
        self.assertEqual(receipt["agent"]["name"], "peer-a")
        for key in receipt:
            self.assertNotRegex(key, r"body|text|screen|output")

        waits = [l for l in self.calllog.read_text().splitlines() if " wait " in f" {l} "]
        self.assertEqual(len(waits), 1, self.calllog.read_text())

        notices = [r for r in self._all_records() if r["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["delivery"]["status"], "received")
        self.assertEqual(notices[0]["delivery"]["receipt"], watch_id)
        self.assertEqual(notices[0]["from"]["session_id"], "steward-1")

    def test_uninterpretable_herdr_yields_a_receipt_not_an_endless_wait(self):
        proc = self._run("watch", "peer-a", env=self._env("timeout"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        self.assertEqual(self._wait_for_receipt(watch_id)["state"], "timeout")


class JoinTest(_WatchMixin, unittest.TestCase):
    """A56-3."""

    def test_existing_receipt_returns_immediately_even_at_timeout_zero(self):
        armed = self._run("watch", "peer-a", env=self._env("idle"))
        watch_id = self._fields(armed.stdout)["watch_id"]
        self._wait_for_receipt(watch_id)
        proc = self._run("join", watch_id, "--timeout", "0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        fields = self._fields(proc.stdout)
        self.assertEqual(fields["state"], "idle")
        self.assertEqual(fields["watch_id"], watch_id)
        self.assertTrue(fields["receipt"].endswith(f"{watch_id}.receipt.json"))

    def test_join_returns_on_the_watcher_exit_event(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(armed.stdout)["watch_id"]
        joiner = subprocess.Popen(
            [sys.executable, str(_HERE / "peer-steward.py"), "join", watch_id],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self._env("held"),
        )
        self._release()
        out, err = joiner.communicate(timeout=30)   # the TEST bound-waits
        self.assertEqual(joiner.returncode, 0, out + err)
        self.assertEqual(self._fields(out)["state"], "idle")

    def test_killed_watcher_is_watcher_dead_exit_5(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        fields = self._fields(armed.stdout)
        os.kill(int(fields["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(fields["pid"]), fields["pid_start"]
        ):
            time.sleep(0.02)
        proc = self._run("join", fields["watch_id"], "--timeout", "5000")
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertIn("state=watcher-dead", proc.stdout)

    def test_bounded_join_that_misses_is_timeout_exit_6_with_the_watcher_alive(self):
        armed = self._run("watch", "peer-a", env=self._env("held"))
        fields = self._fields(armed.stdout)
        started = time.monotonic()
        proc = self._run("join", fields["watch_id"], "--timeout", "700")
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertIn("state=join-timeout", proc.stdout)
        self.assertLess(elapsed, 15, "the SIGALRM bound must actually fire")
        self.assertTrue(peer_steward._pid_identity_ok(int(fields["pid"]), fields["pid_start"]))
        self._release()
        self._wait_for_receipt(fields["watch_id"])


class StatusRearmTest(_WatchMixin, unittest.TestCase):
    """A56-4."""

    def _arm(self, target="peer-a", mode="held", session_id="steward-1"):
        proc = self._run("watch", target, env=self._env(mode, session_id=session_id))
        return self._fields(proc.stdout)

    def test_alive_needs_pid_starttime_and_lock_together(self):
        fields = self._arm()
        self._wait_for_lock(fields["watch_id"])
        arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertTrue(peer_steward._alive(arm, self.watch_root), "live watcher holding the lock")

        # a reaped pid -> dead
        dead = dict(arm, watch_id=arm["watch_id"], watcher={"pid": 2 ** 22 - 1, "pid_start": "1"})
        self.assertFalse(peer_steward._alive(dead, self.watch_root))

        # alive pid whose start ticks do not match -> dead
        skewed = dict(arm, watcher={"pid": arm["watcher"]["pid"], "pid_start": "999999999"})
        self.assertFalse(peer_steward._alive(skewed, self.watch_root))

        # a live process that never flocks -> DEAD. This is the fixture that
        # proves the lock condition is really ANDed and not decorative.
        idle = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                stdin=subprocess.PIPE)
        self.addCleanup(idle.kill)
        never_locks = {
            "watch_id": "deadbeefdeadbeef", "target": "x",
            "watcher": {"pid": idle.pid, "pid_start": peer_steward.process_start_ticks(idle.pid)},
        }
        self.assertTrue(peer_steward._pid_identity_ok(idle.pid, never_locks["watcher"]["pid_start"]))
        self.assertFalse(peer_steward._is_zombie(idle.pid))
        self.assertFalse(peer_steward._alive(never_locks, self.watch_root))

        self._release()
        self._wait_for_receipt(fields["watch_id"])

    def test_undelivered_lists_only_this_sessions_unacked_receipts(self):
        mine = self._arm(mode="idle")
        self._wait_for_receipt(mine["watch_id"])
        other = self._arm(target="peer-b", mode="idle", session_id="steward-2")
        self._wait_for_receipt(other["watch_id"])

        proc = self._run("status", "--undelivered", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["watch_root"], str(self.watch_root))
        ids = [row["watch_id"] for row in payload["watches"]]
        self.assertEqual(ids, [mine["watch_id"]])

        self.assertEqual(self._run("ack", mine["watch_id"], "--carrier", "t").returncode, 0)
        after = json.loads(self._run("status", "--undelivered", "--json").stdout)
        self.assertEqual(after["watches"], [])

    def test_ack_is_created_once_then_silently_skipped(self):
        fields = self._arm(mode="idle")
        self._wait_for_receipt(fields["watch_id"])
        first = self._run("ack", fields["watch_id"], "--carrier", "claude-async-rewake")
        second = self._run("ack", fields["watch_id"], "--carrier", "userprompt-sweep")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertIn("ack=created", first.stdout)
        self.assertIn("ack=already", second.stdout)
        ack = json.loads((self.watch_root / f"{fields['watch_id']}.ack.json").read_text())
        self.assertEqual(ack["carrier"], "claude-async-rewake")
        self.assertEqual(ack["session_id"], "steward-1")

    def test_a_restarted_watch_line_does_not_claim_the_hook_either(self):
        """`state=rearmed` — the branch B1-a was originally raised about.

        Review round 4 found the code correct here and nothing asserting it:
        flipping this branch back to `arms_hook=True` passed all 68 tests.
        """
        dead = self._arm(target="peer-dead", mode="held")
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        proc = self._run("rearm", dead["watch_id"], env=self._env("held"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        head = self._fields(proc.stdout.splitlines()[0])
        self.assertEqual(head["state"], "rearmed", proc.stdout)
        directive = self._directive(proc.stdout)
        self.assertEqual(directive.get("parent_next"), "bounded-wait", proc.stdout)
        self.assertEqual(
            directive.get("parent_next_reason"), "steward-line-does-not-arm", proc.stdout
        )
        # The wait must join *the new watch*, never the dead one it replaced.
        self.assertIn(f"join {head['watch_id']}", directive.get("parent_next_command", ""))
        self.assertNotIn(dead["watch_id"], directive.get("parent_next_command", ""))
        self._release()

    def test_rearm_only_replaces_a_dead_unreceipted_watch(self):
        done = self._arm(mode="idle")
        self._wait_for_receipt(done["watch_id"])
        self.assertIn("state=already-done", self._run("rearm", done["watch_id"]).stdout)

        live = self._arm(target="peer-live", mode="held")
        self._wait_for_lock(live["watch_id"])
        self.assertIn("state=alive", self._run("rearm", live["watch_id"]).stdout)

        dead = self._arm(target="peer-dead", mode="held")
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        out = self._run("rearm", dead["watch_id"], env=self._env("held")).stdout
        fields = self._fields(out.splitlines()[0])
        self.assertEqual(fields["state"], "rearmed")
        self.assertEqual(fields["rearmed_from"], dead["watch_id"])
        self.assertNotEqual(fields["watch_id"], dead["watch_id"])
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["rearmed_from"], dead["watch_id"])
        self.assertEqual(new_arm["rearm_count"], 1)

        self._release()
        self._release()


class ReviewRoundOneTest(_WatchMixin, unittest.TestCase):
    def test_rearm_keeps_the_original_steward_identity_without_env(self):
        # M1: a rearm issued from a process that carries no session id (the
        # hook) must not rewrite steward.session_id to "".
        dead = self._fields(self._run("watch", "peer-a", env=self._env("held")).stdout)
        os.kill(int(dead["pid"]), signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and peer_steward._pid_identity_ok(
            int(dead["pid"]), dead["pid_start"]
        ):
            time.sleep(0.02)
        env = self._env("held")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        out = self._run("rearm", dead["watch_id"], env=env).stdout
        fields = self._fields(out)
        self.assertEqual(fields["state"], "rearmed", out)
        new_arm = json.loads((self.watch_root / f"{fields['watch_id']}.json").read_text())
        self.assertEqual(new_arm["steward"]["session_id"], "steward-1")
        self.assertEqual(new_arm["steward"]["harness"], "claude")
        # Same dedupe key: a third `watch` for the target is `already-armed`, not a second watcher.
        again = self._run("watch", "peer-a", env=self._env("held"))
        self.assertIn("state=already-armed", again.stdout)
        self._release()

    def test_hanging_herdr_get_is_herdr_unavailable_not_a_hang(self):
        # M3: a wedged `herdr agent get` must return exit 4 within the bound.
        env = self._env("hang-get")
        env["AGENT_PEER_STEWARD_HERDR_GET_TIMEOUT"] = "1"
        started = time.monotonic()
        proc = self._run("watch", "peer-a", env=env, timeout=30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(proc.returncode, 4, proc.stdout + proc.stderr)
        self.assertIn("herdr-unavailable", proc.stdout)
        self.assertEqual(list(self.watch_root.glob("*.json")), [])

    def test_receipt_carries_the_real_herdr_exit_code(self):
        # m3: a timeout from herdr exits 1; the receipt must say so.
        armed = self._fields(self._run("watch", "peer-a", env=self._env("timeout")).stdout)
        receipt = self._wait_for_receipt(armed["watch_id"])
        self.assertEqual(receipt["state"], "timeout")
        self.assertEqual(receipt["herdr_exit"], 1)


class KillFixtureTest(_WatchMixin, unittest.TestCase):
    """A56-7 — the smallest falsifying test for the whole direction."""

    def _spawn_caller(self, subcommand):
        script = (
            "import subprocess,sys;"
            f"p=subprocess.run([sys.executable,{str(_HERE / 'peer-steward.py')!r},"
            f"{subcommand!r},'peer-a'],capture_output=True,text=True);"
            "open(%r,'w').write(p.stdout);" % str(self.tmp_root / "caller.out")
            + "import time;time.sleep(300)"
        )
        return subprocess.Popen([sys.executable, "-c", script], env=self._env("held"),
                                start_new_session=True)

    def test_watcher_survives_sigkill_of_the_callers_process_group(self):
        caller = self._spawn_caller("watch")
        # Read the watcher identity from disk, not from the caller's stdout:
        # a killed caller's stdout may never be flushed.
        deadline = time.monotonic() + 30
        arm = None
        while time.monotonic() < deadline:
            arms = [p for p in self.watch_root.glob("*.json")] if self.watch_root.is_dir() else []
            if arms:
                arm = json.loads(arms[0].read_text())
                break
            time.sleep(0.02)
        self.assertIsNotNone(arm, "watch never armed")
        pid, pid_start = arm["watcher"]["pid"], arm["watcher"]["pid_start"]

        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)

        self.assertTrue(peer_steward._pid_identity_ok(pid, pid_start),
                        "the watcher must outlive its caller's process group")
        self._release()
        receipt = self._wait_for_receipt(arm["watch_id"])
        self.assertEqual(receipt["state"], "idle")

    def test_wait_by_contrast_loses_its_herdr_child(self):
        # The regression proof: the identical fixture against (9) `wait`.
        caller = self._spawn_caller("wait")
        deadline = time.monotonic() + 30
        child = None
        while time.monotonic() < deadline and child is None:
            for entry in Path("/proc").glob("[0-9]*"):
                try:
                    if entry.joinpath("comm").read_text().strip() != "herdr":
                        continue
                    stat = entry.joinpath("stat").read_text()
                    if str(caller.pid) in stat.split(")")[-1].split()[2:4]:
                        child = int(entry.name)
                        break
                except OSError:
                    continue
            if child is None:
                time.sleep(0.02)
        os.killpg(os.getpgid(caller.pid), signal.SIGKILL)
        caller.wait(timeout=10)
        if child is not None:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child}").exists(),
                             "`wait`'s herdr child must die with the caller's group")
        self.assertEqual(list(self.watch_root.glob("*.receipt.json"))
                         if self.watch_root.is_dir() else [], [],
                         "`wait` leaves no receipt -- that is the defect (10) fixes")
def _agent_json(harness, sid, name, status="idle"):
    return {"id": "cli:agent:get", "result": {"agent": {
        "agent": harness, "agent_status": status, "name": name, "pane_id": "w1:pX",
        "agent_session": ({"agent": harness, "kind": "id", "value": sid} if sid else None)}}}


class F100cPromptAndResolutionTest(_TmpRootMixin, unittest.TestCase):
    """F-100c — the steward send is `prompt`: herdr resolves the target's exact session
    id, the sender's registry name rides the record, and the body gets the trailer."""

    def setUp(self):
        super().setUp()
        # These transport fixtures now also provide a readable empty input box.
        # Draft parsing/races use actual ANSI reads in PromptDraftGuardTest below.
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        empty = peer_steward._screen_lines("❯\n────────────\n┃  Ask anything...\n┃  Build model\n")
        patch = mock.patch.object(peer_steward, "_read_screen", return_value=empty)
        patch.start()
        self.addCleanup(patch.stop)

    def _fake_run(self, get_payload, prompt_rc=0, calls=None):
        def run(argv, **kw):
            if calls is not None:
                calls.append(argv)
            if argv[:3] == ["herdr", "agent", "get"]:
                return _herdr_json(get_payload)
            if argv[:3] == ["herdr", "agent", "prompt"]:
                return subprocess.CompletedProcess(argv, prompt_rc, stdout="{}", stderr="")
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json({"id": "cli:agent:start", "result": {"agent": get_payload["result"]["agent"]}})
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        return run

    def test_prompt_resolves_target_appends_trailer_and_records_exact_ids(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(_agent_json("codex", "thread-9", "child"), calls=calls)), \
             mock.patch.object(peer_steward.peer_message, "claude_session_name", return_value="hearting-46"), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[handoff] do the thing\nline two"])
        self.assertEqual(rc, 0)
        prompt_call = next(c for c in calls if c[:3] == ["herdr", "agent", "prompt"])
        self.assertEqual(prompt_call[3], "child")
        self.assertIn("(peer-from: claude [?] hearting-46 ; ref=", prompt_call[4])
        self.assertNotIn("sid-steward", prompt_call[4])
        self.assertTrue(prompt_call[4].startswith("[handoff] do the thing\nline two"))
        rec = self._all_records()[-1]
        self.assertEqual(rec["kind"], "handoff")
        self.assertEqual(rec["to"], {"harness": "codex", "session_id": "thread-9", "name": "child", "pane": "w1:pX"})
        self.assertEqual(rec["from"]["name"], "hearting-46")
        self.assertEqual(rec["summary"], "[handoff] do the thing")
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=true", line)
        self.assertIn("to_alias=", line)
        self.assertNotIn("thread-9", line)
        # sending a handoff is a message, not a steward act: no marker (user 2026-09-06)
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})
        self.assertFalse(peer_steward.peer_message.steward_marker_path("claude", "sid-steward").exists())

    def test_consecutive_prompt_captures_sender_and_recipient_once(self):
        calls = []
        senders = [("sender-first", "codex"), ("sender-second", "opencode")]
        targets = [("codex", "recipient-first", "same-name"),
                   ("opencode", "recipient-second", "same-name")]

        def sent(target, text, **kwargs):
            calls.append(text)
            # Ambient sender changes during submission cannot alter this send's ledger.
            os.environ["CODEX_THREAD_ID"] = "unrelated-ambient"
            return 0, {}

        with mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_current_session_identity", side_effect=senders) as identity, \
             mock.patch.object(peer_steward, "_resolve_target", side_effect=targets) as target, \
             mock.patch.object(peer_steward, "_agent_state", return_value=("idle", "w1:p1")), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=sent), \
             mock.patch("builtins.print"):
            for body in ("첫 전송", "둘째 전송"):
                self.assertEqual(peer_steward.main(["prompt", "same-name", body, "--no-verify"]), 0)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual(target.call_count, 2)
        rows = {r["from"]["session_id"]: r for r in self._all_records()}
        for i, text in enumerate(calls):
            row = rows[senders[i][0]]
            self.assertEqual(row["to"]["session_id"], targets[i][1])
            self.assertEqual(row["body_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(row["message_id"], row["transfer_ref"])
            pm = peer_steward.peer_message
            metadata = json.loads(pm._transfer_path(row["transfer_ref"]).read_text())
            self.assertEqual(metadata["from"]["session_id"], row["from"]["session_id"])
            self.assertEqual(metadata["to"]["session_id"], row["to"]["session_id"])
            self.assertEqual(metadata["body_sha256"], row["body_sha256"])
            actual = {"harness": targets[i][0], "session_id": targets[i][1]}
            self.assertEqual(pm.parse_peer_trailer(text, actual)["session_id"], senders[i][0])
            self.assertIsNone(pm.parse_peer_trailer(calls[1-i], actual)["session_id"])

    def test_prompt_without_trailer_flag_and_unresolvable_target(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run({"error": {"code": "agent_not_found"}}, calls=calls)), \
             mock.patch("builtins.print"):
            rc = peer_steward.main(["prompt", "ghost", "hello", "--no-trailer"])
        self.assertEqual(rc, 0)
        prompt_call = next(c for c in calls if c[:3] == ["herdr", "agent", "prompt"])
        self.assertEqual(prompt_call[4], "hello")
        rec = self._all_records()[-1]
        self.assertEqual(rec["to"], {"harness": "unknown", "name": "ghost"})
        self.assertEqual(rec["kind"], "steer")

    def _verify_run(self, status, *, prompt_rc=0, prompt_err="", explain_evidence="\"❯\\n\"", calls=None,
                    pane_text="❯ ", explain_stdout=None):
        """herdr as measured 2026-09-06: an idle Claude pane is explained by
        `live_prompt_box (region=prompt_box_body)` with the box as evidence; a
        working pane by `osc_title_working (region=osc_title)` with the terminal
        title; `explain_stdout` overrides both (e.g. OpenCode's `rule: none`)."""
        get_payload = _agent_json("claude", "sid-child", "child", status=status)
        if explain_stdout is None:
            if status == "working":
                explain_stdout = ("agent: claude\nstate: working\nrule: osc_title_working (region=osc_title priority=1100)\n"
                                  "evidence: \"◐ some title\"\n")
            else:
                explain_stdout = (f"agent: claude\nstate: {status}\nrule: live_prompt_box (region=prompt_box_body priority=950)\n"
                                  f"evidence: {explain_evidence}\n")
        def run(argv, **kw):
            if calls is not None:
                calls.append(argv)
            if argv[:3] == ["herdr", "agent", "get"]:
                return _herdr_json(get_payload)
            if argv[:3] == ["herdr", "agent", "read"]:
                return subprocess.CompletedProcess(argv, 0, stdout=pane_text, stderr="")
            if argv[:3] == ["herdr", "agent", "prompt"]:
                return subprocess.CompletedProcess(argv, prompt_rc, stdout="{}" if prompt_rc == 0 else "",
                                                   stderr=prompt_err)
            if argv[:3] == ["herdr", "agent", "explain"]:
                return subprocess.CompletedProcess(argv, 0, stdout=explain_stdout, stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")
        return run

    def test_prompt_to_an_idle_target_waits_for_the_state_flip(self):
        """SD-122 (11): `prompted=true` only after herdr observed the submission."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("idle", calls=calls)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 0)
        prompt_call = next(c for c in calls if c[:3] == ["herdr", "agent", "prompt"])
        self.assertEqual(prompt_call[5:], ["--wait", "--until", "working", "--timeout", "8000"])
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=true", line)
        self.assertIn("state_before=idle verify=state-flip", line)
        rec = self._all_records()[-1]
        self.assertEqual(rec["delivery"]["status"], "sent")
        # SD-122 (11): the ledger row is the caller attribution herdr's own log lacks
        self.assertEqual(rec["to"]["pane"], "w1:pX")
        self.assertIn("prompted=true state_before=idle verify=state-flip herdr_rc=0", rec["delivery"]["receipt"])
        self.assertEqual(rec["from"]["session_id"], "sid-steward")
        self.assertTrue(rec["body_sha256"])

    def test_prompt_stalled_is_a_typed_failure_not_a_success(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        stalled = json.dumps({"error": {"code": "agent_prompt_stalled", "message": "no state change"}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", prompt_rc=1, prompt_err=stalled)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 1)
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=failed", line)
        self.assertIn("reason=agent-prompt-stalled", line)
        self.assertEqual(self._all_records()[-1]["delivery"]["status"], "failed")

    def test_prompt_to_a_working_target_is_unverified_unless_the_transcript_shows_it(self):
        """Review round 1, B2: a working Claude pane is explained by its terminal
        title, never by the prompt box, so a box that was not read is not
        "clear". The transcript is the ground truth; without it the verdict is
        `unverified` (exit 5, ledger `unknown`), never `true`."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_transcript_arrival", return_value=None), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("working", calls=calls)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[handoff] all done, merge it"])
        self.assertEqual(rc, 5)
        prompt_call = next(c for c in calls if c[:3] == ["herdr", "agent", "prompt"])
        self.assertNotIn("--wait", prompt_call, "a working target is never waited on for a flip")
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=unverified", line)
        self.assertIn("verify=prompt-box-unavailable", line)
        self.assertIn("reason=submission-not-observed", line)
        rec = self._all_records()[-1]
        self.assertEqual(rec["delivery"]["status"], "unknown")
        self.assertIn("prompted=unverified", rec["delivery"]["receipt"])
        calls.clear()
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_transcript_arrival", return_value="2026-09-06T05:00:00.000Z"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("working", calls=calls)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[handoff] all done, merge it"])
        self.assertEqual(rc, 0)
        self.assertIn("prompted=true", print_mock.call_args[0][0])
        self.assertIn("state_before=working verify=transcript-arrival", print_mock.call_args[0][0])

    def test_prompt_timeout_with_our_text_still_in_the_box_is_queued(self):
        """herdr `timeout` (state changed, no `working` within the bound) and a
        *read* box still showing our first line: `queued`, without Enter."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        timeout = json.dumps({"error": {"code": "timeout"}})
        residue = '"❯\\u{a0}[handoff] all done, merge it, then release the gate\\n"'
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_transcript_arrival", return_value=None), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", prompt_rc=1, prompt_err=timeout,
                                                            explain_evidence=residue, calls=calls)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[handoff] all done, merge it, then release the gate"])
        self.assertEqual(rc, 3)
        self.assertEqual([c for c in calls if c[:3] == ["herdr", "agent", "send-keys"]],
                         [])
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=queued", line)
        self.assertIn("reason=prompt-box-residue", line)
        self.assertEqual(self._all_records()[-1]["delivery"]["status"], "unknown")

    def test_prompt_timeout_with_no_box_read_is_unverified_not_true(self):
        """Review round 1, B3: `rule: none` (OpenCode's normal state) has no
        `evidence:` line; a herdr `timeout` must then be `unverified`."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        timeout = json.dumps({"error": {"code": "timeout"}})
        none = "agent: opencode\nstate: idle\nrule: none\nfallback_reason: default_known_agent_idle_fallback\n"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward, "_transcript_arrival", return_value=None), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", prompt_rc=1, prompt_err=timeout, explain_stdout=none)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 5)
        self.assertIn("prompted=unverified", print_mock.call_args[0][0])
        self.assertIn("verify=prompt-box-unavailable", print_mock.call_args[0][0])

    def test_prompt_verify_timeout_is_clamped_to_herdr_stall_bound(self):
        """Review round 1, minor 4: below 5000 ms herdr answers `timeout` instead
        of `agent_prompt_stalled`, hiding a real stall."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("idle", calls=calls)), \
             mock.patch("builtins.print"):
            peer_steward.main(["prompt", "child", "[steer] go", "--verify-timeout-ms", "1000"])
        prompt_call = next(c for c in calls if c[:3] == ["herdr", "agent", "prompt"])
        self.assertEqual(prompt_call[-2:], ["--timeout", "5000"])

    def test_prompt_refuses_a_blocked_target_or_an_open_form(self):
        """Measured 2026-09-06 (3/3): text injected into an open AskUserQuestion
        form is lost and the Enter answers it with the default. It is now
        private pending; no keystrokes or fictional received notice."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = "Probe: pick one?\n❯ 1. A\n  2. B\nEnter to select · ↑/↓ to navigate · Esc to cancel\n"
        for status, pane_text in (("blocked", "❯ "), ("idle", form), ("working", form)):
            with self.subTest(status=status):
                calls = []
                with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
                     mock.patch.object(peer_steward.subprocess, "run",
                                       side_effect=self._verify_run(status, calls=calls, pane_text=pane_text)), \
                     mock.patch("builtins.print") as print_mock:
                    rc = peer_steward.main(["prompt", "child", "[steer] go"])
                self.assertEqual(rc, 3)
                self.assertFalse([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]],
                                 "nothing may be typed into an open form")
                self.assertIn("prompted=queued", print_mock.call_args[0][0])
                self.assertIn("reason=target-form-open", print_mock.call_args[0][0])
                rec = self._all_records()[-1]
                self.assertEqual(rec["delivery"]["status"], "unknown")
                self.assertIn("reason=target-form-open", rec["delivery"]["receipt"])
                self.assertEqual(rec["to"]["pane"], "w1:pX")


    def test_form_closed_normal_retry_reuses_ref_and_actual_receiver_ack(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        refs = []
        with mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", "recipient", "child")), \
             mock.patch.object(peer_steward, "_from_name", return_value="sender"), \
             mock.patch.object(peer_steward, "_agent_state", side_effect=[("blocked", "w1:pX"), ("idle", "w1:pX")]), \
             mock.patch.object(peer_steward, "_form_open", return_value=False), \
             mock.patch.object(peer_steward, "_herdr_prompt", return_value=(0, {})) as sent, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "original body", "--no-verify"]), 3)
            refs.append(self._all_records()[-1]["transfer_ref"])
            sent.assert_not_called() # Even --no-verify never types into a form.
            self.assertEqual(peer_steward.main(["prompt", "child", "original body"]), 0)
            refs.append(self._all_records()[-1]["transfer_ref"])
        self.assertEqual(refs[0], refs[1])
        # The retry carries the same content as the stranded row, so the
        # flush leaves it to the normal path: exactly one send, same ref.
        sent.assert_called_once()
        text = sent.call_args.args[1]
        self.assertEqual(peer_steward.peer_message._read_pending(refs[0])["state"], "received",
                         "the observed retry closes the row even without a receiver hook")
        peer_steward.peer_message.receive_peer_message(text, {"harness": "claude", "session_id": "recipient"})
        self.assertEqual(peer_steward.peer_message._read_pending(refs[0])["state"], "received")

    def test_idle_transcript_mentioning_a_form_stays_receivable(self):
        """2026-10-07 supervisor stall: idle sends queued on transcript
        tokens with no live form. Quoted UI words above the bottom window
        must not withhold keystrokes (a quote inside the bottom window only
        delays the send now; the flush redelivers it instead of stranding)."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        quoted = ("Review note: the old AskUserQuestion footer said esc to cancel "
                  "when done\n" + "\n".join(f"filler line {i}" for i in range(20)) + "\n❯ ")
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", calls=calls, pane_text=quoted)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 0)
        self.assertTrue([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]])
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=true", line)
        self.assertNotIn("queued", line)

    def test_live_bottom_ui_still_withholds_keystrokes(self):
        """The narrowed gate must still catch a real form: options plus
        footer as the trailing block, cursor included."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = ("Background work is running\n❯ 1. Exit and stop tasks\n"
                "  2. Move to background and exit\n  3. Stay\n"
                "Enter to confirm · Esc to cancel\n❯ ")
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", calls=calls, pane_text=form)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 3)
        self.assertFalse([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]])
        self.assertIn("prompted=queued", print_mock.call_args[0][0])

    def test_narrow_wrapped_footer_still_withholds_keystrokes(self):
        """A narrow pane wraps the footer across lines; joined bottom lines
        still reassemble the tokens, so a real form is never typed into."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = ("Pick one?\n❯ 1. Allow\n  2. Deny\nEnter to\nselect · Esc to\ncancel\n❯ ")
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", calls=calls, pane_text=form)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go"])
        self.assertEqual(rc, 3)
        self.assertFalse([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]])
        self.assertIn("prompted=queued", print_mock.call_args[0][0])

    def test_retry_with_same_content_sends_once(self):
        """Retrying the stranded content goes through the normal path's row
        reuse: the flush skips it, so the content arrives exactly once."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = "Probe: pick one?\n❯ 1. A\n  2. B\nEnter to select · Esc to cancel\n"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("blocked", pane_text=form)), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "same body"]), 3)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")) as run_mock, \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "same body"]), 0)
            prompts = [c.args[0] for c in run_mock.call_args_list
                       if c.args[0][:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(len(prompts), 1)

    def test_other_sender_same_body_sends_twice(self):
        """A same-body row from another sender is flushed AND sent anew: the
        normal path mints a separate row, so skipping by content alone would
        strand the old row."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        peer_steward.peer_message.prepare_peer_message(
            "shared body",
            {"harness": "claude", "session_id": "sid-other", "name": "other"},
            {"harness": "claude", "session_id": "sid-child", "name": "child"},
            defer=True, refs=[])
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")) as run_mock, \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "shared body"]), 0)
            prompts = [c.args[0] for c in run_mock.call_args_list
                       if c.args[0][:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all("shared body" in argv[4] for argv in prompts))

    def test_flush_delivers_a_stranded_row_then_skips_it_once_acked(self):
        """A later receivable prompt resends deferred rows first and closes
        the observed row itself, even when no receiver hook runs."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = "Probe: pick one?\n❯ 1. A\n  2. B\nEnter to select · Esc to cancel\n"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("blocked", pane_text=form)), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "stranded body"]), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "pending")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")) as run_mock, \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "fresh body"]), 0)
            sent_texts = [c.args[0][4] for c in run_mock.call_args_list
                          if c.args[0][:3] == ["herdr", "agent", "prompt"]]
        self.assertTrue(any("stranded body" in text for text in sent_texts),
                        "the stranded row text must go out before the new send")
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received",
                         "the observed send must close the row, not wait for a hook")
        stranded = next(text for text in sent_texts if "stranded body" in text)
        peer_steward.peer_message.receive_peer_message(
            stranded, {"harness": "claude", "session_id": "sid-child"})
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")) as run_mock2, \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "third body"]), 0)
            sent_texts2 = [" ".join(c[3:]) for c in run_mock2.call_args_list
                           for c in [c.args[0]] if c[:3] == ["herdr", "agent", "prompt"]]
        self.assertFalse(any("stranded body" in text for text in sent_texts2),
                         "an acked row must never be resent")

    def _deferred_claude_row(self, body):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        text, ref = peer_steward.peer_message.prepare_peer_message(
            body, {"harness": "claude", "session_id": "sid-steward", "name": "sender"},
            {"harness": "claude", "session_id": "sid-child", "name": "child"}, defer=True)
        return text, ref

    def test_busy_redelivery_observes_pasted_queue_without_hook_ack(self):
        text, ref = self._deferred_claude_row("긴 지연 원문 " * 50)
        transcript = Path(os.environ["HOME"]) / ".claude/projects/-fixture/sid-child.jsonl"
        transcript.parent.mkdir(parents=True)
        fake = self._verify_run("working", pane_text="❯ ")
        def send(argv, **kw):
            if argv[:3] == ["herdr", "agent", "prompt"]:
                self.assertEqual(argv[4], text)
                transcript.write_text(json.dumps({
                    "type": "queue-operation", "operation": "enqueue",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "sessionId": "sid-child",
                    "content": '<pasted_content id="fe20">\n' + text + '\n</pasted_content id="fe20">',
                }, ensure_ascii=False) + "\n")
            return fake(argv, **kw)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=send), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "working")[0], 1)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_redelivery_claim_blocks_reentrant_flush(self):
        text, ref = self._deferred_claude_row("one deferred row")
        fake = self._verify_run("idle", pane_text="❯ ")
        def send(argv, **kw):
            if argv[:3] == ["herdr", "agent", "prompt"]:
                self.assertEqual(argv[4], text)
                self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "unverified")
                self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "idle")[0], 0)
            return fake(argv, **kw)
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=send) as calls, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "idle")[0], 1)
        prompts = [call.args[0] for call in calls.call_args_list if call.args[0][:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(len(prompts), 1)

    def test_ambiguous_redelivery_is_preserved_but_not_submitted_twice(self):
        text, ref = self._deferred_claude_row("unobserved row")
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("working", pane_text="❯ ")) as calls, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "working")[0], 0)
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "working")[0], 0)
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(row["state"], "unverified")
        self.assertEqual(row["text"], text)
        prompts = [call.args[0] for call in calls.call_args_list if call.args[0][:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        wrapped = '<pasted_content id="fe20">\n' + text + '\n</pasted_content id="fe20">'
        peer_steward.peer_message.receive_peer_message(wrapped, {"harness": "claude", "session_id": "sid-child"})
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_late_flush_never_leaves_a_separate_banner_in_the_draft(self):
        text, ref = self._deferred_claude_row("body submitted together")
        row = peer_steward.peer_message._read_pending(ref)
        with peer_steward.peer_message.pending_lock(ref):
            peer_steward.peer_message._save_pending(dict(row, created=row["created"] - 7200))
        sent = []
        def input_reason(*args):
            return "target-draft" if sent else None
        def send(*args, **kwargs):
            sent.append(args[1])
            return 0, {}
        with mock.patch.object(peer_steward, "_agent_state", return_value=("idle", "w1:pX")), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", "sid-child", "child")), \
             mock.patch.object(peer_steward, "_prompt_input_reason", side_effect=input_reason), \
             mock.patch.object(peer_steward, "_herdr_prompt", side_effect=send), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", "sid-child", "idle")[0], 1)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("[지연 전달"))
        self.assertTrue(sent[0].endswith(text))
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_late_flush_sends_delay_banner_with_row_text(self):
        """A row stranded over an hour goes out intact preceded by a delay
        notice in the same prompt, so the recipient sees its age first."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = "Probe: pick one?\n❯ 1. A\n  2. B\nEnter to select · Esc to cancel\n"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("blocked", pane_text=form)), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "old body"]), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        with peer_steward.peer_message.pending_lock(ref):
            peer_steward.peer_message._save_pending(dict(row, created=row["created"] - 7200))
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")) as run_mock, \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "fresh body"]), 0)
            sent_texts = [c.args[0][4] for c in run_mock.call_args_list
                          if c.args[0][:3] == ["herdr", "agent", "prompt"]]
        banners = [text for text in sent_texts if text.startswith("[지연 전달")]
        self.assertEqual(len(banners), 1)
        self.assertIn(ref[:8], banners[0])
        stranded = next(text for text in sent_texts if "old body" in text)
        self.assertEqual(banners[0], stranded)
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_long_stuck_rows_warn_with_senders(self):
        """Rows stranded over an hour name their senders on the next prompt
        so the original sender learns without any new gate or input."""
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        form = "Probe: pick one?\n❯ 1. A\n  2. B\nEnter to select · Esc to cancel\n"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("blocked", pane_text=form)), \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "child", "old body"]), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        with peer_steward.peer_message.pending_lock(ref):
            peer_steward.peer_message._save_pending(dict(row, created=row["created"] - 7200))
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._verify_run("idle", pane_text="❯ ")), \
             mock.patch.object(peer_steward, "_FLUSH_ROW_TIMEOUT_S", 0), \
             mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["prompt", "child", "fresh body"]), 0)
        stderr_lines = [call.args[0] for call in print_mock.call_args_list
                        if call.kwargs.get("file") is sys.stderr]
        self.assertTrue(any("pending-stuck" in line and "oldest=2.0h" in line for line in stderr_lines),
                        stderr_lines)

    def test_form_unknown_target_keeps_private_unverified_without_attaching_sid(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sender"
        with mock.patch.object(peer_steward, "_herdr_missing", return_value=False), \
             mock.patch.object(peer_steward, "_resolve_target", return_value=("opencode", None, "same-name")), \
             mock.patch.object(peer_steward, "_agent_state", return_value=("blocked", "w1:pX")), \
             mock.patch.object(peer_steward, "_herdr_prompt") as sent, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["prompt", "same-name", "original body"]), 5)
        sent.assert_not_called()
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        self.assertEqual(row["state"], "unverified")
        self.assertIsNone(row["to"]["session_id"])
        self.assertEqual(peer_steward.peer_message.pending_messages({"harness": "opencode", "session_id": "fork"}), [])
    def test_transcript_arrival_accepts_queued_rows_and_survives_a_stale_session_id(self):
        """Measured 2026-09-06 06:25Z: a mid-turn send lands in the target
        transcript as `queue-operation`/`enqueue` first; and herdr reported a
        stale session id for the steward pane, so recently written transcripts
        are scanned too."""
        import tempfile, time as _time
        home = tempfile.mkdtemp(); saved = os.environ.get("HOME"); os.environ["HOME"] = home
        try:
            proj = Path(home) / ".claude" / "projects" / "-proj"; proj.mkdir(parents=True)
            now = _time.time(); ts = _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(now))
            (proj / "real-sid.jsonl").write_text(json.dumps({
                "type": "queue-operation", "operation": "enqueue", "timestamp": ts,
                "sessionId": "real-sid", "content": "[handoff] merge it now — carrier(w1:p18)\n\nbody"}) + "\n",
                encoding="utf-8")
            (proj / "stale-sid.jsonl").write_text("", encoding="utf-8")
            old = now - 3600; os.utime(proj / "stale-sid.jsonl", (old, old))
            self.assertEqual(peer_steward._transcript_arrival("claude", "stale-sid", "[handoff] merge it now — carrier(w1:p18)", now - 5), ts)
            self.assertIsNone(peer_steward._transcript_arrival("claude", "stale-sid", "[handoff] something else", now - 5))
            self.assertIsNone(peer_steward._transcript_arrival("codex", "x", "[handoff] merge it now — carrier(w1:p18)", now - 5))
            (proj / "real-sid.jsonl").write_text(json.dumps({
                "type": "user", "timestamp": ts, "message": {"content": "[steer] plain user row"}}) + "\n", encoding="utf-8")
            self.assertEqual(peer_steward._transcript_arrival("claude", "real-sid", "[steer] plain user row", now - 5), ts)
        finally:
            if saved is None: os.environ.pop("HOME", None)
            else: os.environ["HOME"] = saved

    def test_only_peer_steward_types_into_panes(self):
        """SD-122 (11): every pane prompt goes through `peer-steward.py prompt` so
        the ledger row exists -- herdr's server log keeps no target and no
        caller. Asserted over the repo's code (not docs/tests): any other
        `herdr agent prompt|send-keys` / `herdr pane send-text|send-keys|run|close`
        caller is a defect. Census 2026-09-06: 0 outside this module."""
        import re
        root = (_HERE / "..").resolve()
        pattern = re.compile(r"herdr[\"', \[]+(agent|pane)[\"', ]+(prompt|send-text|send-keys|run|close)\b")
        offenders = []
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sh", ".js", ".mjs", ".ts", ".toml", ".yaml", ".json"}:
                continue
            rel = path.relative_to(root).as_posix()
            if any(part in rel for part in ("/dist/", "_scratch", ".agent_reports", "node_modules", ".test.", "/tests/")):
                continue
            if rel.startswith(("dist/", ".agent_reports/")) or path.name == "peer-steward.py":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if pattern.search(line) and not line.lstrip().startswith(("#", "//", "*", '"""', "'")):
                    # Recovery launches the verified executable in a newly
                    # created visible pane; it never prompts an existing agent.
                    # Pin this exact launch expression, not a whole-file exemption.
                    if (rel == "utilities/interactive-main-recovery.py"
                            and line.strip() == 'command = ["herdr", "pane", "run", created_pane,'):
                        import ast
                        launch = next(node for node in ast.walk(ast.parse(text))
                                      if isinstance(node, ast.Assign) and node.lineno == n)
                        expected = ast.parse(
                            '["herdr", "pane", "run", created_pane, '
                            'shlex.join([str(launcher), "--cd", workspace, *args.agent_args])]'
                        ).body[0].value
                        self.assertEqual(ast.dump(launch.value), ast.dump(expected))
                        continue
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "pane prompts must go through peer-steward.py prompt")

    def test_prompt_no_verify_keeps_the_legacy_exit_code_report(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        calls = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=self._verify_run("working", calls=calls)), \
             mock.patch("builtins.print") as print_mock:
            rc = peer_steward.main(["prompt", "child", "[steer] go", "--no-verify"])
        self.assertEqual(rc, 0)
        self.assertFalse([c for c in calls if c[:3] == ["herdr", "agent", "explain"]])
        self.assertIn("verify=none", print_mock.call_args[0][0])

    def test_prompt_box_residue_needs_our_text_beyond_the_kind_prefix(self):
        """Claude Code 2.1.263 renders a predicted next prompt in the empty box
        (`❯ [handoff] 후속 결과 확인 — steward-carrier…`). Residue is decided on
        the text after the `[kind]` prefix, at least 24 characters of it; a
        prediction identical to our own text is undecidable here and is settled
        by the transcript first (review round 1, M4)."""
        ghost = '"❯\\u{a0}[handoff] 후속 결과 확인 — steward-carrier…\\n"'
        self.assertFalse(peer_steward._prompt_box_residue(ghost, "[steer] 리뷰 끝나면 handoff 보내라"))
        self.assertFalse(peer_steward._prompt_box_residue(ghost, "[handoff] 후속 결과 확인은 끝났다 — 다음은 병합"))
        self.assertFalse(peer_steward._prompt_box_residue('"❯\\n"', "[steer] anything"))
        self.assertTrue(peer_steward._prompt_box_residue(ghost, "[handoff] 후속 결과 확인 — steward-carrier 후속 4건"))
        # a narrow pane truncates the box: fewer than 24 characters after the prefix is undecidable, not residue
        self.assertFalse(peer_steward._prompt_box_residue('"❯[handoff] all done, m…"', "[handoff] all done, merge it"))
        self.assertTrue(peer_steward._prompt_box_residue(
            '"❯[handoff] all done, merge it, then release …"', "[handoff] all done, merge it, then release the gate"))


class PromptDraftGuardTest(_TmpRootMixin, unittest.TestCase):
    """Actual 0.8 CLI/screen fixtures: user input survives; exact payload goes once."""

    def setUp(self):
        super().setUp()
        for key in ("AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS",
                    "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
            os.environ.pop(key, None)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        os.environ["HERDR_PANE_ID"] = "w1:pX"
        self.harness, self.sid, self.status = "claude", "sid-child", "idle"
        self.screen, self.messages, self.keys = CLAUDE_EMPTY, [], []
        self.enterContext(mock.patch.object(peer_steward.shutil, "which", return_value="/fixture/herdr"))
        self.enterContext(mock.patch.object(peer_steward.subprocess, "run", side_effect=self.run_herdr))
        self.enterContext(mock.patch("builtins.print"))

    def run_herdr(self, argv, **kwargs):
        if argv[:3] in (["herdr", "agent", "get"], ["herdr", "agent", "wait"]):
            return _herdr_json(_agent_json(self.harness, self.sid, "child", self.status))
        if argv[:3] == ["herdr", "agent", "read"]:
            return subprocess.CompletedProcess(argv, 1 if self.screen is None else 0,
                                               stdout=self.screen or "", stderr="")
        if argv[:3] == ["herdr", "agent", "prompt"]:
            self.messages.append(argv[4])
        if argv[:3] == ["herdr", "agent", "send-keys"]:
            self.keys.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def prompt(self, body="peer body"):
        return peer_steward.main(["prompt", "child", body])

    def idle(self):
        # Run the actual existing receive callback entry, with native identity
        # isolated to this fixture. Its internal claims/acks are not mocked.
        with mock.patch("fleet.herdr_projection.may_report", return_value=True):
            peer_steward.peer_message.retry_receiver_idle({"harness": self.harness, "session_id": self.sid})

    def test_today_two_collision_shapes_after_preparation_preserve_draft_and_deliver_once(self):
        prepare = peer_steward.peer_message.prepare_peer_message
        for draft in ("그리고 토큰 효율을 위해서 좀 50% 넘으면 작업 세션들 주기적으로",
                      "산출물 이동 하나 하는게 왜이렇게 오래 걸리는거임?"):
            with self.subTest(draft=draft):
                self.screen = CLAUDE_EMPTY
                before = len(self.messages)
                def user_types(*args, **kwargs):
                    result = prepare(*args, **kwargs)
                    self.screen = RULE + "\n❯ " + draft + "\n" + RULE + "\n"
                    return result
                with mock.patch.object(peer_steward.peer_message, "prepare_peer_message", side_effect=user_types):
                    self.assertEqual(self.prompt("peer " + draft), 3)
                original = self.screen
                ref = self._all_records()[-1]["transfer_ref"]
                row = peer_steward.peer_message._read_pending(ref)
                self.assertEqual((row["state"], row["receipt"]), ("pending", "target-draft"))
                self.assertEqual(len(self.messages), before)
                self.idle()                   # Still a draft: no input, no Enter.
                self.assertEqual(self.screen, original)
                self.assertEqual(self.keys, [])
                self.assertEqual(len(self.messages), before)
                self.screen = CLAUDE_EMPTY
                self.idle()
                self.idle()                   # No duplicate after acknowledgement.
                self.assertEqual(self.messages[before:], [row["text"]])
                self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_claude_and_opencode_draft_or_unreadable_box_wait_but_empty_box_sends(self):
        for harness, draft, empty in (("claude", CLAUDE_DRAFT, CLAUDE_EMPTY),
                                      ("opencode", OPENCODE_DRAFT, OPENCODE_HOME)):
            for screen in (draft, None, NO_BOX):
                with self.subTest(harness=harness, screen=screen):
                    self.harness, self.screen = harness, screen
                    before = len(self.messages)
                    body = harness + str(screen)
                    self.assertEqual(self.prompt(body), 3)
                    ref = self._all_records()[-1]["transfer_ref"]
                    row = peer_steward.peer_message._read_pending(ref)
                    self.assertEqual(row["state"], "pending")
                    self.assertEqual(len(self.messages), before)
                    self.screen = empty
                    self.idle()
                    self.assertEqual(self.messages[before:], [row["text"]])
            self.screen = empty
            before = len(self.messages)
            self.assertEqual(self.prompt(harness + " direct"), 0)
            self.assertEqual(len(self.messages), before + 1)

    def test_draft_after_pending_claim_releases_only_unsent_claim(self):
        self.screen = CLAUDE_DRAFT
        self.assertEqual(self.prompt(), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        row = peer_steward.peer_message._read_pending(ref)
        claim = peer_steward.peer_message.claim_pending_herdr
        self.screen = CLAUDE_EMPTY
        def user_types(*args, **kwargs):
            result = claim(*args, **kwargs)
            self.screen = CLAUDE_DRAFT
            return result
        with mock.patch.object(peer_steward.peer_message, "claim_pending_herdr", side_effect=user_types):
            self.assertEqual(peer_steward._flush_pending_for_target("child", "claude", self.sid, "idle")[0], 0)
        held = peer_steward.peer_message._read_pending(ref)
        self.assertEqual((held["state"], held["rpc_claim"]), ("pending", None))
        self.assertEqual(self.messages, [])
        self.screen = CLAUDE_EMPTY
        self.idle()
        self.assertEqual(self.messages, [row["text"]])

    def test_foreign_callback_and_changed_sid_cannot_drain_pending(self):
        self.screen = CLAUDE_DRAFT
        self.assertEqual(self.prompt(), 3)
        self.screen = CLAUDE_EMPTY
        with mock.patch("fleet.herdr_projection.may_report", return_value=False):
            peer_steward.peer_message.retry_receiver_idle({"harness": "claude", "session_id": self.sid})
        self.sid = "another-session"
        self.idle()
        with mock.patch("fleet.herdr_projection.may_report", return_value=True):
            peer_steward.peer_message.retry_receiver_idle({"harness": "claude", "session_id": "sid-child"})
        self.assertEqual(self.messages, [])

    def test_existing_claude_stop_hook_waits_for_empty_box_and_delivers_once(self):
        import io
        path = _HERE.parent / "hooks" / "herdr-session-projection.py"
        spec = importlib.util.spec_from_file_location("_draft_guard_stop_hook", path)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        payload = json.dumps({"session_id": self.sid, "hook_event_name": "Stop"})
        self.screen = CLAUDE_DRAFT
        self.assertEqual(self.prompt(), 3)
        ref = self._all_records()[-1]["transfer_ref"]
        text = peer_steward.peer_message._read_pending(ref)["text"]
        with mock.patch("fleet.herdr_projection.project"), \
             mock.patch("fleet.herdr_projection.may_report", return_value=True):
            for screen in (CLAUDE_DRAFT, CLAUDE_EMPTY, CLAUDE_EMPTY):
                self.screen = screen
                with mock.patch.object(sys, "stdin", io.StringIO(payload)):
                    self.assertEqual(hook.main(), 0)
                if screen == CLAUDE_DRAFT:
                    self.assertEqual(self.messages, [])
        self.assertEqual(self.messages, [text])
        self.assertEqual(self.keys, [])
        self.assertEqual(peer_steward.peer_message._read_pending(ref)["state"], "received")

    def test_codex_native_queue_is_not_subject_to_draft_reads(self):
        self.harness, self.status, self.screen = "codex", "blocked", CLAUDE_DRAFT
        with mock.patch.object(peer_steward, "_read_screen", side_effect=AssertionError("native queue needs no box")), \
             mock.patch.object(peer_steward.peer_message, "deliver_pending_codex",
                               return_value={"status": "queued", "reason": "native-queue-accepted"}) as queue:
            self.assertEqual(self.prompt(), 3)
            queue.assert_called_once()
        self.assertEqual((self.messages, self.keys), ([], []))


class F100cStewardModeTest(_TmpRootMixin, unittest.TestCase):
    def test_steward_on_off_raises_and_clears_the_flag(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 0)
        self.assertIn("steward=on", print_mock.call_args[0][0])
        markers = peer_steward.peer_message.read_steward_markers()
        self.assertIn(("claude", "sid-steward"), markers)
        entry = markers[("claude", "sid-steward")]["targets"]["-"]
        self.assertEqual((entry["kind"], entry["source"], entry["session_id"]), ("explicit", "explicit", None))
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "off"]), 0)
        self.assertIn("steward=off", print_mock.call_args[0][0])
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_steward_on_without_identity_is_a_typed_failure(self):
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 1)
        self.assertIn("reason=no-session-identity", print_mock.call_args[0][0])


class FromNameTest(_TmpRootMixin, unittest.TestCase):
    """C-3 — `_from_name` reads Claude via `claude_session_name` (unchanged) and
    Codex/OpenCode via `tools/fleet/session_registry.py` (B-1), the same lazy
    `tools/` sys.path trick `peer-message.py:412 steward_marker_roots` uses for
    non-Fleet consumers. Any other harness, or any failure along the way, is
    `None` — a name is never guessed."""

    def test_unknown_harness_and_missing_registry_record_are_none(self):
        self.assertIsNone(peer_steward._from_name("unknown", "sid-1"))
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            self.assertIsNone(peer_steward._from_name("codex", "no-such-session"))
            self.assertIsNone(peer_steward._from_name("opencode", "no-such-session"))

    def test_codex_and_opencode_resolve_via_session_registry(self):
        with tempfile.TemporaryDirectory() as registry_dir:
            os.environ["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            for harness, pid, sid, name in (
                ("codex", 111, "codex-sess-1", "hearting-codex-1"),
                ("opencode", 222, "oc-sess-1", "hearting-oc-1"),
            ):
                d = Path(registry_dir) / harness
                d.mkdir(parents=True)
                (d / ("%d.json" % pid)).write_text(json.dumps(
                    {"pid": pid, "sessionId": sid, "name": name, "harness": harness}))
                self.assertEqual(peer_steward._from_name(harness, sid), name)


class FromNameBareSubprocessTest(unittest.TestCase):
    """C-3 — the lazy `tools/` sys.path insert must work in the actual condition
    a real herdr-launched `peer-steward.py` subprocess runs under: a cwd outside
    the repository and no inherited `PYTHONPATH`. `test_importable_with_only_
    tools_on_sys_path` (B-1) proves this for `session_registry` alone; this
    proves the full `peer-steward.py::_from_name` codex path reaches it too."""

    def test_from_name_resolves_a_codex_name_in_a_bare_subprocess(self):
        with tempfile.TemporaryDirectory() as registry_dir, \
             tempfile.TemporaryDirectory() as outside_cwd:
            pid = 424242
            record_dir = Path(registry_dir) / "codex"
            record_dir.mkdir(parents=True)
            (record_dir / ("%d.json" % pid)).write_text(json.dumps({
                "pid": pid, "sessionId": "codex-sess-c3", "name": "hearting-codex-c3",
                "harness": "codex",
            }))
            script = (
                "import importlib.util\n"
                "spec = importlib.util.spec_from_file_location('peer_steward', %r)\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "print(mod._from_name('codex', 'codex-sess-c3') or '')\n"
            ) % str(_HERE / "peer-steward.py")
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env["FLEET_SESSION_REGISTRY_DIR"] = registry_dir
            proc = subprocess.run([sys.executable, "-c", script], cwd=outside_cwd,
                                  capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "hearting-codex-c3")


class CurrentSessionIdentityDelegationTest(unittest.TestCase):
    """F-<next> fleet-route-chain-r2 plan §3 B-3: `_current_session_identity` delegates to
    `dispatch_parent_completion.interactive_parent_identity` first, keeping the prior
    AGENT_SESSION_ID fallback for the ambiguous/unset case; a native id is never guessed."""

    _ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                 "CODEX_SESSION_ID", "OPENCODE_SESSION_ID", "AGENT_SESSION_ID",
                 "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS")

    def _clean_env(self, **overrides):
        env = {key: None for key in self._ENV_KEYS}
        env.update(overrides)
        return mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None},
                               clear=False)

    def setUp(self):
        for key in self._ENV_KEYS:
            os.environ.pop(key, None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self._ENV_KEYS])

    def test_explicit_caller_harness_wins(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-x", "codex"))

    def test_ambiguous_multiple_sessions_records_unknown_sender(self):
        # No explicit caller harness + two sessions set -> interactive_parent_identity()
        # raises caller-harness-ambiguous. The sender is unknown, never the first of the
        # claude > codex > opencode guesses (that named a Codex thread `claude [bc]`).
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_ambiguous_env_keeps_the_agent_session_id_fallback(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x",
                             AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_explicit_name_without_its_sid_never_takes_foreign_sid(self):
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", AGENT_DISPATCH_CALLER_HARNESS="codex"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_invalid_explicit_name_never_takes_a_native_sid(self):
        with self._clean_env(CODEX_THREAD_ID="sid-x", AGENT_DISPATCH_CALLER_HARNESS="gemini"):
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))

    def test_single_session_delegates_cleanly(self):
        with self._clean_env(CODEX_THREAD_ID="sid-solo"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-solo", "codex"))

    def test_agent_session_id_still_falls_back_to_unknown(self):
        with self._clean_env(AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_nothing_set_returns_empty_unknown(self):
        with self._clean_env():
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))


# --- clear (session-tidy auto-clear) ---------------------------------------------------

RULE = "─" * 40
CLAUDE_EMPTY = "\x1b[0m\x1b[38;2;80;80;80m❯ \x1b[0m\x1b[38;2;255;255;255m[earlier message]\x1b[0m\n\n" \
    + RULE + "\n❯ \r\n" + RULE + "\n  \U0001f4c1 proj │ main\n  bypass permissions on\n"
CLAUDE_SUGGESTION = "❯ earlier\n\n" + RULE + "\n❯ \x1b[2mrun the tests again\x1b[0m\n" + RULE + "\n  footer\n"
CLAUDE_DRAFT = "❯ earlier\n\n" + RULE + "\n❯ half typed text\n" + RULE + "\n  footer\n"
CLAUDE_DRAFT_SECOND_LINE = RULE + "\n❯ \x1b[2m\x1b[0m\n  second line of a draft\n" + RULE + "\n  footer\n"
CLAUDE_FORM = "Do you want to proceed?\n ❯ 1. Yes\n   2. No\n Esc to cancel · Tab to amend\n"
CODEX_EMPTY = "recap line\n\n\n\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n  proj · master · Context 85% used\n"
CODEX_DRAFT = "recap line\n\x1b[1m›\x1b[0m fix the flaky test\n\n  proj · master\n"
CODEX_POPUP = "recap line\n\x1b[1m›\x1b[0m \n  /clear   start a new chat\n  /compact summarize\n"
OPENCODE_EMPTY = "     ▣  Build · Model · 46m\n\n  ┃\n  ┃\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n   /path/to/project\n"
OPENCODE_WIDE_EMPTY = "     \u25a3  Build \u00b7 Model\n\n  \u2503" + " " * 60 + "\n  \u2503" + " " * 60 + "\n  \u2503" + " " * 120 + "/path/to/project:\n  \u2503  Build \u00b7 Model OpenCode Go" + " " * 80 + "main\n  \u2579\u2580\u2580\u2580\u2580\u2580\n"
OPENCODE_DRAFT = "     ▣  Build · Model\n\n  ┃  hello there\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n"
OPENCODE_HOME_BLANK = "\n" * 12 + "  /path/to/project:main\n\n"     # 1.18.34: nothing but the cwd line
OPENCODE_HOME = "\n\n  ┃  Ask anything... \"Fix broken tests\"\n  ┃\n  ┃  Build auto · Model OpenCode Go\n  ╹▀▀▀▀▀\n"
NO_BOX = "just some output\nwith no prompt box at all\n"
OLD_T = "01a0fbf0-3ad5-7501-9838-eb440504686e"
NEW_T = "01a0fbf0-d65e-7041-9744-c8f5300f35a3"


def codex_screen(footer):
    """A measured codex 0.160.0 bottom: the input box, one blank line, the status line, the hint line."""
    return ("• recap of the last turn\n\n\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n  "
            + footer + "\n  ← for agents · ? for shortcuts\n")


class _ClearWorld:
    """A fake herdr: one pane whose agent record and visible screen change as the test says."""

    def __init__(self, harness="claude", sid="sid-A", status="idle", pane="w1:pX", screens=None,
                 new_sid="sid-B", home_after=None):
        self.harness, self.sid, self.status, self.pane = harness, sid, status, pane
        self.screens = list(screens or [CLAUDE_EMPTY])
        self.new_sid, self.home_after = new_sid, home_after
        self.sent = False
        self.calls = []
        self.prompt_rc = 0
        self.reads = 0

    def agent(self):
        sid = self.new_sid if (self.sent and self.new_sid) else self.sid
        return {"agent": self.harness, "agent_status": self.status, "name": "w", "pane_id": self.pane,
                "agent_session": ({"agent": self.harness, "kind": "id", "value": sid}
                                  if self.harness != "opencode" else None)}

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["herdr", "agent", "get"] or argv[:3] == ["herdr", "agent", "wait"]:
            return _herdr_json({"id": "x", "result": {"agent": self.agent(), "type": "agent_info"}})
        if argv[:3] == ["herdr", "agent", "read"]:
            if self.sent and self.home_after is not None:
                screen = self.home_after
            else:
                screen = self.screens[min(self.reads, len(self.screens) - 1)]
                self.reads += 1
            return subprocess.CompletedProcess(argv, 0 if screen is not None else 1,
                                               stdout=screen or "", stderr="")
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return _herdr_json({"id": "x", "result": {"process_info": {"foreground_processes": [
                {"name": self.harness, "pid": 4242}]}, "type": "pane_process_info"}})
        if argv[:3] == ["herdr", "agent", "prompt"]:
            if self.prompt_rc == 0:
                self.sent = True
            return subprocess.CompletedProcess(argv, self.prompt_rc, stdout="{}", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    def typed(self):
        return [c for c in self.calls if c[:3] in (["herdr", "agent", "prompt"], ["herdr", "agent", "send-keys"])]


class ClearTest(_TmpRootMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear
        self.seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "claude", "")
        self.path = clear.reservation_path(self.seat.key)

    def book(self, harness="claude", sid="sid-A", pane="w1:pX", seq=0, deadline_in=600):
        st, clear = self.st, self.clear
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "reserved", "created": now, "deadline": now + deadline_in,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane, "harness": harness, "project_key": ""},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root),
                "card_generation": card["generation"], "prompt_seq": seq})
        self.path = clear.reservation_path(seat.key)
        return seat

    def clear_cmd(self, world, nonce="n0nce", extra=()):
        argv = ["clear", world.pane, "--request", str(self.path), *( ["--nonce", nonce] if nonce else [] ), *extra]
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(argv)
        return rc, (printed[-1] if printed else "")

    def rows(self):
        return [r for r in self._all_records() if r.get("kind") == "notice"]

    # -- success ------------------------------------------------------------------------

    def test_an_idle_claude_with_an_empty_box_gets_exactly_one_clear_and_it_is_true(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY])
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("cleared=true", line)
        self.assertIn("old_session=sid-A", line)
        self.assertIn("new_session=sid-B", line)
        prompts = [c for c in world.calls if c[:3] == ["herdr", "agent", "prompt"]]
        self.assertEqual(prompts, [["herdr", "agent", "prompt", "w1:pX", "/clear"]])   # no trailer, no wait
        self.assertEqual([c for c in world.calls if c[:3] == ["herdr", "agent", "send-keys"]], [])

    def test_a_claude_suggestion_is_an_empty_box_but_typed_text_is_not(self):
        for screen, expect in ((CLAUDE_SUGGESTION, "cleared=true"), (CLAUDE_DRAFT, "reason=draft"),
                               (CLAUDE_DRAFT_SECOND_LINE, "reason=draft")):
            with self.subTest(screen=screen[:30]):
                self.book()
                world = _ClearWorld(screens=[screen])
                rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(rc, 0 if expect == "cleared=true" else 3)
                self.assertEqual(len(world.typed()), 1 if rc == 0 else 0)

    def test_codex_placeholder_is_empty_a_draft_or_a_popup_is_not(self):
        for screen, expect in ((CODEX_EMPTY, "cleared=true"), (CODEX_DRAFT, "reason=draft"),
                               (CODEX_POPUP, "reason=draft-unknown")):
            with self.subTest(screen=screen[:40]):
                self.book(harness="codex", sid="thr-A")
                world = _ClearWorld(harness="codex", sid="thr-A", screens=[screen], new_sid="thr-B")
                rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1 if expect == "cleared=true" else 0)

    def test_opencode_types_new_and_the_home_screen_is_the_proof(self):
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_HOME)
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=true" in line), (0, True), line)
        self.assertEqual(world.typed(), [["herdr", "agent", "prompt", "w1:pX", "/new"]])
        for after, expect in ((OPENCODE_HOME_BLANK, "cleared=true"), (OPENCODE_EMPTY, "cleared=unverified"),
                              ("\n\n", "cleared=unverified")):
            with self.subTest(after=after[:20]):
                self.book(harness="opencode", sid="ses_A")
                world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                                    home_after=after)
                with mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1, line)
        # OpenCode's pane stays `done` through /new, so the pause between looks is a wait for the
        # screen (event-driven), not the idle wait that would return at once.
        self.book(harness="opencode", sid="ses_A")
        world = _ClearWorld(harness="opencode", sid="ses_A", screens=[OPENCODE_EMPTY], new_sid=None,
                            home_after=OPENCODE_EMPTY)
        with mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 3):
            rc, line = self.clear_cmd(world)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len([c for c in world.calls if c[:3] == ["herdr", "pane", "wait-output"]]), 2)
        for screen, reason in ((OPENCODE_DRAFT, "draft"), (NO_BOX, "draft-unknown")):
            self.book(harness="opencode", sid="ses_A")
            world = _ClearWorld(harness="opencode", sid="ses_A", screens=[screen], new_sid=None)
            rc, line = self.clear_cmd(world)
            self.assertEqual((rc, f"reason={reason}" in line, world.typed()), (3, True, []), line)

    # -- doubt is "not cleared": zero input ---------------------------------------------------

    def test_working_blocked_unreadable_and_form_screens_get_no_input(self):
        cases = (({"status": "working"}, "not-idle-working"), ({"status": "blocked"}, "form-open"),
                 ({"status": "unknown"}, "not-idle-unknown"), ({"screens": [None]}, "screen-unknown"),
                 ({"screens": [CLAUDE_FORM]}, "form-open"), ({"screens": [NO_BOX]}, "draft-unknown"))
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual(rc, 3, line)
                self.assertIn(f"reason={reason}", line)
                self.assertEqual(world.typed(), [])

    def test_a_draft_that_appears_between_the_two_looks_is_never_typed_over(self):
        self.book()
        world = _ClearWorld(screens=[CLAUDE_EMPTY, CLAUDE_DRAFT])      # the user starts typing after look 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=draft" in line, world.typed()), (3, True, []), line)

    def test_a_new_request_expiry_a_stale_nonce_or_a_finished_booking_get_no_input(self):
        seat = self.book(seq=0)
        with self.st.seat_lock(seat.key):
            self.st.bump_prompt_seq(seat, "claude", "sid-A", time.time())
        world = _ClearWorld()
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=new-input" in line), (3, True))
        self.book(deadline_in=-5)
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=expired" in line), (3, True))
        self.book()
        rc, line = self.clear_cmd(world, nonce="another")
        self.assertEqual((rc, "reason=superseded" in line), (3, True))
        self.assertEqual(world.typed(), [])
        self.assertEqual(self.rows(), [])                               # a dead request leaves no ledger row

    def test_the_target_must_be_the_booked_pane_harness_and_session(self):
        for kwargs, label in (({"harness": "codex"}, "harness"), ({"sid": "someone-else"}, "session")):
            with self.subTest(label):
                self.book()
                world = _ClearWorld(**kwargs)
                rc, line = self.clear_cmd(world)
                self.assertEqual((rc, "reason=target-changed" in line, world.typed()), (3, True, []), line)
        self.book()
        world = _ClearWorld()
        world.pane = "w9:pZ"
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "reason=target-changed" in line), (3, True))

    def test_the_new_session_is_the_process_one_when_herdr_reports_a_stale_session(self):
        for proof, expect in (("sid-NEW", "cleared=true"), ("sid-A", "cleared=unverified")):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-A", new_sid="sid-stale-older")   # herdr names an older session
                with mock.patch("fleet.collectors.claude.session_id_of_process", return_value=proof), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertIn(expect, line)
                if expect == "cleared=true":
                    self.assertIn("new_session=sid-NEW", line)

    def test_a_pane_record_that_lags_a_clear_is_settled_by_the_process_not_guessed(self):
        # herdr still reports the session before the previous /clear; the process knows better.
        for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
            with self.subTest(process_says=proof):
                self.book()
                world = _ClearWorld(sid="sid-before-the-last-clear", new_sid="sid-B")
                with mock.patch("fleet.collectors.claude.session_id_of_process",
                                side_effect=lambda pid: "sid-B" if world.sent else proof):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), expect_typed, line)
                self.assertEqual("cleared=true" in line, bool(expect_typed), line)
                if not expect_typed:
                    self.assertIn("reason=target-changed", line)

    def test_a_pane_record_without_a_session_id_is_cleared_only_when_the_process_names_the_booked_one(self):
        # herdr names no session (`-`): not a confirmed match -- only the process can vouch for it.
        for harness, collector in (("claude", "claude"), ("codex", "codex")):
            for proof, expect_typed in (("sid-A", 1), ("sid-other", 0), (None, 0)):
                with self.subTest(harness=harness, process_says=proof):
                    self.book(harness=harness)
                    screen = CLAUDE_EMPTY if harness == "claude" else CODEX_EMPTY
                    world = _ClearWorld(harness=harness, sid=None, new_sid=None, screens=[screen])
                    with mock.patch(f"fleet.collectors.{collector}.session_id_of_process", return_value=proof), \
                            mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                        rc, line = self.clear_cmd(world)
                    self.assertEqual(len(world.typed()), expect_typed, line)
                    if not expect_typed:
                        self.assertEqual((rc, "reason=target-changed" in line), (3, True), line)

    def test_a_codex_pane_record_that_trails_the_seat_ledger_is_not_a_changed_target(self):
        # Shared app-server daemon: no rollout for the process check, herdr keeps the session
        # before the last clear.  The seat ledger (both sessions' hooks at this pane) orders them.
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", "sid-A", "prompt", cwd="/w", now=time.time() - 100)
            st.record_event(seat, "codex", "sid-B", "prompt", cwd="/w", now=time.time() - 50)
        for herdr_says, typed in (("sid-A", 1), ("sid-never-seen", 0), ("sid-B", 1)):
            with self.subTest(herdr_says=herdr_says):
                self.book(harness="codex", sid="sid-B")
                world = _ClearWorld(harness="codex", sid=herdr_says, new_sid=None, screens=[CODEX_EMPTY])
                # the process check may name the older session too (same-cwd fallback of the board)
                with mock.patch("fleet.collectors.codex.session_id_of_process",
                                return_value="sid-A" if herdr_says == "sid-A" else None), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual(len(world.typed()), typed, line)
                if typed:
                    # the lagging value is never taken for the new session
                    self.assertIn("cleared=unverified", line)
                    self.assertNotIn("new_session=sid-A", line)
                else:
                    self.assertIn("reason=target-changed", line)

    # -- after the send: never a second one -------------------------------------------------

    def test_a_send_that_cannot_be_confirmed_is_unverified_and_nothing_else_is_typed(self):
        self.book()
        world = _ClearWorld(new_sid=None)                               # herdr keeps the old id, no hook note
        rc, line = self.clear_cmd(world)
        self.assertEqual(rc, 5, line)
        self.assertIn("cleared=unverified", line)
        self.assertEqual(len(world.typed()), 1)                         # one /clear, no Enter retry, no resend

    def test_the_start_hook_note_alone_proves_the_new_conversation(self):
        seat = self.book()
        world = _ClearWorld(new_sid=None)
        original = world.run

        def run(argv, **kw):
            result = original(argv, **kw)
            if argv[:3] == ["herdr", "agent", "prompt"]:               # the new session's start hook fires
                with self.st.seat_lock(seat.key):
                    self.clear.note_start_locked(seat, "claude", "sid-NEW", time.time() + 1)
            return result
        world.run = run
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "new_session=sid-NEW" in line), (0, True), line)

    def test_a_failing_herdr_prompt_is_failed_and_herdr_missing_is_failed(self):
        self.book()
        world = _ClearWorld()
        world.prompt_rc = 1
        rc, line = self.clear_cmd(world)
        self.assertEqual((rc, "cleared=failed" in line), (1, True), line)
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["clear", "w1:pX", "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "cleared=failed reason=herdr-not-found"))

    def test_a_new_thread_on_the_codex_status_line_proves_the_clear(self):
        self.book(harness="codex", sid=OLD_T)
        world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen("proj · main · Fix it")],
                            home_after=codex_screen(f"proj · main · Context 0% used · {NEW_T}"))
        with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
            rc, line = self.clear_cmd(world)
        self.assertEqual((rc, f"new_session={NEW_T}" in line), (0, True), line)
        self.assertEqual(len(world.typed()), 1)

    def test_a_status_line_id_that_was_there_before_is_cut_or_ambiguous_proves_nothing(self):
        other = "01a0fbf0-aaaa-7bbb-8ccc-dddddddddddd"
        st = self.st
        seat = st.Seat("pane", st._digest("pane", "w1:pX"), "w1:pX", "codex", "")
        with st.seat_lock(seat.key):
            st.record_event(seat, "codex", other, "prompt", cwd="/w", now=time.time() - 300)
        cases = ((f"proj · {NEW_T} · Fix it", f"proj · {NEW_T} · Fix it", "already shown"),
                 ("proj · Fix it", f"proj · {NEW_T} · 01a0fbf0-eeee-7fff-8000-111111111111", "two new ids"),
                 ("proj · Fix it", "proj · Context 0% used · 01a0fbf0-d65e-7041-9744-c8f5300f…", "cut id"),
                 (f"proj · {OLD_T}", f"proj · {OLD_T} · {NEW_T}", "the cleared id still shown"),
                 ("proj · Fix it", f"proj · {other}", "a session the seat already knows"))
        for before, after, label in cases:
            with self.subTest(label):
                self.book(harness="codex", sid=OLD_T)
                world = _ClearWorld(harness="codex", sid=OLD_T, new_sid=None, screens=[codex_screen(before)],
                                    home_after=codex_screen(after))
                with mock.patch("fleet.collectors.codex.session_id_of_process", return_value=None), \
                        mock.patch.object(peer_steward, "_CLEAR_OBSERVE_ROUNDS", 1):
                    rc, line = self.clear_cmd(world)
                self.assertEqual((rc, "cleared=unverified" in line), (5, True), line)

    def test_every_judged_call_leaves_one_notice_ledger_row_with_the_action(self):
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_DRAFT]))
        self.book()
        self.clear_cmd(_ClearWorld(screens=[CLAUDE_EMPTY]))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["kind"] == "notice" and "action=clear" in r["delivery"]["receipt"] for r in rows))
        self.assertIn("cleared=skipped", rows[0]["delivery"]["receipt"])
        self.assertIn("cleared=true", rows[1]["delivery"]["receipt"])
        self.assertEqual((rows[1]["to"]["pane"], rows[1]["from"]["session_id"]), ("w1:pX", "sid-A"))

    def test_the_screen_reader_decides_from_layout_not_from_wishful_text(self):
        lines = peer_steward._screen_lines
        draft = peer_steward._draft_state
        self.assertEqual(draft("claude", lines(CLAUDE_EMPTY)), "empty")
        self.assertEqual(draft("claude", lines(RULE + "\n❯ typed\n")), "nonempty")   # no closing rule: still a draft
        self.assertEqual(draft("claude", lines(RULE + "\n❯\n")), "unknown")           # box never closes: unknown
        self.assertEqual(draft("codex", lines("›\n")), "empty")
        self.assertEqual(draft("opencode", lines("  ┃  just text\n")), "unknown")      # a single bar line is no box
        self.assertEqual(draft("unheard-of", lines(CLAUDE_EMPTY)), "unknown")


class _ContinueWorld(_ClearWorld):
    """The window after a confirmed clear: herdr's session stays what it says, the prompt answers as told."""

    def __init__(self, harness="claude", herdr_sid="sid-B", screens=None, reply=(0, ""), **kw):
        super().__init__(harness=harness, sid=herdr_sid, new_sid=None, screens=screens or [CLAUDE_EMPTY], **kw)
        self.reply = reply
        self.on_prompt = self.on_wait = None

    def run(self, argv, **kw):
        if argv[:3] == ["herdr", "agent", "prompt"]:
            self.calls.append(list(argv))
            if self.on_prompt:
                self.on_prompt()
            rc, err = self.reply
            return subprocess.CompletedProcess(argv, rc, stdout="{}" if rc == 0 else "", stderr=err)
        if argv[:3] == ["herdr", "agent", "wait"] and self.on_wait:
            self.on_wait()
        return super().run(argv, **kw)


TIMEOUT_REPLY = (1, json.dumps({"error": {"code": "timeout"}}))


class ContinueTest(_TmpRootMixin, unittest.TestCase):
    """`continue`: the one prompt after a confirmed clear (fake herdr, real booking files)."""

    rows = ClearTest.rows

    def setUp(self):
        super().setUp()
        import session_tidy as st
        import session_tidy_clear as clear
        self.st, self.clear = st, clear

    def cleared(self, harness="claude", sid="sid-A", new="sid-B", pane="w1:pX", seq=0, receipt=True, window=120):
        st, clear = self.st, self.clear
        shutil.rmtree(st.state_root(), ignore_errors=True)       # every case starts from an empty seat
        seat = st.Seat("pane", st._digest("pane", pane), pane, harness, "")
        now = time.time()
        with st.seat_lock(seat.key):
            card = st.write_card(seat, harness, sid, "card body", prompt_seq=seq)
            if receipt and harness == "claude":
                st._write_consumed(seat, generation=card["generation"], receipts=[f"claude:{new}:0"])
            clear._write_reservation({
                "schema": 1, "nonce": "n0nce", "status": "cleared", "created": now - 30, "deadline": now + 570,
                "seat": {"kind": "pane", "key": seat.key, "pane": pane, "harness": harness, "project_key": ""},
                "harness": harness, "sid": sid, "cwd": str(self.tmp_root), "new_session": new,
                "card_generation": card["generation"], "prompt_seq": seq, "continue_off": False,
                "continued": {"state": "pending", "deadline": now + window}})
        self.seat_obj, self.path = seat, clear.reservation_path(seat.key)
        return seat

    def continue_cmd(self, world, nonce="n0nce", process=None):
        argv = ["continue", world.pane, "--request", str(self.path), *(["--nonce", nonce] if nonce else [])]
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch("fleet.collectors.claude.session_id_of_process", return_value=process), \
             mock.patch("fleet.collectors.codex.session_id_of_process", return_value=process), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(argv)
        return rc, (printed[-1] if printed else "")

    def booking(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def rollout(self, thread):
        path = self.tmp_root / "home" / ".codex" / "sessions" / "2026" / "10" / "02" / f"rollout-2026-10-02T18-27-39-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")

    # -- the one prompt -----------------------------------------------------------------------

    def test_a_cleared_claude_window_gets_the_continue_prompt_exactly_once(self):
        self.cleared()
        world = _ContinueWorld()
        rc, line = self.continue_cmd(world)
        self.assertEqual(rc, 0, line)
        self.assertIn("continued=true", line)
        self.assertIn("verify=state-flip", line)
        self.assertEqual(world.typed(), [["herdr", "agent", "prompt", "w1:pX", "이어서해",
                                          "--wait", "--until", "working", "--timeout", "8000"]])  # no trailer
        self.assertEqual(self.booking()["continued"]["state"], "sending")     # the helper writes the end
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("action=continue continued=true", rows[0]["delivery"]["receipt"])
        rc, line = self.continue_cmd(world)                                  # asked again: nothing more
        self.assertEqual((rc, "reason=superseded" in line, len(world.typed())), (3, True, 1), line)

    def test_draft_after_continue_claim_stays_pending_then_idle_sends_once(self):
        self.cleared()
        world = _ContinueWorld()
        claim = self.clear.claim_continue
        def user_types(*args, **kwargs):
            result = claim(*args, **kwargs)
            world.screens = [CLAUDE_DRAFT]
            return result
        with mock.patch.object(self.clear, "claim_continue", side_effect=user_types):
            rc, line = self.continue_cmd(world)
        self.assertEqual(rc, 3, line)
        self.assertIn("continued=queued", line)
        self.assertEqual(world.typed(), [])
        self.assertEqual(self.booking()["continued"]["state"], "pending")
        self.clear._finish_continue(self.seat_obj.key, "n0nce", "queued", "draft")
        world.screens = [CLAUDE_EMPTY]
        os.environ["HERDR_PANE_ID"] = world.pane
        def resume(path, nonce, target):
            code, receipt = self.continue_cmd(world)
            return dict(part.split("=", 1) for part in receipt.split() if "=" in part)
        with mock.patch.object(peer_steward, "_resolve_target", return_value=("claude", "sid-B", "w")), \
             mock.patch.object(peer_steward, "_agent_state", return_value=("idle", world.pane)), \
             mock.patch.object(self.clear, "_run_continue", side_effect=resume):
            peer_steward.receiver_idle({"harness": "claude", "session_id": "sid-B"}, world.pane)
            peer_steward.receiver_idle({"harness": "claude", "session_id": "sid-B"}, world.pane)
        self.assertEqual(len(world.typed()), 1)
        self.assertEqual(self.booking()["continued"]["state"], "sent")

    def test_late_queued_result_cannot_rewind_a_newer_continue_claim(self):
        self.cleared()
        claim, reason = self.clear.claim_continue(self.path, "n0nce")
        self.assertEqual(reason, "")
        self.assertFalse(self.clear._finish_continue(self.seat_obj.key, "n0nce", "queued", "draft"))
        self.assertEqual(self.booking()["continued"], claim["continued"])

    def test_the_claude_session_is_the_process_one_when_herdr_still_names_the_cleared_one(self):
        for process, expect in (("sid-B", "continued=true"), ("sid-A", "reason=target-changed"),
                                (None, "reason=target-changed")):
            with self.subTest(process=process):
                self.cleared()
                world = _ContinueWorld(herdr_sid="sid-A")
                rc, line = self.continue_cmd(world, process=process)
                self.assertIn(expect, line)
                self.assertEqual(len(world.typed()), 1 if expect == "continued=true" else 0)

    def test_claude_gets_a_bounded_while_for_its_start_hook_to_take_the_card(self):
        st = self.st
        seat = self.cleared(receipt=False)
        world = _ContinueWorld()
        waits = []

        def hook_lands():
            waits.append(1)
            if len(waits) == 2:
                with st.seat_lock(seat.key):
                    st._write_consumed(seat, generation=1, receipts=["claude:sid-B:0"])
        world.on_wait = hook_lands
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, len(world.typed())), (0, 1), line)
        self.cleared(receipt=False)
        world = _ContinueWorld()
        with mock.patch.object(peer_steward, "_CONTINUE_LOOK_ROUNDS", 3):
            rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "reason=card-not-delivered" in line, world.typed()), (3, True, []), line)

    def test_a_busy_blocked_unreadable_form_or_draft_window_gets_nothing(self):
        cases = (({"status": "working"}, "not-idle-working"), ({"status": "blocked"}, "form-open"),
                 ({"screens": [None]}, "screen-unknown"), ({"screens": [CLAUDE_FORM]}, "form-open"),
                 ({"screens": [CLAUDE_DRAFT]}, "draft"), ({"screens": [NO_BOX]}, "draft-unknown"),
                 ({"screens": [CLAUDE_EMPTY, CLAUDE_DRAFT]}, "draft"))     # typing starts between the looks
        for kwargs, reason in cases:
            with self.subTest(reason=reason, kwargs=str(kwargs)[:40]):
                self.cleared()
                world = _ContinueWorld(**kwargs)
                with mock.patch.object(peer_steward, "_CONTINUE_LOOK_ROUNDS", 2):
                    rc, line = self.continue_cmd(world)
                self.assertEqual((rc, f"reason={reason}" in line, world.typed()), (3, True, []), line)

    def test_a_new_prompt_new_card_handoff_lapse_or_spent_booking_gets_nothing_and_no_row(self):
        st, clear = self.st, self.clear

        def bump(seat):
            with st.seat_lock(seat.key):
                st.bump_prompt_seq(seat, "claude", "sid-B", time.time())

        def rewrite(seat):
            with st.seat_lock(seat.key):
                st.write_card(seat, "claude", "sid-B", "new card")

        def edit(**fields):
            def apply(_seat):
                data = self.booking()
                data.update(fields)
                self.path.write_text(json.dumps(data), encoding="utf-8")
            return apply
        cases = ((bump, "new-input"), (rewrite, "card-changed"), (st.mark_card_handed_off, "handed-off"),
                 (edit(continued={"state": "pending", "deadline": time.time() - 1}), "expired"),
                 (edit(continued={"state": "sending"}), "superseded"), (edit(status="reserved"), "superseded"),
                 (edit(continue_off=True), "off"))
        for change, reason in cases:
            with self.subTest(reason=reason):
                change(self.cleared())
                world = _ContinueWorld()
                rc, line = self.continue_cmd(world)
                self.assertEqual((rc, f"reason={reason}" in line, world.typed()), (3, True, []), line)
        self.cleared()
        rc, line = self.continue_cmd(_ContinueWorld(), nonce="another")
        self.assertEqual((rc, "reason=superseded" in line), (3, True))
        self.assertEqual(self.rows(), [])                               # a dead request leaves no ledger row

    def test_codex_needs_its_new_thread_on_the_status_line_and_no_rollout_yet(self):
        screen = codex_screen(f"proj · main · Context 0% used · {NEW_T}")
        self.cleared(harness="codex", sid=OLD_T, new=NEW_T)
        world = _ContinueWorld(harness="codex", herdr_sid=OLD_T, screens=[screen])
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, len(world.typed())), (0, 1), line)
        self.cleared(harness="codex", sid=OLD_T, new=NEW_T)
        world = _ContinueWorld(harness="codex", herdr_sid=OLD_T, screens=[codex_screen("proj · main · Fix it")])
        rc, line = self.continue_cmd(world)                              # a title instead: not provably that thread
        self.assertEqual((rc, "reason=target-changed" in line, world.typed()), (3, True, []), line)
        rc, line = self.continue_cmd(world, process=NEW_T)               # ... unless the process names it
        self.assertEqual((rc, len(world.typed())), (0, 1), line)
        self.cleared(harness="codex", sid=OLD_T, new=NEW_T)
        self.rollout(NEW_T)                                              # someone already wrote to it
        world = _ContinueWorld(harness="codex", herdr_sid=OLD_T, screens=[screen])
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "reason=new-input" in line, world.typed()), (3, True, []), line)

    def test_codex_and_opencode_skip_a_card_another_session_already_took(self):
        st = self.st
        seat = self.cleared(harness="codex", sid=OLD_T, new=NEW_T)
        with st.seat_lock(seat.key):
            st._write_consumed(seat, generation=1, receipts=["codex:someone:0"])
        world = _ContinueWorld(harness="codex", herdr_sid=OLD_T, screens=[codex_screen(NEW_T)])
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "reason=card-taken" in line, world.typed()), (3, True, []), line)

    def test_opencode_types_only_on_its_home_screen(self):
        for screen, typed in ((OPENCODE_HOME_BLANK, 1), (OPENCODE_HOME, 1), (OPENCODE_EMPTY, 0), (OPENCODE_DRAFT, 0)):
            with self.subTest(screen=screen[:24]):
                self.cleared(harness="opencode", sid="ses_A", new="-")
                world = _ContinueWorld(harness="opencode", herdr_sid=None, screens=[screen], status="done")
                rc, line = self.continue_cmd(world)
                self.assertEqual(len(world.typed()), typed, line)
                if not typed:
                    self.assertIn("reason=draft" if screen == OPENCODE_DRAFT else "reason=target-changed", line)

    # -- after the send: evidence, never a second one ------------------------------------------

    def test_a_send_herdr_cannot_confirm_needs_the_prompt_hook_or_the_transcript(self):
        st = self.st
        seat = self.cleared()
        world = _ContinueWorld(reply=TIMEOUT_REPLY)

        def hook():
            with st.seat_lock(seat.key):
                st.bump_prompt_seq(seat, "claude", "sid-B", time.time())
        world.on_prompt = hook
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "verify=prompt-hook" in line), (0, True), line)
        self.cleared()
        world = _ContinueWorld(reply=TIMEOUT_REPLY)
        transcript = self.tmp_root / "home" / ".claude" / "projects" / "p" / "sid-B.jsonl"

        def arrives():
            transcript.parent.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1))
            transcript.write_text(json.dumps({"type": "user", "timestamp": stamp,
                                              "message": {"content": "이어서해"}}) + "\n", encoding="utf-8")
        world.on_prompt = arrives
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "verify=transcript-arrival" in line), (0, True), line)
        transcript.unlink()
        self.cleared()
        world = _ContinueWorld(reply=TIMEOUT_REPLY)
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "continued=unverified" in line, len(world.typed())), (5, True, 1), line)  # no Enter
        self.cleared()
        world = _ContinueWorld(reply=(1, json.dumps({"error": {"code": "agent_prompt_stalled"}})))
        rc, line = self.continue_cmd(world)
        self.assertEqual((rc, "reason=agent-prompt-stalled" in line, len(world.typed())), (1, True, 1), line)

    def test_herdr_missing_is_failed(self):
        self.cleared()
        printed = []
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["continue", "w1:pX", "--request", str(self.path)])
        self.assertEqual((rc, printed[-1]), (1, "continued=failed reason=herdr-not-found"))



class BesideStartTest(_TmpRootMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.resolve_primary = peer_steward.INSTALL_PATHS.primary_checkout
        patch = mock.patch.object(peer_steward.INSTALL_PATHS, "primary_checkout",
                                  side_effect=lambda cwd: Path(cwd))
        self.primary_mock = patch.start()
        self.addCleanup(patch.stop)

    def test_default_cwd_resolves_a_real_linked_worktree_subfolder_to_primary(self):
        primary, linked = self.tmp_root / "primary", self.tmp_root / "linked"
        native_run = subprocess.run
        for argv in (["git", "init", "-q", str(primary)],
                     ["git", "-C", str(primary), "-c", "user.name=Fixture", "-c",
                      "user.email=fixture@example.invalid", "commit", "-q", "--allow-empty", "-m", "fixture"],
                     ["git", "-C", str(primary), "worktree", "add", "-q", "-b", "linked", str(linked)]):
            native_run(argv, check=True, capture_output=True, text=True)
        subfolder = linked / "nested"
        subfolder.mkdir()
        self.primary_mock.side_effect = self.resolve_primary
        calls = []

        def run(argv, **kw):
            if argv[0] == "git":
                return native_run(argv, **kw)
            calls.append(argv)
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertEqual(kw["cwd"], str(primary))
                return _herdr_json({"result": {"agent": {"agent": "codex", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.os, "getcwd", return_value=str(subfolder)), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="ready"), \
             mock.patch.object(peer_steward, "_codex_supports_no_daemon", return_value=True), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_pane_is_managed", return_value=False), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"]), 0)
        self.assertEqual(calls[0][-2:], ["--cwd", str(primary)])
        self.assertEqual(calls[1][-2:], ["--cd", str(primary)])
        self.assertIn("cwd=" + str(primary), printed.call_args[0][0])

    def test_opencode_late_bootstrap_waits_for_stable_shell_before_one_native_start(self):
        clock, events = {"now": 0.0}, []

        def export(pane):
            events.append("export")
            return "exported"

        def snapshot(pane, shell, deadline=None):
            self.assertEqual(deadline, 15)
            events.append("snapshot")
            return "settled" if clock["now"] >= .05 else None

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .05)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "opencode", "name": "new"}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", side_effect=export), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root)]), 0)
        self.assertEqual(events, ["export", "snapshot", "snapshot", "native start"])
        self.assertIn("started=true", printed.call_args[0][0])
        self.primary_mock.assert_not_called()

    def test_unstable_bootstrap_uses_original_deadline_and_offers_retained_pane_reuse(self):
        import shlex
        clock = {"now": 0.0}
        split = _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=split) as run, \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_export_opencode_tui_scoped", return_value="exported"), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value=None), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda n: clock.update(now=clock["now"] + n)), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "opencode", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root), "--permission-mode", "inherit",
                                               "--", "--model", "user/model"]), 1)
        self.assertEqual(clock["now"], .1)
        self.assertEqual(run.call_count, 1)
        close.assert_not_called()
        line = printed.call_args[0][0]
        self.assertIn("reason=beside-shell-readiness-timeout", line)
        self.assertIn("pane_cleanup=retained", line)
        reuse = next(v.split("=", 1)[1] for v in shlex.split(line) if v.startswith("reuse_command="))
        self.assertEqual(shlex.split(reuse), ["hearting", "run", "peer-steward", "start", "new",
                         "--kind", "opencode", "--pane", "w1:pN", "--cwd", str(self.tmp_root),
                         "--permission-mode", "inherit", "--", "--model", "user/model"])

    def test_same_tab_right_no_focus_split_reuses_start_with_new_pane_and_cwd(self):
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False,
                                                          "tab_id": "w1:tA"}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None) as ingress, \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit", "--", "--model", "opus"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["herdr", "pane", "split", "--pane", "w1:pOld", "--direction",
                                   "right", "--no-focus", "--cwd", cwd])
        self.assertEqual(calls[1], ["herdr", "agent", "start", "new", "--kind", "claude",
                                   "--pane", "w1:pN", "--", "--model", "opus"])
        ingress.assert_called_once_with("w1:pN", "claude", None)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])

    def test_beside_waits_for_delayed_split_cwd_before_single_start(self):
        calls, events = [], []
        cwd = str(self.tmp_root)
        clock = {"now": 0.0}

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                self.assertEqual(argv[-2:], ["--cwd", cwd])
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                busy = clock["now"] < .2
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 202 if busy else 101,
                    "foreground_processes": [{"pid": 202 if busy else 101}]}}})
            if argv[:3] == ["herdr", "pane", "wait-output"]:
                self.assertGreaterEqual(clock["now"], .2)
                events.append("ready prompt")
                return subprocess.CompletedProcess(argv, 0, stdout="prompt", stderr="")
            if argv[:3] == ["herdr", "agent", "start"]:
                self.assertGreaterEqual(clock["now"], .2)
                self.assertIn("snapshot", events)
                events.append("native start")
                return _herdr_json({"result": {"agent": {"agent": "claude", "name": "new",
                    "pane_id": "w1:pN", "agent_session": {"value": "new-native-sid"}}}})
            raise AssertionError(argv)

        def snapshot(pane, shell, deadline=None):
            self.assertGreaterEqual(clock["now"], .2)
            self.assertEqual((pane, shell), ("w1:pN", (101, "700")))
            events.append("snapshot")
            return "ready screen"

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_managed_ingress_dir", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_proc_cwd", side_effect=lambda pid:
                               cwd if clock["now"] >= .2 else "/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", side_effect=snapshot), \
             mock.patch.object(peer_steward, "_read_screen", return_value=None), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 0)
        self.assertIn("started=true", printed.call_args[0][0])
        self.assertEqual(sum(a[:3] == ["herdr", "agent", "start"] for a in calls), 1)
        self.assertFalse(any(a[:3] in (["herdr", "pane", "send-text"],
                                     ["herdr", "pane", "send-keys"]) for a in calls))
        self.assertEqual(events[-2:], ["snapshot", "native start"])
        close.assert_not_called()

    def test_beside_readiness_deadline_retains_pane_without_start_or_snapshot(self):
        clock = {"now": 0.0}
        calls = []
        cwd = str(self.tmp_root)

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "pane", "get"]:
                return _herdr_json({"result": {"pane": {"agent": None}}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN",
                    "shell_pid": 101, "foreground_process_group_id": 101,
                    "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_BESIDE_READY_SECONDS", .1), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_proc_cwd", return_value="/previous"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: clock["now"]), \
             mock.patch.object(peer_steward.time, "sleep", side_effect=lambda seconds:
                               clock.update(now=clock["now"] + seconds)), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
             mock.patch.object(peer_steward, "_start_shell_snapshot") as snapshot, \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            rc = peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld",
                                   "--cwd", cwd, "--permission-mode", "inherit"])
        self.assertEqual(rc, 1)
        self.assertIn("started=false reason=beside-shell-readiness-timeout", printed.call_args[0][0])
        self.assertIn("pane=w1:pN", printed.call_args[0][0])
        self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
        self.assertEqual(clock["now"], .1)
        self.assertFalse(any(a[:3] == ["herdr", "agent", "start"] for a in calls))
        ingress.assert_not_called(); snapshot.assert_not_called(); close.assert_not_called()

    def test_split_failure_malformed_focused_or_same_pane_never_starts_or_types(self):
        cases = [subprocess.CompletedProcess([], 1, stdout="", stderr="denied"),
                 subprocess.CompletedProcess([], 0, stdout="null", stderr=""),
                 _herdr_json({"error": {"code": "pane_not_found"}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pOld", "focused": False}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": True}}}),
                 _herdr_json({"result": {"pane": {"pane_id": "w1:pN"}}})]
        for response in cases:
            with self.subTest(response=response), \
                 mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
                 mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
                 mock.patch.object(peer_steward, "_ensure_pane_ingress") as ingress, \
                 mock.patch("builtins.print") as printed:
                rc = peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld"])
                self.assertEqual(rc, 1)
                self.assertEqual(run.call_count, 1)
                ingress.assert_not_called()
                self.assertIn("started=false reason=pane-split-failed", printed.call_args[0][0])

    def test_start_with_no_pane_named_opens_beside_the_calling_pane_in_the_calling_cwd(self):
        response = _herdr_json({"error": {"code": "pane_not_found"}})   # stop right after the split
        with tempfile.TemporaryDirectory() as cwd, \
             mock.patch.dict(peer_steward.os.environ, {"HERDR_PANE_ID": "w1:pMe"}), \
             mock.patch.object(peer_steward.os, "getcwd", return_value=cwd), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", return_value=response) as run, \
             mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex"]), 1)
            split = run.call_args_list[0][0][0]
            self.assertEqual(split[:5], ["herdr", "pane", "split", "--pane", "w1:pMe"])
            self.assertEqual(split[split.index("--cwd") + 1], os.path.realpath(cwd))
        self.assertEqual(self.resolve_primary(cwd), Path(cwd))

    def test_start_outside_a_herdr_pane_with_no_pane_named_says_so_and_runs_nothing(self):
        env = {k: v for k, v in os.environ.items() if k != "HERDR_PANE_ID"}
        with mock.patch.dict(peer_steward.os.environ, env, clear=True), \
             mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "claude"]), 1)
        run.assert_not_called()
        self.assertIn("started=false reason=pane-unknown", printed.call_args[0][0])

    def test_non_json_start_failure_has_bounded_reason_and_closes_only_owned_empty_split(self):
        calls = []

        def run(argv, **kw):
            calls.append(list(argv))
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="\x1b[31mError: name too long\x1b[0m\n" + "secret second line")
            if argv[:3] == ["herdr", "pane", "close"]:
                return _herdr_json({"result": {"type": "ok"}})
            if argv[:3] == ["herdr", "pane", "process-info"]:
                return _herdr_json({"result": {"process_info": {"pane_id": "w1:pN", "shell_pid": 101,
                    "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}}})
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_start_shell_snapshot", return_value="recorded screen"), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "too-long", "--kind", "claude", "--beside", "w1:pOld"])
        self.assertIn("started=false", printed.call_args[0][0])
        self.assertIn("reason=herdr-stderr-error-name-too-long", printed.call_args[0][0])
        self.assertIn("pane_cleanup=closed", printed.call_args[0][0])
        self.assertNotIn("secret", printed.call_args[0][0])
        self.assertEqual(calls[-1], ["herdr", "pane", "close", "w1:pN"])
        self.assertFalse(any(a[:3] == ["herdr", "pane", "close"] and a[-1] == "w1:pOld" for a in calls))

    def test_failed_cleanup_refuses_agent_changed_shell_unknown_or_draft(self):
        for occupied, shell, prompt in (("pane-occupied", (101, "700"), True),
                                        ("pane-unknown", (101, "700"), True),
                                        (None, (101, "701"), True),
                                        (None, None, True), (None, (101, "700"), False)):
            clock = iter(range(20))
            with self.subTest(occupied=occupied, shell=shell, prompt=prompt), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=occupied), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=shell), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value="701" if shell == (101, "701") else "700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value=None if shell is None else {"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen" if prompt else "changed screen"), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"), \
                 mock.patch.object(peer_steward, "_close_pane") as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "retained")
                close.assert_not_called()

    def test_existing_pane_failure_never_cleans_up_and_stderr_is_bounded(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell") as readiness, \
             mock.patch.object(peer_steward.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 1, stdout="", stderr="Error: duplicate name\n")) as run, \
             mock.patch.object(peer_steward, "_failed_start_cleanup") as cleanup, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "old", "--kind", "claude", "--pane", "w1:pOld"])
            self.assertIn("reason=herdr-stderr-error-duplicate-name", printed.call_args[0][0])
            cleanup.assert_not_called(); self.assertEqual(run.call_count, 1)
            readiness.assert_not_called()
        code = peer_steward._start_stderr_code("Error: " + "long\tvalue\x00" * 100 + "\n")
        self.assertLessEqual(len(code.encode()), 80)
        self.assertRegex(code, r"^[a-z0-9_-]+$")

    def test_failed_fresh_start_waits_for_same_shell_then_closes_once(self):
        clock = iter(range(20))
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_shell_identity", side_effect=[None, (101, "700"), (101, "700")]), \
             mock.patch.object(peer_steward, "_start_pane_screen", return_value="recorded screen"), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep") as pause, \
             mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), "recorded screen"), "closed")
            pause.assert_called_once()
            close.assert_called_once_with("w1:pN")

    def test_fresh_snapshot_requires_two_stable_nonblank_reads_and_same_shell(self):
        cases = [(["recorded screen", "recorded screen"], "recorded screen"),
                 (["", "recorded screen", "recorded screen"], "recorded screen"),
                 ([""] * 100, None), ([str(i) for i in range(100)], None), ([None], None)]
        for screens, expected in cases:
            clock = iter(i / 100 for i in range(1000))
            with self.subTest(screens=screens[:3]), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", side_effect=screens), \
                 mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
                 mock.patch.object(peer_steward.time, "sleep"):
                self.assertEqual(peer_steward._start_shell_snapshot("w1:pN", (101, "700")), expected)
        with mock.patch.object(peer_steward, "_pane_has_agent", return_value="pane-unknown"), \
             mock.patch.object(peer_steward, "_start_pane_screen") as read:
            self.assertIsNone(peer_steward._start_shell_snapshot("w1:pN", (101, "700")))
            read.assert_not_called()

    def test_visible_screen_is_verbatim_and_unavailable_read_is_unknown(self):
        for stdout, rc, expected in (("user@host $ ", 0, "user@host $ "),
                ("\x1b[0mhost  draft", 0, "\x1b[0mhost  draft"), ("", 0, ""),
                ("denied", 1, None), ("x" * 65537, 0, None), ('{"error":{"code":"denied"}}', 0, None)):
            with self.subTest(rc=rc, length=len(stdout)), mock.patch.object(peer_steward.subprocess,
                    "run", return_value=subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")) as run:
                self.assertEqual(peer_steward._start_pane_screen("w1:pN"), expected)
                self.assertEqual(run.call_args[0][0], ["herdr", "pane", "read", "w1:pN",
                    "--source", "visible", "--format", "ansi"])

    def test_post_record_drafts_and_redraw_never_close_owned_split(self):
        plain = "user@host $ "
        powerline = "user@host  /work  main  "
        styled = "\x1b[0m\x1b[38;5;2m\x1b[0m "
        cases = [(plain, "user@host $ echo $ "), ("user@host % ", "user@host % printf % "),
                 ("user@host ❯ ", "user@host ❯ : # "), (plain, "user@host $echo $ "),
                 ("%", "%printf % "), ("❯", "❯: # "),
                 (powerline, powerline + "echo " + styled),
                 (powerline, powerline + "printf " + styled),
                 (plain, "redrawn prompt"), (plain, None)]
        for recorded, current in cases:
            with self.subTest(current=current), \
                 mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
                 mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
                 mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
                 mock.patch.object(peer_steward, "_start_pane_screen", return_value=current), \
                 mock.patch.object(peer_steward, "_close_pane", return_value=True) as close:
                self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), recorded), "retained")
                close.assert_not_called()
        with mock.patch.object(peer_steward, "_close_pane") as close:
            self.assertEqual(peer_steward._failed_start_cleanup("w1:pN", (101, "700"), None), "retained")
            close.assert_not_called()

    def test_beside_records_stable_screen_before_start_and_retains_later_change(self):
        events = []
        screens = iter(["before", "before", "after draft"])
        clock = iter(i / 100 for i in range(1000))

        def read(pane):
            value = next(screens); events.append("screen:" + value); return value

        def run(argv, **kw):
            if argv[:3] == ["herdr", "pane", "split"]:
                return _herdr_json({"result": {"pane": {"pane_id": "w1:pN", "focused": False}}})
            if argv[:3] == ["herdr", "agent", "start"]:
                events.append("native start")
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="start rejected")
            raise AssertionError(argv)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=run), \
             mock.patch.object(peer_steward, "_ensure_pane_ingress", return_value=None), \
             mock.patch.object(peer_steward, "_wait_for_created_shell", return_value=(None, (101, "700"))), \
             mock.patch.object(peer_steward, "_pane_has_agent", return_value=None), \
             mock.patch.object(peer_steward, "_start_shell_identity", return_value=(101, "700")), \
             mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward, "_retire_pane_info", return_value={"shell_pid": 101}), \
             mock.patch.object(peer_steward, "_start_pane_screen", side_effect=read), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch.object(peer_steward, "_close_pane") as close, mock.patch("builtins.print") as printed:
            peer_steward.main(["start", "new", "--kind", "claude", "--beside", "w1:pOld"])
            self.assertIn("pane_cleanup=retained", printed.call_args[0][0])
            close.assert_not_called()
        self.assertEqual(events, ["screen:before", "screen:before", "native start", "screen:after draft"])

    def test_invalid_cwd_does_not_split_and_pane_choices_are_exclusive(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print"):
            self.assertEqual(peer_steward.main(["start", "new", "--kind", "codex", "--beside", "w1:pOld",
                                               "--cwd", str(self.tmp_root / "missing")]), 1)
        run.assert_not_called()
        with mock.patch.object(peer_steward.sys, "stderr"), self.assertRaises(SystemExit):
            peer_steward.build_parser().parse_args(["start", "new", "--kind", "codex", "--pane", "w1:pA",
                                                   "--beside", "w1:pB"])


class _RetireWorld:
    def __init__(self, harness="codex", status="idle", screen=None, exits=True):
        self.harness, self.status = harness, status
        self.screen = screen if screen is not None else {
            "codex": CODEX_EMPTY, "claude": CLAUDE_EMPTY, "opencode": OPENCODE_EMPTY}[harness]
        self.calls, self.gets, self.process_reads = [], 0, 0
        self.sent, self.exits, self.changed = False, exits, False
        self.bad_process = None
        self.post_unknown, self.close_error, self.send_error = False, False, False
        self.final_changed = False

    def agent(self):
        self.gets += 1
        return {"agent": self.harness, "agent_status": self.status, "name": "old",
                "pane_id": "w1:pOld", "agent_session": {"value": "other" if self.changed and self.gets > 1 else "old-sid"}}

    def info(self):
        self.process_reads += 1
        shell = self.sent and self.exits
        result = {"pane_id": "w1:pOld", "shell_pid": 101,
                  "foreground_process_group_id": 101 if shell else 4242,
                  "foreground_processes": [{"pid": 101 if shell else 4242,
                    "argv": ["/usr/bin/zsh"] if shell else [self.harness],
                    "name": "zsh" if shell else self.harness}]}
        if self.bad_process:
            result.update(self.bad_process)
        if self.final_changed and self.process_reads >= 4:
            result["shell_pid"] = 999
        return result

    def run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["herdr", "agent", "get"]:
            return _herdr_json({"result": {"agent": self.agent()}})
        if argv[:3] == ["herdr", "agent", "read"]:
            return subprocess.CompletedProcess(argv, 0 if self.screen else 1, stdout=self.screen or "", stderr="")
        if argv[:3] == ["herdr", "pane", "process-info"]:
            return (_herdr_json({"error": {"code": "unavailable"}}) if self.sent and self.post_unknown
                    else _herdr_json({"result": {"process_info": self.info()}}))
        if argv[:3] == ["herdr", "pane", "send-text"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["herdr", "pane", "send-keys"]:
            self.sent = True
            return (subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"error": {"code": "denied"}}), stderr="")
                    if self.send_error else subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))
        if argv[:3] == ["herdr", "pane", "close"]:
            return _herdr_json({"error": {"code": "denied"}} if self.close_error else {"result": {"type": "ok"}})
        raise AssertionError(argv)

    def actions(self):
        return [a for a in self.calls if a[:3] in (["herdr", "pane", "send-text"], ["herdr", "pane", "send-keys"], ["herdr", "pane", "close"])]


class RetireTest(_TmpRootMixin, unittest.TestCase):
    def retire(self, world, record=None):
        printed = []
        original_stat = os.stat
        clock = iter(range(100))

        def stat(path, *a, **kw):
            if str(path) == "/proc/4242" and world.sent and world.exits:
                raise FileNotFoundError(path)
            if str(path) == "/proc/4242":
                return object()
            return original_stat(path, *a, **kw)

        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward.subprocess, "run", side_effect=world.run), \
             mock.patch.object(peer_steward, "_retire_process_record", return_value=(record if record is not None else
                              {"start": "800", "group": 4242, "argv": [world.harness]})), \
             mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
             mock.patch.object(peer_steward.os, "stat", side_effect=stat), \
             mock.patch.object(peer_steward.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(peer_steward.time, "sleep"), \
             mock.patch("builtins.print", side_effect=lambda *a, **kw: printed.append(" ".join(map(str, a)))):
            rc = peer_steward.main(["retire", "old"])
        return rc, printed[-1]

    def test_confirmed_normal_exit_closes_once_and_records_notice(self):
        for harness, status in (("codex", "idle"), ("opencode", "done"), ("claude", "idle")):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, status=status)
                rc, line = self.retire(world)
                self.assertEqual((rc, line), (0, f"retired=true reason=normal-exit agent={harness} name=old pane=w1:pOld"))
                expected = ([["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                             ["herdr", "pane", "send-keys", "w1:pOld", "enter"]] if harness == "claude"
                            else [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])
                self.assertEqual(world.actions(), expected + [["herdr", "pane", "close", "w1:pOld"]])
                row = self._all_records()[-1]
                self.assertEqual((row["kind"], row["to"]["pane"], row["delivery"]["receipt"]),
                                 ("notice", "w1:pOld", "normal-exit"))

    def test_a_retire_from_the_pane_started_beside_hands_the_routes_on(self):
        # RA-2 (decision 5f0f10): beside -> ACK -> retire moves the parent role, across harnesses.
        handed_line = "retired=true reason=normal-exit agent=codex name=old pane=w1:pOld"
        def run(mark_beside, mark_sid):
            peer_steward._mark_seat_successor("w1:pNew", mark_beside, "claude", mark_sid)
            with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pNew"}), \
                 mock.patch.object(peer_steward, "_current_session_identity", return_value=("new-sid", "claude")), \
                 mock.patch("dispatch_seat_handover.record_retire_handover",
                            return_value={"bindings": [{}, {}]}) as handed:
                rc, line = self.retire(_RetireWorld(harness="codex"))
            return rc, line, handed
        rc, line, handed = run("w1:pOld", "new-sid")
        self.assertEqual((rc, line), (0, handed_line + " handover=2"))
        handed.assert_called_once_with("old-sid", "codex", "new-sid", "claude", env=mock.ANY)
        self.assertFalse(peer_steward._seat_successor_path("w1:pNew").exists())      # used once
        rc, line, handed = run("w1:pElse", "new-sid")                               # not beside this one
        self.assertEqual((rc, line), (0, handed_line))
        handed.assert_not_called()
        rc, line, handed = run("w1:pOld", "someone-else")                           # another session's pane
        self.assertEqual((rc, line), (0, handed_line + " handover=skipped:successor-unverified"))
        handed.assert_not_called()

    def test_prompt_git_helpers_after_exit_are_not_live_agents(self):
        for harness in ("codex", "claude", "opencode"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness)
                normal_info = world.info

                def prompt_info():
                    value = normal_info()
                    if world.sent:
                        value["foreground_processes"] += [
                            {"pid": 102, "argv": ["zsh"]},
                            {"pid": 103, "argv": ["git", "status", "--porcelain",
                                                  "--ignore-submodules=dirty"]},
                        ]
                    return value

                world.info = prompt_info
                rc, line = self.retire(world)
                self.assertEqual(rc, 0)
                self.assertIn("retired=true reason=normal-exit", line)
                self.assertEqual(world.actions()[-1], ["herdr", "pane", "close", "w1:pOld"])

    def test_only_opencode_waits_for_a_slow_normal_exit(self):
        for harness in ("opencode", "codex", "claude"):
            with self.subTest(harness=harness):
                world = _RetireWorld(harness=harness, exits=False)
                normal_info = world.info

                def delayed_info():
                    if world.sent and peer_steward.time.monotonic() >= 8:
                        world.exits = True
                    return normal_info()

                world.info = delayed_info
                rc, line = self.retire(world)
                if harness == "opencode":
                    self.assertEqual(rc, 0)
                    self.assertIn("retired=true reason=normal-exit", line)
                    self.assertEqual(world.actions(), [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                        ["herdr", "pane", "close", "w1:pOld"],
                    ])
                else:
                    self.assertEqual(rc, 1)
                    self.assertIn("retired=false reason=agent-still-running", line)
                    expected = ([
                        ["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                        ["herdr", "pane", "send-keys", "w1:pOld", "enter"],
                    ] if harness == "claude" else [
                        ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
                    ])
                    self.assertEqual(world.actions(), expected)

    def test_opencode_still_running_is_bounded_without_another_exit_key(self):
        world = _RetireWorld(harness="opencode", exits=False)
        rc, line = self.retire(world)
        self.assertEqual(rc, 1)
        self.assertIn("retired=false reason=agent-still-running", line)
        self.assertEqual(world.actions(), [
            ["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"],
        ])

    def test_busy_form_draft_unknown_self_or_changed_target_receives_no_exit(self):
        cases = [(_RetireWorld(status=s), "agent-" + s) for s in ("working", "blocked", "unknown")]
        cases += [(_RetireWorld(screen=s), reason) for s, reason in
                  ((CODEX_DRAFT, "draft"), (CODEX_POPUP, "draft-unknown"), (NO_BOX, "draft-unknown"),
                   (CLAUDE_FORM, "form-open"), ("", "screen-unavailable"))]
        cases += [(_RetireWorld("claude", screen=CLAUDE_DRAFT_SECOND_LINE), "draft"),
                  (_RetireWorld("opencode", screen=OPENCODE_DRAFT), "draft")]
        changed = _RetireWorld(); changed.changed = True
        cases.append((changed, "target-changed"))
        for world, reason in cases:
            with self.subTest(reason=reason):
                rc, line = self.retire(world)
                self.assertEqual(rc, 1)
                self.assertIn("reason=" + reason, line)
                self.assertEqual(world.actions(), [])
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:pOld"}):
            world = _RetireWorld(); rc, line = self.retire(world)
            self.assertIn("reason=self-target", line); self.assertEqual(world.actions(), [])

    def test_unverified_or_foreign_foreground_receives_no_exit(self):
        for patch in ({"pane_id": "w1:pForeign"}, {"shell_pid": True},
                      {"foreground_process_group_id": 999}, {"foreground_processes": []},
                      {"foreground_processes": [{"pid": 4242, "argv": ["other"]}]}):
            with self.subTest(patch=patch):
                world = _RetireWorld(); world.bad_process = patch
                rc, line = self.retire(world)
                self.assertIn("reason=foreground-unverified", line); self.assertEqual(world.actions(), [])
        world = _RetireWorld()
        rc, line = self.retire(world, {"start": "800", "group": 999, "argv": ["codex"]})
        self.assertIn("reason=foreground-unverified", line); self.assertEqual(world.actions(), [])

    def test_claude_exit_command_unknown_or_still_live_never_closes_or_retries(self):
        # One /exit submission; a missing shell return never permits a retry or close.
        for unknown in (False, True):
            world = _RetireWorld("claude", exits=False); world.post_unknown = unknown
            rc, line = self.retire(world)
            self.assertEqual(rc, 1)
            self.assertIn("reason=" + ("shell-return-unverified" if unknown else "agent-still-running"), line)
            self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"],
                                               ["herdr", "pane", "send-keys", "w1:pOld", "enter"]])

    def test_error_body_on_send_or_close_is_not_success(self):
        world = _RetireWorld(); world.send_error = True
        rc, line = self.retire(world)
        self.assertIn("reason=exit-send-failed", line)
        self.assertEqual(len(world.actions()), 1)
        world = _RetireWorld(); world.close_error = True
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=pane-close-failed", line)
        self.assertEqual(len(world.actions()), 2)

    def test_claude_failed_exit_text_or_changed_foreground_never_submits_enter(self):
        world = _RetireWorld("claude")
        normal_run = world.run

        def failed_text(argv, **kw):
            if argv[:3] == ["herdr", "pane", "send-text"]:
                world.calls.append(list(argv))
                return _herdr_json({"error": {"code": "denied"}})
            return normal_run(argv, **kw)

        world.run = failed_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 1); self.assertIn("reason=exit-send-failed", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])
        world = _RetireWorld("claude")
        normal_info = world.info

        def changed_after_text():
            value = normal_info()
            if any(a[:3] == ["herdr", "pane", "send-text"] for a in world.calls):
                value["foreground_process_group_id"] = 999
            return value

        world.info = changed_after_text
        rc, line = self.retire(world)
        self.assertEqual(rc, 1); self.assertIn("reason=foreground-changed", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-text", "w1:pOld", "/exit"]])

    def test_changed_shell_after_exit_is_not_closed(self):
        world = _RetireWorld(); world.final_changed = True
        rc, line = self.retire(world)
        self.assertIn("retired=false reason=shell-changed", line)
        self.assertEqual(world.actions(), [["herdr", "pane", "send-keys", "w1:pOld", "ctrl+d"]])

    def test_protocol_error_or_missing_herdr_is_typed_and_never_exits(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="herdr"), \
             mock.patch.object(peer_steward, "_run_herdr_get", return_value={"result": {"agent": {
                 "agent": "codex", "agent_status": "idle", "agent_session": "bad"}}}), \
             mock.patch.object(peer_steward.subprocess, "run") as run, mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-protocol-error", printed.call_args[0][0])
            run.assert_not_called()
        with mock.patch.object(peer_steward.shutil, "which", return_value=None), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(peer_steward.main(["retire", "old"]), 1)
            self.assertIn("retired=false reason=herdr-not-found", printed.call_args[0][0])

    def test_shell_return_requires_original_birth_and_absent_predecessor(self):
        identity = {"pid": 4242, "start": "800", "shell_pid": 101, "shell_start": "700"}
        info = {"shell_pid": 101, "foreground_process_group_id": 101, "foreground_processes": [{"pid": 101}]}
        with mock.patch.object(peer_steward, "_proc_start_ticks", return_value="701"):
            self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        for error in (None, PermissionError()):
            with mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else "800"), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object(), side_effect=error):
                self.assertFalse(peer_steward._retire_shell_returned(info, identity))
        with mock.patch.object(peer_steward, "_proc_start_ticks", return_value="700"), \
             mock.patch.object(peer_steward.os, "stat", side_effect=FileNotFoundError()):
            self.assertTrue(peer_steward._retire_shell_returned(info, identity))

        for birth, expected in (("800", False), ("801", True), (None, False)):
            with self.subTest(birth=birth), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", side_effect=lambda pid: "700" if pid == 101 else birth), \
                 mock.patch.object(peer_steward.os, "stat", return_value=object()):
                self.assertEqual(peer_steward._retire_shell_returned(info, identity), expected)

    def test_kernel_record_refuses_unreadable_namespace_or_reused_birth(self):
        stat = "4242 (codex) " + " ".join(["S", "101", "4242"] + ["0"] * 16 + ["800"])
        for birth, namespaces, read_error in (("800", ["pid:[1]", "pid:[1]"], None),
                ("801", [], None), ("800", ["pid:[2]", "pid:[1]"], None),
                ("800", [], PermissionError())):
            with self.subTest(birth=birth, namespaces=namespaces, read_error=read_error), \
                 mock.patch.object(peer_steward.Path, "read_text", return_value=stat, side_effect=read_error), \
                 mock.patch.object(peer_steward.Path, "read_bytes", return_value=b"codex\0"), \
                 mock.patch.object(peer_steward, "_proc_start_ticks", return_value=birth), \
                 mock.patch.object(peer_steward.os, "readlink", side_effect=namespaces):
                result = peer_steward._retire_process_record(4242)
                if birth == "800" and namespaces == ["pid:[1]", "pid:[1]"]:
                    self.assertEqual(result, {"start": "800", "group": 4242, "argv": ["codex"]})
                else:
                    self.assertIsNone(result)


class RetireBackgroundDialogTest(unittest.TestCase):
    def _cells(self, text):
        return [(ch, False) for ch in text]

    def test_detects_live_selection_ui(self):
        lines = [self._cells("Background work is running"),
                 self._cells("python3 train.py"),
                 self._cells("1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to confirm · Esc to cancel"),
                 self._cells("❯ ")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_detects_cursor_prefixed_selection(self):
        lines = [self._cells("Background work is running"),
                 self._cells("❯ 1. Exit and stop tasks"),
                 self._cells("2. Move to background and exit"),
                 self._cells("3. Stay"),
                 self._cells("Enter to select · Esc to cancel")]
        tasks = peer_steward._retire_background_dialog_lines(lines)
        self.assertIsNotNone(tasks)

    def test_ignores_other_forms(self):
        lines = [self._cells("Do you want to proceed?"),
                 self._cells("Enter to confirm")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))
        self.assertIsNone(peer_steward._retire_background_dialog_lines([]))
        self.assertIsNone(peer_steward._retire_background_dialog_lines(None))

    def test_ignores_quoted_transcript_without_live_ui(self):
        lines = [self._cells("● Reading the PR #288 diff"),
                 self._cells("+    The dialog reads \"Background work is running … 1. Exit and stop tasks /"),
                 self._cells("+    2. Move to background and exit / 3. Stay\". …"),
                 self._cells("❯ ")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))

    def test_ignores_busy_answer_quoting_dialog(self):
        lines = [self._cells("✻ Working… (esc to interrupt)"),
                 self._cells("  The dialog reads \"Background work is running"),
                 self._cells("  1. Exit and stop tasks"),
                 self._cells("  2. Move to background and exit / 3. Stay\"."),
                 self._cells("  Enter to confirm · Esc to cancel is only in the real window."),
                 self._cells("❯ let me check the logs")]
        self.assertIsNone(peer_steward._retire_background_dialog_lines(lines))


if __name__ == "__main__":
    unittest.main()
