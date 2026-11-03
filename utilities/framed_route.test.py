#!/usr/bin/env python3
"""The framed route: compose, seal, verify, card, decision record, and the model-less ending.

Real compose, real route files, real producer cycles and the real completion/close/finalize
code. Only the two runtime gates that need live frame attempts (two current frame markers,
a released frame-review gate) and the model launch are controlled here; each has a
refusal test against the unpatched gate.
"""
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# The card screens these tests compare are Korean and UI text defaults to English,
# so this suite declares the Korean operator language itself (tools/i18n/ui_text.py).
os.environ["FLEET_NOW_LANG"] = "Korean"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "tools"))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


T = _load("producer_test_for_framed", "artifact_producer.test.py")
R, P, L, TOPO = T.R, T.P, T.L, T.R.TOPO
import route_plan as RP  # noqa: E402
import work_start as W  # noqa: E402
import capability_topology as CT  # noqa: E402

FRAME_IDS = ["frame", "frame-alternative"]
ONE_LEG_IDS = ["frame", "route-decision"]
NODE_IDS = FRAME_IDS + ["route-decision"]

# Frozen from `git show 45edc48a:capabilities/topologies.json` (the registry before the route-frame
# recipe). They are literals, not a comparison with HEAD, so they mean the same in CI and after any
# commit. Report-format bootstrap intentionally refreshes only the four reader-facing recipes
# and their capability digests. Entry approvals refresh only refine's preview approval mark,
# review output, and the corresponding catalog/recipe digests. Verified resume refreshes only
# the optional resume-run catalog and dedicated resource-exit gate contract digests.
# Each global table is hashed as sha256 of its JSON (sorted keys, compact separators).
FROZEN_GLOBAL_TABLE_DIGESTS = {
    "schema_version": "4a44dc15364204a80fe80e9039455cc1608281820fe2b24f1e5233ade6af1dd5",
    "intensities": "965abb3701067fd845cba3196ebd2c1725c399b515a884c452ebdc3e0e05186c",
    "execution_topologies": "8622e71fa48d847df8c61544623832e7942dfad9f532b596507b270f7ee75f4c",
    "worker_kinds": "c6e834172f80e8172f4576bece68a415ae0c60885cdeceebb8fce45fd7dc4b23",
    "transports": "df28d7d66bcfe9326f606a8df9edd7f303db5930fd809b4c7c0b93915ea91d3e",
    "runtime_requirements": "5da3cf55c86966df30193d5892070e4a8d489442ab7677b4b8cc18029be83921",
    "tracking_values": "39c1812b31915d599cd78f61a68a976f492451f9445fdbaac8c36c720a90f4dd",
    "tracked_gate_evidence": "271082bb2b02743cece7c1d50a653d41e61e3999572858b1d17a21b98f7f8de0",
    "guard_preconditions": "ebf235004f9bea0626ded66d0fbf9298e4909d4e1bb2f7b6c070de557fe2660b",
    "artifact_owners": "bf04f1e84e854aa45368ae6891d567b019dedfca4b9c07d4d2a86667230849b6",
    "rollout": "c023090e49860a7b1c9f9117f74ec147b2782fa9954ac27252bde5b6758dbf67",
    "inline_reasons": "d494c665c99149e73f546a3af5852a00c4b060605a039bb8203b27ca3a33ad3e",
    "gate_command": "a7e4a42cb12603e15fbeed1660c23054e5c2e6cf9e176a928ac441da367e28bd",
    "activation_conditions": "171ddc0dc24cebb02e7cb56d8d8ccf6b7837a8d0dfb9bd307f2c74c0aac1fe32",
    "execution_surfaces": "de42c73e3068f58773195eb6f62568a1361e28edba1e32f8e0f8ef766d6c3374",
    "fallback_hops": "b1ae1168c05d28a6a60e3776059a23a0a6379b294af1fd9d326c10d94cc84d07",
    "unit_catalog": "ddaefe041bff3943462396e59385c3ca56f9f94633e329cd198fb6adfec86d34",
    "unit_families": "b119cbdb257b25422902df25d95457d3f44b1e41d1cfca61fb6cd582b486b4d7",
    "reserved_units": "8fd8169f3eb8d44d75a96b4ab0efd2bb0e9ccb39e9c89a7f615876053eef4d52",
    "unit_kind_compatibility": "15d3d78228d77b8819bf83baa1538be02264e42bc72e92b5734c0f2e742ee54c",
    "model_profiles": "bff612bb22e8bd13e7d5c899de3633859ee4632a182dc175eb7e792f6c64164a",
    "owner_profile_by_intensity": "d9925418217de670821aba84a9018ba0fd58c36231e3108e8bb6b681f8318904",
    "parallel_group_kinds": "cd159bee3f1b03ceabed5141486bc1d87c7d7ced2e539767a7b51451132c111d",
    "parallel_join_policies": "bea0e3ec4c32132ca0641ce9a12dd75c620a9fc89ccc91c8a6bca8432f1bc24f",
    "parallel_independence_axes": "3656cfa465a627ba95ffb6dbed164fc1c2e76dd4c07925efcd8cc308cbffc914",
    "parallel_group_max_width": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
    "leg_classes": "b397c1e152fed9127aa83b832bad9d1fc69e0865906a163623937d9b3904fba8",
    "auxiliary_checks": "b4cbc4bc242726351a6cf6b2c53c55b6e29c87e57c2cd9a837fa8291b0bfeafe",
    "auxiliary_check_units": "7a8bd50e2fbcb0a574d1bae7e26362630cf3551907bf310272a7394795148e0a",
    "workflow_states": "0ac55a71bde1486fc7b03d1696052959a76324ba31ee34bd458b9433c683747b",
    "workflow_failure_states": "7f6c462b51c514f16b68e92e1f8196cc09e0504628a35274f14652645456fe2d",
    "workflow_transitions": "8868f3b57cca8ad4beab4c558098ac10d22e4e0605da67e1ba03e26e8edd5f5e",
    "continuation_kinds": "ecac7e09cb2322bb13a1a0b2a84b13a044d5d1552d090ba4a14d125e19269270",
    "human_gate_positions": "fc45768a57481792e0643d4bec1faae15743411bc06096fdb1740f632f0707b0",
    "artifact_buckets": "ccbd32ef24b84897147e3d1a0862a824d9c6f893a12aed07f53c9f77724cb835",
    "producer_lifecycle": "a9f26a373d2e3af19f55be8c37b8cd0126f30165cdfa0407ef0242aa7c2f9816",
    "part_catalog": "0997dd287c9100c40b532d78daa5cc05a6875667f3d3dc584a9b5d5660d37db5",
}
FROZEN_GATE_CONTRACT_DIGEST = "4b243bd3f897ea98d1ba93283f8297c10c88155c2c2ee80146322023902adb58"
FROZEN_RECIPE_COUNT = 14
FROZEN_RECIPES_DIGEST = "be19089591319912719893b9d2a2f14aca38f900a76b54bfb1c310b060dcf674"
FROZEN_CAPABILITY_REGISTRY_DIGESTS = {
    "analyze-project": "sha256:160c04dc760b81584a7d445da0733c778d7caaabd12f43d606e72a00fb9ca574",
    "analyze-user": "sha256:a787e6d28fbc54ba019dc635f462ddb9b5ffd1112f9a08ecf56dab950761899a",
    "audit": "sha256:d2e6741875289ba300b4334b9a84e1cf99fcf1a4e60c73c6c594a1bcaa65d314",
    "autopilot-apply": "sha256:317c03fb47f59789c1a12b15c1a0dcf93f60fcee57408f0ae1ab4cb45f6f7cd5",
    "autopilot-code": "sha256:8f1f6572ba2e1038c0bceaf6ca411d6f0a2118bac69464429ccfd05d6fb78fe9",
    "autopilot-design": "sha256:95ea188edc7350bc83af19a78aec602c1d6e6d84fdde553c29036be997078f46",
    "autopilot-draft": "sha256:1f6ff17e9bd5f3cec8d38f32cd7415913ff2d9111762cda3f44b8ea718334067",
    "autopilot-lab": "sha256:0d8abcee90861a84161e9d64df97caf2f1423ae421b1d9659b2c76ed55e92ff5",
    "autopilot-refine": "sha256:1721bbf230492a6f2c1994924a938e2e4c451ea21b7d16165ab9647d6e5f5e7a",
    "autopilot-research": "sha256:f1c4802cdf4250d3a97350e875d5414af32bf74fa628a8db69c7401e550421dd",
    "autopilot-ship": "sha256:6a874217db61f46e3b80550487fd6819532244a5c9f1d0bb05f01db1279e9181",
    "autopilot-spec": "sha256:0b14f71a5d072886272f23dcb3d3abebe1d4c901f2f1893937ae0cff8504e9bb",
}


def _frozen_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()



def clean_environment():
    """No worker identity: these tests are the depth-0 caller."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("AGENT_DISPATCH_", "AGENT_ARTIFACT_", "AGENT_OWNER_", "AGENT_ROUTE_"))}


class FramedBase(T.ProducerTestBase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, clean_environment(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        # The runtime that compiles is the runtime that runs: a route published with the gates on
        # refuses a mismatched runtime root, exactly as the public compose does.
        os.environ["AGENT_HOME"] = str(R.ROOT)
        os.environ["AGENT_DISPATCH_JOBS"] = str(self.jobs)
        self.activate()

    # Exercise the default framed route unless a test asks for an explicit intensity.
    FRAME_INTENSITY = None

    def compose(self, **kw):
        arguments = dict(
            capability=None, capability_mode=None, shape="framed", graph=None, slug="framed-fixture",
            intensity=self.FRAME_INTENSITY,
            cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", campaign_key="framed-key",
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]},
            work_request={"text": "Decide how to do this", "owner_harness": None})
        arguments.update(kw)
        return R.compose_route(**arguments)

    def admitted(self, **kw):
        route = self.compose(**kw)
        return route, Path(L.admit_runtime_route(self.root, route).route_file)

    def begin_cycle(self, path):
        """What the frame launch does: begin the route's producer cycle."""
        return Path(P.prepare_route_artifact_env(path, start=True, jobs=self.jobs)["AGENT_ARTIFACT_OUTPUT_DIR"])

    @staticmethod
    def write_frame_outputs(output, *, brief="brief", intent="intent"):
        for node in FRAME_IDS:
            (output / "shards" / node).mkdir(parents=True, exist_ok=True)
            (output / "shards" / node / "direction-brief.md").write_text(f"# {node} {brief}\n", encoding="utf-8")
        (output / "shards/frame/intent.md").write_text(intent + "\n", encoding="utf-8")


def legacy_framed_route(base, intensity):
    """Build a historical sealed fixture from today's route, then remove only the newer basis row."""
    route = base.compose(intensity=intensity)
    basis = route["selection"]["selection_basis"]
    basis[:] = [row for row in basis if row.get("axis") != "frame-legs"]
    if intensity == "quick":
        # The old no-intensity default was one deep leg, even though quick is today's one-leg recipe.
        intensity_row = next(row for row in basis if row.get("axis") == "compose-intensity")
        intensity_row.update(signal="standard", source="shape-default")
    route["route_hash"] = R.route_hash(route)
    route["route_id"] = R.ROUTE_IDENTITY.route_id_from_hash(route["route_hash"])
    return route


class FramedRouteCompileTest(FramedBase):
    def test_a164_1_compose_without_a_capability_seals_the_internal_route(self):
        route = self.compose()
        self.assertEqual((route["capability"], route["capability_mode"]), ("route-frame", "default"))
        self.assertEqual((route["requested_intensity"], route["effective_intensity"]), ("standard", "standard"))
        self.assertEqual(route["selection"]["shape"], "framed")
        self.assertEqual(route["selection"]["route_origin"], "compose")
        nodes = {n["id"]: n for n in route["nodes"]}
        self.assertEqual([n["id"] for n in route["nodes"]], NODE_IDS)
        for frame in FRAME_IDS:
            node = nodes[frame]
            self.assertEqual((node["kind"], node["unit"], node["worker_type"], node["dispatch_depth"]),
                             ("map-worker", "plan/frame", "frame", 1))
            self.assertEqual(node["model_profile"], "top")
            self.assertEqual(node["profile_selection"]["resolved_profile"], "top")
            self.assertEqual(node["depends_on"], [])
            self.assertEqual(node["continuation"], {"kind": "human-gate", "gate": "frame-review"})
        terminal = nodes["route-decision"]
        self.assertEqual(terminal["kind"], "runtime-terminal")
        self.assertEqual(terminal["depends_on"], FRAME_IDS)
        self.assertEqual((terminal["dispatch_depth"], terminal["registered_worker"],
                          terminal["execution_surface"]), (0, False, "inline"))
        for forbidden in ("unit", "model_profile", "profile_selection", "profile_demand", "role",
                          "worker_type", "fallback_hops", "continuation", "unit_choices"):
            self.assertNotIn(forbidden, terminal, forbidden)
        self.assertTrue(terminal["terminal"])
        self.assertEqual(terminal["terminal_gate"], "route-decision")
        self.assertEqual(route["human_gates"], ["frame-review"])
        self.assertEqual(route["human_gate_bindings"],
                         [{"gate": "frame-review", "node": "route-decision", "position": "entry"}])
        self.assertEqual(route["parallel_groups"], [])
        self.assertEqual([n["id"] for n in route["nodes"] if n.get("unit") == "_kernel/owner"], [])
        self.assertEqual(route["workflow_contract"]["terminal_nodes"], ["route-decision"])
        self.assertTrue(RP.is_framed_route(route))
        verified = R.verify_route(json.loads(json.dumps(route)), R.ROOT)
        self.assertEqual(verified["route_hash"], route["route_hash"])

    def test_a164_1_hints_are_recorded_in_the_work_request_and_never_sealed(self):
        route = self.compose(capability="autopilot-spec", capability_mode="app", graph="plan,execute",
                             profile="light")
        self.assertEqual(route["capability"], "route-frame")
        self.assertEqual(route["work_request"]["routing_hints"], {
            "capability": "autopilot-spec", "capability_mode": "app", "graph": "plan,execute",
            "profile": "light"})
        self.assertEqual(route["work_request"]["text"], "Decide how to do this")
        self.assertEqual({n["model_profile"] for n in route["nodes"] if R._frame_node(n)}, {"top"})
        self.assertEqual(route.get("explicit_profiles"), {})
        self.assertNotIn("composed", route)
        R.verify_route(route, R.ROOT)
        bare = self.compose()
        self.assertNotIn("routing_hints", bare["work_request"])
        self.assertEqual(self.compose(capability="autopilot-code")["work_request"]["routing_hints"],
                         {"capability": "autopilot-code"})

    def test_hints_that_are_not_strings_or_unknown_keys_are_refused_as_a_request(self):
        for bad in ({}, {"owner": "x"}, {"graph": ""}, {"graph": 3}, {"capability": "x" * 600}, "graph"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                W.validate_request({"text": "t", "owner_harness": None, "routing_hints": bad})

    def test_a_node_specific_profile_still_beats_the_top_default_but_a_profile_hint_does_not(self):
        route = self.compose(explicit_profiles={"frame-alternative": "balanced"}, profile="light")
        nodes = {n["id"]: n for n in route["nodes"]}
        self.assertEqual(nodes["frame"]["model_profile"], "top")
        self.assertEqual(nodes["frame-alternative"]["model_profile"], "balanced")
        R.verify_route(route, R.ROOT)

    def test_pin_and_campaign_rules_apply_as_for_any_route(self):
        pins = R._parse_selection_pins(["frame=claude"], None)
        route = self.compose(selection_pins=pins)
        self.assertEqual(route["selection_pins"]["frame"], {"harness": "claude", "model": None, "effort": None})
        R.verify_route(route, R.ROOT)
        self.assertEqual(self.compose(campaign_key=None, unassigned=True)["campaign_unassigned"], True)
        with self.assertRaisesRegex(ValueError, "compose-campaign-key-required"):
            self.compose(campaign_key=None)
        with self.assertRaisesRegex(ValueError, "compose-campaign-selection-conflict"):
            self.compose(campaign_key="k", unassigned=True)
        self.assertEqual(self.compose(campaign_key=None, parent_cycle_id="cyc_" + "a" * 32)["parent_cycle_id"],
                         "cyc_" + "a" * 32)

    def test_intensity_picks_default_pair_and_guarded_quick_exception(self):
        """Only an explicit quick/direct request without protected facts can narrow the frame."""
        for intensity, ids in ((None, NODE_IDS), ("direct", ONE_LEG_IDS), ("quick", ONE_LEG_IDS),
                               ("standard", NODE_IDS), ("strong", NODE_IDS), ("thorough", NODE_IDS)):
            with self.subTest(intensity=intensity):
                route = self.compose(intensity=intensity)
                self.assertEqual([n["id"] for n in route["nodes"]], ids)
                self.assertEqual((route["requested_intensity"], route["effective_intensity"]), ("standard", "standard"))
                nodes = {n["id"]: n for n in route["nodes"]}
                self.assertEqual(nodes["route-decision"]["depends_on"], ids[:-1])
                self.assertEqual({nodes[leg]["model_profile"] for leg in ids[:-1]},
                                 {"deep"} if len(ids) == 2 else {"top"})
                self.assertEqual(route["human_gate_bindings"],
                                 [{"gate": "frame-review", "node": "route-decision", "position": "entry"}])
                self.assertTrue(RP.is_framed_route(R.verify_route(json.loads(json.dumps(route)), R.ROOT)))
                self.assertEqual(RP.frame_legs(route), tuple(ids[:-1]))
        with self.assertRaisesRegex(ValueError, "invalid intensity"):
            self.compose(intensity="bogus")
        one = self.compose(intensity="quick")
        self.assertFalse(RP.valid_frame_legs({"nodes": []}, ["frame"]))           # a lone leg only on a framed route
        self.assertTrue(RP.valid_frame_legs(one, ["frame"]))
        tampered = json.loads(json.dumps(one))
        tampered["nodes"][0]["model_profile"] = "top"
        tampered["route_hash"] = R.route_hash(tampered)
        tampered["route_id"] = "rt-" + tampered["route_hash"].split(":", 1)[1][:16]
        with self.assertRaises(ValueError):
            R.verify_route(tampered, R.ROOT)

    def test_the_card_shows_the_two_framed_lines_and_no_other_shape_does(self):
        card = R.compose_card(self.compose())
        self.assertIn("  frame이 방향과 경로를 조립해 제안합니다", card)
        self.assertIn("  비용: 최상위 모델 두 갈래 · 방향 확인 질문 1회", card)
        self.assertIn("framed(standard)", card.splitlines()[0])
        self.assertIn("사람 게이트 frame-review", card.splitlines()[0])
        staged = R.compose_route(
            capability="autopilot-code", capability_mode="dev", shape="staged", graph="execute,test,report",
            slug="other", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", unassigned=True,
            dispatch_evidence={"tuples": [T.nested("claude", "codex")]})
        self.assertNotIn("frame이 방향과 경로를 조립해", R.compose_card(staged))

    def test_the_card_prints_in_english_unless_korean_is_declared(self):
        import re
        hangul = re.compile(r"[\uac00-\ud7a3]")
        route = self.compose()
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "English"}):
            card = R.compose_card(route)
            lines = "\n".join(R.compose_decision_lines(route))
        self.assertIsNone(hangul.search(card), card)
        self.assertIsNone(hangul.search(lines), lines)
        self.assertTrue(card.startswith("[Route] route-frame \u00b7 framed("), card)
        self.assertIn("human gate frame-review", card.splitlines()[0])
        self.assertIn("  frame assembles and proposes the direction and route", card)
        self.assertIn("  Cost: two top-model legs \u00b7 one direction-check question", card)
        self.assertIn("  Size: nodes=", lines)
        self.assertIn("awaiting frame decision", lines)
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Korean"}):
            korean = R.compose_card(route)
        self.assertTrue(korean.startswith("[경로] route-frame"), korean)
        self.assertIn("사람 게이트 frame-review", korean.splitlines()[0])
        # Same card, same structure: only the words differ.
        self.assertEqual(len(card.splitlines()), len(korean.splitlines()))
        with mock.patch.dict(os.environ, {"FLEET_NOW_LANG": "Japanese"}):
            self.assertEqual(R.compose_card(route), card)  # other languages get English

    def test_basis_free_historical_framed_routes_verify_and_keep_the_old_card_label(self):
        """Fixture method: strip frame-legs from today's sealed compose result and reseal with existing hash/id."""
        for intensity, legs, expected_intensity in (
                ("quick", ("frame",), None), ("strong", tuple(FRAME_IDS), "strong")):
            with self.subTest(intensity=intensity):
                route = legacy_framed_route(self, intensity)
                basis = {row.get("axis"): row for row in route["selection"]["selection_basis"]}
                self.assertNotIn("frame-legs", basis)
                self.assertEqual((basis["compose-intensity"].get("source"),
                                  basis["compose-intensity"].get("signal")),
                                 ("shape-default", "standard") if intensity == "quick"
                                 else ("explicit", "strong"))
                verified = R.verify_route(json.loads(json.dumps(route)), R.ROOT)
                self.assertEqual(RP.frame_legs(verified), legs)
                nodes = {node["id"]: node for node in verified["nodes"]}
                self.assertEqual({nodes[node]["model_profile"] for node in legs},
                                 {"deep"} if len(legs) == 1 else {"top"})
                lines = R.compose_decision_lines(verified)
                size = next(line for line in lines if line.startswith("  규모:"))
                count_word = "한" if len(legs) == 1 else "두"
                self.assertIn(f"frame {count_word} 갈래(이전 규칙으로 만든 경로)", size)
                self.assertFalse(any("강도 안내" in line for line in lines))
                command = shlex.split(W._compose_again(verified))
                if expected_intensity is None:
                    self.assertNotIn("--intensity", command)
                else:
                    self.assertEqual(command[command.index("--intensity") + 1], expected_intensity)

    def test_the_internal_capability_is_reachable_only_through_the_framed_shape(self):
        for shape, graph in (("direct", None), ("solo", None), ("staged", None), ("staged", "frame,route-decision")):
            with self.subTest(shape=shape, graph=graph), self.assertRaises(ValueError) as refused:
                self.compose(capability="route-frame", shape=shape, graph=graph, unassigned=True, campaign_key=None,
                             registered_headless_evidence={"candidates": T.registered_headless()["candidates"]})
            self.assertRegex(str(refused.exception), "compose-(capability-unknown|shape-invalid|graph-only-staged)")
        for intensity in ("direct", "quick", "standard"):
            with self.subTest(compile=intensity), self.assertRaisesRegex(ValueError, "compose-shape-invalid"):
                T.compile_for(intensity, self.root, "route-frame", "default")
        with self.assertRaises(ValueError):
            self.compose(capability="autopilot-code", shape="staged", graph="route-frame:frame",
                         unassigned=True, campaign_key=None)

    def test_a_sealed_route_cannot_pair_the_internal_capability_with_another_shape(self):
        route = self.compose()
        forged = copy.deepcopy(route)
        forged["selection"]["shape"] = "staged"
        forged["route_hash"] = R.route_hash(forged)
        forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
        with self.assertRaisesRegex(ValueError, "route-frame-shape-mismatch"):
            R.verify_route(forged, R.ROOT)
        staged = self.compose(capability="autopilot-code", shape="staged", graph="execute,test,report",
                              campaign_key="other")
        forged = copy.deepcopy(staged)
        forged["selection"]["shape"] = "framed"
        forged["route_hash"] = R.route_hash(forged)
        forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
        with self.assertRaisesRegex(ValueError, "route-frame-shape-mismatch"):
            R.verify_route(forged, R.ROOT)

    def test_a_tampered_framed_route_does_not_verify(self):
        route = self.compose()
        for label, mutate in (
                ("extra unit on the terminal", lambda r: r["nodes"][2].update(unit="_kernel/owner")),
                ("terminal at depth 1", lambda r: r["nodes"][2].update(dispatch_depth=1)),
                ("frame leg off top", lambda r: r["nodes"][0].update(model_profile="deep")),
                ("second gate binding", lambda r: r["human_gate_bindings"].append(
                    {"gate": "frame-review", "node": "frame", "position": "entry"})),
                ("dropped node", lambda r: r["nodes"].pop())):
            forged = copy.deepcopy(route)
            mutate(forged)
            forged["route_hash"] = R.route_hash(forged)
            forged["route_id"] = "rt-" + forged["route_hash"].split(":", 1)[1][:16]
            with self.subTest(label), self.assertRaises(ValueError):
                R.verify_route(forged, R.ROOT)

    def test_the_route_compiles_the_same_bytes_twice(self):
        first, second = self.compose(), self.compose()
        self.assertEqual(first["route_hash"], second["route_hash"])

    def test_cli_compose_framed_takes_no_capability_and_records_the_hints(self):
        evidence = Path(self._tmp.name) / "evidence.json"
        evidence.write_text(json.dumps({"tuples": [T.nested("claude", "codex")]}), encoding="utf-8")
        task = Path(self._tmp.name) / "task.md"
        task.write_text("Plan this work\n", encoding="utf-8")
        env = {**clean_environment(), "AGENT_HOME": str(R.ROOT), "AGENT_DISPATCH_JOBS": str(self.jobs),
               "FLEET_ROUTE_CHAIN_DIR": str(Path(self._tmp.name) / "chains")}
        base = [sys.executable, str(HERE / "capability-route.py"), "compose", "--shape", "framed",
                "--cwd", str(R.ROOT), "--artifact-root", str(self.root), "--spec-read", "fixture",
                "--dispatch-evidence", str(evidence), "--prompt-file", str(task), "--slug", "cli-framed"]
        one = subprocess.run(base + ["--unassigned", "--explain"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(one.returncode, 0, one.stderr)
        self.assertIn("비용: 최상위 모델 두 갈래 · 방향 확인 질문 1회", one.stderr)
        base += ["--intensity", "strong"]
        done = subprocess.run(
            base + ["--campaign-key", "cli-key", "--capability", "autopilot-refine", "--graph", "review",
                    "--profile", "light"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        receipt = json.loads(done.stdout.strip().splitlines()[-1])
        route = json.loads(Path(receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(route["capability"], "route-frame")
        self.assertEqual(route["work_request"]["routing_hints"],
                         {"capability": "autopilot-refine", "graph": "review", "profile": "light"})
        self.assertIn("frame이 방향과 경로를 조립해 제안합니다", done.stderr)
        self.assertIn("비용: 최상위 모델 두 갈래 · 방향 확인 질문 1회", done.stderr)
        explained = subprocess.run(base + ["--unassigned", "--explain"], text=True, capture_output=True,
                                   env=env, check=False)
        self.assertEqual(explained.returncode, 0, explained.stderr)
        self.assertEqual(json.loads(explained.stdout.strip().splitlines()[-1])["shape"], "framed")
        # Omission seals a frame; --graph alone remains only a routing hint.
        omitted = [sys.executable, str(HERE / "capability-route.py"), "compose", "--cwd", str(R.ROOT),
                   "--artifact-root", str(self.root), "--spec-read", "fixture", "--prompt-file", str(task),
                   "--dispatch-evidence", str(evidence), "--slug", "omitted-shape", "--unassigned"]
        explained = subprocess.run(omitted + ["--explain"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(explained.returncode, 0, explained.stderr)
        explanation = json.loads(explained.stdout.strip().splitlines()[-1])
        self.assertEqual((explanation["shape"], explanation["capability"]), ("framed", "route-frame"))
        sealed = subprocess.run(omitted, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(sealed.returncode, 0, sealed.stderr)
        receipt = json.loads(sealed.stdout.strip().splitlines()[-1])
        route = json.loads(Path(receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual((route["selection"]["shape"], route["capability"]), ("framed", "route-frame"))

        direct = subprocess.run(omitted + ["--shape", "direct", "--slug", "direct-shape", "--explain"],
                                text=True, capture_output=True, env=env, check=False)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        direct_result = json.loads(direct.stdout.strip().splitlines()[-1])
        self.assertEqual((direct_result["shape"], direct_result["capability"]),
                         ("direct", "autopilot-code"))
        self.assertTrue(direct_result["nodes"])
        self.assertTrue(all(node["dispatch_depth"] == 0 for node in direct_result["nodes"]))

        graph_only = omitted + ["--graph", "execute,test,report", "--slug", "graph-hint"]
        graph_explained = subprocess.run(graph_only + ["--explain"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(graph_explained.returncode, 0, graph_explained.stderr)
        graph_result = json.loads(graph_explained.stdout.strip().splitlines()[-1])
        self.assertEqual((graph_result["shape"], graph_result["capability"]), ("framed", "route-frame"))
        graph_sealed = subprocess.run(graph_only, text=True, capture_output=True, env=env, check=False)
        self.assertEqual(graph_sealed.returncode, 0, graph_sealed.stderr)
        graph_receipt = json.loads(graph_sealed.stdout.strip().splitlines()[-1])
        graph_route = json.loads(Path(graph_receipt["route_file"]).read_text(encoding="utf-8"))
        self.assertEqual(graph_route["work_request"]["routing_hints"]["graph"], "execute,test,report")
        self.assertEqual(graph_route["selection"]["shape"], "framed")


class RegistryRegistrationTest(unittest.TestCase):
    def registry(self):
        return CT.load_registry()

    def before_design_layout(self):
        # Keep every frozen hash below. Authenticate exactly the intentional
        # flat-design delta, then compare the original framed-route surface.
        registry = copy.deepcopy(self.registry())
        self.assertEqual(registry["artifact_buckets"]["designs"], "designs")
        def restore(value):
            if isinstance(value, str):
                self.assertNotIn("designs/<cycle>", value)
                if value == "designs":
                    return "designs/<cycle>"
                if value.startswith("designs/"):
                    return "designs/<cycle>/" + value[len("designs/"):]
                return value
            if isinstance(value, list):
                return [restore(item) for item in value]
            if isinstance(value, dict):
                return {key: restore(item) for key, item in value.items()}
            return value
        layout = restore(registry)
        self._assert_audit_report_delta_and_restore(layout)
        return layout

    def _assert_audit_report_delta_and_restore(self, registry):
        # Audit report authoring intentionally moves the default report unit to
        # editorial/report and lists it first among the catalog choices.
        audit = next(r for r in registry["recipes"] if r["capability"] == "audit")
        report = next(n for n in audit["standard_plus"]["nodes"] if n["id"] == "report")
        self.assertEqual(report["unit"], "editorial/report")
        report["unit"] = "editorial/polish"
        choices = registry["part_catalog"]["parts"]["audit:report"]["unit_choices"]
        self.assertEqual(choices, ["editorial/report", "editorial/polish", "research/research-survey"])
        choices.remove("editorial/report")

    def recipe(self, registry):
        return next(r for r in registry["recipes"] if r["capability"] == "route-frame")

    def _assert_code_authoring_delta_and_restore_historical_scopes(self, recipes):
        # Code-route PRD authoring intentionally changes these exact fields.
        # Keep the historical baseline literals for every remaining field;
        # capability_topology.test.py also pins the full current code recipe.
        codes = [r for r in recipes if r["capability"] == "autopilot-code"]
        self.assertEqual(len(codes), 1)
        code = codes[0]
        self.assertEqual(code["quick"]["write_scope"],
                         ["source-scoped", "plans/<cycle>/**", "spec/**"])
        self.assertEqual(code["quick"]["guard_preconditions"], ["artifact-order-prechecked"])
        execute = next(n for n in code["standard_plus"]["nodes"] if n["id"] == "execute")
        self.assertEqual(execute["write_scope"],
                         ["source/**", "spec/**", "checklist.md", "dev_logs/**", "evidence/**"])
        self.assertEqual(execute["guard_preconditions"], ["artifact-order-prechecked"])
        code["quick"]["write_scope"].remove("spec/**")
        del code["quick"]["guard_preconditions"]
        execute["write_scope"].remove("spec/**")
        del execute["guard_preconditions"]

    def test_the_internal_recipe_is_registered_with_the_smallest_surface(self):
        registry = self.registry()
        summary = CT.validate_registry(registry)
        self.assertEqual((summary["capabilities"], summary["recipes"]), (13, 29))
        self.assertIn(("route-frame", "default"), CT.recipe_keys(registry))
        manifest = json.loads((HERE.parent / "harness-manifest.json").read_text(encoding="utf-8"))
        row = manifest["capabilities"]["route-frame"]
        self.assertEqual(row["invocation"]["class"], "compiler-internal")
        self.assertEqual(row["requires"], {"capabilities": [], "units": ["plan/frame"]})
        self.assertTrue((HERE.parent / "capabilities" / "route-frame.md").is_file())
        self.assertIn(("route-frame", "default"), CT.expected_recipe_keys(manifest))
        self.assertEqual(registry["completion_gate_contracts"]["route-decision"]["kind"], "custom")
        for unchanged in ("worker_kinds", "unit_kind_compatibility", "model_profiles", "owner_profile_by_intensity"):
            self.assertIn(unchanged, registry)
        self.assertNotIn("runtime-terminal", registry["worker_kinds"])

    def test_no_user_facing_surface_exists_for_the_internal_capability(self):
        root = HERE.parent
        self.assertFalse((root / "skills" / "route-frame").exists())
        for adapter in ("claude", "codex", "opencode"):
            self.assertFalse((root / "adapters" / adapter / "skills" / "route-frame").exists(), adapter)
        self.assertFalse((root / "adapters/opencode/commands/route-frame.md").exists())

    def test_the_runtime_terminal_is_accepted_only_by_the_exact_route_frame_shape(self):
        registry = self.registry()
        for label, mutate in (
                ("unit on the terminal", lambda r: r["standard_plus"]["nodes"][2].update(unit="_kernel/owner")),
                ("model on the terminal", lambda r: r["standard_plus"]["nodes"][2].update(model_profile="deep")),
                ("terminal at depth 1", lambda r: r["standard_plus"]["nodes"][2].update(dispatch_depth=1)),
                ("registered terminal", lambda r: r["standard_plus"]["nodes"][2].update(registered_worker=True)),
                ("extra node", lambda r: r["standard_plus"]["nodes"].append(
                    {**copy.deepcopy(r["standard_plus"]["nodes"][0]), "id": "third"})),
                ("renamed frame", lambda r: r["standard_plus"]["nodes"][1].update(id="frame-two")),
                ("second binding", lambda r: r["human_gate_bindings"].append(
                    {"gate": "frame-review", "node": "frame", "position": "entry"})),
                ("no gate", lambda r: r.update(human_gates=[], human_gate_bindings=[]))):
            mutated = copy.deepcopy(registry)
            mutate(self.recipe(mutated))
            with self.subTest(label), self.assertRaises(CT.TopologyError):
                CT.validate_registry(mutated)

    def test_no_other_capability_may_declare_a_runtime_terminal(self):
        registry = self.registry()
        other = next(r for r in registry["recipes"] if r["capability"] == "audit")
        terminal = copy.deepcopy(self.recipe(registry)["standard_plus"]["nodes"][2])
        other["standard_plus"]["nodes"][-1].update({k: terminal[k] for k in ("kind", "dispatch_depth")})
        with self.assertRaisesRegex(CT.TopologyError, "invalid worker kind"):
            CT.validate_registry(registry)
        self.assertFalse(CT.is_route_frame_terminal(other, other["standard_plus"]["nodes"][-1]))

    def test_the_global_tables_that_seal_every_digest_are_untouched(self):
        after = self.before_design_layout()
        for key, digest in FROZEN_GLOBAL_TABLE_DIGESTS.items():
            self.assertEqual(_frozen_digest(after[key]), digest, key)
        # No global table was added either: the only other keys are the two per-name tables.
        self.assertEqual(set(after) - set(FROZEN_GLOBAL_TABLE_DIGESTS), {"recipes", "completion_gate_contracts"})
        recipes = copy.deepcopy(after["recipes"][:FROZEN_RECIPE_COUNT])
        self._assert_code_authoring_delta_and_restore_historical_scopes(recipes)
        for recipe in recipes:
            if recipe["capability"] == "autopilot-lab" and "setup" in recipe["modes"]:
                self.assertEqual(recipe["promotion_signals"].count("gpu"), 1)
                recipe["promotion_signals"].remove("gpu")
        self.assertEqual(_frozen_digest(recipes), FROZEN_RECIPES_DIGEST)
        # New gates do not change the frozen per-name contracts or preset recipe digests.
        self.assertFalse(any(n["completion_gate"] == "compose-owner-close"
                             for r in after["recipes"] for n in r["standard_plus"]["nodes"]))
        rest = {k: v for k, v in after["completion_gate_contracts"].items()
                if k not in ("route-frame", "route-decision", "compose-owner-close")}
        self.assertEqual(_frozen_digest(rest), FROZEN_GATE_CONTRACT_DIGEST)

    def test_other_capabilities_keep_their_registry_digest(self):
        registry = self.before_design_layout()
        self._assert_code_authoring_delta_and_restore_historical_scopes(registry["recipes"])
        for recipe in registry["recipes"]:
            if recipe["capability"] == "autopilot-lab" and "setup" in recipe["modes"]:
                self.assertEqual(recipe["promotion_signals"].count("gpu"), 1)
                recipe["promotion_signals"].remove("gpu")
        self.assertEqual(
            sorted({r["capability"] for r in registry["recipes"]} - {"route-frame"}),
            sorted(FROZEN_CAPABILITY_REGISTRY_DIGESTS),
        )
        for capability, digest in FROZEN_CAPABILITY_REGISTRY_DIGESTS.items():
            with self.subTest(capability=capability):
                self.assertEqual(CT.capability_registry_digest(registry, capability), digest)

    def test_the_producer_admits_the_internal_capability_and_checks_route_equality(self):
        self.assertEqual(P.INTERNAL_CAPABILITIES, ("route-frame",))
        self.assertNotIn("route-frame", P.ENTRY_CAPABILITIES + P.STAGE_CAPABILITIES)


class ProducerCycleTest(FramedBase):
    def test_begin_issues_a_cycle_for_the_framed_route_and_checks_the_capability(self):
        route, path = self.admitted()
        result = P.begin(self.root, route_file=path, capability="route-frame", intensity="standard",
                         require_cycle=True, jobs=self.jobs)
        self.assertEqual(result["status"], "begun")
        record = P.read_cycle_record(self.root, result["env"]["AGENT_ARTIFACT_CYCLE_ID"])
        self.assertEqual((record["route_capability"], record["route_id"]), ("route-frame", route["route_id"]))
        with self.assertRaises(P.ProducerError) as mismatch:
            P.begin(self.root, route_file=path, capability="autopilot-code", intensity="standard",
                    require_cycle=True, jobs=self.jobs)
        self.assertEqual(mismatch.exception.code, "route-capability-mismatch")
        with self.assertRaises(P.ProducerError) as unknown:
            P.begin(self.root, route_file=path, capability="not-a-capability", intensity="standard",
                    jobs=self.jobs)
        self.assertEqual(unknown.exception.code, "capability-unknown")


class RouteDecisionRecordTest(unittest.TestCase):
    FRAME = {"route_id": "rt-0123456789abcdef", "route_hash": "sha256:" + "a" * 64, "cycle_id": "cyc_" + "b" * 32}
    BRIEFS = [{"node": "frame", "path": "c/shards/frame/direction-brief.md", "sha256": "1" * 64},
              {"node": "frame-alternative", "path": "c/shards/frame-alternative/direction-brief.md",
               "sha256": "2" * 64}]
    INTENT = {"path": "c/shards/frame/intent.md", "sha256": "3" * 64}

    def none_record(self):
        return RP.build_record(RP.none_decision(frame_route=self.FRAME, briefs=self.BRIEFS, intent=self.INTENT))

    def test_the_none_decision_carries_a_reason_and_the_exact_bytes_are_stable(self):
        record = self.none_record()
        self.assertEqual(record["schema"], "route_decision_v1")
        decision = record["decision"]
        self.assertEqual((decision["selected"], decision["reason"], decision["proposal"]),
                         ("none", "proposal-not-read", None))
        self.assertEqual([p["node"] for p in decision["proposals"]], FRAME_IDS)
        self.assertEqual(decision["frame_route"], self.FRAME)
        self.assertEqual(set(decision), {"frame_route", "selected", "reason", "proposal", "proposals", "briefs",
                                         "intent", "approvals", "first_leg_compose"})
        self.assertNotIn("first_leg", record)
        self.assertEqual(RP.render(record), RP.render(self.none_record()))
        self.assertTrue(RP.render(record).endswith(b"}\n"))
        self.assertEqual(RP.validate_record(json.loads(RP.render(record))), record)
        self.assertRegex(record["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_the_digest_covers_the_decision_part_only(self):
        record = self.none_record()
        with_leg = {**record, "first_leg": {"route_id": "rt-fedcba9876543210"}}
        self.assertEqual(with_leg["digest"], record["digest"])
        selected = RP.build_decision(frame_route=self.FRAME, selected="Option A", reason="", briefs=self.BRIEFS,
                                     intent=self.INTENT, proposal={"legs": [1]}, first_leg_compose={"leg": 0})
        first = RP.build_record(selected)
        bound = RP.bind_first_leg(first, {"route_id": "rt-fedcba9876543210"})
        self.assertEqual(bound["digest"], first["digest"])
        self.assertEqual(RP.validate_record(bound), bound)
        changed = copy.deepcopy(first)
        changed["decision"]["selected"] = "Option B"
        with self.assertRaisesRegex(ValueError, "route-decision-invalid:digest"):
            RP.validate_record(changed)

    def test_first_leg_is_added_once_and_never_changed(self):
        selected = RP.build_record(RP.build_decision(frame_route=self.FRAME, selected="A", reason="",
                                                     briefs=self.BRIEFS, intent=self.INTENT,
                                                     proposal={"legs": [1]}, first_leg_compose={"leg": 0}))
        bound = RP.bind_first_leg(selected, {"route_id": "rt-1", "route_hash": "h"})
        self.assertEqual(RP.bind_first_leg(bound, {"route_id": "rt-1"}), bound)
        self.assertEqual(RP.bind_first_leg(bound, {"start_receipt": {"x": 1}})["first_leg"],
                         {"route_id": "rt-1", "route_hash": "h", "start_receipt": {"x": 1}})
        with self.assertRaisesRegex(ValueError, "route-decision-conflict:first_leg"):
            RP.bind_first_leg(bound, {"route_id": "rt-2"})
        with self.assertRaisesRegex(ValueError, "route-decision-invalid:first_leg"):
            RP.bind_first_leg(self.none_record(), {"route_id": "rt-1"})

    def test_a_malformed_record_is_refused(self):
        record = self.none_record()
        for label, mutate in (
                ("schema", lambda r: r.update(schema="route_decision_v2")),
                ("extra field", lambda r: r.update(extra=1)),
                ("missing digest", lambda r: r.pop("digest")),
                ("none without reason", lambda r: r["decision"].update(reason="")),
                ("none with proposal", lambda r: r["decision"].update(proposal={"a": 1})),
                ("frame route keys", lambda r: r["decision"]["frame_route"].pop("cycle_id")),
                ("decision keys", lambda r: r["decision"].pop("approvals")),
                ("first leg on none", lambda r: r.update(first_leg={"route_id": "rt-1"}))):
            forged = copy.deepcopy(record)
            mutate(forged)
            with self.subTest(label), self.assertRaises(ValueError):
                RP.validate_record(forged)
        with self.assertRaises(ValueError):
            RP.build_decision(frame_route=self.FRAME, selected="none", reason="", briefs=[], intent={})

    def test_read_record_takes_only_a_small_regular_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "route-decision.json"
            target.write_bytes(RP.render(self.none_record()))
            self.assertEqual(RP.read_record(target), self.none_record())
            link = Path(tmp) / "link.json"
            link.symlink_to(target)
            for bad in (link, Path(tmp) / "missing.json"):
                with self.subTest(bad=bad.name), self.assertRaises(ValueError):
                    RP.read_record(bad)
            target.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                RP.read_record(target)
            target.write_bytes(b" " * (RP.MAX_RECORD_BYTES + 1))
            with self.assertRaises(ValueError):
                RP.read_record(target)

    def test_only_the_exact_framed_shape_is_a_framed_route(self):
        with tempfile.TemporaryDirectory():
            base = {"capability": "route-frame", "effective_intensity": "standard",
                    "selection": {"shape": "framed"}, "nodes": [
                        {"id": "frame"}, {"id": "frame-alternative"},
                        {"id": "route-decision", "kind": "runtime-terminal", "terminal": True}]}
            self.assertTrue(RP.is_framed_route(base))
            for label, mutate in (
                    ("capability", lambda r: r.update(capability="autopilot-code")),
                    ("shape", lambda r: r["selection"].update(shape="staged")),
                    ("intensity", lambda r: r.update(effective_intensity="quick")),
                    ("ids", lambda r: r["nodes"].pop(0)),
                    ("kind", lambda r: r["nodes"][2].update(kind="capability-owner")),
                    ("not terminal", lambda r: r["nodes"][2].update(terminal=False))):
                forged = copy.deepcopy(base)
                mutate(forged)
                with self.subTest(label):
                    self.assertFalse(RP.is_framed_route(forged))
            self.assertFalse(RP.is_framed_route(None))
            self.assertFalse(RP.is_framed_route({}))


class EndingBase(FramedBase):
    """A framed route whose frame legs are done: briefs and intent on disk, gates satisfied."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.admitted()
        self.output = self.begin_cycle(self.path)
        self.write_frame_outputs(self.output)
        self.cli_calls = []
        self.gate_calls = []
        patch = mock.patch("dispatch_contract.completion_marker_gate",
                           side_effect=lambda *a, **k: self.gate_calls.append(a[1:3]))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "_route_cli", side_effect=self.in_process_cli)
        patch.start()
        self.addCleanup(patch.stop)
        self.crash_after = None

    def in_process_cli(self, jobs, *argv):
        """`capability-route.py complete|close` without the subprocess; same functions."""
        command = argv[0]
        self.cli_calls.append(command)
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        if command == "complete":
            route = R.verify_route(raw)
            node = next(n for n in route["nodes"] if n["id"] == argv[argv.index("--node") + 1])
            R.complete_node(route, node, node["id"], Path(argv[argv.index("--evidence") + 1]))
        elif command == "close":
            route = R.verify_route(raw, allow_stale_registry=True)
            R.close_route(route, self.path, summary=argv[argv.index("--summary") + 1], allow_unproven=False)
        else:
            raise AssertionError(command)
        if self.crash_after == command:
            raise RuntimeError("crash after " + command)
        return ""

    def settle(self, **kw):
        return W._framed_settle(self.route, self.path, self.jobs, {"route_id": self.route["route_id"]}, **kw)

    def outcome(self):
        target = R.outcome_path(self.path)
        return json.loads(target.read_text(encoding="utf-8")) if target.exists() else None

    def record_path(self):
        return self.output / RP.RECORD_RELATIVE


class FramedEndingTest(EndingBase):
    def test_proceed_with_no_proposal_fixes_a_none_record_and_ends_the_route(self):
        result = self.settle()
        self.assertEqual((result["state"], result["required_action"], result["reason"]),
                         ("completed", "compose-route", "route-decision-none"))
        self.assertEqual((result["selected"], result["decision_reason"]), ("none", "proposal-not-read"))
        self.assertEqual(result["intent_file"], str(self.output / "shards/frame/intent.md"))
        self.assertEqual(result["brief_files"], [str(self.output / "shards" / n / "direction-brief.md")
                                                 for n in FRAME_IDS])
        self.assertEqual(result["record_file"], str(self.record_path()))
        for forbidden in ("next_leg", "parent_next", "parent_next_command", "owner_attempt_id"):
            self.assertNotIn(forbidden, result)
        record = RP.read_record(self.record_path())
        decision = record["decision"]
        self.assertEqual(decision["selected"], "none")
        self.assertEqual(decision["frame_route"], {"route_id": self.route["route_id"],
                                                   "route_hash": self.route["route_hash"],
                                                   "cycle_id": P.read_cycle_record(
                                                       self.root, decision["frame_route"]["cycle_id"])["cycle_id"]})
        self.assertEqual([b["node"] for b in decision["briefs"]], FRAME_IDS)
        self.assertEqual(decision["briefs"][0]["sha256"], RP.file_digest(
            self.output / "shards/frame/direction-brief.md"))
        self.assertEqual(decision["intent"]["sha256"], RP.file_digest(self.output / "shards/frame/intent.md"))
        self.assertFalse(Path(decision["briefs"][0]["path"]).is_absolute())
        self.assertEqual(self.record_path().read_bytes(), RP.render(record))
        self.assertEqual(self.cli_calls, ["complete", "close"])

    def test_the_terminal_marker_is_the_inline_marker_over_the_record_and_the_route_is_proven_closed(self):
        self.settle()
        marker = json.loads((R.completion_dir(self.route["route_id"]) / "route-decision.json").read_text())
        self.assertEqual((marker["node_id"], marker["completion_gate"], marker["registered_worker"],
                          marker["execution_surface"], marker["dispatch_depth"]),
                         ("route-decision", "route-decision", False, "inline", 0))
        self.assertIsNone(marker["attempt_id"])
        self.assertEqual(marker["evidence"]["sha256"], RP.file_digest(self.record_path()))
        outcome = self.outcome()
        self.assertIs(outcome["terminal_gate_proven"], True)
        self.assertEqual(outcome["capability"], "route-frame")
        self.assertIn("framed route ended without a proposal", outcome["summary"])
        self.assertIn("proposal-not-read", outcome["summary"])

    def test_the_producer_cycle_is_sealed_with_the_record_inside_it(self):
        self.settle()
        record = P.list_cycle_records(self.root)[0]
        self.assertEqual(record["state"], "sealed")
        manifest = json.loads((P.cycle_dir(self.root, record["campaign_id"], record["cycle_id"], record)
                               / "manifest.json").read_text(encoding="utf-8"))
        paths = {(row.get("locator") or {}).get("path") for row in manifest["artifact_revisions"]}
        self.assertTrue(any(str(p).endswith("shards/frame/route-decision.json") for p in paths), paths)
        self.assertEqual({row["capability"] for row in manifest["artifacts"]}, {"route-frame"})
        self.assertEqual(manifest["cycle"]["state"], "completed")

    def test_repeating_start_after_the_ending_returns_the_same_ending_and_does_nothing_more(self):
        first = self.settle()
        calls, record_bytes = list(self.cli_calls), self.record_path().read_bytes()
        again = self.settle(closed=self.outcome())
        self.assertEqual(again, first)
        self.assertEqual(self.cli_calls, calls)
        self.assertEqual(self.record_path().read_bytes(), record_bytes)
        self.assertEqual(len(P.list_cycle_records(self.root)), 1)

    def test_every_interruption_is_finished_by_the_next_start_with_one_record_one_marker_one_outcome(self):
        for index, crash in enumerate(("complete", "close")):
            with self.subTest(crash=crash):
                if index:  # a fresh route and cycle for the next interruption point
                    self.route, self.path = self.admitted(slug=f"again-{index}", campaign_key=f"again-{index}")
                    self.output = self.begin_cycle(self.path)
                    self.write_frame_outputs(self.output)
                    self.cli_calls.clear()
                self.crash_after = crash
                with self.assertRaisesRegex(RuntimeError, "crash after " + crash):
                    self.settle()
                first_record = self.record_path().read_bytes()
                self.crash_after = None
                result = self.settle(closed=self.outcome())
                self.assertEqual(result["required_action"], "compose-route")
                self.assertEqual(self.record_path().read_bytes(), first_record)
                self.assertIs(self.outcome()["terminal_gate_proven"], True)
                cycle = next(r for r in P.list_cycle_records(self.root) if r["route_id"] == self.route["route_id"])
                self.assertEqual(cycle["state"], "sealed")
                markers = sorted(R.completion_dir(self.route["route_id"]).glob("route-decision*.json"))
                self.assertEqual(len(markers), 2, markers)  # the canonical marker and its one history row

    def test_a_closed_route_without_a_record_is_reported_and_never_redecided(self):
        self.settle()
        self.record_path().unlink()
        with self.assertRaisesRegex(ValueError, "route-decision-missing"):
            self.settle(closed=self.outcome())

    def test_missing_frame_outputs_end_in_the_existing_needs_attention(self):
        (self.output / "shards/frame-alternative/direction-brief.md").unlink()
        result = self.settle()
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "frame-outcome-needs-inspection"))
        self.assertFalse(self.record_path().exists())
        self.assertEqual(self.cli_calls, [])

    def test_a_fixed_record_is_never_rewritten_even_if_the_inputs_change_afterwards(self):
        self.crash_after = "complete"
        with self.assertRaisesRegex(RuntimeError, "crash after complete"):
            self.settle()
        before = self.record_path().read_bytes()
        (self.output / "shards/frame/intent.md").write_text("edited later\n", encoding="utf-8")
        self.crash_after = None
        self.assertEqual(self.settle()["required_action"], "compose-route")
        self.assertEqual(self.record_path().read_bytes(), before)
        intent = RP.read_record(self.record_path())["decision"]["intent"]
        self.assertNotEqual(intent["sha256"], RP.file_digest(self.output / "shards/frame/intent.md"))

    def test_two_starts_at_once_settle_one_decision(self):
        import threading
        results, errors = [], []

        def run():
            try:
                results.append(self.settle())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["record_file"], results[1]["record_file"])
        self.assertEqual(self.cli_calls.count("complete"), 1)
        self.assertEqual(self.cli_calls.count("close"), 1)


class TerminalCompletionGateTest(FramedBase):
    """`complete` on the terminal checks the record, the two frame legs and the released gate."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.admitted()
        self.output = self.begin_cycle(self.path)
        self.write_frame_outputs(self.output)
        self.node = self.route["nodes"][2]
        cycle = P.list_cycle_records(self.root)[0]
        self.record = RP.build_record(RP.none_decision(
            frame_route={"route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                         "cycle_id": cycle["cycle_id"]},
            briefs=[{"node": n, "path": "x", "sha256": "0" * 64} for n in FRAME_IDS],
            intent={"path": "y", "sha256": "0" * 64}))
        self.evidence = self.output / RP.RECORD_RELATIVE
        self.evidence.write_bytes(RP.render(self.record))

    def marker(self):
        return R.completion_dir(self.route["route_id"]) / "route-decision.json"

    def test_without_two_current_frame_markers_the_terminal_is_not_completed(self):
        with self.assertRaisesRegex(ValueError, "framed-terminal-not-ready"):
            R.complete_node(self.route, self.node, "route-decision", self.evidence)
        self.assertFalse(self.marker().exists())

    def test_the_real_complete_command_refuses_the_same_way(self):
        with self.assertRaisesRegex(ValueError, r"framed-complete-pending: .*framed-terminal-not-ready"):
            W._route_cli(self.jobs, "complete", "--route", str(self.path), "--node", "route-decision",
                         "--evidence", str(self.evidence))
        self.assertFalse(self.marker().exists())

    def test_an_unreleased_frame_review_gate_is_not_completed(self):
        refusal = R.DispatchContractError("human-gate-unreleased", "frame-review")
        with mock.patch("dispatch_contract.completion_marker_gate", side_effect=refusal), \
                self.assertRaisesRegex(ValueError, "framed-terminal-not-ready:human-gate-unreleased"):
            R.complete_node(self.route, self.node, "route-decision", self.evidence)
        self.assertFalse(self.marker().exists())

    def test_a_record_of_another_route_or_a_broken_record_is_not_completed(self):
        foreign = copy.deepcopy(self.record)
        foreign["decision"]["frame_route"]["route_id"] = "rt-ffffffffffffffff"
        foreign["digest"] = RP.decision_digest(foreign["decision"])
        broken = copy.deepcopy(self.record)
        broken["decision"]["reason"] = "tampered"
        for label, payload, expected in (("foreign route", foreign, "route-decision-invalid:frame_route"),
                                         ("digest mismatch", broken, "route-decision-invalid:digest")):
            self.evidence.write_bytes(RP.render(payload))
            with self.subTest(label), mock.patch("dispatch_contract.completion_marker_gate"), \
                    self.assertRaisesRegex(ValueError, expected):
                R.complete_node(self.route, self.node, "route-decision", self.evidence)
            self.assertFalse(self.marker().exists())

    def test_with_the_gate_satisfied_the_marker_is_published_once_and_replays(self):
        with mock.patch("dispatch_contract.completion_marker_gate") as gate:
            first, row = R.complete_node(self.route, self.node, "route-decision", self.evidence)
            second, _row = R.complete_node(self.route, self.node, "route-decision", self.evidence)
        self.assertIsNone(row)
        self.assertEqual(first["evidence"], second["evidence"])
        self.assertEqual(gate.call_args.args[1:3], ("route-decision", "start"))
        self.assertIs(first["registered_worker"], False)
        self.assertTrue(self.marker().is_file())

    def test_a_node_that_is_not_the_framed_terminal_keeps_the_ordinary_completion_path(self):
        other = T.compile_for("direct", self.root, "autopilot-code", "dev", slug="not-framed")
        node = other["nodes"][0]
        evidence = Path(self._tmp.name) / "evidence.txt"
        evidence.write_text("x", encoding="utf-8")
        with mock.patch.object(R, "_complete_framed_terminal", side_effect=AssertionError("framed path")):
            try:
                marker, row = R.complete_node(other, node, node["id"], evidence)
            except ValueError as exc:   # the ordinary path may refuse for its own reasons, never the framed one
                self.assertNotIn("framed", str(exc))
            else:
                self.assertEqual((marker["node_id"], row), ("inline", None))


class FramedAutocloseTest(EndingBase):
    def evidence(self):
        import route_autoclose as A
        instance = object.__new__(A._Evidence)
        instance.root = self.root
        instance.held, instance.owners, instance.gate_roots = set(), {}, []
        instance.resource_routes, instance.resource_paths, instance.open_paths = set(), [], []
        instance.resource_activity = {}
        return instance

    def test_a_fixed_decision_on_an_open_route_is_never_idle_closed(self):
        import route_autoclose as A
        record = P.list_cycle_records(self.root)[0]
        home = A._cycle_home(self.root, record)
        evidence = self.evidence()
        with mock.patch("route_autoclose._waits_on_human", return_value=False):
            self.assertIsNone(evidence.kept(self.route["route_id"], lambda: home, self.route))
            self.crash_after = "complete"
            with self.assertRaisesRegex(RuntimeError, "crash after complete"):
                self.settle()
            self.assertEqual(evidence.kept(self.route["route_id"], lambda: home, self.route), "decision-pending")
            self.assertIsNone(evidence.kept(self.route["route_id"], lambda: home))  # no route: no claim
            staged = R.compose_route(
                capability="autopilot-code", capability_mode="dev", shape="staged", graph="execute,test,report",
                slug="not-framed", cwd=R.ROOT, artifact_root=self.root, spec_read="fixture", unassigned=True,
                dispatch_evidence={"tuples": [T.nested("claude", "codex")]})
            self.assertIsNone(evidence.kept(staged["route_id"], lambda: home, staged))

    def test_the_decision_predicate_needs_the_exact_framed_shape_and_a_record(self):
        import route_autoclose as A
        self.assertFalse(A._decision_unfinished(self.route, lambda: self.output.parent))
        self.write_frame_outputs(self.output)
        self.record_path().parent.mkdir(parents=True, exist_ok=True)
        self.record_path().write_text("{}", encoding="utf-8")
        self.assertTrue(A._decision_unfinished(self.route, lambda: self.output.parent))
        self.assertFalse(A._decision_unfinished({**self.route, "capability": "autopilot-code"},
                                                lambda: self.output.parent))
        self.assertFalse(A._decision_unfinished(self.route, lambda: None))


class FramedStartTest(FramedBase):
    """`start_work` on a real framed route: two separate frame launches, one interview, no owner."""

    def setUp(self):
        super().setUp()
        self.route, self.path = self.admitted()
        self.calls = []
        self.ready = False
        self.released = False
        self.steps = []
        for target, kwargs in (
                (W, {"default_parent_session_id": {"return_value": "parent"}}),):
            for name, options in kwargs.items():
                patch = mock.patch.object(target, name, **options)
                patch.start()
                self.addCleanup(patch.stop)
        for name, options in (
                ("join_selected_attempts", {"side_effect": lambda **kw: {
                    "state": "ready" if self.ready else "timeout", "children": []}}),
                ("completion_marker_gate", {}),
                ("owner_frame_launch_gate", {"side_effect": self.owner_gate}),
                ("current_delivery_state", {"side_effect": self.delivery}),
                ("frame_interview_step", {"side_effect": self.interview_step})):
            patch = mock.patch.object(W, name, **options)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch("dispatch_contract.completion_marker_gate")
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(W, "_route_cli", side_effect=self.in_process_cli)
        patch.start()
        self.addCleanup(patch.stop)
        # These briefs carry no proposal block, so whether this host has PyYAML is controlled too;
        # the PyYAML test below stops this patch and runs against the real probe.
        self.pyyaml = mock.patch.object(RP, "yaml_available", return_value=True)
        self.pyyaml.start()
        self.addCleanup(self.pyyaml.stop)

    def owner_gate(self, *args, **kw):
        if not self.released:
            raise W.DispatchContractError("human-gate-unreleased", "frame-review")

    def delivery(self, jobs, aid, **kw):
        from dispatch_completion_join import CurrentDeliveryState
        return CurrentDeliveryState(marker={"artifact": "/exact/brief.md"}, marker_digest="sha256:marker",
                                    row_revision="1", row_digest="sha256:row", status="done", verdict="PASS",
                                    quiescent=True, owned_children=0, advanced=False, completion_proven=True)

    def interview_step(self, route, path, jobs, **kw):
        step = self.steps.pop(0) if self.steps else {"state": "needs-interview"}
        return step

    def in_process_cli(self, jobs, *argv):
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        if argv[0] == "complete":
            route = R.verify_route(raw)
            node = next(n for n in route["nodes"] if n["id"] == argv[argv.index("--node") + 1])
            R.complete_node(route, node, node["id"], Path(argv[argv.index("--evidence") + 1]))
        else:
            R.close_route(R.verify_route(raw, allow_stale_registry=True), self.path,
                          summary=argv[argv.index("--summary") + 1], allow_unproven=False)
        return ""

    def run_fake(self, command, **kwargs):
        """The selector admits the frame: one registered row per launch, and the cycle begins."""
        self.calls.append(command)
        value = lambda flag, default="": command[command.index(flag) + 1] if flag in command else default
        if len(self.calls) == 1:
            output = self.begin_cycle(self.path)
            self.write_frame_outputs(output)
        meta = {"attempt_id": value("--attempt-id"), "parent_sid": "parent", "launch_started": "1",
                "worker_type": "frame", "route_node": value("--route-node", "owner"),
                "route_id": self.route["route_id"], "route_hash": self.route["route_hash"],
                "parent_completion_delivery": "codex-managed-gateway"}
        with self.jobs.open("a") as stream:
            stream.write("now\topen\t12\tparent\ttask\t" + ",".join(k + "=" + v for k, v in meta.items()) + "\n")
        return subprocess.CompletedProcess(command, 0, "registered=1 started=1 child_spawned=1\n", "")

    def start(self, **kw):
        return W.start_work(self.route, self.path, self.jobs, run=self.run_fake, **kw)

    def test_a164_1_start_launches_the_two_frame_legs_separately_and_no_owner(self):
        first = self.start()
        self.assertEqual(first["state"], "preparing", first)
        self.assertEqual(len(self.calls), 2)
        nodes = [c[c.index("--route-node") + 1] for c in self.calls]
        self.assertEqual(nodes, FRAME_IDS)
        for command in self.calls:
            self.assertIn("--start", command)
            self.assertNotIn("--adapter", command)
            self.assertEqual(command.count("--route-node"), 1)
        self.assertEqual(len({c[c.index("--attempt-id") + 1] for c in self.calls}), 2)
        self.start()
        self.assertEqual(len(self.calls), 2)  # both rows exist: a repeat launches nothing

    def test_the_alternative_launches_once_the_first_leg_is_claimed_not_after_its_whole_launch(self):
        """The alternative reads only the first leg's row (its harness and model): it is launched while
        the first leg's launch still spawns and watches its worker, and the receipts stay in leg order."""
        import threading
        alternative_started, first_returned, seen = threading.Event(), threading.Event(), {}

        def launcher(command, **kwargs):
            node = command[command.index("--route-node") + 1]
            if node == "frame":
                completed = self.run_fake(command)               # the row is claimed here
                kwargs.get("on_claim", lambda: None)()
                seen["first saw the alternative start"] = alternative_started.wait(timeout=10)
                first_returned.set()
                return completed
            seen["alternative saw the first return"] = first_returned.is_set()
            alternative_started.set()
            return self.run_fake(command)

        result = W.start_work(self.route, self.path, self.jobs, run=launcher)
        self.assertEqual(result["state"], "preparing", result)
        self.assertEqual(seen, {"first saw the alternative start": True,
                                "alternative saw the first return": False})
        nodes = [c[c.index("--route-node") + 1] for c in self.calls]
        self.assertEqual(nodes, FRAME_IDS)
        self.assertEqual([launch["attempt_id"] for launch in result["launches"]],
                         [W.attempt_id(self.route, node) for node in FRAME_IDS])
        self.assertEqual(sorted(result["frame_attempts"]),
                         sorted(W.attempt_id(self.route, node) for node in FRAME_IDS))

    def test_a_first_leg_that_is_never_claimed_launches_no_alternative(self):
        """No row, no second leg: the refusal comes back exactly as a sequential start returned it."""
        def refused(command, **kwargs):
            self.calls.append(command)
            return subprocess.CompletedProcess(
                command, 65, "check=failed\nreason=no-eligible-candidate\nchild_spawned=0\n", "")

        result = W.start_work(self.route, self.path, self.jobs, run=refused)
        self.assertEqual((result["state"], result["reason"]), ("needs-attention", "frame-launch-not-admitted"))
        self.assertEqual([c[c.index("--route-node") + 1] for c in self.calls], ["frame"])
        self.assertEqual(len(result["launches"]), 1)

    def test_without_pyyaml_the_start_launches_no_frame_leg_and_names_the_fix(self):
        self.pyyaml.stop()
        with mock.patch.dict(sys.modules, {"yaml": None}):
            result = self.start()
        self.assertEqual((result["state"], result["reason"], result["required_action"]),
                         ("needs-attention", "yaml-unavailable", "install-pyyaml"), result)
        self.assertEqual((self.calls, result["launches"]), ([], []))   # no frame leg, so nothing spent
        self.assertIn("-m pip install --user pyyaml", result["next_step"])
        self.assertIn("atomic, reversible inline change", result["next_step"])
        self.assertIn("--shape direct", result["next_step"])
        self.assertFalse(R.outcome_path(self.path).exists())           # open: resume_command continues it
        with mock.patch.object(RP, "yaml_available", return_value=True):
            self.assertEqual(self.start()["state"], "preparing")
        self.assertEqual(len(self.calls), 2)

    def test_existing_owner_without_pyyaml_skips_frame_processing(self):
        self.pyyaml.stop()
        owner = "att-existing-owner"
        metadata = {"attempt_id": owner, "parent_sid": "parent", "worker_type": "owner",
                    "launch_started": "1", "route_id": self.route["route_id"],
                    "route_hash": self.route["route_hash"]}
        self.jobs.write_text("now\topen\t12\tparent\ttask\t"
                             + ",".join(k + "=" + v for k, v in metadata.items()) + "\n")
        registered = self.jobs.read_bytes()
        receipt = {"state": "running", "owner_attempt_id": owner}
        # The real registry identity and frame selection run; only downstream
        # settlement is controlled, as its lifecycle has separate coverage.
        with mock.patch.dict(sys.modules, {"yaml": None}), \
                mock.patch.object(W, "_framed_settle", return_value=receipt) as settle, \
                mock.patch.object(W, "frame_interview_step", side_effect=AssertionError("new frame processing")):
            result = self.start()
        self.assertEqual((result["state"], result["owner_attempt_id"]), ("running", owner))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.jobs.read_bytes(), registered)
        settle.assert_called_once()
        self.assertEqual(settle.call_args.args[:3], (self.route, self.path, self.jobs))

    def test_the_joined_frames_reach_the_interview_and_the_proceed_answer_ends_the_route(self):
        self.start()
        self.ready = True
        self.steps = [{"state": "needs-interview", "required_action": "prepare-frame-question"}]
        interview = self.start()
        self.assertEqual((interview["state"], interview["required_action"]), ("needs-interview",
                                                                            "prepare-frame-question"))
        self.steps = [{"state": "needs-question", "required_action": "ask-registered-question"}]
        question = self.start()
        self.assertEqual((question["state"], question["required_action"]), ("needs-question",
                                                                          "ask-registered-question"))
        self.assertEqual(question["gate"], "frame-review")
        self.assertEqual(len(self.calls), 2)
        self.steps = [{"state": "released", "decision": "proceed"}]
        self.released = True
        ending = self.start()
        self.assertEqual((ending["state"], ending["required_action"], ending["selected"]),
                         ("completed", "compose-route", "none"))
        self.assertEqual(len(self.calls), 2)  # never an owner launch
        self.assertEqual(ending["frame_results"][0]["classification"], "success")
        self.assertTrue(Path(ending["intent_file"]).is_file())
        self.assertEqual(len(ending["brief_files"]), 2)
        for forbidden in ("next_leg", "parent_next", "parent_next_command"):
            self.assertNotIn(forbidden, ending)
        self.assertEqual(self.outcome()["capability"], "route-frame")
        # the route is closed now: start again is the same ending, nothing launches
        self.steps = []
        replay = self.start()
        self.assertEqual((replay["state"], replay["required_action"], replay["selected"]),
                         ("completed", "compose-route", "none"))
        self.assertEqual(replay["record_file"], ending["record_file"])
        self.assertEqual(len(self.calls), 2)

    def outcome(self):
        return json.loads(R.outcome_path(self.path).read_text(encoding="utf-8"))

    def test_a_stop_or_revise_answer_keeps_their_existing_states_and_ends_nothing(self):
        self.start()
        self.ready = True
        for step in ({"state": "cancelled", "decision": "stop"},
                     {"state": "needs-revision", "decision": "revise", "required_action": "revise-frame-question"}):
            self.steps = [step]
            result = self.start()
            self.assertEqual(result["state"], step["state"], result)
            self.assertNotIn("selected", result)
            self.assertFalse(R.outcome_path(self.path).exists())
        self.assertEqual(len(self.calls), 2)

    def test_a_frame_that_failed_is_not_turned_into_a_none_ending(self):
        self.start()
        self.ready = True

        def failed(jobs, aid, **kw):
            from dispatch_completion_join import CurrentDeliveryState
            return CurrentDeliveryState(marker=None, marker_digest="", row_revision="1", row_digest="sha256:r",
                                        status="done", verdict="FAIL", quiescent=True, owned_children=0,
                                        advanced=False, completion_proven=False)
        with mock.patch.object(W, "current_delivery_state", side_effect=failed):
            result = self.start()
        self.assertEqual(result["state"], "needs-attention", result)
        self.assertFalse(R.outcome_path(self.path).exists())

    def test_the_composed_work_request_text_reaches_each_frame_launch(self):
        self.start()
        for command in self.calls:
            # A frame leg is handed a prompt file: the request first, then hints and the full catalogue.
            self.assertNotIn("--prompt-text", command)
            text = Path(command[command.index("--prompt-file") + 1]).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("Decide how to do this\n"))
            self.assertIn("## Part catalogue", text)
        self.assertEqual(len({c[c.index("--prompt-file") + 1] for c in self.calls}), 1)

    def test_an_autoclosed_framed_route_composes_again_as_a_framed_route(self):
        command = W._compose_again(self.route)
        self.assertIn("--shape framed", command)
        self.assertNotIn("--intensity", command)              # preserve the original unspecified intensity
        self.assertNotIn("--capability", command)
        self.assertNotIn("route-frame", command)
        explicit = json.loads(json.dumps(self.route))
        explicit["selection"]["selection_basis"] = [
            row for row in explicit["selection"]["selection_basis"]
            if row.get("axis") != "compose-intensity"
        ] + [{"axis": "compose-intensity", "signal": "quick", "source": "explicit"}]
        command = W._compose_again(explicit)
        self.assertIn("--intensity quick", command)


if __name__ == "__main__":
    unittest.main()
