#!/usr/bin/env python3
"""Unit tests for hooks/peer-message-record.py (SD-122)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HOOK = _HERE / "peer-message-record.py"


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self.env = dict(os.environ)
        self.env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        self.env.pop("AGENT_HOME", None)

    def _run(self, mode, payload):
        return subprocess.run(
            [sys.executable, str(_HOOK), mode],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            env=self.env,
            timeout=10,
        )

    def _ledger_root(self):
        return self.tmp_root / "peer-messages"

    def _all_records(self):
        root = self._ledger_root()
        if not root.is_dir():
            return []
        recs = []
        for month in root.glob("*"):
            for f in month.glob("*.jsonl"):
                for line in f.read_text().splitlines():
                    if line.strip():
                        recs.append(json.loads(line))
        return recs


class PostToolTest(_BaseTest):
    def _sendmessage_payload(self, message="hello", notify_when_idle=False, to=None):
        return {
            "session_id": "sid-sender",
            "cwd": "/tmp/proj",
            "tool_name": "SendMessage",
            "tool_input": {
                "to": to if to is not None else {"name": "peer-1"},
                "message": message,
                "notify_when_idle": notify_when_idle,
            },
        }

    def test_sendmessage_writes_exactly_one_record(self):
        proc = self._run("post-tool", self._sendmessage_payload())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(self._all_records()), 1)

    def test_notify_when_idle_maps_to_watch(self):
        self._run("post-tool", self._sendmessage_payload(notify_when_idle=True))
        recs = self._all_records()
        self.assertEqual(recs[0]["kind"], "watch")

    def test_no_prefix_maps_to_steer(self):
        self._run("post-tool", self._sendmessage_payload(message="plain message"))
        recs = self._all_records()
        self.assertEqual(recs[0]["kind"], "steer")

    def test_steer_prefix_maps_to_steer(self):
        self._run("post-tool", self._sendmessage_payload(message="[steer] do X"))
        recs = self._all_records()
        self.assertEqual(recs[0]["kind"], "steer")

    def test_handoff_prefix_maps_to_handoff(self):
        self._run("post-tool", self._sendmessage_payload(message="[handoff] take over"))
        recs = self._all_records()
        self.assertEqual(recs[0]["kind"], "handoff")

    def test_gate_prefix_maps_to_gate_relay(self):
        self._run("post-tool", self._sendmessage_payload(message="[gate] approve?"))
        recs = self._all_records()
        self.assertEqual(recs[0]["kind"], "gate-relay")

    def test_non_sendmessage_tool_writes_nothing(self):
        payload = {"session_id": "sid-sender", "cwd": "/tmp/proj", "tool_name": "Bash",
                   "tool_input": {"command": "ls"}}
        self._run("post-tool", payload)
        self.assertEqual(len(self._all_records()), 0)

    def test_record_failure_does_not_block_send(self):
        ro_root = self.tmp_root / "ro"
        ro_root.mkdir()
        (ro_root / "jobs.log").touch()
        os.chmod(ro_root, 0o500)
        self.addCleanup(lambda: os.chmod(ro_root, 0o700))
        broken_env = dict(self.env)
        broken_env["AGENT_DISPATCH_JOBS"] = str(ro_root / "jobs.log")
        proc = subprocess.run(
            [sys.executable, str(_HOOK), "post-tool"],
            input=json.dumps(self._sendmessage_payload()).encode("utf-8"),
            capture_output=True,
            env=broken_env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(b"permissionDecision", proc.stdout)
        self.assertNotIn(b"deny", proc.stdout)

    def test_hook_never_raises_on_malformed_stdin(self):
        proc = subprocess.run(
            [sys.executable, str(_HOOK), "post-tool"],
            input=b"{not valid json",
            capture_output=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)

        proc2 = subprocess.run(
            [sys.executable, str(_HOOK), "post-tool"],
            input=b"",
            capture_output=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(proc2.returncode, 0)


class PromptTest(_BaseTest):
    def test_prompt_with_cross_session_marker_writes_notice(self):
        payload = {
            "session_id": "sid-receiver",
            "cwd": "/tmp/proj",
            "prompt": 'before <cross-session-message from="hearting-21 [f3e821]">hi</cross-session-message> after',
        }
        proc = self._run("prompt", payload)
        self.assertEqual(proc.returncode, 0)
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["kind"], "notice")

    def test_prompt_without_marker_writes_nothing(self):
        payload = {"session_id": "sid-receiver", "cwd": "/tmp/proj", "prompt": "just a normal prompt"}
        self._run("prompt", payload)
        self.assertEqual(len(self._all_records()), 0)

    def test_prompt_path_emits_no_stdout(self):
        payload = {
            "session_id": "sid-receiver",
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="peer">hi</cross-session-message>',
        }
        proc = self._run("prompt", payload)
        self.assertEqual(len(proc.stdout), 0)

    def test_notice_record_has_no_body_text(self):
        payload = {
            "session_id": "sid-receiver",
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="peer">SECRET-BODY-TEXT</cross-session-message>',
        }
        self._run("prompt", payload)
        recs = self._all_records()
        raw = json.dumps(recs)
        self.assertNotIn("SECRET-BODY-TEXT", raw)

    def test_notice_uses_exact_session_id_not_name_fallback(self):
        payload = {
            "session_id": "sid-receiver-exact",
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="named-peer">hi</cross-session-message>',
        }
        self._run("prompt", payload)
        recs = self._all_records()
        self.assertEqual(recs[0]["to"]["session_id"], "sid-receiver-exact")

    def test_no_record_when_session_id_missing(self):
        payload = {
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="named-peer">hi</cross-session-message>',
        }
        self._run("prompt", payload)
        self.assertEqual(len(self._all_records()), 0)


if __name__ == "__main__":
    unittest.main()
