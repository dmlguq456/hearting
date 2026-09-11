#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

import worker_bootstrap as W

ROOT = Path(__file__).resolve().parents[1]


class WorkerBootstrapTest(unittest.TestCase):
    def test_issued_cycle_context_supplies_missing_output_directory(self):
        env = {"AGENT_ARTIFACT_CYCLE_ID": "cyc-test", "AGENT_ARTIFACT_CYCLE_DIR": "/issued/cycle"}
        values = W.artifact_cycle_environment(env)
        self.assertEqual(values["AGENT_ARTIFACT_OUTPUT_DIR"], "/issued/cycle/artifacts")
        self.assertIn("/issued/cycle/artifacts", W.artifact_context_prompt(env))
        self.assertNotIn("AGENT_ARTIFACT_OUTPUT_DIR", env)
        self.assertEqual(W.artifact_context_prompt({}), "")

    def test_deterministic_fallback_types(self):
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=1), "owner")
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="test"),
            "stage",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, route_node="plan-review"),
            "review",
        )
        self.assertEqual(W.resolve_worker_type(explicit=None, dispatch_depth=2), "support")

    def test_worker_role_is_only_a_legacy_fallback(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="support",
                dispatch_depth=2,
                worker_role="external-adversary",
                route_node="review",
            ),
            "support",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=2, worker_role="code-test"),
            "stage",
        )

    def test_explicit_and_profile_precedence(self):
        self.assertEqual(
            W.resolve_worker_type(
                explicit="review", dispatch_depth=1, profile_type="stage"
            ),
            "review",
        )
        self.assertEqual(
            W.resolve_worker_type(explicit=None, dispatch_depth=1, profile_type="support"),
            "support",
        )

    def test_explicit_frame_beats_depth_one_owner_fallback(self):
        # frame-universal (2026-09-10): a depth-1 frame node's explicit
        # worker_type must win over the dispatch_depth==1 "owner" fallback --
        # the same precedence already pinned for review at depth 1 above, now
        # pinned for frame too, so a depth-1 frame node is never silently
        # resolved away from "frame" (here to "owner"; elsewhere, if it ever
        # reached the unrelated topology-kind fallback, to "support").
        self.assertEqual(
            W.resolve_worker_type(explicit="frame", dispatch_depth=1),
            "frame",
        )
        self.assertEqual(
            W.resolve_worker_type(
                explicit="frame", dispatch_depth=1, worker_role="map-worker",
                route_node="frame",
            ),
            "frame",
        )

    def test_render_has_one_kernel_one_type_and_exact_handoff(self):
        rendered = W.render_worker_bootstrap(ROOT, "stage")
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)
        self.assertIn(W.handoff_template(), rendered)
        self.assertIn("has no Markdown fence", rendered)
        self.assertNotIn("```text\nartifact:", rendered)
        self.assertNotIn("# Worker Type: Owner", rendered)
        self.assertNotIn("# Worker Type: Review", rendered)

    def test_ordinary_worker_does_not_receive_chain_bookkeeping(self):
        for worker_type in W.WORKER_TYPES:
            text=W.render_worker_bootstrap(ROOT,worker_type)
            self.assertNotIn("after at most three",text)
            self.assertNotIn("Native helper support",text)
            self.assertNotIn("dispatch_subsession_handoff.py",text)
        self.assertIn("No per-tool heartbeat",W.runtime_progress_prompt())

    def test_render_appends_unit_persona_body(self):
        bare = W.render_worker_bootstrap(ROOT, "review")
        rendered = W.render_worker_bootstrap(ROOT, "review", unit="qa/code-review")
        self.assertTrue(rendered.startswith(bare.rstrip("\n")))
        self.assertIn("# Unit: qa/code-review", rendered)
        self.assertNotIn("worker_type: review\n", rendered)  # frontmatter stripped
        self.assertEqual(rendered.count("# Portable Worker Kernel"), 1)
        self.assertEqual(rendered.count("# Worker Type:"), 1)

    def test_reserved_and_absent_units_render_unchanged(self):
        bare = W.render_worker_bootstrap(ROOT, "owner")
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit="_kernel/owner"), bare)
        self.assertEqual(W.render_worker_bootstrap(ROOT, "owner", unit=None), bare)
        self.assertIsNone(W.unit_persona_path(ROOT, "_kernel/resource"))
        self.assertIsNone(W.unit_persona_body(ROOT, None))

    def test_unknown_or_malformed_unit_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "unknown unit"):
            W.unit_persona_path(ROOT, "qa/does-not-exist")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.unit_persona_path(ROOT, "../escape/path")
        with self.assertRaisesRegex(ValueError, "invalid unit ref"):
            W.render_worker_bootstrap(ROOT, "stage", unit="Bad/Unit")

    def test_unit_persona_path_points_into_catalog(self):
        path = W.unit_persona_path(ROOT, "editorial/report")
        self.assertEqual(path, ROOT / "roles" / "units" / "editorial" / "report.md")
        body = W.unit_persona_body(ROOT, "editorial/report")
        self.assertTrue(body.startswith("# Unit: editorial/report"))

    def test_profile_type_scalar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles").mkdir()
            (root / "profiles" / "x.yaml").write_text(
                "name: x\nworker_type: review\n", encoding="utf-8"
            )
            self.assertEqual(W.profile_worker_type(root, "x"), "review")

    def test_assigned_stage_contract(self):
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="stage",
                route_node="test",
                completion_gate="code-test",
                root=ROOT,
            ),
            "code-test",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-code",
                worker_type="owner",
                route_node=None,
                root=ROOT,
            ),
            "autopilot-code",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-design",
                worker_type="support",
                route_node="refs",
                completion_gate="design-refs",
                root=ROOT,
            ),
            "design-refs",
        )
        self.assertEqual(
            W.assigned_contract(
                capability="autopilot-spec",
                worker_type="support",
                route_node="research",
                completion_gate="spec-research",
                root=ROOT,
            ),
            "autopilot-spec",
        )

    def test_topology_kind_maps_only_to_bootstrap_type(self):
        self.assertEqual(W.worker_type_for_kind("capability-owner"), "owner")
        self.assertEqual(W.worker_type_for_kind("pipeline-stage"), "stage")
        self.assertEqual(W.worker_type_for_kind("review-worker"), "review")
        self.assertEqual(W.worker_type_for_kind("map-worker"), "support")
        with self.assertRaises(ValueError):
            W.worker_type_for_kind("resource-runner")


if __name__ == "__main__":
    unittest.main()
