#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import fcntl
import hashlib
import importlib.util
import json
import os
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
                                return_value=_herdr_json({"result": {"pane": {}}})) as run_mock:
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
                                return_value=_herdr_json({"result": {"pane": {}}})) as run_mock:
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
                                return_value=_herdr_json({"result": {"pane": {}}})), \
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
                                return_value=_herdr_json({"result": {"pane": {}}})), \
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

    def test_cwd_is_refused_where_the_harness_cannot_honor_it(self):
        # Claude Code has no working-root flag. Refusing is the point: the alternative
        # is a session quietly working in the wrong repository.
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run_mock:
            rc = peer_steward.main(["start", "peer-c", "--kind", "claude",
                                    "--pane", "w1:pM", "--cwd", str(self.tmp_root)])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

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
                                return_value=_herdr_json(_agent_json("codex", None, "peer-c"))):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                peer_steward.main(["start", "peer-c", "--kind", "codex", "--pane", "w1:pM"])
        self.assertIn("session_id=-", out.getvalue())

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

        for name in ("peer-steward.py", "../hooks/peer-steward-rewake.py"):
            path = (_HERE / name).resolve()
            self.assertTrue(path.exists(), f"{name} missing")
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    dotted = (
                        isinstance(func, ast.Attribute) and func.attr == "sleep"
                    ) or (isinstance(func, ast.Name) and func.id == "sleep")
                    self.assertFalse(dotted, f"{path.name} must not sleep")
                if isinstance(node, ast.While):
                    test = node.test
                    unbounded = isinstance(test, ast.Constant) and test.value is True
                    self.assertFalse(unbounded, f"{path.name} must not poll-loop")


class WatcherReceiptTest(_WatchMixin, unittest.TestCase):
    """A56-2."""

    def test_receipt_is_atomic_complete_and_text_free(self):
        proc = self._run("watch", "peer-a", env=self._env("held"))
        watch_id = self._fields(proc.stdout)["watch_id"]
        self._release()
        receipt = self._wait_for_receipt(watch_id)

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

        lock = self.watch_root / f"{watch_id}.lock"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # released by exit
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
        *read* box still showing our first line after one Enter: `queued`."""
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
                         [["herdr", "agent", "send-keys", "child", "Enter"]])
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
        form is lost and the Enter answers it with the default. `blocked` from
        herdr, or the form footer in a narrow pane, is a typed refusal."""
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
                self.assertEqual(rc, 1)
                self.assertFalse([c for c in calls if c[:3] == ["herdr", "agent", "prompt"]],
                                 "nothing may be typed into an open form")
                self.assertIn("prompted=failed", print_mock.call_args[0][0])
                self.assertIn("reason=target-form-open", print_mock.call_args[0][0])
                rec = self._all_records()[-1]
                self.assertEqual(rec["delivery"]["status"], "failed")
                self.assertIn("reason=target-form-open", rec["delivery"]["receipt"])
                self.assertEqual(rec["to"]["pane"], "w1:pX")

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
        `herdr agent prompt|send-keys` / `herdr pane send-text|send-keys|run`
        caller is a defect. Census 2026-09-06: 0 outside this module."""
        import re
        root = (_HERE / "..").resolve()
        pattern = re.compile(r"herdr[\"', \[]+(agent|pane)[\"', ]+(prompt|send-text|send-keys|run)\b")
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
    claude > codex > opencode > AGENT_SESSION_ID fallback for the ambiguous/unset case."""

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

    def test_ambiguous_multiple_sessions_falls_back_to_legacy_priority(self):
        # No explicit caller harness + two sessions set -> interactive_parent_identity()
        # raises caller-harness-ambiguous; the prior claude-first order still applies.
        with self._clean_env(CLAUDE_CODE_SESSION_ID="sid-c", CODEX_THREAD_ID="sid-x"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-c", "claude"))

    def test_single_session_delegates_cleanly(self):
        with self._clean_env(CODEX_THREAD_ID="sid-solo"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-solo", "codex"))

    def test_agent_session_id_still_falls_back_to_unknown(self):
        with self._clean_env(AGENT_SESSION_ID="sid-legacy"):
            self.assertEqual(peer_steward._current_session_identity(), ("sid-legacy", "unknown"))

    def test_nothing_set_returns_empty_unknown(self):
        with self._clean_env():
            self.assertEqual(peer_steward._current_session_identity(), ("", "unknown"))


if __name__ == "__main__":
    unittest.main()
