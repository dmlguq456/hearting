#!/usr/bin/env python3
"""AT16: v44 fields stay isomorphic across dispatch producers and consumers."""

from __future__ import annotations

import ast
from dataclasses import fields
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UTILITIES = ROOT / "utilities"
sys.path[:0] = [str(ROOT), str(UTILITIES)]

import artifact_receipt as ARTIFACT_RECEIPT  # noqa: E402
import dispatch_completion_join as JOIN  # noqa: E402
import dispatch_contract as CONTRACT  # noqa: E402


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CAPABILITY_ROUTE = load_module(
    "v44_capability_route", UTILITIES / "capability-route.py"
)
RECOVERY = load_module("v44_dispatch_recovery", UTILITIES / "dispatch-recovery.py")
CLAUDE_SUPERVISOR = load_module(
    "v44_claude_supervisor", UTILITIES / "claude-session-supervisor.py"
)
CODEX_SUPERVISOR = load_module(
    "v44_codex_supervisor", UTILITIES / "codex-app-server-supervisor.py"
)
MANAGED_COMPLETION = load_module(
    "v44_managed_completion", UTILITIES / "codex-managed-completion.py"
)
MANAGED_GATEWAY = load_module(
    "v44_managed_gateway", UTILITIES / "codex-managed-gateway.py"
)
FLEET_ROUTE = load_module("v44_fleet_route", ROOT / "tools/fleet/route.py")


WRAPPERS = {
    harness: ROOT / "adapters" / harness / "bin" / "dispatch-headless.py"
    for harness in ("claude", "codex", "opencode")
}


def _scope(path: Path, function: str | None = None, class_name: str | None = None):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes: list[ast.AST] = list(tree.body)
    if class_name is not None:
        owner = next(
            node
            for node in nodes
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        nodes = list(owner.body)
    if function is not None:
        return next(
            node
            for node in nodes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
        )
    return tree if class_name is None else owner


def _strings(path: Path, function: str | None = None, class_name: str | None = None):
    return {
        node.value
        for node in ast.walk(_scope(path, function, class_name))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _keyword_names(path: Path, function: str, class_name: str | None = None):
    return {
        keyword.arg
        for node in ast.walk(_scope(path, function, class_name))
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg is not None
    }


def _return_dict_keys(path: Path, function: str):
    rows = []
    for node in ast.walk(_scope(path, function)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            rows.append({
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            })
    if len(rows) != 1:
        raise AssertionError(f"expected one literal return dict in {function}: {rows}")
    return rows[0]


def _assigned_dict_keys(path: Path, function: str, variable: str):
    rows = []
    for node in ast.walk(_scope(path, function)):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if any(isinstance(target, ast.Name) and target.id == variable for target in node.targets):
            rows.append({
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            })
    if len(rows) != 1:
        raise AssertionError(
            f"expected one literal {variable} dict in {function}: {rows}"
        )
    return rows[0]


def _raises(path: Path, class_name: str, function: str, exception: str) -> bool:
    for node in ast.walk(_scope(path, function, class_name)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        called = node.exc.func
        if isinstance(called, ast.Name) and called.id == exception:
            return True
    return False


class DispatchV44ProjectionTest(unittest.TestCase):
    def test_launch_tuple_and_all_three_wrappers_project_the_same_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            launch = CAPABILITY_ROUTE.launch_compatibility_tuple(
                artifact_root=Path(tmp) / "artifacts",
                jobs=Path(tmp) / "state" / "jobs.log",
                cwd=ROOT,
            )
        self.assertEqual(set(launch), {
            "tuple_version", "registry_root", "launch_home", "runtime_root",
            "grounding_roots", "wrapper_root", "jobs_path",
        })
        identity_fields = {
            "kind", "path", "release_id", "content_digest", "binding_digest",
        }
        for name in (
            "registry_root", "launch_home", "runtime_root", "wrapper_root", "jobs_path"
        ):
            self.assertEqual(set(launch[name]), identity_fields, name)
        self.assertEqual(set(launch["grounding_roots"]), {"cwd", "artifact_root"})
        for row in launch["grounding_roots"].values():
            self.assertEqual(set(row), identity_fields)

        observed = {}
        for harness, path in WRAPPERS.items():
            validate_strings = _strings(path, "validate_route_record")
            main_strings = _strings(path, "main")
            zero_fields = _keyword_names(path, "validate_route_record") & {
                "registered", "started", "child_spawned"
            }
            observed[harness] = {
                "validation_flags": validate_strings & {"--launch-phase"},
                "fence_flags": main_strings & {"--route-file", "--launch-phase"},
                "zero_fields": zero_fields,
            }
        expected = {
            "validation_flags": {"--launch-phase"},
            "fence_flags": {"--route-file", "--launch-phase"},
            "zero_fields": {"registered", "started", "child_spawned"},
        }
        self.assertEqual(observed, {harness: expected for harness in WRAPPERS})

    def test_launch_owner_guard_and_fence_keep_the_complete_phase_tuple(self):
        surfaces = {
            "dispatch-node": (UTILITIES / "dispatch-node.py", {
                "--launch-phase", "registered=0", "started=0", "child_spawned=0",
            }),
            "dispatch-batch": (UTILITIES / "dispatch-batch.py", {"--launch-phase"}),
            "fallback": (UTILITIES / "stage-dispatch-fallback.py", {"--launch-phase"}),
            "session-chain": (UTILITIES / "stage-session-chain.py", {"--launch-phase"}),
            "worker-guard": (UTILITIES / "worker-route-guard.py", {"--launch-phase"}),
            "launch-fence": (
                UTILITIES / "launch-fence.py", {"--route-file", "--launch-phase"}
            ),
        }
        for name, (path, expected) in surfaces.items():
            observed = _strings(path)
            self.assertTrue(expected <= observed, (name, expected - observed))

    def test_continuation_partial_group_and_recovery_fields_are_one_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = {
                "route_id": "rt-source-v44",
                "route_hash": "sha256:" + "4" * 64,
                "cwd": str(ROOT),
                "artifact_root": str(Path(tmp) / "artifacts"),
                "nodes": [{
                    "id": "test", "depends_on": [], "completion_gate": "code-tests-pass",
                    "terminal": True, "write_scope": ["utilities/**"],
                }],
                "runtime_lineage": {
                    "runtime": "codex", "thread_id": "thread-v44", "node_turn_ids": {},
                },
            }
            continuation = CAPABILITY_ROUTE.build_continuation_route(
                source,
                resume_from_node="test",
                requested_boundary="test",
                reason="projection-fixture",
                artifact_root=source["artifact_root"],
            )
            request = RECOVERY.RecoveryRequest(
                jobs=Path(tmp) / "jobs.log",
                original_attempt_id="att-source",
                route_file=Path(tmp) / "source-route.json",
                resume_from_node="test",
                requested_boundary="test",
            )
            snapshot = RECOVERY.AttemptSnapshot(
                "done", "/repo", str(ROOT), "execute",
                {
                    "route_id": source["route_id"],
                    "route_hash": source["route_hash"],
                    "route_node": "execute",
                    "attempt_id": request.original_attempt_id,
                },
                "sha256:" + "5" * 64,
            )
            recovery_identity = CONTRACT.recovery_id(
                source_route_id=source["route_id"],
                source_route_hash=source["route_hash"],
                node_or_group_leg="execute",
                original_attempt_id=request.original_attempt_id,
                cancellation_receipt_digest="sha256:" + "6" * 64,
            )
            recovery_record = RECOVERY._new_record(
                request, snapshot, recovery_identity
            )

        continuation_fields = {
            "continuation_contract_version", "source_route_id", "source_route_hash",
            "resume_from_node", "requested_boundary", "reason",
            "source_evidence_digest", "continuation_id", "first_runnable_node",
            "requested_boundary_blocker", "first_runnable_blocker", "lineage_operation",
            "runtime_lineage", "source_route_supersession", "supersession_edges",
            "reused_nodes", "new_nodes", "partial_group_continuation",
            "launch_compatibility_tuple",
        }
        self.assertTrue(continuation_fields <= set(continuation))
        self.assertEqual(set(continuation["new_nodes"][0]), {
            "node_id", "source_contract_hash", "realized_contract_hash",
            "attempt_authority", "new_attempt_count",
        })
        self.assertEqual(set(continuation["source_route_supersession"]), {
            "edge_version", "edge_id", "operation", "from_route_id",
            "from_route_hash", "to_continuation_id", "reason",
            "source_verdict_preserved",
        })

        partial_fields = {
            "contract_version", "source_group_id", "source_batch_manifest_digest",
            "leg_manifest_digests", "original_group_cardinality", "join_policy",
            "failed_source_attempt_id", "gap_leg_id", "realized_peer_set",
            "reused_peer_set_proof_digest", "replacement_leg_identity",
            "replacement_attempt_id",
        }
        self.assertEqual(
            _return_dict_keys(
                UTILITIES / "capability-route.py", "partial_group_continuation"
            ),
            partial_fields,
        )
        peer_fields = (
            "node_id", "terminal_attempt_id", "marker_path", "marker_digest",
            "verdict", "quiescence_proof_digest", "output_evidence_digest",
            "contract_hash",
        )
        batch_tree = ast.parse(
            (UTILITIES / "dispatch-batch.py").read_text(encoding="utf-8")
        )
        peer_literal = next(
            ast.literal_eval(node.value)
            for node in batch_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "PARTIAL_PEER_KEYS"
                for target in node.targets
            )
        )
        self.assertEqual(peer_literal, peer_fields)
        self.assertEqual(
            _assigned_dict_keys(
                UTILITIES / "dispatch-batch.py", "prepare_partial_replacement", "seal"
            ),
            {
                "schema_version", "continuation_id", "source_route_id",
                "source_route_hash", "source_group_id", "source_batch_manifest_digest",
                "failed_source_attempt_id", "gap_leg_id",
                "reused_peer_set_proof_digest", "replacement_leg_identity",
                "replacement_attempt_id", "retry_claim_reused",
            },
        )

        self.assertEqual(set(recovery_record), {
            "schema_version", "recovery_id", "source", "current_phase", "phases",
        })
        self.assertEqual(set(recovery_record["source"]), {
            "jobs", "attempt_id", "route_id", "route_hash", "node_or_group_leg",
        })
        self.assertEqual(
            {field.name for field in fields(CONTRACT.RecoveryRetryClaim)},
            {
                "recovery_id", "original_attempt_id", "retry_ordinal",
                "retry_attempt_id", "state", "reason", "start_permitted",
            },
        )
        self.assertEqual(
            {field.name for field in fields(RECOVERY.RecoveryResult)},
            {
                "recovery_id", "phase", "state", "reason", "retry_attempt_id",
                "child_spawned", "record_path",
            },
        )
        self.assertTrue({
            "recovery_id", "retry_ordinal", "retry_attempt_id", "retry_claimed_at",
            "cancellation_quiescence_receipt", "cancellation_receipt_digest",
            "quiescence_pgid_proof", "quiescence_descendant_proof",
        } <= CONTRACT.ATTEMPT_MUTABLE_METADATA)

    def test_production_recovery_projects_official_continuation_and_batch_owner(self):
        recovery_path = UTILITIES / "dispatch-recovery.py"
        for method in ("publish_continuation", "start_gap"):
            self.assertFalse(
                _raises(
                    recovery_path, "ProductionRecoveryServices", method,
                    "RecoveryInterfaceUnavailable",
                ),
                f"ProductionRecoveryServices.{method} still refuses the Phase 4 interface",
            )
        production_fields = _strings(
            recovery_path, class_name="ProductionRecoveryServices"
        )
        self.assertTrue({
            "continuation", "--source-route", "--resume-from-node",
            "--requested-boundary", "--reason", "--artifact-root", "--output",
            "--continuation",
        } <= production_fields)

    def test_delivery_decoders_and_timing_projection_are_isomorphic(self):
        child = {
            "attempt_id": "att-v44", "status": "done", "readiness": "ready",
            "reason": "registry-closed", "required_action": "advance-completed",
        }
        receipt = {
            "schema_version": 2, "state": "ready",
            "parent_attempt_id": "att-parent", "children": [child],
        }
        claude = CLAUDE_SUPERVISOR.typed_receipt(
            receipt, "att-parent", {"att-v44"}
        )
        codex = CODEX_SUPERVISOR._typed_receipt(
            receipt, "att-parent", {"att-v44"}
        )
        self.assertEqual(claude, codex)
        self.assertEqual(set(claude), {
            "schema_version", "state", "parent_attempt_id", "children",
            "delivery_timing",
        })
        self.assertEqual(set(claude["children"][0]), {
            "attempt_id", "status", "readiness", "reason", "required_action",
        })
        self.assertEqual(
            MANAGED_COMPLETION.REQUIRED_ACTIONS,
            MANAGED_GATEWAY.REQUIRED_ACTIONS,
        )
        self.assertEqual(
            MANAGED_COMPLETION.ALLOWED_REASONS,
            MANAGED_GATEWAY.ALLOWED_REASONS,
        )
        self.assertEqual(MANAGED_GATEWAY.ALLOWED_RECEIPT_KEYS, {
            "schema_version", "state", "parent_attempt_id", "job_registry",
            "children", "delivery_timing", "delivery_classification",
        })
        self.assertEqual(MANAGED_GATEWAY.ALLOWED_CHILD_KEYS, {
            "attempt_id", "status", "readiness", "reason", "required_action",
            "harness", "delivery_classification",
        })
        timing = JOIN.delivery_timing_fields(join_completed_ns=17)
        self.assertEqual(set(timing), {
            "delivery_timing_schema_version", *JOIN.DELIVERY_TIMING_POINTS,
        })
        self.assertEqual(timing["delivery_timing_schema_version"], 1)
        self.assertEqual(MANAGED_GATEWAY.validate_delivery_timing(timing), timing)

    def test_receipt_v1_v2_decoder_bytes_and_decisions_are_unchanged(self):
        fixtures = (
            (
                UTILITIES / "fixtures/artifact-receipt/golden.v1.json",
                1,
                "sha256:ac6460846d010c48fc11eb11c26eca777439f1967c49c20c023e6d742647a206",
            ),
            (
                ROOT / "capabilities/report-bundle-receipt.v2.example.json",
                2,
                "sha256:e95ff73d64b6c92dc1661af12758987c1c5948802a1b1e160042c5bfddf6732c",
            ),
        )
        for path, version, digest in fixtures:
            payload = json.loads(path.read_text(encoding="utf-8"))
            verdict = ARTIFACT_RECEIPT.decode(payload)
            self.assertEqual(
                (verdict.state, verdict.schema_version, verdict.digest),
                ("accepted", version, digest),
            )
            self.assertEqual(verdict.receipt, payload)

    def test_innocent_hook_and_unrelated_route_hashes_regress_zero(self):
        env = os.environ.copy()
        env.pop("AGENT_DISPATCH_JOBS", None)
        hook = subprocess.run(
            [sys.executable, str(ROOT / "hooks/dispatch-owner-rewake.py")],
            input=json.dumps({
                "hook_event_name": "Stop",
                "session_id": "innocent-v44",
                "cwd": "/tmp",
            }),
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual((hook.returncode, hook.stdout, hook.stderr), (0, "", ""))

        route_fixtures = {
            "real_claude_staged.json": (
                "rt-9fa0fed86699b8f5",
                "sha256:9fa0fed86699b8f5f75e8e14327270986e7060cc3bce554458d8554f845d179f",
            ),
            "real_codex_staged.json": (
                "rt-f942824768304759",
                "sha256:f942824768304759f36f378c9ac8d4f063d34ae287b9c6d848b13392a69fe2ad",
            ),
        }
        fixture_root = ROOT / "tools/fleet/tests/fixtures/route"
        for name, expected in route_fixtures.items():
            record = json.loads((fixture_root / name).read_text(encoding="utf-8"))
            self.assertEqual((record["route_id"], record["route_hash"]), expected)
            self.assertEqual(FLEET_ROUTE.route_hash(record), expected[1])


if __name__ == "__main__":
    unittest.main()
