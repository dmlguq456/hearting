#!/usr/bin/env python3
"""2026-11-23: `top` for a person-named stage node or review/verification leg at strong and above.

The route compiler accepts it (and seals it per node), `verify_route` re-derives the rule, the
`tiers:` line shows the owner tier, and nothing ever PRODUCES `top` on its own: every default,
matrix cell, policy band, pair rotation, capacity cascade and resume keeps it out. Fixtures never
launch a model."""
import os, sys  # noqa: E401
if __name__ == "__main__" and not os.environ.get("HEARTING_TEST_ISOLATED"):  # a direct run re-enters under tools/run-tests.py isolation
    os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../tools/run-tests.py"), "--direct-run", sys.executable, *sys.orig_argv[1:]])
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utilities"))
import model_config as C
import model_profile as P
import replica_batch_contract as B


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "utilities" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


F = load("route_fixtures", "capability_route.test.py")
R = F.R
BATCH = load("dispatch_batch_under_test", "dispatch-batch.py")
import worker_bootstrap as WB
_CLAUDE = importlib.util.spec_from_file_location(
    "claude_dispatch_headless_under_test", ROOT / "adapters" / "claude" / "bin" / "dispatch-headless.py")
CLAUDE = importlib.util.module_from_spec(_CLAUDE)
_CLAUDE.loader.exec_module(CLAUDE)
INTENSITIES = ("quick", "standard", "strong", "thorough", "adversarial")
TOP_STAGE = ("strong", "thorough", "adversarial")


class Fixture(unittest.TestCase):
    args = F.TestRoute.args
    dispatch = F.TestRoute.dispatch
    nested = F.TestRoute.nested

    def staged(self, capability="autopilot-code", mode="dev", intensity="adversarial", **kw):
        return R.compile_route(**self.args(
            capability=capability, capability_mode=mode, requested_intensity=intensity, predicates=[],
            transport="headless", inline_reason=None, dispatch_evidence=self.dispatch(self.nested()), **kw))

    def tops(self, route):
        """The non-frame nodes holding top (the frame pair holds it by its own default)."""
        return sorted(n["id"] for n in route["nodes"] if n.get("model_profile") == "top" and not R._frame_node(n))


class ComposeAcceptsTopStageNodes(Fixture):
    AUDIT = dict(capability="audit", mode="default", field="docs")

    def test_the_originating_request_is_accepted_at_strong_and_above_and_refused_below_with_the_rule(self):
        # audit / docs / the three keys: the owner, the investigating stage, the reporting stage
        keys = {"__owner__": "top", "inspect": "top", "report": "top"}
        for intensity in TOP_STAGE:
            with self.subTest(intensity=intensity):
                route = self.staged(intensity=intensity, explicit_profiles=keys, **self.AUDIT)
                R.verify_route(route, R.ROOT)
                self.assertEqual(route["owner_model_profile"], "top")
                self.assertEqual(self.tops(route), ["inspect", "report"])
                for node in route["nodes"]:
                    self.assertEqual(node["profile_selection"]["source"], "explicit")
                    self.assertEqual(node["profile_selection"]["reason"], "explicit-profile-choice")
        route = self.staged(intensity="standard", explicit_profiles={"__owner__": "top"}, **self.AUDIT)
        self.assertEqual(route["owner_model_profile"], "top")   # the owner may always
        self.assertEqual(self.tops(route), [])
        with self.assertRaises(ValueError) as refused:
            self.staged(intensity="standard", explicit_profiles=keys, **self.AUDIT)
        text = str(refused.exception)
        self.assertTrue(text.startswith("profile-explicit-top-owner-only:inspect: "), text)
        self.assertIn("this route is standard", text)
        self.assertIn("below strong", text)
        self.assertIn("can take `top` at any intensity", text)

    def test_only_the_named_nodes_hold_top_and_nothing_else_moves(self):
        plain = self.staged(**self.AUDIT)
        route = self.staged(explicit_profiles={"inspect": "top"}, **self.AUDIT)
        self.assertEqual(self.tops(route), ["inspect"])
        self.assertEqual(route["owner_model_profile"], plain["owner_model_profile"])
        by = {n["id"]: n["model_profile"] for n in plain["nodes"]}
        for node in route["nodes"]:
            if node["id"] != "inspect":
                self.assertEqual(node["model_profile"], by[node["id"]], node["id"])

    def test_a_review_leg_is_named_by_its_realized_id_and_one_leg_per_group(self):
        legs = self.staged("autopilot-code", "dev", "adversarial")
        group = [n["id"] for n in legs["nodes"] if n.get("parallel_group") == "impl-review"]
        self.assertEqual(group, ["impl-review", "impl-review-alternative", "impl-review-failure-mode"])
        for leg in group:
            with self.subTest(leg=leg):
                route = self.staged("autopilot-code", "dev", "adversarial", explicit_profiles={leg: "top"})
                R.verify_route(route, R.ROOT)
                self.assertEqual(self.tops(route), [leg])
                self.assertEqual(sum(1 for n in route["nodes"] if n.get("parallel_group") == "impl-review"
                                     and n["model_profile"] == "top"), 1)
        with self.assertRaises(ValueError) as wide:
            self.staged("autopilot-code", "dev", "adversarial",
                        explicit_profiles={"impl-review": "top", "impl-review-alternative": "top"})
        self.assertTrue(str(wide.exception).startswith("profile-explicit-top-group-width:impl-review: "))
        # the group-width cap is the batch contract's own (one top leg), not a second number
        self.assertEqual(B.MAX_TOP_LEGS, 1)
        # a leg a lower intensity never realizes is not a node at all
        with self.assertRaisesRegex(ValueError, "profile-input-unknown-node"):
            self.staged("autopilot-code", "dev", "standard", explicit_profiles={"impl-review-alternative": "top"})

    def test_the_frame_pair_and_the_owner_keep_their_behavior_at_every_intensity(self):
        for intensity in ("standard", "strong", "adversarial"):
            with self.subTest(intensity=intensity):
                route = self.staged("autopilot-code", "dev", intensity, explicit_profiles={"__owner__": "top"})
                self.assertEqual(route["owner_model_profile"], "top")
                self.assertEqual(sorted(n["id"] for n in route["nodes"] if n["model_profile"] == "top"),
                                 ["frame", "frame-alternative"])
                one = self.staged("autopilot-code", "dev", intensity, explicit_profiles={"frame-alternative": "light"})
                self.assertEqual(one["owner_model_profile"], "deep")
                R.verify_route(route, R.ROOT)

    def test_a_depth_one_node_that_is_neither_owner_nor_frame_is_refused_even_at_adversarial(self):
        nodes = [{"id": "frame", "dispatch_depth": 1, "worker_type": "frame", "model_profile": "deep"},
                 {"id": "prd-transaction", "dispatch_depth": 1, "worker_type": "owner", "kind": "capability-owner",
                  "model_profile": "light"},
                 {"id": "execute", "dispatch_depth": 2, "kind": "pipeline-stage", "model_profile": "light"}]
        for key in ("prd-transaction",):
            with self.assertRaises(ValueError) as refused:
                R._profile_input_maps(nodes, None, {key: "top"}, "adversarial")
            self.assertTrue(str(refused.exception).startswith("profile-explicit-top-owner-only:prd-transaction: "))
            # the cause is the node's kind and depth, not the intensity, and the owner's key is named
            self.assertIn("a step the owner runs itself", str(refused.exception))
            self.assertIn("`__owner__`", str(refused.exception))
            self.assertNotIn("this route is", str(refused.exception))
        self.assertEqual(R._profile_input_maps(nodes, None, {"execute": "top"}, "adversarial")[1], {"execute": "top"})
        for intensity in (None, "", "direct", "quick", "standard"):
            with self.assertRaises(ValueError):
                R._profile_input_maps(nodes, None, {"execute": "top"}, intensity)

    def test_the_route_wide_profile_choice_never_offers_top(self):
        self.assertNotIn("top", P.PORTABLE_PROFILES)
        for script in ("capability-route.py", "compose-route.py"):
            out = subprocess.run([sys.executable, str(ROOT / "utilities" / script), "compose", "--profile", "top"],
                                 capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
            self.assertNotEqual(out.returncode, 0, script)
        with self.assertRaisesRegex(ValueError, "profile-explicit-unknown"):
            self.staged("autopilot-code", "dev", "adversarial", profile="top")


class ComposeAndLaunchShareOnePredicate(Fixture):
    """Every node compose accepts `top` on is one the launch path accepts, and the reverse for the
    kinds launch refuses. The launch side is the wrapper's own model selection, called the way the
    launchers call it: `worker_type` from the node's kind (`worker_type_for_kind`)."""

    def setUp(self):
        import tempfile
        from unittest import mock
        from types import SimpleNamespace
        self.tmp = Path(tempfile.mkdtemp(prefix="top-matrix-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        policy = C.parse_config(ROOT / "adapters" / "claude" / "config" / "models.conf") \
            if hasattr(C, "parse_config") else None
        from model_config import parse_config
        patcher = mock.patch.object(CLAUDE, "_model_policy",
                                    side_effect=lambda: parse_config(ROOT / "adapters" / "claude" / "config" / "models.conf"))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"AGENT_HOME": str(ROOT), "HEARTING_MODEL_CONFIG_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.Namespace = SimpleNamespace

    def launch(self, route, node):
        """The model selection a launcher's argv reaches for this node of this sealed route."""
        path = self.tmp / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        args = self.Namespace(
            inherit_model_settings=False, model_profile="top", model_role="deep reviewer", model=None, effort=None,
            registered_worker=1, dispatch_depth=node["dispatch_depth"], worker_type=WB.worker_type_for_kind(node["kind"]),
            capacity_retry=0, route_file=str(path), route_node=node["id"])
        try:
            CLAUDE.resolve_model_settings(args)
            return None
        except CLAUDE.ModelSelectionError as exc:
            return exc.reason

    _rows = None

    def matrix(self, kinds=None):
        """(capability, mode, intensity, node id, kind, compose accepted, launch refusal reason)."""
        if kinds is None and ComposeAndLaunchShareOnePredicate._rows is not None:
            return ComposeAndLaunchShareOnePredicate._rows
        rows = self._matrix(kinds)
        if kinds is None:
            ComposeAndLaunchShareOnePredicate._rows = rows
        return rows

    def _matrix(self, kinds):
        registry = R.TOPO.load_registry()
        rows = []
        for recipe in registry["recipes"]:
            if recipe["capability"] in R.TOPO.COMPILER_INTERNAL_CAPABILITIES:
                continue
            extra = {"field": recipe["field"]} if recipe.get("field") else {}
            for intensity in TOP_STAGE:
                try:
                    base = self.staged(recipe["capability"], recipe["modes"][0], intensity, **extra)
                except ValueError:
                    continue
                for node in base["nodes"]:
                    if node.get("dispatch_depth") != 2 or R._no_model_node(node) or (
                            kinds is not None and node["kind"] not in kinds):
                        continue
                    try:
                        route = self.staged(recipe["capability"], recipe["modes"][0], intensity,
                                            explicit_profiles={node["id"]: "top"}, **extra)
                        R.verify_route(route, R.ROOT)
                    except ValueError:
                        rows.append((recipe["capability"], intensity, node["id"], node["kind"], False, None))
                        continue
                    sealed = next(n for n in route["nodes"] if n["id"] == node["id"])
                    rows.append((recipe["capability"], intensity, node["id"], node["kind"], True,
                                 self.launch(route, sealed)))
        return rows

    def test_every_node_compose_accepts_top_on_launches_and_fan_out_legs_are_refused_at_compose(self):
        rows = self.matrix()
        accepted = [r for r in rows if r[4]]
        self.assertGreater(len(accepted), 30)
        self.assertEqual([r for r in accepted if r[5] is not None], [])   # accepted at compose -> passes launch
        for row in rows:
            if row[3] == "map-worker":
                self.assertFalse(row[4], row)   # a fan-out leg launches as `support`: refused at compose
        self.assertTrue({"pipeline-stage", "review-worker"} <= {r[3] for r in accepted})
        self.assertEqual({r[3] for r in accepted} - {"pipeline-stage", "review-worker"}, set())
        self.assertTrue(any(r[3] == "map-worker" for r in rows))

    def test_the_old_depth_only_predicate_would_leave_cells_launch_refuses(self):
        from unittest import mock
        depth_only = lambda node: isinstance(node, dict) and node.get("dispatch_depth") == 2
        with mock.patch.object(P, "top_stage_node", depth_only):
            rows = self.matrix(kinds={"map-worker"})
        torn = [r for r in rows if r[4] and r[5] is not None]
        self.assertGreater(len(torn), 0)
        self.assertEqual({r[3] for r in torn}, {"map-worker"})
        self.assertEqual({r[5] for r in torn}, {"invalid-dispatch-model-profile"})
        self.assertEqual(len(torn), len(rows))   # every fan-out cell the old predicate took is one launch refuses
        report = os.environ.get("HEARTING_TOP_MATRIX_REPORT")
        if report:   # numbers for the change record: the depth-only predicate against the shared one
            with mock.patch.object(P, "top_stage_node", depth_only):
                old = self.matrix(kinds=set(WB.WORKER_KIND_TYPES))
            new = self.matrix()
            Path(report).write_text(json.dumps({
                "before_accepted": sum(1 for r in old if r[4]), "before_mismatch": sum(1 for r in old if r[4] and r[5]),
                "after_accepted": sum(1 for r in new if r[4]), "after_mismatch": sum(1 for r in new if r[4] and r[5]),
                "before_nodes": sorted({(r[0], r[3]) for r in old if r[4] and r[5]}),
                "cells": len(new)}), encoding="utf-8")

    def test_a_fan_out_node_is_refused_at_compose_and_the_text_names_its_kind(self):
        with self.assertRaises(ValueError) as refused:
            self.staged("autopilot-research", "academic", "strong", explicit_profiles={"retrieval": "top"})
        text = str(refused.exception)
        self.assertTrue(text.startswith("profile-explicit-top-owner-only:retrieval: "), text)
        self.assertIn("map-worker", text)
        self.assertIn("support worker", text)
        self.assertNotIn("this route is", text)        # the cause is the node's kind, not the intensity
        for intensity in ("thorough", "adversarial"):
            with self.assertRaises(ValueError):
                self.staged("autopilot-research", "academic", intensity, explicit_profiles={"retrieval": "top"})
        # a stage worker of the same route still takes it
        route = self.staged("autopilot-research", "academic", "strong", explicit_profiles={"report": "top"}) \
            if any(n["id"] == "report" for n in self.staged("autopilot-research", "academic", "strong")["nodes"]) else None
        if route is not None:
            R.verify_route(route, R.ROOT)

    def test_a_hand_sealed_fan_out_top_is_refused_by_verify_and_by_launch(self):
        good = self.staged("autopilot-research", "academic", "strong", explicit_profiles={"__owner__": "deep"})
        forged = json.loads(json.dumps(good))
        forged["explicit_profiles"] = {**forged.get("explicit_profiles", {}), "retrieval": "top"}
        node = next(n for n in forged["nodes"] if n["id"] == "retrieval")
        node["model_profile"] = "top"
        node["profile_selection"] = {**node["profile_selection"], "source": "explicit",
                                     "reason": "explicit-profile-choice", "resolved_profile": "top"}
        forged["route_hash"] = R.route_hash(forged)
        forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
        with self.assertRaises(ValueError):
            R.verify_route(forged, R.ROOT)
        self.assertEqual(self.launch(forged, node), "invalid-dispatch-model-profile")

    def test_a_route_with_no_selection_contract_cannot_hold_a_depth_two_top(self):
        good = self.staged("autopilot-code", "dev", "adversarial", explicit_profiles={"execute": "top"})
        R.verify_route(good, R.ROOT)
        legacy = json.loads(json.dumps(good))
        for key in ("profile_selection_contract_version", "profile_demands", "explicit_profiles",
                    "owner_profile_selection", "owner_profile_demand"):
            legacy.pop(key, None)
        for node in legacy["nodes"]:
            node.pop("profile_selection", None)
            node.pop("profile_demand", None)
        legacy["route_hash"] = R.route_hash(legacy)
        legacy["route_id"] = "rt-" + legacy["route_hash"].split(":", 1)[1][:16]
        with self.assertRaises(ValueError) as refused:
            R.verify_route(legacy, R.ROOT)
        self.assertIn("top", str(refused.exception))
        # the same shape without the depth-2 top is not refused for that reason
        with self.assertRaises(ValueError) as probe:
            R._verify_profile_contract({"nodes": [{"id": "execute", "dispatch_depth": 2, "model_profile": "top"}]})
        self.assertTrue(str(probe.exception).startswith("profile-top-needs-selection-contract:execute"))
        R._verify_profile_contract({"nodes": [{"id": "execute", "dispatch_depth": 2, "model_profile": "deep"}]})


class VerifyReDerivesTheRule(Fixture):
    def test_a_forged_top_stage_node_is_refused_by_verify(self):
        good = self.staged("autopilot-code", "dev", "adversarial", explicit_profiles={"execute": "top"})
        R.verify_route(good, R.ROOT)

        def reseal(route):
            route = json.loads(json.dumps(route))
            route["route_hash"] = R.route_hash(route)
            route["route_id"] = "rt-" + route["route_hash"].split(":", 1)[1][:16]
            return route

        forgeries = {}
        # top on a node the person never named
        plain = self.staged("autopilot-code", "dev", "adversarial")
        unnamed = json.loads(json.dumps(plain))
        next(n for n in unnamed["nodes"] if n["id"] == "test")["model_profile"] = "top"
        forgeries["a node the person did not name"] = unnamed
        # the named route claimed at a lower intensity
        lower = json.loads(json.dumps(good))
        lower["effective_intensity"] = "standard"
        forgeries["a route that says standard"] = lower
        # the explicit map dropped but the seal kept
        dropped = json.loads(json.dumps(good))
        dropped["explicit_profiles"] = {}
        forgeries["an explicit map that no longer names it"] = dropped
        for label, route in forgeries.items():
            with self.subTest(label), self.assertRaises(ValueError):
                R.verify_route(reseal(route), R.ROOT)


class TiersLineShowsTheOwner(Fixture):
    def tiers(self, route):
        return next(line for line in R.compose_decision_lines(route) if "tiers:" in line).strip()

    def test_the_owner_tier_leads_the_line_and_a_leg_shows_inside_its_group(self):
        route = self.staged(capability="audit", mode="default", field="docs",
                            explicit_profiles={"__owner__": "top", "inspect": "top", "report": "top"})
        self.assertEqual(self.tiers(route), "tiers: owner=top inspect=top report=top")
        plain = self.staged(capability="audit", mode="default", field="docs")
        self.assertEqual(self.tiers(plain), f"tiers: owner={plain['owner_model_profile']} inspect=light report=balanced-deep")
        leg = self.staged("autopilot-code", "dev", "adversarial", explicit_profiles={"impl-review-alternative": "top"})
        self.assertIn("impl-review=balanced-deep+top+deep", self.tiers(leg))
        self.assertTrue(self.tiers(leg).startswith("tiers: owner=deep frame=top"))

    def test_a_one_shot_route_lists_its_owner_once(self):
        quick = R.compile_route(**self.args(predicates=[], transport=None, inline_reason=None,
                                            registered_headless_evidence=F.TestRoute.registered_headless(self)))
        line = self.tiers(quick)
        self.assertEqual(line.count("one-shot="), 1)
        self.assertNotIn("owner=", line)

    def test_the_help_line_names_the_keys_and_the_condition(self):
        for script in ("capability-route.py", "compose-route.py"):
            out = subprocess.run([sys.executable, str(ROOT / "utilities" / script), "compose", "--help"],
                                 capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout
            flat = " ".join(out.split())
            self.assertIn("--explicit-profiles", flat, script)
            self.assertIn("__owner__", flat, script)
            self.assertIn("top is for the owner at any intensity, or for a depth-2 stage worker or "
                          "review/verification leg you name at strong or higher", flat, script)
            self.assertIn("one top leg per parallel group", flat, script)
            self.assertIn("a route-wide worker pin's model still wins", flat, script)
        # `--intensity` (the strong-or-higher switch) is on the default --help, not only on --help-all
        out = subprocess.run([sys.executable, str(ROOT / "utilities" / "capability-route.py"), "compose", "--help"],
                             capture_output=True, text=True, env={"PATH": os.environ["PATH"]}).stdout
        self.assertIn("--intensity", out)


class NothingProducesTopOnItsOwn(Fixture):
    """The sites that could put `top` on a node without a person naming it, one by one."""

    def test_no_default_route_holds_top_beyond_the_frame_pair(self):
        registry = R.TOPO.load_registry()
        compiled = 0
        for recipe in registry["recipes"]:
            if recipe["capability"] in R.TOPO.COMPILER_INTERNAL_CAPABILITIES:
                continue
            for intensity in ("standard", "strong", "thorough", "adversarial"):
                try:
                    route = self.staged(recipe["capability"], recipe["modes"][0], intensity)
                except ValueError:
                    continue   # a recipe this fixture's evidence cannot compile
                compiled += 1
                self.assertNotEqual(route["owner_model_profile"], "top", (recipe["capability"], intensity))
                for node in route["nodes"]:
                    if node.get("model_profile") == "top":
                        self.assertTrue(R._frame_node(node), (recipe["capability"], intensity, node["id"]))
        self.assertGreater(compiled, 20)

    def test_a_route_wide_profile_and_a_capacity_or_report_graph_never_add_top(self):
        for profile in P.PORTABLE_PROFILES:
            if profile == "mini":
                continue
            route = self.staged("autopilot-code", "dev", "adversarial", profile=profile)
            self.assertEqual([n["id"] for n in route["nodes"] if n["model_profile"] == "top"], [])
        # a per-node choice names the node and nothing wider
        route = self.staged("autopilot-code", "dev", "adversarial", profile="deep",
                            explicit_profiles={"test": "top"})
        self.assertEqual(self.tops(route), ["test"])

    def test_the_registry_the_matrix_the_floors_and_the_policy_bands_never_name_top(self):
        registry = R.TOPO.load_registry()
        self.assertNotIn("top", registry["model_profiles"])
        self.assertNotIn("top", registry["owner_profile_by_intensity"].values())
        for recipe in registry["recipes"]:
            for node in recipe["standard_plus"]["nodes"]:
                if not R._frame_node(node):   # a frame leg's declared profile is a placeholder the ladder stamps
                    self.assertNotEqual(node.get("model_profile"), "top", (recipe["capability"], node["id"]))
            for group in recipe["standard_plus"].get("parallel_groups", []):
                for leg in group["legs"]:
                    self.assertNotEqual(leg["model_profile"], "top", (recipe["capability"], group["id"]))
        self.assertNotIn("top", P.MATRIX.values())
        self.assertNotIn("top", [p for floor in P.JUDGMENT_FLOORS.values() for p in floor])
        D = load("defaults_under_test", "dispatch-defaults.py")
        self.assertNotIn("top", D.MODEL_PROFILES)
        self.assertEqual(D.EXCEPTION_PROFILE_POLICY, {"top": "deep"})

    def test_pair_rotation_never_enters_top_and_never_lowers_a_top_leg(self):
        for adapter in C.ADAPTERS:
            config, _ = C.resolve_config(adapter, source_root=ROOT)
            for own in ("top", "deep", "balanced-deep", "balanced", "light"):
                for sibling in {P.resolve_profile_values(adapter, config, name)["model"]
                                for name in ("deep", "balanced-deep", "balanced", "light")}:
                    proposal = P.pair_model_rotation(adapter, config, own, sibling)
                    if proposal is not None:
                        self.assertNotIn(proposal["profile"], {"top", "mini"}, (adapter, own, sibling))
        # a width-two pair holding a top leg: the proposal never names the top leg, even on a tie
        for top_first in (True, False):
            top = ({"id": "review", "model_profile": "top", "parallel_leg_index": 0}, "opencode", "same", 1)
            other = ({"id": "review-alternative", "model_profile": "balanced-deep", "parallel_leg_index": 1},
                     "opencode", "same", 1)
            pair = [top, other] if top_first else [other, top]
            profiles, diagnostic = BATCH._pair_model_proposals(pair)
            self.assertNotIn("review", profiles, diagnostic)
            self.assertEqual([row[0]["model_profile"] for row in pair],
                             ["top", "balanced-deep"] if top_first else ["balanced-deep", "top"])

    def test_repeat_escalation_neither_climbs_into_top_nor_moves_a_top_node_up(self):
        from unittest import mock
        self.assertNotIn("top", P.ESCALATION_ORDER)
        route = {"route_id": "rt-x", "nodes": [{"id": "execute", "kind": "pipeline-stage", "unit": "dev/backend",
                                                "dispatch_depth": 2, "model_profile": "top"}]}
        repeat = {"streak": 3, "class": "other", "signature": ("FAIL", "other", ()), "attempts": ["a", "b", "c"]}
        with mock.patch("worker_bootstrap.repeat_failure", return_value=repeat):
            self.assertIsNone(P.repeat_escalation(route, "execute", "/tmp/jobs.log"))
            deep = {"route_id": "rt-x", "nodes": [dict(route["nodes"][0], model_profile="deep")]}
            for streak in (2, 3, 4, 5):
                repeat["streak"] = streak
                result = P.repeat_escalation(deep, "execute", "/tmp/jobs.log")
                self.assertNotEqual((result or {}).get("to"), "top")
                self.assertNotEqual((result or {}).get("launch_profile"), "top")

    def test_a_native_agent_catalog_cannot_name_top(self):
        import native_agents
        entries, problems = native_agents.catalog_tokens({"CFG_NATIVE_AGENT_CATALOG": "helper:top helper2:deep"})
        self.assertEqual(entries, [("helper2", "deep")])
        self.assertTrue(problems and "top is not a native agent profile" in problems[0])

    def test_the_batch_contract_admits_one_top_leg_and_refuses_two(self):
        self.assertIn("top", B.SUPPORTED_PROFILES)
        self.assertEqual(B.MAX_TOP_LEGS, 1)


if __name__ == "__main__":
    unittest.main()
