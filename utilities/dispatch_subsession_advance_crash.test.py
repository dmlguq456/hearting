#!/usr/bin/env python3
"""A-7: the five chain-advance crash windows replay to <= 1 successor start.

Mirrors SD-110's own stage-advance crash matrix -- inject a raise at each of
`coordinate_subsession_advance`'s five named checkpoints, verify the
already-committed record survives, then replay without injection and assert
the total successor-start count across BOTH runs never exceeds one, and that
replay returns the identical `subsession_advance_id`.
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
    "dispatch_subsession_advance_fixture_base",
    ROOT / "utilities" / "dispatch_subsession_advance.test.py",
)
BASE = importlib.util.module_from_spec(BASE_SPEC)
sys.modules[BASE_SPEC.name] = BASE
BASE_SPEC.loader.exec_module(BASE)

SA = BASE.SA

CRASH_MATRIX = SA.CHECKPOINTS


class InjectedSubsessionAdvanceCrash(RuntimeError):
    def __init__(self, checkpoint: str):
        super().__init__(f"injected-crash:{checkpoint}")
        self.checkpoint = checkpoint


def make_crash_checkpoint(target: str):
    fired = {"count": 0}

    def checkpoint(name: str) -> None:
        if name == target and fired["count"] == 0:
            fired["count"] += 1
            raise InjectedSubsessionAdvanceCrash(name)

    return checkpoint


class CrashMatrixTest(unittest.TestCase):
    def _run_one_checkpoint(self, target: str):
        sandbox = BASE.Sandbox()
        try:
            manifest = BASE.make_manifest()
            sandbox.mark_terminal(1, "att-stage-session-1")
            services = BASE.FakeServices(sandbox)
            request = BASE.make_request(sandbox, manifest, successor_index=2)

            with self.assertRaises(InjectedSubsessionAdvanceCrash):
                SA.coordinate_subsession_advance(
                    request, services, checkpoint=make_crash_checkpoint(target)
                )

            result = SA.coordinate_subsession_advance(request, services)
            self.assertEqual(result.outcome, "advanced")
            self.assertLessEqual(services.start_calls, 1)
            self.assertLessEqual(services.claim_calls, 1)

            first_id = result.subsession_advance_id
            result2 = SA.coordinate_subsession_advance(request, services)
            self.assertEqual(result2.subsession_advance_id, first_id)
            self.assertLessEqual(services.start_calls, 1)
        finally:
            sandbox.close()

    def test_before_gate_close(self):
        self._run_one_checkpoint("before-gate-close")

    def test_before_claim(self):
        self._run_one_checkpoint("before-claim")

    def test_before_register(self):
        self._run_one_checkpoint("before-register")

    def test_before_start(self):
        self._run_one_checkpoint("before-start")

    def test_after_start(self):
        self._run_one_checkpoint("after-start")

    def test_crash_matrix_covers_all_five_named_checkpoints(self):
        self.assertEqual(
            CRASH_MATRIX,
            ("before-gate-close", "before-claim", "before-register", "before-start", "after-start"),
        )


if __name__ == "__main__":
    unittest.main()
