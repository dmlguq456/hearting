#!/usr/bin/env python3
"""Anonymized current Codex rollout shapes; isolated parser/frontier regression."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MEM = ROOT / "tools/memory/mem.py"
BODY = "ANONNATIVEALPHA user correction: the approved deployment region is ap-northeast-2."
NEXT = "ANONNATIVEBETA next-session correction remains pending."


def response(role, identifier, text, turn="turn-1", kinds=None):
    parts = [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]
    metadata = {"turn_id": turn, "create_time": 1788789000.0}
    if kinds is not None:
        metadata["content_item_kinds"] = kinds
    return {"type": "response_item", "timestamp": "2026-09-07T14:00:00Z", "payload": {
        "type": "message", "id": identifier, "role": role, "content": parts,
        "internal_chat_message_metadata_passthrough": metadata}}


def item(role, identifier, text, turn="turn-1"):
    return {"type": "event_msg", "timestamp": "2026-09-07T14:00:00Z", "payload": {
        "type": "item_completed", "thread_id": "fixture-session", "turn_id": turn,
        "item": {"type": "UserMessage" if role == "user" else "AgentMessage", "id": identifier,
                 "content": [{"type": "text" if role == "user" else "Text", "text": text}]},
        "started_at_ms": 1788789000000, "completed_at_ms": 1788789000000}}


def current_turn(text=BODY, suffix="1"):
    turn = "turn-" + suffix
    return [response("user", "msg-user-" + suffix, text, turn, ["user.text"]),
            item("user", "item-user-" + suffix, text, turn),
            item("assistant", "msg-agent-" + suffix, "ACK", turn),
            response("assistant", "msg-agent-" + suffix, "ACK", turn)]


class CodexTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-transcript-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sid = "fixture-session"
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = self.base / "store"
        self.path = self.sessions / ("rollout-" + self.sid + ".jsonl")
        self.env = {
            "PATH": os.defpath, "HOME": str(self.base / "home"),
            "XDG_CONFIG_HOME": str(self.base / "config"), "XDG_DATA_HOME": str(self.base / "data"),
            "XDG_STATE_HOME": str(self.base / "state"), "XDG_CACHE_HOME": str(self.base / "cache"),
            "AGENT_HOME": str(self.base / "agent"), "MEM_STORE": str(self.store),
            "MEM_PROJECTS": str(self.base / "claude-projects"), "CODEX_SESSIONS": str(self.sessions),
            "CODEX_HOME": str(self.base / "codex"), "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "MEM_WRITE_EVENTS": str(self.base / "write-events.jsonl"),
            "MEM_RECALL_EVENTS": str(self.base / "recall-events.jsonl"),
            "MEM_RECALL_RECEIPTS": str(self.base / "recall-receipts"),
            "MEM_SYNC_REMOTE": "0", "MEM_DUMP_PUSH": "0", "PYTHONDONTWRITEBYTECODE": "1",
        }
        Path(self.env["HOME"]).mkdir()
        self.mem("index")

    def run_python(self, argv):
        result = subprocess.run([sys.executable, *argv], env=self.env, cwd=self.project,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def mem(self, *args):
        return self.run_python([str(MEM), *args])

    def append(self, *rows):
        with self.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def messages(self):
        script = ("import json,sys; sys.path.insert(0,sys.argv[1]); import mem; "
                  "print(json.dumps([row._asdict() for row in mem.CodexJsonlSource(sys.argv[2]).messages()]))")
        return json.loads(self.run_python(["-c", script, str(MEM.parent), self.sid]))

    def capture(self):
        return json.loads(self.mem("distill", self.sid, "--source", "codex", "--capture"))

    def close(self, capture):
        self.mem("distill", self.sid, "--source", "codex", "--advance-capture", capture["frontier"])

    def marker(self):
        return (self.store / (".distill-state-" + self.sid)).read_text().strip()

    def test_actual_native_shape_preserves_user_and_deduplicates_mirrors(self):
        # Structural copy of a real CLI rollout, with all IDs/body/metadata
        # replaced. The native user message lacks legacy user_message events.
        self.append({"type":"session_meta", "payload":{"id":self.sid}},
                    {"type":"event_msg", "payload":{"type":"task_started", "turn_id":"turn-1"}},
                    response("developer", "msg-developer", "INJECTEDDEVELOPER"),
                    response("user", "msg-context", "INJECTEDCONTEXT", kinds=["agents_md.instructions"]),
                    response("user", "msg-user-1", BODY, kinds=["user.text"]),
                    item("user", "item-user-1", BODY),
                    {"type":"event_msg", "payload":{"type":"item_completed", "item":{
                        "type":"Reasoning", "id":"reasoning-1", "raw_content":["REASONINGSECRET"]}}},
                    item("assistant", "msg-agent-1", "ACK"),
                    response("assistant", "msg-agent-1", "ACK"),
                    {"type":"event_msg", "payload":{"type":"task_complete", "last_agent_message":"ACK"}})
        before = self.path.read_bytes()
        captured = self.capture()
        self.assertEqual(captured["delta"], "[user] " + BODY + "\n\n[assistant] ACK\n")
        self.assertEqual(self.path.read_bytes(), before)
        rows = self.messages()
        self.assertEqual([row["uuid"] for row in rows], ["msg-user-1", "item-user-1", "msg-agent-1"])
        self.assertEqual(rows[1]["text"], "")  # Alias preserves marker identity only.
        self.assertEqual(self.messages(), rows)
        self.close(captured)
        self.assertEqual(self.marker(), "msg-agent-1")
        self.assertEqual(self.mem("distill", self.sid, "--source", "codex"), "")

    def test_item_only_and_response_only_text_are_both_supported(self):
        self.append(item("user", "item-u", "first\nsecond"), item("assistant", "item-a", "reply"),
                    response("user", "msg-u", "metadata-tagged fallback", "turn-2", ["user.text"]),
                    response("assistant", "msg-a", "response-only reply", "turn-2"))
        self.assertEqual(self.capture()["delta"], "[user] first\nsecond\n\n[assistant] reply\n\n"
                         "[user] metadata-tagged fallback\n\n[assistant] response-only reply\n")

    def test_legacy_event_uuid_remains_a_valid_incremental_boundary(self):
        self.append(response("user", "response-u", BODY, kinds=["user.text"]),
                    {"type":"event_msg", "payload":{"type":"user_message", "id":"legacy-u", "message":BODY}},
                    item("user", "item-u", BODY))
        captured = self.capture()
        self.assertEqual(captured["delta"], "[user] " + BODY + "\n")
        rows = self.messages()
        self.assertIn("legacy-u", [row["uuid"] for row in rows])
        # Close the supported capture at the legacy alias itself, as an older
        # installed parser would have done, then check the appended delta.
        legacy_capture = json.loads(base64.urlsafe_b64decode(captured["frontier"]))
        legacy_capture["last"] = "legacy-u"
        captured["frontier"] = base64.urlsafe_b64encode(json.dumps(legacy_capture).encode()).decode()
        self.close(captured)
        self.append(response("assistant", "msg-answer", "new answer"))
        self.assertEqual(self.capture()["delta"], "[assistant] new answer\n")

    def test_legacy_unmarked_response_mirror_does_not_inject_context(self):
        self.append(response("user", "msg-unmarked", "UNMARKEDCONTEXT"),
                    {"type":"event_msg", "payload":{"type":"user_message", "id":"legacy-1", "message":BODY}},
                    response("user", "msg-mirror", BODY))
        self.assertEqual(self.capture()["delta"], "[user] " + BODY + "\n")
        self.assertEqual([row["uuid"] for row in self.messages()], ["legacy-1"])

    def test_repeated_real_speech_is_not_semantically_deduplicated(self):
        self.append(*current_turn(BODY, "1"), *current_turn(BODY, "2"))
        self.assertEqual(self.capture()["delta"].count("[user] " + BODY), 2)
        self.assertEqual(self.capture()["delta"].count("[assistant] ACK"), 2)
        # Two separate inputs during one turn retain both utterances even if
        # their exact text is the same. Only the one-to-one format mirrors pair.
        self.append(response("user", "same-turn-u1", "again", "turn-3", ["user.text"]),
                    response("user", "same-turn-u2", "again", "turn-3", ["user.text"]),
                    item("user", "same-turn-i1", "again", "turn-3"),
                    item("user", "same-turn-i2", "again", "turn-3"))
        self.assertEqual(self.capture()["delta"].count("[user] again"), 2)

    def test_completed_assistant_mirrors_and_replays_emit_once(self):
        answer = item("assistant", "answer", "visible reply")
        self.append(answer, answer, response("assistant", "answer", "visible reply"),
                    item("assistant", "different-answer", "visible reply"),
                    response("assistant", "different-answer", "visible reply"),
                    response("assistant", "response-only-answer", "visible reply"))
        self.assertEqual(self.capture()["delta"].count("[assistant] visible reply"), 3)
        self.assertEqual([row["uuid"] for row in self.messages()],
                         ["answer", "different-answer", "response-only-answer"])

    def test_capture_before_mirror_append_keeps_uuid_and_pending_tail(self):
        self.append(response("user", "msg-user-1", BODY, kinds=["user.text"]))
        first = self.capture()
        original = self.messages()[0]
        self.append(item("user", "item-user-1", BODY), item("assistant", "msg-agent-1", "ACK"),
                    response("assistant", "msg-agent-1", "ACK"))
        self.assertEqual(self.messages()[0], original)
        self.close(first)
        self.assertEqual(self.marker(), "msg-user-1")
        second = self.capture()
        self.assertEqual(second["delta"], "[assistant] ACK\n")
        self.append(*current_turn(NEXT, "2"))
        self.close(second)
        self.assertEqual(self.marker(), "msg-agent-1")
        third = self.capture()
        self.assertEqual(third["delta"], "[user] " + NEXT + "\n\n[assistant] ACK\n")
        self.assertNotIn(NEXT, base64.urlsafe_b64decode(third["frontier"]).decode())
        self.close(third)
        self.assertEqual(self.marker(), "msg-agent-2")
        self.assertEqual(self.capture()["delta"], "")

    def test_user_text_metadata_filters_parts_without_content_keyword_rules(self):
        mixed = response("user", "mixed", "placeholder", kinds=[])
        mixed["payload"]["content"] = [
            {"type":"input_text", "text":"INJECTEDAGENTS"},
            {"type":"input_text", "text":"<AGENTS.md> this is actual user speech"},
            {"type":"input_text", "text":"INJECTEDENVIRONMENT"}]
        mixed["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = [
            "agents_md.instructions", "user.text", "environments.environment_context"]
        self.append(mixed)
        self.assertEqual(self.capture()["delta"], "[user] <AGENTS.md> this is actual user speech\n")

    def test_multimodal_text_parts_and_missing_id_fallback_are_stable(self):
        event = item("user", "unused", "one")
        event["payload"]["item"].pop("id")
        event["payload"]["item"]["content"].extend([
            {"type":"image", "url":"synthetic://image"}, {"type":"text", "text":"한국어 two"},
            {"type":"text", "text":42}])
        self.append([], {"type":"event_msg", "payload":[]}, event)
        before = self.messages()
        self.assertEqual(before[0]["text"], "one\n한국어 two")
        self.assertEqual(before[0]["uuid"], "2026-09-07T14:00:00Z:3")
        self.append(item("assistant", "later", "tail"))
        self.assertEqual(self.messages()[0], before[0])


if __name__ == "__main__":
    unittest.main()
