#!/usr/bin/env python3
"""A peer message a session received stays in its MESSAGES list, whatever carried it in.

The receive path every harness goes through keeps one display line per message in a small
file of its own, so a message that arrived as no prompt of its own (a hook comment added to a
prompt, a tool boundary, a native queue) is listed, and runtime notices never push it out.
Isolated state only; no real session, ledger or model.
"""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools/memory/tests"), str(ROOT), str(ROOT / "tools")]
from tidy_isolation import isolated_env
import session_tidy as st
import session_tidy_clear as clear
from fleet.collectors import prompt_inputs
from fleet.session_line import peer_message_rows


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pm = load("received_peers_peer_message", "utilities/peer-message.py")
shell = load("received_peers_session_shell", "utilities/session_shell.py")

TAGS = {("codex", "sender-codex"): "c1", ("claude", "sender-claude"): "c2",
        ("opencode", "sender-opencode"): "c3", ("claude", "me"): "aa", ("codex", "me"): "aa",
        ("opencode", "me"): "aa"}


def tag_of(harness, sid):
    return TAGS.get((harness, sid))


class ReceivedPeers(unittest.TestCase):
    def setUp(self):
        self.iso = isolated_env()
        self.addCleanup(self.iso.cleanup)
        self.env = self.iso.patched_environ({
            "HEARTING_TIDY_TEST_ROOT": str(self.iso.root),
            "AGENT_PEER_LEDGER_ROOT": str(self.iso.root / "peer-state"),
            "AGENT_DISPATCH_JOBS": str(self.iso.root / "jobs.log")})
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        for patch in (mock.patch.object(clear, "_start_helper"),
                      mock.patch("human_wait_state._request_producer")):
            patch.start()
            self.addCleanup(patch.stop)
        prompt_inputs._CACHE.clear()
        shell._SEALED_TRANSFERS.clear()
        self.cwd = self.iso.root / "project"
        self.cwd.mkdir()

    # -- helpers -----------------------------------------------------------------------

    def sender(self, harness="codex"):
        return {"harness": harness, "session_id": "sender-" + harness, "name": "worker"}

    def deliver_later(self, body, harness="claude", sender=None):
        recipient = {"harness": harness, "session_id": "me"}
        text, ref = pm.prepare_peer_message(body, sender or self.sender(), recipient, defer=True)
        return recipient, text, ref

    def seat(self, harness):
        return st.session_seat(st.Seat("native", "", harness=harness, sid="me"), harness, "me")

    def display(self, harness):
        return st.prompt_display(self.seat(harness), harness, "me")

    def lines(self, harness, received=None):
        """What the panel's MESSAGES list reads: the received lines, then the worded rows."""
        received = self.display(harness)["received"] if received is None else received
        kept, _sent = shell._without_request_transfers(received, [])
        return [(row["who"], row["text"]) for row in peer_message_rows(
            kept, [], tag_of, limit=8, own=(harness, "me"))]

    def prompt(self, text, harness, now):
        seat = self.seat(harness)
        with mock.patch.object(st, "resolve_seat", return_value=seat), \
                mock.patch.object(st, "_current_request_origin", return_value=None):
            st.run_hook(harness, "prompt", "me", cwd=str(self.cwd), prompt_text=text, now=now)

    def notifications(self, harness, count, start=2000):
        for index in range(count):
            self.prompt(f"<task-notification><summary>route {index} done</summary></task-notification>",
                        harness, start + index)

    # -- the three ways a message reaches a session --------------------------------------

    def test_claude_hook_comment_added_to_a_person_prompt_is_listed(self):
        self.prompt("내가 쓴 요청", "claude", 1000)
        recipient, _text, ref = self.deliver_later("[notice] 덧붙어 온 글\n둘째 줄", "claude")
        out = io.StringIO()
        pm.emit_peer_context(recipient, "UserPromptSubmit", stream=out, native_delivery=True)
        self.assertIn("덧붙어 온 글", out.getvalue())
        self.assertEqual(pm._read_pending(ref)["state"], "received")
        self.assertEqual(self.lines("claude"), [("[c1]", "[notice] 덧붙어 온 글")])

    def test_claude_tool_boundary_comment_is_listed(self):
        self.prompt("내가 쓴 요청", "claude", 1000)
        recipient, _text, _ref = self.deliver_later("도구 경계로 온 글", "claude")
        pm.emit_peer_context(recipient, "PostToolUse", stream=io.StringIO(), native_delivery=True)
        self.assertEqual(self.lines("claude"), [("[c1]", "도구 경계로 온 글")])

    def test_a_message_that_is_the_prompt_itself_is_listed_once_on_every_harness(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                recipient, text, ref = self.deliver_later("제 프롬프트로 온 글", harness, self.sender(harness))
                self.prompt(text, harness, 1000)
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)  # a repeat adds nothing
                self.assertEqual(self.lines(harness), [("[%s]" % tag_of(harness, "sender-" + harness),
                                                        "제 프롬프트로 온 글")])
                self.assertEqual([row["ref"] for row in self.display(harness)["received"]], [ref])

    def test_a_message_with_no_prompt_at_all_is_listed_on_every_harness(self):
        for harness in st.HARNESSES:
            with self.subTest(harness=harness):
                recipient, text, _ref = self.deliver_later("프롬프트 없이 온 글", harness, self.sender(harness))
                self.assertEqual(pm.receive_peer_message(text, recipient), 0)
                self.assertEqual([row for _who, row in self.lines(harness)], ["프롬프트 없이 온 글"])

    def test_codex_native_queue_and_opencode_native_history_receipts_are_listed(self):
        import codex_queue_delivery as queue
        recipient, _text, ref = self.deliver_later("코덱스 큐로 온 글", "codex", self.sender("claude"))
        proof = {"receipt_type": "fixture"}
        with mock.patch.object(queue, "send_at_least_once", return_value={"status": "consumed"}), \
                mock.patch.object(pm, "_validated_native_history_receipt", return_value=proof):
            self.assertEqual(pm.deliver_pending_codex(ref)["status"], "received")
        self.assertEqual(self.lines("codex"), [("[c2]", "코덱스 큐로 온 글")])
        recipient, _text, ref = self.deliver_later("오픈코드 기록으로 온 글", "opencode", self.sender("claude"))
        with pm.pending_lock(ref):
            row = pm._read_pending(ref)
            pm._save_pending(dict(row, state="queued", actual_message_id="m1", receipt="native-queue-accepted"))
        args = pm.argparse.Namespace(to_harness="opencode", to_session_id="me", received=ref, claim=None,
                                     queued=None, resuming=None, message_id="m1")
        with mock.patch.object(pm, "_validated_native_history_receipt", return_value=proof):
            self.assertEqual(pm.cmd_pending(args), 0)
        self.assertEqual(self.lines("opencode"), [("[c2]", "오픈코드 기록으로 온 글")])

    # -- notices do not push peer messages out -------------------------------------------

    def test_twenty_runtime_notices_do_not_push_out_a_peer_message(self):
        # (a) a message the receive path kept; (b) one that only came as a prompt (older records)
        self.prompt("내가 쓴 요청", "claude", 1000)
        recipient, _text, _ref = self.deliver_later("살아남아야 하는 글", "claude")
        pm.emit_peer_context(recipient, "PostToolUse", stream=io.StringIO(), native_delivery=True)
        self.notifications("claude", 20)
        display = self.display("claude")
        self.assertEqual(self.lines("claude"), [("[c1]", "살아남아야 하는 글")])
        self.assertEqual(sum(row["source"] == "notification" for row in display["received"]),
                         st.RECEIVED_INPUTS_LIMIT)

    def test_a_peer_prompt_kept_in_the_prompt_frontier_survives_notices_too(self):
        self.prompt("내가 쓴 요청", "codex", 1000)
        origin = {"kind": "peer", "name": "worker", "sender_harness": "codex", "sender_session_id": "sender-codex"}
        seat = self.seat("codex")
        with mock.patch.object(st, "resolve_seat", return_value=seat), \
                mock.patch.object(st, "_current_request_origin", return_value=origin):
            st.run_hook("codex", "prompt", "me", cwd=str(self.cwd),
                        prompt_text="<agent-message>프롬프트 틀에 남은 글</agent-message>", now=1500)
        self.notifications("codex", 20)
        self.assertEqual([text for _who, text in self.lines("codex")], ["프롬프트 틀에 남은 글"])

    def test_the_kept_peer_lines_are_the_newest_eight_in_time_order(self):
        for index in range(11):
            recipient, text, _ref = self.deliver_later(f"글 {index}", "claude")
            pm.receive_peer_message(text, recipient)
        texts = [text for _who, text in self.lines("claude")]
        self.assertEqual(texts, [f"글 {index}" for index in range(3, 11)])

    # -- what stays out ------------------------------------------------------------------

    def test_a_request_and_a_notice_to_its_own_seat_are_left_out(self):
        recipient, text, ref = self.deliver_later("요청 본문", "claude")
        pm.receive_peer_message(text, recipient)
        # The request seal is what keeps a request's own messages in PEER REQUEST (as before).
        path = pm._transfer_path(ref)
        record = json.loads(path.read_text())
        record["request"] = {"schema": "peer-request-ref-v1", "id": "rq-0a1b2c", "revision": 1, "role": "request"}
        path.write_text(json.dumps(record))
        ordinary, plain, _ref = self.deliver_later("보통 글", "claude")
        pm.receive_peer_message(plain, ordinary)
        own = {"harness": "claude", "session_id": "me", "name": "me"}
        tidy_text, _ref = pm.prepare_peer_message("정리 알림", own, recipient, defer=True)
        pm.receive_peer_message(tidy_text, recipient)
        self.assertEqual([row["text"] for row in self.display("claude")["received"]],
                         ["요청 본문", "보통 글", "정리 알림"])
        self.assertEqual(self.lines("claude"), [("[c1]", "보통 글")])

    def test_a_runtime_completion_is_not_kept_as_a_peer_message(self):
        origin = {"kind": "runtime-completion", "name": "Hearting runtime", "transfer_ref": "a" * 32}
        self.assertFalse(st.record_received_peer("claude", "me", "[notice] route done", origin))
        origin = {"kind": "peer", "sender_harness": "runtime", "sender_session_id": "x", "transfer_ref": "a" * 32}
        self.assertFalse(st.record_received_peer("claude", "me", "[notice] route done", origin))
        self.assertEqual(self.display("claude")["received"], [])

    def test_only_the_one_display_line_is_kept_never_the_body(self):
        body = "첫 줄\n" + "비밀 본문 " * 400
        recipient, text, _ref = self.deliver_later(body, "claude")
        pm.receive_peer_message(text, recipient)
        side = st.received_peer_sidecar_path("claude", "me")
        raw = side.read_text()
        self.assertIn("첫 줄", raw)
        self.assertNotIn("비밀 본문", raw)
        self.assertLess(len(raw), 2048)

    # -- the panel and Fleet read the same lines -----------------------------------------

    def test_fleet_reads_the_same_lines_and_notices_a_new_one(self):
        self.prompt("내가 쓴 요청", "claude", 1000)
        first = prompt_inputs.read("claude", "me")
        self.assertEqual(first["received"], [])
        recipient, _text, _ref = self.deliver_later("플릿에도 보이는 글", "claude")
        pm.emit_peer_context(recipient, "PostToolUse", stream=io.StringIO(), native_delivery=True)
        fleet = prompt_inputs.read("claude", "me")      # the cached read sees the new file
        self.assertEqual(fleet, self.display("claude"))
        self.assertEqual(self.lines("claude", fleet["received"]), self.lines("claude"))
        self.assertEqual(self.lines("claude"), [("[c1]", "플릿에도 보이는 글")])

    def test_one_draw_reads_the_frontier_and_the_small_file_and_no_ledger(self):
        recipient, text, _ref = self.deliver_later("읽기 수 확인", "claude")
        pm.receive_peer_message(text, recipient)
        self.notifications("claude", 20)
        opened = []
        real_open = os.open

        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=watch):
            self.display("claude")
        self.assertEqual(sorted(Path(path).parent.name for path in opened), ["peer-received", "prompt-seq"])


if __name__ == "__main__":
    unittest.main()
