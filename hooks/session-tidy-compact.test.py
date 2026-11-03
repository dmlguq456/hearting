#!/usr/bin/env python3
"""Compact hook contract checks with isolated state and a fake detached runner."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import session_tidy as st  # noqa: E402
import session_tidy_runner as runner  # noqa: E402

# Markers a dispatched test runner inherits; a main-session case must not see them.
WORKER_MARKERS = ("AGENT_SESSION_ROLE", "AGENT_DISPATCH_CHILD", "AGENT_DISPATCH_DEPTH",
                  "AGENT_DISPATCH_STAGE_AUTHORITY", "AGENT_DISPATCH_SUBSESSION_ID",
                  "OPENCODE_DISPATCH_SLUG", "FLEET_TITLE_REFRESH", "MEM_DISTILL")
_NODE_STDIN_EOF = None


def node_stdin_reaches_eof() -> bool:
    """Whether Node's spawnSync ``input`` reaches the child as EOF here.

    Some tool sandboxes deliver the bytes but never close the child's stdin, so any
    helper that reads its payload to the end waits until the spawn timeout. The plugin
    fixtures below need a normal host to say anything about the plugin.
    """
    global _NODE_STDIN_EOF
    if _NODE_STDIN_EOF is None:
        probe = ('const r = require("node:child_process").spawnSync("cat", [], '
                 '{ input: "x", encoding: "utf8", timeout: 2000, killSignal: "SIGKILL" }); '
                 'process.stdout.write(r.status === 0 && r.stdout === "x" ? "eof" : "hang")')
        try:
            result = subprocess.run(["node", "-e", probe], text=True, capture_output=True, timeout=10)
            _NODE_STDIN_EOF = result.stdout == "eof"
        except (OSError, subprocess.SubprocessError):
            _NODE_STDIN_EOF = False
    return _NODE_STDIN_EOF


class CompactHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-tidy-compact-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.env = {"XDG_STATE_HOME": str(self.state)}
        envpatch = mock.patch.dict("os.environ", self.env)
        envpatch.start()
        self.addCleanup(envpatch.stop)
        for key in WORKER_MARKERS:
            os.environ.pop(key, None)
        self.seat = st.Seat("pane", "fixture-seat", pane="w1:p1", harness="claude", project_key="fixture")
        self.resolve = mock.patch.object(
            st, "resolve_seat",
            side_effect=lambda harness, _cwd=None, _env=None, sid=None, _source="", **_kwargs:
                st.session_seat(self.seat, harness, sid or ""))
        self.resolve.start()
        self.addCleanup(self.resolve.stop)
        self.enqueue = mock.patch.object(runner, "enqueue_item", return_value={"id": "fixture"})
        self.enqueue_mock = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def session_seat(self, sid, harness="claude"):
        return st.session_seat(self.seat, harness, sid)

    def confirmed_handover(self, predecessor, successor, harness="claude"):
        seat = self.session_seat(successor, harness)
        now = st.now_epoch()
        with st.session_state_lock(seat):
            st.record_event(seat, harness, successor, "start", source="clear",
                            cwd=str(self.cwd), now=now)
            st._append_ledger_entry(seat, {"ts": now + 0.001, "harness": harness,
                                           "sid": successor, "event": "handover",
                                           "from": {"harness": harness, "sid": predecessor},
                                           "source": "clear"})

    def test_all_adapter_booking_entries_reject_handed_over_source_without_losing_queue(self):
        import session_tidy_clear as clear
        for harness in ("claude", "opencode", "codex"):
            with self.subTest(harness=harness), \
                 mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                 mock.patch.object(clear, "_start_helper") as helper, \
                 mock.patch.object(runner, "read_item", return_value=None):
                sid = harness + "-old"
                self.confirmed_handover(sid, harness + "-next", harness)
                context = dict(identity=harness + "-compact", trigger="auto", transcript="fixture.jsonl",
                               boundary_offset=0, card_request_required=True)
                if harness == "codex":
                    qid = runner.operation_queue_id(context["identity"])
                    self.enqueue_mock.return_value = dict(id=qid, operation_id=context["identity"])
                    result = clear.schedule_compact_card_request(self.session_seat(sid, harness), harness, sid,
                                                                 str(self.cwd), context)
                    self.assertEqual(self.enqueue_mock.call_args.kwargs["operation_id"], context["identity"])
                else:
                    st.write_card(self.seat, harness, sid, "compact card", compact_context=context)
                    result = clear.schedule_for_enqueue(self.seat, harness, sid, str(self.cwd), compact_context=context)
                self.assertEqual(result, "clear=skipped reason=session-handed-over")
                helper.assert_not_called()
                self.assertIsNone(clear.read_reservation(self.seat.key))

    def test_before_is_shared_and_does_not_touch_state(self):
        self.assertEqual(st.run_hook("claude", "compact", "sid-main", phase="before", env=self.env),
                         st.COMPACT_CARD_INSTRUCTIONS)
        self.assertEqual(st.run_hook("opencode", "compact", "sid-main", phase="before", env=self.env),
                         st.COMPACT_CARD_INSTRUCTIONS)
        self.assertEqual(st.run_hook("codex", "compact", "sid-main", phase="before", env=self.env), "")
        self.assertFalse(self.state.exists())

    def test_after_records_then_saves_then_queues_and_author_never_rereads(self):
        result = st.run_hook("claude", "compact", "sid-main", phase="after",
                             compact_summary="summary\x00\n\tkept", cwd=str(self.cwd),
                             env=self.env, now=1000)
        self.assertEqual(result, "booking=queue-only reason=trigger-unknown")
        self.assertEqual(self.enqueue_mock.call_count, 1)
        self.assertEqual(self.enqueue_mock.call_args.args[1:4], ("claude", "sid-main", str(self.cwd)))
        ledger = list((self.state / "hearting/session-tidy/sessions").glob("*.jsonl"))
        row = json.loads(ledger[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((row["event"], row["source"], row["epoch"]), ("compact", "compact", 1))
        card = st.read_latest_card(self.session_seat("sid-main"))
        self.assertEqual(card["origin"], "runtime-compact")
        self.assertEqual(card["compact"]["summary"], "summary\n\tkept")
        self.assertIn("요약이 보지 못한 최근 대화", card["body"])
        self.assertIn("원본 대화 기록 경로를 확인할 수 없습니다", card["body"])
        self.assertFalse((self.state / "hearting/session-tidy/clear").exists())
        self.assertFalse((self.state / "hearting/session-tidy/handover").exists())
        self.assertEqual(st.run_hook("claude", "prompt", "sid-main", env=self.env, now=1001), "")
        self.assertEqual(st.run_hook("claude", "start", "sid-main", source="compact",
                                     env=self.env, now=1002), "")
        self.assertEqual(st.run_hook("claude", "prompt", "sid-main", env=self.env, now=1003), "")
        self.confirmed_handover("sid-main", "sid-next")
        self.assertIn("summary", st.run_hook("claude", "start", "sid-next", env=self.env, now=1004))
        self.assertEqual(st.run_hook("claude", "prompt", "sid-next", env=self.env, now=1005), "")

    def test_empty_after_still_queues_once_without_replacing_card(self):
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="saved",
                    cwd=str(self.cwd), env=self.env, now=1000)
        card_before = st.read_latest_card(self.session_seat("sid-main"))
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary=" \x00\n ",
                    cwd=str(self.cwd), env=self.env, now=1010)
        self.assertEqual(self.enqueue_mock.call_count, 2)
        self.assertEqual(st.read_latest_card(self.session_seat("sid-main")), card_before)
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)

    def test_card_is_saved_only_at_a_session_seat_and_project_seat_queues_only(self):
        for kind in ("pane", "native"):
            seat = st.Seat(kind, f"fixture-{kind}", pane="w1:p1" if kind == "pane" else "",
                           harness="claude", project_key="fixture",
                           native={"pid": 1} if kind == "native" else None)
            with mock.patch.object(st, "resolve_seat", return_value=seat):
                st.run_hook("claude", "compact", f"sid-{kind}", phase="after", compact_summary=f"{kind} card",
                            cwd=str(self.cwd), env=self.env, now=1000)
            card = st.read_latest_card(st.session_seat(seat, "claude", f"sid-{kind}"))
            self.assertEqual((card["compact"]["summary"], card["origin"]), (f"{kind} card", "runtime-compact"))
        project = st.Seat("project", "fixture-project", harness="codex", project_key="fixture")
        with mock.patch.object(st, "resolve_seat", return_value=project):
            st.write_card(project, "codex", "sid-other", "another session's card", cwd=str(self.cwd), now=900)
            st.run_hook("codex", "compact", "sid-project", phase="after", compact_summary="project summary",
                        cwd=str(self.cwd), env=self.env, now=1000)
        kept = st.read_latest_card(st.session_seat(project, "codex", "sid-other"))
        self.assertEqual((kept["body"], kept["generation"]), ("another session's card", 1))
        self.assertNotIn("origin", kept)
        self.assertEqual(st.session_summary(project)[("codex", "sid-project")]["epoch"], 1)
        self.assertEqual(self.enqueue_mock.call_count, 3)
        self.assertEqual(self.enqueue_mock.call_args.args[0], project)
        self.assertEqual(self.enqueue_mock.call_args.args[1:3], ("codex", "sid-project"))

    def test_compact_summary_above_manual_limit_is_preserved_in_full(self):
        summary = "진행 중: " + "가" * (st.CARD_BODY_MAX_BYTES // 3 + 500)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary=summary,
                    cwd=str(self.cwd), env=self.env, now=1000)
        card = st.read_latest_card(self.session_seat("sid-main"))
        self.assertEqual(card["compact"]["summary"], summary)
        self.assertGreater(len(card["compact"]["summary"].encode("utf-8")), st.CARD_BODY_MAX_BYTES)
        self.assertLessEqual(len(card["compact"]["summary"].encode("utf-8")), st.COMPACT_SUMMARY_MAX_BYTES)
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_compact_admission_boundaries_keep_full_summary_and_reject_overflow(self):
        for size in (st.CARD_BODY_MAX_BYTES + 1, 32 * 1024 + 1, st.COMPACT_SUMMARY_MAX_BYTES):
            summary = "x" * size
            st.run_hook("claude", "compact", f"sid-{size}", phase="after", compact_summary=summary,
                        compact_context={"trigger": "manual"}, cwd=str(self.cwd), env=self.env, now=1000 + size)
            card = st.read_latest_card(self.session_seat(f"sid-{size}"))
            self.assertEqual(card["compact"]["summary"], summary)
            self.assertLessEqual(len(card["body"].encode("utf-8")), st.COMPACT_CARD_BODY_MAX_BYTES)
        before = st.read_latest_card(self.session_seat(f"sid-{st.COMPACT_SUMMARY_MAX_BYTES}"))
        too_large = "x" * (st.COMPACT_SUMMARY_MAX_BYTES + 1)
        st.run_hook("claude", "compact", "sid-too-large", phase="after", compact_summary=too_large,
                    compact_context={"trigger": "auto"}, cwd=str(self.cwd), env=self.env, now=5000)
        self.assertEqual(st.read_latest_card(self.session_seat(f"sid-{st.COMPACT_SUMMARY_MAX_BYTES}")), before)
        self.assertEqual(self.enqueue_mock.call_count, 4)

    def test_auto_compact_uses_shared_snapshot_and_three_hour_booking(self):
        import session_tidy_clear as clear
        transcript = self.root / "sid-auto.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "uuid": "a1",
                                          "message": {"stop_reason": "end_turn",
                                                      "content": [{"type": "text", "text": "완료"}]}})
                             + "\n", encoding="utf-8")
        with mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
             mock.patch.object(clear, "_start_helper", return_value=4321), \
             mock.patch.object(clear, "herdr_command", return_value="/bin/true"):
            clear.schedule_for_enqueue(self.seat, "claude", "sid-auto", str(self.cwd), no_continue=True)
            compact_now = st.now_epoch()
            st.run_hook("claude", "compact", "sid-auto", phase="after", compact_summary="완전한 요약",
                        compact_context={"trigger": "auto"}, transcript=str(transcript),
                        cwd=str(self.cwd), env=self.env, now=compact_now)
            booking = clear.read_reservation(self.seat.key)
            self.assertEqual(booking["origin"], "compact")
            self.assertEqual(booking["deadline"] - booking["created"], clear.COMPACT_CLEAR_DEADLINE_SEC)
            self.assertEqual(booking["compact"]["trigger"], "auto")
            self.assertTrue(booking["continue_off"])
            self.assertEqual(booking["compact"]["boundary_offset"], transcript.stat().st_size)
            self.assertEqual(booking["snapshot_state"], "deferred")

    def test_duplicate_auto_compact_callback_coalesces_card_queue_snapshot_and_booking(self):
        import session_tidy_clear as clear
        transcript = self.root / "sid-duplicate.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "uuid": "a1",
                                          "message": {"stop_reason": "end_turn", "content": [
                                              {"type": "text", "text": "완료"}]}}) + "\n", encoding="utf-8")
        with mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
             mock.patch.object(clear, "_start_helper", return_value=4321) as helper:
            for stamp in (1000, 1001):
                st.run_hook("claude", "compact", "sid-duplicate", phase="after", compact_summary="same summary",
                            compact_context={"trigger": "auto", "summary_id": "compact-id"},
                            transcript=str(transcript), cwd=str(self.cwd), env=self.env, now=stamp)
                if stamp == 1000:
                    with st.seat_lock(self.seat.key):
                        st.write_card(self.seat, "claude", "sid-duplicate", "later manual card",
                                      cwd=str(self.cwd), now=1000.5)
        duplicate_seat = self.session_seat("sid-duplicate")
        self.assertEqual(st.read_latest_card(duplicate_seat)["generation"], 2)
        self.assertEqual(st.read_latest_card(duplicate_seat)["body"], "later manual card")
        self.assertEqual(self.enqueue_mock.call_count, 2)  # both retries carry one operation identity
        self.assertEqual([call.kwargs.get("operation_id") for call in self.enqueue_mock.call_args_list],
                         [self.enqueue_mock.call_args_list[0].kwargs.get("operation_id")] * 2)
        self.assertTrue(self.enqueue_mock.call_args_list[0].kwargs.get("operation_id"))
        helper.assert_called_once()
        self.assertEqual(clear.read_reservation(self.seat.key)["snapshot_state"], "deferred")

    def test_ineligible_compactions_queue_without_replacing_or_creating_a_clear_booking(self):
        import session_tidy_clear as clear
        import dispatch_seat_handover as handover
        transcript = self.root / "sid-excluded.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "uuid": "a1", "message": {
            "stop_reason": "end_turn", "content": [{"type": "text", "text": "완료"}]}}) + "\n", encoding="utf-8")
        project = st.Seat("project", "fixture-project", harness="claude", project_key="fixture")
        cases = [
            ("manual", "claude", {"trigger": "manual"}, "요약", self.seat, {}, 1),
            ("unknown", "claude", {}, "요약", self.seat, {}, 1),
            ("empty", "claude", {"trigger": "auto"}, "  \n\t ", self.seat, {}, 1),
            ("codex", "codex", {"trigger": "auto"}, "요약", self.seat, {}, 1),
            ("project", "claude", {"trigger": "auto"}, "요약", project, {}, 1),
            ("worker", "claude", {"trigger": "auto"}, "요약", self.seat, {"AGENT_SESSION_ROLE": "worker"}, 0),
        ]
        for name, harness, context, summary, seat, extra, queued in cases:
            with self.subTest(name), \
                 mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
                 mock.patch.object(clear, "_start_helper", return_value=4321) as helper, \
                 mock.patch.object(handover, "write_snapshot_locked", return_value=1) as snapshot, \
                 mock.patch.object(st, "resolve_seat", return_value=seat):
                before = self.enqueue_mock.call_count
                clear.cancel(self.seat)
                held = None
                if name == "manual":
                    clear.schedule_for_enqueue(self.seat, "claude", "sid-excluded", str(self.cwd), no_continue=True)
                    held = clear.read_reservation(self.seat.key)
                    helper.reset_mock()
                st.run_hook(harness, "compact", "sid-excluded", phase="after", compact_summary=summary,
                            compact_context=context, transcript=str(transcript), cwd=str(self.cwd),
                            env={**self.env, **extra}, now=2000)
                helper.assert_not_called()
                snapshot.assert_not_called()
                self.assertEqual(self.enqueue_mock.call_count - before, queued)
                after = clear.read_reservation(self.seat.key)
                if held is None:
                    self.assertIsNone(after)
                else:
                    self.assertEqual((after["nonce"], after["continue_off"]), (held["nonce"], True))

    def test_compact_text_write_failure_restores_old_card_and_never_books(self):
        import session_tidy_clear as clear
        previous = st.write_card(self.seat, "claude", "sid-main", "old card", cwd=str(self.cwd), now=900)
        original_atomic_write = st.atomic_write
        logical_text_path = st.card_text_path(self.session_seat("sid-main"))

        def fail_text(path, data):
            if Path(path) == logical_text_path:
                raise OSError("fixture text write failed")
            return original_atomic_write(path, data)

        with mock.patch.object(clear, "herdr_command", return_value="/bin/true"), \
             mock.patch.object(clear, "_start_helper", return_value=4321), \
             mock.patch.object(st, "atomic_write", side_effect=fail_text):
            st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="new summary",
                        compact_context={"trigger": "auto"}, cwd=str(self.cwd), env=self.env, now=1000)
        self.assertEqual(st.read_latest_card(self.session_seat("sid-main"))["generation"], previous["generation"])
        self.assertIsNone(clear.read_reservation(self.seat.key))

    def test_start_compact_then_after_keeps_one_epoch_and_excludes_author_card(self):
        st.run_hook("claude", "start", "sid-main", source="compact", cwd=str(self.cwd),
                    env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                    cwd=str(self.cwd), env=self.env, now=1001)
        row = st.session_summary(self.seat)[("claude", "sid-main")]
        card = st.read_latest_card(self.session_seat("sid-main"))
        self.assertEqual(row["epoch"], 1)
        self.assertEqual(card["author"]["epoch"], 1)
        self.assertEqual(card["origin"], "runtime-compact")
        self.assertEqual(st.run_hook("claude", "start", "sid-main", cwd=str(self.cwd),
                                     env=self.env, now=1002), "")
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_nearby_real_compactions_each_save_a_card_and_queue(self):
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="first",
                    cwd=str(self.cwd), env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="second",
                    cwd=str(self.cwd), env=self.env, now=1010)
        card = st.read_latest_card(self.session_seat("sid-main"))
        self.assertEqual(card["generation"], 2)
        self.assertEqual(card["compact"]["summary"], "second")
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)
        self.assertEqual(self.enqueue_mock.call_count, 2)

    def test_after_prefers_payload_transcript_then_falls_back_to_ledger(self):
        st.run_hook("claude", "start", "sid-ledger", transcript="ledger.jsonl", cwd=str(self.cwd),
                    env=self.env, now=1000)
        st.run_hook("claude", "compact", "sid-ledger", phase="after", cwd=str(self.cwd),
                    env=self.env, now=1001)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "ledger.jsonl")
        st.run_hook("claude", "compact", "sid-payload", phase="after", transcript="payload.jsonl",
                    cwd=str(self.cwd), env=self.env, now=1010)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "payload.jsonl")

    def test_busy_seat_lock_skips_the_card_but_still_queues(self):
        args = SimpleNamespace(harness="claude", session_id="sid-main", event="compact",
                               source=None, transcript="payload.jsonl", cwd=str(self.cwd), reread=False,
                               phase="after")
        with st.seat_lock(self.seat.key), mock.patch.object(st, "COMPACT_LOCK_WAIT_SEC", 0.1):
            with mock.patch.object(st, "resolve_seat", return_value=self.seat), \
                 mock.patch("sys.stdin", io.StringIO('{"summary":"summary"}')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(st.cmd_hook(args), 0)
                self.assertEqual(output.getvalue(), "booking=pending reason=compact-state-BlockingIOError\n")
        self.assertEqual(self.enqueue_mock.call_count, 1)
        self.assertEqual(self.enqueue_mock.call_args.args[-1], "payload.jsonl")
        self.assertIsNone(st.read_latest_card(self.session_seat("sid-main")))

    def test_bounded_seat_lock_waits_for_a_short_holder(self):
        import threading
        held = threading.Event()

        def holder():
            with st.seat_lock(self.seat.key):
                held.set()
                import time
                time.sleep(0.2)

        thread = threading.Thread(target=holder)
        thread.start()
        held.wait(2)
        st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                    cwd=str(self.cwd), env=self.env, now=1000)
        thread.join()
        self.assertEqual(st.read_latest_card(self.session_seat("sid-main"))["compact"]["summary"], "summary")
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_card_write_failure_does_not_suppress_queue(self):
        with mock.patch.object(st, "write_card", side_effect=OSError("fixture")):
            st.run_hook("claude", "compact", "sid-main", phase="after", compact_summary="summary",
                        cwd=str(self.cwd), env=self.env, now=1000)
        self.assertEqual(self.enqueue_mock.call_count, 1)

    def test_legacy_compact_remains_ledger_only(self):
        st.run_hook("claude", "compact", "sid-main", cwd=str(self.cwd), env=self.env, now=1000)
        self.assertEqual(self.enqueue_mock.call_count, 0)
        self.assertIsNone(st.read_latest_card(self.session_seat("sid-main")))
        self.assertEqual(st.session_summary(self.seat)[("claude", "sid-main")]["epoch"], 1)

    def test_worker_hooks_emit_nothing_and_never_queue(self):
        worker = {**self.env, "AGENT_SESSION_ROLE": "worker"}
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="before", env=worker), "")
        self.assertEqual(st.run_hook("claude", "compact", "sid-worker", phase="after",
                                     compact_summary="summary", env=worker), "")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.enqueue_mock.call_count, 0)

    def test_runtime_bridge_before_contract_and_native_worker_exclusion(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        bridge = ROOT / "utilities/worker-state-hook.py"

        def invoke(harness, payload, **extra):
            call_env = {**env, **extra}
            return subprocess.run([sys.executable, str(bridge), "compact-before", "--harness", harness],
                                  input=json.dumps(payload), cwd=self.cwd, env=call_env,
                                  text=True, capture_output=True, timeout=8)

        for harness in ("claude", "codex", "opencode"):
            result = invoke(harness, {"session_id": "sid-bridge", "cwd": str(self.cwd)})
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = st.COMPACT_CARD_INSTRUCTIONS if harness in ("claude", "opencode") else ""
            self.assertEqual(result.stdout.strip(), expected)
            worker = invoke(harness, {"session_id": "sid-worker", "cwd": str(self.cwd)},
                            AGENT_SESSION_ROLE="worker")
            self.assertEqual((worker.returncode, worker.stdout), (0, ""))
        for harness, payload in (("claude", {"session_id": "claude-child", "agent_id": "agent-1"}),
                                 ("codex", {"session_id": "codex-child", "agent_type": "subagent"})):
            result = invoke(harness, payload)
            self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_runtime_bridge_after_forwards_payload_and_keeps_a_bounded_booking_reason(self):
        fake_home = self.root / "fake-tidy-home"
        (fake_home / "utilities").mkdir(parents=True)
        capture = self.root / "tidy-calls.jsonl"
        (fake_home / "utilities/session_tidy.py").write_text(
            "import json,sys\n"
            "argv=sys.argv[1:]; body=sys.stdin.read()\n"
            f"open({str(capture)!r}, 'a').write(json.dumps({{'argv': argv, 'stdin': body}}) + '\\n')\n"
            "if '--phase' in argv and argv[argv.index('--phase')+1] == 'after' and json.loads(body or '{}').get('trigger') == 'manual':\n"
            " print('booking=skipped reason=trigger-manual')\n"
            "else: print('ignored after output')\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "XDG_STATE_HOME": str(self.state),
                    "PYTHONDONTWRITEBYTECODE": "1", "CODEX_THREAD_ID": "inherited-other-session"})
        bridge = ROOT / "utilities/worker-state-hook.py"
        payloads = {
            "claude": {"session_id": "sid-c", "cwd": str(self.cwd), "transcript_path": "c.jsonl",
                       "compact_summary": "claude summary"},
            "codex": {"session_id": "sid-x", "cwd": str(self.cwd), "transcript_path": "x.jsonl",
                      "trigger": "manual"},
            "opencode": {"sessionID": "sid-o", "cwd": str(self.cwd), "summary": "opencode summary"},
        }
        for harness, payload in payloads.items():
            result = subprocess.run([sys.executable, str(bridge), "compact-after", "--harness", harness],
                                    input=json.dumps(payload), env=env, text=True,
                                    capture_output=True, timeout=8)
            expected_status = "booking=skipped reason=trigger-manual\n" if harness == "codex" else ""
            self.assertEqual((result.returncode, result.stdout), (0, expected_status), result.stderr)
        calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(calls), 3)
        expected = {
            "claude": ("sid-c", "c.jsonl", "claude summary"),
            "codex": ("sid-x", "x.jsonl", ""),
            "opencode": ("sid-o", None, "opencode summary"),
        }
        for call in calls:
            argv = call["argv"]
            harness = argv[argv.index("--harness") + 1]
            sid, transcript, summary = expected[harness]
            self.assertEqual(argv[:6], ["hook", "--harness", harness, "--event", "compact", "--phase"])
            self.assertEqual(argv[6], "after")
            self.assertEqual(argv[argv.index("--session-id") + 1], sid)
            self.assertEqual(argv[argv.index("--cwd") + 1], str(self.cwd))
            if transcript:
                self.assertEqual(argv[argv.index("--transcript") + 1], transcript)
            else:
                self.assertNotIn("--transcript", argv)
            expected_input = {"summary": summary}
            if transcript:
                expected_input["transcript"] = transcript
            if harness == "codex":
                expected_input["trigger"] = "manual"
            self.assertEqual(json.loads(call["stdin"]), expected_input)

    def test_main_wrapper_discards_partial_output_and_fails_open_on_timeout(self):
        fake_home = self.root / "fake-home"
        utility_dir = fake_home / "utilities"
        utility_dir.mkdir(parents=True)
        helper = utility_dir / "worker-state-hook.py"
        helper.write_text(
            "import os,sys,time\n"
            "print('partial ledger output', flush=True)\n"
            "if sys.argv[1:2] == ['compact-after'] and os.environ.get('FAKE_COMPACT_MODE') == 'reason':\n"
            " print('booking=skipped reason=trigger-manual', flush=True); raise SystemExit(0)\n"
            "if os.environ.get('FAKE_COMPACT_MODE') == 'timeout': time.sleep(8)\n"
            "raise SystemExit(2 if os.environ.get('AGENT_DISPATCH_STAGE_AUTHORITY') == '0' else 1)\n",
            encoding="utf-8")
        wrapper = ROOT / "hooks/worker-state-compact.py"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(fake_home), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "HERDR_PANE_ID": ""})
        partial = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                                 env={**env, "FAKE_COMPACT_MODE": "partial"}, text=True,
                                 input="{}", capture_output=True, timeout=8)
        self.assertEqual((partial.returncode, partial.stdout), (0, ""))
        timed = subprocess.run([sys.executable, str(wrapper), "compact-before", "--harness", "claude"],
                               env={**env, "FAKE_COMPACT_MODE": "timeout"}, text=True,
                               input="{}", capture_output=True, timeout=8)
        self.assertEqual((timed.returncode, timed.stdout), (0, ""))
        reason = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                                env={**env, "FAKE_COMPACT_MODE": "reason"}, text=True,
                                input="{}", capture_output=True, timeout=8)
        self.assertEqual((reason.returncode, reason.stdout), (0, ""))
        self.assertIn("booking=skipped reason=trigger-manual", reason.stderr)
        sub = subprocess.run([sys.executable, str(wrapper), "compact-after", "--harness", "claude"],
                             env={**env, "AGENT_DISPATCH_STAGE_AUTHORITY": "0"}, text=True,
                             input="{}", capture_output=True, timeout=8)
        self.assertEqual(sub.returncode, 2)
        self.assertEqual(sub.stdout, "partial ledger output\n")

    def _opencode_fixture(self, scenario, extra_env=None):
        """Run the real plugin against a fake v1 client; a python3 shim records every helper call."""
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        if not node_stdin_reaches_eof():
            self.skipTest("node spawnSync input never reaches EOF in this sandbox; run on a normal host")
        fake_bin = self.root / f"bin-{scenario}"
        fake_bin.mkdir()
        calls = self.root / f"calls-{scenario}.jsonl"
        instructions = self.root / "instructions.txt"
        instructions.write_text(st.COMPACT_CARD_INSTRUCTIONS + "\n", encoding="utf-8")
        shim = fake_bin / "python3"
        shim.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -c '\n"
            "import json, os, sys\n"
            "argv = sys.argv[1:]\n"
            "name = os.path.basename(argv[0]) if argv else \"\"\n"
            "body = sys.stdin.read() if name == \"worker-state-hook.py\" else \"\"\n"
            f"open({json.dumps(str(calls))}, \"a\").write(json.dumps({{\"argv\": argv, \"stdin\": body}}) + \"\\n\")\n"
            "if name == \"worker-state-hook.py\":\n"
            "    if os.environ.get(\"FAKE_LEDGER_FAIL\") == \"1\":\n"
            "        sys.stderr.write(\"ledger missing\\n\"); raise SystemExit(2)\n"
            "    if argv[1] == \"compact-before\":\n"
            f"        sys.stdout.write(open({json.dumps(str(instructions))}).read())\n"
            "    elif argv[1] == \"compact-after\" and json.loads(body or \"{}\").get(\"trigger\") == \"auto\":\n"
            "        print(\"booking=scheduled\")\n"
            "    elif argv[1] == \"runtime-status\":\n"
            "        print(\"booking=pending\")\n"
            "' \"$@\"\n", encoding="utf-8")
        shim.chmod(0o700)
        plugin = ROOT / "adapters/opencode/plugins/hearting-guards.js"
        script = f"""import {{ AgentHarnessGuards }} from {json.dumps(plugin.as_uri())}
const scenario = {json.dumps(scenario)}
const sid = "ses_fixture"
const old = {{ info: {{ id: "old-summary", sessionID: sid, role: "assistant", summary: true, finish: "stop", time: {{ created: Date.now() - 5000 }} }}, parts: [{{ type: "text", text: "old" }}] }}
const compactParent = {{ info: {{ id: "compact-parent", sessionID: sid, role: "user", time: {{ created: Date.now() - 1 }} }},
  parts: [{{ id: "compact-part", type: "compaction", auto: true, tail_start_id: "retained-start" }}] }}
const fresh = {{ info: {{ id: "new-summary", sessionID: sid, role: "assistant", parentID: "compact-parent", summary: true, finish: "stop", time: {{ created: 0 }} }}, parts: [{{ type: "text", text: "fresh summary" }}, {{ type: "image", text: "ignored" }}] }}
const calls = []
const ctx = {{ directory: {json.dumps(str(self.cwd))}, worktree: {json.dumps(str(self.cwd))}, client: {{ session: {{
  get: async (request) => {{
    calls.push(["get", request.path.id])
    if (scenario === "unknown") throw new Error("lookup failed")
    return {{ data: {{ id: request.path.id, parentID: request.path.id === "ses_child" ? sid : null }} }}
  }},
  messages: async (request) => {{
    calls.push(["messages", request.path.id, request.query, Boolean(request.signal)])
    fresh.info.time.created = Date.now()
    return {{ data: [old, compactParent, fresh] }}
  }},
  ...(["status-idle", "status-missing"].includes(scenario) ? {{ status: async (request) => {{
    calls.push(["status", sid, request.query])
    if (scenario === "status-missing") return {{ data: {{}} }}
    return {{ data: {{ [sid]: {{ type: "idle" }} }} }}
  }} }} : {{}}),
}} }} }}
const plugin = await AgentHarnessGuards(ctx)
const result = {{ calls }}
const output = {{ context: [], prompt: "keep this prompt" }}
if (scenario === "status-missing") {{
  await plugin.event({{ event: {{ type: "session.status", properties: {{ sessionID: sid, status: {{ type: "idle" }} }} }} }})
  result.preBookingIdleEmitted = true
}}
try {{
  await plugin["experimental.session.compacting"]({{ sessionID: sid }}, output)
  result.before = "ok"
}} catch (error) {{ result.before = String(error.message || error) }}
result.output = output
try {{
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: sid }} }} }})
  result.after = "ok"
}} catch (error) {{ result.after = String(error.message || error) }}
if (scenario === "main") {{
  result.childOutput = {{ context: [], prompt: "child prompt" }}
  await plugin["experimental.session.compacting"]({{ sessionID: "ses_child" }}, result.childOutput)
  await plugin.event({{ event: {{ type: "session.compacted", properties: {{ sessionID: "ses_child" }} }} }})
}}
console.log(JSON.stringify(result))
"""
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AGENT_", "MEM_", "CODEX_", "OPENCODE_", "FLEET_"))}
        env.update({"AGENT_HOME": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.state), "MEM_STORE": str(self.root / "mem"),
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")})
        env.update(extra_env or {})
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=self.cwd,
                                env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] \
            if calls.exists() else []
        hooks = [(Path(c["argv"][0]).name, c["argv"][1:], c["stdin"]) for c in recorded if c["argv"]]
        return json.loads(result.stdout.strip().splitlines()[-1]), hooks

    def test_opencode_main_gets_instructions_in_context_and_saves_only_the_new_summary(self):
        data, hooks = self._opencode_fixture("main")
        self.assertEqual(data["output"], {"context": [st.COMPACT_CARD_INSTRUCTIONS], "prompt": "keep this prompt"})
        # identity once per sid (cached), the summary read only after compaction, nothing before it
        self.assertEqual([c[:2] for c in data["calls"]],
                         [["get", "ses_fixture"], ["messages", "ses_fixture"], ["get", "ses_child"]])
        self.assertEqual(data["calls"][1][2]["limit"], 64)
        self.assertTrue(data["calls"][1][3])
        compact = [h for h in hooks if h[0] == "worker-state-hook.py"]
        self.assertEqual([h[1] for h in compact], [["compact-before", "--harness", "opencode"],
                                                   ["compact-after", "--harness", "opencode"]])
        after = json.loads(compact[1][2])
        self.assertEqual((after["sessionID"], after["summary"], after["worker"], after["trigger"],
                          after["summary_id"], after["parent_id"], after["tail_start_id"],
                          after["compaction_part_id"]),
                         ("ses_fixture", "fresh summary", False, "auto", "new-summary", "compact-parent",
                          "retained-start", "compact-part"))
        # a native child session gets no instructions and no tidy at all
        self.assertEqual(data["childOutput"], {"context": [], "prompt": "child prompt"})
        self.assertTrue(any(h[0] == "mem.py" and "_seen-reset" in h[1] for h in hooks))

    def test_opencode_native_idle_is_forwarded_only_after_an_auto_compact_booking(self):
        data, hooks = self._opencode_fixture("status-idle")
        runtime = [h for h in hooks if h[0] == "worker-state-hook.py" and h[1][0] == "runtime-status"]
        self.assertEqual(len(runtime), 1)
        payload = json.loads(runtime[0][2])
        self.assertEqual((payload["sessionID"], payload["status"]), ("ses_fixture", "idle"))
        self.assertGreater(payload["observed_at"], 0)

    def test_opencode_prebooking_cached_idle_is_not_relabelled_when_current_status_is_missing(self):
        data, hooks = self._opencode_fixture("status-missing")
        self.assertTrue(data["preBookingIdleEmitted"])
        self.assertIn(["status", "ses_fixture", None], data["calls"])
        runtime = [h for h in hooks if h[0] == "worker-state-hook.py" and h[1][0] == "runtime-status"]
        self.assertEqual(runtime, [])

    def test_opencode_unknown_identity_keeps_the_ledger_only_compact_record(self):
        data, hooks = self._opencode_fixture("unknown")
        self.assertEqual(data["output"], {"context": [], "prompt": "keep this prompt"})
        self.assertFalse(any(h[0] == "worker-state-hook.py" for h in hooks))
        legacy = [h[1] for h in hooks if h[0] == "session_tidy.py"]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0][:6], ["hook", "--harness", "opencode", "--event", "compact", "--session-id"])
        self.assertNotIn("--phase", legacy[0])
        self.assertTrue(any(h[0] == "mem.py" and "_seen-reset" in h[1] for h in hooks))

    def test_opencode_sub_session_ledger_failure_still_fails_closed(self):
        data, hooks = self._opencode_fixture(
            "subsession", {"AGENT_DISPATCH_STAGE_AUTHORITY": "0", "FAKE_LEDGER_FAIL": "1"})
        self.assertIn("ledger missing", data["before"])
        self.assertIn("ledger missing", data["after"])
        self.assertEqual(data["output"]["context"], [])
        self.assertEqual(data["calls"], [])
        self.assertEqual([h[1] for h in hooks if h[0] == "worker-state-hook.py"],
                         [["compact-before"], ["compact-after"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
