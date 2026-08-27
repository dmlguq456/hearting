#!/usr/bin/env python3
"""A-16: the five stage-advance crash windows replay to <= 1 successor attempt.

Mirrors `dispatch_recovery_crash.test.py:129
test_at9_all_crash_boundaries_replay_to_one_blocked_retry` -- inject a raise at
each of `coordinate_stage_advance`'s five named checkpoints, verify the
already-committed record survives, then replay without injection and assert
the total successor-start count across BOTH runs never exceeds one, and that
replay returns the identical `stage_advance_id`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))

BASE_SPEC = importlib.util.spec_from_file_location(
    "dispatch_stage_advance_fixture_base", ROOT / "utilities" / "dispatch_stage_advance.test.py"
)
BASE = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = BASE
BASE_SPEC.loader.exec_module(BASE)

SA = BASE.SA


class InjectedStageAdvanceCrash(RuntimeError):
    def __init__(self, checkpoint: str):
        super().__init__(f"injected-crash:{checkpoint}")
        self.checkpoint = checkpoint


CRASH_MATRIX = SA.CHECKPOINTS  # ("before-gate-close", "before-intent",
#                                 "before-register", "before-start", "after-start")


def make_crash_checkpoint(target: str):
    fired = {"count": 0}

    def checkpoint(name: str) -> None:
        if name == target and fired["count"] == 0:
            fired["count"] += 1
            raise InjectedStageAdvanceCrash(name)

    return checkpoint


class CrashMatrixTest(unittest.TestCase):
    def _run_one_checkpoint(self, target: str):
        route = BASE.make_route([BASE.node("a"), BASE.node("b", depends_on=("a",))])
        sandbox = BASE.Sandbox()
        try:
            route_file = sandbox.write_route(route)
            request = BASE.make_request(sandbox, route_file, predecessor_node="a")
            services = BASE.FakeServices()

            with mock.patch.object(SA, "gate_evidence", return_value=BASE.PASS_EVIDENCE):
                with self.assertRaises(InjectedStageAdvanceCrash):
                    SA.coordinate_stage_advance(
                        request, services, checkpoint=make_crash_checkpoint(target)
                    )

                # Replay without injection: must converge to exactly one
                # successful advance regardless of which window crashed.
                result = SA.coordinate_stage_advance(request, services)

            self.assertEqual(result.outcome, "advanced")
            self.assertEqual(result.successor_node, "b")
            self.assertLessEqual(services.start_calls, 1)
            self.assertLessEqual(services.claim_calls, 1)

            # A second replay (fully past the crash) must not start again and
            # must return the identical stage_advance_id.
            first_id = result.stage_advance_id
            with mock.patch.object(SA, "gate_evidence", return_value=BASE.PASS_EVIDENCE):
                result2 = SA.coordinate_stage_advance(request, services)
            self.assertEqual(result2.stage_advance_id, first_id)
            self.assertLessEqual(services.start_calls, 1)
            return services
        finally:
            sandbox.close()

    def test_before_gate_close(self):
        self._run_one_checkpoint("before-gate-close")

    def test_before_intent(self):
        self._run_one_checkpoint("before-intent")

    def test_before_register(self):
        self._run_one_checkpoint("before-register")

    def test_before_start(self):
        self._run_one_checkpoint("before-start")

    def test_after_start(self):
        services = self._run_one_checkpoint("after-start")
        # `after-start` crashes AFTER the durable record already marked the
        # advance complete -- the one call inside the crashing run is the
        # only start that should ever have happened.
        self.assertEqual(services.start_calls, 1)


if __name__ == "__main__":
    unittest.main()
