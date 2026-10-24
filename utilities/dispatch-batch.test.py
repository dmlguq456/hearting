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
os.environ.setdefault("HEARTING_CAPACITY_PROBE_CLI", os.devnull)  # never ask a real Claude login
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
import dispatch_lifecycle as BATCH_LIFECYCLE  # noqa: E402


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


class PairModelProposalTest(unittest.TestCase):
    def test_same_harness_pair_proposes_next_declared_model_without_changing_seals(self):
        nodes = [
            ({"id": "plan-a", "model_profile": "deep", "parallel_leg_index": 0}, "claude", "same", 1),
            ({"id": "plan-b", "model_profile": "balanced-deep", "parallel_leg_index": 1}, "claude", "same", 1),
        ]
        profiles, diagnostic = BATCH._pair_model_proposals(nodes)
        self.assertEqual(profiles, {"plan-b": "balanced"})
        self.assertEqual(diagnostic["reason"], "proposed")
        self.assertEqual([row[0]["model_profile"] for row in nodes], ["deep", "balanced-deep"])

    def test_batch_proposal_flows_through_shared_wrapper_to_realized_rotation_receipt(self):
        from model_config import resolve_config
        from model_profile import resolve_profile_demand, resolve_profile_values, selection_receipt
        import dispatch_wrapper_common as COMMON
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config, _ = resolve_config("claude", source_root=ROOT)
            nodes = [
                ({"id": "plan", "model_profile": "deep", "parallel_leg_index": 0}, "claude", "same", 1),
                ({"id": "plan-alternative", "model_profile": "balanced-deep", "parallel_leg_index": 1}, "claude", "same", 1),
            ]
            proposals, diagnostic = BATCH._pair_model_proposals(nodes)
            self.assertEqual(proposals, {"plan-alternative": "balanced"})
            route = {"route_id": "rt-batch-wrapper", "route_hash": "sha256:batch-wrapper",
                     "profile_selection_contract_version": 1,
                     "nodes": [{"id": name, "parallel_group": "plan", "model_profile": profile,
                                "profile_demand": None,
                                "profile_selection": resolve_profile_demand(None, explicit_profile=profile)}
                               for name, profile in (("plan", "deep"), ("plan-alternative", "balanced-deep"))]}
            route_path = base / "route.json"
            route_path.write_text(json.dumps(route))
            manifest = {"members": [
                {"route_node": "plan", "model_profile": "deep", "harness": "claude"},
                {"route_node": "plan-alternative", "model_profile": "balanced-deep", "harness": "claude"},
            ]}
            jobs = base / "jobs.log"
            jobs.write_text("")
            args = SimpleNamespace(
                model_profile="balanced-deep", pair_model_profile=proposals["plan-alternative"],
                route_file=str(route_path), owner_route_binding=None, route_node="plan-alternative",
                action="start", attempt_id="att-alternative", parent_attempt_id="att-parent",
                fallback_hop="same-harness-headless", fallback_ordinal=1, artifact_root=str(base),
                agent_home=str(ROOT), jobs=str(jobs), worker_type="stage", model=None,
                registered_worker=1, dispatch_depth=2, inherit_model_settings=False,
                model_role="deep maker", effort=None, capacity_retry=0,
            )
            with mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_RESERVATION_TOKEN": "token-fixture"}), \
                 mock.patch("dispatch_contract.replica_batch_expectation", return_value={}), \
                 mock.patch("dispatch_contract.resolve_model_governor_root", return_value=base / "governor"), \
                 mock.patch("dispatch_contract._governor_json", return_value={"state": "unclaimed", "batch_manifest": manifest}), \
                 mock.patch("route_authority.require_batch_reservation"), \
                 mock.patch("replica_batch_contract.verify_manifest", return_value=(manifest, "digest", {})), \
                 mock.patch("dispatch_capacity_evidence.usage_states", return_value={"claude": "ok"}):
                context = COMMON.prepare_pair_model_rotation(args, harness="claude")
                wrapper = importlib.util.spec_from_file_location(
                    "claude_pair_batch_wrapper", ROOT / "adapters/claude/bin/dispatch-headless.py")
                module = importlib.util.module_from_spec(wrapper)
                wrapper.loader.exec_module(module)
                rotated = module.resolve_model_settings(args)
                args.resolved_model_settings = rotated
                receipt = selection_receipt(args)
            baseline = resolve_profile_values("claude", config, "deep")
            results = [
                {"attempt_id": "att-anchor", "node": "plan", "launch_state": "started",
                 "model_profile": "deep", "model": baseline["model"], "model_source": "profile",
                 "model_diversity": "distinct"},
                {"attempt_id": "att-alternative", "node": "plan-alternative", "launch_state": "started",
                 "model_profile": rotated["profile"], "model": rotated["model"],
                 "model_source": receipt.get("model_source", "profile"),
                 "model_diversity": receipt["model_diversity"]},
            ]
            batch_receipt, _success = BATCH.batch_receipt(
                args=SimpleNamespace(parallel_group="plan"), lifecycle="detached", independence="persona",
                required_axes=["model"], realized_axes=[], degradation_reason="",
                legs=[{"attempt_id": row["attempt_id"]} for row in results], results=results, admitted=2,
                selection_diagnostics={"model_diversity_proposal": diagnostic})
        self.assertEqual(context["status"], "rotated")
        self.assertEqual(args.model_profile, "balanced-deep")
        self.assertEqual(rotated["profile"], "balanced")
        self.assertNotEqual(rotated["model"], baseline["model"])
        realized = batch_receipt["selection_diagnostics"]["model_diversity_realized"]["plan-alternative"]
        self.assertEqual(realized["model_diversity"], "rotated")
        self.assertEqual(realized["model_source"], "profile+diversity")

    def test_distinct_harness_pair_is_outside_model_rotation(self):
        nodes = [
            ({"id": "plan-a", "model_profile": "deep"}, "claude", "same", 1),
            ({"id": "plan-b", "model_profile": "deep"}, "codex", "same", 1),
        ]
        profiles, diagnostic = BATCH._pair_model_proposals(nodes)
        self.assertEqual(profiles, {})
        self.assertEqual(diagnostic["reason"], "distinct-harness")

    def test_concrete_model_pin_is_preserved_but_profile_only_selection_can_rotate(self):
        from model_config import resolve_config
        from model_profile import resolve_profile_values
        config, _ = resolve_config("claude", source_root=ROOT)
        pinned_model = resolve_profile_values("claude", config, "balanced-deep")["model"]
        nodes = [
            ({"id": "plan-a", "model_profile": "deep", "parallel_leg_index": 0}, "claude", "same", 1),
            ({"id": "plan-b", "model_profile": "balanced-deep", "parallel_leg_index": 1}, "claude", "same", 1),
        ]
        profiles, diagnostic = BATCH._pair_model_proposals(nodes, route={
            "selection_pins": {"contract_version": 1,
                               "worker": {"harness": "claude", "model": pinned_model, "effort": None}}
        })
        self.assertEqual(profiles, {})
        self.assertEqual(diagnostic["reason"], "explicit-model")
        profiles, _ = BATCH._pair_model_proposals(nodes, route={
            "explicit_profiles": {"plan-b": "balanced-deep"}
        })
        self.assertEqual(profiles, {"plan-b": "balanced"})

    def test_limited_target_keeps_both_sealed_profiles(self):
        nodes = [
            ({"id": "plan-a", "model_profile": "deep", "parallel_leg_index": 0}, "claude", "same", 1),
            ({"id": "plan-b", "model_profile": "balanced-deep", "parallel_leg_index": 1}, "claude", "same", 1),
        ]
        with mock.patch("dispatch_capacity_evidence.usage_states",
                        return_value={"claude": "limited(model-quota)"}):
            profiles, diagnostic = BATCH._pair_model_proposals(nodes, jobs=Path("/tmp/jobs.log"))
        self.assertEqual(profiles, {})
        self.assertEqual(diagnostic["reason"], "target-limited")

    def test_no_different_declared_model_records_unavailable(self):
        from model_config import resolve_config
        config, receipt = resolve_config("claude", source_root=ROOT)
        config = dict(config)
        for profile in ("balanced-deep", "balanced", "light"):
            config["CFG_MODEL_PROFILE_" + profile.upper().replace("-", "_")] = "deep:medium"
        nodes = [
            ({"id": "plan-a", "model_profile": "deep", "parallel_leg_index": 0}, "claude", "same", 1),
            ({"id": "plan-b", "model_profile": "balanced-deep", "parallel_leg_index": 1}, "claude", "same", 1),
        ]
        with mock.patch("model_config.resolve_config", return_value=(config, receipt)):
            profiles, diagnostic = BATCH._pair_model_proposals(nodes)
        self.assertEqual(profiles, {})
        self.assertEqual(diagnostic["reason"], "unavailable")

    def _receipt(self, models):
        args = SimpleNamespace(parallel_group="plan")
        legs = [{"attempt_id": "att-a"}, {"attempt_id": "att-b"}]
        results = [{"attempt_id": "att-a", "launch_state": "started", "registered": "1",
                    "started": "1", "child_spawned": "1", "model": models[0], "node": "a"},
                   {"attempt_id": "att-b", "launch_state": "started", "registered": "1",
                    "started": "1", "child_spawned": "1", "model": models[1], "node": "b"}]
        axes = []
        receipt, _ = BATCH.batch_receipt(
            args=args, lifecycle="detached", independence="persona", required_axes=["model"],
            realized_axes=axes, degradation_reason="", legs=legs, results=results, admitted=2)
        return receipt

    def test_realized_model_axis_uses_actual_receipt_models(self):
        receipt = self._receipt(("opus", "sonnet"))
        self.assertIn("model", receipt["realized_independence_axes"])

    def test_proposal_diagnostics_are_separate_from_realized_row_fields(self):
        args = SimpleNamespace(parallel_group="plan")
        legs = [{"attempt_id": "att-a"}, {"attempt_id": "att-b"}]
        results = [
            {"attempt_id": "att-a", "launch_state": "started", "model_profile": "deep",
             "model": "opus", "model_source": "profile", "model_diversity": "distinct",
             "model_diversity_from": "deep"},
            {"attempt_id": "att-b", "launch_state": "started", "model_profile": "balanced",
             "model": "sonnet", "model_source": "profile+diversity", "model_diversity": "rotated",
             "model_diversity_from": "balanced-deep/opus"},
        ]
        diagnostics = {"model_diversity_proposal": {"reason": "proposed", "profile": "balanced"}}
        receipt, _ = BATCH.batch_receipt(
            args=args, lifecycle="detached", independence="persona", required_axes=["model"],
            realized_axes=[], degradation_reason="", legs=legs, results=results, admitted=2,
            selection_diagnostics=diagnostics)
        self.assertIn("model_diversity_proposal", receipt["selection_diagnostics"])
        self.assertNotIn("model_diversity", receipt["selection_diagnostics"])
        realized = receipt["selection_diagnostics"]["model_diversity_realized"]
        rotated = next(row for row in realized.values() if row["model"] == "sonnet")
        self.assertEqual(rotated["model_diversity"], "rotated")
        self.assertEqual(rotated["model_profile"], "balanced")
        self.assertEqual(rotated["model_source"], "profile+diversity")

    def test_effort_or_variant_does_not_create_model_axis(self):
        receipt = self._receipt(("opus", "opus"))
        self.assertNotIn("model", receipt["realized_independence_axes"])


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
        # SD-OPEN-49 / H8: every state-root derivation that does not take the
        # explicit `--jobs` reads `AGENT_DISPATCH_JOBS`, and unset means the
        # per-user default -- the live root. Pin it to the fixture registry.
        state_env = mock.patch.dict(os.environ, {"AGENT_DISPATCH_JOBS": str(self.jobs)})
        state_env.start()
        self.addCleanup(state_env.stop)
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

    def legs(self, assignments=None, prompt=None, parent_attempt="att-parent-fixture"):
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
                    parent_attempt,
                    adapter,
                    ordinal,
                ),
                "assignment_sha256": "sha256:" + __import__("hashlib").sha256(
                    (BATCH.DEFAULT_PROMPT if prompt is None else prompt).encode("utf-8")
                ).hexdigest(),
                "independence": "cross-harness",
                "model_profile": str(node["model_profile"]),
                "perspective": str(node["perspective"]),
                "parallel_leg_index": int(node["parallel_leg_index"]),
            })
        return legs

    def write_existing(
        self, leg, *, status="open", note="", claimed="1", append=True,
        live_identity=True, parent_attempt="att-parent-fixture",
        all_legs=None,
    ):
        all_legs = all_legs or self.legs()
        _manifest, manifest_digest, leg_digests = BATCH.build_manifest(
            replica_group="plan",
            route_id=self.route["route_id"],
            parent_attempt_id=parent_attempt,
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
            f"parent_attempt_id={parent_attempt},launch_authority=conductor,"
            f"fallback_ordinal={leg['ordinal']},attempt_id={leg['attempt_id']},"
            f"launch_claimed={claimed},launch_fence=registry-v1,parallel_group=plan,replica_group=plan,"
            "reservation_kind=parallel-batch,batch_declared_size=2,"
            "batch_admission_count=2,"
            f"batch_group=plan,batch_route_id={self.route['route_id']},"
            f"batch_parent_attempt_id={parent_attempt},"
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

    def partial_continuation(self, source_legs, parent_attempt="att-parent-fixture"):
        legs = [dict(leg, leg_class="peer") for leg in source_legs]
        manifest, manifest_digest, leg_digests = BATCH.build_manifest(
            parallel_group="plan",
            route_id=self.route["route_id"],
            parent_attempt_id=parent_attempt,
            independence="cross-harness",
            members=BATCH.manifest_members(legs),
            required_independence_axes=[
                "cross-harness", "model-profile", "perspective"
            ],
            realized_independence_axes=[
                "cross-harness", "model-profile", "perspective"
            ],
        )
        peer, gap = legs
        realized = [{
            "node_id": peer["node"],
            "terminal_attempt_id": peer["attempt_id"],
            "marker_path": str(self.base / "peer-marker.json"),
            "marker_digest": "sha256:" + "1" * 64,
            "verdict": "PASS",
            "quiescence_proof_digest": "sha256:" + "2" * 64,
            "output_evidence_digest": "sha256:" + "3" * 64,
            "contract_hash": "sha256:" + "4" * 64,
        }]
        peer_digest = BATCH.record_digest(realized)
        identity = BATCH.record_digest({
            "source_route_id": self.route["route_id"],
            "source_route_hash": self.route["route_hash"],
            "source_group_id": "plan",
            "failed_source_attempt_id": gap["attempt_id"],
            "gap_leg_id": gap["node"],
            "reused_peer_set_proof_digest": peer_digest,
        })
        partial = {
            "contract_version": 1,
            "source_group_id": "plan",
            "source_batch_manifest_digest": manifest_digest,
            "leg_manifest_digests": {
                leg["node"]: leg_digests[leg["attempt_id"]] for leg in legs
            },
            "original_group_cardinality": 2,
            "join_policy": "all",
            "failed_source_attempt_id": gap["attempt_id"],
            "gap_leg_id": gap["node"],
            "realized_peer_set": realized,
            "reused_peer_set_proof_digest": peer_digest,
            "replacement_leg_identity": identity,
            "replacement_attempt_id": "att-" + identity.split(":", 1)[1][:48],
        }
        continuation = {
            "continuation_contract_version": 1,
            "continuation_id": "cont-at5",
            "source_route_id": self.route["route_id"],
            "source_route_hash": self.route["route_hash"],
            "partial_group_continuation": partial,
        }
        return manifest, continuation, partial

    def seal_original_input(self, parent_attempt="att-parent-fixture"):
        from dispatch_replacement_batch import seal_launch_input
        manifest, _continuation, _partial = self.partial_continuation(
            self.legs(parent_attempt=parent_attempt), parent_attempt=parent_attempt)
        _checked, digest, _legs = BATCH.verify_manifest(manifest)
        args = SimpleNamespace(continuation=None, route=self.route_path, parallel_group="plan",
            slug_prefix="fixture", parent="owner", prompt_text=BATCH.DEFAULT_PROMPT,
            qa=None, log_dir=None, allow_degraded_independence=False)
        seal_launch_input(self.jobs, args, self.route, manifest, digest)

    def test_sd160_partial_legacy_rows_without_whole_manifest_refuse(self):
        self.write_existing(self.legs()[0])
        before = self.jobs.read_bytes()
        with self.assertRaises(BATCH.BatchError) as error:
            BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                                   "plan", "att-parent-fixture", BATCH.DEFAULT_PROMPT)
        self.assertEqual(error.exception.reason, "batch-prior-manifest-proof-missing")
        self.assertEqual(self.jobs.read_bytes(), before)

    def test_unfinished_input_discovers_zero_rows_and_register_only_exact_members(self):
        self.jobs.touch()
        self.seal_original_input()
        legs = self.legs()
        admissions = {leg['node']: SimpleNamespace(
            budget=SimpleNamespace(next_round=1), current_attempt_ids=frozenset())
            for leg in legs}
        def read():
            with mock.patch.dict(os.environ, {'AGENT_DISPATCH_SELF_SLUG': 'owner'}):
                return BATCH.unfinished_batch_input(self.jobs, self.route,
                    self.route['nodes'], 'plan', 'att-parent-fixture', BATCH.DEFAULT_PROMPT,
                    admissions, route_file=self.route_path)
        payload = read()
        self.assertIsNotNone(payload)
        before = self.jobs.read_bytes()
        for admission in admissions.values():
            admission.budget.next_round = 2
        self.assertIsNone(read(), 'older generation cannot bind a new admission')
        self.assertEqual(self.jobs.read_bytes(), before)
        for admission in admissions.values():
            admission.budget.next_round = 1
        for leg in legs:
            self.write_existing(leg, claimed='0', live_identity=False)
            admissions[leg['node']].current_attempt_ids = frozenset((leg['attempt_id'],))
        rows = self.jobs.read_bytes()
        self.assertEqual(read(), payload)
        self.assertEqual(self.jobs.read_bytes(), rows)
        from recovery_history import batch_input_path
        path = batch_input_path(self.jobs, payload['manifest_digest'])
        for field, value in (('route', str(self.base / 'foreign.json')), ('parent', 'foreign-owner')):
            changed = dict(payload, options=dict(payload['options'], **{field: value}))
            path.write_text(json.dumps(changed))
            with self.assertRaises(BATCH.BatchError) as error:
                read()
            self.assertEqual(error.exception.reason, 'replacement-batch-source-drift')
            path.write_text(json.dumps(payload))
        for field in ('options', 'manifest'):
            path.write_text(json.dumps(dict(payload, **{field: None})))
            with self.assertRaises(BATCH.BatchError):
                read()
            path.write_text(json.dumps(payload))
        changed = json.loads(json.dumps(payload))
        changed['manifest']['members'][0]['route_node'] = 'foreign-node'
        path.write_text(json.dumps(changed))
        with self.assertRaises(BATCH.BatchError) as error:
            read()
        self.assertEqual(error.exception.reason, 'replacement-batch-source-drift')
        path.write_text(json.dumps(payload))

    def test_capacity_ratios_choose_distinct_healthy_personas(self):
        for strategy in ("balanced", "capacity-aware"):
            for headroom in (71, 77):
                with self.subTest(strategy=strategy, headroom=headroom):
                    route = self._depth_affinity_route(strategy, same_band=True, affinity=False)
                    for node in route["nodes"]:
                        node["harness_affinity"] = "claude"
                    before = json.dumps(route, sort_keys=True)
                    rows, independence, diagnostics = self._assign(route, {
                        "claude": 8, "codex": headroom, "opencode": 1})
                    self.assertEqual([row[1] for row in rows], ["codex", "codex"])
                    self.assertEqual(independence, "persona")
                    self.assertEqual(diagnostics["degradation_cause"], "")
                    self.assertEqual(json.dumps(route, sort_keys=True), before)

    def test_sd160_persona_manifest_preserves_declared_axes_and_exact_hash(self):
        members = BATCH.manifest_members([dict(leg, leg_class="peer") for leg in self.legs()])
        for member in members:
            member["harness"] = "codex"
        kwargs = dict(parallel_group="plan", route_id=self.route["route_id"],
                      parent_attempt_id="att-parent-fixture", members=members,
                      required_independence_axes=["cross-harness", "model-profile", "perspective"],
                      realized_independence_axes=["model-profile", "perspective"])
        manifest, digest, legs = BATCH.build_manifest(**kwargs, independence="persona")
        self.assertEqual(BATCH.verify_manifest(manifest), (manifest, digest, legs))
        self.assertEqual(BATCH.effective_independence_axes(manifest["required_independence_axes"]),
                         ["model-profile", "perspective"])
        self.assertIn("cross-harness", manifest["required_independence_axes"])
        self.assertEqual(manifest["degradation_reason"], "")
        # The same persona fails even if the two runtimes differ.
        for harness in ("codex", "claude"):
            members[1]["harness"] = harness
            members[1]["perspective"] = members[0]["perspective"]
            kwargs["realized_independence_axes"] = ["model-profile"] + (["cross-harness"] if harness == "claude" else [])
            with self.assertRaises(BATCH.ReplicaBatchContractError):
                BATCH.build_manifest(**kwargs, independence="persona")

    def test_sd160_legacy_source_hash_rebuild_preserves_original_encoding(self):
        members = BATCH.manifest_members([dict(leg, leg_class="peer") for leg in self.legs()])
        for encoding in ("cross-harness", "degraded-same-harness", "persona"):
            if encoding != "cross-harness":
                members[1]["harness"] = members[0]["harness"]
            axes = ["model-profile", "perspective"]
            if encoding == "cross-harness":
                axes.insert(0, "cross-harness")
            kwargs = dict(parallel_group="plan", route_id=self.route["route_id"],
                          parent_attempt_id="att-parent-fixture", members=members,
                          required_independence_axes=["cross-harness", "model-profile", "perspective"],
                          realized_independence_axes=axes)
            reason = "cross-harness-unavailable-user-allowed" if encoding == "degraded-same-harness" else ""
            original = BATCH.build_manifest(**kwargs, independence=encoding, degradation_reason=reason)
            self.assertEqual(BATCH.source_manifest_for_digest(kwargs, original[1]), original)
            with self.assertRaises(BATCH.ReplicaBatchContractError):
                BATCH.source_manifest_for_digest(kwargs, "sha256:" + "f" * 64)

    def test_sd160_restart_reuses_verified_sealed_input_without_reallocation(self):
        from dispatch_replacement_batch import seal_launch_input
        legs = self.legs()
        for leg in legs:
            self.write_existing(leg)
        manifest, _continuation, _partial = self.partial_continuation(legs)
        _checked, digest, _leg_digests = BATCH.verify_manifest(manifest)
        args = SimpleNamespace(continuation=None, route=self.route_path, parallel_group="plan",
            slug_prefix="fixture", parent="owner", prompt_text=BATCH.DEFAULT_PROMPT,
            qa=None, log_dir=None, allow_degraded_independence=False)
        seal_launch_input(self.jobs, args, self.route, manifest, digest)
        before = self.jobs.read_bytes()
        payload = BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                                         "plan", "att-parent-fixture", BATCH.DEFAULT_PROMPT)
        self.assertEqual(payload["manifest"], manifest)
        with mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect):
            assignments = BATCH.prior_batch_assignments(payload, self.route, self.route["nodes"], {})
        self.assertEqual([row[1] for row in assignments], [leg["adapter"] for leg in legs])
        self.assertEqual(self.jobs.read_bytes(), before)
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
            rank = stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", side_effect=AssertionError("must reuse sealed placement")))
            reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            spawn = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner", "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}))
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(self.argv())
        self.assertEqual(rc, 0, output.getvalue())
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["state"], "idempotent-existing")
        self.assertEqual(receipt["independence"], "persona")
        self.assertEqual(receipt["effective_required_independence_axes"], ["model-profile", "perspective"])
        self.assertEqual({leg["independence"] for leg in receipt["legs"]}, {"cross-harness"})
        self.assertEqual(self.jobs.read_bytes(), before)
        rank.assert_not_called(); reserve.assert_not_called(); spawn.assert_not_called()
        with self.assertRaises(BATCH.BatchError):
            BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                                   "plan", "att-parent-fixture", "different task")
        self.assertIsNone(BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                                                "plan", "different-parent", BATCH.DEFAULT_PROMPT))

    def test_public_group_restart_keeps_cancelled_manifest_bindings_stable(self):
        from dispatch_replacement_batch import seal_launch_input
        from recovery_history import batch_input_path

        parent_a = "att-parent-fixture"
        parent_b = "att-successor-fixture"
        legs_a = self.legs(parent_attempt=parent_a)
        for leg in legs_a:
            self.write_existing(leg, status="done", note="cancelled-by-parent",
                                claimed="0", live_identity=False, parent_attempt=parent_a)
        rewritten = []
        for line in self.jobs.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            fields[5] += ",launch_outcome=never-launched,failure_class=cancelled"
            rewritten.append("\t".join(fields))
        self.jobs.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        manifest_a, _continuation, _partial = self.partial_continuation(legs_a, parent_a)
        _checked, digest_a, _leg_digests = BATCH.verify_manifest(manifest_a)
        args = SimpleNamespace(continuation=None, route=self.route_path, parallel_group="plan",
            slug_prefix="fixture", parent="owner", prompt_text=BATCH.DEFAULT_PROMPT,
            qa=None, log_dir=None, allow_degraded_independence=False)
        seal_launch_input(self.jobs, args, self.route, manifest_a, digest_a)
        generation_a_rows = self.jobs.read_bytes()

        def public_start(parent_attempt):
            output = io.StringIO()
            created = []

            class FakeProcess:
                returncode = 0
                pid = 10001

                def __init__(self, command, **kwargs):
                    self.command = command
                    created.append(self)

                def communicate(self):
                    return success_receipt(self.command), ""

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
                stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
                stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry",
                    return_value=SimpleNamespace(path=self.jobs)))
                stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
                stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
                stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output",
                    return_value=str(self.base)))
                stack.enter_context(mock.patch.object(BATCH, "assign_harnesses",
                    return_value=(self.common_patches()[1], "cross-harness", {
                        "families_considered": [], "usable_families": [],
                        "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
                stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple",
                    side_effect=resolve_side_effect))
                stack.enter_context(mock.patch.object(BATCH, "reserve_batch",
                    return_value=["a" * 32, "b" * 32]))
                stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
                stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen",
                    side_effect=FakeProcess))
                stack.enter_context(mock.patch.dict(os.environ, {
                    "AGENT_DISPATCH_SELF_SLUG": "owner", "AGENT_DISPATCH_ATTEMPT_ID": parent_attempt,
                    "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                    "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}))
                with contextlib.redirect_stdout(output):
                    result = BATCH.main(self.argv())
            return result, json.loads(output.getvalue()), created

        # This fixture uses ordinary replica nodes without a review_round_cap.
        first_code, first, first_processes = public_start(parent_b)
        self.assertEqual(first_code, 0, json.dumps(first))
        self.assertEqual(first["state"], "launched")
        self.assertEqual(first["admitted"], 2)
        self.assertEqual(first["registered"], 1)
        self.assertEqual(first["started"], 1)
        self.assertEqual(first["child_spawned"], 1)
        self.assertTrue(all(leg["registered"] == "1" for leg in first["legs"]))
        self.assertTrue(all(leg["started"] == "1" for leg in first["legs"]))
        self.assertTrue(all(leg["child_spawned"] == "1" for leg in first["legs"]))
        self.assertEqual(len(first_processes), len(legs_a))
        for refusal in ("batch-prior-binding-", "replacement-batch-source-drift",
                        "review-verdictless-bound", "blocked-live"):
            self.assertNotIn(refusal, json.dumps(first))

        legs_b = self.legs(parent_attempt=parent_b)
        for leg in legs_b:
            self.write_existing(leg, status="running", note="", claimed="1",
                                live_identity=True, parent_attempt=parent_b, all_legs=legs_b)
        generation_a_lines = [line for line in generation_a_rows.decode().splitlines()
            if f"parent_attempt_id={parent_a}" in line]
        current_a_lines = [line for line in self.jobs.read_text().splitlines()
            if f"parent_attempt_id={parent_a}" in line]
        self.assertEqual(current_a_lines, generation_a_lines)
        self.assertEqual(len(current_a_lines), len(legs_a))
        for line in current_a_lines:
            self.assertIn("batch_manifest_sha256=", line)
            self.assertIn("batch_leg_sha256=", line)
            self.assertIn("failure_class=cancelled", line)

        manifest_b, _continuation, _partial = self.partial_continuation(legs_b, parent_b)
        _checked, digest_b, _leg_digests = BATCH.verify_manifest(manifest_b)
        seal_launch_input(self.jobs, args, self.route, manifest_b, digest_b)
        input_path = batch_input_path(self.jobs, digest_b)
        original = input_path.read_text(encoding="utf-8")
        tampered = json.loads(original)
        tampered["manifest"]["members"][0]["route_node"] = "foreign-node"
        input_path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(BATCH.BatchError) as error:
            BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                "plan", parent_b, BATCH.DEFAULT_PROMPT)
        self.assertIn(error.exception.reason,
                      {"replacement-batch-source-drift", "batch-prior-binding-invalid"})
        input_path.write_text(original, encoding="utf-8")

        second_code, second, second_processes = public_start(parent_b)
        self.assertEqual(second_code, 0, json.dumps(second))
        self.assertEqual(second["state"], "idempotent-existing")
        self.assertEqual(second["existing"], 2)
        self.assertEqual(second["newly_started"], 0)
        self.assertEqual(second_processes, [])
        self.assertEqual([leg["attempt_id"] for leg in legs_b],
                         [leg["attempt_id"] for leg in self.legs(parent_attempt=parent_b)])
        self.assertEqual(len([line for line in self.jobs.read_text().splitlines()
            if f"parent_attempt_id={parent_b}" in line]), len(legs_b))
        attempt_rows = [line for line in self.jobs.read_text().splitlines()
                        if "batch_attempt_id=" in line]
        self.assertEqual(len(attempt_rows), len(legs_a) + len(legs_b))

    def test_load_route_rejects_non_object_json_before_verification(self):
        self.route_path.write_text("[]", encoding="utf-8")
        with mock.patch.object(BATCH.subprocess, "run") as verify, \
             self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.load_route(self.route_path)
        self.assertEqual(ctx.exception.reason, "route-record-invalid")
        verify.assert_not_called()

    def test_at5_plain_single_group_start_remains_batch_required(self):
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        output = io.StringIO()
        argv = [
            "dispatch-node.py",
            "--route", str(self.route_path),
            "--node", "plan",
            "--adapter", "codex",
            "--action", "start",
            "--slug", "single-gap",
            "--parent", "owner",
            "--jobs", str(self.jobs),
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            BATCH.DISPATCH_NODE.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ), mock.patch.dict(
            os.environ,
            {BATCH.GOVERNOR_RESERVATION_ENV: ""},
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            BATCH.DISPATCH_NODE.main()
        self.assertEqual(caught.exception.code, 65)
        self.assertIn("reason=parallel-group-batch-required", output.getvalue())
        self.assertFalse(self.jobs.exists())

    def test_at5_plain_full_batch_identity_conflict_still_starts_nothing(self):
        first, _second = self.legs()
        self.write_existing(first)
        raw = self.jobs.read_text(encoding="utf-8")
        self.jobs.write_text(
            raw.replace("batch_route_node=plan,", "batch_route_node=foreign,"),
            encoding="utf-8",
        )
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
        self.assertEqual(rc, 65)
        self.assertEqual(
            json.loads(output.getvalue())["reason"],
            "batch-prior-manifest-proof-missing",
        )
        reserve.assert_not_called()
        popen.assert_not_called()

    def test_route_state_refusal_returns_next_action(self):
        """A-SD154-2 / defect #2 (13.59.3 rule 6, B-2): a completion_marker_gate
        refusal whose reason is in `ROUTE_STATE_REFUSAL_REASONS` -- this
        fixture's `replica_node` nodes depend on `frame`/`frame-replica`,
        which never published a marker anywhere, so the REAL (unmocked) gate
        raises `completion-marker-missing` -- stops with exit 65,
        `child_spawned=0`, and a carried `next_action`, never a `selected_hop`
        descent toward an unregistered fallback the way a generic
        `runtime-unavailable` failure would."""
        self.route["dispatch_contract_version"] = 3
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        stack, assignments = self.common_patches()
        output = io.StringIO()
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            # completion_marker_gate itself is deliberately NOT mocked here --
            # every other full-flow test in this class stubs it out, which is
            # exactly the coverage gap this test closes (the real gate's
            # ROUTE_STATE_REFUSAL_REASONS handling was previously only proven
            # by stub-returned BatchError objects, never a real refusal).
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
        self.assertEqual(rc, 65)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["reason"], "completion-marker-missing")
        self.assertIn("frame", receipt["detail"])
        self.assertEqual(
            {key: receipt[key] for key in ("admitted", "spawned", "registered", "started", "child_spawned")},
            {"admitted": "0", "spawned": "0", "registered": "0", "started": "0", "child_spawned": "0"},
        )
        self.assertIn("next_action", receipt)
        self.assertNotIn("selected_hop", receipt)
        self.assertNotIn("runtime-unavailable", json.dumps(receipt))
        reserve.assert_not_called()
        popen.assert_not_called()

    def test_at5_partial_continuation_reuses_peers_and_claims_only_gap(self):
        self._partial_continuation_launch()

    def test_partial_continuation_uses_sealed_assignment_with_new_gap_brief(self):
        self._partial_continuation_launch(
            retry_prompt="Independent research: finish only the failed alternative.\n",
            sealed=True,
        )

    def test_partial_continuation_preserves_newline_lost_by_shell_substitution(self):
        original = BATCH.DEFAULT_PROMPT + "\n"
        with mock.patch.object(BATCH, "DEFAULT_PROMPT", original):
            self._partial_continuation_launch(retry_prompt=original.rstrip("\n"), sealed=True)

    def test_partial_continuation_legacy_rows_keep_assignment_with_new_brief(self):
        self._partial_continuation_launch(retry_prompt="Recover the failed leg only.")

    def test_partial_continuation_legacy_custom_assignment_keeps_matching_prompt(self):
        self._partial_continuation_launch(
            original_prompt="The original custom assignment.",
            retry_prompt="The original custom assignment.",
        )

    def test_partial_continuation_new_owner_keeps_source_parent_and_replacement(self):
        self._partial_continuation_launch(
            retry_prompt="Continue the authorized gap.", sealed=True,
            parent_attempt="att-corrected-owner",
        )

    def _partial_continuation_launch(self, *, retry_prompt=None, sealed=False,
                                    parent_attempt="att-parent-fixture", original_prompt=None):
        source_legs = self.legs(prompt=original_prompt)
        _source_manifest, continuation, partial = self.partial_continuation(source_legs)
        peer, gap = source_legs
        self.write_existing(peer, status="done", note="completed-marker", all_legs=source_legs)
        self.write_existing(
            gap, status="done", note="cancelled-receipt-unavailable", all_legs=source_legs
        )
        if sealed:
            self.seal_original_input()
        self.jobs.write_text(
            self.jobs.read_text(encoding="utf-8").replace(
                f"note=cancelled-receipt-unavailable\n",
                "note=cancelled-receipt-unavailable,recovery_id=rec-at5,"
                "retry_ordinal=1,retry_attempt_id=att-retry-verbatim\n",
            ),
            encoding="utf-8",
        )
        continuation_path = self.base / "continuation.json"
        argv = self.argv() + ["--continuation", str(continuation_path)]
        if retry_prompt is not None:
            argv += ["--prompt-text", retry_prompt]
        output = io.StringIO()
        created = []

        class FakeProcess:
            returncode = 0
            pid = 10001

            def __init__(self, command, **kwargs):
                self.command = command
                self.env = kwargs.get("env", {})
                created.append(self)

            def communicate(self):
                return success_receipt(self.command), ""

        stack, original_assignments = self.common_patches()
        # A fresh allocator would now reverse both harnesses. Official partial
        # recovery must recover the exact source tuples from the registry rows.
        assignments = [
            (
                self.route["nodes"][0],
                "claude",
                "cross-harness-headless",
                2,
            ),
            (
                self.route["nodes"][1],
                "codex",
                "same-harness-headless",
                1,
            ),
        ]
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH,
                "load_partial_continuation",
                return_value=(continuation, partial),
            ))
            stack.enter_context(mock.patch.object(
                BATCH,
                "revalidate_partial_peers",
                return_value=partial["realized_peer_set"],
            ))
            stack.enter_context(mock.patch.object(
                BATCH.DISPATCH_NODE,
                "resolve_checked_tuple",
                side_effect=resolve_side_effect,
            ))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(assignments, "cross-harness", {"families_considered": [], "usable_families": [], "family_exclusions": {}, "capacity": {}, "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            gate = stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            reserve = stack.enter_context(mock.patch.object(
                BATCH, "reserve_batch", return_value=["b" * 32]
            ))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            popen = stack.enter_context(mock.patch.object(
                BATCH.subprocess, "Popen", side_effect=FakeProcess
            ))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner",
                "AGENT_DISPATCH_ATTEMPT_ID": parent_attempt,
                "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
            }))
            with mock.patch("dispatch_capacity_evidence.active_limits", return_value={
                    "claude": {"reset_epoch": 9999999999}}), contextlib.redirect_stdout(output):
                blocked_rc = BATCH.main(argv)
            self.assertEqual(blocked_rc, 75, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["reason"], "batch-pending-hard-limit")
            reserve.assert_not_called(); popen.assert_not_called()
            gate.reset_mock()
            output.seek(0); output.truncate(0)
            with contextlib.redirect_stdout(output):
                rc = BATCH.main(argv)
        self.assertEqual(rc, 0)
        self.assertEqual(gate.call_count, 1)
        self.assertEqual(reserve.call_count, 1)
        pending = reserve.call_args.args[2]
        self.assertEqual(
            [
                (leg["node"], leg["adapter"], leg["hop"], leg["ordinal"],
                 leg["attempt_id"])
                for leg in pending
            ],
            [(
                partial["gap_leg_id"],
                original_assignments[1][1],
                original_assignments[1][2],
                original_assignments[1][3],
                "att-retry-verbatim",
            )],
        )
        self.assertEqual(
            reserve.call_args.kwargs["source_manifest"],
            _source_manifest,
        )
        self.assertEqual(
            reserve.call_args.kwargs["continuation"],
            continuation,
        )
        self.assertTrue(
            reserve.call_args.kwargs["replacement_seal"]["retry_claim_reused"]
        )
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(len(created), 1)
        command = created[0].command
        self.assertEqual(command[command.index("--prompt-text") + 1],
                         BATCH.DEFAULT_PROMPT if original_prompt is None else original_prompt)
        self.assertEqual(created[0].env["AGENT_DISPATCH_RETRY_BRIEF"],
                         retry_prompt if retry_prompt is not None else BATCH.DEFAULT_PROMPT)
        self.assertEqual(command[command.index("--parent-attempt-id") + 1], parent_attempt)
        self.assertEqual(
            command[command.index("--attempt-id") + 1],
            "att-retry-verbatim",
        )
        receipt = json.loads(output.getvalue())
        self.assertEqual((receipt["newly_started"], receipt["existing"]), (1, 1))
        self.assertEqual(receipt["replacement_attempt_id"], "att-retry-verbatim")
        self.assertEqual(receipt["original_group_cardinality"], 2)
        self.assertEqual(receipt["reused_peer_count"], 1)
        self.assertEqual(
            receipt["legs"][0]["reason"], "reused-successful-peer"
        )
        self.assertEqual(len(self.jobs.read_text(encoding="utf-8").splitlines()), 2)

    def test_partial_source_rejects_tampered_launch_input_and_row_bindings(self):
        from dispatch_replacement_batch import _input_path
        legs = self.legs()
        _manifest, _continuation, partial = self.partial_continuation(legs)
        self.write_existing(legs[0], status="done", note="completed-marker")
        self.write_existing(legs[1], status="done", note="dead-worker-blocked")
        self.seal_original_input()
        path = _input_path(self.jobs, partial["source_batch_manifest_digest"])
        original_input = path.read_text()
        original_rows = self.jobs.read_text()
        for mutation in ("manifest", "prompt", "leg-digest", "profile", "parent", "duplicate"):
            with self.subTest(mutation=mutation):
                path.write_text(original_input)
                self.jobs.write_text(original_rows)
                payload = json.loads(original_input)
                if mutation == "manifest":
                    payload["manifest"]["members"][0]["assignment_sha256"] = "sha256:" + "f" * 64
                    path.write_text(json.dumps(payload))
                elif mutation == "prompt":
                    payload["options"]["prompt_text"] += "tampered"
                    path.write_text(json.dumps(payload))
                else:
                    rows = original_rows
                    if mutation == "leg-digest":
                        rows = rows.replace("batch_leg_sha256=sha256:", "batch_leg_sha256=sha256:f")
                    elif mutation == "profile":
                        rows = rows.replace(f"batch_model_profile={legs[0]['model_profile']}",
                                            "batch_model_profile=mini")
                    elif mutation == "parent":
                        rows = rows.replace("batch_parent_attempt_id=att-parent-fixture",
                                            "batch_parent_attempt_id=att-foreign")
                    else:
                        rows += original_rows.splitlines()[0] + "\n"
                    self.jobs.write_text(rows)
                with self.assertRaises(BATCH.BatchError):
                    BATCH.partial_batch_input(self.jobs, self.route, self.route["nodes"], partial)

    def test_blocked_recovery_claim_can_never_reuse_retry_attempt(self):
        metadata = {
            "recovery_id": "rec-blocked",
            "retry_ordinal": "1",
            "retry_attempt_id": "att-retry-must-not-start",
            "start_permitted": "0",
            "note": "receipt-unavailable-retry-exhausted",
            "failure_class": "blocked",
        }
        with self.assertRaises(BATCH.BatchError) as caught:
            BATCH._recovery_replacement_attempt(metadata)
        self.assertEqual(caught.exception.reason, "receipt-unavailable-retry-exhausted")
        self.assertEqual(
            BATCH._recovery_replacement_attempt({
                "recovery_id": "rec-live",
                "retry_ordinal": "1",
                "retry_attempt_id": "att-retry-live",
            }),
            "att-retry-live",
        )

    def test_launch_runtime_mismatch_is_zero_spawn_and_does_not_create_registry(self):
        output = io.StringIO()
        with mock.patch.object(
            BATCH, "load_route",
            side_effect=BATCH.BatchError(
                "launch-runtime-root-mismatch", "sealed runtime differs"
            ),
        ), contextlib.redirect_stdout(output):
            result = BATCH.main(self.argv("start"))
        self.assertEqual(result, 65)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["reason"], "launch-runtime-root-mismatch")
        self.assertEqual(
            {key: receipt[key] for key in (
                "admitted", "spawned", "registered", "started", "child_spawned"
            )},
            {"admitted": "0", "spawned": "0", "registered": "0",
             "started": "0", "child_spawned": "0"},
        )
        self.assertFalse(self.jobs.exists())

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

    def test_review_round_identity_keeps_replays_stable_and_rounds_distinct(self):
        node = self.route["nodes"][0]
        identities = [BATCH.stable_attempt_id(self.route, node, "display", "owner",
            "att-parent-fixture", "codex", 1, review_round=round_number)
            for round_number in (1, 2, 3)]
        self.assertEqual(len(set(identities)), 3)
        self.assertEqual(identities[1], BATCH.stable_attempt_id(self.route, node,
            "changed-display", "changed-owner-label", "att-parent-fixture", "codex", 1,
            review_round=2))

    def _same_owner_next_review_round(self, prompt, parent_harness, *, notes=None, one_sided_revision=False):
        # Use the lab's actual review kinds/units and derived alternative id.
        # Only launch/process boundaries are faked; admission, both sealed
        # inputs, attempt matching and the round census run for real.
        from dispatch_replacement_batch import _input_path
        self._configure_lab_review()
        assignments = self.common_patches()[1]
        for index, leg in enumerate(self.legs()):
            self.write_existing(leg, status="done",
                note=notes[index] if notes else "completed-review-blocking", append=index != 0)
        self.seal_original_input()
        original_rows = self.jobs.read_bytes()
        source = BATCH.prior_batch_input(self.jobs, self.route, self.route["nodes"],
                                        "plan", "att-parent-fixture", BATCH.DEFAULT_PROMPT)
        source_path = _input_path(self.jobs, source["manifest_digest"])
        original_input = source_path.read_bytes()
        output = io.StringIO()
        commands = []

        def spawn(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(pid=12000, returncode=0,
                communicate=lambda: (success_receipt(command), ""), poll=lambda: 0)

        with contextlib.ExitStack() as stack:
            if one_sided_revision:
                # The canonical reader's proved supersession affects one leg;
                # batch must still apply its existing full-group round policy.
                stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE.ROUTE,
                    "_dependency_revisions", return_value=[{"supersedes": [self.legs()[0]["attempt_id"]]}]))
            for name, value in (("load_route", self.route), ("resolve_agent_home", self.base),
                                ("resolve_global_registry", SimpleNamespace(path=self.jobs)),
                                ("assign_harnesses", (assignments, "cross-harness", {})),
                                ("reserve_batch", ["a" * 32, "b" * 32])):
                stack.enter_context(mock.patch.object(BATCH, name, return_value=value))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt"))
            stack.enter_context(mock.patch.object(BATCH, "completion_marker_gate"))
            stack.enter_context(mock.patch.object(BATCH, "cancel_unclaimed"))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            process = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen", side_effect=spawn))
            stack.enter_context(mock.patch.dict(os.environ, {
                "AGENT_DISPATCH_SELF_SLUG": "owner", "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                "AGENT_DISPATCH_CURRENT_HARNESS": parent_harness, "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write"}))
            with contextlib.redirect_stdout(output):
                result = BATCH.main(self.argv() + ["--prompt-text", prompt])
            self.assertEqual(result, 0, output.getvalue())
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["newly_started"], 2)
            self.assertEqual(len(commands), 2)
            new_ids = {leg["attempt_id"] for leg in receipt["legs"]}
            self.assertTrue(new_ids.isdisjoint(leg["attempt_id"] for leg in self.legs()))
            self.assertEqual(self.jobs.read_bytes(), original_rows)
            self.assertEqual(source_path.read_bytes(), original_input)
            manifest, digest, _ = BATCH.build_manifest(
                parallel_group="plan", route_id=self.route["route_id"], parent_attempt_id="att-parent-fixture",
                independence="cross-harness", members=BATCH.manifest_members(receipt["legs"]),
                required_independence_axes=["cross-harness", "model-profile", "perspective"],
                realized_independence_axes=["cross-harness", "model-profile", "perspective"])
            second_path = _input_path(self.jobs, digest)
            self.assertNotEqual(source_path, second_path)
            self.assertEqual(json.loads(second_path.read_text())["options"]["prompt_text"], prompt)
            # A duplicate start while r2 is live starts nothing. After its
            # blocking verdict, the same command still respects the cap.
            for leg in receipt["legs"]:
                self.write_existing(leg, all_legs=receipt["legs"])
            second_input = second_path.read_bytes()
            process.reset_mock()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = BATCH.main(self.argv() + ["--prompt-text", prompt])
            self.assertEqual(result, 78, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["reason"], "prior-attempt-still-live")
            process.assert_not_called()
            # Fixture settlement replaces only its own open rows.
            self.jobs.write_text(self.jobs.read_text().replace("\topen\t", "\tdone\t")
                .replace("launch_started=1", "launch_started=1,note=completed-review-blocking"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = BATCH.main(self.argv() + ["--prompt-text", "round three"])
            self.assertEqual(result, 65, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["reason"], "review-round-budget-exhausted")
            process.assert_not_called()
            self.assertTrue(self.jobs.read_bytes().startswith(original_rows))
            self.assertEqual(source_path.read_bytes(), original_input)
            self.assertEqual(second_path.read_bytes(), second_input)

    def _configure_lab_review(self):
        import route_identity
        self.route["effective_intensity"] = "strong"
        for index, node in enumerate(self.route["nodes"]):
            node.update(id=("independent-verify" if index == 0 else "independent-verify-alternative"),
                        kind="review-worker", unit="qa/test", parallel_anchor="independent-verify")
            self.assertTrue(BATCH.DISPATCH_NODE.REVIEW_ROUND_CAP.is_round_capped_node(node))
        self.route["route_hash"] = route_identity.route_hash(self.route)

    def test_successful_capped_review_group_replay_reuses_its_attempts(self):
        self._configure_lab_review()
        with mock.patch.object(BATCH, "completion_marker_is_current", return_value=True), \
             mock.patch.object(BATCH, "completion_attempt_readiness", return_value=SimpleNamespace(state="ready")):
            self._assert_duplicate_batch_state(completed=True)

    def _assert_exhausted_replay_readiness(self, state, reason, code):
        # Admission observed settled rows; exact readiness subsequently changes.
        self._configure_lab_review()
        self.route["continuation_budget"] = {"review_round_cap": 1}
        import route_identity
        self.route["route_hash"] = route_identity.route_hash(self.route)
        with mock.patch.object(BATCH, "completion_marker_is_current", return_value=state != "unsettled"), \
             mock.patch.object(BATCH, "completion_attempt_readiness",
                 return_value=SimpleNamespace(state=state, reason="attempt-descendant-" + state)):
            self._assert_duplicate_batch_state(completed=True, refusal=(code, reason))

    def test_exhausted_replay_preserves_draining_refusal(self):
        self._assert_exhausted_replay_readiness("draining", "prior-attempt-still-live", 78)

    def test_exhausted_replay_preserves_unverifiable_refusal(self):
        self._assert_exhausted_replay_readiness("unverifiable", "prior-attempt-unverifiable", 78)

    def test_exhausted_replay_preserves_unsettled_refusal(self):
        self._assert_exhausted_replay_readiness("unsettled", "round-unsettled", 65)

    def test_same_owner_opens_next_review_round_with_changed_prompt(self):
        self._same_owner_next_review_round("closure check after fixes", "codex")

    def test_same_owner_opens_next_review_round_with_original_prompt(self):
        self._same_owner_next_review_round(BATCH.DEFAULT_PROMPT, "claude")

    def test_same_owner_next_review_round_works_for_opencode_parent(self):
        self._same_owner_next_review_round("closure check after fixes", "opencode")

    def test_mixed_pass_fail_opens_the_group_without_reclassifying_pass(self):
        self._same_owner_next_review_round("mixed verdict correction", "codex",
            notes=("completed-marker", "completed-review-blocking"))

    def test_one_sided_supersession_opens_a_full_group_generation(self):
        self._same_owner_next_review_round("one superseded peer", "claude",
            notes=("completed-marker", "completed-marker"), one_sided_revision=True)

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

    def test_assignment_honors_declared_affinity_with_distinct_harnesses(self):
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                self.route, self.route["nodes"], allow_degraded=False
            )
        self.assertEqual(independence, "persona")
        self.assertEqual([row[1] for row in rows], ["codex", "claude"])
        self.assertEqual(diagnostics["degradation_cause"], "")

    def test_assignment_uses_distinct_families_when_multiple_are_eligible(self):
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
        self.assertEqual(independence, "persona")
        self.assertEqual(len({row[1] for row in rows}), 2)
        self.assertEqual(diagnostics["independence_axes"], ["persona", "harness"])

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
        self.assertEqual(independence, "persona")
        self.assertEqual({row[1] for row in rows}, {"claude", "codex"})

    def _depth_affinity_route(self, strategy, *, weight=1.0, same_band=False,
                              affinity=True):
        """3-family fixture whose sealed policy pins the depth-2 worker preference.

        `same_band=True` puts all three families in `primary` so `band_rank` --
        which precedes every allocation term -- cannot decide the placement by
        itself; that is required to observe the preference at all.
        """
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": strategy,
            "window": 30,
            "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        if affinity:
            route["dispatch_allocation"].update({
                "depth_affinity": {"owner": "claude", "worker": "codex"},
                "depth_affinity_weight": weight,
                "usage_headroom_exponent": 2,
            })
        policy = (
            {"primary": ["claude", "codex", "opencode"], "relief": [],
             "last_resort": [], "promote_relief_below": 0}
            if same_band else
            {"primary": ["claude", "codex"], "relief": [],
             "last_resort": ["opencode"], "promote_relief_below": 0}
        )
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["harness_policy"] = dict(policy)
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        return route

    def _assign(self, route, scores):
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(
            BATCH.CAPACITY, "capacity_scores", return_value=scores
        ):
            return BATCH.assign_harnesses(
                route, route["nodes"], allow_degraded=False, jobs=self.jobs
            )

    def test_depth_affinity_allows_same_harness_but_never_beats_usage_gate(self):
        # A healthy preferred family may host both independent personas.
        # Once it crosses the usage gate, neither leg may be placed there.
        route = self._depth_affinity_route("balanced")
        rows, independence, _diag = self._assign(
            route, {"claude": 70.0, "codex": 80.0, "opencode": 60.0}
        )
        self.assertEqual(independence, "persona")
        self.assertEqual({row[1] for row in rows}, {"claude", "codex"})
        # codex is now gated (headroom 10 <= 100-90); an ungated combination exists.
        rows, independence, _diag = self._assign(
            route, {"claude": 70.0, "codex": 10.0, "opencode": 60.0}
        )
        self.assertEqual(independence, "persona")
        self.assertEqual({row[1] for row in rows}, {"claude"})

    def test_depth_affinity_changes_batch_placement_under_both_strategies(self):
        """The reversion falsifier: with the preference removed the batch must
        pick a different pair. `balanced` carries it in the shared deficit,
        `capacity-aware` through the shared margin oracle. Both are asserted as
        exact sets, so patching `preferred_for_depth` to None fails this test.
        """
        scores = {"claude": 70.0, "codex": 55.0, "opencode": 60.0}
        for strategy in ("balanced", "capacity-aware"):
            with self.subTest(strategy=strategy):
                without, _i, _d = self._assign(
                    self._depth_affinity_route(
                        strategy, weight=0.65, same_band=True, affinity=False
                    ),
                    scores,
                )
                with_affinity, _i, _d = self._assign(
                    self._depth_affinity_route(strategy, weight=0.65, same_band=True),
                    scores,
                )
                self.assertEqual({row[1] for row in without}, {"claude", "opencode"})
                self.assertEqual({row[1] for row in with_affinity},
                                 {"claude", "codex"})

    def test_capacity_aware_depth_affinity_stops_outside_the_margin(self):
        """Weight 0.65 is a 30-point margin. codex trails the best gauge by 15
        inside it and by 40 outside it; only the first may reorder."""
        route = self._depth_affinity_route(
            "capacity-aware", weight=0.65, same_band=True
        )
        inside, _i, _d = self._assign(
            route, {"claude": 70.0, "codex": 55.0, "opencode": 60.0}
        )
        self.assertEqual({row[1] for row in inside}, {"claude", "codex"})
        outside, _i, _d = self._assign(
            route, {"claude": 95.0, "codex": 55.0, "opencode": 60.0}
        )
        self.assertEqual({row[1] for row in outside}, {"claude", "opencode"})
        # An unknown gauge is excluded by capacity-aware and cannot be hoisted
        # by a preference either -- the same rule, one oracle.
        unknown, _i, _d = self._assign(
            route, {"claude": 70.0, "codex": None, "opencode": 60.0}
        )
        self.assertEqual({row[1] for row in unknown}, {"claude", "opencode"})

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
        self.assertEqual(independence, "persona")
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
        self.assertEqual(independence, "persona")
        self.assertEqual({row[1] for row in rows}, {"codex"})

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
        self.assertEqual(independence, "persona")
        self.assertNotIn("claude", {row[1] for row in rows})

    def _pinned_balanced_route(self, pin=None, target="worker"):
        route = json.loads(json.dumps(self.route))
        route["dispatch_allocation"] = {
            "strategy": "balanced", "window": 30, "usage_gate_used_percent": 90,
            "harness_order": ["claude", "codex", "opencode"],
        }
        for node in route["nodes"]:
            node["harness_affinity"] = "diverse"
            node["harness_policy"] = {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            }
            node["fallback_hops"][1]["candidates"].append(
                {"child_harness": "opencode", "status": "supported"}
            )
        if pin:
            route["selection_pins"] = {"contract_version": 1, target: {"harness": pin, "model": None, "effort": None}}
        return route

    def _assign_gated(self, route, limits=None):
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), mock.patch.object(BATCH.CAPACITY, "capacity_scores", return_value={
            "claude": 5.0, "codex": 60.0, "opencode": 80.0,
        }), mock.patch("dispatch_capacity_evidence.active_limits", return_value=limits or {}):
            return BATCH.assign_harnesses(route, route["nodes"], allow_degraded=False, jobs=self.jobs)

    def test_a_sealed_worker_pin_is_placed_even_when_the_usage_gate_would_skip_it(self):
        # claude has 5% headroom (gated, not at a limit). With `--pin worker=claude` it still takes
        # every leg; without the pin, or with a pin for another target, today's placement avoids it.
        rows, _independence, _diagnostics = self._assign_gated(self._pinned_balanced_route(pin="claude"))
        self.assertEqual({row[1] for row in rows}, {"claude"})
        for route in (self._pinned_balanced_route(), self._pinned_balanced_route(pin="claude", target="owner")):
            with self.subTest(pins=route.get("selection_pins")):
                rows, _independence, _diagnostics = self._assign_gated(route)
                self.assertNotIn("claude", {row[1] for row in rows})

    def test_a_sealed_worker_pin_at_its_limit_or_without_evidence_keeps_todays_placement(self):
        route = self._pinned_balanced_route(pin="claude")
        rows, _independence, diagnostics = self._assign_gated(
            route, limits={"claude": {"reset_epoch": 9999999999}})
        self.assertNotIn("claude", {row[1] for row in rows})
        self.assertEqual(diagnostics["family_exclusions"]["claude"], ["quota-until-9999999999"])
        unsupported = self._pinned_balanced_route(pin="claude")
        for node in unsupported["nodes"]:
            for hop in node["fallback_hops"]:
                for row in hop["candidates"]:
                    if row["child_harness"] == "claude":
                        row["status"] = "unsupported"
        rows, _independence, _diagnostics = self._assign_gated(unsupported)
        self.assertNotIn("claude", {row[1] for row in rows})

    def test_peer_legs_pinned_to_a_non_peer_family_are_a_degraded_sole_gate_not_a_refusal(self):
        everything = ["claude", "codex", "opencode"]
        nodes = self._quality_peer_nodes(peer_families=everything, aux_families=everything)
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": ["opencode"],
                "last_resort": [], "promote_relief_below": 0,
            },
        }
        with mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect):
            _rows, _independence, unpinned = BATCH.assign_harnesses(route, nodes, allow_degraded=False)
            self.assertEqual(unpinned["sole_gate"], "ok")
            route["selection_pins"] = {"contract_version": 1,
                                       "worker": {"harness": "opencode", "model": None, "effort": None}}
            rows, _independence, diagnostics = BATCH.assign_harnesses(route, nodes, allow_degraded=False)
            self.assertEqual({row[1] for row in rows}, {"opencode"})
            self.assertEqual(diagnostics["sole_gate"], "degraded")
            # a pin on a quality-peer family is an ordinary, healthy gate
            route["selection_pins"]["worker"]["harness"] = "codex"
            rows, _independence, diagnostics = BATCH.assign_harnesses(route, nodes, allow_degraded=False)
            self.assertEqual({row[1] for row in rows}, {"codex"})
            self.assertEqual(diagnostics["sole_gate"], "ok")

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
        self.assertEqual(independence, "persona")
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
        self.assertEqual(independence, "persona")
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
        self.assertEqual(independence, "persona")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("codex"), 3)
        self.assertEqual(placed.count("claude"), 0)

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
        self.assertEqual(independence, "persona")
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
        self.assertEqual(independence, "persona")
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
        self.assertEqual(independence, "persona")
        placed = [row[1] for row in rows]
        self.assertEqual(placed.count("claude"), 0)
        self.assertEqual(placed.count("codex"), 3)

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
        self.assertEqual(independence, "persona")
        by_adapter = {adapter: (hop, ordinal) for _node, adapter, hop, ordinal in rows}
        self.assertEqual(by_adapter["claude"], ("cross-harness-headless", 2))
        self.assertEqual(by_adapter["codex"], ("same-harness-headless", 1))

    def test_legacy_degrade_flag_does_not_change_persona_policy(self):
        # The legacy flag does not change current persona independence.
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                self.route, self.route["nodes"], allow_degraded=True
            )
        self.assertEqual(independence, "persona")
        self.assertEqual(diagnostics["degradation_cause"], "")
        self.assertEqual(set(diagnostics["usable_families"]), {"codex", "claude"})

    def test_single_usable_family_records_degradation_with_exclusion_diagnostics(self):
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
        self.assertEqual(independence, "persona")
        self.assertEqual(diagnostics["usable_families"], ["claude"])
        self.assertEqual(diagnostics["family_exclusions"], {
            "codex": ["dispatch-evidence-candidate-unsupported"],
            "opencode": ["dispatch-evidence-no-eligible-fallback"],
        })
        self.assertEqual(diagnostics["degradation_cause"], "single-usable-family")
        self.assertIn("degradation_detail", diagnostics)

    def test_single_usable_family_without_the_flag_is_normal(self):
        nodes = [single_family_node("plan"), single_family_node("plan-replica")]
        route = {"route_id": "rt-fixture", "route_hash": "sha256:fixture"}
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        self.assertEqual(independence, "persona")
        self.assertEqual({row[1] for row in rows}, {"claude"})
        self.assertEqual(diagnostics["degradation_cause"], "single-usable-family")
        self.assertEqual(diagnostics["family_exclusions"]["codex"],
                         ["dispatch-evidence-candidate-unsupported"])

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

    def test_policy_without_a_quality_peer_family_proceeds_as_degraded_sole_gate(self):
        # Disjoint deep/balanced-deep primaries (a warned user policy) define no
        # quality-peer family. That is a policy shape, not a harness shortage,
        # so the peer group is placed and the degradation is recorded instead
        # of refused with `peer-gate:no-quality-peer-family-hard-eligible`.
        def policy(primary):
            return {"primary": [primary], "relief": [], "last_resort": ["opencode"],
                    "promote_relief_below": 0}
        nodes = self._quality_peer_nodes(
            peer_families=["opencode"], aux_families=["opencode"]
        )
        for node in nodes:
            node["harness_policy"] = policy("codex" if node["model_profile"] == "deep" else "claude")
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": policy("codex"),
        }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=False
            )
        self.assertEqual({row[1] for row in rows}, {"opencode"})
        self.assertEqual(diagnostics["sole_gate"], "degraded")
        self.assertEqual(diagnostics["quality_peer_families"], [])

    def test_a_defined_but_unavailable_quality_peer_set_still_refuses(self):
        # The asymmetric half: the policy DOES define claude/codex as peers but
        # neither is eligible, so this stays the SD-100 availability refusal.
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
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ), self.assertRaises(BATCH.BatchError) as ctx:
            BATCH.assign_harnesses(route, nodes, allow_degraded=True)
        self.assertEqual(ctx.exception.reason, "parallel-cross-harness-unavailable")

    def test_a_node_policy_that_omits_a_harness_excludes_it_from_placement(self):
        # `opencode-light-model`: OpenCode is left out of the deep band, so a
        # deep node never lands on it even when it is the only eligible family
        # left and even though `band_rank` used to merely sort it last.
        nodes = self._quality_peer_nodes(
            peer_families=["claude", "opencode"], aux_families=["opencode"]
        )
        for node in nodes:
            node["harness_policy"] = {
                "primary": ["claude", "codex"], "relief": [], "last_resort": [],
                "promote_relief_below": 0,
            }
        route = {
            "route_id": "rt-fixture", "route_hash": "sha256:fixture",
            "owner_harness_policy": {
                "primary": ["claude", "codex"], "relief": [], "last_resort": [],
                "promote_relief_below": 0,
            },
        }
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            with self.assertRaises(BATCH.BatchError) as ctx:
                BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        # The auxiliary leg only had opencode; with opencode outside its policy
        # the group cannot be placed at all, and the reason says why.
        self.assertEqual(ctx.exception.reason, "parallel-headless-unavailable")
        self.assertIn("outside-profile-policy", ctx.exception.detail)
        peers_only = nodes[:2]
        with mock.patch.object(
            BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect
        ):
            rows, _independence, diagnostics = BATCH.assign_harnesses(
                route, peers_only, allow_degraded=False
            )
        self.assertEqual({row[1] for row in rows}, {"claude"})
        self.assertEqual(
            diagnostics["family_exclusions"]["opencode"], ["outside-profile-policy"]
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
        self.assertEqual(independence, "persona")
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

    def test_single_usable_family_is_normal_when_the_sole_gate_is_satisfied(self):
        # Same-harness placement is normal when the quality peer owns the gate.
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
        ):
            rows, independence, diagnostics = BATCH.assign_harnesses(
                route, nodes, allow_degraded=False
            )
        self.assertEqual(independence, "persona")
        self.assertEqual(diagnostics["sole_gate"], "ok")
        self.assertEqual(
            diagnostics["degradation_cause"], ""
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
        ):
            _rows, _independence, diagnostics = BATCH.assign_harnesses(route, nodes, allow_degraded=False)
        self.assertEqual(diagnostics["family_exclusions"]["codex"], [
            "dispatch-evidence-candidate-unsupported", "dispatch-evidence-no-eligible-fallback"])
        self.assertIn("codex=unsupported+no-candidate", BATCH._exclusion_codes({
            name: set(reasons) for name, reasons in diagnostics["family_exclusions"].items()}))

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

    def test_leg_allocation_receipts_persist_only_for_started_legs(self):
        # 2026-08-29: each realized leg leaves an allocation row carrying the
        # sealed policy and the preferred family beside the family it landed
        # on; existing/failed legs leave nothing (same gate as degradations).
        diagnostics = {
            "capacity": {"claude": 30.0, "codex": 74.0, "opencode": None},
            "sole_gate": "ok",
            "allocation": {
                "policy": {"strategy": "balanced", "window": 30, "depth_affinity": {"worker": "codex"}},
                "preferred": "codex",
                "counts": {"claude": 2, "codex": 1, "opencode": 0},
            },
        }
        node_id = self.route["nodes"][0]["id"]
        results = [
            {"node": node_id, "adapter": "claude", "hop": "same-harness-headless",
             "attempt_id": "att-a", "launch_state": "started", "parallel_group": "pg",
             "parallel_leg_index": 0, "parallel_leg_count": 2},
            {"node": node_id, "adapter": "codex", "hop": "cross-harness-headless",
             "attempt_id": "att-b", "launch_state": "existing"},
            {"node": node_id, "adapter": "codex", "hop": "cross-harness-headless",
             "attempt_id": "att-c", "launch_state": "failed"},
        ]
        with mock.patch.object(
            BATCH, "record_allocation_receipt",
            return_value={"path": "/ledger/rt.jsonl", "event_id": "al-x"},
        ) as record:
            path = BATCH._persist_leg_allocations(
                self.base, self.route, diagnostics, results, action="start"
            )
        self.assertEqual(path, "/ledger/rt.jsonl")
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["route_id"], self.route["route_id"])
        self.assertEqual(kwargs["route_node"], node_id)
        self.assertEqual(kwargs["unit"], self.route["nodes"][0].get("unit"))
        self.assertEqual(kwargs["writer"], "dispatch-batch.py")
        self.assertEqual(kwargs["action"], "start")
        self.assertEqual(kwargs["attempt_id"], "att-a")
        self.assertEqual(kwargs["child_harness"], "claude")
        self.assertEqual(kwargs["preferred"], "codex")
        self.assertEqual(kwargs["allocation"]["strategy"], "balanced")
        self.assertEqual(kwargs["counts"], {"claude": 2, "codex": 1, "opencode": 0})
        self.assertEqual(kwargs["capacity"]["codex"], 74.0)
        self.assertEqual(kwargs["parallel_leg_count"], 2)
        self.assertEqual(kwargs["agent_home"], self.base)
        with mock.patch.object(BATCH, "record_allocation_receipt") as record:
            self.assertIsNone(BATCH._persist_leg_allocations(
                self.base, self.route, diagnostics, results[1:], action="start"
            ))
        record.assert_not_called()

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
            "degradation_cause": "",
        }
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(
                BATCH, "assign_harnesses",
                return_value=(assignments, "persona", diagnostics),
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

    def test_same_harness_receipt_has_no_degradation_reason(self):
        output = io.StringIO()
        rc, receipt = self._degraded_dry_run_receipt(output)
        self.assertEqual(rc, 0)
        self.assertEqual(receipt["state"], "validated")
        # The default action launches nothing, and the receipt says so.
        self.assertEqual((receipt["registered"], receipt["started"], receipt["child_spawned"]), (0, 0, 0))
        self.assertIn("rerun with --start", receipt["next_step"])
        self.assertEqual(receipt["degradation_reason"], "")
        self.assertEqual(receipt["selection_diagnostics"]["degradation_cause"], "")

    def test_same_harness_receipt_never_claims_cross_harness_axis(self):
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
        self.assertEqual(independence, "persona")
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
            with self.subTest(marker=marker, expected=expected), \
                    tempfile.TemporaryDirectory() as governor_root:
                with mock.patch.object(
                    BATCH.subprocess, "run",
                    return_value=self._governor_subprocess_result(stderr=marker),
                ):
                    with self.assertRaises(BATCH.BatchError) as ctx:
                        BATCH.reserve_batch(
                            Path("/tmp/governor"), Path(governor_root),
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

    def test_reservation_mismatch_detail_survives_receipt_and_ledger(self):
        detail = "batch_assignment_sha256:expected=original:actual=retry"
        result = BATCH.wrapper_result(
            {"node": "research-alternative", "adapter": "codex", "attempt_id": "att-gap"},
            SimpleNamespace(returncode=75),
            "check=failed\nreason=parallel-group-reservation-mismatch\n"
            f"detail={detail}\nregistered=0\nstarted=0\nchild_spawned=0\n", "",
        )
        self.assertEqual(result["detail"], detail)
        with mock.patch.object(BATCH, "record_degradation") as record:
            BATCH._record_failed_legs(self.route, [result], self.base)
        self.assertEqual(record.call_args.kwargs["detail"], detail)

    def test_guard_time_cleanup_diagnostics_survive_failed_wrapper(self):
        diagnostic = json.dumps({"attempt_id": "att-prior", "scope": "guard-time",
            "process_group": {"state": "empty", "reason": ""},
            "attempt_tagged_descendants": {"state": "unverifiable", "reason": "procfs-environ-denied"}})
        result = BATCH.wrapper_result(
            {"node": "impl-review", "adapter": "codex", "attempt_id": "att-new"},
            SimpleNamespace(returncode=73),
            "check=failed\nreason=prior-attempt-unverifiable\nchild_spawned=0\n",
            "cleanup_diagnostics=" + diagnostic + "\n")
        self.assertEqual(result["exit_code"], 73)
        self.assertEqual(result["reason"], "prior-attempt-unverifiable")
        self.assertEqual([json.loads(item) for item in result["cleanup_diagnostics"]], [json.loads(diagnostic)])

    def test_duplicate_batch_state_does_not_claim_concurrent_launch(self):
        self._assert_duplicate_batch_state()

    def test_sd160_completed_row_only_batch_reuses_exact_manifest_without_spawn(self):
        with mock.patch.object(BATCH, "completion_marker_is_current", return_value=True), \
             mock.patch.object(BATCH, "completion_attempt_readiness", return_value=SimpleNamespace(state="ready")):
            self._assert_duplicate_batch_state(completed=True)

    def _assert_duplicate_batch_state(self, *, completed=False, refusal=None):
        stack, assignments = self.common_patches()
        output = io.StringIO()
        for leg in self.legs(assignments):
            self.write_existing(leg, status="done" if completed else "open",
                                note="completed-marker" if completed else "")
            if completed:
                marker = BATCH.resolve_dispatch_state_root(self.base, self.jobs) / "completion" / self.route["route_id"] / (str(leg["node"]) + ".json")
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({"attempt_id": leg["attempt_id"]}))

        original_rows = self.jobs.read_bytes()
        with stack:
            stack.enter_context(mock.patch("dispatch_capacity_evidence.active_limits", side_effect=AssertionError("live replay spends no quota")))
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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
        self.assertEqual(self.jobs.read_bytes(), original_rows)
        reserve.assert_not_called()
        popen.assert_not_called()
        if refusal:
            self.assertEqual(rc, refusal[0], output.getvalue())
            receipt = json.loads(output.getvalue())
            self.assertEqual(receipt["reason"], refusal[1])
            self.assertEqual((receipt["admitted"], receipt["spawned"]), (0, 0))
            return
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
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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

        self.seal_original_input()
        with stack:
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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
            before = self.jobs.read_bytes()
            for registered_only in (False, True):
                if registered_only:
                    self.write_existing(self.legs()[1], claimed="0", live_identity=False)
                with mock.patch("dispatch_capacity_evidence.active_limits", return_value={
                        "claude": {"reset_epoch": 9999999999}}), contextlib.redirect_stdout(output):
                    blocked_rc = BATCH.main(self.argv())
                self.assertEqual(blocked_rc, 75, output.getvalue())
                self.assertEqual(json.loads(output.getvalue())["reason"], "batch-pending-hard-limit")
                reserve.assert_not_called(); popen.assert_not_called()
                self.jobs.write_bytes(before)
                output.seek(0); output.truncate(0)
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
        self.seal_original_input()
        with stack:
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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
        self.seal_original_input()
        with stack:
            stack.enter_context(mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_side_effect))
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
        # The admission baseline and aggregate audit model a sealed Git route;
        # give this fixture a real HEAD so strict Git-observation failures stay
        # distinct from an empty/non-Git test folder.
        subprocess.run(["git", "-C", str(self.base), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.base), "config", "user.email", "fixture@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.base), "config", "user.name", "Fixture"], check=True)
        subprocess.run(["git", "-C", str(self.base), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.base), "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
        node = dict(replica_node("plan", "codex"))
        node["completion_gate"] = "code-plan-check"
        node["subdivision"] = {
            "min_intensity": "strong", "max_slices": 4, "disjointness": "exact-fixed-files",
        }
        node["write_scope"] = ["source/**"]
        node2 = dict(replica_node("plan-replica", "claude"))
        node2["completion_gate"] = "code-plan-check"
        route = dict(self.route)
        route["source_commit"] = subprocess.check_output(
            ["git", "-C", str(self.base), "rev-parse", "HEAD"], text=True).strip()
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


class CompiledStrongGroupPlacementTest(unittest.TestCase):
    def test_compiled_strong_width_two_groups_choose_distinct_harnesses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
            (repo / "README.md").write_text("compiled strong route\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

            artifact_root = base / ".agent_reports"
            artifact_root.mkdir()
            jobs = base / "jobs.log"
            evidence_path = base / "dispatch-evidence.json"
            evidence_path.write_text(json.dumps({
                "tuples": [
                    {
                        "parent_harness": "codex", "parent_transport": "headless",
                        "parent_sandbox": "workspace-write", "child_harness": child,
                        "launch_authority": "conductor", "status": "supported",
                        "probe_source": "integration-fixture", "probe_time": "2026-07-24T00:00:00Z",
                        "failure_class": "", "checked_worktree": str(repo.resolve()),
                        "failure_scope": "none",
                        "codex_command": "ok" if child == "codex" else "not-applicable",
                        "retry_on_isolated_worktree": 0,
                    }
                    for child in ("codex", "claude", "opencode")
                ],
                "native_subagent": [],
            }, sort_keys=True), encoding="utf-8")
            env = {
                **{key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_", "AGENT_ROUTE_", "AGENT_ARTIFACT_"))},
                "AGENT_HOME": str(ROOT), "AGENT_DISPATCH_JOBS": str(jobs),
            }
            compiled = subprocess.run([
                sys.executable, str(ROOT / "utilities" / "capability-route.py"),
                "compile", "--slug", "strong-placement", "--capability", "autopilot-code",
                "--capability-mode", "dev", "--intensity", "strong", "--cwd", str(repo),
                "--artifact-root", str(artifact_root), "--signal", "shared-contract",
                "--transport", "headless", "--tracking", "tracked",
                "--dispatch-evidence", str(evidence_path), "--spec-read", "integration-fixture",
                "--drift-verdict", "within-spec", "--workflow-mode", "tracked",
                "--artifact-guard", "integration-fixture",
            ], text=True, capture_output=True, check=False, env=env)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            route = json.loads(compiled.stdout)
            parent = {
                "parent_harness": "codex", "parent_transport": "headless",
                "parent_sandbox": "workspace-write",
            }
            for group in ("plan", "plan-check", "impl-review"):
                nodes = [node for node in route["nodes"]
                         if (node.get("parallel_group") or node.get("replica_group")) == group]
                self.assertEqual(len(nodes), 2, group)
                assignments, _independence, _diagnostics = BATCH.assign_harnesses(
                    route, nodes, allow_degraded=False, parent_identity=parent,
                )
                self.assertEqual(len({row[1] for row in assignments}), 2, group)


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
            "group=node.split('-')[0]\n"
            "expected=3 if group=='frame' else 2\n"
            "deadline=time.monotonic()+60\n"
            "while time.monotonic()<deadline:\n"
            " with open(events) as stream: rows=[json.loads(line) for line in stream if line.strip()]\n"
            " if sum(r['event']=='start' and r['node'].split('-')[0]==group for r in rows)>=expected: break\n"
            " time.sleep(.05)\n"
            "else: raise SystemExit('fixture batch start barrier expired')\n"
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

    def test_framing_is_not_a_batch_parallel_group_in_any_recipe(self):
        # Cheap standing pin for the property the skipped integration test
        # below used to carry incidentally: batch must never be handed a frame
        # group. A depth-1 node carrying `parallel_group` is refused by
        # dispatch-batch itself (`parallel-group-depth-invalid`), so declaring
        # one would compile and then die at launch.
        registry = json.loads(
            (ROOT / "capabilities" / "topologies.json").read_text(encoding="utf-8")
        )
        frame_legs = 0
        for recipe in registry["recipes"]:
            standard_plus = recipe.get("standard_plus", {})
            for group in standard_plus.get("parallel_groups", []):
                self.assertNotEqual(group["id"], "frame", recipe["capability"])
            for node in standard_plus.get("nodes", []):
                if node.get("worker_type") == "frame":
                    frame_legs += 1
                    self.assertNotIn("parallel_group", node)
                    self.assertNotIn("replica_group", node)
                    self.assertEqual(node.get("dispatch_depth"), 1)
        # Five entry recipes x two sibling legs, plus the compiler-internal
        # route-frame recipe's own frame pair (framed shape): its legs are
        # separate depth-1 launches and carry no parallel group either.
        self.assertEqual(frame_legs, 12)

    @unittest.skip(
        "Frame is no longer a dispatch-batch parallel group, and this test's "
        "whole sequence is built on launching it as one: it batches the frame "
        "group first, completes both legs from the batch receipt's attempt "
        "ids, then proves the frame-review gate fences the plan batch until it "
        "is raised and released. The frame bootstrap layer moved framing to two "
        "explicit dispatch-depth-1 sibling nodes launched by the depth-0 "
        "session, which is a different surface entirely (dispatch-owner / "
        "dispatch-node), so step one has no batch to run and every later step "
        "loses the attempt ids it depends on. Skipped rather than left to pass "
        "vacuously -- with the frame group gone the first assertions are all "
        "empty-set comparisons that would go green while proving nothing. "
        "Restoring it means re-founding the fixture on the real depth-1 launch "
        "path; that is a rewrite, not a fixture refresh, and it is out of this "
        "unit's fence. Tracked as gap G7 in the W4 evidence log. The frame "
        "group's absence itself is asserted below and in "
        "tools/capability_topology.test.py::test_parallel_group_declarations."
    )
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
            (repo / ".gitignore").write_text(".dispatch/\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo), "add", "README.md", ".gitignore"],
                check=True,
            )
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

            artifact_root = base / ".agent_reports"
            artifact_root.mkdir()
            jobs = base / "jobs.log"
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
                    "compile", "--slug", "dispatch-batch-fixture",
                    "--capability", "autopilot-code",
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
                env={
                    **{key: value for key, value in os.environ.items()
                       if not key.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_", "AGENT_ROUTE_", "AGENT_ARTIFACT_"))},
                    "AGENT_HOME": str(ROOT),
                    "AGENT_DISPATCH_JOBS": str(jobs),
                },
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
            def group_nodes(group):
                return {
                    node["id"] for node in route["nodes"]
                    if (node.get("parallel_group") or node.get("replica_group")) == group
                }
            # Framing is no longer a dispatch-batch parallel group at all. The
            # frame bootstrap layer declares its legs as explicit sibling nodes
            # at dispatch depth 1, and a depth-1 node carrying `parallel_group`
            # is refused by this very file (`parallel-group-depth-invalid`), so
            # batch must see no frame group here. The third `frame-contrarian`
            # leg the group used to add at strong+ is gone with it.
            self.assertEqual(group_nodes("frame"), set())
            self.assertEqual(
                {node["id"] for node in route["nodes"]
                 if node.get("worker_type") == "frame"},
                {"frame", "frame-alternative"},
            )
            first_nodes = group_nodes("frame")
            plan_nodes = group_nodes("plan")
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
                    "AGENT_HOME": str(ROOT),
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
            # This integration tests cross-harness joining with the shipped Claude
            # config: its deep profile rides the dispatch-eligible opus tier (SD-85).
            user_models = claude_home / "agent-config" / "models.conf"
            user_models.parent.mkdir()
            user_models.write_text((ROOT / "adapters/claude/config/models.conf").read_text())
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
                f"owner_route_id={route['route_id']},parent_sid=integration-parent-session,"
                # Fixture-only depth-0 recipient on the supported human-gate
                # carrier; this does not select either mock child's harness.
                "parent_completion_delivery=claude-parent-runtime,"
                f"attempt_id={parent_attempt},pid={os.getpid()},pid_start={parent_start}\n",
                encoding="utf-8",
            )
            governor_root = artifact_root / ".runtime" / "model-worker-governor"
            # A standalone fixture must not inherit the registered worker's
            # owner/stage identity from the test runner; the fixture below
            # declares its own parent tuple explicitly.
            env = {
                **{key: value for key, value in os.environ.items()
                   if not key.startswith(("AGENT_DISPATCH_", "AGENT_OWNER_ROUTE_",
                                          "AGENT_ROUTE_", "AGENT_ARTIFACT_"))},
                "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                "AGENT_HOME": str(ROOT),
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
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    events = self._events(events_path)
                    if sum(
                        row.get("event") == "start" and row.get("node") in first_nodes
                        for row in events
                    ) == len(first_nodes):
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                starts = [
                    row for row in self._events(events_path)
                    if row.get("event") == "start" and row.get("node") in first_nodes
                ]
                if len(starts) != len(first_nodes) and process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(
                    len(starts), len(first_nodes),
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
                    and job.route_node in first_nodes
                ]
                self.assertEqual(
                    len(visible), len(first_nodes),
                    [(job.slug, job.route_node, job.liveness) for job in fleet_jobs],
                )
                self.assertEqual({job.harness for job in visible}, {"codex", "claude"})
                self.assertEqual({job.route_node for job in visible}, first_nodes)
                self.assertTrue(all(job.dispatch_depth == 2 for job in visible))
                self.assertTrue(all(job.attempt_contract_status == "current" for job in visible))
                self.assertTrue(all(job.liveness == "working" for job in visible))
                route_rows = [
                    job for job in fleet_jobs
                    if job.route_id == route["route_id"] and job.route_node in first_nodes
                ]
                self.assertEqual(len(route_rows), len(first_nodes))
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

            # Completing the frame legs does not raise or release their human
            # review gate. Exercise the real fence before preparing the fixture
            # through the supervisor's public block/release commands.
            refused_plan = launch_group("plan")
            refused_stdout, refused_stderr = refused_plan.communicate(timeout=10)
            self.assertEqual(refused_plan.returncode, 65, refused_stdout + refused_stderr)
            self.assertIn("human-gate-not-raised", refused_stdout + refused_stderr)
            self.assertFalse(any(
                row.get("event") == "start" and row.get("node") in plan_nodes
                for row in self._events(events_path)
            ))
            gate_artifact = base / "frame-review.md"
            gate_artifact.write_text("Batch integration fixture review\n", encoding="utf-8")
            # Model the fixture's depth-0 reviewer, independently of whether
            # the test runner itself is a registered headless worker.
            reviewer_env = {key: value for key, value in env.items()
                            if key != "AGENT_DISPATCH_REGISTERED_WORKER"}
            for action in (
                ["gate", "--block", "--artifact", str(gate_artifact)],
                ["release", "--decision", "proceed"],
            ):
                gate = subprocess.run(
                    [sys.executable, str(ROOT / "utilities" / "workflow-supervisor.py"),
                     *action, "--route", str(route_path), "--gate", "frame-review",
                     "--jobs", str(jobs)],
                    cwd=ROOT, env=reviewer_env, text=True, capture_output=True,
                    check=False,
                )
                self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)

            plan_process = launch_group("plan")
            plan_stdout = plan_stderr = ""
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    plan_starts = [
                        row for row in self._events(events_path)
                        if row.get("event") == "start" and row.get("node") in plan_nodes
                    ]
                    if len(plan_starts) == 2 or plan_process.poll() is not None:
                        break
                    time.sleep(0.05)
                if len(plan_starts) != 2 and plan_process.poll() is not None:
                    plan_stdout, plan_stderr = plan_process.communicate(timeout=5)
                self.assertEqual(
                    len(plan_starts), 2,
                    f"plan batch exited={plan_process.poll()} "
                    f"stdout={plan_stdout} stderr={plan_stderr} jobs={jobs.read_text()}",
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
                self.assertEqual(len(registered), len(first_nodes | plan_nodes))
                self.assertEqual(
                    {metadata["route_node"] for _status, _slug, metadata in registered},
                    first_nodes | plan_nodes,
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
            for group, nodes in (("frame", first_nodes), ("plan", plan_nodes)):
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


class GroupLegReviewRoundCapTest(unittest.TestCase):
    """C-14: a realized parallel_group leg on a capped anchor (plan-check/
    impl-review/test) must not bypass the round cap that dispatch-node.py
    enforces for a single session -- one leg over budget refuses the whole
    group (child 0) before the atomic reservation, not just that leg."""

    def setUp(self) -> None:
        governor_env = mock.patch.dict(os.environ, {"AGENT_MODEL_GOVERNOR_ROOT": ""})
        governor_env.start()
        self.addCleanup(governor_env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.route_path = self.base / "route.json"
        self.route_path.write_text("{}", encoding="utf-8")
        self.jobs = self.base / "jobs.log"

    def _review_node(self, node_id, index, affinity):
        return {
            "id": node_id,
            "dispatch_depth": 2,
            "depends_on": ["execute"],
            "replica_group": "impl-review",
            "parallel_group": "impl-review",
            "parallel_leg_index": index,
            "parallel_leg_count": 2,
            "parallel_independence_axes": ["cross-harness", "model-profile", "perspective"],
            "model_profile": "balanced-deep" if index == 0 else "light",
            "perspective": "primary-review" if index == 0 else "independent-review",
            "harness_affinity": affinity,
            "fallback_hops": [
                candidate("codex", "same-harness-headless", 1),
                candidate("claude", "cross-harness-headless", 2),
            ],
        }

    def _route(self, effective_intensity):
        return {
            "route_id": "rt-fixture",
            "route_hash": "sha256:fixture",
            "cwd": str(self.base),
            "effective_intensity": effective_intensity,
            "nodes": [
                self._review_node("impl-review", 0, "codex"),
                self._review_node("impl-review-replica", 1, "claude"),
            ],
        }

    def _rows(self, node_id, n):
        # SD-153: `failure_class=fail` makes this a genuine FAIL verdict, so
        # it still spends the round budget the way a bare `dead-worker-fail`
        # (a crash, not a verdict) no longer does on its own.
        pipe = ("capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
                "route=rt-fixture,route_node=" + node_id + ",note=dead-worker-fail,failure_class=fail")
        return "".join(
            f"2026-08-24T00:00:0{i}Z\tdone\t{self.base}\t{self.base}\tslug-r{i}\t"
            f"{pipe},attempt_id=att-{node_id}-{i}\n"
            for i in range(1, n + 1)
        )

    def _run(self, route, prior_rows, *, probe_message="assignment must not run once the group round cap is exceeded"):
        self.jobs.write_text(prior_rows, encoding="utf-8")
        argv = [
            "--route", str(self.route_path), "--replica-group", "impl-review",
            "--action", "start", "--slug-prefix", "fixture", "--parent", "owner",
            "--jobs", str(self.jobs),
        ]
        output = io.StringIO()
        with mock.patch.object(BATCH, "load_route", return_value=route), \
             mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base), \
             mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)), \
             mock.patch.object(
                 BATCH, "assign_harnesses",
                 side_effect=AssertionError(probe_message),
             ), \
             mock.patch.dict(os.environ, {
                 "AGENT_DISPATCH_SELF_SLUG": "owner",
                 "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                 "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
             }), \
             contextlib.redirect_stdout(output):
            result = BATCH.main(argv)
        return result, output.getvalue()

    def test_standard_group_leg_is_refused_at_the_third_round(self):
        route = self._route("standard")
        result, out = self._run(route, self._rows("impl-review", 2))
        self.assertEqual(result, 65)
        receipt = json.loads(out)
        self.assertEqual(receipt["reason"], "review-round-budget-exhausted")
        self.assertIn("route_node=impl-review", receipt["detail"])
        self.assertIn("round=3", receipt["detail"])
        self.assertIn("max_round=2", receipt["detail"])
        self.assertEqual(
            {key: receipt[key] for key in (
                "admitted", "spawned", "registered", "started", "child_spawned"
            )},
            {"admitted": "0", "spawned": "0", "registered": "0",
             "started": "0", "child_spawned": "0"},
        )

    def test_standard_group_leg_within_budget_proceeds_past_the_cap_check(self):
        route = self._route("standard")
        # assign_harnesses raising the probe AssertionError (rather than a
        # caught BatchError receipt) proves the cap check let this leg
        # through to assignment, exactly as an unexceeded budget should.
        with self.assertRaisesRegex(
            AssertionError, "assignment must not run once the group round cap is exceeded",
        ):
            self._run(route, self._rows("impl-review", 1))

    def test_direct_group_leg_is_refused_at_the_second_round(self):
        route = self._route("direct")
        result, out = self._run(route, self._rows("impl-review", 1))
        self.assertEqual(result, 65)
        receipt = json.loads(out)
        self.assertEqual(receipt["reason"], "review-round-budget-exhausted")
        self.assertIn("max_round=1", receipt["detail"])

    def test_uncapped_group_is_unaffected(self):
        route = self._route("standard")
        route["nodes"] = [replica_node("plan", "codex"), replica_node("plan-replica", "claude")]
        argv = [
            "--route", str(self.route_path), "--replica-group", "plan",
            "--action", "start", "--slug-prefix", "fixture", "--parent", "owner",
            "--jobs", str(self.jobs),
        ]
        self.jobs.write_text(self._rows("plan", 5), encoding="utf-8")
        output = io.StringIO()
        with mock.patch.object(BATCH, "load_route", return_value=route), \
             mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base), \
             mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)), \
             mock.patch.object(
                 BATCH, "assign_harnesses",
                 side_effect=AssertionError("uncapped node must reach assignment"),
             ), \
             mock.patch.dict(os.environ, {
                 "AGENT_DISPATCH_SELF_SLUG": "owner",
                 "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                 "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
             }), \
             contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(AssertionError, "uncapped node must reach assignment"):
                BATCH.main(argv)

    def test_leg_launch_prompt_carries_the_same_round_protocol_block_as_dispatch_node(self):
        """Real round admission keeps guidance separate from the sealed task.

        Run dispatch-batch and dispatch-node, substituting their launch calls.
        The wrapper receives the original raw task and the exact round protocol
        as recovery context, so its assignment digest stays reserved.
        """
        route = self._route("standard")
        for node in route["nodes"]:
            # `kind="pipeline-stage"` (not "review-worker") deliberately: the
            # SD-155 lineage-verified round census only applies to a real
            # `review-worker` node's hash-sealed route lineage, which this
            # hand-built fixture route (unlike `_route`'s real callers) never
            # seals. The round CAP is keyed by node id (`ROUND_CAPPED_NODE_IDS`
            # membership), not by kind, so `impl-review` still gets the exact
            # same admission/`round_protocol_block` wiring under test here.
            node.update(kind="pipeline-stage", unit="qa/plan-review",
                        completion_gate="impl-review-complete", write_scope=[])
        route["capability"] = "autopilot-code"
        route["capability_mode"] = "dev"
        route["registry_digest"] = "sha256:fixture-registry"
        # `bind_dispatch_evidence` checks the assigned candidate's own
        # parent_harness/transport/sandbox against `current_parent_identity()`
        # (this test's own AGENT_DISPATCH_CURRENT_* env below): the shared
        # `candidate()` fixture helper omits those fields entirely (its other
        # callers never launch a leg far enough to reach this check), so this
        # test's own candidates carry the full real evidence-tuple shape
        # (`dispatch-node.py`'s `EVIDENCE_TUPLE_FIELDS`).
        for node in route["nodes"]:
            for hop in node["fallback_hops"]:
                for row in hop["candidates"]:
                    row.update(
                        parent_harness="codex", parent_transport="headless",
                        parent_sandbox="workspace-write", launch_authority="conductor",
                        probe_source="fixture",
                    )
        self.jobs.write_text(
            "2026-08-24T00:00:01Z\tdone\t" + str(self.base) + "\t" + str(self.base) + "\tslug-r1\t"
            "capability=autopilot-code,attempt_schema_version=2,registered_worker=1,"
            "route=rt-fixture,route_node=impl-review,worker_type=review,"
            "note=dead-invalid-envelope,attempt_id=att-impl-review-1\n",
            encoding="utf-8",
        )
        self.route_path.write_text(json.dumps(route), encoding="utf-8")
        argv = [
            "--route", str(self.route_path), "--parallel-group", "impl-review",
            "--action", "start", "--slug-prefix", "fixture", "--parent", "owner",
            "--jobs", str(self.jobs),
        ]
        captured = {}
        recovery_context = {}

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = command
                self.returncode = 0
                self.pid = 12000
                leg_env = kwargs.get("env") or {}

                def fake_inner_run(inner_argv, *a, **kw):
                    if "--worktree" in inner_argv:
                        node_id = inner_argv[inner_argv.index("--route-node") + 1]
                        captured[node_id] = list(inner_argv)
                        recovery_context[node_id] = kw["env"].get("AGENT_DISPATCH_RETRY_BRIEF", "")
                    return SimpleNamespace(returncode=0, stdout="", stderr="", received_signal=None)

                with mock.patch.dict(os.environ, leg_env, clear=True), \
                     mock.patch.object(sys, "argv", ["dispatch-node.py"] + command[2:]), \
                     mock.patch.object(BATCH.DISPATCH_NODE, "run_forwarding_termination", side_effect=fake_inner_run), \
                     mock.patch.object(BATCH.DISPATCH_NODE.subprocess, "run", side_effect=fake_inner_run):
                    try:
                        BATCH.DISPATCH_NODE.main()
                    except SystemExit:
                        pass

            def communicate(self):
                return success_receipt(self.command), ""

            def poll(self):
                return self.returncode

        def resolve_full_evidence(route, node, adapter, parent_identity=None):
            # Unlike the shared module-level `resolve_side_effect` (which
            # hands `bind_dispatch_evidence` a synthetic two-key `tuple_row`
            # its other callers never inspect further), this test needs the
            # FULL candidate row -- including the parent_harness/transport/
            # sandbox fields added above -- so the real `validate_parent_
            # identity` check inside `bind_dispatch_evidence` (reached only
            # once a leg gets this far) passes on real data, not a stub.
            for entry in sorted(node.get("fallback_hops", []), key=lambda row: row.get("ordinal", 0)):
                rows = [row for row in entry.get("candidates", []) if row.get("child_harness") == adapter]
                if rows:
                    return BATCH.DISPATCH_NODE.CheckedSelection(
                        rows[0], rows[0], str(entry["fallback_hop"]), int(entry["ordinal"]),
                    )
            raise BATCH.DISPATCH_NODE.DispatchNodeError(
                "dispatch-evidence-no-eligible-fallback", adapter=adapter,
            )

        with mock.patch.object(BATCH, "load_route", return_value=route), \
             mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base), \
             mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)), \
             mock.patch.object(BATCH, "resolve_live_parent_attempt"), \
             mock.patch.object(BATCH, "completion_marker_gate"), \
             mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)), \
             mock.patch.object(BATCH, "reserve_batch", return_value=["a" * 32, "b" * 32]), \
             mock.patch.object(BATCH, "cancel_unclaimed"), \
             mock.patch.object(BATCH.DISPATCH_NODE, "resolve_checked_tuple", side_effect=resolve_full_evidence), \
             mock.patch.object(BATCH.subprocess, "Popen", side_effect=FakeProcess), \
             mock.patch.dict(os.environ, {
                 "AGENT_DISPATCH_SELF_SLUG": "owner",
                 "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture",
                 "AGENT_DISPATCH_CURRENT_HARNESS": "codex",
                 "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
                 "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
             }), \
             contextlib.redirect_stdout(io.StringIO()):
            BATCH.main(argv)

        self.assertIn("impl-review", captured)
        wrapper_argv = captured["impl-review"]
        self.assertIn("--prompt-text", wrapper_argv)
        actual_prompt = wrapper_argv[wrapper_argv.index("--prompt-text") + 1]

        impl_review_node = next(n for n in route["nodes"] if n["id"] == "impl-review")
        prior_rows = BATCH.DISPATCH_NODE.prior_round_attempts(
            self.jobs, route["route_id"], "impl-review", route=None,
        )
        budget = BATCH.DISPATCH_NODE.admit_round(
            route, impl_review_node, self.jobs, owner_attempt_id="owner",
        ).budget
        expected_block = BATCH.DISPATCH_NODE.round_protocol_block(
            budget, prior_rows,
            BATCH.DISPATCH_NODE.worker_type_for_kind(impl_review_node["kind"]),
            "impl-review",
        )
        self.assertNotEqual(expected_block, "")
        self.assertIn("판정 없음", expected_block)
        self.assertEqual(actual_prompt, BATCH.DEFAULT_PROMPT)
        self.assertEqual(recovery_context["impl-review"], expected_block)



class NeverStartedBatchRoundAdmissionTest(unittest.TestCase):
    def test_multiple_withdrawn_registrations_do_not_block_batch_admission(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs.log"
            route = {
                "route_id": "rt-batch-unstarted",
                "effective_intensity": "standard",
                "continuation_budget": {"review_round_cap": 2},
                "nodes": [],
            }
            node = {"id": "plan-check", "kind": "pipeline-stage", "depends_on": []}
            rows = []
            for index in (1, 2):
                metadata = (
                    "attempt_schema_version=2,dispatch_depth=2,transport=headless,"
                    "execution_surface=registered-headless,registered_worker=1,"
                    f"route_id={route['route_id']},route_node={node['id']},"
                    f"attempt_id=att-batch-unstarted-{index},launch_claimed=0"
                )
                rows.append(
                    f"2026-10-01T00:00:0{index}Z\tdone\t/r\t/w\ts\t{metadata},"
                    "launch_outcome=never-launched,failure_class=cancelled,note=cancelled-by-parent"
                )
            jobs.write_text("\n".join(rows) + "\n", encoding="utf-8")

            prior = BATCH.DISPATCH_NODE.prior_round_attempts(
                jobs, route["route_id"], node["id"]
            )
            admission = BATCH.DISPATCH_NODE.admit_round(
                route, node, jobs, owner_attempt_id="att-batch-owner"
            )

            self.assertEqual(prior, [])
            self.assertEqual(admission.budget.state, "admit")
            self.assertEqual(admission.budget.verdict_rounds, 0)
            self.assertEqual(admission.budget.verdictless_streak, 0)


class NodeLaunchCommandTest(unittest.TestCase):
    """`--qa` is dispatch-node.py's own option; after `--` it is refused as an override."""

    def command(self, qa):
        leg = {"node": "retrieval", "adapter": "codex", "slug": "s", "attempt_id": "att-x",
               "hop": "same-harness-headless", "ordinal": 0,
               "pair_model_profile": "balanced"}
        return BATCH.node_launch_command(
            route_path=Path("/r.json"), leg=leg, parent="owner", prompt_text="p",
            reviewed_evidence=None, jobs=Path("/jobs.log"), parent_attempt="att-parent",
            log_dir=None, lifecycle="detached", qa=qa)

    def test_qa_goes_before_the_separator_and_passes_the_override_check(self):
        command = self.command("standard")
        cut = command.index("--")
        self.assertIn("--qa", command[:cut])
        self.assertEqual(command[command.index("--qa") + 1], "standard")
        self.assertNotIn("--qa", command[cut:])
        BATCH.DISPATCH_NODE.reject_generated_argument_overrides(command[cut:])

    def test_unset_qa_is_omitted(self):
        self.assertNotIn("--qa", self.command(None))

    def test_pair_profile_hint_is_dispatch_node_owned_before_separator(self):
        command = self.command(None)
        cut = command.index("--")
        self.assertIn("--pair-model-profile", command[:cut])
        self.assertEqual(command[command.index("--pair-model-profile") + 1], "balanced")
        self.assertNotIn("--pair-model-profile", command[cut:])


class SlicesOneCommandTest(unittest.TestCase):
    """`--slices <plan.md>`: one command from a plan's `slices` block to started
    slices, or the ordinary single-session receipt before any admission."""

    FENCE = "`" * 3

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.primary = self.base / "primary"
        self.primary.mkdir()
        self.git("init", "-q", cwd=self.primary)
        self.git("commit", "--allow-empty", "-qm", "init", cwd=self.primary)
        self.route = {
            "route_id": "rt-slices", "route_hash": "sha256:" + "5" * 64, "cwd": str(self.primary),
            "effective_intensity": "standard",
            "nodes": [{"id": "execute", "dispatch_depth": 2, "completion_gate": "code-execute",
                       "kind": "pipeline-stage", "write_scope": ["source/**", "dev_logs/**"],
                       "subdivision": {"min_intensity": "standard", "max_slices": 4,
                                       "disjointness": "exact-fixed-files"}}],
        }
        self.route_path = self.base / "route.json"
        self.route_path.write_text(json.dumps(self.route), encoding="utf-8")
        self.jobs = self.base / "state" / "dispatch" / "jobs.log"
        self.jobs.parent.mkdir(parents=True)
        self.jobs.write_text("", encoding="utf-8")

    @staticmethod
    def git(*args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=cwd, check=True, capture_output=True)

    def plan(self, *blocks: str, name: str = "plan.md") -> Path:
        path = self.base / name
        path.write_text("# plan\n" + "".join(
            f"\n{self.FENCE}slices\n{block}\n{self.FENCE}\n" for block in blocks), encoding="utf-8")
        return path

    GOOD = json.dumps([
        {"id": "a", "files": ["utilities/a.py"], "verify": "true"},
        {"id": "b", "files": ["utilities/b.py"], "verify": "true"},
    ])

    def run_slices(self, plan: Path, *, action="dry-run", extra=(), env=None, admit=None, start=None):
        output = io.StringIO()
        environment = {"AGENT_DISPATCH_SELF_SLUG": "owner", "AGENT_DISPATCH_ATTEMPT_ID": "att-owner-slices"}
        environment.update(env or {})
        dropped = [key for key, value in environment.items() if value is None]
        environment = {key: value for key, value in environment.items() if value is not None}
        admission = BATCH.SUBDIVISION_ADMISSION
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            self.record = stack.enter_context(mock.patch.object(BATCH, "record_degradation"))
            self.reserve = stack.enter_context(mock.patch.object(BATCH, "reserve_batch"))
            self.admit = stack.enter_context(mock.patch.object(
                admission, "admit_batch", side_effect=admit or AssertionError("admit_batch must not run")))
            self.start = stack.enter_context(mock.patch.object(
                admission, "start_admitted_batch", side_effect=start or AssertionError("start must not run")))
            stack.enter_context(mock.patch.dict(os.environ, environment))
            for key in dropped:
                os.environ.pop(key, None)
            argv = ["--route", str(self.route_path), "--parallel-group", "execute", "--slug-prefix", "pe",
                    "--parent", "owner", "--slices", str(plan), "--action", action, *extra]
            with contextlib.redirect_stdout(output), mock.patch.object(sys, "stderr", io.StringIO()):
                rc = BATCH.main(argv)
        return rc, output.getvalue()

    def assert_single_session(self, rc, out, reason, *, ledger):
        self.assertEqual(rc, 0, out)
        receipt = json.loads(out)
        self.assertEqual(receipt["state"], "single-session-required")
        self.assertEqual(receipt["reason"], reason)
        self.assertEqual(receipt["schema_version"], 2)
        self.assertIn("stage-dispatch-fallback.py", receipt["next_action"])
        self.assertIn("--node execute --slug pe --parent owner --start", receipt["next_action"])
        self.admit.assert_not_called()
        self.reserve.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.jobs.read_bytes(), b"")
        self.assertEqual(self.record.call_count, 1 if ledger else 0)
        return receipt

    def test_slices_plan_without_block_prints_single_session_with_next_action(self):
        plan = self.base / "plan.md"
        plan.write_text("# plan\nno slices here\n", encoding="utf-8")
        rc, out = self.run_slices(plan)
        self.assert_single_session(rc, out, "plan-declared-no-slices", ledger=False)
        self.assertFalse((self.jobs.parent / "subdivision").exists())

    def test_slices_malformed_or_duplicate_block_falls_back_without_registration(self):
        for blocks in ((self.GOOD, self.GOOD), ("[not json",), ('{"a": 1}',)):
            with self.subTest(blocks=[b[:12] for b in blocks]):
                rc, out = self.run_slices(self.plan(*blocks))
                receipt = self.assert_single_session(rc, out, "plan-slices-invalid", ledger=True)
                self.assertIn("detail", receipt)

    def test_slices_without_inherited_attempt_id_falls_back_before_admission(self):
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "linked-s", str(linked), cwd=self.primary)
        for extra in ((), ("--worktree", str(linked))):
            with self.subTest(extra=extra):
                rc, out = self.run_slices(self.plan(self.GOOD), extra=extra, env={"AGENT_DISPATCH_ATTEMPT_ID": None})
                self.assert_single_session(rc, out, "parent-attempt-id-absent", ledger=False)

    def test_slices_gates_on_linked_worktree_falls_back_to_single_session(self):
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "linked-s", str(linked), cwd=self.primary)
        rc, out = self.run_slices(
            self.plan(self.GOOD), extra=("--worktree", str(linked)), env={"HEARTING_GATES": "on"})
        receipt = self.assert_single_session(rc, out, "subdivision-disjointness-unproven", ledger=True)
        self.assertIn("plan-slices-worktree-mismatch", receipt["detail"])

    def test_slices_unproven_records_ledger_and_falls_back(self):
        overlap = json.dumps([
            {"id": "a", "files": ["utilities/a.py"], "verify": "true"},
            {"id": "b", "files": ["utilities/a.py"], "verify": "true"},
        ])
        rc, out = self.run_slices(self.plan(overlap))
        receipt = self.assert_single_session(rc, out, "subdivision-disjointness-unproven", ledger=True)
        self.assertIn("parallel-fixed-file-overlap", receipt["detail"])
        self.assertEqual(self.record.call_args.kwargs["reason"], "subdivision-disjointness-unproven")

    def test_slices_plan_builds_manifest_under_state_root_and_admits(self):
        admitted = SimpleNamespace(reservation_identity="r" * 8, sessions=[{}, {}])
        results = [{"started": 1, "registered": 1}, {"started": 1, "registered": 1}]
        plan = self.plan(self.GOOD)
        rc, out = self.run_slices(
            plan, action="start", admit=lambda **kw: admitted, start=lambda *a, **kw: results)
        self.assertEqual(rc, 0, out)
        receipt = json.loads(out)
        self.assertEqual(receipt["state"], "subdivision-batch-started")
        self.assertEqual(receipt["slice_count"], 2)
        manifest_path = Path(receipt["chain_manifest"])
        self.assertEqual(manifest_path.parent, self.jobs.parent / "subdivision" / "rt-slices")
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest["mode"], "parallel")
        self.assertEqual(manifest["worktree"], str(self.primary.resolve()))
        self.assertEqual(self.admit.call_args.kwargs["manifest_path"], manifest_path)
        self.assertIn("capability-route.py complete --route", receipt["next_command"])
        self.assertIn(f"--subsession-manifest {manifest_path}", receipt["next_command"])
        self.assertIn("--node execute", receipt["next_command"])

    def test_next_literal_paths_reach_batch_admission_and_true_globs_do_not(self):
        names = ["app/trips/[id]/page.tsx", "app/[...slug]/page.tsx", "app/(group)/page.tsx"]
        for name in names:
            path = self.primary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("page\n")
        slices = [{"id": "a", "files": names[:2], "verify": "true"},
                  {"id": "b", "files": names[2:], "verify": "true"}]
        admitted = SimpleNamespace(reservation_identity="r" * 8, sessions=[{}, {}])
        rc, out = self.run_slices(self.plan(json.dumps(slices)), action="start",
                                 admit=lambda **kw: admitted,
                                 start=lambda *a, **kw: [{"started": 1}, {"started": 1}])
        self.assertEqual(rc, 0, out)
        receipt = json.loads(out)
        self.assertEqual(receipt["state"], "subdivision-batch-started")
        from stage_session_contract import load_manifest
        manifest = load_manifest(receipt["chain_manifest"], route=self.route, node=self.route["nodes"][0])
        self.assertEqual(manifest["sessions"][0]["fixed_files"],
                         sorted(str(self.primary / name) for name in names[:2]))
        slices[0]["files"] = ["app/*/page.tsx"]
        rc, out = self.run_slices(self.plan(json.dumps(slices)))
        self.assert_single_session(rc, out, "subdivision-disjointness-unproven", ledger=True)
        self.assertIn("fixed-file-must-be-exact", json.loads(out)["detail"])

class PlainSlotReservationTest(unittest.TestCase):
    """F12: a subdivision reserves N plain slots from the real governor, all or none."""

    GOVERNOR = ROOT / "utilities" / "model-worker-governor.py"

    def _reserve(self, root: Path, count: int) -> list[str]:
        return BATCH.reserve_plain_slots(
            self.GOVERNOR, root, [{"attempt_id": f"att-{i}"} for i in range(count)],
            manifest={}, manifest_digest="sha256:" + "0" * 64,
        )

    def test_reserves_n_distinct_plain_tokens(self):
        with tempfile.TemporaryDirectory() as td:
            tokens = self._reserve(Path(td), 3)
            self.assertEqual(len(set(tokens)), 3)
            status = json.loads(subprocess.run(
                [sys.executable, str(self.GOVERNOR), "--root", td, "status"],
                text=True, capture_output=True, check=True).stdout)
            self.assertEqual(len(status["reservations"]), 3)

    def test_shortfall_refuses_all_and_carries_the_governor_detail(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"AGENT_MODEL_WORKER_CLASS_LIMIT_DISPATCH": "2"}):
                with self.assertRaises(BATCH.BatchError) as caught:
                    self._reserve(Path(td), 3)
            self.assertIn("cap reached", str(caught.exception))
            status = json.loads(subprocess.run(
                [sys.executable, str(self.GOVERNOR), "--root", td, "status"],
                text=True, capture_output=True, check=True).stdout)
            self.assertEqual(status["reservations"], {})

    def test_a_dead_wrappers_slots_are_reclaimable_and_a_live_ones_are_not(self):
        with tempfile.TemporaryDirectory() as td:
            mine = self._reserve(Path(td), 1)
            script = (
                "import importlib.util, sys\n"
                f"spec = importlib.util.spec_from_file_location('b', {str(PATH)!r})\n"
                "b = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(b)\n"
                f"print(*b.reserve_plain_slots(__import__('pathlib').Path({str(self.GOVERNOR)!r}),"
                f" __import__('pathlib').Path({td!r}), [{{}}, {{}}]))\n"
            )
            done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                  timeout=120, start_new_session=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            dead = done.stdout.split()
            state = json.loads(Path(td, "state.json").read_text())
            self.assertTrue(all("owner_witness" in state["reservations"][t] for t in mine + dead))
            reclaimed = json.loads(subprocess.run(
                [sys.executable, str(self.GOVERNOR), "--root", td, "reclaim"],
                text=True, capture_output=True, check=True).stdout)
            self.assertEqual(sorted(row["token"] for row in reclaimed["reclaimed"]), sorted(dead))
            self.assertEqual([row["token"] for row in reclaimed["retained"]], mine)


class SelfParentAndForegroundNoticeTest(unittest.TestCase):
    """A mismatched --parent becomes the session's own name with one notice.

    5th Codex run: `gate-off parent-identity-mismatch` read as an error, and
    with gates on it refused. Both batch entry points now use
    AGENT_DISPATCH_SELF_SLUG; a missing session name keeps its refusal.
    """

    # The group fixture only, not DispatchBatchTest's own tests.
    setUp = DispatchBatchTest.setUp
    argv = DispatchBatchTest.argv
    common_patches = DispatchBatchTest.common_patches

    NOTICE = ("notice: --parent wrong-owner is not this session's name; "
              "using owner (from AGENT_DISPATCH_SELF_SLUG).")

    def run_main(self, *, parent="wrong-owner", gates="on", lifecycle=None, self_slug="owner", argv=None, env=None,
                 route_default=None):
        stack, assignments = self.common_patches()
        out, err = io.StringIO(), io.StringIO()
        seen = {}

        def parent_lookup(jobs, **kwargs):
            seen.update(kwargs)
            raise DispatchContractError("parent-attempt-not-found", "fixture stop")

        if argv is None:
            argv = self.argv()
            argv[argv.index("--parent") + 1] = parent
        environment = {**(env or {}),
            "AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture", "HEARTING_GATES": gates,
            "AGENT_DISPATCH_CURRENT_HARNESS": "codex", "AGENT_DISPATCH_CURRENT_TRANSPORT": "headless",
            "AGENT_DISPATCH_CURRENT_SANDBOX": "workspace-write",
        }
        if self_slug:
            environment["AGENT_DISPATCH_SELF_SLUG"] = self_slug
        with stack:
            stack.enter_context(mock.patch.object(BATCH, "load_route", return_value=self.route))
            stack.enter_context(mock.patch.object(BATCH, "assign_harnesses", return_value=(
                assignments, "cross-harness", {"families_considered": [], "usable_families": [],
                                               "family_exclusions": {}, "capacity": {},
                                               "degradation_cause": ""})))
            stack.enter_context(mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base))
            stack.enter_context(mock.patch.object(
                BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)))
            stack.enter_context(mock.patch.object(BATCH, "resolve_live_parent_attempt", side_effect=parent_lookup))
            stack.enter_context(mock.patch.object(BATCH.subprocess, "check_output", return_value=str(self.base)))
            stack.enter_context(mock.patch.object(
                BATCH, "select_launch_lifecycle", return_value=lifecycle or BATCH_LIFECYCLE.DETACHED))
            popen = stack.enter_context(mock.patch.object(BATCH.subprocess, "Popen"))
            if route_default:
                stack.enter_context(mock.patch("owner_route_binding.default_owner_route_file",
                                               return_value=route_default))
            stack.enter_context(mock.patch.dict(os.environ, environment))
            if not self_slug:
                os.environ.pop("AGENT_DISPATCH_SELF_SLUG", None)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = BATCH.main(argv)
        popen.assert_not_called()
        return rc, json.loads(out.getvalue()), err.getvalue(), seen

    def test_an_owner_names_only_the_group_and_start(self):
        rc, _receipt, err, seen = self.run_main(
            argv=["--replica-group", "plan", "--start", "--jobs", str(self.jobs)],
            env={}, route_default=str(self.route_path))
        self.assertEqual(seen["parent_slug"], "owner")
        self.assertNotIn("notice: --parent", err)

    def test_group_start_uses_the_session_name_whatever_the_gates(self):
        for gates in ("on", "off"):
            with self.subTest(gates=gates):
                rc, receipt, err, seen = self.run_main(gates=gates)
                # the fixture stops at the live-parent lookup, after the parent is fixed
                self.assertEqual(receipt["reason"], "parent-attempt-not-found", receipt)
                self.assertEqual(seen["parent_slug"], "owner")
                self.assertEqual(err.splitlines().count(self.NOTICE), 1, err)
                self.assertNotIn("gate-off", err)
                self.assertNotIn("parent-identity-mismatch", json.dumps(receipt) + err)

    def test_group_start_without_a_session_name_keeps_its_refusal(self):
        rc, receipt, err, seen = self.run_main(self_slug=None)
        self.assertEqual(receipt["reason"], "parent-identity-mismatch")
        self.assertEqual(seen, {})
        self.assertNotIn("notice: --parent", err)

    def test_matching_parent_prints_no_notice(self):
        _rc, _receipt, err, seen = self.run_main(parent="owner")
        self.assertEqual(seen["parent_slug"], "owner")
        self.assertNotIn("notice:", err)

    def test_foreground_start_prints_its_notice_first_then_the_parent_notice(self):
        _rc, _receipt, err, _seen = self.run_main(lifecycle=BATCH_LIFECYCLE.FOREGROUND_SCOPED)
        self.assertEqual(err.splitlines()[:2], [BATCH_LIFECYCLE.FOREGROUND_NOTICE, self.NOTICE])

    def test_dry_run_prints_no_foreground_notice(self):
        argv = self.argv("dry-run")
        err = io.StringIO()
        with mock.patch.object(BATCH, "select_launch_lifecycle", return_value=BATCH_LIFECYCLE.FOREGROUND_SCOPED), \
             mock.patch.object(BATCH, "load_route", side_effect=BATCH.BatchError("route-record-invalid", "fixture")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            BATCH.main(argv)
        self.assertNotIn(BATCH_LIFECYCLE.FOREGROUND_NOTICE, err.getvalue())

    def run_subdivision(self, *, gates, self_slug="owner"):
        admission = BATCH.SUBDIVISION_ADMISSION
        args = SimpleNamespace(route=self.route_path, parallel_group="execute", jobs=self.jobs,
                               action="start", parent="wrong-owner", slug_prefix="pe",
                               subdivision_manifest=self.base / "manifest.json", slices=None)
        named = []
        out, err = io.StringIO(), io.StringIO()
        environment = {"AGENT_DISPATCH_ATTEMPT_ID": "att-parent-fixture", "HEARTING_GATES": gates}
        if self_slug:
            environment["AGENT_DISPATCH_SELF_SLUG"] = self_slug
        with mock.patch.object(admission, "route_node", return_value={"id": "execute"}), \
             mock.patch.object(BATCH, "resolve_agent_home", return_value=self.base), \
             mock.patch.object(BATCH, "resolve_global_registry", return_value=SimpleNamespace(path=self.jobs)), \
             mock.patch.object(admission, "raise_if_parallel_entry_fail_closed"), \
             mock.patch.object(admission, "admit_batch",
                               side_effect=admission.SubdivisionAdmissionError("fixture-stop")), \
             mock.patch.object(admission, "single_session_next_action",
                               side_effect=lambda route, node, slug, parent: named.append(parent) or "next"), \
             mock.patch.dict(os.environ, environment), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            if not self_slug:
                os.environ.pop("AGENT_DISPATCH_SELF_SLUG", None)
            try:
                rc = BATCH._run_subdivision_batch_admission(args, {"route_id": "rt-fixture"})
            except BATCH.BatchError as exc:
                return exc.reason, args, named, err.getvalue()
        return rc, args, named, err.getvalue()

    def test_subdivision_admission_uses_the_session_name_whatever_the_gates(self):
        for gates in ("on", "off"):
            with self.subTest(gates=gates):
                rc, args, named, err = self.run_subdivision(gates=gates)
                self.assertEqual(rc, 0)
                self.assertEqual(args.parent, "owner")
                self.assertEqual(named, ["owner"])
                self.assertEqual(err.splitlines().count(self.NOTICE), 1, err)
                self.assertNotIn("gate-off", err)

    def test_subdivision_admission_without_a_session_name_keeps_its_refusal(self):
        reason, args, named, err = self.run_subdivision(gates="on", self_slug=None)
        self.assertEqual(reason, "parent-identity-mismatch")
        self.assertEqual(named, [])


if __name__ == "__main__":
    unittest.main()
