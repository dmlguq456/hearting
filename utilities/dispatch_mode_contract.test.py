#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dispatch_mode_contract import (
    DispatchModeContractError,
    normalize_dispatch_modes,
    validate_capability_mode,
    validate_manifest_mode_axes,
    validate_route_mode_axes,
)


ROOT = Path(__file__).resolve().parents[1]


def args(**overrides):
    values = dict(
        capability="autopilot-code",
        capability_mode="dev",
        worker_mode=None,
        mode=None,
        worker_type="owner",
        unit="_kernel/owner",
        assigned_contract="autopilot-code",
        dispatch_depth=1,
        route_node=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class DispatchModeContractTest(unittest.TestCase):
    def assertReason(self, reason, **values):
        with self.assertRaises(DispatchModeContractError) as caught:
            normalize_dispatch_modes(args(**values))
        self.assertEqual(reason, caught.exception.reason)

    def test_owner_has_capability_mode_and_no_worker_mode(self):
        row = args()
        normalize_dispatch_modes(row)
        self.assertEqual("dev", row.capability_mode)
        self.assertIsNone(row.worker_mode)
        self.assertEqual("_kernel/owner", row.unit)

    def test_legacy_scalar_is_capability_mode(self):
        row = args(capability_mode=None, mode="dev", unit="")
        normalize_dispatch_modes(row)
        self.assertEqual("dev", row.capability_mode)
        self.assertIsNone(row.worker_mode)
        self.assertEqual("_kernel/owner", row.unit)

    def test_owner_rejects_stage_mode(self):
        self.assertReason(
            "owner-worker-mode-forbidden",
            capability_mode=None,
            mode="plan/plan-author",
        )

    def test_owner_rejects_non_owner_unit(self):
        self.assertReason("invalid-owner-unit", unit="plan/plan-author")

    def test_non_owner_rejects_owner_unit(self):
        self.assertReason(
            "owner-unit-worker-type-mismatch",
            worker_type="stage",
            dispatch_depth=2,
            unit="_kernel/owner",
            assigned_contract="code-plan",
        )

    def test_canonical_and_legacy_are_ambiguous(self):
        self.assertReason("ambiguous-dispatch-mode-input", mode="dev")

    def test_stage_derives_worker_mode_from_unit(self):
        row = args(
            worker_type="stage",
            dispatch_depth=2,
            unit="plan/plan-author",
            assigned_contract="code-plan",
        )
        normalize_dispatch_modes(row)
        self.assertEqual("dev", row.capability_mode)
        self.assertEqual("plan/plan-author", row.worker_mode)

    def test_routed_legacy_worker_mode_uses_sealed_capability_default(self):
        row = args(
            capability_mode=None,
            mode="plan/plan-author",
            worker_type="stage",
            dispatch_depth=2,
            unit="plan/plan-author",
            assigned_contract="code-plan",
        )
        normalize_dispatch_modes(row, default_capability_mode="dev")
        self.assertEqual("dev", row.capability_mode)
        self.assertEqual("plan/plan-author", row.worker_mode)

    def test_stage_rejects_worker_mode_unit_mismatch(self):
        self.assertReason(
            "worker-mode-unit-mismatch",
            worker_type="stage",
            dispatch_depth=2,
            unit="plan/plan-author",
            worker_mode="dev/backend",
            assigned_contract="code-plan",
        )

    def test_capability_catalog_membership(self):
        validate_capability_mode(
            "autopilot-code", "dev", "capability_modes=audit,debug,dev\n"
        )
        with self.assertRaises(DispatchModeContractError) as caught:
            validate_capability_mode(
                "autopilot-code", "plan", "capability_modes=audit,debug,dev\n"
            )
        self.assertEqual("invalid-dispatch-capability-mode", caught.exception.reason)

    def test_codex_capability_info_exposes_composed_capability_modes(self):
        result = subprocess.run(
            [str(ROOT / "adapters/codex/bin/capability-map.sh"), "analyze-project"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("capability_modes=code,doc,paper\n", result.stdout)

    def test_manifest_validates_capability_and_worker_axes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "harness-manifest.json").write_text(
                json.dumps({
                    "capabilities": {"autopilot-code": {"modes": ["dev", "debug"]}},
                    "units": {"plan/plan-author": {}},
                }),
                encoding="utf-8",
            )
            validate_manifest_mode_axes(
                root, "autopilot-code", "dev", "plan/plan-author"
            )
            with self.assertRaises(DispatchModeContractError) as caught:
                validate_manifest_mode_axes(
                    root, "autopilot-code", "dev", "dev/not-real"
                )
            self.assertEqual("invalid-dispatch-worker-mode", caught.exception.reason)

    def test_route_axes_match(self):
        row = args(
            worker_type="stage",
            dispatch_depth=2,
            unit="plan/plan-author",
            assigned_contract="code-plan",
            route_node="plan",
        )
        normalize_dispatch_modes(row)
        route = {
            "capability_mode": "dev",
            "nodes": [{"id": "plan", "unit": "plan/plan-author"}],
        }
        validate_route_mode_axes(row, route)
        route["capability_mode"] = "debug"
        with self.assertRaises(DispatchModeContractError) as caught:
            validate_route_mode_axes(row, route)
        self.assertEqual("route-capability-mode-mismatch", caught.exception.reason)


if __name__ == "__main__":
    unittest.main()
