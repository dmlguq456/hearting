#!/usr/bin/env python3
"""Unit tests for hooks/peer-message-record.py (SD-122)."""
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_HOOK = _HERE / "peer-message-record.py"
_UTILITIES = _HERE.parent / "utilities"
sys.path.insert(0, str(_UTILITIES))
import dispatch_pending_delivery as pending_delivery
import peer_obligations
_PEER_SPEC = importlib.util.spec_from_file_location("hook_test_peer_message", _UTILITIES / "peer-message.py")
peer_message = importlib.util.module_from_spec(_PEER_SPEC)
_PEER_SPEC.loader.exec_module(peer_message)


class _BaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_root = Path(self._tmp.name)
        self.jobs_path = self.tmp_root / "jobs.log"
        self.jobs_path.touch()
        self.env = dict(os.environ)
        for key in tuple(self.env):
            if (key.startswith(("AGENT_DISPATCH_", "AGENT_ARTIFACT_", "AGENT_RUNTIME_",
                                "AGENT_SESSION_", "AGENT_THREAD_", "HERDR_"))
                    or key in {"AGENT_HOME", "CLAUDE_HOME", "CODEX_THREAD_ID",
                               "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
                               "OPENCODE_SESSION_ID", "SERVICE_CURRENT",
                               "HEARTING_SERVICE_CURRENT", "AGENT_SERVICE_CURRENT"}):
                self.env.pop(key, None)
        self.env["AGENT_DISPATCH_JOBS"] = str(self.jobs_path)
        # C-1 moved the peer ledger's writer root off AGENT_DISPATCH_JOBS onto
        # peer_state_root(); isolating only AGENT_DISPATCH_JOBS no longer keeps this
        # fixture's records out of the real per-user ledger root. Point it at the
        # same tmp_root the `_ledger_root()` helper below already expects.
        self.env["AGENT_PEER_LEDGER_ROOT"] = str(self.tmp_root)
        self.env.update({
            "HOME": str(self.tmp_root / "home"),
            "XDG_STATE_HOME": str(self.tmp_root / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.tmp_root / "xdg-config"),
            "XDG_DATA_HOME": str(self.tmp_root / "xdg-data"),
            "HARNESS_STATE_ROOT": str(self.tmp_root / "harness-state"),
            "AGENT_ARTIFACT_ROOT": str(self.tmp_root / "artifacts"),
            "AGENT_ARTIFACT_OUTPUT_DIR": str(self.tmp_root / "artifacts" / "output"),
            "AGENT_RUNTIME_ROOT": str(self.tmp_root / "runtime"),
            "CODEX_HOME": str(self.tmp_root / "home" / ".codex"),
            "CLAUDE_HOME": str(self.tmp_root / "runtime" / "claude"),
            "CLAUDE_CONFIG_DIR": str(self.tmp_root / "home" / ".claude"),
            "OPENCODE_CONFIG_DIR": str(self.tmp_root / "home" / ".config" / "opencode"),
            "FAKE_HERDR_SHELL_PID": str(os.getpid()),
        })
        self.env.pop("AGENT_HOME", None)
        for key in ("AGENT_SERVICE_CURRENT", "HEARTING_SERVICE_CURRENT", "SERVICE_CURRENT",
                    "AGENT_INSTALL_CURRENT", "AGENT_INSTALL_ROOT", "HERDR_SOCKET_PATH",
                    "AGENT_HERDR_SESSION", "HERDR_SESSION"):
            self.env.pop(key, None)
        for key in ("FLEET_TITLE_REFRESH", "MEM_DISTILL", "AGENT_SESSION_ROLE",
                    "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH", "CLAUDE_CODE_CHILD_SESSION",
                    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "AGENT_SESSION_ID",
                    "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID",
                    "AGENT_DISPATCH_CALLER_HARNESS", "AGENT_DISPATCH_CURRENT_HARNESS"):
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


class NativeContextReceiptTest(_BaseTest):
    def _seed_message(self, *, recipient_sid="sid-native-context", refs=()):
        with mock.patch.dict(os.environ, self.env, clear=True):
            text, ref = peer_message.prepare_peer_message(
                "[steer] isolated native hook payload",
                {"harness": "codex", "session_id": "thread-native-sender", "name": "sender"},
                {"harness": "claude", "session_id": recipient_sid, "name": "receiver"},
                defer=True, refs=refs,
            )
        return text, ref

    def _seed_registered_message(self, *, runtime=False):
        sid = "sid-native-obligation"
        attempt_id = "att-native-context-owner"
        delivery_id = "delivery-native-context"
        duty_id = "registered-batch-" + "c" * 32
        carrier = "claude-parent-runtime"
        root = self.jobs_path.parent
        owner = "retained-rewake:" + duty_id
        self.jobs_path.write_text(
            "2026-10-18T00:00:00Z\tdone\t/repo\t/wt\tfixture\t"
            "attempt_schema_version=2,dispatch_depth=1,attempt_id=" + attempt_id
            + ",parent_sid=" + sid + ",parent_harness=claude,harness=claude"
            + ",parent_completion_delivery=" + carrier + ",delivery_id=" + delivery_id
            + ",route_id=rt-native-context,route_node=execute"
            + ",note=completed-marker,failure_class=pass\n",
            encoding="utf-8",
        )
        identity = {"jobs": str(self.jobs_path.resolve()), "attempt_id": attempt_id,
                    "session_id": sid, "harness": "claude", "server": "default"}
        store = peer_obligations.ObligationStore(root)
        store.create(duty_id, "registered-batch", identity, {"carrier": carrier})
        receipt = {
            "schema_version": 2, "state": "delivered", "kind": "notice",
            "parent_attempt_id": "att-native-context-parent",
            "job_registry": str(self.jobs_path), "delivery_classification": "attention",
            "children": [{"attempt_id": attempt_id, "status": "done", "readiness": "ready",
                          "reason": "fixture", "required_action": "inspect-done-failure",
                          "harness": "claude", "delivery_classification": "attention"}],
        }
        receipt_digest = pending_delivery._canonical_receipt_digest(receipt)
        pending_delivery.create(
            root, recipient_kind=carrier, recipient_key=sid, delivery_id=delivery_id,
            session_generation="", session_generation_supported="0",
            attempt_ids=[attempt_id], parent_attempt_id="att-native-context-parent",
            route_id="rt-native-context", route_node="execute", receipt=receipt,
            receipt_digest=receipt_digest,
            row_revisions={attempt_id: "fixture"},
        )
        pending_delivery.claim(root, sid, delivery_id, claim_owner=owner, lease_seconds=60)
        binding = {"duty_id": duty_id, "jobs": str(self.jobs_path.resolve()),
                   "attempt_id": attempt_id, "carrier": carrier, "recipient_sid": sid,
                   "storage_recipient": sid, "delivery_id": delivery_id, "claim_owner": owner}
        if runtime:
            producer = {
                "schema": peer_message._RUNTIME_PRODUCER_SCHEMA,
                "duty_id": duty_id, "jobs": str(self.jobs_path.resolve()),
                "attempt_id": attempt_id, "carrier": carrier,
                "delivery_id": delivery_id, "receipt_digest": receipt_digest,
                "recipient": {"harness": "claude", "session_id": sid},
                "route_id": "rt-native-context", "route_node": "execute",
            }
            with mock.patch.dict(os.environ, self.env, clear=True):
                text, ref = peer_message.prepare_peer_message(
                    "[notice] runtime task completion",
                    {"harness": "runtime", "session_id": None, "name": "Hearting runtime"},
                    {"harness": "claude", "session_id": sid, "name": "receiver"},
                    defer=True, refs=[duty_id, attempt_id, delivery_id],
                    producer_binding=producer, receipt="native-pending")
        else:
            text, ref = self._seed_message(recipient_sid=sid, refs=[duty_id])
        with mock.patch.dict(os.environ, self.env, clear=True):
            peer_message.bind_dispatch_notice(ref, binding)
        return text, ref, sid, duty_id, delivery_id, root

    def _prompt(self, sid):
        return self._run("prompt", {"session_id": sid, "cwd": str(self.tmp_root), "prompt": ""})

    def test_native_hook_receipt_completes_exact_registered_obligation_once(self):
        text, ref, sid, duty_id, delivery_id, root = self._seed_registered_message()
        with mock.patch.dict(os.environ, self.env, clear=True):
            origin = peer_message.prompt_origin(text, "claude", sid)
            self.assertEqual(origin["kind"], "runtime-completion")
            self.assertTrue(origin["legacy"])
            import session_tidy
            session_tidy.run_hook("claude", "prompt", sid,
                                  cwd=str(self.tmp_root), prompt_text=text)
            route_spec = importlib.util.spec_from_file_location(
                "legacy_runtime_route_source", _UTILITIES / "capability-route.py")
            route = importlib.util.module_from_spec(route_spec)
            route_spec.loader.exec_module(route)
            self.assertEqual(route._turn_peer_source("claude", sid, str(self.tmp_root)),
                             f"runtime:completion·{origin['producer_binding']['attempt_id']}"
                             f"/{delivery_id}")
        first = self._prompt(sid)
        self.assertEqual(first.returncode, 0, first.stderr)
        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, text)
        with mock.patch.dict(os.environ, self.env, clear=True):
            received = peer_message._read_pending(ref)
            self.assertEqual((received["state"], received["receipt"], received["ref"]),
                             ("received", "exact-peer-ref", ref))
            self.assertEqual(received["body_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(received["from"]["session_id"], "thread-native-sender")
            self.assertEqual(received["to"], {"harness": "claude", "session_id": sid,
                                               "name": "receiver"})
            self.assertIsNone(received["text"])
            transfer = peer_message._read_transfer_record(ref)
            self.assertEqual(transfer["body_sha256"], received["body_sha256"])
            self.assertEqual(pending_delivery.read(root, sid, delivery_id)["state"], "acked")
            self.assertEqual(peer_obligations.ObligationStore(root).get(duty_id)["state"], "complete")
        records = [row for row in self._all_records() if row.get("transfer_ref") == ref]
        self.assertEqual(len(records), 1)
        duplicate = self._prompt(sid)
        self.assertEqual(duplicate.stdout, b"")
        self.assertEqual([row for row in self._all_records() if row.get("transfer_ref") == ref], records)

    def test_subagent_post_tool_leaves_parent_message_pending_until_main_prompt(self):
        text, ref, sid, duty_id, delivery_id, root = self._seed_registered_message()
        post = self._run("post-tool", {
            "session_id": sid, "cwd": str(self.tmp_root), "tool_name": "Bash",
            "agent_id": "agent-child-123", "agent_type": "Explore",
        })
        self.assertEqual(post.returncode, 0, post.stderr)
        self.assertEqual(post.stdout, b"")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(peer_message._read_pending(ref)["state"], "pending")
            self.assertEqual(pending_delivery.read(root, sid, delivery_id)["state"], "claimed")
            self.assertEqual(peer_obligations.ObligationStore(root).get(duty_id)["state"], "pending")

        main = self._prompt(sid)
        self.assertEqual(main.returncode, 0, main.stderr)
        self.assertEqual(json.loads(main.stdout)["hookSpecificOutput"]["additionalContext"], text)
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(peer_message._read_pending(ref)["state"], "received")
            self.assertEqual(pending_delivery.read(root, sid, delivery_id)["state"], "acked")
            self.assertEqual(peer_obligations.ObligationStore(root).get(duty_id)["state"], "complete")

    def test_agent_type_alone_still_delivers_main_context(self):
        text, _ref = self._seed_message()
        spec = importlib.util.spec_from_file_location("agent_type_main_hook", _HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        payload = {"session_id": "sid-native-context", "cwd": str(self.tmp_root),
                   "prompt": "", "agent_type": "Explore"}
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "argv", [str(_HOOK), "prompt"]):
            output = io.StringIO()
            with mock.patch.object(sys, "stdout", output):
                hook.main()
        self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"], text)

    def test_subagent_sendmessage_is_still_recorded(self):
        payload = {
            "session_id": "sid-subagent-sender", "cwd": str(self.tmp_root),
            "tool_name": "SendMessage", "agent_id": "agent-child-456",
            "tool_input": {"to": {"session_id": "sid-recipient", "name": "recipient"},
                           "message": "[steer] recorded from subagent"},
        }
        result = self._run("post-tool", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self._all_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["from"]["session_id"], "sid-subagent-sender")
        self.assertEqual(records[0]["body_sha256"],
                         hashlib.sha256(b"[steer] recorded from subagent").hexdigest())

    def test_runtime_completion_keeps_runtime_identity_through_native_receipt_and_ack(self):
        text, ref, sid, duty_id, delivery_id, root = self._seed_registered_message(runtime=True)
        self.assertIn("(runtime-from:", text)
        self.assertNotIn("(peer-from:", text)
        first = self._prompt(sid)
        self.assertEqual(first.returncode, 0, first.stderr)
        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, text)
        with mock.patch.dict(os.environ, self.env, clear=True):
            received = peer_message._read_pending(ref)
            transfer = peer_message._read_transfer_record(ref)
            producer = transfer["producer_binding"]
            self.assertEqual(received["state"], "received")
            self.assertEqual(received["from"]["harness"], "runtime")
            self.assertEqual(received["from"]["session_id"], None)
            self.assertEqual(received["producer_binding"], producer)
            self.assertEqual(transfer["from"]["harness"], "runtime")
            self.assertEqual(pending_delivery.read(root, sid, delivery_id)["state"], "acked")
            self.assertEqual(peer_obligations.ObligationStore(root).get(duty_id)["state"], "complete")
        records = [row for row in self._all_records() if row.get("transfer_ref") == ref]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["from"]["harness"], "runtime")
        self.assertEqual(records[0]["producer_binding"], producer)
        duplicate = self._prompt(sid)
        self.assertEqual(duplicate.stdout, b"")
        self.assertEqual([row for row in self._all_records() if row.get("transfer_ref") == ref], records)

    def test_retained_hook_context_requires_a_fresh_native_delivery(self):
        text, ref = self._seed_message()
        with mock.patch.dict(os.environ, self.env, clear=True):
            with peer_message.pending_lock(ref):
                row = peer_message._read_pending(ref)
                peer_message._save_pending(dict(row, state="queued", receipt="hook-context-emitted"))
            peer_message.receive_peer_message("ordinary next prompt", {
                "harness": "claude", "session_id": "sid-native-context"})
            self.assertEqual(peer_message._read_pending(ref)["state"], "queued")
        delivered = self._prompt("sid-native-context")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        context = json.loads(delivered.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(context, text)
        with mock.patch.dict(os.environ, self.env, clear=True):
            row = peer_message._read_pending(ref)
            self.assertEqual((row["state"], row["receipt"]), ("received", "exact-peer-ref"))

    def test_native_hook_wrong_session_leaves_the_exact_transfer_pending(self):
        text, ref = self._seed_message()
        output = self._prompt("sid-other-native-context")
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, b"")
        with mock.patch.dict(os.environ, self.env, clear=True):
            row = peer_message._read_pending(ref)
            self.assertEqual((row["state"], row["text"]), ("pending", text))
        self.assertFalse([entry for entry in self._all_records()
                          if entry.get("transfer_ref") == ref])

    def test_native_hook_partial_flush_keeps_claim_and_original_body(self):
        text, ref = self._seed_message()
        spec = importlib.util.spec_from_file_location(
            "native_hook_partial_flush", _HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)

        class PartialOutput:
            def __init__(self):
                self.data = ""
            def write(self, value):
                self.data += value
            def flush(self):
                raise OSError("fixture output flush failed")

        stream = PartialOutput()
        payload = {"session_id": "sid-native-context", "cwd": str(self.tmp_root), "prompt": ""}
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", stream), \
                mock.patch.object(sys, "argv", [str(_HOOK), "prompt"]):
            with self.assertRaisesRegex(OSError, "output flush failed"):
                hook.main()
            row = peer_message._read_pending(ref)
        self.assertEqual(
            json.loads(stream.data)["hookSpecificOutput"]["additionalContext"], text)
        self.assertEqual((row["state"], row["receipt"], row["text"]),
                         ("unverified", "hook-context-inflight", text))
        self.assertTrue(row["rpc_claim"])
        self.assertFalse([entry for entry in self._all_records()
                          if entry.get("transfer_ref") == ref])

    def test_native_hook_rejects_corrupt_body_without_receipt(self):
        _text, ref = self._seed_message()
        with mock.patch.dict(os.environ, self.env, clear=True):
            path = peer_message._pending_path(ref)
            row = json.loads(path.read_text(encoding="utf-8"))
            row["text"] = row["text"].replace("isolated native hook payload", "wrong body")
            path.write_text(json.dumps(row), encoding="utf-8")
        output = self._prompt("sid-native-context")
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout, b"")
        with mock.patch.dict(os.environ, self.env, clear=True):
            path = peer_message._pending_path(ref)
            self.assertFalse(path.exists())
            quarantined = list((path.parent.parent / "quarantine").glob(f"*/{ref}.json"))
            self.assertEqual(len(quarantined), 1)
        self.assertFalse([entry for entry in self._all_records()
                          if entry.get("transfer_ref") == ref])


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

    def test_notify_when_idle_send_raises_no_steward_marker(self):
        """F-100c-2 (review round 1 #1): the hook's `kind=watch` row is a delivery option,
        not the steward role — a plain `[handoff]` with notify_when_idle used to flag the
        sender as d=−1."""
        for message in ("[handoff] done — please review", "plain message", "[steer] do X"):
            self._run("post-tool", self._sendmessage_payload(message=message, notify_when_idle=True))
        self.assertEqual(len(self._all_records()), 3)
        self.assertFalse((self.tmp_root / "peer-steward").exists())

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
        # Keep the ledger root under the same read-only tree so the write still fails
        # (see the AGENT_PEER_LEDGER_ROOT comment in setUp) — otherwise the writable
        # tmp_root from self.env would mask the failure this test means to exercise.
        broken_env["AGENT_PEER_LEDGER_ROOT"] = str(ro_root / "peer-state")
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

    def test_peer_prompt_emits_receiver_guidance_with_receipt_only(self):
        payload = {
            "session_id": "sid-receiver",
            "cwd": "/tmp/proj",
            "prompt": 'x <cross-session-message from="peer">hi</cross-session-message>',
        }
        proc = self._run("prompt", payload)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Receipt/queue/ACK is not execution", context)

    def test_prompt_guidance_receives_exact_claude_payload_identity(self):
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location("peer_message_record_hook_test", _HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        prompt = "actual Claude prompt (peer-from: codex [0a] peer ; ref=abc)"
        payload = {"session_id": "claude-payload-session", "cwd": "/tmp/project", "prompt": prompt}
        calls = []
        mod = SimpleNamespace(
            receiver_guidance=lambda *args, **kwargs: calls.append((args, kwargs)) or "ordinary guidance",
            emit_peer_context=lambda *args, **kwargs: None,
        )
        with mock.patch.object(hook, "_is_helper_process", return_value=False), \
             mock.patch.object(hook, "_read_stdin_json", return_value=payload), \
             mock.patch.object(hook, "handle_prompt"), \
             mock.patch.object(hook, "sweep_undelivered", return_value=[]), \
             mock.patch.object(hook, "_peer_message_module", return_value=mod), \
             mock.patch.object(hook.sys, "argv", [str(_HOOK), "prompt"]):
            hook.main()
        self.assertEqual(calls, [((prompt, {"harness": "claude", "session_id": "claude-payload-session"}),
                                  {"cwd": "/tmp/project"})])

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
if argv[:2] == ["pane", "process-info"]:
    shell_pid = int(os.environ.get("FAKE_HERDR_SHELL_PID", os.getppid()))
    print(json.dumps({"result": {"process_info": {
        "pane_id": argv[-1], "shell_pid": shell_pid,
        "foreground_process_group_id": shell_pid,
        "foreground_processes": [{"pid": shell_pid, "argv": ["zsh"]}]}}}))
    sys.exit(0)
if argv[:2] == ["pane", "list"]:
    print(json.dumps({"result": {"panes": [{"pane_id": "w1:p9", "agent": "claude",
        "agent_session": {"value": "sid-fake"}, "agent_status": "idle"}]}}))
    sys.exit(0)
print(json.dumps({"result": {"agent": {"agent": "claude",
    "agent_session": {"value": "sid-fake"}, "agent_status": "idle",
    "name": target, "pane_id": "w1:p9"}, "type": "agent_info"}}))
sys.exit(0)
'''


class SweepTest(_BaseTest):
    def setUp(self):
        super().setUp()
        # The normal watch path has always supported a registry that has not
        # been created yet. Keep this real runner fixture on that path; peer
        # receipt delivery must not require an empty jobs.log setup file.
        self.jobs_path.unlink()
        self.bin = self.tmp_root / "fakebin"
        self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text(_FAKE_HERDR)
        fake.chmod(0o755)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.session = "sweep-session-1"
        # A pane census must report a live, fixture-owned foreground shell;
        # the hook/observer children must not borrow a caller's Herdr socket.
        self.shell = subprocess.Popen(
            ["/bin/sleep", "60"], env=self.env, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop_fixture_shell)
        self.env["FAKE_HERDR_SHELL_PID"] = str(self.shell.pid)

    def _stop_fixture_shell(self):
        if self.shell.poll() is None:
            self.shell.kill()
            self.shell.wait(timeout=5)

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
        if not receipt.exists():
            paths = self.tmp_root / "peer-steward" / "obligations"
            observation_path = self._watch_root() / f"{watch_id}.observation.json"
            arm_path = self._watch_root() / f"{watch_id}.json"
            watcher_log = self._watch_root() / f"{watch_id}.log"
            evidence = {
                "arm": json.loads(arm_path.read_text()) if arm_path.exists() else None,
                "observation": json.loads(observation_path.read_text())
                    if observation_path.exists() else None,
                "watcher_log": watcher_log.read_text(errors="replace")[-3000:]
                    if watcher_log.exists() else "",
                "obligation_files": sorted(path.name for path in paths.glob("*"))
                    if paths.is_dir() else [],
                "readiness_projections": [
                    {key: value.get(key) for key in
                     ("identity", "result", "lineage", "generation")}
                    for projection_path in sorted((paths / "readiness-v1").glob("*.json"))
                    for value in [json.loads(projection_path.read_text())]
                ] if (paths / "readiness-v1").is_dir() else [],
            }
            self.fail(f"receipt never arrived for {watch_id}; diagnostics={evidence}")
        self.assertFalse(self.jobs_path.exists(),
                         "watch readiness must not create a dispatch registry")
        # The watcher renames its receipt into place *before* it appends its
        # `notice` row, and it holds `<watch_id>.lock` until process exit. Wait
        # for that lock, so a caller counting notice rows never races the
        # watcher's own trailing write.
        lock_path = self._watch_root() / f"{watch_id}.lock"
        fd = os.open(lock_path, os.O_RDWR)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    self.assertLess(time.monotonic(), deadline,
                                    f"watcher never exited for {watch_id}")
                    time.sleep(0.02)
        finally:
            os.close(fd)
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

    def _hook_copy_with_broken_steward(self):
        # E-m5: the hook calls `_PEER_STEWARD_PY`, built from `__file__` at
        # module load, so `PATH` never touches it -- the previous version of
        # this test set PATH="/nonexistent" and asserted a pass, but that
        # never actually routed through the fail-soft branch in
        # `sweep_undelivered` (dispatch_contract.py: `except Exception:
        # return []`). Copy the hook and a full sibling `utilities/` tree
        # into a fresh root and swap in a steward stub that exits non-zero
        # immediately, so the copied hook's own resolved `_PEER_STEWARD_PY`
        # actually points at a broken process.
        root = self.tmp_root / "broken-steward-tree"
        (root / "hooks").mkdir(parents=True)
        shutil.copyfile(_HOOK, root / "hooks" / "peer-message-record.py")
        shutil.copytree(_HERE.parent / "utilities", root / "utilities")
        stub = root / "utilities" / "peer-steward.py"
        stub.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        stub.chmod(0o755)
        return root / "hooks" / "peer-message-record.py"

    def test_broken_peer_steward_stays_fail_soft_and_keeps_the_notice_record(self):
        # A `peer-steward.py` that cannot run must cost neither the prompt nor
        # the pre-existing cross-session `notice` record, and the un-acked
        # receipt it could not read must stay un-acked (never silently
        # consumed by a sweep that could not actually see it).
        watch_id = self._arm_and_complete("peer-a")
        notices_before = [r for r in self._all_records() if r["kind"] == "notice"]
        hook_path = self._hook_copy_with_broken_steward()
        proc = subprocess.run(
            [sys.executable, str(hook_path), "prompt"],
            input=json.dumps({
                "session_id": self.session, "cwd": str(self.tmp_root),
                "prompt": '<cross-session-message from="peer-x">hi</cross-session-message>',
            }).encode("utf-8"),
            capture_output=True, env=self.env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("immediate results once", context)
        self.assertNotIn("undelivered watch receipts", context)
        notices_after = [r for r in self._all_records() if r["kind"] == "notice"]
        # exactly one new notice: the cross-session-message record. The broken
        # steward must neither block it nor add or remove anything else.
        self.assertEqual(len(notices_after), len(notices_before) + 1)
        new_notice = [r for r in notices_after if r not in notices_before][0]
        self.assertEqual(new_notice["to"]["session_id"], self.session)
        self.assertFalse((self._watch_root() / f"{watch_id}.ack.json").exists())

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
        result = self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
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
        self.assertIn(b"immediate results once", result.stdout)
        self.assertIn(b"Receipt/queue/ACK is not execution", result.stdout)

    def test_plain_prompt_still_writes_nothing(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj", "prompt": "peer-from: x"})
        self.assertEqual(self._all_records(), [])

    def test_native_notice_carries_from_name_attribute(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
                             "prompt": '<cross-session-message from="uds:/x.sock" from-name="cairn-bc" from-mode="bypass">hi</cross-session-message>'})
        rec = self._all_records()[0]
        self.assertEqual(rec["from"]["name"], "cairn-bc")
        # The sender's address used to be written into the `to` block — one endpoint's
        # identity filed under the other endpoint's key. `to` is this session.
        self.assertNotIn("uds:/x.sock", json.dumps(rec["to"]))
        self.assertEqual(rec["delivery"]["surface"], "claude-native")

    def test_a_native_sender_is_identified_by_its_socket(self):
        """Claude's envelope carries no sender session id, but `from=` is the sender's
        socket and the socket is named after its PID — which the runtime also writes a
        session file for. Without this the ledger stored the address as a name and the
        board drew a bare `claude` (222 rows measured 2026-09-10)."""
        self._cfg_with("sid-recv", "receiver")
        cfg = Path(self.env["CLAUDE_CONFIG_DIR"])
        (cfg / "sessions" / "4242.json").write_text(json.dumps(
            {"pid": 4242, "sessionId": "sid-sender", "name": "peer-a"}))
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
                             "prompt": '<cross-session-message from="uds:/run/user/1002/cc-socks/4242.sock" from-name="peer-a">hi</cross-session-message>'})
        rec = self._all_records()[0]
        self.assertEqual(rec["from"]["session_id"], "sid-sender")
        self.assertEqual(rec["to"]["session_id"], "sid-recv")

    def test_an_unresolvable_address_still_records_an_honest_empty_id(self):
        self._run("prompt", {"session_id": "sid-recv", "cwd": "/tmp/proj",
                             "prompt": '<cross-session-message from="uds:/run/user/1002/cc-socks/999999.sock">hi</cross-session-message>'})
        rec = self._all_records()[0]
        self.assertEqual(rec["from"]["session_id"], "")

    def test_a_sent_record_resolves_the_targets_socket_to_its_session_id(self):
        self._cfg_with("sid-sender", "hearting-46")
        cfg = Path(self.env["CLAUDE_CONFIG_DIR"])
        (cfg / "sessions" / "4243.json").write_text(json.dumps(
            {"pid": 4243, "sessionId": "sid-target", "name": "peer-b"}))
        self._run("post-tool", {"session_id": "sid-sender", "cwd": "/tmp/proj",
                                "tool_name": "SendMessage",
                                "tool_input": {"to": "uds:/run/user/1002/cc-socks/4243.sock",
                                               "message": "[steer] go"}})
        rec = self._all_records()[0]
        self.assertEqual(rec["to"]["session_id"], "sid-target")

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
