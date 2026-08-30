#!/usr/bin/env python3
"""SD-119 R4 acceptance A-1/A-2: route-leg-independent sub-session batch admission."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).with_name("subdivision_batch_admission.py")
SPEC = importlib.util.spec_from_file_location("subdivision_batch_admission", PATH)
SUBDIV = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = SUBDIV
SPEC.loader.exec_module(SUBDIV)

BATCH_PATH = Path(__file__).with_name("dispatch-batch.py")
BATCH_SPEC = importlib.util.spec_from_file_location("dispatch_batch", BATCH_PATH)
BATCH = importlib.util.module_from_spec(BATCH_SPEC)
assert BATCH_SPEC.loader is not None
sys.modules[BATCH_SPEC.name] = BATCH
BATCH_SPEC.loader.exec_module(BATCH)


def _execute_node() -> dict:
    return {
        "id": "execute",
        "dispatch_depth": 2,
        "completion_gate": "code-execute",
        "write_scope": ["source/**"],
        "subdivision": {
            "min_intensity": "strong",
            "max_slices": 4,
            "disjointness": "exact-fixed-files",
        },
    }


class AdmissionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "source").mkdir(parents=True)
        self.route_path = self.base / "route.json"
        self.node = _execute_node()
        self.route = {
            "route_id": "rt-fixture",
            "route_hash": "sha256:fixture",
            "cwd": str(self.base),
            "effective_intensity": "strong",
            "nodes": [self.node],
        }
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")

    def _manifest(self, count: int = 2) -> Path:
        sessions = []
        for n in range(1, count + 1):
            brief = self.base / f"b{n}.md"
            brief.write_text("slice brief\n", encoding="utf-8")
            sessions.append({
                "subsession_id": f"ss-slice-{n}",
                "attempt_id": f"att-slice-{n}{'a' * 26}",
                "adapter": "codex",
                "slug": f"slice-{n}",
                "phase_brief": str(brief),
                "fixed_files": [str(self.base / "source" / f"{n}.py")],
                "narrow_verify": "true",
                "expected_round_trips": 2,
            })
        manifest_path = self.base / "chain.json"
        manifest_path.write_text(json.dumps({
            "schema_version": 1,
            "kind": "stage-session-chain",
            "chain_id": "ssc-fixture",
            "mode": "parallel",
            "worktree": str(self.base),
            "route_file": str(self.route_path),
            "route_id": self.route["route_id"],
            "route_hash": self.route["route_hash"],
            "route_node": "execute",
            "completion_gate": "code-execute",
            "sessions": sessions,
        }), encoding="utf-8")
        return manifest_path


class AdmissionGateTest(AdmissionFixture):
    """A-1: `execute` admits a manifest with zero `parallel_group` membership."""

    def test_execute_node_admits_two_slice_manifest_without_parallel_group(self):
        self.assertFalse(SUBDIV.has_route_leg_group(self.route, "execute"))
        recorded = []
        tokens = ["a" * 32, "b" * 32]

        def fake_reserve(governor, governor_root, pending, *, manifest, manifest_digest):
            self.assertEqual(len(pending), 2)
            self.assertEqual(manifest["batch_manifest_sha256"], manifest_digest)
            return tokens

        result = SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=self._manifest(2),
            governor=Path("governor"), governor_root=Path("governor-root"),
            reserve=fake_reserve,
            record_baseline=lambda route, node_id, manifest: recorded.append((node_id, manifest["_manifest_sha256"])),
        )
        self.assertEqual(result.tokens, tokens)
        self.assertEqual(len(result.sessions), 2)
        self.assertEqual(recorded, [("execute", result.manifest_digest)])

    def test_dispatch_batch_parallel_group_path_unused(self):
        # A-1: the whole point is that `parallel_nodes` (2..4-member cardinality)
        # is never reached for a node with zero route-leg membership.
        manifest_path = self._manifest(2)
        fake_admission = SUBDIV.AdmissionResult(
            tokens=["a" * 32, "b" * 32], manifest={"chain_id": "ssc-fixture", "sessions": []},
            manifest_digest="deadbeef", sessions=[], node_id="execute",
            reservation_identity="deadbeef",
        )
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "parallel_nodes",
                side_effect=AssertionError("parallel_nodes must not be called for subdivision admission"),
            ))
            stack.enter_context(mock.patch.object(
                BATCH.SUBDIVISION_ADMISSION, "admit_batch", return_value=fake_admission
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=type("R", (), {"path": self.base / "jobs.log"})()
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_model_governor_root", return_value=self.base / "gov"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
            }))
            argv = [
                "--route", str(self.route_path), "--parallel-group", "execute",
                "--action", "dry-run", "--slug-prefix", "fixture", "--parent", "owner",
                "--jobs", str(self.base / "jobs.log"),
                "--subdivision-manifest", str(manifest_path),
            ]
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        self.assertEqual(rc, 0, output.getvalue())
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "subdivision-batch-admitted")

    def test_legacy_group_call_still_raises_parallel_group_cardinality(self):
        # Non-subdivision calls against a group with the wrong width are
        # untouched: `parallel_nodes` is called and raises exactly as before.
        route = dict(self.route)
        route["nodes"] = [self.node]  # "execute" is not in any parallel_group
        output = io.StringIO()
        argv = [
            "--route", str(self.route_path), "--parallel-group", "execute",
            "--action", "dry-run", "--slug-prefix", "fixture", "--parent", "owner",
            "--jobs", str(self.base / "jobs.log"),
        ]
        with mock.patch.object(BATCH, "load_route", return_value=route):
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        self.assertNotEqual(rc, 0)
        self.assertIn("parallel-group-cardinality", output.getvalue())


class GovernorReservationTest(AdmissionFixture):
    """A-2: full-N atomic reservation -- all or nothing, one shared identity."""

    def test_insufficient_governor_slots_yields_zero_rows_zero_models(self):
        recorded = []

        def failing_reserve(*_args, **_kwargs):
            raise SUBDIV.SubdivisionAdmissionError("governor-capacity-insufficient", "cap reached")

        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=self.route, node=self.node, manifest_path=self._manifest(2),
                governor=Path("governor"), governor_root=Path("governor-root"),
                reserve=failing_reserve,
                record_baseline=lambda *a, **k: recorded.append((a, k)),
            )
        self.assertEqual(caught.exception.reason, "governor-capacity-insufficient")
        # baseline (checkpoint 4) is never reached -- no row, no model.
        self.assertEqual(recorded, [])

    def test_sufficient_slots_share_one_reservation_identity(self):
        tokens = ["c" * 32, "d" * 32, "e" * 32]

        def fake_reserve(governor, governor_root, pending, *, manifest, manifest_digest):
            self.assertEqual(len(pending), 3)
            return tokens

        result = SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=self._manifest(3),
            governor=Path("governor"), governor_root=Path("governor-root"),
            reserve=fake_reserve,
            record_baseline=lambda *a, **k: None,
        )
        self.assertEqual(result.tokens, tokens)
        self.assertEqual(len(set(result.tokens)), 3)
        # every admitted slice is keyed to the same manifest identity.
        self.assertEqual(result.reservation_identity, result.manifest_digest)


class PermissionAndFenceTest(AdmissionFixture):
    def test_no_subdivision_permission_is_not_eligible(self):
        node = dict(self.node)
        del node["subdivision"]
        route = dict(self.route)
        route["nodes"] = [node]
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=route, node=node, manifest_path=self._manifest(2),
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "subdivision-not-permitted")

    def test_intensity_below_min_is_not_eligible(self):
        route = dict(self.route)
        route["effective_intensity"] = "standard"
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=route, node=self.node, manifest_path=self._manifest(2),
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "intensity-below-min")

    def test_overlapping_fixed_files_refused_before_reservation(self):
        manifest_path = self._manifest(2)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["sessions"][1]["fixed_files"] = raw["sessions"][0]["fixed_files"]
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.admit_batch(
                route=self.route, node=self.node, manifest_path=manifest_path,
                governor=Path("g"), governor_root=Path("gr"),
                reserve=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reserve")),
            )
        self.assertEqual(caught.exception.reason, "disjointness-unproven")


if __name__ == "__main__":
    unittest.main()
