from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import importlib.util
import os
from pathlib import Path
import fcntl
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
from unittest import mock
from types import SimpleNamespace

# Standalone class selections must not depend on another test importing the
# project namespace first (PYTHONPATH=tools:utilities does not include ROOT).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
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
        rollouts = mock.patch.object(observation, "_codex_rollout_waits",
                                     return_value={"complete": True, "pending": {}})
        rollouts.start()
        self.addCleanup(rollouts.stop)
        # Tests with an injected clock wait by stat beat; the kernel-notification wake has
        # its own tests (WakeSourceTests), which turn it back on.
        for name, value in (("_WAKE_FAILED", True), ("_WAKE_EVENTS", None), ("_NEXT_WAKE", None),
                            ("_WATCH_STAT", ())):
            patch = mock.patch.object(state, name, value)
            patch.start()
            self.addCleanup(patch.stop)
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
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["items"][0]["stale"])
        self.assertEqual((result["total"], result["stale_total"]), (0, 1))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(state.close_signal("seat-3"))
        self.assertEqual(observation.read_answer_waits(jobs_paths=(), now=10 + 8 * 86400)["items"], [])

    def test_s4_current_older_and_expired_windows_match_shared_policy(self):
        now = 10 * 86400
        for sid, seat, since in (("s-current", "seat-current", now - 72 * 3600 + 1),
                                 ("s-older", "seat-older", now - 72 * 3600),
                                 ("s-expired", "seat-expired", now - 7 * 86400 - 1)):
            self.assertTrue(state.open_signal("claude", sid, seat,
                                              "[답변 필요] fixture", now=since))
        result = observation.read_answer_waits(jobs_paths=(), now=now)
        by_id = {item["owner"]["session_id"]: item for item in result["items"]}
        # F104 supersedes the former seven-day disappearance. The historical
        # "expired" fixture name remains to track the original CI failure.
        self.assertEqual(set(by_id), {"s-current", "s-older", "s-expired"})
        self.assertFalse(by_id["s-current"]["stale"])
        self.assertTrue(by_id["s-older"]["stale"])
        self.assertTrue(by_id["s-expired"]["stale"])
        self.assertEqual((result["total"], result["stale_total"]), (1, 2))

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
            self.assertEqual(set(by_session), {"s1-current", "s1-older", "s1-expired"})
            self.assertEqual((result["total"], result["stale_total"]), (1, 2))
            self.assertEqual(by_session["s1-current"]["kind"], "approval")
            self.assertEqual(by_session["s1-older"]["kind"], "permission")
            self.assertTrue(state.publish_cache(result))
            fleet = state.cached_fleet_view(now=now, request=False)
            mapped = {item["session_id"]: item for item in fleet["items"]}
            self.assertEqual(mapped["s1-current"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-current"]["kind"], "approval")
            self.assertEqual(mapped["s1-older"]["source"], "S1-approval")
            self.assertEqual(mapped["s1-older"]["stale"], "older")
            self.assertEqual(mapped["s1-expired"]["source"], "S1-question")
            self.assertEqual(mapped["s1-expired"]["stale"], "older")
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

    def test_one_fold_rule_gates_notifications_for_every_source(self):
        now = 100_000.0
        items = [
            {"id": "S2:gate", "source": "S2", "since": now - 2 * 3600, "stale": False},
            {"id": "S1:native", "source": "S1", "since": now - 2 * 3600, "stale": False},
            {"id": "S3:aged", "source": "S3", "since": now - 72 * 3600, "stale": False},
            {"id": "S4:aged", "source": "S4", "since": now - 72 * 3600, "stale": False},
            {"id": "S3:ownerless", "source": "S3", "since": now - 2 * 3600, "stale": False,
             "owner_state": "none"},
        ]
        sent = []
        with mock.patch.object(state, "_notify_item",
                               side_effect=lambda item, **kw: sent.append(item["id"]) or True):
            state._notify_result({"observed_at": now, "items": items})
        self.assertEqual(set(sent), {"S2:gate", "S1:native"})
        self.assertIsNone(state.wait_fold(now - 72 * 3600 + 1, now))
        self.assertEqual(state.wait_fold(now - 72 * 3600, now), "older")
        self.assertEqual(state.wait_fold(now - 30 * 86400, now), "older")
        self.assertEqual(state.wait_fold(now, now, owner_state="none"), "ownerless")
        self.assertIsNone(state.wait_fold(now, now, owner_state="unknown"))
        self.assertIsNone(state.wait_fold("not-a-time", now))
        self.assertIsNone(state._notification_delay(items[2:], now, enabled=True))
        self.assertEqual(state._notification_delay(items[:1], now, enabled=True), 4 * 3600)

    def test_publish_carries_owner_facts_and_one_count_to_every_projection(self):
        from tools.fleet.refresh import fleet_answer_wait_view
        now = 1_000_000.0
        result = {"observed_at": now, "complete": True, "health": [], "items": [
            {"id": "S3:live", "source": "S3", "kind": "decision", "since": now - 60,
             "stale": False, "fold": None, "route_id": "rt-1", "attempt_id": "att-1",
             "owner": {"harness": "claude", "session_id": "sid-new"},
             "owner_state": "successor", "attribution": "lineage", "cwd": "/work/repo",
             "registered_owner": {"harness": "claude", "session_id": "sid-old"},
             "question": "PRIVATE-BLOCKER"},
            {"id": "S2:gone", "source": "S2", "kind": "decision", "since": now - 60,
             "stale": True, "fold": "ownerless", "route_id": "rt-2", "gate_id": "frame-review",
             "owner": {"harness": "codex", "session_id": "sid-gone"},
             "owner_state": "none", "attribution": "lineage"},
            {"id": "S4:aged", "source": "S4", "kind": "decision", "since": now - 80 * 3600,
             "stale": True, "fold": "older", "owner_state": "unknown", "attribution": "exact",
             "owner": {"harness": "codex", "session_id": "sid-4"}},
        ]}
        self.assertTrue(state.publish_cache(result))
        private = state._read_cache(now=now)
        self.assertEqual((private["total"], private["older"], private["ownerless"]), (1, 2, 1))
        rows = {row["id"]: row for row in private["items"]}
        self.assertEqual(rows["S3:live"]["registered_owner"],
                         {"harness": "claude", "session_id": "sid-old"})
        self.assertEqual((rows["S3:live"]["owner_state"], rows["S3:live"]["cwd"]),
                         ("successor", "/work/repo"))
        self.assertEqual((rows["S2:gone"]["owner_state"], rows["S2:gone"]["fold"]),
                         ("none", "ownerless"))
        self.assertEqual(private["by_session"], {"claude\0sid-new": ["S3:live"]})
        fleet = state.cached_fleet_view(now=now, request=False)
        self.assertEqual((fleet["total"], fleet["older"], fleet["ownerless"]), (1, 2, 1))
        by_id = {row["id"]: row for row in fleet["items"]}
        self.assertEqual((by_id["S3:live"]["registered_session_id"], by_id["S3:live"]["stale"]),
                         ("sid-old", "current"))
        self.assertEqual((by_id["S2:gone"]["fold"], by_id["S2:gone"]["stale"]),
                         ("ownerless", "older"))
        view = fleet_answer_wait_view(fleet)
        self.assertEqual(view["ownerless"], 1)
        self.assertEqual(state.wait_counts(view["items"]),
                         {"total": view["total"], "older": view["older"], "ownerless": 1})
        self.assertEqual({row["id"]: row.get("owner_state") for row in view["items"]},
                         {"S3:live": "successor", "S2:gone": "none", "S4:aged": "unknown"})
        self.assertNotIn("PRIVATE-BLOCKER", state._fleet_cache_path().read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE-BLOCKER", state._cache_path().read_text(encoding="utf-8"))

    def test_frame_interview_detail_names_each_recommended_choice(self):
        import frame_interview as FI
        interview = {"understanding": "Restated task", "questions": [
            {"id": "scope", "question": "Which scope?", "recommended": 1,
             "options": [{"label": "Small", "means": "Only the gate"}, {"label": "Both"}]},
            {"id": "speed", "question": "Fast?", "recommended": 7,
             "options": [{"label": "Yes"}, {"label": "No"}]}]}
        binding = {"route": {"route_id": "rt-x", "nodes": [],
                             "human_gate_bindings": [{"gate": "frame-review", "node": "owner"}]},
                   "resolution": {}}
        with mock.patch.object(state, "_workflow_gate_interview", return_value=(interview, None)):
            detail, error = state._workflow_gate_detail(binding, {"gate_id": "frame-review"})
        self.assertIsNone(error)
        questions = detail["detail"]["questions"]
        self.assertEqual([question["id"] for question in questions],
                         [FI.UNDERSTANDING_SLOT, "scope", "speed"])
        self.assertEqual([question.get("recommended") for question in questions], [0, 1, None])

    def test_absent_herdr_does_not_leave_a_dedupe_marker(self):
        with mock.patch("human_wait_state.shutil.which", return_value=None):
            self.assertFalse(state._notify_item({"id": "S4:missing", "question": "확인"},
                                                boundary="registered"))
        self.assertFalse((state.state_root() / "notifications").exists())

    def _fake_notifiers(self, *, herdr_rc=None, clients="/dev/pts/9\n"):
        """Fake herdr/tmux/notify-send on an otherwise empty PATH; each logs its argv."""
        bin_dir = Path(self.tmp.name) / "notify-bin"
        bin_dir.mkdir(exist_ok=True)
        log = Path(self.tmp.name) / "notify.calls"
        clients_file = Path(self.tmp.name) / "clients"
        clients_file.write_text(clients, encoding="utf-8")
        scripts = {
            "tmux": "#!/bin/sh\nprintf 'tmux' >> \"$NOTIFY_LOG\"\nfor a; do printf ' [%s]' \"$a\" >> \"$NOTIFY_LOG\"; done\n"
                    "printf '\\n' >> \"$NOTIFY_LOG\"\n"
                    "case \" $* \" in *\" list-clients \"*) while IFS= read -r l; do printf '%s\\n' \"$l\"; done"
                    " < \"$NOTIFY_CLIENTS\";; esac\nexit 0\n",
            "notify-send": "#!/bin/sh\nprintf 'notify-send' >> \"$NOTIFY_LOG\"\n"
                           "for a; do printf ' [%s]' \"$a\" >> \"$NOTIFY_LOG\"; done\nprintf '\\n' >> \"$NOTIFY_LOG\"\n",
        }
        herdr_rc_file = Path(self.tmp.name) / "herdr.rc"
        if herdr_rc is not None:
            herdr_rc_file.write_text("%d\n" % herdr_rc, encoding="utf-8")
            scripts["herdr"] = ("#!/bin/sh\nprintf 'herdr %s\\n' \"$*\" >> \"$NOTIFY_LOG\"\n"
                                "read rc < \"$NOTIFY_HERDR_RC\"\nexit \"$rc\"\n")
        for name, body in scripts.items():
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        return {"PATH": str(bin_dir), "NOTIFY_LOG": str(log), "NOTIFY_CLIENTS": str(clients_file),
                "NOTIFY_HERDR_RC": str(herdr_rc_file), "TMUX": self._tmux_socket() + ",1,0"}, log

    def _tmux_socket(self):
        """A real (listening-free) Unix socket file standing in for this process's tmux server."""
        import socket
        directory = tempfile.mkdtemp(prefix="hws", dir="/tmp")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "s")
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.close()
        return path

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})   # Korean notification words
    def test_without_herdr_a_question_reaches_tmux_clients_and_the_desktop_once(self):
        env, log = self._fake_notifiers()
        env.update(DISPLAY=":0", DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent")
        item = {"id": "S4:tmux", "question": "배포 #1 진행?", "since": time.time()}
        sock = env["TMUX"].split(",")[0]
        with mock.patch.dict(os.environ, env):
            self.assertTrue(state._notify_item(item, boundary="registered"))
            self.assertTrue(state._notify_item(item, boundary="registered"))
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, [
            "tmux [-S] [%s] [list-clients] [-F] [#{client_name}]" % sock,
            "tmux [-S] [%s] [display-message] [-c] [/dev/pts/9] [-d] [10000] [--] [◑답변 필요: 배포 ##1 진행?]" % sock,
            "notify-send [-a] [Hearting] [--] [◑답변 필요] [배포 #1 진행?]",
        ])

    @mock.patch.dict("os.environ", {"FLEET_NOW_LANG": "Korean"})   # Korean notification words
    def test_failed_herdr_is_retried_while_tmux_shows_the_reminder_once(self):
        env, log = self._fake_notifiers(herdr_rc=1)
        item = {"id": "S4:fallback", "question": "확인", "since": 1}
        with mock.patch.dict(os.environ, env):
            for key in ("DISPLAY", "WAYLAND_DISPLAY"):
                os.environ.pop(key, None)  # a bare login: the desktop is never tried
            # Herdr fails twice: each refresh tries Herdr again; tmux shows it once.
            self.assertFalse(state._notify_item(item, boundary="6h", sound=True))
            self.assertFalse(state._notify_item(item, boundary="6h", sound=True))
            Path(env["NOTIFY_HERDR_RC"]).write_text("0\n", encoding="utf-8")
            self.assertTrue(state._notify_item(item, boundary="6h", sound=True))
            self.assertTrue(state._notify_item(item, boundary="6h", sound=True))  # delivered: no call
        lines = log.read_text(encoding="utf-8").splitlines()
        herdr = [line for line in lines if line.startswith("herdr ")]
        self.assertEqual(len(herdr), 3)
        self.assertTrue(all(line.startswith("herdr notification show ◑답변 필요 · ") and "--sound request" in line
                            for line in herdr))
        self.assertEqual(sum("[display-message] [-c] [/dev/pts/9]" in line for line in lines), 1)
        self.assertFalse(any(line.startswith("notify-send") for line in lines))

    def test_unseen_reminder_keeps_no_marker_and_shows_once_a_client_attaches(self):
        env, log = self._fake_notifiers(clients="")
        item = {"id": "S4:later", "question": "나중에", "since": time.time()}
        with mock.patch.dict(os.environ, env):
            for key in ("DISPLAY", "WAYLAND_DISPLAY"):
                os.environ.pop(key, None)
            self.assertFalse(state._notify_item(item, boundary="registered"))
            self.assertEqual(list((state.state_root() / "notifications").glob("*.sent")), [])
            Path(env["NOTIFY_CLIENTS"]).write_text("/dev/pts/3\n", encoding="utf-8")
            self.assertTrue(state._notify_item(item, boundary="registered"))
        self.assertEqual(sum("display-message" in line for line in log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(state._notification_available(), False)  # PATH is the empty bin again

    def test_an_installed_tmux_nobody_runs_in_keeps_no_producer_or_notification(self):
        env, log = self._fake_notifiers()
        item = {"id": "S4:no-server", "question": "확인", "since": time.time()}
        for tmux in (None, os.path.join(self.tmp.name, "not-a-socket")):
            with self.subTest(tmux=tmux), mock.patch.dict(os.environ, env):
                for key in ("TMUX", "DISPLAY", "WAYLAND_DISPLAY"):
                    os.environ.pop(key, None)
                if tmux:
                    Path(tmux).write_text("", encoding="utf-8")
                    os.environ["TMUX"] = tmux + ",1,0"
                self.assertFalse(state._notification_available())
                self.assertFalse(state._notify_item(item, boundary="registered"))
                # Without a notifier the producer has no reminder boundary to stay for.
                self.assertIsNone(state._notification_delay(
                    [{"source": "S4", "since": time.time(), "stale": False}], time.time(),
                    enabled=state._notification_available()))
        self.assertFalse(log.exists())

    def test_producer_stays_only_for_future_current_notification_boundaries(self):
        now = 10_000.0
        item = {"source": "S3", "since": now - 1800, "stale": False}
        self.assertEqual(state._notification_delay([item], now, enabled=True), 1800)
        self.assertIsNone(state._notification_delay([item], now, enabled=False))
        self.assertIsNone(state._notification_delay([dict(item, stale=True)], now, enabled=True))
        self.assertIsNone(state._notification_delay(
            [{"source": "S3", "since": now - 73 * 3600, "stale": False}], now, enabled=True))

    def _idle_producer(self, *, display_returns):
        """Run an idle producer whose next notification boundary is an hour away.

        Returns (sleeps, refreshes); the second wait stops the loop."""
        demand = state.state_root() / ".demand"
        demand.parent.mkdir(parents=True, exist_ok=True)
        demand.touch()
        os.utime(demand, (0, 0))
        now = 10_000.0
        cached = {"items": [{"source": "S3", "since": now, "stale": False}]}
        sleeps = []

        class Slept(BaseException):   # not swallowed by the loop's ``except Exception``
            pass

        def wait(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 1:
                raise Slept()
            if display_returns:
                os.utime(demand, (now, now))

        with mock.patch.object(state, "_refresh_cache") as refresh, \
             mock.patch.object(state, "_read_cache", return_value=cached), \
             mock.patch.object(state.shutil, "which", return_value="/bin/herdr"), \
             mock.patch.object(state.time, "time", return_value=now), \
             mock.patch.object(state.time, "sleep", side_effect=wait), \
             mock.patch.object(state.fcntl, "flock"), mock.patch.object(state.os, "close"):
            with self.assertRaises(Slept):
                state._producer_loop(123)
        return sleeps, refresh.call_count

    def test_idle_producer_wakes_every_30s_but_refreshes_only_at_its_boundary(self):
        sleeps, refreshes = self._idle_producer(display_returns=False)
        self.assertEqual(sleeps, [30.0, 30.0])
        self.assertEqual(refreshes, 1)   # the first pass only; the boundary is an hour away

    def test_idle_producer_refreshes_as_soon_as_a_display_returns(self):
        sleeps, refreshes = self._idle_producer(display_returns=True)
        self.assertEqual(sleeps, [30.0, 30.0])   # back on the live cadence
        self.assertEqual(refreshes, 2)

    def _run_producer_with(self, *, during_refresh=None, during_sleep=None):
        """Run the real producer loop with an instant observation and an injected wait.

        Returns (reads, sleeps, kept_running); a second wait stops the loop there."""
        root = state.state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / ".demand").touch()
        lock = os.open(Path(self.tmp.name) / "producer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        result = {"items": [], "complete": True, "observed_at": time.time(), "health": []}
        reads, sleeps = [], []

        class KeptRunning(BaseException):   # not swallowed by the loop's ``except Exception``
            pass

        def read(**_kwargs):
            reads.append(1)
            if during_refresh and len(reads) == 1:
                during_refresh()
            return dict(result)

        def wait(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 1:
                raise KeptRunning()
            if during_sleep:
                during_sleep()
        import tools.fleet.collectors.dispatch as dispatch
        kept_running = False
        with mock.patch.object(observation, "read_answer_waits", side_effect=read), \
             mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[]), \
             mock.patch.object(state.time, "sleep", side_effect=wait):
            try:
                state._producer_loop(lock)
            except KeptRunning:
                kept_running = True
        return reads, sleeps, kept_running

    def test_producer_ends_without_recreating_a_root_removed_while_it_waited(self):
        xdg = Path(self.tmp.name) / "hearting"
        reads, sleeps, kept_running = self._run_producer_with(during_sleep=lambda: shutil.rmtree(xdg))
        self.assertFalse(kept_running)
        self.assertEqual((len(reads), len(sleeps)), (1, 1))
        self.assertFalse(xdg.exists())
        self.assertIsNone(state._producer_root)

    def test_producer_does_not_recreate_a_root_removed_during_its_refresh(self):
        xdg = Path(self.tmp.name) / "hearting"
        reads, sleeps, kept_running = self._run_producer_with(during_refresh=lambda: shutil.rmtree(xdg))
        self.assertFalse(kept_running)
        self.assertEqual((len(reads), sleeps), (1, []))
        self.assertFalse(xdg.exists())

    def test_producer_keeps_refreshing_on_its_cadence_while_its_root_exists(self):
        reads, sleeps, kept_running = self._run_producer_with()
        self.assertTrue(kept_running)
        self.assertEqual(len(reads), 2)
        self.assertTrue(0 < sleeps[0] <= 30.0)
        self.assertTrue((state.state_root() / "projection.json").is_file())
        self.assertIsNone(state._producer_root)

    def _producer_with_registry(self, *, change_at=None):
        """Run the real producer loop over a real registry file and an injected wait.

        The registry is appended on the `change_at`-th one-second check (never if
        None). Returns (refreshes, sleeps); the second pass ends the loop."""
        root = state.state_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / ".demand").touch()
        registry = Path(self.tmp.name) / "jobs.log"
        registry.write_text("open\troute-start\n", encoding="utf-8")
        lock = os.open(Path(self.tmp.name) / "producer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        refreshes, sleeps, clock = [], [], [1000.0]

        class SecondPass(BaseException):   # not swallowed by the loop's ``except Exception``
            pass

        def refresh():
            refreshes.append(len(sleeps))
            if len(refreshes) > 1:
                raise SecondPass()

        def wait(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            if change_at is not None and len(sleeps) == change_at:
                with registry.open("a", encoding="utf-8") as handle:
                    handle.write("done\troute-close\n")
            if sum(sleeps) > 120:
                raise SecondPass()
        import tools.fleet.collectors.dispatch as dispatch
        with mock.patch.object(state, "_refresh_cache", side_effect=refresh), \
             mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[str(registry)]), \
             mock.patch.object(state.time, "sleep", side_effect=wait), \
             mock.patch.object(state.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(state, "_REGISTRY_MIN_GAP", 0.0):
            with self.assertRaises(SecondPass):
                state._producer_loop(lock)
        return refreshes, sleeps

    def test_a_registry_write_starts_the_next_pass_before_the_cadence(self):
        # A route start, stage change or close is a registry write; the panels'
        # route state follows it on the next check, not after the 30 second wait.
        refreshes, sleeps = self._producer_with_registry(change_at=3)
        self.assertEqual(len(refreshes), 2)
        self.assertEqual(refreshes[1], 3)
        self.assertLess(sum(sleeps), 30.0)

    def test_an_unchanged_registry_keeps_the_cadence(self):
        refreshes, sleeps = self._producer_with_registry()
        self.assertEqual(len(refreshes), 2)
        self.assertAlmostEqual(sum(sleeps[:refreshes[1]]), 30.0, delta=1.0)

    def test_a_changing_registry_waits_for_the_previous_pass_duration(self):
        # A registry that changes again at once still gets no pass sooner than
        # the previous pass took, so a slow pass is never run back to back.
        root = state.state_root()
        root.mkdir(parents=True, exist_ok=True)
        registry = Path(self.tmp.name) / "jobs.log"
        registry.write_text("open\n", encoding="utf-8")
        import tools.fleet.collectors.dispatch as dispatch
        sleeps, clock = [], [1000.0]
        def wait(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[str(registry)]):
            generation = state._registry_generation()
            registry.write_text("open\ndone\n", encoding="utf-8")
            with mock.patch.object(state.time, "sleep", side_effect=wait), \
                 mock.patch.object(state.time, "monotonic", side_effect=lambda: clock[0]):
                state._sleep_until_registry_change(30.0, generation, clock[0] + 5.0)
        self.assertEqual(sleeps[0], 1.0)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(sleeps[1], 4.0)

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


class _StrictOpenCodeTransport:
    def __init__(self, session_id):
        self.session_id = session_id
        self.forms = {}
        self.form_ids = []
        self.permissions = {}
        self.permission_ids = []
        self.calls = []
        self.unexpected = []
        self.post_status = 204
        self.server_info = {"version": "fixture-v2", "pid": 1234,
                            "urls": ["http://127.0.0.1:4096"],
                            "paths": {"tmp": "/tmp/opencode-fixture"}}
        self.info_status = 200
        self.after_info = None
        self.after_post = None

    def question(self, request_id="frm_1", *, state="pending", message_id="msg_1",
                 field_type="multiselect", min_items=1, max_items=1, custom=False):
        field = {"type": field_type, "key": "choice", "title": "Pick a color",
                 "description": "Choose the color for the task", "required": True}
        if field_type == "multiselect":
            field.update({"options": [{"value": "red", "label": "Red"},
                                       {"value": "blue", "label": "Blue"}],
                          "minItems": min_items, "maxItems": max_items, "custom": custom})
        value = {
            "id": request_id, "sessionID": self.session_id, "title": "Question title",
            "metadata": {"messageID": message_id},
            "fields": [field],
            "state": {"status": state},
        }
        self.forms[request_id] = value
        if request_id not in self.form_ids:
            self.form_ids.append(request_id)
        return value

    def permission(self, request_id="per_1", *, message_id="msg_permission"):
        value = {"id": request_id, "sessionID": self.session_id,
                 "title": "Run a shell command", "type": "shell",
                 "metadata": {"command": "printf fixture"},
                 "pattern": {"action": "shell", "resource": "printf fixture"},
                 "messageID": message_id}
        self.permissions[request_id] = value
        if request_id not in self.permission_ids:
            self.permission_ids.append(request_id)
        return value

    def __call__(self, endpoint, method, path, payload=None):
        self.calls.append((method, path, payload))
        if endpoint.get("url") != "http://127.0.0.1:4096":
            self.unexpected.append((method, path, "endpoint"))
            return {"status": 0, "reason": "fixture-endpoint-mismatch"}
        if method == "GET" and path == "/api/info":
            if self.after_info:
                callback = self.after_info
                self.after_info = None
                callback()
            return {"status": self.info_status, "body": self.server_info}
        prefix = "/api/session/" + self.session_id + "/"
        if method == "GET" and path == prefix + "form":
            data = [{key: value for key, value in self.forms[request_id].items()
                     if key != "state"} for request_id in self.form_ids]
            return {"status": 200, "body": {"data": data}}
        if method == "GET" and path.startswith(prefix + "form/"):
            request_id = path[len(prefix + "form/"):]
            value = self.forms.get(request_id)
            return ({"status": 200, "body": {"data": value}} if value else
                    {"status": 404, "reason": "not-found"})
        if method == "POST" and path.startswith(prefix + "form/") and path.endswith("/reply"):
            request_id = path[len(prefix + "form/"):-len("/reply")]
            if request_id not in self.forms or not isinstance(payload, dict):
                self.unexpected.append((method, path, "form-reply-shape"))
                return {"status": 400, "reason": "fixture-reply-shape"}
            if self.post_status == 204:
                self.forms[request_id]["state"] = {"status": "answered",
                                                    "answer": payload.get("answer", {})}
                if self.after_post:
                    self.after_post()
            return {"status": self.post_status, "body": None}
        if method == "GET" and path == prefix + "permission":
            data = [self.permissions[request_id] for request_id in self.permission_ids]
            return {"status": 200, "body": {"data": data}}
        if method == "GET" and path.startswith(prefix + "permission/"):
            request_id = path[len(prefix + "permission/"):]
            value = self.permissions.get(request_id)
            return ({"status": 200, "body": {"data": value}} if value else
                    {"status": 404, "reason": "not-found"})
        if method == "POST" and path.startswith(prefix + "permission/") and path.endswith("/reply"):
            if not isinstance(payload, dict) or set(payload) != {"decision"}:
                self.unexpected.append((method, path, "permission-reply-shape"))
                return {"status": 400, "reason": "fixture-reply-shape"}
            if self.post_status == 204 and self.after_post:
                self.after_post()
            return {"status": self.post_status, "body": None}
        self.unexpected.append((method, path, payload))
        return {"status": 0, "reason": "fixture-endpoint-unsupported"}


class _OpenCodeHTTPConsumer:
    """Strict local consumer: only the exact original reply can advance work."""

    def __init__(self, api, reply_path, expected, marker):
        self.consumed = []
        self.continuations = []
        self.allowed_work = []
        consumer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                result = api({"url": "http://127.0.0.1:4096"}, "GET", self.path)
                self.send_response(result["status"])
                self.end_headers()
                self.wfile.write(json.dumps(result.get("body")).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                # No receipt, completion event or marker is produced by a
                # different path, wrong answer, duplicate, or absent response.
                if self.path != reply_path or body != expected or consumer.consumed:
                    self.send_response(409)
                    self.end_headers()
                    return
                result = api({"url": "http://127.0.0.1:4096"}, "POST", self.path, body)
                if result["status"] == 204:
                    consumer.consumed.append((api.session_id, self.path, body))
                    if body.get("decision") != "reject":
                        consumer.allowed_work.append("original-task-work")
                    consumer.continuations.append("original-session-next-step")
                    marker.write_text(json.dumps(consumer.continuations), encoding="utf-8")
                self.send_response(result["status"])
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("owned HTTP consumer did not stop")


class SelectedQuestionActionsTest(unittest.TestCase):
    """Strict isolated tests for selected private detail and one-shot native replies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sid = "ses_fixture"
        self.now = time.time()
        self.endpoint = {"url": "http://127.0.0.1:4096", "generation": "fixture-gen-1",
                         "pid": 1234, "proc_start": "123", "socket_inodes": ["700"],
                         "server_pid": 1234, "server_proc_start": "123",
                         "server_urls": ["http://127.0.0.1:4096"],
                         "frontend_pid": 1234, "frontend_proc_start": "123"}
        self.api = _StrictOpenCodeTransport(self.sid)
        self.real_http = state._opencode_http
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "FLEET_INTERACTION_STATE_DIR": str(Path(self.tmp.name) / "interactions"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        endpoint = mock.patch.object(
            state, "_opencode_endpoint",
            side_effect=lambda sid: dict(self.endpoint) if sid == self.sid else None)
        endpoint.start()
        self.addCleanup(endpoint.stop)
        transport = mock.patch.object(state, "_opencode_http", side_effect=self.api)
        transport.start()
        self.addCleanup(transport.stop)
        self._publish_wait()

    def _publish_wait(self, *, harness="opencode", kind="decision", request_id=None,
                      turn_id=None, item_id=None, sid=None, stamp=None):
        sid = sid or self.sid
        stamp = self.now if stamp is None else stamp
        self.item_id = f"S1:{harness}:{sid}:{int(stamp * 1000)}"
        item = {"id": self.item_id, "source": "S1", "kind": kind,
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": stamp,
                "stale": False, "owner": {"harness": harness, "session_id": sid},
                "attribution": "exact"}
        for key, value in (("request_id", request_id), ("turn_id", turn_id), ("item_id", item_id)):
            if value is not None:
                item[key] = value
        self.assertTrue(state.publish_cache({"items": [item], "complete": True,
                                             "observed_at": time.time(), "health": []}))
        return {"id": self.item_id, "harness": harness, "session_id": sid}

    def test_selected_detail_and_draft_stay_private_across_refresh(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id,
                                                "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["detail"]["question"], "Pick a color")
        self.assertEqual(selected["choices"][0]["value"], "red")
        drafted = state.write_selected_draft(selected, "private draft")
        self.assertEqual(drafted["draft"], "private draft")
        self._publish_wait(stamp=self.now + 0.1)
        reopened = state.read_selected_request({**selected, "selected_id": self.item_id})
        self.assertEqual(reopened["draft"], "private draft")
        fleet_bytes = state._fleet_cache_path().read_text(encoding="utf-8")
        projection_bytes = state._cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Pick a color", fleet_bytes)
        self.assertNotIn("private draft", fleet_bytes)
        self.assertNotIn("Pick a color", projection_bytes)
        self.assertIn("private draft", projection_bytes)
        self.assertEqual(self.api.unexpected, [])

    def test_native_sidecar_refs_reach_body_free_s1_projection(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction
        self.assertTrue(interaction.set_wait(
            "thread_42", "codex", "decision", "codex-appserver", now=self.now,
            request_id="server_req_42", turn_id="turn_8", item_id="item_4"))
        observed = observation.read_answer_waits(jobs_paths=[], sessions=(), now=self.now)
        item = next(row for row in observed["items"] if row["source"] == "S1")
        self.assertEqual((item["request_id"], item["turn_id"], item["item_id"]),
                         ("server_req_42", "turn_8", "item_4"))
        self.assertIsNone(item["question"])
        self.assertEqual(item["choices"], [])
        self.assertTrue(state.publish_cache(observed))
        fleet = state.cached_fleet_view(request=False)
        row = next(row for row in fleet["items"] if row["session_id"] == "thread_42")
        self.assertEqual(row["request_id"], "server_req_42")
        self.assertNotIn("question", row)
        self.assertNotIn("choices", row)

    def test_opencode_v2_form_reply_uses_exact_session_request_and_shape(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="answer-1")
        self.assertEqual(result["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red"]}})])
        self.assertEqual(self.api.unexpected, [])

    def test_opencode_local_cardinality_and_option_errors_leave_request_correctable(self):
        self.api.question(min_items=1, max_items=1)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        drafted = state.write_selected_draft(selected, "red, then blue")
        invalid_count = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red", "blue"]}}, action_id="too-many")
        self.assertEqual(invalid_count["state"], "open")
        self.assertEqual(invalid_count["reason"], "opencode-answer-count-invalid")
        self.assertEqual(invalid_count["draft"], "red, then blue")
        self.assertEqual(invalid_count["native_ref"], selected["native_ref"])
        self.assertEqual(invalid_count["actions"], ["reply"])
        self.assertNotIn("action_id", invalid_count)
        self.assertIsNone(state._selected_entry(invalid_count).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        drafted = state.write_selected_draft(invalid_count, "green")
        invalid_option = state.submit_selected_response(
            drafted, {"answers": {"choice": ["green"]}}, action_id="unknown-option")
        self.assertEqual(invalid_option["state"], "open")
        self.assertEqual(invalid_option["reason"], "opencode-answer-not-in-current-options")
        self.assertEqual(invalid_option["draft"], "green")
        self.assertEqual(invalid_option["native_ref"], selected["native_ref"])
        self.assertEqual(invalid_option["actions"], ["reply"])
        self.assertNotIn("action_id", invalid_option)
        self.assertIsNone(state._selected_entry(invalid_option).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        corrected = state.submit_selected_response(
            invalid_option, {"answers": {"choice": ["red"]}}, action_id="corrected-once")
        self.assertEqual(corrected["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red"]}})])
        self.assertEqual(self.api.unexpected, [])

    def test_opencode_minimum_multiselect_bounds_are_enforced_before_claim(self):
        self.api.question(min_items=2, max_items=2)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["native_ref"]["min_items"], 2)
        self.assertEqual(selected["native_ref"]["max_items"], 2)
        drafted = state.write_selected_draft(selected, "red")
        incomplete = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red"]}}, action_id="one-of-two")
        self.assertEqual(incomplete["state"], "open")
        self.assertEqual(incomplete["reason"], "opencode-answer-count-invalid")
        self.assertEqual(incomplete["draft"], "red")
        self.assertIsNone(state._selected_entry(incomplete).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        corrected = state.submit_selected_response(
            incomplete, {"answers": {"choice": ["red", "blue"]}},
            action_id="two-of-two")
        self.assertEqual(corrected["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply",
                                  {"answer": {"choice": ["red", "blue"]}})])

    def test_opencode_string_form_keeps_single_free_text_answer(self):
        self.api.question(field_type="string")
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        drafted = state.write_selected_draft(selected, "single answer")
        invalid = state.submit_selected_response(
            drafted, {"answers": {"choice": ["one", "two"]}}, action_id="string-too-many")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "opencode-string-answer-count-invalid")
        self.assertEqual(invalid["draft"], "single answer")
        self.assertIsNone(state._selected_entry(invalid).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        result = state.submit_selected_response(
            invalid, {"answers": {"choice": ["private answer"]}},
            action_id="string-answer")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"choice": "private answer"}})])

    def test_opencode_custom_multiselect_accepts_native_custom_value(self):
        self.api.question(min_items=1, max_items=1, custom=True)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertTrue(selected["native_ref"]["custom"])
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["user-supplied"]}}, action_id="custom-answer")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"choice": ["user-supplied"]}})])

    def test_approval_approve_is_one_shot_and_defer_sends_nothing(self):
        self._publish_wait(kind="permission")
        self.api.permission()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        deferred = state.submit_selected_response(selected, {"decision": "defer"})
        self.assertEqual(deferred["state"], "open")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])
        selected = state.write_selected_draft(selected, "keep until native consumption is known")
        approved = state.submit_selected_response(selected, {"decision": "approve"},
                                                 action_id="approve-once")
        self.assertEqual(approved["state"], "received")
        self.assertEqual(approved["result"]["runtime"], "unknown")
        self.assertEqual(approved["draft"], selected["draft"])
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/permission/per_1/reply",
                                  {"decision": "once"})])
        duplicate = state.submit_selected_response(selected, {"decision": "reject"},
                                                   action_id="second-action")
        self.assertEqual(duplicate["action_id"], "approve-once")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        self.assertEqual(self.api.unexpected, [])

    def test_approval_reject_maps_to_native_reject(self):
        self._publish_wait(kind="approval")
        self.api.permission()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        result = state.submit_selected_response(selected, {"decision": "reject"},
                                                action_id="reject-once")
        self.assertEqual(result["state"], "received")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/permission/per_1/reply", {"decision": "reject"})])
        self.assertEqual(self.api.unexpected, [])

    def _exercise_http_consumer(self, *, permission=False, decision="approve"):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet import interaction

        request_id = "per_http" if permission else "frm_http"
        kind = "permission" if permission else "decision"
        if permission:
            self.api.permission(request_id)
            expected = {"decision": "once" if decision == "approve" else "reject"}
            response = {"decision": decision}
            collection = "permission"
            turn = "msg_permission"
        else:
            self.api.question(request_id)
            expected = {"answer": {"choice": ["red"]}}
            response = {"answers": {"choice": ["red"]}}
            collection = "form"
            turn = "msg_1"
        path = f"/api/session/{self.sid}/{collection}/{request_id}/reply"
        marker = Path(self.tmp.name) / "original-task-next-step.json"
        consumer = _OpenCodeHTTPConsumer(self.api, path, expected, marker)
        self.addCleanup(consumer.close)
        self.endpoint["url"] = consumer.url
        self.endpoint["server_urls"] = [consumer.url]
        self.assertTrue(interaction.set_wait(
            self.sid, "opencode", kind, "opencode-question", now=self.now,
            request_id=request_id, turn_id=turn))
        self.assertTrue(state.publish_cache(observation.read_answer_waits(jobs_paths=[])))
        fleet = state.cached_fleet_view(request=False)
        self.assertEqual(fleet["total"], 1)
        row = fleet["items"][0]
        self.assertEqual(row["request_id"], request_id)
        with mock.patch.object(state, "_opencode_http", self.real_http):
            selected = state.read_selected_request(row)
            self.assertTrue(selected["support"]["supported"], selected)
            selected = state.write_selected_draft(selected, "private consumer draft")
            self.assertFalse(marker.exists())
            self.assertEqual(consumer.consumed, [])
            if permission:
                deferred = state.submit_selected_response(selected, {"decision": "defer"})
                self.assertEqual(deferred["state"], "open")
                self.assertFalse(marker.exists())
            result = state.submit_selected_response(selected, response, action_id="http-once")
            self.assertEqual(result["state"], "received" if permission else "applied")
            self.assertEqual(len(consumer.consumed), 1)
            self.assertEqual(json.loads(marker.read_text()), ["original-session-next-step"])
            self.assertEqual(consumer.allowed_work,
                             [] if permission and decision == "reject" else ["original-task-work"])
            duplicate = state.submit_selected_response(selected, response, action_id="http-duplicate")
            self.assertNotEqual(duplicate["state"], "open")
            self.assertEqual(len(consumer.consumed), 1)
        fleet_bytes = state._fleet_cache_path().read_text()
        for private in ("Pick a color", "Run a shell command", "private consumer draft", consumer.url):
            self.assertNotIn(private, fleet_bytes)

    def test_http_form_consumer_advances_original_session_only_after_exact_answer(self):
        self._exercise_http_consumer()

    def test_http_permission_consumer_approve_advances_but_receipt_stays_received(self):
        self._exercise_http_consumer(permission=True)

    def test_http_permission_consumer_reject_skips_work_and_continues_once(self):
        self._exercise_http_consumer(permission=True, decision="reject")

    def test_answered_form_requery_retains_binding_and_distinguishes_progress(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id})
        selected = state.write_selected_draft(selected, "private answered draft")
        submitted = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="form-once")
        self.assertEqual(submitted["state"], "applied")
        reread = state.read_selected_request(selected)
        self.assertEqual(reread["native_ref"], selected["native_ref"])
        self.assertEqual(reread["state"], "applied")
        self.assertEqual(reread["result"]["runtime"], "answered")
        self.assertEqual(reread["result"]["progress"], "unknown")
        self.assertEqual(reread["draft"], "")
        self.assertEqual(reread["actions"], [])

    def test_answered_exact_observation_does_not_reappear_as_current_after_refresh(self):
        self.api.question()
        self._publish_wait(request_id="frm_1", turn_id="msg_1")
        observation_snapshot = state._read_cache()
        selected = state.read_selected_request({"id": self.item_id})
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="answered-once")
        self.assertEqual(result["state"], "applied")
        self.assertTrue(state.publish_cache(observation_snapshot))
        fleet = state.cached_fleet_view(request=False)
        self.assertEqual((fleet["total"], fleet["items"]), (0, []))
        self.assertEqual(state.cached_count("opencode", self.sid), (0, 0))
        # A new observation (or a different exact request/turn) is not the old
        # answered row, even when the session and displayed kind are the same.
        self._publish_wait(request_id="frm_2", turn_id="msg_2", stamp=self.now + .5)
        self.assertEqual(state.cached_fleet_view(request=False)["total"], 1)

    def test_permission_loss_and_restart_preserve_draft_and_never_replay(self):
        for index, status in enumerate((0, 204)):
            with self.subTest(http_status=status):
                request_id = "per_loss_" + str(index)
                self.api.permission_ids = []
                self.api.permission(request_id)
                self._publish_wait(kind="permission", request_id=request_id,
                                   stamp=self.now + index)
                selected = state.write_selected_draft(
                    state.read_selected_request({"id": self.item_id}), "uncertain permission draft")
                self.api.post_status = status
                self.api.after_post = lambda: self.endpoint.update(generation="restarted-generation")
                before = len([row for row in self.api.calls if row[0] == "POST"])
                result = state.submit_selected_response(
                    selected, {"decision": "approve"}, action_id="permission-loss-" + str(index))
                self.assertEqual(result["state"], "unknown")
                self.assertEqual(result["draft"], selected["draft"])
                state.submit_selected_response(selected, {"decision": "approve"}, action_id="no-replay")
                self.assertEqual(len([row for row in self.api.calls if row[0] == "POST"]), before + 1)

    def test_corrupt_fleet_cache_metadata_remains_unknown(self):
        path = state._fleet_cache_path()
        original = json.loads(path.read_text())
        for patch in ({"observed_at": float("nan")}, {"observed_at": True},
                      {"total": True}, {"total": -1}, {"items": [None]},
                      {"items": [{"id": "bad", "stale": []}]}):
            with self.subTest(patch=patch):
                state._write(path, {**original, **patch})
                self.assertIsNone(state.cached_fleet_view(request=False))

    def test_missing_or_expired_projection_keeps_exact_private_draft_read_only(self):
        self.api.question()
        selected = state.write_selected_draft(
            state.read_selected_request({"id": self.item_id}), "private offline draft")
        projection = state._read_cache_file()
        projection["observed_at"] -= state._DISPLAY_TTL + 1
        self.assertTrue(state._write_cache_value(projection))
        unknown = state.read_selected_request(selected)
        self.assertEqual(unknown["state"], "unknown")
        self.assertFalse(unknown["support"]["supported"])
        self.assertEqual(unknown["draft"], "private offline draft")
        self.assertEqual(unknown["actions"], [])
        projection["observed_at"] = time.time()
        projection["items"] = []
        self.assertTrue(state._write_cache_value(projection))
        missing = state.read_selected_request(selected)
        self.assertEqual(missing["draft"], "private offline draft")
        self.assertEqual(missing["state"], "unknown")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_replaced_cancelled_and_restarted_requests_are_refused(self):
        self.api.question()
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.api.question("frm_2", message_id="msg_2")
        self.api.form_ids = ["frm_2"]
        replaced = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="late-answer")
        self.assertNotEqual(replaced["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.form_ids = ["frm_1"]
        self.api.forms["frm_1"]["state"] = {"status": "cancelled"}
        cancelled = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                 "session_id": self.sid})
        self.assertEqual(cancelled["state"], "cancelled")
        refused = state.submit_selected_response(
            cancelled, {"answers": {"choice": ["red"]}}, action_id="cancelled-answer")
        self.assertEqual(refused["state"], "cancelled")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.forms["frm_1"]["state"] = {"status": "pending"}
        fresh = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                             "session_id": self.sid})
        self.endpoint["generation"] = "fixture-gen-restarted"
        expired = state.submit_selected_response(
            fresh, {"answers": {"choice": ["red"]}}, action_id="old-server-answer")
        self.assertNotEqual(expired["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_ambiguous_and_ambiguous_delivery_never_replay(self):
        self.api.question("frm_1", message_id="msg_1")
        self.api.question("frm_2", message_id="msg_2")
        ambiguous = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                 "session_id": self.sid})
        self.assertFalse(ambiguous["support"]["supported"])
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.form_ids = ["frm_1"]
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.api.post_status = 0
        first = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="ambiguous-send")
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        second = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="retry")
        self.assertEqual(second["action_id"], "ambiguous-send")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)
        self.assertEqual(self.api.unexpected, [])

    def test_unsupported_runtime_and_schema_keep_native_fallback(self):
        no_endpoint = mock.patch.object(state, "_opencode_endpoint", return_value=None)
        no_endpoint.start()
        self.addCleanup(no_endpoint.stop)
        unsupported = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                   "session_id": self.sid})
        self.assertFalse(unsupported["support"]["supported"])
        self.assertEqual(unsupported["state"], "unknown")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        claude_ref = self._publish_wait(harness="claude", kind="decision", sid="claude_fixture",
                                        stamp=self.now + 1)
        claude = state.read_selected_request(claude_ref)
        self.assertFalse(claude["support"]["supported"])
        self.assertEqual(claude["state"], "unknown")

    def test_opencode_form_without_native_turn_uses_exact_schema_binding(self):
        self.endpoint["generation"] = "a" * 64
        self.api.question(message_id=None)
        selected = state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                                "session_id": self.sid})
        self.assertEqual(selected["request_id"], "frm_1")
        self.assertIsNone(selected["turn_id"])
        self.assertTrue(selected["support"]["supported"])
        self.assertTrue(state._opencode_turnless_form_binding(selected))
        drafted = state.write_selected_draft(selected, "keep this native fallback draft")
        self.assertEqual(drafted["draft"], "keep this native fallback draft")
        result = state.submit_selected_response(
            drafted, {"answers": {"choice": ["red"]}}, action_id="no-turn")
        self.assertEqual(result["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"choice": ["red"]}})])

    def test_codex_control_shape_uses_native_gateway_questions(self):
        sid = "thread_fixture"
        self._publish_wait(harness="codex", kind="decision", sid=sid,
                           request_id="request_9", turn_id="turn_9", item_id="item_9")
        control = {"path": str(Path(self.tmp.name) / "control.sock"), "pid": 77,
                   "proc_start": "770", "epoch": 5,
                   "status": {"status": "ready", "thread_id": sid}}
        ref = {"harness": "codex", "session_id": sid, "request_id": "request_9",
               "turn_id": "turn_9", "item_id": "item_9", "kind": "question",
               "method": "item/tool/requestUserInput", "epoch": 5,
               "binding_generation": 2}
        detail = {"v": 1, "harness": "codex", "session_id": sid,
                  "request_id": "request_9", "turn_id": "turn_9",
                  "kind": "question", "detail": {
                      "threadId": sid, "turnId": "turn_9", "itemId": "item_9",
                      "questions": [{"id": "choice", "header": "Confirm",
                                     "question": "Exact question",
                                     "options": [{"label": "yes", "description": "Yes"}]}]},
                  "state": "open", "support": {"supported": True}, "actions": ["answer"]}
        operations = []

        def gateway_call(_path, payload):
            operations.append(payload)
            if payload["op"] == "interaction-list":
                return {"schema_version": 1, "status": "ready", "requests": [ref]}
            if payload["op"] == "interaction-detail":
                return {"schema_version": 1, "status": "ready", "request": detail}
            if payload["op"] == "interaction-reply":
                return {"schema_version": 1, "status": "applied",
                        "request": ref, "action_id": payload["action_id"]}
            return {"schema_version": 1, "status": "rejected"}

        with mock.patch.object(state, "_codex_control", return_value=control), \
             mock.patch.object(state, "_codex_control_request", side_effect=gateway_call):
            selected = state.read_selected_request({"id": self.item_id, "harness": "codex",
                                                    "session_id": sid, "request_id": "request_9"})
            self.assertEqual(selected["state"], "open")
            result = state.submit_selected_response(
                selected, {"answers": {"choice": ["yes"]}}, action_id="codex-action")
        self.assertEqual(result["state"], "applied")
        reply = operations[-1]
        self.assertEqual(reply["op"], "interaction-reply")
        self.assertEqual(reply["ref"], ref)
        self.assertEqual(reply["response"], {"answers": {"choice": ["yes"]}})
        self.assertEqual(reply["action_id"], "codex-action")

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


class SelectedAnswerIntegrationTest(unittest.TestCase):
    """Exercise shared selected answers through the real managed-gateway socket."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

        path = Path(__file__).with_name("codex_managed_gateway.test.py")
        spec = importlib.util.spec_from_file_location("codex_managed_gateway_integration_fixture", path)
        if spec is None or spec.loader is None:
            raise AssertionError("managed gateway fixture could not be loaded")
        self.gateway_tests = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gateway_tests)
        self.gateway_fixture = self.gateway_tests.ManagedGatewayTest("runTest")
        self.gateway_fixture.setUp()
        self.addCleanup(self.gateway_fixture.doCleanups)
        self.addCleanup(self.gateway_fixture.tearDown)
        self.control = self._control_binding()
        self._codex_control_patch = mock.patch.object(
            state, "_codex_control",
            side_effect=lambda sid: dict(self.control) if sid == "thread-1" else None)
        self._codex_control_patch.start()
        self.addCleanup(self._codex_control_patch.stop)

    def _control_binding(self):
        status = self.gateway_tests.control(self.gateway_fixture.control, {
            "schema_version": 1, "op": "status"})
        return {"path": str(self.gateway_fixture.control), "pid": os.getpid(),
                "proc_start": state._proc_start_ticks(os.getpid()) or "fixture-process",
                "epoch": status["epoch"], "status": status}

    def _wait(self, request_id, *, kind="decision", turn_id="turn-question",
              item_id="item-question", stale=False, stamp=None, append=False):
        stamp = time.time() if stamp is None else stamp
        row_id = f"S1:codex:thread-1:{request_id!s}:{stamp:.6f}"
        item = {"id": row_id, "source": "S1", "kind": kind,
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": stamp,
                "stale": stale, "owner": {"harness": "codex", "session_id": "thread-1"},
                "attribution": "exact", "request_id": request_id,
                "turn_id": turn_id, "item_id": item_id}
        items = []
        if append:
            previous = state._read_cache()
            if previous is not None:
                items.extend(previous["items"])
        items.append(item)
        self.assertTrue(state.publish_cache({"items": items, "complete": True,
                                             "observed_at": time.time(), "health": []}))
        return {"id": row_id, "harness": "codex", "session_id": "thread-1",
                "request_id": request_id, "turn_id": turn_id, "item_id": item_id}

    def _server_request(self, request):
        current = self.gateway_fixture.server.current
        if current is None:
            raise AssertionError("fake App Server has no owning connection")
        current.write_json(request)
        return self.gateway_fixture.client.wait_for(
            lambda item: item.get("id") == request.get("id")
            and item.get("method") == request.get("method"))

    def _wait_native_response(self, request_id, count=1):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.gateway_fixture.server.lock:
                matches = [item for item in self.gateway_fixture.server.approval_responses
                           if item.get("id") == request_id and "method" not in item
                           and ("result" in item or "error" in item)]
            if len(matches) >= count:
                return matches
            time.sleep(0.01)
        raise AssertionError(f"native response {request_id!r} did not arrive")

    def _selected(self, ref):
        selected = state.read_selected_request(ref)
        self.assertIsNotNone(selected.get("native_ref"))
        return selected

    def test_native_observation_to_consumer_resumes_only_exact_reply_once(self):
        fixture = self.gateway_fixture
        cases = [
            ("question", "item/tool/requestUserInput", {"questions": [
                {"id": "choice", "header": "Route", "question": "Private original question",
                 "options": [{"label": "Proceed", "description": "Continue original work"}]}]},
             {"answers": {"choice": ["Proceed"]}},
             {"answers": {"choice": {"answers": ["Proceed"]}}}),
            ("command", "item/commandExecution/requestApproval", {"command": "fixture task"},
             {"decision": "approve"}, {"decision": "accept"}),
            ("reject", "item/commandExecution/requestApproval", {"command": "fixture task"},
             {"decision": "reject"}, {"decision": "decline"}),
            ("file", "item/fileChange/requestApproval", {"reason": "Update private report"},
             {"decision": "approve"}, {"decision": "accept"}),
            ("permissions", "item/permissions/requestApproval", {
                "permissions": {"network": {"enabled": True}, "fileSystem": None},
                "reason": "Fixture network"},
             {"decision": "approve"},
             {"permissions": {"network": {"enabled": True}}, "scope": "turn"}),
        ]
        for name, method, extra, portable, native in cases:
            with self.subTest(path=name):
                request_id = 502 if name == "question" else "original-" + name
                turn_id, item_id = "turn-" + name, "item-" + name
                request = {"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": {"threadId": "thread-1", "turnId": turn_id,
                                      "itemId": item_id, **extra}}
                marker = Path(self.tmp.name) / (name + ".progress.json")
                consumer = fixture.server.expect_interaction_response(request, native, marker)
                # Wrong/missing responses do not produce progress even at the
                # strict upstream, before any Fleet selected-response handling.
                fixture.server._consume_interaction_response(fixture.server.current, {
                    "id": "different-request", "result": native})
                fixture.server.current.write_json({"jsonrpc": "2.0", "method": "turn/started",
                    "params": {"threadId": "thread-1", "turn": {
                        "id": turn_id, "status": "inProgress", "items": []}}})
                fixture.client.wait_for(lambda message: message.get("method") == "turn/started"
                    and message.get("params", {}).get("turn", {}).get("id") == turn_id)
                self._server_request(request)
                observed = observation.read_answer_waits(jobs_paths=[])
                self.assertTrue(state.publish_cache(observed))
                fleet = state.cached_fleet_view(request=False)
                self.assertEqual(fleet["total"], 1)
                row = fleet["items"][0]
                self.assertEqual(row["request_id"], request_id)
                self.assertEqual(row["source"], "S1-question" if name == "question" else "S1-approval")
                selected = self._selected(row)
                selected = state.write_selected_draft(selected, "private original draft")
                self.assertFalse(marker.exists())
                self.assertFalse(consumer["progress"].is_set())
                if name != "question":
                    deferred = state.submit_selected_response(selected, {"decision": "defer"})
                    self.assertEqual(deferred["state"], "open")
                    self.assertEqual(consumer["consumed"], [])
                    self.assertFalse(marker.exists())
                if name == "file":
                    fixture.server.current.write_json({"jsonrpc": "2.0", "method": "item/started",
                        "params": {"threadId": "thread-1", "turnId": turn_id, "item": {
                            "id": item_id, "type": "fileChange", "status": "inProgress",
                            "changes": [{"path": "/repo/report.py", "kind": {"type": "update"},
                                         "diff": "PRIVATE-DIFF"}]}}})
                    fixture.client.wait_for(lambda message: message.get("method") == "item/started"
                        and message.get("params", {}).get("item", {}).get("id") == item_id)
                    selected = self._selected(row)
                result = state.submit_selected_response(selected, portable, action_id="consume-" + name)
                self.assertIn(result["state"], {"submitted", "received", "applied"}, result)
                self.assertTrue(consumer["progress"].wait(5))
                fixture.client.wait_for(lambda message: message.get("method") == "item/completed"
                    and message.get("params", {}).get("item", {}).get("id") == item_id)
                progress = json.loads(marker.read_text())
                self.assertEqual((progress["thread_id"], progress["turn_id"], progress["request_id"]),
                                 ("thread-1", turn_id, request_id))
                self.assertEqual(progress["work_performed"], name != "reject")
                self.assertEqual(consumer["allowed_work"], [] if name == "reject" else ["original-task-work"])
                applied = state.read_selected_request(selected)
                self.assertEqual(applied["state"], "applied")
                duplicate = state.submit_selected_response(selected, portable, action_id="duplicate-" + name)
                self.assertEqual(duplicate["state"], "applied")
                self.assertEqual(len(consumer["consumed"]), 1)
                self.assertEqual(consumer["rejected"], [])
                self.assertEqual(observation.read_answer_waits(jobs_paths=[])["total"], 0)
                fleet_bytes = state._fleet_cache_path().read_text()
                self.assertNotIn("Private original question", fleet_bytes)
                self.assertNotIn("private original draft", fleet_bytes)

    def test_shared_question_answers_all_native_questions_and_tracks_receipt(self):
        fixture = self.gateway_fixture
        native = {
            "jsonrpc": "2.0", "id": 501, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-question",
                        "itemId": "item-question", "questions": [
                            {"id": "language", "header": "Language",
                             "question": "Which language?", "isOther": False,
                             "options": [{"label": "Rust", "description": "Systems"},
                                         {"label": "Python", "description": "Scripts"}]},
                            {"id": "style", "header": "Style", "question": "How concise?",
                             "isOther": True, "options": None}],
                        "isBlocking": True, "autoResolutionMs": None},
        }
        self._server_request(native)
        listing = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-list", "session_id": "thread-1"})
        self.assertNotIn("Which language?", json.dumps(listing))
        ref = self._wait(501)
        selected = self._selected(ref)
        self.assertEqual(selected["state"], "open")
        self.assertEqual(selected["request_id"], 501)
        wrong_turn = dict(selected, turn_id="turn-forged")
        rejected = state.submit_selected_response(
            wrong_turn, {"answers": {"language": ["Rust"], "style": ["Concise"]}},
            action_id="forged-turn")
        self.assertFalse(rejected.get("support", {}).get("supported"))
        self.assertEqual(fixture.gateway._interaction_actions, {})
        self.assertEqual([q["id"] for q in selected["detail"]["questions"]],
                         ["language", "style"])
        self.assertEqual(selected["detail"]["questions"][0]["choices"][0]["value"], "Rust")
        self.assertIsNone(selected.get("question_id"))
        self.assertEqual(state._portable_response(
            selected, {"answers": {"language": ["Rust"]}})[1],
            "nonempty-exact-question-answer-required")

        drafted = state.write_selected_draft(selected, "private two-part answer")
        self.assertEqual(drafted["draft"], "private two-part answer")
        refreshed_ref = self._wait(501, stamp=time.time() + 0.01)
        selected = self._selected(refreshed_ref)
        self.assertEqual(selected["draft"], "private two-part answer")
        projection = state._cache_path().read_text(encoding="utf-8")
        fleet = state._fleet_cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Which language?", projection)
        self.assertIn("private two-part answer", projection)
        self.assertNotIn("Which language?", fleet)
        self.assertNotIn("private two-part answer", fleet)

        result = state.submit_selected_response(
            selected, {"answers": {"language": ["Rust"], "style": ["Concise"]}},
            action_id="integration-question")
        self.assertEqual(result["state"], "submitted")
        native_reply = self._wait_native_response(501)[0]
        self.assertEqual(native_reply["result"], {
            "answers": {"language": {"answers": ["Rust"]},
                        "style": {"answers": ["Concise"]}}})

        fixture.server.emit_resolved("thread-1", 501)
        fixture.client.wait_for(lambda item: item.get("method") == "serverRequest/resolved")
        received = state.read_selected_request(selected)
        self.assertEqual(received["state"], "received")
        self.assertEqual(received["draft"], "private two-part answer")
        self.assertEqual(received["result"]["status"], "received")

    def test_option_only_codex_question_rejects_free_text_then_accepts_native_choice(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 811, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-option-only",
                        "itemId": "item-option-only", "questions": [
                            {"id": "route", "header": "Route", "question": "Choose a route",
                             "isOther": False,
                             "options": [{"label": "Green", "description": "Allowed"},
                                         {"label": "Blue", "description": "Allowed"}]}]}})
        ref = self._wait(811, turn_id="turn-option-only", item_id="item-option-only")
        selected = self._selected(ref)
        drafted = state.write_selected_draft(selected, "free-form route")
        invalid = state.submit_selected_response(
            drafted, {"answers": {"route": ["free-form route"]}},
            action_id="option-only-invalid")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "codex-answer-not-in-current-options")
        self.assertEqual(invalid["draft"], "free-form route")
        self.assertEqual(invalid["actions"], ["reply"])
        self.assertEqual(invalid["native_ref"], selected["native_ref"])
        self.assertNotIn("action_id", invalid)
        self.assertIsNone(state._selected_entry(invalid).get("action_id"))
        self.assertNotIn("option-only-invalid", fixture.gateway._interaction_actions)
        with fixture.server.lock:
            native_responses = [item for item in fixture.server.approval_responses
                                if item.get("id") == 811 and "method" not in item
                                and ("result" in item or "error" in item)]
        self.assertEqual(native_responses, [])

        corrected = state.submit_selected_response(
            invalid, {"answers": {"route": ["Green"]}}, action_id="option-only-corrected")
        self.assertEqual(corrected["state"], "submitted")
        self.assertEqual(self._wait_native_response(811)[0]["result"], {
            "answers": {"route": {"answers": ["Green"]}}})
        self.assertIn("option-only-corrected", fixture.gateway._interaction_actions)

    def test_unavailable_command_approval_stays_unclaimed_then_allowed_reject_works(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 812,
            "method": "item/commandExecution/requestApproval",
            "params": {"kind": "command", "threadId": "thread-1",
                        "turnId": "turn-command-limited", "itemId": "item-command-limited",
                        "startedAtMs": 1, "environmentId": None, "command": "run fixture",
                        "cwd": "/repo", "reason": "Run fixture",
                        "availableDecisions": ["acceptForSession", "decline", "cancel"]}})
        ref = self._wait(812, kind="approval", turn_id="turn-command-limited",
                         item_id="item-command-limited")
        selected = self._selected(ref)
        self.assertEqual(selected["actions"], ["reject", "defer"])
        self.assertNotIn("acceptForSession", selected["actions"])
        self.assertNotIn("applyNetworkPolicyAmendment", selected["actions"])

        invalid = state.submit_selected_response(
            selected, {"decision": "approve"}, action_id="unsupported-command-approve")
        self.assertEqual(invalid["state"], "open")
        self.assertEqual(invalid["reason"], "codex-command-decision-not-available")
        self.assertEqual(invalid["actions"], ["reject", "defer"])
        self.assertEqual(invalid["native_ref"], selected["native_ref"])
        self.assertNotIn("action_id", invalid)
        entry = state._selected_entry(invalid)
        self.assertFalse(entry and entry.get("action_id"))
        self.assertNotIn("unsupported-command-approve", fixture.gateway._interaction_actions)
        with fixture.server.lock:
            native_responses = [item for item in fixture.server.approval_responses
                                if item.get("id") == 812 and "method" not in item
                                and ("result" in item or "error" in item)]
        self.assertEqual(native_responses, [])

        corrected = state.submit_selected_response(
            invalid, {"decision": "reject"}, action_id="command-reject-once")
        self.assertEqual(corrected["state"], "submitted")
        self.assertEqual(self._wait_native_response(812)[0]["result"], {"decision": "decline"})
        self.assertIn("command-reject-once", fixture.gateway._interaction_actions)

    def test_file_approval_uses_actual_native_targets_and_refuses_session_root(self):
        fixture = self.gateway_fixture
        current = fixture.server.current
        self.assertIsNotNone(current)
        current.write_json({"jsonrpc": "2.0", "method": "turn/started", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-file", "status": "inProgress",
                                               "items": []}}})
        fixture.client.wait_for(lambda item: item.get("method") == "turn/started")

        def request(request_id, item_id, grant_root=None):
            self._server_request({"jsonrpc": "2.0", "id": request_id,
                "method": "item/fileChange/requestApproval", "params": {
                    "threadId": "thread-1", "turnId": "turn-file", "itemId": item_id,
                    "startedAtMs": 1, "reason": "Update report", "grantRoot": grant_root}})
            return self._wait(request_id, kind="approval", turn_id="turn-file", item_id=item_id)

        def native_item(item_id):
            current.write_json({"jsonrpc": "2.0", "method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "turn-file", "item": {
                    "id": item_id, "type": "fileChange", "status": "inProgress",
                    "changes": [{"path": "/repo/report.py", "kind": {"type": "update"},
                                 "diff": "PRIVATE-FILE-DIFF-SENTINEL"}]}}})
            fixture.client.wait_for(lambda item: item.get("method") == "item/started"
                                    and item.get("params", {}).get("item", {}).get("id") == item_id)

        ref = request(530, "file-visible")
        missing = self._selected(ref)
        self.assertEqual(missing["actions"], ["reject", "defer"])
        invalid = state.submit_selected_response(missing, {"decision": "approve"},
                                                  action_id="missing-targets")
        self.assertEqual(invalid["state"], "open")
        self.assertIsNone(state._selected_entry(missing))
        native_item("file-visible")
        visible = self._selected(ref)
        self.assertNotEqual(missing["native_ref"]["request"]["binding_generation"],
                            visible["native_ref"]["request"]["binding_generation"])
        stale = state.submit_selected_response(missing, {"decision": "approve"},
                                               action_id="old-target-context")
        self.assertEqual(stale["state"], "unknown")
        self.assertIsNone(state._selected_entry(missing))
        self.assertEqual(visible["detail"]["changes"],
                         [{"path": "/repo/report.py", "kind": {"type": "update"}}])
        self.assertIn("approve", visible["actions"])
        approved = state.submit_selected_response(visible, {"decision": "approve"},
                                                   action_id="visible-file-once")
        self.assertEqual(approved["state"], "submitted")
        self.assertEqual(self._wait_native_response(530)[0]["result"], {"decision": "accept"})
        state.submit_selected_response(visible, {"decision": "approve"},
                                       action_id="visible-file-duplicate")
        self.assertEqual(len(self._wait_native_response(530)), 1)

        native_item("file-session-root")
        root_ref = request(531, "file-session-root", "/repo")
        root_selected = self._selected(root_ref)
        self.assertEqual(root_selected["actions"], ["reject", "defer"])
        invalid = state.submit_selected_response(root_selected, {"decision": "approve"},
                                                 action_id="no-session-grant")
        self.assertEqual(invalid["state"], "open")
        self.assertIsNone(state._selected_entry(root_selected))
        direct = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-reply",
            "ref": root_selected["native_ref"]["request"], "response": {"decision": "approve"},
            "action_id": "no-direct-session-grant"})
        self.assertEqual(direct["status"], "rejected")
        with fixture.server.lock:
            self.assertFalse([item for item in fixture.server.approval_responses
                              if item.get("id") == 531])
        rejected = state.submit_selected_response(root_selected, {"decision": "reject"},
                                                   action_id="decline-session-root")
        self.assertEqual(rejected["state"], "submitted")
        self.assertEqual(self._wait_native_response(531)[0]["result"], {"decision": "decline"})
        status = self.gateway_tests.control(fixture.control, {"schema_version": 1, "op": "status"})
        listing = self.gateway_tests.control(fixture.control, {
            "schema_version": 1, "op": "interaction-list", "session_id": "thread-1"})
        exported = json.dumps([status, listing]) + state._fleet_cache_path().read_text()
        self.assertNotIn("/repo/report.py", exported)
        self.assertNotIn("PRIVATE-FILE-DIFF-SENTINEL", exported)

    def test_shared_approval_uses_gateway_reply_and_applied_evidence(self):
        fixture = self.gateway_fixture
        fixture.server.emit_approval()
        ref = self._wait(900, kind="approval", turn_id="turn-approval",
                         item_id="item-approval")
        selected = self._selected(ref)
        self.assertEqual(selected["kind"], "approval")
        result = state.submit_selected_response(
            selected, {"decision": "approve"}, action_id="integration-approval")
        self.assertEqual(result["state"], "submitted")
        native_reply = self._wait_native_response(900)[0]
        self.assertEqual(native_reply["result"], {"decision": "accept"})

        fixture.server.emit_resolved("thread-1", 900)
        fixture.client.wait_for(lambda item: item.get("method") == "serverRequest/resolved")
        received = state.read_selected_request(selected)
        self.assertEqual(received["state"], "received")
        current = fixture.server.current
        self.assertIsNotNone(current)
        current.write_json({"jsonrpc": "2.0", "method": "item/completed",
                            "params": {"threadId": "thread-1", "turnId": "turn-approval",
                                       "item": {"id": "item-approval",
                                                "type": "commandExecution",
                                                "status": "completed"}}})
        fixture.client.wait_for(lambda item: item.get("method") == "item/completed")
        applied = state.read_selected_request(selected)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["result"]["status"], "applied")
        settled = state._selected_entry(selected)
        stale_result = dict(settled, state="received", result={"status": "received"})
        self.assertTrue(state._store_selected_entry(selected, stale_result))
        preserved = state._selected_entry(selected)
        self.assertEqual(preserved["state"], "applied")
        self.assertEqual(preserved["result"]["status"], "applied")

    def test_typed_request_ids_remain_distinct_on_real_gateway_list_detail(self):
        first = {"jsonrpc": "2.0", "id": 17, "method": "item/tool/requestUserInput",
                 "params": {"threadId": "thread-1", "turnId": "turn-int",
                            "itemId": "item-int", "questions": [
                                {"id": "int-q", "header": "Integer",
                                 "question": "Integer request?", "options": None}]}}
        second = {"jsonrpc": "2.0", "id": "17", "method": "item/tool/requestUserInput",
                  "params": {"threadId": "thread-1", "turnId": "turn-string",
                             "itemId": "item-string", "questions": [
                                 {"id": "str-q", "header": "String",
                                  "question": "String request?", "options": None}]}}
        self._server_request(first)
        self._server_request(second)
        int_ref = self._wait(17, turn_id="turn-int", item_id="item-int")
        string_ref = self._wait("17", turn_id="turn-string", item_id="item-string",
                                append=True)
        selected_int = self._selected(int_ref)
        selected_string = self._selected(string_ref)
        self.assertEqual(selected_int["request_id"], 17)
        self.assertIs(type(selected_int["request_id"]), int)
        self.assertEqual(selected_int["detail"]["question"], "Integer request?")
        self.assertEqual(selected_string["request_id"], "17")
        self.assertEqual(selected_string["detail"]["question"], "String request?")

    def test_concurrent_same_precheck_claims_one_native_reply(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 601, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-race",
                        "itemId": "item-race", "questions": [
                            {"id": "race", "header": "Race", "question": "Choose one",
                             "options": [{"label": "Yes", "description": ""}]}]}})
        ref = self._wait(601, turn_id="turn-race", item_id="item-race")
        selected = self._selected(ref)
        ready_dir = Path(self.tmp.name) / "claim-ready"
        ready_dir.mkdir(mode=0o700)
        control_json = json.dumps(self.control)
        ref_json = json.dumps(ref)
        worker = r'''
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.environ["HEARTING_UTILITIES"])
import human_wait_state as state
binding = json.loads(os.environ["GATEWAY_BINDING"])
state._codex_control = lambda sid: binding if sid == "thread-1" else None
original = state._claim_selected_action
def synchronized_claim(envelope, action_id, response):
    Path(os.environ["CLAIM_READY"]).write_text("ready")
    deadline = time.monotonic() + 20
    while len(list(Path(os.environ["CLAIM_DIR"]).glob("ready-*"))) < 2:
        if time.monotonic() >= deadline:
            raise TimeoutError("both callers did not reach the same-precheck barrier")
        time.sleep(0.01)
    return original(envelope, action_id, response)
state._claim_selected_action = synchronized_claim
selected = state.read_selected_request(json.loads(os.environ["SELECTED_REF"]))
if selected.get("state") != "open":
    raise AssertionError("precheck did not resolve the same open native request")
result = state.submit_selected_response(
    selected, {"answers": {"race": ["Yes"]}}, action_id=os.environ["ACTION_ID"])
print(json.dumps(result))
'''
        env_base = dict(os.environ)
        env_base.update({"HEARTING_UTILITIES": str(Path(__file__).parent),
                         "GATEWAY_BINDING": control_json, "SELECTED_REF": ref_json,
                         "CLAIM_DIR": str(ready_dir),
                         "XDG_STATE_HOME": self.tmp.name,
                         "FLEET_INTERACTION_STATE_DIR": str(fixture.interactions),
                         "PYTHONDONTWRITEBYTECODE": "1"})
        original_reply = fixture.gateway._interaction_reply
        reply_calls = []
        reply_lock = threading.Lock()
        def counted_reply(payload):
            with reply_lock:
                reply_calls.append(payload)
            return original_reply(payload)
        fixture.gateway._interaction_reply = counted_reply
        children = []
        for action_id in ("race-action-a", "race-action-b"):
            child_env = dict(env_base, CLAIM_READY=str(ready_dir / ("ready-" + action_id)),
                             ACTION_ID=action_id)
            children.append(subprocess.Popen(
                [sys.executable, "-B", "-c", worker], env=child_env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outcomes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr)
            outcomes.append(json.loads(stdout.strip().splitlines()[-1]))
        native = self._wait_native_response(601)
        self.assertEqual(len(reply_calls), 1)
        self.assertEqual(len(native), 1)
        self.assertEqual(sum(item.get("state") == "submitted" for item in outcomes), 2)
        state.write_selected_draft(selected, "stale draft after action claim")
        claimed = state._selected_entry(selected)
        self.assertNotEqual(claimed.get("draft"), "stale draft after action claim")
        self.assertTrue(claimed.get("action_id"))

    def test_older_wait_can_reopen_only_while_exact_native_request_is_live(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 701, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-old",
                        "itemId": "item-old", "questions": [
                            {"id": "old", "header": "Old", "question": "Still open?",
                             "options": None}]}})
        old_since = time.time() - 8 * 86400
        ref = self._wait(701, turn_id="turn-old", item_id="item-old",
                         stale=True, stamp=old_since)
        selected = self._selected(ref)
        self.assertEqual(selected["state"], "open")
        drafted = state.write_selected_draft(selected, "keep while still pending")
        refreshed_ref = self._wait(701, turn_id="turn-old", item_id="item-old",
                                   stale=True, stamp=old_since + 1)
        reopened = state.read_selected_request(refreshed_ref)
        self.assertEqual(reopened["state"], "open")
        self.assertEqual(reopened["draft"], "keep while still pending")

        restart_worker = r'''
import json, os, sys
sys.path.insert(0, os.environ["HEARTING_UTILITIES"])
import human_wait_state as state
state._request_producer = lambda: None
binding = json.loads(os.environ["GATEWAY_BINDING"])
state._codex_control = lambda sid: binding if sid == "thread-1" else None
value = state.read_selected_request(json.loads(os.environ["SELECTED_REF"]))
print(json.dumps({"state": value.get("state"), "draft": value.get("draft")}))
'''
        restart_env = dict(os.environ, HEARTING_UTILITIES=str(Path(__file__).parent),
                           GATEWAY_BINDING=json.dumps(self.control),
                           SELECTED_REF=json.dumps(refreshed_ref),
                           XDG_STATE_HOME=self.tmp.name,
                           FLEET_INTERACTION_STATE_DIR=str(fixture.interactions),
                           PYTHONDONTWRITEBYTECODE="1")
        restarted = subprocess.run([sys.executable, "-B", "-c", restart_worker],
                                   env=restart_env, capture_output=True, text=True, timeout=20)
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(json.loads(restarted.stdout.strip()), {
            "state": "open", "draft": "keep while still pending"})
        self.assertEqual(state.cached_count("codex", "thread-1"), (0, 0))

    def test_exact_no_turn_native_detail_stays_read_only_fallback(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": "mcp-no-turn",
            "method": "mcpServer/elicitation/request",
            "params": {"threadId": "thread-1", "turnId": None,
                        "serverName": "fixture", "mode": "url",
                        "message": "Open the sign-in page?",
                        "url": "https://example.invalid/login", "elicitationId": "elicit-1"}})
        ref = self._wait("mcp-no-turn", kind="elicitation", turn_id=None, item_id=None)
        selected = state.read_selected_request(ref)
        self.assertEqual(selected["state"], "open")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "codex-native-turn-binding-unavailable")
        self.assertEqual(selected["detail"]["message"], "Open the sign-in page?")
        drafted = state.write_selected_draft(selected, "preserve this fallback draft")
        self.assertEqual(drafted["draft"], "preserve this fallback draft")
        refused = state.submit_selected_response(
            drafted, {"decision": "approve"}, action_id="no-turn-approval")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(fixture.gateway._interaction_actions, {})

    def test_retired_request_requery_reports_gateway_epoch_expiry(self):
        fixture = self.gateway_fixture
        self._server_request({
            "jsonrpc": "2.0", "id": 801, "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-1", "turnId": "turn-expire",
                        "itemId": "item-expire", "questions": [
                            {"id": "expire", "header": "Expire", "question": "Still live?",
                             "options": None}]}})
        ref = self._wait(801, turn_id="turn-expire", item_id="item-expire")
        selected = self._selected(ref)
        fixture.client.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.gateway_tests.control(fixture.control, {
                "schema_version": 1, "op": "status"})
            if status.get("status") == "disconnected":
                break
            time.sleep(0.01)
        self.assertEqual(status.get("status"), "disconnected")
        fixture.client = fixture._connect_client()
        self.control = self._control_binding()
        expired = state.read_selected_request(selected)
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["support"]["reason"], "codex-native-request-expired")


class WorkflowSelectedResponseTest(unittest.TestCase):
    """Exercise selected S2/S3/S4 records against isolated workflow sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        agent_home = self.root / "agent-home"
        (agent_home / "core").mkdir(parents=True)
        (agent_home / "core" / "CORE.md").write_text("fixture\n", encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "xdg"),
            "HOME": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_HOME": str(agent_home),
            "AGENT_WORKFLOW_ROOT": str(self.root / "workflow"),
            "AGENT_ARTIFACT_CHECKPOINT": "off",
            "AGENT_ARTIFACT_CAMPAIGN_ID": "",
            "AGENT_ARTIFACT_CYCLE_ID": "",
            "AGENT_ARTIFACT_PRODUCER_ID": "",
            "AGENT_ARTIFACT_CYCLE_DIR": "",
            "AGENT_ARTIFACT_OUTPUT_DIR": "",
            "AGENT_DISPATCH_CALLER_HARNESS": "codex",
            "AGENT_DISPATCH_REGISTERED_WORKER": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_DEPTH": "0",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "CODEX_THREAD_ID": "parent-session",
            "CODEX_SESSION_ID": "",
            "CLAUDE_CODE_SESSION_ID": "",
            "CLAUDE_SESSION_ID": "",
            "OPENCODE_SESSION_ID": "",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)

        # These cases exercise the isolated S2/S3 workflow, not the host's
        # unrelated live Codex processes or its PID namespace visibility.
        rollouts = mock.patch.object(observation, "_codex_rollout_waits",
                                     return_value={"complete": True, "pending": {}})
        rollouts.start()
        self.addCleanup(rollouts.stop)

        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import dispatch
        self.dispatch = dispatch
        self.jobs = self.root / "jobs.log"
        self.jobs.write_text("", encoding="utf-8")
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        registries = mock.patch.object(dispatch, "human_gate_registry_paths",
                                       return_value=(self.jobs,))
        registries.start()
        self.addCleanup(registries.stop)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.route_file = self.root / "owner.route.json"
        self.route = {"artifact_root": str(self.artifacts), "nodes": []}
        self._seal_route()

    def _seal_route(self):
        from route_identity import route_hash, route_id_from_hash
        self.route.pop("route_hash", None)
        self.route.pop("route_id", None)
        self.route["route_hash"] = route_hash(self.route)
        self.route["route_id"] = route_id_from_hash(self.route["route_hash"])
        self.route_file.write_text(json.dumps(self.route), encoding="utf-8")

    def _metadata(self, attempt="att-workflow-owner"):
        from dispatch_contract import supervisor_lease_path
        lease = supervisor_lease_path(self.jobs, attempt)
        return ",".join((
            "attempt_schema_version=2", "dispatch_depth=1", "worker_type=owner",
            "transport=headless", "execution_surface=registered-headless",
            "registered_worker=1", "completion_delivery=app-server-supervised",
            "harness=codex", "attempt_id=" + attempt,
            "supervisor_lease=flock-v1", "supervisor_lease_nonce=" + "d" * 64,
            "supervisor_lease_file=" + str(lease), "parent_sid=parent-session",
            "parent_harness=codex", "route_id=" + self.route["route_id"],
            "route_hash=" + self.route["route_hash"], "route_file=" + str(self.route_file),
            "owner_route_id=" + self.route["route_id"],
            "owner_route_hash=" + self.route["route_hash"],
            "owner_route_file=" + str(self.route_file),
        ))

    def _write_owner(self, status, *, attempt="att-workflow-owner", suffix=""):
        row = (f"{time.time():.6f}\t{status}\t/repo\t/wt\towner\t"
               + self._metadata(attempt) + suffix + "\n")
        self.jobs.write_text(row, encoding="utf-8")

    def _observe(self):
        import human_gate_observation
        result = human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))
        self.assertTrue(result["complete"], result["health"])
        self.assertTrue(state.publish_cache(result))
        return result

    def _blocked_owner_wait(self):
        import dispatch_owner_input
        attempt = "att-workflow-owner"
        self._write_owner("open", attempt=attempt)
        dispatch_owner_input.initialize_owner_input(self.jobs, attempt, "codex-next-turn")
        suffix = (",note=dead-worker-blocked,failure_class=blocked")
        self._write_owner("done", attempt=attempt, suffix=suffix)
        result = self._observe()
        item = next(row for row in result["items"] if row["source"] == "S3")
        return item

    def _human_gate_wait(self):
        self.route["human_gate_bindings"] = [{"gate": "frame-review", "node": "owner"}]
        self._seal_route()
        self._write_owner("open", suffix="")
        import workflow_state
        ledger = workflow_state.WorkflowLedger(self.route["route_id"],
                                                self.route["route_hash"], jobs=self.jobs)
        ledger.root.mkdir(parents=True, exist_ok=True)
        ledger.journal_path.write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "workflow_state": "BLOCKED_HUMAN_GATE",
            "evidence": {"gate": "frame-review"},
        }) + "\n", encoding="utf-8")
        result = self._observe()
        item = next(row for row in result["items"] if row["source"] == "S2")
        return item, ledger.journal_path

    def test_s3_explicit_correction_uses_public_owner_input_once(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["state"], "open")
        self.assertTrue(selected["support"]["supported"])
        self.assertEqual(selected["native_ref"]["transport"], "hearting-owner-input")
        self.assertIsNone(selected["turn_id"])
        self.assertEqual(selected["item_id"], "att-workflow-owner")
        self.assertEqual(selected["route_id"], self.route["route_id"])
        self.assertEqual(state._read_cache()["items"][0]["route_hash"],
                         self.route["route_hash"])

        drafted = state.write_selected_draft(selected, "preserve the approved route and retry")
        self.assertEqual(drafted["draft"], "preserve the approved route and retry")
        empty = state.submit_selected_response(
            drafted, {"answers": {"owner-correction": [""]}}, action_id="empty-correction")
        self.assertEqual(empty["state"], "open")
        self.assertEqual(dispatch_owner_input.inspect(
            self.jobs, "att-workflow-owner")["requests"], [])

        original = state._claim_selected_action
        barrier = threading.Barrier(2)

        def simultaneous_claim(*args):
            barrier.wait(timeout=5)
            return original(*args)

        with mock.patch.object(state, "_claim_selected_action", side_effect=simultaneous_claim), \
             mock.patch.object(state.subprocess, "run",
                               wraps=state.subprocess.run) as public_correct:
            results = []
            failures = []

            def send(action_id):
                try:
                    results.append(state.submit_selected_response(
                        drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
                        action_id=action_id))
                except Exception as exc:
                    failures.append(exc)

            callers = [threading.Thread(target=send, args=(action,))
                       for action in ("owner-action-a", "owner-action-b")]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(timeout=20)
            self.assertTrue(all(not caller.is_alive() for caller in callers))
            self.assertEqual(failures, [])
            self.assertEqual(public_correct.call_count, 1)

        receipt = dispatch_owner_input.inspect(self.jobs, "att-workflow-owner")
        self.assertEqual(len(receipt["requests"]), 1)
        request = receipt["requests"][0]
        self.assertEqual(request["state"], "retained")
        self.assertNotIn(drafted["draft"], json.dumps(receipt))
        self.assertEqual(dispatch_owner_input.retained(self.jobs, "att-workflow-owner")[0]["text"],
                         drafted["draft"])
        current = state.read_selected_request(selected)
        self.assertEqual(current["state"], "received")
        self.assertEqual(current["result"]["status"], "retained")
        self.assertEqual(self._observe()["total"], 0)
        self.assertNotIn(drafted["draft"], state._fleet_cache_path().read_text(encoding="utf-8"))

    def test_s3_consumed_receipt_preserves_its_actual_source_and_request(self):
        import dispatch_owner_input
        import dispatch_replacement

        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        marker = self.root / "original-owner-next-step.json"
        consumed = set()
        proof = {"source_result": "BLOCKED", "corrections": []}
        before = dispatch_replacement._correction_context(self.jobs, item["attempt_id"], proof)
        self.assertNotIn('"id": "owner-consumed"', before)
        self.assertFalse(marker.exists())
        received = state.submit_selected_response(
            selected, {"answers": {"owner-correction": ["continue the approved work"]}},
            action_id="owner-consumed")
        self.assertEqual(received["state"], "received")
        with mock.patch.object(dispatch_owner_input.OwnerInput, "notify_unresolved"):
            consumer = dispatch_owner_input.OwnerInput(
                self.jobs, item["attempt_id"], "actual-owner-thread", "codex-next-turn", lambda _: None)
        retained = dispatch_owner_input.retained(self.jobs, item["attempt_id"])
        proof["corrections"] = [{"id": row["id"], "digest": row["digest"]} for row in retained]
        prompt = dispatch_replacement._correction_context(self.jobs, item["attempt_id"], proof)

        def continue_original_owner(native_prompt):
            # The normal replacement prompt reader feeds a controlled native
            # consumer. Completion is conditional on the exact retained answer,
            # never on the selected transport's receipt alone.
            for line in native_prompt.splitlines():
                if not line.startswith('{"id":'):
                    continue
                answer = json.loads(line)
                if answer != {"id": "owner-consumed", "text": "continue the approved work"}:
                    continue
                if answer["id"] in consumed:
                    continue
                consumer.record([answer["id"]], "accepted", turn_id="actual-owner-turn")
                marker.write_text(json.dumps({"route_id": self.route["route_id"],
                    "thread_id": consumer.thread_id, "next_step": "original-owner-continued"}),
                    encoding="utf-8")
                consumed.add(answer["id"])
                consumer.completed("actual-owner-turn")

        continue_original_owner(before)
        self.assertFalse(marker.exists())
        continue_original_owner(prompt)
        continue_original_owner(prompt)
        self.assertEqual(consumed, {"owner-consumed"})
        self.assertEqual(json.loads(marker.read_text())["route_id"], self.route["route_id"])
        self.assertEqual(dispatch_owner_input.retained(self.jobs, item["attempt_id"]), [])
        applied = state.read_selected_request(selected)
        self.assertEqual(applied["state"], "applied")
        self.assertEqual(applied["result"], {
            "source": "hearting-owner-input", "status": "turn-completed",
            "request_id": "owner-consumed", "delivery_observation": "turn-completed",
            "target_attempt_id": item["attempt_id"],
            "continuation": received["result"]["continuation"]})
        self.assertEqual(applied["draft"], "")

    def test_s3_replaced_owner_route_or_parent_cannot_receive_old_answer(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        selected = state.read_selected_request({"id": item["id"]})
        drafted = state.write_selected_draft(selected, "do not retarget this correction")
        changed = self.jobs.read_text(encoding="utf-8").replace(
            "parent_sid=parent-session", "parent_sid=replacement-session")
        self.jobs.write_text(changed, encoding="utf-8")
        refused = state.submit_selected_response(
            drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
            action_id="stale-owner-action")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(refused["state"], "unknown")
        with self.assertRaisesRegex(dispatch_owner_input.InputError, "target-changed"):
            dispatch_owner_input.inspect(self.jobs, "att-workflow-owner")
        value = json.loads(dispatch_owner_input._path(
            self.jobs, "att-workflow-owner").read_text(encoding="utf-8"))
        self.assertEqual(value["requests"], [])

    def test_s2_detail_is_exact_and_release_stays_disabled_without_raise_binding(self):
        item, journal = self._human_gate_wait()
        before = journal.read_bytes()
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["route_id"], self.route["route_id"])
        self.assertEqual(selected["native_ref"]["transport"], "hearting-human-gate")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "workflow-gate-artifact-unavailable")
        refused = state.submit_selected_response(
            selected, {"answers": {"frame-review": ["proceed"]}},
            action_id="must-not-release")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(journal.read_bytes(), before)

    def _bound_human_gate_wait(self):
        import workflow_state

        self.route["nodes"] = [
            {"id": "owner", "kind": "resource-runner", "depends_on": [],
             "continuation": {"kind": "human-gate", "gate": "frame-review"}},
            {"id": "execute", "kind": "review-worker", "depends_on": ["owner"]},
        ]
        self.route["human_gate_bindings"] = [{"gate": "frame-review", "node": "execute"}]
        self._seal_route()
        self._write_owner("open")
        artifact = self.artifacts / "review.md"
        artifact.write_text("Review the original fixture task", encoding="utf-8")
        ledger = workflow_state.WorkflowLedger(self.route["route_id"],
                                                self.route["route_hash"], jobs=self.jobs)
        ledger.set_workflow_state("BLOCKED_HUMAN_GATE", evidence={
            "gate": "frame-review", "artifact": str(artifact),
            "release_authority": "depth-0"}, actor="fixture-original-request")
        item = next(row for row in self._observe()["items"] if row["source"] == "S2")
        return state.read_selected_request({"id": item["id"]}), ledger

    def test_s2_exact_person_reply_releases_once_and_successor_consumes_original_route(self):
        selected, ledger = self._bound_human_gate_wait()
        self.assertTrue(selected["support"]["supported"], selected)
        marker = self.root / "same-route-next-step.json"
        consumed = set()

        def run_available_successors():
            for key, claim in ledger.claims().items():
                self.assertEqual(claim["route_id"], self.route["route_id"])
                self.assertEqual(claim["successor"], "execute")
                if key not in consumed:
                    marker.write_text(json.dumps({"route_id": claim["route_id"],
                                                   "next_node": "execute"}), encoding="utf-8")
                    consumed.add(key)

        run_available_successors()
        self.assertFalse(marker.exists())
        self.assertEqual(ledger.claims(), {})
        empty = state.submit_selected_response(
            selected, {"answers": {"gate-decision": [""]}}, action_id="empty-gate")
        self.assertEqual(empty["state"], "open")
        run_available_successors()
        self.assertFalse(marker.exists())
        applied = state.submit_selected_response(
            selected, {"answers": {"gate-decision": ["proceed"]}}, action_id="gate-consumed")
        self.assertEqual(applied["state"], "applied", applied)
        self.assertEqual(applied["result"]["actor_kind"], "user")
        run_available_successors()
        self.assertEqual(json.loads(marker.read_text()), {
            "route_id": self.route["route_id"], "next_node": "execute"})
        self.assertEqual(len(consumed), 1)
        duplicate = state.submit_selected_response(
            selected, {"answers": {"gate-decision": ["proceed"]}}, action_id="gate-duplicate")
        self.assertEqual(duplicate["state"], "applied")
        run_available_successors()
        self.assertEqual(len(consumed), 1)
        releases = [row for row in ledger.journal()
                    if (row.get("evidence") or {}).get("selected_request_id") == "gate-consumed"
                    and row.get("workflow_state") == "RUNNING"]
        self.assertEqual(len(releases), 1)
        self.assertEqual(self._observe()["total"], 0)

    def test_s2_replaced_epoch_or_artifact_preserves_draft_without_release(self):
        selected, ledger = self._bound_human_gate_wait()
        selected = state.write_selected_draft(selected, "private gate draft")
        artifact = self.artifacts / "review.md"
        artifact.write_text("Changed review target", encoding="utf-8")
        refused = state.submit_selected_response(
            selected, {"answers": {"gate-decision": ["proceed"]}}, action_id="stale-gate")
        self.assertFalse(refused["support"]["supported"])
        self.assertEqual(refused["draft"], "private gate draft")
        self.assertEqual(ledger.claims(), {})
        self.assertEqual(state._selected_entry(selected)["draft"], "private gate draft")

    def test_s4_marker_detail_and_draft_never_fabricate_a_reply(self):
        import human_gate_observation
        text = "[답변 필요] select the next step"
        self.assertTrue(state.open_signal("codex", "marker-session", "marker-seat", text,
                                           sequence=7))
        result = human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))
        self.assertTrue(state.publish_cache(result))
        item = next(row for row in result["items"] if row["source"] == "S4")
        selected = state.read_selected_request({"id": item["id"]})
        self.assertEqual(selected["detail"]["question"], "select the next step")
        self.assertIsNone(selected["turn_id"])
        self.assertFalse(selected["support"]["supported"])
        self.assertEqual(selected["support"]["reason"],
                         "marker-has-no-bound-native-response-surface")
        drafted = state.write_selected_draft(selected, "keep this private for a native reply")
        refused = state.submit_selected_response(
            drafted, {"answers": {"marker-response": [drafted["draft"]]}},
            action_id="marker-reply")
        self.assertFalse(refused["support"]["supported"])
        self.assertTrue(state.signal_is_current({"id": item["id"], "seat_key": "marker-seat"}))
        self.assertNotIn("select the next step",
                         state._cache_path().read_text(encoding="utf-8"))
        self.assertNotIn("select the next step",
                         state._fleet_cache_path().read_text(encoding="utf-8"))

    def _questions_cli(self, *argv):
        import contextlib
        import io
        import questions
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = questions.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_questions_cli_lists_then_releases_s2_gate_through_the_existing_release(self):
        selected, ledger = self._bound_human_gate_wait()
        item_id = selected["selected_id"]
        code, text, _err = self._questions_cli()
        self.assertEqual(code, 0)
        self.assertIn("questions: 1 current · 0 older", text)
        self.assertIn(item_id + "  human gate", text)
        self.assertIn("1) proceed", text)
        self.assertIn("answer: hearting run questions answer " + item_id, text)
        code, listed, _err = self._questions_cli("list", "--json")
        board = json.loads(listed)
        self.assertEqual(board["counts"], {"total": 1, "older": 0, "ownerless": 0})
        self.assertEqual(board["items"][0]["questions"][0]["id"], "gate-decision")
        self.assertTrue(board["items"][0]["answerable"])

        code, _out, err = self._questions_cli("answer", item_id, "--choice", "9")
        self.assertEqual(code, 1)
        self.assertIn("outside 1..3", err)
        # A prefix could name a re-raised gate the person never saw: only the full id.
        code, _out, err = self._questions_cli("answer", item_id.rsplit(":", 1)[0], "--choice", "1")
        self.assertEqual(code, 2)
        self.assertIn("exactly this id", err)
        self.assertEqual(ledger.claims(), {})
        code, out, err = self._questions_cli("answer", item_id, "--choice", "1", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "applied")
        self.assertTrue(ledger.claims())
        releases = [row for row in ledger.journal()
                    if row.get("workflow_state") == "RUNNING"
                    and (row.get("evidence") or {}).get("selected_request_id")]
        self.assertEqual(len(releases), 1)
        self.assertEqual(self._observe()["total"], 0)

    def test_questions_cli_sends_s3_correction_through_owner_input(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        code, text, _err = self._questions_cli("list")
        self.assertIn(item["id"] + "  blocked owner", text)
        code, out, err = self._questions_cli("answer", item["id"], "--text",
                                             "keep the approved route and retry", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "received")
        retained = dispatch_owner_input.retained(self.jobs, "att-workflow-owner")
        self.assertEqual(retained[0]["text"], "keep the approved route and retry")

    def test_questions_cli_reports_a_reply_claimed_first_elsewhere_as_not_its_own(self):
        import dispatch_owner_input
        item = self._blocked_owner_wait()
        original = state._claim_selected_action

        def fleet_claims_first(current, action_id, response):
            original(current, "fleet-first", {"answers": {"owner-correction": ["fleet reply"]}})
            return original(current, action_id, response)

        with mock.patch.object(state, "_claim_selected_action", side_effect=fleet_claims_first):
            code, out, _err = self._questions_cli("answer", item["id"], "--text", "cli reply",
                                                  "--json")
        self.assertEqual(code, 1)
        reported = json.loads(out)
        self.assertEqual(reported["state"], "answered-elsewhere")
        self.assertIn("another caller", reported["reason"])
        self.assertEqual(dispatch_owner_input.inspect(self.jobs, "att-workflow-owner")["requests"],
                         [])

    def test_questions_cli_leaves_a_marker_without_reply_surface_untouched(self):
        import human_gate_observation
        self.assertTrue(state.open_signal("codex", "marker-session", "marker-seat",
                                           "[답변 필요] select the next step", sequence=7))
        self.assertTrue(state.publish_cache(
            human_gate_observation.read_answer_waits(jobs_paths=(self.jobs,))))
        code, text, _err = self._questions_cli()
        self.assertIn("select the next step", text)
        self.assertIn("answer in that session", text)
        item_id = next(row["id"] for row in state._read_cache()["items"] if row["source"] == "S4")
        code, _out, err = self._questions_cli("answer", item_id, "--text", "next")
        self.assertEqual(code, 2)
        self.assertIn("cannot be answered here", err)
        self.assertTrue(state.signal_is_current({"id": item_id, "seat_key": "marker-seat"}))


class S3EffectiveParentTest(unittest.TestCase):
    """Keep S3's original request binding while following confirmed native handover."""

    setUp = WorkflowSelectedResponseTest.setUp
    _seal_route = WorkflowSelectedResponseTest._seal_route
    _metadata = WorkflowSelectedResponseTest._metadata
    _write_owner = WorkflowSelectedResponseTest._write_owner
    _observe = WorkflowSelectedResponseTest._observe
    _blocked_owner_wait = WorkflowSelectedResponseTest._blocked_owner_wait

    def _native_caller(self, harness, session_id, extra=None):
        session_vars = {
            "claude": "CLAUDE_CODE_SESSION_ID",
            "codex": "CODEX_THREAD_ID",
            "opencode": "OPENCODE_SESSION_ID",
        }
        values = {
            "AGENT_DISPATCH_CALLER_HARNESS": harness,
            "AGENT_DISPATCH_CURRENT_HARNESS": "",
            "CLAUDE_CODE_SESSION_ID": "", "CLAUDE_SESSION_ID": "",
            "CODEX_THREAD_ID": "", "CODEX_SESSION_ID": "",
            "OPENCODE_SESSION_ID": "",
            "AGENT_DISPATCH_REGISTERED_WORKER": "",
            "AGENT_DISPATCH_CHILD": "",
            "AGENT_DISPATCH_ATTEMPT_ID": "",
            "AGENT_DISPATCH_DEPTH": "0",
            "AGENT_DISPATCH_PARENT_SESSION_ID": "",
            "HERDR_PANE_ID": "",
        }
        values[session_vars[harness]] = session_id
        values.update(extra or {})
        return mock.patch.dict(os.environ, values, clear=False)

    def _record_handover(self, successor, successor_harness, seat_key):
        import dispatch_seat_handover
        import session_tidy

        return dispatch_seat_handover.record_retire_handover(
            "parent-session", "codex", successor, successor_harness,
            jobs=self.jobs, now=time.time(),
            successor_seat=session_tidy.Seat("pane", seat_key, "fixture:pane"))

    def _open_pending_delivery(self, item):
        import dispatch_pending_delivery

        attempt = item["attempt_id"]
        receipt = {
            "schema_version": 2, "state": "delivered",
            "parent_attempt_id": attempt, "job_registry": str(self.jobs),
            "delivery_classification": "attention",
            "children": [{"attempt_id": attempt, "status": "done", "readiness": "ready",
                          "reason": "terminal-failure-or-unclosed",
                          "required_action": "inspect-done-failure", "harness": "codex",
                          "delivery_classification": "attention"}],
        }
        dispatch_pending_delivery.create(
            self.jobs.parent, recipient_kind="codex-native-queue",
            recipient_key="parent-session", delivery_id="delivery-effective-parent-fixture",
            session_generation="", session_generation_supported="0", attempt_ids=[attempt],
            parent_attempt_id=attempt, route_id=item["route_id"], route_node="owner",
            receipt=receipt,
            receipt_digest=dispatch_pending_delivery._canonical_receipt_digest(receipt),
            row_revisions={attempt: item["route_hash"]})

    def test_confirmed_cross_harness_successor_keeps_binding_and_corrects_once(self):
        import dispatch_completion_join
        import dispatch_owner_input
        import dispatch_seat_handover

        item = self._blocked_owner_wait()
        jobs_before = self.jobs.read_bytes()
        original = state.read_selected_request({"id": item["id"]})
        self.assertTrue(original["support"]["supported"])
        original_draft = state.write_selected_draft(original, "parent's private draft")
        original_ref = original["native_ref"]
        metadata = dispatch_completion_join.exact_attempt_row(
            self.jobs, item["attempt_id"]).metadata
        self.assertEqual(original_ref["owner_binding"], state._owner_binding(metadata))

        self._open_pending_delivery(item)
        with mock.patch.object(dispatch_seat_handover, "pane_seat", return_value=None):
            handover = self._record_handover("successor-session", "claude", "effective-seat")
            self.assertIsNotNone(handover)
            self.assertEqual(dispatch_seat_handover.effective_parent(metadata, self.jobs),
                             "successor-session")
            self.assertEqual(dispatch_seat_handover.effective_parent_harness(metadata, self.jobs),
                             "claude")

            observed = self._observe()
            current_item = next(row for row in observed["items"] if row["id"] == item["id"])
            self.assertEqual(current_item["owner"], {
                "harness": "claude", "session_id": "successor-session", "seat_key": None})
            self.assertEqual(self.jobs.read_bytes(), jobs_before)

            with mock.patch.object(state.subprocess, "run", wraps=state.subprocess.run) as correct:
                late_parent = state.submit_selected_response(
                    original_draft,
                    {"answers": {"owner-correction": [original_draft["draft"]]}},
                    action_id="late-parent-answer")
                self.assertFalse(late_parent["support"]["supported"])
                self.assertEqual(correct.call_count, 0)
                self.assertEqual(dispatch_owner_input.inspect(
                    self.jobs, item["attempt_id"])["requests"], [])

                with self._native_caller("claude", "successor-session"):
                    selected = state.read_selected_request({"id": item["id"]})
                    self.assertTrue(selected["support"]["supported"], selected["support"])
                    self.assertEqual(selected["native_ref"], original_ref)
                    self.assertEqual(selected["native_ref"]["source_id"], item["id"])
                    self.assertEqual(selected["native_ref"]["attempt_id"], item["attempt_id"])
                    self.assertEqual(selected["native_ref"]["route_hash"], item["route_hash"])
                    self.assertEqual(selected["item_id"], item["attempt_id"])
                    self.assertEqual(selected["route_id"], item["route_id"])
                    self.assertEqual(selected["request_id"], item["id"])
                    drafted = state.write_selected_draft(
                        selected, "successor correction for the original request")
                    received = state.submit_selected_response(
                        drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
                        action_id="successor-owner-answer")
                    self.assertEqual(received["state"], "received")
                    duplicate = state.submit_selected_response(
                        drafted, {"answers": {"owner-correction": [drafted["draft"]]}},
                        action_id="successor-owner-answer")
                    self.assertEqual(duplicate["state"], "received")

                correct_calls = [call for call in correct.call_args_list
                                 if "correct" in call.args[0]]
                self.assertEqual(len(correct_calls), 1)

        receipt = dispatch_owner_input.inspect(self.jobs, item["attempt_id"])
        self.assertEqual(len(receipt["requests"]), 1)
        self.assertEqual(receipt["requests"][0]["id"], "successor-owner-answer")
        self.assertNotIn("successor correction for the original request", json.dumps(receipt))
        self.assertEqual(dispatch_completion_join.exact_attempt_row(
            self.jobs, item["attempt_id"]).metadata["parent_sid"], "parent-session")
        self.assertEqual(original_ref["owner_binding"], state._owner_binding(
            dispatch_completion_join.exact_attempt_row(
                self.jobs, item["attempt_id"]).metadata))

    def test_unconfirmed_forked_different_route_and_worker_contexts_are_refused(self):
        import dispatch_completion_join
        import dispatch_seat_handover

        item = self._blocked_owner_wait()
        metadata = dispatch_completion_join.exact_attempt_row(
            self.jobs, item["attempt_id"]).metadata

        with mock.patch.object(dispatch_seat_handover, "pane_seat", return_value=None):
            # Native identity plus a parent alias and pane hint do not create lineage.
            with self._native_caller("claude", "successor-session", {
                    "AGENT_DISPATCH_PARENT_SESSION_ID": "successor-session",
                    "HERDR_PANE_ID": "unconfirmed-seat"}):
                unconfirmed = state.read_selected_request({"id": item["id"]})
                self.assertFalse(unconfirmed["support"]["supported"])
                self.assertEqual(dispatch_seat_handover.effective_parent(metadata, self.jobs),
                                 "parent-session")

            self._open_pending_delivery(item)
            handover = self._record_handover("successor-session", "claude", "effective-seat")
            self.assertIsNotNone(handover)
            # The old parent cannot fork the same exact route to another seat.
            fork = self._record_handover("fork-session", "opencode", "fork-seat")
            self.assertIsNone(fork)
            self.assertEqual(dispatch_seat_handover.effective_parent(metadata, self.jobs),
                             "successor-session")

            with self._native_caller("opencode", "fork-session"):
                forked = state.read_selected_request({"id": item["id"]})
                self.assertFalse(forked["support"]["supported"])
            with self._native_caller("opencode", "unrelated-session"):
                unrelated = state.read_selected_request({"id": item["id"]})
                self.assertFalse(unrelated["support"]["supported"])
            with self._native_caller("claude", "successor-session", {
                    "AGENT_DISPATCH_REGISTERED_WORKER": "1"}):
                worker = state.read_selected_request({"id": item["id"]})
                self.assertFalse(worker["support"]["supported"])
            with self._native_caller("claude", "successor-session"):
                wrong_route = state.read_selected_request({
                    "id": item["id"], "route_id": "different-route"})
                self.assertFalse(wrong_route["support"]["supported"])

            changed_route = dict(metadata)
            changed_route.update({"route_id": "different-route",
                                  "owner_route_id": "different-route",
                                  "route_hash": "f" * 64,
                                  "owner_route_hash": "f" * 64})
            self.assertEqual(dispatch_seat_handover.effective_parent(
                changed_route, self.jobs), "parent-session")


class OpenCodeCurrentFormSchemaTest(unittest.TestCase):
    """Focused fixtures for current OpenCode V2 form identity and typed answers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sid = "ses_fixture"
        self.now = time.time()
        self.real_opencode_endpoint = state._opencode_endpoint
        self.endpoint = {"url": "http://127.0.0.1:4096", "generation": "a" * 64,
                         "pid": 1234, "proc_start": "123", "socket_inodes": ["700"],
                         "server_pid": 1234, "server_proc_start": "123",
                         "server_urls": ["http://127.0.0.1:4096"],
                         "frontend_pid": 1234, "frontend_proc_start": "123"}
        self.api = _StrictOpenCodeTransport(self.sid)
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "FLEET_INTERACTION_STATE_DIR": str(Path(self.tmp.name) / "interactions"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        demand = mock.patch.object(state, "_request_producer")
        demand.start()
        self.addCleanup(demand.stop)
        endpoint = mock.patch.object(
            state, "_opencode_endpoint",
            side_effect=lambda sid: dict(self.endpoint) if sid == self.sid else None)
        endpoint.start()
        self.addCleanup(endpoint.stop)
        transport = mock.patch.object(state, "_opencode_http", side_effect=self.api)
        transport.start()
        self.addCleanup(transport.stop)
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        self._publish_wait()

    def _publish_wait(self, *, stamp=None, sid=None):
        sid = sid or self.sid
        stamp = self.now if stamp is None else stamp
        self.item_id = f"S1:opencode:{sid}:{int(stamp * 1000)}"
        item = {"id": self.item_id, "source": "S1", "kind": "decision",
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": stamp,
                "stale": False, "owner": {"harness": "opencode", "session_id": sid},
                "attribution": "exact"}
        self.assertTrue(state.publish_cache({"items": [item], "complete": True,
                                             "observed_at": time.time(), "health": []}))

    def _form(self, fields, *, request_id="frm_1", message_id=None, title="Question title"):
        metadata = {} if message_id is None else {"messageID": message_id}
        value = {"id": request_id, "sessionID": self.sid, "title": title,
                 "metadata": metadata, "fields": fields, "state": {"status": "pending"}}
        self.api.forms[request_id] = value
        if request_id not in self.api.form_ids:
            self.api.form_ids.append(request_id)
        return value

    def _selected(self):
        return state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                             "session_id": self.sid})

    def test_endpoint_requires_exact_current_tui_selection(self):
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import opencode as collector

        proc_path = Path("/proc")
        real_iterdir = Path.iterdir

        def fake_iterdir(path):
            if path == proc_path:
                return iter([Path("/proc/4242")])
            return real_iterdir(path)

        def resolve(observed, source):
            with mock.patch.object(Path, "iterdir", fake_iterdir), \
                 mock.patch.object(state, "_proc_uid", return_value=os.getuid()), \
                 mock.patch.object(state, "_proc_start_ticks", return_value=424242), \
                 mock.patch.object(state, "_proc_argv", return_value=["opencode"]), \
                 mock.patch.object(state, "_explicit_opencode_server",
                                   return_value=self.endpoint["url"]), \
                 mock.patch.object(state, "_proc_listening_sockets",
                                   return_value=(4096, "127.0.0.1", ("700",))), \
                 mock.patch.object(collector, "session_of_process",
                                   return_value=(observed, source)):
                return self.real_opencode_endpoint(self.sid)

        self.assertIsNone(resolve(self.sid, "opencode-argv"))
        self.assertIsNone(resolve("ses_switched", "opencode-tui-selection"))
        current = resolve(self.sid, "opencode-tui-selection")
        self.assertEqual(current["frontend_pid"], 4242)
        self.assertEqual(current["server_pid"], 1234)
        self.assertEqual(current["url"], self.endpoint["url"])

    def test_multiple_keyed_fields_emit_native_typed_answer_and_allow_missing_turn(self):
        self._form([
            {"type": "string", "key": "name", "title": "Name", "required": True,
             "minLength": 2},
            {"type": "number", "key": "amount", "title": "Amount", "required": True,
             "minimum": 0, "maximum": 5},
            {"type": "integer", "key": "count", "title": "Count", "required": True,
             "minimum": 1, "maximum": 4},
            {"type": "boolean", "key": "confirm", "title": "Confirm", "required": True},
            {"type": "multiselect", "key": "colors", "title": "Colors", "required": True,
             "options": [{"value": "r", "label": "Red"},
                         {"value": "b", "label": "Blue"}],
             "minItems": 1, "maxItems": 2},
            {"type": "string", "key": "optional", "title": "Optional", "hidden": True,
             "default": "must-not-be-copied"},
        ])
        selected = self._selected()
        self.assertTrue(selected["support"]["supported"])
        self.assertIsNone(selected["turn_id"])
        self.assertIsNone(selected.get("question_id"))
        self.assertEqual([row["id"] for row in selected["detail"]["questions"]],
                         ["name", "amount", "count", "confirm", "colors", "optional"])
        self.assertEqual(selected["detail"]["questions"][4]["choices"], [
            {"value": "r", "label": "Red", "description": ""},
            {"value": "b", "label": "Blue", "description": ""}])
        result = state.submit_selected_response(selected, {"answers": {
            "name": ["Ada"], "amount": ["2.5"], "count": ["3"],
            "confirm": [False], "colors": ["b", "r"]}}, action_id="typed-form")
        self.assertEqual(result["state"], "applied")
        posts = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(posts, [("POST", f"/api/session/{self.sid}/form/frm_1/reply", {
            "answer": {"name": "Ada", "amount": 2.5, "count": 3,
                       "confirm": False, "colors": ["b", "r"]}})])
        self.assertEqual(self.api.unexpected, [])

    def test_no_turn_requires_exact_current_form_schema_reference(self):
        self._form([{"type": "string", "key": "answer", "title": "Answer",
                     "required": True}])
        selected = self._selected()
        self.assertTrue(state._opencode_turnless_form_binding(selected))
        weak = dict(selected)
        weak.pop("native_ref")
        refused = state.submit_selected_response(
            weak, {"answers": {"answer": ["yes"]}}, action_id="weak-form-ref")
        self.assertNotEqual(refused.get("state"), "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])
        self.assertIsNone(state._selected_entry(weak))

    def test_required_optional_hidden_and_conditional_fields_validate_before_claim(self):
        self._form([
            {"type": "boolean", "key": "enabled", "title": "Enable", "required": True},
            {"type": "string", "key": "extra", "title": "Extra", "required": True,
             "when": [{"key": "enabled", "op": "eq", "value": True}]},
            {"type": "string", "key": "hidden", "title": "Hidden", "hidden": True,
             "default": "native-only"},
            {"type": "string", "key": "optional", "title": "Optional",
             "default": "never-use-default"},
        ])
        selected = self._selected()
        drafted = state.write_selected_draft(selected, "retain this draft")
        incomplete = state.submit_selected_response(
            drafted, {"answers": {"enabled": [True]}}, action_id="missing-active-field")
        self.assertEqual(incomplete["state"], "open")
        self.assertEqual(incomplete["reason"], "opencode-form-required-answer-missing")
        self.assertEqual(incomplete["draft"], "retain this draft")
        self.assertNotIn("action_id", incomplete)
        self.assertIsNone(state._selected_entry(incomplete).get("action_id"))
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        corrected = state.submit_selected_response(
            incomplete, {"answers": {"enabled": [True], "extra": ["provided"]}},
            action_id="complete-active-field")
        self.assertEqual(corrected["state"], "applied")
        self.assertEqual([call for call in self.api.calls if call[0] == "POST"], [
            ("POST", f"/api/session/{self.sid}/form/frm_1/reply",
             {"answer": {"enabled": True, "extra": "provided"}})])

    def test_numeric_boolean_required_and_defaults_are_not_coerced_or_invented(self):
        self._form([
            {"type": "number", "key": "amount", "title": "Amount", "required": True,
             "minimum": 1, "maximum": 5, "default": 2},
            {"type": "integer", "key": "count", "title": "Count", "required": True,
             "minimum": 1, "maximum": 5, "default": 3},
            {"type": "boolean", "key": "confirm", "title": "Confirm", "required": True,
             "default": True},
            {"type": "string", "key": "optional", "title": "Optional",
             "default": "not-submitted"},
        ])
        selected = self._selected()
        cases = [
            ({"amount": ["NaN"], "count": ["2"], "confirm": [True]},
             "opencode-form-numeric-answer-invalid"),
            ({"amount": ["2"], "count": ["2.5"], "confirm": [True]},
             "opencode-form-integer-answer-invalid"),
            ({"amount": ["2"], "count": ["2"], "confirm": ["false"]},
             "opencode-form-boolean-answer-explicit-required"),
            ({"amount": ["2"], "confirm": [True]},
             "opencode-form-required-answer-missing"),
        ]
        for index, (answers, reason) in enumerate(cases):
            invalid = state.submit_selected_response(
                selected, {"answers": answers}, action_id=f"invalid-typed-{index}")
            self.assertEqual(invalid["state"], "open")
            self.assertEqual(invalid["reason"], reason)
            self.assertNotIn("action_id", invalid)
            self.assertIsNone(state._selected_entry(invalid))
            self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        valid = state.submit_selected_response(selected, {"answers": {
            "amount": ["2"], "count": ["2"], "confirm": [True]}},
            action_id="valid-typed-values")
        self.assertEqual(valid["state"], "applied")
        post = [call for call in self.api.calls if call[0] == "POST"]
        self.assertEqual(post[0][2], {"answer": {"amount": 2.0, "count": 2, "confirm": True}})
        self.assertNotIn("optional", post[0][2]["answer"])

    def test_changed_schema_endpoint_and_ambiguous_current_form_refuse_old_action(self):
        self._form([{"type": "string", "key": "answer", "title": "Answer",
                     "required": True}])
        selected = self._selected()
        self.api.forms["frm_1"]["fields"][0]["title"] = "Replaced schema"
        stale = state.submit_selected_response(
            selected, {"answers": {"answer": ["old"]}}, action_id="stale-schema")
        self.assertNotEqual(stale["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.api.forms["frm_1"]["fields"][0]["title"] = "Answer"
        current = self._selected()
        self.endpoint["generation"] = "b" * 64
        changed_owner = state.submit_selected_response(
            current, {"answers": {"answer": ["old owner"]}}, action_id="stale-owner")
        self.assertNotEqual(changed_owner["state"], "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

        self.endpoint["generation"] = "a" * 64
        self.api.question("frm_2", message_id=None)
        ambiguous = self._selected()
        self.assertFalse(ambiguous["support"]["supported"])
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_lost_reply_keeps_one_shot_action_and_never_replays(self):
        self._form([{"type": "string", "key": "answer", "title": "Answer",
                     "required": True}])
        selected = self._selected()
        self.api.post_status = 0
        first = state.submit_selected_response(
            selected, {"answers": {"answer": ["yes"]}}, action_id="lost-once")
        self.assertEqual(first["state"], "unknown")
        second = state.submit_selected_response(
            selected, {"answers": {"answer": ["yes"]}}, action_id="must-not-replay")
        self.assertEqual(second["action_id"], "lost-once")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1)

    def test_form_detail_draft_stays_private_and_external_type_keeps_native_fallback(self):
        self._form([{"type": "string", "key": "answer", "title": "Private form",
                     "required": True}])
        selected = self._selected()
        drafted = state.write_selected_draft(selected, "private draft")
        fleet = state._fleet_cache_path().read_text(encoding="utf-8")
        projection = state._cache_path().read_text(encoding="utf-8")
        self.assertNotIn("Private form", fleet)
        self.assertNotIn("Private form", projection)
        self.assertNotIn("private draft", fleet)
        self.assertIn("private draft", projection)
        self.assertEqual(drafted["draft"], "private draft")

        self.api.forms.clear()
        self.api.form_ids.clear()
        self._form([{"type": "external", "key": "external", "url": "https://example.invalid"}],
                   request_id="frm_external")
        unsupported = state.read_selected_request({"id": self.item_id,
                                                    "harness": "opencode",
                                                    "session_id": self.sid})
        self.assertFalse(unsupported["support"]["supported"])
        preserved = state.write_selected_draft(unsupported, "keep external native fallback")
        refused = state.submit_selected_response(
            preserved, {"answers": {"external": [True]}}, action_id="external-no-reply")
        self.assertNotEqual(refused.get("state"), "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])


class OpenCodeV2EndpointIdentityTest(unittest.TestCase):
    """Synthetic /proc and documented /api/info fixtures prove endpoint ownership."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sid = "ses_identity_fixture"
        self.frontend_pid = 4242
        self.server_pid = 5252
        self.url = "http://127.0.0.1:4096"
        self.proc_starts = {self.frontend_pid: "420", self.server_pid: "520"}
        self.listeners = {self.server_pid: (4096, "127.0.0.1", ("7001",))}
        self.listener_calls = []
        self.selection = (self.sid, "opencode-tui-selection")
        self.argv = ["opencode", "--server", self.url]
        self.api = _StrictOpenCodeTransport(self.sid)
        self.api.server_info = {
            "version": "1.2.3", "pid": self.server_pid, "urls": [self.url],
            "paths": {"tmp": "/tmp/opencode-fixture"}}
        self.api.after_info = None
        self.now = time.time()

        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": self.tmp.name,
            "HOME": self.tmp.name,
            "FLEET_INTERACTION_STATE_DIR": str(Path(self.tmp.name) / "interactions"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        producer = mock.patch.object(state, "_request_producer")
        producer.start()
        self.addCleanup(producer.stop)

        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from tools.fleet.collectors import opencode as collector
        proc_path = Path("/proc")
        real_iterdir = Path.iterdir

        def fake_iterdir(path):
            if path == proc_path:
                return iter([Path(f"/proc/{self.frontend_pid}")])
            return real_iterdir(path)

        patches = [
            mock.patch.object(Path, "iterdir", fake_iterdir),
            mock.patch.object(state, "_proc_uid", side_effect=self._proc_uid),
            mock.patch.object(state, "_proc_start_ticks", side_effect=self._proc_start),
            mock.patch.object(state, "_proc_argv", side_effect=self._proc_argv),
            mock.patch.object(state, "_proc_listening_sockets", side_effect=self._listener),
            mock.patch.object(collector, "session_of_process", side_effect=self._selection),
            mock.patch.object(state, "_opencode_http", side_effect=self.api),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _proc_uid(self, pid):
        return os.getuid() if int(pid) in self.proc_starts else None

    def _proc_start(self, pid):
        return self.proc_starts.get(int(pid))

    def _proc_argv(self, pid):
        return list(self.argv) if int(pid) == self.frontend_pid else []

    def _listener(self, pid):
        self.listener_calls.append(int(pid))
        return self.listeners.get(int(pid))

    def _selection(self, pid):
        if int(pid) != self.frontend_pid:
            return None, ""
        return self.selection

    def _attach(self):
        self.argv = ["opencode", "--server", self.url]

    def _standalone(self):
        self.server_pid = self.frontend_pid
        self.proc_starts[self.server_pid] = "420"
        self.listeners[self.frontend_pid] = (4096, "127.0.0.1", ("7001",))
        self.argv = ["opencode"]
        self.api.server_info = {
            "version": "1.2.3", "pid": self.frontend_pid, "urls": [self.url],
            "paths": {"tmp": "/tmp/opencode-fixture"}}

    def _publish_wait(self):
        self.item_id = f"S1:opencode:{self.sid}:{int(self.now * 1000)}"
        item = {"id": self.item_id, "source": "S1", "kind": "decision",
                "question": None, "choices": [], "route_id": None,
                "attempt_id": None, "gate_id": None, "since": self.now,
                "stale": False,
                "owner": {"harness": "opencode", "session_id": self.sid},
                "attribution": "exact"}
        self.assertTrue(state.publish_cache({"items": [item], "complete": True,
                                             "observed_at": time.time(), "health": []}))

    def _form_selection(self):
        self.api.question(message_id=None)
        self._publish_wait()
        return state.read_selected_request({"id": self.item_id, "harness": "opencode",
                                             "session_id": self.sid})

    def test_standalone_requires_current_selection_and_documented_v2_info(self):
        self._standalone()
        endpoint = state._opencode_endpoint(self.sid)
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint["frontend_pid"], self.frontend_pid)
        self.assertEqual(endpoint["server_pid"], self.frontend_pid)
        self.assertEqual(endpoint["socket_inodes"], ["7001"])
        self.assertEqual([call[1] for call in self.api.calls], ["/api/info"])
        self.assertNotIn("/global/health", [call[1] for call in self.api.calls])

    def test_attached_server_uses_server_pid_and_its_owned_listener(self):
        self._attach()
        endpoint = state._opencode_endpoint(self.sid)
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint["frontend_pid"], self.frontend_pid)
        self.assertEqual(endpoint["server_pid"], self.server_pid)
        self.assertEqual(endpoint["pid"], self.server_pid)
        self.assertEqual(endpoint["server_proc_start"], "520")
        self.assertEqual(endpoint["socket_inodes"], ["7001"])
        self.assertEqual(self.listener_calls, [self.server_pid])

    def test_frontend_selection_is_rechecked_after_server_info(self):
        def switch_selection():
            self.api.after_info = None
            self.selection = ("ses_other", "opencode-tui-selection")

        self.api.after_info = switch_selection
        self.assertIsNone(state._opencode_endpoint(self.sid))

    def test_bad_info_auth_url_or_listener_stays_unsupported(self):
        valid = dict(self.api.server_info)
        invalid_bodies = [
            None,
            {"version": "1.2.3", "pid": True, "urls": [self.url],
             "paths": {"tmp": "/tmp"}},
            {"version": "1.2.3", "pid": self.server_pid,
             "urls": ["http://127.0.0.1:4097"], "paths": {"tmp": "/tmp"}},
            {"version": "1.2.3", "pid": self.server_pid,
             "urls": [self.url], "paths": {}},
        ]
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.api.server_info = body
                self.assertIsNone(state._opencode_endpoint(self.sid))
        self.api.server_info = valid
        self.api.info_status = 401
        self.assertIsNone(state._opencode_endpoint(self.sid))
        self.api.info_status = 200
        self.listeners[self.server_pid] = (4097, "127.0.0.1", ("7002",))
        self.assertIsNone(state._opencode_endpoint(self.sid))

    def test_pid_birth_and_socket_replacement_change_generation_at_same_url(self):
        original = state._opencode_endpoint(self.sid)
        self.assertIsNotNone(original)
        self.proc_starts[self.server_pid] = "521"
        self.listeners[self.server_pid] = (4096, "127.0.0.1", ("7002",))
        new_birth = state._opencode_endpoint(self.sid)
        self.assertIsNotNone(new_birth)
        self.assertEqual(new_birth["url"], original["url"])
        self.assertNotEqual(new_birth["generation"], original["generation"])
        self.listeners[self.server_pid] = (4096, "127.0.0.1", ("7003",))
        new_socket = state._opencode_endpoint(self.sid)
        self.assertNotEqual(new_socket["generation"], new_birth["generation"])

    def test_same_url_restart_before_submission_cannot_post_old_form(self):
        selected = self._form_selection()
        self.assertTrue(selected["support"]["supported"])
        self.proc_starts[self.server_pid] = "521"
        self.listeners[self.server_pid] = (4096, "127.0.0.1", ("7002",))
        result = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="old-server")
        self.assertNotEqual(result.get("state"), "applied")
        self.assertFalse([call for call in self.api.calls if call[0] == "POST"])

    def test_server_replacement_after_post_is_unknown_and_never_replayed(self):
        selected = self._form_selection()
        self.assertTrue(selected["support"]["supported"])

        def replace_server():
            self.proc_starts[self.server_pid] = "521"
            self.listeners[self.server_pid] = (4096, "127.0.0.1", ("7002",))

        self.api.after_post = replace_server
        first = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="server-replaced")
        self.assertEqual(first["state"], "unknown")
        second = state.submit_selected_response(
            selected, {"answers": {"choice": ["red"]}}, action_id="must-not-replay")
        self.assertNotEqual(second.get("state"), "applied")
        self.assertEqual(len([call for call in self.api.calls if call[0] == "POST"]), 1,
                         (first, second, self.api.calls))



class WorkflowOwnerPersonContextTest(unittest.TestCase):
    """RV-OWNS-TRI: an unreadable lineage is unknown (None), never "another parent"."""

    def judge(self, owns):
        import dispatch_seat_handover
        import route_authority
        metadata = {"parent_harness": "codex", "parent_sid": "sid-parent"}
        base = {"owner": {"harness": "codex", "session_id": "sid-parent"}}
        env = {key: value for key, value in os.environ.items()
               if key not in {"AGENT_DISPATCH_REGISTERED_WORKER", "AGENT_DISPATCH_CHILD",
                              "AGENT_DISPATCH_ATTEMPT_ID", "AGENT_DISPATCH_DEPTH"}}
        owns_patch = (mock.patch.object(route_authority, "owns", side_effect=owns) if callable(owns)
                      else mock.patch.object(route_authority, "owns", return_value=owns))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(route_authority, "caller_identity", return_value=("codex", "sid-parent")), \
                mock.patch.object(dispatch_seat_handover, "effective_parent", return_value="sid-parent"), \
                mock.patch.object(dispatch_seat_handover, "effective_parent_harness", return_value="codex"), \
                owns_patch:
            return state._workflow_owner_person_context(base, metadata, Path("/nonexistent/jobs.log"))

    def test_owner_not_owner_and_unknown_stay_distinct(self):
        self.assertIs(self.judge(True), True)
        self.assertIs(self.judge(False), False)
        self.assertIsNone(self.judge(None))

        def unreadable(*_args, **_kwargs):
            raise OSError("lineage-directory-changed")
        self.assertIsNone(self.judge(unreadable))



class SessionRouteDisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, XDG_STATE_HOME=self.tmp.name)
        env.start()
        self.addCleanup(env.stop)
        for name, value in (("_WAKE_FAILED", True), ("_WAKE_EVENTS", None), ("_NEXT_WAKE", None),
                            ("_WATCH_STAT", ())):
            patch = mock.patch.object(state, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_wait_and_route_badges_share_one_read_and_keep_exact_sessions(self):
        now = time.time()
        self.assertTrue(state.publish_cache({"items": [], "complete": True, "observed_at": now,
            "route_status": {"claude\0sid-a": "route 2 · frame, execute"},
            "route_sessions": ["claude\0sid-a", "claude\0sid-b"]}))
        with mock.patch.object(state, "_read_cache", wraps=state._read_cache) as reader, \
             mock.patch.object(state, "_request_producer") as request:
            self.assertEqual(state.cached_statusline("claude", "sid-a"),
                             ((0, 0), "route 2 · frame, execute"))
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(state.cached_statusline("claude", "sid-b"), ((0, 0), ""))
            request.assert_not_called()
            self.assertEqual(state.cached_statusline("codex", "sid-a"), ((0, 0), ""))
            request.assert_called_once_with("codex", "sid-a")
        with mock.patch.object(state.time, "time", return_value=now + 181), \
             mock.patch.object(state, "_request_producer") as request:
            self.assertEqual(state.cached_statusline("claude", "sid-a"), (None, ""))
            request.assert_called_once_with("claude", "sid-a")

    def test_statusline_tidy_notice_is_a_third_line_only_when_asked(self):
        # PG-20: the OpenCode footer asks for the waiting session-tidy result the
        # Claude status line shows; Claude's two-line read stays unchanged.
        import contextlib
        import io

        def run(*extra):
            out = io.StringIO()
            with mock.patch.object(state, "cached_statusline",
                                   return_value=((3, 1), "route 1 · plan")), \
                 mock.patch.object(state, "statusline_tidy_notice",
                                   return_value="continue not sent") as notice, \
                 contextlib.redirect_stdout(out):
                self.assertEqual(state.main(["--statusline", "opencode", "ses_a", *extra]), 0)
            return out.getvalue().split("\n"), notice

        lines, notice = run()
        self.assertEqual(lines, ["3 1", "route 1 · plan", ""])
        notice.assert_not_called()
        lines, notice = run("--with-tidy-notice")
        self.assertEqual(lines, ["3 1", "route 1 · plan", "continue not sent", ""])
        notice.assert_called_once_with("opencode", "ses_a")
        with mock.patch.dict(sys.modules, {"session_tidy": None}):
            self.assertEqual(state.statusline_tidy_notice("opencode", "ses_a"), "")

    def test_returned_route_display_reuses_the_idle_single_flight_without_repeated_wakes(self):
        # #1369's idle wake and the route consumer share one real flock/demand.
        root = state.state_root()
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        demand = root / ".demand"
        demand.touch()
        os.utime(demand, (0, 0))
        fd = os.open(root / ".projection.lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = 10_000.0
        sleeps, refreshes = [], []
        class Finished(BaseException):
            pass
        def refresh():
            requests = state._route_demands(now=now)
            refreshes.append(dict(requests))
            state.publish_cache({"items": [{"id": "S3:fixture", "source": "S3", "since": now,
                "owner": {"harness": "claude", "session_id": "other-wait"}}],
                "observed_at": now, "complete": True, "route_sessions": requests,
                "route_status": {key: "route 1 · frame" for key in requests}})
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 1:
                self.assertEqual(state.cached_statusline("claude", "sid-a"), ((1, 0), ""))
                os.utime(demand, (now, now))
                return
            stamp = demand.stat().st_mtime_ns
            for _ in range(20):
                self.assertEqual(state.cached_statusline("claude", "sid-a"),
                                 ((1, 0), "route 1 · frame"))
            self.assertEqual(demand.stat().st_mtime_ns, stamp)
            raise Finished()
        with mock.patch.object(state, "_refresh_cache", side_effect=refresh), \
             mock.patch.object(state, "_notification_available", return_value=True), \
             mock.patch.object(state.time, "time", return_value=now), \
             mock.patch.object(state.time, "sleep", side_effect=sleep), \
             mock.patch.object(state.subprocess, "Popen") as launch:
            with self.assertRaises(Finished):
                state._producer_loop(fd)
        self.assertEqual(sleeps, [30.0, 30.0])
        self.assertEqual(refreshes, [{}, {"claude\0sid-a": now}])
        launch.assert_not_called()

    def test_active_display_renews_demand_even_while_projection_is_fresh(self):
        now = time.time()
        self.assertTrue(state.publish_cache({"items": [], "complete": True, "observed_at": now,
            "route_status": {"claude\0sid-a": "route 1 · frame"},
            "route_sessions": {"claude\0sid-a": now - 61}}))
        with mock.patch.object(state, "_request_producer") as request:
            self.assertEqual(state.cached_statusline("claude", "sid-a"), ((0, 0), "route 1 · frame"))
            request.assert_called_once_with("claude", "sid-a")

    def test_existing_demand_marker_is_private_bounded_and_expires(self):
        with mock.patch.object(state, "_ensure_producer"):
            for index in range(70):
                state._request_producer("opencode", "ses-" + str(index))
        demands = state._route_demands()
        self.assertEqual(len(demands), 64)
        self.assertIn("opencode\0ses-69", demands)
        self.assertEqual(stat.S_IMODE((state.state_root() / ".demand").stat().st_mode), 0o600)
        self.assertEqual(state._route_demands(now=time.time() + 601), {})
        self.assertFalse((state.state_root() / "route-status.json").exists())

    def test_route_failure_does_not_erase_answer_wait_observation(self):
        from tools.fleet import collectors
        result = {"items": [], "complete": True, "observed_at": time.time()}
        with mock.patch.object(state, "_route_demands", return_value={"claude\0sid-a": time.time()}), \
             mock.patch.object(observation, "read_answer_waits", return_value=result), \
             mock.patch.object(collectors, "collect_all", side_effect=OSError("unavailable")), \
             mock.patch.object(state, "_notify_result"):
            self.assertTrue(state._refresh_cache())
        value = state._read_cache()
        self.assertTrue(value["complete"])
        self.assertEqual(value["route_status"], {})

    def test_optional_panel_detail_roundtrips_but_cannot_crowd_out_wait_truth(self):
        panels = {"claude\0sid-a": [{"title": "Current work", "stages": [{"name": "inline", "st": "active"}]}]}
        result = {"items": [], "complete": True, "observed_at": time.time(), "route_panels": panels}
        self.assertTrue(state.publish_cache(result))
        self.assertEqual(state.cached_view(request=False)["route_panels"], panels)
        self.assertTrue(state.publish_cache({**result, "route_panels": {"claude\0sid-a": "x" * state._CACHE_MAX_BYTES}}))
        view = state.cached_view(request=False)
        self.assertTrue(view["complete"])
        self.assertEqual(view["route_panels"], {})


class MessageCompletionTests(unittest.TestCase):
    def setUp(self):
        from fixtures.message_questions import MessageQuestionFixture
        self.fixture = MessageQuestionFixture()
        self.f = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def test_shared_project_questions_remain_open_for_each_exact_session(self):
        import session_tidy as tidy
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                def ask(sid, text):
                    seat = tidy.session_seat(tidy.seat_for_project(harness,
                        tidy.project_key_for(str(self.f.root))), harness, sid)
                    return state.register_message(harness, sid, seat, text,
                        sequence=tidy.read_prompt_seq(seat))
                a = ask("session-a", "question A")
                b = ask("session-b", "question B")
                self.assertEqual(a["seat_key"], b["seat_key"])
                for signal in (a, b):
                    selected = state.read_selected_request({"id": signal["id"],
                        "harness": harness, "session_id": signal["session_id"]})
                    self.assertEqual(selected["state"], "open", selected)
                ask("session-a", "replacement A")
                selected = state.read_selected_request({"id": b["id"],
                    "harness": harness, "session_id": b["session_id"]})
                self.assertEqual(selected["state"], "open", selected)

    def test_an_answered_message_question_leaves_the_published_waits_at_once(self):
        # Correction 3 (11-13): answering a registered question from the panel completed it, but the
        # published answer-wait cache kept listing it until the next collection (up to a minute), so
        # the panel kept `◑ 1` and the question. Completion now drops that one wait from the cache
        # and Fleet's view right away, the rule the collection applies anyway; other waits stay.
        a, ea = self.f.ask("claude", "session-a", "question A")
        b, eb = self.f.ask("claude", "session-b", "question B")
        def item(signal):
            return {"id": signal["id"], "source": "S4", "kind": "message", "since": signal["since"],
                    "owner": {"harness": signal["harness"], "session_id": signal["session_id"]}}
        self.assertTrue(state.publish_cache({"items": [item(a), item(b)], "complete": True}))
        before = state._read_cache_file()
        self.assertEqual(before["total"], 2)
        self.f.submit(ea, "answer A")
        wire, _, _ = self.f.prepare(a)
        self.f.consume(a, wire)
        self.assertEqual(self.source(a)["lifecycle"], "answered")
        after = state._read_cache_file()
        self.assertEqual([row["id"] for row in after["items"]], [b["id"]])
        self.assertEqual(after["total"], 1)
        self.assertNotIn("claude\0session-a", after["by_session"])
        self.assertEqual(after["by_session"]["claude\0session-b"], [b["id"]])
        fleet = json.loads(state._fleet_cache_path().read_text())
        self.assertEqual([row["id"] for row in fleet["items"]], [b["id"]])
        self.assertEqual(fleet["total"], 1)
        self.assertEqual(state.cached_statusline("claude", "session-a")[0][1], 0)
        # A direct answer in the session settles the same way.
        self.f.direct(b, "answer B")
        self.assertEqual(state._read_cache_file()["items"], [])

    def test_a_collection_read_before_the_answer_does_not_publish_the_answered_question(self):
        # Review 1597-1 (11-13): one pass read A, A was answered from the panel and the same
        # session registered B, then the pass published what it had read — the answered A came
        # back to the cache and Fleet until the next collection. The publish re-reads each
        # registered question's exact source under the lock its completion settles with.
        from tools.fleet.collectors import dispatch
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                for path in state.state_root().glob("*.json"):
                    path.unlink()
                a, ea = self.f.ask(harness, "session-a", "first")
                b, _ = self.f.ask(harness, "session-b", "other")
                original = observation.read_answer_waits
                def collect():
                    with mock.patch.object(observation, "_session_liveness_probe",
                                           return_value=lambda *args: "live"), \
                         mock.patch.object(observation, "_codex_rollout_waits",
                                           return_value={"complete": True, "pending": {}}):
                        return original(jobs_paths=[self.f.jobs])
                self.assertTrue(state.publish_cache(collect()))
                later = []
                def read_then_answer(**kwargs):
                    seen = collect()
                    self.f.submit(ea, "answer first")
                    wire, _, _ = self.f.prepare(a)
                    self.f.consume(a, wire)
                    later.append(self.f.ask(harness, "session-a", "second")[0])
                    return seen
                with mock.patch.object(observation, "read_answer_waits", side_effect=read_then_answer), \
                     mock.patch.object(state, "_route_demands", return_value={}), \
                     mock.patch.object(state, "_notify_result"), \
                     mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[self.f.jobs]):
                    self.assertTrue(state._refresh_cache())
                self.assertEqual(self.source(later[0])["lifecycle"], "open")
                shared = state._read_cache_file()
                fleet = json.loads(state._fleet_cache_path().read_text())
                for view in (shared, fleet):
                    self.assertEqual([row["id"] for row in view["items"]], [b["id"]])
                    self.assertEqual(view["total"], 1)
                self.assertNotIn(harness + "\0session-a", shared["by_session"])
                # The next collection (registration requests one) shows B beside the other question.
                with mock.patch.object(observation, "read_answer_waits", side_effect=lambda **kw: collect()), \
                     mock.patch.object(state, "_route_demands", return_value={}), \
                     mock.patch.object(state, "_notify_result"), \
                     mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[self.f.jobs]):
                    self.assertTrue(state._refresh_cache())
                self.assertEqual(sorted(row["id"] for row in state._read_cache_file()["items"]),
                                 sorted([b["id"], later[0]["id"]]))

    def test_a_source_not_read_at_publication_keeps_its_unanswered_question(self):
        # Review 1597-r2 (11-13): the publication's re-read turned a failed read (`_read` folds an
        # OSError into None) into "that question ended", so one EIO on the open A's source left A
        # out of the cache and Fleet with `complete: true` and no health, until the next collection.
        # Not read is not ended: A stays as the collection read it, and the publication says it is
        # incomplete through the existing health path. An ended (answered) A still leaves (above).
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                for path in state.state_root().glob("*.json"):
                    path.unlink()
                a, _ = self.f.ask(harness, "session-a", "unanswered A")
                b, _ = self.f.ask(harness, "session-b", "unanswered B")
                with mock.patch.object(observation, "_session_liveness_probe",
                                       return_value=lambda *args: "live"), \
                     mock.patch.object(observation, "_codex_rollout_waits",
                                       return_value={"complete": True, "pending": {}}):
                    seen = observation.read_answer_waits(jobs_paths=[self.f.jobs])
                self.assertTrue(seen["complete"])
                source = state._signal_path(a)
                raw = source.read_bytes()
                original, failed = Path.read_text, []
                def transient(path, *args, **kwargs):
                    if path == source and not failed:
                        failed.append(path)
                        raise OSError(5, "synthetic transient I/O failure")
                    return original(path, *args, **kwargs)
                with mock.patch.object(Path, "read_text", transient):
                    self.assertTrue(state.publish_cache(seen))
                self.assertEqual(len(failed), 1)
                self.assertEqual(source.read_bytes(), raw)
                self.assertEqual(self.source(a)["lifecycle"], "open")
                shared = state._read_cache_file()
                fleet = json.loads(state._fleet_cache_path().read_text())
                for view in (shared, fleet):
                    self.assertEqual(sorted(row["id"] for row in view["items"]), sorted([a["id"], b["id"]]))
                    self.assertEqual(view["total"], 2)
                    self.assertIs(view["complete"], False)
                    self.assertIn({"source": "S4", "state": "unknown"}, view["health"])
                self.assertEqual(shared["by_session"][harness + "\0session-a"], [a["id"]])
                # The next publication that reads the source is complete again.
                self.assertTrue(state.publish_cache(seen))
                shared = state._read_cache_file()
                self.assertEqual(sorted(row["id"] for row in shared["items"]), sorted([a["id"], b["id"]]))
                self.assertIs(shared["complete"], True)
                self.assertEqual(shared["health"], [])
                # A source that is gone has ended: it still leaves at once.
                source.unlink()
                self.assertTrue(state.publish_cache(seen))
                self.assertEqual([row["id"] for row in state._read_cache_file()["items"]], [b["id"]])

    def test_the_publication_table_for_each_question_state_read_and_collection(self):
        # Review 1597-r3 (11-13), the third finding at this spot (r1: a late publication brought an
        # answered question back; r2: a failed read hid an unanswered one; r3: a failed read brought
        # an answered one back). The whole table is fixed here instead of one cell at a time: the
        # question's real state x the publication's re-read of its source x what is published (a
        # collection read before the change, published late, or a new collection). B, an
        # unanswered question of another session, is read normally and always stays.
        # Review 1597-r4: a source that is read decides; the completions are used only when it
        # cannot be read. "reopened" is A answered and settled, then the same id registered again
        # (another question C, then A, in the same input sequence): read, it shows; not read, its
        # completion keeps it out until a publication that reads it (checked below).
        import session_shell
        LOAD, DROP = "load", "drop"
        # state -> {read: (late publication of the earlier collection, new collection)}
        table = {
            "open":            {"ok": (LOAD, LOAD), "fail": (LOAD, LOAD)},
            "answered":        {"ok": (DROP, DROP), "fail": (DROP, DROP)},
            "source-answered": {"ok": (DROP, DROP), "fail": (LOAD, DROP)},
            "source-expired":  {"ok": (DROP, DROP), "fail": (LOAD, DROP)},
            "source-cancelled": {"ok": (DROP, DROP), "fail": (LOAD, DROP)},
            "source-gone":     {"ok": (DROP, DROP), "fail": (DROP, DROP)},
            "replaced":        {"ok": (DROP, DROP), "fail": (LOAD, DROP)},
            "reopened":        {"ok": (LOAD, LOAD), "fail": (DROP, DROP)},
        }
        def collect():
            with mock.patch.object(observation, "_session_liveness_probe",
                                   return_value=lambda *args: "live"), \
                 mock.patch.object(observation, "_codex_rollout_waits",
                                   return_value={"complete": True, "pending": {}}):
                return observation.read_answer_waits(jobs_paths=[self.f.jobs])
        cell = 0
        for harness in ("claude", "codex", "opencode"):
            for change, reads in table.items():
                for read, expected in reads.items():
                    for collection, want in zip(("late", "new"), expected):
                        cell += 1
                        with self.subTest(harness=harness, state=change, read=read, collection=collection):
                            for path in state.state_root().glob("*.json"):
                                path.unlink()
                            sid = "session-a-%d" % cell
                            a, ea = self.f.ask(harness, sid, "question A %d" % cell)
                            b, _ = self.f.ask(harness, "session-b-%d" % cell, "question B %d" % cell)
                            earlier = collect()
                            self.assertTrue(state.publish_cache(earlier))
                            source = state._signal_path(a)
                            if change in ("answered", "reopened"):
                                self.f.submit(ea, "answer A")
                                wire, _, _ = self.f.prepare(a)
                                self.f.consume(a, wire)
                                self.assertEqual(self.source(a)["lifecycle"], "answered")
                                self.assertNotIn(a["id"], [row["id"] for row in state._read_cache_file()["items"]])
                            if change == "reopened":
                                self.f.ask(harness, sid, "question C %d" % cell)
                                again, _ = self.f.ask(harness, sid, "question A %d" % cell)
                                self.assertEqual(again["id"], a["id"])
                                self.assertEqual(self.source(a)["lifecycle"], "open")
                                self.assertIn(a["id"], [row["id"] for row in
                                                        state._read_cache_file()["settled_waits"]])
                            elif change.startswith("source-") and change != "source-gone":
                                # Reflected in the source only; its completion has not settled yet.
                                signal = self.source(a)
                                signal["lifecycle"] = change.split("-", 1)[1]
                                state._write_signal_source(signal)
                            elif change == "source-gone":
                                source.unlink()
                            elif change == "replaced":
                                replacement, _ = self.f.ask(harness, sid, "next question %d" % cell)
                                self.assertNotEqual(replacement["id"], a["id"])
                                self.assertEqual(state._signal_path(replacement), source)
                            published = earlier if collection == "late" else collect()
                            original = Path.read_text
                            def unreadable(path, *args, **kwargs):
                                if path == source:
                                    raise OSError(5, "synthetic I/O failure")
                                return original(path, *args, **kwargs)
                            with mock.patch.object(Path, "read_text",
                                                   unreadable if read == "fail" else original):
                                self.assertTrue(state.publish_cache(published))
                            shared = state._read_cache_file()
                            fleet = json.loads(state._fleet_cache_path().read_text())
                            panel = session_shell.session_questions(
                                harness, sid, view=state.cached_view(request=False))
                            seen = {"shared": a["id"] in [row["id"] for row in shared["items"]],
                                    "by_session": a["id"] in shared["by_session"].get(harness + "\0" + sid, ()),
                                    "fleet": a["id"] in [row["id"] for row in fleet["items"]],
                                    "panel": a["id"] in [item["id"] for item in panel]}
                            self.assertEqual(seen, dict.fromkeys(seen, want == LOAD))
                            for view in (shared, fleet):
                                self.assertIn(b["id"], [row["id"] for row in view["items"]])
                                self.assertEqual(view["total"], len(view["items"]))
                            if change == "open" and read == "fail":
                                for view in (shared, fleet):
                                    self.assertIs(view["complete"], False)
                                    self.assertIn({"source": "S4", "state": "unknown"}, view["health"])
                            if change == "reopened":
                                if read == "fail":
                                    # The next publication that reads the source shows it.
                                    self.assertTrue(state.publish_cache(published))
                                    shared = state._read_cache_file()
                                    self.assertIn(a["id"], [row["id"] for row in shared["items"]])
                                    self.assertIn(a["id"], [row["id"] for row in json.loads(
                                        state._fleet_cache_path().read_text())["items"]])
                                # Read open, its id leaves the completions.
                                self.assertNotIn(a["id"], [row["id"] for row in
                                                           shared.get("settled_waits", ())])

    def test_settled_waits_leave_once_a_later_collection_is_published(self):
        # Review 1597-r3: the remembered completions do not grow. Many answers between two
        # collections are all remembered; the next collection that started after them clears them.
        def collect():
            with mock.patch.object(observation, "_session_liveness_probe",
                                   return_value=lambda *args: "live"), \
                 mock.patch.object(observation, "_codex_rollout_waits",
                                   return_value={"complete": True, "pending": {}}):
                return observation.read_answer_waits(jobs_paths=[self.f.jobs])
        asked = [self.f.ask("claude", "session-%d" % n, "question %d" % n) for n in range(5)]
        earlier = collect()
        self.assertTrue(state.publish_cache(earlier))
        for signal, envelope in asked:
            self.f.direct(signal, "answer")
            self.assertEqual(self.source(signal)["lifecycle"], "answered")
        ids = [signal["id"] for signal, _ in asked]
        self.assertEqual([row["id"] for row in state._read_cache_file()["settled_waits"]], ids)
        # The same answer settled twice is remembered once.
        state._settle_published_wait(ids[-1])
        self.assertEqual([row["id"] for row in state._read_cache_file()["settled_waits"]], ids)
        # Late publications of the earlier collection publish none of them and keep the list.
        for _ in range(3):
            self.assertTrue(state.publish_cache(earlier))
            value = state._read_cache_file()
            self.assertEqual(value["items"], [])
            self.assertEqual(len(value["settled_waits"]), len(ids))
        # A collection that started after the answers clears the list.
        self.assertTrue(state.publish_cache(collect()))
        self.assertNotIn("settled_waits", state._read_cache_file())
        # The bound holds even if no later collection ever comes.
        for n in range(state._SETTLED_WAITS_MAX + 10):
            state._settle_published_wait("S4:%064x" % n)
        self.assertEqual(len(state._read_cache_file()["settled_waits"]), state._SETTLED_WAITS_MAX)

    def test_legacy_question_keeps_its_address_when_another_session_registers(self):
        import session_tidy as tidy
        a, envelope = self.f.ask("codex", "session-a", "legacy question")
        legacy = state._path(a["seat_key"])
        # Recreate the pre-upgrade source layout, keeping its exact public ID.
        for path in state.state_root().glob("*.json"):
            row = state._read(path)
            if row and row.get("id") == a["id"]:
                path.unlink()
        state._write(legacy, a)
        b, _ = self.f.ask("codex", "session-b", "new question")
        selected = state.read_selected_request(envelope)
        self.assertEqual(selected["state"], "open", selected)
        self.assertEqual(selected["request_id"], a["id"])
        self.f.direct(a, "answer A")
        self.assertEqual(state.read_selected_request(envelope)["state"], "applied")
        selected_b = state.read_selected_request({"id": b["id"], "harness": "codex",
                                                "session_id": b["session_id"]})
        self.assertEqual(selected_b["state"], "open", selected_b)
        self.assertEqual(len(state.read_signals()), 1)

    def test_shared_seat_answers_still_require_each_original_consumed_receipt(self):
        a, ea = self.f.ask("codex", "session-a", "question A")
        b, eb = self.f.ask("codex", "session-b", "question B")
        self.assertEqual(state.read_selected_request(ea)["state"], "open")
        self.f.submit(ea, "answer A")
        wire, _, _ = self.f.prepare(a)
        self.f.direct(b, "answer B")
        self.assertEqual(self.source(a)["lifecycle"], "open")
        self.f.consume(a, wire, sid=b["session_id"])
        self.assertEqual(self.source(a)["lifecycle"], "open")
        self.f.consume(a, wire)
        self.assertEqual(self.source(a)["completion"]["actor"], "fleet")
        self.assertEqual(self.source(b)["completion"]["actor"], "direct")

    def test_same_sid_shared_seat_is_still_separate_between_harnesses(self):
        import session_tidy as tidy
        seat = tidy.Seat("project", "shared-project")
        a = state.register_message("claude", "same-session", seat, "question A", sequence=0)
        b = state.register_message("codex", "same-session", seat, "question B", sequence=0)
        for signal in (a, b):
            view = state.read_selected_request({"id": signal["id"],
                "harness": signal["harness"], "session_id": signal["session_id"]})
            self.assertEqual(view["state"], "open", view)
        self.assertEqual(len(state.read_signals()), 2)

    def test_unreadable_new_slot_never_resurrects_the_legacy_question(self):
        signal, envelope = self.f.ask()
        legacy = state._path(signal["seat_key"])
        state._write(legacy, signal)
        scoped = state._signal_path(signal)
        self.assertNotEqual(scoped, legacy)
        scoped.unlink()
        scoped.symlink_to(scoped.with_suffix(".missing"))
        self.assertIsNone(state._message_source(envelope))
        self.assertEqual(state.read_signals(), [])
        self.assertTrue(legacy.exists())

    def test_interrupted_legacy_retirement_cannot_resurrect_a_cleared_source(self):
        import session_tidy as tidy
        signal = state.signal_value("codex", "legacy-session", "legacy-seat",
                                    "[답변 필요] legacy question", sequence=1)
        legacy = state._path(signal["seat_key"])
        state._write(legacy, signal)
        updated = state.signal_value("codex", "legacy-session", "legacy-seat",
                                     "[답변 필요] current question", sequence=2)
        with mock.patch.object(state, "_retire_legacy_signal", side_effect=OSError("interrupted")):
            self.assertTrue(state.write_signal(updated))
        self.assertTrue(legacy.exists())
        self.assertEqual([row["id"] for row in state.read_signals()], [updated["id"]])
        self.assertTrue(state.close_signal("legacy-seat", harness="codex",
                                          session_id="legacy-session", sequence=3))
        self.assertFalse(legacy.exists())
        self.assertEqual(state.read_signals(), [])

    def source(self, signal):
        return state._read(state._signal_path(signal))

    def test_each_native_direct_input_closes_only_nonempty_exact_session_once(self):
        import tidy_decisions
        for harness in ("claude", "codex", "opencode"):
            signal, _ = self.f.ask(harness)
            self.f.direct(signal, " \n ")
            self.assertEqual(self.source(signal)["lifecycle"], "open")
            foreign = dict(signal, session_id="other-session")
            self.f.direct(foreign, "foreign human input")
            self.assertEqual(self.source(signal)["lifecycle"], "open")
            self.f.direct(signal, "<task-notification>finished</task-notification>")
            self.assertEqual(self.source(signal)["lifecycle"], "open")
            self.f.direct(signal, "<agent-message>ordinary peer</agent-message>")
            self.assertEqual(self.source(signal)["lifecycle"], "open")
            self.f.direct(signal, "physical answer " + harness)
            completion = self.source(signal)["completion"]
            self.assertEqual(completion["actor"], "direct")
            self.f.direct(signal, "later physical prompt")
            self.assertEqual(self.source(signal)["completion"], completion)
        self.assertEqual(len(tidy_decisions.list_pending()), 3)

    def test_queued_fleet_answer_is_not_replaced_by_a_later_prompt(self):
        import tidy_decisions
        import session_notice
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "keep until consumed")
        self.f.submit(envelope, "queued Fleet answer")
        wire, ref, recipient = self.f.prepare(signal)
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.f.direct(signal, "unrelated prompt while delivery waits")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.assertTrue(session_notice.notice_is_current(self.f.record(signal)))
        self.f.peer.receive_peer_message(wire, recipient)
        completion = self.source(signal)["completion"]
        self.assertEqual((completion["actor"], completion["answer"], completion["action_id"]),
                         ("fleet", "queued Fleet answer", "fixture-action"))
        self.f.direct(signal, "later prompt")
        self.assertEqual(self.source(signal)["completion"], completion)
        entry = state._selected_entry(envelope)
        self.assertEqual((entry["state"], entry["result"]["status"]), ("applied", "applied"))
        self.assertEqual(self.f.record(signal)["state"], "acked")
        state.flush_message_decisions()
        decisions = tidy_decisions.list_pending()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0][1]["answers"], ["queued Fleet answer"])
        # A new module instance reads the persisted claim; it never resends it.
        spec = importlib.util.spec_from_file_location("question_restart", state.__file__)
        restarted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(restarted)
        with mock.patch("session_notice.notify", side_effect=AssertionError("must not resend")):
            result = restarted.submit_selected_response(envelope, {"answers": {"message-response": ["retry"]}})
        self.assertFalse(result["support"]["supported"])
        self.assertEqual(result["state"], "applied")

    def prompt(self, signal, text):
        """The real prompt hook with context rendered, as each harness adapter calls it."""
        import session_tidy as tidy
        seat = self.f.seat(signal["harness"], signal["session_id"])
        lines = []
        with mock.patch.object(tidy, "resolve_seat", return_value=seat):
            tidy.run_hook(signal["harness"], "prompt", signal["session_id"], cwd=str(self.f.root),
                          prompt_text=text, env=os.environ, render_context=True, emit=lines.append)
        return "\n".join(lines)

    def test_choice_question_closes_only_on_one_exact_choice_prompt(self):
        choices = ["Alpha plan", "Beta plan", "Gamma plan"]
        kept = ("질문 전송 중이라고 뜨고 바로 안가네..", "0", "4", "12", "1 2", "1, 2",
                "2번으로 하자 근데 왜 안 가지", "Beta plan 말고 Alpha plan", "Beta", "-2", "2-")
        closed = {"2": "Beta plan", "2번": "Beta plan", "2.": "Beta plan", " 2 번 ": "Beta plan",
                  "1": "Alpha plan", "3.": "Gamma plan", "Beta plan": "Beta plan",
                  "  beta PLAN.  ": "Beta plan", "gamma plan。": "Gamma plan"}
        for harness in ("claude", "codex", "opencode"):
            for index, text in enumerate(kept):
                with self.subTest(harness=harness, kept=text):
                    signal, envelope = self.f.ask(harness, text="kept %d" % index, choices=choices)
                    context = self.prompt(signal, text)
                    self.assertEqual(self.source(signal)["lifecycle"], "open")
                    self.assertIn("[답변 필요] 질문 %s이 아직 열려 있습니다" % signal["id"], context)
                    self.assertIn("questions answer %s" % signal["id"], context)
                    self.assertEqual(state.read_selected_request(envelope)["state"], "open")
            for index, (text, answer) in enumerate(closed.items()):
                with self.subTest(harness=harness, closed=text):
                    signal, envelope = self.f.ask(harness, text="closed %d" % index, choices=choices)
                    context = self.prompt(signal, text)
                    completion = self.source(signal)["completion"]
                    self.assertEqual((completion["actor"], completion["answer"]), ("direct", answer))
                    self.assertNotIn(signal["id"], context)

    def test_ambiguous_choice_wording_or_number_leaves_the_question_open(self):
        signal, _ = self.f.ask(text="same key", choices=["Ship", "ship."])
        self.prompt(signal, "ship")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        signal, _ = self.f.ask(text="number wording", choices=["2", "two"])
        self.prompt(signal, "1")   # number 1 is "2"; wording "1" matches none
        self.assertEqual(self.source(signal)["completion"]["answer"], "2")
        signal, _ = self.f.ask(text="number clash", choices=["2", "two"])
        self.prompt(signal, "2")   # number 2 is "two" but wording "2" is choice 1
        self.assertEqual(self.source(signal)["lifecycle"], "open")

    def test_claimed_panel_choice_is_kept_against_later_prompts(self):
        for harness in ("claude", "codex", "opencode"):
            signal, envelope = self.f.ask(harness, text="panel " + harness, choices=["A", "B", "C"])
            self.f.submit(envelope, "B")
            wire, _ref, recipient = self.f.prepare(signal)
            for text in ("질문 전송 중이라고 뜨고 바로 안가네..", "1", "C"):
                context = self.prompt(signal, text)
                self.assertEqual(self.source(signal)["lifecycle"], "open")
                self.assertIn("Fleet에서 고른 답이 전달 대기 중입니다", context)
            self.f.peer.receive_peer_message(wire, recipient)
            completion = self.source(signal)["completion"]
            self.assertEqual((completion["actor"], completion["answer"]), ("fleet", "B"))

    def _prompt_with_unread_claims(self, signal, text):
        import errno
        from unittest import mock
        read, pending, faults = Path.read_text, state.message_answer_pending, []

        def fail_once(path, *args, **kwargs):
            if path == state._cache_path() and not faults:
                faults.append(path)
                raise OSError(errno.EIO, "claim record unreadable")
            return read(path, *args, **kwargs)

        def unread(value):
            # Only the claim check that decides this prompt meets the one failed read.
            with mock.patch.object(Path, "read_text", fail_once):
                return pending(value)
        with mock.patch.object(state, "message_answer_pending", side_effect=unread):
            context = self.prompt(signal, text)
        self.assertEqual(len(faults), 1)
        return context

    def test_unreadable_claim_record_never_lets_a_prompt_take_the_panel_answer(self):
        for harness in ("claude", "codex", "opencode"):
            for choices, text in ((["A", "B", "C"], "1"), ([], "ordinary unrelated prompt")):
                with self.subTest(harness=harness, choices=bool(choices)):
                    signal, envelope = self.f.ask(harness, text="unread " + harness, choices=choices)
                    self.f.submit(envelope, "B")
                    wire, _ref, recipient = self.f.prepare(signal)
                    self._prompt_with_unread_claims(signal, text)
                    self.assertEqual(self.source(signal)["lifecycle"], "open")
                    self.assertEqual(state._selected_entry(envelope)["response"],
                                     {"answers": {"message-response": ["B"]}})
                    self.f.peer.receive_peer_message(wire, recipient)
                    completion = self.source(signal)["completion"]
                    self.assertEqual((completion["actor"], completion["answer"]), ("fleet", "B"))

    def test_unreadable_claim_record_only_defers_a_prompt_until_the_next_readable_one(self):
        signal, _ = self.f.ask(text="free unread")
        self._prompt_with_unread_claims(signal, "first answer")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.prompt(signal, "second answer")
        completion = self.source(signal)["completion"]
        self.assertEqual((completion["actor"], completion["answer"]), ("direct", "second answer"))

    def _prompt_with_claims_missing_at_lookup(self, signal, text):
        from unittest import mock
        pending, seen = state.message_answer_pending, []

        def missing(value):
            # Storage fault: the record is away only while the prompt checks it.
            path = state._cache_path()
            held = path.with_suffix(".held")
            path.rename(held)
            try:
                seen.append(path.exists())
                return pending(value)
            finally:
                held.rename(path)
        with mock.patch.object(state, "message_answer_pending", side_effect=missing):
            self.prompt(signal, text)
        self.assertTrue(seen)
        self.assertNotIn(True, seen)

    def test_claim_record_missing_at_lookup_never_lets_a_prompt_take_the_panel_answer(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness):
                signal, envelope = self.f.ask(harness, text="missing " + harness, choices=["A", "B", "C"])
                self.f.submit(envelope, "B")
                wire, _ref, recipient = self.f.prepare(signal)
                self._prompt_with_claims_missing_at_lookup(signal, "1")
                self.assertEqual(self.source(signal)["lifecycle"], "open")
                self.f.peer.receive_peer_message(wire, recipient)
                completion = self.source(signal)["completion"]
                self.assertEqual((completion["actor"], completion["answer"]), ("fleet", "B"))
                self.assertEqual(state._selected_entry(envelope)["result"]["status"], "applied")

    def test_without_a_claim_a_missing_record_still_lets_the_prompt_answer(self):
        for harness in ("claude", "codex", "opencode"):
            with self.subTest(harness=harness, record="missing-at-lookup"):
                signal, _ = self.f.ask(harness, text="no claim " + harness, choices=["A", "B", "C"])
                self._prompt_with_claims_missing_at_lookup(signal, "1")
                completion = self.source(signal)["completion"]
                self.assertEqual((completion["actor"], completion["answer"]), ("direct", "A"))
            with self.subTest(harness=harness, record="never-written"):
                signal, _ = self.f.ask(harness, text="no record " + harness, choices=["A", "B", "C"])
                state._cache_path().unlink(missing_ok=True)
                self.prompt(signal, "2")
                completion = self.source(signal)["completion"]
                self.assertEqual((completion["actor"], completion["answer"]), ("direct", "B"))

    def test_question_without_choices_still_takes_the_next_prompt(self):
        for harness in ("claude", "codex", "opencode"):
            signal, _ = self.f.ask(harness, text="free " + harness)
            context = self.prompt(signal, "아무 말이나 답")
            completion = self.source(signal)["completion"]
            self.assertEqual((completion["actor"], completion["answer"]), ("direct", "아무 말이나 답"))
            self.assertNotIn(signal["id"], context)

    def test_verified_answer_provenance_is_body_free_and_does_not_become_physical_input(self):
        import session_tidy as tidy
        signal, envelope = self.f.ask()
        self.f.submit(envelope, "PRIVATE-VERIFIED-ANSWER")
        wire, _ref, recipient = self.f.prepare(signal)
        origin = self.f.peer.prompt_origin(wire, signal["harness"], signal["session_id"])
        self.assertEqual(origin["kind"], "human-answer")
        self.assertNotIn("PRIVATE-VERIFIED-ANSWER", json.dumps(origin))
        self.f.direct(signal, wire)
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        seat = self.f.seat()
        prompt = tidy.read_json(tidy._prompt_seq_path(seat))
        self.assertEqual(prompt["source"], "peer")
        self.assertEqual(prompt["request_origin"]["kind"], "human-answer")
        self.assertEqual(tidy.last_human_prompt_seq(seat), 0)
        self.f.peer.receive_peer_message(wire, recipient)
        self.assertEqual(self.source(signal)["completion"]["actor"], "fleet")

    def test_prompt_between_claim_and_notice_write_keeps_the_claimed_answer(self):
        import session_notice
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "claimed draft")
        notify = session_notice.notify
        def prompt_first(*args, **kwargs):
            self.f.direct(signal, "physical prompt before carrier")
            return notify(*args, **kwargs)
        with mock.patch.object(session_notice, "notify", side_effect=prompt_first):
            result = self.f.submit(envelope)
        self.assertEqual(result["state"], "submitted")
        self.assertIsNotNone(self.f.record(signal))
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.assertEqual(state._selected_entry(envelope)["action_id"], "fixture-action")

    def test_decision_queue_failure_does_not_reverse_answer_and_normal_flush_retries(self):
        import tidy_decisions
        signal, envelope = self.f.ask()
        self.f.submit(envelope)
        wire, _ref, recipient = self.f.prepare(signal)
        with mock.patch.object(tidy_decisions, "save_pending", side_effect=OSError("private decision queue unavailable")):
            self.assertEqual(self.f.peer.receive_peer_message(wire, recipient), 0)
        self.assertEqual(self.source(signal)["lifecycle"], "answered")
        self.assertEqual(tidy_decisions.list_pending(), [])
        state.flush_message_decisions()
        pending = tidy_decisions.list_pending()
        self.assertEqual(len(pending), 1)
        state.flush_message_decisions()
        self.assertEqual(tidy_decisions.list_pending(), pending)

    def test_notice_persisted_then_reply_lost_keeps_claim_and_normal_courier_recovers(self):
        import peer_obligations
        import session_notice
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "keep ambiguous draft")
        with mock.patch.object(peer_obligations, "ensure_normal_runner",
                               side_effect=OSError("runner reply lost after notice persistence")):
            result = self.f.submit(envelope)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(self.f.record(signal)["state"], "pending")
        self.assertEqual(state._selected_entry(envelope)["draft"], "keep ambiguous draft")
        with mock.patch.object(session_notice, "notify", side_effect=AssertionError("ambiguous submit must not retransmit")):
            again = self.f.submit(envelope, "retry", "different-action")
        self.assertEqual(again["action_id"], "fixture-action")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        # The normal courier recovers the retained notice once through the
        # recipient's session-shell host (the shared fixture's stand-in).
        calls, _ref, pending = self.f.courier(signal)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["state"], "queued")
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.f.consume(signal, pending["text"])
        self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
        self.assertEqual(state._selected_entry(envelope)["state"], "applied")

    def test_real_prompt_and_exact_receiver_race_apply_the_claimed_answer_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import tidy_decisions
        for harness in ("claude", "codex", "opencode"):
            signal, envelope = self.f.ask(harness)
            self.f.submit(envelope, "fleet " + harness)
            wire, _ref, recipient = self.f.prepare(signal)
            barrier = threading.Barrier(2)
            applied = []
            complete = state._complete_message_locked
            def observe(*args, **kwargs):
                result = complete(*args, **kwargs)
                if result:
                    applied.append(args[2])
                return result
            def direct():
                barrier.wait(timeout=5)
                self.f.direct(signal, "direct " + harness)
            def fleet():
                barrier.wait(timeout=5)
                self.f.peer.receive_peer_message(wire, recipient)
            with mock.patch.object(state, "_complete_message_locked", side_effect=observe), ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(direct), pool.submit(fleet)]
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(len(applied), 1)
            # The claimed Fleet answer is the answer; the racing prompt never replaces it.
            self.assertEqual(self.source(signal)["completion"]["actor"], "fleet")
        self.assertEqual(len(tidy_decisions.list_pending()), 3)

    def test_consumed_ack_source_and_overlay_write_failures_recover_on_normal_runner(self):
        import peer_obligations
        for failure in ("source", "overlay"):
            with self.subTest(failure=failure):
                signal, envelope = self.f.ask(text="recover " + failure,
                    incarnation={"pid": os.getpid(), "proc_start": state._proc_start_ticks(os.getpid())})
                self.f.submit(envelope)
                wire, ref, recipient = self.f.prepare(signal)
                original_write, original_cache = state._write, state._write_cache_value
                def source_write(path, value):
                    if path == state._signal_path(signal) and value.get("lifecycle") == "answered":
                        raise OSError("injected source store failure after exact ACK")
                    return original_write(path, value)
                def cache_write(value):
                    key = state._selected_entry_key(state._selected_identity(envelope))
                    if (value.get("selected_responses", {}).get(key) or {}).get("state") == "applied":
                        return False
                    return original_cache(value)
                with mock.patch.object(state, "_write", side_effect=source_write if failure == "source" else original_write), \
                     mock.patch.object(state, "_write_cache_value", side_effect=cache_write if failure == "overlay" else original_cache):
                    self.f.peer.receive_peer_message(wire, recipient)
                self.assertEqual(self.f.peer._read_pending(ref)["state"], "received")
                self.assertEqual(self.f.record(signal)["state"], "acked")
                # Run the existing runner's ACK branch after a fresh module import.
                spec = importlib.util.spec_from_file_location("message_recovery_steward", Path(state.__file__).with_name("peer-steward.py"))
                runner = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = runner
                self.addCleanup(sys.modules.pop, spec.name, None)
                spec.loader.exec_module(runner)
                store = peer_obligations.ObligationStore(self.f.root)
                with mock.patch.object(runner, "cmd_prompt", side_effect=AssertionError("must not retransmit")), \
                     mock.patch.object(state, "_proc_start_ticks", return_value="process-was-replaced-after-consumption"):
                    runner._resume_session_notice_obligations(store.list(kinds={"run-notice"}), store)
                self.assertEqual(self.source(signal)["lifecycle"], "answered")
                self.assertEqual(state._selected_entry(envelope)["state"], "applied")
                self.assertEqual(self.source(signal)["completion"]["action_id"], "fixture-action")
                import tidy_decisions
                self.assertTrue(any(payload["question"] == "recover " + failure
                                    for _path, payload in tidy_decisions.list_pending()))

    def test_wrong_binding_body_missing_or_tampered_never_completes_or_retargets(self):
        import peer_obligations
        signal, envelope = self.f.ask()
        state.write_selected_draft(envelope, "keep draft")
        self.f.submit(envelope)
        wire, ref, recipient = self.f.prepare(signal)
        record = self.f.record(signal)
        binding = record["receipt"]["answer_binding"]
        for key, wrong in (("request_id", "S4:wrong"), ("question_id", "other"),
                           ("action_id", "other-action"), ("body_sha256", "0" * 64),
                           ("body_bytes", 1), ("sequence", 9), ("session_id", "another")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                state.message_answer_body({**binding, key: wrong})
        forged = self.f.peer._read_pending(ref)
        forged["producer_binding"] = {**forged["producer_binding"], "records": [
            {**forged["producer_binding"]["records"][0], "receipt_digest": "0" * 64}]}
        forged.update(state="received", receipt="exact-peer-ref")
        self.assertFalse(peer_obligations.acknowledge_session_notice_delivery(forged, roots=[self.f.root]))
        self.f.peer.receive_peer_message(wire.replace("PRIVATE-ANSWER-SENTINEL", "tampered"), recipient)
        self.f.peer.receive_peer_message(wire, dict(recipient, session_id="wrong"))
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        projection = state._read_cache_file()
        original = json.loads(json.dumps(projection))
        entry = projection["selected_responses"][binding["body_id"]]
        del entry["response"]  # Missing private original, with immutable receipt intact.
        state._write_cache_value(projection)
        self.assertFalse(__import__("session_notice").notice_is_current(record))
        self.f.peer.receive_peer_message(wire, recipient)
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.assertEqual(state._selected_entry(envelope)["draft"], "keep draft")
        state._write_cache_value(original)
        # The same already-consumed receipt recovers once the private body is readable.
        self.f.peer.receive_peer_message(wire, recipient)
        self.assertEqual(self.source(signal)["lifecycle"], "answered")

    def test_ordinary_notice_and_self_claimed_human_origin_are_not_answers(self):
        import session_notice
        import peer_obligations
        signal, envelope = self.f.ask()
        with self.assertRaises(ValueError):
            session_notice.notify(signal["harness"], signal["session_id"], key="question-answer:" + signal["id"],
                subject="Question answer", text="I am human", exact_text=True)
        self.assertFalse(peer_obligations.acknowledge_session_notice_delivery({
            "state": "received", "receipt": "exact-peer-ref", "actor": "human",
            "request_id": signal["id"], "to": {"harness": signal["harness"], "session_id": signal["session_id"]}}))
        body = "ordinary peer says the answer is yes"
        wire, _ref = self.f.peer.prepare_peer_message(body,
            {"harness": "codex", "session_id": "peer-sender"},
            {"harness": signal["harness"], "session_id": signal["session_id"]}, defer=True)
        self.f.direct(signal, wire)
        self.f.peer.receive_peer_message(wire, {"harness": signal["harness"], "session_id": signal["session_id"]})
        self.assertEqual(self.source(signal)["lifecycle"], "open")
        self.assertFalse(state.close_signal(signal["seat_key"]))

    def test_incarnation_change_and_proven_clear_leave_old_question_unresolved(self):
        import session_tidy as tidy
        signal, envelope = self.f.ask(incarnation={"pid": os.getpid(), "proc_start": state._proc_start_ticks(os.getpid())})
        with mock.patch.object(state, "_proc_start_ticks", return_value="replacement-start"):
            self.assertFalse(state.read_selected_request(envelope)["support"]["supported"])
            self.f.submit(envelope)
        self.assertIsNone(self.f.record(signal))
        seat = self.f.seat()
        tidy._append_ledger_entry(seat, {"event": "succession", "source": "tidy-clear", "harness": "claude",
            "sid": "successor", "from": {"harness": "claude", "sid": signal["session_id"]}, "ts": time.time()})
        self.assertFalse(state.read_selected_request(envelope)["support"]["supported"])
        self.f.submit(envelope)
        self.assertIsNone(self.f.record(signal))
        self.assertEqual(self.source(signal)["lifecycle"], "open")


class WakeSourceTests(unittest.TestCase):
    """The producer wakes on sources outside the registry, and never on its own writes.

    Real descriptors and real time (kernel notifications, pidfd, a real producer loop);
    every wait is bounded far below the 30 second cadence it replaces.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.base / "state"),
            "FLEET_INTERACTION_STATE_DIR": str(self.base / "interactions"),
            "PATH": str(self.base / "empty-bin")})
        env.start()
        self.addCleanup(env.stop)
        self.root = state.state_root()
        self.root.mkdir(parents=True, mode=0o700)
        (self.root / ".demand").touch()
        self.registry = self.base / "reg" / "jobs.log"
        self.registry.parent.mkdir()
        self.registry.write_text("open\n", encoding="utf-8")
        self.stream = self.base / "reg" / "logs" / "e2e.claude.jsonl"
        self.stream.parent.mkdir()
        self.stream.write_text("{}\n", encoding="utf-8")
        interactions = self.base / "interactions" / "claude"
        interactions.mkdir(parents=True)
        self.interactions = interactions
        import tools.fleet.collectors.dispatch as dispatch
        for patch in (
                mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[str(self.registry)]),
                mock.patch.object(observation, "_codex_rollout_waits",
                                  return_value={"complete": True, "pending": {}}),
                mock.patch.object(state, "_request_producer"),
                mock.patch.object(state, "_WAKE_FAILED", False),
                mock.patch.object(state, "_WAKE_EVENTS", None),
                mock.patch.object(state, "_NEXT_WAKE", None),
                mock.patch.object(state, "_WATCH_STAT", ())):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(state._stop_wake_events)
        self.sleepers = []
        self.addCleanup(self._reap)

    def _reap(self):
        for process in self.sleepers:
            process.kill()
            process.wait()

    def sleeper(self):
        from tools.fleet.collectors.procscan import read_proc_start
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.sleepers.append(process)
        return process, read_proc_start(process.pid)

    def snapshot(self, *, pid=None, start=None):
        job = SimpleNamespace(status="open", harness="claude", _transcript_path=str(self.stream),
                              pid=pid, proc_start=start)
        return SimpleNamespace(jobs=[job], sessions=[])

    def arm(self, snapshot=None):
        state._remember_wake_sources(snapshot)
        state._arm_wake_events()
        events = state._wake_events()
        self.assertIsNotNone(events)
        return events

    def slept(self, cause, *, delay=30.0):
        """Seconds `_sleep_until_registry_change` took when `cause` ran 0.3 s into it."""
        generation = state._source_generation()
        timer = threading.Timer(0.3, cause)
        timer.start()
        began = time.monotonic()
        state._sleep_until_registry_change(delay, generation, 0.0)
        timer.join()
        return time.monotonic() - began

    def test_a_worker_stream_append_wakes_the_producer(self):
        self.arm(self.snapshot())
        def append():
            with self.stream.open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
        self.assertLess(self.slept(append), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "event")

    def test_an_s4_signal_wakes_the_producer(self):
        self.arm(self.snapshot())
        def signal():
            value = state.signal_value("claude", "sid-wake", "seat-wake", "[답변 필요] 깨움?", sequence=1)
            self.assertTrue(state.write_signal(value))
        self.assertLess(self.slept(signal), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "event")

    def test_an_s1_state_file_wakes_the_producer(self):
        self.arm(self.snapshot())
        self.assertLess(self.slept(lambda: (self.interactions / "sid-s1.json").write_text("{}")), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "event")

    def test_an_owner_process_ending_wakes_the_producer(self):
        process, start = self.sleeper()
        self.arm(self.snapshot(pid=process.pid, start=start))
        self.assertIn((process.pid, start), state._NEXT_WAKE[2])
        self.assertLess(self.slept(process.terminate), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "event")

    def test_a_network_file_is_stat_watched_not_notification_watched(self):
        import claude_process_identity as identity
        remote = str(self.base / "reg" / "logs")
        real = identity.is_local_path
        with mock.patch.object(identity, "is_local_path",
                               side_effect=lambda path, **kw: not str(path).startswith(remote) and real(path, **kw)):
            events = self.arm(self.snapshot())
        self.assertIn(str(self.stream), state._NEXT_WAKE[3])
        self.assertNotIn(str(self.stream), state._NEXT_WAKE[0])
        self.assertIn(str(self.stream), state._WATCH_STAT)
        self.assertNotIn(str(self.stream), events._sources[0])
        def append():
            with self.stream.open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
        self.assertLess(self.slept(append), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "stat")

    def test_the_producers_own_writes_do_not_wake_it(self):
        events = self.arm(self.snapshot())
        self.assertFalse(events.wait_changed(0.3))
        now = time.time()
        for index in range(3):
            self.assertTrue(state.publish_cache({"items": [], "complete": True, "observed_at": now + index,
                                                 "route_sessions": {}, "route_status": {}}))
            state._write(self.root / ".demand", {"claude\0sid": now})   # a display's demand
            (self.root / ".demand").touch()
            with state._cache_write_lock():
                pass
        self.assertTrue((self.root / "fleet.json").exists() or (self.root / "projection.json").exists())
        self.assertFalse(events.wait_changed(1.0), "a cache write, demand update or lock woke the producer")

    def test_without_a_notification_the_whole_delay_is_slept(self):
        self.arm(self.snapshot())
        self.assertGreaterEqual(self.slept(lambda: None, delay=2.0), 1.9)
        self.assertEqual(state._LAST_WAKE_REASON, "cadence")

    def test_a_missing_kernel_notification_falls_back_to_the_stat_beat(self):
        from tools.fleet import native_events
        with mock.patch.object(native_events.ctypes, "CDLL", side_effect=OSError("no inotify")):
            events = self.arm(self.snapshot())
            self.assertFalse(events.available)
            state._remember_wake_sources(self.snapshot())
        self.assertIn(str(self.stream), state._WATCH_STAT)
        def append():
            with self.stream.open("a", encoding="utf-8") as handle:
                handle.write("{}\n")
        self.assertLess(self.slept(append), 5.0)
        self.assertEqual(state._LAST_WAKE_REASON, "stat")

    def test_stopping_leaves_no_thread_or_descriptor(self):
        process, start = self.sleeper()
        before = len(os.listdir("/proc/self/fd"))
        self.arm(self.snapshot(pid=process.pid, start=start))
        self.assertTrue(any(t.name == "fleet-native-events" for t in threading.enumerate()))
        state._stop_wake_events()
        self.assertFalse(any(t.name == "fleet-native-events" and t.is_alive() for t in threading.enumerate()))
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    # (S-d) the source list is a pure, stable function of what the pass read
    def test_wake_sources_are_stable_sorted_and_keep_missing_streams(self):
        process, start = self.sleeper()
        dead = SimpleNamespace(status="open", harness="claude", _transcript_path=str(self.base / "gone" / "x.jsonl"),
                               pid=process.pid, proc_start="not-its-start")
        live = SimpleNamespace(status="open", harness="claude", _transcript_path=str(self.stream),
                               pid=process.pid, proc_start=start)
        ended = SimpleNamespace(status="done", harness="claude", _transcript_path=str(self.base / "old.jsonl"),
                                pid=None, proc_start=None)
        first = state._wake_sources(SimpleNamespace(jobs=[dead, live, ended], sessions=[]))
        again = state._wake_sources(SimpleNamespace(jobs=[ended, live, dead], sessions=[]))
        self.assertEqual(first, again)
        self.assertEqual(first[2], ((process.pid, start),))              # the process that is no longer the one is left out
        self.assertIn(str(self.base / "gone" / "x.jsonl"), first[0])     # a stream not there yet stays listed
        self.assertNotIn(str(self.base / "old.jsonl"), first[0])         # a finished job is not watched
        for part in first:
            self.assertEqual(list(part), sorted(part))
        self.assertEqual(state._wake_sources(None), state._wake_sources(None))

    # the cost of naming the sources must not grow with how many directories the pass read
    def test_a_pass_with_many_directories_reads_the_mount_table_once(self):
        import claude_process_identity as identity
        jobs = []
        for index in range(60):
            directory = self.base / "many" / f"d{index}"
            directory.mkdir(parents=True)
            jobs.append(SimpleNamespace(status="open", harness="claude", pid=None, proc_start=None,
                                        _transcript_path=str(directory / "s.jsonl")))
        reads = []
        real = identity.read_mount_table
        with mock.patch.object(identity, "read_mount_table", side_effect=lambda *a, **k: reads.append(1) or real(*a, **k)):
            sources = state._wake_sources(SimpleNamespace(jobs=jobs, sessions=[]))
        self.assertEqual(len(reads), 1)
        self.assertEqual(sum(1 for path in sources[0] if "/many/" in path), 60)
        reads.clear()
        with mock.patch.object(identity, "read_mount_table", side_effect=lambda *a, **k: reads.append(1) or real(*a, **k)):
            state._wake_sources(SimpleNamespace(jobs=jobs, sessions=[]))
        self.assertEqual(len(reads), 1)       # nothing is kept: the next call reads the table again

    def test_the_mount_table_gives_the_same_answer_as_the_one_path_read(self):
        import claude_process_identity as identity
        net, inner, spaced = self.base / "net", self.base / "net" / "inner", self.base / "with space"
        for directory in (net / "x", inner / "y", spaced / "z", self.base / "plain"):
            directory.mkdir(parents=True, exist_ok=True)
        mounts = self.base / "mounts"
        mounts.write_text("\n".join([
            "rootfs / ext4 rw 0 0", f"srv:/a {net} nfs4 rw 0 0", f"/dev/b {inner} ext4 rw 0 0",
            f"tmpfs {str(spaced).replace(' ', chr(92) + '040')} tmpfs rw 0 0",
            f"dup {net} ext4 rw 0 0", "short row", ""]), encoding="utf-8")
        table = identity.read_mount_table(mounts)
        link = self.base / "link"
        link.symlink_to(net / "x")
        cases = {self.base / "plain": True, net / "x": False, net: False, inner / "y": True, inner: True,
                 spaced / "z": True, link: False, self.base / "missing" / "q": False}
        for path, local in cases.items():
            with self.subTest(path=str(path)):
                self.assertEqual(identity.is_local_path(path, mounts_path=mounts), local)
                self.assertEqual(identity.is_local_path(path, mount_table=table), local)
        # a table that could not be read, and a path no row holds, are not local
        self.assertIsNone(identity.read_mount_table(self.base / "absent"))
        self.assertFalse(identity.is_local_path(self.base / "plain", mounts_path=self.base / "absent"))
        self.assertFalse(identity.is_local_path(self.base / "plain", mount_table={"/nowhere": "ext4"}))

    # the producer's own passes: the number of passes is the cadence's, not the wakes'
    def _run_producer(self, seconds, *, delay=None, vary=False, snapshot=None):
        """Run the real producer loop (real passes, real listener) for `seconds`.

        Returns (pass start times, rebuild times). `delay` replaces the sleep length; `vary`
        makes the next pass's wake sources differ from the last one's every time."""
        lock = os.open(self.base / "producer.lock", os.O_CREAT | os.O_RDWR, 0o600)
        began = time.monotonic()
        passes, rebuilds = [], []
        original_refresh, original_sleep = state._timed_refresh, state._sleep_until_registry_change
        original_remember = state._remember_wake_sources
        from tools.fleet import native_events
        original_rebuild = native_events.NativeDisplayEvents._rebuild
        counter = [0]

        def timed():
            passes.append(time.monotonic() - began)
            return original_refresh()

        def sleep(wait, generation, ready_at):
            return original_sleep(wait if delay is None else delay, generation, ready_at)

        def remember(snap=None):
            original_remember(snapshot if snapshot is not None else snap)
            if vary:
                counter[0] += 1
                local = state._NEXT_WAKE[0] + (str(self.base / ("varying-%d" % counter[0])),)
                state._NEXT_WAKE = (tuple(sorted(local)),) + state._NEXT_WAKE[1:]

        def rebuild(this, *args):
            rebuilds.append(time.monotonic() - began)
            return original_rebuild(this, *args)

        def run():
            state._producer_loop(lock)
        with mock.patch.object(state, "_timed_refresh", timed), \
             mock.patch.object(state, "_sleep_until_registry_change", sleep), \
             mock.patch.object(state, "_remember_wake_sources", remember), \
             mock.patch.object(native_events.NativeDisplayEvents, "_rebuild", rebuild):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            time.sleep(seconds)
            kept = (list(passes), list(rebuilds))   # before the teardown below rebuilds too
            shutil.rmtree(self.root)          # the producer ends when its root is gone
            thread.join(65)
            self.assertFalse(thread.is_alive())
        return kept

    def test_S_b_the_first_listener_setup_starts_no_extra_pass(self):
        process, start = self.sleeper()
        passes, _ = self._run_producer(5.0, snapshot=self.snapshot(pid=process.pid, start=start))
        self.assertEqual(len(passes), 1, passes)

    def test_S_c_changing_sources_do_not_make_wakes_chain(self):
        # Every pass hands the listener a different source list (a rebuild each time). The pass
        # count stays the cadence's: 1 + floor(15 / 3).
        passes, rebuilds = self._run_producer(15.0, delay=3.0, vary=True)
        self.assertGreaterEqual(len(rebuilds), len(passes) - 1)   # it did rebuild on the passes
        self.assertLessEqual(len(passes), 1 + 15 // 3, passes)

    def test_S_a_a_resting_producer_runs_only_its_cadence(self):
        # 59 seconds with nothing changing: passes at t~0 and t~30, the real publish writes
        # (projection, fleet cache, locks, demand) wake nothing, and the listener is built for the
        # first pass's sources, then once more when that pass added the stream and the process
        # (the sources differ); after the second pass's setup it is never rebuilt.
        process, start = self.sleeper()
        passes, rebuilds = self._run_producer(59.0, snapshot=self.snapshot(pid=process.pid, start=start))
        self.assertEqual(len(passes), 2, passes)
        self.assertLessEqual(len(rebuilds), 2, rebuilds)
        self.assertEqual([t for t in rebuilds if t > passes[1] + 0.5], [])
        self.assertLessEqual(len([t for t in rebuilds if t > 1.0]), 1)



class FinishedRouteWakeTests(unittest.TestCase):
    """Only open work wakes the producer: a finished route stays history for the periodic pass.

    The real registry reader and listener over synthetic routes that ended normally
    (``open -> done/PASS``, a matching ``closed_at``/``terminal_gate_proven`` outcome and a
    completed journal), the way they stay in a registry as its history.
    """

    def setUp(self):
        from route_identity import route_hash, route_id_from_hash
        import tools.fleet.collectors.dispatch as dispatch
        self.route_hash, self.route_id_from_hash = route_hash, route_id_from_hash
        self.tmp = tempfile.TemporaryDirectory(prefix="fin-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.jobs = self.base / "jobs.log"
        self.jobs.touch()
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.base), "XDG_STATE_HOME": str(self.base / "state"),
            "FLEET_INTERACTION_STATE_DIR": str(self.base / "interaction"),
            "AGENT_DISPATCH_JOBS": str(self.jobs), "AGENT_PEER_LEDGER_ROOT": str(self.base),
            "PATH": str(self.base / "empty-bin")})
        env.start()
        self.addCleanup(env.stop)
        state.state_root().mkdir(parents=True, mode=0o700)
        for patch in (
                mock.patch.object(dispatch, "human_gate_registry_paths", return_value=[str(self.jobs)]),
                mock.patch.object(observation, "_codex_rollout_waits",
                                  return_value={"complete": True, "pending": {}}),
                mock.patch.object(observation, "_session_liveness_probe", return_value=lambda *a: "ended"),
                mock.patch.object(observation, "_LAST_PASS_ROUTE_PATHS", ()),
                mock.patch.object(state, "_request_producer"),
                mock.patch.object(state, "_WAKE_FAILED", False),
                mock.patch.object(state, "_WAKE_EVENTS", None),
                mock.patch.object(state, "_NEXT_WAKE", None),
                mock.patch.object(state, "_WATCH_STAT", ())):
            patch.start()
            self.addCleanup(patch.stop)
        observation._WAIT_MEMO.clear()
        self.addCleanup(observation._WAIT_MEMO.clear)
        self.addCleanup(state._stop_wake_events)
        self.count = 0

    def route(self, *, finished=True, journal_complete=True):
        """One route in the registry; ``finished`` closes it the way a normal completion does."""
        index = self.count
        self.count += 1
        folder = self.base / str(index)
        (folder / "artifacts").mkdir(parents=True)
        record = {"artifact_root": str(folder / "artifacts"), "nodes": []}
        record["route_hash"] = self.route_hash(record)
        record["route_id"] = self.route_id_from_hash(record["route_hash"])
        route_file = folder / "route.json"
        route_file.write_text(json.dumps(record), encoding="utf-8")
        journal = self.base / "workflow" / record["route_id"] / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text("", encoding="utf-8")
        meta = ",".join(("attempt_id=att-fin-%s-%d" % (self.base.name, index), "worker_type=owner",
                         "harness=claude", "parent_sid=synthetic", "route_file=" + str(route_file),
                         "route_id=" + record["route_id"], "route_hash=" + record["route_hash"]))
        self.append("100\topen\t/synthetic\t/synthetic\towner\t" + meta + "\n")
        if finished:
            self.close(record, route_file, journal if journal_complete else None)
            self.append("200\tdone\t/synthetic\t/synthetic\towner\t" + meta + ",verdict=PASS\n")
        return {"record": record, "file": route_file, "journal": journal, "meta": meta}

    def close(self, record, route_file, journal):
        route_file.with_name("route.outcome.json").write_text(json.dumps({
            "route_id": record["route_id"], "route_hash": record["route_hash"],
            "closed_at": "2026-11-13T00:00:00Z", "terminal_gate_proven": True}), encoding="utf-8")
        if journal is not None:
            journal.write_text(json.dumps({
                "at": "2026-11-13T00:00:00Z", "route_id": record["route_id"],
                "route_hash": record["route_hash"], "workflow_state": "COMPLETE"}) + "\n", encoding="utf-8")

    def append(self, line):
        with self.jobs.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def pass_once(self):
        """One producer pass: read the waits, then arm what it would wake on."""
        observed = observation.read_answer_waits(jobs_paths=[self.jobs], now=time.time())
        self.assertTrue(observed["complete"])
        state._remember_wake_sources(SimpleNamespace(jobs=[], sessions=[]))
        state._arm_wake_events()
        return observed, set(state._NEXT_WAKE[0])

    def test_finished_routes_add_no_watch_however_many_stay_in_the_registry(self):
        _observed, idle = self.pass_once()             # nothing open: the base sources only
        routes = [self.route() for _ in range(30)]
        observed, after_30 = self.pass_once()
        self.assertEqual(observed["items"], [])
        self.assertEqual(observation.last_pass_route_paths(), ())
        self.assertEqual(after_30, idle)
        routes += [self.route() for _ in range(30)]
        _observed, after_60 = self.pass_once()
        self.assertEqual(after_60, idle)
        self.assertFalse({str(route["file"]) for route in routes} & after_60)

    def test_only_the_open_routes_sources_are_watched(self):
        opened = [self.route(finished=False) for _ in range(3)]
        finished = [self.route() for _ in range(60)]
        _observed, local = self.pass_once()
        watched = set(observation.last_pass_route_paths())
        for route in opened:
            self.assertIn(str(route["file"]), watched)
            self.assertIn(str(route["journal"]), watched)
        for route in finished:
            self.assertNotIn(str(route["file"]), watched)
            self.assertNotIn(str(route["journal"]), watched)
        self.assertTrue(all(path.startswith(tuple(str(route["file"].parent) for route in opened))
                            or path.startswith(str(self.base / "workflow"))
                            for path in watched))
        self.assertLessEqual(len(watched), 4 * len(opened))
        self.assertTrue({str(route["file"]) for route in opened} <= local)

    def test_a_finished_routes_file_change_does_not_wake_the_producer(self):
        finished = self.route()
        opened = self.route(finished=False)
        self.pass_once()
        events = state._wake_events()
        self.assertIsNotNone(events)
        self.assertFalse(events.wait_changed(0.2))
        finished["file"].write_text(finished["file"].read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with finished["journal"].open("a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertFalse(events.wait_changed(1.0), "a finished route's file woke the producer")
        with opened["journal"].open("a", encoding="utf-8") as handle:   # an open route still does
            handle.write("{}\n")
        self.assertTrue(events.wait_changed(5.0))

    def test_a_route_opened_again_is_watched_from_the_next_pass_and_wakes(self):
        # Closed by its outcome (the journal records no end): finished, not watched.
        route = self.route(journal_complete=False)
        self.pass_once()
        self.assertNotIn(str(route["file"]), observation.last_pass_route_paths())
        # It opens again: the closure is withdrawn and a new attempt starts on the same route.
        route["file"].with_name("route.outcome.json").unlink()
        self.append("300\topen\t/synthetic\t/synthetic\towner\t" +
                    route["meta"].replace("att-fin-", "att-again-") + "\n")
        _observed, local = self.pass_once()
        self.assertIn(str(route["file"]), observation.last_pass_route_paths())
        self.assertIn(str(route["journal"]), local)
        events = state._wake_events()
        self.assertFalse(events.wait_changed(0.2))
        began = time.monotonic()
        with route["journal"].open("a", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.assertTrue(events.wait_changed(5.0))
        self.assertLess(time.monotonic() - began, 5.0)      # an event, well inside the 30 s cadence


if __name__ == "__main__":
    unittest.main()
