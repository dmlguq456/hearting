#!/usr/bin/env python3
"""SD-119 R4 acceptance A-1/A-2: route-leg-independent sub-session batch admission."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
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
        # is never reached for a node with zero route-leg membership. F-3
        # (impl-review round 1): the live entry point is fail-closed until R5
        # lands, so `admit_batch` itself is also never reached -- both are
        # asserted as `AssertionError`-raising side effects, not just unused
        # return values, and the receipt is the typed refusal.
        manifest_path = self._manifest(2)
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "parallel_nodes",
                side_effect=AssertionError("parallel_nodes must not be called for subdivision admission"),
            ))
            stack.enter_context(mock.patch.object(
                BATCH.SUBDIVISION_ADMISSION, "admit_batch",
                side_effect=AssertionError("admit_batch must not be reached while F-3 fail-closed gate holds"),
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
        self.assertEqual(receipt["state"], "subdivision-batch-refused")
        self.assertEqual(receipt["reason"], "scope-unproven")
        self.assertEqual(receipt["admitted_rows"], 0)
        self.assertEqual(receipt["admitted_models"], 0)

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


CHAIN_PATH = Path(__file__).with_name("stage-session-chain.py")
CHAIN_SPEC = importlib.util.spec_from_file_location("stage_session_chain_for_admission_test", CHAIN_PATH)
CHAIN = importlib.util.module_from_spec(CHAIN_SPEC)
assert CHAIN_SPEC.loader is not None
sys.modules[CHAIN_SPEC.name] = CHAIN
CHAIN_SPEC.loader.exec_module(CHAIN)


class ParallelEntryFailClosedTest(unittest.TestCase):
    """F-3 (impl-review round 1): both LIVE parallel entry points refuse
    `scope-unproven` before `admit_batch()` is ever reached, and neither
    writes a registry row or spawns a model process, until R5 lands."""

    def test_stage_session_chain_parallel_branch_refuses_before_admit_batch(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.registry"
            jobs.touch()
            args = type("Args", (), {"action": "register", "manifest": "unused", "parent": "owner"})()
            with mock.patch.object(
                CHAIN.SUBDIVISION_ADMISSION, "admit_batch",
                side_effect=AssertionError("admit_batch must not be reached while F-3 fail-closed gate holds"),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    rc = CHAIN._run_parallel_subdivision({}, {}, args, jobs=jobs)
            self.assertEqual(rc, 65)
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["state"], "subdivision-batch-refused")
            self.assertEqual(receipt["reason"], "scope-unproven")
            self.assertEqual(receipt["admitted_rows"], 0)
            self.assertEqual(receipt["admitted_models"], 0)
            # No child row: the fixture's jobs registry stays exactly empty.
            self.assertEqual(jobs.read_text(encoding="utf-8"), "")

    def test_raise_if_parallel_entry_fail_closed_reason_is_scope_unproven(self):
        with self.assertRaises(SUBDIV.SubdivisionAdmissionError) as caught:
            SUBDIV.raise_if_parallel_entry_fail_closed()
        self.assertEqual(caught.exception.reason, "scope-unproven")


class StartAdmittedBatchPartialFailureTest(AdmissionFixture):
    """F-4 (impl-review round 1): a mid-batch registration failure starts
    ZERO slices, including ones that themselves registered cleanly."""

    def _admission(self, count: int) -> "SUBDIV.AdmissionResult":
        manifest_path = self._manifest(count)
        recorded: list = []
        tokens = [chr(ord("a") + i) * 32 for i in range(count)]
        return SUBDIV.admit_batch(
            route=self.route, node=self.node, manifest_path=manifest_path,
            governor=Path("g"), governor_root=Path("gr"),
            reserve=lambda *a, **k: tokens,
            record_baseline=lambda *a, **k: recorded.append(a),
        )

    def test_third_slice_register_failure_starts_no_slice_at_all(self):
        admission = self._admission(3)
        calls: list[list[str]] = []
        cancelled: list[str] = []

        def fake_run(cmd, env):
            calls.append(cmd)
            action = cmd[cmd.index("--action") + 1]
            slug = cmd[cmd.index("--slug") + 1]
            if action == "register" and slug == "slice-3":
                return subprocess.CompletedProcess(cmd, 65, "", "register-failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        def fake_cancel(jobs, attempt_id):
            cancelled.append(attempt_id)
            return 1

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=fake_cancel,
        )
        self.assertEqual([row["started"] for row in results], [0, 0, 0])
        # F-4 (impl-review round 2): the contract is all-or-nothing on BOTH
        # counters. Slices 1/2 registered cleanly, so they are cancel-marked
        # and reported `registered: 0` -- the receipt and the registry agree
        # that this batch admitted nothing.
        self.assertEqual([row["registered"] for row in results], [0, 0, 0])
        self.assertEqual([row["cancelled"] for row in results], [1, 1, 0])
        self.assertEqual(
            [row["refusal_reason"] for row in results],
            [SUBDIV.BATCH_REGISTRATION_INCOMPLETE] * 3,
        )
        self.assertEqual(
            cancelled,
            [session["attempt_id"] for session in admission.sessions[:2]],
        )
        self.assertTrue(all(cmd[cmd.index("--action") + 1] == "register" for cmd in calls))
        self.assertEqual(len(calls), 3)

    def test_first_slice_register_failure_cancels_nothing(self):
        admission = self._admission(3)
        cancelled: list[str] = []

        def fake_run(cmd, env):
            return subprocess.CompletedProcess(cmd, 65, "", "register-failed")

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=lambda jobs, attempt: cancelled.append(attempt) or 1,
        )
        self.assertEqual([row["registered"] for row in results], [0, 0, 0])
        self.assertEqual([row["started"] for row in results], [0, 0, 0])
        self.assertEqual([row["cancelled"] for row in results], [0, 0, 0])
        self.assertEqual(cancelled, [])

    def test_cancel_failure_is_reported_not_raised(self):
        admission = self._admission(2)

        def fake_run(cmd, env):
            slug = cmd[cmd.index("--slug") + 1]
            if slug == "slice-2":
                return subprocess.CompletedProcess(cmd, 65, "", "register-failed")
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        def cancel_fails(jobs, attempt_id):
            return 0

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run, cancel_row=cancel_fails,
        )
        self.assertEqual([row["registered"] for row in results], [0, 0])
        self.assertEqual([row["cancelled"] for row in results], [0, 0])

    def test_all_registers_succeed_then_all_slices_start(self):
        admission = self._admission(2)

        def fake_run(cmd, env):
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        results = SUBDIV.start_admitted_batch(
            admission, parent="owner", jobs=self.base / "jobs.log",
            governor_reservation_env="AGENT_DISPATCH_GOVERNOR_RESERVATION",
            run=fake_run,
        )
        self.assertEqual([row["started"] for row in results], [1, 1])


if __name__ == "__main__":
    unittest.main()
