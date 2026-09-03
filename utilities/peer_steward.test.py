#!/usr/bin/env python3
"""Unit tests for utilities/peer-steward.py (SD-122 (9) steward wait/start)."""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("peer_steward", str(_HERE / "peer-steward.py"))
peer_steward = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(peer_steward)


class _TmpRootMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self._old_environ = dict(os.environ)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        os.environ.pop("AGENT_HOME", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        os.environ.pop("AGENT_SESSION_ID", None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._old_environ)

    def _all_records(self):
        root = peer_steward.peer_message._ledger_root() / "peer-messages"
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


class StartTest(_TmpRootMixin, unittest.TestCase):
    def _start(self, name="peer-c", kind="claude", pane="w1:pM", permission_mode=None, agent_args=None):
        argv = ["start", name, "--kind", kind, "--pane", pane]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if agent_args:
            argv += ["--"] + list(agent_args)
        return peer_steward.main(argv)

    def test_name_is_the_positional_right_after_start(self):
        """herdr `agent start <NAME> --kind --pane`: the name is a required positional.
        Without it herdr answers `unknown option: claude` and starts nothing, while the
        wrapper still printed started=false and recorded a `[start]` steer (measured
        2026-09-03 during the F-100 comms test)."""
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(name="peer-c", kind="claude", pane="w1:pM")
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd[:6], ["herdr", "agent", "start", "peer-c", "--kind", "claude"])
        self.assertEqual(cmd[6:8], ["--pane", "w1:pM"])

    def test_default_bypass_prepends_claude_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            rc = self._start(kind="claude")
        self.assertEqual(rc, 0)
        cmd = run_mock.call_args[0][0]
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)

    def test_default_bypass_prepends_codex_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="codex")
        cmd = run_mock.call_args[0][0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)

    def test_default_bypass_prepends_opencode_flag(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="opencode")
        cmd = run_mock.call_args[0][0]
        self.assertIn("--auto", cmd)

    def test_inherit_prepends_zero_flags(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")) as run_mock:
            self._start(kind="claude", permission_mode="inherit")
        cmd = run_mock.call_args[0][0]
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
        cmd = run_mock.call_args[0][0]
        self.assertIn("--extra-flag", cmd)
        self.assertLess(cmd.index("bypassPermissions"), cmd.index("--extra-flag"))



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
        self.assertTrue(prompt_call[4].endswith("(peer-from: claude sid-steward hearting-46)"))
        self.assertTrue(prompt_call[4].startswith("[handoff] do the thing\nline two"))
        rec = self._all_records()[-1]
        self.assertEqual(rec["kind"], "handoff")
        self.assertEqual(rec["to"], {"harness": "codex", "session_id": "thread-9", "name": "child"})
        self.assertEqual(rec["from"]["name"], "hearting-46")
        self.assertEqual(rec["summary"], "[handoff] do the thing")
        line = print_mock.call_args[0][0]
        self.assertIn("prompted=true", line)
        self.assertIn("to_session_id=thread-9", line)
        # the sender is now flagged as steward of the target
        self.assertIn(("claude", "sid-steward"), peer_steward.peer_message.read_steward_markers())

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

    def test_prompt_empty_body_sends_nothing(self):
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run") as run_mock, \
             mock.patch("builtins.print"):
            rc = peer_steward.main(["prompt", "child", "   "])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()
        self.assertEqual(self._all_records(), [])

    def test_wait_and_start_records_carry_the_resolved_session_id(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch.object(peer_steward.shutil, "which", return_value="/usr/bin/herdr"), \
             mock.patch.object(peer_steward.subprocess, "run",
                               side_effect=self._fake_run(_agent_json("claude", "sid-child", "peer-c", status="idle"))), \
             mock.patch("builtins.print"):
            peer_steward.main(["wait", "peer-c"])
            peer_steward.main(["start", "peer-c", "--kind", "claude", "--pane", "w1:pM"])
        recs = self._all_records()
        self.assertEqual([r["kind"] for r in recs], ["watch", "steer"])
        self.assertEqual(recs[0]["to"]["session_id"], "sid-child")
        self.assertEqual(recs[0]["to"]["harness"], "claude")
        self.assertEqual(recs[1]["to"]["session_id"], "sid-child")

    def test_opencode_env_identifies_an_opencode_steward(self):
        os.environ["OPENCODE_SESSION_ID"] = "ses_1"
        self.assertEqual(peer_steward._current_session_identity(), ("ses_1", "opencode"))



class F100cStewardModeTest(_TmpRootMixin, unittest.TestCase):
    def test_steward_on_off_raises_and_clears_the_flag(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-steward"
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 0)
        self.assertIn("steward=on", print_mock.call_args[0][0])
        self.assertIn(("claude", "sid-steward"), peer_steward.peer_message.read_steward_markers())
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "off"]), 0)
        self.assertIn("steward=off", print_mock.call_args[0][0])
        self.assertEqual(peer_steward.peer_message.read_steward_markers(), {})

    def test_steward_on_without_identity_is_a_typed_failure(self):
        with mock.patch("builtins.print") as print_mock:
            self.assertEqual(peer_steward.main(["steward", "on"]), 1)
        self.assertIn("reason=no-session-identity", print_mock.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
