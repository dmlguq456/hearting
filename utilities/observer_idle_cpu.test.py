#!/usr/bin/env python3
"""Isolated 20-second idle-observer comparison using the original CPU definition."""

from __future__ import annotations
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])

import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SESSION = "fixture-parent-session"
ATTEMPT = "att-observer-idle-fixture"
DELIVERY = "delivery-observer-idle-fixture"
WINDOW_SECONDS = 20.0
REGISTRY_BYTES = 23_000_000
HISTORY_TURNS = 1601

sys.path.insert(0, str(ROOT / "utilities"))
SPEC = importlib.util.spec_from_file_location(
    "observer_idle_completion", ROOT / "utilities/codex-managed-completion.py")
completion = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = completion
SPEC.loader.exec_module(completion)


def _proc_ticks(pid: int):
    """Read the selected process's utime/stime and start tick, as a9-d2measure does."""
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 2:].split()
    return int(fields[11]) + int(fields[12]), int(fields[19])


def _measure_window(run, *, interval: float | None = None):
    pid = os.getpid()
    before_ticks, before_start = _proc_ticks(pid)
    started = time.monotonic()
    count = 0
    if interval is None:
        count = int(run())
    else:
        deadline = started + WINDOW_SECONDS
        while time.monotonic() < deadline:
            run()
            count += 1
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))
    elapsed = time.monotonic() - started
    after_ticks, after_start = _proc_ticks(pid)
    if before_start != after_start:
        raise AssertionError("measured PID was reused during the CPU window")
    hz = os.sysconf("SC_CLK_TCK")
    cpu_percent = 100.0 * (after_ticks - before_ticks) / hz / elapsed
    return cpu_percent, count, elapsed, hz


def _fixture_registry(path: Path):
    exact = (
        "t\tdone\t/repo\t/wt\tobserver-fixture\t"
        "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
        "execution_surface=registered-headless,registered_worker=1,launch_claimed=1,"
        "parent_completion_delivery=codex-native-queue,parent_sid=" + SESSION + ","
        "parent_harness=codex,attempt_id=" + ATTEMPT + ",delivery_id=" + DELIVERY + ","
        "delivery_recipient_kind=codex-native-queue,route_id=route-fixture,"
        "route_hash=sha256:fixture,route_node=execute\n"
    ).encode()
    filler_prefix = b"t\topen\t/repo\t/wt\tsynthetic\tparent_sid=other,"
    filler_prefix += b"parent_completion_delivery=other,pad="
    filler_budget = REGISTRY_BYTES - len(exact)
    count = 1600
    width, remainder = divmod(filler_budget, count)
    filler = (filler_prefix + b"x" * (width - len(filler_prefix) - 1) + b"\n") * count
    if remainder:
        filler += filler_prefix + b"x" * (remainder - len(filler_prefix) - 1) + b"\n"
    path.write_bytes(filler + exact)
    return path.stat().st_size


class ObserverIdleCpuTest(unittest.TestCase):
    def test_idle_wait_reduces_repeated_registry_and_history_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            jobs = Path(temporary) / "jobs.log"
            registry_size = _fixture_registry(jobs)
            input_digest = hashlib.sha256(jobs.read_bytes()).hexdigest()
            attempts = {ATTEMPT}
            history = [{"items": [{"clientId": f"unrelated-{index}"}]}
                       for index in range(HISTORY_TURNS)]
            counters = {"history_pages": 0, "after_observations": 0}

            def history_rpc(_path, _method, params, *, timeout, session=None):
                start = int(params.get("cursor") or 0)
                page = history[start:start + 100]
                next_cursor = str(start + 100) if start + 100 < len(history) else None
                counters["history_pages"] += 1
                return {"data": page, "nextCursor": next_cursor}

            def scan_full_history():
                return completion.codex_queue_delivery.find_turn_by_client_message_id(
                    Path("/fixture/queue.sock"), SESSION, "delivery-not-yet-consumed",
                    session=object())

            args = SimpleNamespace(
                queue_socket=Path("/fixture/queue.sock"), thread_id=SESSION,
                jobs=jobs, timeout=WINDOW_SECONDS, delivery_retry_interval=2.0)
            self.assertEqual(completion.parent_session_stop_reason(args, attempts), "")

            def old_fixed_tick():
                # Match the former observer pass: target selection happened in
                # both the outer loop and delivery call, then child rows were
                # parsed again for the stable ID and ledger validation.
                completion.queue_target(jobs, attempts, SESSION)
                completion.queue_target(jobs, attempts, SESSION)
                completion.current_session_children(
                    jobs, SESSION, attempts, completion.CODEX_QUEUE_PARENT_DELIVERY)
                completion.current_session_children(
                    jobs, SESSION, attempts, completion.CODEX_QUEUE_PARENT_DELIVERY)
                scan_full_history()

            class QueueSession:
                def __init__(self, _path, *, timeout):
                    self.timeout = timeout

                def close(self):
                    pass

            def current_observation(args, _receipt, current_attempts, *, session,
                                    delivery_target, initial_rows, allow_enqueue=True):
                fresh_rows = completion.current_session_children(
                    args.jobs, args.thread_id, current_attempts,
                    completion.CODEX_QUEUE_PARENT_DELIVERY)
                from peer_read_context import PeerReadContext
                fresh_target = completion._queue_target_from_rows(
                    args.jobs, fresh_rows, args.thread_id,
                    read_context=PeerReadContext())
                if (fresh_target != delivery_target
                        or {row.attempt_id for row in fresh_rows} != current_attempts):
                    raise completion.CompletionError("fixture-observation-changed")
                scan_full_history()
                counters["after_observations"] += 1
                return {"schema_version": 1, "status": "sent-ambiguous",
                        "reason": "native-queue-consumption-pending", "submitted": True,
                        "client_user_message_id": "delivery-stable-fixture"}

            import dispatch_registry_cache
            dispatch_registry_cache.clear_cache()
            with mock.patch("dispatch_seat_handover.effective_parent", return_value=SESSION), \
                 mock.patch("dispatch_seat_handover.effective_parent_harness", return_value="codex"), \
                 mock.patch.object(completion.codex_queue_delivery, "_rpc",
                                   side_effect=history_rpc):
                before_cpu, before_passes, before_seconds, hz = _measure_window(
                    old_fixed_tick, interval=2.0)
                counters["history_pages"] = 0
                counters["after_observations"] = 0
                args.timeout = WINDOW_SECONDS
                dispatch_registry_cache.clear_cache()
                with mock.patch.object(completion.codex_queue_delivery, "DeliverySession",
                                       side_effect=QueueSession), \
                     mock.patch.object(completion, "_native_queue_delivery_once",
                                       side_effect=current_observation):
                    def run_current():
                        completion.deliver_to_native_queue(
                            args, {"state": "ready", "children": []}, attempts)
                        return counters["after_observations"]
                    after_cpu, after_observations, after_seconds, _ = _measure_window(run_current)

            after_pages = counters["history_pages"]
            self.assertGreaterEqual(registry_size, REGISTRY_BYTES)
            self.assertEqual(len(history), HISTORY_TURNS)
            self.assertEqual(after_observations, counters["after_observations"])
            self.assertLess(after_observations, before_passes)
            self.assertLess(after_pages, before_passes * completion.codex_queue_delivery.MAX_QUEUE_PAGES)
            self.assertEqual(hashlib.sha256(jobs.read_bytes()).hexdigest(), input_digest)
            print(
                "observer-idle-cpu "
                f"window={WINDOW_SECONDS:.0f}s HZ={hz} 100%=one-core "
                "child_cpu=excluded "
                f"registry_bytes={registry_size} history_turns={len(history)} "
                f"before_cpu={before_cpu:.4f}% before_passes={before_passes} "
                f"after_cpu={after_cpu:.4f}% after_passes={after_observations} "
                f"history_pages_after={after_pages} elapsed={before_seconds:.2f}s/{after_seconds:.2f}s"
            )


if __name__ == "__main__":
    unittest.main()
