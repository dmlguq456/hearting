#!/usr/bin/env python3
"""Codex native-stream classification for the dispatch summary owner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "utilities"), str(ROOT / "tools")]

import dispatch_summary as S  # noqa: E402


def line(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def control(kind="dispatch.supervisor.turn.started"):
    return {"type": kind, "attempt_id": "att-fixture"}


def codex_message(text="작업을 준비하고 있습니다"):
    return {"type": "item.completed", "item": {"type": "agent_message", "text": text}}


def rollout_message(role="assistant", text="작업을 준비하고 있습니다"):
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "payload": {
        "type": "message", "role": role,
        "content": [{"type": kind, "text": text}],
    }}


class CodexDispatchSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "FLEET_TITLE_STATE_DIR": str(Path(self.tmp.name) / "titles"),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.log = Path(self.tmp.name) / "owner.att-fixture.codex.jsonl"

    def write(self, *rows):
        self.log.write_bytes(b"".join(line(row) for row in rows))

    def test_supervisor_controls_then_codex_exec_conversation_selects_attempt_log(self):
        self.write(control(), {"type": "thread.started"}, codex_message())
        self.assertEqual(S.announced_session(self.log), (True, None))

    def test_supervisor_controls_then_native_rollout_conversation_selects_attempt_log(self):
        self.write(control(), {"type": "turn.started"}, rollout_message("user"))
        self.assertEqual(S.announced_session(self.log), (True, None))

    def test_tool_usage_and_lifecycle_only_rows_do_not_decide_a_supervised_log(self):
        self.write(
            control(),
            {"type": "item.completed", "item": {"type": "function_call", "name": "exec_command"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": 10}}},
            {"type": "turn.completed"},
        )
        self.assertEqual(S.announced_session(self.log), (False, None))

    def test_masked_bootstrap_without_dialogue_does_not_select_attempt_log(self):
        self.write(control(), rollout_message("user", (
            "# AGENTS.md instructions for /work\n<INSTRUCTIONS>\n"
            "## Codex masked-profile attach layer\nInternal rules\n</INSTRUCTIONS>\n"
            "<environment_context>fixture</environment_context>")))
        self.assertEqual(S.announced_session(self.log), (False, None))
        with self.log.open("ab") as handle:
            handle.write(line(codex_message("실제 구현 진행")))
        self.assertEqual(S.announced_session(self.log), (True, None))

    def test_empty_and_partial_prefix_are_undecided(self):
        self.log.write_bytes(b"")
        self.assertEqual(S.announced_session(self.log), (False, None))
        self.log.write_bytes(b'{"type":"dispatch.supervisor.started"}\n{"type":"item.completed"')
        self.assertEqual(S.announced_session(self.log), (False, None))

    def test_partial_tail_preserves_completed_announcement_and_conversation(self):
        session = {"type": "dispatch.supervisor.session", "session_id": "sid-exact", "cwd": "/work"}
        self.log.write_bytes(line(control()) + line(session) + b'{"type":"partial')
        self.assertEqual(S.announced_session(self.log), (True, {
            "session_id": "sid-exact", "cwd": "/work",
        }))
        self.log.write_bytes(line(control()) + line(codex_message()) + b'{"type":"partial')
        self.assertEqual(S.announced_session(self.log), (True, None))

    def test_announcement_after_conversation_keeps_precedence(self):
        session = {"type": "dispatch.supervisor.session", "session_id": "sid-exact", "cwd": "/work"}
        self.write(control(), codex_message(), session)
        decided, announcement = S.announced_session(self.log)
        self.assertTrue(decided)
        self.assertEqual(announcement["session_id"], "sid-exact")

    def test_duplicate_and_malformed_supervisor_rows_fail_closed(self):
        session = {"type": "dispatch.supervisor.session", "session_id": "sid-exact", "cwd": "/work"}
        self.write(control(), session, session)
        self.assertEqual(S.announced_session(self.log)[1], {
            "invalid": "multiple-supervisor-session-events",
        })
        self.log.write_bytes(line(control()) + b"not-json\n" + line(codex_message()))
        self.assertEqual(S.announced_session(self.log)[1], {"invalid": "malformed-supervisor-log"})

    def test_claude_terminal_receipt_still_requires_session_announcement(self):
        self.write(control(), {"type": "result", "subtype": "success", "result": "PASS"})
        self.assertEqual(S.announced_session(self.log), (True, {
            "invalid": "missing-supervisor-session-event",
        }))
        self.write(control(), {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "A receipt-side assistant row"},
        ]}}, {"type": "result", "result": "PASS"})
        self.assertEqual(S.announced_session(self.log), (True, {
            "invalid": "missing-supervisor-session-event",
        }))

    def test_native_one_shot_stream_remains_its_own_source(self):
        self.write(codex_message())
        self.assertEqual(S.announced_session(self.log), (True, None))

    def test_head_scan_remains_bounded_beyond_existing_64k_limit(self):
        filler = {"type": "item.started", "item": {"type": "function_call", "name": "exec_command"}}
        prefix = line(control()) + line(filler) * 1500
        self.log.write_bytes(prefix + line(codex_message()))
        self.assertGreater(len(prefix), S.SESSION_ANNOUNCE_SCAN)
        self.assertEqual(S.announced_session(self.log), (False, None))

    def test_native_codex_text_triggers_initial_refresh_while_attempt_is_live(self):
        attempt = "att-codex-initial"
        self.log = Path(self.tmp.name) / f"owner.{attempt}.codex.jsonl"
        self.write(control(), {"type": "turn.started"}, codex_message())
        calls = []
        worker = subprocess.Popen(["sleep", "0.2"], start_new_session=True)
        try:
            start = S.process_observation(worker.pid)[1]
            def refresh(_harness, _sid, _source, *, phase, **_kwargs):
                calls.append((phase, S._proc_live(worker.pid, start)[0]))
                return True
            with mock.patch.object(S, "_refresh", side_effect=refresh):
                self.assertEqual(S.supervise(
                    attempt_id=attempt, harness="codex", transcript=self.log,
                    target_pid=worker.pid, target_start=start, poll=0.01,
                    initial_delay=0, final_grace=0, log_quiet=0.01,
                ), 0)
            self.assertTrue(calls)
            self.assertEqual(calls[0], ("initial", True))
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
