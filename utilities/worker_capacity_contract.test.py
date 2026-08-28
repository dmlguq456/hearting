#!/usr/bin/env python3
"""A-D conformance tests for the worker-capacity contract."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
from dispatch_contract import (  # noqa: E402
    DispatchContractError,
    completion_marker_is_current,
    validate_attempt_metadata,
)
from stage_session_contract import StageSessionError, load_manifest  # noqa: E402


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


ROUTE = load_script("worker_capacity_route", "capability-route.py")
REGISTRY = load_script("worker_capacity_registry", "dispatch-registry.py")
WORKER_ROUTE = load_script("worker_capacity_worker_route", "worker-route-guard.py")
CHAIN = load_script("worker_capacity_chain", "stage-session-chain.py")
STAGE_RUNTIME = load_script("worker_capacity_stage_runtime", "stage_session_runtime.py")


class WorkerCapacityContractTest(unittest.TestCase):
    def ledger(self, *args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            [sys.executable, str(ROOT / "utilities" / "worker-state-ledger.py"), *args],
            text=True, capture_output=True, check=False, env=env,
        )

    def test_a_missing_ledger_and_compact_reanchor_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "_internal" / "state" / "att-12345678.md"
            missing = self.ledger("check", "--path", str(path), "--attempt-id", "att-12345678")
            self.assertEqual(missing.returncode, 65)
            self.assertIn("ledger-missing", missing.stderr)
            fixed = Path(td) / "fixed.py"
            other = Path(td) / "other.py"
            created = self.ledger(
                "init", "--path", str(path), "--attempt-id", "att-12345678",
                "--current-slice", "execute/1", "--next-action", "edit fixed.py",
                "--fixed-file", str(fixed),
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            denied = self.ledger(
                "guard-edit", "--path", str(path), "--attempt-id", "att-12345678",
                "--file", str(other),
            )
            self.assertEqual(denied.returncode, 65)
            before = self.ledger(
                "compact-before", "--path", str(path), "--attempt-id", "att-12345678"
            )
            self.assertEqual(before.returncode, 0, before.stderr)
            blocked = self.ledger(
                "check", "--path", str(path), "--attempt-id", "att-12345678"
            )
            self.assertIn("postcompact-reread-required", blocked.stderr)
            after = self.ledger(
                "compact-after", "--path", str(path), "--attempt-id", "att-12345678"
            )
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertIn("## Exact next command", after.stdout)

    def test_b_subsession_has_no_gate_authority(self):
        axes = {
            "attempt_schema_version": 2,
            "dispatch_depth": 2,
            "transport": "headless",
            "execution_surface": "registered-headless",
            "registered_worker": 1,
            "fallback_hop": "same-harness-headless",
            "route_id": "rt-test",
            "route_node": "execute",
            "subsession_id": "ss-exec1",
            "stage_authority": 0,
            "session_chain_id": "ssc-execute",
            "subsession_index": 1,
            "subsession_count": 2,
            "subsession_mode": "serial",
            "subsession_purpose": "planned",
            "phase_brief": "/tmp/brief.md",
            "phase_brief_sha256": "a" * 64,
            "state_ledger": "/tmp/state.md",
            "fixed_files_sha256": "b" * 64,
            "narrow_verify_sha256": "c" * 64,
            "expected_round_trips": 2,
        }
        validate_attempt_metadata(axes)
        with self.assertRaises(DispatchContractError) as caught:
            validate_attempt_metadata({**axes, "stage_authority": 1})
        self.assertEqual(caught.exception.reason, "subsession-stage-authority-forbidden")

    def test_b_runtime_binds_phase_brief_digest_into_validated_axes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = root / "worktree"
            worktree.mkdir()
            brief = root / "brief.md"
            brief.write_text("bounded phase contract\n", encoding="utf-8")
            fixed = worktree / "fixed.py"
            args = SimpleNamespace(
                subsession_id="ss-exec1",
                subsession_index=1,
                subsession_count=1,
                subsession_mode="serial",
                subsession_purpose="planned",
                session_chain_id="ssc-execute",
                phase_brief=str(brief),
                stage_authority=0,
                fixed_file=[str(fixed)],
                narrow_verify="python3 -m unittest focused",
                expected_round_trips=2,
                state_dir=None,
                dispatch_depth=2,
                route_id="rt-test",
                route_node="execute",
                attempt_id=None,
                worktree=str(worktree),
                execution_surface="registered-headless",
                registered_worker=1,
                fallback_hop="same-harness-headless",
                replica_batch_expectation=None,
                capability_owner="owner",
            )
            STAGE_RUNTIME.bind(args, artifact_root=root, action="dry-run")
            self.assertEqual(
                args.phase_brief_sha256,
                hashlib.sha256(brief.read_bytes()).hexdigest(),
            )

    def _fixture(
        self, td: str, *, mode: str = "serial", overlap: bool = False,
        session_count: int = 2,
    ):
        root = Path(td)
        worktree = root / "worktree"
        worktree.mkdir()
        agent_home = root / "agent-home"
        (agent_home / "core").mkdir(parents=True)
        (agent_home / "core" / "CORE.md").write_text("core\n")
        route_path = root / "route.json"
        route = {
            "route_id": "rt-stage-test",
            "route_hash": "sha256:" + "1" * 64,
            "registry_digest": "sha256:" + "2" * 64,
            "cwd": str(worktree),
        }
        node = {"id": "execute", "dispatch_depth": 2, "completion_gate": "tests-pass", "kind": "agent",
                "write_scope": ["./**"],
                "subdivision": {"min_intensity": "strong", "max_slices": 4, "disjointness": "exact-fixed-files"}}
        route["nodes"] = [node]
        route_path.write_text(json.dumps(route))
        sessions = []
        for index in range(1, session_count + 1):
            brief = root / f"brief-{index}.md"
            brief.write_text(f"slice {index}\n")
            fixed = worktree / ("same.py" if overlap else f"file-{index}.py")
            sessions.append({
                "subsession_id": f"ss-exec{index}",
                "attempt_id": f"att-stage-session-{index}",
                "adapter": "codex",
                "slug": f"exec-{index}",
                "phase_brief": str(brief),
                "fixed_files": [str(fixed)],
                "narrow_verify": f"python -m unittest test_{index}",
                "expected_round_trips": 2,
            })
        manifest = {
            "schema_version": 1,
            "kind": "stage-session-chain",
            "chain_id": "ssc-execute",
            "mode": mode,
            "worktree": str(worktree),
            "route_file": str(route_path),
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_node": node["id"],
            "completion_gate": node["completion_gate"],
            "sessions": sessions,
        }
        manifest_path = root / "chain.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return agent_home, route_path, route, node, manifest_path

    def test_b_one_stage_marker_aggregates_two_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            agent_home, route_path, route, node, manifest_path = self._fixture(td)
            manifest = load_manifest(manifest_path, node=node)
            jobs = Path(td) / "jobs.log"
            rows = []
            for session in manifest["sessions"]:
                metadata = {
                    "attempt_schema_version": "2",
                    "dispatch_depth": "2",
                    "transport": "headless",
                    "execution_surface": "registered-headless",
                    "registered_worker": "1",
                    "fallback_hop": "same-harness-headless",
                    "route_id": route["route_id"],
                    "route_hash": route["route_hash"],
                    "route_node": node["id"],
                    "attempt_id": session["attempt_id"],
                    "subsession_id": session["subsession_id"],
                    "stage_authority": "0",
                    "session_chain_id": manifest["chain_id"],
                    "subsession_index": str(session["index"]),
                    "subsession_count": str(session["count"]),
                    "subsession_mode": "serial",
                    "subsession_purpose": "planned",
                    "phase_brief": session["phase_brief"],
                    "phase_brief_sha256": "a" * 64,
                    "state_ledger": str(Path(td) / f"{session['attempt_id']}.md"),
                    "fixed_files_sha256": "b" * 64,
                    "narrow_verify_sha256": "c" * 64,
                    "expected_round_trips": "2",
                    "note": "completed-supervisor",
                    "failure_class": "pass",
                    "launch_outcome": "never-launched",
                }
                pipe = ",".join(f"{key}={value}" for key, value in metadata.items())
                rows.append(f"2026-08-06T00:00:00Z\tdone\trepo\t{route['cwd']}\t{session['subsession_id']}\t{pipe}")
            jobs.write_text("\n".join(rows) + "\n")
            evidence = Path(td) / "evidence.md"
            evidence.write_text("combined gate PASS\n")
            prior = os.environ.get("AGENT_HOME")
            os.environ["AGENT_HOME"] = str(agent_home)
            # SD-112 chain-3 supersession (§13.33.2-(8)): completion_dir()'s
            # env-less fallback no longer resolves under AGENT_HOME/.dispatch
            # at all -- it always lands under the stable per-user root now.
            # Pin an explicit AGENT_DISPATCH_JOBS under this fixture's
            # isolated `agent_home` instead, so this in-process test stays
            # isolated from the invoking shell's real registry exactly as
            # the comment always intended.
            prior_jobs = os.environ.get("AGENT_DISPATCH_JOBS")
            os.environ["AGENT_DISPATCH_JOBS"] = str(agent_home / ".dispatch" / "jobs.log")
            try:
                route["_route_file"] = str(route_path)
                marker, receipt = ROUTE.complete_subsession_stage(
                    route, node, node["id"], evidence, manifest_path, jobs
                )
                marker_path = agent_home / ".dispatch" / "completion" / route["route_id"] / "execute.json"
                self.assertEqual(receipt["sessions"], 2)
                self.assertEqual(marker["stage_authority"], "owner-chain")
                self.assertTrue(completion_marker_is_current(route, node, marker_path, marker))
                self.assertEqual(len(list(marker_path.parent.glob("execute.[0-9]*.json"))), 1)
            finally:
                if prior is None:
                    os.environ.pop("AGENT_HOME", None)
                else:
                    os.environ["AGENT_HOME"] = prior
                if prior_jobs is None:
                    os.environ.pop("AGENT_DISPATCH_JOBS", None)
                else:
                    os.environ["AGENT_DISPATCH_JOBS"] = prior_jobs

    def test_c_ripple_map_is_bounded_and_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("target()\ntarget_extra()\n")
            (root / "b.py").write_text("def target(): pass\n")
            command = [
                sys.executable, str(ROOT / "utilities" / "ripple-map.py"),
                "--root", td, "--symbol", "target", "--format", "json",
            ]
            first = subprocess.check_output(command, text=True)
            second = subprocess.check_output(command, text=True)
            self.assertEqual(first, second)
            receipt = json.loads(first)
            self.assertEqual(receipt["target_files"], ["a.py", "b.py"])
            self.assertEqual(len(receipt["matches"]), 2)

    def test_d_manifest_hints_are_advisory_but_parallel_overlap_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, node, manifest_path = self._fixture(td)
            manifest = load_manifest(manifest_path, node=node)
            self.assertEqual([row["index"] for row in manifest["sessions"]], [1, 2])
            self.assertTrue(all(row["narrow_verify"] for row in manifest["sessions"]))
        with tempfile.TemporaryDirectory() as td:
            _, _, _, node, manifest_path = self._fixture(td, mode="parallel", overlap=True)
            with self.assertRaisesRegex(StageSessionError, "parallel-fixed-file-overlap"):
                load_manifest(manifest_path, node=node)

    def test_d_three_slice_fixture_reduces_runtime_joins_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, node, manifest_path = self._fixture(td, session_count=3)
            manifest = load_manifest(manifest_path, node=node)
            receipt = CHAIN.continuation_metrics(len(manifest["sessions"]))
            self.assertEqual(receipt["baseline_runtime_joins"], 3)
            self.assertEqual(receipt["runtime_joins"], 1)
            self.assertEqual(receipt["continuation_reduction"], 2)

    def test_d_serial_chain_supervises_three_sessions_in_one_join(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, _, node, manifest_path = self._fixture(td, session_count=3)
            manifest = load_manifest(manifest_path, node=node)
            jobs = Path(td) / "jobs.log"
            started: list[str] = []

            def launch(command):
                started.append(command[command.index("--attempt-id") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(CHAIN, "run_checked", side_effect=launch), \
                    mock.patch.object(CHAIN, "readiness", return_value=(0, {"state": "ready"})):
                result = CHAIN.supervise(manifest, "fixture-owner", jobs, 30)
            self.assertEqual(result, 0)
            self.assertEqual(
                started,
                ["att-stage-session-1", "att-stage-session-2", "att-stage-session-3"],
            )
            receipt = json.loads(manifest_path.with_suffix(".receipt.json").read_text())
            self.assertTrue(receipt["complete"])
            self.assertEqual(len(receipt["sessions"]), 3)

    def test_registry_keeps_subsessions_first_class(self):
        rows = [
            {"order": 1, "meta": {"route_id": "rt", "route_node": "execute", "subsession_id": "ss-a", "attempt_id": "att-a"}},
            {"order": 2, "meta": {"route_id": "rt", "route_node": "execute", "subsession_id": "ss-b", "attempt_id": "att-b"}},
        ]
        self.assertEqual(len(REGISTRY.current(rows)), 2)

    def test_planned_serial_lineage_is_not_retry_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            base = "route_id=rt,route_node=execute,session_chain_id=ssc-chain,subsession_mode=serial,subsession_purpose=planned,subsession_count=2,stage_authority=0,failure_class=pass,note=completed-supervisor"
            jobs.write_text(
                "2026-08-06T00:00:00Z\tdone\trepo\tworktree\tone\t"
                + base + ",attempt_id=att-prior,subsession_id=ss-one,subsession_index=1\n"
                "2026-08-06T00:00:01Z\topen\trepo\tworktree\ttwo\t"
                + base + ",attempt_id=att-current,subsession_id=ss-two,subsession_index=2\n"
            )
            prior = os.environ.get("AGENT_DISPATCH_JOBS")
            os.environ["AGENT_DISPATCH_JOBS"] = str(jobs)
            try:
                self.assertTrue(WORKER_ROUTE._qualifying_subsession_lineage("rt", "execute", "att-current"))
                self.assertFalse(WORKER_ROUTE._qualifying_retry_evidence("rt", "execute", "att-current"))
            finally:
                if prior is None:
                    os.environ.pop("AGENT_DISPATCH_JOBS", None)
                else:
                    os.environ["AGENT_DISPATCH_JOBS"] = prior


if __name__ == "__main__":
    unittest.main()
