#!/usr/bin/env python3
"""Regression tests for the native Codex queue courier."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import stat
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "codex_queue_delivery", HERE / "codex_queue_delivery.py"
)
QUEUE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = QUEUE
SPEC.loader.exec_module(QUEUE)


SOCKET = Path("/tmp/app-server.sock")
THREAD = "01a0e1c0-bd78-7a30-aea5-a7cf4f3356e3"
CLIENT_ID = "delivery-test-0001"
ITEM_ID = "01a0e1c1-fdb8-7190-8524-6812c28b3f9a"
ITEM = {"id": ITEM_ID, "clientUserMessageId": CLIENT_ID,
        "input": [{"type": "text", "text": "fixture"}]}


def native_fixture(root, *, entries=(), turn_ids=(), thread_id=THREAD,
                   rollout_bytes=b"", projected_offset=None,
                   projected_ordinal=10000, rollout_mode=0o600):
    home = Path(root) / "codex"
    control = home / "app-server-control"
    sessions = home / "sessions"
    control.mkdir(parents=True)
    sessions.mkdir()
    home.chmod(0o700)
    control.chmod(0o700)
    sessions.chmod(0o700)
    rollout = sessions / "fixture.jsonl"
    rollout.write_bytes(rollout_bytes)
    rollout.chmod(rollout_mode)

    state_path = home / "state_5.sqlite"
    state = sqlite3.connect(state_path)
    state.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, history_mode TEXT)")
    state.execute("INSERT INTO threads VALUES (?, ?, 'paginated')",
                  (thread_id, str(rollout)))
    state.commit()
    state.close()
    state_path.chmod(0o600)

    history_path = home / "thread_history_1.sqlite"
    history = sqlite3.connect(history_path)
    history.executescript("""
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, status TEXT,
            PRIMARY KEY(thread_id, turn_id)
        );
        CREATE TABLE thread_items (
            thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
            item_json TEXT NOT NULL, item_type TEXT NOT NULL, rollout_ordinal INTEGER,
            PRIMARY KEY(thread_id, turn_id, item_id)
        );
        CREATE INDEX idx_thread_items_user_messages
            ON thread_items(thread_id, rollout_ordinal) WHERE item_type='userMessage';
        CREATE TABLE thread_history_projection_state (
            thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER,
            next_rollout_ordinal INTEGER
        );
    """)
    all_turns = {(thread_id, value) for value in turn_ids}
    all_turns.update((entry.get("thread_id", thread_id), entry["turn_id"])
                     for entry in entries)
    history.executemany("INSERT INTO thread_turns(thread_id, turn_id, status) VALUES (?, ?, 'completed')",
                        sorted(all_turns))
    history.executemany(
        "INSERT INTO thread_items(thread_id, turn_id, item_id, item_json, item_type, rollout_ordinal) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(entry.get("thread_id", thread_id), entry["turn_id"], entry["item_id"],
          entry.get("item_json", json.dumps(entry["item"])), entry["item_type"],
          entry["ordinal"]) for entry in entries],
    )
    history.execute(
        "INSERT INTO thread_history_projection_state VALUES (?, ?, ?)",
        (thread_id, len(rollout_bytes) if projected_offset is None else projected_offset,
         projected_ordinal),
    )
    history.commit()
    history.close()
    history_path.chmod(0o600)
    return home, control / "app-server-control.sock", state_path, history_path, rollout


class QueueDeliveryTest(unittest.TestCase):
    def test_queue_delete_uses_exact_thread_submission_and_requires_boolean_result(self):
        with mock.patch.object(QUEUE, "_rpc", return_value={"deleted": True}) as rpc:
            self.assertTrue(QUEUE.delete_queue_item(SOCKET, THREAD, ITEM_ID, timeout=0.4))
        self.assertEqual(rpc.call_args.args[1], "thread/queue/delete")
        self.assertEqual(rpc.call_args.args[2], {
            "threadId": THREAD, "queuedSubmissionId": ITEM_ID,
        })
        with mock.patch.object(QUEUE, "_rpc", return_value={"deleted": 1}):
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "queue-delete-response-invalid"):
                QUEUE.delete_queue_item(SOCKET, THREAD, ITEM_ID)

    def test_native_completion_builder_keeps_v1_singleton_identity_and_exact_bytes(self):
        import dispatch_pending_delivery as pending
        import dispatch_recipient_receipt as recipient

        record = {
            "delivery_id": "delivery-builder-test", "recipient_kind": "codex-native-queue",
            "recipient_digest": pending.recipient_digest(THREAD), "attempt_ids": ["att-one"],
            "receipt": {"kind": "completion", "children": []}, "receipt_digest": "0" * 64,
            "row_revisions": {}, "session_generation": "", "session_generation_supported": "0",
        }
        record["receipt_digest"] = pending._canonical_receipt_digest(record["receipt"])
        client_id = QUEUE.native_client_message_id(THREAD, [record["delivery_id"]])
        body = QUEUE.native_completion_body(THREAD, [record], record["receipt"], "follow up")
        self.assertEqual(client_id, "delivery-" + __import__("hashlib").sha256(
            ("codex-native-queue-v1\0" + THREAD + "\0" + record["delivery_id"]).encode()
        ).hexdigest()[:32])
        self.assertEqual(body, "AGENT_HARNESS_COMPLETION_V1\n" +
                         __import__("json").dumps(record["receipt"], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False) + "\nfollow up\n" +
                         recipient.receipt_marker(record, THREAD))

    def test_producer_variants_match_pre_refactor_byte_formulas(self):
        import dispatch_pending_delivery as pending
        import dispatch_recipient_receipt as recipient
        import dispatch_session_sweep as sweep

        records = []
        for delivery_id, attempt_id in (("delivery-multi-b", "att-b"),
                                        ("delivery-multi-a", "att-a")):
            receipt = {"kind": "completion", "children": [{
                "attempt_id": attempt_id, "status": "completed",
                "required_action": "advance-completed",
            }]}
            records.append({
                "delivery_id": delivery_id, "recipient_kind": "codex-native-queue",
                "recipient_digest": pending.recipient_digest(THREAD), "attempt_ids": [attempt_id],
                "receipt": receipt,
                "receipt_digest": pending._canonical_receipt_digest(receipt),
                "row_revisions": {}, "session_generation": "",
                "session_generation_supported": "0", "attempts": 7,
                sweep.STORAGE_KEY: THREAD,
            })

        own_receipt = {"kind": "completion", "children": [
            {"attempt_id": "att-a", "status": "completed",
             "required_action": "advance-completed"},
            {"attempt_id": "att-b", "status": "completed",
             "required_action": "advance-completed"},
        ]}
        own_records = [dict(record, receipt=own_receipt,
                            receipt_digest=pending._canonical_receipt_digest(own_receipt))
                       for record in records]
        sorted_ids = sorted(row["delivery_id"] for row in own_records)
        expected_own_id = "delivery-" + hashlib.sha256(
            ("codex-native-queue-v1\0" + THREAD + "\0" + "\0".join(sorted_ids)).encode()
        ).hexdigest()[:32]
        expected_own_body = (
            "AGENT_HARNESS_COMPLETION_V1\n" +
            json.dumps(own_receipt, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\nfollow-up\n" +
            "\n".join(recipient.receipt_marker(row, THREAD) for row in own_records)
        )
        self.assertEqual(QUEUE.native_client_message_id(
            THREAD, [row["delivery_id"] for row in own_records]), expected_own_id)
        self.assertEqual(QUEUE.native_completion_body(
            THREAD, own_records, own_receipt, "follow-up"), expected_own_body)

        ordered = sorted(records, key=lambda row: row["delivery_id"])
        expected_batch_id = "delivery-" + hashlib.sha256(
            ("codex-native-queue-batch-v1\0" + THREAD + "\0" +
             "\0".join(row["delivery_id"] for row in ordered)).encode()
        ).hexdigest()[:32]
        batch_lines = []
        for row in ordered:
            child = row["receipt"]["children"][0]
            batch_lines.append(
                f"delivery_id={row['delivery_id']} route_id=- route_node=- attempts=0 "
                f"attempt_id={child['attempt_id']} status=completed "
                "required_action=advance-completed classification=-\n" +
                recipient.receipt_marker(row, THREAD)
            )
        expected_batch_body = (
            "Hearting runtime delivery. Follow required_action/next-action receipt; "
            "inspect-recovery: diagnostic. runtime-v1 finalizes. Tracked-Workflow Continuation. "
            "Keep new failures/conflicts, unfinished work, questions; no extra "
            "harness-scheduled calls/Monitor/dispatch-wait/polling.\n" +
            "\n".join("- " + line for line in batch_lines)
        )
        self.assertEqual(QUEUE.native_client_message_id(
            THREAD, [row["delivery_id"] for row in records], batch=True), expected_batch_id)
        self.assertEqual(QUEUE.native_batch_unit_body(Path("/tmp"), THREAD, records),
                         expected_batch_body)

        old_followup = (
            "The requested work is complete, including its declared stages and runtime workflow/route/cycle settlement. "
            "Continue and complete the original user task itself, preserving every explicit content and output-format "
            "requirement from that request; this receipt supplements the task and does not replace it with a receipt-only response. "
            "Read the exact result and perform the already approved parent review/follow-through in this turn, "
            "or run its existing registered continuation. On the first user-facing close, explain what changed and why, "
            "the observed before/after, checks actually run (including reused, skipped, and unrun checks), applicable "
            "commit/PR/deployment/serving state, and limits or remaining work. Do not invent deployment or operating "
            "results. Receipt, queue consumption and ACK are not execution. No harvest, route restart, or manual "
            "finalization is required. An automatic repeat of this exact result or a cleanup-only notice after the "
            "follow-through is handled does not call for the same action or a new user ACK; preserve a new failure, "
            "conflict, unfinished work, or direct user question."
        )
        current_followup = "Original request/format apply.\nContinue with the original request."
        prior_followup = old_followup + "\nContinue with the original request."
        singleton = own_records[:1]
        expected_prior_body = (
            "AGENT_HARNESS_COMPLETION_V1\n" +
            json.dumps(own_receipt, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n" + prior_followup + "\n" +
            recipient.receipt_marker(singleton[0], THREAD)
        )
        self.assertEqual(QUEUE.prior_followup_text(current_followup), prior_followup)
        self.assertEqual(QUEUE.native_completion_body(
            THREAD, singleton, own_receipt,
            QUEUE.prior_followup_text(current_followup)), expected_prior_body)

    def test_absolute_deadline_covers_connect_upgrade_and_initialize(self):
        now = [50.0]
        fake_socket = mock.Mock()

        def connect(_path):
            now[0] += 0.4

        def recv(_size):
            if not getattr(recv, "handshake", False):
                recv.handshake = True
                now[0] += 0.4
                key = __import__("base64").b64encode(b"x" * 16).decode()
                accept = __import__("base64").b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                return ("HTTP/1.1 101 Switching Protocols\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            now[0] += fake_socket.settimeout.call_args.args[0]
            raise TimeoutError("stalled initialize")

        fake_socket.connect.side_effect = connect
        fake_socket.recv.side_effect = recv
        with mock.patch.object(QUEUE.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(QUEUE, "resolve_endpoint", return_value=SOCKET), \
             mock.patch.object(QUEUE.Path, "lstat", return_value=SimpleNamespace(
                 st_mode=0, st_uid=QUEUE.os.geteuid())), \
             mock.patch.object(QUEUE.stat, "S_ISSOCK", return_value=True), \
             mock.patch.object(QUEUE.socket, "socket", return_value=fake_socket), \
             mock.patch.object(QUEUE.secrets, "token_bytes", return_value=b"x" * 16):
            started = now[0]
            with self.assertRaises(QUEUE.QueueDeliveryError) as caught:
                with QUEUE.DeliverySession(
                        SOCKET, timeout=1.0, deadline=started + 1.0) as session:
                    session.request(SOCKET, "thread/queue/list", {"threadId": THREAD})
        self.assertEqual(caught.exception.reason, "queue-operation-deadline-exceeded")
        self.assertTrue(caught.exception.ambiguous)
        self.assertLessEqual(now[0] - started, 1.0)

    def test_deadline_bounds_paginated_queue_listing_without_reading_later_pages(self):
        clock = iter((100.0, 100.6))
        with mock.patch.object(QUEUE.time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(QUEUE, "_rpc", return_value={
                 "data": [], "nextCursor": "page-2"}) as rpc:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError,
                                        "queue-operation-deadline-exceeded"):
                QUEUE.list_queue(SOCKET, THREAD, timeout=1.0, deadline=100.5)
        rpc.assert_called_once()
        self.assertEqual(rpc.call_args.kwargs["timeout"], 0.5)

    def test_read_exact_turn_confirms_native_turn_id_from_thread_history(self):
        items = [{"id": "item-user-exact", "type": "userMessage",
                  "content": [{"type": "text", "text": "original requested task"}]}]
        turn = {"id": "turn-native-exact", "status": "inProgress", "items": items}
        with mock.patch.object(QUEUE, "_rpc", side_effect=[{"data": [turn]}, {"data": [turn]}]) as rpc:
            self.assertEqual(QUEUE.read_exact_turn(SOCKET, THREAD, "turn-native-exact"),
                             {"id": "turn-native-exact", "status": "inProgress", "items": items})
            self.assertIsNone(QUEUE.read_exact_turn(SOCKET, THREAD, "message-id-alias"))
        self.assertEqual(rpc.call_args_list[0].args[1], "thread/turns/list")
        self.assertEqual(rpc.call_args_list[0].args[2]["threadId"], THREAD)
        self.assertEqual(rpc.call_args_list[0].args[2]["itemsView"], "full")

    def test_read_exact_turn_accepts_codex_0163_full_view_with_empty_items(self):
        # Shape copied from the recorded first-turn-before-crash.json replay
        # fixture for Codex 0.163; the native hook later observes interruption.
        turn = {
            "completedAt": None, "durationMs": None, "error": None,
            "id": "turn-empty-native", "items": [], "itemsView": "full",
            "rootTurnId": "turn-empty-native", "startedAt": 1792389273,
            "status": "inProgress",
        }
        with mock.patch.object(QUEUE, "_rpc", return_value={"data": [turn]}) as rpc:
            observed = QUEUE.read_exact_turn(SOCKET, THREAD, "turn-empty-native")
        self.assertEqual(observed, {
            "id": "turn-empty-native", "status": "inProgress", "items": []})
        self.assertEqual(rpc.call_args.args[2]["itemsView"], "full")

    def test_rpc_server_request_id_collision_is_not_mistaken_for_a_reply(self):
        client = QUEUE._WebSocket.__new__(QUEUE._WebSocket)
        client.timeout = 1.0
        client.socket = mock.Mock()
        client.next_id = 1
        client.request_sent = False
        client._send_frame = mock.Mock()
        client._recv_message = mock.Mock(side_effect=[
            (1, json.dumps({"jsonrpc": "2.0", "id": 1,
                            "method": "item/commandExecution/requestApproval",
                            "params": {"threadId": THREAD}}).encode()),
            (1, json.dumps({"jsonrpc": "2.0", "method": "turn/completed",
                            "params": {"threadId": THREAD}}).encode()),
            (1, json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()),
        ])

        result = QUEUE._WebSocket.request(client, "thread/queue/list", {"threadId": THREAD})

        self.assertEqual(result, {"ok": True})
        # Only the outgoing queue request was written; the server request was not answered.
        client._send_frame.assert_called_once()

    def test_delivery_session_reuses_and_closes_one_initialized_connection(self):
        client = mock.Mock()
        client.request.side_effect = [{"first": True}, {"second": True}]
        with mock.patch.object(QUEUE, "resolve_endpoint", return_value=SOCKET), \
             mock.patch.object(QUEUE, "_connect", return_value=client) as connect:
            with QUEUE.DeliverySession(SOCKET) as session:
                self.assertEqual(QUEUE._rpc(SOCKET, "thread/read", {}, timeout=1, session=session),
                                 {"first": True})
                self.assertEqual(QUEUE._rpc(SOCKET, "thread/queue/list", {}, timeout=1, session=session),
                                 {"second": True})
        connect.assert_called_once_with(SOCKET, 1)
        self.assertEqual(client.request.call_count, 2)
        client.close.assert_called_once()

    def test_delivery_session_rebind_requires_a_new_exact_state_pass(self):
        client = mock.Mock()
        client.request.return_value = {"ok": True}
        with mock.patch.object(QUEUE, "resolve_endpoint", side_effect=[SOCKET, Path("/tmp/other.sock")]), \
             mock.patch.object(QUEUE, "_connect", return_value=client):
            session = QUEUE.DeliverySession(SOCKET)
            self.assertEqual(session.request(SOCKET, "thread/read", {}, timeout=1), {"ok": True})
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "queue-session-endpoint-changed"):
                session.request(SOCKET, "thread/queue/list", {}, timeout=1)
        client.request.assert_called_once()
        client.close.assert_called_once()

    def test_notification_flood_cannot_extend_one_rpc_deadline(self):
        client = QUEUE._WebSocket.__new__(QUEUE._WebSocket)
        client.timeout = 0.01
        client.socket = mock.Mock()
        client.next_id = 1
        client.request_sent = False
        client._send_frame = mock.Mock()
        client._recv_message = mock.Mock(return_value=(1, json.dumps({
            "jsonrpc": "2.0", "method": "turn/completed", "params": {"threadId": THREAD},
        }).encode()))

        with self.assertRaises(QUEUE.QueueDeliveryError) as caught:
            QUEUE._WebSocket.request(client, "thread/read", {"threadId": THREAD})

        self.assertEqual(caught.exception.reason, "queue-websocket-timeout")
        self.assertTrue(caught.exception.ambiguous)

    def test_add_disconnect_after_send_is_ambiguous(self):
        client = QUEUE._WebSocket.__new__(QUEUE._WebSocket)
        client.timeout = 1.0
        client.socket = mock.Mock()
        client.socket.recv.return_value = b""
        client.buffer = bytearray()
        client.next_id = 1
        client.request_sent = False
        client._send_frame = mock.Mock()

        with self.assertRaises(QUEUE.QueueDeliveryError) as caught:
            QUEUE._WebSocket.request(client, "thread/queue/add", {"threadId": THREAD})

        self.assertTrue(caught.exception.ambiguous)

    def test_read_response_overflow_remains_definite_for_peer_history_paging(self):
        client = QUEUE._WebSocket.__new__(QUEUE._WebSocket)
        client.timeout = 1.0
        client.socket = mock.Mock()
        client.socket.recv.return_value = b"\x81\x7f" + struct.pack("!Q", QUEUE.MAX_FRAME_BYTES + 1)
        client.buffer = bytearray()
        client.next_id = 1
        client.request_sent = False
        client._send_frame = mock.Mock()

        with self.assertRaises(QUEUE.QueueDeliveryError) as caught:
            QUEUE._WebSocket.request(client, "thread/turns/list", {"threadId": THREAD})

        self.assertEqual(caught.exception.reason, "queue-websocket-message-oversized")
        self.assertFalse(caught.exception.ambiguous)

    def test_idle_queue_add_does_not_race_with_tui_wakeup(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertEqual(result["queued_submission_id"], ITEM_ID)
        self.assertFalse(result["started_after_interrupt"])
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])

    def setUp(self):
        self.history = mock.patch.object(QUEUE, "find_turn_by_client_message_id", return_value=None).start()
        self.addCleanup(mock.patch.stopall)

    def test_pending_exact_item_restarts_without_resuming_or_taking_subscription(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"turn": {"id": "turn-1"}}) as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertTrue(result["already_pending"])
        self.assertTrue(result["started_after_interrupt"])
        self.assertEqual([c.args[1] for c in rpc.call_args_list], ["thread/queue/start"])
        self.assertEqual(rpc.call_args.args[2]["queuedSubmissionId"], ITEM_ID)

    def test_start_refusal_is_preserved_without_hidden_resume(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=QUEUE.QueueDeliveryError("resume-required")) as rpc:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "resume-required"):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual([c.args[1] for c in rpc.call_args_list], ["thread/queue/start"])

    def test_consumed_item_is_not_resent(self):
        self.history.return_value = {"id": "turn-consumed"}
        with mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_consume_between_history_and_queue_list_is_not_resent(self):
        self.history.side_effect = [None, {"id": "turn-consumed"}]
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_missing_or_duplicate_pending_identity_suppresses_send_and_start(self):
        for items in ([{"clientUserMessageId": CLIENT_ID}], [ITEM, ITEM]):
            with self.subTest(items=items), mock.patch.object(QUEUE, "list_queue", return_value=items), \
                 mock.patch.object(QUEUE, "_rpc") as rpc:
                result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="fixture")
                self.assertTrue(result["already_pending"])
                self.assertIsNone(result["queued_submission_id"])
                rpc.assert_not_called()

    def test_unavailable_history_does_not_prevent_queueing_or_claim_consumption(self):
        self.history.side_effect = QUEUE.QueueDeliveryError("queue-websocket-message-oversized")
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}):
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "queued")

    def test_add_success_without_item_identity_is_accepted(self):
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", return_value={}) as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="fixture")
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(result["queued_submission_id"])
        rpc.assert_called_once()

    def test_unrelated_user_queue_entry_is_never_started(self):
        user_item = {"id": "human-item", "clientUserMessageId": "human-id"}
        with mock.patch.object(QUEUE, "list_queue", return_value=[user_item]), \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="interrupted"), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.poll_owned_item(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
            )
        self.assertEqual(result, {"pending": False, "started": False})
        rpc.assert_not_called()

    def test_ambiguous_add_lists_before_retry_and_skips_retry_when_pending(self):
        ambiguous = QUEUE.QueueDeliveryError("socket-dropped-after-send", ambiguous=True)
        with mock.patch.object(QUEUE, "list_queue", side_effect=[[], [ITEM], [ITEM]]) as listing, \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=[ambiguous]) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertTrue(result["already_pending"])
        self.assertEqual(listing.call_count, 3)
        rpc.assert_called_once()

    def test_ambiguous_missing_item_is_retried_once(self):
        ambiguous = QUEUE.QueueDeliveryError("socket-dropped-after-send", ambiguous=True)
        with mock.patch.object(QUEUE, "list_queue", side_effect=[[], []]) as listing, \
             mock.patch.object(QUEUE, "_latest_turn_status", return_value="completed"), \
             mock.patch.object(QUEUE, "_rpc", side_effect=[ambiguous, {"queuedSubmission": ITEM}]) as rpc:
            result = QUEUE.send_at_least_once(
                SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                message="fixture",
            )
        self.assertEqual(result["queued_submission_id"], ITEM_ID)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual([call.args[1] for call in rpc.call_args_list],
                         ["thread/queue/add", "thread/queue/add"])


class QueueHistoryBoundTest(unittest.TestCase):
    def test_long_history_does_not_block_a_new_receipt(self):
        with mock.patch.object(QUEUE, "MAX_QUEUE_PAGES", 2), \
             mock.patch.object(QUEUE, "_rpc", return_value={"data": [], "nextCursor": "older"}) as rpc:
            self.assertIsNone(QUEUE.find_turn_by_client_message_id(SOCKET, THREAD, CLIENT_ID))
        self.assertEqual(rpc.call_count, 2)
        self.assertTrue(all(c.args[2]["itemsView"] == "full" for c in rpc.call_args_list))


class PeerQueuePolicy(unittest.TestCase):
    def test_peer_ambiguous_add_is_not_retried(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc", side_effect=QUEUE.QueueDeliveryError("ambiguous", ambiguous=True)) as rpc:
            with self.assertRaises(QUEUE.QueueDeliveryError):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD, client_message_id=CLIENT_ID,
                    message="peer", allow_restart=False, retry_ambiguous=False)
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])

    def test_peer_never_forces_interrupted_turn_restart(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[ITEM]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                client_message_id=CLIENT_ID, message="peer", allow_restart=False)
        self.assertFalse(result["started_after_interrupt"])
        rpc.assert_not_called()

    def test_observation_only_cannot_add_after_lost_ack(self):
        with mock.patch.object(QUEUE, "_known_consumed", return_value=None), \
             mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            with self.assertRaisesRegex(QUEUE.QueueDeliveryError, "previously-unconfirmed"):
                QUEUE.send_at_least_once(SOCKET, thread_id=THREAD,
                    client_message_id=CLIENT_ID, message="peer", allow_restart=False, allow_add=False)
        rpc.assert_not_called()


class NativePeerObservationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def send(self, endpoint, *, message="peer"):
        return QUEUE.send_at_least_once(
            endpoint, thread_id=THREAD, client_message_id=CLIENT_ID,
            message=message, timeout=3, allow_restart=False,
            retry_ambiguous=False, require_message_match=True,
        )

    @staticmethod
    def entry(turn_id, item_id, ordinal, client_id, *, message="peer",
              json_type="userMessage", item_type="userMessage", thread_id=None):
        item = {"type": json_type, "clientId": client_id,
                "content": [{"type": "text", "text": message, "text_elements": []}]}
        result = {"turn_id": turn_id, "item_id": item_id, "ordinal": ordinal,
                  "item_type": item_type, "item": item}
        if thread_id is not None:
            result["thread_id"] = thread_id
        return result

    def test_1601_turn_oldest_second_user_message_is_consumed_without_rpc(self):
        turn_ids = [f"turn-{index}" for index in range(1601)]
        entries = [self.entry("turn-0", "old-first", 1, "other-client"),
                   self.entry("turn-0", "old-second", 2, CLIENT_ID)]
        entries.extend(self.entry(f"turn-{index}", f"item-{index}", index + 2,
                                  f"other-{index}") for index in range(1, 1601))
        _home, endpoint, _state, _history, _rollout = native_fixture(
            self.root, entries=entries, turn_ids=turn_ids, projected_ordinal=2000)
        with mock.patch.object(QUEUE, "_rpc") as rpc:
            result = self.send(endpoint)
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_owned_group_writable_rollout_is_exactly_observable(self):
        entry = self.entry("turn-1", "item-1", 1, CLIENT_ID)
        _home, endpoint, _state, _history, rollout = native_fixture(
            self.root, entries=[entry], projected_ordinal=10, rollout_mode=0o664)
        self.assertEqual(stat.S_IMODE(rollout.stat().st_mode), 0o664)

        observation = QUEUE._observe_peer_message(
            endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(observation.status, "matched")
        with mock.patch.object(QUEUE, "_rpc") as rpc:
            result = self.send(endpoint)
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_matched_consumption_carries_the_confirmed_turn_status(self):
        # A consumed completion is retained with its receiving turn's status.
        # Without it, proven consumption stayed sent-ambiguous until expiry.
        entry = self.entry("turn-1", "item-1", 1, CLIENT_ID)
        _home, endpoint, _state, _history, _rollout = native_fixture(
            self.root, entries=[entry], projected_ordinal=10)

        observation = QUEUE._observe_peer_message(
            endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(observation.status, "matched")
        self.assertEqual((observation.turn["id"], observation.turn["status"]),
                         ("turn-1", "completed"))
        with mock.patch.object(QUEUE, "_rpc") as rpc:
            result = self.send(endpoint)
        self.assertEqual((result["status"], result["turn_id"], result["turn_status"]),
                         ("consumed", "turn-1", "completed"))
        rpc.assert_not_called()

    def test_rollout_and_native_control_safety_checks_remain_strict(self):
        cases = []

        home, endpoint, _state, _history, rollout = native_fixture(
            self.root / "world-write-rollout")
        rollout.chmod(0o666)
        cases.append(("world-write-rollout", endpoint, "native-rollout-unbound"))

        home, endpoint, _state, _history, rollout = native_fixture(
            self.root / "symlink-rollout")
        outside = self.root / "symlink-target.jsonl"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        rollout.unlink()
        rollout.symlink_to(outside)
        cases.append(("symlink-rollout", endpoint, "native-rollout-unbound"))

        _home, endpoint, state_path, _history, _rollout = native_fixture(
            self.root / "outside-rollout")
        outside = self.root / "outside-home.jsonl"
        outside.write_bytes(b"")
        outside.chmod(0o600)
        connection = sqlite3.connect(state_path)
        try:
            connection.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                               (str(outside), THREAD))
            connection.commit()
        finally:
            connection.close()
        cases.append(("outside-rollout", endpoint, "native-rollout-unbound"))

        _home, endpoint, state_path, _history, _rollout = native_fixture(
            self.root / "group-write-database")
        state_path.chmod(0o664)
        cases.append(("group-write-database", endpoint, "native-history-unavailable"))

        home, endpoint, _state, _history, _rollout = native_fixture(
            self.root / "group-write-home")
        home.chmod(0o770)
        cases.append(("group-write-home", endpoint, "native-home-unsafe"))

        home, endpoint, _state, _history, _rollout = native_fixture(
            self.root / "group-write-control")
        (home / "app-server-control").chmod(0o770)
        cases.append(("group-write-control", endpoint, "native-home-unsafe"))

        for name, endpoint, reason in cases:
            with self.subTest(source=name):
                observation = QUEUE._observe_peer_message(
                    endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
                self.assertEqual(observation.status, "unknown")
                self.assertEqual(observation.reason, reason)

    def test_1601_turn_never_attempted_pending_with_large_tool_items_queues_once(self):
        turn_ids = [f"turn-{index}" for index in range(1601)]
        entries = []
        ordinal = 0
        for index, turn_id in enumerate(turn_ids):
            ordinal += 1
            entries.append(self.entry(turn_id, f"user-{index}", ordinal, f"other-{index}"))
            if index % 100 == 0:
                ordinal += 1
                entries.append({"turn_id": turn_id, "item_id": f"tool-{index}",
                                "ordinal": ordinal, "item_type": "commandExecution",
                                "item": {"type": "commandExecution",
                                         "aggregatedOutput": "x" * (256 * 1024)}})
        tool_bytes = sum(len(json.dumps(entry["item"]).encode()) for entry in entries
                         if entry["item_type"] == "commandExecution")
        self.assertGreater(tool_bytes, 4 * 1024 * 1024)
        _home, endpoint, _state, history_path, _rollout = native_fixture(
            self.root, entries=entries, turn_ids=turn_ids,
            projected_ordinal=ordinal + 1)
        queue_item = dict(ITEM, clientUserMessageId=CLIENT_ID,
                          input=[{"type": "text", "text": "peer"}])
        with mock.patch.object(QUEUE, "list_queue", side_effect=[[], [queue_item]]) as listing, \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}) as rpc:
            first = self.send(endpoint)
            replay = self.send(endpoint)
        self.assertEqual(first["status"], "queued")
        self.assertFalse(first["already_pending"])
        self.assertEqual(replay["status"], "queued")
        self.assertTrue(replay["already_pending"])
        self.assertEqual(listing.call_count, 2)
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])
        connection = sqlite3.connect(history_path)
        try:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT ti.item_json FROM thread_items AS ti "
                "WHERE ti.thread_id=? AND ti.item_type='userMessage' "
                "AND json_extract(ti.item_json, '$.clientId')=? ORDER BY ti.rollout_ordinal LIMIT 2",
                (THREAD, CLIENT_ID),
            ).fetchall()
        finally:
            connection.close()
        self.assertTrue(any("idx_thread_items_user_messages" in row[3] for row in plan), plan)

    def test_exact_body_type_and_same_or_cross_turn_duplicates_are_conflicts(self):
        cases = (
            ([self.entry("turn-1", "item-1", 1, CLIENT_ID, message="different")],
             "candidate-content-conflict"),
            ([self.entry("turn-1", "item-1", 1, CLIENT_ID, json_type="agentMessage")],
             "candidate-binding-conflict"),
            ([self.entry("turn-1", "item-1", 1, CLIENT_ID),
              self.entry("turn-1", "item-2", 2, CLIENT_ID)], "duplicate-client-id"),
            ([self.entry("turn-1", "item-1", 1, CLIENT_ID),
              self.entry("turn-2", "item-2", 2, CLIENT_ID)], "duplicate-client-id"),
        )
        for index, (entries, reason) in enumerate(cases):
            with self.subTest(reason=reason, index=index):
                _home, endpoint, *_ = native_fixture(
                    self.root / str(index), entries=entries,
                    turn_ids=[entry["turn_id"] for entry in entries], projected_ordinal=10)
                observation = QUEUE._observe_peer_message(
                    endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
                self.assertEqual(observation.status, "conflict")
                self.assertEqual(observation.reason, reason)
                with mock.patch.object(QUEUE, "_rpc") as rpc:
                    with self.assertRaisesRegex(QUEUE.QueueDeliveryError,
                                                "queue-peer-history-conflict"):
                        self.send(endpoint)
                    rpc.assert_not_called()

    def test_sqlite_json5_candidate_that_python_cannot_decode_is_unknown(self):
        json5 = ("{type: 'userMessage', clientId: '" + CLIENT_ID
                 + "', content: [{type: 'text', text: 'peer', text_elements: []}]}")
        entry = self.entry("turn-1", "item-1", 1, CLIENT_ID)
        entry["item_json"] = json5
        _home, endpoint, _state, history_path, _rollout = native_fixture(
            self.root, entries=[entry], projected_ordinal=10)
        connection = sqlite3.connect(history_path)
        try:
            self.assertEqual(connection.execute(
                "SELECT json_extract(?, '$.clientId')", (json5,)
            ).fetchone()[0], CLIENT_ID)
        finally:
            connection.close()
        with self.assertRaises(json.JSONDecodeError):
            json.loads(json5)

        observation = QUEUE._observe_peer_message(
            endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(observation.status, "unknown")
        self.assertEqual(observation.reason, "candidate-json-invalid")

    def test_foreign_thread_id_is_not_target_consumption(self):
        entry = self.entry("foreign-turn", "foreign-item", 1, CLIENT_ID,
                           thread_id="foreign-thread")
        _home, endpoint, *_ = native_fixture(self.root, entries=[entry], projected_ordinal=10)
        observation = QUEUE._observe_peer_message(
            endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(observation.status, "not_observed")
        with mock.patch.object(QUEUE, "list_queue", return_value=[]), \
             mock.patch.object(QUEUE, "_rpc", return_value={"queuedSubmission": ITEM}) as rpc:
            result = self.send(endpoint)
        self.assertEqual(result["status"], "queued")
        self.assertEqual([call.args[1] for call in rpc.call_args_list], ["thread/queue/add"])

    def test_consumption_between_queue_miss_and_second_observation_wins(self):
        _home, endpoint, _state, history_path, rollout = native_fixture(
            self.root, turn_ids=["turn-1"], projected_ordinal=10)

        def consume_after_queue_miss(*_args, **_kwargs):
            rollout.write_bytes(b"consumed")
            connection = sqlite3.connect(history_path)
            try:
                item = self.entry("turn-1", "consumed-item", 2, CLIENT_ID)["item"]
                connection.execute(
                    "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?)",
                    (THREAD, "turn-1", "consumed-item", json.dumps(item), "userMessage", 2),
                )
                connection.execute(
                    "UPDATE thread_history_projection_state SET next_rollout_byte_offset=8 "
                    "WHERE thread_id=?", (THREAD,),
                )
                connection.commit()
            finally:
                connection.close()
            return []

        with mock.patch.object(QUEUE, "list_queue", side_effect=consume_after_queue_miss), \
             mock.patch.object(QUEUE, "_rpc") as rpc:
            result = self.send(endpoint)
        self.assertEqual(result["status"], "consumed")
        rpc.assert_not_called()

    def test_missing_unbound_corrupt_incomplete_and_timed_out_sources_are_unknown(self):
        _home, endpoint, _state, _history, _rollout = native_fixture(
            self.root / "incomplete", rollout_bytes=b"x", projected_offset=0)
        cases = (
            QUEUE._observe_peer_message(Path("/tmp/managed/app-server.sock"), THREAD,
                                        CLIENT_ID, timeout=1),
            QUEUE._observe_peer_message(endpoint, THREAD, CLIENT_ID, timeout=0),
            QUEUE._observe_peer_message(endpoint, THREAD, CLIENT_ID, timeout=1),
            QUEUE._observe_peer_message(self.root / "missing" / "app-server-control" /
                                        "app-server-control.sock", THREAD, CLIENT_ID, timeout=1),
        )
        self.assertTrue(all(result.status == "unknown" for result in cases), cases)

        bad_entry = dict(self.entry("bad-turn", "bad-item", 1, CLIENT_ID),
                         item_json="{broken-json")
        _home, corrupt_endpoint, *_ = native_fixture(
            self.root / "corrupt", entries=[bad_entry], projected_ordinal=10)
        corrupt = QUEUE._observe_peer_message(
            corrupt_endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(corrupt.status, "unknown")

        _home, schema_endpoint, _state, schema_path, _rollout = native_fixture(
            self.root / "schema", projected_ordinal=10)
        connection = sqlite3.connect(schema_path)
        try:
            connection.execute("DROP TABLE thread_turns")
            connection.commit()
        finally:
            connection.close()
        schema_mismatch = QUEUE._observe_peer_message(
            schema_endpoint, THREAD, CLIENT_ID, timeout=1, expected_message="peer")
        self.assertEqual(schema_mismatch.status, "unknown")

    def test_native_database_lock_is_unknown_and_bounded(self):
        _home, endpoint, state_path, *_ = native_fixture(self.root)
        writer = sqlite3.connect(state_path, timeout=1)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE threads SET history_mode=history_mode WHERE id=?", (THREAD,))
            started = QUEUE.time.monotonic()
            observation = QUEUE._observe_peer_message(endpoint, THREAD, CLIENT_ID, timeout=0.2)
            self.assertLess(QUEUE.time.monotonic() - started, 0.5)
            self.assertEqual(observation.status, "unknown")
        finally:
            writer.rollback()
            writer.close()


if __name__ == "__main__":
    unittest.main()
