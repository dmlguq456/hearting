#!/usr/bin/env python3
"""Regression for compose-on-demand assembly (utilities/compose-route.py).

Covers the compose -> compile -> verify round trip plus the fail-closed cases a
compose-on-demand caller can hit: unknown unit, gate that no contract backs,
gate that names the wrong unit, ambiguous gate auto-derive, and a gate-evidence
fabrication attempt. The dispatch evidence is a fixture supported-conductor
tuple (the release smoke test keeps a live nested probe), so the round trip
does not depend on live auth.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "utilities" / "compose-route.py"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _load("compose_route", "compose-route.py")
R = _load("route", "capability-route.py")

FIXTURE_EVIDENCE = {
    "tuples": [{
        "parent_harness": "claude", "parent_transport": "headless",
        "parent_sandbox": "adapter-default", "child_harness": "claude",
        "launch_authority": "conductor", "status": "supported",
        "probe_source": "fixture-probe", "probe_time": "2026-07-22T00:00:00Z",
        "failure_class": "",
        "checked_worktree": str(ROOT.resolve()), "failure_scope": "none",
        "codex_command": "not-applicable", "retry_on_isolated_worktree": 0,
    }],
    "native_subagent": [],
}
# A two-node analyze-project compose: research survey feeding a claim reviewer.
UNITS = [
    {"id": "survey", "unit": "research/research-survey",
     "write_scope": ["analysis_project/code/**"], "gate": "research-retrieval"},
    {"id": "claim", "unit": "research/claim-verify", "depends_on": ["survey"],
     "write_scope": ["reviews/claims/**"], "gate": "research-claims"},
]


class TestComposeRoute(unittest.TestCase):
    def _gate_index(self):
        return C.unit_io_gate_index(C._load_topology().load_registry())

    def _run(self, units, *, tracking="tracked", spec_read="canonical-sha",
             workflow_mode="tracked", output=None, capability_mode="code", artifact_root=None,
             slug="Compose Route"):
        """Invoke the compose-route.py CLI as a real caller would."""
        with tempfile.TemporaryDirectory() as tmp:
            evidence_path = Path(tmp) / "evidence.json"
            evidence_path.write_text(json.dumps(FIXTURE_EVIDENCE), encoding="utf-8")
            # D-2: --output must resolve inside <artifact-root>/.runtime/routes. A
            # caller who wants the sealed file to outlive this temp dir passes its
            # own artifact_root (see test_round_trip_seals_and_verifies).
            root = artifact_root if artifact_root is not None else tmp
            command = [
                sys.executable, str(COMPOSE),
                "--capability", "analyze-project", "--capability-mode", capability_mode,
                "--units-json", json.dumps(units),
                "--cwd", str(ROOT), "--artifact-root", root,
                "--tracking", tracking, "--spec-read", spec_read,
                "--drift-verdict", "within-spec", "--workflow-mode", workflow_mode,
                "--artifact-guard", "conductor-prechecked",
                "--dispatch-evidence", str(evidence_path),
                "--cycle-anchor", "analysis_project", "--review-anchor", "reviews",
            ]
            if slug is not None:
                command += ["--slug", slug]
            if output is not None:
                command += ["--output", output]
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            return result

    # --- build_recipe: the assembly logic this tool owns ------------------
    def test_build_recipe_derives_role_kind_gate_and_inputs(self):
        recipe = C.build_recipe(
            "analyze-project", "code", UNITS,
            topology_class="staged", quick_write_scope=[],
            quick_model_profile="balanced-deep", gate_index=self._gate_index(),
            cycle_anchors=["analysis_project"], review_anchor="reviews",
        )
        nodes = {n["id"]: n for n in recipe["standard_plus"]["nodes"]}
        # role is derived from unit frontmatter (never caller-supplied).
        self.assertEqual(nodes["survey"]["role"], "deep maker")
        self.assertEqual(nodes["claim"]["role"], "fast fact-checker")
        # kind is derived from worker_type: stage -> pipeline-stage, review -> review-worker.
        self.assertEqual(nodes["survey"]["kind"], "pipeline-stage")
        self.assertEqual(nodes["claim"]["kind"], "review-worker")
        self.assertEqual(nodes["survey"]["model_profile"], "balanced-deep")
        self.assertEqual(nodes["claim"]["model_profile"], "light")
        # a dependent node inherits its upstream write scope as inputs.
        self.assertEqual(nodes["claim"]["inputs"], ["analysis_project/code/**"])
        # every node is a dispatch-depth-2 unit; the quick block excludes spec scopes.
        self.assertTrue(all(n["dispatch_depth"] == 2 for n in nodes.values()))
        self.assertEqual(recipe["standard_plus"]["max_dispatch_depth"], 2)
        self.assertEqual(recipe["standard_plus"]["owner_dispatch_depth"], 1)
        self.assertEqual(recipe["quick"]["model_profile"], "balanced-deep")

    def test_build_recipe_auto_derives_single_unit_io_gate(self):
        recipe = C.build_recipe(
            "analyze-project", "code",
            [{"id": "review", "unit": "qa/plan-review", "write_scope": ["reviews/plan/**"]}],
            topology_class="staged", quick_write_scope=[],
            quick_model_profile="balanced-deep", gate_index=self._gate_index(),
            cycle_anchors=["analysis_project"], review_anchor="reviews",
        )
        # qa/plan-review names exactly one unit-io gate, so no explicit gate is needed.
        self.assertEqual(recipe["standard_plus"]["nodes"][0]["completion_gate"], "code-plan-check")

    # --- compose -> compile -> verify round trip -------------------------
    def test_round_trip_seals_and_verifies(self):
        with tempfile.TemporaryDirectory() as out_dir:
            result = self._run(UNITS, artifact_root=out_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            route = json.loads(result.stdout)
            self.assertIs(route["composed"], True)
            self.assertEqual(route["effective_intensity"], "standard")
            self.assertEqual(route["capability"], "analyze-project")
            self.assertEqual(route["slug"], "compose-route")
            self.assertFalse(route["slug_truncated"])
            self.assertFalse(route["spec_touch"])
            self.assertEqual([n["id"] for n in route["nodes"]], ["survey", "claim"])
            # the sealed file is byte-identical to stdout and passes verify.
            output = Path(out_dir) / ".runtime" / "routes" / f"{route['route_id']}.json"
            self.assertEqual(json.loads(output.read_text()), route)
            R.verify_route(route, ROOT)

    def test_cli_requires_slug(self):
        result = self._run(UNITS, slug=None)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--slug", result.stderr)

    def test_live_probe_refuses_a_path_other_than_final_route_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            route_cwd = Path(tmp) / "route"
            probe_cwd = Path(tmp) / "probe"
            route_cwd.mkdir()
            probe_cwd.mkdir()
            args = type("Args", (), {
                "dispatch_evidence": None,
                "probe_worktree": str(probe_cwd),
                "cwd": str(route_cwd),
            })()
            with mock.patch.object(C, "probe_child") as probe:
                with self.assertRaisesRegex(ValueError, "final route cwd"):
                    C.assemble_dispatch_evidence(args)
            probe.assert_not_called()

    # --- fail-closed cases -----------------------------------------------
    def test_unknown_unit_fails_closed(self):
        result = self._run([{"id": "x", "unit": "research/does-not-exist",
                             "write_scope": ["analysis_project/code/**"], "gate": "research-retrieval"}])
        self.assertEqual(result.returncode, 64)
        self.assertIn("unknown unit", result.stderr)

    def test_gate_without_contract_fails_closed(self):
        result = self._run([{"id": "x", "unit": "research/research-survey",
                             "write_scope": ["analysis_project/code/**"], "gate": "no-such-gate"}])
        self.assertEqual(result.returncode, 64)
        self.assertIn("completion_gate_contracts", result.stderr)

    def test_gate_naming_wrong_unit_fails_closed_at_compile(self):
        # code-plan-check is a real unit-io gate, but it names qa/plan-review, not the node's unit.
        result = self._run([{"id": "x", "unit": "research/research-survey",
                             "write_scope": ["analysis_project/code/**"], "gate": "code-plan-check"}])
        self.assertEqual(result.returncode, 64)
        self.assertIn("must name the carrying node's unit", result.stderr)

    def test_ambiguous_gate_auto_derive_fails_closed(self):
        # research/research-survey backs five unit-io gates, so auto-derive is refused.
        result = self._run([{"id": "x", "unit": "research/research-survey",
                             "write_scope": ["analysis_project/code/**"]}])
        self.assertEqual(result.returncode, 64)
        self.assertIn("multiple gates", result.stderr)

    def test_unsatisfied_tracked_gate_is_not_fabricated(self):
        # --spec-read false means the tracked spec-read gate is unmet; the tool must
        # pass it through unchanged (never fabricate satisfied), so compile fails closed.
        result = self._run(UNITS, spec_read="false")
        self.assertEqual(result.returncode, 64)
        self.assertIn("spec_read", result.stderr)

    def test_workflow_mode_mismatch_fails_closed(self):
        result = self._run(UNITS, tracking="tracked", workflow_mode="untracked")
        self.assertEqual(result.returncode, 64)

    # --- D-1 (P4-15): a composed recipe with no declared artifact_scope ----
    def test_missing_artifact_scope_anchors_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "artifact_scope"):
            C.build_recipe(
                "analyze-project", "code", UNITS,
                topology_class="staged", quick_write_scope=[],
                quick_model_profile="balanced-deep", gate_index=self._gate_index(),
            )
    def test_missing_map_anchor_for_a_map_worker_node_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "map-worker node requires --map-anchor"):
            C.build_recipe(
                "analyze-project", "code",
                [{"id": "x", "unit": "material/web-image-search", "kind": "map-worker",
                  "write_scope": ["analysis_project/refs/**"], "gate": "design-refs"}],
                topology_class="staged", quick_write_scope=[],
                quick_model_profile="balanced-deep", gate_index=self._gate_index(),
                cycle_anchors=["analysis_project"],
            )

    # --- F11: compose had no way to produce `anchor_mode: literal` ---------
    def test_anchor_mode_literal_without_cycle_anchor_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "requires at least one --cycle-anchor"):
            C.build_recipe(
                "analyze-project", "code", UNITS,
                topology_class="staged", quick_write_scope=[],
                quick_model_profile="balanced-deep", gate_index=self._gate_index(),
                root_anchors=["analysis_project"], anchor_mode="literal",
            )

    def test_anchor_mode_rejects_unknown_value(self):
        with self.assertRaisesRegex(ValueError, "anchor-mode must be implicit or literal"):
            C.build_recipe(
                "analyze-project", "code", UNITS,
                topology_class="staged", quick_write_scope=[],
                quick_model_profile="balanced-deep", gate_index=self._gate_index(),
                cycle_anchors=["analysis_project"], anchor_mode="bogus",
            )

    def test_build_recipe_records_anchor_mode_in_artifact_scope(self):
        recipe = C.build_recipe(
            "analyze-project", "code", UNITS,
            topology_class="staged", quick_write_scope=[],
            quick_model_profile="balanced-deep", gate_index=self._gate_index(),
            cycle_anchors=["analysis_project"], review_anchor="reviews",
            anchor_mode="literal",
        )
        self.assertEqual(recipe["artifact_scope"]["anchor_mode"], "literal")

    def test_composed_recipe_anchor_mode_literal_round_trips_through_compile(self):
        # The full compile path runs TOPO._validate_recipe against the SAME
        # registry-declared artifact_buckets/anchor checks an enumerated
        # recipe gets -- a scope that does not carry the declared literal
        # prefix must fail closed here, not silently pass as "implicit" bare.
        literal_units = [
            {"id": "survey", "unit": "research/research-survey",
             "write_scope": ["analysis_project/code/**"], "gate": "research-retrieval"},
            {"id": "claim", "unit": "research/claim-verify", "depends_on": ["survey"],
             "write_scope": ["analysis_project/reviews/**"], "gate": "research-claims"},
        ]
        with tempfile.TemporaryDirectory() as out_dir:
            command_extra = ["--anchor-mode", "literal"]
            result = self._run_with_anchor_mode(literal_units, None, out_dir, command_extra)
            self.assertEqual(result.returncode, 0, result.stderr)

        mismatched_units = [
            {"id": "survey", "unit": "research/research-survey",
             "write_scope": ["analysis_project/code/**"], "gate": "research-retrieval"},
            {"id": "claim", "unit": "research/claim-verify", "depends_on": ["survey"],
             "write_scope": ["reviews/claims/**"], "gate": "research-claims"},
        ]
        with tempfile.TemporaryDirectory() as out_dir:
            command_extra = ["--anchor-mode", "literal"]
            result = self._run_with_anchor_mode(mismatched_units, None, out_dir, command_extra)
            self.assertEqual(result.returncode, 64, result.stdout)
            self.assertIn("matches no declared cycle_anchor", result.stderr)

    def _run_with_anchor_mode(self, units, output, artifact_root, extra_args):
        with tempfile.TemporaryDirectory() as tmp:
            evidence_path = Path(tmp) / "evidence.json"
            evidence_path.write_text(json.dumps(FIXTURE_EVIDENCE), encoding="utf-8")
            command = [
                sys.executable, str(COMPOSE),
                "--capability", "analyze-project", "--capability-mode", "code",
                "--slug", "Compose Route",
                "--units-json", json.dumps(units),
                "--cwd", str(ROOT), "--artifact-root", artifact_root,
                "--tracking", "tracked", "--spec-read", "canonical-sha",
                "--drift-verdict", "within-spec", "--workflow-mode", "tracked",
                "--artifact-guard", "conductor-prechecked",
                "--dispatch-evidence", str(evidence_path),
                "--cycle-anchor", "analysis_project", "--review-anchor", "reviews",
            ] + extra_args
            if output is not None:
                command += ["--output", output]
            return subprocess.run(command, text=True, capture_output=True, check=False)


if __name__ == "__main__":
    unittest.main()
