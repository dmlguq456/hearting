#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest import mock


PATH = Path(__file__).with_name("dispatch-batch.py")
ROOT = PATH.resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dispatch_batch", PATH)
BATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BATCH)
from dispatch_contract import (
    DispatchContractError,
    process_launch_identity,
    validate_dispatch_log_dir,
)


class BatchLogDirAdmissionTest(unittest.TestCase):
    def test_omitted_and_state_root_log_dirs_are_admitted(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "dispatch" / "jobs.log"
            self.assertEqual(
                validate_dispatch_log_dir(jobs, None), jobs.parent / "logs"
            )
            self.assertEqual(
                validate_dispatch_log_dir(jobs, jobs.parent / "nested" / "logs"),
                jobs.parent / "nested" / "logs",
            )

    def test_artifact_log_dir_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "dispatch" / "jobs.log"
            outside = Path(td) / "artifacts" / "dispatch-logs"
            with self.assertRaises(DispatchContractError) as caught:
                validate_dispatch_log_dir(jobs, outside)
            self.assertEqual(caught.exception.reason, "log-dir-outside-dispatch-state-root")
            self.assertFalse(outside.exists())


def candidate(adapter: str, hop: str, ordinal: int) -> dict[str, object]:
    return {
        "fallback_hop": hop,
        "ordinal": ordinal,
        "candidates": [
            {
                "child_harness": adapter,
                "status": "supported",
            }
        ],
    }


def replica_node(node_id: str, affinity: str = "unspecified") -> dict[str, object]:
    index = 0 if node_id == "plan" else 1
    return {
        "id": node_id,
        "dispatch_depth": 2,
        "depends_on": ["frame", "frame-replica"],
        "replica_group": "plan",
        "parallel_group": "plan",
        "parallel_leg_index": index,
        "parallel_leg_count": 2,
        "parallel_independence_axes": ["cross-harness", "model-profile", "perspective"],
        "model_profile": "balanced-deep" if index == 0 else "light",
        "perspective": "primary-plan" if index == 0 else "independent-plan",
        "harness_affinity": affinity,
        "fallback_hops": [
            candidate("codex", "same-harness-headless", 1),
            candidate("claude", "cross-harness-headless", 2),
        ],
    }


def resolve_side_effect(route, node, adapter, parent_identity=None):
    """Walk a fixture node's own fallback_hops, faithfully to resolve_checked_tuple.

    Keeps per-adapter hop/ordinal faithful to the fixture instead of collapsing
    every leg to one constant tuple, so receipt hop/ordinal assertions stay
    meaningful under the mock.
    """
    for entry in sorted(node.get("fallback_hops", []), key=lambda row: row.get("ordinal", 0)):
        if entry.get("fallback_hop") not in BATCH.DISPATCH_NODE.FALLBACK_HOPS:
            continue
        rows = [row for row in entry.get("candidates", []) if row.get("child_harness") == adapter]
        if not rows:
            continue
        if len(rows) > 1:
            raise BATCH.DISPATCH_NODE.DispatchNodeError(
                "dispatch-evidence-ambiguous-candidate", adapter=adapter
            )
        if rows[0].get("status") != "supported":
            raise BATCH.DISPATCH_NODE.DispatchNodeError(
                "dispatch-evidence-candidate-unsupported",
                adapter=adapter, status=str(rows[0].get("status")),
            )
        return BATCH.DISPATCH_NODE.CheckedSelection(
            {"status": "supported", "child_harness": adapter},
            rows[0],
            str(entry["fallback_hop"]),
            int(entry["ordinal"]),
        )
    raise BATCH.DISPATCH_NODE.DispatchNodeError(
        "dispatch-evidence-no-eligible-fallback", adapter=adapter
    )


def single_family_node(node_id: str) -> dict[str, object]:
    """One node whose only checked-supported family is claude."""
    return {
        "id": node_id,
        "dispatch_depth": 2,
        "fallback_hops": [
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                {"child_harness": "claude", "status": "supported"},
                {"child_harness": "codex", "status": "unsupported"},
            ]},
        ],
    }


def zero_family_node(node_id: str) -> dict[str, object]:
    """One node with no usable family at all."""
    return {
        "id": node_id,
        "dispatch_depth": 2,
        "fallback_hops": [
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                {"child_harness": "codex", "status": "unsupported"},
            ]},
        ],
    }


def ambiguous_codex_node(node_id: str) -> dict[str, object]:
    """Succeeds via claude but excludes codex for a *different* typed reason
    (ambiguous-candidate) than zero_family_node's own (candidate-unsupported)
    -- used to prove a later node's per-node exclusion detail is not polluted
    by an earlier, already-succeeded node's accumulated reasons.
    """
    return {
        "id": node_id,
        "dispatch_depth": 2,
        "fallback_hops": [
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                {"child_harness": "codex", "status": "supported"},
                {"child_harness": "codex", "status": "supported"},
                {"child_harness": "claude", "status": "supported"},
            ]},
        ],
    }


def three_family_excluded_node(node_id: str) -> dict[str, object]:
    """One node where all three harness families are checked-unsupported."""
    return {
        "id": node_id,
        "dispatch_depth": 2,
        "fallback_hops": [
            {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                {"child_harness": "codex", "status": "unsupported"},
                {"child_harness": "claude", "status": "unsupported"},
                {"child_harness": "opencode", "status": "unsupported"},
            ]},
        ],
    }


def success_receipt(
    command: list[str],
    *,
    registered: str | None = None,
    started: str = "1",
    child_spawned: str | None = None,
    duplicate: str = "0",
) -> str:
    adapter = command[command.index("--adapter") + 1]
    attempt_id = command[command.index("--attempt-id") + 1]
    if registered is None:
        registered = "1" if started == "1" else "0"
    if child_spawned is None:
        child_spawned = started
    return (
        "check=ok\n"
        f"adapter={adapter}\n"
        "status=start\n"
        f"attempt_id={attempt_id}\n"
        f"registered={registered}\n"
        f"started={started}\n"
        f"child_spawned={child_spawned}\n"
        f"duplicate_attempt={duplicate}\n"
    )


class DispatchBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        governor_env = mock.patch.dict(
            os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": ""}
        )
        governor_env.start()
        self.addCleanup(governor_env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.route_path = self.base / "route.json"
        self.route_path.write_text("{}", encoding="utf-8")
        self.jobs = self.base / "jobs.log"
        self.route = {
            "route_id": "rt-fixture",
            "route_hash": "sha256:fixture",
            "cwd": str(self.base),
            "nodes": [
                replica_node("plan", "codex"),
                replica_node("plan-replica", "claude"),
            ],
        }

    def argv(self, action: str = "start") -> list[str]:
        return [
            "--route",
            str(self.route_path),
            "--replica-group",
            "plan",
            "--action",
            action,
            "--slug-prefix",
            "fixture",
            "--parent",
            "owner",
            "--jobs",
            str(self.jobs),
        ]

    def common_patches(self):
        assignments = [
            (self.route["nodes"][0], "codex", "same-harness-headless", 1),
            (self.route["nodes"][1], "claude", "cross-harness-headless", 2),
        ]
        return contextlib.ExitStack(), assignments

    def legs(self, assignments=None):
        assignments = assignments or self.common_patches()[1]
        legs = []
        for node, adapter, hop, ordinal in assignments:
            slug = BATCH.replica_slug("fixture", node["id"])
            legs.append({
                "node": node["id"],
                "adapter": adapter,
                "hop": hop,
                "ordinal": ordinal,
                "slug": slug,
                "attempt_id": BATCH.stable_attempt_id(
                    self.route,
                    node,
                    slug,
                    "owner",
                    "att-parent-fixture",
                    adapter,
                    ordinal,
                ),
                "assignment_sha256": "sha256:" + __import__("hashlib").sha256(
                    BATCH.DEFAULT_PROMPT.encode("utf-8")
                ).hexdigest(),
                "independence": "cross-harness",
                "model_profile": str(node["model_profile"]),
                "perspective": str(node["perspective"]),
                "parallel_leg_index": int(node["parallel_leg_index"]),
            })
        return legs

    def write_existing(
        self, leg, *, status="open", note="", claimed="1", append=True,
        live_identity=True,
    ):
        all_legs = self.legs()
        _manifest, manifest_digest, leg_digests = BATCH.build_manifest(
            replica_group="plan",
            route_id=self.route["route_id"],
            parent_attempt_id="att-parent-fixture",
            independence="cross-harness",
            members=[{
                "assignment_sha256": str(item["assignment_sha256"]),
                "attempt_id": str(item["attempt_id"]),
                "route_node": str(item["node"]),
                "harness": str(item["adapter"]),
                "fallback_hop": str(item["hop"]),
                "fallback_ordinal": int(item["ordinal"]),
                "model_profile": str(item["model_profile"]),
                "perspective": str(item["perspective"]),
                "parallel_leg_index": int(item["parallel_leg_index"]),
                "leg_class": str(item.get("leg_class", "peer")),
            } for item in all_legs],
            required_independence_axes=["cross-harness", "model-profile", "perspective"],
            realized_independence_axes=["cross-harness", "model-profile", "perspective"],
        )
        metadata = (
            "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
            "execution_surface=registered-headless,registered_worker=1,"
            f"fallback_hop={leg['hop']},harness={leg['adapter']},"
            f"child_harness={leg['adapter']},route_id={self.route['route_id']},"
            f"route_node={leg['node']},parent=owner,"
            "parent_attempt_id=att-parent-fixture,launch_authority=conductor,"
            f"fallback_ordinal={leg['ordinal']},attempt_id={leg['attempt_id']},"
            f"launch_claimed={claimed},launch_fence=registry-v1,parallel_group=plan,replica_group=plan,"
            "reservation_kind=parallel-batch,batch_declared_size=2,"
            "batch_admission_count=2,"
            f"batch_group=plan,batch_route_id={self.route['route_id']},"
            "batch_parent_attempt_id=att-parent-fixture,"
            f"batch_attempt_id={leg['attempt_id']},batch_route_node={leg['node']},"
            f"batch_harness={leg['adapter']},batch_fallback_hop={leg['hop']},"
            f"batch_fallback_ordinal={leg['ordinal']},"
            "batch_independence=cross-harness,"
            f"batch_model_profile={leg['model_profile']},"
            f"batch_perspective={leg['perspective']},"
            f"batch_parallel_leg_index={leg['parallel_leg_index']},"
            f"batch_assignment_sha256={leg['assignment_sha256']},"
            f"batch_manifest_sha256={manifest_digest},"
            f"batch_leg_sha256={leg_digests[str(leg['attempt_id'])]}"
        )
        if status in {"open", "running"} and claimed == "1" and live_identity:
            raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            start = raw[raw.rfind(")") + 2 :].split()[19]
            metadata += (
                f",pid={os.getpid()},pid_start={start},"
                f"pid_observer_ns={os.readlink('/proc/self/ns/pid')},"
                "launch_started=1"
            )
        if note:
            metadata += f",note={note}"
        row = (
            f"2026-07-24T00:00:00Z\t{status}\t{self.base}\t{self.base}\t"
            f"{leg['slug']}\t{metadata}\n"
        )
        mode = "a" if append and self.jobs.exists() else "w"
        with self.jobs.open(mode, encoding="utf-8") as handle:
            handle.write(row)

    def test_load_route_rejects_non_object_json_before_verification(self):
        self.route_path.write_text("[]", encoding="utf-8")
        with mock.patch.object(BATCH.subprocess, "run") as verify, \
             self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.load_route(self.route_path)
        self.assertEqual(ctx.exception.reason, "route-record-invalid")
        verify.assert_not_called()

    def test_stable_attempt_identity_ignores_display_slug_prefix(self):
        node = self.route["nodes"][0]
        first = BATCH.stable_attempt_id(
            self.route, node, "first-display", "owner",
            "att-parent-fixture", "codex", 1,
        )
        second = BATCH.stable_attempt_id(
            self.route, node, "different-display", "renamed-owner",
            "att-parent-fixture", "codex", 1,
        )
        self.assertEqual(first, second)

    def test_group_requires_two_to_four_depth_two_nodes_with_same_dependencies(self):
        self.assertEqual(
            [node["id"] for node in BATCH.replica_nodes(self.route, "plan")],
            ["plan", "plan-replica"],
        )
        for mutation, reason in (
            (lambda route: route["nodes"].pop(), "parallel-group-cardinality"),
            (
                lambda route: route["nodes"][1].update(dispatch_depth=1),
                "parallel-group-depth-invalid",
            ),
            (
                lambda route: route["nodes"][1].update(depends_on=["other"]),
                "parallel-group-dependency-mismatch",
            ),
        ):
            altered = json.loads(json.dumps(self.route))
            mutation(altered)
            with self.subTest(reason=reason), self.assertRaises(BATCH.BatchError) as ctx:
                BATCH.replica_nodes(altered, "plan")
            self.assertEqual(ctx.exception.reason, reason)

    def test_assignment_prefers_distinct_harnesses_and_declared_affinity(self):
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                self.route, self.route["nodes"], allow_degraded=False
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual([row[1] for row in rows], ["codex", "claude"])
        self.assertEqual(diagnostics["degradation_cause"], "")

    def test_assignment_uses_recent_counts_to_share_parallel_legs_across_three(self):
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "least-recent-attempts",
            "window": 30,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        self.jobs.write_text(
            "".join(
                f"2026-08-09T00:00:0{index}Z\tdone\t/r\t/w\tn{index}\t"
                "attempt_schema_version=2,registered_worker=1,"
                f"attempt_id=att-batch-count-{index},harness=codex\n"
                for index in range(5)
            ),
            encoding="utf-8",
        )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual({row[1] for row in rows}, {"claude", "opencode"})

    def test_capacity_aware_batch_preserves_primary_quality_band(self):
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "capacity-aware",
            "window": 30,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["harness_policy"] = {
                "primary": ["claude", "codex"],
                "relief": ["opencode"],
                "last_resort": [],
                "promote_relief_below": 35,
            }
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 60, "codex": 80, "opencode": 100,
        }):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual({row[1] for row in rows}, {"claude", "codex"})

    def test_balanced_batch_avoids_a_usage_gated_harness(self):
        # 2026-08-20 artifact-knowledge-index gen-1/gen-2: under balanced
        # allocation the inverted usage-gate term PREFERRED codex at 0%
        # headroom, and both retrieval groups died on dead-session-limit.
        # A harness past the usage gate (headroom <= 100-usage_gate_used_percent)
        # must lose to any non-gated cross-harness combination; unknown
        # headroom (opencode None) is not gated.
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "balanced",
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 60.0, "codex": 0.0, "opencode": None,
        }):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual({row[1] for row in rows}, {"claude", "opencode"})

    def test_balanced_batch_gate_outranks_the_quality_band(self):
        # B-1 framing's parallel falsifier: the gate must outrank aggregate
        # band_rank, so the gated primary (claude) is never placed while an
        # ungated combination exists.
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "balanced",
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["harness_policy"] = {
                "primary": ["claude", "codex"],
                "relief": ["opencode"],
                "last_resort": [],
                "promote_relief_below": 0,
            }
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 5.0, "codex": 60.0, "opencode": 80.0,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual({row[1] for row in rows}, {"codex", "opencode"})

    def test_balanced_batch_gate_outranks_affinity(self):
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "balanced",
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_policy"] = {
                "primary": ["claude", "codex"],
                "relief": ["opencode"],
                "last_resort": [],
                "promote_relief_below": 0,
            }
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        # Pin the sealed affinity to the gated family on one leg.
        route["nodes"][0]["harness_affinity"] = "claude"
        route["nodes"][1]["harness_affinity"] = "diverse"
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 5.0, "codex": 60.0, "opencode": 80.0,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertNotIn("claude", {row[1] for row in rows})

    def test_balanced_batch_unknown_usage_is_not_gated_across_bands(self):
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "balanced",
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["harness_policy"] = {
                "primary": ["claude", "codex"],
                "relief": ["opencode"],
                "last_resort": [],
                "promote_relief_below": 0,
            }
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 5.0, "codex": 5.0, "opencode": None,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertIn("opencode", {row[1] for row in rows})

    def test_balanced_three_leg_gate_precedes_band_rank(self):
        # Policy prefers codex (primary) over claude (relief); scores gate
        # codex and leave claude ungated. Aggregate band_rank alone would
        # push toward more codex legs -- the gate must still win, so the
        # gated family (codex) gets at most the diversity-forced minimum.
        route = self._three_leg_route()
        for node in route["nodes"]:
            node["harness_policy"] = {
                "primary": ["codex"],
                "relief": ["claude"],
                "last_resort": [],
                "promote_relief_below": 0,
            }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 60.0, "codex": 5.0, "opencode": None,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        placed = [row[1] for row in rows]
        self.assertLessEqual(placed.count("codex"), 1)
        self.assertGreaterEqual(placed.count("claude"), 2)

    def test_balanced_batch_all_gated_still_uses_maximum_headroom(self):
        route = self._three_leg_route()
        for node in route["nodes"]:
            node["harness_policy"] = {
                "primary": ["claude"],
                "relief": ["codex"],
                "last_resort": [],
                "promote_relief_below": 0,
            }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 2.0, "codex": 8.0, "opencode": 1.0,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("codex"), 2)
        self.assertEqual(placed.count("claude"), 1)

    def _three_leg_route(self):
        # Three nodes, exactly two usable families (codex/claude): with the
        # diversity key already satisfied by any distinct combo (>= 2
        # families present), the balanced allocation_order term is the only
        # thing left to decide which family gets the majority (2 of 3) legs.
        route = json.loads(json.dumps(self.route))
        third = dict(replica_node("plan-third"))
        third["id"] = "plan-third"
        third["parallel_leg_index"] = 2
        third["parallel_leg_count"] = 3
        route["nodes"].append(third)
        route["dispatch_allocation"] = {
            "strategy": "balanced",
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        return route

    def test_balanced_group_placement_prefers_the_larger_headroom_family(self):
        route = self._three_leg_route()
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 58.0, "codex": 99.0, "opencode": None,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("codex"), 2)
        self.assertEqual(placed.count("claude"), 1)

    def test_balanced_group_placement_still_honours_recent_attempt_balance(self):
        # Same headroom gap as the test above (58/99), but codex already took
        # 20 of the last 30 attempts to claude's 10: the 2026-08-13
        # balanced-first policy must still pull the majority back to claude.
        route = self._three_leg_route()
        rows_log = []
        for index in range(10):
            rows_log.append(
                f"2026-08-09T00:00:{index:02d}Z\tdone\t/r\t/w\tn{index}\t"
                "attempt_schema_version=2,registered_worker=1,"
                f"attempt_id=att-batch-headroom-claude-{index},harness=claude\n"
            )
        for index in range(20):
            rows_log.append(
                f"2026-08-09T00:01:{index:02d}Z\tdone\t/r\t/w\tn{index}\t"
                "attempt_schema_version=2,registered_worker=1,"
                f"attempt_id=att-batch-headroom-codex-{index},harness=codex\n"
            )
        self.jobs.write_text("".join(rows_log), encoding="utf-8")
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 58.0, "codex": 99.0, "opencode": None,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("claude"), 2)
        self.assertEqual(placed.count("codex"), 1)

    def test_balanced_group_placement_keeps_gated_leg_count_as_the_primary_key(self):
        # 28fef331 invariant: an exhausted (fully gated) family must never
        # take the majority of legs, regardless of what the deficit term
        # alone would prefer.
        route = self._three_leg_route()
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 0.0, "codex": 99.0, "opencode": None,
        }):
            rows, independence, _diagnostics = BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("claude"), 1)
        self.assertEqual(placed.count("codex"), 2)

    def test_receipt_hop_and_ordinal_match_the_bound_tuple(self):
        # D7 live case reproduced end to end: a foreign claude->claude row
        # shadows nothing once assign_harnesses consumes the one shared
        # resolver, so the leg's recorded hop/ordinal is the bound tuple's own
        # rather than a second, independently walked value (the candidate()
        # deletion this guards).
        claude_parent_claude_child = {
            "child_harness": "claude", "status": "supported",
            "parent_harness": "claude", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "launch_authority": "conductor",
            "probe_source": "fixture-check", "failure_class": "",
        }
        codex_parent_claude_child = {
            **claude_parent_claude_child,
            "parent_harness": "codex", "probe_source": "second-check",
        }
        codex_parent_codex_child = {
            "child_harness": "codex", "status": "supported",
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write", "launch_authority": "conductor",
            "probe_source": "third-check", "failure_class": "",
        }
        node1 = {
            "id": "plan", "dispatch_depth": 2,
            "fallback_hops": [
                {"ordinal": 1, "fallback_hop": "same-harness-headless",
                 "candidates": [claude_parent_claude_child]},
                {"ordinal": 2, "fallback_hop": "cross-harness-headless",
                 "candidates": [codex_parent_claude_child]},
            ],
        }
        node2 = {
            "id": "plan-replica", "dispatch_depth": 2,
            "fallback_hops": [
                {"ordinal": 1, "fallback_hop": "same-harness-headless",
                 "candidates": [codex_parent_codex_child]},
            ],
        }
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "dispatch_evidence": {"tuples": [
                claude_parent_claude_child, codex_parent_claude_child, codex_parent_codex_child,
            ]},
        }
        actual_parent = {
            "parent_harness": "codex", "parent_transport": "headless",
            "parent_sandbox": "workspace-write",
        }
        rows, independence, diagnostics = BATCH.assign_harnesses(
            route, [node1, node2], allow_degraded=False, parent_identity=actual_parent,
        )
        self.assertEqual(independence, "cross-harness")
        by_adapter = {adapter: (hop, ordinal) for _node, adapter, hop, ordinal in rows}
        self.assertEqual(by_adapter["claude"], ("cross-harness-headless", 2))
        self.assertEqual(by_adapter["codex"], ("same-harness-headless", 1))

    def test_two_usable_families_stay_cross_harness_even_with_the_degrade_flag(self):
        # D3 equivalence (frame-synthesis.md D3): group width is in [2, 4] and
        # every leg holds >= 1 option, so `len(usable) >= 2` is *exactly*
        # `distinct != []`. A fixture where a same-family assignment would
        # otherwise win is therefore provably impossible to build here (see
        # dev_reviews/phase_01.md #4 and dev_reviews-alternative/phase_01.md's
        # closing note) -- this characterizes that flipping
        # --allow-degraded-independence does not change independence or
        # degradation_cause once two families are usable; it is not, and
        # structurally cannot be, a regression guard on a live gate.
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                self.route, self.route["nodes"], allow_degraded=True
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual(diagnostics["degradation_cause"], "")
        self.assertEqual(set(diagnostics["usable_families"]), {"codex", "claude"})

    def test_single_usable_family_degrades_with_typed_evidence(self):
        # assign_harnesses is side-effect free (G2): it never writes the
        # ledger itself. Persistence is exercised separately by
        # test_persist_degradation_forwards_route_and_reason_fields and
        # test_degraded_dry_run_writes_no_ledger_row.
        nodes = [single_family_node("plan"), single_family_node("plan-replica")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=True
            )
        self.assertEqual(independence, "degraded-same-harness")
        self.assertEqual(diagnostics["usable_families"], ["claude"])
        self.assertEqual(diagnostics["family_exclusions"], {
            "codex": ["dispatch-evidence-candidate-unsupported"],
            "opencode": ["dispatch-evidence-no-eligible-fallback"],
        })
        self.assertEqual(diagnostics["degradation_cause"], "single-usable-harness-family")
        self.assertIn("codex=unsupported", diagnostics["degradation_detail"])

    def test_single_usable_family_without_the_flag_raises_with_evidence(self):
        nodes = [single_family_node("plan"), single_family_node("plan-replica")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        self.assertEqual(ctx.exception.reason, "parallel-cross-harness-unavailable")
        self.assertEqual(ctx.exception.degradation_reason, "single-usable-harness-family")
        self.assertIsNone(ctx.exception.route_node)
        self.assertIn("usable=claude", ctx.exception.detail)
        self.assertIn("codex=unsupported", ctx.exception.detail)

    def _quality_peer_nodes(self, peer_families, aux_families):
        """Build a 2-peer + 1-auxiliary group whose policies derive
        quality-peer = {claude, codex}, with per-family eligibility lists."""
        def node(node_id, profile, families, leg_class):
            return {
                "id": node_id,
                "dispatch_depth": 2,
                "model_profile": profile,
                "leg_class": leg_class,
                "harness_affinity": "diverse",
                "harness_policy": {
                    "primary": ["claude", "codex"],
                    "relief": ["opencode"],
                    "last_resort": [],
                    "promote_relief_below": 0,
                },
                "fallback_hops": [
                    {"ordinal": 1, "fallback_hop": "cross-harness-headless", "candidates": [
                        {"child_harness": a, "status": "supported" if a in families else "unsupported"}
                        for a in BATCH.SUPPORTED_BATCH_HARNESSES
                    ]},
                ],
            }
        return [
            node("x", "deep", peer_families, "peer"),
            node("x-alternative", "balanced-deep", peer_families, "peer"),
            node("x-simplicity", "light", aux_families, "auxiliary"),
        ]

    def test_ac11_peer_gate_fails_closed_without_bypass_flag(self):
        # AC 11 negative: peer legs are only opencode-eligible while the
        # auxiliary leg is claude-eligible, so usable & quality_peer is
        # non-empty and no combination puts a peer on a quality-peer family.
        # The peer-gate hard filter must reject even under
        # --allow-degraded-independence (no escape), leaving row 0/process 0.
        nodes = self._quality_peer_nodes(
            peer_families=["opencode"], aux_families=["claude"]
        )
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            },
        }
        for allow_degraded in (False, True):
            with self.subTest(allow_degraded=allow_degraded):
                with mock.patch.object(
                    BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
                ), self.assertRaises(BATCH.BatchError) as ctx:
                    BATCH.assign_harnesses(
                        route, nodes, allow_degraded=allow_degraded
                    )
                self.assertEqual(
                    ctx.exception.reason, "parallel-cross-harness-unavailable"
                )
                self.assertEqual(
                    ctx.exception.degradation_reason, "sole-gate-non-peer-harness"
                )

    def test_ac11_peer_gate_positive_assigns_quality_peer_and_aux_opencode(self):
        # AC 11 positive: peer legs are claude/codex-eligible and the auxiliary
        # leg is opencode-eligible; the group may mix opencode on the auxiliary
        # while the gate authority stays on quality-peer families.
        nodes = self._quality_peer_nodes(
            peer_families=["claude", "codex"], aux_families=["opencode"]
        )
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            },
        }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=False
            )
        self.assertEqual(independence, "cross-harness")
        peer_rows = [row for row in rows if row[0]["leg_class"] == "peer"]
        aux_rows = [row for row in rows if row[0]["leg_class"] == "auxiliary"]
        self.assertTrue(
            all(row[1] in {"claude", "codex"} for row in peer_rows),
            peer_rows,
        )
        self.assertEqual([row[1] for row in aux_rows], ["opencode"])
        self.assertEqual(diagnostics["sole_gate"], "ok")
        self.assertEqual(
            diagnostics["quality_peer_families"], ["claude", "codex"]
        )

    def test_ac11_sole_gate_degradation_fails_closed_with_or_without_flag(self):
        # AC 11, second half: "peer leg에 배정 가능한 family가 opencode뿐이면
        # row 0 ... --allow-degraded-independence를 주어도 동일." No
        # quality-peer family is hard-eligible at all, so the sole-gate rule
        # itself refuses the assignment and the blanket flag does not relax it
        # (SD-100 13.30.2). Both flag values must reach the SAME refusal, and
        # its degradation_reason names the sole-gate rule, not a family
        # shortage. The previous fixture asserted rows={"opencode"} under the
        # flag -- sealing the exact behaviour the spec forbids.
        nodes = self._quality_peer_nodes(
            peer_families=["opencode"], aux_families=["opencode"]
        )
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            },
        }
        for allow_degraded in (False, True):
            with self.subTest(allow_degraded=allow_degraded):
                with mock.patch.object(
                    BATCH.DISPATCH_NODE, "resolve_checked_tuple",
                    side_effect=resolve_side_effect,
                ), self.assertRaises(BATCH.BatchError) as ctx:
                    BATCH.assign_harnesses(
                        route, nodes, allow_degraded=allow_degraded
                    )
                self.assertEqual(
                    ctx.exception.reason, "parallel-cross-harness-unavailable"
                )
                self.assertEqual(
                    ctx.exception.degradation_reason, "sole-gate-non-peer-harness"
                )
                self.assertEqual(
                    ctx.exception.detail,
                    "peer-gate:no-quality-peer-family-hard-eligible",
                )

    def test_single_usable_family_still_degrades_when_the_sole_gate_is_satisfied(self):
        # The AC 11 refusal above must not swallow the ordinary G2 path: with a
        # quality-peer family on the peer legs but only ONE usable family in the
        # whole group, `sole_gate` stays "ok" and --allow-degraded-independence
        # still reaches degraded-same-harness with the family-shortage cause.
        nodes = self._quality_peer_nodes(
            peer_families=["claude"], aux_families=["claude"]
        )
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            },
        }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        self.assertEqual(
            ctx.exception.degradation_reason, "single-usable-harness-family"
        )
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=True
            )
        self.assertEqual(independence, "degraded-same-harness")
        self.assertEqual(diagnostics["sole_gate"], "ok")
        self.assertEqual(
            diagnostics["degradation_cause"], "single-usable-harness-family"
        )
        self.assertEqual(diagnostics["usable_families"], ["claude"])
        self.assertEqual({row[1] for row in rows}, {"claude"})

    def test_harness_policy_absent_marks_sole_gate_not_applicable(self):
        # D8-①: no sealed harness_policy anywhere -> the peer-gate is
        # not-applicable and the filter is skipped entirely.
        nodes = [replica_node("plan", "diverse"), replica_node("plan-replica", "diverse")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=False
            )
        self.assertEqual(diagnostics["sole_gate"], "not-applicable")
        self.assertIsNone(diagnostics["quality_peer_families"])
        # fm M5 / alt M3: the batch receipt exposes `sole_gate` at the same
        # top level as the fallback receipt, carrying the same word.
        receipt, _ = BATCH.batch_receipt(
            args=SimpleNamespace(parallel_group="plan"), lifecycle="concurrent",
            independence="cross-harness", required_axes=[], realized_axes=[],
            degradation_reason="", legs=[], results=[], admitted=0,
            selection_diagnostics=diagnostics,
        )
        self.assertEqual(receipt["sole_gate"], "not-applicable")
        bare, _ = BATCH.batch_receipt(
            args=SimpleNamespace(parallel_group="plan"), lifecycle="concurrent",
            independence="cross-harness", required_axes=[], realized_axes=[],
            degradation_reason="", legs=[], results=[], admitted=0,
        )
        self.assertEqual(bare["sole_gate"], "not-applicable")

    def test_exclusion_detail_renders_every_reason_for_a_multi_reason_adapter(self):
        # G1: codex fails with a *different* typed reason on each leg
        # (candidate-unsupported on "plan", no-eligible-fallback on the
        # second leg, which has no codex row at all). The old
        # `next(iter(set))` pick was both hash-order nondeterministic and
        # discarded one of the two reasons outright; both must render,
        # deterministically ordered.
        codex_absent_node = {
            "id": "plan-replica-2", "dispatch_depth": 2,
            "fallback_hops": [
                {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                    {"child_harness": "claude", "status": "supported"},
                ]},
            ],
        }
        nodes = [single_family_node("plan"), codex_absent_node]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        self.assertEqual(ctx.exception.reason, "parallel-cross-harness-unavailable")
        self.assertIn("codex=unsupported+no-candidate", ctx.exception.detail)

    def test_no_usable_family_reports_typed_node_evidence(self):
        # zero_family_node must NOT be the first node processed: if it were,
        # the raise would fire before any other node's exclusions had
        # accumulated, and the per-node scoping this test guards (G3) would
        # never be exercised (dev_reviews/phase_01.md #3,
        # dev_reviews-alternative/phase_01.md's matching finding). "plan"
        # excludes codex for a *different* reason (ambiguous-candidate) than
        # "plan-replica" does; that must not leak into plan-replica's detail.
        nodes = [ambiguous_codex_node("plan"), zero_family_node("plan-replica")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=True)
        self.assertEqual(ctx.exception.reason, "parallel-headless-unavailable")
        self.assertEqual(ctx.exception.degradation_reason, "no-usable-harness-family")
        self.assertEqual(ctx.exception.route_node, "plan-replica")
        self.assertIn("node=plan-replica", ctx.exception.detail)
        self.assertIn("codex=unsupported", ctx.exception.detail)
        self.assertNotIn("ambiguous", ctx.exception.detail)

    def test_persist_degradation_forwards_route_and_reason_fields(self):
        with mock.patch.object(BATCH, "record_degradation") as record:
            BATCH._persist_degradation(
                self.base, self.route, route_node="plan",
                reason="no-usable-harness-family",
                detail="node=plan;codex=unsupported",
            )
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["route_node"], "plan")
        self.assertEqual(kwargs["reason"], "no-usable-harness-family")
        self.assertEqual(kwargs["agent_home"], self.base)
        self.assertEqual(kwargs["route_id"], self.route["route_id"])

    def test_degradation_persists_only_after_a_validated_new_start(self):
        diagnostics = {
            "degradation_cause": "single-usable-harness-family",
            "degradation_detail": "usable=claude;codex=unsupported",
        }
        with mock.patch.object(BATCH, "_persist_degradation") as persist:
            BATCH._persist_launched_degradation(
                self.base, self.route, diagnostics,
                [{"launch_state": "existing"}, {"launch_state": "failed"}],
            )
            persist.assert_not_called()
            BATCH._persist_launched_degradation(
                self.base, self.route, diagnostics,
                [{"launch_state": "started"}, {"launch_state": "existing"}],
            )
        persist.assert_called_once_with(
            self.base, self.route, route_node=None,
            reason="single-usable-harness-family",
            detail="usable=claude;codex=unsupported",
        )

    def test_degraded_dry_run_writes_no_ledger_row(self):
        # G2: assign_harnesses is pure and only a validated newly-started child
        # can persist; a dry-run preview must leave no row behind.
        output = io.StringIO()
        with mock.patch.object(BATCH, "record_degradation") as record:
            self._degraded_dry_run_receipt(output)
        record.assert_not_called()

    def _degraded_dry_run_receipt(self, output):
        assignments = [
            (self.route["nodes"][0], "codex", "same-harness-headless", 1),
            (self.route["nodes"][1], "codex", "same-harness-headless", 1),
        ]
        diagnostics = {
            "families_considered": ["codex", "claude", "opencode"],
            "usable_families": ["codex"],
            "family_exclusions": {"claude": ["dispatch-evidence-candidate-unsupported"]},
            "capacity": {"codex": None, "claude": None, "opencode": None},
            "degradation_cause": "single-usable-harness-family",
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "assign_harnesses",
                return_value=(assignments, "degraded-same-harness", diagnostics),
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv("dry-run") + ["--allow-degraded-independence"])
        return rc, json.loads(output.getvalue())

    def test_ac11_sole_gate_refusal_carries_its_reason_into_the_cli_receipt(self):
        # M4: the AC 11 refusal's typed cause lived only on the exception object
        # and `fail()` dropped it, so an operator saw
        # `parallel-cross-harness-unavailable /
        # peer-gate:no-quality-peer-family-hard-eligible` -- which reads as a
        # shortage of harness families -- and nothing in the cycle recorded that
        # the SOLE-GATE rule is what refused the stage. PRD 13.30.2 says there is
        # no silent path. The AC 11 assertion is promoted from the exception
        # object to the CLI receipt a consumer actually reads.
        for allow_degraded in (False, True):
            with self.subTest(allow_degraded=allow_degraded):
                output = io.StringIO()
                error = BATCH.BatchError(
                    "parallel-cross-harness-unavailable",
                    "peer-gate:no-quality-peer-family-hard-eligible",
                    degradation_reason="sole-gate-non-peer-harness",
                )
                argv = self.argv("dry-run")
                if allow_degraded:
                    argv = argv + ["--allow-degraded-independence"]
                with contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch.object(
                        BATCH, "load_route", return_value=self.route))
                    stack.enter_context(mock.patch.object(
                        BATCH, "assign_harnesses", side_effect=error))
                    stack.enter_context(mock.patch.object(
                        BATCH, "resolve_agent_home", return_value=self.base))
                    stack.enter_context(mock.patch.object(
                        BATCH, "resolve_global_registry",
                        return_value=SimpleNamespace(path=self.jobs)))
                    stack.enter_context(mock.patch.object(
                        BATCH.subprocess, "check_output", return_value=str(self.base)))
                    stack.enter_context(mock.patch.dict(os.environ, {
                        "AGENT_DISPATCH_SELF_SLUG": "owner",
                        "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                        "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                        "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                        "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                    }))
                    with contextlib.redirect_stdout(output):
                        rc = BATCH.main(argv)
                receipt = json.loads(output.getvalue())
                self.assertNotEqual(rc, 0)
                self.assertEqual(receipt["state"], "blocked")
                self.assertEqual(receipt["reason"], "parallel-cross-harness-unavailable")
                self.assertEqual(
                    receipt["degradation_reason"], "sole-gate-non-peer-harness"
                )
                self.assertEqual(
                    receipt["detail"], "peer-gate:no-quality-peer-family-hard-eligible"
                )
        # a refusal that carries no typed cause does not grow an empty field
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "assign_harnesses",
                side_effect=BATCH.BatchError("route-record-invalid", "fixture")))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry",
                return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(
                BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                BATCH.main(self.argv("dry-run"))
        self.assertNotIn("degradation_reason", json.loads(output.getvalue()))

    def test_degraded_batch_keeps_the_manifest_stable_degradation_reason(self):
        output = io.StringIO()
        rc, receipt = self._degraded_dry_run_receipt(output)
        self.assertEqual(rc, 0)
        self.assertEqual(receipt["state"], "validated")
        self.assertEqual(receipt["degradation_reason"], "cross-harness-unavailable-user-allowed")
        self.assertEqual(receipt["selection_diagnostics"]["degradation_cause"], "single-usable-harness-family")

    def test_degraded_batch_never_claims_a_cross_harness_realized_axis(self):
        output = io.StringIO()
        _rc, receipt = self._degraded_dry_run_receipt(output)
        self.assertNotIn("cross-harness", receipt["realized_independence_axes"])

    def test_ledger_detail_survives_a_three_family_exclusion(self):
        node_id = "plan-" + "x" * 300
        nodes = [three_family_excluded_node(node_id), single_family_node("plan-replica")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=True)
        self.assertEqual(ctx.exception.degradation_reason, "no-usable-harness-family")
        self.assertEqual(ctx.exception.route_node, node_id)
        self.assertIn("codex=unsupported", ctx.exception.detail)
        with mock.patch.object(BATCH, "record_degradation") as record:
            BATCH._persist_degradation(
                self.base, route, route_node=ctx.exception.route_node,
                reason=ctx.exception.degradation_reason, detail=ctx.exception.detail,
            )
        self.assertLessEqual(len(record.call_args.kwargs["detail"]), 512)

    def test_unknown_capacity_harness_is_still_selectable_in_a_batch(self):
        # Item 4's anti-regression on the batch side: ordering_score's neutral
        # treatment of an unknown gauge must never turn into an eligibility
        # gate, or an OpenCode-only leg (no proactive gauge by design) would
        # fail the whole batch instead of just being ordered neutrally.
        node1 = {
            "id": "plan", "dispatch_depth": 2,
            "fallback_hops": [
                {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                    {"child_harness": "opencode", "status": "supported"},
                ]},
            ],
        }
        node2 = {
            "id": "plan-replica", "dispatch_depth": 2,
            "fallback_hops": [
                {"ordinal": 1, "fallback_hop": "same-harness-headless", "candidates": [
                    {"child_harness": "claude", "status": "supported"},
                ]},
            ],
        }
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "dispatch_allocation": {
                "strategy": "capacity-aware", "window": 30,
                "harness_order": ["claude", "codex", "opencode"],
            },
        }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 80, "codex": 80, "opencode": None,
        }):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, [node1, node2], allow_degraded=False, jobs=self.jobs
            )
        self.assertEqual(independence, "cross-harness")
        self.assertEqual({row[1] for row in rows}, {"opencode", "claude"})

    def test_atomic_denial_starts_no_wrapper(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(BATCH, "reserve_batch", side_effect=BATCH.BatchError("model-worker-governor-denied")))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 75)
        popen.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual((receipt["admitted"], receipt["spawned"]), (0, 0))

    def test_missing_parent_runtime_tuple_stops_before_reservation(self):
        output = io.StringIO()
        with mock.patch.object(BATCH, "load_route", return_value=self.route), \
             mock.patch.object(BATCH, "reserve_batch") as reserve, \
             mock.patch.dict(os.environ, {
                 "AGENT_DISPATCH_SELF_SLUG": "owner",
                 "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
             }, clear=True), \
             contextlib.redirect_stdout(output):
            rc = BATCH.main(self.argv())
        self.assertEqual(rc, 65)
        reserve.assert_not_called()
        self.assertEqual(
            json.loads(output.getvalue())["reason"],
            "parent-runtime-identity-missing",
        )

    def _governor_subprocess_result(self, stderr, rc=1):
        return SimpleNamespace(stdout="", stderr=stderr, returncode=rc)

    def test_ac6_reserve_batch_classifies_shortfall_from_general_denial(self):
        # AC 6: a capacity/budget shortfall is typed
        # governor-atomic-admission-shortfall; other governor failures stay
        # model-worker-governor-denied. Both admit row 0 / model process 0.
        for marker, expected in (
            ("rolling model-worker start budget reached", "governor-atomic-admission-shortfall"),
            ("global model-worker cap reached", "governor-atomic-admission-shortfall"),
            ("dispatch class cap reached", "governor-atomic-admission-shortfall"),
            ("kill switch active", "model-worker-governor-denied"),
            ("", "model-worker-governor-denied"),
        ):
            with self.subTest(marker=marker, expected=expected):
                with mock.patch.object(
                    BATCH.subprocess, "run",
                    return_value=self._governor_subprocess_result(stderr=marker),
                ):
                    with self.assertRaises(BATCH.BatchError) as ctx:
                        BATCH.reserve_batch(
                            Path("/tmp/governor"), Path("/tmp/root"),
                            [{"attempt_id": "att-x"}],
                            manifest={"parallel_group": "plan"},
                            manifest_digest="sha256:" + "a" * 64,
                        )
                self.assertEqual(ctx.exception.reason, expected)

    def test_ac6_shortfall_fails_closed_with_no_partial_admit(self):
        output = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
        assignments = [
            (self.route["nodes"][0], "codex", "same-harness-headless", 1),
            (self.route["nodes"][1], "claude", "cross-harness-headless", 2),
        ]
        stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
        stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
        stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
        stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
        stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
        stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
        stack.enter_context(mock.patch.object(BATCH, "reserve_batch", side_effect=BATCH.BatchError("governor-atomic-admission-shortfall", "rolling model-worker start budget reached")))
        popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
        stack.enter_context(mock.patch.dict(os.environ, {
            "AGENT_DISPATCH_SELF_SLUG": "owner",
            "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
            "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
            "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
        }))
        with stack, contextlib.redirect_stdout(output):
            rc = BATCH.main(self.argv())
        self.assertEqual(rc, 75)
        popen.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual((receipt["admitted"], receipt["spawned"]), (0, 0))
        self.assertEqual(receipt["reason"], "governor-atomic-admission-shortfall")

    def test_both_wrappers_exist_before_either_is_joined(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        created: list[object] = []

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs
                self.pid = 10000 + len(created)
                self.returncode = 0
                created.append(self)

            def communicate(self):
                if len(created) != 2:
                    raise AssertionError("communicate began before both wrapper legs spawned")
                return success_receipt(self.command), ""

            def poll(self):
                return self.returncode

        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(BATCH, "reserve_batch", return_value=["a" * 32, "b" * 32]))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen", side_effect=FakeProcess))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 0)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(proc.kwargs["start_new_session"] for proc in created))
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "launched")
        self.assertEqual([leg["adapter"] for leg in receipt["legs"]], ["codex", "claude"])
        self.assertNotEqual(receipt["legs"][0]["attempt_id"], receipt["legs"][1]["attempt_id"])

    def test_opencode_owner_batch_reserves_spawns_joins_and_receipts(self):
        route = json.loads(json.dumps(self.route))
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        assignments = [
            (route["nodes"][0], "opencode", "cross-harness-headless", 2),
            (route["nodes"][1], "claude", "cross-harness-headless", 2),
        ]
        output = io.StringIO()
        created: list[object] = []
        joined: list[str] = []

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.kwargs = kwargs
                self.pid = 11000 + len(created)
                self.returncode = 0
                created.append(self)

            def communicate(self):
                if len(created) != 2:
                    raise AssertionError("join began before both OpenCode-owner batch legs spawned")
                joined.append(self.command[self.command.index("--adapter") + 1])
                return success_receipt(self.command), ""

            def poll(self):
                return self.returncode

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=route))
            stack.enter_context(mock.patch.object(
                BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(
                BATCH.subprocess, "check_output", return_value=str(self.base)
            ))
            reserve = stack.enter_context(mock.patch.object(
                BATCH, "reserve_batch", return_value=["a" * 32, "b" * 32]
            ))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            stack.enter_context(mock.patch.object(
                BATCH.subprocess, "Popen", side_effect=FakeProcess
            ))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "opencode",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())

        self.assertEqual(rc, 0)
        self.assertEqual(len(created), 2)
        self.assertEqual(set(joined), {"opencode", "claude"})
        pending = reserve.call_args.args[2]
        manifest = reserve.call_args.kwargs["manifest"]
        self.assertEqual({leg["adapter"] for leg in pending}, {"opencode", "claude"})
        self.assertEqual(
            {member["harness"] for member in manifest["members"]},
            {"opencode", "claude"},
        )
        self.assertEqual(
            reserve.call_args.kwargs["manifest_digest"],
            BATCH.build_manifest(
                parallel_group="plan",
                route_id=route["route_id"],
                parent_attempt_id="att-parent-fixture",
                independence="cross-harness",
                required_independence_axes=[
                    "cross-harness", "model-profile", "perspective"
                ],
                realized_independence_axes=[
                    "cross-harness", "model-profile", "perspective"
                ],
                members=manifest["members"],
            )[1],
        )
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "launched")
        self.assertEqual(receipt["registered"], 1)
        self.assertEqual(receipt["started"], 1)
        self.assertEqual(receipt["child_spawned"], 1)
        self.assertEqual(receipt["concurrent_launch"], 1)
        self.assertEqual(receipt["newly_started"], 2)
        self.assertEqual(
            {leg["adapter"] for leg in receipt["legs"]},
            {"opencode", "claude"},
        )
        self.assertTrue(all(leg["check"] == "ok" for leg in receipt["legs"]))
        self.assertTrue(all(leg["registered"] == "1" for leg in receipt["legs"]))
        self.assertTrue(all(leg["started"] == "1" for leg in receipt["legs"]))
        self.assertTrue(all(leg["child_spawned"] == "1" for leg in receipt["legs"]))

    def test_second_wrapper_spawn_failure_preserves_started_sibling(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        created: list[object] = []

        class FakeProcess:
            pid = 10001
            returncode = 0

            def __init__(self, command):
                self.command = command

            def communicate(self):
                return success_receipt(self.command), ""

        def spawn(command, **kwargs):
            if created:
                raise OSError("fixture second-wrapper failure")
            proc = FakeProcess(command)
            created.append(proc)
            return proc

        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(BATCH, "reserve_batch", return_value=["a" * 32, "b" * 32]))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen", side_effect=spawn))
            killpg = stack.enter_context(mock.patch.object(BATCH.os, "killpg"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 70)
        self.assertEqual(len(created), 1)
        killpg.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "partial-failure")
        self.assertEqual(receipt["admitted"], 2)
        self.assertEqual(receipt["legs"][0]["child_spawned"], "1")
        self.assertEqual(receipt["legs"][1]["reason"], "parallel-wrapper-spawn-failed")

    def test_slug_truncation_preserves_distinct_node_identity(self):
        prefix = "x" * 300
        first = BATCH.replica_slug(prefix, "plan")
        second = BATCH.replica_slug(prefix, "plan-replica")
        self.assertLessEqual(len(first), 120)
        self.assertLessEqual(len(second), 120)
        self.assertNotEqual(first, second)

    def test_attempt_identity_changes_with_parent_attempt_generation(self):
        node = self.route["nodes"][0]
        slug = BATCH.replica_slug("fixture", node["id"])
        first = BATCH.stable_attempt_id(
            self.route, node, slug, "owner", "att-parent-one", "codex", 1
        )
        second = BATCH.stable_attempt_id(
            self.route, node, slug, "owner", "att-parent-two", "codex", 1
        )
        self.assertNotEqual(first, second)

    def test_duplicate_receipt_is_idempotent_not_new_launch(self):
        leg = {
            "node": "plan",
            "adapter": "codex",
            "attempt_id": "att-existing-fixture",
        }
        command = [
            "dispatch-node.py", "--adapter", "codex",
            "--attempt-id", "att-existing-fixture",
        ]
        result = BATCH.wrapper_result(
            leg,
            SimpleNamespace(returncode=0),
            success_receipt(command, started="0", duplicate="1"),
            "",
        )
        self.assertEqual(result["launch_state"], "existing")

    def test_wrapper_receipt_missing_literal_launch_triplet_fails_closed(self):
        leg = {
            "node": "plan",
            "adapter": "codex",
            "attempt_id": "att-missing-triplet-fixture",
        }
        command = [
            "dispatch-node.py", "--adapter", "codex",
            "--attempt-id", "att-missing-triplet-fixture",
        ]
        for field in ("registered", "started", "child_spawned"):
            with self.subTest(field=field):
                incomplete = success_receipt(command).replace(f"{field}=1\n", "")
                result = BATCH.wrapper_result(
                    leg,
                    SimpleNamespace(returncode=0),
                    incomplete,
                    "",
                )
                self.assertEqual(result["launch_state"], "failed")
                self.assertEqual(result[field], "unknown")
                self.assertEqual(result["reason"], "invalid-wrapper-receipt")

    def test_duplicate_batch_state_does_not_claim_concurrent_launch(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        for leg in self.legs(assignments):
            self.write_existing(leg)

        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "idempotent-existing")
        self.assertEqual(receipt["registered"], 0)
        self.assertEqual(receipt["started"], 0)
        self.assertEqual(receipt["child_spawned"], 0)
        self.assertEqual(receipt["concurrent_launch"], 0)
        self.assertEqual(receipt["newly_started"], 0)
        self.assertEqual(receipt["existing"], 2)
        self.assertEqual(receipt["admitted"], 0)
        reserve.assert_not_called()
        popen.assert_not_called()

    def test_retry_with_new_display_prefix_reuses_registered_slugs(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        original_legs = self.legs(assignments)
        for leg in original_legs:
            leg["slug"] = BATCH.replica_slug("original-display", str(leg["node"]))
            self.write_existing(leg)

        argv = self.argv()
        argv[argv.index("--slug-prefix") + 1] = "renamed-display"
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)

        self.assertEqual(rc, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "idempotent-existing")
        self.assertEqual(
            {leg["slug"] for leg in receipt["legs"]},
            {leg["slug"] for leg in original_legs},
        )
        reserve.assert_not_called()
        popen.assert_not_called()

    def test_active_idempotent_recall_bypasses_real_saturated_governor(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        for leg in self.legs(assignments):
            self.write_existing(leg)
        raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
        start = raw[raw.rfind(")") + 2:].split()[19]
        governor_root = self.base / ".runtime" / "model-worker-governor"
        governor_root.mkdir(parents=True)
        (governor_root / "state.json").write_text(json.dumps({
            "schema_version": 2,
            "claims": {},
            "reservations": {},
            "starts": [],
            "leases": {
                f"lease-{index}": {
                    "class": "dispatch",
                    "pid": os.getpid(),
                    "starttime": start,
                    "acquired_at": 0,
                }
                for index in range(3)
            },
        }), encoding="utf-8")
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                "AGENT_ARTIFACT_ROOT": str(self.base),
                "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 0)
        popen.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["state"], "idempotent-existing")

    def test_partial_retry_reserves_and_starts_only_missing_leg(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        existing, _missing = self.legs(assignments)
        self.write_existing(existing)

        class FakeProcess:
            returncode = 0
            pid = 10001

            def __init__(self, command, **kwargs):
                self.command = command

            def communicate(self):
                return success_receipt(self.command), ""

        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch", return_value=["b" * 32]))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen", side_effect=FakeProcess))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 0)
        self.assertEqual(reserve.call_count, 1)
        self.assertEqual(len(reserve.call_args.args[2]), 1)
        self.assertEqual(popen.call_count, 1)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "idempotent-mixed")
        self.assertEqual((receipt["admitted"], receipt["existing"]), (1, 1))

    def test_terminal_failed_duplicate_is_not_reported_as_existing(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        failed, _missing = self.legs(assignments)
        self.write_existing(failed, status="done", note="dead-capacity")
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 70)
        reserve.assert_not_called()
        popen.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "partial-failure")

    def test_pidless_claimed_peer_is_not_idempotent_active(self):
        stack, assignments = self.common_patches()
        first, _second = self.legs(assignments)
        self.write_existing(first, live_identity=False)
        output = io.StringIO()
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)
            ))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(
                BATCH.subprocess, "check_output", return_value=str(self.base)
            ))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 70)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "partial-failure")
        reserve.assert_not_called()
        self.assertEqual(receipt["legs"][0]["launch_state"], "failed")

    def test_dead_unstarted_fence_is_reset_to_registered_only(self):
        first, _second = self.legs()
        proc = subprocess.Popen(["sleep", "0.05"], start_new_session=True)
        identity = process_launch_identity(proc.pid)
        self.write_existing(first, live_identity=False)
        text = self.jobs.read_text(encoding="utf-8").rstrip("\n")
        text += ",launch_lifecycle=detached," + ",".join(
            f"{key}={value}" for key, value in identity.items()
        )
        self.jobs.write_text(text + "\n", encoding="utf-8")
        proc.wait(timeout=5)
        manifest, manifest_digest, leg_digests = BATCH.build_manifest(
            replica_group="plan",
            route_id=self.route["route_id"],
            parent_attempt_id="att-parent-fixture",
            independence="cross-harness",
            members=[{
                "assignment_sha256": str(leg["assignment_sha256"]),
                "attempt_id": str(leg["attempt_id"]),
                "route_node": str(leg["node"]),
                "harness": str(leg["adapter"]),
                "fallback_hop": str(leg["hop"]),
                "fallback_ordinal": int(leg["ordinal"]),
                "model_profile": str(leg["model_profile"]),
                "perspective": str(leg["perspective"]),
                "parallel_leg_index": int(leg["parallel_leg_index"]),
                "leg_class": str(leg.get("leg_class", "peer")),
            } for leg in self.legs()],
            required_independence_axes=["cross-harness", "model-profile", "perspective"],
            realized_independence_axes=["cross-harness", "model-profile", "perspective"],
        )
        self.assertEqual(manifest["replica_group"], "plan")
        result = BATCH.existing_leg_result(
            self.jobs,
            first,
            self.route,
            repo=str(self.base),
            parent="owner",
            parent_attempt_id="att-parent-fixture",
            parallel_group="plan",
            declared_size=2,
            manifest_digest=manifest_digest,
            leg_digest=leg_digests[str(first["attempt_id"])],
            agent_home=self.base,
        )
        self.assertIsNone(result)
        metadata = BATCH.parse_registry_metadata(
            self.jobs.read_text(encoding="utf-8").strip().split("\t", 5)[5]
        )
        self.assertEqual(metadata["launch_claimed"], "0")
        self.assertNotIn("pid", metadata)

    def test_collection_exception_keeps_typed_receipt_and_cleans_all_tokens(self):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        created = []

        class FakeProcess:
            pid = 10001
            returncode = 0

            def __init__(self, command, **kwargs):
                self.command = command
                self.index = len(created)
                created.append(self)

            def communicate(self):
                if self.index == 0:
                    raise RuntimeError("fixture collector failure")
                return success_receipt(self.command), ""

            def poll(self):
                return self.returncode

        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(BATCH, "reserve_batch", return_value=["a" * 32, "b" * 32]))
            cancel = stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen", side_effect=FakeProcess))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 70)
        self.assertEqual(cancel.call_count, 2)
        receipt = json.loads(output.getvalue())
        self.assertEqual(len(receipt["legs"]), 2)
        self.assertTrue(any(
            leg["reason"] == "parallel-wrapper-collect-failed:RuntimeError"
            for leg in receipt["legs"]
        ))

    def test_signal_relay_forwards_to_existing_and_late_wrapper_groups(self):
        first = SimpleNamespace(pid=101, poll=lambda: None)
        second = SimpleNamespace(pid=202, poll=lambda: None)
        with mock.patch.object(BATCH.os, "killpg") as killpg:
            relay = BATCH.BatchSignalRelay()
            relay.processes.append(first)
            relay._forward(BATCH.signal.SIGTERM, None)
            relay.add(second)
        self.assertEqual(relay.received, [BATCH.signal.SIGTERM])
        self.assertEqual(
            killpg.call_args_list,
            [
                mock.call(101, BATCH.signal.SIGTERM),
                mock.call(202, BATCH.signal.SIGTERM),
            ],
        )

    def test_wrapper_output_tail_is_memory_bounded(self):
        proc = BATCH.subprocess.Popen(
            [
                os.environ.get("PYTHON", "python3"),
                "-c",
                "import sys;print('x'*200000+'OUT');print('y'*200000+'ERR',file=sys.stderr)",
            ],
            text=True,
            stdout=BATCH.subprocess.PIPE,
            stderr=BATCH.subprocess.PIPE,
        )
        stdout, stderr = BATCH.bounded_process_output(proc)
        self.assertLessEqual(len(stdout.encode()), BATCH.OUTPUT_TAIL_BYTES)
        self.assertLessEqual(len(stderr.encode()), BATCH.OUTPUT_TAIL_BYTES)
        self.assertTrue(stdout.rstrip().endswith("OUT"))
        self.assertTrue(stderr.rstrip().endswith("ERR"))

    def _subdivision_route_and_manifest(self, sessions):
        node = dict(replica_node("plan", "codex"))
        node["completion_gate"] = "code-plan-check"
        node["subdivision"] = {
            "min_intensity": "strong", "max_slices": 4, "disjointness": "exact-fixed-files",
        }
        node["write_scope"] = ["source/**"]
        node2 = dict(replica_node("plan-replica", "claude"))
        node2["completion_gate"] = "code-plan-check"
        route = dict(self.route)
        route["nodes"] = [node, node2]
        for session in sessions:
            Path(session["phase_brief"]).write_text("slice brief\n", encoding="utf-8")
        manifest_path = self.base / "chain.json"
        manifest = {
            "schema_version": 1,
            "kind": "stage-session-chain",
            "chain_id": "ssc-fixture",
            "mode": "parallel",
            "worktree": str(self.base),
            "route_file": str(self.route_path),
            "route_id": route["route_id"],
            "route_hash": route["route_hash"],
            "route_node": "plan",
            "completion_gate": "code-plan-check",
            "sessions": sessions,
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return route, manifest_path

    def _subdivision_dry_run(self, route, manifest_path):
        output = io.StringIO()
        def fake_assign_harnesses(_route, nodes, **_kw):
            harnesses = ["codex", "claude", "opencode", "codex"]
            assignments = [
                (node, harnesses[index % len(harnesses)], "same-harness-headless", 1)
                for index, node in enumerate(nodes)
            ]
            diagnostics = {
                "families_considered": ["codex", "claude", "opencode"],
                "usable_families": ["codex"],
                "family_exclusions": {},
                "capacity": {"codex": None, "claude": None, "opencode": None},
                "degradation_cause": "",
            }
            return assignments, "cross-harness", diagnostics
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", side_effect=fake_assign_harnesses))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            argv = self.argv("dry-run") + ["--subdivision-manifest", str(manifest_path)]
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        return rc, output.getvalue()

    def test_g7_subdivision_fallback_descends_to_a_single_session(self):
        # An overlapping fixed_files pair cannot be proven disjoint, so
        # validate_subdivision_or_fallback returns the typed fallback reason.
        # G7: that must actually change what gets launched -- one leg, not two.
        shared = str(self.base / "source" / "shared.py")
        sessions = [
            {"subsession_id": "ss-slice-1", "attempt_id": "att-slice-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "adapter": "codex", "slug": "slice-1", "phase_brief": str(self.base / "b1.md"),
             "node": "plan",
             "fixed_files": [shared], "narrow_verify": "true", "expected_round_trips": 2},
            {"subsession_id": "ss-slice-2", "attempt_id": "att-slice-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "adapter": "codex", "slug": "slice-2", "phase_brief": str(self.base / "b2.md"),
             "node": "plan-replica",
             "fixed_files": [shared], "narrow_verify": "true", "expected_round_trips": 2},
        ]
        route, manifest_path = self._subdivision_route_and_manifest(sessions)
        with mock.patch.object(BATCH, "record_degradation") as record:
            rc, out = self._subdivision_dry_run(route, manifest_path)
        self.assertEqual(rc, 0, out)
        receipt = json.loads(out)
        self.assertEqual(receipt["state"], "single-session-required")
        self.assertEqual(receipt["reason"], "subdivision-disjointness-unproven")
        record.assert_called_once()
        self.assertEqual(record.call_args.kwargs["reason"], "subdivision-disjointness-unproven")

    def test_g7_subdivision_manifest_is_consumed_into_legs(self):
        # A disjoint, in-scope, exactly-sized manifest validates successfully;
        # G7: its sessions must be consumed, not discarded -- each launched
        # leg carries the exact subsession_id/fixed_files the check proved safe.
        sessions = [
            {"subsession_id": "ss-slice-1", "attempt_id": "att-slice-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "adapter": "codex", "slug": "slice-1", "phase_brief": str(self.base / "b1.md"),
             "node": "plan",
             "fixed_files": [str(self.base / "source" / "a.py")],
             "narrow_verify": "true", "expected_round_trips": 2},
            {"subsession_id": "ss-slice-2", "attempt_id": "att-slice-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "adapter": "codex", "slug": "slice-2", "phase_brief": str(self.base / "b2.md"),
             "node": "plan-replica",
             "fixed_files": [str(self.base / "source" / "b.py")],
             "narrow_verify": "true", "expected_round_trips": 2},
        ]
        route, manifest_path = self._subdivision_route_and_manifest(sessions)
        with mock.patch.object(BATCH, "record_degradation") as record:
            rc, out = self._subdivision_dry_run(route, manifest_path)
        self.assertEqual(rc, 0, out)
        receipt = json.loads(out)
        self.assertEqual(len(receipt["legs"]), 2)
        record.assert_not_called()
        got = {leg["subsession_id"]: leg["fixed_files"] for leg in receipt["legs"]}
        self.assertEqual(got["ss-slice-1"], [str(self.base / "source" / "a.py")])
        self.assertEqual(got["ss-slice-2"], [str(self.base / "source" / "b.py")])
        by_node = {leg["node"]: leg["subsession_id"] for leg in receipt["legs"]}
        self.assertEqual(by_node, {"plan": "ss-slice-1", "plan-replica": "ss-slice-2"})

    def test_n1_slice_binds_by_declared_key_not_manifest_order(self):
        # N1: `assign_harnesses` returns legs in `nodes` order while the
        # manifest's session order is whatever its author wrote. Binding by
        # position hands a slice's fixed_files to the wrong leg the moment the
        # two orders differ, and only the count is checked -- so the proven
        # disjointness stops describing what actually runs. Here the manifest
        # is written in the REVERSE order and each slice must still land on the
        # leg it names.
        sessions = [
            {"subsession_id": "ss-slice-2", "attempt_id": "att-slice-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "adapter": "codex", "slug": "slice-2", "phase_brief": str(self.base / "b2.md"),
             "node": "plan-replica",
             "fixed_files": [str(self.base / "source" / "b.py")],
             "narrow_verify": "true", "expected_round_trips": 2},
            {"subsession_id": "ss-slice-1", "attempt_id": "att-slice-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "adapter": "codex", "slug": "slice-1", "phase_brief": str(self.base / "b1.md"),
             "node": "plan",
             "fixed_files": [str(self.base / "source" / "a.py")],
             "narrow_verify": "true", "expected_round_trips": 2},
        ]
        route, manifest_path = self._subdivision_route_and_manifest(sessions)
        rc, out = self._subdivision_dry_run(route, manifest_path)
        self.assertEqual(rc, 0, out)
        legs = {leg["node"]: leg for leg in json.loads(out)["legs"]}
        self.assertEqual(legs["plan"]["subsession_id"], "ss-slice-1")
        self.assertEqual(legs["plan"]["fixed_files"], [str(self.base / "source" / "a.py")])
        self.assertEqual(legs["plan-replica"]["subsession_id"], "ss-slice-2")
        self.assertEqual(legs["plan-replica"]["fixed_files"], [str(self.base / "source" / "b.py")])
        # `leg_index` is the accepted positional spelling of the same key
        indexed = json.loads(json.dumps(sessions))
        for session in indexed:
            session["leg_index"] = 0 if session["node"] == "plan" else 1
            del session["node"]
        route, manifest_path = self._subdivision_route_and_manifest(indexed)
        rc, out = self._subdivision_dry_run(route, manifest_path)
        self.assertEqual(rc, 0, out)
        legs = {leg["node"]: leg["subsession_id"] for leg in json.loads(out)["legs"]}
        self.assertEqual(legs, {"plan": "ss-slice-1", "plan-replica": "ss-slice-2"})

    def test_n1_unbound_or_conflicting_slice_keys_are_typed_refusals(self):
        # A session naming no leg is refused rather than assumed to be at its
        # own list position, and two sessions naming the same leg is a refusal
        # rather than a silent last-writer-wins.
        def build(mutate):
            sessions = [
                {"subsession_id": "ss-slice-1", "attempt_id": "att-slice-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "adapter": "codex", "slug": "slice-1", "phase_brief": str(self.base / "b1.md"),
                 "node": "plan",
                 "fixed_files": [str(self.base / "source" / "a.py")],
                 "narrow_verify": "true", "expected_round_trips": 2},
                {"subsession_id": "ss-slice-2", "attempt_id": "att-slice-bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                 "adapter": "codex", "slug": "slice-2", "phase_brief": str(self.base / "b2.md"),
                 "node": "plan-replica",
                 "fixed_files": [str(self.base / "source" / "b.py")],
                 "narrow_verify": "true", "expected_round_trips": 2},
            ]
            mutate(sessions)
            return self._subdivision_route_and_manifest(sessions)
        for mutate, reason in (
            (lambda s: s[1].pop("node"), "subdivision-manifest-session-leg-unbound"),
            (lambda s: s[1].update(node="plan"), "subdivision-manifest-session-leg-duplicate"),
            (lambda s: s[1].update(node="not-a-leg"), "subdivision-manifest-session-leg-unknown"),
            (lambda s: [(x.pop("node"), x.update(leg_index=9)) for x in s],
             "subdivision-manifest-session-leg-unknown"),
        ):
            with self.subTest(reason=reason):
                route, manifest_path = build(mutate)
                rc, out = self._subdivision_dry_run(route, manifest_path)
                self.assertEqual(rc, 65, out)
                self.assertEqual(json.loads(out)["reason"], reason)

    def test_g7_subdivision_manifest_session_count_must_match_group_width(self):
        # A manifest that itself validates (3 disjoint, in-scope sessions, within
        # the node's declared max_slices=4) but does not match the realized
        # 2-way group width is a distinct typed rejection, not a silent
        # partial launch or an arbitrary pick of the first 2 sessions.
        sessions = [
            {"subsession_id": f"ss-slice-{n}", "attempt_id": f"att-slice-{n}{'a' * 26}",
             "adapter": "codex", "slug": f"slice-{n}", "phase_brief": str(self.base / f"b{n}.md"),
             "node": "plan" if n == 1 else f"plan-replica-{n}",
             "fixed_files": [str(self.base / "source" / f"{n}.py")],
             "narrow_verify": "true", "expected_round_trips": 2}
            for n in (1, 2, 3)
        ]
        route, manifest_path = self._subdivision_route_and_manifest(sessions)
        rc, out = self._subdivision_dry_run(route, manifest_path)
        self.assertNotEqual(rc, 0)
        self.assertIn("subdivision-manifest-session-count-mismatch", out)


class DispatchBatchIntegrationTest(unittest.TestCase):
    """Exercise the full checked two-way launch path with local fake CLIs."""

    @staticmethod
    def _write_fake_runtime(path: Path, harness: str) -> None:
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, time\n"
            f"harness={harness!r}\n"
            "node=os.environ.get('AGENT_ROUTE_NODE','')\n"
            "events=os.environ['FLEET_BATCH_EVENTS']\n"
            "def emit(event):\n"
            " payload=json.dumps({'harness':harness,'node':node,'event':event,"
            "'ns':time.monotonic_ns(),'pid':os.getpid()},sort_keys=True)\n"
            " fd=os.open(events,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
            " try: os.write(fd,(payload+'\\n').encode())\n"
            " finally: os.close(fd)\n"
            "emit('start')\n"
            "time.sleep(3.0)\n"
            "print('{}',flush=True)\n"
            "emit('end')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    @staticmethod
    def _events(path: Path) -> list[dict[str, object]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        rows = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    def test_real_cross_harness_batch_overlaps_and_is_visible_in_fleet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "fixture@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.name", "Fixture"],
                check=True,
            )
            (repo / "README.md").write_text("batch integration\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

            artifact_root = base / ".agent_reports"
            artifact_root.mkdir()
            evidence_path = base / "dispatch-evidence.json"
            evidence_path.write_text(
                json.dumps({
                    "tuples": [
                        {
                            "parent_harness": "codex",
                            "parent_transport": "headless",
                            "parent_sandbox": "workspace-write",
                            "child_harness": child,
                            "launch_authority": "conductor",
                            "status": "supported",
                            "probe_source": "integration-fixture",
                            "probe_time": "2026-07-24T00:00:00Z",
                            "failure_class": "",
                            "checked_worktree": str(repo.resolve()),
                            "failure_scope": "none",
                            "codex_command": "ok" if child == "codex" else "not-applicable",
                            "retry_on_isolated_worktree": 0,
                        }
                        for child in ("codex", "claude")
                    ],
                    "native_subagent": [],
                }, sort_keys=True),
                encoding="utf-8",
            )
            compile_result = subprocess.run(
                [
                    sys.executable, str(ROOT / "utilities" / "capability-route.py"),
                    "compile", "--capability", "autopilot-code",
                    "--capability-mode", "dev", "--intensity", "strong",
                    "--cwd", str(repo), "--artifact-root", str(artifact_root),
                    "--signal", "shared-contract", "--transport", "headless",
                    "--tracking", "tracked", "--dispatch-evidence", str(evidence_path),
                    "--spec-read", "integration-fixture",
                    "--drift-verdict", "within-spec", "--workflow-mode", "tracked",
                    "--artifact-guard", "integration-fixture",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={key: value for key, value in os.environ.items() if key != "AGENT_ARTIFACT_ROOT"},
            )
            self.assertEqual(
                compile_result.returncode, 0, compile_result.stdout + compile_result.stderr
            )
            route = json.loads(compile_result.stdout)
            route_path = (
                artifact_root / ".runtime" / "routes" / f"{route['route_id']}.json"
            )
            self.assertEqual(
                json.loads(route_path.read_text(encoding="utf-8")), route
            )
            frame_nodes = {
                node["id"] for node in route["nodes"]
                if (node.get("parallel_group") or node.get("replica_group")) == "frame"
            }
            plan_nodes = {
                node["id"] for node in route["nodes"]
                if (node.get("parallel_group") or node.get("replica_group")) == "plan"
            }
            self.assertEqual(frame_nodes, {"frame", "frame-alternative", "frame-contrarian"})
            self.assertEqual(plan_nodes, {"plan", "plan-alternative"})

            agent_home = base / "agent-home"
            agent_home.mkdir()
            for name in (
                "adapters", "capabilities", "codex_setting", "core", "hooks",
                "roles", "skills", "tools", "utilities",
            ):
                (agent_home / name).symlink_to(ROOT / name, target_is_directory=True)
            codex_home = base / "codex-home"
            projection = subprocess.run(
                [
                    str(ROOT / "adapters" / "codex" / "bin" / "install-runtime-projection.sh"),
                    "--skills-mode", "native",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "AGENT_HOME": str(agent_home),
                    "CODEX_HOME": str(codex_home),
                },
                check=False,
            )
            self.assertEqual(
                projection.returncode, 0, projection.stdout + projection.stderr
            )

            fake_bin = base / "bin"
            fake_bin.mkdir()
            events_path = base / "events.jsonl"
            self._write_fake_runtime(fake_bin / "codex", "codex")
            self._write_fake_runtime(fake_bin / "claude", "claude")
            claude_home = base / "claude-home"
            claude_home.mkdir()
            jobs = base / "jobs.log"
            parent_attempt = "att-integration-parent"
            raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="utf-8")
            parent_start = raw[raw.rfind(")") + 2 :].split()[19]
            jobs.write_text(
                f"2026-07-24T00:00:00Z\topen\t{repo}\t{repo}\towner\t"
                "attempt_schema_version=2,dispatch_depth=1,transport=headless,"
                "execution_surface=registered-headless,registered_worker=1,"
                "fallback_hop=same-harness-headless,worker_type=owner,"
                "capability=autopilot-code,intensity=strong,harness=codex,"
                "runtime_sandbox=workspace-write,"
                f"attempt_id={parent_attempt},pid={os.getpid()},pid_start={parent_start}\n",
                encoding="utf-8",
            )
            governor_root = artifact_root / ".runtime" / "model-worker-governor"
            env = {
                **os.environ,
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "AGENT_HOME": str(agent_home),
                "AGENT_ARTIFACT_ROOT": str(artifact_root),
                "AGENT_MODEL_GOVERNOR_ROOT": str(governor_root),
                "AGENT_DISPATCH_JOBS": str(jobs),
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": parent_attempt,
                "AGENT_DISPATCH_PARENT_SESSION_ID": "integration-parent-session",
                "AGENT_DISPATCH_PARENT_CWD": str(repo),
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
                "AGENT_DISPATCH_CHILD": "1",
                "CODEX_HOME": str(codex_home),
                "CLAUDE_CONFIG_DIR": str(claude_home),
                "CODEX_DISPATCH_EARLY_EXIT_WATCH": "8",
                "CLAUDE_DISPATCH_EARLY_EXIT_WATCH": "8",
                "FLEET_BATCH_EVENTS": str(events_path),
            }
            env.pop("AGENT_MODEL_GOVERNOR_RESERVATION", None)
            def launch_group(group: str) -> subprocess.Popen:
                return subprocess.Popen(
                    [
                        sys.executable, str(PATH), "--route", str(route_path),
                        "--parallel-group", group, "--action", "start",
                        "--slug-prefix", f"integration-{group}", "--parent", "owner",
                        "--qa", "standard", "--jobs", str(jobs),
                        "--log-dir", str(base / "logs"),
                        "--prompt-text", "Inspect the fixture independently.",
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )

            process = launch_group("frame")
            stdout = stderr = ""
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    events = self._events(events_path)
                    if sum(
                        row.get("event") == "start" and row.get("node") in frame_nodes
                        for row in events
                    ) == len(frame_nodes):
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                starts = [
                    row for row in self._events(events_path)
                    if row.get("event") == "start" and row.get("node") in frame_nodes
                ]
                if len(starts) != len(frame_nodes) and process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(
                    len(starts), len(frame_nodes),
                    f"batch exited={process.poll()} stdout={stdout} stderr={stderr} "
                    f"jobs={jobs.read_text(encoding='utf-8')}",
                )
                self.assertEqual({row["harness"] for row in starts}, {"codex", "claude"})
                self.assertFalse(
                    any(row.get("event") == "end" for row in self._events(events_path)),
                    "the first runtime ended before both cross-harness legs started",
                )

                tools_dir = str(ROOT / "tools")
                if tools_dir not in sys.path:
                    sys.path.insert(0, tools_dir)
                from fleet.collectors import dispatch as fleet_dispatch

                with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(agent_home),
                    "AGENT_ARTIFACT_ROOT": str(artifact_root),
                    "AGENT_DISPATCH_JOBS": str(jobs),
                    "CLAUDE_CONFIG_DIR": str(claude_home),
                }, clear=False):
                    fleet_jobs = fleet_dispatch.collect(jobs_path=str(jobs))
                visible = [
                    job for job in fleet_jobs
                    if job.parent_slug == "owner" and job.route_id == route["route_id"]
                    and job.route_node in frame_nodes
                ]
                self.assertEqual(
                    len(visible), len(frame_nodes),
                    [(job.slug, job.route_node, job.liveness) for job in fleet_jobs],
                )
                self.assertEqual({job.harness for job in visible}, {"codex", "claude"})
                self.assertEqual({job.route_node for job in visible}, frame_nodes)
                self.assertTrue(all(job.dispatch_depth == 2 for job in visible))
                self.assertTrue(all(job.attempt_contract_status == "current" for job in visible))
                self.assertTrue(all(job.liveness == "working" for job in visible))
                route_rows = [
                    job for job in fleet_jobs
                    if job.route_id == route["route_id"] and job.route_node in frame_nodes
                ]
                self.assertEqual(len(route_rows), len(frame_nodes))
                self.assertTrue(all(job.parent_slug == "owner" for job in route_rows))

                stdout, stderr = process.communicate(timeout=25)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        stdout, stderr = process.communicate(timeout=5)

            self.assertEqual(process.returncode, 0, stdout + stderr)
            receipt = json.loads(stdout.strip())
            self.assertEqual(receipt["state"], "launched")
            self.assertEqual(receipt["independence"], "cross-harness")
            self.assertEqual(receipt["concurrent_launch"], 1)
            self.assertEqual((receipt["admitted"], receipt["newly_started"]), (3, 3))
            self.assertEqual({leg["adapter"] for leg in receipt["legs"]}, {"codex", "claude"})

            for leg in receipt["legs"]:
                evidence = base / f"{leg['node']}.md"
                evidence.write_text(f"completed {leg['node']}\n", encoding="utf-8")
                completed = subprocess.run(
                    [
                        sys.executable, str(ROOT / "utilities" / "capability-route.py"),
                        "complete", "--route", str(route_path),
                        "--node", str(leg["node"]), "--evidence", str(evidence),
                        "--jobs", str(jobs), "--attempt-id", str(leg["attempt_id"]),
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

            plan_process = launch_group("plan")
            plan_stdout = plan_stderr = ""
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    plan_starts = [
                        row for row in self._events(events_path)
                        if row.get("event") == "start" and row.get("node") in plan_nodes
                    ]
                    if len(plan_starts) == 2 or plan_process.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertEqual(
                    len(plan_starts), 2,
                    f"plan batch exited={plan_process.poll()} jobs={jobs.read_text()}",
                )
                self.assertFalse(any(
                    row.get("event") == "end" and row.get("node") in plan_nodes
                    for row in self._events(events_path)
                ))
                with mock.patch.dict(os.environ, {
                    "AGENT_HOME": str(agent_home),
                    "AGENT_ARTIFACT_ROOT": str(artifact_root),
                    "AGENT_DISPATCH_JOBS": str(jobs),
                    "CLAUDE_CONFIG_DIR": str(claude_home),
                }, clear=False):
                    fleet_jobs = fleet_dispatch.collect(jobs_path=str(jobs))
                # Fleet retains completed route siblings for history; this
                # assertion is about the newly launched plan group, not every
                # earlier frame row sharing the route id.
                route_rows = [
                    job for job in fleet_jobs
                    if job.route_id == route["route_id"] and job.route_node in plan_nodes
                ]
                self.assertEqual(len(route_rows), 2)
                self.assertEqual({job.route_node for job in route_rows}, plan_nodes)
                self.assertEqual(len({job.attempt_id for job in route_rows}), 2)
                self.assertTrue(all(job.parent_slug == "owner" for job in route_rows))
                self.assertFalse(any(job.route_node == "one-shot" for job in route_rows))
                self.assertTrue(all(job.liveness == "working" for job in route_rows))
                registered = []
                for line in jobs.read_text(encoding="utf-8").splitlines():
                    fields = line.split("\t")
                    if len(fields) != 6:
                        continue
                    metadata = BATCH.parse_registry_metadata(fields[5])
                    if metadata.get("route_id") == route["route_id"]:
                        registered.append((fields[1], fields[4], metadata))
                self.assertEqual(len(registered), len(frame_nodes | plan_nodes))
                self.assertEqual(
                    {metadata["route_node"] for _status, _slug, metadata in registered},
                    frame_nodes | plan_nodes,
                )
                self.assertTrue(all(
                    metadata.get("parent") == "owner"
                    for _status, _slug, metadata in registered
                ))
                self.assertFalse(any(
                    metadata.get("route_node") == "one-shot"
                    for _status, _slug, metadata in registered
                ))
                plan_stdout, plan_stderr = plan_process.communicate(timeout=25)
            finally:
                if plan_process.poll() is None:
                    plan_process.terminate()
                    try:
                        plan_stdout, plan_stderr = plan_process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(plan_process.pid, signal.SIGKILL)
                        plan_stdout, plan_stderr = plan_process.communicate(timeout=5)
            self.assertEqual(plan_process.returncode, 0, plan_stdout + plan_stderr)
            plan_receipt = json.loads(plan_stdout.strip())
            self.assertEqual(plan_receipt["state"], "launched")
            self.assertEqual(plan_receipt["concurrent_launch"], 1)
            self.assertEqual({leg["node"] for leg in plan_receipt["legs"]}, plan_nodes)

            events = self._events(events_path)
            timings = {}
            for group, nodes in (("frame", frame_nodes), ("plan", plan_nodes)):
                starts_for_group = [
                    row for row in events
                    if row.get("event") == "start" and row.get("node") in nodes
                ]
                ends_for_group = [
                    row for row in events
                    if row.get("event") == "end" and row.get("node") in nodes
                ]
                self.assertEqual(len(starts_for_group), len(nodes))
                self.assertEqual(len(ends_for_group), len(nodes))
                self.assertEqual(
                    {row["harness"] for row in starts_for_group}, {"codex", "claude"}
                )
                timings[group] = (
                    max(int(row["ns"]) for row in starts_for_group),
                    min(int(row["ns"]) for row in ends_for_group),
                    max(int(row["ns"]) for row in ends_for_group),
                )
                self.assertLess(timings[group][0], timings[group][1])
            self.assertLess(timings["frame"][2], min(
                int(row["ns"]) for row in events
                if row.get("event") == "start" and row.get("node") in plan_nodes
            ))

            # Lease/reservation release is asynchronous with wrapper exit, so a
            # bounded poll keeps the drained-governor assertion deterministic.
            deadline = time.monotonic() + 30
            while True:
                status = subprocess.run(
                    [
                        sys.executable, str(ROOT / "utilities" / "model-worker-governor.py"),
                        "--root", str(governor_root), "status",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
                governor = json.loads(status.stdout)
                if not governor["leases"] and not governor["reservations"]:
                    break
                if time.monotonic() >= deadline:
                    self.assertEqual(governor["leases"], {})
                    self.assertEqual(governor["reservations"], {})
                time.sleep(0.2)

            # Post-exit watchers detach from the wrapper and may still write
            # under the fixture; wait for every recorded watcher to exit so
            # TemporaryDirectory cleanup does not race a live writer.
            watcher_pids = set()
            for line in jobs.read_text(encoding="utf-8").splitlines():
                for token in line.replace("\t", ",").split(","):
                    if token.startswith(("orphan_watch_pid=", "reap_watch_pid=")):
                        value = token.split("=", 1)[1]
                        if value.isdigit():
                            watcher_pids.add(int(value))
            deadline = time.monotonic() + 30
            while watcher_pids and time.monotonic() < deadline:
                watcher_pids = {
                    pid for pid in watcher_pids if Path(f"/proc/{pid}").exists()
                }
                if watcher_pids:
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
