#!/usr/bin/env python3
"""Unit tests for hooks/peer-message-record.py (SD-122)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
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
        for key in ("FLEET_TITLE_REFRESH", "MEM_DISTILL", "AGENT_SESSION_ROLE",
                    "AGENT_DISPATCH_DEPTH", "CLAUDE_CODE_CHILD_SESSION"):
            self.env.pop(key, None)

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

    def test_body_stdin_flag_carries_the_real_body_through_the_hook(self):
        """T-1 regression: body must reach peer-message.py via --body-stdin.

        Without --body-stdin on both args_list constructions, _read_body
        never sees the piped body and every record silently gets an empty
        summary and body_sha256 == sha256(""), even though the hook piped a
        real message via input=....encode().
        """
        message = "first line of the real message\nsecond line"
        proc = self._run("post-tool", self._sendmessage_payload(message=message))
        self.assertEqual(proc.returncode, 0)
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["summary"], "first line of the real message")
        self.assertNotEqual(recs[0]["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(recs[0]["body_sha256"], hashlib.sha256(message.encode()).hexdigest())

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

    def test_notice_body_stdin_flag_carries_receipt_body_through_the_hook(self):
        payload = {
            "session_id": "sid-receiver-exact",
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="named-peer">hi</cross-session-message>',
        }
        self._run("prompt", payload)
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        expected_body = "cross-session message received"
        self.assertEqual(recs[0]["summary"], expected_body)
        self.assertNotEqual(recs[0]["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(recs[0]["body_sha256"], hashlib.sha256(expected_body.encode()).hexdigest())

    def test_no_record_when_session_id_missing(self):
        payload = {
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="named-peer">hi</cross-session-message>',
        }
        self._run("prompt", payload)
        self.assertEqual(len(self._all_records()), 0)



# ---------------------------------------------------------------------------
# A56-6 — UserPromptSubmit sweep fallback carrier
# ---------------------------------------------------------------------------

_PEER_STEWARD = _HERE.parent / "utilities" / "peer-steward.py"

_FAKE_HERDR = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
target = argv[2] if len(argv) > 2 else "-"
print(json.dumps({"result": {"agent": {"agent": "claude",
    "agent_session": {"value": "sid-fake"}, "agent_status": "idle",
    "name": target, "pane_id": "w1:p9"}, "type": "agent_info"}}))
sys.exit(0)
'''


class SweepTest(_BaseTest):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.session = "sweep-session-1"

    def _steward(self, *argv, session_id=None):
        env = dict(self.env)
        env["CLAUDE_CODE_SESSION_ID"] = session_id or self.session
        return subprocess.run(
            [sys.executable, str(_PEER_STEWARD), *argv],
            capture_output=True, text=True, env=env, timeout=30,
        )

    def _watch_root(self):
        out = self._steward("status", "--json").stdout
        return Path(json.loads(out)["watch_root"])

    def _arm_and_complete(self, target, session_id=None):
        proc = self._steward("watch", target, session_id=session_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        watch_id = dict(
            t.split("=", 1) for t in proc.stdout.split() if "=" in t
        )["watch_id"]
        receipt = self._watch_root() / f"{watch_id}.receipt.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not receipt.exists():
            time.sleep(0.02)
        self.assertTrue(receipt.exists(), f"receipt never arrived for {watch_id}")
        return watch_id

    def _prompt(self, session_id=None, prompt=""):
        return self._run("prompt", {"session_id": session_id or self.session,
                                    "cwd": str(self.tmp_root), "prompt": prompt})

    def _context(self, proc):
        if not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_undelivered_receipt_is_surfaced_and_acked(self):
        watch_id = self._arm_and_complete("peer-a")
        proc = self._prompt()
        self.assertEqual(proc.returncode, 0)
        context = self._context(proc)
        self.assertIsNotNone(context, proc.stdout)
        self.assertIn(f"watch_id={watch_id}", context)
        self.assertIn("state=idle", context)
        ack = json.loads((self._watch_root() / f"{watch_id}.ack.json").read_text())
        self.assertEqual(ack["carrier"], "userprompt-sweep")

    def test_already_acked_receipt_produces_zero_output(self):
        watch_id = self._arm_and_complete("peer-a")
        self._steward("ack", watch_id, "--carrier", "claude-async-rewake")
        proc = self._prompt()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_another_sessions_receipt_is_never_surfaced(self):
        self._arm_and_complete("peer-b", session_id="someone-else")
        proc = self._prompt()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_sweep_is_capped_at_five_lines(self):
        for index in range(6):
            self._arm_and_complete(f"peer-{index}")
        context = self._context(self._prompt())
        receipt_lines = [l for l in context.splitlines() if l.startswith("state=")]
        self.assertEqual(len(receipt_lines), 5)

    def test_broken_peer_steward_stays_fail_soft_and_keeps_the_notice_record(self):
        # A `peer-steward.py` that cannot run must cost neither the prompt nor
        # the pre-existing cross-session `notice` record.
        env_path = self.env["PATH"]
        self.env["PATH"] = "/nonexistent"
        broken = self.tmp_root / "broken-steward.py"
        try:
            proc = self._run("prompt", {
                "session_id": self.session, "cwd": str(self.tmp_root),
                "prompt": '<cross-session-message from="peer-x">hi</cross-session-message>',
            })
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, b"")
            notices = [r for r in self._all_records() if r["kind"] == "notice"]
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["to"]["session_id"], self.session)
        finally:
            self.env["PATH"] = env_path
            del broken

    def test_prompt_without_session_id_writes_nothing(self):
        proc = self._run("prompt", {"cwd": str(self.tmp_root), "prompt": ""})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
class F100cTrailerAndNameTest(_BaseTest):
    """F-100c — the prompt path records a herdr-trailer steer as a `notice`, and both
    paths carry the sender's registry name."""

    def _cfg_with(self, sid, name):
        cfg = self.tmp_root / "cfg"
        (cfg / "sessions").mkdir(parents=True, exist_ok=True)
        (cfg / "sessions" / "77.json").write_text(json.dumps(
            {"sessionId": sid, "name": name, "nameSource": "derived"}))
        self.env["CLAUDE_CONFIG_DIR"] = str(cfg)

    def test_herdr_trailer_prompt_writes_a_herdr_notice_with_exact_sender(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
                             "prompt": "[steer] do x\n\n(peer-from: codex thread-9 f100-codex)"})
        recs = self._all_records()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["kind"], "notice")
        self.assertEqual(rec["delivery"]["surface"], "herdr")
        self.assertEqual(rec["delivery"]["status"], "received")
        self.assertEqual(rec["to"], {"harness": "claude", "session_id": "sid-recv"})
        self.assertEqual(rec["from"]["harness"], "codex")
        self.assertEqual(rec["from"]["session_id"], "thread-9")
        self.assertEqual(rec["from"]["name"], "f100-codex")
        self.assertEqual(rec["summary"], "herdr steer received")

    def test_plain_prompt_still_writes_nothing(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj", "prompt": "peer-from: x"})
        self.assertEqual(self._all_records(), [])

    def test_native_notice_carries_from_name_attribute(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
                             "prompt": '<cross-session-message from="uds:/x.sock" from-name="cairn-bc" from-mode="bypass">hi</cross-session-message>'})
        rec = self._all_records()[0]
        self.assertEqual(rec["from"]["name"], "cairn-bc")
        self.assertEqual(rec["to"]["name"], "uds:/x.sock")
        self.assertEqual(rec["delivery"]["surface"], "claude-native")

    def test_sent_record_carries_the_senders_registry_name(self):
        self._cfg_with("sid-sender", "hearting-46")
        self._run("post-tool", {"session_id": "sid-sender", "cwd": "/tmp/proj",
                                "tool_name": "SendMessage",
                                "tool_input": {"to": {"name": "peer-1"}, "message": "[steer] go"}})
        rec = self._all_records()[0]
        self.assertEqual(rec["from"]["name"], "hearting-46")
        self.assertEqual(rec["kind"], "steer")


class F100cHelperProcessGuardTest(_BaseTest):
    """A title refresher / memory distiller / worker never records — it is not a receiver
    even though the user's prompt (trailer included) is inside its own prompt."""

    def test_helper_env_writes_nothing_on_either_path(self):
        for key in ("FLEET_TITLE_REFRESH", "MEM_DISTILL", "AGENT_DISPATCH_DEPTH"):
            with self.subTest(key=key):
                self.env[key] = "1"
                self._run("prompt", {"session_id": "sid-helper", "cwd": "/tmp/proj",
                                     "prompt": "(peer-from: claude sid-a hearting-46)"})
                self._run("post-tool", {"session_id": "sid-helper", "cwd": "/tmp/proj",
                                        "tool_name": "SendMessage",
                                        "tool_input": {"to": {"name": "x"}, "message": "m"}})
                self.env.pop(key, None)
        self.env["AGENT_SESSION_ROLE"] = "worker"
        self._run("prompt", {"session_id": "sid-helper", "cwd": "/tmp/proj",
                             "prompt": "(peer-from: claude sid-a hearting-46)"})
        self.assertEqual(self._all_records(), [])

    def test_an_interactive_child_session_is_still_a_receiver(self):
        self.env["CLAUDE_CODE_CHILD_SESSION"] = "1"
        self._run("prompt", {"session_id": "sid-child", "cwd": "/tmp/proj",
                             "prompt": "x (peer-from: claude sid-a hearting-46)"})
        self.assertEqual(len(self._all_records()), 1)


if __name__ == "__main__":
    unittest.main()
