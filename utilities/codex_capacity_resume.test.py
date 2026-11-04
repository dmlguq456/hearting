#!/usr/bin/env python3
"""A Codex main turn that failed on transient model capacity continues once, on its own thread."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import codex_capacity_resume as resume  # noqa: E402
import codex_queue_delivery  # noqa: E402

THREAD = "01a17700-0000-7000-8000-000000000001"
TURN = "01a17700-0000-7000-8000-0000000000aa"
NEXT = "01a17700-0000-7000-8000-0000000000bb"


def event(kind, turn, **extra):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **extra}}


def failed(turn, info="server_overloaded"):
    return event("task_complete", turn, error={"message": "Selected model is at capacity.",
                                               "codex_error_info": info})


def item(turn, kind, client_id=None):
    return event("item_completed", turn, thread_id=THREAD,
                 item={"type": kind, "id": "i", "client_id": client_id})


class Clock:
    """Fake time; `sleep` runs the scheduled rollout appends that are due."""

    def __init__(self, rollout, schedule):
        self.t = 1000.0
        self.rollout = rollout
        self.schedule = sorted(schedule, key=lambda entry: entry[0])
        self.slept = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        self.slept += seconds
        while self.schedule and self.schedule[0][0] <= self.slept:
            _, lines = self.schedule.pop(0)
            with self.rollout.open("a", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(json.dumps(line) + "\n")


class Carrier:
    def __init__(self, latest=None, queue=False, wait=False):
        self.latest = latest if latest is not None else {
            "id": TURN, "status": "failed", "items": [{"type": "userMessage", "clientId": None}],
            "error": {"codexErrorInfo": "serverOverloaded"}}
        self.queue = queue
        self.wait = wait
        self.sends = []

    def __call__(self, _environ):
        return self

    def latest_turn(self, thread_id):
        return self.latest

    def queue_pending(self, thread_id):
        return self.queue

    def human_wait(self, thread_id):
        return self.wait

    def send(self, thread_id, client_message_id, text):
        self.sends.append((thread_id, client_message_id, text))
        return {"status": "queued"}

    def close(self):
        pass


class Fixture:
    def __init__(self, tmp, source="vscode", thread_source="user"):
        self.tmp = Path(tmp)
        self.codex_home = self.tmp / "codex"
        day = self.codex_home / "sessions" / "2026" / "10" / "26"
        day.mkdir(parents=True)
        self.rollout = day / f"rollout-2026-10-26T00-00-00-{THREAD}.jsonl"
        meta = {"type": "session_meta", "payload": {"id": THREAD, "source": source,
                                                     "thread_source": thread_source}}
        self.rollout.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        self.offset = self.rollout.stat().st_size
        self.environ = {"HARNESS_STATE_ROOT": str(self.tmp / "state")}

    def record(self):
        return json.loads((self.tmp / "state" / "codex-capacity-resume" / f"{THREAD}.json").read_text())

    def seed_record(self, value):
        path = self.tmp / "state" / "codex-capacity-resume" / f"{THREAD}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def watch(self, schedule, carrier):
        clock = Clock(self.rollout, schedule)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)}):
            outcome = resume.watch(THREAD, TURN, transcript=str(self.rollout), offset=self.offset,
                                   environ=self.environ, carrier_factory=carrier,
                                   sleep=clock.sleep, now=clock.now)
        return outcome, clock


class TransientCapacityTest(unittest.TestCase):
    def test_only_structured_transient_capacity_codes_qualify(self):
        for info in ("server_overloaded", "serverOverloaded",
                     {"response_too_many_failed_attempts": {"http_status_code": 429}},
                     {"responseTooManyFailedAttempts": {"httpStatusCode": 503}}):
            self.assertTrue(resume.transient_capacity(info), info)
        for info in (None, "usage_limit_exceeded", "usageLimitExceeded", "unauthorized",
                     "context_window_exceeded", "rateLimitExceeded",
                     {"responseTooManyFailedAttempts": {"httpStatusCode": 500}},
                     {"httpConnectionFailed": {"httpStatusCode": 429}},
                     {"responseTooManyFailedAttempts": {"httpStatusCode": True}},
                     "Selected model is at capacity. Please try a different model."):
            self.assertFalse(resume.transient_capacity(info), info)


class WatchTest(unittest.TestCase):
    def test_overloaded_turn_continues_once_on_the_same_thread_after_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            carrier = Carrier()
            outcome, clock = fx.watch([(0, [event("task_started", TURN), item(TURN, "UserMessage"),
                                            failed(TURN)])], carrier)
            self.assertEqual(outcome, "queued")
            self.assertEqual(len(carrier.sends), 1)
            thread, cid, text = carrier.sends[0]
            self.assertEqual((thread, cid), (THREAD, resume.client_id(THREAD, TURN)))
            self.assertTrue(cid.startswith("delivery-"))
            self.assertIn("1/5", text)
            self.assertGreaterEqual(clock.slept, resume.DELAYS[0])
            self.assertEqual(fx.record()["chain"], 1)
            self.assertEqual(fx.record()["client_id"], cid)

    def test_oversized_and_foreign_rollout_lines_do_not_hide_the_turn_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            huge = event("item_completed", TURN, item={"type": "CommandExecution",
                                                       "output": "x" * (resume._MAX_LINE + 10)})
            outcome, _ = fx.watch([(0, [["task_complete"], huge, failed(TURN)])], Carrier())
            self.assertEqual(outcome, "queued")

    def test_retry_exhausted_429_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            code = {"response_too_many_failed_attempts": {"http_status_code": 429}}
            carrier = Carrier(latest={"id": TURN, "status": "failed", "items": [], "error": {
                "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}}})
            outcome, _ = fx.watch([(0, [failed(TURN, code)])], carrier)
            self.assertEqual(outcome, "queued")
            self.assertIn("response_too_many_failed_attempts", carrier.sends[0][2])

    def test_interrupt_success_usage_limit_and_auth_leave_the_thread_alone(self):
        cases = {
            "aborted": [event("turn_aborted", TURN, reason="interrupted")],
            "completed": [event("task_complete", TURN, last_agent_message="done")],
            "not-transient": [failed(TURN, "usage_limit_exceeded")],
        }
        for expected, lines in cases.items():
            with self.subTest(expected), tempfile.TemporaryDirectory() as tmp:
                carrier = Carrier()
                outcome, _ = Fixture(tmp).watch([(0, lines)], carrier)
                self.assertEqual(outcome, expected)
                self.assertEqual(carrier.sends, [])
        with tempfile.TemporaryDirectory() as tmp:
            carrier = Carrier()
            outcome, _ = Fixture(tmp).watch([(0, [failed(TURN, "unauthorized")])], carrier)
            self.assertEqual((outcome, carrier.sends), ("not-transient", []))

    def test_a_newer_turn_during_the_wait_cancels_the_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            carrier = Carrier()
            outcome, _ = Fixture(tmp).watch(
                [(0, [failed(TURN)]), (30, [event("task_started", NEXT)])], carrier)
            self.assertEqual((outcome, carrier.sends), ("superseded", []))

    def test_live_state_rechecked_before_sending(self):
        later = {"id": NEXT, "status": "inProgress", "items": []}
        done = {"id": TURN, "status": "completed", "items": []}
        cases = {"superseded": Carrier(latest=later), "not-transient": Carrier(latest=done),
                 "queue-pending": Carrier(queue=True), "human-wait": Carrier(wait=True)}
        for expected, carrier in cases.items():
            with self.subTest(expected), tempfile.TemporaryDirectory() as tmp:
                outcome, _ = Fixture(tmp).watch([(0, [failed(TURN)])], carrier)
                self.assertEqual((outcome, carrier.sends), (expected, []))

    def test_only_no_progress_continuations_extend_the_chain(self):
        ours = "delivery-capacity-resume-previous"
        latest = {"id": TURN, "status": "failed", "error": {"codexErrorInfo": "serverOverloaded"},
                  "items": [{"type": "userMessage", "clientId": ours}]}
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.seed_record({"client_id": ours, "chain": 2})
            carrier = Carrier(latest=latest)
            outcome, clock = fx.watch([(0, [failed(TURN)])], carrier)
            self.assertEqual(outcome, "queued")
            self.assertIn("3/5", carrier.sends[0][2])
            self.assertGreaterEqual(clock.slept, resume.DELAYS[2])
            self.assertEqual(fx.record()["chain"], 3)
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.seed_record({"client_id": ours, "chain": 4})
            carrier = Carrier(latest=latest)
            outcome, _ = fx.watch([(0, [item(TURN, "CommandExecution"), failed(TURN)])], carrier)
            self.assertEqual(outcome, "queued")
            self.assertIn("1/5", carrier.sends[0][2])
        with tempfile.TemporaryDirectory() as tmp:
            fx = Fixture(tmp)
            fx.seed_record({"client_id": ours, "chain": len(resume.DELAYS)})
            carrier = Carrier(latest=latest)
            outcome, _ = fx.watch([(0, [item(TURN, "Reasoning"), failed(TURN)])], carrier)
            self.assertEqual((outcome, carrier.sends), ("chain-exhausted", []))

    def test_native_subagent_threads_are_not_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            carrier = Carrier()
            subagent = {"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}}
            fx = Fixture(tmp, source=subagent, thread_source="subagent")
            outcome, _ = fx.watch([(0, [failed(TURN)])], carrier)
            self.assertEqual((outcome, carrier.sends), ("not-main-thread", []))


class CarrierTest(unittest.TestCase):
    def test_continuation_is_one_native_queue_add_without_settings(self):
        calls = []

        def request(_self, _path, method, params, timeout=None):
            calls.append((method, params))
            if method == "thread/queue/add":
                return {"queuedSubmission": {"id": "q1"}}
            return {"data": []}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(codex_queue_delivery.DeliverySession, "request", request):
            carrier = resume.NativeCarrier({"CODEX_HOME": tmp})
            result = carrier.send(THREAD, resume.client_id(THREAD, TURN), resume.message(1, "x"))
        self.assertEqual(result["status"], "queued")
        adds = [params for method, params in calls if method == "thread/queue/add"]
        self.assertEqual(adds, [{"threadId": THREAD,
                                 "clientUserMessageId": resume.client_id(THREAD, TURN),
                                 "input": [{"type": "text", "text": resume.message(1, "x")}]}])
        self.assertNotIn("thread/queue/start", [method for method, _ in calls])
        self.assertNotIn("turn/start", [method for method, _ in calls])


class LaunchTest(unittest.TestCase):
    def test_launch_starts_one_detached_observer_for_the_exact_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "rollout.jsonl"
            transcript.write_text("x" * 7)
            popen = mock.Mock()
            payload = {"session_id": THREAD, "turn_id": TURN, "transcript_path": str(transcript)}
            self.assertTrue(resume.launch(payload, environ={}, popen=popen))
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:], ["watch", "--thread", THREAD, "--turn", TURN,
                                        "--transcript", str(transcript), "--offset", "7"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
        popen = mock.Mock()
        self.assertFalse(resume.launch({"session_id": THREAD}, environ={}, popen=popen))
        popen.assert_not_called()

    def test_prompt_hook_launches_for_main_sessions_only(self):
        spec = importlib.util.spec_from_file_location(
            "capacity_prompt_hook", ROOT / "adapters/codex/hooks/userprompt-lifecycle.py")
        hook = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = hook
        spec.loader.exec_module(hook)
        quiet = dict(run_preflight=mock.Mock(return_value=""), card_context=mock.Mock(return_value=("", None)),
                     candidate_context=mock.Mock(return_value=("", None)), launch_trigger=mock.Mock(),
                     token_budget_context=mock.Mock(return_value=""), emit_context=mock.Mock(),
                     sd111_first_prompt_sweep=mock.Mock(), retry_pending_peer=mock.Mock(),
                     native_queue_prompt_receipts=mock.Mock(return_value=([], [])))
        import peer_obligations
        for worker, turn_id, expected in ((False, TURN, 1), (True, TURN, 0), (False, None, 0)):
            payload = json.dumps({"session_id": THREAD, "turn_id": turn_id, "message_id": TURN,
                                  "prompt": "hi", "hook_event_name": "UserPromptSubmit"})
            with self.subTest(worker=worker, turn_id=turn_id), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"HOME": tmp, "XDG_STATE_HOME": tmp,
                                                 "CODEX_HOME": tmp, "PATH": os.environ["PATH"]},
                                    clear=True), \
                    mock.patch.multiple(hook, is_worker_session=mock.Mock(return_value=worker), **quiet), \
                    mock.patch.object(peer_obligations, "request_obligation_recovery"), \
                    mock.patch.object(resume, "launch") as launch, \
                    mock.patch("sys.stdin", io.StringIO(payload)):
                hook.main()
            self.assertEqual(launch.call_count, expected)


if __name__ == "__main__":
    unittest.main()
