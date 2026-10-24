from __future__ import annotations

import os
from pathlib import Path
import fcntl
import json
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock
from types import SimpleNamespace

import human_wait_state as state
import human_gate_observation as observation


class HumanWaitStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path = mock.patch.dict(os.environ, {"PATH": str(Path(self.tmp.name) / "empty-bin")})
        self.path.start()
        self.addCleanup(self.path.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_only_explicit_first_line_markers_open_private_signal(self):
        self.assertIsNone(state.marked_question("Would you like me to proceed?"))
        self.assertIsNone(state.marked_question("문장 [답변 필요] 중간 표지"))
        self.assertEqual(state.marked_question("[답변 필요] 진행할까요?\n부연"), "진행할까요?")
        self.assertEqual(state.marked_question("[결정 필요]\x1b[31m배포 범위\x1b[0m"), "배포 범위")
        self.assertEqual(state.marked_question("긴 작업 보고\n변경 사항과 결과\n[답변 필요] 마지막 선택"), "마지막 선택")
        self.assertIsNone(state.marked_question("예시:\n```text\n[답변 필요] 코드 예시\n```"))
        self.assertEqual(state.marked_question("~~~md\n[결정 필요] 코드 예시\n~~~\n[결정 필요] 실제 선택"),
                         "실제 선택")
        self.assertTrue(state.open_signal("claude", "sid-1", "seat-1",
                                          "[답변 필요] 진행할까요?", sequence=3, now=100))
        path = next(Path(self.tmp.name).rglob("*.json"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertTrue(state.open_signal("claude", "sid-1", "seat-1",
                                          "[답변 필요] 진행할까요?", sequence=3, now=200))
        self.assertEqual(state.read_signals(now=200)[0]["since"], 100)

    def test_reader_is_read_only_and_user_prompt_closes_exact_seat(self):
        self.assertTrue(state.open_signal("codex", "sid-2", "seat-2",
                                          "[결정 필요] A 또는 B", sequence=2, now=100))
        path = next(Path(self.tmp.name).rglob("*.json"))
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        result = observation.read_answer_waits(jobs_paths=(), now=101)
        after = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["question"], "A 또는 B")
        self.assertTrue(state.close_signal("seat-2", sequence=3))
        self.assertEqual(observation.read_answer_waits(jobs_paths=(), now=102)["total"], 0)

    def test_stale_signal_remains_source_state_and_is_not_deleted_by_reader(self):
        self.assertTrue(state.open_signal("opencode", "sid-3", "seat-3",
                                          "[답변 필요] 확인", sequence=0, now=10))
        path = next(Path(self.tmp.name).rglob("*.json"))
        before = path.read_bytes()
        result = observation.read_answer_waits(jobs_paths=(), now=10 + 8 * 86400)
        self.assertEqual(result["items"], [])
        self.assertEqual((result["total"], result["stale_total"]), (0, 0))
        self.assertEqual(path.read_bytes(), before)

    def test_s4_current_older_and_expired_windows_match_shared_policy(self):
        now = 10 * 86400
        for sid, seat, since in (("s-current", "seat-current", now - 72 * 3600 + 1),
                                 ("s-older", "seat-older", now - 72 * 3600),
                                 ("s-expired", "seat-expired", now - 7 * 86400 - 1)):
            self.assertTrue(state.open_signal("claude", sid, seat,
                                              "[답변 필요] fixture", now=since))
        result = observation.read_answer_waits(jobs_paths=(), now=now)
        by_id = {item["owner"]["session_id"]: item for item in result["items"]}
        self.assertEqual(set(by_id), {"s-current", "s-older"})
        self.assertFalse(by_id["s-current"]["stale"])
        self.assertTrue(by_id["s-older"]["stale"])
        self.assertEqual((result["total"], result["stale_total"]), (1, 1))

    def test_s1_windows_kind_mapping_and_stale_activity_fixture(self):
        from tools.fleet import interaction
        now = 20 * 86400
        interactions = Path(self.tmp.name) / "interactions"
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(interactions)}):
            for sid, kind, since in (("s1-current", "approval", now - 1),
                                     ("s1-older", "permission", now - 72 * 3600),
                                     ("s1-expired", "decision", now - 7 * 86400 - 1)):
                self.assertTrue(interaction.set_wait(sid, "codex", kind,
                                                     "codex-rollout", now=since))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
            by_session = {item["owner"]["session_id"]: item for item in result["items"]}
            self.assertEqual(set(by_session), {"s1-current", "s1-older"})
            self.assertEqual((result["total"], result["stale_total"]), (1, 1))
            self.assertEqual(by_session["s1-current"]["kind"], "approval")
            self.assertEqual(by_session["s1-older"]["kind"], "permission")
            self.assertTrue(state.publish_cache(result))
            fleet = state.cached_fleet_view(now=now, request=False)
            mapped = {item["session_id"]: item for item in fleet["items"]}
            self.assertEqual(mapped["s1-current"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-current"]["kind"], "approval")
            self.assertEqual(mapped["s1-older"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-older"]["stale"], "older")
            # A real later session activity closes a stale rollout sidecar; an empty
            # producer session set cannot establish that closure (investigated below).
            active = SimpleNamespace(harness="codex", session_id="s1-current",
                                     started_at=1, _interaction_activity=now)
            closed = observation.read_answer_waits(jobs_paths=(), sessions=(active,), now=now)
            self.assertNotIn("s1-current", {item["owner"]["session_id"]
                                            for item in closed["items"]})

    def test_producer_keeps_codex_sidecar_pending_after_later_rollout_write_without_sessions(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.fleet import interaction
        now = 2_000_000
        sid = "12345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-2026-10-23T00-00-00-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text('{"type":"event_msg"}\n', encoding="utf-8")
        os.utime(rollout, (now - 10, now - 10))
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision",
                                                 "codex-rollout", now=now - 100))
            # This is the detached producer path: no Fleet session objects supplied.
            pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(sid, {item["owner"]["session_id"] for item in pending["items"]})

            missing_sid = "abcdefab-1234-1234-1234-123456789abc"
            self.assertTrue(interaction.set_wait(missing_sid, "codex", "decision",
                                                 "codex-rollout", now=now - 100))
            still_pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(missing_sid, {item["owner"]["session_id"]
                                        for item in still_pending["items"]})

    def test_producer_closes_codex_sidecar_after_later_task_started(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "12345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rollout.write_text(json.dumps({"type": "event_msg", "timestamp": timestamp,
                                       "payload": {"type": "task_started", "turn_id": "new-turn"}}) + "\n",
                           encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision", "codex-rollout", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertNotIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_codex_permission_wait_after_same_turn_records(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "22345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [
            {"type": "event_msg", "timestamp": timestamp,
             "payload": {"type": "token_count", "info": {"total_tokens": 12}}},
            {"type": "response_item", "timestamp": timestamp,
             "payload": {"type": "function_call", "name": "request_permission"}},
        ]
        rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}):
            self.assertTrue(interaction.set_wait(sid, "codex", "permission",
                                                 "codex-permissionrequest", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_codex_wait_when_turn_proof_is_beyond_tail_cap(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "32345678-1234-1234-1234-123456789abc"
        state_dir = Path(self.tmp.name) / "interactions"
        codex_home = Path(self.tmp.name) / "codex-home"
        rollout = codex_home / "sessions" / "2026" / "10" / "23" / ("rollout-session-" + sid + ".jsonl")
        rollout.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        proof = json.dumps({"type": "event_msg", "timestamp": timestamp,
                            "payload": {"type": "task_started", "turn_id": "old"}})
        rollout.write_text(proof + "\n" + ("{}\n" * 100), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CODEX_HOME": str(codex_home)}), \
             mock.patch.object(observation, "_S1_LOG_TAIL_MAX_BYTES", 64):
            self.assertTrue(interaction.set_wait(sid, "codex", "decision", "codex-rollout", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_claude_sidecar_pending_after_transcript_write_without_sessions(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tools.fleet import interaction
        now = 2_000_000
        sid = "fedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"attachment"}\n', encoding="utf-8")
        os.utime(transcript, (now - 10, now - 10))
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision",
                                                 "claude-asktool", now=now - 100))
            # Transcript metadata can be appended while the user is still deciding.
            pending = observation.read_answer_waits(jobs_paths=(), now=now)
            self.assertIn(sid, {item["owner"]["session_id"] for item in pending["items"]})

    def test_producer_closes_claude_sidecar_after_later_user_turn(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "fedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        transcript.write_text(json.dumps({"type": "user", "timestamp": timestamp,
                                          "message": {"content": "A later user reply"}}) + "\n",
                              encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision", "claude-asktool", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertNotIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_producer_keeps_claude_wait_after_metadata_attachment_and_tool_result(self):
        from tools.fleet import interaction
        now = 2_000_100
        sid = "cedcba98-7654-4321-8765-abcdefabcdef"
        state_dir = Path(self.tmp.name) / "interactions"
        claude_home = Path(self.tmp.name) / "claude-home"
        transcript = claude_home / "projects" / "project" / (sid + ".jsonl")
        transcript.parent.mkdir(parents=True)
        timestamp = datetime.fromtimestamp(now - 50, timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [
            {"type": "user", "timestamp": timestamp, "isMeta": True},
            {"type": "attachment", "timestamp": timestamp, "content": "image"},
            {"type": "user", "timestamp": timestamp,
             "message": {"content": [{"type": "tool_result", "content": "tool output"}]}},
            {"type": "user", "timestamp": timestamp,
             "message": {"content": [{"type": "image", "source": {"type": "base64"}}]}},
        ]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FLEET_INTERACTION_STATE_DIR": str(state_dir),
                                          "CLAUDE_CONFIG_DIR": str(claude_home)}):
            self.assertTrue(interaction.set_wait(sid, "claude", "decision", "claude-asktool", now=now - 100))
            result = observation.read_answer_waits(jobs_paths=(), now=now)
        self.assertIn(sid, {item["owner"]["session_id"] for item in result["items"]})

    def test_fake_herdr_gets_quiet_registration_and_only_six_hour_sound(self):
        import time
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        calls = Path(self.tmp.name) / "herdr.calls"
        fake = bin_dir / "herdr"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERDR_TEST_LOG\"\n", encoding="utf-8")
        fake.chmod(0o755)
        now = time.time()
        result = {"observed_at": now, "items": [{"id": "S3:attempt", "since": now - 7 * 3600,
                  "question": "사용자 답을 기다림"}]}
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                                          "HERDR_TEST_LOG": str(calls)}), \
             mock.patch.object(state, "_request_producer"):
            state._notify_result(result)
            state._notify_result(result)
        lines = calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("notification show", lines[0])
        self.assertNotIn("--sound", lines[0])
        self.assertIn("--sound request", lines[-1])
        self.assertTrue(all("사용자 답을 기다림" in line for line in lines))

    def test_absent_herdr_does_not_leave_a_dedupe_marker(self):
        with mock.patch("human_wait_state.shutil.which", return_value=None):
            self.assertFalse(state._notify_item({"id": "S4:missing", "question": "확인"},
                                                boundary="registered"))
        self.assertFalse((state.state_root() / "notifications").exists())

    def test_producer_stays_only_for_future_current_notification_boundaries(self):
        now = 10_000.0
        item = {"source": "S3", "since": now - 1800, "stale": False}
        self.assertEqual(state._notification_delay([item], now, enabled=True), 1800)
        self.assertIsNone(state._notification_delay([item], now, enabled=False))
        self.assertIsNone(state._notification_delay([dict(item, stale=True)], now, enabled=True))
        self.assertIsNone(state._notification_delay(
            [{"source": "S3", "since": now - 73 * 3600, "stale": False}], now, enabled=True))

    def test_idle_producer_sleeps_until_notification_boundary(self):
        demand = state.state_root() / ".demand"
        demand.parent.mkdir(parents=True, exist_ok=True)
        demand.touch()
        os.utime(demand, (0, 0))
        now = 10_000.0
        cached = {"items": [{"source": "S3", "since": now, "stale": False}]}
        class Slept(Exception):
            pass
        with mock.patch.object(state, "_refresh_cache"), \
             mock.patch.object(state, "_read_cache", return_value=cached), \
             mock.patch.object(state.shutil, "which", return_value="/bin/herdr"), \
             mock.patch.object(state.time, "time", return_value=now), \
             mock.patch.object(state.time, "sleep", side_effect=Slept) as sleep, \
             mock.patch.object(state.fcntl, "flock"), mock.patch.object(state.os, "close"):
            state._producer_loop(123)
        sleep.assert_called_once_with(3600.0)

    def test_notification_claim_is_single_under_concurrent_refresh(self):
        bin_dir = Path(self.tmp.name) / "race-bin"
        bin_dir.mkdir()
        calls = Path(self.tmp.name) / "race.calls"
        fake = bin_dir / "herdr"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERDR_TEST_LOG\"\n", encoding="utf-8")
        fake.chmod(0o755)
        item = {"id": "S4:race", "question": "같은 대기", "since": 100}
        errors = []
        def notify():
            try:
                state._notify_item(item, boundary="registered")
            except Exception as exc:
                errors.append(exc)
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir), "HERDR_TEST_LOG": str(calls)}):
            threads = [threading.Thread(target=notify) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(calls.read_text(encoding="utf-8").splitlines()), 1)

    def test_producer_is_single_flight_before_spawn(self):
        fd_holder = []
        spawner = mock.Mock(side_effect=lambda _cmd, **kw: (
            fd_holder.append(os.dup(kw["pass_fds"][0])) or SimpleNamespace()))
        with mock.patch.object(state.subprocess, "Popen", spawner):
            self.assertTrue(state._ensure_producer())
            self.assertFalse(state._ensure_producer())
            self.assertEqual(len(fd_holder), 1)
        for held_fd in fd_holder:
            os.close(held_fd)

    def test_spawn_failure_releases_projection_lock_for_next_demand(self):
        with mock.patch.object(state.subprocess, "Popen", side_effect=OSError("fixture spawn failure")):
            self.assertFalse(state._ensure_producer())
        with mock.patch.object(state.subprocess, "Popen", return_value=SimpleNamespace()):
            self.assertTrue(state._ensure_producer())

    def test_projection_failure_publishes_fresh_incomplete_unknown(self):
        import human_gate_observation
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from fleet.collectors import dispatch
        with mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[]), \
             mock.patch.object(human_gate_observation, "read_answer_waits",
                               side_effect=RuntimeError("fixture failure")):
            self.assertFalse(state._refresh_cache())
        value = state._read_cache()
        self.assertIsNotNone(value)
        self.assertFalse(value["complete"])
        self.assertEqual(value["items"], [])
        self.assertGreater(value["observed_at"], 0)

    def test_v1_and_malformed_projection_are_unknown(self):
        path = state._cache_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text('{"schema_version":1,"observed_at":100,"complete":true,'
                        '"total":9,"by_session":{}}', encoding="utf-8")
        path.chmod(0o600)
        self.assertIsNone(state._read_cache(now=101))
        path.write_text('{"schema_version":2,"observed_at":100,"items":[{}],'
                        '"by_session":{},"total":0,"stale_total":0}', encoding="utf-8")
        self.assertIsNone(state._read_cache(now=101))


if __name__ == "__main__":
    unittest.main()
